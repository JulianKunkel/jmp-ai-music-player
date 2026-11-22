#!/usr/bin/env bash
# Global media keys through GNOME custom shortcuts.
#
# The media row of some keyboards sits behind F5-F8 and sends plain function
# keys when the row is in "function" mode.  Wayland delivers such keys only to
# the focused window, so JMP can see them only while it is focused.  This
# script registers the four keys as GNOME *global* shortcuts that call
# `music-cli`, which makes them work in every application.
#
# Tradeoff, and it is intentional: a global shortcut is exclusive.  While these
# bindings exist, F5-F8 no longer reach other applications (F5 stops reloading
# a browser page).  `--remove` puts everything back exactly as it was.
#
#   application/media-keys.sh              # show what is registered
#   application/media-keys.sh --install    # add the four bindings
#   application/media-keys.sh --remove     # remove them again
#   application/media-keys.sh --keys "F5=stop,F6=prev,F7=toggle,F8=next"
#
# Nothing here touches a music file: a key press runs one transport command.
set -euo pipefail

SCHEMA="org.gnome.settings-daemon.plugins.media-keys"
ROOT="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
# the row as it is printed on the JMP machine; override with --keys
KEYS="${JMP_MEDIA_SHORTCUTS:-F5=stop,F6=prev,F7=toggle,F8=next}"
MUSIC_CLI="${JMP_MUSIC_CLI:-}"

action="status"
while [ $# -gt 0 ]; do
    case "$1" in
        --install|--apply) action="install" ;;
        --remove|--uninstall) action="remove" ;;
        --status|--list) action="status" ;;
        --keys) shift; KEYS="${1:-}" ;;
        --cli) shift; MUSIC_CLI="${1:-}" ;;
        -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

find_cli() {
    if [ -n "$MUSIC_CLI" ]; then printf '%s' "$MUSIC_CLI"; return 0; fi
    if command -v music-cli >/dev/null 2>&1; then command -v music-cli; return 0; fi
    for candidate in "$HOME/.local/bin/music-cli" /usr/local/bin/music-cli; do
        [ -x "$candidate" ] && { printf '%s' "$candidate"; return 0; }
    done
    return 1
}

if ! command -v gsettings >/dev/null 2>&1; then
    echo "gsettings is not available — this is not a GNOME session." >&2
    exit 1
fi
CLI="$(find_cli || true)"

# the paths this script owns, derived from the key list (one per key)
python() { command -v python3 >/dev/null 2>&1 && python3 "$@" || /usr/bin/python3 "$@"; }

paths() {
    python - "$KEYS" <<'PY'
import sys
pairs = [p for p in sys.argv[1].replace(";", ",").split(",") if "=" in p]
for index, _ in enumerate(pairs):
    print(f"/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
          f"custom{90 + index}/")
PY
}

current_list() {
    gsettings get "$SCHEMA" custom-keybindings 2>/dev/null || echo "@as []"
}

# a GVariant list of strings -> one path per line
unpack_list() {
    python - "$1" <<'PY'
import ast, sys
text = sys.argv[1].strip()
if text.startswith("@as "):
    text = text[4:].strip()
try:
    for item in ast.literal_eval(text) or []:
        print(item)
except Exception:
    pass
PY
}

install() {
    if [ -z "$CLI" ]; then
        echo "music-cli was not found; install JMP first or pass --cli PATH." >&2
        exit 1
    fi
    mapfile -t wanted < <(paths)
    mapfile -t have < <(unpack_list "$(current_list)")
    list=("${have[@]}")
    for path in "${wanted[@]}"; do
        seen=0
        for item in "${list[@]}"; do [ "$item" = "$path" ] && seen=1; done
        [ "$seen" = 1 ] || list+=("$path")
    done
    # a GVariant string list, quoted for gsettings
    joined=""
    for item in "${list[@]}"; do
        esc="${item//\'/}"
        joined="${joined:+$joined, }'$esc'"
    done
    gsettings set "$SCHEMA" custom-keybindings "[$joined]"

    index=0
    IFS=',' read -r -a pairs <<< "$KEYS"
    for pair in "${pairs[@]}"; do
        key="${pair%%=*}"
        act="${pair#*=}"
        [ -n "$key" ] || continue
        [ -n "$act" ] || continue
        path="$ROOT/custom$((90 + index))/"
        base="${path#/}"
        base="${base%/}"
        base="${base//\//.}"
        gsettings set "$SCHEMA.custom-keybinding:$path" name "JMP ${act}"
        # A bare F5 is the GTK key name.  <F5> would mean a modifier called
        # F5, which GNOME does not know, so the shortcut would be ignored.
        gsettings set "$SCHEMA.custom-keybinding:$path" binding "${key}"
        gsettings set "$SCHEMA.custom-keybinding:$path" command "$CLI $act"
        printf '  %-6s -> music-cli %-6s  (%s)\n' "$key" "$act" "$path"
        index=$((index + 1))
    done
    echo "installed ${index} global media key bindings for $CLI"
    echo "they are exclusive: other applications no longer receive those keys"
}

remove() {
    mapfile -t wanted < <(paths)
    mapfile -t have < <(unpack_list "$(current_list)")
    keep=()
    for item in "${have[@]}"; do
        drop=0
        for path in "${wanted[@]}"; do [ "$item" = "$path" ] && drop=1; done
        [ "$drop" = 1 ] || keep+=("$item")
    done
    joined=""
    for item in "${keep[@]}"; do
        esc="${item//\'/}"
        joined="${joined:+$joined, }'$esc'"
    done
    gsettings set "$SCHEMA" custom-keybindings "[$joined]"
    for path in "${wanted[@]}"; do
        gsettings reset "$SCHEMA.custom-keybinding:$path" name 2>/dev/null || true
        gsettings reset "$SCHEMA.custom-keybinding:$path" binding 2>/dev/null || true
        gsettings reset "$SCHEMA.custom-keybinding:$path" command 2>/dev/null || true
    done
    echo "removed the JMP media key bindings; the keys reach applications again"
}

status() {
    mapfile -t wanted < <(paths)
    mapfile -t have < <(unpack_list "$(current_list)")
    printf 'music-cli : %s\n' "${CLI:-not found}"
    for path in "${wanted[@]}"; do
        listed=no
        for item in "${have[@]}"; do [ "$item" = "$path" ] && listed=yes; done
        name="$(gsettings get "$SCHEMA.custom-keybinding:$path" name 2>/dev/null || echo '')"
        binding="$(gsettings get "$SCHEMA.custom-keybinding:$path" binding 2>/dev/null || echo '')"
        command="$(gsettings get "$SCHEMA.custom-keybinding:$path" command 2>/dev/null || echo '')"
        printf '  %-58s listed=%s\n    %s  %s  %s\n' "$path" "$listed" \
               "$name" "$binding" "$command"
    done
    echo "all custom bindings:"
    for item in "${have[@]}"; do printf '  %s\n' "$item"; done
}

case "$action" in
    install) install ;;
    remove) remove ;;
    *) status ;;
esac
