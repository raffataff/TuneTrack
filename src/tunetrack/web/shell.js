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
  // The window is a piece of rack gear: a faceplate, a tab strip, and modules (rack.css .su) that each carry a
  // legend with a lamp (lit when off the factory setting), a switch, fader or picker, and a readout of what it does.
  const SECTIONS = [["appearance", "Appearance"], ["colours", "Seat colours"], ["layout", "Layout"], ["fonts", "Type"]];
  const BG = [["aurora", "Aurora", "Soft colour and drifting stars"], ["grid", "Grid", "A lit digital floor"],
    ["carbon", "Carbon", "Woven graphite, no glow"], ["none", "None", "Clean darkness"]];
  const SEATS = { producer: "Producer", spotting: "Spotting", composer: "Composer", arranger: "Arranger", rhythm: "Rhythm",
    sound: "Sound", tracking: "Tracking", mix: "Mix", review: "Review" };
  const mod = (k, legend, inner, cls = "", lit = false) => `<section class="su ${cls}${lit ? " lit" : ""}" data-m="${k}"><h6><i class="led"></i>${legend}</h6>${inner}</section>`;
  const seg = (k, cur, opts) => `<div class="seg">${opts.map(([v, t, tip]) => `<button data-${k}="${esc(v)}" class="${String(v) === String(cur) ? "on" : ""}"${tip ? ` title="${esc(tip)}"` : ""}>${t}</button>`).join("")}</div>`;
  const read = (t) => `<p class="suRead">${t}</p>`;
  const fader = (k, min, max, step, val, shown) => `<div class="stFader"><input type="range" data-n="${k}" min="${min}" max="${max}" step="${step}" value="${val}" aria-label="${k}"><output class="vfd">${shown}</output></div>`;
  const renderFonts = (list) => {
    const opts = (cur) => {
      const all = cur && !list.includes(cur) ? [cur, ...list] : list;
      return all.map((n) => `<option value="${esc(n)}"${n === cur ? " selected" : ""}>${esc(n)}</option>`).join("");
    };
    const sp = typeof F.legend_sp === "number" ? F.legend_sp : DEF.legend_sp, st = typeof F.legend_st === "number" ? F.legend_st : DEF.legend_st;
    sPop.querySelector(".setupPane").innerHTML = `<div class="suDeck">
      ${ROLES.map((r) => mod(r.k, r.name, `<select title="${esc(r.what)}"><option value="">Built in · ${esc(r.def)}</option>${opts(F[r.k] || "")}</select>
        <div class="stSample ${r.k === "screen" ? "vfd" : ""}" style="font-family: var(--${r.k})">${esc(r.sample)}</div>${read(esc(r.what[0].toUpperCase() + r.what.slice(1)) + ".")}`, "", !!F[r.k])).join("")}
      ${mod("labels", "Label lettering", `<div class="stLab">
          <label><span>Spacing</span>${fader("legend_sp", 0, 0.25, 0.01, sp, sp.toFixed(2))}</label>
          <label><span>Width</span>${fader("legend_st", 70, 120, 1, st, st + "%")}</label>
          <label><span>Capitals</span>${seg("caps", F.caps !== false ? 1 : 0, [[1, "ON"], [0, "OFF"]])}</label></div>
        ${read(`Width only changes fonts cut in narrow and wide versions, like Bahnschrift. ${list.length} fonts found on this computer.${fonts ? "" : " Only the fonts Windows ships are listed: restart TuneTrack to see every font you have installed."}`)}`,
        "full", sp !== DEF.legend_sp || st !== DEF.legend_st || F.caps === false)}
    </div>`;
    const pane = sPop.querySelector(".setupPane");
    pane.querySelectorAll(".su[data-m] select").forEach((s) => {
      s.onchange = () => { const k = s.closest(".su").dataset.m; apply({ ...F, [k]: s.value || undefined }, true); s.closest(".su").classList.toggle("lit", !!s.value); };
    });
    pane.querySelectorAll("input[data-n]").forEach((i) => {
      i.oninput = () => { apply({ ...F, [i.dataset.n]: +i.value }, true); i.nextElementSibling.textContent = i.dataset.n === "legend_sp" ? (+i.value).toFixed(2) : i.value + "%"; litLabels(); };
      i.ondblclick = () => { i.value = DEF[i.dataset.n]; i.oninput(); };
    });
    pane.querySelectorAll("[data-caps]").forEach((b) => b.onclick = () => {
      apply({ ...F, caps: b.dataset.caps === "1" }, true); pane.querySelectorAll("[data-caps]").forEach((x) => x.classList.toggle("on", x === b)); litLabels();
    });
    const litLabels = () => pane.querySelector('[data-m="labels"]').classList.toggle("lit", (F.legend_sp ?? DEF.legend_sp) !== DEF.legend_sp || (F.legend_st ?? DEF.legend_st) !== DEF.legend_st || F.caps === false);
  };
  let section = "appearance";
  const render = (list) => {
    const A = TTAppearance.get(), L = window.TTStudioLayout?.get() || {}, D = TTAppearance.colours;
    sPop.innerHTML = `<header class="setupHead"><i class="screw"></i><div><strong>Studio setup</strong><span>The room around the rack · changes apply at once and are kept</span></div><button class="key" data-close aria-label="Close setup" title="Close (Esc)">✕</button><i class="screw"></i></header>
      <nav class="setupTabs tabsB" aria-label="Setup sections">${SECTIONS.map(([k, t]) => `<button data-section="${k}" class="${section === k ? "active" : ""}" aria-pressed="${section === k}">${t}</button>`).join("")}</nav>
      <div class="setupPane"></div>
      <footer class="setupFoot"><span class="stHint"></span><span class="spacer"></span><button class="key" data-default>RESET ${SECTIONS.find(([k]) => k === section)[1].toUpperCase()}</button></footer>`;
    const pane = sPop.querySelector(".setupPane"), hint = sPop.querySelector(".stHint");
    const setA = (v) => TTAppearance.set({ ...TTAppearance.get(), ...v });
    if (section === "appearance") {
      hint.textContent = "Double-click a fader to put it back.";
      pane.innerHTML = `<div class="suDeck three">
        ${mod("effect", "Background", `<div class="stBgs" role="radiogroup">${BG.map(([k, t, d]) => `<button role="radio" data-effect="${k}" class="${A.effect === k ? "on" : ""}" aria-checked="${A.effect === k}"><i class="stBg bg-${k}"></i><b>${t}</b><span>${d}</span></button>`).join("")}</div>`, "full", A.effect !== "aurora")}
        ${mod("intensity", "Glow", `${fader("intensity", 0, 100, 1, A.intensity, A.intensity + "%")}${read("How strongly the background shows behind the rack.")}`, "", A.intensity !== 55)}
        ${mod("motion", "Motion", `${seg("motion", A.motion ? 1 : 0, [[1, "ANIMATE"], [0, "STILL"]])}${read("Drifting light, breathing lamps, the bench sliding open. Your system's reduced-motion setting always wins.")}`, "", !A.motion)}
        ${mod("wiring", "Task links", `${seg("wiring", A.wiring, [["digital", "TRACES"], ["cables", "CABLES"]])}${read(A.wiring === "cables" ? "Curved patch cables from each task to its seat." : "Straight lit traces from each task to its seat.")}`, "", A.wiring !== "digital")}
      </div>`;
      pane.querySelectorAll("[data-effect]").forEach((b) => b.onclick = () => { setA({ effect: b.dataset.effect }); render(list); });
      const f = pane.querySelector('[data-n="intensity"]');
      f.oninput = () => { setA({ intensity: +f.value }); f.nextElementSibling.textContent = f.value + "%"; pane.querySelector('[data-m="intensity"]').classList.toggle("lit", +f.value !== 55); };
      f.ondblclick = () => { f.value = 55; f.oninput(); };
      pane.querySelectorAll("[data-motion]").forEach((b) => b.onclick = () => { setA({ motion: b.dataset.motion === "1" }); render(list); });
      pane.querySelectorAll("[data-wiring]").forEach((b) => b.onclick = () => { setA({ wiring: b.dataset.wiring }); render(list); });
    } else if (section === "colours") {
      hint.textContent = "Click a lamp to choose its colour. Status rings keep their own colours.";
      pane.innerHTML = `<div class="suDeck">
        ${mod("colours", "A colour for every seat", `<div class="stSeats">${Object.entries(A.colours).map(([id, c]) => `<div class="stSeat${c !== D[id] ? " moved" : ""}" style="--c:${c}">
            <label class="stLamp" title="Choose ${esc(SEATS[id] || id)}'s colour"><input type="color" data-colour="${id}" value="${c}" aria-label="${esc(SEATS[id] || id)} colour"></label>
            <b>${esc(SEATS[id] || id)}</b><output class="vfd">${c.toUpperCase()}</output>
            <button class="mini" data-undo="${id}" title="Back to ${D[id].toUpperCase()}">↺</button></div>`).join("")}</div>`, "full", Object.entries(A.colours).some(([id, c]) => c !== D[id]))}
        ${mod("logs", "Message logs", `${seg("logs", A.logs ? 1 : 0, [[1, "SEAT COLOURS"], [0, "PLAIN"]])}${read(A.logs ? "Each line in the monitor and the seat logs takes its seat's colour." : "Logs stay one colour.")}`, "full", !A.logs)}
      </div>`;
      pane.querySelectorAll("[data-colour]").forEach((el) => el.oninput = () => {
        const v = TTAppearance.get(); v.colours[el.dataset.colour] = el.value; TTAppearance.set(v);
        const row = el.closest(".stSeat"); row.style.setProperty("--c", el.value); row.querySelector("output").textContent = el.value.toUpperCase();
        row.classList.toggle("moved", el.value !== D[el.dataset.colour]);
        pane.querySelector('[data-m="colours"]').classList.toggle("lit", !!pane.querySelector(".stSeat.moved"));
      });
      pane.querySelectorAll("[data-undo]").forEach((b) => b.onclick = () => { const v = TTAppearance.get(); v.colours[b.dataset.undo] = D[b.dataset.undo]; TTAppearance.set(v); render(list); });
      pane.querySelectorAll("[data-logs]").forEach((b) => b.onclick = () => { setA({ logs: b.dataset.logs === "1" }); render(list); });
    } else if (section === "layout") {
      const ids = (window.TTStudio?.ST?.board?.seats || []).map((s) => s.seat), folded = (L.fold || []).length;
      hint.textContent = "Drag a unit by its screws to move it. Drag the dividers to size the bays.";
      pane.innerHTML = `<div class="suDeck">
        ${mod("side", "Monitor side", `${seg("side", L.side || "right", [["left", "LEFT"], ["right", "RIGHT"]])}${read(`Needs You and the monitor sit ${L.side === "left" ? "left" : "right"} of the rack.`)}`, "", L.side === "left")}
        ${mod("bay", "Upper bay", `${seg("bay", (L.bays || ["needs"])[0], [["needs", "NEEDS YOU"], ["monitor", "MONITOR"]])}${read(`${(L.bays || ["needs"])[0] === "monitor" ? "The monitor and talkback" : "Needs You"} on top, the other below.`)}`, "", (L.bays || ["needs"])[0] === "monitor")}
        ${mod("empty", "Empty optional seats", `${seg("empty", L.hideEmpty ? 1 : 0, [[0, "SHOW"], [1, "HIDE"]])}${read(L.hideEmpty ? "Optional seats with nobody in them and no work are hidden." : "Every seat has its unit in the rack.")}`, "", !!L.hideEmpty)}
        ${mod("fold", "Folded units", `<div class="seg"><button data-fold="all">FOLD ALL</button><button data-fold="none">UNFOLD ALL</button></div>${read(folded ? `${folded} unit${folded === 1 ? "" : "s"} folded to a thin strip.` : "No units folded. Double-click a unit's name to fold it.")}`, "", folded > 0)}
      </div>`;
      pane.querySelectorAll("[data-side]").forEach((b) => b.onclick = () => { TTStudioLayout.set({ side: b.dataset.side }); render(list); });
      pane.querySelectorAll("[data-bay]").forEach((b) => b.onclick = () => { TTStudioLayout.set({ bays: b.dataset.bay === "needs" ? ["needs", "monitor"] : ["monitor", "needs"], bayH: null }); render(list); });
      pane.querySelectorAll("[data-empty]").forEach((b) => b.onclick = () => { TTStudioLayout.set({ hideEmpty: b.dataset.empty === "1" }); render(list); });
      pane.querySelectorAll("[data-fold]").forEach((b) => b.onclick = () => { TTStudioLayout.set({ fold: b.dataset.fold === "all" ? ids : [] }); render(list); });
    } else { hint.textContent = "Fonts installed on this computer. The built-in ones are always there to go back to."; renderFonts(list); }
    sPop.querySelectorAll("[data-section]").forEach((b) => b.onclick = () => { section = b.dataset.section; render(list); sPop.querySelector(`[data-section="${section}"]`).focus(); });
    sPop.querySelector("[data-close]").onclick = shut;
    sPop.querySelector("[data-default]").onclick = () => {
      if (section === "fonts") apply({}, true);
      else if (section === "layout") TTStudioLayout.reset();
      else if (section === "colours") TTAppearance.set({ ...TTAppearance.get(), colours: TTAppearance.colours, logs: true });
      else { const v = TTAppearance.get(); TTAppearance.reset(); TTAppearance.set({ ...TTAppearance.get(), colours: v.colours, logs: v.logs }); }
      render(list);
    };
    if (!sPop.classList.contains("hidden")) sPlace();
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
