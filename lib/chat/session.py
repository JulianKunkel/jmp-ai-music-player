"""What was playing when the app last stopped.

A player that forgets is a demo: this keeps the queue, the track and the
position in `library-work/app/chat/session.json` — app state, never a tag and
never a music file — so the window can offer **Continue** and the chat can be
told "continue where I left off".

Two kinds of queue are stored, because mpv is not the only thing that can lose
its playlist:

- a **playlist** or a single **track** (a path inside the library, so it can be
  handed to the player again),
- a **temp** queue (a mood mix the engine generated under
  `library-work/app/chat/queues/`).  Its entries are kept too, so the queue
  can be rebuilt after the old file was pruned.

The file is written atomically and only ever replaced, so a crash mid-write
leaves the previous session intact.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

#: bump when the meaning of a field changes; an unknown version is ignored
VERSION = 1

#: a queue longer than this keeps its head only (a temp mood mix is ~30-300)
MAX_ENTRIES = 500


def path(root: Path | str) -> Path:
    return Path(root) / "library-work" / "app" / "chat" / "session.json"


def load(root: Path | str) -> dict | None:
    """The remembered session, or None when there is nothing usable."""
    try:
        data = json.loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("rel"):
        return None
    if int(data.get("version") or 0) != VERSION:
        return None
    return data


def save(root: Path | str, data: dict) -> dict:
    """Write the session atomically; returns what was stored."""
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in (data or {}).items()
               if key in {"kind", "target", "entries", "rel", "position",
                          "duration", "index", "count", "shuffle", "artist",
                          "title", "label"}}
    payload["version"] = VERSION
    payload["updated"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    if isinstance(payload.get("entries"), list):
        payload["entries"] = [str(rel) for rel in payload["entries"]
                              if str(rel).strip()][:MAX_ENTRIES]
    temporary = target.with_name("." + target.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return payload


def clear(root: Path | str) -> bool:
    """Forget the session; True when something was removed."""
    try:
        path(root).unlink()
        return True
    except OSError:
        return False


def describe(data: dict) -> str:
    """One line a listener can recognise: "Artist — Title · 0:42"."""
    data = data or {}
    artist = str(data.get("artist") or "").strip()
    title = str(data.get("title") or "").strip()
    if not title:
        title = Path(str(data.get("rel") or "")).stem.replace("_", " ")
    label = f"{artist} — {title}" if artist else title
    try:
        position = int(float(data.get("position") or 0))
    except (TypeError, ValueError):
        position = 0
    if position > 0:
        label += f" · {position // 60}:{position % 60:02d}"
    return label
