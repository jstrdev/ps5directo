"""nanoDNS (drakmor/nanoDNS): leer y cambiar su configuración /data/nanodns/nanodns.ini sin tocar lo demás.

El archivo es un .ini sencillo: [general] (log, debug, quiet, bind, bind6), [upstream] (server=… uno por línea y
timeout_ms), [overrides] (máscara=IP) y [exceptions] (una máscara por línea). nanoDNS lo lee al arrancar, así que
un cambio vale cuando se vuelve a cargar. Aquí se cambian solo las líneas que tocan: los comentarios, el orden y lo
que no se entiende se quedan como estaban.
"""
import re

INI_PATH = "/data/nanodns/nanodns.ini"
LOG_PATH = "/data/nanodns/nanodns.log"
# Yandex cambió su DNS: las configuraciones antiguas llevan 77.77.88.88, que ya no contesta (lo arregla PS5Upload)
OLD_YANDEX, NEW_YANDEX = "77.77.88.88", "77.88.8.8"
GENERAL = {"general", "settings"}
UPSTREAM = {"upstream", "upstreams"}


def _lines(text):
    nl = "\r\n" if "\r\n" in (text or "") else "\n"
    return (text or "").split(nl), nl


def _section(line):
    m = re.match(r"^\s*\[([^\]]+)\]", line)
    return m.group(1).strip().lower() if m else None


def _assign(line):
    t = line.strip()
    if not t or t[0] in "#;[":
        return None
    for c in "#;":                       # comentario al final de la línea
        i = t.find(c)
        if i >= 0:
            t = t[:i].strip()
    if "=" not in t:
        return None
    k, v = t.split("=", 1)
    k, v = k.strip().lower(), v.strip()
    return (k, v) if k and v else None


def parse(text):
    """Lo que interesa enseñar: servidores, tiempo, avisos, a quién contesta, y cuántas reglas hay."""
    out = {"servers": [], "timeout_ms": None, "quiet": False, "bind": "127.0.0.1", "bind6": "::1",
           "log": LOG_PATH, "debug": False, "overrides": 0, "exceptions": 0, "old_yandex": False}
    sec = "general"                      # nanoDNS acepta las claves generales antes de la primera sección
    for line in _lines(text)[0]:
        s = _section(line)
        if s:
            sec = s
            continue
        t = line.strip()
        if not t or t[0] in "#;":
            continue
        a = _assign(line)
        if sec in GENERAL and a:
            k, v = a
            if k in ("quiet", "debug"):
                out[k] = v.strip() == "1"
            elif k in ("bind", "bind6", "log"):
                out[k] = v
        elif sec in UPSTREAM and a:
            k, v = a
            if k in ("server", "dns"):
                out["servers"].append(v)
                if v == OLD_YANDEX:
                    out["old_yandex"] = True
            elif k == "timeout_ms":
                try:
                    out["timeout_ms"] = int(v)
                except ValueError:
                    pass
        elif sec == "overrides" and a:
            out["overrides"] += 1
        elif sec == "exceptions":
            out["exceptions"] += 1
    return out


def set_general(text, key, value):
    """Pone key=value en [general]: cambia la última línea activa de esa clave (la que usa nanoDNS) o la añade."""
    lines, nl = _lines(text)
    sec, last, gen_head, gen_end = "general", None, None, None
    for i, line in enumerate(lines):
        s = _section(line)
        if s:
            if sec in GENERAL and gen_head is not None and gen_end is None:
                gen_end = i
            sec = s
            if s in GENERAL and gen_head is None:
                gen_head = i
            continue
        a = _assign(line)
        if sec in GENERAL and a and a[0] == key:
            last = i
    if sec in GENERAL and gen_head is not None and gen_end is None:
        gen_end = len(lines)
    new = "%s=%s" % (key, value)
    if last is not None:
        lines[last] = new
    elif gen_head is not None:
        at = gen_end
        while at > gen_head + 1 and not lines[at - 1].strip():
            at -= 1                       # antes de las líneas en blanco del final de la sección
        lines.insert(at, new)
    else:
        lines[:0] = ["[general]", new, ""]
    return nl.join(lines)


def set_servers(text, servers):
    """Cambia la lista de servidores de [upstream] (en ese orden), dejando timeout_ms y los comentarios."""
    servers = [s.strip() for s in servers if s and s.strip()]
    lines, nl = _lines(text)
    sec, head, idx = None, None, []
    for i, line in enumerate(lines):
        s = _section(line)
        if s:
            sec = s
            if s in UPSTREAM and head is None:
                head = i
            continue
        a = _assign(line)
        if sec in UPSTREAM and a and a[0] in ("server", "dns"):
            idx.append(i)
    new = ["server=%s" % s for s in servers]
    if idx:
        first = idx[0]
        for i in reversed(idx):
            del lines[i]
        lines[first:first] = new
    elif head is not None:
        lines[head + 1:head + 1] = new
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["[upstream]"] + new
    return nl.join(lines)


def fix_yandex(text):
    p = parse(text)
    if not p["old_yandex"]:
        return text
    return set_servers(text, [NEW_YANDEX if s == OLD_YANDEX else s for s in p["servers"]])


def check(text):
    """Problemas que impedirían que nanoDNS arranque o que haga lo que esperas (texto para enseñar)."""
    p = parse(text)
    out = []
    if not p["servers"]:
        out.append("No hay ningún servidor en [upstream]: nanoDNS no podrá contestar nada.")
    for s in p["servers"]:
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}|[0-9a-fA-F:]+", s) or s.startswith("127.") or s == "0.0.0.0":
            out.append("«%s» no vale como servidor de [upstream] (pon una IP, por ejemplo 1.1.1.1)." % s)
    if p["old_yandex"]:
        out.append("Lleva la DNS antigua de Yandex (%s), que ya no contesta: cámbiala por %s." % (OLD_YANDEX, NEW_YANDEX))
    if len(text or "") > 256 * 1024:
        out.append("El archivo es demasiado grande.")
    return out
