---
name: rank-library
description: Rank the library by a measured sound property ("the slowest song", "the most boring track") using the psychoacoustic profile database.
example: find the slowest and boring song
triggers: slowest, fastest, quietest, loudest, calmest, most boring, least boring, most aggressive, gentlest, darkest, brightest, saddest, happiest, most danceable, roughest, punchiest, densest, sparsest, most harmonic, langsamste, schnellste, leiseste, lauteste, ruhigste, langweiligste, aggressivste, traurigste, froehlichste, tanzbarste, haerteste, the most, am meisten, am wenigsten, least
---
# Rank by sound property

Some requests are not a **text** search: no tag holds the word "slowest" or
"boring".  They are a question about how the music *sounds*, and the only
source for that is the psychoacoustic profile database
(`library-work/app/psycho/features.db`, 13,000+ measured tracks, twelve 0..1
axes each — see the `psycho-profile` skill).

This skill ranks every measured track by one or more properties and returns
the top of that ranking.

## How a property becomes a ranking

Each property is a **recipe** of axis weights (`chat/psycho.py::CONCEPTS`).
"Axis" means one of Speed, Energy, Aggression, Brightness, Danceability,
Valence, Calmness, Density, Roughness, Harmony, Punch, Width.

- `slow` → Speed −1
- `fast` → Speed +1
- `aggressive` → Aggression +1
- `calm` → Calmness +1
- `boring` → Energy −1, Density −0.8, Brightness −0.6, Punch −0.6,
  Calmness +0.6, Speed −0.4, Danceability −0.6

A negative weight means *less* of that axis is *more* of the property, so
"boring" prefers quiet, sparse, dark, flat, slow and undanceable music instead
of guessing from a genre name.

Two properties in one sentence are combined (weights add up), so
"the slowest and boring song" ranks by slow **and** uneventful at once.  A word
after *least* / *weniger* / *am wenigsten* flips the property to its antonym.

The score is the weighted mean of `2·axis − 1` over the covered axes: 1.0 is
the extreme end of the property, 0.5 is library average.  A track must have at
least 60 % of the recipe's weight measured to be ranked, and rows whose decode
was silent (`energy_db < −100`) are excluded — a broken rip would otherwise win
"most boring" by having no sound at all.

## What it answers

- "find the slowest and boring song" → the quietest, slowest film cues
- "the 5 most aggressive songs" → the loudest, roughest, fastest material
- "the least boring track" → the most interesting one
- "das langsamste Lied von Die Ärzte" → ranked only within that artist
- "play the slowest song" → ranks, then queues the whole list from the top

The reply is a numbered list with the score and the leading axes, so the reason
for a hit is visible.  The numbers stay valid for the next message, and
"save that as <name>" turns the ranking into a playlist.

Arguments: `terms` (list of `[property, sign]` pairs), `properties`
(alternative list of names), `limit` (integer, default 12), `filter` (a genre
or artist to rank within), `play` (bool).

Unknown properties are reported, not silently ignored.  Nothing here writes a
tag, moves a file or edits a playlist.
