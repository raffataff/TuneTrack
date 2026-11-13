// TuneTrack Studio room: the crew rack (one unit per seat), Needs You, and the monitor/talkback section.
// It draws from the board (/api/state, studio_view) and follows /api/events, so every seat's work shows up live.
// Uses the helpers app.js defines: $, S, api, status, toast, esc, modal, closeModal, loadState, renderAll, stop.
"use strict";
(() => {
const SHORT = { producer: "PROD", spotting: "SPOT", composer: "COMP", arranger: "ARR", rhythm: "RHY", sound: "SND", tracking: "TRK", mix: "MIX", review: "REV" };
const TABS = ["tasks", "takes", "log", "setup"];
const OPEN_STATES = ["queued", "working", "submitted", "needs_you"];
const ST = {
  board: null, crew: null, project: null, open: null, tab: {}, units: {}, act: {}, feed: [], lastSeq: 0, es: null,
  target: "producer", wake: true, talkOnly: false, diffs: {}, agents: null, tickets: {}, benchSig: {},
};
const AB = { el: new Audio(), key: null, cache: {} };
const store = {
  get(k, d) { try { return localStorage.getItem("tt." + k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("tt." + k, v); } catch { /* private window */ } },
};
const h = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const clock = (at) => new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const mmss = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const seatOf = (id) => ST.board && ST.board.seats.find((s) => s.seat === id);
const title = (id) => id === "you" ? "You" : id === "all" ? "Everyone" : id === "floor" ? "Floor" : (seatOf(id) || {}).title || id;
const taskOf = (id) => ST.board && ST.board.tasks.find((t) => t.id === id);
const focusedIn = (el) => el.contains(document.activeElement) && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);

// One call for the board and the crew; coalesced, because agents emit several events a second.
let refreshTimer = null, refreshing = false, again = false;
function refresh(now = false) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    if (refreshing) { again = true; return; }
    refreshing = true;
    try { const v = await api("studio_view"); if (v.board) { ST.board = v.board; ST.crew = v.crew; render(); } }
    catch { /* server restarting: the next event or poll brings it back */ }
    refreshing = false;
    if (again) { again = false; refresh(); }
  }, now ? 0 : 140);
}
async function call(fn, args, busy) {
  try { if (busy) status(busy, "busy"); const r = await api(fn, args || {}); if (busy) status(""); refresh(true); return r; }
  catch (e) { if (busy) status(""); toast(e.message); refresh(true); throw e; }
}

// ---------------------------------------------------------------- events
function seatAct(id) { return ST.act[id] || (ST.act[id] = { lines: [], hits: [], last: "", started: null }); }
function line(seat, at, cls, html, plain) {
  const a = seatAct(seat); a.lines.push({ at, cls, html }); if (a.lines.length > 300) a.lines.splice(0, 100);
  if (plain) a.last = plain;
}
function feed(at, cls, html) { ST.feed.push({ at, cls, html }); if (ST.feed.length > 500) ST.feed.splice(0, 150); }
function ingest(events) {
  for (const e of events) {
    if (e.seq <= ST.lastSeq) continue;
    ST.lastSeq = e.seq;
    const who = e.seat ? `<b>${esc(title(e.seat))}</b>` : "";
    if (e.kind === "run") {
      const a = seatAct(e.seat);
      if (e.what === "start") { a.started = e.at; a.hits = []; line(e.seat, e.at, "note", `run starts on ${esc(e.task)} · ${esc(e.agent || "")}${e.take ? " · take " + esc(e.take) : ""}`, "starting…"); feed(e.at, "", `${who} starts ${esc(e.task)}${e.take ? " in " + esc(e.take) : ""}`); }
      else if (e.what === "tool") { a.hits.push(e.at); line(e.seat, e.at, "tool", `› <b>${esc(e.tool)}</b>`, "› " + e.tool); }
      else if (e.what === "says") line(e.seat, e.at, "say", esc(e.text), e.text.replace(/\s+/g, " "));
      else if (e.what === "end") { a.started = null; line(e.seat, e.at, e.ok ? "good" : "bad", e.ok ? `run ended · ${e.tools} tool calls` : `run ended early: ${esc(e.error || "no reason given")}`, e.ok ? "run ended" : "ended early: " + (e.error || "")); feed(e.at, e.ok ? "" : "bad", `${who} ${e.ok ? "finished its run" : "stopped early: " + esc(e.error || "no reason given")}`); }
    } else if (e.kind === "task") {
      const t = `${esc(e.id)} “${esc(e.title)}” → ${esc(String(e.state).replace("_", " "))}${e.reason ? " (" + esc(e.reason) + ")" : ""}`;
      line(e.seat, e.at, e.state === "done" ? "good" : e.state === "needs_you" ? "note" : "", t);
      feed(e.at, e.state === "needs_you" ? "note" : "", `${who} ${t}`);
    } else if (e.kind === "take") {
      const t = `take ${esc(e.id)} ${esc(e.state)}${e.summary ? ": " + esc(e.summary) : ""}${e.reason ? ": " + esc(e.reason) : ""}`;
      if (e.seat) line(e.seat, e.at, e.state === "accepted" ? "good" : e.state === "returned" ? "note" : "", t);
      feed(e.at, e.state === "accepted" ? "good" : "", `${who} ${t}`);
      delete ST.diffs[e.id]; delete AB.cache[e.id];
    } else if (e.kind === "message") {
      feed(e.at, "say" + (e.sender === "you" ? " you" : ""), `<i>${esc(title(e.sender))} → ${esc(title(e.to))}</i>  ${esc(e.text)}`);
      for (const s of [e.sender, e.to]) if (SHORT[s]) line(s, e.at, "say", `<i>${esc(title(e.sender))} → ${esc(title(e.to))}:</i> ${esc(e.text)}`);
    } else if (e.kind === "attention") {
      feed(e.at, e.state === "open" ? "note" : "", e.state === "open" ? `Needs you: ${esc(e.title)}` : `Answered: ${esc(e.title)}${e.answer ? " → " + esc(e.answer) : ""}`);
    } else if (e.kind === "crew") {
      feed(e.at, e.error ? "bad" : "note", e.what === "start" ? "Crew running" : `Crew stopped${e.error ? ": " + esc(e.error) : ""}`);
    } else if (e.kind === "seat") {
      feed(e.at, "", `${who} seat: ${esc(e.occupant)}${e.paused ? " · on hold" : ""}`);
    }
  }
}
async function connect() {
  if (ST.es) { ST.es.close(); ST.es = null; }
  S.live = false;
  Object.assign(ST, { act: {}, feed: [], lastSeq: 0, diffs: {}, benchSig: {} });
  const project = ST.project;
  if (!project) return;
  try { ingest((await api("events", { since: 0, limit: 500 })).events); } catch { /* no board yet */ }
  if (project !== ST.project) return;
  render();
  const es = ST.es = new EventSource("/api/events?since=" + ST.lastSeq);
  es.onopen = () => { S.live = true; };
  es.onerror = () => { S.live = false; };
  es.onmessage = (m) => {
    if (es !== ST.es) return;
    const msg = JSON.parse(m.data);
    if ((msg.project !== S.path || msg.version !== S.version) && !S.pendingSave && !S.drag) loadState();
    if (msg.events && msg.events.length && msg.project === ST.project) { ingest(msg.events); refresh(); }
  };
}

// ---------------------------------------------------------------- header: crew switch, Needs You lamp, rooms
function renderHeader() {
  const c = ST.crew || {}, n = ST.board ? ST.board.needs_you.length : 0;
  const btn = $("#crewBtn"), read = $("#crewRead");
  btn.textContent = c.running ? "STOP" : "RUN"; btn.classList.toggle("on", !!c.running);
  btn.disabled = !ST.board || !!c.elsewhere;
  btn.title = c.elsewhere ? "This session's crew is being run from a terminal (tunetrack crew run)" : c.running ? "Stop the crew: running agents stop, their tasks go back in the queue" : "Let the seated agents work on the open tasks";
  $("#briefBtn").disabled = !ST.board;
  const active = Object.keys(c.active || {}).length, cost = c.usage && c.usage.cost_usd ? ` · $${c.usage.cost_usd.toFixed(2)}` : "";
  read.innerHTML = c.error ? `<span class="amb">FAULT</span>` : c.elsewhere ? `<span class="amb">RUN BY TERMINAL</span>`
    : c.running ? `${active ? `<span class="amb">${active} AT WORK</span>` : "LISTENING"} · ${String(c.launches).padStart(2, "0")}/${c.max_runs}${cost}`
    : c.launches ? `<span class="dimv">STOPPED · ${c.launches} RUNS${cost}</span>` : `<span class="dimv">STANDBY</span>`;
  read.title = c.error || "Agent runs this session / the limit before the crew stops and asks you";
  $("#needsCount").textContent = n; $("#needsBtn").classList.toggle("hot", n > 0);
  $("#needsBtn").querySelector(".lamp").className = "lamp" + (n ? " red blink" : "");
  $("#needsHead").textContent = n || "";
  $("#needs h2 .lamp").className = "lamp" + (n ? " red blink" : "");
}
$("#crewBtn").onclick = async () => {
  if (ST.crew && ST.crew.running) { await call("crew_stop", {}, "stopping the crew…"); return; }
  try { const v = await api("studio_view"); if (v.board) { ST.board = v.board; ST.crew = v.crew; } } catch { /* use what we have */ }
  const agents = ST.board.seats.filter((s) => s.occupant && s.occupant.kind === "agent");
  if (!agents.length) { toast("No agent is seated yet. Use SEAT on a unit to put one in, then RUN."); return; }
  const ready = ST.board.tasks.some((t) => OPEN_STATES.includes(t.state));
  if (!ready) { openBrief(); return; }
  await call("crew_start", {}, "starting the crew…");
};
$("#briefBtn").onclick = () => openBrief();
function openBrief() {
  const prod = seatOf("producer"), agent = prod && prod.occupant && prod.occupant.kind === "agent";
  const box = modal(`<h2>Brief the Producer</h2>
    <p class="hint">Say what you want made: what it is for, mood, length, anything it must fit. The Producer plans it, hands out tasks to the seats that are filled, and auditions what comes back. Each agent run costs whatever that seat's model costs.</p>
    ${agent ? "" : `<p class="hint warn">${prod && prod.occupant ? "The Producer seat is not an agent, so this brief waits there as a task for whoever sits in it." : "Nobody is in the Producer seat. The brief will wait there until you seat an agent (SEAT on the Producer unit)."}</p>`}
    <textarea id="brText" rows="6" placeholder="A calm main-menu loop for a small space game: 8 bars, D minor, about 90 BPM, loops seamlessly…"></textarea>
    <div class="formgrid" style="margin-top:10px"><span>At once</span><input id="brPar" type="number" min="1" max="6" value="3" title="Agents working at the same time">
      <span>Run limit</span><input id="brMax" type="number" min="1" max="200" value="${(ST.crew && ST.crew.max_runs) || 40}" title="Agent runs before the crew stops and asks you"></div>
    <div class="row"><button class="ghost" id="brCancel">Cancel</button><button class="key go" id="brGo">START THE SESSION</button></div>`);
  $("#brText").focus();
  $("#brCancel").onclick = closeModal;
  $("#brGo").onclick = async () => {
    const brief = $("#brText").value.trim(); if (!brief) { toast("Say what you want made"); return; }
    closeModal();
    await call("crew_start", { brief, parallel: +$("#brPar").value || 3, max_runs: +$("#brMax").value || 40 }, "starting the crew…");
  };
}
$("#needsBtn").onclick = () => { setRoom("studio"); $("#needs").scrollIntoView({ block: "nearest" }); const f = $("#needsList .key"); if (f) f.focus(); };
function setRoom(room) {
  document.body.dataset.room = room; store.set("room", room);
  document.querySelectorAll("#rooms button").forEach((b) => b.classList.toggle("active", b.dataset.room === room));
  if (room === "desk") renderAll(); else render();
}
document.querySelectorAll("#rooms button").forEach((b) => b.onclick = () => setRoom(b.dataset.room));

// ---------------------------------------------------------------- the rack
function seatState(s) {
  const run = ST.crew && ST.crew.active && ST.crew.active[s.seat], t = taskOf(s.task), kind = s.occupant && s.occupant.kind;
  if (run) return ["working", "amber pulse", "WORKING"];
  if (t && t.state === "needs_you") return ["needs", "red blink", "NEEDS YOU"];
  if (!s.occupant) return t ? ["needs", "red", "NO ONE SEATED"] : ["vacant", "", "VACANT"];
  if (s.paused) return ["hold", "dimamber", "ON HOLD"];
  if (t && t.state === "submitted") return ["handed", "cyan", "TAKE HANDED IN"];
  if (t && t.state === "working") return ["working", kind === "agent" ? "dimamber" : "amber", kind === "human" ? "YOU · WORKING" : "WORKING"];
  if (t && t.state === "queued") {
    if (kind === "human") return ["yours", "amber blink", "YOUR MOVE"];
    if (t.waiting_for) return ["queued", "dimamber", "WAITS ON " + t.waiting_for.join(" ")];
    if (kind === "agent" && !(ST.crew && (ST.crew.running || ST.crew.elsewhere))) return ["queued", "dimamber", "QUEUED · CREW STOPPED"];
    return ["queued", "dimamber", "QUEUED"];
  }
  return s.presence === "online" ? ["ready", "green", "ONLINE"] : ["ready", "green", "READY"];
}
function buildUnits() {
  const box = $("#units"); box.innerHTML = ""; ST.units = {};
  for (const s of ST.board.seats) {
    const u = h(`<article class="unit" data-seat="${s.seat}">
      <div class="face">
        <div class="ear"><i class="screw" style="--r:${(s.seat.length * 37) % 180}deg"></i><i class="screw" style="--r:${(s.seat.length * 71) % 180}deg"></i></div>
        <div class="ident"><div class="name">${esc(s.title)}</div><div class="room">${esc(s.room)}${s.optional ? " · optional" : ""}</div></div>
        <div class="lampcol"><i class="lamp"></i><span class="state"></span></div>
        <div class="vfd screen" title="Open this seat"><div class="l1"><span class="occ"></span><span class="clk dimv"></span></div><div class="l2"></div><div class="l3"></div></div>
        <div class="meter" title="Activity: tool calls in the last half minute">${"<i></i>".repeat(12)}</div>
        <div class="keys"><button class="key" data-k="seat" title="Choose who sits here">SEAT</button><button class="key toggle" data-k="hold" title="Hold this seat: stop its agent and start nothing new until released">HOLD</button><button class="key toggle" data-k="open" title="Open this seat's workstation">OPEN</button></div>
        <div class="ear"><i class="screw" style="--r:${(s.seat.length * 53) % 180}deg"></i><i class="screw" style="--r:${(s.seat.length * 19) % 180}deg"></i></div>
      </div>
      <div class="bench hidden"><div class="tabsB">${TABS.map((t) => `<button data-tab="${t}">${t}</button>`).join("")}<span class="purpose"></span></div><div class="benchBody"></div></div>
    </article>`);
    const q = (sel) => u.querySelector(sel);
    q('[data-k="seat"]').onclick = () => openSeatPicker(s.seat);
    q('[data-k="hold"]').onclick = () => { const cur = seatOf(s.seat); call("seat_hold", { seat: s.seat, paused: !cur.paused }); };
    const toggle = () => { ST.open = ST.open === s.seat ? null : s.seat; store.set("open", ST.open || ""); renderUnits(); if (ST.open) u.scrollIntoView({ block: "nearest", behavior: "smooth" }); };
    q('[data-k="open"]').onclick = toggle; q(".screen").onclick = toggle;
    u.querySelectorAll(".tabsB button").forEach((b) => b.onclick = () => { ST.tab[s.seat] = b.dataset.tab; delete ST.benchSig[s.seat]; renderUnits(); });
    q(".benchBody").addEventListener("focusout", () => setTimeout(() => { if (!focusedIn(u)) renderBench(s.seat); }, 0));
    q(".purpose").textContent = s.purpose; q(".purpose").title = s.purpose;
    box.appendChild(u); ST.units[s.seat] = u;
  }
}
function renderUnits() {
  if (!ST.board) { $("#units").innerHTML = `<div class="empty">Open or create a project to seat a crew.</div>`; ST.units = {}; return; }
  if (!ST.units[ST.board.seats[0].seat] || !ST.units[ST.board.seats[0].seat].isConnected) buildUnits();
  for (const s of ST.board.seats) {
    const u = ST.units[s.seat], q = (sel) => u.querySelector(sel), [state, lamp, label] = seatState(s), t = taskOf(s.task), a = seatAct(s.seat);
    u.dataset.state = state; u.classList.toggle("vacant", !s.occupant); u.classList.toggle("open", ST.open === s.seat);
    q(".lamp").className = "lamp " + lamp; q(".state").textContent = label; q(".state").title = label;
    q(".occ").innerHTML = s.occupant ? esc((s.label || s.occupant.kind).toUpperCase()) : `<span class="dimv">— VACANT —</span>`;
    q(".l2").innerHTML = t ? `<span class="amb">${esc(t.id)}</span> ${esc(t.title)}${s.open_tasks > 1 ? ` <span class="dimv">+${s.open_tasks - 1}</span>` : ""}` : `<span class="dimv">no open tasks</span>`;
    q(".l3").textContent = s.occupant ? (a.last || "·") : s.purpose;
    q('[data-k="hold"]').classList.toggle("on", s.paused); q('[data-k="hold"]').disabled = !s.occupant || s.occupant.kind !== "agent";
    q('[data-k="open"]').classList.toggle("on", ST.open === s.seat);
    q(".bench").classList.toggle("hidden", ST.open !== s.seat);
    if (ST.open === s.seat) renderBench(s.seat);
  }
  tickUnits();
}
function tickUnits() {
  if (!ST.board || document.body.dataset.room !== "studio") return;
  const now = Date.now() / 1000;
  for (const s of ST.board.seats) {
    const u = ST.units[s.seat]; if (!u) continue;
    const a = seatAct(s.seat), run = ST.crew && ST.crew.active && ST.crew.active[s.seat];
    a.hits = a.hits.filter((x) => now - x < 30);
    const level = run ? Math.min(12, Math.max(1, a.hits.length)) : 0;
    u.querySelectorAll(".meter i").forEach((el, i) => el.classList.toggle("on", i < level));
    u.querySelector(".clk").textContent = run ? mmss(Math.max(0, now - run.since)) : s.occupant ? (s.mode === "direct" ? "DIRECT" : "TAKES") : "";
  }
}
setInterval(tickUnits, 1000);

// ---------------------------------------------------------------- a seat's workstation
const TAG = { queued: "", working: "stale", submitted: "cy", needs_you: "bad", done: "ok", failed: "bad", cancelled: "" };
function takeSig(t) { return t.state === "open" ? "open:" + Date.now() : `${t.id}:${t.state}:${t.submitted || 0}:${t.returned || 0}`; }
function abKeys(take) {
  return `<span class="ab" title="Render and play. Switching between A and B keeps your place."><button class="key" data-ab="session">A · SESSION</button><button class="key" data-ab="${esc(take)}">B · ${esc(take.toUpperCase())}</button></span>`;
}
function wireAb(root) {
  root.querySelectorAll("[data-ab]").forEach((b) => { b.onclick = () => audition(b.dataset.ab === "session" ? null : b.dataset.ab); });
  markAb();
}
function renderBench(seat, force = false) {
  const u = ST.units[seat], s = seatOf(seat); if (!u || !s) return;
  const tab = ST.tab[seat] || (ST.board.takes.some((t) => t.seat === seat && t.state === "submitted") ? "takes" : "tasks");
  u.querySelectorAll(".tabsB button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const body = u.querySelector(".benchBody");
  const tasks = ST.board.tasks.filter((t) => t.seat === seat), takes = ST.board.takes.filter((t) => t.seat === seat), a = seatAct(seat);
  const sig = JSON.stringify(tab === "tasks" ? [tab, tasks, s.occupant] : tab === "takes" ? [tab, takes, Object.fromEntries(takes.map((t) => [t.id, ST.diffs[t.id]]))]
    : tab === "log" ? [tab, a.lines.length] : [tab, s.occupant, s.mode, s.notes, s.paused]);
  if (!force && ST.benchSig[seat] === sig) return;
  if (!force && focusedIn(body)) return;  // don't pull a form out from under someone typing; focusout redraws
  ST.benchSig[seat] = sig;
  if (tab === "tasks") benchTasks(body, s, tasks);
  else if (tab === "takes") benchTakes(body, s, takes);
  else if (tab === "log") benchLog(body, a);
  else benchSetup(body, s);
}
function benchTasks(body, s, tasks) {
  const open = tasks.filter((t) => OPEN_STATES.includes(t.state)), closed = tasks.filter((t) => !OPEN_STATES.includes(t.state)).reverse();
  const human = s.occupant && s.occupant.kind !== "agent";
  const card = (t) => `<div class="item ${OPEN_STATES.includes(t.state) ? "hot" : "done"}" data-task="${esc(t.id)}">
      <h4><span class="id">${esc(t.id)}</span>${esc(t.title)}<span class="tag ${TAG[t.state] || ""}">${esc(t.state.replace("_", " "))}</span>${t.waiting_for ? `<span class="tag">after ${esc(t.waiting_for.join(", "))}</span>` : ""}${t.take ? `<span class="tag cy">${esc(t.take)}</span>` : ""}</h4>
      ${t.detail ? `<p>${esc(t.detail)}</p>` : ""}${t.result ? `<p class="meta">Result: ${esc(t.result)}</p>` : ""}
      <div class="row" style="margin-top:8px">${OPEN_STATES.includes(t.state)
        ? `${human && t.state !== "submitted" ? `<button class="key" data-do="done">MARK DONE</button>` : ""}${t.state === "needs_you" ? `<button class="key" data-do="queued">BACK IN THE QUEUE</button>` : ""}<button class="key warn" data-do="cancelled">CANCEL TASK</button>`
        : t.state !== "done" ? `<button class="key" data-do="queued">QUEUE AGAIN</button>` : ""}<span class="meta">from ${esc(title(t.by || "you"))}</span></div></div>`;
  body.innerHTML = `<div class="stack">${open.map(card).join("") || `<div class="empty">Nothing open for the ${esc(s.title)}.</div>`}
    <div class="item"><h4>Give the ${esc(s.title)} a task</h4>
      <div class="formgrid" style="margin-top:8px"><span>Task</span><input data-f="title" placeholder="One outcome, e.g. “A theme on Piano, bars 5-12”">
      <span>Detail</span><textarea data-f="detail" rows="2" placeholder="Which tracks and bars, what it must fit with, what done sounds like"></textarea>
      <span>After</span><input data-f="deps" placeholder="Task ids that must finish first, e.g. T2, T3 (optional)"></div>
      <div class="row end" style="margin-top:8px">${s.occupant ? "" : `<span class="meta warn">Nobody is seated here: the task waits until someone is.</span>`}<button class="key go" data-do="add">ADD TASK</button></div></div>
    ${closed.map(card).join("")}</div>`;
  body.querySelectorAll("[data-task] [data-do]").forEach((b) => b.onclick = async () => {
    const id = b.closest("[data-task]").dataset.task, state = b.dataset.do, args = { task: id, state };
    if (state === "done") { const r = prompt("What was done? (one or two sentences; the next seat reads it)"); if (r === null) return; args.result = r; }
    if (state === "cancelled" && !confirm(`Cancel ${id}?`)) return;
    await call("task_update", args);
  });
  body.querySelector('[data-do="add"]').onclick = async () => {
    const f = (k) => body.querySelector(`[data-f="${k}"]`).value.trim();
    if (!f("title")) { toast("Give the task a title"); return; }
    await call("task_add", { seat: s.seat, title: f("title"), detail: f("detail"), depends: f("deps") ? f("deps").split(/[\s,]+/).filter(Boolean) : [] });
    body.querySelectorAll("[data-f]").forEach((el) => el.value = ""); renderBench(s.seat, true);
  };
}
function benchTakes(body, s, takes) {
  if (!takes.length) { body.innerHTML = `<div class="empty">${s.mode === "direct" ? `The ${esc(s.title)} works on the session directly, so there are no takes to audition.` : `No takes waiting. When the ${esc(s.title)} hands one in, it shows here and on Needs You.`}</div>`; return; }
  body.innerHTML = `<div class="stack">${takes.slice().reverse().map((t) => {
    const d = ST.diffs[t.id], out = new Set(((d && d.outside_seat) || []).map((x) => x.split(":")[0]));
    const conf = d && d.session_moved && d.session_moved.conflicts || [];
    return `<div class="item ${t.state === "submitted" ? "hot" : ""}" data-take="${esc(t.id)}">
      <h4><span class="id">${esc(t.id)}</span>${esc(t.name || "")}<span class="tag ${t.state === "submitted" ? "cy" : "stale"}">${t.state === "submitted" ? "handed in" : "in progress"}</span>${t.returns ? `<span class="tag">returned ${t.returns}×</span>` : ""}${t.task ? `<span class="tag">${esc(t.task)}</span>` : ""}</h4>
      ${t.summary ? `<p>${esc(t.summary)}</p>` : ""}
      ${d ? `<ul class="changes">${d.changes.map((c) => `<li class="${out.has(c.where) || !s.owns.includes(c.kind) ? "out" : ""}"><b>${esc(c.where)}</b> ${esc(c.what)}</li>`).join("") || `<li>No changes yet.</li>`}
        ${conf.map((c) => `<li class="conf">conflict · ${esc(c.where)}: ${esc(c.why || "")}</li>`).join("")}</ul>
        ${d.session_moved && !conf.length ? `<div class="meta">The session has moved on since this take was cut (${d.session_moved.changes} change${d.session_moved.changes === 1 ? "" : "s"}); they merge cleanly.</div>` : ""}`
        : `<div class="meta">reading what changed…</div>`}
      <div class="row" style="margin-top:10px">${abKeys(t.id)}<span class="spacer"></span>
        <button class="key go" data-do="accept">ACCEPT</button><button class="key" data-do="return">RETURN…</button><button class="key warn" data-do="discard">DISCARD</button></div>
      ${conf.length ? `<div class="row" style="margin-top:8px"><span class="meta bad">Accepting hits ${conf.length} conflict${conf.length === 1 ? "" : "s"}:</span><button class="key" data-do="take">ACCEPT · TAKE WINS</button><button class="key" data-do="session">ACCEPT · SESSION WINS</button></div>` : ""}
      <div class="ret hidden"><textarea rows="2" placeholder="What has to change: bars, tracks, what you heard"></textarea><div class="row end" style="margin-top:6px"><button class="key go" data-do="send">SEND IT BACK</button></div></div></div>`;
  }).join("")}</div>`;
  wireAb(body);
  for (const t of takes) if (ST.diffs[t.id] === undefined) {
    ST.diffs[t.id] = null;
    api("take_diff", { take: t.id }).then((d) => { ST.diffs[t.id] = d; renderBench(s.seat); }).catch(() => { delete ST.diffs[t.id]; });
  }
  body.querySelectorAll("[data-take] [data-do]").forEach((b) => b.onclick = async () => {
    const el = b.closest("[data-take]"), take = el.dataset.take, act = b.dataset.do;
    if (act === "return") { el.querySelector(".ret").classList.toggle("hidden"); el.querySelector("textarea").focus(); return; }
    if (act === "send") { const reason = el.querySelector("textarea").value.trim(); if (!reason) { toast("Say what has to change so the seat can act on it"); return; } await call("take_reject", { take, reason }); return; }
    if (act === "discard") { if (confirm(`Discard ${take}? Its work stays on disk but is not merged.`)) await call("take_discard", { take }); return; }
    const r = await call("take_accept", { take, on_conflict: act === "accept" ? "fail" : act }, "merging " + take + "…");
    if (!r.accepted) { toast(`${take} conflicts with the session in ${r.conflicts.length} place${r.conflicts.length === 1 ? "" : "s"}. Choose which side wins, or return it.`); delete ST.diffs[take]; renderBench(s.seat, true); }
    else toast(`${take} merged into the session (one undo step).`, true);
  });
}
function logHtml(lines) { return lines.map((l) => `<div class="${l.cls}"><time>${clock(l.at)}</time>${l.html}</div>`).join(""); }
function benchLog(body, a) {
  const stick = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  body.innerHTML = a.lines.length ? `<div class="log">${logHtml(a.lines.slice(-200))}</div>` : `<div class="empty">Nothing has happened in this seat yet.</div>`;
  if (stick) body.scrollTop = body.scrollHeight;
}
function benchSetup(body, s) {
  body.innerHTML = `<div class="formgrid">
      <span>In the seat</span><div class="row"><b>${s.occupant ? esc(s.label || s.occupant.kind) : "nobody"}</b><button class="key" data-do="pick">CHANGE…</button></div>
      <span>Works</span><select data-f="mode"><option value="propose" ${s.mode === "propose" ? "selected" : ""}>in takes: nothing is heard until you or the Producer accept it</option><option value="direct" ${s.mode === "direct" ? "selected" : ""}>directly on the session: changes are live at once</option></select>
      <span>Standing notes</span><textarea data-f="notes" rows="3" placeholder="Instructions this seat always gets, e.g. “no brass”, “keep it under 8 voices”">${esc(s.notes || "")}</textarea>
      <span>Department</span><div class="muted">${s.owns.length ? esc(s.owns.join(", ")) : "changes nothing: listens and reports"} · suggested model tier: ${esc(s.tier)}</div>
    </div><div class="row end" style="margin-top:10px"><button class="key go" data-do="save">SAVE</button></div>`;
  body.querySelector('[data-do="pick"]').onclick = () => openSeatPicker(s.seat);
  body.querySelector('[data-do="save"]').onclick = async () => {
    await call("seat_assign", { seat: s.seat, mode: body.querySelector('[data-f="mode"]').value, notes: body.querySelector('[data-f="notes"]').value });
    toast("Saved. It applies from the seat's next run.", true); renderBench(s.seat, true);
  };
}

// ---------------------------------------------------------------- who sits in a seat
async function openSeatPicker(seat) {
  const s = seatOf(seat);
  if (!ST.agents) { try { status("looking for agent apps…", "busy"); ST.agents = await api("crew_detect"); } catch (e) { toast(e.message); } status(""); }
  const cur = s.occupant || {}, runners = (ST.agents && ST.agents.runners) || [];
  const row = (val, label, tags = "", extra = "") => `<label><input type="radio" name="occ" value="${esc(val)}" ${extra}><span class="m">${label}</span>${tags}</label>`;
  const box = modal(`<h2>${esc(s.title)} · who sits here</h2>
    <p class="hint">${esc(s.purpose)} Suggested model tier: <b>${esc(s.tier)}</b>.</p>
    <div class="pick">
      <h5>People and apps</h5>
      ${row("none", "Nobody (leave it empty)", "", !s.occupant ? "checked" : "")}
      ${row("human", "You: do this seat's work yourself and hand it in", "", cur.kind === "human" ? "checked" : "")}
      ${row("external", "An outside app (Claude Desktop, Codex…) that joins over MCP with seat_join", "", cur.kind === "external" ? "checked" : "")}
      ${runners.map((r) => `<h5>${esc(r.name)}${!r.available ? `<span class="tag bad">not installed</span>` : r.auth === "missing" ? `<span class="tag bad">not signed in</span>` : `<span class="tag ok">ready</span>`}</h5>
        ${r.auth === "missing" && r.notes && r.notes[0] ? `<div class="meta muted" style="padding:0 6px 4px">${esc(r.notes[0])}</div>` : ""}
        ${(r.available ? r.models : []).map((m) => row(`${r.id}|${m.id}`, esc(m.label || m.id), `<span class="tag">${esc(m.tier || "")}</span><span class="tag ${m.cost === "free" ? "ok" : ""}">${esc(m.cost || "")}</span>`,
          cur.kind === "agent" && cur.runner === r.id && cur.model === m.id ? "checked" : "")).join("")}`).join("")}
    </div>
    <div class="formgrid" style="margin-top:12px"><span>Effort</span><select id="spEffort"><option value="">default</option></select>
      <span>Works</span><select id="spMode"><option value="propose">in takes (proposes; you or the Producer accept)</option><option value="direct">directly on the session</option></select></div>
    <div class="row"><button class="ghost" id="spScan" title="Look again for installed agent apps and their models">Rescan apps</button><span class="spacer"></span><button class="ghost" id="spCancel">Cancel</button><button class="key go" id="spOk">SEAT</button></div>`);
  $("#spMode").value = s.mode;
  const efforts = () => {
    const v = (box.querySelector('input[name="occ"]:checked') || {}).value || "", [rid, mid] = v.split("|");
    const r = runners.find((x) => x.id === rid), m = r && r.models.find((x) => x.id === mid), list = (m && m.efforts) || (r && r.efforts) || [];
    $("#spEffort").innerHTML = `<option value="">default</option>` + list.map((e) => `<option ${cur.effort === e && cur.runner === rid ? "selected" : ""}>${esc(e)}</option>`).join("");
    $("#spEffort").disabled = !list.length;
  };
  box.querySelectorAll('input[name="occ"]').forEach((i) => i.onchange = efforts); efforts();
  const sel = box.querySelector('input[name="occ"]:checked'); if (sel) sel.scrollIntoView({ block: "center" });
  $("#spCancel").onclick = closeModal;
  $("#spScan").onclick = async () => { status("scanning for agent apps…", "busy"); try { ST.agents = await api("crew_detect", { refresh: true }); } catch (e) { toast(e.message); } status(""); openSeatPicker(seat); };
  $("#spOk").onclick = async () => {
    const v = (box.querySelector('input[name="occ"]:checked') || {}).value; if (!v) { toast("Choose who sits here"); return; }
    const [rid, mid] = v.split("|"), args = { seat, mode: $("#spMode").value };
    if (mid) Object.assign(args, { kind: "agent", runner: rid, model: mid, effort: $("#spEffort").value || null }); else args.kind = v;
    closeModal(); await call("seat_assign", args);
  };
}

// ---------------------------------------------------------------- A/B audition
function markAb() { document.querySelectorAll("[data-ab]").forEach((b) => b.classList.toggle("playing", AB.key === b.dataset.ab)); }
AB.el.onended = () => { AB.key = null; markAb(); };
function hush() { if (!AB.el.paused) AB.el.pause(); AB.key = null; markAb(); }
async function audition(take) {
  const key = take || "session";
  if (AB.key === key) { hush(); return; }
  const at = AB.key && !AB.el.paused ? AB.el.currentTime : 0;  // switching A <-> B keeps your place
  const meta = take && ST.board.takes.find((t) => t.id === take), sig = take ? (meta ? takeSig(meta) : "gone") : String(S.version);
  let c = AB.cache[key];
  try {
    if (!c || c.sig !== sig) {
      status(`rendering ${take || "the session"}…`, "busy");
      const r = await api("take_audition", { take: take || null });
      c = AB.cache[key] = { sig, file: r.file }; status("");
      if (r.warnings && r.warnings.length) toast(`${take || "Session"}: ${r.warnings[0]}${r.warnings.length > 1 ? ` (+${r.warnings.length - 1} more)` : ""}`, true);
    }
    if (typeof stop === "function") stop(false);
    AB.el.src = "/api/audio?file=" + encodeURIComponent(c.file) + "&v=" + encodeURIComponent(c.sig);
    AB.el.onloadedmetadata = () => { if (at && at < AB.el.duration) AB.el.currentTime = at; };
    AB.key = key; markAb();
    await AB.el.play();
  } catch (e) { status(""); AB.key = null; markAb(); toast(e.message); }
}

// ---------------------------------------------------------------- Needs You
function renderNeeds() {
  const box = $("#needsList"), items = ST.board ? ST.board.needs_you : [], seen = new Set(items.map((a) => a.id));
  for (const id of Object.keys(ST.tickets)) if (!seen.has(id)) { ST.tickets[id].remove(); delete ST.tickets[id]; }
  let empty = box.querySelector(".empty");
  if (!items.length) { if (!empty) box.appendChild(h(`<div class="empty">Nothing is waiting on you.</div>`)); return; }
  if (empty) empty.remove();
  for (const a of items) {
    if (ST.tickets[a.id]) continue;
    const audition = !!a.take;
    const el = h(`<div class="ticket ${esc(a.kind)}">
      <div class="from"><span>${esc(a.from === "you" ? "Floor" : title(a.from))} · ${esc(a.kind)}${a.task ? " · " + esc(a.task) : ""}</span><span>${esc(a.id)}</span></div>
      <h3>${esc(a.title)}</h3>${a.detail ? `<p>${esc(a.detail)}</p>` : ""}
      <div class="opts">${audition ? `${abKeys(a.take)}<button class="key go" data-opt="a">ACCEPT</button><button class="key" data-ret>RETURN…</button>`
        : a.options.map((o) => `${o.take ? `<button class="key" data-ab="${esc(o.take)}" title="Hear ${esc(o.take)}">▶</button>` : o.audio ? `<button class="key" data-audio="${esc(o.audio)}" title="Hear it">▶</button>` : ""}<button class="key" data-opt="${esc(o.id)}" title="${esc(o.detail || "")}">${esc(o.label)}</button>`).join("")
          + (a.kind === "blocker" && !a.options.length ? `<button class="key" data-say="Sorted - try again.">TRY AGAIN</button>` : "")}</div>
      <textarea rows="2" class="${audition ? "hidden" : ""}" placeholder="${audition ? "What has to change: bars, tracks, what you heard" : "Add a note, or answer in your own words"}"></textarea>
      <div class="opts ${audition ? "hidden" : ""}" data-send><button class="key" data-go>${audition ? "SEND IT BACK" : "SEND ANSWER"}</button></div>
      <div class="err hidden"></div></div>`);
    const text = el.querySelector("textarea"), err = el.querySelector(".err");
    const resolve = async (option, say) => {
      err.classList.add("hidden");
      try { await api("attention_resolve", { item: a.id, option: option || null, text: say ?? text.value.trim() }); refresh(true); }
      catch (e) { err.textContent = e.message; err.classList.remove("hidden"); refresh(true); }
    };
    el.querySelectorAll("[data-opt]").forEach((b) => b.onclick = () => resolve(b.dataset.opt));
    el.querySelectorAll("[data-say]").forEach((b) => b.onclick = () => resolve(null, text.value.trim() || b.dataset.say));
    el.querySelectorAll("[data-audio]").forEach((b) => b.onclick = () => { hush(); playFile(b.dataset.audio); });
    const ret = el.querySelector("[data-ret]");
    if (ret) ret.onclick = () => { text.classList.remove("hidden"); el.querySelector("[data-send]").classList.remove("hidden"); text.focus(); };
    el.querySelector("[data-go]").onclick = () => {
      if (!text.value.trim()) { err.textContent = audition ? "Say what has to change so the seat can act on it." : "Write an answer, or choose an option."; err.classList.remove("hidden"); return; }
      resolve(audition ? "b" : null);
    };
    wireAb(el);
    box.appendChild(el); ST.tickets[a.id] = el;
  }
}

// ---------------------------------------------------------------- monitor / talkback
function renderMonitor() {
  const box = $("#tbLog"), stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const lines = ST.talkOnly ? ST.feed.filter((l) => l.cls.startsWith("say")) : ST.feed;
  const sig = lines.length + ":" + ST.talkOnly + ":" + (lines.length ? lines[lines.length - 1].at : 0);
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.innerHTML = lines.length ? `<div class="log">${logHtml(lines.slice(-250))}</div>` : `<div class="log"><div>${ST.board ? "The room is quiet. Brief the Producer, or talk to a seat below." : "No session open."}</div></div>`;
    if (stick) box.scrollTop = box.scrollHeight;
  }
  const tg = $("#tbTargets");
  if (ST.board) {
    const want = ST.board.seats.map((s) => s.seat + (s.occupant ? 1 : 0)).join() + ST.target + ST.wake;
    if (tg.dataset.sig !== want) {
      tg.dataset.sig = want;
      tg.innerHTML = ST.board.seats.filter((s) => s.occupant || !s.optional).map((s) => `<button type="button" class="key ${ST.target === s.seat ? "on" : ""} ${s.occupant ? "" : "vac"}" data-to="${s.seat}" title="${esc(s.title)}${s.occupant ? "" : " (nobody seated)"}">${SHORT[s.seat]}</button>`).join("")
        + `<button type="button" class="key ${ST.target === "all" ? "on" : ""}" data-to="all" title="Every seat">ALL</button><span class="spacer"></span>
           <button type="button" class="key toggle ${ST.wake ? "on" : ""}" data-wake title="When the seat's agent is idle, hand it your message as a task so it answers now (one agent run). Off: it reads the message when it next works.">WAKE</button>`;
      tg.querySelectorAll("[data-to]").forEach((b) => b.onclick = () => { ST.target = b.dataset.to; renderMonitor(); $("#tbText").focus(); });
      tg.querySelector("[data-wake]").onclick = () => { ST.wake = !ST.wake; renderMonitor(); };
      $("#tbText").placeholder = ST.target === "all" ? "Talk to everyone…" : `Talk to the ${title(ST.target)}…`;
    }
  } else tg.innerHTML = "";
}
$("#tbFilter").onclick = () => { ST.talkOnly = !ST.talkOnly; $("#tbFilter").classList.toggle("on", ST.talkOnly); renderMonitor(); };
$("#tbForm").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("#tbText").value.trim(); if (!text || !ST.board) return;
  const to = ST.target, s = seatOf(to);
  try {
    await api("message_send", { to, text });
    $("#tbText").value = "";
    // An idle agent only reads its messages when it is started on something: WAKE gives it that something.
    if (ST.wake && s && s.occupant && s.occupant.kind === "agent" && !s.open_tasks) {
      await api("task_add", { seat: to, title: "Answer the user on talkback", detail: `The user said to you on talkback:\n\n${text}\n\nDo what it asks if it is in your department, or answer with message_send("you", ...). Then set this task done with a one-line result.` });
      if (!(ST.crew && (ST.crew.running || ST.crew.elsewhere))) toast(`The ${s.title} will answer when the crew is running (RUN).`, true);
    } else if (s && !s.occupant) toast(`Nobody is in the ${s.title} seat: the message waits for whoever sits there.`, true);
    refresh(true);
  } catch (err) { toast(err.message); }
};

// ---------------------------------------------------------------- wiring
function render() {
  renderHeader();
  if (document.body.dataset.room !== "studio") return;
  renderUnits(); renderNeeds(); renderMonitor();
}
window.Studio = {
  set(board, crew) {
    ST.board = board; ST.crew = crew;
    const project = board ? board.session : null;
    if (project !== ST.project) {
      ST.project = project; ST.units = {}; $("#units").innerHTML = ""; $("#needsList").innerHTML = ""; ST.tickets = {}; AB.cache = {}; hush();
      const open = store.get("open", ""); ST.open = board && board.seats.some((s) => s.seat === open) ? open : null;
      connect();
    }
    render();
  },
  render, hush,
};
setRoom(store.get("room", "studio") === "desk" ? "desk" : "studio");
if (S.path || S.doc) loadState();  // app.js loaded the state before this file defined Studio
})();
