"""PS5 Directo: transfiere archivos a la consola, los descomprime y los deja en su sitio.

Arranque:  python app.py   (o doble clic en "Iniciar PS5 Directo.bat")
"""

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

VERSION = "1.19.0"
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
    "tour_done": False,       # la guia de bienvenida ya se vio
    "seen_version": "",       # ultima version cuyas novedades ya se ensenaron
    "shortcut_done": False,   # ya se creo el acceso directo del escritorio una vez
    "icon_ver": "",           # version cuyo icono ya se dejo en la cache de la ventana
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
        if j["status"] not in ("hecho", "error", "cancelado"):
            j["status"] = "error"
            j["error"] = "Se cerro el programa a medias. Pulsa Reintentar: continua donde se quedo."


def save_jobs():
    with LOCK:
        _save_json(JOBS_FILE, JOBS)


def save_settings():
    with LOCK:
        _save_json(SETTINGS_FILE, SETTINGS)


# ---------------------------------------------------------------------------
# Utilidades de trabajos
# ---------------------------------------------------------------------------

def new_job(lines, password, dest, title=None, where=None, raw=False):
    return {
        "raw": bool(raw),
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

def probe_job(job, cancel):
    setj(job, status="comprobando", phase="Comprobando enlaces")
    srcs = []
    for line in job["lines"]:
        if cancel.is_set():
            raise E.Cancelled()
        s = E.make_source(line)
        jlog(job, "Comprobando: %s" % line[:120])
        s.probe()
        jlog(job, "  %s  (%s)%s" % (s.name, human(s.size) if s.size >= 0 else "tamano desconocido",
                                    "" if s.ranges else "  sin descarga en paralelo"))
        srcs.append(s)
    return srcs


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
                E.upload(cfg, "%s/%s" % (inc, s.name), s.size,
                         lambda off, s=s: s.stream(off, cfg, stop, meter),
                         lambda v, s=s: (progress(s.name, v), phase()),
                         stop, lambda msg: jlog(job, msg))
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
        return folder_job(job, srcs[0], cancel)
    names = [s.name for s in srcs]
    archives = [s for s in srcs if E.is_archive(s.name)]
    if archives and (job_where(job) == "pc" or any(E.is_tar(a.name) for a in archives)):
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
    warn_duplicates(job, bf, E.title_id(srcs[0].name),
                    when=" (aviso antes de enviar: puedes pausarlo si no lo quieres)")

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
            raise E.PipeError("No hay sitio en la PS5: faltan por enviar %s, hay %s libres y dejo %s "
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
        E.upload(cfg, remote, s.size,
                 lambda off: s.stream(off, cfg, cancel, meter),
                 lambda n: set_sent(n), cancel, lambda msg: jlog(job, msg))
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
            })
            _save_json(HISTORY_FILE, hist[-500:])
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
            for name, facts in f.mlsd(path, facts=["type", "size", "modify", "unix.mode"]):
                if facts.get("type") not in ("file", "dir"):
                    continue
                mt = facts.get("modify")
                try:
                    mt = int(time.mktime(time.strptime(mt[:14], "%Y%m%d%H%M%S"))) if mt else None
                except Exception:
                    mt = None
                mode = facts.get("unix.mode")
                out["entries"].append({"name": name, "dir": facts.get("type") == "dir",
                                       "size": int(facts.get("size") or 0), "mtime": mt,
                                       "mode": int(mode, 8) if mode else None})
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
    out = []
    try:
        for name, facts in f.mlsd(path, facts=["type", "size"]):
            t = facts.get("type")
            if t in ("file", "dir"):
                out.append((name, t == "dir", int(facts.get("size") or 0)))
    finally:
        try:
            f.voidcmd("TYPE I")
        except Exception:
            pass
    return out


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
        extra.append(("/user/app", "Juegos instalados desde la PS5", True, 0, "system"))
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
    names = {it["tid"].upper(): it["title"] for it in items if it["tid"] and it["title"]}
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
            raise E.PipeError("No hay sitio en la PS5: quedan %s libres y el margen es %s. Libera espacio "
                              "y pulsa Reintentar." % (human(free), human(margin_bytes())))
    if tid:
        warn_duplicates(job, bf, tid, exclude=target)
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
            raise E.PipeError("No cabe en la PS5: faltan %s por enviar, hay %s libres y el margen es %s. "
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
        with open(part, "r+b" if off else "wb") as f:
            f.seek(off)
            f.truncate()
            for data in s.stream(off, cfg, cancel, meter):
                f.write(data)
                got += len(data)
                setj(job, sent=done_before + got)
        if got != s.size:
            raise E.PipeError("La descarga de %s se quedo corta (%s de %s). Pulsa Reintentar."
                              % (s.name, human(got), human(s.size)))
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
    if not tar and not exe:
        raise E.PipeError("Para descomprimir en el PC hace falta 7-Zip (gratis, 7-zip.org). Instalalo, "
                          "o pon la ruta de 7z.exe en Ajustes, o elige 'Descomprimir en la PS5'.")
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
        local, created = fetch_to_pc(job, srcs, cfg, cancel, dl, folder)
        first = local[E.first_volume(names)]
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
                raise E.PipeError("No cabe en la PS5: descomprimido ocupa %s, faltan por mandar %s, "
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
                raise E.PipeError("Ya existe %s. No lo piso: borralo o renombralo y pulsa Reintentar. "
                                  "Lo enviado esta en %s" % (target, src))
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
            raise E.PipeError("No hay sitio en la PS5: quedan %s libres y el margen es %s. Libera espacio "
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


def place(job, bf, staging, dest, cancel, hint_name):
    found = find_payloads(bf, staging)
    placed = []
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
        if bf.exists(target):
            raise E.PipeError("Ya existe %s. No lo piso: borralo o renombralo y pulsa Reintentar. "
                              "Lo extraido esta en %s" % (target, path))
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
    wait_bfpilot(job, bf, cancel)
    staging = "%s/extract/%s" % (WORK_ROOT, job["id"])

    if info["mode"] == "file":
        name = info["name"]
        low = name.lower()
        target_dir = "/data/pkg" if low.endswith(".pkg") else dest
        target = target_dir + "/" + name
        if bf.exists(target):
            raise E.PipeError("Ya existe %s; no lo piso. Lo enviado esta en %s" % (target, info["path"]))
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
            txt.append("leido del RAR %s de %s" % (human(st.get("bytesWritten") or 0), human(st["totalBytes"])))
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
        label = ("contrasena %d de %d" % (i + 1, len(cands) - 1)) if pw else "sin contrasena"
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


def transfer_worker():
    while True:
        with WAKE:
            job = None
            while job is None:
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
            srcs = probe_job(job, cancel)
            sets = split_into_sets(srcs)
            if len(sets) > 1:
                jlog(job, "Detectados %d archivos distintos: los separo en varios trabajos" % len(sets))
                with LOCK:
                    idx = JOBS.index(job)
                    for k, extra in enumerate(sets[1:], 1):
                        nj = new_job([s.original for s in extra], job["password"], job["dest"],
                                     where=job.get("where"), raw=job.get("raw"))
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
            is_game = (job.pop("_placed_game", False)
                       or any(p.endswith("-app") or p.lower().endswith(E.IMAGE_EXTS) for p in placed)) \
                and (job.get("dest", "").startswith("/data/homebrew") or "/homebrew" in job.get("dest", ""))
            setj(job, status="hecho", result=placed, extract_text="",
                 phase=("Listo. ShadowMountPlus lo anadira al inicio en unos segundos" if is_game
                        else "Listo. Copiado en la PS5"))
            jlog(job, "Terminado")
            record_history(job)
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
    for a in j.get("assets") or []:
        if a.get("name", "").lower().endswith(".zip"):
            url = a.get("browser_download_url")
            break
    url = url or j.get("zipball_url")
    return (j.get("tag_name") or "").lstrip("vV"), url, (j.get("body") or "")[:2000]


# ---------------------------------------------------------------------------
# Enlaces capturados por la extension del navegador
# ---------------------------------------------------------------------------

INBOX = []          # [{"url":..., "name":..., "t":...}]


class Handler(BaseHTTPRequestHandler):
    server_version = "PS5Directo"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if self.headers.get("X-Token") != TOKEN:
            self._send(403, {"ok": False, "error": "token"})
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 5 * 1024 * 1024:
            return {}
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
        if self.path == "/api/ext/ping":
            return self._send(200, {"ok": True, "app": "PS5 Directo", "version": VERSION})
        if self.path == "/api/state":
            if not self._auth():
                return
            if self.headers.get("X-UI"):
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
                      "inbox": len(INBOX), "repo": SETTINGS.get("update_repo") or DEFAULT_REPO}
            st["settings"].pop("ftp_pass", None)
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
            if (q.get("t") or [""])[0] == TOKEN:
                UI_SEEN["bye"] = time.time()
            return self._send(200, {"ok": True})
        if self.path == "/api/ext/capture":
            if not self._from_extension():
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
        if p.startswith("/api/fm/") or p.startswith("/api/space/") or p.startswith("/api/notifs") or p in ("/api/pick", "/api/history", "/api/history/clear"):
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
            return self._send(200, {"ok": True, **test_connection(cfg_snapshot())})
        if p == "/api/add":
            lines = [x.strip() for x in str(b.get("lines", "")).splitlines() if x.strip()]
            if not lines:
                return self._send(400, {"ok": False, "error": "Pega al menos un enlace"})
            dest = (b.get("dest") or SETTINGS["dest"]).rstrip("/") or "/data/homebrew"
            if not dest.startswith("/data") and not dest.startswith("/mnt/"):
                return self._send(400, {"ok": False, "error": "El destino debe estar en /data o /mnt"})
            job = new_job(lines, (b.get("password") or "").strip(), dest, where=b.get("where"),
                          raw=bool(b.get("raw")))
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
                    elif job["status"] in ("comprobando", "enviando"):
                        job["_pause"] = True
                        CANCELS.setdefault(job_id, threading.Event()).set()
                elif action == "cancel":
                    CANCELS.setdefault(job_id, threading.Event()).set()
                    if job["status"] in ("pendiente", "en cola para descomprimir"):
                        job["status"] = "cancelado"
                        job["phase"] = "Cancelado"
                elif action == "retry":
                    if job["status"] in ("error", "cancelado", "pausado"):
                        if b.get("password"):
                            job["password"] = b["password"].strip()
                        CANCELS.setdefault(job_id, threading.Event()).clear()
                        job["error"] = ""
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
        if busy() or not seen:
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
    for target in (transfer_worker, extract_worker):
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
