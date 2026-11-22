#!/usr/bin/env python3
"""Keep the desktop's media keys pointed at the running server.

``org.mpris.MediaPlayer2.jmp`` belongs to the native window while that window
is open (see ``window.py``), but the music keeps playing inside the container
after the window is closed — and a closed window takes the MPRIS player with
it, so the desktop's media keys end up with nothing to talk to.  This agent
owns the same name whenever the window is not there:

    python3 media_keys.py --url 'http://127.0.0.1:8765/?t=…'
    python3 media_keys.py --check         # 0 when one is already running

It talks to the loopback API only, exactly like the window: no tag, no
playlist and no audio file is opened for writing.  The name is yielded back to
the window as soon as the window claims it, and taken over again when the
window closes, so there is only ever one JMP player on the bus.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import window  # noqa: E402  (the standalone window module next to this file)

STATE = (Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") /
         window.APP_ID / "media-keys.json")
#: how often the server is asked whether it is still alive
POLL_MS = 2000
#: failed pings in a row before the agent gives up (a server restart is short)
PING_FAILS = 8
#: wait before asking for the name again after the window took it
YIELD_S = 10.0


def _state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _running() -> dict | None:
    record = _state()
    pid = int(record.get("pid") or 0)
    if pid and pid != os.getpid() and _alive(pid):
        return record
    return None


class Agent:
    def __init__(self, url: str):
        self.media = window._Media(url)
        self.media.present = None
        self.owning = False
        self.name_id = 0
        self.retry_at = 0.0
        self.fails = 0
        self.loop = None

    # -- the MPRIS name ----------------------------------------------------

    def _prepare(self) -> bool:
        """Register the MPRIS object once, so a retry only re-asks for the name."""
        from gi.repository import Gio

        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception:
            return False
        if connection is None:
            return False
        node = Gio.DBusNodeInfo.new_for_xml(window.MPRIS_XML)
        for interface in node.interfaces:
            try:
                connection.register_object(
                    window.MPRIS_PATH, interface,
                    self.media._method, self.media._property, None)
            except Exception:
                return False
        self.media.connection = connection
        return True

    def _own(self) -> None:
        from gi.repository import Gio

        connection = self.media.connection
        if connection is None:
            return

        def _acquired(*_args) -> None:
            self.owning = True
            self.retry_at = 0.0
            window._keylog("agent", "MPRIS", "acquired")
            try:
                self.media.refresh()
            except Exception:
                pass

        def _lost(*_args) -> None:
            # the window is open (or another agent won): stand back and retry
            self.owning = False
            self.name_id = 0
            self.retry_at = time.monotonic() + YIELD_S

        if self.name_id:
            try:
                Gio.bus_unown_name(self.name_id)
            except Exception:
                pass
            self.name_id = 0
        # ALLOW_REPLACEMENT: the native window may take the name while it is
        # open, and the agent simply asks again after the window closes.  It
        # must never REPLACE, or window and agent would steal it back and forth.
        self.name_id = Gio.bus_own_name_on_connection(
            connection, window.MPRIS_NAME, Gio.BusNameOwnerFlags.ALLOW_REPLACEMENT,
            _acquired, _lost)
        self.media.refresh()

    def quit(self) -> None:
        try:
            if self.name_id:
                from gi.repository import Gio

                Gio.bus_unown_name(self.name_id)
        except Exception:
            pass
        try:
            record = _state()
            if int(record.get("pid") or 0) == os.getpid():
                STATE.unlink()
        except Exception:
            pass
        if self.loop is not None:
            self.loop.quit()

    # -- the poll ----------------------------------------------------------

    def tick(self) -> bool:
        try:
            status = self.media.server.status()
        except Exception:
            self.fails += 1
            if self.fails >= PING_FAILS:
                self.quit()
                return False
            return True
        self.fails = 0
        if (not self.owning and not self.name_id
                and time.monotonic() >= self.retry_at):
            try:
                if self.media.connection is None and not self._prepare():
                    self.retry_at = time.monotonic() + YIELD_S
                else:
                    self._own()
            except Exception:
                self.retry_at = time.monotonic() + YIELD_S
        self.media.status = status
        self.media.refresh()
        return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args[:1] == ["--check"]:
        record = _running()
        if record:
            print(record.get("pid"))
            return 0
        return 3

    url = ""
    if args[:1] == ["--url"] and len(args) > 1:
        url = args[1]
    elif args and not args[0].startswith("-"):
        url = args[0]
    if not url:
        print("usage: media_keys.py --url URL | --check", file=sys.stderr)
        return 2

    if _running():
        return 0                        # one agent per session

    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:
        return 3                        # no PyGObject: the window tries alone

    try:
        if Gio.bus_get_sync(Gio.BusType.SESSION, None) is None:
            return 3
    except Exception:
        return 3                        # no session bus, nothing to own

    agent = Agent(url)
    loop = GLib.MainLoop()
    agent.loop = loop
    if not agent._prepare():
        return 3                        # no session bus / no object to export

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_a: agent.quit())
        except Exception:
            pass

    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        STATE.write_text(json.dumps({
            "pid": os.getpid(), "url": url, "at": time.time()}) + "\n",
            encoding="utf-8")
    except Exception:
        pass

    GLib.timeout_add(POLL_MS, agent.tick)
    try:
        loop.run()
    finally:
        agent.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
