// TuneTrack desk (arrangement, piano roll, mixer) and the shared header. Every edit goes through /api/call (the
// same API the agents use); the project file is the source of truth. studio.js draws the crew rack and follows
// /api/events, so agent edits show up live; the 1 s poll below is only the fallback when that stream is down.
"use strict";

const $ = (s) => document.querySelector(s);
const PALETTE = ["#5e9cec", "#ec785e", "#78c878", "#d296e6", "#f0c850", "#5ac8c8", "#e66ea0", "#aaaa5a", "#8c8cf0", "#f09650"];
const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const S = {
  doc: null, version: 0, path: null, lastRender: null, library: null, catalog: null, sfz: null,
  sel: { track: null, clip: null, notes: new Set() },
  zoom: 22, rollZoom: 64, rowH: 12, snap: 1, playhead: 0, loopOn: false, lastLen: 1,
  pendingSave: null, busy: false, tab: "roll",
  audio: { ctx: null, buf: null, src: null, start: 0, offset: 0, playing: false, version: -1 },
};

// ---------------------------------------------------------------- api
const TOKEN = document.querySelector('meta[name="tt-token"]')?.content || "";
async function api(fn, args = {}) {
  const r = await fetch("/api/call", { method: "POST", headers: { "Content-Type": "application/json", "X-TuneTrack-Token": TOKEN }, body: JSON.stringify({ fn, args }) });
  const j = await r.json();
  if (j.error) { const e = new Error(j.error); e.conflict = !!j.conflict; throw e; }
  return j.result;
}
async function act(fn, args, msg) {
  try { status(msg || "", "busy"); const r = await api(fn, args); status(""); await loadState(); return r; }
  catch (e) { toast(e.message); status("error", "err"); throw e; }
}
function status(text, cls = "") { const el = $("#status"); el.textContent = text; el.className = "status " + cls; }
let toastTimer;
function toast(text, info = false) {
  const el = $("#toast"); el.textContent = text; el.className = info ? "info" : "";
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.className = "hidden", info ? 3000 : 7000);
}
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------------------------------------------------------------- time
const bpb = () => S.doc ? S.doc.time_sig[0] * 4 / S.doc.time_sig[1] : 4;
function tempoPts() {
  const pts = [[0, S.doc.tempo], ...(S.doc.tempo_changes || [])].sort((a, b) => a[0] - b[0]);
  const out = []; let sec = 0;
  pts.forEach((p, i) => { if (i) sec += (p[0] - pts[i - 1][0]) * 60 / pts[i - 1][1]; out.push({ b: p[0], bpm: p[1], s: sec }); });
  return out;
}
function beatToSec(b) { const pts = tempoPts(); let p = pts[0]; for (const q of pts) if (q.b <= b) p = q; return p.s + (b - p.b) * 60 / p.bpm; }
function secToBeat(s) { const pts = tempoPts(); let p = pts[0]; for (const q of pts) if (q.s <= s) p = q; return p.b + (s - p.s) * p.bpm / 60; }
function fmtPos(b) { const bar = Math.floor(b / bpb()) + 1; return `${bar}:${(b - (bar - 1) * bpb() + 1).toFixed(2)}`; }
const snapB = (b, snap = S.snap) => snap > 0 ? Math.round(b / snap) * snap : b;
const floorB = (b, snap = S.snap) => snap > 0 ? Math.floor(b / snap + 1e-9) * snap : b;
const noteName = (p) => NOTE_NAMES[p % 12] + (Math.floor(p / 12) - 1);
const trackColor = (t, i) => t.color || PALETTE[i % PALETTE.length];
const songEnd = () => {
  let e = 0; for (const t of S.doc.tracks) for (const c of t.clips) e = Math.max(e, c.start + c.length);
  if (S.doc.loop) e = Math.max(e, S.doc.loop.end); if (S.doc.length) e = Math.max(e, S.doc.length); return e;
};
const findClip = (id) => { for (const t of S.doc.tracks) for (const c of t.clips) if (c.id === id) return [t, c]; return [null, null]; };

// ---------------------------------------------------------------- load / poll
async function loadState() {
  const st = await (await fetch("/api/state")).json();
  if (!st.project) { S.doc = null; S.path = null; renderAll(); window.Studio?.set(null, null); openProjectModal(st.recent || []); return; }
  S.doc = st.doc; S.version = st.version; S.path = st.project;
  window.Studio?.set(st.board, st.crew);
  // notes as last loaded, per clip: whole-list saves send these so the server can refuse a stale overwrite
  S.base = {}; for (const t of S.doc.tracks) for (const c of t.clips) if (c.notes) S.base[c.id] = c.notes.map((n) => [...n]);
  if (st.last_render && !S.lastRender) S.lastRender = st.last_render;
  if (S.sel.clip && !findClip(S.sel.clip)[1]) { S.sel.clip = null; S.sel.notes.clear(); }
  if (S.sel.track && !S.doc.tracks.find((t) => t.id === S.sel.track)) S.sel.track = null;
  renderAll();
}
setInterval(async () => {
  if (S.pendingSave || S.drag || S.live) return;
  try {
    const v = await (await fetch("/api/version")).json();
    if (v.version !== S.version || v.project !== S.path) await loadState();
  } catch { /* server restarting */ }
}, 1000);

function renderAll() {
  const d = S.doc;
  $("#projName").textContent = d ? d.name : "No project";
  if (d) {
    if (document.activeElement !== $("#tempo")) $("#tempo").value = d.tempo;
    if (document.activeElement !== $("#key")) $("#key").value = d.key;
    if (document.activeElement !== $("#sig")) $("#sig").value = d.time_sig.join("/");
  }
  $("#loopBtn").classList.toggle("on", S.loopOn);
  audioTag();
  if (document.body.dataset.room !== "desk") return;  // canvases have no size while the desk is hidden
  drawHeaders(); drawArrangement(); drawRoll(); renderPanel();
}
// Playback is a render of the project, not a live engine: say whether what you hear is the current revision.
function audioTag() {
  const el = $("#audioState"), a = S.audio;
  if (!S.doc || !a.buf) { el.textContent = "NO RENDER"; el.className = "tag"; }
  else if (a.version === S.version) { el.textContent = `RENDER = REV ${S.doc.rev ?? "?"}`; el.className = "tag ok"; }
  else { el.textContent = `RENDER REV ${a.rev ?? "?"} · NOW ${S.doc.rev ?? "?"}`; el.className = "tag stale"; }
}

// ---------------------------------------------------------------- top bar
$("#tempo").addEventListener("change", (e) => act("project_set", { tempo: +e.target.value }));
$("#key").addEventListener("change", (e) => act("project_set", { key: e.target.value }));
$("#sig").addEventListener("change", (e) => act("project_set", { time_sig: e.target.value }));
$("#snap").addEventListener("change", (e) => { S.snap = +e.target.value; });
$("#undoBtn").onclick = () => act("undo");
$("#redoBtn").onclick = () => act("redo");
$("#loopBtn").onclick = () => { S.loopOn = !S.loopOn; renderAll(); };
$("#playBtn").onclick = () => play();
$("#stopBtn").onclick = () => stop(true);
$("#exportBtn").onclick = () => openExportModal();
$("#projBtn").onclick = async () => openProjectModal((await api("project_list")).recent);
$("#addMidi").onclick = () => openAddTrack("midi");
$("#addAudio").onclick = () => openAddTrack("audio");
$("#addBus").onclick = () => openAddTrack("bus");
document.querySelectorAll("#tabs button[data-tab]").forEach((b) => b.onclick = () => {
  S.tab = b.dataset.tab;
  document.querySelectorAll("#tabs button[data-tab]").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + S.tab));
  renderPanel(); drawRoll();
});

// ---------------------------------------------------------------- track headers
function drawHeaders() {
  const box = $("#trackHeaders"); box.innerHTML = "";
  if (!S.doc) return;
  S.doc.tracks.forEach((t, i) => {
    const el = document.createElement("div");
    el.className = "th" + (S.sel.track === t.id ? " sel" : "");
    const inst = t.kind === "midi" ? (t.instrument ? instLabel(t.instrument) : "no instrument") : t.kind;
    el.innerHTML = `<div class="bar" style="background:${trackColor(t, i)}"></div>
      <div class="name" title="${esc(t.name)}">${esc(t.name)}</div>
      <div class="btns"><button class="m ${t.mute ? "on" : ""}" title="Mute">M</button><button class="s ${t.solo ? "on" : ""}" title="Solo">S</button></div>
      <div class="inst" title="${esc(inst)}">${esc(inst)}${t.role ? " · " + esc(t.role) : ""}</div>
      <div class="vol"><input type="range" min="-60" max="12" step="0.5" value="${t.volume_db}"><span>${(+t.volume_db).toFixed(1)}</span></div>`;
    el.onclick = (e) => { if (e.target.tagName === "INPUT" || e.target.tagName === "BUTTON") return; selectTrack(t.id); };
    el.ondblclick = (e) => { if (e.target.classList.contains("name")) renameTrack(t); };
    el.oncontextmenu = (e) => { e.preventDefault(); trackMenu(e, t); };
    el.querySelector(".m").onclick = () => act("track_update", { track: t.id, mute: !t.mute });
    el.querySelector(".s").onclick = () => act("track_update", { track: t.id, solo: !t.solo });
    const vol = el.querySelector("input");
    vol.oninput = () => { el.querySelector(".vol span").textContent = (+vol.value).toFixed(1); };
    vol.onchange = () => act("track_update", { track: t.id, volume_db: +vol.value });
    vol.ondblclick = () => act("track_update", { track: t.id, volume_db: 0 });
    box.appendChild(el);
  });
}
function instLabel(i) {
  if (i.patch) return `${i.name} · ${i.patch}`;
  if (i.type === "sfz") return "sfz · " + i.name.split(/[\\/]/).pop();
  return `${i.type} · ${i.name}`;
}
function selectTrack(id) { S.sel.track = id; drawHeaders(); drawArrangement(); renderPanel(); }
async function renameTrack(t) { const n = prompt("Track name", t.name); if (n && n !== t.name) act("track_update", { track: t.id, name: n }); }
function trackMenu(e, t) {
  menu(e, [
    ["Rename…", () => renameTrack(t)],
    ["Duplicate", () => act("track_duplicate", { track: t.id })],
    ["Move up", () => act("track_move", { track: t.id, index: Math.max(0, S.doc.tracks.indexOf(t) - 1) })],
    ["Move down", () => act("track_move", { track: t.id, index: S.doc.tracks.indexOf(t) + 1 })],
    ["Delete track", () => confirm(`Delete track "${t.name}"?`) && act("track_remove", { track: t.id })],
  ]);
}

// ---------------------------------------------------------------- arrangement
const lanesWrap = $("#lanesWrap"), lanes = $("#lanes"), ruler = $("#ruler");
const laneH = () => parseInt(getComputedStyle(document.documentElement).getPropertyValue("--lane"));
function fitCanvas(cv, w, h) {
  const r = devicePixelRatio || 1;
  if (cv.width !== Math.round(w * r) || cv.height !== Math.round(h * r)) { cv.width = Math.round(w * r); cv.height = Math.round(h * r); }
  cv.style.width = w + "px"; cv.style.height = h + "px";
  const g = cv.getContext("2d"); g.setTransform(r, 0, 0, r, 0, 0); return g;
}
function drawArrangement() {
  const g0 = ruler.getContext("2d");
  if (!S.doc) { g0.clearRect(0, 0, ruler.width, ruler.height); fitCanvas(lanes, 10, 10); return; }
  const L = laneH(), n = S.doc.tracks.length, endB = songEnd() + bpb() * 8;
  const W = Math.max(lanesWrap.clientWidth, endB * S.zoom), H = Math.max(lanesWrap.clientHeight - 2, n * L);
  const g = fitCanvas(lanes, W, H);
  g.fillStyle = "#101011"; g.fillRect(0, 0, W, H);
  if (S.doc.loop) { g.fillStyle = "rgba(95,224,122,0.06)"; g.fillRect(S.doc.loop.start * S.zoom, 0, (S.doc.loop.end - S.doc.loop.start) * S.zoom, H); }
  for (let b = 0, bar = 0; b <= endB; b += bpb(), bar++) {
    g.fillStyle = bar % 4 ? "#1b1b1d" : "#27272a"; g.fillRect(Math.round(b * S.zoom), 0, 1, H);
  }
  S.doc.tracks.forEach((t, i) => {
    const y = i * L;
    if (S.sel.track === t.id) { g.fillStyle = "rgba(255,176,0,0.05)"; g.fillRect(0, y, W, L); }
    g.fillStyle = "#202022"; g.fillRect(0, y + L - 1, W, 1);
    const col = trackColor(t, i);
    for (const c of t.clips) drawClip(g, t, c, y, L, col);
  });
  if (S.drag && S.drag.kind === "clip" && S.drag.ghost) {
    const d = S.drag.ghost; g.strokeStyle = "#fff"; g.setLineDash([4, 3]);
    g.strokeRect(d.start * S.zoom + 0.5, d.row * L + 3.5, d.length * S.zoom - 1, L - 8); g.setLineDash([]);
  }
  for (const m of S.doc.markers) { g.fillStyle = "rgba(255,176,0,.45)"; g.fillRect(m.at * S.zoom, 0, 1, H); }
  g.fillStyle = "#ff4a3a"; g.fillRect(S.playhead * S.zoom, 0, 1.5, H);
  drawRuler();
}
function drawClip(g, t, c, y, L, col) {
  const x = c.start * S.zoom, w = Math.max(3, c.length * S.zoom), sel = S.sel.clip === c.id;
  g.globalAlpha = c.muted ? 0.35 : 1;
  g.fillStyle = col + "33"; g.fillRect(x + 1, y + 3, w - 2, L - 8);
  g.fillStyle = col; g.fillRect(x + 1, y + 3, w - 2, 14);
  g.strokeStyle = sel ? "#ffffff" : col; g.lineWidth = sel ? 2 : 1; g.strokeRect(x + 1.5, y + 3.5, w - 3, L - 9); g.lineWidth = 1;
  g.fillStyle = "#0c0c0d"; g.font = "11px Segoe UI, system-ui, sans-serif";
  g.save(); g.beginPath(); g.rect(x + 2, y + 3, w - 4, L - 8); g.clip();
  g.fillText(c.name || (c.kind === "audio" ? (c.file || "").split("/").pop() : ""), x + 5, y + 14);
  if (c.kind === "midi" && c.notes.length) {
    let lo = 127, hi = 0; for (const n of c.notes) { lo = Math.min(lo, n[0]); hi = Math.max(hi, n[0]); }
    const span = Math.max(12, hi - lo + 2), top = y + 19, h = L - 26;
    g.fillStyle = col;
    for (const n of c.notes) {
      if (n[1] >= c.length) continue;
      const ny = top + (hi + 1 - n[0]) / span * h;
      g.fillRect(x + n[1] * S.zoom, ny, Math.max(1.5, Math.min(n[2], c.length - n[1]) * S.zoom - 0.5), Math.max(1.5, h / span - 0.5));
    }
  } else if (c.kind === "audio") {
    g.fillStyle = col + "99"; for (let i = 0; i < w; i += 3) { const a = (Math.sin(i * 0.37) * 0.5 + 0.5) * (L - 30); g.fillRect(x + i, y + 22 + (L - 30 - a) / 2, 2, a); }
  }
  g.restore(); g.globalAlpha = 1;
}
function drawRuler() {
  const W = lanesWrap.clientWidth, H = 30, g = fitCanvas(ruler, W, H), sx = lanesWrap.scrollLeft;
  g.fillStyle = "#171718"; g.fillRect(0, 0, W, H);
  if (!S.doc) return;
  if (S.doc.loop) {
    g.fillStyle = S.loopOn ? "rgba(95,224,122,.55)" : "rgba(95,224,122,.22)";
    g.fillRect(S.doc.loop.start * S.zoom - sx, 0, (S.doc.loop.end - S.doc.loop.start) * S.zoom, 6);
  }
  g.font = "11px Segoe UI, system-ui, sans-serif";
  const every = Math.max(1, Math.round(40 / (bpb() * S.zoom)));
  for (let bar = 0; bar * bpb() * S.zoom < sx + W; bar++) {
    const x = bar * bpb() * S.zoom - sx; if (x < -40) continue;
    g.fillStyle = "#3b3b40"; g.fillRect(x, H - 10, 1, 10);
    if (bar % every === 0) { g.fillStyle = "#8f897c"; g.fillText(bar + 1, x + 3, H - 12); }
  }
  for (const m of S.doc.markers) { const x = m.at * S.zoom - sx; g.fillStyle = "#ffb000"; g.fillRect(x, 6, 2, H - 6); g.fillText(m.name, x + 4, 16); }
  const px = S.playhead * S.zoom - sx; g.fillStyle = "#ff4a3a";
  g.beginPath(); g.moveTo(px - 5, H - 9); g.lineTo(px + 5, H - 9); g.lineTo(px, H); g.fill();
}
lanesWrap.addEventListener("scroll", () => { $("#trackHeaders").scrollTop = lanesWrap.scrollTop; drawRuler(); });
window.addEventListener("resize", () => { drawArrangement(); drawRoll(); });
lanesWrap.addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return; e.preventDefault();
  const mx = e.offsetX, beat = mx / S.zoom;
  S.zoom = Math.min(200, Math.max(3, S.zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
  drawArrangement(); lanesWrap.scrollLeft = beat * S.zoom - (mx - lanesWrap.scrollLeft);
}, { passive: false });

function laneHit(e) {
  const r = lanes.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top, L = laneH();
  const row = Math.floor(y / L), t = S.doc && S.doc.tracks[row], beat = x / S.zoom;
  let clip = null, edge = false;
  if (t) for (const c of t.clips) if (beat >= c.start && beat <= c.start + c.length) { clip = c; edge = (c.start + c.length) * S.zoom - x < 7; }
  return { row, t, beat, clip, edge, x, y };
}
lanes.addEventListener("mousedown", (e) => {
  if (!S.doc || e.button !== 0) return;
  const h = laneHit(e);
  if (!h.t) return;
  S.sel.track = h.t.id;
  if (!h.clip) { S.sel.clip = null; renderAll(); return; }
  if (S.sel.clip !== h.clip.id) { S.sel.clip = h.clip.id; S.sel.notes.clear(); focusRoll(); }
  S.drag = { kind: "clip", mode: h.edge ? "resize" : (e.altKey ? "copy" : "move"), clip: h.clip, t: h.t, x0: h.x, beat0: h.beat,
    ghost: { start: h.clip.start, length: h.clip.length, row: h.row }, moved: false };
  renderAll();
});
window.addEventListener("mousemove", (e) => {
  if (!S.drag) return;
  if (S.drag.kind === "clip") {
    const h = laneHit(e), d = S.drag, dx = (h.x - d.x0) / S.zoom;
    if (Math.abs(h.x - d.x0) > 3) d.moved = true;
    if (d.mode === "resize") d.ghost.length = Math.max(S.snap || 0.25, snapB(d.clip.length + dx));
    else { d.ghost.start = Math.max(0, snapB(d.clip.start + dx)); d.ghost.row = Math.max(0, Math.min(S.doc.tracks.length - 1, h.row)); }
    drawArrangement();
  } else if (S.drag.kind === "ruler") rulerDrag(e);
  else rollDrag(e);
});
window.addEventListener("mouseup", async (e) => {
  const d = S.drag; if (!d) return; S.drag = null;
  if (d.kind === "clip") {
    if (!d.moved) { drawArrangement(); return; }
    const dest = S.doc.tracks[d.ghost.row];
    if (d.mode === "resize") await act("clip_set", { clip: d.clip.id, length: d.ghost.length });
    else if (d.mode === "copy") await act("clip_edit", { clip: d.clip.id, op: "copy", to: d.ghost.start, track: dest.id });
    else await act("clip_set", { clip: d.clip.id, start: d.ghost.start, track: dest.id !== d.t.id ? dest.id : null });
  } else if (d.kind === "ruler") {
    if (d.loop && d.b1 !== d.b0) await act("project_set", { loop: { start: Math.min(d.b0, d.b1), end: Math.max(d.b0, d.b1) } });
  } else rollUp(e);
});
lanes.addEventListener("dblclick", async (e) => {
  const h = laneHit(e);
  if (!h.t) return;
  if (h.clip) { focusRoll(); return; }
  if (h.t.kind !== "midi") return;
  const r = await act("clip_create", { track: h.t.id, start: floorB(h.beat, bpb()), length: bpb() });
  S.sel.clip = r.clip; focusRoll(); renderAll();
});
lanes.addEventListener("contextmenu", (e) => {
  e.preventDefault(); const h = laneHit(e);
  if (!h.clip) return;
  S.sel.clip = h.clip.id; renderAll();
  const c = h.clip;
  menu(e, [
    ["Open in piano roll", () => focusRoll()],
    ["Rename…", () => { const n = prompt("Clip name", c.name); if (n !== null) act("clip_set", { clip: c.id, name: n }); }],
    ["Duplicate", () => act("clip_edit", { clip: c.id, op: "duplicate", times: 1 })],
    ["Split at playhead", () => act("clip_edit", { clip: c.id, op: "split", at: S.playhead })],
    [c.muted ? "Unmute" : "Mute", () => act("clip_set", { clip: c.id, muted: !c.muted })],
    ["Delete", () => act("clip_edit", { clip: c.id, op: "delete" })],
  ]);
});

ruler.addEventListener("mousedown", (e) => {
  if (!S.doc) return;
  const b = Math.max(0, (e.offsetX + lanesWrap.scrollLeft) / S.zoom);
  if (e.shiftKey || e.offsetY < 8) { S.drag = { kind: "ruler", loop: true, b0: snapB(b, S.snap || 1), b1: snapB(b, S.snap || 1) }; return; }
  setPlayhead(snapB(b)); S.drag = { kind: "ruler", loop: false };
});
function rulerDrag(e) {
  const r = ruler.getBoundingClientRect(), b = Math.max(0, (e.clientX - r.left + lanesWrap.scrollLeft) / S.zoom);
  if (S.drag.loop) { S.drag.b1 = snapB(b, S.snap || 1); S.doc.loop = { start: Math.min(S.drag.b0, S.drag.b1), end: Math.max(S.drag.b0, S.drag.b1) }; drawArrangement(); }
  else setPlayhead(snapB(b));
}
ruler.addEventListener("contextmenu", (e) => {
  e.preventDefault(); const b = snapB((e.offsetX + lanesWrap.scrollLeft) / S.zoom, bpb());
  menu(e, [
    ["Add marker here…", () => { const n = prompt("Marker name"); if (n) act("markers", { add: [{ at: b, name: n }] }); }],
    ["Set loop start here", () => act("project_set", { loop: { start: b, end: Math.max(b + bpb(), S.doc.loop ? S.doc.loop.end : b + 8 * bpb()) } })],
    ["Set loop end here", () => act("project_set", { loop: { start: S.doc.loop ? Math.min(S.doc.loop.start, b - bpb()) : 0, end: b } })],
    ["Clear loop", () => act("project_set", { loop: "off" })],
    ...S.doc.markers.map((m) => [`Remove marker "${m.name}"`, () => act("markers", { remove: [m.name] })]),
  ]);
});
function setPlayhead(b) {
  S.playhead = Math.max(0, b); $("#pos").textContent = fmtPos(S.playhead);
  if (S.audio.playing) { stop(false); startPlayback(); }
  drawArrangement(); drawRoll();
}

// ---------------------------------------------------------------- piano roll
const roll = $("#roll"), rollWrap = $("#rollWrap"), velCv = $("#vel"), KEYW = 44;
function selClip() { return S.sel.clip ? findClip(S.sel.clip) : [null, null]; }
function focusRoll() {
  if (S.tab !== "roll") document.querySelector('#tabs button[data-tab="roll"]').click();
  const [, c] = selClip();
  drawRoll();
  if (c && c.notes.length) {
    const avg = c.notes.reduce((a, n) => a + n[0], 0) / c.notes.length;
    rollWrap.scrollTop = (127 - avg) * S.rowH - rollWrap.clientHeight / 2;
  } else rollWrap.scrollTop = (127 - 66) * S.rowH - rollWrap.clientHeight / 2;
  rollWrap.scrollLeft = 0;
}
function drawRoll() {
  if (S.tab !== "roll") return;
  const [t, c] = selClip();
  const info = $("#rollInfo");
  const g = fitCanvas(roll, Math.max(rollWrap.clientWidth, c ? KEYW + (c.length + bpb()) * S.rollZoom : 0), 128 * S.rowH);
  const W = roll.clientWidth, sx = rollWrap.scrollLeft;
  g.fillStyle = "#101011"; g.fillRect(0, 0, W, 128 * S.rowH);
  if (!c || c.kind !== "midi") {
    info.textContent = "Select a MIDI clip (double-click empty lane space to create one)";
    g.fillStyle = "#8f897c"; g.font = "13px Segoe UI"; g.fillText("No MIDI clip selected", 20 + sx, rollWrap.scrollTop + 30);
    drawVel(null); return;
  }
  info.textContent = `${t.name} · ${c.name || c.id} · starts ${fmtPos(c.start)} · ${c.notes.length} notes · click: add  drag: move  edge: resize  dbl-click: delete  ctrl+wheel: zoom`;
  for (let p = 0; p < 128; p++) {
    const y = (127 - p) * S.rowH, black = [1, 3, 6, 8, 10].includes(p % 12);
    g.fillStyle = black ? "#131314" : "#18181a"; g.fillRect(KEYW, y, W, S.rowH);
    if (p % 12 === 0) { g.fillStyle = "#2a2a2d"; g.fillRect(KEYW, y + S.rowH - 1, W, 1); }
  }
  const len = c.length, X = (b) => KEYW + b * S.rollZoom;
  const step = S.snap > 0 && S.snap * S.rollZoom >= 6 ? S.snap : 0.25;
  for (let b = 0; b <= len + bpb(); b += step) {
    const isBar = Math.abs(b / bpb() - Math.round(b / bpb())) < 1e-6, isBeat = Math.abs(b - Math.round(b)) < 1e-6;
    g.fillStyle = isBar ? "#3b3b40" : isBeat ? "#232325" : "#1c1c1e"; g.fillRect(Math.round(X(b)), 0, 1, 128 * S.rowH);
  }
  g.fillStyle = "rgba(0,0,0,.35)"; g.fillRect(X(len), 0, W, 128 * S.rowH);
  const col = trackColor(t, S.doc.tracks.indexOf(t));
  c.notes.forEach((n, i) => {
    const y = (127 - n[0]) * S.rowH, sel = S.sel.notes.has(i);
    g.fillStyle = sel ? "#ffffff" : col; g.globalAlpha = 0.45 + 0.55 * n[3] / 127;
    g.fillRect(X(n[1]) + 0.5, y + 1, Math.max(3, n[2] * S.rollZoom - 1), S.rowH - 2); g.globalAlpha = 1;
    g.strokeStyle = sel ? "#ffb000" : "#0c0c0d"; g.strokeRect(X(n[1]) + 0.5, y + 1.5, Math.max(3, n[2] * S.rollZoom - 1), S.rowH - 3);
  });
  if (S.drag && S.drag.kind === "box") {
    const d = S.drag; g.strokeStyle = "#ffb000"; g.setLineDash([3, 3]);
    g.strokeRect(Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.abs(d.x1 - d.x0), Math.abs(d.y1 - d.y0)); g.setLineDash([]);
  }
  const ph = S.playhead - c.start; if (ph >= 0 && ph <= len) { g.fillStyle = "#ff4a3a"; g.fillRect(X(ph), 0, 1.5, 128 * S.rowH); }
  // sticky keyboard
  for (let p = 0; p < 128; p++) {
    const y = (127 - p) * S.rowH, black = [1, 3, 6, 8, 10].includes(p % 12);
    g.fillStyle = black ? "#252527" : "#cfcabd"; g.fillRect(sx, y, KEYW, S.rowH - 0.5);
    if (p % 12 === 0) { g.fillStyle = "#101011"; g.font = "9px Segoe UI"; g.fillText(noteName(p), sx + 18, y + S.rowH - 2); }
  }
  drawVel(c, col);
}
function drawVel(c, col) {
  const W = rollWrap.clientWidth, H = 64, g = fitCanvas(velCv, W, H), sx = rollWrap.scrollLeft;
  g.fillStyle = "#131314"; g.fillRect(0, 0, W, H);
  g.fillStyle = "#8f897c"; g.font = "10px Segoe UI"; g.fillText("vel", 8, 14);
  if (!c) return;
  c.notes.forEach((n, i) => {
    const x = KEYW + n[1] * S.rollZoom - sx; if (x < KEYW - 2 || x > W) return;
    const h = (H - 6) * n[3] / 127;
    g.fillStyle = S.sel.notes.has(i) ? "#ffffff" : col; g.fillRect(x, H - h, 3, h);
  });
}
rollWrap.addEventListener("scroll", () => drawRoll());
rollWrap.addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return; e.preventDefault();
  S.rollZoom = Math.min(400, Math.max(8, S.rollZoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15))); drawRoll();
}, { passive: false });
function rollHit(e) {
  const r = roll.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  const beat = (x - KEYW) / S.rollZoom, pitch = 127 - Math.floor(y / S.rowH);
  const [, c] = selClip(); let idx = -1, edge = false;
  if (c) for (let i = c.notes.length - 1; i >= 0; i--) {
    const n = c.notes[i];
    if (n[0] === pitch && beat >= n[1] && beat <= n[1] + n[2]) { idx = i; edge = (n[1] + n[2] - beat) * S.rollZoom < 6; break; }
  }
  return { x, y, beat, pitch, idx, edge, c };
}
roll.addEventListener("mousedown", (e) => {
  const h = rollHit(e); if (!h.c || e.button !== 0 || h.x < rollWrap.scrollLeft + KEYW) return;
  if (h.idx >= 0) {
    if (e.shiftKey) { S.sel.notes.has(h.idx) ? S.sel.notes.delete(h.idx) : S.sel.notes.add(h.idx); drawRoll(); return; }
    if (!S.sel.notes.has(h.idx)) { S.sel.notes.clear(); S.sel.notes.add(h.idx); }
    S.lastLen = h.c.notes[h.idx][2];
    S.drag = { kind: h.edge ? "nresize" : "nmove", b0: h.beat, p0: h.pitch, orig: h.c.notes.map((n) => n.slice()), moved: false };
  } else if (e.shiftKey || e.ctrlKey) {
    S.drag = { kind: "box", x0: h.x, y0: h.y, x1: h.x, y1: h.y };
  } else {
    const start = Math.max(0, floorB(h.beat)), vel = 90;
    h.c.notes.push([h.pitch, start, S.lastLen, vel]); S.sel.notes.clear(); S.sel.notes.add(h.c.notes.length - 1);
    S.drag = { kind: "nresize", b0: start + S.lastLen, p0: h.pitch, orig: h.c.notes.map((n) => n.slice()), moved: true, created: true };
    scheduleSave();
  }
  drawRoll();
});
function rollDrag(e) {
  const d = S.drag, h = rollHit(e), [, c] = selClip(); if (!c) return;
  if (d.kind === "box") {
    d.x1 = h.x; d.y1 = h.y;
    const b0 = (Math.min(d.x0, d.x1) - KEYW) / S.rollZoom, b1 = (Math.max(d.x0, d.x1) - KEYW) / S.rollZoom;
    const p1 = 127 - Math.floor(Math.min(d.y0, d.y1) / S.rowH), p0 = 127 - Math.floor(Math.max(d.y0, d.y1) / S.rowH);
    S.sel.notes.clear(); c.notes.forEach((n, i) => { if (n[0] >= p0 && n[0] <= p1 && n[1] + n[2] >= b0 && n[1] <= b1) S.sel.notes.add(i); });
    drawRoll(); return;
  }
  if (d.kind === "vel") { velDrag(e); return; }
  const db = h.beat - d.b0, dp = h.pitch - d.p0;
  if (Math.abs(db) * S.rollZoom > 2 || dp) d.moved = true;
  for (const i of S.sel.notes) {
    const o = d.orig[i], n = c.notes[i]; if (!o || !n) continue;
    if (d.kind === "nmove") { n[1] = Math.max(0, snapB(o[1] + db)); n[0] = Math.max(0, Math.min(127, o[0] + dp)); }
    else { n[2] = Math.max(S.snap > 0 ? S.snap / 2 : 0.0625, snapB(o[1] + o[2] + db) - o[1]); S.lastLen = n[2]; }
  }
  drawRoll();
}
function rollUp() { const d = S.drag; if (d && (d.moved || d.kind === "vel")) scheduleSave(); drawRoll(); }
roll.addEventListener("dblclick", (e) => {
  const h = rollHit(e); if (h.idx < 0) return;
  h.c.notes.splice(h.idx, 1); S.sel.notes.clear(); scheduleSave(); drawRoll();
});
velCv.addEventListener("mousedown", (e) => { const [, c] = selClip(); if (!c) return; S.drag = { kind: "vel" }; velDrag(e); });
function velDrag(e) {
  const [, c] = selClip(), r = velCv.getBoundingClientRect(), x = e.clientX - r.left + rollWrap.scrollLeft, y = e.clientY - r.top;
  const v = Math.max(1, Math.min(127, Math.round((64 - y) / 58 * 127))), beat = (x - KEYW) / S.rollZoom;
  const targets = S.sel.notes.size ? [...S.sel.notes] : c.notes.map((n, i) => i).filter((i) => Math.abs(c.notes[i][1] - beat) * S.rollZoom < 5);
  for (const i of targets) c.notes[i][3] = v;
  drawRoll();
}
function scheduleSave() {
  clearTimeout(S.pendingSave);
  S.pendingSave = setTimeout(async () => {
    const [, c] = selClip(); S.pendingSave = null; if (!c) return;
    try { await api("clip_set", { clip: c.id, notes: c.notes, expect_notes: S.base[c.id] || [] }); await loadState(); }
    catch (e) { toast(e.message); if (e.conflict) await loadState(); }
  }, 350);
  drawArrangement();
}

// ---------------------------------------------------------------- keyboard
document.addEventListener("keydown", (e) => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  const [, c] = selClip();
  if (e.code === "Space") { e.preventDefault(); S.audio.playing ? stop(false) : play(); return; }
  if (document.body.dataset.room !== "desk") return;  // editing keys belong to the desk
  if (e.ctrlKey && e.key.toLowerCase() === "z") { e.preventDefault(); act("undo"); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "y") { e.preventDefault(); act("redo"); return; }
  if (S.tab === "roll" && c && c.kind === "midi") {
    if (e.ctrlKey && e.key.toLowerCase() === "a") { e.preventDefault(); c.notes.forEach((n, i) => S.sel.notes.add(i)); drawRoll(); return; }
    if (S.sel.notes.size && (e.key === "Delete" || e.key === "Backspace")) {
      c.notes = c.notes.filter((n, i) => !S.sel.notes.has(i)); S.sel.notes.clear(); scheduleSave(); drawRoll(); return;
    }
    const moves = { ArrowUp: [0, e.shiftKey ? 12 : 1], ArrowDown: [0, e.shiftKey ? -12 : -1], ArrowLeft: [-(S.snap || 0.25), 0], ArrowRight: [S.snap || 0.25, 0] };
    if (S.sel.notes.size && moves[e.key]) {
      e.preventDefault(); const [db, dp] = moves[e.key];
      for (const i of S.sel.notes) { const n = c.notes[i]; n[1] = Math.max(0, n[1] + db); n[0] = Math.max(0, Math.min(127, n[0] + dp)); }
      scheduleSave(); drawRoll(); return;
    }
    if (e.ctrlKey && e.key.toLowerCase() === "d" && S.sel.notes.size) {
      e.preventDefault(); const sel = [...S.sel.notes].map((i) => c.notes[i]);
      const span = Math.max(...sel.map((n) => n[1] + n[2])) - Math.min(...sel.map((n) => n[1]));
      const add = sel.map((n) => [n[0], n[1] + span, n[2], n[3]]); S.sel.notes.clear();
      add.forEach((n) => { c.notes.push(n); S.sel.notes.add(c.notes.length - 1); }); scheduleSave(); drawRoll(); return;
    }
  }
  if (S.sel.clip && !S.sel.notes.size && e.key === "Delete") act("clip_edit", { clip: S.sel.clip, op: "delete" });
  if (S.sel.clip && e.ctrlKey && e.key.toLowerCase() === "d" && !S.sel.notes.size) { e.preventDefault(); act("clip_edit", { clip: S.sel.clip, op: "duplicate" }); }
});

// ---------------------------------------------------------------- transport (Web Audio)
async function play() {
  if (!S.doc || S.busy) return;
  window.Studio?.hush();
  try {
    if (S.audio.version !== S.version || !S.audio.buf) {
      S.busy = true; status("rendering…", "busy");
      const rep = await api("preview");
      S.lastRender = rep; S.busy = false;
      const file = rep.files[0];
      S.audio.ctx = S.audio.ctx || new AudioContext();
      const data = await (await fetch("/api/audio?file=" + encodeURIComponent(file) + "&t=" + Date.now())).arrayBuffer();
      S.audio.buf = await S.audio.ctx.decodeAudioData(data); S.audio.version = S.version; S.audio.rev = S.doc.rev; audioTag();
      status(rep.warnings.length ? `${rep.warnings.length} warning(s) - see Render report` : "", rep.warnings.length ? "busy" : "");
      if (S.tab === "report") renderPanel();
    }
    startPlayback();
  } catch (e) { S.busy = false; toast(e.message); status("render failed", "err"); }
}
function startPlayback() {
  const a = S.audio, ctx = a.ctx; if (!a.buf) return;
  if (ctx.state === "suspended") ctx.resume();
  const src = ctx.createBufferSource(); src.buffer = a.buf; src.connect(ctx.destination);
  let off = beatToSec(S.playhead);
  if (S.loopOn && S.doc.loop) {
    src.loop = true; src.loopStart = beatToSec(S.doc.loop.start); src.loopEnd = beatToSec(S.doc.loop.end);
    if (off < src.loopStart || off >= src.loopEnd) off = src.loopStart;
  }
  if (off >= a.buf.duration) off = 0;
  src.start(0, off); a.src = src; a.start = ctx.currentTime; a.offset = off; a.playing = true;
  src.onended = () => { if (a.src === src) { a.playing = false; $("#playBtn").classList.remove("on"); } };
  $("#playBtn").classList.add("on"); tick();
}
function stop(rewind) {
  const a = S.audio; if (a.src) { try { a.src.stop(); } catch { /* already stopped */ } a.src = null; }
  a.playing = false; $("#playBtn").classList.remove("on");
  if (rewind) setPlayhead(0);
}
function tick() {
  const a = S.audio; if (!a.playing) return;
  let t = a.offset + (a.ctx.currentTime - a.start);
  if (a.src && a.src.loop) { const ls = a.src.loopStart, le = a.src.loopEnd; if (t >= le) t = ls + ((t - ls) % (le - ls)); }
  S.playhead = secToBeat(t); $("#pos").textContent = fmtPos(S.playhead);
  const x = S.playhead * S.zoom;
  if (x > lanesWrap.scrollLeft + lanesWrap.clientWidth - 40) lanesWrap.scrollLeft = x - 80;
  drawArrangement(); if (S.tab === "roll") drawRoll();
  requestAnimationFrame(tick);
}

// ---------------------------------------------------------------- panels
function renderPanel() {
  if (S.tab === "mixer") renderMixer();
  else if (S.tab === "inspector") renderInspector();
  else if (S.tab === "report") renderReport();
  else if (S.tab === "brief") renderBrief();
}
function stripFx(list, trackRef) {
  return `<div class="fxl">${(list || []).map((f, i) => `<div class="fx ${f.bypass ? "byp" : ""}" data-i="${i}">
     <span title="${esc(fxLabel(f))} (click to bypass)">${esc(fxLabel(f))}</span><button class="small ghost danger" data-rm="${i}">×</button></div>`).join("")}
     <button class="small" data-addfx="${esc(trackRef)}">+ FX</button></div>`;
}
const fxLabel = (f) => (f.patch ? `${f.name} · ${f.patch}` : f.name) +
  (f.sidechain ? ` ← ${(S.doc.tracks.find((x) => x.id === f.sidechain) || { name: "?" }).name}` : "");
const canSidechain = (f) => f.type === "vst3" || (f.type === "builtin" && f.name === "compressor");
function renderMixer() {
  const box = $("#mixer"); if (!S.doc) { box.innerHTML = ""; return; }
  const buses = S.doc.tracks.filter((t) => t.kind === "bus");
  const lr = S.lastRender && S.lastRender.tracks || {};
  box.innerHTML = S.doc.tracks.map((t, i) => {
    const m = lr[t.name], lvl = m ? Math.max(0, Math.min(100, (m.rms_dbfs + 40) * 2.5)) : 0;
    return `<div class="strip" data-t="${t.id}"><h4 style="color:${trackColor(t, i)}" title="${esc(t.name)}">${esc(t.name)}</h4>
      <div class="muted" style="font-size:11px">${t.kind}${t.output !== "master" && t.output ? " → " + esc((S.doc.tracks.find((x) => x.id === t.output) || {}).name) : ""}</div>
      <div class="meter" title="last render RMS ${m ? m.rms_dbfs + " dBFS" : "-"}"><div style="width:${lvl}%"></div></div>
      ${stripFx(t.fx, t.id)}
      ${t.kind !== "bus" && buses.length ? `<div class="fxl">${(t.sends || []).map((s, k) => `<div class="fx"><span>→ ${esc((buses.find((b) => b.id === s.to) || {}).name)}</span>
        <input type="range" min="-40" max="6" step="0.5" value="${s.db}" data-send="${k}" style="width:56px"></div>`).join("")}
        <button class="small" data-addsend="1">+ Send</button></div>` : ""}
      <input class="fader" type="range" min="-60" max="12" step="0.5" value="${t.volume_db}">
      <div class="db">${(+t.volume_db).toFixed(1)} dB</div>
      <input type="range" class="pan" min="-1" max="1" step="0.05" value="${t.pan}" title="pan">
      <div style="display:flex;gap:4px;justify-content:center"><button class="small ${t.mute ? "on" : ""}" data-mute>M</button><button class="small ${t.solo ? "on" : ""}" data-solo>S</button></div>
    </div>`;
  }).join("") + `<div class="strip master" data-t="master"><h4>Master</h4>
      <div class="muted" style="font-size:11px">target ${S.doc.master.target_lufs} LUFS · ceiling ${S.doc.master.ceiling_db} dB</div>
      ${stripFx(S.doc.master.fx, "master")}
      <input class="fader" type="range" min="-24" max="12" step="0.5" value="${S.doc.master.volume_db || 0}">
      <div class="db">${(+(S.doc.master.volume_db || 0)).toFixed(1)} dB</div></div>`;
  box.querySelectorAll(".strip").forEach((el) => {
    const id = el.dataset.t, t = S.doc.tracks.find((x) => x.id === id);
    const fader = el.querySelector(".fader");
    fader.oninput = () => el.querySelector(".db").textContent = (+fader.value).toFixed(1) + " dB";
    fader.onchange = () => id === "master" ? act("project_set", { master_volume_db: +fader.value }) : act("track_update", { track: id, volume_db: +fader.value });
    const pan = el.querySelector(".pan"); if (pan) pan.onchange = () => act("track_update", { track: id, pan: +pan.value });
    const mu = el.querySelector("[data-mute]"); if (mu) mu.onclick = () => act("track_update", { track: id, mute: !t.mute });
    const so = el.querySelector("[data-solo]"); if (so) so.onclick = () => act("track_update", { track: id, solo: !t.solo });
    el.querySelectorAll(".fx[data-i] span").forEach((s) => s.onclick = () => {
      const i = +s.parentElement.dataset.i, list = id === "master" ? S.doc.master.fx : t.fx;
      act("fx_update", { track: id, index: i, bypass: !list[i].bypass });
    });
    el.querySelectorAll("[data-rm]").forEach((b) => b.onclick = () => act("fx_remove", { track: id, index: +b.dataset.rm }));
    el.querySelectorAll("[data-addfx]").forEach((b) => b.onclick = () => openDevicePicker(false, (dev) => act("fx_add", { track: id, device: dev })));
    el.querySelectorAll("[data-send]").forEach((r) => r.onchange = () => {
      const sends = t.sends.map((s, k) => ({ to: s.to, db: k === +r.dataset.send ? +r.value : s.db })); act("track_update", { track: id, sends });
    });
    const as = el.querySelector("[data-addsend]");
    if (as) as.onclick = (e) => menu(e, buses.filter((b) => !(t.sends || []).some((s) => s.to === b.id)).map((b) =>
      [b.name, () => act("track_update", { track: id, sends: [...(t.sends || []), { to: b.id, db: -12 }] })]));
  });
}

async function ensureLibrary(force = false) {
  if (!S.library || force) {
    [S.library, S.catalog, S.sfz] = await Promise.all([api("library_status"), api("synths_list"), api("sfz_list")]);
  }
}
function paramRows(defs, values, prefix) {
  return `<div class="params">${Object.entries(defs).map(([k, def]) => {
    const v = values && k in values ? values[k] : def;
    const span = Math.abs(def) > 0 ? Math.abs(def) * 4 : 1, step = Math.abs(def) >= 100 ? 1 : Math.abs(def) >= 1 ? 0.01 : 0.001;
    const min = def < 0 ? def * 3 : 0, max = def < 0 ? -def * 1 + 12 : Math.max(span, 1);
    return `<label class="param"><span title="${k}">${k}</span><input type="range" min="${min}" max="${max}" step="${step}" value="${v}" data-p="${prefix}${k}"><input type="number" step="${step}" value="${v}" data-pn="${prefix}${k}"></label>`;
  }).join("")}</div>`;
}
async function renderInspector() {
  const box = $("#inspector");
  const t = S.doc && S.doc.tracks.find((x) => x.id === S.sel.track);
  if (!t) { box.innerHTML = `<div class="empty">Select a track (click its header).</div>`; return; }
  await ensureLibrary();
  const inst = t.instrument;
  const synthDef = inst && inst.type === "synth" && S.catalog.synths[inst.name];
  const outs = S.doc.tracks.filter((x) => x.kind === "bus" && x.id !== t.id);
  box.innerHTML = `
  <div class="card"><h3>${esc(t.name)} <span class="muted">(${t.kind})</span></h3>
    <div class="grid2">
      ${t.kind === "midi" ? `<span>Instrument</span><div><b>${inst ? esc(instLabel(inst)) : "none"}</b>
        <button class="small" id="chooseInst">Change…</button> <button class="small" id="audBtn" ${inst ? "" : "disabled"}>Audition</button>
        ${inst && inst.type === "vst3" ? `<button class="small" id="capBtn" title="Opens the plugin window on this computer; pick a sound, close the window">Pick sound in plugin window…</button>` : ""}</div>` : ""}
      <span>Role</span><input id="roleIn" value="${esc(t.role || "")}" placeholder="violin, cello, bass, pad, drums…">
      <span>Layer</span><input id="layerIn" value="${esc(t.layer || "")}" placeholder="adaptive layer, e.g. base / combat">
      <span>Output</span><select id="outSel"><option value="master">Master</option>${outs.map((b) => `<option value="${b.id}" ${t.output === b.id ? "selected" : ""}>${esc(b.name)}</option>`).join("")}</select>
    </div></div>
  ${synthDef ? `<div class="card"><h3>Synth: ${esc(inst.name)} <span class="muted">${esc(synthDef.description)}</span></h3>${paramRows(synthDef.params, inst.params, "i:")}</div>` : ""}
  <div class="card"><h3>Effects</h3>${(t.fx || []).map((f, i) => {
    const def = f.type === "builtin" && S.catalog.effects[f.name];
    return `<div style="margin-bottom:10px"><b>${i + 1}. ${esc(fxLabel(f))}</b> <span class="muted">${def ? esc(def.description) : f.type}</span>
      <button class="small" data-byp="${i}">${f.bypass ? "Enable" : "Bypass"}</button> <button class="small danger" data-del="${i}">Remove</button>
      ${f.type === "vst3" ? `<button class="small" data-vp="${i}">Parameters…</button>` : ""}
      ${canSidechain(f) ? `<label class="muted" title="Another track's sound drives this effect's detector (ducking). Plugins without a sidechain input refuse.">
        Sidechain <select data-sc="${i}"><option value="">none</option>${S.doc.tracks.filter((x) => x.kind !== "bus" && x.id !== t.id)
          .map((x) => `<option value="${x.id}" ${f.sidechain === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></label>` : ""}
      ${def ? paramRows(def.params, f.params, `f${i}:`) : ""}</div>`;
  }).join("")}<button class="small" id="addFx">+ Add effect</button></div>`;
  const on = (sel, ev, fn) => { const el = box.querySelector(sel); if (el) el[ev] = fn; };
  on("#chooseInst", "onclick", () => openDevicePicker(true, (dev) => act("track_update", { track: t.id, instrument: dev })));
  on("#audBtn", "onclick", async () => {
    status("auditioning…", "busy");
    try { const r = await api("audition", { instrument: devString(inst) }); status(""); playFile(r.file); if (r.silent) toast("Audition was silent - the instrument has no sound loaded"); }
    catch (e) { toast(e.message); status("", ""); }
  });
  on("#capBtn", "onclick", () => openCapture(t));
  on("#roleIn", "onchange", (e) => act("track_update", { track: t.id, role: e.target.value }));
  on("#layerIn", "onchange", (e) => act("track_update", { track: t.id, layer: e.target.value }));
  on("#outSel", "onchange", (e) => act("track_update", { track: t.id, output: e.target.value }));
  on("#addFx", "onclick", () => openDevicePicker(false, (dev) => act("fx_add", { track: t.id, device: dev })));
  box.querySelectorAll("[data-byp]").forEach((b) => b.onclick = () => act("fx_update", { track: t.id, index: +b.dataset.byp, bypass: !t.fx[+b.dataset.byp].bypass }));
  box.querySelectorAll("[data-del]").forEach((b) => b.onclick = () => act("fx_remove", { track: t.id, index: +b.dataset.del }));
  box.querySelectorAll("[data-vp]").forEach((b) => b.onclick = () => openVstParams(t, +b.dataset.vp));
  box.querySelectorAll("[data-sc]").forEach((s) => s.onchange = () => act("fx_update", { track: t.id, index: +s.dataset.sc, sidechain: s.value }));
  const commit = (key, val) => {
    const [where, name] = key.split(":");
    if (where === "i") act("track_update", { track: t.id, instrument_params: { [name]: val } });
    else act("fx_update", { track: t.id, index: +where.slice(1), params: { [name]: val } });
  };
  box.querySelectorAll("[data-p]").forEach((r) => {
    const num = box.querySelector(`[data-pn="${r.dataset.p}"]`);
    r.oninput = () => num.value = r.value; r.onchange = () => commit(r.dataset.p, +r.value);
    num.onchange = () => { r.value = num.value; commit(r.dataset.p, +num.value); };
  });
}
const devString = (d) => d.patch ? `patch:${d.patch}` : `${d.type}:${d.name}`;
function playFile(file) {
  const a = new Audio("/api/audio?file=" + encodeURIComponent(file) + "&t=" + Date.now()); a.play();
}

function renderReport() {
  const box = $("#report"), r = S.lastRender;
  if (!r) { box.innerHTML = `<div class="empty">No render yet. Press Play or Export.</div>`; return; }
  const m = r.mix || {}, b = m.bands_pct || {};
  const maxB = Math.max(1, ...Object.values(b));
  box.innerHTML = `
  <div class="card"><h3>Mix</h3><table class="kv">
    <tr><td>Loudness</td><td><b>${m.lufs} LUFS</b> (normalised ${r.normalize_gain_db >= 0 ? "+" : ""}${r.normalize_gain_db} dB)</td></tr>
    <tr><td>True peak</td><td>${m.true_peak_dbtp} dBTP · limiter ${r.limiter_max_reduction_db} dB</td></tr>
    <tr><td>Duration</td><td>${m.duration_s} s · ${r.loop_mode === "off" ? "one-shot" : r.loop_mode}${r.loop_seam ? ` · seam jump ${r.loop_seam.sample_jump}` : ""}</td></tr>
    <tr><td>Stereo correlation</td><td>${m.stereo_correlation}</td></tr></table>
    <div class="bars">${Object.entries(b).map(([k, v]) => `<div style="height:${(v / maxB) * 100}%" title="${k}: ${v}%"><span>${k.replace("_", "-")}<br>${v}%</span></div>`).join("")}</div><br>
  </div>
  <div class="card"><h3>Warnings</h3>${r.warnings.length ? `<ul class="tight">${r.warnings.map((w) => `<li class="warn">${esc(w)}</li>`).join("")}</ul>` : `<span class="ok">None</span>`}</div>
  <div class="card"><h3>Tracks</h3><table class="kv">${Object.entries(r.tracks).map(([n, s]) => `<tr><td>${esc(n)}</td><td>peak ${s.peak_dbfs} dBFS</td><td>rms ${s.rms_dbfs} dBFS</td><td class="muted">${s.render}</td></tr>`).join("")}</table></div>
  <div class="card"><h3>Files</h3>${r.files.map((f) => `<div>${/\.(wav|ogg|flac|mp3)$/i.test(f) ? `<button class="small" data-play="${esc(f)}">▶</button>` : ""} ${esc(f)}</div>`).join("")}</div>`;
  box.querySelectorAll("[data-play]").forEach((b) => b.onclick = () => playFile(b.dataset.play));
}

function renderBrief() {
  const box = $("#brief"); if (!S.doc) { box.innerHTML = ""; return; }
  const br = S.doc.brief || {}, keys = Array.from(new Set(["genre", "mood", "use", "game", "references", "notes", ...Object.keys(br)]));
  box.innerHTML = `<div class="card"><h3>Brief (what the music is for)</h3><div class="grid2">
    ${keys.map((k) => `<span>${esc(k)}</span>${k === "notes" ? `<textarea data-b="${k}" rows="3">${esc(br[k] || "")}</textarea>` : `<input data-b="${k}" value="${esc(typeof br[k] === "string" ? br[k] : JSON.stringify(br[k] ?? ""))}">`}`).join("")}
  </div></div>
  <div class="card"><h3>Delivery</h3><div class="grid2">
    <span>Loop region</span><span>${S.doc.loop ? `${fmtPos(S.doc.loop.start)} → ${fmtPos(S.doc.loop.end)}` : "none (shift-drag on the ruler to set)"}</span>
    <span>Target loudness</span><input id="lufsIn" type="number" step="0.5" value="${S.doc.master.target_lufs}">
    <span>Peak ceiling dB</span><input id="ceilIn" type="number" step="0.1" value="${S.doc.master.ceiling_db}">
    <span>Tail seconds</span><input id="tailIn" type="number" step="0.5" value="${S.doc.tail}">
  </div></div>`;
  box.querySelectorAll("[data-b]").forEach((el) => el.onchange = () => act("project_set", { brief: { [el.dataset.b]: el.value } }));
  $("#lufsIn").onchange = (e) => act("project_set", { target_lufs: +e.target.value });
  $("#ceilIn").onchange = (e) => act("project_set", { ceiling_db: +e.target.value });
  $("#tailIn").onchange = (e) => act("project_set", { tail: +e.target.value });
}

// ---------------------------------------------------------------- modals & menus
function modal(html) { $("#modalBox").innerHTML = html; $("#modal").classList.remove("hidden"); return $("#modalBox"); }
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("mousedown", (e) => { if (e.target.id === "modal") closeModal(); });
function menu(e, items) {
  const m = $("#menu"); m.innerHTML = ""; if (!items.length) return;
  items.forEach(([label, fn]) => { const d = document.createElement("div"); d.textContent = label; d.onclick = () => { m.classList.add("hidden"); fn(); }; m.appendChild(d); });
  m.style.left = Math.min(e.clientX, innerWidth - 200) + "px"; m.style.top = Math.min(e.clientY, innerHeight - items.length * 30 - 10) + "px";
  m.classList.remove("hidden");
}
document.addEventListener("mousedown", (e) => { if (!e.target.closest("#menu")) $("#menu").classList.add("hidden"); });

function openProjectModal(recent) {
  const box = modal(`<h2>Projects</h2>
    ${recent && recent.length ? `<div class="card"><h3>Recent</h3>${recent.map((r) => `<div><a href="#" data-open="${esc(r)}">${esc(r)}</a></div>`).join("")}</div>` : ""}
    <div class="card"><h3>New project</h3><div class="grid2">
      <span>Name</span><input id="npName" value="Menu Theme">
      <span>Tempo</span><input id="npTempo" type="number" value="96">
      <span>Key</span><input id="npKey" value="D minor">
      <span>Time signature</span><input id="npSig" value="4/4">
      <span>Genre</span><input id="npGenre" placeholder="orchestral fantasy, synthwave, chiptune…">
      <span>Mood</span><input id="npMood" placeholder="hopeful, tense, playful…">
      <span>Use</span><input id="npUse" placeholder="main menu loop, battle, ambience…">
    </div></div>
    <div class="row"><button id="npCancel" class="ghost">Close</button><button id="npCreate">Create</button></div>`);
  box.querySelectorAll("[data-open]").forEach((a) => a.onclick = async (e) => { e.preventDefault(); await act("project_open", { path: a.dataset.open }); closeModal(); });
  $("#npCancel").onclick = closeModal;
  $("#npCreate").onclick = async () => {
    const brief = {}; [["genre", "#npGenre"], ["mood", "#npMood"], ["use", "#npUse"]].forEach(([k, s]) => { if ($(s).value) brief[k] = $(s).value; });
    await act("project_new", { name: $("#npName").value, tempo: +$("#npTempo").value, key: $("#npKey").value, time_sig: $("#npSig").value, brief });
    closeModal();
  };
}
function openAddTrack(kind, init = {}) {
  modal(`<h2>Add ${kind === "midi" ? "instrument" : kind} track</h2><div class="grid2">
    <span>Name</span><input id="atName" value="${esc(init.name || (kind === "bus" ? "Reverb" : kind === "audio" ? "Audio" : "Strings"))}">
    ${kind === "midi" ? `<span>Instrument</span><div><span id="atInst" class="muted">${esc(init.inst || "none")}</span> <button class="small" id="atPick">Choose…</button></div>
    <span>Role</span><input id="atRole" value="${esc(init.role || "")}" placeholder="violin, bass, pad, drums…">` : ""}
    ${kind === "bus" ? `<span>Effect</span><select id="atFx"><option value="builtin:reverb">Built-in reverb</option><option value="builtin:delay">Built-in delay</option><option value="">None</option></select>` : ""}
    </div><div class="row"><button class="ghost" id="atCancel">Cancel</button><button id="atOk">Add</button></div>`);
  $("#atCancel").onclick = closeModal;
  if (kind === "midi") $("#atPick").onclick = () => {
    const keep = { name: $("#atName").value, role: $("#atRole").value };
    openDevicePicker(true, (v) => openAddTrack(kind, { ...keep, inst: v }), () => openAddTrack(kind, { ...keep, inst: init.inst }));
  };
  $("#atOk").onclick = async () => {
    const args = { name: $("#atName").value, kind, instrument: init.inst || null };
    if (kind === "midi" && $("#atRole").value) args.role = $("#atRole").value;
    if (kind === "bus" && $("#atFx").value) args.fx = [$("#atFx").value];
    const r = await act("track_add", args); S.sel.track = r.track; closeModal(); renderAll();
  };
}
async function openDevicePicker(instrument, cb, onCancel) {
  await ensureLibrary();
  const L = S.library, C = S.catalog;
  const groups = instrument ? [
    ["Patches (saved plugin sounds)", L.patches.map((p) => [`patch:${p.id}`, `${p.name} — ${p.plugin}${p.tags.length ? " [" + p.tags.join(", ") + "]" : ""}`])],
    ["Built-in synths", Object.entries(C.synths).map(([k, v]) => [`synth:${k}`, `${k} — ${v.description}`])],
    ["SFZ instruments", (S.sfz.sfz || []).map((f) => [`sfz:${f}`, f.split(/[\/]/).slice(-2).join("/")])],
    ["VST3 instruments (no saved sound)", L.vst3_instruments.map((n) => [`vst3:${n}`, n])],
  ] : [
    ["Built-in effects", Object.entries(C.effects).map(([k, v]) => [`builtin:${k}`, `${k} — ${v.description}`])],
    ["VST3 effects", L.vst3_effects.map((n) => [`vst3:${n}`, n])],
  ];
  const box = modal(`<h2>Choose ${instrument ? "instrument" : "effect"}</h2><input id="dpQ" placeholder="filter…" style="width:100%;margin-bottom:8px">
    <div id="dpList">${groups.map(([g, items]) => items.length ? `<div class="card"><h3>${esc(g)}</h3>${items.map(([v, l]) => `<div class="dpi"><a href="#" data-v="${esc(v)}">${esc(l)}</a></div>`).join("")}</div>` : "").join("")}</div>
    <div class="row"><button class="ghost" id="dpCancel">Cancel</button></div>`);
  $("#dpCancel").onclick = () => onCancel ? onCancel() : closeModal();
  $("#dpQ").oninput = (e) => box.querySelectorAll(".dpi").forEach((d) => d.style.display = d.textContent.toLowerCase().includes(e.target.value.toLowerCase()) ? "" : "none");
  $("#dpQ").focus();
  box.querySelectorAll("[data-v]").forEach((a) => a.onclick = (e) => { e.preventDefault(); closeModal(); cb(a.dataset.v); });
}
async function openVstParams(t, i) {
  const f = t.fx[i];
  const box = modal(`<h2>${esc(fxLabel(f))} parameters</h2><input id="vpQ" placeholder="filter (loads plugin)…" style="width:100%"><div id="vpList" class="muted" style="margin-top:8px">loading…</div>
    <div class="row"><button class="ghost" id="vpClose">Close</button></div>`);
  $("#vpClose").onclick = closeModal;
  const load = async (q) => {
    try {
      const r = await api("plugin_params", { device: devString(f), query: q || null, limit: 80 });
      $("#vpList").innerHTML = `<div class="params">${r.params.map((p) => `<label class="param"><span title="${esc(p.name)}">${esc(p.name)}</span>
        <input type="range" min="0" max="1" step="0.001" value="${f.params[p.name] ?? p.value}" data-n="${esc(p.name)}"><span class="muted">${esc(p.text)}</span></label>`).join("")}</div>
        <div class="muted">${r.total} parameters${r.total > r.params.length ? " (filter to see more)" : ""}</div>`;
      box.querySelectorAll("[data-n]").forEach((el) => el.onchange = () => act("fx_update", { track: t.id, index: i, params: { [el.dataset.n]: +el.value } }));
    } catch (e) { $("#vpList").textContent = e.message; }
  };
  $("#vpQ").onchange = (e) => load(e.target.value); load("");
}
function openCapture(t) {
  const inst = t.instrument;
  modal(`<h2>Pick a sound in ${esc(inst.name)}</h2>
    <p class="muted">The plugin's own window opens on this computer. Load a preset / program, tweak it, then <b>close the plugin window</b>. The sound is saved as a reusable patch the agent can use by name.</p>
    <div class="grid2"><span>Patch name</span><input id="cpName" placeholder="Warm Strings">
    <span>Tags</span><input id="cpTags" placeholder="strings, legato, warm">
    <span>Description</span><input id="cpDesc" placeholder="what it sounds like / best used for">
    <span>Role</span><input id="cpRole" value="${esc(t.role || "")}" placeholder="violin, bass, pad, drums…">
    <span>Dynamics CC</span><select id="cpCC"><option value="">velocity only</option><option value="1">CC1 (mod wheel)</option><option value="11">CC11 (expression)</option></select></div>
    <div class="row"><button class="ghost" id="cpCancel">Cancel</button><button id="cpGo">Open plugin window</button></div>`);
  $("#cpCancel").onclick = closeModal;
  $("#cpGo").onclick = async () => {
    const name = $("#cpName").value.trim(); if (!name) { toast("Give the patch a name"); return; }
    const args = { plugin: inst.name, name, tags: $("#cpTags").value.split(",").map((s) => s.trim()).filter(Boolean),
      description: $("#cpDesc").value, role: $("#cpRole").value || null, dynamics_cc: $("#cpCC").value ? +$("#cpCC").value : null,
      base_patch: inst.patch || null };
    closeModal(); status("waiting for the plugin window to close…", "busy");
    try {
      const meta = await api("patch_capture", args); status("");
      if (meta.warning) toast(meta.warning); else toast(`Saved patch "${meta.id}"`, true);
      S.library = null; await act("track_update", { track: t.id, instrument: `patch:${meta.id}` });
    } catch (e) { toast(e.message); status("", ""); }
  };
}
function openExportModal() {
  if (!S.doc) return;
  modal(`<h2>Export</h2><div class="grid2">
    <span>Output</span><input id="exOut" placeholder="(default: project renders folder) or C:\\Game\\Audio\\Music\\menu.ogg">
    <span>Format</span><select id="exFmt"><option>wav</option><option>ogg</option><option>flac</option><option>mp3</option></select>
    <span>Bit depth (wav)</span><select id="exBits"><option>24</option><option>16</option><option>32</option></select>
    <span>Loop</span><select id="exLoop"><option value="auto">auto (use loop region)</option><option value="off">off (one-shot with tail)</option><option value="loop">loop body only</option><option value="intro+loop">intro + loop (loop points)</option></select>
    <span>Normalise</span><label><input type="checkbox" id="exNorm" checked> to ${S.doc.master.target_lufs} LUFS</label>
    <span>Extras</span><div><label><input type="checkbox" id="exStems"> stems</label> <label><input type="checkbox" id="exLayers"> layers</label> <label><input type="checkbox" id="exMidi"> MIDI</label></div>
  </div><div class="row"><button class="ghost" id="exCancel">Cancel</button><button id="exGo">Render</button></div>`);
  $("#exCancel").onclick = closeModal;
  $("#exGo").onclick = async () => {
    const args = { format: $("#exFmt").value, bits: +$("#exBits").value, loop: $("#exLoop").value, normalize: $("#exNorm").checked,
      stems: $("#exStems").checked, layers: $("#exLayers").checked, midi: $("#exMidi").checked };
    if ($("#exOut").value.trim()) args.out = $("#exOut").value.trim();
    closeModal(); status("rendering…", "busy");
    try { S.lastRender = await api("render", args); status("exported"); document.querySelector('#tabs button[data-tab="report"]').click(); }
    catch (e) { toast(e.message); status("render failed", "err"); }
  };
}

loadState();
