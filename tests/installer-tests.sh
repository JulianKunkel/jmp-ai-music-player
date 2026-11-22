#!/usr/bin/env bash
# Headless tests for application/install.sh and application/install-desktop.sh.
#
# Nothing real is installed and no window is opened: every system tool is a
# fake earlier on PATH, and docker never runs.  The tests check the exact
# package commands, the "no root" path, the docker fallback, the launch
# decisions and the sandbox (only the --prefix is written).
#
#   application/tests/installer-tests.sh          # run everything
#   application/tests/installer-tests.sh -v       # print every run's output
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$(cd "$HERE/.." && pwd)"
INSTALL="$APP/install.sh"
DESKTOP="$APP/install-desktop.sh"
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

WORK_ROOT="${TMPDIR:-/tmp}/opencode/installer-tests"
mkdir -p "$WORK_ROOT"
WORK="$(mktemp -d "$WORK_ROOT/run.XXXXXX")"
FAKEBIN="$WORK/bin"
FAKE_LIB="$WORK/library"
LOG="$WORK/calls.log"
mkdir -p "$FAKEBIN" "$FAKE_LIB/Playlists" "$FAKE_LIB/Artists" \
         "$FAKE_LIB/library-work"
: > "$LOG"

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); }
fail() { FAIL=$((FAIL + 1)); printf '  FAIL: %s\n' "$*"; }

# ---- fakes ---------------------------------------------------------------
cat > "$FAKEBIN/docker" <<'SH'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >> "${FAKE_LOG:?}"
case "${1:-}" in
    --version) echo "Docker version 99.0.0-fake" ;;
    info)
        case "${FAKE_DOCKER_INFO:-ok}" in
            denied) echo "permission denied while trying to connect to the Docker daemon socket" >&2; exit 1 ;;
            down)   echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?" >&2; exit 1 ;;
        esac
        echo "Server Version: fake" ;;
    compose)
        shift
        case "$*" in
            *"ps --status running"*) echo "fake-container-id" ;;
        esac ;;
esac
exit 0
SH

cat > "$FAKEBIN/sudo" <<'SH'
#!/usr/bin/env bash
printf 'sudo %s\n' "$*" >> "${FAKE_LOG:?}"
if [ "${FAKE_SUDO:-deny}" = "deny" ]; then
    echo "sudo: a password is required" >&2
    exit 1
fi
if [ "${1:-}" = "-n" ]; then shift; fi
exec "$@"
SH

cat > "$FAKEBIN/apt-get" <<'SH'
#!/usr/bin/env bash
printf 'apt-get %s\n' "$*" >> "${FAKE_LOG:?}"
exit 0
SH

cat > "$FAKEBIN/apt-cache" <<'SH'
#!/usr/bin/env bash
printf 'apt-cache %s\n' "$*" >> "${FAKE_LOG:?}"
[ "${1:-}" = "show" ] || exit 1
if [ "${FAKE_APT_LISTS:-known}" = "empty" ]; then
    echo "E: Unable to locate package ${2:-}" >&2
    exit 100
fi
printf 'Package: %s\n' "${2:-}"
exit 0
SH

printf '#!/bin/sh\nexit 0\n' > "$FAKEBIN/mpv"
printf '#!/bin/sh\nprintf "notify-send %%s\\n" "$*" >> "${FAKE_LOG:?}"\n' > "$FAKEBIN/notify-send"
printf '#!/bin/sh\nexit 0\n' > "$FAKEBIN/zenity"
chmod +x "$FAKEBIN"/*

# ---- run helper ----------------------------------------------------------
# run NAME [ENV=value ...] -- ARGS...
run() {
    local name="$1"; shift
    local envs=() out
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do envs+=("$1"); shift; done
    [ "${1:-}" = "--" ] && shift
    out="$WORK/$name.out"
    ( cd "$APP" && env PATH="$FAKEBIN:/usr/local/bin:/usr/bin:/bin" \
                    FAKE_LOG="$LOG" "${envs[@]}" \
                    "$INSTALL" "$@" >"$out" 2>&1 )
    STATUS=$?
    if [ "$VERBOSE" = 1 ]; then
        printf '\n--- %s (exit %s)\n' "$name" "$STATUS"
        cat "$out"
    fi
    OUT="$out"
    return 0
}

has()    { grep -qF -- "$2" "$1"; }
expect() { if has "$1" "$2"; then pass; else fail "${3:-missing}: $2"; fi; }
reject() { if ! has "$1" "$2"; then pass; else fail "${3:-unexpected}: $2"; fi; }
status() { if [ "$1" = "$2" ]; then pass; else fail "exit $1, wanted $2"; fi; }
count()  { grep -cF -- "$2" "$1" 2>/dev/null || true; }

REAL_WRAPPER="$HOME/.local/bin/music-cli"
real_wrapper_sum() {
    if [ -f "$REAL_WRAPPER" ]; then
        md5sum "$REAL_WRAPPER" | cut -d' ' -f1
    else
        echo none
    fi
}
WRAPPER_BEFORE="$(real_wrapper_sum)"

echo "installer tests in $WORK"

# ---- 1. syntax and usage -------------------------------------------------
if bash -n "$INSTALL" && bash -n "$DESKTOP"; then pass; else fail "syntax"; fi
if "$INSTALL" --help | grep -q -- "--no-system-deps"; then pass
else fail "usage does not mention --no-system-deps"; fi
if "$INSTALL" --help | grep -q -- "--fallback-host"; then pass
else fail "usage does not mention --fallback-host"; fi
if "$INSTALL" --help | grep -q -- "--no-launch"; then pass
else fail "usage does not mention --no-launch"; fi

# ---- 2. dry run: the exact package commands, nothing written ------------
run dry FAKE_SUDO=deny FAKE_APT_LISTS=empty FAKE_DOCKER_INFO=ok \
    MUSIC_NO_NATIVE_WINDOW=1 \
    -- --docker --dry-run --library "$FAKE_LIB"
status "$STATUS" 0
expect "$OUT" "would: sudo apt-get update -qq" "apt lists refreshed in the plan"
expect "$OUT" "would install the native window (PyGObject + WebKitGTK): sudo apt-get install -y python3-gi gir1.2-webkit2-4.1"
expect "$OUT" "would start:" "dry run announces the launch"
reject "$LOG" "apt-get install" "dry run installed nothing"
reject "$LOG" "apt-get update" "dry run ran no update"

# ---- 2b. no container runtime at all: the docker intent -------------------
run nodocker FAKE_SUDO=deny FAKE_APT_LISTS=known FAKE_DOCKER_INFO=ok \
    MUSIC_NO_NATIVE_WINDOW=1 DOCKER=definitely-not-installed \
    -- --docker --fallback-host --dry-run --library "$FAKE_LIB"
status "$STATUS" 0
expect "$OUT" "would install docker: sudo apt-get install -y docker.io"
expect "$OUT" "a real run falls back to the host build"
expect "$OUT" "version : host CLI" "the plan is the host plan"

# ---- 3. a real host install without root: print the line, keep going ----
run noroot FAKE_SUDO=deny MUSIC_NO_NATIVE_WINDOW=1 \
    -- --host --prefix "$WORK/p1" --app-home "$WORK/a1" --no-desktop \
       --no-venv --no-deps --no-launch --python /usr/bin/python3 \
       --library "$FAKE_LIB"
status "$STATUS" 0
expect "$OUT" "Install it with: sudo apt-get install -y python3-gi gir1.2-webkit2-4.1"
expect "$OUT" "no native window; a browser window will be used instead"
reject "$LOG" "apt-get install" "no install without root"
if [ -f "$WORK/p1/bin/music-cli" ]; then pass; else fail "wrapper missing"; fi

# ---- 4. docker unusable: fallback, and --no-fallback --------------------
run fb FAKE_SUDO=deny FAKE_DOCKER_INFO=down \
    -- --docker --fallback-host --prefix "$WORK/p2" --app-home "$WORK/a2" \
       --no-desktop --no-build --no-venv --no-deps --no-launch \
       --python /usr/bin/python3 --library "$FAKE_LIB"
status "$STATUS" 0
expect "$OUT" "the docker daemon is not running"
expect "$OUT" "falling back to the host build"
expect "$OUT" "version : host CLI"
if [ -f "$WORK/p2/bin/music-cli" ]; then pass; else fail "fallback wrote no wrapper"; fi

run strict FAKE_SUDO=deny FAKE_DOCKER_INFO=down \
    -- --docker --no-fallback --dry-run --library "$FAKE_LIB"
status "$STATUS" 1
expect "$OUT" "the docker daemon is not running" "hard error names the reason"

run strict2 FAKE_SUDO=deny FAKE_DOCKER_INFO=denied \
    -- --docker --strict --dry-run --library "$FAKE_LIB"
status "$STATUS" 1
expect "$OUT" "docker socket is not writable" "group hint on permission denied"

# ---- 5. launch: the wrapper starts `chat --web` exactly once ------------
: > "$LOG"
run launch FAKE_SUDO=deny FAKE_DOCKER_INFO=ok LAUNCH_WAIT=2 \
    MUSIC_CHAT_WAIT_TICKS=2 DISPLAY=:0 \
    -- --docker --prefix "$WORK/p3" --app-home "$WORK/a3" --no-desktop \
       --no-build --no-system-deps --launch --library "$FAKE_LIB"
status "$STATUS" 0
if [ "$(count "$LOG" "chat --web")" = "1" ]; then pass
else fail "chat --web started $(count "$LOG" "chat --web") times"; fi
expect "$LOG" "exec -dT music music chat --web" "the container runs the chat server"
expect "$OUT" "did not answer within 2s" "a slow server is a warning"
expect "$OUT" "Done. JulAIn Music Player is installed." "the install finished"

# ---- 5b. no OpenCode files are mounted any more -------------------------
# the planner is chosen in the app's Settings window (Ollama, GWDG/SAIA or any
# OpenAI-compatible address, with the key stored in secrets.json); the host's
# own OpenCode files are no longer copied into the container
printf '%s' '{"model":"gwdg/test-model","provider":{"gwdg":{"options":{"baseURL":"http://llm.local/v1"}}}}' > "$WORK/oc.json"
printf '%s' '{"gwdg":{"type":"api","key":"sk-test"}}' > "$WORK/auth.json"
run oc FAKE_SUDO=deny FAKE_DOCKER_INFO=ok \
    MUSIC_OPENCODE_CONFIG="$WORK/oc.json" MUSIC_OPENCODE_AUTH="$WORK/auth.json" \
    -- --docker --prefix "$WORK/p8" --app-home "$WORK/a8" --no-desktop \
       --no-build --no-system-deps --no-launch --library "$FAKE_LIB"
status "$STATUS" 0
reject "$WORK/a8/compose.yaml" "/opencode/opencode.json" \
    "the OpenCode config is mounted"
reject "$WORK/a8/compose.yaml" "/opencode/auth.json" \
    "the OpenCode auth file is mounted"
reject "$WORK/a8/compose.yaml" "MUSIC_OPENCODE" \
    "the OpenCode paths are injected"
expect "$OUT" "choose a server in Settings" \
    "the install points at the Settings window"

# ---- 6. --no-launch starts nothing --------------------------------------
: > "$LOG"
run nolaunch FAKE_SUDO=deny FAKE_DOCKER_INFO=ok DISPLAY=:0 \
    -- --docker --prefix "$WORK/p4" --app-home "$WORK/a4" --no-desktop \
       --no-build --no-system-deps --no-launch --library "$FAKE_LIB"
status "$STATUS" 0
reject "$LOG" "chat --web" "--no-launch started the app"

# ---- 7. no display: auto skips the launch, --launch says why ------------
: > "$LOG"
run nodisplay FAKE_SUDO=deny FAKE_DOCKER_INFO=ok DISPLAY= WAYLAND_DISPLAY= \
    -- --docker --prefix "$WORK/p5" --app-home "$WORK/a5" --no-desktop \
       --no-build --no-system-deps --library "$FAKE_LIB"
status "$STATUS" 0
reject "$LOG" "chat --web" "auto launched without a display"
expect "$OUT" "start it      Activities" "points at the launcher"

: > "$LOG"
run nodisplay2 FAKE_SUDO=deny FAKE_DOCKER_INFO=ok DISPLAY= WAYLAND_DISPLAY= \
    -- --docker --prefix "$WORK/p6" --app-home "$WORK/a6" --no-desktop \
       --no-build --no-system-deps --launch --library "$FAKE_LIB"
status "$STATUS" 0
reject "$LOG" "chat --web" "--launch started without a display"
expect "$OUT" "no DISPLAY/WAYLAND_DISPLAY" "says why nothing started"

# ---- 8. install-desktop.sh wires the flags through ----------------------
( cd "$APP" && env PATH="$FAKEBIN:/usr/local/bin:/usr/bin:/bin" \
      FAKE_LOG="$LOG" FAKE_SUDO=deny FAKE_DOCKER_INFO=down DISPLAY=:0 \
      "$DESKTOP" --dry-run --library "$FAKE_LIB" >"$WORK/dl.out" 2>&1 )
status "$?" 0
expect "$WORK/dl.out" "a real run falls back to the host build" "desktop falls back by default"
expect "$WORK/dl.out" "would start:" "desktop launches when a display exists"

( cd "$APP" && env PATH="$FAKEBIN:/usr/local/bin:/usr/bin:/bin" \
      FAKE_LOG="$LOG" FAKE_SUDO=deny FAKE_DOCKER_INFO=down DISPLAY=:0 \
      "$DESKTOP" --dry-run --no-fallback --library "$FAKE_LIB" \
      >"$WORK/dl2.out" 2>&1 )
status "$?" 1
expect "$WORK/dl2.out" "the docker daemon is not running" "--no-fallback is passed through"

( cd "$APP" && "$DESKTOP" --host >"$WORK/dl3.out" 2>&1 )
status "$?" 2
expect "$WORK/dl3.out" "always installs the docker build"

# ---- 9. uninstall dry run ----------------------------------------------
run un -- --uninstall --dry-run --prefix "$WORK/p3"
status "$STATUS" 0
expect "$OUT" "would remove $WORK/p3/bin/music-cli"

# ---- 10. the sandbox stays in the sandbox ------------------------------
if [ "$(real_wrapper_sum)" = "$WRAPPER_BEFORE" ]; then pass
else fail "a sandboxed install rewrote $REAL_WRAPPER"; fi
if [ -f "$WORK/a1/lib/music.py" ] && [ -f "$WORK/a2/lib/music.py" ] \
   && [ -f "$WORK/a3/compose.yaml" ]; then pass
else fail "a sandboxed install did not write its app home"; fi
if [ -d "$WORK/p1/share/julians-music-app" ] \
   || [ -d "$WORK/p2/share/julians-music-app" ]; then
    fail "an --app-home install still wrote the prefix data dir"
else
    pass
fi

# ---- 10b. the folder chosen in the app's Settings is what gets mounted ---
# (the app writes library-work/app/chat/settings.json; the next start must
# mount that folder, while an explicit --library still wins)
PREF_LIB="$WORK/preferred"
mkdir -p "$PREF_LIB/Playlists" "$PREF_LIB/Artists" "$PREF_LIB/library-work"
mkdir -p "$WORK/chat-settings/library-work/app/chat"
printf '{\n "library": "%s"\n}\n' "$PREF_LIB" \
    > "$WORK/chat-settings/library-work/app/chat/settings.json"
mkdir -p "$WORK/chat-settings/Playlists" "$WORK/chat-settings/Artists"

run pref FAKE_SUDO=deny FAKE_APT_LISTS=known FAKE_DOCKER_INFO=ok \
    MUSIC_NO_NATIVE_WINDOW=1 \
    -- --docker --dry-run --no-launch \
       --library "$WORK/chat-settings" --prefix "$WORK/p-pref" \
       --app-home "$WORK/a-pref"
status "$STATUS" 0
expect "$OUT" "would mount $WORK/chat-settings at /library" \
    "an explicit --library is mounted"
reject "$OUT" "would mount $PREF_LIB at /library" \
    "an explicit --library is not overridden by the saved setting"

run pref2 FAKE_SUDO=deny FAKE_APT_LISTS=known FAKE_DOCKER_INFO=ok \
    MUSIC_NO_NATIVE_WINDOW=1 MUSIC_LIBRARY="$WORK/chat-settings" \
    -- --docker --dry-run --no-launch \
       --prefix "$WORK/p-pref2" --app-home "$WORK/a-pref2"
status "$STATUS" 0
expect "$OUT" "using $PREF_LIB (chosen in the app)" \
    "the saved folder from the app's Settings is used"

# ---- 11. the installer leaves the library alone ------------------------
# (the fixture library is the one the installs were pointed at; the real
# library is the user's live state and may change while the app runs)
if [ -d "$FAKE_LIB/Playlists/Custom" ]; then
    fail "the installer created Playlists/Custom in the library"
else
    pass
fi
if [ -s "$FAKE_LIB/library-work/journal/playlists.jsonl" ]; then
    fail "the installer wrote the library's playlist journal"
else
    pass
fi
if [ -f "$FAKE_LIB/library-work/app/chat/server.json" ]; then
    fail "a fake launch left a server.json in the fixture library"
else
    pass
fi

echo
printf '%d passed, %d failed\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
    echo "output kept in $WORK"
    exit 1
fi
rm -rf "$WORK"
exit 0
