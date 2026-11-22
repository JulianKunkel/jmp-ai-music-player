"""Endless mode: the queue keeps growing from the song that just played.

Radio mode seeds one queue from one track; endless mode is the same idea
without a fixed seed.  Whenever the queue is about to run out the engine takes
the track that is playing *right now* and appends a few tracks that sound like
it, so the listening drifts from song to song instead of jumping back to an
old anchor.

When nothing fitting is left the mode falls back to **repeating the queue**:
the music keeps going, which is the one thing the listener asked for.  It is
a playback setting (mpv's `loop-playlist`), never a rewrite of a file.

Nothing here touches the library.  The state is one small JSON file in
`library-work/app/chat/endless.json`; picks are re-checked against the disk by
`radio.candidates()`, so a stale inventory row can never play.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .catalog import library_rel

#: the state file, relative to the library root
STATE = ("library-work", "app", "chat", "endless.json")

#: how many tracks one top-up appends
BATCH = 5

#: how many of the newest picks are never repeated
HISTORY = 120

#: tracks shorter than this are interludes and poor anchors
MIN_SECONDS = 20


def path(root: Path | str) -> Path:
    return Path(root).joinpath(*STATE)


def load(root: Path | str) -> dict:
    """The stored endless state; an off state when there is none."""
    try:
        data = json.loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"on": False, "repeat": False, "played": []}
    if not isinstance(data, dict):
        return {"on": False, "repeat": False, "played": []}
    if not isinstance(data.get("played"), list):
        data["played"] = []
    data["on"] = bool(data.get("on"))
    data["repeat"] = bool(data.get("repeat"))
    return data


def save(root: Path | str, data: dict) -> dict:
    """Write the state atomically, trimmed to the history length."""
    target = path(root)
    payload = {
        "on": bool(data.get("on")),
        "repeat": bool(data.get("repeat")),
        "last": str(data.get("last") or ""),
        "played": [str(rel) for rel in (data.get("played") or [])
                   if str(rel).strip()][-HISTORY:],
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("." + target.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError:
        pass
    return payload


def active(root: Path | str) -> bool:
    """True while endless mode is on (a dry run stores nothing, so it is off)."""
    return bool(load(root).get("on"))


def start(root: Path | str, last: str = "") -> dict:
    """Turn endless mode on, optionally remembering the track it grows from."""
    data = load(root)
    data["on"] = True
    if last:
        data["last"] = library_rel(last)
    return save(root, data)


def stop(root: Path | str) -> bool:
    """Turn the mode off; True when it was on before."""
    data = load(root)
    was = bool(data.get("on"))
    data["on"] = False
    data["repeat"] = False
    save(root, data)
    return was


def set_last(root: Path | str, rel: str) -> dict:
    """Remember the track that just played; it seeds the next batch."""
    rel = library_rel(str(rel or ""))
    data = load(root)
    if rel and rel != data.get("last"):
        data["last"] = rel
        save(root, data)
    return data


def mark_repeat(root: Path | str, value: bool) -> dict:
    """Record that the queue is looping because no fitting track was found."""
    data = load(root)
    data["repeat"] = bool(value)
    return save(root, data)


def remember(root: Path | str, rels) -> dict:
    """Add picks to the history so they are not chosen again soon."""
    data = load(root)
    played = list(data.get("played") or [])
    for rel in rels:
        rel = library_rel(str(rel or ""))
        if rel and rel not in played:
            played.append(rel)
    data["played"] = played
    return save(root, data)


def info(root: Path | str, catalog=None) -> dict:
    """The small picture the window and `/api/state` show."""
    data = load(root)
    last = str(data.get("last") or "")
    track = catalog.track(last) if (catalog is not None and last) else None
    return {
        "on": bool(data.get("on")),
        "repeat": bool(data.get("repeat")),
        "last": last,
        "last_label": (track.label() if track is not None else ""),
        "played": len(data.get("played") or []),
        "batch": BATCH,
    }
