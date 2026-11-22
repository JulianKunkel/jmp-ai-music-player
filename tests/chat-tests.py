#!/usr/bin/env python3
"""Headless tests for the chat's new behaviours.

Covers recommendation -> save, playlist listing, activity, learned phrases,
the skill-agent fallback, settings and the waveform cache.  Everything runs
against a throwaway library under /tmp/opencode: no real music, no player and
no language model are touched.

    application/tests/chat-tests.py
    application/tests/chat-tests.py -v
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERBOSE = "-v" in sys.argv
HERE = Path(__file__).resolve().parent
LIB = HERE.parent / "lib"
sys.path.insert(0, str(LIB))

from chat.engine import Engine                      # noqa: E402
from chat.catalog import Catalog                    # noqa: E402
from chat.nlu import plan as nlu_plan               # noqa: E402
from chat.tools import Toolbox                      # noqa: E402
from chat.llm import _opencode_config               # noqa: E402
from chat.viz import spectrum, waveform             # noqa: E402
from chat import lrc                                # noqa: E402
from chat import lyrics as lyrics_mod                # noqa: E402
from chat import lyrics_job                        # noqa: E402
from chat import psycho_job                        # noqa: E402
from chat import index_job                         # noqa: E402
from chat import settings as setting_registry      # noqa: E402
from chat import smart                             # noqa: E402
from chat import stats as library_stats            # noqa: E402
from chat import radio as radio_mod                 # noqa: E402
from chat import endless as endless_mod             # noqa: E402
from chat.web import ChatService, make_handler, md_to_html  # noqa: E402
import mpvctl as mpv_client                        # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        if VERBOSE:
            print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def build_library(root: Path) -> None:
    """Twenty metal tracks, twenty calm ones, one real WAV for the waveform."""
    rows = []
    shelves = (("Metal", "Eisenwerk", "Workout Dark Epic"),
              ("Chill", "Federleicht", "Chill Focus Romantic"))
    for genre, artist, _ in shelves:
        for index in range(1, 21):
            rel = f"Artists/{artist}/Album/{index:02d}_Track_{index}.mp3"
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64)
            rows.append({
                "rel_path": rel,
                "title": f"Track {index}",
                "artist": artist,
                "albumartist": artist,
                "album": "Album",
                "genre": genre,
                "grouping": "Bands",
                "duration_s": 180,
                "ext": ".mp3",
                "track": str(index),
            })
    tone = root / "Artists/Tone/test.wav"
    tone.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(tone), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"".join(
            (int(8000 * ((i % 80) - 40) / 40)).to_bytes(2, "little", signed=True)
            for i in range(8000)))
    rows.append({
        "rel_path": "Artists/Tone/test.wav",
        "title": "Test Tone",
        "artist": "Tone",
        "album": "Signals",
        "genre": "Electronic",
        "duration_s": 1,
        "ext": ".wav",
    })

    inventory = root / "library-work" / "inventory" / "files.jsonl"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    moods = root / "Playlists" / "Moods"
    moods.mkdir(parents=True)
    (moods / "Mood_Chill.m3u8").write_text(
        "#EXTM3U\n../../Artists/Federleicht/Album/01_Track_1.mp3\n",
        encoding="utf-8")

    build_psycho(root, rows)
    build_lyrics(root)


#: the two synthetic profile shapes the tests exercise
PSYCHO_SHAPES = {
    "Metal": {"speed": 0.88, "energy": 0.92, "aggression": 0.94,
              "brightness": 0.62, "danceability": 0.48, "valence": 0.28,
              "calmness": 0.10, "density": 0.84, "roughness": 0.86,
              "harmony": 0.18, "punch": 0.86, "width": 0.72},
    "Chill": {"speed": 0.18, "energy": 0.16, "aggression": 0.10,
              "brightness": 0.34, "danceability": 0.26, "valence": 0.66,
              "calmness": 0.88, "density": 0.20, "roughness": 0.16,
              "harmony": 0.84, "punch": 0.14, "width": 0.38},
}

PSYCHO_COLUMNS = ("tempo_bpm", "energy_db", "dynamic_db", "lufs", "lra_db",
                  "crest_db", "centroid_hz", "rolloff_hz", "spread_hz",
                  "skewness", "kurtosis", "flatness", "zcr",
                  "percussiveness", "flux", "onset_rate", "pulse_clarity",
                  "beat_stability", "ioi_entropy", "roughness", "harmonicity",
                  "sharpness_acum", "low_ratio", "high_ratio", "entropy",
                  "mode_score", "key_strength", "repetition", "stereo_width",
                  "sub_ratio", "mid_ratio", "presence_ratio",
                  "attack_sharpness", "silence_ratio", "energy_slope")

#: the extra lens sets the profile script derives from the same measurements
SET_AXES = {
    "timbre": ("warmth", "brightness", "presence", "air", "sharpness",
               "harmonicity", "noisiness", "roughness"),
    "rhythm": ("speed", "beat_clarity", "regularity", "onset_density",
               "complexity", "drive"),
    "dynamics": ("loudness", "loudness_range", "micro_dynamics", "punch",
                 "silence", "trend"),
    "instrument": ("bass", "drums", "strings", "keys", "voice", "classical"),
}

AXIS_ORDER = ("speed", "energy", "aggression", "brightness", "danceability",
              "valence", "calmness", "density", "roughness", "harmony",
              "punch", "width")


def build_psycho(root: Path, rows: list[dict]) -> None:
    """A tiny profile database: the metal shelf and the calm shelf differ."""
    import sqlite3

    folder = root / "library-work" / "app" / "psycho"
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / "features.db")
    conn.executescript("""
        CREATE TABLE audio (rel_path TEXT PRIMARY KEY, size INTEGER,
            mtime TEXT, duration_s REAL, source TEXT, analysed_at TEXT,
            estimated INTEGER DEFAULT 0, %s);
        CREATE TABLE profile (rel_path TEXT PRIMARY KEY, %s, family TEXT,
            family_distance REAL, estimated INTEGER DEFAULT 0,
            updated_at TEXT);
        CREATE TABLE family (name TEXT PRIMARY KEY, size INTEGER,
            traits TEXT, playlist TEXT);
        CREATE TABLE profile_set (rel_path TEXT NOT NULL, set_name TEXT NOT NULL,
            axes TEXT NOT NULL, estimated INTEGER DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (rel_path, set_name));
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """ % (", ".join(f"{c} REAL" for c in PSYCHO_COLUMNS),
           ", ".join(f"{a} REAL" for a in AXIS_ORDER)))

    families: dict[str, list[str]] = {}
    for row in rows:
        rel = row["rel_path"]
        shelf = "Metal" if "Eisenwerk" in rel else "Chill"
        shape = dict(PSYCHO_SHAPES[shelf])
        # a spread of speeds per shelf, so moving one slider reorders it
        try:
            index = int(row.get("track") or 1)
        except (TypeError, ValueError):
            index = 1
        if shelf == "Metal":
            shape["speed"] = min(0.99, 0.80 + 0.01 * (index - 1))
        else:
            shape["speed"] = min(0.60, 0.10 + 0.025 * (index - 1))
        # one measured row, plausible raw columns, one per track
        audio = {
            "tempo_bpm": 60 + 140 * shape["speed"],
            "energy_db": -34 + 24 * shape["energy"],
            "dynamic_db": 10 - 6 * shape["energy"],
            "centroid_hz": 800 + 2600 * shape["brightness"],
            "rolloff_hz": 1400 + 3000 * shape["brightness"],
            "flatness": 0.05 + 0.5 * shape["roughness"],
            "zcr": 0.02 + 0.12 * shape["roughness"],
            "percussiveness": 0.2 + 1.4 * shape["energy"],
            "flux": 0.1 + 0.2 * shape["density"],
            "onset_rate": 1.0 + 6.0 * shape["density"],
            "pulse_clarity": 0.2 + 0.6 * shape["danceability"],
            "roughness": 0.002 + 0.02 * shape["roughness"],
            "low_ratio": 0.45 - 0.3 * shape["brightness"],
            "high_ratio": 0.01 + 0.1 * shape["brightness"],
            "entropy": 0.3 + 0.4 * shape["density"],
            "mode_score": -0.5 + 1.0 * shape["valence"],
            "key_strength": 0.4 + 0.5 * shape["harmony"],
            "lufs": -24 + 18 * shape["energy"],
            "lra_db": 14 - 9 * shape["energy"],
            "crest_db": 6 + 12 * shape["punch"],
            "spread_hz": 900 + 1800 * shape["brightness"],
            "skewness": -0.4 + 0.8 * shape["brightness"],
            "kurtosis": 1.5 + 3.0 * shape["roughness"],
            "beat_stability": 0.3 + 0.6 * shape["danceability"],
            "ioi_entropy": 0.8 - 0.5 * shape["danceability"],
            "harmonicity": 0.2 + 0.6 * shape["harmony"],
            "sharpness_acum": 0.8 + 1.6 * shape["brightness"],
            "repetition": 0.86 + 0.12 * shape["density"],
            "stereo_width": 0.1 + 0.6 * shape["width"],
            # v3: band shares and attack, on top of the same shapes
            "sub_ratio": 0.06 + 0.16 * shape["energy"],
            "mid_ratio": 0.5 - 0.2 * shape["brightness"],
            "presence_ratio": 0.05 + 0.2 * shape["brightness"],
            "attack_sharpness": 0.4 + 2.6 * shape["punch"],
            "silence_ratio": 0.02 + 0.2 * (1.0 - shape["density"]),
            "energy_slope": -1.0 + 2.0 * shape["energy"],
        }
        conn.execute(
            f"INSERT INTO audio(rel_path, size, mtime, duration_s, source, "
            f"analysed_at, estimated, {', '.join(PSYCHO_COLUMNS)}) "
            f"VALUES(?, 1, '2026-11-21T00:00:00+00:00', 180, 'analysis', "
            f"'2026-11-21T00:00:00+00:00', 0, "
            f"{', '.join('?' for _ in PSYCHO_COLUMNS)})",
            (rel, *[round(audio[c], 4) for c in PSYCHO_COLUMNS]))
        conn.execute(
            f"INSERT INTO profile(rel_path, {', '.join(AXIS_ORDER)}, family, "
            f"family_distance, estimated, updated_at) VALUES("
            f"{', '.join('?' for _ in range(len(AXIS_ORDER) + 5))})",
            (rel, *[shape[a] for a in AXIS_ORDER],
             "Aggressive · Bright" if shelf == "Metal" else "Quiet · Calm",
             0.12, 0, "2026-11-21T00:00:00+00:00"))
        # the four extra lenses over the very same measurements
        set_values = {
            "timbre": {
                "warmth": 1.0 - shape["brightness"],
                "brightness": shape["brightness"],
                "presence": shape["brightness"],
                "air": shape["brightness"],
                "sharpness": shape["brightness"],
                "harmonicity": shape["harmony"],
                "noisiness": shape["roughness"],
                "roughness": shape["roughness"],
            },
            "rhythm": {
                "speed": shape["speed"],
                "beat_clarity": shape["danceability"],
                "regularity": shape["danceability"],
                "onset_density": shape["density"],
                "complexity": 1.0 - shape["danceability"],
                "drive": shape["energy"],
            },
            "dynamics": {
                "loudness": shape["energy"],
                "loudness_range": 1.0 - shape["energy"],
                "micro_dynamics": 1.0 - shape["energy"],
                "punch": shape["punch"],
                "silence": 1.0 - shape["density"],
                "trend": shape["energy"],
            },
            "instrument": {
                "bass": shape["energy"],
                "drums": shape["energy"],
                "strings": 1.0 - shape["energy"],
                "keys": 1.0 - shape["energy"],
                "voice": 1.0 - shape["roughness"],
                "classical": 1.0 - shape["energy"],
            },
        }
        for set_name, values in set_values.items():
            conn.execute(
                "INSERT INTO profile_set(rel_path, set_name, axes, estimated, "
                "updated_at) VALUES(?, ?, ?, 0, ?)",
                (rel, set_name,
                 json.dumps({key: round(float(values[key]), 4)
                             for key in SET_AXES[set_name]}),
                 "2026-11-21T00:00:00+00:00"))
        families.setdefault(
            "Aggressive · Bright" if shelf == "Metal" else "Quiet · Calm",
            []).append(rel)

    playlists = root / "Playlists" / "Psycho"
    playlists.mkdir(parents=True, exist_ok=True)
    for name, members in families.items():
        filename = name.replace(" · ", "_") + ".m3u8"
        (playlists / filename).write_text(
            "#EXTM3U\n" + "".join(f"../../{rel}\n" for rel in members),
            encoding="utf-8")
    conn.executemany(
        "INSERT INTO family(name, size, traits, playlist) VALUES(?, ?, ?, ?)",
        [(name, len(members),
          json.dumps(["aggression", "roughness"] if "Aggressive" in name
                     else ["energy", "calmness"]),
          f"Playlists/Psycho/{name.replace(' · ', '_')}.m3u8")
         for name, members in families.items()])
    conn.commit()
    conn.close()


def build_lyrics(root: Path) -> None:
    """A tiny lyrics database: one metal song about a boat, one calm one."""
    import sqlite3

    folder = root / "library-work" / "app" / "lyrics"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "lyrics.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE track (rel_path TEXT PRIMARY KEY, title TEXT, artist TEXT,
            album TEXT, duration_s REAL, language TEXT, model TEXT,
            backend TEXT, source_size INTEGER, source_mtime TEXT,
            transcribed_at TEXT, line_count INTEGER DEFAULT 0, text TEXT);
        CREATE TABLE line (rel_path TEXT NOT NULL, idx INTEGER NOT NULL,
            start_s REAL, end_s REAL, text TEXT NOT NULL,
            PRIMARY KEY (rel_path, idx));
        CREATE VIRTUAL TABLE line_fts USING fts5(
            text, rel_path UNINDEXED, idx UNINDEXED, start_s UNINDEXED,
            end_s UNINDEXED);
    """)
    tracks = {
        "Artists/Eisenwerk/Album/01_Track_1.mp3": [
            (0.0, 12.0, "We sail the boat across the burning sea"),
            (12.0, 24.0, "the engines roar and nothing stops us now"),
            (24.0, 36.0, "a boat of steel and fire"),
        ],
        "Artists/Federleicht/Album/01_Track_1.mp3": [
            (0.0, 10.0, "A quiet boat drifts slowly home"),
            (10.0, 20.0, "the water holds the evening light"),
        ],
    }
    for rel, lines in tracks.items():
        stat = (root / rel).stat()
        text = " ".join(line[2] for line in lines)
        conn.execute(
            "INSERT INTO track(rel_path, title, artist, album, duration_s, "
            "language, model, backend, source_size, source_mtime, "
            "transcribed_at, line_count, text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rel, "Track 1", "Eisenwerk" if "Eisenwerk" in rel else "Federleicht",
             "Album", 36.0 if "Eisenwerk" in rel else 20.0, "en", "small",
             "faster-whisper", stat.st_size, f"{stat.st_mtime:.0f}",
             "2026-11-21T00:00:00+00:00", len(lines), text))
        for index, (start, end, line) in enumerate(lines):
            conn.execute("INSERT INTO line(rel_path, idx, start_s, end_s, text) "
                         "VALUES(?,?,?,?,?)", (rel, index, start, end, line))
            conn.execute("INSERT INTO line_fts(text, rel_path, idx, start_s, "
                         "end_s) VALUES(?,?,?,?,?)",
                         (line, rel, index, start, end))
    conn.commit()
    conn.close()


def check_extractor(work: Path) -> None:
    """Measure two synthetic signals: the noisy, fast one must score higher.

    The extractor itself lives in the library's `scripts/` folder and is not
    part of the app bundle, so the test is skipped when it is not there.
    """
    import importlib.util

    try:
        import numpy as np
    except ImportError:                       # pragma: no cover
        return
    path = HERE.parent.parent / "scripts" / "psycho_features.py"
    spec = importlib.util.spec_from_file_location("psycho_features_test", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:                  # noqa: BLE001 - report, do not fail
        check("extractor imports", False, str(exc)[:200])
        return

    folder = work / "psycho-signals"
    folder.mkdir(parents=True, exist_ok=True)
    rate = 22050
    seconds = 20
    t = np.arange(int(rate * seconds)) / rate

    # fast + loud + noisy: 100 BPM noise clicks over a noise bed
    rng = np.random.default_rng(7)
    fast = 0.12 * rng.standard_normal(len(t))
    period = int(rate * 60 / 100)
    burst = int(rate * 0.03)
    envelope = np.exp(-np.linspace(0, 6, burst))
    for start in range(0, len(t) - burst, period):
        fast[start:start + burst] += (0.55 * envelope
                                      * rng.standard_normal(burst))

    # slow + quiet + tonal: a soft sine with a slow swell
    slow = 0.05 * np.sin(2 * np.pi * 220 * t) * (0.5 + 0.5 * np.sin(
        2 * np.pi * t / 2.0))

    def write(name: str, data) -> str:
        import wave as wavemod
        target = folder / name
        with wavemod.open(str(target), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes((np.clip(data, -1, 1) * 32767)
                               .astype("<i2").tobytes())
        return str(target)

    loud = module.analyse(write("fast_noisy.wav", fast), None)
    soft = module.analyse(write("slow_tonal.wav", slow), None)
    check("extractor measures without an error",
          "error" not in loud and "error" not in soft,
          str(loud)[:200] + str(soft)[:100])
    check("extractor finds the tempo of the click train",
          85.0 <= float(loud.get("tempo_bpm") or 0) <= 115.0,
          str(loud.get("tempo_bpm")))
    check("extractor finds the louder signal",
          float(loud.get("energy_db") or -999) > float(soft.get("energy_db") or 0),
          f"{loud.get('energy_db')} vs {soft.get('energy_db')}")
    check("extractor finds the noisier signal",
          float(loud.get("flatness") or 0) > float(soft.get("flatness") or 0),
          f"{loud.get('flatness')} vs {soft.get('flatness')}")
    check("extractor finds the rougher signal",
          float(loud.get("roughness") or 0) > float(soft.get("roughness") or 0),
          f"{loud.get('roughness')} vs {soft.get('roughness')}")

    # the database round-trip the app reads
    conn = module.connect(folder / "test.db")
    module._upsert(conn, {"rel_path": "Artists/X/Y/01.mp3", "size": 10,
                          "mtime": "2026-11-21T00:00:00+00:00",
                          "duration_s": 100, "source": "analysis",
                          "analysed_at": "2026-11-21T00:00:00+00:00",
                          "estimated": 0, **{k: v for k, v in loud.items()
                                             if k in module.MEASURED}})
    conn.commit()
    info = module.stats(conn)
    conn.close()
    check("extractor writes a database row", info["audio"] == 1, str(info))
    check("extractor stores every measured column",
          info["full"] == 1 and info["measured"] == 1, str(info))


def check_phrase_lines() -> None:
    """A Whisper segment must become several sung phrases, not one long line.

    The transcription script lives in the library (`../scripts`), outside this
    repository, so the check is skipped when it is not there.
    """
    path = HERE.parent.parent / "scripts" / "transcribe_lyrics.py"
    if not path.is_file():
        return
    import importlib.util
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location("transcribe_lyrics", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def word(text, start, end):
        return SimpleNamespace(word=text, start=start, end=end)

    def segment(words, start=0.0, end=0.0, text=""):
        return SimpleNamespace(words=words, start=start, end=end, text=text)

    # punctuation ends a phrase once it is long enough to be a line
    lines = module._phrase_lines(segment(
        [word(" Hello", 0.0, 0.3), word(" there", 0.3, 0.6),
         word(" world.", 0.6, 1.0), word(" How", 1.2, 1.4),
         word(" are", 1.4, 1.6), word(" you", 1.6, 1.9)], 0.0, 1.9),
        "Hello there world. How are you")
    check("a sentence split becomes two lines", len(lines) == 2, str(lines))
    check("each phrase keeps its own start",
          lines and lines[0]["start"] == 0.0 and lines[1]["start"] == 1.2,
          str(lines))
    check("the first phrase ends where it was sung",
          lines and lines[0]["end"] == 1.0, str(lines))

    # a sung pause splits a phrase without any punctuation
    lines = module._phrase_lines(segment(
        [word(" One", 0.0, 0.3), word(" two", 0.3, 0.6),
         word(" three", 0.6, 0.9), word(" four", 1.7, 2.0)], 0.0, 2.0),
        "One two three four")
    check("a long gap starts a new line", len(lines) == 2, str(lines))

    # eight seconds is the longest a line may run
    lines = module._phrase_lines(segment(
        [word(f" w{i}", i * 2.0, i * 2.0 + 2.0) for i in range(5)], 0.0, 10.0),
        "w0 w1 w2 w3 w4")
    check("a long segment is broken at the length cap", len(lines) >= 2,
          str(lines))

    # spaces before punctuation are removed again
    joined = module._join_words([word(" hello", 0, 1), word(" ,", 1, 2),
                                word(" world", 2, 3), word(" .", 3, 4)])
    check("punctuation is joined without a space",
          joined == "hello, world.", joined)

    # Japanese is kept whole: joining its words would space every character
    lines = module._phrase_lines(segment(
        [word(" こん", 0.0, 0.4), word("にちは", 0.4, 0.9)], 0.0, 0.9),
        "こんにちは")
    check("CJK stays on one line",
          len(lines) == 1 and lines[0]["text"] == "こんにちは", str(lines))

    # nothing timed to fall back on: the segment text is still a line
    lines = module._phrase_lines(segment([], 3.0, 7.5, "no timing here"),
                                 "no timing here")
    check("an untimed segment keeps its text and span",
          len(lines) == 1 and lines[0]["start"] == 3.0
          and lines[0]["end"] == 7.5
          and lines[0]["text"] == "no timing here", str(lines))


def check_answer_links() -> None:
    """A track listed in an answer must be a play target for the page.

    `_number_tracks()` prints the relative path under a hit, so that span gets
    `data-rel` and its list item becomes clickable; anything that is not a
    library file (absolute path, URL, an ordinary code span) must stay plain,
    otherwise a click would hand the player a target it cannot resolve.
    """
    html = md_to_html(
        "1. **Blind Guardian — Valhalla**  ·  4:55\n"
        "   `Artists/Blind_Guardian/Follow_the_Blind/08_Valhalla.mp3`\n"
        "2. **Something else**  ·  3:00\n"
        "   `/media/Musik/Old/16_Track.mp3`  ·  note\n"
        "3. **A web link**\n"
        "4. **A plain span** `just text`\n")
    check("a listed track line carries its path for the player",
          'class="playable" data-rel="Artists/Blind_Guardian/'
          'Follow_the_Blind/08_Valhalla.mp3"' in html, html[:400])
    check("the path span itself is a play target",
          'class="play-path"' in html, html[:400])
    check("an absolute path is not offered to the player",
          'data-rel="/media' not in html, html[:600])
    check("an ordinary code span stays plain", "<code>just text</code>" in html,
          html[-400:])
    check("a quote in a path cannot break the attribute",
          'data-rel="Own/a&quot;b.mp3"' in md_to_html('x\n   `Own/a"b.mp3`\n'),
          md_to_html('x\n   `Own/a"b.mp3`\n'))
    # source text stays escaped, so a title can never become markup
    check("a title is escaped before it reaches the page",
          "<script>" not in md_to_html("`Own/<script>.mp3`"),
          md_to_html("`Own/<script>.mp3`"))


def check_play_intent(engine) -> None:
    """A lookup must list the hits; only a play word starts the music.

    A language model sometimes answers "Eisenwerk" with `play-music`.  The
    engine follows the sentence instead: without a play word the hits are
    listed (and are clickable in the window), so nothing starts by itself.
    """
    from chat.nlu import Plan

    was_dry = engine.dry_run
    previous = list(engine.ctx.last_tracks or [])
    engine.dry_run = True
    try:
        listed = engine._execute(
            Plan("play-music", {"target": "Eisenwerk"}, source="llm"),
            "Eisenwerk")
        check("a lookup without 'play' does not start playback",
              listed.tool.startswith("search-library") and not listed.played,
              listed.tool)
        check("…and it lists the hits", len(listed.tracks) >= 2,
              str(len(listed.tracks)))
        played = engine._execute(
            Plan("play-music", {"target": "Eisenwerk"}, source="llm"),
            "play Eisenwerk")
        check("'play' still starts the music",
              played.tool.startswith("play-music"), played.tool)
        german = engine._execute(
            Plan("play-music", {"target": "Eisenwerk"}, source="llm"),
            "zeig mir eisenwerk")
        check("a German lookup lists instead of playing",
              german.tool.startswith("search-library"), german.tool)
        # a reference to a track already listed can only mean playback
        engine.ctx.last_tracks = listed.tracks
        again = engine._execute(
            Plan("play-music", {"target": listed.tracks[0].rel},
                 source="llm"),
            "das zweite")
        check("a reference to a listed track still plays",
              again.tool.startswith("play-music"), again.tool)
        # a mood or the whole library is a play request even without the word
        whole = engine._execute(
            Plan("play-music", {"target": "All"}, source="llm"), "anything")
        check("the whole library stays a play request",
              whole.tool.startswith("play-music"), whole.tool)
    finally:
        engine.dry_run = was_dry
        engine.ctx.last_tracks = previous


def check_session(engine, root: Path) -> None:
    """Continue where you left off: the queue, the track and the playhead.

    The window offers **Continue** and the chat understands "continue where I
    left off".  Only `library-work/app/chat/session.json` is written, so the
    checks look at that file, at the order mpv receives, and at the honest
    answer when there is nothing to continue.
    """
    from chat import session

    class FakePlayer:
        """Records what the engine asks of mpv."""

        def __init__(self, state: dict | None = None, entries=None):
            self.state = state or {}
            self.played: list[tuple] = []
            self.commands: list[tuple] = []
            self.entries = list(entries or [])
            self.last_error = ""

        def status(self):
            return dict(self.state)

        def play(self, target, append=False, shuffle=False):
            self.played.append((str(target), append, shuffle))
            return True, ""

        def command(self, action, value=None):
            self.commands.append((action, value))
            return True, ""

        def queue_from(self, limit=200):
            return list(self.entries)

        def status_line(self, root=None):
            return "fake"

        def running(self):
            return True

    real_player = engine.player
    was_dry = engine.dry_run
    try:
        engine.dry_run = False
        session.clear(root)

        # -- the store itself ----------------------------------------------
        check("a fresh library has no session", session.load(root) is None)
        check("nothing to continue yet answers honestly",
              engine.resume_last().error
              and "nothing to continue" in engine.resume_last().text.casefold())
        check("resume_info says it is not available",
              engine.resume_info() == {"available": False},
              str(engine.resume_info()))

        track = "Artists/Eisenwerk/Album/02_Track_2.mp3"
        session.save(root, {"kind": "playlist", "target": "Playlists/All.m3u8",
                            "rel": track, "position": 42.4, "duration": 180,
                            "index": 1, "count": 20, "artist": "Eisenwerk",
                            "title": "Track 2"})
        back = session.load(root) or {}
        check("a session round-trips", back.get("rel") == track
              and float(back.get("position") or 0) == 42.4, str(back)[:300])
        check("the session records its version",
              back.get("version") == session.VERSION, str(back)[:200])
        check("the session is readable as one line",
              session.describe(back) == "Eisenwerk — Track 2 · 0:42",
              session.describe(back))
        check("no temporary file is left behind",
              not list(session.path(root).parent.glob(".session.json*")),
              str(list(session.path(root).parent.glob(".session.json*"))))
        check("resume_info describes what Continue would start",
              engine.resume_info().get("available") is True
              and engine.resume_info().get("label") ==
              "Eisenwerk — Track 2 · 0:42",
              str(engine.resume_info()))

        # -- replaying a playlist session ----------------------------------
        # the daemon holds nothing, so the target must be handed to mpv again
        # (and as a real path, because mpv's own cwd is not the library)
        fake = FakePlayer({"idle": False, "file": "", "playlist_count": 20,
                           "playlist_pos": 0, "time": 0, "duration": 0})
        engine.player = fake
        reply = engine.resume_last()
        check("Continue plays the remembered queue again",
              fake.played and fake.played[0][0] == str(root / "Playlists/All.m3u8"),
              str(fake.played)[:300])
        check("Continue jumps to the remembered entry",
              ("position", 2) in fake.commands, str(fake.commands)[:300])
        check("Continue seeks to the remembered playhead",
              ("seek_abs", 42.4) in fake.commands, str(fake.commands)[:300])
        check("Continue answers with what it started",
              "Track 2" in reply.text and reply.played, reply.text[:200])

        # -- a temp queue is rebuilt when its file was pruned ---------------
        session.save(root, {"kind": "temp", "target":
                            "library-work/app/chat/queues/gone.m3u8",
                            "entries": [track,
                                        "Artists/Federleicht/Album/01_Track_1.mp3"],
                            "label": "mood mix", "rel": track, "position": 0,
                            "duration": 180, "index": 0, "count": 2,
                            "artist": "Eisenwerk", "title": "Track 2"})
        fake = FakePlayer({"idle": False, "file": "", "playlist_count": 2,
                           "playlist_pos": 0, "time": 0, "duration": 0})
        engine.player = fake
        engine.resume_last()
        rebuilt = fake.played[0][0] if fake.played else ""
        check("a temp queue is rebuilt as an ad-hoc list",
              rebuilt.endswith(".m3u8") and str(root) in rebuilt
              and "Playlists/Own" in rebuilt, rebuilt)
        entry_lines = [line for line in Path(rebuilt).read_text(
            encoding="utf-8").splitlines()
            if line and not line.startswith("#")]
        check("the rebuilt queue keeps both tracks",
              len(entry_lines) == 2
              and "../../" + track in entry_lines, str(entry_lines)[:300])

        # -- the queue panel -----------------------------------------------
        fake = FakePlayer({"idle": False, "file": str(root / track),
                           "playlist_count": 20, "playlist_pos": 1,
                           "time": 12, "duration": 180, "shuffle": False},
                          entries=[(2, str(root / track)),
                                   (3, str(root /
                                           "Artists/Federleicht/Album/"
                                           "01_Track_1.mp3"))])
        engine.player = fake
        view = engine.queue_view(50)
        check("the queue panel starts at the current entry",
              view["position"] == 2 and len(view["entries"]) == 2
              and view["entries"][0]["rel"] == track, str(view)[:300])
        check("a queue row carries artist, title and length",
              view["entries"][0]["artist"] == "Eisenwerk"
              and view["entries"][0]["title"] == "Track 2"
              and view["entries"][0]["duration"] == "3:00",
              str(view["entries"][0])[:300])
        check("the queue panel counts what is not shown",
              view["count"] == 20 and view["more"] == 18, str(view)[:200])
        engine.queue_remove(3)
        check("removing an entry is an mpv command",
              ("remove", 3) in fake.commands, str(fake.commands)[:300])
        engine.queue_jump(5)
        check("jumping is an mpv command",
              ("position", 5) in fake.commands, str(fake.commands)[:300])

        # -- the ad-hoc queue ("play something …") --------------------------
        import playlist_edit

        session.clear(root)
        engine.ctx.last_own = ""
        engine.ctx.last_own_at = ""
        check("a fresh engine has no ad-hoc queue",
              engine.queue_view(10).get("own") is None,
              str(engine.queue_view(10).get("own")))

        tracks = [engine.catalog.track(track),
                  engine.catalog.track(
                      "Artists/Federleicht/Album/01_Track_1.mp3")]
        path = engine._temp_queue(tracks, "ad-hoc: Rock", name="Rock Mix")
        rel = path.relative_to(root).as_posix()
        check("the ad-hoc queue is written under Playlists/Own",
              rel.startswith("Playlists/Own/") and path.is_file(), rel)
        check("its name comes from the request, not a stamp",
              path.stem.startswith("Rock_Mix_"), path.stem)
        body = path.read_text(encoding="utf-8").splitlines()
        check("the ad-hoc entries are relative and typed",
              body[0] == "#EXTM3U" and body[1] == "# ad-hoc: Rock"
              and all(not line.startswith("/") for line in body[2:]
                      if not line.startswith("#")),
              str(body)[:300])
        journal = (root / "library-work" / "journal" /
                   "playlists.jsonl")
        lines = [json.loads(line) for line in
                 journal.read_text(encoding="utf-8").splitlines() if line]
        check("the ad-hoc write is journalled before it happens",
              any(row.get("action") == "new"
                  and str(row.get("path") or "").endswith(".m3u8")
                  and "Own" in str(row.get("path") or "")
                  for row in lines), str(lines[-1])[:300])
        view = engine.queue_view(10)
        check("the queue panel reports the ad-hoc list",
              view.get("own") and view["own"]["name"].startswith("Rock_Mix")
              and view["own"]["count"] == 2, str(view.get("own"))[:300])

        # save it under a name: Custom/ gets the list, Own/ loses the copy
        reply = engine.skill_save_playlist({"own": True, "name": "Kept Mix"},
                                           "save it")
        saved = root / "Playlists" / "Custom" / "Kept_Mix.m3u8"
        check("“save it” writes a real custom playlist",
              not reply.error and saved.is_file(), reply.text[:200])
        check("the saved list keeps both tracks",
              sum(1 for line in saved.read_text(encoding="utf-8").splitlines()
                  if line and not line.startswith("#")) == 2,
              saved.read_text(encoding="utf-8")[:300])
        check("saving removes the ad-hoc copy", not path.exists(), rel)
        check("the queue panel drops the saved ad-hoc list",
              engine.queue_view(10).get("own") is None,
              str(engine.queue_view(10).get("own"))[:200])
        check("saving is answered with the new name",
              "Kept_Mix" in reply.text, reply.text[:200])

        # a fresh ad-hoc list, thrown away again
        path = engine._temp_queue(tracks, "genre: Chill", name="Chill")
        reply = engine.discard_own_playlist()
        check("discarding removes the ad-hoc list",
              not reply.error and not path.exists(), reply.text[:200])
        check("discarding keeps a saved list",
              (root / "Playlists" / "Custom" / "Kept_Mix.m3u8").is_file())
        check("discarding deletes nothing from the library",
              (root / tracks[0].rel).is_file()
              and (root / tracks[1].rel).is_file())
        check("there is nothing to discard afterwards",
              engine.discard_own_playlist().error)
        deleted = [json.loads(line) for line in
                   journal.read_text(encoding="utf-8").splitlines() if line
                   and json.loads(line).get("action") == "delete"]
        check("the ad-hoc delete is journalled too",
              any("Own" in str(row.get("path") or "") for row in deleted),
              str(deleted[-1] if deleted else {})[:200])

        # only Playlists/Own/ is managed by save/discard
        custom = root / "Playlists" / "Custom" / "Kept_Mix.m3u8"
        try:
            with playlist_edit.at_root(root):
                playlist_edit.save_own("Kept_Mix", "Nope")
            guard = ""
            custom_still = custom.is_file()
        except SystemExit as exc:
            guard = str(exc)
            custom_still = custom.is_file()
        check("save only accepts an ad-hoc list", bool(guard), guard)
        check("a refused save leaves the custom list alone",
              custom_still, str(custom))

        # the window's Save/Remove buttons go through /api/queue
        service = ChatService(root, use_llm=False, dry_run=False,
                              auto_psycho=False)
        try:
            svc_engine = service.engine
            svc_tracks = [svc_engine.catalog.track(track),
                          svc_engine.catalog.track(
                              "Artists/Federleicht/Album/01_Track_1.mp3")]
            svc_engine._temp_queue(svc_tracks, "ad-hoc: Rock",
                                   name="Window Mix")
            out = service.queue_action("save", None, "From Window", False)
            check("the window can save the ad-hoc queue",
                  out.get("ok") and (root / "Playlists" / "Custom" /
                                     "From_Window.m3u8").is_file(),
                  str(out)[:200])
            check("a window save returns the refreshed queue",
                  isinstance(out.get("queue"), dict)
                  and out["queue"].get("own") is None, str(out)[:200])
            svc_engine._temp_queue(svc_tracks, "genre: Chill", name="Chill")
            before = (root / "Playlists" / "Custom" / "From_Window.m3u8")
            out = service.queue_action("discard")
            check("the window can discard the ad-hoc queue",
                  out.get("ok") and before.is_file(), str(out)[:200])
            out = service.queue_action("discard")
            check("a second discard answers honestly",
                  not out.get("ok"), str(out)[:200])
        finally:
            try:
                service.engine.close()
            except Exception:
                pass

        # the planner routes the pronoun and the throw-away
        planned = nlu_plan("save it", engine.ctx, engine.catalog)
        check("“save it” saves the ad-hoc queue",
              planned is not None and planned.skill == "save-playlist"
              and planned.args.get("own") is True, str(planned)[:200])
        for sentence in ("discard it", "throw this away", "verwirf die liste",
                         "remove it"):
            planned = nlu_plan(sentence, engine.ctx, engine.catalog)
            check(f"“{sentence}” plans a discard",
                  planned is not None and planned.skill == "edit-playlist"
                  and planned.args.get("action") == "discard",
                  str(planned)[:200])
        planned = nlu_plan("delete the playlist Kept Mix", engine.ctx,
                           engine.catalog)
        check("a named playlist delete stays a delete",
              planned is not None and planned.skill == "edit-playlist"
              and planned.args.get("action") == "delete", str(planned)[:200])
        planned = nlu_plan("remove 2 from Kept Mix", engine.ctx,
                           engine.catalog)
        check("removing an entry is not a discard",
              planned is not None and planned.skill == "edit-playlist"
              and planned.args.get("action") == "remove", str(planned)[:200])
        engine.ctx.last_own = ""
        engine.ctx.last_own_at = ""
        # the forgetting check below needs a session to remove
        session.save(root, {"kind": "playlist", "target": "Playlists/All.m3u8",
                            "rel": track, "position": 42.4, "duration": 180,
                            "index": 1, "count": 20, "artist": "Eisenwerk",
                            "title": "Track 2"})

        # -- the planner and the window -------------------------------------
        for sentence in ("continue where I left off",
                         "where did I stop? continue",
                         "resume last", "weiterhören",
                         "wo ich aufgehört habe"):
            planned = nlu_plan(sentence, engine.ctx, engine.catalog)
            check(f"“{sentence}” plans a resume",
                  planned is not None and planned.skill == "play-music"
                  and planned.args.get("resume") is True,
                  str(planned)[:200])
        for sentence, action in (("resume", "resume"),
                                 ("continue", "resume")):
            planned = nlu_plan(sentence, engine.ctx, engine.catalog)
            check(f"a bare “{sentence}” stays the transport {action}",
                  planned is not None and planned.skill == "transport"
                  and planned.args.get("action") == action,
                  str(planned)[:200])

        # the ✕ forgets it, and nothing is remembered afterwards
        check("forgetting removes the file",
              engine.forget_session() is True
              and session.load(root) is None)
        check("Continue disappears after forgetting",
              engine.resume_info() == {"available": False})
    finally:
        engine.player = real_player
        engine.dry_run = was_dry
        session.clear(root)


def clean_text(reply) -> str:
    return getattr(reply, "text", str(reply))


def check_work(work: Path) -> None:
    """The footer chips: one row per background job, pause and continue.

    The three library tools (index, lyrics, profiles) are detached processes, so
    a pause is `SIGSTOP` on the job's process group and a continue is
    `SIGCONT`.  This builds a library whose measurement really runs, freezes it,
    and checks what the window is told.
    """
    from chat import procs

    lib = work / "worklib"
    (lib / "Playlists").mkdir(parents=True)
    (lib / "Artists").mkdir(parents=True)
    inventory = lib / "library-work" / "inventory" / "files.jsonl"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("".join(
        json.dumps({"rel_path": f"Artists/A/{index}.mp3", "ext": ".mp3",
                    "title": f"T{index}", "artist": "A",
                    "duration_s": 1}) + "\n"
        for index in range(4)), encoding="utf-8")
    scripts = lib / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "transcribe_lyrics.py").write_text("# fake\n", encoding="utf-8")
    (scripts / "phase0_inventory.py").write_text(
        "print('[scan] finished 0 files in 0.1s, errors=0', flush=True)\n",
        encoding="utf-8")
    (scripts / "make_playlists.py").write_text("print('ok', flush=True)\n",
                                               encoding="utf-8")
    # a measurement slow enough that a frozen counter is visible
    (scripts / "psycho_features.py").write_text(
        "import time\n"
        "print('to measure: 4  workers: 2', flush=True)\n"
        "for i in range(200):\n"
        "    time.sleep(0.05)\n"
        "    print(f'  {i + 1}/200  measured', flush=True)\n"
        "print('[psycho] measured 4 of 4', flush=True)\n",
        encoding="utf-8")
    (scripts / "psycho_profile.py").write_text(
        "print('[psycho] profile written', flush=True)\n", encoding="utf-8")

    import sqlite3
    (lib / "library-work" / "app" / "psycho").mkdir(parents=True)
    conn = sqlite3.connect(lib / "library-work" / "app" / "psycho" /
                           "features.db")
    conn.executescript("""
        CREATE TABLE audio (rel_path TEXT PRIMARY KEY, source TEXT,
            lufs REAL, sharpness_acum REAL);
        CREATE TABLE profile (rel_path TEXT PRIMARY KEY);
        CREATE TABLE family (name TEXT PRIMARY KEY);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    # four rows that are estimated, not measured: there is work to do
    for index in range(4):
        conn.execute("INSERT INTO audio (rel_path, source) VALUES (?, 'moods')",
                     (f"Artists/A/{index}.mp3",))
    conn.commit()
    conn.close()

    # the shared process helpers, on this very process
    check("a live pid reads as running",
          procs.process_state(os.getpid()) == "running"
          and procs.alive(os.getpid()) is True)
    check("a missing pid reads as dead",
          procs.process_state(999_999_998) == "dead"
          and procs.alive(0) is False)

    # one transcribed song, so the lyric row knows how much is left
    (lib / "library-work" / "app" / "lyrics").mkdir(parents=True,
                                                    exist_ok=True)
    lyrics = sqlite3.connect(lib / "library-work" / "app" / "lyrics" /
                             "lyrics.db")
    lyrics.executescript("""
        CREATE TABLE track (rel_path TEXT PRIMARY KEY, line_count INTEGER,
            transcribed_at TEXT);
        CREATE TABLE line (rel_path TEXT, idx INTEGER, start_s REAL,
            end_s REAL, text TEXT);
    """)
    lyrics.execute("INSERT INTO track (rel_path, line_count, transcribed_at) "
                   "VALUES (?, 2, '2026-11-21T10:00:00+00:00')",
                   ("Artists/A/0.mp3",))
    lyrics.commit()
    lyrics.close()

    service = ChatService(root=lib, use_llm=False, auto_psycho=False)
    try:
        rows = service.work()["jobs"]
        by_key = {row["key"]: row for row in rows}
        check("a row per background job is listed",
            set(by_key) == {"index", "lyrics", "psycho"}, str(sorted(by_key)))
        check("an idle index offers its start button",
            by_key["index"]["active"] is False
            and by_key["index"]["can_start"] is True
            and "tracks" in by_key["index"]["value"],
            str(by_key["index"])[:300])
        check("a row carries what a chip draws",
            {"key", "label", "state", "active", "paused", "percent",
             "done", "total", "value", "can_start"} <= set(by_key["psycho"]),
            str(by_key["psycho"])[:300])
        check("the lyric row counts the songs",
            by_key["lyrics"]["total"] == 4
            and by_key["lyrics"]["done"] == 1
            and by_key["lyrics"]["can_start"] is True,
            str(by_key["lyrics"])[:300])
        check("the profile row reports the estimate as work",
            by_key["psycho"]["total"] == 4
            and by_key["psycho"]["done"] == 0
            and by_key["psycho"]["can_start"] is True,
            str(by_key["psycho"])[:300])

        started = service.control_work("psycho", "start")
        status = started.get("status") or {}
        check("start begins the measurement",
            status.get("state") in {"running", "paused"}
            and started.get("job") == "psycho", str(started)[:300])
        active = {row["key"]: row for row in service.work()["jobs"]}
        check("a running job is shown as active",
            active.get("psycho", {}).get("active") is True,
            str(active.get("psycho"))[:300])

        # let the child print a line or two, so the log has something to show
        pid = int(status.get("pid") or 0)
        for _ in range(100):
            if (lib / "library-work" / "app" / "psycho" / "jobs").is_dir() \
                    and procs.process_state(pid) == "running" \
                    and (service.work()["jobs"]):
                break
            time.sleep(0.05)
        time.sleep(0.3)
        paused = service.control_work("psycho", "pause")
        paused_status = paused.get("status") or {}
        check("pause freezes the measurement",
            paused_status.get("state") == "paused"
            and paused_status.get("paused") is True
            and paused_status.get("active") is True,
            str(paused_status)[:300])
        frozen = procs.process_state(pid)
        check("a paused process really is T",
            frozen == "paused", f"{pid}: {frozen}")
        check("the footer shows the pause",
            {row["key"]: row for row in service.work()["jobs"]}
            .get("psycho", {}).get("paused") is True)

        resumed = service.control_work("psycho", "resume")
        resumed_status = resumed.get("status") or {}
        check("resume continues the same job",
            resumed_status.get("running") is True
            and procs.process_state(pid) == "running",
            str(resumed_status)[:300])

        again = service.control_work("psycho", "resume")
        check("continuing a running job says so instead of failing",
            "note" in (again.get("status") or {}), str(again)[:200])

        # -- no download module in the open build --------------------------
        # Audio download is an optional build-time module.  A plain checkout
        # has none, so the footer must not advertise a queue it cannot run.
        keys = [row["key"] for row in service.work()["jobs"]]
        check("the open build offers no download queue",
              "downloads" not in keys, str(keys))
        check("the open build reports no download module",
              service.state()["downloads"]["available"] is False,
              str(service.state().get("downloads")))

        bogus = service.control_work("flux", "pause")
        check("an unknown job is refused",
            "error" in bogus and bogus["jobs"] is not None, str(bogus)[:200])
        unknown = service.control_work("lyrics", "rewind")
        check("an unknown action is refused",
            "error" in (unknown.get("status") or {}), str(unknown)[:200])

        # the footer's lyric start is a *full* pass: `--missing` with no
        # `--limit`, so it keeps going until it is paused.  The 25-song batch
        # belongs to the lyrics panel's + button only.
        fake = lib / "scripts" / "asr-venv" / "bin" / "python"
        fake.parent.mkdir(parents=True, exist_ok=True)
        argv_file = lib / "asr-argv.txt"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ]; then exit 0; fi\n'
            f"printf '%s\\n' \"$@\" > '{argv_file}'\n"
            "exit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        lyrics_job.forget_backend(lib)
        started_lyrics = service.control_work("lyrics", "start")
        check("starting the lyric transcription is accepted",
            "error" not in (started_lyrics.get("status") or {}),
            str(started_lyrics)[:300])
        for _ in range(100):
            if argv_file.is_file() and argv_file.read_text("utf-8").strip():
                break
            time.sleep(0.05)
        argv = (argv_file.read_text("utf-8").split()
                if argv_file.is_file() else [])
        check("the lyric start runs the whole library",
            "--missing" in argv and "--limit" not in argv, str(argv)[:300])
        for _ in range(100):
            if procs.process_state(
                    int((started_lyrics.get("status") or {}).get("pid") or 0)) \
                    == "dead":
                break
            time.sleep(0.05)
        procs.reap(int((started_lyrics.get("status") or {}).get("pid") or 0))

        # a paused job must be cleaned up, never left behind
        procs.cancel(pid)
        for _ in range(100):
            if procs.process_state(pid) == "dead":
                break
            time.sleep(0.05)
        procs.reap(pid)
        check("the frozen job can be stopped",
            procs.process_state(pid) == "dead", str(pid))
    finally:
        service.engine.close()

    bare = work / "bare-work"
    (bare / "Playlists").mkdir(parents=True, exist_ok=True)
    (bare / "Artists").mkdir(parents=True, exist_ok=True)
    quiet = ChatService(root=bare, use_llm=False, auto_psycho=False)
    try:
        check("a library without the tools lists no work",
              quiet.work()["jobs"] == [], str(quiet.work())[:200])
    finally:
        quiet.engine.close()

    # a file that failed to decode is not "work left": the row exists with
    # source='analysis' and no measurements, and a second pass would fail on
    # exactly the same file
    done_lib = work / "workdone"
    (done_lib / "library-work" / "app" / "psycho").mkdir(parents=True)
    (done_lib / "scripts").mkdir(parents=True)
    (done_lib / "scripts" / "psycho_features.py").write_text(
        "print('nothing to do', flush=True)\n", encoding="utf-8")
    (done_lib / "scripts" / "psycho_profile.py").write_text(
        "print('[psycho] profile written', flush=True)\n", encoding="utf-8")
    conn = sqlite3.connect(done_lib / "library-work" / "app" / "psycho" /
                           "features.db")
    conn.executescript("""
        CREATE TABLE audio (rel_path TEXT PRIMARY KEY, source TEXT,
            lufs REAL, sharpness_acum REAL);
        CREATE TABLE profile (rel_path TEXT PRIMARY KEY);
        CREATE TABLE family (name TEXT PRIMARY KEY);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.execute("INSERT INTO audio VALUES ('A/ok.mp3', 'analysis', -14.0, 1.2)")
    conn.execute("INSERT INTO audio VALUES ('A/broken.mp3', 'analysis', NULL, NULL)")
    conn.execute("INSERT INTO profile VALUES ('A/ok.mp3')")
    conn.commit()
    conn.close()
    done_counts = psycho_job.counts(done_lib)
    check("an undecodable file is counted as failed, not as work",
          done_counts["failed"] == 1 and done_counts["measured"] == 2,
          str(done_counts))
    check("a failed file does not keep the job alive",
          psycho_job.needed(done_lib) == "",
          repr(psycho_job.needed(done_lib)))


def check_inventory_refresh(work: Path) -> None:
    """An index run must re-read only the files whose size or mtime moved."""
    from chat import inventory
    from datetime import datetime, timezone

    lib = work / "inventory-refresh"
    if lib.exists():
        shutil.rmtree(lib)
    for folder in ("Artists/A", "Artists/B", "Artists/C", "Old"):
        (lib / folder).mkdir(parents=True)
    same = lib / "Artists/A/same.mp3"
    moved = lib / "Artists/B/moved.mp3"
    gone = lib / "Artists/C/gone.mp3"
    wrong_time = lib / "Artists/A/touched.mp3"
    # the row still says Music/Old/... while the shelf moved out of Music/
    legacy = lib / "Old/legacy.mp3"
    for path, payload in ((same, b"abc"), (moved, b"abcd"),
                          (wrong_time, b"xyz"), (legacy, b"zz")):
        path.write_bytes(payload)

    def stamp(path: Path) -> str:
        return datetime.fromtimestamp(path.stat().st_mtime,
                                     timezone.utc).isoformat(timespec="seconds")

    def row(rel: str, path: Path, size=None, mtime=None, **extra) -> str:
        payload = {"rel_path": rel,
                   "size_bytes": path.stat().st_size if size is None else size,
                   "mtime": mtime or stamp(path),
                   "title": rel.rsplit("/", 1)[-1]}
        payload.update(extra)
        return json.dumps(payload)

    inv = lib / "library-work" / "inventory" / "files.jsonl"
    inv.parent.mkdir(parents=True)
    good = [row("Artists/A/same.mp3", same),
            row("Artists/A/touched.mp3", wrong_time, mtime="2000-01-01T00:00:00+00:00"),
            row("Music/Old/legacy.mp3", legacy)]
    inv.write_text("\n".join(good + [
        row("Artists/B/moved.mp3", moved, size=1),
        row("Artists/C/gone.mp3", same),
    ]) + "\n", encoding="utf-8")

    before = inventory.audit(lib)
    check("the audit counts changed and missing rows",
          before["rows"] == 5 and before["changed"] == 2
          and before["missing"] == 1 and before["prefix"] == 1,
          str(before))

    result = inventory.prepare_scan(lib)
    kept = [json.loads(line) for line in
            inv.read_text(encoding="utf-8").split("\n") if line]
    rels = [entry["rel_path"] for entry in kept]
    check("the index preparation drops changed and missing rows",
          result["removed"] == 2 and result["missing"] == 1
          and "Artists/B/moved.mp3" not in rels
          and "Artists/C/gone.mp3" not in rels,
          f"{result} / {rels}")
    check("an unchanged row is kept as it is",
          "Artists/A/same.mp3" in rels, str(rels))
    check("a historical Music/ row is repointed, not dropped",
          "Old/legacy.mp3" in rels and result["repaired"] == 1,
          f"{result} / {rels}")
    check("the rewrite leaves a backup of the inventory",
          bool(result["backup"])
          and (lib / result["backup"]).is_file()
          and (lib / result["backup"]).stat().st_size > 0,
          str(result["backup"]))
    check("a second run has nothing left to do",
          inventory.prepare_scan(lib)["removed"] == 0,
          str(inventory.prepare_scan(lib)))
    check("the audit agrees after the rewrite",
          inventory.audit(lib)["changed"] == 0
          and inventory.audit(lib)["missing"] == 0
          and inventory.audit(lib)["prefix"] == 0,
          str(inventory.audit(lib)))
    shutil.rmtree(lib, ignore_errors=True)


def check_downloads(work: Path) -> None:
    """Downloads are an optional build-time module, never the open player.

    A plain checkout must behave exactly as documented: the facade reports no
    module, the chat says so instead of searching, the tool list omits the
    download tools and the window is told `downloads.available = false`.  The
    last part injects a stub module to prove the facade really proxies to one
    when a build supplies it.
    """
    import json as json_mod
    from chat import downloads
    from chat import tools as tools_mod

    lib = work / "downloads-lib"
    (lib / "Playlists").mkdir(parents=True)
    (lib / "Artists" / "A").mkdir(parents=True)
    (lib / "Artists" / "A" / "x.mp3").write_bytes(b"x")
    (lib / "library-work").mkdir(parents=True)

    # -- no module: honest stubs, no queue, no crash --------------------
    downloads.reset()
    saved_modules = os.environ.pop("JMP_MODULES", None)
    saved_path = os.environ.pop("JMP_DOWNLOAD_PATH", None)
    try:
        check("the open build installs no download module",
              downloads.installed() is False)
        check("the open build reports downloads unavailable",
              downloads.available(lib) is False)
        check("the open build has no download queue",
              downloads.jobs(lib) == [] and downloads.latest(lib) is None,
              "queue is not empty")
        check("an absent module answers read status honestly",
              downloads.status_line({}) == "unavailable"
              and downloads._state(lib, None).get("available") is False)
        try:
            downloads.search("portishead")
            raised = None
        except downloads.DownloadUnavailable as exc:
            raised = str(exc)
        check("searching without a module is refused", bool(raised), str(raised))
        check("the refusal points at the optional module",
              raised is not None and "download module" in raised.lower(),
              str(raised))
        import contextlib
        import io
        quiet = io.StringIO()
        with contextlib.redirect_stderr(quiet):
            code = downloads.run_importer(lib, ["--help"])
        check("the CLI importer needs a module", code == 1, str(code))
        check("the CLI refusal names the missing module",
              "download module" in quiet.getvalue().lower(),
              quiet.getvalue()[:200])

        # -- the chat and the tool list say the same thing --------------
        engine = Engine(root=lib, use_llm=False, dry_run=True)
        try:
            reply = engine.handle("search youtube for portishead")
            text = clean_text(reply)
            check("chat refuses downloads without a module",
                  "no download module" in text.lower(), text[:300])
            reply = engine.handle("add https://example.invalid/abc to my library")
            check("a direct link is refused the same way",
                  "no download module" in clean_text(reply).lower(),
                  clean_text(reply)[:300])
            specs = {spec["function"]["name"]
                     for spec in Toolbox(engine).specs()}
            check("a module-less build offers no download tools",
                  "import_search" not in specs
                  and "import_download" not in specs, str(sorted(specs)))
            check("the open tool list still has the catalogue",
                  "search_library" in specs and "library_overview" in specs)
            check("build_specs can request the download tools",
                  {"import_search", "import_download"}
                  <= {spec["function"]["name"]
                      for spec in tools_mod.Toolbox.build_specs(True)})

            # the planner still understands the sentences: only the module
            # is missing, not the intent
            for sentence, action in (
                    ("retry the download", "retry"),
                    ("nochmal herunterladen", "retry"),
                    ("cancel the download", "cancel"),
                    ("stoppe den download", "cancel"),
                    ("clear the failed download", "dismiss"),
                    ("download status", "status")):
                planned = nlu_plan(sentence, engine.ctx, engine.catalog)
                check(f"“{sentence}” plans {action}",
                      planned is not None
                      and planned.skill == "import-audio"
                      and planned.args.get("action") == action,
                      str(planned)[:200])
            planned = nlu_plan("move the download up", engine.ctx,
                               engine.catalog)
            check("a reorder sentence keeps its direction",
                  planned is not None and planned.args.get("action") == "move"
                  and planned.args.get("direction") == "up",
                  str(planned)[:200])
            for sentence in ("stop the music", "pause the playback"):
                planned = nlu_plan(sentence, engine.ctx, engine.catalog)
                check(f"“{sentence}” stays a transport command",
                      planned is not None and planned.skill == "transport",
                      str(planned)[:200])
            planned = nlu_plan(
                "add https://example.invalid/one and https://example.invalid/two "
                "to the library", engine.ctx, engine.catalog)
            check("one sentence can carry several links",
                  planned is not None
                  and planned.args.get("urls") == ["https://example.invalid/one",
                                                   "https://example.invalid/two"],
                  str(planned)[:300])
        finally:
            engine.close()

        # -- a stub module proves the facade proxies --------------------
        stub_root = work / "stub-modules"
        package = stub_root / "jmp_download"
        package.mkdir(parents=True)
        log = work / "stub-calls.jsonl"
        (package / "__init__.py").write_text(
            "import json, os\n"
            "MODULE_NAME = 'jmp_download'\n"
            "IMPORTER = 'jmp_download.importer'\n"
            "def _log(name, *args):\n"
            "    with open(os.environ['JMP_STUB_LOG'], 'a', encoding='utf-8')"
            " as fh:\n"
            "        fh.write(json.dumps([name, list(args)]) + '\\n')\n"
            "def available(root): return True\n"
            "def search(query, count=6, timeout=90):\n"
            "    _log('search', query, count)\n"
            "    return [{'url': 'https://example.invalid/x', 'title': 'T',\n"
            "             'channel': 'C'}]\n"
            "def probe(url, timeout=60):\n"
            "    _log('probe', url); return {'url': url, 'title': 'T'}\n"
            "def guess_metadata(candidate):\n"
            "    return ('A', candidate.get('title') or '')\n"
            "def start(root, candidates, **options):\n"
            "    _log('start', candidates); return list(candidates)\n"
            "def status_line(record): return 'stub state'\n"
            "def _state(root, record):\n"
            "    return {'state': 'idle', 'running': False, 'available': True}\n"
            "def jobs(root): return []\n"
            "def latest(root): return None\n",
            encoding="utf-8")
        os.environ["JMP_MODULES"] = str(stub_root)
        os.environ["JMP_STUB_LOG"] = str(log)
        downloads.reset()
        try:
            check("a supplied module is found",
                  downloads.installed() is True
                  and downloads.available(lib) is True)
            hits = downloads.search("portishead", count=2)
            check("the facade proxies search",
                  hits and hits[0]["url"] == "https://example.invalid/x", str(hits))
            check("the facade proxies the importer entry",
                  downloads.importer_entry() == "jmp_download.importer",
                  downloads.importer_entry())
            calls = [json_mod.loads(line) for line in
                     log.read_text(encoding="utf-8").splitlines() if line]
            check("the call reached the module",
                  calls and calls[0][0] == "search"
                  and calls[0][1][:2] == ["portishead", 2], str(calls))
            check("a module's status text is used",
                  downloads.status_line({}) == "stub state")
        finally:
            os.environ.pop("JMP_MODULES", None)
            os.environ.pop("JMP_STUB_LOG", None)
            downloads.reset()
            if saved_modules is not None:
                os.environ["JMP_MODULES"] = saved_modules
            if saved_path is not None:
                os.environ["JMP_DOWNLOAD_PATH"] = saved_path
        check("the module cache is cleared again",
              downloads.installed() is False)
    finally:
        shutil.rmtree(lib, ignore_errors=True)


def check_loudness(work: Path) -> None:
    """A measured level becomes an mpv audio filter, never a tag write."""
    import sqlite3
    from types import SimpleNamespace
    from chat import loudness
    from chat.engine import Engine

    lib = work / "loudness"
    if lib.exists():
        shutil.rmtree(lib)
    db = loudness.db_path(lib)
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE audio (rel_path TEXT, lufs REAL)")
    conn.executemany("INSERT INTO audio VALUES (?, ?)", [
        ("A/quiet.mp3", -20.0),
        ("A/loud.mp3", -3.0),
        ("A/target.mp3", -16.0),
        ("A/silent.mp3", -120.0),
    ])
    conn.commit()
    conn.close()

    check("a quiet track is lifted to the target",
          round(loudness.gain(lib, "A/quiet.mp3") or 0, 2) == 4.0,
          str(loudness.gain(lib, "A/quiet.mp3")))
    check("a loud track is cut, but no further than the cap",
          round(loudness.gain(lib, "A/loud.mp3") or 0, 2) == -12.0,
          str(loudness.gain(lib, "A/loud.mp3")))
    check("a track at the target needs no filter",
          loudness.filter_for(lib, "A/target.mp3") == "",
          loudness.filter_for(lib, "A/target.mp3"))
    check("a silent measurement is ignored",
          loudness.gain(lib, "A/silent.mp3") is None,
          str(loudness.gain(lib, "A/silent.mp3")))
    check("an unmeasured track plays untouched",
          loudness.filter_for(lib, "A/none.mp3") == "",
          loudness.filter_for(lib, "A/none.mp3"))
    detail = loudness.describe(lib, "A/quiet.mp3")
    check("the properties say what the levelling would do",
          detail["measured"] and detail["lufs"] == -20.0
          and detail["target"] == -16.0 and detail["gain_db"] == 4.0
          and detail["filter"] == "volume=4.0dB", str(detail))

    class FakeLoudPlayer:
        def __init__(self):
            self.running_state = True
            self.filters: list[str] = []

        def running(self):
            return self.running_state

        def set_loudness(self, value):
            self.filters.append(value)
            return True, ""

    engine = SimpleNamespace(
        root=lib, player=FakeLoudPlayer(), _loudness_filter=None,
        profile=SimpleNamespace(setting=lambda key, default=None: setting[0]))
    setting = ["track"]
    Engine._apply_loudness(engine, "A/quiet.mp3")
    check("the engine hands the filter to the player",
          engine.player.filters == ["volume=4.0dB"],
          str(engine.player.filters))
    Engine._apply_loudness(engine, "A/quiet.mp3")
    check("the same filter is not set twice",
          engine.player.filters == ["volume=4.0dB"],
          str(engine.player.filters))
    setting[0] = "off"
    Engine._apply_loudness(engine, "A/quiet.mp3")
    check("switching levelling off clears the filter",
          engine.player.filters == ["volume=4.0dB", ""],
          str(engine.player.filters))
    engine.player.running_state = False
    Engine._apply_loudness(engine, "A/quiet.mp3")
    check("a stopped player forgets the filter for the next daemon",
          engine._loudness_filter is None, str(engine._loudness_filter))
    shutil.rmtree(lib, ignore_errors=True)

    from chat import settings as registry
    for sentence, level in (("normalise the loudness", "track"),
                            ("gleiche Lautstärke", "track"),
                            ("lautstärke angleichen", "track"),
                            ("loudness as mastered", "off"),
                            ("schalte die lautstärke angleichung ein", None)):
        plan = registry.parse(sentence)
        if level is None:
            check(f"“{sentence}” does not set a level",
                  plan is None or plan.get("action") != "set", str(plan))
        else:
            check(f"“{sentence}” sets the levelling",
                  bool(plan) and plan.get("key") == "loudness_level"
                  and plan.get("value") == level, str(plan))


def check_recent(work: Path) -> None:
    """Recent journal changes, and undoing one playlist edit exactly."""
    import base64
    from chat import recent

    lib = work / "recent"
    if lib.exists():
        shutil.rmtree(lib)
    journal = lib / "library-work" / "journal" / "playlists.jsonl"
    journal.parent.mkdir(parents=True)
    custom = lib / "Playlists" / "Custom"
    custom.mkdir(parents=True)
    one = custom / "One.m3u8"

    def stored(text: str) -> str:
        return base64.b64encode(text.encode()).decode("ascii")

    one.write_text("#EXTM3U\nB\n", encoding="utf-8")
    rows = [
        {"ts": "2026-11-22T10:00:00+00:00", "action": "write",
         "path": "Playlists/Custom/One.m3u8", "existed": False,
         "before_b64": ""},
        {"ts": "2026-11-22T10:05:00+00:00", "action": "write",
         "path": "Playlists/Custom/One.m3u8", "existed": True,
         "before_b64": stored("#EXTM3U\nA\n")},
        {"ts": "2026-11-22T10:06:00+00:00", "action": "delete",
         "path": "Playlists/Custom/Gone.m3u8", "existed": True,
         "before_b64": stored("#EXTM3U\nC\n")},
        {"ts": "2026-11-22T10:07:00+00:00", "action": "write",
         "path": "Playlists/Custom/Fresh.m3u8", "existed": False,
         "before_b64": ""},
    ]
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows),
                       encoding="utf-8")
    (custom / "Fresh.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    tags = lib / "library-work" / "journal" / "field-writes.jsonl"
    tags.write_text(json.dumps({
        "ts": "2026-11-22T10:08:00+00:00",
        "rel_path": "Artists/A/A/Song.mp3",
        "before": {"title": "x"}, "after": {"title": "y", "artist": "A"},
    }) + "\n", encoding="utf-8")
    imports = lib / "library-work" / "journal" / "youtube-import.jsonl"
    imports.write_text(json.dumps({
        "ts": "2026-11-22T10:09:00+00:00", "action": "file",
        "from": "library-work/youtube/s/x.opus",
        "to": "Artists/B/B/Song.opus", "tags": {"artist": "B"},
    }) + "\n", encoding="utf-8")
    outside = lib / "library-work" / "journal" / "outside.jsonl"
    outside.write_text("", encoding="utf-8")

    listed = recent.entries(lib, 30)
    check("the newest journal entries come first",
          [row["journal"] for row in listed][:3]
          == ["youtube-import.jsonl", "field-writes.jsonl",
              "playlists.jsonl"],
          str([(r["journal"], r["ts"]) for r in listed]))
    check("only the newest change of a playlist can be undone",
          [(row["title"], row["undoable"]) for row in listed
           if row["journal"] == "playlists.jsonl"]
          == [("saved Fresh", True), ("deleted Gone", True),
              ("saved One", True), ("saved One", False)],
          str([(r["title"], r["undoable"]) for r in listed
               if r["journal"] == "playlists.jsonl"]))
    check("a tag write and an import are listed read-only",
          all(not row["undoable"] for row in listed
              if row["journal"] in {"field-writes.jsonl",
                                    "youtube-import.jsonl"}))

    result = recent.undo_playlist(lib, "2026-11-22T10:05:00+00:00")
    check("undo restores the exact previous playlist content",
          result["ok"] and one.read_text(encoding="utf-8") == "#EXTM3U\nA\n",
          f"{result} / {one.read_text(encoding='utf-8')!r}")
    check("the undo is journalled so it can itself be reversed",
          json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
          ["action"] == "undo")
    result = recent.undo_playlist(lib, "2026-11-22T10:00:00+00:00")
    check("a superseded change is refused",
          not result["ok"] and "changed again" in result["message"],
          str(result))
    result = recent.undo_playlist(lib, "2026-11-22T10:06:00+00:00")
    check("undo brings a deleted playlist back",
          result["ok"] and (custom / "Gone.m3u8").read_text(
              encoding="utf-8") == "#EXTM3U\nC\n", str(result))
    result = recent.undo_playlist(lib, "2026-11-22T10:07:00+00:00")
    check("undo removes a list that was newly written",
          result["ok"] and not (custom / "Fresh.m3u8").exists(), str(result))
    check("an unknown stamp is answered honestly",
          not recent.undo_playlist(lib, "1999-01-01T00:00:00+00:00")["ok"])
    check("a path outside Playlists/ is never touched",
          not recent._safe_playlist("Artists/A/Song.mp3"))

    from chat.nlu import Context, plan as plan_sentence
    for sentence, action in (
            ("what changed recently", "changes"),
            ("show me the recent changes", "changes"),
            ("what did you change", "changes"),
            ("was hat sich geändert", "changes"),
            ("undo the last playlist change", "undo"),
            ("mach die letzte playlist änderung rückgängig", "undo")):
        planned = plan_sentence(sentence, Context(), None)
        check(f"the planner routes “{sentence}”",
              planned is not None and planned.skill == "activity"
              and planned.args.get("action") == action, str(planned)[:200])
    shutil.rmtree(lib, ignore_errors=True)


def check_health(work: Path) -> None:
    """The health audit reads the inventory and the databases, nothing else."""
    import sqlite3
    from datetime import datetime, timezone
    from chat import health

    lib = work / "health"
    if lib.exists():
        shutil.rmtree(lib)
    (lib / "Artists" / "A" / "A").mkdir(parents=True)
    good = lib / "Artists" / "A" / "A" / "01.mp3"
    bad = lib / "Artists" / "B" / "B" / "01.mp3"
    spc = lib / "Chiptune" / "SPC" / "Game" / "theme.spc"
    for path, payload in ((good, b"a"), (bad, b"bb"), (spc, b"ccc")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def stamp(path):
        return datetime.fromtimestamp(path.stat().st_mtime,
                                     timezone.utc).isoformat(timespec="seconds")

    rows = [
        {"rel_path": good.relative_to(lib).as_posix(), "ext": ".mp3",
         "size_bytes": good.stat().st_size, "mtime": stamp(good),
         "duration_s": 60, "decode_ok": True, "has_embedded_art": True,
         "title": "One", "artist": "A", "album": "A", "genre": "Rock"},
        {"rel_path": bad.relative_to(lib).as_posix(), "ext": ".mp3",
         "size_bytes": bad.stat().st_size, "mtime": stamp(bad),
         "duration_s": 120, "decode_ok": False, "art_count": 0,
         "title": "", "artist": "", "album": "B", "genre": "",
         "issues": ["missing title"]},
        {"rel_path": spc.relative_to(lib).as_posix(), "ext": ".spc",
         "size_bytes": spc.stat().st_size, "mtime": stamp(spc),
         "duration_s": 30, "decode_ok": True},
    ]
    inv = lib / "library-work" / "inventory" / "files.jsonl"
    inv.parent.mkdir(parents=True)
    inv.write_text("".join(json.dumps(row) + "\n" for row in rows),
                   encoding="utf-8")

    playlists = lib / "Playlists"
    playlists.mkdir()
    (playlists / "All.m3u8").write_text(
        "#EXTM3U\n#EXTINF:60,A\nArtists/A/A/01.mp3\n"
        "Artists/B/B/01.mp3\n", encoding="utf-8")

    psycho = lib / "library-work" / "app" / "psycho" / "features.db"
    psycho.parent.mkdir(parents=True)
    conn = sqlite3.connect(psycho)
    conn.execute("CREATE TABLE audio (rel_path TEXT, source TEXT, lufs REAL)")
    conn.executemany("INSERT INTO audio VALUES (?,?,?)", [
        ("Artists/A/A/01.mp3", "analysis", -14.0),
        ("Artists/B/B/01.mp3", "analysis", None),
    ])
    conn.commit()
    conn.close()

    lyrics = lib / "library-work" / "app" / "lyrics" / "lyrics.db"
    lyrics.parent.mkdir(parents=True)
    conn = sqlite3.connect(lyrics)
    conn.execute("CREATE TABLE track (rel_path TEXT, text TEXT)")
    conn.execute("INSERT INTO track VALUES ('Artists/A/A/01.mp3', 'la la')")
    conn.commit()
    conn.close()

    reports = lib / "library-work" / "reports"
    reports.mkdir(parents=True)
    (reports / "probable-duplicates.csv").write_text(
        "a,b\n1,2\n3,4\n", encoding="utf-8")
    (reports / "duplicates.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    data = health.summary(lib)
    check("the audit counts every inventory row",
          data["tracks"] == 3 and data["chiptune"] == 1, str(data["tracks"]))
    check("the tag gaps are counted on taggable files only",
          data["tags"]["taggable"] == 2 and data["tags"]["no_title"] == 1
          and data["tags"]["no_artist"] == 1 and data["tags"]["no_genre"] == 1,
          str(data["tags"]))
    check("artwork is counted as embedded, folder or none",
          data["artwork"]["with_embedded"] == 1
          and data["artwork"]["none"] >= 1, str(data["artwork"]))
    check("a decode failure is reported",
          data["decode"]["failed"] == 1 and data["decode"]["ok"] == 2,
          str(data["decode"]))
    check("the inventory audit agrees with the files on disk",
          data["inventory"]["rows"] == 3 and data["inventory"]["missing"] == 0
          and data["inventory"]["changed"] == 0, str(data["inventory"]))
    check("the profile and lyrics counts come from their databases",
          data["profile"]["total"] == 2 and data["profile"]["with_data"] == 1
          and data["profile"]["failed"] == 1
          and data["lyrics"]["with_data"] == 1, f"{data['profile']} {data['lyrics']}")
    check("the playlists are counted with their entries",
          data["playlists"] == {"lists": 1, "entries": 2},
          str(data["playlists"]))
    check("the duplicate reports are summarised",
          data["duplicates"]["probable"] == 2
          and data["duplicates"]["kept_on_purpose"] == 1,
          str(data["duplicates"]))
    text = health.markdown(data)
    check("the health answer reads like a summary",
          "Library health" in text and "Decode" in text and "Playlists" in text,
          text[:200])

    from chat.nlu import Context, plan as plan_sentence
    for sentence in ("is the library healthy", "check the library",
                     "what is broken", "ist die bibliothek gesund",
                     "prüfe die bibliothek"):
        planned = plan_sentence(sentence, Context(), None)
        check(f"“{sentence}” asks for the health check",
              planned is not None and planned.skill == "library-overview"
              and planned.args.get("action") == "health", str(planned)[:200])
    planned = plan_sentence("scan the library", Context(), None)
    check("a scan stays an index, not a health check",
          planned is not None and planned.skill == "index-library",
          str(planned)[:200])
    shutil.rmtree(lib, ignore_errors=True)


def check_duplicates(work: Path) -> None:
    """The duplicate reviewer reads the reports and never touches a file."""
    from chat import duplicates

    lib = work / "duplicates"
    if lib.exists():
        shutil.rmtree(lib)
    album = lib / "Artists" / "Dupe" / "Album" / "01.mp3"
    best = lib / "Artists" / "Dupe" / "Best" / "01.mp3"
    solo = lib / "Artists" / "Solo" / "Only" / "01.mp3"
    for path in (album, best, solo):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ID3" + b"\x00" * 20)

    rows = [
        ("review", "probable", "ghost|dupe", "keep",
         "Music/Artists/Dupe/Album/01.mp3", "Ghost", "Dupe", "Album",
         "211.5", "192000", "5000000"),
        ("review", "probable", "ghost|dupe", "review",
         "Music/Artists/Dupe/Best/01.mp3", "Ghost", "Dupe", "Best Of",
         "213.0", "128000", "3000000"),
        ("review", "probable", "lost|dupe", "review",
         "Music/Artists/Dupe/Gone/01.mp3", "Lost", "Dupe", "Gone",
         "100", "128000", "1000000"),
    ]
    reports = lib / "library-work" / "reports"
    reports.mkdir(parents=True)
    header = ("action,reason,group,role,rel_path,title,artist,album,"
              "duration_s,bitrate,size_bytes\n")
    (reports / "duplicates.csv").write_text(
        header + "".join(",".join(row) + "\n" for row in rows),
        encoding="utf-8-sig")
    (reports / "probable-duplicates.csv").write_text(
        "probable_file,kept_file,artist,title,album_probable,album_kept,"
        "length_probable,length_kept,seconds_probable,seconds_kept,"
        "difference_s,bitrate_probable,bitrate_kept,size_probable_mb,"
        "size_kept_mb,note\n"
        "Music/Artists/Dupe/Best/01.mp3,Music/Artists/Dupe/Album/01.mp3,"
        "Dupe,Ghost,Best Of,Album,3:33,3:31,213.0,211.5,1.5,128000,192000,3,5,\n",
        encoding="utf-8")

    inventory = lib / "library-work" / "inventory" / "files.jsonl"
    inventory.parent.mkdir(parents=True)
    inv_rows = [
        {"rel_path": "Artists/Dupe/Album/01.mp3", "title": "Ghost",
         "artist": "Dupe", "album": "Album", "duration_s": 211.5,
         "ext": ".mp3"},
        {"rel_path": "Artists/Dupe/Best/01.mp3", "title": "Ghost",
         "artist": "Dupe", "album": "Best Of", "duration_s": 213,
         "ext": ".mp3"},
        {"rel_path": "Artists/Solo/Only/01.mp3", "title": "Solo",
         "artist": "Solo", "album": "Only", "duration_s": 100,
         "ext": ".mp3"},
    ]
    inventory.write_text("".join(json.dumps(row) + "\n" for row in inv_rows),
                         encoding="utf-8")

    def snapshot() -> dict[str, int]:
        # app state (profile/session) may be written by any turn; the music,
        # the reports, the inventory and the journals may not
        return {path.relative_to(lib).as_posix(): path.stat().st_size
                for path in lib.rglob("*") if path.is_file()
                and not path.relative_to(lib).as_posix().startswith(
                    "library-work/app/")}

    before = snapshot()

    data = duplicates.summary(lib)
    ghost = next(group for group in data["items"]
                 if group["key"] == "ghost|dupe")
    lost = next(group for group in data["items"]
                if group["key"] == "lost|dupe")
    check("the duplicate reports become groups",
          data["groups"] == 2 and data["files"] == 3
          and data["present"] == 2 and data["missing"] == 1,
          str({key: data[key] for key in ("groups", "files", "present",
                                          "missing")}))
    check("a pair from the two reports is one group of copies",
          len(ghost["tracks"]) == 2
          and {track["album"] for track in ghost["tracks"]}
          == {"Album", "Best Of"}, str(ghost["tracks"])[:300])
    check("a historical Music prefix is stripped",
          all(not track["rel"].startswith("Music/")
              for track in ghost["tracks"]))
    check("the report's keep copy is marked",
          ghost["keep"] == "Artists/Dupe/Album/01.mp3", ghost["keep"])
    check("the bitrate is shown in kbit/s",
          ghost["tracks"][0]["bitrate"] == 192, str(ghost["tracks"][0]))
    check("a copy that is gone is counted, not invented",
          lost["present"] == 0 and lost["missing"] == 1
          and lost["tracks"][0]["exists"] is False, str(lost)[:300])
    check("the length gap tells a master from a take",
          ghost["length_gap"] == 1.5, str(ghost["length_gap"]))
    text = duplicates.markdown(data)
    check("the reviewer says nothing is deleted",
          "Duplicate review" in text and "Nothing is deleted" in text
          and "not on disk" in text, text[:400])
    check("one group reads in full",
          "Nothing was changed" in duplicates.describe(ghost)
          and "Best Of" in duplicates.describe(ghost),
          duplicates.describe(ghost)[:300])

    engine = Engine(root=lib, use_llm=False)
    reply = engine.handle("find duplicate songs")
    check("chat lists the duplicate groups",
          "Duplicate review" in reply.text and "Ghost" in reply.text,
          reply.text[:300])
    reply = engine.handle("show duplicate 1")
    check("chat shows both copies of one group",
          "kept" in reply.text and len(reply.tracks or []) == 2,
          reply.text[:300])
    reply = engine.handle("how many duplicates are there")
    check("chat answers the duplicate count",
          "2 duplicate groups" in reply.text, reply.text[:200])
    engine.dry_run = True
    reply = engine.handle("play duplicate 1")
    check("playing a copy in a dry run starts nothing",
          "playback skipped" in reply.text, reply.text[:300])
    engine.dry_run = False
    engine.close()

    from chat.nlu import Context, plan as plan_sentence

    class _Catalog:
        """The planner only asks a catalogue for a playlist while it parses."""

        def find_playlist(self, *args, **kwargs):
            return None

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    catalog_stub = _Catalog()
    for sentence, action in (("find duplicate songs", "list"),
                             ("zeig mir die duplikate", "list"),
                             ("show duplicate 2", "show"),
                             ("play duplicate 2", "play"),
                             ("how many duplicates are there", "status")):
        planned = plan_sentence(sentence, Context(), catalog_stub)
        check(f"“{sentence}” asks for the duplicate review",
              planned is not None and planned.skill == "duplicates"
              and planned.args.get("action") == action, str(planned)[:200])
    planned = plan_sentence("play something twice as fast", Context(),
                            catalog_stub)
    check("a doubled tempo is not a duplicate request",
          planned is not None and planned.skill == "transport",
          str(planned)[:200])

    check("the duplicate reviewer changed no file", before == snapshot())
    shutil.rmtree(lib, ignore_errors=True)


def check_arrivals(work: Path) -> None:
    """Recently added: the newest files by their own time, read-only."""
    from datetime import datetime, timedelta, timezone
    from chat import arrivals

    lib = work / "arrivals"
    if lib.exists():
        shutil.rmtree(lib)
    now = datetime.now(timezone.utc)

    def ago(hours: float) -> str:
        return (now - timedelta(hours=hours)).isoformat(timespec="seconds")

    rows = [
        {"rel_path": "Music/Artists/Neu/Album/01.mp3", "ext": ".mp3",
         "title": "Fresh", "artist": "Neu", "album": "Album",
         "genre": "Metal", "duration_s": 100, "size_bytes": 1000,
         "mtime": ago(1)},
        {"rel_path": "Artists/Neu/Album/02.mp3", "ext": ".mp3",
         "title": "This Week", "artist": "Neu", "album": "Album",
         "duration_s": 120, "size_bytes": 2000, "mtime": ago(3 * 24)},
        {"rel_path": "Artists/Alt/Other/01.mp3", "ext": ".mp3",
         "title": "This Month", "artist": "Alt", "album": "Other",
         "duration_s": 140, "size_bytes": 3000, "mtime": ago(20 * 24)},
        {"rel_path": "Artists/Alt/Other/02.mp3", "ext": ".mp3",
         "title": "This Year", "artist": "Alt", "album": "Other",
         "duration_s": 160, "size_bytes": 4000, "mtime": ago(200 * 24)},
        {"rel_path": "Artists/Gone/Album/01.mp3", "ext": ".mp3",
         "title": "Missing", "artist": "Gone", "album": "Album",
         "duration_s": 100, "size_bytes": 1000, "mtime": ago(2)},
        {"rel_path": "Artists/Alt/Other/03.mp3", "ext": ".mp3",
         "title": "No Time", "artist": "Alt", "album": "Other",
         "duration_s": 100, "size_bytes": 1000, "mtime": ""},
    ]
    for row in rows:
        if row["title"] == "Missing":
            continue
        path = lib / row["rel_path"].removeprefix("Music/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ID3" + b"\x00" * 20)
    inventory = lib / "library-work" / "inventory" / "files.jsonl"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text("".join(json.dumps(row) + "\n" for row in rows),
                         encoding="utf-8")

    def snapshot() -> dict[str, int]:
        return {path.relative_to(lib).as_posix(): path.stat().st_size
                for path in lib.rglob("*") if path.is_file()
                and not path.relative_to(lib).as_posix().startswith(
                    "library-work/app/")}

    before = snapshot()
    data = arrivals.summary(lib, limit=10)
    check("arrivals count every placeable row",
          data["total"] == 4, str(data["total"]))
    check("a row whose file is gone is counted, not listed",
          data["missing"] == 1 and len(data["items"]) == 4,
          f'{data["missing"]} / {len(data["items"])}')
    check("the windows count today, 7, 31 and 366 days",
          (data["today"], data["week"], data["month"], data["year"])
          == (1, 2, 3, 4),
          str({key: data[key] for key in ("today", "week", "month",
                                          "year")}))
    check("the list is newest first",
          [entry["title"] for entry in data["items"]]
          == ["Fresh", "This Week", "This Month", "This Year"],
          str([entry["title"] for entry in data["items"]]))
    check("a historical Music prefix is stripped",
          data["items"][0]["rel"] == "Artists/Neu/Album/01.mp3"
          and data["items"][0]["exists"] is True,
          data["items"][0]["rel"])
    check("a row without a usable time is dropped",
          all(entry["title"] != "No Time" for entry in data["items"]))
    check("the newest entry carries its age",
          data["newest"]["title"] == "Fresh"
          and "ago" in data["items"][0]["age"], str(data["newest"])[:200])

    check("a window keeps only the last week",
          [entry["title"] for entry in arrivals.recent(lib, days=7)]
          == ["Fresh", "This Week"],
          str([entry["title"] for entry in arrivals.recent(lib, days=7)]))
    check("a limit is honoured",
          len(arrivals.recent(lib, limit=2)) == 2)

    text = arrivals.markdown(data, limit=2)
    check("the answer lists the newest first and names the caveat",
          "Recently added" in text and "1. **Neu — Fresh**" in text
          and "changed recently" in text, text[:500])
    check("every listed track carries its path",
          "`Artists/Neu/Album/01.mp3`" in text, text[:400])
    status = arrivals.status_markdown(data)
    check("the status answer names the newest file",
          "The newest file is **Neu — Fresh**" in status, status[:300])

    engine = Engine(root=lib, use_llm=False)
    reply = engine.handle("what's new")
    check("chat lists what arrived lately",
          "Recently added" in reply.text and len(reply.tracks or []) == 4,
          reply.text[:300])
    reply = engine.handle("show the newest tracks")
    check("chat understands the newest-tracks phrasing",
          "Recently added" in reply.text, reply.text[:200])
    reply = engine.skill_recent({"action": "status"}, "how many new songs")
    check("the status action counts instead of listing",
          "Changed today" in reply.text
          and "The newest file" in reply.text
          and "1. **Neu" not in reply.text, reply.text[:300])
    engine.dry_run = True
    reply = engine.handle("play new music")
    check("playing the newest in a dry run starts nothing",
          "playback skipped" in reply.text, reply.text[:300])
    engine.dry_run = False
    engine.close()

    from chat.nlu import Context, plan as plan_sentence

    class _Catalog:
        def find_playlist(self, *args, **kwargs):
            return None

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    catalog_stub = _Catalog()
    for sentence, action, days in (
            ("what's new", "list", None),
            ("was ist neu", "list", None),
            ("zuletzt hinzugefügt", "list", None),
            ("show me the newest 5 songs", "list", None),
            ("what arrived this week", "list", 7),
            ("play something I added recently", "play", None)):
        planned = plan_sentence(sentence, Context(), catalog_stub)
        check(f"“{sentence}” asks what arrived lately",
              planned is not None and planned.skill == "recent"
              and planned.args.get("action") == action
              and planned.args.get("days") == days,
              str(planned)[:200])
    planned = plan_sentence("what did I add this month", Context(),
                            catalog_stub)
    check("what I added this month stays the statistics slice",
          planned is not None and planned.skill == "library-overview"
          and planned.args.get("focus") == "added", str(planned)[:200])
    planned = plan_sentence("play New Order", Context(), catalog_stub)
    check("a band called New Order stays a playback request",
          planned is not None and planned.skill == "play-music",
          str(planned)[:200])
    planned = plan_sentence("play the latest album by New Order", Context(),
                            catalog_stub)
    check("the latest album by an artist stays playback",
          planned is not None and planned.skill == "play-music",
          str(planned)[:200])

    check("the arrivals view changed no file", before == snapshot())
    shutil.rmtree(lib, ignore_errors=True)


def check_explain(work: Path) -> None:
    """Ranked explanation: the measurements behind an axis, read-only."""
    import sqlite3

    from chat import nlu
    from chat.catalog import Catalog
    from chat.psycho import Psycho

    lib = work / "explain-library"
    if lib.exists():
        shutil.rmtree(lib)

    # Four tracks whose measured columns all increase together, so the
    # writer's rank of the third one is exactly (3 - 0.5) / 4 = 0.625 and the
    # rebuilt axis can be checked against arithmetic, not against itself.
    rels = [f"Artists/Eisenwerk/Album/{index:02d}.mp3" for index in (1, 2, 3, 4)]
    rows = []
    for number, rel in enumerate(rels, start=1):
        path = lib / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ID3" + b"\x00" * 48)
        rows.append({
            "rel_path": rel, "title": f"Track {number}",
            "artist": "Eisenwerk", "albumartist": "Eisenwerk",
            "album": "Album", "genre": "Metal", "grouping": "Bands",
            "duration_s": 180, "ext": ".mp3", "track": str(number),
            "size_bytes": 1000, "bitrate": 192000,
        })
    inventory = lib / "library-work" / "inventory" / "files.jsonl"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text("".join(json.dumps(row) + "\n" for row in rows),
                         encoding="utf-8")

    folder = lib / "library-work" / "app" / "psycho"
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / "features.db")
    conn.executescript("""
        CREATE TABLE audio (rel_path TEXT PRIMARY KEY, size INTEGER,
            mtime TEXT, duration_s REAL, source TEXT, analysed_at TEXT,
            estimated INTEGER DEFAULT 0, %s);
        CREATE TABLE profile (rel_path TEXT PRIMARY KEY, %s, family TEXT,
            family_distance REAL, estimated INTEGER DEFAULT 0,
            updated_at TEXT);
        CREATE TABLE family (name TEXT PRIMARY KEY, size INTEGER,
            traits TEXT, playlist TEXT);
        CREATE TABLE profile_set (rel_path TEXT NOT NULL, set_name TEXT NOT NULL,
            axes TEXT NOT NULL, estimated INTEGER DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (rel_path, set_name));
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """ % (", ".join(f"{column} REAL" for column in PSYCHO_COLUMNS),
           ", ".join(f"{axis} REAL" for axis in AXIS_ORDER)))

    # `flux` is never measured, so its component has to fall back to
    # percussiveness; `stereo_width` is not measured *and* `spread_hz` is not a
    # ranked column, so the width axis really has one part missing.
    absent = {"flux", "stereo_width"}
    for index, rel in enumerate(rels):
        measured = {column: (None if column in absent else float(index))
                    for column in PSYCHO_COLUMNS}
        conn.execute(
            f"INSERT INTO audio(rel_path, size, mtime, duration_s, source, "
            f"analysed_at, estimated, {', '.join(PSYCHO_COLUMNS)}) "
            f"VALUES(?, 1, '2026-11-21T00:00:00+00:00', 180, 'analysis', "
            f"'2026-11-21T00:00:00+00:00', 0, "
            f"{', '.join('?' for _ in PSYCHO_COLUMNS)})",
            (rel, *[measured[column] for column in PSYCHO_COLUMNS]))
        # the axes the writer would have stored for rank 0.625, written out by
        # hand: aggression keeps its `axis:speed` share (0.5) and its inverted
        # mode term (0.375); calmness loses its `1-axis:speed` term entirely
        # because the writer drops that reference; width loses stereo_width.
        shape = {axis: 0.625 for axis in AXIS_ORDER}
        shape["speed"] = 0.5
        shape["aggression"] = 0.5975
        shape["calmness"] = 0.4219
        shape["width"] = 0.625
        conn.execute(
            f"INSERT INTO profile(rel_path, {', '.join(AXIS_ORDER)}, family, "
            f"family_distance, estimated, updated_at) VALUES("
            f"{', '.join('?' for _ in range(len(AXIS_ORDER) + 5))})",
            (rel, *[shape[axis] for axis in AXIS_ORDER], "Aggressive · Bright",
             0.12, 0, "2026-11-21T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO profile_set(rel_path, set_name, axes, estimated, "
            "updated_at) VALUES(?, ?, ?, 0, ?)",
            (rel, "timbre", json.dumps({
                "warmth": 0.5125, "brightness": 0.5, "presence": 0.5,
                "air": 0.5, "sharpness": 0.5, "harmonicity": 0.5,
                "noisiness": 0.5, "roughness": 0.5}),
             "2026-11-21T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO profile_set(rel_path, set_name, axes, estimated, "
            "updated_at) VALUES(?, ?, ?, 0, ?)",
            (rel, "instrument", json.dumps({
                "bass": 0.575, "drums": 0.5, "strings": 0.5, "keys": 0.5,
                "voice": 0.5, "classical": 0.5}),
             "2026-11-21T00:00:00+00:00"))
    conn.commit()
    conn.close()

    def snapshot() -> dict[str, tuple[int, int]]:
        """Everything the explanation must not touch, music and index alike."""
        out: dict[str, tuple[int, int]] = {}
        for path in lib.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(lib).as_posix()
            if rel.startswith("library-work/app/chat/"):
                continue
            stat = path.stat()
            out[rel] = (stat.st_size, stat.st_mtime_ns)
        return out

    before = snapshot()
    psycho = Psycho(lib)
    rel = rels[2]

    data = psycho.explain(rel, terms=[("aggressive", 1.0)])
    check("explain answers for a ranked track",
          bool(data) and data["rel"] == rel, str(data)[:200])
    entry = (data or {}).get("axes", [{}])[0]
    check("the requested concept picks its axis",
          entry.get("key") == "aggression", str(entry.get("key")))
    check("the explanation keeps the stored axis value",
          entry.get("value") == 0.5975, str(entry.get("value")))
    check("the parts rebuild the stored axis",
          entry.get("rebuilt") is not None
          and abs(entry["rebuilt"] - entry["value"]) < 0.0002,
          f'{entry.get("rebuilt")} vs {entry.get("value")}')
    parts = {part["spec"]: part for part in entry.get("parts") or []}
    check("a component names its measured column",
          parts.get("energy_db", {}).get("source") == "energy_db",
          str(list(parts)))
    check("an axis component keeps its own weight",
          parts.get("axis:speed", {}).get("weight") == 0.1
          and parts.get("axis:speed", {}).get("source") == "the speed axis",
          str(parts.get("axis:speed")))
    check("a component weight is the writer's weight",
          parts.get("energy_db", {}).get("weight") == 0.22
          and round(sum(part["weight"] for part in parts.values()), 4) == 1.0,
          str([part["weight"] for part in parts.values()]))
    check("an inverted column reads as its inverse",
          parts.get("1-mode_score", {}).get("value") == 0.375,
          str(parts.get("1-mode_score")))
    check("each part contributes rank times weight",
          all(abs(part["points"] - part["value"] * part["weight"]) < 1e-6
              for part in parts.values()), str(parts))

    calm = psycho.explain(rel, axes=["calmness"])
    calm_entry = (calm or {}).get("axes", [{}])[0]
    missed = {part["spec"]: part for part in calm_entry.get("missing") or []}
    check("calmness is explained too",
          calm_entry.get("key") == "calmness"
          and abs((calm_entry.get("rebuilt") or 0) - 0.4219) < 0.0002,
          str(calm_entry.get("rebuilt")))
    check("a term the writer drops is reported, not counted",
          "1-axis:speed" in missed
          and "does not use" in missed["1-axis:speed"]["note"],
          str(missed))
    check("an unused term does not inflate the covered weight",
          calm_entry.get("covered") == 0.8, str(calm_entry.get("covered")))

    width = (psycho.explain(rel, axes=["width"]) or {}).get("axes", [{}])[0]
    check("an unmeasured column leaves its part missing",
          width.get("covered") == 0.25
          and any(part["spec"] == "stereo_width"
                  for part in width.get("missing") or []),
          str(width.get("missing")))
    check("a non-ranked column is not used as a fallback",
          width.get("rebuilt") == 0.625
          and not any(part["spec"] == "spread_hz"
                      for part in width.get("parts") or []),
          str(width.get("parts")))

    density = (psycho.explain(rel, axes=["density"]) or {}).get("axes", [{}])[0]
    flux = next((part for part in density.get("parts") or []
                 if part["spec"] == "flux"), None)
    check("a fallback part names the column it read",
          flux is not None and flux["source"] == "percussiveness",
          str(flux))

    bass = psycho.explain(rel, terms=[("bassy", 1.0)])
    bass_entry = (bass or {}).get("axes", [{}])[0]
    check("a concept swaps in its own profile set",
          bass_entry.get("set") == "instrument"
          and bass_entry.get("key") == "bass", str(bass_entry.get("set")))
    check("the instrument set is marked as an estimate",
          bass_entry.get("estimated") is True, str(bass_entry.get("estimated")))
    check("the instrument axis rebuilds from band shares",
          abs((bass_entry.get("rebuilt") or 0) - 0.575) < 0.0002,
          str(bass_entry.get("rebuilt")))

    warmth = psycho.explain(rel, axes=["warmth"], set_name="timbre")
    warmth_entry = (warmth or {}).get("axes", [{}])[0]
    check("another lens can be explained by name",
          warmth_entry.get("set") == "timbre"
          and abs((warmth_entry.get("rebuilt") or 0) - 0.5125) < 0.0002,
          str(warmth_entry))
    check("a measured lens is not called an estimate",
          warmth_entry.get("estimated") is False, str(warmth_entry))

    check("an unknown track has no explanation",
          psycho.explain("Artists/Nope/Nothing.mp3") is None)

    # -- the chat and the window API ---------------------------------------
    engine = Engine(root=lib, use_llm=False)
    track = engine.catalog.track(rel)
    engine.ctx.current = track
    reply = engine.handle("why is this the most aggressive?")
    text = clean_text(reply)
    check("a why question explains the playing track",
          reply.tool == "rank-library · why", str(reply.tool))
    check("the explanation names the stored axis",
          "Aggression 0.60" in text and "60th percentile" in text,
          text[:300])
    check("the explanation lists the measurements",
          "level (RMS)" in text and "zero-crossing rate" in text,
          text[:400])
    check("the table shows weights and contributions",
          "22 %" in text and "contributes" in text, text[:400])
    check("an axis reference is not printed as a column",
          "(the speed axis)" not in text, text[:500])
    check("the strongest parts are summarised",
          "Pulling it up" in text, text[-500:])

    engine.ctx.current = None
    engine.ctx.last_tracks = []
    empty = engine.skill_rank({"action": "why", "terms": [["aggressive", 1.0]]},
                              "why is this the most aggressive")
    check("a why question without a playing track asks which one",
          empty.error and "Which track" in empty.text, empty.text[:200])

    engine.ctx.current = track
    second = engine.catalog.track(rels[1])
    engine.ctx.last_tracks = [track, second]
    reply = engine.skill_rank({"action": "why", "terms": [["aggressive", 1.0]],
                               "target": "2"}, "why is number 2 aggressive")
    check("a numbered target explains that hit",
          bool(reply.tracks) and reply.tracks[0].rel == rels[1],
          str([item.rel for item in reply.tracks or []]))
    reply = engine.skill_rank({"action": "why", "terms": [["aggressive", 1.0]],
                               "target": "Track 3"}, "why is Track 3 aggressive")
    check("a named target explains that track",
          bool(reply.tracks) and reply.tracks[0].rel == rels[2],
          str([item.rel for item in reply.tracks or []]))

    ranking = engine.handle("the most aggressive 3")
    check("a ranking carries a why line",
          "↳ It leads on aggression" in ranking.text, ranking.text[-500:])

    # -- the planner --------------------------------------------------------
    catalog = engine.catalog
    planned = nlu.plan("why is this the most aggressive?", nlu.Context(), catalog)
    check("the planner recognises a why question",
          planned is not None and planned.skill == "rank-library"
          and planned.args.get("action") == "why", str(planned)[:200])
    check("it explains the current track by default",
          planned is not None and planned.args.get("target") == "current",
          str(planned)[:200])
    for sentence in ("warum ist das so aggressiv",
                     "what makes this track so loud",
                     "how come this is so calm"):
        planned = nlu.plan(sentence, nlu.Context(), catalog)
        check(f"the planner handles {sentence!r}",
              planned is not None and planned.skill == "rank-library"
              and planned.args.get("action") == "why", str(planned)[:200])
    planned = nlu.plan("why is Track 3 the most aggressive", nlu.Context(),
                       catalog)
    check("a named why target reaches the planner",
          planned is not None and planned.args.get("target") == "track 3",
          str(planned)[:200])
    planned = nlu.plan("why does number 2 sound so bright", nlu.Context(),
                       catalog)
    check("a numbered why target reaches the planner",
          planned is not None and planned.args.get("target") == "2",
          str(planned)[:200])
    planned = nlu.plan("why is the slowest song so sad", nlu.Context(), catalog)
    check("a generic property phrase is not taken as a title",
          planned is not None and planned.args.get("target") == "current",
          str(planned)[:200])

    planned = nlu.plan("why is this playing", nlu.Context(), catalog)
    check("a why question with no property is not a ranking",
          planned is None or planned.args.get("action") != "why",
          str(planned)[:200])
    planned = nlu.plan("find the most aggressive songs", nlu.Context(), catalog)
    check("a plain ranking is not turned into an explanation",
          planned is not None and planned.skill == "rank-library"
          and planned.args.get("action") != "why", str(planned)[:200])
    planned = nlu.plan("what did I add this month", nlu.Context(), catalog)
    check("recently added is still statistics",
          planned is not None and planned.skill == "library-overview"
          and planned.args.get("focus") == "added", str(planned)[:200])

    engine.close()

    # -- and nothing was written -------------------------------------------
    check("the explanation changed no file", before == snapshot(),
          str(sorted(set(before.items()) ^ set(snapshot().items()))[:3]))
    shutil.rmtree(lib, ignore_errors=True)


def check_pwa(work: Path) -> None:
    """The home-screen icon and the phone layout, and nothing secret in them."""
    lib = work / "pwa-library"
    if lib.exists():
        shutil.rmtree(lib)
    (lib / "library-work" / "inventory").mkdir(parents=True)
    (lib / "library-work" / "inventory" / "files.jsonl").write_text(
        "", encoding="utf-8")
    (lib / "Playlists").mkdir()

    token = "pwa-token-123"
    service = ChatService(root=lib, use_llm=False, auto_psycho=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, token))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path: str, with_token: bool = True):
        url = base + path + ("" if not with_token else
                             ("&" if "?" in path else "?") + f"t={token}")
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return (response.status, response.read(),
                        dict(response.headers))
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    try:
        status, body, headers = get("/manifest.webmanifest")
        manifest: dict = {}
        try:
            manifest = json.loads(body.decode("utf-8"))
        except ValueError:
            pass
        check("the manifest is served", status == 200, f"{status}: {body[:100]}")
        check("the manifest names the app",
              manifest.get("name") == "JulAIn Music Player"
              and manifest.get("short_name") == "JMP", str(manifest)[:200])
        check("it opens as a standalone app",
              manifest.get("display") == "standalone"
              and manifest.get("scope") == "/", str(manifest)[:200])
        check("its start URL carries this window's token",
              manifest.get("start_url") == f"/?t={token}",
              str(manifest.get("start_url")))
        icons = manifest.get("icons") or []
        check("it offers the two icon sizes a phone asks for",
              {icon.get("sizes") for icon in icons} >= {"192x192", "512x512"},
              str(icons)[:200])
        check("every icon is a real static file",
              all(icon.get("src", "").startswith("/static/")
                  and (Path(LIB) / "chat" / "webui"
                       / icon["src"].rsplit("/", 1)[-1]).is_file()
                  for icon in icons), str(icons)[:200])
        check("the manifest is not a static file",
              get("/static/manifest.webmanifest")[0] == 404)
        check("the manifest needs the token", get("/manifest.webmanifest",
                                                  with_token=False)[0] == 403)

        status, body, headers = get("/sw.js", with_token=False)
        worker = body.decode("utf-8", "replace")
        check("the service worker is served without a token", status == 200,
              str(status))
        check("it is a JavaScript worker",
              "javascript" in headers.get("Content-Type", ""),
              headers.get("Content-Type", ""))
        check("it caches only the shell",
              "/static/app.css" in worker
              and 'url.pathname.startsWith("/static/")' in worker
              and 'startsWith("/api/")' not in worker
              and "caches.match" in worker, worker[:200])

        status, body, headers = get("/static/icon-192.png", with_token=False)
        check("the 192 icon is a PNG",
              status == 200 and body[:8] == b"\x89PNG\r\n\x1a\n"
              and headers.get("Content-Type") == "image/png",
              f"{status} {headers.get('Content-Type')}")

        status, body, _headers = get("/")
        page = body.decode("utf-8", "replace")
        check("the page links the manifest with the token",
              f'<link rel="manifest" href="/manifest.webmanifest?t={token}">'
              in page, page[:400])
        check("the page asks for the dynamic viewport",
              "viewport-fit=cover" in page and
              '<meta name="theme-color"' in page, page[:400])
        check("the page carries an apple touch icon",
              'rel="apple-touch-icon"' in page
              and "apple-mobile-web-app-capable" in page, page[:500])
        check("the page still needs the token", get("/", with_token=False)[0]
              == 403)

        css = (Path(LIB) / "chat" / "webui" / "app.css").read_text(
            encoding="utf-8")
        check("a phone gets its own layout",
              "@media (max-width: 520px)" in css
              and "env(safe-area-inset-bottom" in css and "100dvh" in css,
              "no phone block")
        check("the composer does not zoom on focus",
              ".composer textarea { font-size: 16px; }" in css,
              "no 16px field")

        script = (Path(LIB) / "chat" / "webui" / "app.js").read_text(
            encoding="utf-8")
        check("the page registers the service worker",
              '"serviceWorker" in navigator' in script
              and 'register("/sw.js")' in script, script[-400:])
        check("a stale token is explained instead of just forbidden",
              "access token is out of date" in script, script[-600:])
    finally:
        httpd.shutdown()
        httpd.server_close()
        service.engine.close()
        shutil.rmtree(lib, ignore_errors=True)


def check_window() -> None:
    """The native window opens large enough and cannot be shrunk to clip.

    The settings panel is a 940 px modal with eight tabs and a sticky Save row;
    a window that starts at 1280×880 clipped it on a smaller screen.  These are
    source checks on purpose: opening a real GTK window needs a display, while
    the numbers and the two enforcement points are what regress.
    """
    source = (LIB / "chat" / "window.py").read_text(encoding="utf-8")
    check("the window opens larger than before",
          "WINDOW_SIZE = (1440, 960)" in source, source[:200])
    check("the window has a minimum size",
          "MIN_SIZE = (1024, 640)" in source, source[:200])
    check("both backends enforce the minimum size",
          source.count("set_size_request(*MIN_SIZE)") == 2,
          str(source.count("set_size_request(*MIN_SIZE)")))
    web = (LIB / "chat" / "web.py").read_text(encoding="utf-8")
    check("the browser fallback opens the same size",
          "--window-size=1440,960" in web,
          str([ln for ln in web.splitlines() if "window-size" in ln][:2]))
    installer = (LIB.parent / "install.sh").read_text(encoding="utf-8")
    check("the docker launcher's browser window matches",
          installer.count("--window-size=1440,960") == 1,
          str(installer.count("--window-size=1440,960")))


def check_journey(work: Path) -> None:
    """One realistic window session, from first paint to a saved playlist.

    The browser makes the same calls the page does — a token in `?t=`, a state
    read, a search, a click on a hit, the transport buttons, feedback, a chat
    turn, the Up-next Save button, the settings panel — and the checks assert
    what a listener would notice: the song plays, pause works, the list is
    saved and journalled, a stale token is refused with a JSON answer, and
    not one audio byte changed.
    """
    lib = work / "journey-library"
    if lib.exists():
        shutil.rmtree(lib)
    build_library(lib)

    # one hostile tag: the answer must escape it, never render it
    inventory = lib / "library-work" / "inventory" / "files.jsonl"
    rows = [json.loads(line) for line in inventory.read_text(
        encoding="utf-8").splitlines() if line.strip()]
    evil = lib / "Artists/Evil/Album/01_Payload.mp3"
    evil.parent.mkdir(parents=True, exist_ok=True)
    evil.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64)
    rows.append({
        "rel_path": "Artists/Evil/Album/01_Payload.mp3",
        "title": "<img src=x onerror=alert(1)>",
        "artist": "Evil <b>Corp</b>",
        "albumartist": "Evil <b>Corp</b>",
        "album": "Payload",
        "genre": "Metal",
        "grouping": "Bands",
        "duration_s": 120,
        "ext": ".mp3",
        "track": "1",
    })
    inventory.write_text("".join(json.dumps(row) + "\n" for row in rows),
                         encoding="utf-8")

    audio = [path for path in lib.rglob("*")
             if path.is_file() and path.suffix in
             {".mp3", ".mp2", ".wav", ".flac", ".ogg", ".opus", ".m4a"}]
    before = {str(path.relative_to(lib)): hashlib.sha256(
        path.read_bytes()).hexdigest() for path in audio}

    class FakePlayer:
        """mpv, without a daemon — every call is recorded instead."""

        def __init__(self):
            self.state = {"idle": True}
            self.played: list[tuple] = []
            self.commands: list[tuple] = []
            self.entries: list[str] = []
            self.filters: list[str] = []
            self.last_error = ""

        def status(self):
            return dict(self.state)

        def play(self, target, append=False, shuffle=False):
            target = str(target)
            self.played.append((target, append, shuffle))
            if target.lower().endswith((".m3u", ".m3u8")):
                try:
                    lines = [line.strip() for line in Path(target).read_text(
                        encoding="utf-8").splitlines()
                        if line.strip() and not line.startswith("#")]
                    self.entries = [str((Path(target).parent / line)
                                        .resolve()) for line in lines]
                except OSError:
                    self.entries = []
                self.state = {"idle": False, "file": "",
                              "playlist_count": max(1, len(self.entries)),
                              "playlist_pos": 0, "time": 0, "duration": 0,
                              "pause": False, "volume": 80}
            else:
                self.entries = [target]
                self.state = {"idle": False, "file": target,
                              "playlist_count": 1, "playlist_pos": 0,
                              "time": 0, "duration": 180, "pause": False,
                              "volume": 80, "title": Path(target).stem}
            return True, ""

        def command(self, action, value=None):
            self.commands.append((action, value))
            if action == "pause":
                self.state["pause"] = True
            elif action == "resume":
                self.state["pause"] = False
            elif action == "toggle":
                self.state["pause"] = not bool(self.state.get("pause"))
            elif action == "stop":
                self.state = {"idle": True}
            elif action == "seek":
                self.state["time"] = max(
                    0.0, float(self.state.get("time") or 0) + float(value or 0))
            elif action == "seek_abs":
                self.state["time"] = float(value or 0)
            elif action == "volume":
                self.state["volume"] = float(value or 0)
            return True, ""

        def queue_from(self, limit=200):
            return [(number, path) for number, path in
                    enumerate(self.entries[:limit], start=1)]

        def status_line(self, root=None):
            return "fake player"

        def running(self):
            return True

        def current_file(self):
            return str(self.state.get("file") or "")

        def set_loudness(self, value):
            self.filters.append(value)
            return True, ""

    token = "journey-token-42"
    service = ChatService(root=lib, use_llm=False, auto_psycho=False)
    service.engine.player = FakePlayer()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, token))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path: str, with_token: bool = True):
        url = base + path + ("" if not with_token else
                             ("&" if "?" in path else "?") + f"t={token}")
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def get_json(path: str, with_token: bool = True) -> tuple[int, dict]:
        status, body, _headers = get(path, with_token)
        try:
            return status, json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return status, {}

    def post(path: str, payload, with_token: bool = True) -> tuple[int, dict]:
        url = base + path + ("" if not with_token else
                             ("&" if "?" in path else "?") + f"t={token}")
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                return response.status, json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                return exc.code, json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return exc.code, {}

    try:
        # -- 1. the window opens -------------------------------------------
        status, body, _headers = get("/")
        page = body.decode("utf-8", "replace")
        check("the window loads with the token",
              status == 200 and "JulAIn Music Player" in page, str(status))
        check("the page carries the token the API needs",
              f'<meta name="chat-token" content="{token}">' in page,
              page[:300])
        check("the window is refused without a token", get("/", False)[0] == 403,
              "no 403")

        # -- 2. first paint: the state the page renders ---------------------
        status, state = get_json("/api/state")
        check("the first paint reads the state", status == 200, str(status))
        check("the state names the library", state.get("root") == str(lib),
              str(state.get("root")))
        check("the track count is the inventory's",
              (state.get("stats") or {}).get("files") == len(rows),
              str((state.get("stats") or {}).get("files")))
        kinds = {row.get("name"): row.get("kind")
                 for row in state.get("playlists") or []}
        check("the sidebar lists the mood list",
              kinds.get("Mood_Chill") == "mood", str(kinds)[:200])
        check("a stale token is a clear 403, not a crashed connection",
              get_json("/api/state?t=stale")[0] == 403
              or get("/api/state?t=stale")[0] == 403, "no 403")

        # -- 3. search and click a hit --------------------------------------
        status, found = get_json("/api/search?q=Eisenwerk&limit=40")
        hits = found.get("hits") or []
        check("searching a performer lists the shelf",
              status == 200 and len(hits) == 20, str(len(hits)))
        target = hits[0].get("rel") if hits else ""
        check("a hit carries the path the click sends",
              target == "Artists/Eisenwerk/Album/01_Track_1.mp3", str(target))

        status, played = post("/api/play", {"target": target})
        fake = service.engine.player
        check("a click on a hit starts that track",
              status == 200 and played.get("played") is True
              and fake.played and fake.played[0][0] == str(lib / target),
              f"{status} {str(played)[:160]} {fake.played[:1]}")
        status, now = get_json("/api/status")
        check("the now-playing strip shows the track",
              now.get("playing") is True and now.get("title") == "Track 1",
              str(now)[:220])

        # -- 4. the transport buttons --------------------------------------
        status, paused = post("/api/transport", {"action": "pause"})
        check("pause reaches the player",
              status == 200 and paused.get("ok") is True
              and ("pause", None) in fake.commands,
              f"{status} {str(paused)[:120]} {fake.commands[-3:]}")
        check("the strip shows it is paused", get_json("/api/status")[1]
              .get("paused") is True)
        post("/api/transport", {"action": "resume"})
        check("resume reaches the player",
              ("resume", None) in fake.commands, str(fake.commands[-3:]))
        post("/api/transport", {"action": "volume", "value": 40})
        check("the volume slider sends a volume",
              ("volume", 40.0) in fake.commands, str(fake.commands[-3:]))
        post("/api/transport", {"action": "seek", "value": 30})
        check("a click on the seek bar sends a relative seek",
              ("seek", 30.0) in fake.commands, str(fake.commands[-3:]))

        # -- 5. the two feedback buttons ------------------------------------
        status, liked = post("/api/feedback", {"kind": "like"})
        profile_file = lib / "library-work" / "app" / "chat" / "profile.json"
        profile = json.loads(profile_file.read_text(encoding="utf-8")) \
            if profile_file.is_file() else {}
        check("the heart writes a like",
              status == 200 and liked.get("error") is not True
              and "Eisenwerk" in json.dumps(profile),
              str(liked)[:120])
        check("a dislike is accepted too",
              post("/api/feedback", {"kind": "dislike"})[0] == 200)

        # -- 6. one chat turn, one ad-hoc queue ------------------------------
        status, answer = post("/api/chat", {"message": "play something chill"})
        own_folder = lib / "Playlists" / "Own"
        own = sorted(own_folder.glob("*.m3u8")) if own_folder.is_dir() else []
        check("a mood request answers", status == 200 and answer.get("text"),
              str(answer)[:160])
        check("it leaves a real ad-hoc list in Playlists/Own",
              len(own) == 1, str(own))
        entries = [line.strip() for line in own[0].read_text(
            encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")] if own else []
        check("the ad-hoc entries are relative and resolve",
              entries and all(line.startswith("../../")
                              and (own[0].parent / line).resolve().is_file()
                              for line in entries), str(entries[:2]))
        check("the ad-hoc list is what the player received",
              any(str(path) == str(own[0]) for path, _a, _s in fake.played),
              str(fake.played[-2:]))

        status, queue = get_json("/api/queue")
        own_view = queue.get("own") or {}
        check("the Up-next panel knows it is an ad-hoc queue",
              own_view.get("name") == own[0].stem if own else False,
              str(own_view)[:200])
        check("the panel counts its tracks",
              int(own_view.get("count") or 0) == len(entries),
              str(own_view)[:200])

        # -- 7. save it from the panel ---------------------------------------
        status, saved = post("/api/queue",
                             {"action": "save", "name": "Journey Mix"})
        custom = lib / "Playlists" / "Custom" / "Journey_Mix.m3u8"
        check("Save writes a normal playlist", status == 200
              and saved.get("ok") is True and custom.is_file(),
              f"{status} {str(saved)[:160]}")
        check("Save removes the ad-hoc copy",
              not own[0].exists() if own else False,
              str(own[0]) if own else "no own list")
        status, state = get_json("/api/state")
        kinds = {row.get("name"): row.get("kind")
                 for row in state.get("playlists") or []}
        check("the sidebar shows the saved list as Mine",
              kinds.get("Journey_Mix") == "custom"
              or kinds.get("Journey Mix") == "custom", str(kinds)[:200])
        journal = lib / "library-work" / "journal" / "playlists.jsonl"
        written = [json.loads(line) for line in journal.read_text(
            encoding="utf-8").splitlines() if line.strip()] \
            if journal.is_file() else []
        check("every playlist write is journalled",
              len(written) >= 2
              and any("Journey_Mix" in json.dumps(row) for row in written),
              str(written)[:300])

        # -- 8. a chat answer is text, never markup --------------------------
        status, answer = post("/api/chat", {"message": "search for Evil"})
        html = str(answer.get("html") or "")
        check("a hostile tag is escaped in the answer",
              status == 200 and "&lt;img" in html
              and "<img src=x" not in html, html[:300])
        check("the answer text is not swallowed either",
              "<img src=x" in str(answer.get("text") or ""),
              str(answer.get("text"))[:200])

        # -- 9. statistics and the settings panel ---------------------------
        status, stats = get_json("/api/stats")
        check("the statistics agree with the inventory",
              status == 200 and stats.get("tracks") == len(rows),
              str(stats.get("tracks")))
        status, panel = get_json("/api/settings")
        keys = {row.get("key") for row in panel.get("rows") or []}
        check("the settings panel carries every control",
              status == 200 and {"visualizer", "autoplay", "voice_enabled",
                                 "llm_provider"} <= keys, str(sorted(keys))[:300])
        status, changed = post("/api/settings", {"visualizer": False})
        check("a setting change is accepted",
              status == 200 and changed.get("visualizer") is False,
              str(changed)[:160])
        check("the change reaches the next state read",
              get_json("/api/state")[1].get("settings", {})
              .get("visualizer") is False)

        # -- 10. the licence and the AI statement are visible ----------------
        status, script, _headers = get("/static/app.js")
        text = script.decode("utf-8", "replace")
        check("the window states the licence and that AI helped write it",
              status == 200 and "AI-assisted · GPL-3.0-or-later" in text,
              str(status))
        check("the full GPL-3 text ships with the app",
              (HERE.parent / "LICENSE").is_file()
              and "Version 3, 29 June 2007"
              in (HERE.parent / "LICENSE").read_text(encoding="utf-8"),
              "no GPL-3 text")

        # -- 11. the footer job list, guarded like every API route ----------
        status, work_view = get_json("/api/work")
        check("the footer asks for jobs and gets a list",
              status == 200 and isinstance(work_view.get("jobs"), list),
              str(work_view)[:200])
        check("the footer endpoint needs the token",
              get_json("/api/work", False)[0] == 403)

        # -- 12. nothing musical was touched --------------------------------
        after = {str(path.relative_to(lib)): hashlib.sha256(
            path.read_bytes()).hexdigest() for path in audio}
        check("not one audio file changed", before == after,
              str([key for key in before if before.get(key) != after.get(key)])[:300])
        check("no tag write journal was created",
              not (lib / "library-work" / "journal"
                   / "field-writes.jsonl").exists())
        logs = list((lib / "library-work" / "app" / "chat").glob(
            "server-*.log"))
        leaked = [str(log) for log in logs
                  if token in log.read_text(encoding="utf-8", errors="replace")]
        check("the token never lands in a log", not leaked, str(leaked))
    finally:
        httpd.shutdown()
        httpd.server_close()
        service.engine.close()
        shutil.rmtree(lib, ignore_errors=True)


def check_tagedit(work: Path) -> None:
    """The tag and artwork editor: journalled writes, album batches, refusals.

    A real two-track album (ffmpeg writes the MP3s) runs the library's own
    `scripts/tag_fields.py` from inside the tree, exactly as the app does, so
    the check sees the journal, the values written back and the index row.
    """
    import subprocess
    from chat import tagedit

    source = HERE.parent.parent / "scripts"
    if not (source / "tag_fields.py").is_file() or shutil.which("ffmpeg") is None:
        check("the tag writer is present", (source / "tag_fields.py").is_file(),
              str(source / "tag_fields.py"))
        return

    lib = work / "tagedit"
    if lib.exists():
        shutil.rmtree(lib)
    album = lib / "Artists" / "Tonband" / "Album"
    album.mkdir(parents=True)
    (lib / "scripts").mkdir()
    for name in ("tag_fields.py", "apply_tier1.py"):
        shutil.copy2(source / name, lib / "scripts" / name)

    def make(name: str, frequency: int) -> Path:
        path = album / name
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi",
                        "-i", f"sine=frequency={frequency}:duration=1",
                        "-c:a", "libmp3lame", "-b:a", "64k", "-y", str(path)],
                       check=True)
        return path

    first = make("01_Erster.mp3", 440)
    second = make("02_Zweiter.mp3", 660)
    rel_first = first.relative_to(lib).as_posix()
    rel_second = second.relative_to(lib).as_posix()
    inventory = lib / "library-work" / "inventory" / "files.jsonl"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text("".join(
        json.dumps({"rel_path": rel, "ext": ".mp3", "title": f"Stück {i}",
                    "artist": "Tonband", "album": "Album",
                    "album_dir": "Artists/Tonband/Album", "track": str(i),
                    "genre": "", "duration_s": 1.0, "issues": "no_genre"})
        + "\n" for i, rel in enumerate((rel_first, rel_second), 1)),
        encoding="utf-8")
    spc = lib / "Chiptune" / "Game" / "theme.spc"
    spc.parent.mkdir(parents=True)
    spc.write_bytes(b"not really an spc")

    shown = tagedit.describe(lib, rel_first)
    check("the editor opens with the values the file holds",
          shown["writable"] and shown["values"]["artist"] == "",
          str(shown["values"]))
    check("the album it belongs to is listed",
          shown["album"]["count"] == 2
          and {item["rel"] for item in shown["album"]["tracks"]}
          == {rel_first, rel_second}, str(shown["album"]))
    check("the editor starts from the file, not a stale index row",
          shown["values"]["title"] == "", str(shown["values"]))

    result = tagedit.apply(lib, rel_first,
                           {"title": "Erster (neu)", "genre": "Electronic"})
    check("a single-file write succeeds", result.get("ok"), str(result)[:300])
    check("the values really reached the file",
          tagedit.read_tags(first)["title"] == "Erster (neu)"
          and tagedit.read_tags(first)["genre"] == "Electronic",
          str(tagedit.read_tags(first)))
    journal = lib / "library-work" / "journal" / "field-writes.jsonl"
    written = [json.loads(line) for line in
               journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    check("the tag journal has the previous state and the new one",
          len(written) == 1 and written[0]["rel_path"] == rel_first
          and written[0]["after"]["title"] == "Erster (neu)"
          and "id3_head_b64" in written[0], str(written[-1])[:200])
    check("the search index row was updated too",
          tagedit.row(lib, rel_first).get("genre") == "Electronic",
          str(tagedit.row(lib, rel_first)))
    check("a fixed field leaves the issue list",
          "no_genre" not in tagedit.row(lib, rel_first).get("issues", ""),
          str(tagedit.row(lib, rel_first).get("issues")))

    result = tagedit.apply(lib, rel_first,
                           {"album": "Das andere Album", "title": "Nope"},
                           album_wide=True)
    check("an album-wide write reaches every track",
          result.get("ok")
          and tagedit.read_tags(first)["album"] == "Das andere Album"
          and tagedit.read_tags(second)["album"] == "Das andere Album",
          str(result)[:300])
    check("a per-track field is refused in an album batch",
          result.get("dropped") == ["title"]
          and tagedit.read_tags(second)["title"] == "",
          str(result.get("dropped")))

    blocked = tagedit.describe(lib, "Chiptune/Game/theme.spc")
    check("a chiptune is shown but not editable",
          blocked["writable"] is False and "tag field" in blocked["reason"],
          str(blocked.get("reason")))
    result = tagedit.apply(lib, "Chiptune/Game/theme.spc", {"title": "x"})
    check("a write to an untaggable format is refused",
          not result.get("ok") and "tag field" in result["error"],
          str(result))

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 400
    result = tagedit.set_artwork(lib, rel_first, png, "image/png", "embed")
    check("a picture can be embedded",
          result.get("ok") and result["art"]["embedded"]
          and result["art"]["mime"] == "image/png",
          str(result.get("art")))
    art_journal = lib / "library-work" / "journal" / "artwork-edit.jsonl"
    arts = [json.loads(line) for line in
            art_journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    check("the artwork journal is written before the picture",
          arts and arts[-1]["where"] == "embed"
          and arts[-1]["before_b64"] == "", str(arts[-1])[:200])
    art_row = tagedit.row(lib, rel_first)
    check("the index knows the file now carries a picture",
          art_row.get("has_embedded_art") is True
          and int(art_row.get("art_bytes") or 0) > 0,
          str({k: art_row.get(k) for k in ("has_embedded_art", "art_bytes")}))
    result = tagedit.set_artwork(lib, rel_first, b"\xff\xd8\xff" + b"1" * 90,
                                 "", "embed")
    check("a replacement keeps the old picture in the journal",
          result["art"]["mime"] == "image/jpeg"
          and len(json.loads(art_journal.read_text(
              encoding="utf-8").splitlines()[-1])["before_b64"]) > 0,
          str(result.get("art")))
    result = tagedit.set_artwork(lib, rel_first, b"", "", "embed", True)
    check("the embedded picture can be removed",
          result.get("ok") and not result["art"]["embedded"],
          str(result.get("art")))

    result = tagedit.set_artwork(lib, rel_first, png, "image/png", "folder")
    check("a folder cover is written next to the album",
          result.get("ok") and result["art"]["folder"] == "cover.png"
          and (album / "cover.png").is_file(), str(result.get("art")))
    check("the index counts the folder cover",
          int(tagedit.row(lib, rel_first).get("folder_image_count") or 0) == 1
          and "cover.png" in str(
              tagedit.row(lib, rel_first).get("folder_image_names")),
          str({k: tagedit.row(lib, rel_first).get(k)
               for k in ("folder_image_count", "folder_image_names")}))
    result = tagedit.set_artwork(lib, rel_first, b"\xff\xd8\xff" + b"2" * 20,
                                 "image/jpeg", "folder")
    check("an existing folder cover is replaced, not doubled",
          result["art"]["folder"] == "cover.png"
          and len(list(album.glob("cover.*"))) == 1,
          str(result.get("art")))
    result = tagedit.set_artwork(lib, rel_first, b"", "", "folder", True)
    check("the folder cover can be removed",
          result.get("ok") and not result["art"]["folder"],
          str(result.get("art")))
    check("an oversized picture is refused",
          "too large" in tagedit.set_artwork(
              lib, rel_first, b"x" * (tagedit.ART_MAX + 1),
              "image/jpeg")["error"])
    check("a non-image is refused",
          "JPEG or PNG" in tagedit.set_artwork(
              lib, rel_first, b"plain text", "text/plain")["error"])
    check("artwork on an untaggable format is refused",
          not tagedit.set_artwork(lib, "Chiptune/Game/theme.spc", png,
                                  "image/png").get("ok"))
    shutil.rmtree(lib, ignore_errors=True)

def check_smart(work: Path) -> None:
    """Smart playlists: rules, building, the journal and the chat routing."""
    from datetime import datetime, timedelta, timezone

    root = work / "smart-library"
    now = datetime.now(timezone.utc)

    def stamp(days_ago: float) -> str:
        return (now - timedelta(days=days_ago)).isoformat(timespec="seconds")

    rows = [
        {"rel_path": "Artists/Metal/A/01_Old_Thrash.mp3", "title": "Old Thrash",
         "artist": "Eisenwerk", "albumartist": "Eisenwerk", "album": "A",
         "genre": "Metal", "grouping": "Bands", "date": "1994-05-01",
         "duration_s": 240, "bitrate": 320, "mtime": stamp(400)},
        {"rel_path": "Artists/Metal/A/02_Nineties.mp3", "title": "Nineties",
         "artist": "Eisenwerk", "albumartist": "Eisenwerk", "album": "A",
         "genre": "Metal", "grouping": "Bands", "date": "1998",
         "duration_s": 600, "bitrate": 192, "mtime": stamp(5)},
        {"rel_path": "Artists/Metal/A/03_New.mp3", "title": "New Metal",
         "artist": "Eisenwerk", "albumartist": "Eisenwerk", "album": "A",
         "genre": "Metal", "grouping": "Bands", "date": "2020-01-01",
         "duration_s": 200, "bitrate": 128, "mtime": stamp(1)},
        {"rel_path": "Artists/Rock/B/01_Rock.mp3", "title": "Rock Song",
         "artist": "Stein", "albumartist": "Stein", "album": "B",
         "genre": "Rock", "grouping": "Bands", "date": "1990",
         "duration_s": 210, "bitrate": 256, "mtime": stamp(60)},
        {"rel_path": "Artists/Calm/C/01_Calm.mp3", "title": "Calm",
         "artist": "Federleicht", "albumartist": "Federleicht", "album": "C",
         "genre": "Chill", "grouping": "Bands", "date": "2010",
         "duration_s": 300, "bitrate": 192, "mtime": stamp(3)},
    ]
    for row in rows:
        path = root / row["rel_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 32)
    inventory = root / "library-work" / "inventory" / "files.jsonl"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text("".join(json.dumps(row) + "\n" for row in rows),
                         encoding="utf-8")
    (root / "Playlists").mkdir(parents=True, exist_ok=True)

    # -- the rule vocabulary -------------------------------------------
    try:
        smart.normalise({})
        empty_refused = False
    except ValueError:
        empty_refused = True
    check("a smart playlist refuses an empty rule set", empty_refused)
    try:
        smart.normalise({"nonsense": 1})
        unknown_refused = False
    except ValueError:
        unknown_refused = True
    check("an unknown rule is refused, not ignored", unknown_refused)
    decade = smart.normalise({"decade": "90s"})
    check("a decade becomes a year range",
          decade == {"year_from": 1990, "year_to": 1999}, str(decade))
    words = smart.normalise({"sort": "title", "genre": "Metal",
                             "added_days": "30"})
    check("a rule set keeps its numbers",
          words.get("added_days") == 30 and words.get("sort") == "title",
          str(words))

    metal90 = smart.matching(
        rows, {"genre": ["Metal"], "year_from": 1990, "year_to": 1999})
    check("90s metal matches exactly the two nineties tracks",
          [row["title"] for row in metal90] == ["Nineties", "Old Thrash"],
          str([row["title"] for row in metal90]))
    check("describe reads like a sentence",
          "genre Metal" in smart.describe(decade | {"genre": ["Metal"]})
          and "the 1990s" in smart.describe(
              decade | {"genre": ["Metal"]}),
          smart.describe(decade | {"genre": ["Metal"]}))
    fresh = smart.matching(rows, {"added_days": 7})
    check("added_days keeps only what arrived lately",
          {row["title"] for row in fresh} == {"Nineties", "New Metal", "Calm"},
          str([row["title"] for row in fresh]))
    long_ones = smart.matching(rows, {"min_duration": 300})
    check("a length rule filters on the duration",
          {row["title"] for row in long_ones} == {"Nineties", "Calm"},
          str([row["title"] for row in long_ones]))
    axes = {row["rel_path"]: {"aggression": 0.9 if "Metal" in row["rel_path"]
                              else 0.1} for row in rows}
    aggressive = smart.matching(rows, {"axes": {"aggression": 0.8}}, axes)
    check("an axis rule uses the profile database",
          len(aggressive) == 3, str([row["title"] for row in aggressive]))
    check("a track without a profile never satisfies an axis rule",
          smart.matching(rows, {"axes": {"aggression": 0.8}}, {}) == [])

    # -- building ------------------------------------------------------
    dry = smart.build(root, "90s Metal",
                      {"genre": ["Metal"], "decade": "90s"},
                      rows=rows, dry_run=True)
    check("a dry run reports the tracks it would write",
          dry["count"] == 2 and not (root / "Playlists" / "Smart").exists(),
          str(dry)[:200])
    built = smart.build(root, "90s Metal",
                        {"genre": ["Metal"], "decade": "90s"}, rows=rows)
    target = root / built["written"]
    check("the smart list is written under Playlists/Smart",
          built["written"] == "Playlists/Smart/90s_Metal.m3u8"
          and target.is_file(), str(built.get("written")))
    entries = [line for line in target.read_text(encoding="utf-8").splitlines()
               if line and not line.startswith("#")]
    check("the entries are relative and exist",
          len(entries) == 2
          and all((target.parent / line).resolve().is_file()
                  for line in entries), str(entries))
    journal = root / "library-work" / "journal" / "playlists.jsonl"
    recorded = [json.loads(line) for line in
                journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    check("the smart write is journalled",
          any("Smart" in str(row.get("path") or "") for row in recorded),
          str([row.get("path") for row in recorded])[:200])
    saved = smart.definitions(root)
    check("the rules are stored",
          saved.get("90s_Metal", {}).get("rules", {}).get("year_from") == 1990,
          str(saved)[:200])
    check("the stored count matches the file",
          saved["90s_Metal"]["count"] == 2, str(saved["90s_Metal"])[:200])

    # -- the chat plans it ---------------------------------------------
    engine = Engine(root=root, use_llm=False)
    for sentence, expected in (
            ("make a smart playlist of 90s metal", {"action": "build"}),
            ("create a smart playlist of everything I added this month",
             {"action": "build"}),
            ("show my smart playlists", {"action": "list"}),
            ("refresh the smart playlists", {"action": "refresh"}),
            ("delete the smart playlist 90s Metal",
             {"action": "delete", "name": "90s Metal"})):
        planned = nlu_plan(sentence, engine.ctx, engine.catalog)
        ok = (planned is not None and planned.skill == "smart-playlist"
              and all(planned.args.get(key) == value
                      for key, value in expected.items()))
        if sentence == "make a smart playlist of 90s metal":
            rules = planned.args.get("rules") or {} if planned else {}
            ok = ok and rules.get("year_from") == 1990 \
                and rules.get("year_to") == 1999 \
                and rules.get("genre") == ["Metal"]
        check(f"“{sentence}” plans a smart playlist", ok, str(planned)[:250])

    ruled = nlu_plan("make a smart playlist of tracks longer than 8 minutes, "
                     "shuffled", engine.ctx, engine.catalog)
    check("a rule phrase is not mistaken for a list name",
          ruled is not None and ruled.skill == "smart-playlist"
          and ruled.args.get("name") == ""
          and (ruled.args.get("rules") or {}).get("min_duration") == 480,
          str(ruled)[:250])

    reply = engine.handle("make a smart playlist of 90s metal")
    text = clean_text(reply)
    check("the chat builds the list", (root / "Playlists" / "Smart"
                                       / "90s_Metal.m3u8").is_file(),
          text[:250])
    check("the chat names the count", "2 tracks" in text, text[:250])
    kinds = {playlist.name: playlist.kind
             for playlist in engine.catalog.playlists(refresh=True)}
    check("the catalog groups it as a smart list",
          kinds.get("90s_Metal") == "smart", str(kinds))

    # playing a smart list rebuilds it first, so a stale file never plays
    smart_file = root / "Playlists" / "Smart" / "90s_Metal.m3u8"
    smart_file.write_text("#EXTM3U\n", encoding="utf-8")
    quiet = Engine(root=root, use_llm=False)
    quiet.player.play = lambda *a, **k: (True, "stub")     # no real playback
    quiet.player.stop = lambda *a, **k: None
    quiet.handle("play 90s Metal")
    check("playing a smart list rebuilds it first",
          any(line and not line.startswith("#")
              for line in smart_file.read_text(encoding="utf-8").splitlines()),
          smart_file.read_text(encoding="utf-8")[:200])
    quiet.close()

    reply = engine.handle("show the smart playlist 90s Metal")
    check("showing one names its rules",
          "year_from" in clean_text(reply) and "Metal" in clean_text(reply),
          clean_text(reply)[:250])
    reply = engine.handle("refresh the smart playlists")
    check("refresh rebuilds every list",
          "Rebuilt" in clean_text(reply), clean_text(reply)[:250])

    # a changed rule under an existing name is refused without "replace"
    reply = engine.handle("make a smart playlist of 90s metal called 90s Metal")
    check("the same rule under the same name is fine",
          not reply.error, clean_text(reply)[:200])
    blocked = engine.skill_smart({"action": "build", "name": "90s Metal",
                                  "rules": {"genre": ["Rock"]}}, "add rock")
    check("different rules under an existing name need a replace word",
          blocked.error and "already exists" in clean_text(blocked),
          clean_text(blocked)[:200])
    replaced = engine.skill_smart({"action": "build", "name": "90s Metal",
                                   "rules": {"genre": ["Rock"]}},
                                  "replace it with rock")
    check("saying replace changes the rules",
          not replaced.error
          and smart.definitions(root)["90s_Metal"]["rules"]["genre"] == ["Rock"],
          clean_text(replaced)[:200])
    smart.build(root, "90s Metal", {"genre": ["Metal"], "decade": "90s"},
                rows=rows)

    # -- delete needs the word and never touches the music --------------
    refused = engine.skill_smart({"action": "delete", "name": "90s_Metal"},
                                 "remove that list")
    check("a delete without the word is refused",
          refused.error and "delete" in clean_text(refused).lower(),
          clean_text(refused)[:200])
    removed = engine.handle("delete the smart playlist 90s Metal")
    check("a spoken delete removes the list and its file",
          not (root / "Playlists" / "Smart" / "90s_Metal.m3u8").exists()
          and "90s_Metal" not in smart.definitions(root),
          clean_text(removed)[:200])
    check("the music is untouched after a delete",
          all((root / row["rel_path"]).is_file() for row in rows))
    check("the delete is journalled",
          any(row.get("action") == "delete" for row in
              [json.loads(line) for line in
               journal.read_text(encoding="utf-8").splitlines() if line.strip()]),
          "no delete entry")
    engine.close()


def check_stats(work: Path) -> None:
    """Library statistics: the numbers, the slices and the chat routing."""
    from datetime import datetime, timedelta, timezone

    root = work / "stats-library"
    now = datetime.now(timezone.utc)

    def ago(days: float) -> str:
        return (now - timedelta(days=days)).isoformat(timespec="seconds")

    rows = [
        {"rel_path": "Artists/Eisenwerk/A/01.mp3", "title": "Old Thrash",
         "artist": "Eisenwerk", "albumartist": "Eisenwerk", "album": "A",
         "genre": "Metal", "date": "1994-05-01", "duration_s": 100,
         "size_bytes": 5_000_000, "bitrate": 320_000, "ext": ".mp3",
         "grouping": "Bands", "mtime": ago(2)},
        {"rel_path": "Artists/Eisenwerk/A/02.mp3", "title": "Long One",
         "artist": "Eisenwerk", "albumartist": "Eisenwerk", "album": "A",
         "genre": "Metal", "date": "1998", "duration_s": 400,
         "size_bytes": 8_000_000, "bitrate": 192_000, "ext": ".mp3",
         "grouping": "Bands", "mtime": ago(100)},
        {"rel_path": "Artists/Stein/B/01.mp3", "title": "Rock Song",
         "artist": "Stein", "albumartist": "Stein", "album": "B",
         "genre": "Rock", "date": "2005", "duration_s": 200,
         "size_bytes": 6_000_000, "bitrate": 256_000, "ext": ".mp3",
         "grouping": "Bands", "mtime": ago(10)},
        {"rel_path": "Artists/Federleicht/C/01.mp3", "title": "Calm",
         "artist": "Federleicht", "albumartist": "Federleicht", "album": "C",
         "genre": "Chill", "duration_s": 300, "size_bytes": 4_000_000,
         "bitrate": 128_000, "ext": ".mp3", "grouping": "Chill",
         "mtime": ago(400)},
        {"rel_path": "Chiptune/C64/game.spc", "title": "", "ext": ".spc",
         "duration_s": 60, "size_bytes": 64_000, "mtime": ago(1)},
    ]
    for row in rows:
        path = root / row["rel_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    inventory = root / "library-work" / "inventory" / "files.jsonl"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text("".join(json.dumps(row) + "\n" for row in rows),
                         encoding="utf-8")
    for relative in ("All.m3u8", "Moods/Mood_Chill.m3u8",
                     "Moods/Mood_Focus_Deep.m3u8", "Custom/Mine.m3u8",
                     "Smart/Old.m3u8", "Own/Temp.m3u8", "Psycho/Family.m3u8"):
        path = root / "Playlists" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#EXTM3U\n../../Artists/Eisenwerk/A/01.mp3\n",
                        encoding="utf-8")

    data = library_stats.summary(root)
    check("statistics count every inventory row",
          data["tracks"] == 5, str(data["tracks"]))
    check("a chiptune is not counted as taggable",
          data["taggable"] == 4 and data["chiptune"] == 1,
          f'{data["taggable"]} / {data["chiptune"]}')
    check("hours and size are summed",
          data["hours"] == 0.29 and data["size_gb"] == 0.02,
          f'{data["hours"]} / {data["size_gb"]}')
    check("the average bitrate ignores the chiptune",
          data["average_bitrate"] == 224.0, str(data["average_bitrate"]))
    check("a bit/s inventory value becomes kbit/s",
          "224" in library_stats.markdown(data, "quality"),
          library_stats.markdown(data, "quality")[:200])
    check("genres are counted and ordered",
          data["genres"][0] == ["Metal", 2], str(data["genres"]))
    check("decades come from the date tag",
          data["decades"] == [["1990s", 2], ["2000s", 1]],
          str(data["decades"]))
    check("formats are counted",
          dict(data["formats"]) == {".mp3": 4, ".spc": 1},
          str(data["formats"]))
    check("the longest and shortest track are named",
          data["longest"]["title"] == "Long One"
          and data["shortest"]["title"] == "game", str(data["longest"]))
    check("arrivals are counted by file time",
          data["added"]["week"] == 2 and data["added"]["month"] == 3
          and data["added"]["year"] == 4, str(data["added"]))
    check("artist and album counts are distinct",
          data["artists"]["unique"] == 3 and data["albums"]["unique"] == 3,
          f'{data["artists"]} / {data["albums"]}')
    check("tag coverage is a percentage of the taggable rows",
          data["coverage"]["artist"] == 100.0
          and data["coverage"]["artwork"] == 0.0, str(data["coverage"]))
    check("playlists are counted by kind",
          data["playlists"] == {"total": 7, "shelf": 1, "mood": 1,
                                "focus": 1, "custom": 1, "smart": 1,
                                "own": 1, "psycho": 1},
          str(data["playlists"]))
    check("a second read is cached, not a different answer",
          library_stats.summary(root)["tracks"] == 5)

    genres = library_stats.markdown(data, "genres")
    check("the genre slice names the top genre",
          "Metal 2" in genres and "2 %" not in genres, genres[:200])
    decades = library_stats.markdown(data, "decades")
    check("the decade slice lists the ranges",
          "1990s" in decades and "2000s" in decades, decades[:200])
    quality = library_stats.markdown(data, "quality")
    check("the quality slice shows the average bitrate",
          "224" in quality, quality[:200])
    coverage = library_stats.markdown(data, "coverage")
    check("the coverage slice reports the percentages",
          "100.0 %" in coverage and "0.0 %" in coverage, coverage[:200])
    added = library_stats.markdown(data, "added")
    check("the added slice names the newest track",
          "Old Thrash" in added, added[:200])
    length = library_stats.markdown(data, "length")
    check("the length slice names the longest track",
          "Long One" in length, length[:200])
    whole = library_stats.markdown(data)
    check("the whole picture mentions tracks and size",
          "5 tracks" in whole and "GB" in whole, whole[:200])

    engine = Engine(root=root, use_llm=False)
    for sentence, focus in (
            ("which genres do I have most of", "genres"),
            ("wie viele interpreten habe ich", "artists"),
            ("which decades are in my library", "decades"),
            ("what formats do I have", "formats"),
            ("how is the sound quality", "quality"),
            ("what did I add this month", "added"),
            ("how complete is the library", "coverage"),
            ("how many playlists do I have", "playlists"),
            ("library statistics", "")):
        planned = nlu_plan(sentence, engine.ctx, engine.catalog)
        check(f"“{sentence}” asks for the statistics",
              planned is not None and planned.skill == "library-overview"
              and planned.args.get("action") == "stats"
              and planned.args.get("focus") == focus,
              str(planned)[:200])

    counted = nlu_plan("a playlist with 30 tracks of rock", engine.ctx,
                       engine.catalog)
    check("a counted genre phrase is a playlist, not a statistics question",
          counted is not None and counted.skill == "make-playlist"
          and counted.args.get("count") == 30
          and counted.args.get("include_genres") == ["Rock"],
          str(counted)[:200])

    still_plays = nlu_plan("play something I added recently", engine.ctx,
                           engine.catalog)
    check("a playback request stays a playback request",
          still_plays is not None and still_plays.skill != "library-overview",
          str(still_plays)[:200])
    this_track = nlu_plan("which album is this", engine.ctx, engine.catalog)
    check("a question about the current track is not statistics",
          this_track is None or this_track.skill != "library-overview",
          str(this_track)[:200])

    reply = engine.handle("which genres do I have most of")
    text = clean_text(reply)
    check("the chat answers the genre question",
          "Metal" in text and "Genres" in text, text[:250])
    reply = engine.handle("how is the sound quality")
    check("the chat answers the quality question",
          "224" in clean_text(reply), clean_text(reply)[:250])

    service = ChatService(root=root, use_llm=False, dry_run=True,
                          auto_psycho=False)
    payload = service.library_stats()
    check("the window API carries the same numbers",
          payload.get("tracks") == 5
          and payload.get("playlists", {}).get("total") == 7,
          str(payload.get("playlists")))
    service.engine.close()
    engine.close()


def check_profile_page(work: Path) -> None:
    """The Profile page: every learned weight and how it is applied.

    The page under Settings → Profile renders `GET /api/profile`, which is
    `Engine.profile_data()`: the buckets with the bounded bias the selection
    really uses, the notes, learned phrases, recent plays, built playlists and
    the newest events.  Everything here is read-only.
    """
    lib = work / "profile-library"
    if lib.exists():
        shutil.rmtree(lib)
    build_library(lib)
    engine = Engine(root=lib, use_llm=False)
    try:
        metal = engine.catalog.search("Eisenwerk", limit=1)[0]
        calm = engine.catalog.search("Federleicht", limit=1)[0]
        engine.profile.like_track(metal, 1.0)
        engine.profile.dislike_track(calm, 1.0)
        engine.profile.note("prefer instrumental music while working")
        engine.profile.add_mapping("smooth", ["Chill", "Jazz"])
        engine.profile.observe(metal, 0.9, "Workout")
        engine.profile.record_playlist("Workout Mix", {"Workout": 1.0}, 12,
                                       "play something aggressive")
        data = engine.profile_data()

        artists = {row["name"]: row for row in data["weights"]["artists"]}
        check("the profile page lists a liked artist",
              "Eisenwerk" in artists and artists["Eisenwerk"]["weight"] > 0,
              str(artists)[:300])
        check("a weight becomes the bounded bias selection applies",
              abs(artists["Eisenwerk"]["bias"])
              <= abs(artists["Eisenwerk"]["weight"]) + 1e-9
              and artists["Eisenwerk"]["bias"] > 0,
              str(artists.get("Eisenwerk"))[:200])
        check("a disliked artist carries a negative weight",
              "Federleicht" in artists and artists["Federleicht"]["weight"] < 0,
              str(artists.get("Federleicht")))
        check("genres and moods are counted too",
              {row["name"] for row in data["weights"]["genres"]} >= {"Metal"}
              and data["counts"]["moods"] >= 1,
              f'{data["weights"]["genres"]} {data["counts"]}')
        check("positive and negative weights are counted separately",
              data["counts"]["positive"] >= 2 and data["counts"]["negative"] >= 1,
              str(data["counts"]))
        check("a note is listed with its text",
              data["notes"] and "instrumental" in data["notes"][0]["text"],
              str(data["notes"])[:200])
        check("a learned phrase is listed with its moods",
              data["mappings"] and data["mappings"][0]["phrase"] == "smooth"
              and "Chill" in data["mappings"][0]["moods"],
              str(data["mappings"])[:200])
        check("a recent play names the track and how much was heard",
              data["recent"] and "Eisenwerk" in data["recent"][0]["label"]
              and data["recent"][0]["fraction"] == 0.9,
              str(data["recent"][:1])[:250])
        kinds = {row["kind"] for row in data["events"]}
        check("the events say what taught the profile",
              {"like", "dislike", "listened"} <= kinds, str(sorted(kinds)))
        check("a built playlist is listed with its request",
              data["playlists"] and data["playlists"][0]["name"] == "Workout Mix"
              and data["playlists"][0]["count"] == 12,
              str(data["playlists"][:1])[:200])
        check("the page names the file it reads",
              data["rel_path"] == "library-work/app/chat/profile.json",
              str(data.get("rel_path")))
        check("the page says when the profile last changed",
              bool(data["updated_ago"]), str(data.get("updated_ago")))

        service = ChatService(root=lib, use_llm=False, auto_psycho=False)
        try:
            page = service.profile_page()
            check("the window API returns the same page",
                  page["counts"]["artists"] == data["counts"]["artists"]
                  and page["notes"][0]["text"] == data["notes"][0]["text"],
                  str(page["counts"])[:200])
        finally:
            service.engine.close()

        reply = engine.handle("what have you learned")
        check("chat points at the Profile page",
              reply.tool == "profile"
              and "Settings → Profile" in clean_text(reply),
              f"{reply.tool}: {clean_text(reply)[-200:]}")
        planned = nlu_plan("show my profile", engine.ctx, engine.catalog)
        check("“show my profile” opens the same summary",
              planned is not None and planned.skill == "learn-preferences"
              and planned.args.get("kind") == "show", str(planned)[:200])

        app_js = (LIB / "chat" / "webui" / "app.js").read_text(
            encoding="utf-8")
        check("Settings has a Profile tab",
              'tabPanel(\n        "Profile"' in app_js
              or '"Profile",\n        "What the app has learned' in app_js,
              "no Profile tab in openSettings")
        check("the tab reads the read-only profile endpoint",
              'api("/api/profile?limit=100"' in app_js, "no /api/profile call")
        check("the page explains how a weight is used",
              "tanh(weight" in app_js, "no formula shown")
        check("nothing on the page deletes a weight",
              '"/api/profile"' in app_js
              and 'api("/api/profile", {' not in app_js,
              "the page posts instead of reading")
    finally:
        engine.close()


def check_radio(work: Path) -> None:
    """Radio mode: the picks, the state, the queue top-up and the chat."""
    import random as random_mod
    from chat.catalog import Track

    root = work / "radio-library"
    rows = []
    details = [
        ("Artists/Eisenwerk/A/01.mp3", "Seed Song", "Eisenwerk", "Metal", 200),
        ("Artists/Eisenwerk/A/02.mp3", "Same Album", "Eisenwerk", "Metal", 210),
        ("Artists/Stahl/B/01.mp3", "Near One", "Stahl", "Metal", 240),
        ("Artists/Stahl/B/02.mp3", "Near Two", "Stahl", "Metal", 180),
        ("Artists/Stahl/B/03.mp3", "Near Three", "Stahl", "Metal", 220),
        ("Artists/Stahl/B/04.mp3", "Near Four", "Stahl", "Metal", 205),
        ("Artists/Stahl/B/05.mp3", "Near Five", "Stahl", "Metal", 195),
        ("Artists/Fels/C/01.mp3", "Rock One", "Fels", "Rock", 190),
        ("Artists/Fels/C/02.mp3", "Rock Two", "Fels", "Rock", 230),
        ("Artists/Federleicht/D/01.mp3", "Calm One", "Federleicht", "Chill", 260),
        # an interlude is a poor anchor and must never be picked
        ("Artists/Eisenwerk/A/00.mp3", "Intro", "Eisenwerk", "Metal", 8),
    ]
    for rel, title, artist, genre, seconds in details:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        rows.append({"rel_path": rel, "title": title, "artist": artist,
                     "albumartist": artist, "album": rel.split("/")[2],
                     "genre": genre, "grouping": genre, "duration_s": seconds,
                     "size_bytes": 1000, "bitrate": 192000, "ext": ".mp3"})
    # a row whose file is gone must never be queued
    rows.append({"rel_path": "Artists/Gone/E/01.mp3", "title": "Missing",
                 "artist": "Gone", "genre": "Metal", "duration_s": 200,
                 "ext": ".mp3"})
    inventory = root / "library-work" / "inventory" / "files.jsonl"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text("".join(json.dumps(row) + "\n" for row in rows),
                         encoding="utf-8")

    catalog = Catalog(root)
    seed = "Artists/Eisenwerk/A/01.mp3"
    near = ["Artists/Stahl/B/01.mp3", "Artists/Stahl/B/02.mp3",
            "Artists/Stahl/B/03.mp3", "Artists/Stahl/B/04.mp3",
            "Artists/Stahl/B/05.mp3"]

    class FakePsycho:
        def __init__(self, neighbours):
            self.neighbours = neighbours
            self.calls = []

        def similar(self, rel=None, **kwargs):
            self.calls.append(rel)
            return [{"rel": entry, "distance": 0.1 * index}
                    for index, entry in enumerate(self.neighbours)]

    fake = FakePsycho(near)
    picks = radio_mod.candidates(catalog, fake, seed, 4, avoid=[],
                                 rng=random_mod.Random(7))
    check("the radio asks the profile about the seed", fake.calls == [seed],
          str(fake.calls))
    check("a radio batch never repeats the seed", seed not in picks,
          str(picks))
    check("a radio batch never picks a missing file",
          "Artists/Gone/E/01.mp3" not in picks, str(picks))
    check("a radio batch never picks an interlude",
          "Artists/Eisenwerk/A/00.mp3" not in picks, str(picks))
    check("the radio starts from the measured neighbours",
          len(picks) >= 2 and set(picks[:2]) <= set(near), str(picks))
    check("a radio batch respects its size", len(picks) == 4, str(picks))
    avoided = radio_mod.candidates(catalog, fake, seed, 4,
                                   avoid=near + [seed],
                                   rng=random_mod.Random(7))
    check("an avoided track is not picked again",
          not (set(avoided) & set(near)) and seed not in avoided, str(avoided))
    check("a different random seed gives a different batch",
          radio_mod.candidates(catalog, fake, seed, 4, avoid=[],
                               rng=random_mod.Random(99)) != picks, str(picks))
    no_profile = radio_mod.candidates(catalog, FakePsycho([]), seed, 3,
                                      avoid=[], rng=random_mod.Random(3))
    check("without a profile the radio falls back to the genre",
          len(no_profile) == 3 and seed not in no_profile, str(no_profile))

    # -- the state file ---------------------------------------------------
    check("the radio starts off", not radio_mod.active(root))
    radio_mod.start(root, seed, "Metal", "Metal")
    state = radio_mod.load(root)
    check("starting the radio stores the seed",
          state["on"] and state["seed"] == seed and state["genre"] == "Metal",
          str(state))
    radio_mod.remember(root, near)
    check("the picks are remembered",
          set(near) <= set(radio_mod.load(root)["played"]),
          str(radio_mod.load(root)["played"]))
    radio_mod.save(root, {"on": True, "seed": seed,
                          "played": [f"t/{index}" for index in range(200)]})
    check("the played history is trimmed",
          len(radio_mod.load(root)["played"]) == radio_mod.HISTORY,
          str(len(radio_mod.load(root)["played"])))
    check("the info names the seed track",
          radio_mod.info(root, catalog)["seed_label"].startswith("Eisenwerk"),
          str(radio_mod.info(root, catalog)))
    check("stopping the radio turns it off", radio_mod.stop(root))
    check("a second stop is a no-op", not radio_mod.stop(root))
    radio_mod.start(root, seed, "Metal", "Metal")

    # -- the engine: start, top up, stop ----------------------------------
    engine = Engine(root=root, use_llm=False)
    plays: list[tuple[str, bool]] = []

    def fake_play(target, append=False, shuffle=False):
        plays.append((str(target), bool(append)))
        return True, ""

    engine.player.play = fake_play
    engine.player.running = lambda: False
    engine.player.status = lambda: {"idle": True}
    engine.player.queue_from = lambda limit=200: []
    engine.psycho.similar = lambda rel=None, **kwargs: [
        {"rel": entry} for entry in near]
    engine.player.current_file = lambda: str(root / seed)

    reply = engine.handle("start the radio")
    text = clean_text(reply)
    check("the chat starts the radio from the playing song",
          "Radio on" in text and "Up next" in text, text[:250])
    check("the seed is played first and the picks are appended",
          plays and plays[0] == (str(root / seed), False)
          and all(append for _target, append in plays[1:]), str(plays))
    check("the radio is on after the turn", radio_mod.active(root))
    check("the reply lists what comes next",
          "Stahl" in text or "Fels" in text or "Federleicht" in text,
          text[:250])
    check("the queue is remembered as a radio queue",
          (engine._queue_target or {}).get("kind") == "temp"
          and (engine._queue_target or {}).get("entries", [])[0] == seed,
          str(engine._queue_target))
    appended_rels = {Path(target).relative_to(root).as_posix()
                     for target, append in plays[1:] if append}
    check("the played picks went into the history",
          appended_rels and appended_rels <= set(radio_mod.load(root)["played"]),
          str(appended_rels))

    # a queue that still has room is left alone
    plays.clear()
    engine._radio_at = 0.0
    engine._youtube_at = __import__("time").monotonic()
    engine.player.status = lambda: {"idle": False,
                                    "file": str(root / seed),
                                    "playlist_count": 10, "playlist_pos": 1}
    engine.poll()
    check("a long queue is not topped up", not plays, str(plays))

    # the last entry triggers a batch
    engine._radio_at = 0.0
    engine.player.status = lambda: {"idle": False,
                                    "file": str(root / seed),
                                    "playlist_count": 3, "playlist_pos": 2}
    engine.poll()
    check("the queue is topped up at the end",
          plays and all(append for _target, append in plays), str(plays))
    played_after = len(radio_mod.load(root)["played"])

    engine._radio_at = 0.0
    engine.poll()
    check("the top-up is throttled, not repeated every poll",
          len(radio_mod.load(root)["played"]) == played_after,
          str(radio_mod.load(root)["played"]))

    reply = engine.handle("is the radio on")
    check("the chat reports the radio status",
          "Radio on" in clean_text(reply)
          and "picked" in clean_text(reply), clean_text(reply)[:250])
    reply = engine.handle("more radio")
    check("the chat can top the queue up on request",
          "Added" in clean_text(reply), clean_text(reply)[:250])

    # a second 'start' while the seed plays must not restart it
    plays.clear()
    engine.player.status = lambda: {"idle": False,
                                    "file": str(root / seed)}
    engine.handle("start the radio")
    check("starting the radio mid-song only appends",
          plays and all(append for _target, append in plays), str(plays))

    reply = engine.handle("stop the radio")
    check("the chat stops the radio",
          "Radio off" in clean_text(reply) and not radio_mod.active(root),
          clean_text(reply)[:250])
    plays.clear()
    engine._radio_at = 0.0
    engine.player.status = lambda: {"idle": False,
                                    "file": str(root / seed),
                                    "playlist_count": 1, "playlist_pos": 0}
    engine.poll()
    check("a stopped radio does not add anything", not plays, str(plays))

    # -- the wording ------------------------------------------------------
    for sentence, action in (
            ("start the radio", "start"),
            ("radio on", "start"),
            ("mach das radio an", "start"),
            ("start a radio from this song", "start"),
            ("radio based on Die Ärzte", "start"),
            ("stop the radio", "stop"),
            ("turn off the radio", "stop"),
            ("radio aus", "stop"),
            ("is the radio on", "status"),
            ("radio status", "status"),
            ("more radio", "more"),
            ("keep the radio going", "more")):
        planned = nlu_plan(sentence, engine.ctx, engine.catalog)
        check(f"“{sentence}” is radio {action}",
              planned is not None and planned.skill == "radio"
              and planned.args.get("action") == action,
              str(planned)[:200])
    planned = nlu_plan("start radio based on Die Ärzte", engine.ctx,
                       engine.catalog)
    check("the seed is taken from the sentence",
          planned is not None and planned.args.get("target") == "Die Ärzte",
          str(planned)[:200])
    planned = nlu_plan("start a radio from this song", engine.ctx,
                       engine.catalog)
    check("“this song” stays a reference, not a genre",
          planned is not None and planned.args.get("target") == "this song",
          str(planned)[:200])
    for sentence in ("play Radio Ga Ga", "play the radio ga ga song",
                     "play some rock"):
        planned = nlu_plan(sentence, engine.ctx, engine.catalog)
        check(f"“{sentence}” stays playback",
              planned is not None and planned.skill != "radio",
              str(planned)[:200])

    service = ChatService(root=root, use_llm=False, dry_run=True,
                          auto_psycho=False)
    payload = service.radio_state()
    check("the window can read the radio state",
          "on" in payload and "seed" in payload, str(payload))
    answer = service.radio_action("start")
    check("the window can turn the radio on",
          answer.get("ok") and "Radio on" in answer.get("note", ""),
          str(answer)[:250])
    answer = service.radio_action("stop")
    check("the window can turn the radio off",
          answer.get("ok") and "Radio off" in answer.get("note", ""),
          str(answer)[:250])
    answer = service.radio_action("nonsense")
    check("an unknown radio action is refused", bool(answer.get("error")),
          str(answer))
    check("the radio never wrote a playlist",
          not (root / "Playlists" / "Own").exists(),
          "Playlists/Own exists")
    service.engine.close()
    engine.close()


def check_endless(work: Path) -> None:
    """Endless mode, shuffle mode and the float transport formatting.

    Endless grows the queue from the track that is playing *right now* (so the
    batch drifts instead of returning to one seed), and repeats the queue when
    nothing fitting is left.  The transport checks pin the regression that made
    `POST /api/transport` answer 500 with `Unknown format 'd' for float`.
    """
    from chat.catalog import Track

    root = work / "endless-library"
    details = [
        ("Artists/Eisenwerk/A/01.mp3", "Seed Song", "Eisenwerk", "Metal", 200),
        ("Artists/Eisenwerk/A/02.mp3", "Same Album", "Eisenwerk", "Metal", 210),
        ("Artists/Stahl/B/01.mp3", "Near One", "Stahl", "Metal", 240),
        ("Artists/Stahl/B/02.mp3", "Near Two", "Stahl", "Metal", 180),
        ("Artists/Stahl/B/03.mp3", "Near Three", "Stahl", "Metal", 220),
        ("Artists/Fels/C/01.mp3", "Rock One", "Fels", "Rock", 190),
        ("Artists/Fels/C/02.mp3", "Rock Two", "Fels", "Rock", 230),
        ("Artists/Federleicht/D/01.mp3", "Calm One", "Federleicht", "Chill", 260),
        ("Artists/Eisenwerk/A/00.mp3", "Intro", "Eisenwerk", "Metal", 8),
    ]
    rows = []
    for rel, title, artist, genre, seconds in details:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        rows.append({"rel_path": rel, "title": title, "artist": artist,
                     "albumartist": artist, "album": rel.split("/")[2],
                     "genre": genre, "grouping": genre, "duration_s": seconds,
                     "size_bytes": 1000, "bitrate": 192000, "ext": ".mp3"})
    rows.append({"rel_path": "Artists/Gone/E/01.mp3", "title": "Missing",
                 "artist": "Gone", "genre": "Metal", "duration_s": 200,
                 "ext": ".mp3"})
    inventory = root / "library-work" / "inventory" / "files.jsonl"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text("".join(json.dumps(row) + "\n" for row in rows),
                         encoding="utf-8")
    playlists = root / "Playlists"
    playlists.mkdir()
    (playlists / "All.m3u8").write_text(
        "#EXTM3U\n../../Artists/Eisenwerk/A/01.mp3\n"
        "../../Artists/Stahl/B/01.mp3\n", encoding="utf-8")

    catalog = Catalog(root)
    seed = "Artists/Eisenwerk/A/01.mp3"
    near = ["Artists/Stahl/B/01.mp3", "Artists/Stahl/B/02.mp3",
            "Artists/Stahl/B/03.mp3", "Artists/Fels/C/01.mp3",
            "Artists/Fels/C/02.mp3"]

    # -- the state file ---------------------------------------------------
    check("endless starts off", not endless_mod.active(root))
    state = endless_mod.start(root, seed)
    check("starting endless stores the anchor",
          state["on"] and state["last"] == seed and endless_mod.active(root),
          str(state))
    endless_mod.set_last(root, near[0])
    check("the anchor follows the song that just played",
          endless_mod.load(root)["last"] == near[0],
          str(endless_mod.load(root)))
    endless_mod.mark_repeat(root, True)
    check("the repeat fallback is recorded",
          endless_mod.load(root)["repeat"] is True,
          str(endless_mod.load(root)))
    endless_mod.remember(root, near)
    check("picks are remembered",
          set(near) <= set(endless_mod.load(root)["played"]),
          str(endless_mod.load(root)["played"]))
    endless_mod.save(root, {"on": True, "last": seed,
                            "played": [f"t/{index}" for index in range(200)]})
    check("the picked history is trimmed",
          len(endless_mod.load(root)["played"]) == endless_mod.HISTORY,
          str(len(endless_mod.load(root)["played"])))
    info = endless_mod.info(root, catalog)
    check("the info names the anchor track",
          info["last_label"].startswith("Eisenwerk") and info["batch"] > 0,
          str(info))
    check("stopping endless turns it off and clears the repeat",
          endless_mod.stop(root) and not endless_mod.active(root)
          and not endless_mod.load(root)["repeat"],
          str(endless_mod.load(root)))
    check("a second stop is a no-op", not endless_mod.stop(root))

    # a corrupt state file must read as off, not crash
    endless_mod.path(root).write_text("{not json", encoding="utf-8")
    check("a broken state file reads as off",
          not endless_mod.active(root), str(endless_mod.load(root)))

    # -- the engine: start, drift, top up ---------------------------------
    engine = Engine(root=root, use_llm=False)
    plays: list[tuple[str, bool, bool]] = []
    commands: list[tuple[str, object]] = []
    asked: list[str] = []

    def fake_play(target, append=False, shuffle=False):
        plays.append((str(target), bool(append), bool(shuffle)))
        return True, ""

    def fake_command(action, value=None):
        commands.append((action, value))
        return True, ""

    def fake_similar(rel=None, **kwargs):
        asked.append(str(rel))
        return [{"rel": entry} for entry in near]

    engine.player.play = fake_play
    engine.player.command = fake_command
    engine.player.running = lambda: True
    engine.player.status = lambda: {"idle": False, "file": str(root / seed),
                                    "playlist_count": 1, "playlist_pos": 0}
    engine.player.queue_from = lambda limit=200: []
    engine.psycho.similar = fake_similar
    engine.player.current_file = lambda: str(root / seed)

    reply = engine.handle("endless mode")
    text = clean_text(reply)
    check("the chat turns endless mode on",
          "Endless mode on" in text and "Up next" in text, text[:300])
    check("endless is on after the turn", endless_mod.active(root))
    check("the anchor is the track that is playing",
          endless_mod.load(root)["last"] == seed,
          str(endless_mod.load(root)))
    check("ending the batch leaves the playing song alone",
          all(append for _target, append, _shuffle in plays), str(plays))
    check("the queue is remembered as an endless queue",
          (engine._queue_target or {}).get("kind") == "temp"
          and bool((engine._queue_target or {}).get("entries")),
          str(engine._queue_target))
    picks = endless_mod.load(root)["played"]
    check("the picks went into the history",
          bool(picks) and seed not in picks
          and "Artists/Gone/E/01.mp3" not in picks, str(picks))
    check("the profile was asked about the playing song",
          asked and asked[0] == seed, str(asked))
    check("no missing file and no interlude was queued",
          "Artists/Gone/E/01.mp3" not in picks
          and "Artists/Eisenwerk/A/00.mp3" not in picks, str(picks))

    # the queue is long: a poll changes nothing
    plays.clear()
    engine._endless_at = 0.0
    engine.player.status = lambda: {"idle": False, "file": str(root / seed),
                                    "playlist_count": 10, "playlist_pos": 1}
    engine.poll()
    check("a long queue is not topped up", not plays, str(plays))

    # the next batch follows the song that plays NOW, not the old anchor
    plays.clear()
    asked.clear()
    engine._endless_at = 0.0
    engine.player.status = lambda: {"idle": False,
                                    "file": str(root / near[0]),
                                    "playlist_count": 1, "playlist_pos": 0}
    engine.poll()
    check("the top-up follows the song that just played",
          asked and asked[0] == near[0], str(asked))
    check("the anchor moved with the song",
          endless_mod.load(root)["last"] == near[0],
          str(endless_mod.load(root)))
    check("the top-up only appends",
          plays and all(append for _t, append, _s in plays), str(plays))
    picked_after = len(endless_mod.load(root)["played"])
    engine.poll()
    check("the top-up is throttled, not repeated every poll",
          len(endless_mod.load(root)["played"]) == picked_after,
          str(len(endless_mod.load(root)["played"])))

    reply = engine.handle("is endless mode on")
    check("the chat reports the endless status",
          "Endless mode on" in clean_text(reply)
          and "picked" in clean_text(reply), clean_text(reply)[:250])
    plays.clear()
    engine._endless_at = 0.0
    reply = engine.handle("more endless")
    check("the chat can add an endless batch on request",
          "Added" in clean_text(reply), clean_text(reply)[:250])

    # -- nothing fitting: repeat the queue --------------------------------
    original_candidates = radio_mod.candidates
    radio_mod.candidates = lambda *a, **k: []
    try:
        commands.clear()
        endless_mod.mark_repeat(root, False)
        added = engine._endless_extend(near[0])
        check("with nothing fitting no track is added", added == [], str(added))
        check("the queue is set to repeat instead of stopping",
              ("repeat_all", None) in commands
              and endless_mod.load(root)["repeat"] is True,
              f"{commands} {endless_mod.load(root)}")
    finally:
        radio_mod.candidates = original_candidates
    commands.clear()
    added = engine._endless_extend(near[0])
    check("a fitting batch is added again", bool(added), str(added))
    check("the repeat is switched off once something fits",
          ("repeat_off", None) in commands
          and endless_mod.load(root)["repeat"] is False,
          f"{commands} {endless_mod.load(root)}")

    reply = engine.handle("stop endless")
    check("the chat stops endless mode",
          "Endless mode off" in clean_text(reply)
          and not endless_mod.active(root), clean_text(reply)[:250])
    plays.clear()
    engine._endless_at = 0.0
    engine.player.status = lambda: {"idle": False, "file": str(root / seed),
                                    "playlist_count": 1, "playlist_pos": 0}
    engine.poll()
    check("a stopped endless mode adds nothing", not plays, str(plays))

    # -- radio and endless are mutually exclusive -------------------------
    radio_mod.start(root, seed, "Metal", "Metal")
    engine.handle("endless mode")
    check("starting endless stops the radio",
          endless_mod.active(root) and not radio_mod.active(root),
          f"{endless_mod.active(root)} {radio_mod.active(root)}")
    engine.handle("start the radio")
    check("starting the radio stops endless",
          radio_mod.active(root) and not endless_mod.active(root),
          f"{endless_mod.active(root)} {radio_mod.active(root)}")
    radio_mod.stop(root)
    endless_mod.stop(root)

    # -- shuffle mode ----------------------------------------------------
    check("shuffle mode starts off", not engine.shuffle_mode())
    plays.clear()
    engine.skill_play({"target": seed}, "test")
    check("without the mode a play is not shuffled",
          plays and plays[-1][2] is False, str(plays))
    engine.set_shuffle_mode(True)
    check("shuffle mode is stored in the profile",
          engine.shuffle_mode()
          and (root / "library-work/app/chat/profile.json").is_file(),
          str((root / "library-work/app/chat/profile.json")))
    plays.clear()
    engine.skill_play({"target": seed}, "test")
    check("with the mode on a playlist starts shuffled",
          plays and plays[-1][2] is True, str(plays))
    engine.set_shuffle_mode(False)
    plays.clear()
    engine.skill_play({"target": seed, "shuffle": True}, "test")
    check("a one-off shuffled play still works with the mode off",
          plays and plays[-1][2] is True, str(plays))

    commands.clear()
    reply = engine.skill_transport({"action": "shuffle_on"}, "test")
    check("shuffle_on stores the preference",
          engine.shuffle_mode() and ("shuffle_on", None) in commands,
          f"{commands} {clean_text(reply)[:80]}")
    reply = engine.skill_transport({"action": "shuffle_off"}, "test")
    check("shuffle_off clears the preference",
          not engine.shuffle_mode() and ("shuffle_off", None) in commands,
          f"{commands} {clean_text(reply)[:80]}")

    # -- the float transport regression ----------------------------------
    # POST /api/transport sends the value as a float; `{delta:+d}` used to
    # raise ValueError: Unknown format 'd' for object of type 'float'
    commands.clear()
    reply = engine.skill_transport({"action": "seek", "value": 1.5}, "test")
    check("a float seek is answered", not reply.error
          and "Jumped" in reply.text
          and ("seek", 1.5) in commands, f"{reply.text[:120]} {commands}")
    reply = engine.skill_transport({"action": "seek", "value": "abc"}, "test")
    check("a non-numeric seek is refused, not raised",
          reply.error and "not a number" in reply.text, reply.text[:120])
    reply = engine.skill_transport({"action": "seek_abs", "value": 12.5},
                                   "test")
    check("a float absolute seek is answered", not reply.error
          and "0:12" in reply.text, reply.text[:120])
    reply = engine.skill_transport({"action": "seek_abs", "value": "later"},
                                   "test")
    check("a non-numeric position is refused, not raised",
          reply.error, reply.text[:120])
    reply = engine.skill_transport({"action": "speed", "value": 1.25}, "test")
    check("a fractional speed is answered", not reply.error
          and "1.25" in reply.text, reply.text[:120])
    reply = engine.skill_transport({"action": "speed_delta", "value": 0.25},
                                   "test")
    check("a fractional speed change is answered", not reply.error,
          reply.text[:120])
    reply = engine.skill_transport({"action": "volume", "value": 65.4},
                                   "test")
    check("a float volume is rounded in the answer", not reply.error
          and "65%" in reply.text, reply.text[:120])
    reply = engine.skill_transport({"action": "volume", "value": "loud"},
                                   "test")
    check("a non-numeric volume is refused, not raised",
          reply.error, reply.text[:120])
    reply = engine.skill_transport({"action": "volume_delta", "value": -5.5},
                                   "test")
    check("a float volume change is answered",
          not reply.error and "Quieter" in reply.text, reply.text[:120])
    reply = engine.skill_transport({"action": "position", "value": "1.5"},
                                   "test")
    check("a float queue position resolves to an entry",
          not reply.error and "entry 1" in reply.text, reply.text[:120])

    # -- the wording -----------------------------------------------------
    for sentence, action in (
            ("endless mode", "start"),
            ("endless on", "start"),
            ("endlosmodus", "start"),
            ("mach endlos an", "start"),
            ("keep the music going", "start"),
            ("never stop the music", "start"),
            ("stop endless", "stop"),
            ("endless off", "stop"),
            ("endlos aus", "stop"),
            ("is endless mode on", "status"),
            ("more endless", "more"),
            ("endless add more", "more")):
        planned = nlu_plan(sentence, engine.ctx, engine.catalog)
        check(f"“{sentence}” is endless {action}",
              planned is not None and planned.skill == "endless"
              and planned.args.get("action") == action,
              str(planned)[:200])
    for sentence in ("play Endless Love", "play some rock",
                     "start the radio", "play the endless song"):
        planned = nlu_plan(sentence, engine.ctx, engine.catalog)
        check(f"“{sentence}” does not start endless mode",
              planned is None or planned.skill != "endless",
              str(planned)[:200])
    for sentence, action in (("shuffle mode on", "shuffle_on"),
                             ("shuffle mode off", "shuffle_off"),
                             ("always shuffle playlists", "shuffle_on")):
        planned = nlu_plan(sentence, engine.ctx, engine.catalog)
        check(f"“{sentence}” is {action}",
              planned is not None and planned.skill == "transport"
              and planned.args.get("action") == action,
              str(planned)[:200])

    # -- a dry run stores nothing ----------------------------------------
    dry = Engine(root=root, use_llm=False, dry_run=True)
    try:
        dry.player.play = lambda *a, **k: (False, "dry run")
        dry.player.status = lambda: {"idle": True}
        dry.player.queue_from = lambda limit=200: []
        dry.player.current_file = lambda: str(root / seed)
        dry.psycho.similar = lambda rel=None, **k: []
        reply = dry.handle("endless mode")
        check("a dry run says so",
              "dry run" in clean_text(reply).casefold(),
              clean_text(reply)[:250])
        check("a dry run stores no endless state",
              not endless_mod.active(root), str(endless_mod.load(root)))
    finally:
        dry.close()

    # -- the window's service --------------------------------------------
    service = ChatService(root=root, use_llm=False, dry_run=True,
                          auto_psycho=False)
    try:
        service.engine.player.command = fake_command
        payload = service.endless_state()
        check("the window can read the endless state",
              "on" in payload and "last" in payload, str(payload))
        answer = service.endless_action("start")
        check("the window can turn endless on",
              answer.get("ok") and "Endless mode on" in answer.get("note", ""),
              str(answer)[:250])
        answer = service.endless_action("nonsense")
        check("an unknown endless action is refused",
              bool(answer.get("error")), str(answer))
        answer = service.shuffle_action(True)
        check("the window can arm shuffle", answer.get("ok"), str(answer))
        commands.clear()
        result = service.transport("seek", 1.5)
        check("the window transport accepts a float seek",
              result.get("ok") and "Jumped" in result.get("text", ""),
              str(result)[:250])
        check("the window never wrote a playlist",
              not (root / "Playlists" / "Own").exists(),
              "Playlists/Own exists")
    finally:
        service.engine.close()

    # -- an mpv error can name a float argument --------------------------
    class _SilentSocket:
        def sendall(self, data):        # mpv is stubbed: nothing listens
            return None

    client = mpv_client.Mpv()
    client._sock = _SilentSocket()
    client._readline = lambda: {"request_id": 1,
                               "error": "loading failed", "data": None}
    try:
        client.cmd("seek", 12.5, "absolute")
        message = ""
    except mpv_client.MpvError as err:
        message = str(err)
    check("an mpv error reports a float argument",
          "loading failed" in message and "12.5" in message, message)

    engine.close()


def check_library_data(work: Path) -> None:
    """The plain-text backup and an import matched by audio fingerprint.

    A tiny library with two real MP3s (ffmpeg writes them) runs the library's
    own `scripts/library_data.py` from inside the tree, exactly as the app
    does.  The one promise that matters is checked directly: a record finds
    its file again after a rename, because the link is a hash of the audio
    payload and not the name, and a tag write does not move the hash.
    """
    import sqlite3
    import subprocess
    from datetime import date

    from chat import library_job

    source = HERE.parent.parent / "scripts" / "library_data.py"
    check("the library-data tool is present", source.is_file(), str(source))
    if not source.is_file() or shutil.which("ffmpeg") is None:
        return

    lib = work / "datalib"
    (lib / "Playlists").mkdir(parents=True)
    album = lib / "Artists" / "Tonband" / "Album"
    album.mkdir(parents=True)
    (lib / "scripts").mkdir()
    # the tool computes its root from its own location, so a copy inside the
    # fake library works on that library and never on the real one
    shutil.copy2(source, lib / "scripts" / "library_data.py")

    def make(name: str, frequency: int) -> Path:
        path = album / name
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi",
                        "-i", f"sine=frequency={frequency}:duration=1",
                        "-c:a", "libmp3lame", "-b:a", "64k", "-y", str(path)],
                       check=True)
        return path

    first = make("01_Erster.mp3", 440)
    second = make("02_Zweiter.mp3", 660)
    rel_first = first.relative_to(lib).as_posix()
    rel_second = second.relative_to(lib).as_posix()
    inventory = lib / "library-work" / "inventory" / "files.jsonl"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text("".join(
        json.dumps({"rel_path": rel, "ext": ".mp3", "title": f"Stück {index}",
                    "artist": "Tonband", "album": "Album",
                    "duration_s": 1.0}) + "\n"
        for index, rel in enumerate((rel_first, rel_second), 1)),
        encoding="utf-8")

    psycho = lib / "library-work" / "app" / "psycho" / "features.db"
    psycho.parent.mkdir(parents=True)
    conn = sqlite3.connect(psycho)
    conn.executescript("""
        CREATE TABLE audio (rel_path TEXT PRIMARY KEY, size INTEGER,
            mtime TEXT, duration_s REAL, source TEXT, analysed_at TEXT,
            estimated INTEGER, tempo_bpm REAL, energy_db REAL);
        CREATE TABLE profile (rel_path TEXT PRIMARY KEY, speed REAL,
            energy REAL, updated_at TEXT);
        CREATE TABLE profile_set (rel_path TEXT NOT NULL,
            set_name TEXT NOT NULL, axes TEXT NOT NULL,
            estimated INTEGER DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (rel_path, set_name));
        CREATE TABLE family (name TEXT PRIMARY KEY, size INTEGER,
            traits TEXT, playlist TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    for rel, tempo in ((rel_first, 120.0), (rel_second, 90.0)):
        conn.execute("INSERT INTO audio(rel_path, size, source, analysed_at, "
                     "estimated, tempo_bpm, energy_db) VALUES (?,?,?,?,?,?,?)",
                     (rel, 1000, "analysis", "2026-11-22T09:00:00+00:00", 0,
                      tempo, -16.0))
        conn.execute("INSERT INTO profile(rel_path, speed, energy, updated_at)"
                     " VALUES (?,?,?,?)",
                     (rel, 0.6, 0.4, "2026-11-22T09:00:00+00:00"))
        conn.execute("INSERT INTO profile_set(rel_path, set_name, axes, "
                     "estimated, updated_at) VALUES (?,?,?,?,?)",
                     (rel, "psychology", '{"speed": 0.6}', 0,
                      "2026-11-22T09:00:00+00:00"))
    conn.execute("INSERT INTO family(name, size, traits, playlist) VALUES "
                 "(?,?,?,?)",
                 ("Bright", 2, '["speed"]', "Playlists/Psycho/Bright.m3u8"))
    conn.commit()
    conn.close()

    lyrics = lib / "library-work" / "app" / "lyrics" / "lyrics.db"
    lyrics.parent.mkdir(parents=True)
    conn = sqlite3.connect(lyrics)
    conn.executescript("""
        CREATE TABLE track (rel_path TEXT PRIMARY KEY, title TEXT,
            artist TEXT, album TEXT, duration_s REAL, language TEXT,
            model TEXT, backend TEXT, source_size INTEGER, source_mtime TEXT,
            transcribed_at TEXT, line_count INTEGER DEFAULT 0, text TEXT);
        CREATE TABLE line (rel_path TEXT, idx INTEGER, start_s REAL,
            end_s REAL, text TEXT);
        CREATE VIRTUAL TABLE line_fts USING fts5(text, rel_path UNINDEXED,
            idx UNINDEXED, start_s UNINDEXED, end_s UNINDEXED);
    """)
    conn.execute("INSERT INTO track(rel_path, title, artist, duration_s, "
                 "language, model, backend, transcribed_at, line_count, text) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (rel_first, "Erster", "Tonband", 1.0, "de", "tiny", "cpu",
                  "2026-11-22T09:00:00+00:00", 1, "erste zeile"))
    conn.execute("INSERT INTO line(rel_path, idx, start_s, end_s, text) "
                 "VALUES (?,?,?,?,?)", (rel_first, 0, 0.1, 0.8, "erste zeile"))
    conn.execute("INSERT INTO line_fts(text, rel_path, idx, start_s, end_s) "
                 "VALUES (?,?,?,?,?)",
                 ("erste zeile", rel_first, 0, 0.1, 0.8))
    conn.commit()
    conn.close()

    def tool(*args: str, timeout: int = 300):
        return subprocess.run(
            [sys.executable, str(lib / "scripts" / "library_data.py"), *args],
            capture_output=True, text=True, timeout=timeout, cwd=str(lib))

    def fingerprints() -> dict:
        conn = sqlite3.connect(lib / "library-work" / "app" / "library-data"
                               / "fingerprints.db")
        try:
            return dict(conn.execute(
                "SELECT rel_path, audio_fp FROM file").fetchall())
        finally:
            conn.close()

    run = tool("fingerprint", "--workers", "2")
    check("fingerprinting both files succeeds", run.returncode == 0,
          (run.stderr or run.stdout)[-300:])
    data = json.loads(tool("status", "--json").stdout or "{}")
    check("status counts the audio and its fingerprints",
          data.get("audio") == 2 and data.get("fingerprinted") == 2,
          str(data)[:300])
    before = fingerprints()
    plain = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(first), "-map", "0:a:0",
         "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-"],
        capture_output=True, text=True)
    manual = (plain.stdout or "").strip().rsplit("=", 1)[-1]
    check("the fingerprint is the hash of the audio payload",
          bool(manual) and before.get(rel_first) == manual,
          f"{before.get(rel_first)} vs {manual}")

    # a tag write must not move the hash: metadata is not part of the audio
    try:
        from mutagen.id3 import APIC, ID3, TIT2, TPE1
        tags = ID3(first)
        tags.add(TIT2(encoding=3, text="Neuer Titel"))
        tags.add(TPE1(encoding=3, text="Anderer Künstler"))
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="cover",
                      data=b"\xff\xd8\xff\xe0" + b"0" * 4096))
        tags.save(first)
        tagged = True
    except Exception:                      # mutagen missing in this install
        tagged = False
    if tagged:
        os.utime(first, (time.time() + 10, time.time() + 10))
        tool("fingerprint", "--workers", "2")
        check("a tag write does not change the fingerprint",
              fingerprints().get(rel_first) == before.get(rel_first),
              f"{fingerprints().get(rel_first)} vs {before.get(rel_first)}")

    run = tool("export", "--keep", "2")
    export = lib / "LibraryData" / f"library-data-{date.today().isoformat()}.jsonl"
    check("export writes today's plain-text file",
          run.returncode == 0 and export.is_file(),
          (run.stderr or run.stdout)[-300:])
    text = export.read_text(encoding="utf-8")
    head = json.loads(text.splitlines()[0])
    check("the export names its format and counts",
          head.get("format") == "julians-music-library-data"
          and (head.get("counts") or {}).get("tracks") == 2, str(head)[:300])
    record = next((json.loads(row) for row in text.splitlines()
                   if rel_first in row), {})
    check("a track line carries its fingerprint",
          record.get("fp") == before.get(rel_first), str(record)[:300])
    check("no tag text leaks into the export",
          "Neuer Titel" not in text and "Anderer Künstler" not in text)

    # rename the file and lose the databases: the text file must put every
    # record back where the *audio* is now
    renamed = album / "01_Erster_umbenannt.mp3"
    first.rename(renamed)
    rel_renamed = renamed.relative_to(lib).as_posix()
    tool("fingerprint", "--workers", "2")
    conn = sqlite3.connect(psycho)
    for statement in ("DELETE FROM audio", "DELETE FROM profile",
                      "DELETE FROM profile_set", "DELETE FROM family"):
        conn.execute(statement)
    conn.commit()
    conn.close()
    conn = sqlite3.connect(lyrics)
    for statement in ("DELETE FROM track", "DELETE FROM line",
                      "DELETE FROM line_fts"):
        conn.execute(statement)
    conn.commit()
    conn.close()

    preview = json.loads(tool("import", str(export), "--dry-run",
                              "--json").stdout or "{}")
    check("an import matches by fingerprint, not by path",
          preview.get("matched") == 2 and preview.get("by_path") == 0,
          str(preview)[:400])
    run = tool("import", str(export), "--json")
    check("the import runs", run.returncode == 0,
          (run.stderr or run.stdout)[-300:])
    conn = sqlite3.connect(psycho)
    rows = {row[0] for row in conn.execute("SELECT rel_path FROM audio")}
    sets = conn.execute("SELECT COUNT(*) FROM profile_set").fetchone()[0]
    families = conn.execute("SELECT COUNT(*) FROM family").fetchone()[0]
    conn.close()
    check("a renamed file gets its measurements back",
          rel_renamed in rows and rel_first not in rows, str(sorted(rows)))
    check("the profile sets and families come back too",
          sets == 2 and families == 1, f"{sets} sets, {families} families")
    conn = sqlite3.connect(lyrics)
    songs = {row[0] for row in conn.execute("SELECT rel_path FROM track")}
    lines = conn.execute("SELECT COUNT(*) FROM line WHERE rel_path=?",
                         (rel_renamed,)).fetchone()[0]
    conn.close()
    check("the lyric lines follow the file too",
          rel_renamed in songs and lines == 1, f"{sorted(songs)} / {lines}")

    for day in ("2020-01-01", "2020-01-02", "2020-01-03"):
        (lib / "LibraryData" / f"library-data-{day}.jsonl").write_text(
            "{}\n", encoding="utf-8")
    tool("export", "--keep", "2")
    left = sorted(path.name for path in (lib / "LibraryData").glob(
        "library-data-*.jsonl"))
    check("old exports are pruned, the newest kept",
          len(left) == 2 and left[-1] == export.name
          and "library-data-2020-01-01.jsonl" not in left, str(left))

    check("the app finds the tool in the library",
          library_job.available(lib) and not library_job.due(lib))
    counts = library_job.counts(lib)
    check("the job counts the exports without walking audio",
          counts.get("exports") == 2
          and (counts.get("latest") or {}).get("day")
          == date.today().isoformat()
          and (counts.get("fingerprints") or {}).get("known") == 2,
          str(counts)[:400])
    job = library_job.start(lib, keep=2)
    status = library_job.status(lib, str(job.get("id") or ""))
    for _ in range(200):
        if not status.get("active"):
            break
        time.sleep(0.05)
        status = library_job.status(lib, str(job.get("id") or ""))
    check("the backup job finishes",
          status.get("state") == "done" and status.get("percent") == 100.0
          and status.get("returncode") == 0, str(status)[:300])
    check("the job record says what it was",
          status.get("mode") == "backup", str(status)[:200])
    check("a finished backup is not started again",
          library_job.autostart(lib) == {}, str(library_job.due(lib)))

    service = ChatService(root=lib, use_llm=False, auto_psycho=False)
    try:
        rows_work = {row["key"]: row for row in service.work()["jobs"]}
        check("a finished footer stays quiet about the backup",
              "backup" not in rows_work, str(sorted(rows_work)))
        summary = service.state().get("library_data") or {}
        check("the state block carries the backup summary",
              summary.get("available") is True
              and (summary.get("counts") or {}).get("exports") == 2,
              str(summary)[:300])
        # an install whose newest export is not from today gets the chip, so
        # the daily job is one click away instead of invisible
        away = export.with_suffix(".away")
        export.rename(away)
        try:
            due_rows = {row["key"]: row for row in service.work()["jobs"]}
            check("a due backup shows in the footer",
                  due_rows.get("backup", {}).get("can_start") is True
                  and "due" in due_rows.get("backup", {}).get("detail", ""),
                  str(due_rows.get("backup"))[:300])
        finally:
            away.rename(export)
        check("the chip goes away again once today's export is back",
              "backup" not in {row["key"]
                               for row in service.work()["jobs"]})
        preview = service.library_preview(str(export))
        check("the window can check a file before importing",
              preview.get("matched") == 2 and preview.get("by_path") == 0,
              str(preview)[:300])
        started = service.start_library_backup()
        check("the window can start the backup",
              "error" not in started, str(started)[:300])
        for _ in range(200):
            if not ((service.library_update_status().get("active"))):
                break
            time.sleep(0.05)
        bogus = service.control_library("rewind")
        check("an unknown backup action is refused",
              "error" in bogus, str(bogus)[:200])
        check("the default retention is a week",
              service._backup_keep() == 7, str(service._backup_keep()))
    finally:
        service.engine.close()

    dry = Engine(root=lib, use_llm=False, dry_run=True)
    try:
        reply = dry.handle("back up the library data")
        check("the chat knows a backup request",
              dry.ctx.last_skill == "library-data" and "Dry run" in reply.text,
              f"{dry.ctx.last_skill}: {reply.text[:200]}")
        reply = dry.handle("import the library data from "
                           "LibraryData/library-data-2026-11-22.jsonl")
        check("the chat takes an import with its file",
              dry.ctx.last_skill == "library-data"
              and "library-data-2026-11-22.jsonl" in reply.text,
              f"{dry.ctx.last_skill}: {reply.text[:200]}")
        reply = dry.handle("when was the last backup?")
        check("a backup question is a status, not a setting",
              dry.ctx.last_skill == "library-data",
              f"{dry.ctx.last_skill}: {reply.text[:200]}")
        reply = dry.handle("turn off the daily backup")
        check("a setting sentence stays a setting",
              dry.ctx.last_skill == "settings",
              f"{dry.ctx.last_skill}: {reply.text[:200]}")
    finally:
        dry.close()

    plan = setting_registry.parse("keep the last 5 backups")
    check("the retention is a setting",
          bool(plan) and plan.get("key") == "library_backup_keep"
          and plan.get("value") == 5, str(plan))
    plan = setting_registry.parse("turn off the daily backup")
    check("the daily backup can be turned off from chat",
          bool(plan) and plan.get("key") == "library_backup"
          and plan.get("value") is False, str(plan))
    check("a backup request is not mistaken for a setting",
          setting_registry.parse("back up the library data") is None,
          str(setting_registry.parse("back up the library data")))
    # "import it" after a preview names the file the preview reported
    from chat.nlu import Context
    follow = Context(last_library_file="LibraryData/library-data-2026-11-22.jsonl")
    plan2 = nlu_plan("import it", follow, service.engine.catalog)
    check("a bare 'import it' uses the file just checked",
          plan2 is not None and plan2.skill == "library-data"
          and plan2.args.get("action") == "import"
          and plan2.args.get("file") == follow.last_library_file,
          str(plan2))
    plan2 = nlu_plan("import the playlist from ~/Downloads", follow,
                     service.engine.catalog)
    check("a playlist import stays a playlist import",
          plan2 is not None and plan2.skill != "library-data", str(plan2))


def check_http(root: Path) -> None:
    """The server must answer a failing route instead of dropping the socket.

    This is the "Load failed" case: WebKit says exactly that when a fetch gets
    no answer at all, which is what an unguarded exception in a handler used to
    produce.  A 500 with the exception text, a counter and a log line are the
    difference between a mystery and a diagnosis.
    """
    token = "s3cr3t-token-value"
    service = ChatService(root=root, use_llm=False, auto_psycho=False)

    def boom(_message: str) -> dict:
        raise RuntimeError("planner exploded")
    service.chat = boom

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, token))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def post(path: str, payload) -> tuple[int, str]:
        request = urllib.request.Request(
            f"{base}{path}?t={token}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:                  # noqa: BLE001
            return 0, f"{type(exc).__name__}: {exc}"

    def get(path: str) -> dict:
        with urllib.request.urlopen(f"{base}{path}?t={token}", timeout=10) as res:
            return json.loads(res.read().decode("utf-8"))

    try:
        code, body = post("/api/chat", {"message": "hello"})
        check("a failing route answers 500, not a dropped connection",
              code == 500, body[:200])
        data: dict = {}
        try:
            data = json.loads(body)
        except ValueError:
            pass
        check("the 500 carries the exception text",
              "planner exploded" in str(data.get("error")), body[:200])
        check("the 500 is marked as internal",
              data.get("kind") == "internal", body[:200])
        check("the 500 names the endpoint",
              data.get("where") == "/api/chat", body[:200])
        check("the 500 names the log file",
              bool(data.get("log")), body[:200])

        code, body = post("/api/feedback", {"kind": "like"})
        check("the server still answers after a failure", code == 200,
              body[:200])

        # a POST the server refuses must not poison a keep-alive connection
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("POST", "/api/settings?t=wrong",
                           body='{"visualizer": false}',
                           headers={"Content-Type": "application/json",
                                    "X-Chat-Token": "wrong"})
        first = connection.getresponse()
        first.read()
        connection.request("GET", f"/api/status?t={token}",
                           headers={"X-Chat-Token": token})
        second = connection.getresponse()
        second.read()
        check("a refused POST keeps the connection usable",
              first.status == 403 and second.status == 200,
              f"{first.status} then {second.status}")
        connection.close()

        diagnostics = get("/api/diagnostics")
        check("diagnostics counts the failure",
              diagnostics.get("errors") == 1, str(diagnostics)[:300])
        check("diagnostics records what failed",
              (diagnostics.get("last_error") or {}).get("path") == "/api/chat",
              str(diagnostics.get("last_error"))[:300])
        check("diagnostics has uptime, pid and counters",
              diagnostics.get("uptime_s", -1) >= 0
              and diagnostics.get("requests", 0) >= 3
              and diagnostics.get("pid"), str(diagnostics)[:300])
        started_at = float(diagnostics.get("started_at") or 0)
        check("diagnostics names the wall-clock start",
              started_at > 0 and abs(time.time() - started_at
                                     - diagnostics.get("uptime_s", 0)) < 5,
              f"started_at={started_at} uptime={diagnostics.get('uptime_s')}")
        check("diagnostics lists the response codes",
              "500" in (diagnostics.get("codes") or {}),
              str(diagnostics.get("codes"))[:200])

        log_path = Path(diagnostics.get("log") or "")
        logged = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
        check("the failure is written to the error log",
              "planner exploded" in logged, str(log_path))
        check("the error log keeps the traceback",
              "Traceback" in logged, logged[:200])
        check("the error log has no token in it",
              token not in logged and "?t=" not in logged, logged[:200])

        state = get("/api/state")
        check("state carries a compact diagnostics block",
              (state.get("diagnostics") or {}).get("requests", 0) >= 3,
              str(state.get("diagnostics"))[:300])

        # cleaning the log from the window: the content moves to `.1`, the
        # error counters restart, the traffic counters stay
        before = diagnostics.get("requests", 0)
        code, body = post("/api/diagnostics", {"action": "clear"})
        cleared = {}
        try:
            cleared = json.loads(body)
        except ValueError:
            pass
        check("the window can clean up the log",
              code == 200 and cleared.get("ok")
              and cleared.get("bytes", 0) > 0, body[:200])
        kept = log_path.with_name(log_path.name + ".1")
        check("the old log is kept as .1",
              kept.is_file() and "planner exploded" in kept.read_text(
                  encoding="utf-8"), str(kept))
        check("the log itself is empty after the clean-up",
              log_path.is_file() and log_path.stat().st_size == 0,
              str(log_path))
        after = get("/api/diagnostics")
        check("cleaning up drops the error history",
              after.get("errors") == 0 and after.get("last_error") is None,
              str({k: after.get(k) for k in ("errors", "last_error")}))
        check("cleaning up keeps the traffic counters",
              after.get("requests", 0) >= before, str(after)[:200])
        code, body = post("/api/diagnostics", {"action": "clear"})
        check("a second clean-up finds nothing to do",
              code == 200 and json.loads(body).get("cleared") == [], body[:200])
        code, body = post("/api/diagnostics", {"action": "nonsense"})
        check("an unknown diagnostics action is refused",
              code == 400 and "unknown" in body, body[:200])

        ping = get("/api/ping")
        check("ping stays a cheap no-op", ping.get("ok") is True, str(ping)[:200])
    finally:
        httpd.shutdown()
        try:
            service.engine.close()
        except Exception:                         # noqa: BLE001
            pass


#: every request the fake model saw, for the agent checks
MODEL_SEEN: list[dict] = []


def _tool_call(name: str, arguments: dict, call_id: str = "call-1") -> dict:
    return {"choices": [{"message": {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(arguments)}}],
    }}]}


class FakeModel(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint with one behaviour per base path."""

    def log_message(self, *args):                 # keep the test output clean
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):                            # noqa: N802 (http.server)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) or b"{}"
        payload = json.loads(raw.decode("utf-8"))
        mode = self.path.strip("/").split("/")[0]
        messages = payload.get("messages") or []
        answered = any(message.get("role") == "tool" for message in messages)
        MODEL_SEEN.append({"mode": mode, "tools": bool(payload.get("tools")),
                           "messages": messages})

        # a provider that does not understand OpenAI tool calling
        if mode == "plain" and payload.get("tools"):
            self._send(400, {"error": {"message": "tools are not supported"}})
            return
        if mode in {"tools", "delete", "download", "empty"} and not answered:
            wanted = {
                "tools": ("search_library", {"query": "Eisenwerk", "limit": 3}),
                "delete": ("delete_playlist", {"playlist": "Agent Mix"}),
                "download": ("import_download", {}),
                "empty": ("search_library", {"query": "Eisenwerk", "limit": 3}),
            }[mode]
            self._send(200, _tool_call(*wanted))
            return
        if mode == "empty":
            # a provider that ends with an empty content field, twice: the
            # agent nudges once, then reports what the tool really returned
            self._send(200, {"choices": [{"message": {"role": "assistant",
                                                      "content": ""}}]})
            return
        if mode == "plain":
            self._send(200, {"choices": [{"message": {
                "role": "assistant",
                "content": json.dumps({"skill": "search-library",
                                       "args": {"query": "Eisenwerk"}})}}]})
            return
        text = {
            "tools": "The Eisenwerk shelf holds the tracks the tool returned.",
            "delete": "I did not delete anything.",
            "download": "I did not download anything.",
            "final": "Just a plain answer.",
        }.get(mode, "ok")
        self._send(200, {"choices": [{"message": {"role": "assistant",
                                                  "content": text}}]})


def _engine_with_model(root: Path, port: int, mode: str) -> "Engine":
    config = root / "library-work" / "app" / "llm.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({
        "base_url": f"http://127.0.0.1:{port}/{mode}",
        "api_key": "test-key",
        "model": "fake-model",
    }), encoding="utf-8")
    return Engine(root=root, use_llm=True)


def check_agent(root: Path) -> None:
    """The planner is an agent, but a fenced one.

    A fake OpenAI-compatible endpoint asks for a tool, gets the real catalogue
    answer fed back and only then replies.  The checks cover the tool loop, the
    JSON fallback for a provider without tool support, and the two request
    guards: no playlist deletion and no import unless the listener asked.
    """
    port_holder: dict = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port_holder["port"] = httpd.server_address[1]
    port = port_holder["port"]

    config = root / "library-work" / "app" / "llm.json"
    saved_env = os.environ.pop("MUSIC_LLM_CONFIG", None)
    engines = []
    custom = root / "Playlists" / "Custom"
    custom.mkdir(parents=True, exist_ok=True)
    (custom / "Agent_Mix.m3u8").write_text(
        "#EXTM3U\n../../Artists/Eisenwerk/Album/01_Track_1.mp3\n",
        encoding="utf-8")
    jobs_folder = root / "library-work" / "app" / "chat" / "youtube"
    jobs_before = {path.name for path in jobs_folder.glob("*.json")} \
        if jobs_folder.is_dir() else set()

    try:
        # -- the tool loop -------------------------------------------------
        agent_engine = _engine_with_model(root, port, "tools")
        engines.append(agent_engine)
        MODEL_SEEN.clear()
        reply = agent_engine.handle("please inspect the Eisenwerk shelf for me")
        text = clean_text(reply)
        check("the llm agent answers through its tools", "Eisenwerk" in text,
              text[:200])
        check("the reply names the tool it used", "search_library" in reply.tool,
              reply.tool)
        check("the agent is offered the tools",
              bool(MODEL_SEEN) and MODEL_SEEN[0]["tools"] is True,
              str(MODEL_SEEN[:1])[:200])
        check("the agent gets the real catalogue answer back",
              any("Eisenwerk/Album" in json.dumps(entry["messages"])
                  for entry in MODEL_SEEN), str(MODEL_SEEN)[:200])

        specs = {spec["function"]["name"]
                 for spec in Toolbox(agent_engine).specs()}
        check("the agent can search the database", "search_library" in specs,
              str(sorted(specs)))
        check("the agent can rank by measured sound", "rank_library" in specs,
              str(sorted(specs)))
        check("the agent cannot delete a music file",
              not any("delete" in name and "playlist" not in name
                      for name in specs), str(sorted(specs)))
        check("the agent can delete a custom playlist",
              "delete_playlist" in specs, str(sorted(specs)))

        # the ranking tool reads the profile database, not the tags
        box = Toolbox(agent_engine, "find the slowest and boring song")
        ranked = box.call("rank_library", {"properties": ["slow", "boring"],
                                           "limit": 2})
        check("the ranking tool uses the profile database",
              ranked.get("tracks")
              and ranked["tracks"][0]["score"] > 0.5,
              str(ranked)[:200])
        check("an unknown tool is refused",
              box.call("delete_file", {"rel": "x"}).get("error", "")
              .startswith("unknown tool"), str(box.last_error)[:200])

        # -- deletion and download are request-guarded ---------------------
        delete_engine = _engine_with_model(root, port, "delete")
        engines.append(delete_engine)
        MODEL_SEEN.clear()
        reply = delete_engine.handle("please clean up my custom lists")
        check("the agent cannot delete a playlist nobody asked to delete",
              (custom / "Agent_Mix.m3u8").is_file(), clean_text(reply)[:200])
        check("the refusal is fed back to the model",
              any("did not ask to delete" in json.dumps(entry["messages"])
                  for entry in MODEL_SEEN), str(MODEL_SEEN[-1:])[:300])

        reply = delete_engine.handle("delete the playlist Agent Mix")
        check("deleting a custom playlist works offline",
              "Deleted" in clean_text(reply), clean_text(reply)[:200])
        check("the playlist file is gone",
              not (custom / "Agent_Mix.m3u8").exists())
        journal = root / "library-work" / "journal" / "playlists.jsonl"
        check("the deletion is journalled",
              journal.is_file()
              and "Agent_Mix.m3u8" in journal.read_text(encoding="utf-8"),
              str(journal))

        download_engine = _engine_with_model(root, port, "download")
        engines.append(download_engine)
        MODEL_SEEN.clear()
        reply = download_engine.handle("please have a look at my custom lists")
        check("the download tool is absent without a module",
              any("unknown tool" in json.dumps(entry["messages"]).lower()
                  for entry in MODEL_SEEN), clean_text(reply)[:200])
        jobs_after = {path.name for path in jobs_folder.glob("*.json")} \
            if jobs_folder.is_dir() else set()
        check("no download was started", jobs_after == jobs_before,
              f"{sorted(jobs_after - jobs_before)}")

        # -- an empty final message still answers from the tool result -----
        empty_engine = _engine_with_model(root, port, "empty")
        engines.append(empty_engine)
        reply = empty_engine.handle("please inspect the Eisenwerk shelf for me")
        text = clean_text(reply)
        check("an empty final answer reports the tool result",
              "Eisenwerk" in text and text.strip() != "Done.", text[:300])
        check("the report names the tracks the tool found",
              "search library" in text.casefold() or "tracks" in text.casefold(),
              text[:300])

        # -- a provider without tool support degrades to the JSON planner ---
        plain_engine = _engine_with_model(root, port, "plain")
        engines.append(plain_engine)
        MODEL_SEEN.clear()
        reply = plain_engine.handle("please inspect the Eisenwerk shelf for me")
        text = clean_text(reply)
        check("tool support is detected once and remembered",
              plain_engine.llm.tools_unsupported is True,
              str(plain_engine.llm.last_error)[:200])
        check("the JSON plan still answers", "Eisenwerk" in text, text[:200])
        check("the retry dropped the tools field",
              [entry["tools"] for entry in MODEL_SEEN] == [True, False],
              str([entry["tools"] for entry in MODEL_SEEN]))

        # -- a plain model answer is used as it is --------------------------
        final_engine = _engine_with_model(root, port, "final")
        engines.append(final_engine)
        reply = final_engine.handle("please inspect the Eisenwerk shelf for me")
        check("a plain model answer becomes the reply",
              clean_text(reply) == "Just a plain answer.", clean_text(reply)[:200])
    finally:
        for engine in engines:
            try:
                engine.close()
            except Exception:                     # noqa: BLE001
                pass
        try:
            config.unlink()
        except OSError:
            pass
        if saved_env is not None:
            os.environ["MUSIC_LLM_CONFIG"] = saved_env
        httpd.shutdown()


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="chat-tests-",
                                 dir="/tmp/opencode"))
    root = work / "library"
    try:
        build_library(root)
        engine = Engine(root=root, use_llm=False)

        # -- recommendation ------------------------------------------------
        reply = engine.handle("what do you recommend for aggressive music?")
        text = clean_text(reply)
        rec = engine.ctx.last_recommendation or {}
        check("recommend answers", "Recommended" in text, text[:200])
        check("recommend keeps tracks", bool(rec.get("tracks")),
              "no last_recommendation")
        metal = sum(1 for rel in rec.get("tracks") or []
                    if "Eisenwerk" in rel)
        check("aggressive leans metal", metal >= len(rec.get("tracks") or []) * 0.5,
              f"{metal} of {len(rec.get('tracks') or [])}")
        check("recommend does not write", not
              (root / "Playlists" / "Custom").exists())

        # a genre-only request still produces a blend
        reply = engine.handle("recommend some jazz")
        check("genre recommendation answers",
              "Recommended" in clean_text(reply), clean_text(reply)[:200])

        # -- save the recommendation ---------------------------------------
        reply = engine.handle("save that playlist as Test Aggro")
        saved = root / "Playlists" / "Custom" / "Test_Aggro.m3u8"
        check("save writes the playlist", saved.is_file(),
              clean_text(reply)[:200])
        check("save answers with the name", "Test_Aggro" in clean_text(reply))
        journal = root / "library-work" / "journal" / "playlists.jsonl"
        check("save is journalled", journal.is_file(), str(journal))
        entries = [line for line in saved.read_text(encoding="utf-8")
                   .splitlines() if line and not line.startswith("#")]
        check("saved playlist has entries", len(entries) > 0)
        check("saved entries are relative",
              all(not line.startswith("/") for line in entries), str(entries[:2]))
        check("saved entries exist",
              all((saved.parent / line).resolve().is_file() for line in entries))

        reply = engine.handle("save as Freitag")
        friday = root / "Playlists" / "Custom" / "Freitag.m3u8"
        check("save as <name> works", friday.is_file(), clean_text(reply)[:200])

        # -- show playlists ------------------------------------------------
        reply = engine.handle("show my playlists")
        text = clean_text(reply)
        check("show-playlists lists the custom list", "Test_Aggro" in text,
              text[:300])
        check("show-playlists lists the mood", "Mood_Chill" in text, text[:300])
        check("show-playlists names the count", "playlists" in text.lower())

        # -- activity ------------------------------------------------------
        reply = engine.handle("what did I do today?")
        text = clean_text(reply)
        check("activity shows the playlist", "Playlists made" in text
              and "Test Aggro" in text, text[:300])
        data = engine.activity_data(20)
        check("activity data has playlists", bool(data["playlists"]))
        check("activity data has scope",
              "recent" in data and "events" in data and "profile" in data)

        # -- learned phrase ------------------------------------------------
        learned_dir = root / "library-work" / "app" / "chat" / "agent" / "skills"
        learned_dir.mkdir(parents=True, exist_ok=True)
        (learned_dir / "sammlung.json").write_text(json.dumps({
            "name": "Meine Sammlung",
            "skill": "show-playlists",
            "args": {},
            "triggers": ["meine sammlung", "my collection"],
        }), encoding="utf-8")
        reply = engine.handle("meine sammlung")
        check("learned trigger plans", "Test_Aggro" in clean_text(reply),
              clean_text(reply)[:200])

        # -- agent fallback ------------------------------------------------
        reply = engine.handle("please transmogrify the flanger cabinet")
        text = clean_text(reply)
        requests = root / "library-work" / "app" / "chat" / "agent" / "requests.jsonl"
        check("unknown request is recorded", "Recorded as a skill request" in text,
              text[:300])
        check("agent journal exists", requests.is_file())
        check("agent journal has one line",
              len([line for line in requests.read_text(encoding="utf-8")
                   .splitlines() if line.strip()]) == 1)
        status = engine.agent.summary_markdown()
        check("agent summary counts the request", "1 recorded request" in status,
              status[:200])

        # -- settings and waveform ----------------------------------------
        service = ChatService(root=root, use_llm=False)
        settings = service.settings({"visualizer": False})
        check("settings turn the visualizer off",
              settings["visualizer"] is False, str(settings))
        check("settings persist",
              service.state()["settings"]["visualizer"] is False)
        waves = service.waveform("Artists/Tone/test.wav")
        check("waveform has peaks", len(waves.get("peaks") or []) == 180,
              str(waves)[:200])
        check("waveform no error", not waves.get("error"), str(waves)[:200])
        cached = service.waveform("Artists/Tone/test.wav")
        check("waveform is cached", cached.get("cached") is True)
        bad = service.waveform("../../etc/passwd")
        check("waveform refuses outside paths",
              not bad.get("peaks") and bool(bad.get("error")), str(bad)[:200])

        # -- the spectrum (a real FFT of the same file) --------------------
        spec = service.spectrum("Artists/Tone/test.wav")
        check("spectrum has frames and bands",
              bool(spec.get("frames")) and spec.get("bands") == 24,
              str(spec)[:200])
        check("spectrum carries its hop and edges",
              spec.get("hop") and len(spec.get("edges") or []) == 25,
              str(spec)[:200])
        row = (spec.get("frames") or [[]])[0]
        check("spectrum values are 0..100",
              all(isinstance(value, int) and 0 <= value <= 100 for value in row),
              str(row)[:120])
        strongest = max(range(len(row)), key=lambda index: row[index])
        check("spectrum finds the tone's band",
              bool(spec.get("edges")) and 0 < strongest < len(row),
              f"band {strongest} of {len(row)}")
        again = service.spectrum("Artists/Tone/test.wav")
        check("spectrum is cached",
              again.get("cached") is True
              and again.get("frames") == spec.get("frames"))
        refused = service.spectrum("../../etc/passwd")
        check("spectrum refuses outside paths",
              not refused.get("frames") and bool(refused.get("error")),
              str(refused)[:200])
        activity = service.activity(10)
        check("service activity works", "playlists" in activity)

        # -- visualizer styles --------------------------------------------
        chosen = service.engine.handle("/viz vu")
        check("/viz picks a style", "VU meters" in clean_text(chosen),
              clean_text(chosen)[:200])
        listing = clean_text(service.engine.handle("/viz"))
        check("/viz lists every style",
              all(f"`{name}`" in listing for name in
                  ("wave", "eq", "leds", "scope", "vu", "spectrum", "axes")),
              listing[:400])
        offered = service.state()["settings"]
        check("state offers every style",
              offered.get("viz_styles") == ["wave", "eq", "leds", "scope", "vu",
                                            "spectrum", "axes"],
              str(offered)[:300])
        check("the chosen style survives a read",
              offered.get("viz_style") == "vu", str(offered)[:300])
        changed = service.settings({"viz_style": "leds"})
        check("a style change sticks",
              changed.get("viz_style") == "leds"
              and service.state()["settings"]["viz_style"] == "leds",
              str(changed)[:200])
        bogus = service.settings({"viz_style": "sparkles"})
        check("an unknown style is refused and the old one stays",
              bogus.get("viz_style") == "leds"
              and bool(bogus.get("errors")), str(bogus)[:300])
        service.engine.profile.set_setting("viz_style", "sparkles")
        check("a stale stored style falls back to bars on read",
              service.state()["settings"]["viz_style"] == "wave",
              str(service.state()["settings"].get("viz_style")))
        service.settings({"viz_style": "leds"})
        service.settings({"visualizer": True})

        # -- the setting registry ----------------------------------------
        rows = service.settings_view()["rows"]
        keys = [row["key"] for row in rows]
        check("the panel lists every setting",
              {"library", "visualizer", "voice_enabled", "llm_provider",
               "llm_api_key", "llm_model"} <= set(keys), str(keys))
        check("a secret is never sent to the page",
              all((not row["secret"]) or row["value"] == "" for row in rows),
              str([row for row in rows if row["secret"]])[:300])
        check("the panel knows which secret is stored",
              any(row["key"] == "llm_api_key" for row in rows)
              and all("has_value" in row for row in rows))

        # chat: list, show, set, reset, and a refusal
        reply = engine.handle("show the settings")
        text = clean_text(reply)
        check("chat lists the settings",
              "Voice input" in text and "Language model provider" in text,
              text[:300])
        reply = engine.handle("voice settings")
        text = clean_text(reply)
        check("a group can be listed alone",
              "Speech" in text or "Voice" in text, text[:200])
        reply = engine.handle("is the visualizer on?")
        check("chat shows one setting",
              "Visualizer is **on**" in reply.text, reply.text[:200])
        reply = engine.handle("turn off the voice input")
        check("chat switches a setting off",
              engine.profile.setting("voice_enabled", True) is False,
              reply.text[:200])
        reply = engine.handle("enable the microphone")
        check("chat switches it back on",
              engine.profile.setting("voice_enabled", True) is True,
              reply.text[:200])
        reply = engine.handle("set the default count to 60")
        check("chat sets a number",
              engine.profile.setting("default_count", 40) == 60,
              reply.text[:200])
        reply = engine.handle("set the default count to 9999")
        check("a number is clamped to its range",
              engine.profile.setting("default_count", 40) == 300,
              reply.text[:200])
        reply = engine.handle("reset the default count")
        check("reset restores the default",
              engine.profile.setting("default_count", 40) == 40,
              reply.text[:200])
        refused = engine.skill_settings(
            {"action": "set", "key": "flux_capacitor", "value": 12}, "")
        check("an unknown setting is refused with the known ones",
              refused.error and "visualizer" in refused.text, refused.text[:300])

        # the planner switch is a setting like any other
        engine.handle("use the offline parser")
        check("the offline parser can be forced", engine.llm.enabled is False)
        engine.handle("enable the language model")
        check("and the planner turned back on", engine.llm.enabled is True)

        from chat.skills import ORDER as SKILL_ORDER, REGISTRY as SKILLS
        check("the settings skill is registered",
              "settings" in SKILLS and "settings" in SKILL_ORDER)

        # -- the window API: masked secret, provider resolution -----------
        written = service.settings({"llm_api_key": "sk-test-1234567890"})
        check("a key is stored and only reported as set",
              written.get("llm_api_key_set") is True
              and "llm_api_key" not in written, str(written)[:300])
        secrets_file = root / "library-work" / "app" / "chat" / "secrets.json"
        mode = (secrets_file.stat().st_mode & 0o777) if secrets_file.is_file() else 0
        check("the key lives in its own 0600 file", mode == 0o600,
              f"{secrets_file} mode {oct(mode)}")
        profile_text = (root / "library-work" / "app" / "chat" /
                        "profile.json").read_text(encoding="utf-8")
        check("the key is not in the profile", "sk-test" not in profile_text)
        row = next(row for row in service.settings_view()["rows"]
                   if row["key"] == "llm_api_key")
        check("a stored secret is only flagged",
              row["value"] == "" and row["has_value"] is True, str(row))
        service.settings({"llm_api_key": ""})
        check("an empty field keeps the stored key",
              service.state()["settings"]["llm_api_key_set"] is True)
        check("the state never carries the key",
              "llm_api_key" not in service.state()["settings"])

        service.settings({"llm_provider": "gwdg"})
        llm = service.engine.llm
        check("gwdg resolves to the academic cloud",
              llm.config.get("base_url")
              == "https://chat-ai.academiccloud.de/v1",
              str(llm.config))
        info = service.state()["settings"]["llm"]
        check("the state names the provider and the key",
              info["provider"] == "gwdg" and info["key_set"] is True,
              str(info)[:300])
        service.settings({"reset": ["llm_api_key"]})
        service.settings({"llm_provider": "ollama"})
        llm = service.engine.llm
        check("ollama resolves to the local server",
              llm.config.get("base_url") == "http://127.0.0.1:11434/v1",
              str(llm.config))
        check("ollama gets its placeholder key",
              llm.config.get("api_key") == "ollama", str(llm.config))
        check("switching the provider clears an old address",
              service.engine.profile.setting("llm_base_url", "") == "")
        service.settings({"llm_provider": "custom",
                          "llm_base_url": "http://127.0.0.1:9/v1"})
        check("a custom address is used as given",
              service.engine.llm.config.get("base_url")
              == "http://127.0.0.1:9/v1"
              and service.engine.llm.config.get("provider") == "custom",
              str(service.engine.llm.config))

        # the model list comes from /models, or from Ollama's /api/tags
        class ModelServer(BaseHTTPRequestHandler):
            mine: list[str] = []
            tags: list[str] = []

            def log_message(self, *args):
                pass

            def do_GET(self):                     # noqa: N802
                if self.path.endswith("/api/tags"):
                    body = {"models": [{"name": name}
                                       for name in self.tags]}
                else:
                    body = {"data": [{"id": name}
                                     for name in self.mine]}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        service.settings({"llm_api_key": "sk-local-test-123456"})
        server = ThreadingHTTPServer(("127.0.0.1", 0), ModelServer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        ModelServer.mine = ["gwdg-model-a", "gwdg-model-b"]
        service.settings({"llm_provider": "custom",
                          "llm_base_url": f"http://127.0.0.1:{port}/v1"})
        models = service.engine.llm.models(force=True)
        check("the model list comes from /models",
              models == ["gwdg-model-a", "gwdg-model-b"], str(models))
        ModelServer.mine = []
        ModelServer.tags = ["llama3.1:8b", "qwen2.5:7b"]
        service.engine.llm.reconfigure()
        models = service.engine.llm.models(force=True)
        check("ollama's /api/tags is the fallback",
              models == ["llama3.1:8b", "qwen2.5:7b"], str(models))
        server.shutdown()
        server.server_close()
        service.settings({"llm_provider": "auto"})

        # -- voice input ---------------------------------------------------
        status = service.voice_status()
        check("voice status reports what a clip may be",
              status.get("max_bytes", 0) > 0 and status.get("types"),
              str(status)[:300])
        no_backend = service.voice(b"RIFF-not-audio", "audio/webm")
        check("voice without a transcriber fails honestly",
              no_backend.get("ok") is False
              and ("no local transcriber" in str(no_backend.get("error"))
                   or "no scripts/transcribe_lyrics.py"
                   in str(no_backend.get("error"))),
              str(no_backend)[:300])

        # a fake interpreter + script: the clip must not survive the call
        scripts = root / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "transcribe_lyrics.py").write_text(
            "#!/usr/bin/env python3\n", encoding="utf-8")
        fake = scripts / "asr-venv" / "bin" / "python"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ]; then exit 0; fi\n'
            "printf '%s\\n' '{\"text\": \"spiel etwas ruhiges\", "
            "\"language\": \"de\", \"model\": \"small\", "
            "\"backend\": \"fake\", \"seconds\": 0.2}'\n",
            encoding="utf-8")
        fake.chmod(0o755)
        lyrics_job.forget_backend(root)
        clips = root / "library-work" / "app" / "chat" / "voice"
        answer = service.voice(b"RIFF-fake-clip", "audio/webm;codecs=opus")
        check("a clip is transcribed locally",
              answer.get("ok") is True
              and answer.get("text") == "spiel etwas ruhiges",
              str(answer)[:300])
        check("the transcriber saw the spoken language",
              answer.get("language") == "de", str(answer)[:200])
        check("the clip is deleted after transcription",
              not clips.is_dir() or not list(clips.iterdir()),
              str(list(clips.iterdir()) if clips.is_dir() else []))

        # Whisper invents a caption on wordless audio; that is not a command
        import chat.voice as voice_module
        check("a known artefact is not a request",
              voice_module._noise("Thanks for watching!")
              and voice_module._noise("Untertitel der Amara.org-Community")
              and voice_module._noise("...")
              and not voice_module._noise("spiel etwas Ruhiges")
              and not voice_module._noise("danke"))
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ]; then exit 0; fi\n'
            "printf '%s\\n' '{\"text\": \"Thanks for watching!\"}'\n",
            encoding="utf-8")
        fake.chmod(0o755)
        noise = service.voice(b"RIFF-fake-clip", "audio/webm")
        check("an artefact answer becomes an honest silence",
              noise.get("ok") is False
              and "heard nothing" in str(noise.get("error")),
              str(noise)[:200])
        check("the voice summary stays secret-free",
              "llm_api_key" not in json.dumps(service._voice_summary()))
        shutil.rmtree(scripts, ignore_errors=True)
        lyrics_job.forget_backend(root)

        # -- the music folder is deferred to the service -------------------
        engine.deferred.clear()
        reply = engine.handle(f"change the music folder to {root}")
        check("the engine only defers the folder change",
              engine.deferred.get("library") == str(root)
              and Path(engine.root) == root, reply.text[:200])
        answer = service.chat(f"change the music folder to {root}")
        result = answer.get("library_result") or {}
        check("the window applies the folder change",
              result.get("ok") is True, str(result)[:300])
        check("the reply says what the folder is",
              "Music folder" in answer.get("text", ""), answer.get("text")[:200])
        bad = service.chat("change the music folder to /nope/not/a/library")
        check("a folder the app cannot read is refused",
              (bad.get("library_result") or {}).get("error")
              and bad.get("error") is True, str(bad)[:300])

        # leave no planner setting behind: it lives in profile.json and the
        # later planner tests install their own server
        service.settings({"reset": ["llm_provider", "llm_base_url",
                                    "llm_model", "llm_api_key"]})
        check("the planner settings can be reset",
              service.state()["settings"]["llm_api_key_set"] is False
              and not str(service.engine.llm.config.get("source") or "")
              .startswith("settings"),
              str(service.engine.llm.config))

        # -- newer planner controls ---------------------------------------
        from chat.nlu import Context as NLUContext, plan as nlu_plan

        def planned(text):
            return nlu_plan(text, NLUContext(), engine.catalog)

        wanted = [
            ("restart the song", {"action": "seek_abs", "value": 0}),
            ("seek to 1:30", {"action": "seek_abs", "value": 90}),
            ("rewind 30 seconds", {"action": "seek", "value": -30}),
            ("forward 2 minutes", {"action": "seek", "value": 120}),
            ("mute", {"action": "mute"}),
            ("unmute", {"action": "unmute"}),
            ("play at 1.5x", {"action": "speed", "value": 1.5}),
            ("half speed", {"action": "speed", "value": 0.5}),
            ("shuffle on", {"action": "shuffle_on"}),
            ("turn off shuffle", {"action": "shuffle_off"}),
            ("repeat one", {"action": "repeat_one"}),
            ("repeat off", {"action": "repeat_off"}),
            ("play entry 3 in the queue", {"action": "position", "value": 3}),
            ("louder", {"action": "volume_delta", "value": 10}),
            ("quieter", {"action": "volume_delta", "value": -10}),
        ]
        missed = []
        for text, expected in wanted:
            got = planned(text)
            if (got is None or got.skill != "transport"
                    or any(got.args.get(key) != value
                           for key, value in expected.items())):
                missed.append(f"{text!r} -> {got.skill if got else None} "
                              f"{got.args if got else ''}")
        check("planner knows the newer transport controls", not missed,
              "; ".join(missed)[:600])

        spotify_plan = planned(
            "import this spotify playlist "
            "https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U?si=x")
        check("a Spotify link plans an import",
              spotify_plan and spotify_plan.skill == "import-playlist"
              and spotify_plan.args.get("spotify", "").endswith(
                  "37i9dQZF1DWXRqgorJj26U"),
              str(spotify_plan.args if spotify_plan else None))
        folder_plan = planned(f"import all playlists from {root}/imported")
        check("a folder plans an import",
              folder_plan and folder_plan.skill == "import-playlist"
              and folder_plan.args.get("folder") == f"{root}/imported",
              str(folder_plan.args if folder_plan else None))

        # -- importing a folder of playlists -------------------------------
        from playlist_import import import_folder

        source = root / "imported"
        (source / "nested").mkdir(parents=True)
        one = "Artists/Eisenwerk/Album/01_Track_1.mp3"
        two = "Artists/Federleicht/Album/02_Track_2.mp3"
        (source / "Two_Lists.m3u8").write_text(
            "#EXTM3U\n#EXTINF:180,Eisenwerk - Track 1\n"
            f"{root / one}\n../{two}\nArtists/Nowhere/Gone.mp3\n",
            encoding="utf-8")
        (source / "nested" / "Deep.pls").write_text(
            f"[playlist]\nFile1=file://{root / two}\nNumberOfEntries=1\n",
            encoding="utf-8")
        (source / "nested" / "notes.txt").write_text("not a playlist",
                                                     encoding="utf-8")
        preview = import_folder(source, root, apply=False)
        check("folder import finds both playlists", len(preview) == 2,
              str([item.name for item in preview]))
        two_lists = next(item for item in preview
                         if item.name == "Two_Lists")
        check("preview matches absolute and relative entries",
              len(two_lists.resolved) == 2, str(two_lists.resolved))
        check("preview reports the missing entry",
              len(two_lists.missing) == 1, str(two_lists.missing))
        check("preview writes nothing",
              not (root / "Playlists" / "Custom" / "Two_Lists.m3u8").exists())
        applied = import_folder(source, root, apply=True)
        check("apply writes both lists",
              all(item.target for item in applied),
              str([(item.name, item.target, item.note) for item in applied]))
        target = root / "Playlists" / "Custom" / "Two_Lists.m3u8"
        lines = [line for line in target.read_text(encoding="utf-8").splitlines()
                 if line and not line.startswith("#")]
        check("imported entries stay relative",
              sorted(lines) == sorted([f"../../{one}", f"../../{two}"]),
              str(lines))
        second = import_folder(source, root, apply=True)
        check("a second import never overwrites",
              all(item.target for item in second)
              and (root / "Playlists" / "Custom" / "Two_Lists_2.m3u8").exists(),
              str([item.target for item in second]))
        no_recursion = import_folder(source, root, recursive=False, apply=False)
        check("--no-recursive stops at the top level", len(no_recursion) == 1,
              str([item.name for item in no_recursion]))

        # -- OpenCode provider injection ----------------------------------
        oc_dir = work / "opencode"
        oc_dir.mkdir()
        (oc_dir / "opencode.json").write_text(json.dumps({
            "model": "gwdg/test-model",
            "provider": {"gwdg": {"options": {
                "baseURL": "http://llm.local/v1"}}},
        }), encoding="utf-8")
        (oc_dir / "auth.json").write_text(json.dumps({
            "gwdg": {"type": "api", "key": "sk-test"},
        }), encoding="utf-8")
        saved_env = {name: os.environ.get(name) for name in
                     ("MUSIC_OPENCODE_CONFIG", "MUSIC_OPENCODE_AUTH")}
        os.environ["MUSIC_OPENCODE_CONFIG"] = str(oc_dir / "opencode.json")
        os.environ["MUSIC_OPENCODE_AUTH"] = str(oc_dir / "auth.json")
        try:
            found = _opencode_config()
        finally:
            for name, value in saved_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        check("OpenCode provider is reused",
              found.get("base_url") == "http://llm.local/v1"
              and found.get("api_key") == "sk-test"
              and found.get("model") == "test-model", str(found))

        # -- multi-track playlist adds -------------------------------------
        reply = engine.handle("add Track 1 or Track 2 to My_Playlist")
        text = clean_text(reply)
        target = root / "Playlists" / "Custom" / "My_Playlist.m3u8"
        entries = [line for line in target.read_text(encoding="utf-8")
                   .splitlines() if line and not line.startswith("#")]
        check("'or' adds two tracks", "Added 2 tracks" in text, text[:200])
        check("both tracks are in the playlist",
              len(entries) == 2 and all("Track" in line for line in entries),
              str(entries))

        reply = engine.handle("add Track 3 and Track 4 to Chill Mix")
        text = clean_text(reply)
        target = root / "Playlists" / "Custom" / "Chill_Mix.m3u8"
        entries = [line for line in target.read_text(encoding="utf-8")
                   .splitlines() if line and not line.startswith("#")]
        check("'and' adds the second track when the phrase misses",
              "Added 2 tracks" in text, text[:200])
        check("the named list holds both tracks",
              len(entries) == 2, str(entries))

        reply = engine.handle("add some jazz to my evening")
        check("a mood add is not mistaken for a playlist add",
              str(reply.tool).startswith("make-playlist"), f"{reply.tool}: "
              f"{clean_text(reply)[:200]}")

        # -- “play something chill” builds a queue, not a saved playlist ----
        planned = nlu_plan("play something chill", engine.ctx, engine.catalog)
        check("a mood request without a list word is ad-hoc",
              planned is not None and planned.skill == "make-playlist"
              and planned.args.get("adhoc") is True, str(planned)[:200])
        planned = nlu_plan("make a playlist called Road Evening",
                           engine.ctx, engine.catalog)
        check("a request that names a playlist keeps it",
              planned is not None and planned.skill == "make-playlist"
              and planned.args.get("adhoc") is False, str(planned)[:200])
        planned = nlu_plan("one hour of metal", engine.ctx, engine.catalog)
        check("a length alone still builds a queue",
              planned is not None and planned.skill == "make-playlist"
              and planned.args.get("adhoc") is True, str(planned)[:200])

        reply = engine.handle("play something chill")
        text = clean_text(reply)
        own_dir = root / "Playlists" / "Own"
        own_files = sorted(own_dir.glob("*.m3u8")) if own_dir.is_dir() else []
        check("the chill request writes an ad-hoc queue",
              bool(own_files), text[:250])
        check("the answer offers to keep or discard it",
              "save it" in text and "discard" in text, text[:250])
        check("it does not create Custom/Chill",
              not (root / "Playlists" / "Custom" / "Chill.m3u8").is_file(),
              text[:200])
        own_before = len(own_files)

        reply = engine.handle("save it")
        text = clean_text(reply)
        check("“save it” turns the queue into a custom list",
              (root / "Playlists" / "Custom" / "Chill.m3u8").is_file(),
              text[:250])
        check("“save it” drops the ad-hoc copy",
              len(list(own_dir.glob("*.m3u8"))) == own_before - 1,
              text[:250])

        engine.handle("play something chill")
        reply = engine.handle("discard it")
        check("“discard it” removes only the queue",
              "Removed the ad-hoc list" in clean_text(reply)
              and (root / "Playlists" / "Custom" / "Chill.m3u8").is_file(),
              clean_text(reply)[:250])

        # -- library search (the window's search box) ----------------------
        found = service.search("Eisenwerk Track 1")
        check("search finds a track", found["count"] >= 1, str(found)[:200])
        check("search hit has the fields the page draws",
              set(("rel", "title", "artist", "album", "duration"))
              <= set(found["hits"][0]), str(found["hits"][0])[:200])
        check("a bare number does not match every extension",
              service.search("3")["count"] < len(service.engine.catalog.rows),
              str(service.search("3")["count"]))
        check("empty search is empty", service.search("")["count"] == 0)
        check("path search works", service.search("Federleicht")["count"] >= 1)

        # -- a playlist is a hit too, and clicking it loads the list --------
        found = service.search("chill")
        names = [item["name"] for item in found.get("playlists") or []]
        check("search finds a playlist by name", "Mood_Chill" in names,
              str(names))
        first = (found.get("playlists") or [{}])[0]
        check("a playlist hit has what the page draws",
              {"name", "path", "kind", "count"} <= set(first), str(first)[:200])
        check("a partial name still finds the list",
              bool(service.search("mood chil").get("playlists")),
              str(service.search("mood chil").get("playlists")))
        check("a bare hit count still reports the tracks",
              isinstance(found["count"], int) and "hits" in found,
              str(found.get("count")))

        quieter = Engine(root=root, use_llm=False, dry_run=True)
        try:
            resolved = quieter.catalog.resolve(str(first.get("path") or ""))
            check("a playlist file resolves to the playlist",
                  resolved.kind == "playlist" and resolved.playlist is not None
                  and resolved.playlist.count >= 1,
                  f"{resolved.kind} {resolved.label}")
            reply = quieter.handle(f"play {first.get('name')}")
            text = clean_text(reply)
            check("playing a playlist name loads the whole list",
                  "Mood_Chill" in text and "tracks" in text, text[:200])
            reply = quieter.skill_search({"query": "Mood_Chill"}, "test")
            check("a playlist-only search names the list",
                  "Mood_Chill" in clean_text(reply), clean_text(reply)[:200])
            reply = quieter.skill_search({"query": "chill"}, "test")
            check("a playlist is offered next to the track hits",
                  "Playlists" in clean_text(reply)
                  and "Mood_Chill" in clean_text(reply),
                  clean_text(reply)[:250])
        finally:
            quieter.close()


        # -- psychoacoustic profile, families and the net diagram ----------
        calm_rel = "Artists/Federleicht/Album/01_Track_1.mp3"
        metal_rel = "Artists/Eisenwerk/Album/01_Track_1.mp3"
        calm = engine.catalog.track(calm_rel)
        engine.ctx.current = calm
        reply = engine.handle("what are the properties of this song?")
        text = clean_text(reply)
        check("properties answer", "Profile" in text and "Speed" in text,
              text[:200])
        check("properties name the family", "Quiet · Calm" in text, text[:200])
        check("properties show all twelve axes",
              all(label in text for label in
                  ("Speed", "Energy", "Aggression", "Brightness",
                   "Danceability", "Valence", "Calmness", "Density",
                   "Roughness", "Harmony", "Punch", "Width")), text[:400])
        check("properties carry the raw measurement",
              "Tempo" in text and "BPM" in text, text[:400])
        check("properties group the measurements",
              all(group in text for group in
                  ("Rhythm", "Loudness", "Timbre", "Tonality",
                   "Bands and space")), text[:800])
        check("properties show loudness in LUFS",
              "LUFS" in text and "Crest factor" in text, text[:800])

        profile = service.psycho(calm_rel)
        check("service profile has twelve axes", len(profile["axes"]) == 12,
              str(profile.get("axes"))[:200])
        check("service profile returns values",
              profile["values"].get("calmness", 0) > 0.5,
              str(profile["values"])[:200])
        check("service profile names the family",
              profile["family"] == "Quiet · Calm", str(profile["family"]))
        check("service profile lists the families",
              len(profile["families"]) == 2, str(profile["families"])[:200])

        # the net diagram: moving aggression must find the metal shelf
        same = service.psycho_similar(calm_rel, adjustments={}, limit=5)
        moved = service.psycho_similar(calm_rel,
                                      adjustments={"speed": 0.3}, limit=5)
        check("similar without a change stays in the calm family",
              all("Eisenwerk" not in hit["rel"] for hit in same["hits"][:3]),
              str([h["rel"] for h in same["hits"][:3]]))
        check("moving one slider shifts the matches",
              moved["hits"] and all("Eisenwerk" not in hit["rel"]
                                    for hit in moved["hits"][:3]),
              str([h["rel"] for h in moved["hits"][:3]]))
        check("moving speed finds faster music",
              moved["hits"][0]["axes"].get("speed", 0)
              > same["hits"][0]["axes"].get("speed", 0) + 0.2,
              f"{moved['hits'][0]['axes'].get('speed')} vs "
              f"{same['hits'][0]['axes'].get('speed')}")
        check("similar reports the moved axes",
              moved["adjustments"].get("speed") == 0.3,
              str(moved["adjustments"]))
        check("similar hits carry a distance and family",
              all("distance" in hit and "family" in hit
                  for hit in moved["hits"]), str(moved["hits"][:1])[:300])

        # -- switchable profile sets ---------------------------------------
        # the same measurements seen as timbre, rhythm, dynamics and
        # (estimated) instruments; the default stays the psychology set
        instrument = service.psycho(calm_rel, "instrument")
        check("instrument set has six axes", len(instrument["axes"]) == 6,
              str(instrument.get("axes"))[:200])
        check("instrument set is reported as such",
              instrument.get("set") == "instrument"
              and all(axis["key"] in ("bass", "drums", "strings", "keys",
                                      "voice", "classical")
                      for axis in instrument["axes"]),
              str([a["key"] for a in instrument["axes"]]))
        check("instrument set carries values",
              instrument["values"].get("classical", 0) > 0.5,
              str(instrument["values"])[:300])
        check("an unknown set falls back to psychology",
              service.psycho(calm_rel, "nonsense").get("set") == "psychology")
        sets = {row["key"]: row for row in service.state()["psycho"]["sets"]}
        check("state offers five profile sets",
              set(sets) == {"psychology", "timbre", "rhythm", "dynamics",
                            "instrument"}, str(sorted(sets)))
        check("the set list carries counts and axes",
              sets["instrument"]["count"] >= 40
              and len(sets["instrument"]["axes"]) == 6
              and sets["instrument"]["estimated"] is True,
              str(sets["instrument"])[:300])

        rhythm_base = service.psycho(calm_rel, "rhythm")
        rhythm_moved = service.psycho_similar(
            calm_rel, adjustments={"speed": 0.25}, limit=5, set_name="rhythm")
        check("similar works inside a set",
              rhythm_moved.get("set") == "rhythm" and rhythm_moved["hits"]
              and all(hit["axes"].get("speed") is not None
                      for hit in rhythm_moved["hits"]),
              str(rhythm_moved["hits"][:1])[:300])
        check("a moved rhythm axis finds something faster",
              rhythm_moved["hits"][0]["axes"].get("speed", 0)
              > rhythm_base["values"].get("speed", 1) - 0.2,
              f"{rhythm_moved['hits'][0]['axes'].get('speed')} vs "
              f"{rhythm_base['values'].get('speed')}")

        reply = engine.handle("show the timbre profile of this song")
        text = clean_text(reply)
        check("chat shows a named set",
              "Timbre" in text and "Warmth" in text and "Air" in text,
              text[:300])
        reply = engine.handle("which profile sets are there")
        check("chat lists the sets",
              all(name in clean_text(reply)
                  for name in ("Timbre", "Rhythm", "Dynamics", "Instruments")),
              clean_text(reply)[:400])

        # an instrument property is a ranking inside the heuristic set
        reply = engine.handle("find the most bass-heavy song")
        text = clean_text(reply)
        check("chat ranks by an instrument property",
              "bass" in text.lower() and "Eisenwerk" in text, text[:300])

        service.settings({"psycho_set": "timbre"})
        check("the chosen set is remembered",
              service.state()["settings"]["psycho_set"] == "timbre",
              str(service.state()["settings"].get("psycho_set")))
        refused = service.settings({"psycho_set": "not a set"})
        check("an unknown profile set is refused",
              service.state()["settings"]["psycho_set"] == "timbre"
              and bool(refused.get("errors")), str(refused)[:300])
        service.engine.profile.set_setting("psycho_set", "not a set")
        check("a stale stored set falls back on read",
              service.state()["settings"]["psycho_set"] == "psychology")
        service.settings({"psycho_set": "timbre"})

        # -- lyrics: local transcription, rough search, the timed panel -----
        check("lyrics database is found",
              service.state()["lyrics"]["tracks"] == 2,
              str(service.state()["lyrics"])[:300])
        check("lyrics count the lines",
              service.state()["lyrics"]["lines"] == 5,
              str(service.state()["lyrics"])[:300])

        one = service.lyrics(metal_rel)
        check("a song's lines come with timestamps",
              one["available"] and len(one["lines"]) == 3
              and one["lines"][1]["start"] == 12.0,
              str(one.get("lines"))[:300])
        check("the lines know their language",
              one.get("language") == "en" and one.get("model") == "small",
              str(one)[:300])

        engine.ctx.current = engine.catalog.track(metal_rel)
        reply = engine.handle("show the lyrics")
        text = clean_text(reply)
        check("chat shows the lyrics of the current song",
              "burning sea" in text and "0:12" in text, text[:300])
        reply = engine.handle("what are the lyrics of this song?")
        check("chat answers a lyrics question",
              "boat of steel" in clean_text(reply), clean_text(reply)[:300])

        # more ways to ask for the words of the song that is playing
        reply = engine.handle("list the lyrics of the currently playing song")
        text = clean_text(reply)
        check("a list request gives the playing song's lyrics",
              "burning sea" in text and "0:12" in text, text[:300])

        reply = engine.handle("read me the words of this song")
        check("the words of this song are its lyrics",
              "boat of steel" in clean_text(reply), clean_text(reply)[:200])

        reply = engine.handle("which line is playing")
        text = clean_text(reply)
        check("the line at the playhead is named",
              "line 1 of 3" in text and "burning sea" in text, text[:300])
        check("the next line comes along as context",
              "engines roar" in text, text[:300])

        reply = engine.handle("what's the next line")
        text = clean_text(reply)
        check("the next lyric line is shown",
              "line 2 of 3" in text and "engines roar" in text, text[:300])

        reply = engine.handle("where are we in the lyrics")
        check("the playhead question shows the line",
              "line 1 of 3" in clean_text(reply), clean_text(reply)[:200])

        reply = engine.handle("show the lyrics of 01_Track_1.mp3")
        text = clean_text(reply)
        check("a named file's lyrics are shown",
              "burning sea" in text or "quiet boat" in text, text[:300])

        engine.dry_run = True
        reply = engine.handle("transcribe the lyrics of 01_Track_1.mp3")
        text = clean_text(reply)
        check("a named file can be transcribed",
              "would transcribe" in text.casefold() and "01_Track_1" in text,
              text[:300])
        reply = engine.handle("transcribe all songs without lyrics")
        text = clean_text(reply)
        check("transcribing everything is a full run, not one song",
              "whole library" in text.casefold(), text[:300])
        engine.dry_run = False

        reply = engine.handle("find songs about a boat")
        text = clean_text(reply)
        hits = reply.tracks or []
        check("lyric search finds both boat songs", len(hits) == 2,
              str([track.rel for track in hits]))
        check("lyric search quotes the line", "boat" in text
              and "_\u201c" in text, text[:400])
        check("lyric search names the timestamp", "0:00" in text, text[:400])

        reply = engine.handle("something with a boat and fast")
        text = clean_text(reply)
        ordered = reply.tracks or []
        check("a lyric search combines with a sound property",
              bool(ordered) and "fast" in text.casefold(), text[:400])
        check("the fast metal boat comes first",
              ordered and "Eisenwerk" in ordered[0].rel,
              str([track.rel for track in (ordered or [])]))
        check("the combined hit carries its sound score",
              "sound 0." in text, text[:400])

        check("a word the song keeps saying is counted",
              "×2" in text and "boat" in text.casefold(), text[:400])

        reply = engine.handle("which song mentions pizza")
        check("an unmatched word is reported, not invented",
              "no lyric line" in clean_text(reply).casefold(),
              clean_text(reply)[:200])

        # -- whole words, not substrings -----------------------------------
        # "sea" must not reach "seaside"/"search", the plural still counts, a
        # song that returns to the word wins and a typo is flagged.
        import sqlite3 as _sqlite3
        word_root = Path(tempfile.mkdtemp(prefix="lyric-words-"))
        wfolder = word_root / "library-work" / "app" / "lyrics"
        wfolder.mkdir(parents=True)
        wconn = _sqlite3.connect(wfolder / "lyrics.db")
        wconn.executescript("""
            CREATE TABLE track (rel_path TEXT PRIMARY KEY, title TEXT,
                artist TEXT, album TEXT, duration_s REAL, language TEXT,
                model TEXT, backend TEXT, source_size INTEGER,
                source_mtime TEXT, transcribed_at TEXT,
                line_count INTEGER DEFAULT 0, text TEXT);
            CREATE TABLE line (rel_path TEXT NOT NULL, idx INTEGER NOT NULL,
                start_s REAL, end_s REAL, text TEXT NOT NULL,
                PRIMARY KEY (rel_path, idx));
            CREATE VIRTUAL TABLE line_fts USING fts5(
                text, rel_path UNINDEXED, idx UNINDEXED, start_s UNINDEXED,
                end_s UNINDEXED);
        """)
        word_tracks = {
            "Artists/Aqua/Sea/01_We_Belong_to_the_Sea.mp3": (
                "We Belong to the Sea", "Aqua",
                ["We belong to the sea", "to the waves, you and me",
                 "We belong to the sea of blue"]),
            "Artists/Passing/Else/01_Else.mp3": (
                "Elsewhere", "Passing",
                ["Passing through the seaside", "I go searching for a sign"]),
            "Artists/Pizza/Pizza/01_Pizza.mp3": (
                "Pizza", "Pizza", ["Sie sah aus wie ein Stück Pizza"]),
        }
        for rel, (title, artist, lines) in word_tracks.items():
            wconn.execute(
                "INSERT INTO track(rel_path, title, artist, album, duration_s,"
                " line_count, text) VALUES(?,?,?,?,?,?,?)",
                (rel, title, artist, "Album", 60.0, len(lines), " ".join(lines)))
            for idx, text_line in enumerate(lines):
                wconn.execute("INSERT INTO line VALUES(?,?,?,?,?)",
                              (rel, idx, idx * 5.0, idx * 5.0 + 4.0, text_line))
                wconn.execute("INSERT INTO line_fts VALUES(?,?,?,?,?)",
                              (text_line, rel, idx, idx * 5.0,
                               idx * 5.0 + 4.0))
        wconn.commit()
        wconn.close()
        exact = lyrics_mod.Lyrics(word_root)
        sea_hits = exact.find("sea", limit=6)
        check("a lyric search matches whole words only",
              [hit["rel"] for hit in sea_hits]
              == ["Artists/Aqua/Sea/01_We_Belong_to_the_Sea.mp3"],
              str([hit["rel"] for hit in sea_hits]))
        check("the song that keeps saying it is the theme",
              sea_hits and sea_hits[0]["about"] is True
              and sea_hits[0]["mentions"] == 2, str(sea_hits)[:300])
        check("a plural search finds the singular too",
              [hit["rel"] for hit in exact.find("seas", limit=6)]
              == ["Artists/Aqua/Sea/01_We_Belong_to_the_Sea.mp3"],
              str([hit["rel"] for hit in exact.find("seas", limit=6)]))
        check("a substring is a different word",
              exact.find("sea", limit=6)
              and not any("Passing" in hit["rel"] for hit in sea_hits),
              str([hit["rel"] for hit in sea_hits]))
        fuzzy_hits = exact.find("piza", limit=4)
        check("a misspelling is offered as a fuzzy hit, never a substring",
              fuzzy_hits and fuzzy_hits[0]["fuzzy"] is True
              and "Pizza" in fuzzy_hits[0]["rel"], str(fuzzy_hits)[:300])
        check("word forms stop at the word",
              lyrics_mod.word_forms("sea") == ["sea", "seas"]
              and "seaside" not in lyrics_mod.word_forms("sea")
              and lyrics_mod.word_forms("boat") == ["boat", "boats"],
              str(lyrics_mod.word_forms("sea")))
        shutil.rmtree(word_root, ignore_errors=True)

        # -- similar lyrics: a lexical neighbour, not a model --------------
        # A TF-IDF comparison of the stored lyric words: the songs that share
        # *rare* words are neighbours, a single "boat" is not enough, and a
        # song without lyrics has none.
        sim_root = Path(tempfile.mkdtemp(prefix="lyric-similar-"))
        sfolder = sim_root / "library-work" / "app" / "lyrics"
        sfolder.mkdir(parents=True)
        sconn = _sqlite3.connect(sfolder / "lyrics.db")
        sconn.executescript("""
            CREATE TABLE track (rel_path TEXT PRIMARY KEY, title TEXT,
                artist TEXT, album TEXT, duration_s REAL, language TEXT,
                model TEXT, backend TEXT, source_size INTEGER,
                source_mtime TEXT, transcribed_at TEXT,
                line_count INTEGER DEFAULT 0, text TEXT);
            CREATE TABLE line (rel_path TEXT NOT NULL, idx INTEGER NOT NULL,
                start_s REAL, end_s REAL, text TEXT NOT NULL,
                PRIMARY KEY (rel_path, idx));
            CREATE VIRTUAL TABLE line_fts USING fts5(
                text, rel_path UNINDEXED, idx UNINDEXED, start_s UNINDEXED,
                end_s UNINDEXED);
        """)
        sim_tracks = {
            metal_rel: ("Track 1", "Eisenwerk", [
                "we sail the boat across the burning sea",
                "the engines roar and nothing stops us now",
                "a boat of steel and fire"]),
            "Artists/Steelworks/Forge/01_Anvil.mp3": ("Anvil", "Steelworks", [
                "the steel and the fire in the forge",
                "the engines roar again"]),
            "Artists/Furnace/Night/01_Ember.mp3": ("Ember", "Furnace", [
                "fire burns in the furnace all night",
                "the engines hum"]),
        }
        for rel, (title, artist, lines) in sim_tracks.items():
            sconn.execute(
                "INSERT INTO track(rel_path, title, artist, album, duration_s,"
                " line_count, text) VALUES(?,?,?,?,?,?,?)",
                (rel, title, artist, "Album", 60.0, len(lines),
                 " ".join(lines)))
            for idx, text_line in enumerate(lines):
                sconn.execute("INSERT INTO line VALUES(?,?,?,?,?)",
                              (rel, idx, idx * 5.0, idx * 5.0 + 4.0,
                               text_line))
        sconn.commit()
        sconn.close()
        similar_db = lyrics_mod.Lyrics(sim_root)
        neighbours = similar_db.similar(metal_rel)
        check("similar lyrics find the songs that share rare words",
              len(neighbours) == 2
              and neighbours[0]["rel"]
              == "Artists/Steelworks/Forge/01_Anvil.mp3",
              str(neighbours)[:300])
        check("a neighbour names the words it shares",
              {"steel", "fire"} <= set(neighbours[0]["shared"]),
              str(neighbours[0].get("shared")))
        check("a neighbour quotes a line with a shared word",
              "fire" in neighbours[0]["line"].casefold(), str(neighbours[0]))
        check("the seed is never its own neighbour",
              all(hit["rel"] != metal_rel for hit in neighbours))
        check("a song without lyrics has no neighbours",
              similar_db.similar("Artists/Nobody/Nothing/01_X.mp3") == [])

        real_lyrics = engine.lyrics
        real_current = engine.ctx.current
        try:
            engine.lyrics = similar_db
            engine.ctx.current = engine.catalog.track(metal_rel)
            reply = engine.handle("find songs with similar lyrics")
            text = clean_text(reply)
            check("chat finds songs with similar lyrics",
                  reply.tool == "lyrics · similar" and "Steelworks" in text
                  and "% word overlap" in text,
                  f"{reply.tool}: {text[:300]}")
            check("chat names the shared words",
                  "shared:" in text and "steel" in text.casefold(), text[:400])
            reply = engine.handle("more songs like this lyrically")
            check("“like this lyrically” is the same search",
                  reply.tool == "lyrics · similar"
                  and "Steelworks" in clean_text(reply),
                  f"{reply.tool}: {clean_text(reply)[:200]}")
            reply = engine.handle("similar lyrics to Federleicht")
            check("a named song without lyrics answers honestly",
                  reply.tool == "lyrics · similar"
                  and "no lyrics" in clean_text(reply).casefold(),
                  clean_text(reply)[:200])
        finally:
            engine.lyrics = real_lyrics
            engine.ctx.current = real_current

        service_lyrics = service.engine.lyrics
        service_current = service.engine.ctx.current
        try:
            service.engine.lyrics = similar_db
            service.engine.ctx.current = service.engine.catalog.track(metal_rel)
            api = service.lyrics_similar(metal_rel)
            check("the window API returns the same neighbours",
                  api.get("available") is True and len(api.get("hits") or []) == 2
                  and api["hits"][0]["rel"]
                  == "Artists/Steelworks/Forge/01_Anvil.mp3",
                  str(api)[:250])
            api = service.lyrics_similar(
                "Artists/Steelworks/Forge/01_Anvil.mp3")
            check("an unknown rel falls back to the playing song",
                  api.get("rel") == metal_rel
                  and len(api.get("hits") or []) == 2, str(api)[:250])
            service.engine.ctx.current = None
            missing = service.lyrics_similar("")
            check("the window API answers honestly without a seed",
                  missing.get("available") is False
                  and bool(missing.get("error")), str(missing)[:200])
        finally:
            service.engine.lyrics = service_lyrics
            service.engine.ctx.current = service_current
        sound_plan = nlu_plan("play songs that sound similar", engine.ctx,
                              engine.catalog)
        check("a sound-similarity sentence is not a lyric search",
              sound_plan is None or sound_plan.skill != "lyrics",
              str(sound_plan)[:200])
        shutil.rmtree(sim_root, ignore_errors=True)

        # "how many songs do I have" shares the word *have* with "songs that
        # have a boat", but it counts the library: it must not search the
        # lyrics for the phrase "how many".
        for sentence in ("how many tracks do I have", "how many songs do I have",
                         "wie viele lieder habe ich"):
            reply = engine.handle(sentence)
            text = clean_text(reply).casefold()
            check(f"{sentence!r} counts the library, it does not search lyrics",
                  reply.tool == "library-overview" and "lyrics with" not in text,
                  f"{reply.tool}: {text[:160]}")
        reply = engine.handle("songs that have a boat")
        check("a weak relation still reaches the lyrics",
              (reply.tool or "").startswith("lyrics"), str(reply.tool))

        reply = engine.handle("how is the transcription going")
        check("lyrics status answers", "## Lyrics" in reply.text,
              reply.text[:200])

        reply = engine.handle("transcribe this song")
        text = clean_text(reply)
        check("chat refuses to transcribe without a backend",
              "could not start" in text.casefold()
              or "no local transcriber" in text.casefold()
              or "no `scripts/transcribe_lyrics.py`" in text.casefold(),
              text[:300])

        check("lyrics_panel defaults to off",
              service.state()["settings"]["lyrics_panel"] is False)
        service.settings({"lyrics_panel": True})
        check("the panel setting is remembered",
              service.state()["settings"]["lyrics_panel"] is True)
        service.settings({"lyrics_panel": False})

        # -- lyrics as a file, the timing shift, one search for both --------
        timing = service.engine.lyrics.lrc(metal_rel)
        check("the lyrics render as an lrc file",
              timing.startswith("[by:") and "[ti:Track 1]" in timing
              and "[ar:Eisenwerk]" in timing
              and "[length:00:36.00]" in timing, timing[:300])
        check("an lrc line carries hundredths",
              "[00:12.00]the engines roar" in timing, timing[:400])

        check("a song starts unshifted",
              service.engine.lyrics.offset(metal_rel) == 0.0)
        stored = service.engine.lyrics.set_offset(metal_rel, 0.5)
        shifted = service.lyrics(metal_rel)
        check("the timing shift moves the lines",
              stored == 0.5 and shifted["offset"] == 0.5
              and shifted["lines"][1]["start"] == 12.5,
              str(shifted.get("lines"))[:300])
        check("the shift lands in the lrc too",
              "[00:12.50]the engines roar" in service.engine.lyrics.lrc(metal_rel))
        check("a shift is clamped to half a minute",
              service.engine.lyrics.set_offset(metal_rel, 100.0) == 30.0)
        try:
            service.engine.lyrics.set_offset(metal_rel, "abc")
            refused_offset = False
        except ValueError:
            refused_offset = True
        check("a non-numeric shift is refused", refused_offset)
        check("zero removes the shift",
              service.engine.lyrics.set_offset(metal_rel, 0) == 0.0
              and metal_rel not in lyrics_mod.read_offsets(root))

        # the journalled sidecar
        sidecar = (root / metal_rel).with_suffix(".lrc")
        first = lrc.export(root, service.engine.lyrics, metal_rel)
        check("the lrc is written next to the song",
              not first.get("error") and sidecar.is_file()
              and first.get("inside") == sidecar.relative_to(root).as_posix(),
              str(first)[:300])
        records = lrc.entries(root)
        check("the export is journalled first",
              bool(records) and records[0]["path"] == str(sidecar)
              and bool(records[0]["inside"]) and not records[0]["replaced"],
              str(records[:1])[:300])
        again = lrc.export(root, service.engine.lyrics, metal_rel)
        check("an existing lrc is not overwritten silently",
              again.get("exists") is True
              and "already exists" in str(again.get("error")),
              str(again)[:300])
        undone = lrc.rollback(root)
        check("rollback removes the file it created",
              undone.get("removed") == 1 and not sidecar.exists(),
              str(undone)[:300])

        lrc.export(root, service.engine.lyrics, metal_rel)
        replaced = lrc.export(root, service.engine.lyrics, metal_rel,
                              overwrite=True)
        check("an overwrite keeps the previous bytes",
              replaced.get("replaced") is True
              and bool(lrc.entries(root)[0].get("previous_b64")),
              str(replaced)[:300])
        restored = lrc.rollback(root)
        check("rollback restores the replaced file",
              restored.get("restored") == 1 and sidecar.is_file(),
              str(restored)[:300])
        sidecar.unlink()

        engine.ctx.current = engine.catalog.track(metal_rel)
        reply = engine.handle("export the lyrics as lrc")
        check("chat exports the lyrics as a file",
              "Lyrics exported" in reply.text and sidecar.is_file(),
              reply.text[:300])
        lrc.rollback(root)
        engine.dry_run = True
        reply = engine.handle("export the lyrics as lrc")
        check("a dry run writes no lrc",
              "Would write" in reply.text and not sidecar.exists(),
              reply.text[:300])
        engine.dry_run = False

        reply = engine.handle("shift the lyrics half a second later")
        check("chat nudges the lyrics later",
              service.engine.lyrics.offset(metal_rel) == 0.5
              and "0.5 s later" in reply.text, reply.text[:300])
        reply = engine.handle("the lyrics are too early")
        check("a complaint about early lines moves them later",
              service.engine.lyrics.offset(metal_rel) == 1.0, reply.text[:300])
        reply = engine.handle("reset the lyrics timing")
        check("chat resets the timing",
              service.engine.lyrics.offset(metal_rel) == 0.0
              and "reset" in reply.text.casefold(), reply.text[:300])

        # one search box for the tags and the sung words
        sung = service.search("steel")
        check("search falls back to the lyrics",
              not sung["hits"] and bool(sung.get("lyrics")),
              str(sung)[:300])
        reply = engine.skill_search({"query": "steel"}, "steel")
        check("chat names the song that sings a word",
              "sing" in reply.text.casefold()
              and any("Eisenwerk" in track.rel for track in (reply.tracks or [])),
              reply.text[:300])
        titled = service.search("Track 1")
        check("a title hit and a lyric hit are one list",
              bool(titled["hits"])
              and not ({hit["rel"] for hit in titled.get("lyrics") or []}
                       & {hit["rel"] for hit in titled["hits"]}),
              str(titled)[:300])

        drafted = service.start_lyrics(metal_rel)
        check("starting a transcription without a backend reports why",
              "error" in drafted
              and ("transcriber" in drafted["error"]
                   or "transcribe_lyrics" in drafted["error"]),
              str(drafted)[:300])

        reply = engine.handle("show the psycho families")
        text = clean_text(reply)
        check("families answer", "Psychoacoustic families" in text, text[:200])
        check("families list the calm family", "Quiet · Calm" in text,
              text[:300])
        check("families link a playlist", "Playlists/Psycho" in text,
              text[:400])

        # -- ranking by sound property -------------------------------------
        # "the slowest and boring song" is not a tag search: those words are
        # nowhere in the tags, the profile database has to rank by them.
        reply = engine.handle("find the slowest and boring song")
        text = clean_text(reply)
        check("a property request is not answered with 'no tracks match'",
              "No tracks match" not in text, text[:300])
        check("a property request ranks the library",
              reply.tool == "rank-library" and len(reply.tracks) >= 5,
              f"{reply.tool} {len(reply.tracks)}")
        check("the ranking picks the calm shelf",
              bool(reply.tracks) and "Federleicht" in reply.tracks[0].rel,
              reply.tracks[0].rel if reply.tracks else "no tracks")
        check("the ranking shows a score and the leading axes",
              "score" in text and "speed" in text, text[:400])

        reply = engine.handle("the least boring track")
        check("'least boring' flips to the most interesting",
              bool(reply.tracks) and "Eisenwerk" in reply.tracks[0].rel,
              reply.tracks[0].rel if reply.tracks else "no tracks")

        reply = engine.handle("show me the 3 most aggressive songs")
        check("a ranking honours the requested count",
              len(reply.tracks) == 3, str(len(reply.tracks)))

        reply = engine.handle("das langsamste Lied von Eisenwerk")
        check("a ranking can be restricted to an artist",
              bool(reply.tracks) and all("Eisenwerk" in track.rel
                                         for track in reply.tracks),
              str([track.rel for track in reply.tracks[:3]]))

        missing = engine.skill_rank({"properties": ["wobbliness"]},
                                    "wobbliness")
        check("an unknown property is refused",
              missing.error and "Rank by what" in missing.text,
              missing.text[:200])

        plan = nlu_plan("play the slowest song", engine.ctx, engine.catalog,
                        engine.profile)
        check("'play the slowest song' plans a ranking with playback",
              plan is not None and plan.skill == "rank-library"
              and plan.args.get("play") is True, str(plan)[:200])
        plan = nlu_plan("show me the metal albums", engine.ctx, engine.catalog,
                        engine.profile)
        check("a genre request stays a search",
              plan is not None and plan.skill == "search-library",
              str(plan)[:200])

        lonely = Engine(root=work / "no-profiles", use_llm=False,
                        dry_run=True)
        bare = lonely.skill_rank({"properties": ["slow"]}, "the slowest song")
        check("ranking without a profile database explains itself",
              bare.error and "not measured" in bare.text, bare.text[:200])
        lonely.close()
        engine.ctx.last_tracks = []

        reply = engine.handle("show my playlists")
        check("show-playlists has a sound-family group",
              "Sound families" in clean_text(reply),
              clean_text(reply)[:400])
        check("state reports the profile database",
              service.state()["psycho"]["ready"] is True
              and service.state()["psycho"]["profiles"] >= 40,
              str(service.state()["psycho"])[:300])
        engine.ctx.current = None

        # -- measuring from inside the app --------------------------------
        state = service.state()
        check("state reports the psycho job slot",
              "job" in state["psycho"], str(state["psycho"])[:200])
        # this test library has no scripts/, so there is nothing to run
        check("job availability is honest",
              psycho_job.available(root) is False)
        check("autostart is a no-op without the scripts",
              psycho_job.autostart(root) == {})
        refused = service.start_psycho_update()
        check("update refuses without the measuring scripts",
              refused.get("available") is False and "error" in refused,
              str(refused)[:200])
        reply = engine.handle("update the sound profiles")
        check("the planner knows the update request",
              "psycho_features" in clean_text(reply).lower()
              or "nothing to run" in clean_text(reply).lower(),
              clean_text(reply)[:200])

        # -- settings: the music folder and the automatic measuring -------
        state = service.state()
        check("settings name the current library",
              state["settings"]["library"] == str(root.resolve())
              or state["settings"]["library"] == str(root),
              str(state["settings"])[:300])
        check("state carries psycho_auto",
              state["settings"]["psycho_auto"] is True,
              str(state["settings"])[:300])
        bad = service.settings({"library": "/does/not/exist"})
        check("a missing folder is refused",
              bad["library_result"]["ok"] is False
              and "not a folder" in bad["library_result"]["error"],
              str(bad["library_result"])[:200])
        empty = service.settings({"library": ""})
        check("an empty folder is refused",
              empty["library_result"]["ok"] is False, str(empty)[:200])
        turned = service.settings({"psycho_auto": False})
        check("psycho_auto can be turned off",
              turned["psycho_auto"] is False, str(turned)[:200])
        check("psycho_auto survives into the state",
              service.state()["settings"]["psycho_auto"] is False)
        service.settings({"psycho_auto": True})

        # -- a real job, against fake measuring scripts -------------------
        joblib = work / "joblib"
        (joblib / "Playlists").mkdir(parents=True)
        (joblib / "Artists").mkdir(parents=True)
        (joblib / "library-work" / "app" / "psycho").mkdir(parents=True)
        (joblib / "scripts").mkdir(parents=True)
        (joblib / "scripts" / "psycho_features.py").write_text(
            "print('to measure: 4  workers: 2', flush=True)\n"
            "print('  2/4 measured, 0 errors', flush=True)\n"
            "print('  4/4 measured, 0 errors', flush=True)\n",
            encoding="utf-8")
        (joblib / "scripts" / "psycho_profile.py").write_text(
            "print('[psycho] profile written', flush=True)\n",
            encoding="utf-8")
        import sqlite3
        conn = sqlite3.connect(joblib / "library-work" / "app" / "psycho"
                               / "features.db")
        conn.executescript("""
            CREATE TABLE audio (rel_path TEXT PRIMARY KEY, source TEXT,
                lufs REAL, sharpness_acum REAL);
            CREATE TABLE profile (rel_path TEXT PRIMARY KEY);
            CREATE TABLE family (name TEXT PRIMARY KEY);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        conn.commit()
        conn.close()
        check("the job is available in a measuring library",
              psycho_job.available(joblib) is True)
        check("the counts read the database",
              psycho_job.counts(joblib)["exists"] is True
              and psycho_job.counts(joblib)["total"] == 0,
              str(psycho_job.counts(joblib)))
        job = psycho_job.start(joblib)
        check("starting returns a running job",
              job.get("state") == "running" and job.get("id"), str(job)[:200])
        check("the job record is on disk",
              (joblib / "library-work" / "app" / "psycho" / "jobs"
               / f"{job['id']}.json").is_file())
        check("a second start reuses the running job",
              psycho_job.start(joblib).get("id") == job["id"])
        status = {}
        for _ in range(200):
            status = psycho_job.status(joblib)
            if status.get("state") != "running":
                break
            time.sleep(0.05)
        check("the job finishes", status.get("state") == "done",
              str(status)[:300])
        check("the job reports its progress",
              status.get("percent") == 100.0, str(status)[:300])
        check("the job parses the measured count",
              status.get("total") == 4, str(status)[:300])
        check("the job keeps the log tail",
              any("measured" in line or "psycho" in line
                  for line in status.get("lines") or []),
              str(status.get("lines"))[:300])
        check("the job record knows the finished time",
              bool(status.get("finished")), str(status)[:200])
        check("a finished job is not started again by autostart",
              psycho_job.autostart(joblib) == {})

        # -- the library index: same job pattern, its own pipeline ---------
        check("state carries the index slot",
              "index" in service.state(),
              str(service.state().get("index"))[:200])
        check("an index without the pipeline is honest",
              index_job.available(root) is False
              and service.state()["index"]["available"] is False,
              str(service.state().get("index"))[:200])
        refused_index = service.start_index()
        check("indexing refuses without the inventory scripts",
              refused_index.get("available") is False
              and "error" in refused_index, str(refused_index)[:200])
        for sentence, skill, action in (
                ("index the library", "index-library", "index"),
                ("please rescan my library", "index-library", "index"),
                ("rebuild the library index", "index-library", "index"),
                ("how is the indexing going", "index-library", "status"),
                ("pause the indexing", "index-library", "pause"),
                ("resume the indexing", "index-library", "resume"),
                ("stop the index", "index-library", "cancel"),
                ("cancel the indexing", "index-library", "cancel"),
                ("is the index paused?", "index-library", "status"),
                ("pausiere das Indizieren", "index-library", "pause"),
                ("brich das Indizieren ab", "index-library", "cancel"),
                ("measure the library", "psycho-profile", "update")):
            planned = nlu_plan(sentence, engine.ctx, engine.catalog)
            check(f"the planner routes “{sentence}”",
                  planned is not None and planned.skill == skill
                  and str(planned.args.get("action")) == action,
                  str(planned)[:200])
        # a short "pause" stays a transport command, not an index job
        for sentence, skill, action in (
                ("pause the music", "transport", "pause"),
                ("resume", "transport", "resume"),
                ("stop", "transport", "stop")):
            planned = nlu_plan(sentence, engine.ctx, engine.catalog)
            check(f"the planner still routes “{sentence}” as {skill}",
                  planned is not None and planned.skill == skill
                  and str(planned.args.get("action")) == action,
                  str(planned)[:200])

        indexlib = work / "indexlib"
        (indexlib / "Playlists").mkdir(parents=True)
        (indexlib / "Artists").mkdir(parents=True)
        inventory = indexlib / "library-work" / "inventory" / "files.jsonl"
        inventory.parent.mkdir(parents=True)
        for i in range(5):
            (indexlib / "Artists" / "A").mkdir(parents=True, exist_ok=True)
            (indexlib / "Artists" / "A" / f"{i}.mp3").write_bytes(b"x")
        inventory.write_text("".join(
            json.dumps({"rel_path": f"Artists/A/{i}.mp3",
                        "title": f"Track {i}", "artist": "A",
                        "duration_s": 1, "ext": ".mp3"}) + "\n"
            for i in range(5)), encoding="utf-8")
        (indexlib / "scripts").mkdir(parents=True)
        (indexlib / "scripts" / "phase0_inventory.py").write_text(
            "import sys\n"
            "if sys.argv[1] == 'scan':\n"
            "    print('[scan] total candidates=7 already_done=5 todo=2',\n"
            "          flush=True)\n"
            "    print('[scan] 1/2  5.0 files/s', flush=True)\n"
            "    print('[scan] 2/2  5.0 files/s', flush=True)\n"
            "    print('[scan] finished 2 files in 0.4s, errors=0',\n"
            "          flush=True)\n"
            "else:\n"
            "    print('[report] loaded 7 records', flush=True)\n"
            "    print('[report] files=7 size=0.01GB duration=0.1h',\n"
            "          flush=True)\n", encoding="utf-8")
        (indexlib / "scripts" / "make_playlists.py").write_text(
            "print('playlists: 3 in Playlists', flush=True)\n",
            encoding="utf-8")
        check("the job is available in an indexed library",
              index_job.available(indexlib) is True)
        check("the counts read the inventory",
              index_job.counts(indexlib)["tracks"] == 5
              and index_job.counts(indexlib)["exists"] is True,
              str(index_job.counts(indexlib)))
        job = index_job.start(indexlib)
        check("starting an index returns a running job",
              job.get("state") == "running" and job.get("id"), str(job)[:200])
        check("a second start reuses the running index",
              index_job.start(indexlib).get("id") == job["id"])
        status = {}
        for _ in range(200):
            status = index_job.status(indexlib)
            if status.get("state") != "running":
                break
            time.sleep(0.05)
        check("the index finishes", status.get("state") == "done",
              str(status)[:300])
        check("the index reports 100 %",
              status.get("percent") == 100.0, str(status)[:300])
        check("the index parses the scanned count",
              status.get("total") == 2 and status.get("done") == 2,
              str(status)[:300])
        check("the index names its last stage",
              status.get("step") == "finished" and status.get("phase") == "done",
              str(status)[:300])
        check("the index keeps the log tail",
              any("playlists" in line or "report" in line
                  for line in status.get("lines") or []),
              str(status.get("lines"))[:300])
        check("the index record knows the finished time",
              bool(status.get("finished")), str(status)[:200])

        index_service = ChatService(root=indexlib, use_llm=False,
                                    auto_psycho=False)
        try:
            check("the service reports the index in its state",
                  index_service.state()["index"]["available"] is True
                  and index_service.state()["index"]["counts"]["tracks"] == 5,
                  str(index_service.state().get("index"))[:300])
            started = index_service.start_index()
            check("the service starts an index job",
                  started.get("running") is True
                  and started.get("started_now") is True, str(started)[:300])
            final_index = {}
            for _ in range(200):
                final_index = index_service.index_update_status()
                if not final_index.get("running"):
                    break
                time.sleep(0.05)
            check("the service sees the index finish",
                  final_index.get("state") == "done",
                  str(final_index)[:300])
            check("a finished index reaches the service state",
                  index_service.state()["index"]["running"] is False
                  and index_service.state()["index"]["counts"]["tracks"] == 5,
                  str(index_service.state().get("index"))[:300])
        finally:
            index_service.engine.close()

        # -- pause, resume and cancel the index ---------------------------
        #
        # The job is a process group, so the controls are signals: SIGSTOP
        # freezes it, SIGCONT continues it, SIGTERM ends it.  The stub scan
        # below ticks once every 100 ms so a frozen counter is visible.
        slowlib = work / "indexslow"
        (slowlib / "Playlists").mkdir(parents=True)
        (slowlib / "Artists").mkdir(parents=True)
        slow_inv = slowlib / "library-work" / "inventory" / "files.jsonl"
        slow_inv.parent.mkdir(parents=True)
        (slowlib / "Artists" / "A").mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (slowlib / "Artists" / "A" / f"{i}.mp3").write_bytes(b"x")
        slow_inv.write_text("".join(
            json.dumps({"rel_path": f"Artists/A/{i}.mp3", "title": f"T{i}",
                        "artist": "A", "duration_s": 1, "ext": ".mp3"}) + "\n"
            for i in range(3)), encoding="utf-8")
        (slowlib / "scripts").mkdir(parents=True)
        (slowlib / "scripts" / "phase0_inventory.py").write_text(
            "import sys, time\n"
            "if sys.argv[1] == 'scan':\n"
            "    print('[scan] total candidates=40 already_done=0 todo=40',\n"
            "          flush=True)\n"
            "    for i in range(40):\n"
            "        time.sleep(0.1)\n"
            "        print(f'[scan] {i + 1}/40  10.0 files/s', flush=True)\n"
            "    print('[scan] finished 40 files in 4.0s, errors=0', flush=True)\n"
            "else:\n"
            "    print('[report] loaded 40 records', flush=True)\n",
            encoding="utf-8")
        (slowlib / "scripts" / "make_playlists.py").write_text(
            "print('playlists: 1 in Playlists', flush=True)\n",
            encoding="utf-8")

        slow = index_job.start(slowlib)
        moving = {}
        for _ in range(200):
            moving = index_job.status(slowlib)
            if int(moving.get("done") or 0) >= 2:
                break
            time.sleep(0.05)
        paused = index_job.pause(slowlib)
        check("pause stops the index",
              paused.get("state") == "paused"
              and paused.get("running") is False
              and paused.get("active") is True, str(paused)[:300])
        frozen = int(paused.get("done") or 0)
        time.sleep(0.5)
        still = index_job.status(slowlib)
        check("a paused index makes no progress",
              still.get("state") == "paused"
              and int(still.get("done") or 0) == frozen, str(still)[:300])

        resumed = index_job.resume(slowlib)
        check("resume continues the same job",
              resumed.get("state") == "running"
              and resumed.get("id") == slow["id"], str(resumed)[:300])
        later = {}
        for _ in range(200):
            later = index_job.status(slowlib)
            if int(later.get("done") or 0) > frozen:
                break
            time.sleep(0.05)
        check("a resumed index counts again",
              int(later.get("done") or 0) > frozen, str(later)[:300])

        # a SIGTERM in the middle of a write must not leave a broken line
        with slow_inv.open("ab") as handle:
            handle.write(b'{"rel_path": "Artists/A/half')
            handle.flush()
        cancelled = index_job.cancel(slowlib)
        check("cancel asks the job to stop",
              cancelled.get("state") in {"cancelling", "cancelled"},
              str(cancelled)[:300])
        final = {}
        for _ in range(200):
            final = index_job.status(slowlib)
            if not final.get("active"):
                break
            time.sleep(0.05)
        check("a cancelled index really ends",
              final.get("state") == "cancelled"
              and final.get("active") is False, str(final)[:300])
        check("the half-written inventory line is trimmed",
              slow_inv.read_bytes().endswith(b"\n")
              and b"half" not in slow_inv.read_bytes(),
              repr(slow_inv.read_bytes()[-90:]))
        check("a cancelled index keeps what it wrote",
              index_job.counts(slowlib)["tracks"] == 3,
              str(index_job.counts(slowlib)))
        idle_control = index_job.pause(slowlib)
        check("pausing a stopped index changes nothing",
              idle_control.get("state") == "cancelled"
              and "note" in idle_control, str(idle_control)[:200])

        slow_service = ChatService(root=slowlib, use_llm=False,
                                   auto_psycho=False)
        try:
            bogus = slow_service.control_index("rewind")
            check("an unknown index action is refused",
                  "error" in bogus, str(bogus)[:200])
            quiet = slow_service.control_index("pause")
            check("the service answers an idle pause honestly",
                  "note" in quiet, str(quiet)[:200])
        finally:
            slow_service.engine.close()

        # -- switching the music folder (the Settings dialog) --------------
        moved = service.settings({"library": str(joblib)})
        result = moved.get("library_result") or {}
        if result.get("applied"):
            check("a readable library switches live",
                  Path(service.root).resolve() == joblib.resolve(),
                  str(result)[:200])
            check("state follows the new library",
                  Path(service.state()["root"]).resolve() == joblib.resolve(),
                  service.state()["root"])
            check("the choice is remembered for the next start",
                  service.state()["settings"]["library_saved"] == str(joblib.resolve()),
                  str(service.state()["settings"])[:300])
            check("the choice is written to the start folder",
                  (root / "library-work" / "app" / "chat"
                   / "settings.json").is_file())
            service.engine.close()
        else:
            check("a readable library switches live", False, str(result)[:200])
        # put the service back on the test library for the remaining checks
        service.engine = Engine(root=root, use_llm=False)
        service.root = service.engine.root

        # -- the extractor itself (only when the library scripts are there)
        check_agent(root)
        check_http(root)
        check_phrase_lines()
        check_answer_links()
        check_play_intent(engine)
        check_session(engine, root)
        check_work(work)
        check_downloads(work)
        check_inventory_refresh(work)
        check_loudness(work)
        check_recent(work)
        check_health(work)
        check_duplicates(work)
        check_arrivals(work)
        check_tagedit(work)
        check_smart(work)
        check_stats(work)
        check_profile_page(work)
        check_radio(work)
        check_endless(work)
        check_explain(work)
        check_pwa(work)
        check_window()
        check_journey(work)
        check_library_data(work)

        extractor = HERE.parent.parent / "scripts" / "psycho_features.py"
        if extractor.is_file():
            check_extractor(work)

        engine.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
