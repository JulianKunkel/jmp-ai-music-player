"""A native GNOME window for the chat front end (optional).

``music chat --web`` serves the UI over HTTP.  A Chromium ``--app`` window
works wherever a Chromium browser exists, but on a GNOME desktop a GTK/WebKit
window looks and behaves like a native application.  This module opens one
when PyGObject and WebKitGTK are installed on the host; otherwise it exits with
``3`` and the caller falls back to the browser app window.

It is standalone on purpose (no package imports), so the docker launcher can
run it with the host's ``python3`` even though the app code lives in the
image:

    python3 window.py --check          # prints gtk4/gtk3, exit 3 if unusable
    python3 window.py --backend gtk4 URL
    python3 window.py URL              # detect, then show

Only the UI is shown here; every action goes to the server started by
``music chat --web`` (in the container, for the docker install).
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

APP_ID = "julians-music-app"
APP_TITLE = "JulAIn Music Player"
#: the size the window opens with: wide enough for the 940 px settings modal
#: and tall enough for the eight tabs plus the sticky Save row
WINDOW_SIZE = (1440, 960)
#: the smallest size that still shows every control (the modal itself scrolls
#: below 760 px of height, so 640 is honest on a 768 px laptop screen); the
#: window refuses to go below it instead of clipping the settings panel
MIN_SIZE = (1024, 640)


def _versions(namespace: str) -> set[str]:
    """Installed typelib versions, without loading the namespace."""
    import gi

    try:
        repo = gi.Repository.get_default()
        return set(repo.enumerate_versions(namespace))
    except Exception:
        return set()


def detect() -> str | None:
    """``gtk4``, ``gtk3`` or ``None`` — chosen from the installed typelibs."""
    try:
        import gi  # noqa: F401
    except Exception:
        return None
    gtk = _versions("Gtk")
    if "4.0" in gtk and "6.0" in _versions("WebKit"):
        return "gtk4"
    if "3.0" in gtk and {"4.1", "4.0"} & _versions("WebKit2"):
        return "gtk3"
    return None


#: the permission requests the chat page may need: recording a spoken command
#: (``getUserMedia``) and enumerating the capture device for it.  Everything
#: else WebKit asks about (geolocation, notifications, …) stays denied.
MEDIA_REQUESTS = ("UserMediaPermissionRequest", "DeviceInfoPermissionRequest")


def _allow_media(view) -> None:
    """Answer WebKitGTK's permission request for the microphone with *allow*.

    WebKitGTK never asks the user itself — an embedder that does not connect
    ``permission-request`` has every request denied, which is why the page's
    ``getUserMedia`` failed with "the microphone is not available" even though
    the device was there.  The signal is connected defensively because its
    name differs between the 4.x and 6.0 API generations.
    """

    def _asked(_view, request) -> bool:
        if type(request).__name__ not in MEDIA_REQUESTS:
            return False            # leave it to WebKit's own default (deny)
        try:
            request.allow()
        except Exception:
            return False
        return True

    try:
        view.connect("permission-request", _asked)
    except Exception:
        pass                        # a WebKit without the signal: no mic there


#: the media keys a keyboard sends (behind F5–F8 on many laptops) and the
#: transport action each one means.  GTK delivers them under these keysym
#: names; a browser delivers the ``Media*`` names, which the page handles.
MEDIA_KEYS = {
    "XF86AudioPlay": "toggle",
    "XF86AudioPause": "pause",
    "XF86AudioStop": "stop",
    "XF86AudioNext": "next",
    "XF86AudioPrev": "prev",
    "XF86AudioMedia": "toggle",
}

MPRIS_NAME = "org.mpris.MediaPlayer2.jmp"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_ROOT = "org.mpris.MediaPlayer2"
MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"

#: the two interfaces the desktop's media keys talk to.  `gsd-media-keys`
#: (GNOME) and `playerctl` send PlayPause/Next/Previous/Stop to the MPRIS
#: player that owns the name, so the keys work while the window is in the
#: background or minimised — not only while it has focus.
MPRIS_XML = f"""<node>
  <interface name="{MPRIS_ROOT}">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="{MPRIS_PLAYER}">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg name="Offset" type="x" direction="in"/>
    </method>
    <method name="SetPosition">
      <arg name="TrackId" type="o" direction="in"/>
      <arg name="Position" type="x" direction="in"/>
    </method>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Metadata" type="a{{sv}}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="Rate" type="d" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanControl" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
  </interface>
</node>"""


class _Server:
    """The loopback HTTP API of the running server, with its token."""

    def __init__(self, url: str):
        parsed = urlparse(url)
        self.base = f"{parsed.scheme}://{parsed.netloc}"
        self.token = (parse_qs(parsed.query).get("t") or [""])[0]

    def _call(self, path: str, payload: dict | None = None,
              timeout: float = 4.0) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base}{path}?t={quote(self.token)}", data=data,
            headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8") or "{}"
        return json.loads(raw)

    def transport(self, action: str, value=None) -> dict:
        payload: dict = {"action": action}
        if value is not None:
            payload["value"] = value
        return self._call("/api/transport", payload)

    def status(self) -> dict:
        return self._call("/api/status", timeout=3.0)


class _Media:
    """Media keys and MPRIS for one window, backed by the HTTP API.

    Two routes, one meaning: the focused GTK window translates a keysym itself
    (``key``), and the desktop's media keys arrive as MPRIS calls even when the
    window is not focused.  Both end in ``POST /api/transport``, so a key does
    exactly what the matching button does.  Nothing here touches a file.
    """

    def __init__(self, url: str):
        self.server = _Server(url)
        self.connection = None
        self.present = None
        self.status: dict = {}
        self.state = "Stopped"
        self.track_key = None

    def act(self, action: str, value=None) -> None:
        def _send() -> None:
            try:
                self.server.transport(action, value)
            except Exception:
                pass                # a closing server is not a crash
        threading.Thread(target=_send, daemon=True).start()

    def key(self, name: str | None) -> bool:
        action = MEDIA_KEYS.get(name or "")
        if not action:
            return False
        self.act(action)
        return True

    # -- MPRIS -------------------------------------------------------------

    def install(self, present) -> bool:
        """Own org.mpris.MediaPlayer2.jmp on the session bus, if there is one."""
        self.present = present
        try:
            from gi.repository import Gio, GLib
        except Exception:
            return False
        try:
            self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception:
            return False
        if self.connection is None:
            return False
        node = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
        for interface in node.interfaces:
            try:
                self.connection.register_object(
                    MPRIS_PATH, interface, self._method, self._property, None)
            except Exception:
                return False
        try:
            Gio.bus_own_name_on_connection(
                self.connection, MPRIS_NAME, Gio.BusNameOwnerFlags.NONE,
                None, None)
        except Exception:
            return False
        self.status = {}
        self.state = "Stopped"
        self.track_key = None
        self.refresh()
        GLib.timeout_add_seconds(2, self.refresh)
        return True

    def _method(self, _conn, _sender, _path, iface, method, params, invocation):
        try:
            if iface == MPRIS_ROOT:
                if method == "Raise" and self.present is not None:
                    self.present()
            elif iface == MPRIS_PLAYER:
                if method == "Next":
                    self.act("next")
                elif method == "Previous":
                    self.act("prev")
                elif method == "Pause":
                    self.act("pause")
                elif method == "PlayPause":
                    self.act("toggle")
                elif method == "Stop":
                    self.act("stop")
                elif method == "Play":
                    self.act("resume")
                elif method == "Seek":
                    self.act("seek", float(params[0]) / 1_000_000.0)
                elif method == "SetPosition":
                    self.act("seek_abs", float(params[1]) / 1_000_000.0)
            invocation.return_value(None)
        except Exception as exc:
            try:
                invocation.return_dbus_error(
                    "org.mpris.MediaPlayer2.Error.Failed", str(exc))
            except Exception:
                pass

    def _metadata(self):
        from gi.repository import GLib

        status = self.status or {}
        rel = str(status.get("rel") or "")
        title = str(status.get("title") or "")
        artist = str(status.get("artist") or "")
        duration = float(status.get("duration") or 0)
        trackid = ("/org/mpris/MediaPlayer2/Track/" +
                   (hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
                    if rel else "none"))
        values = {"mpris:trackid": GLib.Variant("o", trackid)}
        if duration > 0:
            values["mpris:length"] = GLib.Variant("x", int(duration * 1_000_000))
        if title:
            values["xesam:title"] = GLib.Variant("s", title)
        if artist:
            values["xesam:artist"] = GLib.Variant("as", [artist])
        return GLib.Variant("a{sv}", values)

    def _property(self, _conn, _sender, _path, iface, name):
        from gi.repository import GLib

        if iface == MPRIS_ROOT:
            root = {
                "Identity": GLib.Variant("s", APP_TITLE),
                "DesktopEntry": GLib.Variant("s", APP_ID),
                "CanQuit": GLib.Variant("b", False),
                "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False),
                "SupportedUriSchemes": GLib.Variant("as", []),
                "SupportedMimeTypes": GLib.Variant("as", []),
            }
            return root.get(name)
        if iface != MPRIS_PLAYER:
            return None
        volume = max(0.0, min(1.0, float(self.status.get("volume") or 0) / 100.0))
        player = {
            "PlaybackStatus": GLib.Variant("s", self.state),
            "Metadata": self._metadata(),
            "Volume": GLib.Variant("d", volume),
            "Position": GLib.Variant(
                "x", int(float(self.status.get("position") or 0) * 1_000_000)),
            "Rate": GLib.Variant("d", 1.0),
            "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "CanControl": GLib.Variant("b", True),
            "CanPlay": GLib.Variant("b", True),
            "CanPause": GLib.Variant("b", True),
            "CanGoNext": GLib.Variant("b", True),
            "CanGoPrevious": GLib.Variant("b", True),
            "CanSeek": GLib.Variant("b", True),
        }
        return player.get(name)

    def refresh(self) -> bool:
        """Poll the server lightly so MPRIS reports the real state."""
        try:
            status = self.server.status()
        except Exception:
            return True
        self.status = status
        if status.get("playing"):
            state = "Paused" if status.get("paused") else "Playing"
        else:
            state = "Stopped"
        changed: dict = {}
        if state != self.state:
            self.state = state
            from gi.repository import GLib
            changed["PlaybackStatus"] = GLib.Variant("s", state)
        track_key = (status.get("rel"), status.get("title"), status.get("artist"))
        if track_key != self.track_key:
            self.track_key = track_key
            from gi.repository import GLib
            changed["Metadata"] = self._metadata()
        if changed and self.connection is not None:
            from gi.repository import GLib
            try:
                self.connection.emit_signal(
                    None, MPRIS_PATH, "org.freedesktop.DBus.Properties",
                    "PropertiesChanged",
                    GLib.Variant("(sa{sv}as)", (MPRIS_PLAYER, changed, [])))
            except Exception:
                pass
        return True


def _gtk3(url: str) -> None:
    import gi

    gi.require_version("Gtk", "3.0")
    # Gdk must be pinned too: importing it without a version picks 4.0 on a
    # machine that has both toolkits, and Gtk 3.0 then refuses to load.
    gi.require_version("Gdk", "3.0")
    try:
        gi.require_version("WebKit2", "4.1")
    except ValueError:
        gi.require_version("WebKit2", "4.0")
    from gi.repository import Gdk, GLib, Gtk, WebKit2

    GLib.set_prgname(APP_ID)
    try:
        # the desktop file id, so GNOME groups the window with the launcher
        # (X11 via StartupWMClass, Wayland via the app_id)
        Gdk.set_program_class(APP_ID)
    except AttributeError:
        pass

    window = Gtk.Window()
    window.set_title(APP_TITLE)
    window.set_default_size(*WINDOW_SIZE)
    # a minimum size, so shrinking the window cannot clip the tabs or the
    # settings panel; GTK asks the toplevel before it lets the WM resize it
    window.set_size_request(*MIN_SIZE)
    window.set_icon_name(APP_ID)
    window.connect("destroy", Gtk.main_quit)

    # media keys: the focused window translates the keysym itself, and MPRIS
    # lets the desktop's own media keys reach the player in the background
    media = _Media(url)

    def _key(_widget, event) -> bool:
        return media.key(Gdk.keyval_name(event.keyval))

    window.connect("key-press-event", _key)

    view = WebKit2.WebView()
    # the page records short voice commands; WebKit asks before getusermedia
    try:
        view.get_settings().set_property("enable-media-stream", True)
    except Exception:
        pass
    _allow_media(view)
    view.load_uri(url)
    window.add(view)
    window.show_all()
    media.install(window.present)
    Gtk.main()


def _gtk4(url: str) -> None:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gdk, GLib, Gtk, WebKit

    GLib.set_prgname(APP_ID)
    Gtk.init()

    window = Gtk.Window()
    window.set_title(APP_TITLE)
    window.set_default_size(*WINDOW_SIZE)
    # a minimum size, so shrinking the window cannot clip the tabs or the
    # settings panel; GTK asks the toplevel before it lets the WM resize it
    window.set_size_request(*MIN_SIZE)
    window.set_icon_name(APP_ID)

    media = _Media(url)
    controller = Gtk.EventControllerKey()

    def _key(_controller, keyval, _keycode, _state) -> bool:
        return media.key(Gdk.keyval_name(keyval))

    controller.connect("key-pressed", _key)
    window.add_controller(controller)

    view = WebKit.WebView()
    try:
        view.get_settings().set_property("enable-media-stream", True)
    except Exception:
        pass
    _allow_media(view)
    view.load_uri(url)
    window.set_child(view)
    loop = GLib.MainLoop()

    def _closed(*_args) -> bool:
        loop.quit()
        return False

    window.connect("close-request", _closed)
    window.present()
    media.install(window.present)
    loop.run()


BACKENDS = {"gtk4": _gtk4, "gtk3": _gtk3}


def _usage() -> int:
    print("usage: window.py [--check] [--backend gtk3|gtk4] URL",
          file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args[:1] == ["--check"]:
        backend = detect()
        if backend is None:
            return 3
        print(backend)
        return 0

    backend = None
    if args[:1] == ["--backend"]:
        if len(args) < 3 or args[1] not in BACKENDS:
            return _usage()
        backend, args = args[1], args[2:]
    elif not args or args[0].startswith("-"):
        return _usage()

    if not args:
        return _usage()
    url = args[0]
    backend = backend or detect()
    if backend is None:
        return 3
    try:
        BACKENDS[backend](url)
    except Exception as exc:  # a broken WebKit install: let the caller retry
        print(f"window.py: {backend} window failed: {exc}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
