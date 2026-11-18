// Sheets: full-height panels docked to the right edge (or over the whole room) for anything you browse or choose
// from: projects, instruments and effects, who sits in a seat. They replace small centred dialogs. A sheet looks
// like the GEAR drawer (a face plate on top, a dark well under it) and closes with Esc, × or a click beside it.
// pickList() is the list inside most of them: facets down the left, a search you can walk with the arrow keys,
// the highlighted item's detail on the right; Enter or a double-click picks.
// Loaded after app.js and uses its globals ($, esc).
"use strict";
const Sheet = (() => {
  let cur = null;
  // size: "" (760px), "wide" (1120px) or "full" (the whole room, right of the rail)
  function open({ title, sub = "", size = "", body = "", foot = "", onClose = null }) {
    close(true);
    const scrim = document.createElement("div"); scrim.className = "shScrim";
    const el = document.createElement("section");
    el.className = `sheet ${size}`; el.setAttribute("role", "dialog"); el.setAttribute("aria-modal", "true"); el.setAttribute("aria-label", title);
    el.innerHTML = `<header class="gFace shFace"><span class="screw"></span>
        <div class="gIdent"><b>${esc(title)}</b>${sub ? `<span class="legend shSub">${sub}</span>` : ""}</div>
        <button class="mini shX" title="Close (Esc)">ESC ×</button><span class="screw"></span></header>
      <div class="shBody">${body}</div>${foot ? `<footer class="shFoot">${foot}</footer>` : ""}`;
    document.body.append(scrim, el);
    cur = { el, scrim, onClose, back: document.activeElement };
    scrim.addEventListener("mousedown", () => close());
    el.querySelector(".shX").onclick = () => close();
    return el;
  }
  // quiet: closed because the sheet did its job (or another opens), so onClose (the "cancelled" path) does not run
  function close(quiet = false) {
    if (!cur) return;
    const c = cur; cur = null;
    c.el.remove(); c.scrim.remove();
    if (c.el._stop) c.el._stop();
    if (!quiet && c.onClose) c.onClose();
    if (c.back && c.back.focus && document.contains(c.back)) c.back.focus();
  }
  // Esc: a search with text in it is cleared first; then the sheet closes. A dialog over the sheet goes first.
  addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !cur || !$("#modal").classList.contains("hidden")) return;
    const t = e.target;
    e.preventDefault(); e.stopPropagation();
    if (t && t.classList && t.classList.contains("shQ") && t.value) { t.value = ""; t.dispatchEvent(new Event("input")); return; }
    close();
  }, true);
  return { open, close, get el() { return cur && cur.el; } };
})();

// items: [{id, name, hay (lower-case search text), f: {facetKey: value}, ...}]
// opts: groups [{key, label}], cols (grid-template-columns), head [labels], row(item) -> html, detail(item, el),
// onPick(item), current (an id to start on), placeholder, empty (html when nothing matches), sort(a, b)
function pickList(root, opts) {
  const P = { q: "", f: {}, hi: 0, shown: [] };
  root.innerHTML = `<div class="pl ${opts.groups && opts.groups.length ? "" : "noFacets"}">
    ${opts.groups && opts.groups.length ? `<nav class="plFacets"></nav>` : ""}
    <div class="plMain">
      <div class="plTools"><input class="shQ" type="search" spellcheck="false" autocomplete="off" placeholder="${esc(opts.placeholder || "SEARCH")}">
        <span class="plCount"></span></div>
      ${opts.head ? `<div class="plHead" style="grid-template-columns:${opts.cols}">${opts.head.map((h) => `<span>${esc(h)}</span>`).join("")}</div>` : ""}
      <div class="plRows" role="listbox" tabindex="-1"></div>
    </div>
    <aside class="plDetail"></aside></div>`;
  const q = root.querySelector(".shQ"), rows = root.querySelector(".plRows"), det = root.querySelector(".plDetail"), nav = root.querySelector(".plFacets");
  const words = () => P.q.toLowerCase().split(/\s+/).filter(Boolean);
  const passes = (it, skip) => words().every((w) => it.hay.includes(w))
    && Object.entries(P.f).every(([k, v]) => k === skip || v == null || it.f[k] === v);
  function facets() {
    if (!nav) return;
    nav.innerHTML = opts.groups.map((g) => {
      const n = new Map();
      for (const it of opts.items) if (passes(it, g.key) && it.f[g.key] != null) n.set(it.f[g.key], (n.get(it.f[g.key]) || 0) + 1);
      const vals = [...n.keys()].sort(g.order ? (a, b) => g.order.indexOf(a) - g.order.indexOf(b) : (a, b) => String(a).localeCompare(String(b)));
      if (vals.length < 2 && P.f[g.key] == null) return "";
      const all = [...n.values()].reduce((a, b) => a + b, 0);
      return `<h6>${esc(g.label)}</h6>
        <button data-f="${g.key}" data-v="" class="${P.f[g.key] == null ? "on" : ""}"><span>All</span><i>${all}</i></button>
        ${vals.map((v) => `<button data-f="${g.key}" data-v="${esc(v)}" class="${P.f[g.key] === v ? "on" : ""}" title="${esc(v)}"><span>${esc(v)}</span><i>${n.get(v)}</i></button>`).join("")}`;
    }).join("");
    nav.querySelectorAll("button").forEach((b) => b.onclick = () => {
      P.f[b.dataset.f] = b.dataset.v === "" || P.f[b.dataset.f] === b.dataset.v ? null : b.dataset.v;
      draw(); q.focus();
    });
  }
  function draw(keep) {
    const id = keep || (P.shown[P.hi] || {}).id;
    P.shown = opts.items.filter((it) => passes(it));
    if (opts.sort) P.shown.sort(opts.sort);
    const at = P.shown.findIndex((it) => it.id === id);
    P.hi = at >= 0 ? at : 0;
    rows.innerHTML = P.shown.length ? P.shown.map((it, i) => `<div class="plRow ${it.dim ? "dim" : ""} ${it.id === opts.current ? "cur" : ""}" role="option" title="${esc(opts.tip || "")}" data-i="${i}" style="grid-template-columns:${opts.cols}">${opts.row(it)}</div>`).join("")
      : `<div class="plEmpty">${opts.empty || "Nothing matches."}</div>`;
    root.querySelector(".plCount").textContent = `${P.shown.length} / ${opts.items.length}`;
    facets(); mark();
  }
  function mark(scroll = true) {
    rows.querySelectorAll(".plRow.hi").forEach((r) => { r.classList.remove("hi"); r.removeAttribute("aria-selected"); });
    const r = rows.querySelector(`.plRow[data-i="${P.hi}"]`), it = P.shown[P.hi];
    if (r) { r.classList.add("hi"); r.setAttribute("aria-selected", "true"); if (scroll) r.scrollIntoView({ block: "nearest" }); }
    if (it) opts.detail(it, det); else det.innerHTML = `<div class="plNone">${opts.none || ""}</div>`;
  }
  rows.addEventListener("mousedown", (e) => {
    const r = e.target.closest(".plRow"); if (!r) return;
    e.preventDefault(); P.hi = +r.dataset.i; mark(false); q.focus();
  });
  rows.addEventListener("dblclick", (e) => { const r = e.target.closest(".plRow"); if (r) opts.onPick(P.shown[+r.dataset.i]); });
  q.addEventListener("input", () => { P.q = q.value; draw(); });
  q.addEventListener("keydown", (e) => {
    const step = { ArrowDown: 1, ArrowUp: -1, PageDown: 10, PageUp: -10 }[e.key];
    if (step && P.shown.length) { e.preventDefault(); P.hi = Math.max(0, Math.min(P.shown.length - 1, P.hi + step)); mark(); }
    else if (e.key === "Enter" && P.shown[P.hi]) { e.preventDefault(); opts.onPick(P.shown[P.hi]); }
  });
  draw(opts.current);
  setTimeout(() => q.focus(), 0);
  return { redraw: () => draw(), get item() { return P.shown[P.hi]; } };
}

// "3 min ago", "yesterday", "12 Nov", "12 Nov 2025"
function ago(t) {
  const s = Date.now() / 1000 - t, d = new Date(t * 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 172800) return "yesterday";
  return d.toLocaleDateString([], { day: "numeric", month: "short", ...(d.getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}) });
}
