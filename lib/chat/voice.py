"""Voice input: speak a sentence, get its text back — entirely on this box.

The window records a few seconds with `MediaRecorder` (or a WebAudio WAV
fallback), posts the raw bytes to `POST /api/voice`, and this module hands the
bytes to the library's own transcriber:

    scripts/transcribe_lyrics.py --text <file>

That mode prints one JSON line and never touches the lyrics database, so a
spoken command is not a lyric and leaves no row behind.  No cloud service, no
provider key, no upload: the audio exists only as a temp file under

    library-work/app/chat/voice/<random>.<ext>

which is deleted the moment the child exits (also on failure).  Nothing is
stored between turns.

The interpreter is the one the lyrics job already probes (`lyrics_job`): an
explicit `MUSIC_ASR_PYTHON`, the host `scripts/asr-venv` (usually CUDA), or the
app's own interpreter — which carries the backend inside the container.

The returned text is then typed into the chat, so it steers the app exactly
like a typed sentence: "spiel etwas Ruhiges", "make a playlist for work",
"what is this song".
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from . import lyrics_job

SCRIPT = "transcribe_lyrics.py"

#: what the browser may send; the suffix follows the posted MIME type
SUFFIXES = {
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".flac": "audio/flac",
}

#: MIME type -> suffix, for the upload path (the page names what it recorded)
TYPES = {
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/flac": ".flac",
}

#: a spoken command is short; anything bigger is not a microphone clip
MAX_BYTES = 32 * 1024 * 1024

#: one clip transcribes in seconds; the CPU small model needs ~15 s per minute
TIMEOUT = 300.0

#: keep the temp folder tidy: a clip older than this is a leftover
KEEP_SECONDS = 6 * 3600

#: Whisper's well-known artefacts: on music or silence it sometimes invents
#: one of these instead of admitting it heard nothing.  A spoken request is
#: never *only* one of them, so such an answer is treated as "not understood"
#: rather than being typed into the chat as a command.
_HALLUCINATIONS = {
    "thanks for watching",
    "thank you for watching",
    "thanks for watching everyone",
    "please subscribe",
    "please subscribe to my channel",
    "subtitles by the amaraorg community",
    "untertitel der amaraorg community",
    "untertitel von amaraorg",
    "amaraorg",
    "vielen dank fürs zuschauen",
    "vielen dank fuer s zuschauen",
    "sous titres realises par la communaute d amaraorg",
    "untertitelung des zdf",
}


def _noise(text: str) -> bool:
    """True when the transcriber only reproduced one of its artefacts."""
    # compare without any punctuation or spacing, so "Amara.org-Community"
    # and "amara org community" are the same string here
    flat = "".join(ch for ch in str(text or "").lower() if ch.isalnum())
    if not flat:
        return True                      # punctuation or music symbols only
    return flat in {item.replace(" ", "") for item in _HALLUCINATIONS}


def folder(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "chat" / "voice"


def available(root: Path) -> bool:
    return (lyrics_job.scripts(root) / SCRIPT).is_file()


def suffix_for(content_type: str) -> str:
    """Map an upload's MIME type to a file suffix (".webm" when unknown)."""
    name = str(content_type or "").split(";", 1)[0].strip().lower()
    return TYPES.get(name, ".webm")


def status(root: Path) -> dict:
    """Can the window offer a microphone at all, and how long may a clip be?"""
    root = Path(root)
    python = lyrics_job.interpreter(root)
    info = lyrics_job.backend_info(root) if python else {
        "ready": False, "gpu": "unknown", "models": []}
    return {
        "available": available(root),
        "ready": bool(python) and available(root),
        "interpreter": python,
        "gpu": info.get("gpu", "unknown"),
        "models": info.get("models", []),
        "max_bytes": MAX_BYTES,
        "timeout": TIMEOUT,
        "types": sorted(set(TYPES)),
        "note": "" if python else "no local transcriber is installed "
                                  "(scripts/setup_asr.sh)",
    }


def _prune(root: Path) -> None:
    """Remove clips a crash left behind; nothing here is ever needed twice."""
    now = time.time()
    try:
        for entry in folder(root).iterdir():
            try:
                if entry.is_file() and now - entry.stat().st_mtime > KEEP_SECONDS:
                    entry.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _parse(stdout: str) -> dict:
    """The JSON line the transcriber prints last, whatever noise precedes it."""
    for line in reversed(str(stdout or "").splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def transcribe(root: Path, data: bytes, *, content_type: str = "",
               suffix: str = "", language: str = "auto",
               model: str = "", timeout: float = TIMEOUT) -> dict:
    """Transcribe one recorded clip; never raises, never keeps the audio."""
    root = Path(root)
    data = data or b""
    if not data:
        return {"ok": False, "error": "the recording was empty"}
    if len(data) > MAX_BYTES:
        return {"ok": False,
                "error": f"the recording is too large ({len(data)} bytes); "
                         f"at most {MAX_BYTES} are accepted"}
    if not available(root):
        return {"ok": False,
                "error": "this library has no scripts/transcribe_lyrics.py, so "
                         "voice input is not possible"}
    python = lyrics_job.interpreter(root)
    if not python:
        return {"ok": False, "error": "no local transcriber is installed — run "
                                      "scripts/setup_asr.sh (or rebuild the "
                                      "container with the ASR packages)"}

    suffix = suffix if suffix in SUFFIXES else suffix_for(content_type)
    if suffix not in SUFFIXES:
        suffix = ".webm"
    _prune(root)
    target = folder(root) / f"{uuid.uuid4().hex}{suffix}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        argv = [python, "-u", str(lyrics_job.scripts(root) / SCRIPT),
                "--text", str(target), "--language", str(language or "auto")]
        if model:
            argv += ["--model", str(model)]
        started = time.time()
        try:
            result = subprocess.run(
                argv, cwd=str(root), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=float(timeout or TIMEOUT), text=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"})
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "the transcription took too long; "
                                          "try a shorter sentence"}
        elapsed = round(time.time() - started, 1)
        payload = _parse(result.stdout)
        if not payload or payload.get("error"):
            detail = str(payload.get("error") or "").strip()
            if not detail:
                tail = (result.stderr or "").strip().splitlines()[-3:]
                detail = " ".join(tail) or f"exit code {result.returncode}"
            return {"ok": False, "error": f"could not transcribe: {detail}"}
        text = " ".join(str(payload.get("text") or "").split())
        if text and _noise(text):
            # "Thanks for watching!" on a wordless stretch is not a request
            text = ""
        answer = {
            "ok": bool(text),
            "text": text,
            "language": payload.get("language") or "",
            "model": payload.get("model") or model or "auto",
            "backend": payload.get("backend") or "",
            "seconds": payload.get("seconds") or elapsed,
            "bytes": len(data),
        }
        if not text:
            answer["error"] = "I heard nothing — try again a little louder"
        return answer
    except OSError as exc:
        return {"ok": False, "error": f"could not record that: {exc}"}
    finally:
        try:
            target.unlink()
        except OSError:
            pass
