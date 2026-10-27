"""PS5 Directo: transfiere archivos a la consola, los descomprime y los deja en su sitio.

Arranque:  python app.py   (o doble clic en "Iniciar PS5 Directo.bat")
"""

import ftplib
import json
import re
import os
import posixpath
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine as E  # noqa: E402
import features as F  # noqa: E402

VERSION = "1.20.3"
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
    "forge_auto_update": True,   # bajar solo las versiones nuevas de PS5 Dump Forge (comprobando que valen)
    "tour_done": False,       # la guia de bienvenida ya se vio
    "seen_version": "",       # ultima version cuyas novedades ya se ensenaron
    "shortcut_done": False,   # ya se creo el acceso directo del escritorio una vez
    "icon_ver": "",           # version cuyo icono ya se dejo en la cache de la ventana
    # 1.20
    "elf_port": 9021,         # cargador de payloads de la consola (etaHEN / elfldr)
    "console_fw": "",         # firmware de tu PS5 (p. ej. 13.60) para avisar de lo que no arrancara
    "dup_check": True,        # preguntar antes de enviar algo que ya esta en la consola
    "verify_sums": True,      # comprobar .sfv/.md5/.sha1 si vienen con las partes
    "pc_format": "folder",    # en el PC: folder, ffpkg, ffpfsc, lz4, lz4ffpkg (con PS5 Dump Forge)
    "forge_path": "",         # ruta de ps5-dump-forge.exe (vacio = descargarlo solo)
    "keep_awake": True,       # que el PC no se suspenda mientras hay envios
    "welcome_seen": False,    # la ventana de bienvenida ya se ha visto
    "pc_collection": [],      # carpetas del PC o del NAS con contenido para la consola (pestana Biblioteca > Tu PC)
    "backup_dir": "",         # copias de seguridad de la consola (vacio = Documentos\\PS5 Directo\\Copias)
    "backup_auto": False,     # una copia al dia, sola, si la consola esta encendida
    "backup_keep": 10,        # cuantas copias guardar
    "backup_saves": True, "backup_config": True, "backup_appdb": True,
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


def new_job(lines, password, dest, title=None, where=None, raw=False, fmt=None, comp=None):
    return {
        "raw": bool(raw),
        "fmt": fmt if fmt in F.FORGE_FORMATS else "",
        "comp": comp if comp in COMP_LEVELS else "",
        "where": where if where in ("ps5", "pc") else "",
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


def verify_part(job, s, hasher, on_bad=None):
    """Compara la suma calculada al enviar con la del .sfv/.md5. Lanza PipeError si no coincide."""
    if hasher is None:
        return
    algo, want = job["checks"][s.name.lower()]
    if not hasher.valid or hasher.pos != s.size:
        jlog(job, "%s: sin comprobar con %s (ya estaba enviado o se continuo de un envio anterior)"
             % (s.name, algo.upper()))
        return
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


def free_space(bf):
    """Bytes libres en /data segun BFpilot, o None si no responde."""
    try:
        st = bf._req("GET", "/api/fs/stat", {"path": "/data"})
        v = st.get("availableBytes")
        return int(v) if v is not None else None
    except Exception:
        return None


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
                            on_bad=lambda remote=remote: E.Ftp(cfg).call(lambda f: f.delete(remote)))
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
        if job.get("fmt") == "plain" and os.path.isfile(os.path.join(srcs[0].path, "ampr_assets.index")):
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
        if want and srcs[0].name.lower().endswith(want) and fmt != "lz4ffpkg":
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
        verify_part(job, s, hasher, on_bad=lambda: E.Ftp(cfg).call(lambda f: f.delete(remote)))
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
                "where": "raw" if job.get("raw") else ("pc" if (job.get("info") or {}).get("mode") == "pcx" else "ps5"),
                "dest": job.get("dest") or "",
                # para "Descargar de nuevo" desde el historial
                "redo": {"lines": list(job.get("orig_lines") or job.get("lines") or []), "where": job.get("where") or "",
                         "raw": bool(job.get("raw")), "fmt": job.get("fmt") or "", "comp": job.get("comp") or "",
                         "fmt_set": bool(job.get("fmt_set"))},
            })
            _save_json(HISTORY_FILE, hist[-500:])
        if job.get("status") == "hecho":
            torrent_cleanup(job)
        title = job.get("title") or "Transferencia"
        link = {"tab": "Transfers", "job": job["id"]}
        if job.get("status") == "hecho":
            notify("ok", "Terminado: %s" % title, ", ".join(job.get("result") or [])[:300], link)
        elif job.get("status") == "error":
            notify("err", "Error: %s" % title, job.get("error") or "", link)
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
                FMOP["error"] = str(e)
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


def fm_delete(paths):
    paths = [fm_clean(x) for x in paths]
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


def fm_download(paths):
    cfg = cfg_snapshot()
    paths = [fm_clean(x) for x in paths]
    root_local = os.path.join(os.path.expanduser("~"), "Downloads", "PS5 Directo")

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
                SPACE["error"] = str(e)
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
        fp = make_report(str(b.get("id") or "") or None)
        return {"path": fp, "name": os.path.basename(fp)}
    if p == "/api/report/open":
        os.makedirs(report_dir(), exist_ok=True)
        if os.name == "nt":
            os.startfile(report_dir())
        return {"dir": report_dir()}
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
            return {"col": dict(COL), "roots": list(SETTINGS.get("pc_collection") or [])}
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
        secs = [x for x in (b.get("sections") or []) if x in ("saves", "config", "appdb", "list")] or None
        threading.Thread(target=backup_run, args=(secs,), daemon=True).start()
        return {}
    if p == "/api/bk/cancel":
        BK_CANCEL.set()
        return {}
    if p == "/api/bk/restore":
        if BK["busy"]:
            raise E.PipeError("Espera a que termine la copia en marcha")
        threading.Thread(target=backup_restore, args=(str(b.get("path") or ""), b.get("ids") or []),
                         daemon=True).start()
        return {}
    if p == "/api/bk/open":
        d = backup_dir()
        os.makedirs(d, exist_ok=True)
        if os.name == "nt":
            os.startfile(d)
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
        fm_delete(b.get("paths") or [])
        return {}
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
    if p == "/api/fm/open_local":
        path = FMOP.get("local") or os.path.join(os.path.expanduser("~"), "Downloads", "PS5 Directo")
        if os.name == "nt" and os.path.isdir(path):
            os.startfile(path)
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
                     raw=r.get("raw"), fmt=r.get("fmt"), comp=r.get("comp"))
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


def pick_paths(kind, title):
    """Abre el dialogo de Windows para elegir archivos o una carpeta del PC."""
    import subprocess
    if os.name == "nt":
        try:
            return F.pick_windows(kind, title)
        except Exception as e:
            print("Selector de PowerShell:", e)
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
               ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


# ---------------------------------------------------------------------------
# Diagnostico e informe de fallo
# ---------------------------------------------------------------------------

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


def _job_name(j):
    if j.get("title"):
        return j["title"]
    src = (j.get("sources") or [{}])[0].get("name") or ((j.get("lines") or [""])[0].replace("\\", "/").rsplit("/", 1)[-1])
    return src or "transferencia"


def report_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads", "PS5 Directo", "Informes")


def make_report(job_id=None):
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
    with zipfile.ZipFile(fp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("version_y_conexion.json", json.dumps(info, ensure_ascii=False, indent=1))
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
        z.writestr("LEEME.txt", "Informe de PS5 Directo %s (%s)\n\n%s\n\nNo lleva contraseñas. Pásaselo a quien te "
                   "ayude.\n" % (VERSION, info["date"], "\n".join(summary)))
    return fp


# ---------------------------------------------------------------------------
# Comprobar arranque: por que una aplicacion de la Biblioteca no abre (solo lectura)
# ---------------------------------------------------------------------------

LAUNCH_CODES = {
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


def launch_check(path, minutes=30):
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
        info["logs"] = _recent_logs(f, ([path] if is_dir else []) + ["/data"], since)
    E.Ftp(cfg).call(run)
    lib = (LIB.get("items") or {}).get("items") or []
    me = next((x for x in lib if x.get("path") == path), {})
    if me.get("dup"):
        add("warn", "Está repetida en %s: dos copias con el mismo Title ID pueden liar a ShadowMountPlus." % ", ".join(me["dup"]),
            "Deja solo una.")
    try:
        caps = media_list()[:4]
    except Exception:
        caps = []
    info.update(path=path, checks=checks, captures=caps, codes=LAUNCH_CODES)
    return info


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
    roots = [r for r in (SETTINGS.get("pc_collection") or []) if r]
    with LOCK:
        COL.update(busy=True, error="", current="")
    items = []
    try:
        for root in roots:
            if not os.path.isdir(root):
                items.append({"path": root, "kind": "missing", "name": root})
                continue
            for d, dirs, fs in os.walk(root):
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
        with LOCK:
            COL.update(items=items, t=time.time())
    except Exception as e:
        traceback.print_exc()
        with LOCK:
            COL["error"] = str(e)
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
    """Capturas y videos de la consola (los mas nuevos primero)."""
    cfg = cfg_snapshot()
    out = []
    files = E.Ftp(cfg).call(lambda f: _ftp_walk(f, MEDIA_ROOT, [], threading.Event()))
    for p, sz in files:
        ext = posixpath.splitext(p)[1].lower()
        if ext not in MEDIA_TYPES or "/thumbnail" in p.lower():
            continue
        out.append({"path": p, "name": posixpath.basename(p), "size": sz,
                    "video": MEDIA_TYPES[ext].startswith("video"),
                    "folder": posixpath.basename(posixpath.dirname(p))})
    out.sort(key=lambda x: x["name"], reverse=True)
    return out[:3000]


def media_cached(path):
    """Copia local (cache) de una captura o video, para verla en el navegador."""
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
    return fp, MEDIA_TYPES[ext]
FMT_LABEL = {"plain": "carpeta normal", "ffpkg": "imagen .ffpkg", "exfat": "imagen .exfat", "ffpfs": "imagen .ffpfs",
             "ffpfsc": "imagen comprimida .ffpfsc", "fpkg": "paquete .fpkg", "lz4": "carpeta LZ4",
             "lz4ffpkg": "imagen .ffpkg con LZ4"}
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
    if (cfg.get("forge_path") or "").strip() or not cfg.get("forge_auto_update", True):
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
    proc = subprocess.Popen(E._7z_cmd(exe7, "x", archive, "-o" + work, root, password=pw),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=E._NOWIN)
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
        raise E.PipeError("7-Zip no pudo descomprimirlo para convertirlo: %s" % err[-300:])
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
    if fmt == "plain" and lz4.get("packed"):
        extra.append("--lz4-unpack")
        jlog(job, "Viene empaquetado en LZ4: lo desempaqueto a archivos normales")
    if fmt == "fpkg":
        mine = (SETTINGS.get("console_fw") or "").strip()
        if mine and F.fw_ok(FPKG_MAX_FW, mine) is False:
            raise E.PipeError("Los .fpkg solo funcionan con firmware %s o inferior y tu PS5 tiene %s. Elige "
                              "«Imagen .ffpkg» (o «Sin convertir») y pulsa Reintentar." % (FPKG_MAX_FW, mine))
        jlog(job, "Ojo: los .fpkg solo se instalan en firmware %s o inferior (con kstuff, fpkg-enable y "
                  "ppr-patch)" % FPKG_MAX_FW)
    comp = job.get("comp") or ("fast" if fmt == "fpkg" else "balanced")
    if fmt in F.FORGE_COMP and comp in F.FORGE_COMP[fmt]:
        extra += F.FORGE_COMP[fmt][comp]
        jlog(job, "Compresión: %s" % {"fast": "rápida", "balanced": "equilibrada", "smallest": "mínimo tamaño",
                                       "stored": "sin compresión"}[comp])
    if fmt in ("lz4", "lz4ffpkg") and not lz4.get("imports_ampr"):
        jlog(job, "Este título no usa AMPR, así que LZ4 no sirve con él")
        if fmt == "lz4":
            msg = ("PS5 Dump Forge dice que este título no usa AMPR, así que no se puede pasar a LZ4: "
                   "se envía sin convertir, como carpeta normal.")
            with LOCK:
                job["warning"] = msg
            jlog(job, msg)
            return None
        fmt = "ffpkg"
        with LOCK:
            job["warning"] = ("PS5 Dump Forge dice que este título no usa AMPR: se envía como imagen .ffpkg "
                              "normal, sin LZ4.")
        jlog(job, "Lo convierto a imagen .ffpkg normal")
    setj(job, fmt_real=fmt)
    if fmt in ("lz4", "lz4ffpkg"):
        jlog(job, "LZ4 es experimental (ampr_emu de prueba): con algunos títulos se cuelga al guardar partida")
    check_fw_warning(job, tid, title, need_fw, info.get("firmware"), bool(info.get("backport")))
    bf = E.BFpilot(cfg)
    dest = "/data/pkg" if fmt == "fpkg" else (job.get("dest") or cfg["dest"]).rstrip("/")
    ext = F.FORGE_FORMATS[fmt][1]
    base = tid or E.safe_name(E.display_name(srcs[0].name))[:50]
    name = (base + ext) if ext else (base + "-app")
    target = dest + "/" + name
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
        free = free_space(bf) if bf.alive() else None
        est = int(total * (0.5 if fmt in ("ffpfsc", "lz4", "lz4ffpkg") else 0.9 if fmt == "fpkg" else 1.02))
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


def pc_temp_root(cfg):
    d = (cfg.get("pc_temp_dir") or "").strip().strip('"')
    return d or os.path.join(os.path.expanduser("~"), "Downloads", "PS5 Directo (temporal)")


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
    done_before = 0
    for k, s in enumerate(urls, 1):
        final = os.path.join(folder, s.name)
        created.append(final)
        local[s.name] = final
        if os.path.isfile(final) and os.path.getsize(final) == s.size:
            done_before += s.size
            setj(job, sent=done_before)
            continue
        part = final + ".part"
        off = os.path.getsize(part) if os.path.isfile(part) else 0
        if off > s.size:
            off = 0
        if off:
            jlog(job, "%s: continuando desde %s" % (s.name, human(off)))
        setj(job, phase="Bajando al PC: parte %d de %d (%s)" % (k, len(urls), s.name))
        got = off
        hasher = part_hasher(job, s)
        if hasher is not None and off:
            hasher.valid = False          # continuacion de otra vez: no se puede sumar desde el principio
        with open(part, "r+b" if off else "wb") as f:
            f.seek(off)
            f.truncate()
            it = s.stream(off, cfg, cancel, meter)
            for data in (hasher.wrap(it, off) if hasher else it):
                f.write(data)
                got += len(data)
                setj(job, sent=done_before + got)
        if got != s.size:
            raise E.PipeError("La descarga de %s se quedo corta (%s de %s). Pulsa Reintentar."
                              % (s.name, human(got), human(s.size)))
        verify_part(job, s, hasher, on_bad=lambda: os.remove(part))
        os.replace(part, final)
        done_before += s.size
        jlog(job, "En el PC: %s" % s.name)
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
        staging = "%s/extract/%s" % (WORK_ROOT, job["id"])
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
    staging = "%s/extract/%s" % (WORK_ROOT, job["id"])
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
    if not tid:
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
            setj(job, phase="Borrando la copia anterior: %s" % p)
            jlog(job, "Borro la copia que ya habia: %s" % p)
            if bf.alive():
                bf.delete(p)
            else:
                ftp_rmtree(cfg, p, threading.Event())
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
            # Siempre TITLEID-app, venga suelto o dentro de una carpeta del RAR
            tid = (E.title_id(bf.read_text(path + "/sce_sys/param.json"))
                   or E.title_id(name) or E.title_id(hint_name))
            if tid:
                name = tid + "-app"
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
        if bf.exists(target):
            target = resolve_existing(job, target, bf.exists, lambda t: bf.delete(t, cancel), E.title_id(name))
        bf.mkdir(target_dir)
        bf.rename(path, target)
        jlog(job, "Colocado: %s" % target)
        placed.append(target)
        if kind in ("game", "image"):
            job["_placed_game"] = True
    return placed


def fix_game_perms(job, bf, target, cancel=None):
    """Tras descomprimir con BFpilot: permisos 777 (si no, la PS5 no la arranca)."""
    try:
        if not bf.exists(target):
            return
        setj(job, phase="Arreglando permisos de %s" % posixpath.basename(target))
        changed, seen, notable = bf.fix_perms(target, cancel, log=lambda m: jlog(job, m))
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
    staging = "%s/extract/%s" % (WORK_ROOT, job["id"])

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
    bf.mkdir(WORK_ROOT + "/extract")
    if bf.exists(staging):
        # BFpilot solo crea esta carpeta cuando la extraccion termina bien
        jlog(job, "Ya estaba descomprimido de antes; paso a colocarlo")
        cands = []
    else:
        for e in bf.listdir(WORK_ROOT + "/extract"):
            if e["name"].startswith(job["id"] + ".bfpilot-extracting"):
                jlog(job, "Borrando restos de un intento anterior: %s" % e["name"])
                bf.delete(WORK_ROOT + "/extract/" + e["name"], cancel)

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
            if not stage.startswith(WORK_ROOT + "/extract/") or ".bfpilot-extracting" not in stage:
                return
            size = bf.du(stage) or 0
            free = free_space(bf)
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
            raise E.PipeError("Este ZIP usa cifrado AES y BFpilot todavia no sabe abrirlo (solo el cifrado "
                              "ZIP clasico). Los archivos ya estan en la PS5, en %s. Puedes bajarlos al PC, "
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
        try:
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
                                     comp=job.get("comp"))
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
            setj(job, status="error", error=msg, phase="Error")
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
                jlog(job, "Es un .fpkg: instálalo desde /data/pkg con el instalador de paquetes "
                          "(etaHEN, ItemzFlow o ps5upload)")
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
            setj(job, status="error", error=msg, phase="Error")
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
                if c and c.get("mtime") == v.get("mtime") and (c.get("facts") or {}).get("v") == 4:
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
                        if any(k.lower().startswith(("ampr_assets", "ampr_emu")) for k, w in sub):
                            facts["lz4"] = True
                        facts["v"] = 4 if facts.get("tid") else 0
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
                if c and not refresh and c.get("mtime") == x.get("mtime") and (c.get("facts") or {}).get("v") == 4:
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
                            if any(n.lower().startswith(("ampr_assets", "ampr_emu")) for n, v in listing):
                                facts["lz4"] = True
                        else:
                            facts = {"incomplete": True}
                        facts["v"] = 4 if (facts.get("tid") or facts.get("incomplete") or not facts.get("icon")) else 0
                    except ftplib.all_errors as e:
                        facts = {"error": str(e)}
                    c = {"mtime": x.get("mtime"), "facts": facts}
                    ents[path] = c
                it = dict(facts)
                it.update(path=path, name=name, kind="folder", drive=drive, size=c.get("size"),
                          cover=bool(facts.get("icon")))
                if not it.get("incomplete") and LZ4_NAME_RE.search(name):
                    it["lz4"] = True
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
                      "ext": LIB_IMAGE_RE.search(name).group(1).lower(), "size": x.get("size"),
                      "tid": tid, "title": mf.get("title") or t.get("title"),
                      "version": mf.get("version"), "need_fw": mf.get("need_fw") or t.get("need_fw"),
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
            items.append({"path": "/user/app/" + n if tidu in user_app else mp, "name": n, "kind": "installed",
                          "platform": "PS4" if ps4 else "PS5", "drive": "Interno", "tid": tidu,
                          "title": mf.get("title"), "version": mf.get("version"), "need_fw": mf.get("need_fw"),
                          "cover": bool(mf.get("icon")), "cover_path": mp if mf.get("icon") else None,
                          "size": (ents.get("/user/app/" + n) or {}).get("size")})
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
            todo = [it for it in items if (it["kind"] == "folder" or (it["kind"] == "installed" and it["path"].startswith("/user/app/")))
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
                LIB["error"] = str(e)
        finally:
            with LOCK:
                LIB["busy"] = False
    threading.Thread(target=run, daemon=True).start()


def cover_png(path=None, tid=None):
    """Bytes de la portada (icon0.png) de una aplicacion de la consola, guardada en cache."""
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
    fp = os.path.join(COVER_DIR, key + ".png")
    if os.path.isfile(fp):
        with open(fp, "rb") as fh:
            return fh.read()
    icon = path + ("/icon0.png" if path.startswith("/user/appmeta/") else "/sce_sys/icon0.png")
    data = E.Ftp(cfg_snapshot()).call(lambda f: F.ftp_read(f, icon, 0, 8 * 1024 * 1024))
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
                  "/data/ps5_autoloader", "/data/kstuff"]
BK_APPDB = ["/system_data/priv/mms/app.db", "/system_data/priv/mms/appinfo.db", "/system_data/priv/mms/addcont.db"]
BK_SAVE_DIRS = ("savedata_prospero", "savedata")
BK_MAX_FILE = 200 * 1024 * 1024       # en la configuracion no se copian archivos enormes


def backup_dir(cfg=None):
    d = ((cfg or cfg_snapshot()).get("backup_dir") or "").strip().strip('"')
    return d or os.path.join(os.path.expanduser("~"), "Documents", "PS5 Directo", "Copias")


def _ftp_walk(f, root, out, cancel, limit=None):
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
            _ftp_walk(f, p, out, cancel, limit)
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


def backup_run(sections=None, label="copia"):
    """Hace la copia en un .zip con fecha. sections: saves, config, appdb, list."""
    import zipfile
    cfg = cfg_snapshot()
    if sections is None:
        sections = [k for k in ("saves", "config", "appdb") if cfg.get("backup_" + k, True)] + ["list"]
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
                for r in _save_roots(f):
                    todo += [("saves", p, s) for p, s in _ftp_walk(f, r, [], BK_CANCEL)]
            if "config" in sections:
                for r in BK_CONFIG_DIRS:
                    todo += [("config", p, s) for p, s in _ftp_walk(f, r, [], BK_CANCEL, BK_MAX_FILE)]
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
                        manifest["files"].append({"section": sec, "path": p, "size": sz})
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
            BK["error"] = str(e)
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
                    titles = lib_load().get("titles") or {}
                    info["names"] = {i: (titles.get(i.upper()) or {}).get("title") for i in ids}
                    info["files"] = len(m.get("files") or [])
            except Exception as e:
                info["error"] = str(e)
            out.append(info)
    return out


def backup_restore(path, ids):
    """Vuelve a poner en la consola las partidas elegidas de una copia (antes guarda las de ahora)."""
    import zipfile
    cfg = cfg_snapshot()
    path = os.path.abspath(path)
    if not path.startswith(os.path.abspath(backup_dir(cfg))) or not os.path.isfile(path):
        raise E.PipeError("Esa copia no está en la carpeta de copias")
    ids = set(ids or [])
    with LOCK:
        BK.update(busy=True, what="restore", done=0, total=0, files=0, current="Guardando antes las partidas de ahora…",
                  error="")
    BK_CANCEL.clear()
    try:
        backup_run(["saves"], "antes-de-restaurar")
        with LOCK:
            BK.update(busy=True, what="restore", current="Restaurando…", error="")
        with zipfile.ZipFile(path) as z:
            m = json.loads(z.read("manifest.json"))
            files = [it for it in m.get("files") or [] if it.get("section") == "saves"
                     and (not ids or (len(it["path"].split("/")) > 5 and it["path"].split("/")[5] in ids))]
            with LOCK:
                BK.update(total=sum(it.get("size") or 0 for it in files), files=len(files))
            f = E.Ftp(cfg).connect()
            done = 0
            try:
                for it in files:
                    if BK_CANCEL.is_set():
                        raise E.Cancelled()
                    p = it["path"]
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
            BK["error"] = str(e)
        notify("err", "No se pudieron restaurar las partidas", str(e))
    finally:
        with LOCK:
            BK.update(busy=False, current="", t=time.time())


def monitor():
    """Cada pocos segundos: que el PC no se duerma con trabajo y la accion al acabar la cola."""
    awake = None
    last_auto = 0.0
    while True:
        time.sleep(5)
        try:
            # copia de seguridad diaria (si esta activada y la consola responde)
            if (SETTINGS.get("backup_auto") and not BK["busy"] and not busy()
                    and time.time() - float(SETTINGS.get("backup_last") or 0) > 24 * 3600
                    and time.time() - last_auto > 1800):
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
    for p in F.PAYLOADS:
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


def payload_send(pid=None, path=None):
    cfg = cfg_snapshot()
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
LAN_DENY = {"/api/bk/restore", "/api/bk/open", "/api/report/open", "/api/col/set", "/api/lan/firewall", "/api/lan/hits", "/api/update/zip", "/api/update/github", "/api/quit", "/api/pick", "/api/open_ext_folder",
            "/api/fm/open_local", "/api/lan", "/api/forge/download", "/api/payload/file", "/api/fixperms"}
LAN_LOCKED = {"sevenzip_path", "forge_path", "pc_temp_dir", "update_repo", "lan_access", "lan_port", "lan_token"}


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
            out["test_svg"] = F.qr_svg(out["test_url"], scale=3)
        else:
            out["error"] = "No sé la IP de este PC en la red local"
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

UPDATE_EXTS = (".py", ".html", ".txt", ".bat", ".vbs", ".ico", ".js", ".json", ".png", ".css", ".md", ".svg")


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


def whats_new(lang):
    """Novedades que ensenar tras actualizar (vacio si no toca)."""
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
    return {"show": bool(entries), "version": VERSION, "entries": entries[:6]}


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
    return new_ver


def restart_soon():
    """Arranca la version nueva y cierra esta (la nueva espera a que esta se cierre)."""
    import subprocess
    args = [sys.executable, os.path.join(HERE, "app.py"), "--after-update"]
    kw = {"cwd": HERE}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
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
    req = urllib.request.Request("https://api.github.com/repos/%s/releases/latest" % repo,
                                 headers={"User-Agent": "PS5Directo/" + VERSION,
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.load(r)
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
            man = {"name": "PS5 Directo", "short_name": "PS5 Directo", "display": "standalone",
                   "background_color": "#0f1115", "theme_color": "#0f1115", "start_url": "/",
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
        if self.path == "/api/ext/ping":
            return self._send(200, {"ok": True, "app": "PS5 Directo", "version": VERSION})
        if self.path.startswith("/api/media/file?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not token_ok((q.get("t") or [""])[0]):
                return self._send(403, {"ok": False})
            try:
                fp, ctype = media_cached((q.get("p") or [""])[0])
            except Exception as e:
                return self._send(404, {"ok": False, "error": str(e)})
            size = os.path.getsize(fp)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "private, max-age=86400")
            if (q.get("dl") or [""])[0]:
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % os.path.basename(fp))
            self.end_headers()
            with open(fp, "rb") as fh:
                shutil_copy = __import__("shutil").copyfileobj
                shutil_copy(fh, self.wfile, 1024 * 1024)
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
        if self.path.startswith("/api/cover?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not token_ok((q.get("t") or [""])[0]):
                return self._send(403, {"ok": False})
            try:
                data = cover_png((q.get("p") or [None])[0], (q.get("tid") or [None])[0])
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
                      "lan": bool(LAN["srv"]), "remote": self.lan, "os": os.name}
            st["settings"].pop("ftp_pass", None)
            if self.lan:
                st["settings"].pop("lan_token", None)
            return self._send(200, st)
        self._send(404, {"ok": False})

    def _from_extension(self):
        o = self.headers.get("Origin") or ""
        return o.startswith("chrome-extension://") or o.startswith("moz-extension://") \
            or o.startswith("extension://")

    def do_POST(self):
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
                ls = [x.strip() for x in str(b.get("lines", "")).splitlines() if x.strip()]
                if any(not re.match(r"^((https?|ftp)://|magnet:\?)", x, re.I) for x in ls):
                    return self._send(400, {"ok": False, "error": "Desde el móvil solo se pueden añadir enlaces "
                                                                  "(http, https o ftp), no archivos del PC"})
        extra = extra_api(self, p, b)
        if extra is not None:
            return self._send(200, extra)
        if p == "/api/open_ext_folder":
            folder = os.path.join(HERE, "extension")
            try:
                if os.name == "nt":
                    os.startfile(folder)
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
            except Exception as e:
                return self._send(200, {"ok": False, "error": str(e)})
            if _ver_tuple(ver) > _ver_tuple(VERSION):
                notify("info", "Hay una versión nueva: %s" % ver, "Pulsa Actualizar arriba para instalarla.",
                       {"action": "update"}, key="update:" + ver)
            return self._send(200, {"ok": True, "current": VERSION, "latest": ver, "notes": notes,
                                    "newer": _ver_tuple(ver) > _ver_tuple(VERSION)})
        if p == "/api/update/github":
            try:
                ver, url, _ = github_latest(update_repo())
                if not url:
                    raise E.PipeError("Esa release no tiene un .zip")
                req = urllib.request.Request(url, headers={"User-Agent": "PS5Directo/" + VERSION})
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = r.read(50 * 1024 * 1024)
                ver = install_zip(data)
            except Exception as e:
                return self._send(400, {"ok": False, "error": str(e)})
            restart_soon()
            return self._send(200, {"ok": True, "version": ver})
        if p == "/api/whatsnew":
            return self._send(200, dict(ok=True, **whats_new("en" if b.get("lang") == "en" else "es")))
        if p == "/api/whatsnew/seen":
            with LOCK:
                SETTINGS["seen_version"] = VERSION
            save_settings()
            return self._send(200, {"ok": True})
        if p == "/api/settings":
            with LOCK:
                for k, v in b.items():
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
        if p.startswith("/api/fm/") or p.startswith("/api/lib/") or p.startswith("/api/space/") or p.startswith("/api/notifs") or p.startswith("/api/gu/") or p.startswith("/api/bk/") or p.startswith("/api/col/") or p.startswith("/api/media/") or p.startswith("/api/report") or p in ("/api/pick", "/api/history", "/api/history/clear", "/api/history/redo"):
            try:
                return self._send(200, dict(ok=True, **fm_api(p, b)))
            except E.PipeError as e:
                return self._send(200, {"ok": False, "error": str(e)})
            except Exception as e:
                traceback.print_exc()
                return self._send(200, {"ok": False, "error": str(e)})
        if p == "/api/fixperms":
            try:
                return self._send(200, {"ok": True, "results": fix_all_perms()})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})
        if p == "/api/cleanup/scan":
            try:
                return self._send(200, {"ok": True, "items": cleanup_scan()})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})
        if p == "/api/cleanup/delete":
            try:
                return self._send(200, {"ok": True, "deleted": cleanup_delete(b.get("paths") or [])})
            except Exception as e:
                return self._send(500, {"ok": False, "error": str(e)})
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
            lines = [x.strip() for x in str(b.get("lines", "")).splitlines() if x.strip()]
            if not lines:
                return self._send(400, {"ok": False, "error": "Pega al menos un enlace"})
            dest = (b.get("dest") or SETTINGS["dest"]).rstrip("/") or "/data/homebrew"
            if not dest.startswith("/data") and not dest.startswith("/mnt/"):
                return self._send(400, {"ok": False, "error": "El destino debe estar en /data o /mnt"})
            job = new_job(lines, (b.get("password") or "").strip(), dest, where=b.get("where"),
                          raw=bool(b.get("raw")), fmt=b.get("fmt"), comp=b.get("comp"))
            if "fmt" in b:
                job["fmt_set"] = True
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
                                     comp=job.get("comp"))
                        nj["fmt_set"] = job.get("fmt_set", False)
                        nj["title"] = job.get("title") or ""
                        JOBS[JOBS.index(job)] = nj
                        WAKE.notify_all()
                elif action in ("up", "down"):
                    i = JOBS.index(job)
                    k = i - 1 if action == "up" else i + 1
                    if 0 <= k < len(JOBS):
                        JOBS[i], JOBS[k] = JOBS[k], JOBS[i]
            save_jobs()
            return self._send(200, {"ok": True})
        if p == "/api/clear":
            with LOCK:
                JOBS[:] = [j for j in JOBS if j["status"] not in ("hecho", "cancelado")]
            save_jobs()
            return self._send(200, {"ok": True})
        if p == "/api/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
            return
        self._send(404, {"ok": False})


def extra_api(h, p, b):
    """Rutas de la 1.20. Devuelve el dict de respuesta o None si no es suya."""
    try:
        if p == "/api/payload/list":
            with LOCK:
                port = SETTINGS.get("elf_port") or 9021
            return {"ok": True, "items": payload_list(), "elf_port": port}
        if p == "/api/payload/send":
            return dict(ok=True, **payload_send(pid=str(b.get("id") or "")))
        if p == "/api/payload/file":
            return dict(ok=True, **payload_send(path=str(b.get("path") or "")))
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
                save_settings()
                if b.get("enable"):
                    if b.get("regen"):
                        lan_stop()
                    lan_start()
                else:
                    lan_stop()
            return dict(ok=True, **lan_info())
        if p == "/api/lan/hits":
            with LOCK:
                return {"ok": True, "hits": list(LAN_HITS[-12:])}
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


def lan_hit(h, code, raw, sent, err=""):
    path = urllib.parse.urlparse(h.path).path
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
    """Edge (viene con Windows) o Chrome, para abrir la app en su propia ventana."""
    if os.name != "nt":
        return None
    cands = []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            cands.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
            cands.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def open_window(url):
    """Abre la interfaz como ventana de aplicacion. Devuelve el proceso o None."""
    exe = _find_app_browser()
    if exe and "--browser" not in sys.argv:
        import subprocess
        profile = os.path.join(DATA_DIR, "ventana")
        try:
            return subprocess.Popen([exe, "--app=" + url, "--user-data-dir=" + profile,
                                     "--window-size=1150,900", "--no-first-run",
                                     "--no-default-browser-check"])
        except Exception:
            pass
    webbrowser.open(url)
    return None


def _powershell(script, timeout=30):
    import subprocess
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                          capture_output=True, text=True, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def ensure_shortcuts():
    """Acceso directo "PS5 Directo" en el escritorio y en "Enviar a", con el icono del programa.
    Se crea la primera vez; despues solo se corrige si apunta a otra carpeta (una copia vieja)."""
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
        else:
            open_window(ses[0] + "/#t=" + ses[1])   # ya funcionaba: solo enseno la ventana
        sys.exit(3)
    load_state()
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
        threading.Timer(0.5, lambda: open_window(url)).start()
        threading.Thread(target=watch_ui, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
