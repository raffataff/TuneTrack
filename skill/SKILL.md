---
name: tunetrack
description: Compose, arrange, mix and render game music (menu themes, exploration/battle/boss loops, town and shop themes, stingers, adaptive layers) with the TuneTrack MCP tools. Use whenever the user asks for music, a soundtrack, theme, loop, jingle, stinger or ambience for a game or any project, or mentions TuneTrack.
---

# TuneTrack: game music with the `tunetrack` MCP tools

TuneTrack is the user's own music workstation. You write the music as notes, chords, patterns and CC curves; TuneTrack plays them
through real instruments (the user's VST3 plugins via saved patches, SFZ sample libraries, or built-in Faust synths), mixes,
masters and renders loop-ready files. You can't hear, so you work from music theory, the tool's analysis reports and the
piano-roll image. Every decision should be deliberate. No filler loops, no generic noodling.

If the tools are missing, the MCP server isn't registered: `claude mcp add --scope user tunetrack -- C:\Users\Owner\Documents\TuneTrack\.venv\Scripts\tunetrack-mcp.exe`.

## 1. Understand the project first

Before writing a note, find out what the music is for:
- Read the game project: README, design docs, story/setting text, scene/level names, art style (look at a few images), UI theme
  colours, existing audio folders and file naming, the engine (Godot `project.godot`, Unity `Assets/`, Unreal `.uproject`, web).
- Settle on: **genre/palette, mood (2-3 adjectives), energy, tempo, key/mode, cue type, length, loop form, delivery format and path.**
  If the user gave intent ("dark synthwave menu, 100 bpm") it wins over your inference. Ask only if the project gives no clue.
- Record it: `project_new(name, tempo, key, time_sig, brief={genre, mood, use, game, references, notes})`.

## 2. Check the instrument library

`status()` lists VST3 instruments, **patches** (a plugin plus a saved sound, with tags), SFZ instruments and built-in synths.
- Acoustic/orchestral parts need real samples: use `patch:<id>` (e.g. HALion, BBC Symphony Orchestra, Spitfire LABS patches)
  or `sfz:<path>`. Built-in synths are excellent for electronic, synthwave, chiptune, ambient and lo-fi, and fine as support layers.
- VST3 instruments like HALion Sonic and Groove Agent load **empty**: without a patch they render silence (render warns you).
  MODO DRUM works without a patch.
- If the palette the brief needs is missing (for example orchestral strings with no string patch or SFZ), say so plainly and
  offer: (a) the user captures patches (`patch_capture` opens the plugin window; only while they're at the computer), (b) installing
  a free library (README lists some), or (c) a stylised synth version. Don't pass a synth off as an orchestra.
- `audition("patch:<id>")` checks an instrument makes sound and reports its brightness. `sfz_info` shows range and keyswitches.
- Patches with `dynamics_cc` (1 or 11) get their loudness from that CC, not from velocity: always write `cc()` curves for them.

## 3. Plan the form (before the notes)

Set `markers()` for sections and `project_set(loop="5:1-21:1")` for the loop region, then put each section's harmony on
the chord track (section 4). Typical forms:

| Cue | Length | Form and notes |
|---|---|---|
| Main menu | 45-120 s loop | Optional 2-4 bar intro (played once) then an A(8) B(8) or A A' B A loop. Memorable motif, medium density, leaves room for UI sounds. |
| Exploration / overworld | 1.5-3 min loop | Long arcs, sparse texture, breathing room, slow harmonic rhythm (1-2 bars per chord). |
| Town / shop | 40-90 s loop | Warm, rhythmic, small ensemble, catchy. |
| Battle | 60-120 s loop | 130-170 bpm, driving ostinato, strong bass, accents and hits, short phrases; B section raises intensity. |
| Boss | 90-180 s loop | Darker mode, heavier low end, a motif that is twisted from the main theme. |
| Victory / defeat stinger | 2-8 s, one-shot | Victory: rising line to a bright major chord + cymbal. Defeat: falling line to a minor or diminished chord, fade. `loop="off"`. |
| Ambience bed | 1-3 min loop | Pads/drones, slow movement, no strong pulse, little melody. |
| Credits | 2-4 min, one-shot | Medley of the game's motifs. |

Loop rules: the loop is a whole number of bars; its last bar must lead back into its first (dominant, or a pickup into the first
melody note). Don't let notes cross the loop end unless you want them to. Release and reverb tails are folded into the loop start
automatically by `render(loop="auto")`.

Adaptive music: put every layer in the same key, tempo and length, tag tracks with `layer` (`base`, `tension`, `combat`), and
render with `layers=True` for vertically layered stems that the game crossfades.

## 4. Write the music

Order: **motif, then harmony on the chord track, bass, melody phrases, counterline and inner parts, rhythm section,
dynamics, then variation.**

Chord track first
- Set the harmony once per section before writing parts: `harmony("i VI III VII i VI iv V", section="A")`. Roman numerals
  follow the project key; symbols work too (`"Am7 F/A G"`). One chord per bar, or `:beats` per chord (`"i:2 iv:2 V7:4"`);
  with `section` or `end` the progression repeats to fill the range. `harmony()` with no progression reads it back.
- Then generate the parts that follow it, without passing a progression: `chords("Pad", section="A")`,
  `pattern("Bass", pattern="1 - - 1 . 1 5- .", section="A", octave=2)`, `pattern("Harp", pattern="1 5 8 10", section="B")`.
  Write melodies against it with `write()`; `score()` checks them against it.
- Reharmonise by editing the chord track for a range (`harmony("III iv:2 V:2", at="10:1", end="12:1")`) and re-running
  the generators for that range (`chords("Pad", at="10:1", end="12:1", ...)` with the same settings as before). `score()`
  flags any part you forgot as "doesn't follow the chord track". A modulation: `harmony(..., key="F major")` for that range.

Melody
- Build from a 1-2 bar motif with a recognisable rhythm and contour. Develop it with repetition, sequence (moved up a step),
  inversion, rhythmic augmentation. Structure phrases as question (ends open, on 2 or 5) and answer (ends on 1).
- One clear climax per section (highest note, strongest beat). Stepwise motion with occasional leaps; resolve leaps by step in the
  opposite direction. Keep it singable (about an octave and a fifth) unless it's an instrumental showpiece.
- Leave space: rests and long notes give phrases shape and room to breathe. Not every beat needs a note.
- Chord tones on strong beats; passing and neighbour tones on weak beats. Long notes on dissonances are deliberate choices.

Harmony
- `chords()` voice-leads for you (following the chord track). Choose progressions that serve the mood. Vary harmonic rhythm: 1 chord per bar, faster at cadences.
- Useful moves: minor i-VI-III-VII (epic/heroic), i-iv-VII-III (nostalgic), I-V-vi-IV (bright), ii-V-I and IVmaj7-iii7-vi7
  (jazzy/cozy), bVI-bVII-I (triumphant), Lydian I-II (wonder/magic), Phrygian i-bII (menace), Dorian i-IV (adventurous, folk),
  chromatic mediants (bIII, bVI major in major key) for cinematic colour, pedal points under changing chords for tension.
- End sections with real cadences (V-i, iv-V, bVI-bVII-I). Secondary dominants (`V/V`, `V7/vi`) add forward motion.

Bass
- Roots on strong beats, fifths and approach notes (step or leading tone) into chord changes. Give it a rhythm that locks with the
  kick. Keep it below C3 and alone in its register.

Arrangement
- Registers: bass below C3, harmony C3-C5, melody above or clearly separated from the accompaniment. Intervals smaller than a fifth
  below C3 sound muddy (analyze flags this).
- One focal element at a time; counterlines answer in the melody's rests (call and response).
- Build sections by adding and removing layers. B should contrast A: different register, density, rhythm or harmonic colour.
- Orchestral: melody in violins (doubled by flute an octave up or horn an octave down for warmth), harmony in violas/cellos/horns,
  bass in basses/cellos. Horns sound best in fifths and octaves in their mid register; trumpets for fanfare peaks; timpani on roots
  and fifths at phrase ends. Use `role` on tracks so range checks apply. `theory(range="horn")`.
- Drums: a groove with variation every 4 bars and a fill into section changes (the last beat or two of bar 8). Crash on the downbeat
  of new sections. Ghost notes (`o`) and swing add life.

Dynamics and feel
- Shape every phrase: `notes_edit(op="velocity", ramp_from, ramp_to)` for crescendos, accents with `>`.
- CC-controlled orchestral patches: `cc(track, 1, start, end, 40, 100, shape="swell")` on long notes, and base levels per section.
- `notes_edit(op="humanize", timing_ms=5-15, velocity=4-8)` for acoustic parts. Keep chiptune and tight electronic parts on the grid.
- Staccato (`'`) and legato (`notes_edit(op="legato")`) change character a lot. Use them on purpose.

Reuse without copy-paste monotony: `arrange(op="copy")` a section, then vary the copy (new counterline, octave shift,
reharmonised cadence, drums opened up).

## 5. Genre palettes (built-in synth names in brackets; prefer real patches when the genre is acoustic)

| Genre | Tempo | Modes / harmony | Palette and rhythm |
|---|---|---|---|
| Orchestral fantasy / adventure | 80-120 | Major/Dorian/Lydian, i-VI-III-VII | Strings, horns, woodwinds, harp arps, timpani; [pluck], [bell] for colour |
| Epic / battle | 130-170 | Minor, Phrygian, i-bVI-bVII | String ostinato in 8ths/16ths, brass stabs, taiko/toms, choir pads; [brass_synth], [reese_bass] hybrid |
| Cozy / village / farming | 85-110 | Major, add9 and maj7 chords | Nylon/pluck, e-piano, mallets, light shaker; [nylon_pluck], [epiano], [mallet] |
| Synthwave / retro | 90-118 | Minor (Aeolian/Dorian) | [supersaw] pads, [analog_bass] 8th-note bass, [lead], gated snare, big reverb |
| Chiptune (NES-style) | 120-180 | Major or minor, fast arps for chords | Exactly 2 [chip_pulse]/[chip_square] voices, 1 [chip_triangle] bass, [chip_noise] drums; no reverb or a tiny one; quantized |
| Horror / tension | 50-80 or no pulse | Chromatic clusters, tritones, pedal | [dark_pad] drones, sparse high [bell] notes, low pulses, silence as a tool |
| Ambient / space | 60-90 | Lydian, sus chords, slow changes | [pad], [dark_pad], [bell], long reverb sends, slow volume automation |
| Lo-fi / chill | 70-90, swing | ii7-V7-Imaj7, m9 chords | [epiano], soft drums with swing 0.3-0.5, [saturator] and [filter] on the master |
| Action / EDM | 120-150 | Minor, four-on-the-floor | [drums] or a kit, [reese_bass]/[analog_bass], [saw_pluck] arps, [supersaw] leads, sidechain-style volume automation |
| Puzzle / casual | 100-130 | Major, playful chromatic passing tones | [mallet], [pluck], pizzicato patches, staccato bass |

## 6. Mix and master

- Gain staging: start tracks around -6 to -12 dB; the melody sits on top and the pads below it. Render reports per-track levels.
- Add a shared reverb on a bus (`track_add("Reverb", kind="bus", fx=["builtin:reverb"])` or a VST3 such as ValhallaSupermassive with
  Mix at 100%), and send to it per track. Drier for fast or busy music, wetter for ambient.
- High-pass everything that isn't bass or kick (`builtin:eq` hpf 100-200 Hz), cut 250-500 Hz on pads if the report says muddy.
- Pan for width (harp left, second violins right, hats slightly off-centre). Keep bass and kick centred.
- Loudness: the master is normalised to `target_lufs` (default -16). Background/menu cues often sit at -18 to -20; battle at -14 to -16.
- The user's VST3 effects (SSL, TDR Nova, Waves, Valhalla) can go on tracks, buses or the master via `fx_add`; set parameters by name
  with display text (`{"Mix": "100 %", "LowCut": "200 Hz"}`).

## 7. Review loop (always)

1. `score(section="A")` (or `start`/`end`) per section: the chord on each beat and every part's notes in TTN, bar by bar, lined
   up by beat so you can see what sounds together. Read it like a score: does the bass land on roots or chosen inversions, does
   the melody sit on chord tones at strong beats, do counterlines answer in the gaps? Fix every `!` line or decide it's
   deliberate: clashes on strong beats (the fix names chord tones to use), parallel 5ths/8ves between bass and lead, voice
   crossings, parts that don't follow the chord track (re-run that generator for the range). `Dm*` marks a chord inferred
   from the notes where the chord track has none.
   `notes_read(track)` shows a clip's TTN with velocities. `analyze()` gives key/range/overlap/mud checks, the same
   `harmony_warnings` and a piano-roll PNG. **Read the PNG** to check the form, density and register spread.
2. `render()`. Act on every warning: silent track (no patch loaded), mud, dull or harsh top end, peaks, limiter working hard, loop
   seam. Unchanged tracks are cached, so iterate freely.
3. `listen()`: your ears. It measures the rendered audio, not the notes, and catches what the notes can't show you:
   - `energy_arc`, `sections`, `bar_lufs`: does each section do what the form says? B should differ from A in level, brightness,
     density or who leads (`tracks_db`, `most_present`); builds rise bar by bar; the loop's last bar flows into its first.
   - `masking`: each track's `vs_rest_db` in its main band. Keep the lead at 0 or above. Parts under about -10 are barely heard:
     raise them, EQ-dip the `covered_by` track there, move register, or cut them. `pairs` are parts fighting for one band
     (kick vs bass at 63 Hz: let one own the lows, or duck it on the hits).
   - `verify`: probe renders of every pitch each part uses. They show silent or much quieter pitches (outside the instrument's
     range), parts that sound transposed, slow attacks on short notes, bars with notes but no sound (CC at 0?), and `drum_notes`:
     what each drum note really sounds like and how loud it is at equal velocity. Trust these over patch names and descriptions.
   Fix, re-render, listen again until every warning is fixed or deliberate. The first run probe-renders each instrument (seconds
   per plugin track, cached after). `reference="<file>"` compares the mix with a track the user wants to match.
   `score()` tells you what the notes do, `listen()` what the audio does: a part can be right in the score and buried in the mix,
   or loud and clear but off the chord. Use both.
4. Tell the user what you made and how it's built (form, key, tempo, instruments, the motif), give the file path, and offer
   `play()` or the editor (`ui_open()`) so they can listen. Their ears are the final judge, so ask what to change.

## 8. Deliver

`render(out="<game>/<audio dir>/<name>.<ogg|wav>", loop="auto")`, matching the project's folders and naming.
- Godot: `.ogg`. Set Loop in the import dock; for intro+loop use `loop_offset` = `loop_start_sec` from the `.loop.json`.
- Unity: `.ogg` or `.wav`. Loop flag on the AudioSource; intro+loop via two clips or scheduled playback using `.loop.json`.
- Unreal: 16-bit 48 kHz `.wav` (`bits=16`); the WAV carries a `smpl` loop chunk.
- Web: `.ogg` plus `.mp3`.
Use `stems=True` / `layers=True` for adaptive systems and `midi=True` to hand the arrangement to a DAW (Cubase).

## TTN notation (for `write`)

```
D4:q!mf F4 A4:h | (D3 A3 F4):w | r:h C5:e. B4:s A4:q' G4:q> | E4:h~ E4:q r:q
```
Pitch `C4` = middle C (MIDI 60), `F#3`, `Bb5`, or `r` for a rest. Chords `(C4 E4 G4)`.
Durations `w h q e s t` (whole down to 32nd), dotted `q.`, triplet `e3`, beats `1.5` or `3/2`, sums `h+e`; sticky until changed.
Velocity `!pp !p !mp !mf !f !ff` or `!0-127`, sticky. `'` staccato, `>` accent, `~` tie to the next same pitch.
`|` bar line, checked against the time signature (mismatches come back as warnings, so fix them).
`@6.5` moves the cursor to beat 6.5 of the written span, for a second voice in one call. Or use `mode="merge"`.

Pattern tokens (for `pattern`): chord degrees `1 3 5 7 9`, `8 10 12` an octave up, `5-`/`3+` octave shifts, `b7`/`#4` altered degrees above the root, `.` rest,
`-` hold, `1>` accent. Example basses: `"1 - - 1 . 1 5- ."` (pop), `"1 . 1 . 1 . 1 ."` (8ths, synthwave), `"1 - 5- - 8 - 5- -"`
(walking feel). Arps: `"1 5 8 10 12 10 8 5"`, `"1 3 5 8"` with rate 0.25 for 16ths.
