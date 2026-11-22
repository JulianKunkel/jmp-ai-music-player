"""Read the stored lyrics, and search them roughly.

The transcribing itself is done by the library's own tool
(`scripts/transcribe_lyrics.py`), which writes

    library-work/app/lyrics/lyrics.db

with one `track` row per song (model, language, source size/mtime) and one
`line` row per timed lyric line, plus an FTS5 index over the lines.  This
module only reads that database: the app never transcribes in-process and
never touches an audio file.

The search matches **whole words** and their plain plural forms: "boat" finds
"boat" and "boats", "sea" finds "sea" and "seas" — it does not reach
"steamboat", "seaside" or "search".  Every requested word must appear when
possible, otherwise any of them; songs that return to the word rank first.
Only when nothing matches exactly does a spelling fallback compare the stored
words and flag its hits as fuzzy.

A timing shift the listener sets is *app state*
(`library-work/app/lyrics/offsets.json`), not a database edit: `lines()`
applies it, so the panel, the chat and the `.lrc` export all agree, and a
re-transcription keeps the correction.

`lrc(rel)` renders the timed lines as an LRC file; writing it next to the
audio is the one library write in this area and lives in `chat/lrc.py`, which
journals the previous state first.

`combine()` is the piece the chat uses for "something with a boat and fast":
the lyric hits keep their line and timestamp, and an optional sound ranking
reorders them by the measured profile axes.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

#: how far a track's lyrics may be moved, in seconds.  A transcription guesses
#: where a line begins; it is not a re-edit of the song, so a large shift is a
#: mistake, not a preference.
MAX_OFFSET = 30.0

#: the same stopword list the transcribing tool uses, so both agree
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "with", "without", "about", "of",
    "in", "on", "at", "to", "for", "from", "by", "that", "this", "these",
    "those", "is", "are", "was", "were", "be", "been", "being", "it", "its",
    "some", "something", "song", "songs", "track", "tracks", "music", "piece",
    "lyric", "lyrics", "line", "lines", "word", "words", "sings", "sing",
    "singing", "mention", "mentions", "mentioned", "talks", "talk", "text",
    "where", "who", "which", "what", "me", "my", "i", "you", "your", "we",
    "ein", "eine", "einen", "der", "die", "das", "und", "oder", "mit", "ohne",
    "über", "ueber", "von", "im", "am", "auf", "für", "fuer", "ist", "sind",
    "war", "waren", "es", "etwas", "lied", "lieder", "songtext", "singt",
    "singen", "zeile", "zeilen", "wort", "worte", "wörter",
}


def db_path(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "lyrics" / "lyrics.db"


def offsets_path(root: Path) -> Path:
    """Where the per-track timing shifts live (app state, never the database).

    The database belongs to the transcribing tool; a nudge of the timing is a
    listener's correction, so it has its own small file and survives a
    re-transcription.
    """
    return Path(root) / "library-work" / "app" / "lyrics" / "offsets.json"


def read_offsets(root: Path | str) -> dict[str, float]:
    try:
        data = json.loads(offsets_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in data.items():
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds and abs(seconds) <= MAX_OFFSET:
            out[str(key)] = round(seconds, 1)
    return out


def write_offsets(root: Path | str, data: dict[str, float]) -> None:
    """Write the shifts atomically; an empty map removes the file's content."""
    target = offsets_path(root)
    clean = {str(key): round(float(value), 1) for key, value in data.items()
             if value and abs(float(value)) <= MAX_OFFSET}
    payload = json.dumps(dict(sorted(clean.items())), ensure_ascii=False,
                         indent=1)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("." + target.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def models_path(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "lyrics" / "models"


def query_tokens(text: str) -> list[str]:
    """The words a rough lyric search actually uses."""
    tokens: list[str] = []
    for token in re.split(r"[^0-9A-Za-zÄÖÜäöüß']+", str(text or "").casefold()):
        token = token.strip("'")
        if len(token) < 2 or token in STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def word_forms(token: str) -> list[str]:
    """A word and its plain inflections — never another word it is part of.

    "sea" finds "sea" and "seas", but not "seaside" or "search"; "boat" finds
    "boats" without reaching "steamboat".  Songs about the sea should not be
    songs that merely contain the three letters.
    """
    token = str(token or "").casefold()
    if not token:
        return []
    forms = [token]
    if token.endswith("ies") and len(token) > 4:
        forms.append(token[:-3] + "y")
    elif token.endswith("es") and len(token) > 3:
        forms += [token[:-2], token[:-1]]
    elif token.endswith("s") and len(token) > 3:
        forms.append(token[:-1])
    if token.endswith(("s", "x", "z", "ch", "sh")):
        forms.append(token + "es")
    elif token.endswith("y") and len(token) > 2:
        forms.append(token[:-1] + "ies")
    else:
        forms.append(token + "s")
    out: list[str] = []
    for form in forms:
        if form and form not in out:
            out.append(form)
    return out


def _fts_query(tokens: list[str], mode: str) -> str:
    """An exact word query: each token matches only its own forms."""
    joiner = " AND " if mode == "and" else " OR "
    groups = []
    for token in tokens:
        forms = " OR ".join(f'"{form}"' for form in word_forms(token))
        groups.append(f"({forms})")
    return joiner.join(groups)


def _word_in(token: str, text: str) -> bool:
    """Whether one of the token's forms stands in the text as a whole word."""
    folded = str(text or "").casefold()
    return any(re.search(r"(?<!\w)" + re.escape(form) + r"(?!\w)", folded)
               for form in word_forms(token))


def _fuzzy_scan(conn: sqlite3.Connection, tokens: list[str]) -> list[sqlite3.Row]:
    """A misspelling fallback: the closest whole word, never a substring.

    Only words of the stored lines are compared, and only after an exact
    search found nothing, so "piza" can still reach "pizza" while "sea" never
    reaches "seaside".
    """
    from difflib import SequenceMatcher

    wanted = tokens[0]
    if len(wanted) < 3:
        return []
    rows = conn.execute(
        "SELECT l.rel_path, l.idx, l.start_s, l.end_s, l.text, 0 AS score, "
        "t.title, t.artist, t.album, t.duration_s "
        "FROM line l LEFT JOIN track t ON t.rel_path = l.rel_path "
        "LIMIT 20000").fetchall()
    found: list[sqlite3.Row] = []
    for row in rows:
        for word in re.findall(r"[0-9A-Za-zÄÖÜäöüß']+", str(row["text"] or "")):
            word = word.strip("'").casefold()
            if not word or word in STOPWORDS:
                continue
            if SequenceMatcher(None, wanted, word).ratio() >= 0.84:
                found.append(row)
                break
    return found


class Lyrics:
    """Read-only access to the lyrics database."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = db_path(self.root)

    # -- availability ------------------------------------------------------

    def available(self) -> bool:
        return self.path.is_file()

    def _connect(self) -> sqlite3.Connection | None:
        if not self.available():
            return None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                   timeout=2.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            return None

    def count(self) -> int:
        conn = self._connect()
        if conn is None:
            return 0
        try:
            return int(conn.execute("SELECT COUNT(*) FROM track").fetchone()[0])
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    def lines_count(self) -> int:
        conn = self._connect()
        if conn is None:
            return 0
        try:
            return int(conn.execute(
                "SELECT COALESCE(SUM(line_count), 0) FROM track").fetchone()[0])
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    def summary(self) -> dict:
        conn = self._connect()
        if conn is None:
            return {"available": False, "tracks": 0, "lines": 0, "models": [],
                    "transcribed_at": ""}
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS tracks, COALESCE(SUM(line_count), 0) AS "
                "lines, MAX(transcribed_at) AS last FROM track").fetchone()
            models = [dict(entry) for entry in conn.execute(
                "SELECT model, COUNT(*) AS n FROM track GROUP BY model")]
            return {"available": True, "tracks": int(row["tracks"] or 0),
                    "lines": int(row["lines"] or 0), "models": models,
                    "transcribed_at": row["last"] or ""}
        except sqlite3.Error:
            return {"available": False, "tracks": 0, "lines": 0, "models": [],
                    "transcribed_at": ""}
        finally:
            conn.close()

    # -- one track ---------------------------------------------------------

    def track(self, rel: str) -> dict | None:
        conn = self._connect()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT * FROM track WHERE rel_path = ?",
                               (rel,)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if row is None:
            return None
        return {"rel": row["rel_path"], "title": row["title"] or "",
                "artist": row["artist"] or "", "album": row["album"] or "",
                "duration": float(row["duration_s"] or 0),
                "language": row["language"] or "", "model": row["model"] or "",
                "backend": row["backend"] or "",
                "transcribed_at": row["transcribed_at"] or "",
                "line_count": int(row["line_count"] or 0),
                "text": row["text"] or ""}

    def offset(self, rel: str) -> float:
        """How far this song's lyric timings were nudged, in seconds."""
        return read_offsets(self.root).get(rel, 0.0)

    def set_offset(self, rel: str, seconds: float) -> float:
        """Store a timing shift for one song; 0 removes it again."""
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            raise ValueError("the offset must be a number of seconds")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("the offset must be a finite number of seconds")
        value = max(-MAX_OFFSET, min(MAX_OFFSET, round(value, 1)))
        data = read_offsets(self.root)
        if abs(value) < 0.05:
            data.pop(rel, None)
            value = 0.0
        else:
            data[rel] = value
        write_offsets(self.root, data)
        return value

    def lines(self, rel: str) -> list[dict]:
        """The timed lines, already shifted by the stored offset."""
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT idx, start_s, end_s, text FROM line WHERE rel_path = ? "
                "ORDER BY idx", (rel,)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        shift = self.offset(rel)

        def shifted(value) -> float:
            # a line can never begin before the song does
            return max(0.0, float(value or 0.0) + shift)

        return [{"index": int(row["idx"]),
                 "start": shifted(row["start_s"]),
                 "end": max(shifted(row["start_s"]),
                            shifted(row["end_s"])),
                 "text": row["text"]} for row in rows]

    # -- the .lrc form -----------------------------------------------------

    def lrc(self, rel: str) -> str:
        """The timed lyrics as an LRC file, or "" when there are none."""
        row = self.track(rel)
        lines = self.lines(rel)
        if row is None or not lines:
            return ""
        return render_lrc(row, lines)

    def is_current(self, rel: str, size: int, mtime: float) -> bool:
        """True when the stored lyrics belong to this exact file."""
        conn = self._connect()
        if conn is None:
            return False
        try:
            row = conn.execute(
                "SELECT source_size, source_mtime FROM track WHERE rel_path = ?",
                (rel,)).fetchone()
        except sqlite3.Error:
            return False
        finally:
            conn.close()
        if row is None:
            return False
        return (int(row["source_size"] or -1) == int(size)
                and str(row["source_mtime"] or "") == f"{mtime:.0f}")

    # -- rough search ------------------------------------------------------

    def search(self, text: str, limit: int = 40, mode: str = "and"
               ) -> list[dict]:
        """Songs whose sung *words* match, the ones that keep saying it first.

        Matching is by whole word (with the plain plural forms), never by a
        prefix or substring: "sea" does not reach "seaside" or "search".
        Only when nothing matches exactly does a spelling fallback look for
        the closest stored word and flag the hits as ``fuzzy``.
        """
        tokens = query_tokens(text)
        if not tokens:
            return []
        conn = self._connect()
        if conn is None:
            return []
        rows: list[dict] = []
        fuzzy = False
        try:
            for attempt in (mode, "or"):
                try:
                    found = conn.execute(
                        "SELECT f.rel_path, f.idx, f.start_s, f.end_s, f.text, "
                        "bm25(line_fts) AS score, t.title, t.artist, t.album, "
                        "t.duration_s "
                        "FROM line_fts f "
                        "LEFT JOIN track t ON t.rel_path = f.rel_path "
                        "WHERE line_fts MATCH ? ORDER BY score LIMIT ?",
                        (_fts_query(tokens, attempt),
                         max(int(limit) * 8, 160))).fetchall()
                except sqlite3.OperationalError:
                    found = []
                rows = [self._hit(row, tokens) for row in found]
                if rows:
                    break
            if not rows:
                found = _fuzzy_scan(conn, tokens)
                rows = [self._hit(row, tokens) for row in found]
                fuzzy = bool(rows)
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        # several lines of one song are one hit: keep the best line per track
        best: dict[str, dict] = {}
        for hit in rows:
            current = best.get(hit["rel"])
            if current is None or hit["score"] < current["score"]:
                best[hit["rel"]] = hit
            else:
                best[hit["rel"]]["also"] += 1
        for hit in best.values():
            hit["mentions"] = 1 + int(hit.get("also") or 0)
            hit["fuzzy"] = fuzzy
            # one passing line is a mention, not a theme: a song really sings
            # about the word when it comes back to it or names the song with it
            hit["about"] = bool(hit["mentions"] > 1 or any(
                _word_in(token, hit.get("title") or "") for token in tokens))
        # a song that returns to the word is more "about" it than one that
        # drops it once; the text match ranks inside that
        ordered = sorted(best.values(),
                         key=lambda hit: (-hit["mentions"], hit["score"]))
        return ordered[:max(1, int(limit))]

    def _hit(self, row: sqlite3.Row, tokens: list[str]) -> dict:
        text = row["text"]
        return {
            "rel": row["rel_path"],
            "line": int(row["idx"] or 0),
            "start": float(row["start_s"] or 0.0),
            "end": float(row["end_s"] or 0.0),
            "text": text,
            "score": round(float(row["score"] or 0.0), 4),
            "title": row["title"] or "",
            "artist": row["artist"] or "",
            "album": row["album"] or "",
            "duration": float(row["duration_s"] or 0),
            "matched": [token for token in tokens if _word_in(token, text)],
            "also": 0,
            "mentions": 1,
            "fuzzy": False,
            "about": False,
        }

    # -- the chat's combined search ---------------------------------------

    def find(self, text: str, limit: int = 20) -> list[dict]:
        """Lyric hits, one per song, with the matching line emphasised."""
        hits = self.search(text, limit=max(limit * 4, 40))
        out = []
        for hit in hits[:max(1, min(60, limit))]:
            out.append({
                "rel": hit["rel"],
                "title": hit["title"],
                "artist": hit["artist"],
                "album": hit["album"],
                "line": hit["text"],
                "start": hit["start"],
                "end": hit["end"],
                "matched": hit["matched"],
                "also": hit["also"],
                "mentions": hit.get("mentions", 1),
                "about": bool(hit.get("about")),
                "fuzzy": bool(hit.get("fuzzy")),
                "duration": hit["duration"],
            })
        return out


def lrc_time(seconds: float) -> str:
    """`[mm:ss.xx]` — LRC counts hundredths, not milliseconds."""
    centis = max(0, int(round(float(seconds or 0) * 100)))
    return f"{centis // 6000:02d}:{(centis % 6000) // 100:02d}.{centis % 100:02d}"


def _header(value: str) -> str:
    # a header tag ends at the first bracket, so a title cannot break the file
    text = " ".join(str(value or "").replace("[", "(").replace("]", ")").split())
    return text


def render_lrc(row: dict, lines: list[dict]) -> str:
    """The LRC text for one track: header tags, then one line per cue."""
    out = ["[by:JulAIn Music Player]"]
    for tag, field in (("ti", "title"), ("ar", "artist"), ("al", "album")):
        value = _header(row.get(field) or "")
        if value:
            out.append(f"[{tag}:{value}]")
    out.append(f"[length:{lrc_time(row.get('duration') or 0)}]")
    out.append("")
    for line in lines:
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        out.append(f"[{lrc_time(line.get('start') or 0)}]{text}")
    return "\n".join(out) + "\n"


def clock(seconds: float) -> str:
    seconds = max(0, int(round(float(seconds or 0))))
    return f"{seconds // 60}:{seconds % 60:02d}"


def combine(hits: list[dict], ranking: list[dict] | None,
            limit: int = 12) -> list[dict]:
    """Order lyric hits by a sound ranking, keeping the lyric line.

    ``ranking`` is what `Psycho.rank` returns (``rel`` + ``score``).  Without
    one the lyric match order is kept, so "a song about a boat" stays a text
    search and "a song about a boat and fast" becomes a text search narrowed by
    the measured axes.
    """
    def order(rows: list[dict]) -> list[dict]:
        """Songs that really sing the word first, then the passing mentions.

        Inside each group the requested sound wins; a song with no measurement
        follows the measured ones instead of being dropped.
        """
        return sorted(rows, key=lambda hit: (
            0 if hit.get("about") else 1,
            -int(hit.get("mentions") or 1),
            0 if hit.get("sound") is not None else 1,
            -(hit.get("sound") or 0.0)))

    if not ranking:
        return order([dict(hit) for hit in hits])[:max(1, int(limit))]
    scores = {row["rel"]: float(row["score"]) for row in ranking}
    entries: list[dict] = []
    for hit in hits:
        entry = dict(hit)
        if hit["rel"] in scores:
            entry["sound"] = round(scores[hit["rel"]], 4)
        entries.append(entry)
    return order(entries)[:max(1, int(limit))]
