"""What the library actually holds: sizes, counts and the shape of it.

Read-only, and derived only from data the library already keeps: the inventory
rows (tags, duration, size, bitrate, genre, date, artwork flags), the playlists
folder and the two measuring databases.  No audio file is opened, nothing is
written.

The inventory is read once and cached until its size or mtime moves, so the
panel can be asked repeatedly; everything else is cheap.

Two things this deliberately does *not* do: it never guesses a year from a file
name (only the `date` tag counts) and it never counts a chiptune as "missing a
tag" — an `.spc` has no tag field to fill.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .catalog import AUDIO_EXT, fold, library_rel

#: a chiptune carries no tag field, so it is not part of the tag coverage
TAGGABLE = {".mp3", ".mp2", ".m4a", ".mp4", ".opus", ".ogg", ".flac",
            ".wav", ".wma", ".aac"}

#: formats that are lossless by definition
LOSSLESS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".wv"}

#: the focus words the chat understands; anything else shows the whole picture
FOCUS = ("genres", "artists", "albums", "decades", "formats", "added",
         "length", "quality", "playlists", "coverage")

_CACHE: dict[tuple[str, int, int], list[dict]] = {}


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
        return cached
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
    _CACHE[key] = rows
    return rows


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _year(row: dict) -> int | None:
    match = re.search(r"(1[89]\d\d|20\d\d)", str(row.get("date") or ""))
    return int(match.group(1)) if match else None


def _stamp(row: dict) -> float:
    text = str(row.get("mtime") or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# -- the small readers another module may share ---------------------------
#
# arrivals.py lists the newest files from the same inventory and wants the
# same cache, the same "is this a number" tolerance and the same mtime parse,
# so these three are the public names for exactly that.


def rows(root: Path) -> list[dict]:
    """Every inventory row, cached until the inventory file changes."""
    return _rows(root)


def number(value) -> float:
    """A float or 0.0 — a hand-written inventory row may say "192" or ""."""
    return _number(value)


def stamp(row: dict) -> float:
    """A row's `mtime` as a POSIX timestamp (0.0 when it cannot be read)."""
    return _stamp(row)


def _playlists(root: Path) -> dict:
    """How many lists of each kind there are (files, not entries)."""
    base = Path(root) / "Playlists"
    kinds: Counter = Counter()
    if base.is_dir():
        for path in base.rglob("*.m3u8"):
            relative = path.relative_to(base)
            head = relative.parts[0] if len(relative.parts) > 1 else ""
            if head in {"Custom", "Own", "Smart", "Psycho"}:
                kinds[head.lower()] += 1
            elif head == "Moods":
                kinds["focus" if path.name.startswith("Mood_Focus_")
                      else "mood"] += 1
            else:
                kinds["shelf"] += 1
    total = sum(kinds.values())
    return {"total": total, **dict(kinds)}


def _db(root: Path, relative: str, table: str, column: str) -> dict:
    """How many rows a measuring database holds, tolerantly."""
    path = Path(root) / relative
    out = {"total": 0, "with_data": 0, "exists": path.is_file()}
    if not path.is_file():
        return out
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if table not in tables:
                return out
            out["total"] = int(conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            columns = {row[1] for row in conn.execute(
                f"PRAGMA table_info({table})")}
            if column in columns:
                out["with_data"] = int(conn.execute(
                    f"SELECT COUNT(*) FROM {table} "
                    f"WHERE {column} IS NOT NULL AND {column} != ''"
                ).fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    return out


def _top(counter: Counter, limit: int) -> list[list]:
    return [[name, int(count)] for name, count in counter.most_common(limit)]


def summary(root: Path, *, top: int = 8, now: datetime | None = None) -> dict:
    """The statistics of one library, as plain data."""
    root = Path(root)
    rows = [row for row in _rows(root)
            if str(row.get("ext") or "").lower() in AUDIO_EXT
            or str(row.get("rel_path") or "").lower().endswith(
                tuple(AUDIO_EXT))]
    now = now or datetime.now(timezone.utc)
    clock = now.timestamp()

    tracks = taggable = chiptune = lossless = 0
    seconds = 0.0
    size = 0
    bitrates: list[float] = []
    genres: Counter = Counter()
    shelves: Counter = Counter()
    formats: Counter = Counter()
    decades: Counter = Counter()
    artists: Counter = Counter()
    albums: Counter = Counter()
    missing = {"artist": 0, "album": 0, "genre": 0, "title": 0}
    art = {"embedded": 0, "folder": 0, "none": 0, "any": 0}
    longest: dict | None = None
    shortest: dict | None = None
    newest: list[dict] = []
    added = {"week": 0, "month": 0, "year": 0}

    for row in rows:
        rel = library_rel(str(row.get("rel_path") or ""))
        ext = str(row.get("ext") or Path(rel).suffix).lower()
        tracks += 1
        seconds += _number(row.get("duration_s"))
        size += int(_number(row.get("size_bytes")))
        formats[ext or "?"] += 1
        if ext in LOSSLESS:
            lossless += 1
        rate = _number(row.get("bitrate"))
        if rate >= 2000:
            # the inventory stores bit/s (146491), a reply says kbit/s (146.5)
            rate /= 1000.0
        if rate >= 32:
            bitrates.append(rate)
        year = _year(row)
        if year:
            decades[f"{year // 10 * 10}s"] += 1
        genre = str(row.get("genre") or "").strip()
        if genre:
            genres[genre] += 1
        shelf = str(row.get("grouping") or row.get("category")
                    or row.get("collection") or "").strip()
        if shelf:
            shelves[shelf] += 1
        duration = _number(row.get("duration_s"))
        if duration > 0:
            entry = {"rel": rel,
                     "title": str(row.get("title") or Path(rel).stem),
                     "artist": str(row.get("artist")
                                   or row.get("albumartist") or ""),
                     "duration_s": duration}
            if longest is None or duration > longest["duration_s"]:
                longest = entry
            if shortest is None or duration < shortest["duration_s"]:
                shortest = entry
        stamp = _stamp(row)
        if stamp:
            age = clock - stamp
            if age <= 7 * 86400:
                added["week"] += 1
            if age <= 31 * 86400:
                added["month"] += 1
            if age <= 366 * 86400:
                added["year"] += 1
            newest.append({"rel": rel,
                           "title": str(row.get("title") or Path(rel).stem),
                           "artist": str(row.get("artist")
                                         or row.get("albumartist") or ""),
                           "mtime": str(row.get("mtime") or "")})
        embedded = bool(row.get("has_embedded_art")) or int(
            _number(row.get("art_count"))) > 0
        folder_art = bool(row.get("art_files")) or int(
            _number(row.get("folder_image_count"))) > 0
        if embedded:
            art["embedded"] += 1
        if folder_art:
            art["folder"] += 1
        if not embedded and not folder_art:
            art["none"] += 1
        else:
            art["any"] += 1

        if ext not in TAGGABLE:
            chiptune += 1
            continue
        taggable += 1
        name = str(row.get("artist") or row.get("albumartist") or "").strip()
        album = str(row.get("album") or "").strip()
        if name:
            artists[name] += 1
        else:
            missing["artist"] += 1
        if album:
            albums[f"{fold(name)}\u0000{album}"] += 1
        else:
            missing["album"] += 1
        if not genre:
            missing["genre"] += 1
        if not str(row.get("title") or "").strip():
            missing["title"] += 1

    artist_counter: Counter = Counter()
    for name, count in artists.items():
        artist_counter[name] += count
    # fold duplicates only for the count, keep a readable spelling
    folded_artists: dict[str, list] = {}
    for name, count in artist_counter.items():
        key = fold(name)
        if key not in folded_artists:
            folded_artists[key] = [name, 0]
        folded_artists[key][1] += count
    artist_top = sorted(
        ([item[0], item[1]] for item in folded_artists.values()),
        key=lambda pair: (-pair[1], fold(pair[0])))

    playlist_counts = _playlists(root)
    profile = _db(root, "library-work/app/psycho/features.db", "audio", "lufs")
    lyrics = _db(root, "library-work/app/lyrics/lyrics.db", "track", "text")

    def pct(value: int, total: int) -> float:
        return round(value * 100.0 / total, 1) if total else 0.0

    art_any = art["any"]
    return {
        "generated": now.isoformat(timespec="seconds"),
        "tracks": tracks,
        "taggable": taggable,
        "chiptune": chiptune,
        "lossless": lossless,
        "hours": round(seconds / 3600.0, 2),
        "size_gb": round(size / 1e9, 2),
        "average_bitrate": round(sum(bitrates) / len(bitrates), 1)
        if bitrates else 0.0,
        "genres": _top(genres, top),
        "genre_values": len(genres),
        "shelves": _top(shelves, top),
        "decades": [[label, count] for label, count in
                    sorted(decades.items(), key=lambda kv: kv[0])],
        "formats": _top(formats, 12),
        "artists": {"unique": len(folded_artists),
                    "top": artist_top[:top]},
        "albums": {"unique": len(albums)},
        "missing": missing,
        "artwork": {**art, "with_any": art_any,
                    "pct": pct(art_any, tracks)},
        "coverage": {
            "artist": pct(taggable - missing["artist"], taggable),
            "album": pct(taggable - missing["album"], taggable),
            "genre": pct(taggable - missing["genre"], taggable),
            "title": pct(taggable - missing["title"], taggable),
            "artwork": pct(art_any, tracks),
            "profile": pct(profile["with_data"], tracks),
            "lyrics": pct(lyrics["with_data"], tracks),
        },
        "profile": profile,
        "lyrics": lyrics,
        "playlists": playlist_counts,
        "added": dict(added),
        "newest": _newest(newest, 5),
        "longest": longest,
        "shortest": shortest,
        "root": str(root),
    }


def _newest(newest: list[dict], limit: int) -> list[dict]:
    ordered = sorted(newest, key=lambda item: str(item.get("mtime") or ""),
                     reverse=True)
    return ordered[:limit]


# --------------------------------------------------------------------------
# the chat answer


def _line(label: str, value) -> str:
    return f"- **{label}** · {value}"


def _count(number: int, word: str) -> str:
    """`1 artist` / `3 artists` — a bad plural makes a reply look careless."""
    return f"{number:,} {word}" + ("" if number == 1 else "s")


def _genres_line(data: dict) -> str:
    genres = data.get("genres") or []
    if not genres:
        return "_No genre tags yet._"
    total = data.get("taggable") or 1
    parts = [f"{name} {count:,} ({count * 100 // total} %)"
             for name, count in genres]
    return " · ".join(parts)


def markdown(data: dict, focus: str = "") -> str:
    """The same numbers as a chat answer, narrowed to `focus` when given."""
    focus = (focus or "").casefold()
    tracks = data.get("tracks") or 0
    head = (f"**{_count(tracks, 'track')}** · {data.get('hours', 0):,.1f} h · "
            f"{data.get('size_gb', 0):,.2f} GB · "
            f"{_count(data.get('artists', {}).get('unique', 0), 'artist')} · "
            f"{_count(data.get('albums', {}).get('unique', 0), 'album')}")

    if focus == "genres":
        return "\n".join([
            f"**Genres** · "
            f"{_count(data.get('genre_values', 0), 'value')} · "
            f"{data.get('coverage', {}).get('genre', 0)} % of the taggable "
            "tracks tagged",
            "",
            _genres_line(data),
            "",
            f"{data.get('missing', {}).get('genre', 0):,} taggable tracks "
            "still have no genre.",
        ])

    if focus == "artists":
        top = data.get("artists", {}).get("top") or []
        lines = [f"**Artists** · "
                 f"{_count(data.get('artists', {}).get('unique', 0), 'performer')}",
                 ""]
        lines += [_line(name, _count(count, "track")) for name, count in top]
        return "\n".join(lines + ["", "Counted on the inventory rows; a "
                                      "featuring credit stays with its own "
                                      "name, as the tag says."])

    if focus == "decades":
        decades = data.get("decades") or []
        if not decades:
            return ("**Decades** · no `date` tag carries a year yet, so there "
                    "is nothing to count.")
        lines = ["**Decades** · from the `date` tag", ""]
        lines += [_line(label, _count(count, "track"))
                  for label, count in decades]
        dated = sum(count for _label, count in decades)
        return "\n".join(lines + ["",
                                  f"{dated:,} of {data.get('tracks', 0):,} "
                                  "tracks carry a year."])

    if focus == "formats":
        formats = data.get("formats") or []
        lossless = int(data.get("lossless", 0) or 0)
        lines = [f"**Formats** · {_count(len(formats), 'container')} · "
                 f"{lossless:,} lossless", ""]
        lines += [_line(name or "?", _count(count, "file"))
                  for name, count in formats]
        return "\n".join(lines)

    if focus == "added":
        added = data.get("added") or {}
        lines = ["**Recent additions** · by file time", "",
                 _line("last 7 days", _count(added.get("week", 0), "track")),
                 _line("last 31 days", _count(added.get("month", 0), "track")),
                 _line("last year", _count(added.get("year", 0), "track"))]
        newest = data.get("newest") or []
        if newest:
            lines += ["", "Newest first:"]
            lines += [f"- {item.get('artist') or '?'} — {item.get('title')}"
                      for item in newest]
        lines += ["", "Counted from each file's own time, which also moves "
                      "when a tag or the file itself is rewritten — a quiet "
                      "week can still look busy after a tagging pass."]
        return "\n".join(lines)

    if focus == "length":
        longest = data.get("longest") or {}
        shortest = data.get("shortest") or {}
        lines = ["**Track lengths**", ""]
        if longest:
            lines.append(_line(
                "longest", f"{longest.get('title')} — {longest.get('artist')}"
                           f" · {longest.get('duration_s', 0) / 60:.1f} min"))
        if shortest:
            lines.append(_line(
                "shortest", f"{shortest.get('title')} — "
                            f"{shortest.get('artist')} · "
                            f"{shortest.get('duration_s', 0):.0f} s"))
        average = (data.get("hours", 0) * 3600 / data.get("tracks")
                   if data.get("tracks") else 0)
        lines.append(_line("average", f"{average / 60:.1f} min per track"))
        return "\n".join(lines)

    if focus == "quality":
        lines = [
            "**Sound quality**",
            "",
            _line("average bitrate", f"{data.get('average_bitrate', 0):g} "
                                     "kbit/s"),
            _line("lossless", _count(data.get("lossless", 0), "file")),
            _line("total size", f"{data.get('size_gb', 0):,.2f} GB"),
            _line("format", ", ".join(
                f"{name} {count:,}" for name, count in
                (data.get("formats") or [])[:4]) or "—"),
        ]
        if not data.get("average_bitrate"):
            lines.append("")
            lines.append("No bitrate is recorded for these files — a chiptune "
                         "and a lossless container have none.")
        return "\n".join(lines)

    if focus == "playlists":
        lists = data.get("playlists") or {}
        return "\n".join([
            f"**Playlists** · {lists.get('total', 0):,} lists",
            "",
            _line("shelves", f"{lists.get('shelf', 0):,}"),
            _line("moods", f"{lists.get('mood', 0):,}"),
            _line("focus", f"{lists.get('focus', 0):,}"),
            _line("sound families", f"{lists.get('psycho', 0):,}"),
            _line("smart", f"{lists.get('smart', 0):,}"),
            _line("mine", f"{lists.get('custom', 0):,}"),
            _line("ad-hoc", f"{lists.get('own', 0):,}"),
        ])

    if focus == "coverage":
        covers = data.get("coverage") or {}
        profile = data.get("profile") or {}
        lyrics = data.get("lyrics") or {}
        return "\n".join([
            "**How complete the library is**", "",
            _line("title", f"{covers.get('title', 0)} %"),
            _line("artist", f"{covers.get('artist', 0)} %"),
            _line("album", f"{covers.get('album', 0)} %"),
            _line("genre", f"{covers.get('genre', 0)} %"),
            _line("cover art", f"{covers.get('artwork', 0)} %"),
            _line("sound profile", f"{profile.get('with_data', 0):,} of "
                                   f"{profile.get('total', 0):,} measured"),
            _line("lyrics", f"{lyrics.get('with_data', 0):,} of "
                            f"{lyrics.get('total', 0):,} transcribed"),
            "",
            f"{data.get('missing', {}).get('genre', 0):,} taggable tracks "
            "have no genre; "
            f"{data.get('artwork', {}).get('none', 0):,} files have no cover.",
        ])

    # the whole picture
    lists = data.get("playlists") or {}
    decades = data.get("decades") or []
    oldest = decades[0][0] if decades else ""
    newest = decades[-1][0] if decades else ""
    span = (f"{oldest}–{newest}" if oldest else "no dated tracks")
    lines = [
        head,
        "",
        _line("taggable", f"{data.get('taggable', 0):,} (of which "
                          f"{_count(data.get('chiptune', 0), 'chiptune')})"),
        _line("average bitrate", f"{data.get('average_bitrate', 0):g} kbit/s "
                                 f"· {data.get('lossless', 0):,} lossless"),
        _line("genres", _genres_line(data)),
        _line("decades", span),
        _line("formats", " · ".join(f"{name} {count:,}" for name, count in
                                    (data.get("formats") or [])[:6])),
        _line("playlists", f"{lists.get('total', 0):,} lists · "
                           f"{lists.get('mood', 0)} mood · "
                           f"{lists.get('focus', 0)} focus · "
                           f"{lists.get('custom', 0)} of mine"),
        _line("changed recently", _count(
            data.get("added", {}).get("month", 0), "track")
            + " in the last 31 days (file time, so a tagging pass counts)"),
        "",
        "Ask “which genres do I have most of”, “the decades”, “how is the "
        "quality”, “what did I add this month” or “how complete is the "
        "library” for one of these in detail.",
    ]
    return "\n".join(lines)
