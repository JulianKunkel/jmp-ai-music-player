"""What the app changed recently, and how to take a playlist change back.

Every library tool appends to `library-work/journal/` before it touches a
file.  Those journals are the app's honest record, so the Activity view can
show them instead of inventing its own history.

Only a playlist is undoable from here: the journal stores the file's complete
previous content, so restoring one entry is exact and stays inside
`Playlists/`.  Tag writes and imports have their own rollback tools and are
listed read-only — this module never touches audio.

The reader only looks at the tail of each journal, on the byte (a tag value may
contain U+0085, which `str.splitlines()` would treat as a line break).
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path

#: how much of a journal is read for the newest entries
TAIL_BYTES = 256 * 1024

#: the journals the view shows: file -> (label, noun)
JOURNALS: dict[str, tuple[str, str]] = {
    "playlists.jsonl": ("playlist", "Playlist"),
    "youtube-import.jsonl": ("download import", "Import"),
    "add-file.jsonl": ("local import", "Import"),
    "field-writes.jsonl": ("tag write", "Tags"),
    "artwork.jsonl": ("artwork", "Artwork"),
    "artwork-edit.jsonl": ("cover edit", "Artwork"),
    "library-data.jsonl": ("library backup", "Backup"),
}

_PLAYLIST_ACTIONS = {
    "write": "saved",
    "replace": "replaced",
    "append": "extended",
    "remove": "edited",
    "delete": "deleted",
    "rename": "renamed",
    "undo": "undid",
}

UNDOABLE = {"playlists.jsonl"}


def _journal_path(root: Path, name: str) -> Path:
    return Path(root) / "library-work" / "journal" / name


def _tail_rows(path: Path, limit: int) -> list[dict]:
    """The newest `limit` parsed rows of a JSONL file."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - TAIL_BYTES)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    chunks = data.split(b"\n")
    if start > 0:
        chunks = chunks[1:]                 # the first line is probably cut
    rows: list[dict] = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        try:
            rows.append(json.loads(chunk))
        except ValueError:
            continue
    return rows[-max(1, limit):]


def _stamp(value: str) -> float:
    text = str(value or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _name(rel: str) -> str:
    return Path(str(rel or "")).stem or str(rel or "")


def _playlist_title(row: dict) -> tuple[str, str]:
    action = _PLAYLIST_ACTIONS.get(str(row.get("action") or ""), "changed")
    name = _name(str(row.get("path") or ""))
    detail = "the list"
    if action == "deleted":
        detail = "the list is gone; undo brings it back"
    elif not row.get("existed"):
        detail = "a new list; undo removes it"
    return f"{action} {name}", detail


def _import_title(row: dict) -> tuple[str, str]:
    if str(row.get("action") or "") == "duplicate":
        twin = _name(str(row.get("duplicate_of") or ""))
        return f"already had {twin}", "nothing was filed"
    target = row.get("to") or row.get("final") or row.get("path") or ""
    name = _name(str(target))
    tags = row.get("tags") or {}
    who = str(tags.get("artist") or row.get("artist") or "")
    return (f"filed {name}", who or str(row.get("url") or ""))


def _tag_title(row: dict) -> tuple[str, str]:
    name = _name(str(row.get("rel_path") or ""))
    fields = sorted((row.get("after") or {}).keys())
    return f"tags on {name}", ", ".join(fields) or "no field changed"


def _art_title(row: dict) -> tuple[str, str]:
    name = _name(str(row.get("rel_path") or ""))
    if row.get("where"):                    # an edit made in the window
        where = ("next to the album" if row.get("where") == "folder"
                 else "inside the file")
        action = "removed" if row.get("action") == "remove" else "put"
        return f"cover {action} for {name}", where
    return f"artwork for {name}", str(row.get("source") or "")


def _backup_title(row: dict) -> tuple[str, str]:
    name = Path(str(row.get("path") or "")).name
    return f"library data {row.get('op') or 'export'}", name


_TITLES = {
    "playlists.jsonl": _playlist_title,
    "youtube-import.jsonl": _import_title,
    "add-file.jsonl": _import_title,
    "field-writes.jsonl": _tag_title,
    "artwork.jsonl": _art_title,
    "artwork-edit.jsonl": _art_title,
    "library-data.jsonl": _backup_title,
}


def _entry(name: str, row: dict) -> dict:
    title, detail = _TITLES[name](row)
    return {
        "journal": name,
        "label": JOURNALS[name][0],
        "action": str(row.get("action") or row.get("op") or "change"),
        "ts": str(row.get("ts") or row.get("at") or ""),
        "title": title,
        "detail": str(detail or "")[:180],
        "path": str(row.get("path") or row.get("to") or ""),
        "undoable": False,
    }


def entries(root: Path, limit: int = 30) -> list[dict]:
    """The newest changes across the journals, newest first."""
    root = Path(root)
    try:
        limit = max(1, min(200, int(limit or 30)))
    except (TypeError, ValueError):
        limit = 30
    rows: list[dict] = []
    for name in JOURNALS:
        for raw in _tail_rows(_journal_path(root, name), limit):
            rows.append(_entry(name, raw))
    rows.sort(key=lambda row: _stamp(row["ts"]), reverse=True)

    # an undo may only touch the newest change of a given playlist: anything
    # older has been superseded, and restoring it would silently clobber that
    seen: set[str] = set()
    for row in rows:
        if row["journal"] not in UNDOABLE:
            continue
        key = row["path"]
        row["undoable"] = key not in seen
        seen.add(key)
    return rows[:limit]


def _playlist_rows(root: Path) -> list[dict]:
    return _tail_rows(_journal_path(root, "playlists.jsonl"), 100_000)


def _safe_playlist(rel: str) -> bool:
    text = str(rel or "")
    return (text.startswith("Playlists/")
            and Path(text).suffix.lower() in {".m3u8", ".m3u", ".pls"}
            and ".." not in Path(text).parts)


def _append_journal(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def undo_playlist(root: Path, ts: str) -> dict:
    """Restore the playlist content one journal entry replaced.

    The previous bytes are journalled before the restore, so the undo itself
    can be reversed.  Only the newest entry for a list may be undone, and only
    a path under `Playlists/` is ever written or removed.
    """
    root = Path(root)
    rows = _playlist_rows(root)
    target = None
    for index, row in enumerate(rows):
        if str(row.get("ts") or "") == str(ts or ""):
            target = (index, row)
            break
    if target is None:
        return {"ok": False, "message": "that change is no longer in the journal"}
    index, row = target
    rel = str(row.get("path") or "")
    if not _safe_playlist(rel):
        return {"ok": False, "message": "only a playlist can be undone here"}
    for later in rows[index + 1:]:
        if str(later.get("path") or "") == rel:
            return {"ok": False,
                    "message": "that list changed again since — undo the "
                               "newest change instead"}

    path = root / rel
    current = path.read_bytes() if path.is_file() else b""
    _append_journal(_journal_path(root, "playlists.jsonl"), {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": "undo",
        "path": rel,
        "existed": path.is_file(),
        "before_b64": base64.b64encode(current).decode("ascii"),
    })
    name = _name(rel)
    if row.get("existed"):
        before = base64.b64decode(str(row.get("before_b64") or ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(before)
        os.replace(tmp, path)
        return {"ok": True, "path": rel,
                "message": f"restored **{name}** to how it was before"}
    if path.is_file():
        path.unlink()
    return {"ok": True, "path": rel,
            "message": f"removed **{name}** again"}
