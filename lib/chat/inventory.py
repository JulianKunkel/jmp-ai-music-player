"""Keep the inventory in step with the files on disk.

The Phase 0 scan is incremental *by path*: a file that already has a row in
`library-work/inventory/files.jsonl` is skipped, even when its tags were edited
by another program, its bytes were replaced or it was renamed.  For the library
tools that is fine (they rewrite the inventory themselves), but the app's
catalogue and search read the inventory, so a tag changed outside the app never
arrives until somebody runs `phase0_inventory.py scan --reset`.

This module prepares the app's index run instead: it drops the rows whose file
is gone and the rows whose size or mtime no longer matches the file, so the
scanner that follows re-reads exactly those files and nothing else.  It never
guesses a tag value and never touches audio; the only file it writes is the
generated inventory itself, atomically, after copying the old one aside.

The same audit powers the library-health panel (`audit`), which needs the
counts without changing anything.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

#: how many timestamped backups of the inventory to keep
KEEP_BACKUPS = 2

_MTIME_SLACK_S = 1.0


def inventory_path(root: Path) -> Path:
    return Path(root) / "library-work" / "inventory" / "files.jsonl"


def _parse_mtime(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _locate(root: Path, rel: str) -> tuple[Path | None, bool]:
    """The on-disk path for an inventory row, and whether it was repointed.

    A row written before the shelves moved out of `Music/` still carries that
    prefix.  When only the stripped path exists the row is repointed instead of
    being dropped, which repairs the leftovers of the historical layout; when
    the prefixed path still exists it is left alone.
    """
    rel = str(rel or "")
    if not rel:
        return None, False
    direct = Path(root) / rel
    if direct.is_file():
        return direct, False
    if rel.startswith("Music/"):
        stripped = Path(root) / rel[len("Music/"):]
        if stripped.is_file():
            return stripped, True
    return None, False


def _strip_music(rel: str) -> str:
    return rel[len("Music/"):] if rel.startswith("Music/") else rel


def audit(root: Path) -> dict:
    """Count the inventory rows that no longer describe their file.

    Returns `rows`, `ok`, `changed` (size or mtime moved), `missing` (the file
    is gone) and `prefix` (a historical `Music/` path that had to be stripped).
    Read-only: nothing is written.
    """
    root = Path(root)
    path = inventory_path(root)
    counts = {"rows": 0, "ok": 0, "changed": 0, "missing": 0, "prefix": 0,
              "exists": path.is_file()}
    if not path.is_file():
        return counts
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            counts["rows"] += 1
            rel = str(row.get("rel_path") or "")
            target, repointed = _locate(root, rel)
            if target is None:
                counts["missing"] += 1
                continue
            if repointed:
                counts["prefix"] += 1
            try:
                stat = target.stat()
            except OSError:
                counts["missing"] += 1
                continue
            mtime = _parse_mtime(str(row.get("mtime") or ""))
            size = str(row.get("size_bytes") or "")
            if not mtime or not size:
                counts["ok"] += 1      # nothing to compare: keep the row
                continue
            same_time = abs(mtime - stat.st_mtime) <= _MTIME_SLACK_S
            if same_time and size == str(stat.st_size):
                counts["ok"] += 1
            else:
                counts["changed"] += 1
    return counts


def _backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-rescan-{stamp}")
    shutil.copy2(path, target)
    backups = sorted(path.parent.glob(f"{path.name}.bak-rescan-*"))
    for old in backups[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


def prepare_scan(root: Path, log=None) -> dict:
    """Drop the stale rows so the next scan re-reads exactly those files.

    A row is kept when its file exists with the recorded size and mtime.  A row
    whose file moved under the historical `Music/` prefix is repointed.  A row
    whose size/mtime moved is removed — the scanner appends a fresh, complete
    row for the same path a moment later, which avoids two parsers drifting
    apart.  A row whose file is gone is removed as well, so search cannot offer
    a dead track.
    """
    root = Path(root)
    path = inventory_path(root)
    result = {"removed": 0, "missing": 0, "repaired": 0, "kept": 0,
              "backup": "", "exists": path.is_file()}
    if not path.is_file():
        return result

    # JSONL is split on the byte: a tag value may contain U+0085, which
    # str.splitlines() would treat as a line break
    chunks = path.read_bytes().split(b"\n")
    kept: list[bytes] = []
    missing: list[bytes] = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        try:
            row = json.loads(chunk)
        except ValueError:
            kept.append(chunk)          # leave it for the report tools
            continue
        rel = str(row.get("rel_path") or "")
        target, repointed = _locate(root, rel)
        if target is None:
            result["missing"] += 1
            missing.append(chunk)
            continue
        if repointed:
            row["rel_path"] = _strip_music(rel)
            result["repaired"] += 1
            kept.append(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            continue
        try:
            stat = target.stat()
        except OSError:
            result["missing"] += 1
            missing.append(chunk)
            continue
        mtime = _parse_mtime(str(row.get("mtime") or ""))
        size = str(row.get("size_bytes") or "")
        if not mtime or not size:
            kept.append(chunk)      # nothing to compare against: keep it
            result["kept"] += 1
            continue
        same_time = abs(mtime - stat.st_mtime) <= _MTIME_SLACK_S
        if same_time and size == str(stat.st_size):
            kept.append(chunk)
            result["kept"] += 1
        else:
            result["removed"] += 1

    if not result["removed"] and not result["missing"] and not result["repaired"]:
        return result

    result["backup"] = _backup(path).relative_to(root).as_posix()
    if missing:
        sidecar = path.parent / "files.missing.jsonl"
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with sidecar.open("ab") as handle:
            for chunk in missing:
                try:
                    row = json.loads(chunk)
                except ValueError:
                    row = None
                handle.write(json.dumps(
                    {"dropped_at": stamp, "reason": "file-not-found",
                     "row": row}, ensure_ascii=False).encode("utf-8") + b"\n")
        result["missing_log"] = sidecar.relative_to(root).as_posix()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(b"\n".join(kept) + (b"\n" if kept else b""))
    os.replace(tmp, path)
    if log:
        log(f"[index] inventory: {result['kept']} unchanged, "
            f"{result['removed']} changed (to re-read), "
            f"{result['missing']} missing (dropped), "
            f"{result['repaired']} path repaired")
    return result
