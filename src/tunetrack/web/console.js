// The mixing console: one component for every place a mix is shown. The MIX room (full height, the effect's
// settings in a panel on the right), the Desk's Mixer tab (compact: levels, pan, mute and solo) and the Mix seat's
// bench in the Studio (what the seat's take moved, next to where it was). It draws what seat_bench("mix") answers
// and every control edits through the API, one undo step per gesture.
// Controls: drag a fader or the pan knob (relative: a stray click moves nothing; Shift is fine), double-click to
// reset, click a readout to type a value, arrow keys nudge. Click an insert for its settings, its lamp switches it
// in or out, x removes it. While LIVE plays, each strip's stereo meter shows the engine's peaks with a peak hold and
// a clip lamp (click it to clear); otherwise the last render's level and peak.
"use strict";
const TTConsole = (() => {
  const faderPos = (db) => { const v = Math.max(-60, Math.min(12, +db || 0)); return v >= -20 ? 30 + (v + 20) / 32 * 70 : (v + 60) / 40 * 30; };  // % up the slot
  const dbAt = (pct) => pct >= 30 ? -20 + (pct - 30) / 70 * 32 : -60 + pct / 30 * 40;  // faderPos, the other way
  const panName = (p) => Math.abs(p) < 0.005 ? "C" : (p < 0 ? "L" : "R") + Math.round(Math.abs(p) * 100);
  const dbName = (v) => (v > 0 ? "+" : "") + (+v).toFixed(1);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v)), step = (v, by) => Math.round(v / by) * by;
  const SCALE = [12, 6, 0, -6, -12, -20, -30, -40, -60];
  const mk = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
  const KIND = { midi: "INST", audio: "AUDIO", bus: "BUS" };

  // built-in effect settings: where a value sits along its slider (log for frequencies), and how it reads
  const knobPos = (k, v) => k.log ? Math.log(v / k.lo) / Math.log(k.hi / k.lo) : (v - k.lo) / (k.hi - k.lo);
  const knobAt = (k, x) => { const v = k.log ? k.lo * Math.pow(k.hi / k.lo, x) : k.lo + x * (k.hi - k.lo); return +(Math.round(v / k.step) * k.step).toFixed(6); };
  const knobName = (k, v) => {
    if (k.unit === "LP/HP") return v >= 0.5 ? "HP" : "LP";
    if (k.unit === "Hz" && v >= 1000) return `${+(v / 1000).toFixed(2)} kHz`;
    const dp = k.step >= 1 ? 0 : k.step >= 0.1 ? 1 : k.step >= 0.01 ? 2 : 4;
    return `${(+v).toFixed(dp)}${k.unit ? (k.unit === ":1" ? ":1" : " " + k.unit) : ""}`;
  };
  const PLUGIN_PARAMS = new Map();  // device -> promise of plugin_params: a plugin loads once per page, then its list is filtered here
  const pluginParams = (dev) => {
    if (!PLUGIN_PARAMS.has(dev)) PLUGIN_PARAMS.set(dev, api("plugin_params", { device: dev, limit: 5000 }).catch((e) => { PLUGIN_PARAMS.delete(dev); throw e; }));
    return PLUGIN_PARAMS.get(dev);
  };

  // the pan knob: a 270 degree sweep, the lit arc from the centre to where it points
  const pt = (deg, r) => { const t = deg * Math.PI / 180; return `${(18 + r * Math.sin(t)).toFixed(2)} ${(18 - r * Math.cos(t)).toFixed(2)}`; };
  const arc = (a0, a1, r) => `M${pt(a0, r)}A${r} ${r} 0 ${Math.abs(a1 - a0) > 180 ? 1 : 0} ${a1 > a0 ? 1 : 0} ${pt(a1, r)}`;
  const knobSvg = (p, ghost) => {
    const a = clamp(p, -1, 1) * 135;
    return `<svg viewBox="0 0 36 36" aria-hidden="true"><path class="kt" d="${arc(-135, 135, 15)}"/>${ghost != null && Math.abs(ghost) > 0.004 ? `<path class="kg" d="${arc(0, ghost * 135, 15)}"/>` : ""}${Math.abs(a) > 0.6 ? `<path class="kv" d="${arc(0, a, 15)}"/>` : ""}<circle class="kb" cx="18" cy="18" r="10.5"/><path class="kp" d="M${pt(a, 3.5)}L${pt(a, 9)}"/></svg>`;
  };

  // ---------------------------------------------------------------- meters
  // Live: the engine's peak per channel (a number, or one per channel), with a fall, a peak hold and a clip lamp.
  // Without LIVE: the last render's level (rms) as the bar and its peak as the hold line.
  const MS = new Map();
  const levelPos = (lin) => lin > 0 ? faderPos(20 * Math.log10(lin)) : 0;
  function meters() {
    const st = (typeof S !== "undefined" && S.eng.st) || {}, on = liveMode() && !!st.on && !st.audition;  // a gear audition is not this mix
    const now = performance.now();
    document.querySelectorAll(".console").forEach((c) => {
      if (!c.offsetParent) return;
      c.classList.toggle("live", on);
      c.querySelectorAll(".mtr[data-m]").forEach((m) => {
        const id = m.dataset.m, bars = m.querySelectorAll(".mb");
        let s = MS.get(id); if (!s) MS.set(id, s = { v: [0, 0], h: [0, 0], ht: [0, 0], at: now, clip: false });
        const dt = Math.min(0.25, (now - s.at) / 1000); s.at = now;
        let want;
        if (on) {
          const pk = st.peaks ? st.peaks[id] : 0, arr = Array.isArray(pk) ? pk : [pk, pk];
          want = [arr[0] || 0, arr.length > 1 ? arr[1] || 0 : arr[0] || 0];
          if (want.some((x) => x >= 0.999)) s.clip = true;
          want = want.map(levelPos);
          for (let k = 0; k < 2; k++) {
            s.v[k] = Math.max(want[k], s.v[k] - dt * 55);
            if (s.v[k] >= s.h[k]) { s.h[k] = s.v[k]; s.ht[k] = now; } else if (now - s.ht[k] > 1400) s.h[k] = Math.max(s.v[k], s.h[k] - dt * 35);
          }
        } else {
          const r = +m.dataset.rms || 0, p = +m.dataset.pk || 0;
          s.v = [r, r]; s.h = [p, p];
        }
        bars.forEach((b, k) => { b.firstElementChild.style.height = (100 - s.v[k]) + "%"; b.lastElementChild.style.bottom = s.h[k] + "%"; b.lastElementChild.style.opacity = s.h[k] > 0.5 ? 1 : 0; });
        const lamp = m.querySelector(".clip"); if (lamp) lamp.classList.toggle("on", s.clip);
        const pr = m.closest(".ch")?.querySelector(".pk");
        if (pr && on) { const pk = Math.max(...s.h); pr.textContent = pk > 0.5 ? dbName(+dbAt(pk).toFixed(1)) : "−∞"; }
      });
    });
  }
  if (typeof liveES !== "undefined") liveES.addEventListener("message", () => meters());

  // ---------------------------------------------------------------- drawing and wiring
  // host: { take (where edits go: null = the session), compact, fill, hide: {ins, snd, io}, narrow,
  //         reload() (read again and redraw), redraw() (redraw what is held), active(), edited(), select(id), toMix(id), selected }
  function render(box, d, B, host) {
    const r = d.render, was = (t, k) => t.was && k in t.was, on = host.take || null, hide = host.hide || {};
    const tracks = d.tracks.map((t, i) => ({ t, i })), buses = d.tracks.filter((t) => t.kind === "bus");
    // what feeds a bus: the strips sending to it or routed into it
    const feeds = (b) => { const n = d.tracks.filter((x) => x.id !== b.id && ((x.output_id || x.output) === b.id || x.sends.some((s) => (s.id || s.to) === b.id || s.to === b.name))).length; return n ? `${n} strip${n === 1 ? "" : "s"}` : "nothing yet"; };
    // the insert and send sections are as tall on every strip, so the faders line up across the console
    const insRows = clamp(Math.max(0, ...d.tracks.map((t) => t.fx.length), d.master.fx.length) + 1, 2, 7);
    const sndRows = clamp(Math.max(0, ...d.tracks.filter((t) => t.kind !== "bus").map((t) => t.sends.length)) + 1, 1, 5);
    const sendsTo = (t, b) => t.sends.some((x) => x.id ? x.id === b.id : x.to === b.name);  // a bench read before sends carried ids has names only
    const rmKey = (attr, k, what) => `<button class="rm" ${attr}="${k}" title="Remove ${what}" aria-label="Remove ${what}">×</button>`;
    const isOpen = (chId, k) => !!B.fxOpen && B.fxOpen.ch === chId && B.fxOpen.k === k;
    const inserts = (list, fresh, chId) => list.map((f, k) => `<span class="slotRow ${f.bypass ? "byp" : ""}">
        <button class="pw" data-byp="${k}" aria-pressed="${!f.bypass}" title="${f.bypass ? "Bypassed: click to switch it in" : "In: click to bypass it"}"></button><button class="slot ${fresh && fresh(f) ? "new" : ""} ${isOpen(chId, k) ? "open" : ""}" data-fx="${k}" aria-expanded="${isOpen(chId, k)}" title="${esc(f.label)}${f.bypass ? " (bypassed)" : ""}${f.auto ? `. Automated: ${esc(f.auto.join(", "))}` : ""}. Click for its settings.">${esc(f.label)}${f.auto ? "<i>~</i>" : ""}</button>${rmKey("data-fxrm", k, esc(f.label))}</span>`).join("");
    const fader = (name, db, ghost, lo) => `<div class="scale" aria-hidden="true">${SCALE.map((v) => `<span style="bottom:${faderPos(v)}%">${v > 0 ? "+" + v : v}</span>`).join("")}</div>
      <div class="slotway" data-lo="${lo}" title="Drag to set the level (Shift: fine), double-click for 0 dB.">${SCALE.map((v) => `<b style="bottom:${faderPos(v)}%"></b>`).join("")}${ghost != null ? `<i class="cap ghost" style="bottom:${faderPos(ghost)}%"></i>` : ""}<i class="cap now" role="slider" tabindex="0" aria-label="${esc(name)} level" aria-valuemin="${lo}" aria-valuemax="12" aria-valuenow="${+db || 0}" style="bottom:${faderPos(db)}%"></i></div>`;
    const meter = (id, lv) => `<div class="mtr" data-m="${esc(id)}" data-rms="${lv && lv.rms_dbfs != null ? faderPos(lv.rms_dbfs) : 0}" data-pk="${lv && lv.peak_dbfs != null ? faderPos(lv.peak_dbfs) : 0}" title="${lv ? `Last render: peak ${lv.peak_dbfs} dBFS, rms ${lv.rms_dbfs} dBFS. ` : ""}While LIVE plays: the live peak. The lamp lights at 0 dBFS: click it to clear.">
        <button class="clip" data-clip tabindex="-1" aria-label="Clip lamp"></button><div class="mb"><i></i><u></u></div><div class="mb"><i></i><u></u></div></div>`;
    const strip = ({ t, i }) => {
      const moved = [], w = t.was || {};
      if ("volume_db" in w) moved.push(`was ${dbName(w.volume_db)}`);
      if ("pan" in w) moved.push(`pan was ${panName(w.pan)}`);
      if ("mute" in w) moved.push(w.mute ? "was muted" : "was unmuted");
      if ("solo" in w) moved.push(w.solo ? "was solo" : "solo now");
      const chain = (l) => l.map((f) => f.label).join(", ");
      if ("fx" in w) moved.push(chain(w.fx) !== chain(t.fx) ? `fx was ${chain(w.fx) || "none"}` : w.fx.some((f, k) => !f.bypass !== !t.fx[k].bypass) ? "fx switched in or out" : "fx settings changed");
      if ("sends" in w) moved.push("sends changed"); if ("output" in w) moved.push(`out was ${w.output}`); if ("automation" in w) moved.push("automation changed");
      if (t.new) moved.push("new track");
      const oldFx = new Set(((w.fx || t.fx).map((f) => f.label)));
      const free = buses.filter((b) => b.id !== t.id && !sendsTo(t, b)), isBus = t.kind === "bus";
      const inst = t.instrument ? String(t.instrument).replace(/^(synth|vst3|patch|sfz):/, "").split(/[\\/]/).pop().replace(/\.sfz$/i, "") : isBus ? (t.fx[0] ? t.fx[0].label : "shared effects") : t.kind;
      return `<div class="ch ${isBus ? "bus" : ""} ${moved.length ? "moved" : ""} ${t.mute ? "muted" : ""} ${host.selected === t.id ? "sel" : ""}" style="--c:${trackColor(t, i)}" data-ch="${esc(t.id)}">
        <div class="chTop"><span class="kind">${KIND[t.kind] || ""}${t.automation.length ? ` <b class="au" title="Automated: ${esc(t.automation.join(", "))}">AUTO</b>` : ""}</span><span class="inst" title="${esc(inst)}">${esc(inst)}</span></div>
        ${host.compact ? `<button class="sum" data-tomix title="Open this strip in the MIX room">${t.fx.length} FX${isBus ? "" : ` · ${t.sends.length} SND`}${t.output !== "master" ? ` · → ${esc(t.output)}` : ""}</button>` : `
        <div class="sec ins ${hide.ins ? "hidden" : ""}"><div class="secH">INSERTS</div><div class="slots">${inserts(t.fx, (f) => was(t, "fx") && !oldFx.has(f.label), t.id)}<button class="slot add" data-add="fx" title="Add an effect to the end of this chain">+</button></div></div>
        ${isBus ? `<div class="sec snd ${hide.snd ? "hidden" : ""}"><div class="secH">SENDS</div><div class="slots"><span class="none">fed by ${feeds(t)}</span></div></div>` : `<div class="sec snd ${hide.snd ? "hidden" : ""}"><div class="secH">SENDS</div><div class="slots">${t.sends.map((x, k) => `<span class="slotRow"><button class="slot send" data-send="${k}" title="Send to ${esc(x.to)} at ${dbName(x.db)} dB. Click to type a level."><span>${esc(x.to)}</span><b>${dbName(x.db)}</b><i style="width:${faderPos(x.db)}%"></i></button>${rmKey("data-sendrm", k, `the send to ${esc(x.to)}`)}</span>`).join("")}<button class="slot add" data-add="send" ${free.length ? "" : "disabled"} title="${free.length ? "Send to a bus" : buses.length ? "Already sends to every bus" : "No buses yet: add one on the Desk"}">+</button></div></div>`}
        <div class="sec io ${hide.io ? "hidden" : ""}"><button class="slot out" data-add="out" title="Output: ${esc(t.output)}. Click to route it."><span>→</span>${esc(t.output === "master" ? "Master" : t.output)}</button></div>`}
        <div class="pan" role="slider" tabindex="0" aria-label="${esc(t.name)} pan" aria-valuemin="-1" aria-valuemax="1" aria-valuenow="${t.pan}" title="Pan ${panName(t.pan)}. Drag up or right to move it (Shift: fine), double-click to centre.">${knobSvg(t.pan, was(t, "pan") ? w.pan : null)}<span class="pv">${panName(t.pan)}</span></div>
        <div class="fad">${fader(t.name, t.volume_db, was(t, "volume_db") ? w.volume_db : null, -60)}${meter(t.id, t.level)}</div>
        <div class="reads"><button class="db" title="Fader level. Click to type a level in dB">${dbName(t.volume_db)}</button><span class="pk" title="Peak">${t.level && t.level.peak_dbfs != null ? dbName(t.level.peak_dbfs) : "−∞"}</span></div>
        <div class="ms"><button class="m ${t.mute ? "on" : ""}" aria-pressed="${!!t.mute}" title="Mute">M</button><button class="s ${t.solo ? "on" : ""}" aria-pressed="${!!t.solo}" title="Solo">S</button></div>
        ${moved.length ? `<div class="was" title="${esc(moved.join(" · "))}">${esc(moved[0])}${moved.length > 1 ? ` +${moved.length - 1}` : ""}</div>` : ""}
        <div class="plate" title="${esc(t.name)} · ${esc(inst)}${host.select ? ". Click to select it on the Desk" : ""}">${esc(t.name)}</div></div>`;
    };
    const m = d.master, mw = m.was || {}, bands = r ? Object.entries(r.bands_pct) : [], top = Math.max(1, ...bands.map((b) => b[1]));
    const masterStrip = () => `<div class="ch master ${m.was ? "moved" : ""}" data-ch="master" style="--c:var(--amber)">
        <div class="chTop"><span class="kind">MAIN</span><span class="inst">target ${esc(m.target_lufs)} LUFS · ceil ${esc(m.ceiling_db)}</span></div>
        ${host.compact ? `<button class="sum" data-tomix title="Open the master in the MIX room">${m.fx.length} FX</button>` : `
        <div class="sec ins ${hide.ins ? "hidden" : ""}"><div class="secH">INSERTS</div><div class="slots">${inserts(m.fx, null, "master")}<button class="slot add" data-add="fx" title="Add an effect to the master chain">+</button></div></div>
        <div class="bands" title="Band balance of the last render">${bands.map(([k, v]) => `<div title="${esc(k.replace("_", " "))}: ${v}%"><i style="height:${v / top * 100}%"></i><span>${esc(k.replace("_", " ").replace(/^(\w)\w*\s?(\w?).*/, "$1$2"))}</span></div>`).join("") || `<span class="meta">not rendered yet</span>`}</div>`}
        <div class="fad">${fader("Master", m.volume_db || 0, "volume_db" in mw ? mw.volume_db || 0 : null, -24)}${meter("master", r && r.lufs != null ? { rms_dbfs: r.lufs, peak_dbfs: r.true_peak_dbtp } : null)}</div>
        <div class="reads"><button class="db" title="Master level. Click to type a level in dB">${dbName(m.volume_db || 0)}</button><span class="pk" title="Peak">−∞</span></div>
        <div class="loud"><span title="Loudness of the last render">${r && r.lufs != null ? `${r.lufs} <i>LUFS</i>` : "— <i>LUFS</i>"}</span><span title="True peak of the last render">${r && r.true_peak_dbtp != null ? `${r.true_peak_dbtp} <i>dBTP</i>` : "— <i>dBTP</i>"}</span></div>
        <div class="was ${r && (r.stale || r.warnings.length) ? "warn" : ""}">${esc([r && r.stale ? "render is older than the mix" : "", r ? (r.warnings.length ? r.warnings.length + " warning" + (r.warnings.length === 1 ? "" : "s") : "") : "not rendered", "volume_db" in mw ? `level was ${dbName(mw.volume_db || 0)}` : "", Object.keys(mw).some((k) => k !== "volume_db") ? "master changed" : ""].filter(Boolean).join(" · "))}</div>
        <div class="plate">MASTER</div></div>`;
    // The open effect's settings. They stay open through redraws while that effect is still in its place.
    const chainOf = (chId) => chId === "master" ? m.fx : (d.tracks.find((x) => x.id === chId) || {}).fx;
    const opened = (() => {
      const o = B.fxOpen, f = o && (chainOf(o.ch) || [])[o.k];
      if (!f || f.label !== o.label) { B.fxOpen = null; return null; }
      return f;
    })();
    const fxPanel = () => {
      if (!opened || host.compact) return "";
      const f = opened, o = B.fxOpen, ti = d.tracks.findIndex((x) => x.id === o.ch), who = ti < 0 ? "Master" : d.tracks[ti].name;
      const auto = new Set(f.auto || []);
      const body = f.type === "builtin" ? (f.settings || []).map((k) => {
        const v = typeof k.value === "number" ? k.value : k.default;
        return `<div class="fxk ${auto.has(k.name) ? "auto" : ""}" title="${esc(k.name)}${auto.has(k.name) ? ": automated, so the lane decides what plays and this value is used only where the lane has no points" : ""}. Drag (arrow keys nudge), double-click for the default (${esc(knobName(k, k.default))}).">
          <span class="kn">${esc(k.name.replace(/_/g, " "))}${auto.has(k.name) ? " ~" : ""}</span>
          <input type="range" min="0" max="1" step="0.001" value="${Math.max(0, Math.min(1, knobPos(k, v)))}" data-k="${esc(k.name)}" aria-label="${esc(k.name)}" aria-valuetext="${esc(knobName(k, v))}">
          <button class="v" title="Click to type a value">${esc(knobName(k, v))}</button></div>`;
      }).join("") || `<span class="meta">This effect has no settings.</span>`
        : f.type === "vst3" ? `<input class="fxq" type="search" placeholder="Search the settings…" value="${esc(B.fxQuery || "")}" aria-label="Search the plugin's settings" hidden><div class="fxks"><span class="meta">Loading the plugin's settings…</span></div>`
        : `<span class="meta">${esc(f.type)} effects have no settings here.</span>`;
      return `<div class="fxPanel" data-fxp="${esc(f.device)}" style="--c:${ti < 0 ? "var(--amber)" : trackColor(d.tracks[ti], ti)}">
        <div class="fxHead"><span class="nm">${esc(who)} · ${esc(f.label)}</span><span class="meta">${f.type === "vst3" ? "plugin" : "built-in"}${f.bypass ? " · bypassed" : ""}</span><span class="spacer"></span>
          <button class="mini ${f.bypass ? "" : "on"}" data-fxp-byp aria-pressed="${!f.bypass}" title="${f.bypass ? "Bypassed: click to switch it in" : "In: click to bypass it"}">${f.bypass ? "BYPASSED" : "IN"}</button>
          ${f.type === "vst3" ? `<button class="mini" data-fxp-win title="Open this plugin's window in the live engine" ${liveMode() ? "" : "hidden"}>WINDOW</button>` : ""}
          <button class="mini" data-fxp-rm title="Remove this effect">REMOVE</button><button class="mini" data-fxp-x title="Close the settings" aria-label="Close">×</button></div>
        <div class="fxBody">${body}</div></div>`;
    };
    const cls = ["console", host.compact ? "compact" : "", host.narrow ? "narrow" : "", host.fill ? "fill" : ""].join(" ");
    const plain = tracks.filter((x) => x.t.kind !== "bus"), busStrips = tracks.filter((x) => x.t.kind === "bus");
    box.innerHTML = `<div class="conWrap ${host.fill ? "fill" : ""} ${opened && !host.compact ? "withFx" : ""}"><div class="${cls}" style="--insRows:${insRows};--sndRows:${sndRows}">
        <div class="chs">${plain.length ? plain.map(strip).join("") : `<div class="empty">No tracks to mix yet.</div>`}${busStrips.length ? `<div class="grpSep" aria-hidden="true"><span>BUSES</span></div>${busStrips.map(strip).join("")}` : ""}</div>
        <div class="mst">${masterStrip()}</div></div>${fxPanel()}</div>
      ${r && r.warnings.length && !host.compact && !host.fill ? `<ul class="changes">${r.warnings.slice(0, 6).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}`;

    // One edit, sent to what the console shows. It is not redrawn under a control in use (B.hold counts the drags,
    // key runs and edits in flight); the answer is, so the strip shows what was stored and where it was.
    const send = async (fn, args, rev = null) => {
      B.hold++;
      try { await api(fn, args, on, rev); host.edited && host.edited(); }
      catch (e) {  // refused because it moved on since the console read it: say so plainly, the redraw shows it as it is
        toast(e.conflict && rev != null && /changed since you loaded/.test(e.message) ? `${on ? on.toUpperCase() : "The session"} changed while you were looking at it, so that edit was not made. The console now shows it as it is: try again.` : e.message);
      }
      B.hold--; await host.reload();
    };
    const set = (id, args) => send(id === "master" ? "project_set" : "track_update", id === "master" ? { master_volume_db: args.volume_db } : { track: id, ...args });
    // A drag is relative: pressing anywhere on a control takes hold of it where it is, so a stray click moves nothing.
    const drag = (el, ev, onMove, onDone) => {
      if (ev.button) return;
      ev.preventDefault(); try { el.setPointerCapture(ev.pointerId); } catch { /* a pointer that is already gone */ } B.hold++;
      const box0 = el.getBoundingClientRect(); let went = false;
      el.onpointermove = (e) => { went = true; onMove(e, box0); };
      el.onpointerup = el.onpointercancel = (e) => { el.onpointermove = el.onpointerup = el.onpointercancel = null; B.hold--; onDone(went && e.type === "pointerup"); };
    };
    // Arrow keys nudge; the edit is sent once the keys rest, and the control keeps the focus through the redraw.
    const nudger = (key, commit) => { let tm = null; return () => { if (tm == null) B.hold++; B.focus = key; clearTimeout(tm); tm = setTimeout(() => { tm = null; B.hold--; commit(); }, 450); }; };
    // A readout that is clicked becomes a box to type a number in: Enter or leaving it sets it, Escape leaves it be.
    const typeIn = (el, value, label, onValue) => {
      const inp = mk(`<input class="db" type="text" inputmode="decimal" value="${value}" aria-label="${label}">`); let done = false;
      const end = (keep) => { if (done) return; done = true; const x = parseFloat(inp.value.replace(",", ".")); inp.replaceWith(el); if (keep && isFinite(x) && x !== value) onValue(x); };
      inp.onkeydown = (e) => { if (e.key === "Enter") end(true); else if (e.key === "Escape") end(false); };
      inp.onblur = () => end(true);
      el.replaceWith(inp); inp.select();
    };
    // An effect leaving the chain: the open panel follows its effect, or closes with it.
    const fxGone = (chId, k) => {
      const o = B.fxOpen; if (!o || o.ch !== chId) return;
      if (o.k === k) B.fxOpen = null; else if (o.k > k) o.k--;
    };
    // A setting edit: one fx_update per gesture. A slider's change lands when the hand lets go (or the arrow keys rest);
    // edits to other settings of the same effect within that moment go in the same call.
    const fxFire = async () => {
      const p = B.fxPend; if (!p) return;
      clearTimeout(B.fxTm); B.fxPend = null; B.hold--;
      const a = document.activeElement, k = a && a.closest && a.closest(".fxPanel") && a.dataset.k;
      await send("fx_update", { track: p.ch, index: p.k, params: p.params }, d.source.rev);
      if (k) B.focus = "fxp:" + k;
      if (host.active()) host.redraw();  // a slider still has the focus, which holds an ordinary redraw back
    };
    const fxEdit = (params, flush = false) => {
      const o = B.fxOpen; if (!o) return;
      const key = `${o.ch}:${o.k}`;
      if (B.fxPend && B.fxPend.key !== key) fxFire();  // another effect's edit still waiting: it goes first, on its own
      if (!B.fxPend) { B.fxPend = { key, ch: o.ch, k: o.k, params: {} }; B.hold++; }
      Object.assign(B.fxPend.params, params);
      clearTimeout(B.fxTm); B.fxTm = setTimeout(fxFire, flush ? 0 : 350);
    };
    const P = box.querySelector(".fxPanel");
    const focusKnob = () => {
      const k = B.focus.slice(4), el = P && P.querySelector(`[data-k="${CSS.escape(k)}"]`);
      if (el) { B.focus = null; el.focus({ preventScroll: true }); }
    };
    if (P && opened) {
      const o = B.fxOpen, f = opened, pq = (sel) => P.querySelector(sel);
      pq("[data-fxp-x]").onclick = () => { B.fxOpen = null; host.redraw(); };
      pq("[data-fxp-byp]").onclick = () => send("fx_update", { track: o.ch, index: o.k, bypass: !f.bypass }, d.source.rev);
      pq("[data-fxp-rm]").onclick = () => { const k = o.k; fxGone(o.ch, k); send("fx_remove", { track: o.ch, index: k }, d.source.rev); };
      const win = pq("[data-fxp-win]");
      if (win) win.onclick = async () => {
        status("opening the plugin window…", "busy");
        try { const res = await api("live_window", { track: o.ch, slot: o.k, take: on }, on); status(""); if (res && res.opened === false) toast("The plugin window did not open."); }
        catch (e) { status(""); toast(e.message); }
      };
      // a slider row: drag it, nudge it, double-click for the default, click the value to type one
      const wireKnob = (row, name, toValue, fromValue, label, dflt, typed = (x) => x) => {
        const rg = row.querySelector("input[type=range]"), vb = row.querySelector(".v");
        rg.oninput = () => { const v = toValue(+rg.value); vb.textContent = label(v); rg.setAttribute("aria-valuetext", label(v)); };
        rg.onchange = () => fxEdit({ [name]: toValue(+rg.value) });
        if (dflt != null) rg.ondblclick = () => { rg.value = fromValue(dflt); rg.oninput(); fxEdit({ [name]: dflt }, true); };
        vb.onclick = () => typeIn(vb, toValue(+rg.value), `${name} value`, (x) => { rg.value = fromValue(x); fxEdit({ [name]: typed(x) }, true); });
      };
      if (f.type === "builtin") {
        for (const k of f.settings || []) {
          const row = P.querySelector(`.fxk [data-k="${CSS.escape(k.name)}"]`); if (!row) continue;
          wireKnob(row.closest(".fxk"), k.name, (x) => knobAt(k, x), (v) => Math.max(0, Math.min(1, knobPos(k, v))), (v) => knobName(k, v), k.default);
        }
      } else if (f.type === "vst3") {
        const list = pq(".fxks"), q = pq(".fxq"), auto = new Set(f.auto || []), SHOW = 60;
        const fill = (res) => {
          const ql = (B.fxQuery || "").toLowerCase(), hit = res.params.filter((p) => !ql || p.name.toLowerCase().includes(ql));
          q.hidden = res.params.length <= 12;  // a short list needs no search
          list.innerHTML = hit.slice(0, SHOW).map((p) => {
            const st = f.params[p.name], v = typeof st === "number" ? st : p.value;
            const txt = typeof st === "string" ? st : typeof st === "number" ? st.toFixed(3) : p.text;
            return `<div class="fxk ${auto.has(p.name) ? "auto" : ""}" title="${esc(p.name)}${auto.has(p.name) ? ": automated" : ""}${typeof st === "number" ? ". Set to " + st.toFixed(3) + " of its travel" : st != null ? "" : ". As the plugin has it"}">
              <span class="kn">${esc(p.name)}${auto.has(p.name) ? " ~" : ""}</span><input type="range" min="0" max="1" step="0.001" value="${v}" data-k="${esc(p.name)}" aria-label="${esc(p.name)}">
              <button class="v" title="Click to type a value from 0 to 1">${esc(txt)}</button></div>`;
          }).join("") + `<div class="meta">${hit.length > SHOW ? `${SHOW} of ${hit.length} shown: search for the rest. ` : ""}${res.total} setting${res.total === 1 ? "" : "s"}${ql ? `, ${hit.length} match` : ""}.</div>`;
          list.querySelectorAll(".fxk").forEach((row) => {
            const name = row.querySelector("[data-k]").dataset.k, clip = (x) => Math.max(0, Math.min(1, +x));
            wireKnob(row, name, (x) => +clip(x).toFixed(4), clip, (v) => v.toFixed(3), null, (x) => +clip(x).toFixed(4));
          });
          if (B.focus && B.focus.startsWith("fxp:")) focusKnob();
        };
        pluginParams(f.device).then((res) => { if (list.isConnected) fill(res); }, (e) => { if (list.isConnected) list.innerHTML = `<span class="meta">${esc(e.message)}</span>`; });
        let qt = null;
        q.oninput = () => { clearTimeout(qt); qt = setTimeout(() => { B.fxQuery = q.value; pluginParams(f.device).then((res) => { if (list.isConnected) fill(res); }, () => {}); }, 200); };
      }
    }
    box.querySelectorAll("[data-ch]").forEach((ch) => {
      const id = ch.dataset.ch, t = d.tracks.find((x) => x.id === id), q = (sel) => ch.querySelector(sel);
      const way = q(".slotway"), cap = q(".cap.now"), read = q("button.db"), lo = +way.dataset.lo, v0 = +cap.getAttribute("aria-valuenow");
      let v = v0;
      const show = (x) => { v = +clamp(x, lo, 12).toFixed(1); cap.style.bottom = faderPos(v) + "%"; cap.setAttribute("aria-valuenow", v); read.textContent = dbName(v); liveMix(id, { db: v }, on); };
      const commit = () => { if (v !== v0) set(id, { volume_db: v }); }, nudge = nudger(`${id}:cap`, commit);
      way.onpointerdown = (ev) => {
        const y0 = ev.clientY, p0 = faderPos(v); cap.focus({ preventScroll: true });
        drag(way, ev, (e, bx) => show(step(dbAt(clamp(p0 + (y0 - e.clientY) / bx.height * 100, 0, 100)), e.shiftKey ? 0.1 : 0.5)), (ok) => { if (ok) commit(); else show(v0); });
      };
      way.ondblclick = () => { show(0); commit(); };
      cap.onkeydown = (e) => {
        const by = { ArrowUp: 0.5, ArrowRight: 0.5, ArrowDown: -0.5, ArrowLeft: -0.5, PageUp: 3, PageDown: -3 }[e.key];
        if (!by && e.key !== "Home") return;
        e.preventDefault(); show(e.key === "Home" ? 0 : v + (e.shiftKey && Math.abs(by) < 1 ? by / 5 : by)); nudge();
      };
      read.onclick = () => typeIn(read, v, "Level in dB", (x) => { show(x); commit(); });
      const clipL = q("[data-clip]"); if (clipL) clipL.onclick = () => { const s = MS.get(id); if (s) s.clip = false; meters(); };
      ch.querySelectorAll("[data-fx]").forEach((b) => b.onclick = (e) => {
        const k = +b.dataset.fx, f = chainOf(id)[k];
        if (e.shiftKey) return send("fx_update", { track: id, index: k, bypass: !f.bypass }, d.source.rev);
        B.fxOpen = isOpen(id, k) ? null : { ch: id, k, label: f.label }; B.fxQuery = "";
        host.redraw();
        const p = B.fxOpen && box.querySelector(".fxPanel"); if (p) p.scrollIntoView({ block: "nearest" });
      });
      ch.querySelectorAll("[data-byp]").forEach((b) => b.onclick = () => { const k = +b.dataset.byp; send("fx_update", { track: id, index: k, bypass: !chainOf(id)[k].bypass }, d.source.rev); });
      // removing names the effect by its place in the chain, so like bypass it carries the revision the console read
      ch.querySelectorAll("[data-fxrm]").forEach((b) => b.onclick = () => { fxGone(id, +b.dataset.fxrm); send("fx_remove", { track: id, index: +b.dataset.fxrm }, d.source.rev); });
      const addFx = q('[data-add="fx"]'); if (addFx) addFx.onclick = () => openDevicePicker(false, (dev) => send("fx_add", { track: id, device: dev }));
      const sum = q("[data-tomix]"); if (sum && host.toMix) sum.onclick = () => host.toMix(id);
      const plate = q(".plate"); if (plate && host.select && t) plate.onclick = () => host.select(id);
      if (!t) return;  // the master strip has a level and a chain; the rest is per track
      const pan = q(".pan"), lab = q(".pan .pv"), p0 = t.pan, ghost = was(t, "pan") ? t.was.pan : null; let p = p0;
      const showPan = (x) => {
        p = clamp(+x.toFixed(2), -1, 1); if (Math.abs(p) < 0.03) p = 0;
        pan.querySelector("svg").outerHTML = knobSvg(p, ghost); pan.setAttribute("aria-valuenow", p); lab.textContent = panName(p); liveMix(id, { pan: p }, on);
      };
      const commitPan = () => { if (p !== p0) set(id, { pan: p }); }, nudgePan = nudger(`${id}:pan`, commitPan);
      pan.onpointerdown = (ev) => {
        const x0 = ev.clientX, y0 = ev.clientY, start = p; pan.focus({ preventScroll: true });
        drag(pan, ev, (e) => showPan(step(start + ((e.clientX - x0) - (e.clientY - y0)) / (e.shiftKey ? 600 : 120), e.shiftKey ? 0.01 : 0.05)), (ok) => { if (ok) commitPan(); else showPan(p0); });
      };
      pan.ondblclick = () => { showPan(0); commitPan(); };
      pan.onkeydown = (e) => {
        const by = { ArrowRight: 0.05, ArrowUp: 0.05, ArrowLeft: -0.05, ArrowDown: -0.05 }[e.key];
        if (!by && e.key !== "Home") return;
        e.preventDefault(); showPan(e.key === "Home" ? 0 : p + (e.shiftKey ? by / 5 : by)); nudgePan();
      };
      // the send list is stored whole, so every send edit carries the revision the console read
      const sendList = (list) => send("track_update", { track: id, sends: list.map((y) => ({ to: y.id || y.to, db: y.db })) }, d.source.rev);
      ch.querySelectorAll("[data-send]").forEach((b) => b.onclick = () => {
        const k = +b.dataset.send;
        typeIn(b, t.sends[k].db, `Send to ${esc(t.sends[k].to)} in dB`, (x) => sendList(t.sends.map((y, j) => j === k ? { ...y, db: clamp(+x.toFixed(1), -60, 12) } : y)));
      });
      ch.querySelectorAll("[data-sendrm]").forEach((b) => b.onclick = () => sendList(t.sends.filter((y, j) => j !== +b.dataset.sendrm)));
      // the menu opens under the key, so it lands in the same place from the mouse or the keyboard
      const under = (b) => { const rc = b.getBoundingClientRect(); return { clientX: rc.left, clientY: rc.bottom + 2 }; };
      const sk = q('[data-add="send"]');
      if (sk) sk.onclick = () => menu(under(sk), buses.filter((b) => b.id !== id && !sendsTo(t, b))
        .map((b) => [`Send to ${b.name}`, () => sendList([...t.sends, { id: b.id, db: -12 }])]));
      ch.querySelectorAll('[data-add="out"]').forEach((ok) => ok.onclick = () => menu(under(ok), [{ id: "master", name: "Master" }, ...buses.filter((b) => b.id !== id)]
        .map((b) => [`${b.id === (t.output_id || t.output) ? "• " : ""}Out to ${b.name}`, () => { if (b.id !== (t.output_id || t.output)) set(id, { output: b.id }); }])));
      q(".ms .m").onclick = () => set(id, { mute: !t.mute });
      q(".ms .s").onclick = () => set(id, { solo: !t.solo });
    });
    meters();  // the live peaks, without waiting for the next frame
    if (B.focus && B.focus.startsWith("fxp:")) {  // a setting just sent keeps the focus (a plugin's list fills in later)
      focusKnob(); if (!P || (opened && opened.type !== "vst3")) B.focus = null;
    } else if (B.focus) {  // a control nudged from the keyboard keeps the focus through its own redraw
      const [id, what] = B.focus.split(":"), ch = [...box.querySelectorAll("[data-ch]")].find((x) => x.dataset.ch === id);
      B.focus = null; const el = ch && ch.querySelector(what === "pan" ? ".pan" : ".cap.now"); if (el) el.focus({ preventScroll: true });
    }
  }
  return { render, meters, faderPos, panName, dbName };
})();

// ---------------------------------------------------------------- the MIX room and the Desk's Mixer tab
// Both show what the Desk is editing (the session, or the take it has open) and edit it. The MIX room is the full
// console; the Desk's tab is the compact one (levels, pan, M/S; a strip's summary opens it in the MIX room).
const TTMix = (() => {
  const $m = (s) => document.querySelector(s);
  const R = { data: null, version: null, take: undefined, busy: false, again: false, err: null, sig: "", B: { hold: 0, focus: null, fxOpen: null } };
  let prefs = { hide: {}, narrow: false };
  try { prefs = { ...prefs, ...JSON.parse(localStorage.getItem("tt.mix") || "{}") }; } catch { /* defaults */ }
  const save = () => { try { localStorage.setItem("tt.mix", JSON.stringify(prefs)); } catch { /* this page only */ } };
  const where = () => document.body.dataset.room === "mix" ? "room" : document.body.dataset.room === "desk" && S.tab === "mixer" ? "tab" : null;
  const boxOf = (w) => w === "room" ? $m("#mixBody") : w === "tab" ? $m("#mixer") : null;
  async function load(force = false) {
    if (!S.doc) { R.data = null; draw(true); return; }
    const take = S.take || "session";
    if (R.take !== take) { R.take = take; R.version = null; R.data = null; R.B.fxOpen = null; }
    if (R.busy) { R.again = true; return; }
    R.busy = true;
    try {
      const res = await api("seat_bench", { seat: "mix", take, have: force ? null : R.version }, null);
      if (!res.unchanged) { R.data = res; R.version = res.version; }
      R.err = null;
    } catch (e) { R.err = e.message; }
    R.busy = false;
    if (R.again) { R.again = false; return load(); }
    draw();
  }
  function draw(force = false) {
    const w = where(), box = boxOf(w); if (!box) return;
    if (!force && (R.B.hold || (box.contains(document.activeElement) && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)))) return;
    const sig = JSON.stringify([w, R.version, R.err, !!S.doc, prefs, S.sel.track, R.B.fxOpen]);
    if (!force && sig === R.sig && box.firstElementChild) return;
    R.sig = sig;
    head();
    if (!S.doc) { box.innerHTML = `<div class="empty">Open or create a project to mix it.</div>`; return; }
    if (!R.data) { box.innerHTML = `<div class="empty">${R.err ? esc(R.err) : "Reading the mix…"}</div>`; return; }
    TTConsole.render(box, R.data, R.B, {
      take: S.take || null, compact: w === "tab", fill: true, hide: prefs.hide, narrow: prefs.narrow, selected: S.sel.track,
      active: () => !!where(), redraw: () => draw(true),
      reload: async () => { R.version = null; await load(); loadState().catch(() => {}); },
      select: (id) => { S.sel.track = id; draw(true); },
      toMix: (id) => { S.sel.track = id; window.Studio?.room("mix"); },
    });
  }
  function head() {
    const src = $m("#mixSrc"); if (!src) return;
    src.textContent = !S.doc ? "NO PROJECT" : S.take ? `${S.take.toUpperCase()} · TAKE` : `SESSION · REV ${S.doc.rev ?? "?"}`;
    src.classList.toggle("tk", !!S.take);
    const r = R.data && R.data.render;
    $m("#mixRead").innerHTML = r ? `<span>${r.lufs != null ? r.lufs : "—"}<i>LUFS</i></span><span>${r.true_peak_dbtp != null ? r.true_peak_dbtp : "—"}<i>dBTP</i></span>${r.stale ? `<span class="warn">RENDER OLDER THAN THE MIX</span>` : ""}` : `<span class="dim">NOT RENDERED YET</span>`;
    document.querySelectorAll("#mixShow button").forEach((b) => b.classList.toggle("on", !prefs.hide[b.dataset.sec]));
    document.querySelectorAll("#mixWidth button").forEach((b) => b.classList.toggle("on", (b.dataset.w === "n") === !!prefs.narrow));
  }
  document.querySelectorAll("#mixShow button").forEach((b) => b.onclick = () => { prefs.hide[b.dataset.sec] = !prefs.hide[b.dataset.sec]; save(); draw(true); });
  document.querySelectorAll("#mixWidth button").forEach((b) => b.onclick = () => { prefs.narrow = b.dataset.w === "n"; save(); draw(true); });
  window.addEventListener("resize", () => { if (where()) draw(true); });
  // called by renderAll (the Desk's state reloaded) and when the room or tab opens
  return { refresh: () => { if (where()) load(); }, show: () => load(true) };
})();
window.TTConsole = TTConsole; window.TTMix = TTMix;  // app.js reaches them as window.* (a top-level const is not one)
