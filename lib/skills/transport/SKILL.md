---
name: transport
description: Pause, resume, skip, go back, stop, seek or change the volume of the running player.
example: turn the volume up
triggers: pause, stop, resume, continue, next, skip, previous, back, louder, quieter, volume, pausiere, weiter, nächste, zurück, lauter, leiser
---
# Control the player

Short commands while something is playing:

| request | action |
|---|---|
| "pause" / "pausiere" | `pause` |
| "continue" / "weiter" | `resume` |
| "next" / "skip" / "nächster" | `next` |
| "previous" / "zurück" | `prev` |
| "stop" | `stop` |
| "louder" / "leiser" | `volume` with a new level |
| "jump 30 seconds forward" | `seek` with +30 |

`status` / "what is playing?" is answered from the same mpv daemon: title,
position, queue entry and shuffle state.

Arguments: `action` (pause|resume|toggle|stop|next|prev|volume|seek),
`value` (number, for volume or seconds).
