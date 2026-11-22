---
name: library-overview
description: Show the size, artists, genres, playlists and analysis state of the library.
example: how big is my library?
triggers: how many, stats, statistics, overview, library, was habe ich, wie viele, überblick
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
slice, `health`, `index`, `status`) and `focus` (`genres`, `artists`,
`albums`, `decades`, `formats`, `added`, `length`, `quality`, `playlists`,
`coverage`).

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
