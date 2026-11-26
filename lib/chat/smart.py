"""Smart playlists: saved rules that rebuild themselves from the inventory.

A normal playlist is a list of files.  A smart playlist is a *rule* ("Metal
from the 1990s, newest first") kept in `library-work/app/chat/smart.json`, and
the `.m3u8` under `Playlists/Smart/` is generated output — the way the shelf,
mood and psycho lists already are.  Rebuilding reads only the inventory rows
(and, for a sound rule, the profile database), never an audio file, so it costs
a moment on 17k tracks.

The rules are deliberately small and readable, and every one of them is a field
the inventory already carries:

| key | meaning |
|---|---|
| `genre`, `artist`, `album`, `grouping`, `codec` | one word or a list; case/accent-insensitive substring |
| `not_genre`, `not_artist` | the same, as an exclusion |
| `year_from`, `year_to`, `decade` | the four-digit year in `date` (`1995-03-01`, `1995`) |
| `added_days` | file mtime within the last N days (when the file arrived) |
| `min_duration`, `max_duration` | seconds |
| `min_bitrate`, `max_bitrate` | kbit/s |
| `axes` | axis name → minimum percentile (0..1) from the profile database |
| `min_plays`, `max_plays` | how often the track was listened to (`max_plays: 0` = never played) |
| `not_played_days` | not heard in the last N days (a track never played also matches) |
| `sort` | `added` (default, newest first), `oldest`, `title`, `artist`, `duration`, `shuffle` |
| `limit` | how many tracks survive the sort |

A rule set with no filter at all is refused, because "everything" is what
`All.m3u8` already is; `sort` and `limit` alone are allowed only together with
one filter, so a smart list can never silently become the whole library.
"""

from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from .catalog import fold, library_rel

#: where the rules live (app state, not the library)
STATE = Path("library-work") / "app" / "chat" / "smart.json"

#: the keys a rule may carry; anything else is refused rather than ignored
FILTERS = ("genre", "artist", "album", "grouping", "codec",
           "not_genre", "not_artist")
RANGES = ("year_from", "year_to", "added_days",
          "min_duration", "max_duration", "min_bitrate", "max_bitrate",
          "min_plays", "max_plays", "not_played_days")

#: the keys that need the play record (`history.played_index`), not the tags
HISTORY_KEYS = ("min_plays", "max_plays", "not_played_days")
SORTS = ("added", "oldest", "title", "artist", "duration", "shuffle")

MAX_NAME = 80


def clean_name(name: str) -> str:
    """A terminal-safe list name, the same rule the playlist tools use.

    The state key and the file stem must agree, or `prune_stale()` would treat
    the list just written as an orphan and delete it.
    """
    text = re.sub(r"['’`´]", "", str(name or "").strip())
    text = re.sub(r"[^\w.\- ]+", "_", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    return (text or "Smart")[:MAX_NAME]


# --------------------------------------------------------------------------
# state


def state_path(root: Path) -> Path:
    return Path(root) / STATE


def definitions(root: Path) -> dict[str, dict]:
    """The saved rule sets, newest first."""
    try:
        data = json.loads(state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    lists = data.get("lists") if isinstance(data, dict) else None
    if not isinstance(lists, dict):
        return {}
    out: dict[str, dict] = {}
    for name, entry in lists.items():
        if isinstance(entry, dict) and isinstance(entry.get("rules"), dict):
            out[str(name)] = entry
    return dict(sorted(out.items(),
                       key=lambda item: str(item[1].get("updated") or ""),
                       reverse=True))


def _write_state(root: Path, lists: dict[str, dict]) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"format": "julians-music-smart-playlists", "version": 1,
               "lists": lists}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# rules


def _words(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    try:
        items = list(value)
    except TypeError:
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def _number(value, kind=float):
    try:
        return kind(value)
    except (TypeError, ValueError):
        return None


def normalise(rules: dict) -> dict:
    """A clean, JSON-safe rule set; `ValueError` for an unknown key."""
    if not isinstance(rules, dict):
        raise ValueError("A smart playlist needs a rule set.")
    clean: dict = {}
    for key, value in rules.items():
        key = str(key).strip().lower()
        if key in FILTERS:
            words = _words(value)
            if words:
                clean[key] = words
        elif key == "axes":
            if not isinstance(value, dict):
                raise ValueError("`axes` must map an axis to a minimum.")
            axes = {}
            for axis, floor in value.items():
                number = _number(floor)
                if number is None:
                    raise ValueError(f"“{axis}” needs a number between 0 and 1.")
                axes[str(axis).strip().lower()] = max(0.0, min(1.0, number))
            if axes:
                clean["axes"] = axes
        elif key in HISTORY_KEYS:
            number = _number(value, int)
            if number is None or number < 0:
                raise ValueError(f"`{key}` needs a whole number of zero or "
                                 "more.")
            clean[key] = number
        elif key in RANGES:
            number = _number(value, int)
            if number is not None:
                clean[key] = number
        elif key == "decade":
            text = str(value).strip().lower()
            match = re.match(r"^(\d{2,4})s?$", text)
            if not match:
                raise ValueError("A decade looks like 90s, 1990 or 1990s.")
            digits = match.group(1)
            number = int(digits)
            if len(digits) <= 2:
                # "90s" is 1990–1999, "20s" is 2020–2029: the two-digit form
                # cannot be a four-digit year, so the century is decided by
                # the same 30-cut the rest of the app uses
                year = 1900 + number if number >= 30 else 2000 + number
                clean["year_from"] = year
                clean["year_to"] = year + 9
            elif number % 10 == 0:
                # "1990" / "1990s" name a decade
                clean["year_from"] = number
                clean["year_to"] = number + 9
            else:
                # a plain year is a single year, not a decade
                clean["year_from"] = number
                clean["year_to"] = number
        elif key == "sort":
            text = str(value or "").strip().lower()
            if text and text not in SORTS:
                raise ValueError("Sort by " + ", ".join(SORTS) + ".")
            if text:
                clean["sort"] = text
        elif key == "limit":
            number = _number(value, int)
            if number and number > 0:
                clean["limit"] = number
        else:
            raise ValueError(f"Unknown smart-playlist rule “{key}”.")
    if not any(key in clean for key in FILTERS + RANGES + HISTORY_KEYS
               + ("axes",)):
        raise ValueError("A smart playlist needs at least one filter — "
                         "genre, artist, album, shelf, year, added, length, "
                         "bitrate, play count or a sound axis.")
    if clean.get("year_from", 0) and clean.get("year_to", 0) \
            and clean["year_from"] > clean["year_to"]:
        clean["year_from"], clean["year_to"] = (clean["year_to"],
                                                clean["year_from"])
    return clean


def describe(rules: dict) -> str:
    """A short human sentence for a rule set, for replies and the file header."""
    parts: list[str] = []
    for key, label in (("genre", "genre"), ("artist", "artist"),
                       ("album", "album"), ("grouping", "shelf"),
                       ("codec", "format")):
        if rules.get(key):
            parts.append(f"{label} {' / '.join(rules[key])}")
    for key, label in (("not_genre", "not genre"),
                       ("not_artist", "not artist")):
        if rules.get(key):
            parts.append(f"{label} {' / '.join(rules[key])}")
    year_from = rules.get("year_from")
    year_to = rules.get("year_to")
    if year_from and year_to and year_to - year_from == 9:
        parts.append(f"the {str(year_from)[:3]}0s")
    elif year_from and year_to:
        parts.append(f"{year_from}–{year_to}")
    elif year_from:
        parts.append(f"from {year_from}")
    elif year_to:
        parts.append(f"up to {year_to}")
    if rules.get("added_days"):
        parts.append(f"added in the last {rules['added_days']} days")
    if rules.get("min_duration"):
        parts.append(f"longer than {rules['min_duration'] // 60} min")
    if rules.get("max_duration"):
        parts.append(f"shorter than {rules['max_duration'] // 60} min")
    if rules.get("min_bitrate"):
        parts.append(f"at least {rules['min_bitrate']} kbit/s")
    if rules.get("max_bitrate"):
        parts.append(f"at most {rules['max_bitrate']} kbit/s")
    if rules.get("min_plays"):
        parts.append(f"played at least {rules['min_plays']} times")
    if rules.get("max_plays") is not None:
        plays = rules["max_plays"]
        parts.append("never played" if plays == 0
                     else f"played at most {plays} times")
    if rules.get("not_played_days"):
        parts.append(f"not heard in {rules['not_played_days']} days")
    for axis, floor in (rules.get("axes") or {}).items():
        parts.append(f"{axis} ≥ {round(floor * 100)}%")
    if not parts:
        return "every track"
    text = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") \
        + parts[-1]
    sort = rules.get("sort")
    if sort == "added":
        text += ", newest first"
    elif sort == "oldest":
        text += ", oldest first"
    elif sort in {"title", "artist"}:
        text += f", by {sort}"
    elif sort == "duration":
        text += ", shortest first"
    elif sort == "shuffle":
        text += ", shuffled"
    if rules.get("limit"):
        text += f", {rules['limit']} tracks"
    return text


def _year(row: dict) -> int | None:
    match = re.search(r"(1[89]\d\d|20\d\d)", str(row.get("date") or ""))
    return int(match.group(1)) if match else None


def _mtime(row: dict) -> float:
    text = str(row.get("mtime") or "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _hit(needles, row: dict, fields: tuple[str, ...]) -> bool:
    haystack = " ".join(fold(str(row.get(field) or "")) for field in fields)
    return any(fold(needle) in haystack for needle in needles if needle)


def matches(row: dict, rules: dict, axes: dict | None = None,
            now: float | None = None,
            plays: dict | None = None) -> bool:
    """Does one inventory row satisfy the rule set?

    `plays` is `history.played_index(root)` (`rel -> {plays, last}`) and is
    only read when a rule asks about listening.
    """
    rel = library_rel(str(row.get("rel_path") or ""))
    if rules.get("genre") and not _hit(rules["genre"], row, ("genre",)):
        return False
    if rules.get("artist") and not _hit(
            rules["artist"], row, ("artist", "albumartist")):
        return False
    if rules.get("album") and not _hit(rules["album"], row, ("album",)):
        return False
    if rules.get("grouping") and not _hit(
            rules["grouping"], row, ("grouping", "category", "collection")):
        return False
    if rules.get("codec") and not _hit(
            rules["codec"], row, ("codec", "format_name", "ext")):
        return False
    if rules.get("not_genre") and _hit(rules["not_genre"], row, ("genre",)):
        return False
    if rules.get("not_artist") and _hit(
            rules["not_artist"], row, ("artist", "albumartist")):
        return False
    year = _year(row)
    if rules.get("year_from") or rules.get("year_to"):
        if year is None:
            return False
        if rules.get("year_from") and year < rules["year_from"]:
            return False
        if rules.get("year_to") and year > rules["year_to"]:
            return False
    if rules.get("added_days"):
        stamp = _mtime(row)
        if not stamp:
            return False
        now = now if now is not None else datetime.now(timezone.utc).timestamp()
        if now - stamp > rules["added_days"] * 86400:
            return False
    duration = _number(row.get("duration_s"))
    if rules.get("min_duration") is not None:
        if duration is None or duration < rules["min_duration"]:
            return False
    if rules.get("max_duration") is not None:
        if duration is None or duration > rules["max_duration"]:
            return False
    bitrate = _number(row.get("bitrate"))
    if rules.get("min_bitrate") is not None:
        if bitrate is None or bitrate < rules["min_bitrate"]:
            return False
    if rules.get("max_bitrate") is not None:
        if bitrate is None or bitrate > rules["max_bitrate"]:
            return False
    for axis, floor in (rules.get("axes") or {}).items():
        values = (axes or {}).get(rel)
        if not values or values.get(axis) is None:
            return False
        if float(values[axis]) < floor:
            return False
    if any(key in rules for key in HISTORY_KEYS):
        entry = (plays or {}).get(rel) or {}
        count = int(entry.get("plays") or 0)
        last = float(entry.get("last") or 0)
        if rules.get("min_plays") is not None and count < rules["min_plays"]:
            return False
        if rules.get("max_plays") is not None and count > rules["max_plays"]:
            return False
        if rules.get("not_played_days"):
            span = float(rules["not_played_days"]) * 86400
            now = now if now is not None else datetime.now(timezone.utc).timestamp()
            if last and now - last < span:
                return False
    return True


def _sorted(rows: list[dict], rules: dict) -> list[dict]:
    order = rules.get("sort") or "added"
    if order == "added":
        rows = sorted(rows, key=_mtime, reverse=True)
    elif order == "oldest":
        rows = sorted(rows, key=_mtime)
    elif order == "title":
        rows = sorted(rows, key=lambda row: fold(str(row.get("title") or "")))
    elif order == "artist":
        rows = sorted(rows, key=lambda row: (
            fold(str(row.get("artist") or row.get("albumartist") or "")),
            fold(str(row.get("title") or ""))))
    elif order == "duration":
        rows = sorted(rows, key=lambda row: _number(row.get("duration_s")) or 0)
    elif order == "shuffle":
        rows = list(rows)
        random.shuffle(rows)
    if rules.get("limit"):
        rows = rows[:int(rules["limit"])]
    return rows


# --------------------------------------------------------------------------
# building


def _rels(rows: list[dict]) -> list[str]:
    out: list[str] = []
    for row in rows:
        rel = library_rel(str(row.get("rel_path") or ""))
        if rel and rel not in out:
            out.append(rel)
    return out


def matching(rows: list[dict], rules: dict, axes: dict | None = None,
             plays: dict | None = None) -> list[dict]:
    now = datetime.now(timezone.utc).timestamp()
    return _sorted([row for row in rows
                    if matches(row, rules, axes, now, plays)], rules)


def build(root: Path, name: str, rules: dict | None = None, *,
          rows: list[dict] | None = None, axes: dict | None = None,
          plays: dict | None = None,
          dry_run: bool = False, prune: bool = True) -> dict:
    """(Re)write one smart playlist from its rules.

    The definition is stored first (`dry_run` stores nothing), then the file is
    written through `playlist_edit.write_smart`, so the previous content is in
    `playlists.jsonl` before the list changes.  `rows` lets the chat pass the
    catalogue it already holds; without it the inventory is read here.
    """
    root = Path(root)
    name = clean_name(name)
    saved = definitions(root)
    entry = saved.get(name) or {}
    rule_set = normalise(rules if rules is not None else entry.get("rules"))
    description = str(entry.get("description") or "").strip() \
        or describe(rule_set)
    if rows is None:
        from .catalog import Catalog
        rows = Catalog(root).rows
    chosen = matching(rows, rule_set, axes, plays)
    rels = _rels(chosen)
    result = {
        "name": name,
        "rules": rule_set,
        "description": description,
        "count": len(rels),
        "rels": rels,
        "written": "",
        "created": entry.get("created") or _stamp(),
        "dry_run": bool(dry_run),
    }
    if dry_run:
        return result
    import playlist_edit
    with playlist_edit.at_root(root):
        path = playlist_edit.write_smart(name, rels, label=description)
    saved[name] = {"rules": rule_set, "description": description,
                   "created": result["created"], "updated": _stamp(),
                   "count": len(rels),
                   "path": path.relative_to(root).as_posix()}
    _write_state(root, saved)
    result["written"] = path.relative_to(root).as_posix()
    if prune:
        prune_stale(root, saved)
    return result


def prune_stale(root: Path, saved: dict[str, dict] | None = None) -> list[str]:
    """Remove a generated list whose rule set was deleted."""
    folder = Path(root) / "Playlists" / "Smart"
    if not folder.is_dir():
        return []
    known = saved if saved is not None else definitions(root)
    import playlist_edit
    removed: list[str] = []
    with playlist_edit.at_root(root):
        for path in sorted(folder.glob("*.m3u8")):
            if path.stem in known:
                continue
            try:
                playlist_edit.journal_write(
                    playlist_edit.journal_for(path, "delete"))
                path.unlink()
            except OSError:
                continue
            removed.append(path.name)
    return removed


def build_all(root: Path, *, rows: list[dict] | None = None,
              axes: dict | None = None, plays: dict | None = None,
              dry_run: bool = False) -> dict:
    """Rebuild every saved smart list; returns per-list results."""
    root = Path(root)
    saved = definitions(root)
    if rows is None:
        from .catalog import Catalog
        rows = Catalog(root).rows
    built = []
    for name, entry in saved.items():
        built.append(build(root, name, entry.get("rules"), rows=rows,
                           axes=axes, plays=plays, dry_run=dry_run,
                           prune=False))
    return {"lists": built, "count": len(built),
            "tracks": sum(item["count"] for item in built),
            "total": len(rows), "dry_run": bool(dry_run)}


def remove(root: Path, name: str) -> dict:
    """Delete a smart list and its rules, journalling the playlist first."""
    root = Path(root)
    name = clean_name(name)
    saved = definitions(root)
    if name not in saved:
        raise ValueError(f"There is no smart playlist called “{name}”.")
    entry = saved.pop(name)
    _write_state(root, saved)
    import playlist_edit
    written = Path(root) / str(entry.get("path") or
                               f"Playlists/Smart/{name}.m3u8")
    removed = False
    with playlist_edit.at_root(root):
        folder = (Path(root) / "Playlists" / "Smart").resolve()
        try:
            inside = written.resolve().parent == folder
        except OSError:
            inside = False
        if inside and written.is_file():
            playlist_edit.journal_write(
                playlist_edit.journal_for(written, "delete"))
            written.unlink()
            removed = True
    return {"name": name, "removed": removed, "rules": entry.get("rules") or {}}


def suggest_name(rules: dict) -> str:
    """A readable name for a rule set that was never named."""
    bits: list[str] = []
    for key in ("genre", "artist", "album", "grouping"):
        if rules.get(key):
            bits.append(str(rules[key][0]))
    year_from = rules.get("year_from")
    if year_from:
        bits.append(f"{year_from}s" if year_from % 10
                    else f"{str(year_from)[:3]}0s")
    if rules.get("added_days"):
        bits.append(f"new_{rules['added_days']}d")
    if rules.get("axes"):
        bits.append("_".join(rules["axes"])[:24])
    if rules.get("sort") == "shuffle":
        bits.append("mix")
    name = "_".join(bit for bit in bits if bit) or "Smart"
    return name[:MAX_NAME]
