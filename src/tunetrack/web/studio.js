// TuneTrack Studio room: the crew rack (one unit per seat), Needs You, and the monitor/talkback section.
// It draws from the board (/api/state, studio_view) and follows /api/events, so every seat's work shows up live.
// Uses the helpers app.js defines: $, S, api, status, toast, esc, modal, closeModal, loadState, renderAll, stop,
// and for takes on the desk: openTake, cutTake, renderTakeBar.
"use strict";
(() => {
const SHORT = { producer: "PROD", spotting: "SPOT", composer: "COMP", arranger: "ARR", rhythm: "RHY", sound: "SND", tracking: "TRK", mix: "MIX", review: "REV" };
const TABS = ["bench", "tasks", "takes", "log", "setup"];
// The first tab is the seat's own bench: its work in the form that seat reads it in (bench.py decides which).
const VIEW = { producer: "plan", spotting: "picture", composer: "notes", arranger: "notes", rhythm: "notes", tracking: "notes", sound: "sound", mix: "mix", review: "report" };
const BENCH_NAME = { plan: "plan", picture: "cues", notes: "roll", sound: "sounds", mix: "console", report: "report" };
const DESK_TAB = { plan: "brief", picture: "brief", notes: "roll", sound: "inspector", mix: "mixer", report: "report" };
const OPEN_STATES = ["queued", "working", "submitted", "needs_you"];
const ST = {
  board: null, crew: null, project: null, open: null, tab: {}, units: {}, act: {}, feed: [], lastSeq: 0, es: null,
  target: "producer", wake: true, talkOnly: false, diffs: {}, agents: null, benchSig: {}, bench: {},
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
  const a = seatAct(seat); a.lines.push({ at, seat, cls: cls + (isLong(html) ? " long" : ""), html }); if (a.lines.length > 300) a.lines.splice(0, 100);
  if (plain) a.last = plain;
}
function feed(at, cls, html, seat = "") { ST.feed.push({ at, seat, cls: cls + (isLong(html) ? " long" : ""), html }); if (ST.feed.length > 500) ST.feed.splice(0, 150); }
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
      if (e.what === "start") { a.started = e.at; a.hits = []; line(e.seat, e.at, "note", `run starts on ${esc(e.task)} · ${esc(e.agent || "")}${e.take ? " · take " + esc(e.take) : ""}`, "starting…"); feed(e.at, "", `${who} starts ${esc(e.task)}${e.take ? " in " + esc(e.take) : ""}`, e.seat || e.sender || e.from || ""); }
      else if (e.what === "tool") { a.hits.push(e.at); line(e.seat, e.at, "tool", `› <b>${esc(e.tool)}</b>`, "› " + e.tool); }
      else if (e.what === "says") line(e.seat, e.at, "say", esc(e.text), e.text.replace(/\s+/g, " "));
      else if (e.what === "end") { a.started = null; line(e.seat, e.at, e.ok ? "good" : "bad", e.ok ? `run ended · ${e.tools} tool calls` : `run ended early: ${esc(e.error || "no reason given")}`, e.ok ? "run ended" : "ended early: " + (e.error || "")); feed(e.at, e.ok ? "" : "bad", `${who} ${e.ok ? "finished its run" : "stopped early: " + esc(e.error || "no reason given")}`, e.seat || e.sender || e.from || ""); }
    } else if (e.kind === "task") {
      const t = `${esc(e.id)} “${esc(e.title)}” → ${esc(String(e.state).replace("_", " "))}${e.reason ? " (" + esc(e.reason) + ")" : ""}`;
      line(e.seat, e.at, e.state === "done" ? "good" : e.state === "needs_you" ? "note" : "", t);
      feed(e.at, e.state === "needs_you" ? "note" : "", `${who} ${t}`, e.seat || e.sender || e.from || "");
    } else if (e.kind === "take") {
      const t = `take ${esc(e.id)} ${esc(e.state)}${e.summary ? ": " + esc(e.summary) : ""}${e.reason ? ": " + esc(e.reason) : ""}`;
      if (e.seat) line(e.seat, e.at, e.state === "accepted" ? "good" : e.state === "returned" ? "note" : "", t);
      feed(e.at, e.state === "accepted" ? "good" : "", `${who} ${t}`, e.seat || e.sender || e.from || "");
      delete ST.diffs[e.id]; delete AB.cache[e.id];
    } else if (e.kind === "message") {
      const text = fullText(e);
      feed(e.at, "say" + (e.sender === "you" ? " you" : ""), `<i>${esc(title(e.sender))} → ${esc(title(e.to))}</i>  ${esc(text)}`, SHORT[e.sender] ? e.sender : e.to);
      for (const s of [e.sender, e.to]) if (SHORT[s]) line(s, e.at, "say", `<i>${esc(title(e.sender))} → ${esc(title(e.to))}:</i> ${esc(text)}`);
      if (window.Voice) window.Voice.heard(e, text, title(e.sender));  // voice.js reads new ones aloud (old history too comes through here)
    } else if (e.kind === "attention") {
      feed(e.at, e.state === "open" ? "note" : "", e.state === "open" ? `Needs you: ${esc(e.title)}` : `Answered: ${esc(e.title)}${e.answer ? " → " + esc(e.answer) : ""}`, e.seat || e.sender || e.from || "");
    } else if (e.kind === "report") {
      const t = `filed a ${esc(e.what || "report")}: <u data-report="${esc(e.id)}" title="Read it">${esc(e.title)}</u>`;
      feed(e.at, "good", `<b>${esc(title(e.sender))}</b> ${t}`, e.seat || e.sender || e.from || "");
      if (SHORT[e.sender]) line(e.sender, e.at, "good", t);
    } else if (e.kind === "crew") {
      feed(e.at, e.error ? "bad" : "note", e.what === "start" ? "Crew running" : `Crew stopped${e.error ? ": " + esc(e.error) : ""}`, e.seat || e.sender || e.from || "");
    } else if (e.kind === "seat") {
      feed(e.at, "", `${who} seat: ${esc(e.occupant)}${e.paused ? " · on hold" : ""}`, e.seat || e.sender || e.from || "");
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
    if (ev.length && msg.project === ST.project) { window.TTLinks?.events(ev, ST.board); ingest(ev); refresh(); }
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
  $("#needsHead").textContent = String(n).padStart(2, "0"); $("#needsHead").classList.toggle("hot", n > 0);
  const reps = ST.board ? (ST.board.reports || []).length : 0;
  $("#repBtn").textContent = reps ? `REPORTS ${reps}` : "REPORTS"; $("#repBtn").classList.toggle("on", reps > 0); $("#repBtn").disabled = !ST.board;
  $("#needs h2 .lamp").className = "lamp" + (n ? " red blink" : "");
  if (window.TTNeeds) { TTNeeds.renderInvolve(); TTNeeds.update(); }  // the INVOLVE key, and the sheet if it is open
}
// subscription limits each agent app last reported (5-hour and weekly windows); polled, since runs update them
const USAGE_NAMES = { claude: ["CLAUDE", "CL"], codex: ["CODEX", "CX"] };
async function renderUsage() {
  let u; try { u = await api("crew_usage"); } catch { return; }
  const el = $("#usageRead"), now = u.now || Date.now() / 1000, parts = [], tips = [];
  const when = (t) => new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
  const ago = (t) => { const m = Math.round((now - t) / 60); return m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`; };
  for (const [k, [name, short]] of Object.entries(USAGE_NAMES)) {
    const r = u[k]; if (!r) continue;
    const win = (w, label, cls) => {
      if (!w) return "";
      const reset = w.resets_at && w.resets_at < now, pct = Math.round(w.used_pct);
      tips.push(`${name} ${label}: ${reset ? `was ${pct}%, window has reset since` : `${pct}% used, resets ${w.resets_at ? when(w.resets_at) : "?"}`}`);
      return `<span class="${cls}"><i class="lbl">${label} </i><b class="${reset ? "dimv" : pct >= 90 ? "amb" : ""}">${reset ? "–" : pct + "%"}</b></span>`;
    };
    parts.push(`<span class="uName" data-short="${short}"><i class="lbl">${name}</i></span>${win(r["5h"], "5H", "")}${win(r.week, "WK", "wk")}`);
    tips.push(`  (last reported ${ago(r.seen_at)})`);
  }
  el.hidden = !parts.length; el.innerHTML = parts.join('<span class="dimv">·</span>');
  el.title = tips.join("\n") + "\nFigures come from each app's own output; they update when that app runs. Antigravity and Kilo don't report limits.";
}
renderUsage(); setInterval(renderUsage, 60000);
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
  const was = (S.doc && S.doc.brief) || {};
  const curForm = FORMS.some(([v]) => v === was.form) ? was.form : "", curLen = was.length != null ? String(was.length) : "";
  let formV = curForm;
  const box = modal(`<h2>Brief the Producer</h2>
    <p class="hint">Say what you want made: what it is for, mood, length, anything it must fit. The Producer plans it, hands out tasks to the seats that are filled, and auditions what comes back. Each agent run costs whatever that seat's model costs.</p>
    ${agent ? "" : `<p class="hint warn">${prod && prod.occupant ? "The Producer seat is not an agent, so this brief waits there as a task for whoever sits in it." : "Nobody is in the Producer seat. The brief will wait there until you seat an agent (SEAT on the Producer unit)."}</p>`}
    <textarea id="brText" rows="6" placeholder="A calm menu loop, a theme tune, a full song, or a 4-minute symphonic piece: say what it is for, mood, length…"></textarea>
    <div class="formgrid" style="margin-top:10px"><span>Form</span>${formSeg("brForm", curForm)}<span>Length</span><input id="brLen" value="${esc(curLen)}" placeholder="e.g. 90 s, 2:30, about 4 minutes · blank: the Producer decides"></div>
    <div class="formgrid" style="margin-top:10px"><span>Style</span><select id="brStyle" title="A genre style sheet: each seat gets that genre's craft in its brief. Left on auto, the project's genre picks one, or the Producer does."><option value="">auto</option></select><span>At once</span><input id="brPar" type="number" min="1" max="6" value="3" title="Agents working at the same time">
      <span>Run limit</span><input id="brMax" type="number" min="1" max="500" value="${(ST.crew && ST.crew.max_runs) || 100}" title="Agent runs before the crew stops and asks you"></div>
    <div class="row"><button class="ghost" id="brPick" title="Propose a model per seat from how each did in earlier sessions; nothing changes until you apply it">Pick a crew from history…</button><span class="spacer"></span><button class="ghost" id="brCancel">Cancel</button><button class="key go" id="brGo">START THE SESSION</button></div>`);
  $("#brText").focus();
  document.querySelectorAll("#brForm button").forEach((b) => b.onclick = () => { formV = b.dataset.v; document.querySelectorAll("#brForm button").forEach((x) => x.classList.toggle("on", x === b)); });
  const cur = (S.doc && S.doc.brief && S.doc.brief.style) || "";
  api("style_list", {}, null).then((list) => {
    $("#brStyle").innerHTML = `<option value="">auto</option>` + list.map((s) => `<option value="${esc(s.name)}" title="${esc(s.about)}"${s.name === cur ? " selected" : ""}>${esc(s.title)}</option>`).join("");
  }).catch(() => { /* auto only */ });
  $("#brCancel").onclick = closeModal;
  $("#brPick").onclick = openCrewPick;
  $("#brGo").onclick = async () => {
    const brief = $("#brText").value.trim(); if (!brief) { toast("Say what you want made"); return; }
    const style = $("#brStyle").value, len = $("#brLen").value.trim();
    closeModal();
    // only what changed goes to the brief; "" clears a form or length set before
    const set = {};
    if (style !== cur) set.style = style || "none";
    if (formV !== curForm) set.form = formV;
    if (len !== curLen) set.length = len;
    if (Object.keys(set).length && S.doc) await api("project_set", { brief: set }, null).catch((e) => toast(e.message));
    await call("crew_start", { brief, parallel: +$("#brPar").value || 3, max_runs: +$("#brMax").value || 100 }, "starting the crew…");
  };
}
$("#needsBtn").onclick = () => { if (window.TTNeeds) TTNeeds.open(); };
function setRoom(room) {
  document.body.dataset.room = room; store.set("room", room);
  document.querySelectorAll("#rooms button").forEach((b) => b.classList.toggle("active", b.dataset.room === room));
  if (room === "desk") renderAll(); else if (room === "studio") render();
  if (room === "mix") TTMix.show();
  if (room === "cables") window.TTCables?.show(); else window.TTCables?.hide();
}
const ROOMS = ["studio", "desk", "mix", "cables"];
document.querySelectorAll("#rooms button").forEach((b) => b.onclick = () => setRoom(b.dataset.room));

// ---------------------------------------------------------------- the rack
const SIGN_TIP = {
  producer: "Co-Producer: the Producer's decisions on takes, and the plans it hands out, wait for your OK",
  review: "Sign off: the fix tasks Review files wait for your OK",
  take: "Sign off: this seat's hand-ins stop at you before the Producer hears them",
};
function seatState(s) {
  const run = ST.crew && ST.crew.active && ST.crew.active[s.seat], t = taskOf(s.task), kind = s.occupant && s.occupant.kind;
  if (run) return run.quiet ? ["working", "amber blink", `QUIET ${Math.round((Date.now() / 1000 - run.quiet) / 60)} MIN`] : ["working", "amber pulse", "WORKING"];
  if (ST.board.needs_you.some((a) => a.from === s.seat && a.kind !== "blocker")) return ["needs", "amber blink", "WAITS ON YOU"];
  if (t && t.state === "needs_you") return ["needs", "red blink", "NEEDS YOU"];
  if (!s.occupant) return t ? ["needs", "red", "NO ONE SEATED"] : ["vacant", "", "VACANT"];
  if (s.paused) return ["hold", "dimamber", "ON HOLD"];
  if (t && t.state === "submitted") return ["handed", "cyan", "TAKE HANDED IN"];
  if (t && t.state === "working") return ["working", kind === "agent" ? "dimamber" : "amber", kind === "human" ? "YOU · WORKING" : "WORKING"];
  if (t && t.state === "queued") {
    if (t.held) return ["queued", "dimamber", "WAITS ON YOUR OK"];
    if (t.waiting_for) return ["queued", "dimamber", "WAITS ON " + t.waiting_for.join(" ")];  // not your move yet either
    if (kind === "human") return ["yours", "amber blink", "YOUR MOVE"];
    if (t.not_before) return ["queued", "dimamber", "TRIES AGAIN " + new Date(t.not_before * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })];
    if (kind === "agent" && !(ST.crew && (ST.crew.running || ST.crew.elsewhere))) return ["queued", "dimamber", "QUEUED · CREW STOPPED"];
    return ["queued", "dimamber", "QUEUED"];
  }
  return s.presence === "online" ? ["ready", "green", "ONLINE"] : ["ready", "green", "READY"];
}
// The round status dial: one or two short words that always fit; labels it doesn't say in full go on the screen.
const DIAL = { working: "WORKING", needs: "NEEDS YOU", yours: "YOUR MOVE", hold: "ON HOLD", handed: "HANDED IN", queued: "WAITING", vacant: "VACANT" };
const PLAIN = new Set(["WORKING", "YOU · WORKING", "NEEDS YOU", "WAITS ON YOU", "READY", "ONLINE", "VACANT", "ON HOLD", "TAKE HANDED IN", "YOUR MOVE", "QUEUED"]);
function dialWord(state, label) {
  if (state === "working" && label.startsWith("QUIET")) return "QUIET";
  if (state === "ready") return label === "ONLINE" ? "ONLINE" : "READY";
  return DIAL[state] || label.split(" ")[0];
}
// shrink the dial's words until the longest line fits inside the ring (display fonts differ in width)
function fitDial(el) {
  el.style.fontSize = "";
  if (!el.clientWidth) { delete el.dataset.w; return; }  // not on screen (another room): fitted on the next render
  for (let px = parseFloat(getComputedStyle(el).fontSize); el.scrollWidth > el.clientWidth + 0.5 && px > 6; px -= 0.5) el.style.fontSize = px - 0.5 + "px";
}

document.fonts?.ready.then(() => Object.values(ST.units || {}).forEach((u) => fitDial(u.querySelector(".state"))));

// The EFFORT knob: how hard the seat's agent thinks on its next run (runner --effort / reasoning effort / --variant).
// Drag up/down, scroll, or arrow keys; double-click returns it to the app's default. It never touches a run in flight.
const knobs = {};  // seat -> { val, timer }: a turn waiting to be saved
async function agentApps() {
  if (!ST.agents) { try { ST.agents = await api("crew_detect"); } catch { return []; } }
  return ST.agents.runners || [];
}
function effortSteps(occ) {
  const r = ST.agents && (ST.agents.runners || []).find((x) => x.id === occ.runner), m = r && (r.models || []).find((x) => x.id === occ.model);
  const e = (m && m.efforts && m.efforts.length ? m.efforts : r && r.efforts) || [];
  return ["", ...e];
}
// where a level usually sits, for drawing the knob before the apps' own lists have loaded
const EFFORT_GUESS = ["", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"];
function drawKnob(k, s) {
  const occ = s.occupant || {}, agent = occ.kind === "agent", pend = knobs[s.seat];
  const val = pend ? pend.val : agent ? occ.effort || "" : null;
  let steps = agent && ST.agents ? effortSteps(occ) : EFFORT_GUESS;
  if (agent && !steps.includes(val)) steps = EFFORT_GUESS.includes(val) ? EFFORT_GUESS : ["", val];
  const i = Math.max(0, steps.indexOf(val)), n = steps.length;
  // 0 = the app's default, at seven o'clock; the levels spread clockwise to five o'clock
  k.style.setProperty("--a", (agent ? (n > 1 ? -135 + 270 * i / (n - 1) : -135) : -135) + "deg");
  k.classList.toggle("off", !agent); k.classList.toggle("set", agent && !!val);
  k.querySelector(".kv").textContent = !agent ? "—" : val ? val.toUpperCase() : "AUTO";
  k.setAttribute("aria-valuetext", agent ? val || "default" : "no agent");
  k.setAttribute("aria-disabled", String(!agent));
  k.title = !agent ? "Effort: seat an AI agent here to set how hard it thinks"
    : `Effort: ${val || "the app's default"}. Drag up or down, scroll, or use the arrow keys; double-click for the default. Applies from the seat's next run.`;
}
function effortKnob(k, seat) {
  const nudge = async (d, to) => {
    const s = seatOf(seat), occ = s && s.occupant; if (!occ || occ.kind !== "agent") return;
    await agentApps();
    const steps = effortSteps(occ), cur = knobs[seat] ? knobs[seat].val : occ.effort || "";
    if (steps.length < 2) { toast(`${occ.runner} has no effort levels to choose from.`); return; }
    const i = to !== undefined ? 0 : clampN(Math.max(0, steps.indexOf(cur)) + d, 0, steps.length - 1);
    if (steps[i] === cur) return;
    const p = knobs[seat] = knobs[seat] || {}; p.val = steps[i]; drawKnob(k, s);
    clearTimeout(p.timer);
    p.timer = setTimeout(async () => {
      const v = p.val;
      try { await call("seat_assign", { seat, effort: v }); toast(`${s.title} effort: ${v || "default"}. It applies from the seat's next run.`, true); }
      catch { /* call() said why */ }
      if (knobs[seat] === p && p.val === v) delete knobs[seat];
      const now = seatOf(seat); if (now) drawKnob(k, now);
    }, 500);
  };
  k.onpointerenter = k.onfocus = () => { if (!ST.agents) agentApps().then(() => { const s = seatOf(seat); if (s) drawKnob(k, s); }); };
  k.onwheel = (e) => { const s = seatOf(seat); if (!s || !s.occupant || s.occupant.kind !== "agent") return; e.preventDefault(); nudge(e.deltaY < 0 ? 1 : -1); };
  k.ondblclick = () => nudge(0, "");
  k.onkeydown = (e) => {
    const d = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1 }[e.key];
    if (d) { e.preventDefault(); nudge(d); } else if (e.key === "Home" || e.key === "Delete") { e.preventDefault(); nudge(0, ""); }
  };
  k.onpointerdown = (e) => {
    if (e.button !== 0) return;
    e.preventDefault(); k.setPointerCapture(e.pointerId); k.classList.add("grab");
    let y0 = e.clientY;
    k.onpointermove = (m) => { const dy = y0 - m.clientY; if (Math.abs(dy) >= 16) { y0 = m.clientY; nudge(dy > 0 ? 1 : -1); } };
    k.onpointerup = k.onpointercancel = () => { k.onpointermove = null; k.classList.remove("grab"); };
  };
}

function buildUnits() {
  const box = $("#units"); box.innerHTML = ""; ST.units = {};
  for (const s of ordered(ST.board.seats)) {
    const u = h(`<article class="unit" data-seat="${s.seat}">
      <div class="face">
        <div class="ear"><i class="screw" style="--r:${(s.seat.length * 37) % 180}deg"></i><i class="screw" style="--r:${(s.seat.length * 71) % 180}deg"></i></div>
        <div class="ident" title="Double-click to fold or unfold"><div class="name">${esc(s.title)}</div><div class="room">${esc(s.room)}${s.optional ? " · optional" : ""}</div><button class="mini mmFold" data-k="fold"></button></div>
        <div class="lampcol"><i class="lamp"></i><span class="state"></span></div>
        <div class="vfd screen" title="Open this seat"><div class="l1"><span class="occ"></span><span class="why"></span><span class="clk dimv"></span></div><div class="l2"></div><div class="l3"></div></div>
        <div class="meter" title="Activity: tool calls in the last half minute">${"<i></i>".repeat(12)}</div>
        <div class="knob" data-k="effort" tabindex="0" role="slider" aria-label="Effort"><div class="cap"><i></i></div><span class="kv"></span><span class="kl">EFFORT</span></div>
        <div class="keys"><button class="key" data-k="seat" title="Choose who sits here"><i class="led"></i>SEAT</button><button class="key toggle" data-k="hold" title="Hold this seat: stop its agent and start nothing new until released"><i class="led"></i>HOLD</button><button class="key toggle sign" data-k="sign"><i class="led"></i>SIGN OFF</button><button class="key toggle" data-k="open" title="Open this seat's workstation"><i class="led"></i>OPEN</button></div>
        <div class="ear"><i class="screw" style="--r:${(s.seat.length * 53) % 180}deg"></i><i class="screw" style="--r:${(s.seat.length * 19) % 180}deg"></i></div>
      </div>
      <div class="bench shut" inert><div class="benchIn"><div class="tabsB">${TABS.map((t) => `<button data-tab="${t}">${t === "bench" ? BENCH_NAME[VIEW[s.seat]] : t}</button>`).join("")}<span class="purpose"></span></div><div class="benchBody"></div></div></div>
    </article>`);
    const q = (sel) => u.querySelector(sel);
    q('[data-k="seat"]').onclick = () => openSeatPicker(s.seat);
    effortKnob(q(".knob"), s.seat);
    q('[data-k="hold"]').onclick = () => { const cur = seatOf(s.seat); call("seat_hold", { seat: s.seat, paused: !cur.paused }); };
    q('[data-k="sign"]').onclick = async () => { const cur = seatOf(s.seat), r = await call("seat_signoff", { seat: s.seat, on_: !cur.signoff }); if (r && r.note) toast(r.note, true); };
    // the bench slides out of the unit (rack.css): scroll it into view once it has opened
    const toggle = () => { ST.open = ST.open === s.seat ? null : s.seat; store.set("open", ST.open || ""); renderUnits(); if (ST.open) setTimeout(() => u.scrollIntoView({ block: "nearest", behavior: "smooth" }), 440); };
    q('[data-k="open"]').onclick = toggle; q(".screen").onclick = toggle;
    u.querySelectorAll(".tabsB button").forEach((b) => b.onclick = () => { ST.tab[s.seat] = b.dataset.tab; delete ST.benchSig[s.seat]; renderUnits(); });
    q(".benchBody").addEventListener("focusout", () => setTimeout(() => { if (!focusedIn(u)) renderBench(s.seat); }, 0));
    q(".purpose").textContent = s.purpose; q(".purpose").title = s.purpose;
    q('[data-k="fold"]').onclick = () => foldSeat(s.seat); q(".ident").ondblclick = (e) => { if (!e.target.closest("button")) foldSeat(s.seat); };
    u.querySelectorAll(".ear").forEach((el) => { el.title = "Drag to move this unit in the rack"; el.onpointerdown = (e) => dragUnit(e, u); });
    box.appendChild(u); ST.units[s.seat] = u;
  }
}
function renderUnits() {
  if (!ST.board) { $("#units").innerHTML = `<div class="empty">Open or create a project to seat a crew.</div>`; ST.units = {}; window.TTLinks?.draw(null); return; }
  if (!ST.units[ST.board.seats[0].seat] || !ST.units[ST.board.seats[0].seat].isConnected) buildUnits();
  for (const s of ST.board.seats) {
    const u = ST.units[s.seat], q = (sel) => u.querySelector(sel), [state, lamp, label] = seatState(s), t = taskOf(s.task), a = seatAct(s.seat);
    u.dataset.state = state; u.classList.toggle("vacant", !s.occupant); u.classList.toggle("open", ST.open === s.seat);
    // the dial says the state in a word or two; whatever more the label says goes on the screen's top line
    const word = dialWord(state, label), st = q(".state");
    q(".lamp").className = "lamp " + lamp; q(".lampcol").setAttribute("aria-label", label); q(".lampcol").title = label;
    if (st.dataset.w !== word) { st.dataset.w = word; st.innerHTML = word.split(" ").map(esc).join("<br>"); fitDial(st); }
    const why = PLAIN.has(label) ? "" : label;
    q(".why").textContent = why; q(".why").title = why;
    q(".occ").innerHTML = s.occupant ? esc((s.label || s.occupant.kind).toUpperCase()) : `<span class="dimv">— VACANT —</span>`;
    q(".l2").innerHTML = t ? `<span class="amb">${esc(t.id)}</span> ${esc(t.title)}${s.open_tasks > 1 ? ` <span class="dimv">+${s.open_tasks - 1}</span>` : ""}` : `<span class="dimv">no open tasks</span>`;
    // bottom line: what the seat last did (its first sentence), else what the seat is for in a few words;
    // the whole of either is in the bench's log and on the tabs strip
    const busy = s.occupant && a.last, l3 = busy ? a.last.split(/(?<=[.!?])\s/)[0] : s.purpose.split(/:|\. |, then /)[0];
    if (q(".l3").textContent !== l3) q(".l3").textContent = l3;
    q(".l3").title = busy ? a.last : s.purpose; q(".l3").classList.toggle("idle", !busy);
    drawKnob(q(".knob"), s);
    q('[data-k="seat"]').classList.toggle("lit", !!s.occupant);
    q('[data-k="hold"]').classList.toggle("on", s.paused); q('[data-k="hold"]').disabled = !s.occupant || s.occupant.kind !== "agent";
    const sk = q('[data-k="sign"]'), mine = s.occupant && s.occupant.kind === "human";
    sk.classList.toggle("on", !!s.signoff); sk.disabled = mine;
    sk.title = mine ? "You sit here: your own work needs no sign-off" : SIGN_TIP[s.seat] || SIGN_TIP.take;
    q('[data-k="open"]').classList.toggle("on", ST.open === s.seat);
    const bt = q('.tabsB [data-tab="bench"]'); if (bt.textContent !== benchName(s)) bt.textContent = benchName(s);
    const bench = q(".bench"), shut = ST.open !== s.seat;
    if (bench.classList.contains("shut") !== shut) {
      if (shut) holdRack();
      bench.classList.remove("opened"); bench.classList.toggle("shut", shut); bench.inert = shut; clearTimeout(bench.slid);
      // once it has slid fully open (rack.css, .42s; at once with motion off), let its pop-overs out of the clip
      if (!shut) bench.slid = setTimeout(() => { if (!bench.classList.contains("shut")) bench.classList.add("opened"); }, parseFloat(getComputedStyle(bench).transitionDuration) ? 440 : 0);
    }
    const fold = L.fold.includes(s.seat), f = q('[data-k="fold"]');
    u.classList.toggle("folded", fold); f.textContent = fold ? "+" : "–"; f.title = fold ? "Unfold this unit" : "Fold this unit to a 1U strip";
    u.classList.toggle("hidden", L.hideEmpty && s.optional && !s.occupant && !s.open_tasks && !t && ST.open !== s.seat);
    if (ST.open === s.seat) renderBench(s.seat);
  }
  tickUnits();
  window.TTLinks?.draw(ST.board);  // the task cables follow the units (crewlinks.js)
  // the EFFORT knobs need each app's levels: look them up once (crew_detect is cached and quick)
  if (!ST.agents && !ST.agentsAsked && ST.board.seats.some((s) => s.occupant && s.occupant.kind === "agent")) {
    ST.agentsAsked = true;
    agentApps().then(() => ST.board && ST.board.seats.forEach((s) => ST.units[s.seat] && drawKnob(ST.units[s.seat].querySelector(".knob"), s)));
  }
}
// A bench closing shrinks the rack. Scrolled to the bottom, the browser would pull the whole rack down with it while
// the bench folds (the face slides away from under the pointer). So the rack keeps its height until the bench has
// shut, then lets the spare room go.
let rackHeld = null;
function holdRack() {
  const box = $("#units"); if (!box || !parseFloat(getComputedStyle(box.querySelector(".bench") || box).transitionDuration)) return;
  clearTimeout(rackHeld);
  box.style.transition = "none"; box.style.minHeight = box.offsetHeight + "px";
  rackHeld = setTimeout(() => {
    const held = box.style.minHeight; box.style.minHeight = ""; const fit = box.offsetHeight;
    box.style.minHeight = held; void box.offsetHeight;
    box.style.transition = "min-height .32s cubic-bezier(.4, 0, .2, 1)"; box.style.minHeight = fit + "px";
    rackHeld = setTimeout(() => { box.style.transition = box.style.minHeight = ""; rackHeld = null; }, 340);
  }, 440);
}
// a task cable clicked: open the seat doing that task, on its tasks
if (window.TTLinks) window.TTLinks.onOpen = (seat) => {
  if (!ST.units[seat]) return;
  ST.open = seat; ST.tab[seat] = "tasks"; delete ST.benchSig[seat]; store.set("open", seat); renderUnits();
  ST.units[seat].scrollIntoView({ block: "nearest", behavior: "smooth" });
};
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

// ---- movable modules
// The owner's rack layout (unit order, folds, the side column), kept in the library's ui_prefs as "studio_layout",
// so every browser and the desktop app share it.
const LAYOUT0 = { order: [], fold: [], hideEmpty: false, side: "right", sideW: null, bays: ["needs", "monitor"], bayH: null };
let L = { ...LAYOUT0 }, layoutTimer = null;
const clampN = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
function setLayout(v) {
  const o = v && typeof v === "object" ? v : {}, arr = (x) => Array.isArray(x) ? x.filter((s) => typeof s === "string") : [];
  L = { ...LAYOUT0, order: arr(o.order), fold: arr(o.fold), hideEmpty: !!o.hideEmpty, side: o.side === "left" ? "left" : "right",
    sideW: +o.sideW ? clampN(+o.sideW, 280, 640) : null, bays: o.bays && o.bays[0] === "monitor" ? ["monitor", "needs"] : ["needs", "monitor"], bayH: +o.bayH > 0 ? +o.bayH : null };
}
function saveLayout() {
  clearTimeout(layoutTimer);
  layoutTimer = setTimeout(() => api("ui_prefs", { set: { studio_layout: L } }, null).catch(() => { /* kept for this page only */ }), 400);
}
function relayout() { applySide(); if (ST.board && Object.keys(ST.units).length) { const box = $("#units"); ordered(ST.board.seats).forEach((s) => ST.units[s.seat] && box.appendChild(ST.units[s.seat])); renderUnits(); } }
function ordered(seats) {
  const at = (s, i) => { const k = L.order.indexOf(s.seat); return k < 0 ? 1e4 + i : k; };
  return seats.map((s, i) => [at(s, i), s]).sort((a, b) => a[0] - b[0]).map((x) => x[1]);
}
function foldSeat(id) { L.fold = L.fold.includes(id) ? L.fold.filter((x) => x !== id) : [...L.fold, id]; saveLayout(); renderUnits(); }
// A pointer drag that only starts past a few pixels (so clicks stay clicks); Escape cancels it.
function grab(ev, el, on) {
  if (ev.button !== 0) return;
  ev.preventDefault();
  const x0 = ev.clientX, y0 = ev.clientY; let live = false;
  const end = (ok, e) => {
    el.removeEventListener("pointermove", mv); el.removeEventListener("pointerup", up); el.removeEventListener("pointercancel", cx); removeEventListener("keydown", key, true);
    try { el.releasePointerCapture(ev.pointerId); } catch { /* already released */ }
    document.body.classList.remove("mmGrab");
    if (live) (ok ? on.done : on.cancel)(e);
  };
  const mv = (e) => {
    if (!live && Math.hypot(e.clientX - x0, e.clientY - y0) < 4) return;
    if (!live) { live = true; document.body.classList.add("mmGrab"); if (on.start) on.start(e); }
    on.move(e, e.clientX - x0, e.clientY - y0);
  };
  const up = (e) => end(true, e), cx = (e) => end(false, e);
  const key = (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); end(false, e); } };
  try { el.setPointerCapture(ev.pointerId); } catch { /* old browser: moves still arrive while over it */ }
  el.addEventListener("pointermove", mv); el.addEventListener("pointerup", up); el.addEventListener("pointercancel", cx); addEventListener("keydown", key, true);
}
function dragUnit(ev, u) {
  const box = $("#units"), rack = $("#rack"); let line = null, before = undefined;
  grab(ev, ev.currentTarget, {
    start() { u.classList.add("mmLift"); line = h(`<div class="mmLine"></div>`); box.appendChild(line); },
    move(e) {
      const r = rack.getBoundingClientRect();
      if (e.clientY < r.top + 30) rack.scrollTop -= 12; else if (e.clientY > r.bottom - 30) rack.scrollTop += 12;
      const vis = [...box.querySelectorAll(":scope > .unit")].filter((x) => x !== u && !x.classList.contains("hidden"));
      before = vis.find((x) => { const b = x.getBoundingClientRect(); return e.clientY < b.top + b.height / 2; }) || null;
      const last = vis[vis.length - 1];
      line.style.top = (before ? before.offsetTop - 2 : last ? last.offsetTop + last.offsetHeight : 0) + "px";
    },
    done() {
      u.classList.remove("mmLift"); line.remove();
      if (before !== undefined && before !== u.nextElementSibling) {
        box.insertBefore(u, before);
        L.order = [...box.querySelectorAll(":scope > .unit")].map((x) => x.dataset.seat); saveLayout();
        window.TTLinks?.draw();
      }
    },
    cancel() { u.classList.remove("mmLift"); line.remove(); },
  });
}
// The side column: its width, which bay is on top, and the split between them.
const COLGRIP = h(`<div id="mmCol" class="mmGrip" title="Drag to size the side column, double-click to reset"></div>`);
const SPLIT = h(`<div id="mmSplit" class="mmGrip" title="Drag to size the bays, double-click to reset"></div>`);
function applySide() {
  const st = $("#studio"), side = $("#side"), [a, b] = L.bays.map((id) => $("#" + id));
  if (!COLGRIP.isConnected) side.before(COLGRIP);
  st.classList.add("mm"); side.classList.add("mm"); st.classList.toggle("sideL", L.side === "left");
  if (L.sideW) st.style.setProperty("--mm-side", L.sideW + "px"); else st.style.removeProperty("--mm-side");
  b.before(a); a.after(SPLIT);
  side.classList.toggle("mmFree", !!L.bayH || L.bays[0] !== "needs");
  side.style.setProperty("--mm-rows", L.bayH ? `minmax(120px, ${L.bayH}px) 12px minmax(120px, 1fr)` : L.bays[0] === "needs" ? "minmax(120px, auto) 12px minmax(220px, 1fr)" : "minmax(120px, 1fr) 12px minmax(120px, 1fr)");
  window.TTLinks?.draw();  // the cable gutter changes sides with the column
}
COLGRIP.onpointerdown = (ev) => {
  const was = L.sideW, w0 = $("#side").getBoundingClientRect().width;
  grab(ev, COLGRIP, { move(e, dx) { L.sideW = Math.round(clampN(w0 + (L.side === "left" ? dx : -dx), 280, 640)); applySide(); },
    done: saveLayout, cancel() { L.sideW = was; applySide(); } });
};
COLGRIP.ondblclick = () => { L.sideW = null; applySide(); saveLayout(); };
SPLIT.onpointerdown = (ev) => {
  const was = L.bayH, h0 = $("#" + L.bays[0]).getBoundingClientRect().height, room = $("#side").getBoundingClientRect().height - 12 - 120;
  grab(ev, SPLIT, { move(e, dx, dy) { L.bayH = Math.round(clampN(h0 + dy, 120, Math.max(120, room))); applySide(); },
    done: saveLayout, cancel() { L.bayH = was; applySide(); } });
};
SPLIT.ondblclick = () => { L.bayH = null; applySide(); saveLayout(); };
document.querySelectorAll("#side > .bay > h2").forEach((hd) => {
  hd.title = "Drag to swap the bays";
  hd.onpointerdown = (ev) => {
    if (ev.target.closest("button, input, a, select")) return;
    const bay = hd.parentElement, other = L.bays.map((id) => $("#" + id)).find((x) => x !== bay); let over = false;
    const off = () => { bay.classList.remove("mmLift"); other.classList.remove("mmOver"); };
    grab(ev, hd, {
      start() { bay.classList.add("mmLift"); },
      move(e) { const r = other.getBoundingClientRect(); over = e.clientY >= r.top && e.clientY <= r.bottom; other.classList.toggle("mmOver", over); },
      done() { off(); if (over) { L.bays = [L.bays[1], L.bays[0]]; L.bayH = null; applySide(); saveLayout(); } },
      cancel: off,
    });
  };
});
const LAYOUT_KEY = h(`<button id="mmLayout" class="mini" title="Rack layout">LAYOUT</button>`);
document.querySelectorAll("#rack .rail")[1].appendChild(LAYOUT_KEY);
LAYOUT_KEY.onclick = (e) => {
  const set = (fn) => () => { fn(); saveLayout(); relayout(); }, ids = ST.board ? ST.board.seats.map((s) => s.seat) : [];
  menu(e, [["Rack layout", null],
    [`${L.hideEmpty ? "✓ " : ""}Hide empty optional seats`, set(() => { L.hideEmpty = !L.hideEmpty; })],
    ["Fold all", set(() => { L.fold = ids; })], ["Unfold all", set(() => { L.fold = []; })],
    [L.side === "left" ? "Side column on the right" : "Side column on the left", set(() => { L.side = L.side === "left" ? "right" : "left"; })],
    ["Reset layout", set(() => setLayout(null))]]);
};
window.TTStudioLayout = {
  get: () => structuredClone(L),
  set: (v) => { setLayout({ ...L, ...v }); saveLayout(); relayout(); },
  reset: () => { setLayout(null); saveLayout(); relayout(); },
};
applySide();
api("ui_prefs", {}, null).then((p) => { setLayout(p && p.studio_layout); relayout(); }).catch(() => { /* defaults */ });

// ---------------------------------------------------------------- a seat's workstation
const TAG = { queued: "", working: "stale", submitted: "cy", needs_you: "bad", done: "ok", failed: "bad", cancelled: "" };
function takeSig(t) { return t.state === "open" ? "open:" + Date.now() : `${t.id}:${t.state}:${t.submitted || 0}:${t.returned || 0}`; }
function abKeys(take) {
  return `<span class="ab" title="Hear the song as it is, then with this take's work in it. Switching keeps your place."><button class="key" data-ab="session" title="The song as it is now, without this take">▶ THE SONG NOW</button><button class="key" data-ab="${esc(take)}" title="The song with ${esc(take)}'s changes in it">▶ WITH ${esc(take.toUpperCase())}</button></span>`;
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
  return yours && s.seat !== "review" ? "tasks" : "bench";  // a person reviews on the bench: the review sheet
}
const reviewing = (s) => s.seat === "review" && s.occupant && s.occupant.kind === "human";
const benchName = (s) => reviewing(s) ? "review" : BENCH_NAME[VIEW[s.seat]];
function renderBench(seat, force = false) {
  const u = ST.units[seat], s = seatOf(seat); if (!u || !s) return;
  const tab = tabOf(s);
  u.querySelectorAll(".tabsB button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const body = u.querySelector(".benchBody");
  const tasks = ST.board.tasks.filter((t) => t.seat === seat), takes = ST.board.takes.filter((t) => t.seat === seat), a = seatAct(seat);
  const B = benchOf(seat);
  const sig = JSON.stringify(tab === "bench" ? [tab, B.version, B.err, [...B.hidden], body.clientWidth, (ST.crew && ST.crew.active && ST.crew.active[seat] || {}).take, s.seat === "producer" ? ST.board.tasks.map((t) => [t.id, t.state, t.depends]) : 0,
      reviewing(s) ? [ST.board.tasks.filter((t) => t.seat === "review").map((t) => [t.id, t.state, t.waiting_for]), ST.board.seats.map((x) => !!x.occupant), ST.board.takes.map((k) => k.id + k.state)] : s.seat === "review" ? [s.occupant, (ST.board.reports || []).length] : 0]
    : tab === "tasks" ? [tab, tasks, s.occupant, takes.map((t) => [t.id, t.state, t.reason])] : tab === "takes" ? [tab, takes, Object.fromEntries(takes.map((t) => [t.id, ST.diffs[t.id]]))]
    : tab === "log" ? [tab, a.lines.length] : [tab, s.occupant, s.mode, s.notes, s.paused, s.signoff, s.options]);
  if (!force && ST.benchSig[seat] === sig) return;
  if (!force && (focusedIn(body) || B.hold)) return;  // don't pull a form out from under someone typing (focusout redraws) or a fader out of a hand
  ST.benchSig[seat] = sig;
  if (tab === "bench") benchWork(body, s);
  else if (tab === "tasks") benchTasks(body, s, tasks);
  else if (tab === "takes") benchTakes(body, s, takes);
  else if (tab === "log") benchLog(body, a);
  else benchSetup(body, s);
}
// ---------------------------------------------------------------- the seat's bench: its work, in its own form
function benchOf(seat) { return ST.bench[seat] || (ST.bench[seat] = { data: null, version: null, take: null, hidden: new Set(), err: null, busy: false, hold: 0, focus: null }); }
async function loadBench(seat) {
  const B = benchOf(seat); if (B.busy) { B.again = true; return; } B.busy = true;
  try {
    const r = await api("seat_bench", { seat, take: B.take, have: B.version });
    if (!r.unchanged) { B.data = r; B.version = r.version; }
    B.err = null;
  } catch (e) {
    if (B.take) { B.take = null; B.version = null; }  // the take it was pinned to is gone: back to the seat's own
    else B.err = e.message;
  }
  B.busy = false;
  if (B.again) { B.again = false; return loadBench(seat); }  // asked again while reading: that read may predate an edit
  if (ST.open === seat) renderBench(seat);
}
let benchTimer = null;
function benchSoon() { clearTimeout(benchTimer); benchTimer = setTimeout(() => { if (ST.open && ST.board && tabOf(seatOf(ST.open)) === "bench") loadBench(ST.open); }, 400); }
// someone may be editing the take on the desk or from another app: a cheap "anything new?" while a bench is open
setInterval(() => { if (!document.hidden && document.body.dataset.room === "studio") benchSoon(); }, 2500);
window.addEventListener("resize", () => { if (ST.open) renderBench(ST.open); });

function benchWork(body, s) {
  const B = benchOf(s.seat), d = B.data;
  if (!d) { body.innerHTML = `<div class="empty">${B.err ? esc(B.err) : "Reading the work…"}</div>`; if (!B.err) loadBench(s.seat); return; }
  const src = d.source, shown = src.take || "session", where = src.take ? esc(src.take.toUpperCase()) : "the session";
  const run = ST.crew && ST.crew.active && ST.crew.active[s.seat], atWork = !!(run && src.take && run.take === src.take);
  const pick = d.takes.length ? [...d.takes, "session"].map((id) => `<button class="mini ${shown === id ? "on" : ""}" data-src="${esc(id)}" title="${id === "session" ? "The music as accepted" : "This seat's take " + esc(id)}">${esc(id.toUpperCase())}</button>`).join("") : "";
  body.innerHTML = `<div class="work">
    <div class="workBar">
      <span class="vfd small">${src.take ? `${esc(src.take.toUpperCase())} · ${src.state === "submitted" ? "HANDED IN" : "IN PROGRESS"} · <span class="amb">${d.changes} CHANGE${d.changes === 1 ? "" : "S"}</span>` : `SESSION · REV ${esc(src.rev ?? "?")}`}</span>
      ${src.task ? `<span class="tag">${esc(src.task)}</span>` : ""}${src.returns ? `<span class="tag stale">RETURNED ${src.returns}×</span>` : ""}
      <span class="srcs">${pick}</span><span class="spacer"></span>
      ${d.view === "mix" ? `${atWork ? `<span class="tag stale" title="This seat's agent is working in this take right now. Your edits and its edits both land in it.">AGENT AT WORK IN ${where}</span>` : ""}<span class="srcs"><button class="mini" data-hist="undo" title="Undo the last change to ${where} (whoever made it)">UNDO</button><button class="mini" data-hist="redo" title="Redo it">REDO</button></span>` : ""}
      ${src.take ? abKeys(src.take) : ""}<button class="key" data-do="desk" title="${d.view === "mix" ? "Open this on the desk's mixer" : "Open this on the desk to edit it"}">OPEN ON DESK</button>
    </div><div class="workBody"></div>${d.truncated ? `<div class="meta">Too many notes to draw them all here: open it on the desk for the rest.</div>` : ""}</div>`;
  const box = body.querySelector(".workBody");
  ({ notes: workRoll, mix: workConsole, sound: workSounds, plan: workPlan, picture: workPicture, report: workReport })[d.view](box, d, B, s);
  wireAb(body);
  body.querySelectorAll("[data-hist]").forEach((b) => b.onclick = async () => {
    const redo = b.dataset.hist === "redo";
    try {
      const r = await api(b.dataset.hist, {}, src.take || null);
      toast(r.undone || r.redone ? `${redo ? "Redid" : "Undid"} the last change to ${src.take || "the session"}.` : `Nothing to ${b.dataset.hist} in ${src.take || "the session"}.`, true);
      delete AB.cache[src.take || "session"];
    } catch (e) { toast(e.message); }
    B.version = null; await loadBench(s.seat);
    if (!src.take || src.take === S.take) loadState().catch(() => {});
  });
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
  // Tracking: under the roll, each recorded track's passes (clips are [start, length, kind, pass, muted]), one thin
  // row a pass, heard bright and muted dim. Read-only.
  const PR = 9, passT = s.seat !== "tracking" ? [] : vis.filter(({ t }) => (t.clips || []).some((c) => c[3] > 0))
    .map((x) => ({ ...x, ps: [...new Set(x.t.clips.map((c) => c[3] || 0))].sort((a, b) => a - b) }));
  const passH = passT.reduce((a, x) => a + 13 + x.ps.length * PR, passT.length ? 6 : 0);
  const end = Math.max(f.end, f.bpb * 4), W = Math.max(wrap.clientWidth - 2, GUT + end * 6), H = TOP + rows * rh + 6 + passH;
  const g = fitCanvas(cv, W, H), X = (b) => GUT + b * (W - GUT - 6) / end, Y = (p) => TOP + (hi - p) * rh;
  g.fillStyle = "#07090a"; g.fillRect(0, 0, W, H);
  for (let p = lo; p <= hi; p++) {
    if ([1, 3, 6, 8, 10].includes(p % 12)) { g.fillStyle = "rgba(255,255,255,.022)"; g.fillRect(GUT, Y(p), W, rh); }
    if (p % 12 === 0) { g.fillStyle = "#1e2422"; g.fillRect(GUT, Y(p) + rh - 1, W, 1); g.fillStyle = "#5a564d"; g.font = `9px ${MONO}`; g.fillText(noteName(p), 6, Y(p) + rh); }
  }
  if (f.loop) { g.fillStyle = "rgba(95,224,122,.5)"; g.fillRect(X(f.loop.start), 0, X(f.loop.end) - X(f.loop.start), 2); }
  const barW = X(f.bpb) - X(0), every = barW >= 26 ? 1 : barW >= 13 ? 2 : 4;
  g.font = `9px ${MONO}`;
  for (let b = 0, bar = 0; b <= end + 1e-6; b += f.bpb, bar++) {
    g.fillStyle = bar % 4 ? "#141918" : "#232a28"; g.fillRect(Math.round(X(b)), 14, 1, H);
    if (bar % every === 0 && b < end) { g.fillStyle = "#5a564d"; g.fillText(bar + 1, X(b) + 3, 11); }
  }
  for (const m of f.markers) { g.fillStyle = "#ffb000"; g.fillRect(Math.round(X(m.at)), 0, 1, H); g.font = `9px ${LEGEND}`; g.fillText(String(m.name).toUpperCase(), X(m.at) + 16, 11); }
  g.font = `10px ${MONO}`;
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
  let py = TOP + rows * rh + 10;
  for (const { t, col, ps } of passT) {
    g.fillStyle = "#1e2422"; g.fillRect(GUT, py - 2, W - GUT, 1);
    g.fillStyle = "#8f897c"; g.font = `9px ${LEGEND}`; g.fillText(String(t.name).toUpperCase() + " · PASSES", GUT + 2, py + 8);
    py += 11; g.font = `8px ${MONO}`;
    for (const p of ps) {
      g.fillStyle = "#5a564d"; g.fillText("P" + p, 6, py + PR - 1);
      g.fillStyle = col;
      for (const c of t.clips) if ((c[3] || 0) === p) { g.globalAlpha = c[4] ? 0.28 : 0.95; g.fillRect(X(c[0]), py + 1, Math.max(2, X(c[0] + c[1]) - X(c[0]) - 1), PR - 2); }
      g.globalAlpha = 1; py += PR;
    }
    py += 2;
  }
  if (!vis.some((x) => (x.t.notes || []).length) && !passT.length) { g.fillStyle = "#5a564d"; g.font = `12px ${UI_FONT}`; g.fillText(parts.length ? "Nothing written yet." : "No tracks yet.", GUT + 14, TOP + 26); }
}
// Mix: the console (console.js). The bench shows the Mix seat's take, else the session, and every edit goes there;
// a take shows each moved control next to where it was.
function workConsole(box, d, B, s) {
  TTConsole.render(box, d, B, {
    take: d.source.take || null,
    active: () => ST.open === s.seat,
    redraw: () => renderBench(s.seat, true),
    edited: () => { delete AB.cache[d.source.take || "session"]; },
    reload: async () => {
      B.version = null; await loadBench(s.seat);
      if (!d.source.take || d.source.take === S.take) loadState().catch(() => {});  // the desk shows the same music: bring it along
    },
  });
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
    <div class="flow">${tasks.map((t) => `<div class="fl ${OPEN_STATES.includes(t.state) ? "" : "done"}"><span class="id">${esc(t.id)}</span><span class="st">${SHORT[t.seat] || esc(t.seat)}</span><span class="ti" title="${esc(t.detail || "")}">${esc(t.title)}</span>${(t.depends || []).length ? `<span class="dep">after ${esc(t.depends.join(" "))}</span>` : ""}${t.held ? `<span class="tag stale">your OK</span>` : ""}<span class="tag ${TAG[t.state] || ""}">${esc(t.state.replace("_", " "))}</span></div>`).join("") || `<div class="empty">No tasks yet. BRIEF starts the session.</div>`}</div>`;
}
// Spotting: the picture as the music meets it. A timeline (film strip, cuts, loud production sound, bars, hits),
// then the cue sheet with a frame per hit, the cuts and the stills. Read-only: hits are marked on the Desk.
const mss = (t) => `${t < 0 ? "-" : ""}${Math.floor(Math.abs(t) / 60)}:${(Math.abs(t) % 60).toFixed(1).padStart(4, "0")}`;
function workPicture(box, d, B, s) {
  const P = d.picture, v = P.picture, f = d.form, take = d.source.take;
  const q = take ? "&take=" + encodeURIComponent(take) : "";
  const ver = encodeURIComponent(v ? `${v.file}:${(v.mark || []).join(":")}` : "");
  const still = (st) => `/api/picture?kind=still&n=${st.n}${q}&v=${encodeURIComponent(st.file)}`;
  const stills = P.stills.length ? `<div class="picSub">Stills</div><div class="stillsB">${P.stills.map((st) => `<figure><a href="${still(st)}" target="_blank" rel="noopener" title="Open the full image"><img loading="lazy" src="${still(st)}" alt="${esc(st.note || st.file)}"></a><figcaption>${esc(st.note || st.file)}</figcaption></figure>`).join("")}</div>` : "";
  if (!v && !P.hits.length && !(P.gone || []).length) {
    box.innerHTML = `<div class="empty">No picture and no hits yet. Set a video with + Pic on the Desk (or the picture tool), and mark hits with H or a double-click on the picture lane.</div>${stills}`;
    return;
  }
  const miss = (h) => h.on ? `ON ${h.on.toUpperCase()}` : h.off_ms == null ? "—" : `${h.off_ms > 0 ? "+" : ""}${h.off_ms} ms`;
  const row = (h, i, gone) => `<div class="cue ${gone ? "gone" : h.on ? "on" : h.soft ? "soft" : ""}">
      ${v ? `<img class="th" loading="lazy" alt="" src="/api/picture?kind=frame&t=${h.t}&w=160${q}&v=${ver}">` : `<span class="th"></span>`}
      <span class="id">${gone ? "×" : i + 1}</span><span class="nm" title="${esc(h.name)}">${esc(h.name)}</span>
      <span class="num">${esc(h.tc || mss(h.t))}</span><span class="num">${esc(h.at)}</span><span class="miss">${miss(h)}</span>
      <span class="tags">${h.soft ? `<span class="tag">SOFT</span>` : ""}${h.new ? `<span class="tag cy">NEW</span>` : ""}${gone ? `<span class="tag bad">REMOVED</span>` : ""}</span></div>`;
  box.innerHTML = `<div class="planHead"><span class="vfd small">${v ? `${esc(v.file)} · ${mss(v.duration)} · ${esc(v.fps)} FPS · FIRST FRAME AT ${esc(v.start)} S${v.tc ? " · TC " + esc(v.tc) : ""}` : "NO VIDEO · HITS IN SONG TIME"} · ${esc(+(+f.tempo).toFixed(2))} BPM</span>
      ${v && v.size ? `<span class="tag">${esc(v.size.join("×"))}</span>` : ""}${v ? `<span class="tag">${v.audio ? "has sound" : "no sound"}</span>` : ""}<span class="tag">${P.hits.length} HIT${P.hits.length === 1 ? "" : "S"}</span>${v ? `<span class="tag">${P.cuts.length} CUT${P.cuts.length === 1 ? "" : "S"}</span>` : ""}</div>
    <div class="picTL"><canvas></canvas></div>
    <div class="cues">${P.hits.map((h, i) => row(h, i)).join("") || `<div class="meta">No hits yet: H or a double-click on the Desk's picture lane marks one.</div>`}${(P.gone || []).map((h) => row(h, 0, true)).join("")}</div>
    ${P.cuts.length ? `<div class="picSub">Cuts</div><div class="cutsL">${P.cuts.map(([t, at]) => `<span>${mss(t)} · ${esc(at)}</span>`).join("")}</div>` : v ? `<div class="meta">No cuts found${P.strip ? "" : " (the picture has not been read yet)"}.</div>` : ""}
    ${stills}`;
  const cv = box.querySelector("canvas"), wrap = cv.parentElement;
  // the strip is one image for the whole picture: loaded once per file, then every redraw just paints it
  const key = v && P.strip ? `/api/picture?kind=strip${q}&v=${ver}` : "";
  if ((B.strip || {}).key !== key) { B.strip = { key, img: null }; if (key) { const im = new Image(); im.onload = () => { if (B.picDraw) B.picDraw(); }; im.src = key; B.strip.img = im; } }
  const draw = () => {
    if (!cv.isConnected) return;
    const span = Math.max(1, P.span), W = Math.max(200, wrap.clientWidth - 2), LAB = 3, H = 14 + 40 + 8 + LAB * 14 + 4;
    const g = fitCanvas(cv, W, H), X = (t) => 4 + Math.max(0, Math.min(span, t)) * (W - 10) / span, SY = 14, SH = 40;
    g.fillStyle = "#07090a"; g.fillRect(0, 0, W, H);
    if (v) {
      g.fillStyle = "#141416"; g.fillRect(X(0), SY, X(v.duration) - X(0), SH);
      const im = B.strip && B.strip.img, m = P.strip;
      if (im && im.complete && im.naturalWidth && m) {
        const tw = SH * m.w / m.h;
        for (let i = 0; i < m.n; i++) {
          const a = X(i * m.step), b = X(Math.min(v.duration, (i + 1) * m.step));
          g.save(); g.beginPath(); g.rect(a, SY, Math.max(1, b - a - 1), SH); g.clip();
          for (let x = a; x < b; x += tw + 1) g.drawImage(im, i * m.w, 0, m.w, m.h, x, SY, tw, SH);
          g.restore();
        }
      }
      g.strokeStyle = "#3b3b40"; g.strokeRect(Math.round(X(0)) + 0.5, SY + 0.5, Math.round(X(v.duration) - X(0)) - 1, SH - 1);
      g.fillStyle = "rgba(92,200,255,.28)";  // loud production sound: dialogue and effects the music plays around
      for (const [a, b] of P.loud) g.fillRect(X(a), SY + SH + 2, Math.max(1, X(b) - X(a)), 4);
      g.fillStyle = "rgba(92,200,255,.8)";
      for (const [t] of P.peaks) g.fillRect(Math.round(X(t)), SY + SH + 1, 1, 6);
      g.fillStyle = "rgba(255,255,255,.8)";
      for (const [t] of P.cuts) { g.fillRect(Math.round(X(t)), SY - 3, 1, 5); g.fillRect(Math.round(X(t)), SY + SH - 2, 1, 5); }
    }
    g.font = `9px ${MONO}`; g.textBaseline = "alphabetic";
    const barW = P.bars.length > 1 ? X(P.bars[1][0]) - X(P.bars[0][0]) : W, every = barW >= 22 ? 1 : barW >= 11 ? 2 : barW >= 5 ? 4 : 8;
    for (const [t, n] of P.bars) {
      g.fillStyle = (n - 1) % 4 ? "rgba(255,255,255,.08)" : "rgba(255,255,255,.16)"; g.fillRect(Math.round(X(t)), 12, 1, H - 12);
      if ((n - 1) % every === 0) { g.fillStyle = "#5a564d"; g.fillText(n, X(t) + 3, 10); }
    }
    // hits: a line down the whole timeline, the label in the first row it fits in
    g.font = `10px ${MONO}`; g.textBaseline = "top";
    const ends = new Array(LAB).fill(-1e9), LY = SY + SH + 9;
    for (const [h, gone] of [...P.hits.map((x) => [x, false]), ...(P.gone || []).map((x) => [x, true])]) {
      const x = Math.round(X(h.t)), col = gone ? "#ff4a3a" : h.on ? "#5fe07a" : h.soft ? "#b98a2c" : "#ffb000";
      const label = h.name + (gone ? "  removed" : h.on ? `  ● ${h.on}` : h.off_ms == null ? "" : `  ${h.off_ms > 0 ? "+" : ""}${h.off_ms} ms`);
      const w = g.measureText(label).width + 8;
      let r = ends.findIndex((e) => e < x); if (r < 0) r = ends.indexOf(Math.min(...ends));
      ends[r] = x + w + 4;
      g.globalAlpha = gone ? 0.6 : 1; g.fillStyle = col;
      if (gone) { for (let y = SY; y < H; y += 4) g.fillRect(x, y, 1, 2); } else g.fillRect(x, SY, h.soft ? 1 : 2, H - SY);
      g.fillStyle = "rgba(9,9,10,.86)"; g.fillRect(x + 2, LY + r * 14, w, 13);
      g.fillStyle = col; g.fillText(label, x + 6, LY + r * 14 + 2); g.globalAlpha = 1;
      if (h.new) { g.fillStyle = "#5cc8ff"; g.fillRect(x - 2, SY - 4, 6, 3); }
    }
  };
  B.picDraw = draw; draw();
}
// Review: with you in the seat, the review sheet (review.js): listen, write, send it back. With an agent, the last
// render as measured and the review it filed last.
function workReport(box, d, B, s) {
  if (s.occupant && s.occupant.kind === "human") {
    TTReview.render(box, d, {
      get board() { return ST.board; }, title, audition, hush, AB, refresh: () => refresh(true),
      crewRunning: () => !!(ST.crew && (ST.crew.running || ST.crew.elsewhere)),
    });
    return;
  }
  const r = d.render, last = (ST.board.reports || []).filter((x) => x.by === "review").slice(-1)[0];
  const filed = last ? `<div class="meta" style="margin-top:8px">Last review: <u data-report="${esc(last.id)}" title="Read it">${esc(last.id)} ${esc(last.title)}</u></div>` : "";
  if (!r) { box.innerHTML = `<div class="empty">Nothing rendered yet. Play or A/B renders it; the measurements land here.</div>${filed}`; return; }
  const bands = Object.entries(r.bands_pct), top = Math.max(1, ...bands.map((b) => b[1]));
  box.innerHTML = `<div class="planHead"><span class="vfd small">${esc(r.lufs)} LUFS · PEAK ${esc(r.true_peak_dbtp)} dBTP · ${esc(r.duration_s)} S${r.loop_mode && r.loop_mode !== "off" ? " · " + esc(String(r.loop_mode).toUpperCase()) : ""}</span>
      <span class="tag ${r.stale ? "stale" : "ok"}">${r.stale ? "OLDER THAN THE MUSIC" : "CURRENT"}</span><span class="tag">width ${esc(r.stereo_correlation)}</span><span class="tag">limiter ${esc(r.limiter_db)} dB</span></div>
    <div class="report2"><div class="bandsB tall">${bands.map(([k, v]) => `<div title="${esc(k)}: ${v}%"><i style="height:${v / top * 100}%"></i><span>${esc(k.replace("_", " "))}<br>${v}%</span></div>`).join("")}</div>
      <div class="lvls">${d.tracks.filter((t) => t.level).map((t, i) => `<div><span>${esc(t.name)}</span><div class="hbar"><i style="width:${Math.max(0, Math.min(100, (t.level.rms_dbfs + 54) / 54 * 100))}%;background:${trackColor(t, i)}"></i></div><span class="num">${esc(t.level.rms_dbfs)} rms · ${esc(t.level.peak_dbfs)} pk</span></div>`).join("")}</div></div>
    ${r.warnings.length ? `<ul class="changes">${r.warnings.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<div class="meta ok">No warnings from the last render.</div>`}${filed}`;
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
      <h4><span class="id">${esc(t.id)}</span>${esc(t.title)}<span class="tag ${TAG[t.state] || ""}">${esc(t.state.replace("_", " "))}</span>${t.held ? `<span class="tag stale" title="Part of a plan waiting for your OK on Needs You">YOUR OK</span>` : ""}${t.options ? `<span class="tag" title="The seat hands in this many versions and you choose">${t.options} VERSIONS</span>` : ""}${t.waiting_for ? `<span class="tag">after ${esc(t.waiting_for.join(", "))}</span>` : ""}${t.take ? `<span class="tag cy">${esc(t.take)}</span>` : ""}</h4>
      ${t.detail ? `<p>${esc(t.detail)}</p>` : ""}${t.result ? `<p class="meta">Result: ${esc(t.result)}</p>` : ""}${cameBack(t)}
      <div class="row" style="margin-top:8px">${OPEN_STATES.includes(t.state)
        ? `${byTake && t.state !== "submitted" && !t.waiting_for ? `<button class="key go" data-do="take" title="Do this task on the desk in a take, then hand it in">${openTakeOf(t) ? `CONTINUE ${esc(openTakeOf(t).toUpperCase())} ON THE DESK` : "WORK IN A TAKE"}</button>` : ""}${reviewing(s) ? `<button class="key go" data-do="review" title="Listen and write the review on the review sheet; sending it closes this task">${t.waiting_for ? "START IT NOW" : "WRITE THE REVIEW"}</button>` : human && t.state !== "submitted" ? `<button class="key" data-do="done">MARK DONE</button>` : ""}${t.state === "needs_you" ? `<button class="key" data-do="queued">BACK IN THE QUEUE</button>` : ""}<button class="key warn" data-do="cancelled">CANCEL TASK</button>`
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
    if (state === "review") { TTReview.pick(id); ST.tab[s.seat] = "bench"; delete ST.benchSig[s.seat]; renderUnits(); return; }
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
function logHtml(lines) { return lines.map((l) => `<div class="${l.cls}" data-seat="${esc(l.seat || "")}"><time>${clock(l.at)}</time>${l.html}</div>`).join(""); }
// newest at the top; someone scrolled down reading stays on the same lines as new ones arrive above
function benchLog(body, a) {
  const top = body.scrollTop, was = body.scrollHeight;
  body.innerHTML = a.lines.length ? `<div class="log">${logHtml(a.lines.slice(-200).reverse())}</div>` : `<div class="empty">Nothing has happened in this seat yet.</div>`;
  body.scrollTop = top > 40 ? top + body.scrollHeight - was : 0;
}
// Setup: the seat's settings as a row of modules, each a legend, a switch and a readout of what the setting does.
// Switches apply at once (like the keys on the unit's face); the notes are stored with their own key.
const SU_WORKS = { propose: "Works in takes: nothing is heard until you or the Producer accept it.", direct: "Edits the session itself: changes are live at once." };
function benchSetup(body, s) {
  const seat = s.seat, isProd = seat === "producer", o = s.occupant, mine = o && o.kind === "human";
  const versions = s.owns.length && !["producer", "review"].includes(seat);
  const P = { mode: s.mode, sign: !!s.signoff, options: s.options || 0 };
  let saved = s.notes || "";
  const signOff = isProd ? "Its decisions and plans go ahead without you." : seat === "review" ? "Its fix tasks go ahead without you." : "Its hand-ins go straight to the Producer.";
  const signOn = (SIGN_TIP[seat] || SIGN_TIP.take).replace(/^[^:]*: /, "").replace(/^./, (c) => c.toUpperCase()) + ".";
  const who = !o ? `<span class="dimv">— VACANT —</span>` : esc((s.label || o.kind).toUpperCase());
  const facts = !o ? "Nobody sits here" : o.kind === "agent" ? [`effort ${o.effort || "auto"}`, o.audition_model ? `auditions on ${o.audition_model}` : ""].filter(Boolean).join(" · ")
    : o.kind === "human" ? "You do this seat's work and hand it in" : "An outside app sits in over MCP";
  const mod = (k, legend, inner, cls = "") => `<section class="su ${cls}" data-m="${k}"><h6><i class="led"></i>${legend}</h6>${inner}</section>`;
  const seg = (k, opts) => `<div class="seg">${opts.map(([v, t, tip]) => `<button data-${k}="${v}"${tip ? ` title="${esc(tip)}"` : ""}>${t}</button>`).join("")}</div>`;
  body.innerHTML = `<div class="suDeck">
    ${mod("who", "In the seat", `<div class="vfd suScreen"><div class="suWho">${who}</div><div class="dimv">${esc(facts)}</div></div>
      <div class="suFoot"><span class="tag">tier ${esc(s.tier)}</span><button class="key" data-do="pick">CHANGE…</button></div>`, "wide")}
    ${mod("mode", "Works", `${seg("mode", [["propose", "IN TAKES"], ["direct", "ON THE SESSION"]])}<p class="suRead"></p>`)}
    ${mod("sign", isProd ? "Co-Producer" : "Sign-off", `${seg("sign", [["", isProd ? "OFF" : "STRAIGHT ON"], ["1", isProd ? "MY OK FIRST" : "STOPS AT ME"]])}<p class="suRead"></p>`, mine ? "off" : "")}
    ${versions ? mod("options", "Versions", `${seg("opt", [["0", "ONE", "One take per task"], ["2", "TWO", "Two versions of each task for you to choose from"], ["3", "THREE", "Three versions of each task for you to choose from"]])}<p class="suRead"></p>`) : ""}
    ${mod("dept", "Department", `<div class="suOwns">${s.owns.length ? s.owns.map((x) => `<span class="tag cy">${esc(x)}</span>`).join("") : `<span class="tag">changes nothing</span>`}</div>
      <p class="suRead">${s.owns.length ? "What this seat may change in a take." : "It listens and reports."}</p>`)}
    ${mod("notes", "Standing notes", `<textarea data-f="notes" rows="3" spellcheck="true" placeholder="Instructions this seat always gets, e.g. “no brass”, “keep it under 8 voices”">${esc(s.notes || "")}</textarea>
      <div class="suFoot"><span class="suHint">Every run in this seat reads these. Ctrl+Enter stores them.</span><button class="key" data-do="revert">REVERT</button><button class="key go" data-do="notes">STORE</button></div>`, "full")}
  </div>`;
  const q = (sel) => body.querySelector(sel), ta = q('[data-f="notes"]');
  // the readouts and lit switches follow P, so a click shows at once (the bench doesn't redraw under focus)
  const paint = () => {
    body.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === P.mode));
    body.querySelectorAll("[data-sign]").forEach((b) => b.classList.toggle("on", !!b.dataset.sign === P.sign));
    body.querySelectorAll("[data-opt]").forEach((b) => b.classList.toggle("on", +b.dataset.opt === P.options));
    q('[data-m="mode"] .suRead').textContent = SU_WORKS[P.mode] || "";
    q('[data-m="mode"]').classList.toggle("lit", P.mode === "direct");
    q('[data-m="sign"] .suRead').textContent = mine ? "You sit here: your own work needs no sign-off."
      : P.sign && versions && P.mode === "direct" ? "Nothing to stop while it works on the session: set Works to IN TAKES." : P.sign ? signOn : signOff;
    q('[data-m="sign"]').classList.toggle("lit", P.sign && !mine);
    if (versions) { q('[data-m="options"] .suRead').textContent = P.options ? `${P.options === 2 ? "Two" : "Three"} versions of each task: you choose.` : "One take per task."; q('[data-m="options"]').classList.toggle("lit", !!P.options); }
    const dirty = ta.value !== saved;
    q('[data-do="notes"]').disabled = q('[data-do="revert"]').disabled = !dirty; q('[data-m="notes"]').classList.toggle("lit", dirty);
  };
  const set = async (args, undo, said) => {
    paint();
    try { const r = await call(args.on_ !== undefined ? "seat_signoff" : "seat_assign", { seat, ...args }); toast(r && r.note || said, true); }
    catch { undo(); paint(); }
  };
  body.querySelectorAll("[data-mode]").forEach((b) => b.onclick = () => { const was = P.mode; if (was === b.dataset.mode) return; P.mode = b.dataset.mode;
    set({ mode: P.mode }, () => { P.mode = was; }, `${s.title}: ${P.mode === "direct" ? "works on the session" : "works in takes"} from its next run.`); });
  body.querySelectorAll("[data-sign]").forEach((b) => b.onclick = () => { const v = !!b.dataset.sign; if (v === P.sign || mine) return; const was = P.mode; P.sign = v;
    if (v && versions) P.mode = "propose";  // signoff.py: a seat signed off works in takes, so its hand-ins can stop
    set({ on_: v }, () => { P.sign = !v; P.mode = was; }, `${s.title}: ${isProd ? "Co-Producer" : "sign-off"} ${v ? "on" : "off"}.`); });
  body.querySelectorAll("[data-opt]").forEach((b) => b.onclick = () => { const was = P.options, v = +b.dataset.opt; if (v === was) return; P.options = v;
    set({ options: v }, () => { P.options = was; }, `${s.title}: ${v ? `${v === 2 ? "two" : "three"} versions` : "one take"} per task from its next task.`); });
  const store = async () => { try { await call("seat_assign", { seat, notes: ta.value }); saved = ta.value; toast(`${s.title}: notes stored. Its next run reads them.`, true); } catch { /* call toasts */ } paint(); };
  q('[data-do="notes"]').onclick = store;
  q('[data-do="revert"]').onclick = () => { ta.value = saved; paint(); };
  ta.oninput = paint;
  ta.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (!q('[data-do="notes"]').disabled) store(); } };
  q('[data-do="pick"]').onclick = () => openSeatPicker(seat);
  paint();
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
  // A sheet: who sits here (four kinds), and for an AI agent the app on the left and its models as a table;
  // effort, audition model and how the seat works along the bottom.
  const P = { who: cur.kind || "none", rid: cur.kind === "agent" ? cur.runner : null, mid: cur.kind === "agent" ? cur.model : null,
    effort: cur.kind === "agent" ? cur.effort || "" : "", aud: cur.audition_model || "", mode: s.mode, sign: !!s.signoff, q: "" };
  if (!P.rid) P.rid = (runners.find((r) => r.available && r.auth !== "missing") || runners.find((r) => r.available) || runners[0] || {}).id || null;
  const WHO = [["none", "Nobody", "Leave the seat empty"], ["human", "You", "Do this seat's work yourself and hand it in"],
    ["external", "Outside app", "Claude Desktop, Codex… joins over MCP with seat_join"], ["agent", "AI agent", "A model on an app here runs the seat"]];
  const many1 = many.length === 1, isProd = seat === "producer" && many1;
  const el = Sheet.open({ title: many.map((x) => seatOf(x).title).join(", "), sub: "WHO SITS HERE", size: "seat",
    body: `<div class="sp"><p class="spWhy">${esc(s.purpose)} <span>Suggested tier <b>${esc(s.tier)}</b></span></p>
      <div class="tiles" role="radiogroup">${WHO.map(([k, t, d]) => `<button role="radio" data-who="${k}"><b>${t}</b><span>${esc(d)}</span></button>`).join("")}</div>
      <div class="spAgent"><nav class="spApps"></nav><div class="spModels"></div></div>
      <div class="spSet"></div></div>`,
    foot: `<button class="mini" id="spScan" title="Look again for installed agent apps and their models">RESCAN APPS</button><span class="spacer"></span>
      <button class="key" id="spCancel">CANCEL</button><button class="key go" id="spOk">SEAT</button>` });
  const runner = () => runners.find((r) => r.id === P.rid);
  const model = () => { const r = runner(); return r && r.models.find((m) => m.id === P.mid); };
  // model facts the apps report (detect.py `info`): context, price per 1M tokens, what it can take in
  const CAN = { reasoning: ["THINK", "Reasons before answering (thinking / effort levels)"], tools: ["TOOLS", "Calls tools"],
    images: ["IMG", "Reads images (e.g. analyze()'s piano roll)"], pdf: ["PDF", "Reads PDFs"], audio: ["AUD", "Hears audio input"], video: ["VID", "Watches video input"] };
  const toks = (n) => n >= 1e6 ? `${+(n / 1e6).toFixed(n % 1e6 ? 1 : 0)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : String(n);
  const usd = (v) => v == null ? "?" : v === 0 ? "0" : v < 1 ? `${+v.toFixed(3)}` : `${+v.toFixed(2)}`;
  const priceCell = (m) => { const p = m.info && m.info.price;
    if (m.cost === "free" || (p && !p.in && !p.out)) return `<em class="tag ok">free</em>`;
    return p ? `<span class="spNum">$${usd(p.in)} · $${usd(p.out)}</span>` : `<em class="tag" title="${m.cost === "paid" && P.rid === "codex" ? "Billed to your ChatGPT plan" : m.cost === "paid" && P.rid === "agy" ? "Uses your Antigravity plan quota" : "No price reported"}">${esc(m.cost || "—")}</em>`; };
  function infoStrip(r, m) {
    if (!m) return "";
    const i = m.info || {}, p = i.price, bits = [];
    if (i.context) bits.push(`<span><b>${toks(i.context)}</b> context</span>`);
    if (i.output) bits.push(`<span><b>${toks(i.output)}</b> max reply</span>`);
    if (p && (p.in || p.out)) bits.push(`<span><b>$${usd(p.in)}</b> in · <b>$${usd(p.out)}</b> out${p.cache != null ? ` · $${usd(p.cache)} cached` : ""} per 1M tokens${r.id === "claude" ? " (API rates; a Claude plan sign-in uses plan usage)" : ""}</span>`);
    else if (m.cost === "free" || (p && !p.in && !p.out)) bits.push(`<span><b>Free</b></span>`);
    else if (r.id === "codex") bits.push(`<span>Billed to your ChatGPT plan</span>`);
    else if (r.id === "agy") bits.push(`<span>Uses your Antigravity plan quota</span>`);
    if (m.efforts && m.efforts.length) bits.push(`<span>Effort: ${m.efforts.map(esc).join(", ")}</span>`);
    if (i.released) bits.push(`<span>Released ${esc(i.released)}</span>`);
    return `<div class="spInfo"><div class="spInfoTop"><b>${esc(i.name || m.label || m.id)}</b>
        ${(i.can || []).map((c) => CAN[c] ? `<em class="tag" title="${CAN[c][1]}">${CAN[c][0]}</em>` : "").join("")}
        ${i.trains ? `<em class="tag bad" title="The provider may train on what the agent sends it (your project, notes and briefs)">MAY TRAIN ON PROMPTS</em>` : ""}</div>
      ${i.about ? `<p>${esc(i.about)}</p>` : ""}${bits.length ? `<div class="spInfoFacts">${bits.join("")}</div>` : ""}</div>`;
  }
  const status_ = (r) => !r.available ? `<span class="tag bad">NOT INSTALLED</span>` : r.auth === "missing" ? `<span class="tag bad">SIGN IN</span>` : `<span class="lamp green" title="Ready"></span>`;
  function draw() {
    el.querySelectorAll("[data-who]").forEach((b) => { b.classList.toggle("on", b.dataset.who === P.who); b.setAttribute("aria-checked", b.dataset.who === P.who); });
    const ag = el.querySelector(".spAgent"); ag.classList.toggle("hidden", P.who !== "agent");
    if (P.who === "agent") {
      el.querySelector(".spApps").innerHTML = `<h6>Apps</h6>` + (runners.length ? runners.map((r) => `<button data-rid="${esc(r.id)}" class="${r.id === P.rid ? "on" : ""} ${r.available ? "" : "dim"}">
          <span>${esc(r.name)}</span>${status_(r)}</button>`).join("") : `<p class="muted">No agent apps found. Install Claude Code, Codex, Kilo or Antigravity, then RESCAN APPS.</p>`);
      const r = runner(), ms = (r && r.available ? r.models : []).slice();
      // the model sitting here now, even when the app no longer lists it
      if (r && r.available && cur.kind === "agent" && cur.runner === r.id && !ms.some((m) => m.id === cur.model)) ms.unshift({ id: cur.model, label: `${cur.model} (seated now)` });
      el.querySelector(".spModels").innerHTML = !r ? "" : !r.available ? `<p class="spNote">${esc(r.name)} is not installed on this computer.</p>`
        : `${r.auth === "missing" ? `<p class="spNote bad">Not signed in${r.notes && r.notes[0] ? `: ${esc(r.notes[0])}` : "."}</p>` : ""}
          ${ms.length > 12 ? `<div class="plTools spFind"><input class="shQ" type="search" spellcheck="false" autocomplete="off" placeholder="FILTER MODELS · free, claude, gpt…" value="${esc(P.q)}"><span class="plCount"></span></div>` : ""}
          <div class="spHead"><span>Model</span><span>Tier</span><span title="Context window, in tokens">Context</span><span title="Price per million tokens, in / out">$ / 1M in · out</span><span>Can</span><span>In this seat</span></div>
          <div class="spRows" role="radiogroup">${ms.map((m) => { const i = m.info || {}; return `<button role="radio" data-mid="${esc(m.id)}" class="${P.mid === m.id && P.rid === r.id ? "on" : ""}" aria-checked="${P.mid === m.id && P.rid === r.id}" title="${esc(i.about || m.id)}">
            <span class="spM"><i class="dot"></i>${esc(m.label || m.id)}</span><span><em class="tag">${esc(m.tier || "—")}</em></span>
            <span class="spNum">${i.context ? toks(i.context) : "—"}</span><span>${priceCell(m)}</span>
            <span class="spCan">${(i.can || []).filter((c) => CAN[c] && c !== "tools").map((c) => `<em title="${CAN[c][1]}">${CAN[c][0]}</em>`).join("") || `<span class="muted">—</span>`}</span>
            <span>${record(r.id, m.id) || `<span class="muted">—</span>`}</span></button>`; }).join("")}</div>`;
      // filter in place (no redraw, so typing keeps focus); every word must match the id or its cost, the seated model stays
      const q = el.querySelector(".spFind .shQ"), rows = [...el.querySelectorAll(".spRows [data-mid]")];
      const filter = () => {
        const words = P.q.toLowerCase().split(/\s+/).filter(Boolean); let n = 0;
        rows.forEach((b) => { const m = ms.find((x) => x.id === b.dataset.mid), hay = `${m.id} ${m.cost || ""} ${m.tier || ""} ${(m.info && m.info.name) || ""} ${((m.info && m.info.can) || []).map((c) => `${c} ${CAN[c] ? CAN[c][0] : ""}`).join(" ")}`.toLowerCase();
          const show = b.classList.contains("on") || words.every((w) => hay.includes(w)); b.hidden = !show; n += show; });
        const c = el.querySelector(".spFind .plCount"); if (c) c.textContent = words.length ? `${n} of ${rows.length}` : `${rows.length} models`;
      };
      if (q) { q.oninput = () => { P.q = q.value; filter(); }; filter(); }
      el.querySelectorAll("[data-rid]").forEach((b) => b.onclick = () => { if (P.rid !== b.dataset.rid) { P.rid = b.dataset.rid; P.mid = null; P.effort = ""; P.q = ""; } draw(); });
      el.querySelectorAll("[data-mid]").forEach((b) => {
        b.onclick = () => { P.mid = b.dataset.mid; if (cur.runner !== P.rid || cur.model !== P.mid) P.effort = ""; draw(); };
        b.ondblclick = () => { P.mid = b.dataset.mid; ok(); };
      });
    }
    // settings along the bottom
    const r = runner(), m = model(), efforts = P.who === "agent" ? (m && m.efforts) || (r && r.efforts) || [] : [];
    const others = isProd && P.who === "agent" && r && P.mid ? r.models.filter((x) => x.id !== P.mid) : [];
    el.querySelector(".spSet").innerHTML = `${P.who === "agent" && r ? infoStrip(r, m) : ""}
      ${P.who === "agent" ? `<div class="fld"><span>Effort</span>${efforts.length ? `<div class="seg">${["", ...efforts].map((e) => `<button data-eff="${esc(e)}" class="${P.effort === e ? "on" : ""}">${esc(e || "default")}</button>`).join("")}</div>`
        : `<span class="muted spNone">${P.mid ? "this model has one setting" : "choose a model"}</span>`}</div>` : ""}
      ${isProd && P.who === "agent" ? `<label class="fld" title="Runs whose only job is to audition handed-in takes. Planning, and the audition that may close the last open work (where the final listen happens), stay on the model chosen above."><span>Auditions on</span>
        <select id="spAud" ${others.length ? "" : "disabled"}><option value="">the same model</option>${others.map((x) => `<option value="${esc(x.id)}" ${P.aud === x.id ? "selected" : ""}>${esc(x.label || x.id)}${x.cost ? ` · ${esc(x.cost)}` : ""}</option>`).join("")}</select></label>` : ""}
      ${many1 && P.who !== "none" ? `<div class="fld"><span>Works</span><div class="seg"><button data-mode="propose" class="${P.mode === "propose" ? "on" : ""}" title="Proposes changes in takes that you or the Producer accept">IN TAKES</button>
        <button data-mode="direct" class="${P.mode === "direct" ? "on" : ""}" title="Edits the session itself">ON THE SESSION</button></div></div>` : ""}
      ${many1 && P.who === "agent" ? `<div class="fld" title="${esc(SIGN_TIP[seat] || SIGN_TIP.take)}"><span>${isProd ? "Co-Producer" : "Your sign-off"}</span><div class="seg">
        <button data-sign="" class="${P.sign ? "" : "on"}">${isProd ? "OFF" : "STRAIGHT ON"}</button><button data-sign="1" class="${P.sign ? "on" : ""}">${isProd ? "MY OK FIRST" : "STOPS AT ME"}</button></div></div>` : ""}`;
    el.querySelectorAll("[data-eff]").forEach((b) => b.onclick = () => { P.effort = b.dataset.eff; draw(); });
    el.querySelectorAll("[data-mode]").forEach((b) => b.onclick = () => { P.mode = b.dataset.mode; draw(); });
    el.querySelectorAll("[data-sign]").forEach((b) => b.onclick = () => { P.sign = !!b.dataset.sign; draw(); });
    const aud = el.querySelector("#spAud"); if (aud) aud.onchange = () => { P.aud = aud.value; };
    const go = el.querySelector("#spOk"); go.disabled = P.who === "agent" && !P.mid;
  }
  el.querySelectorAll("[data-who]").forEach((b) => b.onclick = () => { P.who = b.dataset.who; draw(); });
  async function ok() {
    if (P.who === "agent" && !P.mid) { toast("Choose a model"); return; }
    const who = P.who === "agent" ? { kind: "agent", runner: P.rid, model: P.mid, effort: P.effort || null } : { kind: P.who };
    if (P.who === "agent" && isProd) who.audition_model = P.aud;  // "" turns it off
    Sheet.close(true);
    // several seats: each keeps its own way of working (takes or direct); only who sits there changes
    for (const x of many) await call("seat_assign", { seat: x, ...who, ...(many1 ? { mode: P.mode } : {}), ...(many1 && P.who === "agent" ? { signoff: P.sign } : {}) });
    if (opts.done) opts.done(P.mid || (P.who === "human" ? "you" : P.who));
  }
  draw();
  const on = el.querySelector(".spRows button.on"); if (on) on.scrollIntoView({ block: "nearest" });
  el.querySelector("#spCancel").onclick = () => Sheet.close();
  el.querySelector("#spScan").onclick = async () => { status("scanning for agent apps…", "busy"); try { ST.agents = await api("crew_detect", { refresh: true }); } catch (e) { toast(e.message); } status(""); openSeatPicker(seat, opts); };
  el.querySelector("#spOk").onclick = ok;
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
// One item as a ticket with the answers that fit it (the Needs You sheet shows these for questions and blockers).
function ticketEl(a) {
  const takes = ticketTakes(a);
  const audition = !!a.take, fromSeat = a.from !== "you", kind = LISTEN.test(`${a.title} ${a.detail || ""}`) ? "listen" : "ask";
  const hear = audition ? "" : takes.length ? takes.slice(0, 2).map(abKeys).join("") + takes.slice(0, 1).map((t) => `<button class="key" data-desk="${esc(t)}" title="See this take on the desk">DESK</button>`).join("")
    : fromSeat ? `<span class="ab"><button class="key" data-ab="session" title="Render and play the session as it is now">▶ SESSION</button></span>` : "";
  const el = h(`<div class="ticket ${esc(a.kind)}">
    <div class="from"><span>${esc(fromSeat ? title(a.from) : "Floor")} · ${esc(a.kind)}${a.task ? " · " + esc(a.task) : ""}</span><span>${esc(a.id)}</span></div>
    <h3>${esc(a.title)}</h3>${a.detail ? `<p>${esc(a.detail)}</p>` : ""}
    ${hear ? `<div class="opts hear">${hear}</div>` : ""}
    <div class="opts">${audition ? `${abKeys(a.take)}<button class="key" data-desk="${esc(a.take)}" title="See and edit this take in the arrangement, piano roll and mixer">DESK</button><button class="key go" data-opt="a">ACCEPT</button><button class="key" data-ret>RETURN…</button><button class="key warn" data-discard title="Close this take without merging it">DISCARD</button>`
      : a.options.map((o, i) => `${o.take && !takes.includes(o.take) ? "" : ""}${o.audio ? `<button class="key" data-audio="${esc(o.audio)}" title="Hear it">▶</button>` : ""}<button class="key ${i === 0 && !fromSeat ? "go" : ""}" data-opt="${esc(o.id)}" data-do="${esc(o.do || "")}" title="${esc(o.detail || "")}">${esc(o.label)}</button>`).join("")
        + (!a.options.length && !fromSeat ? `<button class="key go" data-say="Sorted. Try again.">TRY AGAIN</button>` : "")
        + (!a.options.length && fromSeat ? REPLIES[kind].map(([label, say, more]) => `<button class="key" data-reply="${esc(say)}" ${more ? "data-more" : ""} title="${esc(say)}">${esc(label)}</button>`).join("") : "")}</div>
    <textarea rows="2" class="${audition ? "hidden" : ""}" placeholder="${audition ? "What has to change: bars, tracks, what you heard" : a.options.length ? "Add a note to go with your choice (optional)" : "Or answer in your own words"}"></textarea>
    <div class="opts ${audition ? "hidden" : ""}" data-send><button class="key" data-go>${audition ? "SEND IT BACK" : "SEND ANSWER"}</button></div>
    <div class="err hidden"></div></div>`);
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
  const drop = el.querySelector("[data-discard]");
  if (drop) drop.onclick = async () => {
    if (!confirm(`Discard ${a.take}? Its work stays on disk but is not merged.`)) return;
    err.classList.add("hidden");
    try { await api("take_discard", { take: a.take }); refresh(true); }
    catch (e) { err.textContent = e.message; err.classList.remove("hidden"); refresh(true); }
  };
  const ret = el.querySelector("[data-ret]");
  if (ret) ret.onclick = () => { text.classList.remove("hidden"); el.querySelector("[data-send]").classList.remove("hidden"); text.focus(); };
  el.querySelector("[data-go]").onclick = () => {
    if (!text.value.trim()) { err.textContent = audition ? "Say what has to change so the seat can act on it." : "Write an answer, or choose one above."; err.classList.remove("hidden"); return; }
    resolve(audition ? "b" : null);
  };
  wireAb(el);
  return el;
}
// The bay in the Studio room: one row per item, headed with what it needs from you. A row opens the Needs You
// sheet (needs.js) on it, where it is answered.
function renderNeeds() {
  const box = $("#needsList"), items = ST.board ? ST.board.needs_you.slice().sort((x, y) => x.created - y.created) : [];
  const sig = JSON.stringify(items.map((a) => [a.id, a.title, a.stage || a.kind]));
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  const N = window.TTNeeds;
  box.innerHTML = items.length ? items.map((a) => { const k = N ? N.kindOf(a) : "answer"; return `<button class="nyLine k-${k}" data-id="${esc(a.id)}" title="Open it on the Needs You sheet">
      <span class="nyVerb k-${k}">${N ? N.VERB[k] : "ANSWER"}</span><b>${esc(a.title)}</b>
      <span class="nyFrom">${esc(a.from === "you" ? (a.kind === "blocker" ? "Floor" : "Studio") : title(a.from))} · ${esc(a.id)}</span></button>`; }).join("")
    : `<div class="empty nyClear"><i class="lamp green"></i><span>ALL CLEAR</span><small>Nothing is waiting on you.</small></div>`;
  box.querySelectorAll(".nyLine").forEach((b) => b.onclick = () => N && N.open(b.dataset.id));
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
    // A seat whose open task cannot start now (needs you, or waits on an OK, another seat or a retry) gets one too.
    const running = s && ST.crew && ST.crew.active && ST.crew.active[s.seat], t = s && taskOf(s.task);
    const stuck = t && (t.state === "needs_you" || (t.state === "queued" && (t.held || t.waiting_for || t.not_before)));
    const agent = ST.wake && s && s.occupant && s.occupant.kind === "agent";
    if (agent && running) toast(`The ${s.title} is working: it reads your message on its next start.`, true);
    else if (agent && (!s.open_tasks || stuck)) {
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
    <div class="row"><button class="key go" id="rvAsk" title="File the scorecard and give the Producer the task of writing the review">ASK FOR A REVIEW</button><button class="key" id="rvPick" title="Propose a model per seat from how each did in earlier sessions; nothing changes until you apply it">PICK A CREW…</button><button class="ghost" id="rpClose">Close</button></div>`);
  $("#rpClose").onclick = closeModal;
  $("#rvPick").onclick = openCrewPick;
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
    $("#scBox").innerHTML = c.rows.length ? `<table class="sc"><tr><th>Seat</th><th>Agent</th><th title="Agent runs, and how many ended normally">Runs (ok)</th><th title="Runs that ended because the model had no usage left">Dry</th><th>Failed</th><th>Tools</th><th>Min</th><th title="Takes handed in / accepted / accepted without being sent back / sent back">Takes in · ok · 1st · back</th><th>Tasks</th><th title="Times this seat put something on Needs You">Asked</th><th title="What one accepted take cost (one finished task, for a seat that hands in no takes), from the runs that reported usage: dollars where the app reports them, otherwise tokens. A dash: no run reported usage.">Per take</th></tr>
      ${c.rows.map((r) => `<tr><td>${esc(title(r.seat))}</td><td class="mono">${esc(r.agent)}</td><td>${r.runs} (${r.ok})</td><td class="${r.out_of_usage ? "bad" : ""}">${r.out_of_usage}</td><td class="${r.failed ? "bad" : ""}">${r.failed}</td><td>${r.tool_calls}</td><td>${r.minutes}</td><td>${r.handed_in} · ${r.accepted} · ${r.first_time} · ${r.returned}</td><td>${r.tasks_done}</td><td>${r.asked_you}</td><td>${perTake(r)}</td></tr>`).join("")}</table>` : "No agent has worked in this session yet.";
  } catch (e) { if ($("#scBox")) $("#scBox").textContent = e.message; }
}
function perTake(r) {
  return r.usd_per_take !== undefined ? `$${r.usd_per_take.toFixed(2)}` : r.tokens_per_take !== undefined ? `${Math.round(r.tokens_per_take / 1000)}k tok` : "–";
}
// A model per seat from the crew history (crew_pick). Nothing changes until APPLY, which seats each ticked pick.
async function openCrewPick() {
  let picks = [];
  try { picks = (await api("crew_pick")).picks; } catch (e) { toast(e.message); return; }
  const box = modal(`<h2>Pick a crew from history</h2>
    <p class="hint">For each seat, the model that did best there in earlier sessions: most takes accepted and fewest sent back (finished tasks, for seats that hand in no takes). When two are level, the one that cost less per accepted take wins, where their runs reported usage; a model whose runs reported none is not marked down for it. Tick the seats you want and APPLY. Seats with no history are not listed.</p>
    ${picks.length ? `<table class="sc crewpick"><tr><th></th><th>Seat</th><th>Proposed</th><th>Why</th></tr>
      ${picks.map((p, i) => `<tr><td><input type="checkbox" data-i="${i}" ${p.same ? "disabled" : "checked"}></td><td>${esc(title(p.seat))}</td><td class="mono">${esc(p.agent)}${p.same ? ` <span class="tag ok">seated now</span>` : ""}</td><td>${esc(p.why)}</td></tr>`).join("")}</table>`
      : `<div class="empty">No history yet. The crew history fills in when a session's crew stops or you ask for a review.</div>`}
    <div class="row"><span class="spacer"></span><button class="ghost" id="cpCancel">Cancel</button><button class="key go" id="cpOk" ${picks.some((p) => !p.same) ? "" : "disabled"}>APPLY</button></div>`);
  $("#cpCancel").onclick = closeModal;
  $("#cpOk").onclick = async () => {
    const chosen = [...box.querySelectorAll("input[data-i]:checked")].map((el) => picks[+el.dataset.i]);
    if (!chosen.length) { toast("Tick the seats to change"); return; }
    closeModal();
    try {
      for (const p of chosen) await call("seat_assign", { seat: p.seat, kind: "agent", runner: p.runner, model: p.model, effort: p.effort || null });
      toast(`Seated ${chosen.map((p) => title(p.seat)).join(", ")} from history. Each change applies from that seat's next run.`, true);
    } catch { /* call() showed the error */ }
  };
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
      ST.project = project; ST.units = {}; $("#units").innerHTML = ""; $("#needsList").innerHTML = ""; delete $("#needsList").dataset.sig; AB.cache = {}; hush();
      window.TTLinks?.reset();
      const open = store.get("open", ""); ST.open = board && board.seats.some((s) => s.seat === open) ? open : null;
      connect();
    }
    render();
  },
  render, hush, listen, room: setRoom, board: () => ST.board,
};
// what the Needs You sheet (needs.js, loaded next) draws with
window.TTStudio = { ST, AB, h, title, seatOf, taskOf, focusedIn, abKeys, wireAb, hush, workRoll, workPicture, ticketEl,
  refresh, call };
setRoom(ROOMS.includes(store.get("room", "studio")) ? store.get("room", "studio") : "studio");
if (S.path || S.doc) loadState();  // app.js loaded the state before this file defined Studio
})();
