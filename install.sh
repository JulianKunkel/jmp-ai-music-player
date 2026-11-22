#!/usr/bin/env bash
# Install JulAIn Music Player on this computer.
#
# Two ways to run it:
#
#   container (what install-desktop.sh installs)
#     builds the image from this folder and installs a launcher that starts a
#     container with your music folder mounted at /library
#
#   host (no docker)
#     installs the CLI plus its own Python venv and runs it directly
#
#   application/install.sh --docker            # container-backed launcher
#   application/install.sh --host              # direct CLI + venv
#   application/install.sh --docker --dry-run  # print what would be done
#   application/install.sh --library ~/Musik   # which folder to mount
#   application/install.sh --llm-config FILE   # optional chat planner config
#   application/install.sh --modules DIR       # optional download module checkout
#   application/install.sh --prefix /usr/local # system-wide (run with sudo)
#   application/install.sh --uninstall
#   application/install.sh --no-system-deps    # do not install system packages
#   application/install.sh --fallback-host     # container impossible -> host
#   application/install.sh --no-fallback       # fail instead of falling back
#   application/install.sh --launch            # start the window at the end
#   application/install.sh --no-launch         # install only, start nothing
#
# System packages (PyGObject + WebKitGTK for the native window, mpv for
# playback, docker for --docker) are installed with the distribution's package
# manager when that is possible; without root the exact command is printed and
# the install continues with a browser window.  --no-system-deps skips it.
#
# --docker falls back to the host build when no container runtime can be made
# to work (--no-fallback/--strict refuses instead).  After the install the app
# window is started when a display is available (--launch / --no-launch).
#
# Audio download is not part of this repository.  A build that should have it
# points --modules (or $JMP_MODULES) at a checkout of an optional download
# module; the build copies and byte-compiles it into lib/modules.  Without one
# the installed player simply has no download feature.
#
# Only these paths are written:
#
#   ~/.local/share/julians-music-app/          launcher config + compose file
#   ~/.local/bin/music-cli                     the launcher
#   ~/.local/share/applications/julians-music-app.desktop
#   ~/.local/share/icons/hicolor/scalable/apps/julians-music-app.svg
#
# The music library is never modified.  A replaced file keeps a `.bak`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="julians-music-app"
APP_NAME="$APP_ID.desktop"
ICON_NAME="$APP_ID.svg"
WRAPPER_NAME="music-cli"
LEGACY_NAMES="music-library"
LIBRARY_EXPLICIT=""

MODE=install
TARGET=host
DRY_RUN=0
PREFIX=""
LIBRARY=""
APP_HOME=""
PYTHON="${PYTHON:-python3}"
USE_VENV=1
INSTALL_DEPS=1
INSTALL_DESKTOP=1
FORCE=0
IMAGE="julians-music-app:latest"
BUILD_IMAGE=1
DOCKER_BIN="${DOCKER:-}"
LLM_CONFIG=""
MODULES="${JMP_MODULES:-$HERE/download-modules}"
SYSTEM_DEPS=1
FALLBACK_HOST=0
LAUNCH=auto
# how long the launch waits for the chat server (seconds)
LAUNCH_WAIT="${LAUNCH_WAIT:-30}"

usage() {
    # everything between the shebang and the `set` line, uncommented
    awk 'NR == 1 { next } /^set -euo pipefail$/ { exit }
         { sub(/^# ?/, ""); print }' "$0"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --docker|--container) TARGET=docker ;;
        --host) TARGET=host ;;
        --uninstall) MODE=uninstall ;;
        --dry-run) DRY_RUN=1 ;;
        --prefix) PREFIX="${2:?--prefix needs a directory}"; shift ;;
        --library) LIBRARY="${2:?--library needs a directory}"; LIBRARY_EXPLICIT=1; shift ;;
        --app-home) APP_HOME="${2:?--app-home needs a directory}"; shift ;;
        --python) PYTHON="${2:?--python needs an interpreter}"; shift ;;
        --image) IMAGE="${2:?--image needs a name}"; shift ;;
        --docker-bin) DOCKER_BIN="${2:?--docker-bin needs a path}"; shift ;;
        --llm-config) LLM_CONFIG="${2:?--llm-config needs a file}"; shift ;;
        --modules) MODULES="${2:?--modules needs a directory}"; shift ;;
        --system-deps) SYSTEM_DEPS=1 ;;
        --no-system-deps) SYSTEM_DEPS=0 ;;
        --fallback-host) FALLBACK_HOST=1 ;;
        --no-fallback|--strict) FALLBACK_HOST=0 ;;
        --launch) LAUNCH=1 ;;
        --no-launch) LAUNCH=0 ;;
        --no-build) BUILD_IMAGE=0 ;;
        --no-venv) USE_VENV=0 ;;
        --no-deps) INSTALL_DEPS=0 ;;
        --no-desktop) INSTALL_DESKTOP=0 ;;
        --force) FORCE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ -n "$PREFIX" ]; then
    DATA_HOME="${PREFIX%/}/share"
    BIN_HOME="${PREFIX%/}/bin"
else
    DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
    BIN_HOME="${XDG_BIN_HOME:-$HOME/.local/bin}"
fi

APP_HOME="${APP_HOME:-$DATA_HOME/$APP_ID}"
APP_DIR="$APP_HOME/lib"
VENV_DIR="$APP_HOME/venv"
APP_PY="$VENV_DIR/bin/python"
COMPOSE_FILE="$APP_HOME/compose.yaml"
DESKTOP_FILE="$DATA_HOME/applications/$APP_NAME"
ICON_FILE="$DATA_HOME/icons/hicolor/scalable/apps/$ICON_NAME"
WRAPPER_FILE="$BIN_HOME/$WRAPPER_NAME"
VERSION_FILE="$APP_HOME/VERSION"

say() { printf '%s\n' "$*"; }
do_it() {
    if [ "$DRY_RUN" = "1" ]; then
        say "  would: $*"
    else
        "$@"
    fi
}
backup() {
    [ -e "$1" ] || return 0
    if [ "$DRY_RUN" = "1" ]; then
        say "  would back up $1 -> $1.bak"
    else
        cp -p "$1" "$1.bak"
    fi
}
remove_path() {
    if [ "$DRY_RUN" = "1" ]; then
        say "  would remove $1"
    else
        rm -f "$1"
        say "  removed $1"
    fi
}
remove_tree() {
    if [ "$DRY_RUN" = "1" ]; then
        say "  would remove $1"
    else
        rm -rf "$1"
        say "  removed $1"
    fi
}
have() { command -v "$1" >/dev/null 2>&1; }

is_library() {
    [ -d "$1/Playlists" ] \
        && { [ -d "$1/Artists" ] || [ -d "$1/library-work" ]; }
}

# The app's own folder choice (Settings -> Music folder) is remembered in
# library-work/app/chat/settings.json.  The container can only mount one
# folder, so the launcher has to read that file and mount what it names —
# otherwise the choice would only take effect inside a session.
saved_library() {
    file="$1/library-work/app/chat/settings.json"
    [ -f "$file" ] || return 1
    if [ -n "$PYTHON" ] && command -v "$PYTHON" >/dev/null 2>&1; then
        value="$("$PYTHON" - "$file" <<'PYEOF' 2>/dev/null
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
value = data.get("library") if isinstance(data, dict) else None
if isinstance(value, str) and value.strip():
    print(value.strip())
else:
    sys.exit(1)
PYEOF
)" || return 1
    else
        value="$(sed -n 's/.*"library"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$file" | head -1)"
        [ -n "$value" ] || return 1
    fi
    [ -n "$value" ] && [ -d "$value" ] || return 1
    printf '%s\n' "$value"
}

find_docker() {
    if [ -n "$DOCKER_BIN" ]; then
        have "$DOCKER_BIN" && return 0
        # an absolute path was given but does not exist
        [ -x "$DOCKER_BIN" ] && return 0
        return 1
    fi
    if have docker; then DOCKER_BIN=docker; return 0; fi
    if have podman; then DOCKER_BIN=podman; return 0; fi
    return 1
}

# ---- display / window ----------------------------------------------------
display_available() {
    [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]
}

window_backend() {   # prints gtk4|gtk3 and returns 0, else returns 1
    local py backend
    # test hook: the installer's own tests exercise the "install it" path on a
    # machine that does have the toolkit
    [ "${MUSIC_NO_NATIVE_WINDOW:-0}" = "1" ] && return 1
    for py in python3 /usr/bin/python3 /usr/local/bin/python3; do
        have "$py" || continue
        backend="$("$py" "$HERE/lib/chat/window.py" --check 2>/dev/null)" || continue
        case "$backend" in
            gtk3|gtk4) printf '%s\n' "$backend"; return 0 ;;
        esac
    done
    return 1
}

browser_for_window() {   # prints the first chromium-like browser, else nothing
    local b
    for b in chromium chromium-browser google-chrome google-chrome-stable \
             brave-browser microsoft-edge microsoft-edge-stable vivaldi-stable; do
        if have "$b"; then printf '%s\n' "$b"; return 0; fi
    done
    return 1
}

launch_wanted() {
    case "$LAUNCH" in
        1) return 0 ;;
        0) return 1 ;;
        *) display_available ;;
    esac
}

# ---- system packages -----------------------------------------------------
PM=""
APT_UPDATED=0
PKGS=()

is_root() { [ "$(id -u)" = "0" ]; }

can_root() {
    is_root && return 0
    have sudo || return 1
    sudo -n true >/dev/null 2>&1 && return 0
    # an interactive sudo can only be answered from a terminal
    [ -t 0 ]
}

root_cmd() {   # the prefix the printed command would need
    if is_root; then printf ''
    elif have sudo && sudo -n true >/dev/null 2>&1; then printf 'sudo -n'
    else printf 'sudo'
    fi
}

as_root() {
    if is_root; then
        "$@"
    elif have sudo && sudo -n true >/dev/null 2>&1; then
        sudo -n "$@"
    elif have sudo && [ -t 0 ]; then
        sudo "$@"
    else
        sudo -n "$@"      # no root: fails fast, the caller reports it
    fi
}

detect_pkg_manager() {
    local c
    for c in apt-get dnf pacman zypper apk; do
        if have "$c"; then PM="$c"; return 0; fi
    done
    return 1
}

pkg_available() {
    case "$PM" in
        apt-get) apt-cache show "$1" >/dev/null 2>&1 ;;
        dnf)     dnf -q list --available "$1" >/dev/null 2>&1 ;;
        pacman)  pacman -Si "$1" >/dev/null 2>&1 ;;
        zypper)  zypper --quiet --non-interactive search --match-exact "$1" \
                     >/dev/null 2>&1 ;;
        apk)     apk search -q -e -- "$1" >/dev/null 2>&1 ;;
        *)       return 0 ;;
    esac
}

pm_install_args() {
    case "$PM" in
        apt-get) printf 'install -y' ;;
        dnf)     printf 'install -y' ;;
        pacman)  printf -- '-S --noconfirm --needed' ;;
        zypper)  printf -- '--non-interactive install' ;;
        apk)     printf 'add' ;;
    esac
}

apt_refresh() {
    [ "$PM" = apt-get ] || return 0
    [ "$APT_UPDATED" = "1" ] && return 0
    APT_UPDATED=1
    say "  [dep ] the package lists do not know the wanted packages: apt-get update"
    if [ "$DRY_RUN" = "1" ]; then
        say "  would: $(root_cmd) apt-get update -qq"
        return 0
    fi
    if as_root apt-get update -qq >/dev/null 2>&1; then
        say "  [ok  ] package lists refreshed"
    else
        say "  [warn] apt-get update failed; continuing with what is known"
    fi
}

pkgs_for() {   # $1 = intent -> fills PKGS
    PKGS=()
    case "$PM:$1" in
        apt-get:window)
            PKGS=(python3-gi)
            if pkg_available gir1.2-webkit2-4.1 \
               || ! pkg_available gir1.2-webkit-6.0; then
                PKGS+=(gir1.2-webkit2-4.1)
            else
                PKGS+=(gir1.2-webkit-6.0)
            fi ;;
        dnf:window)     PKGS=(python3-gobject webkit2gtk4.1) ;;
        pacman:window)  PKGS=(python-gobject webkit2gtk-4.1) ;;
        zypper:window)  PKGS=(python3-gobject typelib-1_0-WebKit2-4_1) ;;
        apk:window)     PKGS=(py3-gobject3 webkit2gtk-4.1) ;;
        *:player)       PKGS=(mpv); have ffmpeg || PKGS+=(ffmpeg) ;;
        apt-get:docker) PKGS=(docker.io) ;;
        *:docker)       PKGS=(docker) ;;
    esac
}

install_intent() {   # $1 = intent, $2 = human label
    local intent="$1" label="$2" p cmd
    if [ -z "$PM" ]; then
        say "  [warn] no known package manager: install $label by hand"
        return 1
    fi
    pkgs_for "$intent"
    [ "${#PKGS[@]}" -gt 0 ] || return 0
    # apt-get cannot see a package before its lists exist
    for p in "${PKGS[@]}"; do
        if ! pkg_available "$p"; then
            apt_refresh
            break
        fi
    done
    cmd="$PM $(pm_install_args) ${PKGS[*]}"
    if [ "$DRY_RUN" = "1" ]; then
        say "  would install $label: $(root_cmd) $cmd"
        return 0
    fi
    if ! can_root; then
        say "  [warn] $label is missing and root is not available."
        say "         Install it with: $(root_cmd) $cmd"
        return 1
    fi
    say "  [dep ] installing $label: ${PKGS[*]}"
    # shellcheck disable=SC2086  # pm_install_args is a word list on purpose
    if ! as_root "$PM" $(pm_install_args) "${PKGS[@]}" >/dev/null 2>&1; then
        say "  [warn] installing ${PKGS[*]} failed; continuing without it"
        say "         Install it with: $(root_cmd) $cmd"
        return 1
    fi
    return 0
}

bootstrap_window_deps() {
    local backend
    if backend="$(window_backend)"; then
        say "  [ok  ] window: native GTK/WebKit ($backend)"
        return 0
    fi
    if [ "$SYSTEM_DEPS" = "1" ]; then
        install_intent window "the native window (PyGObject + WebKitGTK)"
    fi
    if backend="$(window_backend)"; then
        say "  [ok  ] window: native GTK/WebKit ($backend)"
        return 0
    fi
    say "  [warn] no native window; a browser window will be used instead"
    say "         Debian/Ubuntu: sudo apt-get install python3-gi gir1.2-webkit2-4.1"
    return 1
}

bootstrap_player_deps() {
    local backend
    if have mpv; then
        say "  [ok  ] player: mpv"
        return 0
    fi
    if [ "$SYSTEM_DEPS" = "1" ]; then
        install_intent player "mpv"
    fi
    if have mpv; then
        say "  [ok  ] player: mpv"
        return 0
    fi
    say "  [warn] mpv is missing: playback will not work in the host build"
    return 1
}

bootstrap_docker_deps() {
    if docker_ready; then
        say "  [ok  ] container runtime: $("$DOCKER_BIN" --version 2>/dev/null | head -1)"
        return 0
    fi
    if [ "$SYSTEM_DEPS" = "1" ] && ! find_docker; then
        install_intent docker "docker"
    fi
    find_docker || return 1
    "$DOCKER_BIN" info >/dev/null 2>&1
}

docker_ready() {
    find_docker || return 1
    "$DOCKER_BIN" info >/dev/null 2>&1
}

docker_trouble() {   # one short line explaining why docker cannot be used
    local msg
    msg="$("$DOCKER_BIN" info 2>&1 >/dev/null | head -2)"
    case "$msg" in
        *ermission*)
            printf 'the docker socket is not writable (add yourself to the docker group and log in again)\n' ;;
        *"Cannot connect"*|*"Is the docker daemon"*|*"connection refused"*|*"daemon is not running"*)
            printf 'the docker daemon is not running\n' ;;
        "") printf 'the container runtime does not answer\n' ;;
        *) printf '%s\n' "$(printf '%s\n' "$msg" | head -1)" ;;
    esac
}

# ---- chat server state ---------------------------------------------------
state_file() { printf '%s\n' "$LIBRARY/library-work/app/chat/server.json"; }

server_url_up() {   # $1 = url from server.json; 0 when /api/ping answers
    local url="$1" py
    for py in python3 /usr/bin/python3; do
        have "$py" || continue
        if "$py" - "$url" >/dev/null 2>&1 <<'PY'
import json
import sys
import urllib.request

url = sys.argv[1].replace("0.0.0.0", "127.0.0.1")
ping = url.replace("/?t=", "/api/ping?t=")
try:
    with urllib.request.urlopen(ping, timeout=1.5) as response:
        json.loads(response.read().decode("utf-8"))
except Exception:
    sys.exit(1)
PY
        then
            return 0
        fi
    done
    return 1
}

wait_for_server() {   # $1 = seconds; prints the url when it answers
    local limit="${1:-30}" state url deadline
    state="$(state_file)"
    deadline=$((SECONDS + limit))
    while [ "$SECONDS" -lt "$deadline" ]; do
        url="$(sed -n 's/.*"url": *"\([^"]*\)".*/\1/p' "$state" 2>/dev/null | head -1)"
        if [ -n "$url" ] && server_url_up "$url"; then
            printf '%s\n' "$url"
            return 0
        fi
        sleep 0.5
    done
    return 1
}

drop_legacy() {
    for name in $LEGACY_NAMES; do
        for path in "$DATA_HOME/applications/$name.desktop" \
                    "$DATA_HOME/icons/hicolor/scalable/apps/$name.svg"; do
            if [ -e "$path" ]; then
                remove_path "$path"
            fi
        done
    done
    return 0
}

refresh_caches() {
    [ "$DRY_RUN" = "1" ] && return 0
    if have update-desktop-database; then
        update-desktop-database "$DATA_HOME/applications" 2>/dev/null || true
    fi
    if have gtk-update-icon-cache && [ -d "$DATA_HOME/icons/hicolor" ]; then
        gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
    fi
}

container_running() {
    [ -f "$COMPOSE_FILE" ] || return 1
    "$DOCKER_BIN" compose -f "$COMPOSE_FILE" ps --status running --quiet \
        2>/dev/null | grep -q .
}

# --------------------------------------------------------------------------
if [ "$MODE" = "uninstall" ]; then
    say "Removing JulAIn Music Player"
    if [ -f "$COMPOSE_FILE" ] && find_docker && [ "$DRY_RUN" = "0" ]; then
        "$DOCKER_BIN" compose -f "$COMPOSE_FILE" down --remove-orphans \
            >/dev/null 2>&1 || true
        say "  stopped the container"
    fi
    for path in "$DESKTOP_FILE" "$ICON_FILE" "$WRAPPER_FILE"; do
        if [ -e "$path" ]; then
            remove_path "$path"
        fi
    done
    if [ -f "$VERSION_FILE" ]; then
        remove_tree "$APP_HOME"
    elif [ -d "$APP_HOME" ]; then
        say "  kept $APP_HOME (not installed by this script)"
    fi
    drop_legacy
    refresh_caches
    say ""
    say "The music library and the image were not touched."
    exit 0
fi

# --------------------------------------------------------------------------
# the folder the app should work on
if [ -z "$LIBRARY" ]; then
    if [ -n "${MUSIC_LIBRARY:-}" ]; then
        LIBRARY="$MUSIC_LIBRARY"
    elif [ -d "$HOME/Musik" ]; then
        LIBRARY="$HOME/Musik"
    else
        REPO_ROOT="$(cd "$HERE/.." && pwd)"
        [ -d "$REPO_ROOT" ] && is_library "$REPO_ROOT" && LIBRARY="$REPO_ROOT"
    fi
fi
if [ -z "$LIBRARY" ]; then
    echo "error: no music folder found." >&2
    echo "       Pass --library ~/Musik (or wherever the music lives), or set" >&2
    echo "       MUSIC_LIBRARY." >&2
    exit 1
fi
# an explicit choice wins; otherwise the folder picked in the app's Settings
# is what the next start mounts
if [ -z "$LIBRARY_EXPLICIT" ]; then
    PREFERRED="$(saved_library "$LIBRARY" || true)"
    if [ -n "$PREFERRED" ] && [ "$PREFERRED" != "$LIBRARY" ] && is_library "$PREFERRED"; then
        say "  settings : using $PREFERRED (chosen in the app)"
        LIBRARY="$PREFERRED"
    fi
fi
LIBRARY="$(cd "$LIBRARY" && pwd)"
if [ ! -f "$HERE/lib/music.py" ]; then
    echo "error: $HERE/lib/music.py is missing — copy the whole" >&2
    echo "       application/ folder, not just this script." >&2
    exit 1
fi

# --------------------------------------------------------------------------
# ---- system pieces -------------------------------------------------------
say ""
say "Checking the system pieces"
if [ "$SYSTEM_DEPS" = "0" ]; then
    say "  [info] --no-system-deps: no package is installed"
fi
if detect_pkg_manager; then
    say "  [info] package manager: $PM"
else
    say "  [info] no apt-get/dnf/pacman/zypper/apk: system pieces are not installed"
fi
bootstrap_window_deps || true

# ---- container runtime ---------------------------------------------------
if [ "$TARGET" = "docker" ]; then
    say ""
    say "Checking the container runtime"
    if ! bootstrap_docker_deps; then
        if find_docker; then
            docker_reason="$(docker_trouble)"
        else
            docker_reason="no docker or podman found"
        fi
        if [ "$FALLBACK_HOST" = "1" ]; then
            say "  [warn] $docker_reason"
            if [ "$DRY_RUN" = "1" ]; then
                say "  [warn] a real run falls back to the host build"
            else
                say "  [warn] falling back to the host build"
                say "         (--no-fallback refuses this instead)"
            fi
            TARGET=host
            BUILD_IMAGE=0
        else
            echo "error: $docker_reason" >&2
            echo "       Install docker (or podman), or run the CLI directly with:" >&2
            echo "         $HERE/install.sh --host" >&2
            echo "       --fallback-host does that automatically." >&2
            exit 1
        fi
    fi
fi

# the player: only the machine itself needs mpv, the image brings its own
if [ "$TARGET" = "host" ]; then
    bootstrap_player_deps || true
else
    say "  [ok  ] player: mpv is baked into the image"
fi

# Optional download modules are a separate checkout.  Stage it into the build
# context so the image can byte-compile it; an empty stage is the normal open
# build and simply yields a player without the download feature.
STAGE_MODULES="$HERE/.build-modules"
stage_modules() {
    do_it rm -rf "$STAGE_MODULES"
    do_it mkdir -p "$STAGE_MODULES"
    if [ -d "$MODULES" ]; then
        do_it cp -a "$MODULES"/. "$STAGE_MODULES"/
    fi
}

if [ "$TARGET" = "docker" ] \
   && [ "$BUILD_IMAGE" = "1" ] && [ "$DRY_RUN" = "0" ]; then
    if [ -d "$MODULES" ]; then
        say "Staging download modules from $MODULES"
    else
        say "No download modules ($MODULES): the image has no download feature"
    fi
    stage_modules
    say "Building $IMAGE"
    "$DOCKER_BIN" build -f "$HERE/Dockerfile" -t "$IMAGE" "$HERE"
fi

if [ "$TARGET" = "host" ] && ! have "$PYTHON"; then
    echo "error: $PYTHON not found; pass --python /path/to/python3" >&2
    exit 1
fi
if [ "$TARGET" = "host" ]; then
    PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    case "$PY_VERSION" in
        3.9|3.10|3.11|3.12|3.13|3.14|3.15) ;;
        *) echo "error: python >= 3.9 is required (found $PY_VERSION)" >&2; exit 1 ;;
    esac
fi

say ""
say "Installing JulAIn Music Player"
say "  version : $([ "$TARGET" = docker ] && echo "container ($IMAGE)" || echo "host CLI")"
say "  source  : $HERE"
say "  library : $LIBRARY"
say "  app     : $APP_HOME"
say "  wrapper : $WRAPPER_FILE"
[ "$INSTALL_DESKTOP" = "1" ] && say "  desktop : $DESKTOP_FILE"
[ "$DRY_RUN" = "1" ] && say "  (dry run: nothing is written)"

if ! is_library "$LIBRARY"; then
    say ""
    say "  [warn] $LIBRARY has no Playlists/ and library-work/ yet."
    say "         Playing single files works; the playlist and search"
    say "         commands need a prepared library."
fi
if [ "$TARGET" = "host" ] && [ ! -f "$LIBRARY/scripts/tag_fields.py" ]; then
    say "  [warn] no scripts/ pipeline there: 'add' and 'yt' cannot tag."
fi

do_it mkdir -p "$APP_DIR" "$BIN_HOME"
[ "$INSTALL_DESKTOP" = "1" ] \
    && do_it mkdir -p "$DATA_HOME/applications" \
                   "$DATA_HOME/icons/hicolor/scalable/apps"

# ---- app code ------------------------------------------------------------
# the whole tree: top-level modules, the chat package and the skill documents
do_it cp -a "$HERE/lib/." "$APP_DIR/"
# drop modules of an older install that no longer exist in the source
if [ "$DRY_RUN" = "0" ]; then
    for file in "$APP_DIR"/*.py; do
        [ -e "$file" ] || continue
        [ -e "$HERE/lib/$(basename "$file")" ] || rm -f "$file"
    done
    rm -rf "$APP_DIR/__pycache__" "$APP_DIR/chat/__pycache__"
    rm -rf "$APP_DIR/modules"
fi

# ---- optional chat planner config ---------------------------------------
LLM_TARGET="$APP_HOME/llm.json"
if [ -n "$LLM_CONFIG" ]; then
    if [ ! -f "$LLM_CONFIG" ]; then
        echo "error: --llm-config $LLM_CONFIG does not exist" >&2
        exit 1
    fi
    do_it cp -f "$LLM_CONFIG" "$LLM_TARGET"
    [ "$DRY_RUN" = "0" ] && chmod 0600 "$LLM_TARGET"
    say "  planner : $LLM_TARGET (from $LLM_CONFIG)"
fi
# the planner is configured in the app's Settings window (provider, address,
# model, key) and stored with the library; --llm-config only pre-seeds an
# alternative file for someone who prefers one
if [ -f "$LLM_TARGET" ]; then
    say "  planner : enabled from $LLM_TARGET"
else
    say "  planner : choose a server in Settings (Ollama, GWDG/SAIA or any OpenAI-compatible API)"
fi

# ---- container: per-machine compose project ------------------------------
UID_N="$(id -u)"
GID_N="$(id -g)"
PULSE_DIR="${XDG_RUNTIME_DIR:-/run/user/$UID_N}/pulse"
PULSE_SOCK="$PULSE_DIR/native"
PULSE_COOKIE="$HOME/.config/pulse/cookie"
SOUND=pulse
[ -e "$PULSE_SOCK" ] || SOUND=none
[ "$SOUND" = none ] && [ -d /dev/snd ] && SOUND=alsa

if [ "$TARGET" = "docker" ]; then
    if [ "$DRY_RUN" = "1" ]; then
        say "  would write $COMPOSE_FILE"
        say "  would mount $LIBRARY at /library (user $UID_N:$GID_N, sound: $SOUND)"
    else
        {
            echo "name: $APP_ID"
            echo "services:"
            echo "  music:"
            echo "    image: $IMAGE"
            echo "    container_name: $APP_ID"
            echo "    user: \"$UID_N:$GID_N\""
            echo "    working_dir: /library"
            echo "    ports:"
            echo "      - \"127.0.0.1:8765:8765\""
            echo "    volumes:"
            echo "      - \"$LIBRARY:/library\""
            if [ "$SOUND" = pulse ]; then
                echo "      - \"$PULSE_DIR:/run/user/$UID_N/pulse\""
                if [ -e "$PULSE_COOKIE" ]; then
                    echo "      - \"$PULSE_COOKIE:/tmp/pulse-cookie:ro\""
                fi
            fi
            if [ -f "$LLM_TARGET" ]; then
                echo "      - \"$LLM_TARGET:/config/llm.json:ro\""
            fi
            echo "    environment:"
            echo "      - HOME=/tmp"
            echo "      - MPV_SOCKET=/tmp/mpv.sock"
            echo "      - MPV_LOG=/tmp/mpv.log"
            if [ -f "$LLM_TARGET" ]; then
                echo "      - MUSIC_LLM_CONFIG=/config/llm.json"
            fi
            case "$SOUND" in
                pulse) echo "      - MPV_AO=pulse"
                       echo "      - PULSE_SERVER=unix:/run/user/$UID_N/pulse/native"
                       [ -e "$PULSE_COOKIE" ] && echo "      - PULSE_COOKIE=/tmp/pulse-cookie" ;;
                alsa)  echo "      - MPV_AO=alsa" ;;
                *)     echo "      - MPV_AO=null" ;;
            esac
            if [ "$SOUND" = alsa ]; then
                echo "    devices:"
                echo "      - /dev/snd:/dev/snd"
            fi
            echo "    stdin_open: true"
            echo "    tty: true"
            echo "    entrypoint: [\"sleep\", \"infinity\"]"
            echo "    restart: unless-stopped"
        } > "$COMPOSE_FILE"
        say "  wrote $COMPOSE_FILE (library $LIBRARY, sound: $SOUND)"
    fi

    # bring the container up so the install ends with something that runs
    if [ "$DRY_RUN" = "1" ]; then
        say "  would start the container: $DOCKER_BIN compose -f $COMPOSE_FILE up -d"
    else
        say "Starting the container"
        if "$DOCKER_BIN" compose -f "$COMPOSE_FILE" up -d >/dev/null 2>&1; then
            say "  [ok  ] container running"
        else
            say "  [warn] could not start the container; run:"
            say "         $DOCKER_BIN compose -f $COMPOSE_FILE up -d"
        fi
    fi
fi

if [ "$DRY_RUN" = "1" ]; then
    say "  would write $VERSION_FILE"
else
    {
        printf 'app=%s\n' "$APP_ID"
        printf 'target=%s\n' "$TARGET"
        printf 'installed=%s\n' "$(date -Iseconds)"
        printf 'source=%s\n' "$HERE"
        printf 'library=%s\n' "$LIBRARY"
        [ "$TARGET" = docker ] && printf 'image=%s\n' "$IMAGE"
    } > "$VERSION_FILE"
fi

# ---- host: its own venv --------------------------------------------------
if [ "$TARGET" = "host" ]; then
    if [ "$USE_VENV" = "1" ]; then
        if [ ! -x "$APP_PY" ] || [ "$FORCE" = "1" ]; then
            do_it "$PYTHON" -m venv "$VENV_DIR"
        fi
        if [ "$INSTALL_DEPS" = "1" ]; then
            if [ "$DRY_RUN" = "1" ]; then
                say "  would: $APP_PY -m pip install -r $HERE/requirements.txt"
            else
                "$APP_PY" -m pip install --quiet --upgrade pip
                "$APP_PY" -m pip install --quiet -r "$HERE/requirements.txt"
            fi
        fi
    else
        APP_PY="$PYTHON"
        if [ "$INSTALL_DEPS" = "1" ] && [ "$DRY_RUN" = "0" ]; then
            if ! "$APP_PY" -c 'import mutagen' >/dev/null 2>&1; then
                echo "error: $APP_PY has no mutagen; install it or drop --no-venv" >&2
                echo "       ($APP_PY -m pip install -r $HERE/requirements.txt)" >&2
                exit 1
            fi
        fi
    fi
fi

# ---- optional download modules (host) -----------------------------------
# The open install has none.  With --modules (or $JMP_MODULES) pointing at a
# module checkout, copy and byte-compile it into $APP_DIR/modules; its
# requirements are installed into the same venv.
if [ "$TARGET" = "host" ]; then
    say ""
    if [ -d "$MODULES" ]; then
        say "Download modules: $MODULES"
    else
        say "Download modules: none ($MODULES does not exist) — the player has no download feature"
    fi
    if [ "$DRY_RUN" = "1" ]; then
        say "  would: $APP_PY $HERE/build-modules.py --modules $MODULES --dest $APP_DIR/modules --clean"
    elif [ -f "$HERE/build-modules.py" ]; then
        "$APP_PY" "$HERE/build-modules.py" \
            --modules "$MODULES" --dest "$APP_DIR/modules" \
            --python "$APP_PY" --clean --requirements \
            || say "  [warn] download module build failed: the player ships without it"
    fi
fi

# ---- launcher ------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
    say "  would write $WRAPPER_FILE"
else
    backup "$WRAPPER_FILE"
    if [ "$TARGET" = "docker" ]; then
        cat > "$WRAPPER_FILE" <<WRAPPER
#!/usr/bin/env bash
# Written by $HERE/install.sh — safe to edit, re-running install.sh overwrites.
# Starts the container (with $LIBRARY mounted) and runs the app inside it.
set -euo pipefail

LIBRARY="\${MUSIC_LIBRARY:-$LIBRARY}"
COMPOSE="$COMPOSE_FILE"
DOCKER="$DOCKER_BIN"
APP_DIR="$APP_DIR"

if ! "\$DOCKER" compose -f "\$COMPOSE" ps --status running --quiet \\
        2>/dev/null | grep -q .; then
    # first use from the launcher: bring the container up
    "\$DOCKER" compose -f "\$COMPOSE" up -d >/dev/null
fi

# ---- chat window -------------------------------------------------------
# The server runs inside the container; the window belongs to the host
# (native GTK/WebKit when it is installed, otherwise a browser).
# compose publishes 127.0.0.1:8765 only, so the page stays local.
say_fail() {
    command -v notify-send >/dev/null 2>&1 && \\
        notify-send "JulAIn Music Player" "\$1"
    command -v zenity >/dev/null 2>&1 && \\
        zenity --error --text="\$1" >/dev/null 2>&1
    echo "\$1" >&2
    exit 1
}

open_host_window() {
    url="\$1"
    # A real GTK/WebKit window looks native on GNOME; it needs the *host's*
    # python3 with PyGObject (the container has no display).
    for py in python3 /usr/bin/python3 /usr/local/bin/python3; do
        command -v "\$py" >/dev/null 2>&1 || continue
        backend="\$("\$py" "\$APP_DIR/chat/window.py" --check 2>/dev/null)" || continue
        case "\$backend" in gtk3|gtk4) ;; *) continue ;; esac
        "\$py" "\$APP_DIR/chat/window.py" --backend "\$backend" "\$url" \\
            >/dev/null 2>&1 &
        sleep 0.7
        kill -0 \$! 2>/dev/null && return 0
    done
    # otherwise a chromeless browser window
    for b in chromium chromium-browser google-chrome google-chrome-stable \\
             brave-browser microsoft-edge microsoft-edge-stable vivaldi-stable; do
        if command -v "\$b" >/dev/null 2>&1; then
            "\$b" --app="\$url" --window-size=1280,880 \\
                  --class=julians-music-app --name=julians-music-app \\
                  >/dev/null 2>&1 &
            return 0
        fi
    done
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "\$url" >/dev/null 2>&1 &
        return 0
    fi
    return 1
}

if [ "\${1:-}" = chat ] && [ "\${2:-}" = --web ] && [ "\$#" -le 2 ]; then
    "\$DOCKER" compose -f "\$COMPOSE" exec -dT music \\
        music chat --web --no-open --host 0.0.0.0 --port 8765
    STATE="\$LIBRARY/library-work/app/chat/server.json"
    for _ in \$(seq 1 "\${MUSIC_CHAT_WAIT_TICKS:-80}"); do
        [ -s "\$STATE" ] && break
        sleep 0.25
    done
    URL="\$(sed -n 's/.*"url": *"\\([^"]*\\)".*/\\1/p' "\$STATE" 2>/dev/null | head -1)"
    [ -n "\$URL" ] || say_fail "The chat server did not start in the container."
    URL="\${URL/0.0.0.0/127.0.0.1}"
    open_host_window "\$URL" || say_fail "No window could be opened — open \$URL by hand."
    exit 0
fi

# an interactive shell or the chat TUI needs the TTY; one-shot commands do not
case "\${1:-}" in
    shell|bash|sh|zsh|chat|"")
        exec "\$DOCKER" compose -f "\$COMPOSE" exec music music "\$@" ;;
esac
exec "\$DOCKER" compose -f "\$COMPOSE" exec -T music music "\$@"
WRAPPER
    else
        cat > "$WRAPPER_FILE" <<WRAPPER
#!/usr/bin/env bash
# Written by $HERE/install.sh — safe to edit, re-running install.sh overwrites.
set -euo pipefail

LIBRARY="\${MUSIC_LIBRARY:-$LIBRARY}"
export MUSIC_LIBRARY="\$LIBRARY"
[ -f "$LLM_TARGET" ] && export MUSIC_LLM_CONFIG="$LLM_TARGET"
PY="$APP_PY"
[ -x "\$PY" ] || PY="\${PYTHON:-python3}"
exec "\$PY" "$APP_DIR/music.py" "\$@"
WRAPPER
    fi
    chmod 0755 "$WRAPPER_FILE"
fi

# ---- launcher entry + icon ----------------------------------------------
if [ "$INSTALL_DESKTOP" = "1" ]; then
    backup "$DESKTOP_FILE"
    do_it cp -f "$HERE/$APP_NAME" "$DESKTOP_FILE"
    if [ "$DRY_RUN" = "1" ]; then
        say "  would point Exec/TryExec at $WRAPPER_FILE"
    else
        sed -E -i "s#^(Exec|TryExec)=music-cli( |\$)#\\1=$WRAPPER_FILE\\2#" \
            "$DESKTOP_FILE"
    fi
    do_it chmod 0644 "$DESKTOP_FILE"

    backup "$ICON_FILE"
    do_it cp -f "$HERE/$ICON_NAME" "$ICON_FILE"
    do_it chmod 0644 "$ICON_FILE"
fi

drop_legacy
refresh_caches

# ---- report --------------------------------------------------------------
if [ "$DRY_RUN" = "0" ]; then
    say ""
    if [ "$TARGET" = "docker" ]; then
        say "Checking the container"
        say "  [ok  ] $("$DOCKER_BIN" --version 2>/dev/null | head -1)"
        say "  [ok  ] image $IMAGE"
        if [ "$SOUND" = pulse ]; then
            say "  [ok  ] sound: PulseAudio socket"
        elif [ "$SOUND" = alsa ]; then
            say "  [ok  ] sound: ALSA (/dev/snd)"
        else
            say "  [warn] no sound device found: playback will be silent"
            say "         (start PipeWire/PulseAudio and re-run, or pass MPV_AO)"
        fi
        say "  container name: $APP_ID"
    else
        say "Checking the optional pieces"
        have ffmpeg || say "  [warn] ffmpeg not found: no mood analysis"
        if have mpv; then
            say "  [ok  ] player: mpv"
        else
            say "  [warn] mpv not found: playback needs it"
        fi
        if [ -d "$APP_DIR/modules" ] \
           && [ -n "$(ls -A "$APP_DIR/modules" 2>/dev/null)" ]; then
            say "  [ok  ] download module: installed"
        else
            say "  [info] download module: none (the player has no download feature)"
        fi
        if "$APP_PY" -c 'import numpy' >/dev/null 2>&1; then
            say "  [ok  ] numpy: mood playlists can be rebuilt"
        else
            say "  [warn] numpy missing: 'music-cli mood build' will not run"
        fi
    fi
    # the launcher opens the chat as a window, so report what will show it
    window_native=""
    window_native="$(window_backend)" || window_native=""
    window_browser="$(browser_for_window)" || window_browser=""
    if [ -n "$window_native" ]; then
        say "  [ok  ] window: native GTK/WebKit ($window_native)"
    elif [ -n "$window_browser" ]; then
        say "  [ok  ] window: $window_browser --app"
        say "         (install PyGObject + WebKitGTK — on Debian/Ubuntu:"
        say "          python3-gi gir1.2-webkit2-4.1 — for a native window)"
    elif have xdg-open; then
        say "  [warn] no chromium-like browser: the window opens in the default browser"
    else
        say "  [warn] no browser found: 'music-cli chat --web' prints the URL"
    fi
    if [ "$INSTALL_DESKTOP" = "1" ] && [ -f "$DESKTOP_FILE" ] \
       && have desktop-file-validate; then
        desktop-file-validate "$DESKTOP_FILE" || true
    fi
fi

# ---- start the app -------------------------------------------------------
# One command, window on screen.  A missing display or a slow server is a
# warning, never a failed install.
if launch_wanted; then
    if [ "$DRY_RUN" = "1" ]; then
        say ""
        say "  would start: $WRAPPER_FILE chat --web"
    elif ! display_available; then
        say ""
        say "  [warn] no DISPLAY/WAYLAND_DISPLAY: not starting the window"
        say "         (start it from Activities, or run: $WRAPPER_NAME chat --web)"
    else
        say ""
        say "Starting the app window"
        launch_log="$APP_HOME/launch.log"
        nohup "$WRAPPER_FILE" chat --web >"$launch_log" 2>&1 &
        if url="$(wait_for_server "$LAUNCH_WAIT")"; then
            say "  [ok  ] chat server: ${url%%\?*}"
            say "         the window should be opening now"
        else
            say "  [warn] the chat server did not answer within ${LAUNCH_WAIT}s"
            say "         open it with: $WRAPPER_NAME chat --web"
            say "         log: $launch_log"
        fi
    fi
fi

say ""
say "Done. JulAIn Music Player is installed."
if ! launch_wanted || [ "$DRY_RUN" = "1" ] || ! display_available; then
    say "  start it      Activities -> type \"JulAIn\"  (or $WRAPPER_NAME chat --web)"
fi
say "  play          music-cli play Bands"
say "  search        music-cli search \"die ärzte\""
if [ "$TARGET" = "docker" ]; then
    say "  the container stays up; stop it with"
    say "    docker compose -f $COMPOSE_FILE down"
fi
if [ -n "$PREFIX" ] || [ "$BIN_HOME" != "$HOME/.local/bin" ]; then
    say ""
    say "If \"$WRAPPER_NAME\" is not found, add $BIN_HOME to PATH."
fi

say "Uninstall with:  $HERE/install.sh --uninstall${PREFIX:+ --prefix $PREFIX}"
