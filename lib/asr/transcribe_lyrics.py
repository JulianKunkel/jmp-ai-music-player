#!/usr/bin/env python3
"""Transcribe the library's songs into lyrics, locally, with timestamps.

Everything stays on this machine: the only backend is a local Whisper model
run by `faster-whisper` (or `whisper.cpp` when no Python backend is
installed).  No audio, no text and no request leaves the box.  The script
writes **only** its own database

    library-work/app/lyrics/lyrics.db

with one `track` row per song and one `line` row per timed lyric line; a
full-text index (`line_fts`, SQLite FTS5) makes the rough search fast.  No
music file is opened for writing and no tag is touched.

    scripts/venv/bin/python scripts/transcribe_lyrics.py --check
    scripts/venv/bin/python scripts/transcribe_lyrics.py --rel Artists/…/x.mp3
    scripts/venv/bin/python scripts/transcribe_lyrics.py --missing --limit 5
    scripts/venv/bin/python scripts/transcribe_lyrics.py --print Artists/…/x.mp3
    scripts/venv/bin/python scripts/transcribe_lyrics.py --search "boat and sea"
    scripts/venv/bin/python scripts/transcribe_lyrics.py --stats
    scripts/venv/bin/python scripts/transcribe_lyrics.py --text /tmp/voice.wav

`--text FILE` transcribes one arbitrary audio file (the app's voice input does
this) and prints one JSON line — no database row, no library path needed.

The interpreter that runs this file needs `faster_whisper`; the app calls
`library-work/app/lyrics/asr-venv/bin/python` when it exists (created by
`setup_asr.sh`).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

def _library_root() -> Path:
    """The library this run works on.

    A packaged copy of this script lives inside the app, not in the library,
    so the folder it is in says nothing about the music.  ``MUSIC_LIBRARY``
    wins; the app starts the job with the library as its working directory;
    only a direct run from the repository layout falls back to the parent of
    the containing folder.
    """
    env = os.environ.get("MUSIC_LIBRARY")
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / "library-work").is_dir() or (cwd / "Playlists").is_dir():
        return cwd.resolve()
    return Path(__file__).resolve().parent.parent


ROOT = _library_root()
INVENTORY = ROOT / "library-work" / "inventory" / "files.jsonl"
DEFAULT_DB = Path("library-work/app/lyrics/lyrics.db")
MODELS = ROOT / "library-work" / "app" / "lyrics" / "models"

#: what the transcriber can read; chiptunes and SID/YM are excluded on purpose
AUDIO_SUFFIXES = {".mp3", ".mp2", ".m4a", ".mp4", ".opus", ".ogg", ".oga",
                  ".flac", ".wav", ".webm", ".wma", ".aac"}

#: words that carry no meaning for a lyric search
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "with", "without", "about", "of",
    "in", "on", "at", "to", "for", "from", "by", "that", "this", "these",
    "those", "is", "are", "was", "were", "be", "been", "being", "it", "its",
    "some", "something", "song", "songs", "track", "tracks", "music", "piece",
    "lyric", "lyrics", "line", "lines", "word", "words", "sings", "sing",
    "singing", "mention", "mentions", "mentioned", "talks", "talk", "text",
    "where", "who", "which", "what", "me", "my", "i", "you", "your", "we",
    "ein", "eine", "einen", "der", "die", "das", "und", "oder", "mit", "ohne",
    "über", "ueber", "von", "im", "in", "am", "an", "auf", "für", "fuer",
    "ist", "sind", "war", "waren", "es", "etwas", "lied", "lieder", "songtext",
    "singt", "singen", "text", "zeile", "zeilen", "wort", "worte", "wörter",
}

MODEL_SIZES = ("auto", "tiny", "base", "small", "medium", "large-v3",
               "distil-large-v3")


# --------------------------------------------------------------------------
# paths and database


def library_rel(value: str | Path) -> str:
    """A library-relative path; the historical `Music/` prefix is stripped."""
    text = str(value).replace("\\", "/").lstrip("./")
    while text.startswith("Music/"):
        text = text[len("Music/"):]
    return text


def absolute(rel: str) -> Path:
    path = Path(rel)
    return path if path.is_absolute() else ROOT / library_rel(rel)


def db_path(value: str | None = None) -> Path:
    return Path(value) if value else ROOT / DEFAULT_DB


SCHEMA = """
CREATE TABLE IF NOT EXISTS track (
    rel_path TEXT PRIMARY KEY,
    title TEXT, artist TEXT, album TEXT,
    duration_s REAL,
    language TEXT, model TEXT, backend TEXT,
    source_size INTEGER, source_mtime TEXT,
    transcribed_at TEXT, line_count INTEGER DEFAULT 0, text TEXT,
    audio_fp TEXT, fp_algo TEXT
);
CREATE TABLE IF NOT EXISTS line (
    rel_path TEXT NOT NULL, idx INTEGER NOT NULL,
    start_s REAL, end_s REAL, text TEXT NOT NULL,
    PRIMARY KEY (rel_path, idx)
);
CREATE VIRTUAL TABLE IF NOT EXISTS line_fts USING fts5(
    text, rel_path UNINDEXED, idx UNINDEXED, start_s UNINDEXED, end_s UNINDEXED);
"""


def connect(path: Path, *, create: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not create and not path.is_file():
        raise SystemExit(f"no lyrics database at {path}")
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # added 2026-11-22: the audio payload hash, so a row can be matched to a
    # file after a rename (written by scripts/library_data.py)
    have = {row[1] for row in conn.execute("PRAGMA table_info(track)")}
    for column in ("audio_fp", "fp_algo"):
        if column not in have:
            conn.execute(f"ALTER TABLE track ADD COLUMN {column} TEXT")
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# the backend


def load_backend():
    """`faster_whisper`, or a whisper.cpp binary; None when neither is there."""
    try:
        from faster_whisper import WhisperModel        # noqa: F401
        return "faster-whisper"
    except Exception:                                # noqa: BLE001
        pass
    for name in ("whisper-cli", "whisper-cpp", "whisper", "main"):
        found = _which(name)
        if found:
            return "whisper.cpp"
    return ""


def _which(name: str) -> str:
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(folder) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def whisper_cpp_model(name: str) -> Path:
    """The GGML file of a whisper.cpp model, looked up next to the database."""
    pattern = name.replace("large-v3", "large-v3")
    for candidate in sorted(MODELS.glob("*.bin")):
        if pattern in candidate.name:
            return candidate
    return MODELS / f"ggml-{pattern}.bin"


def cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:                                # noqa: BLE001
        return False


def pick_model(name: str, backend: str) -> str:
    """`auto` means: the best model this machine transcribes quickly with."""
    name = (name or "auto").strip()
    if name and name != "auto":
        return name
    if backend == "faster-whisper" and cuda_available():
        return "medium"
    return "small"


def transcriber(model_name: str, device: str, compute: str, threads: int):
    backend = load_backend()
    if backend == "faster-whisper":
        from faster_whisper import WhisperModel
        model_name = pick_model(model_name, backend)
        MODELS.mkdir(parents=True, exist_ok=True)
        device = (os.environ.get("MUSIC_ASR_DEVICE") or device or "auto").lower()
        compute = (os.environ.get("MUSIC_ASR_COMPUTE") or compute or "auto").lower()
        if compute == "auto":
            compute = "float16" if (device == "cuda"
                                    or (device == "auto"
                                        and cuda_available())) else "int8"
        if device == "auto" and compute == "float16":
            device = "cuda"
        try:
            model = WhisperModel(model_name, device=device,
                                 compute_type=compute,
                                 download_root=str(MODELS),
                                 cpu_threads=max(1, threads))
        except Exception as exc:                     # noqa: BLE001
            if device != "cpu":
                print(f"[lyrics] {device} failed ({exc}); using CPU int8",
                      flush=True)
                model = WhisperModel(model_name, device="cpu",
                                     compute_type="int8",
                                     download_root=str(MODELS),
                                     cpu_threads=max(1, threads))
            else:
                raise
        return model, backend
    if backend == "whisper.cpp":
        model = whisper_cpp_model(model_name)
        if not model.is_file():
            raise SystemExit(
                f"whisper.cpp needs a model file: put ggml-{model_name}.bin "
                f"into {MODELS} (run the local backend setup first)")
        return str(_which("whisper-cli") or _which("whisper-cpp")
                   or _which("whisper") or _which("main")), backend
    raise SystemExit(
        "no local transcriber is installed.\n"
        "  run  the local backend setup     (installs faster-whisper into\n"
        "                                   library-work/app/lyrics/asr-venv)\n"
        "  or   pip install faster-whisper  into the interpreter that runs "
        "this file")


#: a Whisper segment is often several sung phrases; splitting it keeps the
#: highlighted line close to what is actually being sung
MAX_LINE_S = 8.0
MIN_LINE_WORDS = 3
LINE_GAP_S = 0.7
_END_PUNCT = (".", "!", "?", "…", ";", ":", "。", "！", "？")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")


def _join_words(words) -> str:
    text = " ".join(str(getattr(word, "word", "") or "").strip()
                    for word in words)
    text = re.sub(r"\s+([,.!?;:…%)\]])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    return text.strip()


def _phrase_lines(segment, text: str) -> list[dict]:
    """One segment as several sung phrases, each with its own start and end.

    Whisper hands back a whole sentence or two per segment, which would leave
    the marker sitting on one line for ten seconds.  Its word timestamps let a
    line break where a phrase really ends: at a sentence mark, at a sung pause
    of `LINE_GAP_S`, or when a line would run longer than `MAX_LINE_S`.
    Japanese and Chinese are kept whole, because joining their words with
    spaces would put a gap between every character.
    """
    words = [word for word in (getattr(segment, "words", None) or [])
             if str(getattr(word, "word", "") or "").strip()]
    if not words or _CJK.search(text):
        return [{"start": round(float(segment.start), 2),
                 "end": round(float(segment.end), 2), "text": text}]
    lines: list[dict] = []
    chunk: list = []
    for index, word in enumerate(words):
        token = str(getattr(word, "word", "") or "").strip()
        if getattr(word, "start", None) is None or getattr(word, "end", None) is None:
            continue                      # an untimed word cannot be placed
        chunk.append(word)
        following = words[index + 1] if index + 1 < len(words) else None
        gap = 0.0
        if following is not None and None not in (getattr(following, "start", None),
                                                  getattr(following, "end", None)):
            gap = float(following.start) - float(word.end)
        span = float(word.end) - float(chunk[0].start)
        long_enough = len(chunk) >= MIN_LINE_WORDS
        last = following is None
        if last or (long_enough and (
                token.endswith(_END_PUNCT) or gap >= LINE_GAP_S
                or span >= MAX_LINE_S)):
            line = _join_words(chunk)
            if line:
                lines.append({"start": round(float(chunk[0].start), 2),
                              "end": round(float(word.end), 2),
                              "text": line})
            chunk = []
    if chunk:
        line = _join_words(chunk)
        if line:
            lines.append({"start": round(float(chunk[0].start), 2),
                          "end": round(float(getattr(chunk[-1], "end",
                                                     segment.end)), 2),
                          "text": line})
    if not lines and text:
        # no word could be timed: the segment text is still the best we have
        lines.append({"start": round(float(segment.start), 2),
                      "end": round(float(segment.end), 2), "text": text})
    return lines


def transcribe_file(model, backend: str, path: Path, *, language: str,
                    model_name: str, duration: float = 0.0, vad: bool = False
                    ) -> tuple[list[dict], str]:
    """Segments with timestamps as `{"start", "end", "text"}` rows."""
    if backend == "whisper.cpp":
        return _transcribe_cpp(model, path, language, duration)
    language = "" if str(language).casefold() in {"", "auto", "none"} else language
    segments, info = model.transcribe(
        str(path), language=language or None, vad_filter=bool(vad),
        beam_size=5, condition_on_previous_text=False,
        repetition_penalty=1.1, word_timestamps=True)
    detected = getattr(info, "language", "") or (language or "")
    lines: list[dict] = []
    total = float(duration or getattr(info, "duration", 0) or 0)
    for segment in segments:
        text = (segment.text or "").strip()
        # a segment is split into sung phrases; a repeated identical line is
        # what Whisper does over a long instrumental, so exact back-to-back
        # copies are dropped
        for line in _phrase_lines(segment, text):
            if line["text"] and (not lines or lines[-1]["text"] != line["text"]):
                lines.append(line)
        if total > 0:
            done = min(100.0, 100.0 * float(segment.end) / total)
            print(f"[lyrics] {path.name} {done:5.1f}% "
                  f"({segment.end:.1f}s / {total:.0f}s)", flush=True)
    return lines, detected


def _transcribe_cpp(binary: str, path: Path, language: str,
                    duration: float) -> tuple[list[dict], str]:
    import subprocess
    import tempfile

    model = None
    for name in MODEL_SIZES:
        candidate = whisper_cpp_model(name)
        if candidate.is_file():
            model = candidate
            break
    if model is None:
        raise SystemExit(f"no whisper.cpp model in {MODELS}")
    with tempfile.TemporaryDirectory() as tmp:
        stem = Path(tmp) / "out"
        argv = [binary, "-m", str(model), "-f", str(path), "-oj", "-of",
                str(stem), "-ml", "1", "-pp"]
        if language and language != "auto":
            argv += ["-l", language]
        subprocess.run(argv, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        payload = json.loads(Path(str(stem) + ".json").read_text("utf-8"))
    lines = []
    for segment in payload.get("transcription") or []:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        offsets = segment.get("offsets") or {}
        start = float(offsets.get("from", 0)) / 1000.0
        end = float(offsets.get("to", 0)) / 1000.0
        lines.append({"start": round(start, 2), "end": round(end, 2),
                      "text": text})
    return lines, language if language and language != "auto" else ""


# --------------------------------------------------------------------------
# storing


def store(conn: sqlite3.Connection, rel: str, lines: list[dict], *,
          model: str, backend: str, language: str, path: Path,
          title: str = "", artist: str = "", album: str = "",
          duration: float = 0.0) -> int:
    stat = path.stat()
    text = " ".join(line["text"] for line in lines)
    conn.execute(
        "INSERT INTO track(rel_path, title, artist, album, duration_s, "
        "language, model, backend, source_size, source_mtime, transcribed_at, "
        "line_count, text) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(rel_path) DO UPDATE SET title=excluded.title, "
        "artist=excluded.artist, album=excluded.album, "
        "duration_s=excluded.duration_s, language=excluded.language, "
        "model=excluded.model, backend=excluded.backend, "
        "source_size=excluded.source_size, source_mtime=excluded.source_mtime, "
        "transcribed_at=excluded.transcribed_at, "
        "line_count=excluded.line_count, text=excluded.text",
        (rel, title, artist, album, round(float(duration or 0), 1), language,
         model, backend, stat.st_size, f"{stat.st_mtime:.0f}", now(),
         len(lines), text))
    conn.execute("DELETE FROM line WHERE rel_path = ?", (rel,))
    conn.execute("DELETE FROM line_fts WHERE rel_path = ?", (rel,))
    for index, line in enumerate(lines):
        conn.execute(
            "INSERT INTO line(rel_path, idx, start_s, end_s, text) "
            "VALUES(?, ?, ?, ?, ?)",
            (rel, index, line["start"], line["end"], line["text"]))
        conn.execute(
            "INSERT INTO line_fts(text, rel_path, idx, start_s, end_s) "
            "VALUES(?, ?, ?, ?, ?)",
            (line["text"], rel, index, line["start"], line["end"]))
    conn.commit()
    return len(lines)


def stored(conn: sqlite3.Connection, rel: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM track WHERE rel_path = ?",
                        (library_rel(rel),)).fetchone()


def is_current(conn: sqlite3.Connection, rel: str, path: Path) -> bool:
    row = stored(conn, rel)
    if row is None or not path.is_file():
        return False
    stat = path.stat()
    return (row["source_size"] == stat.st_size
            and str(row["source_mtime"]) == f"{stat.st_mtime:.0f}")


# --------------------------------------------------------------------------
# the library


def inventory() -> list[dict]:
    """Audio files from the inventory, with their tags (best effort)."""
    if not INVENTORY.is_file():
        return []
    rows = []
    for raw in INVENTORY.read_bytes().split(b"\n"):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        rel = library_rel(row.get("rel_path") or "")
        if not rel or Path(rel).suffix.lower() not in AUDIO_SUFFIXES:
            continue
        if not absolute(rel).is_file():
            continue
        tags = row.get("tags") or {}
        rows.append({
            "rel": rel,
            "title": _first(tags, "title") or row.get("title") or "",
            "artist": _first(tags, "artist") or row.get("artist") or "",
            "album": _first(tags, "album") or row.get("album") or "",
            "duration": float(row.get("duration_s") or 0.0),
        })
    return rows


def _first(tags: dict, key: str) -> str:
    value = tags.get(key)
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def record_for(rel: str) -> dict:
    rel = library_rel(rel)
    for row in inventory():
        if row["rel"] == rel:
            return row
    stem = Path(rel).stem.split("_", 1)[-1].replace("_", " ")
    return {"rel": rel, "title": stem, "artist": "", "album": "",
            "duration": 0.0}


# --------------------------------------------------------------------------
# search


def query_tokens(text: str) -> list[str]:
    tokens = []
    for token in re.split(r"[^0-9A-Za-zÄÖÜäöüß']+", text.casefold()):
        token = token.strip("'")
        if len(token) < 2 or token in STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def fts_query(tokens: list[str], *, mode: str = "and") -> str:
    joiner = " AND " if mode == "and" else " OR "
    return joiner.join(f'"{token}"*' for token in tokens)


def search(conn: sqlite3.Connection, text: str, *, limit: int = 40,
           mode: str = "and") -> list[dict]:
    """Rough lyric search: every word first, then any word, then substrings."""
    tokens = query_tokens(text)
    if not tokens:
        return []
    rows: list[dict] = []
    for attempt in (mode, "or"):
        try:
            rows = conn.execute(
                "SELECT f.rel_path, f.idx, f.start_s, f.end_s, f.text, "
                "bm25(line_fts) AS score, t.title, t.artist, t.album "
                "FROM line_fts f LEFT JOIN track t ON t.rel_path = f.rel_path "
                "WHERE line_fts MATCH ? ORDER BY score LIMIT ?",
                (fts_query(tokens, mode=attempt), int(limit))).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if rows:
            break
    if not rows:
        # a word the index never saw (a typo, an inflection) still matches a
        # substring of a stored line
        like = f"%{tokens[0]}%"
        rows = conn.execute(
            "SELECT l.rel_path, l.idx, l.start_s, l.end_s, l.text, 0 AS score, "
            "t.title, t.artist, t.album FROM line l "
            "LEFT JOIN track t ON t.rel_path = l.rel_path "
            "WHERE l.text LIKE ? ORDER BY l.rel_path, l.idx LIMIT ?",
            (like, int(limit))).fetchall()
    return [_hit(row) for row in rows]


def _hit(row: sqlite3.Row) -> dict:
    return {"rel": row["rel_path"], "line": int(row["idx"]),
            "start": float(row["start_s"] or 0.0),
            "end": float(row["end_s"] or 0.0),
            "text": row["text"], "score": round(float(row["score"] or 0), 4),
            "title": row["title"] or "", "artist": row["artist"] or "",
            "album": row["album"] or ""}


def lines_of(conn: sqlite3.Connection, rel: str) -> list[dict]:
    rows = conn.execute(
        "SELECT idx, start_s, end_s, text FROM line WHERE rel_path = ? "
        "ORDER BY idx", (library_rel(rel),)).fetchall()
    return [{"index": int(row["idx"]), "start": float(row["start_s"] or 0),
             "end": float(row["end_s"] or 0), "text": row["text"]}
            for row in rows]


# --------------------------------------------------------------------------
# commands


def check(path: Path) -> int:
    backend = load_backend()
    print(f"backend  : {backend or 'none'}")
    print(f"database : {path} ({'exists' if path.is_file() else 'missing'})")
    print(f"models   : {MODELS}")
    if MODELS.is_dir():
        for file in sorted(MODELS.glob("*")):
            if file.is_file() and file.suffix in {".bin", ".ctranslate2"}:
                print(f"           {file.name}")
    if backend == "faster-whisper":
        try:
            import faster_whisper
            print(f"version  : faster-whisper {faster_whisper.__version__}")
        except Exception:                            # noqa: BLE001
            pass
    if path.is_file():
        conn = connect(path)
        info = stats(conn)
        conn.close()
        print(f"tracks   : {info['tracks']:,} · lines {info['lines']:,} · "
              f"{info['hours']:.1f} h of audio")
    else:
        print("tracks   : 0")
    if backend == "faster-whisper":
        try:
            import ctranslate2
            print(f"devices  : {ctranslate2.get_supported_compute_types('cpu')}")
        except Exception:                            # noqa: BLE001
            pass
    if not backend:
        print("\ninstall one:  run the local backend setup (setup_asr.sh)")
        return 3
    return 0


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS tracks, SUM(line_count) AS lines, "
        "SUM(duration_s) AS seconds FROM track").fetchone()
    seconds = float(row["seconds"] or 0)
    return {"tracks": int(row["tracks"] or 0),
            "lines": int(row["lines"] or 0),
            "hours": seconds / 3600.0,
            "models": [dict(r) for r in conn.execute(
                "SELECT model, COUNT(*) AS n FROM track GROUP BY model")]}


def run(rels: list[str], *, db: str | None = None, model: str = "auto",
        device: str = "auto", compute: str = "auto", threads: int = 8,
        language: str = "auto", vad: bool = False, refresh: bool = False
        ) -> int:
    """Transcribe one or more files (the shared path for CLI and app)."""
    conn = connect(db_path(db))
    backend = load_backend()
    model_name = pick_model(model, backend)
    model_obj, backend = transcriber(model_name, device, compute, threads)
    print(f"[lyrics] backend {backend} · model {model_name}", flush=True)
    for rel in rels:
        path = absolute(rel)
        if not path.is_file():
            print(f"[lyrics] {rel}: no such file", flush=True)
            continue
        info = record_for(rel)
        if not refresh and is_current(conn, info["rel"], path):
            print(f"[lyrics] {info['rel']}: already transcribed", flush=True)
            continue
        started = time.time()
        print(f"[lyrics] {info['title'] or path.name}", flush=True)
        lines, detected = transcribe_file(
            model_obj, backend, path, language=language,
            model_name=model_name, duration=info.get("duration") or 0.0,
            vad=vad)
        written = store(conn, info["rel"], lines, model=model_name,
                        backend=backend, language=detected, path=path,
                        title=info["title"], artist=info["artist"],
                        album=info["album"], duration=info.get("duration") or 0.0)
        print(f"[lyrics] done: {written} lines in {time.time() - started:.1f}s "
              f"({detected or '?'})", flush=True)
    conn.close()
    return 0


def transcribe_once(path: Path, *, model: str = "auto", device: str = "auto",
                    compute: str = "auto", threads: int = 8,
                    language: str = "auto", vad: bool = False) -> int:
    """Transcribe one loose file (the app's voice input) and print JSON.

    Nothing is stored: no database row, no library path, no tag.  The caller
    writes the file, reads the single JSON line and deletes the file again.
    Diagnostics go to stderr so stdout stays one parsable line.
    """
    path = Path(path)
    if not path.is_file():
        print(json.dumps({"error": f"no such file: {path}"}))
        return 2
    backend = load_backend()
    if not backend:
        print(json.dumps({"error": "no local transcriber is installed"}))
        return 3
    started = time.time()
    with contextlib.redirect_stdout(sys.stderr):
        model_name = pick_model(model, backend)
        model_obj, backend = transcriber(model_name, device, compute, threads)
        lines, detected = transcribe_file(
            model_obj, backend, path, language=language,
            model_name=model_name, duration=0.0, vad=vad)
    text = " ".join(line["text"].strip() for line in lines
                    if str(line.get("text") or "").strip())
    print(json.dumps({
        "text": text,
        "language": detected or "",
        "segments": lines,
        "model": model_name,
        "backend": backend,
        "seconds": round(time.time() - started, 2),
    }, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tracks", nargs="*", help="library-relative audio paths")
    ap.add_argument("--db", default=str(ROOT / DEFAULT_DB))
    ap.add_argument("--rel", action="append", default=[],
                    help="one track (repeatable)")
    ap.add_argument("--missing", action="store_true",
                    help="transcribe inventory tracks that have no lyrics yet")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--refresh", action="store_true",
                    help="re-transcribe even when the file did not change")
    ap.add_argument("--model", default=os.environ.get("MUSIC_ASR_MODEL", "auto"),
                    choices=MODEL_SIZES,
                    help="auto = medium on a CUDA GPU, small on CPU")
    ap.add_argument("--language", default=os.environ.get("MUSIC_ASR_LANG", "auto"))
    ap.add_argument("--device", default=os.environ.get("MUSIC_ASR_DEVICE", "auto"))
    ap.add_argument("--compute", default=os.environ.get("MUSIC_ASR_COMPUTE", "auto"))
    ap.add_argument("--threads", type=int, default=os.environ.get("MUSIC_ASR_THREADS")
                    and int(os.environ["MUSIC_ASR_THREADS"]) or 8)
    ap.add_argument("--vad", action="store_true",
                    help="pre-filter speech regions (the bundled Silero VAD "
                         "misses a lot of singing; off by default)")
    ap.add_argument("--print", dest="show", default="",
                    help="print the stored lines of one track")
    ap.add_argument("--search", default="", help="rough search over the lyrics")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--text", default="",
                    help="transcribe one loose audio file and print one JSON "
                         "line (the app's voice input; nothing is stored)")
    args = ap.parse_args()
    path = Path(args.db)

    if args.text:
        return transcribe_once(
            Path(args.text), model=args.model, device=args.device,
            compute=args.compute, threads=args.threads,
            language=args.language, vad=args.vad)
    if args.check:
        return check(path)
    if args.stats:
        if not path.is_file():
            print("no lyrics database yet")
            return 0
        conn = connect(path)
        info = stats(conn)
        conn.close()
        print(f"tracks : {info['tracks']:,}  lines: {info['lines']:,}  "
              f"audio: {info['hours']:.1f} h")
        for row in info["models"]:
            print(f"  {row['model'] or '?'}: {row['n']:,}")
        return 0
    if args.show:
        conn = connect(path, create=False)
        row = stored(conn, args.show)
        if row is None:
            print(f"no lyrics for {args.show}")
            conn.close()
            return 1
        print(f"# {row['title'] or row['rel_path']} · {row['artist'] or '?'}"
              f" · {row['model']} · {row['language']}")
        for line in lines_of(conn, args.show):
            print(f"{line['start']:7.2f}  {line['text']}")
        conn.close()
        return 0
    if args.search:
        conn = connect(path, create=False)
        hits = search(conn, args.search, limit=args.limit or 40)
        conn.close()
        if not hits:
            print("no lyric matches")
            return 1
        for hit in hits:
            print(f"{hit['start']:7.2f}  {hit['artist']} — {hit['title']}")
            print(f"           {hit['text']}")
        return 0

    wanted = [library_rel(rel) for rel in (args.rel or args.tracks)]
    if args.missing:
        conn = connect(path)
        rows = inventory()
        pending = [row for row in rows
                   if args.refresh or not is_current(
                       conn, row["rel"], absolute(row["rel"]))]
        conn.close()
        if args.limit:
            pending = pending[:args.limit]
        wanted += [row["rel"] for row in pending]
        print(f"[lyrics] {len(pending):,} of {len(rows):,} tracks need lyrics",
              flush=True)
    if not wanted:
        ap.error("nothing to do: give a track, --missing, --search or --check")
    wanted = list(dict.fromkeys(wanted))
    return run(wanted, db=args.db, model=args.model, device=args.device,
               compute=args.compute, threads=args.threads,
               language=args.language, vad=args.vad, refresh=args.refresh)


if __name__ == "__main__":
    raise SystemExit(main())
