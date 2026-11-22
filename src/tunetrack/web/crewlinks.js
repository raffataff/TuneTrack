// Task cables on the crew rack: one cable per open task, from whoever handed it out to the seat doing it, looped
// through the patch gutter beside the rack. A task from You comes in over the gutter's outer edge (the side column
// is that way). A cable plugs in when its task opens, takes the colour of the task's state and unplugs when the task
// closes. When two seats talk (talkback, a take handed in, accepted or sent back), a light runs along their cable;
// two seats with no cable between them get a faint one for the moment. studio.js calls draw(board) whenever it
// renders the units and events(batch, board) with each live batch, before the board refreshes.
"use strict";

window.TTLinks = (() => {
  const NS = "http://www.w3.org/2000/svg", YOU = "you";
  const OPEN = ["queued", "working", "submitted", "needs_you"];
  const RANK = { queued: 1, working: 2, submitted: 3, needs_you: 4 };
  const STATE = { queued: "#3f7a98", working: "#5cc8ff", submitted: "#4fdc84", needs_you: "#ff5545" };
  const KIND = { talk: "#ffb224", task: "#5cc8ff", take: "#4fdc84", need: "#ff5545" };
  const SAID = { queued: "queued", working: "working", submitted: "handed in, waiting for review", needs_you: "waiting on you" };
  const motionOff = () => document.documentElement.dataset.motion === "off";
  const digital = () => document.documentElement.dataset.wiring === "digital";
  const still = matchMedia("(prefers-reduced-motion: reduce)");
  let svg = null, gCab, gJack, gPk, board = null, geo = null, sig = "", first = true, raf = 0;
  const cabs = new Map(), jacks = new Map();
  const mk = (n, a, p) => { const e = document.createElementNS(NS, n); for (const k in a) e.setAttribute(k, a[k]); if (p) p.appendChild(e); return e; };
  const titleOf = (id) => id === YOU ? "You" : ((board && board.seats.find((s) => s.seat === id)) || {}).title || id;
  const keyOf = (a, b) => [a, b].sort().join("|");
  const seen = () => svg && geo && !document.hidden && document.body.dataset.room === "studio";

  function init() {
    if (svg) return true;
    const rack = document.getElementById("rack"), units = document.getElementById("units");
    if (!rack || !units) return false;
    svg = mk("svg", { id: "patchSvg" }); rack.appendChild(svg);
    gCab = mk("g", {}, svg); gJack = mk("g", {}, svg); gPk = mk("g", {}, svg);
    new ResizeObserver(() => soon()).observe(units);
    return true;
  }
  function soon() { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => draw()); }

  // ------------------------------------------------------------ where things are
  // In the svg's own pixels: it scrolls with the rack, so these hold while the rack scrolls.
  function measure() {
    const R = svg.getBoundingClientRect(), P = document.getElementById("patch").getBoundingClientRect();
    const left = document.getElementById("studio").classList.contains("sideL"), dir = left ? -1 : 1;
    const g0 = (left ? P.right : P.left) - R.left, g1 = (left ? P.left : P.right) - R.left;
    const at = {}, order = [];
    for (const u of document.querySelectorAll("#units > .unit")) {
      if (u.classList.contains("hidden")) continue;
      const ears = u.querySelectorAll(":scope > .face > .ear"), ear = ears[left ? 0 : ears.length - 1];
      if (!ear) continue;
      const r = ear.getBoundingClientRect();
      at[u.dataset.seat] = { x: r.left + r.width / 2 - R.left, y: r.top + r.height / 2 - R.top, small: r.height < 40 };
      order.push(u.dataset.seat);
    }
    return { dir, g0, g1, w: Math.abs(g1 - g0), at, order, list: [], sp: 8 };
  }
  // The open tasks as cables: one per pair, carrying every task between those two.
  function wanted() {
    const out = new Map();
    for (const t of (board && board.tasks) || []) {
      const a = t.by, b = t.seat;
      if (!OPEN.includes(t.state) || !a || a === b || !geo.at[b] || (a !== YOU && !geo.at[a])) continue;
      const key = keyOf(a, b);
      if (!out.has(key)) out.set(key, { key, from: a, to: b, tasks: [] });
      out.get(key).tasks.push(t);
    }
    for (const c of out.values()) {
      c.state = c.tasks.reduce((s, t) => (RANK[t.state] > RANK[s] ? t.state : s), "queued");
      if (c.from !== YOU) { const ya = geo.at[c.from].y, yb = geo.at[c.to].y; c.lo = Math.min(ya, yb); c.hi = Math.max(ya, yb); }
    }
    return out;
  }
  // Lanes: a cable spanning more of the rack loops further out, so cables nest instead of crossing.
  // (only shorter cables count: they were placed first, so a cable sits just outside the ones it spans)
  const overlaps = (p, q) => Math.max(p.lo, q.lo) < Math.min(p.hi, q.hi) - 1;
  function laneFor(c, placed) { return 1 + Math.max(0, ...placed.filter((q) => q.hi - q.lo <= c.hi - c.lo && overlaps(c, q)).map((q) => q.lane)); }
  function lanes(list) {
    const placed = [];
    for (const c of list.filter((x) => x.from !== YOU).sort((p, q) => (p.hi - p.lo) - (q.hi - q.lo))) { c.lane = laneFor(c, placed); placed.push(c); }
    const max = Math.max(1, ...placed.map((c) => c.lane));
    geo.list = placed; geo.sp = Math.max(3, Math.min(11, (geo.w - 12) / max));
  }
  const bulge = (lane) => Math.min(geo.w - 5, 4 + lane * geo.sp);
  // seat to seat: out over the rail, a loop through the gutter, back in to the other jack
  function loop(a, b, lane, hitOnly) {
    const A = geo.at[a], B = geo.at[b], d = geo.dir, x = geo.g0, k = bulge(lane) / 0.75, sag = Math.min(14, Math.abs(B.y - A.y) * 0.05);
    if (digital()) {
      const laneX = x + d * bulge(lane), turn = Math.min(7, Math.abs(B.y - A.y) / 4), sign = Math.sign(B.y - A.y);
      const trace = `H${laneX - d * turn} L${laneX} ${A.y + sign * turn} V${B.y - sign * turn} L${laneX - d * turn} ${B.y} H${x}`;
      return hitOnly ? `M${x} ${A.y} ${trace}` : `M${A.x} ${A.y} H${x} ${trace} H${B.x}`;
    }
    const c = `C${x + d * k} ${A.y + sag} ${x + d * k} ${B.y + sag} ${x} ${B.y}`;
    return hitOnly ? `M${x} ${A.y} ${c}` : `M${A.x} ${A.y} H${x} ${c} H${B.x}`;
  }
  // a seat and You: straight out over the gutter's outer edge
  function edge(seat, out, hitOnly) {
    const A = geo.at[seat], x0 = hitOnly ? geo.g0 : A.x;
    return out ? `M${x0} ${A.y} H${geo.g1}` : `M${geo.g1} ${A.y} H${x0}`;
  }
  const pathOf = (from, to, lane, hitOnly) => from === YOU ? edge(to, false, hitOnly) : to === YOU ? edge(from, true, hitOnly) : loop(from, to, lane, hitOnly);
  function apex(c) {
    if (c.from === YOU) return { x: (geo.g0 + geo.g1) / 2, y: geo.at[c.to].y };
    return { x: geo.g0 + geo.dir * bulge(c.lane), y: (c.lo + c.hi) / 2 + Math.min(14, (c.hi - c.lo) * 0.05) * 0.75 };
  }

  // ------------------------------------------------------------ drawing
  function clear() { gCab.innerHTML = ""; gJack.innerHTML = ""; gPk.innerHTML = ""; cabs.clear(); jacks.clear(); sig = ""; geo = null; }
  function draw(b) {
    if (b !== undefined) board = b;
    if (!init()) return;
    if (!board) { clear(); return; }
    const units = document.getElementById("units");
    if (!units.offsetParent) return;  // the studio room is hidden: drawn when it shows again
    svg.style.height = units.offsetHeight + "px";
    geo = measure();
    const want = wanted(), list = [...want.values()];
    lanes(list);
    const s = JSON.stringify([digital(), geo.dir, Math.round(geo.g0), Math.round(geo.g1), geo.order.map((id) => [id, Math.round(geo.at[id].x), Math.round(geo.at[id].y)]),
      list.map((c) => [c.key, c.from, c.state, c.lane, c.tasks.map((t) => t.id + t.state + t.title).join()])]);
    if (s === sig) return;
    sig = s;
    for (const c of list) place(c);
    for (const [key, el] of cabs) if (!want.has(key) && !el.classList.contains("gone")) unplug(key, el);
    const plugged = new Set(list.flatMap((c) => [c.from, c.to]));
    for (const [id, el] of jacks) if (!geo.at[id]) { el.remove(); jacks.delete(id); }
    for (const id of geo.order) {
      const p = geo.at[id];
      let el = jacks.get(id);
      if (!el) { el = mk("circle", { class: "jack" }, gJack); jacks.set(id, el); }
      el.setAttribute("cx", p.x); el.setAttribute("cy", p.y); el.setAttribute("r", p.small ? 2.7 : 3.6);
      el.classList.toggle("on", plugged.has(id));
    }
    first = false;
  }
  function place(c) {
    let el = cabs.get(c.key);
    if (el && el.classList.contains("gone")) { el.remove(); el = null; }
    const d = pathOf(c.from, c.to, c.lane);
    if (!el) {
      el = mk("g", { class: "cab" }, gCab);
      mk("path", { class: "sh" }, el); mk("path", { class: "core" }, el);
      mk("title", {}, mk("path", { class: "hit" }, el));
      const tag = mk("g", { class: "tag" }, el); mk("circle", { r: 6 }, tag); mk("text", {}, tag);
      el.querySelector(".hit").addEventListener("click", () => api.onOpen && api.onOpen(el.dataset.to));
      el.busy = 0;
      cabs.set(c.key, el);
      if (!first && !still.matches && !motionOff()) plugIn(el, c);
    }
    el.dataset.from = c.from; el.dataset.to = c.to; el.unplug = false;
    el.style.setProperty("--c", STATE[c.state]);
    el.querySelector(".sh").setAttribute("d", d); el.querySelector(".core").setAttribute("d", d);
    el.querySelector(".hit").setAttribute("d", pathOf(c.from, c.to, c.lane, true));
    el.classList.toggle("multi", c.tasks.length > 1);
    const tag = el.querySelector(".tag"), at = apex(c);
    tag.setAttribute("transform", `translate(${at.x} ${at.y})`); tag.querySelector("text").textContent = c.tasks.length;
    el.querySelector("title").textContent = `${titleOf(c.from)} → ${titleOf(c.to)}\n`
      + c.tasks.map((t) => `${t.id} ${t.title} · ${SAID[t.state] || t.state}`).join("\n") + `\nClick to open ${titleOf(c.to)}'s tasks`;
  }
  // a new cable draws itself from whoever handed the task out to the seat that has it
  function plugIn(el, c) {
    const ps = el.querySelectorAll(".sh, .core");
    ps.forEach((p) => p.setAttribute("pathLength", "1"));
    el.classList.add("new");
    setTimeout(() => { el.classList.remove("new"); ps.forEach((p) => p.removeAttribute("pathLength")); flash(c.to, KIND.task); }, 600);
  }
  function unplug(key, el) {
    if (el.busy) { el.unplug = true; return; }  // a light is still on it: unplugged when it arrives
    el.classList.add("gone");
    setTimeout(() => { el.remove(); if (cabs.get(key) === el) cabs.delete(key); }, 850);
  }

  // ------------------------------------------------------------ lights
  function flash(id, col) {
    if (id === YOU) return;
    const j = jacks.get(id); if (!j) return;
    j.style.setProperty("--c", col); j.classList.remove("hit"); j.getBoundingClientRect(); j.classList.add("hit");
    clearTimeout(j.t); j.t = setTimeout(() => j.classList.remove("hit"), 650);
  }
  function bay(kind) {
    const h = document.querySelector(kind === "need" ? "#needs h2" : "#monitor h2"); if (!h) return;
    h.style.setProperty("--c", KIND[kind]); h.classList.add("rx");
    clearTimeout(h.rxT); h.rxT = setTimeout(() => h.classList.remove("rx"), 700);
  }
  function lit(el, on) {
    el.busy += on ? 1 : -1;
    el.classList.toggle("lit", el.busy > 0);
    if (!el.busy && el.unplug) { el.unplug = false; unplug([...cabs].find(([, v]) => v === el)?.[0], el); }
  }
  // A light from one seat (or You) to another, along their cable, or a faint cable for the moment.
  function pulse(kind, from, to) {
    if (!seen() || !from || !to || from === to) return;
    if ((from !== YOU && !geo.at[from]) || (to !== YOU && !geo.at[to])) return;
    const col = KIND[kind] || KIND.talk;
    let el = cabs.get(keyOf(from, to)), path, back = false, ghost = null;
    if (el && !el.classList.contains("gone")) { path = el.querySelector(".core"); back = el.dataset.from !== from; }
    else {
      el = null;
      const c = from === YOU || to === YOU ? null : { lo: Math.min(geo.at[from].y, geo.at[to].y), hi: Math.max(geo.at[from].y, geo.at[to].y) };
      const d = pathOf(from, to, c ? laneFor(c, geo.list) : 0);
      ghost = mk("g", { class: "cab ghost" }, gCab); ghost.style.setProperty("--c", col);
      mk("path", { class: "sh", d }, ghost); path = mk("path", { class: "core", d }, ghost);
    }
    flash(from, col);
    if (from === YOU) bay("talk");
    const arrive = () => { flash(to, col); if (to === YOU) bay(kind === "need" ? "need" : "talk"); };
    const done = () => {
      if (el) lit(el, false);
      if (ghost) { ghost.classList.add("gone"); setTimeout(() => ghost.remove(), 850); }
    };
    if (el) lit(el, true);
    if (still.matches || motionOff() || gPk.childElementCount > 16) { setTimeout(() => { arrive(); done(); }, 300); return; }
    const len = path.getTotalLength(), dur = Math.min(1500, 420 + len * 1.3), box = kind === "take", tail = 7;
    const g = mk("g", { class: "pk" }, gPk); g.style.setProperty("--c", col);
    const dots = [0, 1, 2, 3].map((i) => box
      ? mk("rect", { width: 7 - i * 1.4, height: 7 - i * 1.4, rx: 1.2, opacity: 1 - i * 0.24 }, g)
      : mk("circle", { r: 3.3 - i * 0.62, opacity: 1 - i * 0.24 }, g));
    const t0 = performance.now(), ease = (t) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2);
    let hit = false, lost = false;
    const step = (now) => {
      let t = lost ? 1 : Math.min(1, (now - t0) / dur);
      const head = ease(t) * (len + tail * 3);
      try {
        dots.forEach((dot, i) => {
          const s = Math.max(0, Math.min(len, head - i * tail)), pt = path.getPointAtLength(back ? len - s : s);
          if (box) { const w = +dot.getAttribute("width"); dot.setAttribute("x", pt.x - w / 2); dot.setAttribute("y", pt.y - w / 2); }
          else { dot.setAttribute("cx", pt.x); dot.setAttribute("cy", pt.y); }
        });
      } catch { lost = true; t = 1; }  // the cable went away under it: finish now
      if (!hit && head >= len) { hit = true; arrive(); }
      if (t < 1) requestAnimationFrame(step); else { g.remove(); if (!hit) arrive(); done(); }
    };
    requestAnimationFrame(step);
  }
  // to everyone: the sender's jack, then every other jack, nearest first
  function broadcast(from) {
    if (!seen()) return;
    if (from === YOU) bay("talk"); else flash(from, KIND.talk);
    const y0 = from !== YOU && geo.at[from] ? geo.at[from].y : 0;
    for (const id of geo.order) if (id !== from) setTimeout(() => flash(id, KIND.talk), 120 + Math.abs(geo.at[id].y - y0) * 0.9);
  }

  // ------------------------------------------------------------ the event stream
  // Live events only (not the history replayed on connect). The board is the one from before this batch, so a
  // task still says who handed it out.
  function events(batch, b) {
    if (!seen()) return;
    let n = 0;
    const later = (fn) => { const d = n++ * 140; if (d) setTimeout(fn, d); else fn(); };
    for (const e of batch) {
      if (e.kind === "message") {
        if (e.to === "all" || e.to === "floor") later(() => broadcast(e.sender)); else later(() => pulse("talk", e.sender, e.to));
      } else if (e.kind === "task") {
        const t = b && b.tasks.find((x) => x.id === e.id), by = t && t.by;
        if (e.state === "submitted" && by) later(() => pulse("take", e.seat, by));
        else if (e.state === "done" && e.by && e.by !== e.seat) later(() => pulse("take", e.by, e.seat));  // accepted
        else if (e.state === "done" && by) later(() => pulse("take", e.seat, by));  // finished without a take
      } else if (e.kind === "attention") {
        if (e.state === "open" && e.sender) later(() => pulse("need", e.sender, YOU));
        else if (e.state === "resolved") { const a = b && b.needs_you.find((x) => x.id === e.id); if (a && a.from) later(() => pulse("talk", YOU, a.from)); }
      }
    }
  }
  function reset() { if (svg) clear(); first = true; }

  addEventListener("tt-appearance", soon);
  const api = { draw, events, pulse, broadcast, reset, onOpen: null };
  return api;
})();
