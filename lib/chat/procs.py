"""Find and stop the processes that make up a running player session.

One session is more than one process:

* ``window`` — the GTK/WebKit window (``chat/window.py``), on the host
* ``server`` — ``music.py chat --web``, on the host or in a container
* ``mpris`` — the small agent that keeps the desktop player alive when the
  window is closed (``chat/media_keys.py``), on the host
* ``player`` — the headless ``mpv`` daemon on ``MPV_SOCKET``

An upgrade replaces files, not running code: an old window keeps answering
with old behaviour until its process ends.  ``music-cli kill`` is the honest
"stop everything and start clean" that this situation needs, instead of
asking the user to hunt PIDs.

Matching is deliberately narrow — a python interpreter running a ``chat/``
script, or an ``mpv`` that uses *our* socket — so an unrelated music player,
editor or ``mpv`` video is never touched.  Only processes of the current user
are considered, and a SIGKILL is sent only when the command line is still the
one that was inspected (so a reused PID cannot hit a stranger).

This module has no third-party imports: the container uses it, the host
window installer uses it, and ``music.py`` imports it for ``music kill``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

#: stable order: the window first, because it would otherwise keep asking the
#: server for state while the rest of the session is torn down
ROLES = ("window", "server", "mpris", "player")
ORDER = {role: index for index, role in enumerate(ROLES)}
DEFAULT_SOCKET = "/tmp/mpv.sock"

_PYTHON = re.compile(r"^(python(?:\d+(?:\.\d+)*)?|pypy(?:\d+)?)$")


@dataclass(frozen=True)
class Proc:
    """One process that belongs to the player session."""

    pid: int
    role: str
    argv: tuple[str, ...]

    @property
    def command(self) -> str:
        return " ".join(shlex.quote(arg) for arg in self.argv)

    def as_dict(self) -> dict:
        return {"pid": self.pid, "role": self.role,
                "command": self.command, "argv": list(self.argv)}


@dataclass(frozen=True)
class Outcome:
    proc: Proc
    result: str                 # stopped | killed | gone | failed

    def as_dict(self) -> dict:
        return {**self.proc.as_dict(), "result": self.result}


def default_socket() -> str:
    return os.environ.get("MPV_SOCKET") or DEFAULT_SOCKET


def _base(path: str) -> str:
    return os.path.basename((path or "").rstrip("/"))


def _dirname(path: str) -> str:
    return os.path.basename(os.path.dirname((path or "").rstrip("/")))


def _is_python(argv: tuple[str, ...] | list[str]) -> bool:
    return bool(argv) and bool(_PYTHON.match(_base(argv[0])))


def _chat_script(argv: tuple[str, ...] | list[str], name: str) -> str:
    """The argument that names ``chat/<name>`` in a python process, else ''."""
    if not argv:
        return ""
    if _base(argv[0]) == name and _dirname(argv[0]) == "chat":
        return argv[0]                  # started directly, via its shebang
    if not _is_python(argv):
        return ""
    for arg in argv[1:]:
        if _base(arg) == name and _dirname(arg) == "chat":
            return arg
    return ""


def _same_socket(given: str, wanted: str) -> bool:
    try:
        if os.path.exists(given) and os.path.exists(wanted):
            return os.path.realpath(given) == os.path.realpath(wanted)
    except OSError:
        pass
    return os.path.abspath(given) == os.path.abspath(wanted)


def classify(argv: tuple[str, ...] | list[str],
             socket_path: str | None = None) -> str:
    """Return the role of a command line, or '' when it is not ours.

    ``socket_path`` matters for mpv: ``--audio-client-name=music`` or *our*
    IPC socket is the marker, never the bare name ``mpv``.
    """
    argv = [str(arg) for arg in argv]
    wanted_socket = socket_path or default_socket()

    if _chat_script(argv, "window.py"):
        return "window"
    if _chat_script(argv, "media_keys.py"):
        return "mpris"

    # the HTTP server: the app front end running `chat --web`, either as
    # `python3 .../music.py chat --web` or through the image's `music` entry
    if _is_python(argv):
        rest: list[str] = []
        for index, arg in enumerate(argv[1:], 1):
            if _base(arg) == "music.py":
                rest = argv[index + 1:]
                break
        if rest[:1] == ["chat"] and "--web" in rest:
            return "server"
    elif _base(argv[0]) in ("music", "music.py", "music-cli"):
        rest = argv[1:]
        if rest[:1] == ["chat"] and "--web" in rest:
            return "server"

    if argv and _base(argv[0]) == "mpv":
        for arg in argv[1:]:
            if arg.startswith("--input-ipc-server="):
                if _same_socket(arg.split("=", 1)[1], wanted_socket):
                    return "player"
            elif arg == "--audio-client-name=music":
                return "player"
    return ""


def _proc_entries():
    """Yield ``(pid, argv)`` for processes of this user, Linux first."""
    base = Path("/proc")
    try:
        names = os.listdir(base)
    except OSError:
        names = []
    if names:
        uid = os.getuid()
        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                if os.stat(base / name).st_uid != uid:
                    continue
                raw = (base / name / "cmdline").read_bytes()
            except OSError:
                continue
            argv = tuple(part.decode("utf-8", "replace")
                         for part in raw.split(b"\0") if part)
            if argv:
                yield pid, argv
        return

    # no /proc (or a restricted one): `ps` is best effort only
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return
    for line in (out.stdout or "").splitlines():
        pid_text, _, rest = line.strip().partition(" ")
        if pid_text.isdigit() and rest:
            yield int(pid_text), tuple(rest.split())


def find(socket_path: str | None = None,
         processes=None) -> list[Proc]:
    """The running session's processes, window first."""
    wanted_socket = socket_path or default_socket()
    own = os.getpid()
    found: list[Proc] = []
    source = processes if processes is not None else _proc_entries()
    for pid, argv in source:
        if pid == own:
            continue
        role = classify(argv, wanted_socket)
        if role:
            found.append(Proc(pid, role, tuple(argv)))
    found.sort(key=lambda proc: (ORDER.get(proc.role, 99), proc.pid))
    return found


def _cmdline(pid: int) -> tuple[str, ...] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return tuple(part.decode("utf-8", "replace")
                 for part in raw.split(b"\0") if part)


def alive(pid: int) -> bool:
    """True while ``pid`` is a live process (a zombie has already exited)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # the comm field is in parentheses and may contain spaces
        if state.rsplit(")", 1)[-1].strip().startswith("Z"):
            return False
    except OSError:
        pass
    return True


def process_state(pid: int) -> str:
    """``running``, ``paused`` (SIGSTOP) or ``dead`` for one process.

    A background job is started with ``start_new_session=True`` and the app
    never waits for it, so ``os.kill(pid, 0)`` alone is misleading: a finished
    child stays a zombie and still answers it.  ``/proc/<pid>/stat`` is the
    state that is true while nobody reaps.
    """
    if not pid or pid <= 0:
        return "dead"
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        state = data.rsplit(b")", 1)[1].split()[0].decode("ascii", "replace")
    except (OSError, IndexError):
        return "dead"
    if state in ("Z", "X", "x"):
        return "dead"
    return "paused" if state == "T" else "running"


def _signal(pid: int, sig: int) -> bool:
    """Signal the child's whole process group, the bare pid as a fallback.

    Every job is started with ``start_new_session=True``, so the pid doubles
    as the group id and a stage the job started is frozen or stopped with it.
    """
    if not pid or pid <= 0:
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


def pause(pid: int) -> bool:
    """Freeze a running job where it is (``SIGSTOP``)."""
    if process_state(pid) != "running":
        return False
    return _signal(pid, signal.SIGSTOP)


def resume(pid: int) -> bool:
    """Continue a frozen job (``SIGCONT``)."""
    if process_state(pid) != "paused":
        return False
    return _signal(pid, signal.SIGCONT)


def cancel(pid: int) -> bool:
    """Stop a job for good: ``SIGCONT`` first, because a stopped process does
    not act on ``SIGTERM`` until it is running again."""
    if not pid or pid <= 0:
        return False
    _signal(pid, signal.SIGCONT)
    return _signal(pid, signal.SIGTERM)


def reap(pid: int) -> bool:
    """Collect a finished child so it does not stay a zombie.

    False when there is nothing to collect (not our child, or still running).
    """
    if not pid or pid <= 0:
        return False
    try:
        collected, _status = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return False
    return collected == pid


def _still_ours(proc: Proc) -> bool:
    """False when the PID has been reused by an unrelated command."""
    current = _cmdline(proc.pid)
    return current is None or current == proc.argv


def stop(found: list[Proc], grace: float = 3.0) -> list[Outcome]:
    """SIGTERM every process, then SIGKILL what is left after ``grace``."""
    outcomes: list[Outcome] = []
    pending: dict[int, Proc] = {}
    for proc in found:
        if not alive(proc.pid):
            outcomes.append(Outcome(proc, "gone"))
            continue
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            outcomes.append(Outcome(proc, "gone"))
        except OSError:
            outcomes.append(Outcome(proc, "failed"))
        else:
            pending[proc.pid] = proc

    deadline = time.monotonic() + max(0.0, float(grace))
    while pending and time.monotonic() < deadline:
        for pid in list(pending):
            if not alive(pid):
                outcomes.append(Outcome(pending.pop(pid), "stopped"))
        if pending:
            time.sleep(0.05)

    for pid, proc in list(pending.items()):
        if not _still_ours(proc):
            outcomes.append(Outcome(proc, "failed"))
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            outcomes.append(Outcome(proc, "stopped"))
            continue
        except OSError:
            outcomes.append(Outcome(proc, "failed"))
            continue
        # `kill` returns before the kernel has delivered the signal, so the
        # honest answer is only known once the process is really gone
        deadline = time.monotonic() + 1.0
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        outcomes.append(Outcome(proc, "killed" if not alive(pid) else "failed"))
    return outcomes


def cleanup_socket(socket_path: str | None = None) -> Path | None:
    """Remove an mpv socket left behind by a process that is gone."""
    target = Path(socket_path or default_socket())
    try:
        if not target.is_socket():
            return None
    except OSError:
        return None
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.3)
    try:
        probe.connect(str(target))
    except OSError:
        try:
            target.unlink()
        except OSError:
            return None
        return target
    finally:
        probe.close()
    return None                        # a player still answers on it


def report(outcomes: list[Outcome], removed: Path | None = None,
           dry_run: bool = False, stream=None) -> None:
    """One line per process; nothing is hidden and nothing is embellished."""
    stream = stream or sys.stdout
    verb = "would stop" if dry_run else "stopped"
    if not outcomes:
        print("nothing to stop.", file=stream)
        return
    print(f"{len(outcomes)} process{'es' if len(outcomes) != 1 else ''} "
          f"{'to stop' if dry_run else ''}".rstrip() + ":", file=stream)
    for outcome in outcomes:
        proc = outcome.proc
        if outcome.result in ("stopped", "killed"):
            state = verb if dry_run else outcome.result
        else:
            state = outcome.result
        print(f"  {proc.role:<6}  {state:<11}  {proc.pid:<7}  {proc.command}",
              file=stream)
    if removed is not None:
        print(f"  removed stale socket {removed}", file=stream)


def kill_all(socket_path: str | None = None, keep_mpv: bool = False,
             dry_run: bool = False, grace: float = 3.0,
             stream=None) -> int:
    """Stop a whole session.  Returns 0, or 1 when a process survived."""
    stream = stream or sys.stdout
    found = find(socket_path)
    if keep_mpv:
        found = [proc for proc in found if proc.role != "player"]
    if dry_run:
        report([Outcome(proc, "stopped") for proc in found], dry_run=True,
               stream=stream)
        return 0
    outcomes = stop(found, grace=grace)
    removed = None if keep_mpv else cleanup_socket(socket_path)
    report(outcomes, removed=removed, stream=stream)
    return 1 if any(o.result == "failed" for o in outcomes) else 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--socket", default=default_socket(),
                        help=f"mpv IPC socket (default {DEFAULT_SOCKET})")
    parser.add_argument("--grace", type=float, default=3.0,
                        help="seconds to wait after SIGTERM (default 3)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="procs.py",
        description="List or stop the processes of a player session.")
    sub = parser.add_subparsers(dest="command")
    listing = sub.add_parser("list", help="show the session's processes")
    _add_common(listing)
    listing.add_argument("--json", action="store_true")
    killing = sub.add_parser("kill", help="stop the session's processes")
    _add_common(killing)
    killing.add_argument("--keep-mpv", action="store_true",
                         help="leave playback running")
    killing.add_argument("--dry-run", action="store_true",
                         help="show what would be stopped")
    killing.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    if args.command == "list":
        found = find(args.socket)
        if args.json:
            print(json.dumps([proc.as_dict() for proc in found], indent=1))
        elif not found:
            print("nothing is running.")
        else:
            for proc in found:
                print(f"{proc.role:<6}  {proc.pid:<7}  {proc.command}")
        return 0

    found = find(args.socket)
    if args.keep_mpv:
        found = [proc for proc in found if proc.role != "player"]
    if args.json:
        if args.dry_run:
            print(json.dumps([proc.as_dict() for proc in found], indent=1))
            return 0
        outcomes = stop(found, grace=args.grace)
        removed = None if args.keep_mpv else cleanup_socket(args.socket)
        print(json.dumps({"processes": [o.as_dict() for o in outcomes],
                          "removed_socket": str(removed) if removed else None},
                         indent=1))
        return 1 if any(o.result == "failed" for o in outcomes) else 0
    return kill_all(socket_path=args.socket, keep_mpv=args.keep_mpv,
                    dry_run=args.dry_run, grace=args.grace)


if __name__ == "__main__":
    raise SystemExit(main())
