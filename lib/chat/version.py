"""The version of the code that is really running — one source of truth.

The number lives in the ``VERSION`` file that ships next to the code:
``application/VERSION`` in a checkout, ``/usr/lib/jmp/VERSION`` in the Debian
package and ``/opt/music/VERSION`` in the container image.  Nothing else keeps
a copy, so the window, the API, the chat answer and the HTTP header cannot
disagree with the build they came from.

The file is read once and cached — the number a *process* runs cannot change.
An upgrade replaces that file while a window is open, though, so
:func:`on_disk` reads it again (cached by size and mtime) and :func:`status`
tells the window that the code on disk is newer than the code in memory; that
is what lets the page restart the server in place instead of asking the
listener to hunt for processes.

A file that does not hold a plain number is *not* accepted, because the host
installer writes an ``app=…`` record to ``$APP_HOME/VERSION``; the real number
sits next to the code there, and a record must never be shown as a version.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

NAME = "JulAIn Music Player"
SHORT = "JMP"
UNKNOWN = "unknown"

#: a plain version: 1.2.8, 1.2.8-1, 2.0, 1.2.8+git.abc
_PLAIN = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:[-+~][0-9A-Za-z][0-9A-Za-z.+-]*)?$")

_cache: dict[str, tuple[str, str]] = {}

#: what the files on disk say, keyed by (path, size, mtime_ns) — an upgrade
#: replaces the VERSION file, so the key changes with it
_disk_cache: dict[tuple[str, int, int], tuple[str, str]] = {}


def _candidates() -> list[Path]:
    """Where the number may sit, nearest first.

    ``lib/chat/version.py`` -> ``<home>`` is the directory the installer or
    the package put next to ``lib/``, so the same two lookups work for a
    checkout (``application/``), the package (``/usr/lib/jmp``) and the image
    (``/opt/music``).
    """
    here = Path(__file__).resolve()
    home = here.parents[2] if len(here.parents) > 2 else here.parent
    code = here.parents[1] if len(here.parents) > 1 else here.parent
    return [home / "VERSION", code / "VERSION"]


def _plain(path: Path) -> str:
    """The version in `path`, or '' when it holds none (or cannot be read)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    first = next((line.strip() for line in text.splitlines()
                  if line.strip()), "")
    return first if _PLAIN.match(first) and len(first) <= 40 else ""


def version_file() -> Path | None:
    """The file the version really came from, or None when there is none."""
    for candidate in _candidates():
        if not candidate.is_file():
            continue
        if _plain(candidate):
            return candidate
    return None


def _read() -> tuple[str, str]:
    """The release this *process* loaded, read once and kept."""
    if "value" in _cache:
        return _cache["value"]
    path = version_file()
    value = _plain(path) if path is not None else ""
    _cache["value"] = ((value, str(path)) if value else (UNKNOWN, ""))
    return _cache["value"]


def on_disk() -> tuple[str, str]:
    """The release the files carry *now* — an upgrade changes it mid-session.

    Cached by (path, size, mtime), so the window can ask on every status poll
    for the cost of one ``stat`` and still notice a package that was installed
    a second ago.
    """
    for candidate in _candidates():
        try:
            info = candidate.stat()
        except OSError:
            continue
        key = (str(candidate), info.st_size, info.st_mtime_ns)
        if key in _disk_cache:
            cached = _disk_cache[key]
            if cached[0]:
                return cached
            continue
        value = _plain(candidate)
        found = (value, str(candidate)) if value else (UNKNOWN, "")
        if len(_disk_cache) > 8:          # the last few upgrades, not a leak
            _disk_cache.clear()
        _disk_cache[key] = found
        if value:
            return found
    return UNKNOWN, ""


def status() -> dict:
    """Running code against installed code — what a restart would change."""
    running, where = _read()
    installed, installed_where = on_disk()
    stale = bool(installed != UNKNOWN and running != UNKNOWN
                 and installed != running)
    return {
        "running": running,
        "disk": installed,
        "file": installed_where or where,
        "stale": stale,
    }


def app_version() -> str:
    """``1.2.8`` — or ``unknown`` when the install carries no version file."""
    return _read()[0]


def app_version_file() -> str:
    """The path the number was read from (empty when unknown)."""
    return _read()[1]


def known() -> bool:
    return app_version() != UNKNOWN


def label() -> str:
    """``JulAIn Music Player 1.2.8`` — the name and number on one line."""
    return f"{NAME} {app_version()}" if known() else NAME


def info() -> dict:
    """What the API hands out: the number, where it came from, and the one on
    disk (which a package upgrade can have moved past this process)."""
    state = status()
    return {
        "name": NAME,
        "short": SHORT,
        "version": state["running"],
        "on_disk": state["disk"],
        "stale": state["stale"],
        "file": app_version_file(),
        "python": sys.version.split()[0],
    }


def markdown(root=None) -> str:
    """The chat answer: the number, where it came from and what runs it."""
    lines = [f"**{NAME}** {app_version()}" if known()
             else f"**{NAME}** — the version is not known here"]
    detail = []
    if app_version_file():
        detail.append(f"read from `{app_version_file()}`")
    else:
        detail.append("no `VERSION` file next to the code, so this build "
                      "cannot say which release it is")
    detail.append(f"Python {sys.version.split()[0]}")
    if root is not None:
        detail.append(f"running from `{root}`")
    lines.append("")
    lines.append(" · ".join(detail) + ".")
    state = status()
    if state["stale"]:
        lines.append("")
        lines.append(f"The code on disk is already **{state['disk']}**, so this "
                     "process is running older code than the install holds.  "
                     "The window restarts the server by itself when it notices "
                     "that; if it did not, start the app again.")
    lines.append("")
    lines.append("The same number is what the window shows next to the name, "
                 "what `/api/state` reports and what the HTTP server and the "
                 "planner identify themselves with — one file, no second copy "
                 "to drift.")
    return "\n".join(lines)
