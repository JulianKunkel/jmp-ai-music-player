---
name: search-library
description: Find tracks, albums and artists in the library by words from the tags or the path.
example: find everything by Die Ärzte
triggers: search, find, look for, show me, what do I have, suche, finde, zeig mir, was habe ich
---
# Search the library

Use when the listener asks **what** exists, not to start playback:

- "find everything by Portishead"
- "do I have anything with Welle in the title?"
- "show me the jazz albums"
- "suche nach Die Ärzte"

Search looks at title, artist, album artist, album, genre, `GROUPING` (the old
shelf) and the path, ignoring case and umlauts (`ärzte` == `arzte`).  Every
word of the query has to match somewhere, so `welle erdball` finds the band
while `welle` alone is broad.

The result is a numbered list.  The numbers stay valid for the next message,
so "play number 2" works without repeating the query.

Arguments: `query` (string), `limit` (integer, default 10).
