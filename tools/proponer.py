#!/usr/bin/env python3
"""Convierte las propuestas de tools/escena.py en pull requests que añaden una tarjeta a catalogo.json.

Lo ejecuta el flujo «Novedades de la scene» después de escena.py. Cada propuesta es una pull request aparte: si el
dueño del repo la acepta (Merge), la tarjeta sale en Descubrir para todos en unas horas; si la cierra, no se vuelve
a proponer. Nunca se añade nada a Descubrir sin que alguien lo acepte.

Uso (en GitHub Actions, con GH_TOKEN): python3 tools/proponer.py propuestas.json
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAT = os.path.join(HERE, "catalogo.json")
MAX_PER_RUN = 2


def run(*a, check=True):
    r = subprocess.run(a, cwd=HERE, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError("%s: %s" % (" ".join(a), (r.stderr or r.stdout).strip()[:400]))
    return r.stdout.strip()


def known(cat):
    """ids y repos que ya están (en catalogo.json o dentro de la app)."""
    sys.path.insert(0, HERE)
    import features as F
    ids = {c["id"] for c in F.CATALOG} | {x.get("id") for x in cat.get("items") or []}
    repos = {(c.get("repo") or "").lower() for c in F.CATALOG} | {(x.get("repo") or "").lower() for x in cat.get("items") or []}
    for c in F.CATALOG:
        repos |= {a.lower() for a in c.get("alts") or []}
    return ids, repos


def body(p):
    c = p["card"]
    lines = [
        "El robot de novedades ha visto un proyecto nuevo de PS5 que se puede instalar y propone añadirlo a **Descubrir**.",
        "",
        "- **Proyecto:** [%s](%s)" % (c["repo"], p["url"]),
        "- **Versión:** %s · **archivo:** `%s`" % (p.get("tag") or "?", p.get("file") or "?"),
        "- **Estrellas:** %s · **nota:** %s" % (p.get("stars"), p.get("score")),
        "- **Qué instala:** %s" % ("una aplicación en /data/homebrew/%s" % c.get("tid") if c["kind"] == "app" else "un payload"),
        "",
        "> %s" % (p.get("desc") or "(sin descripción)"),
        "",
        "**Antes de aceptar**, échale un vistazo a su GitHub: que sea lo que dice, que no sea de lo que no recomendamos y "
        "que su autor sea de fiar. Puedes editar la tarjeta en esta misma pull request (nombre, grupo, textos, guía).",
        "",
        "- **Merge** → sale en Descubrir para todos en unas horas.",
        "- **Close** → no se vuelve a proponer.",
    ]
    return "\n".join(lines)


def main(path):
    try:
        with open(path, encoding="utf-8") as fh:
            props = json.load(fh)
    except Exception as e:
        print("Sin propuestas (%s)" % e)
        return
    with open(CAT, encoding="utf-8") as fh:
        base = json.load(fh)
    ids, repos = known(base)
    run("git", "config", "user.name", "escena-bot")
    run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    start = run("git", "rev-parse", "--abbrev-ref", "HEAD")
    made = 0
    for p in sorted(props, key=lambda p: -float(p.get("score") or 0)):
        if made >= MAX_PER_RUN:
            break
        c = p["card"]
        if c["repo"].lower() in repos:
            continue
        cid = c["id"]
        while cid in ids:
            cid = cid + "-2" if not re.search(r"-\d+$", cid) else re.sub(r"\d+$", lambda m: str(int(m.group(0)) + 1), cid)
        c["id"] = cid
        branch = "propuesta/" + re.sub(r"[^a-z0-9]+", "-", c["repo"].lower())
        # ya propuesta antes (abierta, aceptada o cerrada): no se repite
        if run("gh", "pr", "list", "--state", "all", "--head", branch, "--json", "number", "--jq", "length", check=False) not in ("", "0"):
            continue
        if run("git", "ls-remote", "--heads", "origin", branch, check=False):
            continue
        run("git", "checkout", "-B", branch, "origin/" + start)
        with open(CAT, encoding="utf-8") as fh:
            cat = json.load(fh)
        cat.setdefault("items", []).append(c)
        with open(CAT, "w", encoding="utf-8") as fh:
            json.dump(cat, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        run("git", "add", "catalogo.json")
        run("git", "commit", "-m", "Propuesta para Descubrir: %s" % c["name"])
        run("git", "push", "-u", "origin", branch)
        run("gh", "pr", "create", "--base", start, "--head", branch,
            "--title", "Propuesta para Descubrir: %s (%s)" % (c["name"], c["repo"]), "--body", body(p))
        print("Propuesta creada:", c["repo"])
        made += 1
        ids.add(cid)
        repos.add(c["repo"].lower())
        run("git", "checkout", start)
    print("%d propuestas nuevas" % made)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "propuestas.json")
