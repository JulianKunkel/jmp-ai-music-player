"""Playback for the chat, with errors a conversation can show.

`mpvctl.Mpv` raises ``SystemExit`` when mpv is missing; the chat must not exit,
so every call here is wrapped and returned as a message instead.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .catalog import clock
from mpvctl import Mpv, MpvError


class Player:
    def __init__(self, socket: str | None = None, audio: str | None = None):
        self.socket = socket or os.environ.get("MPV_SOCKET") or "/tmp/mpv.sock"
        self.audio = audio or os.environ.get("MPV_AO") or None
        self._client: Mpv | None = None

    # -- internals ---------------------------------------------------------

    @property
    def client(self) -> Mpv:
        if self._client is None:
            self._client = Mpv(self.socket)
        return self._client

    def running(self) -> bool:
        return self.client.alive()

    def install_hint(self) -> str:
        return ("mpv is not installed here — install it with `apt install mpv` "
                "or run the app in its container")

    # -- playback ----------------------------------------------------------

    def play(self, target: str, append: bool = False,
             shuffle: bool = False) -> tuple[bool, str]:
        if not (self.running() or shutil.which(os.environ.get("MPV_BIN") or "mpv")):
            return False, self.install_hint()
        try:
            client = self.client
            client.start(audio=self.audio)
            client.load(target, append=append)
            if shuffle:
                client.cmd("playlist-shuffle")
            return True, ""
        except (SystemExit, MpvError, OSError, ValueError) as exc:
            return False, str(exc)

    def command(self, action: str, value: float | None = None
                ) -> tuple[bool, str]:
        if not self.running():
            return False, "nothing is playing"
        client = self.client
        try:
            if action == "pause":
                client.set("pause", True)
            elif action == "resume":
                client.set("pause", False)
            elif action == "toggle":
                client.toggle()
            elif action == "stop":
                client.cmd("stop")
            elif action == "next":
                client.cmd("playlist-next")
            elif action == "prev":
                client.cmd("playlist-prev")
            elif action == "volume":
                client.set("volume", max(0.0, min(100.0, float(value or 0))))
            elif action == "volume_delta":
                now = float(client.get("volume", 100) or 100)
                client.set("volume",
                           max(0.0, min(100.0, now + float(value or 0))))
            elif action == "mute":
                client.set("mute", True)
            elif action == "unmute":
                client.set("mute", False)
            elif action == "seek":
                client.cmd("seek", float(value or 0), "relative")
            elif action == "seek_abs":
                client.cmd("seek", max(0.0, float(value or 0)), "absolute")
            elif action == "speed":
                client.set("speed", max(0.25, min(4.0, float(value or 1))))
            elif action == "shuffle_on":
                client.cmd("playlist-shuffle")
            elif action == "shuffle_off":
                client.cmd("playlist-unshuffle")
            elif action == "repeat_all":
                client.set("loop-playlist", "inf")
            elif action == "repeat_one":
                client.set("loop-file", "inf")
            elif action == "repeat_off":
                client.set("loop-playlist", "no")
                client.set("loop-file", "no")
            elif action == "position":
                index = max(0, int(value or 1) - 1)
                client.cmd("playlist-play-index", index)
            elif action == "remove":
                # the window's "up next" list counts entries from 1
                index = max(0, int(value or 1) - 1)
                client.cmd("playlist-remove", index)
            else:
                return False, f"unknown player command: {action}"
            return True, ""
        except (MpvError, OSError, ValueError) as exc:
            return False, str(exc)

    def set_loudness(self, value: str) -> tuple[bool, str]:
        """Set mpv's audio filter; an empty value restores the raw audio.

        The levelling is a playback filter, never a rewrite of the file, so it
        disappears with the daemon and leaves the library untouched.
        """
        if not self.running():
            return False, "nothing is playing"
        try:
            self.client.set("af", str(value or ""))
            return True, ""
        except (MpvError, OSError, ValueError) as exc:
            return False, str(exc)

    def status(self) -> dict | None:
        if not self.running():
            return None
        try:
            return self.client.status()
        except (MpvError, OSError):
            return None

    def queue(self, around: int = 4) -> list[tuple[int, str]]:
        """The current entry and its neighbours, 1-based, as (index, path)."""
        if not self.running():
            return []
        try:
            client = self.client
            total = int(client.get("playlist-count", 0) or 0)
            at = int(client.get("playlist-pos", 0) or 0)
            first = max(0, at - around)
            last = min(total - 1, at + around)
            out = []
            for index in range(first, last + 1):
                path = client.get(f"playlist/{index}/filename")
                if path:
                    out.append((index + 1, str(path)))
            return out
        except (MpvError, OSError, ValueError, TypeError):
            return []

    def queue_from(self, limit: int = 200) -> list[tuple[int, str]]:
        """The current entry and what follows it, 1-based (index, path).

        `queue()` shows a window around the playhead for the chat; this one is
        the "up next" list, which starts at the current track and runs forward.
        """
        if not self.running():
            return []
        try:
            client = self.client
            total = int(client.get("playlist-count", 0) or 0)
            at = max(0, int(client.get("playlist-pos", 0) or 0))
            last = min(total, at + max(1, int(limit or 1)))
            out = []
            for index in range(at, last):
                path = client.get(f"playlist/{index}/filename")
                if path:
                    out.append((index + 1, str(path)))
            return out
        except (MpvError, OSError, ValueError, TypeError):
            return []

    # -- presentation ------------------------------------------------------

    def status_line(self, root: Path | None = None) -> str:
        status = self.status()
        if not status or status.get("idle"):
            return "idle — ask for something to play"
        state = "paused" if status.get("pause") else "playing"
        title = status.get("title") or Path(status.get("file") or "?").name
        text = (f"{state}: {title}   "
                f"{clock(status.get('time'))} / {clock(status.get('duration'))}")
        if (status.get("playlist_count") or 0) > 1:
            text += (f"   {int(status.get('playlist_pos') or 0) + 1}"
                     f"/{status['playlist_count']}")
            if status.get("shuffle"):
                text += " shuffle"
        return text

    def progress(self) -> float | None:
        status = self.status()
        if not status or status.get("idle"):
            return None
        try:
            duration = float(status.get("duration") or 0)
            position = float(status.get("time") or 0)
        except (TypeError, ValueError):
            return None
        if duration <= 0:
            return None
        return max(0.0, min(1.0, position / duration))

    def current_file(self) -> str:
        status = self.status()
        if not status or status.get("idle"):
            return ""
        return str(status.get("file") or "")
