// TuneTrack desk (arrangement, piano roll, mixer) and the shared header. Every edit goes through /api/call (the
// same API the agents use); the project file is the source of truth. studio.js draws the crew rack and follows
// /api/events, so agent edits show up live; the 1 s poll below is only the fallback when that stream is down.
"use strict";

const $ = (s) => document.querySelector(s);
const PALETTE = ["#5e9cec", "#ec785e", "#78c878", "#d296e6", "#f0c850", "#5ac8c8", "#e66ea0", "#aaaa5a", "#8c8cf0", "#f09650"];
const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const S = {
  doc: null, version: 0, path: null, lastRender: null, library: null, catalog: null, sfz: null,
  take: null, takeMeta: null, sessionVersion: 0,  // the take the desk has open (null: the session itself)
  sel: { track: null, clip: null, notes: new Set() },
  zoom: 22, rollZoom: 64, rowH: 12, snap: 1, playhead: 0, loopOn: false, lastLen: 1,
  pendingSave: null, busy: false, tab: "roll",
  audio: { ctx: null, buf: null, src: null, start: 0, offset: 0, playing: false, version: -1 },
  // LIVE transport (S.live is taken: studio.js's event-stream flag). on: the user's choice; st: last engine state
  eng: { on: false, st: null, at: 0, busy: false, stopAfter: false, hold: false, raf: 0 },
};
try { S.eng.on = localStorage.getItem("tt.live") === "1"; } catch { /* no storage */ }

// ---------------------------------------------------------------- api
const TOKEN = document.querySelector('meta[name="tt-token"]')?.content || "";
// With a take open on the desk, the server points the desk's own calls (edits, undo, playback, export) at that
// take. Board calls ignore it. A seat's bench edits what it shows, which need not be what the desk has open: it
// passes its own take (null: the session), and rev where an edit only makes sense against the revision it read.
async function api(fn, args = {}, take = S.take, rev = null) {
  const body = take ? { fn, args, take, session: S.path } : { fn, args };
  if (rev != null) body.rev = rev;
  const r = await fetch("/api/call", { method: "POST", headers: { "Content-Type": "application/json", "X-TuneTrack-Token": TOKEN }, body: JSON.stringify(body) });
  const j = await r.json();
  if (j.error) { const e = new Error(j.error); e.conflict = !!j.conflict; throw e; }
  return j.result;
}
// An edit that names a thing by its place in a list (the second effect, the third track) only means what the page
// showed: it carries the revision the page read, and is refused and reloaded if the project has moved on.
// automation sends a whole lane (clear + points), so it too must not overwrite a lane the page has not seen.
const BY_PLACE = new Set(["fx_update", "fx_remove", "track_move", "automation"]);
async function act(fn, args, msg) {
  const rev = BY_PLACE.has(fn) && S.doc ? S.doc.rev ?? null : null;
  try { status(msg || "", "busy"); const r = await api(fn, args, S.take, rev); status(""); await loadState(); return r; }
  catch (e) {
    const moved = e.conflict && rev != null && /changed since you loaded/.test(e.message);
    toast(moved ? "Someone else changed this while you were looking at it, so that edit was not made. The desk now shows it as it is: try again." : e.message);
    status(moved ? "" : "error", moved ? "" : "err"); if (e.conflict) loadState().catch(() => {}); throw e;
  }
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
function tempoAt(b) { const pts = tempoPts(); let p = pts[0]; for (const q of pts) if (q.b <= b) p = q; return p.bpm; }
function fmtPos(b) { const bar = Math.floor(b / bpb()) + 1; return `${bar}:${(b - (bar - 1) * bpb() + 1).toFixed(2)}`; }
const snapB = (b, snap = S.snap) => snap > 0 ? Math.round(b / snap) * snap : b;
const floorB = (b, snap = S.snap) => snap > 0 ? Math.floor(b / snap + 1e-9) * snap : b;
const noteName = (p) => NOTE_NAMES[p % 12] + (Math.floor(p / 12) - 1);
const trackColor = (t, i) => t.color || PALETTE[i % PALETTE.length];
// The canvases' palette, matching style.css's tokens. UI_FONT/MONO are what the canvases write labels in.
const CV = {
  bg: "#0d0f12", band: "rgba(255,255,255,.014)", beat: "#14171b", bar: "#1d2126", phrase: "#2a2f36",
  lane: "#08090b", laneHi: "rgba(255,255,255,.025)", sel: "rgba(255,178,36,.05)", loop: "rgba(79,220,132,.045)",
  ruler: "#121417", rulerTxt: "#8e8b84", tick: "#2c3138", tickMinor: "#1f2328", marker: "#ffb224", play: "#ff5545",
  vfd: "#7cf0d6", vfdBg: "#0a1716", dim: "#64625c",
};
const UI_FONT = "'Segoe UI Variable Text', 'Segoe UI', system-ui, sans-serif", MONO = "'Cascadia Mono', Consolas, ui-monospace, monospace";
// a rounded rectangle path (r per corner: [tl, tr, br, bl] or one number)
function rrect(g, x, y, w, h, r) { g.beginPath(); g.roundRect(x, y, Math.max(0, w), Math.max(0, h), r); }
const songEnd = () => {
  let e = 0; for (const t of S.doc.tracks) for (const c of t.clips) e = Math.max(e, c.start + c.length);
  if (S.doc.loop) e = Math.max(e, S.doc.loop.end); if (S.doc.length) e = Math.max(e, S.doc.length); return e;
};
const findClip = (id) => { for (const t of S.doc.tracks) for (const c of t.clips) if (c.id === id) return [t, c]; return [null, null]; };

// ---------------------------------------------------------------- load / poll
const TAKE_LIVE = ["open", "submitted"];
async function loadState() {
  const asked = S.take;
  const st = await (await fetch("/api/state" + (asked ? "?take=" + encodeURIComponent(asked) : ""))).json();
  if (asked !== S.take) return;  // the desk moved to another take while this was in flight; that load wins
  if (!st.project) { S.doc = null; S.path = null; leaveTake(); renderAll(); window.Studio?.set(null, null); openProjectModal(st.recent || []); return; }
  if (asked) {
    // A take id only means something inside its own session, and only while the take is still in play.
    const moved = S.path && st.project !== S.path, gone = !st.take || !TAKE_LIVE.includes(st.take.state);
    if (moved || gone) {
      if (!moved) toast(st.take ? `${asked} was ${st.take.state === "accepted" ? "accepted into the session" : "closed"}. The desk is back on the session.` : `${asked} is not a take of this session. The desk is back on the session.`, true);
      leaveTake(); return loadState();
    }
  }
  S.doc = st.doc; S.pic = st.picture || null; S.version = st.version; S.sessionVersion = st.session_version; S.path = st.project; S.takeMeta = st.take;
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
    const v = await (await fetch("/api/version" + (S.take ? "?take=" + encodeURIComponent(S.take) : ""))).json();
    if (v.version !== S.version || v.project !== S.path || v.session_version !== S.sessionVersion) await loadState();
  } catch { /* server restarting */ }
}, 1000);

// ---------------------------------------------------------------- takes on the desk
// The desk shows the session, or one take of it. In a take, every edit, undo, playback and export is the take's;
// the session does not change until the take is accepted.
function leaveTake() {
  if (!S.take && !S.takeMeta) return;
  S.take = null; S.takeMeta = null; switchedView();
}
function switchedView() {
  if (liveMode() && isPlaying()) livePlay(); else stop(false);  // a live engine re-points at what the desk now shows
  S.audio.buf = null; S.audio.version = -1; S.lastRender = null;
  S.sel.notes.clear(); clearTimeout(S.pendingSave); S.pendingSave = null;
  window.Studio?.listen();  // the event stream follows the take the desk has open
}
async function openTake(id, room = true) {
  if (S.pendingSave) { toast("Still saving your last edit: try again in a moment."); return; }
  S.take = id || null; S.takeMeta = null; switchedView();
  if (room) window.Studio?.room("desk");
  await loadState();
}
async function cutTake(args) {
  try {
    status("cutting a take…", "busy");
    const r = await api("take_cut", args); status("");
    await openTake(r.take);
    toast(r.continued ? `Picked up ${r.take} where it was left.` : `${r.take} cut. The desk now edits the take; the session stays as it is until the take is accepted.`, true);
  } catch (e) { status(""); toast(e.message); }
}
function renderTakeBar() {
  const bar = $("#takeBar"), board = window.Studio?.board(), m = S.takeMeta;
  document.body.classList.toggle("intake", !!m);
  bar.classList.toggle("hidden", !S.doc);
  if (!S.doc) return;
  const takes = (board && board.takes) || [], seatName = (id) => (board && (board.seats.find((s) => s.seat === id) || {}).title) || (id === "you" ? "You" : id || "");
  const sel = $("#takeSel"), opts = [["", "SESSION"], ...takes.map((t) => [t.id, `${t.id.toUpperCase()} · ${seatName(t.seat)} · ${t.state === "submitted" ? "handed in" : "in progress"}${t.name && t.name !== t.id ? " · " + t.name : ""}`])];
  if (m && !takes.some((t) => t.id === m.id)) opts.push([m.id, m.id.toUpperCase()]);
  const sig = JSON.stringify(opts);
  if (sel.dataset.sig !== sig) { sel.dataset.sig = sig; sel.innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join(""); }
  if (document.activeElement !== sel) sel.value = S.take || "";
  $("#takeLamp").className = "lamp " + (m ? "cyan" : "green");
  $("#takeRead").innerHTML = m
    ? `${esc(seatName(m.seat).toUpperCase())}${m.task ? ` · <span class="amb">${esc(m.task)}</span>` : ""} · ${m.changes} CHANGE${m.changes === 1 ? "" : "S"}`
    : `LIVE · REV ${S.doc.rev ?? "?"}`;
  $("#takeRead").title = m ? "Whose take this is, the task it is for, and how many things it changes" : "You are editing the session itself: every change is the music everyone hears";
  $("#takeTags").innerHTML = m ? `${m.state === "submitted" ? `<span class="tag cy" title="${esc(m.summary || "")}">HANDED IN</span>` : ""}${m.returns ? `<span class="tag stale ${m.reason ? "why" : ""}" id="takeWhy" title="${esc(m.reason || "")}">RETURNED ${m.returns}×${m.reason ? " · WHY" : ""}</span>` : ""}${m.session_moved ? `<span class="tag stale" title="The session has changed since this take was cut. SYNC brings those changes into the take.">SESSION MOVED ON</span>` : ""}` : "";
  if (m && m.reason && $("#takeWhy")) $("#takeWhy").onclick = () => { modal(`<h2>${esc(m.id)} was sent back</h2><div class="reader">${esc(m.reason)}</div><div class="row"><button class="ghost" id="twClose">Close</button></div>`); $("#twClose").onclick = closeModal; };
  for (const id of ["takeChanges", "takeSync", "takeSubmit", "takeLeave"]) $("#" + id).classList.toggle("hidden", !m);
  $("#takeNew").classList.toggle("hidden", !!m);
  if (m) { $("#takeSync").disabled = !m.session_moved; $("#takeSubmit").textContent = m.state === "submitted" ? "HAND IN AGAIN…" : "HAND IN…"; $("#takeSubmit").disabled = !m.changes; }
}
$("#takeSel").onchange = (e) => openTake(e.target.value, false);
$("#takeLeave").onclick = () => openTake(null, false);
$("#takeNew").onclick = () => {
  const board = window.Studio?.board(); if (!board) return;
  const mine = board.seats.filter((s) => s.occupant && s.occupant.kind === "human");
  const tasksOf = (seat) => board.tasks.filter((t) => t.seat === seat && ["queued", "working", "needs_you"].includes(t.state));
  // A sheet: the seat it is for (tiles), that seat's open tasks (rows), a name.
  const P = { seat: mine[0] ? mine[0].seat : "", task: "" };
  const who = (s) => s.occupant ? (s.occupant.kind === "human" ? "You sit here" : s.label || s.occupant.kind) : "Empty";
  const el = Sheet.open({ title: "Cut a take", size: "seat", body: `<form class="sform" autocomplete="off">
      <p class="spWhy">A take is a private copy of the session. The Desk edits the copy, nobody hears it until you hand it in, and accepting it merges only what you changed.</p>
      <div class="phH sub">For seat</div>
      <div class="tiles ctSeats"><button type="button" data-seat=""><b>NO SEAT</b><span>Your own take</span></button>
        ${board.seats.map((s) => `<button type="button" data-seat="${esc(s.seat)}"><b>${esc(s.title)}</b><span>${esc(who(s))}</span></button>`).join("")}</div>
      <div class="phH sub">For task</div>
      <div class="spRows ctTasks" role="radiogroup"></div>
      <label class="fld"><span>Name</span><input id="ctName" placeholder="What this take tries, e.g. busier bass in the B section (optional)"></label>
    </form>`, foot: `<span class="spacer"></span><button class="key" id="ctCancel">CANCEL</button><button class="key go" id="ctOk">CUT THE TAKE</button>` });
  const draw = () => {
    el.querySelectorAll("[data-seat]").forEach((b) => b.classList.toggle("on", b.dataset.seat === P.seat));
    const ts = P.seat ? tasksOf(P.seat) : [];
    if (!ts.some((t) => t.id === P.task)) P.task = "";
    el.querySelector(".ctTasks").innerHTML = [{ id: "", title: P.seat ? "No task: try something of your own" : "No task" }, ...ts].map((t) => `<button type="button" role="radio" data-task="${esc(t.id)}" class="${P.task === t.id ? "on" : ""}" aria-checked="${P.task === t.id}">
      <span class="spM"><i class="dot"></i>${t.id ? `<b>${esc(t.id)}</b>&nbsp;` : ""}${esc(t.title)}</span><span>${t.take ? `<em class="tag cy">CONTINUES ${esc(t.take)}</em>` : ""}</span></button>`).join("");
    el.querySelectorAll("[data-task]").forEach((b) => b.onclick = () => { P.task = b.dataset.task; draw(); });
  };
  el.querySelectorAll("[data-seat]").forEach((b) => b.onclick = () => { P.seat = b.dataset.seat; draw(); });
  draw();
  const go = () => { const args = { seat: P.seat || null, task: P.task || null, name: $("#ctName").value.trim() || null }; Sheet.close(true); cutTake(args); };
  el.querySelector("#ctCancel").onclick = () => Sheet.close();
  el.querySelector("#ctOk").onclick = go;
  el.querySelector(".sform").onsubmit = (e) => { e.preventDefault(); go(); };
};
function changeList(d) {
  const board = window.Studio?.board(), seat = board && board.seats.find((s) => s.seat === d.seat);
  const conf = (d.session_moved && d.session_moved.conflicts) || [];
  return `<ul class="changes">${d.changes.map((c) => `<li class="${seat && !seat.owns.includes(c.kind) ? "out" : ""}"><b>${esc(c.where)}</b> ${esc(c.what)}</li>`).join("") || `<li>No changes yet.</li>`}
    ${conf.map((c) => `<li class="conf">conflict · ${esc(c.where)}: ${esc(c.why || "")}</li>`).join("")}</ul>
    ${d.session_moved ? `<p class="hint" style="margin-top:10px">The session has moved on since this take was cut (${d.session_moved.changes} change${d.session_moved.changes === 1 ? "" : "s"}); ${conf.length ? `${conf.length} of them collide with this take.` : "they merge cleanly."}</p>` : ""}`;
}
$("#takeChanges").onclick = async () => {
  const id = S.take; if (!id) return;
  try {
    const d = await api("take_diff", { take: id });
    modal(`<h2>${esc(id)} · what it changes</h2>${changeList(d)}<div class="row"><button class="ghost" id="tcClose">Close</button></div>`);
    $("#tcClose").onclick = closeModal;
  } catch (e) { toast(e.message); }
};
$("#takeSync").onclick = () => syncTake("fail");
async function syncTake(on_conflict) {
  const id = S.take; if (!id) return;
  try {
    status("bringing in the session's changes…", "busy");
    const r = await api("take_sync", { take: id, on_conflict }); status("");
    if (!r.synced) {
      const box = modal(`<h2>${esc(id)} · the session changed the same things</h2>
        <p class="hint">Nothing was changed. Choose which version the take keeps where they collide; everything else comes in either way.</p>
        <ul class="changes">${r.conflicts.map((c) => `<li class="conf">${esc(c.where)}: ${esc(c.why || "")}</li>`).join("")}</ul>
        <div class="row"><button class="ghost" id="syCancel">Cancel</button><button class="key" data-w="session">TAKE THE SESSION'S</button><button class="key go" data-w="mine">KEEP MINE</button></div>`);
      $("#syCancel").onclick = closeModal;
      box.querySelectorAll("[data-w]").forEach((b) => b.onclick = () => { closeModal(); syncTake(b.dataset.w); });
      return;
    }
    await loadState();
    toast(r.brought_in && r.brought_in.length ? `${id} now has the session's ${r.brought_in.length} change${r.brought_in.length === 1 ? "" : "s"}.` : `${id} is up to date.`, true);
  } catch (e) { status(""); toast(e.message); }
}
$("#takeSubmit").onclick = async () => {
  const id = S.take, m = S.takeMeta; if (!id || !m) return;
  if (S.pendingSave) { toast("Still saving your last edit: try again in a moment."); return; }
  let d; try { d = await api("take_diff", { take: id }); } catch (e) { toast(e.message); return; }
  modal(`<h2>Hand in ${esc(id)}</h2>
    <p class="hint">It goes to whoever accepts takes (the Producer, or you on Needs You and the seat's TAKES tab). Say what you made and what to listen for.</p>
    ${changeList(d)}
    <textarea id="hiText" rows="3" placeholder="What you made and anything the listener should check">${esc(m.summary || "")}</textarea>
    <div class="row"><button class="ghost" id="hiCancel">Cancel</button><button class="key go" id="hiOk">HAND IT IN</button></div>`);
  $("#hiText").focus();
  $("#hiCancel").onclick = closeModal;
  $("#hiOk").onclick = async () => {
    const summary = $("#hiText").value.trim(); if (!summary) { toast("Say what you made, so whoever auditions it knows what to listen for"); return; }
    closeModal();
    try {
      await api("take_submit", { take: id, summary });
      await openTake(null, false);
      toast(`${id} handed in. The desk is back on the session.`, true);
    } catch (e) { toast(e.message); }
  };
};

// Show the desk on one of its panels with a track in front (a seat's bench sends you here).
function deskShow(tab, trackId) {
  const t = trackId && S.doc && S.doc.tracks.find((x) => x.id === trackId);
  if (t) { S.sel.track = t.id; if (tab === "roll" && t.clips.length) { S.sel.clip = t.clips[0].id; S.sel.notes.clear(); } }
  const b = document.querySelector(`#tabs button[data-tab="${tab}"]`); if (b) b.click();
  renderAll(); if (tab === "roll") focusRoll();
}
function renderAll() {
  const d = S.doc;
  $("#projName").textContent = d ? d.name + (S.takeMeta ? " › " + S.takeMeta.id : "") : "No project";
  renderTakeBar();
  if (d) {
    if (document.activeElement !== $("#tempo")) $("#tempo").value = d.tempo;
    if (document.activeElement !== $("#key")) $("#key").value = d.key;
    if (document.activeElement !== $("#sig")) $("#sig").value = d.time_sig.join("/");
  }
  $("#loopBtn").classList.toggle("on", S.loopOn);
  audioTag();
  window.TTMix?.refresh();  // the MIX room or the Desk's Mixer tab, when one shows
  if (document.body.dataset.room !== "desk") return;  // canvases have no size while the desk is hidden
  drawHeaders(); drawArrangement(); drawRoll(); renderPanel();
}
// Preview playback is a render of the project: say whether what you hear is the current revision. LIVE says what
// the real-time engine is doing instead.
const TAG_TITLE = $("#audioState").title;
function audioTag() {
  const el = $("#audioState"), a = S.audio;
  const what = S.take ? S.take.toUpperCase() + " · " : "";  // playback is of what the desk shows
  if (liveMode()) return liveTag(el, what);
  el.title = TAG_TITLE;
  if (!S.doc || !a.buf) { el.textContent = what + "NO RENDER"; el.className = "tag"; }
  else if (a.version === S.version) { el.textContent = `${what}RENDER = REV ${S.doc.rev ?? "?"}`; el.className = "tag ok"; }
  else { el.textContent = `${what}RENDER REV ${a.rev ?? "?"} · NOW ${S.doc.rev ?? "?"}`; el.className = "tag stale"; }
}

// ---------------------------------------------------------------- top bar
$("#tempo").addEventListener("change", (e) => act("project_set", { tempo: +e.target.value }));
$("#key").addEventListener("change", (e) => act("project_set", { key: e.target.value }));
$("#sig").addEventListener("change", (e) => act("project_set", { time_sig: e.target.value }));
$("#snap").addEventListener("change", (e) => { S.snap = +e.target.value; });
$("#undoBtn").onclick = () => act("undo");
$("#redoBtn").onclick = () => act("redo");
$("#loopBtn").onclick = () => {
  S.loopOn = !S.loopOn;
  if (liveMode() && S.eng.st.on) api("live_loop", { on: S.loopOn && !!(S.doc && S.doc.loop) }).then(liveState, (e) => toast(e.message));
  renderAll();
};
$("#liveBtn").onclick = () => {
  const L = S.eng, on = !L.on;
  if (on) stop(false);  // still preview mode here: stops a playing preview
  else if (L.st && L.st.on) { L.st.playing = false; api("live_off").then(liveState, (e) => toast(e.message)); }
  L.on = on; try { localStorage.setItem("tt.live", on ? "1" : "0"); } catch { /* no storage */ }
  $("#playBtn").classList.remove("on"); $("#liveBtn").classList.toggle("on", on); renderAll();
};
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
  if (S.tab === "mixer") window.TTMix?.show();
  renderPanel(); drawRoll();
});

// ---------------------------------------------------------------- track headers
function drawHeaders() {
  const box = $("#trackHeaders"); box.innerHTML = "";
  if (!S.doc) return;
  const lay = laneLayout(), L = laneH();
  S.doc.tracks.forEach((t, i) => {
    const el = document.createElement("div");
    el.className = "th" + (S.sel.track === t.id ? " sel" : "") + (lay[i].h !== L ? " tall" : "");
    if (lay[i].h !== L) el.style.height = lay[i].h + "px";  // a lane grown for its passes
    const inst = t.kind === "midi" ? (t.instrument ? instLabel(t.instrument) : "no instrument") : t.kind;
    el.innerHTML = `<div class="bar" style="background:${trackColor(t, i)}"></div>
      <div class="name" title="${esc(t.name)}">${esc(t.name)}</div>
      <div class="btns"><button class="m ${t.mute ? "on" : ""}" title="Mute">M</button><button class="s ${t.solo ? "on" : ""}" title="Solo">S</button>${window.Tracking?.headerButtons(t) || ""}</div>
      <div class="inst" title="${esc(inst)}">${window.Tracking?.headerInst(t) || esc(inst) + (t.role ? " · " + esc(t.role) : "")}</div>
      <div class="vol"><input type="range" min="-60" max="12" step="0.5" value="${t.volume_db}"><span>${(+t.volume_db).toFixed(1)}</span></div>`;
    el.onclick = (e) => { if (e.target.tagName === "INPUT" || e.target.tagName === "BUTTON") return; selectTrack(t.id); };
    el.ondblclick = (e) => { if (e.target.classList.contains("name")) renameTrack(t); };
    el.oncontextmenu = (e) => { e.preventDefault(); trackMenu(e, t); };
    el.querySelector(".m").onclick = () => act("track_update", { track: t.id, mute: !t.mute });
    el.querySelector(".s").onclick = () => act("track_update", { track: t.id, solo: !t.solo });
    const vol = el.querySelector("input");
    if (t.automation?.volume_db?.length) vol.title = AUTO_TIP;
    vol.oninput = () => { el.querySelector(".vol span").textContent = (+vol.value).toFixed(1); liveMix(t.id, { db: +vol.value }); };
    vol.onchange = () => act("track_update", { track: t.id, volume_db: +vol.value });
    vol.ondblclick = () => { liveMix(t.id, { db: 0 }); act("track_update", { track: t.id, volume_db: 0 }); };
    window.Tracking?.bindHeader(el, t);
    window.Automation?.bindHeader(el, t);
    box.appendChild(el);
    if (lay[i].a) box.appendChild(Automation.header(t, lay[i]));  // the names of its open automation lanes
  });
  window.Tracking?.meters();
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
// Where each track's lane sits ({y, h} per track; .total is the height of them all). A lane is laneH() tall and
// grows for a track with more than 2 recording passes (about PASS_ROW px a row), so its pass rows stay readable.
// a: the height of the track's open automation lanes, under its lane (automation.js); the next track starts below.
const PASS_ROW = 18;
function laneLayout() {
  const L = laneH(); let y = 0;
  const out = (S.doc ? S.doc.tracks : []).map((t) => {
    const p = passRows(t), h = p && p.length > 2 ? Math.max(L, p.length * PASS_ROW + 6) : L;
    const r = { y, h, a: window.Automation?.height(t) || 0 };
    y += h + r.a; return r;
  });
  out.total = y; return out;
}
function fitCanvas(cv, w, h) {
  const r = devicePixelRatio || 1;
  if (cv.width !== Math.round(w * r) || cv.height !== Math.round(h * r)) { cv.width = Math.round(w * r); cv.height = Math.round(h * r); }
  cv.style.width = w + "px"; cv.style.height = h + "px";
  const g = cv.getContext("2d"); g.setTransform(r, 0, 0, r, 0, 0); return g;
}
function drawArrangement() {
  const g0 = ruler.getContext("2d");
  if (!S.doc) { g0.clearRect(0, 0, ruler.width, ruler.height); fitCanvas(lanes, 10, 10); return; }
  const lay = laneLayout(), endB = songEnd() + bpb() * 8;
  const W = Math.max(lanesWrap.clientWidth, endB * S.zoom), H = Math.max(lanesWrap.clientHeight - 2, lay.total);
  const g = fitCanvas(lanes, W, H);
  g.fillStyle = CV.bg; g.fillRect(0, 0, W, H);
  const barW = bpb() * S.zoom;
  for (let b = 0, ph = 0; b <= endB; b += bpb() * 4, ph++) if (ph % 2) { g.fillStyle = CV.band; g.fillRect(b * S.zoom, 0, barW * 4, H); }  // every other 4-bar phrase
  if (S.doc.loop) { g.fillStyle = CV.loop; g.fillRect(S.doc.loop.start * S.zoom, 0, (S.doc.loop.end - S.doc.loop.start) * S.zoom, H); }
  if (S.zoom >= 12) for (let b = 0; b <= endB; b += 1) { g.fillStyle = CV.beat; g.fillRect(Math.round(b * S.zoom), 0, 1, H); }
  for (let b = 0, bar = 0; b <= endB; b += bpb(), bar++) {
    g.fillStyle = bar % 4 ? CV.bar : CV.phrase; g.fillRect(Math.round(b * S.zoom), 0, 1, H);
  }
  S.doc.tracks.forEach((t, i) => {
    const { y, h: L } = lay[i];
    if (S.sel.track === t.id) { g.fillStyle = CV.sel; g.fillRect(0, y, W, L); }
    g.fillStyle = CV.lane; g.fillRect(0, y + L - 1, W, 1); g.fillStyle = CV.laneHi; g.fillRect(0, y + L, W, 1);
    const col = trackColor(t, i);
    for (const c of t.clips) drawClip(g, t, c, y, L, col);
  });
  window.Automation?.draw(g, lay, W);
  if (S.drag && S.drag.kind === "clip" && S.drag.ghost) {
    const d = S.drag.ghost, r = lay[d.row] || { y: d.row * laneH(), h: laneH() }; g.strokeStyle = "#fff"; g.setLineDash([4, 3]);
    g.strokeRect(d.start * S.zoom + 0.5, r.y + 3.5, d.length * S.zoom - 1, r.h - 8); g.setLineDash([]);
  }
  for (const m of S.doc.markers) { g.fillStyle = "rgba(255,178,36,.38)"; g.fillRect(Math.round(m.at * S.zoom), 0, 1, H); }
  window.Picture?.overlay(g, H);
  window.Tracking?.overlay(g, lay);
  const phx = Math.round(S.playhead * S.zoom);
  g.fillStyle = "rgba(255,85,69,.18)"; g.fillRect(phx - 1.5, 0, 4, H); g.fillStyle = CV.play; g.fillRect(phx, 0, 1.25, H);
  drawRuler();
}
// Recording passes share a stretch of time (tracking.js): a track that has them shows each pass on a row of its
// own inside its lane, the heard one bright and the others dim.
function passRows(t) {
  const ls = [...new Set(t.clips.map((c) => c.lane || 0))].sort((a, b) => a - b);
  return ls.length > 1 ? ls : null;
}
function clipBox(t, lane, y, L) {
  const rows = passRows(t);
  if (!rows) return [y + 3, L - 8];
  const h = (L - 6) / rows.length;
  return [y + 3 + Math.max(0, rows.indexOf(lane || 0)) * h, h - 1];
}
function drawClip(g, t, c, y, L, col) {
  const x = c.start * S.zoom, w = Math.max(3, c.length * S.zoom), sel = S.sel.clip === c.id;
  const [cy, ch] = clipBox(t, c.lane, y, L), band = ch >= 34 ? 15 : 0;  // a pass row has no room for a title band
  const R = Math.min(4, w / 3, ch / 3);
  g.globalAlpha = c.muted ? 0.35 : 1;
  rrect(g, x + 1, cy, w - 2, ch, R); g.fillStyle = "#0b0c0f"; g.fill(); g.fillStyle = col + "26"; g.fill();
  if (band) { rrect(g, x + 1, cy, w - 2, band, [R, R, 0, 0]); g.fillStyle = col + "d9"; g.fill(); }
  else { g.fillStyle = col; g.fillRect(x + 1 + R, cy, Math.max(0, w - 2 - 2 * R), 2); }
  rrect(g, x + 1.5, cy + 0.5, w - 3, ch - 1, R); g.strokeStyle = sel ? "#ffffff" : col + "b3"; g.lineWidth = sel ? 1.5 : 1; g.stroke(); g.lineWidth = 1;
  g.save(); g.beginPath(); g.rect(x + 2, cy, w - 4, ch); g.clip();
  if (band) {
    g.fillStyle = "rgba(6,7,9,.92)"; g.font = `600 10.5px ${UI_FONT}`;
    g.fillText((c.name || (c.kind === "audio" ? (c.file || "").split("/").pop() : ""))
      + (c.bpm ? ` · ${+c.bpm.toFixed(2)} bpm` : "") + (c.pitch ? ` · ${c.pitch > 0 ? "+" : ""}${+c.pitch.toFixed(2)} st` : ""), x + 6, cy + 11);
  }
  const top = cy + (band ? band + 2 : 3), h = Math.max(2, ch - (band ? band + 4 : 5));
  if (c.kind === "midi" && c.notes.length) {
    let lo = 127, hi = 0; for (const n of c.notes) { lo = Math.min(lo, n[0]); hi = Math.max(hi, n[0]); }
    const span = Math.max(12, hi - lo + 2);
    g.fillStyle = col;
    for (const n of c.notes) {
      if (n[1] >= c.length) continue;
      const ny = top + (hi + 1 - n[0]) / span * h;
      g.fillRect(x + n[1] * S.zoom, ny, Math.max(1.5, Math.min(n[2], c.length - n[1]) * S.zoom - 0.5), Math.max(1.5, h / span - 0.5));
    }
  } else if (c.kind === "audio" && !window.Tracking?.wave(g, c, x, w, top, h, col)) {
    // a stand-in for the waveform until the file's peaks arrive (or when it has none), taken from where in the
    // file the clip plays so a cut does not redraw it
    const o = (c.offset || 0) * 40;
    g.fillStyle = col + "99"; for (let i = 0; i < w; i += 3) { const a = (Math.sin((i / S.zoom * 9 + o) * 0.37) * 0.5 + 0.5) * h; g.fillRect(x + i, top + (h - a) / 2, 2, a); }
  }
  if (!band && w > 26) { g.fillStyle = c.muted ? col : "#ffffff"; g.font = `9px ${UI_FONT}`; g.fillText("P" + (c.lane || 0), x + 4, cy + Math.min(ch - 2, 10)); }
  g.restore(); g.globalAlpha = 1;
}
function drawRuler() {
  const W = lanesWrap.clientWidth, H = 30, g = fitCanvas(ruler, W, H), sx = lanesWrap.scrollLeft;
  g.fillStyle = CV.ruler; g.fillRect(0, 0, W, H);
  if (!S.doc) return;
  if (S.doc.loop) {  // the loop region: a bar along the top, lit while the loop is on
    const lx = S.doc.loop.start * S.zoom - sx, lw = (S.doc.loop.end - S.doc.loop.start) * S.zoom;
    rrect(g, lx, 0.5, lw, 4, 2); g.fillStyle = S.loopOn ? "rgba(79,220,132,.8)" : "rgba(79,220,132,.25)"; g.fill();
  }
  const barPx = bpb() * S.zoom, every = Math.max(1, Math.round(44 / barPx));
  g.font = `10px ${MONO}`; g.textBaseline = "alphabetic";
  for (let bar = 0; bar * barPx < sx + W; bar++) {
    const x = Math.round(bar * barPx - sx); if (x < -60) continue;
    if (barPx >= 48) for (let k = 1; k < bpb(); k++) { g.fillStyle = CV.tickMinor; g.fillRect(x + Math.round(k * S.zoom), H - 4, 1, 4); }
    const major = bar % every === 0;
    g.fillStyle = major ? CV.tick : CV.tickMinor; g.fillRect(x, major ? H - 11 : H - 6, 1, major ? 11 : 6);
    if (major) { g.fillStyle = bar % 4 ? CV.dim : CV.rulerTxt; g.fillText(bar + 1, x + 4, H - 3); }
  }
  // section markers: a flag with the name, in the display font
  const legend = getComputedStyle(document.documentElement).getPropertyValue("--legend");
  g.font = `600 9.5px ${legend}`; g.textBaseline = "middle";
  for (const m of S.doc.markers) {
    const x = Math.round(m.at * S.zoom - sx), label = String(m.name).toUpperCase(), tw = g.measureText(label).width;
    if (x + tw + 16 < 0 || x > W) continue;
    rrect(g, x, 5, tw + 12, 12, [0, 3, 3, 0]); g.fillStyle = "rgba(255,178,36,.17)"; g.fill();
    g.fillStyle = CV.marker; g.fillRect(x, 5, 1.5, H - 5); g.fillText(label, x + 6, 11.5);
  }
  g.textBaseline = "alphabetic";
  // tempo changes (a fit to picture hits makes many): a cyan tick at each, its bpm along the bottom where it fits
  const tempos = tempoChanges();
  if (tempos.length > 1) {
    g.font = "9px Cascadia Mono, Consolas, ui-monospace, monospace"; let room = -1e9;
    for (const [b, bpm] of tempos) {
      const x = b * S.zoom - sx; if (x > W) break;
      g.fillStyle = "rgba(92,200,255,.8)"; g.fillRect(Math.round(x), H - 9, 1, 9);
      const label = String(+bpm.toFixed(1)), w = g.measureText(label).width + 6;
      if (x >= room && x + w > 0) { g.fillStyle = "#5cc8ff"; g.fillText(label, x + 3, H - 1); room = x + w; }
    }
  }
  const px = Math.round(S.playhead * S.zoom - sx); g.fillStyle = CV.play;
  g.beginPath(); g.moveTo(px - 5.5, H - 10); g.lineTo(px + 6.5, H - 10); g.lineTo(px + 6.5, H - 6); g.lineTo(px + 0.5, H); g.lineTo(px - 5.5, H - 6); g.closePath(); g.fill();
  drawChords(); window.Picture?.draw(); window.Picture?.sync();
}

// ---------------------------------------------------------------- chord track
// One lane under the ruler. Double-click a bar to write its chord (symbols or roman numerals in the key; several
// in one slot share it), Tab moves on to the next slot, drag a chord's right edge to make it longer or shorter.
// Every change is one harmony() call, so it lands in the take when the desk has one open.
const chordCv = $("#chords"), chordEdit = $("#chordEdit");
let chordEditing = null;  // the slot the box is open on ({chord, at, end}); not kept on the element, where "slot" is the browser's
const chordList = () => [...(S.doc.chord_track || [])].sort((a, b) => a.at - b.at);
function drawChords() {
  const W = lanesWrap.clientWidth, H = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--chordH")) - 1;
  const g = fitCanvas(chordCv, W, H), sx = lanesWrap.scrollLeft;
  g.fillStyle = "#0a0d0d"; g.fillRect(0, 0, W, H);
  if (!S.doc) return;
  for (let bar = Math.floor(sx / (bpb() * S.zoom)); bar * bpb() * S.zoom < sx + W; bar++) {
    g.fillStyle = bar % 4 ? "#121817" : "#1a2322"; g.fillRect(Math.round(bar * bpb() * S.zoom - sx), 0, 1, H);
  }
  const list = chordList(), d = S.drag && S.drag.kind === "chord" ? S.drag : null;
  g.font = `11.5px ${MONO}`; g.textBaseline = "middle";
  if (!list.length) { g.fillStyle = "#3f5a55"; g.fillText("No chords yet: double-click a bar to write its chord, or WRITE… for a whole progression", 10, H / 2 + 1); }
  for (const c of list) {
    const len = d && d.chord === c ? d.end - c.at : c.len, x = c.at * S.zoom - sx, w = len * S.zoom;
    if (x + w < 0 || x > W) continue;
    rrect(g, x + 1.5, 3, Math.max(2, w - 3), H - 6, 3); g.fillStyle = CV.vfdBg; g.fill(); g.strokeStyle = "rgba(124,240,214,.16)"; g.stroke();
    g.fillStyle = c.key ? CV.marker : CV.vfd; g.fillRect(x + 1.5, 5, 2, H - 10);  // amber edge: this chord is in another key
    g.save(); g.beginPath(); g.rect(x + 3, 0, Math.max(0, w - 5), H); g.clip();
    g.fillStyle = CV.vfd; g.fillText(c.chord + (c.key ? "  (" + c.key + ")" : ""), x + 8, H / 2 + 1);
    g.restore();
  }
  g.fillStyle = CV.play; g.fillRect(Math.round(S.playhead * S.zoom - sx), 0, 1.25, H);
}
// What is under the pointer: a chord, or the empty slot there (the bar, cut short by its neighbours).
function chordHit(e) {
  const r = chordCv.getBoundingClientRect(), beat = Math.max(0, (e.clientX - r.left + lanesWrap.scrollLeft) / S.zoom);
  return chordSlot(beat, (e.clientX - r.left + lanesWrap.scrollLeft));
}
function chordSlot(beat, x = null) {
  const list = chordList(), c = list.find((k) => beat >= k.at - 1e-9 && beat < k.at + k.len - 1e-9);
  if (c) return { chord: c, at: c.at, end: c.at + c.len, edge: x !== null && (c.at + c.len) * S.zoom - x < 7 };
  let at = Math.floor(beat / bpb() + 1e-9) * bpb(), end = at + bpb();
  for (const k of list) { if (k.at + k.len <= beat + 1e-9) at = Math.max(at, k.at + k.len); else if (k.at > beat) { end = Math.min(end, k.at); break; } }
  return { chord: null, at, end, edge: false };
}
function editChord(slot) {
  const sx = lanesWrap.scrollLeft;
  if (slot.at * S.zoom < sx || slot.at * S.zoom > sx + lanesWrap.clientWidth - 90) { lanesWrap.scrollLeft = Math.max(0, slot.at * S.zoom - 120); drawRuler(); }
  chordEditing = slot; chordEdit.value = slot.chord ? slot.chord.chord : ""; chordEdit.classList.remove("hidden", "bad");
  chordEdit.style.left = slot.at * S.zoom - lanesWrap.scrollLeft + "px"; chordEdit.style.width = Math.max(84, (slot.end - slot.at) * S.zoom) + "px";
  chordEdit.placeholder = "Am7 · iv · F/A"; chordEdit.title = `Chord for ${fmtPos(slot.at)} to ${fmtPos(slot.end)}. Enter sets it, Tab sets it and moves on, empty clears it, Esc leaves it.`;
  chordEdit.focus(); chordEdit.select();
}
function closeChordEdit() { chordEdit.classList.add("hidden"); chordEditing = null; }
// Returns false when the server refused the text (a chord it cannot read): the box stays open and says why.
async function commitChord() {
  const slot = chordEditing, text = chordEdit.value.trim();
  if (!slot) return true;
  if (text === (slot.chord ? slot.chord.chord : "")) { closeChordEdit(); return true; }
  const args = { at: slot.at, end: slot.end };
  if (!text) args.clear = true;
  else {
    args.progression = text;
    if (!text.includes(":")) args.chord_length = (slot.end - slot.at) / text.split(/\s+/).length;  // several chords share the slot
    if (slot.chord && slot.chord.key) args.key = slot.chord.key;
  }
  try { await act("harmony", args); closeChordEdit(); return true; }
  catch { chordEdit.classList.add("bad"); chordEdit.focus(); return false; }
}
chordEdit.addEventListener("keydown", async (e) => {
  e.stopPropagation();  // the desk's own keys (space, delete, undo) are not for a text box
  if (e.key === "Escape") { closeChordEdit(); return; }
  if (e.key !== "Enter" && e.key !== "Tab") { chordEdit.classList.remove("bad"); return; }
  e.preventDefault();
  const slot = chordEditing, next = e.key === "Tab" && slot ? (e.shiftKey ? slot.at - 1e-3 : slot.end) : null;
  if (await commitChord() && next !== null && next >= 0) editChord(chordSlot(next));
});
chordEdit.addEventListener("blur", closeChordEdit);
chordCv.addEventListener("dblclick", (e) => { if (S.doc) editChord(chordHit(e)); });
chordCv.addEventListener("mousemove", (e) => { if (S.doc && !S.drag) chordCv.style.cursor = chordHit(e).edge ? "ew-resize" : "text"; });
chordCv.addEventListener("mousedown", (e) => {
  if (!S.doc || e.button !== 0) return;
  const h = chordHit(e);
  if (h.edge) { e.preventDefault(); S.drag = { kind: "chord", chord: h.chord, end: h.end, moved: false }; }
});
function chordDrag(e) {
  const d = S.drag, r = chordCv.getBoundingClientRect(), b = (e.clientX - r.left + lanesWrap.scrollLeft) / S.zoom;
  const end = Math.max(d.chord.at + (S.snap || 0.25), snapB(b, S.snap || 0.25));
  if (end !== d.end) { d.end = end; d.moved = true; drawChords(); }
}
async function chordDrop(d) {
  const c = d.chord, was = c.at + c.len;
  if (!d.moved || Math.abs(d.end - was) < 1e-6) { drawChords(); return; }
  // longer: the chord is written again over the new range (what it covers is replaced); shorter: its tail is cleared
  if (d.end > was) await act("harmony", { progression: c.chord, at: c.at, end: d.end, chord_length: d.end - c.at, ...(c.key ? { key: c.key } : {}) });
  else await act("harmony", { clear: true, at: d.end, end: was });
}
chordCv.addEventListener("contextmenu", (e) => {
  e.preventDefault(); if (!S.doc) return;
  const h = chordHit(e);
  menu(e, [
    [h.chord ? `Change ${h.chord.chord}…` : `Write a chord at ${fmtPos(h.at)}…`, () => editChord(h)],
    ...(h.chord ? [[`Clear ${h.chord.chord}`, () => act("harmony", { clear: true, at: h.at, end: h.end })]] : []),
    ["Write a progression…", () => openProgression(h.at)],
    ...(chordList().length ? [["Clear the whole chord track", () => confirm("Clear every chord? The parts stay as they are.") && act("harmony", { clear: true })]] : []),
  ]);
});
$("#chordSet").onclick = () => S.doc && openProgression(floorB(S.playhead, bpb()));
function openProgression(at) {
  const barOf = (b) => Math.floor(b / bpb() + 1e-9) + 1, secs = [...S.doc.markers].sort((a, b) => a.at - b.at), ranges = [];
  secs.forEach((m, i) => ranges.push([`Section ${m.name} (from bar ${barOf(m.at)}${secs[i + 1] ? " to " + (barOf(secs[i + 1].at) - 1) : ""})`, { section: m.name }]));
  if (S.doc.loop) ranges.push([`The loop (bars ${barOf(S.doc.loop.start)} to ${barOf(S.doc.loop.end) - 1})`, { at: S.doc.loop.start, end: S.doc.loop.end }]);
  ranges.push([`From bar ${barOf(at)}, for as long as it is written`, { at }]);
  const box = modal(`<h2>Chord track</h2>
    <p class="hint">The harmony the parts are checked against, and what chords() and pattern() follow. One chord per bar unless you
      give a length in beats (<code>Dm:2 G7:2</code>). Chord symbols or roman numerals in the key, <code>%</code> repeats the last
      chord, <code>N.C.</code> leaves a gap. Over a section or the loop, the progression repeats until it is full.</p>
    <div class="grid2">
      <span>Where</span><select id="cpRange">${ranges.map((r, i) => `<option value="${i}">${esc(r[0])}</option>`).join("")}</select>
      <span>Progression</span><input id="cpProg" spellcheck="false" autocomplete="off" placeholder="i VI III VII   ·   Am7 F/A G   ·   i:2 iv:2 V7:4">
      <span>Key here</span><input id="cpKey" spellcheck="false" placeholder="${esc(S.doc.key)} (the project key) - fill in for a modulation">
    </div>
    <p class="hint">Notes already written do not move when the chords change: score() and the Render report show what no longer fits.</p>
    <div class="row"><button id="cpCancel" class="ghost">Close</button><button id="cpClear" class="ghost danger">Clear this range</button><button id="cpSet">Set</button></div>`);
  const args = () => ({ ...ranges[+$("#cpRange").value][1] });
  const done = (r) => { closeModal(); const said = [r.repeated && "repeated " + r.repeated, ...(r.warnings || [])].filter(Boolean).join(" · "); if (said) toast(said, true); };
  $("#cpCancel").onclick = closeModal;
  $("#cpClear").onclick = async () => { const a = args(); if (a.end === undefined && !a.section) a.end = songEnd() + bpb(); try { done(await act("harmony", { clear: true, ...a })); } catch { /* shown */ } };
  $("#cpSet").onclick = async () => {
    const prog = $("#cpProg").value.trim(), key = $("#cpKey").value.trim();
    if (!prog) { $("#cpProg").focus(); return; }
    try { done(await act("harmony", { progression: prog, ...args(), ...(key ? { key } : {}) })); } catch { /* the toast says what was wrong */ }
  };
  $("#cpProg").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#cpSet").click(); });
  $("#cpProg").focus();
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
  const r = lanes.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top, lay = laneLayout();
  let row = y < 0 ? -1 : lay.length + Math.floor((y - lay.total) / laneH());  // under the last lane: past the tracks
  for (let i = 0; i < lay.length; i++) if (y >= lay[i].y && y < lay[i].y + lay[i].h + lay[i].a) { row = i; break; }
  const t = S.doc && S.doc.tracks[row], beat = x / S.zoom;
  let clip = null, edge = false;
  if (t) for (const c of t.clips) {
    if (beat < c.start || beat > c.start + c.length) continue;
    const [cy, ch] = clipBox(t, c.lane, lay[row].y, lay[row].h);
    if (passRows(t) && (y < cy || y > cy + ch)) continue;  // passes overlap in time: the row under the pointer
    clip = c; edge = (c.start + c.length) * S.zoom - x < 7;
  }
  return { row, t, beat, clip, edge, x, y };
}
lanes.addEventListener("mousedown", (e) => {
  if (window.Automation?.mouse(e)) return;  // on an automation lane
  if (!S.doc || e.button !== 0) return;
  const h = laneHit(e);
  if (!h.t) return;
  S.sel.track = h.t.id;
  if (!h.clip) { S.sel.clip = null; renderAll(); return; }
  if (e.ctrlKey && passRows(h.t) && window.Tracking) { S.sel.clip = h.clip.id; Tracking.compStart(e, h); return; }  // comp: Ctrl+drag along a pass
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
  else if (S.drag.kind === "comp") Tracking.compDrag(e);
  else if (S.drag.kind === "chord") chordDrag(e);
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
  } else if (d.kind === "chord") await chordDrop(d);
  else if (d.kind === "comp") await Tracking.compDrop(d);
  else rollUp(e);
});
lanes.addEventListener("dblclick", async (e) => {
  if (window.Automation?.mouse(e)) return;
  const h = laneHit(e);
  if (!h.t) return;
  if (h.clip) { focusRoll(); return; }
  if (h.t.kind !== "midi") return;
  const r = await act("clip_create", { track: h.t.id, start: floorB(h.beat, bpb()), length: bpb() });
  S.sel.clip = r.clip; focusRoll(); renderAll();
});
lanes.addEventListener("contextmenu", (e) => {
  if (window.Automation?.mouse(e)) return;
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
    ...(window.Tracking?.clipMenu(h.t, c) || []),
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
// [[beat, bpm], ...] from the start, each a real change (the first is the song's tempo)
function tempoChanges() {
  const out = [];
  for (const p of tempoPts()) if (!out.length || Math.abs(p.bpm - out[out.length - 1][1]) > 1e-6) out.push([p.b, p.bpm]);
  return out;
}
ruler.addEventListener("contextmenu", (e) => {
  e.preventDefault(); const b = snapB((e.offsetX + lanesWrap.scrollLeft) / S.zoom, bpb());
  const changes = S.doc.tempo_changes || [], near = changes.find((c) => Math.abs(c[0] * S.zoom - (e.offsetX + lanesWrap.scrollLeft)) <= 5);
  const tb = snapB((e.offsetX + lanesWrap.scrollLeft) / S.zoom, S.snap || 1);  // a tempo change sits on a beat, not only a bar
  const setChanges = (list) => act("project_set", { tempo_changes: list.sort((x, y) => x[0] - y[0]) });
  menu(e, [
    [`Tempo change at ${fmtPos(tb)}…`, () => {
      const v = prompt(`Tempo from ${fmtPos(tb)} on (bpm)`, String(+tempoAt(tb).toFixed(2))); if (!v) return;
      const bpm = +v; if (!(bpm >= 20 && bpm <= 400)) { toast("A tempo between 20 and 400 bpm"); return; }
      if (tb <= 0) act("project_set", { tempo: bpm }); else setChanges([...changes.filter((c) => Math.abs(c[0] - tb) > 1e-6), [tb, bpm]]);
    }],
    ...(near ? [[`Remove the tempo change at ${fmtPos(near[0])} (${+near[1].toFixed(2)} bpm)`, () => setChanges(changes.filter((c) => c !== near))]] : []),
    ...(changes.length ? [[`Clear all ${changes.length} tempo change${changes.length === 1 ? "" : "s"} (steady ${+S.doc.tempo.toFixed(2)} bpm)`, () => setChanges([])]] : []),
    ["Add marker here…", () => { const n = prompt("Marker name"); if (n) act("markers", { add: [{ at: b, name: n }] }); }],
    ["Set loop start here", () => act("project_set", { loop: { start: b, end: Math.max(b + bpb(), S.doc.loop ? S.doc.loop.end : b + 8 * bpb()) } })],
    ["Set loop end here", () => act("project_set", { loop: { start: S.doc.loop ? Math.min(S.doc.loop.start, b - bpb()) : 0, end: b } })],
    ["Clear loop", () => act("project_set", { loop: "off" })],
    ...S.doc.markers.map((m) => [`Remove marker "${m.name}"`, () => act("markers", { remove: [m.name] })]),
  ]);
});
function setPlayhead(b) {
  S.playhead = Math.max(0, b); $("#pos").textContent = fmtPos(S.playhead);
  if (liveMode()) {
    if (S.eng.st.playing && !S.eng.st.audition) { S.eng.st.beat = S.playhead; S.eng.at = performance.now(); api("live_seek", { beat: S.playhead }).then(liveState, (e) => toast(e.message)); }
  } else if (S.audio.playing) { stop(false); startPlayback(); }
  drawArrangement(); drawRoll();
}

// ---------------------------------------------------------------- piano roll
const roll = $("#roll"), rollWrap = $("#rollWrap"), velCv = $("#vel"), KEYW = 44;
function selClip() { return S.sel.clip ? findClip(S.sel.clip) : [null, null]; }
function focusRoll() {
  if (S.tab !== "roll") document.querySelector('#tabs button[data-tab="roll"]').click();
  const [, c] = selClip();
  drawRoll();
  if (c && c.notes && c.notes.length) {  // an audio clip has none
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
  g.fillStyle = CV.bg; g.fillRect(0, 0, W, 128 * S.rowH);
  if (!c || c.kind !== "midi") {
    info.textContent = "Select a MIDI clip (double-click empty lane space to create one)";
    const cx = sx + rollWrap.clientWidth / 2, cy = rollWrap.scrollTop + rollWrap.clientHeight / 2;
    g.textAlign = "center";
    g.fillStyle = "#c8c5bd"; g.font = `600 13px ${UI_FONT}`; g.fillText("No clip selected", cx, cy - 6);
    g.fillStyle = "#64625c"; g.font = `12px ${UI_FONT}`; g.fillText("Click a clip in the arrangement to edit its notes here, or double-click empty lane space to make one.", cx, cy + 14);
    g.textAlign = "left";
    drawVel(null); return;
  }
  info.textContent = `${t.name} · ${c.name || c.id} · starts ${fmtPos(c.start)} · ${c.notes.length} notes · click: add  drag: move  edge: resize  dbl-click: delete  ctrl+wheel: zoom`;
  for (let p = 0; p < 128; p++) {
    const y = (127 - p) * S.rowH, black = [1, 3, 6, 8, 10].includes(p % 12);
    g.fillStyle = black ? "#0f1114" : "#15181c"; g.fillRect(KEYW, y, W, S.rowH);
    g.fillStyle = p % 12 === 0 ? "#262b32" : "#0b0d0f"; g.fillRect(KEYW, y + S.rowH - 1, W, 1);
  }
  const len = c.length, X = (b) => KEYW + b * S.rollZoom;
  const step = S.snap > 0 && S.snap * S.rollZoom >= 6 ? S.snap : 0.25;
  for (let b = 0; b <= len + bpb(); b += step) {
    const isBar = Math.abs(b / bpb() - Math.round(b / bpb())) < 1e-6, isBeat = Math.abs(b - Math.round(b)) < 1e-6;
    g.fillStyle = isBar ? "#333941" : isBeat ? "#20252b" : "#191c21"; g.fillRect(Math.round(X(b)), 0, 1, 128 * S.rowH);
  }
  g.fillStyle = "rgba(0,0,0,.4)"; g.fillRect(X(len), 0, W, 128 * S.rowH);
  const col = trackColor(t, S.doc.tracks.indexOf(t));
  c.notes.forEach((n, i) => {
    const y = (127 - n[0]) * S.rowH, sel = S.sel.notes.has(i), nw = Math.max(3, n[2] * S.rollZoom - 1);
    rrect(g, X(n[1]) + 0.5, y + 1, nw, S.rowH - 2, Math.min(2.5, (S.rowH - 2) / 2));
    g.fillStyle = sel ? "#ffffff" : col; g.globalAlpha = 0.5 + 0.5 * n[3] / 127; g.fill(); g.globalAlpha = 1;
    g.strokeStyle = sel ? CV.marker : "rgba(0,0,0,.55)"; g.lineWidth = sel ? 1.5 : 1; g.stroke(); g.lineWidth = 1;
    if (!sel && S.rowH >= 9) { g.fillStyle = "rgba(255,255,255,.22)"; g.fillRect(X(n[1]) + 2, y + 2, Math.max(0, nw - 3), 1); }
  });
  if (S.drag && S.drag.kind === "box") {
    const d = S.drag; g.strokeStyle = CV.marker; g.fillStyle = "rgba(255,178,36,.06)"; g.setLineDash([3, 3]);
    g.fillRect(Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.abs(d.x1 - d.x0), Math.abs(d.y1 - d.y0));
    g.strokeRect(Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.abs(d.x1 - d.x0), Math.abs(d.y1 - d.y0)); g.setLineDash([]);
  }
  const ph = S.playhead - c.start; if (ph >= 0 && ph <= len) { g.fillStyle = CV.play; g.fillRect(Math.round(X(ph)), 0, 1.25, 128 * S.rowH); }
  // sticky keyboard: white keys with a shade line, black keys raised, C named
  g.fillStyle = "#0a0b0d"; g.fillRect(sx, 0, KEYW, 128 * S.rowH);
  for (let p = 0; p < 128; p++) {
    const y = (127 - p) * S.rowH, black = [1, 3, 6, 8, 10].includes(p % 12);
    g.fillStyle = black ? "#1d2025" : "#d8d4cb"; g.fillRect(sx, y, black ? KEYW - 12 : KEYW - 1, S.rowH - 0.5);
    if (black) { g.fillStyle = "#30353c"; g.fillRect(sx, y, KEYW - 12, 1); }
    if (p % 12 === 0) { g.fillStyle = "#3a3934"; g.font = `600 8.5px ${UI_FONT}`; g.fillText(noteName(p), sx + KEYW - 22, y + S.rowH - 2.5); }
  }
  g.fillStyle = "#000"; g.fillRect(sx + KEYW - 1, 0, 1, 128 * S.rowH);
  drawVel(c, col);
}
function drawVel(c, col) {
  const W = rollWrap.clientWidth, H = 64, g = fitCanvas(velCv, W, H), sx = rollWrap.scrollLeft;
  g.fillStyle = "#0e1013"; g.fillRect(0, 0, W, H);
  g.fillStyle = "#1a1d22"; g.fillRect(KEYW, Math.round(H / 2), W, 1);
  g.fillStyle = "#64625c"; g.font = `9px ${getComputedStyle(document.documentElement).getPropertyValue("--legend")}`; g.fillText("VELOCITY", 6, 14);
  if (!c) return;
  c.notes.forEach((n, i) => {
    const x = Math.round(KEYW + n[1] * S.rollZoom - sx); if (x < KEYW - 2 || x > W) return;
    const h = (H - 8) * n[3] / 127, sel = S.sel.notes.has(i);
    g.fillStyle = sel ? "#ffffff" : col; g.globalAlpha = 0.5; g.fillRect(x + 1, H - h, 1.5, h); g.globalAlpha = 1;
    g.beginPath(); g.arc(x + 1.75, H - h, 2.5, 0, Math.PI * 2); g.fill();
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
  if (e.code === "Space") { e.preventDefault(); isPlaying() ? stop(false) : play(); return; }
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
  if (liveMode()) return livePlay();
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
  const L = S.eng;
  if (liveMode()) {
    if (L.busy) L.stopAfter = true;  // a live_play still in flight: stop once it lands
    else if (L.st.on && L.st.playing) {
      L.st.playing = false; L.hold = true;
      api("live_stop").then((s) => { L.hold = false; liveState(s); window.Tracking?.stopped(s); }, (e) => { L.hold = false; toast(e.message); });
    }
  }
  if (rewind) setPlayhead(0);
}
function tick() {
  const a = S.audio; if (!a.playing) return;
  let t = a.offset + (a.ctx.currentTime - a.start);
  if (a.src && a.src.loop) { const ls = a.src.loopStart, le = a.src.loopEnd; if (t >= le) t = ls + ((t - ls) % (le - ls)); }
  showPlayhead(secToBeat(t));
  requestAnimationFrame(tick);
}
function showPlayhead(b) {
  S.playhead = b; $("#pos").textContent = fmtPos(b);
  const x = b * S.zoom;
  if (x > lanesWrap.scrollLeft + lanesWrap.clientWidth - 40) lanesWrap.scrollLeft = x - 80;
  drawArrangement(); if (S.tab === "roll") drawRoll();
}

// ---------------------------------------------------------------- transport (LIVE engine)
// LIVE drives the server's real-time engine instead of playing a render; /api/live streams its state.
const liveMode = () => S.eng.on && !!(S.eng.st && S.eng.st.available);
// a gear audition (gear.js) plays on the same engine: it is not the Desk's playback
const isPlaying = () => liveMode() ? !!S.eng.st.playing && !S.eng.st.audition : S.audio.playing;
async function livePlay() {
  const L = S.eng; if (L.busy || !S.doc) return;
  window.Studio?.hush();
  const wasOn = L.st.on; L.busy = true; L.stopAfter = false; L.hold = false; audioTag();
  if (!wasOn) status("starting the live engine…", "busy");
  try {
    const st = await api("live_play", { beat: S.playhead, loop: S.loopOn && !!S.doc.loop, take: S.take || null });
    status(""); L.busy = false; liveState(st);
    const w = st.warnings || []; if (!wasOn && w.length) toast(`${w.length} engine warning(s): ${w[0]}`);
    if (L.stopAfter) { L.stopAfter = false; stop(false); }
  } catch (e) { L.busy = false; toast(e.message); status("live engine failed", "err"); audioTag(); }
}
function liveState(st) {
  if (!st) return;
  const L = S.eng, flip = !!(L.st && L.st.available) !== !!st.available;
  L.st = st; L.at = performance.now();
  // the LIVE key stays where it is; without an engine it is greyed out and says why
  $("#liveBtn").disabled = !st.available; $("#liveBtn").classList.toggle("on", L.on && !!st.available);
  $("#liveBtn").title = st.available ? "LIVE: play through the real-time engine (your plugins, as you edit) instead of a rendered preview"
    : "LIVE is not available: the real-time engine is not built or could not start" + (st.error ? ` (${st.error})` : "");
  if (L.hold) st.playing = false;  // frames sent before our live_stop landed
  if (flip) renderAll();
  window.Tracking?.sync(); window.Picture?.guideSync();
  if (!liveMode()) return;
  $("#playBtn").classList.toggle("on", (!!st.playing && !st.audition) || L.busy);
  audioTag(); window.TTConsole?.meters();
  if (st.playing && !st.audition && !L.raf) L.raf = requestAnimationFrame(liveTick);
}
function liveTick() {
  const L = S.eng, st = L.st; L.raf = 0;
  if (!liveMode() || !st.playing || st.audition || !S.doc) return;
  let b = secToBeat(beatToSec(st.beat || 0) + (performance.now() - L.at) / 1000);
  const lp = S.doc.loop;
  if (st.looping && lp && lp.end > lp.start && b >= lp.end) b = lp.start + ((b - lp.start) % (lp.end - lp.start));
  showPlayhead(Math.max(0, b));  // a count-in runs before the start
  L.raf = requestAnimationFrame(liveTick);
}
function liveTag(el, what) {
  const L = S.eng, st = L.st, d = st.device;
  el.title = (st.on && d ? `${d.name} · ${d.sr} Hz · ${d.block} samples · CPU ${Math.round((st.cpu || 0) * 100)}% · ${st.dropouts || 0} dropout(s)\n` : "")
    + (st.on && d && d.converted ? `The project is ${d.rate} Hz and this output cannot run at that rate: playback is converted to ${d.sr} Hz.\n` : "")
    + (st.error ? st.error + "\n" : "") + "Live is the raw mix: no loudness normalisation or limiter, unlike an export."
    + (st.on ? "\nClick: choose the audio device to play through and record from." : "");
  const cut = (s) => s.length > 40 ? s.slice(0, 39) + "…" : s;
  const [text, cls] = st.loading || L.busy ? ["LIVE · LOADING", "tag stale"] : st.error ? ["LIVE · " + cut(st.error), "tag bad"]
    : st.recording ? [what + "LIVE · REC", "tag bad"] : st.audition ? ["LIVE · GEAR AUDITION", "tag cy"]
    : st.on ? [what + "LIVE · " + (d ? d.type : "?"), "tag cy"] : ["LIVE · READY", "tag"];
  el.textContent = text; el.className = cls + (st.on ? " pick" : "");  // pick: a click opens the device menu (tracking.js)
}
const peakPct = (p) => p > 0 ? Math.max(0, Math.min(100, (20 * Math.log10(p) + 60) / 60 * 100)) : 0;
// A fader, pan or send being dragged on LIVE is heard at once (live_mix: the engine moves it without a reload);
// letting go saves it as before. One call in flight at a time, carrying the latest value of each control.
const LM = { want: new Map(), busy: false };
const AUTO_TIP = "This track's volume is automated: the fader moves the whole curve up or down.";
function liveMix(id, what, take = S.take || null) {
  const st = S.eng.st; if (!liveMode() || !st || !st.on) return;
  LM.want.set(`${take}|${id}|${what.to || ("pan" in what ? "pan" : "db")}`, { take, ch: { id, ...what } });
  if (!LM.busy) liveMixSend();
}
async function liveMixSend() {
  LM.busy = true;
  try {
    while (LM.want.size) {
      const take = LM.want.values().next().value.take, chs = [];
      for (const [k, v] of LM.want) if (v.take === take) { chs.push(v.ch); LM.want.delete(k); }
      await api("live_mix", { changes: chs, take }, null).catch(() => {});  // the drag still saves on release
    }
  } finally { LM.busy = false; }
}
const liveES = new EventSource("/api/live");
liveES.onmessage = (m) => { try { liveState(JSON.parse(m.data)); } catch { /* bad frame */ } };

// ---------------------------------------------------------------- panels
function renderPanel() {
  if (S.tab === "mixer") window.TTMix?.refresh();
  else if (S.tab === "inspector") renderInspector();
  else if (S.tab === "report") renderReport();
  else if (S.tab === "brief") renderBrief();
}
async function openLiveWindow(track, slot) {
  status("opening the plugin window…", "busy");
  try { const r = await api("live_window", { track, slot, take: S.take || null }); status(""); if (r && r.opened === false) toast("The plugin window did not open."); }
  catch (e) { status(""); toast(e.message); }
}
const fxLabel = (f) => (f.patch ? `${f.name} · ${f.patch}` : f.name) +
  (f.sidechain ? ` ← ${(S.doc.tracks.find((x) => x.id === f.sidechain) || { name: "?" }).name}` : "");
const canSidechain = (f) => f.type === "vst3" || (f.type === "builtin" && f.name === "compressor");
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
  const liveOk = !!(S.eng.st && S.eng.st.available);
  // The track's strip on the left, then its signal chain left to right: the instrument, each insert, an empty slot.
  const color = PALETTE[S.doc.tracks.indexOf(t) % PALETTE.length];
  const TYPE = { synth: "BUILT-IN SYNTH", vst3: "VST3", sfz: "SFZ", builtin: "BUILT-IN", patch: "SAVED SOUND" };
  box.innerHTML = `<div class="insp">
  <aside class="inspStrip" style="--tc:${color}">
    <div class="inspName"><b>${esc(t.name)}</b><span class="legend">${t.kind === "midi" ? "INSTRUMENT" : t.kind.toUpperCase()} TRACK</span></div>
    <label class="fld"><span>Role</span><input id="roleIn" value="${esc(t.role || "")}" placeholder="violin, bass, pad…"></label>
    <label class="fld"><span>Layer</span><input id="layerIn" value="${esc(t.layer || "")}" placeholder="base, combat…"></label>
    <label class="fld"><span>Output</span><select id="outSel"><option value="master">Master</option>${outs.map((b) => `<option value="${b.id}" ${t.output === b.id ? "selected" : ""}>${esc(b.name)}</option>`).join("")}</select></label>
  </aside>
  <div class="chain">
    ${t.kind === "midi" ? `<section class="dev inst ${inst ? "" : "empty"}">
      <header><span class="lamp ${inst ? "green" : ""}"></span><div class="dvT"><span class="legend">${inst ? TYPE[inst.patch ? "patch" : inst.type] || esc(inst.type) : "NO INSTRUMENT"}</span>
        <b title="${inst ? esc(instLabel(inst)) : ""}">${inst ? esc(instLabel(inst)) : "Empty"}</b></div></header>
      ${synthDef ? `<p class="dvD">${esc(synthDef.description)}</p>` : ""}
      <div class="dvK"><button class="key" id="chooseInst">${inst ? "CHANGE" : "CHOOSE"}</button><button class="key" id="audBtn" ${inst ? "" : "disabled"}>▶ HEAR</button>
        ${inst && inst.type === "vst3" ? `<button class="key" id="capBtn" title="Opens the plugin window on this computer: pick a sound, close the window, and it is saved as a sound you can reuse">PICK SOUND</button>` : ""}
        ${inst && inst.type === "vst3" && liveOk ? `<button class="key" data-win="inst" title="Open this plugin's window in the live engine">WINDOW</button>` : ""}</div>
      ${synthDef ? `<div class="dvP">${paramRows(synthDef.params, inst.params, "i:")}</div>` : ""}
    </section><span class="wire"></span>` : ""}
    ${(t.fx || []).map((f, i) => {
      const def = f.type === "builtin" && S.catalog.effects[f.name];
      return `<section class="dev ${f.bypass ? "off" : ""}">
        <header><button class="pwr ${f.bypass ? "" : "on"}" data-byp="${i}" title="${f.bypass ? "Turn on" : "Bypass"}"></button>
          <div class="dvT"><span class="legend">INSERT ${i + 1} · ${def ? "BUILT-IN" : esc(f.type.toUpperCase())}</span><b title="${esc(fxLabel(f))}">${esc(fxLabel(f))}</b></div>
          <button class="mini danger" data-del="${i}" title="Remove this effect">×</button></header>
        ${def ? `<p class="dvD">${esc(def.description)}</p>` : ""}
        ${f.type === "vst3" ? `<div class="dvK"><button class="key" data-vp="${i}">PARAMETERS</button>${liveOk ? `<button class="key" data-win="${i}" title="Open this plugin's window in the live engine">WINDOW</button>` : ""}</div>` : ""}
        ${canSidechain(f) ? `<label class="fld" title="Another track's sound drives this effect's detector (ducking). Plugins without a sidechain input refuse."><span>Sidechain</span>
          <select data-sc="${i}"><option value="">none</option>${S.doc.tracks.filter((x) => x.kind !== "bus" && x.id !== t.id)
            .map((x) => `<option value="${x.id}" ${f.sidechain === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></label>` : ""}
        ${def ? `<div class="dvP">${paramRows(def.params, f.params, `f${i}:`)}</div>` : ""}
      </section><span class="wire"></span>`;
    }).join("")}
    <button class="dev add" id="addFx" title="Add an effect after the last one"><b>+</b><span class="legend">ADD EFFECT</span></button>
  </div></div>`;
  const on = (sel, ev, fn) => { const el = box.querySelector(sel); if (el) el[ev] = fn; };
  on("#chooseInst", "onclick", () => openDevicePicker(true, (dev) => act("track_update", { track: t.id, instrument: dev }), null, inst ? devString(inst) : null));
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
  box.querySelectorAll("[data-win]").forEach((b) => b.onclick = () => openLiveWindow(t.id, b.dataset.win === "inst" ? "inst" : +b.dataset.win));
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
  const br = S.doc.brief || {}, keys = Array.from(new Set(["genre", "style", "mood", "use", "game", "references", "notes", ...Object.keys(br)])).filter((k) => k !== "stills");
  // style: a genre style sheet every seat's brief follows (auto: the genre's words pick one)
  if (!S.styles) { S.styles = []; api("style_list", {}, null).then((l) => { S.styles = l; if (S.tab === "brief") renderBrief(); }).catch(() => {}); }
  const styleSel = `<select data-b="style" title="A genre style sheet: each seat's brief gets that genre's craft. Auto: the genre's words pick one.">
    <option value="none">auto${br.style ? "" : " (from the genre)"}</option>${S.styles.map((s) => `<option value="${esc(s.name)}" title="${esc(s.about)}"${s.name === br.style ? " selected" : ""}>${esc(s.title)}</option>`).join("")}</select>`;
  // screenshots kept as mood references (picture(file=<image>)): shown, not typed into
  const still = (s, i) => `/api/picture?kind=still&n=${i}${S.take ? "&take=" + encodeURIComponent(S.take) : ""}&v=${encodeURIComponent(s.file || "")}`;
  const stills = (br.stills || []).map((s, i) => `<figure><a href="${still(s, i)}" target="_blank" rel="noopener" title="Open the full image"><img loading="lazy" src="${still(s, i)}" alt="${esc(s.note || s.file || "")}"></a><figcaption>${esc(s.note || String(s.file || "").split("/").pop())}<button class="mini" data-still-rm="${i + 1}" title="Take this screenshot out of the brief">×</button></figcaption></figure>`).join("");
  box.innerHTML = `<div class="card"><h3>Brief (what the music is for)</h3><div class="grid2">
    ${keys.map((k) => `<span>${esc(k)}</span>${k === "style" ? styleSel : k === "notes" ? `<textarea data-b="${k}" rows="3">${esc(br[k] || "")}</textarea>` : `<input data-b="${k}" value="${esc(typeof br[k] === "string" ? br[k] : JSON.stringify(br[k] ?? ""))}">`}`).join("")}
  </div></div>
  ${stills ? `<div class="card"><h3>Stills (screenshots for the mood)</h3><div class="stillsB">${stills}</div></div>` : ""}
  <div class="card"><h3>Delivery</h3><div class="grid2">
    <span>Loop region</span><span>${S.doc.loop ? `${fmtPos(S.doc.loop.start)} → ${fmtPos(S.doc.loop.end)}` : "none (shift-drag on the ruler to set)"}</span>
    <span>Target loudness</span><input id="lufsIn" type="number" step="0.5" value="${S.doc.master.target_lufs}">
    <span>Peak ceiling dB</span><input id="ceilIn" type="number" step="0.1" value="${S.doc.master.ceiling_db}">
    <span>Tail seconds</span><input id="tailIn" type="number" step="0.5" value="${S.doc.tail}">
  </div></div>`;
  box.querySelectorAll("[data-b]").forEach((el) => el.onchange = () => act("project_set", { brief: { [el.dataset.b]: el.value } }));
  box.querySelectorAll("[data-still-rm]").forEach((el) => el.onclick = () => act("picture", { remove_still: el.dataset.stillRm }).catch(() => {}));
  $("#lufsIn").onchange = (e) => act("project_set", { target_lufs: +e.target.value });
  $("#ceilIn").onchange = (e) => act("project_set", { ceiling_db: +e.target.value });
  $("#tailIn").onchange = (e) => act("project_set", { tail: +e.target.value });
}

// ---------------------------------------------------------------- modals & menus
function modal(html) { $("#modalBox").innerHTML = html; $("#modal").classList.remove("hidden"); return $("#modalBox"); }
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("mousedown", (e) => { if (e.target.id === "modal") closeModal(); });
// items: [label, fn]; an item without fn is a section heading.
function menu(e, items) {
  const m = $("#menu"); m.innerHTML = ""; if (!items.length) return;
  items.forEach(([label, fn]) => {
    const d = document.createElement("div"); d.textContent = label;
    if (fn) d.onclick = () => { m.classList.add("hidden"); fn(); }; else d.className = "mh";
    m.appendChild(d);
  });
  m.style.left = Math.min(e.clientX, innerWidth - 200) + "px"; m.style.top = Math.min(e.clientY, innerHeight - items.length * 30 - 10) + "px";
  m.classList.remove("hidden");
}
document.addEventListener("mousedown", (e) => { if (!e.target.closest("#menu")) $("#menu").classList.add("hidden"); });

// The Projects screen (a sheet over the whole room): every project as a row you can search and sort, its details
// beside the list, and a new one on the right.
async function openProjectModal() {
  let d = { details: [], current: null };
  try { d = await api("project_list"); } catch (e) { toast(e.message); }
  const cards = d.details || [];
  const ROOTS = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"];
  const MODES = ["major", "minor", "dorian", "phrygian", "lydian", "mixolydian", "harmonic minor"];
  const METERS = ["4/4", "3/4", "6/8", "2/4", "5/4", "7/8", "12/8"];
  const IDEAS = {
    genre: ["orchestral fantasy", "synthwave", "chiptune", "lo-fi", "ambient", "rock", "jazz", "folk"],
    mood: ["hopeful", "tense", "playful", "melancholy", "heroic", "mysterious", "calm", "driving"],
    use: ["main menu loop", "exploration", "battle", "boss", "town / shop", "stinger", "ambience", "credits"],
  };
  const el = Sheet.open({ title: "Projects", sub: `${cards.length} on this computer`, size: "full", body: `<div class="ph">
    <div class="phLib"></div>
    <form class="phNew" autocomplete="off">
      <div class="phH">New project</div>
      <label class="fld big"><span>Name</span><input id="npName" value="Menu Theme" spellcheck="false"></label>
      <div class="phRow2">
        <div class="fld"><span>Tempo</span><div class="unit2"><input id="npTempo" type="number" min="20" max="300" value="96"><i>BPM</i>
          <button type="button" class="mini" id="npTap" title="Tap the beat a few times">TAP</button></div></div>
        <div class="fld"><span>Key</span><div class="unit2"><select id="npRoot">${ROOTS.map((r) => `<option ${r === "D" ? "selected" : ""}>${r}</option>`).join("")}</select>
          <select id="npMode">${MODES.map((m) => `<option ${m === "minor" ? "selected" : ""}>${m}</option>`).join("")}</select></div></div>
      </div>
      <div class="fld"><span>Meter</span><div class="seg" id="npSig">${METERS.map((m, i) => `<button type="button" class="${i ? "" : "on"}" data-v="${m}">${m}</button>`).join("")}</div></div>
      <div class="phH sub">Brief <i>what the crew writes toward · optional</i></div>
      ${Object.entries(IDEAS).map(([k, list]) => `<div class="fld"><span>${k}</span><input id="np_${k}" placeholder="type, or pick below">
        <div class="ideas" data-for="np_${k}">${list.map((x) => `<button type="button" class="mini">${esc(x)}</button>`).join("")}</div></div>`).join("")}
      <div class="phGo"><button type="submit" class="key go" id="npCreate">CREATE PROJECT</button></div>
    </form></div>` });
  const items = cards.map((c) => ({ ...c, id: c.path, dim: c.exists === false,
    hay: [c.name, c.path, c.key, ...Object.values(c.brief || {}).filter((v) => typeof v === "string")].join(" ").toLowerCase(), f: {} }));
  const bySort = { edited: (a, b) => (b.modified || 0) - (a.modified || 0), name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }) };
  let sort = "edited";
  const briefLine = (c) => Object.values(c.brief || {}).filter((v) => v && typeof v === "string").join(" · ");
  const open = async (it) => {
    if (it.exists === false) { toast("That project's folder is gone"); return; }
    Sheet.close(true);
    if (it.path === d.current && S.doc) return;
    leaveTake(); await act("project_open", { path: it.path });
  };
  const lib = el.querySelector(".phLib");
  const sortKeys = document.createElement("div"); sortKeys.className = "seg";
  sortKeys.innerHTML = `<button type="button" data-s="edited" class="on">EDITED</button><button type="button" data-s="name">NAME</button>`;
  const list = () => {
    pickList(lib, {
      items, current: d.current, cols: "minmax(0,1fr) 104px 58px 52px 52px 86px", sort: bySort[sort],
      head: ["Project", "Key", "Tempo", "Meter", "Tracks", "Edited"], placeholder: "FIND A PROJECT", tip: "Double-click or Enter opens it",
      empty: "No project matches.", none: "No projects yet. Make one on the right.",
      row: (c) => `<span class="phN"><b>${c.path === d.current ? `<span class="lamp amber" title="Open now"></span>` : ""}${esc(c.name)}${c.exists === false ? `<em class="tag bad">MISSING</em>` : ""}</b>
          <small>${esc(briefLine(c) || c.path)}</small></span>
        <span>${esc(c.key || "—")}</span><span class="num">${c.tempo ? Math.round(c.tempo * 10) / 10 : "—"}</span><span class="num">${esc(c.time_sig || "—")}</span>
        <span class="num">${c.tracks ?? "—"}</span><span class="muted">${c.modified ? ago(c.modified) : ""}</span>`,
      detail: (c, box) => {
        box.innerHTML = `<div class="legend">${c.path === d.current ? "OPEN NOW" : "PROJECT"}</div><h3>${esc(c.name)}</h3>
          <dl class="facts"><dt>Key</dt><dd>${esc(c.key || "—")}</dd><dt>Tempo</dt><dd>${c.tempo ? c.tempo + " BPM" : "—"}</dd><dt>Meter</dt><dd>${esc(c.time_sig || "—")}</dd>
            <dt>Tracks</dt><dd>${c.tracks ?? "—"}</dd><dt>Edited</dt><dd>${c.modified ? new Date(c.modified * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—"}</dd>
            ${Object.entries(c.brief || {}).filter(([, v]) => v && typeof v === "string").map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
          <div class="gId" title="${esc(c.path)}">${esc(c.path)}</div>
          <div class="keys"><button type="button" class="key go" data-k="open" ${c.exists === false ? "disabled" : ""}>${c.path === d.current ? "BACK TO IT" : "OPEN"}</button></div>`;
        box.querySelector('[data-k="open"]').onclick = () => open(c);
      },
      onPick: open,
    });
    lib.querySelector(".plTools").appendChild(sortKeys);
  };
  list();
  sortKeys.querySelectorAll("button").forEach((b) => b.onclick = () => {
    sort = b.dataset.s; sortKeys.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); list();
  });
  // a new project
  const form = el.querySelector(".phNew");
  let sig = "4/4";
  form.querySelectorAll("#npSig button").forEach((b) => b.onclick = () => { sig = b.dataset.v; form.querySelectorAll("#npSig button").forEach((x) => x.classList.toggle("on", x === b)); });
  form.querySelectorAll(".ideas").forEach((g) => g.querySelectorAll("button").forEach((b) => b.onclick = () => {
    const inp = $("#" + g.dataset.for), have = inp.value.split(",").map((s) => s.trim()).filter(Boolean);
    const i = have.indexOf(b.textContent); if (i >= 0) have.splice(i, 1); else have.push(b.textContent);
    inp.value = have.join(", "); b.classList.toggle("on", i < 0);
  }));
  let taps = [];
  $("#npTap").onclick = () => {
    const now = performance.now(); taps = taps.filter((t) => now - t < 2500); taps.push(now);
    if (taps.length > 1) $("#npTempo").value = Math.round(60000 * (taps.length - 1) / (now - taps[0]));
  };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const name = $("#npName").value.trim(); if (!name) { toast("Give the project a name"); $("#npName").focus(); return; }
    const brief = {}; Object.keys(IDEAS).forEach((k) => { const v = $("#np_" + k).value.trim(); if (v) brief[k] = v; });
    Sheet.close(true); leaveTake();
    await act("project_new", { name, tempo: +$("#npTempo").value, key: `${$("#npRoot").value} ${$("#npMode").value}`, time_sig: sig, brief });
  };
}
// ---------------------------------------------------------------- choosers and forms as sheets (sheet.js)
const DEV_SRC = { patch: "Saved sounds", vst3: "Plug-ins", sfz: "SFZ", synth: "Built-in", builtin: "Built-in" };
const devFrom = (i) => i.by === "built-in" ? "TuneTrack" : i.by;
async function deviceItems(instrument) {
  const all = (await api("catalog", { kind: instrument ? "instrument" : "effect" })).items;
  return all.map((i) => ({ ...i, dim: i.missing, f: { source: DEV_SRC[i.source], type: i.type, maker: i.maker },
    hay: [i.name, i.by, i.maker, i.type, ...(i.tags || []), i.role || "", i.about || ""].join(" ").toLowerCase() }));
}
// The instrument / effect browser in any host: facets, a list you walk with the keys, the highlighted item's detail
// with a preview in the project's key and tempo. opts: current (device string in use), use(item) -> the main key's
// label, onPick(item), onHi(item) (the highlight moved), hear (an Audio the caller stops when it closes).
function deviceBrowser(host, items, instrument, opts) {
  let playing = null;
  const hear = opts.hear;
  return pickList(host, {
    items, current: opts.current, placeholder: instrument ? "SEARCH SOUNDS, PLUG-INS, TAGS" : "SEARCH EFFECTS",
    groups: [{ key: "source", label: "Source", order: ["Saved sounds", "Built-in", "SFZ", "Plug-ins"] }, { key: "type", label: "Type" }, { key: "maker", label: "Maker" }],
    cols: "minmax(0,1.3fr) minmax(0,1fr) 132px", head: ["Name", "From", "Type"], tip: opts.tip || "Double-click or Enter loads it",
    empty: "Nothing matches. Clear the search or a filter.",
    row: (i) => `<span class="dpN">${i.id === opts.current ? `<span class="lamp amber" title="In use"></span>` : ""}<b>${esc(i.name)}</b>${i.new ? `<em class="tag stale">NEW</em>` : ""}</span>
      <span class="muted">${esc(devFrom(i))}</span><span class="muted">${esc(i.type)}</span>`,
    detail: (i, box) => {
      if (opts.onHi) opts.onHi(i);
      const loose = i.source === "vst3" && i.kind === "instrument";
      box.innerHTML = `<div class="legend">${esc(DEV_SRC[i.source])} · ${esc(i.type)}</div><h3>${esc(i.name)}</h3>
        <div class="by">${esc(devFrom(i))}${i.maker && i.maker !== i.by && i.maker !== devFrom(i) ? ` · ${esc(i.maker)}` : ""}</div>
        ${i.about ? `<p class="about">${esc(i.about)}</p>` : ""}
        ${(i.tags || []).length ? `<div class="chips">${i.tags.map((t) => `<em>${esc(t)}</em>`).join("")}</div>` : ""}
        ${i.role ? `<dl class="facts"><dt>Role</dt><dd>${esc(i.role)}</dd></dl>` : ""}
        ${loose ? `<p class="note">Loads with no sound chosen. Pick one in its window afterwards, or use one of its saved sounds.</p>` : ""}
        ${i.missing ? `<p class="note bad">Its plug-in file is gone from this computer.</p>` : ""}
        <div class="keys"><button type="button" class="key" data-k="hear" title="A short phrase in this project's key and tempo">${playing === i.id ? "■ STOP" : "▶ PREVIEW"}</button>
          <button type="button" class="key go" data-k="use" ${i.missing ? "disabled" : ""}>${esc(opts.use(i))}</button></div>
        <div class="gId" title="${esc(i.id)}">${esc(i.id)}</div>`;
      box.querySelector('[data-k="use"]').onclick = () => opts.onPick(i);
      const hb = box.querySelector('[data-k="hear"]');
      hb.onclick = async () => {
        if (playing === i.id) { hear.pause(); playing = null; hb.textContent = "▶ PREVIEW"; return; }
        hb.disabled = true; hb.textContent = "RENDERING…";
        try {
          const r = await api("gear_audition", { item: i.id, phrase: "auto", live: false });
          hear.src = "/api/audio?file=" + encodeURIComponent(r.file) + "&t=" + Date.now(); await hear.play(); playing = i.id;
          if (r.silent) toast("The preview was silent: no sound is loaded in it", true);
        } catch (e) { toast(e.message); playing = null; }
        hb.disabled = false; hb.textContent = playing === i.id ? "■ STOP" : "▶ PREVIEW";
        hear.onended = () => { playing = null; if (document.contains(hb)) hb.textContent = "▶ PREVIEW"; };
      };
    },
    onPick: opts.onPick,
  });
}
// Choose an instrument or an effect (a sheet). current: the device string in use now (marked, and the list starts
// on it); onCancel runs when the sheet is closed without a pick.
async function openDevicePicker(instrument, cb, onCancel, current = null) {
  let items;
  try { items = await deviceItems(instrument); } catch (e) { toast(e.message); return; }
  const hear = new Audio(); let picked = false;
  const el = Sheet.open({ title: instrument ? "Instrument" : "Insert effect", sub: `${items.length} on this computer`, size: "wide",
    body: `<div class="dpHost"></div>`, onClose: () => { if (!picked && onCancel) onCancel(); } });
  el._stop = () => hear.pause();
  const pick = (it) => { if (it.missing) { toast("Its plug-in file is gone"); return; } picked = true; hear.pause(); Sheet.close(true); cb(it.id); };
  deviceBrowser(el.querySelector(".dpHost"), items, instrument, { current, hear, onPick: pick,
    use: (i) => i.id === current ? "KEEP" : instrument ? "LOAD" : "INSERT" });
}

// Add a track (a sheet): the kind, its name, and for an instrument track the browser right beside it; the
// highlighted instrument is the one it gets. The name and role follow the highlight until you type your own.
async function openAddTrack(kind = "midi") {
  let items = [];
  try { items = await deviceItems(true); } catch (e) { toast(e.message); }
  const KINDS = [["midi", "INSTRUMENT", "Notes played by an instrument: a saved sound, a built-in synth, SFZ or a plug-in."],
    ["audio", "AUDIO", "Recorded or imported sound: voice, guitar, a loop, a sound effect."],
    ["bus", "BUS", "A shared effect other tracks send to, such as one reverb for the whole band."]];
  const ROLES = ["melody", "strings", "pad", "bass", "drums", "percussion", "keys", "brass", "woodwinds", "fx"];
  const hear = new Audio();
  const el = Sheet.open({ title: "Add track", size: kind === "midi" ? "wide" : "", body: `<div class="at">
    <form class="atForm" autocomplete="off">
      <div class="tiles atKinds">${KINDS.map(([k, t, d]) => `<button type="button" data-kind="${k}"><b>${t}</b><span>${esc(d)}</span></button>`).join("")}</div>
      <label class="fld big"><span>Name</span><input id="atName" spellcheck="false"></label>
      <div class="fld atMidi"><span>Role</span><input id="atRole" placeholder="what it plays: violin, bass, pad…">
        <div class="ideas">${ROLES.map((r) => `<button type="button" class="mini">${r}</button>`).join("")}</div></div>
      <div class="fld atBus"><span>Effect</span><div class="seg" id="atFx"><button type="button" data-v="builtin:reverb" class="on">REVERB</button><button type="button" data-v="builtin:delay">DELAY</button><button type="button" data-v="">NONE</button></div></div>
      <div class="atChosen atMidi"><span class="legend">Instrument</span><b id="atInst">—</b><span class="muted">Highlight one in the list. Enter or double-click adds the track.</span></div>
      <div class="phGo"><button type="submit" class="key go">ADD TRACK</button></div>
    </form>
    <div class="atPick"></div></div>` });
  el._stop = () => hear.pause();
  const P = { kind, fx: "builtin:reverb", named: false, roled: false, browser: null, inst: null };
  const name = el.querySelector("#atName"), role = el.querySelector("#atRole");
  const title = (s) => s.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const defaults = { audio: "Audio", bus: "Reverb" };
  name.oninput = () => { P.named = !!name.value.trim(); };
  role.oninput = () => { P.roled = !!role.value.trim(); };
  el.querySelectorAll(".atMidi .ideas button").forEach((b) => b.onclick = () => { role.value = b.textContent; P.roled = true; if (!P.named) name.value = title(b.textContent); });
  el.querySelectorAll("#atFx button").forEach((b) => b.onclick = () => {
    P.fx = b.dataset.v; el.querySelectorAll("#atFx button").forEach((x) => x.classList.toggle("on", x === b));
    if (!P.named) name.value = P.fx ? title(P.fx.split(":")[1]) : "Bus";
  });
  const onHi = (i) => {
    P.inst = i; el.querySelector("#atInst").textContent = `${i.name} · ${devFrom(i)}`;
    if (!P.roled) role.value = i.role || "";
    if (!P.named && P.kind === "midi") name.value = title(i.role || i.name);
  };
  function setKind(k) {
    P.kind = k;
    el.classList.toggle("wide", k === "midi");
    el.querySelector(".at").classList.toggle("solo", k !== "midi");
    el.querySelectorAll("[data-kind]").forEach((b) => b.classList.toggle("on", b.dataset.kind === k));
    el.querySelectorAll(".atMidi").forEach((x) => x.classList.toggle("hidden", k !== "midi"));
    el.querySelectorAll(".atBus").forEach((x) => x.classList.toggle("hidden", k !== "bus"));
    if (k === "midi") {
      if (!P.browser) P.browser = deviceBrowser(el.querySelector(".atPick"), items, true, { hear, onHi, onPick: add, use: () => "ADD TRACK", tip: "Double-click or Enter adds the track" });
      else if (P.inst) onHi(P.inst);
    } else {
      if (!P.named) name.value = defaults[k];
      setTimeout(() => name.focus(), 0);
    }
  }
  async function add() {
    const n = name.value.trim(); if (!n) { toast("Give the track a name"); name.focus(); return; }
    const inst = P.kind === "midi" && P.browser ? P.browser.item : null;
    if (inst && inst.missing) { toast("Its plug-in file is gone"); return; }
    const args = { name: n, kind: P.kind, instrument: inst ? inst.id : null };
    if (P.kind === "midi" && role.value.trim()) args.role = role.value.trim();
    if (P.kind === "bus" && P.fx) args.fx = [P.fx];
    hear.pause(); Sheet.close(true);
    const r = await act("track_add", args); S.sel.track = r.track; renderAll();
  }
  el.querySelectorAll("[data-kind]").forEach((b) => b.onclick = () => setKind(b.dataset.kind));
  el.querySelector(".atForm").onsubmit = (e) => { e.preventDefault(); add(); };
  setKind(kind);
}

// A VST3 effect's parameters (a sheet): filter by name; moving a slider sets it.
async function openVstParams(t, i) {
  const f = t.fx[i];
  const el = Sheet.open({ title: fxLabel(f), sub: "PARAMETERS", size: "seat", body: `<div class="vp">
    <div class="plTools"><input class="shQ" type="search" spellcheck="false" placeholder="FILTER PARAMETERS"><span class="plCount"></span></div>
    <div class="vpList"><p class="muted">Loading the plug-in…</p></div></div>` });
  const q = el.querySelector(".shQ"), list = el.querySelector(".vpList"), count = el.querySelector(".plCount");
  let seq = 0, timer = 0;
  const load = async () => {
    const my = ++seq;
    try {
      const r = await api("plugin_params", { device: devString(f), query: q.value.trim() || null, limit: 120 });
      if (my !== seq || !document.contains(list)) return;
      count.textContent = `${r.params.length} / ${r.total}`;
      list.innerHTML = r.params.length ? `<div class="params">${r.params.map((p) => `<label class="param"><span title="${esc(p.name)}">${esc(p.name)}</span>
        <input type="range" min="0" max="1" step="0.001" value="${(f.params || {})[p.name] ?? p.value}" data-n="${esc(p.name)}"><span class="muted">${esc(p.text)}</span></label>`).join("")}</div>
        ${r.total > r.params.length ? `<p class="muted">${r.total - r.params.length} more: type to narrow the list.</p>` : ""}` : `<p class="muted">No parameter matches.</p>`;
      list.querySelectorAll("[data-n]").forEach((x) => x.onchange = () => act("fx_update", { track: t.id, index: i, params: { [x.dataset.n]: +x.value } }));
    } catch (e) { list.innerHTML = `<p class="bad">${esc(e.message)}</p>`; }
  };
  q.oninput = () => { clearTimeout(timer); timer = setTimeout(load, 250); };
  load(); setTimeout(() => q.focus(), 0);
}

// Save a sound from a plug-in's own window as a reusable patch (a sheet).
function openCapture(t) {
  const inst = t.instrument;
  const el = Sheet.open({ title: "Pick a sound", sub: esc(inst.name), body: `<form class="sform" autocomplete="off">
      <ol class="steps"><li>The plug-in's own window opens on this computer.</li><li>Load a preset or program and shape it.</li>
        <li><b>Close the plug-in window.</b> The sound is saved as a patch this track uses, and the crew can call it by name.</li></ol>
      <label class="fld big"><span>Patch name</span><input id="cpName" placeholder="Warm Strings"></label>
      <div class="phRow2"><label class="fld"><span>Role</span><input id="cpRole" value="${esc(t.role || "")}" placeholder="violin, bass, pad…"></label>
        <label class="fld"><span>Tags</span><input id="cpTags" placeholder="strings, legato, warm"></label></div>
      <label class="fld"><span>Description</span><input id="cpDesc" placeholder="what it sounds like, what it is best for"></label>
      <div class="fld"><span>Dynamics</span><div class="seg" id="cpCC"><button type="button" data-v="" class="on" title="Loudness follows how hard notes are played">VELOCITY</button>
        <button type="button" data-v="1" title="Loudness follows the mod wheel (most orchestral libraries)">CC1 · MOD WHEEL</button><button type="button" data-v="11" title="Loudness follows expression">CC11 · EXPRESSION</button></div></div>
    </form>`, foot: `<span class="spacer"></span><button class="key" id="cpCancel">CANCEL</button><button class="key go" id="cpGo">OPEN PLUG-IN WINDOW</button>` });
  let cc = "";
  el.querySelectorAll("#cpCC button").forEach((b) => b.onclick = () => { cc = b.dataset.v; el.querySelectorAll("#cpCC button").forEach((x) => x.classList.toggle("on", x === b)); });
  el.querySelector("#cpCancel").onclick = () => Sheet.close();
  setTimeout(() => $("#cpName").focus(), 0);
  const go = async () => {
    const name = $("#cpName").value.trim(); if (!name) { toast("Give the patch a name"); $("#cpName").focus(); return; }
    const args = { plugin: inst.name, name, tags: $("#cpTags").value.split(",").map((s) => s.trim()).filter(Boolean),
      description: $("#cpDesc").value, role: $("#cpRole").value || null, dynamics_cc: cc ? +cc : null, base_patch: inst.patch || null };
    Sheet.close(true); status("waiting for the plugin window to close…", "busy");
    try {
      const meta = await api("patch_capture", args); status("");
      if (meta.warning) toast(meta.warning); else toast(`Saved patch "${meta.id}"`, true);
      S.library = null; await act("track_update", { track: t.id, instrument: `patch:${meta.id}` });
    } catch (e) { toast(e.message); status("", ""); }
  };
  el.querySelector("#cpGo").onclick = go;
  el.querySelector(".sform").onsubmit = (e) => { e.preventDefault(); go(); };
}

// Export (a sheet): file type, how the loop is cut, extras, where it goes.
function openExportModal() {
  if (!S.doc) return;
  const LOOPS = [["auto", "AUTO", S.doc.loop ? "Uses the loop region: a seamless loop with loop points" : "No loop region set: plays once with its tail"],
    ["off", "ONE-SHOT", "Plays once and lets the tail ring out"], ["loop", "LOOP BODY", "Only the loop, tail folded into its start"],
    ["intro+loop", "INTRO + LOOP", "From the top, with loop points around the loop"]];
  const EXTRAS = [["norm", "NORMALISE", `Brings it to ${S.doc.master.target_lufs} LUFS`, true], ["stems", "STEMS", "Each track as its own file", false],
    ["layers", "LAYERS", "Each adaptive layer as its own file", false], ["midi", "MIDI", "A .mid of every part", false]];
  const P = { fmt: "wav", bits: 24, loop: "auto", norm: true, stems: false, layers: false, midi: false };
  const el = Sheet.open({ title: "Export", sub: esc(S.doc.name), body: `<form class="sform" autocomplete="off">
      <div class="phRow2"><div class="fld"><span>File type</span><div class="seg" id="exFmt">${["wav", "ogg", "flac", "mp3"].map((f) => `<button type="button" data-v="${f}">${f.toUpperCase()}</button>`).join("")}</div></div>
        <div class="fld exBits"><span>Bit depth</span><div class="seg" id="exBits">${[16, 24, 32].map((b) => `<button type="button" data-v="${b}">${b}-BIT</button>`).join("")}</div></div></div>
      <div class="phH sub">Loop</div>
      <div class="tiles" id="exLoop">${LOOPS.map(([k, t, d]) => `<button type="button" data-v="${k}"><b>${t}</b><span>${esc(d)}</span></button>`).join("")}</div>
      <div class="phH sub">Also</div>
      <div class="tiles" id="exExtra">${EXTRAS.map(([k, t, d]) => `<button type="button" data-v="${k}" aria-pressed="false"><b>${t}</b><span>${esc(d)}</span></button>`).join("")}</div>
      <div class="phH sub">Where</div>
      <label class="fld"><span>Save to</span><input id="exOut" spellcheck="false" placeholder="the project's renders folder"></label>
      <p class="muted sNote">Or a full path into your game, such as C:\\Game\\Audio\\Music\\menu.ogg: its extension sets the file type.</p>
    </form>`, foot: `<span class="spacer"></span><button class="key" id="exCancel">CANCEL</button><button class="key go" id="exGo">RENDER</button>` });
  const draw = () => {
    el.querySelectorAll("#exFmt button").forEach((b) => b.classList.toggle("on", b.dataset.v === P.fmt));
    el.querySelectorAll("#exBits button").forEach((b) => b.classList.toggle("on", +b.dataset.v === P.bits));
    el.querySelector(".exBits").classList.toggle("off", P.fmt !== "wav");
    el.querySelectorAll("#exLoop button").forEach((b) => b.classList.toggle("on", b.dataset.v === P.loop));
    el.querySelectorAll("#exExtra button").forEach((b) => { b.classList.toggle("on", P[b.dataset.v]); b.setAttribute("aria-pressed", P[b.dataset.v]); });
  };
  el.querySelectorAll("#exFmt button").forEach((b) => b.onclick = () => { P.fmt = b.dataset.v; draw(); });
  el.querySelectorAll("#exBits button").forEach((b) => b.onclick = () => { P.bits = +b.dataset.v; draw(); });
  el.querySelectorAll("#exLoop button").forEach((b) => b.onclick = () => { P.loop = b.dataset.v; draw(); });
  el.querySelectorAll("#exExtra button").forEach((b) => b.onclick = () => { P[b.dataset.v] = !P[b.dataset.v]; draw(); });
  draw();
  el.querySelector("#exCancel").onclick = () => Sheet.close();
  const go = async () => {
    const args = { format: P.fmt, bits: P.bits, loop: P.loop, normalize: P.norm, stems: P.stems, layers: P.layers, midi: P.midi };
    if ($("#exOut").value.trim()) args.out = $("#exOut").value.trim();
    Sheet.close(true); status("rendering…", "busy");
    try { S.lastRender = await api("render", args); status("exported"); document.querySelector('#tabs button[data-tab="report"]').click(); }
    catch (e) { toast(e.message); status("render failed", "err"); }
  };
  el.querySelector("#exGo").onclick = go;
  el.querySelector(".sform").onsubmit = (e) => { e.preventDefault(); go(); };
}

loadState();
