#!/usr/bin/env bash
# Set up the local lyrics transcriber — nothing leaves this machine.
#
# Creates a dedicated virtual environment (so the library's own `scripts/venv`
# is not disturbed) and installs faster-whisper, which runs Whisper locally
# through CTranslate2.  A CUDA GPU is used when the driver offers one, otherwise
# it runs on the CPU.
#
#   scripts/setup_asr.sh                    # create the venv, install, check
#   scripts/setup_asr.sh --model medium     # also download the model now
#   scripts/setup_asr.sh --cpu              # force the CPU build
#   scripts/setup_asr.sh --check            # only report what is installed
#   scripts/setup_asr.sh --remove           # delete the venv (models stay)
#
# The model files live in library-work/app/lyrics/models (safe to delete, they
# are downloaded again on demand); the lyrics themselves in
# library-work/app/lyrics/lyrics.db.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# This script ships inside the app, so the library is not the parent of this
# folder.  MUSIC_LIBRARY wins; otherwise the app starts it with the library as
# the working directory; otherwise use the repository layout.
if [ -n "${MUSIC_LIBRARY:-}" ]; then
  ROOT="$(cd "$MUSIC_LIBRARY" && pwd)"
elif [ -d "$PWD/library-work" ] || [ -d "$PWD/Playlists" ]; then
  ROOT="$PWD"
else
  ROOT="$(cd "$HERE/../.." 2>/dev/null && pwd)"
fi
VENV="${JMP_ASR_VENV:-$ROOT/library-work/app/lyrics/asr-venv}"
MODELS="$ROOT/library-work/app/lyrics/models"
MODEL="${MUSIC_ASR_MODEL:-auto}"
CPU=0
CHECK=0
REMOVE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL="${2:?--model needs a size}"; shift 2 ;;
    --model=*) MODEL="${1#*=}"; shift ;;
    --cpu) CPU=1; shift ;;
    --check) CHECK=1; shift ;;
    --remove) REMOVE=1; shift ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '[asr] %s\n' "$*"; }

if [ "$REMOVE" -eq 1 ]; then
  if [ -d "$VENV" ]; then
    rm -rf "$VENV"
    say "removed $VENV (models and lyrics kept)"
  else
    say "nothing to remove at $VENV"
  fi
  exit 0
fi

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "no python3 found" >&2
  exit 1
fi

if [ "$CHECK" -eq 0 ]; then
  if [ ! -x "$VENV/bin/python" ]; then
    say "creating $VENV with $PYTHON"
    if command -v uv >/dev/null 2>&1; then
      uv venv --python "$PYTHON" "$VENV"
    else
      "$PYTHON" -m venv "$VENV"
    fi
  fi
  say "installing faster-whisper (about 450 MB, one time)"
  # `av>=17` renamed the PyAV option faster-whisper 1.2 passes, so it is pinned
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$VENV/bin/python" --upgrade \
        "faster-whisper>=1.2" "av<17" >/dev/null
  else
    "$VENV/bin/python" -m pip install --quiet --upgrade \
        "faster-whisper>=1.2" "av<17"
  fi
fi

if [ ! -x "$VENV/bin/python" ]; then
  say "no environment at $VENV yet — run without --check"
  exit 3
fi

export MUSIC_ASR_MODEL="$MODEL"
"$VENV/bin/python" "$HERE/transcribe_lyrics.py" --check || true

if [ "$CHECK" -eq 0 ] && [ "$MODEL" != "auto" ]; then
  mkdir -p "$MODELS"
  say "downloading the $MODEL model into $MODELS"
  "$VENV/bin/python" - "$MODEL" "$MODELS" <<'PY'
import sys
name, folder = sys.argv[1], sys.argv[2]
from faster_whisper import WhisperModel
WhisperModel(name, device="cpu", compute_type="int8", download_root=folder)
print(f"[asr] model {name} ready")
PY
fi

cat <<EOF

[asr] done.  Transcribe a song with
        scripts/asr-venv/bin/python scripts/transcribe_lyrics.py --rel "Artists/…/song.mp3"
      or let the app do it: Settings → Lyrics → "Transcribe this song".
      If the app runs in the container, the image already carries the backend
      (CPU); the host venv above is what the host install uses (GPU when
      available).
EOF
