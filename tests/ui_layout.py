"""Prueba de la interfaz con un navegador de verdad (Playwright). Abre cada menú, panel, pestaña y sección de Ajustes en
varios tamaños de ventana y MIDE: que la cabecera no cambie de alto, que los menús floten (no empujen la página) y
quepan en pantalla, que no haya scroll horizontal, que ningún botón se quede «cargando» y que no haya errores de
JavaScript. Uso: python tests/ui_layout.py <url con #t=token> [carpeta de capturas]"""
import asyncio, json, sys, os
from playwright.async_api import async_playwright

URL = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else "ui_shots"
SIZES = [(1920, 880), (1366, 768), (420, 860)]
POPS = [("#btnMore", "#headMenu", "Transfers"), ("#tabMore", "#tabMoreMenu", "Transfers"), ("#libMoreBtn", "#libMenu", "Library"),
        ("#btnPow", "#powPanel", "Transfers"), ("#btnBell", "#nPanel", "Transfers")]
fails, checks = [], 0


def check(cond, msg):
    global checks
    checks += 1
    if not cond:
        fails.append(msg)


async def run(p, w, h, lang):
    b = await p.chromium.launch()
    pg = await b.new_page(viewport={"width": w, "height": h})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    await pg.add_init_script("try{localStorage.setItem('ps5d_lang','%s');localStorage.setItem('ps5d_apk_sinpc_pc','{\"never\":1}')}catch(e){}" % lang)
    await pg.goto(URL)
    await pg.wait_for_timeout(2500)
    close = "document.querySelectorAll('dialog[open]').forEach(d=>d.close())"
    await pg.evaluate(close); await pg.wait_for_timeout(1500); await pg.evaluate(close)
    tag = "%dx%d-%s" % (w, h, lang)
    hh = await pg.evaluate("document.querySelector('header').getBoundingClientRect().height")
    for trig, pop, tab in POPS:
        await pg.evaluate("showTab('%s')" % tab); await pg.wait_for_timeout(500); await pg.evaluate(close)
        await pg.evaluate("window.scrollTo(0,0)")
        el = await pg.query_selector(trig)
        if not el or not await el.is_visible():
            continue
        await el.click(); await pg.wait_for_timeout(450)
        r = await pg.evaluate("""(s)=>{const e=document.querySelector(s); if(!e) return null; const b=e.getBoundingClientRect();
            const cs=getComputedStyle(e); return {vis:!e.hidden&&cs.display!=='none'&&b.width>0, x:b.left, y:b.top, w:b.width, h:b.height, r:b.right, bt:b.bottom,
            pos:cs.position, vw:innerWidth, vh:innerHeight, sw:document.documentElement.scrollWidth,
            hh:document.querySelector('header').getBoundingClientRect().height,
            busy:[...document.querySelectorAll('button.busy')].map(x=>x.id||x.className)}}""", pop)
        name = "%s %s" % (tag, pop)
        check(r and r["vis"], name + ": no se abre")
        if r and r["vis"]:
            check(r["pos"] in ("fixed", "absolute"), "%s: no flota (position:%s)" % (name, r["pos"]))
            check(abs(r["hh"] - hh) < 3, "%s: la cabecera cambia de alto (%d -> %d)" % (name, hh, r["hh"]))
            check(r["x"] >= -1 and r["r"] <= r["vw"] + 1, "%s: se sale por los lados (%d..%d de %d)" % (name, r["x"], r["r"], r["vw"]))
            check(r["w"] <= min(520, r["vw"]), "%s: demasiado ancho (%d px)" % (name, r["w"]))
            check(r["y"] >= -1 and r["bt"] <= r["vh"] + 1, "%s: se sale por arriba/abajo (%d..%d de %d)" % (name, r["y"], r["bt"], r["vh"]))
            check(r["sw"] <= r["vw"] + 1, "%s: aparece scroll horizontal" % name)
            check(not r["busy"], "%s: botones cargando: %s" % (name, r["busy"]))
        await pg.screenshot(path="%s/%s_%s.png" % (OUT, tag, pop.strip("#")))
        await pg.keyboard.press("Escape"); await pg.mouse.click(5, h - 5); await pg.wait_for_timeout(250)
    # «Más formas» de Transferencias
    await pg.evaluate("showTab('Transfers')"); await pg.wait_for_timeout(300)
    if await pg.is_visible("#xMoreBtn"):
        await pg.click("#xMoreBtn"); await pg.wait_for_timeout(300)
        busy = await pg.evaluate("[...document.querySelectorAll('button.busy')].map(x=>x.id)")
        check(not busy, "%s Más formas: botón cargando %s" % (tag, busy))
        check(await pg.is_visible("#xMore"), "%s Más formas: no se abre" % tag)
        await pg.click("#xMoreBtn")
    # todas las pestañas: sin scroll horizontal
    for t in ["Transfers", "Library", "Apps", "Files", "Space", "Backup", "Media", "System", "Activity"]:
        await pg.evaluate("showTab('%s')" % t); await pg.wait_for_timeout(900); await pg.evaluate(close)
        sw = await pg.evaluate("[document.documentElement.scrollWidth,innerWidth]")
        check(sw[0] <= sw[1] + 1, "%s pestaña %s: scroll horizontal (%d > %d)" % (tag, t, sw[0], sw[1]))
        await pg.screenshot(path="%s/%s_tab_%s.png" % (OUT, tag, t))
    # Ajustes, cada sección
    await pg.evaluate("showTab('Transfers')"); await pg.evaluate(close); await pg.evaluate("window.scrollTo(0,0)")
    await pg.click("#btnSettings"); await pg.wait_for_timeout(500)
    for sec in ["con", "env", "fmt", "upd", "tools", "adv"]:
        await pg.click("#setNav [data-sec=%s]" % sec); await pg.wait_for_timeout(420)
        r = await pg.evaluate("""()=>{const d=document.getElementById('dlg').getBoundingClientRect(); const bd=document.querySelector('.setbody');
            const sec=document.querySelector('.setsec:not([hidden])').getBoundingClientRect(); const nv=document.getElementById('setNav').lastElementChild.getBoundingClientRect();
            return {t:d.top,b:d.bottom,l:d.left,r:d.right,vw:innerWidth,vh:innerHeight,ox:bd.scrollWidth>bd.clientWidth+1,gap:d.bottom-Math.max(sec.bottom,nv.bottom)}}""")
        n = "%s Ajustes/%s" % (tag, sec)
        check(r["t"] >= 0 and r["b"] <= r["vh"] + 1 and r["l"] >= 0 and r["r"] <= r["vw"] + 1, n + ": se sale de la pantalla")
        check(not r["ox"], n + ": scroll horizontal dentro")
        if w > 720:
            check(r["gap"] < 120, "%s: hueco vacío grande al final (%d px)" % (n, r["gap"]))
        await pg.screenshot(path="%s/%s_set_%s.png" % (OUT, tag, sec))
    await pg.evaluate(close)
    check(not errs, "%s: errores de JavaScript: %s" % (tag, errs[:3]))
    await b.close()


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        for w, h in SIZES:
            await run(p, w, h, "es")
        await run(p, 1366, 768, "en")
    print("%d comprobaciones, %d fallos" % (checks, len(fails)))
    for f in fails:
        print(" FALLO:", f)
    sys.exit(1 if fails else 0)

asyncio.run(main())
