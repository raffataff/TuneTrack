// Cables: the project's signal flow as a patch bay. Columns left to right: the inputs in use (interface inputs
// audio tracks record, MIDI keyboards playing armed tracks), the tracks, the buses (a bus feeding another bus
// sits a column further left), the master and the output. Drag a track's OUT onto a bus or the master to route it,
// drag its SEND onto a bus to add a send, click a cable to change or remove it. Every edit is one act() call.
// Loaded after app.js and shares its globals (S, $, act, esc, menu, toast, trackColor, instLabel, liveMode, ...).
"use strict";

window.TTCables = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const SEND_DB = [0, -3, -6, -12, -18, -24];
  const W_MAIN = 208, W_IO = 172, GAP_Y = 16, TOP = 44, PAD_X = 24;
  const Y_OUT = 15, Y_SEND = 31;  // port centres from a card's top (the name line, the second line)
  let root = null, on = false, raf = 0, sig = "", drag = null, focus = null, nodes = {}, cables = [], lastW = 0;

  // ------------------------------------------------------------ data
  const tracks = () => (S.doc ? S.doc.tracks : []);
  const byId = (id) => tracks().find((t) => t.id === id);
  const colorOf = (id) => { const i = tracks().findIndex((t) => t.id === id); return i < 0 ? null : trackColor(tracks()[i], i); };
  const eng = () => (S.eng.st && S.eng.st.on ? S.eng.st : null);
  const armedMap = () => (eng() && !eng().audition && eng().armed) || {};
  const fxLabel = (f) => { const n = String(f.name || f.type || "?"); return f.type === "builtin" ? n[0].toUpperCase() + n.slice(1) : n; };
  const subLine = (t) => t.kind === "midi" ? (t.instrument ? instLabel(t.instrument) : "No instrument")
    : t.kind === "audio" ? "Audio · " + (t.input && t.input.length ? "In " + t.input.join("+") : "no input")
    : "Bus · " + tracks().filter((x) => x.output === t.id || (x.sends || []).some((s) => s.to === t.id)).length + " sources";
  const peakOf = (id) => {
    const st = eng(); if (!st || st.audition || !st.peaks) return 0;
    const pk = st.peaks[id]; return pk && pk.length ? Math.max(...pk) : +pk || 0;
  };
  // a bus's distance from the master along bus outputs (1: straight into the master)
  function rank(t, seen = new Set()) {
    if (!t || t.output === "master" || !t.output || seen.has(t.id)) return 1;
    seen.add(t.id); const to = byId(t.output); return to && to.kind === "bus" ? 1 + rank(to, seen) : 1;
  }
  // the inputs in use: interface inputs (shared by tracks recording the same channels) and armed MIDI keyboards
  function inputs() {
    const out = new Map(), names = (eng() && eng().inputs && eng().inputs.audio) || [], arm = armedMap();
    for (const t of tracks()) {
      if (t.kind === "audio" && t.input && t.input.length) {
        const id = "in:a:" + t.input.join("+");
        if (!out.has(id)) out.set(id, { id, name: "Input " + t.input.join("+"), sub: t.input.map((c) => names[c - 1] || "channel " + c).join(" / "), to: [] });
        out.get(id).to.push(t.id);
      } else if (t.kind === "midi" && arm[t.id] && arm[t.id].arm) {
        const k = arm[t.id].keyboard || "", id = "in:m:" + k;
        if (!out.has(id)) out.set(id, { id, name: k || "Any MIDI keyboard", sub: "MIDI in", to: [], midi: true });
        out.get(id).to.push(t.id);
      }
    }
    return [...out.values()];
  }

  // ------------------------------------------------------------ build
  function card(id, o) {
    const el = document.createElement("div");
    el.className = "cbNode" + (o.cls ? " " + o.cls : ""); el.dataset.id = id;
    el.style.setProperty("--c", o.color); el.style.width = o.w + "px";
    const fx = (o.fx || []).map((f, i) => `<div class="cbRow${f.bypass ? " off" : ""}${f.sidechain ? " keyed" : ""}" data-i="${i}">
        <b>${i + 1}</b><span title="${esc(fxLabel(f) + (f.patch ? " · " + f.patch : ""))}">${esc(fxLabel(f))}${f.patch ? `<em>${esc(f.patch)}</em>` : ""}</span>
        ${f.bypass ? '<i class="tag">BYP</i>' : f.sidechain ? '<i class="tag key">KEY</i>' : ""}</div>`).join("");
    el.innerHTML = `<div class="cbHead"><div class="cbName" title="${esc(o.name)}">${esc(o.name)}${o.badge || ""}</div>
      <div class="cbSub" title="${esc(o.sub)}">${esc(o.sub)}</div>
      ${o.out ? '<span class="cbJack o">OUT</span>' : ""}${o.send ? '<span class="cbJack s">SEND</span>' : ""}</div>
      ${fx ? `<div class="cbFx">${fx}</div>` : ""}
      ${o.in ? '<i class="cbPort in" data-port="in"></i>' : ""}${o.out ? '<i class="cbPort out" data-port="out" title="Drag onto a bus or the master to route this here"></i>' : ""}
      ${o.send ? '<i class="cbPort send" data-port="send" title="Drag onto a bus to add a send"></i>' : ""}`;
    return el;
  }

  function build() {
    if (!root) return;
    const d = S.doc;
    root.innerHTML = `<div class="cbBar"><span class="legend">CABLES</span><span class="vfd small" id="cbRead"></span>
      <span class="cbHint">Drag <b>OUT</b> onto a bus or the master to route · drag <b>SEND</b> onto a bus · click a cable to change it</span>
      <span class="spacer"></span>
      <span class="cbKey"><i class="m"></i>MAIN</span><span class="cbKey"><i class="s"></i>SEND</span><span class="cbKey"><i class="k"></i>KEY</span></div>
      <div class="cbScroll"><div class="cbStage"><svg class="cbWires" xmlns="${NS}"></svg></div></div>`;
    const stage = root.querySelector(".cbStage"), scroll = root.querySelector(".cbScroll");
    nodes = {}; cables = [];
    if (!d) { stage.insertAdjacentHTML("beforeend", '<div class="cbEmpty">No project open.</div>'); return; }

    const ins = inputs(), trs = tracks().filter((t) => t.kind !== "bus"), buses = tracks().filter((t) => t.kind === "bus");
    const maxR = buses.reduce((m, b) => Math.max(m, rank(b)), 1);
    const busCols = []; for (let r = maxR; r >= 1; r--) busCols.push(buses.filter((b) => rank(b) === r));
    const cols = [{ key: "INPUTS", w: W_IO }, { key: "TRACKS", w: W_MAIN },
      ...busCols.map((_, i) => ({ key: i ? "" : "BUSES", w: W_MAIN })), { key: "MASTER", w: W_MAIN }, { key: "OUTPUT", w: W_IO }];
    const avail = scroll.clientWidth - 2 * PAD_X, sum = cols.reduce((a, c) => a + c.w, 0);
    const gut = Math.max(56, Math.min(168, (avail - sum) / (cols.length - 1)));
    let x = PAD_X + Math.max(0, (avail - sum - gut * (cols.length - 1)) / 2);
    for (const c of cols) { c.x = x; x += c.w + gut; }
    const stageW = x - gut + PAD_X;
    cols.forEach((c) => { if (c.key) stage.insertAdjacentHTML("beforeend", `<div class="cbCol legend" style="left:${c.x}px">${c.key}</div>`); });

    const add = (id, col, o) => { const el = card(id, { w: cols[col].w, ...o }); stage.appendChild(el); nodes[id] = { el, col, x: cols[col].x, w: cols[col].w, o }; return el; };
    ins.forEach((n) => add(n.id, 0, { name: n.name, sub: n.sub, color: "var(--line2)", out: true, cls: "io" + (n.midi ? " midi" : "") }));
    trs.forEach((t) => {
      const i = tracks().indexOf(t);
      add(t.id, 1, { name: t.name, sub: subLine(t), color: trackColor(t, i), fx: t.fx, in: true, out: true, send: true,
        cls: t.mute ? "muted" : "", badge: (t.mute ? '<i class="tag">M</i>' : "") + (t.solo ? '<i class="tag solo">S</i>' : "") });
    });
    busCols.forEach((list, ci) => list.forEach((t) => {
      const i = tracks().indexOf(t);
      add(t.id, 2 + ci, { name: t.name, sub: subLine(t), color: trackColor(t, i), fx: t.fx, in: true, out: true, send: true,
        cls: "bus" + (t.mute ? " muted" : ""), badge: t.mute ? '<i class="tag">M</i>' : "" });
    }));
    const mc = cols.length - 2, m = d.master || {};
    add("master", mc, { name: "Master", sub: `${m.target_lufs ?? -14} LUFS · ceiling ${m.ceiling_db ?? -1} dB`, color: "var(--amber)", fx: m.fx || [], in: true, out: true, cls: "master" });
    const dev = liveMode() && eng() && eng().device;
    add("out", mc + 1, { name: liveMode() ? (dev ? dev.name : "Audio device") : "Render", color: "var(--dim)", in: true, cls: "io",
      sub: liveMode() ? (dev ? `${dev.type || "LIVE"} · ${dev.sr} Hz` : "LIVE engine") : "Offline mix to file" });
    if (!ins.length) stage.insertAdjacentHTML("beforeend", `<div class="cbNote" style="left:${cols[0].x}px;top:${TOP}px;width:${W_IO}px">Audio tracks show the interface input they record; armed MIDI tracks show their keyboard.</div>`);
    if (!buses.length) stage.insertAdjacentHTML("beforeend", `<div class="cbNote" style="left:${cols[2].x}px;top:${TOP}px;width:${W_MAIN}px">No buses. Add one on the Desk (+ Bus) to share effects through sends.</div>`);

    // vertical: each column in its order, a card as near the sources feeding it as the cards above allow
    const outY = (id) => nodes[id].y + Y_OUT;
    const place = (ids, want) => { let y = TOP; for (const id of ids) { const n = nodes[id]; n.h = n.el.offsetHeight; n.y = Math.max(y, Math.round(want(id) ?? TOP)); y = n.y + n.h + GAP_Y; } };
    place(trs.map((t) => t.id), () => TOP);
    place(ins.map((n) => n.id), (id) => { const f = ins.find((n) => n.id === id).to[0]; return nodes[f] ? nodes[f].y : TOP; });
    const feeders = (id) => tracks().filter((x) => nodes[x.id] && nodes[x.id].y != null && (x.output === id || (x.sends || []).some((s) => s.to === id)));
    const mean = (id) => { const f = feeders(id); return f.length ? f.reduce((a, x) => a + outY(x.id), 0) / f.length - Y_OUT : TOP; };
    busCols.forEach((list) => place(list.map((b) => b.id), mean));
    place(["master"], () => Math.min(TOP + 160, mean("master"))); place(["out"], () => nodes.master.y);
    for (const id in nodes) { const n = nodes[id]; n.el.style.left = n.x + "px"; n.el.style.top = n.y + "px"; }
    const stageH = Math.max(...Object.values(nodes).map((n) => n.y + n.h)) + 48;
    stage.style.width = stageW + "px"; stage.style.height = stageH + "px";
    const svg = stage.querySelector(".cbWires"); svg.setAttribute("width", stageW); svg.setAttribute("height", stageH);

    // cables
    const P = (id, port) => { const n = nodes[id]; return port === "in" ? [n.x, n.y + Y_OUT] : [n.x + n.w, n.y + (port === "send" ? Y_SEND : Y_OUT)]; };
    const live = (t) => !t || !t.mute;
    ins.forEach((n) => n.to.forEach((tid) => nodes[tid] && cables.push({ kind: "in", a: n.id, b: tid, src: tid, color: colorOf(tid), from: P(n.id, "out"), to: P(tid, "in"), live: true, peak: () => 0 })));
    for (const t of tracks()) {
      if (!nodes[t.id]) continue;
      const dest = t.output && t.output !== "master" && nodes[t.output] ? t.output : "master";
      cables.push({ kind: "main", a: t.id, b: dest, src: t.id, color: colorOf(t.id), from: P(t.id, "out"), to: P(dest, "in"), live: live(t), peak: () => peakOf(t.id) });
      (t.sends || []).forEach((s, si) => nodes[s.to] && cables.push({ kind: "send", a: t.id, b: s.to, src: t.id, si, db: s.db, color: colorOf(t.id),
        from: P(t.id, "send"), to: P(s.to, "in"), live: live(t), peak: () => peakOf(t.id) }));
      (t.fx || []).forEach((f, fi) => {
        if (!f.sidechain || !nodes[f.sidechain]) return;
        const row = nodes[t.id].el.querySelector(`.cbRow[data-i="${fi}"]`), n = nodes[t.id], k = nodes[f.sidechain];
        const ry = n.y + row.offsetTop + row.offsetHeight / 2, left = k.col < n.col;
        row.insertAdjacentHTML("beforeend", `<i class="cbPort key ${left ? "l" : "r"}"></i>`);
        cables.push({ kind: "key", a: f.sidechain, b: t.id, src: f.sidechain, fi, color: "var(--cyan)", from: P(f.sidechain, "out"),
          to: [left ? n.x : n.x + n.w, ry], back: !left, live: live(byId(f.sidechain)), peak: () => peakOf(f.sidechain) });
      });
    }
    (m.fx || []).forEach((f, fi) => {
      if (!f.sidechain || !nodes[f.sidechain]) return;
      const n = nodes.master, row = n.el.querySelector(`.cbRow[data-i="${fi}"]`);
      row.insertAdjacentHTML("beforeend", '<i class="cbPort key l"></i>');
      cables.push({ kind: "key", a: f.sidechain, b: "master", src: f.sidechain, fi, color: "var(--cyan)", from: P(f.sidechain, "out"),
        to: [n.x, n.y + row.offsetTop + row.offsetHeight / 2], live: live(byId(f.sidechain)), peak: () => peakOf(f.sidechain) });
    });
    cables.push({ kind: "out", a: "master", b: "out", color: "var(--muted)", from: P("master", "out"), to: P("out", "in"), live: true, peak: () => peakOf("master") });
    drawCables(svg);
    markPorts();
    const nSends = tracks().reduce((a, t) => a + (t.sends || []).length, 0);
    root.querySelector("#cbRead").textContent = `${trs.length} TRK · ${buses.length} BUS · ${nSends} SND`;
    bindNodes(stage);
    if (focus && nodes[focus]) setFocus(focus);
  }

  // a cable's curve: forward cables leave right and arrive left; one that runs back (or within its column, as a
  // sidechain key does) loops out past the right edges
  function ctrl(c) {
    const [x1, y1] = c.from, [x2, y2] = c.to;
    if (c.back) { const xr = Math.max(x1, x2) + Math.max(48, Math.abs(y2 - y1) * 0.35); return [c.from, [xr, y1], [xr, y2], c.to]; }
    const k = x2 < x1 - 4 ? Math.max(96, Math.abs(y2 - y1) * 0.7) : Math.max(32, (x2 - x1) * 0.5);
    return [c.from, [x1 + k, y1], [x2 - k, y2], c.to];
  }
  const curve = (c) => { const [a, b, d, e] = ctrl(c); return `M${a[0]},${a[1]} C${b[0]},${b[1]} ${d[0]},${d[1]} ${e[0]},${e[1]}`; };
  const at = (c, t) => {
    const p = ctrl(c), u = 1 - t, w = [u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t];
    return [0, 1].map((k) => p.reduce((a, q, i) => a + q[k] * w[i], 0));
  };
  // where a send's level label goes: the point nearest the middle clear of the cards and the other labels
  function labelAt(c, w, taken) {
    const clear = ([x, y]) => !Object.values(nodes).some((n) => x + w / 2 > n.x - 4 && x - w / 2 < n.x + n.w + 4 && y + 8 > n.y - 4 && y - 8 < n.y + n.h + 4)
      && !taken.some(([tx, ty, tw]) => Math.abs(x - tx) < (w + tw) / 2 + 4 && Math.abs(y - ty) < 20);
    for (const t of [0.5, 0.42, 0.58, 0.34, 0.66, 0.26, 0.74, 0.18, 0.82]) { const p = at(c, t); if (clear(p)) return p; }
    return at(c, 0.5);
  }
  function el(tag, attrs, parent) { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }
  function drawCables(svg) {
    svg.innerHTML = "";
    const layers = { key: el("g", {}, svg), send: el("g", {}, svg), main: el("g", {}, svg), lab: el("g", {}, svg) };
    // mains on top, the labels above everything
    const taken = [];
    cables.forEach((c, i) => {
      const g = el("g", { class: `cb ${c.kind}${c.live ? "" : " dimmed"}`, "data-i": i, "data-a": c.a, "data-b": c.b }, c.kind === "send" ? layers.send : c.kind === "key" ? layers.key : layers.main);
      const d = curve(c);
      el("path", { d, class: "hit" }, g);
      el("path", { d, class: "wire", stroke: c.color }, g);
      c.flow = el("path", { d, class: "flow" }, g);
      c.g = g;
      if (c.kind === "send") {
        const t = `${c.db > 0 ? "+" : ""}${Math.round(c.db * 10) / 10} dB`, w = t.length * 6 + 10, [mx, my] = labelAt(c, w, taken);
        taken.push([mx, my, w]);
        const lg = el("g", { class: `cbLab${c.live ? "" : " dimmed"}`, "data-i": i, "data-a": c.a, "data-b": c.b, transform: `translate(${mx - w / 2},${my - 8})` }, layers.lab);
        el("rect", { width: w, height: 16, rx: 3 }, lg);
        el("text", { x: w / 2, y: 11.5, "text-anchor": "middle" }, lg).textContent = t;
        c.lab = lg;
      }
    });
    svg.querySelectorAll("g.cb, g.cbLab").forEach((g) => {
      g.onmouseenter = () => g.classList.add("hover"); g.onmouseleave = () => g.classList.remove("hover");
      g.onclick = (e) => cableMenu(e, cables[+g.dataset.i]);
    });
  }
  function markPorts() {
    for (const c of cables) {
      const a = nodes[c.a], b = nodes[c.b];
      const pa = a && a.el.querySelector(c.kind === "send" ? ".cbPort.send" : ".cbPort.out"), pb = b && b.el.querySelector(".cbPort.in");
      if (pa) { pa.classList.add("on"); pa.style.setProperty("--pc", c.kind === "key" ? colorOf(c.a) || "var(--cyan)" : c.color); }
      if (pb && c.kind !== "key") { pb.classList.add("on"); if (!pb.style.getPropertyValue("--pc")) pb.style.setProperty("--pc", c.color); }
    }
  }

  // ------------------------------------------------------------ focus
  function setFocus(id) {
    focus = id;
    const svg = root && root.querySelector(".cbWires"); if (!svg) return;
    const touch = new Set([id]);
    svg.classList.toggle("focus", !!id);
    svg.querySelectorAll("g.cb, g.cbLab").forEach((g) => {
      const lit = !!id && (g.dataset.a === id || g.dataset.b === id);
      g.classList.toggle("lit", lit); if (lit) { touch.add(g.dataset.a); touch.add(g.dataset.b); }
    });
    for (const k in nodes) nodes[k].el.classList.toggle("faded", !!id && !touch.has(k));
  }

  // ------------------------------------------------------------ edits
  const run = (fn, args) => act(fn, args).catch(() => {});  // act() has shown a refusal with toast
  const setSends = (t, sends) => run("track_update", { track: t.id, sends: sends.map((s) => ({ to: s.to, db: s.db })) });
  function cableMenu(e, c) {
    if (!c) return;
    const t = byId(c.a), b = c.b === "master" ? { name: "Master" } : byId(c.b) || {};
    if (c.kind === "send" && t) {
      const cur = t.sends[c.si];
      menu(e, [[`Send ${t.name} → ${b.name}`, null],
        ...SEND_DB.map((db) => [`${Math.abs(cur.db - db) < 0.05 ? "• " : ""}${db > 0 ? "+" : ""}${db} dB`, () => setSends(t, t.sends.map((s, i) => i === c.si ? { ...s, db } : s))]),
        ["Remove send", () => setSends(t, t.sends.filter((_, i) => i !== c.si))]]);
    } else if (c.kind === "main" && t) {
      const others = tracks().filter((x) => x.kind === "bus" && x.id !== t.id && x.id !== c.b);
      menu(e, [[`${t.name} → ${b.name}`, null],
        ...(c.b !== "master" ? [["Route to master", () => run("track_update", { track: t.id, output: "master" })]] : []),
        ...others.map((x) => [`Route to ${x.name}`, () => run("track_update", { track: t.id, output: x.id })])]);
    } else if (c.kind === "key" && t) {
      const fx = c.b === "master" ? S.doc.master.fx[c.fi] : byId(c.b).fx[c.fi];
      menu(e, [[`Key ${t.name} → ${fxLabel(fx)} on ${b.name}`, null], ["Disconnect key", () => run("fx_update", { track: c.b, index: c.fi, sidechain: "" })]]);
    }
  }
  // the bus a send would feed: refused here if its route leads back to the sender (the server checks outputs only)
  function feedsBack(from, to) {
    const seen = new Set(); let cur = byId(to);
    while (cur && !seen.has(cur.id)) { if (cur.id === from) return true; seen.add(cur.id); cur = byId(cur.output); }
    return false;
  }
  function targets(kind, t) {
    const ok = new Set();
    for (const x of tracks()) if (x.kind === "bus" && x.id !== t.id && (kind === "out" ? x.id !== t.output : !(t.sends || []).some((s) => s.to === x.id))) ok.add(x.id);
    if (kind === "out" && t.output && t.output !== "master") ok.add("master");
    return ok;
  }
  function bindNodes(stage) {
    for (const id in nodes) {
      const n = nodes[id];
      n.el.onmouseenter = () => { if (!drag) setFocus(id); };
      n.el.onmouseleave = () => { if (!drag) setFocus(null); };
      n.el.querySelectorAll(".cbPort.out, .cbPort.send").forEach((p) => p.onmousedown = (e) => {
        const t = byId(id); if (!t || e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        const kind = p.dataset.port, ok = targets(kind, t), r = stage.getBoundingClientRect();
        const from = kind === "send" ? [n.x + n.w, n.y + Y_SEND] : [n.x + n.w, n.y + Y_OUT];
        const svg = stage.querySelector(".cbWires");
        const ghost = el("path", { class: "ghost" + (kind === "send" ? " send" : ""), stroke: colorOf(id) }, svg);
        drag = { kind, t, ok, ghost, from, stage, hot: null };
        ok.forEach((k) => nodes[k] && nodes[k].el.classList.add("drop"));
        root.classList.add("dragging");
        dragMove(e, r);
      });
    }
  }
  function dragMove(e) {
    if (!drag) return;
    const r = drag.stage.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    const hit = document.elementFromPoint(e.clientX, e.clientY), nd = hit && hit.closest(".cbNode"), id = nd && nd.dataset.id;
    const hot = id && drag.ok.has(id) ? id : null;
    if (hot !== drag.hot) { if (drag.hot) nodes[drag.hot].el.classList.remove("hot"); if (hot) nodes[hot].el.classList.add("hot"); drag.hot = hot; }
    const to = hot ? [nodes[hot].x, nodes[hot].y + Y_OUT] : [x, y];
    drag.ghost.setAttribute("d", curve({ from: drag.from, to, back: false }));
  }
  function dragEnd(commit) {
    if (!drag) return;
    const { kind, t, hot, ghost } = drag; drag = null;
    ghost.remove(); root.classList.remove("dragging");
    for (const k in nodes) nodes[k].el.classList.remove("drop", "hot");
    if (!commit || !hot) return;
    if (kind === "out") run("track_update", { track: t.id, output: hot });
    else if (feedsBack(t.id, hot)) toast(`${t.name} cannot send to ${byId(hot).name}: its sound would be fed back into itself`);
    else setSends(t, [...(t.sends || []), { to: hot, db: -12 }]);
  }
  addEventListener("mousemove", (e) => drag && dragMove(e));
  addEventListener("mouseup", () => dragEnd(true));
  addEventListener("keydown", (e) => { if (e.key === "Escape" && drag) dragEnd(false); });

  // ------------------------------------------------------------ redraw and flow
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  function stateSig() {
    const st = eng();
    return [S.version, S.take, S.path, liveMode(), JSON.stringify(armedMap()), st && st.device && st.device.name,
      st && st.inputs && (st.inputs.audio || []).join("|")].join("·");
  }
  function tick() {
    raf = requestAnimationFrame(tick);
    if (!on || drag) return;
    const s = stateSig(), w = root.querySelector(".cbScroll")?.clientWidth || 0;
    if (s !== sig || w !== lastW) {
      const sc = root.querySelector(".cbScroll"), top = sc ? sc.scrollTop : 0;
      sig = s; build(); lastW = root.querySelector(".cbScroll").clientWidth; root.querySelector(".cbScroll").scrollTop = top;
    }
    const flowing = liveMode() && isPlaying();
    root.classList.toggle("flowing", flowing);
    root.classList.toggle("still", reduced.matches);
    if (!flowing) return;
    for (const c of cables) if (c.flow) c.flow.style.opacity = c.live ? (0.12 + 0.88 * peakPct(c.peak()) / 100).toFixed(2) : 0;
  }
  function show() {
    root = root || document.getElementById("cables");
    if (!root) return;
    on = true; sig = ""; lastW = 0;
    if (!raf) raf = requestAnimationFrame(tick);
  }
  function hide() { on = false; dragEnd(false); if (raf) cancelAnimationFrame(raf); raf = 0; }
  return { show, hide };
})();
