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
    }


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


def github_release(repo):
    """(tag, [assets]) de la ultima version publicada (si solo hay pruebas, la mas nueva)."""
    base = "https://api.github.com/repos/%s/releases" % repo
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
    p = next((x for x in PAYLOADS if x["id"] == pid), None)
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
    elf = _pick_elf(list(names))
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


PLDMGR_PORT = 8084
PAYLOAD_RX = {"ftpsrv": re.compile(r"ftpsrv", re.I), "bfpilot": re.compile(r"bfpilot", re.I)}


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
}
FORGE_STAGES = {"extract": "leyendo el comprimido", "scan": "analizando", "preflight": "comprobando",
                "measure": "midiendo (LZ4)", "write": "escribiendo", "pack": "empaquetando LZ4",
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


def _run_cancellable(args, env, cancel, on_line=None, timeout=None):
    """Ejecuta un programa leyendo su salida linea a linea. Devuelve (codigo, salida, errores)."""
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         env=env, creationflags=NO_WINDOW)
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
}


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
        args = ["ping", "-c", str(n), "-W", "1", "-i", "0.3", ip]
    try:
        p = subprocess.run(args, capture_output=True, timeout=n * 2 + 10, creationflags=NO_WINDOW)
    except Exception:
        return None
    txt = p.stdout.decode("cp850" if os.name == "nt" else "utf-8", "replace")
    loss = re.search(r"(\d+)\s*%\s*(?:packet loss|loss|perdidos|p.rdidas|perdidas)", txt, re.I)
    avg = re.search(r"(?:Average|Media)\s*=\s*(\d+)\s*ms", txt, re.I)
    mx = re.search(r"(?:Maximum|M.ximo)\s*=\s*(\d+)\s*ms", txt, re.I)
    rtt = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", txt)
    out = {"loss": int(loss.group(1)) if loss else None, "avg": None, "max": None}
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
        args = ["osascript"]
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
