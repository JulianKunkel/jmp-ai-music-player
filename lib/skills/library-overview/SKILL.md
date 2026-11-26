---
name: library-overview
description: Show the size, artists, genres, playlists and analysis state of the library.
example: how big is my library?
triggers: how many, stats, statistics, overview, library, was habe ich, wie viele, überblick, version, release, listening history, play history, most played, never played, meistgespielt
---
# Library overview

Answers "what is in here?" with numbers read from the live inventory:

- audio files, total hours, distinct artists;
- the eight most common genres;
- playlist counts (shelf, mood, focus, custom);
- how many tracks have measured mood features.

It is read-only and cheap: the inventory is already on disk.  If a freshly
imported file is missing from the numbers, the library has not been re-indexed
yet (`music index`, or the import does it).

Arguments: `action` (`show` for this overview, `stats` for the statistics
slice, `history` for the listening record, `formats` for what the player can
play, `health`, `index`, `status`, `version` for the running release) and
`focus` (`genres`, `artists`, `albums`, `decades`, `formats`, `added`,
`length`, `quality`, `playlists`, `coverage`; for `history` also `top`,
`recent`, `forgotten`, `never`).

**Listening history.**  `action: history` (also "listening history", "play
history", "what did I play", "what have I listened to", "my most played",
"show my listening statistics", "was habe ich gespielt/gehört",
"meistgespielt") reads `library-work/app/chat/plays.db` through
`chat/history.py` — one row per *finished* listen, written by the player when a
track changes:

- a listen counts from 20 s, or half of a shorter track, and never under 5 s,
  so a skip is not a play;
- a same-file replay (the playhead jumps back to the start) is a second play;
- nothing here writes audio, a tag or a playlist; the database is app state.

`focus` picks the slice: `top` for the most played tracks, artists and genres,
`recent` for the newest listens, `forgotten` for tracks not heard in
`forgotten_days` (default 90; "not in a year" sets 365), `never` for catalogue
tracks the record has no play for.  `days` narrows to a window ("this month"),
and `play` queues the listed tracks (shuffled) instead of only showing them.

**The record starts when the feature first ran**, so "never played" means *not
played since <date>* — every answer names that date instead of pretending the
history is complete.  Files that have left the library stay counted but are
listed apart as gone.  The window has the same view under Settings → Library →
**Listening history** (`GET /api/history?days=&limit=&forgotten_days=`).

The same record feeds smart playlists: "a smart playlist of songs I never
played" ("not heard in a year", "played at least 5 times") becomes a rule set
with `max_plays`/`min_plays`/`not_played_days`, rebuilt like any other
rule-based list.

**Which build is this?**  `action: version` (also "which version of the app am
I running", "what release is this", "welche version läuft") answers with the
number from the `VERSION` file that ships next to the code, the path it was
read from and the Python it runs on.  The same number is what the window shows
next to its name, what `/api/state` reports and what the HTTP `Server` header
uses; a title that merely contains the word ("play the acoustic version") is
playback, not this.

**What can you play?**  `action: formats` (also "can you play spc files?",
"do you support flac", "which formats do you know", "kannst du spc
abspielen") answers from `chat/formats.py`, never by searching titles — a
format is a property of the player, not a title.  Three honest cases:

- **playable** — mpv is the player and ffmpeg its decoder, so any container
  ffmpeg knows plays, even when the library holds none of it (`.flac`);
- **indexed, not playable** — ffmpeg has no decoder for `.sid`, `.ym` or
  `.play`, so those files are listed but a click cannot make a sound;
- **chiptune** — `.spc`/`.mod`/`.xm`/`.it`/`.s3m` play, but a fixed header
  replaces the tag block: no title, artist, genre or cover art, so they are
  found through `reports/chiptune-catalogue.csv`, not by name.

`format` narrows it to one extension or family word ("spc", "sid",
"chiptune", "tracker", "lossless"); without it the whole table is listed with
the library's per-format counts.  A question that names a performer stays
playback ("can you play some jazz"), and the short names that are ordinary
words (`.mod`, `.sid`, `.it`) only count as formats with a dot, a file word,
or a "support" word.  The language-model tool surface has the same answer as
`describe_formats`.

**Health check.**  `action: health` (also "is the library healthy", "check the
library", "what is broken", "prüfe die bibliothek") reads the inventory, the
sound-profile and lyrics databases, the playlists, the duplicate reports and
the journals and reports tag gaps, artwork coverage, decode errors, index
drift and how many tracks are measured/transcribed.  It also runs the
read-only inventory audit (`chat/inventory.py`), so missing and changed files
are named.  Nothing is written; the window's Settings → Library has the same
check under **Library health**, and `GET /api/health` serves the data.

**Statistics.**  `action: stats` (also "library statistics", "show my
statistics", "statistik") reads `chat/stats.py`, which counts the inventory
rows only — no audio is opened, nothing is written:

- tracks, taggable tracks, chiptunes, hours, size, lossless files and the
  average bitrate;
- the most common genres, the decades from the `date` tag, the formats, the
  distinct artists and albums and the busiest performers;
- what arrived in the last 7 / 31 / 366 days and the newest five tracks;
- the longest, shortest and average track;
- playlist counts per kind, and the tag/artwork/profile/lyrics coverage.

A `focus` narrows the answer: "which genres do I have most of", "the decades",
"what formats do I have", "how is the sound quality", "what did I add this
month", "the longest track", "how many playlists do I have", "how complete is
the library" (`coverage`).  A sentence about the track that is playing is not
a statistics request, and a playback verb keeps "play something I added
recently" a play request.  `GET /api/stats` serves the same data and the
window's Settings → Library has it under **Library statistics**.
