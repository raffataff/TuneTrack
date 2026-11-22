// Needs You: one sheet for everything that waits on you. The queue down the left, each item headed with what it
// needs from you (LISTEN & SIGN OFF, CONFIRM, APPROVE PLAN, CHOOSE, ANSWER, UNBLOCK). On the right, a workspace
// made for that item: a take's work as its seat's bench draws it (the instruments for Sound, the roll for the note
// seats, the console for Mix) with A/B against the song and room to change it yourself; the Producer's verdict;
// a plan to edit and approve; options side by side. The answers sit along the bottom.
// Also the INVOLVE key in the top bar: how much of the work stops at you, and away mode.
// Loaded after studio.js and uses its globals (ST, api, esc, h, title, abKeys, wireAb, hush, ...).
"use strict";
const TTNeeds = (() => {
  const { ST, AB, title, seatOf, taskOf, focusedIn, abKeys, wireAb, hush, workRoll, workPicture, ticketEl, refresh, call } = window.TTStudio;
  const N = { el: null, cur: null, sig: "", B: null, hear: new Audio(), diffs: {} };
  const VERB = { sign: "LISTEN & SIGN OFF", decide: "LISTEN & DECIDE", confirm: "CONFIRM", plan: "APPROVE PLAN",
    choose: "CHOOSE", unblock: "UNBLOCK", provide: "PROVIDE", answer: "ANSWER" };
  const kindOf = (a) => a.stage === "signoff" ? (a.options.some((o) => o.do === "pass") ? "sign" : "decide")
    : a.stage ? ({ confirm: "confirm", plan: "plan", choose: "choose" })[a.stage] || "answer"
    : a.take ? "decide" : a.kind === "blocker" ? "unblock" : a.kind === "input" ? "provide" : "answer";
  const items = () => (ST.board ? ST.board.needs_you : []).slice().sort((x, y) => x.created - y.created);
  const from = (a) => a.from === "you" ? (a.kind === "blocker" ? "Floor" : "Studio") : title(a.from);
  const takeOf = (id) => ST.board && ST.board.takes.find((t) => t.id === id);
  const letter = (i) => String.fromCharCode(65 + i);

  // ---- the sheet -------------------------------------------------------------------------------------------------
  function open(id) {
    if (!ST.board) { toast("Open a project first"); return; }
    const all = items();
    N.cur = id && all.some((a) => a.id === id) ? id : all.some((a) => a.id === N.cur) ? N.cur : (all[0] || {}).id || null;
    const el = Sheet.open({ title: "Needs you", sub: count(all.length), size: "full",
      body: `<div class="ny"><nav class="nyQ" aria-label="Waiting on you"></nav><section class="nyW"></section></div>` });
    el._stop = () => { hush(); N.hear.pause(); N.el = null; };
    N.el = el; N.sig = "";
    drawQueue(); drawItem();
  }
  const count = (n) => n ? `${n} WAITING ON YOU` : "NOTHING WAITING";
  // the board moved on: the queue follows, and the workspace too unless you are in the middle of something there
  function update() {
    if (!N.el) return;
    if (!document.contains(N.el)) { N.el = null; return; }
    const all = items();
    N.el.querySelector(".shSub").textContent = count(all.length);
    if (!all.some((a) => a.id === N.cur)) { N.cur = (all[0] || {}).id || null; N.sig = ""; }
    drawQueue();
    const w = N.el.querySelector(".nyW");
    if (N.sig !== sigOf(all.find((a) => a.id === N.cur)) && !focusedIn(w) && !(N.B && N.B.hold)) drawItem();
  }
  const sigOf = (a) => a ? JSON.stringify([a.id, a.title, a.detail, a.options, a.tasks, a.takes,
    (a.tasks || []).map((t) => { const x = taskOf(t); return x && [x.state, x.title, x.detail]; })]) : "none";
  function drawQueue() {
    const q = N.el.querySelector(".nyQ"), all = items();
    q.innerHTML = all.length ? all.map((a) => `<button class="nyRow ${a.id === N.cur ? "on" : ""}" data-id="${esc(a.id)}">
        <span class="nyVerb k-${kindOf(a)}">${VERB[kindOf(a)]}</span><b>${esc(a.title)}</b>
        <span class="nyFrom">${esc(from(a))} · ${esc(a.id)} · ${esc(ago(a.created))}</span></button>`).join("")
      : `<div class="nyQEmpty">Nothing waiting.</div>`;
    q.querySelectorAll(".nyRow").forEach((b) => b.onclick = () => { if (N.cur !== b.dataset.id) { hush(); N.cur = b.dataset.id; drawQueue(); drawItem(); } });
  }
  function drawItem() {
    const w = N.el.querySelector(".nyW"), a = items().find((x) => x.id === N.cur);
    N.sig = sigOf(a); N.B = null; N.hear.pause();
    if (!a) {
      w.innerHTML = `<div class="nyEmpty"><i class="lamp green"></i><b>Nothing is waiting on you.</b>
        <p>The crew carries on by itself. Switch SIGN OFF on a seat in the rack to hear its work before it goes on, or use the
        INVOLVE key in the top bar to set every seat at once.</p></div>`;
      return;
    }
    const k = kindOf(a);
    w.innerHTML = `<header class="nyHead">
        <div class="nyMeta"><span class="nyVerb k-${k}">${VERB[k]}</span><span class="legend">${esc(from(a))} · ${esc(a.id)}${a.task ? " · " + esc(a.task) : ""} · ${esc(ago(a.created))}</span></div>
        <h3>${esc(a.title)}</h3>${ask(a) ? `<p class="nyAsk">${ask(a)}</p>` : ""}</header>
      <div class="nyBody"></div>${a.stage || a.take ? `<footer class="nyAns"></footer>` : ""}`;
    const body = w.querySelector(".nyBody");
    if (k === "plan") planWork(body, a);
    else if (k === "choose") chooseWork(body, a);
    else if (a.take) takeWork(body, a);
    else {
      body.classList.add("nyTicket"); body.appendChild(ticketEl(a));
      body.querySelectorAll("[data-desk]").forEach((b) => { const go = b.onclick; b.onclick = () => { Sheet.close(true); go(); }; });
    }
    if (a.stage || a.take) answers(w.querySelector(".nyAns"), a);
    wireAb(w);
  }
  function ask(a) {
    const seat = a.from && a.from !== "you" ? esc(title(a.from)) : "the seat", p = a.proposal || {};
    switch (kindOf(a)) {
      case "sign": return `Hear what the ${seat} seat handed in. Pass it on to the Producer, send it back with your notes, or change it yourself first: here, or on the Desk.`;
      case "decide": return "Hear it against the song, then accept it, send it back with what has to change, or discard it.";
      case "confirm": return `The Producer heard ${esc(a.take)} and wants to <b>${esc({ accept: "accept it", return: "send it back", discard: "discard it" }[p.decision] || p.decision)}</b>. Confirm it, overrule it, or ask the Producer to listen again.`;
      case "plan": return `${a.from === "producer" ? "The Producer" : seat} wants to hand out these tasks. Nothing starts until you approve. Change a title or a detail, drop a task or add one first if you like.`;
      case "choose": return `The ${seat} seat made ${(a.takes || []).length} versions of ${esc(a.task || "this task")}. Hear each one and keep the one you want.`;
      default: return "";
    }
  }

  // ---- answering a stop ------------------------------------------------------------------------------------------
  const needsText = (a, o) => (o.do === "return" && !o.confirm) || o.do === "ask" || o.do === "plan_back";
  const HINT = { return: "Say what has to change, so the seat can act on it.", ask: "Say what the Producer should listen for.",
    plan_back: "Say what to change in the plan." };
  const PLACE = { sign: "A note for the Producer, or what has to change if you send it back",
    decide: "What has to change, if you send it back: bars, tracks, what you heard",
    confirm: "Your note: needed to send it back or to ask again, optional otherwise",
    plan: "What to change, to send the plan back (or a note to go with your OK)",
    choose: "What you liked about it, or what was wrong with all of them" };
  function answers(foot, a) {
    const k = kindOf(a), opts = a.options.filter((o) => k !== "choose" || o.do !== "choose");
    const tk = a.take && takeOf(a.take), canAsk = (k === "sign" || k === "decide") && tk && tk.task;
    foot.innerHTML = `<textarea class="nyNote" rows="2" placeholder="${esc(PLACE[k] || "A note to go with your answer (optional)")}"></textarea>
      <div class="nyKeys">${opts.map((o, i) => `<button class="key ${i === 0 && o.do !== "return" ? "go" : ""} ${o.do === "discard" ? "warn" : ""}" data-opt="${esc(o.id)}"
          title="${esc(TIP[o.do] || "")}">${esc(o.label.toUpperCase())}</button>`).join("")}
        ${canAsk ? `<label class="nyAsk2" title="The seat makes two versions next time, and you choose"><input type="checkbox" class="nyTwo"> two versions next time</label>` : ""}
        <span class="nyErr hidden"></span></div>`;
    foot.querySelectorAll("[data-opt]").forEach((b) => b.onclick = () => answer(a, a.options.find((o) => o.id === b.dataset.opt), foot));
  }
  const TIP = { pass: "Send it on to the Producer's agent, with your note", accept: "Merge it into the song now (one undo step)",
    return: "Back to the seat that made it, with your note; it carries on in the same take", discard: "Close it without merging (it stays on disk)",
    ask: "Start the Producer again on this take, with your note", plan_ok: "The tasks go to their seats", plan_back: "The Producer revises the plan with your note" };
  async function answer(a, o, foot) {
    const note = foot.querySelector(".nyNote"), err = foot.querySelector(".nyErr"), text = note.value.trim();
    err.classList.add("hidden");
    if (needsText(a, o) && !text) {
      err.textContent = a.stage === "choose" ? "Say what was wrong with all of them." : HINT[o.do];
      err.classList.remove("hidden"); note.focus(); return;
    }
    if (o.do === "discard" && !confirm(`Discard ${a.take}? Its work stays on disk but is not merged.`)) return;
    foot.querySelectorAll("button").forEach((b) => b.disabled = true);
    try {
      const tk = a.take && takeOf(a.take), two = foot.querySelector(".nyTwo");
      if (two && two.checked && o.do === "return" && tk && tk.task) await api("task_update", { task: tk.task, options: 2 }, null);
      status("answering…", "busy");
      await api("attention_resolve", { item: a.id, option: o.id, text }, null);
      status(""); hush();
      toast(DONE[o.do] ? DONE[o.do](a) : "Answered.", true);
      refresh(true);
    } catch (e) {
      status(""); err.textContent = e.message; err.classList.remove("hidden");
      foot.querySelectorAll("button").forEach((b) => b.disabled = false);
    }
  }
  const DONE = { pass: (a) => `${a.take} passed to the Producer.`, accept: (a) => `${a.take} merged into the song.`,
    return: (a) => `${a.take || "It"} went back to the ${title(a.from === "producer" ? (takeOf(a.take) || {}).seat || "seat" : a.from)}.`,
    discard: (a) => `${a.take} discarded.`, ask: () => "The Producer will hear it again.", plan_ok: () => "Plan approved: the tasks go to their seats.",
    plan_back: () => "The plan went back to the Producer.", choose: () => "Kept. The others were put away." };

  // ---- a take: its work as its seat sees it, A/B, and room to change it ------------------------------------------
  function takeWork(body, a) {
    const tk = takeOf(a.take) || {}, p = a.proposal;
    body.innerHTML = `${p ? `<div class="nyVerdict v-${esc(p.decision)}"><span class="legend">THE PRODUCER SAYS</span>
        <b>${esc({ accept: "ACCEPT", return: "SEND IT BACK", discard: "DISCARD" }[p.decision] || p.decision)}</b>${p.reason ? `<p>${esc(p.reason)}</p>` : ""}</div>` : ""}
      ${tk.summary ? `<div class="nySum"><span class="legend">${esc(tk.seat ? title(tk.seat) : "Their")} summary</span><p>${esc(tk.summary)}</p></div>` : ""}
      <div class="nyBar">${abKeys(a.take)}<span class="spacer"></span>
        <span class="vfd small nySrc">${esc(a.take.toUpperCase())} · ${tk.returns ? `CAME BACK ${tk.returns}× · ` : ""}<span class="amb nyN">…</span></span>
        <button class="key" data-k="desk" title="Open this take on the Desk: arrangement, piano roll and mixer. Come back here to answer.">OPEN ON DESK</button></div>
      <div class="nyWork"><div class="empty">Reading ${esc(a.take)}…</div></div>
      <details class="nyDiff"><summary>WHAT ${esc(a.take.toUpperCase())} CHANGED</summary><ul class="changes"><li>reading…</li></ul></details>`;
    body.querySelector('[data-k="desk"]').onclick = async () => { hush(); Sheet.close(true); await openTake(a.take); };
    N.B = { data: null, version: null, take: a.take, hidden: new Set(), err: null, busy: false, hold: 0, focus: null, seat: tk.seat };
    loadWork();
    (N.reDiff = () => api("take_diff", { take: a.take }, null).then((d) => {
      if (!document.contains(body)) return;
      const ul = body.querySelector(".nyDiff ul"), out = new Set((d.outside_seat || []).map((x) => x.split(":")[0]));
      ul.innerHTML = d.changes.map((c) => `<li class="${out.has(c.where) ? "out" : ""}"><b>${esc(c.where)}</b> ${esc(c.what)}</li>`).join("") || "<li>No changes.</li>";
      const conf = (d.session_moved && d.session_moved.conflicts) || [];
      if (conf.length) ul.insertAdjacentHTML("beforeend", conf.map((c) => `<li class="conf">conflict · ${esc(c.where)}: ${esc(c.why || "")}</li>`).join(""));
      body.querySelector(".nyN").textContent = `${d.changes.length} CHANGE${d.changes.length === 1 ? "" : "S"}${conf.length ? ` · ${conf.length} CLASH` : ""}`;
    }).catch(() => {}))();
  }
  async function loadWork() {
    const B = N.B; if (!B) return;
    const seat = B.seat && seatOf(B.seat) ? B.seat : "arranger";  // a take cut for no seat reads like the note seats'
    try { const r = await api("seat_bench", { seat, take: B.take, have: B.version }, null); if (!r.unchanged) { B.data = r; B.version = r.version; } B.err = null; }
    catch (e) { B.err = e.message; }
    if (N.B === B) drawWork();
  }
  function drawWork() {
    const B = N.B, box = N.el && N.el.querySelector(".nyWork"); if (!B || !box) return;
    const d = B.data;
    if (!d) { box.innerHTML = `<div class="empty">${esc(B.err || "Reading…")}</div>`; return; }
    const s = seatOf(B.seat) || seatOf("arranger");
    if (d.view === "sound") return sounds(box, d, B);
    if (d.view === "mix") {
      return TTConsole.render(box, d, B, { take: B.take, active: () => !!N.el, redraw: drawWork,
        edited: () => { delete AB.cache[B.take]; }, reload: async () => { B.version = null; await loadWork(); N.reDiff(); } });
    }
    if (d.view === "picture") return workPicture(box, d, B, s);
    workRoll(box, d, B, s);
    // the part chips redraw this workspace, not the seat's bench in the rack
    box.querySelectorAll("[data-trk]").forEach((b) => b.onclick = () => { B.hidden.has(b.dataset.trk) ? B.hidden.delete(b.dataset.trk) : B.hidden.add(b.dataset.trk); drawWork(); });
  }
  // Sound: one slot per part, what it was before this take, a key to hear the sound alone, and CHANGE to swap it
  // in the take from the instrument browser, right here.
  function sounds(box, d, B) {
    const midi = d.tracks.map((t, i) => [t, i]).filter(([t]) => t.kind === "midi");
    box.innerHTML = midi.length ? `<div class="nySlots">${midi.map(([t, i]) => {
      const can = t.instrument && /^(synth|patch|vst3|sfz):/.test(t.instrument), moved = t.new || (t.was && "instrument" in t.was);
      return `<div class="nySlot ${moved ? "moved" : ""}" style="--c:${trackColor(t, i)}" data-trk="${esc(t.id)}">
        <div class="nm"><b>${esc(t.name)}</b><span>${esc(t.role || "")}${(t.clips || []).length ? "" : " · no part yet"}</span></div>
        <div class="nyInst"><span class="vfd small">${esc(t.instrument || "no instrument")}</span>
          ${t.was && "instrument" in t.was ? `<span class="wasI">was ${esc(t.was.instrument || "nothing")}</span>` : t.new ? `<span class="wasI">new track</span>` : `<span class="wasI dim">unchanged</span>`}</div>
        <div class="nyK">${can ? `<button class="key" data-k="hear" title="A short phrase on this sound alone, in the song's key and tempo">▶ SOUND</button>` : ""}
          <button class="key" data-k="change" title="Choose another instrument for this part, in the take">CHANGE…</button></div></div>`;
    }).join("")}</div><p class="nyHint">Hear the parts together with B above. CHANGE swaps an instrument in ${esc(B.take.toUpperCase())}; the Producer is told what you changed when you pass it on.</p>`
      : `<div class="empty">No instrument tracks in this take.</div>`;
    box.querySelectorAll("[data-k=hear]").forEach((b) => b.onclick = async () => {
      const t = d.tracks.find((x) => x.id === b.closest("[data-trk]").dataset.trk);
      if (b.classList.contains("playing")) { N.hear.pause(); b.classList.remove("playing"); return; }
      hush(); b.disabled = true; status("rendering a phrase…", "busy");
      try {
        const r = await api("gear_audition", { item: t.instrument, phrase: "auto", live: false }, null);
        N.hear.src = "/api/audio?file=" + encodeURIComponent(r.file) + "&t=" + Date.now(); await N.hear.play();
        box.querySelectorAll(".playing").forEach((x) => x.classList.remove("playing")); b.classList.add("playing");
        N.hear.onended = () => b.classList.remove("playing");
        if (r.silent) toast("That phrase was silent: the instrument has no sound loaded.", true);
      } catch (e) { toast(e.message); }
      status(""); b.disabled = false;
    });
    box.querySelectorAll("[data-k=change]").forEach((b) => b.onclick = () => browse(box, d, B, d.tracks.find((x) => x.id === b.closest("[data-trk]").dataset.trk)));
  }
  async function browse(box, d, B, t) {
    let list;
    try { list = await deviceItems(true); } catch (e) { toast(e.message); return; }
    B.hold++;  // the board may redraw meanwhile: keep the browser
    box.innerHTML = `<div class="nyBrowse"><div class="nyBrowseBar"><span class="legend">INSTRUMENT FOR</span><b>${esc(t.name)}</b>
      <span class="muted">now ${esc(t.instrument || "none")}</span><span class="spacer"></span><button class="key" data-k="back">BACK</button></div><div class="nyBrowseHost"></div></div>`;
    const back = () => { B.hold = Math.max(0, B.hold - 1); N.hear.pause(); drawWork(); };
    box.querySelector('[data-k="back"]').onclick = back;
    deviceBrowser(box.querySelector(".nyBrowseHost"), list, true, { current: t.instrument, hear: N.hear,
      use: (i) => i.id === t.instrument ? "KEEP" : `USE ON ${t.name.toUpperCase()}`,
      onPick: async (it) => {
        if (it.missing) { toast("Its plug-in file is gone"); return; }
        if (it.id === t.instrument) { back(); return; }
        try {
          status("changing the instrument…", "busy");
          await api("track_update", { track: t.id, instrument: it.id }, B.take);
          status(""); delete AB.cache[B.take]; toast(`${t.name} now plays ${it.name} in ${B.take}.`, true);
        } catch (e) { status(""); toast(e.message); }
        B.hold = Math.max(0, B.hold - 1); N.hear.pause(); B.version = null; loadWork(); N.reDiff();
      } });
  }

  // ---- a plan: the tasks, editable, and what the Producer also changed on the session ----------------------------
  function planWork(body, a) {
    const rows = (a.tasks || []).map((id) => taskOf(id) || { id, missing: true });
    const seats = ST.board.seats.filter((s) => s.occupant).map((s) => s.seat);
    body.innerHTML = `<div class="nyPlan">
      <div class="nyPlanHead"><span>TASK</span><span>SEAT</span><span>WHAT IT IS</span><span>AFTER</span><span></span></div>
      ${rows.map((t) => t.missing ? `<div class="nyT gone"><span class="id">${esc(t.id)}</span><span class="muted">no longer on the board</span></div>`
        : `<div class="nyT ${t.state === "cancelled" ? "gone" : ""}" data-t="${esc(t.id)}">
          <span class="id">${esc(t.id)}</span><span class="tag">${esc(title(t.seat).toUpperCase())}</span>
          <div class="nyTx"><input class="nyTi" data-f="title" value="${esc(t.title)}" ${t.state === "cancelled" ? "disabled" : ""} title="The task's title">
            <textarea class="nyTd" data-f="detail" rows="2" placeholder="Detail: what to make, which tracks and bars, what done sounds like" ${t.state === "cancelled" ? "disabled" : ""}>${esc(t.detail || "")}</textarea></div>
          <span class="dep">${(t.depends || []).length ? esc(t.depends.join(", ")) : "—"}</span>
          ${t.state === "cancelled" ? `<button class="key" data-k="restore" title="Put it back in the plan">RESTORE</button>` : `<button class="key warn" data-k="drop" title="Take this task out of the plan (cancelled)">DROP</button>`}</div>`).join("")}
      <div class="nyT add"><span class="id">+</span><select class="nySeat" title="Which seat does it">${seats.map((s) => `<option value="${esc(s)}">${esc(title(s))}</option>`).join("")}</select>
        <div class="nyTx"><input class="nyTi" data-new="title" placeholder="Add a task to this plan: one outcome, e.g. “A bridge, bars 17-24”">
          <textarea class="nyTd" data-new="detail" rows="1" placeholder="Detail (optional)"></textarea></div>
        <input class="nyDep" data-new="deps" placeholder="after T2…" title="Task ids that must finish first"><button class="key" data-k="add">ADD</button></div>
      ${(a.changes || []).length ? `<div class="nyAlso"><span class="legend">THE PRODUCER ALSO CHANGED THE SESSION · LIVE NOW, UNDOABLE</span>
        <ul class="changes">${a.changes.map((c) => { const i = c.indexOf(": "); return `<li><b>${esc(i > 0 ? c.slice(0, i) : c)}</b> ${esc(i > 0 ? c.slice(i + 2) : "")}</li>`; }).join("")}</ul></div>` : ""}
    </div>`;
    body.querySelectorAll(".nyT[data-t] [data-f]").forEach((f) => f.onchange = async () => {
      const id = f.closest("[data-t]").dataset.t, v = f.value.trim();
      if (f.dataset.f === "title" && !v) { toast("A task needs a title"); f.value = taskOf(id).title; return; }
      try { await api("task_update", { task: id, [f.dataset.f]: f.dataset.f === "title" ? v : f.value }, null); f.classList.add("saved"); setTimeout(() => f.classList.remove("saved"), 900); }
      catch (e) { toast(e.message); }
    });
    body.querySelectorAll(".nyT[data-t] [data-k]").forEach((b) => b.onclick = async () => {
      const id = b.closest("[data-t]").dataset.t;
      try { await api("task_update", { task: id, state: b.dataset.k === "drop" ? "cancelled" : "queued" }, null); N.sig = ""; refresh(true); }
      catch (e) { toast(e.message); }
    });
    body.querySelector('[data-k="add"]').onclick = async () => {
      const f = (k) => body.querySelector(`[data-new="${k}"]`).value.trim();
      if (!f("title")) { toast("Give the task a title"); body.querySelector('[data-new="title"]').focus(); return; }
      try {
        await api("task_add", { seat: body.querySelector(".nySeat").value, title: f("title"), detail: f("detail"), plan: a.id,
          depends: f("deps") ? f("deps").split(/[\s,]+/).filter(Boolean) : [] }, null);
        N.sig = ""; refresh(true);
      } catch (e) { toast(e.message); }
    };
  }

  // ---- options: side by side, hear each, keep one ----------------------------------------------------------------
  function chooseWork(body, a) {
    const ids = a.takes || [];
    body.innerHTML = `<div class="nyBar"><span class="ab"><button class="key" data-ab="session" title="The song as it is now, without any of them">▶ THE SONG NOW</button></span>
        <span class="muted">Play one, then another: switching keeps your place.</span></div>
      <div class="nyChoose" style="grid-template-columns:repeat(${Math.max(1, ids.length)}, minmax(0, 1fr))">${ids.map((id, i) => {
        const tk = takeOf(id) || {}, o = a.options.find((x) => x.take === id);
        return `<div class="nyOpt" data-take="${esc(id)}"><div class="legend">OPTION ${letter(i)} · ${esc(id.toUpperCase())}</div>
          <h4>${esc((tk.name || id).replace(/^option [a-z]:\s*/i, ""))}</h4>${tk.summary ? `<p>${esc(tk.summary)}</p>` : ""}
          <div class="row"><span class="ab"><button class="key" data-ab="${esc(id)}">▶ PLAY ${letter(i)}</button></span><span class="spacer"></span>
            ${o ? `<button class="key go" data-use="${esc(o.id)}">KEEP ${letter(i)}</button>` : ""}</div>
          <ul class="changes"><li>reading…</li></ul></div>`;
      }).join("")}</div>`;
    for (const id of ids) {
      const show = (d) => { const ul = body.querySelector(`.nyOpt[data-take="${CSS.escape(id)}"] ul`); if (ul) ul.innerHTML = d.changes.map((c) => `<li><b>${esc(c.where)}</b> ${esc(c.what)}</li>`).join("") || "<li>No changes.</li>"; };
      if (N.diffs[id]) show(N.diffs[id]);
      else api("take_diff", { take: id }, null).then((d) => { N.diffs[id] = d; if (document.contains(body)) show(d); }).catch(() => {});
    }
    body.querySelectorAll("[data-use]").forEach((b) => b.onclick = () => answer(a, a.options.find((o) => o.id === b.dataset.use), N.el.querySelector(".nyAns")));
  }

  // ---- INVOLVE: how much stops at you ----------------------------------------------------------------------------
  const PRESET = [["hands_off", "HANDS OFF", "Every hand-in goes straight to the Producer. You hear the finished song."],
    ["co_producer", "CO-PRODUCER", "The Producer's decisions on takes, and the plans it hands out, wait for your OK."],
    ["every", "EVERY HAND-IN", "Every seat's work stops at you before the Producer hears it, and so do the Producer's calls."]];
  const SHORT = { hands_off: "HANDS OFF", co_producer: "CO-PRODUCER", every: "EVERY HAND-IN", custom: "YOUR PICK" };
  function renderInvolve() {
    const b = $("#involveBtn"); if (!b) return;
    const bd = ST.board, aw = bd && bd.away && bd.away.on;
    b.disabled = !bd;
    b.querySelector("span").textContent = bd ? SHORT[bd.involve] || "HANDS OFF" : "HANDS OFF";
    b.querySelector(".lamp").className = "lamp" + (aw ? " amber" : bd && bd.involve !== "hands_off" ? " green" : "");
    b.title = (aw ? `AWAY: stops go through after ${bd.away.minutes} min. ` : "") + "How much of the crew's work stops at you";
    const pop = $("#involvePop"); if (pop && !pop.classList.contains("hidden")) drawInvolve(pop);
  }
  function drawInvolve(pop) {
    const bd = ST.board; if (!bd) return;
    const aw = bd.away && bd.away.on, signed = bd.seats.filter((s) => s.signoff).map((s) => s.title);
    pop.innerHTML = `<div class="popH">WHAT STOPS AT YOU</div>
      <div class="invRows">${PRESET.map(([k, l, d]) => `<button data-p="${k}" class="${bd.involve === k ? "on" : ""}"><b>${l}</b><span>${esc(d)}</span></button>`).join("")}</div>
      <p class="popHint">${bd.involve === "custom" ? `Now: ${esc(signed.join(", "))} sign off. ` : ""}Or use SIGN OFF on single seats in the rack.</p>
      <div class="popH">WHILE YOU ARE AWAY</div>
      <div class="invAway"><button class="key toggle ${aw ? "on" : ""}" data-k="away" title="Stops that wait this long go through as if their switch were off">AWAY</button>
        <span>let stops through after</span><select data-k="min">${[5, 10, 20, 45, 90].map((m) => `<option value="${m}" ${(aw ? bd.away.minutes : 20) == m ? "selected" : ""}>${m} min</option>`).join("")}</select></div>
      <p class="popHint">A choice between versions still waits for you, and so does a hand-in when no agent sits in the Producer seat.</p>`;
    pop.querySelectorAll("[data-p]").forEach((x) => x.onclick = () => call("crew_involve", { preset: x.dataset.p }));
    const min = pop.querySelector('[data-k="min"]');
    pop.querySelector('[data-k="away"]').onclick = () => call("away_set", { on_: !aw, minutes: +min.value });
    min.onchange = () => { if (aw) call("away_set", { on_: true, minutes: +min.value }); };
  }
  function wireInvolve() {
    const b = $("#involveBtn"), pop = $("#involvePop"); if (!b || !pop) return;
    b.onclick = (e) => {
      e.stopPropagation();
      if (!pop.classList.contains("hidden")) { pop.classList.add("hidden"); return; }
      const r = b.getBoundingClientRect();
      pop.style.top = `${r.bottom + 6}px`; pop.style.left = `${Math.max(8, Math.min(innerWidth - 348, r.left - 120))}px`;
      pop.classList.remove("hidden"); drawInvolve(pop);
    };
    addEventListener("mousedown", (e) => { if (!pop.classList.contains("hidden") && !pop.contains(e.target) && e.target !== b && !b.contains(e.target)) pop.classList.add("hidden"); });
    addEventListener("keydown", (e) => { if (e.key === "Escape") pop.classList.add("hidden"); });
  }
  wireInvolve();
  return { open, update, renderInvolve, kindOf, VERB, get openId() { return N.el ? N.cur : null; } };
})();
window.TTNeeds = TTNeeds;
