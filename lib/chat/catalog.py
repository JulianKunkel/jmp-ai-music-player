"""The library as the chat sees it: search, playlists, statistics.

Only read-only access lives here.  Anything that writes goes through the
journalled tools (`playlist_edit.py`, `add_file.py`).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

AUDIO_EXT = {".mp3", ".mp2", ".m4a", ".mp4", ".opus", ".ogg", ".flac",
             ".wav", ".wma", ".aac", ".spc", ".sid", ".mod", ".xm", ".it",
             ".s3m", ".ym", ".play"}

#: a playlist file names a list, not a track
PLAYLIST_EXT = {".m3u", ".m3u8", ".pls"}

#: dropped from a search only when another, meaningful token is left
SEARCH_STOP = {"the", "a", "an", "of", "and", "or", "der", "die", "das",
               "den", "dem", "des", "ein", "eine", "einer", "und", "oder",
               "von", "vom", "zum", "zur", "me", "my", "mir", "alle", "alles",
               "some", "any", "in", "on", "at", "im", "am"}

#: fields the mood analyser measures
FEATURE_KEYS = ("tempo_bpm", "energy_db", "dynamic_db", "centroid_hz",
                "percussiveness", "mode_score", "key_strength")


def fold(text: str) -> str:
    """Case- and accent-insensitive comparison (ä == a, ß == ss)."""
    text = unicodedata.normalize("NFKD", str(text).casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.replace("ß", "ss")


def library_rel(rel_path: str) -> str:
    """The inventory still stores `Music/` for pre-move rows."""
    return rel_path[len("Music/"):] if rel_path.startswith("Music/") else rel_path


def _playlist_key(name: str) -> str:
    """Compare names without caring about case, accents or separators."""
    return " ".join(fold(name).replace("_", " ").replace("-", " ").split())


def playlist_kind(path: Path) -> str:
    """Which shelf a playlist file belongs to (custom, mood, focus, psycho)."""
    if "Custom" in path.parts:
        return "custom"
    if "Psycho" in path.parts:
        return "psycho"
    if "Smart" in path.parts:
        return "smart"
    if path.parent.name == "Moods":
        return "focus" if path.name.startswith("Mood_Focus_") else "mood"
    return "shelf"


def clock(seconds) -> str:
    try:
        total = int(round(float(seconds or 0)))
    except (TypeError, ValueError):
        return "--:--"
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def human_duration(seconds: float) -> str:
    seconds = int(round(float(seconds or 0)))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"


@dataclass(frozen=True)
class Track:
    rel: str
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    grouping: str = ""
    duration_s: float = 0.0
    year: str = ""
    track: str = ""
    ext: str = ""

    @property
    def path(self) -> Path:
        return self.rel  # resolved by the caller against the library root

    @property
    def name(self) -> str:
        return self.title or Path(self.rel).stem.replace("_", " ")

    @property
    def who(self) -> str:
        return self.artist or "Unknown artist"

    def label(self, with_album: bool = False) -> str:
        text = f"{self.who} — {self.name}"
        if with_album and self.album:
            text += f"  ({self.album})"
        return f"{text}  [{clock(self.duration_s)}]"

    def one_line(self) -> str:
        album = f" · {self.album}" if self.album else ""
        genre = f" · {self.genre}" if self.genre else ""
        return f"{self.label()}{album}{genre}"

    @classmethod
    def from_row(cls, row: dict) -> "Track":
        return cls(
            rel=library_rel(row.get("rel_path") or ""),
            title=(row.get("title") or "").strip(),
            artist=(row.get("artist") or row.get("albumartist") or "").strip(),
            album=(row.get("album") or "").strip(),
            genre=(row.get("genre") or "").strip(),
            grouping=(row.get("grouping") or "").strip(),
            duration_s=float(row.get("duration_s") or 0),
            year=(row.get("date") or "").strip(),
            track=(row.get("track") or "").strip(),
            ext=(row.get("ext") or "").strip().lower(),
        )


@dataclass
class Playlist:
    name: str
    path: Path
    kind: str            # shelf | mood | focus | custom
    count: int = 0

    @property
    def rel(self) -> str:
        return self.path.name

    def label(self) -> str:
        return f"{self.name}  ({self.count:,} tracks, {self.kind})"


@dataclass
class Resolve:
    kind: str                       # url | file | playlist | all | search | none
    target: str | None = None
    label: str = ""
    track: Track | None = None
    playlist: Playlist | None = None
    candidates: list[Track] = field(default_factory=list)


class Catalog:
    """A lazily loaded, self-refreshing view of the library."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.inventory_path = self.root / "library-work" / "inventory" / "files.jsonl"
        self.features_path = self.root / "library-work" / "moods" / "features.jsonl"
        self.playlists_dir = self.root / "Playlists"
        self._rows: list[dict] | None = None
        self._by_rel: dict[str, dict] = {}
        self._features: dict[str, dict] | None = None
        self._playlists: list[Playlist] | None = None
        self._stamp: tuple[float, float] = (0.0, 0.0)

    # -- loading -----------------------------------------------------------

    def _stat(self) -> tuple[float, float]:
        def mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0
        return mtime(self.inventory_path), mtime(self.features_path)

    def reload(self) -> None:
        self._rows = None
        self._features = None
        self._playlists = None
        self._load_rows()

    def _load_rows(self) -> list[dict]:
        if self._rows is not None and self._stamp == self._stat():
            return self._rows
        rows: list[dict] = []
        by_rel: dict[str, dict] = {}
        try:
            raw = self.inventory_path.read_bytes()
        except OSError:
            raw = b""
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                row = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            rel = library_rel(row.get("rel_path") or "")
            if not rel:
                continue
            row = dict(row, rel_path=rel)
            rows.append(row)
            by_rel[rel] = row
        self._rows = rows
        self._by_rel = by_rel
        self._stamp = self._stat()
        return rows

    @property
    def rows(self) -> list[dict]:
        return self._load_rows()

    def features(self) -> dict[str, dict]:
        if self._features is not None:
            return self._features
        found: dict[str, dict] = {}
        try:
            raw = self.features_path.read_bytes()
        except OSError:
            raw = b""
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                row = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if row.get("error"):
                continue
            rel = library_rel(row.get("rel_path") or "")
            if rel:
                found[rel] = row
        self._features = found
        return found

    def has_features(self) -> bool:
        return bool(self.features())

    # -- tracks ------------------------------------------------------------

    def track(self, rel: str) -> Track | None:
        rel = library_rel(rel)
        self._load_rows()
        row = self._by_rel.get(rel)
        if row is not None:
            return Track.from_row(row)
        if (self.root / rel).is_file():
            return Track(rel=rel, title=Path(rel).stem.replace("_", " "),
                         ext=Path(rel).suffix.lower())
        return None

    def path(self, track: Track | str) -> Path:
        rel = track.rel if isinstance(track, Track) else track
        return self.root / rel

    def all_tracks(self) -> list[Track]:
        return [Track.from_row(r) for r in self.rows
                if (r.get("ext") or "").lower() in AUDIO_EXT]

    # -- search ------------------------------------------------------------

    def _score_row(self, row: dict, tokens: list[str]) -> float | None:
        title = fold(row.get("title") or "")
        artist = fold(row.get("artist") or "")
        albumartist = fold(row.get("albumartist") or "")
        album = fold(row.get("album") or "")
        genre = fold(row.get("genre") or "")
        grouping = fold(row.get("grouping") or "")
        path = fold(library_rel(row.get("rel_path") or ""))
        # the extension must not score: every MP3 would match a "3" query
        for suffix in AUDIO_EXT:
            if path.endswith(suffix):
                path = path[:-len(suffix)]
                break
        score = 0.0
        for token in tokens:
            hit = False
            if token in title:
                score += 3.0
                hit = True
                if title.startswith(token):
                    score += 0.6
            if token in artist or token in albumartist:
                score += 3.0
                hit = True
            if token in album:
                score += 1.6
                hit = True
            if token in genre or token in grouping:
                score += 1.1
                hit = True
            if token in path:
                score += 0.6
                hit = True
            if not hit:
                return None
        # a shorter title that contains the whole query is a better hit
        score -= len(title) * 0.002
        return score

    def search(self, query: str, limit: int = 15) -> list[Track]:
        # "Merlin_wooden.mp3" is a file name, not a phrase: the extension says
        # nothing about the track, so it is dropped before scoring
        text = re.sub(r"(?i)\.(?:mp3|mp2|m4a|mp4|flac|ogg|oga|opus|wav|wma|aac|"
                      r"webm|spx|mod|xm|it|s3m|sid|spc|ym|play)$",
                      "", str(query or "").strip())
        tokens = [t for t in re.split(r"\s+", fold(text)) if t]
        if len(tokens) > 1:
            main = [t for t in tokens if t not in SEARCH_STOP]
            if main:
                tokens = main
        if not tokens:
            return []
        scored: list[tuple[float, str, dict]] = []
        for row in self.rows:
            if (row.get("ext") or "").lower() not in AUDIO_EXT:
                continue
            score = self._score_row(row, tokens)
            if score is not None:
                scored.append((score, library_rel(row.get("rel_path") or ""),
                               row))
        scored.sort(key=lambda item: (-item[0], item[1].casefold()))
        return [Track.from_row(row) for _, _, row in scored[:limit]]

    def search_playlists(self, query: str, limit: int = 6) -> list[Playlist]:
        """Playlists whose name matches the words — "chill" finds Mood_Chill.

        A playlist is a thing the search box can find like a file: every word
        has to appear somewhere in the name (case, accents and `_`/`-` do not
        matter), an exact name wins, then a name that starts with the query.
        """
        text = str(query or "").strip()
        # "Bands.m3u8" is the file name of the playlist "Bands"
        text = re.sub(r"(?i)\.(?:m3u8?|pls)$", "", text)
        wanted = _playlist_key(text)
        words = [word for word in wanted.split() if word]
        if not words:
            return []
        scored: list[tuple[int, Playlist]] = []
        for playlist in self.playlists():
            key = _playlist_key(playlist.name)
            if not key or not all(word in key for word in words):
                continue
            score = 0
            if key == wanted:
                score += 100
            elif key.startswith(wanted):
                score += 40
            score += sum(len(word) for word in words if word in key.split())
            scored.append((score, playlist))
        scored.sort(key=lambda pair: (-pair[0], fold(pair[1].name)))
        return [playlist for _, playlist in scored[:limit]]

    def by_genre(self, genre: str, limit: int = 500) -> list[Track]:
        """Every track whose genre or old shelf is that value ("play jazz")."""
        key = fold(genre)
        if not key:
            return []
        hits = [Track.from_row(row) for row in self.rows
                if key in fold(row.get("genre") or "")
                or key in fold(row.get("grouping") or "")]
        hits.sort(key=lambda t: (fold(t.artist), fold(t.album), t.track))
        return hits[:limit]

    def by_artist(self, artist: str, limit: int = 50) -> list[Track]:
        key = fold(artist)
        hits = [Track.from_row(row) for row in self.rows
                if key in fold(row.get("artist") or "")
                or key in fold(row.get("albumartist") or "")]
        hits.sort(key=lambda t: (fold(t.album), t.track, fold(t.title)))
        return hits[:limit]

    def artists(self) -> list[str]:
        found = {row.get("albumartist") or row.get("artist") or ""
                 for row in self.rows}
        return sorted({name.strip() for name in found if name.strip()},
                      key=fold)

    # -- playlists ---------------------------------------------------------

    def playlists(self, refresh: bool = False) -> list[Playlist]:
        if self._playlists is not None and not refresh:
            return self._playlists
        found: list[Playlist] = []
        groups = (
            ("shelf", self.playlists_dir, "*.m3u8"),
            ("mood", self.playlists_dir / "Moods", "Mood_*.m3u8"),
            ("focus", self.playlists_dir / "Moods", "Mood_Focus_*.m3u8"),
            ("custom", self.playlists_dir / "Custom", "*.m3u8"),
            ("own", self.playlists_dir / "Own", "*.m3u8"),
            ("smart", self.playlists_dir / "Smart", "*.m3u8"),
            ("psycho", self.playlists_dir / "Psycho", "*.m3u8"),
        )
        seen: set[Path] = set()
        for kind, base, pattern in groups:
            if not base.is_dir():
                continue
            for path in sorted(base.glob(pattern)):
                if not path.is_file() or path in seen:
                    continue
                seen.add(path)
                # Mood_*.m3u8 also matches Mood_Focus_*; keep the focus kind
                real_kind = kind
                if kind == "mood" and path.name.startswith("Mood_Focus_"):
                    real_kind = "focus"
                found.append(Playlist(path.stem, path, real_kind,
                                      self._count_entries(path)))
        found.sort(key=lambda p: (p.kind, fold(p.name)))
        self._playlists = found
        return found

    @staticmethod
    def _count_entries(path: Path) -> int:
        try:
            return sum(1 for line in path.read_text(encoding="utf-8-sig")
                       .splitlines() if line.strip() and not line.startswith("#"))
        except OSError:
            return 0

    def find_playlist(self, text: str) -> Playlist | None:
        handle = text.strip()
        if Path(handle).suffix.lower() in PLAYLIST_EXT:
            candidate = Path(handle)
            if candidate.is_file():
                path = candidate.resolve()
                return Playlist(path.stem, path, playlist_kind(path),
                                self._count_entries(path))
            handle = candidate.stem
        handle = handle.split(":", 1)[-1] if ":" in handle else handle
        wanted = fold(handle)
        if not wanted:
            return None
        # "Test Mix" and "Test_Mix" name the same list
        wanted_key = _playlist_key(handle)
        exact = [p for p in self.playlists() if _playlist_key(p.name) == wanted_key]
        if exact:
            # a hand-made list wins over a generated list of the same name
            exact.sort(key=lambda p: 0 if p.kind == "custom" else 1)
            return exact[0]
        # "chill" should find Mood_Chill
        prefixed = [p for p in self.playlists()
                    if _playlist_key(p.name) in {f"mood {wanted_key}",
                                                 f"mood focus {wanted_key}"}]
        if prefixed:
            return prefixed[0]
        partial = [p for p in self.playlists() if wanted_key in _playlist_key(p.name)]
        if len(partial) == 1:
            return partial[0]
        return None

    def playlist_tracks(self, playlist: Playlist) -> list[Track]:
        found: list[Track] = []
        try:
            lines = playlist.path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            return found
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            target = (playlist.path.parent / line).resolve()
            try:
                rel = target.relative_to(self.root).as_posix()
            except ValueError:
                continue
            track = self.track(rel)
            if track is not None:
                found.append(track)
        return found

    def mood_playlist(self, word: str) -> Playlist | None:
        wanted = _playlist_key(word)
        for playlist in self.playlists():
            stem = _playlist_key(playlist.name)
            if playlist.kind in {"mood", "focus"} and (
                    stem == f"mood {wanted}" or stem == f"mood focus {wanted}"):
                return playlist
        for playlist in self.playlists():
            if playlist.kind in {"mood", "focus"} and wanted in _playlist_key(playlist.name):
                return playlist
        return None

    # -- resolution --------------------------------------------------------

    def resolve(self, text: str) -> Resolve:
        text = (text or "").strip()
        if not text:
            return Resolve(kind="none")
        if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE):
            return Resolve(kind="url", target=text, label=text)

        lowered = fold(text)
        if lowered in {"all", "everything", "alles", "ganze bibliothek",
                       "bibliothek", "library", "sammlung", "my library",
                       "meine bibliothek"}:
            playlist = self.find_playlist("All")
            if playlist:
                return Resolve(kind="all", target=str(playlist.path),
                               label="the whole library", playlist=playlist)

        for candidate in (Path(text).expanduser(),):
            for base in (Path.cwd(), self.root):
                probe = candidate if candidate.is_absolute() else base / candidate
                if probe.is_file() and probe.suffix.lower() in PLAYLIST_EXT:
                    # a playlist *file* is the list, not a track in it
                    path = probe.resolve()
                    playlist = Playlist(path.stem, path, playlist_kind(path),
                                        self._count_entries(path))
                    return Resolve(kind="playlist", target=str(path),
                                   label=playlist.name, playlist=playlist)
                if probe.is_file():
                    try:
                        rel = probe.resolve().relative_to(self.root).as_posix()
                    except ValueError:
                        rel = ""
                    track = self.track(rel) if rel else None
                    return Resolve(kind="file", target=str(probe.resolve()),
                                   label=str(probe), track=track)

        playlist = self.find_playlist(text)
        if playlist is not None:
            return Resolve(kind="playlist", target=str(playlist.path),
                           label=playlist.name, playlist=playlist)

        hits = self.search(text, 10)
        if len(hits) == 1:
            return Resolve(kind="search", target=str(self.path(hits[0])),
                           label=hits[0].label(), track=hits[0],
                           candidates=hits)
        return Resolve(kind="search", label=text, candidates=hits)

    # -- statistics --------------------------------------------------------

    def stats(self) -> dict:
        audio = [r for r in self.rows
                 if (r.get("ext") or "").lower() in AUDIO_EXT]
        total_s = sum(float(r.get("duration_s") or 0) for r in audio)
        artists = {r.get("albumartist") or r.get("artist") or ""
                   for r in audio}
        genres: dict[str, int] = {}
        for row in audio:
            genre = (row.get("genre") or "").strip()
            if genre:
                genres[genre] = genres.get(genre, 0) + 1
        top_genres = sorted(genres.items(), key=lambda kv: -kv[1])[:8]
        kinds: dict[str, int] = {}
        for playlist in self.playlists():
            kinds[playlist.kind] = kinds.get(playlist.kind, 0) + 1
        return {
            "files": len(audio),
            "hours": total_s / 3600.0,
            "artists": len({a for a in artists if a}),
            "genres": top_genres,
            "playlists": kinds,
            "features": len(self.features()),
            "root": str(self.root),
        }
