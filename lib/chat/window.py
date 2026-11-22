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
import os
import sys
import threading
import time
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

def _sibling(name: str):
    """Import a module that sits next to this file (the host-side helpers)."""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("jmp_" + name[:-3], path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


#: the GNOME global media-key bindings (read/write from the *host*)
MEDIA_HOST = _sibling("media_keys_host.py")

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

#: Keysym names a keysym-less keyboard sends when its media row is in
#: "function key" mode instead of "multimedia" mode: the keys are printed with
#: the media symbols but arrive as plain F5–F8 (or F13–F16).  They are mapped
#: to the same actions only while no modifier is held, so Ctrl+F5 and friends
#: stay what they were.
#:
#: The order is per keyboard, not universal.  It was read off the `dakai
#: PS/2+USB Keyboard` on this machine (2026-11-22), whose row prints stop,
#: rewind, play/pause and forward on F5, F6, F7, F8 — the earlier toggle,
#: stop, prev, next guess put play/pause on F7 (restarting the track) and
#: rewind on F6 (stopping the music), which is exactly what was reported.
FKEY_MEDIA = {
    "F5": "stop",
    "F6": "prev",
    "F7": "toggle",
    "F8": "next",
    "F13": "stop",
    "F14": "prev",
    "F15": "toggle",
    "F16": "next",
}

#: the actions a key may be bound to; anything else in an override is ignored
MEDIA_ACTIONS = frozenset({"toggle", "pause", "resume", "stop", "next", "prev"})

#: an override, so a wrongly guessed row never needs a rebuild:
#:   {"F5": "stop", "F6": "prev", "F7": "toggle", "F8": "next"}
#: at this path (or JMP_MEDIA_KEYS="F5=stop,F6=prev,…").
MEDIA_MAP_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or
    os.path.join(os.path.expanduser("~"), ".config"),
    APP_ID, "media-keys.json")


def apply_media_overrides(mapping: dict[str, str] | None = None) -> dict[str, str]:
    """Merge a user mapping over the built-in one; bad entries are ignored."""
    wanted = dict(mapping or {})
    env = os.environ.get("JMP_MEDIA_KEYS", "")
    for part in env.replace(";", ",").split(","):
        name, _, action = part.partition("=")
        if name.strip() and action.strip():
            wanted[name.strip()] = action.strip()
    if not wanted and os.path.isfile(MEDIA_MAP_FILE):
        try:
            with open(MEDIA_MAP_FILE, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                wanted.update({str(k): str(v) for k, v in loaded.items()})
        except Exception:
            pass
    for name, action in wanted.items():
        action = str(action).strip().casefold()
        if action not in MEDIA_ACTIONS:
            continue
        if name.startswith(("XF86", "Media", "Audio")):
            MEDIA_KEYS[name] = action
        elif name.startswith("F") and name[1:].isdigit():
            FKEY_MEDIA[name] = action
    return dict(FKEY_MEDIA)


apply_media_overrides()

#: set JMP_KEYLOG=/path/to/file to append every key the window and the page
#: receive, which is how an unlabelled media row is identified.  Without it only
#: a media key is written: that is what makes a later "the keys do nothing"
#: report diagnosable from the log, and it can never contain typing.
KEYLOG_PATH = os.environ.get("JMP_KEYLOG", "")
#: the always-on log (media keys only), kept small on purpose
KEYLOG_FILE = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or
    os.path.join(os.path.expanduser("~"), ".cache"),
    APP_ID, "media-keys.log")
KEYLOG_MAX = 64 * 1024


def _media_name(name: str) -> bool:
    """True for a key a media row can send (never an ordinary character key)."""
    if not name:
        return False
    if name.startswith("F") and name[1:].isdigit():
        return True                 # F1–F24: only a media row maps to an action
    return (name in MEDIA_KEYS or name in FKEY_MEDIA or
            name.startswith(("XF86", "Media", "Audio")))


def _keylog(tag, name, *rest, always: bool = False) -> None:
    """Append one line: when, who saw it, the key, then the raw details.

    The timestamp matters: it is how a press is matched to the action and to
    the player's answer after the fact, without asking the listener to repeat
    the order in words.
    """
    if not always and not KEYLOG_PATH and not _media_name(str(name)):
        return
    path = KEYLOG_PATH or KEYLOG_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > KEYLOG_MAX:
                with open(path, "rb") as fh:
                    kept = fh.read()[-KEYLOG_MAX // 2:]
                with open(path, "wb") as fh:
                    fh.write(kept)
        except OSError:
            pass
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(stamp + "\t" +
                     "\t".join(str(p) for p in (tag, name) + rest) + "\n")
    except Exception:
        pass


def _modifier_mask(Gdk) -> int:
    """Every "a modifier is held" bit this GTK generation offers.

    GTK3 has ``META_MASK``, GTK4 dropped it for ``SUPER_MASK``; asking for a
    missing name would raise inside a key handler and eat the key, so the list
    is probed instead of assumed.
    """
    mask = 0
    for name in ("SHIFT_MASK", "CONTROL_MASK", "ALT_MASK", "MOD1_MASK",
                 "SUPER_MASK", "META_MASK", "HYPER_MASK"):
        mask |= int(getattr(Gdk.ModifierType, name, 0) or 0)
    return mask


def _eval_js(view, script: str) -> None:
    """Run a script in the page, on WebKit2 4.x and WebKit 6.0 alike."""
    try:
        view.run_javascript(script, None, None, None)       # WebKit2 4.x
        return
    except Exception:
        pass
    try:
        view.evaluate_javascript(script, -1, None, None, None,
                                 None, None)                # WebKit 6.0
    except Exception:
        pass


def _bridge_host(view) -> None:
    """Let the page read and set the GNOME global media-key bindings.

    The server runs in a container without a session bus, so dconf is only
    reachable from this window.  The page posts ``{id, op}`` to ``jmphost``
    and gets the answer back through ``window.jmpHostAnswer``.
    """
    if MEDIA_HOST is None:
        return

    def _received(_manager, value) -> None:
        try:
            text = value.get_js_value().to_string()
        except Exception:
            text = str(value)
        try:
            payload = json.loads(text or "{}")
        except Exception:
            payload = {}
        op = str(payload.get("op") or "status")
        handler = {"install": MEDIA_HOST.install,
                   "remove": MEDIA_HOST.remove}.get(op, MEDIA_HOST.status)
        try:
            answer = handler()
        except Exception as error:                     # never break the page
            answer = {"available": False, "installed": False, "rows": [],
                      "ok": False, "error": str(error)}
        answer = dict(answer or {})
        answer["view"] = "media-keys"
        answer["op"] = op
        answer["id"] = payload.get("id")
        _eval_js(view, "window.jmpHostAnswer && window.jmpHostAnswer("
                       + json.dumps(answer) + ");")

    try:
        manager = view.get_user_content_manager()
        manager.register_script_message_handler("jmphost")
    except Exception:
        return
    try:
        manager.connect("script-message-received::jmphost", _received)
    except Exception:
        pass


def _bridge_keys(view) -> None:
    """Let the page report which keys it saw, for the same diagnosis.

    WebKitGTK can consume a hardware key before the window's own handler runs.
    When that happens the page's ``keydown`` is the only witness, so it posts
    the key name to this message handler and the window logs it next to the
    keys it saw itself.
    """

    def _received(_manager, value) -> None:
        try:
            text = value.get_js_value().to_string()
        except Exception:
            text = str(value)
        try:
            name = json.loads(text).get("key", "")
        except Exception:
            name = ""
        _keylog("page", name, text)

    try:
        manager = view.get_user_content_manager()
        manager.register_script_message_handler("jmpkeys")
    except Exception:
        return
    for signal in ("script-message-received::jmpkeys",):
        try:
            manager.connect(signal, _received)
        except Exception:
            pass


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
        self.view = None            # the WebView, so a hint can reach the page
        self.status: dict = {}
        self.state = "Stopped"
        self.track_key = None
        self._focus_only = None     # cached: are global bindings missing?
        self._hinted = False

    def _global_missing(self) -> bool:
        """True when the keys are focus-only and the desktop could fix that.

        Measured once per window: reading it on every press would mean four
        ``gsettings`` calls per key, and the state is not what changes mid-run.
        """
        if self._focus_only is None:
            state = {}
            if MEDIA_HOST is not None:
                try:
                    state = MEDIA_HOST.status()
                except Exception:
                    state = {}
            self._focus_only = bool(state.get("available")) \
                and not state.get("installed")
        return bool(self._focus_only)

    def notify_hint(self) -> None:
        """Ask the page once to offer global bindings, after a real key press.

        This is the honest moment for it: the listener just pressed a key and
        it worked, so the one thing that would surprise them later — it stops
        working when another window has focus — is worth a single prompt.
        """
        if self._hinted or self.view is None or not self._global_missing():
            return
        self._hinted = True
        _eval_js(self.view, "window.jmpMediaKeysHint && "
                            "window.jmpMediaKeysHint();")

    def act(self, action: str, value=None) -> None:
        def _send() -> None:
            try:
                answer = self.server.transport(action, value) or {}
            except Exception:
                _keylog("sent", action, "(no answer)", always=True)
                return          # a closing server is not a crash
            text = str(answer.get("text") or answer.get("error") or "")
            _keylog("sent", action, text[:120], always=True)
        threading.Thread(target=_send, daemon=True).start()

    def key(self, name: str | None, plain: bool = True) -> str:
        """Run the transport action a keysym means; ``""`` if it means none.

        ``plain`` is false when a modifier is held: an unmodified F5 may be the
        media key of a keyboard whose row is in function mode, but Ctrl+F5 is a
        shortcut and must keep working as one.  The action is returned so the
        key log can say which one a press really became.
        """
        action = MEDIA_KEYS.get(name or "")
        if action is None and plain:
            action = FKEY_MEDIA.get(name or "")
        if not action:
            return ""
        self.act(action)
        return action

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
            # REPLACE (not NONE): the window is the real player while it is
            # open, so it takes the name from the host agent, which owns it
            # only to survive a closed window.  The agent in turn owns it with
            # ALLOW_REPLACEMENT and retries after the window closes.
            Gio.bus_own_name_on_connection(
                self.connection, MPRIS_NAME, Gio.BusNameOwnerFlags.REPLACE,
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
        name = Gdk.keyval_name(event.keyval)
        plain = not (event.state & _modifier_mask(Gdk))
        action = media.key(name, plain)
        _keylog("gtk3", name, event.keyval, event.hardware_keycode,
                int(event.state), action or "-")
        if action:
            media.notify_hint()
        return bool(action)

    window.connect("key-press-event", _key)

    view = WebKit2.WebView()
    # the page records short voice commands; WebKit asks before getusermedia
    try:
        view.get_settings().set_property("enable-media-stream", True)
    except Exception:
        pass
    _allow_media(view)
    _bridge_keys(view)
    _bridge_host(view)
    media.view = view
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

    def _key(_controller, keyval, keycode, state) -> bool:
        name = Gdk.keyval_name(keyval)
        plain = not (state & _modifier_mask(Gdk))
        action = media.key(name, plain)
        _keylog("gtk4", name, keyval, keycode, int(state), action or "-")
        if action:
            media.notify_hint()
        return bool(action)

    controller.connect("key-pressed", _key)
    window.add_controller(controller)

    view = WebKit.WebView()
    try:
        view.get_settings().set_property("enable-media-stream", True)
    except Exception:
        pass
    _allow_media(view)
    _bridge_keys(view)
    _bridge_host(view)
    media.view = view
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
