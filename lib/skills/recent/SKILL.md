---
name: recent
title: What arrived lately
description: List the newest files in the library, newest first, with the honest caveat that "added" is the file's own time. Read-only.
triggers:
  - recently added
  - what's new
  - newest tracks
  - new arrivals
  - was ist neu
  - zuletzt hinzugefügt
---

# What arrived lately

The inventory has no download or import date, so the only honest signal is the
file's own `mtime`.  It moves when a file is retagged or renamed as well as
when it is really new, and every answer says so: read the list as **changed
recently**.

This skill only reads the inventory (`library-work/inventory/files.jsonl`),
which is cached until that file changes, and stats each path to see whether the
file is still there.  It writes nothing.

## Arguments

| argument | meaning |
|---|---|
| `action` | `list` (default), `status`, `play` |
| `limit` | how many entries (1–200, default 30) |
| `days` | only files changed in the last N days (1–3650) |

## Actions

- **list** — the newest files with artist, title, album, genre, how long ago
  they changed, and the library-relative path under each one (a click plays
  it).  Above the list: how many changed today, in 7 days, in 31 days and in
  the last year.
- **status** — the counts and the single newest file, no list.
- **play** — queue the newest entries (up to 20) as an ad-hoc mix, newest
  first.  A dry run starts nothing.

## Examples

- "what's new?"
- "which songs did I add most recently?"
- "show the newest tracks"
- "was ist neu"
- "zuletzt hinzugefügt"
- "what arrived this week" (`days: 7`)

## Guards

- A playback verb keeps a sentence a play request — "play something I added
  recently" is still `play-music`.
- "what did I add this month" stays the statistics slice `added`; this skill
  answers the explicit "what's new / newest tracks / recently added" request.
