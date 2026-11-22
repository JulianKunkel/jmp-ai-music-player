#!/usr/bin/env bash
# One-stop installer for JulAIn Music Player: container first, host as fallback.
#
# It prepares everything the launcher needs and then opens the app:
#
#   1. installs the system pieces (PyGObject + WebKitGTK for a native window,
#      docker for the container) when the package manager allows it,
#   2. builds the container image from this folder,
#   3. writes a compose project that mounts your music folder at /library,
#      runs as your uid/gid and passes the host sound device through,
#   4. installs ~/.local/bin/music-cli, the .desktop entry and the icon,
#   5. starts the chat window when a display is available.
#
# The launcher opens the chat as its own window (`chat --web`): the server runs
# in the container and the host browser shows the page, so clicking the app is
# all that is needed afterwards.
#
# When no container runtime can be made to work, the installer falls back to
# the host build instead of refusing (--no-fallback keeps the old behaviour).
# The host build needs no docker at all: application/install.sh --host.
#
#   application/install-desktop.sh                    # ~/Musik if present, else repo
#   application/install-desktop.sh --library ~/Music   # a different folder
#   application/install-desktop.sh --dry-run           # print the plan, write nothing
#   application/install-desktop.sh --no-build          # reuse the image
#   application/install-desktop.sh --prefix /usr/local # system-wide (sudo)
#   application/install-desktop.sh --no-fallback       # fail when docker is absent
#   application/install-desktop.sh --no-launch         # install, start nothing
#   application/install-desktop.sh --uninstall
#
# Every other option is passed to install.sh unchanged; run
# `application/install.sh --help` for the full list.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

args=()
fallback=1
launch=""

for arg in "$@"; do
    case "$arg" in
        --host)
            echo "error: install-desktop.sh always installs the docker build." >&2
            echo "       For the host build run: $HERE/install.sh --host" >&2
            exit 2
            ;;
        --docker|--container)
            # the backend is fixed below, so do not pass a duplicate
            ;;
        --no-fallback|--strict) fallback=0 ;;
        --fallback-host) fallback=1 ;;
        --launch) launch=1 ;;
        --no-launch) launch=0 ;;
        *)
            args+=("$arg")
            ;;
    esac
done

# "one command" means the container build, a host build when that is the only
# way to run, and the window on screen when there is a display to show it on.
extra=()
if [ "$fallback" = 1 ]; then
    extra+=(--fallback-host)
fi
if [ -z "$launch" ]; then
    if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
        launch=1
    else
        launch=0
    fi
fi
if [ "$launch" = 1 ]; then
    extra+=(--launch)
else
    extra+=(--no-launch)
fi

exec "$HERE/install.sh" --docker "${extra[@]}" "${args[@]}"
