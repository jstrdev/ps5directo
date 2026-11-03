// PS5 Directo: con Alt + clic en un enlace, esa descarga la hace el navegador normal (no se captura).
(function () {
  const tell = e => {
    if (e.altKey && e.target && e.target.closest && e.target.closest("a")) {
      try { chrome.runtime.sendMessage({ type: "bypass" }); } catch (err) { /* extension recargada */ }
    }
  };
  document.addEventListener("mousedown", tell, true);
  document.addEventListener("click", tell, true);
})();
