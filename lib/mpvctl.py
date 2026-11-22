#!/usr/bin/env python3
"""Control a running mpv over its JSON IPC socket.

`music.py` uses this class; the script is also a usable command of its own:

    python3 application/lib/mpvctl.py start
    python3 application/lib/mpvctl.py load Playlists/All.m3u8
    python3 application/lib/mpvctl.py status
    python3 application/lib/mpvctl.py volume 65
    python3 application/lib/mpvctl.py seek +30
    python3 application/lib/mpvctl.py quit

`music-cli` drives the same client for every playback command.

One mpv daemon per socket (`MPV_SOCKET`, default `/tmp/mpv.sock`).  It runs
headless (`--no-video`), so it works in a container and on a desktop alike.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SOCKET = Path(os.environ.get("MPV_SOCKET") or "/tmp/mpv.sock")
DEFAULT_LOG = Path(os.environ.get("MPV_LOG") or "/tmp/mpv.log")
MPV_BIN = os.environ.get("MPV_BIN") or "mpv"


class MpvError(RuntimeError):
    pass


class Mpv:
    def __init__(self, path: str | Path = DEFAULT_SOCKET, timeout: float = 5.0):
        self.path = Path(path)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._id = 0

    # -- connection -------------------------------------------------------

    def connect(self) -> "Mpv":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self.path))
        self._sock = sock
        self._buf = b""
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def alive(self) -> bool:
        try:
            self.connect()
        except OSError:
            return False
        self.close()
        return True

    def _readline(self) -> dict:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise MpvError("mpv closed the socket")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    # -- protocol ---------------------------------------------------------

    def cmd(self, *args):
        if self._sock is None:
            self.connect()
        self._id += 1
        request = {"command": list(args), "request_id": self._id}
        assert self._sock is not None
        self._sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        while True:
            message = self._readline()
            if message.get("request_id") != self._id:
                continue                      # an event, not our reply
            if message.get("error") != "success":
                # str() every argument: `seek`/`speed`/`volume` carry floats,
                # and a plain join would raise TypeError instead of the mpv error
                detail = " ".join(str(arg) for arg in args)
                raise MpvError(f"{message.get('error')}: {detail}")
            return message.get("data")

    def get(self, prop: str, default=None):
        try:
            value = self.cmd("get_property", prop)
        except MpvError:
            return default
        return default if value is None else value

    def set(self, prop: str, value) -> None:
        self.cmd("set_property", prop, value)

    # -- lifecycle --------------------------------------------------------

    def start(self, audio: str | None = None, volume: int | None = None,
              log: str | Path = DEFAULT_LOG) -> "Mpv":
        """Start a headless daemon unless one already listens on the socket."""
        if self.alive():
            if volume is not None:
                self.set("volume", volume)
            return self
        binary = shutil.which(MPV_BIN)
        if binary is None:
            raise SystemExit(
                f"{MPV_BIN} is not installed (apt install mpv)")
        if self.path.exists():
            self.path.unlink()               # stale socket from a dead mpv
        self.path.parent.mkdir(parents=True, exist_ok=True)
        command = [binary, "--idle=yes", "--no-video", "--force-window=no",
                   "--audio-display=no", "--no-terminal", "--really-quiet",
                   "--input-ipc-server=" + str(self.path),
                   "--audio-client-name=music",
                   "--ytdl-format=bestaudio/best",
                   "--save-position-on-quit=no"]
        if audio:
            command.append(f"--ao={audio}")
        if volume is not None:
            command.append(f"--volume={volume}")
        log_path = Path(log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as fh:
            subprocess.Popen(command, stdout=fh, stderr=fh,
                             stdin=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self.path.exists() and self.alive():
                return self
            time.sleep(0.05)
        raise SystemExit(f"mpv did not open {self.path} — see {log_path}")

    def quit(self) -> None:
        if not self.alive():
            return
        try:
            self.cmd("quit")
        except MpvError:
            pass
        self.close()
        for _ in range(100):
            if not self.path.exists():
                return
            time.sleep(0.05)

    # -- playback ---------------------------------------------------------

    def load(self, target: str, append: bool = False) -> None:
        flag = "append-play" if append else "replace"
        self.cmd("loadfile", target, flag)

    def toggle(self) -> bool:
        self.cmd("cycle", "pause")
        return bool(self.get("pause", False))

    def status(self) -> dict:
        keys = {
            "idle": "idle-active", "pause": "pause", "volume": "volume",
            "mute": "mute", "time": "time-pos", "duration": "duration",
            "title": "media-title", "file": "path",
            "playlist_pos": "playlist-pos", "playlist_count": "playlist-count",
            "shuffle": "shuffle", "loop": "loop", "loop_file": "loop-file",
            "speed": "speed",
        }
        return {name: self.get(prop) for name, prop in keys.items()}


def clock(seconds) -> str:
    if seconds is None:
        return "--:--"
    total = int(round(float(seconds)))
    return f"{total // 60}:{total % 60:02d}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--socket", default=str(DEFAULT_SOCKET))
    ap.add_argument("--timeout", type=float, default=5.0)
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("start", help="start the daemon if it is not running")
    p.add_argument("--audio", default=os.environ.get("MPV_AO"),
                   help="audio output driver (pulse, alsa, null)")
    p.add_argument("--volume", type=int, default=None)

    p = sub.add_parser("load", help="play a file, playlist or URL")
    p.add_argument("target")
    p.add_argument("--append", action="store_true")

    sub.add_parser("toggle", help="pause/resume")
    sub.add_parser("play", help="resume")
    sub.add_parser("pause", help="pause")
    sub.add_parser("stop", help="stop, keep the daemon")
    sub.add_parser("next", help="next playlist entry")
    sub.add_parser("prev", help="previous playlist entry")
    sub.add_parser("shuffle", help="shuffle the playlist")
    sub.add_parser("status", help="what is playing")
    sub.add_parser("quit", help="stop the daemon")

    p = sub.add_parser("seek", help="relative seconds, or a percentage")
    p.add_argument("amount")

    p = sub.add_parser("volume", help="set volume 0..100")
    p.add_argument("level", type=int)

    p = sub.add_parser("raw", help="send a raw JSON command array")
    p.add_argument("json")

    args = ap.parse_args()
    player = Mpv(args.socket, args.timeout)

    if args.command == "start":
        player.start(args.audio, args.volume)
        print(f"mpv listening on {args.socket}")
        return 0
    if args.command == "quit":
        player.quit()
        print("mpv stopped")
        return 0
    if not player.alive():
        raise SystemExit("mpv is not running (start it with `music play …`)")

    try:
        if args.command == "load":
            player.load(args.target, args.append)
            print(f"playing {args.target}")
        elif args.command == "toggle":
            paused = player.toggle()
            print("paused" if paused else "playing")
        elif args.command == "play":
            player.set("pause", False)
            print("playing")
        elif args.command == "pause":
            player.set("pause", True)
            print("paused")
        elif args.command == "stop":
            player.cmd("stop")
            print("stopped")
        elif args.command == "next":
            player.cmd("playlist-next")
        elif args.command == "prev":
            player.cmd("playlist-prev")
        elif args.command == "shuffle":
            player.cmd("playlist-shuffle")
            print("shuffled")
        elif args.command == "seek":
            amount = args.amount.strip()
            if amount.endswith("%"):
                player.cmd("seek", float(amount[:-1]), "absolute-percent")
            else:
                player.cmd("seek", float(amount), "relative")
        elif args.command == "volume":
            player.set("volume", max(0, min(100, args.level)))
            print(f"volume {args.level}%")
        elif args.command == "raw":
            print(json.dumps(player.cmd(*json.loads(args.json)),
                             ensure_ascii=False))
        elif args.command == "status":
            s = player.status()
            if s["idle"]:
                print("idle — nothing playing")
                return 0
            mark = "paused" if s["pause"] else "playing"
            print(f"{mark}: {s['title'] or s['file']}")
            print(f"  {clock(s['time'])} / {clock(s['duration'])}"
                  f"   volume {int(round(s['volume'] or 0))}%"
                  + (" (muted)" if s["mute"] else ""))
            if s["playlist_count"] and s["playlist_count"] > 1:
                print(f"  entry {int(s['playlist_pos'] or 0) + 1}"
                      f" of {s['playlist_count']}"
                      + ("  [shuffle]" if s["shuffle"] else ""))
        else:
            ap.print_help()
            return 1
    except MpvError as exc:
        raise SystemExit(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
