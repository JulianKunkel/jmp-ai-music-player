"""Every setting the app has, in one declarative table.

The chat skill, the `/api/settings` route and the language model's tools all
read this module, so a setting can never be validated in one place and written
in another.  A new setting is one entry in :data:`SPECS` — the page builds its
controls from the same list, and an unknown key is refused everywhere.

Two kinds of setting live here:

* **profile settings** are remembered in `library-work/app/chat/profile.json`
  (`profile.setting`), read on demand by the engine and the page;
* **secret settings** (an API key) go to `library-work/app/chat/secrets.json`
  with mode 0600, so they never land in a profile that is shown or exported;
* the **music folder** (`library`) is service-level: the engine only validates
  it and defers the switch to the web service, which owns the container, the
  player and the catalogue.

Nothing in this module touches a music file; a setting is app state.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .profile import fold
from .psycho import SETS as PSYCHO_SETS
from .viz import STYLES as VIZ_STYLES

#: the music folder is the only setting that changes the running service
SERVICE_KEYS = {"library"}

#: values that are written to their own 0600 file instead of profile.json
SECRET_KEYS = {"llm_api_key"}

#: an OpenAI-compatible server per provider; the address is only a default
PROVIDER_BASE = {
    "ollama": "http://127.0.0.1:11434/v1",
    "gwdg": "https://chat-ai.academiccloud.de/v1",
}

#: providers that accept any key; Ollama ignores the header but wants one
PROVIDER_KEYS = {"ollama": "ollama"}

_TRUE_WORDS = {
    "on", "true", "yes", "y", "1", "enabled", "enable", "activated", "active",
    "an", "ein", "angeschaltet", "aktiv", "aktiviert", "einschalten",
    "anschalten", "anmachen", "einschalte", "turn on", "switch on",
}
_FALSE_WORDS = {
    "off", "false", "no", "n", "0", "disabled", "disable", "deactivated",
    "inactive", "aus", "ausgeschaltet", "inaktiv", "deaktiviert",
    "ausschalten", "ausmachen", "turn off", "switch off",
}
_TOGGLE_WORDS = ("toggle", "umschalten", "flip", "switch it", "das gegenteil")
_RESET_WORDS = ("reset", "zuruecksetzen", "zurückzusetzen", "restore",
                "back to default", "back to the default", "auf standard",
                "standardwert", "default value", "defaults")
_SHOW_WORDS = ("show", "what", "which", "how", "was", "welche", "welcher",
               "welches", "wie", "zeig", "zeige", "status", "current",
               "aktuell", "is set", "are set", "eingestellt", "steht",
               "list", "tell me", "explain")
_LIST_WORDS = ("settings", "setting", "einstellungen", "einstellung",
               "optionen", "options", "preferences", "konfiguration")

#: how each choice value is written, in the order shown
_CHOICES = {
    "viz_style": {key: label for key, label in VIZ_STYLES.items()},
    "psycho_set": {item["key"]: item["label"] for item in PSYCHO_SETS},
    "loudness_level": {"off": "off (play as mastered)",
                       "track": "even out every track"},
    "voice_language": {"auto": "automatic", "de": "German", "en": "English"},
    "voice_model": {"auto": "automatic", "small": "small (CPU)",
                    "medium": "medium (GPU)"},
    "llm_provider": {"auto": "automatic (installed configuration)",
                     "ollama": "local Ollama server",
                     "gwdg": "GWDG / SAIA (AcademicCloud)",
                     "custom": "other OpenAI-compatible server"},
}

#: extra words a choice accepts besides its key and label
_VALUE_WORDS = {
    "viz_style": {
        "wave": ("wave", "bars", "waveform", "balken", "welle"),
        "eq": ("equalizer", "eq", "spectrum bars", "balken"),
        "leds": ("led", "leds", "led columns", "saeulen", "columns"),
        "scope": ("scope", "oscilloscope", "oszi", "wave line"),
        "vu": ("vu", "vu meter", "vu meters", "zeiger"),
        "spectrum": ("spectrum", "fft", "frequencies", "frequenz"),
        "axes": ("axes", "properties", "profile bars", "eigenschaften"),
    },
    "psycho_set": {
        "psychology": ("psychology", "psychologisch", "emotion", "emotions",
                       "arousal"),
        "timbre": ("timbre", "klang", "klangfarbe", "sound"),
        "rhythm": ("rhythm", "rhythmus", "beat", "groove"),
        "dynamics": ("dynamics", "dynamik", "loudness", "mastering"),
        "instrument": ("instrument", "instruments", "instrumente",
                       "instruments set"),
    },
    "loudness_level": {
        "off": ("off", "as mastered", "original", "aus", "ausgeschaltet",
                "wie gemastert", "unveraendert", "unverändert"),
        "track": ("track", "per track", "every track", "even out", "normalise",
                  "normalize", "levelling", "leveling", "pro titel",
                  "jeden titel", "angleichen", "gleiche"),
    },
    "voice_language": {
        "auto": ("auto", "automatic", "automatisch", "detect", "detected"),
        "de": ("de", "german", "deutsch", "german language"),
        "en": ("en", "english", "englisch", "english language"),
    },
    "voice_model": {
        "auto": ("auto", "automatic", "automatisch"),
        "small": ("small", "tiny", "fast", "schnell", "cpu"),
        "medium": ("medium", "bigger", "better", "genauer", "gpu"),
    },
    "llm_provider": {
        "auto": ("auto", "automatic", "automatisch", "installed configuration",
                 "standard", "default"),
        "ollama": ("ollama", "local", "lokal", "local server", "lokaler server",
                   "ollama server", "local model", "lokales modell"),
        "gwdg": ("gwdg", "saia", "academiccloud", "academic cloud",
                 "gwdg saia", "gwdg server", "gwdg api", "goettingen",
                 "göttingen"),
        "custom": ("custom", "other", "own server", "openai compatible",
                   "openai-compatible", "any server", "eigener server",
                   "anderer server"),
    },
}

#: how a free-text value is pulled out of a sentence, per setting
_TEXT_PATTERNS = {
    "llm_base_url": (
        r"(https?://[^\s\"'<>]+)",
        r"\b((?:[\w.-]+:\d{2,5})(?:/[^\s\"'<>]*)?)",
    ),
    "llm_model": (
        r"(?:model|modell)\s+(?:to|is|as|auf|=|:)?\s*[\"'`]?"
        r"([\w.:/@+-]{2,})",
        r"(?:use|nutze|nimm|waehle|wähle)\s+[\"'`]?([\w.:/@+-]{2,})[\"'`]?"
        r"\s+(?:as|als)\s+(?:the\s+|das\s+|den\s+)?(?:model|modell)",
    ),
    "llm_api_key": (
        r"(?:api[\s-]?key|apikey|schl(?:ue|ü)ssel|api token)\s*"
        r"(?:to|is|=|:|on)?\s*[\"'`]?([A-Za-z0-9._~+/=-]{8,})",
        r"\b(sk-[A-Za-z0-9._-]{8,})\b",
    ),
}


def _spec(key: str, label: str, kind: str, group: str, default,
          aliases: tuple[str, ...], help_text: str = "",
          choices: dict[str, str] | None = None,
          minimum: int = 0, maximum: int = 0) -> dict:
    return {
        "key": key,
        "label": label,
        "kind": kind,                       # bool | choice | int | path
        "group": group,
        "default": default,
        "aliases": aliases,
        "help": help_text,
        "choices": choices or {},
        "minimum": minimum,
        "maximum": maximum,
        "service": key in SERVICE_KEYS,
    }


SPECS: tuple[dict, ...] = (
    _spec("library", "Music folder", "path", "Library", "",
          ("music folder", "music directory", "musikordner", "musik ordner",
           "musikverzeichnis", "library folder", "library path",
           "library root", "bibliotheksordner", "bibliothek pfad",
           "musik ordner pfad"),
          "The folder the app reads.  Changing it reopens the library live."),
    _spec("use_llm", "Language model planner", "bool", "Chat", True,
          ("language model", "llm", "planner", "sprachmodell", "ki planer",
           "use the model", "use llm"),
          "Off means the offline parser plans every turn — no network."),
    _spec("default_count", "Tracks per request", "int", "Chat", 40,
          ("default count", "tracks per request", "number of tracks",
           "default number", "anzahl titel", "standardanzahl"),
          "How many tracks a plain request aims for (5–300).",
          minimum=5, maximum=300),
    _spec("visualizer", "Visualizer", "bool", "Playback", True,
          ("visualizer", "visualiser", "visualisierung", "visuals",
           "waveform strip", "visualizer strip"),
          "The now-playing strip above the composer."),
    _spec("viz_style", "Visualizer style", "choice", "Playback", "wave",
          ("visualizer style", "visualizer type", "viz style", "viz style",
           "visualisation style", "visualisierung stil", "visualisierung art",
           "waveform style"),
          "How the strip draws the current track.",
          choices=_CHOICES["viz_style"]),
    _spec("autoplay", "Autoplay", "bool", "Playback", True,
          ("autoplay", "auto play", "auto-play", "automatisch abspielen",
           "autostart", "play automatically"),
          "Start playing as soon as something is chosen."),
    _spec("loudness_level", "Loudness levelling", "choice", "Playback", "off",
          ("loudness levelling", "loudness leveling", "loudness", "levelling",
           "leveling", "lautstarke", "lautstaerke", "lautheit"),
          "Pull every track towards one loudness while it plays, using the "
          "measured level.  The audio files are never rewritten.",
          choices=_CHOICES["loudness_level"]),
    _spec("fade_songs", "Soft endings (fade)", "bool", "Playback", True,
          ("soft endings", "soft ending", "fading", "fade songs",
           "fade the songs", "fade out", "fade-out", "fade", "crossfade",
           "cross fade", "sanftes ausblenden", "ausblenden", "ueberblenden",
           "überblenden", "sanfter uebergang", "sanfter übergang",
           "weiches ende"),
          "Ease the last seconds of every track out and the next one in, so "
          "tracks blend instead of cutting.  Playback only; the audio files "
          "are never rewritten."),
    _spec("fade_seconds", "Fade length", "int", "Playback", 3,
          ("fade length", "fade duration", "fade time", "fade seconds",
           "crossfade length", "crossfade duration", "length of the fade",
           "dauer des ausblendens", "ausblenddauer", "laenge des ausblendens",
           "länge des ausblendens", "ueberblenddauer", "überblenddauer"),
          "How many seconds a track eases out (1–10).",
          minimum=1, maximum=10),
    _spec("psycho_auto", "Measure new tracks automatically", "bool",
          "Sound profile", True,
          ("automatic measurement", "auto measurement", "auto measure",
           "automatic measuring", "measure automatically", "psycho auto",
           "automatische messung"),
          "The app measures tracks it has no sound profile for, in the "
          "background."),
    _spec("psycho_set", "Profile lens", "choice", "Sound profile",
          "psychology",
          ("profile set", "profile lens", "psycho set", "lens", "perspective",
           "profil satz", "profil linse", "psycho satz"),
          "Which set of axes the Profile panel and “similar” use.",
          choices=_CHOICES["psycho_set"]),
    _spec("lyrics_panel", "Lyrics panel", "bool", "Lyrics", False,
          ("lyrics panel", "lyric panel", "songtext panel", "lyrics view",
           "text panel", "songtext anzeige"),
          "Keep the timed lyrics under the now-playing strip open."),
    _spec("library_backup", "Daily plain-text backup", "bool", "Library", True,
          ("daily backup", "library backup", "backup", "back up",
           "tägliche sicherung", "sicherung", "datensicherung",
           "backup der bibliotheksdaten", "library data backup"),
          "Once a day the app writes the sound profile and the lyrics as "
          "plain text to LibraryData/ in the library."),
    _spec("library_backup_keep", "Backups to keep", "int", "Library", 7,
          ("backups to keep", "backup days", "keep backups", "backup retention",
           "backups", "exports to keep", "export files",
           "sicherungen behalten", "aufbewahrung", "anzahl sicherungen",
           "sicherungen"),
          "How many daily exports stay in LibraryData/ (1–365).",
          minimum=1, maximum=365),
    _spec("voice_enabled", "Voice input", "bool", "Voice", True,
          ("voice input", "voice control", "microphone", "mikrofon",
           "speech input", "spracheingabe", "sprachsteuerung", "voice"),
          "The microphone button in the composer (local Whisper)."),
    _spec("voice_autosend", "Send after speaking", "bool", "Voice", True,
          ("voice autosend", "autosend", "auto send", "automatically send",
           "automatisch senden", "send after speaking", "nach dem sprechen "
           "senden"),
          "On: a finished recording runs at once.  Off: it is put into the "
          "text box so it can be corrected first."),
    _spec("voice_language", "Spoken language", "choice", "Voice", "auto",
          ("voice language", "spoken language", "speech language",
           "spracheingabe sprache", "sprache fuer voice", "language for voice"),
          "What the microphone expects; “auto” detects it per clip.",
          choices=_CHOICES["voice_language"]),
    _spec("voice_model", "Transcription model", "choice", "Voice", "auto",
          ("voice model", "transcription model", "speech model",
           "whisper model", "modell fuer sprache"),
          "“auto” is medium on a GPU and small on the CPU.",
          choices=_CHOICES["voice_model"]),
    _spec("llm_provider", "Language model provider", "choice", "Language model",
          "auto",
          ("llm provider", "model provider", "language model provider",
           "provider", "anbieter", "ollama", "local model server",
           "lokaler modellserver", "gwdg", "gwdg server", "gwdg api", "saia",
           "saia server", "academiccloud", "academic cloud",
           "openai compatible server", "openai-compatible server",
           "custom server", "model backend"),
          "“automatic” uses an installed configuration; “ollama” a server on "
          "this machine; “GWDG / SAIA” the AcademicCloud; “other” any "
          "OpenAI-compatible address.",
          choices=_CHOICES["llm_provider"]),
    _spec("llm_base_url", "Language model server", "text", "Language model",
          "",
          ("llm server", "model server", "llm base url", "base url",
           "modellserver", "server url", "ollama url", "ollama server url",
           "server", "address"),
          "Empty means the provider's own address; otherwise the full "
          "OpenAI-compatible base, usually ending in /v1."),
    _spec("llm_api_key", "API key", "secret", "Language model", "",
          ("api key", "api-key", "apikey", "api schluessel", "api token",
           "gwdg key", "saia key", "token for the model", "schluessel"),
          "Kept locally in secrets.json (mode 0600); never shown again."),
    _spec("llm_model", "Language model", "text", "Language model", "",
          ("llm model", "model name", "modellname", "modell name",
           "language model name", "sprachmodell name", "model"),
          "Empty means: the first model the server lists."),
)

BY_KEY = {spec["key"]: spec for spec in SPECS}


def spec(key: str) -> dict:
    return BY_KEY.get(str(key or "").strip())


def resolve(text: str) -> str:
    """The setting a (folded or plain) phrase names — longest alias wins."""
    flat = " ".join(fold(text).split())
    found = ""
    length = 0
    for item in SPECS:
        for alias in item["aliases"]:
            if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", flat) \
                    and len(alias) > length:
                found, length = item["key"], len(alias)
    return found


def _choice_value(item: dict, text: str) -> str:
    """The choice a phrase names, or "" when it names none of them."""
    flat = " " + " ".join(fold(text).split()) + " "
    words = _VALUE_WORDS.get(item["key"], {})
    best, length = "", 0
    for value in item["choices"]:
        candidates = (value,) + tuple(words.get(value, ()))
        for candidate in candidates:
            if re.search(rf"(?<!\w){re.escape(fold(candidate))}(?!\w)", flat) \
                    and len(candidate) > length:
                best, length = value, len(candidate)
    return best


def _value(item: dict, text: str, *, around: str = ""):
    """(value, found) for one setting, from a free sentence."""
    flat = " " + " ".join(fold(text).split()) + " "
    kind = item["kind"]
    if kind == "choice":
        value = _choice_value(item, text)
        return value, bool(value)
    if kind == "int":
        # a wider match is clamped by `coerce`, so "1000 tracks" becomes the
        # maximum instead of being read as a question ("is the … ?")
        match = re.search(r"(?<!\d)(\d{1,5})(?!\d)", flat)
        if not match:
            return "", False
        return int(match.group(1)), True
    if kind == "path":
        for token in str(text or "").split():
            name = token.strip(" .,;:!?()[]'\"“”")
            if name.startswith(("/", "~")) and len(name) > 1:
                return name, True
        return "", False
    if kind in {"text", "secret"}:
        for pattern in _TEXT_PATTERNS.get(item["key"], ()):
            match = re.search(pattern, str(text or ""), re.IGNORECASE)
            if match:
                value = match.group(1).strip(" .,;:\"'“”")
                if value:
                    return value, True
        return "", False
    # bool: a value word, but a bare "an"/"aus" only next to a set verb
    verb = re.search(r"(?:turn|switch|set|put|make|enable|disable|schalte|"
                     r"schalt|mach|setze|stelle|aktiviere|deaktiviere)\b",
                     flat)
    for word in sorted(_TRUE_WORDS | _FALSE_WORDS, key=len, reverse=True):
        pattern = rf"(?<!\w){re.escape(fold(word))}(?!\w)"
        match = re.search(pattern, flat)
        if not match:
            continue
        if word in {"an", "aus", "ein"} and not verb:
            continue
        return word in _TRUE_WORDS, True
    return "", False


def coerce(item: dict, raw):
    """Validate one incoming value; raises ValueError with a readable reason."""
    kind = item["kind"]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        flat = " ".join(fold(raw).split())
        if raw in (0, 1) or flat in {"0", "1"}:
            return bool(int(raw))
        if flat in {fold(word) for word in _TRUE_WORDS}:
            return True
        if flat in {fold(word) for word in _FALSE_WORDS}:
            return False
        raise ValueError(f"“{raw}” is not on or off")
    if kind == "choice":
        flat = " ".join(fold(raw).split())
        for value, label in item["choices"].items():
            if flat in {fold(value), fold(label)}:
                return value
        words = _VALUE_WORDS.get(item["key"], {})
        for value, candidates in words.items():
            if flat in {fold(candidate) for candidate in candidates}:
                return value
        allowed = ", ".join(item["choices"])
        raise ValueError(f"“{raw}” is not one of: {allowed}")
    if kind == "int":
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"“{raw}” is not a number") from None
        low, high = item["minimum"], item["maximum"]
        if low and high:
            value = max(low, min(high, value))
        return value
    if kind == "path":
        name = str(raw or "").strip().strip("'\"")
        if not name:
            raise ValueError("the folder name is empty")
        return str(Path(name).expanduser())
    if kind == "text":
        value = str(raw or "").strip().strip("'\"")
        if item["key"] == "llm_base_url":
            if not re.match(r"(?i)^https?://", value):
                raise ValueError("the server address must start with "
                                 "http:// or https://")
            return value.rstrip("/")
        return value
    if kind == "secret":
        return str(raw or "").strip().strip("'\"")
    return raw


# ---------------------------------------------------------------------------
# reading and writing


def secrets_path(root: str | Path) -> Path:
    return Path(root) / "library-work" / "app" / "chat" / "secrets.json"


def read_secrets(root: str | Path = "") -> dict:
    """The secret settings, tolerantly read; never raises."""
    if not root:
        return {}
    try:
        data = json.loads(secrets_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_secret(root: str | Path, key: str, value) -> None:
    """Store one secret atomically in a file only this user can read."""
    if not root:
        raise ValueError("no library root, so a secret cannot be stored")
    path = secrets_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_secrets(root)
    if value in (None, ""):
        data.pop(key, None)
    else:
        data[key] = str(value)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".secrets-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
            stream.write("\n")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except OSError:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def has_secret(root: str | Path, key: str) -> bool:
    return bool(str(read_secrets(root).get(key) or "").strip())


def values(profile, root: str | Path = "") -> dict:
    """Every setting as the app currently has it (secrets included)."""
    result = {}
    secrets = read_secrets(root) if root else {}
    for item in SPECS:
        key = item["key"]
        if key == "library":
            result[key] = str(root or "")
        elif key in SECRET_KEYS:
            result[key] = str(secrets.get(key) or "")
        else:
            value = profile.setting(key, item["default"])
            if item["kind"] == "choice" \
                    and str(value) not in item["choices"]:
                value = item["default"]
            elif item["kind"] == "bool":
                value = bool(value)
            result[key] = value
    return result


def api(profile, root: str | Path = "") -> list[dict]:
    """The list the settings panel builds its controls from.

    A secret is never sent back: the row says whether one is stored, and the
    page shows an empty password field that only replaces it when filled in.
    """
    current = values(profile, root)
    rows = []
    for item in SPECS:
        row = {key: item[key] for key in
               ("key", "label", "kind", "group", "help", "default",
                "minimum", "maximum", "service")}
        value = current.get(item["key"], item["default"])
        row["secret"] = item["key"] in SECRET_KEYS
        row["has_value"] = bool(str(value or "").strip())
        row["value"] = "" if row["secret"] else value
        row["choices"] = [{"value": key, "label": label}
                          for key, label in item["choices"].items()]
        rows.append(row)
    return rows


def apply(profile, item: dict, value, root: str | Path = "") -> None:
    """Write one setting (the service keys are handled elsewhere)."""
    if item["key"] in SERVICE_KEYS:
        return
    if item["key"] in SECRET_KEYS:
        write_secret(root, item["key"], value)
        return
    profile.set_setting(item["key"], value)


def reset(profile, item: dict, root: str | Path = "") -> None:
    apply(profile, item, item["default"], root)


def describe(profile, item: dict, root: str | Path = "") -> str:
    """One line: `**Label** is `value` (kind) — help`."""
    current = values(profile, root).get(item["key"], item["default"])
    if item["kind"] == "bool":
        shown = "on" if current else "off"
    elif item["kind"] == "choice":
        shown = item["choices"].get(str(current), str(current))
    elif item["kind"] == "secret":
        shown = "set (hidden)" if current else "not set"
    elif item["key"] == "llm_base_url" and not current:
        # an empty address means "the provider's own", so say which one
        provider = str(profile.setting("llm_provider", "auto") or "auto")
        preset = PROVIDER_BASE.get(provider, "")
        shown = f"the {provider} address ({preset})" if preset else "—"
    else:
        shown = str(current) or "—"
    text = f"{item['label']} is **{shown}**"
    if item["help"]:
        text += f" — {item['help']}"
    return text


def markdown_all(profile, root: str | Path = "", *, group: str = "") -> str:
    """The settings table, grouped, with the current value of each."""
    current = values(profile, root)
    groups: dict[str, list[dict]] = {}
    for item in SPECS:
        if group and fold(item["group"]) != fold(group):
            continue
        groups.setdefault(item["group"], []).append(item)
    lines = ["## Settings", "",
             "Every setting is reachable from here; a music file is never "
             "touched.  Ask “set the … to …”, “turn off the …”, or “reset "
             "the …”." ]
    for name, items in groups.items():
        lines += ["", f"### {name}", ""]
        for item in items:
            value = current.get(item["key"], item["default"])
            if item["kind"] == "bool":
                shown = "on" if value else "off"
            elif item["kind"] == "choice":
                shown = item["choices"].get(str(value), str(value))
            elif item["kind"] == "secret":
                shown = "set (hidden)" if value else "not set"
            else:
                shown = f"`{value}`" if value else "—"
            line = f"- **{item['label']}** · {shown}"
            if item["help"]:
                line += f"  ·  {item['help']}"
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the sentence


#: other words a group's settings are asked for by ("Spracheinstellungen")
_GROUP_WORDS = {
    "Voice": ("sprache", "spracheingabe", "mikrofon", "voice input"),
    "Language model": ("sprachmodell", "llm", "ki modell", "model"),
    "Playback": ("wiedergabe",),
    "Sound profile": ("klangprofil", "psycho"),
    "Lyrics": ("songtext", "liedtext"),
    "Library": ("bibliothek", "musikordner"),
    "Chat": ("planner", "planer"),
}


def _named_group(text: str) -> str:
    """The setting group a sentence names ("voice settings"), or ""."""
    flat = " " + " ".join(fold(text).replace("_", " ").split()) + " "
    best, length = "", 0
    for item in SPECS:
        candidates = (fold(item["group"]),) + tuple(
            fold(word) for word in _GROUP_WORDS.get(item["group"], ()))
        for name in candidates:
            if len(name) > length and re.search(
                    rf"(?<!\w){re.escape(name)}(?!\w)", flat):
                best, length = item["group"], len(name)
    return best


def parse(text: str) -> dict | None:
    """A settings intent from a sentence, or None when it is something else.

    Deliberately conservative: a sentence only counts when it names a setting
    *and* asks for it, so "play the song Visualiser" stays a playback request.
    Returns ``{"action": "list"}``, ``{"action": "show", "key": …}``,
    ``{"action": "set", "key": …, "value": …}``, ``{"action": "toggle", …}``
    or ``{"action": "reset", …}``.
    """
    original = str(text or "").strip()
    if not original:
        return None
    flat = " ".join(fold(original).replace("_", " ").split())
    padded = f" {flat} "
    key = resolve(original)
    list_word = any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", padded)
                    for word in _LIST_WORDS)
    if list_word:
        group = _named_group(original)
        if group:
            return {"action": "list", "group": group}
    if not key:
        if list_word:
            return {"action": "list"}
        return None
    item = BY_KEY[key]
    if key == "visualizer":
        style_value = _choice_value(BY_KEY["viz_style"], original)
        if style_value:
            key, item = "viz_style", BY_KEY["viz_style"]
    show = any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", padded)
               for word in _SHOW_WORDS)
    verb = any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", padded)
               for word in ("set", "change", "turn", "switch", "put", "use",
                            "enable", "disable", "activate", "deactivate",
                            "schalte", "schalt", "setze", "stelle", "mach",
                            "andere", "ändere", "aendere", "aktiviere",
                            "deaktiviere", "waehle", "wähle", "nimm"))
    question = (original.rstrip().endswith("?") or bool(re.match(
        r"\s*(?:is|are|was|were|does|do|did|how|what|which|who|where|why|"
        r"can|could|should|would|ist|sind|war|waren|wie|welche|welcher|"
        r"welches|wo|warum|kann|soll)\b", flat))) and not verb
    if key == "fade_songs" and re.search(
            r"(?<!\w)(?:how long|wie lang(?:e)?|welche lange)(?!\w)", flat):
        # “how long is the fade” asks for the length, not for the switch
        return {"action": "show", "key": "fade_seconds"}
    if question:
        return {"action": "show", "key": key}
    if key == "fade_songs":
        # "fade the songs out over 4 seconds" names the pair, not only the
        # switch: a number sets the length and switches the fading on
        count = re.search(r"(?<!\d)(\d{1,2})(?!\d)", flat)
        refused = any(
            re.search(rf"(?<!\w){re.escape(fold(word))}(?!\w)", padded)
            for word in _FALSE_WORDS)
        if count and not refused:
            return {"action": "set", "key": "fade_songs", "value": True,
                    "also": {"fade_seconds": int(count.group(1))}}
    if any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", padded)
           for word in _RESET_WORDS) and not re.search(r"(?<!\d)\d{1,3}(?!\d)",
                                                       padded):
        return {"action": "reset", "key": key}
    value, found = _value(item, original)
    if not found and key == "use_llm" and re.search(
            r"(?<!\w)(?:use|nutze|verwende|enable)(?!\w)", padded):
        value = not any(re.search(rf"(?<!\w){word}(?!\w)", padded)
                        for word in ("offline", "offline parser",
                                     "local parser", "lokale parser",
                                     "without", "ohne", "no model",
                                     "kein modell"))
        found = True
    if found:
        result = {"action": "set", "key": key, "value": value}
        # one sentence may configure a whole group: "use gwdg with api key X"
        if item["group"] == "Language model":
            also = {}
            for other in SPECS:
                if other["key"] == key or other["group"] != item["group"]:
                    continue
                if other["kind"] not in {"text", "secret", "choice"}:
                    continue
                other_value, other_found = _value(other, original)
                if other_found and other_value != other["default"]:
                    also[other["key"]] = other_value
            if also:
                result["also"] = also
        return result
    if any(re.search(rf"(?<!\w){re.escape(word)}", padded)
           for word in _TOGGLE_WORDS) and item["kind"] == "bool":
        return {"action": "toggle", "key": key}
    if show or list_word or verb or item["kind"] == "path":
        return {"action": "show", "key": key}
    return None
