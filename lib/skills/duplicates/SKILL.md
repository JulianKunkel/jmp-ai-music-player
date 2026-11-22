---
name: duplicates
title: Review the duplicate groups
description: Show the duplicate groups the reports still list, both copies side by side. Read-only — nothing is moved or deleted.
triggers:
  - find duplicates
  - show duplicates
  - duplicate songs
  - which songs are duplicated
  - doppelte songs
  - duplikate
---

# Review the duplicate groups

The library was deduplicated already.  Exact copies were proven by the audio
payload, probable ones by length, bitrate, Shazam id or shared spectral peaks,
and every file that survived is either a different recording or a different
master.  What the reports still list is `action=review`: the same song on a
best-of *and* on its own album, where both copies are wanted.

This skill therefore **only reads** `library-work/reports/duplicates.csv` and
`probable-duplicates.csv`.  It has no delete path by design: removing a song
needs the listener's explicit approval and a journalled library tool.

## Arguments

| argument | meaning |
|---|---|
| `action` | `list` (default), `status`, `show`, `play` |
| `target` | a group number (`3`) or words from the artist/title |
| `number` | which copy to play with `action=play` |

## Actions

- **list** — every group, with each copy's album, length, bitrate, size, the
  copy the report marked `keep`, whether the file is still on disk, and how far
  the two lengths are apart.  Fewer than two seconds is the same-take rule,
  more than two is a different master.
- **status** — the counts only: groups, files, how many are on disk, reasons.
- **show** — one group in full; its copies become the last result, so
  "play number 2" then plays a copy.
- **play** — play one copy of a group.

## Examples

- "find duplicate songs"
- "which songs are in the library twice?"
- "show duplicate 3"
- "zeig mir die duplikate"
- "show duplicate 1", then "play number 2"

## What it never does

It does not delete, move, quarantine or retag a file, and it does not invent a
"better" copy: the two albums are usually both wanted.
