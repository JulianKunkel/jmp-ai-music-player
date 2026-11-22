#!/usr/bin/env python3
"""Import playlists from a folder — or from Spotify — into Playlists/Custom/.

Two sources, one path into the library:

* **a folder** of `.m3u`, `.m3u8` or `.pls` files (recursively by default).
  That is what players and phones leave behind, and what people copy from an
  old machine.  Every entry is resolved against the live tree: an absolute
  path inside the library, a path relative to the playlist, a `file://` URI,
  a bare file name, or - as the last resort - an `#EXTINF` "artist - title"
  matched against the library tags.
* **a Spotify playlist** (or album / track), by public link or id.  The
  Spotify Web API would need credentials, so the public *embed* page is read
  instead: it is server-rendered and carries the playlist name, the track
  titles, the artists and the lengths.  Each entry is then matched against
  the local library by title and artist.

Nothing streams from Spotify; the import only *names* the tracks.  Entries the
library does not hold are reported, so they can be fetched with the normal
audio import module.

Writes only through `playlist_edit.write_custom()`, so every new list is
journalled to `library-work/journal/playlists.jsonl` before it appears and
paths stay relative (`../../Artists/...`).  Preview is the default; `--apply`
writes.  A list that already exists gets the next free name (`_2`), never a
silent overwrite.

Usage:
    music-cli pl import --folder ~/Downloads/playlists
    music-cli pl import --folder ~/Mail/playlists --apply
    music-cli pl import --folder ~/playlists --no-recursive --apply
    music-cli pl import --spotify https://open.spotify.com/playlist/37i9dQ...
    music-cli pl import --spotify 37i9dQZF1DXcBWIGoYBM5M --apply
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from libpaths import library_root                      # noqa: E402
import playlist_edit                                    # noqa: E402
from chat.catalog import AUDIO_EXT, Catalog, fold       # noqa: E402

PLAYLIST_EXT = {".m3u", ".m3u8", ".pls"}
ROOT = library_root()
REPORT = ROOT / "library-work" / "reports" / "playlist-import.csv"

#: a browser UA: the embed page is served to browsers, not to scripts
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


@dataclass
class Entry:
    """One playlist line, before it is matched to a file in the tree."""
    raw: str = ""
    title: str = ""
    artist: str = ""
    seconds: float | None = None


@dataclass
class Imported:
    source: str
    name: str
    entries: list[Entry] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    missing: list[Entry] = field(default_factory=list)
    target: str = ""
    note: str = ""


# --------------------------------------------------------------------------
# matching helpers


def _key(text: str) -> str:
    """A comparable key: folded, no extension, no track number, no separators."""
    text = fold(text)
    text = re.sub(r"^[\s\d._-]+", "", text)          # a leading track number
    text = re.sub(r"\.[a-z0-9]{2,4}$", "", text)     # an extension
    text = re.sub(r"[_\-]+", " ", text)
    return " ".join(text.split())


#: Spotify (and most shops) decorate a title: "(feat. X)", "[From …]",
#: " - 2004 Remaster".  None of that names the track, so it goes before matching.
_BRACKET = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*")
_SUFFIX = re.compile(r"\s+[-–—]\s+(?:.*?\b(?:remaster\w*|version|edit|mix|live|"
                     r"mono|stereo|single|album|radio|demo|re-?recorded|"
                     r"bonus|deluxe|acoustic|instrumental)\b.*)$", re.IGNORECASE)


def base_title(text: str) -> str:
    """The title without the decorations the shops add."""
    original = str(text or "")
    text = original
    previous = None
    while previous != text:                     # "(feat. X) [From Y]"
        previous = text
        text = _BRACKET.sub(" ", text).strip()
    text = _SUFFIX.sub("", text).strip()
    return text or original


def _artists(text: str) -> list[str]:
    parts = re.split(r",|;|/|&|\bfeat\.?\b|\bft\.?\b|\bwith\b", fold(text))
    return [part.strip(" .") for part in parts if part.strip(" .")]


def _similar(a: str, b: str) -> float:
    """0..1 agreement between two names: char ratio or shared words."""
    a, b = fold(a).strip(), fold(b).strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    words_a, words_b = set(a.split()), set(b.split())
    shared = len(words_a & words_b) / max(1, min(len(words_a), len(words_b)))
    return max(ratio, shared)


class Resolver:
    """Maps a playlist entry to a library-relative path."""

    def __init__(self, root: Path, catalog: Catalog):
        self.root = Path(root)
        self.catalog = catalog
        self.by_name: dict[str, str] = {}
        self.by_key: dict[str, str] = {}
        for rel in (row.get("rel_path") or "" for row in catalog.rows):
            if not rel:
                continue
            path = Path(rel)
            self.by_name.setdefault(path.name.casefold(), rel)
            self.by_key.setdefault(_key(path.stem), rel)

    # -- paths -------------------------------------------------------------

    def _inside(self, path: Path) -> str | None:
        try:
            rel = path.resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return None
        return rel if (self.root / rel).is_file() else None

    def from_path(self, raw: str, playlist_dir: Path | None) -> str | None:
        text = raw.strip().strip('"').strip("'")
        if not text or text.startswith("#"):
            return None
        if text.casefold().startswith("file://"):
            text = unquote(urlparse(text).path)
        candidate = Path(text)
        if candidate.is_absolute():
            found = self._inside(candidate)
            if found:
                return found
        elif playlist_dir is not None:
            found = self._inside(playlist_dir / candidate)
            if found:
                return found
        # a path from another machine: use the file name, then the stem
        name = candidate.name
        found = self.by_name.get(name.casefold())
        if found:
            return found
        if name.casefold().endswith(tuple(AUDIO_EXT)):
            return self.by_key.get(_key(name))
        return None

    # -- tags --------------------------------------------------------------

    def from_text(self, title: str, artist: str) -> str | None:
        base = base_title(title)
        if not base:
            return None
        names = _artists(artist)
        query = " ".join(part for part in (base, " ".join(names[:2])) if part)
        best = None
        best_score = 0.0
        for hit in self.catalog.search(query, 12):
            hit_base = base_title(hit.name)
            if _key(base) and _key(base) == _key(hit_base):
                title_score = 1.0
            else:
                title_score = _similar(base, hit_base)
            if names:
                who = fold(f"{hit.artist} {hit.album}")
                artist_score = max((_similar(name, who) for name in names),
                                   default=0.0)
                score = 0.72 * title_score + 0.28 * artist_score
            else:
                score = title_score
            if score > best_score:
                best, best_score = hit, score
        if best is not None and best_score >= 0.72:
            return best.rel
        return None

    def resolve(self, entry: Entry, playlist_dir: Path | None) -> str | None:
        found = self.from_path(entry.raw, playlist_dir) if entry.raw else None
        if found:
            return found
        if entry.title:
            return self.from_text(entry.title, entry.artist)
        return None


# --------------------------------------------------------------------------
# parsing playlist files


def _split_extinf(text: str) -> tuple[str, str]:
    """"Artist - Title" -> (title, artist); a bare line keeps the title."""
    text = text.strip()
    for dash in (" – ", " — ", " - ", "-"):
        if dash in text:
            left, _, right = text.partition(dash)
            left, right = left.strip(), right.strip()
            if left and right:
                return right, left
    return text, ""


def parse_playlist(path: Path) -> list[Entry]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.casefold() == ".pls":
        return _parse_pls(text)
    entries: list[Entry] = []
    pending = Entry()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            head, _, label = line.partition(",")
            seconds = None
            match = re.search(r":\s*(-?\d+)\s*$", head)
            if match:
                seconds = float(match.group(1))
            title, artist = _split_extinf(label)
            pending = Entry(title=title, artist=artist, seconds=seconds)
            continue
        if line.startswith("#"):
            continue
        entry = Entry(raw=line, title=pending.title, artist=pending.artist,
                      seconds=pending.seconds)
        entries.append(entry)
        pending = Entry()
    return entries


def _parse_pls(text: str) -> list[Entry]:
    rows: dict[int, Entry] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        match = re.match(r"(file|title|length)(\d+)$", key.strip().casefold())
        if not match:
            continue
        kind, number = match.group(1), int(match.group(2))
        entry = rows.setdefault(number, Entry())
        if kind == "file":
            entry.raw = value.strip()
        elif kind == "title":
            entry.title = value.strip()
        else:
            try:
                entry.seconds = float(value.strip())
            except ValueError:
                entry.seconds = None
    return [rows[number] for number in sorted(rows)]


def find_playlists(folder: Path, recursive: bool = True) -> list[Path]:
    pattern = "**/*" if recursive else "*"
    found = [path for path in folder.glob(pattern)
             if path.is_file() and path.suffix.casefold() in PLAYLIST_EXT]
    return sorted(found, key=lambda path: str(path).casefold())


# --------------------------------------------------------------------------
# Spotify


def spotify_id(text: str) -> tuple[str, str] | None:
    """(kind, id) for a Spotify link or a bare id."""
    text = text.strip()
    match = re.search(r"open\.spotify\.com/(?:intl-[a-z-]+/)?"
                      r"(playlist|album|track|artist)/([A-Za-z0-9]+)", text)
    if match:
        return match.group(1), match.group(2)
    match = re.search(r"spotify:(playlist|album|track|artist):([A-Za-z0-9]+)",
                      text)
    if match:
        return match.group(1), match.group(2)
    if re.fullmatch(r"[A-Za-z0-9]{22}", text):
        return "playlist", text
    return None


def _fetch(url: str, timeout: float = 25.0) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "en",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def spotify_entries(url: str) -> Imported:
    """Read the public embed page: name, titles, artists, lengths."""
    found = spotify_id(url)
    if not found:
        raise SystemExit("not a Spotify link or id: " + url)
    kind, ident = found
    page = _fetch(f"https://open.spotify.com/embed/{kind}/{ident}")
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">'
                      r"(.*?)</script>", page, re.S)
    if not match:
        raise SystemExit("Spotify returned no playlist data (private list?)")
    data = json.loads(match.group(1))
    entity = (data.get("props", {}).get("pageProps", {}).get("state", {})
              .get("data", {}).get("entity") or {})
    if not entity:
        raise SystemExit("Spotify returned no playlist data (private list?)")
    name = str(entity.get("name") or entity.get("title") or "Spotify")
    tracks = entity.get("trackList") or []
    entries = []
    for item in tracks:
        title = str(item.get("title") or "").strip()
        artist = str(item.get("subtitle") or "").replace("\u00a0", " ")
        artist = ", ".join(part.strip() for part in artist.split(",") if part.strip())
        seconds = item.get("duration")
        entries.append(Entry(title=title, artist=artist,
                             seconds=(float(seconds) / 1000.0
                                      if isinstance(seconds, (int, float))
                                      else None)))
    imported = Imported(source=f"spotify:{ident}", name=name, entries=entries)
    imported.note = (f"{len(entries)} tracks on the embed page"
                     + ("" if entries else " (empty or private)"))
    return imported


# --------------------------------------------------------------------------
# importing


def _write(name: str, rels: list[str], root: Path) -> Path:
    with playlist_edit.at_root(root):
        return playlist_edit.write_custom(name, rels)


def collect(source: Imported, resolver: Resolver,
            playlist_dir: Path | None) -> Imported:
    for entry in source.entries:
        rel = resolver.resolve(entry, playlist_dir)
        if rel:
            if rel not in source.resolved:
                source.resolved.append(rel)
        else:
            source.missing.append(entry)
    return source


def import_folder(folder: Path, root: Path, *, recursive: bool = True,
                  apply: bool = False, limit: int = 0) -> list[Imported]:
    if not folder.is_dir():
        raise SystemExit(f"no such folder: {folder}")
    files = find_playlists(folder, recursive)
    if limit:
        files = files[:limit]
    catalog = Catalog(root)
    resolver = Resolver(root, catalog)
    results = []
    for path in files:
        try:
            entries = parse_playlist(path)
        except OSError as exc:
            results.append(Imported(source=str(path),
                                    name=path.stem,
                                    note=f"unreadable: {exc}"))
            continue
        name = playlist_edit.clean_list_name(path.stem)
        imported = Imported(source=str(path), name=name, entries=entries)
        collect(imported, resolver, path.parent)
        if apply and imported.resolved:
            try:
                target = _write(name, imported.resolved, root)
                imported.target = str(target.relative_to(root))
            except SystemExit as exc:
                imported.note = str(exc)
        results.append(imported)
    return results


def import_spotify(url: str, root: Path, *, apply: bool = False) -> Imported:
    source = spotify_entries(url)
    catalog = Catalog(root)
    resolver = Resolver(root, catalog)
    source.name = playlist_edit.clean_list_name(source.name)
    collect(source, resolver, None)
    if apply and source.resolved:
        try:
            target = _write(source.name, source.resolved, root)
            source.target = str(target.relative_to(root))
        except SystemExit as exc:
            source.note = (source.note + "; " if source.note else "") + str(exc)
    return source


# --------------------------------------------------------------------------
# reporting


def write_report(results: list[Imported], path: Path = REPORT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = not path.is_file()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(["imported_at", "source", "playlist", "entries",
                             "resolved", "missing", "target", "note"])
        for item in results:
            writer.writerow([stamp, item.source, item.name, len(item.entries),
                             len(item.resolved), len(item.missing),
                             item.target, item.note])
    return path


def _print(results: list[Imported], apply: bool) -> None:
    total_entries = sum(len(item.entries) for item in results)
    total_found = sum(len(item.resolved) for item in results)
    for item in results:
        state = (f"-> {item.target}" if item.target else
                 "(preview, nothing written)" if apply else "")
        print(f"{item.name:<32} {len(item.entries):4d} entries "
              f"{len(item.resolved):4d} found {len(item.missing):4d} missing "
              f"{state}".rstrip())
        if item.note:
            print(f"    {item.note}")
        for entry in item.missing[:6]:
            label = entry.title or entry.raw
            who = f" — {entry.artist}" if entry.artist else ""
            print(f"    missing: {label}{who}")
        if len(item.missing) > 6:
            print(f"    … and {len(item.missing) - 6} more")
    print(f"\n{len(results)} playlist(s), {total_entries:,} entries, "
          f"{total_found:,} matched locally "
          f"({total_found / max(1, total_entries):.0%}).")
    if not apply:
        print("Nothing was written. Re-run with --apply to create the lists.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", default="",
                        help="folder with .m3u/.m3u8/.pls files")
    parser.add_argument("--spotify", default="",
                        help="Spotify playlist link or id")
    parser.add_argument("--apply", action="store_true",
                        help="create the playlists (default: preview)")
    parser.add_argument("--no-recursive", action="store_true",
                        help="do not look into sub-folders")
    parser.add_argument("--limit", type=int, default=0,
                        help="at most this many playlist files")
    parser.add_argument("--root", default="", help="library root")
    parser.add_argument("--report", default=str(REPORT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser() if args.root else ROOT
    if not args.folder and not args.spotify:
        parser.error("give --folder or --spotify")

    results: list[Imported] = []
    if args.folder:
        results += import_folder(Path(args.folder).expanduser(), root,
                                 recursive=not args.no_recursive,
                                 apply=args.apply, limit=args.limit)
    if args.spotify:
        results.append(import_spotify(args.spotify, root, apply=args.apply))

    path = write_report(results, Path(args.report))
    if args.json:
        print(json.dumps([{
            "source": item.source, "playlist": item.name,
            "entries": len(item.entries), "resolved": len(item.resolved),
            "missing": [entry.title or entry.raw for entry in item.missing],
            "target": item.target, "note": item.note,
        } for item in results], indent=2))
    else:
        _print(results, args.apply)
        print(f"report: {path}")
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
