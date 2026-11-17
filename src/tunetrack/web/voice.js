// Voice talkback: push-to-talk into #tbText (Web Speech API) and crew replies read aloud (speechSynthesis).
(() => {
  const form = $("#tbForm"), input = $("#tbText"), talk = form && form.querySelector(".key.talk");
  const mon = $("#monitor"), tools = mon && mon.querySelector(".h2tools");
  if (!form || !input || !talk || !tools) { window.Voice = { heard() {} }; return; }

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = window.speechSynthesis;
  const lang = document.documentElement.lang || navigator.language || "en-US";
  const NOTE = "Hold to talk (or hold `): your browser turns speech into text with its own speech service (Microsoft's in Edge, Google's in Chrome)";
  const load = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v === "1"; } catch { return d; } };
  const save = (k, v) => { try { localStorage.setItem(k, v ? "1" : "0"); } catch {} };

  // ---- the two toggles and the LISTENING label in the monitor's header
  const opt = { send: load("tt.voice.send", true), read: load("tt.voice.read", false) };
  const mkToggle = (key, label, tip) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "mini toggle" + (opt[key] ? " on" : ""); b.textContent = label; b.title = tip;
    b.onclick = () => {
      opt[key] = !opt[key]; b.classList.toggle("on", opt[key]); save("tt.voice." + key, opt[key]);
      if (key === "read" && !opt.read && synth) synth.cancel();
    };
    return b;
  };
  tools.prepend(
    mkToggle("send", "SEND ON RELEASE", "Push-to-talk sends what you said when you let go (off: it stays in the box to edit)"), " ",
    mkToggle("read", "READ ALOUD", "Read the crew's new talkback to you aloud, each seat in its own voice"), " ");
  const live = document.createElement("span");
  live.className = "vcLive"; live.innerHTML = '<i class="lamp red pulse"></i>LISTENING';
  tools.before(live);
  talk.title = (talk.title ? talk.title + ". " : "") + NOTE;

  // ---- push-to-talk
  let rec = null, before = "", finishing = null, holdTimer = 0, suppressClick = false, warned = false;
  const setLive = (on) => { talk.classList.toggle("listening", on); mon.classList.toggle("listening", on); };
  const once = (flag, text, info) => {
    if (flag === "warned") { if (warned) return; warned = true; return toast(text, info); }
    if (load(flag, false)) return; save(flag, true); toast(text, info);
  };

  function start() {
    if (rec || finishing) return;
    if (!SR) return once("warned", "Voice talkback needs Edge or Chrome (this browser has no speech recognition).");
    if (synth) synth.cancel();  // never talk over the user
    before = input.value; const base = before && !/\s$/.test(before) ? before + " " : before;
    const r = rec = new SR();
    r.continuous = true; r.interimResults = true; r.lang = lang;
    r.onresult = (ev) => {
      let said = "";
      for (let i = 0; i < ev.results.length; i++) said += ev.results[i][0].transcript;
      input.value = base + said.trim();
      if ([...ev.results].some((x) => x.isFinal)) once("tt.voice.noted", NOTE, true);
    };
    r.onerror = (ev) => {
      if (ev.error === "not-allowed" || ev.error === "service-not-allowed") toast("Allow the microphone for this page to talk (the icon in the address bar).");
      else if (ev.error === "network") toast("The browser's speech service could not be reached.");
    };
    r.onend = () => { if (r === rec) { rec = null; setLive(false); } if (r.done) r.done(); };
    try { r.start(); setLive(true); } catch { rec = null; }
  }

  function stop() {
    const r = rec; if (!r) return;
    rec = null; setLive(false);
    finishing = new Promise((res) => { r.done = res; setTimeout(res, 1500); });  // wait for the last final result
    try { r.stop(); } catch { r.done(); }
    finishing.then(() => {
      finishing = null; r.onresult = null;
      if (opt.send && input.value.trim()) form.requestSubmit();
      else { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
    });
  }

  function cancel() {
    const r = rec; if (!r) return;
    rec = null; setLive(false); r.onresult = null;
    try { r.abort(); } catch {}
    input.value = before;
  }

  // TALK key: a quick click sends as before; holding it past 250 ms talks.
  const release = () => {
    clearTimeout(holdTimer); holdTimer = 0;
    if (rec) { suppressClick = true; stop(); }
  };
  talk.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    suppressClick = false;
    holdTimer = setTimeout(() => { holdTimer = 0; start(); if (rec) suppressClick = true; }, 250);
  });
  for (const ev of ["pointerup", "pointercancel", "pointerleave"]) talk.addEventListener(ev, release);
  talk.addEventListener("click", (e) => { if (suppressClick) { suppressClick = false; e.preventDefault(); } }, true);
  talk.addEventListener("contextmenu", (e) => { if (rec || holdTimer) e.preventDefault(); });

  // Backquote anywhere outside a text field; Escape while holding cancels.
  const editing = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  let keyHeld = false;
  document.addEventListener("keydown", (e) => {
    if (e.code === "Escape" && rec) { e.preventDefault(); keyHeld = false; clearTimeout(holdTimer); suppressClick = true; return cancel(); }
    if (e.code !== "Backquote" || e.ctrlKey || e.altKey || e.metaKey) return;
    if (!keyHeld && editing(document.activeElement)) return;
    e.preventDefault();
    if (e.repeat || keyHeld) return;
    keyHeld = true; start();
  });
  document.addEventListener("keyup", (e) => {
    if (e.code !== "Backquote" || !keyHeld) return;
    e.preventDefault(); keyHeld = false; stop();
  });
  window.addEventListener("blur", () => { if (keyHeld) { keyHeld = false; stop(); } });

  // ---- read aloud
  const spoken = new Set();
  let voices = [];
  const loadVoices = () => {
    const all = synth ? synth.getVoices() : [], pre = lang.slice(0, 2).toLowerCase();
    const mine = all.filter((v) => (v.lang || "").toLowerCase().startsWith(pre));
    voices = mine.length ? mine : all;
  };
  if (synth) { loadVoices(); synth.addEventListener("voiceschanged", loadVoices); }
  const hash = (s) => { let h = 7; for (const c of s) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h; };
  const clean = (t) => {
    t = t.replace(/```[\s\S]*?```/g, " ").replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/[*_`#>~|]+/g, "").replace(/^\s*[-+]\s+/gm, "").replace(/\s+/g, " ").trim();
    if (t.length <= 400) return t;
    const cut = t.slice(0, 400), end = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("! "), cut.lastIndexOf("? "));
    return end > 120 ? cut.slice(0, end + 1) : cut.slice(0, cut.lastIndexOf(" ")) + "…";
  };

  function heard(e, text, senderTitle) {
    if (!synth || !e || spoken.has(e.seq)) return;
    if (!opt.read || e.sender === "you" || (e.to !== "you" && e.to !== "all")) return;
    if (!(Date.now() / 1000 - e.at < 20)) return;
    spoken.add(e.seq);
    if (!voices.length) loadVoices();
    const h = hash(e.sender || ""), u = new SpeechSynthesisUtterance(`${senderTitle}: ${clean(text || "")}`);
    if (voices.length) { u.voice = voices[h % voices.length]; u.lang = u.voice.lang; } else u.lang = lang;
    u.pitch = 0.85 + ((h >>> 8) % 7) * 0.05;
    if (!rec) synth.speak(u);  // the browser queues utterances itself
  }

  window.Voice = { heard };
})();
