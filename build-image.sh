#!/usr/bin/env bash
# Build the container image of JulAIn Music Player from this folder.
#
#   application/build-image.sh                    # docker build
#   application/build-image.sh --dry-run          # print the command only
#   application/build-image.sh --tag mine:dev
#   application/build-image.sh --with-clementine  # also the GUI player
#   application/build-image.sh --no-cache
#   application/build-image.sh --modules ../jmp-download-module
#
# The build context is this folder; the library itself is never copied into
# the image.  Run it with:
#
#   docker compose -f application/compose.yaml up -d
#   docker compose -f application/compose.yaml exec music music play Bands
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

TAG="julians-music-app:latest"
WITH_CLEMENTINE=0
NO_CACHE=0
DRY_RUN=0
DOCKER_BIN="${DOCKER:-}"
MODULES="${JMP_MODULES:-}"

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --tag) TAG="${2:?--tag needs a name}"; shift ;;
        --with-clementine) WITH_CLEMENTINE=1 ;;
        --no-cache) NO_CACHE=1 ;;
        --modules) MODULES="${2:?--modules needs a directory}"; shift ;;
        --dry-run) DRY_RUN=1 ;;
        --docker) DOCKER_BIN="${2:?--docker needs a binary}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ -z "$DOCKER_BIN" ]; then
    if command -v docker >/dev/null 2>&1; then
        DOCKER_BIN=docker
    elif command -v podman >/dev/null 2>&1; then
        DOCKER_BIN=podman
    else
        echo "error: no docker or podman found." >&2
        echo "       Install one, or use $HERE/install.sh for the host CLI." >&2
        exit 1
    fi
fi

CMD=("$DOCKER_BIN" build -f "$HERE/Dockerfile" -t "$TAG")
[ "$WITH_CLEMENTINE" = "1" ] && CMD+=(--build-arg WITH_CLEMENTINE=true)
[ -n "$MODULES" ] && [ -d "$MODULES" ] && CMD+=(--build-arg WITH_MODULES=1)
[ "$NO_CACHE" = "1" ] && CMD+=(--no-cache)
CMD+=("$HERE")

# stage an optional closed module checkout for the build context
STAGE="$HERE/.build-modules"
stage_modules() {
    rm -rf "$STAGE"
    mkdir -p "$STAGE"
    if [ -d "$MODULES" ]; then
        cp -a "$MODULES"/. "$STAGE"/ 2>/dev/null || true
    fi
}

if [ "$DRY_RUN" = "1" ]; then
    if [ -d "$MODULES" ]; then
        echo "would stage download modules from $MODULES"
    else
        echo "no download modules ($MODULES) — the image has no download feature"
    fi
    printf 'would run:\n  '
    printf '%q ' "${CMD[@]}"
    printf '\n'
    exit 0
fi

stage_modules
printf 'building %s\n' "$TAG"
"${CMD[@]}"

cat <<EOF

Done. Run it with:

  docker compose -f $HERE/compose.yaml up -d
  docker compose -f $HERE/compose.yaml exec music music play Bands

or one-shot:

  docker run --rm -it --user "\$(id -u):\$(id -g)" \\
      -v "$REPO_ROOT:/library" -e HOME=/tmp \\
      $TAG search "die ärzte"
EOF
