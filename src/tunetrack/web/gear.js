// The gear browser (stage 6): every instrument and effect on this computer in one drawer, opened from the header's
// GEAR key over either room. Search and filter; NEW marks what arrived since you last looked; AUDITION plays it
// (on the live engine when LIVE is on: a loop in the project's key and tempo that your keyboards play too;
// otherwise a short render); USE puts it to work (an instrument on a track or a new one, an effect as an insert or
// on a new bus), in whatever the Desk shows. FOLDERS lists what TuneTrack watches, failed scans and RETRY.
// gear.py is the server's side; app.js gives S, api, act, menu, toast, liveMode and liveES.
"use strict";
(() => {
  const SRC = { patch: "SOUNDS", vst3: "PLUG-INS", sfz: "SFZ", synth: "BUILT-IN", builtin: "BUILT-IN" };
  const SRC_TIP = { patch: "Saved plugin sounds (patches)", vst3: "VST3 plug-ins", sfz: "SFZ sample instruments",
    synth: "TuneTrack's own synths and effects", builtin: "TuneTrack's own synths and effects" };
  // the list's sections, in order: [kind, sources, heading, grouped by maker/library]
  const SECTIONS = [
    ["instrument", ["patch"], "Saved sounds", true], ["instrument", ["vst3"], "Plug-in instruments", false],
    ["instrument", ["sfz"], "SFZ instruments", true], ["instrument", ["synth"], "Built-in synths", false],
    ["effect", ["patch"], "Saved effect settings", true], ["effect", ["vst3"], "Plug-in effects", false],
    ["effect", ["builtin"], "Built-in effects", false],
  ];
  const FOLD_AT = 6;  // a maker's group longer than this starts folded (unless you are searching)
  const G = {
    open: false, items: null, watch: null, loading: false, q: "", kind: "", src: "", sel: null, tab: "list",
    phrase: "auto", fold: new Map(), playing: null, busy: null, audio: null, mark: null, timer: 0,
  };
  const gearBtn = $("#gearBtn"), countEl = $("#gearCount"), lampEl = gearBtn.querySelector(".lamp");

  const el = document.createElement("aside");
  el.id = "gear"; el.className = "hidden"; el.setAttribute("aria-label", "Gear");
  el.innerHTML = `
    <div class="gFace">
      <span class="screw"></span>
      <div class="gIdent"><b>GEAR</b><span class="legend" id="gSum"></span></div>
      <nav class="gTabs"><button data-gt="list" class="on">LIST</button><button data-gt="folders">FOLDERS</button></nav>
      <button class="mini" id="gClose" title="Close (Esc). A live audition stops.">×</button>
      <span class="screw"></span>
    </div>
    <div class="gPane" data-pane="list">
      <div class="gTools">
        <input id="gQ" type="search" placeholder="search name, maker, tag" spellcheck="false" autocomplete="off" aria-label="Search gear">
        <div class="gChips" role="group" aria-label="Filter">
          <button data-k="" class="on">ALL</button><button data-k="instrument">INSTRUMENTS</button><button data-k="effect">EFFECTS</button>
          <i></i>
          <button data-s="" class="on">ANY</button><button data-s="patch" title="${SRC_TIP.patch}">SOUNDS</button><button data-s="vst3" title="${SRC_TIP.vst3}">PLUG-INS</button><button data-s="sfz" title="${SRC_TIP.sfz}">SFZ</button><button data-s="builtin" title="${SRC_TIP.builtin}">BUILT-IN</button>
        </div>
      </div>
      <div id="gRows" class="gRows" tabindex="0" aria-label="Gear list"></div>
      <div id="gDetail" class="gDetail"></div>
    </div>
    <div class="gPane hidden" data-pane="folders"><div id="gFolders" class="gFolders"></div></div>
    <div id="gWatch" class="gWatch vfd small"></div>`;
  document.body.appendChild(el);
  const rowsEl = el.querySelector("#gRows"), detailEl = el.querySelector("#gDetail"), qEl = el.querySelector("#gQ");

  const call = (fn, args = {}) => api(fn, args, null);
  const byId = (id) => (G.items || []).find((i) => i.id === id) || null;
  const short = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const ago = (t) => {
    if (!t) return "";
    const s = Math.max(0, Date.now() / 1000 - t);
    return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
  };
  const words = () => G.q.toLowerCase().split(/\s+/).filter(Boolean);
  const matches = (it, ws) => {
    if (G.kind && it.kind !== G.kind) return false;
    if (G.src && !(G.src === "builtin" ? it.source === "builtin" || it.source === "synth" : it.source === G.src)) return false;
    if (!ws.length) return true;
    const hay = [it.name, it.by, ...(it.tags || []), it.role || ""].join(" ").toLowerCase();
    return ws.every((w) => hay.includes(w));
  };

  // ------------------------------------------------------------ the header key
  function header() {
    const w = G.watch || {}, n = w.new || 0, scanning = (w.scanning || []).length > 0;
    countEl.textContent = n ? String(n) : "";
    lampEl.className = "lamp" + (scanning ? " amber pulse" : n ? " amber" : "");
    gearBtn.classList.toggle("hot", n > 0);
    gearBtn.title = (scanning ? `Scanning ${w.scanning.join(", ")}…\n` : n ? `${n} new since you last looked.\n` : "")
      + "Gear: every instrument and effect you have, new ones marked. Audition them and put them on a track.";
  }

  // ------------------------------------------------------------ the list
  function sections() {
    const ws = words(), items = (G.items || []).filter((i) => matches(i, ws));
    const out = [], fresh = items.filter((i) => i.new);
    if (fresh.length) out.push({ key: "new", head: "Just arrived", items: fresh, fresh: true });
    for (const [kind, srcs, head, grouped] of SECTIONS) {
      const its = items.filter((i) => !i.new && i.kind === kind && srcs.includes(i.source));
      if (!its.length) continue;
      if (!grouped) { out.push({ key: kind + srcs, head, items: its }); continue; }
      const groups = new Map();
      for (const i of its) { if (!groups.has(i.by)) groups.set(i.by, []); groups.get(i.by).push(i); }
      out.push({ key: kind + srcs, head, groups: [...groups].map(([by, list]) => ({ key: `${kind}|${srcs}|${by}`, by, items: list })), count: its.length });
    }
    return { out, total: items.length };
  }
  const isOpen = (g) => (G.fold.has(g.key) ? G.fold.get(g.key) : words().length > 0 || g.items.length <= FOLD_AT || g.items.some((i) => i.id === G.sel));
  function rowHtml(it) {
    const tags = (it.tags || []).slice(0, 3).map((t) => `<em>${esc(t)}</em>`).join("");
    const on = G.playing && G.playing.id === it.id;
    return `<div class="gRow${it.id === G.sel ? " sel" : ""}${it.missing ? " gone" : ""}${on ? " playing" : ""}" data-id="${esc(it.id)}" title="${esc(it.about || it.id)}">
      <span class="gN">${on ? '<i class="lamp green"></i>' : ""}${esc(it.name)}</span>
      <span class="gT">${it.new ? '<b class="tag stale">NEW</b>' : ""}${it.missing ? '<b class="tag bad">FILE GONE</b>' : ""}${tags}</span>
      ${it.new && it.by !== it.name ? `<span class="gB">${esc(it.by)}</span>` : ""}
    </div>`;
  }
  function drawList() {
    if (!G.items) { rowsEl.innerHTML = `<div class="empty">${G.loading ? "Reading your gear…" : ""}</div>`; return; }
    const { out, total } = sections();
    el.querySelector("#gSum").textContent = `${total} of ${G.items.length}`;
    if (!out.length) { rowsEl.innerHTML = `<div class="empty">Nothing matches${G.q ? ` “${esc(G.q)}”` : ""}.</div>`; return; }
    rowsEl.innerHTML = out.map((s) => {
      const n = s.items ? s.items.length : s.count;
      const head = `<h5 class="gH${s.fresh ? " fresh" : ""}"><span>${esc(s.head)}</span><span class="gC">${n}</span>${s.fresh ? '<button class="mini" data-seen title="They stop being marked new">MARK SEEN</button>' : ""}</h5>`;
      if (s.items) return head + s.items.map(rowHtml).join("");
      return head + s.groups.map((g) => {
        const open = isOpen(g);
        return `<div class="gG${open ? " open" : ""}" data-g="${esc(g.key)}"><span class="gCar">${open ? "▾" : "▸"}</span><span>${esc(g.by)}</span><span class="gC">${g.items.length}</span></div>`
          + (open ? `<div class="gGi">${g.items.map(rowHtml).join("")}</div>` : "");
      }).join("");
    }).join("");
    const sel = rowsEl.querySelector(".gRow.sel");
    if (sel && G.scrollSel) { sel.scrollIntoView({ block: "nearest" }); G.scrollSel = false; }
  }
  rowsEl.addEventListener("click", (e) => {
    const g = e.target.closest(".gG");
    if (g) { const k = g.dataset.g; G.fold.set(k, !g.classList.contains("open")); drawList(); return; }
    if (e.target.closest("[data-seen]")) { markSeen(); return; }
    const r = e.target.closest(".gRow"); if (r) select(r.dataset.id);
  });
  rowsEl.addEventListener("dblclick", (e) => { const r = e.target.closest(".gRow"); if (r) { select(r.dataset.id); audition(); } });
  function select(id, scroll = false) { G.sel = id; G.scrollSel = scroll; drawList(); drawDetail(); }
  function step(d) {
    const ids = [...rowsEl.querySelectorAll(".gRow")].map((r) => r.dataset.id);
    if (!ids.length) return;
    const i = ids.indexOf(G.sel);
    select(ids[Math.max(0, Math.min(ids.length - 1, i < 0 ? 0 : i + d))], true);
  }

  // ------------------------------------------------------------ the selected item
  function drawDetail() {
    const it = byId(G.sel);
    if (!it) { detailEl.innerHTML = `<p class="gHint">Pick something to hear it. Double-click or Enter auditions; ↑ ↓ move.</p>`; return; }
    const live = liveMode(), inst = it.kind === "instrument", on = G.playing && G.playing.id === it.id;
    const busy = G.busy === it.id;
    const phr = ["auto", "melody", "chords", "bass", "drums"].concat(live ? ["keys"] : [])
      .map((p) => `<option value="${p}"${G.phrase === p ? " selected" : ""}>${p === "keys" ? "keys only" : p}</option>`).join("");
    const what = it.source === "patch" ? `${it.kind === "effect" ? "Saved setting" : "Saved sound"} of ${it.by}`
      : it.source === "sfz" ? `SFZ · ${it.by}` : it.source === "vst3" ? `VST3 ${it.kind}` : `Built-in ${inst ? "synth" : "effect"}`;
    const hear = inst ? (live ? "Loops on the live engine in the project's key and tempo; your MIDI keyboard and KEYS play it too."
      : "Renders a short phrase in the project's key and tempo. Turn LIVE on to loop it and play it yourself.")
      : (live ? "Loops a built-in e-piano and drum kit through it on the live engine." : "Renders a built-in e-piano and drum kit through it.");
    detailEl.innerHTML = `
      <div class="gDn"><b>${esc(it.name)}</b>${it.new ? '<span class="tag stale">NEW</span>' : ""}${it.missing ? '<span class="tag bad">FILE GONE</span>' : ""}</div>
      <div class="gDm">${esc(what)}${it.role ? ` · plays ${esc(it.role)}` : ""}${it.added ? ` · arrived ${ago(it.added)}` : ""}</div>
      ${it.about ? `<p class="gDa">${esc(it.about)}</p>` : ""}
      ${(it.tags || []).length ? `<div class="gDt">${it.tags.map((t) => `<em>${esc(t)}</em>`).join("")}</div>` : ""}
      <div class="gKeys">
        ${inst ? `<select id="gPhrase" title="What it plays: auto picks drums for kits, a bass line for basses, chords for pads">${phr}</select>` : ""}
        <button id="gHear" class="key${on ? " on" : ""}" ${it.missing || busy ? "disabled" : ""} title="${esc(hear)}">${busy ? "LOADING…" : on ? "■ STOP" : live ? "▶ AUDITION · LIVE" : "▶ AUDITION"}</button>
        <button id="gUse" class="key go" ${it.missing ? "disabled" : ""} title="${inst ? "Make it a track's instrument, or start a new track with it" : "Put it on a track, the master, or a new bus"}">USE ▾</button>
      </div>
      <div class="gId" title="What the tools call it">${esc(it.id)}</div>`;
    const ph = detailEl.querySelector("#gPhrase");
    if (ph) ph.onchange = () => { G.phrase = ph.value; if (on) audition(true); };
    detailEl.querySelector("#gHear").onclick = () => audition();
    detailEl.querySelector("#gUse").onclick = (e) => useMenu(e, it);
  }

  // ------------------------------------------------------------ hearing it
  function stopLocal() {
    if (G.audio) { G.audio.pause(); G.audio = null; }
    G.playing = null;
  }
  async function stopAudition() {
    const p = G.playing; stopLocal();
    if (p && p.live) {
      try { liveState(await call("gear_audition_stop", { take: S.take || null })); } catch (e) { toast(e.message); }
    }
    redraw();
  }
  // again: the phrase changed while it plays; the live engine keeps going with the new one
  async function audition(again = false) {
    const it = byId(G.sel); if (!it || it.missing || G.busy) return;
    if (!again && G.playing && G.playing.id === it.id) { stopAudition(); return; }
    const st = S.eng.st || {};
    if (st.recording) { toast("Recording: stop it before auditioning gear."); return; }
    const live = liveMode();
    window.Studio?.hush();
    if (S.audio.playing) stop(false);  // the Desk's preview
    if (G.audio) { G.audio.pause(); G.audio = null; }
    G.busy = it.id; G.playing = null; drawDetail(); drawList();
    try {
      const r = await call("gear_audition", { item: it.id, phrase: it.kind === "instrument" ? G.phrase : "auto", live });
      if (r.live) G.playing = { id: it.id, live: true, seen: false, at: performance.now() };
      else {
        if (r.silent) toast(`${it.name} made no sound in the audition: it may need a saved sound (a patch) or have none loaded.`);
        const a = new Audio("/api/audio?file=" + encodeURIComponent(r.file) + "&t=" + Date.now());
        a.onended = () => { if (G.audio === a) { G.audio = null; G.playing = null; redraw(); } };
        G.audio = a; G.playing = { id: it.id, live: false };
        await a.play().catch(() => { G.audio = null; G.playing = null; });
      }
    } catch (e) { toast(e.message); }
    G.busy = null; redraw();
  }
  // the live engine's state: an audition ends when anything else takes the engine (the Desk's play, stop, LIVE off)
  liveES.addEventListener("message", () => {
    const p = G.playing; if (!p || !p.live) return;
    const st = S.eng.st || {}, on = !!(st.on && st.audition && st.playing);
    if (on) p.seen = true;
    else if (p.seen || performance.now() - p.at > 15000) { G.playing = null; redraw(); }
  });

  // ------------------------------------------------------------ putting it to work
  const trackLabel = (t) => `${t.id} ${t.name}`;
  function useMenu(e, it) {
    if (!S.doc) { toast("Open or start a project first (the project button, top left)."); return; }
    const where = S.take ? ` (in ${S.take.toUpperCase()})` : "";
    const done = async (fn, args, target) => {
      try {
        const r = await act(fn, args);
        if (fn === "track_add" && r && r.track) S.sel.track = r.track;
        toast(`${it.name} is on ${target}${where}.`, true);
        if (it.new) markSeen([it.id]);
      } catch { /* act said why */ }
    };
    const items = [];
    if (it.kind === "instrument") {
      const midi = S.doc.tracks.filter((t) => t.kind === "midi");
      const sel = midi.find((t) => t.id === S.sel.track);
      if (midi.length) items.push(["Instrument of" + where]);
      for (const t of sel ? [sel, ...midi.filter((x) => x !== sel)] : midi) {
        items.push([`${trackLabel(t)}${t === sel ? "  · selected" : ""}`, () => done("track_update", { track: t.id, instrument: it.id }, trackLabel(t))]);
      }
      items.push(["New track"], [short(`+ ${it.name}`, 44), () => done("track_add", { name: short(it.name, 40), instrument: it.id, ...(it.role ? { role: it.role } : {}) }, "a new track")]);
    } else {
      items.push(["Insert on" + where]);
      const sel = S.doc.tracks.find((t) => t.id === S.sel.track);
      for (const t of sel ? [sel, ...S.doc.tracks.filter((x) => x !== sel)] : S.doc.tracks) {
        items.push([`${trackLabel(t)}${t === sel ? "  · selected" : ""}`, () => done("fx_add", { track: t.id, device: it.id }, trackLabel(t))]);
      }
      items.push(["Master", () => done("fx_add", { track: "master", device: it.id }, "the master")]);
      items.push(["New bus"], [short(`+ ${it.name} bus`, 44), () => done("track_add", { name: short(it.name, 40), kind: "bus", fx: [it.id] }, "a new bus")]);
    }
    const r = e.currentTarget.getBoundingClientRect();
    menu({ clientX: Math.min(r.left, innerWidth - 220), clientY: r.bottom + 2 }, items.slice(0, 40));
  }
  async function markSeen(ids) {
    const all = !ids;
    ids = ids || (G.items || []).filter((i) => i.new).map((i) => i.id);
    if (!ids.length) return;
    try {
      G.watch = await call("gear_seen", all ? {} : { ids });
      for (const i of G.items || []) if (ids.includes(i.id)) { delete i.new; delete i.added; }
      G.mark = mark(G.watch); header(); redraw();
    } catch (e) { toast(e.message); }
  }

  // ------------------------------------------------------------ folders, scans
  function drawFolders() {
    const w = G.watch; const box = el.querySelector("#gFolders");
    if (!w) { box.innerHTML = '<div class="empty">…</div>'; return; }
    const list = (kind, dirs) => dirs.map((d) => `<div class="gF"><span title="${esc(d)}">${esc(d)}</span><button class="mini" data-rm="${esc(d)}" data-kind="${kind}" title="Stop watching this folder (what was found in it stays)">×</button></div>`).join("")
      || '<div class="gF muted">none</div>';
    const add = (kind, ph) => `<form class="gAdd" data-kind="${kind}"><input placeholder="${ph}" spellcheck="false" aria-label="Folder to watch"><button class="key">ADD</button></form>`;
    const failed = w.failed || [];
    box.innerHTML = `
      <h5 class="gH"><span>Plug-in folders · VST3</span></h5>${list("vst3", w.folders.vst3)}${add("vst3", "paste a folder path, e.g. C:\\Program Files\\Common Files\\VST3")}
      <h5 class="gH"><span>Sample folders · SFZ</span></h5>${list("samples", w.folders.samples)}${add("samples", "a folder with .sfz instruments in it")}
      <p class="gHint">TuneTrack looks at these every 15 s. A new plug-in is scanned once its files stop changing (an installer still copying is left alone), then it shows here marked NEW.</p>
      <div class="gKeys"><button class="key" id="gRescan" title="Look now and scan whatever is new">RESCAN NOW</button></div>
      <h5 class="gH${failed.length ? " bad" : ""}"><span>Failed to load</span><span class="gC">${failed.length}</span>${failed.length ? '<button class="mini" id="gRetry" title="Try every failed plug-in again (after updating or reinstalling it)">RETRY ALL</button>' : ""}</h5>
      ${failed.map((f) => `<div class="gFail"><b>${esc(f.name)}</b><span>${esc(f.error || "no reason given")}</span></div>`).join("") || '<div class="gF muted">nothing</div>'}
      ${(w.found || []).length ? `<h5 class="gH"><span>Found lately</span></h5><div class="gF">${w.found.map(esc).join(" · ")}</div>` : ""}`;
    box.querySelectorAll("[data-rm]").forEach((b) => b.onclick = () => folders({ remove: b.dataset.rm, kind: b.dataset.kind }));
    box.querySelectorAll(".gAdd").forEach((f) => f.onsubmit = (e) => {
      e.preventDefault(); const v = f.querySelector("input").value.trim(); if (v) folders({ add: v, kind: f.dataset.kind });
    });
    box.querySelector("#gRescan").onclick = () => rescan(false);
    const rt = box.querySelector("#gRetry"); if (rt) rt.onclick = () => rescan(true);
  }
  async function folders(args) {
    try { G.watch = await call("gear_folders", args); watched(); } catch (e) { toast(e.message); }
  }
  async function rescan(retry) {
    try { G.watch = await call("gear_rescan", { retry_failed: retry }); watched(); toast(retry ? "Trying the failed plug-ins again…" : "Looking now…", true); poll(1500); }
    catch (e) { toast(e.message); }
  }
  function drawWatch() {
    const w = G.watch, box = el.querySelector("#gWatch");
    if (!w) { box.textContent = ""; return; }
    const nf = w.folders.vst3.length + w.folders.samples.length;
    let text, cls = "";
    if (w.error) { text = "WATCHER: " + w.error; cls = "bad"; }
    else if ((w.scanning || []).length) { text = "SCANNING " + w.scanning.join(", ") + " …"; cls = "amb"; }
    else if ((w.waiting || []).length) { text = "WAITING FOR " + w.waiting.join(", ") + " TO FINISH COPYING"; cls = "amb"; }
    else text = `WATCHING ${nf} FOLDER${nf === 1 ? "" : "S"}` + (w.last_poll ? ` · LOOKED ${ago(w.last_poll).toUpperCase()}` : w.watching ? " · STARTING" : " · OFF");
    if ((w.failed || []).length && !cls) text += ` · ${w.failed.length} FAILED`;
    box.innerHTML = `<span class="${cls}">${esc(text)}</span>`;
  }

  // ------------------------------------------------------------ keeping up with the watcher
  // the catalog is read again when the watcher found or scanned something since
  const mark = (w) => (w ? `${w.last_scan || 0}|${w.new || 0}|${(w.found || []).join(",")}|${w.folders.vst3.length}|${w.folders.samples.length}` : null);
  function watched() {
    header(); drawWatch(); if (G.tab === "folders") drawFolders();
    const m = mark(G.watch);
    if (G.open && G.mark !== null && m !== G.mark) load();
    else G.mark = m;
  }
  async function poll(delay) {
    clearTimeout(G.timer);
    G.timer = setTimeout(async () => {
      if (document.visibilityState === "visible" || G.open) {
        try { G.watch = await call("gear_state"); watched(); } catch { /* the server is restarting: try later */ }
      }
      const w = G.watch || {}, busy = (w.scanning || []).length || (w.waiting || []).length;
      poll(G.open ? (busy ? 2500 : 10000) : 20000);
    }, delay);
  }
  async function load() {
    G.loading = true; if (!G.items) drawList();
    try {
      const c = await call("catalog");
      G.items = c.items; G.watch = c.watch; G.mark = mark(c.watch);
      if (G.sel && !byId(G.sel)) G.sel = null;
    } catch (e) { toast(e.message); }
    G.loading = false; header(); redraw(); drawWatch();
  }
  function redraw() { if (!G.open) return; drawList(); drawDetail(); if (G.tab === "folders") drawFolders(); }

  // ------------------------------------------------------------ the drawer
  function topH() { document.documentElement.style.setProperty("--topH", $("#top").offsetHeight + "px"); }
  function open() {
    G.open = true; topH(); el.classList.remove("hidden"); gearBtn.classList.add("on");
    load(); poll(2500); setTimeout(() => qEl.focus(), 0);
  }
  function close() {
    G.open = false; el.classList.add("hidden"); gearBtn.classList.remove("on");
    if (G.playing) stopAudition();
    poll(20000);
  }
  gearBtn.onclick = () => (G.open ? close() : open());
  el.querySelector("#gClose").onclick = close;
  addEventListener("resize", () => { if (G.open) topH(); });
  el.querySelectorAll("[data-gt]").forEach((b) => b.onclick = () => {
    G.tab = b.dataset.gt;
    el.querySelectorAll("[data-gt]").forEach((x) => x.classList.toggle("on", x === b));
    el.querySelectorAll(".gPane").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== G.tab));
    if (G.tab === "folders") drawFolders();
  });
  let qTimer = 0;
  qEl.oninput = () => { clearTimeout(qTimer); qTimer = setTimeout(() => { G.q = qEl.value.trim(); drawList(); }, 90); };
  el.querySelectorAll("[data-k]").forEach((b) => b.onclick = () => {
    G.kind = b.dataset.k; el.querySelectorAll("[data-k]").forEach((x) => x.classList.toggle("on", x === b)); drawList();
  });
  el.querySelectorAll("[data-s]").forEach((b) => b.onclick = () => {
    G.src = b.dataset.s; el.querySelectorAll("[data-s]").forEach((x) => x.classList.toggle("on", x === b)); drawList();
  });
  // keys inside the drawer are the drawer's (Space would otherwise play the Desk)
  el.addEventListener("keydown", (e) => {
    const inField = ["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName) && e.target !== qEl;
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); if (qEl.value && e.target === qEl) { qEl.value = ""; G.q = ""; drawList(); } else close(); return; }
    if (inField || G.tab !== "list") { e.stopPropagation(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); step(e.key === "ArrowDown" ? 1 : -1); }
    else if (e.key === "Enter") { e.preventDefault(); audition(); }
    else if (e.code === "Space" && e.target !== qEl) { e.preventDefault(); audition(); }
    else return;
    e.stopPropagation();
  });
  // Escape with the focus outside the drawer (on the Desk) closes it too, unless a dialog or menu is up
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && G.open && !el.contains(document.activeElement) && $("#modal").classList.contains("hidden")) close();
  });

  window.Gear = { open, close, state: () => G };
  poll(1200);
})();
