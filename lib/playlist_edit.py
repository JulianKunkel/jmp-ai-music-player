#!/usr/bin/env python3
"""Add, remove and create playlist entries — the journalled way.

`make_playlists.py` *generates* the shelf lists from `GROUPING` and would
overwrite any hand edit, so those lists are never edited here.  This tool edits
lists that survive a rebuild: anything under `Playlists/Custom/` (also under
`Playlists/Moods/`, although the mood generator owns those), and it refuses to
touch a generated shelf list unless `--force` is given.  `Playlists/Own/` holds
the ad-hoc lists the chat leaves behind ("play something smooth"); the API
below can save one into `Custom/` or delete it, and keeps the folder pruned.

Every list targets the live tree with relative paths, so the library can be
mounted anywhere.  A track may appear once per list; adding a duplicate is
skipped.  Paths are validated before anything is written: outside the library,
non-audio and missing files are refused.

Journal: `library-work/journal/playlists.jsonl`, written and fsync'd **before**
the file changes, one line per edit with the whole previous content (base64).
`--rollback` restores every edited list in reverse order.

Usage:
    music-cli pl list
    music-cli pl show Mix_Chill
    music-cli pl new Mix_Chill
    music-cli pl add Mix_Chill --track Artists/X/Y/01_Z.mp3
    music-cli pl add Mix_Chill --index 2 --track ...
    music-cli pl remove Mix_Chill --index 3
    music-cli pl delete Mix_Chill
    music-cli pl check
    music-cli pl --rollback
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from mutagen import File as MFile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from libpaths import library_root  # noqa: E402

ROOT = library_root()
PLAYLISTS = ROOT / "Playlists"
CUSTOM = PLAYLISTS / "Custom"
OWN = PLAYLISTS / "Own"
MOODS = PLAYLISTS / "Moods"
SMART = PLAYLISTS / "Smart"
JOURNAL = ROOT / "library-work" / "journal" / "playlists.jsonl"

AUDIO_EXT = {".mp3", ".mp2", ".m4a", ".mp4", ".opus", ".ogg", ".flac",
             ".wav", ".wma", ".aac", ".spc", ".sid", ".mod", ".xm", ".it",
             ".s3m", ".ym", ".play"}


# --------------------------------------------------------------------------
# paths and entries
#
# The CLI works on one library, but the chat can be pointed at another one
# (tests, or a library chosen with --root).  `at_root` swaps the module-level
# paths for the duration of a call; every function below reads them at call
# time, so no other change is needed.


@contextmanager
def at_root(root):
    """Run playlist edits against `root` instead of the start-up library."""
    global ROOT, PLAYLISTS, CUSTOM, OWN, MOODS, SMART, JOURNAL
    saved = (ROOT, PLAYLISTS, CUSTOM, OWN, MOODS, SMART, JOURNAL)
    ROOT = Path(root)
    PLAYLISTS = ROOT / "Playlists"
    CUSTOM = PLAYLISTS / "Custom"
    OWN = PLAYLISTS / "Own"
    MOODS = PLAYLISTS / "Moods"
    SMART = PLAYLISTS / "Smart"
    JOURNAL = ROOT / "library-work" / "journal" / "playlists.jsonl"
    try:
        yield ROOT
    finally:
        ROOT, PLAYLISTS, CUSTOM, OWN, MOODS, SMART, JOURNAL = saved


def library_rel(path: Path) -> str:
    """A path relative to the library root (no `Music/`, no `..`)."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        raise SystemExit(f"not inside the library: {path}")


def absolute(rel: str) -> Path:
    return ROOT / rel


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8-sig").splitlines()


def entries(path: Path) -> list[tuple[int, str, str]]:
    """(line index, EXTINF label, library-relative path) for audio entries."""
    out = []
    lines = read_lines(path)
    for i, line in enumerate(lines):
        if not line.strip() or line.startswith("#"):
            continue
        # the entry is relative to the playlist file, which is how VLC and mpv
        # resolve it; store the library-relative form so we can compare
        target = (path.parent / line.strip()).resolve()
        label = ""
        if i and lines[i - 1].startswith("#EXTINF:"):
            label = lines[i - 1].split(",", 1)[-1]
        try:
            rel = target.relative_to(ROOT).as_posix()
        except ValueError:
            rel = ""
        out.append((i, label, rel))
    return out


def extinf_for(rel: str) -> str:
    """`#EXTINF:<seconds>,<artist> - <title>` for one track."""
    path = absolute(rel)
    seconds = -1
    artist = title = ""
    try:
        audio = MFile(path, easy=True)
        if audio is not None:
            if audio.info is not None and audio.info.length:
                seconds = int(round(audio.info.length))
            tags = audio.tags or {}
            artist = (tags.get("artist") or [""])[0]
            if not artist:
                artist = (tags.get("albumartist") or [""])[0]
            title = (tags.get("title") or [""])[0]
    except Exception:
        pass
    if not title:
        title = Path(rel).stem
    label = f"{artist} - {title}" if artist and title else (title or artist)
    return f"#EXTINF:{seconds},{label}"


def dedupe_key(rel: str) -> str:
    return os.path.normcase(rel)


# --------------------------------------------------------------------------
# journal


def journal_write(entry: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def journal_for(path: Path, action: str) -> dict:
    before = path.read_bytes() if path.exists() else b""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "path": library_rel(path),
        "existed": path.exists(),
        "before_b64": base64.b64encode(before).decode("ascii"),
    }


def rollback() -> int:
    if not JOURNAL.exists():
        print(f"no journal at {JOURNAL.relative_to(ROOT)}")
        return 0
    entries = [json.loads(line) for line in
               JOURNAL.read_text(encoding="utf-8").splitlines() if line.strip()]
    stats = collections.Counter()
    for e in reversed(entries):
        path = ROOT / e["path"]
        if not e["existed"]:
            if path.exists():
                path.unlink()
                stats["removed (was new)"] += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(e["before_b64"]))
        stats["restored"] += 1
    print(f"rollback from {JOURNAL.relative_to(ROOT)} ({len(entries):,} entries)")
    for key, value in stats.most_common():
        print(f"  {value:,}  {key}")
    return 0


# --------------------------------------------------------------------------
# locating a playlist


def find(name: str, must_exist: bool = True) -> Path:
    """`Custom:Mix`, `Moods:Mood_Chill`, a name, or a path."""
    candidate = Path(name)
    if candidate.suffix == ".m3u8" or "/" in name:
        path = candidate if candidate.is_absolute() else Path.cwd() / candidate
        if path.is_file():
            return path.resolve()
        path = ROOT / name if not candidate.is_absolute() else path
        if path.is_file():
            return path.resolve()
        if must_exist:
            raise SystemExit(f"no such playlist: {name}")
        return path
    if ":" in name:
        where, _, rest = name.partition(":")
        base = {"custom": CUSTOM, "own": OWN, "moods": MOODS,
                "mood": MOODS}.get(where.lower())
        if base is None:
            raise SystemExit(f"unknown playlist group: {where}")
        path = base / f"{rest}.m3u8"
        if must_exist and not path.is_file():
            raise SystemExit(f"no such playlist: {path.relative_to(ROOT)}")
        return path
    for base in (CUSTOM, OWN, MOODS, PLAYLISTS):
        path = base / f"{name}.m3u8"
        if path.is_file():
            return path
    if must_exist:
        raise SystemExit(f"no such playlist: {name}")
    return CUSTOM / f"{name}.m3u8"


def is_generated(path: Path) -> bool:
    """Shelf lists are rebuilt from `GROUPING`; editing one is pointless."""
    try:
        rel = path.resolve().relative_to(PLAYLISTS.resolve())
    except ValueError:
        return False
    if rel.parts and rel.parts[0] in {"Smart", "Psycho", "Moods"}:
        return True
    return len(rel.parts) == 1


# --------------------------------------------------------------------------
# commands


def cmd_list(args) -> int:
    groups = (("shelf (generated)", PLAYLISTS.glob("*.m3u8")),
              ("moods", MOODS.glob("*.m3u8")),
              ("own (ad-hoc)", OWN.glob("*.m3u8")),
              ("custom", CUSTOM.glob("*.m3u8")))
    for title, found in groups:
        paths = sorted(p for p in found if p.is_file())
        print(f"{title}: {len(paths)}")
        for path in paths:
            print(f"  {len(entries(path)):6,}  {path.stem}"
                  f"  ({path.relative_to(ROOT)})")
    return 0


def cmd_show(args) -> int:
    path = find(args.name)
    found = entries(path)
    print(f"{path.relative_to(ROOT)}: {len(found)} tracks")
    for number, (_, label, rel) in enumerate(found, 1):
        mark = "" if rel and absolute(rel).is_file() else "  [MISSING]"
        print(f"  {number:4}  {label}{mark}")
        print(f"        {rel or '?'}")
    return 0


def cmd_new(args) -> int:
    path = find(args.name, must_exist=False)
    if path.exists():
        raise SystemExit(f"already exists: {path.relative_to(ROOT)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    journal_write(journal_for(path, "new"))
    path.write_text("#EXTM3U\n", encoding="utf-8")
    print(f"created {path.relative_to(ROOT)}")
    return 0


def resolve_track(text: str) -> str:
    """Accept a library path, a path relative to CWD, or a playlist entry."""
    for base in (Path.cwd(), ROOT):
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = base / text
        if candidate.is_file():
            rel = library_rel(candidate)
            if Path(rel).suffix.lower() not in AUDIO_EXT:
                raise SystemExit(f"not an audio file: {text}")
            return rel
    raise SystemExit(f"no such file: {text}")


def blocks(path: Path) -> tuple[list[str], list[list[str]]]:
    """(header lines, entry blocks); a block is [#EXTINF, ..., path]."""
    lines = read_lines(path)
    head: list[str] = []
    out: list[list[str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("#EXTINF:"):
            block = [line]
            i += 1
            while i < len(lines) and (not lines[i].strip()
                                      or lines[i].startswith("#")):
                block.append(lines[i])
                i += 1
            if i < len(lines):
                block.append(lines[i])
                i += 1
            out.append(block)
            continue
        if line.startswith("#"):
            if not out:
                head.append(line)
            i += 1
            continue
        out.append([line])
        i += 1
    return head, out


def block_rel(block: list[str]) -> str:
    return block[-1].strip()


def write_blocks(path: Path, head: list[str], groups: list[list[str]]) -> None:
    lines = list(head) or ["#EXTM3U"]
    for block in groups:
        lines.extend(block)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# programmatic API (used by the chat)
#
# These functions write the same journal entries as the commands above, but
# do a whole list in one go: the chat creates a 40-track playlist, and 40
# separate journal snapshots of a growing list would be pure overhead.


def clean_list_name(name: str) -> str:
    """A terminal-safe playlist name, in the same style as the file names."""
    name = re.sub(r"['’`´]", "", str(name).strip())
    name = re.sub(r"[^\w.\- ]+", "_", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"_+", "_", name).strip("._-")
    return (name or "Mix")[:80]


def custom_path(name: str) -> Path:
    return CUSTOM / f"{clean_list_name(name)}.m3u8"


def own_path(name: str) -> Path:
    return OWN / f"{clean_list_name(name)}.m3u8"


def smart_path(name: str) -> Path:
    return SMART / f"{clean_list_name(name)}.m3u8"


def _validated_rels(rels: list[str]) -> list[str]:
    out: list[str] = []
    for rel in rels:
        candidate = Path(rel)
        probe = candidate if candidate.is_absolute() else ROOT / rel
        if not probe.is_file():
            raise SystemExit(f"no such file: {rel}")
        if probe.suffix.lower() not in AUDIO_EXT:
            raise SystemExit(f"not an audio file: {rel}")
        try:
            out.append(probe.resolve().relative_to(ROOT).as_posix())
        except ValueError:
            raise SystemExit(f"not inside the library: {rel}")
    return out


def _entry_block(path: Path, rel: str) -> list[str]:
    return [
        extinf_for(rel),
        os.path.relpath(absolute(rel), path.parent).replace(os.sep, "/"),
    ]


def write_custom(name: str, rels: list[str], overwrite: bool = False,
                 replace: bool = False) -> Path:
    """Create (or replace) a custom playlist with exactly these tracks.

    One journal entry covers the whole write: it records the previous content
    (empty for a new list) before the file changes.  An existing list is only
    touched when `overwrite`/`replace` is set; otherwise the next free name is
    used, so a request never silently destroys a hand-made list.
    """
    rels = _validated_rels(rels)
    path = custom_path(name)
    if path.exists() and not (overwrite or replace):
        base = path.stem
        for number in range(2, 100):
            trial = CUSTOM / f"{base}_{number}.m3u8"
            if not trial.exists():
                path = trial
                break
        else:
            raise SystemExit(f"too many playlists named {base!r}")
    if path.exists() and is_generated(path):
        raise SystemExit(f"{path.relative_to(ROOT)} is a generated list")
    if path.exists() and not replace and not overwrite:
        raise SystemExit(f"already exists: {path.relative_to(ROOT)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    journal_write(journal_for(path, "replace" if path.exists() else "new"))
    lines = ["#EXTM3U"]
    for rel in rels:
        lines.extend(_entry_block(path, rel))
    tmp = path.with_suffix(".m3u8.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


# -- ad-hoc lists the app leaves behind for "play something …" ------------
#
# A queue the chat built from a request is a real playlist file the listener
# can keep or throw away.  It lives in `Playlists/Own/`, is journalled like any
# other list, and is pruned to the newest few so the folder cannot grow
# without bound.  "Save" writes it to `Playlists/Custom/` and removes the
# ad-hoc copy; "remove" deletes only the ad-hoc copy.


def write_own(name: str, rels: list[str], label: str = "") -> Path:
    """Create (or replace) an ad-hoc list in `Playlists/Own/`, journalled."""
    rels = _validated_rels(rels)
    path = own_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    journal_write(journal_for(path, "replace" if path.exists() else "new"))
    lines = ["#EXTM3U"]
    if label:
        lines.append(f"# {label}")
    for rel in rels:
        lines.extend(_entry_block(path, rel))
    tmp = path.with_suffix(".m3u8.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _require_own(path: Path) -> None:
    try:
        inside = path.resolve().parent == OWN.resolve()
    except OSError:
        inside = False
    if not inside:
        raise SystemExit(
            f"{path.relative_to(ROOT)} is not an ad-hoc list "
            "(only `Playlists/Own/` is managed here)")


def write_smart(name: str, rels: list[str], label: str = "") -> Path:
    """Write a rule-generated list under `Playlists/Smart/`, journalled.

    A smart list is rebuilt from the rules, so it is generated output like the
    shelf and mood lists: the write is recorded (with the previous content)
    before it happens, and the chat's playlist editor never edits or deletes
    it — changing the rules and rebuilding is the way.
    """
    rels = _validated_rels(rels)
    path = smart_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    journal_write(journal_for(path, "replace" if path.exists() else "new"))
    lines = ["#EXTM3U"]
    if label:
        lines.append(f"# {label}")
    for rel in rels:
        lines.extend(_entry_block(path, rel))
    tmp = path.with_suffix(".m3u8.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def delete_own(name: str) -> Path:
    """Remove one ad-hoc list, journalling its content first."""
    path = find(name)
    _require_own(path)
    journal_write(journal_for(path, "delete"))
    path.unlink()
    return path


def prune_own(keep: int = 12, protect=()) -> list[Path]:
    """Delete the oldest ad-hoc lists beyond `keep` (journalled each)."""
    if not OWN.is_dir():
        return []
    try:
        paths = sorted((path for path in OWN.glob("*.m3u8") if path.is_file()),
                       key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return []
    keep = max(1, int(keep))
    guarded = {str(name) for name in (protect or ())}
    removed: list[Path] = []
    for path in paths[keep:]:
        if path.name in guarded:
            continue
        try:
            journal_write(journal_for(path, "delete"))
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


def save_own(name: str, new_name: str = "",
             overwrite: bool = False) -> tuple[Path, Path]:
    """Keep an ad-hoc list as a custom playlist, then drop the ad-hoc copy."""
    source = find(name)
    _require_own(source)
    rels = [rel for _, _, rel in entries(source) if rel]
    if not rels:
        raise SystemExit(f"{source.relative_to(ROOT)} has no playable entries")
    target_name = (new_name or "").strip() or re.sub(
        r"_\d{8}-\d{6}$", "", source.stem) or "Mix"
    target = write_custom(target_name, rels, overwrite=overwrite)
    journal_write(journal_for(source, "delete"))
    source.unlink()
    return target, source


def remove_from_custom(name: str, indexes: list[int]) -> tuple[Path, int]:
    """Remove entries by their 1-based position; journals before the write."""
    path = find(name, must_exist=False)
    if not path.exists():
        raise SystemExit(f"no such playlist: {name}")
    if is_generated(path):
        raise SystemExit(f"{path.relative_to(ROOT)} is generated — not editable")
    head, groups = blocks(path)
    drop = {number - 1 for number in indexes
            if 1 <= number <= len(groups)}
    if not drop:
        return path, 0
    journal_write(journal_for(path, "remove"))
    write_blocks(path, head, [block for index, block in enumerate(groups)
                              if index not in drop])
    return path, len(drop)


def append_custom(name: str, rels: list[str]) -> tuple[Path, int]:
    """Add tracks to a custom list; creates it when it does not exist."""
    rels = _validated_rels(rels)
    path = find(name, must_exist=False)
    if is_generated(path):
        raise SystemExit(f"{path.relative_to(ROOT)} is generated — "
                         "use a custom list")
    if not path.exists():
        return write_custom(name, rels), len(rels)
    head, groups = blocks(path)
    present = set()
    for block in groups:
        try:
            present.add(dedupe_key(
                (path.parent / block_rel(block)).resolve()
                .relative_to(ROOT).as_posix()))
        except (ValueError, OSError):
            pass
    fresh: list[list[str]] = []
    for rel in rels:
        key = dedupe_key(rel)
        if key in present:
            continue
        present.add(key)
        fresh.append(_entry_block(path, rel))
    if not fresh:
        return path, 0
    journal_write(journal_for(path, "add"))
    write_blocks(path, head, groups + fresh)
    return path, len(fresh)


def delete_custom(name: str, force: bool = False) -> Path:
    """Delete a playlist file, journalling its content first.

    A generated shelf list is refused unless `force` is set: it is rebuilt from
    `GROUPING`, so deleting it only means it comes back.  A custom list is the
    only list a listener really owns.
    """
    path = find(name)
    if is_generated(path) and not force:
        raise SystemExit(
            f"{path.relative_to(ROOT)} is a generated shelf list; it comes "
            "back on the next rebuild (--force overrides)")
    journal_write(journal_for(path, "delete"))
    path.unlink()
    return path


def rename_custom(name: str, new_name: str) -> Path:
    """Rename a custom playlist; both sides are journalled.

    The old path is journalled as a delete (its content is kept in the journal)
    and the new path as a creation, so `--rollback` restores the old file and
    removes the new one.
    """
    path = find(name)
    try:
        inside = path.resolve().parent == CUSTOM.resolve()
    except OSError:
        inside = False
    if not inside:
        raise SystemExit(
            f"{path.relative_to(ROOT)} is not a custom list — the shelf, mood "
            "and focus lists are generated, so only custom lists are renamed")
    target = CUSTOM / f"{clean_list_name(new_name)}.m3u8"
    if target.resolve() == path.resolve():
        return path
    if target.exists():
        raise SystemExit(f"already exists: {target.relative_to(ROOT)}")
    journal_write(journal_for(path, "delete"))
    journal_write({
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": "new",
        "path": library_rel(target),
        "existed": False,
        "before_b64": "",
    })
    os.replace(path, target)
    return target


def cmd_add(args) -> int:
    path = find(args.name)
    if is_generated(path) and not args.force:
        raise SystemExit(
            f"{path.relative_to(ROOT)} is a generated shelf list and is "
            "rebuilt from GROUPING — use a custom list "
            f"(`new {args.name}`) or pass --force")
    head, groups = blocks(path)
    present = set()
    for block in groups:
        target = path.parent / block_rel(block)
        try:
            present.add(dedupe_key(target.resolve()
                                   .relative_to(ROOT).as_posix()))
        except (ValueError, OSError):
            pass

    new_blocks = []
    skipped = 0
    for text in args.track:
        rel = resolve_track(text)
        if dedupe_key(rel) in present:
            print(f"  already in the list: {rel}")
            skipped += 1
            continue
        present.add(dedupe_key(rel))
        new_blocks.append([
            extinf_for(rel),
            os.path.relpath(absolute(rel), path.parent).replace(os.sep, "/"),
        ])

    if not new_blocks:
        print("nothing to add")
        return 0

    if args.index:
        if args.index < 1 or args.index > len(groups) + 1:
            raise SystemExit(f"--index must be 1..{len(groups) + 1}")
        at = args.index - 1
        groups[at:at] = new_blocks
    else:
        groups.extend(new_blocks)

    if args.dry_run:
        print(f"would add {len(new_blocks)} track(s) to "
              f"{path.relative_to(ROOT)}")
        for block in new_blocks:
            print("  " + block[-1])
        return 0

    journal_write(journal_for(path, "add"))
    write_blocks(path, head, groups)
    print(f"added {len(new_blocks)} track(s) to {path.relative_to(ROOT)}"
          + (f", {skipped} already there" if skipped else ""))
    return 0


def cmd_remove(args) -> int:
    path = find(args.name)
    if is_generated(path) and not args.force:
        raise SystemExit(
            f"{path.relative_to(ROOT)} is a generated shelf list; edit the "
            "GROUPING tag or use a custom list (--force overrides)")
    head, groups = blocks(path)
    rels = []
    for block in groups:
        target = path.parent / block_rel(block)
        try:
            rels.append(target.resolve().relative_to(ROOT).as_posix())
        except (ValueError, OSError):
            rels.append("")
    drop: set[int] = set()
    for number in args.index:
        if number < 1 or number > len(groups):
            raise SystemExit(f"--index {number} is outside 1..{len(groups)}")
        drop.add(number - 1)
    for text in args.track:
        rel = resolve_track(text)
        hit = [i for i, have in enumerate(rels)
               if have and dedupe_key(have) == dedupe_key(rel)]
        if not hit:
            print(f"  not in the list: {rel}")
            continue
        drop.update(hit)
    if not drop:
        print("nothing to remove")
        return 0
    if args.dry_run:
        print(f"would remove {len(drop)} track(s) from "
              f"{path.relative_to(ROOT)}")
        return 0
    kept = [b for i, b in enumerate(groups) if i not in drop]
    journal_write(journal_for(path, "remove"))
    write_blocks(path, head, kept)
    print(f"removed {len(drop)} track(s) from {path.relative_to(ROOT)}")
    return 0


def cmd_delete(args) -> int:
    path = find(args.name)
    if args.dry_run:
        print(f"would delete {path.relative_to(ROOT)}")
        return 0
    delete_custom(args.name, force=args.force)
    print(f"deleted {path.relative_to(ROOT)} (content is in the journal)")
    return 0


def cmd_check(args) -> int:
    if args.name:
        paths = [find(args.name)]
    else:
        paths = sorted([p for p in PLAYLISTS.glob("*.m3u8") if p.is_file()]
                       + [p for p in MOODS.glob("*.m3u8") if p.is_file()]
                       + [p for p in CUSTOM.glob("*.m3u8") if p.is_file()])
    bad = 0
    for path in paths:
        found = entries(path)
        missing = [rel for _, _, rel in found
                   if not rel or not absolute(rel).is_file()]
        outside = [rel for _, _, rel in found if not rel]
        if missing:
            bad += len(missing)
            print(f"[FAIL] {path.relative_to(ROOT)}: {len(missing)} dead "
                  f"entr{'y' if len(missing) == 1 else 'ies'}")
            for rel in missing[:5]:
                print(f"       {rel or '<outside the library>'}")
        else:
            print(f"[ok  ] {path.relative_to(ROOT)}: {len(found)} tracks")
        if outside:
            print(f"       {len(outside)} entr(y/ies) point outside the tree")
    return 0 if not bad else 1


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="allow editing a generated shelf list")
    ap.add_argument("--rollback", action="store_true")
    sub = ap.add_subparsers(dest="command", required=False)

    sub.add_parser("list", help="every playlist with its track count")

    p = sub.add_parser("show", help="entries of one playlist")
    p.add_argument("name")

    p = sub.add_parser("new", help="create an empty playlist")
    p.add_argument("name")

    p = sub.add_parser("add", help="append or insert tracks")
    p.add_argument("name")
    p.add_argument("--track", action="append", default=[], required=True)
    p.add_argument("--index", type=int, default=None,
                   help="1-based position (default: append)")

    p = sub.add_parser("remove", help="drop tracks")
    p.add_argument("name")
    p.add_argument("--track", action="append", default=[])
    p.add_argument("--index", type=int, action="append", default=[],
                   help="1-based entry number (repeatable)")

    p = sub.add_parser("delete", help="delete a playlist file")
    p.add_argument("name")

    p = sub.add_parser("check", help="verify entries resolve")
    p.add_argument("name", nargs="?")

    args = ap.parse_args()
    if args.rollback:
        return rollback()
    if not args.command:
        ap.print_help()
        return 1
    handler = {
        "list": cmd_list, "show": cmd_show, "new": cmd_new, "add": cmd_add,
        "remove": cmd_remove, "delete": cmd_delete, "check": cmd_check,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
