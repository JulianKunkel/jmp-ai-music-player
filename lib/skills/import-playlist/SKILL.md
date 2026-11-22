---
name: import-playlist
description: Import playlists from a folder of .m3u/.m3u8/.pls files, or from a public Spotify link, into Playlists/Custom/
triggers: import my playlists, import playlist folder, import spotify playlist, bring my playlists over, playlists aus ordner importieren
example: import the playlists from ~/Downloads
---

# Import playlists

Two sources, one destination: a list under `Playlists/Custom/`, written
through `playlist_edit.write_custom()` and journalled before it appears.

**A folder** — every `.m3u`, `.m3u8` and `.pls` in it, recursively by
default. Each entry is resolved against the live tree: an absolute path inside
the library, a path relative to the playlist file, a `file://` URI, a bare
file name (a playlist from another machine), or an `#EXTINF` "artist - title"
matched against the tags.

**Spotify** — `https://open.spotify.com/playlist/<id>` or just the id. The
public embed page is read (no Spotify account, no credentials), so the
playlist name, titles, artists and lengths come from Spotify while the audio
comes from the library. Shop decorations such as "- 2004 Remaster",
"(feat. …)" and "[From …]" are stripped before matching.

Nothing is downloaded and nothing is written over: an existing list gets the
next free name. Tracks the library does not hold are reported, so they can be
fetched through an optional download module and re-imported afterwards.

Arguments: `folder`, `spotify`, `recursive` (default true), `apply`
(false = preview only).
