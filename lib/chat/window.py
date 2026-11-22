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

import sys

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
    Gtk.main()


def _gtk4(url: str) -> None:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import GLib, Gtk, WebKit

    GLib.set_prgname(APP_ID)
    Gtk.init()

    window = Gtk.Window()
    window.set_title(APP_TITLE)
    window.set_default_size(*WINDOW_SIZE)
    # a minimum size, so shrinking the window cannot clip the tabs or the
    # settings panel; GTK asks the toplevel before it lets the WM resize it
    window.set_size_request(*MIN_SIZE)
    window.set_icon_name(APP_ID)
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
