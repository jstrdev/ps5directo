"""Actualizacion segura de payloads (.elf) en la consola.

Que hace y que NO hace:
- Reduce riesgos al instalar, cambiar o cargar payloads: comprueba el .elf, guarda copia de lo anterior, cambia los
  archivos con un rename en el mismo volumen (nunca escribe encima directamente), comprueba con SHA-256 lo que queda
  en la consola, deja un diario en el PC para terminar o deshacer lo que se corte y avisa de mezclas y tiempos de
  carga peligrosos.
- NO evita kernel panics ni corrupciones del sistema de archivos que cause un payload defectuoso al ejecutarse: eso
  pasa dentro de la consola, fuera del alcance de PS5 Directo.

Todo lo que toca la consola va por funciones de un objeto FTP tipo ftplib (storbinary, retrbinary, rename, delete,
size, mkd, rmd) que se recibe desde fuera, para poder probarlo con un FTP simulado (tests/test_pldsafe.py).
Payload Manager (itsPLK) ya escribe sus slots y su autoload.txt de forma atomica; aqui se comprueba el resultado y se
guarda copia para poder volver atras.
"""

import ftplib
import hashlib
import io
import json
import os
import posixpath
import re
import struct
import threading
import time
import uuid

MiB = 1024 * 1024
ELF_MAX = 64 * MiB
PM_ENTRY_GAP_MS = 500          # Payload Manager deja 500 ms fijos tras cada payload del autoload (src/autoload.c)

FTP_ERRORS = ftplib.all_errors + (OSError, EOFError)


class SafeError(Exception):
    """Error que se le puede ensenar tal cual a quien usa la app."""


class Interrupted(SafeError):
    """Se corto a medias (conexion, cierre...): el diario queda abierto para recuperarlo."""


# ---------------------------------------------------------------------------
# Validacion estructural del ELF
# ---------------------------------------------------------------------------

PT_LOAD = 1


def elf_check(data):
    """Comprueba que es un ELF de 64 bits para x86-64 (la CPU de la PS5) bien formado y completo.
    Devuelve {"ok", "errors", "warnings", "info"}. No dice nada de si el payload es bueno o seguro."""
    errs, warns, info = [], [], {}
    n = len(data or b"")
    info["size"] = n
    if n < 64:
        return {"ok": False, "errors": ["El archivo es demasiado pequeño para ser un .elf (%d bytes)" % n],
                "warnings": [], "info": info}
    if n > ELF_MAX:
        errs.append("El archivo pesa más de 64 MB: no parece un payload")
    if data[:4] != b"\x7fELF":
        if data[:4] in (b"\x4f\x15\x3d\x1d", b"\x54\x14\xf5\xee"):
            errs.append("Es un SELF/fself (formato firmado de la consola), no un .elf de payload")
        else:
            errs.append("No empieza como un .elf (cabecera \\x7fELF)")
        return {"ok": False, "errors": errs, "warnings": warns, "info": info}
    ei_class, ei_data, ei_ver = data[4], data[5], data[6]
    if ei_class != 2:
        errs.append("Es un ELF de 32 bits: los payloads de PS5 son de 64")
    if ei_data != 1:
        errs.append("No es little-endian")
    if ei_ver != 1:
        warns.append("Versión de ELF rara (%d)" % ei_ver)
    if errs:
        return {"ok": False, "errors": errs, "warnings": warns, "info": info}
    (e_type, e_machine, e_version, e_entry, e_phoff, e_shoff, e_flags, e_ehsize, e_phentsize, e_phnum,
     e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    info.update(type=e_type, machine=e_machine, entry=e_entry, phnum=e_phnum, shnum=e_shnum)
    if e_machine != 0x3E:
        errs.append("No es para x86-64 (máquina 0x%X)" % e_machine)
    if e_type not in (2, 3):
        errs.append("No es ejecutable (tipo %d)" % e_type)
    if e_ehsize < 64:
        errs.append("Cabecera ELF incompleta")
    if e_phnum == 0 or e_phentsize < 56:
        errs.append("No tiene tabla de segmentos (program headers)")
    elif e_phoff + e_phnum * e_phentsize > n:
        errs.append("La tabla de segmentos se sale del archivo: está cortado")
    loads = []
    if not errs:
        for i in range(e_phnum):
            off = e_phoff + i * e_phentsize
            p_type, p_flags, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_align = struct.unpack_from(
                "<IIQQQQQQ", data, off)
            if p_type == PT_LOAD:
                loads.append((p_vaddr, p_memsz))
                if p_offset + p_filesz > n:
                    errs.append("El segmento %d se sale del archivo (falta el final: archivo cortado)" % i)
                if p_filesz > p_memsz:
                    errs.append("El segmento %d es incoherente (ocupa más en disco que en memoria)" % i)
        if not loads:
            errs.append("No tiene segmentos cargables (PT_LOAD)")
        elif e_entry and not any(v <= e_entry < v + m for v, m in loads):
            errs.append("El punto de entrada no cae dentro de ningún segmento")
    if e_shnum and e_shoff and e_shoff + e_shnum * e_shentsize > n:
        warns.append("La tabla de secciones se sale del archivo (puede estar cortado)")
    info["loads"] = len(loads)
    return {"ok": not errs, "errors": errs, "warnings": warns, "info": info}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Matriz de compatibilidad (lo que piden sus autores; revisada en noviembre de 2026)
# ---------------------------------------------------------------------------
# critical: parchea el kernel o el sistema de montaje: si falla puede colgar la consola.
# group: dos del mismo grupo «excl» no deben ir a la vez (gestores de parches del kernel).
# delay_before / delay_after: pausa recomendada (ms) antes y despues en la carga automatica.
# fw: rango de firmware documentado. experimental: todas sus versiones son de prueba.

MATRIX = [
    {"family": "kstuff-dr", "name": "kstuff-dr (drakmor)",
     "rx": r"kstuff[\w.\-]*?[-_]dr(?:[-_.]|$)|kstuff[-_]?dr", "critical": True, "group": "kpatch",
     "experimental": True, "fw": ("1.00", "13.60"), "delay_before": 5000, "delay_after": 5000, "order": 16,
     "needs": [], "source": "drakmor",
     "note": "Versión de pruebas: su autor pide 5 s antes y después y cargarlo antes que ShadowMountPlus."},
    {"family": "kstuff-toggle", "name": "kstuff-toggle", "rx": r"kstuff[-_ ]?toggle", "critical": False,
     "fw": ("3.00", "12.00"), "needs": ["kpatch"], "order": 25, "source": "EchoStretch"},
    # kstuff-lite trae parches hasta la 13.60 (shellcore_patches/13_60.h) y es el que pide ShadowMountPlus (1.07 o
    # posterior), que se carga ANTES que él con 3 s de pausa (su README: shadowmountplus.elf, !3000, kstuff.elf)
    {"family": "kstuff-lite", "name": "kstuff-lite", "rx": r"kstuff[-_ ]?lite", "critical": True, "group": "kpatch",
     "fw": ("1.00", "13.60"), "delay_before": 3000, "delay_after": 3000, "order": 20, "source": "EchoStretch/kstuff-lite",
     "note": "Es el que pide ShadowMountPlus: va después de él, con 3 s de pausa."},
    {"family": "kstuff", "name": "kstuff", "rx": r"kstuff", "critical": True, "group": "kpatch",
     "fw": ("3.00", "13.60"), "delay_before": 3000, "delay_after": 3000, "order": 20, "source": "sleirsgoevy y otros"},
    {"family": "etahen", "name": "etaHEN", "rx": r"eta.?hen", "critical": True, "group": "kpatch", "hen": True,
     "fw": ("3.00", "10.01"), "order": 15, "source": "etaHEN",
     "note": "Trae su propio kstuff: no cargues otro a la vez."},
    {"family": "onionhen", "name": "onionHEN", "rx": r"onion.?hen", "critical": True, "hen": True, "order": 15},
    {"family": "hyperhen", "name": "HyperHEN", "rx": r"hyper.?hen", "critical": True, "hen": True, "order": 15},
    {"family": "titancore", "name": "TITAN CORE", "rx": r"titan.?core", "critical": True, "group": "kpatch",
     "order": 20, "note": "Hace de kstuff y de montador: no se mezcla con kstuff-dr ni con ShadowMountPlus."},
    {"family": "krakenmount", "name": "Kraken Mount", "rx": r"kraken.?mount", "critical": True, "group": "mount",
     "needs": ["kpatch"], "order": 30, "note": "No funciona a la vez que ShadowMountPlus."},
    {"family": "shadowmountplus", "name": "ShadowMountPlus", "rx": r"shadow.?mount", "critical": True,
     "group": "mount", "needs": ["kpatch"], "order": 18, "source": "drakmor/ShadowMountPlus"},
    {"family": "klog", "name": "Kernel LOG collector", "rx": r"k(?:ernel)?.?log.?(?:collector|srv|server)|klogsrv",
     "critical": False, "order": 10},
    {"family": "elfldr", "name": "elfldr", "rx": r"elfldr", "critical": False, "order": 5, "delay_after": 2000,
     "source": "Payload Manager #154",
     "note": "Justo después de kstuff el cargador puede dejar de escuchar unos segundos."},
    {"family": "sentinel", "name": "sentinel_warden", "rx": r"sentinel.?warden", "critical": False, "order": 40},
    {"family": "backpork", "name": "backpork", "rx": r"backpork", "critical": False, "order": 40},
    {"family": "ftpsrv", "name": "ftpsrv", "rx": r"ftp.?srv|ftpsrv", "critical": False, "order": 50},
    {"family": "bfpilot", "name": "BFpilot", "rx": r"bf.?pilot", "critical": False, "order": 50},
    {"family": "websrv", "name": "websrv", "rx": r"websrv", "critical": False, "order": 50},
    {"family": "nanodns", "name": "nanodns", "rx": r"nanodns", "critical": False, "order": 50},
    {"family": "pldmgr", "name": "Payload Manager", "rx": r"pldmgr|payload.?manager", "critical": False, "order": 0},
    {"family": "prefbar", "name": "PrefBar", "rx": r"pref.?bar", "critical": False, "group": "overlay",
     "experimental": True, "fw": ("4.00", "13.60"), "order": 60, "source": "X1NONs/PrefBar",
     "note": "Overlay de rendimiento joven (versiones 0.x): dibuja dentro de la interfaz de la consola."},
    {"family": "hwoverlay", "name": "PS5 Hardware Overlay", "rx": r"ps5.?(?:new.?)?overlay|hardware.?overlay",
     "critical": False, "group": "overlay", "order": 60, "source": "smoxa/ps5-new-overlay"},
]
for _m in MATRIX:
    _m["_rx"] = re.compile(r"(?i)" + _m["rx"])

# Parejas que no deben ir juntas: (familia o grupo, familia o grupo, nivel, texto, condicion opcional)
CONFLICTS = [
    ("etahen", "kstuff-dr", "bad", "etaHEN ya trae su kstuff: no cargues también kstuff-dr."),
    ("etahen", "kstuff-lite", "bad", "etaHEN ya trae su kstuff: sobra kstuff-lite."),
    ("etahen", "kstuff", "bad", "etaHEN ya trae su kstuff: sobra el otro."),
    ("titancore", "kstuff-dr", "bad", "TITAN CORE y kstuff-dr no se cargan juntos."),
    ("krakenmount", "kstuff-dr", "bad", "Kraken Mount no se carga junto a kstuff-dr (su guía pide quitarlo)."),
    ("krakenmount", "shadowmountplus", "bad", "Kraken Mount y ShadowMountPlus no funcionan a la vez."),
    ("titancore", "shadowmountplus", "warn", "TITAN CORE ya monta títulos: puede chocar con ShadowMountPlus."),
    ("shadowmountplus", "sentinel", "bad", "ShadowMountPlus 1.6 o posterior ya pausa kstuff solo: quita "
     "sentinel_warden (dos programas pausándolo a la vez).", (1, 6)),
    ("shadowmountplus", "backpork", "bad", "ShadowMountPlus 1.6 o posterior monta los backports él mismo: su autor "
     "pide quitar backpork.", (1, 6)),
    ("prefbar", "hwoverlay", "warn", "Dos overlays dibujando encima de la imagen a la vez: usa solo uno."),
    ("shadowmountplus", "etahen", "bad", "Con ShadowMountPlus 1.7 su autor pide no usar etaHEN (usa kstuff).", (1, 7)),
]

EXPERIMENTAL_RX = re.compile(
    r"(?i)(?:^|[^a-z])(test\d*|alpha\d*|beta\d*|b\d+|rc\d*|dev|nightly|wip|preview|pre\d*|exp|experimental|"
    r"canary|draft|unstable|debug)(?:[^a-z]|$)")


def ver_tuple(v):
    m = re.search(r"(\d+)\.(\d+)", v or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def fw_tuple(v):
    m = re.search(r"(\d{1,2})\.(\d{2})", v or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def family_of(*names):
    text = " ".join(x for x in names if x)
    for m in MATRIX:
        if m["_rx"].search(text):
            return m
    return None


def version_of(filename):
    m = re.search(r"(?i)[_\- ]v?(\d+(?:\.\d+)+(?:[\w.\-]*?)?)(?:\.elf)?$", filename or "")
    return m.group(1) if m else ""


def is_experimental(name="", version="", filename="", fam=None):
    """De pruebas si su familia lo es o si el nombre o la version llevan test/beta/rc/alpha...; que ademas diga
    «stable» no cambia nada (kstuff-1.15-dr-test3-stable sigue siendo de pruebas)."""
    fam = fam or family_of(name, filename)
    if fam and fam.get("experimental"):
        return True
    for t in (version, filename, name):
        if t and EXPERIMENTAL_RX.search(re.sub(r"(?i)\.elf$", "", t)):
            return True
    return False


def classify(name="", version="", filename="", fw=""):
    """{"family", "label", "critical", "experimental", "known", "level", "tags", "fw_ok", "notes"}.
    level: «experimental» > «critico» > «desconocido» > «normal»."""
    fam = family_of(name, filename)
    exp = is_experimental(name, version, filename, fam)
    crit = bool(fam and fam.get("critical"))
    tags, notes = [], []
    if crit:
        tags.append("critico")
    if exp:
        tags.append("experimental")
    if not fam:
        tags.append("desconocido")
    fw_ok = None
    if fam and fam.get("fw") and fw_tuple(fw):
        lo, hi = fw_tuple(fam["fw"][0]), fw_tuple(fam["fw"][1])
        fw_ok = lo <= fw_tuple(fw) <= hi
        if not fw_ok:
            notes.append("%s está documentado para firmware %s–%s y tu consola tiene %s." %
                         (fam["name"], fam["fw"][0], fam["fw"][1], fw))
    if fam and fam.get("note"):
        notes.append(fam["note"])
    level = "experimental" if exp else "critico" if crit else "desconocido" if not fam else "normal"
    return {"family": fam["family"] if fam else "", "label": fam["name"] if fam else (name or filename),
            "critical": crit, "experimental": exp, "known": bool(fam), "level": level, "tags": tags,
            "fw_ok": fw_ok, "notes": notes}


# ---------------------------------------------------------------------------
# Revision de la lista de carga automatica (Payload Manager: una entrada por linea, «!<ms>» = pausa)
# ---------------------------------------------------------------------------

def _secs(ms):
    return ("%.1f" % (ms / 1000.0)).rstrip("0").rstrip(".")


def parse_autoload(text):
    """«a,!5000,b» o una por linea -> [{"key", "delay"}] (delay en ms para las pausas, None para payloads)."""
    out = []
    for x in re.split(r"[,\n]", text or ""):
        x = x.strip()
        if not x:
            continue
        if re.fullmatch(r"!\d{1,7}", x):
            out.append({"key": x, "delay": int(x[1:])})
        else:
            out.append({"key": x, "delay": None})
    return out


def join_autoload(entries):
    return ",".join(e["key"] for e in entries)


def autoload_check(entries, items, fw="", running=(), start_delay_ms=None, gap_ms=PM_ENTRY_GAP_MS):
    """Revisa la lista: pausas segun lo documentado, repetidos, choques entre gestores de parches, orden, versiones
    de pruebas que se cargarian solas en cada arranque y firmware. items: slot -> {"filename", "name", "version"}.
    Devuelve {"issues": [{"level", "code", "text"}], "rows": [...], "fixed": [...] | None}."""
    issues, rows = [], []
    pay = []                         # [(indice en entries, info)]
    for i, e in enumerate(entries):
        if e["delay"] is not None:
            rows.append({"key": e["key"], "delay": e["delay"]})
            continue
        it = items.get(e["key"]) or {}
        fn = it.get("filename") or e["key"]
        c = classify(it.get("name") or "", it.get("version") or version_of(fn), fn, fw)
        fam = family_of(it.get("name") or "", fn)
        row = {"key": e["key"], "file": fn, "missing": not it, "class": c}
        rows.append(row)
        pay.append((i, row, fam))
        if not it and items:
            issues.append({"level": "warn", "code": "missing",
                           "text": "«%s» está en la lista pero no aparece instalado en Payload Manager." % e["key"]})
    # pausa efectiva entre payloads consecutivos = 500 ms fijos + las «!<ms>» que haya entre medias
    def gap_between(a, b):
        return gap_ms + sum(entries[k]["delay"] for k in range(a + 1, b) if entries[k]["delay"] is not None)

    def lead_in(a):
        return sum(entries[k]["delay"] for k in range(0, a) if entries[k]["delay"] is not None) + \
            (start_delay_ms or 0)

    need_before = {}                 # posicion en entries -> pausa minima que deberia haber delante
    for n, (i, row, fam) in enumerate(pay):
        if not fam:
            continue
        nm = fam["name"]
        db, da = fam.get("delay_before") or 0, fam.get("delay_after") or 0
        if db:
            have = gap_between(pay[n - 1][0], i) if n else lead_in(i)
            if have < db:
                issues.append({"level": "bad", "code": "delay_before", "text":
                               "Antes de %s hay %s s de pausa y se recomiendan %d s (según %s)." %
                               (nm, _secs(have), db // 1000, fam.get("source") or "su autor")})
                need_before[i] = max(need_before.get(i, 0), db - (have if n else 0))
        if da and n + 1 < len(pay):
            j = pay[n + 1][0]
            have = gap_between(i, j)
            if have < da:
                issues.append({"level": "bad" if fam.get("critical") else "warn", "code": "delay_after", "text":
                               "Después de %s hay %s s de pausa y se recomiendan %d s (según %s)." %
                               (nm, _secs(have), da // 1000, fam.get("source") or "su autor")})
                need_before[j] = max(need_before.get(j, 0), da - have + gap_ms)
    # repetidos
    seen_k, seen_f = {}, {}
    dups, reported = set(), set()
    for i, row, fam in pay:
        if row["key"] in seen_k:
            issues.append({"level": "bad", "code": "dup_slot",
                           "text": "«%s» está dos veces en la lista: se cargaría dos veces." % row["file"]})
            dups.add(i)
        seen_k.setdefault(row["key"], i)
        if fam:
            first = seen_f.setdefault(fam["family"], row["key"])
            if first != row["key"] and fam["family"] not in reported:
                reported.add(fam["family"])
                issues.append({"level": "bad", "code": "dup_family",
                               "text": "Hay dos %s distintos en la lista (%s): deja solo uno." %
                               (fam["name"], ", ".join(sorted({r["file"] for _, r, f in pay
                                                               if f and f["family"] == fam["family"]})))})
    fams = [f for _, _, f in pay if f]
    famset = {f["family"] for f in fams}
    # dos gestores de parches del kernel distintos
    kp = sorted({f["name"] for f in fams if f.get("group") == "kpatch"})
    if len(kp) > 1:
        issues.append({"level": "bad", "code": "kpatch",
                       "text": "Hay varios gestores de parches del kernel a la vez (%s): usa solo uno." % ", ".join(kp)})
    hens = sorted({f["name"] for f in fams if f.get("hen")})
    if len(hens) > 1:
        issues.append({"level": "bad", "code": "hens", "text": "Varios HEN a la vez (%s): usa solo uno." % ", ".join(hens)})
    issues += conflict_issues(famset, {r["class"]["family"]: r for _, r, _ in pay}, items)
    # dependencias
    for f in fams:
        for need in f.get("needs") or []:
            if not any(g.get("group") == need or g["family"] == need for g in fams) and \
                    not any(family_of(p) and (family_of(p).get("group") == need) for p in running or ()):
                issues.append({"level": "warn", "code": "needs",
                               "text": "%s necesita un kstuff y no está en la lista." % f["name"]})
    # orden
    pos = {}
    for i, row, fam in pay:
        if fam:
            pos.setdefault(fam["family"], i)
    swaps = []
    # cada autor manda: kstuff-dr antes de ShadowMountPlus (drakmor); kstuff y kstuff-lite después (README de SMP)
    for a, b in (("kstuff-dr", "shadowmountplus"), ("shadowmountplus", "kstuff-lite"), ("shadowmountplus", "kstuff"),
                 ("klog", "kstuff-dr")):
        if a in pos and b in pos and pos[b] < pos[a]:
            swaps.append((a, b))
            issues.append({"level": "warn", "code": "order", "text": "%s debería ir antes que %s." %
                           (next(m["name"] for m in MATRIX if m["family"] == a),
                            next(m["name"] for m in MATRIX if m["family"] == b))})
    # versiones de pruebas y firmware
    for i, row, fam in pay:
        c = row["class"]
        if c["experimental"]:
            issues.append({"level": "warn", "code": "experimental",
                           "text": "«%s» es una versión de pruebas y se carga sola en cada arranque: si cuelga la "
                                   "consola, volverá a pasar al encenderla." % row["file"]})
        if c["fw_ok"] is False:
            issues.append({"level": "bad", "code": "fw", "text": c["notes"][0]})
    fixed = None
    if swaps:
        # orden mal: el que tiene que ir antes se pone justo delante del otro (lo demás queda como estaba)
        for a, b in swaps:
            ia = next(n for n, x in enumerate(pay) if x[2] and x[2]["family"] == a)
            item = pay.pop(ia)
            ib = next(n for n, x in enumerate(pay) if x[2] and x[2]["family"] == b)
            pay.insert(ib, item)
    if need_before or dups or swaps:
        # una sola pausa entre cada par de payloads, la mayor de: la que ya habia, la que pide el siguiente antes
        # y la que pide el anterior despues; sin pausas sueltas al final
        out, prev, last = [], None, -1
        for n, (i, row, fam) in enumerate(pay):
            if i in dups:
                continue
            have = sum(entries[k]["delay"] for k in range(last + 1, i) if entries[k]["delay"] is not None)
            need = (fam or {}).get("delay_before") or 0
            if prev is not None:
                need = max(need, (prev or {}).get("delay_after") or 0)
            elif start_delay_ms and need:
                need = max(0, need - start_delay_ms)
            want = max(have, need)            # la pausa tal cual la documenta el autor (los 500 ms fijos, de margen)
            if want > 0:
                ms = int(-(-want // 500) * 500)
                out.append({"key": "!%d" % ms, "delay": ms})
            out.append(dict(entries[i]))
            prev, last = fam or {}, i
        if join_autoload(out) != join_autoload(entries):
            fixed = out
    order = {"bad": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda x: order.get(x["level"], 3))
    return {"issues": issues, "rows": rows, "fixed": fixed}


def conflict_issues(famset, rows_by_family=None, items=None, smp_version=""):
    out = []
    smp_v = ver_tuple(smp_version)
    if not smp_v and rows_by_family and rows_by_family.get("shadowmountplus"):
        r = rows_by_family["shadowmountplus"]
        smp_v = ver_tuple(((items or {}).get(r["key"]) or {}).get("version") or version_of(r["file"]))
    for c in CONFLICTS:
        a, b, lvl, text = c[:4]
        cond = c[4] if len(c) > 4 else None
        if a in famset and b in famset:
            if cond and smp_v and smp_v < cond:
                continue
            out.append({"level": lvl, "code": "conflict", "text": text})
    return out


def running_check(procs, fw=""):
    """Payloads en marcha ahora mismo: duplicados y choques (por ejemplo dos kstuff cargados)."""
    names = [p.get("name") or "" for p in procs or []]
    fams = {}
    for n in names:
        f = family_of(n)
        if f:
            fams.setdefault(f["family"], []).append(n)
    issues = []
    for k, v in fams.items():
        if len(v) > 1 and k not in ("pldmgr",):
            issues.append({"level": "bad", "code": "dup_running",
                           "text": "%s está cargado %d veces a la vez." % (v[0], len(v))})
    kp = sorted({family_of(n)["name"] for n in names if family_of(n) and family_of(n).get("group") == "kpatch"})
    if len(kp) > 1:
        issues.append({"level": "bad", "code": "kpatch_running",
                       "text": "Hay varios gestores de parches del kernel en marcha (%s)." % ", ".join(kp)})
    issues += conflict_issues(set(fams))
    return issues


# ---------------------------------------------------------------------------
# FTP: leer con hash, escribir, renombrar (todo sobre un objeto tipo ftplib.FTP)
# ---------------------------------------------------------------------------

def is_missing(e):
    return isinstance(e, ftplib.error_perm) and str(e)[:3] in ("550", "450", "553")


def ftp_read_all(f, path, limit=ELF_MAX + 1):
    """Lee el archivo entero (los payloads pesan poco). None si no existe."""
    buf = bytearray()

    def cb(chunk):
        buf.extend(chunk)
        if len(buf) > limit:
            raise SafeError("%s es demasiado grande para ser un payload" % path)
    try:
        f.retrbinary("RETR " + path, cb, 65536)
    except ftplib.error_perm as e:
        if is_missing(e):
            return None
        raise
    return bytes(buf)


def ftp_hash(f, path):
    """(sha256, tamano) leyendo el archivo de vuelta por FTP (ftpsrv no tiene comando de hash). None si no existe."""
    d = ftp_read_all(f, path)
    return None if d is None else (sha256(d), len(d))


def ftp_mkdirs(f, path):
    cur = ""
    for part in [p for p in path.split("/") if p]:
        cur += "/" + part
        try:
            f.mkd(cur)
        except ftplib.error_perm:
            pass


def ftp_try_delete(f, path):
    try:
        f.delete(path)
        return True
    except FTP_ERRORS:
        return False


def ftp_try_rmd(f, path):
    try:
        f.rmd(path)
    except FTP_ERRORS:
        pass


def stage_root(target):
    """Carpeta temporal en el MISMO volumen que el destino (si no, rename falla con EXDEV) y fuera de las carpetas
    que cargan solas los autoloaders (/data/pldmgr, /data/ps5_autoloader, /data/etaHEN...)."""
    target = posixpath.normpath(target or "")
    m = re.match(r"^(/data|/mnt/(?:usb|ext)\d+)(?:/|$)", target)
    if not m:
        return None
    return "/data/ps5directo/.pldtx" if m.group(1) == "/data" else m.group(1) + "/ps5directo/.pldtx"


AUTOLOAD_DIRS = ("/data/pldmgr/payloads", "/data/ps5_autoloader", "/data/etaHEN", "/mnt/usb0/ps5_autoloader",
                 "/mnt/usb1/ps5_autoloader", "/mnt/usb0/pldmgr", "/mnt/usb1/pldmgr")


# ---------------------------------------------------------------------------
# Diario, copias e historial (en el PC)
# ---------------------------------------------------------------------------

def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


class Store:
    """Carpeta ~/.ps5directo/payloads_seguros:
    diario.json (operaciones a medias), copias/<sha256>.elf + copias/indice.json (versiones guardadas),
    config/<fecha>.json (copias de la configuracion de Payload Manager), registro.jsonl (todo lo que se hace,
    con hashes, versiones, rutas, fechas y errores) y lanzamientos.json (cargas cuyo resultado no se sabe)."""

    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        for d in ("", "copias", "config"):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        self.journal_path = os.path.join(root, "diario.json")
        self.index_path = os.path.join(root, "copias", "indice.json")
        self.log_path = os.path.join(root, "registro.jsonl")
        self.launch_path = os.path.join(root, "lanzamientos.json")

    # registro
    def log(self, op, **kw):
        rec = {"t": round(time.time(), 3), "fecha": time.strftime("%Y-%m-%d %H:%M:%S"), "op": op}
        rec.update({k: v for k, v in kw.items() if v is not None})
        line = json.dumps(rec, ensure_ascii=False)
        with self.lock:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        return rec

    def read_log(self, n=200):
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                lines = fh.readlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    # diario
    def journal(self):
        with self.lock:
            return _load_json(self.journal_path, {})

    def jput(self, tx):
        with self.lock:
            j = self.journal()
            tx["updated"] = time.time()
            j[tx["id"]] = tx
            _atomic_json(self.journal_path, j)

    def jdel(self, txid):
        with self.lock:
            j = self.journal()
            if j.pop(txid, None) is not None:
                _atomic_json(self.journal_path, j)

    # copias
    def index(self):
        with self.lock:
            return _load_json(self.index_path, [])

    def save_copy(self, data, **meta):
        h = sha256(data)
        p = os.path.join(self.root, "copias", h + ".elf")
        with self.lock:
            if not (os.path.isfile(p) and self.copy_ok(h)):
                tmp = p + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
                os.replace(tmp, p)
            idx = self.index()
            rec = {"sha256": h, "size": len(data), "saved": time.time()}
            rec.update({k: v for k, v in meta.items() if v is not None})
            idx = [x for x in idx if not (x["sha256"] == h and x.get("target") == rec.get("target") and
                                          x.get("slot") == rec.get("slot"))]
            idx.append(rec)
            _atomic_json(self.index_path, idx[-400:])
        return h

    def copy_ok(self, h):
        p = os.path.join(self.root, "copias", h + ".elf")
        try:
            with open(p, "rb") as fh:
                return sha256(fh.read()) == h
        except OSError:
            return False

    def load_copy(self, h):
        p = os.path.join(self.root, "copias", re.sub(r"[^0-9a-f]", "", h) + ".elf")
        with open(p, "rb") as fh:
            data = fh.read()
        if sha256(data) != h:
            raise SafeError("La copia guardada de %s… está dañada en el PC" % h[:12])
        return data

    def save_config(self, kind, conf, raw=None):
        name = "%s-%s.json" % (time.strftime("%Y%m%d-%H%M%S"), kind)
        p = os.path.join(self.root, "config", name)
        _atomic_json(p, {"t": time.time(), "kind": kind, "config": conf, "raw": raw})
        files = sorted(os.listdir(os.path.join(self.root, "config")))
        for old in files[:-100]:
            try:
                os.remove(os.path.join(self.root, "config", old))
            except OSError:
                pass
        return name

    def configs(self):
        d = os.path.join(self.root, "config")
        out = []
        for n in sorted(os.listdir(d), reverse=True)[:30]:
            j = _load_json(os.path.join(d, n), None)
            if j:
                out.append({"name": n, "t": j.get("t"), "kind": j.get("kind"),
                            "list": (j.get("config") or {}).get("AUTOLOAD_LIST", "")})
        return out

    def load_config(self, name):
        n = os.path.basename(name or "")
        j = _load_json(os.path.join(self.root, "config", n), None)
        if not j:
            raise SafeError("No encuentro esa copia de la configuración")
        return j

    # lanzamientos
    def launches(self):
        with self.lock:
            return _load_json(self.launch_path, {})

    def set_launch(self, key, **kw):
        with self.lock:
            d = self.launches()
            if kw.get("clear"):
                d.pop(key, None)
            else:
                d[key] = dict(kw, t=time.time())
            _atomic_json(self.launch_path, d)


# ---------------------------------------------------------------------------
# Transacciones
# ---------------------------------------------------------------------------

class Safe:
    """Operaciones seguras. connect() devuelve un objeto FTP nuevo y conectado. busy() devuelve un texto si hay
    transferencias, descompresiones o instalaciones en marcha (o "" si no)."""

    def __init__(self, store, connect=None, busy=None, hook=None):
        self.s = store
        self.connect = connect
        self.busy = busy or (lambda: "")
        self.hook = hook or (lambda point, tx=None: None)    # las pruebas lo usan para simular cierres
        self.op_lock = threading.Lock()
        self.boot_epoch = None            # función: cuándo se encendió la consola (segundos, reloj de este PC) o None

    # comprobaciones previas ------------------------------------------------
    def preflight(self, data, name, filename="", version="", fw="", allow_experimental=False, critical_gate=True):
        chk = elf_check(data)
        if not chk["ok"]:
            raise SafeError("El .elf no es válido: " + "; ".join(chk["errors"]))
        c = classify(name, version, filename, fw)
        if c["fw_ok"] is False:
            raise SafeError(c["notes"][0])
        if c["critical"] and critical_gate:
            b = self.busy()
            if b:
                raise SafeError("Espera a que termine lo que está en marcha (%s) antes de cambiar %s: es un "
                                "payload crítico." % (b, c["label"]))
        if c["experimental"] and not allow_experimental:
            raise NeedsConfirm(c)
        return chk, c

    # instalar un archivo por FTP (destino fijo) -----------------------------
    def ftp_install(self, target, data, meta=None, allow_experimental=False, fw=""):
        meta = dict(meta or {})
        target = posixpath.normpath(target)
        root = stage_root(target)
        if not root:
            raise SafeError("Solo se puede instalar de forma segura dentro de /data o de un USB/disco externo")
        if any(root == d or root.startswith(d + "/") for d in AUTOLOAD_DIRS):
            raise SafeError("La carpeta temporal no puede estar dentro de una carpeta de carga automática")
        name = meta.get("name") or posixpath.basename(target)
        chk, c = self.preflight(data, name, posixpath.basename(target), meta.get("version") or "", fw,
                                allow_experimental)
        if not self.op_lock.acquire(blocking=False):
            raise SafeError("Ya hay otra operación de payloads en marcha")
        try:
            self.recover(only_kind="ftp")
            if any(t.get("target") == target for t in self.s.journal().values()):
                raise SafeError("Hay una operación sin terminar sobre %s: recupérala primero" % target)
            return self._ftp_install(target, data, meta, c, root)
        finally:
            self.op_lock.release()

    def _ftp_install(self, target, data, meta, c, root):
        new_h = sha256(data)
        txid = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        sdir = "%s/%s" % (root, txid)
        tx = {"id": txid, "kind": "ftp", "target": target, "stage": sdir, "staged": sdir + "/nuevo.elf",
              "aside": sdir + "/anterior.elf", "new_sha": new_h, "new_size": len(data), "state": "inicio",
              "name": meta.get("name") or "", "version": meta.get("version") or "", "source": meta.get("source"),
              "class": c["level"], "started": time.time()}
        f = self.connect()
        try:
            old = ftp_read_all(f, target)
        except FTP_ERRORS as e:
            self._close(f)
            raise SafeError("No he podido leer el payload actual (%s): no cambio nada" % e)
        tx["old_sha"] = sha256(old) if old is not None else None
        tx["old_size"] = len(old) if old is not None else None
        if old is not None:
            if tx["old_sha"] == new_h:
                self._close(f)
                self.s.log("instalar", resultado="ya_estaba", destino=target, sha256=new_h, version=tx["version"])
                return {"ok": True, "same": True, "target": target, "sha256": new_h}
            self.s.save_copy(old, target=target, filename=posixpath.basename(target), name=meta.get("old_name"),
                             note="anterior (guardada antes de cambiar)", version=meta.get("old_version"))
        self.s.save_copy(data, target=target, name=tx["name"], version=tx["version"], source=tx["source"],
                         note="nueva")
        tx["state"] = "preparado"
        self.s.jput(tx)
        self.s.log("instalar_inicio", tx=txid, destino=target, sha_nuevo=new_h, sha_anterior=tx["old_sha"],
                   version=tx["version"], nombre=tx["name"], clase=c["level"], temporal=tx["staged"])
        self.hook("preparado", tx)
        try:
            ftp_mkdirs(f, sdir)
            f.storbinary("STOR " + tx["staged"], io.BytesIO(data))
            self.hook("subido", tx)
            got = ftp_hash(f, tx["staged"])
            if not got or got[0] != new_h:
                ftp_try_delete(f, tx["staged"])
                ftp_try_rmd(f, sdir)
                self.s.jdel(txid)
                self.s.log("instalar", resultado="error", tx=txid, error="copia temporal distinta (%s)" %
                           ("no existe" if not got else "%d bytes, sha %s" % (got[1], got[0][:16])))
                raise SafeError("Lo que ha llegado a la consola no coincide con el original (archivo cortado o "
                                "dañado por el camino). No he cambiado nada.")
            tx["state"] = "subido"
            self.s.jput(tx)
            self.hook("antes_de_cambiar", tx)
            tx["state"] = "cambiando"
            tx["mode"] = "directo"
            self.s.jput(tx)
            try:
                f.rename(tx["staged"], target)          # rename encima: atomico en el mismo volumen
            except ftplib.error_perm as e:
                if tx["old_sha"] is None:
                    raise SafeError("El servidor FTP no deja renombrar (%s). No he cambiado nada." % e)
                # algunos servidores no renombran encima de algo que existe: dos pasos con vuelta atras
                tx["mode"] = "dos_pasos"
                self.s.jput(tx)
                try:
                    f.rename(target, tx["aside"])
                except ftplib.error_perm as e2:
                    self._cleanup(f, tx)
                    self.s.jdel(txid)
                    self.s.log("instalar", resultado="sin_cambios", tx=txid, error="rename: %s / %s" % (e, e2))
                    raise SafeError("El servidor FTP no permite cambiar el archivo con rename (%s). He dejado la "
                                    "versión anterior tal cual; no escribo encima." % e2)
                self.hook("apartado", tx)
                try:
                    f.rename(tx["staged"], target)
                except ftplib.error_perm as e3:
                    f.rename(tx["aside"], target)
                    self._cleanup(f, tx)
                    self.s.jdel(txid)
                    self.s.log("instalar", resultado="deshecho", tx=txid, error="rename: %s" % e3)
                    raise SafeError("No he podido poner la versión nueva (%s): he vuelto a dejar la anterior." % e3)
            self.hook("cambiado", tx)
            got = ftp_hash(f, target)
            if not got or got[0] != new_h:
                self.s.log("instalar", aviso="comprobación final distinta: deshago", tx=txid,
                           sha_leido=got and got[0])
                self._restore_old(f, tx)
                self.s.jdel(txid)
                raise SafeError("La comprobación final no coincide: he vuelto a la versión anterior.")
            tx["state"] = "aplicado"
            self.s.jput(tx)
            self._cleanup(f, tx)
            self.s.jdel(txid)
            self.s.log("instalar", resultado="ok", tx=txid, destino=target, sha256=new_h, tamano=len(data),
                       version=tx["version"], nombre=tx["name"], modo=tx.get("mode"), anterior=tx["old_sha"])
            self._close(f)
            return {"ok": True, "target": target, "sha256": new_h, "old_sha256": tx["old_sha"], "mode": tx["mode"]}
        except SafeError:
            self._close(f)
            raise
        except FTP_ERRORS as e:
            self._close(f)
            self.s.log("instalar", resultado="cortado", tx=txid, estado=tx["state"], error=str(e))
            try:
                r = self.recover(only=txid)
            except Exception as e2:
                r = {"error": str(e2)}
            raise Interrupted("Se cortó la conexión a mitad (%s). %s" % (e, _recover_text(r, txid)))

    def _restore_old(self, f, tx):
        """Deja otra vez la version anterior en el destino (desde la copia apartada en la consola o desde la del PC)."""
        target = tx["target"]
        if tx.get("old_sha") is None:
            ftp_try_delete(f, target)
            self._cleanup(f, tx)
            return "borrado"
        got = ftp_hash(f, tx["aside"]) if tx.get("mode") == "dos_pasos" else None
        if got and got[0] == tx["old_sha"]:
            f.rename(tx["aside"], target)
        else:
            old = self.s.load_copy(tx["old_sha"])
            ftp_mkdirs(f, tx["stage"])
            back = tx["stage"] + "/vuelta.elf"
            f.storbinary("STOR " + back, io.BytesIO(old))
            g2 = ftp_hash(f, back)
            if not g2 or g2[0] != tx["old_sha"]:
                raise SafeError("No he podido volver a subir la versión anterior: está guardada en el PC")
            f.rename(back, target)
        got = ftp_hash(f, target)
        if not got or got[0] != tx["old_sha"]:
            raise SafeError("No he podido comprobar la versión anterior en %s" % target)
        self._cleanup(f, tx)
        self.s.log("deshacer", resultado="ok", tx=tx["id"], destino=target, sha256=tx["old_sha"])
        return "anterior"

    def _cleanup(self, f, tx):
        for p in (tx.get("staged"), tx.get("aside"), (tx.get("stage") or "") + "/vuelta.elf"):
            if p:
                try:
                    f.delete(p)
                except FTP_ERRORS:
                    pass
        if tx.get("stage"):
            ftp_try_rmd(f, tx["stage"])

    @staticmethod
    def _close(f):
        try:
            f.quit()
        except Exception:
            try:
                f.close()
            except Exception:
                pass

    # recuperar ---------------------------------------------------------------
    def recover(self, only=None, only_kind=None, pm=None):
        """Termina o deshace lo que se quedo a medias. Nunca vuelve a ejecutar nada.
        Devuelve {txid: resultado}."""
        res = {}
        for txid, tx in sorted(self.s.journal().items()):
            if only and txid != only:
                continue
            if only_kind and tx.get("kind") != only_kind:
                continue
            try:
                if tx["kind"] == "ftp":
                    res[txid] = self._recover_ftp(tx)
                elif tx["kind"] == "config" and pm is not None:
                    res[txid] = self._recover_config(tx, pm)
                elif tx["kind"] == "pm" and pm is not None:
                    res[txid] = self._recover_pm(tx, pm)
            except Exception as e:
                res[txid] = "pendiente: %s" % e
                self.s.log("recuperar", tx=txid, resultado="pendiente", error=str(e))
        return res

    def _recover_ftp(self, tx):
        f = self.connect()
        try:
            cur = ftp_hash(f, tx["target"])
            cur_h = cur[0] if cur else None
            if cur_h == tx["new_sha"] and tx["state"] in ("subido", "cambiando", "aplicado"):
                self._cleanup(f, tx)
                self.s.jdel(tx["id"])
                self.s.log("recuperar", tx=tx["id"], resultado="terminado", destino=tx["target"], sha256=cur_h)
                return "terminado (la versión nueva ya estaba puesta y comprobada)"
            if cur_h == tx.get("old_sha"):
                self._cleanup(f, tx)
                self.s.jdel(tx["id"])
                self.s.log("recuperar", tx=tx["id"], resultado="sin_cambios", destino=tx["target"])
                return "sin cambios (sigue la versión anterior)"
            if cur_h is None or cur_h == tx["new_sha"]:
                # falta el archivo (cortado entre los dos rename) o la nueva sin estado claro: se vuelve a la anterior
                r = self._restore_old(f, tx)
                self.s.jdel(tx["id"])
                self.s.log("recuperar", tx=tx["id"], resultado="deshecho", destino=tx["target"], como=r)
                return "deshecho (vuelve a estar la versión anterior)" if r == "anterior" else "deshecho"
            tx["state"] = "conflicto"
            tx["found_sha"] = cur_h
            self.s.jput(tx)
            self.s.log("recuperar", tx=tx["id"], resultado="conflicto", destino=tx["target"], sha_encontrado=cur_h)
            return "conflicto: en %s hay otro archivo distinto; no lo toco" % tx["target"]
        finally:
            self._close(f)

    # Payload Manager ---------------------------------------------------------
    def pm_install(self, pm, filename, data, meta=None, allow_experimental=False, fw="", no_ftp_ok=False):
        """Sube a Payload Manager con un nombre nuevo (nunca reemplaza el anterior, que se queda para volver) y
        comprueba el archivo en la consola leyendo su ruta por FTP. No toca la carga automatica."""
        meta = dict(meta or {})
        chk, c = self.preflight(data, meta.get("name") or filename, filename, meta.get("version") or "", fw,
                                allow_experimental)
        new_h = sha256(data)
        if not self.op_lock.acquire(blocking=False):
            raise SafeError("Ya hay otra operación de payloads en marcha")
        try:
            self.recover(pm=pm)
            items = pm.payloads()
            hit = next((x for x in items if x.get("filename") == filename), None)
            if hit:
                try:
                    got = self._pm_hash(hit)
                except FTP_ERRORS:
                    if not no_ftp_ok:
                        raise
                    got = None                       # sin FTP no se puede comparar: se sube con otro nombre
                if got and got[0] == new_h:
                    self.s.log("instalar_pm", resultado="ya_estaba", archivo=filename, sha256=new_h, slot=hit.get("slot"))
                    return {"ok": True, "same": True, "slot": hit.get("slot"), "sha256": new_h, "path": hit.get("path")}
                stem, ext = os.path.splitext(filename)
                filename = "%s_%s%s" % (stem, new_h[:8], ext or ".elf")
            txid = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
            tx = {"id": txid, "kind": "pm", "filename": filename, "new_sha": new_h, "state": "preparado",
                  "name": meta.get("name") or "", "version": meta.get("version") or "", "class": c["level"],
                  "started": time.time(), "no_ftp_ok": bool(no_ftp_ok)}
            self.s.save_copy(data, name=tx["name"], version=tx["version"], source=meta.get("source"),
                             filename=filename, note="subida a Payload Manager")
            self.s.jput(tx)
            self.s.log("instalar_pm_inicio", tx=txid, archivo=filename, sha256=new_h, version=tx["version"],
                       clase=c["level"])
            self.hook("pm_preparado", tx)
            try:
                pm.upload(filename, data)
            except Exception as e:
                self.s.log("instalar_pm", tx=txid, resultado="cortado", error=str(e))
                r = self._recover_pm(tx, pm, quiet=True)
                if r.startswith("terminado"):
                    hit = next((x for x in pm.payloads() if x.get("filename") == filename), None)
                    return {"ok": True, "slot": hit and hit.get("slot"), "sha256": new_h, "filename": filename,
                            "path": hit and hit.get("path")}
                raise SafeError("Payload Manager no ha guardado el payload (%s). %s" % (e, r))
            self.hook("pm_subido", tx)
            r = self._recover_pm(tx, pm, quiet=True)
            hit = next((x for x in pm.payloads() if x.get("filename") == filename), None)
            if not r.startswith("terminado"):
                raise SafeError("La comprobación del payload en la consola ha fallado: %s" % r)
            self.s.log("instalar_pm", resultado="ok", tx=txid, archivo=filename, slot=hit and hit.get("slot"),
                       ruta=hit and hit.get("path"), sha256=new_h, version=tx["version"], nombre=tx["name"])
            return {"ok": True, "slot": hit and hit.get("slot"), "sha256": new_h, "filename": filename,
                    "path": hit and hit.get("path")}
        finally:
            self.op_lock.release()

    def _pm_hash(self, item):
        p = item.get("path")
        if not p or not self.connect:
            return None
        f = self.connect()
        try:
            return ftp_hash(f, p)
        finally:
            self._close(f)

    def _recover_pm(self, tx, pm, quiet=False):
        items = pm.payloads()
        hit = next((x for x in items if x.get("filename") == tx["filename"]), None)
        if not hit:
            self.s.jdel(tx["id"])
            self.s.log("recuperar", tx=tx["id"], resultado="sin_cambios", archivo=tx["filename"])
            return "sin cambios (Payload Manager no lo tiene)"
        try:
            got = self._pm_hash(hit)
        except FTP_ERRORS as e:
            if tx.get("no_ftp_ok"):
                # consola recién restaurada: todavía no hay FTP (es justo lo que se está instalando). Payload Manager
                # comprueba su propia escritura y ya lo tiene en su lista: vale así
                self.s.jdel(tx["id"])
                self.s.log("comprobar_pm", tx=tx["id"], resultado="sin_ftp", archivo=tx["filename"], error=str(e))
                return "terminado (sin FTP para leerlo de vuelta)"
            if quiet:
                raise SafeError("no he podido leerlo por FTP para comprobarlo (%s)" % e)
            raise
        if got is None and not hit.get("path"):
            # sin ruta que leer: Payload Manager ya comprueba su propia escritura (sha256 interno)
            self.s.jdel(tx["id"])
            self.s.log("recuperar", tx=tx["id"], resultado="terminado_sin_leer", archivo=tx["filename"])
            return "terminado (sin poder leerlo de vuelta)"
        if got and got[0] == tx["new_sha"]:
            self.s.jdel(tx["id"])
            self.s.log("comprobar_pm", tx=tx["id"], resultado="ok", archivo=tx["filename"], ruta=hit.get("path"),
                       sha256=got[0])
            return "terminado"
        # está pero distinto: se quita para que nadie lo cargue
        try:
            pm.delete(hit.get("slot"))
            gone = True
        except Exception:
            gone = False
        self.s.jdel(tx["id"])
        self.s.log("comprobar_pm", tx=tx["id"], resultado="distinto", archivo=tx["filename"], quitado=gone,
                   sha_leido=got and got[0])
        return "lo guardado en la consola no coincide con el original; %s" % (
            "lo he quitado de Payload Manager" if gone else "quítalo de Payload Manager, no lo cargues")

    # configuracion (lista de carga automatica) -----------------------------
    def pm_set_autoload(self, pm, new_list, reason="", enabled=None, raw_reader=None, allow_experimental=False,
                        fw=""):
        """Cambia la lista de carga automatica con copia previa, comprobacion y vuelta atras si no queda igual."""
        conf = pm.config()
        old_list = conf.get("AUTOLOAD_LIST") or ""
        new_entries = parse_autoload(new_list)
        old_keys = {e["key"] for e in parse_autoload(old_list)}
        items = {x.get("slot"): x for x in pm.payloads() if x.get("slot")}
        added = [e["key"] for e in new_entries if e["delay"] is None and e["key"] not in old_keys]
        for k in added:
            it = items.get(k) or {}
            c = classify(it.get("name") or "", it.get("version") or version_of(it.get("filename") or ""),
                         it.get("filename") or k, fw)
            if c["experimental"] and not allow_experimental:
                raise NeedsConfirm(c)
            if c["critical"]:
                b = self.busy()
                if b:
                    raise SafeError("Espera a que termine lo que está en marcha (%s) antes de cambiar la carga "
                                    "automática de %s." % (b, c["label"]))
        raw = None
        if raw_reader:
            try:
                raw = raw_reader()
            except Exception:
                raw = None
        backup = self.s.save_config("antes", conf, raw)
        new_list = join_autoload(new_entries)
        txid = time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        en = bool(conf.get("AUTOLOAD_ENABLED", True)) if enabled is None else bool(enabled)
        tx = {"id": txid, "kind": "config", "old": old_list, "new": new_list, "old_enabled":
              bool(conf.get("AUTOLOAD_ENABLED", True)), "new_enabled": en, "backup": backup, "state": "preparado",
              "reason": reason, "started": time.time()}
        self.s.jput(tx)
        self.s.log("autoload_inicio", tx=txid, antes=old_list, despues=new_list, motivo=reason, copia=backup)
        self.hook("cfg_preparado", tx)
        try:
            pm.set_config({"AUTOLOAD_LIST": new_list, "AUTOLOAD_ENABLED": en})
        except Exception as e:
            r = self._recover_config(tx, pm)
            if r.startswith("terminado"):         # se corto la respuesta pero la lista quedo bien guardada
                return {"ok": True, "list": new_list, "backup": backup}
            raise SafeError("Payload Manager no ha guardado la lista (%s). %s" % (e, r))
        self.hook("cfg_guardado", tx)
        r = self._recover_config(tx, pm)
        if not r.startswith("terminado"):
            raise SafeError("La lista no ha quedado como debía: %s" % r)
        return {"ok": True, "list": new_list, "backup": backup}

    def _recover_config(self, tx, pm):
        conf = pm.config()
        cur = join_autoload(parse_autoload(conf.get("AUTOLOAD_LIST") or ""))
        if cur == tx["new"]:
            self.s.jdel(tx["id"])
            self.s.log("autoload", tx=tx["id"], resultado="ok", lista=cur)
            return "terminado"
        if cur == join_autoload(parse_autoload(tx["old"])):
            self.s.jdel(tx["id"])
            self.s.log("autoload", tx=tx["id"], resultado="sin_cambios", lista=cur)
            return "sin cambios (sigue la lista anterior)"
        # quedo otra cosa: se vuelve a la anterior y se comprueba
        try:
            pm.set_config({"AUTOLOAD_LIST": tx["old"], "AUTOLOAD_ENABLED": tx["old_enabled"]})
            back = join_autoload(parse_autoload(pm.config().get("AUTOLOAD_LIST") or ""))
        except Exception as e:
            self.s.log("autoload", tx=tx["id"], resultado="pendiente", error=str(e))
            raise SafeError("no he podido volver a la lista anterior (%s); está copiada en el PC (%s)" %
                            (e, tx["backup"]))
        self.s.jdel(tx["id"])
        ok = back == join_autoload(parse_autoload(tx["old"]))
        self.s.log("autoload", tx=tx["id"], resultado="deshecho" if ok else "conflicto", lista=back)
        return "deshecho (vuelve a estar la lista anterior)" if ok else "conflicto: revisa la lista a mano"

    def pm_restore_config(self, pm, name):
        j = self.s.load_config(name)
        conf = j.get("config") or {}
        return self.pm_set_autoload(pm, conf.get("AUTOLOAD_LIST") or "", reason="restaurar " + name,
                                    enabled=conf.get("AUTOLOAD_ENABLED", True), allow_experimental=True)

    # cargar (ejecutar) --------------------------------------------------------
    def launch(self, key, fn, meta=None, force=False, auto=False):
        """Ejecuta un payload una sola vez. fn() devuelve "ok" (confirmado), "not_sent" (no llego a enviarse:
        se puede repetir) o lanza excepcion; si la excepcion trae .unknown o es un corte despues de enviar, el
        resultado es «desconocido» y NO se vuelve a ejecutar solo: hace falta confirmarlo a mano."""
        meta = dict(meta or {})
        prev = self.s.launches().get(key)
        if prev and prev.get("outcome") == "unknown" and (auto or not force):
            raise UnknownLaunch(prev)
        c = classify(meta.get("name") or key, meta.get("version") or "", meta.get("filename") or "", meta.get("fw"))
        if c["critical"]:
            b = self.busy()
            if b:
                raise SafeError("Espera a que termine lo que está en marcha (%s) antes de cargar %s: es un payload "
                                "crítico." % (b, c["label"]))
        if c["experimental"] and auto:
            raise NeedsConfirm(c)
        kp = self.is_kpatch(c)
        boot = None
        if kp:
            try:
                boot = self.boot_epoch() if self.boot_epoch else None
            except Exception:
                boot = None
            prev_k = self.kpatch_same_boot(boot)
            if prev_k and (auto or not force):
                raise SameBoot(prev_k, c)
        self.s.log("cargar_inicio", clave=key, nombre=meta.get("name"), version=meta.get("version"),
                   sha256=meta.get("sha256"), forzado=bool(force) or None, automatico=bool(auto) or None)
        try:
            r = fn()
        except Exception as e:
            unknown = bool(getattr(e, "unknown", False))
            if unknown and kp:
                self.kpatch_note(c, boot)           # no se sabe si se cargó: cuenta como cargado
            if unknown:
                self.s.set_launch(key, outcome="unknown", error=str(e), name=meta.get("name"))
                self.s.log("cargar", clave=key, resultado="desconocido", error=str(e))
                raise UnknownLaunch({"outcome": "unknown", "error": str(e), "name": meta.get("name")})
            self.s.set_launch(key, clear=True)
            self.s.log("cargar", clave=key, resultado="error", error=str(e))
            raise
        if kp:
            self.kpatch_note(c, boot)
        if r == "unknown":
            self.s.set_launch(key, outcome="unknown", name=meta.get("name"))
            self.s.log("cargar", clave=key, resultado="desconocido")
            return "unknown"
        self.s.set_launch(key, clear=True)
        self.s.log("cargar", clave=key, resultado=r or "ok")
        return r or "ok"

    # parches del sistema: una vez por arranque -------------------------------
    # Cargar kstuff (o etaHEN, HyperHEN…) encima de uno que ya está cargado pone una segunda copia sobre la primera y
    # puede colgar la consola (PS5Upload lo ha visto: de 2 a 8 kstuff a la vez en una consola). La lista de procesos no
    # siempre lo enseña (hay cargadores que lo llaman todo «payload.elf»), así que se apunta cada carga y no se repite
    # en el mismo arranque si hay prueba de que es el mismo: la consola se encendió antes de esa carga (su tiempo
    # encendida), o PS5 Directo la ha estado viendo desde entonces sin que se apagase.
    SESSION = "%x-%x" % (os.getpid(), int(time.time()))
    BOOT_SLACK = 120

    @staticmethod
    def is_kpatch(c):
        m = next((x for x in MATRIX if x["family"] == (c or {}).get("family")), None) or {}
        return m.get("group") == "kpatch" or bool(m.get("hen"))

    def kpatch_same_boot(self, boot_epoch=None):
        """La carga de un parche del sistema de este mismo arranque, si la hay (o None)."""
        for r in reversed(_load_json(os.path.join(self.s.root, "parches_sistema.json"), [])):
            if boot_epoch:
                if r.get("t", 0) > boot_epoch - self.BOOT_SLACK:
                    return r
            elif r.get("session") == self.SESSION:
                return r
        return None

    def kpatch_note(self, c, boot_epoch=None):
        path = os.path.join(self.s.root, "parches_sistema.json")
        with self.s.lock:
            d = _load_json(path, [])[-20:]
            d.append({"family": c.get("family"), "label": c.get("label"), "t": time.time(), "session": self.SESSION,
                      "boot": boot_epoch})
            _atomic_json(path, d)

    def kpatch_clear(self):
        with self.s.lock:
            _atomic_json(os.path.join(self.s.root, "parches_sistema.json"), [])

    def launch_blocked(self, key):
        p = self.s.launches().get(key)
        return bool(p and p.get("outcome") == "unknown")

    def clear_launch(self, key=None, why=""):
        if key:
            self.s.set_launch(key, clear=True)
        else:
            for k in list(self.s.launches()):
                self.s.set_launch(k, clear=True)
            self.kpatch_clear()                     # la consola se apagó o reinició: ya no queda nada cargado
        self.s.log("cargar_desbloquear", clave=key or "*", motivo=why)


class SameBoot(SafeError):
    def __init__(self, prev, c):
        self.prev, self.c = prev, c
        when = time.strftime("%H:%M", time.localtime(prev.get("t") or time.time()))
        SafeError.__init__(self, "Ya se cargó %s en este arranque de la consola (a las %s). Cargar otro parche del "
                                 "sistema encima (%s) puede colgarla: reinicia la consola antes." % (
                                     prev.get("label") or "un parche del sistema", when, c.get("label") or "?"))


class NeedsConfirm(SafeError):
    def __init__(self, c):
        self.c = c
        SafeError.__init__(self, "«%s» es una versión de pruebas: no la activo sola. Confírmalo si de verdad quieres "
                                 "usarla (puede colgar la consola). Antes, haz una copia en «Copia de seguridad»." % (c.get("label") or "?"))


class UnknownLaunch(SafeError):
    def __init__(self, prev):
        self.prev = prev
        SafeError.__init__(self, "La última vez no se supo si %s llegó a cargarse (%s). No lo vuelvo a cargar solo: "
                                 "comprueba en la consola si está en marcha y, si no, cárgalo a mano." %
                           (prev.get("name") or "el payload", prev.get("error") or "sin respuesta"))


def _recover_text(r, txid):
    v = (r or {}).get(txid) if isinstance(r, dict) else None
    if not v:
        return "Queda anotado para recuperarlo en cuanto la consola responda."
    return "Recuperación: %s." % v
