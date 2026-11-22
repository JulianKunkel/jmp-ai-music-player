#!/usr/bin/env python3
"""Locate the music library from wherever the app is running.

The same code runs in three places:

* straight from the repository (``application/lib/music.py``),
* from an installed copy (``~/.local/share/julians-music-app/lib``),
* inside the container (``/opt/music/app``).

The library root is the folder that holds the shelves and the work folder.
``MUSIC_LIBRARY`` wins; otherwise the nearest parent of the current directory
or of this file that looks like a library is used.  A missing library is a
hard error with an actionable message — never a silent guess.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

#: The folder this file lives in — the app's own modules.
APP_DIR = Path(__file__).resolve().parent


def looks_like_library(path: Path) -> bool:
    """A library has its playlists and either shelves or a work folder."""
    try:
        return (path / "Playlists").is_dir() and (
            (path / "library-work").is_dir() or (path / "Artists").is_dir()
        )
    except OSError:
        return False


@lru_cache(maxsize=1)
def library_root() -> Path:
    """The library this app works on."""
    env = os.environ.get("MUSIC_LIBRARY")
    if env:
        # an explicit setting is trusted: the installer already validated it,
        # and a plain folder of music has no Playlists/ to check against
        root = Path(env).expanduser()
        if not root.is_dir():
            raise SystemExit(f"MUSIC_LIBRARY is not a directory: {root}")
        return root.resolve()
    for start in (Path.cwd(), APP_DIR):
        for candidate in (start, *start.parents):
            if looks_like_library(candidate):
                return candidate.resolve()
    raise SystemExit(
        "cannot find the music library: set MUSIC_LIBRARY to the folder that "
        "holds Artists/ and Playlists/ (for example "
        "MUSIC_LIBRARY=$HOME/Music)")


def library_tool(name: str) -> Path:
    """A maintenance tool of the library (tagging, scan, playlist build)."""
    return library_root() / "scripts" / name


def app_module(name: str) -> Path:
    """A module of the app itself — the file next to this one."""
    return APP_DIR / name
