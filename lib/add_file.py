#!/usr/bin/env python3
"""File local audio files into the library: tag them, place them, index them.

This tool starts from a file you already have on disk (a rip, a purchase, a
recording of your own); an optional download module is what handles a URL.
Nothing is parked in an `Incoming/` folder — the file is tagged, moved into
place and indexed in one journalled run.

Per input file:

  1. copy it into `library-work/import/<stamp>/` (the source is kept unless
     `--move` is given, because deleting is never automatic here);
  2. read the tags it already carries and fill the gaps from the command line
     (`--artist`, `--album`, `--title`, `--track`, `--genre`, `--date`);
  3. write title/artist/albumartist/album/track/genre/date/grouping through
     `tag_fields.py`, so the write lands in its journal;
  4. move it to `Artists/<Artist>/<Album>/NN_Title.<ext>` (`--dest` overrides,
     a missing album means a flat `Artists/<Artist>/Title.<ext>`);
  5. rescan the inventory and rebuild the generated playlists.

A file that is already inside the library tree is refused: it is already
indexed and moving it again would only split an album.

Journal: `library-work/journal/add-file.jsonl`, fsync'd before the move, one
line per file with source, staged and final path, size, md5 and the tags that
were written.  `--rollback` moves every journalled file back to its source
(when that path is still free and the payload is unchanged).

Usage:
    music-cli add ~/Downloads/song.flac
    music-cli add --artist "Die Ärzte" \\
        --album "Best Of" --genre Punk --track 4 rip.mp3
    music-cli add --dest Own --grouping Eigene-Mix mine.wav
    music-cli add --recursive ~/Recordings/
    music-cli add --dry-run song.opus
    music-cli add --rollback
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mutagen import File as MFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from libpaths import library_root  # noqa: E402

ROOT = library_root()

# `rename_safe` is a library tool, not part of the app: keep one copy.
sys.path.insert(0, str(ROOT / "scripts"))
from rename_safe import safe_name  # noqa: E402
STAGE = ROOT / "library-work" / "import"
JOURNAL = ROOT / "library-work" / "journal" / "add-file.jsonl"
REPORT = ROOT / "library-work" / "reports" / "add-file.csv"
TAG_PLAN = ROOT / "library-work" / "reports" / "import-tag-plan.csv"
PY = sys.executable

AUDIO_EXT = {".mp3", ".mp2", ".m4a", ".mp4", ".opus", ".ogg", ".flac",
             ".wav", ".wma", ".aac"}


# --------------------------------------------------------------------------
# helpers


def md5_of(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def journal_write(entry: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_tags(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        audio = MFile(path, easy=True)
    except Exception:
        return out
    if audio is None:
        return out
    tags = audio.tags or {}
    for field in ("title", "artist", "albumartist", "album", "tracknumber",
                  "genre", "date"):
        value = (tags.get(field) or [""])[0]
        if value:
            out[field] = str(value).strip()
    return out


def first_int(text: str | None) -> int | None:
    if not text:
        return None
    digits = ""
    for ch in str(text):
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    try:
        return int(digits) if digits else None
    except ValueError:
        return None


def inside_library(path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(ROOT)
    except ValueError:
        return False
    return not rel.parts[0].startswith("library-work")


def free_path(path: Path) -> Path:
    """`X.mp3`, `X_2.mp3`, `X_3.mp3` — never overwrite a library file."""
    if not path.exists():
        return path
    number = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        if not candidate.exists():
            return candidate
        number += 1


def write_tags(rel: str, fields: list[dict]) -> None:
    """Write through `tag_fields.py` so the change is in its journal."""
    TAG_PLAN.parent.mkdir(parents=True, exist_ok=True)
    with TAG_PLAN.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["rel_path", "ext", "field", "before", "after",
                         "confidence"])
        for row in fields:
            writer.writerow([rel, Path(rel).suffix.lower(), row["field"], "",
                             row["after"], row.get("confidence", "high")])
    result = subprocess.run([PY, str(ROOT / "scripts" / "tag_fields.py"),
                             "--plan", str(TAG_PLAN), "--confidence", "high"],
                            cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit("tag_fields.py failed")


def refresh_index() -> None:
    run = [PY, str(ROOT / "scripts" / "phase0_inventory.py"), "scan",
           "--work", str(ROOT / "library-work")]
    subprocess.run(run, cwd=str(ROOT), check=False)
    subprocess.run([PY, str(ROOT / "scripts" / "phase0_inventory.py"),
                    "report", "--work", str(ROOT / "library-work")],
                   cwd=str(ROOT), check=False)
    subprocess.run([PY, str(ROOT / "scripts" / "make_playlists.py"),
                    "--from-tree"], cwd=str(ROOT), check=False)


def refresh_moods() -> None:
    subprocess.run([PY, str(ROOT / "scripts" / "analyze_moods.py")],
                   cwd=str(ROOT), check=False)
    subprocess.run([PY, str(ROOT / "scripts" / "make_mood_playlists.py"),
                    "--apply"], cwd=str(ROOT), check=False)


def target_rel(artist: str, album: str, title: str, track: int | None,
               ext: str, dest: str | None) -> str:
    if dest:
        folder = dest.strip("/")
        stem = f"{track:02d}_{safe_name(title)}" if track else safe_name(title)
        return f"{folder}/{stem}{ext}"
    parts = ["Artists", safe_name(artist)]
    if album:
        parts.append(safe_name(album))
        stem = f"{track:02d}_{safe_name(title)}" if track else safe_name(title)
    else:
        stem = safe_name(title)
    return "/".join(parts + [stem + ext])


def collect(paths: list[str], recursive: bool) -> list[Path]:
    out: list[Path] = []
    for text in paths:
        path = Path(text).expanduser()
        if path.is_dir():
            if not recursive:
                raise SystemExit(f"{path} is a directory (use --recursive)")
            out += [p for p in sorted(path.rglob("*"))
                    if p.is_file() and p.suffix.lower() in AUDIO_EXT]
        elif path.is_file():
            if path.suffix.lower() not in AUDIO_EXT:
                raise SystemExit(f"not an audio file: {path}")
            out.append(path)
        else:
            raise SystemExit(f"no such file: {path}")
    return [p for p in out if not inside_library(p)]


# --------------------------------------------------------------------------
# rollback


def rollback() -> int:
    if not JOURNAL.exists():
        print(f"no journal at {JOURNAL.relative_to(ROOT)}")
        return 0
    entries = [json.loads(line) for line in
               JOURNAL.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = failed = 0
    for entry in reversed(entries):
        if entry.get("action") != "file":
            continue
        source = Path(entry["from"]).expanduser()
        final = ROOT / entry["to"]
        if not final.is_file():
            print(f"  gone, cannot roll back: {entry['to']}")
            failed += 1
            continue
        if entry.get("md5") and md5_of(final) != entry["md5"]:
            print(f"  payload changed, refusing: {entry['to']}")
            failed += 1
            continue
        if source.exists():
            print(f"  source exists again, refusing: {source}")
            failed += 1
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(final), str(source))
        # drop the album/artist folders the import created, if now empty
        parent = final.parent
        while parent != ROOT and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
        done += 1
    print(f"rollback from {JOURNAL.relative_to(ROOT)}: {done} restored, "
          f"{failed} left")
    return 0 if not failed else 1


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="audio file(s) or folders")
    ap.add_argument("--artist", help="artist (folder and tag)")
    ap.add_argument("--album", help="album folder and tag")
    ap.add_argument("--title", help="title (single file only)")
    ap.add_argument("--track", type=int, help="track number")
    ap.add_argument("--genre", help="genre to write")
    ap.add_argument("--date", help="date/year to write")
    ap.add_argument("--grouping", default="Downloads",
                    help="GROUPING (shelf) tag; default Downloads")
    ap.add_argument("--dest", help="library-relative folder (e.g. Own)")
    ap.add_argument("--move", action="store_true",
                    help="remove the source instead of copying it")
    ap.add_argument("--recursive", action="store_true",
                    help="walk folders given as input")
    ap.add_argument("--overwrite-tags", action="store_true",
                    help="replace the fields given on the command line even "
                         "when the file already carries a value")
    ap.add_argument("--dry-run", action="store_true", help="change nothing")
    ap.add_argument("--no-index", action="store_true",
                    help="do not rescan the inventory / rebuild playlists")
    ap.add_argument("--moods", action="store_true",
                    help="also refresh the mood playlists")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    if args.rollback:
        return rollback()
    if not args.paths:
        ap.error("give at least one file (or --rollback)")
    if args.title and len(args.paths) > 1:
        ap.error("--title only works with a single file")

    sources = collect(args.paths, args.recursive)
    if not sources:
        raise SystemExit("nothing to import (all inputs are already inside "
                         "the library)")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    stage = STAGE / f"{stamp}-{os.getpid()}"
    rows: list[dict] = []
    failures = 0

    for source in sources:
        tags = read_tags(source)
        artist = (args.artist or tags.get("artist")
                  or tags.get("albumartist") or "").strip()
        album = (args.album or tags.get("album") or "").strip()
        title = (args.title or tags.get("title") or source.stem).strip()
        title = title.replace("_", " ").strip() or source.stem
        track = args.track if args.track is not None else first_int(
            tags.get("tracknumber"))
        if not artist and not args.dest:
            print(f"  no artist for {source.name}: pass --artist or --dest")
            failures += 1
            continue

        if args.overwrite_tags:
            given = {"artist": args.artist, "album": args.album,
                     "title": args.title, "genre": args.genre,
                     "date": args.date}
            tags = {k: v for k, v in tags.items() if not given.get(k)}

        ext = source.suffix.lower()
        rel = target_rel(artist or "Unknown", album, title, track, ext,
                         args.dest)
        final = free_path(ROOT / rel)
        rel = final.relative_to(ROOT).as_posix()

        if args.dry_run:
            print(f"  would file {source} -> {rel}")
            rows.append({"source": str(source), "path": rel, "artist": artist,
                         "album": album, "title": title, "track": track or "",
                         "size_mb": round(source.stat().st_size / 1e6, 1)})
            continue

        stage.mkdir(parents=True, exist_ok=True)
        staged = stage / f"{safe_name(title)}{ext}"
        counter = 2
        while staged.exists():
            staged = stage / f"{safe_name(title)}_{counter}{ext}"
            counter += 1
        shutil.copy2(source, staged)

        fields = [{"field": "title", "after": title},
                  {"field": "artist", "after": artist},
                  {"field": "albumartist", "after": artist}]
        if album:
            fields.append({"field": "album", "after": album})
        if track:
            fields.append({"field": "track", "after": str(track)})
        if args.genre or tags.get("genre"):
            fields.append({"field": "genre",
                           "after": args.genre or tags["genre"]})
        if args.date or tags.get("date"):
            fields.append({"field": "date",
                           "after": args.date or tags["date"]})
        fields.append({"field": "grouping", "after": args.grouping})
        fields = [f for f in fields if f["after"]]

        write_tags(staged.relative_to(ROOT).as_posix(), fields)

        digest = md5_of(staged)
        size = staged.stat().st_size
        journal_write({"ts": datetime.now(timezone.utc).isoformat(),
                       "action": "file", "mode": "move" if args.move else "copy",
                       "from": str(source.resolve()),
                       "staged": staged.relative_to(ROOT).as_posix(),
                       "to": rel, "size": size, "md5": digest,
                       "tags": {f["field"]: f["after"] for f in fields}})
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(final))
        if args.move:
            source.unlink()
        print(f"  filed {rel}  ({size / 1e6:.1f} MB)")
        rows.append({"source": str(source.resolve()), "path": rel,
                     "artist": artist, "album": album, "title": title,
                     "track": track or "", "genre": args.genre or
                     tags.get("genre", ""), "grouping": args.grouping,
                     "size_mb": round(size / 1e6, 1)})

    if not args.dry_run and stage.exists() and not any(stage.iterdir()):
        stage.rmdir()

    if rows and not args.dry_run:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        new = not REPORT.exists()
        with REPORT.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            if new:
                writer.writeheader()
            writer.writerows(rows)
        if not args.no_index:
            refresh_index()
            if args.moods:
                refresh_moods()

    print(f"\nfiled {len(rows)} of {len(sources)} file(s)"
          + (" (dry run, nothing changed)" if args.dry_run else ""))
    if rows and not args.dry_run and not args.no_index:
        print("inventory rescanned, playlists rebuilt"
              + (", moods refreshed" if args.moods else ""))
    if not args.dry_run:
        print(f"journal: {JOURNAL.relative_to(ROOT)}")
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
