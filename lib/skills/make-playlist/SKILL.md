---
name: make-playlist
description: Build a new playlist from a mood, activity, artist seed, genre or length — and optionally play it.
example: create a smooth playlist for work
triggers: create a playlist, make a playlist, build a playlist, mix, erstelle eine playlist, mach eine playlist, playlist für, playlist zum, smooth, chill, konzentration, party
---
# Make a playlist

The core creative skill.  It blends:

1. **measured audio features** — tempo, loudness, dynamics, brightness,
   percussiveness, major/minor — analysed for every MP3 in the library;
2. **genre and shelf priors** — the same ones `Mood_*.m3u8` uses;
3. **what the app learned** — favourite artists, genres, moods, phrases;
4. **hard filters** — "no metal", "only jazz", an artist seed, a length.

Requests it understands:

- "create a smooth playlist" → Chill + Romantic + Focus, ~40 tracks
- "make me a two-hour party playlist" → Party, ~120 min
- "playlist for work" / "Playlist für die Arbeit" → Focus + Chill
- "something like Portishead" → audio/artist seed
- "30 jazz tracks, no vocals" → genre + count
- "more like this" → the current track is the seed
- "call it Late Night" → explicit name

**A queue, not a list, unless a list is named.**  "play something chill",
"spiele etwas Entspanntes", "one hour of metal" don't name a playlist, so the
selection is written as an ad-hoc queue under `Playlists/Own/` and played; the
answer says "say *save it* to keep it, or *discard it* to remove it".  The word
*playlist*, *liste*, *mix*, *save*, *called*/"as …", or a quoted name writes
`Playlists/Custom/<Name>.m3u8` directly (relative paths, so VLC and mpv can use
it outside the app) through `playlist_edit.py`, which writes a journal entry
first.  Existing hand-made lists are never overwritten unless the listener asks
to refine that exact list.

Arguments: `name`, `moods` (list of mood names), `count`, `duration_min`,
`seed` (track/artist), `include_genres`, `exclude_genres`, `autoplay`,
`overwrite`, `adhoc`.
