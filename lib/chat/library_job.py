"""Back up the app's own library data once a day, from inside the app.

The sound profile and the lyric lines are hours of measuring and
transcription and no music, so they are worth a plain-text copy that survives
a lost database, a moved file or another machine.  The work itself is done by
the library's own tool, `scripts/library_data.py`, which this module runs as
one detached background job:

    library_data.py backup --keep N --workers N
    library_data.py import FILE [--dry-run] [--overwrite]

The backup fingerprints what is new or changed (the audio payload only, so a
renamed or retagged file keeps its fingerprint), writes

    <root>/LibraryData/library-data-YYYY-MM-DD.jsonl

and prunes exports older than `keep` days (7 by default).  An import reads
such a file back into the databases and matches every record to the local
file **by fingerprint**, never by the stored path.

One job runs at a time; a second request returns the running one.  The child
writes `library-work/app/library-data/jobs/<id>.{log,rc,runner.py}` and the
record `<id>.json` is written here before the process starts, so a page reload
— or a restarted app — still shows a running backup.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import procs

TOOL = "library_data.py"

#: how many daily exports the backup keeps when nothing says otherwise
DEFAULT_KEEP = 7

_RE_COUNT = re.compile(r"fingerprinted\s+([\d,]+)/([\d,]+)")
_RE_STEP = re.compile(r"^\[library-data\]\s*step:\s*(.+)$")
_RE_NOTE = re.compile(r"^\[library-data\]\s*(?!step:|fingerprinted)(.+)$")


def base(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "library-data"


def job_dir(root: Path) -> Path:
    return base(root) / "jobs"


def fp_db(root: Path) -> Path:
    return base(root) / "fingerprints.db"


def export_dir(root: Path) -> Path:
    return Path(root) / "LibraryData"


def scripts(root: Path) -> Path:
    root = Path(root)
    for candidate in (root / "scripts", root.parent / "scripts"):
        if (candidate / TOOL).is_file():
            return candidate
    return root / "scripts"


def available(root: Path) -> bool:
    return (scripts(root) / TOOL).is_file()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(text: str) -> int:
    return int(str(text).replace(",", ""))


# ---------------------------------------------------------------------------
# what the index and the exports hold (read-only, cheap enough to poll)


def exports(root: Path) -> list[dict]:
    folder = export_dir(root)
    try:
        files = sorted(folder.glob("library-data-*.jsonl"))
    except OSError:
        return []
    found = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append({"name": path.name,
                      "day": path.stem.replace("library-data-", ""),
                      "bytes": stat.st_size,
                      "mtime": datetime.fromtimestamp(
                          stat.st_mtime).isoformat(timespec="seconds")})
    return found


def _sqlite_count(path: Path, sql: str) -> int:
    if not path.is_file():
        return 0
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            return int(conn.execute(sql).fetchone()[0] or 0)
        finally:
            conn.close()
    except Exception:                 # a locked or older DB is not an error
        return 0


def counts(root: Path) -> dict:
    """The index, the exports and the two databases, without walking audio."""
    root = Path(root)
    found = exports(root)
    latest = found[-1] if found else {}
    fingerprint = {"known": 0, "without": 0, "at": ""}
    path = fp_db(root)
    if path.is_file():
        fingerprint["known"] = _sqlite_count(
            path, "SELECT COUNT(*) FROM file WHERE audio_fp IS NOT NULL")
        fingerprint["without"] = _sqlite_count(
            path, "SELECT COUNT(*) FROM file WHERE audio_fp IS NULL")
        if path.is_file():
            try:
                import sqlite3
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                       timeout=10)
                try:
                    row = conn.execute(
                        "SELECT value FROM meta WHERE key='fingerprinted_at'"
                    ).fetchone()
                    fingerprint["at"] = (row[0] if row else "") or ""
                finally:
                    conn.close()
            except Exception:
                pass
    return {
        "available": available(root),
        "export_dir": str(export_dir(root)),
        "exports": len(found),
        "latest": latest,
        "due": latest.get("day") != date.today().isoformat(),
        "fingerprints": fingerprint,
        "psycho": _sqlite_count(
            root / "library-work" / "app" / "psycho" / "features.db",
            "SELECT COUNT(*) FROM audio"),
        "lyrics": _sqlite_count(
            root / "library-work" / "app" / "lyrics" / "lyrics.db",
            "SELECT COUNT(*) FROM track"),
    }


def due(root: Path) -> bool:
    """Is today's export still missing?"""
    found = exports(root)
    return (found[-1]["day"] if found else "") != date.today().isoformat()


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
    if job_id:
        return _load(root, job_id)
    return _running(root) or latest(root)


def _alive(pid: int) -> bool:
    return procs.alive(pid)


RUNNER = '''"""Run one library-data command, nothing else."""
import subprocess
import sys
from pathlib import Path

argv = {argv!r}
rc_file = Path({rc!r})

code = subprocess.call(argv)
rc_file.write_text(str(code), encoding="utf-8")
sys.exit(code)
'''


def _start(root: Path, argv: list[str], *, mode: str, detail: dict) -> dict:
    root = Path(root)
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = job_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / f"{job_id}.log"
    rc_file = folder / f"{job_id}.rc"
    runner = folder / f"{job_id}.runner.py"
    runner.write_text(RUNNER.format(argv=argv, rc=str(rc_file)),
                      encoding="utf-8")
    job = {
        "id": job_id,
        "mode": mode,
        "state": "running",
        "started": _now(),
        "finished": "",
        "pid": 0,
        "root": str(root),
        "log": os.path.relpath(str(log), str(root)),
        "command": " ".join(argv),
        **detail,
    }
    _save(root, job)
    with log.open("wb") as handle:
        handle.write(f"[library-data] {mode} in {root}\n".encode())
        handle.flush()
        process = subprocess.Popen(
            [sys.executable, "-u", str(runner)],
            cwd=str(root), stdin=subprocess.DEVNULL, stdout=handle,
            stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    job["pid"] = process.pid
    _save(root, job)
    return job


def start(root: Path, *, keep: int = DEFAULT_KEEP, workers: int = 0) -> dict:
    """Queue one daily backup; returns the job record (running or reused)."""
    root = Path(root)
    if not available(root):
        raise RuntimeError("the library has no scripts/library_data.py")
    running = _running(root)
    if running:
        return running
    keep = max(1, int(keep or DEFAULT_KEEP))
    workers = int(workers) if workers else min(6, os.cpu_count() or 2)
    argv = [sys.executable, "-u", str(scripts(root) / TOOL), "backup",
            "--keep", str(keep), "--workers", str(workers)]
    return _start(root, argv, mode="backup",
                  detail={"keep": keep, "workers": workers})


def start_import(root: Path, file: str, *, overwrite: bool = False,
                 dry_run: bool = False) -> dict:
    """Queue an import of one export file, matched by fingerprint."""
    root = Path(root)
    if not available(root):
        raise RuntimeError("the library has no scripts/library_data.py")
    source = Path(file).expanduser()
    if not source.is_absolute():
        source = (Path(root) / source).resolve()
    if not source.is_file():
        raise ValueError(f"no such export file: {file}")
    running = _running(root)
    if running:
        return running
    argv = [sys.executable, "-u", str(scripts(root) / TOOL), "import",
            str(source)]
    if overwrite:
        argv.append("--overwrite")
    if dry_run:
        argv.append("--dry-run")
    return _start(root, argv, mode="import",
                  detail={"file": str(source), "overwrite": bool(overwrite),
                          "dry_run": bool(dry_run)})


def preview(root: Path, file: str, *, timeout: float = 180.0) -> dict:
    """A dry-run import: what a file would fill in, writing nothing.

    Runs in the caller because it only reads (and fingerprints a renamed file
    the index has not seen yet), so the answer is immediate once the daily
    backup has fingerprinted the library.
    """
    root = Path(root)
    if not available(root):
        return {"error": "This library has no scripts/library_data.py."}
    source = Path(str(file or "")).expanduser()
    if not str(file or "").strip():
        return {"error": "Name an export file first, e.g. "
                         "LibraryData/library-data-2026-11-22.jsonl."}
    if not source.is_absolute():
        source = (root / source).resolve()
    if not source.is_file():
        return {"error": f"no such export file: {file}"}
    argv = [sys.executable, "-u", str(scripts(root) / TOOL), "import",
            str(source), "--dry-run", "--json"]
    try:
        proc = subprocess.run(argv, cwd=str(root), capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"the check did not finish: {exc}"}
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {"error": reason[-1][:400] if reason else
                f"library_data.py exited {proc.returncode}"}
    try:
        return json.loads(proc.stdout or "{}")
    except ValueError:
        return {"error": "the check printed no JSON",
                "output": (proc.stdout or "")[-500:]}


def _running(root: Path) -> dict:
    job = latest(root)
    pid = int(job.get("pid") or 0) if job else 0
    if job and job.get("state") in {"running", "paused"} and _alive(pid):
        return job
    if job and job.get("state") in {"running", "paused"}:
        procs.reap(pid)
    return {}


def status(root: Path, job_id: str = "") -> dict:
    """The job record plus parsed progress and the tail of its log."""
    root = Path(root)
    job = _load(root, job_id) if job_id else latest(root)
    if not job:
        return {"state": "idle", "running": False, "active": False,
                "paused": False, "percent": 0.0, "mode": "",
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
            procs.reap(pid)
        state = "done" if rc == 0 else "failed"
        if stored in {"running", "paused"} and rc is None:
            state = "failed"
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
    root = Path(root)
    job = _pick(root, job_id)
    if not job:
        return status(root, job_id)
    pid = int(job.get("pid") or 0)
    if procs.process_state(pid) != "running":
        return {**status(root, str(job["id"])),
                "note": "Nothing to pause: the backup is not running."}
    if not procs.pause(pid):
        return {**status(root, str(job["id"])),
                "error": "The backup process could not be stopped."}
    job["state"] = "paused"
    job["paused_at"] = _now()
    _save(root, job)
    return status(root, str(job["id"]))


def resume(root: Path, job_id: str = "") -> dict:
    root = Path(root)
    job = _pick(root, job_id)
    if not job:
        return status(root, job_id)
    pid = int(job.get("pid") or 0)
    if procs.process_state(pid) != "paused":
        return {**status(root, str(job["id"])),
                "note": "Nothing to resume: the backup is not paused."}
    if not procs.resume(pid):
        return {**status(root, str(job["id"])),
                "error": "The backup process could not be continued."}
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
    done = total = 0
    step = ("importing" if job.get("mode") == "import" else "fingerprinting")
    notes = []
    for line in lines:
        counted = _RE_COUNT.search(line)
        if counted:
            done, total = _number(counted.group(1)), _number(counted.group(2))
            step = "fingerprinting"
            continue
        marker = _RE_STEP.match(line)
        if marker:
            step = marker.group(1)
            continue
        note = _RE_NOTE.match(line)
        if note:
            notes.append(note.group(1).strip())
    if job.get("state") == "done":
        step = "finished"
        done = total or done
    percent = (90.0 * done / total) if total else 0.0
    if step == "exporting":
        percent = 95.0
    if job.get("state") == "done":
        percent = 100.0
    started = job.get("started") or ""
    elapsed = 0.0
    try:
        began = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = (datetime.fromisoformat(job["finished"].replace("Z", "+00:00"))
               if job.get("finished") else datetime.now(timezone.utc))
        elapsed = max(0.0, (end - began).total_seconds())
    except (ValueError, TypeError, KeyError):
        pass
    return {"step": step, "done": done, "total": total,
            "percent": round(min(100.0, percent), 1),
            "elapsed": round(elapsed, 1), "lines": lines[-10:],
            "notes": notes[-3:]}


def autostart(root: Path, enabled: bool = True,
              keep: int = DEFAULT_KEEP) -> dict:
    """Start today's backup on app start when it is missing.

    A running job is returned unchanged; `{}` means "nothing to do".
    """
    if not enabled or not available(root):
        return {}
    running = _running(root)
    if running:
        return running
    if not due(root):
        return {}
    try:
        return start(root, keep=keep)
    except (OSError, RuntimeError):
        return {}
