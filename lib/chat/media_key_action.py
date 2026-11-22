#!/usr/bin/env python3
"""Run one transport action from a desktop shortcut.

A GNOME custom shortcut runs with the session environment, not the shell that
started the player.  The command therefore cannot assume that ``music-cli``
points at the copy that owns the mpv socket: a source install and a .deb can be
present at the same time, and the player may run in a container.  This helper
tries the routes the running app itself uses, in order:

1. the loopback server API, with the URL the window (or the MPRIS agent) wrote
   to ``~/.cache/julians-music-app/host.json``;
2. the MPRIS player on the session bus, which the window or the agent owns;
3. ``music-cli`` as the last resort, for a player that has neither.

It is stdlib only and never opens a music file.  One key press runs one
transport command.

    jmp-media-action toggle
    jmp-media-action --probe          # what would work, as JSON
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_ID = "julians-music-app"
ACTIONS = ("toggle", "pause", "resume", "stop", "next", "prev")
#: the MPRIS method for each action
MPRIS = {
    "toggle": "PlayPause",
    "pause": "Pause",
    "resume": "Play",
    "stop": "Stop",
    "next": "Next",
    "prev": "Previous",
}
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
STATE_FILES = (CACHE / APP_ID / "host.json",
               CACHE / APP_ID / "media-keys.json")
LOG = CACHE / APP_ID / "media-keys.log"
TIMEOUT = 1.5


def _safe(url: str) -> str:
    """A URL without its query string: ``?t=`` carries the access token."""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return ""
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    "", ""))


def _log(action: str, route: str, detail: str = "") -> None:
    """One line in the same log the window writes, so a key is traceable."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        line = (time.strftime("%Y-%m-%dT%H:%M:%S") + "\tshortcut\t" +
                action + "\t" + route + "\t" + _safe(detail)[:200] + "\n")
        fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _urls() -> list[str]:
    """The server URLs the window or the agent recorded, newest first."""
    found: list[tuple[float, str]] = []
    for path in STATE_FILES:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        url = str(data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        found.append((float(data.get("at") or 0), url))
    return [url for _at, url in sorted(found, reverse=True)]


def _http(action: str, url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    netloc = host + (f":{parts.port}" if parts.port else "")
    target = urllib.parse.urlunsplit(
        (parts.scheme, netloc, "/api/transport", parts.query, ""))
    body = json.dumps({"action": action}).encode("utf-8")
    request = urllib.request.Request(
        target, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            text = reply.read(4000).decode("utf-8", "replace")
            if not 200 <= reply.status < 300:
                return False
            try:
                return not json.loads(text or "{}").get("error")
            except ValueError:
                return True
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _mpris(action: str) -> bool:
    gdbus = shutil.which("gdbus")
    if not gdbus:
        return False
    try:
        done = subprocess.run(
            [gdbus, "call", "--session",
             "--dest", "org.mpris.MediaPlayer2.jmp",
             "--object-path", "/org/mpris/MediaPlayer2",
             "--method",
             f"org.mpris.MediaPlayer2.Player.{MPRIS[action]}"],
            capture_output=True, text=True, timeout=TIMEOUT)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _cli_path(wanted: str = "") -> str:
    if wanted:
        return shutil.which(wanted) or (
            wanted if os.access(wanted, os.X_OK) else "")
    found = shutil.which("music-cli")
    if found:
        return found
    for candidate in (os.path.expanduser("~/.local/bin/music-cli"),
                      "/usr/local/bin/music-cli", "/usr/bin/music-cli"):
        if os.access(candidate, os.X_OK):
            return candidate
    return ""


def _cli(action: str, cli: str) -> bool:
    path = _cli_path(cli)
    if not path:
        return False
    try:
        done = subprocess.run([path, action], capture_output=True, text=True,
                              timeout=8)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def probe(cli: str = "") -> dict:
    """Which route a key press would take right now."""
    urls = _urls()
    answer = {
        "http": any(_http_status(url) for url in urls),
        "mpris": _mpris_owner(),
        "cli": _cli_path(cli),
        "urls": [_safe(url) for url in urls[:2]],
    }
    answer["ok"] = bool(answer["http"] or answer["mpris"] or answer["cli"])
    return answer


def _http_status(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    netloc = host + (f":{parts.port}" if parts.port else "")
    target = urllib.parse.urlunsplit(
        (parts.scheme, netloc, "/api/status", parts.query, ""))
    try:
        with urllib.request.urlopen(target, timeout=TIMEOUT) as reply:
            return 200 <= reply.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _mpris_owner() -> bool:
    gdbus = shutil.which("gdbus")
    if not gdbus:
        return False
    try:
        done = subprocess.run(
            [gdbus, "call", "--session", "--dest", "org.freedesktop.DBus",
             "--object-path", "/org/freedesktop/DBus",
             "--method", "org.freedesktop.DBus.NameHasOwner",
             "org.mpris.MediaPlayer2.jmp"],
            capture_output=True, text=True, timeout=TIMEOUT)
        return done.returncode == 0 and "true" in done.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def run(action: str, cli: str = "") -> int:
    if action not in ACTIONS:
        print(f"unknown action {action!r}; use one of {', '.join(ACTIONS)}",
              file=sys.stderr)
        return 2
    errors = []
    for url in _urls():
        if _http(action, url):
            _log(action, "http", url)
            return 0
        errors.append("http")
    if _mpris(action):
        _log(action, "mpris")
        return 0
    errors.append("mpris")
    if _cli(action, cli):
        _log(action, "cli", _cli_path(cli))
        return 0
    errors.append("cli")
    _log(action, "failed", ",".join(errors))
    print("no route to the player: " + ", ".join(errors), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cli = ""
    if "--cli" in args:
        index = args.index("--cli")
        if index + 1 >= len(args):
            print("--cli needs a path", file=sys.stderr)
            return 2
        cli = args[index + 1]
        del args[index:index + 2]
    if args[:1] == ["--probe"]:
        answer = probe(cli)
        print(json.dumps(answer, ensure_ascii=False))
        return 0 if answer.get("ok") else 1
    if not args:
        print("usage: jmp-media-action ACTION | --probe", file=sys.stderr)
        return 2
    return run(args[0].strip().lower(), cli)


if __name__ == "__main__":
    raise SystemExit(main())
