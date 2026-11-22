"""Play every track at a comparable loudness.

The sound-profile database already holds a BS.1770 measurement per track
(`psycho/features.db`, column `lufs`).  A track mastered in 1994 and one from
2024 can differ by 10 dB, which makes a shuffled queue jump.  This module turns
that measurement into a gain relative to one target, and the player applies it
as an mpv audio filter, so the volume slider keeps meaning what the listener
set and nothing is rewritten in the audio files.

A measurement never exists for every file: a track that was not analysed, or
whose decode failed, simply plays untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: what every track is pulled towards (EBU R128-ish; streaming uses -14)
TARGET_LUFS = -16.0
#: never boost or cut more than this, so a broken measurement cannot shout
MAX_GAIN_DB = 12.0

_CACHE: dict[tuple[str, int, int], dict[str, float]] = {}


def db_path(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "psycho" / "features.db"


def _measurements(root: Path) -> dict[str, float]:
    """Every usable LUFS value, cached by the database's size and mtime."""
    path = db_path(root)
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_size, int(stat.st_mtime))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    values: dict[str, float] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            rows = conn.execute(
                "SELECT rel_path, lufs FROM audio WHERE lufs IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    for rel, lufs in rows:
        try:
            value = float(lufs)
        except (TypeError, ValueError):
            continue
        if value <= -70.0 or value >= 0.0:     # silence or a broken measure
            continue
        values[str(rel)] = value
    _CACHE.clear()
    _CACHE[key] = values
    return values


def gain(root: Path, rel: str) -> float | None:
    """The gain in dB for one track, or None when there is no measurement."""
    if not rel:
        return None
    lufs = _measurements(Path(root)).get(str(rel))
    if lufs is None:
        return None
    gain_db = TARGET_LUFS - lufs
    return max(-MAX_GAIN_DB, min(MAX_GAIN_DB, gain_db))


def filter_for(root: Path, rel: str) -> str:
    """mpv's `af` value for a track ("" when it should play untouched)."""
    gain_db = gain(root, rel)
    if gain_db is None or abs(gain_db) < 0.1:
        return ""
    return f"volume={gain_db:.1f}dB"


def describe(root: Path, rel: str) -> dict:
    """What the app reports for one track, for the properties panel."""
    lufs = _measurements(Path(root)).get(str(rel))
    gain_db = gain(root, rel)
    return {"measured": lufs is not None,
            "lufs": round(lufs, 2) if lufs is not None else None,
            "target": TARGET_LUFS,
            "gain_db": round(gain_db, 2) if gain_db is not None else None,
            "filter": filter_for(root, rel)}
