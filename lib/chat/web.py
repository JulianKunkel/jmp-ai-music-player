"""The window UI: a local web app over the same ``Engine``.

`music chat --web` starts a tiny HTTP server on ``127.0.0.1`` and shows the page
in its own window: a native GTK/WebKit window when the host has PyGObject and
WebKitGTK (``window.py``), otherwise a chromeless browser window
(``chromium --app=…``).  That is what makes the chat feel like a desktop
application rather than a terminal.  The page talks to the engine over a
token-protected JSON API, and only localhost can reach it.

    music chat --web                 # own window
    music chat --web --no-open       # print the URL instead
    music chat --web --port 8765     # a fixed port
    music chat --web --idle-exit 1800

The terminal UI (`music chat`) is unchanged; both drive the same engine and
therefore the same journals, playlists and learning profile.

Nothing here writes a tag or a file itself.  Chat turns go through
``Engine.handle``, transport buttons through ``Engine.skill_transport`` and an
optional download module through the ``downloads`` facade, so every side
effect keeps its own journal entry.
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import downloads as youtube
from .engine import Engine
from .skills import ORDER, examples, help_markdown, load as load_skills
from . import index_job
from . import library_job
from . import lyrics_job
from . import fade
from . import loudness
from . import lrc
from . import psycho_job
from . import radio
from . import endless
from . import settings as setting_registry
from . import tagedit
from . import version
from . import voice
from .viz import STYLES as VIZ_STYLES, spectrum as viz_spectrum, waveform

STATIC_DIR = Path(__file__).resolve().parent / "webui"
APP_DIR = Path(__file__).resolve().parent.parent
TOKEN_HEADER = "X-Chat-Token"
MAX_BODY = 64 * 1024

#: browsers that can open a page as a chromeless application window
APP_BROWSERS = ("chromium", "chromium-browser", "google-chrome",
                "google-chrome-stable", "brave-browser", "microsoft-edge",
                "microsoft-edge-stable", "vivaldi-stable")

#: A request slower than this is worth a line in the log: the page polls every
#: two seconds, so a slow answer is what makes a click feel broken.
SLOW_SECONDS = 2.0
#: MUSIC_WEB_DEBUG=1 logs every request, not only the failures and the slow ones
DEBUG = os.environ.get("MUSIC_WEB_DEBUG", "").strip().lower() not in {
    "", "0", "no", "false"}
LOG_MAX_BYTES = 512 * 1024


def _clean_path(raw: str) -> str:
    """The path of a request without its query string.

    The token travels in ``?t=…`` for the page itself, so a log line that kept
    the query would copy the token into a world-readable file.
    """
    try:
        path = urlparse(raw or "").path or "/"
    except ValueError:
        path = "/"
    return path[:200]


def _append_log(path: Path, line: str) -> None:
    """Append one line, rotating the file once it passes :data:`LOG_MAX_BYTES`."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
    except OSError:
        pass


class Diagnostics:
    """Counters and a small error log for the web server.

    A handler that raises before it writes a response closes the connection
    without an answer, and WebKit shows that to the user as **"Load failed"** —
    a message that names neither the endpoint nor the cause.  Everything that
    goes wrong is therefore recorded here and written to

        library-work/app/chat/server-errors.log
        library-work/app/chat/server-requests.log

    so the next "Load failed" can be explained from the files instead of being
    guessed at.  Both logs rotate at :data:`LOG_MAX_BYTES`.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else None
        self.started = time.time()
        self.requests = 0
        self.errors = 0
        self.slow = 0
        self.last_error: dict | None = None
        self.codes: Counter[str] = Counter()
        self.recent: deque[dict] = deque(maxlen=40)
        self.lock = threading.Lock()

    # -- paths ------------------------------------------------------------

    def directory(self) -> Path | None:
        if self.root is None:
            return None
        return self.root / "library-work" / "app" / "chat"

    @property
    def error_log(self) -> Path | None:
        folder = self.directory()
        return folder / "server-errors.log" if folder else None

    @property
    def request_log(self) -> Path | None:
        folder = self.directory()
        return folder / "server-requests.log" if folder else None

    def set_root(self, root: Path) -> None:
        self.root = Path(root)

    # -- recording --------------------------------------------------------

    def record(self, method: str, path: str, code: int, seconds: float,
               error: str = "") -> None:
        millis = round(seconds * 1000)
        entry = {
            "time": round(time.time(), 3),
            "method": method, "path": path, "code": code,
            "ms": millis, "error": error,
        }
        with self.lock:
            self.requests += 1
            self.codes[str(code)] += 1
            if error:
                self.errors += 1
                self.recent.append(entry)
                self.last_error = entry
            elif millis >= SLOW_SECONDS * 1000:
                self.slow += 1
                self.recent.append(entry)
            if not error and not DEBUG and millis < SLOW_SECONDS * 1000:
                return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["time"]))
        if error:
            _append_log(self.error_log,
                        f"{stamp} {method} {path} -> {code or 500} "
                        f"({millis} ms)\n{error}")
        elif self.request_log is not None:
            _append_log(self.request_log,
                        f"{stamp} {method} {path} -> {code} ({millis} ms)")

    def note(self, message: str, path: str = "") -> None:
        """Record a failure that was answered *without* an exception.

        A route may answer a proper JSON error (a cover that cannot be read) and
        still be a real fault; this puts it in the error log and in the recent
        list without counting a second request.
        """
        text = str(message or "").strip()
        if not text:
            return
        entry = {
            "time": round(time.time(), 3),
            "method": "GET", "path": path or "", "code": 500,
            "ms": 0, "error": text,
        }
        with self.lock:
            self.errors += 1
            self.recent.append(entry)
            self.last_error = entry
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["time"]))
        _append_log(self.error_log, f"{stamp} artwork {path} -> {text}")

    def snapshot(self, compact: bool = False) -> dict:
        with self.lock:
            data = {
                "pid": os.getpid(),
                # the wall-clock start, so "up 8 s" can never be mistaken for
                # a counter that got stuck
                "started_at": round(self.started, 3),
                "uptime_s": round(time.time() - self.started, 2),
                "requests": self.requests,
                "errors": self.errors,
                "slow": self.slow,
                "codes": dict(self.codes),
                "last_error": self.last_error,
                "recent": list(self.recent)[-10:] if not compact else [],
                "log": str(self.error_log) if self.error_log else "",
                "request_log": str(self.request_log) if self.request_log else "",
                "debug": DEBUG,
                "version": version.info(),
            }
            if not compact:
                data["threads"] = threading.active_count()
                data["python"] = sys.version.split()[0]
        return data

    def clear(self) -> dict:
        """Empty the two log files and the error history they fed.

        The previous content is moved aside to `.1` — the same rotation rule
        :func:`_append_log` uses — so cleaning the panel can never destroy the
        evidence of a crash.  Only the error side of the counters is reset;
        `requests` and the status codes stay, because they count traffic and
        not failures.
        """
        cleared: list[str] = []
        freed = 0
        with self.lock:
            for path in (self.error_log, self.request_log):
                if path is None:
                    continue
                try:
                    size = path.stat().st_size if path.exists() else 0
                except OSError:
                    size = 0
                if size <= 0:
                    continue
                try:
                    os.replace(path, path.with_name(path.name + ".1"))
                    path.write_bytes(b"")
                except OSError:
                    continue
                cleared.append(str(path))
                freed += size
            self.errors = 0
            self.slow = 0
            self.last_error = None
            self.recent.clear()
        return {"ok": True, "cleared": cleared, "bytes": freed,
                "log": str(self.error_log) if self.error_log else ""}


# ---------------------------------------------------------------------------
# markdown -> html
#
# The replies are Markdown; the page must not interpret raw HTML, so every line
# is escaped first and only the small subset the chat actually emits is turned
# back into markup (headings, lists, code, bold/italic, links).


_AUDIO_EXT = re.compile(
    r"\.(?:mp3|mp2|m4a|mp4|flac|ogg|oga|opus|wav|aac|wma|webm|mka|aif|aiff|"
    r"spc|sid|mod|xm|it|s3m|ym|play)$", re.IGNORECASE)


_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _urls(text: str) -> list[str]:
    """Every http(s) link in a pasted block, in order, without duplicates.

    Trailing punctuation is stripped, because a link pasted from a chat window
    often ends in `)`, `.` or `,`; a line that is not a link is ignored.
    """
    found: list[str] = []
    for raw in _URL_RE.findall(str(text or "")):
        link = raw.rstrip(").,;:!?\"'")
        if link not in found:
            found.append(link)
    return found


def _play_target(text: str) -> bool:
    """Is that code span a library file the page could hand to the player?

    `_number_tracks()` prints the relative path under every hit as a code
    span, so turning that span into a play target makes the listed track
    clickable.  Anything absolute or on the web is not a library file.
    """
    value = text.strip()
    if not value or value.startswith(("/", "~", "http://", "https://", "www.")):
        return False
    return "/" in value and bool(_AUDIO_EXT.search(value))


def _inline(text: str) -> str:
    text = html.escape(text, quote=True)
    parts = re.split(r"`([^`]+)`", text)
    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2:
            if _play_target(part):
                out.append(f'<code class="play-path" data-rel="{part}" '
                           f'title="Click to play this track">{part}</code>')
            else:
                out.append(f"<code>{part}</code>")
            continue
        part = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", part)
        part = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])",
                      r"<em>\1</em>", part)
        part = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                      r'<a href="\2" target="_blank" rel="noreferrer">\1</a>',
                      part)
        out.append(part)
    return "".join(out)


def md_to_html(text: str) -> str:
    """A small, safe Markdown renderer for the answers."""
    lines = (text or "").splitlines()
    out: list[str] = []
    index = 0
    list_tag: str | None = None

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    while index < len(lines):
        raw = lines[index]
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            close_list()
            index += 1
            code: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1
            out.append("<pre><code>" + html.escape("\n".join(code))
                       + "</code></pre>")
            continue
        if not stripped:
            close_list()
            index += 1
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            close_list()
            level = min(4, len(heading.group(1)) + 1)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
            continue
        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
            close_list()
            out.append("<hr>")
            index += 1
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if bullet or numbered:
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                out.append(f"<{tag}>")
                list_tag = tag
            body = (bullet or numbered).group(1)
            index += 1
            extra: list[str] = []
            while (index < len(lines) and lines[index].strip()
                   and lines[index].startswith((" ", "\t"))
                   and not re.match(r"^\s*(?:[-*+]|\d+[.)])\s+",
                                    lines[index])):
                extra.append(lines[index].strip())
                index += 1
            item = _inline(body)
            for part in extra:
                item += f'<span class="meta">{_inline(part)}</span>'
            target = re.search(r'data-rel="([^"]+)"', item)
            if target:
                out.append(f'<li class="playable" data-rel="{target.group(1)}" '
                           f'title="Click to play this track">{item}</li>')
            else:
                out.append(f"<li>{item}</li>")
            continue
        close_list()
        out.append(f"<p>{_inline(stripped)}</p>")
        index += 1
    close_list()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# the service: one engine, one lock, import jobs


#: where the app remembers choices that are not taste (the library it was
#: pointed at), next to the learner profile and the journal
APP_SETTINGS = ("library-work", "app", "chat", "settings.json")


def app_settings(root: Path) -> dict:
    """The small app-level settings file ({} when it is missing)."""
    path = Path(root).joinpath(*APP_SETTINGS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_app_settings(root: Path, **values) -> dict:
    """Merge values into the app settings file, atomically.

    A value of `None` removes the key, which is how a memory of "work on
    another folder" is forgotten again.
    """
    path = Path(root).joinpath(*APP_SETTINGS)
    data = app_settings(root)
    for key, value in values.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        pass
    return data


def looks_like_library(path: Path) -> bool:
    """The same test the installer uses, so both agree on what is a library."""
    path = Path(path)
    return (path / "Playlists").is_dir() and (
        (path / "Artists").is_dir() or (path / "library-work").is_dir())


class ChatService:
    """Everything the page can ask for, serialised on one lock."""

    def __init__(self, root: Path | None = None, use_llm: bool = True,
                 dry_run: bool = False, auto_psycho: bool = True) -> None:
        self.engine = Engine(root=root, use_llm=use_llm, dry_run=dry_run)
        self.root = self.engine.root
        self.lock = threading.RLock()
        self.last_request = time.time()
        self.diagnostics = Diagnostics(self.root)
        self._last_status: dict = {}
        #: the greeting text and its HTML, keyed by the counts it quotes
        self._welcome: tuple[tuple, str, str] | None = None
        self.dry_run = bool(dry_run)
        self.use_llm = bool(use_llm)
        # the folder this process was started on: it is the one whose
        # `settings.json` the launcher reads, so a live switch is remembered
        # there and not in the folder we switch to
        self.start_root = Path(self.root)
        self.psycho_job: dict = {}
        self.index_job: dict = {}
        self.library_job: dict = {}
        # "if run": a start fills in whatever the profile is missing, in the
        # background, so the numbers are never stale for long
        if auto_psycho and not dry_run and self.engine.profile.setting(
                "psycho_auto", True):
            try:
                self.psycho_job = psycho_job.autostart(self.root)
            except OSError:
                self.psycho_job = {}
        # the daily plain-text backup of the profile and the lyrics
        if not dry_run and self.engine.profile.setting("library_backup", True):
            try:
                self.library_job = library_job.autostart(
                    self.root, keep=self._backup_keep())
            except (OSError, TypeError):
                self.library_job = {}

    def _backup_keep(self) -> int:
        try:
            return max(1, int(self.engine.profile.setting(
                "library_backup_keep", library_job.DEFAULT_KEEP)))
        except (TypeError, ValueError):
            return library_job.DEFAULT_KEEP

    def clear_diagnostics(self) -> dict:
        """Empty the diagnostics log from the window (previous bytes to `.1`)."""
        with self.lock:
            return self.diagnostics.clear()

    # -- state ------------------------------------------------------------

    def _planner(self) -> dict:
        llm = self.engine.llm
        return {
            "enabled": bool(getattr(llm, "enabled", False)),
            "available": bool(llm.available()),
            "summary": llm.summary(),
        }

    def state(self) -> dict:
        with self.lock:
            engine = self.engine
            stats = engine.catalog.stats()
            skills = load_skills()
            # the greeting is built from the stats, so compute it once and reuse
            # the same string for the markdown; the page asks for the state on
            # every refresh and a second build was a second of the same work
            welcome_key = (stats.get("files"), round(float(stats.get("hours") or 0)),
                           stats.get("artists"))
            if self._welcome is None or self._welcome[0] != welcome_key:
                text = engine.welcome()
                self._welcome = (welcome_key, text, md_to_html(text))
            welcome, welcome_html = self._welcome[1], self._welcome[2]
            profile = engine.profile.data
            families = engine.psycho.families() if engine.psycho.available() else []
            psycho_ready = bool(families)
            return {
                "root": str(self.root),
                # the release that is really running, read from the VERSION
                # file next to the code — one number for the window, the API
                # and the chat
                "version": version.info(),
                # ...and the release the files on disk carry, which an upgrade
                # can have moved past this process
                "restart": restart_status(),
                "artwork": self.artwork_reader(),
                "dry_run": bool(self.dry_run),
                "stats": stats,
                "planner": self._planner(),
                "psycho": {
                    "ready": psycho_ready,
                    "profiles": engine.psycho.count() if psycho_ready else 0,
                    "sets": (engine.psycho.sets()
                             if engine.psycho.available() else []),
                    "families": [
                        {"name": row["name"], "size": row["size"],
                         "traits": row["traits"], "playlist": row["playlist"]}
                        for row in families
                    ],
                    "job": self._psycho_summary(),
                },
                "index": self._index_summary(),
                # radio mode: whether the queue tops itself up, and from what
                "radio": radio.info(self.root, engine.catalog),
                # endless mode: grows from whatever just played, repeats when
                # nothing fitting is left
                "endless": endless.info(self.root, engine.catalog),
                "shuffle_mode": engine.shuffle_mode(),
                "library_data": self._library_summary(),
                # download support is an optional build-time module; the page
                # hides its import button and queue when this is false
                "downloads": {"available": youtube.available(self.root)},
                "lyrics": self._lyrics_summary(),
                # what "Continue" would start (and whether it can)
                "resume": engine.resume_info(),
                "welcome": welcome,
                "welcome_html": welcome_html,
                "suggestions": examples(),
                "help": help_markdown(),
                "profile": {
                    "summary": engine.profile.summary_markdown(),
                    "artists": len(profile.get("artists") or {}),
                    "genres": len(profile.get("genres") or {}),
                    "moods": len(profile.get("moods") or {}),
                    "notes": len(profile.get("notes") or []),
                    "mappings": len(profile.get("mappings") or {}),
                },
                "skills": [
                    {
                        "name": name,
                        "title": skills[name].title,
                        "description": skills[name].description,
                        "example": skills[name].example,
                        "triggers": skills[name].triggers[:6],
                    }
                    for name in ORDER if name in skills
                ],
                "playlists": [
                    {"name": playlist.name, "kind": playlist.kind,
                     "count": playlist.count}
                    for playlist in engine.catalog.playlists(refresh=True)
                ],
                "settings": self._state_settings(),
                "voice": self._voice_summary(),
                # enough to explain a "Load failed" from the page itself: the
                # full picture is GET /api/diagnostics and the log files
                "diagnostics": self.diagnostics.snapshot(compact=True),
            }

    def status(self) -> dict:
        # A chat turn (or a language-model call) holds the lock for as long as
        # it takes.  The strip must not wait for it: after a second the last
        # known state is answered instead, marked busy, so the page keeps
        # ticking and no poll looks like a dead server.
        if not self.lock.acquire(timeout=1.0):
            stale = dict(self._last_status) if self._last_status else {}
            stale["busy"] = True
            return stale
        try:
            self.engine.poll()          # implicit learning from the player
            raw = self.engine.player.status() or {}
            current = self.engine.ctx.current
            playing = bool(raw) and not raw.get("idle")
            duration = float(raw.get("duration") or 0)
            position = float(raw.get("time") or 0)
            # The strip's cover needs the library-relative path of whatever mpv
            # is really playing.  ctx.current is only set for tracks the chat
            # queued, so a queue started elsewhere (a playlist, `music play`,
            # a media key after a restart) had no rel and therefore no cover.
            rel = ""
            file_name = str(raw.get("file") or "")
            if playing and file_name:
                try:
                    rel = Path(file_name).resolve().relative_to(
                        Path(self.engine.root).resolve()).as_posix()
                except (ValueError, OSError):
                    rel = ""
            if playing and not rel and current is not None:
                rel = current.rel
            track = self.engine.catalog.track(rel) if rel else None
            if playing and rel and track is not None:
                title, artist = track.name, track.artist
            elif playing and rel:
                title = Path(rel).stem.replace("_", " ")
                artist = ""
            else:
                title = Path(file_name).name
                artist = ""
            # what the playing track itself still lacks (a sound profile and
            # lyrics); the strip says that next to the artist instead of only
            # quoting the library-wide counts.  Cached, so the 2 s poll is cheap.
            track_state = (self.engine.track_state(rel)
                           if (playing and rel) else None)
            payload = {
                "playing": playing,
                "paused": bool(raw.get("pause")),
                "title": title,
                "artist": artist,
                "rel": rel,
                "art": _art_kind(self.engine.root, rel) if rel else "none",
                "track_state": track_state,
                "position": position,
                "duration": duration,
                "progress": (position / duration) if duration else 0.0,
                "playlist_count": int(raw.get("playlist_count") or 0),
                "playlist_pos": int(raw.get("playlist_pos") or 0),
                "volume": float(raw.get("volume") or 0),
                "line": self.engine.player.status_line(),
                "time": _clock(position),
                "total": _clock(duration),
                "error": getattr(self.engine.player, "last_error", "") or "",
                # the release this process loaded against the one on disk.
                # One stat per poll, so the page notices an upgrade within two
                # seconds and can restart the server instead of asking.
                "restart": restart_status(),
            }
            payload.pop("busy", None)
            self._last_status = payload
            return payload
        finally:
            self.lock.release()

    # -- chat -------------------------------------------------------------

    def chat(self, message: str) -> dict:
        with self.lock:
            self.engine.poll()
            reply = self.engine.handle(message)
            # commands that change what the library looks like; download
            # imports are asynchronous, so their reload happens on the job poll
            if reply.tool in {"make-playlist", "save-playlist",
                              "edit-playlist", "add-file"}:
                self.engine.catalog.reload()
            out = _reply(reply)
            wanted = self.engine.deferred.pop("library", "")
        # a folder switch rebuilds the engine, so it happens outside the turn
        if wanted:
            result = self.set_library(wanted)
            out["library_result"] = result
            if result.get("error"):
                out.update(text=f"Could not switch the music folder: "
                                f"{result['error']}"
                                f"{(' ' + result.get('hint', '')) if result.get('hint') else ''}",
                           error=True)
            else:
                note = f" {result['note']}" if result.get("note") else ""
                out.update(text=f"**Music folder** is now "
                                f"`{result.get('library') or wanted}`.{note}",
                           error=False)
            out["html"] = md_to_html(out["text"])
        return out

    def activity(self, limit: int = 40) -> dict:
        with self.lock:
            return self.engine.activity_data(limit)

    def profile_page(self, limit: int = 40) -> dict:
        """Everything the Profile page shows — read-only."""
        with self.lock:
            return self.engine.profile_data(limit)

    def recent_changes(self, limit: int = 30) -> dict:
        rows = self.engine.recent_changes(limit)
        return {"changes": rows,
                "undoable": sum(1 for row in rows if row.get("undoable"))}

    def library_health(self) -> dict:
        with self.lock:
            return self.engine.library_health()

    def library_stats(self) -> dict:
        with self.lock:
            return self.engine.library_stats()

    def play_history(self, days: int | None = None,
                     limit: int = 20,
                     forgotten_days: int = 365) -> dict:
        """The listening record — read-only, app state only."""
        with self.lock:
            return self.engine.play_history(days=days, limit=limit,
                                            forgotten_days=forgotten_days)

    def library_duplicates(self) -> dict:
        """The duplicate review: read-only, no song is moved or deleted."""
        with self.lock:
            return self.engine.library_duplicates()

    def library_arrivals(self, limit: int = 30,
                         days: int | None = None) -> dict:
        """The newest files by their own time — read-only, nothing written."""
        with self.lock:
            return self.engine.library_arrivals(limit=limit, days=days)

    # -- metadata and cover art -------------------------------------------

    def tags(self, rel: str) -> dict:
        """The editable fields, the album it belongs to and its artwork."""
        with self.lock:
            return tagedit.describe(self.engine.root, rel)

    def set_tags(self, payload: dict) -> dict:
        rel = str(payload.get("rel") or "").strip()
        if not rel:
            return {"ok": False, "error": "no track given",
                    "message": "No track was given."}
        values = payload.get("values")
        if not isinstance(values, dict):
            return {"ok": False, "error": "no fields given",
                    "message": "No fields were given."}
        with self.lock:
            result = tagedit.apply(self.engine.root, rel, values,
                                   bool(payload.get("album_wide")))
            if result.get("ok"):
                self.engine.catalog.reload()
            return result

    def cover(self, rel: str) -> dict:
        """The image the now-playing strip shows: embedded art, else the album.

        Answers ``mime``/``data``/``source``/``error``; an empty ``data`` with
        an ``error`` means the reader failed, which the route reports as a 500
        instead of the 404 it sends for a track without any art.

        No lock: this reads one file (and one small image) and writes nothing,
        so a chat turn must not make the strip's cover wait.
        """
        return tagedit.art_read(self.engine.root, rel)

    def artwork_reader(self) -> dict:
        """Whether this interpreter can read tags at all.

        The packaged app runs on the distribution Python, which has no mutagen
        unless ``python3-mutagen`` is installed.  Without it every cover looks
        absent, so the state and the Diagnostics tab say so outright.
        """
        cached = getattr(self, "_art_reader", None)
        if cached is not None:
            return dict(cached)
        try:
            import mutagen
        except Exception as exc:                      # noqa: BLE001
            result = {"ok": False, "version": "",
                      "error": f"the tag reader is missing ({exc})",
                      "python": sys.executable,
                      "hint": "install python3-mutagen (or pip install mutagen)"}
        else:
            version_tuple = getattr(mutagen, "version", ()) or ()
            try:
                number = ".".join(str(part) for part in version_tuple)
            except TypeError:
                number = str(version_tuple)
            result = {"ok": True, "version": number, "error": "",
                      "python": sys.executable, "hint": ""}
        self._art_reader = result
        return dict(result)

    def set_artwork(self, payload: dict) -> dict:
        rel = str(payload.get("rel") or "").strip()
        if not rel:
            return {"ok": False, "error": "no track given",
                    "message": "No track was given."}
        raw = str(payload.get("data") or "")
        try:
            data = base64.b64decode(raw.split(",", 1)[-1], validate=True)
        except (ValueError, TypeError):
            data = b""
        with self.lock:
            result = tagedit.set_artwork(
                self.engine.root, rel, data,
                str(payload.get("mime") or ""),
                str(payload.get("target") or "embed"),
                bool(payload.get("remove")))
            if result.get("ok"):
                self.engine.catalog.reload()
            return result

    def undo_change(self, ts: str) -> dict:
        with self.lock:
            result = self.engine.undo_change(ts)
            result["changes"] = self.engine.recent_changes(30)
            return result

    # -- radio mode --------------------------------------------------------

    def radio_state(self) -> dict:
        with self.lock:
            return radio.info(self.root, self.engine.catalog)

    def radio_action(self, action: str = "status", target: str = "") -> dict:
        """The window's radio button: the same skill the chat uses."""
        action = str(action or "status").casefold()
        if action not in {"start", "stop", "status", "more"}:
            return {"error": f"unknown radio action: {action}"}
        with self.lock:
            reply = self.engine.skill_radio(
                {"action": action, "target": str(target or "")}, action)
            return {"ok": not reply.error, "note": reply.text,
                    "action": action,
                    "radio": radio.info(self.root, self.engine.catalog)}

    # -- endless mode and shuffle -----------------------------------------

    def endless_state(self) -> dict:
        with self.lock:
            return endless.info(self.root, self.engine.catalog)

    def endless_action(self, action: str = "status") -> dict:
        """The window's endless button: the same skill the chat uses."""
        action = str(action or "status").casefold()
        if action not in {"start", "on", "stop", "off", "status", "more"}:
            return {"error": f"unknown endless action: {action}"}
        if action == "on":
            action = "start"
        if action == "off":
            action = "stop"
        with self.lock:
            reply = self.engine.skill_endless({"action": action}, action)
            return {"ok": not reply.error, "note": reply.text,
                    "action": action,
                    "endless": endless.info(self.root, self.engine.catalog)}

    def shuffle_action(self, on: bool) -> dict:
        """Arm or disarm shuffle for the next playlist (a stored setting)."""
        action = "shuffle_on" if on else "shuffle_off"
        with self.lock:
            reply = self.engine.skill_transport({"action": action},
                                                "window button")
            return {"ok": not reply.error, "on": self.engine.shuffle_mode(),
                    "note": reply.text}

    # -- settings ----------------------------------------------------------

    def llm_info(self, models: bool = False) -> dict:
        """The planner as the page shows it — never the key itself."""
        llm = self.engine.llm
        provider = str(self.engine.profile.setting("llm_provider", "auto")
                       or "auto")
        configured = str(self.engine.profile.setting("llm_base_url", "") or "")
        info = {
            "enabled": bool(llm.enabled),
            "available": llm.available(),
            "provider": provider,
            "base_url": (str(llm.config.get("base_url") or "")
                         or configured
                         or setting_registry.PROVIDER_BASE.get(provider, "")),
            "model": str(self.engine.profile.setting("llm_model", "") or ""),
            "resolved_model": str(llm.config.get("model") or ""),
            "source": str(llm.config.get("source") or ""),
            "key_set": setting_registry.has_secret(self.root, "llm_api_key"),
            "tools": not llm.tools_unsupported,
            "summary": llm.summary(),
            "error": llm.last_error,
        }
        if models and info["available"]:
            info["models"] = llm.models()
        return info

    def settings_view(self) -> dict:
        """Every setting for the panel, plus what voice input needs."""
        with self.lock:
            rows = setting_registry.api(self.engine.profile, self.root)
            return {
                "rows": rows,
                "voice": self.voice_status(),
                "llm": self.llm_info(models=True),
                "library": str(self.root),
                "library_saved": self._saved_library(),
            }

    def settings(self, values: dict) -> dict:
        """Apply changes from the window through the one registry."""
        values = dict(values or {})
        result: dict = {}
        if "library" in values:
            result["library_result"] = self.set_library(
                str(values.pop("library") or ""))
        errors: list[str] = []
        with self.lock:
            engine = self.engine
            # choosing a preset provider resets the address and the model, so
            # an old Ollama URL cannot silently win over a GWDG choice; an
            # explicit address is sent as its own field and is kept
            provider = str(values.get("llm_provider") or "").strip()
            if provider in {"ollama", "gwdg"}:
                for key in ("llm_base_url", "llm_model"):
                    if key in values:
                        continue
                    item = setting_registry.spec(key)
                    if item is not None:
                        setting_registry.apply(engine.profile, item, "",
                                               self.root)
                if "use_llm" not in values:
                    item = setting_registry.spec("use_llm")
                    if item is not None:
                        setting_registry.apply(engine.profile, item, True,
                                               self.root)
                engine.llm.reconfigure()
            for key in values.pop("reset", None) or []:
                item = setting_registry.spec(str(key))
                if item is None or item["key"] in setting_registry.SERVICE_KEYS:
                    continue
                try:
                    setting_registry.reset(engine.profile, item, self.root)
                    engine._settings_applied(item)
                except (OSError, ValueError) as exc:
                    errors.append(f"{item['label']}: {exc}")
            for key, raw in values.items():
                item = setting_registry.spec(str(key))
                if item is None or item["key"] in setting_registry.SERVICE_KEYS:
                    continue
                # never delete a secret by submitting an empty field
                if item["key"] in setting_registry.SECRET_KEYS \
                        and not str(raw or "").strip():
                    continue
                try:
                    value = setting_registry.coerce(item, raw)
                    setting_registry.apply(engine.profile, item, value,
                                           self.root)
                    engine._settings_applied(item)
                except (OSError, ValueError) as exc:
                    errors.append(f"{item['label']}: {exc}")
            current = setting_registry.values(engine.profile, self.root)
            for key, value in current.items():
                if key in setting_registry.SECRET_KEYS:
                    result["llm_api_key_set"] = bool(value)
                else:
                    result[key] = value
            result["library"] = str(self.root)
            result["library_saved"] = self._saved_library()
            result["llm"] = self.llm_info()
            if errors:
                result["errors"] = errors
            return result

    # -- voice input -------------------------------------------------------

    def voice_status(self) -> dict:
        status = voice.status(self.root)
        try:
            profile = self.engine.profile
            status["enabled"] = bool(profile.setting("voice_enabled", True))
            status["autosend"] = bool(profile.setting("voice_autosend", True))
            status["language"] = str(profile.setting("voice_language", "auto")
                                     or "auto")
            status["model"] = str(profile.setting("voice_model", "auto") or "auto")
        except Exception:                      # no engine yet during start-up
            status.setdefault("enabled", True)
        return status

    def voice(self, data: bytes, content_type: str = "") -> dict:
        """Transcribe one microphone clip locally; the audio is not kept."""
        with self.lock:
            profile = self.engine.profile
            enabled = bool(profile.setting("voice_enabled", True))
            language = str(profile.setting("voice_language", "auto") or "auto")
            model = str(profile.setting("voice_model", "auto") or "auto")
            root = self.root
        if not enabled:
            return {"ok": False,
                    "error": "voice input is switched off — turn it on in the "
                             "settings"}
        # outside the lock: a clip takes seconds and the strip must keep
        # ticking while it is transcribed
        return voice.transcribe(
            root, data, content_type=content_type, language=language,
            model="" if model == "auto" else model)

    def _state_settings(self) -> dict:
        """The flat settings block /api/state carries, without any secret."""
        values = setting_registry.values(self.engine.profile, self.root)
        flat = {key: value for key, value in values.items()
                if key not in setting_registry.SECRET_KEYS}
        flat["viz_styles"] = list(VIZ_STYLES)
        flat["llm_api_key_set"] = bool(values.get("llm_api_key"))
        flat["library_saved"] = self._saved_library()
        flat["llm"] = self.llm_info()
        return flat

    def _voice_summary(self) -> dict:
        """What the mic button needs; the interpreter probe is cached."""
        profile = self.engine.profile
        present = bool(voice.available(self.root))
        return {
            "enabled": bool(profile.setting("voice_enabled", True)),
            "autosend": bool(profile.setting("voice_autosend", True)),
            "language": str(profile.setting("voice_language", "auto") or "auto"),
            "model": str(profile.setting("voice_model", "auto") or "auto"),
            "available": present,
            # `available` only says the script exists; whether the interpreter
            # can import faster-whisper is what `ready` means (probed once and
            # remembered by `lyrics_job.interpreter`)
            "ready": bool(lyrics_job.interpreter(self.root)) if present else False,
            "note": "" if present else "this library has no voice transcriber",
        }

    # -- lyrics: read the stored lines, transcribe from here ----------------

    def lyrics(self, rel: str = "") -> dict:
        """The timed lines of one song, or of the current one."""
        rel = str(rel or "").strip()
        with self.lock:
            track = None
            if rel:
                track = self.engine.catalog.track(rel)
            if track is None:
                track = self.engine.ctx.current
            if track is None and rel:
                # a rel the catalogue does not know (a database row from a
                # file that has since been renamed) still answers with its text
                row = self.engine.lyrics.track(rel)
                if row is not None:
                    return {"rel": rel, "title": row["title"],
                            "artist": row["artist"], "album": row["album"],
                            "language": row["language"], "model": row["model"],
                            "transcribed_at": row["transcribed_at"],
                            "lines": self.engine.lyrics.lines(rel),
                            "offset": self.engine.lyrics.offset(rel),
                            "current": False, "available": True}
                return {"rel": rel, "lines": [], "available": False,
                        "error": "no lyrics for that song"}
            if track is None:
                return {"rel": "", "lines": [], "available": False,
                        "error": "nothing is playing"}
            record = self.engine.lyrics.track(track.rel)
            state = self.engine.track_state(track.rel)
            if record is None:
                return {"rel": track.rel, "title": track.name,
                        "artist": track.who, "album": track.album,
                        "lines": [], "offset": 0.0, "available": False,
                        "backend": lyrics_job.backend_info(self.root),
                        "track_state": state}
            path = self.root / track.rel
            current = False
            if path.is_file():
                stat = path.stat()
                current = self.engine.lyrics.is_current(
                    track.rel, stat.st_size, stat.st_mtime)
            return {"rel": track.rel, "title": record["title"] or track.name,
                    "artist": record["artist"] or track.who,
                    "album": record["album"] or track.album,
                    "language": record["language"], "model": record["model"],
                    "transcribed_at": record["transcribed_at"],
                    "lines": self.engine.lyrics.lines(track.rel),
                    "offset": self.engine.lyrics.offset(track.rel),
                    "current": current, "available": True,
                    "track_state": state}

    def track_state(self, rel: str = "") -> dict:
        """What one track still lacks — read-only, for the API and tests.

        Without a ``rel`` the playing track is used, the same one the strip,
        the profile panel and the lyrics panel speak about.
        """
        with self.lock:
            rel = str(rel or "").strip()
            if not rel:
                track = self.engine._playing_track()
                rel = track.rel if track is not None else ""
            return self.engine.track_state(rel)

    def lyrics_status(self) -> dict:
        with self.lock:
            return {
                "status": lyrics_job.status(self.root),
                "counts": lyrics_job.counts(self.root),
                "backend": lyrics_job.backend_info(self.root),
            }

    def start_lyrics(self, rel: str = "", refresh: bool = False,
                     missing: int = 0) -> dict:
        with self.lock:
            if self.dry_run:
                return {"state": "idle", "running": False, "dry_run": True,
                        "note": "Dry run: the transcription is not started."}
            if not lyrics_job.available(self.root):
                return {"state": "idle", "running": False, "available": False,
                        "error": "No local transcriber is "
                                 "available here."}
            if not lyrics_job.backend_info(self.root).get("ready"):
                return {"state": "idle", "running": False, "available": False,
                        "error": "No local transcriber is installed — run "
                                 "setup_asr.sh first."}
            try:
                job = lyrics_job.start(self.root, rel=str(rel or ""),
                                       refresh=bool(refresh),
                                       missing=int(missing or 0))
            except (OSError, RuntimeError) as exc:
                return {"state": "idle", "running": False, "error": str(exc)}
            return lyrics_job.status(self.root, str(job.get("id") or ""))

    def lyrics_similar(self, rel: str = "", limit: int = 12) -> dict:
        """Songs whose sung words resemble one song's — read-only.

        Same seed rule as the panel: the named track, else the playing one.
        No audio, no database edit; only the stored lyric text is compared.
        """
        with self.lock:
            rel = str(rel or "").strip()
            track = self.engine.catalog.track(rel) if rel else None
            if track is None:
                track = self.engine.ctx.current
            if track is None:
                return {"rel": "", "hits": [], "available": False,
                        "error": "nothing is playing"}
            record = self.engine.lyrics.track(track.rel)
            if record is None:
                return {"rel": track.rel, "title": track.name,
                        "artist": track.who, "hits": [], "available": False}
            try:
                count = int(limit or 12)
            except (TypeError, ValueError):
                count = 12
            hits = self.engine.lyrics.similar(
                track.rel, limit=max(1, min(40, count)))
            return {"rel": track.rel,
                    "title": record["title"] or track.name,
                    "artist": record["artist"] or track.who,
                    "available": True, "hits": hits}

    def lyrics_offset(self, rel: str = "", value=None,
                      relative: bool = False) -> dict:
        """Shift (or read, or reset) one song's lyric timings."""
        with self.lock:
            rel = str(rel or "").strip()
            track = self.engine.catalog.track(rel) if rel else None
            if track is None:
                track = self.engine.ctx.current
            if track is None:
                return {"error": "nothing is playing"}
            current = self.engine.lyrics.offset(track.rel)
            if value is None or str(value).strip() == "":
                return {"rel": track.rel, "offset": current}
            try:
                wanted = float(value) + (current if relative else 0.0)
            except (TypeError, ValueError):
                return {"rel": track.rel, "offset": current,
                        "error": "the offset must be a number of seconds"}
            try:
                stored = self.engine.lyrics.set_offset(track.rel, wanted)
            except ValueError as exc:
                return {"rel": track.rel, "offset": current, "error": str(exc)}
            return {"rel": track.rel, "offset": stored,
                    "previous": current}

    def lyrics_export(self, rel: str = "", path: str = "",
                      overwrite: bool = False) -> dict:
        """Write the timed lyrics next to the song (journalled first)."""
        with self.lock:
            rel = str(rel or "").strip()
            track = self.engine.catalog.track(rel) if rel else None
            if track is None:
                track = self.engine.ctx.current
            if track is None:
                return {"error": "nothing is playing"}
            if self.dry_run:
                return {"rel": track.rel, "dry_run": True,
                        "target": str(lrc.target_for(self.root, track.rel,
                                                     str(path or ""))),
                        "note": "Dry run: no file is written."}
            result = lrc.export(self.root, self.engine.lyrics, track.rel,
                                out=str(path or ""),
                                overwrite=bool(overwrite))
            result.setdefault("rel", track.rel)
            return result

    def setup_lyrics(self, cpu: bool = False) -> dict:
        with self.lock:
            if self.dry_run:
                return {"state": "idle", "running": False, "dry_run": True}
            try:
                job = lyrics_job.start_setup(self.root, cpu=bool(cpu))
            except (OSError, RuntimeError) as exc:
                return {"state": "idle", "running": False, "error": str(exc)}
            return lyrics_job.status(self.root, str(job.get("id") or ""))

    def _lyrics_summary(self) -> dict:
        """A small note for the page: how many songs have lyrics, is a job on?"""
        counts = lyrics_job.counts(self.root)
        info = lyrics_job.backend_info(self.root)
        status = lyrics_job.status(self.root)
        state = str(status.get("state") or "idle")
        tracks = int(counts.get("tracks", 0) or 0)
        audio = int(counts.get("audio", 0) or 0)
        return {
            "available": lyrics_job.available(self.root),
            "ready": bool(info.get("ready")),
            "gpu": info.get("gpu") or "unknown",
            "tracks": tracks,
            "lines": counts.get("lines", 0),
            "audio": audio,
            "state": state,
            "running": state == "running",
            "paused": state == "paused",
            "active": state in {"running", "paused"},
            "percent": float(status.get("percent") or 0.0),
            "step": str(status.get("step") or ""),
            "done": tracks,
            "total": audio,
        }

    def waveform(self, rel: str) -> dict:
        # no lock: this only reads a file and writes its cache entry, and
        # ffmpeg can take a minute on a long track.  Holding the chat lock
        # here would stall the status strip for exactly that long.
        return waveform(self.root, rel)

    def spectrum(self, rel: str) -> dict:
        return viz_spectrum(self.root, rel)

    # -- sound profile: measuring it from here -----------------------------

    def _psycho_summary(self) -> dict:
        """A small note for the page: is the profile being measured right now?"""
        if not psycho_job.available(self.root):
            return {"state": "idle", "running": False, "percent": 0.0,
                    "available": False}
        status = psycho_job.status(self.root)
        state = str(status.get("state") or "idle")
        summary = {
            "state": state,
            "running": state == "running",
            "paused": state == "paused",
            "active": state in {"running", "paused"},
            "percent": float(status.get("percent") or 0.0),
            "step": str(status.get("step") or ""),
            "done": int(status.get("done") or 0),
            "total": int(status.get("total") or 0),
            "available": True,
        }
        if state != "running" and getattr(self, "psycho_job", None):
            self.psycho_job = {**self.psycho_job, "state": state}
        return summary

    def psycho_update_status(self) -> dict:
        with self.lock:
            status = psycho_job.status(self.root)
            self.psycho_job = {key: status.get(key) for key in
                               ("id", "state", "started", "finished",
                                "pid", "log", "steps")}
            return status

    def start_psycho_update(self, refresh: bool = False,
                            workers: int = 0) -> dict:
        with self.lock:
            if self.dry_run:
                return {"state": "idle", "running": False, "dry_run": True,
                        "note": "Dry run: the measurement is not started.",
                        "available": psycho_job.available(self.root),
                        "counts": psycho_job.counts(self.root)}
            if not psycho_job.available(self.root):
                return {"state": "idle", "running": False, "available": False,
                        "error": "This library has no scripts/psycho_features.py, "
                                 "so the app cannot measure it."}
            try:
                job = psycho_job.start(self.root, refresh=refresh,
                                       workers=workers)
            except (OSError, RuntimeError) as exc:
                return {"state": "idle", "running": False, "active": False,
                        "available": True, "error": str(exc)}
            self.psycho_job = job
            status = psycho_job.status(self.root, str(job.get("id") or ""))
            status["started_now"] = True
            return status

    # -- the background jobs the footer shows ------------------------------
    #
    # Three tools fill the library while the window is open: the inventory
    # index, the lyric transcription and the sound profile.  The footer shows
    # each one's progress with a pause/start and a refresh button, so none of
    # them has to be driven from a shell.

    def work(self) -> dict:
        """Progress rows for whatever background work this library can do.

        All three jobs stay in the list while the library has the scripts for
        them: an idle one is the place to *start* it, a running one shows its
        progress, and a paused one shows that it is waiting.  The lyric and
        profile rows also disappear once there is nothing left to do, so the
        footer stays quiet on a finished library.
        """
        jobs: list[dict] = []
        with self.lock:
            # downloads first: an import that is really running is the one
            # thing the footer must show, and it sits left of the index chip
            try:
                records = youtube.jobs(self.root)
            except Exception:
                records = []
            live = [record for record in records
                    if str(record.get("status") or "") in {"running", "queued"}]
            if live:
                from . import procs
                record = next((row for row in live
                               if row.get("status") == "running"), live[0])
                pid = int(record.get("pid") or 0)
                paused = (record.get("status") == "running" and pid
                          and procs.process_state(pid) == "paused")
                running = (record.get("status") == "running" and not paused)
                queued = len([row for row in live
                              if row.get("status") == "queued"])
                jobs.append({
                    "key": "downloads", "label": "downloads",
                    "state": "paused" if paused else (
                        "running" if running else "queued"),
                    "active": True,
                    "paused": bool(paused),
                    "available": True,
                    "percent": 0.0,
                    "value": (f"{len(live)} queued" if queued else "1 import"),
                    "detail": str(record.get("display")
                                  or record.get("title")
                                  or "download")[:80],
                    "job": str(record.get("id") or ""),
                    "can_start": False,
                })
            else:
                # nothing is downloading: keep a failed or partly filed import
                # in the queue until the listener retries or clears it, so the
                # footer never hides an error
                failed = youtube.attention(self.root, records)
                if failed:
                    status = str(failed.get("status") or "unknown")
                    reason = str(failed.get("summary") or "").strip()
                    waiting = sum(
                        1 for row in records
                        if not row.get("dismissed")
                        and str(row.get("status") or "")
                        in youtube.ATTENTION)
                    label = {"partial": "partly filed"}.get(status, status)
                    jobs.append({
                        "key": "downloads", "label": "downloads",
                        "state": status if status in
                                 {"failed", "partial", "unknown"} else "failed",
                        "active": False, "paused": False, "available": True,
                        "percent": 0.0,
                        "value": (f"{waiting} {label}" if waiting > 1
                                  else label),
                        "detail": (
                            str(failed.get("display") or failed.get("title")
                                or failed.get("url") or "download")
                            + (f" — {reason}" if reason else ""))[:160],
                        "job": str(failed.get("id") or ""),
                        "retry": True, "dismissible": True,
                        "can_start": True,
                    })
                elif youtube.available(self.root):
                    # the download queue keeps its place next to the index
                    # chip even when it is empty, so the footer always shows
                    # where an import would appear; a build without a module
                    # stays quiet instead
                    newest = next((row for row in records
                                   if not row.get("dismissed")), None)
                    if newest:
                        detail = (f"last: {youtube.status_line(newest)} · "
                                  + str(newest.get("display")
                                        or newest.get("url") or ""))
                    else:
                        detail = ("nothing queued — say “search for …” "
                                  "and then “download 1”")
                    jobs.append({
                        "key": "downloads", "label": "downloads",
                        "state": "idle",
                        "active": False, "paused": False, "available": True,
                        "percent": 0.0,
                        "value": "0 queued",
                        "detail": detail.strip()[:160],
                        "job": "", "startable": False,
                        "can_start": False,
                    })
            if index_job.available(self.root):
                counts = index_job.counts(self.root)
                status = index_job.status(self.root)
                state = str(status.get("state") or "idle")
                self._note_index_state(state)
                # `cancelling` still counts as active: it stays visible during
                # the grace period, just without a pause button
                active = state in {"running", "paused", "cancelling"}
                percent = float(status.get("percent") or 0.0)
                tracks = int(counts.get("tracks") or 0)
                jobs.append({
                    "key": "index", "label": "index",
                    "state": state,
                    "active": active,
                    "paused": state == "paused",
                    "stopping": state == "cancelling",
                    "available": True,
                    "percent": percent,
                    "done": int(status.get("done") or 0),
                    "total": int(status.get("total") or 0),
                    "tracks": tracks,
                    # a scan is incremental, so "work left" is only knowable by
                    # running it: the button is always offered
                    "value": (f"{percent:.0f} %" if active
                              else f"{tracks:,} tracks"),
                    "detail": str(status.get("step")
                                  or status.get("phase") or ""),
                    "can_start": state not in {"running", "paused",
                                               "cancelling"},
                })
            if lyrics_job.available(self.root):
                counts = lyrics_job.counts(self.root)
                status = lyrics_job.status(self.root)
                state = str(status.get("state") or "idle")
                active = state in {"running", "paused"}
                tracks = int(counts.get("tracks") or 0)
                audio = int(counts.get("audio") or 0)
                jobs.append({
                    "key": "lyrics", "label": "lyrics",
                    "state": state,
                    "active": active,
                    "paused": state == "paused",
                    "available": True,
                    "ready": bool(lyrics_job.backend_info(self.root)
                                  .get("ready")),
                    "percent": (100.0 * tracks / audio) if audio else 0.0,
                    "done": tracks, "total": audio,
                    "value": (f"{tracks:,} / {audio:,}" if audio
                              else f"{tracks:,} songs"),
                    # the step is only meaningful while something runs; an
                    # idle row would otherwise keep the last song's name
                    "detail": (str(status.get("step") or "") if active else ""),
                    "can_start": audio > tracks,
                })
            if psycho_job.available(self.root):
                counts = psycho_job.counts(self.root)
                status = psycho_job.status(self.root)
                state = str(status.get("state") or "idle")
                active = state in {"running", "paused"}
                done = int(counts.get("measured") or 0)
                # a track imported after the last run has no row yet, so the
                # inventory count is the honest denominator (13,199 of 13,200)
                total = max(int(counts.get("total") or 0),
                            int(counts.get("known") or 0))
                # the reason a finished-looking database still has work to do
                missing = psycho_job.needed(self.root)
                jobs.append({
                    "key": "psycho", "label": "profiles",
                    "state": state,
                    "active": active,
                    "paused": state == "paused",
                    "available": True,
                    "percent": (100.0 * done / total) if total else 0.0,
                    "done": done, "total": total,
                    "value": (f"{done:,} / {total:,}" if total
                              else "no database"),
                    "detail": (str(status.get("step") or "") if active
                               else missing),
                    "can_start": bool(missing),
                })
            if (library_job.available(self.root)
                    and self.engine.profile.setting("library_backup", True)):
                counts = library_job.counts(self.root)
                status = library_job.status(self.root)
                state = str(status.get("state") or "idle")
                active = state in {"running", "paused"}
                latest = counts.get("latest") or {}
                jobs.append({
                    "key": "backup", "label": "backup",
                    "state": state,
                    "active": active,
                    "paused": state == "paused",
                    "available": True,
                    "percent": float(status.get("percent") or 0.0),
                    "done": int(status.get("done") or 0),
                    "total": int(status.get("total") or 0),
                    "value": (latest.get("day") or "never"),
                    "detail": (str(status.get("step") or "") if active
                               else (f"{counts.get('exports', 0)} exports"
                                     if not counts.get("due")
                                     else "today's backup is due")),
                    "can_start": bool(counts.get("due")) or state == "failed",
                })
            # only what is worth a line: a running job, or one with work left;
            # the download queue is always there, empty or not
            jobs = [job for job in jobs
                    if job["active"] or job.get("can_start")
                    or job["key"] == "downloads"]
            return {"jobs": jobs}

    def control_work(self, job: str, action: str, job_id: str = "") -> dict:
        """Pause, continue, start, retry or re-check one background job."""
        job = str(job or "").strip().lower()
        action = str(action or "").strip().lower()
        job_id = str(job_id or "").strip()
        if job not in {"index", "lyrics", "psycho", "backup", "downloads"}:
            return {"error": f"unknown background job: {job}",
                    "jobs": self.work()["jobs"]}
        if job == "downloads":
            result = self.control_downloads(action, job_id)
        elif job == "index":
            result = (self.control_index(action) if action in
                      {"pause", "resume", "cancel"} else self.start_index())
        elif job == "lyrics":
            result = self.control_lyrics(action)
        elif job == "backup":
            result = self.control_library(action)
        else:
            result = self.control_psycho(action)
        return {"job": job, "action": action, "status": result,
                "jobs": self.work()["jobs"]}

    def control_downloads(self, action: str = "", job_id: str = "") -> dict:
        """Pause, continue, retry, cancel, reorder or clear a download import.

        A failed import keeps its place in the footer until it is retried or
        acknowledged, so `retry` queues the same URL again and `dismiss` (also
        sent by the ✕ on the chip) only stops showing it.  `up`/`down` reorder
        the waiting jobs, and `cancel` drops a waiting one or stops a running
        one without turning it into a failure.
        """
        action = str(action or "refresh").strip().lower()
        job_id = str(job_id or "").strip()
        with self.lock:
            if self.dry_run:
                return {"state": "idle", "running": False, "active": False,
                        "dry_run": True,
                        "note": f"Dry run: nothing to {action}."}
            handler = {"pause": youtube.pause, "resume": youtube.resume,
                       "retry": youtube.retry,
                       "cancel": youtube.cancel,
                       "dismiss": youtube.dismiss,
                       "forget": youtube.dismiss}.get(action)
            if action in {"up", "down"}:
                return youtube.move(self.root, job_id,
                                    -1 if action == "up" else 1)
            if handler is not None:
                return handler(self.root, job_id)
            if action in {"refresh", "start"}:
                record = youtube.latest(self.root)
                state = youtube._state(self.root, record)
                if not state.get("active"):
                    failed = youtube.attention(self.root)
                    if failed is not None:
                        return {**youtube._state(self.root, failed),
                                "attention": True}
                    state["note"] = "No download is running."
                return state
            return {"state": "idle", "running": False, "active": False,
                    "error": f"Unknown download action `{action}`. Use pause, "
                             "resume, retry, cancel, up, down or dismiss."}

    def control_lyrics(self, action: str = "") -> dict:
        """Pause, continue, start or re-check the lyric transcription.

        The footer's play button runs a *full* pass (every song without
        lyrics, pausable like the index) — the 25-song batch is only what the
        lyrics panel's `+` button asks for via `/api/lyrics/transcribe`.
        """
        action = str(action or "refresh").strip().lower()
        if action in {"start", "refresh"}:
            if not lyrics_job.available(self.root):
                return {"state": "idle", "running": False, "active": False,
                        "available": False,
                        "error": "No local transcriber is "
                                 "available here."}
            status = lyrics_job.status(self.root)
            if status.get("active"):
                return status
            counts = lyrics_job.counts(self.root)
            if action == "refresh" and (int(counts.get("audio") or 0)
                                        <= int(counts.get("tracks") or 0)):
                return {**status,
                        "note": "Every song in the library has lyrics."}
            # `missing=0` means `--missing` with no `--limit`: the whole
            # library, one song after the other, until it is paused.
            return self.start_lyrics(missing=0)
        handler = {"pause": lyrics_job.pause,
                   "resume": lyrics_job.resume}.get(action)
        if handler is None:
            return {"state": "idle", "running": False, "active": False,
                    "error": f"Unknown lyrics action `{action}`. Use start, "
                             "pause or resume."}
        with self.lock:
            if self.dry_run:
                return {"state": "idle", "running": False, "active": False,
                        "dry_run": True,
                        "note": f"Dry run: nothing to {action}."}
            return handler(self.root)

    def control_psycho(self, action: str = "") -> dict:
        """Pause, continue, start or re-check the sound-profile measurement."""
        action = str(action or "refresh").strip().lower()
        if action in {"start", "refresh"}:
            if not psycho_job.available(self.root):
                return {"state": "idle", "running": False, "active": False,
                        "available": False,
                        "error": "This library has no "
                                 "scripts/psycho_features.py."}
            status = psycho_job.status(self.root)
            if status.get("active"):
                return status
            if not psycho_job.needed(self.root):
                return {**status,
                        "note": "The sound profile is complete."}
            return self.start_psycho_update()
        handler = {"pause": psycho_job.pause,
                   "resume": psycho_job.resume}.get(action)
        if handler is None:
            return {"state": "idle", "running": False, "active": False,
                    "error": f"Unknown profile action `{action}`. Use start, "
                             "pause or resume."}
        with self.lock:
            if self.dry_run:
                return {"state": "idle", "running": False, "active": False,
                        "dry_run": True,
                        "note": f"Dry run: nothing to {action}."}
            return handler(self.root)

    # -- the library index: rebuilding it from here ------------------------

    def _note_index_state(self, state: str) -> None:
        """Reload the catalogue once an index job has stopped for good."""
        previous = str((getattr(self, "index_job", None) or {}).get("state") or "")
        active = {"running", "paused", "cancelling"}
        if previous in active and state not in active:
            try:
                self.engine.catalog.reload()
            except Exception:                 # a reload must never fail a poll
                pass
        self.index_job = {**(getattr(self, "index_job", None) or {}),
                          "state": state}

    def _index_summary(self) -> dict:
        """A small note for the page: is the library being indexed right now?"""
        if not index_job.available(self.root):
            return {"state": "idle", "running": False, "active": False,
                    "paused": False, "percent": 0.0,
                    "available": False, "counts": index_job.counts(self.root)}
        status = index_job.status(self.root)
        state = str(status.get("state") or "idle")
        self._note_index_state(state)
        return {
            "state": state,
            "running": state == "running",
            "active": bool(status.get("active")),
            "paused": state == "paused",
            "percent": float(status.get("percent") or 0.0),
            "step": str(status.get("step") or ""),
            "phase": str(status.get("phase") or ""),
            "done": int(status.get("done") or 0),
            "total": int(status.get("total") or 0),
            "finished": str(status.get("finished") or ""),
            "elapsed": float(status.get("elapsed") or 0.0),
            "available": True,
            "counts": status.get("counts") or index_job.counts(self.root),
        }

    def index_update_status(self) -> dict:
        with self.lock:
            status = index_job.status(self.root)
            self._note_index_state(str(status.get("state") or "idle"))
            return status

    def start_index(self) -> dict:
        with self.lock:
            if self.dry_run:
                return {"state": "idle", "running": False, "dry_run": True,
                        "note": "Dry run: the index is not rebuilt.",
                        "available": index_job.available(self.root),
                        "counts": index_job.counts(self.root)}
            if not index_job.available(self.root):
                return {"state": "idle", "running": False, "available": False,
                        "error": "This library has no "
                                 "scripts/phase0_inventory.py, so the app "
                                 "cannot index it."}
            job = index_job.start(self.root)
            status = index_job.status(self.root, str(job.get("id") or ""))
            self.index_job = job
            status["started_now"] = True
            return status

    def control_index(self, action: str) -> dict:
        """Pause, resume or cancel the running index job."""
        action = str(action or "").strip().lower()
        handler = {"pause": index_job.pause, "resume": index_job.resume,
                   "cancel": index_job.cancel}.get(action)
        with self.lock:
            if handler is None:
                return {"state": "idle", "running": False, "active": False,
                        "error": f"Unknown index action `{action}`. Use pause, "
                                 "resume or cancel."}
            if self.dry_run:
                return {"state": "idle", "running": False, "active": False,
                        "dry_run": True,
                        "note": f"Dry run: nothing to {action}."}
            status = handler(self.root)
            self._note_index_state(str(status.get("state") or "idle"))
            return status

    # -- the library data: a daily plain-text backup ------------------------

    def _library_summary(self) -> dict:
        """A small note for the page: is the profile backed up?"""
        if not library_job.available(self.root):
            return {"state": "idle", "running": False, "active": False,
                    "paused": False, "available": False, "due": False,
                    "counts": {"available": False}}
        status = library_job.status(self.root)
        state = str(status.get("state") or "idle")
        return {
            "state": state,
            "running": state == "running",
            "active": bool(status.get("active")),
            "paused": state == "paused",
            "mode": str(status.get("mode") or ""),
            "percent": float(status.get("percent") or 0.0),
            "step": str(status.get("step") or ""),
            "due": bool(status.get("counts", {}).get("due")),
            "enabled": bool(self.engine.profile.setting("library_backup",
                                                        True)),
            "available": True,
            "counts": status.get("counts") or library_job.counts(self.root),
        }

    def library_update_status(self) -> dict:
        with self.lock:
            return library_job.status(self.root)

    def start_library_backup(self, keep: int = 0) -> dict:
        with self.lock:
            if self.dry_run:
                return {"state": "idle", "running": False, "dry_run": True,
                        "note": "Dry run: nothing is backed up.",
                        "available": library_job.available(self.root),
                        "counts": library_job.counts(self.root)}
            if not library_job.available(self.root):
                return {"state": "idle", "running": False, "available": False,
                        "error": "This library has no "
                                 "scripts/library_data.py, so the app cannot "
                                 "back its data up."}
            job = library_job.start(self.root, keep=keep or
                                    self._backup_keep())
            status = library_job.status(self.root, str(job.get("id") or ""))
            self.library_job = job
            status["started_now"] = True
            return status

    def control_library(self, action: str = "") -> dict:
        """Pause, continue or start the backup job."""
        action = str(action or "start").strip().lower()
        with self.lock:
            if self.dry_run:
                return {"state": "idle", "running": False, "active": False,
                        "dry_run": True,
                        "note": f"Dry run: nothing to {action}."}
            if action in {"start", "refresh", "backup"}:
                return self.start_library_backup()
            handler = {"pause": library_job.pause,
                       "resume": library_job.resume}.get(action)
            if handler is None:
                return {"state": "idle", "running": False, "active": False,
                        "error": f"Unknown backup action `{action}`. Use "
                                 "start, pause or resume."}
            return handler(self.root)

    def library_import(self, file: str, overwrite: bool = False) -> dict:
        """Queue an import of one export file (matched by fingerprint)."""
        with self.lock:
            if self.dry_run:
                return {"state": "idle", "running": False, "dry_run": True,
                        "note": "Dry run: nothing is imported."}
            try:
                job = library_job.start_import(self.root, str(file),
                                               overwrite=bool(overwrite))
            except (OSError, RuntimeError, ValueError) as exc:
                return {"state": "idle", "running": False, "error": str(exc)}
            return library_job.status(self.root, str(job.get("id") or ""))

    def library_preview(self, file: str) -> dict:
        """A dry-run import: what a file would fill in, without writing."""
        return library_job.preview(self.root, str(file))

    # -- which folder the app works on -------------------------------------

    def _saved_library(self) -> str:
        return str(app_settings(
            getattr(self, "start_root", self.root)).get("library") or "")

    def set_library(self, value: str) -> dict:
        """Switch to another music folder, or remember it for the next start.

        The container mounts exactly one folder; everything below it is
        readable and nothing outside it is.  So a folder this process cannot
        see is saved as the preference the installer picks up on the next
        start, and a folder it *can* see is opened immediately.
        """
        raw = str(value or "").strip()
        if not raw:
            return {"ok": False,
                    "error": "Give a folder, for example `/home/you/Musik`."}
        target = Path(raw).expanduser()
        if not target.is_absolute():
            return {"ok": False,
                    "error": "Use an absolute path — a relative one means "
                             "nothing to the app."}
        if not target.is_dir():
            return {"ok": False,
                    "error": f"`{raw}` is not a folder this app can read.",
                    "hint": "The app (and the container) sees only the folder "
                            "it was given and everything below it."}
        resolved = target.resolve()
        current = Path(self.root).resolve()
        if not looks_like_library(resolved):
            return {"ok": False,
                    "error": f"`{raw}` has no `Playlists/` and no "
                             f"`library-work/`, so it is not a music library "
                             f"root.",
                    "hint": "Choose the folder that holds Artists/ and "
                            "Playlists/."}
        start = Path(getattr(self, "start_root", self.root)).resolve()
        # the launcher reads this file in the folder it would start on
        # anyway; naming that very folder means "nothing to remember"
        saved = save_app_settings(
            start, library=None if resolved == start else str(resolved))
        if resolved == current:
            return {"ok": True, "applied": False, "library": str(current),
                    "note": "That is the folder already in use."}
        if self.dry_run:
            return {"ok": True, "applied": False, "library": str(resolved),
                    "note": "Dry run: saved, but the folder was not opened."}
        with self.lock:
            old = self.engine
            try:
                old.player.stop()
            except Exception:            # no player or no daemon: not fatal
                pass
            try:
                fresh = Engine(root=resolved, use_llm=self.use_llm,
                               dry_run=self.dry_run)
            except Exception as exc:
                return {"ok": False, "library": str(current),
                        "error": f"Could not open `{raw}`: {exc}"}
            try:
                old.close()
            except Exception:
                pass
            self.engine = fresh
            self.root = fresh.root
            self.diagnostics.set_root(self.root)
        return {"ok": True, "applied": True, "library": str(self.root),
                "saved": saved}

    def transport(self, action: str, value=None) -> dict:
        allowed = {"pause", "resume", "toggle", "stop", "next", "prev",
                   "volume", "volume_delta", "seek", "seek_abs", "restart",
                   "mute", "speed", "shuffle", "shuffle_on", "shuffle_off",
                   "repeat", "position", "status"}
        if action not in allowed:
            return {"ok": False, "text": f"unknown action: {action}",
                    "error": True}
        args: dict = {"action": action}
        if value is not None:
            text = str(value).strip()
            try:
                args["value"] = float(text)
            except (TypeError, ValueError):
                args["value"] = text        # "on"/"off"/"one"/"all" stay words
        acquired = self.lock.acquire(timeout=0.08)
        if acquired:
            try:
                reply = self.engine.skill_transport(args, "window button")
            finally:
                self.lock.release()
        else:
            # the assistant is in the middle of a turn (a model answer holds
            # the lock for a second or more).  A playback key must not wait
            # that out: talk to mpv directly, and let the next status poll
            # bring the context and the saved session up to date.
            fast = self.engine.fast_transport(args)
            if fast is None:
                with self.lock:
                    reply = self.engine.skill_transport(args, "window button")
            else:
                reply = fast
        return {"ok": not reply.error, "text": reply.text,
                "html": md_to_html(reply.text), "error": reply.error}

    def play(self, target: str, shuffle: bool | None = None) -> dict:
        args: dict = {"target": target}
        if shuffle:
            args["shuffle"] = True
        with self.lock:
            reply = self.engine.skill_play(args, f"play {target}")
            return _reply(reply)

    # -- continue where you left off --------------------------------------

    def resume(self) -> dict:
        """Put the remembered queue back and seek to where it stopped."""
        with self.lock:
            reply = self.engine.resume_last()
            return _reply(reply)

    def queue(self, limit: int = 200) -> dict:
        """The current entry and what follows, for the Up-next panel."""
        with self.lock:
            try:
                return self.engine.queue_view(int(limit or 200))
            except (ValueError, TypeError):
                return self.engine.queue_view(200)

    def queue_action(self, action: str, index=None, name: str = "",
                     overwrite: bool = False) -> dict:
        """play|remove|forget|save|discard from the Up-next panel."""
        action = str(action or "").casefold()
        if action in {"save", "discard"}:
            with self.lock:
                if action == "save":
                    reply = self.engine.skill_save_playlist(
                        {"own": True, "name": str(name or "").strip(),
                         "overwrite": bool(overwrite)}, "window button")
                else:
                    reply = self.engine.discard_own_playlist()
                out = _reply(reply)
                out["ok"] = not reply.error
                out["queue"] = self.engine.queue_view(200)
                return out
        if action == "forget":
            with self.lock:
                removed = self.engine.forget_session()
            return {"ok": True, "forgotten": bool(removed)}
        try:
            number = int(index or 0)
        except (TypeError, ValueError):
            number = 0
        if action not in {"play", "remove"}:
            return {"ok": False, "error": True,
                    "text": f"unknown queue action: {action}"}
        with self.lock:
            reply = (self.engine.queue_jump(number) if action == "play"
                     else self.engine.queue_remove(number))
            out = _reply(reply)
            out["ok"] = not reply.error
            return out

    def feedback(self, kind: str) -> dict:
        if kind not in {"like", "dislike", "note"}:
            return {"html": "<p>Unknown feedback.</p>", "text": "", "error": True}
        with self.lock:
            reply = self.engine.skill_learn({"kind": kind}, "window button")
            return _reply(reply)

    def quit(self) -> None:
        with self.lock:
            try:
                self.engine.close()
            except Exception:
                pass

    # -- youtube import ---------------------------------------------------

    def start_import(self, url: str, artist: str = "", album: str = "",
                     fmt: str = "best", genre: str = "") -> dict:
        """Queue one import per link; the first one starts at once.

        Several links may arrive at once (one per line, or separated by
        spaces), because pasting a playlist of videos is the common case.  A
        link that is already waiting or running is not queued twice.
        """
        urls = _urls(url)
        if not urls:
            raise ValueError("That does not look like an http(s) link.")
        known = {str(record.get("url") or "")
                 for record in youtube.jobs(self.root)
                 if record.get("status") in {"running", "queued"}}
        fresh = [item for item in urls if item not in known]
        skipped = [item for item in urls if item in known]
        if not fresh:
            raise ValueError("That link is already in the download queue.")
        candidates = []
        for item in fresh:
            # one metadata call keeps the job record readable and the artist
            # real; a link the fetcher cannot read still gets queued and reports
            candidates.append(youtube.probe(item, timeout=30) or {"url": item})
        try:
            started = youtube.start(
                self.root, candidates, artist=artist.strip(),
                album=album.strip(), fmt=fmt or "best",
                genre=genre.strip())
        except (OSError, RuntimeError) as exc:
            raise ValueError(str(exc))
        if not started:
            raise ValueError("The import could not be queued.")
        ids = [str(record.get("id") or "") for record in started]
        return {"id": ids[0], "ids": ids, "skipped": skipped,
                "count": len(ids),
                "message": (f"Queued {len(ids)} import"
                            f"{'s' if len(ids) != 1 else ''}."
                            + (f" {len(skipped)} already queued."
                               if skipped else ""))}

    def preview_import(self, url: str) -> dict:
        """What the importer would call this link, before anything downloads."""
        url = (url or "").strip()
        if not re.match(r"^https?://\S+$", url):
            return {"error": "That does not look like an http(s) link."}
        if not youtube.available(self.root):
            return {"error": "This build has no download module installed."}
        found = youtube.probe(url, timeout=30) or {"url": url}
        artist, title = youtube.guess_metadata(found)
        return {
            "url": str(found.get("url") or url),
            "title": str(found.get("title") or title or ""),
            "channel": str(found.get("channel") or ""),
            "duration_s": int(found.get("duration_s") or 0),
            "artist": artist, "guessed_title": title,
            "probed": bool(found.get("title") or found.get("id")),
        }

    def import_job(self, job_id: str) -> dict | None:
        with self.lock:
            records = self.engine.youtube_jobs()
        for record in records:
            if str(record.get("id") or "") != job_id:
                continue
            status = str(record.get("status") or "unknown")
            return dict(
                record,
                status=status,
                code=(0 if status in {"done", "partial", "duplicate"}
                      else 1 if status == "failed" else None),
                log=youtube.log_tail(self.root, record),
                started=record.get("created") or "",
            )
        return None

    def youtube_jobs(self) -> dict:
        with self.lock:
            records = self.engine.youtube_jobs()
        waiting_jobs = youtube._queued(self.root, records)
        positions = {str(record.get("id") or ""): index
                     for index, record in enumerate(waiting_jobs, 1)}
        jobs = []
        for record in records:
            status = str(record.get("status") or "unknown")
            waiting = (status in youtube.ATTENTION
                       and not record.get("dismissed"))
            jobs.append({
                "id": str(record.get("id") or ""),
                "label": str(record.get("display")
                             or record.get("url") or "import"),
                "channel": str(record.get("channel") or ""),
                "url": str(record.get("url") or ""),
                "artist": str(record.get("artist") or ""),
                "title": str(record.get("title") or ""),
                "album": str(record.get("album") or ""),
                "genre": str(record.get("genre") or ""),
                "track": str(record.get("track") or ""),
                "summary": str(record.get("summary") or ""),
                "created": str(record.get("created") or ""),
                "ago": self.engine._ago(str(record.get("created") or "")),
                "status": status,
                "state": youtube.status_line(record),
                # what the downloads panel needs to offer the right buttons
                "filed": str(record.get("filed") or ""),
                "duplicate": str(record.get("duplicate") or ""),
                "integrity": str(record.get("integrity") or ""),
                "retries": len(record.get("attempts") or []),
                "position": positions.get(str(record.get("id") or ""), 0),
                "queued": len(waiting_jobs),
                "cancelled": status == "cancelled",
                "movable": str(record.get("id") or "") in positions,
                "cancellable": status in {"running", "queued"},
                "retry": bool(waiting),
                "dismissible": bool(waiting),
                "dismissed": bool(record.get("dismissed")),
                "active": status in {"running", "queued"},
            })
        return {
            "jobs": jobs,
            "count": len(jobs),
            "pending": sum(1 for job in jobs if job["active"]),
            "failed": sum(1 for job in jobs if job["retry"]),
            "filed": sum(1 for job in jobs if job["status"] == "done"),
            "duplicate": sum(1 for job in jobs
                             if job["status"] == "duplicate"),
            "cancelled": sum(1 for job in jobs if job["status"] == "cancelled"),
            "queued": len(waiting_jobs),
        }

    # -- psychoacoustic profile -------------------------------------------

    def _psycho_track(self, rel: str):
        """The requested track, else whatever is playing / last selected.

        A ``rel`` that does not resolve is *not* replaced by the playing
        track: the window would then show a different song than the one the
        user picked, which is worse than an honest error.
        """
        rel = str(rel or "").strip()
        if rel:
            return self.engine.catalog.track(rel)
        return self.engine._playing_track()

    def _psycho_axes(self, set_name: str = "psychology") -> list[dict]:
        from .psycho import set_axes
        return [dict(axis) for axis in set_axes(set_name)]

    def _psycho_set(self, value: str | None) -> str:
        """The lens to use: what the caller asked for, else the remembered one."""
        from .psycho import set_key
        if value:
            return set_key(value)
        return set_key(self.engine.profile.setting("psycho_set",
                                                    "psychology"))

    def psycho(self, rel: str = "", set_name: str | None = None) -> dict:
        """"The properties of one song" in the selected profile set."""
        set_name = self._psycho_set(set_name)
        with self.lock:
            track = self._psycho_track(rel)
            sets = self.engine.psycho.sets() if self.engine.psycho.available() else []
            if track is None:
                return {"error": "no track selected", "set": set_name,
                        "sets": sets, "axes": self._psycho_axes(set_name)}
            record = self.engine.psycho.describe(track.rel, set_name)
            if record is not None and not any(
                    value is not None
                    for value in (record.get("axes") or {}).values()):
                # an audio row whose decode failed (every measurement NULL)
                # would draw an empty net with no explanation — treat it as
                # "no profile", so the panel names the gap and its reason
                record = None
            from .psycho import RAW, SETS
            state = self.engine.track_state(track.rel, record)
            if record is None and state.get("profile") and set_name != "psychology":
                # the psychology profile exists but this lens has no row yet
                # (an older database): say that instead of a bare "no profile"
                label = next((item["label"] for item in SETS
                              if item["key"] == set_name), set_name)
                state = dict(state)
                state["summary"] = (f"The {label} view has not been built for "
                                    f"this track yet. "
                                    + state.get("summary", "")).strip()
            if record is None:
                # whether a measurement could fill this gap, and why not
                hint = self.engine.psycho.measure_hint(track.rel)
                return {
                    "rel": track.rel, "title": track.name,
                    "artist": track.who, "album": track.album,
                    "set": set_name, "sets": sets,
                    "axes": self._psycho_axes(set_name), "values": {},
                    "measured": {}, "raw": list(RAW),
                    "family": None, "families": [], "estimated": True,
                    "available": self.engine.psycho.available(),
                    "error": "no profile for this track yet",
                    # what *this* file still lacks, so the panel can say that
                    # instead of only quoting the library-wide counts
                    "track_state": state,
                    # the panel offers "Measure this track" only when a run
                    # can really produce a profile (a broken rip cannot)
                    "can_measure": bool(hint.get("measurable")),
                    "measure_reason": str(hint.get("reason") or ""),
                    "loudness": loudness.describe(self.engine.root, track.rel),
                    "fade": fade.describe(self.engine.profile),
                }
            families = self.engine.psycho.families()
            return {
                "rel": track.rel,
                "title": track.name,
                "artist": track.who,
                "album": track.album,
                "grouping": track.grouping,
                "duration": _clock(track.duration_s),
                "set": set_name,
                "sets": sets,
                "axes": self._psycho_axes(set_name),
                "values": record.get("axes") or {},
                "measured": record.get("measured") or {},
                "raw": list(RAW),
                "family": record.get("family"),
                "family_distance": record.get("family_distance"),
                "estimated": bool(record.get("estimated")),
                "families": families,
                "available": True,
                "track_state": state,
                "loudness": loudness.describe(self.engine.root, track.rel),
                "fade": fade.describe(self.engine.profile),
            }

    def psycho_explain(self, rel: str = "", set_name: str | None = None) -> dict:
        """Why an axis holds its value: the measurements behind it, read-only."""
        set_name = self._psycho_set(set_name)
        with self.lock:
            track = self._psycho_track(rel)
            if track is None:
                return {"error": "no track selected", "set": set_name}
            data = self.engine.psycho.explain(track.rel, set_name=set_name)
            if data is None:
                return {"rel": track.rel, "set": set_name,
                        "error": "no profile for this track yet",
                        # the same per-track sentence as the panel note, so the
                        # "why" box does not answer with a bare machine reason
                        "track_state": self.engine.track_state(track.rel)}
            data["title"] = track.name
            data["artist"] = track.who
            return data

    def psycho_similar(self, rel: str = "", axes: dict | None = None,
                       adjustments: dict | None = None,
                       limit: int = 12, set_name: str | None = None) -> dict:
        """Tracks that fit a profile after the sliders were moved."""
        set_name = self._psycho_set(set_name)
        from .psycho import set_keys
        allowed = set_keys(set_name)
        with self.lock:
            track = self._psycho_track(rel)
            base: dict[str, float] = {}
            for key, value in (axes or {}).items():
                if str(key) not in allowed:
                    continue
                try:
                    base[str(key)] = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    continue
            moved: dict[str, float] = {}
            for key, value in (adjustments or {}).items():
                if str(key) not in allowed:
                    continue
                try:
                    moved[str(key)] = max(-1.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    continue
            try:
                limit = max(1, min(60, int(limit)))
            except (TypeError, ValueError):
                limit = 12
            hits = self.engine.psycho.similar(
                track.rel if track is not None else None,
                axes=base or None, adjustments=moved or None, limit=limit,
                set_name=set_name)
            out = []
            for hit in hits:
                other = self.engine.catalog.track(hit["rel"])
                if other is None:
                    continue
                out.append({
                    "rel": other.rel,
                    "title": other.name,
                    "artist": other.who,
                    "album": other.album,
                    "duration": _clock(other.duration_s),
                    "distance": hit["distance"],
                    "family": hit.get("family", ""),
                    "axes": hit.get("axes") or {},
                })
            return {"target": track.rel if track is not None else "",
                    "set": set_name,
                    "adjustments": moved, "count": len(out), "hits": out}

    def search(self, query: str, limit: int = 40) -> dict:
        query = " ".join(str(query or "").split())
        try:
            limit = max(1, min(80, int(limit)))
        except (TypeError, ValueError):
            limit = 40
        if not query:
            return {"query": "", "count": 0, "hits": [], "lyrics": [],
                    "playlists": []}
        tracks = self.engine.catalog.search(query, limit)
        hits = [{
            "rel": track.rel,
            "title": track.name,
            "artist": track.artist,
            "album": track.album,
            "genre": track.genre,
            "grouping": track.grouping,
            "duration": _clock(track.duration_s),
            "path": track.rel,
        } for track in tracks]
        # one search box for the whole library: a song that *sings* the word
        # is a hit too, with the line and the moment it is sung
        known = {hit["rel"] for hit in hits}
        lyric_hits: list[dict] = []
        try:
            found = self.engine.lyrics.find(query, limit=10)
        except Exception:                       # a broken database is no result
            found = []
        for hit in found:
            if hit["rel"] in known or len(lyric_hits) >= 8:
                continue
            known.add(hit["rel"])
            lyric_hits.append({
                "rel": hit["rel"],
                "title": hit["title"] or Path(hit["rel"]).stem,
                "artist": hit["artist"],
                "album": hit["album"],
                "line": hit["line"],
                "start": round(float(hit.get("start") or 0), 2),
                "matched": hit.get("matched") or [],
                "also": int(hit.get("also") or 0),
                "duration": _clock(float(hit.get("duration") or 0)),
                "path": hit["rel"],
            })
        # a playlist is a hit too, and clicking it hands the whole list to the
        # player — the same thing the sidebar and "play Bands" do
        playlists = [{
            "name": playlist.name,
            "path": str(playlist.path),
            "kind": playlist.kind,
            "count": playlist.count,
        } for playlist in self.engine.catalog.search_playlists(query)]
        return {"query": query, "count": len(hits), "hits": hits,
                "lyrics": lyric_hits, "playlists": playlists}


def _reply(reply) -> dict:
    return {
        "text": reply.text,
        "html": md_to_html(reply.text),
        "tool": reply.tool,
        "planner": getattr(reply, "planner", "") or "",
        "error": bool(reply.error),
        "clear": bool(reply.clear),
        "quit": bool(reply.quit),
        "played": bool(reply.played),
        "tracks": [
            {"artist": track.artist, "title": track.name, "rel": track.rel,
             "duration": _clock(track.duration_s)}
            for track in reply.tracks[:40]
        ],
    }


def _clock(seconds: float) -> str:
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    return f"{seconds // 60}:{seconds % 60:02d}"


def _art_kind(root, rel: str) -> str:
    """``embedded`` / ``folder`` / ``none`` from the inventory row, not a read.

    The status poll runs every two seconds, so it must not open the audio file;
    the row the scan already wrote answers "should there be a cover here".  A
    mismatch with `/api/cover` is exactly what the page reports, which is how a
    broken reader stops looking like a track without art.
    """
    if not rel:
        return "none"
    try:
        info = tagedit.row(root, rel)
    except Exception:                                 # noqa: BLE001
        return "none"
    if bool(info.get("has_embedded_art")) or int(info.get("art_count") or 0) > 0:
        return "embedded"
    if int(info.get("folder_image_count") or 0) > 0 or info.get("folder_image_names"):
        return "folder"
    return "none"


# ---------------------------------------------------------------------------
# HTTP


class Handler(BaseHTTPRequestHandler):
    service: ChatService
    token: str
    server_version = f"JulAInMusicPlayer/{version.app_version()}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        pass

    # -- guard ------------------------------------------------------------
    #
    # A route that raises used to end the connection with no response at all;
    # WebKit then tells the user "Load failed", which names neither the
    # endpoint nor the cause.  Every request now goes through _guard(), so a
    # failure becomes a JSON 500 with the exception text, is counted, and is
    # written to the error log.

    def do_GET(self) -> None:       # noqa: N802
        self._guard(self._get)

    def do_HEAD(self) -> None:      # noqa: N802
        self._guard(self._get)

    def do_POST(self) -> None:      # noqa: N802
        self._guard(self._post)

    def do_PUT(self) -> None:       # noqa: N802
        self._guard(lambda: self._json({"error": "not found"}, 404))

    def do_DELETE(self) -> None:    # noqa: N802
        self._guard(lambda: self._json({"error": "not found"}, 404))

    def _guard(self, route) -> None:
        started = time.perf_counter()
        self._status_sent = 0
        self._body_seen = self.command != "POST"
        error = ""
        try:
            route()
        except (BrokenPipeError, ConnectionResetError) as exc:
            # the page navigated away or was closed mid-answer: nothing to
            # reply to, but worth a line because it looks like a failure
            error = f"{type(exc).__name__}: {exc} (client went away)"
            self.close_connection = True
        except Exception as exc:                  # noqa: BLE001 - last resort
            error = "".join(traceback.format_exception(exc))
            if not self._status_sent:
                self._fail(exc)
            self.close_connection = True
        finally:
            try:
                self.service.diagnostics.record(
                    self.command, _clean_path(self.path),
                    self._status_sent, time.perf_counter() - started, error)
            except Exception:                     # noqa: BLE001 - never fail
                pass

    def _fail(self, exc: Exception) -> None:
        """Turn an unhandled exception into an answer the page can show."""
        log = ""
        try:
            log = str(self.service.diagnostics.error_log or "")
        except Exception:                         # noqa: BLE001
            pass
        payload = {
            "error": f"{type(exc).__name__}: {exc}"[:600],
            "kind": "internal",
            "where": _clean_path(self.path),
            "log": log,
        }
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(500, body, "application/json; charset=utf-8")
        except Exception:                         # noqa: BLE001
            self.close_connection = True

    # -- helpers ----------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self._status_sent = code
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if not self._body_seen:
            # the POST body was never read; leaving it in the socket would make
            # the next request on this keep-alive connection parse as garbage
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: dict, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _token(self) -> str:
        header = self.headers.get(TOKEN_HEADER) or ""
        if header:
            return header
        query = parse_qs(urlparse(self.path).query)
        return (query.get("t") or [""])[0]

    def _authorised(self) -> bool:
        return hmac.compare_digest(self._token(), self.token)

    def _body(self, limit: int = MAX_BODY) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._body_seen = True
            return {}
        if length > limit:
            raise ValueError("request too large")
        raw = self.rfile.read(length)
        self._body_seen = True
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def _raw(self, limit: int) -> bytes:
        """Read a binary body (a recorded clip) with an explicit size cap."""
        length = int(self.headers.get("Content-Length") or 0)
        self._body_seen = True
        if length <= 0:
            return b""
        if length > limit:
            raise ValueError(f"request too large ({length} bytes; at most "
                             f"{limit} are accepted)")
        return self.rfile.read(length)

    # -- GET --------------------------------------------------------------

    def _get(self) -> None:
        self.service.last_request = time.time()
        parsed = urlparse(self.path)
        path = parsed.path

        if path in {"/", "/index.html"}:
            if not self._authorised():
                self._json({"error": "forbidden"}, 403)
                return
            self._index()
            return

        if path.startswith("/static/"):
            self._static(path)
            return

        if path == "/sw.js":
            # A service worker script cannot carry a query string, so this one
            # route is public like /static/.  It caches the shell assets and
            # nothing else — no page, no API answer — so there is no data in it
            # that a token protects.
            self._worker()
            return

        if not self._authorised():
            self._json({"error": "forbidden"}, 403)
            return

        if path == "/manifest.webmanifest":
            self._json(pwa_manifest(self.token))
            return

        if path == "/api/state":
            self._json(self.service.state())
            return
        if path == "/api/diagnostics":
            # "Load failed" has no other explanation: this says how many
            # requests failed, what the last one was and where the log is
            snapshot = self.service.diagnostics.snapshot()
            snapshot["artwork"] = self.service.artwork_reader()
            self._json(snapshot)
            return
        if path == "/api/settings":
            self._json(self.service.settings_view())
            return
        if path == "/api/voice":
            self._json(self.service.voice_status())
            return
        if path == "/api/status":
            self._json(self.service.status())
            return
        if path == "/api/activity":
            query = parse_qs(parsed.query)
            raw = (query.get("limit") or ["40"])[0]
            try:
                limit = int(raw)
            except ValueError:
                limit = 40
            self._json(self.service.activity(limit))
            return
        if path == "/api/profile":
            query = parse_qs(parsed.query)
            raw = (query.get("limit") or ["40"])[0]
            try:
                limit = int(raw)
            except ValueError:
                limit = 40
            self._json(self.service.profile_page(limit))
            return
        if path == "/api/recent":
            query = parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or ["30"])[0])
            except ValueError:
                limit = 30
            self._json(self.service.recent_changes(limit))
            return
        if path == "/api/health":
            self._json(self.service.library_health())
            return
        if path == "/api/duplicates":
            self._json(self.service.library_duplicates())
            return
        if path == "/api/stats":
            self._json(self.service.library_stats())
            return
        if path == "/api/history":
            query = parse_qs(parsed.query)
            raw = (query.get("days") or [""])[0].strip()
            try:
                days = int(raw) if raw and raw != "0" else None
            except ValueError:
                days = None
            try:
                limit = int((query.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            forgotten = (query.get("forgotten_days") or [""])[0].strip()
            try:
                forgotten_days = int(forgotten) if forgotten else 365
            except ValueError:
                forgotten_days = 365
            self._json(self.service.play_history(
                days=days, limit=max(1, min(200, limit)),
                forgotten_days=forgotten_days))
            return
        if path == "/api/radio":
            self._json(self.service.radio_state())
            return
        if path == "/api/endless":
            self._json(self.service.endless_state())
            return
        if path == "/api/tags":
            query = parse_qs(parsed.query)
            rel = (query.get("rel") or [""])[0]
            if len(rel) > 500:
                self._json({"error": "rel too long"}, 400)
                return
            self._json(self.service.tags(rel))
            return
        if path == "/api/cover":
            query = parse_qs(parsed.query)
            rel = (query.get("rel") or [""])[0]
            if not rel or len(rel) > 500:
                self._json({"error": "rel required"}, 400)
                return
            found = self.service.cover(rel)
            if found.get("data"):
                self._send(200, found["data"], found.get("mime") or "image/jpeg")
                return
            if found.get("error"):
                # The track has (or should have) a picture but it could not be
                # read: mutagen missing, an unreadable image.  This is a real
                # failure, so it is reported and logged instead of looking like
                # a track without art.
                self.service.diagnostics.note(found["error"], f"/api/cover?rel={rel}")
                self._json({"error": found["error"],
                            "hint": ("a Debian install needs python3-mutagen; "
                                     "the window's Diagnostics tab names the "
                                     "interpreter it runs on")}, 500)
                return
            # no artwork is not an error the page shows: the strip keeps
            # its music-note icon, which is most of the chiptune shelves
            self._json({"error": "no cover art"}, 404)
            return
        if path == "/api/waveform":
            query = parse_qs(parsed.query)
            rel = (query.get("rel") or [""])[0]
            if not rel or len(rel) > 500:
                self._json({"error": "rel required"}, 400)
                return
            self._json(self.service.waveform(rel))
            return
        if path == "/api/spectrum":
            query = parse_qs(parsed.query)
            rel = (query.get("rel") or [""])[0]
            if not rel or len(rel) > 500:
                self._json({"error": "rel required"}, 400)
                return
            self._json(self.service.spectrum(rel))
            return
        if path == "/api/search":
            query = parse_qs(parsed.query)
            text = (query.get("q") or [""])[0]
            raw = (query.get("limit") or ["40"])[0]
            try:
                limit = int(raw)
            except ValueError:
                limit = 40
            self._json(self.service.search(text, limit))
            return
        if path == "/api/recent-tracks":
            query = parse_qs(parsed.query)
            raw = (query.get("limit") or ["30"])[0]
            try:
                limit = max(1, min(200, int(raw)))
            except ValueError:
                limit = 30
            span = (query.get("days") or [""])[0]
            try:
                days = max(1, int(span)) if span else None
            except ValueError:
                days = None
            self._json(self.service.library_arrivals(limit=limit, days=days))
            return
        if path == "/api/queue":
            query = parse_qs(parsed.query)
            raw = (query.get("limit") or ["200"])[0]
            try:
                limit = int(raw)
            except ValueError:
                limit = 200
            self._json(self.service.queue(limit))
            return
        if path == "/api/psycho":
            query = parse_qs(parsed.query)
            rel = (query.get("rel") or [""])[0]
            set_name = (query.get("set") or [""])[0]
            if len(rel) > 500:
                self._json({"error": "rel too long"}, 400)
                return
            self._json(self.service.psycho(rel, set_name))
            return
        if path == "/api/psycho/explain":
            query = parse_qs(parsed.query)
            rel = (query.get("rel") or [""])[0]
            set_name = (query.get("set") or [""])[0]
            if len(rel) > 500:
                self._json({"error": "rel too long"}, 400)
                return
            self._json(self.service.psycho_explain(rel, set_name))
            return
        if path == "/api/psycho/update":
            self._json(self.service.psycho_update_status())
            return
        if path == "/api/index/update":
            self._json(self.service.index_update_status())
            return
        if path == "/api/lyrics":
            query = parse_qs(parsed.query)
            rel = (query.get("rel") or [""])[0]
            if len(rel) > 500:
                self._json({"error": "rel too long"}, 400)
                return
            self._json(self.service.lyrics(rel))
            return
        if path == "/api/lyrics/update":
            self._json(self.service.lyrics_status())
            return
        if path == "/api/work":
            self._json(self.service.work())
            return
        if path == "/api/library-data":
            self._json(self.service.library_update_status())
            return
        if path == "/api/youtube":
            self._json(self.service.youtube_jobs())
            return
        if path == "/api/ping":
            self._json({"ok": True, "pid": os.getpid(),
                        "root": str(self.service.root)})
            return
        if path == "/api/restart":
            # read-only: what a restart would change, and whether this process
            # can do it.  POST below is the one that acts.
            self._json(restart_status())
            return
        if path.startswith("/api/import/"):
            job = self.service.import_job(path.rsplit("/", 1)[-1])
            if job is None:
                self._json({"error": "unknown job"}, 404)
            else:
                self._json(job)
            return
        self._json({"error": "not found"}, 404)

    def _index(self) -> None:
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        page = page.replace("__TOKEN__", self.token)
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _static(self, path: str) -> None:
        name = path[len("/static/"):]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name or ""):
            self._json({"error": "not found"}, 404)
            return
        target = STATIC_DIR / name
        if not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        ctype = {"css": "text/css; charset=utf-8",
                 "js": "text/javascript; charset=utf-8",
                 "svg": "image/svg+xml",
                 "png": "image/png"}.get(target.suffix.lstrip("."),
                                         "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _worker(self) -> None:
        target = STATIC_DIR / "sw.js"
        if not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        # served from the root, so its default scope already covers the page
        self._send(200, target.read_bytes(),
                   "text/javascript; charset=utf-8")

    # -- POST -------------------------------------------------------------

    def _post(self) -> None:
        self.service.last_request = time.time()
        if not self._authorised():
            self._json({"error": "forbidden"}, 403)
            return
        path = urlparse(self.path).path
        if path == "/api/voice":
            try:
                payload = self._raw(voice.MAX_BYTES)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            if not payload:
                self._json({"error": "the recording was empty"}, 400)
                return
            self._json(self.service.voice(
                payload, self.headers.get("Content-Type") or ""))
            return
        if path == "/api/tags/artwork":
            # a cover is the one request that is not a tiny JSON message
            try:
                data = self._body(limit=9 * 1024 * 1024)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json(self.service.set_artwork(data))
            return
        try:
            data = self._body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, 400)
            return

        if path == "/api/chat":
            message = str(data.get("message") or "").strip()
            if not message:
                self._json({"error": "empty message"}, 400)
                return
            self._json(self.service.chat(message))
            return
        if path == "/api/diagnostics":
            action = str(data.get("action") or "clear").strip().lower()
            if action not in ("clear", "clean", "cleanup"):
                self._json({"error": f"unknown diagnostics action {action!r}"},
                           400)
                return
            self._json(self.service.clear_diagnostics())
            return
        if path == "/api/transport":
            self._json(self.service.transport(str(data.get("action") or ""),
                                              data.get("value")))
            return
        if path == "/api/play":
            target = str(data.get("target") or "").strip()
            if not target:
                self._json({"error": "what should I play?"}, 400)
                return
            self._json(self.service.play(target,
                                         bool(data.get("shuffle")) or None))
            return
        if path == "/api/resume":
            self._json(self.service.resume())
            return
        if path == "/api/queue":
            self._json(self.service.queue_action(
                str(data.get("action") or ""), data.get("index"),
                str(data.get("name") or ""), bool(data.get("overwrite"))))
            return
        if path == "/api/feedback":
            self._json(self.service.feedback(str(data.get("kind") or "")))
            return
        if path == "/api/psycho/similar":
            self._json(self.service.psycho_similar(
                str(data.get("rel") or ""),
                data.get("axes") if isinstance(data.get("axes"), dict) else None,
                data.get("adjustments") if isinstance(data.get("adjustments"), dict) else None,
                data.get("limit") or 12,
                str(data.get("set") or "")))
            return
        if path == "/api/psycho/update":
            action = str(data.get("action") or "").strip()
            if action in {"pause", "resume", "start"}:
                self._json(self.service.control_psycho(action))
                return
            try:
                refresh = bool(data.get("refresh"))
                workers = int(data.get("workers") or 0)
            except (TypeError, ValueError):
                self._json({"error": "workers must be a number"}, 400)
                return
            self._json(self.service.start_psycho_update(refresh, workers))
            return
        if path == "/api/index/update":
            action = str((data or {}).get("action") or "").strip()
            if action in {"pause", "resume", "cancel"}:
                self._json(self.service.control_index(action))
            else:
                self._json(self.service.start_index())
            return
        if path == "/api/work":
            self._json(self.service.control_work(
                str(data.get("job") or ""), str(data.get("action") or ""),
                str(data.get("id") or "")))
            return
        if path == "/api/library-data":
            action = str(data.get("action") or "").strip().lower()
            if action in {"preview", "check"}:
                self._json(self.service.library_preview(
                    str(data.get("file") or "")))
                return
            if action == "import":
                self._json(self.service.library_import(
                    str(data.get("file") or ""), bool(data.get("overwrite"))))
                return
            self._json(self.service.control_library(action or "start"))
            return
        if path == "/api/lyrics/update":
            self._json(self.service.control_lyrics(
                str(data.get("action") or "")))
            return
        if path == "/api/recent":
            self._json(self.service.undo_change(str(data.get("ts") or "")))
            return
        if path == "/api/radio":
            self._json(self.service.radio_action(
                str(data.get("action") or "status"),
                str(data.get("target") or "")))
            return
        if path == "/api/endless":
            self._json(self.service.endless_action(
                str(data.get("action") or "status")))
            return
        if path == "/api/shuffle":
            self._json(self.service.shuffle_action(bool(data.get("on"))))
            return
        if path == "/api/tags":
            self._json(self.service.set_tags(data))
            return
        if path == "/api/settings":
            self._json(self.service.settings(data))
            return
        if path == "/api/lyrics":
            action = str(data.get("action") or "offset").casefold()
            if action == "export":
                self._json(self.service.lyrics_export(
                    str(data.get("rel") or ""), str(data.get("path") or ""),
                    bool(data.get("overwrite"))))
                return
            if action in {"offset", "shift"}:
                value = data.get("value")
                self._json(self.service.lyrics_offset(
                    str(data.get("rel") or ""), value,
                    bool(data.get("relative"))))
                return
            if action in {"similar", "like"}:
                try:
                    limit = int(data.get("limit") or 12)
                except (TypeError, ValueError):
                    limit = 12
                self._json(self.service.lyrics_similar(
                    str(data.get("rel") or ""), limit))
                return
            self._json({"error": f"unknown lyrics action: {action}"}, 400)
            return
        if path == "/api/lyrics/transcribe":
            try:
                missing = int(data.get("missing") or 0)
            except (TypeError, ValueError):
                self._json({"error": "missing must be a number"}, 400)
                return
            self._json(self.service.start_lyrics(
                str(data.get("rel") or ""), bool(data.get("refresh")), missing))
            return
        if path == "/api/lyrics/setup":
            self._json(self.service.setup_lyrics(bool(data.get("cpu"))))
            return
        if path == "/api/import/preview":
            self._json(self.service.preview_import(
                str(data.get("url") or "")))
            return
        if path == "/api/import":
            try:
                queued = self.service.start_import(
                    str(data.get("url") or data.get("urls") or ""),
                    str(data.get("artist") or ""),
                    str(data.get("album") or ""), str(data.get("format") or "best"),
                    str(data.get("genre") or ""))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json(queued)
            return
        if path == "/api/downloads/tags":
            try:
                with self.service.lock:
                    result = youtube.update_tags(
                        self.service.root, str(data.get("id") or ""), data)
            except youtube.DownloadUnavailable as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json(result, 400 if result.get("error") else 200)
            return
        if path == "/api/downloads/forget":
            try:
                with self.service.lock:
                    result = youtube.forget(self.service.root,
                                            str(data.get("id") or ""))
            except youtube.DownloadUnavailable as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json(result, 400 if not result.get("removed") else 200)
            return
        if path == "/api/restart":
            state = restart_status()
            if not state["stale"]:
                self._json({"ok": True, "restarted": False,
                            "note": ("the running code is already the installed "
                                     f"one ({state['running']})")})
                return
            if not state["available"]:
                self._json({"error": state["reason"], "restartable": False,
                            "running": state["running"],
                            "disk": state["disk"]}, 409)
                return
            answer = {"ok": True, "restarted": True, "pid": os.getpid(),
                      "from": state["running"], "to": state["disk"]}
            self._json(answer)
            schedule_restart()
            return
        if path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self._json({"error": "not found"}, 404)


def pwa_manifest(token: str) -> dict:
    """The web app manifest, with this window's token as its start URL.

    The page is token-protected, so a static manifest could not name a URL that
    opens it.  The name, the icons and the standalone display are what make
    "add to home screen" produce a real app instead of a bookmark; the icons
    are served from /static/, which needs no token.
    """
    return {
        "id": "/",
        "name": "JulAIn Music Player",
        "short_name": "JMP",
        "description": ("Play, search and shape the library from one window: "
                        "conversational playlists, a sound profile, lyrics and "
                        "the import queue."),
        "start_url": f"/?t={token}",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0f1014",
        "theme_color": "#0f1014",
        "categories": ["music", "entertainment"],
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "/static/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
        ],
    }


def make_handler(service: ChatService, token: str):
    class Bound(Handler):
        pass
    Bound.service = service
    Bound.token = token
    return Bound


# ---------------------------------------------------------------------------
# lifecycle


def _state_path(root: Path) -> Path:
    return root / "library-work" / "app" / "chat" / "server.json"


def _live_server(root: Path) -> str | None:
    """The URL of a server already serving this library, if there is one."""
    path = _state_path(root)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = str(info.get("url") or "")
    pid = int(info.get("pid") or 0)
    if not url or not pid:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        path.unlink(missing_ok=True)
        return None
    ping = url.replace("/?t=", "/api/ping?t=")
    try:
        with urllib.request.urlopen(ping, timeout=1.5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return url if data.get("root") == str(root) else None


def _write_state(root: Path, url: str) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "url": url,
                          "root": str(root), "at": time.time()}, indent=1)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _clear_state(root: Path) -> None:
    try:
        info = json.loads(_state_path(root).read_text(encoding="utf-8"))
        if int(info.get("pid") or 0) == os.getpid():
            _state_path(root).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


# -- restarting in place ----------------------------------------------------
#
# An upgrade replaces files, not a running window: the old process keeps its
# old code in memory until it is replaced.  Instead of asking the listener to
# find and kill PIDs, the server re-executes itself when the page notices that
# the number on disk differs from the one it loaded.
#
# ``os.execv`` keeps the pid, so the port, the token and ``server.json`` all
# stay valid — the open window keeps polling the URL it already has and only
# has to reload once the new image answers.  mpv and the detached background
# jobs are separate processes, so playback and a running measurement survive.

#: how this server was started: {"argv", "port", "token"} — set by run_web
_RESTART: dict = {}


def restart_available() -> bool:
    """Whether this process may replace itself (only the real window may)."""
    return bool(_RESTART.get("argv"))


def restart_reason() -> str:
    if restart_available():
        return ""
    return ("this server was not started as `music chat --web`, so it cannot "
            "replace itself — start the app again")


def restart_status() -> dict:
    """What the window needs to decide: running release, installed one, can it?"""
    state = version.status()
    state["available"] = restart_available()
    state["reason"] = restart_reason()
    return state


def restart_argv() -> list[str]:
    """The command that starts this same server again, on the same port.

    The window is already open, so the copy starts with ``--no-open``; the port
    and the token are forced to the ones in use, because the page's URL carries
    both.  Any ``--port``/``--token``/``--open`` the command already had is
    dropped first, so a restart cannot accumulate them.
    """
    argv = [str(part) for part in _RESTART.get("argv") or []]
    if not argv:
        raise RuntimeError("this server cannot restart itself")
    out: list[str] = []
    skip = False
    for part in argv:
        if skip:
            skip = False
            continue
        if part in ("--port", "--token"):
            skip = True
            continue
        if part in ("--open", "--no-open"):
            continue
        out.append(part)
    out += ["--no-open", "--port", str(_RESTART["port"]),
            "--token", str(_RESTART["token"])]
    return [sys.executable, *out]


def schedule_restart(delay: float = 0.4, execute=None) -> None:
    """Replace this process with a freshly loaded copy of the same command.

    The delay lets the answer that announced the restart leave the socket
    first.  ``execute`` exists for the tests: with it, the command that would
    run can be inspected without ending the test process.
    """
    run = execute or os.execv
    argv = restart_argv()          # raise here, before anything is promised

    def _go() -> None:
        time.sleep(delay)
        try:
            run(argv[0], argv)
        except OSError:
            # nothing is left to answer with: a clean exit lets the next start
            # pick the new code up instead of leaving a half-dead server
            os._exit(1)

    threading.Thread(target=_go, name="chat-restart", daemon=True).start()



def _window_pythons() -> list[str]:
    """Interpreters worth trying for the native window, best first."""
    seen: set[str] = set()
    out: list[str] = []
    for cand in (sys.executable, "/usr/bin/python3", "/usr/local/bin/python3",
                 shutil.which("python3")):
        if not cand or cand in seen or not os.path.exists(cand):
            continue
        seen.add(cand)
        out.append(cand)
    return out


def _native_window(url: str) -> bool:
    """Show `url` in a GTK/WebKit window when the host provides one.

    The venv the app runs in normally has no PyGObject, so the system Python is
    tried as well; each candidate is asked ``--check`` first, which imports
    ``gi`` without building a window.  The first one that names a backend gets
    the URL.  Returns False when no toolkit is available, so the caller can fall
    back to a browser window.
    """
    script = Path(__file__).with_name("window.py")
    if not script.is_file() or not sys.platform.startswith("linux"):
        return False
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)  # would point a system python at the venv
    for python in _window_pythons():
        try:
            probe = subprocess.run([python, str(script), "--check"],
                                   capture_output=True, text=True, timeout=20,
                                   env=env)
        except (OSError, subprocess.SubprocessError):
            continue
        backend = (probe.stdout or "").strip()
        if probe.returncode != 0 or backend not in ("gtk3", "gtk4"):
            continue
        try:
            proc = subprocess.Popen(
                [python, str(script), "--backend", backend, url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=env)
        except OSError:
            continue
        # A missing display/typelib fails within a moment; a window that is
        # still running after this is a window the user can see.
        for _ in range(12):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            return True
    return False


def _media_agent(url: str) -> None:
    """Start the host MPRIS agent next to a browser window, when possible.

    The native window owns `org.mpris.MediaPlayer2.jmp` while it is open.  A
    browser window does not, and a closed window takes the name with it, so
    the desktop's own media keys would have nothing to talk to.  The agent
    keeps the name on the *host* session bus; in a server container it finds
    no bus (or no PyGObject) and exits on its own.
    """
    if os.environ.get("JMP_NO_MEDIA_AGENT"):
        return                      # the tests start throwaway servers
    script = Path(__file__).with_name("media_keys.py")
    if not script.is_file():
        return
    if not (os.environ.get("DBUS_SESSION_BUS_ADDRESS")
            or os.path.exists(f"/run/user/{os.getuid()}/bus")):
        return
    for python in _window_pythons():
        try:
            running = subprocess.run([python, str(script), "--check"],
                                     capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if running.returncode == 0:
            return
        try:
            has_gi = subprocess.run([python, "-c", "import gi"],
                                    capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if has_gi.returncode != 0:
            continue
        env = dict(os.environ)
        env.pop("PYTHONHOME", None)
        try:
            subprocess.Popen([python, str(script), "--url", url],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True, env=env)
        except OSError:
            continue
        return


def open_window(url: str) -> bool:
    """Open `url` as an application window when possible.

    A GTK/WebKit window is preferred on GNOME; otherwise a chromeless browser
    window is used, and plain browser opening is the last resort.
    """
    if _native_window(url):
        return True
    for name in APP_BROWSERS:
        binary = shutil.which(name)
        if not binary:
            continue
        try:
            subprocess.Popen(
                [binary, f"--app={url}", "--window-size=1440,960",
                 "--class=julians-music-app", "--name=julians-music-app"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            return True
        except OSError:
            continue
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["open", "-na", "Google Chrome", "--args",
                              f"--app={url}"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _notify(message: str) -> None:
    """A desktop notice when no terminal is attached (launcher start)."""
    for argv in (["notify-send", "JulAIn Music Player", message],
                 ["zenity", "--error", f"--text={message}"]):
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
        except OSError:
            continue


def run_web(root: Path | None = None, host: str = "127.0.0.1", port: int = 0,
            use_llm: bool = True, dry_run: bool = False,
            open_browser: bool = True, idle_exit: int = 0,
            token: str = "") -> int:
    service = ChatService(root=root, use_llm=use_llm, dry_run=dry_run)
    service.dry_run = dry_run

    existing = _live_server(service.root)
    if existing:
        print(f"JulAIn Music Player is already running: {existing}")
        if open_browser and not open_window(existing):
            print(existing)
            _notify("JulAIn Music Player is already running: " + existing)
        return 0

    # a restart re-executes this same command, so the token has to survive it:
    # the open page carries it in every request and in its own address
    token = token or secrets.token_urlsafe(18)
    httpd = ThreadingHTTPServer((host, port), make_handler(service, token))
    httpd.daemon_threads = True
    bound_port = httpd.server_address[1]
    url = f"http://{host}:{bound_port}/?t={token}"
    _write_state(service.root, url)
    _media_agent(url)

    # only a real `music chat --web` invocation may replace itself: a test or a
    # script that builds a service gets a plain "restart the app" answer
    if "chat" in sys.argv and "--web" in sys.argv:
        _RESTART.update({"argv": list(sys.argv), "port": bound_port,
                         "token": token})

    thread = threading.Thread(target=httpd.serve_forever, name="chat-http",
                              daemon=True)
    thread.start()

    stop = threading.Event()

    def _stop(*_args) -> None:
        stop.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        try:
            signal.signal(getattr(signal, signal_name), _stop)
        except (AttributeError, ValueError):
            pass

    if open_browser:
        if not open_window(url):
            print("No browser found — open this URL by hand:")
            print(url)
            _notify("No browser was found. Open the chat at " + url)
    else:
        print(url)
    sys.stdout.flush()

    def _watch() -> None:
        while not stop.wait(5.0):
            if idle_exit and time.time() - service.last_request > idle_exit:
                stop.set()
                return
            if not thread.is_alive():
                stop.set()
                return

    threading.Thread(target=_watch, name="chat-idle", daemon=True).start()

    try:
        while not stop.wait(0.5):
            if not thread.is_alive():
                break
    finally:
        httpd.shutdown()
        httpd.server_close()
        service.quit()
        _clear_state(service.root)
    return 0
