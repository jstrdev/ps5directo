"""Diagnóstico de la red para envíos lentos (idea de PS5Upload, «Diagnose slow transfers»): por qué tarjeta de red
sale el PC hacia la PS5, si es una VPN o un adaptador virtual, si va por cable o por Wi-Fi y a qué velocidad, y si
la latencia se dispara cuando la red va cargada (el Wi-Fi o el router se atascan). Todo se mira en el PC: nada se
cambia. Cada hallazgo: {"id", "level": ok|warn|bad|skip, "text"}.
"""
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import threading
import time

VPN_RX = re.compile(r"(?i)\b(vpn|tap-?windows|tap\d|tun\d*|wintun|wireguard|wg\d+|tailscale|zerotier|openvpn|"
                    r"nordlynx|proton|mullvad|hamachi|radmin|fortinet|forticlient|anyconnect|cisco|globalprotect|"
                    r"pangp|sonicwall|warp|cloudflare)\b")
VIRTUAL_RX = re.compile(r"(?i)(hyper-v|vethernet|virtualbox|vmware|vbox|docker|wsl|loopback|npcap)")
_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}


def _run(args, timeout=8):
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, **_NOWIN)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""
    except Exception:
        return ""


def local_ip_for(target):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


def adapter_for(ip):
    """{"name", "desc", "wifi", "mbps", "signal", "band", "radio"} de la tarjeta que tiene esa IP (lo que se sepa)."""
    out = {"name": "", "desc": "", "wifi": None, "mbps": None, "signal": None, "band": "", "radio": ""}
    if not ip:
        return out
    if os.name == "nt":
        ps = ("$a = Get-NetIPAddress -IPAddress '%s' -ErrorAction SilentlyContinue | Get-NetAdapter; "
              "$a | Select-Object Name,InterfaceDescription,LinkSpeed,MediaType,PhysicalMediaType | ConvertTo-Json"
              % ip.replace("'", ""))
        txt = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], 12)
        try:
            j = json.loads(txt) if txt.strip() else {}
            j = j[0] if isinstance(j, list) and j else j
        except ValueError:
            j = {}
        out["name"], out["desc"] = j.get("Name") or "", j.get("InterfaceDescription") or ""
        media = "%s %s" % (j.get("MediaType") or "", j.get("PhysicalMediaType") or "")
        out["wifi"] = bool(re.search(r"802\.11|Native 802|Wireless|Wi-?Fi", media + " " + out["desc"], re.I)) \
            if (j or out["desc"]) else None
        m = re.match(r"([\d.,]+)\s*([GMK])bps", str(j.get("LinkSpeed") or ""))
        if m:
            v = float(m.group(1).replace(",", "."))
            out["mbps"] = v * {"G": 1000, "M": 1, "K": 0.001}[m.group(2)]
        if out["wifi"]:
            w = _run(["netsh", "wlan", "show", "interfaces"])
            # bloque de esa tarjeta (en el idioma de Windows: se buscan los valores, no las palabras)
            sig = re.search(r"(\d{1,3})\s*%", w)
            out["signal"] = int(sig.group(1)) if sig else None
            if re.search(r"802\.11(ax|be)", w):
                out["radio"] = "Wi-Fi 6/7"
            elif re.search(r"802\.11ac", w):
                out["radio"] = "Wi-Fi 5"
            elif re.search(r"802\.11n", w):
                out["radio"] = "Wi-Fi 4"
            b = re.search(r"(2[.,]4|5|6)\s*GHz", w)
            out["band"] = (b.group(1).replace(",", ".") + " GHz") if b else ""
            rates = [float(x.replace(",", ".")) for x in re.findall(r":\s*([\d.,]+)\s*\n", w)
                     if re.fullmatch(r"\d{2,5}([.,]\d+)?", x.strip())]
            if rates:
                out["mbps"] = max(rates)
    elif sys.platform.startswith("linux"):
        txt = _run(["ip", "-o", "-4", "addr", "show"])
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+%s/" % re.escape(ip), txt, re.M)
        if m:
            name = m.group(1)
            out["name"] = name
            out["wifi"] = os.path.isdir("/sys/class/net/%s/wireless" % name)
            try:
                sp = int(open("/sys/class/net/%s/speed" % name).read().strip())
                out["mbps"] = sp if sp > 0 else None
            except Exception:
                pass
    elif sys.platform == "darwin":
        txt = _run(["ifconfig"])
        name = ""
        for block in re.split(r"\n(?=\S)", txt):
            if re.search(r"inet %s\b" % re.escape(ip), block):
                name = block.split(":", 1)[0]
        out["name"] = name
        hw = _run(["networksetup", "-listallhardwareports"])
        m = re.search(r"Hardware Port: (.+)\nDevice: %s\b" % re.escape(name), hw) if name else None
        if m:
            out["desc"] = m.group(1)
            out["wifi"] = bool(re.search(r"Wi-?Fi|AirPort", m.group(1), re.I))
    return out


def rtt_ms(host, port, timeout=1.5):
    t = time.perf_counter()
    try:
        socket.create_connection((host, port), timeout).close()
    except ConnectionRefusedError:
        pass                                       # contesta igual: el tiempo vale
    except OSError:
        return None
    return (time.perf_counter() - t) * 1000


class LoadSampler:
    """Mide la latencia hacia la consola mientras se envía (para ver si la red se atasca con carga)."""
    def __init__(self, host, port):
        self.host, self.port, self.samples, self._stop = host, port, [], threading.Event()
        self._th = None

    def __enter__(self):
        def run():
            while not self._stop.wait(0.4):
                v = rtt_ms(self.host, self.port)
                if v is not None:
                    self.samples.append(v)
        self._th = threading.Thread(target=run, daemon=True)
        self._th.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._th.join(3)


def idle_rtt(host, port, n=8):
    vals = []
    for _ in range(n):
        v = rtt_ms(host, port)
        if v is not None:
            vals.append(v)
        time.sleep(0.15)
    return statistics.median(vals) if vals else None


def findings(host, idle=None, loaded=None, up_mbs=None):
    """Lo que se ha visto, lo más importante primero."""
    out = []
    ip = local_ip_for(host)
    ad = adapter_for(ip)
    label = ad["desc"] or ad["name"] or ""
    both = "%s %s" % (ad["name"], ad["desc"])
    if not ip:
        out.append({"id": "route", "level": "bad", "text": "Este PC no encuentra camino hacia %s." % host})
        return out
    if VPN_RX.search(both):
        out.append({"id": "vpn", "level": "bad",
                    "text": "Hacia la PS5 sales por una VPN (%s). Va más lento y a veces ni llega: desactívala "
                            "mientras envías o configúrala para que no tape la red de casa." % label})
    elif VIRTUAL_RX.search(both):
        out.append({"id": "virtual", "level": "warn",
                    "text": "Hacia la PS5 sales por un adaptador virtual (%s). Si va lento, revisa la red de "
                            "Hyper-V, VirtualBox o similares." % label})
    if host.count(".") == 3 and ip.rsplit(".", 1)[0] != host.rsplit(".", 1)[0]:
        out.append({"id": "subnet", "level": "warn",
                    "text": "Tu PC (%s) y la PS5 (%s) no están en la misma red: cada salto entre routers resta "
                            "velocidad." % (ip, host)})
    if ad["wifi"] is True:
        bits = [x for x in (ad["radio"], ad["band"], ("%d Mb/s" % ad["mbps"]) if ad["mbps"] else "",
                            ("señal %d %%" % ad["signal"]) if ad["signal"] is not None else "") if x]
        lv, extra = "ok", ""
        if ad["signal"] is not None and ad["signal"] < 55:
            lv, extra = "warn", " La señal es floja: acerca el PC al router o usa cable."
        elif ad["band"] == "2.4 GHz":
            lv, extra = "warn", " Estás en 2,4 GHz, la banda lenta: conéctate a la de 5 GHz (o 6 GHz) si tu router la tiene."
        elif ad["mbps"] and ad["mbps"] < 300:
            lv, extra = "warn", " El enlace es lento: con cable o 5 GHz iría bastante más rápido."
        out.append({"id": "link", "level": lv,
                    "text": "El PC va por Wi-Fi%s.%s El cable de red es lo que más mejora la velocidad." % (
                        (" (" + ", ".join(bits) + ")") if bits else "", extra)})
    elif ad["wifi"] is False:
        if ad["mbps"] and ad["mbps"] < 900:
            out.append({"id": "link", "level": "warn",
                        "text": "El PC va por cable, pero a %d Mb/s (como mucho unos %d MB/s). Suele ser un cable "
                                "viejo o mal puesto, o un puerto lento del router: con 1 Gb/s llegarías a unos "
                                "110 MB/s." % (ad["mbps"], ad["mbps"] / 8 * 0.94)})
        else:
            out.append({"id": "link", "level": "ok", "text": "El PC va por cable%s." % (
                " a %s" % ("%d Gb/s" % (ad["mbps"] / 1000) if ad["mbps"] >= 1000 else "%d Mb/s" % ad["mbps"])
                if ad["mbps"] else "")})
    else:
        out.append({"id": "link", "level": "skip", "text": "No he podido saber si el PC va por cable o por Wi-Fi."})
    if idle is not None and loaded:
        lo = statistics.median(loaded)
        if lo > idle + 60 and lo > 3 * idle:
            out.append({"id": "load", "level": "warn",
                        "text": "Con la red cargada la consola tarda en contestar (%d ms, en reposo %d ms): el "
                                "Wi-Fi o el router se atascan. Probar otro canal, la banda de 5 GHz o un límite de "
                                "velocidad algo más bajo en Ajustes suele ayudar." % (lo, idle)})
        else:
            out.append({"id": "load", "level": "ok",
                        "text": "La latencia aguanta bien con la red cargada (%d ms; en reposo %d ms)." % (lo, idle)})
    elif idle is not None:
        out.append({"id": "load", "level": "skip", "text": "Latencia en reposo: %d ms." % idle})
    if up_mbs is not None and ad["wifi"] is False and (ad["mbps"] or 0) >= 900 and up_mbs < 40:
        out.append({"id": "console", "level": "warn",
                    "text": "Por cable a 1 Gb/s deberías pasar de 40 MB/s y van %.1f: el límite puede estar en la "
                            "PS5 (su Wi-Fi si no tiene cable, o el FTP de la consola) o en el disco del PC." % up_mbs})
    rank = {"bad": 0, "warn": 1, "ok": 2, "skip": 3}
    out.sort(key=lambda x: rank[x["level"]])
    return out
