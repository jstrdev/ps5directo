"""Motor de PS5 Directo.

- Descarga enlaces HTTP/HTTPS en paralelo (por rangos) y los envia EN ORDEN al
  FTP de la PS5, sin escribir nada en el disco del PC.
- Une archivos troceados (.001, .002 ...) en uno solo mientras los envia.
- Habla con BFpilot (puerto 5905) para extraer RAR/7z/ZIP dentro de la consola,
  listar, mover y borrar.

Solo usa la biblioteca estandar de Python 3.9+.
"""

import ftplib
import http.client
import json
import math
import os
import posixpath
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
MiB = 1024 * 1024


class Cancelled(Exception):
    pass


class PipeError(Exception):
    pass


# ---------------------------------------------------------------------------
# Nombres de archivo
# ---------------------------------------------------------------------------

ARCHIVE_RE = re.compile(
    r"\.(part\d+\.rar|rar|r\d{2,3}|7z|7z\.\d{3}|zip|z\d{2}|tar|tar\.gz|tgz|tar\.bz2|tbz2?|tar\.xz|txz)$", re.I)
TAR_RE = re.compile(r"\.(tar|tar\.gz|tgz|tar\.bz2|tbz2?|tar\.xz|txz)$", re.I)


def is_tar(name):
    return bool(TAR_RE.search(name or ""))
SPLIT_RAW_RE = re.compile(r"^(?P<base>.+)\.(?P<num>\d{3})$")
IMAGE_EXTS = (".exfat", ".ffpkg", ".ffpfs", ".ffpfsc", ".ufs")


def display_name(name):
    """Nombre sin la parte de volumen (conserva mayusculas)."""
    for pat in (r"\.part\d+\.rar$", r"\.7z\.\d{3}$", r"\.r\d{2,3}$", r"\.z\d{2}$",
                r"\.tar\.(gz|bz2|xz)$", r"\.(tgz|tbz2?|txz|tar)$",
                r"\.rar$", r"\.7z$", r"\.zip$", r"\.\d{3}$"):
        new = re.sub(pat, "", name, flags=re.I)
        if new != name:
            return new
    return name


def is_archive(name):
    return bool(ARCHIVE_RE.search(name))


def set_key(name):
    """Nombre comun de un conjunto de volumenes (para agruparlos)."""
    n = name
    for pat in (r"\.part\d+\.rar$", r"\.7z\.\d{3}$", r"\.r\d{2,3}$",
                r"\.z\d{2}$", r"\.tar\.(gz|bz2|xz)$", r"\.(tgz|tbz2?|txz|tar)$",
                r"\.rar$", r"\.7z$", r"\.zip$"):
        new = re.sub(pat, "", n, flags=re.I)
        if new != n:
            return new.lower()
    m = SPLIT_RAW_RE.match(n)
    if m:
        return m.group("base").lower()
    return n.lower()


def volume_order(name):
    """Orden natural de volumenes: part1 < part2 ..., .rar < .r00 < .r01, .001 < .002."""
    m = re.search(r"\.part(\d+)\.rar$", name, re.I)
    if m:
        return int(m.group(1))
    if re.search(r"\.rar$", name, re.I):
        return 0
    m = re.search(r"\.r(\d{2,3})$", name, re.I)
    if m:
        return int(m.group(1)) + 1
    if re.search(r"\.zip$", name, re.I):
        return 10 ** 6
    m = re.search(r"\.z(\d{2})$", name, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\.(\d{3})$", name)
    if m:
        return int(m.group(1))
    return 0


def first_volume(names):
    """El volumen que hay que pasar al extractor."""
    names = list(names)
    for n in names:
        if re.search(r"\.part0*1\.rar$", n, re.I):
            return n
    for n in names:
        if re.search(r"\.rar$", n, re.I) and not re.search(r"\.part\d+\.rar$", n, re.I):
            return n
    for n in names:
        if re.search(r"\.7z\.0*1$", n, re.I) or re.search(r"\.7z$", n, re.I):
            return n
    for n in names:
        if re.search(r"\.zip$", n, re.I):
            return n
    return sorted(names, key=volume_order)[0]


def password_guesses(names):
    """Contrasenas tipicas sacadas del nombre: [WEB.COM] -> WEB.COM, web.com, www.web.com ..."""
    out = []
    for n in names:
        for tag in re.findall(r"[\[\(\{]([^\]\)\}]{3,60})[\]\)\}]", n):
            tag = tag.strip()
            for c in (tag, tag.lower(), tag.upper(), "www." + tag.lower()):
                if c and c not in out:
                    out.append(c)
    return out


def title_id(text):
    # sin \b: en "...-PPSA19733_(02.004.000)" el "_" pegado impedia encontrarlo
    m = re.search(r"(?<![A-Za-z0-9])(PPSA|CUSA)\d{5}(?!\d)", text or "", re.I)
    return m.group(0).upper() if m else None


def safe_name(name):
    name = name.replace("\\", "_").replace("/", "_").strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return name or "archivo"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_SSL = ssl.create_default_context()


# Cabeceras alternativas por servidor. Algunos (Cloudflare) rechazan con 403 un
# programa que dice ser Chrome sin serlo; a esos les decimos la verdad: somos un
# gestor de descargas.
HONEST_UA = "PS5Directo/1.0 (download manager)"
_HOST_MODE = {}


# Lo que la extension del navegador nos cuenta de cada alojamiento: sus cookies (sesion,
# cuenta premium), el navegador y la pagina de origen. Asi el servidor nos trata igual que
# al navegador, como hacen los gestores de descargas. Solo en memoria, nunca en disco.
_BROWSER = {}
_BROWSER_LOCK = threading.Lock()


def set_browser_headers(url, cookie="", ua="", referer=""):
    host = urllib.parse.urlparse(url).netloc.lower()
    if not host:
        return
    info = {}
    if cookie:
        info["Cookie"] = cookie[:8000]
    if ua:
        info["User-Agent"] = ua[:500]
    if referer and re.match(r"^https?://", referer, re.I):
        info["Referer"] = referer[:2000]
    with _BROWSER_LOCK:
        if info:
            _BROWSER[host] = info
        else:
            _BROWSER.pop(host, None)


def _headers(url, extra=None):
    pu = urllib.parse.urlparse(url)
    host = pu.netloc.lower()
    with _BROWSER_LOCK:
        browser = dict(_BROWSER.get(host) or {})
    if _HOST_MODE.get(pu.netloc) == "honest" and not browser:
        h = {"User-Agent": HONEST_UA, "Accept": "*/*"}
    else:
        h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}
        if pu.scheme in ("http", "https") and pu.netloc:
            h["Referer"] = "%s://%s/" % (pu.scheme, pu.netloc)
        h.update(browser)
    if extra:
        h.update(extra)
    return h


def _open(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=_headers(url, headers))
    return urllib.request.urlopen(req, timeout=timeout, context=_SSL)


class _KeepAlive:
    """Una conexion HTTP que se reutiliza para muchos trozos seguidos (como los gestores
    de descargas): sin repetir el saludo TCP/TLS ni el arranque lento de TCP en cada trozo.
    Si hay proxy o el servidor redirige, se usa la via normal."""

    def __init__(self):
        self.conn = None
        self.key = None
        self.used = False
        self.off = False

    @staticmethod
    def usable(url):
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            return False
        try:
            return not urllib.request.getproxies().get(scheme)
        except Exception:
            return False

    def open(self, url, headers, timeout):
        if self.off:
            return None
        pu = urllib.parse.urlparse(url)
        key = (pu.scheme.lower(), pu.netloc)
        if self.conn is None or self.key != key:
            self.close()
            if key[0] == "https":
                self.conn = http.client.HTTPSConnection(pu.netloc, timeout=timeout, context=_SSL)
            else:
                self.conn = http.client.HTTPConnection(pu.netloc, timeout=timeout)
            self.key = key
        path = pu.path or "/"
        if pu.query:
            path += "?" + pu.query
        h = _headers(url, headers)
        h["Connection"] = "keep-alive"
        try:
            self.conn.request("GET", path, headers=h)
            r = self.conn.getresponse()
        except Exception:
            stale = self.used          # el servidor cerro la conexion mientras esperaba:
            self.close()               # es normal, se abre otra sin contarlo como fallo
            if not stale:
                raise
            return self.open(url, headers, timeout)
        self.used = True
        if r.status >= 300:
            hdrs = r.headers
            try:
                r.read(64 * 1024)
            except Exception:
                pass
            self.close()
            if r.status < 400:
                self.off = True       # redirige en cada trozo: que lo lleve urllib
                return None
            raise urllib.error.HTTPError(url, r.status, r.reason, hdrs, None)
        return r

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None
        self.key = None
        self.used = False


def resolve_url(url):
    """Convierte paginas de algunos alojamientos en el enlace directo (lo que se pueda)."""
    url = url.strip()
    m = re.search(r"pixeldrain\.com/u/([A-Za-z0-9]+)", url)
    if m:
        return "https://pixeldrain.com/api/file/%s?download" % m.group(1)
    if "dropbox.com" in url:
        return re.sub(r"([?&])dl=0", r"\1dl=1", url) if "dl=0" in url else (
            url + ("&" if "?" in url else "?") + "dl=1")
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:.*&)?id=)([A-Za-z0-9_-]+)", url)
    if m:
        return ("https://drive.usercontent.google.com/download?id=%s"
                "&export=download&confirm=t" % m.group(1))
    if "mediafire.com/file" in url:
        try:
            with _open(url, timeout=30) as r:
                page = r.read(2 * MiB).decode("utf-8", "replace")
            m = re.search(r'href="(https://download\d*\.mediafire\.com/[^"]+)"', page)
            if m:
                return m.group(1)
        except Exception:
            pass
    return url


def _filename_from_headers(headers, url):
    cd = headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.I)
    if m:
        return urllib.parse.unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I) or \
        re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    if m:
        return m.group(1).strip()
    path = urllib.parse.urlparse(url).path
    return urllib.parse.unquote(path.rsplit("/", 1)[-1]) or "descarga"


class UrlSource:
    kind = "url"

    def __init__(self, url):
        self.original = url.strip()
        self.url = self.original
        self.name = None
        self.size = -1
        self.ranges = False

    def probe(self):
        self.url = resolve_url(self.original)
        last = None
        for attempt in range(3):
            try:
                with _open(self.url, {"Range": "bytes=0-0"}, timeout=30) as r:
                    final = r.geturl()
                    cr = r.headers.get("Content-Range") or ""
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if r.status == 206 and "/" in cr and cr.split("/")[-1].strip().isdigit():
                        self.size = int(cr.split("/")[-1])
                        self.ranges = True
                    else:
                        cl = r.headers.get("Content-Length")
                        self.size = int(cl) if cl and cl.isdigit() else -1
                        self.ranges = False
                    self.name = safe_name(_filename_from_headers(r.headers, final))
                    self.url = final
                if "text/html" in ctype and (self.size < 0 or self.size < 2 * MiB):
                    raise PipeError("El enlace devuelve una pagina web, no un archivo. "
                                    "Usa el enlace de descarga directa.")
                return self
            except PipeError:
                raise
            except urllib.error.HTTPError as e:
                last = "HTTP %s" % e.code
                host = urllib.parse.urlparse(self.url).netloc
                if e.code == 403 and _HOST_MODE.get(host) != "honest":
                    _HOST_MODE[host] = "honest"   # probar identificandonos tal cual
                    continue
                if e.code in (401, 403, 404, 410):
                    break
            except Exception as e:  # red
                last = str(e)
            time.sleep(2 * (attempt + 1))
        hint = ""
        if last == "HTTP 403" and "akirabox.com/download/" in self.original:
            hint = (" -- Akirabox: ese enlace solo vale en el navegador. Empieza la descarga, "
                    "copia el enlace de download.akirabox.com que sale en Ctrl+J (o en tu "
                    "gestor de descargas), cancela y pegalo aqui.")
        elif last == "HTTP 403":
            hint = (" -- El servidor rechaza a este programa (403). Si el enlace ha caducado, "
                    "genera uno nuevo.")
        raise PipeError("No se puede abrir el enlace (%s): %s%s" % (last, self.original, hint))

    # -- lectura ----------------------------------------------------------
    def stream(self, offset, cfg, cancel, meter):
        if self.size >= 0 and offset >= self.size:
            return
        if self.ranges and self.size > 0:
            yield from _ranged(self.url, offset, self.size, cfg, cancel, meter)
        else:
            if offset > 0:
                raise PipeError("El servidor no permite continuar a medias")
            yield from _single(self.url, cancel, meter)


def _single(url, cancel, meter):
    with _open(url, timeout=60) as r:
        while True:
            if cancel.is_set():
                raise Cancelled()
            data = r.read(MiB)
            if not data:
                return
            meter(len(data))
            yield data


def _fetch_range(url, a, b, cancel, meter, tries=6, on_trouble=None, ka=None):
    want = b - a + 1
    last = None
    rng = {"Range": "bytes=%d-%d" % (a, b)}
    for attempt in range(tries):
        if cancel.is_set():
            raise Cancelled()
        got = bytearray()
        try:
            r = ka.open(url, rng, 25) if ka is not None else None
            reused = r is not None
            if r is None:
                r = _open(url, rng, timeout=25)
            try:
                if r.status != 206:
                    raise PipeError("el servidor no respeto el rango pedido")
                while len(got) < want:
                    if cancel.is_set():
                        raise Cancelled()
                    piece = r.read(min(MiB, want - len(got)))
                    if not piece:
                        break
                    got += piece
                    meter(len(piece))
            except BaseException:
                if reused:
                    ka.close()
                raise
            finally:
                if not reused:
                    r.close()
            if len(got) == want:
                if reused and r.will_close:
                    ka.close()        # el servidor no deja reutilizarla
                return bytes(got)
            if reused:
                ka.close()
            last = "respuesta corta (%d de %d bytes)" % (len(got), want)
        except (Cancelled, PipeError):
            raise
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (429, 503, 509) and on_trouble:
                on_trouble(last)
            if e.code == 429:
                time.sleep(10)
        except Exception as e:
            last = str(e)
            if on_trouble:
                on_trouble("corte")
        time.sleep(min(30, 2 ** attempt))
    raise PipeError("Fallo al descargar un trozo: %s" % last)


# Alojamientos que limitan o castigan muchas conexiones a la vez (cuentas gratis,
# enlaces de un solo uso, debrid). Con ellos se usan pocas conexiones y sin rescates.
LIMITED_HOSTS = ("akirabox.", "datanodes.to", "vikingfile.com", "fileditch", "1fichier.com",
                 "rapidgator.net", "ddownload.com", "katfile.com", "nitroflare.com",
                 "torbox", "tb-cdn", "real-debrid", "rdb.gg", "alldebrid", "debrid-link",
                 "premiumize", "pikpak")
LIMITED_CAP = 2


def host_conn_cap(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    return LIMITED_CAP if any(h in host for h in LIMITED_HOSTS) else None


class _Limiter:
    """Limite de velocidad comun a todos los envios (bytes/s; 0 = sin limite)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.t = time.time()
        self.allow = 0.0

    def take(self, n, rate):
        if not rate or rate <= 0:
            return
        while True:
            with self.lock:
                now = time.time()
                self.allow = min(float(rate), self.allow + (now - self.t) * rate)
                self.t = now
                if self.allow >= min(n, rate):
                    self.allow -= n
                    return
                wait = (min(n, rate) - self.allow) / rate
            time.sleep(min(wait, 0.5))


LIMITER = _Limiter()


def _ranged(url, start, end, cfg, cancel, meter):
    chunk = max(1, int(cfg.get("chunk_mb", 8))) * MiB
    workers = max(1, int(cfg.get("streams", 8)))
    log = cfg.get("_log") or (lambda m: None)
    cap = host_conn_cap(url)
    if cap and workers > cap:
        workers = cap
        log("Este alojamiento limita las conexiones: uso %d por archivo" % cap)
    can_hedge = cap is None
    window = max(workers + 1, int(cfg.get("buffer_mb", 256)) * MiB // chunk)
    total = int(math.ceil((end - start) / float(chunk)))
    results = {}
    cond = threading.Condition()
    state = {"next_task": 0, "needed": 0, "error": None, "stop": False,
             "allowed": workers, "active": 0, "trouble_t": 0.0}

    def trouble(kind):
        """El servidor da problemas (429, 503, cortes): usar una conexion menos."""
        with cond:
            now = time.time()
            if state["allowed"] > 1 and now - state["trouble_t"] > 10:
                state["allowed"] -= 1
                state["trouble_t"] = now
                log("El servidor va con problemas (%s): bajo a %d conexiones" % (kind, state["allowed"]))

    use_ka = _KeepAlive.usable(url)

    def worker():
        ka = _KeepAlive() if use_ka else None
        try:
            _worker(ka)
        finally:
            if ka is not None:
                ka.close()

    def _worker(ka):
        while True:
            with cond:
                # recuperar conexiones si lleva un rato sin problemas
                if (state["allowed"] < workers and state["trouble_t"]
                        and time.time() - state["trouble_t"] > 120):
                    state["allowed"] += 1
                    state["trouble_t"] = time.time()
                while (state["active"] >= state["allowed"] and not state["stop"]
                       and not cancel.is_set()):
                    cond.wait(0.5)
                i = state["next_task"]
                if i >= total or state["stop"]:
                    return
                state["next_task"] += 1
                while (i >= state["needed"] + window and not state["stop"]
                       and not cancel.is_set()):
                    cond.wait(0.5)
                if state["stop"] or cancel.is_set():
                    return
                started[i] = time.time()
                state["active"] += 1
            try:
                fetch(i, ka=ka)
            finally:
                with cond:
                    state["active"] -= 1
                    cond.notify_all()

    def fetch(i, hedge=False, ka=None):
        a = start + i * chunk
        b = min(end, a + chunk) - 1
        t0 = time.time()
        try:
            data = _fetch_range(url, a, b, cancel, meter, on_trouble=trouble, ka=ka)
        except Exception as e:
            with cond:
                # si es la copia de rescate, o el original ya llego, no es grave
                if not hedge and i >= state["needed"] and i not in results:
                    state["error"] = e
                cond.notify_all()
            return
        with cond:
            durations.append(time.time() - t0)
            del durations[:-32]
            if i >= state["needed"] and i not in results:
                results[i] = data
            cond.notify_all()

    started = {}
    durations = []
    hedged = set()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(workers, total))]
    for t in threads:
        t.start()
    try:
        for i in range(total):
            with cond:
                while i not in results:
                    if state["error"] is not None:
                        raise state["error"]
                    if cancel.is_set():
                        raise Cancelled()
                    # Un trozo atascado frena todo el envio a la PS5: si tarda mucho
                    # mas de lo normal, se pide otra vez por otra conexion y gana
                    # la que llegue antes (como hacen los gestores de descargas).
                    if can_hedge and i in started and i not in hedged and durations:
                        typical = sorted(durations)[len(durations) // 2]
                        if time.time() - started[i] > max(4.0, 3 * typical):
                            hedged.add(i)
                            threading.Thread(target=fetch, args=(i, True), daemon=True).start()
                    cond.wait(0.5)
                data = results.pop(i)
                state["needed"] = i + 1
                cond.notify_all()
            yield data
    finally:
        with cond:
            state["stop"] = True
            cond.notify_all()


class FileSource:
    kind = "file"

    def __init__(self, path):
        self.path = path.strip().strip('"')
        self.original = self.path
        self.name = safe_name(os.path.basename(self.path))
        self.size = -1
        self.ranges = True

    def probe(self):
        if not os.path.isfile(self.path):
            raise PipeError("No existe el archivo: %s" % self.path)
        self.size = os.path.getsize(self.path)
        return self

    def stream(self, offset, cfg, cancel, meter):
        with open(self.path, "rb") as f:
            f.seek(offset)
            while True:
                if cancel.is_set():
                    raise Cancelled()
                data = f.read(4 * MiB)
                if not data:
                    return
                meter(len(data))
                yield data


class FolderSource:
    """Una carpeta del PC: se manda entera, archivo a archivo, conservando la estructura."""
    kind = "folder"

    def __init__(self, path):
        self.path = os.path.abspath(path.strip().strip('"'))
        self.original = self.path
        self.name = safe_name(os.path.basename(self.path.rstrip("\\/")) or "carpeta")
        self.size = -1
        self.ranges = True
        self.files = []      # [(ruta relativa con /, tamano)]
        self.dirs = []

    def probe(self):
        if not os.path.isdir(self.path):
            raise PipeError("No existe la carpeta: %s" % self.path)
        files, dirs = [], []
        for root, ds, fs in os.walk(self.path):
            ds.sort()
            rel_root = os.path.relpath(root, self.path).replace("\\", "/")
            rel_root = "" if rel_root == "." else rel_root
            for d in ds:
                dirs.append((rel_root + "/" + d).lstrip("/"))
            for f in sorted(fs):
                full = os.path.join(root, f)
                try:
                    files.append(((rel_root + "/" + f).lstrip("/"), os.path.getsize(full)))
                except OSError:
                    pass
        self.files, self.dirs = files, dirs
        self.size = sum(sz for _, sz in files)
        return self


class FtpSource:
    """Un archivo en otro servidor FTP (un NAS, por ejemplo): ftp://usuario:clave@host:puerto/ruta"""
    kind = "url"

    def __init__(self, url):
        self.original = url.strip()
        u = urllib.parse.urlparse(self.original)
        self.host = u.hostname or ""
        self.port = u.port or 21
        self.user = urllib.parse.unquote(u.username or "anonymous")
        self.pw = urllib.parse.unquote(u.password or "")
        self.path = urllib.parse.unquote(u.path or "/")
        self.name = safe_name(posixpath.basename(self.path) or "archivo")
        self.size = -1
        self.ranges = True

    def _connect(self):
        f = ftplib.FTP()
        f.encoding = "utf-8"
        f.connect(self.host, self.port, timeout=30)
        f.login(self.user, self.pw)
        f.set_pasv(True)
        f.voidcmd("TYPE I")
        return f

    def probe(self):
        try:
            f = self._connect()
        except Exception as e:
            raise PipeError("No puedo conectar con el FTP %s:%s (%s)" % (self.host, self.port, e))
        try:
            s = Ftp.size(f, self.path)
            if s is None:
                raise PipeError("No existe en el FTP: %s" % self.path)
            self.size = s
        finally:
            try:
                f.quit()
            except Exception:
                pass
        return self

    def stream(self, offset, cfg, cancel, meter):
        tries = 0
        while True:
            f = None
            try:
                f = self._connect()
                conn = f.transfercmd("RETR " + self.path, rest=offset or None)
                with conn:
                    while True:
                        if cancel.is_set():
                            raise Cancelled()
                        data = conn.recv(MiB)
                        if not data:
                            break
                        offset += len(data)
                        tries = 0
                        meter(len(data))
                        yield data
                try:
                    f.voidresp()
                except Exception:
                    pass
                if self.size >= 0 and offset < self.size:
                    raise PipeError("el FTP corto la descarga")
                return
            except (Cancelled, GeneratorExit):
                raise
            except Exception as e:
                tries += 1
                if tries > int(cfg.get("retries", 8)):
                    raise PipeError("Fallo al leer del FTP %s: %s" % (self.host, e))
                time.sleep(min(30, 2 * tries))
            finally:
                if f is not None:
                    try:
                        f.close()
                    except Exception:
                        pass


def make_source(text):
    text = text.strip().strip('"')
    if re.match(r"^https?://", text, re.I):
        return UrlSource(text)
    if re.match(r"^ftp://", text, re.I):
        return FtpSource(text)
    if os.path.isdir(text):
        return FolderSource(text)
    return FileSource(text)


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                       "oct", "nov", "dec"), 1)}


def _list_mode(perm):
    """'drwxr-xr-x' -> 0o755 (None si no se entiende)."""
    if len(perm) < 10:
        return None
    m = 0
    for i, ch in enumerate(perm[1:10]):
        if ch not in "-":
            m |= 1 << (8 - i)
    return m


def _list_mtime(mon, day, yt):
    import time as _t
    try:
        mo = _MONTHS[mon[:3].lower()]
        d = int(day)
        if ":" in yt:
            hh, mm = (int(x) for x in yt.split(":"))
            now = _t.localtime()
            y = now.tm_year if (mo, d) <= (now.tm_mon, now.tm_mday) else now.tm_year - 1
        else:
            y, hh, mm = int(yt), 0, 0
        return int(_t.mktime((y, mo, d, hh, mm, 0, 0, 0, -1)))
    except Exception:
        return None


def ftp_listdir(f, path):
    """[(nombre, {"type": "file"|"dir", "size": int, "mtime": int|None, "mode": int|None})].
    Usa MLSD tal cual (sin OPTS MLST: ftpsrv responde "504 Option not supported") y, si el
    servidor no tiene MLSD, LIST. Lanza ftplib.error_perm 550 si la carpeta no existe."""
    import time as _t
    out = []
    # Primero se entra en la carpeta y se lista "lo de aqui": ftpsrv no hace caso a la ruta de
    # MLSD/LIST y devolvia la carpeta actual (la raiz de la PS5). Si no existe, CWD da 550.
    f.cwd(path)
    try:
        lines = []
        f.retrlines("MLSD", lines.append)
        for ln in lines:
            facts_s, _, name = ln.partition(" ")
            if not name or name in (".", ".."):
                continue
            facts = {}
            for kv in facts_s.split(";"):
                k, _, v = kv.partition("=")
                if k:
                    facts[k.lower()] = v
            t = facts.get("type", "").lower()
            if t not in ("file", "dir"):
                continue
            mt = facts.get("modify")
            try:
                mt = int(_t.mktime(_t.strptime(mt[:14], "%Y%m%d%H%M%S"))) if mt else None
            except Exception:
                mt = None
            mode = facts.get("unix.mode")
            try:
                mode = int(mode, 8) if mode else None
            except ValueError:
                mode = None
            try:
                size = int(facts.get("size") or 0)
            except ValueError:
                size = 0
            out.append((name, {"type": t, "size": size, "mtime": mt, "mode": mode}))
        return out
    except ftplib.error_perm as e:
        if str(e).startswith("550"):
            raise
    finally:
        try:
            f.voidcmd("TYPE I")
        except Exception:
            pass
    # servidor sin MLSD: LIST al estilo "ls -l"
    lines = []
    try:
        f.retrlines("LIST", lines.append)
    finally:
        try:
            f.voidcmd("TYPE I")
        except Exception:
            pass
    for ln in lines:
        parts = ln.split(None, 8)
        if len(parts) < 9 or parts[8] in (".", ".."):
            continue
        kind = "dir" if ln.startswith("d") else ("file" if ln.startswith("-") else None)
        if not kind:
            continue
        try:
            size = int(parts[4])
        except ValueError:
            size = 0
        out.append((parts[8], {"type": kind, "size": size, "mtime": _list_mtime(parts[5], parts[6], parts[7]),
                               "mode": _list_mode(parts[0])}))
    return out


def ftp_sizes(f, path):
    """{nombre: tamano} de los archivos de una carpeta remota (None si no existe)."""
    try:
        return _ftp_sizes(f, path)
    finally:
        try:
            f.voidcmd("TYPE I")     # MLSD/LIST dejan la conexion en ASCII y REST fallaria
        except Exception:
            pass


def _ftp_sizes(f, path):
    try:
        ents = ftp_listdir(f, path)
    except ftplib.error_perm:
        return None
    return {n: x["size"] for n, x in ents if x["type"] == "file"}


def upload_tree(cfg, src, remote_root, cancel, log, progress, workers=None, on_plan=None):
    """Manda una FolderSource a remote_root. Salta los archivos que ya estan con el mismo
    tamano, continua los que se cortaron (mas pequenos) y vuelve a mandar los demas.
    Usa varias conexiones a la vez (cfg parallel_files). progress(bytes_hechos, ruta_actual).
    on_plan(bytes_que_faltan), si se da, se llama tras comparar y antes de enviar nada
    (para comprobar el espacio libre); puede lanzar una excepcion para no seguir.
    Devuelve (enviados, saltados, continuados)."""
    import queue
    by_dir = {}
    for rel, size in src.files:
        d, _, n = rel.rpartition("/")
        by_dir.setdefault(d, []).append((n, size, rel))
    workers = max(1, min(4, int(workers or cfg.get("parallel_files", 2) or 1)))
    lock = threading.Lock()
    st = {"done": 0, "sent": 0, "skipped": 0, "resumed": 0, "err": None}
    tasks = queue.Queue()
    # 1) una sola conexion: crear carpetas y ver que hay ya en la consola
    w = _FtpWriter(cfg, cancel, log)
    try:
        w.mkdir(remote_root)
        for d in src.dirs:
            w.mkdir(remote_root + "/" + d)
        for d in sorted(by_dir):
            rdir = remote_root + ("/" + d if d else "")
            w.mkdir(rdir)
            have = w._retry(lambda: ftp_sizes(w.f, rdir)) or {}
            for name, size, rel in by_dir[d]:
                got = have.get(name)
                if got == size:
                    st["skipped"] += 1
                    st["done"] += size
                    continue
                offset = got if (got is not None and 0 < got < size) else 0
                st["done"] += offset      # lo que ya habia en la consola cuenta como hecho
                tasks.put((rdir + "/" + name, rel, size, offset))
    finally:
        w.close()
    if on_plan is not None:
        on_plan(max(0, src.size - st["done"]))
    progress(st["done"], "")
    if tasks.empty():
        return 0, st["skipped"], 0

    # 2) varias conexiones subiendo archivos a la vez
    def worker():
        ww = _FtpWriter(cfg, cancel, log)
        try:
            while st["err"] is None:
                try:
                    rpath, rel, size, offset = tasks.get_nowait()
                except queue.Empty:
                    return
                if cancel.is_set():
                    raise Cancelled()
                if offset:
                    with lock:
                        st["resumed"] += 1
                    log("Continuando %s desde %s MB" % (rel, offset // MiB))
                local = os.path.join(src.path, *rel.split("/"))
                ww.begin(rpath, offset)
                pos = offset
                with open(local, "rb") as fh:
                    fh.seek(offset)
                    while True:
                        if cancel.is_set() or st["err"] is not None:
                            raise Cancelled()
                        chunk = fh.read(2 * MiB)
                        if not chunk:
                            break
                        ww.write(chunk)
                        pos += len(chunk)
                        with lock:
                            st["done"] += len(chunk)
                            d = st["done"]
                        progress(d, rel)
                ww.end(size)
                with lock:
                    st["sent"] += 1
        except Exception as e:
            with lock:
                if st["err"] is None:
                    st["err"] = e
        finally:
            ww.close()

    ths = [threading.Thread(target=worker, daemon=True) for _ in range(min(workers, tasks.qsize()))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    if st["err"] is not None:
        if cancel.is_set() or isinstance(st["err"], Cancelled):
            raise Cancelled()
        raise st["err"]
    return st["sent"], st["skipped"], st["resumed"]


def ftp_download(cfg, remote, local, cancel, progress, size=None):
    """Baja un archivo de la PS5 al PC (continua si ya hay un .part)."""
    os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
    part = local + ".part"
    retries = int(cfg.get("retries", 8))
    for attempt in range(retries + 1):
        if cancel.is_set():
            raise Cancelled()
        off = os.path.getsize(part) if os.path.isfile(part) else 0
        f = None
        try:
            f = Ftp(cfg).connect()
            with open(part, "ab") as fh:
                state = {"n": off}

                def cb(data):
                    if cancel.is_set():
                        raise Cancelled()
                    fh.write(data)
                    state["n"] += len(data)
                    progress(state["n"])
                f.retrbinary("RETR " + remote, cb, blocksize=MiB, rest=off or None)
            if size is not None and os.path.getsize(part) != size:
                raise PipeError("descarga incompleta")
            os.replace(part, local)
            return
        except Cancelled:
            raise
        except Exception as e:
            if attempt >= retries:
                raise PipeError("No pude bajar %s: %s" % (remote, e))
            time.sleep(min(30, 2 * (attempt + 1)))
        finally:
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass


def concat_stream(parts, offset, cfg, cancel, meter):
    """Lee varias fuentes como si fueran un unico archivo (para unir .001 .002 ...)."""
    base = 0
    for p in parts:
        if offset < base + p.size:
            yield from p.stream(max(0, offset - base), cfg, cancel, meter)
            offset = base + p.size
        base += p.size


# ---------------------------------------------------------------------------
# FTP de la PS5
# ---------------------------------------------------------------------------

def clean_host(text):
    """Deja solo la IP/nombre de la PS5 aunque se escriba con espacios, ftp://, puerto o
    comas: ' ftp://192,168,1,50:2121/ ' -> ('192.168.1.50', 2121)."""
    t = re.sub(r"[\s\u200b\u00a0\ufeff]+", "", str(text or ""))
    t = re.sub(r"^[a-z][a-z0-9+.-]*://", "", t, flags=re.I)
    t = t.split("/", 1)[0].split("@")[-1]
    port = None
    m = re.match(r"^(.*?):(\d{1,5})$", t)
    if m and m.group(1).count(":") == 0:
        t, port = m.group(1), int(m.group(2))
    if re.fullmatch(r"[\d,.]+", t):
        t = t.replace(",", ".")
    return t.strip(".").strip("[]"), port


def raw_mode(f):
    """ftpsrv (ps5-payload-dev) descifra al vuelo los SELF (eboot.bin, .prx, .sprx): al bajarlos
    llega el ELF de dentro, con otro tamano, y los listados tambien dan ese tamano. Su comando
    SELF lo activa/desactiva en cada conexion: lo dejamos apagado para ver los archivos tal
    cual estan. Los servidores que no lo conocen responden con error y no pasa nada.
    (Gracias a RichyMX por encontrarlo.)"""
    try:
        r = f.sendcmd("SELF")
        if "enabled" in r.lower():
            r = f.sendcmd("SELF")
        return "disabled" in r.lower()
    except ftplib.all_errors:
        return False


class Ftp:
    def __init__(self, cfg):
        self.host = clean_host(cfg["ps5_ip"])[0]
        self.port = int(cfg.get("ftp_port", 2121))
        self.user = cfg.get("ftp_user") or "anonymous"
        self.pw = cfg.get("ftp_pass") or ""

    def connect(self):
        if not self.host:
            raise PipeError("Falta la IP de la PS5: ponla en Ajustes")
        f = ftplib.FTP()
        f.encoding = "utf-8"
        try:
            f.connect(self.host, self.port, timeout=30)
        except socket.gaierror:
            raise PipeError("La IP de la PS5 no es valida (%s): en Ajustes escribe solo los numeros, "
                            "por ejemplo 192.168.1.50, sin ftp:// ni puerto" % self.host)
        f.login(self.user, self.pw)
        f.set_pasv(True)
        f.voidcmd("TYPE I")
        raw_mode(f)
        try:
            f.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass
        return f

    @staticmethod
    def size(f, path):
        try:
            s = f.size(path)
            return int(s) if s is not None else None
        except ftplib.all_errors:
            return None

    @staticmethod
    def mkdirs(f, path):
        cur = ""
        for part in [p for p in path.split("/") if p]:
            cur += "/" + part
            try:
                f.mkd(cur)
            except ftplib.error_perm:
                pass

    def call(self, fn):
        f = self.connect()
        try:
            return fn(f)
        finally:
            try:
                f.quit()
            except Exception:
                try:
                    f.close()
                except Exception:
                    pass


def upload(cfg, remote_final, total, make_iter, progress, cancel, log, resume=True):
    """Envia un flujo a remote_final. Escribe en .ps5part y renombra al final.

    make_iter(offset) devuelve un iterador de bytes desde 'offset'.
    progress(done_bytes) recibe el total enviado.
    Si se corta la conexion, reconecta y continua desde lo que ya hay en la PS5.
    """
    ftp = Ftp(cfg)
    part = remote_final + ".ps5part"
    retries = int(cfg.get("retries", 8))
    attempt = 0
    while True:
        if cancel.is_set():
            raise Cancelled()
        f = None
        try:
            f = ftp.connect()
            Ftp.mkdirs(f, remote_final.rsplit("/", 1)[0])
            if total >= 0 and Ftp.size(f, remote_final) == total:
                log("Ya estaba en la consola: %s" % remote_final)
                progress(total)
                return
            offset = 0
            if resume:
                have = Ftp.size(f, part)
                if have and (total < 0 or have <= total):
                    offset = have
            if total >= 0 and offset == total:
                pass
            else:
                if offset:
                    log("Continuando desde %.1f MB" % (offset / MiB))
                progress(offset)
                conn = None
                if offset:
                    try:
                        conn = f.transfercmd("STOR " + part, rest=offset)
                    except ftplib.error_perm:
                        conn = f.transfercmd("APPE " + part)
                else:
                    conn = f.transfercmd("STOR " + part)
                sent = offset
                try:
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * MiB)
                except Exception:
                    pass
                rate_fn = cfg.get("_limit_bps") or (lambda: 0)
                with conn:
                    for data in make_iter(offset):
                        mv = memoryview(data)
                        for k in range(0, len(mv), MiB):
                            piece = mv[k:k + MiB]
                            LIMITER.take(len(piece), rate_fn())
                            if cancel.is_set():
                                raise Cancelled()
                            conn.sendall(piece)
                            sent += len(piece)
                            progress(sent)
                f.voidresp()
                attempt = 0
            got = Ftp.size(f, part)
            if total >= 0 and got is not None and got != total:
                raise PipeError("Tamano en la consola %s distinto del esperado %s" % (got, total))
            try:
                f.delete(remote_final)
            except ftplib.all_errors:
                pass
            f.rename(part, remote_final)
            log("Enviado: %s" % remote_final)
            return
        except Cancelled:
            raise
        except PipeError as e:
            if "no permite continuar" in str(e):
                log("El servidor no deja continuar; se empieza desde cero")
                resume = False
                try:
                    if f:
                        f.delete(part)
                except Exception:
                    pass
            attempt += 1
            if attempt > retries:
                raise
            log("Problema: %s. Reintento %d/%d" % (e, attempt, retries))
            time.sleep(min(60, 3 * attempt))
        except Exception as e:
            attempt += 1
            if attempt > retries:
                raise PipeError("Conexion con la PS5 perdida: %s" % e)
            log("Conexion cortada (%s). Reintento %d/%d" % (e, attempt, retries))
            time.sleep(min(60, 3 * attempt))
        finally:
            if f is not None:
                try:
                    f.quit()
                except Exception:
                    try:
                        f.close()
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# BFpilot (puerto 5905)
# ---------------------------------------------------------------------------

class BFpilot:
    def __init__(self, cfg):
        self.base = "http://%s:%s" % (clean_host(cfg["ps5_ip"])[0], int(cfg.get("bf_port", 5905)))
        self.cfg = cfg

    def _req(self, method, path, params=None, form=None, timeout=20):
        if not self.cfg.get("ps5_ip"):
            raise PipeError("Falta la IP de la PS5: ponla en Ajustes")
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"User-Agent": "PS5Directo"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                j = json.loads(body.decode("utf-8", "replace"))
                raise PipeError(j.get("error") or "HTTP %s" % e.code)
            except ValueError:
                raise PipeError("BFpilot respondio HTTP %s" % e.code)
        try:
            j = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            raise PipeError("Respuesta rara de BFpilot")
        if isinstance(j, dict) and j.get("ok") is False:
            raise PipeError(j.get("error") or "error de BFpilot")
        return j

    def alive(self):
        try:
            j = self._req("GET", "/api/status", timeout=5)
            return j.get("version") or j.get("tag") or "ok"
        except Exception:
            return None

    def listdir(self, path):
        j = self._req("GET", "/api/fs/list", {"path": path})
        return j.get("entries", [])

    def exists(self, path):
        try:
            self._req("GET", "/api/fs/stat", {"path": path})
            return True
        except PipeError:
            return False

    def mkdir(self, path):
        self._req("GET", "/api/fs/mkdir", {"path": path})

    def rename(self, src, dst):
        self._req("GET", "/api/fs/rename", {"src": src, "dst": dst})

    def chmod(self, path, mode="777"):
        self._req("GET", "/api/fs/chmod", {"path": path, "mode": mode})

    def fix_perms(self, root, cancel=None, log=None):
        """Pone permisos 777 a una carpeta y todo lo que hay dentro.

        El unrar de BFpilot copia los permisos que trae el RAR (666 o 444 si se hizo en
        Windows): sin permiso de ejecucion la PS5 no la arranca (CE-107750-0).
        Devuelve (cambiados, revisados, {nombre: modo_antiguo} de los importantes).
        """
        changed = seen = 0
        notable = {}
        ftp = None
        use_ftp = False

        def do(path, mode_now):
            nonlocal changed, use_ftp, ftp
            if mode_now == 0o777:
                return
            if not use_ftp:
                try:
                    self.chmod(path)
                    changed += 1
                    return
                except PipeError as e:
                    if "endpoint" not in str(e).lower() and "404" not in str(e):
                        raise
                    use_ftp = True
                    if log:
                        log("Este BFpilot no sabe cambiar permisos: lo hago por FTP")
            if ftp is None:
                ftp = Ftp(self.cfg).connect()
            ftp.sendcmd("SITE CHMOD 777 " + path)
            changed += 1

        try:
            do(root, None)
            stack = [root]
            while stack:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                d = stack.pop()
                for e in self.listdir(d):
                    seen += 1
                    path = d + "/" + e["name"]
                    mode = e.get("mode")
                    if e["name"].lower() == "eboot.bin" and mode is not None:
                        notable[path[len(root):].lstrip("/")] = mode
                    do(path, mode)
                    if e.get("dir"):
                        stack.append(path)
        finally:
            if ftp is not None:
                try:
                    ftp.quit()
                except Exception:
                    pass
        return changed, seen, notable

    def read_text(self, path):
        try:
            j = self._req("GET", "/api/fs/text", {"path": path})
            return j.get("text") or j.get("content") or ""
        except Exception:
            return ""

    def wait_job(self, cancel, timeout=3600, on_tick=None):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if on_tick is not None:
                try:
                    on_tick()
                except Exception:
                    pass
            j = self._req("GET", "/api/fs/job/status")
            if not j.get("busy"):
                if j.get("error"):
                    raise PipeError(j.get("error"))
                return
            time.sleep(1)
        raise PipeError("BFpilot tarda demasiado")

    def delete(self, path, cancel=None, on_tick=None):
        for _ in range(60):
            try:
                self._req("POST", "/api/fs/delete", form={"path": path, "recursive": "1"})
                break
            except PipeError as e:
                if "already running" in str(e) or "another" in str(e):
                    time.sleep(2)
                    continue
                if "No such file" in str(e) or "not found" in str(e).lower():
                    return
                raise
        self.wait_job(cancel, timeout=4 * 3600, on_tick=on_tick)

    def du(self, path, timeout=20):
        """Tamano total de una carpeta (o archivo), o None."""
        try:
            return int(self._req("GET", "/api/fs/du", {"path": path}, timeout=timeout).get("size") or 0)
        except Exception:
            return None

    def places(self):
        try:
            return self._req("GET", "/api/fs/places")
        except Exception:
            return {}

    def extract(self, src, dst, password, cancel, on_status, threads=0):
        form = {"src": src, "dst": dst, "password": password or "",
                "threads": str(max(0, min(8, int(threads or 0)))), "cleanupPartial": "1"}
        for _ in range(120):
            try:
                self._req("POST", "/api/fs/archive/prepare", form=form)
                break
            except PipeError as e:
                if "already running" in str(e) or "another" in str(e):
                    on_status({"state": "esperando", "current": "BFpilot ocupado con otra tarea"})
                    time.sleep(5)
                    continue
                raise
        misses = 0
        while True:
            if cancel.is_set():
                try:
                    self._req("GET", "/api/fs/job/cancel")
                except Exception:
                    pass
                raise Cancelled()
            try:
                j = self._req("GET", "/api/fs/archive/status")
                misses = 0
            except Exception as e:
                misses += 1
                if misses > 120:
                    raise PipeError("BFpilot ha dejado de responder: %s" % e)
                time.sleep(2)
                continue
            mine = (j.get("source") == src and j.get("destination") == dst)
            state = j.get("state")
            if mine:
                on_status(j)
                if state == "done":
                    return j
                if state == "error":
                    raise PipeError(j.get("error") or "la extraccion fallo")
            time.sleep(1)


def is_password_error(msg):
    m = (msg or "").lower()
    return "password" in m or "checksum" in m or "crc" in m


# ---------------------------------------------------------------------------
# Descomprimir en el PC con 7-Zip, sobre la marcha, y mandar los archivos sueltos
# ---------------------------------------------------------------------------
#
# 7-Zip saca TODOS los archivos seguidos por su salida estandar (x -so), en el
# mismo orden que su listado. Con el listado (nombres, tamanos y CRC) cortamos ese
# chorro en archivos y los subimos uno a uno por FTP. No se escribe nada
# descomprimido en el disco del PC, y cada archivo se comprueba con su CRC.

import subprocess
import zlib
from collections import deque


def find_7z(custom=""):
    cands = []
    if custom:
        c = custom.strip().strip('"')
        cands.append(os.path.join(c, "7z.exe") if os.path.isdir(c) else c)
    for env in ("PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            cands.append(os.path.join(root, "7-Zip", "7z.exe"))
            cands.append(os.path.join(root, "Programs", "7-Zip", "7z.exe"))
    import shutil
    for n in ("7z", "7zz", "7z.exe"):
        w = shutil.which(n)
        if w:
            cands.append(w)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


_NOWIN = 0x08000000 if os.name == "nt" else 0      # CREATE_NO_WINDOW


def _7z_cmd(exe, *args, password=None):
    cmd = [exe] + list(args) + ["-sccUTF-8", "-bd", "-y"]
    if password:
        cmd.append("-p" + password)
    return cmd


def _pw_error(text):
    t = (text or "").lower()
    return ("wrong password" in t or "encrypted" in t or "cannot read password" in t
            or "can not read password" in t or "break signaled" in t)


def list_7z(exe, archive, password=None):
    """Devuelve (entradas, propiedades del archivo). Cada entrada: path, size, dir, crc, enc."""
    p = subprocess.run(_7z_cmd(exe, "l", "-slt", archive, password=password),
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       creationflags=_NOWIN, timeout=600)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0 or "----------" not in out:
        msg = (err.strip() or out.strip()[-400:] or "codigo %s" % p.returncode)
        raise PipeError("7-Zip no pudo leer el archivo: %s" % msg[-400:])
    head, body = out.split("----------", 1)
    props = {}
    for line in head.splitlines():
        if " = " in line:
            k, v = line.split(" = ", 1)
            props[k.strip()] = v.strip()
    entries = []
    for block in re.split(r"\r?\n\s*\r?\n", body):
        d = {}
        for line in block.splitlines():
            if " = " in line:
                k, v = line.split(" = ", 1)
                d.setdefault(k.strip(), v.strip())
            elif line.endswith(" ="):
                d.setdefault(line[:-2].strip(), "")
        if "Path" not in d:
            continue
        attrs = d.get("Attributes", "")
        is_dir = d.get("Folder") == "+" or attrs.startswith("D")
        path = d["Path"].replace("\\", "/").strip("/")
        parts = [x for x in path.split("/") if x not in ("", ".")]
        if not parts or ".." in parts:
            raise PipeError("Ruta peligrosa dentro del comprimido: %s" % d["Path"])
        if d.get("Hard Link") or d.get("Symbolic Link"):
            raise PipeError("El comprimido tiene enlaces (%s); eso solo lo sabe extraer BFpilot. "
                            "Descomprimelo en la PS5." % path)
        if d.get("Alternate Stream") == "+":
            continue
        crc = d.get("CRC") or ""
        entries.append({
            "path": "/".join(parts), "dir": is_dir,
            "size": 0 if is_dir else int(d.get("Size") or 0),
            "crc": int(crc, 16) if re.fullmatch(r"[0-9A-Fa-f]{8}", crc) else None,
            "enc": d.get("Encrypted") == "+",
        })
    return entries, props


def needs_password(entries):
    return any(e["enc"] for e in entries)


class _FtpWriter:
    """Una conexion FTP abierta para subir muchos archivos seguidos, con reconexion.

    Guarda los ultimos MB enviados del archivo en curso: si se corta la conexion,
    pregunta a la PS5 cuanto le llego y reenvia desde ahi sin volver a descomprimir.
    """
    KEEP = 64 * MiB

    def __init__(self, cfg, cancel, log):
        self.cfg, self.cancel, self.log = cfg, cancel, log
        self.ftp = Ftp(cfg)
        self.f = None
        self.conn = None
        self.dirs = set()
        self.retries = int(cfg.get("retries", 8))
        self.rate_fn = cfg.get("_limit_bps") or (lambda: 0)

    def _connect(self):
        self.close()
        self.f = self.ftp.connect()
        self.dirs = set()

    def mkdir(self, path):
        if path in self.dirs:
            return
        self._retry(lambda: Ftp.mkdirs(self.f, path))
        cur = ""
        for part in [p for p in path.split("/") if p]:
            cur += "/" + part
            self.dirs.add(cur)

    def _retry(self, fn):
        attempt = 0
        while True:
            if self.cancel.is_set():
                raise Cancelled()
            try:
                if self.f is None:
                    self._connect()
                return fn()
            except (Cancelled, PipeError):
                raise
            except Exception as e:
                attempt += 1
                self.close()
                if attempt > self.retries:
                    raise PipeError("Conexion con la PS5 perdida: %s" % e)
                self.log("Conexion cortada (%s). Reintento %d/%d" % (e, attempt, self.retries))
                time.sleep(min(30, 3 * attempt))

    def _open_data(self, path, offset):
        if offset:
            c = self.f.transfercmd("STOR " + path, rest=offset)
        else:
            c = self.f.transfercmd("STOR " + path)
        try:
            c.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * MiB)
        except Exception:
            pass
        return c

    def begin(self, path, offset=0):
        self.path = path
        self.sent = offset
        self.tail = deque()
        self.tail_len = 0
        self.mkdir(path.rsplit("/", 1)[0] or "/")
        self.conn = self._retry(lambda: self._open_data(path, offset))

    def _remember(self, data):
        self.tail.append(data)
        self.tail_len += len(data)
        while self.tail and self.tail_len - len(self.tail[0]) >= self.KEEP:
            self.tail_len -= len(self.tail.popleft())

    def _recover(self, why):
        """Reconecta y reenvia lo que no llego, desde la copia de los ultimos MB."""
        attempt = 0
        while True:
            attempt += 1
            if attempt > self.retries:
                raise PipeError("Conexion con la PS5 perdida: %s" % why)
            self.log("Conexion cortada (%s). Reintento %d/%d" % (why, attempt, self.retries))
            time.sleep(min(30, 3 * attempt))
            if self.cancel.is_set():
                raise Cancelled()
            try:
                self._connect()
                have = Ftp.size(self.f, self.path) or 0
                start = self.sent - self.tail_len
                if have < start or have > self.sent:
                    raise PipeError("se perdio demasiado del archivo %s; empieza otra vez" % self.path)
                self.conn = self._open_data(self.path, have)
                skip = have - start
                for chunk in self.tail:
                    if skip >= len(chunk):
                        skip -= len(chunk)
                        continue
                    self.conn.sendall(memoryview(chunk)[skip:])
                    skip = 0
                return
            except (Cancelled, PipeError):
                raise
            except Exception as e:
                why = str(e)

    def write(self, data):
        mv = memoryview(data)
        for k in range(0, len(mv), MiB):
            piece = mv[k:k + MiB]
            LIMITER.take(len(piece), self.rate_fn())
            if self.cancel.is_set():
                raise Cancelled()
            chunk = bytes(piece)
            self.sent += len(chunk)
            self._remember(chunk)
            try:
                self.conn.sendall(chunk)
            except Exception as e:
                self._recover(str(e))

    def end(self, size):
        for attempt in range(3):
            try:
                self.conn.close()
                self.conn = None
                self.f.voidresp()
                got = Ftp.size(self.f, self.path)
                if got is not None and got != size:
                    raise PipeError("tamano en la consola %s distinto del esperado %s" % (got, size))
                return
            except PipeError:
                raise
            except Exception as e:
                self._recover(str(e))
        raise PipeError("No pude cerrar %s en la PS5" % self.path)

    def close(self):
        for o in (self.conn, self.f):
            if o is not None:
                try:
                    o.close()
                except Exception:
                    pass
        self.conn = self.f = None


def stream_extract_7z(exe, archive, entries, password, remote_root, cfg, cancel, log,
                      progress, on_file=None, skip=0, capture=None):
    """Descomprime con 7-Zip hacia la salida estandar y sube cada archivo a remote_root.

    skip: cuantas entradas iniciales ya estaban subidas (se leen y se descartan).
    progress(bytes_subidos_en_total, ruta_actual). on_file(indice_hecho).
    Devuelve el numero de entradas hechas.
    """
    proc = subprocess.Popen(_7z_cmd(exe, "x", "-so", "-bb0", "-bsp0", archive, password=password),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_NOWIN, bufsize=0)
    errbuf = []
    threading.Thread(target=lambda: errbuf.append(proc.stderr.read()), daemon=True).start()
    stop_watch = threading.Event()

    def watch_cancel():
        while not stop_watch.wait(0.5):
            if cancel.is_set():
                try:
                    proc.kill()
                except Exception:
                    pass
                return
    threading.Thread(target=watch_cancel, daemon=True).start()

    def err_text():
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        time.sleep(0.2)
        return b"".join(x for x in errbuf if x).decode("utf-8", "replace").strip()

    w = _FtpWriter(cfg, cancel, log)
    done_bytes = sum(e["size"] for e in entries[:skip])
    i = 0
    try:
        for i, e in enumerate(entries):
            remote = remote_root + "/" + e["path"]
            if e["dir"]:
                if i >= skip:
                    w.mkdir(remote)
                    if on_file:
                        on_file(i + 1)
                continue
            left = e["size"]
            crc = 0
            upload = i >= skip
            keep = [] if capture is not None and e["path"] in capture and e["size"] < MiB else None
            if upload:
                w.begin(remote)
            while left > 0:
                data = proc.stdout.read(min(4 * MiB, left))
                if cancel.is_set():
                    raise Cancelled()
                if not data:
                    msg = err_text()
                    if _pw_error(msg):
                        raise PipeError("7-Zip: contrasena incorrecta (%s)" % msg[-200:])
                    raise PipeError("7-Zip se paro en %s: %s" % (e["path"], msg[-300:] or "sin datos"))
                left -= len(data)
                crc = zlib.crc32(data, crc)
                if keep is not None:
                    keep.append(data)
                if upload:
                    w.write(data)
                    done_bytes += len(data)
                    progress(done_bytes, e["path"])
            if e["crc"] is not None and crc != e["crc"]:
                raise PipeError("El archivo %s no cuadra con su CRC (datos danados o contrasena "
                                "incorrecta)" % e["path"])
            if keep is not None:
                capture[e["path"]] = b"".join(keep)
            if upload:
                w.end(e["size"])
                if on_file:
                    on_file(i + 1)
        extra = proc.stdout.read(1)
        rc = proc.wait(timeout=120)
        if cancel.is_set():
            raise Cancelled()
        if extra or rc != 0:
            msg = err_text()
            raise PipeError("7-Zip termino con errores (codigo %s): %s" % (rc, msg[-300:]))
        return len(entries)
    finally:
        stop_watch.set()
        w.close()
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


class _CountReader:
    """Envuelve un archivo y cuenta lo leido (para el progreso del .tar.gz)."""
    def __init__(self, f):
        self.f, self.n = f, 0

    def read(self, k=-1):
        d = self.f.read(k)
        self.n += len(d)
        return d


def stream_extract_tar(path, remote_root, cfg, cancel, log, progress, on_file=None, skip=0):
    """Descomprime un .tar / .tar.gz / .tar.bz2 / .tar.xz del PC sobre la marcha y sube cada
    archivo a remote_root. progress(bytes_del_comprimido_leidos, total_comprimido, ruta).
    skip: miembros ya subidos antes (se leen y se descartan).
    Devuelve (archivos, bytes, {ruta de sce_sys/param.json: contenido})."""
    import tarfile
    total = os.path.getsize(path)
    nfiles = nbytes = 0
    params = {}
    w = _FtpWriter(cfg, cancel, log)
    try:
        with open(path, "rb") as raw:
            cr = _CountReader(raw)
            with tarfile.open(fileobj=cr, mode="r|*") as tf:
                for i, m in enumerate(tf):
                    if cancel.is_set():
                        raise Cancelled()
                    parts = [x for x in m.name.replace("\\", "/").split("/") if x not in ("", ".")]
                    if not parts or ".." in parts:
                        raise PipeError("Ruta peligrosa dentro del comprimido: %s" % m.name)
                    remote = remote_root + "/" + "/".join(parts)
                    rel = "/".join(parts)
                    if m.isfile() and rel.lower().endswith("sce_sys/param.json") and m.size < MiB:
                        params[rel] = tf.extractfile(m).read()
                        if i < skip:
                            continue
                        w.begin(remote)
                        w.write(params[rel])
                        w.end(m.size)
                        nfiles += 1
                        nbytes += m.size
                        if on_file:
                            on_file(i + 1)
                        continue
                    if i < skip:
                        continue
                    if m.isdir():
                        w.mkdir(remote)
                    elif m.isfile():
                        fh = tf.extractfile(m)
                        w.begin(remote)
                        while True:
                            data = fh.read(4 * MiB)
                            if not data:
                                break
                            w.write(data)
                            progress(cr.n, total, m.name)
                        w.end(m.size)
                        nfiles += 1
                        nbytes += m.size
                    else:
                        log("Me salto %s (enlace o archivo especial)" % m.name)
                    if on_file:
                        on_file(i + 1)
                    progress(cr.n, total, m.name)
    except tarfile.TarError as e:
        raise PipeError("El .tar esta danado o no es un tar: %s" % e)
    finally:
        w.close()
    return nfiles, nbytes, params
