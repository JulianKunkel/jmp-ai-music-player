---
name: show-playlists
description: List every playlist, grouped into hand-made, ad-hoc, shelf, mood, focus and sound-family lists.
example: show my playlists
triggers: show playlists, list playlists, my playlists, which playlists, what playlists, zeig meine playlists, welche playlists, playlists
---
# Show the playlists

Prints the library's playlists with their track counts, grouped by kind:

- **Mine** — `Playlists/Custom/`, the lists made in the chat or by hand;
- **Ad-hoc** — `Playlists/Own/`, the queues a request just built; say
  "save it" to keep one or "discard it" to remove it;
- **Shelves** — generated from `GROUPING`, one per old shelf;
- **Sound families** — `Playlists/Psycho/`, one per measured cluster;
- **Moods** — the ten `Mood_*` lists;
- **Focus** — `Mood_Focus_*` (deep, classical, beats, game, film).

This skill only reads.  The window UI shows the same list in the sidebar; the
chat answer is for the terminal and for "what have I got?".
