---
name: learn-preferences
description: Remember what the listener likes, dislikes, calls a mood, or wants kept in mind.
example: I like this song
triggers: i like, i love, i don't like, i hate, remember, note, when i say, lerne, merke, ich mag, ich hasse, wenn ich sage
---
# Learn from the listener

The profile is a JSON file (`library-work/app/chat/profile.json`), not a black
box.  It holds weights for artists, genres, moods and keywords, free-text notes
and learned phrases.

What teaches it:

- **explicit** — "I like this", "not this one", "I love Portishead",
  "remember that I prefer instrumental music while working";
- **a learned phrase** — "when I say smooth I mean chill and jazz" makes
  *smooth* select that blend from then on;
- **implicit** — a track heard past 65 % counts as a small like, one skipped
  in the first 15 % as a small dislike.  Implicit signals weigh roughly a
  tenth of an explicit one, so one skip cannot ruin an artist.

Selection reads the weights through `tanh`, which keeps a single enthusiasm
from dominating the measured audio features.  `/learn` or "what have you
learned?" shows the current profile.

Arguments: `kind` (like|dislike|note|mapping|forget), `value`, `moods`.
