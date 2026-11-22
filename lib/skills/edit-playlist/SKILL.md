---
name: edit-playlist
description: Show playlists, add tracks to a hand-made list, remove entries, or discard the ad-hoc queue — journalled, never destructive.
example: add this to my Sunday mix
triggers: add to playlist, put in playlist, remove from playlist, show playlist, list playlists, discard playlist, throw this away, verwirf die liste, füge hinzu, playlist anzeigen, welche playlists
---
# Edit a playlist

- "which playlists do I have?" → list every list with its track count
- "show Smooth" → the entries of one list
- "add this to Smooth" → the current or last-found track
- "remove track 3 from Smooth" → by its number in that list
- "discard it" / "throw this away" → remove the **ad-hoc** queue that the
  last request built (`Playlists/Own/`), music untouched

Only hand-made lists under `Playlists/Custom/` are edited.  The generated shelf
lists are rebuilt from `GROUPING` by `make_playlists.py`, so editing them would
be undone; the app refuses instead of pretending.  An ad-hoc list under
`Playlists/Own/` is not a saved list: say "save it" to keep it under a name,
or "discard it" to remove the copy.

Every change goes through `playlist_edit.py`, which writes the previous content
to `library-work/journal/playlists.jsonl` before the file changes.  Nothing is
ever deleted from disk without that journal line: deleting a saved list is only
available on the command line (`music pl delete`), not in the conversation.

Arguments: `action` (list|show|add|remove|discard), `playlist`, `tracks`
(paths or numbers from the last answer), `indexes`.
