---
name: psycho-profile
description: Show a song's measured sound properties, the net diagram, the sound families, or find music that fits a changed profile.
example: show the properties of this song
triggers: properties, eigenschaften, profile, klangprofil, sound profile, psychoacoustic, psychoakustik, net diagram, netzdiagramm, radar, bpm, tempo, how fast, how loud, how aggressive, families, familien, similar but faster, more aggressive, less speed
---
# Psychoacoustic profile

Every track is measured by `scripts/psycho_features.py` (ffmpeg decode + numpy:
tempo, loudness, dynamics, spectral centroid/rolloff, flatness, zero-crossings,
onset flux, onset rate, pulse clarity, Sethares dissonance, band shares,
spectral entropy, Krumhansl major/minor) and stored in the SQLite database
`library-work/app/psycho/features.db`.

`scripts/psycho_profile.py` turns those raw columns into ten axes, each a
percentile rank **over this library** (0.5 = average, 0.9 = top 10 %):

| Axis | Wording | From |
|---|---|---|
| Speed | slow ↔ fast | tempo |
| Energy | quiet ↔ loud | RMS level, onsets, flux, high band |
| Aggression | gentle ↔ aggressive | level, high band, dissonance, flatness, zero-crossings, speed, minor |
| Brightness | dark ↔ bright | centroid, rolloff |
| Danceability | freeform ↔ danceable | pulse clarity, onsets, tempo near 120 |
| Valence | melancholy ↔ upbeat | major mode, brightness, speed, low roughness |
| Calmness | intense ↔ calm | quiet, few onsets, slow, wide dynamics |
| Density | sparse ↔ dense | onsets per second, flux |
| Roughness | smooth ↔ rough | Sethares dissonance, flatness, zero-crossings |
| Harmony | dissonant ↔ harmonic | key clarity, tonal spectrum, low roughness |

The same script groups the library into k-means families over the ten axes,
names each family by the axes where it differs from the others, and writes one
playlist per family to `Playlists/Psycho/` (kind `psycho`).

## What the skill answers

- **show** — the properties of the current track (or the one named after "of").
  Every axis with a bar and its word, plus the raw measurements and the family.
- **families** — the sound families with size, strongest axes and playlist.
- **similar** — a track plus axis deltas (`{"speed": 0.2}`), returning the
  nearest profiles.  Changed axes are weighted 1.6×, so moving one dimension
  really changes the ranking instead of being averaged away.

## Where it is in the app

The **Profile** button in the window opens the net diagram: the current song as
the violet polygon, your slider changes as the dashed amber one, and the match
list below updates as you move a dimension.  A click on a match plays it.

API: `GET /api/psycho?rel=…`,
`POST /api/psycho/similar {rel, axes, adjustments, limit}`; `/api/state` carries
`psycho.ready`, the profile count and the family list.

Nothing here writes a tag, moves a file or edits a playlist.  The only writes in
this area happen when you run the two scripts above, and those write the
database, `library-work/reports/psycho-*.{md,csv}` and `Playlists/Psycho/`.

Arguments: `action` (show|similar|families), `track`, `adjustments`, `count`.
