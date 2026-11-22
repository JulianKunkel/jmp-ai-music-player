#!/usr/bin/env bash
#
# publish-release.sh — create the GitHub release for $VERSION and upload the
# Debian package as a release asset.
#
# Why a script with a login step:
#
#   An SSH key proves to GitHub that you may *push* to the repository, but the
#   Releases API is a separate, token-authenticated surface.  There is no way
#   to create a release with an SSH key alone.  So this script keeps git on SSH
#   (`gh auth login --git-protocol ssh`) and gets the one API token through the
#   browser/device flow, once.  Afterwards `gh auth status` is green and every
#   later run needs no interaction.
#
#   If you already have a token, export GH_TOKEN (or GITHUB_TOKEN) and the
#   script never prompts.
#
# Usage:
#
#   ./publish-release.sh                     # build the .deb, tag check, upload
#   ./publish-release.sh --no-build          # upload the .deb already in dist/
#   ./publish-release.sh --notes FILE.md     # release notes from a file
#   ./publish-release.sh --draft             # create it as a draft first
#   ./publish-release.sh --tag v1.0.1        # a tag other than v$VERSION
#   ./publish-release.sh --repo OWNER/NAME   # override the origin remote
#
# Re-running is safe: an existing release gets its assets replaced, a missing
# one is created.  Nothing is deleted and no git history is rewritten.

set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$HERE"

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

TAG=""
REPO=""
NOTES_FILE=""
DO_BUILD=1
DRAFT=0
PRERELEASE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --no-build)   DO_BUILD=0 ;;
        --draft)      DRAFT=1 ;;
        --prerelease) PRERELEASE=1 ;;
        --tag)        [ $# -ge 2 ] || die "--tag needs a value"; TAG=$2; shift ;;
        --tag=*)      TAG=${1#*=} ;;
        --repo)       [ $# -ge 2 ] || die "--repo needs a value"; REPO=$2; shift ;;
        --repo=*)     REPO=${1#*=} ;;
        --notes)      [ $# -ge 2 ] || die "--notes needs a file"; NOTES_FILE=$2; shift ;;
        --notes=*)    NOTES_FILE=${1#*=} ;;
        -h|--help)    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next }
                            NR > 1 { exit }' "$0"; exit 0 ;;
        *)            die "unknown argument: $1 (try --help)" ;;
    esac
    shift
done

[ -f VERSION ] || die "VERSION is missing — run this from the repository root"
VERSION=$(tr -d '[:space:]' < VERSION)
[ -n "$VERSION" ] || die "VERSION is empty"
TAG=${TAG:-v$VERSION}

have gh || die "the GitHub CLI is missing — install 'gh' (apt install gh)"
have git || die "git is missing"
git rev-parse --git-dir >/dev/null 2>&1 || die "this is not a git checkout"

# -- which repository ------------------------------------------------------
if [ -z "$REPO" ]; then
    origin=$(git remote get-url origin 2>/dev/null || true)
    [ -n "$origin" ] || die "no 'origin' remote — pass --repo OWNER/NAME"
    # git@github.com:OWNER/NAME.git  |  https://github.com/OWNER/NAME.git
    REPO=$(printf '%s' "$origin" \
        | sed -E 's#^(git@|ssh://git@|https://|http://)##; s#^[^/:]+[:/]##; s#\.git$##')
    case "$REPO" in
        */*) ;;
        *) die "cannot read OWNER/NAME from origin '$origin' — pass --repo" ;;
    esac
fi
say "repository : $REPO"
say "tag        : $TAG"

# -- authentication --------------------------------------------------------
# GH_TOKEN wins, then an existing gh login, then the one-time device flow.
if [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; then
    say "auth       : token from the environment"
elif gh auth status --hostname github.com >/dev/null 2>&1; then
    say "auth       : gh is already logged in"
else
    say ""
    say "GitHub releases need an API token; an SSH key alone cannot create them."
    say "The next step keeps git on SSH and asks GitHub once for a token:"
    say ""
    say "    gh auth login --hostname github.com --git-protocol ssh --web"
    say ""
    [ -t 0 ] || die "not a terminal — run 'gh auth login --hostname github.com --git-protocol ssh --web' once, then re-run this script"
    gh auth login --hostname github.com --git-protocol ssh --web \
        || die "login failed — export GH_TOKEN=… instead"
    gh auth status --hostname github.com >/dev/null 2>&1 \
        || die "still not authenticated — export GH_TOKEN=… instead"
    say "auth       : logged in"
fi
say ""

# -- the tag ---------------------------------------------------------------
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null \
    || die "local tag $TAG does not exist — create it with: git tag -a $TAG -m 'JulAIn Music Player $VERSION'"
if ! git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
    say "pushing    : tag $TAG (it was only local)"
    git push origin "refs/tags/$TAG"
fi

# -- the package -----------------------------------------------------------
DEB="dist/jmp-ai-music-player_${VERSION}-1_all.deb"
if [ "$DO_BUILD" = 1 ]; then
    [ -x ./build-deb.sh ] || die "build-deb.sh is missing or not executable"
    say "building   : $DEB"
    ./build-deb.sh >/dev/null || die "build-deb.sh failed"
fi
[ -f "$DEB" ] || die "$DEB is missing — run ./build-deb.sh (or drop --no-build)"
SUM=$(sha256sum "$DEB" | awk '{print $1}')
printf '%s  %s\n' "$SUM" "$(basename "$DEB")" > "$DEB.sha256"
say "package    : $DEB ($(du -h "$DEB" | cut -f1))"
say "sha256     : $SUM"

# -- the notes -------------------------------------------------------------
if [ -n "$NOTES_FILE" ]; then
    [ -f "$NOTES_FILE" ] || die "notes file not found: $NOTES_FILE"
    say "notes      : $NOTES_FILE"
else
    NOTES_FILE=$(mktemp)
    trap 'rm -f "$NOTES_FILE"' EXIT
    cat > "$NOTES_FILE" <<EOF
# JulAIn Music Player $VERSION

JMP chats with a music folder you already own: it plays it through mpv,
searches it, builds playlists, ranks tracks by measured psychoacoustics, reads
timed lyrics and changes its own settings — in English or German, offline by
default.

## This package

\`$(basename "$DEB")\` — architecture \`all\`, installs under \`/usr/lib/jmp\` and
\`/usr/bin/music-cli\`, runs on the distribution's Python 3.

\`\`\`bash
sudo apt install ./$(basename "$DEB")
music-cli chat --web
\`\`\`

- Depends: \`python3\` (>= 3.10), \`python3-mutagen\`, \`python3-numpy\`, \`mpv\`, \`ffmpeg\`
- Recommends: \`python3-gi\`, \`gir1.2-gtk-3.0\`, \`gir1.2-webkit2-4.1\`, \`python3-textual\`
- Suggests: \`the import fetcher\` (only for a build with a download module)

The package contains **no download feature**.  Only builds that compile an
optional download module add one.

SHA-256: \`$SUM\` (also published as \`$(basename "$DEB").sha256\`).

Licence: GPL-3.0-or-later.  AI assistance is disclosed in \`NOTICE\`.
EOF
    say "notes      : generated"
fi

# -- create or update the release -----------------------------------------
ASSETS=("$DEB" "$DEB.sha256")
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
    say "release    : $TAG exists — replacing the assets"
    gh release upload "$TAG" "${ASSETS[@]}" --repo "$REPO" --clobber
else
    say "release    : creating $TAG"
    flags=(--repo "$REPO" --verify-tag --title "JulAIn Music Player $VERSION"
           --notes-file "$NOTES_FILE")
    [ "$DRAFT" = 1 ] && flags+=(--draft)
    [ "$PRERELEASE" = 1 ] && flags+=(--prerelease)
    gh release create "$TAG" "${ASSETS[@]}" "${flags[@]}"
fi

# -- verify ----------------------------------------------------------------
URL=$(gh release view "$TAG" --repo "$REPO" --json url --jq .url)
say ""
say "assets     :"
gh release view "$TAG" --repo "$REPO" --json assets \
    --jq '.assets[] | "  \(.name)  \(.size) bytes  \(.digest // "")"'
say ""
say "done       : $URL"
say "install    : sudo apt install ./$DEB"
