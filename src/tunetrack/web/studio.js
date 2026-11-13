// TuneTrack Studio room: the crew rack (one unit per seat), Needs You, and the monitor/talkback section.
// It draws from the board (/api/state, studio_view) and follows /api/events, so every seat's work shows up live.
// Uses the helpers app.js defines: $, S, api, status, toast, esc, modal, closeModal, loadState, renderAll, stop,
// and for takes on the desk: openTake, cutTake, renderTakeBar.
"use strict";
(() => {
const SHORT = { producer: "PROD", spotting: "SPOT", composer: "COMP", arranger: "ARR", rhythm: "RHY", sound: "SND", tracking: "TRK", mix: "MIX", review: "REV" };
const TABS = ["bench", "tasks", "takes", "log", "setup"];
// The first tab is the seat's own bench: its work in the form that seat reads it in (bench.py decides which).
const VIEW = { producer: "plan", spotting: "plan", composer: "notes", arranger: "notes", rhythm: "notes", tracking: "notes", sound: "sound", mix: "mix", review: "report" };
const BENCH_NAME = { plan: "plan", notes: "roll", sound: "sounds", mix: "console", report: "report" };
const DESK_TAB = { plan: "brief", notes: "roll", sound: "inspector", mix: "mixer", report: "report" };
const OPEN_STATES = ["queued", "working", "submitted", "needs_you"];
const ST = {
  board: null, crew: null, project: null, open: null, tab: {}, units: {}, act: {}, feed: [], lastSeq: 0, es: null,
  target: "producer", wake: true, talkOnly: false, diffs: {}, agents: null, tickets: {}, benchSig: {}, bench: {},
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
const isLong = (html) => html.length > 340 || (html.match(/\n/g) || []).length > 3;  // folded in the log; click to read
function line(seat, at, cls, html, plain) {
  const a = seatAct(seat); a.lines.push({ at, cls: cls + (isLong(html) ? " long" : ""), html }); if (a.lines.length > 300) a.lines.splice(0, 100);
  if (plain) a.last = plain;
}
function feed(at, cls, html) { ST.feed.push({ at, cls: cls + (isLong(html) ? " long" : ""), html }); if (ST.feed.length > 500) ST.feed.splice(0, 150); }
// Whatever was said in full: older events carry a shortened copy, the board keeps the whole message.
const fullText = (e) => { const m = e.id && ST.board && ST.board.messages.find((x) => x.id === e.id); return (m && m.text) || e.text || ""; };
document.addEventListener("click", (ev) => {
  const el = ev.target.closest && ev.target.closest(".log div.long"); if (!el) return;
  const t = el.querySelector("time"), box = modal(`<h2>${t ? esc(t.textContent) : ""}</h2><div class="reader"></div><div class="row"><button class="ghost" id="rdClose">Close</button></div>`);
  const body = el.cloneNode(true); if (body.firstElementChild && body.firstElementChild.tagName === "TIME") body.firstElementChild.remove();
  box.querySelector(".reader").innerHTML = body.innerHTML; $("#rdClose").onclick = closeModal;
});
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
      const text = fullText(e);
      feed(e.at, "say" + (e.sender === "you" ? " you" : ""), `<i>${esc(title(e.sender))} → ${esc(title(e.to))}</i>  ${esc(text)}`);
      for (const s of [e.sender, e.to]) if (SHORT[s]) line(s, e.at, "say", `<i>${esc(title(e.sender))} → ${esc(title(e.to))}:</i> ${esc(text)}`);
    } else if (e.kind === "attention") {
      feed(e.at, e.state === "open" ? "note" : "", e.state === "open" ? `Needs you: ${esc(e.title)}` : `Answered: ${esc(e.title)}${e.answer ? " → " + esc(e.answer) : ""}`);
    } else if (e.kind === "report") {
      const t = `filed a ${esc(e.what || "report")}: <u data-report="${esc(e.id)}" title="Read it">${esc(e.title)}</u>`;
      feed(e.at, "good", `<b>${esc(title(e.sender))}</b> ${t}`);
      if (SHORT[e.sender]) line(e.sender, e.at, "good", t);
    } else if (e.kind === "crew") {
      feed(e.at, e.error ? "bad" : "note", e.what === "start" ? "Crew running" : `Crew stopped${e.error ? ": " + esc(e.error) : ""}`);
    } else if (e.kind === "seat") {
      feed(e.at, "", `${who} seat: ${esc(e.occupant)}${e.paused ? " · on hold" : ""}`);
    }
  }
  // the open seat's bench follows its work: every tool call may have changed what it shows
  if (ST.open && events.some((e) => e.seat === ST.open || e.kind === "take")) benchSoon();
}
async function connect() {
  if (ST.es) { ST.es.close(); ST.es = null; }
  S.live = false;
  Object.assign(ST, { act: {}, feed: [], lastSeq: 0, diffs: {}, benchSig: {}, bench: {} });
  const project = ST.project;
  if (!project) return;
  try { ingest((await api("events", { since: 0, limit: 500 })).events); } catch { /* no board yet */ }
  if (project !== ST.project) return;
  render();
  listen();
}
// The live stream: the session's file and board, plus the take the desk has open (app.js calls this again when
// the desk moves to another take, so the stream follows it).
function listen() {
  if (ST.es) { ST.es.close(); ST.es = null; }
  S.live = false;
  if (!ST.project) return;
  const es = ST.es = new EventSource("/api/events?since=" + ST.lastSeq + (S.take ? "&take=" + encodeURIComponent(S.take) : ""));
  es.onopen = () => { S.live = true; };
  es.onerror = () => { S.live = false; };
  es.onmessage = (m) => {
    if (es !== ST.es) return;
    const msg = JSON.parse(m.data), ev = msg.events || [];
    const stale = msg.project !== S.path || msg.version !== S.sessionVersion
      || (S.take && msg.take && msg.take.id === S.take && msg.take.version !== S.version)
      || (S.take && ev.some((e) => e.kind === "take" && e.id === S.take && e.seq > ST.lastSeq));
    if (stale && !S.pendingSave && !S.drag) loadState();
    if (ev.length && msg.project === ST.project) { ingest(ev); refresh(); }
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
  const reps = ST.board ? (ST.board.reports || []).length : 0;
  $("#repBtn").textContent = reps ? `REPORTS ${reps}` : "REPORTS"; $("#repBtn").classList.toggle("on", reps > 0); $("#repBtn").disabled = !ST.board;
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
    if (t.not_before) return ["queued", "dimamber", "TRIES AGAIN " + new Date(t.not_before * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })];
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
      <div class="bench hidden"><div class="tabsB">${TABS.map((t) => `<button data-tab="${t}">${t === "bench" ? BENCH_NAME[VIEW[s.seat]] : t}</button>`).join("")}<span class="purpose"></span></div><div class="benchBody"></div></div>
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
// What a seat's workstation opens on: a take waiting for your ears, your own next task, otherwise the work itself.
function tabOf(s) {
  if (ST.tab[s.seat]) return ST.tab[s.seat];
  if (ST.board.takes.some((t) => t.seat === s.seat && t.state === "submitted")) return "takes";
  const yours = s.occupant && s.occupant.kind === "human" && ST.board.tasks.some((t) => t.seat === s.seat && t.state === "queued");
  return yours ? "tasks" : "bench";
}
function renderBench(seat, force = false) {
  const u = ST.units[seat], s = seatOf(seat); if (!u || !s) return;
  const tab = tabOf(s);
  u.querySelectorAll(".tabsB button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const body = u.querySelector(".benchBody");
  const tasks = ST.board.tasks.filter((t) => t.seat === seat), takes = ST.board.takes.filter((t) => t.seat === seat), a = seatAct(seat);
  const B = benchOf(seat);
  const sig = JSON.stringify(tab === "bench" ? [tab, B.version, B.err, [...B.hidden], body.clientWidth, s.seat === "producer" ? ST.board.tasks.map((t) => [t.id, t.state, t.depends]) : 0]
    : tab === "tasks" ? [tab, tasks, s.occupant, takes.map((t) => [t.id, t.state, t.reason])] : tab === "takes" ? [tab, takes, Object.fromEntries(takes.map((t) => [t.id, ST.diffs[t.id]]))]
    : tab === "log" ? [tab, a.lines.length] : [tab, s.occupant, s.mode, s.notes, s.paused]);
  if (!force && ST.benchSig[seat] === sig) return;
  if (!force && focusedIn(body)) return;  // don't pull a form out from under someone typing; focusout redraws
  ST.benchSig[seat] = sig;
  if (tab === "bench") benchWork(body, s);
  else if (tab === "tasks") benchTasks(body, s, tasks);
  else if (tab === "takes") benchTakes(body, s, takes);
  else if (tab === "log") benchLog(body, a);
  else benchSetup(body, s);
}
// ---------------------------------------------------------------- the seat's bench: its work, in its own form
function benchOf(seat) { return ST.bench[seat] || (ST.bench[seat] = { data: null, version: null, take: null, hidden: new Set(), err: null, busy: false }); }
async function loadBench(seat) {
  const B = benchOf(seat); if (B.busy) return; B.busy = true;
  try {
    const r = await api("seat_bench", { seat, take: B.take, have: B.version });
    if (!r.unchanged) { B.data = r; B.version = r.version; }
    B.err = null;
  } catch (e) {
    if (B.take) { B.take = null; B.version = null; }  // the take it was pinned to is gone: back to the seat's own
    else B.err = e.message;
  }
  B.busy = false;
  if (ST.open === seat) renderBench(seat);
}
let benchTimer = null;
function benchSoon() { clearTimeout(benchTimer); benchTimer = setTimeout(() => { if (ST.open && ST.board && tabOf(seatOf(ST.open)) === "bench") loadBench(ST.open); }, 400); }
// someone may be editing the take on the desk or from another app: a cheap "anything new?" while a bench is open
setInterval(() => { if (!document.hidden && document.body.dataset.room === "studio") benchSoon(); }, 2500);
window.addEventListener("resize", () => { if (ST.open) renderBench(ST.open); });

const faderPos = (db) => { const v = Math.max(-60, Math.min(12, +db || 0)); return v >= -20 ? 30 + (v + 20) / 32 * 70 : (v + 60) / 40 * 30; };  // % up the slot
const panName = (p) => Math.abs(p) < 0.005 ? "C" : (p < 0 ? "L" : "R") + Math.round(Math.abs(p) * 100);
const dbName = (v) => (v > 0 ? "+" : "") + (+v).toFixed(1);
function benchWork(body, s) {
  const B = benchOf(s.seat), d = B.data;
  if (!d) { body.innerHTML = `<div class="empty">${B.err ? esc(B.err) : "Reading the work…"}</div>`; if (!B.err) loadBench(s.seat); return; }
  const src = d.source, shown = src.take || "session";
  const pick = d.takes.length ? [...d.takes, "session"].map((id) => `<button class="mini ${shown === id ? "on" : ""}" data-src="${esc(id)}" title="${id === "session" ? "The music as accepted" : "This seat's take " + esc(id)}">${esc(id.toUpperCase())}</button>`).join("") : "";
  body.innerHTML = `<div class="work">
    <div class="workBar">
      <span class="vfd small">${src.take ? `${esc(src.take.toUpperCase())} · ${src.state === "submitted" ? "HANDED IN" : "IN PROGRESS"} · <span class="amb">${d.changes} CHANGE${d.changes === 1 ? "" : "S"}</span>` : `SESSION · REV ${esc(src.rev ?? "?")}`}</span>
      ${src.task ? `<span class="tag">${esc(src.task)}</span>` : ""}${src.returns ? `<span class="tag stale">RETURNED ${src.returns}×</span>` : ""}
      <span class="srcs">${pick}</span><span class="spacer"></span>
      ${src.take ? abKeys(src.take) : ""}<button class="key" data-do="desk" title="Open this on the desk to edit it">OPEN ON DESK</button>
    </div><div class="workBody"></div>${d.truncated ? `<div class="meta">Too many notes to draw them all here: open it on the desk for the rest.</div>` : ""}</div>`;
  const box = body.querySelector(".workBody");
  ({ notes: workRoll, mix: workConsole, sound: workSounds, plan: workPlan, report: workReport })[d.view](box, d, B, s);
  wireAb(body);
  body.querySelectorAll("[data-src]").forEach((b) => b.onclick = () => { B.take = b.dataset.src; B.version = null; loadBench(s.seat); });
  body.querySelector('[data-do="desk"]').onclick = async () => {
    hush(); await openTake(src.take || null);
    const moved = d.tracks.find((t) => t.new || t.was || (t.notes || []).some((n) => n[4])) || d.tracks.find((t) => t.kind === "midi");
    deskShow(DESK_TAB[d.view], moved && moved.id);
  };
}
// Composer, Arranger, Rhythm, Tracking: every part on one roll; what the take added is lit, what it removed is outlined.
function workRoll(box, d, B, s) {
  const parts = d.tracks.map((t, i) => ({ t, col: trackColor(t, i) })).filter((x) => x.t.kind !== "bus");
  box.innerHTML = `<div class="rollB"><canvas></canvas></div><div class="chips">${parts.map(({ t, col }) => {
    const add = (t.notes || []).filter((n) => n[4]).length, gone = (t.gone || []).length;
    return `<button class="chip ${B.hidden.has(t.id) ? "off" : ""}" data-trk="${esc(t.id)}" title="Show or hide this part"><i style="background:${col}"></i>${esc(t.name)}<span>${esc(t.instrument || t.kind)}</span>${t.new ? `<b>new</b>` : ""}${add && d.source.take ? `<b>+${add}</b>` : ""}${gone ? `<em>−${gone}</em>` : ""}${!(t.notes || []).length ? `<span>silent</span>` : ""}</button>`;
  }).join("")}</div>`;
  box.querySelectorAll("[data-trk]").forEach((b) => b.onclick = () => { B.hidden.has(b.dataset.trk) ? B.hidden.delete(b.dataset.trk) : B.hidden.add(b.dataset.trk); renderBench(s.seat, true); });
  const cv = box.querySelector("canvas"), wrap = cv.parentElement, f = d.form, inTake = !!d.source.take;
  const vis = parts.filter((x) => !B.hidden.has(x.t.id));
  let lo = 127, hi = 0;
  for (const { t } of vis) for (const n of [...(t.notes || []), ...(t.gone || [])]) { lo = Math.min(lo, n[0]); hi = Math.max(hi, n[0]); }
  if (lo > hi) { lo = 55; hi = 79; }
  lo = Math.max(0, lo - 2); hi = Math.min(127, hi + 2);
  if (hi - lo < 23) { const p = Math.ceil((23 - (hi - lo)) / 2); lo = Math.max(0, lo - p); hi = Math.min(127, hi + p); }
  const rows = hi - lo + 1, rh = Math.max(3, Math.min(9, Math.floor(240 / rows))), GUT = 34, TOP = 36;
  const end = Math.max(f.end, f.bpb * 4), W = Math.max(wrap.clientWidth - 2, GUT + end * 6), H = TOP + rows * rh + 6;
  const g = fitCanvas(cv, W, H), X = (b) => GUT + b * (W - GUT - 6) / end, Y = (p) => TOP + (hi - p) * rh;
  g.fillStyle = "#07090a"; g.fillRect(0, 0, W, H);
  for (let p = lo; p <= hi; p++) {
    if ([1, 3, 6, 8, 10].includes(p % 12)) { g.fillStyle = "rgba(255,255,255,.022)"; g.fillRect(GUT, Y(p), W, rh); }
    if (p % 12 === 0) { g.fillStyle = "#1e2422"; g.fillRect(GUT, Y(p) + rh - 1, W, 1); g.fillStyle = "#5a564d"; g.font = "9px Consolas, monospace"; g.fillText(noteName(p), 6, Y(p) + rh); }
  }
  if (f.loop) { g.fillStyle = "rgba(95,224,122,.5)"; g.fillRect(X(f.loop.start), 0, X(f.loop.end) - X(f.loop.start), 2); }
  const barW = X(f.bpb) - X(0), every = barW >= 26 ? 1 : barW >= 13 ? 2 : 4;
  g.font = "9px Consolas, monospace";
  for (let b = 0, bar = 0; b <= end + 1e-6; b += f.bpb, bar++) {
    g.fillStyle = bar % 4 ? "#141918" : "#232a28"; g.fillRect(Math.round(X(b)), 14, 1, H);
    if (bar % every === 0 && b < end) { g.fillStyle = "#5a564d"; g.fillText(bar + 1, X(b) + 3, 11); }
  }
  for (const m of f.markers) { g.fillStyle = "#ffb000"; g.fillRect(Math.round(X(m.at)), 0, 1, H); g.font = "9px Bahnschrift, 'Segoe UI', sans-serif"; g.fillText(String(m.name).toUpperCase(), X(m.at) + 16, 11); }
  g.font = "10px Consolas, monospace";
  for (const c of f.chords) { g.fillStyle = "#0d1614"; g.fillRect(X(c.at) + 1, 17, X(c.at + c.len) - X(c.at) - 2, 14); g.fillStyle = "#74f0d6"; g.fillText(c.name, X(c.at) + 5, 28); }
  for (const { t, col } of vis) {
    for (const n of t.notes || []) {
      const x = X(n[1]), w = Math.max(2, X(n[1] + n[2]) - x - 1), fresh = n[4] || !inTake;
      g.globalAlpha = fresh ? 0.55 + 0.45 * n[3] / 127 : 0.26; g.fillStyle = col; g.fillRect(x, Y(n[0]) + 0.5, w, Math.max(2, rh - 1));
      if (n[4] && rh >= 5) { g.globalAlpha = 0.5; g.fillStyle = "#fff"; g.fillRect(x, Y(n[0]) + 0.5, w, 1); }
    }
    g.globalAlpha = 0.8; g.strokeStyle = "#ff4a3a"; g.setLineDash([2, 2]);
    for (const n of t.gone || []) g.strokeRect(X(n[1]) + 0.5, Y(n[0]) + 0.5, Math.max(2, X(n[1] + n[2]) - X(n[1]) - 1), Math.max(2, rh - 1));
    g.setLineDash([]); g.globalAlpha = 1;
  }
  if (!vis.some((x) => (x.t.notes || []).length)) { g.fillStyle = "#5a564d"; g.font = "12px 'Segoe UI', sans-serif"; g.fillText(parts.length ? "Nothing written yet." : "No tracks yet.", GUT + 14, TOP + 26); }
}
// Mix: the console. Every moved fader stands next to a dashed outline of where it was.
function workConsole(box, d) {
  const r = d.render, was = (t, k) => t.was && k in t.was;
  const strip = (t, i) => {
    const moved = [], w = t.was || {};
    if ("volume_db" in w) moved.push(`was ${dbName(w.volume_db)}`);
    if ("pan" in w) moved.push(`pan was ${panName(w.pan)}`);
    if ("mute" in w) moved.push(w.mute ? "was muted" : "was unmuted");
    if ("solo" in w) moved.push(w.solo ? "was solo" : "solo now");
    if ("fx" in w) moved.push(`fx was ${w.fx.length ? w.fx.map((f) => f.label).join(", ") : "none"}`);
    if ("sends" in w) moved.push("sends changed"); if ("output" in w) moved.push(`out was ${w.output}`); if ("automation" in w) moved.push("automation changed");
    if (t.new) moved.push("new track");
    const lv = t.level, rms = lv && lv.rms_dbfs != null ? Math.max(0, Math.min(100, (lv.rms_dbfs + 54) / 54 * 100)) : 0, pk = lv && lv.peak_dbfs != null ? Math.max(0, Math.min(100, (lv.peak_dbfs + 54) / 54 * 100)) : null;
    const oldFx = new Set(((w.fx || t.fx).map((f) => f.label)));
    return `<div class="ch ${moved.length ? "moved" : ""}" style="--c:${trackColor(t, i)}">
      <div class="nm" title="${esc(t.name)} · ${esc(t.instrument || t.kind)}">${esc(t.name)}</div>
      <div class="slots">${t.fx.map((f) => `<span class="slot ${f.bypass ? "byp" : ""} ${was(t, "fx") && !oldFx.has(f.label) ? "new" : ""}" title="${esc(f.label)}">${esc(f.label)}</span>`).join("")}${t.sends.map((x) => `<span class="slot send" title="send to ${esc(x.to)}">→ ${esc(x.to)} ${dbName(x.db)}</span>`).join("")}${t.output !== "master" ? `<span class="slot send">out ${esc(t.output)}</span>` : ""}${t.automation.length ? `<span class="slot send">auto ${esc(t.automation.join(", "))}</span>` : ""}</div>
      <div class="well"><div class="lvl" title="${lv ? `last render: peak ${lv.peak_dbfs} dBFS, rms ${lv.rms_dbfs} dBFS` : "not rendered yet"}"><i style="height:${rms}%"></i>${pk != null ? `<u style="bottom:${pk}%"></u>` : ""}</div>
        <div class="slotway"><i class="zero" style="bottom:${faderPos(0)}%"></i>${was(t, "volume_db") ? `<i class="cap ghost" style="bottom:${faderPos(w.volume_db)}%"></i>` : ""}<i class="cap" style="bottom:${faderPos(t.volume_db)}%"></i></div></div>
      <div class="db">${dbName(t.volume_db)}</div>
      <div class="pan" title="pan ${panName(t.pan)}">${was(t, "pan") ? `<i class="ghost" style="left:${(w.pan + 1) * 50}%"></i>` : ""}<i style="left:${(t.pan + 1) * 50}%"></i></div>
      <div class="ms"><b class="m ${t.mute ? "on" : ""}">M</b><span>${panName(t.pan)}</span><b class="s ${t.solo ? "on" : ""}">S</b></div>
      <div class="was" title="${esc(moved.join(" · "))}">${esc(moved[0] || "")}${moved.length > 1 ? ` +${moved.length - 1}` : ""}</div></div>`;
  };
  const m = d.master, bands = r ? Object.entries(r.bands_pct) : [], top = Math.max(1, ...bands.map((b) => b[1]));
  box.innerHTML = d.tracks.length ? `<div class="console">${d.tracks.map(strip).join("")}
    <div class="ch master"><div class="nm">Master</div>
      <div class="slots">${m.fx.map((f) => `<span class="slot ${f.bypass ? "byp" : ""}">${esc(f.label)}</span>`).join("") || `<span class="slot send">no master chain</span>`}</div>
      <div class="bandsB" title="Band balance of the last render">${bands.map(([k, v]) => `<div title="${esc(k)}: ${v}%"><i style="height:${v / top * 100}%"></i><span>${esc(k.replace("_", " "))}</span></div>`).join("") || `<span class="meta">not rendered yet</span>`}</div>
      <div class="db">${r && r.lufs != null ? `${r.lufs} LUFS` : "— LUFS"}</div>
      <div class="ms"><span>target ${esc(m.target_lufs)} · ceil ${esc(m.ceiling_db)}</span></div>
      <div class="ms"><span>${r ? `peak ${esc(r.true_peak_dbtp)} dBTP` : ""}</span></div>
      <div class="was">${r ? `${r.stale ? "render is older than the mix · " : ""}${r.warnings.length ? r.warnings.length + " warning" + (r.warnings.length === 1 ? "" : "s") : "no warnings"}` : ""}${m.was ? " · master changed" : ""}</div></div></div>
    ${r && r.warnings.length ? `<ul class="changes">${r.warnings.slice(0, 6).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}` : `<div class="empty">No tracks to mix yet.</div>`;
}
// Sound: the instrument rack. One slot per part, with what it was before this take, and a key to hear it.
function workSounds(box, d) {
  const midi = d.tracks.map((t, i) => [t, i]).filter(([t]) => t.kind === "midi");
  box.innerHTML = midi.length ? `<div class="stack">${midi.map(([t, i]) => {
    const can = t.instrument && /^(synth|patch|vst3):/.test(t.instrument);
    return `<div class="slotB ${t.new || (t.was && "instrument" in t.was) ? "moved" : ""}" style="--c:${trackColor(t, i)}"><div class="nm">${esc(t.name)}<span>${esc(t.role || "")}</span></div>
      <span class="vfd small">${esc(t.instrument || "no instrument")}</span>${t.was && "instrument" in t.was ? `<span class="wasI">was ${esc(t.was.instrument)}</span>` : t.new ? `<span class="wasI">new track</span>` : ""}
      <span class="spacer"></span><span class="meta">${(t.clips || []).length ? "" : "no part written yet"}</span>${can ? `<button class="key" data-aud="${esc(t.instrument)}" title="Play a short phrase on this instrument">AUDITION</button>` : ""}</div>`;
  }).join("")}</div>` : `<div class="empty">No instrument tracks yet.</div>`;
  box.querySelectorAll("[data-aud]").forEach((b) => b.onclick = async () => {
    try { status("auditioning…", "busy"); const r = await api("audition", { instrument: b.dataset.aud }); status(""); hush(); playFile(r.file); if (r.silent) toast("That audition was silent: the instrument has no sound loaded."); }
    catch (e) { status(""); toast(e.message); }
  });
}
// Producer, Spotting: the plan. Form and harmony over the arrangement, then every task and what it waits on.
function workPlan(box, d) {
  const f = d.form, end = Math.max(f.end, f.bpb), pct = (b) => (b / end * 100).toFixed(3) + "%", bars = Math.round(end / f.bpb);
  const sect = f.markers.map((m, i) => ({ ...m, to: i + 1 < f.markers.length ? f.markers[i + 1].at : end }));
  const lane = (label, inner, cls = "") => `<div class="tlName">${label}</div><div class="tlLane ${cls}">${inner}</div>`;
  const tasks = ST.board.tasks.slice().sort((a, b) => +a.id.slice(1) - +b.id.slice(1));
  box.innerHTML = `<div class="planHead"><span class="vfd small">${esc(String(f.key || "").toUpperCase())} · ${esc(f.tempo)} BPM · ${f.time_sig.join("/")} · ${bars} BAR${bars === 1 ? "" : "S"}${f.loop ? ` · LOOP ${Math.round(f.loop.start / f.bpb) + 1}–${Math.round(f.loop.end / f.bpb) + 1}` : ""}</span>
      ${Object.entries(f.brief).filter(([k]) => ["genre", "mood", "use"].includes(k)).map(([k, v]) => `<span class="tag" title="${esc(k)}">${esc(Array.isArray(v) ? v.join(", ") : v)}</span>`).join("")}</div>
    <div class="tl">${lane("FORM", sect.map((m) => `<i class="sec" style="left:${pct(m.at)};width:${pct(m.to - m.at)}">${esc(m.name)} <span>${Math.round(m.at / f.bpb) + 1}</span></i>`).join("") || `<span class="meta">no sections marked yet</span>`)}
      ${lane("CHORDS", f.chords.map((c) => `<i class="chd" style="left:${pct(c.at)};width:${pct(c.len)}">${esc(c.name)}</i>`).join("") || `<span class="meta">no chord track yet</span>`)}
      ${d.tracks.map((t, i) => lane(esc(t.name), t.clips.map((c) => `<i class="clp" style="left:${pct(c[0])};width:${pct(c[1])};background:${trackColor(t, i)}"></i>`).join("") || (t.kind === "bus" ? `<span class="meta">bus</span>` : `<span class="meta">nothing written</span>`), t.clips.length || t.kind === "bus" ? "" : "bare")).join("")}</div>
    <div class="flow">${tasks.map((t) => `<div class="fl ${OPEN_STATES.includes(t.state) ? "" : "done"}"><span class="id">${esc(t.id)}</span><span class="st">${SHORT[t.seat] || esc(t.seat)}</span><span class="ti" title="${esc(t.detail || "")}">${esc(t.title)}</span>${(t.depends || []).length ? `<span class="dep">after ${esc(t.depends.join(" "))}</span>` : ""}<span class="tag ${TAG[t.state] || ""}">${esc(t.state.replace("_", " "))}</span></div>`).join("") || `<div class="empty">No tasks yet. BRIEF starts the session.</div>`}</div>`;
}
// Review: the last render of what this seat is listening to, as measured.
function workReport(box, d) {
  const r = d.render;
  if (!r) { box.innerHTML = `<div class="empty">Nothing rendered yet. Play or A/B renders it; the measurements land here.</div>`; return; }
  const bands = Object.entries(r.bands_pct), top = Math.max(1, ...bands.map((b) => b[1]));
  box.innerHTML = `<div class="planHead"><span class="vfd small">${esc(r.lufs)} LUFS · PEAK ${esc(r.true_peak_dbtp)} dBTP · ${esc(r.duration_s)} S${r.loop_mode && r.loop_mode !== "off" ? " · " + esc(String(r.loop_mode).toUpperCase()) : ""}</span>
      <span class="tag ${r.stale ? "stale" : "ok"}">${r.stale ? "OLDER THAN THE MUSIC" : "CURRENT"}</span><span class="tag">width ${esc(r.stereo_correlation)}</span><span class="tag">limiter ${esc(r.limiter_db)} dB</span></div>
    <div class="report2"><div class="bandsB tall">${bands.map(([k, v]) => `<div title="${esc(k)}: ${v}%"><i style="height:${v / top * 100}%"></i><span>${esc(k.replace("_", " "))}<br>${v}%</span></div>`).join("")}</div>
      <div class="lvls">${d.tracks.filter((t) => t.level).map((t, i) => `<div><span>${esc(t.name)}</span><div class="hbar"><i style="width:${Math.max(0, Math.min(100, (t.level.rms_dbfs + 54) / 54 * 100))}%;background:${trackColor(t, i)}"></i></div><span class="num">${esc(t.level.rms_dbfs)} rms · ${esc(t.level.peak_dbfs)} pk</span></div>`).join("")}</div></div>
    ${r.warnings.length ? `<ul class="changes">${r.warnings.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<div class="meta ok">No warnings from the last render.</div>`}`;
}

function benchTasks(body, s, tasks) {
  const open = tasks.filter((t) => OPEN_STATES.includes(t.state)), closed = tasks.filter((t) => !OPEN_STATES.includes(t.state)).reverse();
  const human = s.occupant && s.occupant.kind !== "agent";
  // You in a seat that works in takes: do the task on the desk in a take, like any other seat.
  const byTake = s.occupant && s.occupant.kind === "human" && s.mode === "propose" && s.owns.length;
  const openTakeOf = (t) => t.take && ST.board.takes.some((k) => k.id === t.take && k.state === "open") ? t.take : null;
  // a take that was sent back: whoever sits here reads why on the task itself, not in the log
  const cameBack = (t) => { const k = t.take && ST.board.takes.find((x) => x.id === t.take && x.state === "open" && x.returns && x.reason); return k ? `<p class="back"><b>${esc(k.id.toUpperCase())} came back${k.returns > 1 ? ` (${k.returns}×)` : ""}:</b> ${esc(k.reason)}</p>` : ""; };
  const card = (t) => `<div class="item ${OPEN_STATES.includes(t.state) ? "hot" : "done"}" data-task="${esc(t.id)}">
      <h4><span class="id">${esc(t.id)}</span>${esc(t.title)}<span class="tag ${TAG[t.state] || ""}">${esc(t.state.replace("_", " "))}</span>${t.waiting_for ? `<span class="tag">after ${esc(t.waiting_for.join(", "))}</span>` : ""}${t.take ? `<span class="tag cy">${esc(t.take)}</span>` : ""}</h4>
      ${t.detail ? `<p>${esc(t.detail)}</p>` : ""}${t.result ? `<p class="meta">Result: ${esc(t.result)}</p>` : ""}${cameBack(t)}
      <div class="row" style="margin-top:8px">${OPEN_STATES.includes(t.state)
        ? `${byTake && t.state !== "submitted" && !t.waiting_for ? `<button class="key go" data-do="take" title="Do this task on the desk in a take, then hand it in">${openTakeOf(t) ? `CONTINUE ${esc(openTakeOf(t).toUpperCase())} ON THE DESK` : "WORK IN A TAKE"}</button>` : ""}${human && t.state !== "submitted" ? `<button class="key" data-do="done">MARK DONE</button>` : ""}${t.state === "needs_you" ? `<button class="key" data-do="queued">BACK IN THE QUEUE</button>` : ""}<button class="key warn" data-do="cancelled">CANCEL TASK</button>`
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
    if (state === "take") { await cutTake({ seat: s.seat, task: id }); return; }
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
      ${t.summary ? `<p>${esc(t.summary)}</p>` : ""}${t.returns && t.reason ? `<p class="back"><b>Last sent back for:</b> ${esc(t.reason)}</p>` : ""}
      ${d ? `<ul class="changes">${d.changes.map((c) => `<li class="${out.has(c.where) || !s.owns.includes(c.kind) ? "out" : ""}"><b>${esc(c.where)}</b> ${esc(c.what)}</li>`).join("") || `<li>No changes yet.</li>`}
        ${conf.map((c) => `<li class="conf">conflict · ${esc(c.where)}: ${esc(c.why || "")}</li>`).join("")}</ul>
        ${d.session_moved && !conf.length ? `<div class="meta">The session has moved on since this take was cut (${d.session_moved.changes} change${d.session_moved.changes === 1 ? "" : "s"}); they merge cleanly.</div>` : ""}`
        : `<div class="meta">reading what changed…</div>`}
      <div class="row" style="margin-top:10px">${abKeys(t.id)}<button class="key" data-do="desk" title="See and edit this take in the arrangement, piano roll and mixer">OPEN ON DESK</button><span class="spacer"></span>
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
    if (act === "desk") { hush(); await openTake(take); return; }
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
// opts.seats: put the same occupant in several seats at once (a model ran out of usage); opts.done(label) runs after.
async function openSeatPicker(seat, opts = {}) {
  const s = seatOf(seat), many = (opts.seats || [seat]).filter((x) => seatOf(x));
  if (!ST.agents) { try { status("looking for agent apps…", "busy"); ST.agents = await api("crew_detect"); } catch (e) { toast(e.message); } status(""); }
  const cur = s.occupant || {}, runners = (ST.agents && ST.agents.runners) || [];
  // how each model has done in this seat so far, across sessions (crew_history)
  let past = []; try { past = (await api("crew_history", { seat })).history; } catch { /* no history yet */ }
  const record = (rid, mid) => {
    const h = past.filter((x) => x.agent === `${rid} · ${mid}` || x.agent.startsWith(`${rid} · ${mid} · `)); if (!h.length) return "";
    const sum = (k) => h.reduce((a, x) => a + x[k], 0), dry = sum("out_of_usage");
    return `<span class="tag ${sum("returned") > sum("accepted") || dry > sum("ok") ? "stale" : "ok"}" title="In this seat so far: ${sum("runs")} runs (${sum("ok")} ended ok, ${dry} out of usage, ${sum("failed")} failed); ${sum("handed_in")} takes handed in, ${sum("accepted")} accepted (${sum("first_time")} first time), ${sum("returned")} sent back">${sum("handed_in") ? `${sum("accepted")}/${sum("handed_in")} takes` : `${sum("ok")}/${sum("runs")} runs`}</span>`;
  };
  const row = (val, label, tags = "", extra = "") => `<label><input type="radio" name="occ" value="${esc(val)}" ${extra}><span class="m">${label}</span>${tags}</label>`;
  const box = modal(`<h2>${esc(many.map((x) => seatOf(x).title).join(", "))} · who sits here</h2>
    <p class="hint">${esc(s.purpose)} Suggested model tier: <b>${esc(s.tier)}</b>.</p>
    <div class="pick">
      <h5>People and apps</h5>
      ${row("none", "Nobody (leave it empty)", "", !s.occupant ? "checked" : "")}
      ${row("human", "You: do this seat's work yourself and hand it in", "", cur.kind === "human" ? "checked" : "")}
      ${row("external", "An outside app (Claude Desktop, Codex…) that joins over MCP with seat_join", "", cur.kind === "external" ? "checked" : "")}
      ${runners.map((r) => `<h5>${esc(r.name)}${!r.available ? `<span class="tag bad">not installed</span>` : r.auth === "missing" ? `<span class="tag bad">not signed in</span>` : `<span class="tag ok">ready</span>`}</h5>
        ${r.auth === "missing" && r.notes && r.notes[0] ? `<div class="meta muted" style="padding:0 6px 4px">${esc(r.notes[0])}</div>` : ""}
        ${(r.available ? r.models : []).map((m) => row(`${r.id}|${m.id}`, esc(m.label || m.id), `${record(r.id, m.id)}<span class="tag">${esc(m.tier || "")}</span><span class="tag ${m.cost === "free" ? "ok" : ""}">${esc(m.cost || "")}</span>`,
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
  $("#spScan").onclick = async () => { status("scanning for agent apps…", "busy"); try { ST.agents = await api("crew_detect", { refresh: true }); } catch (e) { toast(e.message); } status(""); openSeatPicker(seat, opts); };
  $("#spOk").onclick = async () => {
    const v = (box.querySelector('input[name="occ"]:checked') || {}).value; if (!v) { toast("Choose who sits here"); return; }
    const [rid, mid] = v.split("|"), who = mid ? { kind: "agent", runner: rid, model: mid, effort: $("#spEffort").value || null } : { kind: v }, mode = $("#spMode").value;
    closeModal();
    // several seats: each keeps its own way of working (takes or direct); only who sits there changes
    for (const x of many) await call("seat_assign", { seat: x, ...who, ...(many.length === 1 ? { mode } : {}) });
    if (opts.done) opts.done(mid || (v === "human" ? "you" : v));
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
  const meta = take && ST.board.takes.find((t) => t.id === take), sig = take ? (meta ? takeSig(meta) : "gone") : String(S.sessionVersion);
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
// Each ticket offers the answers that fit it. The floor manager's own tickets say what an option does ("do":
// choose who sits there, rewrite the task, start again later, cancel, stop the crew); a seat's question gets keys
// to hear what it is asking about and short replies to send as they are or to finish in your own words.
const LISTEN = /\b(listen|audition|hear|playback|by ear)/i;
const REPLIES = {
  listen: [["SOUNDS RIGHT", "I listened: it sounds right. Carry on."], ["NEEDS WORK…", "I listened. What has to change: ", true], ["YOU DECIDE", "Judge it yourself from listen() and score(). Don't wait on me for this one."]],
  ask: [["GO AHEAD", "Go ahead as you propose."], ["YOU DECIDE", "Use your own judgement. Don't wait on me for this one."], ["SKIP IT", "Leave this and carry on with the rest of the task."]],
};
function ticketTakes(a) {
  const live = new Set(ST.board.takes.map((t) => t.id)), found = [];
  for (const id of [a.take, ...a.options.map((o) => o.take), ...(`${a.title} ${a.detail || ""}`.toLowerCase().match(/\btk\d+\b/g) || [])]) if (id && live.has(id) && !found.includes(id)) found.push(id);
  return found;
}
function rewriteTask(id, then) {
  const t = taskOf(id); if (!t) { toast(`${id} is no longer on the board.`); return; }
  modal(`<h2>Rewrite ${esc(t.id)} for the ${esc(title(t.seat))}</h2>
    <p class="hint">Say it so the seat can finish it this time: one outcome, which tracks and bars, what done sounds like. It goes back in the queue with a fresh set of attempts.</p>
    <div class="formgrid"><span>Task</span><input id="rwTitle" value="${esc(t.title)}"><span>Detail</span><textarea id="rwDetail" rows="7">${esc(t.detail || "")}</textarea></div>
    <div class="row"><button class="ghost" id="rwCancel">Cancel</button><button class="key go" id="rwOk">SAVE AND TRY AGAIN</button></div>`);
  $("#rwCancel").onclick = closeModal;
  $("#rwOk").onclick = async () => {
    const args = { task: t.id, title: $("#rwTitle").value.trim(), detail: $("#rwDetail").value };
    if (!args.title) { toast("Give the task a title"); return; }
    closeModal();
    try { await api("task_update", args); then(); } catch (e) { toast(e.message); }
  };
}
function renderNeeds() {
  const box = $("#needsList"), items = ST.board ? ST.board.needs_you : [], seen = new Set(items.map((a) => a.id));
  for (const id of Object.keys(ST.tickets)) if (!seen.has(id)) { ST.tickets[id].remove(); delete ST.tickets[id]; }
  let empty = box.querySelector(".empty");
  if (!items.length) { if (!empty) box.appendChild(h(`<div class="empty">Nothing is waiting on you.</div>`)); return; }
  if (empty) empty.remove();
  for (const a of items) {
    const takes = ticketTakes(a), sig = JSON.stringify([a.title, a.detail, a.seats, a.options, takes]);
    const old = ST.tickets[a.id];
    if (old && (old.dataset.sig === sig || focusedIn(old))) continue;  // a ticket can grow (more seats on a dry model)
    const audition = !!a.take, fromSeat = a.from !== "you", kind = LISTEN.test(`${a.title} ${a.detail || ""}`) ? "listen" : "ask";
    const hear = audition ? "" : takes.length ? takes.slice(0, 2).map(abKeys).join("") + takes.slice(0, 1).map((t) => `<button class="key" data-desk="${esc(t)}" title="See this take on the desk">DESK</button>`).join("")
      : fromSeat ? `<span class="ab"><button class="key" data-ab="session" title="Render and play the session as it is now">▶ SESSION</button></span>` : "";
    const el = h(`<div class="ticket ${esc(a.kind)}">
      <div class="from"><span>${esc(fromSeat ? title(a.from) : "Floor")} · ${esc(a.kind)}${a.task ? " · " + esc(a.task) : ""}</span><span>${esc(a.id)}</span></div>
      <h3>${esc(a.title)}</h3>${a.detail ? `<p>${esc(a.detail)}</p>` : ""}
      ${hear ? `<div class="opts hear">${hear}</div>` : ""}
      <div class="opts">${audition ? `${abKeys(a.take)}<button class="key" data-desk="${esc(a.take)}" title="See and edit this take in the arrangement, piano roll and mixer">DESK</button><button class="key go" data-opt="a">ACCEPT</button><button class="key" data-ret>RETURN…</button>`
        : a.options.map((o, i) => `${o.take && !takes.includes(o.take) ? "" : ""}${o.audio ? `<button class="key" data-audio="${esc(o.audio)}" title="Hear it">▶</button>` : ""}<button class="key ${i === 0 && !fromSeat ? "go" : ""}" data-opt="${esc(o.id)}" data-do="${esc(o.do || "")}" title="${esc(o.detail || "")}">${esc(o.label)}</button>`).join("")
          + (!a.options.length && !fromSeat ? `<button class="key go" data-say="Sorted. Try again.">TRY AGAIN</button>` : "")
          + (!a.options.length && fromSeat ? REPLIES[kind].map(([label, say, more]) => `<button class="key" data-reply="${esc(say)}" ${more ? "data-more" : ""} title="${esc(say)}">${esc(label)}</button>`).join("") : "")}</div>
      <textarea rows="2" class="${audition ? "hidden" : ""}" placeholder="${audition ? "What has to change: bars, tracks, what you heard" : a.options.length ? "Add a note to go with your choice (optional)" : "Or answer in your own words"}"></textarea>
      <div class="opts ${audition ? "hidden" : ""}" data-send><button class="key" data-go>${audition ? "SEND IT BACK" : "SEND ANSWER"}</button></div>
      <div class="err hidden"></div></div>`);
    el.dataset.sig = sig;
    const text = el.querySelector("textarea"), err = el.querySelector(".err");
    const resolve = async (option, say) => {
      err.classList.add("hidden");
      try { await api("attention_resolve", { item: a.id, option: option || null, text: say ?? text.value.trim() }); refresh(true); }
      catch (e) { err.textContent = e.message; err.classList.remove("hidden"); refresh(true); }
    };
    el.querySelectorAll("[data-opt]").forEach((b) => b.onclick = () => {
      const act = b.dataset.do, seats = a.seats && a.seats.length ? a.seats : [(taskOf(a.task) || {}).seat].filter(Boolean);
      if (act === "seat" && seats.length) openSeatPicker(seats[0], { seats, done: (who) => resolve(b.dataset.opt, text.value.trim() || `Now in the seat: ${who}.`) });
      else if (act === "edit" && a.task) rewriteTask(a.task, () => resolve(b.dataset.opt, text.value.trim() || "The task was rewritten."));
      else if (act === "cancel" && !confirm(`Cancel ${a.task}?`)) return;
      else resolve(b.dataset.opt);
    });
    el.querySelectorAll("[data-say]").forEach((b) => b.onclick = () => resolve(null, text.value.trim() || b.dataset.say));
    el.querySelectorAll("[data-reply]").forEach((b) => b.onclick = () => {
      if (b.hasAttribute("data-more")) { text.value = b.dataset.reply; text.focus(); text.setSelectionRange(text.value.length, text.value.length); return; }
      resolve(null, [b.dataset.reply, text.value.trim()].filter(Boolean).join(" "));
    });
    el.querySelectorAll("[data-audio]").forEach((b) => b.onclick = () => { hush(); playFile(b.dataset.audio); });
    el.querySelectorAll("[data-desk]").forEach((b) => b.onclick = () => { hush(); openTake(b.dataset.desk); });
    const ret = el.querySelector("[data-ret]");
    if (ret) ret.onclick = () => { text.classList.remove("hidden"); el.querySelector("[data-send]").classList.remove("hidden"); text.focus(); };
    el.querySelector("[data-go]").onclick = () => {
      if (!text.value.trim()) { err.textContent = audition ? "Say what has to change so the seat can act on it." : "Write an answer, or choose one above."; err.classList.remove("hidden"); return; }
      resolve(audition ? "b" : null);
    };
    wireAb(el);
    if (old) old.replaceWith(el); else box.appendChild(el);
    ST.tickets[a.id] = el;
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

// ---------------------------------------------------------------- reports and the crew scorecard
// Anything longer than talkback is filed as a report (report_write) and read here, in full.
function md(src) {
  const inl = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const out = []; let list = false, table = false;
  const close = () => { if (list) { out.push("</ul>"); list = false; } if (table) { out.push("</table>"); table = false; } };
  for (const raw of String(src).split(/\r?\n/)) {
    const l = raw.trimEnd(), hd = l.match(/^(#{1,4})\s+(.*)$/), li = l.match(/^\s*[-*]\s+(.*)$/);
    if (hd) { close(); out.push(`<h${hd[1].length + 2}>${inl(hd[2])}</h${hd[1].length + 2}>`); }
    else if (li) { if (table) close(); if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inl(li[1])}</li>`); }
    else if (/^\s*\|.*\|\s*$/.test(l)) {
      if (list) close();
      if (/^\s*\|[\s:|-]+\|\s*$/.test(l)) continue;
      const cells = l.trim().slice(1, -1).split("|").map((c) => inl(c.trim()));
      if (!table) { out.push(`<table class="sc"><tr>${cells.map((c) => `<th>${c}</th>`).join("")}</tr>`); table = true; }
      else out.push(`<tr>${cells.map((c) => `<td>${c}</td>`).join("")}</tr>`);
    } else if (!l.trim()) close();
    else { close(); out.push(`<p>${inl(l)}</p>`); }
  }
  close(); return out.join("");
}
async function openReport(id) {
  try {
    const r = await api("report_read", { report: id });
    modal(`<h2>${esc(r.title)}</h2><p class="hint">${esc(r.kind)} · filed by ${esc(title(r.by))} · ${new Date(r.updated * 1000).toLocaleString()}</p>
      <div class="reader doc">${md(r.body)}</div><div class="row"><button class="ghost" id="rpBack">All reports</button><button class="ghost" id="rpClose">Close</button></div>`);
    $("#rpClose").onclick = closeModal; $("#rpBack").onclick = openReports;
  } catch (e) { toast(e.message); }
}
async function openReports() {
  if (!ST.board) return;
  const reps = (ST.board.reports || []).slice().reverse();
  const box = modal(`<h2>Reports</h2>
    <p class="hint">What the crew has filed: reviews, reports and notes too long for talkback. The scorecard counts each agent's runs and takes from the activity log; ASK FOR A REVIEW files it and has the Producer write up how each seat did.</p>
    <div class="stack">${reps.map((r) => `<div class="item hot" data-report="${esc(r.id)}"><h4><span class="id">${esc(r.id)}</span>${esc(r.title)}<span class="tag ${r.kind === "review" ? "cy" : ""}">${esc(r.kind)}</span><span class="tag">${esc(title(r.by))}</span><span class="meta">${clock(r.updated)} · ${r.chars} characters</span></h4></div>`).join("") || `<div class="empty">Nothing filed yet.</div>`}</div>
    <h2 style="margin-top:16px">Crew scorecard · this session</h2><div id="scBox" class="meta">counting…</div>
    <div class="row"><button class="key go" id="rvAsk" title="File the scorecard and give the Producer the task of writing the review">ASK FOR A REVIEW</button><button class="ghost" id="rpClose">Close</button></div>`);
  $("#rpClose").onclick = closeModal;
  box.querySelectorAll("[data-report]").forEach((el) => el.onclick = () => openReport(el.dataset.report));
  $("#rvAsk").onclick = async () => {
    try {
      const r = await api("crew_review"); closeModal(); refresh(true);
      toast(r.task ? `Scorecard filed as ${r.scorecard}. The Producer has ${r.task}: the review arrives here${ST.crew && ST.crew.running ? "" : " once the crew runs (RUN)"}.` : `Scorecard filed as ${r.scorecard}. ${r.note}`, true);
    } catch (e) { toast(e.message); }
  };
  try {
    const c = await api("crew_scorecard");
    if (!$("#scBox")) return;
    $("#scBox").innerHTML = c.rows.length ? `<table class="sc"><tr><th>Seat</th><th>Agent</th><th title="Agent runs, and how many ended normally">Runs (ok)</th><th title="Runs that ended because the model had no usage left">Dry</th><th>Failed</th><th>Tools</th><th>Min</th><th title="Takes handed in / accepted / accepted without being sent back / sent back">Takes in · ok · 1st · back</th><th>Tasks</th><th title="Times this seat put something on Needs You">Asked</th></tr>
      ${c.rows.map((r) => `<tr><td>${esc(title(r.seat))}</td><td class="mono">${esc(r.agent)}</td><td>${r.runs} (${r.ok})</td><td class="${r.out_of_usage ? "bad" : ""}">${r.out_of_usage}</td><td class="${r.failed ? "bad" : ""}">${r.failed}</td><td>${r.tool_calls}</td><td>${r.minutes}</td><td>${r.handed_in} · ${r.accepted} · ${r.first_time} · ${r.returned}</td><td>${r.tasks_done}</td><td>${r.asked_you}</td></tr>`).join("")}</table>` : "No agent has worked in this session yet.";
  } catch (e) { if ($("#scBox")) $("#scBox").textContent = e.message; }
}
$("#repBtn").onclick = openReports;
document.addEventListener("click", (ev) => { const el = ev.target.closest && ev.target.closest("#tbLog [data-report], .benchBody [data-report]"); if (el) { ev.stopPropagation(); openReport(el.dataset.report); } }, true);

// ---------------------------------------------------------------- wiring
function render() {
  renderHeader();
  renderTakeBar();  // the desk's list of takes comes from the board
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
  render, hush, listen, room: setRoom, board: () => ST.board,
};
setRoom(store.get("room", "studio") === "desk" ? "desk" : "studio");
if (S.path || S.doc) loadState();  // app.js loaded the state before this file defined Studio
})();
