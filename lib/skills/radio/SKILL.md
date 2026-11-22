---
name: radio
description: Keep the music going: a queue that tops itself up from the sound of a seed song.
example: start the radio
triggers: radio, radio on, radio aus, start the radio, radio mode, more radio
---
# Radio mode

A queue runs out; the radio does not.  Turn it on and the app appends a few
tracks whenever the queue is about to end, so the music keeps playing without
another request.

**What it picks.**  A blend, not a shuffle of the whole library:

1. the **measured sound** of the seed track — nearest neighbours in the sound
   profile (`psycho.similar`), so a fast, aggressive song leads to fast,
   aggressive music whatever the tags say;
2. the **genre and old shelf** of the seed, so the drift stays plausible and
   the next track does not come from an unrelated folder;
3. a little of everything else, so the radio can leave the folder at all.

Tracks already played, whatever is queued, and anything under 20 seconds are
skipped.  Every pick is re-checked on disk, so an inventory row whose file was
deleted or renamed can never be queued.

**Nothing in the library changes.**  The state is one JSON file,
`library-work/app/chat/radio.json`; no tag, no playlist and no audio file is
written.  The queue it builds is remembered like any other queue, so
*Continue* after a restart rebuilds it.

## Arguments

`action`:

| action | what it does |
|---|---|
| `start` | turn the radio on and start the seed (default: the song that is playing) |
| `stop` | turn it off; the queue that is playing stays as it is |
| `status` | is it on, from what, and how many tracks it has picked |
| `more` | add a batch right now |

`target` seeds the radio from a track, an artist or a genre:
"start the radio from this song", "radio based on Die Ärzte",
"start radio from Rock".  Without one it uses what is playing, and with
nothing playing it picks a track from the library.

## Examples

- "start the radio", "radio on", "mach das radio an"
- "start a radio from this song", "radio based on Rock"
- "stop the radio", "turn off the radio", "radio aus"
- "is the radio on?", "radio status"
- "more radio", "keep the radio going"

## The window

The radio icon in the now-playing header turns it on and off; it is lit while
the radio runs.  `GET /api/radio` reports the state, `POST /api/radio` with
`{"action": "start|stop|more", "target": …}` does the same as the chat.
