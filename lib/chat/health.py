"""A one-page answer to "is the library healthy?".

Everything here is read-only and derived from data the library already keeps:
the inventory (tags, artwork flags, decode result), the sound-profile and
lyrics databases, the playlists folder, the duplicate reports and the journals.
No audio file is opened and nothing is written.

The inventory is the slow part (17k rows), so it is read once and cached until
its size or mtime moves; the databases are small and read directly.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import inventory, recent

#: taggable audio: a chiptune has no tag field to fill, so it is not counted
TAGGABLE = {".mp3", ".mp2", ".m4a", ".mp4", ".opus", ".ogg", ".flac",
            ".wav", ".wma", ".aac"}

_CACHE: dict[tuple[str, int, int], dict] = {}


def _inventory_path(root: Path) -> Path:
    return Path(root) / "library-work" / "inventory" / "files.jsonl"


def _rows(root: Path) -> list[dict]:
    path = _inventory_path(root)
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (str(path), stat.st_size, int(stat.st_mtime))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached["rows"]
    rows: list[dict] = []
    with path.open("rb") as handle:
        for chunk in handle:
            if not chunk.strip():
                continue
            try:
                rows.append(json.loads(chunk))
            except ValueError:
                continue
    _CACHE.clear()
    _CACHE[key] = {"rows": rows}
    return rows


def _db_counts(path: Path) -> dict:
    """Track counts from a small SQLite database, tolerantly."""
    result = {"total": 0, "with_data": 0, "failed": 0, "exists": path.is_file()}
    if not path.is_file():
        return result
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            table = "audio" if "audio" in tables else (
                "track" if "track" in tables else "")
            if not table:
                return result
            result["total"] = int(conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            columns = {row[1] for row in conn.execute(
                f"PRAGMA table_info({table})")}
            for name in ("lufs", "text"):
                if name in columns:
                    result["with_data"] = int(conn.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        f"WHERE {name} IS NOT NULL AND {name} != ''"
                    ).fetchone()[0])
                    break
            if "source" in columns:
                result["failed"] = int(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE source = 'analysis' "
                    "AND lufs IS NULL" if "lufs" in columns else
                    f"SELECT 0 FROM {table} WHERE 0").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return result
    return result


def _csv_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            return max(0, sum(1 for _ in csv.reader(fh)) - 1)
    except OSError:
        return 0


def _playlists(root: Path) -> dict:
    base = Path(root) / "Playlists"
    lists = entries = 0
    for pattern, folder in (("*.m3u8", base),
                            ("*.m3u8", base / "Moods"),
                            ("*.m3u8", base / "Custom"),
                            ("*.m3u8", base / "Own"),
                            ("*.m3u8", base / "Psycho")):
        if not folder.is_dir():
            continue
        for path in folder.glob(pattern):
            if not path.is_file():
                continue
            lists += 1
            try:
                for line in path.read_text(encoding="utf-8",
                                           errors="replace").splitlines():
                    if line.strip() and not line.startswith("#"):
                        entries += 1
            except OSError:
                continue
    return {"lists": lists, "entries": entries}


def summary(root: Path) -> dict:
    """The health picture, as plain data for the panel and the chat."""
    root = Path(root)
    rows = _rows(root)
    tags = {"taggable": 0, "no_title": 0, "no_artist": 0, "no_album": 0,
            "no_genre": 0, "junk_issues": 0}
    art = {"with_embedded": 0, "with_folder": 0, "none": 0}
    decode = {"ok": 0, "failed": 0, "unknown": 0}
    chiptune = 0
    hours = 0.0
    size = 0
    samples: dict[str, list[str]] = {"decode": [], "tags": [], "art": []}
    for row in rows:
        rel = str(row.get("rel_path") or "")
        ext = str(row.get("ext") or Path(rel).suffix).lower()
        try:
            size += int(row.get("size_bytes") or 0)
        except (TypeError, ValueError):
            pass
        try:
            hours += float(row.get("duration_s") or 0) / 3600.0
        except (TypeError, ValueError):
            pass
        if ext not in TAGGABLE:
            chiptune += 1
        ok = row.get("decode_ok")
        if ok is True:
            decode["ok"] += 1
        elif ok is False:
            decode["failed"] += 1
            if len(samples["decode"]) < 8:
                samples["decode"].append(rel)
        else:
            decode["unknown"] += 1
        embedded = bool(row.get("has_embedded_art")) or int(
            row.get("art_count") or 0) > 0
        folder = bool(row.get("art_files")) or int(
            row.get("folder_image_count") or 0) > 0
        if embedded:
            art["with_embedded"] += 1
        if folder:
            art["with_folder"] += 1
        if not embedded and not folder:
            art["none"] += 1
            if ext in TAGGABLE and len(samples["art"]) < 8:
                samples["art"].append(rel)
        if ext not in TAGGABLE:
            continue
        tags["taggable"] += 1
        missing = []
        if not str(row.get("title") or "").strip():
            tags["no_title"] += 1
            missing.append("title")
        if not str(row.get("artist") or row.get("albumartist") or "").strip():
            tags["no_artist"] += 1
            missing.append("artist")
        if not str(row.get("album") or "").strip():
            tags["no_album"] += 1
            missing.append("album")
        if not str(row.get("genre") or "").strip():
            tags["no_genre"] += 1
            missing.append("genre")
        if row.get("issues"):
            tags["junk_issues"] += 1
        if missing and len(samples["tags"]) < 8:
            samples["tags"].append(f"{rel} ({', '.join(missing)})")

    audit = inventory.audit(root)
    profile = _db_counts(root / "library-work" / "app" / "psycho" /
                         "features.db")
    lyrics = _db_counts(root / "library-work" / "app" / "lyrics" / "lyrics.db")

    reports = root / "library-work" / "reports"
    duplicates = _csv_rows(reports / "probable-duplicates.csv")
    review = _csv_rows(reports / "duplicates.csv")
    playlist_counts = _playlists(root)
    try:
        disk = shutil.disk_usage(str(root))
        free_gb = round(disk.free / 1e9, 1)
    except OSError:
        free_gb = 0.0
    try:
        latest = recent.entries(root, 1)
    except OSError:
        latest = []
    try:
        indexed = datetime.fromtimestamp(
            _inventory_path(root).stat().st_mtime,
            timezone.utc).isoformat(timespec="seconds")
    except OSError:
        indexed = ""

    notes = []
    if audit["missing"]:
        notes.append(f"{audit['missing']:,} inventory rows point at a file "
                     "that is gone — the next index drops them.")
    if audit["changed"]:
        notes.append(f"{audit['changed']:,} files changed on disk since the "
                     "last index — the next index re-reads them.")
    if decode["failed"]:
        notes.append(f"{decode['failed']:,} files did not decode cleanly.")
    if not playlist_counts["lists"]:
        notes.append("No playlists found.")
    if not tags["taggable"]:
        notes.append("The inventory lists no taggable audio.")

    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tracks": len(rows),
        "hours": round(hours, 1),
        "size_gb": round(size / 1e9, 2),
        "chiptune": chiptune,
        "tags": tags,
        "artwork": art,
        "decode": decode,
        "inventory": audit,
        "profile": profile,
        "lyrics": lyrics,
        "playlists": playlist_counts,
        "duplicates": {"probable": duplicates, "kept_on_purpose": review},
        "disk": {"free_gb": free_gb},
        "indexed_at": indexed,
        "latest_change": latest[0] if latest else None,
        "samples": samples,
        "notes": notes,
    }


def markdown(data: dict) -> str:
    """The same picture as a chat answer."""
    tags = data.get("tags") or {}
    art = data.get("artwork") or {}
    decode = data.get("decode") or {}
    playlists = data.get("playlists") or {}
    audit = data.get("inventory") or {}
    profile = data.get("profile") or {}
    lyrics = data.get("lyrics") or {}
    lines = [
        f"**Library health** · {data.get('tracks', 0):,} tracks · "
        f"{data.get('hours', 0):,} h · {data.get('size_gb', 0):,} GB",
        "",
        f"- **Tags**: {tags.get('taggable', 0):,} taggable files — "
        f"{tags.get('no_title', 0):,} without a title, "
        f"{tags.get('no_artist', 0):,} without an artist, "
        f"{tags.get('no_genre', 0):,} without a genre, "
        f"{tags.get('no_album', 0):,} without an album",
        f"- **Artwork**: {art.get('with_embedded', 0):,} embedded, "
        f"{art.get('with_folder', 0):,} folder-only, "
        f"{art.get('none', 0):,} with none",
        f"- **Decode**: {decode.get('ok', 0):,} clean, "
        f"{decode.get('failed', 0):,} with a decode error, "
        f"{decode.get('unknown', 0):,} unknown",
        f"- **Index**: {audit.get('rows', 0):,} rows · "
        f"{audit.get('missing', 0):,} missing · "
        f"{audit.get('changed', 0):,} changed since the last scan",
        f"- **Playlists**: {playlists.get('lists', 0):,} lists · "
        f"{playlists.get('entries', 0):,} entries",
        f"- **Sound profile**: {profile.get('with_data', 0):,} of "
        f"{profile.get('total', 0):,} measured · "
        f"{profile.get('failed', 0):,} undecodable",
        f"- **Lyrics**: {lyrics.get('with_data', 0):,} of "
        f"{lyrics.get('total', 0):,} transcribed",
    ]
    if data.get("notes"):
        lines.append("")
        for note in data["notes"]:
            lines.append(f"- {note}")
    if data.get("latest_change"):
        change = data["latest_change"]
        lines.append("")
        lines.append(f"Latest change: {change.get('title', '')} · "
                     f"{change.get('label', '')} · {change.get('ts', '')}")
    lines.append("")
    lines.append("`LibraryWork/reports/` holds the full detail; nothing here "
                 "was written or deleted.")
    return "\n".join(lines)
