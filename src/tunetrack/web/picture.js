// The picture: a video locked to the transport, its lane under the chord track, and the hit points.
// The video lives in seconds and the music in beats: the picture's first frame plays at `picture.start` seconds
// of the song, and a hit is a moment of the picture (`t`, seconds from its first frame). So when the tempo
// changes, the frames and hits stay where they are in time and the bars move under them.
// Loaded after app.js and shares its globals (S, $, api, act, menu, modal, beatToSec, ...).
"use strict";

const Picture = (() => {
  const cv = $("#pic"), win = $("#picWin"), vid = $("#picVid"), tcEl = $("#picTc"), vol = $("#picVol");
  let strip = null, stripKey = "", srcKey = "", pending = null, shown = false;
  // A drag in the lane: a hit along the picture ({kind: "hit"}), or the whole picture against the music ({kind:
  // "start"}). While it lasts, start() and hits() answer with the dragged value so the lane, the hit lines and the
  // video window follow the pointer; nothing is written until the button comes up.
  let drag = null;
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { /* no storage */ } return null; };

  const pic = () => (S.doc && S.doc.picture) || null;
  const same = (a, b) => a.name === b.name && Math.abs(a.t - b.t) < 1e-9;  // by value: a reload mid-drag makes new objects
  const hits = () => {
    const hs = (S.doc && S.doc.hits) || [];
    return drag && drag.kind === "hit" && drag.moved ? hs.map((h) => (same(h, drag.h) ? { ...h, t: drag.t } : h)) : hs;
  };
  const start = () => (drag && drag.kind === "start" ? drag.start : (pic() && pic().start) || 0);
  const fps = () => (pic() && pic().fps) || 25;
  const tToBeat = (t) => secToBeat(t + start());
  const beatToT = (b) => beatToSec(b) - start();
  const lanePicH = () => parseInt(getComputedStyle(document.body).getPropertyValue("--picH")) || 0;
  const q = () => (S.take ? "&take=" + encodeURIComponent(S.take) : "");

  // HH:MM:SS:FF, or HH:MM:SS;FF drop-frame (29.97 / 59.94: labels skip frame numbers to keep up with the clock).
  // The same arithmetic as picture.timecode() on the server.
  function tc(t) {
    const f = fps(), base = Math.max(1, Math.round(f)), p = pic();
    const d = p && p.df ? (Math.abs(f - 29.97) < 0.01 ? 2 : Math.abs(f - 59.94) < 0.02 ? 4 : 0) : 0;
    const frames = (s) => {
      const a = s.split(/[:;]/).map(Number), m = a[0] * 60 + a[1];
      return (m * 60 + a[2]) * base + a[3] - (d ? d * (m - Math.floor(m / 10)) : 0);
    };
    let n = Math.round(t * f) + (p && p.tc ? frames(p.tc) : 0);
    const neg = n < 0; n = Math.abs(n);
    if (d) { const per10 = base * 600 - d * 9, tens = Math.floor(n / per10), r = n % per10; n += d * 9 * tens + (r > d ? d * Math.floor((r - d) / (base * 60 - d)) : 0); }
    const two = (x) => String(x).padStart(2, "0");
    return (neg ? "-" : "") + `${two(Math.floor(n / (base * 3600)))}:${two(Math.floor(n / (base * 60)) % 60)}:${two(Math.floor(n / base) % 60)}${d ? ";" : ":"}${two(n % base)}`;
  }
  // How far a hit is from the nearest beat, in seconds (+ = the hit comes after the beat).
  function offBeat(t) { const b = tToBeat(t), nb = Math.round(b); return { off: (t + start()) - beatToSec(nb), bar: Math.abs(nb / bpb() - Math.round(nb / bpb())) < 1e-6 }; }

  // ------------------------------------------------------------ the lane
  function draw() {
    const on = !!(pic() || hits().length);
    if (on !== shown) { shown = on; document.body.classList.toggle("haspic", on); requestAnimationFrame(drawArrangement); }
    $("#picCorner").classList.toggle("hidden", !on);
    if (!on) { win.classList.add("hidden"); if (!vid.paused) vid.pause(); return; }
    $("#picView").classList.toggle("hidden", !pic()); $("#picView").classList.toggle("on", !win.classList.contains("hidden"));
    if (pic() && store("tt.pic.win") !== "0" && win.classList.contains("hidden") && !win.dataset.closed) openWin();
    if (!pic()) win.classList.add("hidden");
    const W = lanesWrap.clientWidth, H = Math.max(10, lanePicH() - 1), g = fitCanvas(cv, W, H), sx = lanesWrap.scrollLeft;
    g.fillStyle = "#0c0c0d"; g.fillRect(0, 0, W, H);
    const X = (t) => tToBeat(t) * S.zoom - sx;
    const p = pic(), view = S.pic;
    if (p) {
      const dur = p.duration || 0, x0 = X(0), x1 = X(dur);
      g.fillStyle = "#141416"; g.fillRect(x0, 0, x1 - x0, H);
      loadStrip();
      if (strip && strip.complete && strip.naturalWidth && view && view.strip) {
        const m = view.strip, tw = (H - 4) * m.w / m.h;
        for (let i = 0; i < m.n; i++) {
          const a = X(i * m.step), b = X(Math.min(dur, (i + 1) * m.step));
          if (b < 0 || a > W) continue;
          g.save(); g.beginPath(); g.rect(a, 2, Math.max(1, b - a - 1), H - 4); g.clip();
          for (let x = a; x < b; x += tw + 1) g.drawImage(strip, i * m.w, 0, m.w, m.h, x, 2, tw, H - 4);
          g.restore();
        }
      } else { g.fillStyle = "#5c574d"; g.font = "11px Segoe UI, system-ui, sans-serif"; g.textBaseline = "middle"; g.fillText(p.file.split("/").pop(), Math.max(6, x0 + 6), H / 2); }
      g.strokeStyle = drag && drag.kind === "start" ? "#5cc8ff" : "#3b3b40"; g.strokeRect(Math.round(x0) + 0.5, 1.5, Math.round(x1 - x0) - 1, H - 3);
      g.fillStyle = drag && drag.kind === "start" ? "#5cc8ff" : "#8f897c"; g.fillRect(Math.round(x0), 0, 3, H);  // the first frame: grab it to move the picture
      for (const t of (view && view.cuts) || []) { const x = X(t); if (x >= 0 && x <= W) { g.fillStyle = "rgba(255,255,255,.75)"; g.fillRect(Math.round(x), 0, 1, 5); g.fillRect(Math.round(x), H - 5, 1, 5); } }
    }
    g.font = "10px Cascadia Mono, Consolas, ui-monospace, monospace"; g.textBaseline = "top";
    for (const h of hits()) {
      const x = Math.round(X(h.t)); if (x < -160 || x > W) continue;
      const o = offBeat(h.t), exact = Math.abs(o.off) <= 0.5 / fps() + 1e-6, ms = Math.round(o.off * 1000);
      const col = exact ? "#5fe07a" : h.soft ? "#b98a2c" : "#ffb000";
      const label = h.name + (exact ? (o.bar ? "  ● bar" : "  ● beat") : `  ${ms > 0 ? "+" : ""}${ms} ms`);
      g.fillStyle = col; g.fillRect(x, 0, h.soft ? 1 : 2, H);
      const w = g.measureText(label).width + 8;
      g.fillStyle = "rgba(9,9,10,.86)"; g.fillRect(x + 2, 2, w, 14);
      g.fillStyle = col; g.fillText(label, x + 6, 4);
    }
    g.fillStyle = "#ff4a3a"; g.fillRect(S.playhead * S.zoom - sx, 0, 1.5, H);
  }
  // Hit lines through the track lanes (drawn by drawArrangement, in its own coordinates).
  function overlay(g, H) {
    for (const h of hits()) {
      const o = offBeat(h.t), exact = Math.abs(o.off) <= 0.5 / fps() + 1e-6;
      g.fillStyle = exact ? "rgba(95,224,122,.30)" : "rgba(255,176,0,.28)";
      for (let y = 0; y < H; y += 6) g.fillRect(Math.round(tToBeat(h.t) * S.zoom), y, 1, 3);
    }
  }
  function loadStrip() {
    const p = pic(), key = p && S.pic && S.pic.strip ? S.path + "|" + p.file : "";
    if (key === stripKey) return;
    stripKey = key; strip = null;
    if (!key) return;
    strip = new Image(); strip.onload = draw; strip.src = "/api/picture?kind=strip" + q() + "&v=" + encodeURIComponent(key);
  }
  const laneB = (e, free) => { const b = (e.clientX - cv.getBoundingClientRect().left + lanesWrap.scrollLeft) / S.zoom; return free ? b : Math.max(0, b); };
  const laneT = (e) => beatToT(laneB(e));
  function hitNear(e) {
    const x = e.clientX - cv.getBoundingClientRect().left + lanesWrap.scrollLeft;
    return hits().find((h) => Math.abs(tToBeat(h.t) * S.zoom - x) <= 5) || null;
  }
  // the picture's first frame (its left edge in the lane), or anywhere on it with Shift: what moves the picture
  function onPicture(e) {
    const p = pic(); if (!p) return false;
    const x = e.clientX - cv.getBoundingClientRect().left + lanesWrap.scrollLeft, x0 = tToBeat(0) * S.zoom;
    if (Math.abs(x - x0) <= 5) return true;
    const t = laneT(e); return e.shiftKey && t >= 0 && t <= (p.duration || 0);
  }
  cv.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || !S.doc) return;
    const h = hitNear(e);
    if (h) { drag = { kind: "hit", h: { ...h }, t: h.t, x0: e.clientX, moved: false }; setPlayhead(tToBeat(h.t)); }
    else if (onPicture(e)) drag = { kind: "start", x0: e.clientX, s0: beatToSec(laneB(e, true)), start0: start(), start: start(), moved: false };
    else { setPlayhead(tToBeat(laneT(e))); return; }
    e.preventDefault();
  });
  addEventListener("mousemove", (e) => {
    if (!drag) return;
    if (Math.abs(e.clientX - drag.x0) > 3) drag.moved = true;
    if (!drag.moved) return;
    const f = fps();
    if (drag.kind === "hit") {
      // a hit is a moment of the picture: it moves frame by frame, and the video shows the frame it is on
      drag.t = Math.max(0, Math.round(laneT(e) * f) / f);
      if (pic()) drag.t = Math.min(drag.t, pic().duration || drag.t);
      if (!isPlaying()) { S.playhead = Math.max(0, tToBeat(drag.t)); $("#pos").textContent = fmtPos(S.playhead); }
      status(`${drag.h.name} · ${pic() ? tc(drag.t) : drag.t.toFixed(2) + " s"}`);
    } else {
      // the whole picture slides against the music; its first frame lands on the grid (Alt: anywhere, to the ms)
      const want = drag.start0 + (beatToSec(laneB(e, true)) - drag.s0);  // past the left edge: the music enters later
      drag.start = e.altKey || !(S.snap > 0) ? Math.round(want * 1000) / 1000 : +beatToSec(snapB(secToBeat(want))).toFixed(4);
      status(`first frame at ${drag.start.toFixed(3)} s of the music (${fmtPos(Math.max(0, secToBeat(drag.start)))})${e.altKey ? "" : " · Alt: off the grid"}`);
    }
    drawArrangement();
  });
  addEventListener("mouseup", () => {
    const d = drag; if (!d) return;
    drag = null; status("");
    if (!d.moved) { drawArrangement(); return; }
    if (d.kind === "hit" && Math.abs(d.t - d.h.t) > 1e-6)
      act("hits", { remove: [d.h.t], add: [{ t: +d.t.toFixed(4), name: d.h.name, soft: !!d.h.soft }] }).catch(() => {});
    else if (d.kind === "start" && Math.abs(d.start - d.start0) > 1e-6) act("picture", { start: d.start }).catch(() => {});
    else drawArrangement();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drag) { drag = null; status(""); drawArrangement(); } });
  cv.addEventListener("dblclick", (e) => { if (S.doc && !hitNear(e)) addHit(laneT(e)); });
  cv.addEventListener("mousemove", (e) => {
    if (drag) return;
    cv.style.cursor = !S.doc ? "default" : hitNear(e) || onPicture(e) ? "ew-resize" : "crosshair";
    cv.title = pic() ? "Click: move the playhead · drag a hit to move it · drag the first frame (or Shift-drag the picture) to move the picture against the music · double-click or H: mark a hit" : "";
  });
  cv.addEventListener("contextmenu", (e) => {
    e.preventDefault(); if (!S.doc) return;
    const t = laneT(e), h = hitNear(e);
    menu(e, [
      ...(h ? [[`Rename "${h.name}"…`, () => { const n = prompt("Hit name", h.name); if (n && n !== h.name) act("hits", { remove: [h.t], add: [{ t: h.t, name: n, soft: !!h.soft }] }); }],
        [h.soft ? "Make it a hard hit (must land on a beat)" : "Make it a soft hit (near a beat is enough)", () => act("hits", { remove: [h.t], add: [{ t: h.t, name: h.name, soft: !h.soft }] })],
        [`Remove "${h.name}"`, () => act("hits", { remove: [h.t] })]] : [["Add a hit point here…", () => addHit(t)]]),
      ["Fit the tempo to the hits…", openFit],
      ...(pic() ? [["Put the picture's first frame here", () => act("picture", { start: +beatToSec(snapB(tToBeat(t))).toFixed(4) })]] : []),
      ["Picture…", openSetup],
    ]);
  });
  function addHit(t, name) {
    if (pic()) t = Math.round(t * fps()) / fps();  // on a frame
    const n = name || prompt(`Hit point at ${pic() ? tc(t) : t.toFixed(2) + " s"}: what happens here?`, `Hit ${hits().length + 1}`);
    if (n) act("hits", { add: [{ t: +t.toFixed(4), name: n }] }).catch(() => {});
  }
  // H marks a hit at the playhead. While playing it does not stop to ask: you tap along and name them afterwards.
  document.addEventListener("keydown", (e) => {
    if (e.key.toLowerCase() !== "h" || e.ctrlKey || e.altKey || e.metaKey || !S.doc || document.body.dataset.room !== "desk") return;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) || !$("#modal").classList.contains("hidden")) return;
    if (!pic() && !hits().length) return;
    e.preventDefault(); addHit(beatToT(S.playhead), isPlaying() ? `Hit ${hits().length + 1}` : null);
  });

  // ------------------------------------------------------------ the window
  function openWin() {
    win.classList.remove("hidden"); delete win.dataset.closed; store("tt.pic.win", "1");
    try { const r = JSON.parse(store("tt.pic.box") || "null"); if (r) { win.style.left = Math.max(0, Math.min(innerWidth - 120, r.x)) + "px"; win.style.top = Math.max(40, Math.min(innerHeight - 80, r.y)) + "px"; win.style.width = r.w + "px"; win.style.right = "auto"; win.style.bottom = "auto"; } } catch { /* default place */ }
    $("#picView").classList.add("on"); sync(); guideSync();
  }
  function closeWin() { win.classList.add("hidden"); win.dataset.closed = "1"; store("tt.pic.win", "0"); $("#picView").classList.remove("on"); vid.pause(); guideSync(); }
  const saveBox = () => { const r = win.getBoundingClientRect(); store("tt.pic.box", JSON.stringify({ x: r.left, y: r.top, w: r.width })); };
  $("#picView").onclick = () => (win.classList.contains("hidden") ? openWin() : closeWin());
  $("#picClose").onclick = closeWin;
  $("#picBar").addEventListener("mousedown", (e) => {
    if (e.target.closest("button, input")) return;
    const r = win.getBoundingClientRect(), dx = e.clientX - r.left, dy = e.clientY - r.top;
    const move = (m) => { win.style.left = Math.max(0, Math.min(innerWidth - 80, m.clientX - dx)) + "px"; win.style.top = Math.max(0, Math.min(innerHeight - 40, m.clientY - dy)) + "px"; win.style.right = "auto"; win.style.bottom = "auto"; };
    const up = () => { removeEventListener("mousemove", move); removeEventListener("mouseup", up); saveBox(); };
    addEventListener("mousemove", move); addEventListener("mouseup", up); e.preventDefault();
  });
  new ResizeObserver(() => { if (!win.classList.contains("hidden")) saveBox(); }).observe(win);
  // The video's own sound (dialogue, effects) as a guide under the music. Off by default; never part of a render.
  // In preview the browser plays it from the video. On LIVE the engine holds the audio driver, so the browser's
  // sound would come out of another device: the video is muted and the engine plays the guide on a strip of its own
  // (live_guide), for as long as the video window is open.
  function setVol(v) { vid.volume = Math.max(0, Math.min(1, v / 100)); vid.muted = v <= 0 || liveMode(); }
  vol.value = +(store("tt.pic.vol") || 0); setVol(+vol.value);
  vol.oninput = () => { setVol(+vol.value); store("tt.pic.vol", vol.value); guideSync(); };
  let guideBusy = false;
  function guideWant() {
    const p = pic(), v = +vol.value;
    return p && p.audio && v > 0 && !win.classList.contains("hidden") ? Math.round(200 * Math.log10(v / 100)) / 10 : null;
  }
  // called with every LIVE state: one call at a time, and only when the engine's level is not the one wanted
  function guideSync() {
    vid.muted = +vol.value <= 0 || liveMode();
    const st = S.eng.st;
    if (!liveMode() || !st.on || guideBusy || st.loading) return;
    const want = guideWant();
    if (want === (st.guide ?? null)) return;
    guideBusy = true;
    api("live_guide", { db: want, take: S.take || null })
      .then((r) => { guideBusy = false; liveState(r); }, (e) => { guideBusy = false; st.guide = want; toast(e.message); });
  }

  // The video follows the playhead: it runs by itself while the transport plays and is pulled back when it has
  // drifted more than a few frames; stopped, it shows the frame under the playhead.
  function sync() {
    const p = pic();
    if (!p || win.classList.contains("hidden") || document.body.dataset.room !== "desk") { if (!vid.paused) vid.pause(); return; }
    const key = S.path + "|" + p.file + "|" + (S.take || "");
    if (key !== srcKey) { srcKey = key; vid.src = "/api/picture?kind=video" + q() + "&v=" + encodeURIComponent(key); }
    const t = beatToT(S.playhead), dur = p.duration || 0, inside = t >= 0 && t < dur - 0.5 / fps();
    tcEl.textContent = inside ? tc(t) : t < 0 ? "BEFORE PICTURE" : "PICTURE OVER";
    vid.style.visibility = inside ? "visible" : "hidden";
    if (!inside) { if (!vid.paused) vid.pause(); return; }
    if (isPlaying()) {
      if (vid.paused) { vid.currentTime = t; vid.play().catch(() => {}); }
      else if (Math.abs(vid.currentTime - t) > 0.12) vid.currentTime = t;
    } else {
      if (!vid.paused) vid.pause();
      if (Math.abs(vid.currentTime - t) > 0.5 / fps()) { if (vid.seeking) pending = t; else vid.currentTime = t; }
    }
  }
  vid.addEventListener("seeked", () => { if (pending != null && !isPlaying()) { const t = pending; pending = null; vid.currentTime = t; } else pending = null; });
  setInterval(() => { if (!vid.paused && !isPlaying()) sync(); }, 150);  // the transport stopped: back to its frame

  // ------------------------------------------------------------ dialogs
  function openSetup() {
    if (!S.doc) return;
    const p = pic();
    modal(`<h2>Picture</h2>
      <p class="hint">${p ? `<b>${esc(p.file.split("/").pop())}</b> · ${p.duration} s · ${p.fps} fps · ${p.size ? p.size.join("×") : ""}${p.tc ? " · starts at " + esc(p.tc) : ""}`
        : "A video the music is written to: a cutscene, a trailer, recorded gameplay. It is copied into the session and follows the transport. A screenshot is kept as a mood reference for the crew."}</p>
      <div class="grid2">
        <span>${p ? "Replace with" : "Video or screenshot"}</span><input id="pcFile" placeholder="C:\\Videos\\cutscene.mp4  (the full path of the file)" spellcheck="false">
        <span>First frame at</span><label><input id="pcStart" type="number" step="0.01" value="${start()}" style="width:90px"> seconds into the music (negative: the music enters later)</label>
      </div>
      <div class="row">${p ? '<button class="ghost" id="pcClear">Remove picture</button><button class="ghost" id="pcExport" title="Render the mix and put it under the video">Export video with music</button>' : ""}
        <button class="ghost" id="pcCancel">Cancel</button><button id="pcGo">${p ? "Apply" : "Set picture"}</button></div>`);
    $("#pcCancel").onclick = closeModal;
    $("#pcGo").onclick = async () => {
      const file = $("#pcFile").value.trim().replace(/^"|"$/g, ""), st = +$("#pcStart").value || 0;
      if (!file && !p) return;
      closeModal();
      try { await act("picture", file ? { file, start: st } : { start: st }, file ? "reading the picture…" : ""); delete win.dataset.closed; } catch { /* act said why */ }
    };
    if (p) {
      $("#pcClear").onclick = () => { closeModal(); act("picture", { clear: true }).catch(() => {}); };
      $("#pcExport").onclick = async () => {
        closeModal(); status("rendering the video…", "busy");
        try { const r = await api("render", { out: S.doc.name.replace(/[^\w\- ]+/g, "_") + "-picture.mp4" }); S.lastRender = r; status("exported"); toast("Video with music: " + r.files[0], true); }
        catch (e) { toast(e.message); status("export failed", "err"); }
      };
    }
  }
  async function openFit() {
    if (!S.doc) return;
    if (!hits().length) { toast("No hit points yet. Double-click the picture lane, or press H at the playhead, to mark one."); return; }
    const box = modal(`<h2>Fit the tempo to the hits</h2>
      <p class="hint">The hits stay with the picture; the tempo decides which beat each one lands on. Notes already written keep their bars, so do this before the parts are written.</p>
      <div class="row" style="justify-content:flex-start;margin:0 0 10px">
        <label class="field">FROM <input id="tfLow" type="number" value="${Math.round(Math.max(40, S.doc.tempo * 0.75))}" style="width:64px"></label>
        <label class="field">TO <input id="tfHigh" type="number" value="${Math.round(Math.min(240, S.doc.tempo * 1.25))}" style="width:64px"></label>
        <label class="field">GRID <select id="tfGrid"><option value="1">beats</option><option value="0.5">eighths</option></select></label>
        ${pic() ? `<label class="field" title="Let the picture slide against the music by under a beat, so the music need not enter on its first frame: each tempo then says where the first frame goes"><input type="checkbox" id="tfFree"> MOVE THE PICTURE TOO</label>` : ""}
        <button id="tfFind" class="key">FIND</button></div>
      <div id="tfOut"></div>
      <div class="row"><button class="ghost" id="tfClose">Close</button></div>`);
    $("#tfClose").onclick = closeModal;
    const out = box.querySelector("#tfOut");
    const use = (args) => { closeModal(); act("tempo_fit", { ...args, apply: true }, "setting the tempo…").catch(() => {}); };
    const find = async () => {
      out.innerHTML = '<p class="hint">looking…</p>';
      try {
        const free = !!($("#tfFree") && $("#tfFree").checked), grid = +$("#tfGrid").value;
        const r = await api("tempo_fit", { low: +$("#tfLow").value, high: +$("#tfHigh").value, grid, free_start: free });
        const names = Object.keys(r.candidates[0].hits);
        const moved = (c) => `${c.start} s <span class="hint">(${c.shift_ms > 0 ? "+" : ""}${c.shift_ms} ms)</span>`;
        out.innerHTML = `<table class="tf"><tr><th>BPM</th><th>typical miss</th>${free ? "<th>first frame at</th>" : ""}${names.map((n) => `<th>${esc(n)}</th>`).join("")}<th></th><th></th></tr>`
          + r.candidates.map((c, i) => `<tr><td class="num">${c.tempo}</td><td class="num">${c.rms_ms} ms</td>${free ? `<td class="num">${moved(c)}</td>` : ""}`
            + names.map((n) => `<td class="num ${Math.abs(c.hits[n]) <= 40 ? "ok" : "off"}">${c.hits[n] > 0 ? "+" : ""}${c.hits[n]}</td>`).join("")
            + `<td><button class="mini" data-steady="${i}" title="${free ? "One tempo throughout, and the picture moved to where it says" : "One tempo throughout"}">STEADY</button></td><td>${free ? "" : `<button class="mini" data-map="${i}" title="Around this tempo, nudge it between hits (at most 8%) so every hard hit lands exactly on a beat">EXACT</button>`}</td></tr>`).join("")
          + `</table><p class="hint" style="margin-top:8px">Misses in milliseconds (+ = the hit comes after the beat). Under about 40 ms a hit reads as on the beat.</p>`;
        out.querySelectorAll("[data-steady]").forEach((b) => (b.onclick = () => use({ tempo: r.candidates[+b.dataset.steady].tempo, grid, free_start: free })));
        out.querySelectorAll("[data-map]").forEach((b) => (b.onclick = () => use({ mode: "map", tempo: r.candidates[+b.dataset.map].tempo })));
      } catch (e) { out.innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
    };
    $("#tfFind").onclick = find; if ($("#tfFree")) $("#tfFree").onchange = find; find();
  }
  $("#addPic").onclick = openSetup; $("#picSet").onclick = openSetup; $("#picFit").onclick = openFit;

  return { draw, overlay, sync, openSetup, openFit, guideSync };
})();
window.Picture = Picture;
if (S.doc) drawArrangement();
