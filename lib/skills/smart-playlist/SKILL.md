---
name: smart-playlist
description: Playlists that rebuild themselves from a rule (genre, year, added date, length, sound) instead of a frozen list of files.
example: make a smart playlist of 90s metal
triggers: smart playlist, smart playlists, rule-based playlist, intelligente playlist, smarte playlist, dynamische playlist, stays up to date, keep itself current
---
# Smart playlists

A normal playlist is a list of files: it says what was true when it was made.
A **smart** playlist is a rule — "Metal from the 1990s", "everything added this
month", "tracks longer than 8 minutes" — and the `.m3u8` under
`Playlists/Smart/` is rebuilt from the rule, so it stays true as the library
changes.

- "make a smart playlist of 90s metal" — genre + decade
- "a smart playlist of everything I added this month" — arrival date
- "create a smart playlist of the 50 newest metal tracks" — a limit and an order
- "make a smart playlist of tracks longer than 8 minutes, shuffled"
- "show my smart playlists" — the rules of every saved one
- "show the smart playlist 90s Metal" — one rule set in detail
- "refresh the smart playlists" — rebuild them all now
- "delete the smart playlist Old Stuff" — the file and the rule (the music stays)
- "play 90s Metal" — playing a smart list rebuilds it first

Rules the chat understands (all case- and umlaut-insensitive), matching the
keys in `lib/chat/smart.py`:

| key | examples |
|---|---|
| `genre`, `not_genre` | metal, jazz, no punk |
| `artist`, `album`, `grouping`, `codec` | by Portishead, album Dummy, shelf Bands, flac |
| `year_from`, `year_to`, `decade` | 90s, the nineties, 1995, from 1995, before 2000 |
| `added_days` | this week, this month, the last 30 days, recently added |
| `min_duration`, `max_duration` | longer than 8 minutes, under 2 min |
| `min_bitrate`, `max_bitrate` | at least 192 kbps, under 128 kbps |
| `axes` | a sound floor, e.g. `{"aggression": 0.8}` (percentile 0..1) |
| `sort` | newest (default), oldest, title, artist, duration, shuffle |
| `limit` | the 50 newest |

Every rule is a field the inventory already carries (or the profile database
for `axes`), so a rebuild reads no audio.  A rule set with **no filter** is
refused: "everything" is what `All.m3u8` is, and a smart list must never
silently become the whole library.  A list that already exists with different
rules is not overwritten unless the request says "replace"/"update" it.

A smart list is generated output, like the shelf and mood lists: the playlist
tools never edit or delete it, changing the rules and rebuilding is the way,
and every write (and delete) is journalled in
`library-work/journal/playlists.jsonl` with the previous content.  The rules
live in `library-work/app/chat/smart.json`.

Arguments: `action` (list|show|build|refresh|delete), `name`, `rules` (a dict
of the keys above), plus any single rule as its own argument.
