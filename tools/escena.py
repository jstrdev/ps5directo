#!/usr/bin/env python3
"""Genera escena.json: las novedades de la scene de PS5 que salen en el cartel de Descubrir.

Lo ejecuta solo una GitHub Action (.github/workflows/escena.yml) cada 3 horas y sube el resultado al repo.
No hace falta tocarlo a mano. Mira dos cosas:
  1. Versiones nuevas de todo lo que hay en Descubrir (emuladores, apps, payloads) y de los payloads base.
  2. Proyectos nuevos de PS5 en GitHub (temas ps5-homebrew, ps5-jailbreak, ps5-payload...) con alguna versión
     publicada y un mínimo de estrellas. Lo que encaje con la lista de cosas que no recomendamos se descarta.

Uso: GITHUB_TOKEN=... python3 tools/escena.py [salida.json]
"""
import datetime as dt
import json
import os
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
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n].rsplit(" ", 1)[0] + "…" if len(t) > n else t


def newest(repo):
    rels = gh("/repos/%s/releases" % repo, {"per_page": 5}) or []
    rels = [r for r in rels if isinstance(r, dict) and not r.get("draft") and r.get("published_at")]
    rels.sort(key=lambda r: r["published_at"], reverse=True)
    return rels[0] if rels else None


def main(out):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
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
        items.append({"id": "%s@%s" % (repo, rel.get("tag_name")), "kind": "update", "repo": repo, "cid": cid,
                      "name": name, "tag": rel.get("tag_name") or "", "pre": bool(rel.get("prerelease")),
                      "date": rel["published_at"], "notes": short(rel.get("body")), "url": rel.get("html_url") or ""})
    # proyectos nuevos de PS5 que aún no están en Descubrir
    seen = set(repos)
    created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)).strftime("%Y-%m-%d")
    for t in TOPICS:
        try:
            res = gh("/search/repositories", {"q": "topic:%s created:>%s stars:>=%d" % (t, created, NEW_MIN_STARS),
                                              "sort": "stars", "per_page": 30}) or {}
        except Exception as e:
            print("!", t, e)
            continue
        for r in res.get("items") or []:
            full = r.get("full_name") or ""
            if full.lower() in seen or r.get("fork") or r.get("archived"):
                continue
            seen.add(full.lower())
            text = " ".join([full, r.get("description") or ""] + list(r.get("topics") or []))
            if SKIP.search(text):
                continue
            try:
                rel = newest(full)
            except Exception:
                rel = None
            if not rel:
                continue                                          # sin versión publicada: aún no es para el usuario
            items.append({"id": "%s@%s" % (full, rel.get("tag_name")), "kind": "new", "repo": full, "cid": "",
                          "name": r.get("name") or full, "tag": rel.get("tag_name") or "", "pre": bool(rel.get("prerelease")),
                          "date": rel["published_at"], "desc": short(r.get("description"), 280), "notes": short(rel.get("body")),
                          "stars": int(r.get("stargazers_count") or 0), "url": r.get("html_url") or ""})
        time.sleep(0 if "127.0.0.1" in API else 2)                                             # la búsqueda tiene un límite más bajo
    items.sort(key=lambda x: x["date"], reverse=True)
    data = {"_como": "Lo genera solo tools/escena.py (GitHub Action cada 3 horas). No lo edites a mano: "
                     "para destacar algo usa destacados.json.",
            "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "items": items[:80]}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print("escena.json: %d novedades" % len(items))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "escena.json"))
