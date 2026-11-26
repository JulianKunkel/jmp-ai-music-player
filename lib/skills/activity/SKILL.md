---
name: activity
description: Show what happened recently — playlists made, likes, skips and changes.
example: what did I do recently?
triggers: activity, activities, what did I do, past activities, verlauf, aktivitäten, was habe ich gemacht, letzte aktivitäten, recent changes, what changed, undo playlist change
---
# Recent activity

Reads the conversation profile in `library-work/app/chat/profile.json` and
renders it as a timeline:

- **playlists** made (name, track count, request);
- **events** — likes, dislikes, notes, learned phrases, skipped and listened;
- **recently played** tracks with how much of each was heard.

Nothing is changed by the `changes` action.  The same data feeds the Activity
panel in the window UI (`GET /api/activity`).  Entries are stored newest first
and capped by the profile itself (300 events, 120 recent plays, 80 playlists).

The `recent` list behind this timeline is the rolling window the learner sees
(last 120 tracks), there so the app knows what it just saw — not a record that
survives.  "What did I play today", "my most played", "what haven't I heard"
and the listening record as a whole are `library-overview · history`, which
reads the persistent play database; this skill answers "what did I do",
"what changed" and "undo".

**What changed / undo.**  `action: changes` reads the tail of the library
journals (`playlists.jsonl`, the import journal, `field-writes.jsonl`,
`add-file.jsonl`, `artwork.jsonl`, `library-data.jsonl`) and lists the newest
entries.  `action: undo` restores the newest playlist change from the journal's
own `before_b64` — only a file under `Playlists/`, only the newest change of
that list, and the undo is journalled before the write so it can be reversed.
Audio is never touched; tag writes and imports keep their own rollback tools.

Arguments: `limit` (default 20), `action` (`changes` or `undo`).
