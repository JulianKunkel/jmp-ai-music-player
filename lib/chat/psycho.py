"""Read the psychoacoustic profile database (library-work/app/psycho).

The measurements are written by `scripts/psycho_features.py`, the ten 0..1
axes and the family/cluster labels by `scripts/psycho_profile.py`.  Nothing
here writes: the app only reads the SQLite database and turns a row into the
material the chat, the API and the net diagram need.

Every axis is a percentile rank over this library, so 0.5 is average and 0.9
is "among the top 10 %".  That is what makes the diagram and the similarity
search comparable across tracks of very different mastering.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

#: the ten axes, with the wording the UI and the chat use
AXES = (
    {"key": "speed", "label": "Speed", "low": "slow", "high": "fast",
     "hint": "tempo in beats per minute"},
    {"key": "energy", "label": "Energy", "low": "quiet", "high": "loud",
     "hint": "loudness, onsets, spectral flux and high-frequency power"},
    {"key": "aggression", "label": "Aggression", "low": "gentle",
     "high": "aggressive",
     "hint": "loud, noisy, rough, fast and minor"},
    {"key": "brightness", "label": "Brightness", "low": "dark", "high": "bright",
     "hint": "spectral centroid and rolloff"},
    {"key": "danceability", "label": "Danceability", "low": "freeform",
     "high": "danceable", "hint": "beat clarity, onsets and a tempo near 120"},
    {"key": "valence", "label": "Valence", "low": "melancholy", "high": "upbeat",
     "hint": "major mode, brightness, speed and low roughness"},
    {"key": "calmness", "label": "Calmness", "low": "intense", "high": "calm",
     "hint": "quiet, few onsets, slow, wide dynamics"},
    {"key": "density", "label": "Density", "low": "sparse", "high": "dense",
     "hint": "onsets per second and spectral flux"},
    {"key": "roughness", "label": "Roughness", "low": "smooth", "high": "rough",
     "hint": "sensory dissonance, flatness and zero-crossings"},
    {"key": "harmony", "label": "Harmony", "low": "dissonant", "high": "harmonic",
     "hint": "clear key, tonal spectrum, low roughness"},
    {"key": "punch", "label": "Punch", "low": "soft", "high": "punchy",
     "hint": "crest factor, transients, low band"},
    {"key": "width", "label": "Width", "low": "mono", "high": "wide",
     "hint": "stereo side/mid energy and dynamic range"},
)

AXIS_KEYS = tuple(axis["key"] for axis in AXES)

# --------------------------------------------------------------------------
# profile sets
#
# `AXES` above is the *psychology* set and stays the default: it is what the
# families, the ranking concepts and the chat describe.  The same measured
# columns are also read through three other lenses.  A set only renames and
# regroups; every value is still a percentile rank over this library, so 0.5
# is average inside that set as well.
# --------------------------------------------------------------------------

TIMBRE_AXES = (
    {"key": "warmth", "label": "Warmth", "low": "cool", "high": "warm",
     "hint": "low-band energy and a darker centroid"},
    {"key": "brightness", "label": "Brightness", "low": "dark", "high": "bright",
     "hint": "spectral centroid and rolloff"},
    {"key": "presence", "label": "Presence", "low": "distant", "high": "present",
     "hint": "2-5 kHz band and Zwicker sharpness"},
    {"key": "air", "label": "Air", "low": "closed", "high": "airy",
     "hint": "energy above 4 kHz and spectral spread"},
    {"key": "sharpness", "label": "Sharpness", "low": "soft", "high": "sharp",
     "hint": "Zwicker sharpness in acum"},
    {"key": "harmonicity", "label": "Harmonicity", "low": "inharmonic",
     "high": "harmonic", "hint": "autocorrelation periodicity and tonality"},
    {"key": "noisiness", "label": "Noisiness", "low": "clean", "high": "noisy",
     "hint": "spectral flatness, zero crossings and entropy"},
    {"key": "roughness", "label": "Roughness", "low": "smooth", "high": "rough",
     "hint": "sensory dissonance and flatness"},
)

RHYTHM_AXES = (
    {"key": "speed", "label": "Speed", "low": "slow", "high": "fast",
     "hint": "tempo in beats per minute"},
    {"key": "beat_clarity", "label": "Beat clarity", "low": "hazy",
     "high": "clear", "hint": "how strongly a pulse stands out"},
    {"key": "regularity", "label": "Regularity", "low": "free", "high": "steady",
     "hint": "onsets on the grid and simple intervals"},
    {"key": "onset_density", "label": "Onset density", "low": "sparse",
     "high": "dense", "hint": "onsets per second and transients"},
    {"key": "complexity", "label": "Complexity", "low": "simple",
     "high": "complex", "hint": "entropy of the inter-onset intervals"},
    {"key": "drive", "label": "Drive", "low": "laid-back", "high": "driving",
     "hint": "pulse, transients, onsets and a tempo near 120"},
)

DYNAMICS_AXES = (
    {"key": "loudness", "label": "Loudness", "low": "quiet", "high": "loud",
     "hint": "integrated loudness in LUFS (ITU-R BS.1770)"},
    {"key": "loudness_range", "label": "Loudness range", "low": "compressed",
     "high": "wide", "hint": "EBU Tech 3342 loudness range"},
    {"key": "micro_dynamics", "label": "Micro-dynamics", "low": "flat",
     "high": "lively", "hint": "frame-to-frame level spread"},
    {"key": "punch", "label": "Punch", "low": "soft", "high": "punchy",
     "hint": "crest factor and transients"},
    {"key": "silence", "label": "Silence", "low": "continuous",
     "high": "gappy", "hint": "share of quiet frames"},
    {"key": "trend", "label": "Trend", "low": "falling", "high": "rising",
     "hint": "level change across the window (build-up or fade)"},
)

#: heuristic, not an instrument classifier: band shares and transients
INSTRUMENT_AXES = (
    {"key": "bass", "label": "Bass", "low": "thin", "high": "bass-heavy",
     "hint": "sub-bass and low-band energy"},
    {"key": "drums", "label": "Drums", "low": "sustained", "high": "percussive",
     "hint": "transients, onset rate and crest factor"},
    {"key": "strings", "label": "Strings", "low": "unstring-like",
     "high": "string-like", "hint": "sustained harmonic midrange"},
    {"key": "keys", "label": "Keys", "low": "unkey-like", "high": "key-like",
     "hint": "harmonic presence with a defined attack"},
    {"key": "voice", "label": "Voice", "low": "instrumental", "high": "vocal",
     "hint": "presence-band harmonics and a smooth timbre"},
    {"key": "classical", "label": "Classical", "low": "popular",
     "high": "classical",
     "hint": "wide, quiet, harmonic and sustained (an acoustic estimate)"},
)

#: every set the window can switch to; the first one is the default
SETS = (
    {"key": "psychology", "label": "Psychology", "axes": AXES,
     "note": "Arousal, valence and the classic emotion dimensions."},
    {"key": "timbre", "label": "Timbre", "axes": TIMBRE_AXES,
     "note": "What the sound is made of: warmth, presence, air, noise."},
    {"key": "rhythm", "label": "Rhythm", "axes": RHYTHM_AXES,
     "note": "How it moves: tempo, beat clarity, regularity, drive."},
    {"key": "dynamics", "label": "Dynamics", "axes": DYNAMICS_AXES,
     "note": "How it is mastered: loudness, range, punch, silence."},
    {"key": "instrument", "label": "Instruments", "axes": INSTRUMENT_AXES,
     "note": "Estimated sound sources from band shares and transients — a "
             "heuristic, not a classifier.",
     "estimated": True},
)

SET_KEYS = tuple(item["key"] for item in SETS)
SET_BY_KEY = {item["key"]: item for item in SETS}


def set_key(name: str | None) -> str:
    """Coerce a set name from the UI, the API or a planner."""
    key = str(name or "").strip().casefold().replace(" ", "_")
    return key if key in SET_BY_KEY else "psychology"


def set_spec(name: str | None) -> dict:
    return SET_BY_KEY[set_key(name)]


def set_axes(name: str | None) -> tuple[dict, ...]:
    return set_spec(name)["axes"]


def set_keys(name: str | None) -> tuple[str, ...]:
    return tuple(axis["key"] for axis in set_axes(name))


#: Properties a listener can ask for as a superlative ("the slowest song",
#: "the most boring track").  ``recipe`` maps an axis to a weight: +1 means
#: "more of the axis is more of this property", -1 the opposite, and a
#: fractional value mixes several axes (boredom is quiet, sparse, dark and
#: unexciting at once).  Ranking is a weighted mean of the axes, so the result
#: stays a 0..1 score that can be shown next to the track.
CONCEPTS: dict[str, dict] = {
    "slow": {"recipe": {"speed": -1.0}, "label": "slowest"},
    "fast": {"recipe": {"speed": 1.0}, "label": "fastest"},
    "quiet": {"recipe": {"energy": -1.0}, "label": "quietest"},
    "loud": {"recipe": {"energy": 1.0}, "label": "loudest"},
    "calm": {"recipe": {"calmness": 1.0}, "label": "calmest"},
    "aggressive": {"recipe": {"aggression": 1.0}, "label": "most aggressive"},
    "gentle": {"recipe": {"aggression": -1.0}, "label": "gentlest"},
    "dark": {"recipe": {"brightness": -1.0}, "label": "darkest"},
    "bright": {"recipe": {"brightness": 1.0}, "label": "brightest"},
    "sad": {"recipe": {"valence": -1.0}, "label": "saddest"},
    "happy": {"recipe": {"valence": 1.0}, "label": "happiest"},
    "danceable": {"recipe": {"danceability": 1.0}, "label": "most danceable"},
    "rough": {"recipe": {"roughness": 1.0}, "label": "roughest"},
    "harmonic": {"recipe": {"harmony": 1.0}, "label": "most harmonic"},
    "punchy": {"recipe": {"punch": 1.0}, "label": "punchiest"},
    "wide": {"recipe": {"width": 1.0}, "label": "widest"},
    "dense": {"recipe": {"density": 1.0}, "label": "densest"},
    "sparse": {"recipe": {"density": -1.0}, "label": "sparsest"},
    # boredom is not an axis: it is the absence of everything that makes a
    # track eventful, best expressed as a mix
    "boring": {"recipe": {"energy": -1.0, "density": -0.8, "brightness": -0.6,
                          "punch": -0.6, "calmness": 0.6, "speed": -0.4,
                          "danceability": -0.6},
               "label": "most boring",
               "note": "slow, quiet, sparse, dark and uneventful"},
    "interesting": {"recipe": {"energy": 0.7, "density": 0.8, "brightness": 0.6,
                               "punch": 0.6, "danceability": 0.5},
                    "label": "most interesting"},
    # instrument properties are read from the heuristic `instrument` set; a
    # recipe names its set so `rank` knows which axes to score
    "bassy": {"recipe": {"bass": 1.0}, "label": "most bass-heavy",
              "set": "instrument"},
    "percussive": {"recipe": {"drums": 1.0}, "label": "most percussive",
                   "set": "instrument"},
    "stringy": {"recipe": {"strings": 1.0}, "label": "most string-like",
                "set": "instrument"},
    "keyy": {"recipe": {"keys": 1.0}, "label": "most key-like",
             "set": "instrument"},
    "vocal": {"recipe": {"voice": 1.0}, "label": "most vocal",
              "set": "instrument"},
    "classical": {"recipe": {"classical": 1.0}, "label": "most classical",
                  "set": "instrument"},
}

#: how a listener words those properties (already folded: no umlauts, no case)
CONCEPT_WORDS: dict[str, str] = {}


def _add_words(concept: str, words: str) -> None:
    for word in words.split():
        CONCEPT_WORDS[word] = concept


_add_words("slow", "slow slowest slower langsam langsame langsamer langsamste "
                   "langsamsten entschleunigt")
_add_words("fast", "fast fastest faster schnell schnelle schneller schnellste "
                   "schnellsten uptempo rasend")
_add_words("quiet", "quiet quietest quieter leise leiser leiseste leisester "
                    "leisesten")
_add_words("loud", "loud loudest louder laut laute lauter lauteste lautesten")
_add_words("calm", "calm calmest calmer ruhig ruhige ruhiger ruhigste "
                   "ruhigsten entspannt entspannte entspannteste relaxed "
                   "gelassen")
_add_words("aggressive", "aggressive aggressiv aggressivste "
                         "aggressivsten hard hardest harder hart harte haerte "
                         "haerteste haertesten heavy heaviest metal brutal "
                         "wuetend wutend")
_add_words("gentle", "gentle gentlest zart zarte zarteste zartesten softest "
                     "sanft sanfteste")
_add_words("dark", "dark darkest darker dunkel dunkle dunkler dunkelste "
                   "dunkelsten duester duestere duesterste gloomy")
_add_words("bright", "bright brightest brighter hell helle heller hellste "
                     "hellsten glaenzend")
_add_words("sad", "sad saddest sadder traurig traurige trauriger traurigste "
                  "traurigsten melancholisch melancholische "
                  "melancholischste wehmütig wehmutig")
_add_words("happy", "happy happiest happier froehlich frohliche froehlichste "
                    "fröhlich fröhliche fröhlichste upbeat gutgelaunt")
_add_words("danceable", "danceable tanzbar tanzbare tanzbarste clubby groovy")
_add_words("rough", "rough roughest rougher rau rauhe rauheste rauhesten "
                    "noisy dissonant dissonante schraeg schraege")
_add_words("harmonic", "harmonic harmonious harmonisch harmonische "
                       "harmonischste tonal wohlklingend")
_add_words("punchy", "punchy punchiest druckvoll druckvolle druckvollste "
                     "wuchtig wuchtigste")
_add_words("wide", "wide widest breit breite breiteste raeumlich raumlich")
_add_words("dense", "dense densest dicht dichte dichteste busy vollgepackt")
_add_words("sparse", "sparse sparsest spaerlich sparliche kaerg kaergste karg "
                     "kargste")
_add_words("boring", "boring bored langweilig langweilige langweiliger "
                     "langweiligste langweiligsten oede ode oedeste oedesten "
                     "monoton monotone monotonste dull fad fade")
_add_words("interesting", "interesting exciting spannend spannende "
                          "spannendste aufregend abwechslungsreich")
_add_words("bassy", "bass bassy bass-heavy basslastig bassbetont bassreich")
_add_words("percussive", "percussive drums drumming drum-heavy drumlastig "
                          "schlagzeugbetont trommelnd")
_add_words("stringy", "strings stringy streicher streicherlastig geigen "
                       "violins")
_add_words("keyy", "piano pianos keys keyboards klavier klavierlastig")
_add_words("vocal", "vocal vocals voice voices gesang stimme gesanglich")
_add_words("classical", "classical klassisch orchestral sinfonisch symphonic")

#: the raw measurements, grouped for display.  `group` is the heading the chat
#: and the window use; `unit` and `digits` control the formatting.
RAW = (
    {"key": "tempo_bpm", "label": "Tempo", "unit": "BPM", "digits": 0,
     "group": "Rhythm"},
    {"key": "onset_rate", "label": "Onsets", "unit": "/s", "digits": 2,
     "group": "Rhythm"},
    {"key": "pulse_clarity", "label": "Pulse clarity", "unit": "", "digits": 2,
     "group": "Rhythm"},
    {"key": "beat_stability", "label": "Beat stability", "unit": "", "digits": 2,
     "group": "Rhythm"},
    {"key": "ioi_entropy", "label": "Rhythmic complexity", "unit": "",
     "digits": 2, "group": "Rhythm"},

    {"key": "lufs", "label": "Loudness", "unit": "LUFS", "digits": 1,
     "group": "Loudness"},
    {"key": "energy_db", "label": "Level", "unit": "dBFS", "digits": 1,
     "group": "Loudness"},
    {"key": "dynamic_db", "label": "Micro-dynamics", "unit": "dB", "digits": 1,
     "group": "Loudness"},
    {"key": "lra_db", "label": "Loudness range", "unit": "LU", "digits": 1,
     "group": "Loudness"},
    {"key": "crest_db", "label": "Crest factor", "unit": "dB", "digits": 1,
     "group": "Loudness"},
    {"key": "energy_slope", "label": "Trend", "unit": "dB/10s", "digits": 1,
     "group": "Loudness"},
    {"key": "silence_ratio", "label": "Silence", "unit": "", "digits": 2,
     "group": "Loudness"},

    {"key": "centroid_hz", "label": "Centroid", "unit": "Hz", "digits": 0,
     "group": "Timbre"},
    {"key": "rolloff_hz", "label": "Rolloff", "unit": "Hz", "digits": 0,
     "group": "Timbre"},
    {"key": "spread_hz", "label": "Spread", "unit": "Hz", "digits": 0,
     "group": "Timbre"},
    {"key": "skewness", "label": "Skewness", "unit": "", "digits": 2,
     "group": "Timbre"},
    {"key": "kurtosis", "label": "Kurtosis", "unit": "", "digits": 2,
     "group": "Timbre"},
    {"key": "flatness", "label": "Flatness", "unit": "", "digits": 3,
     "group": "Timbre"},
    {"key": "entropy", "label": "Spectral entropy", "unit": "", "digits": 2,
     "group": "Timbre"},
    {"key": "sharpness_acum", "label": "Sharpness", "unit": "acum",
     "digits": 2, "group": "Timbre"},
    {"key": "harmonicity", "label": "Harmonicity", "unit": "", "digits": 2,
     "group": "Timbre"},
    {"key": "zcr", "label": "Zero crossings", "unit": "", "digits": 3,
     "group": "Timbre"},
    {"key": "percussiveness", "label": "Percussiveness", "unit": "",
     "digits": 3, "group": "Timbre"},
    {"key": "flux", "label": "Spectral flux", "unit": "", "digits": 3,
     "group": "Timbre"},

    {"key": "key_strength", "label": "Key clarity", "unit": "", "digits": 2,
     "group": "Tonality"},
    {"key": "mode_score", "label": "Major/minor", "unit": "", "digits": 2,
     "group": "Tonality"},
    {"key": "roughness", "label": "Dissonance", "unit": "", "digits": 4,
     "group": "Tonality"},
    {"key": "repetition", "label": "Repetition", "unit": "", "digits": 2,
     "group": "Tonality"},

    {"key": "low_ratio", "label": "Low band", "unit": "", "digits": 2,
     "group": "Bands and space"},
    {"key": "high_ratio", "label": "High band", "unit": "", "digits": 2,
     "group": "Bands and space"},
    {"key": "stereo_width", "label": "Stereo width", "unit": "", "digits": 2,
     "group": "Bands and space"},
)

#: the order the groups appear in
RAW_GROUPS = tuple(dict.fromkeys(item["group"] for item in RAW))


def db_path(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "psycho" / "features.db"


def normalise_terms(terms) -> list[tuple[str, float]]:
    """Coerce whatever a planner sent into ``(concept, sign)`` pairs."""
    out: list[tuple[str, float]] = []
    for item in terms or []:
        name = ""
        sign = 1.0
        if isinstance(item, (tuple, list)) and item:
            name = str(item[0] or "")
            try:
                sign = float(item[1]) if len(item) > 1 else 1.0
            except (TypeError, ValueError):
                sign = 1.0
        else:
            name = str(item or "")
        name = name.strip().casefold()
        name = CONCEPT_WORDS.get(name, name)
        if name in CONCEPTS and all(name != other for other, _ in out):
            out.append((name, -1.0 if sign < 0 else 1.0))
    return out


#: the property a "least …" request flips to, when a clean antonym exists
ANTONYMS = {
    "slow": "fast", "fast": "slow", "quiet": "loud", "loud": "quiet",
    "calm": "aggressive", "aggressive": "gentle", "gentle": "aggressive",
    "dark": "bright", "bright": "dark", "sad": "happy", "happy": "sad",
    "danceable": "sparse", "rough": "harmonic", "harmonic": "rough",
    "punchy": "gentle", "wide": "sparse", "dense": "sparse",
    "sparse": "dense", "boring": "interesting", "interesting": "boring",
}


def recipe_axes(terms, limit: int = 3) -> list[str]:
    """The axes that carry the ranking, strongest first (for the reply)."""
    weights: dict[str, float] = {}
    for name, sign in normalise_terms(terms):
        for axis, weight in CONCEPTS[name]["recipe"].items():
            weights[axis] = weights.get(axis, 0.0) + float(weight) * sign
    ordered = sorted(weights.items(), key=lambda pair: -abs(pair[1]))
    return [axis for axis, _ in ordered[:max(1, limit)]]


def concept_labels(terms) -> list[str]:
    labels: list[str] = []
    for name, sign in normalise_terms(terms):
        if sign < 0:
            other = ANTONYMS.get(name)
            label = CONCEPTS[other]["label"] if other else f"least {name}"
        else:
            label = CONCEPTS[name]["label"]
        if label not in labels:
            labels.append(label)
    return labels


# --------------------------------------------------------------------------
# why an axis holds the value it does
#
# Every 0..1 axis is written by `scripts/psycho_profile.py` from measured
# columns, each turned into a percentile rank over the library.  The weights
# below are that writer's `SPECS`/`SET_TAGS[...]["specs"]`, in the same order,
# so an explanation reconstructs the stored axis instead of describing it from
# memory.  A component names
#
#   - a measured column          `energy_db`
#   - a pseudo column            `flux` (falls back along FALLBACK)
#   - a value that is not a rank `tempo_closeness` (a 0..1 distance to 120 BPM)
#   - another axis of the set    `axis:speed`
#   - an inverse                 `1-flatness`, `1-mode_score`
#
# Do not "simplify" a weight here: a number that differs changes the
# explanation of a value the app already shows.  Only the psychology set feeds
# the families; the other four are lenses over the same measurements, and the
# instrument set is a documented heuristic rather than a classifier.
# --------------------------------------------------------------------------

#: the components of the twelve psychology axes (→ SPECS)
AXIS_PARTS: dict[str, tuple[tuple[str, str, float], ...]] = {
    "speed": (("tempo", "tempo_bpm", 1.0),),
    "energy": (("loudness", "energy_db", 0.35),
               ("onsets", "percussiveness", 0.25),
               ("flux", "flux", 0.20),
               ("high", "high_ratio", 0.20)),
    "aggression": (("loudness", "energy_db", 0.22),
                   ("high", "high_ratio", 0.18),
                   ("dissonance", "roughness", 0.18),
                   ("flatness", "flatness", 0.14),
                   ("zcr", "zcr", 0.12),
                   ("speed", "axis:speed", 0.10),
                   ("minor", "1-mode_score", 0.06)),
    "brightness": (("centroid", "centroid_hz", 0.6),
                   ("rolloff", "rolloff_hz", 0.4)),
    "danceability": (("pulse", "pulse_clarity", 0.30),
                     ("onsets", "percussiveness", 0.25),
                     ("onset_rate", "onset_rate", 0.20),
                     ("tempo", "tempo_closeness", 0.15),
                     ("energy", "axis:energy", 0.10)),
    "valence": (("major", "mode_score", 0.40),
                ("brightness", "axis:brightness", 0.20),
                ("speed", "axis:speed", 0.15),
                ("smooth", "1-roughness", 0.15),
                ("key", "key_strength", 0.10)),
    "calmness": (("quiet", "1-energy_db", 0.30),
                 ("few_onsets", "1-percussiveness", 0.25),
                 ("slow", "1-axis:speed", 0.20),
                 ("dynamic", "dynamic_db", 0.15),
                 ("low_high", "1-high_ratio", 0.10)),
    "density": (("onset_rate", "onset_rate", 0.45),
                ("flux", "flux", 0.30),
                ("onsets", "percussiveness", 0.25)),
    "roughness": (("dissonance", "roughness", 0.50),
                  ("flatness", "flatness", 0.30),
                  ("zcr", "zcr", 0.20)),
    "harmony": (("key", "key_strength", 0.40),
                ("tonal", "1-flatness", 0.35),
                ("smooth", "1-roughness", 0.25)),
    "punch": (("crest", "crest_db", 0.40),
              ("transients", "percussiveness", 0.30),
              ("low", "low_ratio", 0.15),
              ("pulse", "pulse_clarity", 0.15)),
    "width": (("side", "stereo_width", 0.75),
              ("dynamic", "dynamic_db", 0.25)),
}

#: the four alternative lenses, straight from SET_TAGS[...]["specs"]
SET_PARTS: dict[str, dict[str, tuple[tuple[str, str, float], ...]]] = {
    "psychology": AXIS_PARTS,
    "timbre": {
        "warmth": (("low", "low_ratio", 0.55),
                   ("dark", "1-centroid_hz", 0.45)),
        "brightness": (("centroid", "centroid_hz", 0.60),
                       ("rolloff", "rolloff_hz", 0.40)),
        "presence": (("band", "presence_ratio", 0.70),
                     ("sharp", "sharpness_acum", 0.30)),
        "air": (("high", "high_ratio", 0.70),
                ("spread", "entropy", 0.30)),
        "sharpness": (("acum", "sharpness_acum", 0.70),
                      ("centroid", "centroid_hz", 0.30)),
        "harmonicity": (("periodic", "harmonicity", 0.60),
                        ("tonal", "1-flatness", 0.40)),
        "noisiness": (("flat", "flatness", 0.50),
                      ("zcr", "zcr", 0.30),
                      ("entropy", "entropy", 0.20)),
        "roughness": (("dissonance", "roughness", 0.60),
                      ("flat", "flatness", 0.40)),
    },
    "rhythm": {
        "speed": (("tempo", "tempo_bpm", 1.0),),
        "beat_clarity": (("pulse", "pulse_clarity", 0.80),
                         ("grid", "beat_stability", 0.20)),
        "regularity": (("grid", "beat_stability", 0.70),
                       ("simple", "1-ioi_entropy", 0.30)),
        "onset_density": (("onsets", "onset_rate", 0.60),
                          ("transients", "percussiveness", 0.40)),
        "complexity": (("ioi", "ioi_entropy", 0.70),
                       ("off_grid", "1-beat_stability", 0.30)),
        "drive": (("pulse", "pulse_clarity", 0.35),
                  ("transients", "percussiveness", 0.30),
                  ("onsets", "onset_rate", 0.20),
                  ("tempo", "tempo_closeness", 0.15)),
    },
    "dynamics": {
        "loudness": (("lufs", "lufs", 1.0),),
        "loudness_range": (("lra", "lra_db", 1.0),),
        "micro_dynamics": (("rms_spread", "dynamic_db", 1.0),),
        "punch": (("crest", "crest_db", 0.70),
                  ("transients", "percussiveness", 0.30)),
        "silence": (("quiet_frames", "silence_ratio", 1.0),),
        "trend": (("slope", "energy_slope", 1.0),),
    },
    "instrument": {
        "bass": (("sub", "sub_ratio", 0.45),
                 ("low", "low_ratio", 0.35),
                 ("dark", "1-centroid_hz", 0.20)),
        "drums": (("transients", "percussiveness", 0.45),
                  ("onsets", "onset_rate", 0.25),
                  ("crest", "crest_db", 0.20),
                  ("zcr", "zcr", 0.10)),
        "strings": (("periodic", "harmonicity", 0.35),
                    ("mid", "mid_ratio", 0.30),
                    ("sustained", "1-percussiveness", 0.20),
                    ("smooth", "1-flux", 0.15)),
        "keys": (("periodic", "harmonicity", 0.30),
                 ("presence", "presence_ratio", 0.25),
                 ("attack", "attack_sharpness", 0.25),
                 ("tonal", "1-flatness", 0.20)),
        "voice": (("presence", "presence_ratio", 0.35),
                  ("periodic", "harmonicity", 0.30),
                  ("mid", "mid_ratio", 0.20),
                  ("smooth", "1-roughness", 0.15)),
        "classical": (("range", "lra_db", 0.30),
                      ("quiet", "1-energy_db", 0.25),
                      ("periodic", "harmonicity", 0.20),
                      ("sustained", "1-percussiveness", 0.15),
                      ("low", "1-low_ratio", 0.10)),
    },
}

#: the columns `build_ranks` turns into percentiles.  A name outside this tuple
#: has no rank, which is why `spread_hz` never stands in for a missing
#: `stereo_width` in the writer and must not here either.
RANKED = ("tempo_bpm", "energy_db", "dynamic_db", "centroid_hz", "rolloff_hz",
          "flatness", "zcr", "percussiveness", "flux", "onset_rate",
          "pulse_clarity", "roughness", "low_ratio", "high_ratio", "entropy",
          "mode_score", "key_strength", "crest_db", "stereo_width", "lufs",
          "lra_db", "sub_ratio", "mid_ratio", "presence_ratio",
          "attack_sharpness", "ioi_entropy", "beat_stability", "silence_ratio",
          "energy_slope", "harmonicity", "sharpness_acum")

#: where a column is missing, the writer tries these in order
FALLBACK: dict[str, tuple[str, ...]] = {
    "flux": ("flux", "percussiveness"),
    "onset_rate": ("onset_rate", "percussiveness"),
    "pulse_clarity": ("pulse_clarity", "percussiveness"),
    "roughness": ("roughness", "flatness"),
    "high_ratio": ("high_ratio", "centroid_hz"),
    "low_ratio": ("low_ratio",),
    "entropy": ("entropy", "flatness"),
    "crest_db": ("crest_db", "dynamic_db"),
    "stereo_width": ("stereo_width", "spread_hz"),
    "lufs": ("lufs", "energy_db"),
    "lra_db": ("lra_db", "dynamic_db"),
    "sub_ratio": ("sub_ratio", "low_ratio"),
    "mid_ratio": ("mid_ratio",),
    "presence_ratio": ("presence_ratio", "high_ratio"),
    "attack_sharpness": ("attack_sharpness", "percussiveness"),
    "ioi_entropy": ("ioi_entropy",),
    "beat_stability": ("beat_stability", "pulse_clarity"),
    "silence_ratio": ("silence_ratio",),
    "energy_slope": ("energy_slope",),
    "harmonicity": ("harmonicity",),
}

#: how a component reads in a sentence
PART_LABELS: dict[str, str] = {
    "tempo_bpm": "tempo",
    "centroid_hz": "spectral centroid",
    "rolloff_hz": "spectral rolloff",
    "energy_db": "level (RMS)",
    "percussiveness": "percussiveness",
    "flux": "spectral flux",
    "high_ratio": "high-frequency share",
    "onset_rate": "onset rate",
    "roughness": "roughness",
    "flatness": "spectral flatness",
    "zcr": "zero-crossing rate",
    "mode_score": "major mode",
    "key_strength": "key clarity",
    "pulse_clarity": "pulse clarity",
    "tempo_closeness": "tempo near 120 BPM",
    "dynamic_db": "micro-dynamics",
    "crest_db": "crest factor",
    "low_ratio": "low-band share",
    "stereo_width": "stereo width",
    "harmonicity": "harmonicity (periodicity)",
    "presence_ratio": "presence band (2–5 kHz)",
    "sharpness_acum": "sharpness (Aures)",
    "entropy": "spectral entropy",
    "beat_stability": "beat stability",
    "ioi_entropy": "rhythmic complexity",
    "silence_ratio": "silent stretches",
    "energy_slope": "energy trend",
    "sub_ratio": "sub-band share (<60 Hz)",
    "mid_ratio": "mid-band share",
    "attack_sharpness": "attack sharpness",
    "lufs": "integrated loudness",
    "lra_db": "loudness range",
    "axis:speed": "the Speed axis",
    "axis:energy": "the Energy axis",
    "axis:brightness": "the Brightness axis",
    "1-energy_db": "quietness",
    "1-high_ratio": "little high-frequency energy",
    "1-mode_score": "minor mode",
    "1-percussiveness": "few onsets",
    "1-flatness": "tonality (little flatness)",
    "1-roughness": "smoothness",
    "1-centroid_hz": "darkness",
    "1-flux": "steady timbre",
    "1-ioi_entropy": "even rhythm",
    "1-beat_stability": "off-grid feel",
    "1-low_ratio": "little low band",
    "1-axis:speed": "slowness",
}


def part_label(spec: str) -> str:
    """A readable name for a component spec."""
    if spec in PART_LABELS:
        return PART_LABELS[spec]
    return spec.replace("_hz", "").replace("1-", "").replace("axis:", "") \
        .replace("_", " ")


class Psycho:
    """Read-only access to the profile database."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = db_path(self.root)
        self._axes: dict[str, dict[str, dict[str, float]]] = {}
        self._families: dict[str, str] | None = None
        self._silent_cache: set[str] | None = None
        self._loaded: set[str] = set()

    # -- internals ---------------------------------------------------------

    def available(self) -> bool:
        return self.path.is_file()

    def _connect(self) -> sqlite3.Connection | None:
        if not self.available():
            return None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                   timeout=2.0)
        except sqlite3.Error:
            return None
        conn.row_factory = sqlite3.Row
        return conn

    def _columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        try:
            return {row["name"] for row in
                    conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    # -- reading -----------------------------------------------------------

    def describe(self, rel: str, set_name: str | None = None) -> dict | None:
        """Everything the app shows for one track, or None when unknown.

        ``set_name`` selects the lens: ``psychology`` reads the `profile`
        table, every other set reads its JSON row from `profile_set`.  The raw
        measurements and the (psychology) family are always included.
        """
        set_name = set_key(set_name)
        conn = self._connect()
        if conn is None:
            return None
        try:
            if set_name == "psychology":
                if "family" not in self._columns(conn, "profile"):
                    return None
                row = conn.execute(
                    f"SELECT a.*, "
                    f"{', '.join('p.' + key + ' AS axis_' + key for key in AXIS_KEYS)}, "
                    "p.family AS family, p.family_distance AS family_distance, "
                    "p.estimated AS profile_estimated "
                    "FROM audio a LEFT JOIN profile p ON p.rel_path = a.rel_path "
                    "WHERE a.rel_path = ?", (rel,)).fetchone()
                if row is None:
                    # a profile may exist without the audio row (never expected)
                    row = conn.execute(
                        "SELECT p.* FROM profile p WHERE p.rel_path = ?",
                        (rel,)).fetchone()
                if row is None:
                    return None
                keys = set(row.keys())
                axes = {key: (row["axis_" + key]
                              if "axis_" + key in keys else None)
                        for key in AXIS_KEYS}
                family = row["family"] if "family" in keys else None
                distance = (row["family_distance"]
                            if "family_distance" in keys else None)
                estimated = bool((row["profile_estimated"]
                                  if "profile_estimated" in keys else 1) or
                                 (row["estimated"] if "estimated" in keys else 1))
            else:
                if not self._columns(conn, "profile_set"):
                    return None
                row = conn.execute(
                    "SELECT a.*, s.axes AS set_axes, "
                    "s.estimated AS set_estimated, "
                    "p.family AS family, p.family_distance AS family_distance, "
                    "p.estimated AS profile_estimated "
                    "FROM audio a "
                    "LEFT JOIN profile_set s ON s.rel_path = a.rel_path "
                    "AND s.set_name = ? "
                    "LEFT JOIN profile p ON p.rel_path = a.rel_path "
                    "WHERE a.rel_path = ?", (set_name, rel)).fetchone()
                if row is None:
                    return None
                keys = set(row.keys())
                try:
                    parsed = json.loads(row["set_axes"] or "{}")
                except (TypeError, ValueError):
                    return None
                if not parsed:
                    return None
                axes = {axis["key"]: (float(parsed[axis["key"]])
                                      if axis["key"] in parsed else None)
                        for axis in set_axes(set_name)}
                family = row["family"] if "family" in keys else None
                distance = (row["family_distance"]
                            if "family_distance" in keys else None)
                estimated = bool((row["set_estimated"]
                                  if "set_estimated" in keys else 1) or
                                 (row["profile_estimated"]
                                  if "profile_estimated" in keys else 1))
            measured = {
                item["key"]: (row[item["key"]] if item["key"] in keys else None)
                for item in RAW
            }
            return {
                "rel": rel,
                "set": set_name,
                "family": family,
                "family_distance": distance,
                "estimated": estimated,
                "axes": axes,
                "measured": measured,
            }
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._connect()
        if conn is None:
            return 0
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM profile").fetchone()[0])
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    def families(self) -> list[dict]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT name, size, traits, playlist FROM family "
                "ORDER BY size DESC").fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        out = []
        for row in rows:
            try:
                traits = json.loads(row["traits"] or "[]")
            except ValueError:
                traits = []
            out.append({"name": row["name"], "size": row["size"],
                        "traits": traits, "playlist": row["playlist"]})
        return out

    def load_axes(self, set_name: str | None = None
                  ) -> dict[str, dict[str, float]]:
        """All profiles of one set in memory; cached per set."""
        set_name = set_key(set_name)
        if set_name in self._loaded:
            return self._axes.get(set_name, {})
        conn = self._connect()
        if conn is None:
            return {}
        axes: dict[str, dict[str, float]] = {}
        try:
            if set_name == "psychology":
                rows = conn.execute(
                    f"SELECT rel_path, {', '.join(AXIS_KEYS)} FROM profile"
                ).fetchall()
                for row in rows:
                    values = {}
                    for key in AXIS_KEYS:
                        value = row[key]
                        if value is not None:
                            values[key] = float(value)
                    if len(values) >= 6:
                        axes[row["rel_path"]] = values
            else:
                if not self._columns(conn, "profile_set"):
                    return {}
                rows = conn.execute(
                    "SELECT rel_path, axes FROM profile_set WHERE set_name = ?",
                    (set_name,)).fetchall()
                keys = set_keys(set_name)
                need = max(3, len(keys) // 2)
                for row in rows:
                    try:
                        parsed = json.loads(row["axes"] or "{}")
                    except (TypeError, ValueError):
                        continue
                    values = {key: float(parsed[key]) for key in keys
                              if parsed.get(key) is not None}
                    if len(values) >= need:
                        axes[row["rel_path"]] = values
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        self._axes[set_name] = axes
        self._loaded.add(set_name)
        return axes

    def sets(self) -> list[dict]:
        """The switchable profile sets, with availability and track counts."""
        counts: dict[str, int] = {}
        conn = self._connect()
        if conn is not None:
            try:
                if self._columns(conn, "profile"):
                    counts["psychology"] = int(conn.execute(
                        "SELECT COUNT(*) FROM profile").fetchone()[0])
                if self._columns(conn, "profile_set"):
                    for row in conn.execute(
                            "SELECT set_name, COUNT(*) AS n FROM profile_set "
                            "GROUP BY set_name"):
                        counts[str(row["set_name"])] = int(row["n"])
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        out = []
        for spec in SETS:
            out.append({
                "key": spec["key"],
                "label": spec["label"],
                "note": spec.get("note", ""),
                "estimated": bool(spec.get("estimated")),
                "axes": [dict(axis) for axis in spec["axes"]],
                "count": counts.get(spec["key"], 0),
            })
        return out

    def load_family(self) -> dict[str, str]:
        """rel_path -> family label, cached like ``load_axes``."""
        if self._families is not None:
            return self._families
        conn = self._connect()
        if conn is None:
            return {}
        try:
            rows = conn.execute(
                "SELECT rel_path, family FROM profile "
                "WHERE family IS NOT NULL").fetchall()
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        self._families = {row["rel_path"]: row["family"] for row in rows}
        return self._families

    def invalidate(self) -> None:
        self._loaded = set()
        self._axes = {}
        self._families = None
        self._silent_cache = None

    # -- similarity --------------------------------------------------------

    def similar(self, rel: str | None = None, axes: dict[str, float] | None = None,
                adjustments: dict[str, float] | None = None,
                limit: int = 12, exclude_exact: bool = True,
                set_name: str | None = None) -> list[dict]:
        """Tracks nearest to a profile, optionally after moving some axes.

        ``adjustments`` is a mapping axis -> delta (-1..1).  The changed axes
        are weighted 1.6x, so moving speed actually changes the result
        instead of being averaged away by the untouched axes.  ``set_name``
        chooses which lens both the target and the candidates use.
        """
        set_name = set_key(set_name)
        keys = set_keys(set_name)
        base = dict(axes or {})
        if rel and not base:
            entry = self.load_axes(set_name).get(rel)
            if entry:
                base = dict(entry)
        if not base:
            return []
        target = dict(base)
        moved: set[str] = set()
        for key, delta in (adjustments or {}).items():
            if key not in keys:
                continue
            if key not in target:
                target[key] = 0.5
            target[key] = max(0.0, min(1.0, target[key] + float(delta)))
            moved.add(key)
        weights = {key: (1.6 if key in moved else 1.0) for key in keys}
        all_axes = self.load_axes(set_name)
        scored = []
        for other, values in all_axes.items():
            if exclude_exact and other == rel:
                continue
            distance = 0.0
            weight = 0.0
            for key in keys:
                if key not in values or key not in target:
                    continue
                w = weights[key]
                distance += w * (values[key] - target[key]) ** 2
                weight += w
            if weight <= 0:
                continue
            scored.append((distance / weight, other))
        scored.sort(key=lambda pair: pair[0])
        family_of = self.load_family()
        out = []
        for distance_sq, other in scored[:max(1, min(200, limit))]:
            out.append({
                "rel": other,
                "set": set_name,
                "distance": round(distance_sq ** 0.5, 4),
                "axes": all_axes.get(other, {}),
                "family": family_of.get(other, ""),
            })
        return out

    def _silent(self) -> set[str]:
        """Rows whose audio could not be decoded, so every axis is meaningless."""
        if self._silent_cache is not None:
            return self._silent_cache
        conn = self._connect()
        if conn is None:
            return set()
        try:
            rows = conn.execute(
                "SELECT rel_path FROM audio "
                "WHERE energy_db IS NULL OR energy_db < -100").fetchall()
            self._silent_cache = {row["rel_path"] for row in rows}
        except sqlite3.Error:
            self._silent_cache = set()
        finally:
            conn.close()
        return self._silent_cache

    # -- ranking by property -----------------------------------------------

    def rank(self, terms, limit: int = 12, rels=None,
             min_cover: float = 0.6) -> list[dict]:
        """The tracks that are *most* of the requested properties.

        ``terms`` is a list of ``(concept, sign)`` pairs — a negative sign
        means "least" — and each concept contributes its axis recipe from
        :data:`CONCEPTS`.  The score is the weighted mean of the measured axes
        mapped to 0..1, so 1.0 is the extreme end of the property and 0.5 is
        average.  ``rels`` restricts the pool (a genre, an artist, a playlist).

        Only tracks with at least ``min_cover`` of the recipe's weight
        measured are ranked; a seeded profile that lacks the axis would
        otherwise score as if it were zero.
        """
        weights_by_set: dict[str, dict[str, float]] = {}
        for name, sign in normalise_terms(terms):
            concept = CONCEPTS[name]
            set_name = set_key(concept.get("set", "psychology"))
            bucket = weights_by_set.setdefault(set_name, {})
            for axis, weight in concept["recipe"].items():
                bucket[axis] = bucket.get(axis, 0.0) + float(weight) * sign
        weights_by_set = {
            set_name: {key: value for key, value in weights.items()
                       if abs(value) > 1e-9}
            for set_name, weights in weights_by_set.items()}
        weights_by_set = {set_name: weights
                          for set_name, weights in weights_by_set.items()
                          if weights}
        if not weights_by_set:
            return []
        totals = {set_name: sum(abs(value) for value in weights.values())
                  for set_name, weights in weights_by_set.items()}
        axes_by_set = {set_name: self.load_axes(set_name)
                       for set_name in weights_by_set}
        if not all(axes_by_set.values()):
            return []
        family_of = self.load_family()
        dead = self._silent()
        pool = set(rels) if rels is not None else None
        # score each set on its own, then combine the per-set weighted means
        parts: dict[str, dict[str, float]] = {}
        for set_name, weights in weights_by_set.items():
            total = totals[set_name]
            for rel, values in axes_by_set[set_name].items():
                if pool is not None and rel not in pool:
                    continue
                if rel in dead:    # a silent decode would win "most boring"
                    continue
                covered = sum(abs(weight) for key, weight in weights.items()
                              if key in values)
                if covered < min_cover * total:
                    continue
                # the axes are percentile ranks in 0..1; 2*v-1 centres them so
                # a negative weight can reach the same distance from the middle
                score = sum(weight * (2.0 * values[key] - 1.0)
                            for key, weight in weights.items()
                            if key in values) / covered
                parts.setdefault(rel, {})[set_name] = score
        wanted = sum(totals.values())
        scored: list[tuple[float, str]] = []
        for rel, found in parts.items():
            # a track missing one of the requested lenses is skipped rather
            # than scored on a partial recipe
            if len(found) != len(weights_by_set):
                continue
            score = sum(found[set_name] * totals[set_name]
                        for set_name in found) / wanted
            scored.append((score, rel))
        scored.sort(key=lambda pair: -pair[0])
        out = []
        for score, rel in scored[:max(1, min(500, int(limit)))]:
            axes = {}
            for set_name in weights_by_set:
                axes.update(axes_by_set[set_name].get(rel, {}))
            # psychology wins a key collision (speed/brightness/roughness)
            axes.update(axes_by_set.get("psychology", {}).get(rel, {})
                        if "psychology" in weights_by_set else {})
            out.append({
                "rel": rel,
                "score": round((score + 1.0) / 2.0, 4),
                "raw": round(score, 4),
                "axes": axes,
                "family": family_of.get(rel, ""),
            })
        return out

    # -- why an axis holds its value ---------------------------------------

    def _percentile(self, conn: sqlite3.Connection, rel: str, column: str,
                    cache: dict[str, float | None]) -> float | None:
        """One column's percentile rank for one track, exactly as the writer
        computes it: `(row_number - 0.5) / measured`, ties broken by path.

        The writer ranks every measured column once and keeps the number; this
        asks SQLite for that single row instead, so an explanation costs a few
        clauses rather than a 13k-row pass.  `column` is always one of
        :data:`RANKED`, so it can be interpolated safely.
        """
        if column in cache:
            return cache[column]
        value: float | None = None
        if column in RANKED:
            try:
                row = conn.execute(
                    'SELECT (rn - 0.5) / total AS pct FROM ('
                    '  SELECT rel_path, ROW_NUMBER() OVER ('
                    f'           ORDER BY "{column}", rel_path) AS rn,'
                    '         COUNT(*) OVER () AS total,'
                    f'         "{column}" AS v FROM audio'
                    f'    WHERE "{column}" IS NOT NULL)'
                    ' WHERE rel_path = ?', (rel,)).fetchone()
            except sqlite3.Error:
                row = None
            if row is not None and row["pct"] is not None:
                value = float(row["pct"])
        cache[column] = value
        return value

    def _column_rank(self, conn: sqlite3.Connection, rel: str, name: str,
                     cache: dict[str, float | None]) -> float | None:
        """A component name resolved to 0..1, following the writer's FALLBACK."""
        if name.startswith("1-"):
            base = self._column_rank(conn, rel, name[2:], cache)
            return None if base is None else 1.0 - base
        if name == "tempo_closeness":
            # not a rank: a Gaussian distance to 120 BPM, as build_ranks has it
            if "tempo_closeness" in cache:
                return cache["tempo_closeness"]
            value: float | None = None
            try:
                row = conn.execute(
                    "SELECT tempo_bpm FROM audio WHERE rel_path = ?",
                    (rel,)).fetchone()
            except sqlite3.Error:
                row = None
            bpm = float(row["tempo_bpm"] or 0.0) if row is not None else 0.0
            if bpm > 0:
                value = math.exp(-0.5 * (math.log2(bpm / 120.0) / 0.35) ** 2)
            cache["tempo_closeness"] = value
            return value
        for option in FALLBACK.get(name, (name,)):
            value = self._percentile(conn, rel, option, cache)
            if value is not None:
                return value
        return None

    def _resolved_column(self, conn: sqlite3.Connection, rel: str, name: str,
                         cache: dict[str, float | None]) -> str:
        """The column that actually supplies ``name`` after FALLBACK."""
        for option in FALLBACK.get(name, (name,)):
            if self._percentile(conn, rel, option, cache) is not None:
                return option
        return ""

    def _part_value(self, conn: sqlite3.Connection, rel: str, spec: str,
                    stored: dict, cache: dict
                    ) -> tuple[float | None, str]:
        """A component's 0..1 value and where it came from."""
        if spec.startswith("axis:"):
            axis = spec[5:]
            value = stored.get(axis)
            return (None if value is None else float(value),
                    f"the {axis} axis")
        if spec.startswith("1-axis:"):
            # the writer's `_axis_value` only resolves `axis:`, so a `1-axis:`
            # term is dropped from the stored profile.  Mirror that here: a
            # value would explain a number the app does not show.
            return None, "the profile writer does not use this term"
        value = self._column_rank(conn, rel, spec, cache)
        if value is None:
            return None, ""
        base = spec[2:] if spec.startswith("1-") else spec
        # name the column that really supplied the number, so a fallback
        # (a missing `flux` read from `percussiveness`) is not disguised as
        # the measurement it stands in for
        return value, (self._resolved_column(conn, rel, base, cache) or base)

    def _explain_set(self, conn: sqlite3.Connection, rel: str, set_name: str,
                     wanted: list[str] | None, cache: dict) -> list[dict]:
        """The axis entries of one set."""
        specs = SET_PARTS[set_name]
        entry = self.describe(rel, set_name) or {}
        stored = entry.get("axes") or {}
        labels = {axis["key"]: axis["label"] for axis in set_axes(set_name)}
        measured = self._set_count(conn, set_name)
        if wanted is None:
            wanted = [key for key in specs if stored.get(key) is not None]
            wanted.sort(key=lambda key: -abs((stored.get(key) or 0.5) - 0.5))
        out = []
        for axis in wanted:
            if axis not in specs:
                continue
            parts: list[dict] = []
            missing: list[dict] = []
            weighted = 0.0
            weight = 0.0
            for name, spec, share in specs[axis]:
                value, source = self._part_value(conn, rel, spec, stored, cache)
                if value is None:
                    missing.append({
                        "key": name,
                        "label": part_label(spec),
                        "spec": spec,
                        "weight": round(share, 4),
                        "note": source or "not measured for this track",
                    })
                    continue
                weighted += value * share
                weight += share
                parts.append({
                    "key": name,
                    "label": part_label(spec),
                    "spec": spec,
                    "source": source,
                    "value": round(value, 4),
                    "weight": round(share, 4),
                    "points": round(value * share, 4),
                })
            parts.sort(key=lambda part: (-abs(part["value"] - 0.5)
                                         * part["weight"], part["label"]))
            value = stored.get(axis)
            rebuilt = (weighted / weight) if weight > 0 else None
            out.append({
                "key": axis,
                "label": labels.get(axis, axis.title()),
                "set": set_name,
                "set_label": set_spec(set_name)["label"],
                "value": None if value is None else round(float(value), 4),
                "rebuilt": None if rebuilt is None else round(rebuilt, 4),
                "estimated": bool(entry.get("estimated"))
                or bool(set_spec(set_name).get("estimated")),
                "parts": parts,
                "missing": missing,
                "covered": round(weight, 4),
                "measured": measured,
            })
        return out

    def explain(self, rel: str, axes=None, set_name: str | None = None,
                terms=None) -> dict | None:
        """Reconstruct the axes of one track from their components.

        Each entry gives the stored 0..1 axis, the same value rebuilt from its
        parts, and every part's percentile, declared weight and contribution.
        ``terms`` is the chat's ``(concept, sign)`` list and decides which axes
        (and which profile set) to explain; ``axes``/``set_name`` address one
        set directly.  Without either, the axes the track deviates most from
        average in are explained.

        A part the writer drops (``1-axis:``) or that was never measured comes
        back with ``value: None`` and a reason, never as a zero.
        """
        conn = self._connect()
        if conn is None:
            return None
        cache: dict[str, float | None] = {}
        try:
            wanted_by_set: dict[str, list[str] | None] = {}
            if terms:
                for name, _sign in normalise_terms(terms):
                    concept = CONCEPTS[name]
                    target = set_key(concept.get("set", "psychology"))
                    bucket = wanted_by_set.setdefault(target, [])
                    for axis in concept["recipe"]:
                        if bucket is not None and axis not in bucket:
                            bucket.append(axis)
            elif axes:
                wanted_by_set[set_key(set_name)] = list(axes)
            else:
                wanted_by_set[set_key(set_name)] = None
            entries: list[dict] = []
            for target_set, wanted in wanted_by_set.items():
                if target_set not in SET_PARTS:
                    continue
                entries.extend(self._explain_set(conn, rel, target_set,
                                                 wanted, cache))
            if not entries:
                return None
            return {
                "rel": rel,
                "sets": sorted({entry["set"] for entry in entries}),
                "estimated": any(entry["estimated"] for entry in entries),
                "axes": entries,
            }
        finally:
            conn.close()

    def _set_count(self, conn: sqlite3.Connection, set_name: str) -> int:
        """How many tracks the set holds — a "top 2 %" needs the total."""
        try:
            if set_name == "psychology":
                return int(conn.execute(
                    "SELECT COUNT(*) FROM profile").fetchone()[0])
            return int(conn.execute(
                "SELECT COUNT(*) FROM profile_set WHERE set_name = ?",
                (set_name,)).fetchone()[0])
        except sqlite3.Error:
            return 0

    def signature(self) -> dict:
        """A cheap change marker, so callers reload after a new profile run."""
        try:
            stat = self.path.stat()
        except OSError:
            return {"available": False}
        return {"available": True, "size": stat.st_size,
                "mtime": int(stat.st_mtime)}
