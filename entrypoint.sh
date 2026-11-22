#!/bin/sh
# Entry point of the JulAIn Music Player image: everything goes to the app's
# `music.py`.  The app code is baked in at /opt/music/app, but a checkout
# mounted at /library always wins (so the image can never run stale code).
#
#   docker run --rm -it -v "$PWD:/library" julians-music-app:latest play Bands
#   docker run --rm -it -v "$PWD:/library" julians-music-app:latest bash
set -eu

LIBRARY="${LIBRARY:-/library}"
PYTHON="${PYTHON:-/opt/music/venv/bin/python}"
APP_HOME="${APP_HOME:-/opt/music/app}"
export LIBRARY PYTHON APP_HOME
# the app resolves the library through MUSIC_LIBRARY; inside the container the
# mount is the library, even when it is a plain folder without Playlists/
export MUSIC_LIBRARY="$LIBRARY"
export MPV_SOCKET="${MPV_SOCKET:-/tmp/mpv.sock}"
export MPV_LOG="${MPV_LOG:-/tmp/mpv.log}"

ENTRY="$APP_HOME/music.py"
if [ -f "$LIBRARY/application/lib/music.py" ]; then
    ENTRY="$LIBRARY/application/lib/music.py"
fi

cd "$LIBRARY" 2>/dev/null || cd /tmp

case "${1:-}" in
    ""|help|-h|--help)
        exec "$PYTHON" "$ENTRY" --help
        ;;
    bash|sh|zsh)
        exec "$1"
        ;;
esac

exec "$PYTHON" "$ENTRY" "$@"
