"""Edit a song's metadata and cover art from the app — always journalled.

Two kinds of write live here, and both keep the rule the library tools follow:
the previous state is recorded in `library-work/journal/` and fsync'd *before*
the file changes, so a change can be traced and taken back.

* **Tags** go through `scripts/tag_fields.py` with a one-file plan, exactly as
  every other tag pass does.  That tool checks the current value against the
  plan's `before` (a concurrent change counts as drift and is skipped), keeps
  its own byte-exact rollback for MP3 and field-by-field for the rest, and
  appends to `field-writes.jsonl`.  Afterwards the inventory row is rewritten
  so search sees the new title without a full re-index.
* **Cover art** is embedded (or a `cover.jpg` written next to the album) by
  this module, because no library tool accepts an image from the app.  The
  previous picture — bytes, MIME and file name — is journalled to
  `artwork-edit.jsonl` first.

Only a file the tag writer supports is editable: `.spc`, `.sid`, tracker
modules and a bare `.wav` have no tag field, so they are refused with a
reason instead of a silent no-op.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .catalog import library_rel

#: the fields the editor offers, in display order
FIELDS = ("title", "artist", "albumartist", "album", "track", "genre",
          "date", "comment")

LABELS = {
    "title": "Title",
    "artist": "Artist",
    "albumartist": "Album artist",
    "album": "Album",
    "track": "Track",
    "genre": "Genre",
    "date": "Date",
    "comment": "Comment",
}

#: a change with "apply to the whole album" may only touch these
ALBUM_FIELDS = ("album", "albumartist", "genre", "date")

#: containers `tag_fields.py` can write
TAGGABLE = {".mp3", ".mp2", ".flac", ".ogg", ".oga", ".opus", ".m4a",
            ".mp4", ".aac", ".wma"}

#: how large an uploaded cover may be (the base64 body is a little bigger)
ART_MAX = 6 * 1024 * 1024
#: a preview is only inlined below this size
PREVIEW_MAX = 1_500_000
ART_MIME = {"image/jpeg": ".jpg", "image/png": ".png"}

COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg",
               "folder.jpeg", "folder.png", "front.jpg", "front.jpeg",
               "front.png", "albumart.jpg", "albumart.png", "album.jpg",
               "album.png")
_COVER_RE = re.compile(r"^(?:cover|folder|front|albumart\d*|album)\.(?:jpe?g|png)$",
                       re.IGNORECASE)

#: what the inventory counts as a folder image (same set as the scanner)
IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}

_ROW_CACHE: dict[tuple[str, int, int], dict[str, dict]] = {}


# --------------------------------------------------------------------------
# the inventory row (what search shows, and the values the editor starts from)


def _rows(root: Path) -> dict[str, dict]:
    path = Path(root) / "library-work" / "inventory" / "files.jsonl"
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_size, int(stat.st_mtime))
    cached = _ROW_CACHE.get(key)
    if cached is not None:
        return cached
    rows: dict[str, dict] = {}
    try:
        with path.open("rb") as handle:
            for chunk in handle:
                if not chunk.strip():
                    continue
                try:
                    row = json.loads(chunk)
                except ValueError:
                    continue
                rows[library_rel(str(row.get("rel_path") or ""))] = row
    except OSError:
        return {}
    _ROW_CACHE.clear()
    _ROW_CACHE[key] = rows
    return rows


def row(root: Path, rel: str) -> dict:
    return _rows(Path(root)).get(library_rel(rel), {})


def album_folder(root: Path, rel: str) -> str:
    """The album folder a file sits in, library-relative (no `Music/`)."""
    stored = str(row(root, rel).get("album_dir") or "")
    if stored:
        return library_rel(stored).rstrip("/")
    return Path(library_rel(rel)).parent.as_posix()


def album_members(root: Path, rel: str) -> list[dict]:
    """Every editable file in the same album folder, plus itself."""
    folder = album_folder(root, rel)
    members = []
    for other, info in _rows(root).items():
        if Path(other).parent.as_posix() != folder:
            continue
        if Path(other).suffix.lower() not in TAGGABLE:
            continue
        members.append({
            "rel": other,
            "title": str(info.get("title") or ""),
            "track": str(info.get("track") or ""),
        })
    members.sort(key=lambda item: (item["track"] or "9999", item["rel"]))
    return members


# --------------------------------------------------------------------------
# tag values


def _get(tags: dict, key: str) -> str:
    value = tags.get(key)
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return "" if value is None else str(value)


def read_tags(path: Path) -> dict | None:
    """The editable fields as the file itself holds them (None if unreadable)."""
    values = {field: "" for field in FIELDS}
    try:
        from mutagen import File as MFile
    except ImportError:
        return None
    try:
        handle = MFile(str(path), easy=True)
    except Exception:                                    # noqa: BLE001
        return None
    if handle is None:
        return None
    tags = getattr(handle, "tags", None) or {}
    for field, key in (("title", "title"), ("artist", "artist"),
                       ("albumartist", "albumartist"), ("album", "album"),
                       ("track", "tracknumber"), ("genre", "genre"),
                       ("date", "date"), ("comment", "comment")):
        values[field] = _get(tags, key).strip()
    return values


def current(root: Path, rel: str) -> dict:
    """The values the editor starts from: the file itself, else the index."""
    rel = library_rel(rel)
    actual = read_tags(Path(root) / rel)
    if actual is not None:
        return actual
    info = row(root, rel)
    return {field: str(info.get(field) or "").strip() for field in FIELDS}


# --------------------------------------------------------------------------
# cover art


def folder_cover(folder: Path) -> Path | None:
    for name in COVER_NAMES:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    try:
        for candidate in sorted(folder.iterdir()):
            if candidate.is_file() and _COVER_RE.match(candidate.name):
                return candidate
    except OSError:
        pass
    return None


def folder_files(root: Path, rel: str) -> list[Path]:
    """Every image file next to the track, the way the scanner counts them."""
    folder = Path(root) / album_folder(root, rel)
    try:
        return sorted(item for item in folder.iterdir()
                      if item.is_file()
                      and item.suffix.lower() in IMG_EXTS)
    except OSError:
        return []


def embedded_art(path: Path) -> tuple[str, bytes]:
    """(MIME, bytes) of the first embedded picture, or ("", b"")."""
    ext = path.suffix.lower()
    try:
        if ext in {".mp3", ".mp2"}:
            from mutagen.id3 import ID3
            tag = ID3(str(path))
            for key in tag.keys():
                if key.startswith("APIC"):
                    frame = tag[key]
                    return str(frame.mime or "image/jpeg"), bytes(frame.data)
            return "", b""
        from mutagen import File as MFile
        handle = MFile(str(path))
        if handle is None:
            return "", b""
        if ext in {".m4a", ".mp4", ".aac"}:
            covers = (handle.tags or {}).get("covr") or []
            if covers:
                fmt = int(getattr(covers[0], "imageformat", 13) or 13)
                return ("image/png" if fmt == 14 else "image/jpeg",
                        bytes(covers[0]))
            return "", b""
        if ext == ".flac":
            pictures = getattr(handle, "pictures", None) or []
            if pictures:
                return (str(pictures[0].mime or "image/jpeg"),
                        bytes(pictures[0].data))
            return "", b""
        if ext in {".ogg", ".oga", ".opus"}:
            blocks = (handle.tags or {}).get("metadata_block_picture") or []
            if blocks:
                from mutagen.flac import Picture
                picture = Picture(base64.b64decode(blocks[0]))
                return (str(picture.mime or "image/jpeg"),
                        bytes(picture.data))
            return "", b""
    except Exception:                                     # noqa: BLE001
        return "", b""
    return "", b""


def _image_mime(data: bytes) -> str:
    """The MIME of the picture itself, not the name it happens to carry.

    A cover is served with `nosniff`, so a wrong type would show as a broken
    image; the magic bytes are what a browser really renders.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def art_bytes(root: Path, rel: str) -> tuple[str, bytes]:
    """(MIME, bytes) of a track's cover: inside the file first, then the folder.

    This is what the now-playing strip shows.  It only reads, and a path that
    resolves outside the library is refused rather than read as a "cover".
    """
    root = Path(root).resolve()
    rel = library_rel(str(rel or "").strip())
    try:
        path = (root / rel).resolve()
    except (OSError, ValueError):
        return "", b""
    if not path.is_relative_to(root):
        return "", b""
    data = b""
    if path.is_file():
        _, data = embedded_art(path)
        if data:
            mime = _image_mime(data)
            if mime:
                return mime, data
    data = b""
    folder = (root / album_folder(root, rel)).resolve()
    if not folder.is_relative_to(root):
        return "", b""
    cover = folder_cover(folder)
    if cover is None:
        names = str(row(root, rel).get("folder_image_names") or "").split(";")
        cover = next((folder / name for name in names
                      if name and (folder / name).is_file()), None)
    if cover is not None:
        try:
            data = cover.read_bytes()
        except OSError:
            data = b""
    if not data:
        return "", b""
    mime = _image_mime(data)
    return (mime, data) if mime else ("", b"")


def art_state(root: Path, rel: str) -> dict:
    """What artwork the track has, with a preview when it is small enough."""
    root = Path(root)
    rel = library_rel(rel)
    path = root / rel
    info = row(root, rel)
    folder = root / album_folder(root, rel)
    mime, data = embedded_art(path) if path.is_file() else ("", b"")
    cover = folder_cover(folder)
    if not mime and int(info.get("art_count") or 0) > 0:
        mime = str(info.get("art_mime") or "").strip()
    if not cover and info.get("folder_image_names"):
        names = str(info.get("folder_image_names") or "").split(";")
        cover = next((folder / name for name in names
                      if name and (folder / name).is_file()), None)
    preview, mime, size = "", mime, len(data)
    if data and mime in ART_MIME and size <= PREVIEW_MAX:
        preview = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    elif cover is not None and cover.is_file():
        try:
            raw = cover.read_bytes()
        except OSError:
            raw = b""
        guess = "image/png" if cover.suffix.lower() == ".png" else "image/jpeg"
        if raw and len(raw) <= PREVIEW_MAX:
            preview = f"data:{guess};base64,{base64.b64encode(raw).decode('ascii')}"
            if not mime:
                mime, size = guess, len(raw)
    return {
        "embedded": bool(data),
        "mime": mime,
        "bytes": size,
        "folder": cover.name if cover is not None else "",
        "folder_path": (cover.relative_to(root).as_posix()
                        if cover is not None else ""),
        "preview": preview,
    }


# --------------------------------------------------------------------------
# the journal


def _journal_path(root: Path) -> Path:
    return Path(root) / "library-work" / "journal" / "artwork-edit.jsonl"


def _append_journal(root: Path, entry: dict) -> None:
    path = _journal_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# writing the cover


def _write_embedded(path: Path, mime: str, data: bytes) -> None:
    ext = path.suffix.lower()
    if ext in {".mp3", ".mp2"}:
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError
        try:
            tag = ID3(str(path))
        except ID3NoHeaderError:
            tag = ID3()
        tag.delall("APIC")
        if data:
            tag.add(APIC(encoding=3, mime=mime, type=3, desc="", data=data))
        tag.save(str(path), v2_version=4, v1=0)
        return
    from mutagen import File as MFile
    handle = MFile(str(path))
    if handle is None:
        raise ValueError("this container cannot hold a picture")
    if ext in {".m4a", ".mp4", ".aac"}:
        from mutagen.mp4 import MP4Cover
        if handle.tags is None:
            handle.add_tags()
        if data:
            fmt = (MP4Cover.FORMAT_PNG if mime == "image/png"
                   else MP4Cover.FORMAT_JPEG)
            handle.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        elif "covr" in handle.tags:
            del handle.tags["covr"]
        handle.save()
        return
    if ext == ".flac":
        from mutagen.flac import Picture
        handle.clear_pictures()
        if data:
            picture = Picture()
            picture.type = 3
            picture.mime = mime
            picture.desc = ""
            picture.data = data
            handle.add_picture(picture)
        handle.save()
        return
    if ext in {".ogg", ".oga", ".opus"}:
        from mutagen.flac import Picture
        if handle.tags is None:
            handle.add_tags()
        if data:
            picture = Picture()
            picture.type = 3
            picture.mime = mime
            picture.desc = ""
            picture.data = data
            handle.tags["metadata_block_picture"] = [
                base64.b64encode(picture.write()).decode("ascii")]
        elif "metadata_block_picture" in handle.tags:
            del handle.tags["metadata_block_picture"]
        handle.save()
        return
    raise ValueError("this container cannot hold a picture")


def _write_cover(folder: Path, mime: str, data: bytes) -> Path | None:
    """Write (or remove) the folder image, keeping one file and its name."""
    existing = folder_cover(folder)
    if not data:
        if existing is not None:
            existing.unlink()
        return existing
    target = existing or folder / ("cover" + ART_MIME[mime])
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target


def set_artwork(root: Path, rel: str, data: bytes, mime: str,
                target: str = "embed", remove: bool = False) -> dict:
    """Embed, replace or remove artwork; the previous picture is journalled."""
    root = Path(root)
    rel = library_rel(rel)
    path = root / rel
    if not path.is_file():
        return {"ok": False, "error": f"{rel} is not in the library.",
                "message": f"{rel} is not in the library."}
    if path.suffix.lower() not in TAGGABLE:
        return {"ok": False,
                "error": f"a {path.suffix or 'file'} has no artwork field.",
                "message": "This format cannot carry a picture."}
    target = str(target or "embed").strip().lower()
    if target not in {"embed", "folder"}:
        return {"ok": False, "error": "target must be embed or folder",
                "message": "Choose where the picture should go."}
    mime = str(mime or "").strip().lower()
    if remove:
        data = b""
    else:
        if mime not in ART_MIME:
            # a missing or guessed MIME: decide from the bytes
            if data[:3] == b"\xff\xd8\xff":
                mime = "image/jpeg"
            elif data[:8] == b"\x89PNG\r\n\x1a\n":
                mime = "image/png"
            else:
                return {"ok": False, "error": "only JPEG or PNG is accepted",
                        "message": "Choose a JPEG or PNG image."}
        if not data:
            return {"ok": False, "error": "the image was empty",
                    "message": "The image was empty."}
        if len(data) > ART_MAX:
            return {"ok": False,
                    "error": f"the image is too large ({len(data):,} bytes; "
                             f"at most {ART_MAX:,} are accepted)",
                    "message": "That image is too large."}

    folder = root / album_folder(root, rel)
    entry = {"ts": _now(), "rel_path": rel, "where": target,
             "action": "remove" if remove else "write", "mime": mime,
             "bytes": len(data)}
    if target == "embed":
        before_mime, before = embedded_art(path)
        entry.update({"before_mime": before_mime,
                      "before_b64": base64.b64encode(before).decode("ascii")})
        _append_journal(root, entry)                 # before the write
        try:
            _write_embedded(path, mime, data)
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"the picture could not be written: {exc}",
                    "message": "The picture could not be written."}
        message = ("Artwork removed from the file." if remove
                   else "Artwork embedded in the file.")
    else:
        existing = folder_cover(folder)
        before = b""
        if existing is not None:
            try:
                before = existing.read_bytes()
            except OSError:
                before = b""
        entry.update({"file": (existing.name if existing is not None else ""),
                      "before_b64": base64.b64encode(before).decode("ascii")})
        _append_journal(root, entry)                 # before the write
        try:
            written = _write_cover(folder, mime, data)
        except OSError as exc:
            return {"ok": False, "error": f"the cover could not be written: {exc}",
                    "message": "The cover could not be written."}
        message = ("Folder cover removed." if remove
                   else f"Wrote {written.name if written else 'the cover'} "
                        f"next to the album.")
    state = art_state(root, rel)
    sync_inventory(root, rel, read_tags(path) or {}, art=state)
    return {"ok": True, "where": target, "mime": mime, "bytes": len(data),
            "removed": bool(remove), "message": message, "art": state}


# --------------------------------------------------------------------------
# writing the tags


def _plan_path(root: Path, rel: str) -> Path:
    folder = (Path(root) / "library-work" / "app" / "chat" / "tag-plans")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    stem = Path(rel).stem[:40] or "track"
    return folder / f"{stem}-{stamp}.csv"


#: which inventory issue flag an edited field answers
_ISSUE_FIELDS = {"title": "no_title", "artist": "no_artist",
                 "albumartist": "no_albumartist", "album": "no_album",
                 "track": "no_track", "genre": "no_genre", "date": "no_date"}


def sync_inventory(root: Path, rel: str, values: dict,
                   art: dict | None = None) -> bool:
    """Mirror a tag write into the row the catalogue and search read.

    The inventory scan skips a path it already knows, so a retagged file would
    keep its old title in search until the whole index was rebuilt.  The row is
    rewritten in place (the inventory is generated output, not music) and only
    the edited fields change; the audio's own history stays in the tag journal.

    `art` (the result of :func:`art_state`) also refreshes the artwork columns,
    so the health audit and the editor agree with the file after a picture
    change.
    """
    path = Path(root) / "library-work" / "inventory" / "files.jsonl"
    if not path.is_file():
        return False
    try:
        chunks = path.read_bytes().split(b"\n")
    except OSError:
        return False
    for index, chunk in enumerate(chunks):
        if not chunk.strip():
            continue
        try:
            row = json.loads(chunk)
        except ValueError:
            continue
        if library_rel(str(row.get("rel_path") or "")) != library_rel(rel):
            continue
        for field, value in values.items():
            row[field] = value
        issues = [part for part in str(row.get("issues") or "").split(";")
                  if part]
        for field, code in _ISSUE_FIELDS.items():
            if field not in row:
                continue
            if row.get(field):
                issues = [part for part in issues if part != code]
            elif code not in issues:
                issues.append(code)
        row["issues"] = ";".join(issues)
        row["tag_issue"] = row["issues"]
        if art is not None:
            embedded = bool(art.get("embedded"))
            row["has_embedded_art"] = embedded
            row["art_count"] = 1 if embedded else 0
            row["art_mime"] = str(art.get("mime") or "") if embedded else ""
            row["art_bytes"] = int(art.get("bytes") or 0) if embedded else 0
            names = [item.name for item in folder_files(root, rel)]
            row["folder_image_names"] = ";".join(names[:8])
            row["folder_image_count"] = len(names)
        try:
            target = Path(root) / library_rel(rel)
            stat = target.stat()
            row["size_bytes"] = stat.st_size
            row["mtime"] = datetime.fromtimestamp(
                stat.st_mtime, timezone.utc).isoformat(timespec="seconds")
            digest = hashlib.md5()
            with target.open("rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
            row["md5"] = digest.hexdigest()
        except OSError:
            row["md5"] = ""        # the bytes changed and cannot be re-read
        chunks[index] = json.dumps(row, ensure_ascii=False).encode("utf-8")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(b"\n".join(chunks))
        os.replace(tmp, path)
        _ROW_CACHE.clear()
        return True
    return False


def apply(root: Path, rel: str, values: dict, album_wide: bool = False) -> dict:
    """Write the edited fields through `scripts/tag_fields.py` and re-index."""
    root = Path(root)
    rel = library_rel(rel)
    path = root / rel
    if not path.is_file():
        return {"ok": False, "error": f"{rel} is not in the library.",
                "message": f"{rel} is not in the library."}
    if path.suffix.lower() not in TAGGABLE:
        return {"ok": False,
                "error": f"a {path.suffix or 'file'} has no tag field.",
                "message": "This format cannot be tagged."}
    fields = {}
    for field, value in (values or {}).items():
        if field not in FIELDS:
            continue
        fields[field] = str(value if value is not None else "").strip()
    if album_wide:
        dropped = sorted(set(fields) - set(ALBUM_FIELDS))
        fields = {field: value for field, value in fields.items()
                  if field in ALBUM_FIELDS}
    else:
        dropped = []
    if not fields:
        return {"ok": False, "error": "No field was given.",
                "message": "Change at least one field."}

    files = [rel]
    if album_wide:
        members = [item["rel"] for item in album_members(root, rel)]
        if members:
            files = members
    script = root / "scripts" / "tag_fields.py"
    if not script.is_file():
        return {"ok": False,
                "error": "This library has no `scripts/tag_fields.py`, so the "
                         "tags could not be written.",
                "message": "The tag writer is missing from this library."}

    plan = _plan_path(root, rel)
    with plan.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rel_path", "field", "after", "confidence", "before"])
        for other in files:
            before = read_tags(root / other) or {}
            for field, value in fields.items():
                writer.writerow([other, field, value, "high",
                                 before.get(field, "")])
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--plan", str(plan),
             "--allow-clear"],
            cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"The tag writer could not run: {exc}",
                "message": "The tag writer could not run."}
    lines = [line.strip() for line in (proc.stdout or "").splitlines()
             if line.strip()]
    result = {"plan": plan.relative_to(root).as_posix(),
              "output": lines[-10:], "album_wide": bool(album_wide),
              "fields": fields, "files": files}
    if dropped:
        result["dropped"] = dropped
    if proc.returncode != 0:
        result.update({"ok": False,
                       "error": "The tag write failed: "
                                + (lines[-1] if lines else
                                   f"tag_fields.py exited {proc.returncode}"),
                       "message": "The tag write failed."})
        return result

    # read the values back from the files, so a drift the writer skipped is
    # never mirrored into the index as if it had been written
    indexed = 0
    applied = []
    for other in files:
        actual = read_tags(root / other) or {}
        landed = all(actual.get(field, "") == value
                     for field, value in fields.items())
        if sync_inventory(root, other, actual):
            indexed += 1
        if landed:
            applied.append(other)
    result.update({
        "ok": True,
        "written": applied,
        "indexed": indexed,
        "skipped": [other for other in files if other not in applied],
        "message": (f"Wrote {', '.join(sorted(fields))} to "
                    f"{len(applied)} file{'s' if len(applied) != 1 else ''}"
                    + (" (album-wide)." if album_wide else ".")
                    + (" The index was updated." if indexed else "")),
        "tags": describe(root, rel),
    })
    return result


# --------------------------------------------------------------------------
# one payload for the editor


def describe(root: Path, rel: str) -> dict:
    root = Path(root)
    rel = library_rel(rel)
    path = root / rel
    if not path.is_file():
        return {"error": f"{rel} is not in the library."}
    info = row(root, rel)
    values = current(root, rel)
    ext = path.suffix.lower()
    writable = ext in TAGGABLE and (root / "scripts" / "tag_fields.py").is_file()
    reason = ""
    if ext not in TAGGABLE:
        reason = (f"a {ext or 'file'} file has no tag field — chiptunes, "
                  "tracker modules and a bare WAV are indexed but not tagged")
    elif not (root / "scripts" / "tag_fields.py").is_file():
        reason = "this library has no scripts/tag_fields.py"
    folder = album_folder(root, rel)
    members = album_members(root, rel)
    return {
        "rel": rel,
        "values": values,
        "fields": [{"key": field, "label": LABELS[field],
                    "value": values.get(field, ""),
                    "album_field": field in ALBUM_FIELDS}
                   for field in FIELDS],
        "writable": writable,
        "reason": reason,
        "album": {
            "folder": folder,
            "name": str(info.get("album") or Path(folder).name),
            "tracks": members,
            "count": len(members),
        },
        "art": art_state(root, rel),
        "art_writable": ext in TAGGABLE,
        "art_limit": ART_MAX,
        "taggable": sorted(TAGGABLE),
    }
