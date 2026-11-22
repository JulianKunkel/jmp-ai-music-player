---
name: index-library
description: Rebuild the library index — scan new or changed files, refresh the reports and regenerate the playlists.
example: index the library
triggers: index, re-index, reindex, scan the library, rescan, refresh the library, inventory, indizieren, neu einlesen
---
# Index the library

Runs the same pipeline as `music index`, as one detached background job:

1. `scripts/phase0_inventory.py scan` — tags, audio properties and checksums
   for every file that is new; a file already in the inventory is skipped, so
   a re-index normally only reads what changed;
2. `scripts/phase0_inventory.py report` — the CSV/Markdown reports;
3. `scripts/make_playlists.py --from-tree` — the canonical `.m3u8` lists.

It reads tags and audio and writes the inventory, the reports and the
generated playlists.  It never edits a music file, never deletes anything and
never touches a custom playlist.  Only one job runs at a time; asking again
returns the running one.

The window shows the progress in **Settings → Library index** and as a small
chip next to the library stats; the chat answers "how is the indexing going"
with the current stage, percentage and log tail.

A running job can be held and continued or stopped for good: *Pause* /
*Continue* and *Cancel* sit next to the button in Settings, and the chat takes
"pause the indexing", "resume the indexing" and "stop the index" (German
"pausiere das Indizieren", "brich das Indizieren ab").  Pause is `SIGSTOP`, so
the scan keeps its pid and its place; resume is `SIGCONT`; cancel is `SIGTERM`
(after `SIGCONT`, since a stopped process does not act on a signal) and a stage
that ignores it is killed after 15 s.  A cancelled run keeps every track it had
already written and the next run skips those, and a half-written last inventory
line is trimmed.

Arguments: `action` — `index` to start (also `rebuild`, `scan`, `rescan`,
`refresh`), `status` for the progress, `pause`/`resume`/`cancel` to control a
running job.  Nothing to fill in.
