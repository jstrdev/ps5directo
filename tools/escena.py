#!/usr/bin/env python3
"""Genera escena.json: las novedades de la scene de PS5 que salen en el cartel de Descubrir.

Lo ejecuta solo una GitHub Action (.github/workflows/escena.yml) cada 3 horas y sube el resultado al repo.
No hace falta tocarlo a mano. Mira dos cosas:
  1. Versiones nuevas de todo lo que hay en Descubrir (emuladores, apps, payloads) y de los payloads base.
  2. Proyectos nuevos de PS5 en GitHub (temas ps5-homebrew, ps5-jailbreak, ps5-payload...) con alguna versión
     publicada y un mínimo de estrellas. Lo que encaje con la lista de cosas que no recomendamos se descarta.

Además decide qué es «gordo» (nota por estrellas que gana de un día para otro, autor conocido, si trae algo que se
instala, versión grande de algo que ya está en Descubrir): lo mejor sale solo en la portada de Descubrir una semana
(«hot» en escena.json) y, si es un proyecto nuevo que se puede instalar, deja una propuesta de tarjeta para
catalogo.json (la convierte en una pull request el propio flujo, y entra solo si la aceptas).

Uso: GITHUB_TOKEN=... python3 tools/escena.py [salida.json] [propuestas.json]
"""
import datetime as dt
import json
import os
import html
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import features as F  # noqa: E402

API = os.environ.get("PS5D_GITHUB_API") or "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN") or ""
DAYS = 45               # lo que se guarda (la app enseña en el cartel solo lo de los últimos días)
NEW_MIN_STARS = 5       # proyectos nuevos: mínimo de estrellas para salir
TOPICS = ["ps5-homebrew", "ps5-jailbreak", "ps5-payload", "ps5-emulator", "playstation5-homebrew"]
BASE = ["drakmor/ShadowMountPlus", "ps5-payload-dev/ftpsrv", "ps5-payload-dev/elfldr", "ItsBlurf/BFpilot",
        "seregonwar/zftpd", "LightningMods/etaHEN", "EchoStretch/kstuff", "Gezine/Y2JB"]
# lo que decidimos no recomendar (y cualquier cosa de piratería): no sale nunca
SKIP = re.compile(r"(?i)playden|art.?swap|psgfx|steamgrid|romm|nuvio|stremio|smarttube|trophy|savegam|save.?(manager|edit)"
                  r"|cheat|dump(er)?s?\b|piracy|pirat|warez|crack|keygen|store.?(pkg|game)|free.?games|pkg.?(links|store)"
                  r"|nopaystation|psbox|hen.?store")


def gh(path, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    h = {"User-Agent": "PS5Directo-escena", "Accept": "application/vnd.github+json"}
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (404, 451):
                return None
            if e.code in (403, 429) and i < 2:
                time.sleep(20 * (i + 1))
                continue
            raise
        except Exception:
            if i == 2:
                raise
            time.sleep(5)


def short(md, n=380):
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md or "")             # imágenes fuera
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)                # enlaces: solo el texto
    t = re.sub(r"<[^>]+>|[#*_`>|]+", " ", t)
    t = html.unescape(t).replace("\u00a0", " ")                    # &nbsp; y compañía
    t = re.sub(r"(?m)^\s*[-=]{3,}\s*$|\s[-=]{3,}\s", " ", t)          # separadores ---
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n].rsplit(" ", 1)[0] + "…" if len(t) > n else t


HOT_MIN = 6.0           # nota mínima para salir sola en portada
HOT_MAX = 2             # como mucho, a la vez
HOT_DAYS = 7            # días que se queda en portada
PROPOSE_MIN = 5.0       # nota mínima para proponer una tarjeta de Descubrir
PROPOSE_LINK_MIN = 7.0  # y para proponer una de solo guía (lo que se construye en el PC o no trae nada instalable)
TEXT_MIN_STARS = 15     # búsqueda por nombre o descripción: más exigente que por temas
NOREL_MIN_STARS = 30    # proyectos sin versión publicada (solo código): solo si ya son conocidos
SCENE_WORDS = re.compile(r"(?i)jailbr|homebrew|payload|etahen|kstuff|elfldr|emulat|\bport\b|recomp|native ps5|"
                         r"ps5.?(title|app)|ppsa\d|shadowmount|bfpilot|ftpsrv|byepervisor|umtx|y2jb")


def _ver(tag):
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", tag or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def bump(new_tag, old_tag):
    """'major', 'minor' o '' comparando dos versiones (v2.0 frente a v1.9 = major)."""
    a, b = _ver(new_tag), _ver(old_tag)
    if not a or not b or a <= b:
        return ""
    return "major" if a[0] > b[0] else "minor" if a[1] > b[1] else ""


def stars_delta(hist, repo, now_stars, today):
    """Estrellas ganadas desde hace ~1 día (la historia guarda una foto por día)."""
    h = [x for x in hist.get(repo.lower()) or [] if x[0] < today]
    return now_stars - h[-1][1] if h else 0


def score(x, known_owners, delta, big):
    """Nota de «lo gordo que es». Ver la explicación de arriba."""
    sc = 0.0
    own = x["repo"].split("/")[0].lower()
    if x["kind"] == "new":
        sc += 2 + min(x.get("stars", 0) / 25.0, 6)
        if x.get("installable"):
            sc += 2
    else:
        sc += 5 if big == "major" else 1.5 if big == "minor" else 0
        if x.get("cid"):
            sc += 0.5
    if own in known_owners:
        sc += 3 if x["kind"] == "new" else 0.5
    sc += min(max(delta, 0) / 5.0, 4)
    if x.get("pre"):
        sc -= 1
    return round(sc, 2)


def installable(rel):
    """Qué trae la versión: 'app' (.zip/.7z/.rar), 'payload' (.elf/.bin) o ''. Y el archivo."""
    best = ("", None)
    for a in rel.get("assets") or []:
        n = (a.get("name") or "").lower()
        if re.search(r"\.(zip|7z|rar)$", n) and not re.search(r"source|src|windows|win64|linux|macos|android|\.apk", n):
            if best[0] != "app":
                best = ("app", a)
        elif re.search(r"\.(elf|bin)$", n) and not re.search(r"eboot|param|bios|firmware", n) and not best[0]:
            best = ("payload", a)
    return best


def zip_tid(url):
    """Title ID (PPSA…/CUSA…) de un .zip sin bajarlo entero (lee solo el índice)."""
    import io
    import zipfile

    class RF(io.RawIOBase):
        def __init__(self, u):
            super().__init__()
            self.u, self.pos = u, 0
            with urllib.request.urlopen(urllib.request.Request(u, headers={"Range": "bytes=0-0", "User-Agent": "PS5Directo-escena"}),
                                        timeout=30) as r:
                self.size = int((r.headers.get("Content-Range") or "/0").rsplit("/", 1)[1])
                self.u = r.geturl()

        def readable(self):
            return True

        def seekable(self):
            return True

        def tell(self):
            return self.pos

        def seek(self, o, w=0):
            self.pos = o if w == 0 else self.pos + o if w == 1 else self.size + o
            return self.pos

        def read(self, n=-1):
            n = self.size - self.pos if n is None or n < 0 else min(n, self.size - self.pos)
            if n <= 0:
                return b""
            hd = {"Range": "bytes=%d-%d" % (self.pos, self.pos + n - 1), "User-Agent": "PS5Directo-escena"}
            with urllib.request.urlopen(urllib.request.Request(self.u, headers=hd), timeout=60) as r:
                d = r.read(n)
            self.pos += len(d)
            return d

        def readinto(self, b):
            d = self.read(len(b))
            b[:len(d)] = d
            return len(d)

    with zipfile.ZipFile(RF(url)) as z:
        for t in {n.split("/")[0] for n in z.namelist() if "/" in n}:
            if re.fullmatch(r"(?i)(PPSA|CUSA)\d{5}", t):
                return t.upper()
    return ""


def proposal(x, rel, kind, asset):
    """Tarjeta para catalogo.json de un proyecto nuevo (la revisa el dueño antes de entrar)."""
    repo = x["repo"]
    cid = re.sub(r"[^a-z0-9]+", "-", repo.split("/")[1].lower()).strip("-")[:30] or "nuevo"
    desc = x.get("desc") or x.get("notes") or ""
    card = {"id": cid, "kind": kind, "group": "tool", "name": x["name"], "repo": repo, "exp": True,
            "what": desc, "needs": "", "auto_tr": True, "en": {"what": desc},
            "icon": "https://github.com/%s.png" % repo.split("/")[0]}
    if kind == "app":
        tid = ""
        if asset and asset.get("name", "").lower().endswith(".zip"):
            try:
                tid = zip_tid(asset["browser_download_url"])
            except Exception as e:
                print("! zip", repo, e)
        if not tid:
            m = re.search(r"(?i)(PPSA|CUSA)\d{5}", asset.get("name") or "")
            tid = m.group(0).upper() if m else ""
        if not tid:
            return None                     # no se sabe su carpeta: mejor no proponer «Instalar»
        card["tid"] = tid
        card["sys"] = "Aplicación · " + tid
    else:
        card["sys"] = "Payload"
    stem = re.split(r"[-_ .]?v?\d", (asset.get("name") or "").rsplit(".", 1)[0], 1)[0]
    ext = (asset.get("name") or "x.zip").rsplit(".", 1)[-1].lower()
    card["asset"] = ([r"(?i)^" + re.escape(stem) + r".*\." + ext + "$"] if len(stem) >= 3 else []) + [r"(?i)\." + ext + "$"]
    return {"card": card, "score": x["score"], "stars": x.get("stars", 0), "url": x.get("url") or "https://github.com/" + repo,
            "tag": x.get("tag"),
            "file": asset.get("name"), "desc": desc}


def proposal_link(x):
    """Tarjeta de solo guía (enlace, qué es y pasos de su GitHub) para lo que no trae nada que se instale solo."""
    repo = x["repo"]
    cid = re.sub(r"[^a-z0-9]+", "-", repo.split("/")[1].lower()).strip("-")[:30] or "nuevo"
    desc = x.get("desc") or x.get("notes") or ""
    text = " ".join([repo, desc]).lower()
    group = "emu" if re.search(r"emulat|rpcs|pcsx|dolphin|ppsspp|mame|retro", text) else "tool"
    card = {"id": cid, "kind": "link", "group": group, "name": x["name"], "repo": repo, "exp": True, "what": desc,
            "needs": "No viene hecho para instalar: sigue la guía de su GitHub.", "auto_tr": True,
            "en": {"what": desc, "needs": "It doesn't come ready to install: follow its GitHub guide."},
            "guide": ["Ábrelo en GitHub y sigue su guía (el README)."],
            "icon": "https://github.com/%s.png" % repo.split("/")[0]}
    card["en"]["guide"] = ["Open it on GitHub and follow its guide (the README)."]
    return {"card": card, "score": x["score"], "stars": x.get("stars", 0), "url": x.get("url") or "https://github.com/" + repo,
            "tag": x.get("tag"), "file": "", "desc": desc}


def newest(repo):
    rels = gh("/repos/%s/releases" % repo, {"per_page": 5}) or []
    rels = [r for r in rels if isinstance(r, dict) and not r.get("draft") and r.get("published_at")]
    rels.sort(key=lambda r: r["published_at"], reverse=True)
    return rels[0] if rels else None


def main(out, props_out=""):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    try:                                                          # las tarjetas extra de catalogo.json, también
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "catalogo.json"),
                  encoding="utf-8") as fh:
            F.remote_apply(json.load(fh))
    except Exception as e:
        print("catalogo.json: %s" % e)
    known = {}                                                    # repo (minúsculas) -> (id de Descubrir, nombre)
    for c in F.CATALOG:
        for r in [c.get("repo")] + list(c.get("alts") or []):
            if r and "/" in r:
                known.setdefault(r.lower(), (c["id"], c.get("name") or r.split("/")[1]))
    for p in F.EXTRA_PAYLOADS:
        if p.get("repo"):
            known.setdefault(p["repo"].lower(), (p["id"], p.get("name") or p["repo"].split("/")[1]))
    for r in BASE:
        known.setdefault(r.lower(), ("", r.split("/")[1]))
    repos = {r.lower(): r for r in [c.get("repo") for c in F.CATALOG] + [p.get("repo") for p in F.EXTRA_PAYLOADS] + BASE if r}
    items = []
    for low, repo in sorted(repos.items()):
        try:
            rel = newest(repo)
        except Exception as e:
            print("!", repo, e)
            continue
        if not rel or rel["published_at"] < since:
            continue
        cid, name = known.get(low, ("", repo.split("/")[1]))
        stars = 0
        try:
            stars = int((gh("/repos/" + repo) or {}).get("stargazers_count") or 0)
        except Exception:
            pass
        prev = ""
        try:
            rels = [r for r in gh("/repos/%s/releases" % repo, {"per_page": 5}) or [] if isinstance(r, dict)
                    and not r.get("draft") and r.get("published_at") and r.get("tag_name") != rel.get("tag_name")]
            rels.sort(key=lambda r: r["published_at"], reverse=True)
            prev = rels[0].get("tag_name") or "" if rels else ""
        except Exception:
            pass
        items.append({"id": "%s@%s" % (repo, rel.get("tag_name")), "kind": "update", "repo": repo, "cid": cid,
                      "name": name, "tag": rel.get("tag_name") or "", "pre": bool(rel.get("prerelease")),
                      "date": rel["published_at"], "notes": short(rel.get("body")), "url": rel.get("html_url") or "",
                      "stars": stars, "_big": bump(rel.get("tag_name"), prev)})
    # proyectos nuevos de PS5 que aún no están en Descubrir
    seen = set(repos)
    created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)).strftime("%Y-%m-%d")
    # por temas y, como muchos proyectos no los ponen, también por nombre o descripción (más estrellas y con palabras de
    # la scene, para no colar programas de PC que solo mencionan la PS5)
    queries = ["topic:%s created:>%s stars:>=%d" % (t, created, NEW_MIN_STARS) for t in TOPICS]
    queries += ["ps5 in:name,description created:>%s stars:>=%d" % (created, TEXT_MIN_STARS),
                "playstation 5 in:name,description created:>%s stars:>=%d" % (created, TEXT_MIN_STARS)]
    for q in queries:
        try:
            res = gh("/search/repositories", {"q": q, "sort": "stars", "per_page": 30}) or {}
        except Exception as e:
            print("!", q, e)
            continue
        for r in res.get("items") or []:
            full = r.get("full_name") or ""
            if full.lower() in seen or r.get("fork") or r.get("archived"):
                continue
            seen.add(full.lower())
            text = " ".join([full, r.get("description") or ""] + list(r.get("topics") or []))
            if SKIP.search(text):
                continue
            if q.startswith(("ps5 in:", "playstation 5 in:")) and not SCENE_WORDS.search(text):
                continue
            try:
                rel = newest(full)
            except Exception:
                rel = None
            stars = int(r.get("stargazers_count") or 0)
            if not rel:
                if stars < NOREL_MIN_STARS:
                    continue                                      # sin versión publicada y poco conocido: aún no
                rel = {"tag_name": "", "published_at": r.get("created_at") or r.get("pushed_at") or "", "body": "",
                       "html_url": r.get("html_url") or "", "assets": []}
                if not rel["published_at"]:
                    continue
            kind, asset = installable(rel)
            items.append({"id": "%s@%s" % (full, rel.get("tag_name")), "kind": "new", "repo": full, "cid": "",
                          "name": r.get("name") or full, "tag": rel.get("tag_name") or "", "pre": bool(rel.get("prerelease")),
                          "date": rel["published_at"], "desc": short(r.get("description"), 280), "notes": short(rel.get("body")),
                          "stars": int(r.get("stargazers_count") or 0), "url": r.get("html_url") or "",
                          "installable": kind, "_rel": rel, "_asset": asset})
        time.sleep(0 if "127.0.0.1" in API else 2)                                             # la búsqueda tiene un límite más bajo
    items.sort(key=lambda x: x["date"], reverse=True)
    # --- lo gordo: nota de cada novedad, con la historia de estrellas de la vez anterior
    try:
        with open(out, encoding="utf-8") as fh:
            old = json.load(fh)
    except Exception:
        old = {}
    hist, now = dict(old.get("hist") or {}), dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    owners = {(c.get("repo") or "").split("/")[0].lower() for c in F.CATALOG if c.get("repo")}
    owners.discard("")
    recent = (now - dt.timedelta(days=HOT_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    for x in items:
        d = stars_delta(hist, x["repo"], x.get("stars", 0), today)
        x["score"] = score(x, owners, d, x.get("_big", ""))
        h = [e for e in hist.get(x["repo"].lower()) or [] if e[0] != today] + [[today, x.get("stars", 0)]]
        hist[x["repo"].lower()] = h[-8:]
    hist = {k: v for k, v in hist.items() if v and v[-1][0] >= (now - dt.timedelta(days=10)).strftime("%Y-%m-%d")}
    # portada: lo que ya estaba sigue su semana; se completa con lo de más nota (de la última semana)
    hot = [h for h in old.get("hot") or [] if str(h.get("from") or "") >= (now - dt.timedelta(days=HOT_DAYS)).strftime("%Y-%m-%d")]
    have = {h["repo"].lower() for h in hot}
    for x in sorted(items, key=lambda x: -x["score"]):
        if len(hot) >= HOT_MAX or x["score"] < HOT_MIN:
            break
        if x["date"] < recent or x["repo"].lower() in have:
            continue
        hot.append({"id": x["id"], "repo": x["repo"], "cid": x.get("cid") or "", "kind": x["kind"], "name": x["name"],
                    "tag": x.get("tag") or "", "text": x.get("desc") or x.get("notes") or "", "url": x["url"],
                    "stars": x.get("stars", 0), "score": x["score"], "big": x.get("_big", ""), "from": today})
        have.add(x["repo"].lower())
    # propuestas de tarjeta: proyectos nuevos que se pueden instalar y tienen nota
    props = []
    for x in items:
        if x["kind"] != "new":
            continue
        pr = None
        if x.get("installable") and x["score"] >= PROPOSE_MIN:
            pr = proposal(x, x["_rel"], x["installable"], x["_asset"] or {})
        if not pr and x["score"] >= PROPOSE_LINK_MIN:
            pr = proposal_link(x)
        if pr:
            props.append(pr)
    for x in items:
        for k in [k for k in x if k.startswith("_")]:
            del x[k]
        x.pop("installable", None)
    data = {"_como": "Lo genera solo tools/escena.py (GitHub Action cada 3 horas). No lo edites a mano: "
                     "para destacar algo usa destacados.json.",
            "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "items": items[:80], "hot": hot, "hist": hist}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print("escena.json: %d novedades, %d en portada" % (len(items), len(hot)))
    if props_out:
        with open(props_out, "w", encoding="utf-8") as fh:
            json.dump(props, fh, ensure_ascii=False, indent=1)
        print("propuestas: %d" % len(props))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "escena.json"), sys.argv[2] if len(sys.argv) > 2 else "")
