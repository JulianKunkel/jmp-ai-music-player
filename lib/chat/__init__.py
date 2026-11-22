"""The chat front end of JulAIn Music Player.

The command line (`music.py`) exposes every tool one by one; this package turns
the same tools into a conversation.  It is deliberately split so that the UI is
replaceable:

    engine.py     conversation state, skills, replies (no UI import)
    nlu.py        the offline language parser
    llm.py        optional OpenAI-compatible planner (never required)
    catalog.py    inventory, search, playlists
    selection.py  mood/feature-based track selection for new playlists
    profile.py    what the app learned about the listener
    player.py     mpv playback with friendly errors
    app.py        the Textual TUI (imported only when it is used)
    plain.py      line-based fallback (`music chat --plain`)

The sibling modules of the app (`playlist_edit`, `mpvctl`, `libpaths`) live one
directory up, so that directory is put on ``sys.path`` here, once.
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

__all__ = ["APP_DIR"]
