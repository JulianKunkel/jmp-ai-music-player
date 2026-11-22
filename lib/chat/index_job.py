"""Rebuild the library index from inside the app.

The app reads `library-work/inventory/files.jsonl` and the `Playlists/`
folders.  The indexing is done by the library's own pipeline, which this module
runs as one detached background job (after `chat/inventory.py` has dropped the
rows whose file changed, so the incremental scan re-reads them):

    phase0_inventory.py scan --work <root>/library-work
    phase0_inventory.py report --work <root>/library-work
    make_playlists.py --from-tree

Only one job runs at a time; a second request returns the running one.  The
child writes its output to

    library-work/app/index/jobs/<id>.log

and its exit code to `<id>.rc`, while the job record `<id>.json` is written
here before the process starts.  The window polls :func:`status`, which reads
the record, the log and the pid — so a page reload, or even a restarted app,
still shows a running index.

The scan is incremental: a file already in `files.jsonl` is skipped, so a
re-index normally only reads whatever is new.  Before the scan, `prepare_scan`
removes the rows whose size or mtime no longer matches the file, so a tag
edited outside the app is re-read instead of staying stale.  Nothing here
touches a music file: the scripts read tags and audio and write the inventory,
the reports and the canonical playlists.

A running job can be held and continued or stopped for good.  The child is its
own process group (`start_new_session=True`), so the controls are signals to
that group: `pause()` sends SIGSTOP and keeps the pid, the log and the place,
`resume()` sends SIGCONT, and `cancel()` continues a stopped child so it can
act on the SIGTERM that follows (and SIGKILL after `CANCEL_GRACE_S` when a stage
ignores it).  A cancelled run keeps every inventory row it had written; only a
half-written last line is trimmed, and the next run skips what is already there.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import inventory

#: the pipeline this module drives
INVENTORY = "phase0_inventory.py"
PLAYLISTS = "make_playlists.py"

#: the three stages, in the order the runner calls them
STEPS = (
    ("scanning the library", "scan"),
    ("writing the reports", "report"),
    ("rebuilding the playlists", "playlists"),
)

#: how long a cancelled job may take to honour SIGTERM before SIGKILL
CANCEL_GRACE_S = 15.0

_RE_TOTAL = re.compile(
    r"^\[scan\] total candidates=([\d,]+) already_done=([\d,]+) todo=([\d,]+)")
_RE_TICK = re.compile(r"^\[scan\] ([\d,]+)/([\d,]+)\s+[\d.]+ files/s")
_RE_FINISH = re.compile(
    r"^\[scan\] finished ([\d,]+) files in [\d.]+s, errors=(\d+)")
_RE_REPORT = re.compile(r"^\[report\] (.+)$")
_RE_STEP = re.compile(r"^\[index\] (.+)$")


def job_dir(root: Path) -> Path:
    return Path(root) / "library-work" / "app" / "index" / "jobs"


def inventory_path(root: Path) -> Path:
    return Path(root) / "library-work" / "inventory" / "files.jsonl"


def scripts(root: Path) -> Path:
    """The library's `scripts/` folder, or a sensible guess."""
    root = Path(root)
    for candidate in (root / "scripts", root.parent / "scripts"):
        if (candidate / INVENTORY).is_file():
            return candidate
    return root / "scripts"


def available(root: Path) -> bool:
    folder = scripts(root)
    return (folder / INVENTORY).is_file() and (folder / PLAYLISTS).is_file()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(text: str) -> int:
    return int(str(text).replace(",", ""))


# ---------------------------------------------------------------------------
# what the library holds right now

#: a cached scan of files.jsonl, keyed by (path, size, mtime)
_COUNT_CACHE: dict[tuple[str, int, int], dict] = {}


def counts(root: Path) -> dict:
    """How many tracks the inventory lists, and when it was written."""
    root = Path(root)
    result = {"tracks": 0, "playlists": 0, "indexed_at": "", "exists": False,
              "inventory": ""}
    path = inventory_path(root)
    # every list counts: the shelf lists sit on the top level, the mood, focus
    # and sound-family lists in subfolders, hand-made ones under Custom/
    playlists = list((root / "Playlists").rglob("*.m3u8"))
    result["playlists"] = len(playlists)
    try:
        stat = path.stat()
    except OSError:
        return result
    key = (str(path), stat.st_size, int(stat.st_mtime))
    cached = _COUNT_CACHE.get(key)
    if cached is None:
        tracks = 0
        try:
            with path.open("rb") as handle:
                for line in handle:
                    if line.strip():
                        tracks += 1
        except OSError:
            tracks = 0
        # read with bytes, not `splitlines()`: a tag may contain U+0085, which
        # Python also treats as a line break
        cached = {"tracks": tracks,
                  "indexed_at": datetime.fromtimestamp(
                      stat.st_mtime, timezone.utc).isoformat(timespec="seconds")}
        _COUNT_CACHE.clear()
        _COUNT_CACHE[key] = cached
    result.update(cached)
    result["exists"] = result["tracks"] > 0
    result["inventory"] = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    return result


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


def _process_state(pid: int) -> str:
    """`running`, `paused` or `dead` for one pid.

    The job is our own child and the app never waits for it, so a finished
    index stays a zombie until it is reaped.  `os.kill(pid, 0)` still succeeds
    for a zombie, which would make a finished job look running forever — so
    the process state is checked as well.  `T` is a stopped process, which is
    exactly what a paused index is.
    """
    if not pid:
        return "dead"
    try:
        os.kill(pid, 0)
    except OSError:
        return "dead"
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().split(b") ", 1)[1].split()
        if fields and fields[0] in (b"Z", b"X"):
            return "dead"
        if fields and fields[0] == b"T":
            return "paused"
    except (OSError, IndexError):
        pass
    return "running"


def _alive(pid: int) -> bool:
    """Is that pid a live process (running or paused)?"""
    return _process_state(pid) in {"running", "paused"}


def _signal(pid: int, sig: int) -> bool:
    """Signal the job's whole process group, so the scan stops with it.

    The runner is started with `start_new_session=True`, so its pid is also its
    process-group id and every stage it starts inherits that group.
    """
    if not pid:
        return False
    try:
        os.killpg(pid, sig)
        return True
    except OSError:
        pass
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def _settle(pid: int, want: str, timeout: float = 0.4) -> None:
    """Wait for a delivered SIGSTOP/SIGCONT to show up in `/proc`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_state(pid) == want:
            return
        time.sleep(0.02)


def _trim_inventory(root: Path) -> bool:
    """Drop a half-written last line a killed scan may have left behind.

    The scanner appends one JSON line per file and flushes; a SIGTERM in the
    middle of that write can leave a partial line.  Every reader already skips
    an unparsable line, so nothing breaks — this only keeps the file clean.
    """
    path = inventory_path(root)
    try:
        with path.open("rb+") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if not size:
                return False
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return False
            handle.seek(0)
            data = handle.read()
            cut = data.rfind(b"\n")
            handle.truncate(cut + 1 if cut >= 0 else 0)
        return True
    except OSError:
        return False


def _reap(pid: int) -> None:
    """Collect a finished child so it does not stay a zombie."""
    if not pid:
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


RUNNER = '''"""Run the library's index pipeline one stage after the other."""
import subprocess
import sys
from pathlib import Path

steps = {steps!r}
rc_file = Path({rc!r})

for label, argv in steps:
    print("[index] " + label, flush=True)
    code = subprocess.call(argv)
    if code:
        print("[index] " + label + " failed with exit " + str(code),
              flush=True)
        rc_file.write_text(str(code), encoding="utf-8")
        sys.exit(code)

rc_file.write_text("0", encoding="utf-8")
print("[index] done", flush=True)
'''


def start(root: Path) -> dict:
    """Queue one index rebuild; returns the job record (running or reused)."""
    root = Path(root)
    if not available(root):
        raise RuntimeError("the library has no scripts/phase0_inventory.py")
    running = _running(root)
    if running:
        return running

    # the scan skips a path it already knows, so a file edited elsewhere would
    # never be re-read: drop those rows first and let the scanner bring them back
    try:
        refreshed = inventory.prepare_scan(root)
    except Exception as exc:                     # never block the index
        refreshed = {"error": f"{type(exc).__name__}: {exc}"}

    folder = scripts(root)
    work = root / "library-work"
    argv_steps = [
        (label,
         [sys.executable, "-u", str(folder / INVENTORY),
          "scan" if phase == "scan" else "report",
          "--work", str(work)]
         if phase != "playlists" else
         [sys.executable, "-u", str(folder / PLAYLISTS), "--from-tree"])
        for label, phase in STEPS
    ]

    job_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    job_dir(root).mkdir(parents=True, exist_ok=True)
    log = job_dir(root) / f"{job_id}.log"
    rc_file = job_dir(root) / f"{job_id}.rc"
    runner = job_dir(root) / f"{job_id}.runner.py"
    runner.write_text(RUNNER.format(
        steps=[(label, argv) for label, argv in argv_steps],
        rc=str(rc_file)), encoding="utf-8")

    job = {
        "id": job_id,
        "state": "running",
        "started": _now(),
        "finished": "",
        "pid": 0,
        "root": str(root),
        "steps": [label for label, _ in STEPS],
        "log": str(log.relative_to(root)),
        "command": [" ".join(argv) for _, argv in argv_steps],
    }
    _save(root, job)

    with log.open("wb") as handle:
        handle.write(
            f"[index] indexing the library in {root}\n".encode())
        if refreshed.get("error"):
            handle.write(
                f"[index] inventory refresh skipped: {refreshed['error']}\n"
                .encode())
        elif refreshed.get("removed") or refreshed.get("missing"):
            handle.write(
                (f"[index] inventory: {refreshed['kept']} unchanged, "
                 f"{refreshed['removed']} changed (to re-read), "
                 f"{refreshed['missing']} missing (dropped), "
                 f"{refreshed['repaired']} path repaired\n").encode())
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
    """The job that is still running or paused ({} when there is none)."""
    job = latest(root)
    if not job:
        return {}
    pid = int(job.get("pid") or 0)
    if pid and _process_state(pid) in {"running", "paused"}:
        return job
    if pid:
        _reap(pid)
    return {}


def _pick(root: Path, job_id: str = "") -> dict:
    """One recorded job: the named one, else the newest live one, else latest."""
    if job_id:
        return _load(root, job_id)
    return _running(root) or latest(root)


def status(root: Path, job_id: str = "") -> dict:
    """The job record plus parsed progress and the tail of its log.

    The process itself decides the state, not the stored record: a record still
    says `running` after the child died, and a pause (`SIGSTOP`) is invisible
    in the record until something observes it.  `cancelling` and `cancelled`
    are ours, because a killed scanner reports no exit code of its own.
    """
    root = Path(root)
    job = _load(root, job_id) if job_id else latest(root)
    if not job:
        return {"state": "idle", "running": False, "active": False,
                "percent": 0.0, "lines": [], "exists": False,
                "available": available(root), "counts": counts(root)}
    pid = int(job.get("pid") or 0)
    live = _process_state(pid) if pid else "dead"
    stored = str(job.get("state") or "")
    stopping = bool(job.get("cancel_requested")) or stored in {
        "cancelling", "cancelled"}
    rc = None
    rc_file = job_dir(root) / f"{job['id']}.rc"
    try:
        rc = int(rc_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        rc = None
    if live in {"running", "paused"}:
        if stopping:
            state = "cancelling"
            if _cancelling_too_long(job):
                _signal(pid, signal.SIGKILL)     # it ignores SIGTERM
        else:
            state = "paused" if live == "paused" else "running"
    else:
        if pid:
            _reap(pid)
        if stopping:
            state = "cancelled"
        else:
            state = "done" if rc == 0 else "failed"
        if state == "failed" and rc is None and stored not in {
                "done", "failed"}:
            state = "failed"                # died before it could report
    if state != stored:
        job["state"] = state
        if state in {"done", "failed", "cancelled"}:
            job["finished"] = job.get("finished") or _now()
        _save(root, job)
        if state == "cancelled":
            _trim_inventory(root)
    active = state in {"running", "paused", "cancelling"}
    return {**job, "state": state, "running": state == "running",
            "active": active, "paused": state == "paused",
            "returncode": rc, "available": available(root),
            "counts": counts(root), **_progress(root, job)}


def _cancelling_too_long(job: dict) -> bool:
    """Has a cancelled job ignored SIGTERM for long enough to be killed?"""
    stamp = str(job.get("cancel_at") or "")
    try:
        began = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    waited = (datetime.now(timezone.utc) - began).total_seconds()
    return waited > CANCEL_GRACE_S


def pause(root: Path, job_id: str = "") -> dict:
    """Stop the job with SIGSTOP; it keeps its pid, its log and its place."""
    root = Path(root)
    job = _pick(root, job_id)
    if not job:
        return status(root, job_id)
    pid = int(job.get("pid") or 0)
    if _process_state(pid) != "running":
        return {**status(root, str(job["id"])),
                "note": "Nothing to pause: the index is not running."}
    if not _signal(pid, signal.SIGSTOP):
        return {**status(root, str(job["id"])), "error":
                "The index process could not be stopped."}
    _settle(pid, "paused")
    job["state"] = "paused"
    job["paused_at"] = _now()
    job.pop("cancel_requested", None)
    _save(root, job)
    return status(root, str(job["id"]))


def resume(root: Path, job_id: str = "") -> dict:
    """Continue a paused job where it stopped."""
    root = Path(root)
    job = _pick(root, job_id)
    if not job:
        return status(root, job_id)
    pid = int(job.get("pid") or 0)
    if _process_state(pid) != "paused":
        return {**status(root, str(job["id"])),
                "note": "Nothing to resume: the index is not paused."}
    if not _signal(pid, signal.SIGCONT):
        return {**status(root, str(job["id"])), "error":
                "The index process could not be continued."}
    _settle(pid, "running")
    job["state"] = "running"
    _save(root, job)
    return status(root, str(job["id"]))


def cancel(root: Path, job_id: str = "") -> dict:
    """Stop the job for good; a paused job is continued so it can die.

    The whole process group gets SIGTERM, because the runner's stages are
    children of the runner.  A process stopped with SIGSTOP does not act on a
    signal until it is continued, so SIGCONT goes first.  A stage that ignores
    SIGTERM is killed by :func:`status` after `CANCEL_GRACE_S` seconds.
    """
    root = Path(root)
    job = _pick(root, job_id)
    if not job:
        return status(root, job_id)
    job_id = str(job["id"])
    pid = int(job.get("pid") or 0)
    if _process_state(pid) not in {"running", "paused"}:
        return {**status(root, job_id),
                "note": "The index is not running any more."}
    _signal(pid, signal.SIGCONT)
    _signal(pid, signal.SIGTERM)
    job["state"] = "cancelling"
    job["cancel_requested"] = True
    job["cancel_at"] = _now()
    _save(root, job)
    return status(root, job_id)


def _progress(root: Path, job: dict) -> dict:
    path = Path(job.get("log") or "")
    if not path.is_absolute():
        path = Path(root) / str(job.get("log") or "")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    phase = "scan"
    done = total = errors = 0
    for line in lines:
        marker = _RE_STEP.match(line)
        if marker:
            label = marker.group(1).strip()
            for name, key in STEPS:
                if label.startswith(name):
                    phase = key
            if label.startswith("done"):
                phase = "done"
            continue
        counted = _RE_TOTAL.match(line)
        if counted:
            total = _number(counted.group(3))
            phase = "scan"
            continue
        tick = _RE_TICK.match(line)
        if tick:
            done, total = _number(tick.group(1)), _number(tick.group(2))
            phase = "scan"
            continue
        finished = _RE_FINISH.match(line)
        if finished:
            done = total or _number(finished.group(1))
            errors = _number(finished.group(2))
            phase = "report"
            continue
        report = _RE_REPORT.match(line)
        if report:
            phase = "report"
    if phase == "done":
        done = total or done
    step = next((name for name, key in STEPS if key == phase),
                "finished" if phase == "done" else "starting")
    if phase == "scan":
        percent = 95.0 * (done / total) if total else 0.0
    elif phase == "report":
        percent = 96.0
    elif phase == "playlists":
        percent = 98.0
    else:
        percent = 100.0 if job.get("state") == "done" else 0.0
    started = job.get("started") or ""
    elapsed = 0.0
    try:
        began = datetime.fromisoformat(started.replace("Z", "+00:00"))
        stamp = job.get("finished") or (
            job.get("paused_at") if job.get("state") == "paused" else "")
        end = (datetime.fromisoformat(stamp.replace("Z", "+00:00"))
               if stamp else datetime.now(timezone.utc))
        elapsed = max(0.0, (end - began).total_seconds())
    except (ValueError, TypeError, KeyError):
        pass
    if job.get("state") == "done":
        percent = 100.0
    return {"step": step, "phase": phase, "done": done, "total": total,
            "errors": errors, "percent": round(min(100.0, percent), 1),
            "elapsed": round(elapsed, 1), "lines": lines[-8:]}
