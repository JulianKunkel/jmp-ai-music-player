"""Turn a mood request into actual tracks.

The heavy lifting is done once per process: every track gets the ten measured
mood scores (or a tag-only estimate when it was never analysed), standardised
against the whole library.  A request is then a weighted blend of those moods
plus everything the profile has learned, with hard filters for what the
listener explicitly does not want.

The result is ordered to flow: calm lists rise gently, party lists build to a
peak, focus lists stay steady.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from .catalog import FEATURE_KEYS, Track, fold, human_duration
from .moodmodel import (CALM_GENRES, LOUD_GENRES, MOODS, Ranker, gauss,
                        score_track, tag_only_scores)

#: mood words -> blend (worlds, not exclusives).  English and German.
MOOD_WORDS: dict[str, dict[str, float]] = {
    "smooth":     {"Chill": 0.60, "Romantic": 0.25, "Focus": 0.15},
    "smooth jazz": {"Chill": 0.50, "Romantic": 0.20, "Focus": 0.30},
    "chill":      {"Chill": 1.0},
    "chilled":    {"Chill": 1.0},
    "chillout":   {"Chill": 0.80, "Focus": 0.20},
    "relax":      {"Chill": 1.0},
    "relaxed":    {"Chill": 1.0},
    "calm":       {"Chill": 0.75, "Focus": 0.25},
    "soft":       {"Chill": 0.55, "Romantic": 0.45},
    "sanft":      {"Chill": 0.55, "Romantic": 0.45},
    "mellow":     {"Chill": 0.7, "Melancholy": 0.3},
    "ruhig":      {"Chill": 0.8, "Focus": 0.2},
    "entspannt":  {"Chill": 0.8, "Focus": 0.2},
    "entspannen": {"Chill": 0.8, "Focus": 0.2},
    "entspannung": {"Chill": 0.8, "Focus": 0.2},
    "relaxing":   {"Chill": 0.9, "Focus": 0.1},
    "easy":       {"Chill": 0.7, "Happy": 0.3},
    "ambient":    {"Chill": 0.6, "Focus": 0.4},
    "sleep":      {"Chill": 0.6, "Romantic": 0.2, "Melancholy": 0.2},
    "einschlafen": {"Chill": 0.6, "Romantic": 0.2, "Melancholy": 0.2},
    "happy":      {"Happy": 1.0},
    "glücklich":  {"Happy": 1.0},
    "fröhlich":   {"Happy": 1.0},
    "froehlich":  {"Happy": 1.0},
    "good vibes": {"Happy": 0.7, "Chill": 0.3},
    "sunny":      {"Happy": 0.7, "Chill": 0.3},
    "party":      {"Party": 1.0},
    "feiern":     {"Party": 1.0},
    "feier":      {"Party": 1.0},
    "dance":      {"Party": 0.8, "Happy": 0.2},
    "tanz":       {"Party": 0.8, "Happy": 0.2},
    "dancing":    {"Party": 0.8, "Happy": 0.2},
    "club":       {"Party": 0.8, "Workout": 0.2},
    "workout":    {"Workout": 1.0},
    "sport":      {"Workout": 1.0},
    "gym":        {"Workout": 1.0},
    "running":    {"Workout": 1.0},
    "laufen":     {"Workout": 1.0},
    "power":      {"Workout": 0.7, "Epic": 0.3},
    "dark":       {"Dark": 1.0},
    "düster":     {"Dark": 1.0},
    "duester":    {"Dark": 1.0},
    "gothic":     {"Dark": 0.8, "Epic": 0.2},
    "melancholy": {"Melancholy": 1.0},
    "melancholisch": {"Melancholy": 1.0},
    "sad":        {"Melancholy": 1.0},
    "traurig":    {"Melancholy": 1.0},
    "epic":       {"Epic": 1.0},
    "episch":     {"Epic": 1.0},
    "cinematic":  {"Epic": 0.8, "Focus": 0.2},
    "filmisch":   {"Epic": 0.8, "Focus": 0.2},
    "orchestral": {"Epic": 0.7, "Focus": 0.3},
    "romantic":   {"Romantic": 1.0},
    "romantisch": {"Romantic": 1.0},
    "love":       {"Romantic": 0.8, "Chill": 0.2},
    "liebe":      {"Romantic": 0.8, "Chill": 0.2},
    "ballads":    {"Romantic": 0.8, "Melancholy": 0.2},
    "balladen":   {"Romantic": 0.8, "Melancholy": 0.2},
    "focus":      {"Focus": 1.0},
    "konzentration": {"Focus": 1.0},
    "concentrate": {"Focus": 1.0},
    "study":      {"Focus": 0.8, "Chill": 0.2},
    "lernen":     {"Focus": 0.8, "Chill": 0.2},
    "work":       {"Focus": 0.65, "Chill": 0.25, "Instrumental": 0.10},
    "arbeiten":   {"Focus": 0.65, "Chill": 0.25, "Instrumental": 0.10},
    "büro":       {"Focus": 0.8, "Chill": 0.2},
    "buero":      {"Focus": 0.8, "Chill": 0.2},
    "coding":     {"Focus": 0.8, "Chill": 0.2},
    "reading":    {"Focus": 0.6, "Chill": 0.4},
    "retro":      {"Retro": 1.0},
    "8-bit":      {"Retro": 1.0},
    "8bit":       {"Retro": 1.0},
    "chiptune":   {"Retro": 1.0},
    "c64":        {"Retro": 1.0},
    "oldies":     {"Retro": 0.7, "Happy": 0.3},
    "instrumental": {"Focus": 0.6, "Chill": 0.4},
    # intensity words: these were missing, so "aggressive music" fell through
    "aggressive": {"Workout": 0.55, "Dark": 0.30, "Epic": 0.15},
    "aggressiv":  {"Workout": 0.55, "Dark": 0.30, "Epic": 0.15},
    "angry":      {"Dark": 0.55, "Workout": 0.35, "Epic": 0.10},
    "wütend":     {"Dark": 0.55, "Workout": 0.35, "Epic": 0.10},
    "wuetend":    {"Dark": 0.55, "Workout": 0.35, "Epic": 0.10},
    "heavy":      {"Workout": 0.45, "Dark": 0.40, "Epic": 0.15},
    "hart":       {"Workout": 0.45, "Dark": 0.40, "Epic": 0.15},
    "intense":    {"Workout": 0.40, "Epic": 0.40, "Dark": 0.20},
    "intensiv":   {"Workout": 0.40, "Epic": 0.40, "Dark": 0.20},
    "energetic":  {"Workout": 0.60, "Party": 0.30, "Happy": 0.10},
    "energetisch": {"Workout": 0.60, "Party": 0.30, "Happy": 0.10},
    "powerful":   {"Epic": 0.50, "Workout": 0.40, "Dark": 0.10},
    "driving":    {"Workout": 0.55, "Epic": 0.30, "Focus": 0.15},
    "treibend":   {"Workout": 0.55, "Epic": 0.30, "Focus": 0.15},
    "uplifting":  {"Happy": 0.55, "Epic": 0.30, "Party": 0.15},
    "dreamy":     {"Chill": 0.50, "Romantic": 0.30, "Melancholy": 0.20},
    "verträumt":  {"Chill": 0.50, "Romantic": 0.30, "Melancholy": 0.20},
    "vertraeumt": {"Chill": 0.50, "Romantic": 0.30, "Melancholy": 0.20},
    "nostalgic":  {"Retro": 0.50, "Melancholy": 0.30, "Chill": 0.20},
    "nostalgisch": {"Retro": 0.50, "Melancholy": 0.30, "Chill": 0.20},
}

#: a genre-only recommendation still needs a mood blend
GENRE_MOODS: dict[str, dict[str, float]] = {
    "Metal":        {"Workout": 0.45, "Dark": 0.35, "Epic": 0.20},
    "Rock":         {"Workout": 0.40, "Epic": 0.35, "Happy": 0.25},
    "Punk":         {"Workout": 0.55, "Party": 0.25, "Dark": 0.20},
    "Gothic":       {"Dark": 0.60, "Epic": 0.30, "Melancholy": 0.10},
    "Industrial":   {"Dark": 0.55, "Workout": 0.35, "Epic": 0.10},
    "Techno":       {"Workout": 0.50, "Party": 0.40, "Dark": 0.10},
    "Trance":       {"Workout": 0.45, "Epic": 0.35, "Party": 0.20},
    "Electronic":   {"Workout": 0.40, "Focus": 0.35, "Party": 0.25},
    "Dance":        {"Party": 0.60, "Happy": 0.30, "Workout": 0.10},
    "House":        {"Party": 0.55, "Workout": 0.35, "Happy": 0.10},
    "Jazz":         {"Chill": 0.50, "Romantic": 0.30, "Happy": 0.20},
    "Blues":        {"Chill": 0.40, "Melancholy": 0.35, "Romantic": 0.25},
    "Soul":         {"Romantic": 0.45, "Chill": 0.35, "Happy": 0.20},
    "Funk":         {"Happy": 0.45, "Party": 0.40, "Chill": 0.15},
    "Classical":    {"Epic": 0.40, "Focus": 0.40, "Romantic": 0.20},
    "Folk":         {"Chill": 0.50, "Melancholy": 0.30, "Romantic": 0.20},
    "Ambient":      {"Chill": 0.80, "Focus": 0.20},
    "New Age":      {"Chill": 0.70, "Focus": 0.30},
    "Soundtrack":   {"Epic": 0.55, "Focus": 0.30, "Romantic": 0.15},
    "Chiptune":     {"Retro": 0.75, "Happy": 0.25},
    "Rap":          {"Workout": 0.50, "Dark": 0.30, "Party": 0.20},
    "Hip-Hop":      {"Workout": 0.50, "Party": 0.30, "Dark": 0.20},
    "Pop":          {"Happy": 0.60, "Party": 0.30, "Romantic": 0.10},
    "Reggae":       {"Happy": 0.60, "Chill": 0.40},
    "Country":      {"Chill": 0.45, "Happy": 0.35, "Melancholy": 0.20},
    "Latin":        {"Party": 0.55, "Happy": 0.35, "Romantic": 0.10},
    "Musical":      {"Epic": 0.45, "Happy": 0.35, "Romantic": 0.20},
    "Comedy":       {"Happy": 0.60, "Party": 0.40},
    "Instrumental": {"Focus": 0.60, "Chill": 0.40},
}


def moods_for_genres(genres: list[str] | tuple[str, ...]) -> dict[str, float]:
    """A usable mood blend for a genre-only request ("recommend metal")."""
    blend: dict[str, float] = {}
    for genre in genres:
        for mood, weight in GENRE_MOODS.get(genre, {}).items():
            blend[mood] = blend.get(mood, 0.0) + weight
    if not blend:
        return {}
    total = sum(blend.values())
    if total > 1.4:
        blend = {mood: weight * 1.4 / total for mood, weight in blend.items()}
    return {mood: round(weight, 3) for mood, weight in blend.items()
            if weight > 0.02}

#: phrase -> mood blend for requests that are not a single word
MOOD_PHRASES: list[tuple[str, dict[str, float]]] = [
    ("for work", {"Focus": 0.65, "Chill": 0.25, "Instrumental": 0.10}),
    ("for the office", {"Focus": 0.75, "Chill": 0.25}),
    ("für die arbeit", {"Focus": 0.65, "Chill": 0.25, "Instrumental": 0.10}),
    ("fuer die arbeit", {"Focus": 0.65, "Chill": 0.25, "Instrumental": 0.10}),
    ("beim arbeiten", {"Focus": 0.65, "Chill": 0.25, "Instrumental": 0.10}),
    ("zum einschlafen", {"Chill": 0.6, "Romantic": 0.2, "Melancholy": 0.2}),
    ("zum entspannen", {"Chill": 0.8, "Focus": 0.2}),
    ("beim sport", {"Workout": 1.0}),
    ("zum tanzen", {"Party": 0.8, "Happy": 0.2}),
    ("good vibes", {"Happy": 0.7, "Chill": 0.3}),
]

#: explicit genre words in a request -> catalogue genre
GENRE_WORDS = {
    "jazz": "Jazz", "metal": "Metal", "rock": "Rock", "punk": "Punk",
    "classical": "Classical", "klassik": "Classical", "electronic": "Electronic",
    "elektronik": "Electronic", "techno": "Techno", "folk": "Folk",
    "hip-hop": "Hip-Hop", "hiphop": "Hip-Hop", "rap": "Rap",
    "soundtrack": "Soundtrack", "filmmusik": "Soundtrack", "anime": "Soundtrack",
    "game": "Soundtrack", "spiele": "Soundtrack", "chiptune": "Chiptune",
    "ambient": "Ambient", "pop": "Pop", "reggae": "Reggae",
    "country": "Country", "blues": "Blues", "indie": "Indie",
    "gothic": "Gothic", "industrial": "Industrial", "folk": "Folk",
    "soul": "Soul", "funk": "Funk", "latin": "Latin",
}

#: moods that should never include the loud guitar/electronic families
CALM_MOODS = {"Chill", "Romantic", "Focus", "Melancholy"}


def fold_mood(word: str) -> str:
    return fold(word).strip()


def moods_from_text(text: str, profile=None) -> dict[str, float]:
    """Extract a weighted mood blend from a free-text request."""
    folded = " ".join(fold(text).split())
    blend: dict[str, float] = {}

    # longest phrases first, so "for work" beats the bare "work"
    for phrase, weights in sorted(MOOD_PHRASES, key=lambda item: -len(item[0])):
        if phrase in folded:
            for mood, weight in weights.items():
                blend[mood] = blend.get(mood, 0.0) + weight
            folded = folded.replace(phrase, " ")

    # learned phrases win over the built-in lexicon
    if profile is not None:
        for word, moods in profile.data.get("mappings", {}).items():
            if word and word in folded:
                for mood in moods:
                    if mood in MOODS:
                        blend[mood] = blend.get(mood, 0.0) + 0.9
                folded = folded.replace(word, " ")

    words = [w.strip(".,!?;:()[]") for w in folded.split()]
    for index, word in enumerate(words):
        if word in MOOD_WORDS:
            for mood, weight in MOOD_WORDS[word].items():
                if mood == "Instrumental":
                    continue
                blend[mood] = blend.get(mood, 0.0) + weight

    if "Instrumental" in blend:
        # handled by the genre preference, not a mood score
        blend.pop("Instrumental", None)

    # normalise so 1.0 means "a single clear mood"
    total = sum(blend.values())
    if total > 1.4:
        blend = {mood: weight * 1.4 / total for mood, weight in blend.items()}
    return {mood: round(weight, 3) for mood, weight in blend.items()
            if weight > 0.02}


@dataclass
class Selection:
    tracks: list[Track]
    moods: dict[str, float]
    dominant: dict[str, int]
    total_s: float
    tempo_range: tuple[float, float] | None
    seed: Track | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> str:
        return human_duration(self.total_s)

    def summary(self) -> str:
        mix = " · ".join(f"{mood} {count}"
                         for mood, count in sorted(self.dominant.items(),
                                                   key=lambda kv: -kv[1]))
        bits = [f"{len(self.tracks)} tracks · {self.duration}"]
        if mix:
            bits.append(mix)
        if self.tempo_range:
            low, high = self.tempo_range
            bits.append(f"{int(low)}–{int(high)} BPM")
        return " · ".join(bits)


class Selector:
    """Builds and caches the per-track score table."""

    def __init__(self, catalog, profile):
        self.catalog = catalog
        self.profile = profile
        self._rows: list[dict] | None = None
        self._track_index: dict[str, Track] = {}
        self._stamp = None

    # -- the score table ---------------------------------------------------

    def _prepare(self) -> list[dict]:
        stamp = (self.catalog.inventory_path.stat().st_mtime
                 if self.catalog.inventory_path.exists() else 0.0,
                 self.catalog.features_path.stat().st_mtime
                 if self.catalog.features_path.exists() else 0.0)
        if self._rows is not None and self._stamp == stamp:
            return self._rows

        feats = self.catalog.features()
        ranker = Ranker({
            key: [float(f.get(key) or 0) for f in feats.values()]
            for key in ("energy_db", "dynamic_db", "centroid_hz",
                        "percussiveness", "mode_score", "key_strength")
        })

        prepared: list[dict] = []
        raw_scores: list[dict[str, float]] = []
        tag_scores: list[dict[str, float]] = []
        for row in self.catalog.rows:
            ext = (row.get("ext") or "").lower()
            if ext not in {".mp3", ".mp2", ".m4a", ".mp4", ".opus", ".ogg",
                           ".flac", ".wav", ".wma", ".aac"}:
                continue
            duration = float(row.get("duration_s") or 0)
            if duration < 30:
                continue
            rel = row["rel_path"]
            feature = feats.get(rel)
            track = Track.from_row(row)
            if feature:
                merged = dict(row)
                merged.update({key: feature.get(key) for key in FEATURE_KEYS})
                raw = score_track(merged, ranker)
                raw_scores.append(raw)
                prepared.append({
                    "track": track, "row": row, "feature": feature,
                    "raw": raw, "z": {}, "energy": 0.0, "tempo": 0.0,
                    "has_features": True,
                })
            else:
                raw = tag_only_scores(row)
                tag_scores.append(raw)
                prepared.append({
                    "track": track, "row": row, "feature": None,
                    "raw": raw, "z": {}, "energy": 0.0, "tempo": 0.0,
                    "has_features": False,
                })

        # standardise the mood scales against the library
        if raw_scores:
            means = {mood: statistics.fmean(s[mood] for s in raw_scores)
                     for mood in MOODS}
            stdevs = {mood: (statistics.pstdev([s[mood] for s in raw_scores])
                             or 1.0) for mood in MOODS}
        else:
            means = {mood: 0.0 for mood in MOODS}
            stdevs = {mood: 1.0 for mood in MOODS}
        tag_means = ({mood: statistics.fmean(s[mood] for s in tag_scores)
                      for mood in MOODS} if tag_scores
                     else {mood: 0.0 for mood in MOODS})
        tag_stdevs = ({mood: (statistics.pstdev([s[mood] for s in tag_scores])
                              or 1.0) for mood in MOODS} if tag_scores
                      else {mood: 1.0 for mood in MOODS})

        for item in prepared:
            raw = item["raw"]
            if item["has_features"]:
                item["z"] = {mood: (raw[mood] - means[mood]) / stdevs[mood]
                             for mood in MOODS}
                item["energy"] = ranker.rank(
                    "energy_db", float(item["feature"].get("energy_db") or -40))
                item["tempo"] = float(item["feature"].get("tempo_bpm") or 0)
            else:
                # tag-only tracks get a conservative estimate, scaled into the
                # same range as the measured z-scores
                item["z"] = {mood: 0.55 * (raw[mood] - tag_means[mood])
                             / tag_stdevs[mood] for mood in MOODS}
                item["energy"] = min(1.0, max(0.0, (raw["Workout"]
                                                    - raw["Chill"] + 2) / 4))
                item["tempo"] = 0.0

        self._rows = prepared
        self._track_index = {item["track"].rel: item["track"]
                             for item in prepared}
        self._stamp = stamp
        return prepared

    # -- selection ---------------------------------------------------------

    def seed_moods(self, track: Track, top: int = 3) -> dict[str, float]:
        """The moods a track itself leans toward, for "more like this"."""
        for item in self._prepare():
            if item["track"].rel != track.rel:
                continue
            z = item["z"]
            ranked = sorted(((mood, z.get(mood, 0.0)) for mood in MOODS),
                            key=lambda pair: -pair[1])
            picked = [(mood, value) for mood, value in ranked[:top]
                      if value > 0.05]
            if not picked:
                return {}
            best = picked[0][1] or 1.0
            return {mood: round(value / best, 3) for mood, value in picked}
        return {}

    def build(self, moods: dict[str, float] | None = None, *,
              count: int | None = None, duration_min: float | None = None,
              seed: Track | None = None, include_genres: list[str] | None = None,
              exclude_genres: list[str] | None = None,
              exclude_artists: list[str] | None = None,
              only_genres: list[str] | None = None,
              max_per_artist: int | None = None,
              min_duration: float = 45.0,
              allow_recent: bool = False,
              request: str = "") -> Selection:
        moods = dict(moods or {"Chill": 1.0})
        moods = {mood: float(weight) for mood, weight in moods.items()
                 if mood in MOODS and weight > 0}
        if not moods:
            moods = {"Chill": 1.0}
        total_weight = sum(moods.values()) or 1.0

        include = {fold(g) for g in (include_genres or [])}
        exclude = {fold(g) for g in (exclude_genres or [])}
        only = {fold(g) for g in (only_genres or [])}
        exclude_artists_f = {fold(a) for a in (exclude_artists or [])}

        # an explicit artist filter wins over the diversity cap
        seed_artist = fold(seed.artist) if seed and seed.artist else ""
        if max_per_artist is None:
            if seed_artist:
                max_per_artist = 6
            else:
                max_per_artist = max(2, int(math.ceil((count or 40) / 12)))

        calm = sum(weight for mood, weight in moods.items()
                   if mood in CALM_MOODS) / total_weight
        loud = sum(weight for mood, weight in moods.items()
                   if mood in {"Party", "Workout", "Dark"}) / total_weight

        seed_tempo = 0.0
        seed_energy = 0.0
        if seed is not None:
            seed_feature = self.catalog.features().get(seed.rel) or {}
            seed_tempo = float(seed_feature.get("tempo_bpm") or 0)
            if seed_feature:
                seed_energy = 0.5

        candidates: list[tuple[float, dict]] = []
        recent = set(self.profile.recent_paths(30))
        for item in self._prepare():
            track: Track = item["track"]
            row = item["row"]
            if track.duration_s < min_duration:
                continue
            if track.grouping.strip() == "Sprache":
                continue
            genre_f = fold(track.genre)
            grouping_f = fold(track.grouping)
            artist_f = fold(track.artist)

            if only and not any(g in genre_f for g in only):
                continue
            if any(g in genre_f or g in grouping_f for g in exclude):
                continue
            if exclude_artists_f and any(a in artist_f for a in exclude_artists_f):
                continue
            if track.artist and self.profile.artist_bias(track.artist) <= -0.6:
                continue
            if track.genre and self.profile.genre_bias(track.genre) <= -0.65:
                continue

            z = item["z"]
            base = sum(moods[mood] * z.get(mood, 0.0)
                       for mood in moods) / total_weight

            # measured calm/loud guard: a "smooth" list must not contain the
            # metal/techno families even if their keyword score sneaks in
            if calm >= 0.6 and track.genre in LOUD_GENRES:
                base -= 1.2
            if loud >= 0.6 and track.genre in CALM_GENRES and track.genre != "Classical":
                base -= 0.35

            boost = (0.55 * self.profile.artist_bias(track.artist)
                     + 0.35 * self.profile.genre_bias(track.genre)
                     + 0.25 * max(self.profile.mood_bias(mood)
                                  for mood in moods))
            words = set(fold(f"{track.title} {track.album}").split())
            keyword_bonus = sum(
                min(0.5, weight * 0.2)
                for word, weight in self.profile.data.get("keywords", {}).items()
                if word in words)
            boost += keyword_bonus

            if any(g in genre_f for g in include):
                boost += 0.8
            if any(g in genre_f for g in only):
                boost += 0.5

            if seed is not None and track.rel != seed.rel:
                if seed.artist and fold(seed.artist) == artist_f:
                    boost += 1.7
                if seed.album and fold(seed.album) == fold(track.album) \
                        and seed.artist and fold(seed.artist) == artist_f:
                    boost += 0.6
                if seed.genre and fold(seed.genre) == genre_f:
                    boost += 0.35
                if seed_tempo and item["tempo"]:
                    boost += 0.55 * gauss(item["tempo"], seed_tempo, 0.28)
                if seed_energy and item["energy"]:
                    boost += 0.25 * (1.0 - abs(item["energy"] - seed_energy))

            if not allow_recent and track.rel in recent:
                boost -= 0.55

            item["score"] = base + boost
            candidates.append((item["score"], item))

        if not candidates:
            return Selection(tracks=[], moods=moods, dominant={}, total_s=0.0,
                             tempo_range=None, seed=seed,
                             notes=["nothing matched the filters"])

        candidates.sort(key=lambda pair: (-pair[0], pair[1]["track"].rel))

        # choose with a per-artist cap; relax it when the library is thin
        chosen: list[dict] = []
        per_artist: dict[str, int] = {}
        target_total = float(duration_min) * 60 if duration_min else 0.0
        accumulated = 0.0
        wanted = count or (1000 if duration_min else 40)

        def take(limit_cap: int) -> None:
            nonlocal accumulated
            for score, item in candidates:
                if len(chosen) >= wanted:
                    return
                track = item["track"]
                if any(track.rel == other["track"].rel for other in chosen):
                    continue
                artist_key = fold(track.artist)
                if artist_key and per_artist.get(artist_key, 0) >= limit_cap:
                    continue
                chosen.append(item)
                if artist_key:
                    per_artist[artist_key] = per_artist.get(artist_key, 0) + 1
                accumulated += track.duration_s
                if target_total and accumulated >= target_total:
                    return

        take(max_per_artist)
        if len(chosen) < min(wanted, 5):
            take(max_per_artist + 3)
        if not chosen:
            take(10_000)

        # "more like this" must actually contain the artist it was seeded with
        if seed is not None and (seed.artist or seed.rel):
            seed_key = fold(seed.artist) if seed.artist else ""
            def is_seed(item: dict) -> bool:
                if seed_key:
                    return fold(item["track"].artist) == seed_key
                return item["track"].rel == seed.rel
            have = sum(1 for item in chosen if is_seed(item))
            if have < 4:
                extra = [item for _, item in candidates
                         if is_seed(item)
                         and all(item["track"].rel != c["track"].rel
                                 for c in chosen)][:4 - have]
                for item in extra:
                    if len(chosen) >= wanted:
                        # replace the weakest non-seed entry instead of growing
                        victim = next((index for index in range(len(chosen) - 1, -1, -1)
                                       if not is_seed(chosen[index])), None)
                        if victim is None:
                            break
                        accumulated -= chosen[victim]["track"].duration_s
                        chosen.pop(victim)
                    chosen.append(item)
                    accumulated += item["track"].duration_s
            if seed.rel and all(item["track"].rel != seed.rel for item in chosen):
                chosen.insert(0, {"track": seed, "row": {}, "feature": None,
                                  "raw": {}, "z": {}, "energy": 0.0,
                                  "tempo": 0.0, "has_features": False,
                                  "score": 0.0})
                accumulated += seed.duration_s

        if target_total:
            # do not overshoot the requested length by a whole extra track
            while (len(chosen) > 1
                   and accumulated - chosen[-1]["track"].duration_s >= target_total):
                accumulated -= chosen[-1]["track"].duration_s
                chosen.pop()
            # one long ambient track can still blow the budget: drop it and
            # top the list up with shorter picks instead of overrunning badly
            while accumulated > target_total * 1.10 and len(chosen) > 1:
                longest = max(range(len(chosen)),
                              key=lambda index:
                              chosen[index]["track"].duration_s)
                size = chosen[longest]["track"].duration_s
                if accumulated - size < target_total * 0.8:
                    break
                accumulated -= size
                chosen.pop(longest)
                used = {item["track"].rel for item in chosen}
                for _, item in candidates:
                    track = item["track"]
                    if track.rel in used:
                        continue
                    if accumulated + track.duration_s > target_total:
                        continue
                    chosen.append(item)
                    accumulated += track.duration_s
                    used.add(track.rel)
                    if accumulated >= target_total * 0.97:
                        break

        # -- flow ----------------------------------------------------------
        if calm >= 0.6:
            chosen.sort(key=lambda item: (item["energy"], -item["score"]))
        elif loud >= 0.6:
            chosen.sort(key=lambda item: item["energy"])
            third = max(1, len(chosen) // 3)
            low = chosen[:third]
            middle = chosen[third:-third] if len(chosen) > 3 * third else []
            high = chosen[-third:]
            chosen = low + middle + list(reversed(high))
        else:
            chosen.sort(key=lambda item: -item["score"])

        tracks = [item["track"] for item in chosen]
        total_s = sum(track.duration_s for track in tracks)
        dominant: dict[str, int] = {}
        for item in chosen:
            z = item["z"]
            best = max(moods, key=lambda mood: z.get(mood, 0.0))
            dominant[best] = dominant.get(best, 0) + 1
        tempos = [item["tempo"] for item in chosen if item["tempo"]]
        tempo_range = None
        if tempos:
            tempos.sort()
            tempo_range = (tempos[max(0, len(tempos) // 10)],
                           tempos[min(len(tempos) - 1,
                                      (len(tempos) * 9) // 10)])
        notes = []
        if not self.catalog.has_features():
            notes.append("no mood analysis in this library yet — "
                         "selected from genre and tags")
        return Selection(tracks=tracks, moods=moods, dominant=dominant,
                         total_s=total_s, tempo_range=tempo_range,
                         seed=seed, notes=notes)
