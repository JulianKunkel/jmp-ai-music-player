"""What the player can really play, and what the library holds.

"Can you play spc files?" is a question about the player, not a command.  The
play rule used to strip the word *play* and search the titles for "can you spc
files", which answered "Nothing matches".  A format is not a title, so this
module answers it from one table instead:

* **playable** — mpv is the player and ffmpeg is its decoder, so a container
  mpv knows plays even when the library holds none of it (``.flac``);
* **indexed, not playable** — ffmpeg has no decoder for C64 SID, Atari YM or
  Play! modules, so those files are listed by the library but a click on one
  cannot make a sound;
* **chiptune** — an SPC or a tracker module plays, but it carries no tag
  field at all: no title, no artist, no genre, no cover art.  That is why it
  never appears in a title search and why the chiptune catalogue indexes it by
  system and game instead.

Read-only: the counts come from the inventory and no audio file is opened.
"""

from __future__ import annotations

from pathlib import Path

from .catalog import AUDIO_EXT, fold

#: what mpv/ffmpeg can decode: extension -> the name a person recognises
PLAYABLE = {
    ".mp3": "MPEG-1/2 audio", ".mp2": "MPEG audio", ".flac": "FLAC",
    ".wav": "WAV", ".ogg": "Ogg Vorbis", ".opus": "Ogg Opus",
    ".m4a": "MPEG-4 audio", ".mp4": "MPEG-4 audio", ".aac": "ADTS AAC",
    ".wma": "Windows Media Audio", ".aiff": "AIFF", ".aif": "AIFF",
    ".ape": "Monkey's Audio", ".wv": "WavPack",
    ".spc": "SNES SPC", ".mod": "ProTracker module",
    ".xm": "FastTracker module", ".it": "Impulse Tracker module",
    ".s3m": "Scream Tracker module",
}

#: the library indexes these, but ffmpeg has no decoder for them
NOT_PLAYABLE = {
    ".sid": "C64 SID", ".ym": "Atari YM", ".play": "Play! module",
}

#: fixed-header chiptunes: playable, but they carry no tag field at all
CHIPTUNE = {".spc", ".mod", ".xm", ".it", ".s3m", ".sid", ".ym", ".play"}

#: a format without a tag block (title/artist/genre/artwork)
UNTAGGABLE = CHIPTUNE | {".aac"}

LOSSLESS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".wv"}

#: words a person uses instead of an extension -> the extensions they mean
ALIASES = {
    "chiptune": (".spc", ".mod", ".xm", ".it", ".s3m", ".sid", ".ym"),
    "chip tune": (".spc", ".mod", ".xm", ".it", ".s3m", ".sid", ".ym"),
    "8-bit": (".spc", ".mod", ".xm", ".it", ".s3m", ".sid", ".ym"),
    "8bit": (".spc", ".mod", ".xm", ".it", ".s3m", ".sid", ".ym"),
    "8 bit": (".spc", ".mod", ".xm", ".it", ".s3m", ".sid", ".ym"),
    "snes": (".spc",),
    "super nintendo": (".spc",),
    "tracker": (".mod", ".xm", ".it", ".s3m"),
    "module": (".mod", ".xm", ".it", ".s3m"),
    "modules": (".mod", ".xm", ".it", ".s3m"),
    "lossless": (".flac", ".wav", ".aiff", ".aif", ".ape", ".wv"),
    "vorbis": (".ogg",),
    "windows media": (".wma",),
}

#: short names that are also ordinary words; only accepted with a file marker
AMBIGUOUS = {".sid", ".mod", ".it", ".ym", ".play"}

#: names that must be written with their dot: "play" is an ordinary verb
DOTTED_ONLY = {".play"}

#: "spc files", "spc-file", "spc dateien", ".spc", "spc format"
FILE_MARKER = {"file", "files", "datei", "dateien", "format", "formats",
               "extension", "extensions", "container", "type", "types"}

KNOWN = set(PLAYABLE) | set(NOT_PLAYABLE)


def resolve(word: str, lenient: bool = False) -> list[str]:
    """The extensions a spoken format name means (empty when unknown).

    ``lenient`` accepts the short names that are also ordinary words
    (``.mod``, ``.sid``, ``.it``); a caller turns it on only when the sentence
    really asks about a format ("do you support sid"), never for an ordinary
    sentence where "it" is a pronoun.
    """
    text = fold(" ".join(str(word or "").split())).strip(" ,!?;:")
    if not text:
        return []
    for alias in sorted(ALIASES, key=len, reverse=True):
        if alias in text:
            return list(ALIASES[alias])
    tokens = text.replace("/", " ").replace("\\", " ").split()
    marked = bool(FILE_MARKER & set(tokens))
    for token in tokens:
        raw = token.rstrip(".,;:!?")
        if not raw:
            continue
        ext = "." + raw.lstrip(".")
        if ext not in KNOWN:
            continue
        if ext in DOTTED_ONLY and not raw.startswith("."):
            continue
        # "mod"/"sid"/"it" are ordinary words; "mod file" is a format
        if (ext in AMBIGUOUS and not lenient and not marked
                and not raw.startswith(".") and "." not in raw):
            continue
        return [ext]
    return []


def _counts(root: Path) -> dict:
    """How many files of each audio extension the inventory holds."""
    from . import stats

    counts: dict = {}
    for row in stats.rows(root):
        ext = str(row.get("ext") or "").casefold()
        if not ext:
            ext = Path(str(row.get("rel_path") or "")).suffix.casefold()
        if ext and ext in AUDIO_EXT:
            counts[ext] = counts.get(ext, 0) + 1
    return counts


def _files(count: int) -> str:
    return "{:,} {}".format(count, "file" if count == 1 else "files")


def _quote(text: str) -> str:
    return str(text).replace("`", "'")


def _limits(extensions: list) -> str:
    """The honest sentence about the tags a format cannot carry."""
    chips = [ext for ext in extensions if ext in CHIPTUNE]
    if chips:
        names = ", ".join("`{}`".format(ext) for ext in chips)
        return ("A chiptune has a fixed header instead of a tag block, so "
                + names + " carries no title, artist, genre or cover art: it "
                "plays, but it is not searchable by name.  "
                "`reports/chiptune-catalogue.csv` indexes it by system and "
                "game instead.")
    if any(ext in UNTAGGABLE for ext in extensions):
        return ("This container has no tag block, so it has no title, artist "
                "or cover art in the library.")
    return ""


def _held_text(extensions: list, counts: dict) -> str:
    parts = ["{:,} × `{}`".format(counts.get(ext, 0), ext)
             for ext in extensions if counts.get(ext)]
    total = sum(counts.get(ext, 0) for ext in extensions)
    if not total:
        return ("No `{}` file is in the library, but the player would play "
                "one.".format(extensions[0]))
    return "{} in the library ({}).".format(_files(total), ", ".join(parts))


def answer(root: Path, wanted: str = "", limit: int = 8) -> str:
    """The reply to "can you play <format> files?" — always read-only."""
    counts = _counts(root)
    # a single word is taken at its word ("sid" is a format here, not a
    # pronoun); a whole sentence keeps the strict reading
    extensions = resolve(wanted, lenient=(" " not in str(wanted).strip()))

    if wanted and not extensions:
        clean = _quote(str(wanted).strip().strip("."))
        return ("I don't know the format “" + clean + "”.  "
                + _supported_text(counts, limit))

    if extensions:
        playable = [ext for ext in extensions if ext in PLAYABLE]
        blocked = [ext for ext in extensions if ext in NOT_PLAYABLE]
        if playable:
            names = " / ".join("`{}`".format(ext) for ext in playable)
            labels = ", ".join(PLAYABLE[ext] for ext in playable)
            lines = ["**Yes — " + names + " plays.**", "",
                     labels + ".  mpv is the player and ffmpeg is its "
                     "decoder, so a format ffmpeg knows needs no extra code "
                     "here.", "",
                     _held_text(extensions, counts)]
            limits = _limits(extensions)
            if limits:
                lines += ["", limits]
            if blocked:
                blocked_text = "`, `".join(blocked)
                lines += ["", "`" + blocked_text + "` is the opposite case: "
                          "the library lists it, but ffmpeg has no decoder "
                          "for it, so it cannot be played."]
            return "\n".join(lines)
        if blocked:
            labels = ", ".join(NOT_PLAYABLE[ext] for ext in blocked)
            blocked_text = " / ".join("`{}`".format(ext) for ext in blocked)
            total = sum(counts.get(ext, 0) for ext in extensions)
            return "\n".join([
                "**No — " + blocked_text + " is indexed, not playable.**", "",
                labels + ": the library holds " + _files(total) + ", and "
                "ffmpeg has no decoder for that format, so mpv cannot turn it "
                "into sound.  The file is still listed (and the chiptune "
                "catalogue names it), it simply cannot play here.",
            ])

    return _supported_text(counts, limit)


def _supported_text(counts: dict, limit: int) -> str:
    """The list of formats this player handles, with the library's counts."""
    held = [(ext, counts[ext]) for ext in counts
            if ext in PLAYABLE and counts[ext]]
    held.sort(key=lambda pair: -pair[1])
    lines = ["**Formats I can play** · mpv plays, ffmpeg decodes", ""]
    for ext in PLAYABLE:
        line = "- `{}` — {}".format(ext, PLAYABLE[ext])
        if counts.get(ext):
            line += " · {:,} in the library".format(counts[ext])
        lines.append(line)
    for ext in NOT_PLAYABLE:
        line = "- `{}` — {} · **indexed only, no decoder**".format(
            ext, NOT_PLAYABLE[ext])
        if counts.get(ext):
            line += " · {:,} in the library".format(counts[ext])
        lines.append(line)
    lines += ["", "A chiptune (`.spc`, `.mod`, `.xm`, `.it`, `.s3m`) plays, "
                  "but it has no tag block: no title, artist, genre or cover "
                  "art, so it is indexed by the chiptune catalogue rather "
                  "than by name."]
    if held:
        top = ", ".join("`{}` {:,}".format(ext, count)
                        for ext, count in held[:limit])
        lines += ["", "In this library: " + top + "."]
    return "\n".join(lines)
