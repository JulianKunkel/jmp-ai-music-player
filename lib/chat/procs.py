"""Process helpers the background job modules share.

Every job in this folder (the library index, the lyric transcription, the
sound profile) is one detached child started with `start_new_session=True`, so
its pid is also its process-group id and every stage it starts inherits that
group.  That is what makes the controls simple:

- **pause** is `SIGSTOP` — the job keeps its pid, its log and its place, and
  the CPU/GPU load drops to nothing;
- **resume** is `SIGCONT`;
- **cancel** is `SIGCONT` followed by `SIGTERM`, because a stopped process does
  not act on a signal until it is continued.

Nothing here waits for a child either: the app never `waitpid`s, so a finished
job stays a zombie, and `/proc/<pid>/stat` is the only reliable answer to "is
it still there" (a zombie still answers `os.kill(pid, 0)`).
"""

from __future__ import annotations

import os
import signal
import time


def process_state(pid: int) -> str:
    """`running`, `paused` or `dead` for a pid, without reaping it."""
    if not pid:
        return "dead"
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().split(b") ", 1)[1].split()
    except (OSError, IndexError):
        return "dead"
    if fields and fields[0] in (b"Z", b"X"):
        return "dead"
    if fields and fields[0] == b"T":
        return "paused"
    return "running"


def alive(pid: int) -> bool:
    """Is that pid a live process (running or paused)?"""
    return process_state(pid) in {"running", "paused"}


def signal_group(pid: int, sig: int) -> bool:
    """Signal the job's whole process group (its pid doubles as the pgid)."""
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
    """Wait (briefly) for a delivered signal to show up in `/proc`.

    `SIGSTOP`/`SIGCONT` are asynchronous, so a status read taken right after
    the signal can still report the old state — which is what the pause button
    would otherwise echo back to the window.  A few milliseconds is enough.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_state(pid) == want:
            return
        time.sleep(0.02)


def pause(pid: int) -> bool:
    sent = signal_group(pid, signal.SIGSTOP)
    if sent:
        _settle(pid, "paused")
    return sent


def resume(pid: int) -> bool:
    sent = signal_group(pid, signal.SIGCONT)
    if sent:
        _settle(pid, "running")
    return sent


def cancel(pid: int) -> bool:
    signal_group(pid, signal.SIGCONT)
    return signal_group(pid, signal.SIGTERM)


def reap(pid: int) -> None:
    """Collect a finished child so it does not stay a zombie."""
    if not pid:
        return
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
