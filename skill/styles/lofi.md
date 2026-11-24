---
title: Lo-fi
match: lo-fi, lo fi, lofi, chillhop, chill hop, lo-fi hip hop, study beats, chill beats, jazzhop, chill
about: Dusty jazz chords over a lazy swung beat; warm, a little broken, never busy.
---

## producer
- 70-90 bpm, swung. 8-16 bar loops that repeat with small changes; usually no big climax or key change unless the brief wants a journey.
- A small band: e-piano or nylon chords, soft drums, round bass, one lead voice that comes and goes. Room left
  for the listener to think.

## composer
- Seventh and ninth chords: ii7-V7-Imaj7, Imaj7-vi7-ii7-V7, iv7 or bVII9 borrowed from minor; one chord a bar or
  two, the last bar of a phrase sometimes a half-step slide into the next chord.
- The lead is sparse and conversational: short phrases with long rests, blue notes (b3, b7) as colour, ending off
  the beat.
- Voicings with the 3rd and 7th low and the 9th on top; leave the root to the bass.
- Feel matters more than notes: everything humanised afterwards (notes_edit(op="humanize", timing_ms=15-25,
  velocity=8-12)), chords rolled a little late, the lead behind the beat.

## arranger
- Usually four to six parts. Chords in the middle register (C3-C5), never doubled by another instrument.
- Drop parts in and out every 4-8 bars instead of adding new ones: the bass alone for a bar, the drums out for a
  break.
- notes_edit(op="humanize", timing_ms=15-25, velocity=8-12) on every played part; never on the drums' kick.

## rhythm
- A soft boom-bap kit: kick on 1 and the "and" of 2 or 3, snare (or rim) on 2 and 4, hats in swung 8ths with
  velocity alternating loud-soft. swing=0.3-0.5 in drums(), or notes_edit(op="quantize", swing=0.35).
- Ghost notes on the snare (o) and hats that skip a beat now and then. A fill is one missing kick, not a roll.
- Bass: round and short, roots and fifths that leave space after the kick; humanised, a little late.

## sound
- [epiano] or a sampled Rhodes or piano for the chords; nylon guitar, a mellow sine or flute for the lead; a soft
  kit (patch or [drums] on a soft setting); a round bass with no click.
- Prefer the duller choice when two candidates are close: brightness is the enemy here.

## mix
- Warm and narrow at the top: a low-pass around 8-11 kHz on the chords and lead, the hats darkened too.
- [saturator] and [filter] on the master (soft drive, low-pass about 12 kHz), a short room reverb on a bus for the
  chords and lead.
- Kick and bass centre; the chords a little wide; the lead dry-ish and upfront but quiet. Master around -16 LUFS.
- Sidechain the chords lightly from the kick (2-3 dB) for the gentle pump.

## review
- Too clean or on the grid is the most common fault: played parts must breathe.
- A busy lead, a bright mix, or a chord change on every beat is out of style.
- The loop should be listenable for minutes: flag anything that pokes out on the fifth hearing.
