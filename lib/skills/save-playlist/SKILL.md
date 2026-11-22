---
name: save-playlist
description: Keep the last recommendation, search or ad-hoc queue as a custom playlist.
example: save that playlist as Late Night
triggers: save that playlist, save this playlist, save it, keep that, save as, speichere das, sichere das, behalte das
---
# Save the last selection

Turns whatever is on the table into a real custom playlist:

1. the **ad-hoc queue** a request just built ("play something smooth",
   "30 tracks of rock") — it already exists as a file under `Playlists/Own/`,
2. the tracks of the last **recommendation**,
3. otherwise the last **search result**, the last shown playlist or the current
   track.

An ad-hoc queue wins over an older recommendation when it is newer, and "save
it"/"keep this" always means that queue.  Saving writes the list to
`Playlists/Custom/<Name>.m3u8` and then removes the ad-hoc copy (the delete is
journalled too, so `playlist_edit.py --rollback` brings it back).

The name comes from "as …"/"als …", a quoted name, the ad-hoc list's own name,
or the mood blend; without one it uses the strongest mood ("Chill",
"Workout", …).  If a list of that name already exists, a free `_2` name is used
instead of overwriting it — say "overwrite" to replace it.

The write goes through `playlist_edit.write_custom()`, so the previous content
is journalled in `library-work/journal/playlists.jsonl` before the file changes.
Paths inside the playlist are relative, so VLC and mpv can use it too.

Arguments: `name`, `overwrite`, `own`.
