---
name: endless
description: Keep the music going: grow the queue from the song that just played, and repeat it when nothing fitting is left.
example: endless mode
triggers: endless, endless mode, endlos, endlosmodus, never stop the music, keep the music going, stop endless
---
# Endless mode

A queue ends; endless mode does not.  Whenever the queue is about to run out
the app looks at the track that is playing **right now** and appends a few
tracks that sound like it.  Unlike radio mode there is no fixed seed, so the
listening drifts: the next batch follows the last song, not the one it started
from.

When nothing fitting is left the queue **repeats** instead of going silent.
That is a playback setting (mpv's `loop-playlist`), never a rewritten file.

**What it picks.**  The same blend as radio (`radio.candidates`):

1. nearest neighbours in the measured sound profile of the just-played track;
2. tracks with the same genre or old shelf, shuffled;
3. a little of everything else.

Tracks already played, whatever is queued, and anything under 20 seconds are
skipped, and every pick is re-checked on disk.

**Nothing in the library changes.**  The state is one JSON file,
`library-work/app/chat/endless.json`; no tag, no playlist and no audio file is
written.  The queue is remembered like any other queue, so *Continue* after a
restart rebuilds it.

Radio mode and endless mode are mutually exclusive: starting one stops the
other, so only one of them appends to the queue.

## Arguments

| action | what it does |
|---|---|
| `start` (default) | turn it on; if nothing plays, start the remembered or a random track |
| `stop` | turn it off; the queue that is playing stays as it is |
| `status` | is it on, what is the anchor, and how many tracks it has picked |
| `more` | add a batch right now |

## Examples

- "endless mode", "endless on", "endlosmodus", "mach endlos an"
- "never stop the music", "keep the music going"
- "stop endless", "endless off", "endlos aus"
- "is endless mode on?", "endless status"
- "endless more", "add more endless"

## Shuffle mode

`shuffle mode on` (or the shuffle icon in the now-playing header) arms random
order: every playlist that is started from then on begins shuffled.  "shuffle
mode off" / the icon again clears it.  A one-off "play this playlist shuffled"
still works without changing the mode.

## The window

Two icons in the now-playing header: the infinity turns endless mode on and
off (amber while it is repeating), the shuffle icon arms random playlists.
`GET /api/endless` reports the state and `POST /api/endless` with
`{"action": "start|stop|more"}` does the same as the chat; `POST /api/shuffle`
with `{"on": true|false}` toggles random playlists.
