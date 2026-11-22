"""Write the timed lyrics as an `.lrc` sidecar — the journal comes first.

A player that does not know this app still shows the words when they sit next
to the audio as `<name>.lrc`, so this is the **one** place the app puts a new
file inside the music tree.  It follows the rule every other writer uses: the
previous state goes to `library-work/journal/lyrics-lrc.jsonl` and is fsync'd
**before** the file appears, so an export can always be rolled back.

The lyrics themselves stay in `library-work/app/lyrics/lyrics.db`; the
sidecar is a copy for other players, never the source of truth.  No tag and no
audio file is touched.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import lyrics as lyrics_mod

#: where every export is recorded, relative to the library root
JOURNAL = ("library-work", "journal", "lyrics-lrc.jsonl")

#: the extension an LRC file carries
SUFFIX = ".lrc"


def journal_path(root: Path | str) -> Path:
    return Path(root).joinpath(*JOURNAL)


def target_for(root: Path | str, rel: str, out: str = "") -> Path:
    """Where the `.lrc` goes: next to the audio, or the path that was asked for.

    A relative `out` is read from the library root, so "export it to
    Lyrics/foo.lrc" cannot depend on the shell's working directory.
    """
    if out:
        candidate = Path(out).expanduser()
        if not candidate.is_absolute():
            candidate = Path(root) / candidate
        return candidate
    return (Path(root) / rel).with_suffix(SUFFIX)


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _journal(root: Path | str, entry: dict) -> Path:
    """Append one record and fsync it, so the write below is always recoverable."""
    path = journal_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def export(root: Path | str, lyrics: "lyrics_mod.Lyrics", rel: str,
           out: str = "", overwrite: bool = False) -> dict:
    """Write one song's lyrics as an LRC file; returns what happened.

    ``out`` may name any path; without one the sidecar lands next to the
    audio.  An existing file is only replaced with ``overwrite``, and its
    bytes are in the journal either way.
    """
    root = Path(root)
    rel = str(rel or "").strip()
    record = lyrics.track(rel)
    lines = lyrics.lines(rel)
    if record is None or not lines:
        return {"error": "That song has no timed lyrics yet — transcribe it "
                         "first, then export it again."}
    text = lyrics_mod.render_lrc(record, lines)
    data = text.encode("utf-8")
    target = target_for(root, rel, out)
    previous = b""
    existed = target.exists()
    if existed:
        if not overwrite:
            return {"error": f"{target} already exists — say “overwrite” to "
                             "replace it.", "path": str(target),
                    "exists": True, "lines": len(lines)}
        try:
            previous = target.read_bytes()
        except OSError as exc:
            return {"error": f"cannot read {target}: {exc}"}
    try:
        inside = target.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        inside = ""
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rel": rel,
        "path": str(target),
        "inside": inside,
        "created": not existed,
        "replaced": existed,
        "size": len(data),
        "md5": _md5(data),
        "previous_size": len(previous),
        "previous_md5": _md5(previous) if previous else "",
        "previous_b64": base64.b64encode(previous).decode("ascii"),
        "lines": len(lines),
        "offset": lyrics.offset(rel),
    }
    journal = _journal(root, entry)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("." + target.name + ".tmp")
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        return {"error": f"cannot write {target}: {exc}",
                "path": str(target), "journal": str(journal)}
    return {"path": str(target), "name": target.name, "rel": rel,
            "inside": inside, "lines": len(lines), "bytes": len(data),
            "offset": float(lyrics.offset(rel) or 0),
            "replaced": existed, "journal": str(journal)}


def entries(root: Path | str, limit: int = 200) -> list[dict]:
    """The newest journal records first (read with bytes, not splitlines)."""
    path = journal_path(root)
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(raw.split(b"\n")):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
        if len(out) >= limit:
            break
    return out


def rollback(root: Path | str, all_entries: bool = False) -> dict:
    """Undo the exports: restore a replaced file, remove a created one.

    Only a sidecar this tool wrote is touched.  The newest record per path
    wins, because an older export has been superseded by a newer one.
    """
    root = Path(root)
    records = entries(root, limit=1_000_000 if all_entries else 200)
    restored = removed = skipped = 0
    last: dict[str, dict] = {}
    for entry in records:
        path = str(entry.get("path") or "")
        if path and path not in last:
            last[path] = entry
    for path, entry in last.items():
        target = Path(path)
        if not entry.get("inside"):
            skipped += 1
            continue
        try:
            if target.is_file():
                live = _md5(target.read_bytes())
            else:
                live = ""
        except OSError:
            skipped += 1
            continue
        if live != str(entry.get("md5") or ""):
            skipped += 1               # somebody edited it since: leave it
            continue
        try:
            if entry.get("replaced") and entry.get("previous_b64"):
                target.write_bytes(base64.b64decode(entry["previous_b64"]))
                restored += 1
            elif entry.get("created") and target.exists():
                target.unlink()
                removed += 1
            else:
                skipped += 1
        except (OSError, ValueError):
            skipped += 1
    return {"restored": restored, "removed": removed, "skipped": skipped,
            "journal": str(journal_path(root))}
