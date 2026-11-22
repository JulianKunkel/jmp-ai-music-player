---
name: play-music
description: Play the whole library, a playlist, a mood list, an album or a search hit through mpv.
example: play the whole library shuffled
triggers: play, start, listen, put on, shuffle, spiel, abspielen, hör, shuffle, zufällig
---
# Play music

Use for every playback request:

- "play the whole library shuffled" / "spiele alles zufällig"
- "play Bands" — a playlist by name, case-insensitive
- "play the chill list"
- "play something by Portishead"
- "queue this after the current track" (`append`)
- "play number 3" — a hit from the previous answer

The target is resolved in this order: URL, existing file path, playlist name,
then search.  A search with one hit plays immediately; several hits are listed
and the numbers are kept for the next message.

Playback runs in one headless mpv daemon (`MPV_SOCKET`, default
`/tmp/mpv.sock`), so playback survives between chat messages.  The whole
library means `Playlists/All.m3u8`, not a random subset.

Arguments: `target` (string), `shuffle` (bool), `append` (bool),
`whole_library` (bool).
