"""Funciones extra de PS5 Directo (desde la 1.20).

- Payloads: descarga ftpsrv/BFpilot de GitHub y los manda al cargador de ELF (puerto 9021).
- Biblioteca: lee param.json, icon0.png y eboot.bin de lo que hay en /data/homebrew.
- Firmware: que firmware pide un juego (param.json y el SDK de sus ejecutables).
- Sumas de comprobacion (.sfv/.md5/.sha1/.sha256) calculadas mientras se envia.
- Diagnostico de red del PC (por que va lento).
- Codigo QR para abrir la app desde el movil.
- Mantener el PC despierto y apagar/suspender al terminar la cola.
- PS5 Dump Forge (https://github.com/quer3q/ps5-dump-forge, GPL-3): se usa su programa de linea de
  comandos tal cual, como 7-Zip, para crear imagenes .ffpkg/.ffpfsc o empaquetar en LZ4.

Solo biblioteca estandar de Python.
"""

import ftplib
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

import engine as E

MiB = E.MiB
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _ua():
    return {"User-Agent": "PS5Directo", "Accept": "application/vnd.github+json"}


# ---------------------------------------------------------------------------
# Codigo QR (modo byte, versiones 1-10). Escrito a mano para no depender de nada.
# ---------------------------------------------------------------------------

# por nivel: (codewords de correccion por bloque, numero de bloques) para las versiones 1..10
_QR_ECC = {
    "L": ([7, 10, 15, 20, 26, 18, 20, 24, 30, 18], [1, 1, 1, 1, 1, 2, 2, 2, 2, 4]),
    "M": ([10, 16, 26, 18, 24, 16, 18, 22, 22, 26], [1, 1, 1, 2, 2, 4, 4, 4, 5, 5]),
}
_QR_FMT = {"L": 1, "M": 0}

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a, b):
    return 0 if a == 0 or b == 0 else _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_divisor(degree):
    res = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            res[j] = _gf_mul(res[j], root)
            if j + 1 < degree:
                res[j] ^= res[j + 1]
        root = _gf_mul(root, 2)
    return res


def _rs_remainder(data, divisor):
    res = [0] * len(divisor)
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i, c in enumerate(divisor):
            res[i] ^= _gf_mul(c, factor)
    return res


def _qr_raw_modules(ver):
    r = (16 * ver + 128) * ver + 64
    if ver >= 2:
        na = ver // 7 + 2
        r -= (25 * na - 10) * na - 55
        if ver >= 7:
            r -= 36
    return r


def _qr_data_cw(ver, lvl):
    ecc, blocks = _QR_ECC[lvl][0][ver - 1], _QR_ECC[lvl][1][ver - 1]
    return _qr_raw_modules(ver) // 8 - ecc * blocks


def _qr_align(ver):
    if ver == 1:
        return []
    na = ver // 7 + 2
    size = ver * 4 + 17
    step = (ver * 4 + na * 2 + 1) // (na * 2 - 2) * 2
    out = []
    pos = size - 7
    for _ in range(na - 1):
        out.insert(0, pos)
        pos -= step
    out.insert(0, 6)
    return out


def qr_matrix(text, lvl="M"):
    """Matriz (lista de filas de bool) del codigo QR de un texto (hasta ~200 bytes)."""
    data = text.encode("utf-8")
    ver = None
    for v in range(1, 11):
        cc = 8 if v < 10 else 16
        if 4 + cc + 8 * len(data) <= _qr_data_cw(v, lvl) * 8:
            ver = v
            break
    if ver is None:
        raise ValueError("texto demasiado largo para el QR")
    cap = _qr_data_cw(ver, lvl)
    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)
    put(0b0100, 4)
    put(len(data), 8 if ver < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, cap * 8 - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < cap * 8:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    cw = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    # bloques y correccion de errores, intercalados
    ecc_len, nblocks = _QR_ECC[lvl][0][ver - 1], _QR_ECC[lvl][1][ver - 1]
    raw_cw = _qr_raw_modules(ver) // 8
    nshort = nblocks - raw_cw % nblocks
    short_len = raw_cw // nblocks
    div = _rs_divisor(ecc_len)
    blocks = []
    k = 0
    for i in range(nblocks):
        dl = short_len - ecc_len + (0 if i < nshort else 1)
        dat = cw[k:k + dl]
        k += dl
        ecc = _rs_remainder(dat, div)
        if i < nshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    final = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= nshort:
                final.append(blk[i])

    size = ver * 4 + 17
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, dark):
        mod[y][x] = dark
        fn[y][x] = True

    for i in range(size):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                xx, yy = cx + dx, cy + dy
                if 0 <= xx < size and 0 <= yy < size:
                    setf(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))
    al = _qr_align(ver)
    n = len(al)
    for i in range(n):
        for j in range(n):
            if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(al[i] + dx, al[j] + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask):
        d = _QR_FMT[lvl] << 3 | mask
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        b = (d << 10 | rem) ^ 0x5412
        bit = lambda i: (b >> i) & 1 == 1  # noqa: E731
        for i in range(6):
            setf(8, i, bit(i))
        setf(8, 7, bit(6))
        setf(8, 8, bit(7))
        setf(7, 8, bit(8))
        for i in range(9, 15):
            setf(14 - i, 8, bit(i))
        for i in range(8):
            setf(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            setf(8, size - 15 + i, bit(i))
        setf(8, size - 8, True)

    draw_format(0)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        b = ver << 12 | rem
        for i in range(18):
            dark = (b >> i) & 1 == 1
            a, c = size - 11 + i % 3, i // 3
            setf(a, c, dark)
            setf(c, a, dark)

    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not fn[y][x] and i < len(final) * 8:
                    mod[y][x] = (final[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2

    masks = [
        lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
        lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
        lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
        lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
    ]

    def apply(m):
        f = masks[m]
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and f(x, y):
                    mod[y][x] = not mod[y][x]

    def penalty():
        p = 0
        lines = [row for row in mod] + [[mod[y][x] for y in range(size)] for x in range(size)]
        for ln in lines:
            run, prev = 0, None
            for v in ln:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        p += 3 + run - 5
                    run, prev = 1, v
            if run >= 5:
                p += 3 + run - 5
            s = "".join("1" if v else "0" for v in ln)
            p += 40 * (s.count("10111010000") + s.count("00001011101"))
        for y in range(size - 1):
            for x in range(size - 1):
                v = mod[y][x]
                if v == mod[y][x + 1] == mod[y + 1][x] == mod[y + 1][x + 1]:
                    p += 3
        dark = sum(sum(1 for v in row if v) for row in mod)
        total = size * size
        p += 10 * (abs(dark * 20 - total * 10) // total)
        return p

    best, best_p = 0, None
    for m in range(8):
        apply(m)
        draw_format(m)
        pen = penalty()
        if best_p is None or pen < best_p:
            best, best_p = m, pen
        apply(m)
    apply(best)
    draw_format(best)
    return mod


def qr_svg(text, scale=6, border=4):
    m = qr_matrix(text)
    n = len(m)
    path = []
    for y, row in enumerate(m):
        for x, v in enumerate(row):
            if v:
                path.append("M%d %dh1v1h-1z" % (x + border, y + border))
    dim = n + 2 * border
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
            'shape-rendering="crispEdges"><rect width="100%%" height="100%%" fill="#fff"/>'
            '<path d="%s" fill="#000"/></svg>' % (dim, dim, dim * scale, dim * scale, "".join(path)))


def lan_ip(target=None):
    """IP del PC en la red local (la que usaria para llegar a la PS5)."""
    for t in ([target] if target else []) + ["192.168.1.1", "8.8.8.8"]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((t, 9))
                ip = s.getsockname()[0]
            finally:
                s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Sumas de comprobacion (.sfv / .md5 / .sha1 / .sha256)
# ---------------------------------------------------------------------------

CHECKSUM_RE = re.compile(r"\.(sfv|md5|sha1|sha256)$", re.I)


def is_checksum_name(name):
    return bool(CHECKSUM_RE.search(name or ""))


def parse_checksums(text, fname=""):
    """{nombre_en_minusculas: (algoritmo, hex)} de un .sfv, .md5, .sha1 o .sha256."""
    out = {}
    ext = (CHECKSUM_RE.search(fname or "") or [None, ""])[1].lower() if fname else ""
    for line in (text or "").splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line[0] in ";#":
            continue
        m = re.match(r"^(MD5|SHA1|SHA256)\s*\((.+)\)\s*=\s*([0-9A-Fa-f]+)$", line)
        if m:
            out[posixpath.basename(m.group(2).replace("\\", "/")).lower()] = (m.group(1).lower(), m.group(3).lower())
            continue
        m = re.match(r"^([0-9A-Fa-f]{32}|[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})\s+\*?(.+)$", line)
        if m and ext != "sfv":
            h = m.group(1).lower()
            algo = {32: "md5", 40: "sha1", 64: "sha256"}[len(h)]
            out[posixpath.basename(m.group(2).strip().replace("\\", "/")).lower()] = (algo, h)
            continue
        m = re.match(r"^(.+?)\s+([0-9A-Fa-f]{8})$", line)
        if m:
            out[posixpath.basename(m.group(1).strip().replace("\\", "/")).lower()] = ("crc32", m.group(2).lower())
    return out


class StreamHasher:
    """Calcula la suma de un archivo mientras se envia, aunque el envio se reanude.

    Los reintentos vuelven a pedir el flujo desde lo que ya tiene la consola: si es antes de
    lo ya sumado, esos bytes se saltan (son los mismos); si hay un hueco, ya no se puede
    comprobar (valid=False)."""

    def __init__(self, algo, start=0):
        self.algo = algo
        self.pos = 0
        self.valid = start == 0
        self.crc = 0
        self.h = None if algo == "crc32" else hashlib.new(algo)

    def _update(self, b):
        if self.h is None:
            self.crc = zlib.crc32(b, self.crc)
        else:
            self.h.update(b)

    def wrap(self, it, offset):
        skip = 0
        if self.valid:
            if offset > self.pos:
                self.valid = False
            else:
                skip = self.pos - offset
        for data in it:
            if self.valid:
                if skip:
                    if len(data) <= skip:
                        skip -= len(data)
                        yield data
                        continue
                    part = data[skip:]
                    skip = 0
                else:
                    part = data
                self._update(part)
                self.pos += len(part)
            yield data

    def hexdigest(self):
        return "%08x" % (self.crc & 0xFFFFFFFF) if self.h is None else self.h.hexdigest()


# ---------------------------------------------------------------------------
# Firmware que pide un juego
# ---------------------------------------------------------------------------

def bcd(word):
    """'0x1160000000000000' -> '11.60'; '0x0700...' -> '7.00'. None si no lo es."""
    if not isinstance(word, str):
        return None
    w = word.strip()
    if not w.lower().startswith("0x"):
        return None
    h = w[2:]
    width = 8 if len(h) <= 8 else 16
    h = h.rjust(width, "0")
    if len(h) != width or not re.match(r"^[0-9A-Fa-f]+$", h) or not h[:4].isdigit():
        return None
    major = h[:2].lstrip("0") or "0"
    return "%s.%s" % (major, h[2:4])


def fw_tuple(s):
    m = re.match(r"^\s*(\d{1,2})[.,](\d{1,2})", str(s or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2).ljust(2, "0"))


def fw_ok(console_fw, need_fw):
    """True si vale, False si pide uno mas nuevo, None si no se sabe."""
    a, b = fw_tuple(console_fw), fw_tuple(need_fw)
    if not a or not b:
        return None
    return b <= a


def _sdk_fw(sdk):
    major, minor = (sdk >> 24) & 0xFF, (sdk >> 16) & 0xFF
    ok = lambda b: (b >> 4) <= 9 and (b & 0xF) <= 9  # noqa: E731
    if sdk == 0 or not ok(major) or not ok(minor):
        return None
    return "%x.%02x" % (major, minor)


_ELF = b"\x7fELF"
_SELF_PS5 = b"\x54\x14\xF5\xEE"
_SELF_PS4 = b"\x4F\x15\x3D\x1D"
_PROCPARAM, _MODPARAM = 0x61000001, 0x61000002
_PROC_MAGIC, _MOD_MAGIC = 0x4942524F, 0x3C13F4BF


_DYNLIBDATA = 0x61000000


def exe_uses_ampr(read_range):
    """¿El ejecutable importa libSceAmpr (lee sus datos con AMPR)? True/False, o None si no se puede leer
    (cifrado o formato raro). Lo que importa un ELF de PS4/PS5 esta en su segmento SCE_DYNLIBDATA."""
    import struct
    head = read_range(0, 0x10000)
    if len(head) < 0x40:
        return None
    u16 = lambda a: struct.unpack_from("<H", head, a)[0] if a + 2 <= len(head) else None  # noqa: E731
    u32 = lambda a: struct.unpack_from("<I", head, a)[0] if a + 4 <= len(head) else None  # noqa: E731
    u64 = lambda a: struct.unpack_from("<Q", head, a)[0] if a + 8 <= len(head) else None  # noqa: E731
    magic = head[:4]
    if magic in (_SELF_PS5, _SELF_PS4):
        nseg = u16(0x18) or 0
        elf, segments = 0x20 + nseg * 0x20, nseg
    elif magic == _ELF:
        elf, segments = 0, 0
    else:
        return None
    if head[elf:elf + 4] != _ELF:
        return None
    phoff, phentsize, phnum = u64(elf + 0x20), u16(elf + 0x36), u16(elf + 0x38)
    if None in (phoff, phentsize, phnum):
        return None
    hit = None
    for i in range(phnum):
        ph = elf + phoff + i * phentsize
        t, off, sz = u32(ph), u64(ph + 8), u64(ph + 0x20)
        if None in (t, off, sz):
            return None
        if t == _DYNLIBDATA:
            hit = (i, off, sz)
    if not hit or not hit[2]:
        return None
    seg, at, size = hit
    if segments:          # fSELF: buscar donde esta ese segmento dentro del archivo (sin cifrar)
        at = None
        for k in range(segments):
            e = 0x20 + k * 0x20
            props, s_off, s_size = u64(e), u64(e + 8), u64(e + 0x10)
            if props is None:
                continue
            plain = props & ((1 << 1) | (1 << 3)) == 0
            if plain and (props >> 20) & 0xFFFF == seg:
                at, size = s_off, s_size
                break
        if at is None:
            return None
    data = read_range(at, min(size, 32 * 1024 * 1024))
    if not data:
        return None
    return b"libSceAmpr" in data


def exe_firmware(read_range):
    """Firmware minimo que pide un ejecutable (el SDK de su process/module param), como '7.00'.
    read_range(offset, n) -> bytes. Mismo formato que leen los backports y PS5 Dump Forge.
    None si no es un ELF o fSELF legible (por ejemplo, si esta cifrado)."""
    import struct
    head = read_range(0, 0x10000)
    if len(head) < 0x40:
        return None
    u16 = lambda a: struct.unpack_from("<H", head, a)[0] if a + 2 <= len(head) else None  # noqa: E731
    u32 = lambda a: struct.unpack_from("<I", head, a)[0] if a + 4 <= len(head) else None  # noqa: E731
    u64 = lambda a: struct.unpack_from("<Q", head, a)[0] if a + 8 <= len(head) else None  # noqa: E731
    magic = head[:4]
    if magic in (_SELF_PS5, _SELF_PS4):
        nseg = u16(0x18) or 0
        elf, segments = 0x20 + nseg * 0x20, nseg
    elif magic == _ELF:
        elf, segments = 0, 0
    else:
        return None
    if head[elf:elf + 4] != _ELF:
        return None
    phoff, phentsize, phnum = u64(elf + 0x20), u16(elf + 0x36), u16(elf + 0x38)
    if None in (phoff, phentsize, phnum):
        return None
    headers = []
    for i in range(phnum):
        ph = elf + phoff + i * phentsize
        t, off, sz = u32(ph), u64(ph + 8), u64(ph + 0x20)
        if None in (t, off, sz):
            return None
        headers.append((t, off, sz))
    hit = next((h for h in headers if h[0] in (_PROCPARAM, _MODPARAM)), None)
    if not hit:
        return None
    kind, param, _ = hit
    at = param
    if segments:
        at = None
        for seg, (_, off, size) in enumerate(headers):
            if not (off <= param < off + size):
                continue
            for k in range(segments):
                e = 0x20 + k * 0x20
                props, s_off, s_size = u64(e), u64(e + 8), u64(e + 0x10)
                if props is None:
                    continue
                plain = props & ((1 << 1) | (1 << 3)) == 0
                if plain and (props >> 20) & 0xFFFF == seg and s_size == size:
                    at = s_off + (param - off)
                    break
            if at is not None:
                break
        if at is None:
            return None
    b = read_range(at, 0x20)
    if len(b) < 0x14:
        return None
    want = _PROC_MAGIC if kind == _PROCPARAM else _MOD_MAGIC
    for base in (0, 8):
        if base + 0x10 <= len(b) and struct.unpack_from("<I", b, base)[0] == want:
            return _sdk_fw(struct.unpack_from("<I", b, base + 0xC)[0])
    return None


def ftp_read(f, path, offset=0, n=256 * 1024):
    """Lee hasta n bytes de un archivo remoto desde offset (y corta la transferencia)."""
    conn = f.transfercmd("RETR " + path, rest=offset or None)
    buf = bytearray()
    try:
        conn.settimeout(30)
        while len(buf) < n:
            chunk = conn.recv(min(256 * 1024, n - len(buf)))
            if not chunk:
                break
            buf += chunk
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        f.voidresp()
    except ftplib.all_errors:
        pass
    if len(buf) >= n:
        _ftp_resync(f)
    return bytes(buf)


def _ftp_resync(f):
    """Al cortar una descarga a medias algunos servidores mandan una respuesta de mas (426 y luego 226),
    y la siguiente orden recibiria la respuesta equivocada. Manda un NOOP y lee hasta su 200."""
    old = None
    try:
        old = f.sock.gettimeout()
        f.sock.settimeout(5)
        f.putcmd("NOOP")
        for _ in range(4):
            line = f.getmultiline()
            if line.startswith("200"):
                break
    except Exception:
        pass
    finally:
        try:
            f.sock.settimeout(old)
        except Exception:
            pass


def title_from_param(text):
    try:
        j = json.loads(text) if isinstance(text, str) else text
    except Exception:
        m = re.search(r'"titleName"\s*:\s*"([^"]+)"', text or "")
        return m.group(1) if m else None
    if not isinstance(j, dict):
        return None
    lp = j.get("localizedParameters") or {}
    for lang in (lp.get("defaultLanguage"), "es-ES", "en-US", "en-GB"):
        if lang and isinstance(lp.get(lang), dict) and lp[lang].get("titleName"):
            return lp[lang]["titleName"]
    for v in lp.values():
        if isinstance(v, dict) and v.get("titleName"):
            return v["titleName"]
    return j.get("titleName")


def param_facts(text):
    """Lo util de un param.json: titulo, Title ID, version y firmware."""
    try:
        j = json.loads(text)
    except Exception:
        j = {}
    if not isinstance(j, dict):
        j = {}
    return {
        "title": title_from_param(j) if j else title_from_param(text),
        "tid": (j.get("titleId") or E.title_id(text or "") or None),
        "version": j.get("contentVersion") or j.get("masterVersion"),
        "fw_param": bcd(j.get("requiredSystemSoftwareVersion")),
        "sdk": bcd(j.get("sdkVersion")),
        "region": content_region(j.get("contentId") or ""),
    }


_REGIONS = {"EP": "EU", "UP": "US", "JP": "JP", "HP": "ASIA", "KP": "KR"}


def content_region(cid):
    """Region por el Content ID (EP9000-PPSA01234_00-... -> EU)."""
    return _REGIONS.get(str(cid or "")[:2].upper(), "")


def parse_sfo(data):
    """param.sfo (PS4): diccionario con sus claves (TITLE, TITLE_ID, APP_VER, SYSTEM_VER, CATEGORY...)."""
    import struct
    out = {}
    if len(data) < 20 or data[:4] != b"\0PSF":
        return out
    _, _, key_off, data_off, n = struct.unpack_from("<4sIIII", data, 0)
    for i in range(min(n, 256)):
        e = 20 + i * 16
        if e + 16 > len(data):
            break
        k_off, fmt, ln, _mx, d_off = struct.unpack_from("<HHIII", data, e)
        ks = key_off + k_off
        key = data[ks:data.find(b"\0", ks)].decode("ascii", "replace")
        raw = data[data_off + d_off:data_off + d_off + ln]
        if fmt == 0x0404 and len(raw) >= 4:
            out[key] = struct.unpack_from("<I", raw)[0]
        else:
            out[key] = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    return out


def sfo_facts(data):
    s = parse_sfo(data)
    sv = s.get("SYSTEM_VER")
    fw = None
    if isinstance(sv, int) and sv:
        fw = "%x.%02x" % (sv >> 24, (sv >> 16) & 0xFF)
    cat = str(s.get("CATEGORY") or "")
    return {"title": s.get("TITLE") or None, "tid": s.get("TITLE_ID") or None,
            "version": s.get("APP_VER") or s.get("VERSION") or None, "fw_param": fw, "need_fw": fw,
            "extra": cat in ("ac", "gp", "sd")}


def inspect_remote_game(f, path, listing=None):
    """Datos de una aplicacion en carpeta en la consola, por FTP. listing: entradas de 'path'."""
    names = {n: x for n, x in (listing if listing is not None else E.ftp_listdir(f, path))}
    out = {"has_eboot": "eboot.bin" in names, "backport": "fakelib" in names or "fakelib2" in names,
           "icon": False, "fw_exe": None}
    try:
        sce = {n for n, _ in E.ftp_listdir(f, path + "/sce_sys")}
    except ftplib.all_errors:
        sce = set()
    out["icon"] = "icon0.png" in sce
    if "param.json" in sce:
        raw = ftp_read(f, path + "/sce_sys/param.json", 0, 512 * 1024)
        out.update(param_facts(raw.decode("utf-8-sig", "replace")))
        if not out.get("tid"):
            out["param_err"] = "param.json: %d bytes leídos (%r)" % (len(raw), raw[:60])
    elif sce:
        out["param_err"] = "sce_sys no tiene param.json"
    if out["has_eboot"]:
        try:
            out["fw_exe"] = exe_firmware(lambda off, n: ftp_read(f, path + "/eboot.bin", off, n))
        except Exception:
            out["fw_exe"] = None
    # con backport manda lo que piden los ejecutables (rebajados); sin el, lo que dice param.json
    out["need_fw"] = (out.get("fw_exe") if out["backport"] else None) or out.get("fw_param") or out.get("fw_exe")
    out["extra"] = not out["has_eboot"] and bool(out.get("tid"))   # DLC o actualizacion
    return out


# ---------------------------------------------------------------------------
# Payloads: descargar de GitHub y mandar al cargador de ELF
# ---------------------------------------------------------------------------

PAYLOADS = [
    {"id": "ftpsrv", "name": "ftpsrv", "repo": "ps5-payload-dev/ftpsrv",
     "what": "Servidor FTP (puerto 2121): por él se envía todo"},
    {"id": "bfpilot", "name": "BFpilot", "repo": "ItsBlurf/BFpilot",
     "what": "Descomprime, mueve y borra en la consola (puerto 5905)"},
]


# Otros payloads que PS5 Directo sabe iniciar (no los necesita para funcionar)
EXTRA_PAYLOADS = [
    {"id": "rpgetpin", "name": "Remote Play: PIN para vincular", "repo": "idlesauce/ps5-remoteplay-get-pin", "port": None,
     "what": "Saca el PIN y el ID de cuenta para vincular un programa de Remote Play sin PSN"},
    {"id": "dsoverlay", "name": "DualSense Overlay", "repo": "erickdavestech/ps5-dualsense-overlay", "port": None,
     "pick": [r"(?i)^ps5-dualsense-overlay.*\.elf$", r"(?i)\.elf$"],
     "what": "Dibuja el mando en una esquina mientras juegas y se iluminan los botones que pulsas"},
    {"id": "retropapa", "name": "Retro Papa", "repo": "zx0CF1/RetroPapaPS5-Beta", "port": None,
     "pick": [r"(?i)^RetroPapa-Installer\.elf$", r"(?i)installer.*\.elf$"],
     "what": "Instala Retro Papa, un lanzador de emuladores clásicos nativo"},
    {"id": "patchsys", "name": "ps-patch-system", "repo": "illusionyy/ps-patch-system", "port": 23900,
     "pick": [r"(?i)^patch-bundle-loader-prospero\.elf$", r"(?i)^patch-bundle-prospero\.elf$"],
     "what": "Parches para títulos de PS4 y PS5 (60 fps, resolución…), se eligen en su web (puerto 23900)"},
    {"id": "neoenabler", "name": "NeoEnabler", "repo": "gasb07/NeoEnabler", "port": None,
     "pick": [r"(?i)^neo-manager\.elf$", r"(?i)neo.*\.elf$"],
     "what": "Títulos de PS4 en modo PS4 Pro (hasta 4K) con sus parches; va después de ps-patch-system"},
    {"id": "patchdl", "name": "PatchDL", "repo": "knutwurst/ps5-patchdl", "port": 12880,
     "what": "Actualizaciones oficiales de lo instalado, desde el navegador (puerto 12880)"},
    {"id": "forge", "name": "PS5 Dump Forge", "repo": "quer3q/ps5-dump-forge", "port": 8095,
     "what": "Conversor e instalador de .pkg dentro de la consola (se usa desde su web, puerto 8095)"},
    {"id": "overlay", "name": "PS5 Hardware Overlay", "repo": "smoxa/ps5-new-overlay", "port": 8585,
     "pick": [r"(?i)^ps5-overlay.*\.elf$", r"(?i)overlay.*\.elf$"],
     "what": "FPS, temperaturas, frecuencias, RAM, ventilador y consumo encima de la imagen (se ajusta en su web, puerto 8585)"},
    {"id": "exporter", "name": "ps5-exporter", "repo": "Marice/ps5-exporter", "port": 9100,
     "what": "Temperaturas, ventilador, consumo, FPS y título abierto, de solo lectura (puerto 9100)"},
    {"id": "pkgmanager", "name": "PKG Manager", "repo": "itsPLK/ps5-pkg-manager", "port": 8844,
     "what": "Instalar .pkg desde USB, carpeta compartida o el PC (puerto 8844)"},
    {"id": "linuxloader", "name": "ps5-linux-loader", "repo": "ps5-linux/ps5-linux-loader", "port": None, "rest": True,
     "pick": [r"(?i)^ps5-linux-loader.*\.elf$", r"(?i)linux.*loader.*\.elf$"],
     "what": "Arranca Linux (Ubuntu o Bazzite) desde tu USB o tu M.2: la consola pasa a reposo y al encenderla entra en Linux"},
    {"id": "smplusgui", "name": "SMPlusGui", "repo": "KarnerF/SMPlusGui", "port": 7777,
     "pick": [r"(?i)^SMPlusGui.*\.elf$"],
     "what": "Ajustes de ShadowMountPlus desde el navegador, con copias de su configuración y su registro (puerto 7777)"},
    {"id": "tailscale", "name": "ps5-tailscale", "repo": "holdmysocks/ps5-tailscale", "port": 8090,
     "pick": [r"(?i)^tailscale-[\d.]+\.elf$", r"(?i)^tailscale.*\.elf$"],
     "what": "Mete la consola en tu red de Tailscale: FTP, BFpilot y Remote Play desde fuera de casa, sin abrir puertos (estado en el puerto 8090)"},
    {"id": "zftpd", "name": "zftpd", "repo": "seregonwar/zftpd", "port": 8888,
     "pick": [r"(?i)ps5.*zhttp.*\.elf$", r"(?i)zhttp.*ps5.*\.elf$", r"(?i)ps5.*zhttp.*\.bin$"],
     "what": "Servidor de archivos con web (puerto 8888) que también descarga enlaces y torrents en la propia consola"},
]


# «Recomendados» de la Biblioteca: programas libres para la PS5, bajados de la pagina oficial de cada autor en
# GitHub (no se aloja nada). Solo emuladores y herramientas: nada de tiendas ni catalogos de contenido.
CATALOG = [
    {"id": "psswanstation", "kind": "app", "group": "emu", "fam": "ps", "name": "PSSwanStation", "sys": "PS1", "tid": "PPSA99248",
     "repo": "Press5elect/PSSwanStation", "asset": [r"(?i)PPSA99248.*\.zip$"],
     "what": "Emulador de PlayStation (PS1), adaptado a la PS5.",
     "needs": "Tu propio contenido de PS1 y, si te la pide, la BIOS de tu PS1.",
     "update": "Se actualiza desde su propio menú (Menú › Update).",
     "emu": {"data": "/data/psswanstation", "bios": "bios", "games": "games",
             "bios_what": "BIOS de tu PS1 (opcional: sin ella usa OpenBIOS, que arranca casi todo)",
             "bios_ext": [".bin", ".rom"],
             "files": ["network.cfg", "logs/", "data/"],
             "guide": ["Instálalo con el botón de arriba: queda en /data/homebrew/PPSA99248.",
                       "Contenido: «Enviar contenido» lo pone en su carpeta games. También lee un USB o una carpeta compartida "
                       "de tu red (se configura en network.cfg).",
                       "Formatos: .cue/.bin, .img, .chd, .pbp sin cifrar, .mds/.mdf y .ecm. Si una copia trae .sbi "
                       "(protección LibCrypt), déjala junto al .cue con el mismo nombre.",
                       "BIOS: no hace falta, pero con la de tu propia consola arrancan algunos títulos más.",
                       "Registros: en su carpeta logs (las dos últimas veces que lo abriste)."]}},
    {"id": "crux2", "kind": "app", "group": "emu", "fam": "ps", "name": "Crux2", "sys": "PS2", "tid": "PPSA99249",
     "repo": "Press5elect/Crux2", "asset": [r"(?i)^Crux2-v[\d.]+\.zip$", r"(?i)^Crux2.*\.zip$"],
     "what": "Emulador de PlayStation 2 (basado en PCSX2), adaptado a la PS5.",
     "needs": "La BIOS de tu propia PS2 (en su carpeta bios) y tu contenido (en games, un USB o una carpeta compartida).",
     "update": "Para actualizarlo, copia encima la carpeta nueva de su GitHub: se conservan ajustes, partidas y contenido.",
     "emu": {"data": "/data/crux2", "bios": "bios", "games": "games",
             "bios_what": "BIOS de tu PS2 (obligatoria: sin ella no arranca nada)",
             "bios_ext": [".bin", ".rom0", ".rom1", ".rom2", ".erom", ".nvm", ".mec"],
             "files": ["inis/PCSX2.ini", "network.cfg", "crux2-boot.log", "crux2-boot.previous.log", "logs/emulog.txt"],
             "guide": ["Instálalo con el botón de arriba: queda en /data/homebrew/PPSA99249.",
                       "BIOS: la de tu propia PS2, con «Poner tu BIOS». Sin ella no arranca nada.",
                       "Contenido: «Enviar contenido» lo pone en su carpeta games. También lee un USB con una carpeta llamada "
                       "crux2, ps2, ps2iso o playstation2, y carpetas compartidas de tu red (network.cfg).",
                       "Formatos: .iso, .chd, .cso, .zso, .gz, .bin, .mdf, .nrg e .img. Los .chd y .cso ocupan menos.",
                       "Truco: si creas la carpeta /data/crux2, guarda allí ajustes, BIOS, contenido y partidas, y al "
                       "actualizar no se toca nada tuyo.",
                       "Si algo falla, mira crux2-boot.log y logs/emulog.txt aquí abajo."]}},
    {"id": "prosperoeden", "kind": "app", "group": "emu", "fam": "nin", "exp": True, "name": "ProsperoEden", "sys": "Switch", "tid": "PPSA99016",
     "also": ["PPSA99008"], "repo": "heydemoura/ProsperoEden", "asset": [r"(?i)PPSA\d{5}.*\.(zip|exfat)$", r"(?i)\.(zip|exfat)$"],
     "what": "Emulador de Nintendo Switch para la PS5 (en pruebas).",
     "needs": "Las claves y el firmware de tu propia Switch (van en /data/prosperoeden) y tu contenido.",
     "update": "Sus versiones de prueba salen a menudo: mira su GitHub.",
     "emu": {"data": "/data/prosperoeden", "fixed": True, "bios": "keys", "games": "roms",
             "bios_what": "Claves de tu propia Switch (prod.keys y, si la tienes, title.keys)",
             "bios_ext": [".keys"],
             "extra_dirs": [{"dir": "firmware", "what": "Firmware de tu Switch (los archivos .nca)", "ext": [".nca"]},
                            {"dir": "updates", "what": "Actualizaciones y DLC (.nsp o .xci)", "ext": [".nsp", ".xci"]}],
             "files": ["config/prosperoeden.json", "logs/boot-trace.txt", "logs/"],
             "guide": ["Todo va en /data/prosperoeden (se puede cambiar en sus Ajustes › Game files).",
                       "Claves: prod.keys de tu Switch es obligatoria, title.keys opcional (van en keys).",
                       "Firmware: los .nca sacados de tu Switch (en firmware).",
                       "Contenido: .nsp y .xci en roms; actualizaciones y DLC en updates.",
                       "Está en pruebas: muchos títulos aún no van o van lentos.",
                       "Si se cierra, mira logs/boot-trace.txt y la carpeta logs aquí abajo."]}},
    # --- mas emuladores (todos libres y de la pagina de su autor; ninguno trae BIOS, claves, firmware ni contenido)
    {"id": "ps5sx2", "kind": "app", "group": "emu", "fam": "ps", "name": "PS5SX2", "sys": "PS2", "tid": "PPSA99203",
     "repo": "Swordpdf/PS5SX2", "asset": [r"(?i)PPSA99203.*\.zip$", r"(?i)\.zip$"],
     "what": "Emulador de PS2 basado en PCSX2: el más avanzado, con versiones cada pocos días.",
     "needs": "La BIOS de tu propia PS2 (sin ella usa OpenPS2BIOS, experimental) y tu contenido.",
     "update": "Sus versiones salen a menudo: el aviso de aquí te dice cuándo.",
     "emu": {"data": "/data/PCSX2", "fixed": True, "bios": "bios", "games": "games",
             "bios_what": "BIOS de tu PS2 (recomendada)", "bios_ext": [".bin", ".rom0", ".erom", ".nvm", ".mec"],
             "extra_dirs": [{"dir": "patches", "what": "Parches de pantalla ancha y 60 FPS (.pnach)", "ext": [".pnach"]}],
             "files": ["inis/PCSX2.ini", "logs/"],
             "guide": ["Instálalo con el botón de arriba: queda en /data/homebrew/PPSA99203.",
                       "BIOS: la de tu propia PS2 va en /data/PCSX2/bios.",
                       "Contenido: /data/PCSX2/games, o un USB con una carpeta games, PCSX2/games o PS5SX2/games.",
                       "También arranca tus discos originales de PS2 desde el lector de la consola (en beta).",
                       "Parches .pnach en /data/PCSX2/patches: se activan por título en sus ajustes.",
                       "Teclado y ratón USB funcionan como mando (no en firmware 11.x y 12.00)."]}},
    {"id": "psxs5", "kind": "app", "group": "emu", "fam": "ps", "name": "PSXS5", "sys": "PS1", "tid": "PPSA97510",
     "repo": "SynoPiia/PSXS5", "asset": [r"(?i)PPSA97510.*\.zip$", r"(?i)\.zip$"],
     "what": "Emulador de PS1 (PCSX-ReARMed y Beetle) con estantería de carátulas, perfiles y gestor de tarjetas de memoria.",
     "needs": "Nada obligatorio. Con la BIOS de tu propia PS1 usa Beetle, más fiel.",
     "update": "Avisa de versión nueva y se actualiza desde Ajustes › Acerca de.",
     "emu": {"data": "/data/PSXS5", "fixed": True, "bios": "bios", "games": "games",
             "bios_what": "BIOS de tu PS1 (opcional: scph5500, scph5501 o scph5502 según la región)",
             "bios_ext": [".bin"], "files": ["logs/"],
             "guide": ["Contenido en /data/PSXS5/games (o un USB).",
                       "BIOS opcional en /data/PSXS5/bios: con ella usa Beetle para ese título.",
                       "Carátulas propias en /data/PSXS5/covers, con el nombre del título.",
                       "Necesita etaHEN y kstuff cargados."]}},
    {"id": "psp5", "kind": "app", "group": "emu", "fam": "ps", "name": "PSP5", "sys": "PSP", "tid": "PPSA99131",
     "repo": "tsuramatsu1/PSP5", "asset": [r"(?i)PPSA99131.*\.zip$", r"(?i)\.zip$"], "exp": True,
     "what": "Emulador de PSP basado en PPSSPP.",
     "needs": "Tu contenido (.iso, .cso, .chd o EBOOT.PBP). No necesita BIOS.",
     "emu": {"data": "/data/homebrew/PPSA99131", "fixed": True, "games": "memstick/PSP/GAME",
             "files": [],
             "guide": ["Instálalo con el botón de arriba.",
                       "Contenido en memstick/PSP/GAME dentro de su carpeta, o en PSP/GAME de un USB.",
                       "Para ver las carpetas necesita que el listado esté desbloqueado (con el Helper: añade PSP5 a /data/whitelist.txt).",
                       "L2 + R2 abre su menú: trucos, estados guardados y salir."]}},
    {"id": "porpoise", "kind": "app", "group": "emu", "fam": "nin", "name": "Porpoise", "sys": "GameCube / Wii",
     "tid": "PPSA99764", "repo": "elripalda/Porpoise", "asset": [r"(?i)PPSA99764.*\.zip$", r"(?i)\.zip$"],
     "what": "Emulador de GameCube y Wii basado en Dolphin, con biblioteca de carátulas, tarjetas de memoria y estados guardados.",
     "needs": "Tu contenido (.iso, .rvz, .gcz, .wbfs…). No necesita BIOS.",
     "emu": {"data": "/data/porpoise", "fixed": True, "games": "games", "files": [],
             "guide": ["Contenido en /data/porpoise/games (también busca en /data/games, /data/GameCube, /data/Wii y USB).",
                       "Formatos: .iso, .gcm, .rvz, .ciso, .gcz, .wbfs y .wia.",
                       "Desde los ajustes de un título puedes crear su propio icono en el menú de la PS5.",
                       "Con OnionHEN: añade PPSA99764 a exact_title_ids en /data/OnionHEN/config.ini."]}},
    {"id": "mupen64", "kind": "app", "group": "emu", "fam": "nin", "name": "Mupen64Plus PS5", "sys": "N64",
     "tid": "PPSA99064", "repo": "TheRealRetro/mupen64plus-ps5", "asset": [r"(?i)PPSA99064.*\.zip$", r"(?i)\.zip$"],
     "what": "Emulador de Nintendo 64 con estantería de carátulas, vibración del DualSense y estados guardados.",
     "needs": "Tu contenido (.z64, .n64, .v64). No necesita BIOS.",
     "update": "Desde la 0.6.3 se actualiza solo desde dentro de la app.",
     "emu": {"data": "/data/mupen64plus", "fixed": True, "games": "roms", "files": ["logs/"],
             "guide": ["Contenido en /data/mupen64plus/roms (o mupen64plus/roms en un USB).",
                       "Partidas, estados, carátulas y ajustes quedan en /data/mupen64plus y no se tocan al actualizar.",
                       "Si falla, mira logs/boot.log aquí abajo."]}},
    {"id": "snes9x", "kind": "app", "group": "emu", "fam": "nin", "name": "Snes9x PS5", "sys": "SNES",
     "tid": "PPSA99922", "repo": "MisterTemaki/snes9xPS5", "asset": [r"(?i)PPSA99922.*\.zip$", r"(?i)\.zip$"],
     "what": "Emulador de Super Nintendo, pequeño y muy pulido.", "needs": "Tu contenido (.sfc, .smc…).",
     "emu": {"data": "/data/snes9x", "fixed": True, "games": "roms", "files": ["logs/"],
             "guide": ["Contenido en /data/snes9x/roms (o un USB).",
                       "Usa el mismo Helper que PS5SX2 para salir de su caja: cárgalo si no ve tus carpetas."]}},
    {"id": "eglantine", "kind": "app", "group": "emu", "fam": "nin", "name": "EglantinePS5", "sys": "Switch 2",
     "tid": "PPSA98515", "repo": "mucahidkambur/EglantinePS5", "asset": [r"(?i)PPSA98515.*\.zip$", r"(?i)\.zip$"], "exp": True,
     "alts": ["viejunerd/Eglantine-PS5-", "heydemoura/EglantinePS5"],
     "what": "Emulador de Nintendo Switch 2 (adaptación de Eglantine, basado en Eden). Muy experimental.",
     "needs": "Las claves y el firmware de tu propia consola y tu contenido (.nsp o .xci).",
     "update": "Avisa de versión nueva al abrirlo.",
     "emu": {"data": "/data/eglantineps5", "fixed": True, "bios": "keys", "games": "roms",
             "bios_what": "Claves de tu propia consola (prod.keys y, si la tienes, title.keys)", "bios_ext": [".keys"],
             "extra_dirs": [{"dir": "firmware", "what": "Firmware de tu consola (los archivos .nca)", "ext": [".nca"]},
                            {"dir": "updates", "what": "Actualizaciones y DLC (.nsp o .xci)", "ext": [".nsp", ".xci"]}],
             "files": ["logs/"],
             "guide": ["Todo va en /data/eglantineps5 (se puede cambiar en sus Ajustes › Game files).",
                       "Claves en keys y firmware en firmware: tienen que ser de tu propia consola.",
                       "Contenido (.nsp o .xci) en roms; actualizaciones y DLC en updates.",
                       "Después de añadir claves o firmware, ciérralo y vuelve a abrirlo.",
                       "Está en pruebas: muchos títulos aún no van o van lentos."]}},
    {"id": "ecam", "kind": "app", "group": "emu", "fam": "nin", "name": "PS5-ECAM", "sys": "Wii U / 3DS / DS / Switch",
     "tid": "PPSA99360", "repo": "premohq/PS5-ECAM", "asset": [r"(?i)PPSA99360.*\.zip$", r"(?i)\.zip$"], "exp": True,
     "what": "Cuatro emuladores en uno: Cemu (Wii U), Azahar (3DS), melonDS (DS) y Eden (Switch).",
     "needs": "Según el sistema: claves de tus discos de Wii U, contenido de 3DS descifrado por ti, y las claves y el firmware de tu Switch.",
     "update": "Se actualiza desde dentro de la app.",
     "emu": {"data": "/data", "fixed": True, "bios": "eden/keys", "games": "eden/roms",
             "bios_what": "Claves de tu propia Switch (prod.keys y title.keys)", "bios_ext": [".keys"],
             "extra_dirs": [{"dir": "eden/firmware", "what": "Switch: tu firmware (.nca)", "ext": [".nca"]},
                            {"dir": "cemu/games", "what": "Wii U: contenido (.wua, .wud, .wux)", "ext": []},
                            {"dir": "azahar/games", "what": "3DS: contenido descifrado", "ext": []},
                            {"dir": "melonds/games", "what": "DS: contenido (.nds)", "ext": [".nds"]},
                            {"dir": "melonds/bios", "what": "DS: BIOS y firmware de tu propia DS (opcional)", "ext": [".bin"]}],
             "files": ["cemu/log.txt", "eden/logs/"],
             "guide": ["Cada sistema tiene su carpeta: /data/cemu (Wii U), /data/azahar (3DS), /data/melonds (DS) y /data/eden (Switch).",
                       "Wii U: las claves de tus discos van en /data/cemu/keys.txt, una por línea.",
                       "Switch: prod.keys y title.keys en /data/eden/keys y tu firmware en /data/eden/firmware.",
                       "Avanza muy rápido y está poco probado en consola: si algo falla, actualiza primero."]}},
    {"id": "psflycast", "kind": "app", "group": "emu", "fam": "sega", "name": "PSFlycast", "sys": "Dreamcast / Naomi",
     "tid": "PPSA99247", "repo": "Press5elect/PSFlycast", "asset": [r"(?i)PPSA99247.*\.zip$", r"(?i)\.zip$"],
     "what": "Emulador de Dreamcast, Naomi, Naomi 2 y Atomiswave basado en Flycast (del autor de Crux2).",
     "needs": "Tu contenido (.gdi, .cdi, .chd). La BIOS de tu Dreamcast es opcional.",
     "emu": {"data": "/data/homebrew/PPSA99247", "fixed": True, "games": "games", "files": [],
             "guide": ["Contenido en la carpeta games de su instalación.",
                       "Las tarjetas de memoria (VMU) quedan en su carpeta vmu."]}},
    {"id": "genplus", "kind": "app", "group": "emu", "fam": "sega", "name": "Genesis Plus GX PS5",
     "sys": "Mega Drive / Mega-CD / Master System / Game Gear", "tid": "PPSA99921", "repo": "MisterTemaki/genplusgxPS5",
     "asset": [r"(?i)PPSA99921.*\.zip$", r"(?i)\.zip$"], "what": "Emulador de las consolas de Sega de 8 y 16 bits, Mega-CD incluido.",
     "needs": "Tu contenido. Para Mega-CD, la BIOS de tu propia consola.",
     "emu": {"data": "/data/genplus", "fixed": True, "bios": "bios", "games": "roms",
             "bios_what": "BIOS de tu Mega-CD (solo para Mega-CD)", "bios_ext": [".bin"], "files": ["logs/"],
             "guide": ["Contenido en /data/genplus/roms (o un USB).",
                       "Mega-CD: la BIOS de tu consola (bios_CD_E.bin, bios_CD_U.bin o bios_CD_J.bin) en /data/genplus/bios."]}},
    {"id": "fbneo", "kind": "app", "group": "emu", "fam": "sega", "name": "FBNeo PS5", "sys": "Arcade",
     "tid": "PPSA99924", "repo": "MisterTemaki/fbneo-ps5", "asset": [r"(?i)PPSA99924.*\.zip$", r"(?i)\.zip$"],
     "what": "Emulador de recreativas (FinalBurn Neo).", "needs": "Tus sets con el nombre que usa FBNeo (.zip o .7z).",
     "emu": {"data": "/data/fbneo", "fixed": True, "games": "roms", "files": ["logs/"],
             "guide": ["Contenido en /data/fbneo/roms (o fbneo/roms en un USB), con el nombre de set de FBNeo.",
                       "Los controles se cambian por título desde su menú de pausa."]}},
    {"id": "pce", "kind": "app", "group": "emu", "fam": "sega", "name": "Beetle PCE PS5", "sys": "PC Engine / TurboGrafx",
     "tid": "PPSA99925", "repo": "MisterTemaki/beetlepcePS5", "asset": [r"(?i)PPSA99925.*\.zip$", r"(?i)\.zip$"],
     "what": "Emulador de PC Engine y TurboGrafx-16, CD incluido.", "needs": "Tu contenido. Para los CD, la System Card de tu consola.",
     "emu": {"data": "/data/pce", "fixed": True, "bios": "bios", "games": "roms",
             "bios_what": "System Card de tu PC Engine (solo para CD)", "bios_ext": [".pce", ".bin"], "files": ["logs/"],
             "guide": ["Contenido en /data/pce/roms (o un USB).", "CD: la System Card de tu consola en /data/pce/bios."]}},
    {"id": "ps5x360", "kind": "app", "group": "emu", "fam": "xbox", "name": "PS5X360", "sys": "Xbox 360",
     "tid": "PPSA50011", "repo": "BrinooTk/PS5X360", "asset": [r"(?i)PPSA50011.*\.zip$", r"(?i)\.zip$"], "exp": True,
     "what": "Emulador de Xbox 360 basado en Xenia Canary. Experimental.",
     "needs": "Tu contenido, una carpeta por título.",
     "emu": {"data": "/data/homebrew/PPSA50011", "fixed": True, "games": "assets/roms", "files": [],
             "guide": ["Cada título en su propia carpeta dentro de assets/roms de su instalación.",
                       "Después: Ajustes › Refresh game list.",
                       "Probado con firmware 13.60, etaHEN, ShadowMountPlus y kstuff."]}},
    {"id": "xpsemu", "kind": "app", "group": "emu", "fam": "xbox", "name": "XPSemu", "sys": "Xbox",
     "tid": "PPSA97358", "repo": "ZiZc3/XPSemu", "asset": [r"(?i)PPSA97358.*\.zip$", r"(?i)\.zip$"], "exp": True,
     "what": "Emulador de la primera Xbox basado en xemu. En fase alfa.",
     "needs": "El MCPX boot ROM y la BIOS de tu propia Xbox, y tu contenido (.iso).",
     "emu": {"data": "/data/xemu", "fixed": True, "bios": ".", "games": "games",
             "bios_what": "MCPX boot ROM y BIOS de tu propia Xbox", "bios_ext": [".bin"], "files": [],
             "guide": ["El MCPX boot ROM y la BIOS de tu consola van en /data/xemu (su pantalla de inicio te dice qué falta).",
                       "Contenido en /data/xemu/games (o xemu/games en un USB).",
                       "Si no tienes disco duro de Xbox, crea uno vacío solo."]}},
    {"id": "retroarch", "kind": "app", "group": "emu", "fam": "multi", "name": "RetroArch", "sys": "Muchos sistemas",
     "tid": "PPSA99169", "repo": "mihawk-99/PS5_RetroArch", "asset": [r"(?i)PPSA99169.*\.zip$", r"(?i)\.zip$"], "exp": True,
     "what": "RetroArch con EmulationStation: unos 40 sistemas en una sola app, con su propia web (puerto 6769) para "
             "subir contenido y cambiar ajustes desde el móvil o el PC.",
     "needs": "Tu contenido y, según el núcleo, la BIOS de tu propia consola.",
     "update": "Se actualiza desde su web.",
     "emu": {"data": "/data/homebrew/PPSA99169", "fixed": True, "bios": "system", "games": "content",
             "bios_what": "BIOS y archivos de sistema de tus consolas (en system)", "bios_ext": [],
             "files": ["config/retroarch.cfg"],
             "guide": ["Contenido en content/<sistema> de su carpeta (psx, snes, saturn…) o desde su web.",
                       "BIOS de tus consolas en system (la de PS2 en system/pcsx2/bios).",
                       "Su web: http://IP-de-tu-PS5:6769 mientras está abierto.",
                       "Partidas en savefiles y estados en savestates."]}},
    {"id": "ruffle", "kind": "app", "group": "emu", "fam": "other", "name": "Ruffle", "sys": "Flash",
     "tid": "PPSA68091", "repo": "ZiZc3/Ruffle-Flash-PS5", "asset": [r"(?i)PPSA68091.*\.zip$", r"(?i)\.zip$"],
     "what": "Reproductor de contenido Flash (.swf).", "needs": "Tus archivos .swf.",
     "emu": {"data": "/data/ruffle", "fixed": True, "games": "games", "files": ["ruffle.log"],
             "guide": ["Archivos .swf en /data/ruffle/games (los que traen más archivos, en una carpeta propia).",
                       "Carátulas en /data/ruffle/covers con el mismo nombre."]}},
    # --- multimedia (tus propios vídeos, música y servidores)
    {"id": "doom", "kind": "app", "group": "media", "name": "DOOM (port nativo)", "tid": "PPSA99666",
     "repo": "lowbit/ps5-doom", "asset": [r"(?i)PPSA99666.*\.zip$", r"(?i)\.zip$"],
     "what": "Port nativo del código libre de DOOM para la PS5, con mando, vibración y su música. Trae el episodio "
             "shareware gratuito; para los demás juegos de la saga necesitas tus propios archivos (o los libres de Freedoom).",
     "needs": "kstuff y ShadowMountPlus."},
    {"id": "vlc", "kind": "app", "group": "media", "name": "VLC", "tid": "PPSA85300", "repo": "ZiZc3/VLC",
     "asset": [r"(?i)PPSA85300.*\.zip$", r"(?i)\.zip$"],
     "what": "VLC para PS5: vídeo hasta 4K con HDR, música, subtítulos, listas, carpetas compartidas y DLNA.",
     "needs": "kstuff y ShadowMountPlus. Para ver /data y los USB, el Helper (o etaHEN u OnionHEN).",
     "emu": {"data": "/data/vlc", "fixed": True, "games": ".", "games_label": "vídeos y música", "files": [],
             "guide": ["Tus vídeos y música en /data/vlc, en un USB o desde su página para el móvil (puerto 8080).",
                       "«Enviar vídeos y música» los pone en /data/vlc."]}},
    {"id": "jelly5", "kind": "app", "group": "media", "name": "Jelly5", "tid": "PPSA99505", "repo": "02dnot/Jelly5",
     "asset": [r"(?i)PPSA99505.*\.zip$", r"(?i)\.zip$"], "what": "Cliente de Jellyfin (y Emby) para ver tu propio servidor de vídeos en la PS5.",
     "needs": "Tu servidor de Jellyfin o Emby en casa."},
    {"id": "emby5", "kind": "app", "group": "media", "name": "Emby5", "tid": "PPSA99515", "repo": "bornaradusin/Emby5",
     "asset": [r"(?i)PPSA99515.*\.zip$", r"(?i)\.zip$"], "what": "Cliente de Emby y Jellyfin para la PS5.", "needs": "Tu servidor de Emby o Jellyfin en casa."},
    # --- herramientas que se instalan como aplicacion
    {"id": "pshare", "kind": "app", "group": "tool", "name": "PShare", "tid": "PPSA99773", "repo": "StonedModder/PShare",
     "asset": [r"(?i)PPSA99773.*\.zip$", r"(?i)\.zip$"],
     "what": "Manda tus capturas y clips desde la propia consola a Discord, Telegram, un USB o una carpeta del PC, "
             "con galería y reproductor.",
     "needs": "El cargador de ELF en el puerto 9021. Su configuración va en /data/pshare/config.ini."},
    {"id": "chiral", "kind": "app", "group": "tool", "name": "Chiral-FTP", "tid": "PPSA96024", "repo": "Herbrax/Chiral-FTP",
     "asset": [r"(?i)PPSA96024.*\.zip$", r"(?i)\.zip$"],
     "what": "Gestor de archivos de dos paneles y cliente FTP/SFTP en la propia consola, con el mando.",
     "needs": "ShadowMountPlus y el cargador de ELF en el puerto 9021. Sus ajustes quedan en /data/chiral-ftp."},
    {"id": "dsoverlay", "kind": "payload", "group": "tool", "name": "DualSense Overlay",
     "repo": "erickdavestech/ps5-dualsense-overlay",
     "what": "Dibuja el mando DualSense en una esquina de la pantalla mientras juegas: los botones, sticks y gatillos se "
             "iluminan al pulsarlos. Ideal para grabar o hacer directos. No toca la memoria del juego.",
     "needs": "Se carga una vez por arranque (con kstuff-lite). No lo uses a la vez que otros contadores de FPS."},
    {"id": "retropapa", "kind": "payload", "group": "emu", "fam": "multi", "sys": "NES / SNES / Mega Drive / PS1 / Arcade",
     "name": "Retro Papa", "repo": "zx0CF1/RetroPapaPS5-Beta", "exp": True,
     "what": "Lanzador de emuladores clásicos nativo (NES, Super Nintendo, Mega Drive, PS1 y arcade) con su propia "
             "interfaz. PS5 Directo envía su instalador y luego lo abres desde el menú de juegos.",
     "needs": "En pruebas. No trae ROMs ni BIOS: lo tuyo lo pones tú."},
    {"id": "patchsys", "kind": "payload", "group": "patch", "name": "Parches de juegos (ps-patch-system)",
     "repo": "illusionyy/ps-patch-system",
     "what": "Mejoras para títulos de PS4 y PS5 que hace la comunidad: más fps, más resolución, arreglos… Se activan una a "
             "una en su web. PS5 Directo lo carga y te pone los parches en la consola con un botón.",
     "needs": "Firmware de PS5 3.00 a 12.70 (según su autor). Se vuelve a cargar tras cada reinicio. Los parches solo cambian "
              "el ejecutable al arrancar: el título no se toca."},
    {"id": "neoenabler", "kind": "payload", "group": "patch", "name": "NeoEnabler (PS4 en modo Pro)",
     "repo": "gasb07/NeoEnabler", "exp": True,
     "what": "Hace que los títulos de PS4 de su lista arranquen como en una PS4 Pro y suban de resolución (hasta 4K) con "
             "sus parches. Necesita ps-patch-system cargado antes.",
     "needs": "Firmware 4.00 a 13.60 y ps-patch-system cargado primero, después de cada reinicio. Solo sirve con los títulos "
              "que tienen parche."},
    {"id": "rpgetpin", "kind": "payload", "group": "rp", "name": "Remote Play: sacar el PIN",
     "repo": "idlesauce/ps5-remoteplay-get-pin",
     "what": "Con el jailbreak la consola no entra en PSN, así que no da el PIN para vincular Remote Play. Este payload "
             "lo saca: sale en una notificación de la PS5 junto con el ID de cuenta que hay que usar.",
     "needs": "Nada instalado: se carga una vez cuando vas a vincular. Luego usa ese PIN en chiaki-ng."},
    {"id": "chiaki", "kind": "pc", "group": "rp", "name": "chiaki-ng", "repo": "streetpea/chiaki-ng",
     "what": "Remote Play libre para PC (Windows, Mac, Linux), Steam Deck y Android: ves la consola y la manejas con "
             "mando, o con teclado y ratón, sin moverte del PC.",
     "needs": "Vincularlo una vez con el PIN de arriba y la IP de tu PS5. Va mejor con cable o Wi-Fi de 5 GHz."},
    {"id": "patchdl", "kind": "payload", "group": "tool", "name": "PatchDL", "repo": "knutwurst/ps5-patchdl",
     "what": "Busca e instala las actualizaciones oficiales de lo que tienes instalado (de disco o de la tienda). "
             "Se usa desde el navegador del PC, del móvil o de la PS5.",
     "needs": "Nada instalado en la consola: se carga como payload cuando lo necesitas. Es la alternativa a "
              "PS5 Game Updater sin tener que instalar una aplicación."},
    {"id": "forge", "kind": "payload", "group": "tool", "name": "PS5 Dump Forge", "repo": "quer3q/ps5-dump-forge",
     "what": "Convierte formatos e instala .pkg dentro de la propia consola, desde su web.",
     "needs": "Nada instalado en la consola: se carga como payload."},
    {"id": "overlay", "kind": "payload", "group": "tool", "name": "PS5 Hardware Overlay", "repo": "smoxa/ps5-new-overlay",
     "what": "Como MangoHud en el PC: pone encima de la imagen (en el menú y dentro de las aplicaciones) los FPS, la "
             "temperatura de la CPU, la GPU y el SSD, las frecuencias, el uso de RAM y VRAM, el ventilador y el consumo. "
             "Eliges qué se ve, dónde y con qué colores desde su web.",
     "needs": "Nada instalado: se carga como payload y se quita desde su web (botón Unload) sin reiniciar. "
              "Sus ajustes se guardan en /data/overlay/config.ini."},
    {"id": "exporter", "kind": "payload", "group": "tool", "name": "ps5-exporter", "repo": "Marice/ps5-exporter",
     "what": "Datos de la consola de solo lectura: temperaturas, ventilador, consumo, FPS, qué título está abierto y "
             "horas de uso. Lo usa la pestaña Sistema.",
     "needs": "Nada instalado: se carga como payload (puerto 9100). Puedes añadirlo a la carga automática de Payload Manager."},
    {"id": "pkgmanager", "kind": "payload", "group": "tool", "name": "PKG Manager", "repo": "itsPLK/ps5-pkg-manager",
     "what": "Instala .pkg desde un USB, una carpeta compartida de tu PC o NAS, o directamente desde el PC, sin copia temporal.",
     "needs": "Nada instalado: se carga como payload y se usa desde su web (puerto 8844)."},
    {"id": "ps5linux", "kind": "guide", "group": "os", "name": "Linux en la PS5 (Ubuntu)", "repo": "ps5-linux/ps5-linux-loader",
     "payload": "linuxloader", "doc": "https://wiki.ps5-linux.eu",
     "what": "Convierte la consola en un PC con Linux mientras lo usas: escritorio, navegador, Steam, emuladores de PC y "
             "programas de Linux. Al reiniciar vuelve el sistema de la PS5, sin tocarlo.",
     "needs": "PS5 normal, Slim o Pro con firmware 3.00 a 13.60 (Pro: 13.60), un USB de 64 GB o más (mejor un SSD externo), "
              "teclado y ratón USB. Opcional: un M.2 solo para Linux (se borra entero).",
     "guide": ["Baja la imagen ps5-ubuntu de ps5-linux-image y grábala en el USB con Balena Etcher (se borra el USB).",
               "En la PS5: Ajustes › Sistema › Ahorro de energía › Funciones en modo de reposo › Suministrar energía a "
               "los puertos USB: Siempre. Y desactiva «Activar vínculo de dispositivo HDMI».",
               "Conecta el USB en un puerto trasero (el delantero de arriba es USB 2.0, más lento).",
               "Haz el jailbreak y quita etaHEN o kstuff si los tienes cargados (no son compatibles).",
               "Pulsa «Enviar el cargador»: la consola pasa a reposo. Espera a que la luz naranja deje de parpadear y "
               "enciéndela: la luz blanca indica que ha entrado en Linux.",
               "Si se queda en negro, prueba amdgpu.force_1080p=1 en cmdline.txt (en la partición FAT32 del USB)."]},
    {"id": "bazzite", "kind": "guide", "group": "os", "name": "Bazzite Deck (Steam como en la Steam Deck)", "repo": "VivaLaVent/ps5-bazzite-deck",
     "payload": "linuxloader", "doc": "https://wiki.ps5-linux.eu/installation/bazzite-deck", "exp": True,
     "what": "Bazzite en modo Steam Deck: arranca directamente en la pantalla completa de Steam (tu biblioteca de Steam, Proton, "
             "Decky Loader) y con un clic tienes escritorio KDE. Desde un USB o el M.2, sin tocar el sistema de la PS5.",
     "needs": "Lo mismo que Linux en la PS5 (firmware hasta 13.60, USB o M.2 rápido de 64 GB o más) y una cuenta de Steam. "
              "Proyecto de la comunidad, no oficial de Bazzite.",
     "guide": ["Sigue la guía de su wiki: baja la imagen de Bazzite Deck de su GitHub (la marcada como Latest) y grábala en el USB o el M.2.",
               "Prepara la consola igual que para Linux: energía USB en reposo «Siempre» y vínculo HDMI desactivado.",
               "Haz el jailbreak y pulsa «Enviar el cargador». Cuando la luz deje de parpadear, enciéndela.",
               "La primera vez completa el asistente de Steam. Para volver a la PS5, apaga y enciende normal.",
               "Desbloquear las unidades de cálculo extra de la GPU es opcional y puede dar fallos: déjalo para el final."]},
    {"id": "mihawkos", "kind": "app", "group": "os", "name": "MihawkOS", "tid": "PPSA99170", "exp": True,
     "repo": "mihawk-99/PS5_MihawkOS", "asset": [r"(?i)PPSA99170.*\.zip$", r"(?i)mihawk.*\.zip$", r"(?i)\.zip$"],
     "what": "Tus programas de Windows en la PS5, sin Linux: un escritorio como el de Windows con explorador, navegador y "
             "Steam oficial dentro (tu cuenta y tu biblioteca). Se actualiza solo.",
     "needs": "Solo estos payloads y ninguno más (tampoco etaHEN): Kstuff Lite 1.11 Beta o posterior, elfldr, websrv y "
              "ShadowMountPlus beta11 o posterior. Probado en 10.01 (Pro) y 13.40-13.60. No funciona lo que lleva DRM "
              "como Denuvo ni antitrampas (EAC, BattlEye). Experimental.",
     "update": "Se actualiza solo al abrirlo (desde la 7.1)."},
    {"id": "smplusgui", "kind": "payload", "group": "tool", "name": "SMPlusGui", "repo": "KarnerF/SMPlusGui",
     "what": "Todos los ajustes de ShadowMountPlus desde el navegador del PC o del móvil, con copias de seguridad de su "
             "configuración, su registro y arranque automático.",
     "needs": "ShadowMountPlus 1.6 o posterior. Se carga como payload y se abre en el puerto 7777."},
    {"id": "tailscale", "kind": "payload", "group": "tool", "name": "ps5-tailscale", "repo": "holdmysocks/ps5-tailscale",
     "what": "Pone la consola en tu red privada de Tailscale: desde fuera de casa llegas a su FTP, a BFpilot y a Remote Play "
             "como si estuvieras en casa, sin abrir puertos del router. En PS5 Directo, pon como IP de la consola su "
             "dirección de Tailscale (100.x.x.x).",
     "needs": "Una cuenta de Tailscale (gratis) y Tailscale también en el PC. Se carga como payload; la primera vez "
              "abre http://IP-de-la-consola:8090 para vincularla. Experimental: probado por su autor en una sola consola."},
    {"id": "zftpd", "kind": "payload", "group": "tool", "name": "zftpd", "repo": "seregonwar/zftpd",
     "what": "Servidor de archivos con web propia. Lo usa PS5 Directo para que la consola descargue sola los enlaces "
             "(destino «La PS5 lo baja sola») y para la pestaña Sistema (temperatura, RAM, tiempo encendida).",
     "needs": "Nada instalado: se carga como payload. También es un servidor FTP: si ya tienes ftpsrv, zftpd usa otro "
              "puerto (lo dice en la notificación) y PS5 Directo sigue con ftpsrv."},
]


def pick_asset(assets, rxs):
    """El primer archivo de la release que encaje con alguno de los patrones, por orden."""
    names = [a for a in assets if not a.get("name", "").lower().startswith("source")]
    for rx in rxs:
        hit = next((a for a in names if re.search(rx, a.get("name", ""))), None)
        if hit:
            return hit
    return None


def http_get(url, limit=200 * MiB, timeout=60, dest=None, progress=None):
    req = urllib.request.Request(url, headers={"User-Agent": "PS5Directo"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if dest is None:
            return r.read(limit)
        got = 0
        with open(dest, "wb") as fo:
            while True:
                chunk = r.read(MiB)
                if not chunk:
                    break
                got += len(chunk)
                if got > limit:
                    raise E.PipeError("La descarga es demasiado grande")
                fo.write(chunk)
                if progress:
                    progress(got)
        return dest


GH_API = os.environ.get("PS5D_GITHUB_API") or "https://api.github.com"   # se puede cambiar para pruebas


def github_release(repo):
    """Como _github_release, pero con un error que se entiende si GitHub no contesta."""
    try:
        return _github_release(repo)
    except E.PipeError:
        raise
    except Exception as e:
        code = getattr(e, "code", None)
        if code in (403, 429):
            raise E.PipeError("GitHub ha limitado las consultas desde tu conexión: prueba otra vez en un rato")
        raise E.PipeError("No he podido consultar GitHub (%s)" % E.net_reason(e))


def _github_release(repo):
    """(tag, [assets]) de la ultima version publicada (si solo hay pruebas, la mas nueva)."""
    base = GH_API + "/repos/%s/releases" % repo
    try:
        req = urllib.request.Request(base + "/latest", headers=_ua())
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        req = urllib.request.Request(base + "?per_page=10", headers=_ua())
        with urllib.request.urlopen(req, timeout=20) as r:
            lst = [x for x in json.load(r) if not x.get("draft")]
        if not lst:
            raise E.PipeError("%s no tiene versiones publicadas" % repo)
        j = lst[0]
    return (j.get("tag_name") or "").strip(), j.get("assets") or []


def _pick_elf(names):
    elfs = [n for n in names if n.lower().endswith((".elf", ".bin"))]
    pref = [n for n in elfs if "ps5" in n.lower()] or [n for n in elfs if "ps4" not in n.lower()] or elfs
    return pref[0] if pref else None


def fetch_payload(pid, data_dir, log=None):
    """Ruta local del payload (lo baja de GitHub si hace falta) y su version."""
    p = next((x for x in PAYLOADS + EXTRA_PAYLOADS if x["id"] == pid), None)
    if not p:
        raise E.PipeError("Payload desconocido")
    folder = os.path.join(data_dir, "payloads")
    os.makedirs(folder, exist_ok=True)
    cached = sorted((n for n in os.listdir(folder) if n.startswith(pid + "-") and n.endswith(".elf")),
                    key=lambda n: os.path.getmtime(os.path.join(folder, n)), reverse=True)
    try:
        tag, assets = github_release(p["repo"])
    except Exception as e:
        if cached:
            if log:
                log("Sin conexión con GitHub (%s): uso la copia guardada %s" % (e, cached[0]))
            return os.path.join(folder, cached[0]), cached[0][len(pid) + 1:-4]
        raise E.PipeError("No pude consultar GitHub (%s): %s" % (p["repo"], e))
    safe_tag = re.sub(r"[^\w.-]", "_", tag or "ultima")
    dest = os.path.join(folder, "%s-%s.elf" % (pid, safe_tag))
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest, tag
    names = {a.get("name", ""): a.get("browser_download_url") for a in assets}
    elf = None
    if p.get("pick"):
        hit = pick_asset(assets, p["pick"])
        elf = hit.get("name") if hit else None
    elf = elf or _pick_elf(list(names))
    if elf:
        data = http_get(names[elf], limit=64 * MiB)
    else:
        z = next((n for n in names if n.lower().endswith(".zip")), None)
        if not z:
            raise E.PipeError("La última versión de %s no trae un .elf" % p["repo"])
        zf = zipfile.ZipFile(io.BytesIO(http_get(names[z], limit=128 * MiB)))
        inner = _pick_elf([n for n in zf.namelist() if not n.endswith("/")])
        if not inner:
            raise E.PipeError("El .zip de %s no trae un .elf" % p["repo"])
        data = zf.read(inner)
    if not data[:4] == b"\x7fELF":
        raise E.PipeError("Lo descargado de %s no parece un payload (.elf)" % p["repo"])
    tmp = dest + ".tmp"
    with open(tmp, "wb") as fo:
        fo.write(data)
    os.replace(tmp, dest)
    if log:
        log("Descargado %s %s de GitHub" % (p["name"], tag))
    return dest, tag


class Zftpd:
    """La web de zftpd (seregonwar/zftpd) en la consola, puerto 8888. Su descargador baja enlaces http/https/ftp
    y magnet en la propia PS5 (continua si se corta) y su API de estadisticas da la temperatura, la RAM y el tiempo
    encendida. Las peticiones POST llevan el codigo anti-CSRF que pone en su pagina principal."""
    PORT = 8888

    def __init__(self, ip, timeout=8):
        self.base = "http://%s:%d" % (E.clean_host(ip or "")[0], self.PORT)
        self.timeout = timeout
        self._csrf = None

    def _get(self, path, timeout=None):
        req = urllib.request.Request(self.base + path, headers={"User-Agent": "PS5Directo"})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return r.read(4 * MiB)

    def get_json(self, path, timeout=None):
        return json.loads(self._get(path, timeout).decode("utf-8", "replace") or "{}")

    def alive(self):
        try:
            self.get_json("/api/status", 3)
            return True
        except Exception:
            return False

    def csrf(self):
        if self._csrf is None:
            try:
                html = self._get("/", 6).decode("utf-8", "replace")
                m = re.search(r'name="csrf-token"\s+content="([0-9a-fA-F]*)"', html)
                self._csrf = m.group(1) if m else ""
            except Exception:
                self._csrf = ""
        return self._csrf

    def post(self, path, body=None, timeout=None):
        data = json.dumps(body or {}).encode()
        h = {"User-Agent": "PS5Directo", "Content-Type": "application/json"}
        tok = self.csrf()
        if tok:
            h["X-CSRF-Token"] = tok
        req = urllib.request.Request(self.base + path, data=data, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read(MiB).decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read(65536).decode("utf-8", "replace")).get("error") or ""
            except Exception:
                msg = ""
            raise E.PipeError("zftpd: %s (HTTP %s)" % (msg or e.reason, e.code))

    # -- descargas en la consola
    def dl_start(self, url, dst):
        return self.post("/api/download/start", {"url": url, "dst": dst})

    def dl_status(self):
        return self.get_json("/api/download/status").get("downloads") or []

    def dl_cancel(self, i):
        return self.post("/api/download/cancel", {"id": int(i)})

    # -- descomprimir en la consola (ZIP siempre; otros formatos si su version trae libarchive)
    def extract(self, path, dst):
        q = urllib.parse.urlencode({"path": path, "dst": dst})
        return self.post("/api/extract?" + q)

    def extract_progress(self):
        return self.get_json("/api/extract_progress")

    # -- titulos: abrir uno instalado e instalar un .pkg que ya esta en la consola
    def launch(self, tid):
        return self.post("/api/admin/launch?" + urllib.parse.urlencode({"id": tid}), timeout=30)

    def install(self, path, overwrite=False, dest="internal"):
        q = {"path": path, "dest": dest}
        if overwrite:
            q["overwrite"] = "1"
        try:
            return self.post("/api/admin/games/install?" + urllib.parse.urlencode(q), timeout=60)
        except E.PipeError as e:
            if "HTTP 409" in str(e):
                return {"ok": False, "already": True}
            raise

    def install_status(self):
        return self.get_json("/api/admin/games/install_status", 6)

    # -- estado del sistema
    def stats(self):
        out = {}
        try:
            out.update(self.get_json("/api/stats/system", 4))
        except Exception:
            pass
        try:
            out["ram"] = self.get_json("/api/stats/ram", 4)
        except Exception:
            pass
        try:
            d = self.get_json("/api/stats?path=/data", 4)
            out["disk"] = {k: d.get(k) for k in ("disk_used", "disk_total", "disk_free", "disk_path")}
        except Exception:
            pass
        return out


EXPORTER_PORT = 9100
_PROM_RX = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)')
_LABEL_RX = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def exporter_metrics(ip, timeout=4):
    """Lee el texto de ps5-exporter (Marice/ps5-exporter, puerto 9100, solo lectura) y devuelve
    {nombre: [(etiquetas, valor)]}."""
    url = "http://%s:%d/metrics" % (E.clean_host(ip or "")[0], EXPORTER_PORT)
    req = urllib.request.Request(url, headers={"User-Agent": "PS5Directo"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read(4 * MiB).decode("utf-8", "replace")
    out = {}
    for ln in text.splitlines():
        if not ln or ln.startswith("#"):
            continue
        m = _PROM_RX.match(ln)
        if not m:
            continue
        try:
            v = float(m.group(3))
        except ValueError:
            continue
        labels = dict(_LABEL_RX.findall(m.group(2) or ""))
        out.setdefault(m.group(1), []).append((labels, v))
    return out


def exporter_summary(mt):
    """Lo que ensena PS5 Directo de las metricas del exporter."""
    def one(metric, default=None, **want):
        for lb, v in mt.get(metric, []):
            if all(lb.get(k) == w for k, w in want.items()):
                return v
        return default
    temps = {lb.get("sensor"): v for lb, v in mt.get("ps5_temperature_celsius", []) if lb.get("sensor")}
    socs = [v for k, v in temps.items() if k and k.startswith("soc")]
    cpu_use = [v for lb, v in mt.get("ps5_cpu_usage_ratio", [])]
    running = [lb.get("title_id") for lb, v in mt.get("ps5_game_info", []) if lb.get("title_id")]
    if not running:
        running = [lb.get("title_id") for lb, v in mt.get("ps5_game_running", []) if v and lb.get("title_id")]
    info = next((lb for lb, v in mt.get("ps5_info", [])), {})
    fs = [{"mount": lb.get("mountpoint"), "size": v} for lb, v in mt.get("ps5_filesystem_size_bytes", [])]
    avail = {lb.get("mountpoint"): v for lb, v in mt.get("ps5_filesystem_avail_bytes", [])}
    for x in fs:
        x["avail"] = avail.get(x["mount"])
    data = next((x for x in fs if x["mount"] in ("/user", "/data", "/system_data")), None)
    mounted = [lb.get("title_id") for lb, v in mt.get("ps5_shadowmount_game_mounted", []) if v and lb.get("title_id")]
    names = {lb.get("title_id"): lb.get("name") for lb, v in mt.get("ps5_shadowmount_game_info", []) if lb.get("title_id")}
    return {
        "cpu_temp": temps.get("cpu"), "soc_temp": max(socs) if socs else None,
        "fan": one("ps5_fan_duty_ratio"), "watts": one("ps5_power_watts"), "fps": one("ps5_fps"),
        "cpu_use": (sum(cpu_use) / len(cpu_use)) if cpu_use else None,
        "cpu_mhz": (one("ps5_cpu_frequency_hertz") or 0) / 1e6 or None,
        "gpu_mhz": (one("ps5_soc_clock_hertz", None, name="gpu") or 0) / 1e6 or None,
        "uptime_seconds": one("ps5_uptime_seconds"), "power_hours": (one("ps5_power_operating_seconds_total") or 0) / 3600 or None,
        "power_cycles": one("ps5_power_cycles_total"), "model": info.get("model"), "firmware": info.get("firmware"),
        "running": running[0] if running else "", "mounted": mounted, "smp_names": names,
        "disk": {"disk_total": data["size"], "disk_free": data.get("avail"),
                 "disk_used": data["size"] - (data.get("avail") or 0)} if data else None,
        "smp_up": bool(one("ps5_shadowmount_up")), "smp_version": next((lb.get("version") for lb, v in mt.get("ps5_shadowmount_info", [])), ""),
    }


PLDMGR_PORT = 8084
PAYLOAD_RX = {"ftpsrv": re.compile(r"ftpsrv", re.I), "bfpilot": re.compile(r"bfpilot", re.I)}


def _ver_tokens(v):
    v = (v or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:].strip()
    return re.findall(r"\d+|[A-Za-z]+", v)


def pm_compare(a, b, ua=None, ub=None):
    """Compara versiones igual que Payload Manager: 1 si a es mas nueva, -1 si es mas vieja, 0 si iguales
    (1.7 > 1.7beta3; 1.0.1 > 1.0; con la fecha de publicacion para desempatar)."""
    ta, tb = _ver_tokens(a), _ver_tokens(b)
    if not ta or not tb:
        if ta or tb:
            return 1 if ta else -1
    for i in range(max(len(ta), len(tb))):
        if i >= len(ta):
            return 1 if tb[i].isalpha() else -1
        if i >= len(tb):
            return -1 if ta[i].isalpha() else 1
        x, y = ta[i], tb[i]
        if x.isdigit() != y.isdigit():
            return 1 if x.isdigit() else -1
        if x.isdigit():
            x, y = int(x), int(y)
        else:
            x, y = x.lower(), y.lower()
        if x != y:
            return 1 if x > y else -1
    if ua and ub and ua != ub:
        return 1 if ua > ub else -1
    return 0


class PayloadManager:
    """Panel web de Payload Manager (itsPLK/ps5-payload-manager) en la consola, puerto 8084.
    Es lo que usa casi todo el mundo para cargar payloads: desde el PC se le puede pedir que
    instale, lance o cierre payloads y que los cargue solos al encender (su lista de autoload).
    Sus acciones son POST y piden la cabecera X-PLDMGR-Request: 1 (su proteccion anti-CSRF)."""

    def __init__(self, host, port=PLDMGR_PORT):
        self.host = E.clean_host(host or "")[0]
        self.port = int(port or PLDMGR_PORT)

    def _req(self, method, path, body=None, ctype=None, timeout=8):
        if not self.host:
            raise E.PipeError("Falta la IP de la PS5: ponla en Ajustes")
        url = "http://%s:%d%s" % (self.host, self.port, path)
        headers = {"User-Agent": "PS5Directo"}
        if method == "POST":
            headers["X-PLDMGR-Request"] = "1"
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(url, data=body if method == "POST" else None, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(8 * MiB)
        except urllib.error.HTTPError as e:
            msg = e.read(2000).decode("utf-8", "replace").strip()
            raise E.PipeError("Payload Manager respondió %d: %s" % (e.code, msg[:200]))

    def _json(self, path, timeout=8):
        return json.loads(self._req("GET", path, timeout=timeout).decode("utf-8", "replace") or "{}")

    def version(self, timeout=3):
        """Version de Payload Manager, o None si no esta abierto en la consola."""
        try:
            v = self._req("GET", "/version", timeout=timeout).decode("utf-8", "replace").strip()
            return v or "?"
        except Exception:
            return None

    def payloads(self):
        return self._json("/list_payloads").get("items") or []

    def processes(self):
        return self._json("/processes_list").get("processes") or []

    def config(self):
        return self._json("/get_config")

    def find(self, pid, items=None):
        """El payload instalado que corresponde a 'ftpsrv' o 'bfpilot' (el mas nuevo si hay varios)."""
        rx = PAYLOAD_RX[pid]
        items = self.payloads() if items is None else items
        hits = [x for x in items if rx.search(x.get("filename") or "") or rx.search(x.get("name") or "")]
        if pid == "ftpsrv":   # el ftpsrv "normal" antes que otras versiones
            hits.sort(key=lambda x: "drakmor" in (x.get("filename") or "").lower())
        return hits[0] if hits else None

    def running(self, pid, procs=None):
        rx = PAYLOAD_RX[pid]
        procs = self.processes() if procs is None else procs
        return [p for p in procs if rx.search(p.get("name") or "")]

    def list_full(self):
        """/list_payloads entero: {"self": {...}, "items": [...]} (con version y origen de lo instalado del repositorio)."""
        return self._json("/list_payloads")

    def repository(self, refresh=False):
        """Catalogo de Payload Manager (su repositorio oficial y las fuentes que hayas anadido): [payload, ...]."""
        j = json.loads(self._req("POST" if refresh else "GET", "/repository_refresh" if refresh else "/repository_payloads",
                                 timeout=40).decode("utf-8", "replace") or "{}")
        out = []
        if isinstance(j, list):
            srcs = [{"payloads": j}]
        elif isinstance(j.get("sources"), list):
            srcs = j["sources"]
        else:
            srcs = [j]
        for src in srcs:
            for x in (src.get("payloads") or []):
                if isinstance(x, dict):
                    x = dict(x)
                    x.setdefault("source_url", src.get("url") or "")
                    x.setdefault("source_name", src.get("name") or "")
                    out.append(x)
        return out

    def repo_install(self, source_url, pid, mode="replace"):
        """Lo mismo que el boton «Actualizar» de Payload Manager: baja la version nueva de su fuente y la deja en su sitio."""
        q = urllib.parse.urlencode({"source_url": source_url, "id": pid, "mode": mode})
        r = self._req("POST", "/repository_install?" + q, timeout=120)
        try:
            return json.loads(r.decode("utf-8", "replace") or "{}")
        except ValueError:
            return {}

    def launch(self, slot):
        self._req("POST", "/loadpayload:" + urllib.parse.quote(slot, safe="/:"), timeout=30)

    def kill(self, pid):
        self._req("POST", "/process_kill?pid=%d" % int(pid), timeout=15)

    def upload(self, filename, data):
        q = urllib.parse.urlencode({"filename": filename, "mode": "new"})
        self._req("POST", "/manage:upload?" + q, body=data, ctype="application/octet-stream", timeout=60)

    def add_autoload(self, slots):
        """Anade esos payloads a su lista de carga automatica (sin quitar lo que ya tuviera) y la activa."""
        cfg = self.config()
        cur = [x.strip() for x in re.split(r"[,\n]", cfg.get("AUTOLOAD_LIST") or "") if x.strip()]
        new = cur + [s for s in slots if s and s not in cur]
        body = json.dumps({"AUTOLOAD_LIST": ",".join(new), "AUTOLOAD_ENABLED": True}).encode()
        self._req("POST", "/set_config", body=body, ctype="application/json", timeout=15)
        return new


def send_elf(host, port, data, timeout=15):
    """Manda un payload al cargador de ELF de la consola (elfldr/etaHEN, puerto 9021)."""
    host = E.clean_host(host)[0]
    if not host:
        raise E.PipeError("Falta la IP de la PS5: ponla en Ajustes")
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError as e:
        raise E.PipeError("El cargador de payloads no responde en %s:%s (%s). Necesitas el "
                          "jailbreak cargado con un cargador de ELF que acepte la red (etaHEN, "
                          "elfldr). El de Y2JB Autoloader solo acepta la propia consola: con él usa "
                          "Payload Manager." % (host, port, e))
    try:
        s.sendall(data)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        s.settimeout(3)
        try:
            s.recv(1024)
        except OSError:
            pass
    finally:
        s.close()


# ---------------------------------------------------------------------------
# PS5 Dump Forge (programa de linea de comandos)
# ---------------------------------------------------------------------------

FORGE_REPO = "quer3q/ps5-dump-forge"
FORGE_FORMATS = {
    # formato de PS5 Directo -> (argumentos de Forge, extension del resultado o None si es carpeta)
    "plain": (["--to", "folder"], None),          # carpeta normal (deshace imagenes, .fpkg y LZ4)
    "ffpkg": (["--to", "ffpkg"], ".ffpkg"),
    "exfat": (["--to", "exfat"], ".exfat"),
    "ffpfs": (["--to", "ffpfs"], ".ffpfs"),
    "ffpfsc": (["--to", "ffpfsc"], ".ffpfsc"),
    "fpkg": (["--to", "pkg"], ".pkg"),            # FPKG de depuracion: solo firmware 11.60 o inferior
    "lz4": (["--to", "lz4"], None),
    "lz4ffpkg": (["--to", "ffpkg", "--lz4-pack"], ".ffpkg"),
    "zstd": (["--to", "folder", "--zstd-pack"], None),          # PS5 Dump Forge 0.5 o posterior
    "zstdffpkg": (["--to", "ffpkg", "--zstd-pack"], ".ffpkg"),
}
FORGE_STAGES = {"extract": "leyendo el comprimido", "scan": "analizando", "preflight": "comprobando",
                "measure": "midiendo", "write": "escribiendo", "pack": "empaquetando",
                "verify": "verificando", "finalize": "terminando"}


def forge_cli_name():
    return "ps5-dump-forge.exe" if os.name == "nt" else "ps5-dump-forge"


FORGE_NEEDED_FLAGS = ("--to", "--lz4-pack", "--lz4-unpack", "--kraken", "--level")


def forge_cli_ok(exe):
    """Comprueba que una version de Forge sigue entendiendo las opciones que usa PS5 Directo.
    True = vale, False = le faltan opciones, None = no se ha podido saber."""
    import subprocess
    try:
        p = subprocess.run([exe, "convert", "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=60, creationflags=E._NOWIN)
    except Exception:
        return None
    out = p.stdout.decode("utf-8", "replace")
    if "--to" not in out:
        return None
    return all(f in out for f in FORGE_NEEDED_FLAGS)


_FORGE_HELP = {}


def forge_has(exe, flag):
    """True si esta version de Forge conoce esa opcion (se mira una vez por ejecutable)."""
    import subprocess
    if exe not in _FORGE_HELP:
        try:
            p = subprocess.run([exe, "convert", "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=60, creationflags=E._NOWIN)
            _FORGE_HELP[exe] = p.stdout.decode("utf-8", "replace")
        except Exception:
            return False
    return flag in _FORGE_HELP[exe]


def find_forge(custom, data_dir, skip=()):
    custom = (custom or "").strip().strip('"')
    if custom and os.path.isfile(custom):
        return custom
    root = os.path.join(data_dir, "tools", "forge")
    best = None
    if os.path.isdir(root):
        for d in os.listdir(root):
            if d in skip:
                continue
            p = os.path.join(root, d, forge_cli_name())
            if os.path.isfile(p) and (best is None or os.path.getmtime(p) > os.path.getmtime(best)):
                best = p
    return best or shutil.which("ps5-dump-forge")


def _forge_asset(assets):
    import platform
    mach = platform.machine().lower()
    arm = "arm" in mach or "aarch64" in mach
    if os.name == "nt":
        rx = r"windows-arm64\.zip$" if arm else r"windows-x86-64\.zip$"
    elif sys.platform == "darwin":
        rx = r"macos-universal\.zip$"
    else:
        rx = r"linux-arm64\.tar\.gz$" if arm else r"linux-x86-64\.tar\.gz$"
    for a in assets:
        n = a.get("name", "")
        if re.search(rx, n, re.I) and "webview2" not in n.lower():
            return a
    return None


def download_forge(data_dir, log=None, progress=None, release=None):
    tag, assets = release or github_release(FORGE_REPO)
    a = _forge_asset(assets)
    if not a:
        raise E.PipeError("La última versión de PS5 Dump Forge no trae descarga para este sistema")
    safe_tag = re.sub(r"[^\w.-]", "_", tag or "ultima")
    folder = os.path.join(data_dir, "tools", "forge", safe_tag)
    exe = os.path.join(folder, forge_cli_name())
    if os.path.isfile(exe):
        return exe, tag
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, "descarga.tmp")
    if log:
        log("Descargando PS5 Dump Forge %s (%s)…" % (tag, a.get("name")))
    http_get(a["browser_download_url"], limit=400 * MiB, timeout=120, dest=tmp, progress=progress)
    want = forge_cli_name()
    try:
        if a["name"].lower().endswith(".zip"):
            with zipfile.ZipFile(tmp) as z:
                m = next((n for n in z.namelist() if posixpath.basename(n) == want and not n.endswith("/")), None)
                if not m:
                    raise E.PipeError("El .zip de Forge no trae %s" % want)
                with z.open(m) as src, open(exe + ".tmp", "wb") as dst:
                    shutil.copyfileobj(src, dst)
        else:
            with tarfile.open(tmp) as t:
                m = next((x for x in t.getmembers() if x.isfile() and posixpath.basename(x.name) == want), None)
                if not m:
                    raise E.PipeError("El paquete de Forge no trae %s" % want)
                with t.extractfile(m) as src, open(exe + ".tmp", "wb") as dst:
                    shutil.copyfileobj(src, dst)
        os.replace(exe + ".tmp", exe)
        if os.name != "nt":
            os.chmod(exe, 0o755)
            mac_prepare_exe(exe)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return exe, tag


def _forge_env(password):
    env = dict(os.environ)
    env.pop("FORGE_ARCHIVE_PASSWORD", None)
    if password:
        env["FORGE_ARCHIVE_PASSWORD"] = password
    return env


# ---------------------------------------------------------------------------
# Programas que lanza un trabajo (Forge, 7-Zip): pausarlos donde estan y bajarles la prioridad
# ---------------------------------------------------------------------------

CHILD = threading.local()        # CHILD.job = id del trabajo que esta usando este hilo
CHILD_PROCS = {}                 # id del trabajo -> {pid}
LOW_PRIO = [False]               # «Usar menos el PC» al convertir


def set_low_priority(pid, on=True):
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x0200 | 0x0400, False, pid)      # SET_INFORMATION | QUERY_INFORMATION
            if h:
                k.SetPriorityClass(h, 0x4000 if on else 0x20)    # BELOW_NORMAL o NORMAL
                k.CloseHandle(h)
        else:
            os.setpriority(os.PRIO_PROCESS, pid, 15 if on else 0)
    except Exception:
        pass


def child_started(proc):
    """Lo llama cada sitio que lanza un programa largo: lo apunta al trabajo del hilo y aplica la prioridad baja."""
    if LOW_PRIO[0]:
        set_low_priority(proc.pid, True)
    jid = getattr(CHILD, "job", None)
    if jid:
        live = {p for p in CHILD_PROCS.get(jid, set()) if _pid_alive(p)}
        live.add(proc.pid)
        CHILD_PROCS[jid] = live
    return proc


def _pid_alive(pid):
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == 259                      # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def suspend_pids(pids, pause=True):
    """Congela (o descongela) programas donde estan: no gastan procesador ni disco hasta seguir."""
    done = 0
    for pid in pids:
        try:
            if os.name == "nt":
                import ctypes
                h = ctypes.windll.kernel32.OpenProcess(0x0800, False, pid)     # PROCESS_SUSPEND_RESUME
                if not h:
                    continue
                fn = ctypes.windll.ntdll.NtSuspendProcess if pause else ctypes.windll.ntdll.NtResumeProcess
                fn(h)
                ctypes.windll.kernel32.CloseHandle(h)
            else:
                import signal
                os.kill(pid, signal.SIGSTOP if pause else signal.SIGCONT)
            done += 1
        except Exception:
            pass
    return done


def priority_all(on):
    LOW_PRIO[0] = bool(on)
    for pids in list(CHILD_PROCS.values()):
        for pid in pids:
            if _pid_alive(pid):
                set_low_priority(pid, on)


def _run_cancellable(args, env, cancel, on_line=None, timeout=None):
    """Ejecuta un programa leyendo su salida linea a linea. Devuelve (codigo, salida, errores)."""
    p = child_started(subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                       env=env, creationflags=NO_WINDOW))
    out, err = [], []

    def pump_err():
        for ln in iter(p.stderr.readline, b""):
            err.append(ln.decode("utf-8", "replace").rstrip())
            del err[:-200]
    th = threading.Thread(target=pump_err, daemon=True)
    th.start()
    stop = threading.Event()

    def watch():
        t0 = time.time()
        while not stop.wait(0.5):
            if (cancel is not None and cancel.is_set()) or (timeout and time.time() - t0 > timeout):
                try:
                    p.terminate()
                except Exception:
                    pass
                return
    threading.Thread(target=watch, daemon=True).start()
    try:
        for ln in iter(p.stdout.readline, b""):
            s = ln.decode("utf-8", "replace").rstrip("\r\n")
            if on_line:
                on_line(s)
            else:
                out.append(s)
        code = p.wait()
    finally:
        stop.set()
        th.join(5)
    if cancel is not None and cancel.is_set():
        raise E.Cancelled()
    return code, out, err


def _forge_err(err):
    lines = [x for x in err if x.strip()]
    msg = lines[-1] if lines else "error desconocido"
    return re.sub(r"^ps5-dump-forge:\s*", "", msg)


def forge_inspect(exe, src, password, cancel):
    code, out, err = _run_cancellable([exe, "inspect", src, "--json"], _forge_env(password), cancel)
    if code != 0:
        raise E.PipeError(_forge_err(err))
    try:
        return json.loads("\n".join(out))
    except ValueError:
        raise E.PipeError("PS5 Dump Forge respondió algo que no entiendo")


FORGE_COMP = {
    # compresion elegida -> argumentos para .fpkg (kraken) y .ffpfsc (nivel zlib)
    "fpkg": {"fast": ["--kraken", "fast"], "balanced": ["--kraken", "balanced"],
             "smallest": ["--kraken", "smallest"], "stored": ["--kraken", "stored"]},
    "ffpfsc": {"fast": ["--level", "1"], "balanced": ["--level", "6"], "smallest": ["--level", "9"],
               "stored": ["--level", "0"]},
    "lz4": {"fast": ["--lz4-level", "0"], "balanced": ["--lz4-level", "9"], "smallest": ["--lz4-level", "12"]},
    "zstd": {"fast": ["--zstd-level", "5"], "balanced": ["--zstd-level", "9"], "smallest": ["--zstd-level", "19"]},
}
FORGE_COMP["lz4ffpkg"] = FORGE_COMP["lz4"]
FORGE_COMP["zstdffpkg"] = FORGE_COMP["zstd"]


def forge_convert(exe, src, out_path, fmt, password, cancel, on_progress, on_log=None, extra=None):
    """Convierte con Forge. on_progress(etapa, hecho, total). Devuelve el informe (dict)."""
    args = [exe, "convert", src] + FORGE_FORMATS[fmt][0] + list(extra or []) + ["-o", out_path]
    state = {"report": None, "error": None}

    def on_line(s):
        try:
            ev = json.loads(s)
        except ValueError:
            return
        k = ev.get("kind")
        if k == "progress":
            on_progress(ev.get("stage") or "", int(ev.get("done") or 0), int(ev.get("total") or 0))
        elif k == "log" and on_log:
            on_log(str(ev.get("line") or ""))
        elif k == "done":
            r = ev.get("result") or {}
            if "Ok" in r:
                state["report"] = r["Ok"]
            else:
                state["error"] = str(r.get("Err") or "falló")
    code, _, err = _run_cancellable(args, _forge_env(password), cancel, on_line)
    if state["report"] is not None and code == 0:
        return state["report"]
    raise E.PipeError("PS5 Dump Forge: %s" % (state["error"] or _forge_err(err)))


# ---------------------------------------------------------------------------
# Diagnostico de red: por que va lento
# ---------------------------------------------------------------------------

def _ps(script, timeout=40):
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                          capture_output=True, timeout=timeout, creationflags=NO_WINDOW)


def _ping(ip, n=10):
    if os.name == "nt":
        args = ["ping", "-n", str(n), "-w", "1000", ip]
    else:
        # en Mac, -W va en milisegundos (en Linux, en segundos)
        args = ["ping", "-c", str(n), "-W", "1000" if sys.platform == "darwin" else "1", "-i", "0.3", ip]
    try:
        p = subprocess.run(args, capture_output=True, timeout=n * 2 + 10, creationflags=NO_WINDOW)
    except Exception:
        return None
    txt = p.stdout.decode("cp850" if os.name == "nt" else "utf-8", "replace")
    # Mac escribe "100.0% packet loss": con decimales
    loss = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:packet loss|loss|perdidos|p.rdidas|perdidas)", txt, re.I)
    avg = re.search(r"(?:Average|Media)\s*=\s*(\d+)\s*ms", txt, re.I)
    mx = re.search(r"(?:Maximum|M.ximo)\s*=\s*(\d+)\s*ms", txt, re.I)
    rtt = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", txt)
    out = {"loss": int(round(float(loss.group(1)))) if loss else None, "avg": None, "max": None}
    if avg:
        out["avg"] = float(avg.group(1))
    if mx:
        out["max"] = float(mx.group(1))
    if rtt:
        out["avg"], out["max"] = float(rtt.group(2)), float(rtt.group(3))
    return out


VPN_RX = re.compile(r"tap-|tun\b|wireguard|wintun|vpn|openvpn|tailscale|zerotier|fortinet|anyconnect|"
                    r"nordlynx|proton|hamachi|radmin|sonicwall|globalprotect|pangp", re.I)


def _wlan_info():
    try:
        p = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
    except Exception:
        return {}
    txt = p.stdout.decode("cp850", "replace")
    info = {}
    pats = {"signal": r"^\s*(?:Signal|Se.al)\s*:\s*(\d+)",
            "rx": r"^\s*(?:Receive rate|Velocidad de recepci.n)[^:]*:\s*([\d.,]+)",
            "tx": r"^\s*(?:Transmit rate|Velocidad de transmisi.n)[^:]*:\s*([\d.,]+)",
            "radio": r"^\s*(?:Radio type|Tipo de radio)\s*:\s*(.+)$",
            "band": r"^\s*(?:Band|Banda)\s*:\s*(.+)$",
            "channel": r"^\s*(?:Channel|Canal)\s*:\s*(\d+)",
            "ssid": r"^\s*SSID\s*:\s*(.+)$"}
    for k, rx in pats.items():
        m = re.search(rx, txt, re.I | re.M)
        if m:
            info[k] = m.group(1).strip()
    return info


def diagnose(ps5_ip, cfg, speed=None, active=0):
    """Lista de comprobaciones: {name, level (ok/warn/bad/info), detail, fix}."""
    items = []

    def add(name, level, detail, fix=""):
        items.append({"name": name, "level": level, "detail": detail, "fix": fix})

    ip = E.clean_host(ps5_ip or "")[0]
    if not ip:
        add("IP de la PS5", "bad", "No está puesta", "Ponla en Ajustes")
        return items
    pc_ip = lan_ip(ip)
    adapter = None
    if os.name == "nt":
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$r=Find-NetRoute -RemoteIPAddress '%s' | Where-Object {$_.InterfaceIndex} | Select-Object -First 1;"
            "$a=$null;$mtu=$null;if($r){$a=Get-NetAdapter -InterfaceIndex $r.InterfaceIndex;"
            "$mtu=(Get-NetIPInterface -InterfaceIndex $r.InterfaceIndex -AddressFamily IPv4).NlMtu};"
            "$up=@(Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object Name,InterfaceDescription,LinkSpeed);"
            "[pscustomobject]@{name=$a.Name;desc=$a.InterfaceDescription;speed=$a.LinkSpeed;"
            "media=[string]$a.MediaType;phys=[string]$a.PhysicalMediaType;mtu=$mtu;up=$up} | ConvertTo-Json -Depth 4 -Compress"
        ) % ip.replace("'", "")
        try:
            r = _ps(script)
            adapter = json.loads(r.stdout.decode("utf-8", "replace").strip() or "null")
        except Exception:
            adapter = None
    if adapter and adapter.get("name"):
        wifi = "802.11" in (adapter.get("phys") or "") or "wireless" in (adapter.get("media") or "").lower() \
            or re.search(r"wi-?fi|wireless|wlan|802\.11", adapter.get("desc") or "", re.I)
        spd = adapter.get("speed") or ""
        if wifi:
            w = _wlan_info()
            det = "El PC llega a la PS5 por Wi-Fi (%s)" % adapter.get("name")
            bits = []
            if w.get("ssid"):
                bits.append("red %s" % w["ssid"])
            if w.get("band"):
                bits.append(w["band"])
            elif w.get("channel"):
                bits.append("canal %s" % w["channel"])
            if w.get("signal"):
                bits.append("señal %s%%" % w["signal"])
            if w.get("tx"):
                bits.append("enlace %s Mb/s" % w["tx"])
            if bits:
                det += ": " + ", ".join(bits)
            add("Conexión del PC", "warn", det,
                "Con cable de red el PC suele enviar 2-3 veces más rápido: el Wi-Fi tiene que bajar el "
                "archivo y mandarlo a la PS5 por la misma radio.")
            try:
                if w.get("signal") and int(w["signal"]) < 60:
                    add("Señal Wi-Fi", "bad", "Señal del %s%%" % w["signal"],
                        "Acerca el PC al router o usa cable: con poca señal la velocidad cae mucho.")
            except ValueError:
                pass
            band = (w.get("band") or "") + " " + (w.get("radio") or "")
            ch = int(w["channel"]) if (w.get("channel") or "").isdigit() else None
            if "2.4" in band or "2,4" in band or (ch is not None and ch <= 14 and not re.search(r"5|6", w.get("band") or "")):
                add("Banda Wi-Fi", "bad", "Conectado a 2,4 GHz",
                    "Conéctate a la red de 5 GHz del router (suele ser 3-5 veces más rápida).")
        else:
            m = re.match(r"([\d.]+)\s*([GMK])bps", spd.replace(",", "."), re.I)
            mbps = None
            if m:
                mbps = float(m.group(1)) * {"G": 1000, "M": 1, "K": 0.001}[m.group(2).upper()]
            if mbps is not None and mbps <= 100:
                add("Cable de red", "bad", "El PC va por cable, pero el enlace está a %s" % spd,
                    "A 100 Mb/s el máximo son unos 11 MB/s. Prueba otro cable (Cat5e o mejor) u otro "
                    "puerto del router/switch: debería poner 1 Gbps.")
            else:
                add("Conexión del PC", "ok", "Por cable (%s, %s)" % (adapter.get("name"), spd or "?"))
        vpn = [u for u in (adapter.get("up") or []) if VPN_RX.search((u.get("InterfaceDescription") or "") + " " + (u.get("Name") or ""))]
        if vpn:
            add("VPN", "warn", "Hay una VPN o red virtual activa: %s" % ", ".join(u.get("Name") or "?" for u in vpn[:3]),
                "Si el tráfico a la PS5 o las descargas pasan por la VPN, todo va más lento. Desactívala "
                "mientras envías o excluye la red local.")
        if VPN_RX.search((adapter.get("desc") or "") + " " + (adapter.get("name") or "")):
            add("Ruta hacia la PS5", "bad", "El PC llega a la PS5 a través de %s" % adapter.get("name"),
                "La red local está yendo por la VPN: desactívala o permite el acceso a la red local.")
        try:
            if adapter.get("mtu") and int(adapter["mtu"]) < 1400:
                add("MTU", "warn", "MTU de %s bytes en esa conexión" % adapter["mtu"],
                    "Una MTU baja (VPN, PPPoE) hace los envíos más lentos en la red local.")
        except (TypeError, ValueError):
            pass
    elif os.name == "nt":
        add("Conexión del PC", "info", "No pude saber por qué conexión llega el PC a la PS5")
    else:
        add("Conexión del PC", "info", "Las comprobaciones de Wi-Fi y cable solo están en Windows")
    if pc_ip and not ip.startswith("127."):
        a, b = pc_ip.split(".")[:3], ip.split(".")[:3]
        if len(b) == 3 and a != b:
            add("Misma red", "warn", "El PC (%s) y la PS5 (%s) están en redes distintas" % (pc_ip, ip),
                "Si uno está en la red de invitados o en otro router/repetidor, el tráfico da un rodeo. "
                "Ponlos en la misma red.")
    pg = _ping(ip)
    if pg is None or pg.get("avg") is None:
        add("Ping a la PS5", "bad" if pg and pg.get("loss") == 100 else "info",
            "La PS5 no responde al ping" if pg and pg.get("loss") == 100 else "No pude medir el ping",
            "Comprueba la IP y que la PS5 esté encendida y en la misma red." if pg and pg.get("loss") == 100 else "")
    else:
        lvl = "ok"
        fix = ""
        det = "Media %.0f ms, máximo %.0f ms, %s%% perdidos" % (pg["avg"], pg.get("max") or pg["avg"], pg.get("loss") or 0)
        if (pg.get("loss") or 0) > 0:
            lvl, fix = "bad", "Se pierden paquetes: Wi-Fi saturado o con interferencias. Prueba cable o 5 GHz en la PS5."
        elif pg["avg"] > 15 or (pg.get("max") or 0) > 80:
            lvl, fix = "warn", ("Latencia alta para una red de casa: suele ser la PS5 por Wi-Fi lejos del router. "
                                "Con cable en la PS5 se nota mucho.")
        add("Ping a la PS5", lvl, det, fix)
    lim = float(cfg.get("limit_mbps") or 0)
    if lim > 0:
        add("Límite de velocidad", "warn", "Tienes puesto un límite de %g MB/s" % lim, "Ajustes > Límite (0 = sin límite)")
    if active:
        add("Otras transferencias", "info", "Hay %d transferencia(s) en marcha compartiendo la red" % active)
    if speed:
        mb = speed.get("up")
        if mb is not None:
            add("Último test PC → PS5", "ok" if mb >= 20 else ("warn" if mb >= 6 else "bad"), "%.1f MB/s" % mb,
                "" if mb >= 20 else "Por debajo de lo normal en red de casa: mira los avisos de arriba.")
        dl = speed.get("dl")
        if dl is not None and dl < 5:
            add("Último test de descarga", "warn", "%.1f MB/s desde el enlace" % dl,
                "El alojamiento da poca velocidad: no es tu red.")
    add("FTP de la consola", "info", "Si la PS5 escribe en un USB lento, ese disco limita la velocidad",
        "Prueba a enviar a /data/homebrew (disco interno) para comparar.")
    return items


# ---------------------------------------------------------------------------
# Energia: no dormir mientras hay trabajo y apagar/suspender al acabar
# ---------------------------------------------------------------------------

_AWAKE = {"proc": None}


def keep_awake(on):
    """Que el PC no se suspenda mientras hay envios. En Linux/Mac se hace con un proceso
    (systemd-inhibit / caffeinate) que dura mientras haga falta."""
    if os.name == "nt":
        try:
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
            return True
        except Exception:
            return False
    p = _AWAKE["proc"]
    if not on:
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
            _AWAKE["proc"] = None
        return True
    if p is not None and p.poll() is None:
        return True
    if sys.platform == "darwin":
        cmd = ["caffeinate", "-i", "-w", str(os.getpid())]
    elif shutil.which("systemd-inhibit"):
        cmd = ["systemd-inhibit", "--what=sleep:idle", "--who=PS5 Directo", "--why=Enviando a la PS5",
               "--mode=block", "sleep", "infinity"]
    else:
        return False
    try:
        _AWAKE["proc"] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def power_action(kind):
    """'sleep' (suspender) o 'shutdown' (apagar). Devuelve un texto de lo hecho."""
    if os.name == "nt":
        if kind == "shutdown":
            subprocess.Popen(["shutdown", "/s", "/t", "0", "/c", "PS5 Directo: cola terminada"], creationflags=NO_WINDOW)
            return "Apagando el PC"
        if kind == "sleep":
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=NO_WINDOW)
            return "Suspendiendo el PC"
        return ""
    keep_awake(False)
    if sys.platform == "darwin":
        cmd = {"shutdown": ["osascript", "-e", 'tell application "System Events" to shut down'],
               "sleep": ["pmset", "sleepnow"]}.get(kind)
    else:
        cmd = {"shutdown": ["systemctl", "poweroff"], "sleep": ["systemctl", "suspend"]}.get(kind)
    if not cmd:
        return ""
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return "No pude: %s" % e
    return "Apagando el PC" if kind == "shutdown" else "Suspendiendo el PC"


def open_path(path):
    """Abre una carpeta o archivo del PC con el programa del sistema (Explorador, Finder...)."""
    if os.name == "nt":
        os.startfile(path)
        return
    cmd = ["open", path] if sys.platform == "darwin" else ["xdg-open", path]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal_path(path):
    """Abre la carpeta donde esta un archivo con el archivo ya marcado (o la propia carpeta si es una)."""
    if os.path.isdir(path):
        return open_path(path)
    if os.name == "nt":
        subprocess.Popen('explorer /select,"%s"' % os.path.normpath(path), creationflags=E._NOWIN)
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return
    open_path(os.path.dirname(path) or path)


def pick_native(kind, title):
    """Selector de archivos/carpeta del sistema en Linux (zenity/kdialog) o Mac (osascript).
    Devuelve la lista elegida ([] si se cancela) o None si no hay ninguno disponible."""
    title = title or "PS5 Directo"
    if sys.platform == "darwin":
        if kind == "folder":
            script = 'POSIX path of (choose folder with prompt %s)' % _as_str(title)
        else:
            script = ('set l to choose file with prompt %s with multiple selections allowed\n'
                      'set o to ""\nrepeat with f in l\nset o to o & POSIX path of f & linefeed\nend repeat\nreturn o'
                      % _as_str(title))
        args = ["osascript", "-e", "activate"]   # que el selector salga delante del navegador
        for ln in script.split("\n"):
            args += ["-e", ln]
        p = subprocess.run(args, capture_output=True, timeout=3600)
        if p.returncode != 0:
            return []
        return [x.rstrip("/") or "/" for x in p.stdout.decode("utf-8", "replace").splitlines() if x.strip()]
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--title=" + title, "--separator=\n"]
        cmd += ["--directory"] if kind == "folder" else ["--multiple"]
    elif shutil.which("kdialog"):
        home = os.path.expanduser("~")
        cmd = (["kdialog", "--title", title, "--getexistingdirectory", home] if kind == "folder" else
               ["kdialog", "--title", title, "--getopenfilename", home, "--multiple", "--separate-output"])
    else:
        return None
    p = subprocess.run(cmd, capture_output=True, timeout=3600)
    if p.returncode != 0:
        return []
    return [x for x in p.stdout.decode("utf-8", "replace").splitlines() if x.strip()]


def _as_str(t):
    return '"' + str(t).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Elegir archivos del PC (Windows sin tkinter)
# ---------------------------------------------------------------------------

def pick_windows(kind, title):
    t = (title or "").replace("'", "''")
    if kind == "folder":
        body = ("$d=New-Object System.Windows.Forms.FolderBrowserDialog;$d.Description='%s';"
                "$d.ShowNewFolderButton=$false;if($d.ShowDialog($f) -eq 'OK'){$out=@($d.SelectedPath)}") % t
    else:
        body = ("$d=New-Object System.Windows.Forms.OpenFileDialog;$d.Title='%s';$d.Multiselect=$true;"
                "if($d.ShowDialog($f) -eq 'OK'){$out=@($d.FileNames)}") % t
    script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;Add-Type -AssemblyName System.Windows.Forms;"
              "$f=New-Object System.Windows.Forms.Form -Property @{TopMost=$true;ShowInTaskbar=$false};"
              "$out=@();" + body + ";ConvertTo-Json -InputObject @($out) -Compress")
    p = subprocess.run(["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, timeout=3600, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise E.PipeError((p.stderr.decode("utf-8", "replace").strip().splitlines() or ["?"])[-1])
    res = json.loads(p.stdout.decode("utf-8", "replace").strip() or "[]")
    if isinstance(res, str):
        res = [res]
    return [os.path.normpath(x) for x in res if x]


# ---------------------------------------------------------------------------
# Firewall de Windows para abrir la app desde el movil
# ---------------------------------------------------------------------------

FW_RULE = "PS5 Directo (movil)"


def _python_exes():
    """python.exe y pythonw.exe con los que corre PS5 Directo (los que Windows puede tener bloqueados)."""
    out = []
    d = os.path.dirname(os.path.abspath(sys.executable))
    for n in (os.path.basename(sys.executable), "python.exe", "pythonw.exe"):
        p = os.path.join(d, n)
        if os.path.isfile(p) and p not in out:
            out.append(p)
    return out


def _ps_list(items):
    return "@(" + ",".join("'%s'" % x.replace("'", "''") for x in items) + ")"


_FW_FIND_BLOCKS = (
    "$exes=%s;"
    "$blk=@(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | Where-Object { $p=$_.Program; "
    "($exes | Where-Object { $_ -ieq $p }).Count -gt 0 } | Get-NetFirewallRule -ErrorAction SilentlyContinue | "
    "Where-Object { $_.Direction -eq 'Inbound' -and $_.Action -eq 'Block' -and $_.Enabled -eq 'True' });")


def firewall_status(port):
    """{'rule': hay permiso para el puerto, 'blocked': Windows tiene una regla que bloquea Python,
    'public': la red esta como Publica}. Las reglas de bloqueo ganan a las de permitir."""
    if os.name != "nt":
        return {"rule": True, "blocked": False, "public": False}
    script = ("$ErrorActionPreference='SilentlyContinue';" + _FW_FIND_BLOCKS % _ps_list(_python_exes()) +
              "$r=@(Get-NetFirewallRule -DisplayName '%s' | Where-Object {$_.Enabled -eq 'True'}).Count;"
              "$p=@(Get-NetConnectionProfile | Where-Object {$_.NetworkCategory -eq 'Public'}).Count;"
              "[pscustomobject]@{rule=$r;blocked=$blk.Count;public=$p} | ConvertTo-Json -Compress") % FW_RULE
    try:
        j = json.loads(_ps(script, timeout=40).stdout.decode("utf-8", "replace").strip() or "{}")
        return {"rule": int(j.get("rule") or 0) > 0, "blocked": int(j.get("blocked") or 0) > 0,
                "public": int(j.get("public") or 0) > 0}
    except Exception:
        return {"rule": True, "blocked": False, "public": False}


def firewall_allow(port):
    """Con permiso de administrador: quita los bloqueos de Python que pone Windows si se cancela su aviso
    y crea el permiso de entrada (puerto y programa) en redes privadas y publicas."""
    if os.name != "nt":
        return True
    import base64
    exes = _python_exes()
    lines = ["$ErrorActionPreference='SilentlyContinue'",
             _FW_FIND_BLOCKS % _ps_list(exes) + "$blk | Remove-NetFirewallRule",
             "Get-NetFirewallRule -DisplayName '%s' | Remove-NetFirewallRule" % FW_RULE,
             "New-NetFirewallRule -DisplayName '%s' -Direction Inbound -Action Allow -Protocol TCP -LocalPort %d "
             "-Profile Private,Public | Out-Null" % (FW_RULE, int(port))]
    for e in exes:
        lines.append("New-NetFirewallRule -DisplayName '%s' -Direction Inbound -Action Allow -Program '%s' "
                     "-Profile Private,Public | Out-Null" % (FW_RULE, e.replace("'", "''")))
    enc = base64.b64encode(";".join(lines).encode("utf-16-le")).decode()
    script = ("Start-Process -FilePath powershell.exe -Verb RunAs -WindowStyle Hidden -Wait "
              "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','%s'" % enc)
    try:
        _ps(script, timeout=180)
    except Exception:
        pass
    st = firewall_status(port)
    return st["rule"] and not st["blocked"]


# ---------------------------------------------------------------------------
# Portapapeles (para «Captar enlaces que copies»)
# ---------------------------------------------------------------------------

def clipboard_text():
    """Texto del portapapeles del PC, o None si no se puede leer en este sistema."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        u32.GetClipboardData.restype = wintypes.HANDLE
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [wintypes.HANDLE]
        k32.GlobalUnlock.argtypes = [wintypes.HANDLE]
        if not u32.IsClipboardFormatAvailable(13):        # CF_UNICODETEXT
            return ""
        for _ in range(3):
            if u32.OpenClipboard(None):
                break
            time.sleep(0.05)
        else:
            return None
        try:
            h = u32.GetClipboardData(13)
            if not h:
                return ""
            p = k32.GlobalLock(h)
            if not p:
                return ""
            try:
                return ctypes.wstring_at(p)[:200000]
            finally:
                k32.GlobalUnlock(h)
        finally:
            u32.CloseClipboard()
    if sys.platform == "darwin":
        cmds = [["pbpaste"]]
    else:
        cmds = [["wl-paste", "-n"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-bo"]]
    for c in cmds:
        if shutil.which(c[0]):
            try:
                p = subprocess.run(c, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   timeout=3)
                return p.stdout.decode("utf-8", "replace")[:200000] if p.returncode == 0 else ""
            except Exception:
                return None
    return None


def clipboard_seq():
    """En Windows, un numero que cambia cada vez que se copia algo (para no leer el portapapeles sin parar)."""
    if os.name == "nt":
        import ctypes
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    return None


# enlaces que parecen una descarga de verdad: archivos comprimidos, partes, imagenes y paquetes, torrents
CLIP_LINK_RE = re.compile(
    r"(?i)\b(?:(?:https?|ftp)://[^\s\"'<>]+?\.(?:rar|zip|7z|tar|tgz|gz|xz|zst|001|\d{3}|r\d\d|pkg|exfat|ffpkg|ffpfs|ffpfsc"
    r"|iso|torrent)(?:\?[^\s\"'<>]*)?(?=[\s\"'<>,;)\]]|\.(?:\s|$)|$)|magnet:\?xt=urn:[^\s\"'<>]+)")


def clipboard_links(text):
    seen, out = set(), []
    for m in CLIP_LINK_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,);]")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:300]


# ---------------------------------------------------------------------------
# Direccion fisica (MAC) de la tarjeta de red con esa IP: la app del movil la guarda para despertar el PC
# (Wake-on-LAN) si esta en reposo.
# ---------------------------------------------------------------------------

def mac_for_ip(ip):
    mac = ""
    try:
        if os.name == "nt":
            out = subprocess.run(["ipconfig", "/all"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=15, creationflags=E._NOWIN).stdout
            txt = out.decode("cp850", "replace")
            for block in re.split(r"\r?\n(?=\S)", txt):
                if re.search(r"\b%s\b" % re.escape(ip), block):
                    m = re.search(r"([0-9A-F]{2}(?:-[0-9A-F]{2}){5})", block, re.I)
                    if m:
                        mac = m.group(1)
                        break
        elif sys.platform == "darwin":
            out = subprocess.run(["ifconfig"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15).stdout
            for block in re.split(r"\n(?=\S)", out.decode("utf-8", "replace")):
                if "inet %s " % ip in block:
                    m = re.search(r"ether ([0-9a-f:]{17})", block)
                    if m:
                        mac = m.group(1)
                        break
        else:
            out = subprocess.run(["ip", "-o", "addr"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15).stdout
            for line in out.decode("utf-8", "replace").splitlines():
                if " %s/" % ip in line:
                    dev = line.split()[1]
                    with open("/sys/class/net/%s/address" % dev) as fh:
                        mac = fh.read().strip()
                    break
    except Exception:
        pass
    mac = mac.replace("-", ":").lower()
    return mac if re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac) and mac != "00:00:00:00:00:00" else ""



# ---------------------------------------------------------------------------
# Nubes con rclone (rclone.org, libre): Google Drive, OneDrive, Dropbox, MEGA, pCloud, Box, SFTP, WebDAV...
# PS5 Directo no guarda tus contrasenas: la configuracion es la de rclone (rclone.conf, en tu PC).
# ---------------------------------------------------------------------------

CLOUD_TYPES = [
    {"type": "drive", "name": "Google Drive", "oauth": True},
    {"type": "onedrive", "name": "OneDrive", "oauth": True},
    {"type": "dropbox", "name": "Dropbox", "oauth": True},
    {"type": "pcloud", "name": "pCloud", "oauth": True},
    {"type": "box", "name": "Box", "oauth": True},
    {"type": "mega", "name": "MEGA", "fields": ["user", "pass"]},
    {"type": "webdav", "name": "WebDAV / Nextcloud", "fields": ["url", "user", "pass"]},
    {"type": "sftp", "name": "SFTP (tu servidor o NAS)", "fields": ["host", "user", "pass"]},
]


def mac_prepare_exe(path):
    """Mac: un programa bajado (Forge, rclone) se prepara para que macOS lo deje abrir: sin la marca
    de «descargado de internet» y, si no trae firma, con una firma local (en los Mac con chip de
    Apple, un programa sin ninguna firma se cierra nada mas abrirlo)."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(["xattr", "-d", "com.apple.quarantine", path], capture_output=True, timeout=15)
        if subprocess.run(["codesign", "-v", path], capture_output=True, timeout=30).returncode != 0:
            subprocess.run(["codesign", "--force", "--sign", "-", path], capture_output=True, timeout=60)
    except Exception:
        pass


def mac_activate(pid):
    """Mac: trae al frente la ventana de un programa por su numero de proceso (sin pedir permisos)."""
    if sys.platform != "darwin" or not pid:
        return False
    try:
        import ctypes
        import ctypes.util
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        get = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)(addr)
        act = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)(addr)
        app = get(objc.objc_getClass(b"NSRunningApplication"),
                  objc.sel_registerName(b"runningApplicationWithProcessIdentifier:"), int(pid))
        if not app:
            return False
        return bool(act(app, objc.sel_registerName(b"activateWithOptions:"), 3))  # todas las ventanas, delante
    except Exception:
        return False


def rclone_exe_name():
    return "rclone.exe" if os.name == "nt" else "rclone"


def rclone_path(data_dir):
    mine = os.path.join(data_dir, "tools", "rclone", rclone_exe_name())
    if os.path.isfile(mine):
        return mine
    w = shutil.which("rclone")
    if w:
        return w
    for c in ("/opt/homebrew/bin/rclone", "/usr/local/bin/rclone", "/usr/bin/rclone"):
        if os.name != "nt" and os.path.isfile(c):
            return c
    return None


def rclone_asset_rx():
    import platform
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "386" if m in ("i386", "i686", "x86") else "amd64"
    osn = "windows" if os.name == "nt" else "osx" if sys.platform == "darwin" else "linux"
    return [r"(?i)^rclone-v[\d.]+-%s-%s\.zip$" % (osn, arch)]


def rclone_install(data_dir, progress=None):
    """Baja rclone de su GitHub oficial y deja el ejecutable en la carpeta de datos."""
    tag, assets = github_release("rclone/rclone")
    a = pick_asset(assets, rclone_asset_rx())
    if not a:
        raise E.PipeError("No encuentro rclone %s para este sistema en su GitHub" % tag)
    d = os.path.join(data_dir, "tools", "rclone")
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "rclone.zip.part")
    total = int(a.get("size") or 0)
    http_get(a["browser_download_url"], limit=200 * MiB, timeout=120, dest=tmp,
             progress=(lambda got: progress(got, total)) if progress else None)
    with zipfile.ZipFile(tmp) as z:
        inner = next((n for n in z.namelist() if n.rsplit("/", 1)[-1] == rclone_exe_name()), None)
        if not inner:
            raise E.PipeError("El paquete de rclone no trae el ejecutable")
        dst = os.path.join(d, rclone_exe_name())
        with z.open(inner) as fi, open(dst + ".new", "wb") as fo:
            shutil.copyfileobj(fi, fo)
    os.replace(dst + ".new", dst)
    os.remove(tmp)
    if os.name != "nt":
        os.chmod(dst, 0o755)
        mac_prepare_exe(dst)
    return dst, tag


def rclone_run(exe, args, timeout=60, input_text=None):
    p = subprocess.run([exe] + list(args), capture_output=True, timeout=timeout, creationflags=NO_WINDOW,
                       input=input_text.encode() if input_text is not None else None)
    out = p.stdout.decode("utf-8", "replace")
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise E.PipeError("rclone: %s" % (err[-1] if err else "error %d" % p.returncode))
    return out


def open_terminal(cmd):
    """Abre una ventana de terminal con una orden (para «rclone config», que es interactivo)."""
    if os.name == "nt":
        subprocess.Popen(["cmd", "/c", "start", "rclone", "cmd", "/k"] + cmd)
        return True
    if sys.platform == "darwin":
        # un archivo .command se abre en Terminal con «open», sin pedir permiso para controlar Terminal
        import tempfile
        q = " ".join("'%s'" % c.replace("'", "'\\''") for c in cmd)
        fd, fp = tempfile.mkstemp(prefix="ps5directo-", suffix=".command")
        with os.fdopen(fd, "w") as fh:
            fh.write("#!/bin/bash\nclear\n%s\necho\nread -n 1 -s -r -p 'Pulsa una tecla para cerrar / Press any key'\n"
                     "rm -f \"$0\"\n" % q)
        os.chmod(fp, 0o700)
        subprocess.Popen(["open", "-a", "Terminal", fp])
        return True
    for t in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["konsole", "-e"], ["xfce4-terminal", "-x"],
              ["xterm", "-e"]):
        if shutil.which(t[0]):
            subprocess.Popen(t + cmd)
            return True
    return False


# Arte oficial de cada proyecto (icono y portada de su propio GitHub; Bazzite, de su «press kit»). Si falta, se usa el
# del catálogo de RecompStore para ese repositorio o el icono de la consola si ya está instalado.
CATALOG_ART = {
    'crux2': {"icon": 'https://raw.githubusercontent.com/Press5elect/Crux2/main/ps5/sce_sys/icon0.png', "hero": ''},
    'prosperoeden': {"icon": 'https://raw.githubusercontent.com/heydemoura/ProsperoEden/main/sce_sys/icon0.png', "hero": ''},
    'ps5sx2': {"icon": 'https://raw.githubusercontent.com/Swordpdf/PS5SX2/main/ps5/sce_sys/icon0.png', "hero": 'https://raw.githubusercontent.com/Swordpdf/PS5SX2/main/ps5/docs/banner.png'},
    'psxs5': {"icon": 'https://raw.githubusercontent.com/SynoPiia/PSXS5/main/sce_sys/icon0.png', "hero": 'https://raw.githubusercontent.com/SynoPiia/PSXS5/main/docs/media/banner.png'},
    'psp5': {"icon": 'https://raw.githubusercontent.com/tsuramatsu1/PSP5/master/sce_sys/icon0.png', "hero": ''},
    'porpoise': {"icon": 'https://raw.githubusercontent.com/elripalda/Porpoise/Main/sce_sys/icon0.png', "hero": 'https://raw.githubusercontent.com/elripalda/Porpoise/Main/docs/images/banner.png'},
    'mupen64': {"icon": 'https://raw.githubusercontent.com/TheRealRetro/mupen64plus-ps5/main/ps5/app/sce_sys/icon0.png', "hero": 'https://raw.githubusercontent.com/TheRealRetro/mupen64plus-ps5/main/docs/banner.png'},
    'snes9x': {"icon": 'https://raw.githubusercontent.com/MisterTemaki/snes9xPS5/main/ps5/app/sce_sys/icon0.png', "hero": ''},
    'eglantine': {"icon": 'https://raw.githubusercontent.com/mucahidkambur/EglantinePS5/main/sce_sys/icon0.png', "hero": 'https://raw.githubusercontent.com/mucahidkambur/EglantinePS5/main/sce_sys/background-source.png'},
    'ecam': {"icon": 'https://raw.githubusercontent.com/premohq/PS5-ECAM/PS5-ECAM/sce_sys/icon0.png', "hero": 'https://raw.githubusercontent.com/premohq/PS5-ECAM/PS5-ECAM/docs/banner.png'},
    'psflycast': {"icon": 'https://raw.githubusercontent.com/Press5elect/PSFlycast/main/shell/ps5/sce_sys/icon0.png', "hero": ''},
    'genplus': {"icon": 'https://raw.githubusercontent.com/MisterTemaki/genplusgxPS5/main/ps5/app/sce_sys/icon0.png', "hero": ''},
    'fbneo': {"icon": 'https://raw.githubusercontent.com/MisterTemaki/fbneo-ps5/main/ps5/app/sce_sys/icon0.png', "hero": 'https://raw.githubusercontent.com/MisterTemaki/fbneo-ps5/main/ps5/docs/banner.jpg'},
    'pce': {"icon": 'https://raw.githubusercontent.com/MisterTemaki/beetlepcePS5/main/ps5/app/sce_sys/icon0.png', "hero": 'https://raw.githubusercontent.com/MisterTemaki/beetlepcePS5/main/docs/banner.jpg'},
    'ps5x360': {"icon": 'https://raw.githubusercontent.com/BrinooTk/PS5X360/main/assets/branding/icon0.png', "hero": 'https://raw.githubusercontent.com/BrinooTk/PS5X360/main/assets/branding/pic0.png'},
    'xpsemu': {"icon": 'https://raw.githubusercontent.com/ZiZc3/XPSemu/main/ps5/sce_sys/icon0.png', "hero": ''},
    'retroarch': {"icon": 'https://raw.githubusercontent.com/mihawk-99/PS5_RetroArch/main/sce_sys/icon0.png', "hero": ''},
    'ruffle': {"icon": 'https://raw.githubusercontent.com/ZiZc3/Ruffle-Flash-PS5/main/ps5/sce_sys/icon0.png', "hero": ''},
    'vlc': {"icon": 'https://raw.githubusercontent.com/ZiZc3/VLC/main/ps5/sce_sys/icon0.png', "hero": ''},
    'jelly5': {"icon": 'https://raw.githubusercontent.com/02dnot/Jelly5/main/app/sce_sys/icon0.png', "hero": ''},
    'emby5': {"icon": 'https://raw.githubusercontent.com/bornaradusin/Emby5/main/app/sce_sys/icon0.png', "hero": 'https://raw.githubusercontent.com/bornaradusin/Emby5/main/app/sce_sys/pic0.png'},
    'pshare': {"icon": 'https://raw.githubusercontent.com/StonedModder/PShare/main/sce_sys/icon0.png', "hero": 'https://raw.githubusercontent.com/StonedModder/PShare/main/docs/banner.png'},
    'chiral': {"icon": 'https://raw.githubusercontent.com/Herbrax/Chiral-FTP/main/ps5/sce_sys/icon0.png', "hero": ''},
    'chiaki': {"icon": 'https://raw.githubusercontent.com/streetpea/chiaki-ng/main/assets/official-artwork/Chiaki-ng%20logo%20color.png', "hero": 'https://raw.githubusercontent.com/streetpea/chiaki-ng/main/gui/res/chiaking-logo.svg', "bg": ["#0e1626", "#05080f"]},
    'patchdl': {"icon": 'https://raw.githubusercontent.com/knutwurst/ps5-patchdl/main/assets/icon0.png', "hero": ''},
    'forge': {"icon": 'https://raw.githubusercontent.com/quer3q/ps5-dump-forge/main/ps5/icon0.png', "hero": ''},
    'pkgmanager': {"icon": 'https://raw.githubusercontent.com/itsPLK/ps5-pkg-manager/main/assets/icon0.png', "hero": ''},
    'ps5linux': {"bg": ["#2b2f3a", "#0f1116"], "icon": 'https://raw.githubusercontent.com/ps5-linux/ps5-linux-loader/main/logo.webp', "hero": ''},
    'bazzite': {"icon": 'https://raw.githubusercontent.com/ublue-os/bazzite/main/press_kit/Bazzite_Color.svg', "hero": 'https://raw.githubusercontent.com/ublue-os/bazzite/main/press_kit/Bazzite_Color_Long.svg', "bg": ["#6a3fd8", "#24196b"]},
    'mihawkos': {"icon": 'https://raw.githubusercontent.com/mihawk-99/PS5_MihawkOS/main/ps5/sce_sys/icon0.png', "hero": ''},
    'smplusgui': {"icon": 'https://raw.githubusercontent.com/KarnerF/SMPlusGui/main/assets-app/icon1.png', "hero": ''},
    'tailscale': {"icon": 'https://raw.githubusercontent.com/holdmysocks/ps5-tailscale/main/appicon/icon0.png', "hero": ''},
    'patchsys': {"icon": '', "hero": 'https://github.com/user-attachments/assets/fd047e71-391e-46d2-91a8-5bde99dfdf7d'},
    'neoenabler': {"icon": 'https://raw.githubusercontent.com/gasb07/NeoEnabler/main/assets/icon0.png', "hero": 'https://raw.githubusercontent.com/gasb07/NeoEnabler/main/docs/panel-games.jpg'},
    'doom': {"icon": '', "hero": '', "bg": ["#7a1e12", "#1c0805"]},
    'zftpd': {"icon": 'https://raw.githubusercontent.com/seregonwar/zftpd/main/assets/zftpd-logo.png', "hero": ''},
}
