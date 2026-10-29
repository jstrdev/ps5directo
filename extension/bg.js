const EN = !(navigator.language || "es").toLowerCase().startsWith("es");
const L = (es, en) => (EN ? en : es);
// PS5 Directo - extension del navegador.
//  - Al descargar un archivo comprimido o paquete (.rar, .7z, .zip, .pkg...), manda el enlace FINAL (despues de
//    redirecciones) a la caja de enlaces de PS5 Directo y cancela la descarga. Si PS5 Directo no esta abierto,
//    no toca nada: la descarga sigue normal.
//  - Clic derecho en un enlace o en un texto seleccionado: "Enviar a PS5 Directo".
//  - El icono enseña el progreso de la cola y avisa cuando algo termina o falla.
// Todo va solo a PS5 Directo en este PC (127.0.0.1), a ningun otro sitio.

const WANTED_FILE = /\.(part\d+\.rar|rar|r\d{2,3}|7z(\.\d{3})?|zip|z\d{2}|\d{3}|pkg|exfat|ffpkg|ffpfsc?|ufs)$/i;
const PORTS = [8777, 8778, 8779];

async function settings() {
  return chrome.storage.local.get({ enabled: true, captured: 0, mode: "box", where: "ps5", progress: true });
}

async function findApp() {
  const { port: last } = await chrome.storage.local.get({ port: 0 });
  for (const port of (last ? [last, ...PORTS.filter(p => p !== last)] : PORTS)) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/api/ext/ping`, { cache: "no-store" });
      if (r.ok) {
        const j = await r.json();
        if (j.app === "PS5 Directo") {
          if (port !== last) chrome.storage.local.set({ port });
          selfUpdate(j.ext_version);
          return { port, version: j.version, ext_version: j.ext_version };
        }
      }
    } catch (e) { /* no esta en ese puerto */ }
  }
  return null;
}

// --- actualizarse sola: PS5 Directo trae una version mas nueva de esta extension en su carpeta.
// Una extension "descomprimida" se puede recargar desde el disco (chrome.runtime.reload). Si tras recargar sigue
// siendo vieja, es que el navegador la tiene cargada desde otra carpeta: lo dice la ventanita.
const MINE = chrome.runtime.getManifest().version;
const vt = v => String(v || "").split(".").map(n => parseInt(n) || 0);
function older(a, b) { const x = vt(a), y = vt(b); for (let i = 0; i < 3; i++) { if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) < (y[i] || 0); } return false; }
async function selfUpdate(want) {
  if (!want || !older(MINE, want)) { chrome.storage.local.set({ outdated: "" }); return; }
  const { reloadedFor = "" } = await chrome.storage.local.get({ reloadedFor: "" });
  if (reloadedFor !== want) {
    await chrome.storage.local.set({ reloadedFor: want });
    chrome.runtime.reload();
    return;
  }
  chrome.storage.local.set({ outdated: want });
}

async function post(app, path, body) {
  const r = await fetch(`http://127.0.0.1:${app.port}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {})
  });
  return r.json();
}

// Como los gestores de descargas: pasamos las cookies de ese alojamiento (sesion, cuenta premium), el navegador
// y la pagina de origen, para que el servidor nos trate igual.
async function cookiesFor(urls) {
  const cookies = {};
  for (const u of urls) {
    try {
      const host = new URL(u).host.toLowerCase();
      if (host in cookies) continue;
      const list = await chrome.cookies.getAll({ url: u });
      cookies[host] = list.map(c => `${c.name}=${c.value}`).join("; ");
    } catch (e) { /* sin permiso o enlace raro: sin cookies */ }
  }
  return cookies;
}

function note(title, message) {
  chrome.notifications.create({ type: "basic", iconUrl: "icon128.png", title, message: message || "" });
}

function fileName(item) {
  const f = (item.filename || "").split(/[\\/]/).pop();
  if (f) return f;
  try { return decodeURIComponent(new URL(item.finalUrl || item.url).pathname.split("/").pop()); }
  catch (e) { return ""; }
}

// --- 1) descargas capturadas: siempre a la caja de enlaces (para juntar todas las partes de algo)
async function handle(item) {
  const cfg = await settings();
  if (!cfg.enabled) return;
  const url = item.finalUrl || item.url;
  if (!/^https?:/i.test(url)) return;
  const name = fileName(item);
  const path = url.split("?")[0];
  if (!WANTED_FILE.test(name) && !WANTED_FILE.test(path)) return;     // no es un archivo que nos interese
  const app = await findApp();
  if (!app) {                                                          // PS5 Directo cerrado: no tocar
    chrome.action.setBadgeText({ text: "off" });
    return;
  }
  try {
    const r = await fetch(`http://127.0.0.1:${app.port}/api/ext/capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, original: item.url, name, cookies: await cookiesFor([url, item.url]),
                             ua: navigator.userAgent, referrer: item.referrer || "" })
    });
    if (!r.ok) return;
  } catch (e) { return; }
  try { await chrome.downloads.cancel(item.id); } catch (e) {}
  try { await chrome.downloads.erase({ id: item.id }); } catch (e) {}
  const n = (cfg.captured || 0) + 1;
  await chrome.storage.local.set({ captured: n, last: name });
  note(L("Enviado a PS5 Directo", "Sent to PS5 Directo"),
       (name || L("Enlace", "Link")) + L(" — no se descarga en el PC. Está en la caja de enlaces.",
                                         " — not downloaded to the PC. It's in the link box."));
}

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  suggest();                // no cambiamos el nombre
  handle(item);
});

// --- 2) clic derecho: enviar un enlace o los enlaces de lo seleccionado
function makeMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "ps5d-link", contexts: ["link"], title: L("Enviar enlace a PS5 Directo", "Send link to PS5 Directo") });
    chrome.contextMenus.create({ id: "ps5d-sel", contexts: ["selection"], title: L("Enviar los enlaces seleccionados a PS5 Directo", "Send selected links to PS5 Directo") });
  });
}
chrome.runtime.onInstalled.addListener(() => { makeMenus(); setupAlarm(); });
chrome.runtime.onStartup.addListener(() => { makeMenus(); setupAlarm(); });

// En la pagina: enlaces que hay dentro de lo seleccionado (los href) y los que se ven escritos en el texto
function linksInSelection() {
  const out = new Set();
  const sel = window.getSelection();
  for (let i = 0; i < sel.rangeCount; i++) {
    const r = sel.getRangeAt(i);
    const box = r.commonAncestorContainer.nodeType === 1 ? r.commonAncestorContainer : r.commonAncestorContainer.parentElement;
    const anchors = box ? [box.closest("a"), ...box.querySelectorAll("a[href]")] : [];
    for (const a of anchors) if (a && a.href && sel.containsNode(a, true)) out.add(a.href);
  }
  const txt = sel.toString();
  for (const m of txt.matchAll(/\b(?:https?|ftp):\/\/[^\s<>"'\]\[(){}|]+|magnet:\?[^\s<>"']+/gi)) out.add(m[0].replace(/[.,;:!?]+$/, ""));
  return [...out].filter(u => /^(https?|ftp):|^magnet:/i.test(u));
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  let links = [];
  if (info.menuItemId === "ps5d-link" && info.linkUrl) links = [info.linkUrl];
  if (info.menuItemId === "ps5d-sel") {
    try {
      const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id, frameIds: [info.frameId || 0] }, func: linksInSelection });
      links = (res && res.result) || [];
    } catch (e) {
      links = (info.selectionText || "").match(/\b(?:https?|ftp):\/\/\S+/gi) || [];
    }
  }
  if (!links.length) { note("PS5 Directo", L("No hay enlaces en lo que has seleccionado.", "There are no links in your selection.")); return; }
  const app = await findApp();
  if (!app) { note("PS5 Directo", L("PS5 Directo no está abierto. Ábrelo y vuelve a probar.", "PS5 Directo isn't open. Open it and try again.")); return; }
  const cfg = await settings();
  try {
    const r = await post(app, "/api/ext/send", { links, mode: cfg.mode, where: cfg.where,
      cookies: await cookiesFor(links.filter(u => /^https?:/i.test(u))), ua: navigator.userAgent, referrer: (tab && tab.url) || "" });
    if (!r.ok) throw new Error(r.error || "error");
    const n = links.length;
    note(L("Enviado a PS5 Directo", "Sent to PS5 Directo"),
      cfg.mode === "queue"
        ? L(`${n} enlace(s) añadidos a la cola` + (cfg.where === "keep" ? " (solo a tu PC)." : "."), `${n} link(s) added to the queue` + (cfg.where === "keep" ? " (PC only)." : "."))
        : L(`${n} enlace(s) en la caja de enlaces. Revisa y pulsa «Añadir a la cola».`, `${n} link(s) in the link box. Check them and click "Add to queue".`));
    pollStatus();
  } catch (e) { note("PS5 Directo", L("No se pudo enviar: ", "Couldn't send: ") + e.message); }
});

// --- 3) progreso de la cola en el icono y avisos al terminar
function setupAlarm() { chrome.alarms.create("ps5d-poll", { periodInMinutes: 0.5 }); }
chrome.alarms.onAlarm.addListener(a => { if (a.name === "ps5d-poll") pollStatus(); });

async function pollStatus() {
  const cfg = await settings();
  const app = await findApp();
  if (!app) { chrome.action.setBadgeText({ text: "" }); return null; }
  let st;
  try { st = await post(app, "/api/ext/status", { ext_version: MINE }); } catch (e) { return null; }
  if (!st || !st.ok) return null;
  const { seen = {} } = await chrome.storage.session.get({ seen: {} }).catch(() => ({ seen: {} }));
  const first = !Object.keys(seen).length;
  for (const j of st.jobs) {
    const prev = seen[j.id];
    if (!first && prev && prev !== j.status && (j.status === "hecho" || j.status === "error"))
      note(j.status === "hecho" ? L("Listo: ", "Done: ") + j.title : L("Problema: ", "Problem: ") + j.title,
           j.status === "hecho" ? (j.where === "keep" ? L("Ya está en tu PC.", "It's on your PC now.") : L("Ya está en la PS5.", "It's on the PS5 now."))
                                : L("Ábrelo en PS5 Directo para ver qué ha pasado.", "Open PS5 Directo to see what happened."));
    seen[j.id] = j.status;
  }
  chrome.storage.session.set({ seen }).catch(() => {});
  const active = st.jobs.filter(j => !["hecho", "error", "cancelado", "pausado"].includes(j.status));
  const err = st.jobs.some(j => j.status === "error");
  if (!cfg.progress) chrome.action.setBadgeText({ text: "" });
  else if (active.length) {
    const pct = Math.round(active.reduce((a, j) => a + (j.pct || 0), 0) / active.length);
    chrome.action.setBadgeBackgroundColor({ color: "#2563eb" });
    chrome.action.setBadgeText({ text: pct + "%" });
  } else if (err) {
    chrome.action.setBadgeBackgroundColor({ color: "#dc2626" });
    chrome.action.setBadgeText({ text: "!" });
  } else chrome.action.setBadgeText({ text: "" });
  return st;
}

chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg && msg.type === "poll") { pollStatus().then(reply); return true; }
  if (msg && msg.type === "job") {
    findApp().then(app => app ? post(app, "/api/ext/job", { id: msg.id, action: msg.action }) : null).then(reply);
    return true;
  }
});
