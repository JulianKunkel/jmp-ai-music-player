---
name: recommend
description: Suggest tracks for a mood, genre or situation — without saving anything.
example: what do you recommend for aggressive music?
triggers: recommend, recommendation, suggest, what should I listen to, what should I play, empfehlung, empfehl, was soll ich hören, surprise me, überrasch mich
---
# Recommend music

Answers "what do you recommend …?" with a short, playable suggestion instead of
creating a playlist.  It uses the same measured mood model as `make-playlist`:

- **mood words** — "aggressive" becomes Workout + Dark + Epic, "calm" becomes
  Chill + Focus, and the German words work the same way;
- **genres** — "recommend metal" or "recommend jazz" picks a mood blend for
  that genre and restricts the result to it;
- **what the app learned** — favourite artists, genres and moods;
- **a seed** — "recommend something like Portishead".

The tracks are remembered as the last recommendation, so the listener can say
"play number 2", "save that playlist" or "save it as Friday" afterwards.
Nothing is written until the suggestion is saved.

Arguments: `moods`, `include_genres`, `exclude_genres`, `seed`, `count`,
`autoplay` (play the suggestion immediately).
