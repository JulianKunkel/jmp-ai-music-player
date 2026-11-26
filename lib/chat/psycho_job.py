"""Measure the library's sound profile from inside the app.

The app only ever *reads* `library-work/app/psycho/features.db`.  The measuring
is done by the library's own tools (`scripts/psycho_features.py` and
`scripts/psycho_profile.py`), which this module runs as one detached background
job:

    psycho_features.py --apply [--refresh] --workers N
    psycho_profile.py --apply

Only one job runs at a time; a second request returns the running one.  The
child writes its output to

    library-work/app/psycho/jobs/<id>.log

and its exit code to `<id>.rc`, while the job record `<id>.json` is written
here before the process starts.  The window polls :func:`status`, which reads
the record, the log and the pid — so a page reload, or even a restarted app,
still shows a running measurement.

Nothing here touches a music file: the scripts only read audio and write the
database, the generated `Playlists/Psycho/` lists and their reports.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import procs

#: how the measurement is allowed to look at the library
FEATURES = "psycho_features.py"
PROFILE = "psycho_profile.py"

#: progress lines the extractor prints
_RE_TODO = re.compile(r"to measure:\s*([\d,]+)\s+workers:\s*(\d+)")
_RE_DONE = re.compile(r"([\d,]+)/([\d,]+)\s+measured,\s*([\d,]+)\s+errors")
_RE_STEP = re.compile(r"^\[psycho\]\s*(.+)$")


def job_dir(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "psycho" / "jobs"


def db_path(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "psycho" / "features.db"


def scripts(root: Path) -> Path:
    """The library's `scripts/` folder, or a sensible guess."""
    root = Path(root)
    for candidate in (root / "scripts", root.parent / "scripts"):
        if (candidate / FEATURES).is_file():
            return candidate
    return root / "scripts"


def available(root: Path) -> bool:
    folder = scripts(root)
    return (folder / FEATURES).is_file() and (folder / PROFILE).is_file()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(text: str) -> int:
    return int(str(text).replace(",", ""))


# ---------------------------------------------------------------------------
# the database tells us whether there is anything to do


#: the last counts() answer, keyed by the database stamp, the inventory stamp
#: and the measurable count.  Three aggregate queries over 13k rows cost
#: ~15 ms and the footer asks every few seconds.
_COUNTS: dict[tuple, dict] = {}

#: the formats `scripts/psycho_features.py` can read (`AUDIO_EXT` there).  A
#: chiptune has no such extension, so it can never get a profile.
MEASURABLE_EXT = {".mp3", ".mp2", ".m4a", ".opus", ".ogg", ".flac", ".wav",
                  ".webm"}

#: how many files in the live inventory could be measured, keyed by the
#: inventory stamp alone — the 13k stat() calls happen once per index change,
#: not once per poll (the database stamp moves while a job writes).
_KNOWN: dict[tuple, frozenset] = {}

#: how many of those have no row yet, with the time it was worked out.  The
#: set difference costs ~120 ms (13k paths out of SQLite), so a positive answer
#: is refreshed at most every `UNMEASURED_TTL` seconds; a zero is final until
#: the inventory changes, which keeps a finished library free.
_UNMEASURED: dict[tuple, tuple[float, int]] = {}
UNMEASURED_TTL = 30.0


def _stamp(path: Path) -> tuple:
    try:
        info = path.stat()
    except OSError:
        return (str(path), 0, 0)
    return (str(path), info.st_size, info.st_mtime_ns)


def inventory_path(root: Path) -> Path:
    return Path(root) / "library-work" / "inventory" / "files.jsonl"


def _library_rel(path: str) -> str:
    text = str(path or "").replace("\\", "/").lstrip("/")
    return text[len("Music/"):] if text.startswith("Music/") else text


def measurable(root: Path) -> frozenset[str]:
    """The library-relative paths the analyser could measure right now.

    `counts()` only knows the rows already in the database, so a track that
    arrived after the last run (a fresh import) is invisible to it and the
    footer offered nothing to do.  This walks the inventory the same way
    `psycho_features.py` does — a readable format that is still on disk.
    """
    root = Path(root)
    path = inventory_path(root)
    stamp = _stamp(path)
    cached = _KNOWN.get(stamp)
    if cached is not None:
        return cached
    found: set[str] = set()
    if path.is_file():
        try:
            raw = path.read_bytes()
        except OSError:
            raw = b""
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (row.get("ext") or "").lower() not in MEASURABLE_EXT:
                continue
            rel = _library_rel(row.get("rel_path"))
            if rel and (root / rel).is_file():
                found.add(rel)
    answer = frozenset(found)
    _KNOWN.clear()
    _KNOWN[stamp] = answer
    return answer


def known(root: Path) -> int:
    """How many files the analyser could measure (see :func:`measurable`)."""
    return len(measurable(root))


def _unmeasured(root: Path, paths: frozenset, db_stamp: tuple) -> int:
    """How many measurable files have no row in the database yet.

    The set difference is ~120 ms on this library, so it is not recomputed on
    every poll: a zero is final until the inventory or the database changes
    (a finished job moves the database stamp), while a positive answer is
    refreshed at most every `UNMEASURED_TTL` seconds — a job that is running
    has already moved the stamp, so the count drops as soon as it writes.
    """
    key = (_stamp(inventory_path(root)), db_stamp, len(paths))
    now = time.monotonic()
    cached = _UNMEASURED.get(key)
    if cached is not None:
        when, value = cached
        if value == 0 or now - when < UNMEASURED_TTL:
            return value
    value = len(paths)
    if paths:
        path = db_path(root)
        if path.is_file():
            try:
                import sqlite3
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    have = {item[0] for item in
                            conn.execute("SELECT rel_path FROM audio")}
                    value = len(paths - have)
                finally:
                    conn.close()
            except Exception:            # a locked DB must not invent work
                value = 0
    if len(_UNMEASURED) > 8:
        _UNMEASURED.clear()
    _UNMEASURED[key] = (now, value)
    return value


def counts(root: Path) -> dict:
    """Rows in the profile database, read-only and tolerant of a missing DB.

    `known` is how many files the library *could* measure (see :func:`known`)
    and `unmeasured` how many of them have no row yet — the count a freshly
    imported track adds, which the row totals alone cannot show.  Rows for
    files that are gone from the tree are not work, so the two sets are
    compared instead of the counts.
    """
    result = {"total": 0, "measured": 0, "detailed": 0, "profiles": 0,
              "families": 0, "failed": 0, "measured_at": "", "exists": False,
              "known": 0, "unmeasured": 0}
    path = db_path(root)
    paths = measurable(root)
    result["known"] = len(paths)
    db_stamp = (_stamp(path), _stamp(Path(str(path) + "-wal")))
    stamp = (db_stamp, _stamp(inventory_path(root)), len(paths))
    cached = _COUNTS.get(stamp)
    if cached is not None:
        result = dict(cached)
    elif not path.is_file():
        _COUNTS.clear()
        _COUNTS[stamp] = dict(result)
    else:
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n, "
                    "SUM(CASE WHEN source='analysis' THEN 1 ELSE 0 END) "
                    "AS measured, "
                    "SUM(CASE WHEN lufs IS NOT NULL AND sharpness_acum IS NOT "
                    "NULL THEN 1 ELSE 0 END) AS detailed, "
                    "SUM(CASE WHEN source='analysis' AND lufs IS NULL "
                    "THEN 1 ELSE 0 END) AS failed "
                    "FROM audio").fetchone()
                result.update(total=row[0] or 0, measured=row[1] or 0,
                              detailed=row[2] or 0, failed=row[3] or 0,
                              exists=True)
                result["profiles"] = conn.execute(
                    "SELECT COUNT(*) FROM profile").fetchone()[0] or 0
                result["families"] = conn.execute(
                    "SELECT COUNT(*) FROM family").fetchone()[0] or 0
                meta = conn.execute(
                    "SELECT value FROM meta WHERE key='measured_at'").fetchone()
                result["measured_at"] = (meta[0] if meta else "") or ""
            finally:
                conn.close()
        except Exception:               # a locked or older DB is not an error
            pass
        _COUNTS.clear()
        _COUNTS[stamp] = dict(result)
    result["unmeasured"] = _unmeasured(root, paths, db_stamp)
    return result


def needed(root: Path) -> str:
    """Why a measurement should run now ("" when everything is in place).

    A file that was decoded and failed is *not* work: `psycho_features.py`
    stores such a row with `source='analysis'` and empty measurements, so a
    second pass would fail on exactly the same files.  Those stay visible in
    :func:`counts` as `failed`, and the job does not start for them alone.

    A file with **no row at all** is work: that is a track imported after the
    last run, and the row totals alone would never show it.
    """
    if not available(root):
        return "the library has no scripts/psycho_features.py"
    state = counts(root)
    if state.get("unmeasured"):
        missing = int(state["unmeasured"])
        return (f"{missing:,} track has never been measured" if missing == 1
                else f"{missing:,} tracks have never been measured")
    if not state["exists"]:
        return "there is no profile database yet"
    if state["measured"] < state["total"]:
        missing = state["total"] - state["measured"]
        return f"{missing:,} tracks are still estimated"
    broken = int(state["failed"] or 0)
    if state["detailed"] < state["measured"] - broken:
        missing = state["measured"] - broken - state["detailed"]
        return f"{missing:,} tracks lack the newer measurements"
    if state["profiles"] < state["measured"] - broken:
        missing = state["measured"] - broken - state["profiles"]
        return f"{missing:,} tracks have no profile axes yet"
    return ""


# ---------------------------------------------------------------------------
# the job


def _record(root: Path, job_id: str) -> Path:
    return job_dir(root) / f"{job_id}.json"


def _load(root: Path, job_id: str) -> dict:
    try:
        data = json.loads(_record(root, job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(root: Path, job: dict) -> None:
    folder = job_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    target = _record(root, str(job["id"]))
    tmp = target.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(job, handle, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def latest(root: Path) -> dict:
    """The newest job record, running or not ({} when there is none)."""
    folder = job_dir(root)
    try:
        files = sorted(folder.glob("*.json"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return {}
    for path in files[:10]:
        job = _load(root, path.stem)
        if job:
            return job
    return {}


def _pick(root: Path, job_id: str = "") -> dict:
    """One recorded job: the named one, else the newest live one, else latest."""
    if job_id:
        return _load(root, job_id)
    return _running(root) or latest(root)


def _alive(pid: int) -> bool:
    """Is that pid a live process (running or paused)?

    The job is our own child and the app never waits for it, so a finished
    measurement stays a zombie until it is reaped.  `os.kill(pid, 0)` still
    succeeds for a zombie, which would make a finished job look like a running
    one forever — so :mod:`procs` reads the process state instead.
    """
    return procs.alive(pid)


def _reap(pid: int) -> None:
    """Collect a finished child so it does not stay a zombie."""
    procs.reap(pid)


RUNNER = '''"""Run the library's profile scripts one after the other."""
import subprocess
import sys
from pathlib import Path

steps = {steps!r}
rc_file = Path({rc!r})

for label, argv in steps:
    print("[psycho] " + label, flush=True)
    code = subprocess.call(argv)
    if code:
        print("[psycho] " + label + " failed with exit " + str(code),
              flush=True)
        rc_file.write_text(str(code), encoding="utf-8")
        sys.exit(code)

rc_file.write_text("0", encoding="utf-8")
print("[psycho] done", flush=True)
'''


def start(root: Path, *, refresh: bool = False, workers: int = 0) -> dict:
    """Queue one measurement; returns the job record (running or reused)."""
    root = Path(root)
    if not available(root):
        raise RuntimeError("the library has no scripts/psycho_features.py")
    running = _running(root)
    if running:
        return running

    folder = scripts(root)
    workers = int(workers) if workers else min(6, os.cpu_count() or 2)
    steps = [
        ("measuring the audio", [sys.executable, "-u",
                                 str(folder / FEATURES), "--apply",
                                 "--workers", str(workers)]
         + (["--refresh"] if refresh else [])),
        ("rebuilding the profile axes and families",
         [sys.executable, "-u", str(folder / PROFILE), "--apply"]),
    ]

    job_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    job_dir(root).mkdir(parents=True, exist_ok=True)
    log = job_dir(root) / f"{job_id}.log"
    rc_file = job_dir(root) / f"{job_id}.rc"
    runner = job_dir(root) / f"{job_id}.runner.py"
    runner.write_text(RUNNER.format(
        steps=[(label, argv) for label, argv in steps],
        rc=str(rc_file)), encoding="utf-8")

    job = {
        "id": job_id,
        "state": "running",
        "started": _now(),
        "finished": "",
        "pid": 0,
        "root": str(root),
        "refresh": bool(refresh),
        "workers": workers,
        "steps": [label for label, _ in steps],
        "log": str(log.relative_to(root)),
        "command": [" ".join(argv) for _, argv in steps],
    }
    _save(root, job)

    with log.open("wb") as handle:
        handle.write(f"[psycho] measuring the library in {root}\n".encode())
        handle.flush()
        process = subprocess.Popen(
            [sys.executable, "-u", str(runner)],
            cwd=str(root), stdin=subprocess.DEVNULL, stdout=handle,
            stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    job["pid"] = process.pid
    _save(root, job)
    return job


def _running(root: Path) -> dict:
    """The job that is running or paused right now ({} when there is none)."""
    job = latest(root)
    pid = int(job.get("pid") or 0) if job else 0
    if job and job.get("state") in {"running", "paused"} and _alive(pid):
        return job
    if job and job.get("state") in {"running", "paused"}:
        _reap(pid)
    return {}


def status(root: Path, job_id: str = "") -> dict:
    """The job record plus parsed progress and the tail of its log.

    The process decides the state, not the stored record: a record still says
    `running` after the child died, and a pause (`SIGSTOP`) is invisible in
    the record until something observes it.
    """
    root = Path(root)
    job = _load(root, job_id) if job_id else latest(root)
    if not job:
        return {"state": "idle", "running": False, "active": False,
                "paused": False, "progress": 0.0,
                "lines": [], "exists": False, "available": available(root),
                "counts": counts(root)}
    pid = int(job.get("pid") or 0)
    stored = str(job.get("state") or "")
    live = procs.process_state(pid) if pid else "dead"
    rc = None
    rc_file = job_dir(root) / f"{job['id']}.rc"
    try:
        rc = int(rc_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        rc = None
    if live in {"running", "paused"} and stored in {"running", "paused"}:
        state = "paused" if live == "paused" else "running"
    else:
        if pid and stored in {"running", "paused"}:
            _reap(pid)
        state = "done" if rc == 0 else "failed"
        if stored in {"running", "paused"} and rc is None:
            state = "failed"              # killed before it could report
    if state != stored:
        job["state"] = state
        if state not in {"running", "paused"} and not job.get("finished"):
            job["finished"] = _now()
        _save(root, job)
    return {**job, "running": state == "running",
            "paused": state == "paused",
            "active": state in {"running", "paused"},
            "returncode": rc, "available": available(root),
            "counts": counts(root), **_progress(root, job)}


def pause(root: Path, job_id: str = "") -> dict:
    """Freeze the measurement where it is (`SIGSTOP`)."""
    root = Path(root)
    job = _pick(root, job_id)
    if not job:
        return status(root, job_id)
    pid = int(job.get("pid") or 0)
    if procs.process_state(pid) != "running":
        return {**status(root, str(job["id"])),
                "note": "Nothing to pause: the measurement is not running."}
    if not procs.pause(pid):
        return {**status(root, str(job["id"])),
                "error": "The measuring process could not be stopped."}
    job["state"] = "paused"
    job["paused_at"] = _now()
    _save(root, job)
    return status(root, str(job["id"]))


def resume(root: Path, job_id: str = "") -> dict:
    """Continue a frozen measurement from exactly where it stopped."""
    root = Path(root)
    job = _pick(root, job_id)
    if not job:
        return status(root, job_id)
    pid = int(job.get("pid") or 0)
    if procs.process_state(pid) != "paused":
        return {**status(root, str(job["id"])),
                "note": "Nothing to resume: the measurement is not paused."}
    if not procs.resume(pid):
        return {**status(root, str(job["id"])),
                "error": "The measuring process could not be continued."}
    job["state"] = "running"
    _save(root, job)
    return status(root, str(job["id"]))


def _progress(root: Path, job: dict) -> dict:
    path = Path(job.get("log") or "")
    if not path.is_absolute():
        path = Path(root) / str(job.get("log") or "")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    done = total = errors = 0
    step = job.get("steps", [""])[0] if job.get("steps") else ""
    for line in lines:
        todo = _RE_TODO.search(line)
        if todo:
            total = _number(todo.group(1))
            step = "measuring the audio"
            continue
        counted = _RE_DONE.search(line)
        if counted:
            done, total = _number(counted.group(1)), _number(counted.group(2))
            errors = _number(counted.group(3))
            continue
        marker = _RE_STEP.match(line)
        if marker:
            step = marker.group(1)
    if "done" in {line.lower().replace("[psycho] ", "").strip()
                  for line in lines}:
        done = total or done
        step = "finished"
    percent = (100.0 * done / total) if total else (
        100.0 if job.get("state") == "done" else 0.0)
    started = job.get("started") or ""
    elapsed = 0.0
    try:
        began = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = (datetime.fromisoformat(job["finished"].replace("Z", "+00:00"))
               if job.get("finished") else datetime.now(timezone.utc))
        elapsed = max(0.0, (end - began).total_seconds())
    except (ValueError, TypeError, KeyError):
        pass
    return {"step": step, "done": done, "total": total, "errors": errors,
            "percent": round(min(100.0, percent), 1),
            "elapsed": round(elapsed, 1), "lines": lines[-8:]}


def autostart(root: Path, enabled: bool = True) -> dict:
    """Start a measurement on app start when the profile is incomplete.

    A running job is returned unchanged; `{}` means "nothing to do".
    """
    if not enabled or not available(root):
        return {}
    running = _running(root)
    if running:
        return running
    if not needed(root):
        return {}
    try:
        return start(root)
    except (OSError, RuntimeError):
        return {}
