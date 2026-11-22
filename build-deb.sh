#!/usr/bin/env bash
# Build a Debian package of JulAIn Music Player for Ubuntu 24.04+/26.04.
#
#   ./build-deb.sh                        # version from VERSION, output in dist/
#   ./build-deb.sh --version 1.0.1        # override the version
#   ./build-deb.sh --modules /path/to/modules
#   ./build-deb.sh --out /tmp/release
#
# The package installs the application under /usr/lib/jmp, the launcher as
# /usr/bin/music-cli, the desktop entry, the icon and the documentation.  It
# runs on the distribution's Python 3 with python3-mutagen and python3-numpy;
# no virtualenv and no pip are involved.
#
# An optional download module is NOT part of the open build.  With --modules
# pointing at a module checkout (or $JMP_MODULES), build-modules.py compiles it
# into /usr/lib/jmp/lib/modules as sourceless byte-code.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PKG=jmp-ai-music-player
MAINTAINER="Julian M. Kunkel <julian.kunkel@gwdg.de>"
HOMEPAGE="https://github.com/JulianKunkel/jmp-ai-music-player"
PYTHON=${PYTHON:-/usr/bin/python3}

VERSION=""
REVISION=1
OUT=""
MODULES=${JMP_MODULES:-}
KEEP_WORK=0

usage() {
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
    cat <<EOF

options:
  --version X.Y.Z    package version (default: $(cat "$HERE/VERSION" 2>/dev/null || echo 1.0.0))
  --revision N       Debian revision (default: 1)
  --out DIR          where the .deb is written (default: $HERE/dist)
  --modules DIR      optional download-module checkout to compile in
  --keep             keep the staging tree under build/deb/
  -h, --help         this help
EOF
}

die() { echo "build-deb: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION=${2:?--version needs a value}; shift 2 ;;
        --revision) REVISION=${2:?--revision needs a value}; shift 2 ;;
        --out) OUT=${2:?--out needs a directory}; shift 2 ;;
        --modules) MODULES=${2:?--modules needs a directory}; shift 2 ;;
        --keep) KEEP_WORK=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

VERSION=${VERSION:-$(cat "$HERE/VERSION" 2>/dev/null || echo 1.0.0)}
VERSION=${VERSION#v}
REVISION=${REVISION#-}
[[ "$VERSION" =~ ^[0-9][0-9A-Za-z.+~]*$ ]] || die "version must start with a digit: $VERSION"
[[ "$REVISION" =~ ^[0-9][0-9A-Za-z.+~]*$ ]] || die "revision must start with a digit: $REVISION"
OUT=${OUT:-$HERE/dist}
DEBVERSION="${VERSION}-${REVISION}"

command -v dpkg-deb >/dev/null || die "dpkg-deb is missing (install dpkg-dev)"
[ -f "$HERE/lib/music.py" ] || die "run this from the application folder"
[ -x "$PYTHON" ] || die "$PYTHON is missing"

WORK="$HERE/build/deb"
STAGE="$WORK/${PKG}_${DEBVERSION}_all"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$OUT"

say() { printf '  %s\n' "$*"; }
echo "Building ${PKG}_${DEBVERSION}_all.deb"

# ---- the application ----------------------------------------------------
say "application      /usr/lib/jmp"
mkdir -p "$STAGE/usr/lib/jmp"
cp -a "$HERE/lib" "$STAGE/usr/lib/jmp/lib"
cp -a "$HERE/README.md" "$HERE/LICENSE" "$HERE/NOTICE" "$HERE/requirements.txt" \
      "$HERE/build-modules.py" "$STAGE/usr/lib/jmp/"
mkdir -p "$STAGE/usr/lib/jmp/docs"
for doc in architecture.html licences.md modules.md logo-jmp.svg logo-jmp.png; do
    [ -f "$HERE/docs/$doc" ] && cp -a "$HERE/docs/$doc" "$STAGE/usr/lib/jmp/docs/"
done
# compiled artefacts of a source tree are never shipped
find "$STAGE/usr/lib/jmp" \( -name __pycache__ -o -name '*.pyc' -o -name '*.pyo' \
     -o -name .DS_Store -o -name '.pytest_cache' \) -prune -exec rm -rf {} + 2>/dev/null || true

if [ -n "$MODULES" ]; then
    [ -d "$MODULES" ] || die "--modules is not a directory: $MODULES"
    say "download module  $(basename "$MODULES") (compiled, no .py shipped)"
    "$PYTHON" "$HERE/build-modules.py" --modules "$MODULES" \
        --dest "$STAGE/usr/lib/jmp/lib/modules" --python "$PYTHON" \
        --clean --requirements >/dev/null
else
    say "download module  none (open build: the player has no download feature)"
    # a local module checkout must never leak into the open package
    rm -rf "$STAGE/usr/lib/jmp/lib/modules"
fi

# ---- launcher, desktop entry, icon, man page ----------------------------
say "launcher         /usr/bin/music-cli"
install -D -m 0755 "$HERE/packaging/deb/music-cli" "$STAGE/usr/bin/music-cli"

say "desktop entry    /usr/share/applications"
install -D -m 0644 "$HERE/julians-music-app.desktop" \
    "$STAGE/usr/share/applications/julians-music-app.desktop"

say "icon             /usr/share/icons/hicolor"
install -D -m 0644 "$HERE/julians-music-app.svg" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/julians-music-app.svg"
if command -v inkscape >/dev/null 2>&1; then
    for size in 48 64 128 256 512; do
        png="$STAGE/usr/share/icons/hicolor/${size}x${size}/apps/julians-music-app.png"
        mkdir -p "$(dirname "$png")"
        inkscape "$HERE/julians-music-app.svg" --export-type=png \
            --export-filename="$png" --export-width="$size" --export-height="$size" \
            >/dev/null 2>&1 || rm -f "$png"
    done
else
    say "[warn] inkscape missing: only the scalable icon is installed"
fi

say "man page         /usr/share/man/man1/music-cli.1.gz"
install -D -m 0644 "$HERE/packaging/deb/music-cli.1" "$WORK/music-cli.1"
gzip -9n -f "$WORK/music-cli.1"
install -D -m 0644 "$WORK/music-cli.1.gz" "$STAGE/usr/share/man/man1/music-cli.1.gz"

# ---- documentation ------------------------------------------------------
DOCDIR="$STAGE/usr/share/doc/$PKG"
say "documentation    /usr/share/doc/$PKG"
mkdir -p "$DOCDIR/docs"
cp -a "$HERE/README.md" "$DOCDIR/README.md"
cp -a "$HERE/NOTICE" "$DOCDIR/NOTICE"
cp -a "$HERE/LICENSE" "$DOCDIR/LICENSE"
cp -a "$HERE/packaging/deb/copyright" "$DOCDIR/copyright"
cp -a "$HERE/packaging/deb/changelog" "$WORK/changelog"
gzip -9n -f "$WORK/changelog"
cp -a "$WORK/changelog.gz" "$DOCDIR/changelog.Debian.gz"
for doc in architecture.html licences.md modules.md; do
    [ -f "$HERE/docs/$doc" ] && cp -a "$HERE/docs/$doc" "$DOCDIR/docs/"
done

# ---- package metadata ---------------------------------------------------
SIZE=$(du -sk "$STAGE" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $DEBVERSION
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-mutagen (>= 1.46), python3-numpy, mpv, ffmpeg
Recommends: python3-gi, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, python3-textual
Suggests: the import fetcher
Section: sound
Priority: optional
Homepage: $HOMEPAGE
Description: AI-driven music player that chats with your local library
 JulAIn Music Player indexes a music folder you already own, plays it
 through mpv and lets you search, queue, build playlists, rank tracks by
 measured psychoacoustics, read timed lyrics and change every setting by
 typing or speaking English or German.
 .
 The offline intent parser is the default; an optional OpenAI-compatible
 planner can be configured in the window.  The player never deletes audio
 and journals every playlist write.  The window runs on GTK/WebKit when
 python3-gi is installed and falls back to a browser window otherwise.
 .
 This package does not include a download feature.  Builds with an optional
 download module can add one; the open package never contains it.
EOF

install -D -m 0755 "$HERE/packaging/deb/postinst" "$STAGE/DEBIAN/postinst"
install -D -m 0755 "$HERE/packaging/deb/postrm" "$STAGE/DEBIAN/postrm"

# md5sums over everything but the metadata
( cd "$STAGE" && find . -path ./DEBIAN -prune -o -type f -print0 |
  sort -z | xargs -0 md5sum ) > "$STAGE/DEBIAN/md5sums"

# normalise permissions: root-owned, readable, only the launcher executable
chmod -R u+rwX,go+rX "$STAGE"
chmod 0755 "$STAGE/usr/bin/music-cli" "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

# ---- build --------------------------------------------------------------
export SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git -C "$HERE" log -1 --format=%ct 2>/dev/null || date +%s)}
DEB="$OUT/${PKG}_${DEBVERSION}_all.deb"
rm -f "$DEB"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" >/dev/null
( cd "$OUT" && sha256sum "$(basename "$DEB")" > "$(basename "$DEB").sha256" )

[ "$KEEP_WORK" = 1 ] || rm -rf "$WORK"

echo
dpkg-deb --info "$DEB" | sed 's/^/  /'
echo
say "files: $(dpkg-deb --contents "$DEB" | wc -l)"
say "built: $DEB ($(du -h "$DEB" | cut -f1))"
say "sha256: $(cut -d' ' -f1 "$DEB.sha256")"
say "check: dpkg-deb --contents '$DEB' | less"
say "install: sudo apt install '$DEB'"
