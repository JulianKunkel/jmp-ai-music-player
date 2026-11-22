---
name: import-audio
description: Audio import is provided by an optional build-time module; the open player ships without it.
example: is the import module installed?
triggers: download, import, add from a link, import module, lade herunter, integriere
---
# Audio import is a module

JulAIn Music Player itself never downloads audio and ships without that
feature.  A build may add an optional **import module** (its own repository,
compiled into `lib/modules` at build time); with one installed, this skill
covers searching a source, queueing an import and following its progress, and
the window shows the matching button and queue.  The module owns the whole
flow and its own journals.

Without a module the skill answers that this build has none and changes
nothing.  That is the normal state of the open player: play, search, playlists,
lyrics and the sound profile work exactly the same.

Arguments: `action` (search|download|status|retry|dismiss), `query`, `url`,
`urls`, `indexes`, `count`, `all`, `similar`, `artist`, `album`, `genre`.
