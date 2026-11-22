"""Turn a sentence into one skill call — offline, deterministic, bilingual.

This is the first planner.  It is deliberately conservative: when the wording
is clear it returns a plan, when it is not it returns ``None`` and the engine
may ask the optional LLM or answer with help.  Nothing here writes anything;
the engine validates and executes.

English and German are treated as one language: "create a smooth playlist",
"erstelle eine sanfte Playlist" and "mach mir was Entspanntes" all reach the
same skill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .catalog import Track, fold
from .psycho import CONCEPT_WORDS, CONCEPTS
from .selection import GENRE_WORDS, MOOD_PHRASES, MOOD_WORDS, moods_from_text
from .settings import parse as parse_settings

ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "erste": 1, "ersten": 1, "zweite": 2, "zweiten": 2, "dritte": 3,
    "dritten": 3, "vierte": 4, "fünfte": 5, "fuenfte": 5, "sechste": 6,
    "siebte": 7, "achte": 8, "neunte": 9, "zehnte": 10,
}

#: words that carry no target on their own
FILLER = {
    "the", "a", "an", "some", "any", "my", "me", "for", "please", "bitte",
    "mal", "doch", "just", "now", "jetzt", "song", "songs", "track", "tracks",
    "tune", "tunes", "music", "musik", "etwas", "something", "was", "this",
    "that", "it", "davon", "mir", "ich", "to", "on", "of", "from", "in",
}

#: "the *least* boring track" flips a property instead of naming another one
LEAST_WORDS = {"least", "less", "wenigsten", "wenigste", "am wenigsten"}

#: nouns that tell a ranking request from a mood request
RANK_NOUNS = ("song", "songs", "track", "tracks", "titel", "lied", "lieder",
              "stuck", "stueck", "tune", "tunes", "piece", "pieces",
              "music", "musik", "album", "albums")

#: words that make a sentence about the *words* of a song.  The lyric rules
#: use them so that "more songs like this lyrically" compares words instead of
#: being read as feedback ("like this") or as a "more like this" queue.
LYRIC_CONTEXT = ("lyrics", "lyric", "songtext", "songtexte", "liedtext",
                 "liedtexte", "gesangstext", "lyrically", "lyrisch",
                 "lyrische", "lyrischen", "lyrischem", "lyrischer")


#: words a lyric search is *about* — never part of the words to look for
LYRIC_STRIP = set("""
lyrics lyric songtext songtexte songtexts gesangstext liedtext liedtexte
text textes zeile zeilen line lines verse verses words word wort worte
song songs track tracks lied lieder title titel tune tunes something anything
etwas irgendwas about ueber uber where wo whose which what that das der die den
dem des ein eine einen einem einer und and or oder but aber is are was sind war
were be been im am an auf on of von for fuer with mit has have hat haben
contains enthaelt mention mentions mentioned erwaehnt erwahnt erwahnen sings
sing singt singen talks talk spricht handelt geht um es it me my mir mich this
diese dieser dies welches welche welcher shows show zeig zeige find finde search
suche such the a an to do you your dein deine gibt give does i in my mine auch
noch mal schon nur sehr wie wer wann warum denn doch bitte wenn ob als so zu
vom beim zur zum uns euch ihr seine seinen ihre ihren
make create erstelle erstellen mach mache machst bau baue playlist playlists
liste mix songlist tracksong
list read print give tell display recite sing singing along aloud show
showing shows current currently playing played running now just last previous
next one ones comes come follows following us we jetzt aktuell aktuelle
aktuellen aktueller gerade eben laufend laufende laufenden gespielt gespielte
gespielten vorlesen lies vor
""".split())

#: words that point at the song *now* rather than at words to look for —
#: "the lyrics of this song", "was gerade läuft", "what's playing"
NOW_POINTERS = (
    "this song", "this track", "this one", "that song", "the current song",
    "the currently playing song", "what's playing", "whats playing",
    "what is playing", "right now", "at the moment", "being sung",
    "dieses lied", "diesen song", "diesen track", "dieser song",
    "das aktuelle lied", "der aktuelle song", "was gerade", "was laeuft",
    "was läuft", "wird gesungen",
)

#: "something with a beat" is a sound request, "something with a boat" is not
ABSTRACT_WORDS = {
    "beat", "beats", "bass", "melody", "melodie", "guitar", "gitarre",
    "voice", "stimme", "sound", "klang", "rhythm", "rhythmus", "vibe",
    "mood", "stimmung", "feeling", "gefuehl", "energy", "energie", "tempo",
    "groove", "hook", "chorus", "refrain", "instrumental", "atmosphere",
}


def lyric_text(text: str) -> tuple[str, list[tuple[str, float]]]:
    """The words to search for, and the sound properties beside them.

    "something with a boat and fast" gives ``("boat", [("fast", 1)])`` — the
    word *boat* is what the lyrics must say, *fast* is a measured property that
    only orders the hits.  Returns ``("", [])`` when nothing is left to look
    for, so the sentence stays whatever it was.
    """
    terms = rank_terms(text)
    remaining: list[str] = []
    for token in re.split(r"[^0-9A-Za-zÄÖÜäöüß']+", text):
        word = fold(token).strip("'")
        if not word or word in LYRIC_STRIP:
            continue
        if "'" in word and word.split("'", 1)[0] in LYRIC_STRIP:
            continue                  # "what's", "where's" — a request word
        if word in CONCEPT_WORDS:      # "fast" is a property, not a lyric word
            continue
        remaining.append(word)
    if not remaining:
        return "", terms
    if all(word in ABSTRACT_WORDS for word in remaining):
        return "", terms
    return " ".join(dict.fromkeys(remaining)), terms


def rank_terms(text: str) -> list[tuple[str, float]]:
    """The sound properties a sentence names, as ``(concept, sign)`` pairs.

    "the slowest and most boring song" gives ``[("slow", 1), ("boring", 1)]``
    and "the least boring track" gives ``[("boring", -1)]``.  Matched against
    the whole word list in :mod:`chat.psycho`, so "boring" and "langweiligste"
    both work and "slower" is not missed.
    """
    folded = " ".join(fold(text).split())
    if not folded:
        return []
    tokens = re.split(r"[^0-9a-z]+", folded)
    found: list[tuple[str, float]] = []
    for index, token in enumerate(tokens):
        concept = CONCEPT_WORDS.get(token)
        if not concept:
            continue
        before = [word for word in tokens[max(0, index - 3):index] if word]
        sign = -1.0 if any(word in LEAST_WORDS for word in before) else 1.0
        if all(concept != other for other, _ in found):
            found.append((concept, sign))
    return found


@dataclass
class Plan:
    skill: str
    args: dict = field(default_factory=dict)
    say: str = ""
    confidence: float = 0.0
    source: str = "rules"


@dataclass
class Context:
    last_tracks: list[Track] = field(default_factory=list)
    last_playlist: str | None = None
    last_query: str | None = None
    last_skill: str | None = None
    current: Track | None = None
    last_request: str = ""
    last_moods: dict[str, float] = field(default_factory=dict)
    last_recommendation: dict | None = None
    last_youtube: dict | None = None
    #: the export the last backup preview named, for "import it"
    last_library_file: str = ""
    #: the ad-hoc list ("play something …") still on disk, for save/discard
    last_own: str = ""
    last_own_at: str = ""


def _rx(pattern: str, text: str, flags: int = 0):
    return re.search(pattern, text, flags | re.IGNORECASE)


def _has(folded: str, *words: str) -> bool:
    """Word-boundary match on folded text, folding the patterns too."""
    return any(re.search(rf"(?<!\w){re.escape(fold(word))}(?!\w)", folded)
               for word in words)


#: words that mean the listener wants a *saved* playlist (“make a playlist
#: called Evening”). Without one, `make-playlist` only builds a queue.
KEEP_PLAYLIST_WORDS = (
    "playlist", "playlists", "liste", "listen", "mixtape", "mix",
    "save", "speichere", "sichere", "store", "behalte",
    "called", "named", "genannt", "namens",
)


def wants_kept_playlist(folded: str, original: str) -> bool:
    """True when a `make-playlist` request asks for a list to keep.

    “play something chill” / “spiele etwas Entspanntes” only build a queue,
    which lives in `Playlists/Own/` until the listener says “save it”.  A
    sentence that names a list — the word *playlist*, *mix*, *save*, *called*,
    “as …” — or quotes a name writes `Playlists/Custom/` as before.
    """
    return bool(_has(folded, *KEEP_PLAYLIST_WORDS)
                or re.search(r"(?:^|\s)(?:as|als)(?:\s|$)", folded)
                or extract_explicit_name(original))


#: words that make the library index job the subject of a sentence
INDEX_CONTEXT = ("index", "indexes", "indexing", "reindex", "re-index",
                 "rescan", "re-scan", "inventory", "scan", "scanning",
                 "indizieren", "indiziere", "indiziert", "indizierung")


#: words that make the app's own library data (profile + lyrics) the subject
LIBRARY_DATA_CONTEXT = (
    "back up", "backup", "backups", "sicherung", "datensicherung",
    "library data", "library-data", "bibliotheksdaten", "plain text",
    "plain-text", "export file", "jsonl", "fingerprint", "fingerprints",
    "profile data", "profiledaten", "lyrics backup", "sound profile backup",
)


def _export_path(text: str) -> str:
    """The export file a sentence names, or ""."""
    for match in re.finditer(r"[^\s\"'“”]+\.jsonl", text):
        return match.group(0)
    return ""


def _library_export_path(text: str) -> str:
    """A `library-data-….jsonl` export named in a sentence, or ""."""
    match = re.search(r"[^\s\"'“”]*library-data-[^\s\"'“”]+\.jsonl", text)
    return match.group(0) if match else ""


def _index_asking(folded: str) -> bool:
    """Is the sentence asking *about* the job rather than telling it?"""
    return _has(folded, "status", "progress", "fortschritt", "how is",
                "how far", "how's", "wie weit", "wie steht", "laeuft",
                "läuft", "going", "fertig", "done", "finished", "paused",
                "cancelled", "canceled", "stopped")


#: "the slowest song" names a sound property, "the metal albums" does not
SUPERLATIVE = re.compile(
    r"(slowest|fastest|quietest|loudest|calmest|darkest|brightest|saddest|"
    r"happiest|roughest|punchiest|densest|sparsest|hardest|"
    r"langsamste[ns]?|schnellste[ns]?|leiseste[ns]?|lauteste[ns]?|"
    r"ruhigste[ns]?|dunkelste[ns]?|hellste[ns]?|traurigste[ns]?|"
    r"froehlichste[ns]?|langweiligste[ns]?|haerteste[ns]?|aggressivste[ns]?|"
    r"zarteste[ns]?|tanzbarste[ns]?)")


def _states_a_property(folded: str) -> bool:
    """True when the sentence asks for an extreme ("slowest", "most boring")."""
    return bool(SUPERLATIVE.search(folded)) or _has(
        folded, "most", "least", "meisten", "wenigsten", "extremely")


def _ordinal(text: str) -> int | None:
    folded = fold(text)
    for word, number in ORDINAL_WORDS.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", folded):
            return number
    return None


def _span_seconds(match) -> int:
    """Seconds for a "<number> <unit>" match: seconds, or minutes when given."""
    amount = int(match.group(1))
    unit = match.group(2).casefold()
    return amount * 60 if unit.startswith("m") else amount


def _strip_filler(text: str) -> str:
    words = text.split()
    while words and fold(words[0]).strip(".,!?") in FILLER:
        words.pop(0)
    while words and fold(words[-1]).strip(".,!?") in FILLER:
        words.pop()
    return " ".join(words).strip(" .,!?")


def _track_phrase(text: str) -> str:
    """A track name for "add … to …": strip only a leading article.

    "Track 1" is a title, not filler, so the songs/tracks nouns are kept
    here; ``_resolve_tracks`` retries without one when the whole phrase finds
    nothing.
    """
    phrase = " ".join(str(text or "").split()).strip(" .,")
    phrase = re.sub(r"(?i)^(?:the|a|an|some|my|meine|meinen|den|die|das)\s+",
                    "", phrase).strip()
    return phrase


#: comparative words that move one psychoacoustic axis
PSYCHO_WORDS = {
    "faster": ("speed", 1.0), "schneller": ("speed", 1.0),
    "slower": ("speed", -1.0), "langsamer": ("speed", -1.0),
    "louder": ("energy", 1.0), "lauter": ("energy", 1.0),
    "quieter": ("energy", -1.0), "leiser": ("energy", -1.0),
    "more aggressive": ("aggression", 1.0),
    "less aggressive": ("aggression", -1.0),
    "aggressiver": ("aggression", 1.0),
    "harder": ("aggression", 1.0), "haerter": ("aggression", 1.0),
    "härter": ("aggression", 1.0),
    "softer": ("aggression", -1.0), "sanfter": ("aggression", -1.0),
    "heavier": ("aggression", 1.0), "lighter": ("aggression", -1.0),
    "brighter": ("brightness", 1.0), "heller": ("brightness", 1.0),
    "darker": ("brightness", -1.0), "dunkler": ("brightness", -1.0),
    "more danceable": ("danceability", 1.0),
    "less danceable": ("danceability", -1.0),
    "happier": ("valence", 1.0), "fröhlicher": ("valence", 1.0),
    "more upbeat": ("valence", 1.0), "sadder": ("valence", -1.0),
    "more melancholy": ("valence", -1.0),
    "calmer": ("calmness", 1.0), "ruhiger": ("calmness", 1.0),
    "more calm": ("calmness", 1.0),
    "more intense": ("calmness", -1.0), "intenser": ("calmness", -1.0),
    "denser": ("density", 1.0), "sparser": ("density", -1.0),
    "rougher": ("roughness", 1.0), "rauer": ("roughness", 1.0),
    "smoother": ("roughness", -1.0), "glatter": ("roughness", -1.0),
    "more harmonic": ("harmony", 1.0), "more dissonant": ("harmony", -1.0),
}

#: nouns that can follow "more"/"less"
PSYCHO_AXIS_NOUNS = {
    "speed": "speed", "tempo": "speed", "energie": "energy",
    "energy": "energy", "aggression": "aggression",
    "brightness": "brightness", "danceability": "danceability",
    "valence": "valence", "calmness": "calmness", "density": "density",
    "roughness": "roughness", "harmony": "harmony",
}


def psycho_adjustments(text: str) -> dict[str, float]:
    """Read "20 % faster", "much more aggressive", "less speed" into deltas."""
    folded = fold(text)
    found: dict[str, float] = {}
    amount = 0.2
    if re.search(r"(?<!\w)(?:much|viel|deutlich|way)(?!\w)", folded):
        amount = 0.3
    if re.search(r"(?<!\w)(?:a bit|slightly|etwas|leicht)(?!\w)", folded):
        amount = 0.1
    percent = re.search(r"(\d{1,2})\s*(?:%|percent|prozent)\s+(?:more|less|"
                        r"mehr|weniger)?\s*(\w+)", folded)
    if percent:
        axis = PSYCHO_WORDS.get(percent.group(2), (None, 0))
        if axis[0]:
            amount = max(0.05, min(0.6, int(percent.group(1)) / 100.0))
    for word, (axis, sign) in PSYCHO_WORDS.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", folded):
            found[axis] = found.get(axis, 0.0) + sign * amount
    # "more speed" / "less energy"
    for word, axis in PSYCHO_AXIS_NOUNS.items():
        match = re.search(rf"(?<!\w)(more|less|mehr|weniger)\s+"
                          rf"{re.escape(word)}(?!\w)", folded)
        if match:
            sign = 1.0 if match.group(1) in {"more", "mehr"} else -1.0
            found[axis] = found.get(axis, 0.0) + sign * amount
    # "speed +0.3" / "raise the aggression by 0.2"
    for word, axis in PSYCHO_AXIS_NOUNS.items():
        match = re.search(rf"(?<!\w){re.escape(word)}\s*"
                          rf"(?:by\s+|um\s+)?([+-]?\d*\.\d+)", folded)
        if match:
            found[axis] = found.get(axis, 0.0) + float(match.group(1))
    return {axis: max(-0.9, min(0.9, delta))
            for axis, delta in found.items() if abs(delta) > 0.01}


def psycho_track(text: str) -> str:
    """The track named in "the properties of X" / "tempo of X" ("" = this one)."""
    original = " ".join(str(text or "").split()).strip(" ?.!")
    match = re.search(r"(?i)\b(?:of|von|for|from)\s+(.+)$", original)
    phrase = match.group(1) if match else ""
    if not phrase:
        match = re.search(r"(?i)(?:profile|properties|eigenschaften|features|"
                          r"analyse|analyze)\s+(.+)$", original)
        phrase = match.group(1) if match else ""
    if not phrase:
        match = re.search(r"(?i)how\s+(?:fast|loud|aggressive|bright|danceable|"
                          r"calm|rough|harmonic)\s+is\s+(.+)$", original)
        phrase = match.group(1) if match else ""
    if not phrase:
        match = re.search(r"(?i)(?:tempo|bpm)\s+(?:of|von)\s+(.+)$", original)
        phrase = match.group(1) if match else ""
    phrase = re.sub(r"(?i)\b(?:this|that|the|current|die|der|das)\s+"
                    r"(?:song|track|tune|stück|stueck|titel)\b", " ", phrase)
    phrase = re.sub(r"(?i)^(?:the|a|an|this|that|current|my)\s+",
                    "", phrase.strip())
    phrase = " ".join(phrase.split()).strip(" .,:;!?-")
    if fold(phrase) in {"", "song", "track", "tune", "this", "that", "it",
                        "das", "diese", "dieses", "dem", "den"}:
        return ""
    return phrase


def extract_url(text: str) -> str | None:
    match = _rx(r"(https?://[^\s<>\"']+|www\.[^\s<>\"']+)", text)
    if not match:
        return None
    url = match.group(1).rstrip(").,;!?]")
    if url.startswith("www."):
        url = "https://" + url
    return url


def extract_urls(text: str) -> list[str]:
    """Every http(s) link in the sentence, in order, without duplicates."""
    found: list[str] = []
    for match in re.finditer(r"(https?://[^\s<>\"']+|www\.[^\s<>\"']+)",
                             text or ""):
        link = match.group(1).rstrip(").,;!?]")
        if link.startswith("www."):
            link = "https://" + link
        if link not in found:
            found.append(link)
    return found


_DECADE_WORDS = {
    "fifties": 1950, "sixties": 1960, "seventies": 1970, "eighties": 1980,
    "nineties": 1990, "noughties": 2000, "twenties": 2020,
}


def smart_rules(text: str) -> dict:
    """Turn a sentence into smart-playlist rules (genre, decade, added, …).

    Only what the sentence really says becomes a rule; anything it does not
    mention is left out, so a smart list can never be wider than the request.
    The keys are the ones `chat/smart.py` accepts.
    """
    f = " ".join(fold(text).split())
    rules: dict = {}
    include, only, exclude = extract_genres(text)
    chosen = only or include
    if chosen:
        rules["genre"] = chosen[:3]
    if exclude:
        rules["not_genre"] = exclude[:3]

    decade = re.search(r"\b(1[89]\d0|20[0-4]0)s?\b", f)
    if decade:
        year = int(decade.group(1))
        rules["year_from"], rules["year_to"] = year, year + 9
    else:
        decade = re.search(r"\b(\d{2})s\b", f)
        if decade:
            digits = int(decade.group(1))
            year = 1900 + digits if digits >= 30 else 2000 + digits
            rules["year_from"], rules["year_to"] = year, year + 9
        else:
            for word, year in _DECADE_WORDS.items():
                if re.search(rf"(?<!\w){word}(?!\w)", f):
                    rules["year_from"] = year
                    rules["year_to"] = year + 9
                    break
    years = re.search(r"\b(1[89]\d\d|20[0-4]\d)\s*(?:-|to|bis|until)\s*"
                      r"(1[89]\d\d|20[0-4]\d)\b", f)
    if years:
        rules["year_from"] = int(years.group(1))
        rules["year_to"] = int(years.group(2))
    elif "year_from" not in rules:
        since = re.search(r"\b(?:since|from|ab|seit|after|nach)\s+"
                          r"(1[89]\d\d|20[0-4]\d)\b", f)
        if since:
            rules["year_from"] = int(since.group(1))
        until = re.search(r"\b(?:before|until|bis|up to)\s+"
                          r"(1[89]\d\d|20[0-4]\d)\b", f)
        if until:
            rules["year_to"] = int(until.group(1)) - 1

    added = None
    for phrase, days in (("today", 1), ("this week", 7), ("diese woche", 7),
                         ("this month", 31), ("dieser monat", 31),
                         ("diesen monat", 31), ("this year", 365),
                         ("dieses jahr", 365)):
        if phrase in f:
            added = days
            break
    span = re.search(r"\b(?:last|past|letzte[nrs]?|vergangene[nrs]?)\s+"
                     r"(\d{1,3})\s+(day|days|tag|tage|week|weeks|woche|"
                     r"wochen|month|months|monat|monate)\b", f)
    if span:
        number = int(span.group(1))
        unit = span.group(2)
        added = number * (7 if unit.startswith(("week", "woche")) else
                          30 if unit.startswith(("month", "monat")) else 1)
    if added is None and _has(f, "recently added", "newly added", "just added",
                              "frisch importiert", "neu hinzugefugt",
                              "recently imported"):
        added = 30
    if added:
        rules["added_days"] = added

    length = re.search(r"\b(longer than|over|more than|at least|langer als|"
                       r"uber|ueber)\s+(\d{1,2})\s*(?:min|mins|minutes?|"
                       r"minuten)\b", f)
    if length:
        rules["min_duration"] = int(length.group(2)) * 60
    length = re.search(r"\b(shorter than|under|less than|kurzer als|"
                       r"kuerzer als)\s+(\d{1,2})\s*(?:min|mins|minutes?|"
                       r"minuten)\b", f)
    if length:
        rules["max_duration"] = int(length.group(2)) * 60

    rate = re.search(r"\b(at least|over|mindestens)\s+(\d{2,3})\s*"
                     r"(?:kbps|kbit|kbit/s)\b", f)
    if rate:
        rules["min_bitrate"] = int(rate.group(2))
    rate = re.search(r"\b(under|below|less than|hochstens|hoechstens)\s+"
                     r"(\d{2,3})\s*(?:kbps|kbit|kbit/s)\b", f)
    if rate:
        rules["max_bitrate"] = int(rate.group(2))

    if _has(f, "oldest first", "aelteste zuerst", "alteste zuerst"):
        rules["sort"] = "oldest"
    elif _has(f, "alphabetical", "by title", "nach titel", "alphabetisch"):
        rules["sort"] = "title"
    elif _has(f, "by artist", "nach interpret"):
        rules["sort"] = "artist"
    elif _has(f, "shuffled", "mixed", "durchmischt", "zufallig",
              "zufaellig"):
        rules["sort"] = "shuffle"

    limit = re.search(r"\b(?:the\s+)?(\d{1,3})\s+(?:newest|latest|most "
                      r"recent|tracks|songs|stucke|stuecke|titel)\b", f)
    if limit and (rules or "newest" in f or "latest" in f):
        rules["limit"] = int(limit.group(1))
    return rules


def stats_focus(text: str) -> str:
    """Which slice of the statistics a sentence asks for ("" = the whole).

    The order is strongest first: "how complete is the library" is coverage,
    not a tag count, and "how is the quality" is not a format list.
    """
    f = " " + " ".join(fold(text).split()) + " "
    if _has(f, "coverage", "complete", "completeness", "vollstandig",
                 "vollstaendig", "wie vollstandig", "how complete"):
        return "coverage"
    if _has(f, "quality", "bitrate", "lossless", "verlustfrei",
                 "qualitat", "klangqualitat", "sound quality"):
        return "quality"
    if _has(f, "genre", "genres"):
        return "genres"
    if _has(f, "artist", "artists", "interpret", "interpreten",
                 "performer", "performers", "kunstler", "kuenstler"):
        return "artists"
    if _has(f, "album", "albums", "alben"):
        return "albums"
    if _has(f, "decade", "decades", "jahrzehnt", "jahrzehnte", "years",
                 "jahre", "jahrgang"):
        return "decades"
    if _has(f, "format", "formats", "codec", "codecs", "container",
                 "dateityp", "dateitypen", "file type", "file types"):
        return "formats"
    time_phrase = _has(f, "this week", "this month", "this year", "today",
                       "last week", "last month", "last year", "recently",
                       "diese woche", "diesen monat", "dieses jahr", "heute",
                       "letzte woche", "letzten monat", "vergangenen",
                       "der letzten", "den letzten")
    if _has(f, "added", "hinzugefugt", "dazugekommen", "imported",
            "importiert", "newest", "neueste", "recent",
            "recently added") or (
            time_phrase and _has(f, "add", "neu", "neue", "import")):
        return "added"
    if _has(f, "longest", "shortest", "langste", "kurzeste",
                 "average length", "durchschnittliche lange",
                 "track length", "song length", "track lengths"):
        return "length"
    if _has(f, "playlist", "playlists", "listen"):
        return "playlists"
    return ""


def extract_paths(text: str) -> list[str]:
    """Quoted paths and bare tokens that end in an audio extension."""
    found: list[str] = []
    for match in re.finditer(r"[\"“”']([^\"“”']+)[\"“”']", text):
        candidate = match.group(1).strip()
        if _looks_like_path(candidate):
            found.append(candidate)
    for token in text.split():
        token = token.strip(".,;:!?()[]'\"“”")
        if _looks_like_path(token):
            found.append(token)
    seen: set[str] = set()
    out = []
    for path in found:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _looks_like_path(text: str) -> bool:
    if not text or len(text) > 500:
        return False
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return False
    return bool(re.search(r"\.(mp3|mp2|m4a|mp4|opus|ogg|flac|wav|wma|aac)$",
                          lowered)) or ("/" in text and "." in text)


def lyric_offset(text: str) -> dict | None:
    """The timing shift a sentences asks for, e.g. "half a second later".

    Returns ``{"value": seconds, "relative": True}`` — the shift is applied on
    top of whatever the song already carries, so asking twice moves it twice.
    "the lyrics are too early" moves them later, "too late" moves them
    earlier: the complaint names the symptom, not the direction.
    """
    folded = " " + " ".join(fold(text).split()) + " "
    if _has(folded, "reset the lyric timing", "reset the lyrics timing",
            "remove the offset", "clear the offset", "no offset",
            "setz die lyrics zuruck", "setz den text zuruck",
            "setz den songtext zuruck", "ohne versatz", "offset zurucksetzen",
            "offset zuruecksetzen"):
        return {"value": 0.0, "relative": False}
    value: float | None = None
    match = _rx(r"(\d+(?:[.,]\d+)?)\s*(?:s\b|secs?\b|seconds?\b|"
                r"sekunden?\b)", folded)
    if match:
        value = float(match.group(1).replace(",", "."))
    if value is None:
        if _has(folded, "half a second", "halbe sekunde", "halben sekunde",
                "halber sekunde"):
            value = 0.5
        elif _has(folded, "quarter of a second", "viertel sekunde",
                  "viertelsekunde"):
            value = 0.25
        elif _has(folded, "one second", "a second", "eine sekunde",
                  "einer sekunde"):
            value = 1.0
    if value is None:
        if not _has(folded, "too early", "too late", "zu fruh", "zu frueh",
                    "zu spat", "zu spaet"):
            return None
        value = 0.5                 # a nudge, not a re-transcription
    if _has(folded, "too early", "zu fruh", "zu frueh"):
        sign = 1.0                  # they show before the singing: go later
    elif _has(folded, "too late", "zu spat", "zu spaet"):
        sign = -1.0
    elif _has(folded, "earlier", "fruher", "frueher", "nach vorne",
              "vorziehen", "advance"):
        sign = -1.0
    else:
        sign = 1.0                  # "later", "nach hinten", or no direction
    return {"value": round(sign * value, 2), "relative": True}


def extract_count(text: str) -> int | None:
    match = _rx(r"(\d{1,3})\s*(?:songs?|tracks?|titel|lieder|stücke|stuecke|"
                r"nummers?|tunes?)", text)
    if match:
        return max(1, min(500, int(match.group(1))))
    match = _rx(r"(?:playlist|liste|mix)\s+(?:with|mit)\s+(\d{1,3})\b", text)
    if match:
        return max(1, min(500, int(match.group(1))))
    return None


#: number words accepted in a length ("two-hour", "zwei Stunden")
_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "ninety": 90,
    "ein": 1, "eine": 1, "einer": 1, "eins": 1, "zwei": 2, "drei": 3,
    "vier": 4, "fünf": 5, "funf": 5, "sechs": 6, "sieben": 7, "acht": 8,
    "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "zwolf": 12,
    "fünfzehn": 15, "funfzehn": 15, "zwanzig": 20, "dreißig": 30,
    "dreissig": 30, "vierzig": 40, "neunzig": 90,
}


def _number(word: str) -> float | None:
    word = fold(word)
    if re.fullmatch(r"\d+(?:[.,]\d+)?", word):
        return float(word.replace(",", "."))
    return _WORD_NUM.get(word)


def extract_duration_min(text: str) -> float | None:
    folded = fold(text)
    total = 0.0
    # "90 min", "2 hours", "two-hour", "zwei Stunden", "1,5 h"
    hours = _rx(r"([\wäöüß]+|\d+(?:[.,]\d+)?)\s*-?\s*"
                r"(?:hours?|stunden?|std\.?|h)\b", folded)
    minutes = _rx(r"([\wäöüß]+|\d+)\s*-?\s*"
                  r"(?:minutes?|minuten?|mins?|min\.?)\b", folded)
    if hours:
        value = _number(hours.group(1))
        if value is not None:
            total += value * 60
    if minutes:
        value = _number(minutes.group(1))
        if value is not None:
            total += value
    if not total:
        if _has(folded, "halbe stunde", "half an hour"):
            total = 30
        elif _has(folded, "eine stunde", "one hour", "an hour"):
            total = 60
        elif _has(folded, "zwei stunden", "two hours"):
            total = 120
    return total or None


def _strip_duration(text: str) -> str:
    """Remove "30 minutes", "two-hour", "zwei Stunden" from a request."""
    words = r"\d+(?:[.,]\d+)?|" + "|".join(
        re.escape(word) for word in sorted(_WORD_NUM, key=len, reverse=True))
    unit = r"hours?|stunden?|std\.?|h|minutes?|minuten?|mins?|min\.?"
    stripped = re.sub(rf"(?i)\b(?:{words})\s*-?\s*(?:{unit})\b\.?", " ", text)
    if stripped != text:
        stripped = re.sub(r"(?i)\b(?:of|von|vom|an)\b", " ", stripped)
        stripped = re.sub(r"(?i)^\s*(?:eine|einen|einer)\s+", " ", stripped)
    return " ".join(stripped.split())


def extract_explicit_name(text: str) -> str | None:
    quoted = re.search(r"[\"“”']([^\"“”']{1,80})[\"“”']", text)
    if quoted and not _looks_like_path(quoted.group(1)):
        return quoted.group(1).strip()
    match = _rx(r"(?:called|named|namens|heißt|heisst|title it|nenn(?:e)? es|"
                r"call it)\s+[\"“”']?([\wÄÖÜäöüß .'&+-]{1,60}?)"
                r"(?:\s+playlist|\s+liste|$|[,.!?])", text)
    if match:
        name = match.group(1).strip(" .'\"")
        if fold(name) not in {"playlist", "liste", "mix"}:
            return name
    return None


def extract_genres(text: str) -> tuple[list[str], list[str], list[str]]:
    """(include, only, exclude) genre names."""
    folded = fold(text)
    include: list[str] = []
    only: list[str] = []
    exclude: list[str] = []
    words = set(re.findall(r"[0-9a-z\-]+", folded))
    for word, genre in GENRE_WORDS.items():
        if word not in words:
            continue
        if re.search(rf"(?:only|just|nur|ausschließlich|ausschliesslich)\s+"
                     rf"{re.escape(word)}", folded):
            only.append(genre)
        elif re.search(rf"(?:no|without|ohne|kein|keine|not|exclude|"
                       rf"excluding|nicht)\s+{re.escape(word)}", folded):
            exclude.append(genre)
        else:
            include.append(genre)
    # a genre mentioned after "no" in a list ("no metal or punk")
    for match in re.finditer(r"(?:no|without|ohne|kein|keine|not)\s+"
                             r"([a-z\-]+(?:\s+(?:or|oder|and|und)\s+"
                             r"[a-z\-]+)*)", folded):
        for token in re.findall(r"[a-z\-]+", match.group(1)):
            genre = GENRE_WORDS.get(token)
            if genre and genre not in exclude:
                exclude.append(genre)
                if genre in include:
                    include.remove(genre)
    seen: set[str] = set()
    include = [g for g in include if not (g in seen or seen.add(g))]
    only = [g for g in only if not (g in seen or seen.add(g))]
    exclude = [g for g in exclude if not (g in seen or seen.add(g))]
    return include, only, exclude


def extract_seed(text: str) -> str | None:
    match = _rx(r"(?:sounds? like|similar to|like|ähnlich wie|so wie|"
                r"im stil von|in the style of|something by|von|by)\s+"
                r"[\"“”']?([\wÄÖÜäöüß .'&+-]{2,60}?)"
                r"(?:\s+(?:playlist|liste|mix|music|musik|songs?|tracks?)|"
                r"$|[,.!?])", text)
    if not match:
        return None
    value = match.group(1).strip(" .'\"")
    if fold(value) in {"this", "that", "it", "the same", "a playlist",
                       "a mix", "playlist", "music", "musik", "something"}:
        return None
    if fold(value).startswith(("a ", "an ", "the ")):
        value = value.split(" ", 1)[1]
    return value or None


def _current_track(ctx: Context, catalog) -> Track | None:
    if ctx.current is not None:
        return ctx.current
    if ctx.last_tracks:
        return ctx.last_tracks[0]
    return None


#: words that only point at a list member, not at a search string
_POINTERS = {"this", "that", "it", "them", "these", "those", "das", "die",
             "den", "diese", "dieses", "die da", "the one"}

#: connectors that can separate several tracks in one sentence
_TRACK_SPLIT = re.compile(r"\s*(?:,|;|\+|&|\band\b|\bor\b|\bund\b|\boder\b)\s*",
                          re.IGNORECASE)


#: "transcribe this song" points at the current track, not at a search string
_THIS_TRACK = {
    "this", "this song", "this track", "this one", "that song", "that track",
    "the song", "the track", "song", "track", "lied", "the current song",
    "the playing song", "the song playing", "currently playing", "current",
    "now playing", "das lied", "dieses lied", "den song", "diesen song",
    "den track", "diesen track", "das aktuelle lied", "den aktuellen song",
    "it", "das", "es",
}


def _transcribe_target(text: str, ctx: Context, catalog, playing) -> str:
    """The track a transcribe request names ("" means: the playing one).

    "transcribe the lyrics of Merlin_wooden.mp3" names a file, "transcribe
    this song" points at the playhead.  Only a name that really resolves is
    accepted; anything else keeps whatever is playing, so a typo does not
    silently transcribe the wrong song under a guess.
    """
    original = str(text or "").strip()
    match = re.search(
        r"(?i)(?:transcribe|transkribiere)\s+(?:the\s+|die\s+|den\s+)?"
        r"(?:lyrics?|songtext\w*|liedtext\w*|gesangstext\w*|text|words?)\s+"
        r"(?:of|from|for|von|vom|des|der|zu|zum)\s+(.+)$", original)
    if match is None:
        match = re.search(r"(?i)\blyrics?\s+for\s+(.+)$", original)
    if match is None:
        match = re.search(r"(?i)(?:transcribe|transkribiere)\s+"
                          r"(?:the\s+|die\s+|den\s+)?(.+)$", original)
    if match is None:
        return playing.rel if playing else ""
    name = match.group(1).strip(" .,?!\"“”'")
    name = re.sub(r"(?i)\s+(?:please|bitte)$", "", name).strip()
    if not name or fold(name) in _THIS_TRACK:
        return playing.rel if playing else ""
    hits = catalog.search(name, limit=1)
    return hits[0].rel if hits else (playing.rel if playing else "")


def _resolve_tracks(what: str, ctx: Context, catalog) -> tuple[list[str], list[str]]:
    """(rels, unresolved) for "add X and Y to …".

    A whole phrase that finds something wins first, so "Simon and Garfunkel"
    stays one artist.  A disjunction ("or"/"oder") or a comma always counts as
    a list, and anything that finds nothing is split and retried part by part.
    """
    text = " ".join(str(what or "").split()).strip(" .,")
    if not text:
        return [], []
    folded = fold(text)
    if folded in _POINTERS:
        track = _current_track(ctx, catalog)
        return ([track.rel] if track else []), ([] if track else [text])

    def lookup(part: str) -> str | None:
        part = part.strip(" .,")
        if not part:
            return None
        key = fold(part)
        if key in _POINTERS:
            track = _current_track(ctx, catalog)
            return track.rel if track else None
        if part.isdigit():
            index = int(part) - 1
            if 0 <= index < len(ctx.last_tracks):
                return ctx.last_tracks[index].rel
            return None
        number = _ordinal(part)
        if number is not None and len(part.split()) <= 3:
            index = number - 1
            if 0 <= index < len(ctx.last_tracks):
                return ctx.last_tracks[index].rel
            return None
        hits = catalog.search(part, limit=1)
        if not hits:
            # "add the track Test Tone": the noun was filler, the name is not
            bare = re.sub(r"(?i)^(?:songs?|tracks?|tunes?|titel|titles?)\s+",
                          "", part).strip()
            if bare and bare != part:
                hits = catalog.search(bare, limit=1)
        return hits[0].rel if hits else None

    whole = lookup(text)
    joined = bool(re.search(r"\s+(?:or|oder)\s+", text, re.IGNORECASE)) \
        or "," in text or ";" in text
    if whole and not joined:
        return [whole], []
    parts = [part for part in _TRACK_SPLIT.split(text) if part.strip()]
    if len(parts) <= 1:
        return ([whole] if whole else []), ([] if whole else [text])
    rels: list[str] = []
    missing: list[str] = []
    for part in parts:
        rel = lookup(part)
        if rel:
            rels.append(rel)
        else:
            missing.append(part.strip(" .,"))
    # preserve order, drop duplicates
    seen: set[str] = set()
    rels = [rel for rel in rels if not (rel in seen or seen.add(rel))]
    return rels, missing


def _name_for(moods: dict[str, float], text: str) -> str:
    folded = fold(text)
    for word in MOOD_WORDS:
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", folded):
            return word.title().replace("-", " ")
    if moods:
        first = max(moods, key=lambda key: moods[key])
        return first
    return "Mix"


def plan(text: str, ctx: Context, catalog, profile=None) -> Plan | None:
    original = text.strip()
    if not original:
        return None
    f = " ".join(fold(original).split())

    # -- Spotify comes before the generic link rule: its URL is a playlist ---

    spotify_link = re.search(
        r"(?:https?://)?open\.spotify\.com/(?:intl-[a-z-]+/)?"
        r"(?:playlist|album|track)/[A-Za-z0-9]+", original, re.IGNORECASE)
    if spotify_link:
        return Plan("import-playlist", {"spotify": spotify_link.group(0)},
                    confidence=0.95)

    # -- links and files come first: they are unambiguous ------------------

    links = extract_urls(original)
    url = links[0] if links else None
    if url:
        if _has(f, "play", "stream", "spiele", "abspielen", "hör", "hoer"):
            return Plan("play-music", {"target": url}, confidence=0.9)
        artist = None
        match = _rx(r"(?:by|von|artist|künstler|kuenstler)\s+"
                    r"([\wÄÖÜäöüß .'&+-]{2,50})", original)
        if match:
            artist = match.group(1).strip(" .,'\"")
        args = {"url": url, "artist": artist}
        if len(links) > 1:
            args["urls"] = links
        return Plan("import-audio", args, confidence=0.95)

    # a library-data export is a file, but it is not music: it is read back
    # into the analysis databases, matched by audio fingerprint
    export_named = _library_export_path(original)
    if export_named:
        importing = _has(f, "import", "importiere", "einlesen", "einlese",
                         "restore", "wiederherstellen", "zurückspielen",
                         "zurueckspielen")
        return Plan("library-data",
                    {"action": "import" if importing else "preview",
                     "file": export_named}, confidence=0.92)

    paths = extract_paths(original)
    # "transcribe the lyrics of Merlin_wooden.mp3" names a file to read, not a
    # file to play: the lyric rules below own that sentence
    if paths and not _has(f, "transcribe", "transkribiere", "lyrics", "lyric",
                          "songtext", "liedtext", "gesangstext", "untertitel"):
        if _has(f, "play", "spiele", "abspielen", "hör", "hoer", "queue"):
            return Plan("play-music", {"target": paths[0]}, confidence=0.9)
        if _has(f, "add", "file", "import", "nimm", "füge", "fuege", "einordnen"):
            args = {"path": paths[0]}
            genre_match = _rx(r"(?:genre|als)\s+([\wÄÖÜäöüß-]+)", original)
            if genre_match:
                args["genre"] = genre_match.group(1)
            return Plan("add-file", args, confidence=0.9)
        return Plan("play-music", {"target": paths[0]}, confidence=0.7)

    # -- the import source: search, then download a chosen result --------------------

    yt_named = _has(f, "youtube", "you tube", "yt")

    if f in {"downloads", "download status", "download queue",
             "download progress", "youtube downloads", "import status",
             "downloads status", "warteschlange", "download warteschlange"} \
            or _has(f, "what's downloading", "whats downloading",
                    "is it done", "is it finished", "what is downloading",
                    "läuft der download", "laeuft der download",
                    "ist es fertig", "download status", "import status"):
        return Plan("import-audio", {"action": "status"}, confidence=0.9)

    # a failed import keeps its place in the footer until it is tried again or
    # cleared, so both need a spoken command; they come before the download
    # rule, because "download it again" means the failed job, not the hit
    if _has(f, "retry", "again", "erneut", "nochmal", "noch einmal",
            "neu versuchen", "erneut versuchen") \
            and _has(f, "download", "downloads", "import", "imports",
                     "youtube", "failed", "failure", "fehlgeschlagen",
                     "herunterladen", "herunter", "runter") \
            and not _has(f, "search", "suche", "finde", "find"):
        return Plan("import-audio", {"action": "retry"}, confidence=0.9)

    if _has(f, "clear", "dismiss", "acknowledge", "hide", "remove",
            "discard", "aufraumen", "wegraumen", "entfernen", "entferne",
            "losche", "loesche", "verwerfen") \
            and (_has(f, "failed download", "failed import", "failed",
                      "error", "fehler")
                 or "fehlgeschlagen" in f) \
            and not _has(f, "search", "suche"):
        return Plan("import-audio", {"action": "dismiss"}, confidence=0.85)

    if _has(f, "cancel", "abort", "stoppe", "abbrechen", "brich", "kill",
            "verwerfe") \
            and _has(f, "download", "downloads", "import", "imports",
                     "youtube", "herunterladen", "herunter", "runter") \
            and not _has(f, "search", "suche", "finde", "find"):
        return Plan("import-audio", {"action": "cancel"}, confidence=0.85)

    if _has(f, "move", "schiebe", "verschiebe", "prioritise", "prioritize",
            "queue", "einordnen") \
            and _has(f, "download", "downloads", "import", "imports",
                     "youtube", "warteschlange") \
            and _has(f, "up", "down", "first", "next", "top", "hoch", "runter",
                     "oben", "unten", "vorne", "hinten", "earlier", "later",
                     "spater", "später") \
            and not _has(f, "search", "suche"):
        direction = ("down" if _has(f, "down", "later", "spater", "später",
                                   "last", "back", "runter", "hinten")
                     else "up")
        return Plan("import-audio", {"action": "move",
                                       "direction": direction}, confidence=0.85)

    pending = bool(getattr(ctx, "last_youtube", None))
    wants_download = _has(
        f, "download it", "download that", "download this", "download them",
        "download number", "download no", "download the first",
        "download the second", "download the third", "download all",
        "grab it", "get it", "import it", "integrate it", "integrate that",
        "add it to my library", "add that to my library",
        "lade es herunter", "lade das herunter", "herunterladen",
        "integriere das", "integriere es", "nimm das", "hole das")
    if pending and (wants_download or (
            re.search(r"(?:^|\s)download(?:\s|$)", f)
            and (_ordinal(original) is not None
                 or re.search(r"(?<!\w)\d{1,2}(?!\w)", original)))):
        index = None
        numbered = re.findall(r"(?:number|no\.?|#)\s*(\d{1,2})", original,
                              re.IGNORECASE)
        if numbered:
            indexes = [int(value) for value in numbered]
        elif _ordinal(original) is not None:
            indexes = [_ordinal(original)]
        else:
            indexes = [int(value) for value in
                       re.findall(r"(?<!\w)(\d{1,2})(?!\w)", original)]
        args = {"action": "download"}
        if indexes:
            args["indexes"] = indexes[:5]
        if _has(f, "all", "alle", "everything"):
            args["all"] = True
        artist = _rx(r"(?:by|von)\s+([\wÄÖÜäöüß .'&+-]{2,60})$", original)
        if artist:
            args["artist"] = artist.group(1).strip(" .'\"")
        return Plan("import-audio", args, confidence=0.9)

    if yt_named and _has(f, "search", "find", "look", "suche", "finde", "such",
                         "similar", "ähnliche", "aehnliche"):
        query = ""
        for pattern in (
                r"(?:search|find|look)\s+(?:on|in|at)?\s*(?:youtube|you tube|yt)"
                r"\s+(?:for\s+)?(.+)$",
                r"(?:youtube|you tube|yt)\s+(?:search|find)\s+(?:for\s+)?(.+)$",
                r"(?:search|find|look)\s+(?:for\s+)?(.+?)\s+(?:on|in)\s+"
                r"(?:youtube|you tube|yt)$",
                r"(?:suche|finde|such)\s+(?:auf\s+)?(?:youtube|you tube|yt)"
                r"\s+(?:nach\s+)?(.+)$",
                r"(?:youtube|you tube|yt)\s+(?:suche|finde)\s+(?:nach\s+)?(.+)$",
                r"(?:suche|finde)\s+(.+?)\s+auf\s+(?:youtube|you tube|yt)$"):
            match = _rx(pattern, original)
            if match:
                query = match.group(1)
                break
        query = re.sub(r"(?i)\s+(?:and|then|und|dann)\s+(?:download|grab|get|"
                       r"integrate|import|add)\b.*$", "", query)
        query = query.strip(" .,!?\"'")
        similar = _has(f, "similar", "ähnliche", "aehnliche", "like this",
                       "so wie das", "mehr davon", "more like this")
        if fold(query) in {"similar music", "music like this", "more like this",
                           "something similar", "similar", "ähnliche musik",
                           "so etwas", "mehr davon", "this", "that", "das"}:
            query = ""
        return Plan("import-audio", {
            "action": "search", "query": query, "similar": similar,
        }, confidence=0.9)

    # -- feedback and learning --------------------------------------------

    if (_has(f, "more like this", "more of this", "more like that", "mehr davon",
             "nochmal sowas", "noch so etwas", "similar", "ähnliches")
            or re.search(r"(?:^|\s)(?:more|mehr)(?:\s|$)", f)) \
            and not _has(f, "youtube", "you tube", "yt", "radio", "sender",
                         "endless", "endlos",
                         # a lyric request compares words, not sound
                         *LYRIC_CONTEXT) \
            and not psycho_adjustments(original):
        seed_track = _current_track(ctx, catalog)
        if seed_track is not None:
            return Plan("make-playlist", {
                "name": "More like " + (seed_track.artist or "this"),
                "seed": seed_track.rel,
                "count": 25,
                "autoplay": True,
                # “more like this” builds a queue, not a kept list
                "adhoc": True,
            }, confidence=0.85)
        return None

    # the dislike check must come first: "I don't like this" contains
    # "like this", so the positive rule would claim it
    if _has(f, "not this", "not this one", "skip this", "hate this",
            "don't like this", "dont like this", "do not like this",
            "gefallt mir nicht", "nicht mein ding", "hasse das"):
        track = _current_track(ctx, catalog)
        return Plan("learn-preferences", {
            "kind": "dislike",
            "value": track.rel if track else "",
            "label": track.label() if track else "",
        }, confidence=0.9)

    if (re.search(r"(?:^|\s)(?:i\s+)?(?:really\s+)?(?:like|love)\s+this"
                  r"(?:\s+(?:song|track|one|tune))?(?:\s|$)", f)
            and not psycho_adjustments(original)
            and not _has(f, *LYRIC_CONTEXT)) \
            or re.search(r"(?:das|der|die)\s+gefallt mir|gefallt mir"
                         r"(?:\s|$)", f) \
            or f in {"like", "love", "mag das"}:
        track = _current_track(ctx, catalog)
        if track is not None:
            return Plan("learn-preferences", {
                "kind": "like", "value": track.rel,
                "label": track.label(),
            }, confidence=0.95)
        return Plan("learn-preferences", {"kind": "like", "value": ""},
                    confidence=0.4)

    if re.search(r"(?:remember|merke dir|merk dir|note that|notiere)\s+(.+)", f):
        match = re.search(r"(?:remember|merke dir|merk dir|note that|notiere)"
                          r"\s+(.+)", original, re.IGNORECASE)
        note = re.sub(r"(?i)^that\s+", "", match.group(1).strip(" ."))
        return Plan("learn-preferences", {
            "kind": "note", "value": note,
        }, confidence=0.9)

    mapping = _rx(r"(?:when i say|wenn ich)\s+[\"“”']?([\wÄÖÜäöüß-]+)"
                  r"[\"“”']?\s+(?:i mean|sage,?\s*meine ich|meine ich)\s+(.+)",
                  original)
    if mapping:
        word = mapping.group(1)
        moods = moods_from_text(mapping.group(2), profile)
        if moods:
            return Plan("learn-preferences", {
                "kind": "mapping", "value": word,
                "moods": sorted(moods, key=lambda key: -moods[key]),
            }, confidence=0.95)
        return Plan("learn-preferences", {
            "kind": "note",
            "value": f"When I say {word} I mean {mapping.group(2).strip()}",
        }, confidence=0.75)

    if _has(f, "what have you learned", "what do you know about me",
            "was hast du gelernt", "was weisst du ueber mich",
            "was weißt du über mich", "show my profile", "show profile",
            "mein profil", "my profile", "what do you remember") \
            or f in {"learned", "gelernt", "/learn"}:
        return Plan("learn-preferences", {"kind": "show"}, confidence=0.9)

    # -- history and the skill agent --------------------------------------

    if _has(f, "undo the last playlist change", "undo that playlist change",
            "undo the playlist change", "undo the last playlist",
            "take back the last playlist", "reverse the last playlist change",
            "mach die letzte playlist änderung rückgängig",
            "mach die letzte playlist aenderung rueckgaengig",
            "nimm die letzte playlist änderung zurück",
            "nimm die letzte playlist aenderung zurueck",
            "letzte playlist änderung rückgängig",
            "letzte playlist aenderung rueckgaengig") \
            or re.search(r"^(?:undo|ruckgangig|rueckgaengig)\b.*"
                         r"(?:playlist|liste)", f):
        return Plan("activity", {"action": "undo"}, confidence=0.9)

    if _has(f, "recent changes", "recently changed", "what changed",
            "what did you change", "what have you changed",
            "what has changed", "last changes", "change log", "changelog",
            "was hat sich geändert", "was hat sich geaendert",
            "was hast du geändert", "was hast du geaendert",
            "letzte änderungen", "letzte aenderungen",
            "änderungsverlauf", "aenderungsverlauf") \
            or f in {"changes", "/changes", "änderungen", "aenderungen"}:
        return Plan("activity", {"action": "changes"}, confidence=0.9)

    if _has(f, "activity", "activities", "history", "verlauf", "aktivitäten",
            "aktivitaeten", "what did i do", "what have i done",
            "what did i play", "what have i played", "what did i listen to",
            "listening history", "play history", "recent activity",
            "past activities", "was habe ich gemacht", "was habe ich gespielt",
            "was habe ich gehoert", "was habe ich gehört",
            "letzte aktivitäten", "letzte aktivitaeten") \
            or f in {"activity", "aktivität", "aktivitaet", "history",
                     "/activity", "/history"}:
        return Plan("activity", {}, confidence=0.9)

    if _has(f, "teach that", "teach me that", "add that skill", "add this skill",
            "learn that skill", "use that skill", "accept that skill",
            "approve that skill", "keep that skill", "remember that skill",
            "übernimm das", "uebernimm das", "lerne das", "lerne diesen befehl",
            "akzeptiere das", "ja, lern das", "ja lern das") \
            or re.search(r"^(?:yes|ja)[,! ]+(?:add|learn|keep|teach)\b", f):
        return Plan("agent", {"action": "approve"}, confidence=0.85)

    if _has(f, "skill request", "skill requests", "unknown requests",
            "what did you not understand", "was hast du nicht verstanden",
            "agent requests", "agent status", "skill proposals",
            "vorschläge für skills", "vorschlaege fuer skills"):
        return Plan("agent", {"action": "show"}, confidence=0.85)

    if _has(f, "discard that", "forget that skill", "reject that skill",
            "throw that away", "verwirf das", "vergiss das"):
        return Plan("agent", {"action": "reject"}, confidence=0.85)

    # -- lyrics as a file, and the timing nudge ----------------------------
    #
    # Both need a lyric word; the block sits before the line questions so
    # "shift the lyrics half a second later" is not read as "which line".

    lyric_word = _has(f, "lyrics", "lyric", "songtext", "songtexte",
                      "liedtext", "liedtexte", "untertitel", "subtitle",
                      "karaoke", "lrc")
    if lyric_word and _has(f, "export", "save", "write", "make", "create",
                           "erzeuge", "erstell", "exportiere", "schreibe",
                           "speichere", "sichere"):
        if _has(f, "lrc", "sidecar", "subtitle file", "untertitel datei",
                "untertitel-datei", "textdatei"):
            path = ""
            path_match = _rx(r"(\S+\.lrc)\b", original)
            if path_match:
                path = path_match.group(1).strip(" \"'")
            return Plan("lyrics", {
                "action": "export", "path": path,
                "overwrite": _has(f, "overwrite", "replace", "uberschreib",
                                  "ueberschreib", "ersetz"),
            }, confidence=0.9)
    if lyric_word and _has(f, "shift", "move", "nudge", "offset", "delay",
                           "align", "sync", "verschieb", "verschiebe",
                           "verruck", "verrueck", "nach vorne", "nach hinten",
                           "zu fruh", "zu frueh", "zu spat", "zu spaet",
                           "too early", "too late", "timing", "versatz"):
        shift = lyric_offset(original)
        if shift is not None:
            return Plan("lyrics", {"action": "offset", **shift},
                        confidence=0.9)

    # -- the lyric line at the playhead ------------------------------------
    #
    # "list the lyrics of the currently playing song" wants the words, not a
    # word to search for: the show verbs and the song pointers are stripped in
    # `lyric_text`, and this block answers the line questions before "next"
    # is read as a transport command.  A real search term ("songs about a
    # boat") is left to the lyric search below.
    line_word = _has(f, "line", "lines", "zeile", "zeilen", "textzeile",
                     "textzeilen", "verse", "verses")
    words_cue = _has(f, "lyrics", "lyric", "songtext", "songtexte", "liedtext",
                     "liedtexte", "gesangstext", "words", "word", "text")
    at_playhead = _has(f, *NOW_POINTERS) or _has(
        f, "playing", "played", "current", "currently", "now", "laufende",
        "laufenden", "aktuell", "aktuelle", "aktuellen", "gerade", "eben",
        "jetzt", "wo sind wir", "where are we", "where we are",
        "being sung", "wird gesungen", "gesungen", "wie weit", "how far")
    at_line = _has(f, "which line", "what line", "welche zeile", "welcher vers",
                   "where are we", "where we are", "wo sind wir", "how far",
                   "wie weit", "being sung", "wird gesungen")
    sung = _has(f, "being sung", "wird gesungen", "gesungen", "what is sung",
                "was gesungen wird")
    ahead = _has(f, "next", "following", "after this", "naechste", "nächste",
                 "naechsten", "naechster", "kommt", "danach", "als nachstes",
                 "als naechstes")
    if (line_word or words_cue or sung) and not _has(
            f, "playlist", "playlists", "add", "download", "youtube",
            "mention", "mentions", "erwahnt", "erwähnt", "about", "uber",
            "über", "search", "suche", "find", "which songs", "transcribe",
            "transkribiere", "transcription", "transkription", "setup",
            "status", "progress", "fortschritt"):
        if (line_word or at_line) and ahead:
            return Plan("lyrics", {"action": "next"}, confidence=0.85)
        if (line_word or at_line or sung) and at_playhead:
            return Plan("lyrics", {"action": "now"}, confidence=0.85)
        if words_cue and at_playhead:
            return Plan("lyrics", {"action": "show", "track": ""},
                        confidence=0.85)

    # -- endless mode -----------------------------------------------------
    #
    # Endless follows the song that just played and repeats the queue when
    # nothing fits.  The phrases need a mode word, so "Endless Love" stays a
    # track and "play some rock" stays a play request.

    endless_cue = _has(f, "endlos", "endlosmodus", "ohne ende",
                       "never stop the music", "never stop playing",
                       "keep the music going", "keep playing forever")
    if not endless_cue and re.search(
            r"\bendless\b(?:\s+(?:mode|modus|music|playback|queue))?", f):
        endless_cue = (len(f.split()) <= 2
                       or _has(f, "mode", "modus", "on", "off", "stop",
                               "status", "keep", "more", "endlos"))
    if endless_cue and not _has(f, "youtube", "download", "playlist",
                                "playlists", "playliste", "spotify"):
        keep_going = _has(f, "never stop the music", "never stop playing",
                          "keep the music going", "keep playing forever",
                          "ohne ende")
        if _has(f, "status", "is endless", "is it on", "still on", "laeuft",
                "lauft", "läuft", "is the endless"):
            return Plan("endless", {"action": "status"}, confidence=0.9)
        if not keep_going and _has(f, "off", "stop", "disable", "aus",
                                   "beenden", "beende", "ausschalten",
                                   "no longer"):
            return Plan("endless", {"action": "stop"}, confidence=0.9)
        if not keep_going and _has(f, "more", "mehr", "add", "nachschub",
                                   "top up", "weiter"):
            return Plan("endless", {"action": "more"}, confidence=0.85)
        return Plan("endless", {"action": "start"}, confidence=0.9)

    # -- radio mode -------------------------------------------------------
    #
    # Radio is a mode, not a playback request: it keeps the queue topped up
    # from the sound of a seed song.  The phrases are explicit, so "Radio Ga
    # Ga" stays a track and "play some rock" stays a play request.

    if _has(f, "radio", "radiostation", "radio station", "radio sender",
            "radio mode", "radio-modus") \
            and not _has(f, "youtube", "download", "playlist", "playlists",
                         "playliste", "spotify", "podcast"):
        if _has(f, "stop", "end the radio", "radio off", "radio aus",
                "schalte das radio aus", "schalte radio aus",
                "mach das radio aus", "mach radio aus", "beende das radio",
                "beende radio", "radio ausmachen", "turn off the radio",
                "turn the radio off", "radio stoppen", "stoppe das radio",
                "stoppe radio", "radio ausgeschaltet"):
            return Plan("radio", {"action": "stop"}, confidence=0.9)
        if _has(f, "status", "is the radio", "is radio", "laeuft das radio",
                "lauft das radio", "läuft das radio", "welches radio",
                "radio on?", "is the radio on", "radio an?"):
            return Plan("radio", {"action": "status"}, confidence=0.9)
        more_radio = _rx(r"(?:radio)[^.]*\b(?:more|mehr|nachschub|keep|"
                         r"continue|weiter|top up|aufstocken)\b", f) or _rx(
            r"\b(?:more|mehr|nachschub|keep|continue|weiter|top up|"
            r"aufstocken)\b[^.]*(?:radio)", f)
        if more_radio and not _rx(r"\b(?:start|turn on|einschalten)\b", f):
            return Plan("radio", {"action": "more"}, confidence=0.85)
        # "radio based on Rock" has no verb at all: the seed is the sentence
        seed_phrase = _rx(
            r"(?:^|\b)radio\s+(?:based on|from|ab|ausgehend von|"
            r"basierend auf)\s+(.{2,80})$", original)
        if seed_phrase is not None:
            seed = seed_phrase.group(1).strip(" .'\"")
            seed = re.sub(r"(?i)\b(?:this|that|the current|the playing)\s+"
                          r"(?:song|track|one|music)\b", "this song", seed)
            return Plan("radio", {"action": "start", "target": seed},
                        confidence=0.85)
        start_match = _rx(
            r"(?:^|\b)(?:start|begin|play|turn on|switch on|give me)\s+"
            r"(?:the\s+|a\s+|some\s+)?radio\b"
            r"|(?:^|\b)radio\s+(?:on|an|starten|einschalten|bitte|mode|"
            r"modus|station)\b"
            r"|\b(?:mach|schalte|starte|spiele|spiel)\s+"
            r"(?:das\s+|den\s+|ein\s+)?radio\b", f)
        if start_match is not None:
            # whatever follows "radio" decides: "from <song>" is a seed,
            # "ga ga" is the title of the song that was asked for
            tail = f[start_match.end():].strip(" .,!?")
            seed_words = ("from ", "based on ", "like ", "ab ", "ausgehend ",
                          "basierend ")
            if not tail or tail in {"on", "an", "bitte", "starten",
                                    "einschalten", "jetzt", "please", "now",
                                    "mode", "modus", "station"} \
                    or tail.startswith(seed_words):
                seed = ""
                from_match = _rx(
                    r"(?:from|based on|like|ab|ausgehend von|basierend auf)\s+"
                    r"(.{2,80})$", original)
                if from_match:
                    seed = from_match.group(1).strip(" .'\"")
                    seed = re.sub(
                        r"(?i)\b(?:this|that|the current|the playing)\s+"
                        r"(?:song|track|one|music)\b", "this song", seed)
                return Plan("radio", {"action": "start", "target": seed},
                            confidence=0.85)

    # "play duplicate 2" is the reviewer, not a search for that title
    if _has(f, "duplicate", "duplicates", "duplikat", "duplikate") \
            and _has(f, "play", "spiele", "abspielen", "hear", "hore",
                     "anhören", "anhoeren"):
        group_number = _rx(r"(?:duplicate|duplikat|doppelte[snr]?)\s*"
                           r"(?:number|nummer|no\.?)?\s*(\d{1,3})", f)
        if group_number:
            return Plan("duplicates", {"action": "play",
                                       "target": group_number.group(1)},
                        confidence=0.85)
        ordinal = _ordinal(f)
        if ordinal:
            return Plan("duplicates", {"action": "play",
                                       "target": str(ordinal)},
                        confidence=0.8)

    # -- playback status and transport ------------------------------------

    if _has(f, "status", "what's playing", "whats playing", "what is playing",
            "was läuft", "was laeuft", "welcher song", "which song",
            "now playing") \
            and not _has(f, "profile", "profiles", "psycho", "measurement",
                         "measurements", "messung") \
            and not _has(f, "lyrics", "lyric", "songtext", "textzeile",
                         "mention", "mentions", "erwähnt", "about", "über",
                         "ueber", "whose", "with"):
        return Plan("transport", {"action": "status"}, confidence=0.95)

    # -- player modes and position ----------------------------------------

    if _has(f, "restart", "start over", "from the beginning", "from the start",
            "von vorne", "von anfang", "nochmal von anfang", "neu starten",
            "start again", "play it again from the start"):
        return Plan("transport", {"action": "seek_abs", "value": 0},
                    confidence=0.9)

    clock_seek = _rx(r"(?:seek|jump|go|springe?|spule?)\s+(?:to|auf|zu)\s+"
                     r"(\d{1,2}):([0-5]\d)", f)
    if clock_seek:
        return Plan("transport", {
            "action": "seek_abs",
            "value": int(clock_seek.group(1)) * 60 + int(clock_seek.group(2)),
        }, confidence=0.9)

    back = (_rx(r"(?:back|rewind|backward|zurück|zurueck|zuruck)\s+(?:by\s+)?"
                r"(\d{1,3})\s*(s|sec|secs|seconds?|sekunden?|m|min|mins|"
                r"minutes?|minuten?)(?![\w])", f)
            or _rx(r"(\d{1,3})\s*(s|sec|secs|seconds?|sekunden?|m|min|mins|"
                   r"minutes?|minuten?)\s+(?:back|rewind|zurück|zurueck|"
                   r"zuruck)(?![\w])", f))
    if back:
        return Plan("transport", {"action": "seek",
                                  "value": -_span_seconds(back)},
                    confidence=0.9)

    ahead = (_rx(r"(?:forward|ahead|vorwärts|vorwarts|vorwaerts|vorspulen|"
                 r"skip ahead)\s+(?:by\s+)?(\d{1,3})\s*"
                 r"(s|sec|secs|seconds?|sekunden?|m|min|mins|minutes?|minuten?)"
                 r"(?![\w])", f)
             or _rx(r"(\d{1,3})\s*(s|sec|secs|seconds?|sekunden?|m|min|mins|"
                    r"minutes?|minuten?)\s+(?:forward|ahead|vorwärts|vorwarts|"
                    r"vorwaerts)(?![\w])", f))
    if ahead:
        return Plan("transport", {"action": "seek",
                                  "value": _span_seconds(ahead)},
                    confidence=0.9)

    if _has(f, "mute", "stumm", "stummschalten", "ton aus", "sound off",
            "silence") and len(f.split()) <= 4:
        return Plan("transport", {"action": "mute"}, confidence=0.9)
    if _has(f, "unmute", "ton an", "sound on", "wieder ton", "laut wieder") \
            and len(f.split()) <= 4:
        return Plan("transport", {"action": "unmute"}, confidence=0.9)

    # playback speed ("1.5x", "half speed", "play faster") — never the tempo
    # of the music being searched for, so a music context is excluded
    speed_x = _rx(r"(?:at|auf|speed|geschwindigkeit|tempo)\s*"
                  r"(\d(?:[.,]\d+)?)\s*(?:x|times|fach)(?![\w])", f)
    if speed_x:
        try:
            value = float(speed_x.group(1).replace(",", "."))
            return Plan("transport", {"action": "speed", "value": value},
                        confidence=0.9)
        except ValueError:
            pass
    if _has(f, "half speed", "halbe geschwindigkeit", "half tempo",
            "halbes tempo"):
        return Plan("transport", {"action": "speed", "value": 0.5},
                    confidence=0.9)
    if _has(f, "double speed", "twice as fast", "doppelt so schnell",
            "2x speed"):
        return Plan("transport", {"action": "speed", "value": 2.0},
                    confidence=0.9)
    if _has(f, "normal speed", "normale geschwindigkeit", "original speed",
            "reset the speed", "play at normal speed"):
        return Plan("transport", {"action": "speed", "value": 1.0},
                    confidence=0.9)
    playing = _has(f, "play", "spiele", "spiel", "abspielen", "playback",
                   "speed", "geschwindigkeit", "tempo") \
        and not _has(f, "music", "musik", "song", "songs", "track", "tracks",
                     "similar", "ähnliche", "aehnliche", "find", "show",
                     "recommend", "playlist", "liste")
    if playing and _has(f, "faster", "schneller", "speed up", "beschleunige"):
        return Plan("transport", {"action": "speed_delta", "value": 0.1},
                    confidence=0.8)
    if playing and _has(f, "slower", "langsamer", "slow down", "verlangsame"):
        return Plan("transport", {"action": "speed_delta", "value": -0.1},
                    confidence=0.8)

    if _rx(r"\bshuffle\s+(?:mode|modus|playback)\b", f) \
            or _rx(r"\b(?:shuffle|zufall)\b[^.]*\b(?:always|jedes mal|"
                   r"standard|default)\b", f) \
            or _rx(r"\b(?:always|immer)\b[^.]*\b(?:shuffle|zufall)\b", f):
        if _has(f, "off", "disable", "aus", "ausschalten", "stop", "no",
                "kein", "keine"):
            return Plan("transport", {"action": "shuffle_off"},
                        confidence=0.9)
        return Plan("transport", {"action": "shuffle_on"}, confidence=0.9)
    if re.search(r"(?:shuffle|zufall|zufallig|zufaellig)\b.*\b"
                 r"(?:on|enable|einschalten|ein)\b", f) \
            or re.search(r"\b(?:turn on|enable|aktiviere)\b.*\b"
                         r"(?:shuffle|zufall)\b", f):
        return Plan("transport", {"action": "shuffle_on"}, confidence=0.85)
    if re.search(r"(?:shuffle|zufall|zufallig|zufaellig)\b.*\b"
                 r"(?:off|disable|aus|ausschalten|stop|no)\b", f) \
            or re.search(r"\b(?:turn off|disable|deaktiviere)\b.*\b"
                         r"(?:shuffle|zufall)\b", f) \
            or _has(f, "no shuffle", "stop shuffling", "ohne zufall"):
        return Plan("transport", {"action": "shuffle_off"}, confidence=0.85)

    if (_has(f, "repeat", "loop", "wiederholen", "wiederhole", "loop it")
            and len(f.split()) <= 7):
        if _has(f, "off", "aus", "stop", "no repeat", "kein", "beenden"):
            return Plan("transport", {"action": "repeat_off"}, confidence=0.85)
        if _has(f, "one", "this song", "this track", "das lied", "diesen song",
                "single", "one track", "diesen titel"):
            return Plan("transport", {"action": "repeat_one"}, confidence=0.85)
        return Plan("transport", {"action": "repeat_all"}, confidence=0.8)

    if re.search(r"(?:what'?s|what is|show|zeig|was ist|was lauft|läuft)\s+"
                 r"(?:in\s+)?(?:the\s+|der\s+|die\s+)?"
                 r"(?:queue|warteschlange)\b", f) \
            or f in {"queue", "up next", "coming up", "queue status",
                     "was kommt als nachstes", "was kommt als naechstes"}:
        return Plan("transport", {"action": "queue"}, confidence=0.9)

    position = _rx(r"(?:play|jump|go|skip|springe|spiele?)\s+(?:to\s+)?"
                   r"(?:entry\s+|track\s+|number\s+|nummer\s+|titel\s+)?"
                   r"(\d{1,3})(?:st|nd|rd|th)?\s+(?:in|of|from|aus|der)\s+"
                   r"(?:the\s+)?(?:queue|warteschlange)(?![\w])", f)
    if position:
        return Plan("transport", {"action": "position",
                                  "value": int(position.group(1))},
                    confidence=0.85)

    # -- the library index job: pause, resume, cancel ----------------------
    #
    # Before the transport rules, which accept a short sentence ("pause the
    # indexing" is three words) and would otherwise stop the music instead of
    # the scan.  A question about the job is a status, not a command.
    if _has(f, *INDEX_CONTEXT) and not _index_asking(f):
        if _has(f, "pause", "paused", "pausiere", "pausieren", "pausiert",
                "anhalten", "anhalte", "unterbrechen", "unterbreche"):
            return Plan("index-library", {"action": "pause"}, confidence=0.9)
        if _has(f, "resume", "resumed", "unpause", "continue", "continuing",
                "fortsetzen", "fortsetze", "fort", "setz", "setze",
                "weitermachen", "weiterlaufen", "weiter", "carry on"):
            return Plan("index-library", {"action": "resume"}, confidence=0.9)
        if _has(f, "cancel", "cancelled", "canceled", "cancelling",
                "canceling", "abort", "abbruch", "abbrechen", "breche ab",
                "brich", "beenden", "beende", "stop", "stopp", "stoppe",
                "stoppen"):
            return Plan("index-library", {"action": "cancel"}, confidence=0.9)

    # -- continue the last session ----------------------------------------
    #
    # "continue where I left off" is not the transport ``resume`` (which
    # carries on the song that is still loaded) but "put the last queue back
    # and seek to where it stopped".  A bare "continue" stays transport, so
    # the rule needs a marker that points at the remembered session.
    continue_words = _has(f, "continue", "resume", "carry on", "fortsetzen",
                          "weiterspielen", "weiterhören", "weiterhoeren",
                          "weitermachen", "weiter machen", "pick up")
    session_words = _has(f, "where i left", "where i stopped", "where we left",
                         "where we stopped", "where did i stop",
                         "where did i leave", "what i was playing",
                         "what was playing", "last session", "last time",
                         "letzte sitzung", "zuletzt", "wo ich aufgehört",
                         "wo ich aufgehoert", "wo ich stehen", "queue",
                         "warteschlange", "listening", "wiedergabe",
                         "unfinished", "yesterday")
    if continue_words and session_words:
        return Plan("play-music", {"resume": True}, confidence=0.95)
    if _has(f, "where i left off", "where i stopped", "wo ich aufgehört habe",
            "wo ich aufgehoert habe", "continue listening", "keep listening",
            "weiterhören", "weiterhoeren", "weiterspielen", "last session",
            "letzte sitzung", "play what i was playing",
            "what i was playing", "resume last", "continue last"):
        return Plan("play-music", {"resume": True}, confidence=0.95)

    # -- the app's own library data: backup, export, import ---------------
    #
    # Before the short transport rules ("resume the backup" is three words)
    # and after the link/file rule (an export is not music to add).  A setting
    # verb still wins, so "turn off the daily backup" and "keep 5 backups"
    # stay settings; "when was the last backup?" asks about the job rather
    # than about the setting's default value.
    settings_plan = parse_settings(original)
    if settings_plan is not None and settings_plan.get("action") in {
            "set", "toggle", "reset"}:
        return Plan("settings", settings_plan, confidence=0.92)
    # "import it" right after a preview: the file is the one just named
    if (_has(f, "import", "importiere", "einlesen", "einlese", "restore",
             "wiederherstellen")
            and getattr(ctx, "last_library_file", "")
            and _has(f, "it", "that", "this", "the export", "the file",
                     "die datei", "den export", "das export", "davon")
            and not _has(f, "playlist", "playlists", "liste", "spotify",
                         "folder", "ordner", "youtube")):
        return Plan("library-data", {"action": "import",
                                     "file": ctx.last_library_file},
                    confidence=0.9)
    if _has(f, *LIBRARY_DATA_CONTEXT):
        job_question = (
            settings_plan is not None
            and settings_plan.get("action") == "show"
            and settings_plan.get("key") in {"library_backup",
                                             "library_backup_keep"}
            and _has(f, "status", "progress", "fortschritt", "how is",
                     "how old", "when", "wann", "last", "letzte", "fertig",
                     "finished", "running", "laeuft", "läuft"))
        if settings_plan is None or job_question:
            if _has(f, "pause", "pausiere", "pausieren", "anhalten",
                    "unterbrechen"):
                return Plan("library-data", {"action": "pause"},
                            confidence=0.9)
            if _has(f, "resume", "unpause", "continue", "fortsetzen",
                    "weiter"):
                return Plan("library-data", {"action": "resume"},
                            confidence=0.9)
            if _has(f, "import", "importiere", "einlesen", "einlese",
                    "restore", "wiederherstellen", "zurückspielen",
                    "zurueckspielen"):
                return Plan("library-data", {"action": "import",
                                             "file": _export_path(original)},
                            confidence=0.9)
            if _has(f, "check", "preview", "verify", "pruefe", "prüfe",
                    "test"):
                return Plan("library-data", {"action": "preview",
                                             "file": _export_path(original)},
                            confidence=0.85)
            if _has(f, "status", "progress", "fortschritt", "how is",
                    "how old", "when", "wann", "how many", "wie viele",
                    "wie alt", "ready"):
                return Plan("library-data", {"action": "status"},
                            confidence=0.9)
            if _has(f, "back up", "backup", "export", "exportiere", "sichere",
                    "sicherung", "write", "schreibe", "save", "speichere",
                    "fingerprint"):
                return Plan("library-data", {"action": "backup"},
                            confidence=0.9)
            return Plan("library-data", {"action": "status"}, confidence=0.85)

    if _has(f, "pause", "pausiere", "anhalten", "halt") \
            and len(f.split()) <= 5:
        return Plan("transport", {"action": "pause"}, confidence=0.95)
    if _has(f, "resume", "continue", "fortsetzen", "weiter", "weiter spielen") \
            and len(f.split()) <= 5:
        return Plan("transport", {"action": "resume"}, confidence=0.9)
    if _has(f, "next", "skip", "überspringen", "ueberspringen", "nächster",
            "naechster", "nächste", "naechste") and len(f.split()) <= 5:
        return Plan("transport", {"action": "next"}, confidence=0.9)
    if _has(f, "previous", "back", "zurück", "zurueck", "vorherige",
            "vorheriger") and len(f.split()) <= 5:
        return Plan("transport", {"action": "prev"}, confidence=0.9)
    if _has(f, "stop", "stopp", "beenden", "aufhören", "aufhoeren") \
            and len(f.split()) <= 5:
        return Plan("transport", {"action": "stop"}, confidence=0.95)

    volume = _rx(r"(?:volume|vol\.?|lautstärke|lautstaerke|volume to|"
                 r"lautstarke)\s*(?:to|auf)?\s*(\d{1,3})", f)
    if volume:
        return Plan("transport", {"action": "volume",
                                  "value": int(volume.group(1))},
                    confidence=0.95)
    volume_up = (_rx(r"(?:volume|lautstärke|lautstaerke|vol\.?)\s*\+"
                     r"(\d{1,2})", f)
                 or _rx(r"(?:volume|lautstärke|lautstaerke)\s+"
                        r"(?:up|rauf|hoch)\s*(?:by|um)?\s*(\d{1,2})", f)
                 or _rx(r"(?:increase|lauter um|erhöhe|erhoehe)\s+"
                        r"(?:the\s+)?(?:volume|lautstärke|lautstaerke)?\s*"
                        r"(?:by|um)?\s*(\d{1,2})", f))
    if volume_up:
        return Plan("transport", {"action": "volume_delta",
                                  "value": int(volume_up.group(1))},
                    confidence=0.85)
    volume_down = (_rx(r"(?:volume|lautstärke|lautstaerke|vol\.?)\s*-"
                       r"(\d{1,2})", f)
                   or _rx(r"(?:volume|lautstärke|lautstaerke)\s+"
                          r"(?:down|runter)\s*(?:by|um)?\s*(\d{1,2})", f)
                   or _rx(r"(?:decrease|leiser um|reduziere)\s+"
                          r"(?:the\s+)?(?:volume|lautstärke|lautstaerke)?\s*"
                          r"(?:by|um)?\s*(\d{1,2})", f))
    if volume_down:
        return Plan("transport", {"action": "volume_delta",
                                  "value": -int(volume_down.group(1))},
                    confidence=0.85)
    if _has(f, "louder", "lauter", "turn it up", "volume up",
            "turn the volume up", "dreh lauter", "mach lauter",
            "etwas lauter") and len(f.split()) <= 6:
        return Plan("transport", {"action": "volume_delta", "value": 10},
                    confidence=0.75)
    if _has(f, "quieter", "leiser", "turn it down", "volume down",
            "turn the volume down", "dreh leiser", "mach leiser",
            "etwas leiser") and len(f.split()) <= 6:
        return Plan("transport", {"action": "volume_delta", "value": -10},
                    confidence=0.75)

    seek = _rx(r"(?:seek|jump|springe?)\s*(?:to\s+|um\s+)?([+-]?\d+)\s*"
               r"(?:s|sec|secs|seconds?|sekunden?)?(?![\w])", f)
    if seek:
        try:
            return Plan("transport", {"action": "seek",
                                      "value": int(seek.group(1))},
                        confidence=0.9)
        except ValueError:
            pass

    # -- playlist import: a folder of lists, or a Spotify id ---------------

    if _has(f, "spotify") and re.search(r"(?<![\w])[A-Za-z0-9]{22}(?![\w])",
                                        original) \
            and _has(f, "import", "playlist", "liste", "importiere", "übernimm",
                     "uebernimm"):
        ident = re.search(r"(?<![\w])([A-Za-z0-9]{22})(?![\w])",
                          original).group(1)
        return Plan("import-playlist", {"spotify": ident}, confidence=0.9)

    wants_import = _has(f, "import", "importiere", "übernimm", "uebernimm",
                        "bring over", "kopiere", "einlesen")
    if wants_import and _has(f, "playlist", "playlists", "listen", "wiedergabe"
                             "listen", "m3u", "pls"):
        found = _rx(r"(?:from|aus|out of|von)\s+(?:the\s+folder\s+|ordner\s+|"
                    r"directory\s+|verzeichnis\s+)?([~/.][^\s,;]*|/[^\s,;]+)",
                    original)
        if found:
            return Plan("import-playlist", {
                "folder": found.group(1).strip(" .,;'\""),
                "recursive": not _has(f, "without subfolders", "nicht rekursiv",
                                      "no subfolders"),
            }, confidence=0.9)

    # -- saving the current selection --------------------------------------

    if not _has(f, "save only", "nur speichern") and re.search(
            r"\b(?:save|keep|store|speichere|sichere|behalte)\b", f) and (
            _has(f, "playlist", "liste", "mix", "selection", "auswahl",
                 "recommendation", "empfehlung", "that", "this", "it", "das",
                 "diese", "dieses", "as", "als")):
        name = None
        named = _rx(r"(?:save|keep|store|speichere|sichere|behalte)"
                    r"(?:\s+.*?)?\s+(?:as|als|under|unter)\s+"
                    r"[\"“”']?(.+?)[\"“”']?$", original)
        if named:
            name = named.group(1).strip(" .'\"")
        else:
            name = extract_explicit_name(original)
        args = {"name": name} if name else {}
        # "save it" / "keep this" points at the queue that just played, not at
        # an older recommendation the listener has moved on from
        if _has(f, "save it", "keep it", "save this", "keep this",
                "save that", "keep that", "speichere das", "behalte das",
                "speichere dies", "behalte dies"):
            args["own"] = True
        return Plan("save-playlist", args, confidence=0.9)

    # -- playlist listing / showing ---------------------------------------

    if re.search(r"(?:list|show|zeig|welche|which|what)\s+(?:me\s+)?"
                 r"(?:all\s+)?(?:my\s+)?(?:playlists?|listen)\b", f) \
            or f in {"playlists", "my playlists", "meine playlists",
                     "alle playlists", "/playlists"}:
        return Plan("show-playlists", {}, confidence=0.95)

    show = _rx(r"(?:show|zeig(?:e)?|what'?s in|was ist in|inhalte von)\s+"
               r"(?:the\s+|die\s+|der\s+)?(?:playlist\s+|liste\s+)?"
               r"[\"“”']?([\wÄÖÜäöüß .'&+-]{2,60}?)[\"“”']?$", original)
    if show:
        name = show.group(1).strip(" .'\"")
        playlist = catalog.find_playlist(name)
        if playlist is not None:
            return Plan("edit-playlist", {"action": "show",
                                          "playlist": playlist.name},
                        confidence=0.9)

    # -- app settings ------------------------------------------------------
    #
    # "turn off the visualizer", "use the gwdg server", "which settings are
    # there".  The parser is deliberately conservative: a sentence that only
    # sounds like a setting ("play the song Visualizer") stays playback.
    settings_plan = parse_settings(original)
    if settings_plan is not None:
        return Plan("settings", settings_plan, confidence=0.92)

    # -- lyrics: transcribe, look for a line, show one ---------------------
    #
    # Local transcription, so the words really exist; a sentence may pair a
    # lyric word with a sound property ("something with a boat and fast").
    if _has(f, "transcribe", "transkribiere", "transkription", "transcription",
            "untertitel", "subtitles", "speech to text", "lyrics for") \
            and not _has(f, "how is", "status", "progress", "fortschritt"):
        playing = _current_track(ctx, catalog)
        all_tracks = _has(f, "all", "alles", "missing", "fehlende", "library",
                          "bibliothek")
        return Plan("lyrics", {"action": "transcribe",
                               "track": "" if all_tracks
                               else _transcribe_target(original, ctx, catalog,
                                                       playing),
                               "all": all_tracks,
                               "refresh": _has(f, "again", "erneut", "neu")},
                    confidence=0.9)
    if _has(f, "lyrics", "lyric", "songtext", "songtexte", "liedtext",
            "transcription", "transkription") \
            and _has(f, "how is", "going", "progress", "status", "done",
                     "finished", "fortschritt", "fertig", "läuft", "laeuft",
                     "how far", "how many", "wie viele", "wieviele", "count"):
        return Plan("lyrics", {"action": "status"}, confidence=0.9)
    if _has(f, "set up the transcriber", "install lyrics", "setup asr",
            "install the transcriber", "setup_asr"):
        return Plan("lyrics", {"action": "setup"}, confidence=0.9)

    lyric_cue = _has(f, "lyrics", "lyric", "songtext", "songtexte",
                     "songtexts", "liedtext", "liedtexte", "gesangstext",
                     "textzeile", "textzeilen", "verse", "verses",
                     "sing along", "sing mit", "mitsingen", "mitlesen",
                     "read along", "being sung", "gesungen")
    something_with = re.search(r"(?<!\w)(?:something|anything|etwas|irgendwas)"
                               r"\s+with(?!\w)", f)
    # the folded text has no umlauts, so the German cues are written folded
    # ("über" arrives as "uber", "erwähnt" as "erwahnt")
    # "songs about the sea", "a track that mentions rain": a real lyric
    # question.  The weak relations (has/have/with) are kept apart, because
    # "how many songs do I have" counts the library and is not a search for
    # the word *have* in a lyric.
    song_about = re.search(
        r"(?<!\w)(?:song|songs|track|tracks|lied|lieder|tune)\b[^.]*?"
        r"\b(?:about|uber|ueber|where|wo|whose|mentions?|erwahnt|erwaehnt|"
        r"handelt)\b", f) or (
        not _has(f, "how many", "how much", "wie viele", "wieviele")
        and re.search(
            r"(?<!\w)(?:song|songs|track|tracks|lied|lieder|tune)\b[^.]*?"
            r"\b(?:has|have|contains|enthaelt|with)\b", f))
    # a sentence that asks for playback, a download or an *add* keeps its own
    # skill — but "make a playlist with lyrics about the sea" is a lyric search
    # first (the reply offers "save that as <name>")
    lyric_blocked = _has(f, "play", "spiele", "spiel", "abspielen", "hör",
                         "hoer", "listen", "add", "fuege", "füge", "download",
                         "youtube", "search on", "suche auf", "off", "hide",
                         "ausblenden", "schliessen", "schließen", "panel",
                         "fenster", "window", "settings", "einstellungen")
    lyric_search = (lyric_cue or bool(something_with) or bool(song_about))

    # "songs with similar lyrics", "more songs like this lyrically": compare
    # the words of one song's lyrics with every transcribed song.  The word
    # *similar* alone is not enough — "similar songs that are faster" is a
    # sound request for the profile ranking, which mentions no lyrics at all.
    similar_word = re.search(
        r"(?<!\w)(?:similar|alike|ahnlich\w*|aehnlich\w*|verwandt\w*|"
        r"lyrically|lyrisch\w*)(?!\w)", f)
    if similar_word and (lyric_cue or _has(f, "lyrically", "lyrisch",
                                          "lyrische", "lyrischen")) \
            and not _has(f, "download", "youtube", "settings", "einstellungen",
                         "hide", "panel", "fenster", "window"):
        target = ""
        named_like = re.search(
            r"(?:similar|alike|ahnlich\w*|aehnlich\w*|verwandt\w*|lyrically|"
            r"lyrisch\w*)[^.]*?(?:to|like|as|wie|von|vom|zu|zum)\s+"
            r"[\"“”']?([\wÄÖÜäöüß .'&+-]{2,60}?)[\"“”']?\s*[?.!]*$",
            original, re.IGNORECASE)
        if named_like:
            found = catalog.search(named_like.group(1).strip(" .'\""), limit=1)
            if found:
                target = found[0].rel
        return Plan("lyrics", {"action": "similar", "track": target,
                               "limit": 12}, confidence=0.85)

    if lyric_search and not lyric_blocked:
        # "show the lyrics of <a named song>" is a show, not a search for the
        # song's name as a word inside the lyrics
        named = re.search(
            r"(?:lyrics?|songtext\w*|liedtext\w*|gesangstext\w*)\s+"
            r"(?:of|from|for|von|vom|des|der|zu|zum)\s+"
            r"[\"“”']?([\wÄÖÜäöüß .'&+-]{2,60}?)[\"“”']?\s*[?.!]*$", original)
        if named:
            hits = catalog.search(named.group(1).strip(" .'\""), limit=1)
            if hits:
                return Plan("lyrics", {"action": "show", "track": hits[0].rel},
                            confidence=0.85)
        text, terms = lyric_text(original)
        if text:
            return Plan("lyrics", {"action": "search", "text": text,
                                   "terms": terms, "limit": 12},
                        confidence=0.85)
    if lyric_cue and not lyric_blocked:
        # "show the lyrics" / "what are the lyrics" — nothing to search for
        playing = _current_track(ctx, catalog)
        return Plan("lyrics", {"action": "show",
                               "track": playing.rel if playing else ""},
                    confidence=0.85)

    # -- library health ----------------------------------------------------
    #
    # A read-only audit, recognised before the index rules: "check the library"
    # asks how it is, not for a scan.
    if _has(f, "library health", "health of the library", "healthy",
            "is the library healthy", "check the library", "library check",
            "audit the library", "what is broken", "what's broken",
            "broken files", "damaged files", "bibliotheksgesundheit",
            "gesundheit der bibliothek", "ist die bibliothek gesund",
            "prüfe die bibliothek", "pruefe die bibliothek",
            "bibliothek prüfen", "bibliothek pruefen", "was ist kaputt",
            "kaputte dateien", "defekte dateien") \
            or re.search(r"\bhealth\b.*\b(?:library|bibliothek)\b", f):
        return Plan("library-overview", {"action": "health"}, confidence=0.9)

    # -- the library index -------------------------------------------------
    #
    # Re-indexing is the app's own pipeline (scan -> reports -> playlists),
    # not a sound-profile measurement, so it is recognised first: "scan the
    # library" is an index, "measure the library" is the profile.
    index_words = ("index", "indexes", "indexing", "reindex", "re-index",
                   "rescan", "re-scan", "inventory", "indizieren", "indiziere")
    index_ish = INDEX_CONTEXT
    # the control verbs were already handled above; what is left is a start,
    # a status, or a question about the job
    if _has(f, *index_ish) and _has(
            f, "going", "progress", "status", "done", "finished",
            "fortschritt", "fertig", "läuft", "laeuft", "how far", "how is",
            "wie weit", "wie steht", "paused", "cancelled", "canceled",
            "stopped"):
        return Plan("index-library", {"action": "status"}, confidence=0.9)
    rebuild_words = ("scan", "rescan", "refresh", "rebuild", "update",
                     "reindex", "re-index", "einlesen", "aktualisiere")
    profile_words = ("profile", "profiles", "psycho", "psychoacoustic",
                     "sound", "klang", "features", "measurement",
                     "measurements", "messwerte", "vermessen")
    if _has(f, *index_words) or (
            _has(f, *rebuild_words)
            and _has(f, "library", "bibliothek", "sammlung", "inventory",
                     "index")
            and not _has(f, *profile_words)):
        return Plan("index-library", {"action": "index"}, confidence=0.9)

    # -- psychoacoustic profile, families and the net diagram -------------

    # measuring is a job the app runs itself, so it must be recognised before
    # "analyse this song" (a show) and before the generic search rules
    if _has(f, "update", "refresh", "recompute", "recalculate", "remeasure",
            "re-measure", "measure", "scan", "aktualisiere", "neu berechnen",
            "vermessen", "analysiere", "analyse", "analyze", "einlesen") \
            and _has(f, "profile", "profiles", "psycho", "psychoacoustic",
                     "sound", "klang", "features", "measurements", "messwerte",
                     "library", "bibliothek", "sammlung"):
        return Plan("psycho-profile", {"action": "update"}, confidence=0.9)

    if _has(f, "measurement", "measurements", "profile", "profiles", "psycho",
            "messung") \
            and _has(f, "going", "progress", "status", "done", "finished",
                     "fortschritt", "fertig", "läuft", "laeuft", "how far"):
        return Plan("psycho-profile", {"action": "status"}, confidence=0.85)

    if _has(f, "families", "family", "familien") or (
            _has(f, "clusters", "gruppen", "groups")
            and _has(f, "psycho", "sound", "klang", "music", "musik")):
        return Plan("psycho-profile", {"action": "families"}, confidence=0.85)

    # the net diagram can show the same measurements through switchable sets;
    # a set is only taken when the sentence also asks for a profile, so "fast
    # rhythm" stays a mood request
    if _has(f, "sets", "lenses", "linsen", "sichten") and _has(
            f, "profile", "profiles", "psycho", "measurement", "measurements",
            "dimensions"):
        return Plan("psycho-profile", {"action": "sets"}, confidence=0.85)
    set_words = (
        ("psychology", ("psychology", "psychologie", "emotion set")),
        ("timbre", ("timbre", "klangfarbe")),
        ("rhythm", ("rhythm set", "rhythm lens", "rhythmus set", "rhythmus")),
        ("dynamics", ("dynamics set", "dynamic set", "dynamics lens",
                      "dynamik set", "dynamik")),
        ("instrument", ("instruments", "instrument set", "instrument lens",
                        "instrumente")),
    )
    named_set = next((key for key, words in set_words if _has(f, *words)), None)
    if named_set and _has(f, "profile", "profiles", "set", "lens", "lenses",
                          "properties", "eigenschaften", "show", "zeig",
                          "switch", "wechsle", "diagram", "radar"):
        return Plan("psycho-profile", {"action": "show", "set": named_set,
                                       "track": psycho_track(original)},
                    confidence=0.85)

    adjustments = psycho_adjustments(original)
    similar_cue = _has(f, "similar", "fits", "like this", "like it", "closest",
                       "more like", "match", "passt", "ähnlich", "aehnlich",
                       "same but", "sounds like", "would fit")
    reshape_cue = similar_cue or (adjustments and _has(
        f, "find", "show", "zeig", "music", "musik", "song", "songs", "track",
        "tracks", "something", "irgendwas", "recommend", "empfiehl"))
    # "are my 90s tracks louder than the newer ones?" compares two groups of
    # the library — it is a question for the agent, not a "find something like
    # this" request.  Without this guard the current track became the seed and
    # the answer was a list of unrelated tracks.
    comparing = bool(re.search(
        r"(?<!\w)(?:than|compared|comparing|average|avg|median|how many|"
        r"wie viele|wie laut|durchschnitt|verglichen)(?!\w)", f))
    if adjustments and reshape_cue and not comparing \
            and not _has(f, "playlist", "liste", "mix"):
        phrase = ""
        match = _rx(r"(?:similar to|like|from)\s+(.+?)\s+"
                    r"(?:but|with|that|which|aber|mit)\b", original)
        if match:
            phrase = match.group(1).strip(" .'\"")
        if fold(phrase) in {"this", "it", "this song", "the song", "das",
                            "diesen song", "that"}:
            phrase = ""
        return Plan("psycho-profile", {"action": "similar", "track": phrase,
                               "adjustments": adjustments}, confidence=0.85)

    show_cue = (
        _has(f, "psychoacoustic", "psychoacoustics", "psychoakustik",
             "net diagram", "netzdiagramm", "radar chart", "radar diagram",
             "sound profile", "klangprofil", "properties", "eigenschaften")
        or re.search(r"(?<![\w])(?:tempo|bpm)(?![\w])", f)
        or re.search(r"how (?:fast|loud|aggressive|bright|danceable|calm|"
                     r"rough|harmonic) is", f)
        or _has(f, "analyse this", "analyze this", "analyse the song",
                "analyze the song", "analysiere das", "analysiere diesen")
        or (re.search(r"(?<![\w])profile(?![\w])", f)
            and not _has(f, "learned", "learn", "gelernt", "preferences",
                         "vorlieben", "what do you know"))
    )
    if show_cue:
        return Plan("psycho-profile", {"action": "show", "track": psycho_track(original)},
                    confidence=0.85)

    # -- discarding the ad-hoc queue ---------------------------------------
    #
    # A queue built for one request is a real file in `Playlists/Own/`; "throw
    # it away" deletes that copy only.  The words are deliberately narrow, and
    # "remove <track> from <list>" keeps its own rule below, so one sentence
    # can never delete the wrong thing.

    if re.search(r"\b(?:discard|throw\s+(?:it|this|that)\s+(?:away|out)|"
                 r"wegwerfen|wegschmei[ßs]{1,2}en|verwirf\w*|verwerf\w*|"
                 r"entsorg\w*)\b", f) \
            or (re.search(r"\b(?:remove|delete|drop)\s+(?:it|this|that)\b", f)
                and not re.search(r"\b(?:from|aus|von)\b", f)):
        return Plan("edit-playlist", {"action": "discard"}, confidence=0.85)

    # -- smart playlists: saved rules that rebuild themselves --------------
    #
    # A smart list is a rule set, not a track list, so the sentence only has to
    # say what it should contain.  The block sits before the create-playlist
    # rule because "make a smart playlist of 90s metal" is not a mood mix.

    smart_named = _has(f, "smart playlist", "smart playlists", "smart mix",
                       "smart liste", "intelligente playlist",
                       "intelligente liste", "smarte playlist",
                       "dynamische playlist", "rule-based playlist")
    if smart_named:
        name_match = extract_explicit_name(original)
        if not name_match:
            # "delete the smart playlist 90s Metal": the name is simply the
            # tail after the phrase, because a smart list has no "called"
            tail = _rx(
                r"(?:smart|intelligente[nr]?|smarte[nr]?|dynamische[nr]?|"
                r"rule-based)\s+(?:playlist[s]?|mix|liste[n]?)\s+"
                r"(?:called\s+|named\s+)?[\"“”']?(.{2,60}?)[\"“”']?$",
                original)
            if tail:
                candidate = tail.group(1).strip(" .'\"")
                # "make a smart playlist of tracks longer than 8 minutes"
                # names a rule, not a list: the tail is a prepositional
                # phrase, so the name is left to suggest_name().
                first = fold(candidate).split()[0] if candidate else ""
                if first in {"of", "with", "that", "which", "from", "for",
                             "containing", "holding", "in", "mit", "von",
                             "aus", "die", "der", "das", "den"}:
                    candidate = ""
                if candidate and fold(candidate) not in {
                        "list", "liste", "playlist", "playlists"}:
                    name_match = candidate
        if _has(f, "delete", "drop", "loesche", "losche", "entferne",
                "schmeiss", "schmeiß", "verwirf") and name_match:
            return Plan("smart-playlist",
                        {"action": "delete", "name": name_match},
                        confidence=0.9)
        if _has(f, "refresh", "rebuild", "update", "aktualisier", "erneuere",
                "neu bauen", "neu erstellen") \
                and not _has(f, "play", "spiele"):
            return Plan("smart-playlist", {"action": "refresh"},
                        confidence=0.9)
        if _has(f, "show", "list", "which", "what are", "zeige", "welche",
                "liste") and not _has(f, "play", "spiele", "abspielen"):
            if name_match and not _has(f, "my smart", "meine smart"):
                return Plan("smart-playlist",
                            {"action": "show", "name": name_match},
                            confidence=0.85)
            return Plan("smart-playlist", {"action": "list"},
                        confidence=0.9)
        rules = smart_rules(original)
        if rules:
            return Plan("smart-playlist", {
                "action": "build", "rules": rules,
                "name": name_match or "",
            }, confidence=0.9)
        if not _has(f, "play", "spiele", "abspielen"):
            return Plan("smart-playlist", {"action": "list"},
                        confidence=0.6)

    # -- delete or rename a whole playlist --------------------------------
    #
    # The engine refuses a generated shelf/mood/focus list and keeps the
    # entries in the journal, so the name alone is enough to plan the call.
    # "remove" stays out on purpose: "remove 2 from Mix" edits entries.

    delete_list = _rx(
        r"(?:delete|drop|loesch\w*|lösch\w*|entfern\w*|schmeiss\w*|schmeiß\w*)\s+"
        r"(?:the\s+|my\s+|meine[rn]?\s+|die\s+|den\s+|das\s+)?"
        r"(?:playlist|liste|list|mix)\s+"
        r"(?:called\s+|named\s+)?[\"“”']?(.{2,60}?)[\"“”']?$", original)
    if delete_list:
        return Plan("edit-playlist", {
            "action": "delete", "playlist": delete_list.group(1).strip(" .'\""),
        }, confidence=0.9)

    rename_list = _rx(
        r"(?:rename|benenne)\s+(?:the\s+|my\s+|meine[rn]?\s+)?"
        r"(?:playlist|liste|list|mix)\s+[\"“”']?(.{2,40}?)[\"“”']?\s+"
        r"(?:to|in|zu|als)\s+[\"“”']?(.{2,40})[\"“”']?$", original)
    if rename_list:
        return Plan("edit-playlist", {
            "action": "rename",
            "playlist": rename_list.group(1).strip(" .'\""),
            "to": rename_list.group(2).strip(" .'\""),
        }, confidence=0.9)

    # -- add to / remove from a playlist ----------------------------------

    add_to = _rx(r"(?:add|put|füge|fuege|packe|nimm)\s+(.+?)\s+"
                 r"(?:to|in|zu|zu der|zur|auf)\s+(?:the\s+|my\s+|meine\s+|"
                 r"meiner\s+)?(?:playlist\s+|liste\s+|mix\s+)?"
                 r"[\"“”']?([\wÄÖÜäöüß .'&+-]{2,60}?)[\"“”']?$", original)
    if add_to and not _has(f, "playlist erstellen", "create"):
        what = _track_phrase(add_to.group(1))
        name = add_to.group(2).strip(" .'\"")
        name = re.sub(r"(?i)^(?:my|meine|meiner|unser)\s+", "", name).strip()
        # "to my Test playlist" names the list "Test", not "Test playlist"
        if re.search(r"(?:to|in|zu|zur|auf)\s+(?:the|my|meine|meiner)\b", f):
            trimmed = re.sub(r"(?i)\s+(?:playlist|liste|mix)$", "", name).strip()
            if trimmed:
                name = trimmed
        # "to <name>" only means a playlist when the word is said, or when
        # that list already exists — otherwise the sentence is a mood
        explicit = re.search(r"(?:to|in|zu|zur|auf)\s+(?:the\s+|my\s+|meine\s+|"
                             r"meiner\s+)?(?:playlist|liste|mix)\b", f) \
            or re.search(r"(?:playlist|liste|mix|sammlung)\s*$", f)
        if name and (explicit or catalog.find_playlist(name) is not None):
            rels, missing = _resolve_tracks(what, ctx, catalog)
            # "my playlist" points at the list that was last shown or made
            if fold(name) in {"playlist", "liste", "mix"}:
                if ctx.last_playlist \
                        and catalog.find_playlist(ctx.last_playlist) is not None:
                    name = ctx.last_playlist
                else:
                    name = "My_Playlist"
            # an unknown name is fine: the engine creates that custom list
            args = {"action": "add", "playlist": name, "tracks": rels}
            if missing:
                args["unresolved"] = missing
            return Plan("edit-playlist", args, confidence=0.9)
        # "add some jazz to my evening": not a list name, but a mood/genre ask
        moods = moods_from_text(original, profile)
        include, only, exclude = extract_genres(original)
        if moods or include or only:
            args = {
                "name": extract_explicit_name(original)
                or _name_for(moods, original),
                "moods": moods,
                "autoplay": True,
                "include_genres": include,
                "only_genres": only,
                "exclude_genres": exclude,
                "adhoc": not wants_kept_playlist(f, original),
            }
            return Plan("make-playlist", args, confidence=0.7)

    remove_from = _rx(r"(?:remove|delete|lösche|loesche|nimm)\s+(.+?)\s+"
                      r"(?:from|aus|von)\s+(?:the\s+)?(?:playlist\s+)?"
                      r"[\"“”']?([\wÄÖÜäöüß .'&+-]{2,60}?)[\"“”']?$",
                      original)
    if remove_from:
        name = remove_from.group(2).strip(" .'\"")
        number = _rx(r"(\d{1,3})", remove_from.group(1))
        if number:
            return Plan("edit-playlist", {
                "action": "remove", "playlist": name,
                "indexes": [int(number.group(1))],
            }, confidence=0.9)
        # "remove Bohemian Rhapsody from Bands" names the track, not a number
        what = _track_phrase(remove_from.group(1))
        what = re.sub(r"(?i)^(?:the\s+)?(?:song|track|titel|lied)\s+", "",
                      what).strip(" .'\"")
        if what:
            return Plan("edit-playlist", {
                "action": "remove", "playlist": name, "match": what,
            }, confidence=0.85)

    # -- recommendations ---------------------------------------------------
    #
    # "what do you recommend for aggressive music?" is not a playlist request:
    # the answer is a suggestion the listener can accept, play or save.

    wants_recommendation = (
        re.search(r"(?:what|which|anything|something)\s+(?:would\s+you\s+|"
                  r"do\s+you\s+|can\s+you\s+)?(?:recommend|suggest)", f)
        or _has(f, "recommend", "recommendation", "recommendations", "suggest",
                "suggestion", "suggestions", "empfehl", "empfiehl",
                "empfehlung", "empfehlungen", "vorschlag", "vorschläge",
                "vorschlaege", "what should i listen", "what should i play",
                "what should i hear", "surprise me", "überrasch mich",
                "ueberrasch mich", "was soll ich hören", "was soll ich hoeren",
                "was soll ich spielen"))
    if wants_recommendation and not _has(
            f, "don't play", "dont play", "save only", "nur speichern"):
        moods = moods_from_text(original, profile)
        include, only, exclude = extract_genres(original)
        seed = extract_seed(original)
        args: dict = {
            "moods": moods,
            "include_genres": include,
            "only_genres": only,
            "exclude_genres": exclude,
            "count": extract_count(original) or 12,
            "autoplay": _has(f, "and play", "then play", "und spiele",
                             "play it", "spiele es"),
        }
        if seed:
            args["seed"] = seed
        return Plan("recommend", args, confidence=0.9)

    # -- create a playlist ------------------------------------------------

    create_verb = _has(f, "create", "make", "build", "generate", "erstelle",
                       "erstellen", "mach", "mache", "baue", "bau", "generiere",
                       "zusammenstellen", "compilen")
    playlist_word = _has(f, "playlist", "liste", "mixtape", "mix")
    has_mood = any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", f)
                   for word in MOOD_WORDS) \
        or any(phrase in f for phrase, _ in MOOD_PHRASES)
    activity = _has(f, "for work", "für die arbeit", "fuer die arbeit",
                    "zum arbeiten", "beim arbeiten", "zum einschlafen",
                    "beim sport", "to work", "for studying", "zum lernen")

    # "a playlist with 30 tracks of rock" names neither a mood nor a verb, but
    # a count plus a genre is a complete request.
    counted_genre = bool(extract_count(original)
                         and any(extract_genres(original)))
    if (create_verb and (playlist_word or has_mood or activity
                         or counted_genre)) \
            or (playlist_word and (has_mood or activity or counted_genre)) \
            or (has_mood and (activity or create_verb)) \
            or (has_mood and _has(f, "something", "anything", "irgendwas",
                                  "irgendetwas", "some", "etwas",
                                  "ein bisschen", "a bit of")):
        moods = moods_from_text(original, profile)
        if not moods:
            moods = {"Chill": 1.0}
        include, only, exclude = extract_genres(original)
        name = extract_explicit_name(original) or _name_for(moods, original)
        count = extract_count(original)
        duration = extract_duration_min(original)
        seed = extract_seed(original)
        autoplay = not _has(f, "don't play", "dont play", "save only",
                            "nur speichern", "without playing",
                            "ohne abspielen")
        if not count and not duration and ctx.last_playlist \
                and fold(ctx.last_playlist) in {fold(name), fold("")}:
            pass
        args: dict = {
            "name": name,
            "moods": moods,
            "autoplay": autoplay,
            "include_genres": include,
            "only_genres": only,
            "exclude_genres": exclude,
            # a queue unless the sentence names a list to keep
            "adhoc": not wants_kept_playlist(f, original),
        }
        if count:
            args["count"] = count
        if duration:
            args["duration_min"] = duration
        if seed:
            args["seed"] = seed
        if _has(f, "make it longer", "make it shorter", "verlängere",
                "verlaengere", "kürzer", "kuerzer", "update it", "change it") \
                and ctx.last_playlist:
            args["name"] = ctx.last_playlist
            args["overwrite"] = True
            args["autoplay"] = True
            # refining an existing list keeps it, it is not a throwaway queue
            args["adhoc"] = False
        return Plan("make-playlist", args,
                    confidence=0.9 if (playlist_word or create_verb) else 0.7)

    # "one hour of metal", "zwei Stunden Entspannung", "30 minutes chill":
    # a length plus a mood or genre is a playlist request on its own
    if not create_verb and not _has(f, "play", "spiele", "abspielen", "hör",
                                    "hoer", "start", "put on"):
        duration = extract_duration_min(original)
        if duration:
            moods = moods_from_text(original, profile)
            include, only, exclude = extract_genres(original)
            if moods or include or only or exclude:
                # with no mood word, "of metal" means metal, not metal-ish
                if not moods and include:
                    only = include
                    include = []
                name = extract_explicit_name(original) \
                    or _name_for(moods, original)
                if name == "Mix" and only:
                    name = only[0]
                args = {
                    "name": name,
                    "moods": moods,
                    "duration_min": duration,
                    "include_genres": include,
                    "only_genres": only,
                    "exclude_genres": exclude,
                    "autoplay": not _has(f, "don't play", "dont play",
                                         "save only", "nur speichern",
                                         "without playing", "ohne abspielen"),
                    "adhoc": not wants_kept_playlist(f, original),
                }
                return Plan("make-playlist", args, confidence=0.6)

    # -- why one track scores the way it does ------------------------------
    #
    # "why is this the most aggressive?" names a property *and* asks about one
    # track, so it must not become the ranking itself — that would list the top
    # twelve and never answer the question.  It sits before the ranking rule
    # for exactly that reason.  The target is the playing track unless the
    # sentence names another one.

    if _has(f, "why", "warum", "weshalb", "wieso", "what makes", "was macht",
            "how come"):
        why_terms = rank_terms(original)
        if why_terms:
            target = ""
            if _has(f, "this", "this song", "this track", "this one",
                    "this piece", "the current", "current track", "now playing",
                    "das hier", "dieser song", "diesen song", "dieses lied",
                    "diese nummer", "gerade lauft", "gerade laeuft"):
                target = "current"
            if not target:
                number = _rx(r"(?:number|nummer|nr\.?)\s*(\d{1,2})", f)
                if number:
                    target = number.group(1)
            if not target:
                named = _rx(
                    r"(?:why is|why are|why does|warum ist|warum sind|"
                    r"warum klingt|weshalb ist|how come)\s+(.+?)\s+"
                    r"(?:so|the|most|least|more|less|ranked|scored)\b", f)
                if named:
                    phrase = named.group(1).strip(" .,'\"")
                    # a pronoun or a generic "the slowest song" is not a name:
                    # the playing track is what the question is about
                    generic = {"the", "a", "an", "this", "that", "it", "das",
                               "es", "er", "sie", "diese", "dieser", "dieses",
                               "they", "song", "songs", "track", "tune",
                               "piece", "lied", "nummer", "stuck", "one"}
                    words = [word for word in phrase.split() if word]
                    if words and all(word in generic or word in CONCEPT_WORDS
                                     for word in words):
                        target = "current"
                    else:
                        target = phrase
            args: dict = {"action": "why",
                          "terms": [[axis, sign] for axis, sign in why_terms]}
            if target:
                args["target"] = target
            return Plan("rank-library", args, confidence=0.86)

    # -- ranking by sound property ---------------------------------------
    #
    # "find the slowest and boring song" is not a text search: no tag contains
    # those words.  The profile database can rank by them, so the request must
    # not fall through to skill_search, which would answer "no tracks match".
    # A superlative is required, because one property word alone is often a
    # genre or a title ("find the metal albums", "play Slow").

    terms = rank_terms(original)
    if terms and _states_a_property(f):
        limit = 12
        requested = _rx(r"\b(?:top|first|best|the)\s+(\d{1,2})\b", f)
        if requested:
            limit = max(1, min(50, int(requested.group(1))))
        # "the slowest metal song" / "das langsamste Lied von Die Arzte"
        about = _rx(r"(?:von|by|from|among|aus)\s+([\wÄÖÜäöüß .'&-]{2,40})",
                    original)
        args: dict = {"terms": [[axis, sign] for axis, sign in terms],
                      "limit": limit}
        if about:
            args["filter"] = about.group(1).strip(" .,'\"")
        if _has(f, "play", "spiele", "spiel", "abspielen", "hör", "hoer"):
            args["play"] = True
        return Plan("rank-library", args, confidence=0.86)

    # -- what arrived lately ----------------------------------------------
    #
    # The inventory has no download date, so the honest answer is "the newest
    # files by their own time".  Only an explicit phrase reaches this rule, so
    # "what did I add this month" stays the statistics slice and a lookup for
    # a band called *New Order* stays a lookup.  A playback verb turns it into
    # a queue of the newest tracks instead of a list.

    recent_added = _has(
        f, "what's new", "whats new", "what is new", "anything new",
        "new arrivals", "newly added", "recently added", "latest additions",
        "latest arrivals", "what arrived", "just arrived", "just added",
        "newest tracks", "newest songs", "newest files", "newest music",
        "newest additions", "new additions", "new music", "something new",
        "new stuff",
        "was ist neu", "was gibt es neues", "was ist dazugekommen",
        "neu dazugekommen", "neu hinzugekommen", "zuletzt hinzugefugt",
        "zuletzt hinzugefuegt", "neueste tracks", "neueste songs",
        "neue musik", "neues zeug")
    if not recent_added:
        # "which songs did I add recently" — the verb carries the time word
        recent_added = bool(_rx(
            r"\b(?:add|added|arrive[ds]?|hinzugefugt|hinzugefuegt|"
            r"dazugekommen)\b[^.?!]*\b(?:recently|lately|zuletzt)\b", f))
    if not recent_added:
        # "the newest 5 songs" — a count may sit between the words; an *album*
        # is not this rule, because "play the latest album by X" must stay a
        # playback request
        recent_added = bool(_rx(
            r"\b(?:newest|latest|neueste[ns]?)\b[^.?!]*"
            r"\b(?:tracks?|songs?|titles?|titel|lieder|files?|music)\b", f))
    if recent_added and not _has(f, "youtube", "download", "import",
                                 "herunterlad", "spotify"):
        days = None
        if _has(f, "today", "heute"):
            days = 1
        elif _has(f, "this week", "last week", "diese woche", "letzte woche"):
            days = 7
        elif _has(f, "this month", "last month", "diesen monat",
                  "letzten monat"):
            days = 31
        elif _has(f, "this year", "last year", "dieses jahr", "letztes jahr"):
            days = 366
        else:
            span = _rx(r"\b(?:last|past|previous|letzten|letzte|vergangenen)\s+"
                       r"(\d{1,3})\s+(day|days|tag|tage|week|weeks|woche|"
                       r"wochen|month|months|monat|monate)\b", f)
            if span:
                count = int(span.group(1))
                unit = span.group(2)
                days = count * (7 if unit.startswith(("week", "woche"))
                                else 31 if unit.startswith(("month", "monat"))
                                else 1)
        playing = _has(f, "play", "spiele", "spiel", "abspielen", "shuffle",
                       "queue", "hoer", "hore", "hör")
        args: dict = {"action": "play" if playing else "list"}
        if days:
            args["days"] = days
        return Plan("recent", args, confidence=0.85)

    # -- play -------------------------------------------------------------

    whole = (_has(f, "all", "everything", "alles", "ganze", "ganzen",
                  "complete", "entire", "library", "bibliothek", "sammlung")
             and _has(f, "play", "spiele", "abspielen", "shuffle", "shuffled",
                      "zufällig", "zufaellig", "random", "mix", "hör", "hoer"))
    if whole:
        return Plan("play-music", {
            "target": "All",
            "whole_library": True,
            "shuffle": _has(f, "shuffle", "shuffled", "random", "zufällig",
                            "zufaellig", "mix"),
        }, confidence=0.95)

    if _has(f, "play", "spiele", "spiel", "abspielen", "hör", "hoer", "listen",
            "put on", "queue", "start") or \
            (_has(f, "shuffle", "zufällig", "zufaellig") and len(f.split()) <= 4):
        shuffle = _has(f, "shuffle", "shuffled", "random", "zufällig",
                       "zufaellig")
        append = _has(f, "queue", "append", "danach", "anschließend",
                      "anschliessend", "after that")
        target = original
        target = re.sub(r"(?i)\b(?:please|bitte|for me|für mich|fuer mich|"
                        r"now|jetzt|mal|doch)\b", " ", target)
        target = re.sub(r"(?i)\b(?:play|spiele|spiel|abspielen|höre|hoer|hör|"
                        r"listen to|put on|queue|start|shuffle|shuffled|"
                        r"random|zufällig|zufaellig)\b", " ", target)
        target = re.sub(r"(?i)\b(?:the|a|an|some|my|me|song|songs|track|tracks|"
                        r"music|musik|etwas)\b", " ", target)
        target = re.sub(r"(?i)^(?:by|von|from)\s+", "", target)
        target = re.sub(r"(?i)\s+(?:playlist|liste|list)$", "", target)
        target = _strip_duration(target)
        target = " ".join(target.split()).strip(" .,!?-")
        moods = moods_from_text(original, profile)
        # "play something smooth" names a mood, not a track
        bare = target
        for word in sorted(MOOD_WORDS, key=len, reverse=True):
            bare = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", " ", fold(bare))
        bare = re.sub(r"(?i)\b(?:something|anything|etwas|was)\b", " ", bare)
        bare = " ".join(bare.split()).strip(" .,!?-")
        if not bare and moods:
            target = ""
        if not target or fold(target) in {"music", "musik", "something"}:
            if moods:
                planned = {"target": "", "moods": moods, "shuffle": shuffle}
                length = extract_duration_min(original)
                if length:
                    planned["duration_min"] = length
                return Plan("play-music", planned, confidence=0.65)
            return Plan("play-music", {"target": "All"}, confidence=0.6)
        # "play number 2" / "play the second one"
        number = _rx(r"(?:number|nummer|no\.?)\s*(\d{1,2})", f) \
            or _rx(r"^(\d{1,2})(?:st|nd|rd|th)?$", target)
        if number and ctx.last_tracks:
            index = int(number.group(1)) - 1
            if 0 <= index < len(ctx.last_tracks):
                return Plan("play-music", {
                    "target": ctx.last_tracks[index].rel,
                }, confidence=0.95)
        ordinal = _ordinal(f) or _ordinal(target)
        if ordinal and ctx.last_tracks:
            index = ordinal - 1
            if 0 <= index < len(ctx.last_tracks):
                return Plan("play-music", {
                    "target": ctx.last_tracks[index].rel,
                }, confidence=0.95)
        if fold(target) in {"this", "that", "it", "das", "dieses", "diese"}:
            track = _current_track(ctx, catalog)
            if track:
                return Plan("play-music", {"target": track.rel},
                            confidence=0.9)
        return Plan("play-music", {
            "target": target, "shuffle": shuffle, "append": append,
        }, confidence=0.85)

    # -- duplicate groups: a read-only review -----------------------------
    #
    # The reports that remain are `action=review` (the same song on two
    # albums), so this is a listing, never a delete.  "play duplicate 2"
    # plays the first copy of group 2; "play number 2" after a `show` uses
    # the ordinary last-result rule.

    duplicate_word = _has(
        f, "duplicate", "duplicates", "duplicated", "duplikat",
        "duplikate", "dupliziert", "doppelte songs", "doppelte titel",
        "doppelte lieder",
        "doppelt vorhanden", "doppelt in der bibliothek",
        "same song twice", "twice in the library",
        "zwei mal in der bibliothek")
    if duplicate_word and not _has(f, "youtube", "download", "add to",
                                   "hinzufugen", "hinzufuegen"):
        group_number = _rx(r"(?:duplicate|duplikat|doppelte[snr]?|gruppe)\s*"
                           r"(?:number|nummer|no\.?)?\s*(\d{1,3})", f)
        playing = _has(f, "play", "spiele", "abspielen", "hear", "hore",
                       "anhören", "anhoeren")
        if playing and group_number:
            return Plan("duplicates", {"action": "play",
                                       "target": group_number.group(1)},
                        confidence=0.85)
        if _has(f, "how many", "count", "wie viele", "wieviele", "status"):
            return Plan("duplicates", {"action": "status"}, confidence=0.85)
        if group_number:
            return Plan("duplicates", {"action": "show",
                                       "target": group_number.group(1)},
                        confidence=0.9)
        return Plan("duplicates", {"action": "list"}, confidence=0.85)

    # -- library statistics: numbers about the collection -----------------
    #
    # "how much music do I have" is the overview; "which genres do I have most
    # of" is one slice of it.  The slice is only planned when the sentence
    # really asks for numbers, so a search for a genre stays a search.

    focus = stats_focus(original)
    stats_verb = _has(f, "statistics", "stats", "statistik", "statistiken",
                      "library statistics", "library stats")
    about_this = _has(f, "is this", "ist das", "playing", "current track",
                      "currently", "just played", "gerade lauft",
                      "gerade laeuft")
    # "what formats do I have" needs no number word — the topic itself is the
    # request — while "do I have the album X" must stay a search
    # `playlists` is not a plain topic: "a playlist with 30 tracks of rock" is a
    # create request, and "how many playlists do I have" still matches through
    # the number word below.
    plain_topic = focus in {"formats", "quality", "coverage", "decades",
                            "length", "added"}
    play_verb = _has(f, "play", "spiele", "abspielen", "shuffle", "queue",
                     "hoer", "hore ich")
    if stats_verb or (focus and not about_this and not play_verb and (
            plain_topic or _has(
                f, "how many", "how much", "how big", "how large", "which",
                "what are", "top", "most", "largest", "biggest", "average",
                "wie viele", "wieviele", "wieviel", "welche", "meiste",
                "grosste", "grossten", "durchschnitt"))):
        return Plan("library-overview",
                    {"action": "stats", "focus": focus}, confidence=0.85)

    # -- overview / help --------------------------------------------------

    if _has(f, "how many", "stats", "statistics", "overview", "wie viele",
            "wieviele", "überblick", "ueberblick", "library stats",
            "bibliothek", "was habe ich", "what do i have", "how big",
            "how large", "library size", "size of my library",
            "how much music", "wie groß", "wie gross", "wieviel musik") \
            and len(f.split()) <= 10:
        return Plan("library-overview", {}, confidence=0.85)

    # -- search -----------------------------------------------------------

    search_verb = _has(f, "search", "find", "look for", "suche", "such",
                       "finde", "zeig", "zeige", "show me", "do i have",
                       "habe ich", "have i got")
    if search_verb:
        query = original
        query = re.sub(r"(?i)\b(?:search|look for|find|suche|such|finde|zeig|"
                       r"zeige|show me|do i have|habe ich|have i got|"
                       r"in my library|in der bibliothek|"
                       r"please|bitte|for|nach|mir|me)\b", " ", query)
        query = re.sub(r"(?i)\b(?:everything|all|alle|alles)\s+(?:by|von)\b",
                       " ", query)
        query = re.sub(r"(?i)^\s*(?:by|von)\s+", " ", query)
        query = re.sub(r"(?i)\b(?:songs?|tracks?|titel|lieder|alben|albums?|"
                       r"bands?|artists?|künstler|kuenstler)\b", " ", query)
        query = " ".join(query.split()).strip(" .,!?:;-")
        if query:
            return Plan("search-library", {"query": query, "limit": 12},
                        confidence=0.9)

    if _has(f, "help", "hilfe", "skills", "commands", "befehle",
            "what can you do", "was kannst du", "how does this work"):
        return Plan("library-overview", {"help": True}, confidence=0.9)

    return None
