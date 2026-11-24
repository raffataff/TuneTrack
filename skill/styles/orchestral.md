---
title: Orchestral
match: orchestral, orchestra, symphonic, symphony, fantasy, adventure, film score, cinematic, classical, string quartet, chamber
about: A real orchestra writing as if a composer had scored it: sections in their registers, phrases that breathe.
---

## producer
- Sampled instruments only (patches or SFZ): strings, woodwinds, horns, harp, timpani. When status() shows none,
  ask the user before planning a synth in their place.
- Plan the orchestration per section: who has the tune, who accompanies, what enters for B, where the tutti is.
  A short cue has one climax, not one per section; a long piece builds several peaks toward one main climax.
- 80-120 bpm for adventure, slower for wonder; a short cue is one theme, varied; a longer piece has two or more themes that develop, combine and return, not a string of unrelated tunes.

## composer
- The theme first, as a single line, before any accompaniment: a motif someone could whistle, then grow it
  (sequence, inversion, augmentation in the horns).
- Harmony with motion in the inner voices: suspensions resolving down, a pedal under moving chords, chromatic
  mediants for wonder, Dorian or Lydian for adventure.
- Write long lines in long notes; let the accompaniment carry the rhythm. Rests are where the hall rings.
- Dynamics are drawn with cc() on the controller the patch answers to (usually CC1), not with velocity: a swell
  into each phrase's peak, a fall at its end, a base level per section.

## arranger
- Melody in violins (doubled by flute an octave up, or horn an octave down for warmth); harmony in violas, second
  violins and horns; bass in cellos and basses an octave apart.
- Horns in their middle register (F3-F5), in fifths and octaves; trumpets for peaks only; trombones for weight in
  the tutti; timpani on roots and fifths at phrase ends.
- Woodwinds answer the strings in their rests; harp arpeggios and glissandos for colour and transitions.
- Humanise every part (notes_edit(op="humanize", timing_ms=8-15, velocity=4-6)); strings slightly late on
  entries; legato on lines, separate tracks for short articulations.

## rhythm
- No drum kit. Timpani rolls into peaks, timpani on 1 at phrase ends, a cymbal swell into the climax, a soft bass
  drum under a march.
- Rhythmic drive comes from string ostinatos (8ths in violas or cellos), not percussion.

## sound
- Sections, not soloists, unless the brief asks for intimacy: ensemble strings, a horn section, solo flute and
  oboe for colour.
- Check each patch answers to CC1 or CC11 and say which, so the writers draw dynamics on it.
- Legato and long patches for lines, a separate short (spiccato, staccato, pizzicato) patch for ostinatos.

## mix
- One hall reverb on a bus, every section sent to it; strings and woodwinds a little wetter than brass and
  timpani. Early reflections give the depth, not the level.
- Seating as panning: violins left, violas and cellos centre-right, basses right of centre, horns left-centre,
  woodwinds centre, harp left, timpani centre-back.
- Gentle EQ only (Nova): a high-pass per section under its lowest note, a dip in 250-400 Hz on low strings when
  muddy. No compression on the orchestra; leave the dynamics alone. Master -18 to -20 LUFS for menu and
  exploration cues.

## review
- Sounds composed, or like patches playing chords? Look for a theme that returns, inner voices that move, and
  dynamics drawn on CC rather than flat.
- Instruments outside their range, horns above their comfortable register, and every section playing all the time
  are faults.
- One climax: two equal peaks is a form problem for the Composer.
