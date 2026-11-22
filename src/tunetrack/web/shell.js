// The app shell around the rooms: the recording options under the caret next to ●, the SETUP key (the fonts, from
// fonts installed on this computer; kept in the library's ui_prefs so the app window and every browser agree) and
// the engine readout on the status bar.
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

  // ---------------------------------------------------------------- SETUP: the fonts
  // Four roles, each a CSS variable (style.css): labels, text, the green-glass displays and numbers. A choice is a
  // font installed on this computer; "" keeps the built-in one. Kept in ui_prefs as "fonts" (and in localStorage,
  // which index.html applies before the first paint).
  const ROLES = [
    { k: "legend", name: "Labels", what: "keys, tabs, headings, rack unit names", def: "Bahnschrift", sample: "Studio · Composer · Needs you" },
    { k: "ui", name: "Text", what: "lists, notes, menus, talkback", def: "Segoe UI", sample: "The bridge wants a darker colour, bar 17." },
    { k: "screen", name: "Displays", what: "rack screens, playhead, crew readout", def: "same as Numbers", sample: "COMPOSER · WRITING A2  0:42" },
    { k: "mono", name: "Numbers", what: "IDs, timings, values, timeline and piano-roll labels", def: "Cascadia Mono", sample: "t-014  17:1.00  -14.2 LUFS" },
  ];
  const DEF = { legend_sp: 0.12, legend_st: 88, caps: true };
  const OLD = { astra: { legend: "Cyber Astra", legend_sp: 0.05, legend_st: 100 }, nexaro: { legend: "Nexaro Display", legend_sp: 0.07, legend_st: 100 } };
  const root = document.documentElement.style;
  let F = {};
  const quote = (n) => `"${String(n).replace(/["\\]/g, "")}"`;
  const apply = (f, save) => {
    F = f && typeof f === "object" ? f : {};
    const css = {}, num = (k, lo, hi) => (typeof F[k] === "number" && F[k] >= lo && F[k] <= hi ? F[k] : null);
    for (const r of ROLES) {
      if (typeof F[r.k] === "string" && F[r.k]) css[`--${r.k}`] = `${quote(F[r.k])}, ${r.k === "mono" || r.k === "screen" ? "Consolas, monospace" : "'Segoe UI', system-ui, sans-serif"}`;
    }
    if (num("legend_sp", 0, 0.3) != null) css["--legend-sp"] = F.legend_sp + "em";
    if (num("legend_st", 50, 150) != null) css["--legend-st"] = F.legend_st + "%";
    if (F.caps === false) css["--legend-case"] = "none";
    for (const p of ["--legend", "--ui", "--screen", "--mono", "--legend-sp", "--legend-st", "--legend-case"]) {
      if (css[p]) root.setProperty(p, css[p]); else root.removeProperty(p);
    }
    try { localStorage.setItem("tt.fonts", JSON.stringify(F)); localStorage.setItem("tt.fontcss", JSON.stringify(css)); localStorage.removeItem("tt.font"); } catch { /* this page only */ }
    dispatchEvent(new Event("tt-fonts"));
    dispatchEvent(new Event("resize"));  // canvases and the console measure their labels again
    if (save) {
      clearTimeout(apply.t);
      apply.t = setTimeout(() => api("ui_prefs", { set: { fonts: F, display_font: null } }, null).catch(() => { /* kept for this page */ }), 300);
    }
  };
  // A font the browser can draw changes the width of a test line against both generic fallbacks.
  const cx = document.createElement("canvas").getContext("2d"), TXT = "mmmmmwwwwwlli10OQ@#&";
  const wid = (f) => { cx.font = f; return cx.measureText(TXT).width; };
  const base = { m: wid("40px monospace"), s: wid("40px serif") };
  const drawable = (n) => wid(`40px ${quote(n)}, monospace`) !== base.m || wid(`40px ${quote(n)}, serif`) !== base.s;
  // Fonts Windows ships, tried even when the server can't list the installed ones (an older server, not Windows)
  const COMMON = ["Arial", "Arial Black", "Bahnschrift", "Calibri", "Cambria", "Candara", "Cascadia Code", "Cascadia Mono",
    "Comic Sans MS", "Consolas", "Constantia", "Corbel", "Courier New", "Ebrima", "Franklin Gothic Medium", "Gabriola",
    "Gadugi", "Georgia", "Impact", "Ink Free", "Lucida Console", "Lucida Sans Unicode", "Palatino Linotype",
    "Segoe Print", "Segoe Script", "Segoe UI", "Segoe UI Variable", "Sitka Text", "Sylfaen", "Tahoma",
    "Times New Roman", "Trebuchet MS", "Verdana"];
  let fonts = null;
  const fontList = async () => {
    if (fonts) return fonts;
    let names = [], listed = false;
    try { names = (await api("font_list", {}, null)).fonts || []; listed = names.length > 0; } catch { /* an older server */ }
    const list = [...new Set([...names, ...COMMON])].filter(drawable).sort((a, b) => a.localeCompare(b));
    if (listed) fonts = list;  // ask again next time when the server could not list them
    return list;
  };

  const sPop = document.createElement("div");
  sPop.id = "setPop"; sPop.className = "pop setPop hidden"; sPop.setAttribute("role", "dialog"); sPop.setAttribute("aria-label", "Setup");
  document.body.append(sPop);
  const setBtn = $s("#setBtn");
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const renderFonts = (list) => {
    const opts = (cur) => {
      const all = cur && !list.includes(cur) ? [cur, ...list] : list;
      return all.map((n) => `<option value="${esc(n)}"${n === cur ? " selected" : ""}>${esc(n)}</option>`).join("");
    };
    sPop.querySelector(".setupPane").innerHTML = `<h3>Typography</h3><p>Choose installed fonts for each part of your studio.</p>
      ${ROLES.map((r) => `<div class="stRole" data-k="${r.k}">
        <label class="field">${r.name.toUpperCase()}<select title="${esc(r.what)}"><option value="">Default (${esc(r.def)})</option>${opts(F[r.k] || "")}</select></label>
        <div class="stWhat">${esc(r.what)}</div>
        <div class="stSample ${r.k === "screen" ? "vfd" : ""}" style="font-family: var(--${r.k})">${esc(r.sample)}</div>
      </div>`).join("")}
      <div class="stRole stLab">
        <label class="field">LABEL SPACING<input type="range" data-n="legend_sp" min="0" max="0.25" step="0.01"></label>
        <label class="field">LABEL WIDTH<input type="range" data-n="legend_st" min="70" max="120" step="1"></label>
        <div class="stRow"><button class="key toggle" data-caps>CAPITALS</button><span class="spacer"></span><button class="key" data-reset title="Back to the built-in fonts and spacing">RESET</button></div>
        <div class="stWhat">Width only changes fonts that come in narrow and wide cuts, like Bahnschrift. ${list.length} fonts found on this computer.${fonts ? "" : " Only the fonts Windows ships are listed: restart TuneTrack to see every font you have installed."}</div>
      </div>`;
    sPop.querySelectorAll(".stRole[data-k] select").forEach((s) => {
      s.onchange = () => apply({ ...F, [s.closest(".stRole").dataset.k]: s.value || undefined }, true);
    });
    sPop.querySelectorAll("input[data-n]").forEach((i) => {
      i.value = typeof F[i.dataset.n] === "number" ? F[i.dataset.n] : DEF[i.dataset.n];
      i.oninput = () => apply({ ...F, [i.dataset.n]: +i.value }, true);
    });
    const caps = sPop.querySelector("[data-caps]");
    caps.classList.toggle("on", F.caps !== false);
    caps.onclick = () => { apply({ ...F, caps: F.caps === false }, true); caps.classList.toggle("on", F.caps !== false); };
    sPop.querySelector("[data-reset]").onclick = () => { apply({}, true); render(list); };
  };
  let section = "appearance";
  const render = (list) => {
    const A = TTAppearance.get(), L = window.TTStudioLayout?.get() || {};
    sPop.innerHTML = `<div class="setupHead"><div><strong>Studio setup</strong><p>Make this space your own.</p></div><button data-close aria-label="Close setup">×</button></div>
      <nav class="setupTabs" aria-label="Setup sections">${[["appearance", "Appearance"], ["colours", "Seat colours"], ["layout", "Layout"], ["fonts", "Typography"]].map(([k, t]) => `<button data-section="${k}" class="${section === k ? "on" : ""}" aria-pressed="${section === k}">${t}</button>`).join("")}</nav>
      <div class="setupPane"></div><div class="setupFoot"><span>Changes apply immediately · saved automatically</span><span class="spacer"></span><button data-default>Reset ${section === "colours" ? "colours" : section === "fonts" ? "fonts" : section}</button></div>`;
    const pane = sPop.querySelector(".setupPane");
    const select = (key, title, values, cur) => `<label class="setupControl">${title}<select data-option="${key}">${values.map(([v, t]) => `<option value="${v}"${v === cur ? " selected" : ""}>${t}</option>`).join("")}</select></label>`;
    if (section === "appearance") {
      pane.innerHTML = `<h3>Atmosphere & light</h3><p>A quiet glow behind the work. Tune the texture and movement to suit your session.</p><div class="setupGrid">
        ${select("effect", "Background", [["aurora", "Aurora · soft colour & stars"], ["grid", "Grid · digital studio"], ["carbon", "Carbon · woven graphite"], ["none", "None · clean darkness"]], A.effect)}
        ${select("wiring", "Task connections", [["digital", "Digital traces"], ["cables", "Curved cables"]], A.wiring)}
        <label class="setupControl">Effect intensity <output id="effectLevel">${A.intensity}%</output><input aria-label="Effect intensity" data-intensity type="range" min="0" max="100" value="${A.intensity}"></label></div>
        <label class="setupCheck"><input type="checkbox" data-motion ${A.motion ? "checked" : ""}>Animate atmosphere and activity</label><p>Your system’s reduced-motion preference is always respected.</p>`;
      pane.querySelectorAll("[data-option]").forEach((el) => el.onchange = () => TTAppearance.set({ ...TTAppearance.get(), [el.dataset.option]: el.value }));
      pane.querySelector("[data-intensity]").oninput = (e) => { TTAppearance.set({ ...TTAppearance.get(), intensity: +e.target.value }); pane.querySelector("output").textContent = e.target.value + "%"; };
      pane.querySelector("[data-motion]").onchange = (e) => TTAppearance.set({ ...TTAppearance.get(), motion: e.target.checked });
    } else if (section === "colours") {
      pane.innerHTML = `<h3>A colour for every seat</h3><p>Follow each seat across the rack, displays and message log. Status rings keep their own state colours.</p><div class="setupColours">${Object.entries(A.colours).map(([id, colour]) => `<label class="setupColour"><input type="color" data-colour="${id}" value="${colour}" aria-label="${id} colour"><span>${id}</span></label>`).join("")}</div><label class="setupCheck"><input type="checkbox" data-logs ${A.logs ? "checked" : ""}>Use seat colours in message logs</label>`;
      pane.querySelectorAll("[data-colour]").forEach((el) => el.oninput = () => { const v = TTAppearance.get(); v.colours[el.dataset.colour] = el.value; TTAppearance.set(v); });
      pane.querySelector("[data-logs]").onchange = (e) => TTAppearance.set({ ...TTAppearance.get(), logs: e.target.checked });
    } else if (section === "layout") {
      pane.innerHTML = `<h3>Arrange your workspace</h3><p>Drag a rack by its screws to reorder it. Drag the dividers to resize the monitor and bays.</p><div class="setupGrid">
        ${select("side", "Monitor position", [["right", "Right of rack"], ["left", "Left of rack"]], L.side)}
        ${select("bay", "Upper bay", [["needs", "Needs you"], ["monitor", "Monitor / talkback"]], L.bays?.[0])}</div>
        <label class="setupCheck"><input type="checkbox" data-empty ${L.hideEmpty ? "checked" : ""}>Hide empty optional seats</label><button data-unfold>Unfold all seats</button>`;
      pane.querySelectorAll("[data-option]").forEach((el) => el.onchange = () => TTStudioLayout.set(el.dataset.option === "bay" ? { bays: el.value === "needs" ? ["needs", "monitor"] : ["monitor", "needs"], bayH: null } : { side: el.value }));
      pane.querySelector("[data-empty]").onchange = (e) => TTStudioLayout.set({ hideEmpty: e.target.checked });
      pane.querySelector("[data-unfold]").onclick = () => TTStudioLayout.set({ fold: [] });
    } else renderFonts(list);
    sPop.querySelectorAll("[data-section]").forEach((b) => b.onclick = () => { section = b.dataset.section; render(list); sPop.querySelector(`[data-section="${section}"]`).focus(); });
    sPop.querySelector("[data-close]").onclick = shut;
    sPop.querySelector("[data-default]").onclick = () => {
      if (section === "fonts") apply({}, true);
      else if (section === "layout") TTStudioLayout.reset();
      else if (section === "colours") TTAppearance.set({ ...TTAppearance.get(), colours: TTAppearance.colours });
      else { const v = TTAppearance.get(); TTAppearance.reset(); TTAppearance.set({ ...TTAppearance.get(), colours: v.colours, logs: v.logs }); }
      render(list);
    };
  };
  const sPlace = () => {
    const r = setBtn.getBoundingClientRect(), h = sPop.offsetHeight;
    sPop.style.left = Math.max(8, Math.min(r.right + 10, innerWidth - sPop.offsetWidth - 8)) + "px"; sPop.style.top = Math.max(8, Math.min(68, innerHeight - h - 8)) + "px";
  };
  const shut = () => { sPop.classList.add("hidden"); setBtn.classList.remove("on"); setBtn.setAttribute("aria-expanded", "false"); setBtn.focus(); };
  setBtn.onclick = async (e) => {
    e.stopPropagation();
    if (!sPop.classList.contains("hidden")) { shut(); return; }
    render(fonts || COMMON.filter(drawable)); sPop.classList.remove("hidden"); setBtn.classList.add("on"); setBtn.setAttribute("aria-expanded", "true"); sPlace(); sPop.querySelector("[data-close]").focus();
    fontList().then((list) => { if (!sPop.classList.contains("hidden") && section === "fonts") render(list); });
  };
  document.addEventListener("mousedown", (e) => { if (!sPop.classList.contains("hidden") && !sPop.contains(e.target) && !setBtn.contains(e.target)) shut(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !sPop.classList.contains("hidden")) shut(); });
  addEventListener("resize", () => { if (!sPop.classList.contains("hidden")) sPlace(); });

  try { apply(JSON.parse(localStorage.getItem("tt.fonts") || "null") || {}); } catch { /* defaults */ }
  api("ui_prefs", {}, null).then((p) => {
    if (!p) return;
    if (p.fonts && typeof p.fonts === "object") { if (JSON.stringify(p.fonts) !== JSON.stringify(F)) apply(p.fonts); }
    else if (OLD[p.display_font]) apply({ ...OLD[p.display_font] }, true);  // the old one-font Setup choice
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
