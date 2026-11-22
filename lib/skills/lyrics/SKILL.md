---
name: lyrics
description: Show a song's lyrics line by line, find songs by a half-remembered line, or transcribe songs locally with Whisper.
example: something with a boat and fast
triggers: lyrics, lyric, songtext, songtexte, liedtext, gesangstext, textzeile, line, verse, lyrics of, what are the lyrics, show the lyrics, list the lyrics, read the lyrics, words of this song, sing along, which line is playing, where are we in the lyrics, next line, songs about, mentions, erwähnt, transcribe, transkribiere, transcribe the lyrics of <file>, transcription, untertitel, subtitles, speech to text, how is the transcription going, transcribe the library
---
# Lyrics

The words come from a **local** transcription: `scripts/transcribe_lyrics.py`
runs faster-whisper on this machine (GPU when the driver offers one, otherwise
CPU) and stores timed lines in `library-work/app/lyrics/lyrics.db` (SQLite,
FTS5 index).  No audio and no text leaves the machine, and nothing is sent to a
lyrics website.

## What the skill answers

- **show** — the lines of the current song (or the one named after "of", or a
  file name such as `Merlin_wooden.mp3`), each with its timestamp.  Works for
  any transcribed song, whether it is playing or not.  The window's lyrics
  panel marks the line being sung as the playhead moves and seeks when a line
  is clicked.
- **now / next** — the line at the playhead with two neighbours: "which line is
  playing", "where are we in the lyrics", "wie weit sind wir im Text", and the
  following line for "what's the next line", "was kommt als Nächstes".
  "sing along" is a show of the current song.  Without playback it says so and
  starts at the beginning instead of inventing a position.
- **search** — a rough line, a word, or a fragment ("find songs about a boat",
  "which song mentions pizza").  FTS5 matches stems, so `sail` finds `sailing`;
  the answer quotes the matched line with its timestamp.  The hits become the
  current result list, so "play number 1" works.
- **search + sound property** — the same sentence may name a measured property:
  "something with a boat and fast" searches the lyrics for *boat* and orders
  those hits by the Speed axis (and any other property: slow, aggressive,
  happy, electronic, quiet, …).  The words pick the songs, the profile orders
  them; a track with no measurement keeps its lyric order at the end.
- **transcribe** — starts the background job for the current song, for a named
  one ("transcribe the lyrics of Merlin_wooden.mp3", a title, or a path), or
  for the next songs that have no lyrics yet (`all`/`missing`), optionally
  again (`refresh`).
  A job is detached: closing the window does not stop it, and the app adopts a
  running one instead of starting a second.
- **status** — how many songs are transcribed, how many lines, which backend
  (GPU or CPU), and the log tail of the running job.

## Requirements

A local transcriber: `scripts/setup_asr.sh` creates `scripts/asr-venv` with
`faster-whisper` (about 450 MB, one time) and picks the model — `medium` on a
CUDA GPU, `small` on CPU.  The container ships its own copy of the backend
(CPU only, because Docker here has no nvidia runtime).  Models download on
first use into `library-work/app/lyrics/models`, so they live in the library
and survive a rebuild.

`vad` is off by default: the bundled Silero VAD finds almost no speech on
music here, so turning it on would throw the song away.  Consecutive duplicate
lines are collapsed and a repetition penalty is set, so an instrumental track
does not produce the same sentence forty times.

## Where it is in the app

`#lyrics-toggle` in the Now-playing header shows and hides the panel; the
sparkle button transcribes the current song and `+` queues the next ones.
API: `GET /api/lyrics?rel=…`, `GET /api/lyrics/update`,
`POST /api/lyrics/transcribe {rel|missing, refresh}`,
`POST /api/lyrics/setup {cpu}`; `/api/state` carries `lyrics`
(transcribed count, backend, job progress) and the `lyrics_panel` setting.

Arguments: `action` (show|now|next|search|transcribe|setup|status), `track`,
`terms`, `limit`, `refresh`, `all`.
