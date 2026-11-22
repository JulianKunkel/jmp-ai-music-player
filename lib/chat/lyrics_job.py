"""Transcribe lyrics from inside the app, in the background.

The app never transcribes in-process: it runs the library's own tool
`scripts/transcribe_lyrics.py` as one detached child, exactly like the sound
profile job.  The child writes

    library-work/app/lyrics/jobs/<id>.log     its output, one progress line each
    library-work/app/lyrics/jobs/<id>.rc      its exit code
    library-work/app/lyrics/jobs/<id>.json    the job record (written first)

and the lyrics themselves to `library-work/app/lyrics/lyrics.db`.  Nothing
here writes an audio file or a tag.

One job runs at a time; a second request returns the running one.  A finished
job keeps its record, so a page reload (or a restarted app) still shows the
result.

The interpreter matters: a CUDA-capable `scripts/asr-venv` (created by
`scripts/setup_asr.sh`) transcribes a song in seconds, the app's own venv in
the container uses the CPU and takes about a minute.  :func:`interpreter`
probes both once and picks the first that can import `faster_whisper`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import procs

SCRIPT = "transcribe_lyrics.py"
SETUP = "setup_asr.sh"

_RE_PERCENT = re.compile(r"(\d{1,3}\.\d)%")
_RE_TOTAL = re.compile(r"to need lyrics|tracks need lyrics")
_RE_DONE = re.compile(r"\[lyrics\] done: (\d+) lines")
_RE_TRACK = re.compile(r"^\[lyrics\] (?!done:|backend|no such|already)(.+)$")

#: interpreter -> probe result, per root, for this process only
_PROBED: dict[str, str] = {}


def job_dir(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "lyrics" / "jobs"


def db_path(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "lyrics" / "lyrics.db"


def models_path(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "lyrics" / "models"


def scripts(root: Path) -> Path:
    root = Path(root)
    for candidate in (root / "scripts", root.parent / "scripts"):
        if (candidate / SCRIPT).is_file():
            return candidate
    return root / "scripts"


def available(root: Path) -> bool:
    return (scripts(root) / SCRIPT).is_file()


def setup_available(root: Path) -> bool:
    return (scripts(root) / SETUP).is_file()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _imports(python: str, timeout: float = 25.0) -> bool:
    try:
        result = subprocess.run(
            [python, "-c", "import faster_whisper, ctranslate2"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def interpreter(root: Path) -> str:
    """The python that can run the transcriber here ("" when there is none).

    Order of preference: an explicit `MUSIC_ASR_PYTHON`, the host ASR venv
    (`scripts/setup_asr.sh`, usually CUDA), and finally the app's own
    interpreter — which is what carries the backend inside the container.
    """
    root = Path(root)
    key = str(root)
    if key in _PROBED:
        return _PROBED[key]
    candidates = []
    explicit = os.environ.get("MUSIC_ASR_PYTHON")
    if explicit:
        candidates.append(explicit)
    candidates.append(str(scripts(root) / "asr-venv" / "bin" / "python"))
    candidates.append(sys.executable)
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        if _imports(candidate):
            _PROBED[key] = candidate
            return candidate
    _PROBED[key] = ""
    return ""


def forget_backend(root: Path) -> None:
    _PROBED.pop(str(Path(root)), None)


def backend_info(root: Path) -> dict:
    root = Path(root)
    python = interpreter(root)
    gpu = "unknown"
    if python:
        try:
            result = subprocess.run(
                [python, "-c",
                 "import ctranslate2; print(ctranslate2.get_cuda_device_count())"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=20, text=True)
            gpu = "yes" if result.stdout.strip() not in {"", "0"} else "no"
        except (OSError, subprocess.SubprocessError):
            gpu = "unknown"
    models = []
    folder = models_path(root)
    if folder.is_dir():
        models = sorted(entry.name for entry in folder.iterdir()
                        if entry.is_dir() and entry.name.startswith("models--"))
    return {"ready": bool(python), "interpreter": python, "gpu": gpu,
            "models": models, "available": available(root),
            "setup_available": setup_available(root)}


# ---------------------------------------------------------------------------
# jobs


def latest(root: Path) -> dict:
    folder = job_dir(root)
    if not folder.is_dir():
        return {}
    records = sorted(folder.glob("*.json"),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    for path in records:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id"):
            return data
    return {}


def _save(root: Path, job: dict) -> None:
    folder = job_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{job['id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    tmp.replace(path)


def _alive(pid: int) -> bool:
    """A pid is alive unless /proc says it is a zombie (no waitpid needed)."""
    return procs.alive(pid)


def _pick(root: Path, job_id: str = "") -> dict:
    """One recorded job: the named one, else the newest live one, else latest."""
    if job_id:
        try:
            return json.loads((job_dir(root) / f"{job_id}.json")
                              .read_text("utf-8"))
        except (OSError, ValueError):
            return {}
    return _running(root) or latest(root)


def _running(root: Path) -> dict:
    job = latest(root)
    if job and job.get("state") in {"running", "paused"}:
        if _alive(int(job.get("pid") or 0)):
            return job
        _finish(root, job)
        return {}
    return {}


def _finish(root: Path, job: dict) -> dict:
    """Adopt a child that ended while nobody was watching."""
    folder = job_dir(root)
    rc_file = folder / f"{job['id']}.rc"
    try:
        code = int(rc_file.read_text("utf-8").strip() or 0)
    except (OSError, ValueError):
        code = 0
    job["state"] = "done" if code == 0 else "failed"
    job["finished"] = job.get("finished") or _now()
    job["rc"] = code
    job["percent"] = 100.0 if code == 0 else job.get("percent", 0.0)
    _save(root, job)
    return job


def _steps(root: Path, *, rel: str, refresh: bool, missing: int,
           model: str, language: str) -> list[tuple[str, list[str]]]:
    folder = scripts(root)
    python = interpreter(root)
    argv = [python, "-u", str(folder / SCRIPT)]
    if rel:
        argv += ["--rel", rel]
        label = f"transcribing {Path(rel).name}"
    else:
        argv += ["--missing"]
        if missing:
            argv += ["--limit", str(int(missing))]
        label = "transcribing the next songs without lyrics"
    if refresh:
        argv.append("--refresh")
    if model:
        argv += ["--model", str(model)]
    if language:
        argv += ["--language", str(language)]
    return [(label, argv)]


def start(root: Path, *, rel: str = "", refresh: bool = False,
          missing: int = 0, model: str = "", language: str = "") -> dict:
    """Queue one transcription; returns the job record (running or reused)."""
    root = Path(root)
    if not available(root):
        raise RuntimeError("the library has no scripts/transcribe_lyrics.py")
    running = _running(root)
    if running:
        return running
    python = interpreter(root)
    if not python:
        raise RuntimeError(
            "no local transcriber is installed — run scripts/setup_asr.sh")

    steps = _steps(root, rel=rel, refresh=refresh, missing=missing,
                   model=model, language=language)
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = job_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / f"{job_id}.log"
    rc_file = folder / f"{job_id}.rc"
    job = {
        "id": job_id,
        "kind": "transcribe",
        "state": "running",
        "started": _now(),
        "finished": "",
        "pid": 0,
        "root": str(root),
        "rel": rel,
        "refresh": bool(refresh),
        "missing": int(missing or 0),
        "percent": 0.0,
        "step": steps[0][0],
        "log": str(log.relative_to(root)),
        "command": [" ".join(argv) for _, argv in steps],
    }
    _save(root, job)
    with log.open("wb") as handle:
        handle.write(f"[lyrics] job {job_id} in {root}\n".encode())
        handle.flush()
        process = subprocess.Popen(
            steps[0][1], cwd=str(root), stdin=subprocess.DEVNULL,
            stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    job["pid"] = process.pid
    _save(root, job)
    return job


def start_setup(root: Path, *, model: str = "", cpu: bool = False) -> dict:
    """Install the local backend (`scripts/setup_asr.sh`) as a background job."""
    root = Path(root)
    if not setup_available(root):
        raise RuntimeError("the library has no scripts/setup_asr.sh")
    running = _running(root)
    if running:
        return running
    script = scripts(root) / SETUP
    bash = shutil.which("bash") or "/bin/bash"
    argv = [bash, str(script)]
    if model:
        argv += ["--model", model]
    if cpu:
        argv.append("--cpu")
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = job_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / f"{job_id}.log"
    job = {"id": job_id, "kind": "setup", "state": "running",
           "started": _now(), "finished": "", "pid": 0, "root": str(root),
           "rel": "", "percent": 0.0, "step": "installing the local backend",
           "log": str(log.relative_to(root)), "command": [" ".join(argv)]}
    _save(root, job)
    with log.open("wb") as handle:
        handle.write(f"[lyrics] installing the transcriber in {root}\n".encode())
        handle.flush()
        process = subprocess.Popen(
            argv, cwd=str(root), stdin=subprocess.DEVNULL, stdout=handle,
            stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    job["pid"] = process.pid
    _save(root, job)
    return job


def status(root: Path, job_id: str = "") -> dict:
    """The job record plus parsed progress and the tail of its log."""
    root = Path(root)
    job = latest(root)
    if not job:
        return {"state": "idle", "running": False, "active": False,
                "paused": False, "percent": 0.0,
                "step": "", "done": 0, "total": 0, "tail": []}
    if job_id and job.get("id") != job_id:
        path = job_dir(root) / f"{job_id}.json"
        try:
            job = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            pass
    stored = str(job.get("state") or "")
    live = procs.process_state(int(job.get("pid") or 0))
    if stored in {"running", "paused"}:
        if live == "paused":
            job["state"] = "paused"
        elif live == "running":
            job["state"] = "running"
        else:
            job = _finish(root, job)
    tail = _tail(root, job)
    percent = float(job.get("percent") or 0.0)
    for line in tail[-80:]:
        match = _RE_PERCENT.search(line)
        if match:
            percent = float(match.group(1))
        if _RE_DONE.search(line):
            percent = 100.0
    job.update(percent=round(percent, 1),
               running=job.get("state") == "running",
               paused=job.get("state") == "paused",
               active=job.get("state") in {"running", "paused"},
               tail=tail[-12:])
    return job


def pause(root: Path, job_id: str = "") -> dict:
    """Freeze the transcription where it is (`SIGSTOP`)."""
    root = Path(root)
    job = _pick(root, job_id)
    if not job:
        return status(root, job_id)
    pid = int(job.get("pid") or 0)
    if procs.process_state(pid) != "running":
        return {**status(root, str(job["id"])),
                "note": "Nothing to pause: no transcription is running."}
    if not procs.pause(pid):
        return {**status(root, str(job["id"])),
                "error": "The transcription process could not be stopped."}
    job["state"] = "paused"
    job["paused_at"] = _now()
    _save(root, job)
    return status(root, str(job["id"]))


def resume(root: Path, job_id: str = "") -> dict:
    """Continue a frozen transcription from where it stopped."""
    root = Path(root)
    job = _pick(root, job_id)
    if not job:
        return status(root, job_id)
    pid = int(job.get("pid") or 0)
    if procs.process_state(pid) != "paused":
        return {**status(root, str(job["id"])),
                "note": "Nothing to resume: the transcription is not paused."}
    if not procs.resume(pid):
        return {**status(root, str(job["id"])),
                "error": "The transcription process could not be continued."}
    job["state"] = "running"
    _save(root, job)
    return status(root, str(job["id"]))


def _tail(root: Path, job: dict, limit: int = 120) -> list[str]:
    path = Path(root) / (job.get("log") or "")
    if not path.is_file():
        return []
    try:
        raw = path.read_text("utf-8", "replace").splitlines()
    except OSError:
        return []
    return raw[-limit:]


def counts(root: Path) -> dict:
    """How much of the library has lyrics (read-only, tolerant)."""
    path = db_path(root)
    result = {"exists": path.is_file(), "tracks": 0, "lines": 0,
              "audio": 0, "last": ""}
    if not path.is_file():
        return result
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS tracks, COALESCE(SUM(line_count), 0) AS "
                "lines, MAX(transcribed_at) AS last FROM track").fetchone()
            result.update(tracks=int(row[0] or 0), lines=int(row[1] or 0),
                          last=row[2] or "")
        finally:
            conn.close()
    except Exception:                    # a locked or older DB is not an error
        pass
    inventory = Path(root) / "library-work" / "inventory" / "files.jsonl"
    if inventory.is_file():
        # the extension is an inventory field, not the line's last key, so match
        # `"ext": ".mp3"` rather than the end of the record
        suffixes = (b'"ext": ".mp3"', b'"ext": ".mp2"', b'"ext": ".m4a"',
                    b'"ext": ".mp4"', b'"ext": ".flac"', b'"ext": ".ogg"',
                    b'"ext": ".oga"', b'"ext": ".opus"', b'"ext": ".wav"',
                    b'"ext": ".wma"', b'"ext": ".aac"', b'"ext": ".webm"')
        try:
            with inventory.open("rb") as handle:
                audio = 0
                for raw in handle:
                    if b'"rel_path"' in raw and any(s in raw for s in suffixes):
                        audio += 1
            result["audio"] = audio
        except OSError:
            pass
    return result
