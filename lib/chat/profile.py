"""What the app has learned about the listener.

The profile is a plain JSON file in the library's work folder, so it survives
container restarts (the container's HOME is temporary) and travels with the
music folder:

    library-work/app/chat/profile.json

Everything is a weight, not a rule.  A positive number means "more of this",
a negative one means "less of this"; the selection engine maps the weights
through ``tanh`` so no single enthusiasm can drown out the measured audio
features.  Explicit feedback ("I like this") weighs much more than an implicit
signal (a track skipped after ten seconds).

Nothing here touches a music file.  This is app state only.
"""

from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STOPWORDS = {
    "the", "and", "with", "from", "that", "this", "eine", "einen", "einer",
    "eines", "und", "oder", "aber", "für", "fuer", "mit", "ohne", "von",
    "vom", "zum", "zur", "ein", "ich", "mir", "mich", "mein", "meine",
    "music", "musik", "song", "songs", "track", "tracks", "lied", "lieder",
    "album", "playlist", "liste", "more", "mehr", "like", "wie", "bitte",
    "please", "etwas", "something", "want", "will", "möchte", "moechte",
}

# Words worth learning as a taste when the listener asks for them explicitly.
LEARNABLE_KEYWORDS = {
    "piano", "klavier", "guitar", "gitarre", "bass", "drums", "schlagzeug",
    "synth", "synthesizer", "sax", "saxophone", "violin", "geige", "cello",
    "flute", "flöte", "floete", "trumpet", "trompete", "organ", "orgel",
    "acoustic", "akustik", "instrumental", "vocal", "vocals", "gesang",
    "female", "male", "deutsch", "german", "englisch", "english", "lofi",
    "lo-fi", "ambient", "orchestral", "orchester", "electronic", "elektronik",
    "live", "acoustic", "piano", "ballad", "ballade", "rock", "metal", "jazz",
    "classical", "klassik", "folk", "pop", "punk", "rap", "hiphop", "reggae",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.replace("ß", "ss")


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[0-9a-zäöüßà-ÿ]+", fold(text))
            if len(w) >= 3 and w not in STOPWORDS]


class Profile:
    """Loads, queries and updates the listener profile."""

    def __init__(self, root: Path | None = None):
        self.root = root
        self.path = (root / "library-work" / "app" / "chat" / "profile.json"
                     if root else None)
        self.data: dict[str, Any] = self._empty()
        self.loaded = False
        self.writable = True
        self.load()

    # -- persistence -------------------------------------------------------

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": 1,
            "created": now(),
            "updated": now(),
            "artists": {},
            "genres": {},
            "moods": {},
            "keywords": {},
            "notes": [],
            "mappings": {},
            "playlists": [],
            "events": [],
            "recent": [],
            "settings": {
                "autoplay": True,
                "default_count": 40,
                "use_llm": True,
                "diversity": 3,
            },
        }

    def load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        base = self._empty()
        for key, value in data.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                base[key].update(value)
            else:
                base[key] = value
        self.data = base
        self.loaded = True

    def save(self) -> bool:
        if not self.path:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.data["updated"] = now()
            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            self.writable = True
            return True
        except OSError:
            # a read-only library must not break the conversation
            self.writable = False
            return False

    # -- generic weights ---------------------------------------------------

    @property
    def settings(self) -> dict[str, Any]:
        return self.data.setdefault("settings", {})

    def setting(self, key: str, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key: str, value) -> None:
        self.settings[key] = value
        self.save()

    def score(self, bucket: str, key: str) -> float:
        if not key:
            return 0.0
        return float(self.data.get(bucket, {}).get(key, 0.0))

    @staticmethod
    def bias(score: float) -> float:
        """A bounded, explainable influence: +1 for a strong favourite."""
        return math.tanh(float(score) / 2.5)

    def artist_bias(self, artist: str) -> float:
        return self.bias(self.score("artists", artist))

    def genre_bias(self, genre: str) -> float:
        return self.bias(self.score("genres", genre))

    def mood_bias(self, mood: str) -> float:
        return self.bias(self.score("moods", mood))

    def _bump(self, bucket: str, key: str, amount: float,
              cap: float = 8.0) -> None:
        if not key:
            return
        table = self.data.setdefault(bucket, {})
        value = float(table.get(key, 0.0)) + amount
        value = max(-cap, min(cap, value))
        if abs(value) < 0.01:
            table.pop(key, None)
        else:
            table[key] = round(value, 3)
        self.save()

    # -- explicit feedback -------------------------------------------------

    def like_track(self, track, amount: float = 1.0,
                   source: str = "explicit") -> list[str]:
        changes = []
        if getattr(track, "artist", ""):
            self._bump("artists", track.artist, amount)
            changes.append(f"artist {track.artist}")
        if getattr(track, "genre", ""):
            self._bump("genres", track.genre, amount * 0.7)
            changes.append(f"genre {track.genre}")
        for word in words(f"{track.title} {track.album}"):
            if word in LEARNABLE_KEYWORDS:
                self._bump("keywords", word, amount * 0.5)
                changes.append(f"keyword {word}")
        self.event("like" if amount > 0 else "dislike", getattr(track, "rel", ""),
                   source, changes)
        return changes

    def dislike_track(self, track, amount: float = 1.0,
                      source: str = "explicit") -> list[str]:
        return self.like_track(track, -amount, source)

    def like_mood(self, mood: str, amount: float = 1.0) -> None:
        self._bump("moods", mood, amount)
        self.event("like-mood", mood, "explicit", [mood])

    def note(self, text: str) -> str:
        text = " ".join(str(text).split()).strip()
        if not text:
            return ""
        notes = self.data.setdefault("notes", [])
        notes.insert(0, {"text": text, "at": now()})
        del notes[80:]
        self.save()
        return text

    def forget(self, value: str) -> list[str]:
        """Set every learned weight matching `value` back to neutral."""
        key = fold(value).strip()
        removed: list[str] = []
        for bucket in ("artists", "genres", "moods", "keywords"):
            table = self.data.get(bucket, {})
            for name in list(table):
                if not key or key == fold(name) or key in fold(name):
                    table.pop(name, None)
                    removed.append(f"{bucket}: {name}")
        if not key:
            self.data["notes"] = []
            self.data["mappings"] = {}
            removed.append("all notes and learned phrases")
        self.save()
        return removed

    def add_mapping(self, word: str, moods: list[str]) -> None:
        word = fold(word).strip()
        if not word:
            return
        self.data.setdefault("mappings", {})[word] = list(dict.fromkeys(moods))
        self.save()

    def mapping(self, word: str) -> list[str]:
        return list(self.data.get("mappings", {}).get(fold(word), []))

    # -- implicit signals --------------------------------------------------

    def recent_paths(self, limit: int = 30) -> list[str]:
        rows = self.data.get("recent", [])
        return [r.get("rel", "") for r in rows[:limit] if r.get("rel")]

    def was_played(self, rel: str, within: int = 40) -> bool:
        return rel in self.recent_paths(within)

    def observe(self, track, fraction: float, dominant_mood: str = "") -> None:
        """Learn from how much of a track was actually heard.

        Short skips only count for real songs (not 20-second cues), and the
        same track does not teach the profile twice in a row.
        """
        rel = getattr(track, "rel", "")
        if not rel:
            return
        duration = float(getattr(track, "duration_s", 0) or 0)
        recent = self.data.setdefault("recent", [])
        first = not recent or recent[0].get("rel") != rel
        recent.insert(0, {
            "rel": rel,
            "at": now(),
            "fraction": round(float(fraction), 3),
        })
        del recent[120:]
        interesting = duration >= 45
        if first and interesting:
            if fraction >= 0.65:
                self._bump("artists", track.artist, 0.12)
                self._bump("genres", track.genre, 0.08)
                if dominant_mood:
                    self._bump("moods", dominant_mood, 0.08)
                self.event("listened", rel, "implicit", [])
            elif fraction <= 0.15:
                self._bump("artists", track.artist, -0.08)
                self._bump("genres", track.genre, -0.05)
                self.event("skipped", rel, "implicit", [])
        self.save()

    def event(self, kind: str, value: str, source: str,
              changes: list[str] | None = None) -> None:
        events = self.data.setdefault("events", [])
        events.insert(0, {
            "kind": kind,
            "value": value,
            "source": source,
            "changes": changes or [],
            "at": now(),
        })
        del events[300:]
        self.save()

    # -- playlists ---------------------------------------------------------

    def record_playlist(self, name: str, moods: dict[str, float],
                        count: int, request: str = "") -> None:
        rows = self.data.setdefault("playlists", [])
        rows.insert(0, {
            "name": name,
            "moods": dict(moods),
            "count": count,
            "request": request,
            "at": now(),
        })
        del rows[80:]
        self.save()

    # -- rendering ---------------------------------------------------------

    def top(self, bucket: str, limit: int = 6,
            positive: bool = True) -> list[tuple[str, float]]:
        table = self.data.get(bucket, {})
        items = [(k, float(v)) for k, v in table.items()
                 if (v > 0 if positive else v < 0)]
        items.sort(key=lambda kv: -abs(kv[1]) if not positive else -kv[1])
        return items[:limit]

    def summary_markdown(self) -> str:
        lines = []
        artists = self.top("artists")
        genres = self.top("genres")
        moods = self.top("moods")
        if artists:
            lines.append("- favourite artists: " + ", ".join(
                f"{name} ({value:+.1f})" for name, value in artists))
        if genres:
            lines.append("- favourite genres: " + ", ".join(
                f"{name} ({value:+.1f})" for name, value in genres))
        if moods:
            lines.append("- favourite moods: " + ", ".join(
                f"{name} ({value:+.1f})" for name, value in moods))
        disliked_artists = self.top("artists", positive=False)
        if disliked_artists:
            lines.append("- avoiding: " + ", ".join(
                name for name, _ in disliked_artists))
        if self.data.get("notes"):
            lines.append("- notes: " + " · ".join(
                n["text"] for n in self.data["notes"][:5]))
        if self.data.get("mappings"):
            lines.append("- learned phrases: " + ", ".join(
                f"{word} = {'/'.join(moods)}"
                for word, moods in list(self.data["mappings"].items())[:8]))
        playlists = self.data.get("playlists", [])
        if playlists:
            lines.append("- recent playlists: " + ", ".join(
                p.get("name", "?") for p in playlists[:5]))
        return "\n".join(lines) if lines else "(nothing learned yet)"

    def compact(self, limit: int = 8) -> str:
        """One-line summary for the LLM system prompt."""
        bits = []
        for bucket in ("artists", "genres", "moods"):
            top = self.top(bucket, limit=limit)
            if top:
                bits.append(f"{bucket}: " + ", ".join(
                    f"{name} {value:+.1f}" for name, value in top))
        disliked = self.top("artists", limit=limit, positive=False)
        if disliked:
            bits.append("avoid: " + ", ".join(name for name, _ in disliked))
        if self.data.get("notes"):
            bits.append("notes: " + " | ".join(
                n["text"] for n in self.data["notes"][:3]))
        return "; ".join(bits) or "no preferences learned yet"
