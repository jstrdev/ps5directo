const EN = !(navigator.language || "es").toLowerCase().startsWith("es");
const T = EN ? {
  capture: "Capture .rar, .7z, .zip, .pkg… downloads", prog: "Queue progress on the icon", checking: "Checking…", reset: "Reset",
  help: "Pass the captcha and click Download as usual: if it's a .rar, .7z, .zip, .pkg… and PS5 Directo is open, the link goes to its link box and the browser downloads nothing. If PS5 Directo is closed, the download goes ahead normally.",
  old: (a, b) => `This extension is version ${a} and PS5 Directo comes with ${b}. Remove it on the extensions page and load the folder again (Settings → "Open the extension folder" → Load unpacked). From then on it updates by itself.`,
  sent: "Captured", last: "last", connected: "● Connected", to: "to PS5 Directo", closed: "● PS5 Directo is not open", normal: "(downloads will work normally)",
  send: "Right-click → \"Send to PS5 Directo\"", box: "To the link box (you check it and click \"Add to queue\")", queue: "Straight to the queue",
  where: "Where does it go?", ps5: "📺 To the PS5", keep: "💻 Only to my PC", empty: "Nothing in the queue.", pause: "Pause", resume: "Resume", cancel: "Cancel",
  st: { pendiente: "waiting", comprobando: "checking", enviando: "sending", convirtiendo: "converting", extrayendo: "extracting", colocando: "placing",
        "en cola para descomprimir": "waiting to extract", hecho: "done", error: "error", cancelado: "cancelled", pausado: "paused" }
} : {
  capture: null, prog: null, checking: "Comprobando…", reset: null, help: null,
  sent: "Capturados", last: "último", connected: "● Conectado", to: "a PS5 Directo", closed: "● PS5 Directo no está abierto", normal: "(las descargas seguirán normales)",
  empty: "No hay nada en la cola.", pause: "Pausar", resume: "Reanudar", cancel: "Cancelar", st: {},
  old: (a, b) => `Esta extensión es la versión ${a} y PS5 Directo trae la ${b}. Quítala en la página de extensiones y vuelve a cargar la carpeta (Ajustes → «Abrir la carpeta de la extensión» → Cargar descomprimida). Desde entonces se actualiza sola.`
};
const $ = id => document.getElementById(id);
if (EN) {
  document.documentElement.lang = "en";
  $("lblEn").textContent = T.capture; $("lblProg").textContent = T.prog; $("st").textContent = T.checking;
  $("reset").textContent = T.reset; $("help").textContent = T.help; $("lgSend").textContent = T.send;
  $("mBox").textContent = T.box; $("mQueue").textContent = T.queue; $("lWhere").textContent = T.where;
  $("where").options[0].textContent = T.ps5; $("where").options[1].textContent = T.keep;
}
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const human = b => { b = +b || 0; const u = ["B", "KB", "MB", "GB"]; let i = 0; while (b >= 1024 && i < 3) { b /= 1024; i++ } return b.toFixed(i ? 1 : 0) + " " + u[i]; };

function paint(st) {
  const s = $("st");
  if (!st) { s.innerHTML = `<span class="bad">${T.closed}</span> ${T.normal}`; $("dots").hidden = true; $("q").hidden = true; return; }
  s.innerHTML = `<span class="ok">${T.connected}</span> ${T.to} v${esc(st.version)}`;
  $("dots").hidden = false; $("dFtp").classList.toggle("on", st.ftp === true); $("dBf").classList.toggle("on", st.bf === true);
  const jobs = st.jobs.slice().reverse().slice(0, 6);
  $("q").hidden = false;
  $("q").innerHTML = jobs.length ? jobs.map(j => {
    const act = !["hecho", "error", "cancelado", "pausado"].includes(j.status);
    const btns = (act ? `<button class="x" data-a="pause" data-id="${j.id}" title="${T.pause}">⏸</button><button class="x" data-a="cancel" data-id="${j.id}" title="${T.cancel}">✕</button>`
                      : j.status === "pausado" ? `<button class="x" data-a="resume" data-id="${j.id}" title="${T.resume}">▶</button>` : "");
    return `<div class="job"><div class="jt"><b title="${esc(j.title)}">${esc(j.title)}</b><span class="muted">${esc(T.st[j.status] || j.status)}${act && j.pct ? " · " + j.pct + "%" : ""}</span>${btns}</div>
      ${act ? `<div class="bar"><i style="width:${j.pct || 0}%"></i></div><div class="ph">${esc(j.phase)}${j.speed > 0 ? " · " + human(j.speed) + "/s" : ""}</div>` : ""}</div>`;
  }).join("") : `<div class="muted">${T.empty}</div>`;
  $("q").querySelectorAll("button[data-a]").forEach(b => b.onclick = () =>
    chrome.runtime.sendMessage({ type: "job", id: b.dataset.id, action: b.dataset.a }, () => refresh()));
}
async function refresh() { chrome.runtime.sendMessage({ type: "poll" }, paint); }

(async () => {
  const cfg = await chrome.storage.local.get({ enabled: true, captured: 0, last: "", mode: "box", where: "ps5", progress: true });
  $("en").checked = cfg.enabled; $("en").onchange = () => chrome.storage.local.set({ enabled: $("en").checked });
  $("prog").checked = cfg.progress; $("prog").onchange = () => { chrome.storage.local.set({ progress: $("prog").checked }); refresh(); };
  document.querySelectorAll("input[name=mode]").forEach(r => { r.checked = r.value === cfg.mode; r.onchange = () => { chrome.storage.local.set({ mode: r.value }); $("whereRow").hidden = r.value !== "queue"; }; });
  $("whereRow").hidden = cfg.mode !== "queue";
  $("where").value = cfg.where; $("where").onchange = () => chrome.storage.local.set({ where: $("where").value });
  $("cnt").textContent = cfg.captured ? `${T.sent}: ${cfg.captured}${cfg.last ? " (" + T.last + ": " + cfg.last + ")" : ""}` : "";
  $("reset").onclick = async () => { await chrome.storage.local.set({ captured: 0, last: "" }); $("cnt").textContent = ""; };
  const { outdated = "" } = await chrome.storage.local.get({ outdated: "" });
  if (outdated) { const w = document.createElement("div"); w.className = "warn"; w.textContent = T.old(chrome.runtime.getManifest().version, outdated); document.body.insertBefore(w, $("st")); }
  refresh(); setInterval(refresh, 1500);
})();
