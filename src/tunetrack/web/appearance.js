// Studio appearance is shared through the existing library preferences. CSS-only
// atmosphere keeps the rack offline-capable and leaves the audio thread alone.
"use strict";
window.TTAppearance = (() => {
  const colours = { producer: "#68e6ac", spotting: "#91b6ff", composer: "#62d5ff", arranger: "#b69aff", rhythm: "#ffc56c", sound: "#58e0dc", tracking: "#ff9e84", mix: "#ef8dda", review: "#a4df84" };
  const defaults = { effect: "aurora", intensity: 55, motion: true, wiring: "digital", logs: true, colours };
  let value, timer, revision = 0;
  function normalise(v) {
    v = v && typeof v === "object" ? v : {};
    return { effect: ["aurora", "grid", "carbon", "none"].includes(v.effect) ? v.effect : defaults.effect,
      intensity: Number.isFinite(v.intensity) ? Math.max(0, Math.min(100, v.intensity)) : 55,
      motion: v.motion !== false, logs: v.logs !== false,
      wiring: ["digital", "cables"].includes(v.wiring) ? v.wiring : "digital",
      colours: Object.fromEntries(Object.entries(colours).map(([id, c]) => [id, /^#[\da-f]{6}$/i.test(v.colours?.[id]) ? v.colours[id] : c])) };
  }
  function apply(v, save = false) {
    value = normalise(v);
    const root = document.documentElement;
    for (const [id, c] of Object.entries(value.colours)) root.style.setProperty("--seat-" + id, c);
    root.style.setProperty("--atmosphere", value.intensity / 100);
    root.dataset.atmosphere = value.effect; root.dataset.motion = value.motion ? "on" : "off";
    root.dataset.wiring = value.wiring; root.dataset.seatLogs = value.logs ? "on" : "off";
    try { localStorage.setItem("tt.appearance", JSON.stringify(value)); } catch { /* private mode */ }
    dispatchEvent(new Event("tt-appearance"));
    if (save) {
      revision++;
      clearTimeout(timer);
      timer = setTimeout(() => api("ui_prefs", { set: { studio_appearance: value } }, null).catch(() => {
        toast("Appearance saved in this browser. Library settings could not be saved.");
      }), 350);
    }
  }
  try { apply(JSON.parse(localStorage.getItem("tt.appearance") || "null")); } catch { apply(null); }
  api("ui_prefs", {}, null).then((p) => { if (!revision && p?.studio_appearance) apply(p.studio_appearance); }).catch(() => {});
  return { get: () => structuredClone(value), set: (v) => apply(v, true), reset: () => apply(defaults, true), colours };
})();
