"""What was actually listened to — one row per play, and the numbers it feeds.

The old `profile.json` keeps a `recent` list of the last 120 tracks so the
learner knows what it just saw.  That list is a rolling window, not a record:
it forgets, it is rewritten on every save, and it cannot answer "what did I
play most this year?".

This module is the record.  It is one small SQLite file under the app state:

```
library-work/app/chat/plays.db
└── play(id, rel_path, started_at, ended_at, seconds, duration_s,
         fraction, source)
```

One row per **finished** listen, written by `Engine.poll()` when the track
changes (the same moment the profile learns from it).  A row is only written
when the listen is real: at least `MIN_HEARD` seconds, or half of a track that
is shorter than that, and never for a few-second skip.  Nothing here touches
audio, a tag or a playlist — the database is app state, like the profile.

Honest limits, because a history that pretends is worse than none:

- it starts when this release first runs.  "Never played" therefore means
  *not played since <since>*, and the answer says so with the date;
- a listen counts once the track is left, so a song that is paused and never
  resumed is not recorded;
- a seek to the end counts as a play, because every player decides this from
  the last observed playhead;
- a row is keyed by the library-relative path.  A renamed file keeps its play
  history only if the renaming tool also updated this table (it does not), so
  a stale row is reported as `gone` instead of being guessed at.

Reads never create the database: a library where nothing has played yet simply
has no file, and every reader answers with `available: False`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import stats
from .catalog import AUDIO_EXT, fold, library_rel

#: where the record lives (app state, never inside the music tree)
DB = Path("library-work") / "app" / "chat" / "plays.db"

#: bumped only when the table shape changes; readers ignore a newer file
SCHEMA = 1

#: a listen shorter than this is a skip, not a play
SKIP_BELOW = 5.0

#: …and a listen counts after this many seconds, or half of a shorter track
MIN_HEARD = 20.0
MIN_FRACTION = 0.5

#: what one answer lists when it is not told otherwise
DEFAULT_LIMIT = 20

#: the "forgotten" window when the sentence does not name one
FORGOTTEN_DAYS = 365

#: the caveat printed once with every history answer
NOTE = ("This history starts when the feature first ran, so “never played” "
        "means not played since then — and a track counts once you leave it.")


# --------------------------------------------------------------------------
# the database


def db_path(root: Path) -> Path:
    return Path(root) / DB


def _connect(root: Path, create: bool = False) -> sqlite3.Connection | None:
    """The connection, or None when there is nothing to read (or write)."""
    path = db_path(root)
    if not path.is_file() and not create:
        return None
    try:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS play (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    rel_path   TEXT    NOT NULL,
                    started_at REAL    NOT NULL,
                    ended_at   REAL    NOT NULL,
                    seconds    REAL    NOT NULL,
                    duration_s REAL    NOT NULL DEFAULT 0,
                    fraction   REAL    NOT NULL DEFAULT 0,
                    source     TEXT    NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS play_rel ON play(rel_path);
                CREATE INDEX IF NOT EXISTS play_started ON play(started_at);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,
                                                 value TEXT);
                """
            )
            conn.commit()
        return conn
    except (OSError, sqlite3.Error):
        return None


def _epoch(at=None) -> float:
    if at is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(at, (int, float)):
        return float(at)
    try:
        parsed = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc).timestamp()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso(stamp: float | None) -> str:
    if not stamp:
        return ""
    try:
        return datetime.fromtimestamp(float(stamp), timezone.utc) \
            .astimezone().strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _day(stamp: float | None) -> str:
    if not stamp:
        return ""
    try:
        return datetime.fromtimestamp(float(stamp), timezone.utc) \
            .astimezone().strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


# --------------------------------------------------------------------------
# writing


def counts_as_play(heard_s, duration_s) -> bool:
    """Is this listen a play, or just a skip?"""
    try:
        heard = float(heard_s or 0)
        duration = float(duration_s or 0)
    except (TypeError, ValueError):
        return False
    if heard < SKIP_BELOW:
        return False
    threshold = MIN_HEARD
    if duration > 0:
        threshold = min(MIN_HEARD, duration * MIN_FRACTION)
    return heard >= threshold


def record(root: Path, rel: str, heard_s, duration_s, *,
           at=None, source: str = "") -> bool:
    """Append one finished listen.  Never raises — this runs inside poll()."""
    rel = library_rel(str(rel or "").strip())
    if not rel:
        return False
    try:
        heard = max(0.0, float(heard_s or 0))
        duration = max(0.0, float(duration_s or 0))
    except (TypeError, ValueError):
        return False
    if not counts_as_play(heard, duration):
        return False
    ended = _epoch(at)
    started = ended - heard
    fraction = min(1.0, heard / duration) if duration > 0 else 0.0
    conn = _connect(root, create=True)
    if conn is None:
        return False
    try:
        conn.execute(
            "INSERT INTO play (rel_path, started_at, ended_at, seconds,"
            " duration_s, fraction, source) VALUES (?,?,?,?,?,?,?)",
            (rel, started, ended, round(heard, 2), round(duration, 2),
             round(fraction, 4), str(source or "")),
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('first_play', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (str(ended),),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def clear(root: Path) -> int:
    """Forget the whole record; returns how many plays were removed."""
    conn = _connect(root)
    if conn is None:
        return 0
    try:
        total = int(conn.execute("SELECT COUNT(*) FROM play").fetchone()[0])
        conn.execute("DELETE FROM play")
        conn.execute("DELETE FROM meta WHERE key = 'first_play'")
        conn.commit()
        return total
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# reading


def _aggregate(root: Path, since: float | None = None) -> list[dict]:
    """Per track: plays, heard seconds and the last listen."""
    conn = _connect(root)
    if conn is None:
        return []
    query = ("SELECT rel_path, COUNT(*) AS plays, SUM(seconds) AS heard,"
             " MAX(ended_at) AS last FROM play")
    params: tuple = ()
    if since is not None:
        query += " WHERE ended_at >= ?"
        params = (since,)
    query += " GROUP BY rel_path"
    try:
        rows = [dict(row) for row in conn.execute(query, params)]
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    for row in rows:
        row["rel_path"] = library_rel(str(row.get("rel_path") or ""))
        row["plays"] = int(row.get("plays") or 0)
        row["heard"] = float(row.get("heard") or 0)
        row["last"] = float(row.get("last") or 0)
    return [row for row in rows if row["rel_path"]]


def _recent(root: Path, limit: int) -> list[dict]:
    conn = _connect(root)
    if conn is None:
        return []
    try:
        rows = [dict(row) for row in conn.execute(
            "SELECT rel_path, started_at, ended_at, seconds, duration_s,"
            " fraction FROM play ORDER BY ended_at DESC LIMIT ?",
            (max(1, int(limit)),))]
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    for row in rows:
        row["rel_path"] = library_rel(str(row.get("rel_path") or ""))
    return rows


def _tracks(root: Path) -> dict[str, dict]:
    """The catalogue, keyed by rel path — titles for the numbers."""
    out: dict[str, dict] = {}
    for row in stats.rows(root):
        rel = library_rel(str(row.get("rel_path") or ""))
        ext = str(row.get("ext") or Path(rel).suffix).casefold()
        if not rel or ext not in AUDIO_EXT:
            continue
        out[rel] = {
            "rel": rel,
            "title": str(row.get("title") or "").strip()
                     or Path(rel).stem.replace("_", " "),
            "artist": str(row.get("artist") or row.get("albumartist")
                          or "").strip(),
            "album": str(row.get("album") or "").strip(),
            "genre": str(row.get("genre") or "").strip(),
            "duration_s": round(stats.number(row.get("duration_s")), 1),
        }
    return out


def played_index(root: Path) -> dict[str, dict]:
    """rel path -> {"plays": n, "last": epoch} for every track ever played.

    The one shape `smart.matches()` and the summary both want; a library with
    no record answers with an empty mapping, not an error.
    """
    return {row["rel_path"]: {"plays": row["plays"], "last": row["last"]}
            for row in _aggregate(root)}


def play_counts(root: Path) -> dict[str, int]:
    """rel path -> how often it was played (all time)."""
    return {rel: int(entry["plays"])
            for rel, entry in played_index(root).items()}


def last_played(root: Path) -> dict[str, float]:
    """rel path -> the epoch of the last listen (all time)."""
    return {rel: float(entry["last"])
            for rel, entry in played_index(root).items()}


def _artist_name(row: dict) -> str:
    return str(row.get("artist") or row.get("albumartist") or "").strip() \
        or "unknown artist"


def summary(root: Path, days: int | None = None,
            limit: int = DEFAULT_LIMIT,
            forgotten_days: int = FORGOTTEN_DAYS) -> dict:
    """Everything one answer or one panel needs, from app state only."""
    root = Path(root)
    limit = max(1, min(200, int(limit or DEFAULT_LIMIT)))
    try:
        forgotten_days = max(1, min(3650, int(forgotten_days)))
    except (TypeError, ValueError):
        forgotten_days = FORGOTTEN_DAYS
    since = None
    if days:
        since = datetime.now(timezone.utc).timestamp() - int(days) * 86400

    tracks = _tracks(root)
    aggregate = _aggregate(root, since)
    known = [row for row in aggregate if row["rel_path"] in tracks]
    gone = len(aggregate) - len(known)

    conn = _connect(root)
    first = 0.0
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT MIN(ended_at) FROM play").fetchone()
            first = float(row[0] or 0) if row else 0.0
        except sqlite3.Error:
            first = 0.0
        finally:
            conn.close()

    plays = sum(row["plays"] for row in known)
    heard = sum(row["heard"] for row in known)

    artists: dict[str, dict] = {}
    genres: dict[str, dict] = {}
    for row in known:
        info = tracks[row["rel_path"]]
        name = _artist_name(info)
        key = fold(name) or name.casefold()
        entry = artists.setdefault(key, {"name": name, "plays": 0,
                                         "tracks": 0})
        entry["plays"] += row["plays"]
        entry["tracks"] += 1
        genre = info["genre"]
        if genre:
            gkey = fold(genre) or genre.casefold()
            bucket = genres.setdefault(gkey, {"name": genre, "plays": 0})
            bucket["plays"] += row["plays"]

    def top(bucket: dict, cut: int) -> list[dict]:
        return sorted(bucket.values(),
                      key=lambda item: (-item["plays"], item["name"]))[:cut]

    top_tracks = []
    for row in sorted(known, key=lambda item: (-item["plays"], -item["heard"],
                                              -item["last"]))[:limit]:
        info = tracks[row["rel_path"]]
        top_tracks.append({
            **info,
            "plays": row["plays"],
            "seconds": round(row["heard"], 1),
            "last": _iso(row["last"]),
        })

    recent = []
    for row in _recent(root, limit * 2):
        info = tracks.get(row["rel_path"])
        if info is None:
            continue                       # a renamed or removed file
        recent.append({
            **info,
            "at": _iso(row["ended_at"]),
            "heard_s": round(float(row["seconds"] or 0), 1),
            "fraction": round(float(row["fraction"] or 0), 2),
        })
        if len(recent) >= limit:
            break

    # forgotten: played before, but not for a long time.  All-time, not the
    # window: "not heard in a year" means a year, whatever the range shows.
    all_time = {row["rel_path"]: row for row in _aggregate(root)}
    cutoff = (datetime.now(timezone.utc).timestamp()
              - forgotten_days * 86400)
    forgotten = []
    for rel, row in all_time.items():
        info = tracks.get(rel)
        if info is None or row["last"] > cutoff:
            continue
        forgotten.append({**info,
                          "plays": row["plays"],
                          "last": _iso(row["last"]),
                          "days": max(0, int(
                              (datetime.now(timezone.utc).timestamp()
                               - row["last"]) / 86400))})
    forgotten.sort(key=lambda item: item["last"])
    forgotten = forgotten[:limit]

    never = [info for rel, info in tracks.items() if rel not in all_time]
    never.sort(key=lambda item: (fold(_artist_name(item)),
                                 fold(item["title"])))
    played = sum(1 for rel in tracks if rel in all_time)
    return {
        "available": bool(first or aggregate),
        "root": str(root),
        "plays": plays,
        "tracks": len(known),
        "artists": len(artists),
        "hours": round(heard / 3600.0, 2),
        "seconds": round(heard, 1),
        "days": int(days) if days else None,
        "first": _iso(first),
        "first_day": _day(first),
        "top_artists": top(artists, limit),
        "top_genres": top(genres, min(8, limit)),
        "top_tracks": top_tracks,
        "recent": recent,
        "forgotten": forgotten,
        "forgotten_days": forgotten_days,
        "never": {"count": len(never), "sample": never[:limit]},
        "covered": played,
        "catalogue": len(tracks),
        "gone": gone,
        "note": NOTE,
        "generated": _iso(datetime.now(timezone.utc).timestamp()),
    }


# --------------------------------------------------------------------------
# rendering


def markdown(data: dict, focus: str = "", limit: int = 12) -> str:
    """The chat answer.  `focus` narrows it to one slice."""
    if not data.get("available"):
        return ("**No listening history yet.**  Plays are recorded from now "
                "on, one row each time a track is left after at least "
                f"{int(MIN_HEARD)} seconds (or half of a shorter track).  "
                "Start something and ask again later.")

    focus = str(focus or "").casefold()
    head = (f"**Listening history** · {data['plays']:,} plays · "
            f"{data['tracks']:,} tracks · {data['hours']:,.1f} h heard")
    if data.get("days"):
        head += f" · last {data['days']} days"
    elif data.get("first_day"):
        head += f" · since {data['first_day']}"
    lines = [head]
    if data.get("gone"):
        lines.append(f"({data['gone']:,} older plays name a file that is no "
                     "longer in the library)")

    def show_artists() -> list[str]:
        rows = data.get("top_artists") or []
        if not rows:
            return ["\nNo artist has been played yet."]
        out = ["\n**Top artists**"]
        for item in rows[:limit]:
            out.append(f"- **{_escape(item['name'])}** · "
                       f"{item['plays']:,} plays · {item['tracks']:,} tracks")
        return out

    def show_tracks() -> list[str]:
        rows = data.get("top_tracks") or []
        if not rows:
            return ["\nNothing has been played yet."]
        out = ["\n**Most played**"]
        for number, item in enumerate(rows[:limit], 1):
            out.append(f"{number}. **{_escape(_artist_name(item))} — "
                       f"{_escape(item['title'])}**  ·  "
                       f"{item['plays']:,} plays · last {item['last']}")
            out.append(f"   `{item['rel']}`")
        return out

    def show_genres() -> list[str]:
        rows = data.get("top_genres") or []
        if not rows:
            return ["\nNo genre has been played yet."]
        return ["\n**Genres** · " + " · ".join(
            f"{_escape(item['name'])} {item['plays']:,}" for item in rows)]

    def show_recent() -> list[str]:
        rows = data.get("recent") or []
        if not rows:
            return ["\nNothing has been played yet."]
        out = ["\n**Recently played**"]
        for item in rows[:limit]:
            out.append(f"- `{item['at']}` · **{_escape(_artist_name(item))} — "
                       f"{_escape(item['title'])}**  ·  "
                       f"{int(round(item['fraction'] * 100))} %")
        return out

    def show_forgotten() -> list[str]:
        rows = data.get("forgotten") or []
        if not rows:
            return ["\nEvery track that was ever played has been heard in the "
                    f"last {data.get('forgotten_days', FORGOTTEN_DAYS)} days."]
        out = [f"\n**Not heard in {data.get('forgotten_days', FORGOTTEN_DAYS)}"
               " days**"]
        for item in rows[:limit]:
            out.append(f"- **{_escape(_artist_name(item))} — "
                       f"{_escape(item['title'])}**  ·  last {item['last']} "
                       f"({item['days']:,} days ago)")
        return out

    def show_never() -> list[str]:
        never = data.get("never") or {}
        count = int(never.get("count") or 0)
        if not count:
            return ["\nEvery track in the library has been played at least "
                    "once since the history began."]
        out = [f"\n**Never played** · {count:,} of "
               f"{data.get('catalogue', 0):,} tracks"
               + (f" (since {data['first_day']})" if data.get("first_day")
                  else "")]
        for item in (never.get("sample") or [])[:limit]:
            out.append(f"- **{_escape(_artist_name(item))} — "
                       f"{_escape(item['title'])}**")
        return out

    if focus in {"artists", "artist", "top", "most"}:
        lines += show_artists()
        lines += show_tracks()
    elif focus in {"genres", "genre"}:
        lines += show_genres()
    elif focus in {"recent", "recently", "last"}:
        lines += show_recent()
    elif focus in {"forgotten", "rediscover", "old"}:
        lines += show_forgotten()
    elif focus in {"never", "unplayed"}:
        lines += show_never()
    else:
        lines += show_artists()
        lines += show_tracks()
        lines += show_genres()
        lines += show_recent()
        if data.get("forgotten"):
            lines += show_forgotten()
    lines.append("\n" + str(data.get("note") or NOTE))
    return "\n".join(lines)


def _escape(text: str) -> str:
    return str(text).replace("*", "\\*").replace("_", "\\_") \
        .replace("`", "\\`")


def _artist_name(item: dict) -> str:
    return str(item.get("artist") or item.get("albumartist") or "").strip() \
        or "unknown artist"
