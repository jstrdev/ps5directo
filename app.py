"""PS5 Directo: transfiere archivos a la consola, los descomprime y los deja en su sitio.

Arranque:  python app.py   (o doble clic en "Iniciar PS5 Directo.bat")
"""

import ftplib
import hashlib
import io
import json
import re
import os
import posixpath
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# Dentro de la app de Android, modo «Usar sin PC»: el movil hace de PC (sin 7-Zip ni Dump Forge: descomprime
# BFpilot y convierte Alembic, en la consola)
PHONE = os.environ.get("PS5D_PHONE") == "1"

# Mac: abierto desde el Finder, el PATH no incluye Homebrew ni MacPorts, y no se encontrarian
# aria2, ffmpeg o rclone instalados con ellos (en Linux se anade ~/.local/bin, de pip y similares)
if sys.platform == "darwin" or sys.platform.startswith("linux"):
    _extra = (["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"] if sys.platform == "darwin"
              else [os.path.expanduser("~/.local/bin")])
    _have = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(_have + [d for d in _extra if os.path.isdir(d) and d not in _have])
    # el Python incluido busca los certificados de las webs en /etc/ssl; si en esta distribucion estan
    # en otro sitio (Fedora, openSUSE...), se le dice donde, para que las descargas https no fallen
    try:
        import ssl as _ssl
        _vp = _ssl.get_default_verify_paths()
        if not os.environ.get("SSL_CERT_FILE") and not (_vp.cafile and os.path.isfile(_vp.cafile)) and \
                not (_vp.capath and os.path.isdir(_vp.capath) and os.listdir(_vp.capath)):
            for _c in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                       "/etc/ssl/ca-bundle.pem", "/etc/pki/tls/cacert.pem", "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"):
                if os.path.isfile(_c):
                    os.environ["SSL_CERT_FILE"] = _c
                    break
            else:
                if os.path.isdir("/system/etc/security/cacerts"):        # Android
                    os.environ["SSL_CERT_DIR"] = "/system/etc/security/cacerts"
    except Exception:
        pass
sys.path.insert(0, HERE)
import engine as E  # noqa: E402
import features as F  # noqa: E402

VERSION = "2.0.6"
# Repositorio de GitHub donde se publican las versiones (usuario/nombre). Si lo rellenas,
# todas las copias buscan actualizaciones ahi sin tener que configurar nada.
DEFAULT_REPO = "jstrdev/ps5directo"
DATA_DIR = os.path.join(os.path.expanduser("~"), ".ps5directo")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
WORK_ROOT = "/data/ps5directo"          # fuera de las rutas que escanea ShadowMountPlus

DEFAULTS = {
    "ps5_ip": "",
    "ftp_port": 2121,
    "ftp_user": "anonymous",
    "ftp_pass": "",
    "bf_port": 5905,
    "dest": "/data/homebrew",
    "streams": 8,
    "parallel_files": 2,
    "limit_mbps": 0,          # MB/s maximos hacia la PS5 (0 = sin limite)
    "space_margin_gb": 25,    # GB que siempre se dejan libres en la PS5
    "update_repo": "",        # "usuario/repositorio" de GitHub para buscar actualizaciones
    "extract_threads": 0,     # hilos de BFpilot al descomprimir (0 = automatico, max 8)
    "extract_where": "ps5",   # "ps5" = BFpilot en la consola; "pc" = 7-Zip en el PC sobre la marcha
    "pc_temp_dir": "",        # carpeta del PC donde bajar los RAR en modo "pc" (vacio = Descargas)
    "sevenzip_path": "",      # ruta de 7z.exe (vacio = buscarlo)
    "chunk_mb": 4,
    "buffer_mb": 256,
    "retries": 8,
    "passwords": [],
    "guess_passwords": True,
    "delete_archives": True,
    "trash_on": True,          # Biblioteca y Archivos: «Borrar» manda a la papelera de la consola (se recupera)
    "trash_days": 7,           # lo de la papelera se borra solo pasados estos dias
    "watch_since": 0.0,       # lo de la carpeta vigilada anterior a esto ya se vio
    "shots_auto": False,      # bajar solas al PC las capturas y videos nuevos (Descargas/PS5 Directo/Capturas/<titulo>)
    "low_prio": False,        # «Usar menos el PC»: Forge y 7-Zip con prioridad baja (va mas lento, el PC sigue agil)
    "shots_send": "",         # y ademas mandarlas a: "tg" (Telegram), "dc" (Discord), "both" o "" (a ningun sitio)
    "shots_tg_token": "",     # bot de Telegram (lo creas tu con @BotFather)
    "shots_tg_chat": "",      # chat, grupo o canal de Telegram donde publica el bot
    "shots_dc_hook": "",      # webhook de un canal de Discord
    "shots_route": {},        # Title ID -> "tg", "dc", "both" o "none" (si no, el de shots_send)
    "zftpd_dl": False,        # «Descargar en la PS5»: la propia consola baja el enlace con zftpd (sin pasar por el PC)
    "watch_dir": "",          # carpeta vigilada: lo que termine de bajar ahi (FDM, JDownloader...) llega a la caja
    "clip_watch": False,       # captar los enlaces de descarga que copies (van a la caja de enlaces)
    "pc_mode": False,          # «Modo PC»: todo va a tu PC (bajar, descomprimir o convertir), nada a la consola
    "pc_mode_steps": "extract",  # en Modo PC: dl, extract o convert (lo que usan la extension y el movil)
    "pc_mode_fmt": "",         # en Modo PC con convert: formato
    "forge_auto_update": False,  # bajar solo las versiones nuevas de PS5 Dump Forge (si no, avisa y pregunta)
    "tour_done": False,       # la guia de bienvenida ya se vio
    "tour20_done": False, "spot_packs": False, "spot_ver": "", "spot_done": "", "spot_hint_x": "", "rec_src": {}, "ps5_toasts": True, "chiaki_path": "", "rp_nick": "",     # la guia de la 2.0 (lo nuevo) ya se vio
    "seen_version": "",       # ultima version cuyas novedades ya se ensenaron
    "shortcut_done": False,   # ya se creo el acceso directo del escritorio una vez
    "icon_ver": "",           # version cuyo icono ya se dejo en la cache de la ventana
    # 1.20
    "elf_port": 9021,         # cargador de payloads de la consola (etaHEN / elfldr)
    "auto_payloads": True,    # al abrir, iniciar solos ftpsrv/BFpilot con Payload Manager si faltan
    "console_fw": "",
    "fw_guard": True,         # Guardian del firmware: avisa si la consola baja una actualizacion del sistema
    "fw_guard_auto": False,   # y la quita sola
    "bug_ask": True,          # proponer el informe cuando el programa detecta un fallo suyo         # firmware de tu PS5 (p. ej. 13.60) para avisar de lo que no arrancara
    "dup_check": True,        # preguntar antes de enviar algo que ya esta en la consola
    "verify_sums": True,      # comprobar .sfv/.md5/.sha1 si vienen con las partes
    "pc_format": "folder",    # en el PC: folder, ffpkg, ffpfsc, lz4, lz4ffpkg, zstd, zstdffpkg (con PS5 Dump Forge)
    "forge_path": "",         # ruta de ps5-dump-forge.exe (vacio = descargarlo solo)
    "keep_awake": True,       # que el PC no se suspenda mientras hay envios
    "welcome_seen": False,    # la ventana de bienvenida ya se ha visto
    "pc_collection": [],      # carpetas del PC o del NAS con contenido para la consola (pestana Biblioteca > Tu PC)
    "pc_library_dir": "",     # donde llega lo que bajas de la consola desde la Biblioteca (vacio = Descargas/PS5 Directo/Biblioteca)
    "backup_dir": "",         # copias de seguridad de la consola (vacio = Documentos\\PS5 Directo\\Copias)
    "backup_auto": False,     # una copia al dia, sola, si la consola esta encendida
    "backup_keep": 10,        # cuantas copias guardar
    "backup_saves": True, "backup_config": True, "backup_appdb": True, "backup_emu": True,
    "lan_access": False,      # abrir la app desde el movil (misma red)
    "lan_port": 8780,
    "lan_token": "",
    "fmt_notice_seen": False, # ya se vio el aviso de que convertir ocupa sitio en el PC
}

LOCK = threading.RLock()
SETTINGS = dict(DEFAULTS)
JOBS = []                # lista de dicts (orden de cola)
CANCELS = {}             # id -> threading.Event
WAKE = threading.Condition(LOCK)
EXTRACT_Q = []           # ids pendientes de extraer


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------

def _save_json(path, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_state():
    global JOBS
    load_notifs()
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            SETTINGS.update(json.load(f))
    except Exception:
        pass
    # 1.8.1: la 1.8.0 puso 1 hilo por error; volver a automatico una sola vez
    if SETTINGS.get("_threads_v18") and not SETTINGS.get("_threads_v181"):
        if int(SETTINGS.get("extract_threads") or 0) == 1:
            SETTINGS["extract_threads"] = 0
    SETTINGS["_threads_v181"] = True
    # 1.28.0: las versiones nuevas de PS5 Dump Forge se avisan con sus novedades y se actualiza cuando quieras
    if not SETTINGS.get("_forge_ask_v128"):
        SETTINGS["forge_auto_update"] = False
        SETTINGS["_forge_ask_v128"] = True
    try:
        with open(JOBS_FILE, encoding="utf-8") as f:
            JOBS = json.load(f)
    except Exception:
        JOBS = []
    for j in JOBS:
        if j["status"] not in ("hecho", "error", "cancelado", "pausado"):
            j["status"] = "error"
            j["error"] = "Se cerro el programa a medias. Pulsa Reintentar: continua donde se quedo."
    sc = SETTINGS.get("sched") or {}
    try:
        if float(sc.get("start") or 0) > time.time():
            SCHED["start"] = float(sc["start"])
        if sc.get("after") in ("sleep", "shutdown"):
            SCHED["after"] = sc["after"]
    except (TypeError, ValueError):
        pass


def save_jobs():
    with LOCK:
        _save_json(JOBS_FILE, JOBS)


def save_settings():
    with LOCK:
        _save_json(SETTINGS_FILE, SETTINGS)


# ---------------------------------------------------------------------------
# Utilidades de trabajos
# ---------------------------------------------------------------------------

COMP_LEVELS = ("fast", "balanced", "smallest", "stored")


def new_job(lines, password, dest, title=None, where=None, raw=False, fmt=None, comp=None, pcdir="", keep_steps=""):
    return {
        "pcdir": pcdir or "",
        "keep_steps": keep_steps if keep_steps in ("dl", "extract", "convert") else "",
        "raw": bool(raw),
        "fmt": fmt if fmt in F.FORGE_FORMATS else "",
        "comp": comp if comp in COMP_LEVELS else "",
        "where": where if where in ("ps5", "pc", "keep", "zdl") else "",
        "id": secrets.token_hex(4),
        "created": time.time(),
        "title": title or "",
        "lines": lines,
        "sources": [],
        "password": password or "",
        "dest": dest,
        "status": "pendiente",
        "phase": "En cola",
        "sent": 0, "total": -1, "speed": 0.0,
        "extract_pct": 0, "extract_text": "",
        "error": "", "result": [], "log": [],
    }


def find(job_id):
    for j in JOBS:
        if j["id"] == job_id:
            return j
    return None


def jlog(job, msg):
    line = time.strftime("%H:%M:%S ") + msg
    with LOCK:
        job["log"].append(line)
        del job["log"][:-300]
    try:
        print("[%s] %s" % (job["id"], msg))
    except Exception:  # consola de Windows sin UTF-8
        print(("[%s] %s" % (job["id"], msg)).encode("ascii", "replace").decode())


def setj(job, **kw):
    with LOCK:
        job.update(kw)


def cfg_snapshot():
    with LOCK:
        return dict(SETTINGS)


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.1f %s" % (n, u) if u != "B" else "%d B" % n
        n /= 1024


class Meter:
    """Velocidad media de los ultimos segundos."""

    def __init__(self):
        self.lock = threading.Lock()
        self.samples = []
        self.total = 0

    def __call__(self, n):
        with self.lock:
            self.total += n
            now = time.time()
            self.samples.append((now, self.total))
            while self.samples and now - self.samples[0][0] > 6:
                self.samples.pop(0)

    def rate(self):
        """Media de los ultimos segundos. Si hay pocas muestras muy juntas (arranque,
        reanudar), se divide al menos entre 2 s para no inventar velocidades absurdas."""
        with self.lock:
            now = time.time()
            while self.samples and now - self.samples[0][0] > 6:
                self.samples.pop(0)
            if len(self.samples) < 2:
                return 0.0
            (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
            if now - t1 > 3:      # lleva rato sin llegar nada
                return 0.0
            return (b1 - b0) / max(2.0, t1 - t0)


# ---------------------------------------------------------------------------
# Fase 1: comprobar enlaces y agrupar por conjunto de archivos
# ---------------------------------------------------------------------------

def _read_small(s, cancel, limit=4 * 1024 * 1024):
    if s.size > limit:
        raise E.PipeError("demasiado grande para ser un archivo de sumas")
    buf = bytearray()
    for data in s.stream(0, cfg_snapshot(), cancel, lambda n: None):
        buf += data
        if len(buf) > limit:
            break
    return bytes(buf).decode("utf-8", "replace")


def probe_job(job, cancel):
    setj(job, status="comprobando", phase="Comprobando enlaces")
    srcs = []
    sums = dict(job.get("checks") or {})
    for line in job["lines"]:
        if cancel.is_set():
            raise E.Cancelled()
        s = E.make_source(line)
        jlog(job, "Comprobando: %s" % line[:120])
        s.probe()
        if s.kind != "folder" and F.is_checksum_name(s.name):
            # .sfv / .md5 / .sha1: no se envian, sirven para comprobar las partes
            try:
                got = F.parse_checksums(_read_small(s, cancel), s.name)
                sums.update(got)
                jlog(job, "  %s: sumas de comprobacion de %d archivo(s)" % (s.name, len(got)))
            except Exception as e:
                jlog(job, "  No pude leer %s: %s" % (s.name, e))
            continue
        jlog(job, "  %s  (%s)%s" % (s.name, human(s.size) if s.size >= 0 else "tamano desconocido",
                                    "" if s.ranges else "  sin descarga en paralelo"))
        srcs.append(s)
    # partes que estan en el PC: si al lado hay un .sfv/.md5, tambien vale
    seen_dirs = set()
    for s in srcs:
        if s.kind != "file":
            continue
        d = os.path.dirname(os.path.abspath(s.path))
        if d in seen_dirs:
            continue
        seen_dirs.add(d)
        try:
            for n in os.listdir(d):
                fp = os.path.join(d, n)
                if F.is_checksum_name(n) and os.path.getsize(fp) < 4 * 1024 * 1024:
                    with open(fp, encoding="utf-8", errors="replace") as fh:
                        got = F.parse_checksums(fh.read(), n)
                    mine = {k: v for k, v in got.items() if k in {x.name.lower() for x in srcs}}
                    if mine:
                        sums.update(mine)
                        jlog(job, "  %s: sumas de comprobacion de %d parte(s)" % (n, len(mine)))
        except OSError:
            pass
    if not srcs:
        raise E.PipeError("Solo hay archivos de sumas (.sfv/.md5): pega tambien las partes")
    names = {s.name.lower() for s in srcs}
    with LOCK:
        job["checks"] = {k: v for k, v in sums.items() if k in names}
    return srcs


def part_hasher(job, s):
    """StreamHasher para una parte si hay suma con la que compararla (y esta activado)."""
    with LOCK:
        on = SETTINGS.get("verify_sums", True)
    chk = (job.get("checks") or {}).get(s.name.lower())
    if not on or not chk:
        return None
    return F.StreamHasher(chk[0])


def verify_part(job, s, hasher, on_bad=None, local=None, remote=None):
    """Compara la suma calculada al enviar con la del .sfv/.md5. Lanza PipeError si no coincide.
    Si no se pudo sumar al vuelo (se continuo a medias), la calcula leyendo el archivo entero: del PC (local) o de
    la consola por FTP (remote)."""
    if hasher is None:
        return
    algo, want = job["checks"][s.name.lower()]
    if not hasher.valid or hasher.pos != s.size:
        h2 = F.StreamHasher(algo)
        try:
            if local and os.path.isfile(local):
                jlog(job, "%s: se continuó a medias, compruebo el %s leyendo el archivo entero" % (s.name, algo.upper()))
                with open(local, "rb") as fh:
                    for _ in h2.wrap(iter(lambda: fh.read(4 * E.MiB), b""), 0):
                        pass
            elif remote:
                jlog(job, "%s: se continuó a medias, compruebo el %s leyéndolo de la consola" % (s.name, algo.upper()))
                setj(job, phase="Comprobando %s (%s) en la consola" % (s.name, algo.upper()))
                def feed(b):
                    h2._update(b)
                    h2.pos += len(b)
                E.Ftp(cfg_snapshot()).call(lambda f: f.retrbinary("RETR " + remote, feed, 4 * E.MiB))
            else:
                raise E.PipeError("sin archivo")
        except E.PipeError:
            jlog(job, "%s: sin comprobar con %s (se continuó de un envío anterior)" % (s.name, algo.upper()))
            return
        except Exception as e:
            jlog(job, "%s: no pude releerlo para comprobar el %s (%s)" % (s.name, algo.upper(), e))
            return
        hasher = h2
    got = hasher.hexdigest()
    if got.lower() == want.lower():
        jlog(job, "%s: %s correcto" % (s.name, algo.upper()))
        return
    if on_bad:
        try:
            on_bad()
        except Exception:
            pass
    raise E.PipeError("La parte %s ha llegado dañada: su %s es %s y debería ser %s. Puede ser una descarga "
                      "corrupta o un .sfv/.md5 de otra versión. Pulsa Reintentar para volver a bajarla."
                      % (s.name, algo.upper(), got, want))


def split_into_sets(srcs):
    groups = {}
    order = []
    for s in srcs:
        k = E.set_key(s.name)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(s)
    return [sorted(groups[k], key=lambda s: E.volume_order(s.name)) for k in order]


def describe(srcs):
    return [{"input": s.original, "name": s.name, "size": s.size, "kind": s.kind} for s in srcs]


# ---------------------------------------------------------------------------
# Fase 2: enviar a la PS5
# ---------------------------------------------------------------------------

GiB = 1024 ** 3
RUNAWAY_SLACK = int(os.environ.get("PS5D_RUNAWAY_SLACK", 2 * 1024 ** 3))


def free_space(bf, path="/data"):
    """Bytes libres en /data (o en el disco de path) segun BFpilot, o None si no responde."""
    try:
        st = bf._req("GET", "/api/fs/stat", {"path": path})
        v = st.get("availableBytes")
        return int(v) if v is not None else None
    except Exception:
        return None


def work_root(dest):
    """Carpeta de trabajo en el mismo disco que el destino: si el destino es un USB o un disco externo, se descomprime
    allí mismo (no ocupa el disco interno y colocarlo es un simple renombrado)."""
    m = re.match(r"^(/mnt/(?:usb|ext)\d+)(/|$)", dest or "")
    return (m.group(1) + "/ps5directo") if m else WORK_ROOT


def margin_bytes():
    with LOCK:
        return max(0, int(SETTINGS.get("space_margin_gb", 25) or 0)) * GiB


def start_space_watch(job, bf, cancel, what):
    """Vigila el espacio libre mientras se envia o descomprime. Si baja del margen,
    lo para todo antes de que la PS5 se quede sin sitio. Devuelve un Event para pararlo."""
    done = threading.Event()
    margin = margin_bytes()
    if margin <= 0:
        return done

    def loop():
        low_logged = False
        while not done.wait(15):
            free = free_space(bf)
            if free is None:
                continue
            if free < margin:
                with LOCK:
                    left = max(0, int(job.get("total") or 0) - int(job.get("sent") or 0))
                    job["need_bytes"] = int(max(0, left + margin - free)) or int(margin - free)
                    job["_stop"] = ("Parado para no llenar la PS5 (%s): quedan %s libres y el margen "
                                    "es %s. Libera espacio y pulsa Reintentar; sigue donde iba."
                                    % (what, human(free), human(margin)))
                jlog(job, "ESPACIO: quedan %s libres (< %s). Paro %s." % (human(free), human(margin), what))
                cancel.set()
                return
            if free < margin * 2 and not low_logged:
                jlog(job, "Aviso: queda poco espacio en la PS5 (%s libres)" % human(free))
                low_logged = True

    threading.Thread(target=loop, daemon=True).start()
    return done


STAMPS_FILE = os.path.join(DATA_DIR, "huellas.json")
STAMPS_LOCK = threading.Lock()


def stamps_check_console(job, bf, inc, srcs):
    """Antes de continuar un envio a medias: si el archivo del servidor ya no es el mismo (otro ETag, otra fecha o
    otro tamano), lo que hay a medias en la consola es de otro archivo y se borra para empezar de cero."""
    with STAMPS_LOCK:
        try:
            with open(STAMPS_FILE, encoding="utf-8") as fh:
                known = json.load(fh)
        except Exception:
            known = {}
        changed = False
        for s in srcs:
            if not hasattr(s, "stamp") or not s.name:
                continue
            key = "%s/%s" % (inc, s.name)
            new = s.stamp()
            if E.stamp_changed(known.get(key), new):
                part = key + ".ps5part"
                try:
                    if bf.exists(part):
                        bf.delete(part)
                        jlog(job, "%s: el archivo del servidor ha cambiado desde el último envío. Lo que había a "
                                  "medias en la PS5 no vale: empiezo desde cero." % s.name)
                except Exception as e:
                    jlog(job, "No pude borrar lo enviado a medias de %s: %s" % (s.name, e))
            if known.get(key) != new:
                known[key] = new
                changed = True
        if changed:
            try:
                _save_json(STAMPS_FILE, dict(list(known.items())[-400:]))
            except Exception:
                pass


def adopt_previous(job, bf, inc, names):
    """Mueve a 'inc' las partes que otros trabajos ya mandaron (completas o a medias)."""
    root = WORK_ROOT + "/incoming"
    try:
        dirs = [e["name"] for e in bf.listdir(root) if e.get("dir")]
    except Exception:
        return
    try:
        bf.mkdir(inc)
    except Exception:
        pass
    want = set(names) | {n + ".ps5part" for n in names}
    for d in dirs:
        path = "%s/%s" % (root, d)
        if path == inc:
            continue
        try:
            entries = bf.listdir(path)
        except Exception:
            continue
        for e in entries:
            if e["name"] in want and not bf.exists("%s/%s" % (inc, e["name"])):
                try:
                    bf.rename("%s/%s" % (path, e["name"]), "%s/%s" % (inc, e["name"]))
                    jlog(job, "Aprovecho lo ya enviado: %s" % e["name"])
                except Exception:
                    pass


def send_parts_parallel(job, cfg, srcs, inc, cancel, meter, set_sent):
    """Envia las partes de un comprimido, varias a la vez (cada una por su propia
    conexion FTP). En Wi-Fi, dos conexiones suman mas que una sola."""
    n = max(1, min(int(cfg.get("parallel_files", 2)), 4, len(srcs)))
    if n > 1:
        cfg = dict(cfg)
        cfg["buffer_mb"] = max(64, int(cfg.get("buffer_mb", 256)) // n)
    stop = threading.Event()          # se activa si cancelas o si una parte falla
    done = {}                         # parte -> bytes enviados
    errors = []
    lock = threading.Lock()
    pending = list(srcs)

    def relay_cancel():
        while not stop.is_set():
            if cancel.wait(0.5):
                stop.set()

    def progress(name, v):
        with lock:
            done[name] = v
            total = sum(done.values())
        set_sent(total)

    def phase():
        with lock:
            active = [s.name for s in srcs if 0 < done.get(s.name, 0) < max(1, s.size)]
        if active:
            setj(job, phase="Enviando " + " + ".join(active))

    def runner():
        while not stop.is_set():
            with lock:
                if not pending:
                    return
                s = pending.pop(0)
                done.setdefault(s.name, 0)
            try:
                hasher = part_hasher(job, s)
                remote = "%s/%s" % (inc, s.name)

                def make(off, s=s, hasher=hasher):
                    it = s.stream(off, cfg, stop, meter)
                    return hasher.wrap(it, off) if hasher else it
                E.upload(cfg, remote, s.size, make,
                         lambda v, s=s: (progress(s.name, v), phase()),
                         stop, lambda msg: jlog(job, msg))
                verify_part(job, s, hasher,
                            on_bad=lambda remote=remote: E.Ftp(cfg).call(lambda f: f.delete(remote)), remote=remote)
                progress(s.name, max(0, s.size))
            except Exception as e:
                with lock:
                    errors.append(e)
                stop.set()
                return

    threading.Thread(target=relay_cancel, daemon=True).start()
    if n > 1:
        jlog(job, "Enviando %d partes a la vez" % n)
    threads = [threading.Thread(target=runner, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    if cancel.is_set():
        raise E.Cancelled()
    real = [e for e in errors if not isinstance(e, E.Cancelled)]
    if real:
        raise real[0]
    if errors:
        raise errors[0]


def live_limit_bps():
    with LOCK:
        return float(SETTINGS.get("limit_mbps", 0) or 0) * 1024 * 1024


def transfer_job(job, srcs, cancel):
    cfg = cfg_snapshot()
    cfg["_log"] = lambda m: jlog(job, m)
    cfg["_limit_bps"] = live_limit_bps
    if job.get("raw"):
        return raw_job(job, srcs, cancel)
    if srcs[0].kind == "folder":
        if job.get("fmt") == "plain" and (os.path.isfile(os.path.join(srcs[0].path, "ampr_assets.index"))
                                         or os.path.isfile(os.path.join(srcs[0].path, "ampr_zstd.index"))):
            with LOCK:
                job["_plain_conv"] = True     # carpeta empaquetada en LZ4 -> carpeta normal
        return folder_job(job, srcs[0], cancel)
    names = [s.name for s in srcs]
    archives = [s for s in srcs if E.is_archive(s.name)]
    with LOCK:
        if job.get("fmt") == "plain" and len(srcs) == 1 and (
                srcs[0].name.lower().endswith(CONV_SRC_EXTS) or (archives and LZ4_NAME_RE.search(srcs[0].name))):
            job["_plain_conv"] = True     # imagen, .fpkg o LZ4 -> carpeta normal con Forge
    fmt = job_fmt(job)
    if fmt and len(srcs) == 1 and srcs[0].name.lower().endswith(CONV_SRC_EXTS):
        want = F.FORGE_FORMATS[fmt][1]
        if want and srcs[0].name.lower().endswith(want) and fmt not in ("lz4ffpkg", "zstdffpkg"):
            jlog(job, "Ya viene como %s: lo envío tal cual, sin convertir" % want)
        else:
            return pc_extract_job(job, srcs, cancel)
    if archives and (job_where(job) == "pc" or fmt or any(E.is_tar(a.name) for a in archives)):
        return pc_extract_job(job, srcs, cancel)
    m = E.SPLIT_RAW_RE.match(srcs[0].name)
    raw_split = (not archives and len(srcs) >= 1 and m
                 and all(E.SPLIT_RAW_RE.match(s.name) for s in srcs))
    known = all(s.size >= 0 for s in srcs)
    total = sum(s.size for s in srcs) if known else -1
    # Carpeta por conjunto de archivos (no por trabajo): si mandas luego las partes que faltaban
    # en otro trabajo, aprovecha lo que ya estaba en la consola.
    inc = "%s/incoming/%s" % (WORK_ROOT, E.safe_name(E.set_key(srcs[0].name)) or job["id"])
    meter = Meter()      # descarga (lo que entra al PC)
    up = Meter()         # envio a la PS5 (lo que importa)
    stop_speed = threading.Event()
    last_sent = {"v": None}
    sent_lock = threading.Lock()

    def set_sent(v):
        # Varias partes avisan a la vez y los avisos pueden llegar desordenados:
        # solo cuenta lo que supera el maximo ya visto (nunca dos veces lo mismo).
        with sent_lock:
            prev = last_sent["v"]
            if prev is not None and v <= prev:
                return
            if prev is not None and v - prev < 64 * 1024 * 1024:
                up(v - prev)
            last_sent["v"] = v
        setj(job, sent=v)

    def speed_loop():
        while not stop_speed.wait(1):
            setj(job, speed=up.rate(), dl_speed=meter.rate())

    threading.Thread(target=speed_loop, daemon=True).start()
    setj(job, status="enviando", total=total, sent=0)
    bf = E.BFpilot(cfg)

    adopt_previous(job, bf, inc, [s.name for s in srcs])
    stamps_check_console(job, bf, inc, srcs)
    check_duplicates_first(job, bf, E.title_id(srcs[0].name))

    # Comprobacion de espacio: lo que falta por enviar + margen. La descompresion la
    # vuelve a comprobar BFpilot antes de empezar (sabe lo que ocupa de verdad).
    free = free_space(bf)
    if free is not None and total > 0:
        present = 0
        try:
            want = {s.name: s.size for s in srcs}
            for e in bf.listdir(inc):
                n = e.get("name", "")
                base = n[:-8] if n.endswith(".ps5part") else n
                if base in want:
                    present += min(int(e.get("size") or 0), max(0, want[base]))
        except Exception:
            pass
        remaining = max(0, total - present)
        margin = margin_bytes()
        if remaining + margin > free:
            stop_speed.set()
            raise no_space(job, remaining, free,
                           "No hay sitio en la PS5: faltan por enviar %s, hay %s libres y dejo %s "
                           "de margen (Ajustes). Libera espacio y pulsa Reintentar."
                           % (human(remaining), human(free), human(margin)))
        if archives:
            est = int(total * 5)
            if free - remaining - est < margin:
                jlog(job, "Ojo: el contenido descomprimido puede ocupar hasta ~5 veces el RAR (~%s) y no se "
                          "si cabra. Si al descomprimir baja del margen, lo paro y los RAR se quedan "
                          "en la PS5." % human(est))
        jlog(job, "Espacio libre en la PS5: %s" % human(free))
    watch = start_space_watch(job, bf, cancel, "el envio")
    done_before = 0
    try:
        if archives or len(srcs) > 1 and not raw_split:
            # Volumenes de un archivo comprimido: cada uno a su sitio, separados
            send_parts_parallel(job, cfg, srcs, inc, cancel, meter, set_sent)
            return {"mode": "archive", "dir": inc, "first": E.first_volume(names)}
        if raw_split:
            # .001 .002 ... -> se unen en un solo archivo mientras se envian
            base_name = E.SPLIT_RAW_RE.match(srcs[0].name).group("base")
            if not known:
                raise E.PipeError("Para unir partes hace falta saber el tamano de cada una")
            remote = "%s/%s" % (inc, base_name)
            setj(job, phase="Enviando y uniendo %d partes en %s" % (len(srcs), base_name))
            jlog(job, "Uniendo %d partes en %s" % (len(srcs), base_name))
            E.upload(cfg, remote, total,
                     lambda off: E.concat_stream(srcs, off, cfg, cancel, meter),
                     lambda n: set_sent(n), cancel, lambda msg: jlog(job, msg))
            if E.is_archive(base_name):
                return {"mode": "archive", "dir": inc, "first": base_name}
            return {"mode": "file", "path": remote, "name": base_name}
        s = srcs[0]
        remote = "%s/%s" % (inc, s.name)
        setj(job, phase="Enviando %s" % s.name)
        hasher = part_hasher(job, s)

        def make(off):
            it = s.stream(off, cfg, cancel, meter)
            return hasher.wrap(it, off) if hasher else it
        E.upload(cfg, remote, s.size, make,
                 lambda n: set_sent(n), cancel, lambda msg: jlog(job, msg))
        verify_part(job, s, hasher, on_bad=lambda: E.Ftp(cfg).call(lambda f: f.delete(remote)), remote=remote)
        return {"mode": "file", "path": remote, "name": s.name}
    finally:
        stop_speed.set()
        watch.set()
        setj(job, speed=0.0, dl_speed=0.0)


# ---------------------------------------------------------------------------
# Envio "tal cual" (desde el explorador de la consola): sin descomprimir ni colocar
# ---------------------------------------------------------------------------

def raw_job(job, srcs, cancel):
    cfg = cfg_snapshot()
    cfg["_limit_bps"] = live_limit_bps
    dest = (job.get("dest") or cfg["dest"]).rstrip("/") or "/"
    total = sum(max(0, s.size) for s in srcs)
    up, meter = Meter(), Meter()
    state = {"base": 0, "last": 0}
    stop_speed = threading.Event()

    def speed_loop():
        while not stop_speed.wait(1):
            setj(job, speed=up.rate(), dl_speed=meter.rate())
    threading.Thread(target=speed_loop, daemon=True).start()

    def prog(v):
        cur = state["base"] + v
        if cur > state["last"]:
            up(cur - state["last"])
            state["last"] = cur
        setj(job, sent=cur)
    setj(job, status="enviando", total=total if total > 0 else -1, sent=0)
    placed = []
    try:
        for s in srcs:
            target = dest + "/" + s.name
            if s.kind == "folder":
                setj(job, phase="Enviando carpeta: %s" % s.name)
                sent, skipped, resumed = E.upload_tree(cfg, s, target, cancel, lambda m: jlog(job, m),
                                                       lambda v, rel: prog(v))
                jlog(job, "Carpeta enviada: %d archivos nuevos, %d ya estaban iguales, %d continuados"
                     % (sent, skipped, resumed))
            else:
                setj(job, phase="Enviando %s" % s.name)
                E.upload(cfg, target, s.size, lambda off, s=s: s.stream(off, cfg, cancel, meter),
                         prog, cancel, lambda m: jlog(job, m))
            state["base"] += max(0, s.size)
            placed.append(target)
    finally:
        stop_speed.set()
        setj(job, speed=0.0, dl_speed=0.0)
    return {"mode": "done", "placed": placed, "game": False}


# ---------------------------------------------------------------------------
# Historial (pestana Actividad)
# ---------------------------------------------------------------------------

HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
HISTORY_LOCK = threading.Lock()


def record_history(job):
    try:
        with HISTORY_LOCK:
            try:
                with open(HISTORY_FILE, encoding="utf-8") as f:
                    hist = json.load(f)
            except Exception:
                hist = []
            hist.append({
                "t": time.time(), "id": job["id"], "title": job.get("title") or "",
                "status": job.get("status"), "size": job.get("total", -1),
                "result": job.get("result") or [], "error": job.get("error") or "",
                "secs": int(time.time() - float(job.get("started") or job.get("created") or time.time())),
                "where": "raw" if job.get("raw") else "keep" if job.get("where") == "keep" else ("pc" if (job.get("info") or {}).get("mode") == "pcx" else "ps5"),
                "dest": job.get("dest") or "",
                # para "Descargar de nuevo" desde el historial
                "redo": {"lines": list(job.get("orig_lines") or job.get("lines") or []), "where": job.get("where") or "",
                         "raw": bool(job.get("raw")), "fmt": job.get("fmt") or "", "comp": job.get("comp") or "",
                         "fmt_set": bool(job.get("fmt_set")), "pcdir": job.get("pcdir") or "", "keep_steps": job.get("keep_steps") or ""},
            })
            _save_json(HISTORY_FILE, hist[-500:])
        if job.get("status") == "hecho":
            torrent_cleanup(job)
            rv = job.get("rec_ver")
            if rv:
                vers = _emu_vers()
                vers[rv["id"]] = rv["tag"]
                _save_json(EMU_VER_FILE, vers)
        title = job.get("title") or "Transferencia"
        link = {"tab": "Transfers", "job": job["id"]}
        if job.get("status") == "hecho":
            notify("ok", "Terminado: %s" % title, ", ".join(job.get("result") or [])[:300], link)
            ps5_toast("PS5 Directo: listo %s" % title)
        elif job.get("status") == "error":
            notify("err", "Error: %s" % title, job.get("error") or "", link)
            ps5_toast("PS5 Directo: error en %s" % title)
    except Exception:
        traceback.print_exc()


NOTIF_FILE = os.path.join(DATA_DIR, "notifications.json")
NOTIF_LOCK = threading.Lock()
NOTIFS = []          # lo mas nuevo primero


def load_notifs():
    try:
        with open(NOTIF_FILE, encoding="utf-8") as f:
            NOTIFS[:] = json.load(f)[:100]
    except Exception:
        pass


def save_notifs():
    with NOTIF_LOCK:
        data = list(NOTIFS)
    try:
        _save_json(NOTIF_FILE, data)
    except Exception:
        pass


def notify(level, title, body="", link=None, key=None):
    """Aviso para la campana. level: ok, info, warn, err. key evita repetir el mismo aviso."""
    with NOTIF_LOCK:
        if key and any(n.get("key") == key for n in NOTIFS):
            return
        NOTIFS.insert(0, {"id": secrets.token_hex(4), "t": time.time(), "level": level, "title": title,
                          "body": (body or "")[:600], "link": link, "key": key, "read": False})
        del NOTIFS[100:]
    save_notifs()


def read_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return list(reversed(json.load(f)))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Explorador de archivos de la consola
# ---------------------------------------------------------------------------

FMOP = {"busy": False, "op": "", "current": "", "items_done": 0, "items_total": 0,
        "bytes_done": 0, "bytes_total": 0, "error": "", "message": "", "t0": 0.0, "local": ""}
FM_CANCEL = threading.Event()


def fm_clean(path):
    path = "/" + "/".join(x for x in str(path or "/").replace("\\", "/").split("/") if x and x != ".")
    if ".." in path.split("/"):
        raise E.PipeError("Ruta no valida")
    return path


def fm_list(path):
    cfg = cfg_snapshot()
    path = fm_clean(path)
    bf = E.BFpilot(cfg)
    out = {"path": path, "entries": [], "free": None, "total": None, "via": "bfpilot"}
    if bf.alive():
        ents = bf.listdir(path)
        out["entries"] = [{"name": e.get("name"), "dir": bool(e.get("dir")), "size": e.get("size"),
                           "mtime": e.get("mtime"), "mode": e.get("mode")} for e in ents]
        try:
            st = bf._req("GET", "/api/fs/stat", {"path": path})
            out["free"] = st.get("availableBytes")
            out["total"] = st.get("usableTotalBytes") or st.get("totalBytes")
        except Exception:
            pass
    else:
        out["via"] = "ftp"
        f = E.Ftp(cfg).connect()
        try:
            for name, x in E.ftp_listdir(f, path):
                out["entries"].append({"name": name, "dir": x["type"] == "dir", "size": x["size"],
                                       "mtime": x["mtime"], "mode": x["mode"]})
        finally:
            try:
                f.quit()
            except Exception:
                pass
    out["entries"].sort(key=lambda e: (not e["dir"], (e["name"] or "").lower()))
    return out


def fm_bf():
    bf = E.BFpilot(cfg_snapshot())
    if not bf.alive():
        raise E.PipeError("BFpilot no responde: cargalo para copiar, mover, borrar o calcular tamanos")
    return bf


def fm_bf_opt():
    """BFpilot si esta cargado, o None (entonces se hace por FTP)."""
    bf = E.BFpilot(cfg_snapshot())
    return bf if bf.alive() else None


# ---- lo mismo por FTP, para cuando BFpilot no esta cargado -------------------

def _ftp_entries(f, path):
    """[(nombre, es_carpeta, tamano)] de una carpeta remota."""
    return [(n, x["type"] == "dir", x["size"]) for n, x in E.ftp_listdir(f, path)]


def _ftp_kind(f, path):
    """'dir', 'file' o None."""
    parent, name = posixpath.dirname(path) or "/", posixpath.basename(path)
    try:
        for n, d, _ in _ftp_entries(f, parent):
            if n == name:
                return "dir" if d else "file"
    except Exception:
        pass
    return None


def _ftp_walk(f, root, cancel=None):
    """(archivos [(ruta, rel, tamano)], carpetas [rel]) de una carpeta, de arriba abajo."""
    files, dirs = [], []
    stack = [""]
    while stack:
        if cancel is not None and cancel.is_set():
            raise E.Cancelled()
        rel = stack.pop()
        cur = root + ("/" + rel if rel else "")
        for n, d, sz in _ftp_entries(f, cur):
            r = (rel + "/" + n).lstrip("/")
            if d:
                dirs.append(r)
                stack.append(r)
            else:
                files.append((cur + "/" + n, r, sz))
    return files, dirs


def ftp_du(cfg, path):
    def go(f):
        k = _ftp_kind(f, path)
        if k == "file":
            return int(f.size(path) or 0)
        files, _ = _ftp_walk(f, path)
        return sum(sz for _, _, sz in files)
    return E.Ftp(cfg).call(go)


def ftp_rmtree(cfg, path, cancel, on_item=None):
    def go(f):
        k = _ftp_kind(f, path)
        if k is None:
            return
        if k == "file":
            f.delete(path)
            return
        files, dirs = _ftp_walk(f, path, cancel)
        for i, (rp, _, _) in enumerate(files):
            if cancel.is_set():
                raise E.Cancelled()
            if on_item:
                on_item(rp, i, len(files))
            f.delete(rp)
        for d in sorted(dirs, key=lambda x: -x.count("/")):
            f.rmd(path + "/" + d)
        f.rmd(path)
    E.Ftp(cfg).call(go)


def ftp_chmod_tree(cfg, path, cancel):
    def go(f):
        n = 1
        f.sendcmd("SITE CHMOD 777 " + path)
        if _ftp_kind(f, path) == "dir":
            files, dirs = _ftp_walk(f, path, cancel)
            for d in dirs:
                f.sendcmd("SITE CHMOD 777 %s/%s" % (path, d))
            for rp, _, _ in files:
                if cancel.is_set():
                    raise E.Cancelled()
                f.sendcmd("SITE CHMOD 777 " + rp)
            n += len(files) + len(dirs)
        return n
    return E.Ftp(cfg).call(go)


def ftp_relay_copy(cfg, src, target, cancel, progress):
    """Copia dentro de la consola pasando por el PC (sin BFpilot): lee por una conexion FTP
    y escribe por otra. progress(bytes_hechos, bytes_totales, ruta_actual)."""
    a = E.Ftp(cfg).connect()
    b = E.Ftp(cfg).connect()
    try:
        k = _ftp_kind(a, src)
        if k is None:
            raise E.PipeError("No existe %s" % src)
        if k == "file":
            files, dirs = [(src, "", int(a.size(src) or 0))], []
        else:
            files, dirs = _ftp_walk(a, src, cancel)
            E.Ftp.mkdirs(b, target)
            for d in dirs:
                E.Ftp.mkdirs(b, target + "/" + d)
        total = sum(sz for _, _, sz in files)
        done = 0
        for rp, rel, sz in files:
            if cancel.is_set():
                raise E.Cancelled()
            dst = target + ("/" + rel if rel else "")
            E.Ftp.mkdirs(b, posixpath.dirname(dst))
            cr = a.transfercmd("RETR " + rp)
            cw = b.transfercmd("STOR " + dst)
            try:
                while True:
                    if cancel.is_set():
                        raise E.Cancelled()
                    chunk = cr.recv(E.MiB)
                    if not chunk:
                        break
                    cw.sendall(chunk)
                    done += len(chunk)
                    progress(done, total, rp)
            finally:
                cr.close()
                cw.close()
            a.voidresp()
            b.voidresp()
            got = E.Ftp.size(b, dst)
            if got is not None and got != sz:
                raise E.PipeError("La copia de %s salio incompleta (%s de %s)" % (rp, got, sz))
    finally:
        for f in (a, b):
            try:
                f.quit()
            except Exception:
                pass


# ---- operaciones largas con progreso -----------------------------------------

OP_DONE = {"copy": "Copiado", "move": "Movido", "delete": "Borrado", "download": "Descargado al PC",
           "chmod": "Permisos puestos", "extract": "Extraído"}
OP_FAIL = {"copy": "Error al copiar", "move": "Error al mover", "delete": "Error al borrar",
           "download": "Error al descargar", "chmod": "Error al poner permisos", "extract": "Error al extraer"}


def _fm_start(op, items_total, fn, where=None, names=None):
    with LOCK:
        if FMOP["busy"]:
            raise E.PipeError("Ya hay una operacion en marcha en el explorador")
        FMOP.update(busy=True, op=op, current="", items_done=0, items_total=items_total, bytes_done=0,
                    bytes_total=0, error="", message="", t0=time.time(), local="")
    FM_CANCEL.clear()
    what = ", ".join((names or [])[:3]) + ("…" if names and len(names) > 3 else "")
    link = {"tab": "Files", "path": where} if where else None

    def run():
        try:
            fn()
            with LOCK:
                if not FMOP["message"]:
                    FMOP["message"] = "Hecho"
                msg = FMOP["message"]
            if time.time() - FMOP["t0"] > 8 or op in ("extract",):
                notify("ok", "%s: %s" % (OP_DONE.get(op, op), what) if what else OP_DONE.get(op, op),
                       msg if msg != "Hecho" else "", link)
        except E.Cancelled:
            with LOCK:
                FMOP["error"] = "Cancelado"
        except Exception as e:
            traceback.print_exc()
            with LOCK:
                FMOP["error"] = conn_error_text(e)
            notify("err", "%s: %s" % (OP_FAIL.get(op, op), what) if what else OP_FAIL.get(op, op),
                   str(e), link)
        finally:
            with LOCK:
                FMOP["busy"] = False
    threading.Thread(target=run, daemon=True).start()


def _names_in(path):
    try:
        return {e["name"]: e["dir"] for e in fm_list(path)["entries"]}
    except Exception:
        return {}


def _free_name(name, taken):
    """'juego' -> 'juego (2)'; 'a.zip' -> 'a (2).zip'."""
    base, ext = name, ""
    m = re.match(r"^(.*?)(\.[A-Za-z0-9]{1,5})$", name)
    if m and m.group(1):
        base, ext = m.group(1), m.group(2)
    k = 2
    while "%s (%d)%s" % (base, k, ext) in taken:
        k += 1
    return "%s (%d)%s" % (base, k, ext)


def fm_conflicts(srcs, dst):
    dst = fm_clean(dst)
    have = _names_in(dst)
    out = []
    for s in srcs:
        s = fm_clean(s)
        n = posixpath.basename(s)
        if n in have:
            out.append({"name": n, "dir": have[n], "same": posixpath.dirname(s) == dst})
    return out


def fm_paste(op, srcs, dst, mode="replace"):
    """mode: replace (reemplaza archivos / combina carpetas), skip, keepboth."""
    dst = fm_clean(dst)
    srcs = [fm_clean(x) for x in srcs]
    if mode not in ("replace", "skip", "keepboth"):
        mode = "replace"
    for s in srcs:
        if dst == s or dst.startswith(s + "/"):
            raise E.PipeError("No se puede %s una carpeta dentro de si misma" % ("mover" if op == "move" else "copiar"))
    cfg = cfg_snapshot()
    bf = fm_bf_opt()

    def bf_job(kind, s, d, overwrite):
        bf._req("POST", "/api/fs/" + kind, form={"src": s, "dst": d, "overwrite": "1" if overwrite else "0"})
        while True:
            time.sleep(0.7)
            if FM_CANCEL.is_set():
                try:
                    bf._req("GET", "/api/fs/job/cancel")
                except Exception:
                    pass
            j = bf._req("GET", "/api/fs/job/status")
            with LOCK:
                FMOP.update(bytes_done=int(j.get("copiedBytes") or 0), bytes_total=int(j.get("totalBytes") or 0),
                            current=j.get("current") or s)
            if not j.get("busy"):
                if j.get("error"):
                    raise E.PipeError(j["error"])
                break
        if FM_CANCEL.is_set():
            raise E.Cancelled()

    def relay(s, target):
        def prog(done, total, cur):
            with LOCK:
                FMOP.update(bytes_done=done, bytes_total=total, current=cur)
        ftp_relay_copy(cfg, s, target, FM_CANCEL, prog)

    def work():
        taken = set(_names_in(dst))
        for i, s in enumerate(srcs):
            if FM_CANCEL.is_set():
                raise E.Cancelled()
            name = posixpath.basename(s)
            same_dir = posixpath.dirname(s) == dst
            with LOCK:
                FMOP.update(current=s, items_done=i, bytes_done=0, bytes_total=0)
            if op == "move" and same_dir:
                continue
            new = name
            if name in taken:
                if mode == "skip":
                    continue
                if mode == "keepboth" or same_dir:
                    new = _free_name(name, taken)
            taken.add(new)
            target = dst.rstrip("/") + "/" + new
            if bf is not None:
                if new == name:
                    bf_job(op, s, dst, mode == "replace")
                else:
                    # BFpilot copia con el mismo nombre: a una carpeta temporal y luego se renombra
                    tmp = "%s/.ps5d-%s" % (dst.rstrip("/"), secrets.token_hex(3))
                    bf.mkdir(tmp)
                    try:
                        bf_job(op, s, tmp, False)
                        bf.rename(tmp + "/" + name, target)
                    finally:
                        try:
                            bf.delete(tmp)
                        except Exception:
                            pass
            else:
                if op == "move":
                    try:
                        E.Ftp(cfg).call(lambda f: f.rename(s, target))
                        continue
                    except Exception:
                        pass            # otro disco o ya existe: copia y luego borra
                relay(s, target)
                if op == "move":
                    ftp_rmtree(cfg, s, FM_CANCEL)
        with LOCK:
            FMOP["items_done"] = len(srcs)
    _fm_start(op, len(srcs), work, where=dst, names=[posixpath.basename(x) for x in srcs])


# ---------------------------------------------------------------------------
# Papelera en la consola: lo borrado (y la copia vieja al reemplazar) se aparta en una carpeta del mismo disco,
# se puede recuperar unos dias y luego se borra solo. Apartar es instantaneo, pero no libera espacio.
# ---------------------------------------------------------------------------

TRASH_FILE = os.path.join(DATA_DIR, "papelera.json")
TRASH_LOCK = threading.Lock()


def trash_root(path):
    m = re.match(r"^(/mnt/(?:ext|usb)\d+)(/|$)", path)
    if m:
        return m.group(1) + "/ps5directo-papelera"
    if path.startswith("/data/"):
        return "/data/ps5directo/papelera"
    return None


def trash_load():
    try:
        with open(TRASH_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return []


def trash_save(items):
    _save_json(TRASH_FILE, items)


def _rename_console(src, dst):
    bf = E.BFpilot(cfg_snapshot())
    if bf.alive():
        bf.mkdir(posixpath.dirname(dst))
        bf.rename(src, dst)
    else:
        def go(f):
            E.Ftp.mkdirs(f, posixpath.dirname(dst))
            f.rename(src, dst)
        E.Ftp(cfg_snapshot()).call(go)


def trash_move(path, why="borrado", size=None):
    """Aparta path en la papelera. True si se pudo (si no, quien llama lo borra del todo)."""
    root = trash_root(path)
    if not root or not SETTINGS.get("trash_on", True) or path.startswith(root):
        return False
    tid = "%d%s" % (int(time.time() * 1000), secrets.token_hex(2))
    dst = "%s/%s__%s" % (root, tid, posixpath.basename(path.rstrip("/")))
    try:
        _rename_console(path, dst)
    except Exception as e:
        print("Papelera: no pude apartar %s: %s" % (path, e))
        return False
    with TRASH_LOCK:
        items = trash_load()
        items.append({"id": tid, "orig": path, "at": dst, "t": time.time(), "why": why, "size": size})
        trash_save(items)
    return True


def _delete_console(path, cancel=None):
    bf = E.BFpilot(cfg_snapshot())
    if bf.alive():
        bf.delete(path, cancel)
    else:
        ftp_rmtree(cfg_snapshot(), path, cancel or threading.Event())


def trash_delete(ids=None, older_than=None):
    """Borra del todo lo de la papelera (todo, unos ids o lo mas viejo que older_than). Devuelve cuantos."""
    with TRASH_LOCK:
        items = trash_load()
    gone = []
    for it in items:
        if ids is not None and it["id"] not in ids:
            continue
        if older_than is not None and it["t"] > older_than:
            continue
        try:
            _delete_console(it["at"])
            gone.append(it["id"])
        except Exception as e:
            print("Papelera: no pude borrar %s: %s" % (it["at"], e))
    with TRASH_LOCK:
        trash_save([x for x in trash_load() if x["id"] not in gone])
    return len(gone)


def trash_restore(tid):
    with TRASH_LOCK:
        it = next((x for x in trash_load() if x["id"] == tid), None)
    if not it:
        raise E.PipeError("Ya no está en la papelera")
    cfg = cfg_snapshot()
    exists = E.BFpilot(cfg).exists(it["orig"]) if E.BFpilot(cfg).alive() else \
        E.Ftp(cfg).call(lambda f: _ftp_isdir(f, it["orig"]) or E.Ftp.size(f, it["orig"]) is not None)
    if exists:
        raise E.PipeError("En %s ya hay otra cosa: muévela o bórrala antes de recuperar esta." % it["orig"])
    _rename_console(it["at"], it["orig"])
    with TRASH_LOCK:
        trash_save([x for x in trash_load() if x["id"] != tid])
    return it["orig"]


def trash_list():
    items = trash_load()
    # quitar de la lista lo que ya no esta (borrado a mano, otra consola...)
    try:
        roots = sorted({posixpath.dirname(x["at"]) for x in items})

        def names(f):
            out = set()
            for r in roots:
                try:
                    out |= {r + "/" + n for n, v in E.ftp_listdir(f, r)}
                except ftplib.all_errors:
                    pass
            return out
        if roots:
            have = E.Ftp(cfg_snapshot()).call(names)
            if any(x["at"] not in have for x in items):
                with TRASH_LOCK:
                    items = [x for x in trash_load() if x["at"] in have]
                    trash_save(items)
    except Exception:
        pass
    days = int(SETTINGS.get("trash_days") or 7)
    for it in items:
        it["left_days"] = max(0, round(days - (time.time() - it["t"]) / 86400, 1))
    return sorted(items, key=lambda x: -x["t"])


def trash_tidy():
    """Borra lo que lleve en la papelera mas de los dias elegidos (si la consola contesta)."""
    days = int(SETTINGS.get("trash_days") or 7)
    if trash_load():
        trash_delete(older_than=time.time() - days * 86400)


def trash_make_room(need, bf=None):
    """Si no hay sitio para `need` bytes, vacia la papelera empezando por lo mas viejo."""
    items = sorted(trash_load(), key=lambda x: x["t"])
    if not items:
        return
    try:
        free = free_space(bf or E.BFpilot(cfg_snapshot()))
    except Exception:
        return
    for it in items:
        if free is None or free >= need:
            return
        trash_delete(ids=[it["id"]])
        try:
            free = free_space(bf or E.BFpilot(cfg_snapshot()))
        except Exception:
            return


def fm_delete(paths, trash=False):
    paths = [fm_clean(x) for x in paths]
    if trash and SETTINGS.get("trash_on", True):
        rest = [p for p in paths if not trash_move(p)]
        if not rest:
            with LOCK:
                FMOP.update(busy=False, op="delete", items_done=len(paths), items_total=len(paths), error="",
                            message="En la papelera: se puede recuperar unos días (Espacio › Papelera)")
            return
        paths = rest
    for p in paths:
        if p.count("/") < 2:
            raise E.PipeError("Por seguridad no borro carpetas de primer nivel (%s)" % p)
    bf = fm_bf_opt()
    cfg = cfg_snapshot()

    def free_now():
        if bf is None:
            return None
        try:
            return int(bf._req("GET", "/api/fs/stat", {"path": paths[0].rsplit("/", 1)[0] or "/"},
                               timeout=8).get("availableBytes") or 0)
        except Exception:
            return None

    def work():
        start = free_now()
        last = {"t": 0.0}

        def tick():
            # Borrar un juego grande tarda: ensenamos lo que se va liberando para que se vea que avanza
            if start is None or time.time() - last["t"] < 3:
                return
            last["t"] = time.time()
            now = free_now()
            if now is not None:
                with LOCK:
                    FMOP["bytes_done"] = max(0, now - start)
                    FMOP["message"] = ""
        for i, p in enumerate(paths):
            if FM_CANCEL.is_set():
                raise E.Cancelled()
            with LOCK:
                FMOP.update(current=p, items_done=i)
            if bf is not None:
                bf.delete(p, FM_CANCEL, on_tick=tick)
            else:
                ftp_rmtree(cfg, p, FM_CANCEL,
                           lambda rp, k, n: FMOP.update(current=rp))
        tick()
        with LOCK:
            FMOP["items_done"] = len(paths)
            if FMOP["bytes_done"]:
                FMOP["message"] = "Hecho: liberados %s" % human(FMOP["bytes_done"])
    _fm_start("delete", len(paths), work, where=posixpath.dirname(paths[0]) if paths else None,
              names=[posixpath.basename(x) for x in paths])


def fm_du(path):
    path = fm_clean(path)
    bf = fm_bf_opt()
    if bf is not None:
        return bf.du(path, timeout=300)
    return ftp_du(cfg_snapshot(), path)


def fm_extract(src, folder, password="", exists="keepboth"):
    """Descomprime un .rar/.7z/.zip que ya esta en la consola, en una carpeta a su lado."""
    src = fm_clean(src)
    bf = E.BFpilot(cfg_snapshot())
    if not bf.alive():
        raise E.PipeError("Descomprimir en la consola necesita BFpilot cargado en la PS5")
    parent, name = posixpath.dirname(src) or "/", posixpath.basename(src)
    if not E.is_archive(name):
        raise E.PipeError("Eso no es un comprimido (.rar, .7z o .zip)")
    have = _names_in(parent)
    # de un .part3.rar o un .7z.002 se extrae desde el primero
    group = [n for n in have if not have[n] and E.set_key(n) == E.set_key(name) and E.is_archive(n)]
    first = E.first_volume(group or [name]) or name
    src = parent.rstrip("/") + "/" + first
    folder = (folder or "").strip().strip("/")
    if not folder or "/" in folder or folder in (".", ".."):
        raise E.PipeError("Pon un solo nombre de carpeta, sin /")
    if folder in have:          # BFpilot no extrae sobre una carpeta que ya existe
        folder = _free_name(folder, set(have))
    dst = parent.rstrip("/") + "/" + folder

    def work():
        with LOCK:
            FMOP.update(current=first)

        def st(j):
            with LOCK:
                FMOP.update(bytes_done=int(j.get("bytesWritten") or 0), bytes_total=int(j.get("totalBytes") or 0),
                            current=j.get("current") or first)
        try:
            j = bf.extract(src, dst, password, FM_CANCEL, st)
        except E.PipeError as e:
            if E.is_password_error(str(e)):
                raise E.PipeError("Contraseña incorrecta o necesaria: vuelve a intentarlo con la contraseña")
            raise
        try:
            bf.fix_perms(dst, FM_CANCEL)
        except Exception:
            pass
        with LOCK:
            FMOP.update(items_done=1, message="Extraído en %s" % dst)
    _fm_start("extract", 1, work, where=parent, names=[first])


def pc_library_dir():
    """Carpeta del PC donde llega lo que bajas de la consola desde la Biblioteca. Siempre sale en
    Biblioteca > En tu PC."""
    d = (SETTINGS.get("pc_library_dir") or "").strip().strip('"')
    return d or os.path.join(downloads_dir(), "PS5 Directo", "Biblioteca")


def lib_scan_safe():
    try:
        library_start(False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Lo archivado en el PC: lista con fecha, ruta y suma SHA-256 de cada archivo, para comprobar mas adelante que
# sigue entero (un disco que falla, una copia a medias...).
# ---------------------------------------------------------------------------

ARCH_FILE = os.path.join(DATA_DIR, "archivados.json")
ARCH_LOCK = threading.Lock()
ARCH_STATE = {}          # id -> {"busy", "pct", "msg"}


def _arch_load():
    try:
        with open(ARCH_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return []


def _arch_save(lst):
    _save_json(ARCH_FILE, lst)


def _sha256_file(path, cancel=None, on_bytes=None):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if cancel is not None and cancel.is_set():
                raise E.Cancelled()
            b = fh.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
            if on_bytes:
                on_bytes(len(b))
    return h.hexdigest()


def arch_record(console_path, root_local, files):
    """Apunta lo archivado y calcula sus sumas en segundo plano (en un disco lento puede tardar)."""
    name = posixpath.basename(console_path)
    rec = {"id": secrets.token_hex(5), "t": time.time(), "name": name, "tid": E.title_id(name) or "",
           "console": console_path, "pc": os.path.join(root_local, name),
           "size": sum(sz or 0 for _, _, sz in files),
           "files": [{"rel": rel, "size": sz} for _, rel, sz in files], "state": "sumando", "checked": None}
    titles = (lib_load().get("titles") or {})
    if rec["tid"] and (titles.get(rec["tid"]) or {}).get("title"):
        rec["title"] = titles[rec["tid"]]["title"]
    with ARCH_LOCK:
        lst = _arch_load()
        lst.append(rec)
        _arch_save(lst)
    threading.Thread(target=_arch_hash, args=(rec["id"], root_local, False), daemon=True).start()


def _arch_hash(rid, root_local=None, verify=True):
    with ARCH_LOCK:
        rec = next((r for r in _arch_load() if r["id"] == rid), None)
    if not rec:
        return
    base = os.path.dirname(rec["pc"]) if root_local is None else root_local
    total = max(1, rec.get("size") or 1)
    st = ARCH_STATE.setdefault(rid, {})
    st.update(busy=True, pct=0, msg="Comprobando…" if verify else "Calculando las sumas…")
    done = {"n": 0}

    def tick(n):
        done["n"] += n
        st["pct"] = min(99, int(done["n"] * 100 / total))
    bad, missing = [], []
    try:
        for fe in rec["files"]:
            lp = os.path.join(base, *fe["rel"].split("/"))
            if not os.path.isfile(lp):
                missing.append(fe["rel"])
                continue
            if fe.get("size") is not None and os.path.getsize(lp) != fe["size"]:
                bad.append(fe["rel"])
                continue
            hx = _sha256_file(lp, on_bytes=tick)
            if verify and fe.get("sha256"):
                if hx != fe["sha256"]:
                    bad.append(fe["rel"])
            else:
                fe["sha256"] = hx
        state = "ok" if not bad and not missing else "mal"
        msg = ("Todo entero (%d archivos)" % len(rec["files"]) if state == "ok" else
               "%s%s" % ("Cambiados o dañados: %s. " % ", ".join(bad[:4]) if bad else "",
                         "Faltan: %s." % ", ".join(missing[:4]) if missing else ""))
    except Exception as e:
        state, msg = "error", str(e)
    with ARCH_LOCK:
        lst = _arch_load()
        for r in lst:
            if r["id"] == rid:
                if not verify:
                    r["files"] = rec["files"]
                r["state"] = state
                r["msg"] = msg
                r["checked"] = time.time()
        _arch_save(lst)
    st.update(busy=False, pct=100, msg=msg)
    if verify and state != "ok":
        notify("warn", "Archivado con problemas: %s" % (rec.get("title") or rec["name"]), msg, {"tab": "Library"})


def arch_list():
    with ARCH_LOCK:
        lst = _arch_load()
    out = []
    for r in sorted(lst, key=lambda x: x.get("t", 0), reverse=True):
        it = {k: r.get(k) for k in ("id", "t", "name", "title", "tid", "console", "pc", "size", "state", "msg", "checked")}
        it["n"] = len(r.get("files") or [])
        it["exists"] = os.path.exists(r.get("pc") or "")
        st = ARCH_STATE.get(r["id"]) or {}
        it["busy"] = bool(st.get("busy"))
        it["pct"] = st.get("pct", 0)
        out.append(it)
    return {"items": out}


def arch_verify_start(rid):
    if (ARCH_STATE.get(rid) or {}).get("busy"):
        return {"busy": True}
    threading.Thread(target=_arch_hash, args=(rid, None, True), daemon=True).start()
    return {"busy": True}


def arch_forget(rid):
    with ARCH_LOCK:
        lst = [r for r in _arch_load() if r["id"] != rid]
        _arch_save(lst)
    return {"ok": True}


def fm_download(paths, dest=None, scan=True, after=None):
    cfg = cfg_snapshot()
    paths = [fm_clean(x) for x in paths]
    root_local = dest or os.path.join(downloads_dir(), "PS5 Directo")

    def walk(rpath):
        """[(ruta remota, ruta relativa, tamano)] de un archivo o carpeta."""
        out = []
        base = posixpath.basename(rpath)
        listing = None
        try:
            listing = fm_list(rpath)["entries"]
        except Exception:
            listing = None
        if listing is None:
            return [(rpath, base, None)]
        stack = [(rpath, base, listing)]
        while stack:
            cur, rel, ents = stack.pop()
            for e in ents:
                rp, rr = cur + "/" + e["name"], rel + "/" + e["name"]
                if e["dir"]:
                    stack.append((rp, rr, fm_list(rp)["entries"]))
                else:
                    out.append((rp, rr, e.get("size")))
        return out

    def work():
        files = []
        for p in paths:
            parent = fm_list(posixpath.dirname(p) or "/")["entries"]
            ent = next((e for e in parent if e["name"] == posixpath.basename(p)), None)
            if ent and not ent["dir"]:
                files.append((p, posixpath.basename(p), ent.get("size")))
            else:
                files.extend(walk(p))
        total = sum(sz or 0 for _, _, sz in files)
        with LOCK:
            FMOP.update(items_total=len(files), bytes_total=total, local=root_local)
        done = 0
        for i, (rp, rel, sz) in enumerate(files):
            if FM_CANCEL.is_set():
                raise E.Cancelled()
            with LOCK:
                FMOP.update(current=rp, items_done=i)
            local = os.path.join(root_local, *rel.split("/"))
            if sz is not None and os.path.isfile(local) and os.path.getsize(local) == sz:
                done += sz
                continue
            base = done
            E.ftp_download(cfg, rp, local, FM_CANCEL, lambda n: FMOP.update(bytes_done=base + n), sz)
            done = base + (sz or 0)
        with LOCK:
            FMOP.update(items_done=len(files), bytes_done=done,
                        message="Guardado en %s" % root_local)
        if after:
            after(files, root_local)
        if dest and scan:   # bajado a la carpeta de la Biblioteca: que salga ya en «En tu PC»
            threading.Thread(target=collection_scan, daemon=True).start()
    _fm_start("download", len(paths), work, names=[posixpath.basename(x) for x in paths])


def fm_chmod(paths):
    bf = fm_bf_opt()
    cfg = cfg_snapshot()
    paths = [fm_clean(x) for x in paths]

    def work():
        changed = 0
        for i, p in enumerate(paths):
            with LOCK:
                FMOP.update(current=p, items_done=i)
            if bf is not None:
                c, _, _ = bf.fix_perms(p, FM_CANCEL)
            else:
                c = ftp_chmod_tree(cfg, p, FM_CANCEL)
            changed += c
        with LOCK:
            FMOP.update(items_done=len(paths), message="Permisos 777 puestos en %d elementos" % changed)
    _fm_start("chmod", len(paths), work)


PICK_SCRIPT = r'''
import json, sys, os
import tkinter as tk
from tkinter import filedialog
r = tk.Tk(); r.withdraw()
try:
    r.attributes("-topmost", True)
except Exception:
    pass
kind, title = sys.argv[1], sys.argv[2]
if kind == "folder":
    p = filedialog.askdirectory(title=title, mustexist=True)
    res = [p] if p else []
else:
    res = list(filedialog.askopenfilenames(title=title))
print(json.dumps([os.path.normpath(x) for x in res]))
'''


# ---------------------------------------------------------------------------
# Pestana Espacio: que ocupa sitio en la consola (grafico de quesitos + lista)
# ---------------------------------------------------------------------------

SPACE = {"busy": False, "root": "", "done": 0, "total": 0, "current": "", "result": None, "error": ""}
SPACE_CANCEL = threading.Event()
IMAGE_RE = re.compile(r"\.(exfat|ffpkg|ffpfsc?|ufs|img)$", re.I)
EXPAND = ("homebrew", "pkg")     # carpetas cuyo contenido se ensena elemento a elemento


def title_name(text):
    """Nombre del juego/app en un param.json (o None)."""
    try:
        j = json.loads(text)
    except Exception:
        m = re.search(r'"titleName"\s*:\s*"([^"]+)"', text or "")
        return m.group(1) if m else None
    lp = j.get("localizedParameters") or {}
    for lang in (lp.get("defaultLanguage"), "es-ES", "en-US", "en-GB"):
        if lang and isinstance(lp.get(lang), dict) and lp[lang].get("titleName"):
            return lp[lang]["titleName"]
    for v in lp.values():
        if isinstance(v, dict) and v.get("titleName"):
            return v["titleName"]
    return j.get("titleName")


def space_scan(root):
    cfg = cfg_snapshot()
    bf = E.BFpilot(cfg)
    if not bf.alive():
        raise E.PipeError("Para analizar el espacio hace falta BFpilot cargado en la PS5")
    root = fm_clean(root or "/data")
    st = bf._req("GET", "/api/fs/stat", {"path": root})
    total = int(st.get("usableTotalBytes") or st.get("totalBytes") or 0)
    free = int(st.get("availableBytes") or 0)
    work = []                                   # (ruta, nombre, es_carpeta, tamano_listado, grupo)
    for e in bf.listdir(root):
        path = "%s/%s" % (root.rstrip("/"), e["name"])
        if e.get("dir") and e["name"].lower() in EXPAND:
            for c in bf.listdir(path):
                work.append(("%s/%s" % (path, c["name"]), c["name"], bool(c.get("dir")), c.get("size") or 0,
                             e["name"].lower()))
        else:
            work.append((path, e["name"], bool(e.get("dir")), e.get("size") or 0, ""))
    extra = []
    if root == "/data":
        extra.append(("/user/app", "Instalado desde la PS5", True, 0, "system"))
    with LOCK:
        SPACE.update(total=len(work) + len(extra), done=0)
    items = []
    for i, (path, name, is_dir, lsize, group) in enumerate(work + extra):
        if SPACE_CANCEL.is_set():
            raise E.Cancelled()
        with LOCK:
            SPACE.update(done=i, current=path)
        size = bf.du(path, timeout=300) if is_dir else int(lsize or 0)
        if size is None:
            size = int(lsize or 0)
        it = {"path": path, "name": name, "size": size, "dir": is_dir, "group": group,
              "kind": "folder" if is_dir else "file", "title": None, "tid": None,
              "deletable": True, "notes": []}
        if group == "system":
            it.update(kind="system", deletable=False)
            it["notes"].append("Bórralos desde la consola: Ajustes > Almacenamiento")
        elif group == "homebrew":
            if is_dir and bf.exists(path + "/sce_sys/param.json"):
                txt = bf.read_text(path + "/sce_sys/param.json")
                it.update(kind="app", title=title_name(txt), tid=E.title_id(txt) or E.title_id(name))
            elif IMAGE_RE.search(name):
                it.update(kind="image", tid=E.title_id(name))
            elif is_dir:
                it["kind"] = "incomplete"
                it["notes"].append("No tiene sce_sys: no es una app completa (¿envío cortado o copia a medias?)")
        elif group == "pkg":
            it["kind"] = "pkg"
            it["notes"].append("Instalador .pkg: si ya está instalado, se puede borrar")
        elif root == "/data" and name == "ps5directo":
            it["kind"] = "leftover"
            it["notes"].append("Restos de envíos de PS5 Directo")
        elif root == "/data" and is_dir:
            it["kind"] = "data"
            it["notes"].append("Datos de payloads o ajustes: bórralo solo si sabes qué es")
        if re.search(r"\.(ps5part|ava-part|part|tmp)$", name, re.I):
            it["kind"] = "leftover"
            it["notes"].append("Archivo a medias de un envío")
        if is_dir and 0 < size < 1024 * 1024 and group == "homebrew":
            it["notes"].append("Casi vacía (%s)" % human(size))
        items.append(it)
    # el mismo juego dos veces (por ejemplo la carpeta y la imagen convertida a lz4)
    by_tid = {}
    for it in items:
        if it["tid"]:
            by_tid.setdefault(it["tid"].upper(), []).append(it)
    for tid, lst in by_tid.items():
        if len(lst) > 1:
            for it in lst:
                others = [x["name"] for x in lst if x is not it]
                it["dup"] = True
                it["notes"].append("Duplicado: %s también está en %s" % (tid, ", ".join(others[:3])))
    names = {k: v.get("title") for k, v in lib_load()["titles"].items() if v.get("title")}
    names.update({it["tid"].upper(): it["title"] for it in items if it["tid"] and it["title"]})
    for it in items:
        if not it["title"] and it["tid"] and it["tid"].upper() in names:
            it["title"] = names[it["tid"].upper()]
    items.sort(key=lambda x: -x["size"])
    used = max(0, total - free)
    known = sum(x["size"] for x in items)
    if extra and known > used:                 # /user/app no comparte particion: no lo contamos
        items = [x for x in items if x["kind"] != "system"]
        known = sum(x["size"] for x in items)
    return {"root": root, "total": total, "free": free, "used": used,
            "other": max(0, used - known), "items": items, "t": time.time()}


def space_start(root):
    with LOCK:
        if SPACE["busy"]:
            raise E.PipeError("Ya se está analizando")
        SPACE.update(busy=True, root=root, done=0, total=0, current="", error="")
    SPACE_CANCEL.clear()

    def run():
        try:
            res = space_scan(root)
            with LOCK:
                SPACE["result"] = res
        except E.Cancelled:
            with LOCK:
                SPACE["error"] = "Cancelado"
        except Exception as e:
            traceback.print_exc()
            with LOCK:
                SPACE["error"] = conn_error_text(e)
        finally:
            with LOCK:
                SPACE["busy"] = False
    threading.Thread(target=run, daemon=True).start()


def fm_api(p, b):
    if p == "/api/space/scan":
        space_start(str(b.get("root") or "/data"))
        return {}
    if p == "/api/space/state":
        with LOCK:
            return {"space": dict(SPACE)}
    if p == "/api/space/cancel":
        SPACE_CANCEL.set()
        return {}
    if p == "/api/fm/stat":
        st = E.BFpilot(cfg_snapshot())._req("GET", "/api/fs/stat", {"path": fm_clean(b.get("path") or "/data")}, timeout=8)
        return {"free": st.get("availableBytes"), "total": st.get("usableTotalBytes") or st.get("totalBytes")}
    if p == "/api/fm/list":
        return fm_list(b.get("path") or "/data")
    if p == "/api/fm/du":
        return {"size": fm_du(b.get("path"))}
    if p == "/api/fm/dirs":
        path = fm_clean(b.get("path") or "/data")
        return {"path": path, "dirs": sorted([e["name"] for e in fm_list(path)["entries"] if e["dir"]],
                                             key=str.lower)}
    if p == "/api/fm/conflicts":
        return {"conflicts": fm_conflicts(b.get("srcs") or [], b.get("dst") or "/")}
    if p == "/api/fm/extract":
        fm_extract(b.get("src"), b.get("folder"), str(b.get("password") or ""), b.get("exists") or "keepboth")
        return {}
    if p == "/api/fm/drives":
        bf = fm_bf_opt()
        out = [{"path": "/data", "name": "Almacenamiento interno"}]
        if bf is not None:
            for m in (bf.places() or {}).get("mounts") or []:
                mp = (m.get("path") or "").rstrip("/")
                if re.match(r"^/mnt/(usb|ext)\d+$", mp):
                    out.append({"path": mp, "name": ("USB " if "/usb" in mp else "Disco externo ") + mp[-1]})
            for d in out:
                try:
                    st = bf._req("GET", "/api/fs/stat", {"path": d["path"]}, timeout=6)
                    d["free"] = st.get("availableBytes")
                    d["total"] = st.get("usableTotalBytes") or st.get("totalBytes")
                except Exception:
                    pass
        return {"drives": out, "bfpilot": bf is not None}
    if p == "/api/fm/mkdir":
        path = fm_clean(b.get("path"))
        bf = E.BFpilot(cfg_snapshot())
        if bf.alive():
            bf.mkdir(path)
        else:
            E.Ftp(cfg_snapshot()).call(lambda f: f.mkd(path))
        return {}
    if p == "/api/report/diag":
        j = find(str(b.get("id") or ""))
        if not j:
            raise E.PipeError("No encuentro esa transferencia")
        d = diagnose(j)
        d.update(error=j.get("error") or "", phase=j.get("phase") or "", log=list(j.get("log") or [])[-40:],
                 title=_job_name(j))
        return d
    if p == "/api/report/make":
        fp = make_report(str(b.get("id") or "") or None, b.get("form"))
        return {"path": fp, "name": os.path.basename(fp), "repo": update_repo() or DEFAULT_REPO,
                "os": __import__("platform").platform(terse=True), "fw": SETTINGS.get("console_fw") or ""}
    if p == "/api/report/open":
        os.makedirs(report_dir(), exist_ok=True)
        _open_quiet(report_dir())
        return {"dir": report_dir()}
    if p == "/api/lib/checkall":
        if b.get("cancel"):
            CHECKALL_CANCEL.set()
            return {}
        with LOCK:
            if CHECKALL["busy"]:
                return {"busy": True}
            if not (LIB.get("items") or {}).get("items"):
                raise E.PipeError("Primero lee la Biblioteca de la consola")
            CHECKALL["busy"] = True
        threading.Thread(target=check_all, daemon=True).start()
        return {}
    if p == "/api/lib/checkall/state":
        with LOCK:
            return {"st": {k: (list(v) if isinstance(v, list) else v) for k, v in CHECKALL.items()}}
    if p == "/api/lib/meta":
        tid = str(b.get("tid") or "").upper()
        meta = dict((lib_load()["titles"].get(tid) or {})) if tid else {}
        meta.pop("cover", None)
        played = (played_load().get("titles") or {}).get(tid) if tid else None
        with LOCK:
            pcs = [{"path": x.get("path"), "kind": x.get("kind"), "size": x.get("size"), "version": x.get("version")}
                   for x in (COL.get("items") or []) if tid and (x.get("tid") or "").upper() == tid]
        bks = []
        try:
            for x in backup_list():
                if tid and tid in (x.get("saves") or []):
                    bks.append({"name": x["name"], "t": x["t"]})
        except Exception:
            pass
        return {"meta": meta, "played": played, "pc": pcs, "backups": bks[:10]}
    if p == "/api/lib/rename":
        if b.get("apply") or b.get("undo"):
            return rename_apply(b.get("paths") or [], undo=bool(b.get("undo")))
        return rename_plan()
    if p == "/api/lib/note":
        tid = str(b.get("tid") or "").upper()
        if not re.match(r"^[A-Z]{4}\d{5}$", tid):
            raise E.PipeError("Sin Title ID")
        d = lib_load()
        t = d["titles"].setdefault(tid, {})
        if "note" in b:
            t["note"] = str(b.get("note") or "")[:4000]
        if "tags" in b:
            t["tags"] = [str(x)[:40] for x in (b.get("tags") or [])][:20]
        lib_save(d)
        return {}
    if p == "/api/lib/check":
        return launch_check(str(b.get("path") or ""), int(b.get("minutes") or 30))
    if p == "/api/lib/checkreport":
        import zipfile
        r = launch_check(str(b.get("path") or ""), int(b.get("minutes") or 30))
        os.makedirs(report_dir(), exist_ok=True)
        tag = re.sub(r"[^\w-]+", "_", r.get("title") or r.get("tid") or "aplicacion")[:40]
        fp = os.path.join(report_dir(), "arranque-%s-%s.zip" % (tag, time.strftime("%Y-%m-%d_%H%M")))
        with zipfile.ZipFile(fp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("comprobacion.json", json.dumps({k: v for k, v in r.items() if k != "codes"},
                                                       ensure_ascii=False, indent=1))
            z.writestr("codigo_de_error.txt", str(b.get("code") or ""))
            z.writestr("ajustes_sin_contrasenas.json", json.dumps(_clean_settings(), ensure_ascii=False, indent=1))
            for k, c in enumerate(r.get("captures") or []):
                try:
                    cp, _ = media_cached(c["path"])
                    z.write(cp, "capturas/%d_%s" % (k, c["name"]))
                except Exception:
                    pass
        return {"path": fp, "name": os.path.basename(fp)}
    if p == "/api/col/state":
        with LOCK:
            return {"col": dict(COL), "roots": list(SETTINGS.get("pc_collection") or []),
                    "libdir": pc_library_dir(), "libdir_custom": bool(SETTINGS.get("pc_library_dir"))}
    if p == "/api/col/libdir":
        d = str(b.get("path") or "").strip().strip('"')
        if d and not os.path.isabs(d):
            raise E.PipeError("Pon una ruta completa, por ejemplo D:\\PS5")
        with LOCK:
            SETTINGS["pc_library_dir"] = d
        save_settings()
        with LOCK:
            COL["items"] = None
        return {"libdir": pc_library_dir()}
    if p == "/api/col/openlibdir":
        d = pc_library_dir()
        os.makedirs(d, exist_ok=True)
        _open_quiet(d)
        return {"dir": d}
    if p == "/api/lib/archive":
        # «Archivar en el PC»: se copia a la carpeta de la Biblioteca del PC, se comprueba que ha llegado entero y
        # entonces se quita de la consola (del todo: para liberar espacio). Vuelve con «Enviar a la consola».
        path = str(b.get("path") or "")
        if not path.startswith(("/data/", "/mnt/")) or path.count("/") < 3:
            raise E.PipeError("Eso no se puede archivar")
        d = pc_library_dir()
        os.makedirs(d, exist_ok=True)

        def after(files, root_local):
            bad = []
            for rp, rel, sz in files:
                lp = os.path.join(root_local, *rel.split("/"))
                if not os.path.isfile(lp) or (sz is not None and os.path.getsize(lp) != sz):
                    bad.append(rel)
            if bad or not files:
                raise E.PipeError("No se ha copiado entero al PC (%s): no lo quito de la consola."
                                  % (", ".join(bad[:3]) or "sin archivos"))
            with LOCK:
                FMOP["message"] = "Comprobado en tu PC: lo quito de la consola…"
            _delete_console(path, FM_CANCEL)
            total = sum(sz or 0 for _, _, sz in files)
            with LOCK:
                FMOP["message"] = "Archivado en tu PC (%s): liberados %s en la consola. Para devolverlo: Biblioteca › En tu PC › Enviar a la consola." % (os.path.join(root_local, posixpath.basename(path)), human(total))
            threading.Thread(target=lambda: lib_scan_safe(), daemon=True).start()
            arch_record(path, root_local, files)
        fm_download([path], dest=d, after=after)
        return {"dir": d}
    if p == "/api/archived/list":
        return arch_list()
    if p == "/api/archived/verify":
        return arch_verify_start(str(b.get("id") or ""))
    if p == "/api/archived/reveal":
        with ARCH_LOCK:
            r = next((x for x in _arch_load() if x["id"] == b.get("id")), None)
        if not r or not os.path.exists(r.get("pc") or ""):
            raise E.PipeError("Ya no está en tu PC (¿lo has movido o borrado?)")
        F.reveal_path(r["pc"])
        return {}
    if p == "/api/archived/forget":
        return arch_forget(str(b.get("id") or ""))
    if p == "/api/lib/download":
        paths = [str(x) for x in (b.get("paths") or []) if str(x).startswith(("/data/", "/mnt/"))]
        if not paths:
            raise E.PipeError("No hay nada que bajar")
        d = pc_library_dir()
        os.makedirs(d, exist_ok=True)
        fm_download(paths, dest=d)
        return {"dir": d}
    if p == "/api/col/scan":
        if not COL["busy"]:
            threading.Thread(target=collection_scan, daemon=True).start()
        return {}
    if p == "/api/col/set":
        roots = [str(x).strip().strip('"') for x in (b.get("roots") or []) if str(x).strip()]
        with LOCK:
            SETTINGS["pc_collection"] = roots
        save_settings()
        return {"roots": roots}
    if p == "/api/media/list":
        return {"items": media_list()}
    if p == "/api/media/tools":
        return {"ffmpeg": bool(ffmpeg_path()), "can_install": os.name == "nt",
                "hint": "" if os.name == "nt" else ffmpeg_hint()}
    if p == "/api/media/convert":
        paths = [fm_clean(str(x)) for x in (b.get("paths") or [])]
        paths = [x for x in paths if x.startswith(MEDIA_ROOT + "/") and re.search(r"(?i)\.(webm|mp4|mkv|mov)$", x)]
        if not paths:
            raise E.PipeError("Elige algún vídeo")
        media_convert(paths)
        return {}
    if p == "/api/media/download":
        paths = [fm_clean(str(x)) for x in (b.get("paths") or [])]
        paths = [x for x in paths if x.startswith(MEDIA_ROOT + "/")]
        if not paths:
            raise E.PipeError("Elige alguna captura")
        d = MEDIA_CONV_DIR()
        os.makedirs(d, exist_ok=True)
        fm_download(paths, dest=d, scan=False)
        return {"dir": d}
    if p == "/api/media/mp4ready":
        return {"ready": {x: bool(media_mp4_path(x) and os.path.isfile(media_mp4_path(x)))
                          for x in [str(v) for v in (b.get("paths") or [])][:500]}}
    if p == "/api/media/convstate":
        with LOCK:
            return {"conv": json.loads(json.dumps(CONV))}
    if p == "/api/media/convcancel":
        CONV_CANCEL.set()
        return {}
    if p == "/api/media/opendir":
        d = MEDIA_CONV_DIR()
        os.makedirs(d, exist_ok=True)
        _open_quiet(d)
        return {"dir": d}
    if p == "/api/grab/start":
        with LOCK:
            if GRAB["busy"]:
                raise E.PipeError("Ya estoy revisando enlaces")
            GRAB.update(busy=True, done=0, total=0, packages=[], error="")
        threading.Thread(target=grab_scan, args=(str(b.get("text") or "")[:500000],), daemon=True).start()
        return {}
    if p == "/api/grab/state":
        with LOCK:
            return {"grab": json.loads(json.dumps(GRAB))}
    if p == "/api/discover":
        return discover_ps5(hint=str(b.get("hint") or "")[:80])
    if p == "/api/played":
        with PLAYED_LOCK:
            return {"titles": played_load().get("titles") or {}}
    if p == "/api/residue/scan":
        return residue_scan()
    if p == "/api/residue/delete":
        return residue_delete([str(x) for x in (b.get("ids") or [])])
    if p == "/api/media/clearcache":
        import shutil
        shutil.rmtree(MEDIA_DIR, ignore_errors=True)
        return {}
    if p == "/api/bk/state":
        with LOCK:
            st = dict(BK)
        st["dir"] = backup_dir()
        st["last_t"] = SETTINGS.get("backup_last") or 0
        return {"bk": st}
    if p == "/api/bk/list":
        return {"items": backup_list(), "dir": backup_dir()}
    if p == "/api/bk/run":
        if BK["busy"]:
            raise E.PipeError("Ya hay una copia en marcha")
        secs = [x for x in (b.get("sections") or []) if x in ("saves", "emu", "config", "appdb", "list")] or None
        tids = [str(x).upper() for x in (b.get("tids") or []) if re.match(r"^[A-Za-z]{4}\d{5}$", str(x))]
        if tids:
            def has_saves(f):
                for r in _save_roots(f):
                    try:
                        if any(v["type"] == "dir" and n.upper()[:9] in tids for n, v in E.ftp_listdir(f, r)):
                            return True
                    except ftplib.all_errors:
                        pass
                return False
            if not E.Ftp(cfg_snapshot()).call(has_saves):
                raise E.PipeError("No encuentro partidas de %s en la consola (¿no lo has jugado todavía?)"
                                  % ", ".join(tids))
            threading.Thread(target=backup_run, args=(["saves"], "partidas-" + "-".join(tids[:3]), tids),
                             daemon=True).start()
            return {"tids": tids}
        threading.Thread(target=backup_run, args=(secs,), daemon=True).start()
        return {}
    if p == "/api/bk/cancel":
        BK_CANCEL.set()
        return {}
    if p == "/api/bk/restore":
        if BK["busy"]:
            raise E.PipeError("Espera a que termine la copia en marcha")
        bpath = os.path.abspath(str(b.get("path") or ""))
        if not bpath.startswith(os.path.abspath(backup_dir(cfg_snapshot()))) or not os.path.isfile(bpath):
            raise E.PipeError("Esa copia ya no está en la carpeta de copias (¿la moviste o borraste?). "
                              "Pulsa Actualizar en la lista de copias.")
        threading.Thread(target=backup_restore, args=(bpath, b.get("ids") or [], b.get("emus") or []),
                         daemon=True).start()
        return {}
    if p == "/api/bk/open":
        d = backup_dir()
        os.makedirs(d, exist_ok=True)
        _open_quiet(d)
        return {"dir": d}
    if p == "/api/gu/status":
        return gu_status()
    if p == "/api/gu/fix":
        st = gu_status()
        src = st.get("fix_from")
        if not src:
            raise E.PipeError("No hay nada que arreglar")
        dst = "/data/homebrew/" + GU_TID
        if st.get("present"):
            raise E.PipeError("Ya existe %s" % dst)
        bf = E.BFpilot(cfg_snapshot())
        if bf.alive():
            bf.rename(src, dst)
        else:
            E.Ftp(cfg_snapshot()).call(lambda f: f.rename(src, dst))
        parent = posixpath.dirname(src)
        if parent != "/data/homebrew":
            try:
                E.Ftp(cfg_snapshot()).call(lambda f: f.rmd(parent))   # solo si se ha quedado vacia
            except ftplib.all_errors:
                pass
        return {"path": dst}
    if p == "/api/fm/rename":
        src, dst = fm_clean(b.get("src")), fm_clean(b.get("dst"))
        if posixpath.basename(dst) in _names_in(posixpath.dirname(dst) or "/"):
            raise E.PipeError("Ya existe algo llamado %s en esa carpeta" % posixpath.basename(dst))
        bf = E.BFpilot(cfg_snapshot())
        if bf.alive():
            bf.rename(src, dst)
        else:
            E.Ftp(cfg_snapshot()).call(lambda f: f.rename(src, dst))
        return {}
    if p == "/api/fm/paste":
        op = b.get("op")
        if op not in ("copy", "move"):
            raise E.PipeError("operacion no valida")
        fm_paste(op, b.get("srcs") or [], b.get("dst"), b.get("mode") or ("replace" if b.get("overwrite") else "replace"))
        return {}
    if p == "/api/fm/delete":
        fm_delete(b.get("paths") or [], trash=bool(b.get("trash")))
        return {}
    if p == "/api/trash/list":
        return {"items": trash_list(), "days": int(SETTINGS.get("trash_days") or 7), "on": bool(SETTINGS.get("trash_on", True))}
    if p == "/api/trash/restore":
        return {"path": trash_restore(str(b.get("id") or ""))}
    if p == "/api/trash/delete":
        ids = b.get("ids")
        return {"n": trash_delete(ids=None if b.get("all") else [str(x) for x in (ids or [])])}
    if p == "/api/fm/download":
        fm_download(b.get("paths") or [])
        return {}
    if p == "/api/fm/chmod":
        fm_chmod(b.get("paths") or [])
        return {}
    if p == "/api/fm/op":
        with LOCK:
            return {"op": dict(FMOP)}
    if p == "/api/fm/cancel":
        FM_CANCEL.set()
        return {}
    if p == "/api/job/reveal":
        with LOCK:
            j = next((x for x in JOBS if x["id"] == b.get("id")), None)
            res = list((j or {}).get("result") or [])
            keep = (j or {}).get("where") == "keep"
        i = int(b.get("i") or 0)
        if not j or not keep or i >= len(res):
            raise E.PipeError("Ya no encuentro ese resultado")
        path = res[i]
        if not os.path.exists(path):
            raise E.PipeError("Ya no está en %s (¿lo has movido o borrado?)" % path)
        if b.get("open") and os.path.isdir(path):
            F.open_path(path)
        else:
            F.reveal_path(path)
        return {"path": path}
    if p == "/api/fm/open_local":
        path = FMOP.get("local") or os.path.join(downloads_dir(), "PS5 Directo")
        if os.path.isdir(path):
            _open_quiet(path)
        return {"path": path}
    if p == "/api/pick":
        return {"paths": pick_paths(b.get("kind") or "files", b.get("title") or "")}
    if p == "/api/history":
        return {"items": read_history()}
    if p == "/api/history/redo":
        h = next((x for x in read_history() if x.get("id") == b.get("id")), None)
        r = (h or {}).get("redo")
        if not r or not r.get("lines"):
            raise E.PipeError("No sé qué enlaces tenía esa transferencia (es de una versión anterior)")
        nj = new_job(r["lines"], "", h.get("dest") or SETTINGS["dest"], title=h.get("title"), where=r.get("where"),
                     raw=r.get("raw"), fmt=r.get("fmt"), comp=r.get("comp"), pcdir=r.get("pcdir") or "",
                     keep_steps=r.get("keep_steps") or "")
        nj["fmt_set"] = r.get("fmt_set", False)
        with WAKE:
            JOBS.append(nj)
            WAKE.notify_all()
        save_jobs()
        return {"id": nj["id"]}
    if p == "/api/notifs":
        with NOTIF_LOCK:
            return {"items": list(NOTIFS), "unread": sum(1 for n in NOTIFS if not n.get("read"))}
    if p == "/api/notifs/read":
        with NOTIF_LOCK:
            for n in NOTIFS:
                if b.get("all") or n["id"] == b.get("id"):
                    n["read"] = not b.get("unread")
        save_notifs()
        return {}
    if p == "/api/notifs/remove":
        with NOTIF_LOCK:
            NOTIFS[:] = [n for n in NOTIFS if n["id"] != b.get("id")]
        save_notifs()
        return {}
    if p == "/api/notifs/clear":
        with NOTIF_LOCK:
            NOTIFS[:] = []
        save_notifs()
        return {}
    if p == "/api/history/clear":
        _save_json(HISTORY_FILE, [])
        return {}
    raise E.PipeError("no existe")


_DL_DIR = []


def phone_shared(sub):
    """Android «sin PC»: la carpeta compartida del movil (Download, Documents) si se puede escribir en ella,
    para que lo bajado salga en «Archivos» y en la galeria y no se quede escondido dentro de la app."""
    for root in ("/storage/emulated/0", "/sdcard"):
        d = os.path.join(root, sub)
        try:
            if os.path.isdir(d):
                t = os.path.join(d, ".ps5d_test")
                with open(t, "w") as fh:
                    fh.write("1")
                os.remove(t)
                return d
        except OSError:
            pass
    return None


def downloads_dir():
    """Carpeta de Descargas del usuario. En Linux puede llamarse "Descargas" (xdg-user-dir)."""
    if _DL_DIR:
        return _DL_DIR[0]
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    if PHONE:
        _DL_DIR.append(phone_shared("Download") or d)
        return _DL_DIR[0]
    if sys.platform.startswith("linux"):
        try:
            import subprocess
            r = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, timeout=5)
            x = r.stdout.decode("utf-8", "replace").strip()
            if r.returncode == 0 and x and x != os.path.expanduser("~") and os.path.isdir(x):
                d = x
        except Exception:
            pass
    _DL_DIR.append(d)
    return d


def _open_quiet(path):
    try:
        F.open_path(path)
    except Exception as e:
        print("No pude abrir %s: %s" % (path, e))


def pick_paths(kind, title):
    """Abre el dialogo del sistema para elegir archivos o una carpeta del PC."""
    import subprocess
    if os.name == "nt":
        try:
            return F.pick_windows(kind, title)
        except Exception as e:
            print("Selector de PowerShell:", e)
    else:
        try:
            r = F.pick_native(kind, title)
            if r is not None:
                return r
        except Exception as e:
            print("Selector del sistema:", e)
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("pythonw.exe"):
        alt = exe[:-len("pythonw.exe")] + "python.exe"
        exe = alt if os.path.isfile(alt) else exe
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    p = subprocess.run([exe, "-c", PICK_SCRIPT, kind, title or ""], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=3600, **kw)
    if p.returncode != 0:
        raise E.PipeError("No pude abrir el selector de archivos: %s"
                          % (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["?"])[-1])
    return json.loads(p.stdout.decode("utf-8", "replace").strip() or "[]")


# ---------------------------------------------------------------------------
# Carpetas del PC: se mandan tal cual, saltando lo que ya este en la consola
# ---------------------------------------------------------------------------

def folder_job(job, src, cancel):
    cfg = cfg_snapshot()
    cfg["_limit_bps"] = live_limit_bps
    dest = (job.get("dest") or cfg["dest"]).rstrip("/")
    tid = None
    pj = os.path.join(src.path, "sce_sys", "param.json")
    if os.path.isfile(pj):
        try:
            with open(pj, encoding="utf-8", errors="replace") as fh:
                tid = E.title_id(fh.read())
        except Exception:
            pass
        tid = tid or E.title_id(src.name)
    name = (tid + "-app") if tid else src.name
    target = dest + "/" + name
    total = src.size
    jlog(job, "Carpeta con %d archivos (%s) -> %s" % (len(src.files), human(total), target))
    bf = E.BFpilot(cfg)
    free = free_space(bf)
    if free is not None:
        jlog(job, "Espacio libre en la PS5: %s" % human(free))
        if free < margin_bytes():
            raise no_space(job, total, free,
                           "No hay sitio en la PS5: quedan %s libres y el margen es %s. Libera espacio "
                           "y pulsa Reintentar." % (human(free), human(margin_bytes())))
    if tid and job_fmt(job) and not job.get("raw"):
        # carpeta de un juego del PC -> imagen (o LZ4) con PS5 Dump Forge
        res = forge_job(job, [src], cfg, cancel, src.path, [], "", Meter())
        if res is not None:
            return res
        jlog(job, "La mando como carpeta normal")
    if tid:
        check_duplicates_first(job, bf, tid, exclude=target)
        claim_target(job, target)
    up = Meter()
    last = {"v": 0, "t": 0.0}
    stop_speed = threading.Event()

    def speed_loop():
        while not stop_speed.wait(1):
            setj(job, speed=up.rate(), dl_speed=0.0)
    threading.Thread(target=speed_loop, daemon=True).start()

    def progress(v, rel):
        if v > last["v"]:
            up(v - last["v"])
            last["v"] = v
        now = time.time()
        if now - last["t"] > 0.5:
            last["t"] = now
            setj(job, sent=v, phase="Enviando carpeta: %s" % rel[-80:])

    setj(job, status="enviando", total=total, sent=0, phase="Comparando con lo que ya hay en la PS5")
    def plan(need):
        jlog(job, "Falta por enviar: %s" % human(need))
        if free is not None and need > 0 and free - need < margin_bytes():
            raise no_space(job, need, free,
                           "No cabe en la PS5: faltan %s por enviar, hay %s libres y el margen es %s. "
                           "Libera espacio y pulsa Reintentar." % (human(need), human(free), human(margin_bytes())))

    watch = start_space_watch(job, bf, cancel, "el envio")
    try:
        sent, skipped, resumed = E.upload_tree(cfg, src, target, cancel, lambda m: jlog(job, m), progress,
                                               on_plan=plan)
    finally:
        stop_speed.set()
        watch.set()
        setj(job, speed=0.0)
    setj(job, sent=total)
    jlog(job, "Carpeta enviada: %d archivos nuevos, %d ya estaban iguales, %d continuados"
         % (sent, skipped, resumed))
    if bf.alive():
        fix_game_perms(job, bf, target, cancel)
    return {"mode": "done", "placed": [target], "game": bool(tid)}


# ---------------------------------------------------------------------------
# Alternativa: descomprimir en el PC con 7-Zip y mandar el contenido ya suelto
# ---------------------------------------------------------------------------

PCX_DIR = os.path.join(DATA_DIR, "pc_extract")


def job_where(job):
    with LOCK:
        w = job.get("where") or SETTINGS.get("extract_where") or "ps5"
    return "pc" if w == "pc" else "ps5"


def job_fmt(job):
    """Formato con PS5 Dump Forge ('' = carpeta normal). Solo para lo que se prepara en el PC."""
    with LOCK:
        f = job.get("fmt")
        if f is None or f == "" and not job.get("fmt_set"):
            f = SETTINGS.get("pc_format") if job_where(job) == "pc" else ""
    if job.get("_force_plain"):
        return "plain"     # sin 7-Zip: PS5 Dump Forge tambien sabe sacar el contenido a una carpeta
    if f == "plain" and not job.get("_plain_conv"):
        return ""          # un comprimido normal ya acaba en carpeta: no hace falta Forge
    return f if f in F.FORGE_FORMATS else ""


CONV_SRC_EXTS = E.IMAGE_EXTS + (".pkg",)      # lo que Forge sabe deshacer o pasar a otro formato


# ---------------------------------------------------------------------------
# PS5 Dump Forge: imagen .ffpkg / .ffpfsc o empaquetado LZ4, preparado en el PC
# ---------------------------------------------------------------------------

FORGE_LOCK = threading.Lock()
LIB_FILE = os.path.join(DATA_DIR, "library.json")
LIB_LOCK = threading.Lock()
COVER_DIR = os.path.join(DATA_DIR, "covers")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
MEDIA_ROOT = "/user/av_contents"
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
               ".jxr": "image/vnd.ms-photo",   # capturas con la pantalla en HDR
               ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


# ---------------------------------------------------------------------------
# Diagnostico e informe de fallo
# ---------------------------------------------------------------------------

_CONN_RX = re.compile(r"(?i)10054|10053|10060|10061|10065|connection (reset|refused|aborted)|timed out|"
                      r"no route|unreachable|broken pipe|forzado la interrupci|denegado expresamente|"
                      r"no respondi|errno (32|54|60|61|64|65|104|110|111|113)")


MAC_LAN_MSG = ("macOS no deja a PS5 Directo usar la red local. Ve a Ajustes del Sistema → Privacidad y seguridad → Red local, activa «PS5 Directo» (o «python3») y vuelve a abrir el programa. Si no sale en esa lista, cierra PS5 Directo (Salir) y ábrelo otra vez desde Aplicaciones: macOS pregunta la primera vez que intenta conectar con la consola.")


def mac_lan_blocked(host=None):
    """En macOS 15+ hace falta el permiso «Red local». Si no lo tiene, conectar con la consola falla al
    instante con "No route to host" (errno 65), aunque la PS5 este encendida."""
    if sys.platform != "darwin":
        return False
    host = E.clean_host(host or cfg_snapshot().get("ps5_ip") or "")[0]
    if not host:
        return False
    t0 = time.time()
    try:
        socket.create_connection((host, 2121), timeout=2).close()
        return False
    except OSError as e:
        return getattr(e, "errno", None) == 65 and time.time() - t0 < 0.5


def conn_error_text(e):
    """Los errores de red de Windows ("[WinError 10054] Se ha forzado la interrupción...") dicen poco:
    se cambian por un mensaje claro sobre la conexión con la consola."""
    t = str(e) or e.__class__.__name__
    if sys.platform == "darwin" and re.search(r"(?i)no route to host|errno 65", t):
        return MAC_LAN_MSG + " (%s)" % t
    if isinstance(e, (ConnectionError, TimeoutError, socket.timeout)) or _CONN_RX.search(t):
        return ("No hay conexión con la consola. Comprueba que la PS5 está encendida, que el FTP está cargado "
                "(arriba el punto «FTP» tiene que estar en verde) y pulsa Actualizar. (%s)" % t)
    return t


DIAG_RULES = [
    (r"(?i)10061|refused|rechaz|No route|timed out|unreachable|No puedo conectar con el FTP|conexi[oó]n con la PS5",
     "No se pudo hablar con la consola por FTP.",
     ["Comprueba que la PS5 está encendida y que el servidor FTP (ftpsrv) está cargado: el punto de FTP arriba tiene que "
      "estar en verde.", "Revisa la IP de la PS5 en Ajustes.",
      "Si el PC y la PS5 están en redes distintas (por ejemplo 192.168.50.x y 192.168.1.x), conéctalos al mismo router.",
      "En Ajustes, «Revisión completa» te dice qué falla."]),
    (r"(?i)BFpilot", "BFpilot no responde o no está cargado.",
     ["Carga BFpilot en la consola (pulsa el punto de BFpilot arriba) y pulsa Reintentar.",
      "Si no puedes usar BFpilot, elige «En el PC (7-Zip)» en «Dónde descomprimir»."]),
    (r"(?i)No (cabe|hay sitio) en la PS5|Parado para no llenar|ESPACIO", "No hay espacio suficiente en la consola.",
     ["Libera espacio (pestaña Espacio → «Hacer sitio») y pulsa Reintentar: sigue donde iba.",
      "O baja el margen que se deja libre en Ajustes."]),
    (r"(?i)No cabe en el PC|No space left|Errno 28", "No hay espacio suficiente en el disco del PC.",
     ["Libera espacio en el PC o cambia la carpeta temporal en Ajustes a otro disco.",
      "Sin convertir y descomprimiendo en la PS5 no hace falta sitio en el PC."]),
    (r"(?i)contrase|password|encrypt", "El comprimido tiene contraseña y no se ha encontrado la buena.",
     ["Escribe la contraseña en la transferencia (o en Ajustes → contraseñas) y pulsa Reintentar."]),
    (r"(?i)CRC|da[nñ]ad|corrupt|checksum|sumas", "Algún archivo ha llegado dañado o no cuadra con su comprobación.",
     ["Pulsa «Descargar de nuevo»: si vuelve a pasar, el archivo de origen está mal.",
      "Si tiene contraseña, comprueba que es la correcta (una contraseña mala también da error de CRC)."]),
    (r"(?i)HTTP (403|404|410)|caduc|expired|Forbidden|Not Found", "El enlace de descarga ya no vale (caducado o borrado).",
     ["Saca un enlace nuevo en la web de descarga y pégalo otra vez."]),
    (r"(?i)Name or service not known|getaddrinfo|nodename nor servname|No se puede abrir el enlace|11001",
     "El PC no ha podido llegar al servidor del enlace.",
     ["Comprueba que el PC tiene internet.", "Revisa que el enlace está bien copiado, entero y sin espacios.",
      "Si es de tu NAS, comprueba que está encendido y que el nombre o la IP son correctos."]),
    (r"(?i)quedo corta|se cort|Conexion cortada|connection reset|reset by peer|incompleta", "La descarga o el envío se cortó a medias.",
     ["Pulsa Reintentar: sigue donde iba, no empieza de cero.",
      "Si pasa a menudo, prueba con cable de red o baja «Conexiones por archivo» en Ajustes."]),
    (r"(?i)HTTP 429|limita las conexiones|Too Many", "El servidor de descarga está limitando las conexiones.",
     ["Espera unos minutos y pulsa Reintentar, o baja «Conexiones por archivo» en Ajustes."]),
    (r"(?i)CE-107750|permis", "Problema de permisos: la consola no deja abrir los archivos.",
     ["En la Biblioteca pulsa «Permisos» en esa aplicación (pone 777)."]),
    (r"(?i)pide (un )?firmware m[aá]s|no arrancar|solo funcionan con firmware|no te funcionar", "Pide un firmware más nuevo que el de tu PS5.",
     ["Busca una versión con backport para tu firmware o una versión anterior."]),
    (r"(?i)Dump Forge|Forge:|convert", "Ha fallado la conversión de formato en el PC.",
     ["Prueba «Sin convertir» o «Carpeta»: el contenido se envía igual.",
      "Si quieres convertir, prueba otro formato (.exfat suele ser el más compatible)."]),
    (r"(?i)AMPR|LZ4", "LZ4 no vale para este contenido.",
     ["Elige «Sin convertir», «Carpeta» o «Imagen .exfat»."]),
    (r"(?i)torrent|aria2", "No se pudo bajar el torrent.",
     ["Comprueba que el torrent tiene gente compartiendo (pares) y que el PC tiene internet.",
      "Si el antivirus o el firewall bloquean aria2, dale permiso."]),
    (r"(?i)Ya existe", "En la consola ya hay algo con ese nombre.",
     ["Elige en la cola si lo colocas igualmente con otro nombre, reemplazas lo que hay o cancelas."]),
]


def diagnose(job):
    bad = [x for x in list(job.get("log") or [])[-60:]
           if re.search(r"(?i)error|fallo|no pude|no se pudo|no puedo|aviso|warning|ojo|no cabe|no hay sitio|aria2:|HTTP \d{3}|corta", x)]
    text = " ".join([job.get("error") or "", job.get("phase") or ""] + bad)
    first = job.get("error") or ""
    found, seen = [], set()
    # primero lo que explica el error final; luego lo que sale en el registro
    for src in (first, text):
        for rx, cause, fixes in DIAG_RULES:
            if cause in seen or not re.search(rx, src):
                continue
            seen.add(cause)
            found.append({"cause": cause, "fixes": fixes})
    if not found:
        found.append({"cause": "No reconozco este error.",
                      "fixes": ["Pulsa Reintentar: muchos fallos de red se arreglan solos.",
                                "Si sigue fallando, guarda el informe y pásaselo a quien te ayude."]})
    codes = sorted(set(re.findall(r"\b(CE-\d{6}-\d|HTTP \d{3}|Errno \d+|WinError \d+|errorCode=\d+|codigo -?\d+|código -?\d+|0x[0-9a-fA-F]{6,8})\b",
                                  text)))
    return {"items": found[:4], "codes": codes}


SECRET_KEYS = ("ftp_pass", "passwords", "lan_token", "token", "password")


def _clean_settings():
    with LOCK:
        st = dict(SETTINGS)
    for k in list(st):
        if any(x in k.lower() for x in SECRET_KEYS):
            st[k] = "(oculto)" if st[k] else ""
    return st


def _clean_job(j):
    d = {k: v for k, v in j.items() if k not in ("password", "info")}
    d["has_password"] = bool(j.get("password"))
    return d


EXT_DIR = os.path.join(DATA_DIR, "extension")      # copia fija de la extension: el navegador la carga desde aqui
EXT_SEEN = {"version": None, "t": 0}


def _ext_version(folder):
    try:
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            return str(json.load(f).get("version") or "")
    except Exception:
        return ""


def sync_extension():
    """Copia la extension a una carpeta que no cambia (~/.ps5directo/extension). Asi, aunque PS5 Directo se
    mueva o se actualice, el navegador siempre la carga del mismo sitio y basta recargarla (lo hace ella sola)."""
    import shutil
    src = os.path.join(HERE, "extension")
    if not os.path.isdir(src):
        return
    if _ext_version(src) and _ext_version(src) == _ext_version(EXT_DIR):
        return
    try:
        os.makedirs(EXT_DIR, exist_ok=True)
        for n in os.listdir(src):
            if os.path.isfile(os.path.join(src, n)):
                shutil.copy2(os.path.join(src, n), os.path.join(EXT_DIR, n))
    except Exception as e:
        print("No pude copiar la extension:", e)


def _vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:3])


def job_pct(j):
    """Porcentaje de un trabajo para la extension (la fase que este haciendo)."""
    st = j.get("status")
    if st == "hecho":
        return 100
    if st == "extrayendo":
        return int(j.get("extract_pct") or 0)
    if st == "convirtiendo":
        return int(j.get("conv_pct") or 0)
    tot, sent = j.get("total") or 0, j.get("sent") or 0
    return int(min(100, sent * 100 / tot)) if tot > 0 else 0


def _job_name(j):
    if j.get("title"):
        return j["title"]
    src = (j.get("sources") or [{}])[0].get("name") or ((j.get("lines") or [""])[0].replace("\\", "/").rsplit("/", 1)[-1])
    return src or "transferencia"


def report_dir():
    return os.path.join(downloads_dir(), "PS5 Directo", "Informes")


def make_report(job_id=None, form=None):
    """Un .zip para pasar a quien te ayuda: registro, ajustes (sin contraseñas), versiones y estado."""
    import zipfile
    import platform
    with LOCK:
        jobs = [dict(j) for j in JOBS if job_id is None or j["id"] == job_id]
    if job_id and not jobs:
        raise E.PipeError("No encuentro esa transferencia")
    os.makedirs(report_dir(), exist_ok=True)
    tag = re.sub(r"[^\w-]+", "_", _job_name(jobs[0]) if job_id else "general")[:40]
    fp = os.path.join(report_dir(), "informe-%s-%s.zip" % (tag, time.strftime("%Y-%m-%d_%H%M")))
    forge = F.find_forge(SETTINGS.get("forge_path"), DATA_DIR)
    info = {"app": "PS5 Directo", "version": VERSION, "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "os": platform.platform(), "python": sys.version.split()[0],
            "forge": os.path.basename(os.path.dirname(forge)) if forge else None,
            "console_fw": SETTINGS.get("console_fw")}
    try:
        info["connection"] = test_connection(cfg_snapshot())
    except Exception as e:
        info["connection"] = str(e)
    try:
        info["network"] = net_mismatch(cfg_snapshot()) or "PC y PS5 en la misma red"
    except Exception:
        pass
    summary = []
    form = form if isinstance(form, dict) else {}
    told = ""
    if any(str(form.get(k) or "").strip() for k in ("doing", "what", "steps")):
        told = ("Qué estaba haciendo: %s\n\nQué pasó:\n%s\n\nPasos para que vuelva a pasar:\n%s\n"
                % (str(form.get("doing") or "-")[:200], str(form.get("what") or "-")[:5000],
                   str(form.get("steps") or "-")[:5000]))
    with zipfile.ZipFile(fp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("version_y_conexion.json", json.dumps(info, ensure_ascii=False, indent=1))
        if told:
            z.writestr("lo_que_paso.txt", told)
        z.writestr("ajustes_sin_contrasenas.json", json.dumps(_clean_settings(), ensure_ascii=False, indent=1))
        for j in jobs:
            name = re.sub(r"[^\w-]+", "_", (j.get("title") or j["id"]))[:40] + "_" + j["id"]
            z.writestr("transferencias/%s.json" % name, json.dumps(_clean_job(j), ensure_ascii=False, indent=1))
            z.writestr("transferencias/%s.txt" % name, "\n".join(j.get("log") or []))
            dg = diagnose(j) if j.get("status") in ("error", "pausado", "cancelado") else None
            summary.append("%s [%s]%s" % (j.get("title") or j["id"], j.get("status"),
                                          (": " + (j.get("error") or "")) if j.get("error") else ""))
            if dg:
                for it in dg["items"]:
                    summary.append("  - " + it["cause"])
        if job_id is None:
            z.writestr("actividad.json", json.dumps(read_history()[-60:], ensure_ascii=False, indent=1))
            with NOTIF_LOCK:
                z.writestr("avisos.json", json.dumps(NOTIFS[:60], ensure_ascii=False, indent=1))
        try:   # registro tecnico del programa (sin el codigo de acceso de la sesion)
            with open(os.path.join(DATA_DIR, "ps5directo.log"), encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-400:]
            z.writestr("registro_del_programa.txt",
                       "".join(re.sub(r"([#?&]t=)[\w-]+", r"\1(oculto)", ln) for ln in lines))
        except OSError:
            pass
        with AUTOBUG_LOCK:
            if AUTOBUG["items"]:
                z.writestr("fallos_detectados.json", json.dumps(AUTOBUG["items"], ensure_ascii=False, indent=1))
        z.writestr("LEEME.txt", "Informe de PS5 Directo %s (%s)\n\n%s%s\n\nNo lleva contraseñas. Pásaselo a quien te "
                   "ayude.\n" % (VERSION, info["date"], (told + "\n") if told else "", "\n".join(summary)))
    return fp


# ---------------------------------------------------------------------------
# Comprobar arranque: por que una aplicacion de la Biblioteca no abre (solo lectura)
# ---------------------------------------------------------------------------

LAUNCH_CODES = {
    "SIGBUS": "La aplicación leyó memoria que no podía. Si está en LZ4, casi seguro que ese ejecutable no usa AMPR y "
              "no es compatible: vuelve a ponerla sin LZ4 (carpeta o .exfat) o usa «Imagen comprimida .ffpfsc».",
    "SIGSEGV": "La aplicación se ha cerrado por un fallo de memoria. Si está en LZ4, prueba la versión sin convertir. "
               "Si no, puede faltar un backport para tu firmware o estar dañada: vuelve a enviarla.",
    "CE-107750-0": "La consola no puede abrir los archivos: suele ser un problema de permisos. Pulsa «Permisos» "
                   "en la tarjeta (pone 777) y prueba otra vez.",
    "CE-108255-1": "La aplicación se ha cerrado por un error al arrancar. Si es LZ4, prueba la versión sin convertir "
                   "o .exfat; si no, mira el firmware que pide y el backport.",
    "CE-100005-6": "La consola no puede leer los datos: pueden estar incompletos o dañados. Vuelve a enviarla.",
    "CE-100002-3": "Pide una versión del sistema más nueva. Busca una versión con backport para tu firmware.",
    "CE-34878-0": "La aplicación se ha cerrado por un error. Prueba a reiniciar la consola y vuelve a abrirla; si "
                  "sigue, vuelve a enviarla o prueba otro formato.",
    "CE-30005-8": "Error al leer los datos de la aplicación. Vuelve a enviarla.",
}
LOG_RX = re.compile(r"(?i)\.(log|txt)$")


def _console_now(f):
    """La hora segun la consola (su FTP puede ir con otra zona horaria que el PC): crea un archivo vacio en la
    carpeta de trabajo, mira la fecha que le pone y lo borra."""
    import io
    p = WORK_ROOT + "/.reloj"
    try:
        E.Ftp.mkdirs(f, WORK_ROOT)
        f.storbinary("STOR " + p, io.BytesIO(b""))
        mt = dict(E.ftp_listdir(f, WORK_ROOT)).get(".reloj", {}).get("mtime")
        try:
            f.delete(p)
        except ftplib.all_errors:
            pass
        return mt or time.time()
    except ftplib.all_errors:
        return time.time()


def _recent_logs(f, roots, since, limit=6):
    """Archivos .log/.txt modificados despues de 'since' (hasta 3 niveles), con sus ultimas lineas."""
    found = []

    def walk(path, depth):
        try:
            ls = E.ftp_listdir(f, path)
        except ftplib.all_errors:
            return
        for n, v in ls:
            p = path.rstrip("/") + "/" + n
            if v["type"] == "dir":
                if depth < 3 and n not in ("homebrew", "pkg", "ps5directo") and not n.startswith("."):
                    walk(p, depth + 1)
            elif LOG_RX.search(n) and (v.get("mtime") or 0) >= since and (v.get("size") or 0) < 64 * 1024 * 1024:
                found.append((v.get("mtime") or 0, p, v.get("size") or 0))
    for r in roots:
        walk(r, 0)
    found.sort(reverse=True)
    out = []
    for mt, p, sz in found[:limit]:
        try:
            raw = F.ftp_read(f, p, max(0, sz - 6000), 6000).decode("utf-8", "replace")
        except Exception:
            raw = ""
        out.append({"path": p, "mtime": mt, "tail": raw[-6000:]})
    return out


def launch_check(path, minutes=30, quick=False):
    cfg = cfg_snapshot()
    path = fm_clean(path)
    checks = []

    def add(level, text, fix=""):
        checks.append({"level": level, "text": text, "fix": fix})
    info = {}

    def run(f):
        is_dir = True
        try:
            ls = {n: v for n, v in E.ftp_listdir(f, path)}
        except ftplib.all_errors:
            is_dir = False
            ls = {}
        if not is_dir:
            add("info", "Es una imagen (%s): lo de dentro solo se puede revisar cuando ShadowMountPlus la monta."
                % posixpath.basename(path), "Si no aparece en el menú, prueba a enviarla como carpeta o .exfat.")
        else:
            sce = {}
            if "sce_sys" in ls:
                sce = {n: v for n, v in E.ftp_listdir(f, path + "/sce_sys")}
            if "eboot.bin" in ls:
                add("ok", "Tiene eboot.bin (el ejecutable principal).")
                mode = ls["eboot.bin"].get("mode")
                if mode is not None and (mode & 0o111) == 0:
                    add("bad", "eboot.bin no tiene permiso de ejecución.", "Pulsa «Permisos» en la tarjeta (pone 777).")
            else:
                add("bad", "Falta eboot.bin: la aplicación no puede arrancar.", "Vuelve a enviarla: está incompleta.")
            if "param.json" in sce or "param.sfo" in sce:
                add("ok", "Tiene sce_sys/param.json.")
            else:
                add("bad", "Falta sce_sys/param.json: la consola no sabe qué es.", "Vuelve a enviarla: está incompleta.")
            if "icon0.png" not in sce:
                add("warn", "Falta la portada (icon0.png). No impide arrancar.")
            facts = {}
            if "param.json" in sce:
                try:
                    facts = F.param_facts(F.ftp_read(f, path + "/sce_sys/param.json", 0, 512 * 1024)
                                          .decode("utf-8-sig", "replace"))
                except Exception:
                    pass
            info.update(tid=facts.get("tid") or E.title_id(posixpath.basename(path)), title=facts.get("title"))
            fw_exe = None
            if "eboot.bin" in ls:
                try:
                    fw_exe = F.exe_firmware(lambda off, n: F.ftp_read(f, path + "/eboot.bin", off, n))
                except Exception:
                    pass
            backport = "fakelib" in ls or "fakelib2" in ls
            need = (fw_exe if backport else None) or facts.get("fw_param") or fw_exe
            mine = (SETTINGS.get("console_fw") or "").strip()
            if need and mine:
                ok = F.fw_ok(mine, need)
                if ok is False:
                    add("bad", "Pide firmware %s y tu PS5 tiene %s." % (need, mine),
                        "Necesitas una versión con backport para tu firmware.")
                elif ok:
                    add("ok", "Firmware: pide %s y tu PS5 tiene %s." % (need, mine))
            elif need:
                add("info", "Pide firmware %s. Pon el de tu PS5 en Ajustes para comprobarlo." % need)
            if backport:
                add("info", "Lleva backport (carpeta fakelib).")
            # LZ4 / AMPR
            amp = [n for n in ls if n.lower().startswith("ampr_assets")]
            if amp:
                vols = [n for n in amp if n.lower() != "ampr_assets.index" and not n.lower().endswith((".crc", ".runtime"))]
                if "ampr_assets.index" not in [n.lower() for n in amp]:
                    add("bad", "Está empaquetada en LZ4 pero falta ampr_assets.index.", "Vuelve a convertirla o envíala sin convertir.")
                else:
                    add("ok", "LZ4: tiene el índice de paquetes (%d archivos de datos)." % len(vols))
                if not any(n.lower() == "ampr_emu.index" for n in ls):
                    add("warn", "LZ4: falta ampr_emu.index.", "Si no arranca, vuelve a convertirla o envíala sin convertir.")
                if not any(n.lower().endswith(".runtime") for n in amp):
                    add("info", "LZ4: no lleva perfil .runtime (usa los valores por defecto). Si se cierra al arrancar, "
                        "es una causa habitual.", "Prueba a enviarla sin convertir, como carpeta o .exfat.")
                empty = [n for n in vols if not ls[n].get("size")]
                if empty:
                    add("bad", "LZ4: hay archivos de datos vacíos (%s)." % ", ".join(empty[:4]), "Vuelve a enviarla.")
                fl = [d for d in ("fakelib", "fakelib2") if d in ls]
                has_ampr = False
                for d in fl:
                    try:
                        has_ampr |= any("ampr" in n.lower() for n, _ in E.ftp_listdir(f, path + "/" + d))
                    except ftplib.all_errors:
                        pass
                if has_ampr:
                    add("ok", "LZ4: lleva el emulador AMPR en su fakelib.")
                else:
                    add("warn", "LZ4: no encuentro el emulador AMPR (libSceAmpr) en fakelib.",
                        "Sin él los datos empaquetados no se pueden leer. Vuelve a convertirla.")
                info["lz4"] = True
                # LZ4 solo funciona si cada ejecutable lee sus datos con AMPR (las colecciones llevan varios)
                exes = [n for n in ls if not ls[n].get("type") == "dir" and
                        (n.lower() == "eboot.bin" or n.lower().endswith((".elf", ".self")))]
                no_ampr = []
                for n in sorted(exes, key=lambda x: x.lower() != "eboot.bin")[:8]:
                    try:
                        r = F.exe_uses_ampr(lambda off, k, n=n: F.ftp_read(f, path + "/" + n, off, k))
                    except Exception:
                        r = None
                    if r is False:
                        no_ampr.append(n)
                tid0 = (facts.get("tid") or E.title_id(posixpath.basename(path)) or "").upper()
                forge_no = bool((lib_load()["titles"].get(tid0) or {}).get("no_ampr"))
                if no_ampr or forge_no:
                    who = ", ".join(no_ampr) if no_ampr else "el título"
                    verb = "no leen" if len(no_ampr) > 1 else "no lee"
                    add("bad", "LZ4 no sirve con este título: %s %s sus datos con AMPR, así que con LZ4 recibe "
                        "los datos comprimidos sin descomprimir y se cierra (suele salir SIGBUS o «general "
                        "protection fault»)." % (who, verb),
                        "Vuelve a ponerlo sin LZ4 (carpeta o .exfat). Para ahorrar espacio con este título, usa "
                        "«Imagen comprimida .ffpfsc», que no depende de AMPR.")
                    info["lz4_bad"] = True
                elif len(exes) > 1:
                    add("info", "Tiene varios ejecutables (%s): en las colecciones, si uno no usa AMPR se cierra "
                        "al elegirlo aunque el menú arranque bien." % ", ".join(exes[:5]))
            # zstd (PS5 Dump Forge 0.5): ampr_zstd.index + ampr_zstd-NNN.pak, leidos por ampr-emu-ng
            zst = [n for n in ls if n.lower().startswith("ampr_zstd")]
            if zst:
                vols = [n for n in zst if n.lower().endswith(".pak")]
                if "ampr_zstd.index" not in [n.lower() for n in zst]:
                    add("bad", "Está empaquetada en zstd pero falta ampr_zstd.index.", "Vuelve a convertirla o envíala sin convertir.")
                else:
                    add("ok", "zstd: tiene el índice de paquetes (%d archivos de datos)." % len(vols))
                empty = [n for n in vols if not ls[n].get("size")]
                if empty:
                    add("bad", "zstd: hay archivos de datos vacíos (%s)." % ", ".join(empty[:4]), "Vuelve a enviarla.")
                has_rt = False
                for d in [d for d in ("fakelib", "fakelib2") if d in ls]:
                    try:
                        has_rt |= any(n.lower() == "libsceampr.sprx" for n, _ in E.ftp_listdir(f, path + "/" + d))
                    except ftplib.all_errors:
                        pass
                if has_rt:
                    add("ok", "zstd: lleva el emulador AMPR (ampr-emu-ng) en su fakelib.")
                else:
                    add("bad", "zstd: no encuentro el emulador AMPR (fakelib/libSceAmpr.sprx).",
                        "Sin él no puede leer sus datos. Vuelve a convertirla.")
                info["lz4"] = True
            if amp or zst:
                add("info", "Si ShadowMountPlus tiene activada la actualización de fakelib, puede cambiar el emulador AMPR "
                    "de este título por el suyo y entonces no lee sus datos empaquetados.",
                    "Si se cierra al arrancar, desactiva esa opción en ShadowMountPlus y vuelve a enviarlo.")
        # backports de otras carpetas que pueden pisar los de esta
        tid = (info.get("tid") or E.title_id(posixpath.basename(path)) or "").upper()
        if tid:
            for base in ("/data/shadowmount/backports", "/data/shadowmountplus/backports", WORK_ROOT + "/backports"):
                try:
                    names = [n for n, v in E.ftp_listdir(f, base) if tid in n.upper()]
                except ftplib.all_errors:
                    continue
                for n in names:
                    add("warn", "Hay un backport aparte para este título en %s/%s, que puede sustituir al suyo." % (base, n),
                        "Si se cierra al arrancar, prueba a apartar esa carpeta.")
        since = _console_now(f) - minutes * 60
        info["logs"] = [] if quick else _recent_logs(f, ([path] if is_dir else []) + ["/data"], since)
    E.Ftp(cfg).call(run)
    # cierres que ha visto ShadowMountPlus (signal / general protection fault) en los registros recientes
    tid = (info.get("tid") or "").upper()
    for lg in info.get("logs") or []:
        m = None
        for m in re.finditer(r"game error: ([A-Z]{4}\d{5})[^\n]*?: signal: (\d+) \((\w+)\)(?:\s*thread name: (\S+))?",
                             lg.get("tail") or ""):
            pass
        if m and (not tid or m.group(1).upper() == tid):
            sig, thread = m.group(3), m.group(4) or ""
            if info.get("lz4") and sig in ("SIGBUS", "SIGSEGV"):
                if not info.get("lz4_bad"):
                    add("bad", "Se cerró con %s%s y está empaquetado en LZ4: lo más probable es que ese ejecutable no "
                        "lea sus datos con AMPR y no sea compatible con LZ4." % (sig, " en " + thread if thread else ""),
                        "Vuelve a ponerlo sin LZ4 (carpeta o .exfat), o usa «Imagen comprimida .ffpfsc» para ahorrar "
                        "espacio.")
            else:
                add("warn", "ShadowMountPlus vio que se cerró con %s%s." % (sig, " en " + thread if thread else ""),
                    "Mira el registro de abajo. Si acabas de convertirlo, prueba la versión sin convertir.")
            break
    lib = (LIB.get("items") or {}).get("items") or []
    me = next((x for x in lib if x.get("path") == path), {})
    if me.get("dup"):
        add("warn", "Está repetida en %s: dos copias con el mismo Title ID pueden liar a ShadowMountPlus." % ", ".join(me["dup"]),
            "Deja solo una.")
    try:
        caps = [] if quick else media_list()[:4]
    except Exception:
        caps = []
    info.update(path=path, checks=checks, captures=caps, codes=LAUNCH_CODES)
    return info


CHECKALL = {"busy": False, "done": 0, "total": 0, "current": "", "results": [], "error": "", "t": 0}
CHECKALL_CANCEL = threading.Event()


def check_all():
    """«Revisar todo»: «Comprobar arranque» de cada cosa de la Biblioteca, de una vez (sin registros ni capturas)."""
    items = [x for x in ((LIB.get("items") or {}).get("items") or [])
             if x.get("kind") in ("folder", "image") and not x.get("extra") and str(x.get("path", "")).startswith(("/data/", "/mnt/"))]
    with LOCK:
        CHECKALL.update(busy=True, done=0, total=len(items), current="", results=[], error="")
    CHECKALL_CANCEL.clear()
    rank = {"bad": 3, "warn": 2, "info": 1, "ok": 0}
    try:
        for i, it in enumerate(items):
            if CHECKALL_CANCEL.is_set():
                break
            with LOCK:
                CHECKALL.update(done=i, current=it.get("title") or it.get("path"))
            try:
                r = launch_check(it["path"], 0, quick=True)
                probs = [c for c in r.get("checks") or [] if c["level"] in ("bad", "warn")
                         and not c["text"].startswith("Falta la portada")]     # no impide arrancar
                probs.sort(key=lambda c: -rank[c["level"]])
                res = {"path": it["path"], "title": it.get("title") or posixpath.basename(it["path"]), "tid": it.get("tid"),
                       "level": probs[0]["level"] if probs else "ok", "issues": probs[:4]}
            except Exception as e:
                res = {"path": it["path"], "title": it.get("title") or it["path"], "tid": it.get("tid"), "level": "warn",
                       "issues": [{"level": "warn", "text": "No pude revisarlo: %s" % e, "fix": ""}]}
            with LOCK:
                CHECKALL["results"].append(res)
    except Exception as e:
        with LOCK:
            CHECKALL["error"] = str(e)
    finally:
        with LOCK:
            CHECKALL.update(busy=False, done=CHECKALL["total"], current="", t=time.time())


COL = {"busy": False, "items": None, "error": "", "t": 0, "current": ""}
COL_EXT = re.compile(r"(?i)\.(exfat|ffpkg|ffpfs|ffpfsc|pkg|zip|7z|rar|001)$")


def _local_dir_size(path, limit_s=8.0):
    t0, total = time.time(), 0
    for d, _dirs, fs in os.walk(path):
        for n in fs:
            try:
                total += os.path.getsize(os.path.join(d, n))
            except OSError:
                pass
        if time.time() - t0 > limit_s:
            return None
    return total


def collection_scan():
    """Lee las carpetas de la coleccion del PC: aplicaciones en carpeta (sce_sys), imagenes, .pkg y comprimidos."""
    lib = pc_library_dir()
    roots = [lib] + [r for r in (SETTINGS.get("pc_collection") or [])
                     if r and os.path.normcase(os.path.abspath(r)) != os.path.normcase(os.path.abspath(lib))]
    with LOCK:
        COL.update(busy=True, error="", current="")
    items = []
    try:
        for root in roots:
            if root == lib and not os.path.isdir(root):
                continue   # la carpeta de la Biblioteca se crea al bajar lo primero
            if not os.path.isdir(root):
                items.append({"path": root, "kind": "missing", "name": root})
                continue
            unread = []
            for d, dirs, fs in os.walk(root, onerror=lambda e: unread.append(getattr(e, "filename", "") or str(e))):
                depth = d[len(root):].count(os.sep)
                with LOCK:
                    COL["current"] = d
                if "sce_sys" in dirs:
                    facts = {}
                    pj = os.path.join(d, "sce_sys", "param.json")
                    if os.path.isfile(pj):
                        try:
                            with open(pj, encoding="utf-8-sig", errors="replace") as fh:
                                facts = F.param_facts(fh.read())
                        except OSError:
                            pass
                    sfo = os.path.join(d, "sce_sys", "param.sfo")
                    if not facts.get("tid") and os.path.isfile(sfo):
                        with open(sfo, "rb") as fh:
                            facts = F.sfo_facts(fh.read())
                    items.append({"path": d, "kind": "folder", "name": os.path.basename(d),
                                  "tid": facts.get("tid") or E.title_id(os.path.basename(d)),
                                  "title": facts.get("title"), "version": facts.get("version"),
                                  "need_fw": facts.get("fw_param") or facts.get("need_fw"),
                                  "size": _local_dir_size(d), "icon": os.path.isfile(os.path.join(d, "sce_sys", "icon0.png"))})
                    dirs[:] = []
                    continue
                if depth >= 4:
                    dirs[:] = []
                for n in sorted(fs):
                    if not COL_EXT.search(n):
                        continue
                    low = n.lower()
                    # de los comprimidos por partes solo cuenta la primera (.part1.rar / .001)
                    if re.search(r"\.part0*([2-9]|[1-9]\d+)\.rar$", low):
                        continue
                    fp = os.path.join(d, n)
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        sz = None
                    kind = "image" if re.search(r"(?i)\.(exfat|ffpkg|ffpfs|ffpfsc)$", n) else (
                        "pkg" if low.endswith(".pkg") else "archive")
                    tid = E.title_id(n)
                    t = (lib_load().get("titles") or {}).get((tid or "").upper()) or {}
                    items.append({"path": fp, "kind": kind, "name": n, "tid": tid, "title": t.get("title"),
                                  "size": sz, "ext": posixpath.splitext(low)[1].lstrip(".")})
            # una carpeta que no se pudo leer (disco dormido, NAS caído, permisos): se dice, para no creer que no está
            for u in unread[:20]:
                items.append({"path": u, "kind": "missing", "name": u, "unread": True})
        with LOCK:
            COL.update(items=items, t=time.time())
    except Exception as e:
        traceback.print_exc()
        with LOCK:
            COL["error"] = conn_error_text(e)
    finally:
        with LOCK:
            COL.update(busy=False, current="")


def col_icon(path):
    path = os.path.abspath(path)
    if not any(path.startswith(os.path.abspath(r)) for r in (SETTINGS.get("pc_collection") or []) if r):
        return None
    fp = os.path.join(path, "sce_sys", "icon0.png")
    if os.path.isfile(fp):
        with open(fp, "rb") as fh:
            return fh.read()
    return None


def media_list():
    """Capturas y videos de la consola (los mas nuevos primero). La PS5 los guarda en
    /user/av_contents/{photo,video}/NPXS40087/<Title ID>/<carpeta>/<fecha>.jpg|jxr|webm."""
    cfg = cfg_snapshot()
    out = []
    files = E.Ftp(cfg).call(lambda f: _ftp_walk_sizes(f, MEDIA_ROOT, [], threading.Event()))
    titles = lib_load().get("titles") or {}
    for p, sz in files:
        ext = posixpath.splitext(p)[1].lower()
        low = p.lower()
        # miniaturas y copias temporales que hace la consola al compartir
        if ext not in MEDIA_TYPES or "/thumbnail" in low or "/content_tmp/" in low:
            continue
        name = posixpath.basename(p)
        tids = [c for c in p.split("/") if re.match(r"^[A-Z]{4}\d{5}$", c) and c != "NPXS40087"]
        tid = tids[-1] if tids else ""
        m = re.search(r"(20\d{2})(\d{2})(\d{2})_?(\d{2})(\d{2})(\d{2})?", name)
        when = "%s-%s-%sT%s:%s:%s" % (m.group(1), m.group(2), m.group(3), m.group(4), m.group(5),
                                       m.group(6) or "00") if m else ""
        out.append({"path": p, "name": name, "size": sz,
                    "video": MEDIA_TYPES[ext].startswith("video"), "hdr": ext == ".jxr",
                    "tid": tid, "app": (titles.get(tid) or {}).get("title") or "",
                    "when": when, "folder": posixpath.basename(posixpath.dirname(p))})
    out.sort(key=lambda x: (x["when"] or "", x["name"]), reverse=True)
    return out[:3000]


def _jxr_to_jpg(src, dst):
    """Vista previa de una captura HDR (.jxr): Windows trae el decodificador de JPEG XR
    (y en Mac se intenta con sips, la herramienta de imagenes del sistema)."""
    if sys.platform == "darwin":
        import subprocess
        try:
            r = subprocess.run(["sips", "-s", "format", "jpeg", src, "--out", dst], capture_output=True, timeout=60)
            return r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0
        except Exception:
            return False
    if os.name != "nt":
        return False
    q = lambda t: "'" + t.replace("'", "''") + "'"
    script = (
        "Add-Type -AssemblyName PresentationCore;"
        "$s=[IO.File]::OpenRead(%s);"
        "try{ $d=[System.Windows.Media.Imaging.BitmapDecoder]::Create($s,"
        " [System.Windows.Media.Imaging.BitmapCreateOptions]::None,"
        " [System.Windows.Media.Imaging.BitmapCacheOption]::OnLoad) } finally { $s.Close() };"
        "$c=New-Object System.Windows.Media.Imaging.FormatConvertedBitmap($d.Frames[0],"
        " [System.Windows.Media.PixelFormats]::Bgr24, $null, 0);"
        "$e=New-Object System.Windows.Media.Imaging.JpegBitmapEncoder; $e.QualityLevel=90;"
        "$e.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($c));"
        "$o=[IO.File]::Create(%s); try{ $e.Save($o) } finally { $o.Close() }"
    ) % (q(src), q(dst))
    r = _powershell(script, timeout=60)
    return r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0


def media_cached(path, preview=False):
    """Copia local (cache) de una captura o video, para verla en el navegador. Con preview,
    una captura HDR (.jxr, que los navegadores no abren) se da convertida a JPG."""
    import hashlib
    path = fm_clean(path)
    if not path.startswith(MEDIA_ROOT + "/"):
        raise E.PipeError("Ruta no permitida")
    ext = posixpath.splitext(path)[1].lower()
    if ext not in MEDIA_TYPES:
        raise E.PipeError("Tipo no admitido")
    os.makedirs(MEDIA_DIR, exist_ok=True)
    fp = os.path.join(MEDIA_DIR, hashlib.sha1(path.encode("utf-8")).hexdigest()[:20] + ext)
    if not os.path.isfile(fp):
        with open(fp + ".part", "wb") as fo:
            E.Ftp(cfg_snapshot()).call(lambda f: f.retrbinary("RETR " + path, fo.write, 1024 * 1024))
        os.replace(fp + ".part", fp)
    if preview and ext == ".jxr":
        jpg = fp[:-4] + "_vista.jpg"
        if os.path.isfile(jpg) or _jxr_to_jpg(fp, jpg):
            return jpg, "image/jpeg"
    return fp, MEDIA_TYPES[ext]
FMT_LABEL = {"plain": "carpeta normal", "ffpkg": "imagen .ffpkg", "exfat": "imagen .exfat", "ffpfs": "imagen .ffpfs",
             "ffpfsc": "imagen comprimida .ffpfsc", "fpkg": "paquete .fpkg", "lz4": "carpeta LZ4",
             "lz4ffpkg": "imagen .ffpkg con LZ4", "zstd": "carpeta zstd", "zstdffpkg": "imagen .ffpkg con zstd"}
FPKG_MAX_FW = "11.60"


def lib_load():
    try:
        with open(LIB_FILE, encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("entries", {})
        d.setdefault("titles", {})
        return d
    except Exception:
        return {"entries": {}, "titles": {}}


def lib_save(d):
    try:
        with LIB_LOCK:
            _save_json(LIB_FILE, d)
    except Exception:
        pass


def remember_title(tid, title=None, need_fw=None, fw_param=None, cover_data_url=None, extra=None):
    """Guarda lo que se sabe de un juego por su Title ID (para ensenar imagenes con nombre y portada)."""
    if not tid:
        return
    import base64
    d = lib_load()
    t = d["titles"].get(tid.upper(), {})
    if title:
        t["title"] = title
    if need_fw:
        t["need_fw"] = need_fw
    if fw_param:
        t["fw_param"] = fw_param
    if extra:
        t.update(extra)
    if cover_data_url and cover_data_url.startswith("data:image/png;base64,"):
        try:
            os.makedirs(COVER_DIR, exist_ok=True)
            fn = "tid-%s.png" % re.sub(r"[^\w]", "", tid.upper())
            with open(os.path.join(COVER_DIR, fn), "wb") as fo:
                fo.write(base64.b64decode(cover_data_url.split(",", 1)[1]))
            t["cover"] = fn
        except Exception:
            pass
    d["titles"][tid.upper()] = t
    lib_save(d)


def forge_maybe_update(job, cfg, exe):
    """Una vez al dia, al convertir: si hay un PS5 Dump Forge mas nuevo en GitHub, lo baja y lo usa. Antes comprueba
    que entiende las opciones que usa PS5 Directo; si no, sigue con la version que ya funcionaba."""
    import shutil
    if (cfg.get("forge_path") or "").strip() or not cfg.get("forge_auto_update", False):
        return exe
    now = time.time()
    if now - float(SETTINGS.get("forge_checked") or 0) < 24 * 3600:
        return exe
    with LOCK:
        SETTINGS["forge_checked"] = now
    save_settings()
    try:
        tag, assets = F.github_release(F.FORGE_REPO)
    except Exception as e:
        jlog(job, "No pude mirar si hay PS5 Dump Forge nuevo (%s): sigo con el que hay" % e)
        return exe
    safe = re.sub(r"[^\w.-]", "_", tag or "")
    bad = set(SETTINGS.get("forge_bad") or [])
    have = os.path.basename(os.path.dirname(exe))
    if not safe or safe == have or safe in bad or os.path.isdir(os.path.join(DATA_DIR, "tools", "forge", safe)):
        return exe
    setj(job, phase="Actualizando PS5 Dump Forge a la %s" % tag)
    try:
        new, _ = F.download_forge(DATA_DIR, log=lambda m: jlog(job, m), release=(tag, assets))
    except Exception as e:
        jlog(job, "No pude bajar PS5 Dump Forge %s (%s): sigo con el que hay" % (tag, e))
        return exe
    ok = F.forge_cli_ok(new)
    if ok is False:
        with LOCK:
            SETTINGS["forge_bad"] = sorted(bad | {safe})
        save_settings()
        shutil.rmtree(os.path.dirname(new), ignore_errors=True)
        jlog(job, "PS5 Dump Forge %s ha cambiado sus opciones y PS5 Directo aún no las conoce: sigo con %s" % (tag, have))
        notify("warn", "PS5 Dump Forge %s no es compatible todavía" % tag,
               "Sigo usando la %s, que funciona. Saldrá una versión de PS5 Directo que la admita." % have,
               key="forgebad:" + safe)
        return exe
    jlog(job, "PS5 Dump Forge actualizado a la %s (antes %s)" % (tag, have))
    # se guardan las dos ultimas versiones; las mas viejas se borran
    root = os.path.join(DATA_DIR, "tools", "forge")
    olds = sorted((d for d in os.listdir(root) if d not in (safe, have)),
                  key=lambda d: os.path.getmtime(os.path.join(root, d)))
    for d in olds:
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    return new


FORGE_UPD = {"busy": False, "pct": 0, "text": "", "error": "", "done": ""}


def _vtuple(tag):
    return tuple(int(x) for x in re.findall(r"\d+", tag or "")[:4]) or (0,)


def forge_installed(cfg=None):
    """(version, ruta) del PS5 Dump Forge que se usa. La version sale de la carpeta (tools/forge/v0.5.1)."""
    cfg = cfg or cfg_snapshot()
    exe = F.find_forge(cfg.get("forge_path"), DATA_DIR, skip=tuple(SETTINGS.get("forge_bad") or ()))
    if not exe:
        return "", ""
    if (cfg.get("forge_path") or "").strip():
        return "propio", exe
    d = os.path.basename(os.path.dirname(exe))
    return (d if re.match(r"v?\d", d) else ""), exe


def forge_news(force=False):
    """Mira en GitHub si hay PS5 Dump Forge nuevo (como mucho cada 6 h) y guarda las novedades de cada version
    posterior a la que tienes. No baja nada: eso lo decides tu con «Actualizar»."""
    have, exe = forge_installed()
    with LOCK:
        nw = dict(SETTINGS.get("forge_news") or {})
    if not force and time.time() - float(nw.get("checked") or 0) < 6 * 3600:
        return nw
    try:
        base = F.GH_API + "/repos/%s/releases?per_page=15" % F.FORGE_REPO
        req = urllib.request.Request(base, headers=F._ua())
        with urllib.request.urlopen(req, timeout=20) as r:
            rels = [x for x in json.load(r) if not x.get("draft")]
    except Exception as e:
        nw["error"] = str(e)
        nw["checked"] = time.time()
        with LOCK:
            SETTINGS["forge_news"] = nw
        save_settings()
        return nw
    stable = [x for x in rels if not x.get("prerelease")] or rels
    latest = stable[0] if stable else None
    notes = []
    for x in stable:
        tag = (x.get("tag_name") or "").strip()
        if have and have != "propio" and _vtuple(tag) <= _vtuple(have):
            break
        notes.append({"tag": tag, "date": (x.get("published_at") or "")[:10], "url": x.get("html_url") or "",
                      "body": (x.get("body") or "").strip()[:12000]})
        if len(notes) >= 8:
            break
    nw = {"checked": time.time(), "error": "", "latest": (latest or {}).get("tag_name", ""),
          "latest_url": (latest or {}).get("html_url", ""), "notes": notes}
    with LOCK:
        SETTINGS["forge_news"] = nw
    save_settings()
    lt = nw["latest"]
    if have and have != "propio" and lt and _vtuple(lt) > _vtuple(have) and lt not in (SETTINGS.get("forge_bad") or []):
        notify("info", "PS5 Dump Forge %s ya está disponible (tienes la %s)" % (lt, have),
               "Ajustes → PS5 Dump Forge: «Ver novedades» y «Actualizar» cuando quieras.", key="forgenew:" + lt)
    return nw


def forge_info():
    have, exe = forge_installed()
    with LOCK:
        nw = dict(SETTINGS.get("forge_news") or {})
        bad = list(SETTINGS.get("forge_bad") or [])
        upd = dict(FORGE_UPD)
        auto = bool(SETTINGS.get("forge_auto_update"))
    lt = nw.get("latest") or ""
    has = bool(have and have != "propio" and lt and _vtuple(lt) > _vtuple(have) and lt not in bad)
    return {"ok": True, "installed": have, "path": exe, "latest": lt, "latest_url": nw.get("latest_url", ""),
            "notes": nw.get("notes") or [], "checked": nw.get("checked") or 0, "error": nw.get("error") or "",
            "update": has, "bad": lt in bad, "auto": auto, "custom": have == "propio", "upd": upd}


def forge_update_run():
    """Baja la ultima version de PS5 Dump Forge, comprueba que sigue entendiendo lo que usa PS5 Directo y pasa a
    usarla. Si no vale, se queda con la que habia."""
    import shutil
    have, old = forge_installed()
    try:
        with LOCK:
            FORGE_UPD.update(busy=True, pct=0, text="Mirando la última versión…", error="", done="")
        tag, assets = F.github_release(F.FORGE_REPO)

        a = F._forge_asset(assets) or {}
        total = int(a.get("size") or 0)

        def prog(done):
            with LOCK:
                FORGE_UPD.update(pct=min(99, int(done * 100 / total)) if total else 0,
                                 text="Descargando %s: %s de %s" % (tag, human(done), human(total) if total else "?"))
        with FORGE_LOCK:
            new, tag = F.download_forge(DATA_DIR, progress=prog, release=(tag, assets))
        with LOCK:
            FORGE_UPD.update(pct=100, text="Comprobando que funciona…")
        ok = F.forge_cli_ok(new)
        safe = os.path.basename(os.path.dirname(new))
        if ok is False:
            with LOCK:
                SETTINGS["forge_bad"] = sorted(set(SETTINGS.get("forge_bad") or []) | {safe})
            save_settings()
            shutil.rmtree(os.path.dirname(new), ignore_errors=True)
            raise E.PipeError("PS5 Dump Forge %s ha cambiado sus opciones y esta versión de PS5 Directo aún no las "
                              "conoce: sigues con la %s. Actualiza PS5 Directo cuando salga una versión nueva." % (tag, have or "que tenías"))
        # se guardan las dos ultimas versiones
        root = os.path.join(DATA_DIR, "tools", "forge")
        keep = {safe, os.path.basename(os.path.dirname(old)) if old else ""}
        for d in sorted(os.listdir(root)):
            if d not in keep:
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)
        # que la carpeta nueva sea la mas reciente (find_forge elige por fecha)
        try:
            os.utime(new, None)
        except OSError:
            pass
        with LOCK:
            FORGE_UPD.update(done=tag, text="")
            n = SETTINGS.get("forge_news") or {}
            n["notes"] = [x for x in (n.get("notes") or []) if _vtuple(x.get("tag")) > _vtuple(tag)]
            SETTINGS["forge_news"] = n
        save_settings()
        notify("ok", "PS5 Dump Forge actualizado a la %s" % tag, "Antes tenías la %s." % (have or "—"),
               key="forgeok:" + tag)
    except Exception as e:
        with LOCK:
            FORGE_UPD.update(error=str(e), text="")
    finally:
        with LOCK:
            FORGE_UPD["busy"] = False


def ensure_forge(job, cfg):
    exe = F.find_forge(cfg.get("forge_path"), DATA_DIR, skip=tuple(SETTINGS.get("forge_bad") or ()))
    if exe:
        return forge_maybe_update(job, cfg, exe)
    with FORGE_LOCK:
        exe = F.find_forge(cfg.get("forge_path"), DATA_DIR)
        if exe:
            return exe
        setj(job, phase="Descargando PS5 Dump Forge de GitHub (solo la primera vez)")
        try:
            exe, tag = F.download_forge(DATA_DIR, log=lambda m: jlog(job, m))
        except Exception as e:
            raise E.PipeError("No pude descargar PS5 Dump Forge (%s). Bájalo de "
                              "github.com/quer3q/ps5-dump-forge y pon la ruta de ps5-dump-forge.exe en "
                              "Ajustes, o elige 'Carpeta normal'." % e)
        jlog(job, "PS5 Dump Forge %s listo" % tag)
        return exe


# ---------------------------------------------------------------------------
# Torrents (magnet o .torrent): se bajan al PC con aria2 y luego siguen como un archivo del PC
# ---------------------------------------------------------------------------

ARIA2_URL = "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
ARIA2_LOCK = threading.Lock()
_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4}
_ARIA_RX = re.compile(r"\[#(\w+)\s+([\d.]+)(\w*B)/([\d.]+)(\w*B)(?:\((\d+)%\))?([^\]]*)\]")
_PLAY_RX = re.compile(r"(?i)\.(rar|r\d{2,3}|zip|z\d{2}|7z|\d{3}|tar|tgz|gz|bz2|xz|exfat|ffpkg|ffpfs|ffpfsc|ufs|pkg|"
                      r"sfv|md5|sha1)$")


def is_torrent(line):
    t = (line or "").strip().strip('"')
    return bool(re.match(r"(?i)^magnet:\?", t)) or t.lower().split("?")[0].endswith(".torrent")


def _bytes(num, unit):
    return int(float(num) * _UNITS.get(unit.upper(), 1))


def torrent_name(line):
    t = line.strip().strip('"')
    m = re.search(r"(?i)[?&]dn=([^&]+)", t)
    if m:
        return urllib.parse.unquote_plus(m.group(1))
    base = t.split("?")[0].replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"(?i)\.torrent$", "", urllib.parse.unquote(base)) or "torrent"


def ensure_aria2(job, cfg):
    import shutil
    custom = (cfg.get("aria2_path") or "").strip().strip('"')
    if custom and os.path.isfile(custom):
        return custom
    mine = os.path.join(DATA_DIR, "tools", "aria2", "aria2c.exe" if os.name == "nt" else "aria2c")
    if os.path.isfile(mine):
        return mine
    local = os.path.join(HERE, "aria2c.exe" if os.name == "nt" else "aria2c")
    if os.path.isfile(local):
        return local
    w = shutil.which("aria2c")
    if w:
        return w
    if os.name != "nt":
        if sys.platform == "darwin":
            raise E.PipeError("Para bajar torrents hace falta aria2. Instálalo con Homebrew (brew.sh): brew install aria2")
        raise E.PipeError("Para bajar torrents hace falta aria2 (instálalo, por ejemplo: sudo apt install aria2)")
    with ARIA2_LOCK:
        if os.path.isfile(mine):
            return mine
        setj(job, phase="Descargando aria2 para los torrents (solo la primera vez)")
        jlog(job, "Descargando aria2 (el programa que baja los torrents)…")
        os.makedirs(os.path.dirname(mine), exist_ok=True)
        tmp = mine + ".zip"
        try:
            F.http_get(ARIA2_URL, limit=50 * 1024 * 1024, timeout=120, dest=tmp)
            import zipfile
            with zipfile.ZipFile(tmp) as z:
                n = next((x for x in z.namelist() if x.lower().endswith("/aria2c.exe")), None)
                if not n:
                    raise E.PipeError("el .zip de aria2 no trae aria2c.exe")
                with z.open(n) as src, open(mine + ".tmp", "wb") as dst:
                    shutil.copyfileobj(src, dst)
            os.replace(mine + ".tmp", mine)
        except Exception as e:
            raise E.PipeError("No pude descargar aria2 para los torrents (%s). Bájalo de github.com/aria2/aria2 y "
                              "copia aria2c.exe en la carpeta de PS5 Directo." % e)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        jlog(job, "aria2 listo")
        return mine


def run_aria2(job, exe, line, root, cancel):
    """Baja un torrent a root. Devuelve cuando ha terminado (sin compartir despues)."""
    import subprocess
    import shutil
    t = line.strip().strip('"')
    args = [exe, "--dir=" + root, "--seed-time=0", "--bt-save-metadata=true", "--continue=true",
            "--file-allocation=none", "--summary-interval=1", "--show-console-readout=false",
            "--console-log-level=warn", "--enable-color=false", "--auto-save-interval=5",
            "--bt-detach-seed-only=true", "--auto-file-renaming=false", "--allow-overwrite=false",
            "--check-certificate=false" if os.name != "nt" else "--check-certificate=true"]
    if not re.match(r"(?i)^(magnet:|https?://)", t):
        if not os.path.isfile(t):
            raise E.PipeError("No encuentro el archivo .torrent: %s" % t)
        args += ["--torrent-file=" + t]
    else:
        args += [t]
    name = torrent_name(t)
    with LOCK:
        if not job.get("title"):
            job["title"] = name
        job["sources"] = [{"input": t[:200], "name": name, "size": -1, "kind": "url"}]
    setj(job, status="enviando", pc_step="bajar", phase="Torrent: buscando pares y datos del torrent…",
         speed_label="Descarga del torrent", sent_label="Bajado al PC", sent=0, total=-1, speed=0.0, dl_speed=0.0)
    jlog(job, "Bajando el torrent al PC con aria2 en %s" % root)
    os.makedirs(root, exist_ok=True)
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=E._NOWIN, cwd=root)
    tail, checked = [], {"space": False}

    def reader():
        for raw in proc.stdout:
            ln = raw.decode("utf-8", "replace").rstrip()
            if not ln:
                continue
            m = _ARIA_RX.search(ln)
            if not m:
                if ln.startswith(("FILE:", "***", "===", "---")) or "Download Progress Summary" in ln:
                    continue
                # aria2 avisa de cosas sin importancia (DHT, IPv6) como "ERROR": solo se muestran si al final falla
                tail.append(ln)
                del tail[:-15]
                continue
            done, tot = _bytes(m.group(2), m.group(3)), _bytes(m.group(4), m.group(5))
            rest = m.group(7) or ""
            dl = re.search(r"DL:([\d.]+)(\w*B)", rest)
            cn = re.search(r"CN:(\d+)", rest)
            sd = re.search(r"SD:(\d+)", rest)
            speed = _bytes(dl.group(1), dl.group(2)) if dl else 0
            if tot <= 0 or "[METADATA]" in ln:
                setj(job, phase="Torrent: consiguiendo los datos del torrent (%s conexiones)" % (cn.group(1) if cn else "0"))
                continue
            if not checked["space"]:
                checked["space"] = True
                free = shutil.disk_usage(root).free
                with LOCK:
                    job["sources"] = [{"input": t[:200], "name": name, "size": tot, "kind": "url"}]
                if tot + GiB > free:
                    job["_stop"] = ("No cabe en el PC: el torrent ocupa %s y en %s hay %s libres. Libera espacio o "
                                    "cambia la carpeta temporal en Ajustes." % (human(tot), root, human(free)))
                    cancel.set()
            peers = ("%s conexiones%s" % (cn.group(1), (", %s compartiendo" % sd.group(1)) if sd else "")) if cn else ""
            setj(job, sent=done, total=tot, speed=float(speed), dl_speed=float(speed),
                 phase="Bajando el torrent al PC: %d%%%s" % (int(done * 100 / tot), (" · " + peers) if peers else ""))
    th = threading.Thread(target=reader, daemon=True)
    th.start()
    while proc.poll() is None:
        if cancel.is_set():
            try:
                proc.terminate()
                proc.wait(10)
            except Exception:
                proc.kill()
            raise E.Cancelled()
        time.sleep(0.5)
    th.join(5)
    if proc.returncode != 0:
        for x in tail[-8:]:
            jlog(job, "aria2: " + x[:200])
        msg = " ".join(x for x in tail[-4:] if x)[-300:] or ("código %s" % proc.returncode)
        raise E.PipeError("No se pudo bajar el torrent: %s" % msg)
    setj(job, sent=max(job.get("total") or 0, job.get("sent") or 0), speed=0.0, dl_speed=0.0)
    jlog(job, "Torrent bajado al PC")


def torrent_outputs(root):
    """Lo que se ha bajado, convertido en lineas para la cola: aplicaciones (carpetas con sce_sys), comprimidos e
    imagenes; si no hay nada de eso, la carpeta tal cual."""
    tops = [os.path.join(root, n) for n in sorted(os.listdir(root))
            if not n.lower().endswith((".aria2", ".torrent")) and not n.startswith(".")]
    apps, files = [], []
    for top in tops:
        if os.path.isfile(top):
            files.append(top)
            continue
        for d, dirs, fs in os.walk(top):
            if "sce_sys" in dirs:
                apps.append(d)
                dirs[:] = []
                continue
            dirs.sort()
            for f in sorted(fs):
                if not f.lower().endswith(".aria2"):
                    files.append(os.path.join(d, f))
    play = [f for f in files if _PLAY_RX.search(f)]
    out = apps + play
    if not out:
        out = tops
    return out


def torrent_stage(job, cfg, cancel):
    lines = list(job["lines"])
    tor = [l for l in lines if is_torrent(l)]
    rest = [l for l in lines if not is_torrent(l)]
    with LOCK:
        job.setdefault("orig_lines", lines)
        job["torrent"] = True
    exe = ensure_aria2(job, cfg)
    root = os.path.join(pc_temp_root(cfg), "torrent", job["id"])
    for t in tor:
        run_aria2(job, exe, t, root, cancel)
    local = torrent_outputs(root)
    if not local:
        raise E.PipeError("El torrent no ha dejado nada en %s" % root)
    jlog(job, "Del torrent sale: %s" % ", ".join(os.path.relpath(x, root) for x in local[:8]) +
         (" y %d más" % (len(local) - 8) if len(local) > 8 else ""))
    with LOCK:
        job["lines"] = local + rest
        job["torrent_dir"] = root
    setj(job, status="comprobando", pc_step="enviar", speed_label="", sent_label="", sent=0, total=-1)
    save_jobs()


def torrent_forget(job):
    """Al quitar un trabajo de torrent de la cola, lo bajado al PC ya no sirve: se borra."""
    if not job.get("torrent"):
        return
    import shutil
    d = job.get("torrent_dir") or os.path.join(pc_temp_root(cfg_snapshot()), "torrent", job["id"])
    threading.Thread(target=shutil.rmtree, args=(d, True), daemon=True).start()


def torrent_cleanup(job):
    d = job.get("torrent_dir")
    if d and os.path.isdir(d) and cfg_snapshot().get("delete_archives", True):
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        jlog(job, "Borrado del PC lo bajado del torrent")


def check_fw_warning(job, tid, title, need_fw, fw_param=None, backport=False):
    """Aviso si el juego pide un firmware mas nuevo que el de tu PS5 (Ajustes)."""
    with LOCK:
        mine = SETTINGS.get("console_fw") or ""
    if not need_fw:
        return
    ok = F.fw_ok(mine, need_fw)
    name = title or tid or "Esta aplicación"
    extra = " (con backport)" if backport else ""
    if ok is None:
        jlog(job, "%s pide firmware %s%s" % (name, need_fw, extra))
        return
    if ok:
        jlog(job, "Firmware: %s pide %s%s y tu PS5 tiene %s: vale" % (name, need_fw, extra, mine))
        return
    msg = ("Ojo: %s pide firmware %s%s y tu PS5 tiene %s: lo más probable es que no arranque. Busca una "
           "versión con backport para tu firmware." % (name, need_fw, extra, mine))
    jlog(job, msg)
    with LOCK:
        job["warning"] = msg
    notify("warn", "No arrancará en tu firmware: %s" % name, msg, {"tab": "Transfers", "job": job["id"]},
           key="fw:%s:%s" % (job["id"], tid))


def _dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for n in files:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


def send_local_as_is(job, cfg, path, cancel, up):
    """Manda un archivo del PC tal cual a la carpeta de destino (por ejemplo, una imagen ya hecha)."""
    dest = (job.get("dest") or cfg["dest"]).rstrip("/")
    name = os.path.basename(path)
    target = dest + "/" + name
    fs = E.FileSource(path).probe()
    jlog(job, "Lo envío tal cual: %s" % name)
    claim_target(job, target)
    setj(job, status="enviando", pc_step="enviar", total=fs.size, sent=0, phase="Enviando %s" % name)
    last = {"v": 0}

    def progress(v, *_):
        if v > last["v"]:
            up(v - last["v"])
            last["v"] = v
        setj(job, sent=v, speed=up.rate())
    E.upload(cfg, target, fs.size, lambda off: fs.stream(off, cfg, cancel, lambda n: None), progress, cancel,
             lambda m: jlog(job, m))
    setj(job, sent=fs.size)
    with LOCK:
        job["_placed_game"] = name.lower().endswith(E.IMAGE_EXTS)
    return {"mode": "done", "placed": [target], "game": job["_placed_game"]}


def forge_split_archive(job, cfg, cancel, archive, cands):
    """El comprimido trae varios juegos (lo normal: la version normal y un backport) y PS5 Dump Forge
    solo convierte uno. Descomprime en el PC solo el que toca y devuelve su carpeta (None si no se puede)."""
    import subprocess
    import shutil
    exe7 = E.find_7z(cfg.get("sevenzip_path") or "")
    if not exe7:
        jlog(job, "Trae varias aplicaciones y para separarlas hace falta 7-Zip, que no está instalado")
        return None
    entries, pw = None, None
    for c in cands:
        try:
            entries, _ = E.list_7z(exe7, archive, c)
            pw = c
            break
        except E.PipeError as e:
            if not E._pw_error(str(e)):
                jlog(job, str(e))
                return None
    if entries is None:
        return None
    roots = sorted({e["path"][:-len("/sce_sys/param.json")] for e in entries
                    if e["path"].lower().endswith("/sce_sys/param.json")})
    if not roots:
        return None
    normal = [r for r in roots if not _backport_label(r)]
    root = normal[0] if normal else roots[0]
    others = [r for r in roots if r != root]
    total = sum(e["size"] for e in entries if e["path"] == root or e["path"].startswith(root + "/"))
    jlog(job, "Trae %d aplicaciones (%s): PS5 Dump Forge solo convierte una, así que descomprimo en el PC solo "
              "%s y convierto eso" % (len(roots), ", ".join(roots), root))
    if others:
        jlog(job, "Lo que no se usa: %s" % ", ".join(others))
    work = os.path.join(pc_temp_root(cfg), "forge", job["id"], "origen")
    folder = os.path.join(work, *root.split("/"))
    if os.path.isfile(os.path.join(work, ".hecho")) and os.path.isfile(os.path.join(folder, "sce_sys", "param.json")):
        jlog(job, "Ya estaba descomprimido en el PC: %s" % root)
        return folder
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    free_pc = shutil.disk_usage(work).free
    if total * 2 + GiB > free_pc:
        raise E.PipeError("No cabe en el PC: para separarlo y convertirlo hacen falta unos %s y en %s hay %s "
                          "libres. Libera espacio o elige «Sin convertir»." % (human(total * 2 + GiB), work,
                                                                              human(free_pc)))
    setj(job, status="convirtiendo", pc_step="convertir", phase="Descomprimiendo %s en el PC para convertirlo" % root,
         conv_pct=0, conv_text="")
    proc = F.child_started(subprocess.Popen(E._7z_cmd(exe7, "x", archive, "-o" + work, root, password=pw),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=E._NOWIN))
    while proc.poll() is None:
        if cancel.is_set():
            proc.kill()
            raise E.Cancelled()
        done = _dir_size(work) if os.path.isdir(work) else 0
        if total:
            pct = min(99, int(done * 100 / total))
            setj(job, conv_pct=pct, conv_text="Descomprimiendo · %s de %s" % (human(done), human(total)),
                 phase="Descomprimiendo %s en el PC para convertirlo: %d%%" % (root, pct))
        time.sleep(0.5)
    if proc.returncode != 0:
        err = proc.stderr.read().decode("utf-8", "replace").strip()
        jlog(job, "7-Zip dice: %s" % err[-400:])
        raise E.PipeError(E.explain_7z(err)[1])
    if not os.path.isfile(os.path.join(folder, "sce_sys", "param.json")):
        jlog(job, "No encuentro %s después de descomprimir" % root)
        return None
    open(os.path.join(work, ".hecho"), "w").close()
    return folder


def forge_job(job, srcs, cfg, cancel, src_path, created, pc_folder, up):
    """Convierte con PS5 Dump Forge en el PC y envia el resultado. None = mejor como carpeta normal."""
    import shutil
    fmt = job_fmt(job)
    exe = ensure_forge(job, cfg)
    setj(job, status="convirtiendo", pc_step="convertir", phase="Analizando con PS5 Dump Forge",
         conv_pct=0, conv_text="", speed_label="", sent_label="")
    info, pw_ok = None, None
    cands = [None] + password_candidates(job, cfg)
    last = None
    for pw in cands:
        try:
            info = F.forge_inspect(exe, src_path, pw, cancel)
            pw_ok = pw
            break
        except E.PipeError as e:
            last = e
            low = str(e).lower()
            if not any(w in low for w in ("password", "encrypt", "wrong", "contrase", "crc")):
                break
    if info is None and last and "more than one ps5 game" in str(last).lower() and os.path.isfile(src_path):
        sub = forge_split_archive(job, cfg, cancel, src_path, cands)
        if sub:
            return forge_job(job, srcs, cfg, cancel, sub, created, pc_folder, up)
    if info is None:
        jlog(job, "PS5 Dump Forge no puede leerlo: %s" % last)
        if not any(w in str(last).lower() for w in ("password", "encrypt")):
            with LOCK:
                job["warning"] = ("No se ha podido convertir (PS5 Dump Forge: %s). Se ha enviado sin convertir."
                                  % str(last)[:160])
        if last and any(w in str(last).lower() for w in ("password", "encrypt")):
            raise E.PipeError("Ninguna contraseña abre el comprimido. Escríbela en el trabajo o en Ajustes y "
                              "pulsa Reintentar (no se vuelve a descargar).")
        return None
    tid = info.get("title_id") or E.title_id(srcs[0].name)
    title = info.get("title_name")
    need_fw = info.get("backport_firmware") or info.get("firmware")
    remember_title(tid, title, need_fw, info.get("firmware"), info.get("cover"),
                   {"backport": bool(info.get("backport"))})
    if title:
        setj(job, title="%s (%s)" % (title, tid) if tid else title)
    for line in (info.get("findings") or [])[:8]:
        jlog(job, "Forge avisa: %s" % line)
    if info.get("additional_content") and fmt != "plain":
        jlog(job, "Es un DLC o una actualización: no se puede convertir a imagen")
        return None
    lz4 = info.get("lz4") or {}
    extra = []
    zst_src = bool(info.get("zstd_packed"))
    if fmt == "plain" and zst_src:
        extra.append("--zstd-unpack")
        jlog(job, "Viene empaquetado en zstd: lo desempaqueto a archivos normales")
    elif fmt == "plain" and lz4.get("packed"):
        extra.append("--lz4-unpack")
        jlog(job, "Viene empaquetado en LZ4: lo desempaqueto a archivos normales")
    if zst_src and fmt in ("lz4", "lz4ffpkg"):
        raise E.PipeError("Viene empaquetado en zstd y PS5 Dump Forge no lo pasa directo a LZ4. Elige «zstd», "
                          "«Carpeta» o una imagen, o pásalo antes a «Carpeta».")
    if fmt in ("zstd", "zstdffpkg") and not F.forge_has(exe, "--zstd-pack"):
        jlog(job, "Para zstd hace falta PS5 Dump Forge 0.5 o posterior: lo actualizo")
        setj(job, phase="Actualizando PS5 Dump Forge (hace falta para zstd)")
        forge_update_run()
        exe = F.find_forge(cfg.get("forge_path"), DATA_DIR, skip=tuple(SETTINGS.get("forge_bad") or ()))
        if not exe or not F.forge_has(exe, "--zstd-pack"):
            raise E.PipeError("Para zstd hace falta PS5 Dump Forge 0.5 o posterior y no he podido actualizarlo (%s). "
                              "Mira Ajustes → PS5 Dump Forge." % (FORGE_UPD.get("error") or "sin conexión"))
    if fmt == "fpkg":
        mine = (SETTINGS.get("console_fw") or "").strip()
        if mine and F.fw_ok(FPKG_MAX_FW, mine) is False:
            raise E.PipeError("Los .fpkg solo funcionan con firmware %s o inferior y tu PS5 tiene %s. Elige "
                              "«Imagen .ffpkg» (o «Sin convertir») y pulsa Reintentar." % (FPKG_MAX_FW, mine))
        jlog(job, "Ojo: los .fpkg solo se instalan en firmware %s o inferior (con kstuff, fpkg-enable y "
                  "ppr-patch)" % FPKG_MAX_FW)
    if fmt in ("lz4", "lz4ffpkg", "zstd", "zstdffpkg") and not lz4.get("imports_ampr"):
        kind = "zstd" if fmt.startswith("zstd") else "LZ4"
        jlog(job, "Este título no usa AMPR, así que %s no sirve con él" % kind)
        remember_title(tid, extra={"no_ampr": True})
        if fmt in ("lz4", "zstd"):
            img = len(srcs) == 1 and IMAGE_RE.search(srcs[0].name or "")
            msg = ("PS5 Dump Forge dice que este título no usa AMPR, así que no se puede pasar a %s: " % kind
                   + ("se envía sin convertir, tal cual (%s)." % srcs[0].name if img else
                      "se envía sin convertir, como carpeta normal."))
            with LOCK:
                job["warning"] = msg
            setj(job, fmt_real="none")
            jlog(job, msg)
            return None
        fmt = "ffpkg"
        with LOCK:
            job["warning"] = ("PS5 Dump Forge dice que este título no usa AMPR: se envía como imagen .ffpkg "
                              "normal, sin %s." % kind)
        jlog(job, "Lo convierto a imagen .ffpkg normal")
    setj(job, fmt_real=fmt)
    comp = job.get("comp") or ("fast" if fmt in ("fpkg", "lz4", "lz4ffpkg", "zstd", "zstdffpkg") else "balanced")
    if fmt in F.FORGE_COMP and comp in F.FORGE_COMP[fmt]:
        extra += F.FORGE_COMP[fmt][comp]
        jlog(job, "Compresión: %s" % {"fast": "rápida", "balanced": "equilibrada", "smallest": "mínimo tamaño",
                                       "stored": "sin compresión"}[comp])
    if fmt in ("lz4", "lz4ffpkg"):
        jlog(job, "LZ4 es experimental: con algunos títulos no arranca o se cuelga al guardar partida")
    if fmt in ("zstd", "zstdffpkg"):
        jlog(job, "zstd es nuevo: comprueba que arranca antes de borrar el original. Si ShadowMountPlus tiene activada "
                  "la actualización de fakelib, desactívala para este título")
    check_fw_warning(job, tid, title, need_fw, info.get("firmware"), bool(info.get("backport")))
    bf = E.BFpilot(cfg)
    dest = "/data/pkg" if fmt == "fpkg" else (job.get("dest") or cfg["dest"]).rstrip("/")
    ext = F.FORGE_FORMATS[fmt][1]
    base = tid or E.safe_name(E.display_name(srcs[0].name))[:50]
    name = (base + ext) if ext else (base + "-app")
    target = dest + "/" + name
    keep = job.get("where") == "keep"
    if not keep:
        check_duplicates_first(job, bf, tid, exclude=target)
    total = int(info.get("total_bytes") or 0)
    work = os.path.join(pc_temp_root(cfg), "forge", job["id"])
    os.makedirs(work, exist_ok=True)
    out = os.path.join(work, name)
    marker = os.path.join(work, ".hecho-" + fmt)
    if not (os.path.exists(out) and os.path.isfile(marker)):
        free_pc = shutil.disk_usage(work).free
        need_pc = int(total * 1.05) + GiB
        if total and need_pc > free_pc:
            raise E.PipeError("No cabe en el PC: la %s ocupará hasta %s y en %s hay %s libres. Libera espacio, "
                              "cambia la carpeta temporal en Ajustes o elige 'Carpeta normal'."
                              % (FMT_LABEL[fmt], human(need_pc), work, human(free_pc)))
        free = free_space(bf) if (not keep and bf.alive()) else None
        est = int(total * (0.5 if fmt in ("ffpfsc", "lz4", "lz4ffpkg", "zstd", "zstdffpkg") else 0.9 if fmt == "fpkg" else 1.02))
        if free is not None and total and est + margin_bytes() > free:
            raise no_space(job, est, free, "Lo más probable es que no quepa en la PS5: la %s ocupará unos %s, hay "
                           "%s libres y dejo %s de margen." % (FMT_LABEL[fmt], human(est), human(free),
                                                               human(margin_bytes())))
        for p in (out, out + ".part"):
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        jlog(job, "Convirtiendo a %s con PS5 Dump Forge (%s sin convertir)" % (FMT_LABEL[fmt], human(total)))
        t0 = {"t": 0.0}

        def prog(stage, done, tot):
            now = time.time()
            if now - t0["t"] < 0.4:
                return
            t0["t"] = now
            pct = int(done * 100 / tot) if tot else 0
            label = F.FORGE_STAGES.get(stage, stage)
            setj(job, conv_pct=pct, conv_text="%s · %s de %s" % (label, human(done), human(tot) if tot else "?"),
                 phase="Convirtiendo a %s: %s %d%%" % (FMT_LABEL[fmt], label, pct))
        F.forge_convert(exe, src_path, out, fmt, pw_ok, cancel, prog, lambda m: jlog(job, "Forge: " + m), extra)
        with open(marker, "w") as fh:
            fh.write("ok")
    size = _dir_size(out) if os.path.isdir(out) else os.path.getsize(out)
    jlog(job, "Listo en el PC: %s (%s)" % (name, human(size)))
    if total and size:
        jlog(job, "Ocupa %s frente a %s sin convertir" % (human(size), human(total)))
    if keep:
        dst = _unique_path(os.path.join(keep_dir(job), name))
        shutil.move(out, dst)
        shutil.rmtree(work, ignore_errors=True)
        setj(job, conv_pct=100)
        return {"mode": "keep", "path": dst}
    free = free_space(bf) if bf.alive() else None
    if free is not None and size + margin_bytes() > free:
        raise no_space(job, size, free, "No cabe en la PS5: la %s ocupa %s, hay %s libres y dejo %s de margen. "
                       "Libera espacio y pulsa Reintentar (no se vuelve a convertir)."
                       % (FMT_LABEL[fmt], human(size), human(free), human(margin_bytes())))
    setj(job, status="enviando", pc_step="enviar", total=size, sent=0, conv_pct=100, conv_text="",
         phase="Enviando %s" % name)
    last = {"v": 0}

    def progress(v, *_):
        if v > last["v"]:
            up(v - last["v"])
            last["v"] = v
        setj(job, sent=v, speed=up.rate())
    if target.rstrip("/") not in set(job.get("own_paths") or []) and bf.alive() and bf.exists(target):
        # en el destino ya hay algo con ese nombre que no es de este trabajo: no se mezcla con lo convertido
        if os.path.isdir(out) and not bf.exists(target + "/sce_sys/param.json"):
            jlog(job, "En %s había un envío anterior a medias: lo sustituyo" % target)
            bf.delete(target, cancel)
        else:
            target = resolve_existing(job, target, bf.exists, lambda t: bf.delete(t, cancel), tid)
    claim_target(job, target)
    watch = start_space_watch(job, bf, cancel, "el envio")
    try:
        if os.path.isdir(out):
            src = E.FolderSource(out).probe()
            sent, skipped, resumed = E.upload_tree(cfg, src, target, cancel, lambda m: jlog(job, m),
                                                   lambda v, rel: progress(v))
            jlog(job, "Carpeta enviada: %d archivos nuevos, %d ya estaban, %d continuados" % (sent, skipped, resumed))
            if bf.alive():
                fix_game_perms(job, bf, target, cancel)
        else:
            fs = E.FileSource(out).probe()
            # se escribe como .ps5part y se renombra al final: ShadowMountPlus no ve una imagen a medias
            E.upload(cfg, target, fs.size, lambda off: fs.stream(off, cfg, cancel, lambda n: None),
                     progress, cancel, lambda m: jlog(job, m))
    finally:
        watch.set()
    setj(job, sent=size)
    shutil.rmtree(work, ignore_errors=True)
    if created and cfg.get("delete_archives", True):
        for pth in created:
            try:
                os.remove(pth)
            except OSError:
                pass
        try:
            os.rmdir(pc_folder)
        except OSError:
            pass
        jlog(job, "Borradas del PC las partes descargadas")
    with LOCK:
        job["_placed_game"] = True
    return {"mode": "done", "placed": [target], "game": True}


def _unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = (path, "") if os.path.isdir(path) else os.path.splitext(path)
    k = 2
    while os.path.exists("%s (%d)%s" % (base, k, ext)):
        k += 1
    return "%s (%d)%s" % (base, k, ext)


def keep_dir(job):
    d = (job.get("pcdir") or "").strip().strip('"')
    return d if d and os.path.isabs(d) else pc_library_dir()


def keep_on_pc_job(job, srcs, cancel):
    """Modo «Solo en el PC»: baja todo, une las partes, descomprime y lo deja en una carpeta del PC.
    No toca la consola. Devuelve la ruta final."""
    import shutil
    import subprocess
    cfg = cfg_snapshot()
    cfg["_limit_bps"] = live_limit_bps
    mine = bool(job.get("torrent"))     # lo bajado por torrent si se puede mover
    local = srcs[0].kind == "folder" or (all(s.kind == "file" for s in srcs) and not mine)
    fmt0 = job.get("fmt") if job.get("fmt") in F.FORGE_FORMATS else ""
    if local and (job.get("keep_steps") or "extract") == "convert" and fmt0 and fmt0 != "plain":
        # convertir lo que ya tienes en el PC (un .pkg, una carpeta, una imagen) a otro formato, sin mandar nada:
        # el resultado queda en tu carpeta de destino y el original no se toca
        first = srcs[0].path if len(srcs) == 1 else E.first_volume([x.path for x in srcs])
        work = os.path.join(pc_temp_root(cfg), "local-" + job["id"])
        os.makedirs(work, exist_ok=True)
        try:
            res = forge_job(job, srcs, cfg, cancel, first, [], work, Meter())
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if isinstance(res, dict) and res.get("mode") == "keep":
            return res["path"]
        raise E.PipeError("PS5 Dump Forge no ha podido convertirlo a %s." % FMT_LABEL.get(fmt0, fmt0))
    if srcs[0].kind == "folder" or (all(s.kind == "file" for s in srcs) and not mine
                                    and not E.is_archive(E.first_volume([x.name for x in srcs]))):
        raise E.PipeError("Eso ya está en tu PC. Para convertirlo a otro formato sin mandarlo, en «Solo a mi PC» "
                          "elige «Convertir» y el formato (LZ4, zstd, .ffpkg…).")
    dest_root = keep_dir(job)
    os.makedirs(dest_root, exist_ok=True)
    names = [s.name for s in srcs]
    folder = os.path.join(pc_temp_root(cfg), E.safe_name(E.set_key(srcs[0].name)))
    dl = Meter()
    stop_speed = threading.Event()

    def speed_loop():
        while not stop_speed.wait(1):
            if job.get("pc_step") == "bajar":
                setj(job, speed=dl.rate(), dl_speed=0.0)
    threading.Thread(target=speed_loop, daemon=True).start()
    try:
        setj(job, status="enviando", total=sum(max(0, s.size) for s in srcs), sent=0, pc_step="bajar",
             speed_label="Descarga al PC", sent_label="Bajado al PC")
        local, created = fetch_to_pc(job, srcs, cfg, cancel, dl, folder)
        stop_speed.set()
        setj(job, speed=0.0)
        first = local[E.first_volume(names)]
        steps = job.get("keep_steps") or "extract"
        fmt = job.get("fmt") if job.get("fmt") in F.FORGE_FORMATS else ""
        if steps == "convert" and fmt and fmt != "plain":
            res = forge_job(job, srcs, cfg, cancel, first, created, folder, Meter())
            if isinstance(res, dict) and res.get("mode") == "keep":
                for c in created:
                    try:
                        os.remove(c)
                    except OSError:
                        pass
                shutil.rmtree(folder, ignore_errors=True)
                return res["path"]
            jlog(job, "No se ha convertido: lo dejo descomprimido, sin convertir")
        raw_split = (not E.is_archive(first) and len(names) > 1
                     and all(E.SPLIT_RAW_RE.match(n) for n in names))
        if raw_split and steps != "dl":
            # una imagen partida en .001, .002...: se une en un solo archivo
            base = E.SPLIT_RAW_RE.match(names[0]).group("base")
            dst = _unique_path(os.path.join(dest_root, base))
            setj(job, status="convirtiendo", pc_step="descomprimir", conv_pct=0, phase="Uniendo las partes")
            tot, done = sum(os.path.getsize(local[n]) for n in names), 0
            with open(dst + ".part", "wb") as fo:
                for n in sorted(names, key=E.volume_order):
                    with open(local[n], "rb") as fi:
                        while True:
                            if cancel.is_set():
                                raise E.Cancelled()
                            b = fi.read(8 * 1024 * 1024)
                            if not b:
                                break
                            fo.write(b)
                            done += len(b)
                            setj(job, conv_pct=int(done * 100 / max(1, tot)))
            os.replace(dst + ".part", dst)
            for c in created:
                try:
                    os.remove(c)
                except OSError:
                    pass
            shutil.rmtree(folder, ignore_errors=True)
            return dst
        if job.get("raw") or steps == "dl" or not E.is_archive(first):
            # solo bajar, o una imagen, un .pkg o un archivo suelto: se deja tal cual
            outs = []
            if len(names) > 1:          # varias partes: juntas en su carpeta
                dest_root = _unique_path(os.path.join(dest_root, E.safe_name(E.display_name(names[0])) or "descarga"))
                os.makedirs(dest_root, exist_ok=True)
            for n in names:
                dst = _unique_path(os.path.join(dest_root, n))
                if local[n] in created or mine:
                    shutil.move(local[n], dst)
                else:
                    shutil.copy2(local[n], dst)
                outs.append(dst)
            shutil.rmtree(folder, ignore_errors=True)
            return outs[0] if len(outs) == 1 else dest_root
        # descomprimir con 7-Zip (o tarfile para los .tar)
        name = E.safe_name(E.display_name(srcs[0].name)) or "descomprimido"
        work = os.path.join(dest_root, "." + name + ".descomprimiendo")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
        setj(job, status="convirtiendo", pc_step="descomprimir", conv_pct=0, conv_text="",
             phase="Descomprimiendo en el PC", sent_label="Bajado al PC")
        if E.is_tar(first):
            import tarfile
            with tarfile.open(first) as t:
                try:
                    t.extractall(work, filter="data")
                except TypeError:
                    t.extractall(work)
        else:
            exe = E.find_7z(cfg.get("sevenzip_path"))
            if not exe:
                raise E.PipeError("Para descomprimir en el PC hace falta 7-Zip (gratis, 7-zip.org). Instálalo o "
                                  "pon la ruta de 7z.exe en Ajustes.")
            cands = password_candidates(job, cfg)
            pw, entries = None, None
            try:
                entries, _ = E.list_7z(exe, first)
                if E.needs_password(entries):
                    pw = "?"
            except E.PipeError as e:
                if not E._pw_error(str(e)):
                    raise
                pw = "?"
            if pw == "?":
                pw = None
                for c in cands:
                    try:
                        entries, _ = E.list_7z(exe, first, c)
                        pw = c
                        break
                    except E.PipeError:
                        continue
                if pw is None:
                    raise E.PipeError("El comprimido tiene contraseña y ninguna lo abre. Escríbela en el trabajo o "
                                      "en Ajustes y pulsa Reintentar (no se vuelve a descargar).")
            total = sum(e["size"] for e in entries or [] if not e["dir"])
            free = shutil.disk_usage(dest_root).free
            if total and total + GiB > free:
                raise E.PipeError("No cabe en el PC: descomprimido ocupa %s y en %s hay %s libres. Libera espacio "
                                  "o elige otra carpeta." % (human(total), dest_root, human(free)))
            only = [x for x in (job.get("only") or []) if x]
            extra = []
            if only:
                import tempfile
                lst = os.path.join(tempfile.gettempdir(), "ps5directo-solo-%s.txt" % job["id"])
                with open(lst, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(only))
                extra = ["-i@" + lst, "-scsUTF-8"]
                sel = set(only)
                total = sum(e["size"] for e in entries or [] if not e["dir"] and
                            any(e["path"] == x or e["path"].startswith(x.rstrip("/") + "/") for x in sel))
                jlog(job, "Saco solo lo elegido: %d elemento(s)" % len(only))
            jlog(job, "Descomprimiendo %s en %s" % (human(total), dest_root))
            proc = F.child_started(subprocess.Popen(E._7z_cmd(exe, "x", first, "-o" + work, *extra, password=pw),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    creationflags=E._NOWIN))
            while proc.poll() is None:
                if cancel.is_set():
                    proc.kill()
                    raise E.Cancelled()
                done = _dir_size(work)
                if total:
                    pct = min(99, int(done * 100 / total))
                    setj(job, conv_pct=pct, conv_text="%s de %s" % (human(done), human(total)),
                         phase="Descomprimiendo en el PC: %d%%" % pct)
                time.sleep(0.5)
            if proc.returncode != 0:
                err = proc.stderr.read().decode("utf-8", "replace").strip()
                key, msg = E.explain_7z(err)
                jlog(job, "7-Zip dice: %s" % err[-400:])
                if not pc_extract_fallback(job, key, first, work, pw, cfg, cancel):
                    raise E.PipeError(msg)
        # si dentro hay una sola carpeta, esa es la que se queda (sin carpetas dobles)
        inside = [x for x in os.listdir(work) if not x.startswith(".")]
        if len(inside) == 1 and os.path.isdir(os.path.join(work, inside[0])):
            out = _unique_path(os.path.join(dest_root, inside[0]))
            shutil.move(os.path.join(work, inside[0]), out)
            shutil.rmtree(work, ignore_errors=True)
        else:
            out = _unique_path(os.path.join(dest_root, name))
            os.replace(work, out)
        setj(job, conv_pct=100)
        # las partes bajadas ya no hacen falta
        for c in created:
            try:
                os.remove(c)
            except OSError:
                pass
        shutil.rmtree(folder, ignore_errors=True)
        jlog(job, "Borradas del PC las partes descargadas")
        return out
    except BaseException:
        w = locals().get("work")
        if w and os.path.isdir(w):
            shutil.rmtree(w, ignore_errors=True)     # lo descomprimido a medias no sirve
        raise
    finally:
        stop_speed.set()


# ---------------------------------------------------------------------------
# Revisar enlaces (como el capturador de JDownloader): saca los enlaces de cualquier texto,
# los comprueba y los agrupa en paquetes (todas las partes de lo mismo juntas).
# ---------------------------------------------------------------------------

GRAB = {"busy": False, "done": 0, "total": 0, "packages": [], "error": "", "id": 0}
GRAB_URL_RX = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"'\]\[(){}|]+|magnet:\?[^\s<>\"']+")


PW_NEAR_RX = re.compile(r"(?i)(?:contrase(?:ñ|n)a|password|passwort|senha|pass|clave|pwd|pw)\s*[:=]\s*[\"'«]?([^\s\"'«»<>]{1,80})")


def password_in_text(text):
    """La contraseña si el texto la dice («Contraseña: abc», «Password = xyz»...), o ''."""
    m = PW_NEAR_RX.search(text or "")
    return m.group(1).strip(".,;)") if m else ""


def add_lines(text):
    """Lineas para la cola: un enlace o una ruta del PC por linea tal cual; de una linea con texto y enlaces
    dentro (un mensaje copiado), solo los enlaces."""
    out = []
    for ln in str(text or "").splitlines():
        t = ln.strip()
        if not t:
            continue
        if re.match(r"(?i)^((https?|ftp)://|magnet:\?)\S+$", t):
            out.append(t)          # ya es un enlace solo (aunque lleve parentesis u otros signos)
            continue
        urls = grab_links(t)
        if not urls or (len(urls) == 1 and urls[0] == t):
            out.append(t)
            continue
        for u in urls:
            if u not in out:
                out.append(u)
    return out


def grab_links(text):
    out = []
    for m in GRAB_URL_RX.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?»”’")
        if u not in out:
            out.append(u)
    return out


def missing_parts(names):
    """Numeros de parte que faltan en un conjunto (part1, part2... / .001, .002... / .rar, .r00...)."""
    nums, base = [], 1
    for n in names:
        m = re.search(r"\.part(\d+)\.rar$", n, re.I) or re.search(r"\.(\d{3})$", n)
        if m:
            nums.append(int(m.group(1)))
            continue
        m = re.search(r"\.r(\d{2,3})$", n, re.I)
        if m:
            nums.append(int(m.group(1)) + 2)        # .rar = 1, .r00 = 2...
            base = 1
        elif re.search(r"\.rar$", n, re.I):
            nums.append(1)
    if len(nums) < 1:
        return []
    return sorted(set(range(base, max(nums) + 1)) - set(nums))


def grab_scan(text):
    from concurrent.futures import ThreadPoolExecutor
    links = grab_links(text)
    with LOCK:
        GRAB.update(busy=True, done=0, total=len(links), packages=[], error="", id=GRAB["id"] + 1)
        gid = GRAB["id"]
    if not links:
        with LOCK:
            GRAB.update(busy=False, error="No encuentro enlaces en ese texto (tienen que empezar por http://, "
                                          "https://, ftp:// o magnet:).")
        return

    def one(u):
        if u.lower().startswith("magnet:") or u.lower().split("?")[0].endswith(".torrent"):
            r = {"url": u, "name": torrent_name(u), "size": -1, "ok": True, "err": "", "torrent": True}
        else:
            try:
                src = E.make_source(u).probe()
                r = {"url": u, "name": src.name or u.rsplit("/", 1)[-1], "size": src.size, "ok": True, "err": ""}
            except Exception as e:
                name = urllib.parse.unquote(u.split("?")[0].rstrip("/").rsplit("/", 1)[-1]) or u
                r = {"url": u, "name": name, "size": -1, "ok": False, "err": conn_error_text(e)}
        with LOCK:
            if GRAB["id"] == gid:
                GRAB["done"] += 1
        return r

    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(one, links))
    groups, order = {}, []
    for r in res:
        k = r["url"] if r.get("torrent") else E.set_key(r["name"])
        if F.is_checksum_name(r["name"]):
            k = E.set_key(os.path.splitext(r["name"])[0])
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    pk = []
    for k in order:
        parts = sorted(groups[k], key=lambda r: E.volume_order(r["name"]))
        real = [r for r in parts if not F.is_checksum_name(r["name"])]
        if not real:          # un .sfv suelto
            continue
        sizes = [r["size"] for r in real]
        pk.append({"key": k, "name": E.display_name(real[0]["name"]), "parts": parts,
                   "size": sum(x for x in sizes if x > 0), "unknown": any(x < 0 for x in sizes),
                   "offline": sum(1 for r in parts if not r["ok"]),
                   "missing": missing_parts([r["name"] for r in real]),
                   "torrent": bool(real[0].get("torrent"))})
    with LOCK:
        if GRAB["id"] == gid:
            GRAB.update(busy=False, packages=pk)


def pc_temp_root(cfg):
    d = (cfg.get("pc_temp_dir") or "").strip().strip('"')
    return d or os.path.join(downloads_dir(), "PS5 Directo (temporal)")


def _pcx_state(job):
    try:
        with open(os.path.join(PCX_DIR, job["id"] + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _pcx_save(job, st):
    try:
        os.makedirs(PCX_DIR, exist_ok=True)
        tmp = os.path.join(PCX_DIR, job["id"] + ".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, os.path.join(PCX_DIR, job["id"] + ".json"))
    except Exception:
        pass


def _pcx_forget(job):
    try:
        os.remove(os.path.join(PCX_DIR, job["id"] + ".json"))
    except Exception:
        pass


def pc_extract_fallback(job, key, archive, work, pw, cfg, cancel):
    """Si 7-Zip falla por algo suyo (metodo que no conoce, no lo reconoce), prueba otra forma: el .zip con Python y,
    si no, PS5 Dump Forge (sabe leer .zip, .7z y .rar de un titulo). True si lo ha conseguido."""
    import shutil
    if key not in ("method", "notarchive", "other"):
        return False            # contraseña, archivo dañado, falta una parte, sin espacio...: otro programa no lo arregla
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    if archive.lower().endswith(".zip"):
        import zipfile
        try:
            jlog(job, "Pruebo a descomprimirlo sin 7-Zip (el .zip con Python)")
            with zipfile.ZipFile(archive) as z:
                for m in z.infolist():
                    if cancel.is_set():
                        raise E.Cancelled()
                    parts = [x for x in m.filename.replace("\\", "/").split("/") if x not in ("", ".")]
                    if ".." in parts:
                        raise E.PipeError("Ruta peligrosa dentro del comprimido")
                    z.extract(m, work, pwd=pw.encode() if pw else None)
            jlog(job, "Descomprimido sin 7-Zip")
            return True
        except E.Cancelled:
            raise
        except Exception as e:
            jlog(job, "Tampoco así: %s" % e)
            shutil.rmtree(work, ignore_errors=True)
            os.makedirs(work, exist_ok=True)
    exe = F.find_forge(cfg.get("forge_path"), DATA_DIR, skip=tuple(SETTINGS.get("forge_bad") or ()))
    if exe:
        try:
            jlog(job, "Pruebo a sacarlo con PS5 Dump Forge en vez de 7-Zip")
            out = os.path.join(work, "contenido")
            F.forge_convert(exe, archive, out, "plain", pw or "", cancel, lambda *a: None, lambda m: jlog(job, "Forge: " + m))
            jlog(job, "Sacado con PS5 Dump Forge")
            return True
        except E.Cancelled:
            raise
        except Exception as e:
            jlog(job, "PS5 Dump Forge tampoco pudo: %s" % e)
            shutil.rmtree(work, ignore_errors=True)
            os.makedirs(work, exist_ok=True)
    return False


def fetch_to_pc(job, srcs, cfg, cancel, meter, folder):
    """Baja al PC las partes que son enlaces (las de tu PC se usan tal cual).
    Devuelve {nombre: ruta local} y la lista de archivos que hemos creado nosotros."""
    import shutil
    local, created = {}, []
    urls = [s for s in srcs if s.kind != "file"]
    for s in srcs:
        if s.kind == "file":
            local[s.name] = s.path
    if not urls:
        return local, created
    if any(s.size < 0 for s in urls):
        raise E.PipeError("Para bajar al PC hace falta saber el tamano de cada parte")
    os.makedirs(folder, exist_ok=True)

    def have(s):
        final = os.path.join(folder, s.name)
        if os.path.isfile(final) and os.path.getsize(final) == s.size:
            return s.size
        part = final + ".part"
        return min(os.path.getsize(part), s.size) if os.path.isfile(part) else 0

    total = sum(s.size for s in urls)
    missing = sum(s.size - have(s) for s in urls)
    free = shutil.disk_usage(folder).free
    if missing + 2 * GiB > free:
        raise E.PipeError("No cabe en el PC: hay que bajar %s a %s y solo hay %s libres. Libera espacio, "
                          "cambia la carpeta temporal en Ajustes o descomprime en la PS5."
                          % (human(missing), folder, human(free)))
    jlog(job, "Bajando %d partes al PC (%s) en %s" % (len(urls), human(total), folder))
    # varias partes a la vez (como los gestores de descargas): con las mismas conexiones en total, repartidas
    at_once = max(1, min(4, int(cfg.get("parallel_files") or 1), len(urls)))
    hosts = {urllib.parse.urlparse(getattr(x, "url", "") or "").netloc.lower() for x in urls}
    if any(any(h in host for h in E.LIMITED_HOSTS) and not any(d in host for d in E.DEBRID_HOSTS) for host in hosts):
        at_once = 1      # cuentas gratis de alojamientos: suelen dejar solo una descarga a la vez
    pcfg = dict(cfg)
    pcfg["_log"] = lambda m: jlog(job, m)
    if at_once > 1:
        pcfg["streams"] = max(2, int(cfg.get("streams", 8)) // at_once)
        jlog(job, "Bajo %d partes a la vez (%d conexiones cada una)" % (at_once, pcfg["streams"]))
    prog = {}
    plock = threading.Lock()

    def bump(name, n):
        with plock:
            prog[name] = n
            tot = sum(prog.values())
        setj(job, sent=tot)

    def one(k, s):
        final = os.path.join(folder, s.name)
        if os.path.isfile(final) and os.path.getsize(final) == s.size:
            bump(s.name, s.size)
            return
        part = final + ".part"
        off = os.path.getsize(part) if os.path.isfile(part) else 0
        if off > s.size:
            off = 0
        stamp = s.stamp() if hasattr(s, "stamp") else None
        if off and stamp:
            try:
                with open(part + ".huella", encoding="utf-8") as fh:
                    old = json.load(fh)
            except Exception:
                old = None
            if E.stamp_changed(old, stamp):
                jlog(job, "%s: el archivo del servidor ha cambiado desde la última vez (otra versión o se ha "
                          "vuelto a subir). Lo bajado no vale: empiezo desde cero." % s.name)
                off = 0
        if stamp:
            try:
                with open(part + ".huella", "w", encoding="utf-8") as fh:
                    json.dump(stamp, fh)
            except OSError:
                pass
        seg = getattr(s, "kind", "") == "url" and getattr(s, "ranges", False) and s.size > 0
        if not off and seg and os.path.isfile(part + ".trozos"):
            off = 1      # hay trozos de antes (el tamaño del .part no dice cuánto hay)
        if off and not (seg and os.path.isfile(part + ".trozos")):
            jlog(job, "%s: continuando desde %s" % (s.name, human(off)))
        elif off:
            jlog(job, "%s: continuando con los trozos que ya había" % s.name)
        if at_once == 1:
            setj(job, phase="Bajando al PC: parte %d de %d (%s)" % (k, len(urls), s.name))
        if seg:
            # como un gestor de descargas: cada conexión escribe su trozo en su sitio, sin esperar a las demás
            if not off:
                for x in (part, part + ".trozos"):
                    try:
                        os.remove(x)
                    except OSError:
                        pass
            had = os.path.isfile(part + ".trozos")
            prefix = 0 if had else (os.path.getsize(part) if off and os.path.isfile(part) else 0)
            E.segmented_to_file(s.url, part, s.size, pcfg, cancel, meter, progress=lambda n: bump(s.name, n),
                                log=lambda m: jlog(job, m), prefix=min(prefix, s.size))
            got = s.size
            hasher = part_hasher(job, s)
            if hasher is not None:
                hasher.valid = False          # bajado a trozos desordenados: se comprueba leyendo el archivo
            verify_part(job, s, hasher, on_bad=lambda: os.remove(part), local=part)
            os.replace(part, final)
            for x in (part + ".huella", part + ".trozos"):
                try:
                    os.remove(x)
                except OSError:
                    pass
            jlog(job, "En el PC: %s" % s.name)
            return
        got = off
        bump(s.name, got)
        hasher = part_hasher(job, s)
        if hasher is not None and off:
            hasher.valid = False          # continuacion de otra vez: no se puede sumar desde el principio
        with open(part, "r+b" if off else "wb") as f:
            f.seek(off)
            f.truncate()
            it = s.stream(off, pcfg, cancel, meter)
            for data in (hasher.wrap(it, off) if hasher else it):
                f.write(data)
                got += len(data)
                bump(s.name, got)
        if got != s.size:
            raise E.PipeError("La descarga de %s se quedo corta (%s de %s). Pulsa Reintentar."
                              % (s.name, human(got), human(s.size)))
        verify_part(job, s, hasher, on_bad=lambda: os.remove(part), local=part)
        os.replace(part, final)
        try:
            os.remove(part + ".huella")
        except OSError:
            pass
        jlog(job, "En el PC: %s" % s.name)

    for s in urls:
        final = os.path.join(folder, s.name)
        created.append(final)
        local[s.name] = final
    if at_once == 1:
        for k, s in enumerate(urls, 1):
            one(k, s)
    else:
        import concurrent.futures as cf
        nxt = {"i": 0}
        done_n = {"n": 0}
        stop = threading.Event()

        def run():
            while not stop.is_set() and not cancel.is_set():
                with plock:
                    if nxt["i"] >= len(urls):
                        return
                    k = nxt["i"]
                    nxt["i"] += 1
                one(k + 1, urls[k])
                with plock:
                    done_n["n"] += 1
                setj(job, phase="Bajando al PC: %d de %d partes listas (%d a la vez)" % (done_n["n"], len(urls), at_once))
        setj(job, phase="Bajando al PC: %d partes, %d a la vez" % (len(urls), at_once))
        with cf.ThreadPoolExecutor(max_workers=at_once) as ex:
            futs = [ex.submit(run) for _ in range(at_once)]
            err = None
            for fu in cf.as_completed(futs):
                try:
                    fu.result()
                except BaseException as e:
                    stop.set()
                    err = err or e
            if err:
                raise err
        if cancel.is_set():
            raise E.Cancelled()
    return local, created


def pc_extract_job(job, srcs, cancel):
    cfg = cfg_snapshot()
    cfg["_limit_bps"] = live_limit_bps
    log = lambda m: jlog(job, m)  # noqa: E731
    names = [s.name for s in srcs]
    tar = any(E.is_tar(n) for n in names)
    exe = None if tar else E.find_7z(cfg.get("sevenzip_path"))
    no7z = E.PipeError("Para descomprimir en el PC hace falta 7-Zip (gratis, 7-zip.org). Instalalo, "
                       "o pon la ruta de 7z.exe en Ajustes, o elige 'Descomprimir en la PS5'.")
    if not tar and not exe and not job_fmt(job):
        with LOCK:
            job["_force_plain"] = True    # si Forge tampoco puede, mas abajo sale el aviso de 7-Zip
        jlog(job, "No encuentro 7-Zip: lo intento sacar a una carpeta con PS5 Dump Forge")
    folder = os.path.join(pc_temp_root(cfg), E.safe_name(E.set_key(srcs[0].name)))
    dl, up = Meter(), Meter()
    phase = {"dl": True}
    stop_speed = threading.Event()

    def speed_loop():
        while not stop_speed.wait(1):
            if phase["dl"]:
                setj(job, speed=dl.rate(), dl_speed=0.0)
            else:
                setj(job, speed=up.rate(), dl_speed=0.0)
    threading.Thread(target=speed_loop, daemon=True).start()
    bf = E.BFpilot(cfg)
    watch = None
    try:
        # 1) Las partes tienen que estar en el PC (7-Zip necesita poder saltar de una a otra)
        if any(s.kind != "file" for s in srcs):
            setj(job, status="enviando", total=sum(max(0, s.size) for s in srcs), sent=0,
                 speed_label="Descarga al PC", sent_label="Bajado al PC", pc_step="bajar")
        check_duplicates_first(job, bf, E.title_id(srcs[0].name))
        local, created = fetch_to_pc(job, srcs, cfg, cancel, dl, folder)
        first = local[E.first_volume(names)]
        if job_fmt(job) and not tar:
            phase["dl"] = False
            res = forge_job(job, srcs, cfg, cancel, first, created, folder, up)
            if res is not None:
                return res
            if not E.is_archive(first):
                return send_local_as_is(job, cfg, first, cancel, up)
            jlog(job, "Sigo con 7-Zip y lo mando como carpeta normal")
            if not exe:
                raise no7z
        if tar:
            phase["dl"] = False
            return pc_tar_job(job, srcs, cfg, cancel, local[names[0]], created, folder, up)

        # 2) Leer el indice (con contrasena si hace falta)
        setj(job, phase="Leyendo el contenido del comprimido", speed_label="", sent_label="",
             pc_step="enviar")
        cands = password_candidates(job, cfg)
        try:
            entries, props = E.list_7z(exe, first)
            list_pw = None
        except E.PipeError as e:
            if not E._pw_error(str(e)):
                raise
            entries = None
            for pw in cands:
                try:
                    entries, props = E.list_7z(exe, first, pw)
                    list_pw = pw
                    break
                except E.PipeError:
                    continue
            if entries is None:
                raise E.PipeError("Ninguna contrasena abre el comprimido. Escribela en el trabajo o en "
                                  "Ajustes y pulsa Reintentar (no se vuelve a descargar).")
        files = [e for e in entries if not e["dir"]]
        total = sum(e["size"] for e in files)
        jlog(job, "Dentro hay %d archivos que ocupan %s descomprimidos%s"
             % (len(files), human(total), " (solido)" if props.get("Solid") == "+" else ""))
        if list_pw is not None:
            pws = [list_pw]
        elif E.needs_password(entries):
            pws = cands
            if not pws:
                raise E.PipeError("El comprimido tiene contrasena. Escribela en el trabajo o en Ajustes "
                                  "y pulsa Reintentar.")
        else:
            pws = [None]

        # 3) Sitio en la PS5: ahora se sabe exacto lo que ocupara todo
        staging = "%s/extract/%s" % (work_root(job.get("dest") or cfg_snapshot()["dest"]), job["id"])
        sig = "%s|%d|%d" % (entries[0]["path"] if entries else "", len(entries), total)
        st = _pcx_state(job)
        skip = int(st.get("done") or 0) if st.get("sig") == sig else 0
        if skip:
            try:
                if not bf.alive() or not bf.exists(staging):
                    skip = 0
            except Exception:
                skip = 0
        already = sum(e["size"] for e in entries[:skip])
        free = free_space(bf)
        margin = margin_bytes()
        if free is not None:
            need = total - already
            if need + margin > free:
                raise no_space(job, need, free, "No cabe en la PS5: descomprimido ocupa %s, faltan por mandar %s, "
                                  "hay %s libres y dejo %s de margen. Libera espacio y pulsa Reintentar."
                                  % (human(total), human(need), human(free), human(margin)))
            jlog(job, "Espacio libre en la PS5: %s (hacen falta %s)" % (human(free), human(need)))
        watch = start_space_watch(job, bf, cancel, "el envio")

        # 4) Descomprimir y mandar, archivo a archivo
        phase["dl"] = False
        setj(job, status="enviando", total=total, sent=already, pc_step="enviar",
             phase="Descomprimiendo en el PC y enviando a la PS5")
        ordinal = {}
        for e in entries:
            if not e["dir"]:
                ordinal[e["path"]] = len(ordinal) + 1
        if skip:
            jlog(job, "Continuo donde iba: %s ya estaban en la PS5" % human(already))
        last = {"v": already, "t": 0.0}

        def progress(v, path):
            if v > last["v"]:
                up(v - last["v"])
                last["v"] = v
            now = time.time()
            if now - last["t"] > 0.5:
                last["t"] = now
                name = path.rsplit("/", 1)[-1]
                setj(job, sent=v, phase="Descomprimiendo en el PC y enviando: archivo %d de %d · %s"
                     % (ordinal.get(path, 0), len(ordinal), name[-60:]))

        # Donde estan las aplicaciones dentro del comprimido (carpeta con sce_sys/param.json)
        capture = {}
        roots = []
        for e in entries:
            low = e["path"].lower()
            if not e["dir"] and (low == "sce_sys/param.json" or low.endswith("/sce_sys/param.json")):
                root = e["path"][:-len("sce_sys/param.json")].rstrip("/")
                if not any(root == r or root.startswith(r + "/") for r in roots if r) and "" not in roots:
                    roots.append(root)
                    capture[e["path"]] = None
        state = {"done": skip, "files": 0}

        def on_file(n):
            state["done"] = n
            if not entries[n - 1]["dir"]:
                state["files"] += 1
            _pcx_save(job, {"sig": sig, "done": n})

        for k, pw in enumerate(pws):
            start = state["done"]
            files_before = state["files"]
            if pw:
                jlog(job, "Probando contrasena %d de %d" % (k + 1, len(pws)))
            try:
                E.stream_extract_7z(exe, first, entries, pw, staging, cfg, cancel, log, progress,
                                    on_file=on_file, skip=start, capture=capture)
                if pw:
                    jlog(job, "Contrasena correcta")
                break
            except E.PipeError as e:
                if cancel.is_set():
                    raise E.Cancelled()
                bad_pw = state["files"] == files_before and len(pws) > 1 and (
                    "contrasena" in str(e) or "CRC" in str(e))
                if bad_pw and k + 1 < len(pws):
                    continue
                if bad_pw:
                    raise E.PipeError("Ninguna contrasena ha funcionado. Escribela en el trabajo o en "
                                      "Ajustes y pulsa Reintentar (no se vuelve a descargar).")
                raise
        setj(job, sent=total)
        jlog(job, "Enviado ya descomprimido: %d archivos, %s, todos comprobados con su CRC"
             % (len(files), human(total)))
        games = []
        for root in roots:
            pj = (root + "/" if root else "") + "sce_sys/param.json"
            raw = capture.get(pj) or b""
            tid = (E.title_id(raw.decode("utf-8", "replace")) or E.title_id(root)
                   or E.title_id(srcs[0].name))
            games.append({"root": root, "tid": tid})
        return {"mode": "pcx", "staging": staging, "hint": E.display_name(srcs[0].name), "games": games,
                "pc_files": created, "pc_folder": folder if created else ""}
    finally:
        stop_speed.set()
        if watch:
            watch.set()
        setj(job, speed=0.0, dl_speed=0.0, speed_label="", sent_label="")


def ftp_place(job, info, dest, cfg):
    """Coloca lo enviado en modo PC solo con FTP (sin BFpilot): renombrar es instantaneo."""
    staging = info["staging"]
    hint = info.get("hint") or job["id"]
    ftp = E.Ftp(cfg)
    f = ftp.connect()

    def is_dir(path):
        try:
            f.cwd(path)
            f.cwd("/")
            return True
        except Exception:
            return False
    try:
        if not is_dir(staging):
            _pcx_forget(job)
            raise E.PipeError("No encuentro en la PS5 lo enviado (%s). Pulsa Reintentar para mandarlo "
                              "de nuevo." % staging)
        games = info.get("games") or []
        mains = [g for g in games if not _backport_label(g.get("root") or "")]
        if mains and len(mains) < len(games):
            for g in games:
                label = _backport_label(g.get("root") or "")
                if not label:
                    continue
                _, msg = _backport_msg(job, label, g.get("tid") or "?", None)
                aside = "%s/backports/%s-%s" % (WORK_ROOT, g.get("tid") or "x", E.safe_name(label))
                try:
                    E.Ftp.mkdirs(f, WORK_ROOT + "/backports")
                    f.rename(staging + "/" + g["root"], aside)
                    jlog(job, msg + " Lo dejo en %s." % aside)
                except Exception as e:
                    jlog(job, "No pude apartar el backport (%s): %s" % (g["root"], e))
            games = mains
        moves = []
        if games:
            for g in games:
                src = staging + ("/" + g["root"] if g["root"] else "")
                name = (g["tid"] + "-app") if g.get("tid") else E.safe_name(
                    g["root"].rsplit("/", 1)[-1] if g["root"] else hint)
                moves.append((src, dest + "/" + name))
        else:
            moves.append((staging, dest + "/" + E.safe_name(hint)))
        placed = []
        for src, target in moves:
            if is_dir(target):
                target = resolve_existing(job, target, is_dir, lambda t: ftp_rmtree(cfg, t, threading.Event()),
                                          E.title_id(target))
            E.Ftp.mkdirs(f, dest)
            f.rename(src, target)
            jlog(job, "Colocado: %s" % target)
            placed.append(target)
        job["_placed_game"] = bool(games)
        # carpeta de trabajo vacia (si venia dentro de otra carpeta, quedan restos)
        try:
            f.rmd(staging)
        except Exception:
            if is_dir(staging):
                jlog(job, "Quedan restos en %s (vacios). Puedes borrarlos con Limpieza." % staging)
        return placed
    finally:
        try:
            f.quit()
        except Exception:
            pass


def pc_tar_job(job, srcs, cfg, cancel, path, created, folder, up):
    """.tar / .tar.gz / .tar.bz2 / .tar.xz: se descomprime en el PC con Python (sin 7-Zip)."""
    staging = "%s/extract/%s" % (work_root(job.get("dest") or cfg_snapshot()["dest"]), job["id"])
    total = os.path.getsize(path)
    sig = "tar|%s|%d" % (os.path.basename(path), total)
    st = _pcx_state(job)
    skip = int(st.get("done") or 0) if st.get("sig") == sig else 0
    bf = E.BFpilot(cfg)
    if skip:
        try:
            if not bf.alive() or not bf.exists(staging):
                skip = 0
        except Exception:
            skip = 0
    free = free_space(bf)
    if free is not None:
        jlog(job, "Espacio libre en la PS5: %s" % human(free))
        if free < margin_bytes():
            raise no_space(job, total, free,
                           "No hay sitio en la PS5: quedan %s libres y el margen es %s. Libera espacio "
                           "y pulsa Reintentar." % (human(free), human(margin_bytes())))
    jlog(job, "Descomprimiendo el .tar en el PC y enviando a la PS5")
    setj(job, status="enviando", total=total, sent=0, pc_step="enviar", sent_label="Leído del comprimido",
         speed_label="Velocidad (comprimido)", phase="Descomprimiendo en el PC y enviando a la PS5")
    last = {"v": 0, "t": 0.0}

    def progress(v, tot, name):
        if v > last["v"]:
            up(v - last["v"])
            last["v"] = v
        now = time.time()
        if now - last["t"] > 0.5:
            last["t"] = now
            setj(job, sent=v, phase="Descomprimiendo en el PC y enviando: %s" % name.rsplit("/", 1)[-1][-60:])

    def on_file(n):
        _pcx_save(job, {"sig": sig, "done": n})
    watch = start_space_watch(job, bf, cancel, "el envio")
    try:
        nfiles, nbytes, params = E.stream_extract_tar(path, staging, cfg, cancel, lambda m: jlog(job, m),
                                                      progress, on_file, skip)
    finally:
        watch.set()
    setj(job, sent=total)
    jlog(job, "Enviado ya descomprimido: %d archivos, %s" % (nfiles, human(nbytes)))
    games = []
    for pj, raw in params.items():
        root = pj[:-len("sce_sys/param.json")].rstrip("/")
        if any(root == g["root"] or root.startswith(g["root"] + "/") for g in games if g["root"]):
            continue
        games.append({"root": root, "tid": E.title_id(raw.decode("utf-8", "replace")) or E.title_id(root)})
    return {"mode": "pcx", "staging": staging, "hint": E.display_name(srcs[0].name), "games": games,
            "pc_files": created, "pc_folder": folder if created else ""}


def finish_pc_extract(job, info, bf, dest, cancel, cfg):
    staging = info["staging"]
    setj(job, status="colocando", phase="Colocando en su sitio")
    if not bf.alive():
        jlog(job, "BFpilot no esta cargado: lo coloco por FTP")
        placed = ftp_place(job, info, dest, cfg)
    else:
        if not bf.exists(staging):
            _pcx_forget(job)
            raise E.PipeError("No encuentro en la PS5 lo enviado (%s). Pulsa Reintentar para mandarlo "
                              "de nuevo." % staging)
        placed = place(job, bf, staging, dest, cancel, info.get("hint") or job["id"])
        for target in placed:
            fix_game_perms(job, bf, target, cancel)
        if bf.exists(staging):
            try:
                bf.delete(staging, cancel)
            except Exception as e:
                jlog(job, "No pude borrar %s: %s" % (staging, e))
    _pcx_forget(job)
    pc_files = info.get("pc_files") or []
    if pc_files and cfg.get("delete_archives", True):
        for pth in pc_files:
            try:
                os.remove(pth)
            except Exception:
                pass
        try:
            os.rmdir(info.get("pc_folder") or "")
        except Exception:
            pass
        jlog(job, "Borradas del PC las partes descargadas")
    elif pc_files:
        jlog(job, "Las partes descargadas se quedan en %s" % info.get("pc_folder"))
    return placed


# ---------------------------------------------------------------------------
# Fase 3: extraer en la PS5 y colocar el resultado
# ---------------------------------------------------------------------------

def wait_bfpilot(job, bf, cancel):
    first = True
    while True:
        v = bf.alive()
        if v:
            return v
        if first:
            jlog(job, "BFpilot no responde. Cargalo en la PS5 desde Payload Manager; sigo esperando.")
            first = False
        setj(job, phase="Esperando a BFpilot (cargalo desde Payload Manager)")
        if cancel.wait(10):
            raise E.Cancelled()


def find_payloads(bf, root, depth=4):
    """Busca aplicaciones (carpetas con sce_sys), imagenes y pkg dentro de lo extraido."""
    found = []

    def walk(path, d):
        entries = bf.listdir(path)
        names = {e["name"]: e for e in entries}
        if "sce_sys" in names and names["sce_sys"].get("dir"):
            sub = {e["name"] for e in bf.listdir(path + "/sce_sys")}
            if "param.json" in sub or "param.sfo" in sub:
                found.append(("game", path))
                return
        for e in entries:
            full = path + "/" + e["name"]
            low = e["name"].lower()
            if e.get("dir"):
                if d < depth and not low.startswith(".bfpilot-extracting"):
                    walk(full, d + 1)
            elif low.endswith(E.IMAGE_EXTS):
                found.append(("image", full))
            elif low.endswith(".pkg"):
                found.append(("pkg", full))

    walk(root, 0)
    return found


def game_bases(bf):
    """Carpetas que escanea ShadowMountPlus: interno + USB/externos montados."""
    bases = ["/data/homebrew"]
    pl = bf.places() or {}
    for m in (pl.get("mounts") or []) + (pl.get("places") or []):
        path = (m.get("path") or "").rstrip("/")
        if re.match(r"^/mnt/(usb|ext)\d+$", path):
            for b in (path + "/homebrew", path):
                if b not in bases:
                    bases.append(b)
    return bases


def fix_all_perms():
    """Repasa las aplicaciones en carpeta de /data/homebrew (y USB) y les pone permisos 777."""
    bf = E.BFpilot(cfg_snapshot())
    if not bf.alive():
        raise E.PipeError("Hace falta BFpilot cargado para revisar los permisos")
    out = []
    for base in game_bases(bf):
        try:
            ents = bf.listdir(base)
        except Exception:
            continue
        for e in ents:
            if not e.get("dir"):
                continue
            path = base + "/" + e["name"]
            try:
                changed, seen, notable = bf.fix_perms(path)
                out.append({"path": path, "changed": changed, "seen": seen + 1,
                            "eboot": ["%o" % v for v in notable.values()][:1]})
            except Exception as ex:
                out.append({"path": path, "error": str(ex)})
    return out


def find_duplicates(bf, tid, exclude=None):
    """Rutas que ya contienen ese Title ID (carpetas o imagenes)."""
    if not tid:
        return []
    out = []
    for base in game_bases(bf):
        try:
            entries = bf.listdir(base)
        except Exception:
            continue
        for e in entries:
            if tid.upper() in e["name"].upper():
                p = "%s/%s" % (base, e["name"])
                if p != exclude:
                    out.append(p)
    return out


class NeedsDecision(Exception):
    """El trabajo se para a preguntar algo (por ejemplo, que hacer con una copia que ya existe)."""


def ftp_find_duplicates(cfg, tid, exclude=None):
    """Como find_duplicates pero solo con FTP (sin BFpilot): mira /data/homebrew y los USB."""
    out = []

    def go(f):
        for base in ["/data/homebrew"] + ["/mnt/usb%d/homebrew" % i for i in range(4)] + \
                    ["/mnt/ext%d/homebrew" % i for i in range(2)]:
            try:
                ents = E.ftp_listdir(f, base)
            except ftplib.all_errors:
                continue
            for n, _ in ents:
                if tid.upper() in n.upper() and not n.endswith(".ps5part"):
                    p = "%s/%s" % (base, n)
                    if p != exclude:
                        out.append(p)
    E.Ftp(cfg).call(go)
    return out


def check_duplicates_first(job, bf, tid, exclude=None):
    """Antes de enviar: si ese juego ya esta en la consola, pregunta que hacer (una vez).
    Respuestas (job["dup_choice"]): send = enviar igualmente, replace = borrar la copia y enviar."""
    if not tid or job.get("merge_into"):
        return
    cfg = cfg_snapshot()
    try:
        dups = find_duplicates(bf, tid, exclude) if bf.alive() else ftp_find_duplicates(cfg, tid, exclude)
    except Exception:
        return
    own = set(job.get("own_paths") or [])
    # lo que este mismo trabajo ya empezo a enviar (por ejemplo, al reanudar) no es una copia repetida
    dups = [d for d in dups if not d.endswith(".ps5part") and d.rstrip("/") not in own]
    if not dups:
        return
    choice = job.get("dup_choice")
    if choice == "replace":
        for p in dups:
            if trash_move(p, why="reemplazado"):
                jlog(job, "La copia que ya había (%s) va a la papelera: si lo nuevo no funciona, la recuperas desde "
                          "Espacio › Papelera" % p)
                continue
            setj(job, phase="Borrando la copia anterior: %s" % p)
            jlog(job, "Borro la copia que ya habia: %s" % p)
            if bf.alive():
                bf.delete(p)
            else:
                ftp_rmtree(cfg, p, threading.Event())
        need = int(max(0, job.get("total") or 0) * 2.1) + margin_bytes()
        if need and trash_load():
            trash_make_room(need, bf)
        with LOCK:
            job["dup_choice"] = "send"
        return
    if choice == "send" or not cfg.get("dup_check", True):
        msg = ("Ojo: ya tienes %s en %s. Dos copias con el mismo Title ID pueden liar a "
               "ShadowMountPlus (deja solo una)." % (tid, ", ".join(dups[:4])))
        jlog(job, msg)
        with LOCK:
            job["warning"] = msg
        return
    with LOCK:
        job["ask"] = {"kind": "dup", "tid": tid, "paths": dups[:8]}
    jlog(job, "%s ya esta en la consola (%s): espero a que elijas" % (tid, ", ".join(dups[:4])))
    raise NeedsDecision()


def claim_target(job, target):
    """Apunta que este trabajo escribe en `target`, para no confundirlo con una copia repetida al reanudar."""
    with LOCK:
        own = job.setdefault("own_paths", [])
        if target not in own:
            own.append(target.rstrip("/"))
    save_jobs()


def no_space(job, need, free, msg):
    """Error de falta de sitio, guardando cuanto habria que liberar (boton "Hacer sitio")."""
    with LOCK:
        job["need_bytes"] = int(max(0, need + margin_bytes() - (free or 0)))
    return E.PipeError(msg)


def warn_duplicates(job, bf, tid, exclude=None, when=""):
    try:
        dups = find_duplicates(bf, tid, exclude)
    except Exception:
        return
    if dups:
        msg = ("Ojo: ya tienes %s en %s. Dos copias con el mismo Title ID pueden liar a "
               "ShadowMountPlus (deja solo una)." % (tid, ", ".join(dups[:4])))
        jlog(job, msg + when)
        with LOCK:
            job["warning"] = msg


def resolve_existing(job, target, exists, delete, tid=None):
    """Al colocar, si en el destino ya hay algo con ese nombre: lo decide el usuario (una vez).
    Reemplazar = se borra lo que habia; Enviar igualmente = se coloca al lado con otro nombre.
    Devuelve el nombre final, o lanza NeedsDecision para que el trabajo se pare a preguntar."""
    choice = job.get("dup_choice")
    if choice == "replace":
        if trash_move(target, why="reemplazado"):
            jlog(job, "La copia que ya había en %s va a la papelera (se recupera desde Espacio › Papelera)" % target)
            return target
        jlog(job, "Borro la copia que ya había en %s" % target)
        setj(job, phase="Borrando la copia anterior: %s" % target)
        delete(target)
        return target
    if choice == "send":
        k = 2
        while exists("%s-%d" % (target, k)):
            k += 1
        alt = "%s-%d" % (target, k)
        msg = ("Ya tenías %s: esta copia queda como %s. Dos copias con el mismo Title ID pueden liar a "
               "ShadowMountPlus (deja solo una)." % (target, alt))
        jlog(job, msg)
        with LOCK:
            job["warning"] = msg
        return alt
    with LOCK:
        job["ask"] = {"kind": "dup", "tid": tid or posixpath.basename(target), "paths": [target], "at": "place"}
    jlog(job, "Ya existe %s: espero a que elijas" % target)
    raise NeedsDecision()


def _backport_label(rel):
    """'backport (4.xx)' si alguna carpeta de la ruta se llama asi; None si no."""
    for seg in rel.strip("/").split("/"):
        if "backport" in seg.lower():
            return seg
    return None


def _backport_msg(job, label, tid, need_fw):
    mine = (SETTINGS.get("console_fw") or "").strip()
    ok = F.fw_ok(mine, need_fw) if mine and need_fw else None
    if ok:
        return ok, ("El comprimido trae también un %s de %s para firmwares más bajos. Tu PS5 (%s) puede abrir "
                    "la versión normal, así que coloco esa y no el backport." % (label, tid, mine))
    return ok, ("El comprimido trae la versión normal de %s y un %s. Coloco la normal; el backport lo dejo aparte "
                "por si tu firmware lo necesita." % (tid, label))


def split_backports(job, bf, found, staging):
    """Si lo extraido trae la aplicacion normal y ademas un 'backport' con el mismo Title ID, solo se coloca la
    normal (antes salian las dos con el mismo nombre y la segunda daba 'Ya existe'). El backport se aparta."""
    games = [p for k, p in found if k == "game"]
    mains = [p for p in games if not _backport_label(p[len(staging):])]
    bps = [p for p in games if _backport_label(p[len(staging):])]
    if not mains or not bps:
        return found
    try:
        facts = F.param_facts(bf.read_text(mains[0] + "/sce_sys/param.json"))
    except Exception:
        facts = {}
    tid = facts.get("tid") or E.title_id(mains[0]) or "?"
    for bp in bps:
        label = _backport_label(bp[len(staging):])
        ok, msg = _backport_msg(job, label, tid, facts.get("fw_param"))
        jlog(job, msg)
        if ok:
            try:
                bf.delete(bp)
            except Exception:
                pass
        else:
            aside = "%s/backports/%s-%s" % (WORK_ROOT, tid, E.safe_name(label))
            try:
                bf.mkdir(WORK_ROOT + "/backports")
                if bf.exists(aside):
                    bf.delete(aside)
                bf.rename(bp, aside)
                jlog(job, "Backport apartado en %s: si tu firmware lo necesita, copia su contenido encima de la "
                          "carpeta de la aplicación." % aside)
            except Exception as e:
                jlog(job, "No pude apartar el backport (%s): %s" % (bp, e))
    return [(k, p) for k, p in found if not (k == "game" and p in bps)]


def place(job, bf, staging, dest, cancel, hint_name):
    found = find_payloads(bf, staging)
    placed = []
    found = split_backports(job, bf, found, staging)
    if not found:
        jlog(job, "No encontre sce_sys ni imagenes: dejo todo junto en una carpeta")
        found = [("folder", staging)]
    for kind, path in found:
        if cancel.is_set():
            raise E.Cancelled()
        name = posixpath.basename(path)
        if kind == "game":
            # Siempre TITLEID-app, venga suelto o dentro de una carpeta del RAR (los de «Recomendados», TITLEID tal
            # cual: esos programas buscan su propia carpeta por ese nombre)
            tid = (E.title_id(bf.read_text(path + "/sce_sys/param.json"))
                   or E.title_id(name) or E.title_id(hint_name))
            if tid:
                name = tid if job.get("exact_name") else tid + "-app"
            elif path == staging:
                name = E.safe_name(hint_name)
        elif path == staging:
            name = E.safe_name(hint_name)
        target_dir = "/data/pkg" if kind == "pkg" else dest
        target = target_dir + "/" + name
        if kind in ("game", "image"):
            warn_duplicates(job, bf, E.title_id(name), exclude=target)
        if bf.exists(target) and kind == "game" and not bf.exists(target + "/sce_sys/param.json"):
            # resto de un envio anterior que se quedo a medias: no es una aplicacion, se sustituye
            jlog(job, "En %s había un envío anterior a medias: lo sustituyo" % target)
            bf.delete(target, cancel)
        if job.get("merge_into") and kind == "game" and bf.exists(target):
            # actualizar un emulador o programa de «Recomendados»: lo nuevo encima, sin tocar ajustes, partidas,
            # BIOS ni contenido que tengas dentro de su carpeta
            jlog(job, "Actualizo %s: copio lo nuevo encima y conservo tus archivos" % target)
            bf_merge(bf, path, target, cancel, job)
            placed.append(target)
            job["_placed_game"] = True
            continue
        if bf.exists(target):
            target = resolve_existing(job, target, bf.exists, lambda t: bf.delete(t, cancel), E.title_id(name))
        bf.mkdir(target_dir)
        bf.rename(path, target)
        jlog(job, "Colocado: %s" % target)
        placed.append(target)
        if kind in ("game", "image"):
            job["_placed_game"] = True
    return placed


def bf_merge(bf, src, dst, cancel=None, job=None):
    """Mueve el contenido de src dentro de dst, sustituyendo solo los archivos que vienen en src.
    Rápido aunque sean decenas de miles de archivos (RetroArch): se lista cada carpeta de destino una vez, cada
    archivo se mueve con un solo renombrado, y los que había se apartan a una papelera que se borra al final
    de una vez (borrar archivo a archivo en BFpilot es lentísimo: cada borrado es una tarea)."""
    trash = dst.rstrip("/") + "/.ps5directo-viejo-%d" % int(time.time())
    st = {"n": 0, "t": 0.0, "trash": False}

    def tick():
        st["n"] += 1
        if job is not None and time.time() - st["t"] > 1:
            st["t"] = time.time()
            setj(job, phase="Colocando en su sitio: %d archivos" % st["n"])

    def to_trash(b):
        if not st["trash"]:
            bf.mkdir(trash)
            st["trash"] = True
        bf.rename(b, trash + "/%d-%s" % (st["n"], posixpath.basename(b)))

    def walk(a_dir, b_dir):
        try:
            have = {e["name"]: bool(e.get("dir")) for e in bf.listdir(b_dir)}
        except Exception:
            have = {}
        for e in bf.listdir(a_dir):
            if cancel is not None and cancel.is_set():
                raise E.Cancelled()
            name = e["name"]
            a, b = a_dir + "/" + name, b_dir + "/" + name
            if e.get("dir"):
                if have.get(name) is True:
                    walk(a, b)
                    continue
                if name in have:
                    to_trash(b)          # había un archivo con el nombre de la carpeta nueva
                bf.rename(a, b)
            else:
                if name in have:
                    try:
                        bf.rename(a, b)  # renombrar sustituye el archivo de una vez
                    except Exception:
                        to_trash(b)
                        bf.rename(a, b)
                else:
                    bf.rename(a, b)
            tick()

    walk(src, dst)
    for x in (src, trash if st["trash"] else None):
        if x:
            try:
                bf.delete(x, cancel)
            except Exception:
                pass


def fix_game_perms(job, bf, target, cancel=None):
    """Tras descomprimir con BFpilot: permisos 777 (si no, la PS5 no la arranca)."""
    try:
        if not bf.exists(target):
            return
        setj(job, phase="Arreglando permisos de %s" % posixpath.basename(target))
        changed, seen, notable = bf.fix_perms(
            target, cancel, log=lambda m: jlog(job, m),
            progress=lambda n, c: setj(job, phase="Arreglando permisos de %s: %d revisados" % (posixpath.basename(target), n)))
        old = ", ".join("%s tenia %o" % (k, v) for k, v in list(notable.items())[:2])
        if changed:
            jlog(job, "Permisos arreglados (777) en %d de %d elementos%s" % (changed, seen + 1,
                                                                          (": " + old) if old else ""))
        else:
            jlog(job, "Permisos ya correctos")
    except E.Cancelled:
        raise
    except Exception as e:
        jlog(job, "AVISO: no pude poner los permisos de %s (%s). Si no arranca, en FileZilla: "
                  "clic derecho en la carpeta > Permisos > 777 > recurrir a subdirectorios." % (target, e))


def password_candidates(job, cfg):
    cands = []
    for p in ([job.get("password")] + list(cfg.get("passwords") or [])):
        if p and p not in cands:
            cands.append(p)
    if cfg.get("guess_passwords", True):
        for p in E.password_guesses([s["name"] for s in job["sources"]]):
            if p not in cands:
                cands.append(p)
    return cands


def extract_job(job, info, cancel):
    bf = E.BFpilot(cfg_snapshot())
    watch = start_space_watch(job, bf, cancel, "la descompresion")
    try:
        return _extract_job(job, info, cancel)
    finally:
        watch.set()


def _extract_job(job, info, cancel):
    cfg = cfg_snapshot()
    bf = E.BFpilot(cfg)
    dest = job["dest"] or cfg["dest"]
    if info["mode"] == "done":
        for p in info.get("placed") or []:
            jlog(job, "Colocado: %s" % p)
        job["_placed_game"] = bool(info.get("game"))
        return info.get("placed") or []
    if info["mode"] == "pcx":
        return finish_pc_extract(job, info, bf, dest, cancel, cfg)
    staging = "%s/extract/%s" % (work_root(job.get("dest") or cfg_snapshot()["dest"]), job["id"])

    if info["mode"] == "file" and not bf.alive():
        # sin BFpilot: renombrar por FTP es instantaneo (mismo disco)
        name = info["name"]
        low = name.lower()
        target_dir = "/data/pkg" if low.endswith(".pkg") else dest
        target = target_dir + "/" + name
        box = {"target": target}

        def go(f):
            try:
                E.ftp_listdir(f, target_dir)
            except ftplib.error_perm:
                E.Ftp.mkdirs(f, target_dir)
            if E.Ftp.size(f, target) is not None:
                base, ext = os.path.splitext(target)

                def ftp_del(t):
                    f.delete(t)
                t2 = resolve_existing(job, base, lambda t: E.Ftp.size(f, t + ext) is not None,
                                      lambda t: ftp_del(t + ext), E.title_id(name))
                box["target"] = t2 + ext
            f.rename(info["path"], box["target"])
            try:
                f.rmd(info["path"].rsplit("/", 1)[0])
            except ftplib.all_errors:
                pass
        E.Ftp(cfg).call(go)
        target = box["target"]
        jlog(job, "Colocado: %s" % target)
        if low.endswith(E.IMAGE_EXTS):
            job["_placed_game"] = True
        return [target]

    wait_bfpilot(job, bf, cancel)
    if info["mode"] == "file":
        name = info["name"]
        low = name.lower()
        target_dir = "/data/pkg" if low.endswith(".pkg") else dest
        target = target_dir + "/" + name
        if bf.exists(target):
            base, ext = os.path.splitext(target)
            target = resolve_existing(job, base, lambda t: bf.exists(t + ext), lambda t: bf.delete(t + ext, cancel),
                                      E.title_id(name)) + ext
        bf.mkdir(target_dir)
        bf.rename(info["path"], target)
        jlog(job, "Colocado: %s" % target)
        try:
            bf.delete(info["path"].rsplit("/", 1)[0], cancel)
        except Exception:
            pass
        if low.endswith(".pkg"):
            jlog(job, "Es un .pkg: instalalo con ps5upload (Install Package) desde /data/pkg.")
        return [target]

    src = info["dir"] + "/" + info["first"]
    cands = password_candidates(job, cfg) + [""]
    xroot = posixpath.dirname(staging)
    if xroot != WORK_ROOT + "/extract":
        bf.mkdir(posixpath.dirname(xroot))
        jlog(job, "Se descomprime directamente en el disco de destino (%s)" % xroot.rsplit("/ps5directo", 1)[0])
    bf.mkdir(xroot)
    if bf.exists(staging):
        # BFpilot solo crea esta carpeta cuando la extraccion termina bien
        jlog(job, "Ya estaba descomprimido de antes; paso a colocarlo")
        cands = []
    else:
        for e in bf.listdir(xroot):
            if e["name"].startswith(job["id"] + ".bfpilot-extracting"):
                jlog(job, "Borrando restos de un intento anterior: %s" % e["name"])
                bf.delete(xroot + "/" + e["name"], cancel)

    live = {"sig": None, "t": time.time(), "warned": False}
    guard = {"t": 0.0}

    def check_runaway(st):
        """Mide cuanto lleva escrito BFpilot y cuanto queda libre, para avisar a tiempo."""
        total = int(st.get("totalBytes") or 0)
        if total <= 0 or time.time() - guard["t"] < 10:
            return
        guard["t"] = time.time()
        try:
            # Solo la carpeta temporal de ESTA descompresion (BFpilot la dice en "stage").
            # Antes se sumaban restos de intentos anteriores y saltaba en falso.
            stage = st.get("stage") or ""
            if "/ps5directo/extract/" not in stage or ".bfpilot-extracting" not in stage:
                return
            size = bf.du(stage) or 0
            free = free_space(bf, stage.split("/ps5directo/extract/")[0] or "/data")
            guard["size"], guard["free"] = size, free
            # Prevision: el progreso de BFpilot cuenta lo LEIDO del RAR, no lo escrito. Un volcado en
            # carpeta va sin comprimir y puede ocupar varias veces el RAR (visto: 35 GB -> 145 GB).
            # Regla prudente: hasta ~5 veces lo que ocupa el RAR.
            guard["fits"] = None
            if free is not None:
                worst = total * 5
                guard["final"] = max(worst, size)
                # lo que aun puede crecer (al menos 10 GB de colchon aunque ya pase del estimado)
                guard["fits"] = (free - margin_bytes()) > max(worst - size, 10 * GiB)
        except Exception:
            pass

    def on_status(st):
        pct = int(st.get("percent") or 0)
        txt = []
        if st.get("totalBytes"):
            # OJO: BFpilot cuenta lo LEIDO del RAR, no el tamano final (que puede ser mucho mayor)
            txt.append("leído del comprimido %s de %s" % (human(st.get("bytesWritten") or 0), human(st["totalBytes"])))
        if st.get("averageMBps"):
            txt.append("%.0f MB/s" % float(st["averageMBps"]))
        # Senal de vida: el porcentaje a veces solo sube al acabar cada archivo grande,
        # pero lo leido/escrito y la CPU de BFpilot cambian aunque el % no se mueva.
        sig = (st.get("bytesWritten"), st.get("inputBytes"), st.get("outputBytes"),
               st.get("cpuTotalMs"), st.get("filesDone"), st.get("current"))
        now = time.time()
        if sig != live["sig"]:
            live.update(sig=sig, t=now)
            if live["warned"]:
                jlog(job, "La descompresion vuelve a avanzar")
                live["warned"] = False
        if guard.get("size") is not None:
            t = "descomprimido %s" % human(guard["size"])
            if guard.get("free") is not None:
                t += " · libre %s" % human(guard["free"])
            if guard.get("fits") is False:
                t += " · justo de espacio (en el peor caso podria llegar a ~%s)" % human(guard.get("final", 0))
            elif guard.get("fits"):
                t += " · hay sitio de sobra"
            txt.append(t)
        idle = now - live["t"]
        if st.get("cpuUtilPercent") is not None and idle < 30:
            txt.append("CPU %d%%" % float(st["cpuUtilPercent"]))
        if idle >= 300:
            txt.append("sin cambios desde hace %d min" % (idle // 60))
            if not live["warned"]:
                jlog(job, "Aviso: la descompresion no da senales de vida desde hace 5 min. "
                          "Mira en BFpilot (puerto 5905) si sigue 'running' antes de cancelar.")
                live["warned"] = True
        elif st.get("current"):
            txt.append(str(st["current"])[-60:])
        setj(job, extract_pct=pct, extract_text=" · ".join(txt))
        check_runaway(st)
        if guard.get("fits") is False and not guard.get("fit_logged"):
            guard["fit_logged"] = True
            jlog(job, "Aviso: lo descomprimido puede ocupar hasta ~5 veces el RAR (~%s) y quedan %s "
                      "libres. Puede bastar, pero si el espacio baja del margen lo paro. Si puedes, "
                      "libera espacio." % (human(guard.get("final", 0)), human(guard.get("free") or 0)))

    last_err = None
    final_st = None
    for i, pw in enumerate(cands):
        label = ("contraseña %d de %d" % (i + 1, len(cands) - 1)) if pw else "sin contraseña"
        setj(job, status="extrayendo", phase="Descomprimiendo en la PS5 (%s)" % label,
             extract_pct=0, extract_text="")
        jlog(job, "Descomprimiendo %s (%s)" % (info["first"], label))
        try:
            final_st = bf.extract(src, staging, pw, cancel, on_status,
                                  threads=cfg_snapshot().get("extract_threads", 0))
            if pw:
                jlog(job, "Contrasena correcta")
            last_err = None
            break
        except E.PipeError as e:
            last_err = e
            jlog(job, "Fallo: %s" % e)
            if bf.exists(staging):
                bf.delete(staging, cancel)
            if not E.is_password_error(str(e)):
                break
    if last_err is not None:
        msg = str(last_err).lower()
        if "aes" in msg:
            raise E.PipeError("Este ZIP usa cifrado AES y tu BFpilot no sabe abrirlo: actualizalo a la 0.4.9 o posterior, "
                              "que ya lo abre. Los archivos ya estan en la PS5, en %s. Puedes bajarlos al PC, "
                              "extraerlos con 7-Zip y subir la carpeta." % info["dir"])
        if "unsupported archive type" in msg and re.search(r"\.z\d{2}$|\.zip$", info["first"], re.I):
            raise E.PipeError("BFpilot no abre ZIP partidos (.z01, .z02...). Uno los RAR/7z partidos, "
                              "pero este formato no. Los archivos estan en %s." % info["dir"])
        if E.is_password_error(str(last_err)):
            raise E.PipeError("Ninguna contrasena ha funcionado. Escribela en el trabajo o en Ajustes "
                              "y pulsa Reintentar (no hace falta volver a descargar).")
        raise last_err

    got = bf.du(staging)
    if got is not None:
        jlog(job, "Lo descomprimido ocupa %s" % human(got))

    setj(job, status="colocando", phase="Colocando en su sitio", extract_pct=100)
    hint = info["first"][:len(E.set_key(info["first"]))]  # mismo nombre, sin cambiar mayusculas
    placed = place(job, bf, staging, dest, cancel, hint)
    for target in placed:
        fix_game_perms(job, bf, target, cancel)
    for leftover in (staging,):
        if bf.exists(leftover):
            try:
                bf.delete(leftover, cancel)
            except Exception as e:
                jlog(job, "No pude borrar %s: %s" % (leftover, e))
    if cfg.get("delete_archives", True):
        jlog(job, "Borrando los comprimidos de la PS5")
        try:
            bf.delete(info["dir"], cancel)
        except Exception as e:
            jlog(job, "No pude borrar %s: %s" % (info["dir"], e))
    else:
        jlog(job, "Los comprimidos se quedan en %s" % info["dir"])
    return placed


# ---------------------------------------------------------------------------
# Hilos de trabajo: uno envia, otro descomprime (pueden ir a la vez)
# ---------------------------------------------------------------------------

ALEMBIC_TO = {"exfat": {"to": "exfat"}, "ffpkg": {"to": "ffpkg"}, "ffpfsc": {"to": "ffpfsc"},
              "lz4": {"to": "folder", "lz4": True}, "zstd": {"to": "folder", "zstd": True},
              "lz4ffpkg": {"to": "ffpkg", "lz4": True}, "zstdffpkg": {"to": "ffpkg", "zstd": True}}
ALEMBIC_PORT = 5940


def _alembic(method, path, body=None, timeout=15):
    host = E.clean_host(cfg_snapshot().get("ps5_ip") or "")[0]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://%s:%d%s" % (host, ALEMBIC_PORT, path), data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8", "replace")).get("error") or ""
        except Exception:
            msg = ""
        raise E.PipeError("Alembic: %s" % (msg or "HTTP %d" % e.code))


def alembic_convert(job, placed, cancel):
    """Convierte en la propia PS5, con Alembic, lo que se acaba de colocar (si no está cargado, se carga). El
    original solo se borra si Alembic comprueba que el resultado es igual; si algo falla, se queda como estaba."""
    fmt = job.get("alembic")
    spec0 = ALEMBIC_TO.get(fmt)
    src = next((p for p in placed if p.startswith("/")), "")
    if not spec0 or not src:
        return None
    label = FMT_LABEL.get(fmt, fmt)
    setj(job, status="convirtiendo", phase="Convirtiendo en la PS5 con Alembic a %s…" % label)
    try:
        try:
            _alembic("GET", "/api/info", timeout=4)
        except Exception:
            jlog(job, "Cargo Alembic en la consola")
            extra_payload("alembic", "send")
            for _ in range(30):
                if cancel.wait(2):
                    raise E.Cancelled()
                try:
                    _alembic("GET", "/api/info", timeout=4)
                    break
                except Exception:
                    pass
            else:
                raise E.PipeError("Alembic no ha arrancado en la consola")
        name = posixpath.basename(src.rstrip("/"))
        spec = dict(spec0, kind="convert", input=src, output_dir=posixpath.dirname(src.rstrip("/")),
                    delete_source=True)
        if spec0["to"] == "folder":
            spec["output_name"] = name + "-" + ("zstd" if spec0.get("zstd") else "lz4")
        need = "ampr_runtime_ng" if spec0.get("zstd") else "ampr_runtime" if spec0.get("lz4") else ""
        try:
            jb = _alembic("POST", "/api/jobs", spec)
        except E.PipeError as e:
            if not need or "Components" not in str(e):
                raise
            jlog(job, "Alembic necesita su emulador AMPR: se lo instalo")
            _alembic("POST", "/api/deps/%s/install" % need, {}, timeout=120)
            jb = _alembic("POST", "/api/jobs", spec)
        jid = jb.get("id")
        jlog(job, "Alembic convierte %s a %s (trabajo %s)" % (name, label, jid))
        while True:
            if cancel.wait(2):
                try:
                    _alembic("POST", "/api/jobs/%s/cancel" % jid, {})
                except Exception:
                    pass
                raise E.Cancelled()
            try:
                st = _alembic("GET", "/api/jobs/%s" % jid)
            except Exception:
                continue
            state = st.get("state")
            tot, done = int(st.get("total") or 0), int(st.get("done") or 0)
            pct = int(done * 100 / tot) if tot else 0
            setj(job, phase="Convirtiendo en la PS5 con Alembic: %s%s" % (st.get("stage") or "", " %d%%" % pct if tot else ""),
                 extract_pct=pct)
            if state == "done":
                res = st.get("result") or ""
                jlog(job, "Alembic terminó: %s" % (res or label))
                try:
                    _alembic("POST", "/api/smp/scan", {})
                except Exception:
                    pass
                return [res] if isinstance(res, str) and res.startswith("/") else None
            if state in ("failed", "cancelled", "interrupted"):
                raise E.PipeError("Alembic no ha podido convertirlo: %s" % (st.get("error") or state))
    except E.Cancelled:
        raise
    except Exception as e:
        jlog(job, "AVISO: %s. El título se queda como se mandó, sin convertir." % e)
        notify("warn", "Sin convertir en la PS5", "%s: %s" % (job.get("title") or "", e), {"tab": "Transfers", "job": job["id"]})
        return None


SMP_SEEN = {"t": 0.0, "ok": True}


def smp_present(cfg):
    """¿Hay rastro de ShadowMountPlus en la consola? (su carpeta en /data). Si no se puede mirar, se da por bueno."""
    if time.time() - SMP_SEEN["t"] < 600:
        return SMP_SEEN["ok"]
    ok = True
    try:
        names = {n.lower() for n, v in E.Ftp(cfg).call(lambda f: E.ftp_listdir(f, "/data"))}
        ok = bool(names & {"shadowmount", "shadowmountplus"})
    except Exception:
        ok = True
    SMP_SEEN.update(t=time.time(), ok=ok)
    return ok


def stopped(job):
    """Un trabajo se ha parado: por falta de espacio, por pausa o porque lo cancelaste."""
    with LOCK:
        reason = job.pop("_stop", None)
        paused = job.pop("_pause", False)
    if reason:
        setj(job, status="error", error=reason, phase="Error")
        jlog(job, "ERROR: %s" % reason)
    elif paused:
        setj(job, status="pausado", phase="En pausa (pulsa Reanudar para seguir)")
        jlog(job, "En pausa")
    else:
        setj(job, status="cancelado", phase="Cancelado")


def sched_wait():
    """Segundos que faltan para la hora programada (0 = se puede empezar)."""
    with LOCK:
        t = float(SCHED.get("start") or 0)
    if not t:
        return 0
    left = t - time.time()
    if left <= 0:
        with LOCK:
            SCHED["start"] = 0
        notify("info", "Empieza la cola programada")
        return 0
    return left


ZDL_DIR = "/data/ps5directo/descargas"


def zftpd_job(job, cancel):
    """«La PS5 lo baja sola»: cada enlace lo descarga zftpd en la propia consola (http, https, ftp y magnet; sigue
    donde iba si se corta). Los .zip se descomprimen alli mismo; el resto se queda tal cual en la carpeta."""
    cfg = cfg_snapshot()
    z = F.Zftpd(cfg["ps5_ip"])
    if not z.alive():
        raise E.PipeError("zftpd no está en marcha en la consola. Inícialo en Biblioteca › ⭐ Recomendados › zftpd "
                          "(o en Payloads) y pulsa Reintentar.")
    urls = [x for x in job["lines"] if re.match(r"^((https?|ftp)://|magnet:\?)", x, re.I)]
    if not urls:
        raise E.PipeError("La consola solo puede bajar sola enlaces http, https, ftp o magnet (no archivos de tu PC).")
    dst = (job.get("zdl_dir") or ZDL_DIR).rstrip("/")
    try:
        E.Ftp(cfg).call(lambda f: _ftp_makedirs(f, dst))
    except Exception as e:
        raise E.PipeError("No pude crear %s en la consola por FTP: %s" % (dst, e))
    setj(job, status="enviando", phase="La consola empieza a descargar", total=-1, sent=0,
         title=job.get("title") or E.display_name(urllib.parse.unquote(urls[0].split("?")[0].rstrip("/").split("/")[-1]) or "magnet"))
    with LOCK:
        ids = dict(job.get("zdl_ids") or {})
    for u in urls:
        if u in ids:
            continue
        r = z.dl_start(u, dst)
        if not r.get("id"):
            raise E.PipeError("zftpd no aceptó el enlace: %s" % (r.get("error") or u))
        ids[u] = int(r["id"])
        jlog(job, "La consola descarga %s (en %s)" % (r.get("name") or u, dst))
    with LOCK:
        job["zdl_ids"] = ids
    save_jobs()
    want = set(ids.values())
    names = {}
    while True:
        if cancel.is_set():
            for i in want:
                try:
                    z.dl_cancel(i)
                except Exception:
                    pass
            with LOCK:
                job["zdl_ids"] = {}
            raise E.Cancelled()
        try:
            st = [d for d in z.dl_status() if int(d.get("id") or 0) in want]
        except Exception as e:
            setj(job, phase="No contesta zftpd (%s): sigo esperando…" % e)
            time.sleep(5)
            continue
        if len(st) < len(want):
            gone = want - {int(d["id"]) for d in st}
            raise E.PipeError("zftpd ya no tiene %d de las descargas (¿se reinició la consola o el payload?). "
                              "Pulsa Reintentar: lo que llevaba se aprovecha." % len(gone))
        bad = [d for d in st if d.get("error")]
        if bad:
            with LOCK:
                job["zdl_ids"] = {}
            raise E.PipeError("La consola no pudo bajar %s: %s" % (bad[0].get("name") or bad[0].get("url"), bad[0]["error"]))
        tot = sum(int(d.get("total_size") or 0) for d in st)
        got = sum(int(d.get("downloaded") or 0) for d in st)
        spd = sum(float(d.get("speed") or 0) for d in st)
        ndone = sum(1 for d in st if d.get("done"))
        for d in st:
            names[int(d["id"])] = d.get("name") or ""
        queued = any(d.get("queued") for d in st)
        setj(job, total=tot if tot else -1, sent=got, speed=spd,
             phase=("En espera en la cola de zftpd" if queued and not got else
                    "La consola descarga sola: %d de %d listos" % (ndone, len(st))))
        if ndone == len(st):
            break
        time.sleep(2)
    files = ["%s/%s" % (dst, names[i]) for i in want if names.get(i)]
    out = []
    for fp in files:
        if fp.lower().endswith(".zip") and not job.get("raw"):
            folder = "%s/%s" % ((job.get("dest") or "/data/homebrew").rstrip("/"), posixpath.basename(fp)[:-4])
            setj(job, status="descomprimiendo", phase="Descomprimiendo en la consola: %s" % posixpath.basename(fp), sent=0)
            try:
                z.extract(fp, folder)
                while True:
                    if cancel.is_set():
                        try:
                            z.post("/api/extract_cancel")
                        except Exception:
                            pass
                        raise E.Cancelled()
                    pr = z.extract_progress()
                    setj(job, total=int(pr.get("total_bytes") or 0) or -1, sent=int(pr.get("bytes_extracted") or 0))
                    if pr.get("error"):
                        raise E.PipeError(pr.get("error_msg") or "error al descomprimir")
                    if pr.get("done") or not pr.get("active"):
                        break
                    time.sleep(1.5)
                folder = _zdl_flatten(cfg, folder)
                out.append(folder)
                jlog(job, "Descomprimido en la consola: %s" % folder)
                try:
                    E.Ftp(cfg).call(lambda f: f.delete(fp))
                except Exception:
                    pass
                continue
            except E.Cancelled:
                raise
            except Exception as e:
                jlog(job, "No pude descomprimirlo en la consola (%s): se queda el .zip" % e)
        out.append(fp)
    setj(job, status="hecho", result=out, speed=0.0, phase="Bajado por la consola: %s" % ", ".join(out)[:300])
    with LOCK:
        job["zdl_ids"] = {}
    jlog(job, "Terminado (descargado por la consola)")
    record_history(job)


def _zdl_flatten(cfg, folder):
    """Si el .zip traia una sola carpeta dentro (lo normal), se sube un nivel: /data/homebrew/PPSA..., no
    /data/homebrew/nombre-del-zip/PPSA..."""
    def work(f):
        lst = E.ftp_listdir(f, folder)
        if len(lst) != 1 or lst[0][1].get("type") != "dir":
            return folder
        sub = lst[0][0]
        up = posixpath.dirname(folder) + "/" + sub
        if _ftp_isdir(f, up):
            return folder
        f.rename(folder + "/" + sub, up)
        f.rmd(folder)
        return up
    try:
        return E.Ftp(cfg).call(work)
    except Exception:
        return folder


def _ftp_makedirs(f, path):
    cur = ""
    for part in [x for x in path.split("/") if x]:
        cur += "/" + part
        try:
            f.mkd(cur)
        except ftplib.all_errors:
            pass


# ---------------------------------------------------------------------------
# Panel de actualizaciones: version que tienes de cada titulo frente a la ultima que conocen prosperopatches.com
# (PS5) y orbispatches.com (PS4), webs publicas de terceros. Solo se mira el numero de version: no se descarga nada
# ni se habla con servidores de Sony. Para actualizar, PatchDL (Recomendados).
# ---------------------------------------------------------------------------

UPD_FILE = os.path.join(DATA_DIR, "actualizaciones.json")
UPD = {"busy": False, "done": 0, "n": 0, "current": "", "error": ""}
UPD_CANCEL = threading.Event()
PS5_VER_RX = re.compile(r"\b(\d{2}\.\d{3}\.\d{3})\b")
PS4_VER_RX = re.compile(r"\b(?:v|ver\.?\s*|version\s*)?(\d{2}\.\d{2})\b", re.I)


def _ver_key(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except Exception:
        return ()


def _fw_tuple(fw):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", str(fw))[:2])
    except Exception:
        return ()


def upd_lookup(tid):
    """Versiones de un Title ID en prosperopatches.com (PS5) u orbispatches.com (PS4), como las pide su propia web
    (la pagina del titulo trae un codigo y con el se piden sus versiones). Solo numeros de version y firmware."""
    tid = tid.upper()
    ps4 = tid.startswith(("CUSA", "PLAS"))
    base = os.environ.get("PS5D_PATCHSITE") or ("https://orbispatches.com" if ps4 else "https://prosperopatches.com")
    url = "%s/%s" % (base, tid)
    ua = {"User-Agent": "PS5Directo (%s)" % VERSION}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=dict(ua, Accept="text/html")), timeout=20) as r:
            html = r.read(3 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"latest": "", "url": url, "found": False}
        raise
    vers = []
    m = re.search(r'data-key="([0-9a-fA-F]+)"', html) or re.search(r"'key':\s*'([0-9a-fA-F]+)'", html)
    if m:
        body = json.dumps({"titleid": tid, "key": m.group(1)}).encode()
        req = urllib.request.Request(base + "/api/internal/loadpatches", data=body, method="POST",
                                     headers=dict(ua, **{"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                                         "X-Requested-With": "XMLHttpRequest", "Referer": url}))
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                txt = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
            j = json.loads(txt)
            for e in _walk_dicts(j):
                v = e.get("content_ver") or e.get("version")
                if v and re.match(r"^\d{2}\.\d{2,3}", str(v)):
                    vers.append({"v": str(v), "fw": str(e.get("required_firmware") or "")})
        except Exception:
            vers = []
    if not vers:
        rx = PS4_VER_RX if ps4 else PS5_VER_RX
        vers = [{"v": v, "fw": ""} for v in rx.findall(html) if _ver_key(v) and _ver_key(v)[0] < 100]
    if not vers:
        return {"latest": "", "url": url, "found": False}
    best = max(vers, key=lambda x: _ver_key(x["v"]))
    out = {"latest": best["v"], "need_fw": "" if ps4 else best["fw"], "url": url, "found": True}
    with LOCK:
        mine = (SETTINGS.get("console_fw") or "").strip()
    if mine and not ps4:
        ok = [x for x in vers if x["fw"] and _fw_tuple(x["fw"]) <= _fw_tuple(mine)]
        if ok:
            out["fits"] = max(ok, key=lambda x: _ver_key(x["v"]))["v"]
    return out


def _walk_dicts(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk_dicts(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_dicts(v)


def upd_load():
    try:
        with open(UPD_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def upd_items():
    """Lo instalado de la Biblioteca (una entrada por Title ID, la version mas alta que tengas)."""
    with LOCK:
        res = LIB.get("items") or {}
    out = {}
    for it in (res.get("items") or []):
        tid = (it.get("tid") or "").upper()
        if not re.match(r"^[A-Z]{4}\d{5}$", tid) or tid.startswith("NPXS") or it.get("extra"):
            continue
        v = it.get("version") or ""
        cur = out.get(tid)
        if not cur or _ver_key(v) > _ver_key(cur["have"]):
            out[tid] = {"tid": tid, "title": it.get("title") or it.get("name") or tid, "have": v,
                        "path": it.get("path") or ""}
    return out


def upd_check(force=False):
    if UPD["busy"]:
        return
    UPD_CANCEL.clear()
    items = upd_items()
    if not items:
        UPD.update(error="Primero abre la Biblioteca para que lea lo que tienes en la consola.")
        return
    UPD.update(busy=True, done=0, n=len(items), current="", error="")

    def run():
        cache = upd_load()
        try:
            for k, (tid, it) in enumerate(sorted(items.items(), key=lambda x: x[1]["title"].lower())):
                if UPD_CANCEL.is_set():
                    break
                UPD.update(done=k, current=it["title"])
                c = cache.get(tid)
                if c and not force and time.time() - c.get("t", 0) < 24 * 3600:
                    continue
                try:
                    r = upd_lookup(tid)
                    r["t"] = time.time()
                    cache[tid] = r
                except Exception as e:
                    cache[tid] = dict(c or {}, err=str(e)[:200], t=time.time())
                time.sleep(1.2)            # sin prisas: es una web de voluntarios
                if k % 5 == 0:
                    _save_json(UPD_FILE, cache)
            _save_json(UPD_FILE, cache)
            news = [it for tid, it in items.items() if cache.get(tid, {}).get("latest")
                    and it["have"] and _ver_key(cache[tid]["latest"]) > _ver_key(it["have"])]
            if news:
                notify("info", "Actualizaciones disponibles: %d" % len(news),
                       ", ".join(x["title"] for x in news[:6]) + (" y más" if len(news) > 6 else ""),
                       {"tab": "Library"}, key="upd%d" % int(time.time() // 3600))
        except Exception as e:
            UPD["error"] = str(e)
        finally:
            UPD.update(busy=False, done=UPD["n"], current="")
    threading.Thread(target=run, daemon=True).start()


def upd_state():
    cache = upd_load()
    items = upd_items()
    rows = []
    for tid, it in items.items():
        c = cache.get(tid) or {}
        latest = c.get("latest") or ""
        st = ("new" if latest and it["have"] and _ver_key(latest) > _ver_key(it["have"]) else
              "ok" if latest and it["have"] else "unknown" if c else "unchecked")
        rows.append(dict(it, latest=latest, url=c.get("url") or "", state=st, err=c.get("err") or "",
                         checked=c.get("t"), need_fw=c.get("need_fw") or "", fits=c.get("fits") or ""))
    order = {"new": 0, "ok": 2, "unknown": 1, "unchecked": 3}
    rows.sort(key=lambda r: (order[r["state"]], r["title"].lower()))
    return dict(UPD, items=rows)


# ---------------------------------------------------------------------------
# ☁️ Mis almacenamientos (rclone): navegar tus nubes, bajar al PC o mandar a la consola. Los archivos sueltos se
# sirven en este PC solo para PS5 Directo (127.0.0.1, con codigo en la ruta) y entran en la cola como cualquier
# enlace: se bajan con varias conexiones, continuan si se cortan y se descomprimen o convierten igual.
# ---------------------------------------------------------------------------

CLOUD = {"busy": False, "phase": "", "pct": 0, "error": "", "done": ""}
CLOUD_SRV = {}           # remoto -> {"proc", "port", "token"}
CLOUD_LOCK = threading.Lock()
CLOUD_VER = {"v": None}


def cloud_exe():
    return F.rclone_path(DATA_DIR)


def _remote_ok(name):
    if not re.match(r"^[\w][\w .-]{0,60}$", name or ""):
        raise E.PipeError("Nombre de almacenamiento no válido (letras, números, guiones)")
    return name


def cloud_state():
    exe = cloud_exe()
    out = {"installed": bool(exe), "types": F.CLOUD_TYPES, "remotes": [], "srv": sorted(CLOUD_SRV)}
    out.update({k: CLOUD[k] for k in CLOUD})
    if not exe:
        return out
    if CLOUD_VER["v"] is None:
        try:
            CLOUD_VER["v"] = (F.rclone_run(exe, ["version"], 20).splitlines() or [""])[0].replace("rclone ", "")
        except Exception:
            CLOUD_VER["v"] = ""
    out["version"] = CLOUD_VER["v"]
    try:
        for ln in F.rclone_run(exe, ["listremotes", "--long"], 30).splitlines():
            name, _, typ = ln.partition(":")
            if name.strip():
                out["remotes"].append({"name": name.strip(), "type": typ.strip()})
    except Exception as e:
        out["error"] = str(e)
    return out


def _cloud_bg(phase, fn):
    if CLOUD["busy"]:
        raise E.PipeError("Espera: ya estoy con otra cosa de las nubes")
    CLOUD.update(busy=True, phase=phase, pct=0, error="", done="")

    def run():
        try:
            CLOUD["done"] = fn() or ""
        except Exception as e:
            CLOUD["error"] = str(e)
        finally:
            CLOUD.update(busy=False, phase="")
    threading.Thread(target=run, daemon=True).start()


def cloud_install():
    def fn():
        def prog(got, total):
            CLOUD["pct"] = int(got * 100 / total) if total else 0
        path, tag = F.rclone_install(DATA_DIR, prog)
        CLOUD_VER["v"] = None
        return "rclone %s instalado" % tag
    _cloud_bg("Bajando rclone de su GitHub oficial…", fn)


def cloud_add(name, typ, fields):
    exe = cloud_exe()
    if not exe:
        raise E.PipeError("Primero instala rclone")
    name = _remote_ok(name)
    t = next((x for x in F.CLOUD_TYPES if x["type"] == typ), None)
    if not t:
        raise E.PipeError("Tipo no admitido aquí: usa «Configurar a mano»")
    args = ["config", "create", name, typ]
    for k in t.get("fields") or []:
        v = str((fields or {}).get(k) or "")
        if not v:
            raise E.PipeError("Falta: %s" % k)
        args += [k, v]
    if t.get("fields"):
        args.append("--obscure")
    if t.get("oauth"):
        _cloud_bg("Se ha abierto el navegador: entra en tu cuenta y da permiso a rclone. Vuelve aquí al terminar.",
                  lambda: (F.rclone_run(exe, args, 600), "Añadido: %s" % name)[1])
        return
    F.rclone_run(exe, args, 60)
    CLOUD["done"] = "Añadido: %s" % name


def cloud_ls(remote, path):
    exe = cloud_exe()
    if not exe:
        raise E.PipeError("Primero instala rclone")
    _remote_ok(remote)
    path = (path or "").strip("/")
    raw = F.rclone_run(exe, ["lsjson", "%s:%s" % (remote, path), "--max-depth", "1", "--no-mimetype"], 180)
    items = []
    for x in json.loads(raw or "[]"):
        items.append({"name": x.get("Name"), "dir": bool(x.get("IsDir")), "size": x.get("Size"),
                      "t": x.get("ModTime"), "path": (path + "/" if path else "") + (x.get("Name") or "")})
    items.sort(key=lambda x: (not x["dir"], (x["name"] or "").lower()))
    return {"remote": remote, "path": path, "items": items}


def cloud_serve(remote):
    """Arranca (o reutiliza) «rclone serve http» de un remoto, solo en este PC."""
    exe = cloud_exe()
    if not exe:
        raise E.PipeError("rclone no está instalado")
    with CLOUD_LOCK:
        s = CLOUD_SRV.get(remote)
        if s and s["proc"].poll() is None:
            return s
        import subprocess
        sk = socket.socket()
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
        sk.close()
        token = secrets.token_hex(8)
        proc = subprocess.Popen([exe, "serve", "http", remote + ":", "--addr", "127.0.0.1:%d" % port, "--read-only",
                                 "--baseurl", "/" + token, "--vfs-cache-mode", "off"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=F.NO_WINDOW)
        t0 = time.time()
        while time.time() - t0 < 20 and not _port_open("127.0.0.1", port, 0.5):
            if proc.poll() is not None:
                raise E.PipeError("rclone no pudo abrir %s" % remote)
            time.sleep(0.3)
        s = {"proc": proc, "port": port, "token": token}
        CLOUD_SRV[remote] = s
        return s


def cloud_stop_all():
    for s in list(CLOUD_SRV.values()):
        try:
            s["proc"].terminate()
        except Exception:
            pass


import atexit  # noqa: E402
atexit.register(lambda: cloud_stop_all())


def cloud_urls(remote, paths):
    s = cloud_serve(remote)
    return ["http://127.0.0.1:%d/%s/%s" % (s["port"], s["token"], urllib.parse.quote(p.strip("/"))) for p in paths]


def cloud_fix_job(job):
    """Al empezar o reintentar un trabajo de la nube, el servidor local puede ser otro: rehace los enlaces."""
    c = job.get("cloud")
    if c:
        with LOCK:
            job["lines"] = cloud_urls(c["remote"], c["paths"])


def cloud_send(remote, items, to):
    """items: [{"path", "dir", "name"}]. to: 'pc' (bajar a tu PC) o 'ps5' (a la consola, como cualquier enlace)."""
    _remote_ok(remote)
    files = [x["path"] for x in items if not x.get("dir")]
    dirs = [x["path"] for x in items if x.get("dir")]
    out = {"jobs": 0, "copy": bool(dirs)}
    with LOCK:
        dest = SETTINGS.get("dest") or "/data/homebrew"
    if files:
        c = {"remote": remote, "paths": files}
        job = new_job(cloud_urls(remote, files), "", dest, where="keep" if to == "pc" else None,
                      keep_steps="dl" if to == "pc" else "")
        job["cloud"] = c
        job["title"] = "☁️ %s: %s" % (remote, posixpath.basename(files[0]) + (" y %d más" % (len(files) - 1) if len(files) > 1 else ""))
        with WAKE:
            JOBS.append(job)
            WAKE.notify_all()
        save_jobs()
        out["jobs"] = 1
    if dirs:
        exe = cloud_exe()
        root = os.path.join(downloads_dir(), "PS5 Directo", "Nube")

        def fn():
            done = []
            for k, d in enumerate(dirs):
                local = os.path.join(root, E.safe_name(posixpath.basename(d)) or "carpeta")
                CLOUD["phase"] = "Bajando la carpeta %s (%d de %d)" % (d, k + 1, len(dirs))
                _rclone_copy(exe, "%s:%s" % (remote, d), local)
                done.append(local)
                if to == "ps5":
                    job = new_job([local], "", dest, title="☁️ %s" % posixpath.basename(d))
                    with WAKE:
                        JOBS.append(job)
                        WAKE.notify_all()
                    save_jobs()
            return ("Carpetas en tu PC: %s" % ", ".join(done)) + (" — y en la cola para la consola" if to == "ps5" else "")
        _cloud_bg("Bajando carpetas de la nube…", fn)
    return out


def _rclone_copy(exe, src, dst):
    import subprocess
    os.makedirs(dst, exist_ok=True)
    p = subprocess.Popen([exe, "copy", src, dst, "--stats", "1s", "--use-json-log", "--stats-log-level", "NOTICE",
                          "--transfers", "4"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         creationflags=F.NO_WINDOW)
    last = ""
    for raw in p.stderr:
        ln = raw.decode("utf-8", "replace").strip()
        try:
            j = json.loads(ln)
        except Exception:
            last = ln or last
            continue
        st = j.get("stats") or {}
        if st.get("totalBytes"):
            CLOUD["pct"] = int(st.get("bytes", 0) * 100 / st["totalBytes"])
        if j.get("level") == "error":
            last = j.get("msg") or last
    if p.wait() != 0:
        raise E.PipeError("rclone no pudo bajar %s: %s" % (src, last[:300]))


SYS_HIST = []         # [(t, cpu_temp, title_id)] para la grafica de la pestana Sistema
TEMP_FILE = os.path.join(DATA_DIR, "temperaturas.json")
NOWPLAY = {"tid": "", "name": "", "fps": None, "t": 0, "src": ""}
SYS_T = [0.0]


def _title_name(tid, extra=None):
    if not tid:
        return ""
    if extra and extra.get(tid):
        return extra[tid]
    t = (lib_load().get("titles") or {}).get(tid) or {}
    return t.get("title") or tid


def _temps_load():
    try:
        with open(TEMP_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"titles": {}, "hist": []}


def sys_sample(ex=None):
    """Cada minuto, con ps5-exporter cargado: apunta la temperatura y que titulo estaba abierto, para la grafica y
    para saber que titulo calienta mas la consola. Tambien rellena «Ahora en la consola»."""
    ip = cfg_snapshot().get("ps5_ip") or ""
    if ex is None:
        if not ip or not _port_open(ip, F.EXPORTER_PORT, 1.0):
            NOWPLAY.update(tid="", name="", fps=None, src="")
            return None
        try:
            ex = F.exporter_summary(F.exporter_metrics(ip))
        except Exception:
            return None
    tid = ex.get("running") or ""
    NOWPLAY.update(tid=tid, name=_title_name(tid, ex.get("smp_names")), fps=ex.get("fps"), t=time.time(), src="exporter")
    temp = ex.get("cpu_temp")
    if temp is None:
        return ex
    now = int(time.time())
    with LOCK:
        if not SYS_HIST or now - SYS_HIST[-1][0] >= 50:
            SYS_HIST.append((now, temp, tid))
            del SYS_HIST[:-1440]
            d = _temps_load()
            d["hist"] = [h for h in d.get("hist", []) if now - h[0] < 7 * 86400][-10080:] + [[now, temp, tid]]
            if tid:
                a = d.setdefault("titles", {}).setdefault(tid, {"n": 0, "sum": 0.0, "max": 0.0, "name": ""})
                a["n"] += 1
                a["sum"] += temp
                a["max"] = max(a["max"], temp)
                a["name"] = NOWPLAY["name"] or a.get("name") or tid
                if ex.get("watts"):
                    a["wsum"] = a.get("wsum", 0.0) + ex["watts"]
            try:
                _save_json(TEMP_FILE, d)
            except Exception:
                pass
    return ex


def sys_state():
    cfg = cfg_snapshot()
    ip = cfg.get("ps5_ip") or ""
    host = E.clean_host(ip)[0] if ip else ""
    out = {"overlay": _port_open(ip, 8585, 1.2) if ip else False,
           "overlay_url": "http://%s:8585" % host if ip else "",
           "zftpd_url": "http://%s:8888" % host if ip else "",
           "exporter": bool(ip) and _port_open(ip, F.EXPORTER_PORT, 1.2)}
    if out["exporter"]:
        try:
            ex = F.exporter_summary(F.exporter_metrics(ip))
            out.update({k: v for k, v in ex.items() if k != "smp_names"})
            sys_sample(ex)
            out["running_name"] = NOWPLAY["name"]
        except Exception as e:
            out["exporter_error"] = str(e)
    z = F.Zftpd(ip)
    out["zftpd"] = z.alive() if ip else False
    if out["zftpd"] and out.get("cpu_temp") is None:
        st = z.stats()
        out.update({k: v for k, v in st.items() if out.get(k) is None})
        if st.get("cpu_temp") is not None:
            with LOCK:
                SYS_HIST.append((int(time.time()), st["cpu_temp"], ""))
                del SYS_HIST[:-1440]
    d = _temps_load()
    hist = [tuple(h) for h in d.get("hist", []) if time.time() - h[0] < 6 * 3600]
    with LOCK:
        live = [h for h in SYS_HIST if time.time() - h[0] < 6 * 3600]
    seen = {h[0] for h in hist}
    hist = sorted(hist + [h for h in live if h[0] not in seen])
    out["hist"] = [[h[0], h[1], h[2] if len(h) > 2 else ""] for h in hist][-720:]
    names = {}
    for h in out["hist"]:
        if h[2] and h[2] not in names:
            names[h[2]] = _title_name(h[2])
    out["hist_names"] = names
    hot = []
    for tid, a in (d.get("titles") or {}).items():
        if a.get("n", 0) >= 5:
            hot.append({"tid": tid, "name": a.get("name") or tid, "avg": round(a["sum"] / a["n"], 1),
                        "max": a.get("max"), "minutes": a["n"],
                        "watts": round(a["wsum"] / a["n"]) if a.get("wsum") else None})
    out["hot"] = sorted(hot, key=lambda x: -x["avg"])[:10]
    return out


def transfer_worker():
    while True:
        with WAKE:
            job = None
            while job is None:
                hold = sched_wait()
                if hold > 0:
                    WAKE.wait(min(hold, 30))
                    continue
                for j in JOBS:
                    if j["status"] == "pendiente":
                        job = j
                        break
                if job is None:
                    WAKE.wait()
            cancel = CANCELS.setdefault(job["id"], threading.Event())
            cancel.clear()
            job["status"] = "comprobando"
            F.CHILD.job = job["id"]
        try:
            if job.get("where") == "zdl":
                zftpd_job(job, cancel)
                save_jobs()
                continue
            if job.get("cloud"):
                cloud_fix_job(job)
            if any(is_torrent(x) for x in job["lines"]):
                torrent_stage(job, cfg_snapshot(), cancel)
            srcs = probe_job(job, cancel)
            sets = split_into_sets(srcs)
            if len(sets) > 1:
                jlog(job, "Detectados %d archivos distintos: los separo en varios trabajos" % len(sets))
                with LOCK:
                    idx = JOBS.index(job)
                    for k, extra in enumerate(sets[1:], 1):
                        nj = new_job([s.original for s in extra], job["password"], job["dest"],
                                     where=job.get("where"), raw=job.get("raw"), fmt=job.get("fmt"),
                                     comp=job.get("comp"), pcdir=job.get("pcdir") or "",
                                     keep_steps=job.get("keep_steps") or "")
                        nj["fmt_set"] = job.get("fmt_set", False)
                        nj["checks"] = {k: v for k, v in (job.get("checks") or {}).items()
                                        if k in {x.name.lower() for x in extra}}
                        nj["sources"] = describe(extra)
                        nj["title"] = E.display_name(extra[0].name)
                        JOBS.insert(idx + k, nj)
                    job["lines"] = [s.original for s in sets[0]]
                srcs = sets[0]
            setj(job, sources=describe(srcs), title=job["title"] or E.display_name(srcs[0].name))
            save_jobs()
            if job.get("where") == "keep":
                out = keep_on_pc_job(job, srcs, cancel)
                setj(job, status="hecho", result=[out], speed=0.0, conv_text="",
                     phase="Listo en tu PC: %s" % out)
                jlog(job, "Terminado: %s" % out)
                record_history(job)
                notify("ok", "Listo en tu PC: %s" % (job.get("title") or os.path.basename(out)), out,
                       {"tab": "Library", "view": "pc"})
                if not COL["busy"]:
                    threading.Thread(target=collection_scan, daemon=True).start()
                save_jobs()
                continue
            info = transfer_job(job, srcs, cancel)
            setj(job, info=info, status="en cola para descomprimir",
                 phase="Enviado. Esperando turno para descomprimir")
            jlog(job, "Envio completo")
            with WAKE:
                EXTRACT_Q.append(job["id"])
                WAKE.notify_all()
        except NeedsDecision:
            setj(job, status="pausado", phase="Ya está en la consola: elige qué hacer", speed=0.0)
            ask = job.get("ask") or {}
            notify("warn", "Ya está en la PS5: %s" % (job.get("title") or ask.get("tid") or ""),
                   "Elige en la cola si lo envías igualmente, lo reemplazas o lo cancelas.",
                   {"tab": "Transfers", "job": job["id"]})
        except E.Cancelled:
            stopped(job)
        except Exception as e:
            msg = str(e)
            if "perdida" in msg:
                free = free_space(E.BFpilot(cfg_snapshot()))
                if free is not None and free < margin_bytes():
                    msg += " -- La PS5 solo tiene %s libres: seguramente se lleno el disco." % human(free)
            setj(job, status="error", error=msg, phase="Error", auto_retry=is_conn_lost(msg))
            jlog(job, "ERROR: %s" % msg)
            record_history(job)
            traceback.print_exc()
        save_jobs()


def extract_worker():
    while True:
        with WAKE:
            while not EXTRACT_Q:
                WAKE.wait()
            job = find(EXTRACT_Q.pop(0))
        if job is None or job["status"] != "en cola para descomprimir":
            continue
        cancel = CANCELS.setdefault(job["id"], threading.Event())
        F.CHILD.job = job["id"]
        try:
            placed = extract_job(job, job["info"], cancel)
            try:
                check_compat(job, placed)
            except Exception as e:
                jlog(job, "No pude comprobar el firmware: %s" % e)
            is_game = (job.pop("_placed_game", False)
                       or any(p.endswith("-app") or p.lower().endswith(E.IMAGE_EXTS) for p in placed)) \
                and (job.get("dest", "").startswith("/data/homebrew") or "/homebrew" in job.get("dest", ""))
            is_pkg = any(p.lower().endswith(".pkg") for p in placed)
            if is_pkg:
                jlog(job, "Es un .fpkg: instálalo desde /data/pkg con «Instalar en la consola» (Biblioteca o Archivos "
                          "de la consola, con zftpd) o con el instalador de paquetes que uses")
            if is_game and job.get("alembic") and placed:
                placed = alembic_convert(job, placed, cancel) or placed
            if is_game and not smp_present(cfg_snapshot()):
                jlog(job, "AVISO: no veo ShadowMountPlus en la consola. Sin él, lo que está en /data/homebrew no sale "
                          "en la pantalla de inicio: cárgalo (Descubrir › Herramientas, o Payload Manager) y en unos "
                          "segundos aparecerá.")
                notify("warn", "Copiado, pero falta ShadowMountPlus",
                       "%s está en la PS5, pero sin ShadowMountPlus no sale en la pantalla de inicio. Cárgalo y "
                       "aparecerá solo." % (job.get("title") or ""), {"tab": "Transfers", "job": job["id"]})
            setj(job, status="hecho", result=placed, extract_text="",
                 phase=("Listo en /data/pkg: instálalo con el instalador de paquetes" if is_pkg
                        else "Listo. ShadowMountPlus lo añadirá al inicio en unos segundos" if is_game
                        else "Listo. Copiado en la PS5"))
            jlog(job, "Terminado")
            record_history(job)
        except NeedsDecision:
            setj(job, status="pausado", phase="Ya está en la consola: elige qué hacer", speed=0.0)
            notify("warn", "Ya está en la PS5: %s" % (job.get("title") or ""),
                   "Elige en la cola si lo colocas igualmente, reemplazas la que hay o cancelas.",
                   {"tab": "Transfers", "job": job["id"]})
        except E.Cancelled:
            stopped(job)
        except Exception as e:
            msg = str(e)
            if "perdida" in msg:
                free = free_space(E.BFpilot(cfg_snapshot()))
                if free is not None and free < margin_bytes():
                    msg += " -- La PS5 solo tiene %s libres: seguramente se lleno el disco." % human(free)
            setj(job, status="error", error=msg, phase="Error", auto_retry=is_conn_lost(msg))
            jlog(job, "ERROR: %s" % msg)
            record_history(job)
            traceback.print_exc()
        save_jobs()


# ---------------------------------------------------------------------------
# Firmware: avisar de juegos que tu PS5 no podra arrancar
# ---------------------------------------------------------------------------

def check_compat(job, placed):
    if job.get("raw"):
        return
    folders = [p for p in placed or [] if not p.lower().endswith(E.IMAGE_EXTS + (".pkg",))]
    if not folders:
        return
    cfg = cfg_snapshot()
    f = E.Ftp(cfg).connect()
    try:
        for p in folders:
            try:
                facts = F.inspect_remote_game(f, p)
            except ftplib.all_errors:
                continue
            if not facts.get("tid"):
                continue
            remember_title(facts["tid"], facts.get("title"), facts.get("need_fw"), facts.get("fw_param"),
                           extra={"backport": facts.get("backport")})
            check_fw_warning(job, facts["tid"], facts.get("title"), facts.get("need_fw"), facts.get("fw_param"),
                             facts.get("backport"))
    finally:
        try:
            f.quit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Biblioteca: los juegos y aplicaciones de la consola, con portada y firmware
# ---------------------------------------------------------------------------

LIB = {"busy": False, "done": 0, "total": 0, "current": "", "items": None, "error": "", "t": 0}
LIB_CANCEL = threading.Event()
LZ4_NAME_RE = re.compile(r"(?i)(^|[-_. \[(])lz4([-_. \])]|$)")
LIB_IMAGE_RE = re.compile(r"\.(ffpkg|exfat|ffpfsc|ffpfs|ufs2?)$", re.I)


GU_TID = "PPSA99440"     # PS5 Game Updater (theghostonline): su web va en el puerto 7920


def gu_status():
    """¿Esta PS5 Game Updater en la consola, bien colocado y en marcha?"""
    cfg = cfg_snapshot()
    out = {"present": False, "running": False, "url": "", "fix_from": "", "problem": ""}

    def look(f):
        names = {n: v for n, v in E.ftp_listdir(f, "/data/homebrew")}
        if GU_TID in names and names[GU_TID]["type"] == "dir":
            sub = {n for n, _ in E.ftp_listdir(f, "/data/homebrew/" + GU_TID)}
            out["present"] = "sce_sys" in sub
            if not out["present"]:
                out["problem"] = "La carpeta /data/homebrew/%s no tiene sce_sys: está incompleta." % GU_TID
        for n, v in names.items():
            if n == GU_TID or GU_TID not in n.upper() or v["type"] != "dir":
                continue
            p = "/data/homebrew/" + n
            sub = {k: w for k, w in E.ftp_listdir(f, p)}
            if "sce_sys" in sub:
                out["fix_from"] = p
            else:
                inner = [k for k, w in sub.items() if w["type"] == "dir"]
                for k in inner:
                    if "sce_sys" in {x for x, _ in E.ftp_listdir(f, p + "/" + k)}:
                        out["fix_from"] = p + "/" + k
                        break
            if out["fix_from"]:
                out["problem"] = ("Está en %s, pero tiene que estar en /data/homebrew/%s para que funcione."
                                  % (out["fix_from"], GU_TID))
                break
        port = 7920
        try:
            raw = F.ftp_read(f, "/data/GameUpdater/engine.port", 0, 32).decode("ascii", "ignore").strip()
            if raw.isdigit():
                port = int(raw)
        except Exception:
            pass
        return port
    try:
        port = E.Ftp(cfg).call(look)
    except Exception as e:
        if conn_error_text(e) == str(e):   # sin conexion ya lo dice la Biblioteca: aqui no se repite
            out["problem"] = "No pude mirarlo: %s" % e
        return out
    out["url"] = "http://%s:%d/" % (cfg["ps5_ip"], port)
    try:
        s = socket.create_connection((cfg["ps5_ip"], port), timeout=1.5)
        s.close()
        out["running"] = True
    except OSError:
        pass
    return out


def library_bases(bf, f):
    if bf.alive():
        try:
            return [b for b in game_bases(bf) if b.endswith("/homebrew") or b == "/data/homebrew"]
        except Exception:
            pass
    out = ["/data/homebrew"]
    for b in ["/mnt/usb%d/homebrew" % i for i in range(4)] + ["/mnt/ext%d/homebrew" % i for i in range(2)]:
        try:
            E.ftp_listdir(f, b)
            out.append(b)
        except ftplib.all_errors:
            pass
    return out


# ---------------------------------------------------------------------------
# Ordenar nombres: las imagenes (.ffpkg, .exfat, .ffpfs, .ffpfsc) y los .pkg con un nombre claro,
# «Titulo-PPSA01234-EU», sacado del propio param.json (sin bases de datos externas). Nunca carpetas: muchas
# aplicaciones y emuladores tienen que seguir llamandose como su Title ID. Idea de pristmc-code/game-library-renamer.
# ---------------------------------------------------------------------------

REN_LOG = os.path.join(DATA_DIR, "renombrados.json")
REN_SKIP_RX = re.compile(r"(?i)patch|backport|update|upd[_-]|dlc")


def ren_clean(t):
    import unicodedata
    t = unicodedata.normalize("NFKD", str(t or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[™®©]", "", t)
    t = re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_")
    return re.sub(r"_+", "_", t)


def ren_target(title, tid, region, ext):
    """Nombre propuesto. ShadowMountPlus monta en /mnt/shadowmnt/<nombre>_<hash>: el nombre sin extension no puede
    pasar de 63 caracteres (58 en .ffpfsc, que va en pfsc/)."""
    tail = "-" + tid.upper() + ("-" + region if region else "")
    base = ren_clean(title) or tid.upper()
    if base.upper() == tid.upper():
        base, tail = tid.upper(), ("-" + region if region else "")
    limit = 58 if ext == "ffpfsc" else 63 if ext != "pkg" else 200
    base = base[:max(8, limit - len(tail))].rstrip("_")
    return base + tail + "." + ext


def rename_plan():
    with LOCK:
        lib = list(((LIB.get("items") or {}).get("items")) or [])
    if not lib:
        raise E.PipeError("Primero lee la Biblioteca (botón «Actualizar lista»).")
    titles = lib_load().get("titles") or {}
    cands = []
    for it in lib:
        if it.get("kind") == "image" and it.get("path", "").startswith(("/data/", "/mnt/usb", "/mnt/ext")):
            cands.append({"path": it["path"], "name": it["name"], "tid": it.get("tid") or "", "title": it.get("title") or "",
                          "region": it.get("region") or "", "ext": (it.get("ext") or it["name"].rsplit(".", 1)[-1]).lower(),
                          "mounted": bool(it.get("mounted"))})
    # .pkg en /data/pkg y en las carpetas de la Biblioteca (el titulo, si ya se conoce por su Title ID)
    cfg = cfg_snapshot()
    dirs = sorted({posixpath.dirname(c["path"]) for c in cands} | {"/data/pkg"})
    listing = {}

    def work(f):
        for d in dirs:
            try:
                listing[d] = {n for n, v in E.ftp_listdir(f, d)}
                for n, v in E.ftp_listdir(f, d):
                    if v["type"] == "file" and n.lower().endswith(".pkg"):
                        tid = E.title_id(n) or ""
                        t = titles.get(tid.upper()) or {}
                        cands.append({"path": d.rstrip("/") + "/" + n, "name": n, "tid": tid, "title": t.get("title") or "",
                                      "region": "", "ext": "pkg", "mounted": False})
            except ftplib.all_errors:
                listing[d] = set()
    E.Ftp(cfg).call(work)
    out, targets = [], {}
    for c in cands:
        d = posixpath.dirname(c["path"])
        it = dict(c, new="", status="", why="")
        if not c["tid"] or not c["title"]:
            it.update(status="skip", why="No sé su título (léelo antes en la Biblioteca)" if c["tid"] else "Sin Title ID")
        elif c["ext"] == "pkg" and REN_SKIP_RX.search(c["name"]):
            it.update(status="skip", why="Parche o backport: se deja como está")
        else:
            new = ren_target(c["title"], c["tid"], c["region"], c["ext"])
            it["new"] = new
            if new == c["name"]:
                it["status"] = "ok"
            elif new in listing.get(d, set()) or (d + "/" + new) in targets:
                it.update(status="conflict", why="Ya hay otro con ese nombre")
            else:
                it["status"] = "ready"
                targets[d + "/" + new] = c["path"]
        stem = c["name"].rsplit(".", 1)[0]
        if c["ext"] != "pkg" and len(stem) > (58 if c["ext"] == "ffpfsc" else 63):
            it["too_long"] = True       # tal como esta, ShadowMountPlus no lo puede montar
        out.append(it)
    order = {"ready": 0, "conflict": 1, "skip": 2, "ok": 3}
    out.sort(key=lambda x: (order.get(x["status"], 9), (x["title"] or x["name"]).lower()))
    try:
        with open(REN_LOG, encoding="utf-8") as fh:
            last = json.load(fh)
    except (OSError, ValueError):
        last = []
    return {"items": out, "undo": len(last[-1]["items"]) if last else 0}


def rename_apply(paths=None, undo=False):
    cfg = cfg_snapshot()
    try:
        with open(REN_LOG, encoding="utf-8") as fh:
            log = json.load(fh)
    except (OSError, ValueError):
        log = []
    if undo:
        if not log:
            raise E.PipeError("No hay nada que deshacer.")
        pairs = [(x["to"], x["from"]) for x in reversed(log[-1]["items"])]
    else:
        plan = {x["path"]: x for x in rename_plan()["items"] if x["status"] == "ready"}
        pairs = [(p, posixpath.dirname(p) + "/" + plan[p]["new"]) for p in (paths or []) if p in plan]
        if not pairs:
            raise E.PipeError("No hay nada marcado para renombrar.")
    done, errs = [], []

    def work(f):
        for a, b in pairs:
            d = posixpath.dirname(b)
            try:
                if posixpath.basename(b) in {n for n, _ in E.ftp_listdir(f, d)}:
                    errs.append("%s: ya existe" % posixpath.basename(b))
                    continue
                f.rename(a, b)
                done.append({"from": a, "to": b})
            except ftplib.all_errors as e:
                errs.append("%s: %s" % (posixpath.basename(a), e))
    E.Ftp(cfg).call(work)
    if undo:
        log = log[:-1]
    elif done:
        log.append({"t": time.time(), "items": done})
    try:
        with open(REN_LOG, "w", encoding="utf-8") as fh:
            json.dump(log[-20:], fh, ensure_ascii=False, indent=1)
    except OSError:
        pass
    # la Biblioteca ya sabe los nombres nuevos (sin volver a leerlo todo)
    with LOCK:
        m = {x["from"]: x["to"] for x in done}
        for it in (((LIB.get("items") or {}).get("items")) or []):
            if it.get("path") in m:
                it["path"] = m[it["path"]]
                it["name"] = posixpath.basename(it["path"])
                it.pop("mounted", None)
    if done:
        print("Renombrados %d en la consola%s" % (len(done), " (deshecho)" if undo else ""))
    return {"done": len(done), "errors": errs}


def library_scan(refresh=False):
    cfg = cfg_snapshot()
    bf = E.BFpilot(cfg)
    alive = bool(bf.alive())
    cache = lib_load()
    ents = cache["entries"]
    titles = cache["titles"]
    conn = {"f": E.Ftp(cfg).connect()}
    ROOT_NAMES = {"system_data", "system_ex", "dev", "mnt", "preinst", "user"}

    def listdir(path):
        """Con BFpilot su listado (fiable); si no, FTP. Mismo formato que E.ftp_listdir."""
        if alive:
            try:
                return [(e["name"], {"type": "dir" if e.get("dir") else "file", "size": e.get("size") or 0,
                                     "mtime": e.get("mtime"), "mode": e.get("mode")}) for e in bf.listdir(path)]
            except E.PipeError:
                raise ftplib.error_perm("550 no existe")
        return E.ftp_listdir(ftp(), path)

    def ftp():
        try:
            conn["f"].voidcmd("NOOP")
        except Exception:
            try:
                conn["f"].close()
            except Exception:
                pass
            conn["f"] = E.Ftp(cfg).connect()
        return conn["f"]
    items = []
    try:
        work = []
        for base in library_bases(bf, ftp()):
            try:
                listing = listdir(base)
            except ftplib.all_errors:
                continue
            if len(ROOT_NAMES & {n for n, _ in listing}) >= 3:
                print("Biblioteca: %s devolvio la raiz de la consola, la salto" % base)
                continue                       # servidor que lista otra carpeta: no es la buena
            for n, x in listing:
                work.append((base, n, x))
        with LOCK:
            LIB.update(total=len(work), done=0)
        # imagenes montadas por ShadowMountPlus: /mnt/shadowmnt/<imagen>_<hash> (y pfsc/...). Dentro estan
        # su param.json y su portada, asi que las imagenes tambien tienen nombre y portada.
        mounts = []
        for mb in ("/mnt/shadowmnt", "/mnt/shadowmnt/pfsc"):
            try:
                ml = listdir(mb)
            except ftplib.all_errors:
                try:
                    ml = E.ftp_listdir(ftp(), mb)      # BFpilot no siempre lista los puntos de montaje
                except ftplib.all_errors:
                    continue
            for n, v in ml:
                if v["type"] != "dir" or n == "pfsc":
                    continue
                mp = mb + "/" + n
                c = ents.get("mount:" + mp)
                if c and c.get("mtime") == v.get("mtime") and (c.get("facts") or {}).get("v") == 5:
                    facts = c["facts"]
                else:
                    try:
                        try:
                            sub = listdir(mp)
                        except ftplib.all_errors:
                            sub = E.ftp_listdir(ftp(), mp)
                        if not any(k == "sce_sys" and w["type"] == "dir" for k, w in sub):
                            continue
                        facts = E.Ftp(cfg).call(lambda fx: F.inspect_remote_game(fx, mp, sub))
                        if any(k.lower().startswith(("ampr_assets", "ampr_emu", "ampr_zstd")) for k, w in sub):
                            facts["lz4"] = True
                            if any(k.lower().startswith("ampr_zstd") for k, w in sub):
                                facts["pack"] = "zstd"    # ampr-emu-ng (zstd), no LZ4
                        facts["v"] = 5 if facts.get("tid") else 0
                    except ftplib.all_errors:
                        continue
                    ents["mount:" + mp] = {"mtime": v.get("mtime"), "facts": facts}
                mounts.append({"path": mp, "key": n.rsplit("_", 1)[0].lower(), "facts": facts})

        def mount_for(fname, tid):
            low = fname.lower()
            stem = low.rsplit(".", 1)[0]
            for m in mounts:
                if m["key"] in (low, stem):
                    return m
            if tid:
                for m in mounts:
                    if (m["facts"].get("tid") or "").upper() == tid.upper():
                        return m
            return None
        for i, (base, name, x) in enumerate(work):
            if LIB_CANCEL.is_set():
                raise E.Cancelled()
            path = base + "/" + name
            with LOCK:
                LIB.update(done=i, current=path)
            drive = "USB" if base.startswith("/mnt/") else "Interno"
            if x["type"] == "dir":
                c = ents.get(path)
                if c and not refresh and c.get("mtime") == x.get("mtime") and (c.get("facts") or {}).get("v") == 5:
                    facts = c["facts"]
                else:
                    try:
                        listing = listdir(path)
                        if any(n == "sce_sys" and v["type"] == "dir" for n, v in listing):
                            # una conexion por juego (como las portadas): leer el eboot.bin a medias deja
                            # algunas conexiones descolocadas y fallaban las lecturas de los siguientes
                            facts = E.Ftp(cfg).call(lambda fx: F.inspect_remote_game(fx, path, listing))
                            if not facts.get("tid") and facts.get("icon"):
                                facts = E.Ftp(cfg).call(lambda fx: F.inspect_remote_game(fx, path, listing))
                            # carpeta empaquetada en LZ4 (PS5 Dump Forge): lleva ampr_assets.index
                            if any(n.lower().startswith(("ampr_assets", "ampr_emu", "ampr_zstd")) for n, v in listing):
                                facts["lz4"] = True
                                if any(n.lower().startswith("ampr_zstd") for n, v in listing):
                                    facts["pack"] = "zstd"
                        else:
                            facts = {"incomplete": True}
                        facts["v"] = 5 if (facts.get("tid") or facts.get("incomplete") or not facts.get("icon")) else 0
                    except ftplib.all_errors as e:
                        facts = {"error": str(e)}
                    c = {"mtime": x.get("mtime"), "facts": facts}
                    ents[path] = c
                it = dict(facts)
                it.update(path=path, name=name, kind="folder", drive=drive, size=c.get("size"),
                          cover=bool(facts.get("icon")))
                if not it.get("incomplete") and LZ4_NAME_RE.search(name):
                    it["lz4"] = True
                if not it.get("incomplete") and re.search(r"(?i)(^|[-_. \[(])zstd([-_. \])]|$)", name):
                    it["lz4"] = True
                    it["pack"] = "zstd"
                if not it.get("tid") and not it.get("incomplete"):
                    # sin param.json legible: el Title ID del nombre y el titulo si ya lo conociamos
                    tn = E.title_id(name)
                    if tn:
                        it["tid"] = tn
                        known = titles.get(tn.upper()) or {}
                        it["title"] = it.get("title") or known.get("title")
                        it["need_fw"] = it.get("need_fw") or known.get("need_fw")
                if it.get("tid"):
                    t = titles.setdefault(it["tid"].upper(), {})
                    if it.get("title"):
                        t["title"] = it["title"]
                    if it.get("need_fw"):
                        t["need_fw"] = it["need_fw"]
                items.append(it)
            elif LIB_IMAGE_RE.search(name):
                tid = E.title_id(name)
                m = mount_for(name, tid)
                if m and not tid:
                    tid = m["facts"].get("tid")
                t = titles.get((tid or "").upper(), {})
                mf = m["facts"] if m else {}
                it = {"path": path, "name": name, "kind": "image", "drive": drive,
                      "lz4": bool(LZ4_NAME_RE.search(name)) or bool(mf.get("lz4")),
                      "pack": mf.get("pack") or ("zstd" if re.search(r"(?i)(^|[-_. \[(])zstd([-_. \])]|$)", name) else ""),
                      "ext": LIB_IMAGE_RE.search(name).group(1).lower(), "size": x.get("size"),
                      "tid": tid, "title": mf.get("title") or t.get("title"),
                      "version": mf.get("version"), "need_fw": mf.get("need_fw") or t.get("need_fw"),
                      "region": mf.get("region") or "",
                      "backport": mf.get("backport") or t.get("backport"),
                      "cover": bool(mf.get("icon") or t.get("cover"))}
                if m and mf.get("icon"):
                    it["cover_path"] = m["path"]
                if m:
                    it["mounted"] = m["path"]
                if tid and it["title"]:
                    tt = titles.setdefault(tid.upper(), {})
                    tt["title"] = it["title"]
                    if it.get("need_fw"):
                        tt["need_fw"] = it["need_fw"]
                items.append(it)
        # portadas y nombres que faltan: la PS5 guarda los de cada aplicacion registrada en su menu en
        # /user/appmeta/<Title ID>/ (icon0.png y param.json). Sirve para imagenes y carpetas sin param.json.
        try:
            meta_list = E.Ftp(cfg).call(lambda fx: E.ftp_listdir(fx, "/user/appmeta"))
        except ftplib.all_errors:
            meta_list = []
        meta_ids = {n.upper() for n, v in meta_list if v["type"] == "dir"}
        # aplicaciones instaladas en la consola (PS5 y PS4) que no son copias de /data/homebrew
        have = {(it.get("tid") or "").upper() for it in items}
        try:
            user_app = {n.upper() for n, v in listdir("/user/app")}
        except ftplib.all_errors:
            user_app = set()
        # lo instalado en el SSD M.2 o en un disco de almacenamiento ampliado está en /mnt/extN/user/app
        ext_app = {}
        try:
            xbs = sorted({"/mnt/" + n for n, v in listdir("/mnt") if re.match(r"(?i)^ext\d+$", n)} |
                         {"/mnt/ext0", "/mnt/ext1"})
        except ftplib.all_errors:
            xbs = ["/mnt/ext0", "/mnt/ext1", "/mnt/ext2"]
        for xb in xbs:
            try:
                for n2, v2 in listdir(xb + "/user/app"):
                    if v2["type"] == "dir":
                        ext_app.setdefault(n2.upper(), (xb + "/user/app/" + n2, n2))
            except ftplib.all_errors:
                try:
                    for n2, v2 in E.ftp_listdir(ftp(), xb + "/user/app"):
                        if v2["type"] == "dir":
                            ext_app.setdefault(n2.upper(), (xb + "/user/app/" + n2, n2))
                except ftplib.all_errors:
                    pass
        for n, v in sorted(meta_list):
            tidu = n.upper()
            if v["type"] != "dir" or not re.match(r"^(PPSA|CUSA)\d{5}$", tidu) or tidu in have:
                continue
            mp = "/user/appmeta/" + n
            c = ents.get("installed:" + tidu)
            if not (c and not refresh and c.get("mtime") == v.get("mtime") and (c.get("facts") or {}).get("v") == 4):
                try:
                    def look(fx, mp=mp, tidu=tidu):
                        names = {k for k, _ in E.ftp_listdir(fx, mp)}
                        out = {"icon": "icon0.png" in names, "v": 4}
                        if "param.json" in names:
                            raw = F.ftp_read(fx, mp + "/param.json", 0, 512 * 1024)
                            out.update(F.param_facts(raw.decode("utf-8-sig", "replace")))
                            out["need_fw"] = out.get("fw_param")
                        elif "param.sfo" in names:
                            out.update(F.sfo_facts(F.ftp_read(fx, mp + "/param.sfo", 0, 256 * 1024)))
                        return out
                    facts = E.Ftp(cfg).call(look)
                except ftplib.all_errors:
                    continue
                c = ents["installed:" + tidu] = {"mtime": v.get("mtime"), "facts": facts}
            mf = c["facts"]
            if mf.get("extra"):
                continue
            if mf.get("title"):
                titles.setdefault(tidu, {})["title"] = mf["title"]
            ps4 = tidu.startswith("CUSA")
            # si está en el SSD M.2 o en un disco ampliado, esa es la de verdad: en /user/app queda solo un resto pequeño
            if tidu in ext_app:
                ipath, idrive = ext_app[tidu][0], "Ampliado (M.2/USB)"
            elif tidu in user_app:
                ipath, idrive = "/user/app/" + n, "Interno"
            else:
                ipath, idrive = mp, "Interno"
            items.append({"path": ipath, "name": n, "kind": "installed",
                          "platform": "PS4" if ps4 else "PS5", "drive": idrive, "tid": tidu,
                          "title": mf.get("title"), "version": mf.get("version"), "need_fw": mf.get("need_fw"),
                          "cover": bool(mf.get("icon")), "cover_path": mp if mf.get("icon") else None,
                          "size": (ents.get(ipath) or ents.get("/user/app/" + n) or {}).get("size")})
        need = [it for it in items if it.get("tid") and not it.get("incomplete") and it["kind"] != "installed"
                and (not it.get("cover") or not it.get("title"))]
        if need:
            for it in need:
                tidu = it["tid"].upper()
                if tidu not in meta_ids:
                    continue
                mp = "/user/appmeta/" + tidu
                c = ents.get("appmeta:" + tidu)
                if not (c and (c.get("facts") or {}).get("v") == 4 and not refresh):
                    try:
                        def look(fx, mp=mp):
                            names = {n for n, _ in E.ftp_listdir(fx, mp)}
                            out = {"icon": "icon0.png" in names, "v": 4}
                            if "param.json" in names:
                                raw = F.ftp_read(fx, mp + "/param.json", 0, 512 * 1024)
                                out.update(F.param_facts(raw.decode("utf-8-sig", "replace")))
                            return out
                        facts = E.Ftp(cfg).call(look)
                    except ftplib.all_errors:
                        continue
                    c = ents["appmeta:" + tidu] = {"facts": facts}
                mf = c["facts"]
                if mf.get("icon") and not it.get("cover"):
                    it["cover"] = True
                    it["cover_path"] = mp
                if mf.get("title") and not it.get("title"):
                    it["title"] = mf["title"]
                    titles.setdefault(tidu, {})["title"] = mf["title"]
                if mf.get("version") and not it.get("version"):
                    it["version"] = mf["version"]
                # el firmware que pide su param.json (dentro de una imagen no se puede mirar el eboot.bin)
                fwp = mf.get("need_fw") or mf.get("fw_param")
                if fwp and not it.get("need_fw"):
                    it["need_fw"] = fwp
                    it["fw_from_param"] = True
        # tamano de las carpetas (rapido con BFpilot; por FTP seria muy lento)
        if alive:
            todo = [it for it in items if (it["kind"] == "folder" or (it["kind"] == "installed" and re.match(r"^(/mnt/ext\d)?/user/app/", it["path"])))
                    and it.get("size") is None]
            for k, it in enumerate(todo):
                if LIB_CANCEL.is_set():
                    raise E.Cancelled()
                with LOCK:
                    LIB.update(current="Calculando tamaños %d de %d" % (k + 1, len(todo)))
                sz = bf.du(it["path"], timeout=120)
                if sz is not None:
                    it["size"] = sz
                    ents.setdefault(it["path"], {})["size"] = sz
        # quitar de la cache lo que ya no esta
        seen = {it["path"] for it in items} | {"mount:" + m["path"] for m in mounts} | \
            {"appmeta:" + it["tid"].upper() for it in items if it.get("tid")} | \
            {"installed:" + it["tid"].upper() for it in items if it.get("tid")}
        for p in list(ents):
            if p not in seen:
                ents.pop(p, None)
        lib_save(cache)
    finally:
        try:
            conn["f"].quit()
        except Exception:
            pass
    # repetidos: mismo Title ID en dos sitios
    by = {}
    for it in items:
        if it.get("tid") and not it.get("extra"):
            by.setdefault(it["tid"].upper(), []).append(it)
    for lst in by.values():
        if len(lst) > 1:
            for it in lst:
                it["dup"] = [o["path"] for o in lst if o is not it]
    items.sort(key=lambda it: ((it.get("title") or it["name"]).lower(), it["path"]))
    return {"items": items, "bfpilot": alive, "t": time.time(), "mounts": len(mounts)}


def library_start(refresh=False):
    with LOCK:
        if LIB["busy"]:
            return
        LIB.update(busy=True, done=0, total=0, current="", error="")
    LIB_CANCEL.clear()

    def run():
        try:
            res = library_scan(refresh)
            with LOCK:
                LIB["items"] = res
                LIB["t"] = res["t"]
        except E.Cancelled:
            with LOCK:
                LIB["error"] = "Cancelado"
        except Exception as e:
            traceback.print_exc()
            with LOCK:
                LIB["error"] = conn_error_text(e)
        finally:
            with LOCK:
                LIB["busy"] = False
    threading.Thread(target=run, daemon=True).start()


def cover_png(path=None, tid=None, pic=False):
    """Bytes de la portada (icon0.png) de una aplicacion de la consola, guardada en cache. Con pic, su imagen de
    fondo (pic0.png, la que sale detras en el menu de la PS5)."""
    import hashlib
    if tid:
        t = lib_load()["titles"].get(tid.upper()) or {}
        fn = t.get("cover")
        if fn:
            fp = os.path.join(COVER_DIR, os.path.basename(fn))
            if os.path.isfile(fp):
                with open(fp, "rb") as fh:
                    return fh.read()
        return None
    path = fm_clean(path)
    if not (path.startswith("/data/") or path.startswith("/mnt/") or path.startswith("/user/appmeta/")):
        return None
    c = lib_load()["entries"].get(path) or {}
    key = hashlib.sha1(("%s|%s" % (path, c.get("mtime"))).encode("utf-8")).hexdigest()[:20]
    fp = os.path.join(COVER_DIR, key + ("_pic0" if pic else "") + ".png")
    if os.path.isfile(fp):
        with open(fp, "rb") as fh:
            return fh.read()
    name = "pic0.png" if pic else "icon0.png"
    icon = path + ("/" + name if path.startswith("/user/appmeta/") else "/sce_sys/" + name)
    data = E.Ftp(cfg_snapshot()).call(lambda f: F.ftp_read(f, icon, 0, 16 * 1024 * 1024))
    if not data.startswith(b"\x89PNG"):
        return None
    os.makedirs(COVER_DIR, exist_ok=True)
    with open(fp + ".tmp", "wb") as fo:
        fo.write(data)
    os.replace(fp + ".tmp", fp)
    return data


# ---------------------------------------------------------------------------
# Programar la cola, no dormir el PC y apagar/suspender al terminar
# ---------------------------------------------------------------------------

SCHED = {"start": 0, "after": "none", "seen_busy": False, "countdown": 0}


def sched_state():
    with LOCK:
        return {"start": SCHED["start"], "after": SCHED["after"], "countdown": SCHED["countdown"]}


def sched_save():
    with LOCK:
        SETTINGS["sched"] = {"start": SCHED["start"], "after": SCHED["after"]}
    save_settings()


def sched_set(b):
    with WAKE:
        if "start" in b:
            v = str(b.get("start") or "").strip()
            if not v:
                SCHED["start"] = 0
            else:
                m = re.match(r"^(\d{1,2}):(\d{2})$", v)
                if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                    raise E.PipeError("Hora no valida (usa HH:MM)")
                lt = time.localtime()
                t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
                if t <= time.time():
                    t += 86400
                SCHED["start"] = t
        if "after" in b:
            a = b.get("after")
            SCHED["after"] = a if a in ("sleep", "shutdown") else "none"
            SCHED["countdown"] = 0
            SCHED["seen_busy"] = False
        if b.get("cancel_countdown"):
            SCHED.update(after="none", countdown=0, seen_busy=False)
        if b.get("now"):
            SCHED["start"] = 0
        WAKE.notify_all()
    sched_save()
    return sched_state()


# ---------------------------------------------------------------------------
# Copia de seguridad de la consola en el PC (partidas, configuracion y base de datos de aplicaciones)
# ---------------------------------------------------------------------------

BK = {"busy": False, "what": "", "done": 0, "total": 0, "files": 0, "current": "", "error": "", "last": "", "t": 0}
BK_CANCEL = threading.Event()
BK_CONFIG_DIRS = ["/data/shadowmount", "/data/shadowmountplus", "/data/pldmgr", "/data/etaHEN", "/data/GameUpdater",
                  "/data/ps5_autoloader", "/data/kstuff", "/data/overlay", "/data/SMPlusGui", "/data/tailscale"]
BK_APPDB = ["/system_data/priv/mms/app.db", "/system_data/priv/mms/appinfo.db", "/system_data/priv/mms/addcont.db"]
BK_SAVE_DIRS = ("savedata_prospero", "savedata")
BK_MAX_FILE = 200 * 1024 * 1024       # en la configuracion no se copian archivos enormes


def backup_dir(cfg=None):
    d = ((cfg or cfg_snapshot()).get("backup_dir") or "").strip().strip('"')
    if not d and PHONE:
        docs = phone_shared("Documents")
        if docs:
            return os.path.join(docs, "PS5 Directo", "Copias")
    return d or os.path.join(os.path.expanduser("~"), "Documents", "PS5 Directo", "Copias")


def _ftp_walk_sizes(f, root, out, cancel, limit=None):
    """[(ruta, tamano)] de todos los archivos bajo root (vacio si no existe)."""
    try:
        listing = E.ftp_listdir(f, root)
    except ftplib.all_errors:
        return out
    for n, v in listing:
        if cancel.is_set():
            raise E.Cancelled()
        p = root.rstrip("/") + "/" + n
        if v["type"] == "dir":
            _ftp_walk_sizes(f, p, out, cancel, limit)
        elif v["type"] == "file" and (limit is None or (v.get("size") or 0) <= limit):
            out.append((p, v.get("size") or 0))
    return out


def _save_roots(f):
    """Carpetas de partidas de cada usuario de la consola."""
    roots = []
    try:
        users = [n for n, v in E.ftp_listdir(f, "/user/home") if v["type"] == "dir"]
    except ftplib.all_errors:
        return roots
    for u in users:
        try:
            sub = {n for n, v in E.ftp_listdir(f, "/user/home/" + u) if v["type"] == "dir"}
        except ftplib.all_errors:
            continue
        for d in BK_SAVE_DIRS:
            if d in sub:
                roots.append("/user/home/%s/%s" % (u, d))
    return roots


# Partidas de los emuladores que corren en la PS5 (son archivos normales, no partidas de PS5). Donde guarda cada uno,
# segun su codigo (lo mismo que mira PS5Upload): carpetas de datos por orden, la primera que exista.
_USB = range(8)
EMU_SAVES = [
    {"id": "PPSA99249", "name": "Crux2 (PS2)", "subs": ["memcards", "sstates"],
     "outside": ["/data/crux2"] + ["/mnt/usb%d/crux2-data" % i for i in _USB], "in_use": "",
     "installed": ["/data/homebrew/PPSA99249", "/user/app/PPSA99249", "/mnt/ext0/homebrew/PPSA99249",
                   "/mnt/ext1/homebrew/PPSA99249", "/mnt/usb0/homebrew/PPSA99249", "/mnt/usb1/homebrew/PPSA99249"]},
    {"id": "PPSA99203", "name": "PS5SX2 (PS2)", "subs": ["memcards", "savestates"], "loose": (".ps2", ".p2s"),
     "outside": ["/data/PCSX2"], "in_use": "", "installed": []},
    {"id": "PPSA99248", "name": "PSSwanStation (PS1)", "subs": ["data/saves", "data/states"],
     "outside": ["/data/psswanstation"] + ["/mnt/usb%d/PSSwanStation" % i for i in _USB], "in_use": "data",
     "installed": ["/data/homebrew/PPSA99248", "/user/app/PPSA99248", "/mnt/ext0/homebrew/PPSA99248",
                   "/mnt/ext1/homebrew/PPSA99248", "/mnt/usb0/homebrew/PPSA99248", "/mnt/usb1/homebrew/PPSA99248"]},
    {"id": "PPSA99008", "name": "ProsperoEden (Switch)",
     "root": "/data/prosperoeden/user/nand/user/save/0000000000000000", "also": ["PPSA99016"]},
]


def _emu_save(tid, name, folder, subs):
    return {"id": tid, "name": name, "subs": subs, "outside": [folder], "in_use": "", "installed": []}


EMU_SAVES += [
    _emu_save("PPSA97510", "PSXS5 (PS1)", "/data/PSXS5", ["memcards", "states", "saves", "profiles"]),
    _emu_save("PPSA99764", "Porpoise (GameCube / Wii)", "/data/porpoise", ["GC", "Wii/title", "StateSaves", "saves"]),
    _emu_save("PPSA99064", "Mupen64Plus (N64)", "/data/mupen64plus", ["save", "saves", "states"]),
    _emu_save("PPSA99922", "Snes9x (SNES)", "/data/snes9x", ["saves", "sram", "states"]),
    _emu_save("PPSA99921", "Genesis Plus GX (Sega)", "/data/genplus", ["saves", "states"]),
    _emu_save("PPSA99924", "FBNeo (arcade)", "/data/fbneo", ["saves", "states"]),
    _emu_save("PPSA99925", "Beetle PCE (PC Engine)", "/data/pce", ["saves", "states"]),
    _emu_save("PPSA99247", "PSFlycast (Dreamcast)", "/data/homebrew/PPSA99247", ["vmu"]),
    _emu_save("PPSA99169", "RetroArch", "/data/homebrew/PPSA99169", ["savefiles", "savestates"]),
    _emu_save("PPSA68091", "Ruffle (Flash)", "/data/ruffle", ["saves"]),
]
EMU_NAMES = {e["id"]: e["name"] for e in EMU_SAVES}


def _ftp_isdir(f, p):
    try:
        E.ftp_listdir(f, p)
        return True
    except ftplib.all_errors:
        return False


def emu_save_files(f, cancel):
    """[(id del emulador, ruta, tamano)] de las partidas de los emuladores que haya en la consola."""
    out = []
    for e in EMU_SAVES:
        if e.get("root"):
            out += [(e["id"], p, sz) for p, sz in _ftp_walk_sizes(f, e["root"], [], cancel)]
            continue
        base = None
        for d in e["outside"]:
            if _ftp_isdir(f, d) and (not e["in_use"] or _ftp_isdir(f, d + "/" + e["in_use"])):
                base = d
                break
        if base is None:
            base = next((d for d in e["installed"] if _ftp_isdir(f, d)), None)
        if base is None:
            continue
        found = False
        for sub in e["subs"]:
            got = _ftp_walk_sizes(f, base + "/" + sub, [], cancel)
            found |= bool(got)
            out += [(e["id"], p, sz) for p, sz in got]
        if not found and e.get("loose"):
            # PS5SX2 de la tienda: las tarjetas (.ps2) y los estados (.p2s) sueltos; nunca su BIOS ni sus juegos
            try:
                for n, v in E.ftp_listdir(f, base):
                    if v["type"] == "file" and n.lower().endswith(e["loose"]):
                        out.append((e["id"], base + "/" + n, v.get("size") or 0))
            except ftplib.all_errors:
                pass
    return out


def open_titles(max_age=12 * 3600):
    """Titulos que segun el registro de ShadowMountPlus siguen abiertos (arrancados hace menos de max_age)."""
    try:
        with PLAYED_LOCK:
            d = played_load()
        now = time.time()
        return {t for t, ts in (d.get("open") or {}).items() if ts and now - ts < max_age}
    except Exception:
        return set()


def backup_run(sections=None, label="copia", tids=None):
    """Hace la copia en un .zip con fecha. sections: saves, config, appdb, list."""
    import zipfile
    cfg = cfg_snapshot()
    if sections is None:
        sections = [k for k in ("saves", "emu", "config", "appdb") if cfg.get("backup_" + k, True)] + ["list"]
    BK_CANCEL.clear()
    with LOCK:
        BK.update(busy=True, what="backup", done=0, total=0, files=0, current="Buscando qué copiar…", error="")
    out_dir = backup_dir(cfg)
    os.makedirs(out_dir, exist_ok=True)
    name = "PS5-%s-%s.zip" % (label, time.strftime("%Y-%m-%d_%H%M"))
    final = os.path.join(out_dir, name)
    tmp = final + ".part"
    try:
        f = E.Ftp(cfg).connect()
        try:
            todo = []      # (seccion, ruta, tamano)
            if "saves" in sections:
                want = {t.upper() for t in (tids or [])}
                for r in _save_roots(f):
                    if not want:
                        todo += [("saves", p, s) for p, s in _ftp_walk_sizes(f, r, [], BK_CANCEL)]
                        continue
                    try:
                        subs = [n for n, v in E.ftp_listdir(f, r) if v["type"] == "dir" and n.upper()[:9] in want]
                    except ftplib.all_errors:
                        subs = []
                    for n in subs:
                        todo += [("saves", p, s) for p, s in _ftp_walk_sizes(f, r + "/" + n, [], BK_CANCEL)]
                if want and not todo:
                    raise E.PipeError("No encuentro partidas de %s en la consola (¿no lo has jugado todavía?)"
                                      % ", ".join(sorted(want)))
            if "emu" in sections and not tids:
                emu_files = emu_save_files(f, BK_CANCEL)
                todo += [("emu", p, sz) for _, p, sz in emu_files]
                emu_of = {p: i for i, p, _ in emu_files}
            else:
                emu_of = {}
            if "config" in sections:
                for r in BK_CONFIG_DIRS:
                    todo += [("config", p, s) for p, s in _ftp_walk_sizes(f, r, [], BK_CANCEL, BK_MAX_FILE)]
            if "appdb" in sections:
                for p in BK_APPDB:
                    try:
                        sz = f.size(p)
                        todo.append(("appdb", p, sz or 0))
                    except ftplib.all_errors:
                        pass
            with LOCK:
                BK.update(total=sum(x[2] for x in todo), files=len(todo))
            manifest = {"app": "PS5 Directo", "version": VERSION, "date": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "ps5_ip": cfg.get("ps5_ip"), "console_fw": cfg.get("console_fw"), "sections": sections,
                        "files": []}
            done = 0
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                for sec, p, sz in todo:
                    if BK_CANCEL.is_set():
                        raise E.Cancelled()
                    with LOCK:
                        BK["current"] = p
                    arc = "consola" + p
                    try:
                        with z.open(zipfile.ZipInfo(arc, time.localtime()[:6]), "w", force_zip64=True) as dst:
                            f.retrbinary("RETR " + p, dst.write, 1024 * 1024)
                        manifest["files"].append(dict({"section": sec, "path": p, "size": sz},
                                                      **({"emu": emu_of[p]} if p in emu_of else {})))
                    except ftplib.all_errors as e:
                        manifest.setdefault("skipped", []).append({"path": p, "error": str(e)[:200]})
                        try:
                            f.close()
                        except Exception:
                            pass
                        f = E.Ftp(cfg).connect()
                    done += sz
                    with LOCK:
                        BK["done"] = done
                if "list" in sections:
                    items = (lib_load().get("entries") or {})
                    titles = lib_load().get("titles") or {}
                    z.writestr("lista_de_la_consola.json", json.dumps(
                        {"titles": titles, "paths": sorted(k for k in items if not k.startswith(("mount:", "appmeta:")))},
                        ensure_ascii=False, indent=1))
                z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        finally:
            try:
                f.quit()
            except Exception:
                pass
        os.replace(tmp, final)
        keep = int(cfg.get("backup_keep") or 10)
        pre = sorted(x for x in os.listdir(out_dir) if x.startswith("PS5-antes-de-restaurar-") and x.endswith(".zip"))
        olds = sorted(x for x in os.listdir(out_dir) if x.startswith("PS5-copia-") and x.endswith(".zip"))
        for x in (olds[:-keep] if keep > 0 else []) + pre[:-5]:
            try:
                os.remove(os.path.join(out_dir, x))
            except OSError:
                pass
        with LOCK:
            SETTINGS["backup_last"] = time.time()
            BK.update(last=final)
        save_settings()
        nf = len(manifest["files"])
        notify("ok", "Copia de seguridad hecha", "%d archivos (%s) en %s" % (nf, human(done), final), {"tab": "Backup"})
        return final
    except E.Cancelled:
        with LOCK:
            BK["error"] = "Cancelada"
    except Exception as e:
        traceback.print_exc()
        with LOCK:
            BK["error"] = conn_error_text(e)
        notify("err", "No se pudo hacer la copia de seguridad", str(e))
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        with LOCK:
            BK.update(busy=False, current="", t=time.time())
    return None


def backup_list():
    d = backup_dir()
    out = []
    if os.path.isdir(d):
        import zipfile
        for x in sorted(os.listdir(d), key=lambda n: os.path.getmtime(os.path.join(d, n)), reverse=True):
            if not x.endswith(".zip") or not x.startswith("PS5-"):
                continue
            fp = os.path.join(d, x)
            info = {"name": x, "path": fp, "size": os.path.getsize(fp), "t": os.path.getmtime(fp), "saves": [],
                    "names": {}}
            try:
                with zipfile.ZipFile(fp) as z:
                    m = json.loads(z.read("manifest.json"))
                    info["sections"] = m.get("sections")
                    ids = set()
                    for it in m.get("files") or []:
                        if it.get("section") == "saves":
                            parts = it["path"].split("/")
                            if len(parts) > 5:
                                ids.add(parts[5])
                    info["saves"] = sorted(ids)
                    info["emus"] = sorted({it.get("emu") for it in m.get("files") or [] if it.get("emu")})
                    info["emu_names"] = {i: EMU_NAMES.get(i, i) for i in info["emus"]}
                    titles = lib_load().get("titles") or {}
                    info["names"] = {i: (titles.get(i.upper()) or {}).get("title") for i in ids}
                    info["files"] = len(m.get("files") or [])
            except Exception as e:
                info["error"] = str(e)
            out.append(info)
    return out


def backup_restore(path, ids, emus=None):
    """Vuelve a poner en la consola las partidas elegidas de una copia (antes guarda las de ahora)."""
    import zipfile
    cfg = cfg_snapshot()
    emus = set(emus or [])
    busy_emu = [EMU_NAMES.get(i, i) for i in emus if i in open_titles() or
                any(a in open_titles() for e in EMU_SAVES if e["id"] == i for a in e.get("also", []))]
    if busy_emu:
        with LOCK:
            BK["error"] = ("Cierra %s en la consola antes de restaurar sus partidas (si ya está cerrado, espera un "
                           "par de minutos y vuelve a probar)." % ", ".join(busy_emu))
        return
    path = os.path.abspath(path)
    if not path.startswith(os.path.abspath(backup_dir(cfg))) or not os.path.isfile(path):
        raise E.PipeError("Esa copia no está en la carpeta de copias")
    ids = set(ids or [])
    with LOCK:
        BK.update(busy=True, what="restore", done=0, total=0, files=0, current="Guardando antes las partidas de ahora…",
                  error="")
    BK_CANCEL.clear()
    try:
        backup_run(["saves", "emu"] if emus else ["saves"], "antes-de-restaurar")
        with LOCK:
            BK.update(busy=True, what="restore", current="Restaurando…", error="")
        with zipfile.ZipFile(path) as z:
            m = json.loads(z.read("manifest.json"))
            files = [it for it in m.get("files") or [] if it.get("section") == "saves" and ids != ["-"]
                     and (not ids or (len(it["path"].split("/")) > 5 and it["path"].split("/")[5] in ids))]
            files += [it for it in m.get("files") or [] if it.get("section") == "emu" and it.get("emu") in emus]
            with LOCK:
                BK.update(total=sum(it.get("size") or 0 for it in files), files=len(files))
            f = E.Ftp(cfg).connect()
            done = 0
            try:
                for it in files:
                    if BK_CANCEL.is_set():
                        raise E.Cancelled()
                    p = it["path"]
                    if it.get("section") != "emu":
                        user_dir = "/".join(p.split("/")[:4])
                        try:
                            E.ftp_listdir(f, user_dir)
                        except ftplib.all_errors:
                            raise E.PipeError("En la consola no existe %s: esta copia es de otro usuario o de otra "
                                              "consola" % user_dir)
                    with LOCK:
                        BK["current"] = p
                    E.Ftp.mkdirs(f, posixpath.dirname(p))
                    with z.open("consola" + p) as src:
                        f.storbinary("STOR " + p, src, 1024 * 1024)
                    done += it.get("size") or 0
                    with LOCK:
                        BK["done"] = done
            finally:
                try:
                    f.quit()
                except Exception:
                    pass
        notify("ok", "Partidas restauradas", "%d archivos desde %s" % (len(files), os.path.basename(path)),
               {"tab": "Backup"})
    except E.Cancelled:
        with LOCK:
            BK["error"] = "Cancelado"
    except Exception as e:
        with LOCK:
            BK["error"] = conn_error_text(e)
        notify("err", "No se pudieron restaurar las partidas", str(e))
    finally:
        with LOCK:
            BK.update(busy=False, current="", t=time.time())


def send_now(job):
    """«Enviar ahora»: pasa delante de todo; lo que se esta enviando se pausa y sigue solo al acabar este.
    Llamar con WAKE cogido."""
    JOBS.remove(job)
    first = next((i for i, x in enumerate(JOBS) if x["status"] in ("pendiente", "pausado")), len(JOBS))
    JOBS.insert(first, job)
    if job["status"] == "pausado":
        job["status"] = "pendiente"
        job["phase"] = "En cola"
        CANCELS.setdefault(job["id"], threading.Event()).clear()
    for x in JOBS:
        if x is not job and x["status"] in ("comprobando", "enviando", "convirtiendo"):
            x["_pause"] = True
            x["resume_after"] = job["id"]
            CANCELS.setdefault(x["id"], threading.Event()).set()
            jlog(x, "En pausa para enviar antes otra cosa: sigue sola cuando termine")
    WAKE.notify_all()


def resume_after_now():
    """Lo que se pauso por «Enviar ahora» sigue solo cuando ha terminado lo que se paso delante."""
    with WAKE:
        for x in JOBS:
            ra = x.get("resume_after")
            if not ra:
                continue
            other = find(ra)
            if other and other["status"] not in ("hecho", "error", "cancelado"):
                continue
            x.pop("resume_after", None)
            if x["status"] == "pausado":
                CANCELS.setdefault(x["id"], threading.Event()).clear()
                x["error"] = ""
                x["status"] = "pendiente"
                x["phase"] = "En cola"
                jlog(x, "Sigo donde lo dejé")
                WAKE.notify_all()


def monitor():
    """Cada pocos segundos: que el PC no se duerma con trabajo y la accion al acabar la cola."""
    awake = None
    last_auto = 0.0
    while True:
        time.sleep(5)
        try:
            resume_after_now()
        except Exception:
            traceback.print_exc()
        try:
            # copia de seguridad diaria (si esta activada y la consola responde)
            if (SETTINGS.get("backup_auto") and not BK["busy"] and not busy()
                    and time.time() - float(SETTINGS.get("backup_last") or 0) > 24 * 3600
                    and time.time() - last_auto > 1800 and not open_titles(6 * 3600)):
                # si hay algo abierto en la consola se espera a que lo cierres: asi no se copia una partida a medio guardar
                last_auto = time.time()
                try:
                    E.Ftp(cfg_snapshot()).call(lambda f: f.voidcmd("NOOP"))
                    threading.Thread(target=backup_run, daemon=True).start()
                except Exception:
                    pass       # consola apagada: se intentara mas tarde
        except Exception:
            traceback.print_exc()
        try:
            b = busy()
            with LOCK:
                want = bool(b and SETTINGS.get("keep_awake", True))
            if want != awake:
                F.keep_awake(want)
                awake = want
            with LOCK:
                after, cd = SCHED["after"], SCHED["countdown"]
            if after not in ("sleep", "shutdown"):
                continue
            if b or SCHED["start"]:
                with LOCK:
                    SCHED["seen_busy"] = True
                    SCHED["countdown"] = 0
                continue
            if not SCHED["seen_busy"]:
                continue
            if not cd:
                with LOCK:
                    SCHED["countdown"] = time.time() + 60
                notify("warn", "Cola terminada: el PC se %s en 1 minuto" %
                       ("apagará" if after == "shutdown" else "suspenderá"),
                       "Pulsa Cancelar en la cola si no quieres.", {"tab": "Transfers"})
            elif time.time() >= cd:
                with LOCK:
                    SCHED.update(after="none", countdown=0, seen_busy=False)
                sched_save()
                F.keep_awake(False)
                awake = False
                print(F.power_action(after))
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------------------
# Payloads (ftpsrv, BFpilot) y PS5 Dump Forge
# ---------------------------------------------------------------------------

def payload_list():
    folder = os.path.join(DATA_DIR, "payloads")
    out = []
    for p in F.PAYLOADS + [x for x in F.EXTRA_PAYLOADS if x["id"] in ("zftpd", "overlay")]:
        cached = []
        try:
            cached = sorted(n[len(p["id"]) + 1:-4] for n in os.listdir(folder)
                            if n.startswith(p["id"] + "-") and n.endswith(".elf"))
        except OSError:
            pass
        out.append({"id": p["id"], "name": p["name"], "what": p["what"], "repo": p["repo"],
                    "cached": cached[-1] if cached else None})
    return out


def wait_service(pid, cfg, secs=20):
    t0 = time.time()
    while time.time() - t0 < secs:
        if pid == "ftpsrv":
            try:
                E.Ftp(cfg).connect().quit()
                return True
            except Exception:
                pass
        elif pid == "bfpilot":
            if E.BFpilot(cfg).alive():
                return True
        else:
            return None
        time.sleep(1.5)
    return False


LOST = {"ftpsrv": "Sin el FTP no funciona casi nada: enviar, Biblioteca, Archivos de la consola, Copia de "
                  "seguridad ni Capturas.",
        "bfpilot": "Sin BFpilot no se puede descomprimir en la consola, ver lo que ocupa cada carpeta, la "
                   "pestaña Espacio ni copiar o mover rápido dentro de la consola. Sí se puede enviar "
                   "descomprimiendo en el PC."}
CONSOLE = {"auto": None}      # lo que hizo el arranque automatico (para ensenarlo en la ventana)


def _port_open(host, port, timeout=1.5):
    host = E.clean_host(host or "")[0]
    if not host:
        return False
    try:
        socket.create_connection((host, int(port)), timeout=timeout).close()
        return True
    except OSError:
        return False


def _svc_ok(pid, cfg):
    if pid == "ftpsrv":
        try:
            E.Ftp(cfg).connect().quit()
            return True, ""
        except Exception as e:
            return False, str(e)
    v = E.BFpilot(cfg).alive()
    return bool(v), "" if v else "no responde"


def console_status():
    """Que hay en la consola: FTP, BFpilot, Payload Manager (y si los tiene instalados y en marcha)
    y el cargador de ELF por red (9021)."""
    cfg = cfg_snapshot()
    pm = F.PayloadManager(cfg["ps5_ip"])
    ver = pm.version()
    items, procs, autoload = [], [], []
    pm_err = ""
    if ver:
        try:
            items = pm.payloads()
            procs = pm.processes()
            autoload = [x.strip() for x in re.split(r"[,\n]", pm.config().get("AUTOLOAD_LIST") or "") if x.strip()]
        except Exception as e:
            pm_err = str(e)
    out = {"ip": cfg["ps5_ip"], "pm": {"present": bool(ver), "version": ver, "error": pm_err},
           "loader": _port_open(cfg["ps5_ip"], cfg.get("elf_port") or 9021), "items": [], "auto": CONSOLE["auto"]}
    for p in F.PAYLOADS:
        ok, err = _svc_ok(p["id"], cfg)
        slot = pm.find(p["id"], items) if ver else None
        run = pm.running(p["id"], procs) if ver else []
        # colgado: Payload Manager lo tiene en marcha, o su puerto acepta conexiones pero no contesta bien
        # (por ejemplo un ftpsrv cargado dos veces da "WinError 10054")
        port = 2121 if p["id"] == "ftpsrv" else 5905
        state = "ok" if ok else ("stuck" if (run or _port_open(cfg["ps5_ip"], port, 1.5)) else "off")
        out["items"].append({"id": p["id"], "name": p["name"], "ok": ok, "error": conn_error_text(Exception(err)) if err else "",
                             "state": state, "installed": bool(slot), "slot": (slot or {}).get("slot"),
                             "file": (slot or {}).get("filename"), "pids": [x.get("pid") for x in run],
                             "autoload": bool(slot and slot.get("slot") in autoload), "lost": LOST[p["id"]]})
    out["reachable"] = bool(ver or out["loader"] or any(i["ok"] for i in out["items"]) or
                            _port_open(cfg["ps5_ip"], 2121, 1) or _port_open(cfg["ps5_ip"], 5905, 1))
    out["mac_lan"] = MAC_LAN_MSG if (not out["reachable"] and mac_lan_blocked(cfg["ps5_ip"])) else ""
    # sin nada que conteste: ¿esta apagada, o encendida pero sin jailbreak (por ejemplo, recien reiniciada)?
    out["alive"] = True if out["reachable"] else (False if out["mac_lan"] else _host_alive(cfg["ps5_ip"]))
    return out


def console_fix(pid, action):
    """Arregla un payload con Payload Manager: 'start' (iniciarlo), 'install' (instalarlo e iniciarlo),
    'restart' (cerrar el que esta colgado o repetido y volver a iniciarlo) o 'autoload'."""
    cfg = cfg_snapshot()
    pm = F.PayloadManager(cfg["ps5_ip"])
    if not pm.version():
        if action in ("start", "install") and _port_open(cfg["ps5_ip"], cfg.get("elf_port") or 9021):
            return payload_send(pid=pid)
        raise E.PipeError("Abre Payload Manager en la consola (es una aplicación del menú de inicio de la PS5) "
                          "y vuelve a intentarlo: PS5 Directo lo usa para cargar los payloads.")
    if action == "autoload":
        st = console_status()
        slots = [i["slot"] for i in st["items"] if i["slot"]]
        if not slots:
            raise E.PipeError("Primero instala ftpsrv y BFpilot en Payload Manager")
        pm.add_autoload(slots)
        return {"done": "autoload"}
    p = next((x for x in F.PAYLOADS if x["id"] == pid), None)
    if not p:
        raise E.PipeError("Payload desconocido")
    slot = pm.find(pid)
    if action == "install" or not slot:
        path, tag = F.fetch_payload(pid, DATA_DIR, log=print)
        with open(path, "rb") as fh:
            pm.upload("%s_%s.elf" % (p["name"], re.sub(r"[^\w.-]", "_", tag or "")), fh.read(64 * 1024 * 1024))
        slot = pm.find(pid)
        if not slot:
            raise E.PipeError("Payload Manager no muestra %s después de instalarlo" % p["name"])
    port = 2121 if pid == "ftpsrv" else 5905
    if action == "restart":
        if not pm.running(pid):
            raise E.PipeError("No encuentro el proceso de %s en Payload Manager para cerrarlo. Ciérralo desde "
                              "Payload Manager en la consola (Procesos activos) o reinicia la consola, y luego "
                              "pulsa «Iniciar»." % p["name"])
        for x in pm.running(pid):
            try:
                pm.kill(x["pid"])
            except Exception as e:
                print("No pude cerrar %s (%s): %s" % (x.get("name"), x.get("pid"), e))
        time.sleep(2)
    elif (pm.running(pid) or _port_open(cfg["ps5_ip"], port, 1.5)) and not _svc_ok(pid, cfg)[0]:
        raise E.PipeError("%s ya está en marcha pero no responde: pulsa «Reiniciar»" % p["name"])
    pm.launch(slot["slot"])
    up = wait_service(pid, cfg)
    try:
        test_connection(cfg)
    except Exception:
        pass
    return {"name": p["name"], "up": up}


def _host_alive(host):
    """¿Contesta la consola aunque no tenga payloads? (Remote Play escucha en 9295; si no, un ping)."""
    host = E.clean_host(host or "")[0]
    if not host:
        return False
    if _port_open(host, 9295, 1.2) or _port_open(host, 997, 0.8):
        return True
    import subprocess
    try:
        # ojo: en Mac, -W va en milisegundos (en Linux, en segundos)
        args = (["ping", "-n", "1", "-w", "1000", host] if os.name == "nt" else
                ["ping", "-c", "1", "-W", "1000" if sys.platform == "darwin" else "1", host])
        r = subprocess.run(args, capture_output=True, timeout=4,
                           **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        return r.returncode == 0 and b"ttl" in r.stdout.lower()
    except Exception:
        return False


AUTO_TRIED = {}
PLAYED_T = [0.0]
SHOTS_T = [0.0]
SHOTS_FILE = os.path.join(DATA_DIR, "capturas_bajadas.json")
SHOTS = {"busy": False, "done": 0, "n": 0, "last": None, "msg": ""}
SHOTS_LOCK = threading.Lock()


def _shots_seen():
    try:
        with open(SHOTS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


SENT_FILE = os.path.join(DATA_DIR, "capturas_envios.json")
TG_API = os.environ.get("PS5D_TG_API") or "https://api.telegram.org"      # se puede cambiar para pruebas
DC_RX = re.compile(os.environ.get("PS5D_DC_RX") or r"^https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/")
SENT_LOCK = threading.Lock()


def _multipart(fields, files):
    """Cuerpo multipart/form-data: fields {nombre: texto}, files [(campo, nombre, bytes, tipo)]."""
    bnd = "----PS5Directo" + secrets.token_hex(12)
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (bnd, k, v)).encode("utf-8"))
    for field, name, data, ctype in files:
        out.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                   % (bnd, field, name.replace('"', ''), ctype)).encode("utf-8"))
        out.write(data)
        out.write(b"\r\n")
    out.write(("--%s--\r\n" % bnd).encode())
    return out.getvalue(), "multipart/form-data; boundary=" + bnd


def _post_multipart(url, fields, files, timeout=300):
    body, ctype = _multipart(fields, files)
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": ctype, "User-Agent": "PS5Directo"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        txt = e.read(4096).decode("utf-8", "replace")
        try:
            msg = json.loads(txt).get("description") or json.loads(txt).get("message") or txt
        except Exception:
            msg = txt
        raise E.PipeError("HTTP %s: %s" % (e.code, str(msg)[:200]))


def send_capture(path, dest, title="", when=""):
    """Manda una captura o un clip que ya esta en el PC a Telegram (tu bot) o a Discord (un webhook)."""
    cfg = cfg_snapshot()
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    size = os.path.getsize(path)
    caption = " · ".join(x for x in (title, when.replace("T", " ")[:16]) if x)
    if ext == ".jxr":                          # HDR: Telegram y Discord no lo ensenan; se manda su vista en JPG
        jpg = os.path.splitext(path)[0] + "_vista.jpg"
        if os.path.isfile(jpg) or _jxr_to_jpg(path, jpg):
            path, name, ext, size = jpg, os.path.basename(jpg), ".jpg", os.path.getsize(jpg)
    with open(path, "rb") as fh:
        data = fh.read(60 * 1024 * 1024)
    img = ext in (".jpg", ".jpeg", ".png", ".webp")
    vid = ext in (".mp4", ".mov", ".webm")
    ctype = MEDIA_TYPES.get(ext) or ("image/jpeg" if img else "application/octet-stream")
    if dest == "tg":
        tok, chat = cfg.get("shots_tg_token") or "", cfg.get("shots_tg_chat") or ""
        if not tok or not chat:
            raise E.PipeError("Falta el bot o el chat de Telegram (Capturas › Enviar también a…)")
        if size > 50 * 1024 * 1024:
            raise E.PipeError("Pesa más de 50 MB: Telegram no deja a los bots mandar archivos tan grandes")
        if img and size <= 10 * 1024 * 1024:
            method, field = "sendPhoto", "photo"
        elif ext == ".mp4":
            method, field = "sendVideo", "video"
        else:
            method, field = "sendDocument", "document"
        _post_multipart("%s/bot%s/%s" % (TG_API, tok, method), {"chat_id": chat, "caption": caption},
                        [(field, name, data, ctype)])
    elif dest == "dc":
        hook = cfg.get("shots_dc_hook") or ""
        if not DC_RX.match(hook):
            raise E.PipeError("Falta el webhook de Discord (Capturas › Enviar también a…)")
        if size > 10 * 1024 * 1024:
            raise E.PipeError("Pesa más de 10 MB: Discord no lo acepta sin Nitro en el servidor")
        _post_multipart(hook, {"content": caption}, [("files[0]", name, data, ctype)])
    else:
        raise E.PipeError("Destino desconocido")
    return True


def _sent_log(entry):
    with SENT_LOCK:
        try:
            with open(SENT_FILE, encoding="utf-8") as fh:
                lst = json.load(fh)
        except Exception:
            lst = []
        lst = [x for x in lst if not (x["file"] == entry["file"] and x["dest"] == entry["dest"])]
        lst.append(entry)
        _save_json(SENT_FILE, lst[-300:])


def shots_route(tid):
    cfg = cfg_snapshot()
    d = (cfg.get("shots_route") or {}).get(tid) or cfg.get("shots_send") or ""
    return [] if d in ("", "none") else (["tg", "dc"] if d == "both" else [d])


def shots_forward(local, it):
    for dest in shots_route(it.get("tid") or ""):
        entry = {"t": time.time(), "file": local, "dest": dest, "title": it.get("app") or it.get("tid") or "",
                 "when": it.get("when") or "", "ok": False, "err": ""}
        try:
            send_capture(local, dest, entry["title"], entry["when"])
            entry["ok"] = True
        except Exception as e:
            entry["err"] = str(e)[:300]
        _sent_log(entry)


def shots_sync(all_old=False, baseline=False):
    """Baja al PC las capturas y videos nuevos de la consola, ordenados en carpetas por titulo. Lo que ya se bajo
    (o lo que habia al activarlo, con baseline) queda apuntado y no se repite aunque lo borres del PC."""
    if not SHOTS_LOCK.acquire(blocking=False):
        return
    try:
        SHOTS.update(busy=True, done=0, n=0, msg="Mirando las capturas de la consola…")
        items = media_list()
        seen = _shots_seen()
        if baseline:
            for it in items:
                seen[it["path"]] = it["size"]
            _save_json(SHOTS_FILE, seen)
            SHOTS.update(msg="Listo: a partir de ahora se bajarán solas las nuevas (%d que ya había no)." % len(items))
            return
        todo = [it for it in items if all_old or seen.get(it["path"]) != it["size"]]
        SHOTS.update(n=len(todo))
        cfg = cfg_snapshot()
        root = MEDIA_CONV_DIR()
        got = 0
        for it in reversed(todo):          # las mas viejas primero
            folder = (E.safe_name(it["app"]) if it.get("app") else "") or it.get("tid") or "Otros"
            dst = os.path.join(root, folder, it["name"])
            if not (os.path.isfile(dst) and os.path.getsize(dst) == it["size"]):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                SHOTS["msg"] = "Bajando %s (%s)" % (it["name"], folder)
                E.ftp_download(cfg, it["path"], dst, threading.Event(), lambda n: None, it["size"])
                got += 1
                if not all_old and shots_route(it.get("tid") or ""):
                    SHOTS["msg"] = "Enviando %s…" % it["name"]
                    shots_forward(dst, it)
            seen[it["path"]] = it["size"]
            SHOTS["done"] += 1
            if SHOTS["done"] % 10 == 0:
                _save_json(SHOTS_FILE, seen)
        _save_json(SHOTS_FILE, seen)
        SHOTS.update(last=time.time(), msg=("Bajadas %d capturas o vídeos nuevos a %s" % (got, root)) if got else
                     "No hay capturas nuevas")
        if got:
            notify("ok", "Capturas nuevas en tu PC", "%d en %s" % (got, root), None, key="shots%d" % int(time.time() // 60))
    except Exception as e:
        SHOTS["msg"] = "No pude bajar las capturas: %s" % e
    finally:
        SHOTS["busy"] = False
        SHOTS_LOCK.release()
CONN_LOST_RX = re.compile(r"(?i)no puedo conectar con el ftp|conexi[oó]n con la ps5 perdida|bfpilot ha dejado de responder|"
                          r"bfpilot no responde|no hay conexi[oó]n con la consola|10054|10053|10060|10061|10065|"
                          r"connection (reset|refused|aborted)|timed out|no route|unreachable|broken pipe")


def is_conn_lost(msg):
    """¿Fallo porque se perdio la consola (reposo, reinicio, payload caido)? Entonces se puede reanudar sola."""
    m = str(msg or "")
    return bool(CONN_LOST_RX.search(m)) and not re.search(r"(?i)no cabe|no hay sitio|contrase|crc|da[nñ]ad", m)


def auto_resume():
    """Reanuda las transferencias que fallaron por perder la consola, cuando vuelve a contestar (max. 5 veces)."""
    cfg = cfg_snapshot()
    with LOCK:
        cands = [j for j in JOBS if j["status"] == "error" and j.get("auto_retry") and (j.get("auto_tries") or 0) < 5]
    if not cands:
        return
    ftp_ok = _svc_ok("ftpsrv", cfg)[0]
    if not ftp_ok:
        return
    bf_ok = None
    names = []
    with WAKE:
        for job in cands:
            if job["status"] != "error":
                continue
            if re.search(r"(?i)bfpilot", job.get("error") or ""):
                if bf_ok is None:
                    bf_ok = bool(E.BFpilot(cfg).alive())
                if not bf_ok:
                    continue
            job["auto_tries"] = (job.get("auto_tries") or 0) + 1
            CANCELS.setdefault(job["id"], threading.Event()).clear()
            jlog(job, "La consola ha vuelto: reanudo solo (intento %d de 5)" % job["auto_tries"])
            job["error"] = ""
            job.pop("need_bytes", None)
            job.pop("ask", None)
            if job.get("info") and job.get("sent", 0) >= max(0, job.get("total", 0)) and job.get("total", -1) > 0:
                job["status"] = "en cola para descomprimir"
                job["phase"] = "Reintentando la descompresion"
                EXTRACT_Q.append(job["id"])
            else:
                job["status"] = "pendiente"
                job["phase"] = "En cola"
            names.append(_job_name(job))
        WAKE.notify_all()
    if names:
        save_jobs()
        notify("info", "La consola ha vuelto: reanudo %s" % ", ".join(names[:3]), "")      # id -> momento del ultimo intento automatico (para no insistir cada pocos segundos)


FORGE_T = [0.0]
TRASH_T = [0.0]
CLIP = {"last": None, "seq": None, "ok": None}
WATCH = {"dir": None, "seen": {}, "sizes": {}}
WATCH_EXTS = (".rar", ".zip", ".7z", ".exfat", ".ffpkg", ".ffpfs", ".ffpfsc", ".pkg", ".tar", ".torrent")
WATCH_SKIP = (".part", ".crdownload", ".tmp", ".download", ".!ut", ".aria2", ".partial")


def watch_dir_tick():
    """«Carpeta vigilada»: lo nuevo que termine de bajar ahi (con FDM, JDownloader, el navegador...) va a la caja de
    enlaces como archivo del PC, todas las partes juntas. Se espera a que deje de crecer."""
    with LOCK:
        d = (SETTINGS.get("watch_dir") or "").strip()
    if not d or not os.path.isdir(d):
        WATCH["dir"] = None
        return
    names = {}
    for n in os.listdir(d):
        fp = os.path.join(d, n)
        low = n.lower()
        if not os.path.isfile(fp) or low.endswith(WATCH_SKIP):
            continue
        if not (low.endswith(WATCH_EXTS) or re.search(r"\.(\d{3}|r\d\d)$", low)):
            continue
        try:
            names[fp] = (os.path.getsize(fp), os.path.getmtime(fp))
        except OSError:
            pass
    if WATCH["dir"] != d:            # lo que ya habia al activarla (o ya se recogio antes) no cuenta
        since = float(SETTINGS.get("watch_since") or 0) or time.time()
        WATCH.update(dir=d, seen={fp: st for fp, st in names.items() if st[1] <= since}, sizes={})
    new = []
    for fp, st in names.items():
        if WATCH["seen"].get(fp) == st:
            continue
        if WATCH["sizes"].get(fp) != st:      # sigue creciendo: otra vuelta
            WATCH["sizes"][fp] = st
            continue
        WATCH["seen"][fp] = st
        WATCH["sizes"].pop(fp, None)
        new.append(fp)
    if not new:
        return
    with LOCK:
        SETTINGS["watch_since"] = max([float(SETTINGS.get("watch_since") or 0)] + [names[fp][1] for fp in new])
        for fp in sorted(new):
            if not any(x["url"] == fp for x in INBOX):
                INBOX.append({"url": fp, "name": os.path.basename(fp), "t": time.time()})
        del INBOX[:-200]
    notify("info", "%d archivo(s) nuevo(s) en la carpeta vigilada" % len(new),
           "Están en la caja de enlaces. Revísalos y pulsa «Añadir a la cola».", {"tab": "Transfers"},
           key="watch:%d" % int(time.time() // 10))
    save_settings()


def clip_watch():
    """«Captar enlaces que copies»: si copias enlaces de descarga (en cualquier programa), van a la caja de enlaces
    de PS5 Directo, como con la extension. Solo enlaces de archivos (.rar, .7z, .zip, partes, .pkg, imagenes...)."""
    while True:
        time.sleep(1.2)
        try:
            with LOCK:
                on = bool(SETTINGS.get("clip_watch"))
            if not on:
                CLIP["last"] = None
                CLIP["seq"] = None
                continue
            seq = F.clipboard_seq()
            if seq is not None and seq == CLIP["seq"]:
                continue
            CLIP["seq"] = seq
            txt = F.clipboard_text()
            CLIP["ok"] = txt is not None
            if txt is None:
                time.sleep(5)
                continue
            if CLIP["last"] is None:          # lo que ya habia al activarlo no cuenta
                CLIP["last"] = txt
                continue
            if txt == CLIP["last"]:
                continue
            CLIP["last"] = txt
            links = F.clipboard_links(txt)
            if not links:
                continue
            new = 0
            with LOCK:
                for u in links:
                    if not any(x["url"] == u for x in INBOX) and not any(u in (j.get("lines") or []) for j in JOBS):
                        INBOX.append({"url": u, "name": urllib.parse.unquote(u.split("?")[0].rsplit("/", 1)[-1])[:300],
                                      "t": time.time()})
                        new += 1
                del INBOX[:-200]
            if new:
                notify("info", "%d enlace(s) copiado(s) en la caja de enlaces" % new,
                       "Los has copiado y PS5 Directo los ha recogido. Revísalos y pulsa «Añadir a la cola».",
                       {"tab": "Transfers"}, key="clip:%d" % int(time.time() // 5))
        except Exception as e:
            print("Portapapeles:", e)
            time.sleep(5)


def console_watch():
    """Cada poco, si falta ftpsrv o BFpilot y Payload Manager esta abierto (por ejemplo, despues de reiniciar
    la consola y volver a hacer el jailbreak), los inicia solos. No repite el mismo intento antes de 2 min."""
    time.sleep(40)
    while True:
        try:
            watch_dir_tick()
        except Exception as e:
            print("Carpeta vigilada:", e)
        try:
            with LOCK:
                on = SETTINGS.get("auto_payloads", True)
            cfg = cfg_snapshot()
            if on and cfg.get("ps5_ip") and not all(_svc_ok(x, cfg)[0] for x in ("ftpsrv", "bfpilot")):
                if F.PayloadManager(cfg["ps5_ip"]).version():
                    console_auto(min_gap=120)
            auto_resume()
            if time.time() - TRASH_T[0] > 3600 and _svc_ok("ftpsrv", cfg)[0]:
                TRASH_T[0] = time.time()
                trash_tidy()
            if time.time() - SYS_T[0] > 60 and cfg.get("ps5_ip"):
                SYS_T[0] = time.time()
                threading.Thread(target=sys_sample, daemon=True).start()
            if time.time() - EMU_T[0] > 6 * 3600:
                EMU_T[0] = time.time()
                threading.Thread(target=emu_news, daemon=True).start()
            if time.time() - FORGE_T[0] > 1800:
                FORGE_T[0] = time.time()
                threading.Thread(target=forge_news, daemon=True).start()
            if cfg.get("shots_auto") and time.time() - SHOTS_T[0] > 300 and _svc_ok("ftpsrv", cfg)[0]:
                SHOTS_T[0] = time.time()
                threading.Thread(target=shots_sync, daemon=True).start()
            if time.time() - PLAYED_T[0] > 120 and _svc_ok("ftpsrv", cfg)[0]:
                PLAYED_T[0] = time.time()
                played_poll()
            if cfg.get("ps5_ip") and time.time() - PLD_T[0] > 6 * 3600:
                PLD_T[0] = time.time() - 6 * 3600 + 600          # si Payload Manager no esta, otra vez en 10 min
                if F.PayloadManager(cfg["ps5_ip"]).version(timeout=2):
                    PLD_T[0] = time.time()
                    threading.Thread(target=pld_tick, daemon=True).start()
            if cfg.get("ps5_ip") and time.time() - GUARD_T[0] > 600 and _svc_ok("ftpsrv", cfg)[0]:
                GUARD_T[0] = time.time()
                threading.Thread(target=guard_tick, daemon=True).start()
            if cfg.get("ps5_ip") and _svc_ok("ftpsrv", cfg)[0]:
                threading.Thread(target=crash_tick, daemon=True).start()
            if time.time() - RECOMP["t"] > 6 * 3600:
                threading.Thread(target=recomp_fetch, daemon=True).start()
        except Exception as e:
            print("Vigilancia de la consola:", e)
        time.sleep(15)


DISCOVER_PORTS = ((2121, "FTP"), (8084, "Payload Manager"), (5905, "BFpilot"), (9021, "cargador de ELF"),
                  (9295, "PS5"))


def _local_ipv4s():
    ips = set()
    # la IP con la que se sale hacia cada sitio (en Android getaddrinfo solo da «localhost», asi que esto es lo que vale)
    for t in ("10.255.255.255", "192.168.1.1", "192.168.0.1", "172.16.0.1", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((t, 1))
                ips.add(s.getsockname()[0])
            finally:
                s.close()
        except Exception:
            pass
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(ai[4][0])
    except Exception:
        pass
    return [ip for ip in ips if not ip.startswith(("127.", "169.254."))]


def discover_ps5(timeout=0.35, hint=""):
    """Busca la consola en la red de casa: equipos que contesten en los puertos de los payloads o de la PS5.
    hint: la IP de la Wi-Fi que da la app de Android (la red de casa va primero)."""
    from concurrent.futures import ThreadPoolExecutor
    mine = _local_ipv4s()
    hint = [h for h in re.split(r"[,\s]+", hint or "") if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", h)]
    mine = list(dict.fromkeys(hint + mine))
    # primero la Wi-Fi de la app y las redes de casa típicas (192.168…), luego el resto (los datos móviles suelen ser 10.x)
    order = lambda n: (0 if any(h.startswith(n + ".") for h in hint) else 1 if n.startswith("192.168.") else 2 if n.startswith("172.") else 3, n)
    nets = sorted({ip.rsplit(".", 1)[0] for ip in mine}, key=order)[:3]
    hosts = [n + "." + str(k) for n in nets for k in range(1, 255) if n + "." + str(k) not in mine]

    def probe(h):
        found = []
        for port, label in DISCOVER_PORTS:
            try:
                socket.create_connection((h, port), timeout=timeout).close()
                found.append(label)
            except OSError:
                pass
        return h, found
    out = []
    with ThreadPoolExecutor(max_workers=96) as ex:
        for h, found in ex.map(probe, hosts):
            if found:
                out.append({"ip": h, "found": found})
    out.sort(key=lambda x: -len(x["found"]))
    return {"items": out, "nets": [n + ".x" for n in nets]}


PLAYED_FILE = os.path.join(DATA_DIR, "abiertos.json")
PLAYED_LOCK = threading.Lock()
SMP_LOGS = ("/data/shadowmount/debug.log", "/data/shadowmountplus/debug.log")
GAME_EV_RX = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\][^\n]*?\[GAME\] started: ([A-Z]{4}\d{5}) pid=(\d+)", re.M)
GAME_HAND_RX = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\][^\n]*?process handoff: ([A-Z]{4}\d{5})[^\n]*?-> pid=(\d+)", re.M)
GAME_STOP_RX = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\][^\n]*?game stopped: pid=(\d+) title=([A-Z]{4}\d{5})", re.M)


def played_load():
    try:
        with open(PLAYED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"titles": {}, "seen": [], "open": {}}


def _log_ts(h, m, sec, now):
    """La hora del registro de ShadowMountPlus (sin fecha) -> fecha y hora: hoy, o ayer si aun no ha llegado."""
    t = time.mktime(time.localtime(now)[:3] + (int(h), int(m), int(sec), 0, 0, -1))
    return t - 86400 if t > now + 300 else t


def played_poll():
    """Lee el final del registro de ShadowMountPlus y apunta cuando se abre y se cierra cada titulo
    (para «Abierto hace…» y el tiempo de uso). Solo cuenta desde que PS5 Directo lo mira."""
    cfg = cfg_snapshot()
    now = time.time()

    def read(f):
        for p in SMP_LOGS:
            try:
                size = f.size(p)
            except ftplib.all_errors:
                continue
            if not size:
                continue
            off = max(0, size - 256 * 1024)
            return F.ftp_read(f, p, off, size - off).decode("utf-8", "replace")
        return ""
    try:
        txt = E.Ftp(cfg).call(read)
    except Exception:
        return
    if not txt:
        return
    with PLAYED_LOCK:
        d = played_load()
        seen = set(d.get("seen") or [])
        opened = d.setdefault("open", {})
        titles = d.setdefault("titles", {})
        evs = [(_log_ts(h, m, x, now), "start", tid, pid) for h, m, x, tid, pid in GAME_EV_RX.findall(txt)]
        evs += [(_log_ts(h, m, x, now), "stop", tid, pid) for h, m, x, pid, tid in GAME_STOP_RX.findall(txt)]
        evs += [(_log_ts(h, m, x, now) + 0.5, "hand", tid, pid) for h, m, x, tid, pid in GAME_HAND_RX.findall(txt)]
        changed = False
        for ts, kind, tid, pid in sorted(evs):
            key = "%s|%s|%s|%d" % (kind, tid, pid, int(ts) // 60)
            if key in seen:
                continue
            seen.add(key)
            changed = True
            t = titles.setdefault(tid, {"last": 0, "times": 0, "secs": 0})
            if kind == "start":
                if ts - t.get("last", 0) > 120:      # el mismo arranque sale dos veces (relevo de proceso)
                    t["times"] = t.get("times", 0) + 1
                t["last"] = max(t.get("last", 0), ts)
                opened[tid] = opened.get(tid) or ts
            elif kind == "hand":                     # el titulo sigue abierto con otro proceso
                opened[tid] = opened.get(tid) or ts
                t.setdefault("_hand", 0)
                t.pop("_hand", None)
            else:
                st = opened.pop(tid, None)
                if st and 0 < ts - st < 24 * 3600:
                    t["secs"] = t.get("secs", 0) + int(ts - st)
        if changed:
            d["seen"] = sorted(seen)[-3000:]
            try:
                _save_json(PLAYED_FILE, d)
            except Exception:
                pass


def console_auto(min_gap=0):
    """Al abrir PS5 Directo: si falta ftpsrv o BFpilot y Payload Manager esta abierto en la consola,
    los inicia solo (solo los que no esten ya en marcha: nunca dos veces lo mismo)."""
    with LOCK:
        on = SETTINGS.get("auto_payloads", True)
    if not on:
        return
    try:
        st = console_status()
    except Exception as e:
        print("Comprobación de la consola:", e)
        return
    done = []
    if st["pm"]["present"]:
        for it in st["items"]:
            if it["ok"] or it["state"] != "off" or not it["installed"]:
                continue
            if min_gap and time.time() - AUTO_TRIED.get(it["id"], 0) < min_gap:
                continue
            AUTO_TRIED[it["id"]] = time.time()
            try:
                r = console_fix(it["id"], "start")
                done.append({"id": it["id"], "name": it["name"], "up": r.get("up")})
            except Exception as e:
                done.append({"id": it["id"], "name": it["name"], "error": str(e)})
    CONSOLE["auto"] = {"t": time.time(), "done": done}
    if done:
        ok = [d["name"] for d in done if d.get("up")]
        if ok:
            notify("info", "Iniciado desde Payload Manager: %s" % ", ".join(ok), "")


def extra_payload(pid, action):
    """PS5 Dump Forge (y otros) en la consola: 'status' mira si su web contesta; 'send' lo baja de GitHub y lo
    inicia (con Payload Manager si esta abierto, si no por el cargador de ELF)."""
    cfg = cfg_snapshot()
    p = next((x for x in F.EXTRA_PAYLOADS if x["id"] == pid), None)
    if not p:
        raise E.PipeError("Payload desconocido")
    host = E.clean_host(cfg.get("ps5_ip") or "")[0]
    url = "http://%s:%d" % (host, p["port"]) if p.get("port") else ""
    if action == "send":
        path, tag = F.fetch_payload(pid, DATA_DIR, log=print)
        pm = F.PayloadManager(cfg["ps5_ip"])
        with open(path, "rb") as fh:
            data = fh.read(64 * 1024 * 1024)
        if pm.version():
            name = "%s_%s.elf" % (p["name"].replace(" ", "_"), re.sub(r"[^\w.-]", "_", tag or ""))
            if not any(x.get("filename") == name for x in pm.payloads()):
                pm.upload(name, data)
            hit = next((x for x in pm.payloads() if x.get("filename") == name), None)
            if not hit:
                raise E.PipeError("Payload Manager no muestra %s después de instalarlo" % p["name"])
            pm.launch(hit["slot"])
        elif _port_open(cfg["ps5_ip"], cfg.get("elf_port") or 9021):
            F.send_elf(cfg["ps5_ip"], int(cfg.get("elf_port") or 9021), data)
        else:
            raise E.PipeError("Abre Payload Manager en la consola (o un cargador de ELF en el puerto 9021) y vuelve a probar.")
        if not p.get("port"):
            return {"running": False, "sent": True, "url": "", "tag": tag, "name": p["name"]}
        t0 = time.time()
        while time.time() - t0 < 30 and not _port_open(cfg["ps5_ip"], p["port"], 1.5):
            time.sleep(1.5)
        return {"running": _port_open(cfg["ps5_ip"], p["port"], 1.5), "url": url, "tag": tag, "name": p["name"]}
    if not p.get("port"):
        return {"running": False, "url": "", "name": p["name"]}
    return {"running": _port_open(cfg["ps5_ip"], p["port"], 1.5), "url": url, "name": p["name"]}


def rec_installed(f):
    """Title IDs que hay en las carpetas de aplicaciones de la consola."""
    have = set()
    for base in ("/data/homebrew", "/mnt/ext0/homebrew", "/mnt/ext1/homebrew", "/mnt/usb0/homebrew", "/mnt/usb1/homebrew",
                 "/user/app"):
        try:
            for n, v in E.ftp_listdir(f, base):
                t = E.title_id(n)
                if t:
                    have.add(t.upper())
        except ftplib.all_errors:
            pass
    return have



# ---------------------------------------------------------------------------
# Arte de «Descubrir» y de las novedades: portadas e iconos oficiales de cada proyecto (su GitHub o el catálogo de
# RecompStore). Se bajan una vez y se guardan en ~/.ps5directo/arte, así se ven al instante y también sin internet.
# ---------------------------------------------------------------------------
ART_DIR = os.path.join(DATA_DIR, "arte")
ART_HOSTS = ("raw.githubusercontent.com", "githubusercontent.com", "github.com", "recompstore.com")
ART_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml",
             "gif": "image/gif"}


def _art_host_ok(u):
    try:
        pu = urllib.parse.urlparse(u)
    except Exception:
        return False
    h = (pu.hostname or "").lower()
    extra = urllib.parse.urlparse(RECOMP_URL).hostname or ""
    if pu.scheme == "http" and h in ("127.0.0.1", "localhost") and h == extra:
        return True                                  # el catálogo de pruebas
    return pu.scheme == "https" and (h == extra or any(h == x or h.endswith("." + x) for x in ART_HOSTS))


def art_get(u):
    """Devuelve (bytes, tipo) de una imagen de arte, de la caché o bajándola (máx. 12 MB)."""
    u = (u or "").strip()
    if not u or len(u) > 600 or not _art_host_ok(u):
        return None
    key = hashlib.sha1(u.encode("utf-8")).hexdigest()
    path = os.path.join(ART_DIR, key)
    try:
        if os.path.isfile(path) and time.time() - os.path.getmtime(path) < 30 * 86400:
            with open(path, "rb") as fh:
                data = fh.read()
            with open(path + ".t", encoding="utf-8") as fh:
                return data, fh.read().strip() or "image/png"
    except Exception:
        pass
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "PS5Directo/" + VERSION})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read(12 * 1024 * 1024 + 1)
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if len(data) > 12 * 1024 * 1024:
            return None
        if not ctype.startswith("image/"):
            ext = u.rsplit("?", 1)[0].rsplit(".", 1)[-1].lower()
            ctype = ART_TYPES.get(ext, "")
        if not ctype.startswith("image/"):
            return None
        head = data[:200].lstrip()
        if not (data[:4] in (b"\x89PNG", b"GIF8", b"RIFF") or data[:3] == b"\xff\xd8\xff" or head[:5] in (b"<?xml", b"<svg ")
                or head[:4] == b"<svg"):
            return None                              # p. ej. un puntero de Git LFS en vez de la imagen
        os.makedirs(ART_DIR, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        with open(path + ".t", "w", encoding="utf-8") as fh:
            fh.write(ctype)
        return data, ctype
    except Exception:
        try:                                         # sin internet: vale la copia vieja
            with open(path, "rb") as fh:
                data = fh.read()
            with open(path + ".t", encoding="utf-8") as fh:
                return data, fh.read().strip() or "image/png"
        except Exception:
            return None


def rec_art(c):
    """Portada (hero) e icono de un programa de «Descubrir»: los oficiales de su proyecto o, si no, los del catálogo
    de RecompStore para ese mismo repositorio."""
    a = dict(F.CATALOG_ART.get(c["id"]) or {})
    if not (a.get("icon") and a.get("hero")):
        repo = (c.get("repo") or "").lower()
        with LOCK:
            m = next((x for x in RECOMP["items"] if (x.get("repo") or "").lower() == repo), None) if repo else None
        if m:
            a["icon"] = a.get("icon") or m.get("icon") or ""
            a["hero"] = a.get("hero") or m.get("hero") or ""
    return {"icon": a.get("icon") or "", "hero": a.get("hero") or "", "bg": a.get("bg") or []}

def rec_list():
    cfg = cfg_snapshot()
    try:
        recomp_fetch()                               # para el arte de los que no traen el suyo
    except Exception:
        pass
    try:
        have = E.Ftp(cfg).call(rec_installed)
        ok = True
    except Exception:
        have, ok = set(), False
    out = []
    for c in F.CATALOG:
        it = {k: c[k] for k in ("id", "kind", "group", "name", "what", "needs", "repo", "fam", "mine") if k in c}
        it["exp"] = bool(c.get("exp"))
        it["sys"] = c.get("sys", "")
        it["update"] = c.get("update", "")
        it.update(rec_art(c))
        if c["kind"] == "app":
            it["tid"] = c["tid"]
            tids = [c["tid"]] + list(c.get("also") or [])
            it["installed"] = any(t in have for t in tids) if ok else None
            it["emu"] = bool(c.get("emu"))
            it["ver"] = _emu_vers().get(c["id"]) or ""
            it["latest"] = rec_latest(rec_repo(c)) if it["installed"] else ""
            it["latest_t"] = F.GH_DATES.get(rec_repo(c).lower(), "") if it["latest"] else ""
            it["repo"] = rec_repo(c)
            it["official"] = c["repo"]
            it["alts"] = [c["repo"]] + [r for r in (c.get("alts") or []) if r.lower() != c["repo"].lower()]
            it["guide"] = (c.get("emu") or {}).get("guide") or []
            em = c.get("emu") or {}
            it["has_bios"] = bool(em.get("bios"))
            it["has_games"] = bool(em.get("games"))
            it["games_label"] = em.get("games_label") or ""
        elif c["kind"] == "payload":
            p = next((x for x in F.EXTRA_PAYLOADS if x["id"] == c["id"]), None)
            it["once"] = bool(p and not p.get("port"))
            it["running"] = _port_open(cfg["ps5_ip"], p["port"], 1.2) if p and p.get("port") else False
            it["url"] = "http://%s:%d" % (E.clean_host(cfg.get("ps5_ip") or "")[0], p["port"]) if p and p.get("port") else ""
        elif c["kind"] == "link":
            it["url"] = "https://github.com/%s" % c["repo"]
            it["guide"] = c.get("guide") or []
        elif c["kind"] == "guide":
            it["guide"] = c.get("guide") or []
            it["doc"] = c.get("doc") or ""
            it["payload"] = c.get("payload") or ""
        else:
            it["url"] = "https://github.com/%s/releases/latest" % c["repo"]
            it["ip"] = E.clean_host(cfg.get("ps5_ip") or "")[0]
        out.append(it)
    return {"items": out, "console": ok}



# ---------------------------------------------------------------------------
# Linux, Bazzite y MihawkOS: lo que se puede comprobar y hacer desde aquí para sus asistentes paso a paso
# ---------------------------------------------------------------------------
OS_IMAGES = {
    "ps5linux": ("ps5-linux/ps5-linux-image", [r"(?i)ps5-ubuntu.*\.img\.xz$", r"(?i)ubuntu.*\.img\.(xz|zst|gz)$", r"(?i)\.img\.xz$"]),
    "bazzite": ("VivaLaVent/ps5-bazzite-deck", [r"(?i)\.img\.(xz|zst|gz)$", r"(?i)\.raw\.(xz|zst)$", r"(?i)\.img$",
                                                r"(?i)\.(7z|zip)$"]),
}



# ---------------------------------------------------------------------------
# Traducción automática de las descripciones de las Novedades (vienen en inglés, como las publica cada autor).
# Solo se manda ese texto al traductor de Google; cada frase se traduce una vez y se guarda en el PC.
# ---------------------------------------------------------------------------
TR_URL = os.environ.get("PS5D_TR") or "https://translate.googleapis.com/translate_a/single"
TR_FILE = os.path.join(DATA_DIR, "traducciones.json")
TR_CACHE = {"d": None}


def _tr_one(text, to):
    q = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": to, "dt": "t", "q": text})
    req = urllib.request.Request(TR_URL + "?" + q, headers={"User-Agent": "Mozilla/5.0 PS5Directo/" + VERSION})
    with urllib.request.urlopen(req, timeout=12) as r:
        j = json.loads(r.read(400000).decode("utf-8", "replace"))
    out = "".join(seg[0] for seg in (j[0] or []) if seg and seg[0])
    return out.strip()


def tr_texts(texts, to="es"):
    """Traduce una lista de textos (con caché en disco). Lo que no se pueda traducir vuelve vacío."""
    to = to if re.match(r"^[a-z]{2}(-[A-Z]{2})?$", to) else "es"
    with LOCK:
        if TR_CACHE["d"] is None:
            try:
                with open(TR_FILE, encoding="utf-8") as fh:
                    TR_CACHE["d"] = json.load(fh)
            except Exception:
                TR_CACHE["d"] = {}
        cache = TR_CACHE["d"]
    keys = [hashlib.sha1((to + "|" + t).encode("utf-8")).hexdigest() for t in texts]
    out = [cache.get(k, "") for k in keys]
    todo = [i for i, t in enumerate(texts) if t.strip() and not out[i]]
    if todo:
        import concurrent.futures
        def work(i):
            try:
                return i, _tr_one(texts[i][:4500], to)
            except Exception:
                return i, ""
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            for i, r in ex.map(work, todo):
                if r:
                    out[i] = r
                    cache[keys[i]] = r
        with LOCK:
            if len(cache) > 6000:
                for k in list(cache)[:2000]:
                    cache.pop(k, None)
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(TR_FILE, "w", encoding="utf-8") as fh:
                    json.dump(cache, fh, ensure_ascii=False)
            except Exception:
                pass
    return out


def recomp_install(rid):
    """Instala en la consola algo del catálogo de RecompStore: la última versión de su GitHub, como los de Descubrir."""
    with LOCK:
        x = next((i for i in RECOMP["items"] if i["id"] == rid), None)
    if not x or not x.get("repo"):
        raise E.PipeError("No sé de dónde bajarlo: no tiene página en GitHub")
    tag, assets = F.github_release(x["repo"])
    def pick(exts):
        hits = [a for a in assets if (a.get("name") or "").lower().endswith(exts) and "source" not in (a.get("name") or "").lower()]
        hits.sort(key=lambda a: (not re.search(r"(?i)ps5|ppsa|prospero", a.get("name") or ""), -(a.get("size") or 0)))
        return hits[0] if hits else None
    a = pick((".zip", ".7z", ".rar"))
    if a:
        job = new_job([a["browser_download_url"]], "", "/data/homebrew", title="%s %s" % (x["name"], tag or ""), where="ps5")
    else:
        a = pick((".pkg",))
        if a:
            job = new_job([a["browser_download_url"]], "", "/data/pkg", title="%s %s" % (x["name"], tag or ""), where="ps5",
                          raw=True)
        elif pick((".elf", ".bin")):
            raise E.PipeError("%s es un payload: cárgalo con Payload Manager o con el cargador de ELF." % x["name"])
        else:
            raise E.PipeError("La última versión de %s no trae nada que sepa instalar. Míralo en github.com/%s/releases"
                              % (x["name"], x["repo"]))
    with WAKE:
        JOBS.append(job)
        WAKE.notify_all()
    save_jobs()
    return {"file": a["name"], "pkg": a["name"].lower().endswith(".pkg")}


PATCH_DIR = os.path.join(DATA_DIR, "parches")


def _patches_zip(max_age=86400):
    """patch1.zip de PS-Game-Patch (los parches de la comunidad), guardado en el PC y renovado una vez al día."""
    import zipfile
    path = os.path.join(PATCH_DIR, "patch1.zip")
    fresh = os.path.isfile(path) and time.time() - os.path.getmtime(path) < max_age
    if not fresh:
        try:
            tag, assets = F.github_release("illusionyy/PS-Game-Patch")
            a = next((x for x in assets if re.match(r"(?i)^patch.*\.zip$", x.get("name") or "")), None)
            if not a:
                raise E.PipeError("La última versión de los parches no trae patch1.zip")
            req = urllib.request.Request(a["browser_download_url"], headers={"User-Agent": "PS5Directo/" + VERSION})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read(80 * 1024 * 1024)
            os.makedirs(PATCH_DIR, exist_ok=True)
            with open(path + ".part", "wb") as fh:
                fh.write(data)
            os.replace(path + ".part", path)
        except Exception:
            if not os.path.isfile(path):
                raise
    return zipfile.ZipFile(path)


def patches_index():
    """Title ID -> nombres de los parches que hay para él (para avisar en la biblioteca)."""
    idx_path = os.path.join(PATCH_DIR, "indice.json")
    try:
        z = _patches_zip()
        src = os.path.getmtime(os.path.join(PATCH_DIR, "patch1.zip"))
        if os.path.isfile(idx_path) and os.path.getmtime(idx_path) >= src:
            with open(idx_path, encoding="utf-8") as fh:
                return json.load(fh)
        idx = {}
        for n in z.namelist():
            if not n.lower().endswith(".xml"):
                continue
            t = z.read(n)[:200000].decode("utf-8", "replace")
            names = re.findall(r'(?i)<Metadata[^>]*\bName="([^"]{1,80})"', t) or re.findall(r'(?i)\bName="([^"]{1,80})"', t)[:3]
            for tid in set(re.findall(r"(?:PPSA|CUSA)\d{5}", t.upper())):
                cur = idx.setdefault(tid, [])
                for nm in names[:6]:
                    if nm not in cur:
                        cur.append(nm)
        with open(idx_path, "w", encoding="utf-8") as fh:
            json.dump(idx, fh, ensure_ascii=False)
        return idx
    except Exception:
        return {}


def patches_sync():
    """Deja los parches de la comunidad (PS-Game-Patch) en /data/user/patches de la consola."""
    z = _patches_zip(max_age=3600)
    files = [n for n in z.namelist() if not n.endswith("/") and re.search(r"(?i)(^|/)xml_(prospero|orbis)/", n)]
    if not files:
        raise E.PipeError("patch1.zip no trae las carpetas xml_prospero ni xml_orbis")
    def run(f):
        done = 0
        for n in files:
            rel = n[n.lower().index("xml_"):]
            dst = "/data/user/patches/" + rel
            E.Ftp.mkdirs(f, posixpath.dirname(dst))
            f.storbinary("STOR " + dst, io.BytesIO(z.read(n)))
            done += 1
        return done
    n = E.Ftp(cfg_snapshot()).call(run)
    return {"files": n}



# ---------------------------------------------------------------------------
# Aviso en la pantalla de la PS5 al terminar un envío (con zftpd cargado: su web acepta /api/notify)
# ---------------------------------------------------------------------------
def ps5_toast(text):
    if not SETTINGS.get("ps5_toasts", True):
        return
    host = (SETTINGS.get("ps5_ip") or "").split(":")[0]
    if not host:
        return
    def go():
        try:
            u = "http://%s:%d/api/notify?text=%s" % (host, F.Zftpd.PORT, urllib.parse.quote(str(text)[:120]))
            urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "PS5Directo/" + VERSION}), timeout=4).read(64)
        except Exception:
            pass                                  # sin zftpd no hay aviso, y no pasa nada
    threading.Thread(target=go, daemon=True).start()



def os_check():
    """Firmware que tienes puesto, si hay Payload Manager y qué tiene en marcha (sin lo del propio sistema)."""
    cfg = cfg_snapshot()
    pm = F.PayloadManager(cfg["ps5_ip"])
    ver, procs, names = "", [], []
    try:
        ver = pm.version()
        if ver:
            procs = pm.processes()
    except Exception:
        pass
    for x in procs:
        n = str(x.get("name") or "")
        if n and not n.startswith("Sce") and n not in names:
            names.append(n)
    return {"fw": SETTINGS.get("console_fw") or "", "pm": bool(ver), "procs": names[:40],
            "loader": _port_open(cfg["ps5_ip"], cfg.get("elf_port") or 9021, 1.5)}


def os_image(oid):
    """Pone en la cola la imagen de Linux o de Bazzite (la última publicada en su GitHub) para bajarla a tu PC."""
    if oid not in OS_IMAGES:
        raise E.PipeError("No lo conozco")
    repo, rxs = OS_IMAGES[oid]
    tag, assets = F.github_release(repo)
    a = None
    for rx in rxs:
        hits = [x for x in assets if re.search(rx, x.get("name") or "")]
        if hits:
            a = max(hits, key=lambda x: x.get("size") or 0)
            break
    if not a:
        raise E.PipeError("La última versión (%s) no trae una imagen que reconozca. Míralo en github.com/%s/releases"
                          % (tag or "?", repo))
    job = new_job([a["browser_download_url"]], "", SETTINGS["dest"], title=a["name"], where="keep", keep_steps="dl")
    with WAKE:
        JOBS.append(job)
        WAKE.notify_all()
    save_jobs()
    return {"file": a["name"], "size": a.get("size") or 0, "tag": tag}


def rec_repo(c):
    """De qué GitHub se baja este programa: el oficial, otra versión conocida (fork) o una que hayas añadido tú."""
    want = (SETTINGS.get("rec_src") or {}).get(c["id"]) or ""
    return want if re.match(r"^[\w.-]+/[\w.-]+$", want) else c["repo"]


def rec_source(cid, repo):
    c = next((x for x in F.CATALOG if x["id"] == cid and x["kind"] == "app"), None)
    if not c:
        raise E.PipeError("No lo conozco")
    repo = (repo or "").strip()
    m = re.match(r"^(?:https?://github\.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?(?:/.*)?$", repo)
    if repo and not m:
        raise E.PipeError("Pon el enlace de GitHub del proyecto (github.com/usuario/proyecto)")
    repo = m.group(1) if repo else ""
    if repo and repo.lower() != c["repo"].lower():
        tag, assets = F.github_release(repo)          # que exista y tenga una versión con algo instalable
        if not F.pick_asset(assets, c["asset"]):
            raise E.PipeError("La última versión de %s (%s) no trae un .zip que sepa instalar" % (repo, tag or "?"))
    src = dict(SETTINGS.get("rec_src") or {})
    if repo and repo.lower() != c["repo"].lower():
        src[cid] = repo
    else:
        src.pop(cid, None)
    SETTINGS["rec_src"] = src
    save_settings()
    REC_LATEST.pop(rec_repo(c), None)
    return {"repo": rec_repo(c)}

# ---------------------------------------------------------------------------
# «Añadidos por ti»: cualquier aplicación (homebrew en .zip) o payload (.elf) de GitHub que no esté en Descubrir.
# Se tratan como uno más: se instalan desde su GitHub, se abren y avisan de las versiones nuevas.
# ---------------------------------------------------------------------------
MINE_FILE = os.path.join(DATA_DIR, "mis_apps.json")
MINE_MAX = 40


class _RangeFile(io.RawIOBase):
    """Un archivo de internet que se lee a trozos (Range): para mirar dentro de un .zip sin bajarlo entero."""

    def __init__(self, url):
        super().__init__()
        self.url, self.pos, self.n = url, 0, 0
        req = urllib.request.Request(url, headers=dict(F._ua(), Range="bytes=0-0"))
        with urllib.request.urlopen(req, timeout=25) as r:
            cr = r.headers.get("Content-Range") or ""
            if r.status != 206 or "/" not in cr:
                raise E.PipeError("El servidor no deja leer el archivo a trozos")
            self.size = int(cr.rsplit("/", 1)[1])
            self.url = r.geturl()

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        self.n += 1
        if self.n > 40:
            raise E.PipeError("Demasiadas lecturas")
        req = urllib.request.Request(self.url, headers=dict(F._ua(), Range="bytes=%d-%d" % (self.pos, self.pos + n - 1)))
        with urllib.request.urlopen(req, timeout=40) as r:
            data = r.read(n)
        self.pos += len(data)
        return data

    def readinto(self, b):
        d = self.read(len(b))
        b[:len(d)] = d
        return len(d)


def _zip_tid(url):
    """Title ID de la app que trae un .zip (su carpeta PPSA/CUSA de arriba, o el titleId de su param.json)."""
    import zipfile
    with zipfile.ZipFile(_RangeFile(url)) as z:
        names = z.namelist()
        tops = {n.split("/")[0] for n in names if n.strip("/")}
        for t in tops:
            if re.fullmatch(r"(?i)(PPSA|CUSA)\d{5}", t):
                return t.upper()
        pj = [n for n in names if n.lower().endswith("sce_sys/param.json") and n.count("/") == 2]
        if pj and len(tops) == 1:
            try:
                tid = json.loads(z.read(pj[0]).decode("utf-8", "replace")).get("titleId") or ""
            except Exception:
                tid = ""
            if re.fullmatch(r"(?i)(PPSA|CUSA)\d{5}", tid) and tid.upper() == list(tops)[0].upper():
                return tid.upper()
    return ""


def _mine_read():
    try:
        with open(MINE_FILE, encoding="utf-8") as fh:
            return [x for x in json.load(fh) if isinstance(x, dict) and x.get("id") and x.get("repo")][:MINE_MAX]
    except Exception:
        return []


def _mine_apply(lst):
    """Mete los añadidos en el catálogo (y los .elf en la lista de payloads), quitando los que ya no estén."""
    ids = {x["id"] for x in lst}
    F.CATALOG[:] = [c for c in F.CATALOG if not c.get("mine")]
    F.EXTRA_PAYLOADS[:] = [p for p in F.EXTRA_PAYLOADS if not p.get("mine")]
    for x in lst:
        c = {"id": x["id"], "kind": x["kind"], "group": "mine", "name": x["name"], "repo": x["repo"], "mine": True,
             "what": x.get("what") or "Añadido por ti desde GitHub.", "sys": x.get("sys") or ""}
        if x["kind"] == "app":
            c["tid"] = x["tid"]
            c["asset"] = x["asset"]
        else:
            F.EXTRA_PAYLOADS.append({"id": x["id"], "name": x["name"], "repo": x["repo"], "port": None,
                                     "pick": x["asset"], "what": c["what"], "mine": True})
        F.CATALOG.append(c)
    return ids


def _asset_rx(name, ext):
    """Patrón que reconoce el mismo archivo en versiones futuras (sin los números de versión)."""
    stem = re.split(r"[-_ .]?v?\d", name, 1)[0]
    out = []
    if len(stem) >= 3:
        out.append(r"(?i)^" + re.escape(stem) + r".*\." + ext + r"$")
    out.append(r"(?i)\." + ext + r"$")
    return out


def mine_add(text):
    m = re.match(r"^\s*(?:https?://)?(?:www\.)?(?:github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?].*)?\s*$", text or "")
    if not m:
        raise E.PipeError("Pega el enlace de GitHub del proyecto (github.com/autor/proyecto)")
    repo = "%s/%s" % (m.group(1), m.group(2))
    for c in F.CATALOG:
        if c.get("mine"):
            continue
        if (c.get("repo") or "").lower() == repo.lower() or any(a.lower() == repo.lower() for a in c.get("alts") or []):
            raise E.PipeError("%s ya está en Descubrir: búscalo como «%s»" % (repo, c["name"]))
    lst = _mine_read()
    if len(lst) >= MINE_MAX:
        raise E.PipeError("Ya tienes %d añadidos: quita alguno antes" % MINE_MAX)
    try:
        tag, assets = F.github_release(repo)
    except E.PipeError as e:
        if "404" in str(e):
            raise E.PipeError("No encuentro %s en GitHub, o no tiene ninguna versión publicada (Releases)" % repo)
        raise
    names = [a.get("name") or "" for a in assets]
    what, real = "", repo
    try:
        req = urllib.request.Request(F.GH_API + "/repos/%s" % repo, headers=F._ua())
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.load(r)
        what, real = (j.get("description") or "").strip()[:300], j.get("full_name") or repo
    except Exception:
        pass
    ent = {"id": "gh-" + re.sub(r"[^a-z0-9]+", "-", real.lower()).strip("-"), "repo": real,
           "name": real.split("/")[1], "what": what, "added": int(time.time())}
    zips = [a for a in assets if (a.get("name") or "").lower().endswith(".zip")]
    elfs = [a for a in assets if (a.get("name") or "").lower().endswith(".elf")]
    if zips:
        zips.sort(key=lambda a: (not re.search(r"(?i)(PPSA|CUSA)\d{5}", a["name"]), -int(a.get("size") or 0)))
        a = zips[0]
        tid = ""
        try:
            tid = _zip_tid(a["browser_download_url"])
        except Exception as e:
            print("No pude mirar dentro de %s: %s" % (a["name"], e))
            mt = re.search(r"(?i)(PPSA|CUSA)\d{5}", a["name"])
            tid = mt.group(0).upper() if mt else ""
        if not tid:
            if not elfs:
                raise E.PipeError("El .zip de %s (%s) no trae la carpeta de una aplicación (PPSA… o CUSA…): "
                                  "instálalo a mano siguiendo su guía" % (real, a["name"]))
        else:
            ent.update(kind="app", tid=tid, asset=_asset_rx(a["name"], "zip"), sys="Aplicación · " + tid)
    if "kind" not in ent and elfs:
        elfs.sort(key=lambda a: len(a["name"]))
        ent.update(kind="payload", asset=_asset_rx(elfs[0]["name"], "elf"), sys="Payload")
    if "kind" not in ent:
        raise E.PipeError("La última versión de %s (%s) no trae nada que sepa instalar (un .zip con la aplicación o un "
                          ".elf). Archivos: %s" % (real, tag or "?", ", ".join(names[:6]) or "ninguno"))
    lst = [x for x in lst if x["id"] != ent["id"]] + [ent]
    with LOCK:
        _save_json(MINE_FILE, lst)
        _mine_apply(lst)
    return {"id": ent["id"], "name": ent["name"], "kind": ent["kind"], "tag": tag}


def mine_del(cid):
    lst = [x for x in _mine_read() if x["id"] != cid]
    with LOCK:
        _save_json(MINE_FILE, lst)
        _mine_apply(lst)
    return {}


try:
    _mine_apply(_mine_read())
except Exception as _e:
    print("No pude leer tus añadidos de GitHub: %s" % _e)


def rec_install(cid, update=False):
    """Pone en la cola la ultima version oficial (su GitHub) de un programa de «Recomendados». Con update, lo nuevo
    se copia encima de lo que hay (se conservan ajustes, partidas y lo que tengas dentro de su carpeta)."""
    c = next((x for x in F.CATALOG if x["id"] == cid and x["kind"] == "app"), None)
    if not c:
        raise E.PipeError("No lo conozco")
    repo = rec_repo(c)
    tag, assets = F.github_release(repo)
    REC_LATEST[repo] = (time.time(), tag)
    a = F.pick_asset(assets, c["asset"])
    if not a:
        raise E.PipeError("La última versión de %s (%s) no trae un archivo que sepa instalar. Míralo en su GitHub: "
                          "github.com/%s/releases" % (c["name"], tag or "?", repo))
    job = new_job([a["browser_download_url"]], "", "/data/homebrew", title="%s %s" % (c["name"], tag), where="ps5")
    job["exact_name"] = True
    job["fmt_set"] = True
    job["rec_ver"] = {"id": cid, "tag": tag}
    if update:
        job["merge_into"] = True
        job["title"] = "%s: actualizar a %s" % (c["name"], tag)
    with WAKE:
        JOBS.append(job)
        WAKE.notify_all()
    save_jobs()
    return {"id": job["id"], "tag": tag, "file": a.get("name")}


# ---------------------------------------------------------------------------
# Emuladores de «Recomendados»: version instalada frente a la de su GitHub, poner tu BIOS, enviar tus juegos a su
# carpeta, y ver (y retocar) sus ajustes y registros.
# ---------------------------------------------------------------------------

EMU_VER_FILE = os.path.join(DATA_DIR, "emu_versions.json")
REC_LATEST = {}          # repo -> (t, tag)
EMU_T = [0.0]
EMU_EDIT_EXT = (".ini", ".cfg", ".json", ".txt")


def _emu_vers():
    try:
        with open(EMU_VER_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def rec_latest(repo, max_age=6 * 3600):
    hit = REC_LATEST.get(repo)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    try:
        tag = F.github_release(repo)[0]
    except Exception:
        return hit[1] if hit else ""
    REC_LATEST[repo] = (time.time(), tag)
    return tag


GH_NOTES = {}        # repo -> (hora, [releases])


def _md_translate(body, to):
    """Traduce las notas de una versión línea a línea, sin romper listas, títulos ni código."""
    lines = body.split("\n")
    idx, texts, code = [], [], False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```"):
            code = not code
            continue
        m = re.match(r"^(\s*(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+|>\s*)*)(.*)$", ln)
        txt = m.group(2).strip()
        if code or not re.search(r"[A-Za-zÀ-ÿ]{3}", txt) or re.fullmatch(r"(!?\[[^\]]*\]\([^)]*\)|<[^>]+>|https?://\S+)\s*", txt):
            continue
        idx.append((i, m.group(1)))
        texts.append(txt)
        if len(texts) >= 160:
            break
    if not texts:
        return body
    out = tr_texts(texts, to)
    for (i, pre), t in zip(idx, out):
        if t:
            t = re.sub(r"\*\*\s*(.+?)\s*\*\*", r"**\1**", t)
            t = re.sub(r"`\s*([^`]+?)\s*`", r"`\1`", t)
            lines[i] = pre + t
    return "\n".join(lines)


def gh_notes(repo, have="", to=""):
    """Las notas de las versiones de un proyecto de GitHub más nuevas que la que tienes (como mucho 6)."""
    repo = (repo or "").strip().strip("/")
    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        raise E.PipeError("Ese proyecto no está en GitHub")
    hit = GH_NOTES.get(repo.lower())
    if hit and time.time() - hit[0] < 1800:
        rels = hit[1]
    else:
        try:
            req = urllib.request.Request(F.GH_API + "/repos/%s/releases?per_page=15" % repo, headers=F._ua())
            with urllib.request.urlopen(req, timeout=20) as r:
                rels = [x for x in json.load(r) if not x.get("draft")]
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise E.PipeError("GitHub ha limitado las consultas desde tu conexión: prueba en un rato o léelas en GitHub")
            raise E.PipeError("No he podido leer las notas en GitHub (%s)" % E.net_reason(e))
        except Exception as e:
            raise E.PipeError("No he podido leer las notas en GitHub (%s)" % E.net_reason(e))
        GH_NOTES[repo.lower()] = (time.time(), rels)
    norm = lambda t: re.sub(r"^[vV]", "", (t or "").strip()).lower()
    have_n = norm(have)
    out = []
    for x in rels:
        tag = x.get("tag_name") or ""
        if have_n and have_n not in ("propio",):
            if norm(tag) == have_n:
                break
            if re.search(r"\d", have_n) and re.search(r"\d", tag) and _vtuple(tag) < _vtuple(have_n):
                break
        if x.get("prerelease") and out == [] and any(not y.get("prerelease") for y in rels):
            continue          # las de prueba solo si no hay otra cosa
        out.append({"tag": tag, "name": (x.get("name") or tag).strip(), "date": (x.get("published_at") or "")[:10],
                    "url": x.get("html_url") or "", "body": (x.get("body") or "").strip()[:8000]})
        if len(out) >= (6 if have_n else 1):
            break
    if to:
        for it in out:
            it["body"] = _md_translate(it["body"], to)
    return {"items": out, "url": "https://github.com/%s/releases" % repo}


# ---------------------------------------------------------------------------
# Jugar en el PC: PS5 Directo no emite nada; abre chiaki-ng (si lo tienes) directo en tu consola
# ---------------------------------------------------------------------------
CHIAKI_FLATPAK = "io.github.streetpea.Chiaki4deck"


def chiaki_find():
    """El chiaki-ng del PC: el que elegiste, o el de los sitios donde suele instalarse."""
    import glob
    import shutil
    own = (SETTINGS.get("chiaki_path") or "").strip()
    if own and os.path.isfile(own):
        return [own]
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA") or "", "Programs"), os.environ.get("LOCALAPPDATA"),
                     os.path.join(os.path.expanduser("~"), "Downloads"), os.path.join(os.path.expanduser("~"), "Desktop")):
            if base:
                cands += glob.glob(os.path.join(base, "chiaki*", "chiaki*.exe")) + \
                    glob.glob(os.path.join(base, "chiaki*", "*", "chiaki*.exe"))
    elif sys.platform == "darwin":
        cands += glob.glob("/Applications/chiaki*.app/Contents/MacOS/chiaki*") + \
            glob.glob(os.path.expanduser("~/Applications/chiaki*.app/Contents/MacOS/chiaki*"))
    else:
        for n in ("chiaki-ng", "chiaki"):
            w = shutil.which(n)
            if w:
                cands.append(w)
        cands += glob.glob(os.path.expanduser("~/Applications/chiaki*.AppImage")) + \
            glob.glob(os.path.expanduser("~/Downloads/chiaki*.AppImage"))
    cands = [c for c in cands if os.path.isfile(c) and not re.search(r"(?i)unins|setup|install", os.path.basename(c))]
    if cands:
        return [sorted(cands, key=lambda c: ("ng" not in c.lower(), len(c)))[0]]
    if os.name != "nt" and sys.platform != "darwin" and shutil.which("flatpak"):
        try:
            r = subprocess.run(["flatpak", "info", CHIAKI_FLATPAK], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            if r.returncode == 0:
                return ["flatpak", "run", CHIAKI_FLATPAK]
        except Exception:
            pass
    return None


def ps5_discover(host, timeout=1.5):
    """Pregunta a la consola por la red local si está encendida o en reposo (el «SRCH» que usa cualquier
    Remote Play: sin cuentas ni claves). Devuelve {"state": "on"|"standby", "name": ...} o None."""
    import socket
    if not host:
        return None
    msg = b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n"
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.settimeout(timeout)
        try:
            for _ in range(2):
                sk.sendto(msg, (host, 9302))
                try:
                    data = sk.recv(2048).decode("utf-8", "replace")
                    break
                except socket.timeout:
                    data = ""
        finally:
            sk.close()
    except OSError:
        return None
    if not data:
        return None
    head = data.split("\n", 1)[0]
    kv = dict(ln.split(":", 1) for ln in data.splitlines()[1:] if ":" in ln)
    return {"state": "on" if " 200" in head else "standby" if "620" in head else "other",
            "name": (kv.get("host-name") or "").strip()}


def rp_state():
    exe = chiaki_find()
    host = E.clean_host(SETTINGS.get("ps5_ip") or "")[0]
    d = ps5_discover(host)
    return {"chiaki": bool(exe), "path": (exe or [""])[-1] if exe and exe[0] != "flatpak" else ("Flatpak" if exe else ""),
            "own": bool(SETTINGS.get("chiaki_path")), "host": host, "console": d,
            "nick": SETTINGS.get("rp_nick") or (d or {}).get("name") or ""}


def rp_play(open_only=False, full=False, ds=False, nick=""):
    exe = chiaki_find()
    if not exe:
        raise E.PipeError("No encuentro chiaki-ng en este PC: instálalo o dime dónde está")
    host = E.clean_host(SETTINGS.get("ps5_ip") or "")[0]
    nick = (nick or SETTINGS.get("rp_nick") or "").strip()
    if not open_only:
        d = ps5_discover(host) or {}
        nick = nick or d.get("name") or ""
        if not host or not nick:
            raise E.PipeError("No sé el nombre de tu consola: escríbelo tal como sale en chiaki-ng")
        if d.get("state") == "standby":
            open_only = True       # dormida: chiaki-ng normal la despierta al elegirla
    args = list(exe) + ([] if open_only else ["stream", nick, host] + (["--fullscreen"] if full else []) +
                        (["--dualsense"] if ds else []))
    kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     cwd=os.path.dirname(exe[-1]) if exe[0] != "flatpak" else None, **kw)
    return {"opened": "app" if open_only else "stream"}


def emu_news():
    """Cada 6 h: si hay version nueva de un emulador que tienes, avisa en la campana (una vez por version)."""
    vers = _emu_vers()
    for c in F.CATALOG:
        if c.get("kind") != "app" or c["id"] not in vers:
            continue
        new = rec_latest(rec_repo(c))
        if new and new != vers[c["id"]]:
            notify("info", "Versión nueva de %s: %s" % (c["name"], new),
                   "Tienes la %s. En Descubrir, «Qué cambia» te enseña sus novedades y «Actualizar» la pone sin tocar "
                   "tus ajustes, partidas, BIOS ni contenido." % vers[c["id"]], {"tab": "Library", "view": "rec"}, key="emu:%s:%s" % (c["id"], new))


def _emu_cat(cid):
    c = next((x for x in F.CATALOG if x["id"] == cid and x.get("emu")), None)
    if not c:
        raise E.PipeError("No es un emulador que conozca")
    return c


def _emu_join(base, sub):
    """Carpeta de un emulador: relativa a su base, absoluta, o la propia base («.»)."""
    sub = (sub or "").strip()
    if sub in ("", "."):
        return base
    if sub.startswith("/"):
        return sub
    return base.rstrip("/") + "/" + sub


def emu_dirs(f, c):
    """Carpeta del emulador instalado y la carpeta donde guarda tus cosas."""
    em = c["emu"]
    app = None
    for t in [c["tid"]] + list(c.get("also") or []):
        for base in ("/data/homebrew", "/mnt/ext0/homebrew", "/mnt/ext1/homebrew", "/mnt/usb0/homebrew",
                     "/mnt/usb1/homebrew", "/user/app"):
            if _ftp_isdir(f, "%s/%s" % (base, t)):
                app = "%s/%s" % (base, t)
                break
        if app:
            break
    data_on = _ftp_isdir(f, em["data"])
    base = em["data"] if (em.get("fixed") or data_on) else app
    return {"app": app, "data": em["data"], "data_on": data_on, "base": base}


def _ftp_files(f, d):
    try:
        return [(n, v) for n, v in E.ftp_listdir(f, d)]
    except ftplib.all_errors:
        return None


def emu_info(cid):
    c = _emu_cat(cid)
    em = c["emu"]

    def work(f):
        d = emu_dirs(f, c)
        out = {"dirs": d, "bios": [], "games": None, "files": [], "extra": []}
        if not d["base"]:
            return out

        def files_in(sub):
            if not sub:
                return None
            lst = _ftp_files(f, _emu_join(d["base"], sub))
            if lst is None:
                return None
            return [{"name": n, "size": v.get("size") if isinstance(v, dict) else None,
                     "dir": bool(isinstance(v, dict) and v.get("type") == "dir")} for n, v in lst]
        out["bios"] = (files_in(em["bios"]) or []) if em.get("bios") else []
        g = files_in(em.get("games"))
        out["games"] = None if g is None else len([x for x in g if not x["dir"]]) + len([x for x in g if x["dir"]])
        for x in em.get("extra_dirs") or []:
            lst = files_in(x["dir"])
            out["extra"].append({"dir": x["dir"], "what": x["what"], "n": len(lst) if lst is not None else None})
        for rel in em.get("files") or []:
            roots = [d["base"]] + ([d["app"]] if d["app"] and d["app"] != d["base"] else [])
            if rel.endswith("/"):
                for r in roots:
                    lst = _ftp_files(f, r + "/" + rel.rstrip("/"))
                    if lst:
                        for n, v in lst[:40]:
                            if isinstance(v, dict) and v.get("type") == "dir":
                                continue
                            out["files"].append({"path": "%s/%s%s" % (r, rel, n), "rel": rel + n,
                                                 "size": v.get("size") if isinstance(v, dict) else None})
                        break
                continue
            for r in roots:
                parent, name = posixpath.split(r + "/" + rel)
                lst = _ftp_files(f, parent) or []
                hit = next((v for n, v in lst if n == name), None)
                if hit is not None:
                    out["files"].append({"path": parent + "/" + name, "rel": rel,
                                         "size": hit.get("size") if isinstance(hit, dict) else None})
                    break
        for x in out["files"]:
            x["edit"] = x["rel"].lower().endswith(EMU_EDIT_EXT[:3])
        return out
    out = E.Ftp(cfg_snapshot()).call(work)
    out.update(name=c["name"], id=cid, bios_what=em.get("bios_what"), bios_dir=em.get("bios") or "",
               games_dir=em.get("games") or "", games_label=em.get("games_label") or "", exp=bool(c.get("exp")),
               needs=c.get("needs") or "", installed=bool(out["dirs"].get("app")), tid=c["tid"],
               saves=any(e["id"] == c["tid"] or c["tid"] in (e.get("also") or []) for e in EMU_SAVES),
               guide=em.get("guide") or [], ver=_emu_vers().get(cid) or "", latest=rec_latest(rec_repo(c)))
    return out


def _emu_path_ok(cid, path):
    info = emu_info(cid)
    ok = {x["path"]: x for x in info["files"]}
    if path not in ok:
        raise E.PipeError("Ese archivo no es de %s" % info["name"])
    return ok[path]


def _zftpd_or_fail(what):
    z = F.Zftpd(cfg_snapshot().get("ps5_ip") or "")
    if not z.alive():
        raise E.PipeError("Para %s desde aquí hace falta zftpd en la consola: inícialo en Biblioteca › "
                          "⭐ Recomendados › zftpd y vuelve a probar." % what)
    return z


def title_launch(tid):
    """Abre en la consola un titulo instalado (con zftpd), como si lo eligieras en su menu."""
    tid = (tid or "").upper()
    if not re.match(r"^[A-Z]{4}\d{5}$", tid):
        raise E.PipeError("Title ID no válido")
    r = _zftpd_or_fail("abrirlo").launch(tid)
    if not r.get("ok") and str(r.get("status")) != "ok":
        raise E.PipeError("La consola no lo ha abierto: %s" % (r.get("message") or r.get("error") or "sin respuesta"))
    threading.Thread(target=after_launch, args=(tid,), daemon=True).start()
    return {"tid": tid}


PKG = {"path": "", "t": 0}


def pkg_install(path, overwrite=False):
    """Instala un .pkg que ya esta en la consola (con zftpd). No firma ni cambia nada: instala lo que tu le das."""
    path = fm_clean(path)
    if not path.lower().endswith(".pkg") or not path.startswith(("/data/", "/mnt/")):
        raise E.PipeError("Elige un archivo .pkg de la consola")
    z = _zftpd_or_fail("instalar .pkg")
    r = z.install(path, overwrite)
    if r.get("already"):
        return {"already": True, "path": path}
    if not r.get("ok", True):
        raise E.PipeError("La consola no aceptó el paquete: %s" % (r.get("message") or r.get("error") or "?"))
    PKG.update(path=path, t=time.time())
    return {"started": True, "path": path}


def pkg_state():
    try:
        st = F.Zftpd(cfg_snapshot().get("ps5_ip") or "").install_status()
    except Exception as e:
        return {"active": False, "error": str(e), "path": PKG["path"]}
    return {"active": bool(st.get("active")), "progress": st.get("progress") or 0, "error": st.get("error") or 0,
            "title_id": st.get("title_id") or "", "path": PKG["path"] or st.get("path") or ""}


def emu_read(cid, path):
    meta = _emu_path_ok(cid, path)
    buf = bytearray()
    size = meta.get("size") or 0
    start = max(0, size - 400 * 1024) if size and not meta["edit"] else 0

    def work(f):
        def cb(b):
            buf.extend(b)
            if len(buf) > 1024 * 1024:
                raise E.Cancelled()
        try:
            f.retrbinary("RETR " + path, cb, 65536, rest=start or None)
        except E.Cancelled:
            pass
    E.Ftp(cfg_snapshot()).call(work)
    return {"text": bytes(buf).decode("utf-8", "replace"), "cut": bool(start), "edit": meta["edit"], "path": path}


def emu_write(cid, path, text):
    meta = _emu_path_ok(cid, path)
    if not meta["edit"]:
        raise E.PipeError("Ese archivo solo se puede ver")
    data = str(text).encode("utf-8")
    if len(data) > 1024 * 1024:
        raise E.PipeError("Demasiado grande")

    def work(f):
        old = bytearray()
        f.retrbinary("RETR " + path, old.extend, 65536)
        f.storbinary("STOR " + path + ".bak", io.BytesIO(bytes(old)))
        f.storbinary("STOR " + path, io.BytesIO(data))
    E.Ftp(cfg_snapshot()).call(work)
    return {"saved": path, "backup": path + ".bak"}


def emu_put(cid, what, paths):
    """Pone en la cola tus archivos del PC (BIOS, contenido, claves...) para la carpeta que toca del emulador."""
    c = _emu_cat(cid)
    em = c["emu"]
    sub = {"bios": em.get("bios"), "games": em.get("games")}.get(what)
    if sub is None:
        sub = next((x["dir"] for x in em.get("extra_dirs") or [] if x["dir"] == what), None)
    if sub is None:
        raise E.PipeError("No sé dónde va eso")
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        raise E.PipeError("No has elegido nada")
    d = E.Ftp(cfg_snapshot()).call(lambda f: emu_dirs(f, c))
    if not d["base"]:
        raise E.PipeError("%s no está instalado en la consola" % c["name"])
    target = _emu_join(d["base"], sub)
    E.Ftp(cfg_snapshot()).call(lambda f: _ftp_makedirs(f, target))
    label = {"bios": "BIOS", "games": em.get("games_label") or "contenido"}.get(what, sub)
    job = new_job(paths, "", target, title="%s: %s (%d)" % (c["name"], label, len(paths)), raw=True)
    with WAKE:
        JOBS.append(job)
        WAKE.notify_all()
    save_jobs()
    return {"id": job["id"], "target": target}


def payload_send(pid=None, path=None):
    if pid and any(x["id"] == pid for x in F.EXTRA_PAYLOADS):
        r = extra_payload(pid, "send")
        return {"name": r.get("name") or pid, "tag": r.get("tag") or "", "up": r.get("running")}
    cfg = cfg_snapshot()
    if pid and F.PayloadManager(cfg["ps5_ip"]).version():
        # con Payload Manager abierto en la consola, lo carga el (el cargador 9021 suele ser solo local)
        r = console_fix(pid, "start")
        return {"name": r.get("name") or pid, "tag": "", "up": r.get("up")}
    if path and F.PayloadManager(cfg["ps5_ip"]).version():
        pm = F.PayloadManager(cfg["ps5_ip"])
        if not os.path.isfile(path):
            raise E.PipeError("No existe ese archivo")
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            pm.upload(name, fh.read(64 * 1024 * 1024))
        hit = next((x for x in pm.payloads() if x.get("filename") == name), None)
        if hit:
            pm.launch(hit["slot"])
        return {"name": name, "tag": "", "up": None}
    if pid:
        path, tag = F.fetch_payload(pid, DATA_DIR, log=print)
        name = next(p["name"] for p in F.PAYLOADS if p["id"] == pid)
    else:
        if not path or not os.path.isfile(path):
            raise E.PipeError("No existe ese archivo")
        name, tag = os.path.basename(path), ""
    with open(path, "rb") as fh:
        data = fh.read(64 * 1024 * 1024)
    F.send_elf(cfg["ps5_ip"], int(cfg.get("elf_port") or 9021), data)
    up = wait_service(pid, cfg) if pid else None
    try:
        test_connection(cfg)
    except Exception:
        pass
    return {"name": name, "tag": tag, "up": up}


# ---------------------------------------------------------------------------
# Abrir PS5 Directo desde el movil (misma red)
# ---------------------------------------------------------------------------

LAN = {"srv": None, "port": None, "error": ""}
LAN_HITS = []        # lo ultimo que ha pedido el movil: para saber si llega y si se corta
LAN_DEVICES = {}     # ip -> {name, first, last, n}: moviles que han entrado con el codigo actual
LAN_DENY = {"/api/archive/list", "/api/archived/reveal", "/api/emu/put", "/api/shots/send", "/api/cloud/install", "/api/cloud/add", "/api/cloud/manual", "/api/cloud/remove", "/api/trash/delete", "/api/col/reveal", "/api/forge/update", "/api/job/reveal", "/api/bk/restore", "/api/bk/open", "/api/report/open", "/api/col/set", "/api/col/libdir",
            "/api/col/openlibdir", "/api/lan/firewall", "/api/lan/hits", "/api/update/zip", "/api/update/github", "/api/quit", "/api/pick", "/api/open_ext_folder",
            "/api/fm/open_local", "/api/lan", "/api/forge/download", "/api/payload/file", "/api/fixperms",
            "/api/ext/openpage", "/api/ext/setup", "/api/ui/focus", "/api/youtube", "/api/bug/list",
            "/api/rp/play", "/api/rp/path"}
LAN_LOCKED = {"sevenzip_path", "forge_path", "chiaki_path", "pc_temp_dir", "pc_library_dir", "update_repo", "lan_access", "lan_port", "lan_token",
              "shots_tg_token", "shots_tg_chat", "shots_dc_hook"}


def token_ok(tok):
    if not tok:
        return False
    if secrets.compare_digest(str(tok), TOKEN):
        return True
    with LOCK:
        lt, on = SETTINGS.get("lan_token") or "", SETTINGS.get("lan_access")
    return bool(on and lt and secrets.compare_digest(str(tok), lt))


def lan_start():
    with LOCK:
        port = int(SETTINGS.get("lan_port") or 8780)
        if not SETTINGS.get("lan_token"):
            SETTINGS["lan_token"] = secrets.token_urlsafe(16)
    save_settings()
    if LAN["srv"]:
        return
    try:
        srv = Server(("0.0.0.0", port), LanHandler)
    except OSError as e:
        LAN["error"] = "No pude abrir el puerto %d (%s). Cambia el puerto o cierra lo que lo use." % (port, e)
        raise E.PipeError(LAN["error"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    LAN.update(srv=srv, port=port, error="")
    print("Acceso desde el movil: puerto %d" % port)


def lan_stop():
    srv = LAN.get("srv")
    LAN["srv"] = None
    if srv:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass


def lan_info():
    with LOCK:
        tok = SETTINGS.get("lan_token") or ""
        ip_ps5 = SETTINGS.get("ps5_ip") or ""
    out = {"enabled": bool(LAN["srv"]), "url": "", "svg": "", "error": LAN.get("error") or ""}
    if LAN["srv"]:
        ip = F.lan_ip(E.clean_host(ip_ps5)[0] or None)
        if ip:
            out["url"] = "http://%s:%d/#t=%s" % (ip, LAN["port"], tok)
            out["svg"] = F.qr_svg(out["url"])
            out["test_url"] = "http://%s:%d/prueba" % (ip, LAN["port"])
            # para el atajo de iPhone («Compartir» › Enviar a PS5 Directo) y las apps de Wake-on-LAN
            out["add_url"] = "http://%s:%d/api/add" % (ip, LAN["port"])
            out["token"] = tok
            try:
                out["pc_mac"] = F.mac_for_ip(ip)
            except Exception:
                out["pc_mac"] = ""
            out["test_svg"] = F.qr_svg(out["test_url"], scale=3)
        else:
            out["error"] = "No sé la IP de este PC en la red local"
        ts = F.tailscale_ip()                 # fuera de casa: el mismo PS5 Directo por la red privada de Tailscale
        if ts:
            out["ts_url"] = "http://%s:%d/#t=%s" % (ts, LAN["port"], tok)
            out["ts_svg"] = F.qr_svg(out["ts_url"])
    return out


# ---------------------------------------------------------------------------
# Servidor web local
# ---------------------------------------------------------------------------

TOKEN = secrets.token_urlsafe(18)


CONN_STATE = {"ftp": None, "bf": None}


def _conn_change(kind, ok, detail):
    """Avisa solo cuando algo que funcionaba deja de funcionar (y cuando vuelve)."""
    name = "el FTP" if kind == "ftp" else "BFpilot"
    prev = CONN_STATE[kind]
    CONN_STATE[kind] = ok
    if prev is True and not ok:
        notify("warn", "Se perdió la conexión con %s de la PS5" % name, detail)
    elif prev is False and ok:
        notify("info", "Otra vez conectado con %s de la PS5" % name)


def test_connection(cfg):
    out = _test_connection(cfg)
    _conn_change("ftp", out["ftp"] == "ok", out["ftp"])
    _conn_change("bf", str(out["bf"]).startswith("ok"), out["bf"])
    return out


def _test_connection(cfg):
    out = {"ftp": None, "bf": None, "free": None}
    try:
        f = E.Ftp(cfg).connect()
        f.quit()
        out["ftp"] = "ok"
    except Exception as e:
        out["ftp"] = "error: %s" % e
    bf = E.BFpilot(cfg)
    v = bf.alive()
    out["bf"] = ("ok (%s)" % v) if v else "no responde (cargalo desde Payload Manager)"
    if v:
        try:
            st = bf._req("GET", "/api/fs/stat", {"path": "/data"})
            out["free"] = human(int(st.get("availableBytes") or 0))
            out["free_bytes"] = int(st.get("availableBytes") or 0)
            out["total_bytes"] = int(st.get("usableTotalBytes") or st.get("totalBytes") or 0)
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Limpieza de restos en /data/ps5directo
# ---------------------------------------------------------------------------

def _job_dirs():
    """Carpetas de trabajo que todavia usa algun trabajo de la cola -> titulo."""
    used = {}
    with LOCK:
        jobs = [dict(j) for j in JOBS if j["status"] != "hecho"]
    for j in jobs:
        title = j.get("title") or "trabajo"
        srcs = j.get("sources") or []
        if srcs:
            used["%s/incoming/%s" % (WORK_ROOT, E.safe_name(E.set_key(srcs[0]["name"])))] = title
        used["%s/incoming/%s" % (WORK_ROOT, j["id"])] = title
        used["%s/extract/%s" % (WORK_ROOT, j["id"])] = title
    return used


def cleanup_scan():
    bf = E.BFpilot(cfg_snapshot())
    if not bf.alive():
        raise E.PipeError("BFpilot no responde: cargalo para poder buscar restos")
    used = _job_dirs()
    items = []

    def add(path, label):
        size = bf.du(path)
        owner = None
        for u, title in used.items():
            if path == u or path.startswith(u + ".") or path.startswith(u + "/"):
                owner = title
        items.append({"path": path, "label": label, "size": size or 0, "in_use": owner})

    try:
        top = bf.listdir(WORK_ROOT)
    except Exception:
        top = []
    for e in top:
        n = e["name"]
        if n in ("incoming", "extract") and e.get("dir"):
            try:
                sub = bf.listdir("%s/%s" % (WORK_ROOT, n))
            except Exception:
                sub = []
            for x in sub:
                add("%s/%s/%s" % (WORK_ROOT, n, x["name"]),
                    ("Partes enviadas: " if n == "incoming" else "Descompresion a medias: ") + x["name"])
        else:
            add("%s/%s" % (WORK_ROOT, n), "Otro: " + n)
    items.sort(key=lambda x: -x["size"])
    return items


def cleanup_delete(paths):
    with LOCK:
        if any(j["status"] in ("extrayendo", "colocando") for j in JOBS):
            raise E.PipeError("Espera a que termine la descompresion: BFpilot esta ocupado")
    bf = E.BFpilot(cfg_snapshot())
    done = []
    for p in paths:
        p = posixpath.normpath(str(p))
        if not p.startswith(WORK_ROOT + "/") or ".." in p:
            continue                                  # solo dentro de la carpeta de trabajo
        bf.delete(p)
        done.append(p)
    return done


# ---------------------------------------------------------------------------
# Limpiar restos (menu Espacio): envios a medias y archivos temporales que ocupan sitio
# en la consola y en el PC. Solo se puede borrar lo que salio en la ultima busqueda.
# ---------------------------------------------------------------------------

RESIDUE_RX = re.compile(r"(?i)\.(ps5part|ava-part|part|tmp|convirtiendo\.mp4)$")
RESIDUE_LAST = {}          # id -> elemento de la ultima busqueda


def _local_size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    n = 0
    for r, _ds, fs in os.walk(path):
        for f in fs:
            try:
                n += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return n


def residue_scan():
    cfg = cfg_snapshot()
    items, warn = [], []
    with LOCK:
        pending = [j.get("title") or "transferencia" for j in JOBS if j["status"] not in ("hecho", "cancelado")]
        fm_busy = FMOP.get("busy")

    def add(where, path, label, size, checked=True, in_use=None, note=""):
        items.append({"id": "%s:%s" % (where, path), "where": where, "path": path, "label": label,
                      "size": int(size or 0), "checked": checked and not in_use, "in_use": in_use, "note": note})

    # --- en el PC
    pc_dirs = [(pc_library_dir(), "Bajado de la Biblioteca"), (os.path.join(downloads_dir(), "PS5 Directo"), "Descargas"),
               (MEDIA_CONV_DIR(), "Capturas")]
    seen = set()
    for d, lab in pc_dirs:
        if not os.path.isdir(d) or d in seen:
            continue
        seen.add(d)
        for r, ds, fs in os.walk(d):
            if r.count(os.sep) - d.count(os.sep) > 3:
                ds[:] = []
            for f in fs:
                fp = os.path.join(r, f)
                if RESIDUE_RX.search(f) and os.path.normcase(fp) not in seen:
                    seen.add(os.path.normcase(fp))
                    add("pc", fp, "Bajada a medias (%s): %s" % (lab, f), _local_size(fp),
                        in_use="algo se está bajando ahora" if fm_busy else None)
    tmp = pc_temp_root(cfg)
    if os.path.isdir(tmp):
        for n in sorted(os.listdir(tmp)):
            fp = os.path.join(tmp, n)
            add("pc", fp, "Temporal de transferencias por el PC: " + n, _local_size(fp),
                in_use=("puede que lo use «%s» de la cola" % pending[0]) if pending else None)
    if os.path.isdir(PCX_DIR):
        for n in os.listdir(PCX_DIR):
            fp = os.path.join(PCX_DIR, n)
            if os.path.isdir(fp):
                add("pc", fp, "Descompresión a medias en el PC: " + n, _local_size(fp),
                    in_use=("puede que lo use «%s» de la cola" % pending[0]) if pending else None)
    if os.path.isdir(MEDIA_DIR):
        sz = _local_size(MEDIA_DIR)
        if sz:
            add("pc", MEDIA_DIR, "Copias guardadas para ver las capturas (se vuelven a bajar al abrirlas)", sz)

    # --- en la consola
    try:
        bf = fm_bf_opt()
        if bf is not None:
            for it in cleanup_scan():
                add("ps5", it["path"], it["label"], it["size"],
                    in_use=("lo usa «%s» de la cola" % it["in_use"]) if it.get("in_use") else None)
        else:
            warn.append("BFpilot no responde: en la consola solo miro los archivos a medias, no la carpeta de trabajo.")
        for root in ("/data/homebrew", "/data/pkg", "/data/etaHEN/games", "/mnt/usb0/homebrew", "/mnt/ext0/homebrew"):
            try:
                ents = fm_list(root)["entries"]
            except Exception:
                continue
            for e in ents:
                path = root + "/" + e["name"]
                if not e["dir"] and RESIDUE_RX.search(e["name"]):
                    add("ps5", path, "Envío a medias: " + e["name"], e.get("size"),
                        in_use=("puede que lo use «%s» de la cola" % pending[0]) if pending else None)
                elif e["dir"] and root.endswith("homebrew") and bf is not None and not bf.exists(path + "/sce_sys/param.json"):
                    add("ps5", path, "Carpeta sin sce_sys (¿envío cortado?): " + e["name"], bf.du(path) or 0,
                        checked=False, note="No es una aplicación completa. Márcala solo si sabes que es un envío que se cortó.")
    except Exception as e:
        warn.append("No pude revisar la consola: %s" % conn_error_text(e))
    items = [x for x in items if x["size"] or x["where"] == "ps5"]
    items.sort(key=lambda x: (x["where"] != "ps5", -x["size"]))
    with LOCK:
        RESIDUE_LAST.clear()
        RESIDUE_LAST.update({x["id"]: x for x in items})
    return {"items": items, "warn": warn}


def residue_delete(ids):
    import shutil
    with LOCK:
        sel = [RESIDUE_LAST[i] for i in ids if i in RESIDUE_LAST]
    freed, errs = 0, []
    con = [x for x in sel if x["where"] == "ps5"]
    bf = fm_bf_opt() if con else None
    for x in sel:
        try:
            if x["where"] == "pc":
                if os.path.isdir(x["path"]):
                    shutil.rmtree(x["path"])
                elif os.path.exists(x["path"]):
                    os.remove(x["path"])
            elif x["path"].startswith(WORK_ROOT + "/"):
                cleanup_delete([x["path"]])
            elif bf is not None:
                bf.delete(x["path"])
            else:
                ftp_rmtree(cfg_snapshot(), x["path"], threading.Event())
            freed += x["size"]
            with LOCK:
                RESIDUE_LAST.pop(x["id"], None)
        except Exception as e:
            errs.append("%s: %s" % (x["label"], conn_error_text(e)))
    return {"freed": freed, "errors": errs}


# ---------------------------------------------------------------------------
# Convertir capturas para compartir: videos a MP4 (H.264) y, si estan en HDR, a SDR para que no
# se vean lavados. Usa FFmpeg: en Windows se descarga solo la primera vez (33 MB).
# ---------------------------------------------------------------------------

FFMPEG_URL = "https://github.com/GyanD/codexffmpeg/releases/download/8.1/ffmpeg-8.1-essentials_build.7z"
FFMPEG_LOCK = threading.Lock()
CONV = {"busy": False, "phase": "", "i": 0, "n": 0, "pct": 0, "current": "", "done": [], "errors": [], "dir": ""}
CONV_CANCEL = threading.Event()


def MEDIA_CONV_DIR():
    return os.path.join(downloads_dir(), "PS5 Directo", "Capturas")


def media_mp4_path(path):
    """Donde queda el MP4 convertido de un clip de la consola."""
    path = fm_clean(str(path or ""))
    if not path.startswith(MEDIA_ROOT + "/"):
        return None
    return os.path.join(MEDIA_CONV_DIR(), os.path.splitext(posixpath.basename(path))[0] + ".mp4")


def ffmpeg_path():
    import shutil
    mine = os.path.join(DATA_DIR, "tools", "ffmpeg", "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if os.path.isfile(mine):
        return mine
    w = shutil.which("ffmpeg")
    if w:
        return w
    for c in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        if os.name != "nt" and os.path.isfile(c):
            return c
    return None


def ffmpeg_hint():
    if sys.platform == "darwin":
        return "Para convertir hace falta FFmpeg. Instálalo con Homebrew (brew.sh): brew install ffmpeg"
    return "Para convertir hace falta FFmpeg. Instálalo (por ejemplo: sudo apt install ffmpeg) y vuelve a probar."


def ffmpeg_install():
    """Solo Windows: baja FFmpeg (compilacion de gyan.dev publicada en GitHub) y guarda ffmpeg.exe."""
    import subprocess
    mine = os.path.join(DATA_DIR, "tools", "ffmpeg", "ffmpeg.exe")
    with FFMPEG_LOCK:
        if os.path.isfile(mine):
            return mine
        d = os.path.dirname(mine)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "ffmpeg.7z.part")
        total = 33541925

        def prog(got):
            with LOCK:
                CONV.update(pct=min(99, int(got * 100 / total)))
            if CONV_CANCEL.is_set():
                raise E.Cancelled()
        with LOCK:
            CONV.update(phase="Instalando el convertidor (FFmpeg, 33 MB, solo la primera vez)", pct=0)
        try:
            F.http_get(FFMPEG_URL, limit=200 * 1024 * 1024, timeout=120, dest=tmp, progress=prog)
            exe = E.find_7z(cfg_snapshot().get("sevenzip_path") or "")
            if not exe:
                raise E.PipeError("falta 7-Zip para abrir el paquete de FFmpeg")
            r = subprocess.run([exe, "e", "-y", "-o" + d, tmp, "ffmpeg.exe", "-r"], capture_output=True,
                               timeout=300, **({"creationflags": 0x08000000} if os.name == "nt" else {}))
            if r.returncode != 0 or not os.path.isfile(mine):
                raise E.PipeError("no pude sacar ffmpeg.exe del paquete")
        except E.Cancelled:
            raise
        except Exception as e:
            raise E.PipeError("No pude instalar FFmpeg (%s). Puedes bajarlo de gyan.dev/ffmpeg y copiar ffmpeg.exe en %s"
                              % (conn_error_text(e), d))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return mine


def _ff_probe(ff, src):
    import subprocess
    r = subprocess.run([ff, "-hide_banner", "-nostdin", "-i", src], capture_output=True, timeout=60,
                       **({"creationflags": 0x08000000} if os.name == "nt" else {}))
    txt = r.stderr.decode("utf-8", "replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", txt)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0
    vline = next((ln for ln in txt.splitlines() if "Video:" in ln), "")
    hdr = bool(re.search(r"smpte2084|arib-std-b67|bt2020", vline))
    h = re.search(r"\b\d{2,5}x(\d{2,5})\b", vline)
    return dur, hdr, int(h.group(1)) if h else 0, "Audio:" in txt


def _ff_convert(ff, src, dst, dur, hdr, height, audio):
    import subprocess
    vf = []
    if height > 1080:
        vf.append("scale=-2:1080")
    if hdr:
        vf.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                  "zscale=t=bt709:m=bt709:r=tv")
    vf.append("format=yuv420p")
    cmd = [ff, "-hide_banner", "-nostdin", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(vf),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-color_primaries", "bt709",
           "-color_trc", "bt709", "-colorspace", "bt709"]
    cmd += ["-c:a", "aac", "-b:a", "192k"] if audio else []
    cmd += ["-movflags", "+faststart", "-f", "mp4", "-progress", "pipe:1", "-nostats", dst]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         **({"creationflags": 0x08000000} if os.name == "nt" else {}))
    err = []
    threading.Thread(target=lambda: err.extend(p.stderr.read().decode("utf-8", "replace").splitlines()[-8:]),
                     daemon=True).start()
    for ln in p.stdout:
        if CONV_CANCEL.is_set():
            p.kill()
            break
        m = re.match(rb"out_time_(?:us|ms)=(\d+)", ln)
        if m and dur:
            with LOCK:
                CONV["pct"] = max(0, min(99, int(int(m.group(1)) / 1e6 * 100 / dur)))
    rc = p.wait()
    if CONV_CANCEL.is_set():
        raise E.Cancelled()
    if rc != 0:
        raise E.PipeError("FFmpeg falló: " + (" ".join(err[-2:]) or "código %d" % rc))


def media_convert(paths):
    with LOCK:
        if CONV["busy"]:
            raise E.PipeError("Ya se está convirtiendo")
        CONV.update(busy=True, phase="", i=0, n=len(paths), pct=0, current="", done=[], errors=[],
                    dir=MEDIA_CONV_DIR())
    CONV_CANCEL.clear()

    def run():
        try:
            ff = ffmpeg_path()
            if not ff:
                if os.name != "nt":
                    raise E.PipeError(ffmpeg_hint())
                ff = ffmpeg_install()
            out_dir = MEDIA_CONV_DIR()
            os.makedirs(out_dir, exist_ok=True)
            for k, path in enumerate(paths, 1):
                name = posixpath.basename(path)
                stem = os.path.splitext(name)[0]
                with LOCK:
                    CONV.update(i=k, current=name, pct=0, phase="Bajando de la consola")
                try:
                    src, _ = media_cached(path)
                    dst = os.path.join(out_dir, stem + ".mp4")
                    with LOCK:
                        CONV.update(phase="Convirtiendo a MP4")
                    dur, hdr, height, audio = _ff_probe(ff, src)
                    if hdr:
                        with LOCK:
                            CONV.update(phase="Convirtiendo a MP4 y quitando el HDR (tarda un poco más)")
                    tmp = os.path.join(out_dir, stem + ".convirtiendo.mp4")
                    _ff_convert(ff, src, tmp, dur, hdr, height, audio)
                    os.replace(tmp, dst)
                    with LOCK:
                        CONV["done"].append(os.path.basename(dst))
                except E.Cancelled:
                    try:
                        os.remove(os.path.join(out_dir, stem + ".convirtiendo.mp4"))
                    except OSError:
                        pass
                    raise
                except Exception as e:
                    with LOCK:
                        CONV["errors"].append("%s: %s" % (name, conn_error_text(e)))
            with LOCK:
                CONV.update(pct=100, phase="Hecho")
        except E.Cancelled:
            with LOCK:
                CONV.update(phase="Cancelado")
        except Exception as e:
            with LOCK:
                CONV["errors"].append(conn_error_text(e))
                CONV.update(phase="Error")
        finally:
            with LOCK:
                CONV["busy"] = False
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Test de velocidad: descarga sola, envio solo y los dos a la vez
# ---------------------------------------------------------------------------

SPEEDTEST = {"running": False, "result": None}


def _dl_test(url, cfg, seconds, stop):
    src = E.UrlSource(url).probe()
    got = [0]
    if not src.ranges or src.size <= 0:
        gen = E._single(src.url, stop, lambda n: None)
    else:
        start = src.size // 3        # mitad del archivo: no siempre el principio cacheado
        gen = E._ranged(src.url, start, src.size, cfg, stop, lambda n: None)
    t0 = time.time()
    try:
        for data in gen:
            got[0] += len(data)
            if time.time() - t0 >= seconds or stop.is_set():
                break
    finally:
        try:
            gen.close()
        except Exception:
            pass
    return got[0] / max(0.5, time.time() - t0)


def _up_test(cfg, seconds, stop):
    path = WORK_ROOT + "/speedtest.tmp"
    f = E.Ftp(cfg).connect()
    try:
        E.Ftp.mkdirs(f, WORK_ROOT)
        conn = f.transfercmd("STOR " + path)
        block = b"\0" * E.MiB
        sent = 0
        t0 = time.time()
        with conn:
            while time.time() - t0 < seconds and not stop.is_set():
                conn.sendall(block)
                sent += len(block)
        el = time.time() - t0
        try:
            f.voidresp()
        except Exception:
            pass
        try:
            f.delete(path)
        except Exception:
            pass
        return sent / max(0.5, el)
    finally:
        try:
            f.quit()
        except Exception:
            pass


CAL = {"running": False, "results": [], "best": None, "error": "", "now": None, "before": None}


def calibrate(url):
    """«Ajustar las conexiones»: prueba ese enlace con 2, 4, 8 y 16 conexiones a la vez y se queda con la mejor (si
    otra da casi lo mismo con menos conexiones, esa: es mas amable con el servidor)."""
    stop = threading.Event()
    with LOCK:
        CAL.update(running=True, results=[], best=None, error="", now=None, before=int(SETTINGS.get("streams") or 8))
    try:
        src = E.UrlSource(url).probe()
        if not src.ranges or src.size <= 0:
            raise E.PipeError("Este servidor no deja partir la descarga en trozos: da igual cuántas conexiones uses.")
        res = []
        for n in (2, 4, 8, 16):
            with LOCK:
                CAL["now"] = n
            cfg = cfg_snapshot()
            cfg["streams"] = n
            try:
                v = _dl_test(src.url, cfg, 7, stop)
            except Exception as e:
                v = 0.0
                print("Ajuste de conexiones (%d): %s" % (n, e))
            res.append({"n": n, "speed": v})
            with LOCK:
                CAL["results"] = list(res)
        top = max(x["speed"] for x in res)
        if top <= 0:
            raise E.PipeError("No he podido medir nada con ese enlace (¿ha caducado?).")
        best = min((x for x in res if x["speed"] >= top * 0.93), key=lambda x: x["n"])["n"]
        with LOCK:
            SETTINGS["streams"] = best
            CAL["best"] = best
        save_settings()
    except Exception as e:
        with LOCK:
            CAL["error"] = str(e)
    finally:
        with LOCK:
            CAL.update(running=False, now=None)


def run_speedtest(url, seconds=10):
    """Mide por separado para saber de quien es la culpa cuando va lento."""
    cfg = cfg_snapshot()
    res = {"url": bool(url), "dl": None, "up": None, "both_dl": None, "both_up": None, "pipe": None,
           "error": None, "verdict": ""}
    stop = threading.Event()
    try:
        if url:
            res["dl"] = _dl_test(url, cfg, seconds, stop)
        res["up"] = _up_test(cfg, seconds, stop)
        if url:
            box = {}
            th = threading.Thread(target=lambda: box.update(d=_dl_test(url, cfg, seconds, stop)))
            th.start()
            box["u"] = _up_test(cfg, seconds, stop)
            th.join()
            res["both_dl"], res["both_up"] = box.get("d"), box["u"]
    except Exception as e:
        res["error"] = str(e)
    mb = 1024 * 1024
    dl, up, both = res["dl"], res["up"], res["both_up"]
    v = []
    if up is not None:
        v.append("PC -> PS5 solo: %.1f MB/s." % (up / mb))
        if up < 6 * mb:
            v.append("Es bajo: el cuello es el Wi-Fi/cable entre PC y PS5 o el FTP de la consola.")
    if dl is not None:
        v.append("Descarga sola desde ese enlace: %.1f MB/s." % (dl / mb))
        if dl < 5 * mb:
            v.append("Es bajo: el alojamiento te esta dando poca velocidad ahora mismo "
                     "(limite de cuenta gratis, enlace castigado o servidor lento). No es tu red.")
    if dl and up and both is not None and res["both_dl"] is not None:
        ideal = 1 / (1 / dl + 1 / up)
        pipe = min(both, res["both_dl"])      # lo que llegaria de verdad descargando y enviando
        res["pipe"] = pipe
        v.append("Las dos cosas a la vez: en la practica llegarian unos %.1f MB/s a la PS5." % (pipe / mb))
        if dl >= 5 * mb:
            # solo tiene sentido cuando el enlace no es el cuello de botella
            v.append("Con una sola radio Wi-Fi lo maximo esperable seria unos %.1f MB/s." % (ideal / mb))
        if dl < 5 * mb:
            v.append("Aqui manda el alojamiento: mientras de tan poca velocidad, nada de tu lado "
                     "lo va a mejorar.")
        elif pipe < 0.6 * ideal:
            v.append("Estan compitiendo peor de lo normal: prueba el limite de velocidad en Ajustes "
                     "un poco por debajo de esa cifra, o un cable de red en el PC.")
        else:
            v.append("Es lo normal para una sola radio: compartir el aire tiene ese coste. "
                     "Solo un cable de red en el PC lo mejoraria de verdad.")
    res["verdict"] = " ".join(v)
    for k in ("dl", "up", "both_dl", "both_up", "pipe"):
        if res[k] is not None:
            res[k] = round(res[k] / mb, 2)
    return res


def _speedtest_thread(url):
    try:
        SPEEDTEST["result"] = run_speedtest(url)
    finally:
        SPEEDTEST["running"] = False


def net_mismatch(cfg):
    """Si el PC y la PS5 parecen estar en redes distintas (p. ej. 192.168.50.x y 192.168.1.x), lo explica."""
    host = E.clean_host(cfg.get("ps5_ip") or "")[0]
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", host or ""):
        return ""
    pc = F.lan_ip(host)
    if not pc or pc.rsplit(".", 1)[0] == host.rsplit(".", 1)[0]:
        return ""
    return ("Tu PC está en la red %s.x y la PS5 en la %s.x: parecen redes distintas (dos routers, la Wi-Fi de "
            "invitados o una VPN). Conecta los dos al mismo router o a la misma Wi-Fi; si cambia la IP de la PS5, "
            "ponla otra vez en Ajustes." % (pc.rsplit(".", 1)[0], host.rsplit(".", 1)[0]))


SLOW_MIN = 2 * 1024 * 1024        # por debajo de 2 MB/s no se vigila (no hay caida que avisar)


def speed_watch():
    """Avisa si la velocidad hacia la PS5 cae de golpe y no se recupera. Lo tipico: la PS5 lleva un rato sin
    que nadie toque el mando y baja el ritmo; al usar su menu vuelve a ir rapida."""
    hist = {}
    while True:
        time.sleep(5)
        now = time.time()
        try:
            with LOCK:
                live = [(j["id"], float(j.get("speed") or 0), j.get("title") or "") for j in JOBS
                        if j.get("status") == "enviando" and j.get("pc_step") not in ("bajar", "convertir")]
            ids = set()
            for jid, sp, title in live:
                ids.add(jid)
                h = hist.setdefault(jid, {"s": [], "alert": False})
                h["s"].append((now, sp))
                h["s"] = [x for x in h["s"] if now - x[0] <= 300]
                old = sorted(v for t, v in h["s"] if 30 <= now - t <= 300)
                new = [v for t, v in h["s"] if now - t < 25]
                if len(old) < 6 or len(new) < 4:
                    continue
                before = old[int(len(old) * 0.75)]       # lo que solia ir (sin picos)
                after = sum(new) / len(new)
                job = find(jid)
                if job is None:
                    continue
                if not h["alert"] and before >= SLOW_MIN and max(new) < before * 0.3:
                    h["alert"] = True
                    mb = lambda v: "%.1f" % (v / 1048576)
                    with LOCK:
                        job["slow"] = {"from": mb(before), "to": mb(after), "t": now}
                    jlog(job, "La velocidad a la PS5 ha bajado de %s a %s MB/s" % (mb(before), mb(after)))
                    notify("warn", "La velocidad ha bajado mucho: %s" % title,
                           "Ha pasado de %s a %s MB/s. Si la PS5 lleva un rato sin que nadie toque el mando, pulsa "
                           "un botón: cuando está inactiva va más lenta. Si no, puede ser el Wi-Fi o el servidor de "
                           "descarga." % (mb(before), mb(after)), {"tab": "Transfers", "job": jid},
                           key="slow:%s:%d" % (jid, int(now)))
                elif h["alert"] and after > before * 0.6:
                    h["alert"] = False
                    with LOCK:
                        job.pop("slow", None)
                    jlog(job, "La velocidad ha vuelto a subir")
            for jid in list(hist):
                if jid not in ids:
                    hist.pop(jid, None)
                    j = find(jid)
                    if j is not None:
                        with LOCK:
                            j.pop("slow", None)
        except Exception:
            traceback.print_exc()


def health_check():
    """Revision completa: que funciona, que no y que hacer."""
    cfg = cfg_snapshot()
    out = []

    def add(name, ok, detail, fix=""):
        out.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    f = None
    try:
        f = E.Ftp(cfg).connect()
        add("FTP de la PS5", True, "%s:%s responde" % (cfg["ps5_ip"], cfg["ftp_port"]))
        try:
            E.Ftp.mkdirs(f, WORK_ROOT)
            path = WORK_ROOT + "/prueba_escritura.tmp"
            import io
            f.storbinary("STOR " + path, io.BytesIO(b"x" * 65536))
            ok = E.Ftp.size(f, path) == 65536
            try:
                f.delete(path)
            except Exception:
                pass
            add("Escribir en la PS5", ok, "Se puede escribir en %s" % WORK_ROOT if ok else "La prueba no se guardo bien",
                "" if ok else "Revisa que el servidor FTP permita escribir en /data")
        except Exception as e:
            add("Escribir en la PS5", False, str(e), "El FTP no deja escribir: prueba a recargar el payload FTP")
    except Exception as e:
        add("FTP de la PS5", False, str(e),
            net_mismatch(cfg) or
            "Comprueba la IP en Ajustes y que el payload FTP este cargado (puerto %s)" % cfg["ftp_port"])
    finally:
        try:
            f and f.quit()
        except Exception:
            pass
    exe = E.find_7z(cfg.get("sevenzip_path"))
    where = cfg.get("extract_where") or "ps5"
    add("7-Zip en el PC", bool(exe) or where != "pc", exe or "no instalado",
        "" if exe else ("Hace falta para 'Descomprimir en el PC': instalalo desde 7-zip.org"
                        if where == "pc" else "Solo hace falta si eliges descomprimir en el PC"))
    bf = E.BFpilot(cfg)
    v = bf.alive()
    add("BFpilot", bool(v), ("version %s" % v) if v else "no responde en el puerto %s" % cfg["bf_port"],
        "" if v else "Cargalo desde Payload Manager: sin el no se puede descomprimir")
    if v:
        try:
            sup = bf._req("GET", "/api/fs/archive/support")
            add("Descompresion", True, "Formatos: %s" % ", ".join(sup.get("supported") or []))
        except Exception as e:
            add("Descompresion", False, str(e), "Actualiza BFpilot a la ultima version")
        free = free_space(bf)
        if free is not None:
            margin = margin_bytes()
            ok = free > margin + 10 * GiB
            add("Espacio libre", ok, "%s libres (margen %s)" % (human(free), human(margin)),
                "" if ok else "Queda poco: borra restos (Limpieza) o algo que no uses")
        try:
            st = bf._req("GET", "/api/fs/archive/status")
            if st.get("state") in ("running", "prepared"):
                add("Tarea en BFpilot", True, "Ahora mismo esta descomprimiendo: %s%%" % st.get("percent", "?"))
        except Exception:
            pass
    exe_f = F.find_forge(cfg.get("forge_path"), DATA_DIR)
    if (cfg.get("pc_format") or "folder") != "folder" or exe_f:
        add("PS5 Dump Forge", True, exe_f or "se descargará solo la primera vez que lo uses",
            "" if exe_f else "Hace falta para enviar como imagen .ffpkg/.ffpfsc o LZ4")
    if not cfg.get("console_fw"):
        add("Firmware de la PS5", True, "sin poner",
            "Ponlo en Ajustes (por ejemplo 13.60) y te aviso de lo que no arrancará")
    else:
        add("Firmware de la PS5", True, cfg["console_fw"])
    return out


# ---------------------------------------------------------------------------
# Actualizaciones: desde un .zip o desde GitHub
# ---------------------------------------------------------------------------

UPDATE_EXTS = (".py", ".html", ".txt", ".bat", ".vbs", ".sh", ".ico", ".js", ".json", ".png", ".css", ".md", ".svg")


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:4])


def _changelog(lang):
    """{version: [novedades]} sacado de LEEME.txt (es) o README_EN.txt (en)."""
    fn = "README_EN.txt" if lang == "en" else "LEEME.txt"
    try:
        with open(os.path.join(HERE, fn), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return {}
    out, cur, item = {}, None, None
    in_en = False
    for ln in lines:
        if lang == "en":
            if ln.startswith("WHAT'S NEW"):
                in_en = True
                continue
            if not in_en:
                continue
            if ln.strip() == "" and cur:
                break
            m = re.match(r"^(\d+\.\d+(?:\.\d+)?)\s+(.*)$", ln)
            if m:
                cur = m.group(1)
                out[cur] = []
                rest = m.group(2).strip()
                item = None
                ln = "      " + rest
        else:
            m = re.match(r"^NOVEDADES (\d+\.\d+(?:\.\d+)?)\s*$", ln)
            if m:
                cur = m.group(1)
                out[cur] = []
                item = None
                continue
            if cur and ln.strip() == "":
                cur = None
                continue
        if not cur:
            continue
        t = ln.strip()
        if t.startswith("- "):
            out[cur].append(t[2:])
        elif t and out[cur]:
            out[cur][-1] += " " + t
        elif t:
            out[cur].append(t)
    return out


def whats_new(lang, everything=False):
    """Novedades que ensenar tras actualizar (vacio si no toca). Con everything, las de las ultimas versiones."""
    if everything:
        log = _changelog(lang)
        entries = [{"version": v, "items": items} for v, items in log.items() if items and _ver_tuple(v) <= _ver_tuple(VERSION)]
        entries.sort(key=lambda e: _ver_tuple(e["version"]), reverse=True)
        return {"show": bool(entries), "version": VERSION, "entries": entries[:8], "all": True}
    with LOCK:
        seen = SETTINGS.get("seen_version") or ""
        tour_done = SETTINGS.get("tour_done")
    cur = _ver_tuple(VERSION)
    if seen and _ver_tuple(seen) >= cur:
        return {"show": False}
    if not seen:
        if not tour_done:                     # instalacion nueva: ya ve la guia de bienvenida
            with LOCK:
                SETTINGS["seen_version"] = VERSION
            save_settings()
            return {"show": False}
        # venia de una version sin este aviso: la anterior es la copia en .backup
        prev = None
        try:
            for d in os.listdir(os.path.join(HERE, ".backup")):
                if re.match(r"^\d+(\.\d+)+$", d) and _ver_tuple(d) < cur:
                    if prev is None or _ver_tuple(d) > _ver_tuple(prev):
                        prev = d
        except Exception:
            pass
        seen = prev
    log = _changelog(lang)
    pad = lambda t: (t + (0, 0, 0))[:3]
    entries = []
    for v, items in log.items():
        vt = pad(_ver_tuple(v))
        if vt <= pad(cur) and (seen is None and vt[:2] == pad(cur)[:2] or seen is not None and vt > pad(_ver_tuple(seen))):
            if items:
                entries.append({"version": v, "items": items})
    entries.sort(key=lambda e: _ver_tuple(e["version"]), reverse=True)
    return {"show": bool(entries), "version": VERSION, "entries": entries[:20]}


def install_zip(data):
    """Instala una version nueva desde los bytes de un .zip. Devuelve la version instalada."""
    import io
    import zipfile
    if busy():
        raise E.PipeError("Hay trabajos en marcha: espera a que terminen o pausalos antes de actualizar")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise E.PipeError("Ese archivo no es un .zip valido")
    names = z.namelist()
    roots = [n[:-len("app.py")] for n in names if n.endswith("app.py") and (n[:-len("app.py")] + "engine.py") in names]
    if not roots:
        raise E.PipeError("Ese .zip no es de PS5 Directo (no trae app.py y engine.py)")
    root = min(roots, key=len)
    m = re.search(r'VERSION = "([^"]+)"', z.read(root + "app.py").decode("utf-8", "replace"))
    if not m:
        raise E.PipeError("No encuentro la version dentro del .zip")
    new_ver = m.group(1)
    files = []
    for n in names:
        if not n.startswith(root) or n.endswith("/"):
            continue
        rel = n[len(root):]
        parts = rel.replace("\\", "/").split("/")
        if not rel or rel.startswith("/") or ".." in parts or ":" in rel:
            continue
        if not rel.lower().endswith(UPDATE_EXTS):
            continue
        files.append((n, rel))
    # copia de seguridad de lo que se va a reemplazar
    backup = os.path.join(HERE, ".backup", VERSION)
    for _, rel in files:
        cur = os.path.join(HERE, *rel.split("/"))
        if os.path.isfile(cur):
            dst = os.path.join(backup, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(cur, "rb") as fi, open(dst, "wb") as fo:
                fo.write(fi.read())
    for n, rel in files:
        dst = os.path.join(HERE, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".nuevo"
        with open(tmp, "wb") as fo:
            fo.write(z.read(n))
        os.replace(tmp, dst)
        if dst.endswith(".sh") and os.name != "nt":
            os.chmod(dst, 0o755)
    return new_ver


def restart_soon():
    """Arranca la version nueva y cierra esta (la nueva espera a que esta se cierre)."""
    import subprocess
    args = [sys.executable, os.path.join(HERE, "app.py"), "--after-update"]
    kw = {"cwd": HERE}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    def go():
        time.sleep(0.8)
        try:
            subprocess.Popen(args, **kw)
        finally:
            os._exit(0)
    threading.Thread(target=go, daemon=True).start()


def update_repo():
    with LOCK:
        return (SETTINGS.get("update_repo") or DEFAULT_REPO or "").strip()


def github_latest(repo):
    """(version, url_del_zip, notas) de la ultima release de GitHub."""
    repo = (repo or "").strip().strip("/")
    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        raise E.PipeError("Pon el repositorio como usuario/nombre en Ajustes")
    req = urllib.request.Request(F.GH_API + "/repos/%s/releases/latest" % repo,
                                 headers={"User-Agent": "PS5Directo/" + VERSION,
                                          "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code not in (403, 429):
            raise
        return _github_latest_web(repo)          # la API limita las consultas: se mira la web de releases
    url = None
    zips = [a for a in j.get("assets") or [] if a.get("name", "").lower().endswith(".zip")]
    # el mas ligero primero: "without-Python" (o el normal de antes); el que trae Python, el ultimo
    zips.sort(key=lambda a: (0 if "without-python" in a.get("name", "").lower() else
                             2 if ("portable" in a.get("name", "").lower() or "-windows.zip" in a.get("name", "").lower())
                             else 1))
    if zips:
        url = zips[0].get("browser_download_url")
    url = url or j.get("zipball_url")
    return (j.get("tag_name") or "").lstrip("vV"), url, (j.get("body") or "")[:12000]


UPD_DL = {"got": 0, "size": 0}


def _download_update(url, limit=60 * 1024 * 1024):
    """Baja el zip de la versión nueva sin límite de tiempo total: si la conexión se corta, sigue donde iba
    (hasta 5 veces). UPD_DL lleva lo bajado, para la barra."""
    buf = bytearray()
    UPD_DL.update(got=0, size=0)
    for attempt in range(6):
        hd = {"User-Agent": "PS5Directo/" + VERSION}
        if buf:
            hd["Range"] = "bytes=%d-" % len(buf)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hd), timeout=60) as r:
                if buf and r.status != 206:
                    buf = bytearray()          # el servidor no sabe seguir: de cero
                tot = r.headers.get("Content-Length")
                if tot and tot.isdigit():
                    UPD_DL["size"] = len(buf) + int(tot)
                while True:
                    ch = r.read(256 * 1024)
                    if not ch:
                        break
                    buf += ch
                    UPD_DL["got"] = len(buf)
                    if len(buf) > limit:
                        raise E.PipeError("El zip de la actualización es demasiado grande")
            if not UPD_DL["size"] or len(buf) >= UPD_DL["size"]:
                return bytes(buf)
        except E.PipeError:
            raise
        except Exception as e:
            if attempt == 5:
                raise E.PipeError("La descarga de la actualización se cortó (%s). Prueba otra vez: sigue donde iba"
                                  % E.net_reason(e))
            time.sleep(2 + attempt * 2)
    return bytes(buf)


UPD_PREP = {"ver": "", "data": None, "busy": False, "error": ""}


def update_prepare():
    """Baja en segundo plano la versión nueva (solo la baja: se instala cuando pulses «Reiniciar ahora»)."""
    if UPD_PREP["busy"]:
        return

    def run():
        try:
            ver, url, _ = github_latest(update_repo())
            if not url or _ver_tuple(ver) <= _ver_tuple(VERSION) or (UPD_PREP["ver"] == ver and UPD_PREP["data"]):
                return
            data = _download_update(url)
            import io
            import zipfile
            zipfile.ZipFile(io.BytesIO(data)).testzip()
            UPD_PREP.update(ver=ver, data=data, error="")
        except Exception as e:
            UPD_PREP["error"] = str(e)
        finally:
            UPD_PREP["busy"] = False
    UPD_PREP["busy"] = True
    UPD_PREP["error"] = ""
    threading.Thread(target=run, daemon=True).start()


def _github_latest_web(repo):
    """Lo mismo que github_latest sin la API de GitHub (que limita a 60 consultas por hora): la página «latest»
    redirige a la etiqueta de la última versión, y el zip se llama como lo publica la acción de GitHub."""
    req = urllib.request.Request("https://github.com/%s/releases/latest" % repo,
                                 headers={"User-Agent": "PS5Directo/" + VERSION})
    with urllib.request.urlopen(req, timeout=20) as r:
        final = r.geturl()
    m = re.search(r"/releases/tag/([^/?#]+)", final)
    if not m:
        raise E.PipeError("GitHub ha limitado las consultas desde tu conexión: prueba en un rato o instala el .zip a mano")
    tag = urllib.parse.unquote(m.group(1))
    ver = tag.lstrip("vV")
    url = "https://github.com/%s/releases/download/%s/PS5Directo-%s-Windows.zip" % (repo, urllib.parse.quote(tag), ver)
    return ver, url, ""


# ---------------------------------------------------------------------------
# Enlaces capturados por la extension del navegador
# ---------------------------------------------------------------------------

INBOX = []          # [{"url":..., "name":..., "t":...}]


class Handler(BaseHTTPRequestHandler):
    server_version = "PS5Directo"
    lan = False          # True en el servidor que se abre a la red local (movil)

    def log_message(self, *a):
        pass

    def parse_request(self):
        self._t0 = time.time()
        return super().parse_request()

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        raw = len(body)
        gz = (len(body) > 4096 and ctype.split(";")[0] in ("text/html", "application/json", "image/svg+xml")
              and "gzip" in (self.headers.get("Accept-Encoding") or ""))
        if gz:
            body = gzip_cached(body)
        err = ""
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except OSError as e:
            err = str(e) or e.__class__.__name__
            raise
        finally:
            if self.lan:
                lan_hit(self, code, raw, len(body), err)

    def _auth(self):
        if not token_ok(self.headers.get("X-Token")):
            self._send(403, {"ok": False, "error": "token"})
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        # el zip de una actualizacion va en base64 dentro del JSON: puede pasar de los 5 MB
        limit = (200 if self.path.split("?")[0] == "/api/update/zip" and not self.lan else 5) * 1024 * 1024
        if n > limit:
            left = n                       # leerlo entero igualmente: si no, el navegador da "Failed to fetch"
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                left -= len(chunk)
            return {"_too_big": True}
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        if self.path == "/logo.svg":
            with open(os.path.join(HERE, "web", "logo.svg"), "rb") as f:
                return self._send(200, f.read(), "image/svg+xml")
        if self.path.startswith("/icon-") and self.path.split("?")[0].endswith(".png"):
            n = re.sub(r"\D", "", self.path.split("?")[0])
            fp = os.path.join(HERE, "web", "icon-%s.png" % n)
            if n and os.path.isfile(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "image/png")
        if self.path.split("?")[0] == "/manifest.webmanifest":
            # con el codigo (?t=...), el icono de la pantalla de inicio se abre ya conectado: en iPhone,
            # la app de la pantalla de inicio no comparte lo guardado con Safari y si no pediria el QR otra vez
            mt = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("t") or [""])[0]
            start = "/#t=" + urllib.parse.quote(mt, safe="") if mt and token_ok(mt) else "/"
            man = {"name": "PS5 Directo", "short_name": "PS5 Directo", "display": "standalone",
                   "background_color": "#0f1115", "theme_color": "#0f1115", "start_url": start, "scope": "/",
                   "icons": [{"src": "/icon-%d.png?v=%s" % (n, VERSION), "sizes": "%dx%d" % (n, n),
                              "type": "image/png"} for n in (192, 256, 512)]}
            return self._send(200, json.dumps(man).encode(), "application/manifest+json")
        if self.path.split("?")[0] == "/favicon.ico":
            with open(os.path.join(HERE, "web", "ps5directo.ico"), "rb") as f:
                return self._send(200, f.read(), "image/x-icon")
        if self.path in ("/", "/index.html") or self.path.startswith("/?"):
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path.split("?")[0] == "/prueba":
            # pagina minima, sin token ni JavaScript: para saber si el movil llega al PC
            return self._send(200, PRUEBA_HTML, "text/html; charset=utf-8")
        if self.path.split("?")[0] == "/api/ext/ping":
            v = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("v") or [""])[0][:20]
            if v and self._from_extension():
                EXT_SEEN.update(version=v, t=time.time())
            return self._send(200, {"ok": True, "app": "PS5 Directo", "version": VERSION,
                                    "ext_version": _ext_version(os.path.join(HERE, "extension"))})
        if self.path.startswith("/api/media/file?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not token_ok((q.get("t") or [""])[0]):
                return self._send(403, {"ok": False})
            try:
                fp, ctype = media_cached((q.get("p") or [""])[0], preview=bool((q.get("v") or [""])[0]))
            except Exception as e:
                return self._send(404, {"ok": False, "error": str(e)})
            size = os.path.getsize(fp)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, max-age=86400")
            if (q.get("dl") or [""])[0]:
                orig = posixpath.basename((q.get("p") or [""])[0]) or os.path.basename(fp)
                if ctype == "image/jpeg" and not orig.lower().endswith((".jpg", ".jpeg")):
                    orig = os.path.splitext(orig)[0] + ".jpg"
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % urllib.parse.quote(orig))
            self.end_headers()
            with open(fp, "rb") as fh:
                shutil_copy = __import__("shutil").copyfileobj
                shutil_copy(fh, self.wfile, 1024 * 1024)
            return
        if self.path.startswith("/api/media/mp4?"):
            # el MP4 ya convertido de un clip (para guardarlo en el movil)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not token_ok((q.get("t") or [""])[0]):
                return self._send(403, {"ok": False})
            fp = media_mp4_path((q.get("p") or [""])[0])
            if not fp or not os.path.isfile(fp):
                return self._send(404, {"ok": False})
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(os.path.getsize(fp)))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % urllib.parse.quote(os.path.basename(fp)))
            self.end_headers()
            with open(fp, "rb") as fh:
                __import__("shutil").copyfileobj(fh, self.wfile, 1024 * 1024)
            return
        if self.path.startswith("/api/report/file?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not token_ok((q.get("t") or [""])[0]):
                return self._send(403, {"ok": False})
            name = os.path.basename((q.get("n") or [""])[0])
            fp = os.path.join(report_dir(), name)
            if not name.endswith(".zip") or not os.path.isfile(fp):
                return self._send(404, {"ok": False})
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(os.path.getsize(fp)))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.end_headers()
            with open(fp, "rb") as fh:
                self.wfile.write(fh.read())
            return
        if self.path.startswith("/api/colicon?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not token_ok((q.get("t") or [""])[0]):
                return self._send(403, {"ok": False})
            data = col_icon((q.get("p") or [""])[0])
            if not data:
                return self._send(404, {"ok": False})
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/api/art?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not token_ok((q.get("t") or [""])[0]):
                return self._send(403, {"ok": False})
            got = art_get((q.get("u") or [""])[0])
            if not got:
                return self._send(404, {"ok": False})
            data, ctype = got
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=604800")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/api/cover?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not token_ok((q.get("t") or [""])[0]):
                return self._send(403, {"ok": False})
            try:
                data = cover_png((q.get("p") or [None])[0], (q.get("tid") or [None])[0], (q.get("k") or [""])[0] == "pic0")
            except Exception:
                data = None
            if not data:
                return self._send(404, {"ok": False})
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/api/state":
            if not self._auth():
                return
            if self.headers.get("X-UI") and not self.lan:
                UI_SEEN["t"] = time.time()
                UI_SEEN["bye"] = 0.0
            with LOCK:
                jobs = []
                for j in JOBS:
                    d = {k: v for k, v in j.items() if k not in ("lines", "info", "password")}
                    d["has_password"] = bool(j.get("password"))
                    d["pcx"] = (j.get("info") or {}).get("mode") == "pcx"
                    jobs.append(d)
                st = {"ok": True, "version": VERSION, "home": HERE,
                      "unread": sum(1 for n in NOTIFS if not n.get("read")), "settings": dict(SETTINGS), "jobs": jobs,
                      "inbox": len(INBOX), "repo": SETTINGS.get("update_repo") or DEFAULT_REPO,
                      "sched": {"start": SCHED["start"], "after": SCHED["after"], "countdown": SCHED["countdown"]},
                      "lan": bool(LAN["srv"]), "remote": self.lan, "phone": PHONE, "os": os.name,
                      "platform": sys.platform,
                      "bugs": bug_state() if not self.lan else None,
                      "now": dict(NOWPLAY) if NOWPLAY.get("tid") and time.time() - NOWPLAY.get("t", 0) < 180 else None}
            st["settings"].pop("ftp_pass", None)
            for k in ("shots_tg_token", "shots_dc_hook"):      # no se ensenan: solo si estan puestos
                st["settings"][k] = "••••" if st["settings"].get(k) else ""
            if self.lan:
                st["settings"].pop("lan_token", None)
            return self._send(200, st)
        self._send(404, {"ok": False})

    def _from_extension(self):
        o = self.headers.get("Origin") or ""
        return o.startswith("chrome-extension://") or o.startswith("moz-extension://") \
            or o.startswith("extension://")

    def _ext_api(self):
        """Lo que usa la extension del navegador: estado de la cola, mandar enlaces y pausar/cancelar.
        Solo desde una extension en este PC (nunca desde la red)."""
        b = self._body()
        if b.get("ext_version"):
            have, want = str(b["ext_version"])[:20], _ext_version(os.path.join(HERE, "extension"))
            EXT_SEEN.update(version=have, t=time.time())
            if want and _vt(have) < _vt(want):
                notify("warn", "La extensión del navegador está desactualizada (%s, hay %s)" % (have, want),
                       "Quítala en la página de extensiones del navegador y vuelve a cargarla desde Ajustes → «Abrir "
                       "la carpeta de la extensión» (Cargar descomprimida). Desde entonces se actualiza sola.",
                       {"tab": "Transfers"}, key="extold:" + want)
        if self.path == "/api/ext/status":
            with LOCK:
                js = [{"id": j["id"], "title": _job_name(j), "status": j["status"], "pct": job_pct(j),
                       "speed": float(j.get("speed") or 0), "phase": j.get("phase") or "", "where": j.get("where") or "",
                       "left": max(0, int(j.get("total") or 0) - int(j.get("sent") or 0)) if (j.get("total") or 0) > 0 else None}
                      for j in JOBS[-12:]]
                inbox = len(INBOX)
            return self._send(200, {"ok": True, "version": VERSION, "jobs": js, "ftp": CONN_STATE["ftp"],
                                    "bf": CONN_STATE["bf"], "inbox": inbox, "pc_mode": bool(SETTINGS.get("pc_mode"))})
        if self.path == "/api/ext/send":
            links = [x for x in add_lines("\n".join(str(v) for v in (b.get("links") or [])[:500]))
                     if re.match(r"^((https?|ftp)://|magnet:\?)", x, re.I)]
            if not links:
                return self._send(400, {"ok": False, "error": "No hay enlaces"})
            cookies = b.get("cookies") if isinstance(b.get("cookies"), dict) else {}
            for u in links:
                if re.match(r"^https?://", u, re.I):
                    host = urllib.parse.urlparse(u).netloc.lower()
                    E.set_browser_headers(u, str(cookies.get(host) or ""), str(b.get("ua") or ""),
                                          str(b.get("referrer") or ""))
            pw_found = password_in_text(str(b.get("text") or ""))[:120]
            if b.get("mode") == "queue":
                with LOCK:
                    pcm = bool(SETTINGS.get("pc_mode"))
                    steps = SETTINGS.get("pc_mode_steps") or "extract"
                    pfmt = SETTINGS.get("pc_mode_fmt") or ""
                # en Modo PC va a tu PC salvo que en la extension hayas elegido otra cosa a proposito
                where = "keep" if b.get("where") == "keep" or (pcm and not b.get("where_set")) else ""
                job = new_job(links, pw_found, SETTINGS.get("dest") or "/data/homebrew", where=where,
                              keep_steps=(steps if pcm else "extract") if where else "",
                              fmt=(pfmt or None) if where and pcm and steps == "convert" else None)
                with WAKE:
                    JOBS.append(job)
                    WAKE.notify_all()
                save_jobs()
                return self._send(200, {"ok": True, "queued": len(links), "id": job["id"], "password": bool(pw_found)})
            with LOCK:
                for u in links:
                    if not any(x["url"] == u for x in INBOX):
                        it = {"url": u, "name": urllib.parse.unquote(u.split("?")[0].rsplit("/", 1)[-1])[:300],
                              "t": time.time()}
                        if pw_found:
                            it["password"] = pw_found
                        INBOX.append(it)
                del INBOX[:-200]
            return self._send(200, {"ok": True, "boxed": len(links), "password": bool(pw_found)})
        if self.path == "/api/ext/job":
            action, jid = str(b.get("action") or ""), str(b.get("id") or "")
            with WAKE:
                job = find(jid)
                if not job:
                    return self._send(404, {"ok": False, "error": "no existe"})
                if action == "pause":
                    if job["status"] == "pendiente":
                        job["status"] = "pausado"
                        job["phase"] = "En pausa (pulsa Reanudar para seguir)"
                    elif job["status"] in ("comprobando", "enviando", "convirtiendo"):
                        job["_pause"] = True
                        CANCELS.setdefault(jid, threading.Event()).set()
                elif action == "cancel":
                    CANCELS.setdefault(jid, threading.Event()).set()
                    if job["status"] in ("pendiente", "en cola para descomprimir"):
                        job["status"] = "cancelado"
                        job["phase"] = "Cancelado"
                elif action == "resume" and job["status"] == "pausado" and not job.get("ask"):
                    CANCELS.setdefault(jid, threading.Event()).clear()
                    job["status"] = "pendiente"
                    job["phase"] = "En cola"
                    WAKE.notify_all()
                elif action == "now" and job["status"] in ("pendiente", "pausado"):
                    send_now(job)
                elif action == "reveal":
                    res = list(job.get("result") or [])
                    if job.get("where") == "keep" and res and os.path.exists(res[0]):
                        F.reveal_path(res[0])
            save_jobs()
            return self._send(200, {"ok": True})
        if self.path == "/api/ext/pcmode":
            with LOCK:
                SETTINGS["pc_mode"] = bool(b.get("on"))
            save_settings()
            return self._send(200, {"ok": True, "pc_mode": bool(b.get("on"))})
        return self._send(404, {"ok": False})

    def do_POST(self):
        if self.path in ("/api/ext/status", "/api/ext/send", "/api/ext/job", "/api/ext/pcmode"):
            if self.lan or not self._from_extension():
                return self._send(403, {"ok": False, "error": "solo desde la extension"})
            return self._ext_api()
        if self.path.split("?")[0] == "/api/bye":
            # la ventana se esta cerrando (sendBeacon no deja poner cabeceras: token en la URL)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if (q.get("t") or [""])[0] == TOKEN and not self.lan:
                UI_SEEN["bye"] = time.time()
            return self._send(200, {"ok": True})
        if self.path == "/api/ext/capture":
            if self.lan or not self._from_extension():
                return self._send(403, {"ok": False, "error": "solo desde la extension"})
            b = self._body()
            url = str(b.get("url") or "").strip()
            if not re.match(r"^https?://", url, re.I) or len(url) > 4000:
                return self._send(400, {"ok": False, "error": "enlace no valido"})
            # cookies y navegador que usa ese alojamiento: el servidor nos dara la misma
            # velocidad y el mismo permiso que al navegador (sesion, cuenta premium)
            cookies = b.get("cookies") if isinstance(b.get("cookies"), dict) else {}
            for u in (url, str(b.get("original") or "")):
                if re.match(r"^https?://", u, re.I):
                    host = urllib.parse.urlparse(u).netloc.lower()
                    E.set_browser_headers(u, str(cookies.get(host) or ""), str(b.get("ua") or ""),
                                          str(b.get("referrer") or ""))
            with LOCK:
                if not any(x["url"] == url for x in INBOX):
                    INBOX.append({"url": url, "name": str(b.get("name") or "")[:300], "t": time.time()})
                    del INBOX[:-200]
            return self._send(200, {"ok": True})
        if not self._auth():
            return
        b = self._body()
        p = self.path
        if self.lan:
            if p in LAN_DENY:
                return self._send(403, {"ok": False, "error": "Eso solo se puede hacer desde el PC"})
            if p == "/api/settings":
                b = {k: v for k, v in b.items() if k not in LAN_LOCKED}
            if p == "/api/add":
                ls = add_lines(b.get("lines", ""))
                if any(not re.match(r"^((https?|ftp)://|magnet:\?)", x, re.I) for x in ls):
                    return self._send(400, {"ok": False, "error": "Desde el móvil solo se pueden añadir enlaces "
                                                                  "(http, https o ftp), no archivos del PC"})
        extra = extra_api(self, p, b)
        if extra is not None:
            return self._send(200, extra)
        if p == "/api/ext/setup":
            sync_extension()
            return self._send(200, {"ok": True, "browsers": [{"id": x["id"], "name": x["name"]} for x in ext_browsers()],
                                    "folder": EXT_DIR, "want": _ext_version(os.path.join(HERE, "extension")),
                                    "seen": EXT_SEEN.get("version"), "seen_ago": (time.time() - EXT_SEEN["t"]) if EXT_SEEN.get("t") else None})
        if p == "/api/ui/focus":
            return self._send(200, {"ok": ui_focus_here()})
        if p == "/api/ext/openpage":
            try:
                return self._send(200, {"ok": True, "name": ext_open_page(str(b.get("id") or ""))})
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
        if p == "/api/open_ext_folder":
            sync_extension()
            folder = EXT_DIR if os.path.isdir(EXT_DIR) else os.path.join(HERE, "extension")
            try:
                F.open_path(folder)
                return self._send(200, {"ok": True, "path": folder})
            except Exception as e:
                return self._send(200, {"ok": False, "path": folder, "error": str(e)})
        if p == "/api/inbox/take":
            with LOCK:
                items = list(INBOX)
                INBOX[:] = []
            return self._send(200, {"ok": True, "items": items})
        if p == "/api/update/zip":
            import base64
            if b.get("_too_big"):
                return self._send(400, {"ok": False, "error": "El zip es demasiado grande (más de 150 MB)"})
            try:
                ver = install_zip(base64.b64decode(b.get("data") or ""))
            except Exception as e:
                return self._send(400, {"ok": False, "error": str(e)})
            restart_soon()
            return self._send(200, {"ok": True, "version": ver})
        if p == "/api/update/check":
            try:
                ver, url, notes = github_latest(update_repo())
            except E.PipeError as e:
                return self._send(200, {"ok": False, "error": str(e)})
            except Exception as e:
                return self._send(200, {"ok": False, "error": "No he podido consultar GitHub (%s)" % E.net_reason(e)})
            if _ver_tuple(ver) > _ver_tuple(VERSION):
                notify("info", "Hay una versión nueva: %s" % ver,
                       "Baja la app nueva desde el aviso de arriba e instálala encima." if PHONE else "Pulsa Actualizar arriba para instalarla.",
                       {"action": "update"}, key="update:" + ver)
            newer = _ver_tuple(ver) > _ver_tuple(VERSION)
            try:      # todas las versiones que te faltan, no solo la última
                nl = gh_notes(update_repo(), VERSION if newer else "")["items"]
            except Exception:
                nl = []
            return self._send(200, {"ok": True, "current": VERSION, "latest": ver, "notes": notes, "notes_list": nl,
                                    "newer": newer})
        if p == "/api/update/github":
            try:
                ver, url, _ = github_latest(update_repo())
                if not url:
                    raise E.PipeError("Esa release no tiene un .zip")
                data = UPD_PREP["data"] if UPD_PREP.get("ver") == ver and UPD_PREP.get("data") else None
                ver = install_zip(data or _download_update(url))
            except Exception as e:
                return self._send(400, {"ok": False, "error": str(e)})
            restart_soon()
            return self._send(200, {"ok": True, "version": ver})
        if p == "/api/update/prepare":
            update_prepare()
            return self._send(200, {"ok": True, "busy": UPD_PREP["busy"], "ready": UPD_PREP["ver"] if UPD_PREP["data"] else "",
                                    "error": UPD_PREP["error"], **UPD_DL})
        if p == "/api/update/progress":
            return self._send(200, {"ok": True, **UPD_DL})
        if p == "/api/whatsnew":
            return self._send(200, dict(ok=True, **whats_new("en" if b.get("lang") == "en" else "es", bool(b.get("all")))))
        if p == "/api/whatsnew/seen":
            with LOCK:
                SETTINGS["seen_version"] = VERSION
            save_settings()
            return self._send(200, {"ok": True})
        if p == "/api/settings":
            with LOCK:
                for k, v in b.items():
                    if k in ("shots_tg_token", "shots_dc_hook") and v == "••••":
                        continue
                    if k in DEFAULTS:
                        if isinstance(DEFAULTS[k], bool):
                            v = bool(v)
                        elif isinstance(DEFAULTS[k], int):
                            try:
                                v = int(v)
                            except (TypeError, ValueError):
                                continue
                        elif isinstance(DEFAULTS[k], list):
                            v = [x.strip() for x in (v or []) if str(x).strip()]
                        if k == "ps5_ip":
                            # "ftp://192.168.1.50:2121", con espacios o comas: dejar solo la IP
                            v, port = E.clean_host(v)
                            if port and port != int(b.get("bf_port") or SETTINGS.get("bf_port", 5905)):
                                SETTINGS["ftp_port"] = port
                                if "ftp_port" in b:
                                    b["ftp_port"] = port
                        SETTINGS[k] = v
            save_settings()
            if "low_prio" in b:
                F.priority_all(bool(SETTINGS.get("low_prio")))
            return self._send(200, {"ok": True})
        if p == "/api/locate":
            files = b.get("files") or []
            return self._send(200, {"ok": True, "found": locate_files(files)})
        if p == "/api/health":
            return self._send(200, {"ok": True, "checks": health_check()})
        if p == "/api/places":
            bf = E.BFpilot(cfg_snapshot())
            try:
                bases = game_bases(bf) if bf.alive() else ["/data/homebrew"]
            except Exception:
                bases = ["/data/homebrew"]
            return self._send(200, {"ok": True, "dests": [b for b in bases if b.endswith("homebrew")]})
        if p.startswith("/api/fm/") or p.startswith("/api/lib/") or p.startswith("/api/space/") or p.startswith("/api/notifs") or p.startswith("/api/gu/") or p.startswith("/api/bk/") or p.startswith("/api/col/") or p.startswith("/api/media/") or p.startswith("/api/residue/") or p.startswith("/api/grab/") or p in ("/api/discover", "/api/played", "/api/job/reveal") or p.startswith("/api/trash/") or p.startswith("/api/archived/") or p.startswith("/api/report") or p in ("/api/pick", "/api/history", "/api/history/clear", "/api/history/redo"):
            try:
                return self._send(200, dict(ok=True, **fm_api(p, b)))
            except E.PipeError as e:
                return self._send(200, {"ok": False, "error": str(e)})
            except Exception as e:
                traceback.print_exc()
                return self._send(200, {"ok": False, "error": conn_error_text(e)})
        if p == "/api/fixperms":
            try:
                return self._send(200, {"ok": True, "results": fix_all_perms()})
            except Exception as e:
                return self._send(500, {"ok": False, "error": conn_error_text(e)})
        if p == "/api/cleanup/scan":
            try:
                return self._send(200, {"ok": True, "items": cleanup_scan()})
            except Exception as e:
                return self._send(500, {"ok": False, "error": conn_error_text(e)})
        if p == "/api/cleanup/delete":
            try:
                return self._send(200, {"ok": True, "deleted": cleanup_delete(b.get("paths") or [])})
            except Exception as e:
                return self._send(500, {"ok": False, "error": conn_error_text(e)})
        if p == "/api/speedtest":
            if SPEEDTEST["running"]:
                return self._send(409, {"ok": False, "error": "Ya hay un test en marcha"})
            with LOCK:
                active = [j for j in JOBS if j["status"] in ("comprobando", "enviando")]
            if active:
                return self._send(409, {"ok": False, "error": "Pausa el envio antes del test (si no, se mezclan las medidas)"})
            url = (b.get("url") or "").strip()
            if url:
                url = E.resolve_url(url)
            SPEEDTEST.update(running=True, result=None)
            threading.Thread(target=_speedtest_thread, args=(url,), daemon=True).start()
            return self._send(200, {"ok": True})
        if p == "/api/speedtest/result":
            return self._send(200, {"ok": True, "running": SPEEDTEST["running"], "result": SPEEDTEST["result"]})
        if p == "/api/test":
            cfg = cfg_snapshot()
            r = dict(test_connection(cfg))
            if r.get("ftp") != "ok":
                warn = net_mismatch(cfg)
                if warn:
                    r["net_warn"] = warn
                    notify("warn", "El PC y la PS5 están en redes distintas", warn, key="net:" + warn[:60])
            return self._send(200, {"ok": True, **r})
        if p == "/api/add":
            lines = add_lines(b.get("lines", ""))
            if not lines:
                return self._send(400, {"ok": False, "error": "Pega al menos un enlace"})
            dest = (b.get("dest") or SETTINGS["dest"]).rstrip("/") or "/data/homebrew"
            if not dest.startswith("/data") and not dest.startswith("/mnt/"):
                return self._send(400, {"ok": False, "error": "El destino debe estar en /data o /mnt"})
            pcdir = "" if self.lan else str(b.get("pcdir") or "").strip().strip('"')
            if pcdir and not os.path.isabs(pcdir):
                return self._send(400, {"ok": False, "error": "La carpeta del PC tiene que ser una ruta completa"})
            job = new_job(lines, (b.get("password") or "").strip(), dest, where=b.get("where"),
                          raw=bool(b.get("raw")), fmt=b.get("fmt"), comp=b.get("comp"), pcdir=pcdir,
                          keep_steps=str(b.get("keep_steps") or ""))
            if b.get("title"):
                job["title"] = str(b["title"])[:200]
            if "fmt" in b:
                job["fmt_set"] = True
            if PHONE:
                # en el movil no hay 7-Zip ni Dump Forge: descomprime BFpilot y convierte Alembic, en la consola
                if job.get("where") == "keep":
                    return self._send(400, {"ok": False, "error": "En el móvil no se puede guardar en el propio móvil: elige «A la PS5»."})
                if job.get("where") == "pc":
                    job["where"] = "ps5"
                if job["fmt"] and job["fmt"] != "plain" and job["fmt"] not in ALEMBIC_TO:
                    return self._send(400, {"ok": False, "error": "Sin PC solo se convierte en la PS5 (Alembic): elige LZ4, zstd, .exfat, .ffpkg o .ffpfsc."})
                if job["fmt"] in ALEMBIC_TO:
                    b["conv_at"] = "ps5"
            if b.get("conv_at") == "ps5" and job["fmt"] in ALEMBIC_TO and job["where"] in ("ps5", "pc", ""):
                # se manda tal cual (descarga y envío a la vez) y luego lo convierte Alembic en la propia consola
                job["alembic"], job["fmt"] = job["fmt"], ""
            only = [str(x) for x in (b.get("only") or []) if str(x).strip()][:20000]
            if only and job.get("where") == "keep":
                job["only"] = only          # sacar del comprimido solo esto
            with WAKE:
                JOBS.append(job)
                WAKE.notify_all()
            save_jobs()
            return self._send(200, {"ok": True, "id": job["id"]})
        if p.startswith("/api/job/"):
            parts = p.split("/")
            if len(parts) != 5:
                return self._send(404, {"ok": False})
            job_id, action = parts[3], parts[4]
            with WAKE:
                job = find(job_id)
                if not job:
                    return self._send(404, {"ok": False, "error": "no existe"})
                if action == "pause":
                    if job["status"] == "pendiente":
                        job["status"] = "pausado"
                        job["phase"] = "En pausa (pulsa Reanudar para seguir)"
                    elif job["status"] in ("comprobando", "enviando", "convirtiendo"):
                        job["_pause"] = True
                        CANCELS.setdefault(job_id, threading.Event()).set()
                elif action == "cancel":
                    if job.get("held"):
                        F.suspend_pids(F.CHILD_PROCS.get(job_id) or set(), False)
                        job["held"] = False
                    CANCELS.setdefault(job_id, threading.Event()).set()
                    if job["status"] in ("pendiente", "en cola para descomprimir"):
                        job["status"] = "cancelado"
                        job["phase"] = "Cancelado"
                elif action == "decide":
                    choice = b.get("choice")
                    at_place = (job.get("ask") or {}).get("at") == "place"
                    job.pop("ask", None)
                    if choice == "cancel":
                        job["status"] = "cancelado"
                        job["phase"] = "Cancelado"
                    elif choice in ("send", "replace") and job["status"] in ("pausado", "error"):
                        job["dup_choice"] = choice
                        job["error"] = ""
                        CANCELS.setdefault(job_id, threading.Event()).clear()
                        if at_place and job.get("info"):
                            job["status"] = "en cola para descomprimir"
                            job["phase"] = "Colocando"
                            EXTRACT_Q.append(job_id)
                        else:
                            job["status"] = "pendiente"
                            job["phase"] = "En cola"
                        WAKE.notify_all()
                elif action in ("hold", "unhold"):
                    pids = F.CHILD_PROCS.get(job_id) or set()
                    n = F.suspend_pids(pids, action == "hold")
                    if action == "hold" and n:
                        job["held"] = True
                        job["_phase_before_hold"] = job.get("phase") or ""
                        job["phase"] = "En pausa: no gasta procesador ni disco hasta que pulses Seguir"
                    elif action == "unhold":
                        job["held"] = False
                        job["phase"] = job.pop("_phase_before_hold", "") or job.get("phase")
                    elif action == "hold":
                        return self._send(200, {"ok": False, "error": "Ahora mismo no hay nada que pausar en este trabajo"})
                elif action == "retry":
                    if job["status"] in ("error", "cancelado", "pausado"):
                        if b.get("password"):
                            job["password"] = b["password"].strip()
                        CANCELS.setdefault(job_id, threading.Event()).clear()
                        job["error"] = ""
                        job.pop("need_bytes", None)
                        job.pop("ask", None)
                        if job.get("info") and job.get("phase") != "Cancelado" and \
                                job.get("sent", 0) >= max(0, job.get("total", 0)) and job.get("total", -1) > 0:
                            job["status"] = "en cola para descomprimir"
                            job["phase"] = "Reintentando la descompresion"
                            EXTRACT_Q.append(job_id)
                        else:
                            job["status"] = "pendiente"
                            job["phase"] = "En cola"
                        WAKE.notify_all()
                elif action == "remove":
                    if job["status"] in ("hecho", "error", "cancelado", "pausado"):
                        JOBS.remove(job)
                        torrent_forget(job)
                elif action == "restart":
                    # "Descargar de nuevo": como si se volviera a pegar el enlace (trabajo nuevo, desde cero)
                    if job["status"] in ("hecho", "error", "cancelado"):
                        torrent_forget(job)
                        nj = new_job(list(job.get("orig_lines") or job.get("lines") or []), job.get("password") or "", job["dest"],
                                     where=job.get("where"), raw=job.get("raw"), fmt=job.get("fmt"),
                                     comp=job.get("comp"), pcdir=job.get("pcdir") or "",
                                     keep_steps=job.get("keep_steps") or "")
                        nj["fmt_set"] = job.get("fmt_set", False)
                        nj["title"] = job.get("title") or ""
                        JOBS[JOBS.index(job)] = nj
                        WAKE.notify_all()
                elif action in ("up", "down"):
                    # entre los que esperan (los terminados o en marcha no cuentan)
                    waiting = [x for x in JOBS if x["status"] in ("pendiente", "pausado")]
                    if job in waiting:
                        i = waiting.index(job)
                        k = i - 1 if action == "up" else i + 1
                        if 0 <= k < len(waiting):
                            a, c = JOBS.index(job), JOBS.index(waiting[k])
                            JOBS[a], JOBS[c] = JOBS[c], JOBS[a]
                elif action == "now":
                    if job["status"] in ("pendiente", "pausado"):
                        send_now(job)
            save_jobs()
            return self._send(200, {"ok": True})
        if p == "/api/clear":
            with LOCK:
                JOBS[:] = [j for j in JOBS if j["status"] not in ("hecho", "cancelado")]
            save_jobs()
            return self._send(200, {"ok": True})
        if p == "/api/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.5), cloud_stop_all(), os._exit(0)), daemon=True).start()
            return
        self._send(404, {"ok": False})



# ---------------------------------------------------------------------------
# Fallos del programa detectados solos: para proponer el informe ya rellenado (Mac y Linux, en pruebas, y Windows)
# Solo cuentan los fallos de programacion (no los de red, enlaces o consola apagada, que no son culpa del programa).
# ---------------------------------------------------------------------------

AUTOBUG = {"items": [], "n": 0}
AUTOBUG_LOCK = threading.Lock()
_BUG_TYPES = (TypeError, KeyError, AttributeError, IndexError, NameError, ZeroDivisionError, AssertionError,
              RecursionError, UnicodeError, NotImplementedError, ValueError)


def _scrub(t):
    t = str(t or "")
    home = os.path.expanduser("~")
    if home and len(home) > 3:
        t = t.replace(home, "~")
    t = re.sub(r"([#?&]t=|X-Token[\"': ]+)[\w-]{8,}", r"\1(oculto)", t)
    t = re.sub(r"(bot)\d+:[\w-]{20,}", r"\1(oculto)", t)
    t = re.sub(r"https://discord(?:app)?\.com/api/webhooks/\S+", "(webhook oculto)", t)
    return t


def _is_program_bug(exc, tb_text):
    if exc is None or isinstance(exc, (E.PipeError, E.Cancelled, KeyboardInterrupt, SystemExit)):
        return False
    if isinstance(exc, json.JSONDecodeError):
        return False
    if isinstance(exc, _BUG_TYPES):
        return True
    # Mac y Linux: un programa del sistema que no esta (osascript, xdg-open...) tambien es un fallo nuestro
    if os.name != "nt" and isinstance(exc, (FileNotFoundError, PermissionError)) and "subprocess" in tb_text:
        return True
    return False


def bug_record(where, exc=None, tb_text="", js=False):
    try:
        if not js and not _is_program_bug(exc, tb_text):
            return
        msg = _scrub(("%s: %s" % (type(exc).__name__, exc)) if exc is not None else where)[:400]
        key = re.sub(r"\d+", "#", msg)[:200] + "|" + where
        with AUTOBUG_LOCK:
            for it in AUTOBUG["items"]:
                if it["key"] == key:
                    it["count"] += 1
                    it["t"] = time.time()
                    return
            AUTOBUG["n"] += 1
            AUTOBUG["items"].insert(0, {"id": AUTOBUG["n"], "key": key, "t": time.time(), "where": where[:120],
                                        "msg": msg, "tb": _scrub(tb_text)[-6000:], "count": 1, "js": js,
                                        "seen": False})
            del AUTOBUG["items"][20:]
    except Exception:
        pass


_orig_print_exc = traceback.print_exc


def _print_exc_hook(*a, **k):
    et, ev, tb = sys.exc_info()
    try:
        txt = "".join(traceback.format_exception(et, ev, tb))
        where = traceback.extract_tb(tb)[-1].name if tb else "?"
        bug_record(where, ev, txt)
    except Exception:
        pass
    return _orig_print_exc(*a, **k)


traceback.print_exc = _print_exc_hook


def _thread_hook(args):
    try:
        txt = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        bug_record("hilo %s" % (args.thread.name if args.thread else "?"), args.exc_value, txt)
    except Exception:
        pass
    _orig_thread_hook(args)


_orig_thread_hook = threading.excepthook
threading.excepthook = _thread_hook


def bug_state():
    with AUTOBUG_LOCK:
        new = [x for x in AUTOBUG["items"] if not x["seen"]]
        return {"new": len(new), "last": ({"id": new[0]["id"], "msg": new[0]["msg"], "where": new[0]["where"]}
                                          if new else None)}


def bug_os_label():
    return "macOS" if sys.platform == "darwin" else "Linux" if sys.platform.startswith("linux") else "Windows"



# ---------------------------------------------------------------------------
# Actualizaciones de los payloads: lo que tienes en Payload Manager frente a la ultima version de su catalogo (su
# repositorio oficial y tus fuentes) y, para lo subido a mano con la version en el nombre, frente al GitHub de su
# autor. Avisa una vez por version nueva; «Actualizar» hace lo mismo que el boton de Payload Manager.
# ---------------------------------------------------------------------------

PLD = {"t": 0, "items": [], "error": "", "busy": False, "pm": None}
PLD_T = [0.0]
PLD_SEEN_FILE = os.path.join(DATA_DIR, "payloads_avisados.json")
# subidos a mano (sin datos del catalogo): nombre del archivo -> GitHub de su autor
PLD_GITHUB = [(re.compile(r"(?i)shadow ?mount"), "drakmor/ShadowMountPlus", "ShadowMountPlus"),
              (re.compile(r"(?i)^ftpsrv"), "ps5-payload-dev/ftpsrv", "ftpsrv"),
              (re.compile(r"(?i)^elfldr"), "ps5-payload-dev/elfldr", "elfldr"),
              (re.compile(r"(?i)bfpilot"), "ItsBlurf/BFpilot", "BFpilot"),
              (re.compile(r"(?i)zhttp|zftpd"), "seregonwar/zftpd", "zftpd")]
_FILE_VER = re.compile(r"(?i)[_\- ]v?(\d+(?:\.\d+)+(?:[-_.]?(?:alpha|beta|b|rc)\.?\d*)?)")


GH_TAGS = {}


def gh_newest_tag(repo, pre=False):
    """La versión más nueva de un proyecto en GitHub; con pre=True cuenta también las de prueba (beta, rc).
    Se guarda una hora para no gastar consultas."""
    k = (repo.lower(), pre)
    hit = GH_TAGS.get(k)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    req = urllib.request.Request(F.GH_API + "/repos/%s/releases?per_page=10" % repo, headers=F._ua())
    with urllib.request.urlopen(req, timeout=20) as r:
        lst = [x for x in json.load(r) if not x.get("draft") and (pre or not x.get("prerelease"))]
    tag = ""
    for x in lst:
        t = (x.get("tag_name") or "").strip()
        if t and (not tag or F.pm_compare(t, tag) > 0):
            tag = t
    GH_TAGS[k] = (time.time(), tag)
    return tag


def pld_check(refresh=False):
    cfg = cfg_snapshot()
    if not cfg.get("ps5_ip"):
        raise E.PipeError("Falta la IP de la PS5: ponla en Ajustes")
    pm = F.PayloadManager(cfg["ps5_ip"])
    ver = pm.version()
    if not ver:
        with LOCK:
            PLD.update(t=time.time(), pm=None, error="Payload Manager no está abierto en la consola.")
        return pld_state()
    full = pm.list_full()
    try:
        repo = pm.repository(refresh=refresh)
    except Exception as e:
        repo, rerr = [], str(e)
    else:
        rerr = ""
    items = []
    for x in full.get("items") or []:
        it = {"slot": x.get("slot") or "", "file": x.get("filename") or "", "kind": x.get("kind") or "",
              "name": x.get("name") or x.get("filename") or "", "have": x.get("version") or "", "new": "",
              "source": x.get("source_name") or "", "source_url": x.get("source_url") or "", "id": x.get("id") or "",
              "repo": "", "how": ""}
        if it["kind"] == "repo" and it["id"]:
            cands = [r for r in repo if (r.get("id") or "") == it["id"] and
                     (not it["source_url"] or (r.get("source_url") or "").rstrip("/") == it["source_url"].rstrip("/"))]
            best = None
            for r in cands:
                if best is None or F.pm_compare(r.get("version"), best.get("version"), r.get("last_update"),
                                                best.get("last_update")) > 0:
                    best = r
            if best and F.pm_compare(best.get("version"), it["have"], best.get("last_update"),
                                     x.get("last_update")) > 0:
                it.update(new=best.get("version") or "?", how="pm", when=best.get("last_update") or "")
            # el catálogo de Payload Manager a veces va por detrás de GitHub (ShadowMountPlus, ftpsrv…): se mira allí
            gh = next((g for g in PLD_GITHUB if g[0].search(it["name"] + " " + it["file"])), None)
            if gh:
                it["repo"] = gh[1]
                if not it["new"]:
                    try:
                        pre = bool(re.search(r"(?i)alpha|beta|rc|test", it["have"] or ""))
                        tag = gh_newest_tag(gh[1], pre)
                        if tag and F.pm_compare(tag, it["have"]) > 0:
                            it.update(new=tag, how="github")
                    except Exception:
                        pass
        else:
            m = _FILE_VER.search(it["file"])
            gh = next((g for g in PLD_GITHUB if g[0].search(it["file"])), None)
            if gh:
                it["repo"] = gh[1]
                it["name"] = gh[2]
                if m:
                    it["have"] = m.group(1)
                    try:
                        tag, _ = F.github_release(gh[1])
                        if tag and F.pm_compare(tag, it["have"]) > 0:
                            it.update(new=tag, how="github")
                    except Exception:
                        pass
        items.append(it)
    # la versión nueva ya está puesta (por ejemplo con «Instalar en la consola», que la sube con su versión en el
    # nombre): el antiguo deja de pedir actualizar y se dice cuál es el nuevo
    for it in items:
        if it["how"] != "github" or not it["new"] or not it["repo"]:
            continue
        newer = [o for o in items if o is not it and o["repo"] == it["repo"] and o["have"] and
                 F.pm_compare(o["have"], it["new"]) >= 0]
        if newer:
            it.update(new="", how="", replaced=newer[0]["file"] or newer[0]["name"], replaced_ver=newer[0]["have"])
    sv = full.get("self") or {}
    if sv.get("pending_version") and F.pm_compare(sv.get("pending_version"), sv.get("running_version")) > 0:
        items.insert(0, {"slot": "pldmgr", "file": "", "kind": "self", "name": "Payload Manager",
                         "have": sv.get("running_version") or ver, "new": sv["pending_version"], "how": "restart",
                         "source": "", "source_url": "", "id": "pldmgr", "repo": "itsPLK/ps5-payload-manager"})
    with LOCK:
        PLD.update(t=time.time(), items=items, pm=ver, error=rerr)
    return pld_state()


def pld_state():
    with LOCK:
        d = dict(PLD)
        d["updates"] = sum(1 for x in PLD["items"] if x.get("new"))
    return d


def pld_tick():
    """Cada 6 horas con Payload Manager abierto: si sale una version nueva de algo que tienes, aviso una vez."""
    try:
        st = pld_check(refresh=True)
    except Exception as e:
        print("Actualizaciones de payloads:", e)
        return
    try:
        with open(PLD_SEEN_FILE, encoding="utf-8") as fh:
            seen = set(json.load(fh))
    except (OSError, ValueError):
        seen = set()
    new = [x for x in st["items"] if x.get("new") and "%s@%s" % (x["slot"] or x["name"], x["new"]) not in seen]
    if not new:
        return
    names = ", ".join("%s %s" % (x["name"], x["new"]) for x in new)
    notify("info", "Versión nueva de %s" % (new[0]["name"] if len(new) == 1 else "%d payloads" % len(new)),
           "%s. Pulsa para actualizar." % names, {"tab": "System", "card": "pldCard"},
           key="pld-" + names[:150])
    try:
        shots_text("🧩 PS5 Directo: hay versión nueva de %s. Actualízalo desde PS5 Directo › Sistema." % names)
    except Exception:
        pass
    seen |= {"%s@%s" % (x["slot"] or x["name"], x["new"]) for x in new}
    try:
        with open(PLD_SEEN_FILE, "w", encoding="utf-8") as fh:
            json.dump(sorted(seen)[-300:], fh)
    except OSError:
        pass


def pld_update(slot):
    """Actualiza un payload que vino del catalogo de Payload Manager (como su propio boton «Actualizar»)."""
    with LOCK:
        it = next((x for x in PLD["items"] if x["slot"] == slot), None)
    if it and it.get("how") == "github" and it.get("repo"):
        return pld_github_install(it)
    if not it or it.get("how") != "pm":
        raise E.PipeError("Ese payload no se puede actualizar desde aquí: mira su página de GitHub.")
    cfg = cfg_snapshot()
    r = F.PayloadManager(cfg["ps5_ip"]).repo_install(it["source_url"], it["id"], "replace")
    if r.get("ok") is False:
        raise E.PipeError("Payload Manager no lo ha actualizado: %s" % (r.get("message") or r.get("error") or "?"))
    st = pld_check()
    return dict(st, done=it["name"], version=it["new"])


def pld_github_install(it):
    """La versión nueva de GitHub, puesta en Payload Manager sin que tengas que bajar nada: se baja su .elf, se
    sube a Payload Manager con su versión en el nombre y, si el antiguo se cargaba solo al arrancar, el nuevo
    ocupa su sitio en esa lista (el antiguo se queda, por si quieres volver)."""
    cfg = cfg_snapshot()
    pm = F.PayloadManager(cfg["ps5_ip"])
    if not pm.version():
        raise E.PipeError("Abre Payload Manager en la consola para poder ponerlo")
    tag = it["new"]
    req = urllib.request.Request(F.GH_API + "/repos/%s/releases/tags/%s" % (it["repo"], urllib.parse.quote(tag)),
                                 headers=F._ua())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            rel = json.load(r)
    except Exception as e:
        raise E.PipeError("No he podido leer esa versión en GitHub (%s)" % E.net_reason(e))
    elfs = [a for a in rel.get("assets") or [] if a.get("name", "").lower().endswith(".elf")]
    if not elfs:
        raise E.PipeError("Esa versión no trae un .elf para cargar: mira su página de GitHub")
    stem = re.sub(r"(?i)[-_ ]?v?\d[\w.]*\.elf$|\.elf$", "", it.get("file") or it["name"]).lower()
    elfs.sort(key=lambda a: (stem not in a["name"].lower(), len(a["name"])))
    a = elfs[0]
    with urllib.request.urlopen(urllib.request.Request(a["browser_download_url"], headers=F._ua()), timeout=120) as r:
        data = r.read(64 * 1024 * 1024)
    if data[:4] != b"\x7fELF":
        raise E.PipeError("Lo que se ha bajado no es un payload (.elf)")
    name = "%s_%s.elf" % (re.sub(r"(?i)\.elf$", "", a["name"]), re.sub(r"[^\w.-]", "_", tag))
    if not any(x.get("filename") == name for x in pm.payloads()):    # ya estaba puesto: no se vuelve a subir
        pm.upload(name, data)
    hit = next((x for x in pm.payloads() if x.get("filename") == name), None)
    auto = False
    if hit:
        try:
            conf = pm.config()
            cur = [x.strip() for x in re.split(r"[,\n]", conf.get("AUTOLOAD_LIST") or "") if x.strip()]
            if it.get("slot") in cur:
                cur = [hit["slot"] if x == it["slot"] else x for x in cur]
                body = json.dumps({"AUTOLOAD_LIST": ",".join(cur), "AUTOLOAD_ENABLED": bool(conf.get("AUTOLOAD_ENABLED", True))}).encode()
                pm._req("POST", "/set_config", body=body, ctype="application/json", timeout=15)
                auto = True
        except Exception:
            pass
    st = pld_check()
    return dict(st, done=it["name"], version=tag, auto=auto)


# ---------------------------------------------------------------------------
# Guardian del firmware: que la consola no se actualice sola y pierdas el jailbreak (ideas de lvndry/ps5-firmware-pin)
# Solo borra la actualizacion que la consola ya bajo (/update/PS5UPDATE.PUP); nada de firmware ni de juegos.
# ---------------------------------------------------------------------------

GUARD = {"t": 0, "fw": "", "pending": None, "error": "", "hosts": None, "hosts_path": "", "busy": False, "deleted": 0}
GUARD_T = [0.0]
FW_VERSION_FILES = ("/system_ex/version.txt", "/system/version.txt", "/dev/nfs/system/version.txt", "/etc/version.txt",
                    "/system_data/settings/version.txt")
UPDATE_DIR = "/update"
UPDATE_HOSTS = ("update.playstation.com", "dl.playstation.com")
HOSTS_FILES = ("/dev/nfs/etc/hosts", "/etc/hosts")


def _ftp_text(f, path, n=64 * 1024):
    try:
        return F.ftp_read(f, path, 0, n).decode("utf-8", "replace")
    except Exception:
        return None


def guard_check(remove=None):
    """Mira la version del sistema y si hay una actualizacion descargada esperando. remove=True la borra."""
    cfg = cfg_snapshot()
    out = {}

    def work(f):
        fw = ""
        for p in FW_VERSION_FILES:
            t = _ftp_text(f, p, 4096)
            m = re.search(r"\d{1,2}\.\d{2}(?:\.\d+)?", t or "")
            if m:
                fw = m.group(0)
                break
        out["fw"] = fw
        pend = None
        try:
            for n, v in E.ftp_listdir(f, UPDATE_DIR):
                if v["type"] != "dir" and re.search(r"(?i)\.pup", n):
                    pend = {"name": n, "path": UPDATE_DIR + "/" + n, "size": v.get("size") or 0,
                            "mtime": v.get("mtime") or 0}
                    break
        except ftplib.all_errors:
            out["no_dir"] = True
        if pend and remove:
            try:
                f.delete(pend["path"])
            except ftplib.all_errors as e:
                raise E.PipeError("No pude borrar %s: %s" % (pend["path"], e))
            still = any(n == pend["name"] for n, _ in E.ftp_listdir(f, UPDATE_DIR))
            if still:
                raise E.PipeError("La consola no deja borrar %s ahora. Prueba otra vez desde la pantalla de inicio."
                                  % pend["path"])
            out["removed"] = pend["name"]
            pend = None
        out["pending"] = pend
        hp = ""
        state_hosts = None
        for p in HOSTS_FILES:
            t = _ftp_text(f, p)
            if t is not None:
                hp = p
                state_hosts = all(h in t for h in UPDATE_HOSTS)
                break
        out["hosts"], out["hosts_path"] = state_hosts, hp
    E.Ftp(cfg).call(work)
    with LOCK:
        GUARD.update(t=time.time(), fw=out.get("fw") or "", pending=out.get("pending"), error="",
                     hosts=out.get("hosts"), hosts_path=out.get("hosts_path") or "")
        if out.get("removed"):
            GUARD["deleted"] += 1
        real = re.match(r"\d+\.\d+", out["fw"]).group(0) if out.get("fw") else ""
        if real and real != (SETTINGS.get("console_fw") or "").strip():
            SETTINGS["console_fw"] = real   # el de verdad, leido de la consola (para «Comprobar arranque» y la Biblioteca)
            fw_new = True
        else:
            fw_new = False
    if fw_new:
        save_settings()
    if out.get("removed"):
        record_event = "Actualización del sistema quitada de la consola (%s)" % out["removed"]
        notify("ok", "Actualización quitada", record_event + ". Tu jailbreak sigue a salvo.", {"tab": "System"},
               key="guard-removed-%d" % int(time.time() // 60))
    return guard_state()


def guard_state():
    with LOCK:
        g = dict(GUARD)
        g.update(on=SETTINGS.get("fw_guard", True), auto=bool(SETTINGS.get("fw_guard_auto")))
    return g


def guard_tick():
    """Cada 10 minutos con la consola conectada: si ha bajado una actualizacion, aviso (o la quito si lo pediste)."""
    with LOCK:
        on, auto = SETTINGS.get("fw_guard", True), bool(SETTINGS.get("fw_guard_auto"))
    if not on:
        return
    try:
        st = guard_check(remove=False)
    except Exception as e:
        with LOCK:
            GUARD["error"] = str(e)
        return
    pend = st.get("pending")
    if not pend:
        return
    if auto:
        try:
            guard_check(remove=True)
            return
        except Exception as e:
            print("Guardián del firmware:", e)
    gb = (pend.get("size") or 0) / 1e9
    notify("err", "Tu PS5 ha descargado una actualización del sistema",
           "Si la instalas, pierdes el jailbreak. Pulsa aquí para quitarla (%s, %.1f GB). Y si la consola te la "
           "ofrece, pulsa ✕: nunca «Actualizar»." % (pend["name"], gb), {"tab": "System"},
           key="guard-%s-%s" % (pend["name"], pend.get("mtime")))
    try:
        shots_text("⚠️ PS5 Directo: tu PS5 ha descargado una actualización del sistema. Si la instalas pierdes el "
                   "jailbreak. Ábrela en PS5 Directo › Sistema › Guardián del firmware para quitarla.")
    except Exception:
        pass


def shots_text(text):
    """Avisos importantes tambien por tu bot de Telegram o webhook de Discord (si los tienes puestos en Capturas)."""
    cfg = cfg_snapshot()
    if not cfg.get("shots_send"):
        return
    tok, chat, hook = cfg.get("shots_tg_token") or "", cfg.get("shots_tg_chat") or "", cfg.get("shots_dc_hook") or ""
    if tok and chat:
        req = urllib.request.Request("%s/bot%s/sendMessage" % (TG_API, tok),
                                     data=json.dumps({"chat_id": chat, "text": text}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=20).read()
    if hook and re.match(DC_RX, hook):
        req = urllib.request.Request(hook, method="POST", data=json.dumps({"content": text}).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "PS5Directo"})
        urllib.request.urlopen(req, timeout=20).read()


def guard_hosts(block):
    """block=True: apunta los servidores de actualizacion a 127.0.0.1 en el hosts de la consola (con copia).
    block=False: deja el hosts como estaba. Solo funciona si el hosts se puede escribir (kstuff o etaHEN)."""
    cfg = cfg_snapshot()
    res = {}

    def work(f):
        path = next((p for p in HOSTS_FILES if _ftp_text(f, p) is not None), None)
        if not path:
            raise E.PipeError("No encuentro el archivo hosts de la consola.")
        bak = path + ".ps5directo.bak"
        cur = _ftp_text(f, path) or ""
        if block:
            if all(h in cur for h in UPDATE_HOSTS):
                res["already"] = True
                return
            if _ftp_text(f, bak) is None:
                f.storbinary("STOR " + bak, io.BytesIO(cur.encode("utf-8")))
            add = "".join("127.0.0.1   %s\n" % h for h in UPDATE_HOSTS if h not in cur)
            new = cur + ("" if cur.endswith("\n") or not cur else "\n") + add
        else:
            old = _ftp_text(f, bak)
            new = old if old is not None else "".join(ln + "\n" for ln in cur.splitlines()
                                                       if not any(h in ln for h in UPDATE_HOSTS))
        try:
            f.storbinary("STOR " + path, io.BytesIO(new.encode("utf-8")))
        except ftplib.all_errors as e:
            raise E.PipeError("La consola no deja escribir %s (%s). Hace falta kstuff o etaHEN con el sistema de "
                              "archivos completo; si no, usa la opción del DNS." % (path, e))
        now = _ftp_text(f, path) or ""
        ok = all(h in now for h in UPDATE_HOSTS) if block else not any(h in now for h in UPDATE_HOSTS)
        if not ok:
            raise E.PipeError("El cambio en %s no se ha quedado guardado (la consola lo tiene en solo lectura). "
                              "Usa la opción del DNS." % path)
        res["path"] = path
    E.Ftp(cfg).call(work)
    guard_check(remove=False)
    return dict(res, **guard_state())


# --- YouTube (para quien entra con Y2JB): que la consola no actualice la app y se pierda el jailbreak.
# Lo mismo que hacen appinfo_editor.py de Gezine/Y2JB y la guia de SvenGDK: version 99.999.999 y la direccion
# de actualizaciones apuntando a 127.0.0.2, con copia de todo en el PC para deshacerlo.
YT_TIDS = ("PPSA01650", "PPSA01651", "PPSA01652")
YT_FAKE_VER, YT_FAKE_URI = "99.999.999", "http://127.0.0.2"
MMS = "/system_data/priv/mms"


_YT_KEY = "__system_data__priv__mms__appinfo.db"     # copia en el PC: la ruta de la consola con / cambiado por __


def yt_dir():
    return os.path.join(DATA_DIR, "youtube-copia")


def _yt_appmeta(tid):
    return ["/system_data/priv/appmeta/%s/param.json" % tid, "/user/appmeta/%s/param.json" % tid]


def yt_state():
    cfg = cfg_snapshot()
    out = {"installed": [], "blocked": None, "version": "", "backup": os.path.isfile(os.path.join(yt_dir(), "valores_originales.json"))}

    def work(f):
        for tid in YT_TIDS:
            for p in _yt_appmeta(tid):
                t = _ftp_text(f, p, 512 * 1024)
                if t:
                    try:
                        j = json.loads(t)
                    except ValueError:
                        j = {}
                    out["installed"].append(tid)
                    v = str(j.get("contentVersion") or "")
                    out["version"] = out["version"] or v
                    if out["blocked"] is None:
                        out["blocked"] = v == YT_FAKE_VER
                    break
    E.Ftp(cfg).call(work)
    return out


def yt_block(undo=False):
    """Bloquea (o deshace) las actualizaciones de YouTube.
    - Se baja appinfo.db, app.db y el param.json de YouTube, se cambian SOLO las filas de YouTube y se comprueba
      que la base de datos sigue sana antes de subirla.
    - Se sube con otro nombre y luego se renombra (si se corta a medias, la consola sigue con la buena).
    - Deshacer vuelve a poner los valores originales de YouTube en la base de datos de AHORA (no se pisa entera con
      una copia vieja: se perderia lo instalado despues). Las copias completas quedan en ~/.ps5directo/youtube-copia."""
    import sqlite3
    import shutil
    import tempfile
    cfg = cfg_snapshot()
    bdir = yt_dir()
    os.makedirs(bdir, exist_ok=True)
    orig_file = os.path.join(bdir, "valores_originales.json")
    tmp = tempfile.mkdtemp(prefix="ps5d-yt-")
    done = []

    def get(f, remote, local):
        with open(local, "wb") as fh:
            f.retrbinary("RETR " + remote, fh.write, 65536)

    def put(f, local, remote):
        part = remote + ".ps5d-nuevo"
        with open(local, "rb") as fh:
            f.storbinary("STOR " + part, fh, 65536)
        size = os.path.getsize(local)
        try:
            got = int(f.sendcmd("SIZE " + part).split()[-1])
        except (ftplib.all_errors, ValueError):
            got = size
        if got != size:
            try:
                f.delete(part)
            except ftplib.all_errors:
                pass
            raise E.PipeError("La copia de %s no ha llegado entera a la consola; no he cambiado nada." % remote)
        f.sendcmd("RNFR " + part)
        f.sendcmd("RNTO " + remote)

    def sane(db):
        con = sqlite3.connect(db)
        try:
            return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            con.close()

    try:
        with open(orig_file, encoding="utf-8") as fh:
            orig = json.load(fh)
    except (OSError, ValueError):
        orig = {}

    def work(f):
        installed = [t for t in YT_TIDS if any(_ftp_text(f, p, 1024) is not None for p in _yt_appmeta(t))]
        if not installed:
            raise E.PipeError("No encuentro YouTube instalado en la consola.")
        if undo and not orig:
            raise E.PipeError("No hay valores originales guardados en este PC para deshacerlo.")
        files = [MMS + "/appinfo.db", MMS + "/app.db"] + [p for t in installed for p in _yt_appmeta(t)]
        local = {}
        for r in files:
            lp = os.path.join(tmp, r.replace("/", "__"))
            try:
                get(f, r, lp)
            except ftplib.all_errors:
                continue
            local[r] = lp
            keep = os.path.join(bdir, r.replace("/", "__"))
            if not os.path.isfile(keep):          # copia completa de antes de tocar nada, por si acaso
                shutil.copy2(lp, keep)
        adb = MMS + "/appinfo.db"
        if adb not in local:
            raise E.PipeError("No pude leer %s de la consola." % adb)
        if not sane(local[adb]):
            raise E.PipeError("appinfo.db de la consola ya viene dañada: no la toco. Reinicia la consola y prueba otra vez.")
        con = sqlite3.connect(local[adb])
        try:
            for t in installed:
                rows = dict(con.execute("SELECT key, val FROM tbl_appinfo WHERE titleId=? AND key IN "
                                        "('CONTENT_VERSION','VERSION_FILE_URI')", (t,)).fetchall())
                if len(rows) != 2:
                    continue
                if not undo:
                    if rows["CONTENT_VERSION"] != YT_FAKE_VER:
                        orig.setdefault(t, {}).update(ver=rows["CONTENT_VERSION"], uri=rows["VERSION_FILE_URI"])
                    ver, uri = YT_FAKE_VER, YT_FAKE_URI
                else:
                    o = orig.get(t)
                    if not o:
                        continue
                    ver, uri = o["ver"], o["uri"]
                con.execute("UPDATE tbl_appinfo SET val=? WHERE titleId=? AND key='CONTENT_VERSION'", (ver, t))
                con.execute("UPDATE tbl_appinfo SET val=? WHERE titleId=? AND key='VERSION_FILE_URI'", (uri, t))
            con.commit()
        finally:
            con.close()
        if not undo:
            with open(orig_file, "w", encoding="utf-8") as fh:
                json.dump(orig, fh, indent=1)
        if not sane(local[adb]):
            raise E.PipeError("Algo ha salido mal al preparar appinfo.db: no he cambiado nada en la consola.")
        # app.db (su copia en JSON), solo si se deja hacer limpio
        if MMS + "/app.db" in local:
            try:
                con = sqlite3.connect(local[MMS + "/app.db"])
                cols = [r[1] for r in con.execute("PRAGMA table_info(tbl_contentinfo)")]
                if "AppInfoJson" in cols:
                    for t in installed:
                        row = con.execute("SELECT AppInfoJson FROM tbl_contentinfo WHERE titleId=?", (t,)).fetchone()
                        o = orig.get(t) or {}
                        if row and row[0] and (not undo or o):
                            j = json.loads(row[0])
                            j["CONTENT_VERSION"] = o.get("ver") if undo else YT_FAKE_VER
                            j["VERSION_FILE_URI"] = o.get("uri") if undo else YT_FAKE_URI
                            con.execute("UPDATE tbl_contentinfo SET AppInfoJson=? WHERE titleId=?",
                                        (json.dumps(j, separators=(",", ":")), t))
                    con.commit()
                con.close()
                if not sane(local[MMS + "/app.db"]):
                    raise ValueError("integridad")
            except Exception as e:
                print("YouTube, app.db (no se toca):", e)
                local.pop(MMS + "/app.db", None)
        for r, lp in list(local.items()):
            if r.endswith("param.json"):
                tid = r.split("/")[-2]
                o = orig.get(tid) or {}
                if undo and not o:
                    local.pop(r)
                    continue
                with open(lp, encoding="utf-8-sig") as fh:
                    j = json.load(fh)
                j["contentVersion"] = o.get("ver") if undo else YT_FAKE_VER
                j["versionFileUri"] = o.get("uri") if undo else YT_FAKE_URI
                with open(lp, "w", encoding="utf-8") as fh:
                    json.dump(j, fh, ensure_ascii=False, indent=2)
        # primero lo pequeño; la base de datos la ultima
        for r in sorted(local, key=lambda x: x.endswith(".db")):
            put(f, local[r], r)
            done.append(r)
    try:
        E.Ftp(cfg).call(work)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"done": done, "backup": bdir}


# ---------------------------------------------------------------------------
# Cierres al arrancar detectados solos: cuando ShadowMountPlus apunta que algo se ha cerrado, se revisa ese
# titulo con «Comprobar arranque» y se avisa con la causa mas probable (sin tener que buscarla).
# ---------------------------------------------------------------------------

CRASH = {"seen": set(), "init": False, "last": []}
CRASH_T = [0.0]
CRASH_RX = re.compile(r"game error: ([A-Z]{4}\d{5})[^\n]*?: signal: (\d+) \((\w+)\)(?:\s*thread name: (\S+))?")


def _lib_path_for(tid):
    for x in ((LIB.get("items") or {}).get("items") or []):
        if (x.get("tid") or "").upper() == tid and x.get("path"):
            return x["path"], x.get("title") or tid
    return None, tid


def crash_explain(tid, sig=""):
    """Revisa un titulo que no ha arrancado y avisa con lo mas probable."""
    path, title = _lib_path_for(tid)
    top = None
    if path:
        try:
            r = launch_check(path, 15, quick=True)
            rank = {"bad": 3, "warn": 2}
            probs = sorted([c for c in r.get("checks") or [] if c["level"] in rank
                            and not c["text"].startswith("Falta la portada")], key=lambda c: -rank[c["level"]])
            top = probs[0] if probs else None
            title = r.get("title") or title
        except Exception as e:
            print("Revisar cierre:", e)
    if top:
        body = "Lo más probable: %s %s" % (top["text"], top.get("fix") or "")
    elif sig:
        body = LAUNCH_CODES.get(sig) or "Se cerró con %s." % sig
    else:
        body = "No encuentro nada raro en la carpeta. Pulsa para ver el registro y las últimas capturas."
    notify("err", "«%s» se ha cerrado al arrancar%s" % (title, " (%s)" % sig if sig else ""), body,
           {"tab": "Library", "check": path, "title": title} if path else {"tab": "Library"},
           key="crash-%s-%d" % (tid, int(time.time() // 120)))
    with LOCK:
        CRASH["last"].insert(0, {"t": time.time(), "tid": tid, "title": title, "sig": sig, "body": body, "path": path})
        del CRASH["last"][10:]


def crash_tick(force=False):
    if not force and time.time() - CRASH_T[0] < 90:
        return
    CRASH_T[0] = time.time()
    cfg = cfg_snapshot()
    found = []

    def work(f):
        since = _console_now(f) - 20 * 60
        for lg in _recent_logs(f, ["/data/shadowmount", "/data/shadowmountplus"], since, limit=4):
            for m in CRASH_RX.finditer(lg.get("tail") or ""):
                found.append((m.group(0), m.group(1).upper(), m.group(3)))
    try:
        E.Ftp(cfg).call(work)
    except Exception:
        return
    fresh = [x for x in found if x[0] not in CRASH["seen"]]
    for x in found:
        CRASH["seen"].add(x[0])
    if not CRASH["init"]:          # lo que ya estaba al abrir el programa no se avisa
        CRASH["init"] = True
        return
    for _, tid, sig in fresh[-3:]:
        crash_explain(tid, sig)


def after_launch(tid):
    """Tras «Abrir en la consola»: si a los 25 s no esta abierto (lo dice ps5-exporter), se revisa por que."""
    time.sleep(25)
    try:
        crash_tick(force=True)
    except Exception:
        pass
    now = dict(NOWPLAY)
    if now.get("src") == "exporter" and time.time() - now.get("t", 0) < 120 and now.get("tid") and now["tid"] != tid:
        crash_explain(tid)


# ---------------------------------------------------------------------------
# Novedades de la scene: lo que se publica en el catalogo de RecompStore (recompstore.com/catalog.json), solo para
# leerlo (nombre, autor, que es, version y su pagina). PS5 Directo no instala ni aloja nada de eso.
# ---------------------------------------------------------------------------

RECOMP_URL = os.environ.get("PS5D_RECOMP") or "https://recompstore.com/catalog.json"
RECOMP = {"t": 0, "items": [], "error": "", "updated": ""}
RECOMP_FILE = os.path.join(DATA_DIR, "novedades_scene.json")


def _recomp_item(x, soon):
    src = x.get("source") if isinstance(x.get("source"), dict) else {}
    repo = str(src.get("repo") or x.get("repo") or "")   # catalogo de RecompStore: "repo" va suelto
    tags = x.get("tags") if isinstance(x.get("tags"), list) else []
    req = [r for r in (x.get("requires") or []) if isinstance(r, str)][:8]
    when = str(x.get("released") or x.get("date") or "")[:25]
    shots = []
    for k in ("screenshots", "images", "gallery", "media", "shots"):
        for v in (x.get(k) if isinstance(x.get(k), list) else []):
            u = v if isinstance(v, str) else (v.get("url") or v.get("src") or v.get("image") or "") if isinstance(v, dict) else ""
            if u and not re.search(r"\.(mp4|webm|m3u8)(\?|$)", str(u), re.I):
                shots.append(str(u))
    vid = ""
    for k in ("video", "trailer", "youtube"):
        v = x.get(k)
        v = v.get("url") if isinstance(v, dict) else v
        if isinstance(v, str) and re.match(r"^https://", v):
            vid = v[:300]
            break
    pop = 0
    for k, v in x.items():                         # downloads, downloadCount, installs, popularity, stars…
        if re.search(r"(?i)download|install|popular|views|stars|likes|plays", str(k)) and not isinstance(v, (dict, list, bool)):
            try:
                pop = max(pop, int(float(v or 0)))
            except (TypeError, ValueError):
                pass
        elif isinstance(v, dict) and re.search(r"(?i)stat|metric|count", str(k)):
            for k2, v2 in v.items():
                if re.search(r"(?i)download|install|popular|views", str(k2)) and isinstance(v2, (int, float)):
                    pop = max(pop, int(v2))
    def url(u):
        u = str(u or "")
        if u.startswith("/"):
            u = "https://recompstore.com" + u
        return u if re.match(r"^https://", u) else ""
    return {"id": str(x.get("id") or "")[:60], "name": str(x.get("name") or x.get("id") or "")[:120],
            "author": str(x.get("author") or "")[:80], "cat": str(x.get("category") or "games")[:20],
            "blurb": str(x.get("blurb") or "")[:300], "desc": str(x.get("description") or "")[:2500],
            "needs": str(x.get("needs") or "")[:600], "version": str(x.get("version") or "")[:30],
            "when": when, "repo": repo if re.match(r"^[\w.-]+/[\w.-]+$", repo) else "",
            "icon": url(x.get("icon")), "hero": url(x.get("hero") or x.get("banner")), "tags": [str(t)[:30] for t in tags][:8],
            "requires": req, "soon": soon, "news": str(x.get("news") or "")[:600],
            "shots": [u for u in (url(v) for v in shots) if u][:8], "video": vid, "pop": pop,
            "added": str(x.get("added") or x.get("addedAt") or x.get("createdAt") or x.get("created") or x.get("dateAdded")
                         or x.get("firstReleased") or x.get("published") or "")[:25]}



# Correcciones de portadas (portadas.json en el GitHub de PS5 Directo): para los pocos casos en que la imagen del
# catálogo no representa bien un port. Solo cambia lo que está en la lista.
COVERS_URL = os.environ.get("PS5D_COVERS") or "https://raw.githubusercontent.com/jstrdev/ps5directo/main/portadas.json"
COVERS = {"t": 0, "d": {}}


def covers_fix():
    if time.time() - COVERS["t"] < 6 * 3600:
        return COVERS["d"]
    d = {}
    try:
        req = urllib.request.Request(COVERS_URL, headers={"User-Agent": "PS5Directo/" + VERSION})
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read(500000).decode("utf-8", "replace"))
        if isinstance(j, dict):
            d = {str(k).lower(): v for k, v in j.items() if isinstance(v, dict) and not str(k).startswith("_")}
    except Exception:
        try:
            with open(os.path.join(HERE, "portadas.json"), encoding="utf-8") as fh:
                j = json.load(fh)
            d = {str(k).lower(): v for k, v in j.items() if isinstance(v, dict) and not str(k).startswith("_")}
        except Exception:
            d = COVERS["d"]
    COVERS.update(t=time.time(), d=d)
    return d

FEAT_URL = os.environ.get("PS5D_FEAT") or "https://raw.githubusercontent.com/jstrdev/ps5directo/main/destacados.json"
FEAT = {"t": 0.0, "d": []}


def featured():
    """Los destacados del cartel de Descubrir: un destacados.json en GitHub que se cambia sin sacar versión nueva
    (lo que más se está jugando o lo nuevo de la emulación). Si GitHub no contesta, el que viene con la app."""
    if time.time() - FEAT["t"] < 3 * 3600:
        return FEAT["d"]
    j = None
    try:
        req = urllib.request.Request(FEAT_URL, headers={"User-Agent": "PS5Directo/" + VERSION})
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read(500000).decode("utf-8", "replace"))
    except Exception:
        try:
            with open(os.path.join(HERE, "destacados.json"), encoding="utf-8") as fh:
                j = json.load(fh)
        except Exception:
            j = None
    items = []
    today = time.strftime("%Y-%m-%d")
    for x in ((j or {}).get("items") if isinstance(j, dict) else j) or []:
        if not isinstance(x, dict) or not x.get("title"):
            continue
        if x.get("until") and str(x["until"]) < today:
            continue                      # caducado
        items.append({k: x.get(k) for k in ("id", "title", "title_en", "text", "text_en", "img", "badge", "badge_en",
                                            "cat", "cat_en", "install", "url", "pro", "until", "note", "note_en")})
    FEAT.update(t=time.time(), d=items[:10] if j is not None else FEAT["d"])
    return FEAT["d"]


ESC_URL = os.environ.get("PS5D_ESCENA") or "https://raw.githubusercontent.com/jstrdev/ps5directo/main/escena.json"
ESC = {"t": 0.0, "d": [], "upd": ""}


def escena():
    """Novedades de la scene que se generan solas: una GitHub Action del repo de PS5 Directo (tools/escena.py) mira
    cada 3 horas las versiones nuevas de todo lo de Descubrir y los proyectos nuevos de PS5 en GitHub y deja la
    lista en escena.json. La app solo la lee (1 petición por hora, sin gastar el límite de GitHub de cada uno)."""
    if time.time() - ESC["t"] < 3600:
        return ESC["d"]
    j = None
    try:
        req = urllib.request.Request(ESC_URL, headers={"User-Agent": "PS5Directo/" + VERSION})
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read(2000000).decode("utf-8", "replace"))
    except Exception:
        try:
            with open(os.path.join(HERE, "escena.json"), encoding="utf-8") as fh:
                j = json.load(fh)
        except Exception:
            j = None
    out = []
    for x in ((j or {}).get("items") or []) if isinstance(j, dict) else []:
        if not isinstance(x, dict) or not re.match(r"^[\w.-]+/[\w.-]+$", str(x.get("repo") or "")):
            continue
        u = str(x.get("url") or "")
        out.append({"id": str(x.get("id") or "")[:120], "kind": "new" if x.get("kind") == "new" else "update",
                    "repo": x["repo"], "cid": str(x.get("cid") or "")[:40], "name": str(x.get("name") or x["repo"])[:80],
                    "tag": str(x.get("tag") or "")[:40], "pre": bool(x.get("pre")), "date": str(x.get("date") or "")[:25],
                    "notes": str(x.get("notes") or "")[:400], "desc": str(x.get("desc") or "")[:300],
                    "stars": int(x.get("stars") or 0) if str(x.get("stars") or "0").isdigit() else 0,
                    "url": u if u.startswith("https://github.com/") else "https://github.com/" + x["repo"]})
    if j is not None:
        ESC.update(t=time.time(), d=out[:80], upd=str((j or {}).get("updated") or "")[:30])
    else:
        ESC["t"] = time.time() - 3000           # sin red: se reintenta en 10 minutos
    return ESC["d"]


def recomp_fetch(force=False):
    with LOCK:
        fresh = time.time() - RECOMP["t"] < 3600 and RECOMP["items"]
    if fresh and not force:
        return
    try:
        req = urllib.request.Request(RECOMP_URL, headers={"User-Agent": "PS5Directo/%s" % VERSION, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read(8 * 1024 * 1024).decode("utf-8", "replace"))
        items, seen = [], set()
        known = ["apps", "newerStoreApps", "v2Apps", "comingSoon", "comingSoonV2"]
        keys = known + [k for k, v in j.items() if k not in known and isinstance(v, list) and v and isinstance(v[0], dict)]
        for key in keys:                              # todas las listas del catálogo, también las que añadan más adelante
            soon = "soon" in key.lower()
            for x in j.get(key) or []:
                if not isinstance(x, dict) or x.get("beta") or x.get("testing") or not (x.get("name") or x.get("id")):
                    continue
                plat = x.get("platform") or x.get("platforms") or ""
                plat = " ".join(plat) if isinstance(plat, list) else str(plat)
                if plat and "ps5" not in plat.lower():
                    continue                          # solo lo de PS5 (el catálogo también puede traer cosas de PC)
                it = _recomp_item(x, soon)
                if it["id"] and it["id"] not in seen:
                    seen.add(it["id"])
                    it["pos"] = len(items)            # orden del catálogo (las tiendas añaden lo nuevo al final)
                    it["src"] = key
                    items.append(it)
        items.sort(key=lambda x: (not x["soon"], x["when"]), reverse=True)
        rank = []                                     # listas de «populares» del catálogo (ids o fichas, en orden)
        for k, v in j.items():
            if isinstance(v, list) and re.search(r"(?i)popular|trending|top|featured|mostused", k):
                for e in v:
                    i = e if isinstance(e, str) else (e.get("id") if isinstance(e, dict) else None)
                    if i and i not in rank:
                        rank.append(str(i))
        for it in items:
            if it["id"] in rank:
                it["prank"] = rank.index(it["id"]) + 1
        prev = {}
        try:
            with open(RECOMP_FILE, encoding="utf-8") as fh:
                prev = json.load(fh)
        except Exception:
            pass
        known = set(prev.get("known") or [])
        seen_at = dict(prev.get("seen_at2") or {})     # cuándo vio PS5 Directo cada cosa por primera vez
        now = int(time.time())
        for it in items:                              # lo que ya conocía antes de guardar fechas cuenta como «de siempre»
            seen_at.setdefault(it["id"], 0 if (not known or it["id"] in known or not prev.get("seen_at2")) else now)
            it["seen"] = seen_at[it["id"]]
        first = not known
        for it in items:
            it["new"] = (not first) and it["id"] not in known
            it["new"] = it["new"] or it["id"] in set(prev.get("unseen") or [])
        unseen = [it["id"] for it in items if it["new"]]
        try:
            with open(RECOMP_FILE, "w", encoding="utf-8") as fh:
                json.dump({"known": [it["id"] for it in items], "unseen": unseen, "seen_at2": seen_at}, fh)
        except OSError:
            pass
        with LOCK:
            RECOMP.update(t=time.time(), items=items, error="", updated=str(j.get("updated") or "")[:30])
        if unseen and not first:
            notify("info", "Novedades de la scene: %d nuevas" % len(unseen),
                   ", ".join(it["name"] for it in items if it["new"])[:300], {"tab": "Library", "view": "news"},
                   key="recomp-%s" % ",".join(sorted(unseen))[:120])
    except Exception as e:
        with LOCK:
            RECOMP.update(error=str(e) if isinstance(e, E.PipeError) else
                          "recompstore.com: %s" % e, t=time.time())


def recomp_seen():
    try:
        with open(RECOMP_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        d["unseen"] = []
        with open(RECOMP_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
    except Exception:
        pass
    with LOCK:
        for it in RECOMP["items"]:
            it["new"] = False


# ---------------------------------------------------------------------------
# «Descubrir» en la ficha de cada titulo: resumen de Wikipedia (sin cuentas ni claves) y enlaces de busqueda
# ---------------------------------------------------------------------------

WIKI = {}
WIKI_API = os.environ.get("PS5D_WIKI") or "https://%s.wikipedia.org"


def wiki_summary(title, lang):
    title = re.sub(r"[™®©]", "", str(title or "")).strip()[:120]
    lang = "en" if lang == "en" else "es"
    if not title:
        return {}
    k = (title.lower(), lang)
    if k in WIKI and time.time() - WIKI[k]["t"] < 7 * 86400:
        return WIKI[k]["v"]
    base = WIKI_API % lang if "%s" in WIKI_API else WIKI_API
    out = {}
    try:
        q = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": title + " videojuego"
                                    if lang == "es" else title + " video game", "srlimit": 3, "format": "json"})
        req = urllib.request.Request(base + "/w/api.php?" + q, headers={"User-Agent": "PS5Directo/%s (desktop app)" % VERSION})
        with urllib.request.urlopen(req, timeout=10) as r:
            hits = (json.load(r).get("query") or {}).get("search") or []
        words = set(re.findall(r"\w{3,}", title.lower()))
        hit = next((h for h in hits if words and len(words & set(re.findall(r"\w{3,}", h["title"].lower()))) >= max(1, len(words) // 2)), None)
        if hit:
            req = urllib.request.Request(base + "/api/rest_v1/page/summary/" + urllib.parse.quote(hit["title"].replace(" ", "_")),
                                         headers={"User-Agent": "PS5Directo/%s (desktop app)" % VERSION})
            with urllib.request.urlopen(req, timeout=10) as r:
                j = json.load(r)
            out = {"title": j.get("title") or hit["title"], "text": (j.get("extract") or "")[:1200],
                   "url": ((j.get("content_urls") or {}).get("desktop") or {}).get("page") or "",
                   "img": ((j.get("thumbnail") or {}).get("source") or ""), "lang": lang}
    except Exception as e:
        out = {"error": conn_error_text(e)}
    WIKI[k] = {"t": time.time(), "v": out}
    return out


CREDITS_CORE = [
    ("ftpsrv", "ps5-payload-dev/ftpsrv", "Servidor FTP de la consola"),
    ("elfldr", "ps5-payload-dev/elfldr", "Cargador de payloads (puerto 9021)"),
    ("Payload Manager", "itsPLK/ps5-payload-manager", "Carga automática de payloads"),
    ("ShadowMountPlus", "drakmor/ShadowMountPlus", "Monta las imágenes .exfat y .ffpkg"),
    ("PS5 Payload SDK", "ps5-payload-dev/sdk", "Herramientas con las que se hacen los payloads"),
    ("rclone", "rclone/rclone", "Mis almacenamientos (nubes)"),
    ("Bazzite (press kit)", "ublue-os/bazzite", "Logo e imagen de la tarjeta de Bazzite Deck en Descubrir"),
    ("7-Zip", "ip7z/7zip", "Descomprimir en el PC"),
    ("aria2", "aria2/aria2", "Torrents"),
    ("FFmpeg", "FFmpeg/FFmpeg", "Convertir vídeos de las capturas"),
    ("python-build-standalone", "astral-sh/python-build-standalone", "El Python que va dentro de la app"),
    ("Game Library Renamer", "pristmc-code/game-library-renamer", "La idea de «Ordenar nombres»"),
]


def credits_list():
    """Proyectos libres que usa o recomienda PS5 Directo, con su autor (para la ventana «Créditos»)."""
    seen, out = set(), []
    def add(name, repo, what, kind):
        if repo and repo.lower() not in seen:
            seen.add(repo.lower())
            out.append({"name": name, "repo": repo, "author": repo.split("/")[0], "what": what, "kind": kind})
    for n, r, w in CREDITS_CORE:
        add(n, r, w, "core")
    for x in F.EXTRA_PAYLOADS:
        add(x["name"], x.get("repo"), x.get("what", ""), "payload")
    add("BFpilot", "ItsBlurf/BFpilot", "Explorador rápido, descomprimir en la consola", "payload")
    add("PS5 Dump Forge", F.FORGE_REPO, "Convertir a .exfat y .ffpkg", "core")
    for x in F.CATALOG:
        add(x["name"], x.get("repo"), x.get("what", ""), x.get("group", "app"))
    return out


def extra_api(h, p, b):
    """Rutas de la 1.20. Devuelve el dict de respuesta o None si no es suya."""
    try:
        if p == "/api/credits":
            return {"ok": True, "items": credits_list()}
        if p == "/api/bug/list":
            with AUTOBUG_LOCK:
                items = [dict(x) for x in AUTOBUG["items"]]
                if b.get("seen"):
                    for x in AUTOBUG["items"]:
                        x["seen"] = True
            return {"ok": True, "items": items, "os": bug_os_label(), "platform": sys.platform,
                    "testing": sys.platform != "nt" and os.name != "nt"}
        if p == "/api/bug/js":
            bug_record("interfaz: %s" % str(b.get("src") or "")[-80:], None,
                       "%s\n%s" % (str(b.get("msg") or "")[:500], str(b.get("stack") or "")[:3000]), js=True)
            with AUTOBUG_LOCK:
                if AUTOBUG["items"] and AUTOBUG["items"][0]["js"]:
                    AUTOBUG["items"][0]["msg"] = _scrub(str(b.get("msg") or "Error en la interfaz"))[:400]
            return {"ok": True}
        if p == "/api/guard":
            if "on" in b or "auto" in b:
                with LOCK:
                    if "on" in b:
                        SETTINGS["fw_guard"] = bool(b.get("on"))
                    if "auto" in b:
                        SETTINGS["fw_guard_auto"] = bool(b.get("auto"))
                save_settings()
            if b.get("hosts") in ("block", "unblock"):
                return dict(ok=True, **guard_hosts(b["hosts"] == "block"))
            if b.get("check") or b.get("remove"):
                return dict(ok=True, **guard_check(remove=bool(b.get("remove"))))
            return dict(ok=True, **guard_state())
        if p == "/api/youtube":
            if b.get("block") or b.get("undo"):
                return dict(ok=True, **yt_block(undo=bool(b.get("undo"))))
            return dict(ok=True, **yt_state())
        if p == "/api/alembic":
            cfg = cfg_snapshot()
            run = False
            try:
                _alembic("GET", "/api/info", timeout=3)
                run = True
            except Exception:
                pass
            ip = cfg.get("ps5_ip") or ""
            pm = bool(ip) and not run and _port_open(ip, F.PLDMGR_PORT, 1.2)
            elf = bool(ip) and not run and _port_open(ip, cfg.get("elf_port") or 9021, 1.2)
            return {"ok": True, "running": run, "can_load": run or pm or elf}
        if p == "/api/recomp":
            if b.get("seen"):
                recomp_seen()
                return {"ok": True}
            recomp_fetch(force=bool(b.get("refresh")))
            fix = covers_fix()
            with LOCK:
                its, err, upd = RECOMP["items"], RECOMP["error"], RECOMP["updated"]
            return {"ok": True, "items": its, "covers": fix, "error": err, "updated": upd,
                    "source": "recompstore.com", "feat": featured(), "scene": escena()}
        if p == "/api/wiki":
            return dict(ok=True, **wiki_summary(b.get("title"), b.get("lang")))
        if p == "/api/pld/updates":
            if b.get("update"):
                return dict(ok=True, **pld_update(str(b.get("update"))))
            if b.get("check") or b.get("refresh"):
                return dict(ok=True, **pld_check(refresh=bool(b.get("refresh"))))
            return dict(ok=True, **pld_state())
        if p == "/api/crash/last":
            with LOCK:
                return {"ok": True, "items": list(CRASH["last"])}
        if p == "/api/payload/list":
            with LOCK:
                port = SETTINGS.get("elf_port") or 9021
            return {"ok": True, "items": payload_list(), "elf_port": port}
        if p == "/api/payload/send":
            return dict(ok=True, **payload_send(pid=str(b.get("id") or "")))
        if p == "/api/payload/file":
            return dict(ok=True, **payload_send(path=str(b.get("path") or "")))
        if p == "/api/console/status":
            return dict(ok=True, **console_status())
        if p == "/api/console/fix":
            return dict(ok=True, **console_fix(str(b.get("id") or ""), str(b.get("action") or "start")))
        if p == "/api/library/scan":
            library_start(bool(b.get("refresh")))
            return {"ok": True}
        if p == "/api/library/state":
            with LOCK:
                return {"ok": True, "lib": dict(LIB)}
        if p == "/api/library/cancel":
            LIB_CANCEL.set()
            return {"ok": True}
        if p == "/api/diag":
            cfg = cfg_snapshot()
            with LOCK:
                active = sum(1 for j in JOBS if j["status"] in ("comprobando", "enviando", "convirtiendo"))
            return {"ok": True, "items": F.diagnose(cfg.get("ps5_ip"), cfg, SPEEDTEST.get("result"), active)}
        if p == "/api/schedule":
            return dict(ok=True, **sched_set(b))
        if p == "/api/lan":
            if "enable" in b:
                with LOCK:
                    SETTINGS["lan_access"] = bool(b.get("enable"))
                    if b.get("regen"):
                        SETTINGS["lan_token"] = secrets.token_urlsafe(16)
                        LAN_DEVICES.clear()      # los que tenian el codigo viejo ya no entran
                save_settings()
                if b.get("enable"):
                    if b.get("regen"):
                        lan_stop()
                    lan_start()
                else:
                    lan_stop()
            return dict(ok=True, **lan_info())
        if p == "/api/title/launch":
            return dict(ok=True, **title_launch(str(b.get("tid") or "")))
        if p == "/api/pkg/install":
            return dict(ok=True, **pkg_install(str(b.get("path") or ""), bool(b.get("overwrite"))))
        if p == "/api/pkg/state":
            return dict(ok=True, **pkg_state())
        if p == "/api/emu/info":
            return dict(ok=True, **emu_info(str(b.get("id") or "")))
        if p == "/api/emu/read":
            return dict(ok=True, **emu_read(str(b.get("id") or ""), str(b.get("path") or "")))
        if p == "/api/emu/write":
            return dict(ok=True, **emu_write(str(b.get("id") or ""), str(b.get("path") or ""), b.get("text") or ""))
        if p == "/api/emu/put":
            return dict(ok=True, **emu_put(str(b.get("id") or ""), str(b.get("what") or ""),
                                           [str(x) for x in (b.get("paths") or [])][:500]))
        if p == "/api/cloud/state":
            return dict(ok=True, **cloud_state())
        if p == "/api/cloud/install":
            cloud_install()
            return {"ok": True}
        if p == "/api/cloud/add":
            cloud_add(str(b.get("name") or ""), str(b.get("type") or ""), b.get("fields") or {})
            return {"ok": True}
        if p == "/api/cloud/manual":
            exe = cloud_exe()
            if not exe:
                raise E.PipeError("Primero instala rclone")
            if not F.open_terminal([exe, "config"]):
                raise E.PipeError("No pude abrir una terminal. Abre una y escribe: %s config" % exe)
            return {"ok": True}
        if p == "/api/cloud/remove":
            F.rclone_run(cloud_exe(), ["config", "delete", _remote_ok(str(b.get("name") or ""))], 30)
            return {"ok": True}
        if p == "/api/cloud/ls":
            return dict(ok=True, **cloud_ls(str(b.get("remote") or ""), str(b.get("path") or "")))
        if p == "/api/cloud/send":
            items = [x for x in (b.get("items") or []) if isinstance(x, dict) and x.get("path")][:500]
            return dict(ok=True, **cloud_send(str(b.get("remote") or ""), items, "pc" if b.get("to") == "pc" else "ps5"))
        if p == "/api/upd/state":
            if b.get("check"):
                upd_check(bool(b.get("force")))
            if b.get("cancel"):
                UPD_CANCEL.set()
            return dict(ok=True, **upd_state())
        if p == "/api/sys/state":
            return dict(ok=True, **sys_state())
        if p == "/api/shots/send":
            if "cfg" in b:
                c = b.get("cfg") or {}
                with LOCK:
                    for k in ("shots_send", "shots_tg_chat"):
                        if k in c:
                            SETTINGS[k] = str(c[k] or "").strip()
                    for k in ("shots_tg_token", "shots_dc_hook"):
                        if c.get(k) and c[k] != "••••":
                            SETTINGS[k] = str(c[k]).strip()
                        elif k in c and not c[k]:
                            SETTINGS[k] = ""
                    if isinstance(c.get("route"), dict):
                        SETTINGS["shots_route"] = {str(t).upper(): str(v) for t, v in c["route"].items()
                                                   if re.match(r"^[A-Za-z]{4}\d{5}$", str(t)) and v in ("tg", "dc", "both", "none")}
                save_settings()
            if b.get("test"):
                d = str(b.get("test"))
                cfg = cfg_snapshot()
                try:
                    if d == "tg":
                        tok, chat = cfg.get("shots_tg_token") or "", cfg.get("shots_tg_chat") or ""
                        req = urllib.request.Request("%s/bot%s/sendMessage" % (TG_API, tok),
                                                     data=json.dumps({"chat_id": chat, "text": "PS5 Directo: prueba ✔"}).encode(),
                                                     headers={"Content-Type": "application/json"}, method="POST")
                        urllib.request.urlopen(req, timeout=20).read()
                    else:
                        req = urllib.request.Request(cfg.get("shots_dc_hook") or "", method="POST",
                                                     data=json.dumps({"content": "PS5 Directo: prueba ✔"}).encode(),
                                                     headers={"Content-Type": "application/json", "User-Agent": "PS5Directo"})
                        urllib.request.urlopen(req, timeout=20).read()
                except urllib.error.HTTPError as e:
                    raise E.PipeError("No ha funcionado (HTTP %s). Revisa el bot, el chat o el webhook." % e.code)
                except Exception as e:
                    raise E.PipeError("No ha funcionado: %s" % e)
            if b.get("retry"):
                with SENT_LOCK:
                    try:
                        lst = json.load(open(SENT_FILE, encoding="utf-8"))
                    except Exception:
                        lst = []
                it = next((x for x in lst if x["file"] == b["retry"].get("file") and x["dest"] == b["retry"].get("dest")), None)
                if it and os.path.isfile(it["file"]):
                    def again(it=it):
                        e2 = dict(it, t=time.time(), ok=False, err="")
                        try:
                            send_capture(it["file"], it["dest"], it.get("title") or "", it.get("when") or "")
                            e2["ok"] = True
                        except Exception as e:
                            e2["err"] = str(e)[:300]
                        _sent_log(e2)
                    threading.Thread(target=again, daemon=True).start()
                    time.sleep(0.3)
            if b.get("paths"):
                items = {x["path"]: x for x in media_list()}
                dests = [b.get("dest")] if b.get("dest") in ("tg", "dc") else ["tg"]

                def manual(paths=list(b["paths"])[:50], dests=dests):
                    for rp in paths:
                        it = items.get(rp)
                        if not it:
                            continue
                        local, _ = media_cached(rp)
                        for d in dests:
                            e2 = {"t": time.time(), "file": local, "dest": d, "title": it.get("app") or it.get("tid") or "",
                                  "when": it.get("when") or "", "ok": False, "err": ""}
                            try:
                                send_capture(local, d, e2["title"], e2["when"])
                                e2["ok"] = True
                            except Exception as e:
                                e2["err"] = str(e)[:300]
                            _sent_log(e2)
                threading.Thread(target=manual, daemon=True).start()
            cfg = cfg_snapshot()
            try:
                hist = json.load(open(SENT_FILE, encoding="utf-8"))
            except Exception:
                hist = []
            return {"ok": True, "send": cfg.get("shots_send") or "", "tg_chat": cfg.get("shots_tg_chat") or "",
                    "tg_token": bool(cfg.get("shots_tg_token")), "dc_hook": bool(cfg.get("shots_dc_hook")),
                    "route": cfg.get("shots_route") or {},
                    "history": [dict(x, name=os.path.basename(x["file"])) for x in reversed(hist[-40:])]}
        if p == "/api/shots/auto":
            if "on" in b:
                with LOCK:
                    SETTINGS["shots_auto"] = bool(b.get("on"))
                save_settings()
                if b.get("on"):
                    threading.Thread(target=shots_sync, kwargs={"baseline": not b.get("old")}, daemon=True).start()
            if b.get("now"):
                threading.Thread(target=shots_sync, kwargs={"all_old": bool(b.get("old"))}, daemon=True).start()
                time.sleep(0.2)
            with LOCK:
                on = bool(SETTINGS.get("shots_auto"))
            return dict(ok=True, on=on, dir=MEDIA_CONV_DIR(), **SHOTS)
        if p == "/api/lan/hits":
            with LOCK:
                devs = sorted(LAN_DEVICES.values(), key=lambda d: d["last"], reverse=True)
                return {"ok": True, "hits": list(LAN_HITS[-12:]),
                        "devices": [dict(d, ago=int(time.time() - d["last"])) for d in devs[:12]]}
        if p == "/api/lan/firewall":
            with LOCK:
                port = int(SETTINGS.get("lan_port") or 8780)
            if b.get("allow"):
                F.firewall_allow(port)
            return dict(ok=True, **F.firewall_status(port))
        if p == "/api/pcspace":
            import shutil
            path = pc_temp_root(cfg_snapshot())
            probe = path
            while probe and not os.path.isdir(probe):
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
            try:
                free = shutil.disk_usage(probe or path).free
            except OSError:
                free = None
            return {"ok": True, "path": path, "free": free}
        if p == "/api/forge/status":
            exe = F.find_forge(cfg_snapshot().get("forge_path"), DATA_DIR)
            return {"ok": True, "path": exe or "", "found": bool(exe)}
        if p == "/api/forge/download":
            exe, tag = F.download_forge(DATA_DIR, log=print)
            return {"ok": True, "path": exe, "tag": tag}
        if p == "/api/archive/list":
            path = str(b.get("path") or "").strip().strip('"')
            if not os.path.isfile(path):
                raise E.PipeError("No encuentro ese archivo en tu PC")
            exe = E.find_7z(cfg_snapshot().get("sevenzip_path"))
            if not exe:
                raise E.PipeError("Para ver qué hay dentro hace falta 7-Zip (7-zip.org)")
            pw = str(b.get("password") or "") or None
            try:
                entries, _ = E.list_7z(exe, path, pw)
            except E.PipeError as e:
                if E._pw_error(str(e)) and not pw:
                    return {"ok": True, "need_password": True, "entries": []}
                raise
            if E.needs_password(entries) and not pw:
                return {"ok": True, "need_password": True, "entries": [x for x in entries][:5000]}
            return {"ok": True, "entries": entries[:20000]}
        if p == "/api/calibrate":
            url = str(b.get("url") or "").strip()
            if not re.match(r"^https?://", url):
                raise E.PipeError("Hace falta un enlace de descarga (http o https) para medir")
            with LOCK:
                if CAL["running"]:
                    return {"ok": True}
                CAL["running"] = True
            threading.Thread(target=calibrate, args=(url,), daemon=True).start()
            return {"ok": True}
        if p == "/api/calibrate/state":
            with LOCK:
                return dict(ok=True, **CAL)
        if p == "/api/pcinfo":
            ip = (h.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) or ip.startswith("127."):
                ip = (_local_ipv4s() or [""])[0]
            mac = F.mac_for_ip(ip) if ip else ""
            return {"ok": True, "ip": ip, "mac": mac, "bcast": ip.rsplit(".", 1)[0] + ".255" if ip else ""}
        if p == "/api/rec/list":
            return dict(ok=True, **rec_list())
        if p == "/api/md/tr":
            to = b.get("to") if b.get("to") in ("es", "en") else "es"
            return dict(ok=True, items=[_md_translate(str(x or "")[:8000], to) for x in (b.get("texts") or [])[:8]])
        if p == "/api/gh/notes":
            return dict(ok=True, **gh_notes(str(b.get("repo") or ""), str(b.get("have") or ""),
                                            str(b.get("to") or "") if b.get("to") in ("es", "en") else ""))
        if p == "/api/rp/state":
            return dict(ok=True, **rp_state())
        if p == "/api/rp/play":
            if b.get("nick") is not None:
                with LOCK:
                    SETTINGS["rp_nick"] = str(b.get("nick") or "").strip()[:80]
                save_settings()
            return dict(ok=True, **rp_play(bool(b.get("open")), bool(b.get("full")), bool(b.get("ds"))))
        if p == "/api/rp/path":
            pth = pick_paths("files", "Dónde está chiaki-ng (chiaki.exe, la .app o la AppImage)")
            if pth:
                f = pth[0]
                if f.endswith(".app") and os.path.isdir(f):
                    import glob
                    f = (glob.glob(os.path.join(f, "Contents", "MacOS", "*")) or [f])[0]
                with LOCK:
                    SETTINGS["chiaki_path"] = f
                save_settings()
            return dict(ok=True, **rp_state())
        if p == "/api/tr":
            return {"ok": True, "out": tr_texts([str(x) for x in (b.get("texts") or [])][:80], str(b.get("to") or "es"))}
        if p == "/api/recomp/install":
            return dict(ok=True, **recomp_install(str(b.get("id") or "")))
        if p == "/api/patches/index":
            return {"ok": True, "idx": patches_index()}
        if p == "/api/patches/sync":
            return dict(ok=True, **patches_sync())
        if p == "/api/os/check":
            return dict(ok=True, **os_check())
        if p == "/api/os/image":
            return dict(ok=True, **os_image(str(b.get("id") or "")))
        if p == "/api/rec/source":
            return dict(ok=True, **rec_source(str(b.get("id") or ""), str(b.get("repo") or "")))
        if p == "/api/rec/kinds":
            # Title ID -> categoria de «Descubrir», sin red: para el filtro de tipo de la biblioteca
            kinds, art = {}, {}
            for c in F.CATALOG:
                if c["kind"] == "app":
                    ic = (F.CATALOG_ART.get(c["id"]) or {}).get("icon") or ""
                    for t in [c["tid"]] + list(c.get("also") or []):
                        kinds[t.upper()] = c.get("group") or "tool"
                        if ic:
                            art[t.upper()] = ic
            return {"ok": True, "kinds": kinds, "art": art}
        if p == "/api/rec/mine/add":
            return dict(ok=True, **mine_add(str(b.get("repo") or "")))
        if p == "/api/rec/mine/del":
            return dict(ok=True, **mine_del(str(b.get("id") or "")))
        if p == "/api/rec/install":
            return dict(ok=True, **rec_install(str(b.get("id") or ""), bool(b.get("update"))))
        if p == "/api/col/reveal":
            path = str(b.get("path") or "")
            with LOCK:
                known = {x.get("path") for x in (COL.get("items") or [])}
            if path not in known or not os.path.exists(path):
                raise E.PipeError("Ya no está en tu PC (¿lo has movido o borrado?)")
            F.reveal_path(path)
            return {"ok": True}
        if p == "/api/extra_payload":
            return dict(ok=True, **extra_payload(str(b.get("id") or ""), "send" if b.get("send") else "status"))
        if p == "/api/forge/info":
            if b.get("check"):
                forge_news(force=True)
            return forge_info()
        if p == "/api/forge/update":
            with LOCK:
                if FORGE_UPD["busy"]:
                    return {"ok": True, "busy": True}
                FORGE_UPD["busy"] = True
            threading.Thread(target=forge_update_run, daemon=True).start()
            return {"ok": True}
    except E.PipeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": str(e)}
    return None


SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata", "appdata",
             "$recycle.bin", "system volume information", "node_modules", ".git",
             "msocache", "recovery", "$windows.~bt", "$windows.~ws", "perflogs"}


def _search_roots():
    home = os.path.expanduser("~")
    roots = []
    for sub in ("Downloads", "Descargas", "Desktop", "Escritorio", "Documents",
                "Documentos", "Videos", "Vídeos", ""):
        d = os.path.join(home, sub) if sub else home
        if os.path.isdir(d) and d not in roots:
            roots.append(d)
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            d = letter + ":\\"
            if os.path.isdir(d):
                roots.append(d)
    else:
        roots.append("/")
    return roots


def locate_files(files, budget=12.0, max_depth=6):
    """Busca en el PC archivos por nombre y tamano (los que se sueltan en el navegador)."""
    want = {}
    for f in files:
        try:
            want.setdefault(str(f["name"]).lower(), []).append(int(f["size"]))
        except Exception:
            pass
    found = {}
    t0 = time.time()
    seen = set()
    for root in _search_roots():
        stack = [(root, 0)]
        while stack and len(found) < len(want):
            if time.time() - t0 > budget:
                return found
            path, depth = stack.pop()
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                with os.scandir(path) as it:
                    entries = list(it)
            except OSError:
                continue
            if True:
                for e in entries:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            n = e.name.lower()
                            if depth < max_depth and n not in SKIP_DIRS and not n.startswith("."):
                                stack.append((e.path, depth + 1))
                        elif e.name.lower() in want and e.name not in found:
                            if e.stat().st_size in want[e.name.lower()]:
                                found[e.name] = e.path
                    except OSError:
                        pass
        if len(found) >= len(want):
            break
    return found


PRUEBA_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>
<title>PS5 Directo - prueba</title>
<body style='font:18px system-ui,sans-serif;background:#0e1116;color:#e6edf3;padding:24px;margin:0'>
<h2 style='color:#4ade80;margin-top:0'>&#10004; El móvil llega a PS5 Directo</h2>
<p>La red y el firewall están bien.</p>
<p id=r style='color:#8b97a5'>Probando a descargar la página completa…</p>
<p style='color:#8b97a5;font-size:15px'>Your phone can reach PS5 Directo.</p>
<script>
var t=Date.now(),r=document.getElementById('r');
fetch('/?prueba='+t,{cache:'no-store'}).then(function(x){return x.text()}).then(function(s){
 var ok=s.indexOf('</html>')>0;
 r.innerHTML=ok?'&#10004; Página completa recibida ('+Math.round(s.length/1024)+' KB en '+((Date.now()-t)/1000).toFixed(1)+' s). Vuelve al PC y escanea el código grande.'
  :'&#10008; La página llegó cortada ('+Math.round(s.length/1024)+' KB). Mira en el PC: Abrir en el móvil → Prueba de conexión.';
 r.style.color=ok?'#4ade80':'#f87171';
}).catch(function(e){r.innerHTML='&#10008; No se pudo descargar la página completa: '+e;r.style.color='#f87171'});
setTimeout(function(){if(r.style.color!=='rgb(74, 222, 128)'&&r.textContent.indexOf('Probando')==0){r.innerHTML='&#9888; Lleva más de 15 s descargando la página: algo la está frenando o cortando entre el PC y el móvil.';r.style.color='#fbbf24'}},15000);
</script>"""


class LanHandler(Handler):
    lan = True
    timeout = 60         # una conexion del movil que se queda colgada no bloquea nada


_GZ = {}


def gzip_cached(body):
    """index.html pesa ~270 KB: comprimido son ~60 KB (el movil por Wi-Fi lo nota)."""
    import gzip
    import hashlib
    k = hashlib.sha1(body).digest()
    if k not in _GZ:
        if len(_GZ) > 8:
            _GZ.clear()
        _GZ[k] = gzip.compress(body, 6)
    return _GZ[k]


def _ua_name(ua):
    u = ua or ""
    if "Shortcuts" in u or "ShortcutRunner" in u or "WorkflowKit" in u:
        return "iPhone · Atajos"
    dev = ("iPhone" if "iPhone" in u else "iPad" if "iPad" in u else "Android" if "Android" in u else
           "Windows" if "Windows" in u else "Mac" if "Macintosh" in u else "Linux" if "Linux" in u else "Dispositivo")
    app = "app PS5 Directo" if "PS5Directo" in u else ("Chrome" if "Chrome/" in u and "Edg" not in u else "Edge" if "Edg" in u
                                                       else "Firefox" if "Firefox" in u else "Safari" if "Safari" in u else "")
    return dev + (" · " + app if app else "")


def lan_hit(h, code, raw, sent, err=""):
    path = urllib.parse.urlparse(h.path).path
    ip = h.client_address[0]
    if code < 400:
        with LOCK:
            d = LAN_DEVICES.setdefault(ip, {"ip": ip, "first": time.time()})
            d.update(last=time.time(), name=_ua_name(h.headers.get("User-Agent") or ""), n=d.get("n", 0) + 1)
    if path.startswith("/api/") and path not in ("/api/state",) and not err and code < 400:
        return                          # solo lo que sirve para diagnosticar
    with LOCK:
        if path == "/api/state" and LAN_HITS and LAN_HITS[-1]["path"] == path and not err:
            LAN_HITS[-1].update(t=time.strftime("%H:%M:%S"), n=LAN_HITS[-1].get("n", 1) + 1, code=code)
            return
        LAN_HITS.append({"t": time.strftime("%H:%M:%S"), "ip": h.client_address[0], "path": path, "code": code,
                         "bytes": raw, "sent": sent, "ms": int((time.time() - getattr(h, "_t0", time.time())) * 1000),
                         "err": err, "ua": (h.headers.get("User-Agent") or "")[:160]})
        del LAN_HITS[:-30]


class Server(ThreadingHTTPServer):
    """En Windows, SO_REUSEADDR deja que DOS programas escuchen en el mismo puerto y las
    peticiones se reparten al azar entre los dos ("Failed to fetch", la cola que aparece y
    desaparece). Aqui el puerto es exclusivo."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


LOCK_PORT = 8776
_instance_lock = []


def take_instance_lock(wait=0.0):
    """Solo un PS5 Directo a la vez: se queda con un puerto fijo mientras funciona.
    Devuelve False si otro ya lo tiene (tras esperar hasta `wait` segundos)."""
    end = time.time() + wait
    while True:
        s = socket.socket()
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("127.0.0.1", LOCK_PORT))
            _instance_lock.append(s)
            return True
        except OSError:
            s.close()
            if time.time() >= end:
                return False
            time.sleep(0.25)


def pick_port():
    for port in (8777, 8778, 8779, 0):
        s = socket.socket()
        try:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("127.0.0.1", port))
            p = s.getsockname()[1]
            s.close()
            return p
        except OSError:
            s.close()
    return 0


SESSION_FILE = os.path.join(DATA_DIR, "session.json")


def dropped_paths():
    """Rutas de archivos arrastrados sobre el .bat o enviados con 'Enviar a'."""
    out = []
    for a in sys.argv[1:]:
        if a.startswith("--"):
            continue
        a = a.strip().strip('"')
        if os.path.isfile(a) or os.path.isdir(a):
            out.append(os.path.abspath(a))
    return out


def running_session(full=False):
    """Devuelve (url_base, token) si ya hay un PS5 Directo funcionando
    (con full=True, (url_base, token, estado))."""
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            ses = json.load(f)
        base = "http://127.0.0.1:%d" % int(ses["port"])
        req = urllib.request.Request(base + "/api/state", headers={"X-Token": ses["token"]})
        with urllib.request.urlopen(req, timeout=3) as r:
            st = json.load(r)
        return (base, ses["token"], st) if full else (base, ses["token"])
    except Exception:
        return None


def replace_other_version(ses):
    """Si lo que ya esta abierto es OTRA version (o la de otra carpeta, por ejemplo una copia
    vieja del escritorio) y no tiene nada en marcha, la cierra para abrir esta.
    Devuelve True si se cerro."""
    base, token, st = ses
    if st.get("version") == VERSION and st.get("home", HERE) == HERE:
        return False
    for j in st.get("jobs") or []:
        if j.get("status") not in ("hecho", "error", "cancelado", "pausado"):
            return False                      # esta trabajando: no se le corta
    try:
        req = urllib.request.Request(base + "/api/quit", data=b"{}",
                                     headers={"X-Token": token, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass
    for _ in range(40):
        time.sleep(0.25)
        if not running_session():
            print("Cerrada la version %s que seguia abierta" % st.get("version", "antigua"))
            return True
    return False


def forward_to_running(ses, paths):
    base, token = ses
    try:
        req = urllib.request.Request(base + "/api/add",
                                     data=json.dumps({"lines": "\n".join(paths)}).encode(),
                                     headers={"X-Token": token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r).get("ok", False)
    except Exception:
        return False


def _find_app_browser():
    """Edge o Chrome (o Chromium/Brave), para abrir la app en su propia ventana sin pestañas."""
    import shutil
    cands = []
    if os.name == "nt":
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if root:
                cands.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
                cands.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
    elif sys.platform == "darwin":
        for base in ("/Applications", os.path.expanduser("~/Applications")):
            for app, exe in (("Google Chrome", "Google Chrome"), ("Microsoft Edge", "Microsoft Edge"),
                             ("Brave Browser", "Brave Browser"), ("Chromium", "Chromium")):
                cands.append(os.path.join(base, app + ".app", "Contents", "MacOS", exe))
    else:
        for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                  "microsoft-edge-stable", "brave-browser", "brave"):
            w = shutil.which(n)
            if w:
                cands.append(w)
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def ext_browsers():
    """Navegadores instalados donde se puede cargar la extension: [{id, name, exe, page}]."""
    import shutil
    out, seen = [], set()
    defs = [("chrome", "Chrome", "chrome://extensions"), ("edge", "Edge", "edge://extensions"),
            ("brave", "Brave", "brave://extensions"), ("chromium", "Chromium", "chrome://extensions")]
    for bid, name, page in defs:
        cands = []
        if os.name == "nt":
            sub = {"chrome": ("Google", "Chrome", "Application", "chrome.exe"),
                   "edge": ("Microsoft", "Edge", "Application", "msedge.exe"),
                   "brave": ("BraveSoftware", "Brave-Browser", "Application", "brave.exe")}.get(bid)
            for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                if sub and os.environ.get(env):
                    cands.append(os.path.join(os.environ[env], *sub))
        elif sys.platform == "darwin":
            app = {"chrome": "Google Chrome", "edge": "Microsoft Edge", "brave": "Brave Browser", "chromium": "Chromium"}[bid]
            for base in ("/Applications", os.path.expanduser("~/Applications")):
                cands.append(os.path.join(base, app + ".app", "Contents", "MacOS", app))
        else:
            names = {"chrome": ("google-chrome", "google-chrome-stable"), "edge": ("microsoft-edge", "microsoft-edge-stable"),
                     "brave": ("brave-browser", "brave"), "chromium": ("chromium", "chromium-browser")}[bid]
            cands += [shutil.which(n) for n in names if shutil.which(n)]
        exe = next((c for c in cands if c and os.path.isfile(c)), None)
        if exe and exe not in seen:
            seen.add(exe)
            out.append({"id": bid, "name": name, "exe": exe, "page": page})
    return out


def ext_open_page(bid):
    """Abre la pagina de extensiones de ese navegador (una pagina web no puede abrir chrome://, el programa si)."""
    import subprocess
    b = next((x for x in ext_browsers() if x["id"] == bid), None)
    if not b:
        raise E.PipeError("No encuentro ese navegador")
    subprocess.Popen([b["exe"], b["page"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **({"creationflags": 0x08000000} if os.name == "nt" else {}))
    return b["name"]


APP_USER_MODEL_ID = "PS5Directo.App"
_TITLE_RX = re.compile(r"^PS5 Directo( v[\d.]+)?$")


def _app_windows():
    """Ventanas de la interfaz (modo aplicacion de Edge/Chrome). En una pestaña normal del
    navegador el titulo lleva " - Microsoft Edge" detras, asi que no se confunden."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    found = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        try:
            if not u32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            u32.GetClassNameW(hwnd, cls, 64)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(hwnd, buf, 256)
            if _TITLE_RX.match(buf.value.strip()):
                found.append(hwnd)
        except Exception:
            pass
        return True
    u32.EnumWindows(proto(cb), 0)
    return found


def _set_window_identity(hwnd):
    """Le dice a Windows que esa ventana es "PS5 Directo" y no Edge: icono propio en la
    barra de tareas, y al anclarla se ancla PS5 Directo (con su icono y su acceso)."""
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                    ("d4", ctypes.c_ubyte * 8)]

    def guid(s):
        b = uuid.UUID(s).bytes_le
        g = GUID()
        ctypes.memmove(ctypes.byref(g), b, 16)
        return g

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("p", ctypes.c_void_p), ("p2", ctypes.c_void_p)]

    fmt = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"   # PKEY_AppUserModel_*
    vbs = os.path.join(HERE, "PS5 Directo.vbs")
    wscript = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "wscript.exe")
    props = []
    if os.path.isfile(vbs):
        props += [(2, '"%s" "%s"' % (wscript, vbs)),                       # RelaunchCommand
                  (3, os.path.join(HERE, "web", "ps5directo.ico")),           # RelaunchIconResource
                  (4, "PS5 Directo")]                                         # RelaunchDisplayName
    props.append((5, APP_USER_MODEL_ID))                                      # ID (el ultimo)

    shell32 = ctypes.windll.shell32
    shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(GUID),
                                                    ctypes.POINTER(ctypes.c_void_p)]
    store = ctypes.c_void_p()
    iid = guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")   # IPropertyStore
    if shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid), ctypes.byref(store)) != 0 or not store:
        return False
    vtbl = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY),
                                   ctypes.POINTER(PROPVARIANT))(vtbl[6])
    commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[7])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
    ok = True
    try:
        for pid, text in props:
            key = PROPERTYKEY(guid(fmt), pid)
            s = ctypes.create_unicode_buffer(text)
            pv = PROPVARIANT()
            pv.vt = 31   # VT_LPWSTR (SetValue hace su propia copia)
            pv.p = ctypes.cast(s, ctypes.c_void_p).value
            ok = set_value(store, ctypes.byref(key), ctypes.byref(pv)) == 0 and ok
        commit(store)
    finally:
        release(store)
    return ok


def taskbar_watch():
    """Pone la identidad de PS5 Directo a cada ventana nueva de la interfaz."""
    done = set()
    while True:
        try:
            for h in _app_windows():
                if h not in done:
                    if _set_window_identity(h):
                        done.add(h)
        except Exception as e:
            print("Barra de tareas:", e)
            return
        time.sleep(2)


WIN_PROC = [None]   # la ventana que abrio este programa (Mac: para traerla al frente)


def ui_focus_here():
    """En el programa que ya estaba abierto: trae su ventana al frente si sigue abierta."""
    p = WIN_PROC[0]
    if p is None or p.poll() is not None:
        return False
    return F.mac_activate(p.pid)


def focus_window(ses=None):
    """Si la ventana ya esta abierta, la trae al frente (en vez de abrir otra). True si habia."""
    if os.name != "nt":
        import shutil
        import subprocess
        if sys.platform == "darwin" and ses:
            try:   # se lo pido al programa que ya estaba abierto: el sabe cual es su ventana
                req = urllib.request.Request(ses[0] + "/api/ui/focus", data=b"{}",
                                             headers={"X-Token": ses[1], "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return bool(json.load(r).get("ok"))
            except Exception:
                return False
        if sys.platform.startswith("linux"):
            for cmd in (["wmctrl", "-x", "-a", "PS5Directo"],
                        ["xdotool", "search", "--class", "PS5Directo", "windowactivate"]):
                if shutil.which(cmd[0]):
                    try:
                        if subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0:
                            return True
                    except Exception:
                        pass
        return False
    try:
        import ctypes
        u32 = ctypes.windll.user32
        wins = _app_windows()
        if not wins:
            return False
        h = wins[0]
        if u32.IsIconic(h):
            u32.ShowWindow(h, 9)   # SW_RESTORE
        u32.SetForegroundWindow(h)
        return True
    except Exception:
        return False


def open_window(url):
    """Abre la interfaz como ventana de aplicacion. Devuelve el proceso o None."""
    exe = _find_app_browser()
    if exe and "--browser" not in sys.argv:
        import subprocess
        profile = os.path.join(DATA_DIR, "ventana")
        try:
            args = [exe, "--app=" + url, "--user-data-dir=" + profile, "--window-size=1150,900",
                    "--no-first-run", "--no-default-browser-check"]
            if os.name != "nt" and sys.platform != "darwin":
                args.append("--class=PS5Directo")   # en Linux agrupa la ventana con su icono del menu
            return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=os.name != "nt")
        except Exception:
            pass
    webbrowser.open(url)
    return None


def _powershell(script, timeout=30):
    import subprocess
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                          capture_output=True, text=True, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def ensure_linux_menu():
    """Linux: entrada "PS5 Directo" en el menu de aplicaciones (con su icono). Se rehace si el
    programa cambia de carpeta. Tambien acepta archivos soltados encima (Exec ... %F)."""
    launcher = os.path.join(HERE, "ps5directo.sh")
    if not os.path.isfile(launcher):
        return
    try:
        if not os.access(launcher, os.X_OK):
            os.chmod(launcher, 0o755)
        apps = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "applications")
        fp = os.path.join(apps, "ps5directo.desktop")
        q = lambda t: '"' + t.replace("\\", "\\\\").replace('"', '\\"') + '"'
        body = ("[Desktop Entry]\nType=Application\nName=PS5 Directo\n"
                "Comment=Descarga, convierte y envía a tu PS5\nComment[en]=Download, convert and send to your PS5\n"
                "Exec=%s %%F\nIcon=%s\nTerminal=false\nCategories=Network;FileTransfer;\n"
                "StartupWMClass=PS5Directo\n" % (q(launcher), os.path.join(HERE, "web", "icon-256.png")))
        try:
            with open(fp, encoding="utf-8") as f:
                if f.read() == body:
                    return
        except OSError:
            pass
        os.makedirs(apps, exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(body)
        print("Entrada en el menu de aplicaciones:", fp)
    except Exception as e:
        print("No se pudo crear la entrada del menu:", e)


def ensure_shortcuts():
    """Acceso directo "PS5 Directo" en el escritorio y en "Enviar a", con el icono del programa.
    Se crea la primera vez; despues solo se corrige si apunta a otra carpeta (una copia vieja)."""
    if sys.platform.startswith("linux"):
        return ensure_linux_menu()
    if os.name != "nt":
        return
    try:
        vbs = os.path.join(HERE, "PS5 Directo.vbs")
        ico = os.path.join(HERE, "web", "ps5directo.ico")
        if not os.path.isfile(vbs):
            return
        with LOCK:
            first = not SETTINGS.get("shortcut_done")
        q = lambda t: "'" + t.replace("'", "''") + "'"
        script = (
            "$w=New-Object -ComObject WScript.Shell; $first=$%s;"
            "foreach($f in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('SendTo'))){"
            " $p=Join-Path $f 'PS5 Directo.lnk'; $ex=Test-Path -LiteralPath $p;"
            " $l=$w.CreateShortcut($p);"
            " $want='\"'+%s+'\"';"
            " if($ex -and $l.Arguments -eq $want -and $l.IconLocation -like ('*'+%s+'*')){continue}"
            " if(-not $ex -and -not $first){continue}"
            " if($ex -and $l.Arguments -notlike '*PS5 Directo.vbs*'){continue}"
            " $l.TargetPath=(Join-Path $env:WINDIR 'System32\\wscript.exe'); $l.Arguments=$want;"
            " $l.WorkingDirectory=%s; $l.IconLocation=%s+',0'; $l.Description='PS5 Directo'; $l.Save();"
            " 'ok '+$p }"
        ) % ("true" if first else "false", q(vbs), q(ico), q(HERE), q(ico))
        r = _powershell(script)
        if r.stdout.strip():
            print("Acceso directo:", r.stdout.strip().replace("\n", " | "))
        if r.returncode == 0 and first:
            with LOCK:
                SETTINGS["shortcut_done"] = True
            save_settings()
    except Exception as e:
        print("No se pudo crear el acceso directo:", e)


def refresh_window_icon():
    """La ventana guarda el icono en su cache: tras actualizar se borra para que la barra
    de tareas enseñe el icono actual y no el de una version vieja."""
    with LOCK:
        if SETTINGS.get("icon_ver") == VERSION:
            return
        SETTINGS["icon_ver"] = VERSION
    save_settings()
    prof = os.path.join(DATA_DIR, "ventana")
    for sub in ("Default", ""):
        for name in ("Favicons", "Favicons-journal"):
            try:
                os.remove(os.path.join(prof, sub, name))
            except OSError:
                pass
    import shutil
    for d in ("Web Applications", os.path.join("Default", "Web Applications")):
        shutil.rmtree(os.path.join(prof, d), ignore_errors=True)


def busy():
    with LOCK:
        return any(j["status"] not in ("hecho", "error", "cancelado", "pausado") for j in JOBS)


UI_SEEN = {"t": 0.0, "bye": 0.0}


def watch_ui(grace=30, silent=6 * 3600):
    """Se cierra el programa cuando cierras la ventana (la ventana avisa al cerrarse) y no hay
    nada en marcha. Si la ventana solo esta minimizada, el navegador la "duerme" y deja de
    preguntar: eso NO cuenta como cerrada (antes el programa se apagaba y al volver salia
    "Failed to fetch"). Por si el navegador se cuelga sin avisar, tras `silent` segundos
    sin noticias tambien se cierra."""
    while True:
        time.sleep(10)
        seen, bye = UI_SEEN["t"], UI_SEEN["bye"]
        if busy() or not seen or LAN["srv"]:
            continue
        now = time.time()
        if bye and bye >= seen - 1 and now - bye > grace:
            os._exit(0)
        if now - seen > silent:
            os._exit(0)


def main():
    if sys.stdout is None:  # abierto sin consola (pythonw): registro a archivo
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            sys.stdout = sys.stderr = open(os.path.join(DATA_DIR, "ps5directo.log"), "a",
                                           encoding="utf-8", buffering=1)
        except Exception:
            pass
    paths = dropped_paths()
    if "--after-update" in sys.argv:
        for _ in range(60):
            if not running_session():
                break
            time.sleep(0.5)
    ses = running_session(full=True)
    if ses and replace_other_version(ses):
        ses = None
    if not ses and not take_instance_lock(wait=30 if "--after-update" in sys.argv else 4):
        # otro PS5 Directo esta arrancando ahora mismo (doble clic, por ejemplo): uso ese
        for _ in range(40):
            ses = running_session(full=True)
            if ses:
                break
            time.sleep(0.25)
        if not ses:
            print("Ya hay otro PS5 Directo abierto")
            sys.exit(3)
    if ses:
        ses = ses[:2]
        if paths:
            forward_to_running(ses, paths)
            print("Anadido a la cola de PS5 Directo (ya estaba abierto)")
        elif "--browser" in sys.argv or not focus_window(ses):
            open_window(ses[0] + "/#t=" + ses[1])   # ya funcionaba: solo enseno la ventana
        sys.exit(3)
    load_state()
    F.LOW_PRIO[0] = bool(SETTINGS.get("low_prio"))
    if paths:
        job = new_job(paths, "", SETTINGS["dest"])
        with WAKE:
            JOBS.append(job)
        save_jobs()
    for target in (transfer_worker, extract_worker, monitor, speed_watch):
        threading.Thread(target=target, daemon=True).start()
    port = pick_port()
    srv = Server(("127.0.0.1", port), Handler)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump({"port": srv.server_address[1], "token": TOKEN}, f)
    except Exception:
        pass
    threading.Thread(target=ensure_shortcuts, daemon=True).start()
    threading.Thread(target=console_auto, daemon=True).start()   # payloads que falten, con Payload Manager
    threading.Thread(target=console_watch, daemon=True).start()
    threading.Thread(target=clip_watch, daemon=True).start()
    threading.Thread(target=sync_extension, daemon=True).start()  # y si luego se abre Payload Manager, igual
    refresh_window_icon()
    if SETTINGS.get("lan_access"):
        try:
            lan_start()
        except Exception as e:
            print("Acceso desde el movil:", e)
    url = "http://127.0.0.1:%d/#t=%s" % (srv.server_address[1], TOKEN)
    print("PS5 Directo %s" % VERSION)
    print("Abre en el navegador: %s" % url)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: WIN_PROC.__setitem__(0, open_window(url))).start()
        threading.Thread(target=watch_ui, daemon=True).start()
    if os.name == "nt":
        threading.Thread(target=taskbar_watch, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def phone_start():
    """Arranque dentro de la app de Android (modo «Usar sin PC»): el mismo PS5 Directo, sin ventana ni accesos
    directos ni nada del escritorio. Devuelve la direccion (con su codigo) que abre la app."""
    load_state()
    for target in (transfer_worker, extract_worker, monitor, speed_watch, console_auto, console_watch):
        threading.Thread(target=target, daemon=True).start()
    srv = Server(("127.0.0.1", pick_port()), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("PS5 Directo %s (Android, sin PC)" % VERSION)
    return "http://127.0.0.1:%d/#t=%s" % (srv.server_address[1], TOKEN)


if __name__ == "__main__":
    main()
