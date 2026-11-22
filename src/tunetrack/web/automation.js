// Automation lanes on the arrangement: each track can open its lanes under its row (the "A" button on the track
// header). A lane is [[beat, value], ...] with straight lines between points, held flat before the first point and
// after the last (as the engine plays it). Fader lanes (volume_db, pan) are always offered; a device setting gets a
// lane once it has points or the user adds one. A gesture edits a copy of the lane and, when the button comes up,
// sends the whole lane in one automation(clear=True, points=...) call; nothing is sent while the pointer moves.
// Loaded after app.js and shares its globals (S, $, act, api, modal, menu, laneLayout, snapB, fmtPos, ...).
"use strict";

const Automation = (() => {
  const LANE_H = 40, ADD_H = 20, PAD = 5, HIT = 6;
  const SYNTH_SKIP = new Set(["freq", "gain", "gate"]);  // set per note, not settings (api._auto_target)

  // ---- what is open, per browser: {open: {trackId: 1}, extra: {trackId: ["dev|param", ...]}}
  let mem = { open: {}, extra: {} };
  try { mem = { open: {}, extra: {}, ...JSON.parse(localStorage.getItem("tt.auto") || "{}") }; } catch { /* no storage */ }
  const save = () => { try { localStorage.setItem("tt.auto", JSON.stringify(mem)); } catch { /* no storage */ } };
  const isOpen = (t) => !!mem.open[t.id];

  // ---- lanes of a track
  // dev: null (the fader), "instrument", or an effect index (as fx_update takes it)
  const keyOf = (dev, param) => `${dev == null ? "" : dev}|${param}`;
  const device = (t, dev) => (dev === "instrument" ? t.instrument : t.fx && t.fx[dev]) || null;
  function settings(d) {
    // the setting names a built-in device has (null: a plugin, whose names come from plugin_params)
    if (!d || !S.catalog) return null;
    if (d.type === "synth") return Object.keys((S.catalog.synths[d.name] || {}).params || {}).filter((k) => !SYNTH_SKIP.has(k));
    if (d.type === "builtin") return Object.keys((S.catalog.effects[d.name] || {}).params || {});
    return null;
  }
  function makeLane(t, dev, param) {
    if (dev == null) {
      return { key: keyOf(null, param), t, dev: null, param, kind: "fader", label: param === "pan" ? "Pan" : "Volume",
        pts: (t.automation || {})[param] || [], stat: param === "pan" ? t.pan : t.volume_db };
    }
    const d = device(t, dev);
    if (!d || d.type === "sfz") return null;
    const known = settings(d);
    if (known && !known.includes(param)) return null;  // the effect at that place changed under a remembered lane
    const C = S.catalog, def = !C ? null : d.type === "synth" ? (C.synths[d.name] || {}).params : d.type === "builtin" ? (C.effects[d.name] || {}).params : null;
    const sv = d.params && param in d.params ? d.params[param] : def ? def[param] : undefined;
    const where = dev === "instrument" ? "Inst" : `${dev + 1} ${d.name}`;
    return { key: keyOf(dev, param), t, dev, param, kind: d.type === "vst3" ? "vst3" : "builtin", label: `${where} · ${param}`,
      pts: (d.automation || {})[param] || [], stat: typeof sv === "number" ? sv : undefined };
  }
  function lanesOf(t) {
    const out = [makeLane(t, null, "volume_db"), makeLane(t, null, "pan")], seen = new Set(out.map((l) => l.key));
    const add = (dev, param) => {
      const k = keyOf(dev, param); if (seen.has(k)) return;
      const l = makeLane(t, dev, param); if (l) { out.push(l); seen.add(k); }
    };
    if (t.instrument) for (const p of Object.keys(t.instrument.automation || {})) add("instrument", p);
    (t.fx || []).forEach((f, i) => { for (const p of Object.keys(f.automation || {})) add(i, p); });
    for (const k of mem.extra[t.id] || []) {
      const [d, p] = k.split("|");
      add(d === "instrument" ? "instrument" : +d, p);
    }
    return out;
  }
  const hasAny = (t) => Object.keys(t.automation || {}).length || Object.keys((t.instrument && t.instrument.automation) || {}).length
    || (t.fx || []).some((f) => Object.keys(f.automation || {}).length);

  // ---- values
  function range(l, pts) {
    if (l.kind === "fader") return l.param === "pan" ? [-1, 1] : [-60, 12];
    if (l.kind === "vst3") return [0, 1];
    // built-ins carry defaults, not ranges: fit the points and the static value, with room to drag past them
    const vs = pts.map((p) => p[1]).filter((v) => typeof v === "number");
    if (typeof l.stat === "number") vs.push(l.stat);
    if (!vs.length) return [0, 1];
    let lo = Math.min(...vs), hi = Math.max(...vs);
    if (lo >= 0 && hi <= 1) return [0, 1];
    const pad = hi - lo > 1e-9 ? (hi - lo) * 0.25 : Math.max(1, Math.abs(hi) * 0.5);
    const floor0 = lo >= 0;
    lo -= pad; hi += pad;
    if (floor0) lo = Math.max(0, lo);
    return [lo, hi];
  }
  function fmt(l, v) {
    if (typeof v !== "number") return String(v);
    if (l.kind === "fader") {
      if (l.param === "pan") return Math.abs(v) < 0.005 ? "C" : `${v < 0 ? "L" : "R"}${Math.round(Math.abs(v) * 100)}`;
      return `${v > 0 ? "+" : ""}${v.toFixed(1)} dB`;
    }
    return l.kind === "vst3" ? v.toFixed(3) : String(+v.toPrecision(3));
  }

  // ---- gesture state
  // drag: {key, t, l, pts (a copy being edited), idx, bx, vx (unsnapped position/value), lx, ly (last pointer),
  // ox, oy (where it went down), moved, added, rng (the value range, frozen for the drag)}
  // pending: lanes sent but not yet reloaded, drawn as sent so the curve does not jump back meanwhile
  let drag = null, queue = Promise.resolve();
  const pending = new Map();
  const ptsOf = (l) => (drag && drag.key === l.key && drag.t.id === l.t.id ? drag.pts : pending.get(l.t.id + "/" + l.key) || l.pts);

  // where each open lane sits on the lanes canvas
  function places() {
    const lay = laneLayout(), out = [];
    (S.doc ? S.doc.tracks : []).forEach((t, i) => {
      if (!lay[i] || !lay[i].a) return;
      lanesOf(t).forEach((l, k) => out.push({ t, i, l, y0: lay[i].y + lay[i].h + k * LANE_H }));
    });
    return out;
  }
  function hitTest(e) {
    if (!S.doc) return null;
    const r = lanes.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top, lay = laneLayout();
    const i = lay.findIndex((q) => q.a && y >= q.y + q.h && y < q.y + q.h + q.a);
    if (i < 0) return null;
    const t = S.doc.tracks[i], k = Math.floor((y - lay[i].y - lay[i].h) / LANE_H), ls = lanesOf(t);
    return { t, i, x, y, l: ls[k] || null, y0: lay[i].y + lay[i].h + k * LANE_H };
  }
  const yOf = (v, y0, [lo, hi]) => y0 + PAD + (hi - (typeof v === "number" ? Math.min(hi, Math.max(lo, v)) : (lo + hi) / 2)) / (hi - lo) * (LANE_H - 2 * PAD);
  const vOf = (y, y0, [lo, hi]) => Math.min(hi, Math.max(lo, hi - (y - y0 - PAD) / (LANE_H - 2 * PAD) * (hi - lo)));
  function nearest(h, pts, rng) {
    let best = -1, bd = HIT * HIT;
    pts.forEach(([b, v], j) => {
      const dx = b * S.zoom - h.x, dy = yOf(v, h.y0, rng) - h.y, d = dx * dx + dy * dy;
      if (d <= bd) { bd = d; best = j; }
    });
    return best;
  }

  function commit(l, pts) {
    // one call per gesture: the whole lane, sorted, one point per beat (the dragged one wins a tie)
    const tid = l.t.id, pk = tid + "/" + l.key;
    const sorted = [...pts].sort((a, b) => a[0] - b[0]).filter((p, j, a) => j === a.length - 1 || Math.abs(a[j + 1][0] - p[0]) > 1e-6);
    const dp = l.kind === "fader" ? (l.param === "pan" ? 2 : 1) : 4;  // fader levels to 0.1 dB, pan to 0.01
    const points = sorted.map(([b, v]) => [+b.toFixed(4), typeof v === "number" ? +v.toFixed(dp) : v]);
    pending.set(pk, points);
    const args = { track: tid, param: l.param, clear: true, points };
    if (l.dev != null) args.device = l.dev;
    // queued so a quick second gesture is sent after the first has reloaded (it carries the new revision)
    queue = queue.then(() => act("automation", args)).catch(() => {}).then(() => { pending.delete(pk); drawArrangement(); });
    drawArrangement();
  }

  // ---- mouse (app.js hands every lanes-canvas event here first; true: it was on a lane and is handled)
  function mouse(e) {
    const h = hitTest(e);
    if (!h) return false;
    if (e.type === "contextmenu") { e.preventDefault(); context(e, h); return true; }
    if (e.type !== "mousedown" || e.button !== 0 || !h.l) return true;
    e.preventDefault();
    const l = h.l, pts = ptsOf(l).map((p) => [...p]), rng = range(l, pts);
    const j = nearest(h, pts, rng);
    if (j >= 0 && e.detail >= 2) { pts.splice(j, 1); commit(l, pts); return true; }  // double-click a point: gone
    if (j >= 0) drag = { key: l.key, t: h.t, l, pts, idx: j, bx: pts[j][0], vx: pts[j][1], lx: h.x, ly: h.y, ox: h.x, oy: h.y, moved: false, added: false, rng, y0: h.y0 };
    else {
      if (e.detail >= 2) return true;  // the second click of a double-click on empty lane: the first already added
      const b = Math.max(0, h.x / S.zoom), v = vOf(h.y, h.y0, rng);
      pts.push([e.altKey ? b : Math.max(0, snapB(b)), v]);
      drag = { key: l.key, t: h.t, l, pts, idx: pts.length - 1, bx: b, vx: v, lx: h.x, ly: h.y, ox: h.x, oy: h.y, moved: false, added: true, rng, y0: h.y0 };
    }
    drawArrangement();
    return true;
  }
  window.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const r = lanes.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top, d = drag;
    const fine = e.shiftKey ? 0.1 : 1, [lo, hi] = d.rng;
    if (!d.moved && Math.hypot(x - d.ox, y - d.oy) > 2) d.moved = true;
    // moved by the pointer's step, scaled down with Shift, so pressing Shift mid-drag does not jump
    d.bx = Math.max(0, d.bx + (x - d.lx) * fine / S.zoom);
    if (typeof d.vx === "number") d.vx = Math.min(hi, Math.max(lo, d.vx - (y - d.ly) * fine / (LANE_H - 2 * PAD) * (hi - lo)));
    d.lx = x; d.ly = y;
    d.pts[d.idx] = [e.altKey || e.shiftKey ? d.bx : Math.max(0, snapB(d.bx)), d.vx];
    status(`${d.l.label}: ${fmt(d.l, d.vx)} at ${fmtPos(d.pts[d.idx][0])}`);
    drawArrangement();
  });
  window.addEventListener("mouseup", () => {
    const d = drag; if (!d) return;
    drag = null; status("");
    if (d.moved || d.added) {
      // the dragged point replaces any other point it was dropped on
      const me = d.pts[d.idx], pts = d.pts.filter((p, j) => j === d.idx || Math.abs(p[0] - me[0]) > 1e-6);
      commit(d.l, pts.filter((p) => p !== me).concat([me]));
    } else drawArrangement();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && drag) { e.stopPropagation(); drag = null; status(""); drawArrangement(); }
  }, true);

  function context(e, h) {
    if (!h.l) return;
    const l = h.l, pts = ptsOf(l).map((p) => [...p]), j = nearest(h, pts, range(l, pts));
    if (j >= 0) { pts.splice(j, 1); commit(l, pts); return; }  // right-click a point: gone
    laneMenu(e, h.t, l);
  }
  function laneMenu(e, t, l) {
    const extra = mem.extra[t.id] || [];
    menu(e, [
      [l.label, null],
      ...(ptsOf(l).length ? [["Clear lane", () => commit(l, [])]] : []),
      ...(extra.includes(l.key) ? [["Hide lane", () => hide(t, l)]] : []),
    ]);
  }
  function hide(t, l) {
    mem.extra[t.id] = (mem.extra[t.id] || []).filter((k) => k !== l.key); save(); drawHeaders(); drawArrangement();
  }

  // ---- drawing (into the lanes canvas: it scrolls and zooms with the clips)
  function draw(g, lay, W) {
    for (const { t, i, l, y0 } of places()) {
      const pts = ptsOf(l), rng = d0(l, pts), col = trackColor(t, i);
      g.fillStyle = "rgba(0,0,0,.35)"; g.fillRect(0, y0, W, LANE_H);
      g.fillStyle = "#202022"; g.fillRect(0, y0 + LANE_H - 1, W, 1);
      // a reference line: 0 dB / centre pan
      if (l.kind === "fader") { g.fillStyle = "#26262a"; g.fillRect(0, Math.round(yOf(0, y0, rng)), W, 1); }
      if (!pts.length) {
        // no lane yet: the static value, dashed and dim
        if (typeof l.stat === "number") {
          g.strokeStyle = col + "66"; g.setLineDash([3, 4]); g.beginPath();
          const y = Math.round(yOf(l.stat, y0, rng)) + 0.5; g.moveTo(0, y); g.lineTo(W, y); g.stroke(); g.setLineDash([]);
        }
        continue;
      }
      const sorted = pts.map((p, j) => [p[0], p[1], j]).sort((a, b) => a[0] - b[0]);
      g.fillStyle = col + "22"; g.strokeStyle = col; g.lineWidth = 1.5; g.beginPath();
      g.moveTo(0, yOf(sorted[0][1], y0, rng));
      for (const [b, v] of sorted) g.lineTo(b * S.zoom, yOf(v, y0, rng));
      g.lineTo(W, yOf(sorted[sorted.length - 1][1], y0, rng));
      g.stroke();
      g.lineTo(W, y0 + LANE_H - 1); g.lineTo(0, y0 + LANE_H - 1); g.closePath(); g.fill();
      g.lineWidth = 1;
      const live = drag && drag.key === l.key && drag.t.id === t.id;
      for (const [b, v, j] of sorted) {
        const x = b * S.zoom, y = yOf(v, y0, rng), on = live && j === drag.idx;
        g.fillStyle = on ? "#ffffff" : typeof v === "number" ? col : "#8f897c";
        g.fillRect(Math.round(x) - 3, Math.round(y) - 3, 6, 6);
        g.strokeStyle = "#0c0c0d"; g.strokeRect(Math.round(x) - 3.5, Math.round(y) - 3.5, 7, 7);
        if (typeof v !== "number") { g.fillStyle = "#8f897c"; g.font = `9px ${MONO}`; g.fillText(v, x + 6, y - 4); }
      }
      if (live) {
        // the value under the pointer, beside the point being dragged
        const [b, v] = drag.pts[drag.idx], text = `${fmt(l, v)} · ${fmtPos(b)}`;
        g.font = `10px ${MONO}`;
        const w = g.measureText(text).width + 8, x = Math.min(b * S.zoom + 8, W - w), y = Math.max(y0 + 1, Math.min(yOf(v, y0, rng) - 8, y0 + LANE_H - 15));
        g.fillStyle = "#030d0b"; g.fillRect(x, y, w, 14); g.strokeStyle = "#1f4f47"; g.strokeRect(x + 0.5, y + 0.5, w - 1, 13);
        g.fillStyle = "#74f0d6"; g.fillText(text, x + 4, y + 10);
      }
    }
  }
  // the range a lane draws with: frozen for the lane being dragged, so the scale does not move under the pointer
  const d0 = (l, pts) => (drag && drag.key === l.key && drag.t.id === l.t.id ? drag.rng : range(l, pts));

  // ---- track header: the A button and the lane names under the header
  function bindHeader(el, t) {
    const btns = el.querySelector(".btns"); if (!btns) return;
    const b = document.createElement("button");
    b.className = "a" + (isOpen(t) ? " on" : "") + (hasAny(t) ? " has" : "");
    b.textContent = "A"; b.title = "Automation lanes" + (hasAny(t) ? " (this track has automation)" : "");
    b.onclick = (e) => {
      e.stopPropagation();
      if (isOpen(t)) delete mem.open[t.id]; else mem.open[t.id] = 1;
      save(); drawHeaders(); drawArrangement();
    };
    btns.appendChild(b);
  }
  function header(t, lay) {
    const box = document.createElement("div");
    box.className = "ah"; box.style.height = lay.a + "px";
    for (const l of lanesOf(t)) {
      const row = document.createElement("div"), n = ptsOf(l).length;
      row.className = "al"; row.style.height = LANE_H + "px";
      row.innerHTML = `<span class="aln" title="${esc(l.label)}">${esc(l.label)}</span><span class="alc">${n ? n + " pt" : typeof l.stat === "number" ? esc(fmt(l, l.stat)) : ""}</span>`;
      row.oncontextmenu = (e) => { e.preventDefault(); laneMenu(e, t, l); };
      box.appendChild(row);
    }
    const add = document.createElement("div");
    add.className = "aadd"; add.style.height = ADD_H + "px";
    add.innerHTML = `<button class="small" title="Automate a setting of this track's instrument or effects">+ lane</button>`;
    add.querySelector("button").onclick = () => addLane(t);
    box.appendChild(add);
    return box;
  }
  // built-in setting names and defaults come from the synth/effect catalog: fetched once when a lane first shows
  let asked = false;
  function height(t) {
    if (!isOpen(t)) return 0;
    if (!S.catalog && !asked) { asked = true; ensureLibrary().then(() => { drawHeaders(); drawArrangement(); }).catch(() => {}); }
    return lanesOf(t).length * LANE_H + ADD_H;
  }

  // ---- adding a lane: the track's devices and their settings
  async function addLane(t) {
    try { await ensureLibrary(); } catch { /* the built-in names are missing; plugins can still be typed */ }
    const shown = new Set(lanesOf(t).map((l) => l.key));
    const devs = [];
    if (t.instrument && t.instrument.type !== "sfz") devs.push(["instrument", t.instrument, "Instrument"]);
    (t.fx || []).forEach((f, i) => devs.push([i, f, `Effect ${i + 1}`]));
    const sect = ([dev, d, what]) => {
      const names = settings(d);
      const head = `<h3>${esc(what)}: ${esc(d.name)} <span class="muted">${esc(d.type)}</span></h3>`;
      if (names) {
        const chips = names.map((p) => `<button class="small" data-dev="${dev}" data-p="${esc(p)}" ${shown.has(keyOf(dev, p)) ? "disabled" : ""}>${esc(p)}</button>`).join(" ");
        return `<div class="card">${head}<div class="achips">${chips || `<span class="muted">no settings</span>`}</div></div>`;
      }
      return `<div class="card">${head}<div class="arow"><input data-q="${dev}" placeholder="filter, then Enter (loads the plugin)"><button class="small" data-load="${dev}">List settings</button></div>
        <div class="achips" data-list="${dev}"></div>
        <div class="arow"><input data-type="${dev}" placeholder="or type a setting name exactly as the plugin shows it"><button class="small" data-typed="${dev}">Add</button></div></div>`;
    };
    const box = modal(`<h2>Add automation lane · ${esc(t.name)}</h2>
      ${devs.length ? devs.map(sect).join("") : `<p class="hint">This track has no instrument or effect settings to automate. Volume and pan lanes are always there.</p>`}
      <div class="row"><button class="ghost" id="alClose">Close</button></div>`);
    const devOf = (s) => (s === "instrument" ? "instrument" : +s);
    const pick = (dev, p) => {
      const k = keyOf(dev, p), ex = mem.extra[t.id] || [];
      if (!ex.includes(k)) mem.extra[t.id] = [...ex, k];
      mem.open[t.id] = 1; save(); closeModal(); drawHeaders(); drawArrangement();
    };
    $("#alClose").onclick = closeModal;
    const bindChips = (root) => root.querySelectorAll("[data-p]").forEach((b) => b.onclick = () => pick(devOf(b.dataset.dev), b.dataset.p));
    bindChips(box);
    box.querySelectorAll("[data-typed]").forEach((b) => b.onclick = () => {
      const v = box.querySelector(`[data-type="${b.dataset.typed}"]`).value.trim(); if (v) pick(devOf(b.dataset.typed), v);
    });
    const list = async (s) => {
      const out = box.querySelector(`[data-list="${s}"]`), dev = devOf(s), d = device(t, dev);
      const q = box.querySelector(`[data-q="${s}"]`).value.trim();
      out.innerHTML = `<span class="muted">loading the plugin…</span>`;
      try {
        const r = await api("plugin_params", { device: devString(d), query: q || null, limit: 120 });
        out.innerHTML = r.params.map((p) => `<button class="small" data-dev="${s}" data-p="${esc(p.name)}" title="${esc(p.text)}" ${shown.has(keyOf(dev, p.name)) ? "disabled" : ""}>${esc(p.name)}</button>`).join(" ")
          + `<div class="muted">${r.total} settings${r.total > r.params.length ? " (filter to see more)" : ""}</div>`;
        bindChips(out);
      } catch (e) { out.textContent = e.message; }
    };
    box.querySelectorAll("[data-load]").forEach((b) => b.onclick = () => list(b.dataset.load));
    box.querySelectorAll("[data-q]").forEach((i) => i.onkeydown = (e) => { if (e.key === "Enter") list(i.dataset.q); });
  }

  return { height, header, bindHeader, draw, mouse };
})();
window.Automation = Automation;
