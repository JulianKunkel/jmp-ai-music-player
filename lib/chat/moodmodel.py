"""The measured-feature mood model, shared by the chat's playlist builder.

This mirrors ``scripts/make_mood_playlists.py``: the ten mood scores, the genre
and shelf priors and the keyword lists are the same, so a playlist the chat
builds for "chill" agrees with ``Mood_Chill.m3u8`` instead of inventing a
second, contradictory notion of chill.

Differences from the script:

* the ten raw scores are standardised (z-scored) against the whole library so
  a mood whose raw score is simply high for everything cannot win by default;
* the caller can blend several moods with weights, which is what "smooth for
  work" needs;
* a track without measured features still gets a tag-only estimate, so a
  library that has never run the mood analyser can still produce a playlist.
"""

from __future__ import annotations

import math
import re

MOODS = ["Chill", "Happy", "Party", "Workout", "Dark",
         "Melancholy", "Epic", "Romantic", "Focus", "Retro"]

MOOD_TITLE = {
    "Chill": "ruhig, entspannt, Downtempo",
    "Happy": "fröhlich, hell, Dur",
    "Party": "Tanz, Club, Beat",
    "Workout": "Power, schnell, hart",
    "Dark": "düster, Nacht, Spannung",
    "Melancholy": "traurig, nachdenklich",
    "Epic": "orchestral, filmisch, groß",
    "Romantic": "Liebe, sanft, Balladen",
    "Focus": "instrumental, gleichmäßig",
    "Retro": "Chiptune, C64, 8-Bit, Oldies",
}

GENRE = {
    "Ambient":        {"Chill": 2.2, "Focus": 1.4, "Melancholy": 0.4},
    "New Age":        {"Chill": 1.8, "Focus": 1.4, "Epic": 0.5},
    "Easy Listening": {"Chill": 1.6, "Romantic": 0.7},
    "Classical":      {"Epic": 1.4, "Focus": 1.0, "Romantic": 0.7,
                       "Chill": 0.5},
    "Instrumental":   {"Focus": 1.5, "Chill": 0.6, "Epic": 0.5},
    "Electronic":     {"Party": 1.0, "Focus": 0.9, "Chill": 0.4},
    "Trance":         {"Party": 1.8, "Workout": 1.0, "Focus": 0.5},
    "Techno":         {"Party": 2.0, "Workout": 1.1, "Dark": 0.5},
    "Dance":          {"Party": 2.0, "Happy": 0.6},
    "Rock":           {"Workout": 0.7, "Happy": 0.5, "Dark": 0.4},
    "Metal":          {"Workout": 1.5, "Dark": 1.1, "Epic": 0.4},
    "Punk":           {"Workout": 1.3, "Happy": 0.5},
    "Industrial":     {"Dark": 1.7, "Workout": 0.7},
    "Gothic":         {"Dark": 1.6, "Melancholy": 0.7, "Epic": 0.4},
    "Pop":            {"Happy": 1.0, "Party": 0.7, "Romantic": 0.4},
    "Indie":          {"Melancholy": 0.8, "Happy": 0.5},
    "Folk":           {"Chill": 0.8, "Romantic": 0.7, "Melancholy": 0.6},
    "Acoustic":       {"Chill": 1.0, "Romantic": 1.0},
    "Jazz":           {"Chill": 1.0, "Romantic": 0.8, "Focus": 0.6},
    "Blues":          {"Melancholy": 1.0, "Chill": 0.7},
    "Country":        {"Chill": 0.6, "Melancholy": 0.5, "Happy": 0.4},
    "Vocal":          {"Romantic": 1.4, "Chill": 0.5},
    "Musical":        {"Happy": 1.2, "Epic": 0.7, "Romantic": 0.4},
    "Comedy":         {"Happy": 1.4},
    "Soundtrack":     {"Epic": 0.8, "Dark": 0.3, "Happy": 0.2},
    "Chiptune":       {"Retro": 2.4, "Happy": 0.6},
    "Hip-Hop":        {"Party": 1.2, "Workout": 0.7},
    "Rap":            {"Party": 1.0, "Workout": 0.7},
    "Reggae":         {"Chill": 1.0, "Happy": 0.7},
    "Latin":          {"Party": 1.2, "Happy": 0.7},
    "World":          {"Chill": 0.5, "Party": 0.5},
    "Drum & Bass":    {"Workout": 1.7, "Party": 0.9},
    "Trip-Hop":       {"Chill": 1.2, "Dark": 0.6},
}

SHELF = {
    "Ambience":          {"Chill": 1.1, "Focus": 0.7},
    "VerschiedenesSlow": {"Chill": 0.9},
    "Techno":            {"Party": 1.2, "Workout": 0.4},
    "Dancefloor":        {"Party": 1.2},
    "DanceSuper":        {"Party": 1.2},
    "ComputerMusic":     {"Party": 0.6, "Focus": 0.6},
    "Metal":             {"Workout": 0.7, "Dark": 0.7},
    "Gothic":            {"Dark": 1.1},
    "Mystisch":          {"Dark": 0.9, "Epic": 0.4},
    "Mittelalter":       {"Dark": 0.5, "Epic": 0.4},
    "Klassisch":         {"Epic": 1.3, "Focus": 0.5},
    "Orchestral":        {"Epic": 1.3, "Focus": 0.6},
    "Musical":           {"Happy": 0.8, "Epic": 0.5},
    "Oldies":            {"Retro": 0.9, "Happy": 0.4},
    "OldiesSuper":       {"Retro": 0.9, "Happy": 0.4},
    "Tanzlieder":        {"Retro": 0.4, "Party": 0.4, "Happy": 0.4},
    "Fun":               {"Happy": 0.8, "Party": 0.4},
    "Themes/Animes":     {"Happy": 0.2, "Epic": 0.2},
    "Themes/Filme":      {"Epic": 0.3},
    "Themes/Spiele":     {"Epic": 0.2},
    "Themes/C64":        {"Retro": 2.2},
    "Tracker":           {"Retro": 2.0},
    "Themes/Tabaluga":   {"Happy": 0.5},
    "DeutschRapetc":     {"Party": 0.5},
    "Hiphop-Rap.etc":    {"Party": 0.5},
    "Punk":              {"Workout": 0.6},
}

KEYWORDS = {
    "Happy": ["happy", "glück", "glueck", "sun", "sonne", "sunshine",
              "summer", "sommer", "fun", "spaß", "spass", "joy", "freude",
              "laugh", "smile", "lächeln", "feier", "celebration", "holiday"],
    "Party": ["party", "dance", "tanz", "club", "disco", "rave", "beat",
              "techno", "celebration", "feiern"],
    "Workout": ["power", "energy", "fight", "run", "speed", "härte",
                "haerte", "workout", "gym"],
    "Dark": ["dark", "dunkel", "night", "nacht", "shadow", "schatten",
             "demon", "dämon", "daemon", "death", "tod", "blood", "blut",
             "fear", "angst", "horror", "ghost", "geist", "hell", "hölle",
             "hoelle", "sinister", "evil", "böse", "boese"],
    "Melancholy": ["sad", "traurig", "tears", "tränen", "traenen", "lonely",
                   "einsam", "alone", "allein", "lost", "verloren", "cry",
                   "wein", "rain", "regen", "winter", "goodbye", "farewell",
                   "abschied", "memories", "erinnerung"],
    "Epic": ["epic", "battle", "schlacht", "war", "krieg", "hero", "held",
             "quest", "legend", "legende", "saga", "adventure", "abenteuer",
             "orchestra", "symphony", "sinfonie", "kingdom", "königreich"],
    "Romantic": ["love", "liebe", "heart", "herz", "kiss", "kuss", "romance",
                 "romantik", "lover", "baby", "darling", "schatz", "träum",
                 "traeum"],
    "Chill": ["chill", "relax", "entspann", "calm", "ruhig", "sleep",
              "schlaf", "dream", "traum", "soft", "sanft", "quiet", "still",
              "gentle", "zart", "ambient", "lullaby", "wiegenlied"],
    "Retro": ["retro", "8-bit", "8bit", "chiptune", "c64", "amiga", "arcade",
              "pixel", "nintendo", "sega", "commodore"],
}

LOUD_GENRES = {"Metal", "Punk", "Industrial", "Techno", "Trance",
               "Drum & Bass", "Thrash Metal", "Hardcore"}
CALM_GENRES = {"Ambient", "New Age", "Easy Listening", "Classical",
               "Instrumental"}
CALM_SHELVES = {"Ambience", "VerschiedenesSlow", "Klassisch", "Orchestral",
                "Musical"}
LOUD_SHELVES = {"Techno", "Dancefloor", "DanceSuper", "Metal", "Punk",
                "ComputerMusic", "Gothic"}


def gauss(value: float, center: float, width_oct: float) -> float:
    if value <= 0:
        return 0.0
    return math.exp(-0.5 * (math.log2(value / center) / width_oct) ** 2)


class Ranker:
    """Percentile ranks for features whose absolute units are not portable."""

    def __init__(self, values: dict[str, list[float]]):
        self.sorted = {key: sorted(vals) for key, vals in values.items()}

    def rank(self, key: str, value: float) -> float:
        arr = self.sorted.get(key) or []
        if not arr:
            return 0.5
        lo, hi = 0, len(arr)
        while lo < hi:
            mid = (lo + hi) // 2
            if arr[mid] < value:
                lo = mid + 1
            else:
                hi = mid
        return lo / (len(arr) - 1) if len(arr) > 1 else 0.5


def score_track(row: dict, rank: Ranker) -> dict[str, float]:
    """The ten raw mood scores for one row (features already merged in)."""
    tempo = float(row.get("tempo_bpm") or 0)
    energy = rank.rank("energy_db", float(row.get("energy_db") or -40))
    dyn = rank.rank("dynamic_db", float(row.get("dynamic_db") or 0))
    bright = rank.rank("centroid_hz", float(row.get("centroid_hz") or 1500))
    perc = rank.rank("percussiveness", float(row.get("percussiveness") or 0))
    mode = rank.rank("mode_score", float(row.get("mode_score") or 0))
    key = rank.rank("key_strength", float(row.get("key_strength") or 0))

    s = {mood: 0.0 for mood in MOODS}
    s["Chill"] += 1.7 * (1 - energy) + 1.3 * (1 - perc) \
        + 1.1 * gauss(tempo, 82, 0.45) + 0.7 * (1 - bright) + 0.3 * (1 - dyn)
    s["Happy"] += 1.3 * mode + 1.0 * bright \
        + 0.9 * gauss(tempo, 120, 0.40) + 0.5 * energy
    s["Party"] += 1.5 * perc + 1.2 * energy \
        + 1.4 * gauss(tempo, 124, 0.25) + 0.4 * bright
    s["Workout"] += 1.8 * energy + 1.2 * perc \
        + 1.2 * gauss(tempo, 148, 0.35) + 0.4 * bright
    s["Dark"] += 1.6 * (1 - mode) + 0.9 * (1 - bright) \
        + 0.5 * gauss(tempo, 110, 0.55) + 0.3 * (1 - dyn)
    s["Melancholy"] += 1.4 * (1 - mode) + 1.0 * (1 - bright) \
        + 0.9 * (1 - energy) + 0.8 * gauss(tempo, 78, 0.50)
    s["Epic"] += 1.5 * dyn + 1.0 * (1 - perc) + 0.6 * key \
        + 0.5 * gauss(tempo, 95, 0.55) + 0.3 * bright
    s["Romantic"] += 1.3 * (1 - perc) + 1.0 * (1 - energy) \
        + 0.9 * gauss(tempo, 80, 0.45) + 0.5 * mode
    s["Focus"] += 1.6 * (1 - perc) + 1.1 * (1 - dyn) \
        + 0.8 * (1 - energy) + 0.3 * key
    s["Retro"] += 0.4 * bright + 0.2 * perc

    genre = (row.get("genre") or "").strip()
    shelf = (row.get("grouping") or "").strip()
    for mood, weight in GENRE.get(genre, {}).items():
        s[mood] += weight
    for mood, weight in SHELF.get(shelf, {}).items():
        s[mood] += weight

    if genre == "Chiptune" or shelf in {"Themes/C64", "Tracker"} \
            or str(row.get("rel_path") or "").startswith("Chiptune/"):
        s["Retro"] += 2.0

    if genre in LOUD_GENRES:
        for mood in ("Chill", "Romantic", "Focus"):
            s[mood] -= 0.5
    if genre in CALM_GENRES:
        s["Workout"] -= 0.5
        s["Party"] -= 0.5
    if shelf in LOUD_SHELVES:
        for mood in ("Chill", "Romantic", "Focus", "Melancholy"):
            s[mood] -= 0.5
    if shelf in CALM_SHELVES:
        s["Workout"] -= 0.5
        s["Party"] -= 0.5

    text = " ".join((row.get("title") or "", row.get("album") or "")).casefold()
    hits = set(re.findall(r"[0-9a-zäöüßà-ÿ]+", text))
    for mood, keys in KEYWORDS.items():
        count = sum(1 for key in keys if key in hits)
        if count:
            s[mood] += min(1.6, 0.8 * count)
    return s


def tag_only_scores(row: dict) -> dict[str, float]:
    """A tag-based estimate for tracks that have no measured features."""
    s = {mood: 0.0 for mood in MOODS}
    genre = (row.get("genre") or "").strip()
    shelf = (row.get("grouping") or "").strip()
    for mood, weight in GENRE.get(genre, {}).items():
        s[mood] += weight
    for mood, weight in SHELF.get(shelf, {}).items():
        s[mood] += weight
    if genre == "Chiptune" or shelf in {"Themes/C64", "Tracker"}:
        s["Retro"] += 2.0
    text = " ".join((row.get("title") or "", row.get("album") or "")).casefold()
    hits = set(re.findall(r"[0-9a-zäöüßà-ÿ]+", text))
    for mood, keys in KEYWORDS.items():
        count = sum(1 for key in keys if key in hits)
        if count:
            s[mood] += min(1.0, 0.5 * count)
    return s
