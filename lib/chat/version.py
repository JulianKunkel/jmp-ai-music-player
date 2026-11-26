"""The version of the code that is really running — one source of truth.

The number lives in the ``VERSION`` file that ships next to the code:
``application/VERSION`` in a checkout, ``/usr/lib/jmp/VERSION`` in the Debian
package and ``/opt/music/VERSION`` in the container image.  Nothing else keeps
a copy, so the window, the API, the chat answer and the HTTP header cannot
disagree with the build they came from.

The file is read once and cached — it cannot change while the process runs.  A
file that does not hold a plain number is *not* accepted, because the host
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


def version_file() -> Path | None:
    """The file the version really came from, or None when there is none."""
    for candidate in _candidates():
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        first = next((line.strip() for line in text.splitlines()
                      if line.strip()), "")
        if _PLAIN.match(first) and len(first) <= 40:
            return candidate
    return None


def _read() -> tuple[str, str]:
    if "value" in _cache:
        return _cache["value"]
    path = version_file()
    value = UNKNOWN
    where = ""
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        first = next((line.strip() for line in text.splitlines()
                      if line.strip()), "")
        if _PLAIN.match(first):
            value, where = first, str(path)
    _cache["value"] = (value, where)
    return _cache["value"]


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
    """What the API hands out: the number and where it came from."""
    return {
        "name": NAME,
        "short": SHORT,
        "version": app_version(),
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
    lines.append("")
    lines.append("The same number is what the window shows next to the name, "
                 "what `/api/state` reports and what the HTTP server and the "
                 "planner identify themselves with — one file, no second copy "
                 "to drift.")
    return "\n".join(lines)
