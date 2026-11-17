---
title: Chiptune
match: chiptune, chip tune, chip, 8-bit, 8 bit, 8bit, nes, famicom, game boy, gameboy, bitpop, retro console
about: A console sound chip: two pulse voices, a triangle bass, noise drums. Every note on the grid.
---

## producer
- Four voices at most and only these: two pulse leads ([chip_pulse], [chip_square]), one [chip_triangle] bass,
  [chip_noise] drums. No pads, reverb buses or sampled instruments; nothing a chip could not play.
- 120-180 bpm. A loop of 16-32 bars with a hook in the first two; B goes to the relative key or up a fourth.
- Everything quantised: tell every seat that writes notes no humanising.

## composer
- The lead carries the tune alone, so it must be singable and busy enough to hold the ear: 8th and 16th motion,
  octave leaps, a repeated rhythmic cell.
- Chords are arpeggios, not stacks: the second pulse plays the chord as fast 16th or 32nd arps (pattern() at
  rate 0.125-0.25, "1 3 5 8"), or answers the lead in its rests.
- Echo: a copy of the lead in the second pulse, a 16th or a dotted 8th late, two dynamic steps softer, when no arp
  is needed.
- Velocities in a few fixed steps (!f the lead, !mp the echo); no ramps. Staccato (') on the bass and arps.

## arranger
- Never more than one note at a time per voice. Chords become arps or are split between the two pulses.
- Registers: triangle bass C2-C4, pulses C4-C7, the lead above the arp.
- Stay on the grid: no humanize, no legato overlaps.

## rhythm
- [chip_noise] only: short high noise for hats, a longer low one for the snare; the kick is often the triangle
  bass's own attack, or a very short low noise.
- Straight 8th or 16th hats, the snare on 2 and 4 or a busy 16th pattern in B. No ghost notes, no swing.
- Triangle bass in octaves (pattern "1 8 1 8") or a walking line; staccato, quantised.

## sound
- Built-in [chip_pulse], [chip_square], [chip_triangle], [chip_noise] are right here; no patches, SFZ or plug-ins.
- Pulse width is the colour: a thinner pulse for the arp, a fuller one for the lead (instrument_params, then
  audition to compare).

## mix
- Almost dry: no reverb bus, or one tiny room under -20 dB on the lead only.
- Hard pan is period-true but tiring: lead centre, arp 20-30% to one side, echo to the other; bass and noise
  centre.
- No EQ smoothing or compression on the voices; a high-pass under 40 Hz and the master's loudness is all.

## review
- Count the voices: more than four, a held chord on one track, or a pad is out of style.
- Off-grid notes or ramped velocities are faults here, not feel.
- The hook should be recognisable from the lead alone with the arp muted.
