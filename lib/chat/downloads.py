"""Optional download modules — the only download surface of the open app.

Audio download is not part of this repository.  A *download module* is a
separate package (its own repository, usually closed) that a build copies into
``lib/modules`` and byte-compiles.  This module is the facade every host call
site uses, so the player compiles and runs either way:

* with a module, ``available()`` is true and the calls are proxied to it;
* without one, reads answer empty, writes raise :class:`DownloadUnavailable`,
  the window hides the import button and the chat says the feature is not
  installed.

The contract a provider package implements is documented in
``docs/modules.md``; the reference implementation stays in its own repository.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

#: the package name a build installs (``jmp_download`` by default)
MODULE_NAME = os.environ.get("JMP_DOWNLOAD_MODULE", "jmp_download")

#: the directory ``lib/modules`` next to this package
MODULES_DIR = Path(__file__).resolve().parent.parent / "modules"

# the host code imports these constants; without a module they still exist so
# nothing at import time can fail
ATTENTION = ("failed", "partial", "unknown")
FINISHED = {"done", "partial", "failed", "duplicate", "unknown", "cancelled"}
EDITABLE = ("title", "artist", "album", "genre", "track")
KEEP_JOBS = 0

_UNAVAILABLE = (
    "No download module is installed.  The player itself never downloads "
    "audio; a build may add an optional download module under `lib/modules`."
)


class DownloadUnavailable(RuntimeError):
    """A download action was requested but no module provides it."""


_provider = None
_loaded = False
#: the search paths this module put on ``sys.path`` (removed again by reset)
_added_paths: list[str] = []


def _search_paths() -> list[str]:
    paths: list[str] = []
    # a test or a custom build may point at more than one module folder
    for variable in ("JMP_MODULES", "JMP_DOWNLOAD_PATH"):
        value = os.environ.get(variable) or ""
        paths.extend(part for part in value.split(os.pathsep) if part)
    paths.append(str(MODULES_DIR))
    return paths


def provider():
    """The loaded module package, or ``None`` when this build has none."""
    global _provider, _loaded
    if _loaded:
        return _provider
    _loaded = True
    for path in reversed(_search_paths()):
        # the first configured path must win, so insert at 0 from the end
        if path and Path(path).is_dir() and path not in sys.path:
            sys.path.insert(0, path)
            _added_paths.append(path)
    try:
        _provider = importlib.import_module(MODULE_NAME)
    except Exception:                      # absent, broken or wrong version
        _provider = None
    return _provider


def reset() -> None:
    """Forget the loaded module (a build or a test changed the search path)."""
    global _provider, _loaded
    _provider = None
    _loaded = False
    sys.modules.pop(MODULE_NAME, None)
    while _added_paths:
        path = _added_paths.pop()
        try:
            sys.path.remove(path)
        except ValueError:
            pass


def installed() -> bool:
    """True when a download module package is present (tooling aside)."""
    return provider() is not None


def available(root=None) -> bool:
    """True when a module is present *and* this library can file an import."""
    module = provider()
    if module is None:
        return False
    probe = getattr(module, "available", None)
    if callable(probe):
        try:
            return bool(probe(root))
        except Exception:
            return False
    return True


def _require():
    module = provider()
    if module is None:
        raise DownloadUnavailable(_UNAVAILABLE)
    return module


def _call(name: str, *args, **kwargs):
    return getattr(_require(), name)(*args, **kwargs)


# -- read-only calls -------------------------------------------------------


def search(query: str, count: int = 6, timeout: int = 90) -> list[dict]:
    return _call("search", query, count=count, timeout=timeout)


def probe(url: str, timeout: int = 60) -> dict:
    module = provider()
    if module is None:
        return {}
    return module.probe(url, timeout=timeout)


def guess_metadata(candidate: dict) -> tuple[str, str]:
    module = provider()
    if module is None:
        return "", str(candidate.get("title") or "").strip()
    return module.guess_metadata(candidate)


def jobs(root, records=None) -> list[dict]:
    module = provider()
    if module is None:
        return []
    return module.jobs(root)


def latest(root):
    module = provider()
    return module.latest(root) if module is not None else None


def attention(root, records=None):
    module = provider()
    return module.attention(root, records) if module is not None else None


def _queued(root, records=None) -> list[dict]:
    module = provider()
    if module is None:
        return []
    return module._queued(root, records)


def log_tail(root, record: dict, lines: int = 400) -> list[str]:
    module = provider()
    return module.log_tail(root, record, lines) if module is not None else []


def status_line(record: dict) -> str:
    module = provider()
    if module is None:
        return "unavailable"
    return module.status_line(record)


def _state(root, record):
    module = provider()
    if module is None:
        return {"state": "unavailable", "running": False, "paused": False,
                "active": False, "available": False}
    return module._state(root, record)


# -- queue writes ----------------------------------------------------------


def start(root, candidates: list[dict], **options) -> list[dict]:
    return _call("start", root, candidates, **options)


def pause(root, job_id: str = "") -> dict:
    return _call("pause", root, job_id)


def resume(root, job_id: str = "") -> dict:
    return _call("resume", root, job_id)


def cancel(root, job_id: str = "") -> dict:
    return _call("cancel", root, job_id)


def move(root, job_id: str, by: int = -1) -> dict:
    return _call("move", root, job_id, by)


def retry(root, job_id: str = "") -> dict:
    return _call("retry", root, job_id)


def dismiss(root, job_id: str = "") -> dict:
    return _call("dismiss", root, job_id)


def forget(root, job_id: str) -> dict:
    return _call("forget", root, job_id)


def update_tags(root, job_id: str, fields: dict) -> dict:
    return _call("update_tags", root, job_id, fields)


# -- the importer subprocess ----------------------------------------------


def importer_entry() -> str:
    module = provider()
    if module is None:
        return ""
    return str(getattr(module, "IMPORTER", "") or "")


def run_importer(root, argv: list[str]) -> int:
    """Run ``music import …`` through the module's importer; 1 when absent."""
    module = provider()
    entry = importer_entry()
    if module is None or not entry:
        print(f"error: {_UNAVAILABLE}", file=sys.stderr)
        return 1
    env = dict(os.environ, MUSIC_LIBRARY=str(root), PYTHONUNBUFFERED="1")
    roots = [str(Path(module.__file__).resolve().parent.parent)]
    if env.get("PYTHONPATH"):
        roots.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(roots)
    try:
        return subprocess.call([sys.executable, "-u", "-m", entry, *argv],
                               cwd=str(root), env=env)
    except OSError as exc:
        print(f"error: could not start the download module: {exc}",
              file=sys.stderr)
        return 1
