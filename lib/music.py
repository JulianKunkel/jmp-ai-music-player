#!/usr/bin/env python3
"""One command for everything in the music library.

This is the user-facing front end: it plays the library, searches it, edits
playlists, imports downloads and new files, and rebuilds the mood lists.  It
works on the host and inside the container — both only need the repository
mounted and an mpv binary.

    music-cli play Artists/Die_Ärzte/.../01_Song.mp3
    music play Bands                     # a playlist by name
    music play "die ärzte"               # or a search hit
    music play --shuffle --append All
    music pause | resume | next | prev | stop
    music seek +30 | seek 50%
    music vol 65
    music status
    music search "chill piano" --limit 10
    music pl list | show Mix | add Mix --track PATH | remove Mix --index 2
    music import URL                         # optional download module
    music add ~/Downloads/song.flac      # tag + file a local file
    music mood | mood list | mood build | mood play chill
    music index                          # rescan + rebuild playlists
    music data                           # backup/export/import the profile
    music verify
    music shell                          # a shell in the library

Playback runs in one headless mpv daemon listening on `MPV_SOCKET`
(`/tmp/mpv.sock`); `music play` starts it on demand.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from libpaths import app_module, library_root, library_tool  # noqa: E402
from mpvctl import Mpv, MpvError, clock  # noqa: E402

ROOT = library_root()
PLAYLISTS = ROOT / "Playlists"
MOODS = PLAYLISTS / "Moods"
CUSTOM = PLAYLISTS / "Custom"
INVENTORY = ROOT / "library-work" / "inventory" / "files.jsonl"
REPORTS = ROOT / "library-work" / "reports"
PY = sys.executable

AUDIO_EXT = {".mp3", ".mp2", ".m4a", ".mp4", ".opus", ".ogg", ".flac",
             ".wav", ".wma", ".aac", ".spc", ".sid", ".mod", ".xm", ".it",
             ".s3m", ".ym", ".play"}


# ---------------------------------------------------------------------------
# helpers


def script(name: str) -> str:
    """The app's own tool next to this file, else the library's copy."""
    local = app_module(name)
    return str(local if local.is_file() else library_tool(name))


def run(name: str, argv: list[str]) -> int:
    """Run one of the journalled repo tools with the same interpreter."""
    result = subprocess.run([PY, script(name), *argv], cwd=str(ROOT))
    return result.returncode


def player(args) -> Mpv:
    return Mpv(getattr(args, "socket", None)
               or os.environ.get("MPV_SOCKET") or "/tmp/mpv.sock")


def playlists() -> list[Path]:
    found: list[Path] = []
    for base in (CUSTOM, MOODS, PLAYLISTS):
        found += [p for p in sorted(base.glob("*.m3u8")) if p.is_file()]
    return found


def find_playlist(text: str) -> Path | None:
    """`Bands`, `bands`, `Moods:Mood_Chill`, or a path to an .m3u8."""
    handle = text[:-5] if text.lower().endswith(".m3u8") else text
    handle = handle.split(":", 1)[-1]
    direct = Path(handle)
    if direct.suffix == ".m3u8" and direct.is_file():
        return direct.resolve()
    wanted = handle.casefold()
    for path in playlists():
        if path.stem.casefold() == wanted:
            return path
    for path in playlists():
        if wanted in path.stem.casefold():
            return path
    return None


def inventory_rows() -> list[dict]:
    if not INVENTORY.exists():
        return []
    rows = []
    for line in INVENTORY.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line.decode("utf-8")))
        except ValueError:
            continue
    return rows


def library_rel(rel_path: str) -> str:
    """The inventory still stores a `Music/` prefix for pre-move rows."""
    return rel_path[len("Music/"):] if rel_path.startswith("Music/") else rel_path


def score_row(row: dict, tokens: list[str]) -> float | None:
    artist = (row.get("artist") or row.get("albumartist") or "").casefold()
    title = (row.get("title") or "").casefold()
    album = (row.get("album") or "").casefold()
    genre = (row.get("genre") or "").casefold()
    grouping = (row.get("grouping") or "").casefold()
    path = library_rel(row.get("rel_path") or "").casefold()
    score = 0.0
    for token in tokens:
        hit = False
        if token in title:
            score += 2.5
            hit = True
        if token in artist:
            score += 2.5
            hit = True
        if token in album:
            score += 1.5
            hit = True
        if token in genre or token in grouping:
            score += 1.0
            hit = True
        if token in path:
            score += 0.5
            hit = True
        if not hit:
            return None
    return score


def search_rows(query: str, limit: int) -> list[dict]:
    tokens = [t for t in re.split(r"\s+", query.casefold()) if t]
    if not tokens:
        return []
    scored = []
    for row in inventory_rows():
        score = score_row(row, tokens)
        if score is not None:
            scored.append((score, library_rel(row.get("rel_path") or ""), row))
    scored.sort(key=lambda item: (-item[0], item[1].casefold()))
    return [dict(row, rel_path=rel) for _, rel, row in scored[:limit]]


def label(row: dict) -> str:
    artist = row.get("artist") or row.get("albumartist") or ""
    title = row.get("title") or Path(row.get("rel_path") or "?").stem
    album = row.get("album") or ""
    length = clock(row.get("duration_s")) if row.get("duration_s") else "--:--"
    text = f"{artist} - {title}" if artist else title
    if album:
        text += f"  —  {album}"
    return f"{text}  ({length})"


def resolve_target(text: str, first: bool) -> tuple[str, bool]:
    """(what mpv should load, whether it was resolved by search)."""
    if re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE):
        return text, False
    candidate = Path(text).expanduser()
    for base in (Path.cwd(), ROOT):
        probe = candidate if candidate.is_absolute() else base / candidate
        if probe.exists():
            return str(probe.resolve()), False
    playlist = find_playlist(text)
    if playlist is not None:
        return str(playlist), False
    hits = search_rows(text, 10)
    if len(hits) == 1 or (hits and first):
        return str(ROOT / hits[0]["rel_path"]), True
    if hits:
        print(f"{len(hits)} match(es) for {text!r} — play one by path, or "
              "repeat with --first:")
        for row in hits:
            print(f"  {label(row)}")
            print(f"      {row['rel_path']}")
        raise SystemExit(2)
    raise SystemExit(f"nothing matches {text!r} (try `music search`)")


def banner() -> None:
    rows = inventory_rows()
    audio = [r for r in rows if (r.get("ext") or "").lower() in AUDIO_EXT]
    total_h = sum(float(r.get("duration_s") or 0) for r in audio) / 3600.0
    print(f"library   : {ROOT}")
    print(f"audio     : {len(audio):,} files, {total_h:,.1f} h")
    print(f"playlists : {len([p for p in PLAYLISTS.glob('*.m3u8')])} shelf"
          f" + {len([p for p in MOODS.glob('*.m3u8')])} mood/focus"
          f" + {len([p for p in CUSTOM.glob('*.m3u8')])} custom")
    print()
    print("  music play <path|playlist|search>   music search <words>")
    print("  music pause|resume|next|prev|stop   music status")
    print("  music pl list                       music mood list")
    print("  music import <url>                      music add <file>")
    print("  music index                         music verify")
    print("  written with AI assistance · GPL-3.0-or-later · see NOTICE")
    print()


# ---------------------------------------------------------------------------
# commands


def cmd_play(args) -> int:
    target, _ = resolve_target(args.target, args.first)
    pc = player(args)
    pc.start(audio=args.audio, volume=args.volume)
    pc.load(target, append=args.append)
    if args.shuffle:
        pc.cmd("playlist-shuffle")
    print(f"{'queued' if args.append else 'playing'}: {target}")
    return 0


def cmd_toggle(args) -> int:
    pc = player(args)
    if not pc.alive():
        raise SystemExit("nothing is playing")
    print("paused" if pc.toggle() else "playing")
    return 0


def cmd_simple(args) -> int:
    pc = player(args)
    if not pc.alive():
        raise SystemExit("mpv is not running (start it with `music play …`)")
    action = args.command
    message = {"resume": "playing", "pause": "paused", "stop": "stopped",
               "next": "next", "prev": "previous"}[action]
    if action == "resume":
        pc.set("pause", False)
    elif action == "pause":
        pc.set("pause", True)
    elif action == "stop":
        pc.cmd("stop")
    elif action == "next":
        pc.cmd("playlist-next")
    elif action == "prev":
        pc.cmd("playlist-prev")
    print(message)
    return 0


def cmd_seek(args) -> int:
    pc = player(args)
    if not pc.alive():
        raise SystemExit("nothing is playing")
    amount = args.amount.strip()
    if amount.endswith("%"):
        pc.cmd("seek", float(amount[:-1]), "absolute-percent")
    else:
        pc.cmd("seek", float(amount), "relative")
    print(f"seek {amount}")
    return 0


def cmd_vol(args) -> int:
    pc = player(args)
    if not pc.alive():
        raise SystemExit("nothing is playing")
    level = max(0, min(100, args.level))
    pc.set("volume", level)
    print(f"volume {level}%")
    return 0


def cmd_status(args) -> int:
    pc = player(args)
    if not pc.alive():
        print("idle — mpv is not running; `music play …` starts it")
        return 0
    s = pc.status()
    if s.get("idle"):
        print("idle — the playlist finished")
        return 0
    print(f"{'paused' if s['pause'] else 'playing'}: "
          f"{s['title'] or s['file']}")
    print(f"  {clock(s['time'])} / {clock(s['duration'])}"
          f"   volume {int(round(s['volume'] or 0))}%"
          + ("  (muted)" if s["mute"] else ""))
    if (s["playlist_count"] or 0) > 1:
        print(f"  entry {int(s['playlist_pos'] or 0) + 1} of "
              f"{s['playlist_count']}" + ("  [shuffle]" if s["shuffle"] else ""))
    return 0


def cmd_search(args) -> int:
    hits = search_rows(args.query, args.limit)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return 0
    if not hits:
        print(f"no match for {args.query!r}")
        return 1
    for number, row in enumerate(hits, 1):
        print(f"{number:3}. {label(row)}")
        print(f"     {row['rel_path']}")
    return 0


def cmd_pl(args) -> int:
    return run("playlist_edit.py", args.rest)


def cmd_yt(args) -> int:
    """Hand the import to the optional download module, if this build has one."""
    from chat import downloads
    return downloads.run_importer(ROOT, list(args.rest))


def cmd_add(args) -> int:
    return run("add_file.py", args.rest)


def cmd_data(args) -> int:
    """fingerprint / export / import / backup / status of the app data."""
    return run("library_data.py", args.rest)


def cmd_mood(args) -> int:
    action = args.action or "plan"
    if action == "list":
        for path in sorted(MOODS.glob("*.m3u8")):
            count = sum(1 for line in path.read_text(encoding="utf-8-sig")
                        .splitlines() if line and not line.startswith("#"))
            print(f"  {path.stem:22} {count:5,}")
        return 0
    if action == "play":
        if not args.name:
            raise SystemExit("mood play needs a name (see `music mood list`)")
        wanted = args.name.casefold()
        options = [p for p in MOODS.glob("*.m3u8")
                   if p.stem.casefold() == f"mood_{wanted}"
                   or p.stem.casefold() == f"mood_focus_{wanted}"]
        if not options:
            options = [p for p in MOODS.glob("*.m3u8")
                       if wanted in p.stem.casefold()]
        if len(options) != 1:
            raise SystemExit(f"{args.name!r} matches "
                             f"{len(options)} mood lists")
        pc = player(args)
        pc.start(audio=args.audio)
        pc.load(str(options[0]))
        print(f"playing {options[0].relative_to(ROOT)}")
        return 0
    if action == "build":
        code = run("analyze_moods.py", [])
        if code:
            return code
        return run("make_mood_playlists.py", ["--apply"])
    return run("make_mood_playlists.py", [])


def cmd_index(args) -> int:
    code = run("phase0_inventory.py", ["scan", "--work",
                                       str(ROOT / "library-work")])
    if code:
        return code
    code = run("phase0_inventory.py", ["report", "--work",
                                       str(ROOT / "library-work")])
    if code:
        return code
    return run("make_playlists.py", ["--from-tree"])


def cmd_verify(args) -> int:
    first = run("verify_playlists.py", [])
    second = run("verify_final.py", [])
    if not args.tags:
        return max(first, second)
    return max(first, second, run("verify_tags.py", []))


def cmd_chat(args) -> int:
    """The conversational front end: window, Textual TUI, or plain lines."""
    root = Path(args.root).expanduser() if getattr(args, "root", None) else None
    use_llm = not getattr(args, "no_llm", False)
    dry_run = bool(getattr(args, "dry_run", False))
    message = getattr(args, "message", None)

    if getattr(args, "web", False):
        from chat.web import run_web
        return run_web(root=root, host=getattr(args, "host", "127.0.0.1"),
                       port=int(getattr(args, "port", 0) or 0),
                       use_llm=use_llm, dry_run=dry_run,
                       open_browser=not getattr(args, "no_open", False),
                       idle_exit=int(getattr(args, "idle_exit", 0) or 0))

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not message and not getattr(args, "plain", False) and interactive:
        try:
            from chat.app import ChatApp
        except ImportError:
            print("textual is not installed — falling back to the plain chat "
                  "(install it with pip install textual)")
        else:
            ChatApp(root=root, use_llm=use_llm, dry_run=dry_run).run()
            return 0
    from chat.plain import run as plain_run
    return plain_run(root=root, use_llm=use_llm, message=message,
                     dry_run=dry_run)


def cmd_shell(args) -> int:
    banner()
    sys.stdout.flush()          # exec replaces us; unflushed output would be lost
    shell = os.environ.get("SHELL") or "/bin/bash"
    if not shutil.which(shell):
        shell = "/bin/sh"
    os.chdir(ROOT)
    os.execvp(shell, [shell])
    return 0


# ---------------------------------------------------------------------------


PASSTHROUGH = {"pl": cmd_pl, "import": cmd_yt, "yt": cmd_yt, "add": cmd_add,
               "data": cmd_data}


def split_passthrough(argv: list[str]) -> tuple[list[str], str | None, list[str]]:
    """Split `pl`/`import`/`add`/`data` off so their own flags reach the real tool.

    `argparse` would reject `music import --dry-run URL` (an unknown option of the
    subparser) and would eat `--track` the same way, so everything after the
    command goes through verbatim.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--socket":
            index += 2
            continue
        if token.startswith("--socket="):
            index += 1
            continue
        break
    if index < len(argv) and argv[index] in PASSTHROUGH:
        return argv[:index], argv[index], argv[index + 1:]
    return argv, None, []


def socket_of(argv: list[str]) -> str | None:
    for index, token in enumerate(argv):
        if token == "--socket" and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith("--socket="):
            return token.split("=", 1)[1]
    return None


def main() -> int:
    head, command, rest = split_passthrough(sys.argv[1:])
    if command is not None:
        args = argparse.Namespace(command=command, rest=rest,
                                  socket=socket_of(head))
        return PASSTHROUGH[command](args)

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""three subcommands hand their arguments straight to another tool,
so every option of that tool works unchanged:
  music pl  ...    -> application/lib/playlist_edit.py --help
  music import  ...    -> the installed download module's importer --help
  music add ...    -> application/lib/add_file.py --help
  music data ...   -> scripts/library_data.py --help""")
    ap.add_argument("--socket", default=None, help="mpv IPC socket")
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("play", help="play a file, playlist, URL or search")
    p.add_argument("target")
    p.add_argument("--append", action="store_true",
                   help="queue instead of replacing")
    p.add_argument("--shuffle", action="store_true")
    p.add_argument("--first", action="store_true",
                   help="take the best search hit without asking")
    p.add_argument("--audio", default=os.environ.get("MPV_AO"),
                   help="mpv audio driver (pulse, alsa, null)")
    p.add_argument("--volume", type=int, default=None)

    sub.add_parser("toggle", help="pause or resume")
    sub.add_parser("resume", help="resume")
    sub.add_parser("pause", help="pause")
    sub.add_parser("stop", help="stop playback")
    sub.add_parser("next", help="next entry")
    sub.add_parser("prev", help="previous entry")
    sub.add_parser("status", help="what is playing")

    p = sub.add_parser("seek", help="seek by seconds or to a percentage")
    p.add_argument("amount")

    p = sub.add_parser("vol", help="set volume 0..100")
    p.add_argument("level", type=int)

    p = sub.add_parser("search", help="search artist/title/album/shelf")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("mood", help="plan/build/list/play mood playlists")
    p.add_argument("action", nargs="?", choices=["plan", "build", "list", "play"])
    p.add_argument("name", nargs="?")
    p.add_argument("--audio", default=os.environ.get("MPV_AO"))

    sub.add_parser("index", help="rescan the inventory, rebuild playlists")
    p = sub.add_parser("verify", help="wiring and library checks")
    p.add_argument("--tags", action="store_true",
                   help="also re-check every tag against the inventory")
    sub.add_parser("shell", help="a shell in the library")

    p = sub.add_parser("chat", help="talk to the library (skills, playlists)")
    p.add_argument("message", nargs="?",
                   help="handle one message, print the answer, exit")
    p.add_argument("--web", action="store_true",
                   help="open the chat as its own application window")
    p.add_argument("--host", default="127.0.0.1",
                   help="address the window server listens on (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=0,
                   help="fixed port (default: any free one)")
    p.add_argument("--no-open", action="store_true",
                   help="with --web: print the URL instead of opening a window")
    p.add_argument("--idle-exit", type=int, default=0, metavar="SECONDS",
                   help="with --web: stop after this long without a request")
    p.add_argument("--plain", action="store_true",
                   help="line-based chat instead of the full-screen app")
    p.add_argument("--no-llm", action="store_true",
                   help="use only the offline planner")
    p.add_argument("--dry-run", action="store_true",
                   help="plan playlists but never write one")
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)

    args = ap.parse_args()
    if not args.command:
        ap.print_help()
        return 1
    handler = {
        "play": cmd_play, "toggle": cmd_toggle,
        "resume": cmd_simple, "pause": cmd_simple, "stop": cmd_simple,
        "next": cmd_simple, "prev": cmd_simple,
        "seek": cmd_seek, "vol": cmd_vol, "status": cmd_status,
        "search": cmd_search, "mood": cmd_mood, "index": cmd_index,
        "verify": cmd_verify,
        "shell": cmd_shell, "chat": cmd_chat,
    }[args.command]
    try:
        return handler(args)
    except MpvError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    sys.exit(main())
