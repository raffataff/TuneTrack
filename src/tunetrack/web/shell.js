// The app shell around the rooms: the recording options under the caret next to ●, the SETUP key (the display
// font, from fonts installed on this computer; kept in the library's ui_prefs so the app window and every browser
// agree) and the engine readout on the status bar.
"use strict";
(() => {
  const $s = (q) => document.querySelector(q);
  // ---------------------------------------------------------------- recording options
  const pop = $s("#recPop"), opt = $s("#recOpt");
  const place = () => { const r = opt.getBoundingClientRect(); pop.style.left = Math.max(8, Math.min(r.left - 110, innerWidth - 272)) + "px"; pop.style.top = r.bottom + 6 + "px"; };
  const close = () => { pop.classList.add("hidden"); opt.classList.remove("on"); };
  opt.onclick = (e) => { e.stopPropagation(); if (pop.classList.contains("hidden")) { place(); pop.classList.remove("hidden"); opt.classList.add("on"); } else close(); };
  document.addEventListener("mousedown", (e) => { if (!pop.classList.contains("hidden") && !pop.contains(e.target) && e.target !== opt && !opt.contains(e.target)) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !pop.classList.contains("hidden")) close(); });
  addEventListener("resize", () => { if (!pop.classList.contains("hidden")) place(); });

  // ---------------------------------------------------------------- display font
  const FONTS = [
    { id: "", name: "Bahnschrift", note: "built in" },
    { id: "astra", name: "Cyber Astra", face: "TT Astra" },
    { id: "nexaro", name: "Nexaro Display", face: "TT Nexaro" },
  ];
  const apply = (id) => {
    if (id) document.documentElement.dataset.font = id; else delete document.documentElement.dataset.font;
    try { localStorage.setItem("tt.font", id || ""); } catch { /* this page only */ }
    dispatchEvent(new Event("resize"));  // canvases and the console measure their labels again
  };
  const installed = async (f) => {
    if (!f.face) return true;
    try { await document.fonts.load(`12px "${f.face}"`); return document.fonts.check(`12px "${f.face}"`); } catch { return false; }
  };
  $s("#setBtn").onclick = async (e) => {
    const cur = document.documentElement.dataset.font || "", r = e.currentTarget.getBoundingClientRect();
    const have = await Promise.all(FONTS.map(installed));
    menu({ clientX: r.right + 6, clientY: Math.max(8, r.top - 120) }, [["Display font"], ...FONTS.map((f, i) => [
      `${f.id === cur ? "• " : ""}${f.name}${f.note ? ` (${f.note})` : have[i] ? "" : " (not installed)"}`,
      () => {
        if (!have[i]) { toast(`${f.name} is not installed on this computer. Install the font for your user, then reload.`); return; }
        apply(f.id); api("ui_prefs", { set: { display_font: f.id } }, null).catch(() => { /* kept for this page */ });
      }])]);
  };
  api("ui_prefs", {}, null).then((p) => {
    const id = p && typeof p.display_font === "string" ? p.display_font : null;
    if (id != null && id !== (document.documentElement.dataset.font || "") && FONTS.some((f) => f.id === id)) apply(id);
  }).catch(() => { /* the page's own choice stands */ });

  // ---------------------------------------------------------------- the status bar's engine readout
  const right = $s("#statRight");
  const engine = () => {
    const st = S.eng.st || {}, d = st.device;
    right.textContent = !st.available ? "PREVIEW · RENDERED PLAYBACK"
      : !S.eng.on ? "PREVIEW · LIVE ENGINE READY"
      : st.on && d ? `LIVE · ${d.type || ""} ${d.name || ""} · ${d.sr} Hz · ${d.block} · CPU ${Math.round((st.cpu || 0) * 100)}%${st.dropouts ? ` · ${st.dropouts} DROPOUTS` : ""}`
      : "LIVE · STARTING";
  };
  if (typeof liveES !== "undefined") liveES.addEventListener("message", engine);
  setInterval(engine, 2000); engine();

  // a link straight to a room (and the Desk's bottom tab): /?room=mix, /?room=desk&tab=mixer
  const q = new URLSearchParams(location.search);
  if (["studio", "desk", "mix", "cables"].includes(q.get("room"))) window.Studio?.room(q.get("room"));
  if (q.get("tab")) setTimeout(() => document.querySelector(`#tabs button[data-tab="${CSS.escape(q.get("tab"))}"]`)?.click(), 300);
})();
