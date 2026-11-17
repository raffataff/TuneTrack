// Tracking on the Desk: arming tracks, the input each audio track records, monitoring, REC with a count-in, the
// click, punch in/out over the loop region, quantise on record, the audio device, on-screen keys, waveforms, and
// comping between passes.
// A pass is one time through while recording (each round of the loop, each new recording over old material).
// Every clip carries its pass number (`lane`); the passes of a track share a stretch of time and one is heard,
// the others are muted clips. A track with passes shows each on a row of its own inside its lane.
// Recording runs on the LIVE engine (the server's /api/live state carries armed, recording, click, ins, inputs).
// Loaded after app.js and shares its globals (S, $, api, act, menu, toast, liveMode, ...).
"use strict";

window.Tracking = (() => {
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { /* no storage */ } return null; };
  const st = () => (S.eng.st && S.eng.st.on ? S.eng.st : null);
  const armed = (id) => (st() && st().armed && st().armed[id]) || { arm: false, monitor: false };
  const recording = () => !!(st() && st().recording);
  const take = () => S.take || null;
  let punch = null, armedSig = "", starting = false, lastDone = null, keysOn = false, peaksGen = 0;

  const NEED_LIVE = "Recording runs on the live engine: switch LIVE on first (it plays through your audio interface).";
  const NO_PUNCH = "Punch records the loop region: set one first (shift-drag on the ruler).";
  function needLive() { if (liveMode()) return true; toast(NEED_LIVE); return false; }
  const inLabel = (t) => t.input && t.input.length ? "IN " + t.input.join("+") : "NO INPUT";

  // ------------------------------------------------------------ track headers
  function headerButtons(t) {
    if (t.kind === "bus") return "";
    const a = armed(t.id);
    const tip = t.kind === "audio" ? "Arm: record this track's input"
      : `Arm: ${a.keyboard ? `the keyboard ${esc(a.keyboard)}` : "any MIDI keyboard"} plays and records this track. Right-click: choose the keyboard`;
    return `<button class="r ${a.arm ? "on" : ""}" title="${tip}">R</button>`
      + (t.kind === "audio" ? `<button class="mon ${a.monitor ? "on" : ""}" title="Monitor: hear the input through this track's effects (leave off when the interface's own direct monitoring is on)">I</button>` : "");
  }
  function headerInst(t) {
    return t.kind === "audio" ? `<button class="inp mini ${t.input ? "" : "none"}" title="The interface input this track records">${inLabel(t)} ▾</button>` : null;
  }
  async function setArm(t, what) {
    if (!needLive()) return;
    const a = armed(t.id);
    if (t.kind === "audio" && !t.input && !a[what]) { toast(`Choose the input "${t.name}" records first (${inLabel(t)} on its header).`); return; }
    status(st() ? "" : "starting the live engine…", "busy");
    try { liveState(await api("live_arm", { track: t.id, [what]: !a[what], take: take() })); status(""); }
    catch (e) { status(""); toast(e.message); }
    drawHeaders();
  }
  async function pickInput(e, t) {
    let names = (st() && st().inputs && st().inputs.audio) || [];
    if (!names.length && liveMode() && st()) {
      try { liveState(await api("live_inputs", { take: take() })); names = (st() && st().inputs.audio) || []; } catch (err) { toast(err.message); }
    }
    const n = Math.max(names.length, 2), items = [];
    for (let i = 1; i <= n; i++) items.push([`${i} · ${names[i - 1] || "input " + i}`, () => act("track_update", { track: t.id, input: [i] })]);
    for (let i = 1; i < n; i += 2) items.push([`${i}+${i + 1} · stereo pair`, () => act("track_update", { track: t.id, input: [i, i + 1] })]);
    items.push(["No input", () => act("track_update", { track: t.id, input: 0 })]);
    menu(e, items);
  }
  // Which MIDI keyboard plays a MIDI track (right-click on its R): any of them, or one by name.
  async function pickKeyboard(e, t) {
    if (!needLive()) return;
    const midi = () => (st() && st().inputs && st().inputs.midi) || [];
    if (!midi().length) {
      status(st() ? "" : "starting the live engine…", "busy");
      try { liveState(await api("live_inputs", { take: take() })); status(""); } catch (err) { status(""); toast(err.message); return; }
    }
    const cur = armed(t.id).keyboard || "", names = midi();
    const use = (k) => async () => {
      try { liveState(await api("live_arm", { track: t.id, keyboard: k, take: take() })); } catch (err) { toast(err.message); }
      drawHeaders();
    };
    menu(e, [[`Keyboard for ${t.name}`, null], [(cur ? "" : "• ") + "Any keyboard", use("")],
      ...names.map((n) => [(n === cur ? "• " : "") + n, use(n)]), ...(names.length ? [] : [["No MIDI keyboard found", null]])]);
  }
  function bindHeader(el, t) {
    const r = el.querySelector(".r"), mon = el.querySelector(".mon"), inp = el.querySelector(".inp");
    if (r) r.onclick = () => setArm(t, "arm");
    if (r && t.kind === "midi") r.oncontextmenu = (e) => { e.preventDefault(); e.stopPropagation(); pickKeyboard(e, t); };
    if (mon) mon.onclick = () => setArm(t, "monitor");
    if (inp) inp.onclick = (e) => pickInput(e, t);
    if (t.kind === "audio") { const m = document.createElement("div"); m.className = "inlvl"; m.dataset.t = t.id; el.appendChild(m); }
  }
  // The level coming in on each armed or monitored audio track, from the engine's per-input peaks.
  function meters() {
    const s = st(), ins = (s && s.ins) || [];
    document.querySelectorAll("#trackHeaders .inlvl").forEach((m) => {
      const t = S.doc && S.doc.tracks.find((x) => x.id === m.dataset.t), a = t && armed(t.id);
      const pk = t && t.input && (a.arm || a.monitor) ? Math.max(...t.input.map((c) => ins[c - 1] || 0)) : 0;
      m.style.width = peakPct(pk) + "%"; m.classList.toggle("hot", pk > 0.89);
    });
  }

  // ------------------------------------------------------------ transport
  const recBtn = $("#recBtn"), clickBtn = $("#clickBtn"), countIn = $("#countIn"), punchBtn = $("#punchBtn"), recQ = $("#recQ"), keysBtn = $("#keysBtn");
  countIn.value = store("tt.countin") || "1";
  countIn.onchange = () => store("tt.countin", countIn.value);
  // PUNCH: ● records only the loop region, once, and stops by itself
  let punchOn = store("tt.punch") === "1";
  punchBtn.onclick = () => { punchOn = !punchOn; store("tt.punch", punchOn ? "1" : "0"); sync(); if (punchOn && S.doc && !S.doc.loop) toast(NO_PUNCH, true); };
  // quantise on record: the grid the recorded notes' starts are moved to, in beats (0 = as played)
  recQ.value = store("tt.recq") || "0";
  if (!recQ.value) recQ.value = "0";
  recQ.onchange = () => store("tt.recq", recQ.value);
  const quant = () => ({ t8: 1 / 3, t16: 1 / 6 })[recQ.value] ?? (+recQ.value || 0);
  clickBtn.onclick = async () => {
    if (!needLive()) return;
    try { liveState(await api("live_click", { on: !(S.eng.st && S.eng.st.click) })); } catch (e) { toast(e.message); }
  };
  recBtn.onclick = () => (recording() ? stop(false) : record());
  async function record() {
    if (!S.doc || starting || !needLive()) return;
    const any = st() && Object.values(st().armed || {}).some((a) => a.arm);
    if (!any) { toast("No track is armed. Press R on the track to record on (an audio track needs its input chosen first)."); return; }
    const lp = S.doc.loop;
    if (punchOn && !lp) { toast(NO_PUNCH); return; }
    const where = punchOn ? { beat: lp.start, until: lp.end, loop: false } : { beat: S.playhead, loop: S.loopOn && !!lp };
    starting = true; punch = where.beat; window.Studio?.hush();
    try {
      liveState(await api("live_record", { ...where, count_in: +countIn.value, quantise: quant(), take: take() }));
    } catch (e) { punch = null; toast(e.message); }
    starting = false;
  }
  // The answer to a stop that ended a recording (or the state's `done` when a punch stopped by itself).
  function stopped(s) {
    if (!s || (!s.recorded && !s.record_error)) return;
    punch = null; peaksGen++;  // new files: look again
    if (s.record_error) toast(s.record_error);
    else {
      const n = s.recorded.clips.length;
      toast(n > 1 ? `Recorded ${n} passes. The last full one is heard; Ctrl+drag along another pass to use it there.` : "Recorded.", true);
    }
    loadState().catch(() => {});
  }
  function sync() {
    const s = S.eng.st || {}, live = liveMode();
    for (const el of [recBtn, clickBtn, countIn, punchBtn, recQ, keysBtn]) el.classList.toggle("hidden", !s.available);
    recBtn.classList.toggle("on", recording()); recBtn.disabled = clickBtn.disabled = punchBtn.disabled = keysBtn.disabled = !live;
    clickBtn.classList.toggle("on", !!s.click && live);
    punchBtn.classList.toggle("on", punchOn);
    if (keysOn && !live) setKeys(false);
    if (!recording() && !starting) punch = null;
    // a punch that stopped by itself: `done.n` goes up once each time (the first n seen is where we start from)
    if (s.on && s.done && typeof s.done.n === "number") {
      const n = s.done.n, rose = lastDone != null && n > lastDone;
      lastDone = n;
      if (rose) stopped(s.done);
    }
    const sig = JSON.stringify(s.armed || {}) + (s.on ? 1 : 0);
    if (sig !== armedSig) { armedSig = sig; if (document.body.dataset.room === "desk") drawHeaders(); }
    meters();
  }

  // ------------------------------------------------------------ the audio device
  // The LIVE tag (while the engine is on) opens this: what to play through, and what to record from.
  async function pickDevice(e) {
    if (!liveMode() || !st()) return;
    if (recording()) { toast("Recording: stop it before changing the audio device."); return; }
    let d; try { d = await api("live_devices", { take: take() }); } catch (err) { toast(err.message); return; }
    const cur = d.current || {}, chosenIn = (d.chosen && d.chosen.input) || "", nm = (x) => `${x.type}: ${x.name}`;
    const set = (args) => async () => {
      if (recording()) { toast("Recording: stop it before changing the audio device."); return; }
      status("switching the audio device…", "busy");
      try { liveState(await api("live_device", { ...args, take: take() })); status(""); } catch (err) { status(""); toast(err.message); }
    };
    const ins = (d.inputs || []).filter((x) => x.type === cur.type);
    menu(e, [
      ["Play through", null], ["Pick for me", set({ name: "" })],
      ...(d.devices || []).map((x) => [(x.type === cur.type && x.name === cur.name ? "• " : "") + nm(x), set({ name: nm(x) })]),
      ["Record from", null], [(chosenIn ? "" : "• ") + "The device's own inputs", set({ input: "" })],
      ...ins.map((x) => [(x.name === chosenIn ? "• " : "") + x.name, set({ input: x.name })]),
    ]);
  }
  $("#audioState").addEventListener("click", (e) => { if (liveMode() && st()) pickDevice(e); });

  // ------------------------------------------------------------ on-screen keys
  // Two octaves to play the armed MIDI tracks with the mouse (lower on a key = louder) or the computer keyboard.
  const keysBox = $("#keys"), kbd = $("#kbd"), KMAP = "awsedftgyhujk", BLACK = [1, 3, 6, 8, 10];
  let base = +(store("tt.keys.base") || 60), warned = false;
  const held = new Set(), byKey = new Map(), byPtr = new Map();
  let outQ = [], sending = false;
  // in order, one request at a time: what piles up while one is in flight goes in the next
  async function sendKeys(m) {
    outQ.push(m); if (sending) return;
    sending = true;
    while (outQ.length) { const msgs = outQ; outQ = []; try { await api("live_keys", { msgs }); } catch (e) { toast(e.message); } }
    sending = false;
  }
  function canPlay() {
    if (!liveMode()) { if (!warned) toast(NEED_LIVE); warned = true; return false; }
    const any = S.doc && S.doc.tracks.some((t) => t.kind === "midi" && armed(t.id).arm);
    if (!any) { if (!warned) toast("The keys play the armed MIDI tracks: press R on a MIDI track first."); warned = true; return false; }
    return true;
  }
  const keyEl = (n) => kbd.querySelector(`[data-n="${n - base}"]`);
  function noteOn(n, vel) {
    if (held.has(n) || n < 0 || n > 127 || !canPlay()) return false;
    held.add(n); keyEl(n)?.classList.add("dn"); sendKeys([0x90, n, vel]); return true;
  }
  function noteOff(n) {
    if (!held.delete(n)) return;
    keyEl(n)?.classList.remove("dn"); sendKeys([0x80, n, 0]);
  }
  function releaseAll() { for (const n of [...held]) noteOff(n); byKey.clear(); byPtr.clear(); }
  function drawKeys() {
    let html = "", wi = 0;
    for (let i = 0; i <= 24; i++) {
      if (BLACK.includes((base + i) % 12)) html += `<div class="b" data-n="${i}" style="left:${wi * 24 - 8}px"></div>`;
      else { html += `<div class="w" data-n="${i}">${(base + i) % 12 === 0 ? `<span>${noteName(base + i)}</span>` : ""}</div>`; wi++; }
    }
    kbd.innerHTML = html;
    $("#kRange").textContent = `${noteName(base)}–${noteName(base + 24)}`;
  }
  function octave(by) {
    const b = Math.max(12, Math.min(96, base + 12 * by)); if (b === base) return;
    releaseAll(); base = b; store("tt.keys.base", String(b)); drawKeys();
  }
  function setKeys(on) {
    if (on && !needLive()) return;
    keysOn = on; warned = false;
    if (!on) releaseAll(); else drawKeys();
    keysBtn.classList.toggle("on", on); keysBox.classList.toggle("hidden", !on);
    requestAnimationFrame(() => { if (document.body.dataset.room === "desk") { drawArrangement(); drawRoll(); } });  // the desk got shorter or taller
  }
  keysBtn.onclick = () => setKeys(!keysOn);
  $("#kDn").onclick = () => octave(-1); $("#kUp").onclick = () => octave(1);
  kbd.addEventListener("pointerdown", (e) => {
    const k = e.target.closest("[data-n]"); if (!k || e.button) return;
    e.preventDefault(); try { kbd.setPointerCapture(e.pointerId); } catch { /* gone already */ }
    const r = k.getBoundingClientRect(), n = base + +k.dataset.n;
    const vel = Math.max(1, Math.min(127, Math.round(20 + (e.clientY - r.top) / r.height * 107)));
    if (noteOn(n, vel)) byPtr.set(e.pointerId, n);
  });
  const ptrUp = (e) => { const n = byPtr.get(e.pointerId); if (n != null) { byPtr.delete(e.pointerId); noteOff(n); } };
  kbd.addEventListener("pointerup", ptrUp); kbd.addEventListener("pointercancel", ptrUp);
  const typing = () => { const a = document.activeElement; return !!a && (["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName) || a.isContentEditable); };
  // capture on window: ahead of the desk's own keys (H marks a hit) while the keys are on
  window.addEventListener("keydown", (e) => {
    if (!keysOn || e.ctrlKey || e.metaKey || e.altKey || typing() || document.body.dataset.room !== "desk") return;
    const k = e.key.length === 1 ? e.key.toLowerCase() : "", i = k ? KMAP.indexOf(k) : -1;
    if (k === "z" || k === "x") { e.preventDefault(); e.stopImmediatePropagation(); if (!e.repeat) octave(k === "z" ? -1 : 1); return; }
    if (i < 0) return;
    e.preventDefault(); e.stopImmediatePropagation();
    if (e.repeat || byKey.has(k)) return;
    if (noteOn(base + i, 100)) byKey.set(k, base + i);
  }, true);
  window.addEventListener("keyup", (e) => {
    const k = e.key.length === 1 ? e.key.toLowerCase() : "";
    if (!byKey.has(k)) return;
    const n = byKey.get(k); byKey.delete(k); noteOff(n);
  }, true);
  window.addEventListener("blur", releaseAll);

  // ------------------------------------------------------------ waveforms
  // Each audio file's peaks (/api/peaks: one byte a bin, the loudest sample there, X-Peaks-Per-Sec bins a second),
  // fetched once and again after a recording ends (that is when new files appear). Until they arrive, or when the
  // file has none, app.js draws the clip's stand-in.
  const peaks = new Map();
  function peaksOf(file) {
    const key = (S.path || "") + "|" + (S.take || "") + "|" + file;
    let p = peaks.get(key);
    if (!p || (p.gen !== peaksGen && !p.busy)) {
      p = p || { data: null, rate: 0 }; p.gen = peaksGen; p.busy = true; peaks.set(key, p);
      fetch("/api/peaks?file=" + encodeURIComponent(file) + (S.take ? "&take=" + encodeURIComponent(S.take) : ""))
        .then(async (r) => { if (r.ok) { p.rate = +r.headers.get("X-Peaks-Per-Sec") || 200; p.data = new Uint8Array(await r.arrayBuffer()); } else p.data = null; })
        .catch(() => {})
        .finally(() => { p.busy = false; if (document.body.dataset.room === "desk") drawArrangement(); });
    }
    return p.data && p.data.length ? p : null;
  }
  // Min/max about the clip's middle, one column a pixel, only what is on screen. A clip plays its file from
  // `offset` seconds: at its own tempo when stretched (`bpm`), otherwise following the tempo map.
  function wave(g, c, x, w, top, h, col) {
    const p = c.file && peaksOf(c.file); if (!p) return false;
    const sx = lanesWrap.scrollLeft, x0 = Math.max(Math.floor(x), Math.floor(sx)), x1 = Math.min(Math.ceil(x + w), Math.ceil(sx + lanesWrap.clientWidth));
    const pk = p.data, n = pk.length, rate = p.rate, off = c.offset || 0, mid = top + h / 2, s0 = beatToSec(c.start);
    const flat = !(S.doc.tempo_changes || []).length, spb = 60 / (c.bpm || S.doc.tempo);
    const fileSec = (b) => off + (c.bpm || flat ? (b - c.start) * spb : beatToSec(b) - s0);
    g.fillStyle = col + "cc";
    let a = fileSec(c.start + (x0 - x) / S.zoom);
    for (let px = x0; px < x1; px++) {
      const b = fileSec(c.start + (px + 1 - x) / S.zoom), i0 = Math.max(0, Math.floor(a * rate)), i1 = Math.min(n, Math.max(i0 + 1, Math.ceil(b * rate)));
      let m = 0; for (let i = i0; i < i1; i++) if (pk[i] > m) m = pk[i];
      if (m) { const ah = Math.max(1, m / 255 * h); g.fillRect(px, mid - ah / 2, 1, ah); }
      a = b;
    }
    return true;
  }

  // ------------------------------------------------------------ the arrangement
  // What is being recorded right now, on the armed tracks (only between the punch points when there are some);
  // and the stretch a comp drag covers. lay: each track's lane ({y, h}, from laneLayout).
  function overlay(g, lay) {
    if (!S.doc) return;
    const s = st(), pp = s && s.punch, from = pp ? pp[0] : punch, to = pp && pp[1] != null ? pp[1] : Infinity;
    if (recording() && from != null && S.playhead > from) {
      const end = Math.min(S.playhead, to);
      S.doc.tracks.forEach((t, i) => {
        if (!armed(t.id).arm || !lay[i]) return;
        const { y, h } = lay[i];
        g.fillStyle = "rgba(255,74,58,0.22)"; g.fillRect(from * S.zoom, y + 3, (end - from) * S.zoom, h - 8);
        g.fillStyle = "#ff4a3a"; g.fillRect(from * S.zoom, y + 3, 2, h - 8);
        if (to < Infinity) g.fillRect(to * S.zoom - 2, y + 3, 2, h - 8);
      });
    }
    const d = S.drag;
    if (d && d.kind === "comp") {
      const a = Math.min(d.b0, d.b1), b = Math.max(d.b0, d.b1);
      // Shift: every track recorded with it follows, so light the same pass wherever it has a row in the stretch
      S.doc.tracks.forEach((t, i) => {
        if (!lay[i]) return;
        if (t.id !== d.t.id) {
          const rows = d.group && passRows(t);
          if (!rows || !rows.includes(d.lane) || !t.clips.some((c) => (c.lane || 0) === d.lane && c.start < b && c.start + c.length > a)) return;
        }
        const [cy, ch] = clipBox(t, d.lane, lay[i].y, lay[i].h);
        g.fillStyle = "rgba(92,200,255,0.25)"; g.fillRect(a * S.zoom, cy, (b - a) * S.zoom, ch);
        g.strokeStyle = "#5cc8ff"; g.strokeRect(a * S.zoom + 0.5, cy + 0.5, Math.max(1, (b - a) * S.zoom - 1), ch - 1);
      });
    }
  }
  function compStart(e, h) {
    S.drag = { kind: "comp", t: h.t, row: h.row, lane: h.clip.lane || 0, b0: snapB(h.beat), b1: snapB(h.beat), group: e.shiftKey };
    drawArrangement();
  }
  function compDrag(e) { S.drag.b1 = snapB(laneHit(e).beat); S.drag.group = e.shiftKey; drawArrangement(); }
  async function compDrop(d) {
    const a = Math.min(d.b0, d.b1), b = Math.max(d.b0, d.b1);
    if (b - a < 1e-6) { drawArrangement(); return; }
    try { await act("comp", { track: d.t.id, start: a, end: b, use: d.lane, ...(d.group ? { group: true } : {}) }, "comping…"); } catch { /* shown by act */ }
  }
  function clipMenu(t, c) {
    if (!passRows(t)) return [];
    const n = c.lane || 0, args = { track: t.id, start: c.start, end: c.start + c.length, use: n };
    return [[`Hear pass ${n} here`, () => act("comp", args)],
      ...(c.kind === "audio" && /^audio\/rec\//.test(c.file || "") ? [[`Hear pass ${n} here on all tracks recorded with it`, () => act("comp", { ...args, group: true })]] : [])];
  }

  return { headerButtons, headerInst, bindHeader, meters, sync, stopped, overlay, compStart, compDrag, compDrop, clipMenu, recording, record, wave };
})();
