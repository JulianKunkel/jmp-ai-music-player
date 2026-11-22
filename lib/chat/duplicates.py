"""Read the duplicate reports — a reviewer that never deletes anything.

The library was deduplicated already: exact copies were proven by audio hash
and probable ones by length, bitrate, Shazam id or shared spectral peaks, and
the copies that survived are the ones that really are a different recording or
a different master.  What is left in `reports/duplicates.csv` is
`action=review`: the same song on a best-of *and* on its own album, where both
copies are wanted.

So this module only reads.  It turns the CSV reports into groups a person can
look at — both albums, both lengths, both bitrates, whether the file is still
on disk — and the chat and the settings panel render that.  There is no delete
path here on purpose: removing a song needs the listener's explicit approval
and one of the journalled library tools, never a button in the app.
"""

from __future__ import annotations

import csv
from pathlib import Path

#: the reports, strongest evidence first
REPORTS = ("probable-duplicates.csv", "duplicates.csv")



def report_paths(root: Path | str) -> list[Path]:
    base = Path(root) / "library-work" / "reports"
    return [base / name for name in REPORTS if (base / name).is_file()]


def library_rel(root: Path | str, value: str) -> str:
    """A report path as a path inside the library (`Music/` is historical)."""
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("Music/"):
        text = text[len("Music/"):]
    return text


def _number(value, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _exists(root: Path, rel: str) -> bool:
    if not rel:
        return False
    try:
        return (root / rel).is_file()
    except OSError:
        return False


def _track(root: Path, row: dict, rel: str, role: str,
           duration, bitrate, size_mb, title, artist, album) -> dict:
    return {
        "rel": rel,
        "exists": _exists(root, rel),
        "role": role,
        "title": str(title or "").strip(),
        "artist": str(artist or "").strip(),
        "album": str(album or "").strip(),
        "duration_s": round(_number(duration), 1),
        "bitrate": int(round(_number(bitrate) / 1000.0)),
        "size_mb": round(_number(size_mb), 2),
    }


def _group_rows(path: Path, root: Path) -> list[dict]:
    """One entry per `duplicates.csv` group (action/reason/group/role rows)."""
    groups: dict[str, dict] = {}
    try:
        handle = path.open(newline="", encoding="utf-8-sig",
                           errors="replace")
    except OSError:
        return []
    with handle:
        for row in csv.DictReader(handle):
            key = str(row.get("group") or "").strip()
            rel = library_rel(root, row.get("rel_path") or "")
            if not key or not rel:
                continue
            entry = groups.setdefault(key, {
                "key": key,
                "reason": str(row.get("reason") or "probable").strip(),
                "action": str(row.get("action") or "review").strip(),
                "tracks": [],
            })
            if any(track["rel"] == rel for track in entry["tracks"]):
                continue
            entry["tracks"].append(_track(
                root, row, rel, str(row.get("role") or "review").strip(),
                row.get("duration_s"), row.get("bitrate"),
                _number(row.get("size_bytes")) / 1e6,
                row.get("title"), row.get("artist"), row.get("album")))
    return list(groups.values())


def _pair_rows(path: Path, root: Path) -> list[dict]:
    """One entry per `probable-duplicates.csv` pair (two columns of files)."""
    groups: list[dict] = []
    try:
        handle = path.open(newline="", encoding="utf-8-sig",
                           errors="replace")
    except OSError:
        return []
    with handle:
        for row in csv.DictReader(handle):
            probable = library_rel(root, row.get("probable_file") or "")
            kept = library_rel(root, row.get("kept_file") or "")
            if not probable or not kept:
                continue
            artist = str(row.get("artist") or "").strip()
            title = str(row.get("title") or "").strip()
            key = f"{title.casefold()}|{artist.casefold()}"
            tracks = [
                _track(root, row, probable, "review",
                       row.get("seconds_probable"), row.get("bitrate_probable"),
                       row.get("size_probable_mb"), title, artist,
                       row.get("album_probable")),
                _track(root, row, kept, "keep",
                       row.get("seconds_kept"), row.get("bitrate_kept"),
                       row.get("size_kept_mb"), title, artist,
                       row.get("album_kept")),
            ]
            groups.append({"key": key, "reason": "probable",
                           "action": "review", "tracks": tracks})
    return groups


def groups(root: Path | str) -> list[dict]:
    """Every duplicate group the reports still list, read-only.

    A group carries its copies (each with the album, the length, the bitrate
    and whether the file is still there), the copy the report marked `keep`,
    and the pair's worst length gap — the one number that tells a different
    master from a different take.
    """
    root = Path(root)
    found: dict[str, dict] = {}
    for path in report_paths(root):
        entries = (_pair_rows(path, root) if "probable-duplicates" in path.name
                   else _group_rows(path, root))
        for entry in entries:
            current = found.get(entry["key"])
            if current is None:
                found[entry["key"]] = entry
                continue
            # the same pair in two reports: keep one copy of each track
            seen = {track["rel"] for track in current["tracks"]}
            current["tracks"].extend(
                track for track in entry["tracks"] if track["rel"] not in seen)
            if entry["action"] == "review":
                current["action"] = "review"
    out: list[dict] = []
    for entry in found.values():
        tracks = entry["tracks"]
        tracks.sort(key=lambda track: 0 if track["role"] == "keep" else 1)
        keep = next((track for track in tracks if track["role"] == "keep"),
                    tracks[0] if tracks else None)
        present = [track for track in tracks if track["exists"]]
        lengths = [track["duration_s"] for track in tracks if track["duration_s"]]
        gap = round(max(lengths) - min(lengths), 1) if len(lengths) > 1 else 0.0
        size_mb = round(sum(track["size_mb"] for track in tracks), 2)
        extra_mb = round(sum(track["size_mb"] for track in present
                             if keep is None or track["rel"] != keep["rel"]), 2)
        out.append({
            **entry,
            "keep": keep["rel"] if keep else "",
            "present": len(present),
            "missing": len(tracks) - len(present),
            "size_mb": size_mb,
            "extra_mb": extra_mb,
            "length_gap": gap,
        })
    out.sort(key=lambda group: (-group["present"], group["key"]))
    return out


def summary(root: Path | str) -> dict:
    """Counts for the panel and the chat, plus the groups themselves."""
    root = Path(root)
    every = groups(root)
    present = sum(group["present"] for group in every)
    missing = sum(group["missing"] for group in every)
    reasons: dict[str, int] = {}
    for group in every:
        reasons[group["reason"] or "probable"] = reasons.get(
            group["reason"] or "probable", 0) + 1
    on_purpose = sum(1 for group in every
                     if group["present"] >= 2 and group["length_gap"] <= 2.0)
    return {
        "groups": len(every),
        "files": sum(len(group["tracks"]) for group in every),
        "present": present,
        "missing": missing,
        "reasons": reasons,
        # both copies present and within the two-second rule: the same
        # recording on two albums, which is exactly what was kept on purpose
        "review": on_purpose,
        "size_mb": round(sum(group["size_mb"] for group in every), 1),
        "items": every,
    }


def _clock(seconds: float) -> str:
    value = max(0, int(round(float(seconds or 0))))
    return f"{value // 60}:{value % 60:02d}"


def _copy_line(track: dict) -> str:
    parts: list[str] = []
    if track["duration_s"]:
        parts.append(_clock(track["duration_s"]))
    if track["bitrate"]:
        parts.append(f"{track['bitrate']:,} kbit/s")
    if track["size_mb"]:
        parts.append(f"{track['size_mb']:.1f} MB")
    if not track["exists"]:
        parts.append("not on disk")
    return " · ".join(parts)


def markdown(data: dict, limit: int = 12) -> str:
    """The reviewer as a chat answer; it never offers a delete."""
    groups = data.get("items") or []
    if not data.get("groups"):
        return ("**No duplicate groups are open.**  The reports list nothing "
                "to review — every copy left in the tree is a different "
                "recording or a different master.")
    lines = [
        f"**Duplicate review** · {data['groups']:,} groups · "
        f"{data['files']:,} files · {data['present']:,} still on disk",
        "",
        "Every group here is `action=review`: the same recording on a best-of "
        "*and* on its own album, and both copies are wanted.  Nothing is "
        "deleted from this app — removing a song needs your explicit approval "
        "and a journalled library tool.",
    ]
    for number, group in enumerate(groups[:limit], 1):
        first = group["tracks"][0] if group["tracks"] else {}
        who = first.get("artist") or "unknown artist"
        title = first.get("title") or group["key"]
        lines.append("")
        lines.append(f"{number}. **{who} — {title}**")
        for track in group["tracks"]:
            mark = " · **kept**" if track["rel"] == group["keep"] else ""
            where = track["album"] or Path(track["rel"]).parent.name
            lines.append(f"   - *{where}* · {_copy_line(track)}{mark}")
            lines.append(f"     `{track['rel']}`")
        if group["length_gap"]:
            lines.append(f"   - {group['length_gap']:.1f} s apart"
                         + (" — a different master"
                            if group["length_gap"] > 2 else
                            " — within the same-take rule"))
    if len(groups) > limit:
        lines.append("")
        lines.append(f"… and {len(groups) - limit:,} more groups.  Say "
                     "“show duplicate 13” for one of them.")
    else:
        lines.append("")
        lines.append("Say “show duplicate 1” to see one group's files, then "
                     "“play number 2” to hear a copy.")
    return "\n".join(lines)


def describe(group: dict) -> str:
    """One group, in full, for “show duplicate 3”."""
    first = group["tracks"][0] if group["tracks"] else {}
    lines = [
        f"**{first.get('artist') or 'unknown artist'} — "
        f"{first.get('title') or group['key']}**",
        "",
        f"{group['present']} of {len(group['tracks'])} copies are on disk · "
        f"the report says `{group['action']}` ({group['reason']})"
        + (f" · {group['length_gap']:.1f} s apart" if group["length_gap"] else ""),
    ]
    for number, track in enumerate(group["tracks"], 1):
        mark = " · **kept**" if track["rel"] == group["keep"] else ""
        lines.append("")
        lines.append(f"{number}. *{track['album'] or 'no album'}* · "
                     f"{_copy_line(track)}{mark}")
        lines.append(f"   `{track['rel']}`")
    lines.append("")
    lines.append("Nothing was changed: these files are read, not moved or "
                 "deleted.  Say “play number 2” to hear a copy.")
    return "\n".join(lines)
