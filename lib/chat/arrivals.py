"""What arrived lately — the newest files in the library, newest first.

The inventory has no arrival field, so the only honest signal is the file's
own `mtime`.  That is the same number `library-work/reports/stats.py` uses for
its "changed recently" slice, and it moves when a file is retagged or renamed
as well as when it is really new.  The answer says so instead of pretending
there is a download date.

Read-only: the inventory rows are read (and cached until the file's size or
mtime moves), the file is stat'ed to see whether it is still there, and
nothing is written.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import stats
from .catalog import AUDIO_EXT, library_rel

#: how many arrivals one answer lists
DEFAULT_LIMIT = 30

#: the windows the counter reports, in days
WINDOWS = (("today", 1), ("week", 7), ("month", 31), ("year", 366))

#: the honest caveat, printed once per answer
NOTE = ("“Added” is the file's own time: a tag write or a rename moves a track "
        "up this list too, so read it as *changed recently*.")


def _entry(root: Path, row: dict) -> dict | None:
    """One inventory row as an arrival, or None when it cannot be placed."""
    rel = library_rel(str(row.get("rel_path") or ""))
    ext = str(row.get("ext") or Path(rel).suffix).casefold()
    if not rel or ext not in AUDIO_EXT:
        return None
    stamp = stats.stamp(row)
    if not stamp:
        return None
    try:
        size = int(row.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size = 0
    try:
        exists = (root / rel).is_file()
    except OSError:
        exists = False
    return {
        "rel": rel,
        "title": str(row.get("title") or "").strip(),
        "artist": str(row.get("artist") or row.get("albumartist")
                      or "").strip(),
        "album": str(row.get("album") or "").strip(),
        "genre": str(row.get("genre") or "").strip(),
        "grouping": str(row.get("grouping") or "").strip(),
        "duration_s": round(stats.number(row.get("duration_s")), 1),
        "size_bytes": size,
        "ext": ext,
        "mtime": str(row.get("mtime") or ""),
        "stamp": stamp,
        "exists": exists,
    }


def all_arrivals(root: Path | str) -> tuple[list[dict], int]:
    """Every placeable row, newest first, plus how many files are gone."""
    root = Path(root)
    entries: list[dict] = []
    missing = 0
    for row in stats.rows(root):
        entry = _entry(root, row)
        if entry is None:
            continue
        if not entry["exists"]:
            missing += 1
            continue
        entries.append(entry)
    entries.sort(key=lambda item: (-item["stamp"], item["rel"]))
    return entries, missing


def recent(root: Path | str, limit: int = DEFAULT_LIMIT,
           days: int | None = None) -> list[dict]:
    """The newest files, optionally only those from the last `days` days."""
    entries, _missing = all_arrivals(root)
    if days:
        floor = datetime.now(timezone.utc).timestamp() - int(days) * 86400
        entries = [entry for entry in entries if entry["stamp"] >= floor]
    limit = max(1, int(limit or DEFAULT_LIMIT))
    return entries[:limit]


def summary(root: Path | str, limit: int = DEFAULT_LIMIT,
            days: int | None = None) -> dict:
    """The counts, the newest entry and the list one answer shows."""
    root = Path(root)
    entries, missing = all_arrivals(root)
    now = datetime.now(timezone.utc).timestamp()
    counted = {}
    for name, span in WINDOWS:
        floor = now - span * 86400
        counted[name] = sum(1 for entry in entries if entry["stamp"] >= floor)
    windowed = entries
    if days:
        floor = now - int(days) * 86400
        windowed = [entry for entry in entries if entry["stamp"] >= floor]
    limit = max(1, int(limit or DEFAULT_LIMIT))
    items = windowed[:limit]
    for entry in items:
        entry["age"] = age(now - entry["stamp"])
    return {
        "total": len(entries),
        "missing": missing,
        **counted,
        "newest": items[0] if items else None,
        "items": items,
        "window_days": int(days) if days else 0,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def age(seconds: float) -> str:
    """A readable distance from now ("4 h ago", "3 weeks ago")."""
    value = max(0.0, float(seconds or 0))
    if value < 120:
        return "just now"
    if value < 7200:
        return f"{int(value // 60)} min ago"
    if value < 172800:
        return f"{int(value // 3600)} h ago"
    days = value / 86400.0
    if days < 14:
        return f"{int(days)} day{'s' if int(days) != 1 else ''} ago"
    if days < 60:
        return f"{int(days / 7)} weeks ago"
    if days < 730:
        return f"{int(days / 30)} months ago"
    return f"{int(days / 365)} years ago"


def _when(entry: dict) -> str:
    """How long ago the file changed (computed once in `summary`)."""
    if entry.get("age"):
        return str(entry["age"])
    now = datetime.now(timezone.utc).timestamp()
    return age(now - entry["stamp"])


def _count(value: int, word: str) -> str:
    return f"{value:,} {word}{'' if value == 1 else 's'}"


def markdown(data: dict, limit: int = 12) -> str:
    """The arrivals as a chat answer, newest first."""
    items = data.get("items") or []
    if not items:
        return ("**Nothing new.**  No file in the inventory is newer than the "
                "requested window — the library has not changed lately.")
    window = data.get("window_days")
    lines = [
        "**Recently added**"
        + (f" · the last {window} day{'s' if window != 1 else ''}"
           if window else "")
        + f" · {_count(data.get('total', 0), 'track')} in the inventory",
        "",
        "Today {today} · 7 days {week} · 31 days {month} · a year {year}"
        .format(today=f"{data.get('today', 0):,}",
                week=f"{data.get('week', 0):,}",
                month=f"{data.get('month', 0):,}",
                year=f"{data.get('year', 0):,}"),
        "",
    ]
    if data.get("total") and data.get("today", 0) >= data["total"] // 2:
        lines.append("Most of the tree changed within the last day — a tag, "
                     "rename or reorganisation pass moves every file it "
                     "touches into this window.")
        lines.append("")
    for number, entry in enumerate(items[:limit], 1):
        who = entry.get("artist") or "unknown artist"
        title = entry.get("title") or Path(entry["rel"]).stem.replace("_", " ")
        lines.append(f"{number}. **{who} — {title}** · {_when(entry)}")
        detail = " · ".join(part for part in (
            entry.get("album") or Path(entry["rel"]).parent.name,
            entry.get("genre"),
        ) if part)
        if detail:
            lines.append(f"   - {detail}")
        lines.append(f"     `{entry['rel']}`")
    if len(items) > limit:
        lines.append("")
        lines.append(f"… and {len(items) - limit:,} more in this list.")
    lines.append("")
    lines.append(NOTE)
    lines.append("")
    lines.append("Say “play number 1” to hear the newest one.")
    return "\n".join(lines)


def status_markdown(data: dict) -> str:
    """The counts only, for “how many new songs are there”."""
    newest = data.get("newest")
    head = f"**{data.get('total', 0):,} tracks in the inventory.**"
    if data.get("missing"):
        head += (f"  {_count(data['missing'], 'row')} of the inventory points "
                 "at a file that is gone and is not counted.")
    lines = [
        head,
        "",
        f"Changed today: {data.get('today', 0):,} · last 7 days: "
        f"{data.get('week', 0):,} · last 31 days: {data.get('month', 0):,} · "
        f"last year: {data.get('year', 0):,}.",
    ]
    if newest:
        who = newest.get("artist") or "unknown artist"
        title = (newest.get("title")
                 or Path(newest["rel"]).stem.replace("_", " "))
        lines.append("")
        lines.append(f"The newest file is **{who} — {title}** ({_when(newest)}).")
        lines.append(f"`{newest['rel']}`")
    lines.append("")
    lines.append(NOTE)
    return "\n".join(lines)
