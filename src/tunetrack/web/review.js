// The review sheet: where a person in the Review seat writes their review while they listen, and sends it back.
// Listen with the sheet's PLAY, the top bar's transport or Space: while the Desk is on the session they are one
// transport (with a take open on the Desk, PLAY plays the session through the A/B player instead). The strip under
// it shows the form, where you are and every finding: click it to play from a bar, drag across bars to note a
// finding there, press M to note one at the bar you are hearing. A finding says where, what you heard and which
// seat fixes it ("Producer decides" leaves it to the Producer); OVERALL is the view as a whole; IT'S READY or NEEDS
// WORK is the verdict. SEND THE REVIEW runs review.review_send: the review filed under REPORTS, one task per seat,
// the Producer's share, the review task closed, and if asked a second listen once the fixes are in. On a second
// listen the earlier findings are listed to tick off. The draft is kept on the server as you type (review_draft).
// studio.js draws this as the Review seat's bench when a person sits there, passing what it needs in ctx.
"use strict";
const TTReview = (() => {
  const OPEN = ["queued", "working", "submitted", "needs_you"];
  const R = { key: null, pick: null, free: false, draft: null, earlier: null, loading: null, saved: 0, timer: 0,
              dirty: false, sent: null, closed: new Set(), open: null, cursor: 0, raf: 0, box: null, d: null, ctx: null, drag: null };
  let uid = 0;
  const fid = () => `f${++uid}${Date.now().toString(36)}`;
  const fresh = () => ({ verdict: null, findings: [], notes: "", again: null, checked: {} });
  const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const BARS = /^\s*\d+\s*(-\s*\d+\s*)?$/;

  // ---- time, from the bench's own form: it reads the session even while the Desk shows a take
  function pts(form) {
    const all = [[0, form.tempo || 120], ...(form.tempo_changes || [])].sort((a, b) => a[0] - b[0]), out = [];
    let sec = 0;
    all.forEach((p, i) => { if (i) sec += (p[0] - all[i - 1][0]) * 60 / all[i - 1][1]; out.push({ b: p[0], bpm: p[1], s: sec }); });
    return out;
  }
  const toSec = (form, b) => { let p = null; for (const q of pts(form)) if (!p || q.b <= b) p = q; return p.s + (b - p.b) * 60 / p.bpm; };
  const toBeat = (form, s) => { let p = null; for (const q of pts(form)) if (!p || q.s <= s) p = q; return p.b + (s - p.s) * p.bpm / 60; };
  const bpb = () => R.d.form.bpb || 4;
  const barOf = (b) => Math.floor(b / bpb() + 1e-6) + 1;
  const endB = () => Math.max(1, Math.ceil((R.d.form.end || bpb() * 8) / bpb() - 1e-6)) * bpb();
  const barsOf = (s) => { const m = String(s || "").match(/^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$/); if (!m) return null; const a = +m[1], b = +(m[2] || m[1]); return a >= 1 ? [Math.min(a, b), Math.max(a, b)] : null; };

  // ---- what you hear: the Desk's transport when it is on the session, else the A/B player
  const onDesk = () => !!S.doc && !S.take;
  const hearing = () => onDesk() ? isPlaying() : R.ctx.AB.key === "session" && !R.ctx.AB.el.paused;
  function beatNow() {
    if (onDesk()) return S.playhead || 0;
    const ab = R.ctx.AB;
    return ab.key === "session" ? toBeat(R.d.form, ab.el.currentTime) : R.cursor;
  }
  async function playFrom(beat) {
    R.cursor = Math.max(0, beat);
    if (onDesk()) { R.ctx.hush(); setPlayhead(R.cursor); if (!isPlaying()) await play(); return; }
    const ab = R.ctx.AB;
    if (ab.key !== "session") await R.ctx.audition(null);
    if (ab.key === "session") ab.el.currentTime = toSec(R.d.form, R.cursor);
  }
  function halt() { if (onDesk()) { if (isPlaying()) stop(false); } else R.ctx.hush(); }

  // ---- which review this sheet is for
  const tasks = () => R.ctx.board.tasks.filter((t) => t.seat === "review" && OPEN.includes(t.state) && !R.closed.has(t.id));
  function current() {
    if (R.free) return null;
    const open = tasks();
    if (R.pick) { const t = open.find((x) => x.id === R.pick); if (t) return t; R.pick = null; }
    return open.find((t) => !t.waiting_for && !t.not_before) || null;
  }
  const seatOf = (id) => R.ctx.board.seats.find((s) => s.seat === id);
  const filled = (id) => !!(seatOf(id) || {}).occupant;
  const defaultTo = () => filled("producer") ? "producer" : "";
  function seatsFor() {
    const out = filled("producer") ? [["producer", "Producer decides"]] : [];
    for (const s of R.ctx.board.seats) if (s.seat !== "producer" && s.seat !== "review" && s.owns.length && s.occupant) out.push([s.seat, s.title]);
    return out;
  }
  const trackList = () => R.d.tracks.map((t, i) => ({ t, col: trackColor(t, i) })).filter((x) => x.t.kind !== "bus").map((x) => ({ name: x.t.name, col: x.col }));
  function lineOf(f) {
    let where = f.bars ? `${/-/.test(f.bars) ? "Bars" : "Bar"} ${f.bars}` : "";
    if ((f.tracks || []).length) where += where ? ` (${f.tracks.join(", ")})` : f.tracks.join(", ");
    return where ? `${where}: ${f.text}` : f.text;
  }

  // ---- the draft: kept on the server as you type
  function normalise(dr) {
    const d = { ...fresh(), ...(dr || {}) };
    d.findings = (d.findings || []).map((f) => ({ text: "", bars: "", tracks: [], at: null, to: "", ...f, id: f.id || fid() }));
    d.checked = d.checked || {};
    return d;
  }
  async function load(key, t) {
    R.loading = key;
    try {
      const r = await api("review_draft", { task: t ? t.id : null });
      if (R.key !== key) return;
      R.draft = normalise(r.draft); R.earlier = r.earlier; R.saved = r.draft ? r.draft.saved : 0;
    } catch (e) { if (R.key !== key) return; R.draft = fresh(); toast(e.message); }
    R.loading = null; draw();
  }
  function save(now = false) {
    clearTimeout(R.timer);
    const key = R.key, draft = R.draft; if (!key || !draft) return;
    const go = async () => {
      try { const r = await api("review_draft", { task: key === "free" ? null : key, draft }); if (R.key === key) { R.saved = r.saved; R.dirty = false; savedText(); } }
      catch (e) { if (R.key === key) savedText("Not saved: " + e.message); }
    };
    if (now) go(); else R.timer = setTimeout(go, 600);
  }
  function savedText(err) {
    const el = R.box && R.box.querySelector(".rvSaved"); if (!el) return;
    el.textContent = err || (R.dirty ? "Saving…" : R.saved ? `Draft kept ${new Date(R.saved * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}` : "");
    el.classList.toggle("bad", !!err);
  }
  function changed() { R.dirty = true; savedText(); save(); route(); }

  // ---- drawing
  function render(box, d, ctx) {
    R.box = box; R.d = d; R.ctx = ctx;
    for (const id of [...R.closed]) if (!ctx.board.tasks.some((t) => t.id === id && OPEN.includes(t.state))) R.closed.delete(id);
    const t = current(), key = t ? t.id : R.free ? "free" : null;
    if (key !== R.key) {
      if (R.dirty) save(true);  // the sheet moves to another review: keep what was written for this one
      R.key = key; R.draft = null; R.earlier = null; R.open = null; R.dirty = false;
      if (key) load(key, t);
    }
    draw();
  }
  function draw() {
    const box = R.box; if (!box || !R.d) return;
    const t = current();
    if (!R.key) { box.innerHTML = `<div class="rv">${sentHtml()}${idleHtml()}</div>`; wireIdle(box); return; }
    if (!R.draft) { box.innerHTML = `<div class="rv">${sentHtml()}<div class="empty">Opening the review sheet…</div></div>`; wireIdle(box); return; }
    box.innerHTML = `<div class="rv">${sentHtml()}${askHtml(t)}${deckHtml()}
      <div class="rvGrid"><div class="rvSheet">${earlierHtml()}${findingsHtml()}${overallHtml()}${sendHtml()}</div><aside class="rvSide">${measuredHtml()}</aside></div></div>`;
    wire(box, t); wireIdle(box);
    box.querySelectorAll("textarea").forEach(grow);
    drawTl(); route(); savedText(); loop();
  }
  const grow = (el) => { el.style.height = "auto"; el.style.height = Math.min(320, el.scrollHeight + 2) + "px"; };

  function sentHtml() {
    const s = R.sent; if (!s) return "";
    const parts = s.tasks.map((t) => `<b>${esc(R.ctx.title(t.seat))}</b> ${t.fixes === 1 ? "a fix" : `${t.fixes} fixes`} <span class="id">${esc(t.id)}</span>${t.after.length ? " after the others" : ""}`);
    if (s.producer) parts.push(`<b>Producer</b> ${s.findings && s.tasks.reduce((a, t) => a + t.fixes, 0) < s.findings ? "hands out the rest" : "acts on your note"} <span class="id">${esc(s.producer)}</span>`);
    const run = R.ctx.crewRunning();
    return `<div class="rvSent"><div class="row"><i class="rvDot"></i><span class="legend">SENT · ${esc(new Date(s.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }))}</span>
        <b>${s.verdict === "ready" ? "Ready" : "Needs work"}</b><span class="meta">${s.findings} finding${s.findings === 1 ? "" : "s"}${s.closed ? ` · ${esc(s.closed)} done` : ""}</span><span class="spacer"></span>
        <button class="key" data-report="${esc(s.report)}" title="Read the review as filed">READ ${esc(s.report)}</button><button class="mini" data-k="unsent" title="Hide this">×</button></div>
      <p>${parts.length ? parts.join(" · ") + "." : "Nothing more for the crew to do."}${s.again ? ` You hear it again once they're in (<span class="id">${esc(s.again)}</span>).` : ""}${parts.length && !run ? " The crew starts on it when it runs (RUN)." : ""}</p>
      ${s.final ? `<p class="rvFinal">${finalHtml()}</p>` : ""}</div>`;
  }
  // a ready verdict renders the session as the finished cue (review.finalize): its state, until the report is filed
  function finalHtml() {
    const f = R.final;
    if (!f || f.state === "rendering") return `<span class="meta">Rendering the final mix…</span>`;
    if (f.state === "failed") return `<span class="bad">The final mix did not render: ${esc(f.error || "")}</span>`;
    return `<b>Final mix rendered</b> <span class="meta">${esc((f.file || "").split(/[\\/]/).pop())}</span> <button class="key" data-report="${esc(f.report)}" title="Its numbers, what is still open, and what made it">READ ${esc(f.report)}</button>`;
  }
  function pollFinal() {
    clearTimeout(R.finalTimer);
    api("final_status", {}).then((f) => {
      R.final = f;
      const el = R.box && R.box.querySelector(".rvFinal");
      if (el) el.innerHTML = finalHtml();  // its READ opens through studio.js's [data-report] click
      if (!f || f.state === "rendering") R.finalTimer = setTimeout(pollFinal, 3000);
    }).catch(() => { R.finalTimer = setTimeout(pollFinal, 6000); });
  }
  // IT'S READY: the session measured now (review.review_check), so a fix that never reached it shows before you pass it
  function runCheck(t) {
    const key = R.key; R.check = { key, loading: true }; route();
    api("review_check", { task: t ? t.id : null })
      .then((r) => { if (R.key === key) { R.check = { key, ...r }; route(); } })
      .catch((e) => { if (R.key === key) { R.check = { key, error: e.message }; route(); } });
  }
  function checkHtml() {
    const c = R.check; if (!c || c.key !== R.key) return "";
    if (c.loading) return `<span class="rvChecks meta">Measuring the session…</span>`;
    if (c.error) return `<span class="rvChecks bad">Could not measure it: ${esc(c.error)}</span>`;
    const head = `<span class="legend">MEASURED NOW · REV ${esc(c.rev)}</span>`;
    return c.checks.length ? `<span class="rvChecks">${head}<ul>${c.checks.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></span>`
      : `<span class="rvChecks">${head} <span class="ok">No silent bars, and the ending holds.</span></span>`;
  }
  function idleHtml() {
    const next = tasks()[0], name = (id) => { const x = R.ctx.board.tasks.find((y) => y.id === id); return x ? `${id} (${R.ctx.title(x.seat)})` : id; };
    if (next) {
      const why = next.waiting_for ? `It starts when ${next.waiting_for.map(name).join(", ")} ${next.waiting_for.length > 1 ? "are" : "is"} in` : "It starts later";
      return `<div class="rvIdle"><div><span class="legend">NEXT FOR YOU</span><h4><span class="id">${esc(next.id)}</span>${esc(next.title)}</h4><p class="meta">${esc(why)}: the seat lights up when it's your move.</p></div>
        <div class="row"><button class="key" data-k="start" data-t="${esc(next.id)}" title="Listen and write it now, before what it waits on is in">START IT NOW</button><button class="key" data-k="free">WRITE ANOTHER REVIEW</button></div></div>`;
    }
    return `<div class="rvIdle"><div><span class="legend">NOTHING TO REVIEW</span><p class="meta">Nobody has asked for a review right now. You can still write one: its findings go to the seats you name.</p></div>
      <div class="row"><button class="key go" data-k="free">WRITE A REVIEW</button></div></div>`;
  }
  function askHtml(t) {
    const br = R.d.form.brief || {}, b = R.ctx.board;
    const brief = ["genre", "mood", "use", "references"].filter((k) => br[k] && String(br[k]).trim()).map((k) => `<span><i>${k}</i>${esc(Array.isArray(br[k]) ? br[k].join(", ") : br[k])}</span>`).join("");
    const name = (id) => { const x = b.tasks.find((y) => y.id === id); return x ? `${id} (${R.ctx.title(x.seat)})` : id; };
    const waits = t && t.waiting_for ? `<p class="rvWarn">${esc(t.id)} still waits on ${esc(t.waiting_for.map(name).join(", "))}: what you hear may still change.</p>` : "";
    const pending = b.takes.filter((k) => k.state === "submitted");
    const pend = pending.length ? `<p class="rvWarn">${esc(pending.map((k) => `${k.id.toUpperCase()} (${R.ctx.title(k.seat)})`).join(", "))} ${pending.length > 1 ? "are" : "is"} handed in but not accepted yet: you are hearing the session without ${pending.length > 1 ? "them" : "it"}.</p>` : "";
    const others = tasks().filter((x) => !t || x.id !== t.id);
    const ask = t && t.recheck ? `Your review ${t.recheck} sent fixes out, and they are in. Tick off what's fixed below, note anything still off or new, and say whether it's ready.` : t ? t.detail : "";
    return `<div class="rvAsk">
      <div class="row"><span class="legend">${t ? (t.recheck ? "SECOND LISTEN" : "REVIEWING") : "YOUR OWN REVIEW"}</span>${t ? `<span class="id">${esc(t.id)}</span><b>${esc(t.title)}</b>${t.by && t.by !== "you" ? `<span class="tag">from the ${esc(R.ctx.title(t.by))}</span>` : ""}` : `<b>Not for a task: the findings go to the seats you name.</b>`}
        <span class="spacer"></span>${others.map((x) => `<button class="mini" data-k="start" data-t="${esc(x.id)}" title="${esc(x.title)}">${esc(x.id)}</button>`).join("")}${R.free ? `<button class="mini" data-k="unfree" title="Put this sheet away (the draft is kept)">PUT AWAY</button>` : ""}</div>
      ${ask ? `<p>${esc(ask)}</p>` : ""}${brief ? `<div class="rvBrief">${brief}</div>` : ""}${waits}${pend}</div>`;
  }
  function deckHtml() {
    return `<div class="rvDeck">
      <div class="row"><button class="key rvPlay" data-k="play" title="Play the session from where you are (Space does too)">▶ PLAY</button>
        <span class="vfd small rvPos">BAR 1 · 0:00</span><span class="rvSec"></span>
        <button class="key" data-k="mark" title="Note a finding at the bar you are hearing">+ FINDING HERE <kbd>M</kbd></button>
        <span class="spacer"></span><span class="rvVia"></span></div>
      <div class="rvTl"></div>
      <div class="rvHint">Click the strip to play from a bar · drag across bars to note a finding there · <kbd>M</kbd> notes one where you are · <kbd>Space</kbd> plays and stops · <kbd>Esc</kbd> leaves a text box</div></div>`;
  }
  function earlierHtml() {
    const e = R.earlier; if (!e || !(e.findings || []).length) return "";
    return `<div class="rvEarlier"><div class="rvH"><span class="legend">LAST TIME · ${esc(e.report)}</span><span class="meta">Tick off what's fixed. STILL OFF copies it into this review.</span></div>
      ${e.findings.map((f) => { const st = R.draft.checked[f.n] || ""; return `<div class="rvE ${st}" data-n="${f.n}"><b class="rvN">${f.n}</b><span class="tx">${esc(lineOf(f))}</span><span class="meta">${esc(R.ctx.title(f.to))}</span>
        <span class="seg"><button data-e="fixed" class="${st === "fixed" ? "on" : ""}">FIXED</button><button data-e="still" class="${st === "still" ? "on" : ""}">STILL OFF</button></span></div>`; }).join("")}</div>`;
  }
  function findingsHtml() {
    const rows = R.draft.findings, opts = seatsFor();
    return `<div class="rvH"><span class="legend">FINDINGS</span><span class="meta">${rows.length ? "" : "None yet. Press M while it plays, drag across the strip, or add one."}</span></div>
      <div class="rvList">${rows.map((f, i) => rowHtml(f, i, opts)).join("")}</div>
      <div class="row"><button class="key" data-k="add" title="A finding with no bars yet">+ FINDING</button></div>`;
  }
  function rowHtml(f, i, opts) {
    const names = f.tracks || [];
    const to = `<select data-f="to" title="Who fixes it">${f.to ? "" : `<option value="" selected>Who fixes it?</option>`}${opts.map(([v, l]) => `<option value="${v}" ${f.to === v ? "selected" : ""}>${esc(l)}</option>`).join("")}${f.to && !opts.some(([v]) => v === f.to) ? `<option value="${esc(f.to)}" selected>${esc(R.ctx.title(f.to))} (nobody seated)</option>` : ""}</select>`;
    const bad = f.bars && !BARS.test(f.bars);
    return `<div class="rvF ${f.text.trim() ? "" : "blank"}" data-id="${esc(f.id)}">
      <div class="rvFh"><b class="rvN">${i + 1}</b>
        <label class="rvBars ${bad ? "bad" : ""}" title="A bar (17) or a range (17-24); empty for the whole cue"><span>BARS</span><input data-f="bars" value="${esc(f.bars || "")}" placeholder="all" spellcheck="false" autocomplete="off"></label>
        ${f.at != null ? `<button class="mini" data-k="at" title="Play from where you noted it">▶ ${clock(f.at)}</button>` : ""}
        <button class="mini rvTrk ${names.length ? "on" : ""}" data-k="tracks" title="${names.length ? esc(names.join(", ")) : "Which tracks it is about"}">${names.length ? esc(names.join(", ")) : "TRACKS"} ▾</button>
        ${f.from ? `<span class="tag" title="Still off from the last review">${esc(R.earlier ? R.earlier.report : "")} #${esc(f.from)}</span>` : ""}
        <span class="spacer"></span><label class="rvFor"><span>FOR</span>${to}</label>
        <button class="mini" data-k="del" title="Remove this finding">×</button></div>
      <div class="chips rvTracks ${R.open === f.id ? "" : "hidden"}">${trackList().map((t) => `<button class="chip ${names.includes(t.name) ? "" : "off"}" data-tr="${esc(t.name)}"><i style="background:${t.col}"></i>${esc(t.name)}</button>`).join("") || `<span class="meta">No tracks.</span>`}</div>
      <textarea data-f="text" rows="2" placeholder="What you hear, and what would fix it">${esc(f.text)}</textarea></div>`;
  }
  const overallHtml = () => `<div class="rvH"><span class="legend">OVERALL</span></div>
    <textarea data-k="notes" rows="3" placeholder="What works, what doesn't, and whether it does what the brief asks">${esc(R.draft.notes)}</textarea>`;
  const againOn = () => R.draft.again ?? (R.draft.verdict !== "ready");
  function sendHtml() {
    const v = R.draft.verdict;
    return `<div class="rvSend">
      <div class="row"><span class="legend">VERDICT</span><div class="seg rvVerdict"><button data-v="ready" class="${v === "ready" ? "on" : ""}" title="It does what the brief asks; any findings are polish">IT'S READY</button><button data-v="fix" class="${v === "fix" ? "on" : ""}" title="It isn't finished until the findings are dealt with">NEEDS WORK</button></div>
        <button class="key toggle" data-k="again" title="A second listen for you, which starts once every fix is in">HEAR IT AGAIN AFTER THE FIXES</button></div>
      <p class="rvRoute"></p>
      <div class="row end"><span class="meta rvSaved"></span><button class="key go" data-k="send">SEND THE REVIEW</button></div></div>`;
  }
  function measuredHtml() {
    const r = R.d.render;
    if (!r) return `<div class="legend">MEASURED</div><p class="meta">Nothing rendered yet: PLAY renders it, and the numbers land here.</p>`;
    const lv = R.d.tracks.map((t, i) => ({ t, col: trackColor(t, i) })).filter((x) => x.t.level && x.t.kind !== "bus");
    const num = (v) => v == null ? "–" : esc(v);
    return `<div class="legend">MEASURED · LAST RENDER</div>
      <div class="rvRead"><span>${num(r.lufs)}<i>LUFS</i></span><span>${num(r.true_peak_dbtp)}<i>dBTP</i></span><span>${num(r.stereo_correlation)}<i>WIDTH</i></span></div>
      <span class="tag ${r.stale ? "stale" : "ok"}">${r.stale ? "OLDER THAN THE MUSIC" : "CURRENT"}</span>
      ${lv.length ? `<div class="rvLv">${lv.map(({ t, col }) => `<div title="${esc(t.name)}: ${t.level.rms_dbfs} dBFS rms, ${t.level.peak_dbfs} peak"><span>${esc(t.name)}</span><b><i style="width:${Math.max(0, Math.min(100, (t.level.rms_dbfs + 54) / 54 * 100))}%;background:${col}"></i></b><em>${num(t.level.rms_dbfs)}</em></div>`).join("")}</div>` : ""}
      ${r.warnings.length ? `<ul class="rvWarnL">${r.warnings.slice(0, 8).map((w) => `<li>${esc(w)}</li>`).join("")}${r.warnings.length > 8 ? `<li>and ${r.warnings.length - 8} more</li>` : ""}</ul>` : `<p class="meta">No warnings from the last render.</p>`}`;
  }

  // ---- the strip: sections, bars, the loop, the findings, the playhead
  function drawTl() {
    const el = R.box && R.box.querySelector(".rvTl"); if (!el) return;
    const form = R.d.form, end = endB(), n = Math.round(end / bpb()), w = el.clientWidth || 640;
    const pc = (b) => (Math.max(0, Math.min(end, b)) / end * 100).toFixed(3) + "%";
    const span = (a, b) => `left:${pc(a)};width:${((Math.min(end, b) - Math.max(0, a)) / end * 100).toFixed(3)}%`;
    const marks = (form.markers || []).slice().sort((a, b) => a.at - b.at).filter((m) => m.at < end);
    const secs = marks.map((m, i) => `<i class="sec" style="${span(m.at, marks[i + 1] ? marks[i + 1].at : end)}" title="${esc(m.name)} · bar ${barOf(m.at)}">${esc(m.name)}</i>`).join("");
    let step = 64; for (const s of [1, 2, 4, 8, 16, 32, 64]) if (w / (n / s) >= 30) { step = s; break; }
    let ticks = ""; for (let b = 1; b <= n; b += step) ticks += `<span style="left:${pc((b - 1) * bpb())}">${b}</span>`;
    const lp = form.loop && form.loop.end > form.loop.start ? `<i class="loop" style="${span(form.loop.start, form.loop.end)}" title="The loop"></i>` : "";
    const flags = R.draft.findings.map((f, i) => { const r = barsOf(f.bars); return r ? `<i class="fl ${f.text.trim() ? "" : "blank"}" data-id="${esc(f.id)}" style="${span((r[0] - 1) * bpb(), r[1] * bpb())}" title="${esc(i + 1 + ". " + (f.text.trim() || "(not written yet)"))}">${i + 1}</i>` : ""; }).join("");
    el.innerHTML = `<div class="secs">${secs || `<i class="sec none" style="left:0;width:100%">no sections marked</i>`}</div>
      <div class="lane" style="background-image:repeating-linear-gradient(90deg, var(--line2) 0 1px, transparent 1px ${(100 / n).toFixed(4)}%)">${lp}${flags}</div>
      <div class="nums">${ticks}</div><i class="sel hidden"></i><i class="ph"></i>`;
    const at = (x) => { const r = el.getBoundingClientRect(); return Math.max(0, Math.min(1, (x - r.left) / r.width)) * end; };
    const sel = el.querySelector(".sel");
    const range = (b0, b1) => { const lo = Math.min(b0, b1), hi = Math.max(b0, b1); const a = barOf(lo); return [a, Math.max(a, barOf(Math.max(lo, hi - 1e-3)))]; };
    el.onpointerdown = (e) => {
      if (e.button !== 0) return;
      const fl = e.target.closest(".fl");
      if (fl) { const f = R.draft.findings.find((x) => x.id === fl.dataset.id); focusRow(fl.dataset.id); const r = f && barsOf(f.bars); if (r) playFrom((r[0] - 1) * bpb()); return; }
      R.drag = { x0: e.clientX, b0: at(e.clientX), moved: false };
      try { el.setPointerCapture(e.pointerId); } catch { /* a pointer the browser no longer tracks: the drag still works inside the strip */ }
    };
    el.onpointermove = (e) => {
      const dr = R.drag; if (!dr) return;
      if (Math.abs(e.clientX - dr.x0) > 4) dr.moved = true;
      if (!dr.moved) return;
      const [a, b] = range(dr.b0, at(e.clientX));
      sel.setAttribute("style", span((a - 1) * bpb(), b * bpb())); sel.textContent = a === b ? `BAR ${a}` : `BARS ${a}-${b}`; sel.classList.remove("hidden");
    };
    el.onpointerup = (e) => {
      const dr = R.drag; R.drag = null; sel.classList.add("hidden"); if (!dr) return;
      if (!dr.moved) { playFrom((barOf(dr.b0) - 1) * bpb()); return; }
      const [a, b] = range(dr.b0, at(e.clientX));
      addFinding({ bars: a === b ? `${a}` : `${a}-${b}`, at: +toSec(form, (a - 1) * bpb()).toFixed(2) });
    };
    el.onpointercancel = () => { R.drag = null; sel.classList.add("hidden"); };
  }
  function loop() {
    cancelAnimationFrame(R.raf);
    const step = () => {
      const box = R.box;
      if (!box || !box.isConnected || !R.draft) return;
      if (box.offsetParent) frame(box);
      R.raf = requestAnimationFrame(step);
    };
    R.raf = requestAnimationFrame(step);
  }
  function frame(box) {
    const form = R.d.form, beat = beatNow(), end = endB(), on = hearing();
    const ph = box.querySelector(".rvTl .ph"); if (ph) ph.style.left = (Math.min(beat, end) / end * 100).toFixed(3) + "%";
    const set = (sel, text) => { const el = box.querySelector(sel); if (el && el.textContent !== text) el.textContent = text; };
    set(".rvPos", `BAR ${barOf(beat)} · ${clock(toSec(form, beat))}`);
    const m = (form.markers || []).filter((x) => x.at <= beat + 1e-6).sort((a, b) => b.at - a.at)[0];
    set(".rvSec", m ? m.name : "");
    const pb = box.querySelector(".rvPlay");
    if (pb) { set(".rvPlay", on ? "■ STOP" : "▶ PLAY"); pb.classList.toggle("on", on); }
    set(".rvVia", S.take ? `THE DESK HAS ${S.take.toUpperCase()} OPEN · PLAY HERE IS THE SESSION` : "");
  }

  // ---- editing
  function addFinding(base) {
    const f = { id: fid(), text: "", bars: "", tracks: [], at: null, to: defaultTo(), ...base };
    R.draft.findings.push(f); changed(); draw(); focusRow(f.id);
  }
  function mark() { const beat = beatNow(); addFinding({ bars: String(barOf(beat)), at: +toSec(R.d.form, beat).toFixed(2) }); }
  function focusRow(id) {
    const el = R.box && R.box.querySelector(`.rvF[data-id="${CSS.escape(id)}"] textarea`);
    if (el) { el.focus({ preventScroll: true }); el.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
  }
  function route() {
    const box = R.box, el = box && box.querySelector(".rvRoute"); if (!el || !R.draft) return;
    const p = plan(), send = box.querySelector('[data-k="send"]');
    el.innerHTML = (p.problems.length ? `<span class="bad">${esc(p.problems[0])}</span>` : p.say)
      + (R.draft.verdict === "ready" ? checkHtml() : "");
    send.disabled = !!p.problems.length; send.title = p.problems.join(" ") || "File the review and hand its findings out";
    const ag = box.querySelector('[data-k="again"]');
    ag.classList.toggle("on", againOn()); ag.classList.toggle("hidden", !p.fixes);
    box.querySelectorAll(".rvVerdict [data-v]").forEach((b) => b.classList.toggle("on", R.draft.verdict === b.dataset.v));
  }
  // what sending will do, in words, and what stops it
  function plan() {
    const d = R.draft, rows = d.findings.filter((f) => f.text.trim()), problems = [], notes = d.notes.trim();
    d.findings.forEach((f, i) => {
      if (!f.text.trim() && ((f.bars || "").trim() || (f.tracks || []).length)) problems.push(`Write what you hear in finding ${i + 1}, or remove it.`);
      if (f.bars && !BARS.test(f.bars)) problems.push(`Finding ${i + 1}: write the bars as 17 or 17-24.`);
      if (f.text.trim() && !f.to) problems.push(`Finding ${i + 1}: say who fixes it.`);
      else if (f.text.trim() && !filled(f.to)) problems.push(`Finding ${i + 1} is for the ${R.ctx.title(f.to)}, and nobody is in that seat.`);
    });
    if (!d.verdict) problems.push("Choose IT'S READY or NEEDS WORK.");
    if (d.verdict === "fix" && !rows.length && !notes) problems.push("Say what needs work: a finding, or an overall note for the Producer.");
    if (d.verdict === "fix" && !rows.length && notes && !filled("producer")) problems.push("Nobody is in the Producer seat to act on a note: write what needs work as findings.");
    const groups = {}; rows.forEach((f) => { if (f.to) (groups[f.to] = groups[f.to] || []).push(f); });
    const seats = R.ctx.board.seats.map((s) => s.seat).filter((s) => groups[s] && s !== "producer");
    const parts = seats.map((s) => `<b>${esc(R.ctx.title(s))}</b> gets ${groups[s].length === 1 ? "a fix" : `${groups[s].length} fixes`}${s === "mix" && (seats.length > 1 || groups.producer) ? ", after the others" : ""}`);
    const toProd = (groups.producer || []).length;
    if (toProd) parts.push(`the <b>Producer</b> hands out ${toProd === 1 ? "one" : toProd}`);
    else if (d.verdict === "fix" && notes && !seats.length) parts.push("the <b>Producer</b> acts on your note");
    const t = current(), again = againOn() && parts.length;
    const say = (parts.length ? parts.join(" · ") + "." : "Nothing for the crew to do.") + (again ? " You hear it again once they're in." : "")
      + ` Filed under REPORTS${t ? `; ${t.id} is done` : ""}.`
      + (d.verdict === "ready" && !parts.length ? " Then the final mix renders as the finished cue." : "");
    return { problems, say, fixes: parts.length };
  }
  function wire(box, t) {
    const q = (s) => box.querySelector(s), d = R.draft;
    q('[data-k="play"]').onclick = () => hearing() ? halt() : playFrom(beatNow());
    q('[data-k="mark"]').onclick = mark;
    q('[data-k="add"]').onclick = () => addFinding({});
    box.querySelectorAll(".rvF").forEach((el) => {
      const f = d.findings.find((x) => x.id === el.dataset.id); if (!f) return;
      const k = (s) => el.querySelector(s);
      k('[data-f="text"]').oninput = (e) => { f.text = e.target.value; grow(e.target); el.classList.toggle("blank", !f.text.trim()); changed(); };
      k('[data-f="bars"]').oninput = (e) => { f.bars = e.target.value; k(".rvBars").classList.toggle("bad", !!f.bars && !BARS.test(f.bars)); changed(); drawTl(); };
      k('[data-f="to"]').onchange = (e) => { f.to = e.target.value; changed(); };
      k('[data-k="del"]').onclick = () => {
        d.findings.splice(d.findings.indexOf(f), 1);
        if (f.from && d.checked[f.from] === "still") delete d.checked[f.from];
        if (R.open === f.id) R.open = null;
        changed(); draw();
      };
      k('[data-k="tracks"]').onclick = () => { R.open = R.open === f.id ? null : f.id; draw(); };
      const atK = k('[data-k="at"]'); if (atK) atK.onclick = () => playFrom(toBeat(R.d.form, f.at));
      el.querySelectorAll("[data-tr]").forEach((b) => b.onclick = () => {
        const n = b.dataset.tr; f.tracks = f.tracks.includes(n) ? f.tracks.filter((x) => x !== n) : [...f.tracks, n]; changed(); draw();
      });
    });
    box.querySelectorAll(".rvE [data-e]").forEach((b) => b.onclick = () => {
      const n = b.closest(".rvE").dataset.n, v = b.dataset.e, old = (R.earlier.findings || []).find((x) => String(x.n) === n);
      if (d.checked[n] === v) delete d.checked[n]; else d.checked[n] = v;
      const copy = d.findings.find((x) => String(x.from) === n);
      if (d.checked[n] === "still" && !copy && old) d.findings.push({ id: fid(), text: old.text, bars: old.bars, tracks: [...(old.tracks || [])], at: old.at, to: filled(old.to) ? old.to : defaultTo(), from: old.n });
      if (d.checked[n] !== "still" && copy && old && copy.text === old.text) d.findings.splice(d.findings.indexOf(copy), 1);
      changed(); draw();
    });
    q('[data-k="notes"]').oninput = (e) => { d.notes = e.target.value; grow(e.target); changed(); };
    box.querySelectorAll(".rvVerdict [data-v]").forEach((b) => b.onclick = () => {
      d.verdict = b.dataset.v; changed();
      if (d.verdict === "ready" && !(R.check && R.check.key === R.key && !R.check.error)) runCheck(t);
    });
    if (d.verdict === "ready" && !(R.check && R.check.key === R.key)) runCheck(t);  // a draft opened on READY
    q('[data-k="again"]').onclick = () => { d.again = !againOn(); changed(); };
    q('[data-k="send"]').onclick = () => send(t);
  }
  function wireIdle(box) {
    box.querySelectorAll('[data-k="start"]').forEach((b) => b.onclick = () => { R.pick = b.dataset.t; R.free = false; render(R.box, R.d, R.ctx); });
    box.querySelectorAll('[data-k="free"]').forEach((b) => b.onclick = () => { R.free = true; R.pick = null; render(R.box, R.d, R.ctx); });
    const un = box.querySelector('[data-k="unfree"]'); if (un) un.onclick = () => { R.free = false; render(R.box, R.d, R.ctx); };
    const gone = box.querySelector('[data-k="unsent"]'); if (gone) gone.onclick = () => { R.sent = null; draw(); };
  }
  async function send(t) {
    const p = plan(); if (p.problems.length) { toast(p.problems[0]); return; }
    clearTimeout(R.timer);
    const d = R.draft, btn = R.box.querySelector('[data-k="send"]'); btn.disabled = true; btn.textContent = "SENDING…";
    const findings = d.findings.filter((f) => f.text.trim()).map(({ text, bars, tracks, at, to }) => ({ text, bars, tracks, at, to }));
    try {
      const r = await api("review_send", { verdict: d.verdict, findings, notes: d.notes, task: t ? t.id : null, again: againOn(), checked: d.checked });
      if (t) R.closed.add(t.id);
      R.sent = { ...r, at: Date.now() / 1000 }; R.dirty = false; R.free = false; R.pick = null; R.key = null; R.draft = null; R.check = null;
      if (r.final) { R.final = { state: "rendering" }; setTimeout(pollFinal, 1500); }
      toast(`Review sent: ${r.report} is under REPORTS.`, true);
      R.ctx.refresh();
      render(R.box, R.d, R.ctx);
    } catch (e) { toast(e.message); btn.disabled = false; btn.textContent = "SEND THE REVIEW"; }
  }

  // M notes a finding at the bar you are hearing; Esc leaves a text box so Space and M work again
  document.addEventListener("keydown", (e) => {
    const box = R.box;
    if (!box || !box.isConnected || !box.offsetParent || !R.draft || document.body.dataset.room !== "studio") return;
    const el = document.activeElement, typing = ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
    if (typing && e.key === "Escape" && box.contains(el)) { el.blur(); return; }
    if (typing || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
    if (e.key === "m" || e.key === "M") { e.preventDefault(); mark(); }
  });

  return { render, pick(id) { R.pick = id; R.free = false; } };
})();
