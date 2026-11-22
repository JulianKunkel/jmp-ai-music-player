"""The conversation engine: plans in, skills out, no UI imports.

The Textual app and the plain-text mode both call ``Engine.handle()``, so the
behaviour is identical in the terminal and in tests.  Every side effect goes
through an existing journalled tool; the only thing this module writes on its
own is app state under ``library-work/app/chat/``.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import (endless, index_job, library_job, lrc, lyrics_job, loudness,
               psycho_job, radio)
from . import downloads as youtube, recent, session
from . import arrivals as arrivals_mod
from . import duplicates as duplicates_mod
from . import health
from .agent import Agent
from .catalog import (AUDIO_EXT, Catalog, Playlist, Track, clock, fold,
                      human_duration, library_rel)
from .llm import LLM
from .lyrics import Lyrics, clock as lyric_clock, combine as lyrics_combine
from .nlu import Context, Plan, plan as nlu_plan, rank_terms
from .player import Player
from .profile import Profile
from .psycho import (AXES, AXIS_KEYS, RANKED, RAW, RAW_GROUPS, Psycho,
                     concept_labels, normalise_terms, recipe_axes, set_axes,
                     set_key, set_keys, SETS)
from .selection import (MOOD_WORDS, Selector, moods_for_genres,
                        moods_from_text)
from .settings import (PROVIDER_BASE, SERVICE_KEYS, SPECS,
                       apply as setting_apply, coerce as setting_coerce,
                       describe as setting_describe,
                       markdown_all as settings_markdown,
                       reset as setting_reset, resolve as setting_resolve,
                       spec as setting_spec, values as setting_values)
from .skills import REGISTRY, examples, help_markdown
from .tools import Toolbox, wants_delete
from .viz import STYLES as VIZ_STYLES, style as viz_style

#: append to the queue when this many entries are left (radio mode)
RADIO_LOW = 1

#: how often the poll may add a batch, in seconds (the poll runs every 2 s)
RADIO_EVERY = 5.0

#: endless mode tops the queue up at the same point, but follows the song that
#: is playing instead of a fixed seed
ENDLESS_LOW = 1
ENDLESS_EVERY = 5.0

#: how far into a track the back key must be before it restarts the track
#: instead of stepping to the previous entry — the usual remote/player rule
PREV_RESTART_S = 3.0

PY = sys.executable


@dataclass
class Reply:
    text: str = ""
    tool: str = ""
    #: which planner produced the answer: "local planner", "learned phrase",
    #: or the language model's label.  The window shows it under the bubble so
    #: a listener can tell an offline answer from a model answer.
    planner: str = ""
    tracks: list[Track] = field(default_factory=list)
    played: bool = False
    error: bool = False
    clear: bool = False
    quit: bool = False


def _escape(text: str) -> str:
    for char in ("\\", "*", "`", "[", "]"):
        text = str(text).replace(char, "\\" + char)
    return text


def _short(text: str, limit: int = 42) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


#: A sentence that asks for music to start carries one of these.  Without one
#: a language model may still propose `play-music` out of helpfulness, but the
#: listener asked to look, so the engine lists the hits instead of starting
#: them.  Folded forms only: `fold()` drops the umlauts.
#: an offline answer that found nothing.  Such a dead end is not an answer
#: when a language model is configured, so it is handed on instead.
DEAD_ENDS = ("nothing matches", "no tracks match", "no track title matches",
             "i found no tracks", "i found nothing")


def is_dead_end(reply: "Reply") -> bool:
    """Did the offline planner answer with nothing at all?"""
    if not reply.error:
        return False
    folded = fold(reply.text)
    return any(marker in folded for marker in DEAD_ENDS)


PLAYBACK_WORDS = ("play", "plays", "playing", "spiel", "spiele", "spielen",
                  "abspielen", "abspiel", "anhoeren", "anhören", "hoer", "hör",
                  "hoere", "höre", "queue", "shuffle", "zufaellig", "zufällig",
                  "random", "listen", "stream", "start", "starte", "starten",
                  "los", "wiedergabe", "put on", "anmachen", "auflegen",
                  "resume", "weiter")

#: "the second one", "this", "nochmal": a request that can only mean one of the
#: tracks already listed, so it is playback even without a play word
_CONTEXT_REF = re.compile(
    r"\b(?:\d{1,2}|first|second|third|fourth|fifth|last|next|another|"
    r"erste[nrs]?|zweite[nrs]?|dritte[nrs]?|vierte[nrs]?|fuenfte[nrs]?|"
    r"fünfte[nrs]?|letzte[nrs]?|naechste[nrs]?|nächste[nrs]?|"
    r"this|that|it|these|those|das|dies(?:e[sr]?)?|davon)\b")


def _wants_playback(text: str) -> bool:
    """Does that sentence ask for playback rather than for a list?"""
    folded = fold(text)
    return any(re.search(rf"(?<!\w){re.escape(fold(word))}(?!\w)", folded)
               for word in PLAYBACK_WORDS)


def _number_tracks(tracks: list[Track], limit: int = 12,
                   show_path: bool = True) -> str:
    lines = []
    for number, track in enumerate(tracks[:limit], 1):
        lines.append(f"{number}. **{_escape(track.who)} — "
                     f"{_escape(track.name)}**  ·  {clock(track.duration_s)}")
        if show_path:
            lines.append(f"   `{track.rel}`")
    if len(tracks) > limit:
        lines.append(f"… and {len(tracks) - limit} more")
    return "\n".join(lines)


def _views(count) -> str:
    try:
        value = int(count)
    except (TypeError, ValueError):
        return ""
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B views"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M views"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K views"
    return f"{value} views"


class Engine:
    def __init__(self, root: Path | None = None, use_llm: bool = True,
                 dry_run: bool = False):
        from libpaths import library_root
        self.root = Path(root) if root is not None else library_root()
        self.catalog = Catalog(self.root)
        self.profile = Profile(self.root)
        self.selector = Selector(self.catalog, self.profile)
        self.player = Player()
        self.psycho = Psycho(self.root)
        self.lyrics = Lyrics(self.root)
        self.ctx = Context()
        self.dry_run = dry_run
        llm_enabled = bool(use_llm and self.profile.setting("use_llm", True))
        self.llm = LLM(self.root, enabled=llm_enabled, profile=self.profile)
        self.agent = Agent(self.root, self.llm)
        self.history: list[dict] = []
        #: a service-level setting (the music folder) the web front end applies
        #: after the turn, because it owns the player and the catalogue
        self.deferred: dict[str, str] = {}
        self._session = self._open_session()
        self._last_rel = ""
        self._last_pos = 0.0
        self._last_duration = 0.0
        self._observed: set[str] = set()
        self._youtube_pending: set[str] = set()
        self._youtube_at = 0.0
        #: the audio filter mpv currently holds, so it is set only on a change
        self._loudness_filter: str | None = None
        #: the export the last preview/import named, so "import it" works
        self._library_file = ""
        #: the random source the radio picks with (one per engine)
        self._radio_rng = random.Random()
        #: when the radio last appended a batch
        self._radio_at = 0.0
        #: when endless mode last appended a batch
        self._endless_at = 0.0
        #: the queue the last play handed to mpv, so "continue" can rebuild it
        self._queue_target: dict | None = None
        #: a temp queue written just before the play that used it
        self._pending_queue: dict | None = None
        #: when the session file was last written (the poll throttles on it)
        self._session_saved_at = 0.0

    # -- session -----------------------------------------------------------

    def _open_session(self) -> Path | None:
        try:
            folder = self.root / "library-work" / "app" / "chat" / "sessions"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            return folder / f"{stamp}.jsonl"
        except OSError:
            return None

    def _save_turn(self, role: str, text: str, meta: dict | None = None) -> None:
        if self._session is None:
            return
        try:
            with self._session.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "role": role,
                    "text": text,
                    "meta": meta or {},
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # -- the one entry point ----------------------------------------------

    def handle(self, text: str) -> Reply:
        original = " ".join(str(text or "").split())
        if not original:
            return Reply("")

        if original.startswith("/"):
            reply = self._slash(original)
        else:
            reply = self._converse(original)

        self.history.append({"role": "user", "text": original})
        self.history.append({"role": "assistant", "text": reply.text})
        del self.history[:-40]
        self._save_turn("user", original)
        self._save_turn("assistant", reply.text, {"tool": reply.tool})
        return reply

    def _converse(self, text: str) -> Reply:
        planner = "local planner"
        try:
            plan = nlu_plan(text, self.ctx, self.catalog, self.profile)
            # a phrase learned from an earlier unrecognised request comes
            # before the language model: the listener already confirmed it
            if plan is None or plan.confidence < 0.6:
                learned = self.agent.plan(text)
                if learned is not None:
                    plan = learned
                    planner = "learned phrase · local"
            if plan is None or plan.confidence < 0.6:
                if self.llm.available():
                    planner = self.llm.badge()
                    # the planner is an agent: it may look the library up with
                    # tools and make the one allowed write (a playlist) before
                    # answering, instead of only picking a skill name
                    toolbox = Toolbox(self, text)
                    result = self.llm.agent(text, self.ctx, self.profile,
                                            self.catalog.stats(), toolbox,
                                            self.history)
                    if result and result.get("plan") is not None:
                        plan = result["plan"]
                    elif result is not None:
                        reply = self._agent_reply(result, toolbox)
                        reply.planner = planner
                        return reply
                    else:
                        better = self.llm.plan(text, self.ctx, self.profile,
                                               self.catalog.stats(),
                                               self.history)
                        if better is not None:
                            plan = better
            if plan is None:
                reply = self._fallback(text)
                reply.planner = planner
                return reply
            reply = self._execute(plan, text)
            if is_dead_end(reply) and self.llm.available():
                # the offline parser matched but found nothing: a dead end is
                # not the answer, so the planner gets the sentence after all
                rescued = self._llm_turn(text)
                if rescued is not None:
                    rescued.planner = self.llm.badge()
                    return rescued
            if plan.say and plan.say.casefold() not in reply.text.casefold():
                reply.text = plan.say + "\n\n" + reply.text
            reply.planner = planner
            return reply
        except (SystemExit, OSError, ValueError) as exc:
            return Reply(f"That did not work: {exc}", error=True)
        except Exception as exc:  # never take the UI down
            return Reply(f"Unexpected problem: `{type(exc).__name__}: {exc}`",
                         error=True)

    def _llm_turn(self, text: str) -> Reply | None:
        """One planner turn, used to rescue an offline plan that found nothing.

        Returns ``None`` when the planner produced nothing usable, so the
        caller keeps the offline answer instead of replacing a hint with an
        empty reply or a second identical miss.
        """
        toolbox = Toolbox(self, text)
        result = self.llm.agent(text, self.ctx, self.profile,
                                self.catalog.stats(), toolbox, self.history)
        if result is None:
            better = self.llm.plan(text, self.ctx, self.profile,
                                   self.catalog.stats(), self.history)
            if better is None:
                return None
            reply = self._execute(better, text)
        else:
            plan = result.get("plan")
            if plan is not None:
                reply = self._execute(plan, text)
            elif toolbox.calls or str(result.get("text") or "").strip():
                reply = self._agent_reply(result, toolbox)
            else:
                return None
        if is_dead_end(reply):
            return None
        return reply

    def _play_intended(self, args: dict, request: str) -> bool:
        """Did the listener ask to hear something, or only to look for it?

        The offline parser only produces `play-music` when the sentence has a
        play word, but a language model sometimes answers a lookup with it.
        The engine follows the sentence: without a play word the hits are
        listed, and clicking one plays it.  A reference to something already
        found ("the second one") still counts as playback, because it can only
        mean one of the listed tracks.
        """
        args = args or {}
        target = str(args.get("target") or "").strip()
        if _wants_playback(request):
            return True
        # a mood mix, the whole library, an explicit shuffle or a queue is a
        # play request even when the wording is terse
        if (args.get("moods") or args.get("whole_library")
                or args.get("shuffle") or args.get("append")):
            return True
        # a URL, an absolute path, "everything" or a bare empty target is not a
        # search query: those can only mean playback
        if not target or target.startswith(("/", "~", "http://", "https://",
                                            "www.")):
            return True
        if fold(target) in {"all", "everything", "alles", "library",
                            "bibliothek"}:
            return True
        known = {track.rel for track in (self.ctx.last_tracks or [])[:80]}
        current = getattr(self.ctx.current, "rel", None)
        if target in known or target == current:
            return bool(_CONTEXT_REF.search(fold(request)))
        return False

    def _execute(self, plan: Plan, request: str) -> Reply:
        if plan.skill == "answer":
            # the optional planner decided this is conversation, not an action
            return Reply(str(plan.args.get("text") or "").strip()
                         or self._welcome(), tool="chat")
        if plan.skill == "play-music" and not self._play_intended(plan.args,
                                                                  request):
            # "merlin wooden", not "play merlin wooden": the listener asked to
            # see, so the hits are listed (and are clickable) instead
            target = str((plan.args or {}).get("target") or "").strip()
            query = target
            for track in (self.ctx.last_tracks or [])[:80]:
                if track.rel == target:
                    query = track.name or target
                    break
            reply = self.skill_search({"query": query}, request)
            reply.tool = "search-library · not started"
            return reply
        meta = REGISTRY.get(plan.skill)
        if meta is None:
            return self._fallback(request, "I don't know that skill yet.")
        handler = getattr(self, meta["handler"], None)
        if handler is None:
            return Reply(f"`{plan.skill}` is not implemented.", error=True)
        reply = handler(plan.args, request)
        if not reply.tool:
            args = " · ".join(f"{key}={_short(value)}"
                              for key, value in list(plan.args.items())[:2]
                              if value not in (None, "", [], {}))
            reply.tool = plan.skill + (f" · {args}" if args else "")
        self.ctx.last_skill = plan.skill
        self.ctx.last_request = request
        return reply

    def _agent_reply(self, result: dict, toolbox: Toolbox) -> Reply:
        """Turn the agent's final text into a reply, keeping what it did."""
        text = str(result.get("text") or "").strip()
        if not text:
            text = toolbox.report() or toolbox.summary()
        if not text:
            text = "Done."
        used = ", ".join(dict.fromkeys(toolbox.calls))
        label = "llm" + (f" · {used}" if used else "")
        self.ctx.last_skill = "llm"
        self.ctx.last_request = ""
        return Reply(text, tool=label,
                     tracks=(toolbox.last_tracks or [])[:20],
                     played=toolbox.played)

    # -- fallback ----------------------------------------------------------

    def _fallback(self, text: str, prefix: str = "") -> Reply:
        folded = fold(text).strip()
        if re.fullmatch(r"(hi|hey|hello|hallo|moin|servus|good (morning|evening)|"
                        r"guten (morgen|abend)|na)\b.*", folded):
            return Reply(self._welcome())
        if folded in {"thanks", "thank you", "danke", "super", "great", "nice",
                      "ok", "okay", "cool"}:
            return Reply("Any time. Ask for a playlist, a search, or a link.")

        # a bare query ("portishead") is most likely a search
        words = folded.split()
        if 0 < len(words) <= 8 and not re.search(
                r"\b(what|how|why|who|when|where|was|wie|warum|wer|wann|wo)\b",
                folded):
            hits = self.catalog.search(text, 8)
            if hits:
                self.ctx.last_tracks = hits
                self.ctx.last_query = text
                return Reply(
                    f"I found {len(hits)} tracks for “{_escape(text)}”:"
                    f"\n\n{_number_tracks(hits)}"
                    "\n\nSay “play number 2”, or be more specific.",
                    tool="search-library · fallback search", tracks=hits)

        # not an action and not a search: keep the request for the skill
        # agent instead of throwing it away
        proposal = None
        if len(words) >= 3 and not prefix:
            try:
                proposal = self.agent.propose(text)
            except OSError:
                proposal = None
        hint = prefix + "\n\n" if prefix else ""
        body = (hint + "I didn't catch that as an action. Things I can do:\n\n"
                + "\n".join(f"- “{example}”" for example in examples()))
        if proposal:
            guess = str(proposal.get("skill") or "")
            body += ("\n\n**Recorded as a skill request** "
                     f"`{proposal.get('id')}` in "
                     "`library-work/app/chat/agent/requests.jsonl`.")
            if guess:
                phrases = ", ".join(f"“{t}”" for t in
                                    (proposal.get("triggers") or [])[:4])
                body += (f"\nMy best guess: `{guess}`"
                         + (f", triggered by {phrases}" if phrases else "")
                         + ".\nSay **teach that** to keep it, or teach a "
                         "phrase: “when I say <phrase> I mean chill and jazz”.")
            else:
                body += ("\nWithout a language model I cannot guess the "
                         "action, so teach a phrase: “when I say <phrase> I "
                         "mean …”. `/agent` shows what is waiting.")
        body += "\n\nType `/skills` for the full list, or `/help` for commands."
        return Reply(body, error=False)

    def _welcome(self) -> str:
        stats = self.catalog.stats()
        text = (
            f"**JulAIn Music Player** · {stats['files']:,} tracks · "
            f"{stats['hours']:,.0f} h · {stats['artists']:,} artists\n\n"
            "Tell me what you want to hear — a mood, a length, an artist — "
            "or ask me to add something.\n\n"
            + "\n".join(f"- “{example}”" for example in examples()[:6])
            + "\n\n`/skills` shows every skill, `/llm` shows the planner."
            + "\n\n_Written with AI assistance · GPL-3.0-or-later · see NOTICE._"
        )
        return text

    # -- lifecycle helpers used by the UIs --------------------------------

    def welcome(self) -> str:
        """The greeting, also used when the conversation is cleared."""
        return self._welcome()

    def close(self) -> None:
        """Persist what was learned; safe to call more than once."""
        self.profile.save()

    # -- slash commands ----------------------------------------------------

    def _slash(self, text: str) -> Reply:
        command, _, rest = text[1:].partition(" ")
        command = command.casefold().strip()
        rest = rest.strip()
        if command in {"help", "skills", "commands", "?"}:
            return Reply(help_markdown(), tool="skills")
        if command == "play":
            return self._execute(Plan("play-music", {"target": rest or "All"},
                                      source="slash"), text)
        if command == "search":
            return self._execute(Plan("search-library", {"query": rest},
                                      source="slash"), text)
        if command in {"make", "playlist"}:
            moods = moods_from_text(rest, self.profile) or {"Chill": 1.0}
            return self._execute(Plan("make-playlist", {
                "name": "", "moods": moods, "autoplay": True,
                "request": rest,
            }, source="slash"), text)
        if command == "mood":
            return self._execute(Plan("play-music", {"target": rest},
                                      source="slash"), text)
        if command in {"playlists", "pls"}:
            return self._execute(Plan("show-playlists", {}, source="slash"),
                                 text)
        if command == "save":
            return self._execute(Plan("save-playlist", {"name": rest},
                                      source="slash"), text)
        if command in {"activity", "history"}:
            return self._execute(Plan("activity", {}, source="slash"), text)
        if command == "agent":
            action = rest.casefold().strip() or "show"
            if action not in {"show", "approve", "reject"}:
                action = "show"
            return self._execute(Plan("agent", {"action": action},
                                      source="slash"), text)
        if command in {"viz", "visualizer"}:
            wanted = rest.casefold().strip()
            if wanted in {name.casefold() for name in VIZ_STYLES}:
                chosen = viz_style(wanted)
                self.profile.set_setting("visualizer", True)
                self.profile.set_setting("viz_style", chosen)
                return Reply(f"Visualizer on, style **{VIZ_STYLES[chosen]}**.",
                             tool="viz")
            if wanted in {"off", "0", "false", "no"}:
                self.profile.set_setting("visualizer", False)
                return Reply("Visualizer off.", tool="viz")
            if wanted in {"on", "1", "true", "yes"}:
                self.profile.set_setting("visualizer", True)
                return Reply("Visualizer on — the now-playing strip shows the "
                             "waveform.", tool="viz")
            state = "on" if self.profile.setting("visualizer", True) else "off"
            current = viz_style(self.profile.setting("viz_style", ""))
            styles = ", ".join(f"`{name}`" for name in VIZ_STYLES)
            return Reply(f"Visualizer is **{state}**, style "
                         f"**{VIZ_STYLES[current]}**.\n\n"
                         f"`/viz on`, `/viz off`, or one of {styles}.",
                         tool="viz")
        if command in {"import", "yt", "youtube"}:
            return self._execute(Plan("import-audio", {"url": rest},
                                      source="slash"), text)
        if command in {"add", "file"}:
            return self._execute(Plan("add-file", {"path": rest},
                                      source="slash"), text)
        if command in {"learn", "profile"}:
            return Reply("## What I learned\n\n" + self.profile.summary_markdown(),
                         tool="profile")
        if command == "forget":
            removed = self.profile.forget(rest)
            return Reply("Forgot:\n" + "\n".join(f"- {item}" for item in removed)
                         if removed else "Nothing matched.", tool="profile")
        if command == "llm":
            if rest.casefold() in {"off", "0", "false"}:
                self.profile.set_setting("use_llm", False)
                self.llm.enabled = False
                return Reply("Language model off — the local planner stays on.",
                             tool="llm")
            if rest.casefold() in {"on", "1", "true"}:
                self.profile.set_setting("use_llm", True)
                self.llm.enabled = True
                return Reply("Language model on. " + self.llm.summary(),
                             tool="llm")
            detail = self.llm.summary()
            if self.llm.last_error:
                detail += f"\n\nlast error: `{_escape(self.llm.last_error)}`"
            return Reply("**Planner:** " + detail
                         + "\n\n`/llm off` uses only the offline parser.",
                         tool="llm")
        if command in {"stats", "status"}:
            if command == "status":
                return self._execute(Plan("transport", {"action": "status"},
                                          source="slash"), text)
            return self._execute(Plan("library-overview", {}, source="slash"), text)
        if command == "clear":
            self.history = []
            return Reply("", clear=True)
        if command in {"quit", "exit"}:
            return Reply("", quit=True)
        return Reply(f"Unknown command `/{command}` — try `/help`.")

    # -- skills: search ----------------------------------------------------

    def skill_search(self, args: dict, request: str) -> Reply:
        query = str(args.get("query") or "").strip()
        if not query:
            return Reply("What should I search for?", error=True)
        # "show me the metal albums" leaves an English filler article behind
        cleaned = re.sub(r"(?i)^the\s+", "", query).strip()
        if cleaned:
            query = cleaned
        limit = int(args.get("limit") or 12)
        limit = max(1, min(60, limit))
        hits = self.catalog.search(query, limit)
        lists = self.catalog.search_playlists(query, 3)
        self.ctx.last_tracks = hits
        self.ctx.last_query = query
        # a list the listener made or a built-in shelf is a hit too: naming it
        # is the whole answer, and "play Bands" then queues it
        list_names = " · ".join(
            f"**{_escape(item.name)}** ({item.count:,} tracks)"
            for item in lists)
        list_hint = ("\n\nSay “play " + lists[0].name
                     + "” to load it into the player." if lists else "")
        if not hits:
            if lists:
                return Reply(
                    f"No track title matches “{_escape(query)}” — but the name "
                    f"is a playlist:\n\n{list_names}{list_hint}",
                    tool="search-library")
            # a song that *sings* the word is a hit even when no tag holds
            # it: one search box covers the library and its lyrics
            sung = self._search_lyrics(query, limit) if self.lyrics.available() \
                else None
            if sung is not None:
                return sung
            # "the slowest and boring song" is not a tag search: when the
            # wording names a measured sound property, the profile database
            # can rank by it, so do not answer "no tracks match" here.
            terms = rank_terms(query)
            if terms:
                ranked = self.skill_rank(
                    {"terms": [list(pair) for pair in terms],
                     "limit": limit}, request)
                if not ranked.error:
                    ranked.text += (
                        f"\n\n_No title matched “{_escape(query)}”, so this is "
                        "the sound-property ranking._")
                    ranked.tool = "rank-library"
                    return ranked
            artists = [name for name in self.catalog.artists()
                       if fold(query) in fold(name)][:5]
            extra = ""
            if artists:
                extra = "\n\nDid you mean: " + ", ".join(
                    f"**{_escape(name)}**" for name in artists) + "?"
            return Reply(f"No tracks match “{_escape(query)}”.{extra}",
                         error=True)
        hint = ("\n\nSay “play number 2” to play one." if len(hits) > 1
                else "\n\nSay “play this” to play it.")
        return Reply(
            f"**{len(hits)} hit{'s' if len(hits) != 1 else ''}** for "
            f"“{_escape(query)}”:\n\n{_number_tracks(hits)}" + hint
            + (f"\n\n**Playlists** · {list_names}{list_hint}"
               if lists else ""),
            tracks=hits)

    def _search_lyrics(self, query: str, limit: int) -> Reply | None:
        """The lyric-line answer a title search falls back to, or None."""
        try:
            hits = self.lyrics.find(query, limit=min(10, limit))
        except Exception:                   # a broken database is no result
            return None
        tracks: list[Track] = []
        shown: list[dict] = []
        seen: set[str] = set()
        for hit in hits:
            if hit["rel"] in seen:
                continue
            track = self.catalog.track(hit["rel"])
            if track is None:
                continue
            seen.add(hit["rel"])
            tracks.append(track)
            shown.append(hit)
        if not tracks:
            return None
        lines = [f"**No title matches “{_escape(query)}” — but "
                 f"{len(tracks)} song{'s' if len(tracks) != 1 else ''} sing"
                 f"{'s' if len(tracks) == 1 else ''} it:**", ""]
        for number, hit in enumerate(shown, 1):
            artist = hit.get("artist") or ""
            lines.append(f"{number}. **{_escape(artist + ' — ' if artist else '')}"
                         f"{_escape(hit.get('title') or Path(hit['rel']).stem)}**"
                         f"  ·  `{lyric_clock(hit.get('start') or 0)}`")
            lines.append(f"   _“{_escape(hit.get('line') or '')}”_")
            lines.append(f"   `{hit['rel']}`")
        lines += ["", "Say “play number 1” to hear one — the search covers the "
                      "tags and the transcribed lyrics alike."]
        self.ctx.last_tracks = tracks
        self.ctx.last_query = query
        return Reply("\n".join(lines), tracks=tracks,
                     tool="search-library · lyrics")

    # -- skills: rank by sound ---------------------------------------------
    #
    # "the slowest and boring song" cannot be a text search: no tag holds
    # those words.  The profile database holds the measured axes, so this
    # ranks the library (or a genre/artist pool) by them instead.

    def _rank_rows(self, terms, limit: int,
                   about: str = "") -> tuple[list[dict], list[Track], str]:
        """Rows + tracks for a property ranking, optionally within a pool."""
        rels = None
        used = ""
        if about:
            pool = (self.catalog.by_genre(about)
                    or self.catalog.by_artist(about, limit=4000)
                    or self.catalog.search(about, 500))
            rels = {track.rel for track in pool}
            used = about
            if not rels:
                return [], [], about
        rows = self.psycho.rank(terms, limit=limit, rels=rels)
        tracks: list[Track] = []
        for row in rows:
            track = self.catalog.track(row["rel"])
            if track is not None:
                tracks.append(track)
        return rows, tracks, used

    @staticmethod
    def _rank_terms(args: dict) -> list[tuple[str, float]]:
        raw = args.get("terms")
        if not raw:
            raw = args.get("properties") or args.get("property") or []
        if isinstance(raw, str):
            raw = [raw]
        return normalise_terms(raw)

    def _rank_markdown(self, terms, rows: list[dict], tracks: list[Track],
                       about: str = "") -> str:
        labels = " + ".join(concept_labels(terms))
        axes = recipe_axes(terms)
        total = self.psycho.count()
        header = f"**{_escape(labels.capitalize())}**"
        if about:
            header += f" · among “{_escape(about)}”"
        header += (f" · ranked over {total:,} measured profiles"
                   if total else "")
        lines = [header, ""]
        scores = {row["rel"]: row for row in rows}
        for number, track in enumerate(tracks[:12], 1):
            row = scores.get(track.rel) or {}
            values = row.get("axes") or {}
            detail = "  ·  ".join(
                f"{axis} {values[axis]:.2f}" for axis in axes if axis in values)
            score = row.get("score")
            score_text = f"score {score:.2f}" if isinstance(score, float) else ""
            bits = "  ·  ".join(part for part in (score_text, detail) if part)
            lines.append(f"{number}. **{_escape(track.who)} — "
                         f"{_escape(track.name)}**  ·  {clock(track.duration_s)}"
                         + (f"  ·  {bits}" if bits else ""))
            lines.append(f"   `{track.rel}`")
        if not tracks:
            lines.append("_Nothing measured matches that._")
        if tracks:
            why = self._rank_why_line(tracks[0], rows[0] if rows else {}, terms)
            if why:
                lines += ["", why]
            lines += ["", "Say “play number 1” to play one, “save that as "
                          "<name>” to keep the list, or “the least "
                          f"{concept_labels(terms)[0].split()[-1]}” to flip it."]
        return "\n".join(lines)

    # -- why one track is the most (or least) of something -----------------
    #
    # The axes are 0..1 percentile ranks the writer builds from measured
    # columns, and `psycho.AXIS_PARTS` mirrors those weights, so an answer can
    # name the measurements that lifted (or lowered) the number instead of
    # repeating it.

    @staticmethod
    def _percentile_text(value: float) -> str:
        """0.98 → "98th percentile" — capped at 99, so the maximum is not 100th."""
        number = max(0, min(99, int(round(value * 100))))
        if 10 <= number % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
        return f"{number}{suffix} percentile"

    def _rank_why_line(self, track: Track, row: dict, terms) -> str:
        """One line under a ranking: what put this track on top."""
        try:
            data = self.psycho.explain(track.rel, terms=terms)
        except (OSError, ValueError):
            return ""
        if not data:
            return ""
        entry = data["axes"][0] if data.get("axes") else None
        if entry is None or not entry.get("parts"):
            return ""
        strengths = [part for part in entry["parts"]
                     if part["value"] >= 0.5][:2]
        if not strengths:
            strengths = entry["parts"][:1]
        bits = " and ".join(
            f"**{_escape(part['label'])}** {part['value']:.2f}" for part in strengths)
        return (f"↳ It leads on {_escape(entry['label'].lower())} because "
                f"{bits} sit at the top of the library — say “why is number 1 "
                f"the {concept_labels(terms)[0]}” for the full breakdown.")

    def _why_markdown(self, track: Track, data: dict, terms) -> str:
        """The explanation of one track's axes, part by part."""
        labels = " + ".join(concept_labels(terms)) if terms else ""
        head = f"**Why {_escape(track.who)} — {_escape(track.name)}**"
        head += f" is the {_escape(labels)}" if labels else " scores as it does"
        lines = [head, f"`{track.rel}`", ""]
        for entry in data.get("axes") or []:
            value = entry.get("value")
            rebuilt = entry.get("rebuilt")
            if value is None:
                continue
            line = (f"**{_escape(entry['label'])} {value:.2f}** · "
                    f"{self._percentile_text(value)} of "
                    f"{entry.get('measured', 0):,} measured tracks")
            if entry.get("set") == "instrument":
                line += " · estimated from band shares, not a classifier"
            elif entry.get("estimated"):
                line += " · estimated"
            elif entry.get("set") != "psychology":
                line += f" · {entry['set_label']} set"
            lines.append(line)
            if rebuilt is not None and abs(rebuilt - value) > 0.011:
                lines.append(
                    f"_The parts below rebuild it as {rebuilt:.2f}: the stored "
                    "profile was written in an earlier measurement pass._")
            lines.append("")
            if not entry.get("parts"):
                lines.append("_No component of this axis was measured for this "
                             "track._")
                lines.append("")
                continue
            lines.append("| measurement | this track | weight | contributes |")
            lines.append("|---|---:|---:|---:|")
            for part in entry["parts"]:
                # the source is the measured column; an axis reference already
                # says what it is, so it is not repeated in brackets
                source = part["source"]
                shown = f" ({_escape(source)})" if source in RANKED else ""
                lines.append(
                    f"| **{_escape(part['label'])}**{shown} "
                    f"| {part['value']:.2f} "
                    f"| {round(part['weight'] * 100)} % "
                    f"| {part['points']:.3f} |")
            weaker = [part for part in entry["parts"] if part["value"] < 0.5]
            stronger = [part for part in entry["parts"] if part["value"] >= 0.5]
            lines.append("")
            if stronger:
                lines.append(
                    "Pulling it up: " + ", ".join(
                        f"{_escape(part['label'])} {part['value']:.2f}"
                        for part in stronger[:3]) + ".")
            if weaker:
                lines.append(
                    "Holding it back: " + ", ".join(
                        f"{_escape(part['label'])} {part['value']:.2f}"
                        for part in weaker[:2]) + ".")
            for miss in entry.get("missing") or []:
                lines.append(f"Not used: {_escape(miss['label'])} — "
                             f"{_escape(miss['note'])}.")
            lines.append("")
        total = data.get("axes") or []
        if len(total) > 1:
            lines.append("The other axes: " + ", ".join(
                f"{_escape(entry['label'])} {entry['value']:.2f}"
                for entry in total[1:6] if entry.get("value") is not None) + ".")
            lines.append("")
        lines.append("Say “play this” to hear it, or “the most aggressive” to "
                     "rank the library by the same axis.")
        return "\n".join(lines)

    def _rank_why(self, args: dict, terms) -> Reply:
        """Explain one track's axis values from the measurements behind them."""
        if not self.psycho.available() or not self.psycho.count():
            return Reply("The sound profile is not measured yet, so there is "
                         "nothing to explain. The Settings dialog can start "
                         "it.", error=True)
        target = str(args.get("target") or "").strip()
        track: Track | None = None
        if target:
            number = re.match(r"^(?:number|no\.?|nr\.?)?\s*(\d{1,2})$",
                              fold(target))
            if number and self.ctx.last_tracks:
                index = int(number.group(1)) - 1
                if 0 <= index < len(self.ctx.last_tracks):
                    track = self.ctx.last_tracks[index]
            if track is None:
                track = self._resolve_seed(target)
        if track is None:
            track = self.ctx.current or (self.ctx.last_tracks[0]
                                         if self.ctx.last_tracks else None)
        if track is None:
            return Reply("Which track? Play it first, or say “why is <title> "
                         "the most aggressive”.", error=True)
        data = self.psycho.explain(track.rel, terms=terms)
        if not data:
            return Reply(f"No measured profile for **{_escape(track.who)} — "
                         f"{_escape(track.name)}** yet. The measurement job in "
                         "Settings fills that in.", error=True)
        self.ctx.last_tracks = [track]
        self.ctx.current = track
        return Reply(self._why_markdown(track, data, terms), tracks=[track],
                     tool="rank-library · why")

    def skill_rank(self, args: dict, request: str) -> Reply:
        terms = self._rank_terms(args)
        if not terms:
            terms = rank_terms(request)
        if not terms:
            return Reply("Rank by what? Try “the slowest song”, “the most "
                         "aggressive track” or “the most boring 20”.",
                         error=True)
        action = str(args.get("action") or "").casefold()
        if action in {"why", "explain", "reason", "breakdown"}:
            return self._rank_why(args, terms)
        if not self.psycho.available() or not self.psycho.count():
            return Reply("The sound profile is not measured yet, so there is "
                         "nothing to rank. The Settings dialog can start it.",
                         error=True)
        try:
            limit = int(args.get("limit") or 12)
        except (TypeError, ValueError):
            limit = 12
        limit = max(1, min(50, limit))
        about = str(args.get("filter") or "").strip()
        rows, tracks, used = self._rank_rows(terms, limit, about)
        if not rows:
            return Reply(f"Nothing measured matches "
                         f"“{' + '.join(concept_labels(terms))}”"
                         + (f" among “{_escape(used)}”." if used else "."),
                         error=True)
        self.ctx.last_tracks = tracks
        self.ctx.last_query = " + ".join(concept_labels(terms))
        text = self._rank_markdown(terms, rows, tracks, used)
        played = False
        if args.get("play") and tracks:
            queue = self._temp_queue(
                tracks, "rank: " + " + ".join(concept_labels(terms)),
                name="Ranked " + " + ".join(concept_labels(terms)))
            played, message = self._play_path(queue, shuffle=False)
            self.ctx.current = tracks[0]
            text += ("\n\n▶ playing" if played else f"\n\n_{message}_")
        return Reply(text, tracks=tracks, played=played, tool="rank-library")

    # -- skills: playback --------------------------------------------------

    def _all_playlist(self) -> Playlist | None:
        return self.catalog.find_playlist("All")

    def _temp_queue(self, tracks: list[Track], label: str,
                    name: str = "") -> Path:
        """Write the queue a request just built as an ad-hoc playlist.

        It lands in `Playlists/Own/` — journalled and pruned to the newest few
        — so the listener can keep it ("save it") or throw it away ("remove
        it") without the queue living in a hidden app folder.  A dry run writes
        no playlist: it keeps the old app-state path so nothing in the library
        changes.
        """
        if self.dry_run:
            folder = self.root / "library-work" / "app" / "chat" / "queues"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            path = folder / f"{stamp}.m3u8"
            lines = ["#EXTM3U", f"# {label}"]
            for track in tracks:
                lines.append(f"#EXTINF:{int(track.duration_s)},"
                             f"{track.artist} - {track.title}")
                lines.append(str(self.root / track.rel))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._pending_queue = {"kind": "temp",
                                   "label": label,
                                   "entries": [track.rel for track in tracks]}
            return path
        from playlist_edit import at_root, clean_list_name, prune_own, write_own
        stem = clean_list_name(self._own_stem(label, name))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        rels = [track.rel for track in tracks]
        with at_root(self.root):
            path = write_own(f"{stem}_{stamp}", rels, label=label)
            prune_own(12, protect=[path.name])
        self.catalog.playlists(refresh=True)
        rel = path.relative_to(self.root).as_posix()
        self.ctx.last_own = rel
        self.ctx.last_own_at = datetime.now(timezone.utc).isoformat()
        self._pending_queue = {"kind": "own", "label": label, "name": stem,
                               "entries": rels}
        return path

    @staticmethod
    def _own_stem(label: str, name: str = "") -> str:
        """A readable playlist name from a queue label ("genre: Rock" → Rock)."""
        text = str(name or label or "Mix").strip()
        text = re.sub(r"^(?:ad-hoc|mood|genre|search|rank|recommendation|"
                      r"continue)\s*:\s*", "", text, flags=re.IGNORECASE)
        text = text.split("·")[0].strip()
        return text or "Mix"

    def _own_tracks(self, rel: str) -> list[str]:
        """The library-relative audio entries of an ad-hoc list, in order."""
        path = self.root / str(rel or "")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows: list[str] = []
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                rows.append((path.parent / text).resolve()
                            .relative_to(self.root).as_posix())
            except (ValueError, OSError):
                continue
        return rows

    def _own_view(self) -> dict | None:
        """The ad-hoc list behind the current queue, for save/remove."""
        rel = str(self.ctx.last_own or "")
        if not rel:
            data = session.load(self.root) or {}
            target = str(data.get("target") or "")
            if "/Playlists/Own/" in f"/{target}":
                rel = target
        if not rel or not (self.root / rel).is_file():
            self.ctx.last_own = ""
            return None
        tracks = self._own_tracks(rel)
        if not tracks:
            self.ctx.last_own = ""
            return None
        path = self.root / rel
        label = ""
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("# ") and not line.startswith("#EXT"):
                    label = line[2:].strip()
                    break
        except OSError:
            pass
        return {"rel": rel, "name": path.stem, "label": label,
                "count": len(tracks), "tracks": tracks}

    def _play_path(self, path, append: bool = False,
                   shuffle: bool = False) -> tuple[bool, str]:
        """Play, unless this is a dry run (then report instead of playing)."""
        if self.dry_run:
            return False, "dry run — playback skipped"
        ok, message = self.player.play(str(path), append=append,
                                      shuffle=shuffle)
        if ok:
            rel = self._rel_of(str(path))
            # an ad-hoc list stays "the current queue" until something else is
            # played; anything else ends it, so the window drops save/remove
            self.ctx.last_own = rel if rel.startswith("Playlists/Own/") else ""
            self._queue_target = self._queue_for(path)
            self.save_session(force=True)
        return ok, message

    # -- what was playing (session) ---------------------------------------
    #
    # The window offers "Continue" and the chat understands "continue where I
    # left off", so the queue, the track and the playhead survive a restart.
    # Only `library-work/app/chat/session.json` is written — never a tag.

    def _rel_of(self, path: str) -> str:
        """The library-relative form of a path mpv reports, or ""."""
        text = str(path or "")
        if not text:
            return ""
        try:
            if Path(text).is_relative_to(self.root):
                return os.path.relpath(text, self.root).replace(os.sep, "/")
        except (ValueError, OSError):
            return ""
        return ""

    def _queue_for(self, path) -> dict:
        """Describe a play target so it can be queued again next time."""
        pending = self._pending_queue or {}
        self._pending_queue = None
        text = str(path)
        rel = self._rel_of(text) or text
        if rel.lower().endswith((".m3u", ".m3u8")):
            if pending.get("kind") in {"own", "temp"} and pending.get("entries"):
                return {"kind": pending["kind"], "target": rel,
                        "entries": pending["entries"],
                        "label": pending.get("label", ""),
                        "name": pending.get("name", "")}
            return {"kind": "playlist", "target": rel}
        return {"kind": "track", "target": rel}

    def save_session(self, force: bool = False,
                     status: dict | None = None) -> dict | None:
        """Remember the queue, the track and the position (throttled).

        The 2 s status poll calls this, so a run rewrites a few dozen bytes
        every 20 s; a new queue or a transport action saves at once.  Nothing
        is written in a dry run or while the player is idle.
        """
        if self.dry_run:
            return None
        if not force and time.monotonic() - self._session_saved_at < 20.0:
            return None
        status = status if status is not None else self.player.status()
        if not status or status.get("idle"):
            return None
        rel = self._rel_of(str(status.get("file") or ""))
        if not rel:
            return None
        track = self.catalog.track(rel)
        data = dict(self._queue_target or {"kind": "track", "target": rel})
        data.update({
            "rel": rel,
            "position": round(float(status.get("time") or 0), 1),
            "duration": round(float(status.get("duration") or 0), 1),
            "index": int(status.get("playlist_pos") or 0),
            "count": int(status.get("playlist_count") or 0),
            "shuffle": bool(status.get("shuffle")),
            "artist": (track.artist if track is not None else ""),
            "title": (track.title if track is not None
                      else str(status.get("title") or "")),
        })
        try:
            session.save(self.root, data)
        except OSError:
            return None
        self._session_saved_at = time.monotonic()
        return data

    def resume_info(self) -> dict:
        """What "Continue" would start, for the window and the chat."""
        data = session.load(self.root)
        if not data:
            return {"available": False}
        return {
            "available": True,
            "label": session.describe(data),
            "rel": str(data.get("rel") or ""),
            "position": float(data.get("position") or 0),
            "duration": float(data.get("duration") or 0),
            "index": int(data.get("index") or 0),
            "count": int(data.get("count") or 0),
            "kind": str(data.get("kind") or "track"),
            "updated": str(data.get("updated") or ""),
        }

    def _jump_to(self, index: int, position: float, rel: str = "") -> None:
        """Jump to a queue entry and a playhead once mpv has the playlist.

        `loadfile` returns before the playlist is built, so the index is set
        only when the entry really exists; the seek waits for the new file so
        it is not applied to the old one.
        """
        if index <= 0 and position <= 2:
            return
        for _ in range(20):
            status = self.player.status() or {}
            count = int(status.get("playlist_count") or 0)
            if index <= 0 or count > index:
                break
            time.sleep(0.1)
        if index > 0:
            # only when that entry really exists: a stale session index (the
            # queue of last time was longer) would make mpv jump past its end
            # and stop, which looked like "Continue" playing nothing
            now = self.player.status() or {}
            if int(now.get("playlist_count") or 0) > index:
                self.player.command("position", index + 1)
        if position > 2:
            if rel:
                for _ in range(15):
                    status = self.player.status() or {}
                    if self._rel_of(str(status.get("file") or "")) == rel:
                        break
                    time.sleep(0.1)
            self.player.command("seek_abs", position)

    def resume_last(self) -> Reply:
        """Carry on with the queue, the track and the playhead of last time."""
        data = session.load(self.root)
        if not data:
            return Reply("There is nothing to continue yet — play something "
                         "first.", error=True, tool="player")
        position = float(data.get("position") or 0)
        index = int(data.get("index") or 0)
        rel = str(data.get("rel") or "")
        status = self.player.status() or {}
        message = ""
        if (status and not status.get("idle")
                and self._rel_of(str(status.get("file") or "")) == rel):
            # the daemon still holds the queue: only the playhead is off
            self.player.command("resume")
            self._jump_to(0, position, rel)
        else:
            target = str(data.get("target") or rel)
            kind = str(data.get("kind") or "")
            if kind == "own":
                saved = str(data.get("target") or "")
                if saved and (self.root / saved).is_file():
                    # the ad-hoc list is still on disk: play it again instead
                    # of leaving another copy in Playlists/Own/
                    self.ctx.last_own = saved
                    target = str(self.root / saved)
                else:
                    tracks = [self.catalog.track(entry)
                              for entry in (data.get("entries") or [])]
                    tracks = [track for track in tracks if track is not None]
                    if not tracks:
                        return Reply(
                            "The queue from last time is gone — its ad-hoc "
                            "playlist and its tracks are no longer in the "
                            "library.", error=True, tool="player")
                    target = str(self._temp_queue(tracks, "continue"))
            elif kind == "temp":
                tracks = [self.catalog.track(entry)
                          for entry in (data.get("entries") or [])]
                tracks = [track for track in tracks if track is not None]
                if not tracks:
                    return Reply(
                        "The queue from last time is gone — its tracks are no "
                        "longer in the library.", error=True, tool="player")
                target = str(self._temp_queue(tracks, "continue"))
            elif target and not os.path.isabs(target):
                # the stored target is library-relative, mpv wants a real path
                target = str(self.root / target)
            if not target:
                return Reply("There is nothing to continue yet.",
                             error=True, tool="player")
            ok, message = self._play_path(target)
            if not ok:
                return Reply(f"That did not start: {message}", error=True,
                             tool="player")
            self._jump_to(index, position, rel)
        track = self.catalog.track(rel)
        if track is not None:
            self.ctx.current = track
            self.ctx.last_tracks = [track]
        # the queue that really plays now, not the one the record remembers:
        # a stale index/count would otherwise make the answer claim an entry
        # that does not exist ("entry 10 of 10" in a 6-entry queue)
        live = self.save_session(force=True) or {}
        text = f"▶ Continuing **{_escape(session.describe(data))}**"
        count = int(live.get("count") or data.get("count") or 0)
        shown = int(live.get("index") or 0) if live else index
        if 0 < shown < count:
            text += (f"\n\nentry {shown + 1} of {count}")
        if message:
            text += f"\n\n_{message}_"
        return Reply(text, tracks=[track] if track is not None else [],
                     played=True, tool="player")

    def forget_session(self) -> bool:
        """Drop the remembered session (the window's ✕)."""
        self._queue_target = None
        return session.clear(self.root)

    # -- the queue (what plays next) --------------------------------------

    def queue_view(self, limit: int = 200) -> dict:
        """The current entry and what follows, for the window's Up next."""
        status = self.player.status() or {}
        entries = []
        for number, path in self.player.queue_from(limit):
            rel = self._rel_of(path)
            track = self.catalog.track(rel) if rel else None
            entries.append({
                "index": number,
                "rel": rel or str(path),
                "artist": track.artist if track is not None else "",
                "title": (track.title if track is not None
                          else Path(path).stem.replace("_", " ")),
                "duration": clock(track.duration_s) if track is not None else "",
            })
        total = int(status.get("playlist_count") or len(entries))
        at = int(status.get("playlist_pos") or 0) + 1 if entries else 0
        return {
            "playing": bool(status) and not status.get("idle"),
            "count": total,
            "position": at,
            "shuffle": bool(status.get("shuffle")),
            "endless": endless.active(self.root),
            "endless_repeat": bool(endless.load(self.root).get("repeat")),
            "entries": entries,
            "more": max(0, total - len(entries)),
            "own": self._own_view(),
            "playlist": str((self._queue_target or {}).get("target") or ""),
        }

    def queue_jump(self, index: int) -> Reply:
        if index <= 0:
            return Reply("Which entry of the queue?", error=True, tool="player")
        ok, message = self.player.command("position", index)
        if ok:
            self.poll()
            self.save_session(force=True)
        return Reply(f"Jumped to queue entry {index}" if ok else message,
                     error=not ok, tool="player")

    def queue_remove(self, index: int) -> Reply:
        if index <= 0:
            return Reply("Which entry of the queue?", error=True, tool="player")
        ok, message = self.player.command("remove", index)
        if ok:
            self.poll()
            self.save_session(force=True)
        return Reply(f"Removed queue entry {index}" if ok else message,
                     error=not ok, tool="player")

    def skill_play(self, args: dict, request: str) -> Reply:
        if args.get("resume"):
            return self.resume_last()
        shuffle = bool(args.get("shuffle")) or self.shuffle_mode()
        append = bool(args.get("append"))
        whole = bool(args.get("whole_library"))
        target = str(args.get("target") or "").strip()
        moods = args.get("moods")

        # an ad-hoc mood request ("play something smooth")
        if isinstance(moods, dict) and moods:
            length = args.get("duration_min")
            try:
                length = float(length) if length else None
            except (TypeError, ValueError):
                length = None
            selection = self.selector.build(
                moods,
                count=None if length else int(args.get("count") or 30),
                duration_min=length, allow_recent=False)
            if not selection.tracks:
                return Reply("I could not find anything for that mood.",
                             error=True)
            blend = " + ".join(mood for mood, _ in sorted(
                selection.moods.items(), key=lambda kv: -kv[1]))
            queue = self._temp_queue(selection.tracks,
                                     "ad-hoc: " + selection.summary(),
                                     name=blend or "Mood mix")
            ok, message = self._play_path(queue, append=append,
                                          shuffle=shuffle or True)
            self.ctx.last_tracks = selection.tracks
            self.ctx.last_moods = dict(selection.moods)
            self.ctx.current = selection.tracks[0]
            text = (f"**{_escape(blend or 'Mood mix')}** · "
                    f"{selection.summary()}\n\n"
                    + _number_tracks(selection.tracks, 6)
                    + ("\n\n▶ playing" if ok else f"\n\n_{message}_"))
            return Reply(text, tracks=selection.tracks, played=ok)

        if whole or fold(target) in {"all", "everything", "alles", "library",
                                     "bibliothek"}:
            playlist = self._all_playlist()
            if playlist is None:
                return Reply("There is no `Playlists/All.m3u8` in this "
                             "library, so I cannot queue everything.",
                             error=True)
            tracks = self.catalog.playlist_tracks(playlist)
            ok, message = self._play_path(playlist.path, append=append,
                                          shuffle=shuffle)
            self.ctx.last_tracks = tracks[:80]
            self.ctx.current = tracks[0] if tracks else None
            self.ctx.last_playlist = "All"
            text = (f"**The whole library** · {len(tracks):,} tracks · "
                    f"{human_duration(sum(t.duration_s for t in tracks))}"
                    + (" · shuffled" if shuffle else "")
                    + (f"\n\n▶ {'queued' if append else 'playing'}"
                       if ok else f"\n\n_{message}_"))
            return Reply(text, tracks=tracks[:20], played=ok)

        if not target:
            playlist = self._all_playlist()
            target = str(playlist.path) if playlist else "All"

        resolved = self.catalog.resolve(target)
        if resolved.kind == "none":
            return Reply("What should I play?", error=True)

        # a smart list is generated output: refresh it before it is played,
        # so the queue matches the rules at this moment
        if (resolved.playlist is not None
                and resolved.playlist.kind == "smart"):
            try:
                from . import smart as smart_lists
                smart_lists.build(
                    self.root, resolved.playlist.name,
                    rows=self.catalog.rows,
                    axes=self._smart_axes(
                        (smart_lists.definitions(self.root)
                         .get(resolved.playlist.name) or {}).get("rules") or {}),
                    dry_run=self.dry_run)
                self.catalog.playlists(refresh=True)
            except (OSError, ValueError, SystemExit):
                pass                    # play whatever the file holds now

        if resolved.kind == "search" and not resolved.target:
            hits = resolved.candidates
            if not hits:
                mood = self.catalog.mood_playlist(target)
                if mood is not None:
                    ok, message = self._play_path(mood.path, append=append,
                                                  shuffle=shuffle)
                    tracks = self.catalog.playlist_tracks(mood)
                    self.ctx.last_tracks = tracks[:80]
                    self.ctx.last_playlist = mood.name
                    self.ctx.current = tracks[0] if tracks else None
                    return Reply(
                        f"**{mood.name}** · {mood.count:,} tracks"
                        + (f"\n\n▶ {'queued' if append else 'playing'}"
                           if ok else f"\n\n_{message}_"),
                        tracks=tracks[:20], played=ok)
                terms = rank_terms(target)
                if terms:
                    # "play the slowest song": no title matches those words,
                    # but the profile database can rank by them
                    ranked = self.skill_rank(
                        {"terms": [list(pair) for pair in terms]}, request)
                    if not ranked.error and ranked.tracks:
                        queue = self._temp_queue(
                            ranked.tracks, "rank: " + " + ".join(
                                concept_labels(terms)),
                            name="Ranked " + " + ".join(
                                concept_labels(terms)))
                        ok, message = self._play_path(
                            queue, append=append, shuffle=shuffle)
                        self.ctx.current = ranked.tracks[0]
                        text = (f"_No title matched “{_escape(target)}”, so I "
                                "ranked the library by sound._\n\n"
                                + ranked.text
                                + ("\n\n▶ " + ("queued" if append
                                               else "playing")
                                   if ok else f"\n\n_{message}_"))
                        return Reply(text, tracks=ranked.tracks, played=ok,
                                     tool="rank-library")
                return Reply(
                    f"Nothing matches “{_escape(target)}”.\n\n"
                    "Try a broader word, or ask what I can play — for "
                    "example “can you play spc files?”.", error=True)

            # several hits: a genre queue beats a list of titles that share the word
            genre_hits = self.catalog.by_genre(target)
            if len(genre_hits) >= 5 and len(genre_hits) >= len(hits):
                queue = self._temp_queue(genre_hits, f"genre: {target}",
                                         name=str(target).title())
                ok, message = self._play_path(queue, append=append,
                                              shuffle=True)
                self.ctx.last_tracks = genre_hits[:80]
                self.ctx.current = genre_hits[0]
                return Reply(
                    f"**{_escape(str(target).title())}** · "
                    f"{len(genre_hits):,} tracks · shuffled"
                    + (f"\n\n▶ {'queued' if append else 'playing'}"
                       if ok else f"\n\n_{message}_"),
                    tracks=genre_hits[:20], played=ok)

            # several hits: an artist query plays that artist, otherwise list
            artist_hits = [track for track in hits
                           if fold(target) and fold(target) in
                           fold(track.artist)]
            if len(artist_hits) == len(hits) and len(hits) >= 2:
                artist = artist_hits[0].artist
                full = (self.catalog.by_artist(artist, 2000)
                        if artist else artist_hits)
                queue = self._temp_queue(full, f"search: {target}",
                                         name=artist or str(target))
                ok, message = self._play_path(queue, append=append,
                                              shuffle=shuffle)
                self.ctx.last_tracks = full[:80]
                self.ctx.current = full[0]
                return Reply(
                    f"**{_escape(full[0].artist)}** · "
                    f"{len(full)} tracks"
                    + (" · shuffled" if shuffle else "")
                    + (f"\n\n▶ {'queued' if append else 'playing'}"
                       if ok else f"\n\n_{message}_"),
                    tracks=full[:20], played=ok)
            self.ctx.last_tracks = hits
            return Reply(
                f"{len(hits)} matches for “{_escape(target)}” — which one?"
                f"\n\n{_number_tracks(hits)}"
                "\n\nSay “play number 1”, or give me the artist too.",
                tracks=hits)

        ok, message = self._play_path(resolved.target, append=append,
                                      shuffle=shuffle)
        if resolved.playlist is not None:
            tracks = self.catalog.playlist_tracks(resolved.playlist)
            label = (f"**{resolved.playlist.name}** · "
                     f"{len(tracks):,} tracks · "
                     f"{human_duration(sum(t.duration_s for t in tracks))}")
        elif resolved.track is not None:
            tracks = [resolved.track]
            label = f"**{_escape(resolved.track.label())}**"
        else:
            tracks = []
            label = f"**{_escape(resolved.label or target)}**"
        self.ctx.last_tracks = tracks[:80] or self.ctx.last_tracks
        self.ctx.current = tracks[0] if tracks else self.ctx.current
        if resolved.playlist is not None:
            self.ctx.last_playlist = resolved.playlist.name
        if ok:
            label += ("\n\n▶ queued after the current track" if append
                      else "\n\n▶ playing")
        else:
            label += f"\n\n_{message}_"
        return Reply(label, tracks=tracks[:20], played=ok)

    # -- radio -------------------------------------------------------------

    def skill_radio(self, args: dict, request: str) -> Reply:
        """Endless radio: keep the queue topped up from the sound of a song."""
        action = str(args.get("action") or "start").casefold()
        note = "  (dry run — the radio state was not written.)" if self.dry_run else ""
        if action in {"stop", "off"}:
            if not self.dry_run and not radio.stop(self.root):
                return Reply("The radio is not on — nothing keeps playing by "
                             "itself right now.", tool="radio")
            return Reply("Radio off.  The queue that is playing stays as it "
                         "is; nothing new is added." + note, tool="radio")
        if action in {"status", "show"}:
            state = radio.load(self.root)
            if not state.get("on"):
                return Reply("The radio is off.  Say “start the radio” (or "
                             "“radio from this song”) to turn it on.",
                             tool="radio · status")
            seed = self.catalog.track(str(state.get("seed") or ""))
            return Reply(
                "**Radio on** · seeded from "
                + (_escape(seed.label()) if seed is not None else "a track")
                + f"\n\n{len(state.get('played') or []):,} tracks picked so "
                  "far.  A few more are added whenever the queue runs low.",
                tool="radio · status")
        if action == "more":
            if not self.dry_run and not radio.active(self.root):
                return Reply("The radio is off, so there is nothing to top up "
                             "— say “start the radio” first.", error=True,
                             tool="radio")
            if self.dry_run:
                # nothing may be started, but the plan can still be named
                data = radio.load(self.root)
                seed_rel = str(data.get("seed") or "")
                added = radio.candidates(
                    self.catalog, self.psycho, seed_rel, radio.BATCH,
                    avoid=set(data.get("played") or []),
                    rng=self._radio_rng) if seed_rel else []
                if not added:
                    return Reply("A dry run cannot top anything up — there is "
                                 "no seed stored for this radio yet.",
                                 tool="radio · more")
                lines = [f"**Would add {len(added)} more** _(dry run)_"] + [
                    f"- {_escape(track.who)} — {_escape(track.name)}"
                    for rel in added
                    for track in [self.catalog.track(rel)] if track is not None]
                return Reply("\n".join(lines), tool="radio · more")
            added = self._radio_extend()
            if not added:
                return Reply("I could not find anything to add — the library "
                             "may be too small for this seed.",
                             tool="radio · more")
            lines = [f"**Added {len(added)} more**"] + [
                f"- {_escape(track.who)} — {_escape(track.name)}"
                for rel in added
                for track in [self.catalog.track(rel)] if track is not None]
            return Reply("\n".join(lines), tool="radio · more")

        # -- start ------------------------------------------------------
        seed = self._radio_seed(str(args.get("target") or ""))
        if seed is None:
            return Reply("There is nothing to build a radio from — the "
                         "library has no playable track yet.", error=True,
                         tool="radio")
        label = seed.genre or seed.who or "mix"
        state = radio.load(self.root)
        if not self.dry_run:
            state = radio.start(self.root, seed.rel, label, seed.genre)
            if endless.active(self.root):
                endless.stop(self.root)   # one mode owns the queue
        avoid = set(state.get("played") or [])
        status = self.player.status() or {}
        playing = "" if status.get("idle") else self._rel_of(
            str(status.get("file") or ""))
        for _index, entry in self.player.queue_from(limit=400):
            rel = self._rel_of(str(entry))
            if rel:
                avoid.add(rel)
        picks = radio.candidates(self.catalog, self.psycho, seed.rel,
                                 radio.BATCH, avoid=avoid,
                                 rng=self._radio_rng)
        ok, message = True, ""
        if playing != seed.rel:
            ok, message = self._play_path(self.root / seed.rel)
        appended: list[str] = []
        for rel in picks:
            started, _note = self.player.play(str(self.root / rel),
                                              append=True)
            if started:
                appended.append(rel)
        if not self.dry_run and appended:
            radio.remember(self.root, appended)
        self._queue_target = {
            "kind": "temp",
            "target": seed.rel,
            "entries": [seed.rel] + appended,
            "label": f"Radio · {label}",
        }
        if not self.dry_run:
            self.save_session(force=True)
        head = ("**Radio on** · " + _escape(label)
                + (f" · from {_escape(seed.label())}"
                   if playing != seed.rel else " · seeded from this song"))
        if picks:
            lines = [head, "", "Up next:"] + [
                f"- {_escape(track.who)} — {_escape(track.name)}"
                for rel in picks
                for track in [self.catalog.track(rel)] if track is not None]
        else:
            lines = [head, "", "_No further track was found; the radio will "
                               "try again when the queue runs low._"]
        lines.append("")
        lines.append("▶ playing" if ok and not self.dry_run
                     else f"_{message or 'dry run — nothing started'}_")
        return Reply("\n".join(lines),
                     tracks=[self.catalog.track(rel) for rel in picks
                             if self.catalog.track(rel) is not None],
                     played=bool(ok and not self.dry_run), tool="radio")

    def _radio_seed(self, target: str) -> Track | None:
        """What the radio should sound like: a track, a genre or what plays."""
        target = (target or "").strip()
        if target:
            playlist = self.catalog.find_playlist(target)
            if playlist is not None:
                tracks = self.catalog.playlist_tracks(playlist)
                if tracks:
                    return self._radio_rng.choice(tracks)
            if fold(target) not in {"this", "that", "it", "das", "dieses",
                                    "diese", "current song", "current track",
                                    "this song", "this track", "dieses lied"}:
                tracks = self.catalog.by_genre(target, limit=200)
                if tracks:
                    return self._radio_rng.choice(tracks)
        current = self._playing_track()
        if current is not None:
            return current
        rows = [row for row in self.catalog.rows
                if str(row.get("ext") or "").lower() in AUDIO_EXT
                and float(row.get("duration_s") or 0) >= radio.MIN_SECONDS]
        if not rows:
            return None
        row = self._radio_rng.choice(rows)
        return Track.from_row(row)

    def _radio_extend(self, limit: int | None = None) -> list[str]:
        """Append the next batch to the queue; returns what really started."""
        if self.dry_run:
            return []
        state = radio.load(self.root)
        seed = str(state.get("seed") or "")
        if not seed:
            return []
        avoid = set(state.get("played") or [])
        for _index, entry in self.player.queue_from(limit=400):
            rel = self._rel_of(str(entry))
            if rel:
                avoid.add(rel)
        picks = radio.candidates(self.catalog, self.psycho, seed,
                                 limit or radio.BATCH, avoid=avoid,
                                 rng=self._radio_rng)
        added: list[str] = []
        for rel in picks:
            started, _message = self.player.play(str(self.root / rel),
                                                 append=True)
            if started:
                added.append(rel)
        if not added:
            return []
        radio.remember(self.root, added)
        target = dict(self._queue_target or {})
        entries = list(target.get("entries") or [])
        entries.extend(rel for rel in added if rel not in entries)
        target.update({
            "kind": target.get("kind") or "temp",
            "target": target.get("target") or seed,
            "entries": entries,
        })
        if not target.get("label"):
            target["label"] = f"Radio · {state.get('genre') or 'mix'}"
        self._queue_target = target
        self.save_session(force=True)
        return added

    def _radio_tick(self, status: dict) -> None:
        """Top the queue up when it is about to run out (called from poll)."""
        if self.dry_run or not status or status.get("idle"):
            return
        if endless.active(self.root):
            return                      # endless mode owns the queue
        count = int(status.get("playlist_count") or 0)
        pos = int(status.get("playlist_pos") or 0)
        if not count or count - pos > RADIO_LOW:
            return
        now = time.monotonic()
        if now - self._radio_at < RADIO_EVERY:
            return
        if not radio.active(self.root):
            return
        self._radio_at = now
        self._radio_extend()

    # -- endless mode ------------------------------------------------------
    #
    # Radio mode grows one queue from one seed.  Endless mode follows whatever
    # is playing: each top-up sounds like the song that just ran, so the queue
    # drifts.  When nothing fitting is left it repeats the queue instead of
    # going silent.

    def skill_endless(self, args: dict, request: str) -> Reply:
        """A queue that keeps growing from the track that just played."""
        action = str(args.get("action") or "start").casefold()
        note = ("  (dry run — nothing was stored.)" if self.dry_run else "")
        if action in {"off", "stop", "disable"}:
            was_repeating = endless.load(self.root).get("repeat")
            was = False if self.dry_run else endless.stop(self.root)
            if was_repeating and self.player.running():
                self.player.command("repeat_off")
            if not self.dry_run and not was:
                return Reply("Endless mode is already off.", tool="endless")
            return Reply("Endless mode off.  The queue that is playing stays "
                         "as it is; nothing new is added." + note,
                         tool="endless")
        if action in {"status", "show"}:
            state = endless.load(self.root)
            if not state.get("on"):
                return Reply("Endless mode is off.  Say “endless mode” to "
                             "keep the music going without asking again.",
                             tool="endless · status")
            seed = self.catalog.track(str(state.get("last") or ""))
            lines = ["**Endless mode on** · grows from the song that just "
                     "played"]
            if seed is not None:
                lines.append(f"Last anchor: {_escape(seed.label())}")
            if state.get("repeat"):
                lines.append("The queue is repeating because no fitting "
                             "track was left.")
            lines.append(f"{len(state.get('played') or []):,} tracks picked "
                         "so far.")
            return Reply("\n\n".join(lines), tool="endless · status")
        if action == "more":
            if not self.dry_run and not endless.active(self.root):
                return Reply("Endless mode is off, so there is nothing to top "
                             "up — say “endless mode” first.", error=True,
                             tool="endless")
            added = self._endless_extend()
            if not added:
                return Reply("Nothing fitting was left, so the queue now "
                             "repeats instead of stopping." if not self.dry_run
                             else "A dry run cannot top anything up.",
                             tool="endless · more")
            lines = [f"**Added {len(added)} more**"] + [
                f"- {_escape(track.who)} — {_escape(track.name)}"
                for rel in added
                for track in [self.catalog.track(rel)] if track is not None]
            return Reply("\n".join(lines), tool="endless · more")

        # -- on ------------------------------------------------------------
        state = endless.load(self.root)
        seed = self._playing_track()
        if seed is None:
            stored = self.catalog.track(str(state.get("last") or ""))
            seed = stored if stored is not None else self._radio_seed("")
        if seed is None:
            return Reply("There is nothing to grow the queue from — the "
                         "library has no playable track yet.", error=True,
                         tool="endless")
        if not self.dry_run:
            endless.start(self.root, seed.rel)
            if radio.active(self.root):
                radio.stop(self.root)     # one mode owns the queue
        status = self.player.status() or {}
        playing = "" if status.get("idle") else self._rel_of(
            str(status.get("file") or ""))
        ok, message = True, ""
        if playing != seed.rel:
            ok, message = self._play_path(self.root / seed.rel)
        picks = self._endless_extend(seed.rel)
        head = "**Endless mode on** · " + _escape(seed.label())
        lines = [head, ""]
        if picks:
            lines.append("Up next:")
            lines += [f"- {_escape(track.who)} — {_escape(track.name)}"
                      for rel in picks
                      for track in [self.catalog.track(rel)]
                      if track is not None]
        else:
            lines.append("_Nothing fitting was found, so the queue repeats "
                         "itself when it ends._")
        lines.append("")
        lines.append("▶ playing" if ok and not self.dry_run
                     else f"_{message or 'dry run — nothing started'}_")
        return Reply("\n".join(lines),
                     tracks=[self.catalog.track(rel) for rel in picks
                             if self.catalog.track(rel) is not None],
                     played=bool(ok and not self.dry_run), tool="endless")

    def _endless_seed(self, status: dict | None = None) -> str:
        """The track the next batch should sound like: what just played."""
        status = status if status is not None else (self.player.status() or {})
        rel = self._rel_of(str(status.get("file") or "")) if status else ""
        if rel:
            endless.set_last(self.root, rel)
            return rel
        return str(endless.load(self.root).get("last") or "")

    def _endless_extend(self, seed_rel: str = "",
                        limit: int | None = None) -> list[str]:
        """Append a batch that fits ``seed_rel``; repeat when nothing fits."""
        if self.dry_run:
            return []
        state = endless.load(self.root)
        seed = seed_rel or str(state.get("last") or "")
        if not seed:
            seed = self._endless_seed()
        if not seed:
            return []
        avoid = set(state.get("played") or [])
        for _index, entry in self.player.queue_from(limit=400):
            rel = self._rel_of(str(entry))
            if rel:
                avoid.add(rel)
        picks = radio.candidates(self.catalog, self.psycho, seed,
                                 limit or endless.BATCH, avoid=avoid,
                                 rng=self._radio_rng)
        added: list[str] = []
        for rel in picks:
            started, _message = self.player.play(str(self.root / rel),
                                                 append=True)
            if started:
                added.append(rel)
        if not added:
            # no fitting track left: keep the music going by repeating the
            # queue, a playback setting only (never a rewritten file)
            if self.player.running():
                self.player.command("repeat_all")
                endless.mark_repeat(self.root, True)
            return []
        endless.remember(self.root, added)
        endless.set_last(self.root, seed)
        if state.get("repeat"):
            if self.player.running():
                self.player.command("repeat_off")
            endless.mark_repeat(self.root, False)
        target = dict(self._queue_target or {})
        entries = list(target.get("entries") or [])
        entries.extend(rel for rel in added if rel not in entries)
        target.update({
            "kind": target.get("kind") or "temp",
            "target": target.get("target") or seed,
            "entries": entries,
            "label": target.get("label") or "Endless",
        })
        self._queue_target = target
        self.save_session(force=True)
        return added

    def _endless_tick(self, status: dict) -> None:
        """Top the queue up at the end, following the song that just played."""
        if self.dry_run or not status or status.get("idle"):
            return
        if radio.active(self.root):
            return                      # radio owns the queue while it is on
        if not endless.active(self.root):
            return
        if endless.load(self.root).get("repeat"):
            return                      # already looping; nothing to add
        count = int(status.get("playlist_count") or 0)
        pos = int(status.get("playlist_pos") or 0)
        if not count or count - pos > ENDLESS_LOW:
            return
        now = time.monotonic()
        if now - self._endless_at < ENDLESS_EVERY:
            return
        self._endless_at = now
        self._endless_extend(self._endless_seed(status))

    def shuffle_mode(self) -> bool:
        """Whether a playlist should play in random order (app setting)."""
        return bool(self.profile.setting("shuffle_mode", False))

    def set_shuffle_mode(self, on: bool) -> bool:
        """Remember the shuffle preference; writes only the app profile."""
        on = bool(on)
        if self.dry_run:
            return on
        self.profile.set_setting("shuffle_mode", on)
        return on

    def fast_transport(self, args: dict) -> Reply | None:
        """A playback key while the assistant is busy.

        A chat turn holds the service lock for as long as the planner takes
        (a language-model answer is easily a second), so a play/pause press
        would wait for the whole turn.  This path talks to mpv only: no
        context, profile or session write happens here — the two-second poll
        reconciles all three a moment later.  It answers ``None`` whenever the
        usual path must run instead (nothing loaded, unknown action), and the
        caller then waits for the lock as it always did.
        """
        action = str((args or {}).get("action") or "").casefold()
        if action == "shuffle":
            action = "shuffle_on"
        value = (args or {}).get("value")
        if action in {"shuffle_on", "shuffle_off", "repeat_all", "repeat_one",
                      "repeat_off", "speed_delta", "unmute", "position",
                      "restart"}:
            return None
        if action not in {"pause", "resume", "toggle", "stop", "next", "prev",
                          "seek", "seek_abs", "volume", "volume_delta",
                          "mute", "speed"}:
            return None
        if (action in {"seek", "seek_abs", "volume", "volume_delta", "speed"}
                and value is None):
            return None                  # the slow path explains the missing value
        status = self.player.status() or {}
        if not status or status.get("idle") or not int(
                status.get("playlist_count") or 0):
            return None                  # let the slow path start what was last
        if action == "prev":
            elapsed = float(status.get("position") or status.get("time") or 0)
            pos = int(status.get("playlist_pos") or 0)
            if elapsed > PREV_RESTART_S or pos <= 0:
                ok, message = self.player.command("seek_abs", 0.0)
                return Reply("Restarted this track." if ok else message,
                             error=not ok, tool="player")
        ok, message = self.player.command(action, value)
        if not ok:
            return Reply(message, error=True, tool="player")
        labels = {"pause": "Paused.", "resume": "Continuing.",
                  "toggle": "Toggled.", "stop": "Stopped.",
                  "next": "Next.", "prev": "Previous.", "mute": "Muted."}
        if action == "speed":
            return Reply(f"Speed {float(value):g}×", tool="player")
        if action in {"seek", "seek_abs", "volume", "volume_delta"}:
            number = float(value or 0)
            if action == "seek":
                return Reply(f"Jumped {number:+.0f} s", tool="player")
            if action == "seek_abs":
                return Reply(f"Jumped to {clock(number)}", tool="player")
            if action == "volume":
                return Reply(f"Volume {int(round(number))}%", tool="player")
            verb = "Louder" if number > 0 else "Quieter"
            return Reply(f"{verb} by {abs(int(round(number)))}%", tool="player")
        return Reply(labels.get(action, action.title()), tool="player")

    def skill_transport(self, args: dict, request: str) -> Reply:
        action = str(args.get("action") or "status").casefold()
        if action == "shuffle":
            action = "shuffle_on"
        if action == "status":
            status = self.player.status()
            if not status or status.get("idle"):
                return Reply("Nothing is playing right now.", tool="player")
            current = self.ctx.current
            lines = [f"**{'Paused' if status.get('pause') else 'Playing'}**"]
            if current:
                lines.append(f"{_escape(current.who)} — {_escape(current.name)}")
            else:
                lines.append(_escape(str(status.get("title") or "?")))
            lines.append(f"{clock(status.get('time'))} / "
                         f"{clock(status.get('duration'))}")
            if (status.get("playlist_count") or 0) > 1:
                lines.append(f"entry {int(status.get('playlist_pos') or 0) + 1}"
                             f" of {status['playlist_count']}"
                             + (" · shuffle" if status.get("shuffle") else ""))
            flags = []
            if status.get("mute"):
                flags.append("muted")
            speed = status.get("speed")
            if speed and abs(float(speed) - 1.0) > 0.01:
                flags.append(f"speed {float(speed):g}×")
            if status.get("loop_file") not in (None, "no"):
                flags.append("repeat one")
            elif status.get("loop") not in (None, "no"):
                flags.append("repeat")
            lines.append(f"volume {int(round(status.get('volume') or 0))}%"
                         + (f" · {' · '.join(flags)}" if flags else ""))
            return Reply("\n\n".join(lines), tool="player")
        if action == "queue":
            entries = self.player.queue()
            if not entries:
                return Reply("The queue is empty.", tool="player")
            lines = ["**Queue**"]
            for index, path in entries:
                name = Path(path).stem.replace("_", " ")
                lines.append(f"{index}. {_escape(name)}")
            return Reply("\n\n".join(lines), tool="player")
        if action == "volume":
            value = args.get("value")
            if value is None:
                return Reply("How loud? Say a number from 0 to 100.",
                             error=True)
            try:
                level = float(value)
            except (TypeError, ValueError):
                return Reply(f"“{value}” is not a volume from 0 to 100.",
                             error=True, tool="player")
            ok, message = self.player.command("volume", level)
            return Reply(f"Volume {int(round(level))}%" if ok else message,
                         error=not ok, tool="player")
        if action == "volume_delta":
            try:
                delta = float(args.get("value") or 0)
            except (TypeError, ValueError):
                return Reply("That is not a volume change.",
                             error=True, tool="player")
            ok, message = self.player.command("volume_delta", delta)
            verb = "Louder" if delta > 0 else "Quieter"
            return Reply(f"{verb} by {abs(int(round(delta)))}%"
                         if ok else message,
                         error=not ok, tool="player")
        if action == "seek":
            value = args.get("value")
            try:
                delta = float(value or 0)
            except (TypeError, ValueError):
                return Reply(f"“{value}” is not a number of seconds.",
                             error=True, tool="player")
            ok, message = self.player.command("seek", delta)
            return Reply(f"Jumped {delta:+.0f} s" if ok else message,
                         error=not ok, tool="player")
        if action == "seek_abs":
            try:
                value = float(args.get("value") or 0)
            except (TypeError, ValueError):
                return Reply("That is not a position in seconds.",
                             error=True, tool="player")
            ok, message = self.player.command("seek_abs", value)
            return Reply(f"Jumped to {clock(value)}" if ok else message,
                         error=not ok, tool="player")
        if action == "speed":
            try:
                value = float(args.get("value") or 1.0)
            except (TypeError, ValueError):
                return Reply("That is not a playback speed.",
                             error=True, tool="player")
            ok, message = self.player.command("speed", value)
            return Reply(f"Speed {value:g}×" if ok else message,
                         error=not ok, tool="player")
        if action == "speed_delta":
            status = self.player.status() or {}
            try:
                now = float(status.get("speed") or 1.0)
                delta = float(args.get("value") or 0)
            except (TypeError, ValueError):
                return Reply("That is not a speed change.",
                             error=True, tool="player")
            value = max(0.25, min(4.0, round(now + delta, 2)))
            ok, message = self.player.command("speed", value)
            return Reply(f"Speed {value:g}×" if ok else message,
                         error=not ok, tool="player")
        if action == "position":
            try:
                index = int(float(args.get("value") or 1))
            except (TypeError, ValueError):
                return Reply("That is not a queue position.",
                             error=True, tool="player")
            ok, message = self.player.command("position", index)
            if ok:
                self.save_session(force=True)
            return Reply(f"Jumped to queue entry {index}" if ok else message,
                         error=not ok, tool="player")
        if action in {"shuffle_on", "shuffle_off"}:
            on = action == "shuffle_on"
            self.set_shuffle_mode(on)
            ok, message = self.player.command(action)
            label = ("Shuffle on — playlists start in random order"
                     if on else "Shuffle off")
            # the preference is stored even with nothing playing, so the next
            # playlist starts shuffled; only a real player error is a failure
            if ok or "nothing is playing" in message.casefold():
                return Reply(label, tool="player")
            return Reply(message, error=True, tool="player")
        if action in {"repeat_all", "repeat_one", "repeat_off"}:
            ok, message = self.player.command(action)
            labels = {"repeat_all": "Repeating the queue",
                      "repeat_one": "Repeating this track",
                      "repeat_off": "Repeat off"}
            return Reply(labels[action] if ok else message,
                         error=not ok, tool="player")
        if action in {"mute", "unmute"}:
            ok, message = self.player.command(action)
            label = "Muted" if action == "mute" else "Sound on"
            return Reply(label if ok else message,
                         error=not ok, tool="player")
        # A media key must never be a silent no-op: a stopped player has no
        # pause flag to toggle and no previous entry to step back to, so the
        # key used to answer "Toggled." and leave the room quiet.  Play,
        # prev and next start what was playing last instead (the same
        # continue-last-session path as the window's Continue row); prev also
        # restarts the current track when it is well past its start, which is
        # what a remote's back button does.
        before = self.player.status() or {}
        idle = (not before) or bool(before.get("idle")) or not int(
            before.get("playlist_count") or 0)
        if action in {"toggle", "resume", "prev", "next"} and idle:
            reply = self.resume_last()
            if not reply.error:
                return reply
            return Reply("Nothing is playing right now — say “play …” and "
                         "I'll start something.", tool="player")
        if action == "pause" and idle:
            return Reply("Nothing is playing right now — say “play …” and "
                         "I'll start something.", tool="player")
        if action == "prev":
            elapsed = float(before.get("position")
                            or before.get("time") or 0)
            pos = int(before.get("playlist_pos") or 0)
            if elapsed > PREV_RESTART_S or pos <= 0:
                # more than a few seconds in, or the queue is at its first
                # entry: back means this track from the top, not silence
                ok, message = self.player.command("seek_abs", 0.0)
                if ok:
                    self.poll()
                    self.save_session(force=True)
                    return Reply("Restarted this track.", tool="player")
                return Reply(message, error=True, tool="player")
        ok, message = self.player.command(action)
        labels = {"pause": "Paused.", "resume": "Continuing.",
                  "toggle": "Toggled.", "stop": "Stopped.",
                  "next": "Next.", "prev": "Previous."}
        if ok:
            self.poll()
            self.save_session(force=True)
            if action == "next":
                after = self.player.status() or {}
                if after.get("idle") or not int(
                        after.get("playlist_count") or 0):
                    return Reply("That was the end of the queue — "
                                 "“continue” plays it again.", tool="player")
            return Reply(labels.get(action, action), tool="player")
        if "nothing is playing" in message.casefold():
            # a harmless state, not a failure: say what to do instead
            return Reply("Nothing is playing right now — say “play …” and "
                         "I'll start something.", tool="player")
        return Reply(message, error=True, tool="player")

    # -- skills: playlist creation ----------------------------------------

    def _normalise_moods(self, value, request: str) -> dict[str, float]:
        if isinstance(value, dict):
            moods = {str(key): float(weight) for key, weight in value.items()
                     if key in MOOD_WORDS or key in
                     {"Chill", "Happy", "Party", "Workout", "Dark",
                      "Melancholy", "Epic", "Romantic", "Focus", "Retro"}}
        elif isinstance(value, list):
            moods = {str(item): 1.0 for item in value
                     if str(item) in {"Chill", "Happy", "Party", "Workout",
                                      "Dark", "Melancholy", "Epic", "Romantic",
                                      "Focus", "Retro"}}
        elif isinstance(value, str):
            moods = moods_from_text(value, self.profile)
        else:
            moods = {}
        if not moods:
            moods = moods_from_text(request, self.profile)
        return {mood: weight for mood, weight in moods.items() if weight > 0}

    def _resolve_seed(self, value) -> Track | None:
        if not value:
            return None
        text = str(value).strip()
        if fold(text) in {"current", "this", "now", "das"}:
            return self.ctx.current or (self.ctx.last_tracks[0]
                                        if self.ctx.last_tracks else None)
        direct = self.catalog.track(text)
        if direct is not None:
            return direct
        hits = self.catalog.search(text, 1)
        return hits[0] if hits else None

    def _playlist_name(self, name: str, moods: dict[str, float],
                       request: str = "") -> str:
        if name.strip():
            return name.strip()
        text = request or self.ctx.last_request
        word = next((word for word in MOOD_WORDS
                     if re.search(rf"(?<!\w){re.escape(word)}(?!\w)",
                                  fold(text))), "")
        if word:
            return word.title()
        if moods:
            return max(moods, key=lambda key: moods[key])
        return "Mix"

    def skill_make_playlist(self, args: dict, request: str) -> Reply:
        seed = self._resolve_seed(args.get("seed"))
        moods = self._normalise_moods(args.get("moods"), request)
        if not moods:
            # "more like this" has no mood words: take the seed's own leanings
            inferred = self.selector.seed_moods(seed) if seed is not None else {}
            moods = inferred or {"Chill": 1.0}
        count = args.get("count")
        duration = args.get("duration_min")
        try:
            count = int(count) if count else None
        except (TypeError, ValueError):
            count = None
        try:
            duration = float(duration) if duration else None
        except (TypeError, ValueError):
            duration = None
        if count:
            count = max(5, min(300, count))
        if duration:
            duration = max(5, min(600, duration))
        if not count and not duration:
            count = int(self.profile.setting("default_count", 40) or 40)

        include = [str(g) for g in (args.get("include_genres") or [])]
        exclude = [str(g) for g in (args.get("exclude_genres") or [])]
        only = [str(g) for g in (args.get("only_genres") or [])]

        selection = self.selector.build(
            moods, count=count, duration_min=duration, seed=seed,
            include_genres=include, exclude_genres=exclude,
            only_genres=only, request=request)

        name = self._playlist_name(str(args.get("name") or ""), moods, request)
        if not selection.tracks:
            hint = ""
            if exclude:
                hint = " Try without “" + "”, “".join(exclude) + "”."
            return Reply(f"I found no tracks for **{_escape(name)}**."
                         f"{hint}", error=True)

        overwrite = bool(args.get("overwrite"))
        autoplay = bool(args.get("autoplay", True))
        # "play something chill" builds a queue, it does not create a playlist:
        # the list lands in `Playlists/Own/` until the listener says "save it".
        # Only a request that names a playlist ("make a playlist called
        # Evening", "save … as …") writes `Playlists/Custom/` directly.
        adhoc = bool(args.get("adhoc"))
        if self.dry_run:
            from playlist_edit import at_root, custom_path, own_path
            with at_root(self.root):
                planned = custom_path(name)
                own_planned = own_path(name)
            target = planned if not adhoc else own_planned
            self.ctx.last_playlist = planned.stem
            self.ctx.last_tracks = selection.tracks[:80]
            self.ctx.last_moods = dict(moods)
            self.ctx.current = selection.tracks[0] if selection.tracks else None
            return Reply(
                f"**{_escape(planned.stem)}** · {selection.summary()}\n\n"
                f"Dry run — would write "
                f"`{target.relative_to(self.root)}` "
                f"with {len(selection.tracks)} tracks.\n\n"
                + _number_tracks(selection.tracks, 5),
                tracks=selection.tracks, tool="make-playlist · dry run")

        if adhoc:
            queue = self._temp_queue(selection.tracks,
                                     "ad-hoc: " + selection.summary(),
                                     name=name)
            self.ctx.last_tracks = selection.tracks[:80]
            self.ctx.last_moods = dict(moods)
            self.ctx.current = selection.tracks[0] if selection.tracks else None
            played = False
            play_note = ""
            if autoplay:
                played, message = self._play_path(queue)
                play_note = ("\n\n▶ playing it now" if played
                             else f"\n\n_Playback: {message}_")
            artists: dict[str, int] = {}
            for track in selection.tracks:
                if track.artist:
                    artists[track.artist] = artists.get(track.artist, 0) + 1
            top = ", ".join(f"{_escape(name)} ({count})"
                            for name, count in sorted(artists.items(),
                                                      key=lambda kv: -kv[1])[:4])
            text = (f"**{_escape(name)}** · {selection.summary()}\n\n"
                    f"An ad-hoc queue in `{queue.relative_to(self.root)}` — "
                    "say “save it” to keep it, or “discard it” to remove it."
                    + (f"\nArtists: {top}" if top else "")
                    + play_note
                    + ("\n\n" + "\n".join(f"- {note}"
                                          for note in selection.notes)
                       if selection.notes else "")
                    + "\n\n" + _number_tracks(selection.tracks, 5))
            return Reply(text, tracks=selection.tracks, played=played)

        try:
            from playlist_edit import at_root, write_custom
            with at_root(self.root):
                path = write_custom(
                    name, [track.rel for track in selection.tracks],
                    overwrite=overwrite)
        except SystemExit as exc:
            return Reply(str(exc), error=True)

        self.ctx.last_playlist = path.stem
        self.ctx.last_tracks = selection.tracks[:80]
        self.ctx.last_moods = dict(moods)
        self.ctx.current = selection.tracks[0] if selection.tracks else None
        self.profile.record_playlist(path.stem, moods, len(selection.tracks),
                                     request)
        for mood, weight in moods.items():
            if weight >= 0.5:
                self.profile.like_mood(mood, 0.1)

        played = False
        play_note = ""
        if autoplay and not self.dry_run:
            played, message = self._play_path(path)
            play_note = ("\n\n▶ playing it now" if played
                         else f"\n\n_Playback: {message}_")

        artists: dict[str, int] = {}
        for track in selection.tracks:
            if track.artist:
                artists[track.artist] = artists.get(track.artist, 0) + 1
        top = ", ".join(f"{_escape(name)} ({count})"
                        for name, count in sorted(artists.items(),
                                                  key=lambda kv: -kv[1])[:4])
        text = (f"**{_escape(path.stem)}** · {selection.summary()}\n\n"
                f"Saved `{path.relative_to(self.root)}`."
                + (f"\nArtists: {top}" if top else "")
                + play_note
                + ("\n\n" + "\n".join(f"- {note}" for note in selection.notes)
                   if selection.notes else "")
                + "\n\n" + _number_tracks(selection.tracks, 5))
        return Reply(text, tracks=selection.tracks, played=played)

    # -- skills: recommendations, playlists and history --------------------

    @staticmethod
    def _ago(stamp: str) -> str:
        try:
            then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return ""
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc)
                              - then).total_seconds()))
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} min ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} h ago"
        return f"{hours // 24} d ago"

    def _event_label(self, value: str) -> str:
        value = str(value or "")
        if value and Path(value).suffix.lower() in AUDIO_EXT:
            track = self.catalog.track(value)
            return track.label() if track is not None else Path(value).stem
        return value

    def activity_data(self, limit: int = 40) -> dict:
        """Everything the activity view shows, as plain data."""
        try:
            limit = int(limit or 40)
        except (TypeError, ValueError):
            limit = 40
        limit = max(5, min(200, limit))
        data = self.profile.data
        events = []
        for event in (data.get("events") or [])[:limit]:
            events.append({
                "kind": str(event.get("kind") or ""),
                "value": str(event.get("value") or ""),
                "label": self._event_label(str(event.get("value") or "")),
                "source": str(event.get("source") or ""),
                "at": str(event.get("at") or ""),
                "ago": self._ago(str(event.get("at") or "")),
            })
        playlists = []
        for row in (data.get("playlists") or [])[:limit]:
            playlists.append({
                "name": str(row.get("name") or ""),
                "count": int(row.get("count") or 0),
                "moods": dict(row.get("moods") or {}),
                "request": str(row.get("request") or ""),
                "at": str(row.get("at") or ""),
                "ago": self._ago(str(row.get("at") or "")),
            })
        recent = []
        for row in (data.get("recent") or [])[:limit]:
            rel = str(row.get("rel") or "")
            track = self.catalog.track(rel) if rel else None
            recent.append({
                "rel": rel,
                "label": track.label() if track is not None else Path(rel).stem,
                "fraction": float(row.get("fraction") or 0),
                "at": str(row.get("at") or ""),
                "ago": self._ago(str(row.get("at") or "")),
            })
        downloads = []
        try:
            for record in self.youtube_jobs()[:limit]:
                downloads.append({
                    "id": str(record.get("id") or ""),
                    "label": str(record.get("display")
                                 or record.get("url") or "Download"),
                    "channel": str(record.get("channel") or ""),
                    "url": str(record.get("url") or ""),
                    "status": youtube.status_line(record),
                    "raw_status": str(record.get("status") or "unknown"),
                    "summary": str(record.get("summary") or ""),
                    "artist": str(record.get("artist") or ""),
                    "album": str(record.get("album") or ""),
                    "at": str(record.get("created") or ""),
                    "ago": self._ago(str(record.get("created") or "")),
                })
        except OSError:
            downloads = []
        changes = []
        for row in self.recent_changes(limit):
            row = dict(row)
            row["ago"] = self._ago(row["ts"])
            changes.append(row)
        return {
            "events": events,
            "playlists": playlists,
            "recent": recent,
            "changes": changes,
            "downloads": downloads,
            "agent": {
                "requests": self.agent.count(),
                "learned": [data.get("name") or data.get("skill")
                            for data in self.agent.learned()],
            },
            "profile": {
                "artists": len(data.get("artists") or {}),
                "genres": len(data.get("genres") or {}),
                "moods": len(data.get("moods") or {}),
                "notes": len(data.get("notes") or []),
                "mappings": len(data.get("mappings") or {}),
            },
        }

    def profile_data(self, limit: int = 40) -> dict:
        """What the app has learned about the listener, as plain data.

        The Profile page in Settings renders exactly this, and so does
        `GET /api/profile`: every learned weight with the bounded bias the
        selection really applies (`tanh(weight / 2.5)`, so `+1` is a strong
        favourite), the notes, the learned phrases, the recent plays and the
        newest events.  Read-only — nothing here writes to the profile or to a
        music file.
        """
        try:
            limit = int(limit or 40)
        except (TypeError, ValueError):
            limit = 40
        limit = max(5, min(400, limit))
        data = self.profile.data

        def bucket_rows(bucket: str) -> list[dict]:
            table = data.get(bucket) or {}
            rows = []
            if isinstance(table, dict):
                for name, value in table.items():
                    try:
                        weight = float(value)
                    except (TypeError, ValueError):
                        continue
                    rows.append({
                        "name": str(name),
                        "weight": round(weight, 2),
                        "bias": round(self.profile.bias(weight), 3),
                    })
            rows.sort(key=lambda row: -abs(row["weight"]))
            return rows

        weights = {bucket: bucket_rows(bucket)
                   for bucket in ("artists", "genres", "moods", "keywords")}
        notes = []
        for note in (data.get("notes") or [])[:limit]:
            notes.append({
                "text": str(note.get("text") or ""),
                "at": str(note.get("at") or ""),
                "ago": self._ago(str(note.get("at") or "")),
            })
        mappings = []
        table = data.get("mappings") or {}
        if isinstance(table, dict):
            for phrase, moods in table.items():
                mappings.append({
                    "phrase": str(phrase),
                    "moods": [str(mood) for mood in (moods or [])],
                })
        recent = []
        for row in (data.get("recent") or [])[:limit]:
            rel = str(row.get("rel") or "")
            track = self.catalog.track(rel) if rel else None
            recent.append({
                "rel": rel,
                "label": track.label() if track is not None else Path(rel).stem,
                "artist": track.artist if track is not None else "",
                "genre": track.genre if track is not None else "",
                "fraction": round(float(row.get("fraction") or 0), 3),
                "at": str(row.get("at") or ""),
                "ago": self._ago(str(row.get("at") or "")),
            })
        events = []
        for event in (data.get("events") or [])[:limit]:
            events.append({
                "kind": str(event.get("kind") or ""),
                "value": str(event.get("value") or ""),
                "label": self._event_label(str(event.get("value") or "")),
                "source": str(event.get("source") or ""),
                "changes": [str(item) for item in (event.get("changes") or [])],
                "at": str(event.get("at") or ""),
                "ago": self._ago(str(event.get("at") or "")),
            })
        playlists = []
        for row in (data.get("playlists") or [])[:limit]:
            playlists.append({
                "name": str(row.get("name") or ""),
                "count": int(row.get("count") or 0),
                "request": str(row.get("request") or ""),
                "at": str(row.get("at") or ""),
                "ago": self._ago(str(row.get("at") or "")),
            })
        try:
            learned = [{
                "name": str(item.get("name") or item.get("skill") or ""),
                "skill": str(item.get("skill") or ""),
                "triggers": [str(t) for t in (item.get("triggers") or [])],
            } for item in self.agent.learned()]
        except OSError:
            learned = []
        path = self.profile.path
        return {
            "created": str(data.get("created") or ""),
            "updated": str(data.get("updated") or ""),
            "updated_ago": self._ago(str(data.get("updated") or "")),
            "path": str(path) if path else "",
            "rel_path": ("library-work/app/chat/profile.json" if path else ""),
            "loaded": bool(self.profile.loaded),
            "writable": bool(self.profile.writable),
            "weights": weights,
            "counts": {
                "artists": len(weights["artists"]),
                "genres": len(weights["genres"]),
                "moods": len(weights["moods"]),
                "keywords": len(weights["keywords"]),
                "positive": sum(1 for rows in weights.values()
                                for row in rows if row["weight"] > 0),
                "negative": sum(1 for rows in weights.values()
                                for row in rows if row["weight"] < 0),
                "notes": len(data.get("notes") or []),
                "mappings": len(mappings),
                "recent": len(data.get("recent") or []),
                "events": len(data.get("events") or []),
                "playlists": len(data.get("playlists") or []),
                "learned": len(learned),
            },
            "notes": notes,
            "mappings": mappings,
            "recent": recent,
            "events": events,
            "playlists": playlists,
            "learned": learned,
            "agent": {
                "requests": self.agent.count(),
                "learned": len(learned),
            },
        }

    def recent_changes(self, limit: int = 30) -> list[dict]:
        """The newest journalled changes, newest first (read-only)."""
        try:
            return recent.entries(self.root, limit)
        except OSError:
            return []

    def undo_change(self, ts: str) -> dict:
        """Take back one playlist change; audio is never touched."""
        result = recent.undo_playlist(self.root, ts)
        if result.get("ok"):
            self.catalog.reload()
        return result

    def skill_activity(self, args: dict, request: str) -> Reply:
        action = str(args.get("action") or "").strip().lower()
        if action == "undo":
            ts = str(args.get("ts") or "")
            if ts:
                result = self.undo_change(ts)
                return Reply(result.get("message", ""), tool="activity · undo")
            undoable = [row for row in self.recent_changes(80)
                        if row.get("undoable")]
            if not undoable:
                return Reply("There is no playlist change to undo.",
                             tool="activity · undo")
            result = self.undo_change(undoable[0]["ts"])
            return Reply(result.get("message", ""), tool="activity · undo")
        if action == "changes":
            rows = self.recent_changes(25)
            if not rows:
                return Reply("Nothing has changed yet — no journal entry to "
                             "show.", tool="activity · changes")
            lines = ["**Recently changed**", ""]
            for row in rows:
                label = row["label"]
                if row.get("undoable"):
                    label += " · undoable"
                detail = (f" · {row['detail']}" if row.get("detail") else "")
                lines.append(f"- {_escape(row['title'])} · {label}{detail} · "
                             f"{self._ago(row['ts'])}")
            lines.append("\nA playlist change can be taken back with "
                         "“undo the last playlist change”. Tag writes and "
                         "imports keep their own rollback tools.")
            return Reply("\n".join(lines), tool="activity · changes")
        return self._activity_report(args, request)

    def _activity_report(self, args: dict, request: str) -> Reply:
        raw = args.get("limit") or 20
        try:
            limit = max(5, min(100, int(raw)))
        except (TypeError, ValueError):
            limit = 20
        data = self.activity_data(limit)
        lines = []
        if data["playlists"]:
            lines.append("**Playlists made**")
            for row in data["playlists"][:8]:
                extra = f" · “{_short(row['request'], 40)}”" if row["request"] else ""
                lines.append(f"- **{_escape(row['name'])}** · "
                             f"{row['count']} tracks · {row['ago']}{extra}")
        if data["events"]:
            lines.append("\n**Events**")
            for event in data["events"][:limit]:
                who = f" · {event['source']}" if event["source"] else ""
                lines.append(f"- {event['kind']} · "
                             f"{_escape(_short(event['label'], 60))} · "
                             f"{event['ago']}{who}")
        if data["recent"]:
            lines.append("\n**Recently played**")
            for row in data["recent"][:8]:
                heard = int(round(row["fraction"] * 100))
                lines.append(f"- {_escape(_short(row['label'], 70))} · "
                             f"{heard}% · {row['ago']}")
        if data.get("downloads"):
            lines.append("\n**Downloads**")
            for row in data["downloads"][:8]:
                detail = (f" · {_short(row['summary'], 60)}"
                          if row["summary"] else "")
                lines.append(f"- `{row['status']}` · "
                             f"{_escape(_short(row['label'], 60))} · "
                             f"{row['ago']}{detail}")
        if not lines:
            lines.append("Nothing recorded yet — play something, make a "
                         "playlist or say “I like this” and it will show up "
                         "here.")
        lines.append("\n`/agent` shows requests I did not understand; "
                     "`/learn` shows the taste profile.")
        return Reply("\n".join(lines), tool="activity")

    def _recommend_name(self, moods: dict[str, float],
                        request: str) -> str:
        name = self._playlist_name("", moods, request)
        if len(moods) >= 3 and name in {"Mix", "Recommended Mix"}:
            return "Recommendation"
        return name

    def skill_recommend(self, args: dict, request: str) -> Reply:
        seed = self._resolve_seed(args.get("seed"))
        moods = self._normalise_moods(args.get("moods"), request)
        include = [str(g) for g in (args.get("include_genres") or [])]
        exclude = [str(g) for g in (args.get("exclude_genres") or [])]
        only = [str(g) for g in (args.get("only_genres") or [])]
        genre_basis = [genre for genre in (only or include) if genre]
        if not moods and genre_basis:
            moods = moods_for_genres(genre_basis)
        if not moods and seed is not None:
            moods = self.selector.seed_moods(seed)
        if not moods:
            moods = moods_from_text(request, self.profile)
        broad = False
        if not moods:
            # "surprise me": a spread rather than a vertical genre slice
            moods = {"Chill": 0.30, "Happy": 0.25, "Epic": 0.25,
                     "Workout": 0.20}
            broad = True
        try:
            count = int(args.get("count") or 12)
        except (TypeError, ValueError):
            count = 12
        count = max(3, min(40, count))
        selection = self.selector.build(
            moods, count=count, seed=seed, include_genres=include,
            exclude_genres=exclude, only_genres=only, allow_recent=False,
            request=request)
        if not selection.tracks:
            hint = ""
            if exclude:
                hint = " Try without “" + "”, “".join(exclude) + "”."
            return Reply("I found nothing that fits that recommendation."
                         + hint, error=True)

        blend = " + ".join(mood for mood, _ in sorted(
            selection.moods.items(), key=lambda item: -item[1]))
        why = f"**{_escape(blend or 'Mix')}**"
        if genre_basis:
            why += " · " + ", ".join(_escape(genre) for genre in genre_basis)
        if broad:
            why += " · a bit of everything"

        self.ctx.last_tracks = selection.tracks[:80]
        self.ctx.last_moods = dict(selection.moods)
        name = ("Recommendation" if broad
                else self._recommend_name(selection.moods, request))
        self.ctx.last_recommendation = {
            "tracks": [track.rel for track in selection.tracks],
            "moods": dict(selection.moods),
            "summary": selection.summary(),
            "request": request,
            "name": name,
            "at": datetime.now(timezone.utc).isoformat(),
        }

        played = False
        play_note = ""
        if bool(args.get("autoplay")):
            queue = self._temp_queue(selection.tracks,
                                     "recommendation: " + selection.summary(),
                                     name=name)
            played, message = self._play_path(queue)
            if played:
                self.ctx.current = selection.tracks[0]
                play_note = "\n\n▶ playing the first one"
            else:
                play_note = f"\n\n_{message}_"

        hint = ("\n\nSay “play number 1”, “save that playlist”"
                + (" or “more like this”" if played else "")
                + ".")
        return Reply(
            f"**Recommended** · {selection.summary()}\n{why}\n\n"
            + _number_tracks(selection.tracks, 8) + hint + play_note,
            tracks=selection.tracks, played=played)

    def skill_save_playlist(self, args: dict, request: str) -> Reply:
        name = str(args.get("name") or "").strip()
        recommendation = self.ctx.last_recommendation or {}
        own = self._own_view()
        rec_at = str(recommendation.get("at") or "")
        own_at = str(self.ctx.last_own_at or "")
        # the queue behind "play something …" wins when it is newer than the
        # last recommendation, or when the listener clearly means "this one"
        prefer_own = bool(own) and (
            bool(args.get("own")) or not recommendation
            or (own_at and own_at > rec_at))
        if prefer_own:
            rels = list(own.get("tracks") or [])
            tracks = [track for track in (self.catalog.track(rel)
                                          for rel in rels)
                      if track is not None]
            moods = dict(self.ctx.last_moods or {})
        else:
            rels = list(recommendation.get("tracks") or [])
            moods = dict(recommendation.get("moods")
                         or self.ctx.last_moods or {})
            tracks = [track for track in (self.catalog.track(rel)
                                          for rel in rels)
                      if track is not None]
        if not tracks:
            tracks = list(self.ctx.last_tracks)
        if not tracks and self.ctx.current is not None:
            tracks = [self.ctx.current]
        if not tracks:
            return Reply("There is nothing to save yet — ask for a "
                         "recommendation, a search or a playlist first.",
                         error=True)
        if not name:
            if prefer_own:
                name = re.sub(r"_\d{8}-\d{6}$", "", str(own.get("name") or ""))
            name = (name or str(recommendation.get("name") or "")
                    or self._playlist_name("", moods, request) or "Saved Mix")
        overwrite = bool(args.get("overwrite"))
        if self.dry_run:
            from playlist_edit import at_root, custom_path
            with at_root(self.root):
                planned = custom_path(name)
            return Reply(f"Dry run — would save "
                         f"`{planned.relative_to(self.root)}` with "
                         f"{len(tracks)} tracks.", tool="save-playlist · dry run",
                         tracks=tracks[:12])
        try:
            from playlist_edit import at_root, write_custom
            with at_root(self.root):
                path = write_custom(name, [track.rel for track in tracks],
                                    overwrite=overwrite)
                if prefer_own and self.ctx.last_own:
                    # the ad-hoc copy has done its job; journalled like any
                    # other playlist delete, so `--rollback` brings it back
                    from playlist_edit import delete_own
                    delete_own(self.ctx.last_own)
        except SystemExit as exc:
            return Reply(str(exc), error=True)
        self.ctx.last_own = ""
        self.ctx.last_own_at = ""
        self.catalog.playlists(refresh=True)
        self.ctx.last_playlist = path.stem
        self.ctx.last_tracks = tracks[:80]
        self.ctx.last_moods = dict(moods)
        self.profile.record_playlist(path.stem, moods, len(tracks), request)
        total = human_duration(sum(track.duration_s for track in tracks))
        removed = (" The ad-hoc copy is gone." if prefer_own else "")
        return Reply(
            f"Saved **{_escape(path.stem)}** · {len(tracks)} tracks · {total}\n"
            f"`{path.relative_to(self.root)}`{removed}\n\n"
            f"Say “play {path.stem}” to hear it, or “show {path.stem}”.",
            tracks=tracks[:20], tool="save-playlist")

    def discard_own_playlist(self) -> Reply:
        """Throw away the ad-hoc list behind the current queue.

        Only `Playlists/Own/` is touched, and the delete is journalled, so the
        entries are recoverable; the music itself is never involved.
        """
        own = self._own_view()
        if not own:
            return Reply("There is no ad-hoc playlist to remove — I make one "
                         "when you ask me to play something built from your "
                         "request.", error=True, tool="edit-playlist")
        if self.dry_run:
            return Reply(f"Dry run — would remove `{own['rel']}` "
                         f"({own['count']} tracks).", tool="edit-playlist · dry run")
        try:
            from playlist_edit import at_root, delete_own
            with at_root(self.root):
                delete_own(own["rel"])
        except SystemExit as exc:
            return Reply(str(exc), error=True)
        self.ctx.last_own = ""
        self.ctx.last_own_at = ""
        self.catalog.playlists(refresh=True)
        return Reply(
            f"Removed the ad-hoc list **{_escape(own['name'])}** · "
            f"{own['count']} tracks. The music and every saved playlist are "
            "untouched; the entries stay in the playlist journal.",
            tool="edit-playlist")

    def skill_show_playlists(self, args: dict, request: str) -> Reply:
        playlists = self.catalog.playlists(refresh=True)
        groups: dict[str, list[Playlist]] = {}
        for playlist in playlists:
            groups.setdefault(playlist.kind, []).append(playlist)
        total = sum(playlist.count for playlist in playlists)
        lines = [f"**{len(playlists)} playlists** · {total:,} entries"]
        for kind, title in (("custom", "Mine"), ("own", "Ad-hoc (not kept)"),
                            ("smart", "Smart (rule-based)"),
                            ("shelf", "Shelves"), ("psycho", "Sound families"),
                            ("mood", "Moods"), ("focus", "Focus")):
            rows = groups.get(kind, [])
            if not rows:
                continue
            lines.append(f"\n**{title}** ({len(rows)})")
            for playlist in rows[:40]:
                word = "track" if playlist.count == 1 else "tracks"
                lines.append(f"- {_escape(playlist.name)} · "
                             f"{playlist.count:,} {word}")
            if len(rows) > 40:
                lines.append(f"… and {len(rows) - 40} more")
        if not groups.get("custom"):
            lines.append("\nNo custom lists yet — ask for a recommendation "
                         "and say “save that playlist”.")
        if groups.get("own"):
            lines.append("\nThe ad-hoc lists under **Ad-hoc** are queues I "
                         "built for one request: say “save it” to keep one or "
                         "“discard it” to remove it.")
        lines.append("\nSay “show <name>”, “play <name>” or “save that "
                     "playlist”.")
        return Reply("\n".join(lines), tool="show-playlists")

    def _smart_axes(self, rules: dict) -> dict | None:
        """The profile axes, but only when a rule really needs them."""
        if not (rules or {}).get("axes"):
            return None
        try:
            return self.psycho.load_axes()
        except Exception:                       # noqa: BLE001 - never fatal
            return None

    def skill_smart(self, args: dict, request: str) -> Reply:
        """Saved rules that rebuild a playlist from the inventory."""
        from . import smart

        action = str(args.get("action") or "list").casefold()
        name = smart.clean_name(str(args.get("name") or "").strip()) \
            if str(args.get("name") or "").strip() else ""
        try:
            if action in {"list", "show"} and not (action == "show" and name):
                saved = smart.definitions(self.root)
                if not saved:
                    return Reply(
                        "No smart playlists yet.  A smart list keeps itself "
                        "current from a rule — say “make a smart playlist of "
                        "90s metal” or “a smart playlist of everything I "
                        "added this month”.", tool="smart-playlist · list")
                lines = [f"**{len(saved)} smart playlist"
                         f"{'s' if len(saved) != 1 else ''}**"]
                for item, entry in saved.items():
                    count = entry.get("count")
                    lines.append(
                        f"- **{_escape(item)}** · {smart.describe(entry['rules'])}"
                        + (f" · {int(count):,} tracks" if count is not None
                           else ""))
                lines.append("\nA smart list is rebuilt when it is played; say "
                             "“refresh the smart playlists” to rebuild them "
                             "all now, or “play <name>”.")
                return Reply("\n".join(lines), tool="smart-playlist · list")

            if action == "show":
                saved = smart.definitions(self.root)
                entry = saved.get(name)
                if entry is None:
                    return Reply(f"There is no smart playlist called "
                                 f"“{_escape(name)}”.", error=True)
                rules = entry["rules"]
                lines = [f"**{_escape(name)}** · {smart.describe(rules)}",
                         f"{int(entry.get('count') or 0):,} tracks · "
                         f"`{entry.get('path') or ''}`"]
                lines.append("\nRules:")
                for key in sorted(rules):
                    value = rules[key]
                    if isinstance(value, dict):
                        value = ", ".join(f"{axis} ≥ {round(floor * 100)}%"
                                          for axis, floor in value.items())
                    elif isinstance(value, list):
                        value = ", ".join(value)
                    lines.append(f"- `{key}`: {_escape(str(value))}")
                if entry.get("updated"):
                    lines.append(f"\nLast rebuilt: {_escape(str(entry['updated']))}")
                return Reply("\n".join(lines), tool="smart-playlist · show")

            if action == "refresh":
                result = smart.build_all(
                    self.root, rows=self.catalog.rows,
                    axes=self._smart_axes({"axes": True}),
                    dry_run=self.dry_run)
                self.catalog.playlists(refresh=True)
                if not result["lists"]:
                    return Reply("There are no smart playlists to rebuild.",
                                 tool="smart-playlist · refresh")
                if self.dry_run:
                    return Reply(
                        f"Dry run — would rebuild {result['count']} smart "
                        f"playlist{'s' if result['count'] != 1 else ''} over "
                        f"{result['total']:,} tracks.", tool="smart-playlist")
                lines = [f"Rebuilt **{result['count']} smart playlist"
                         f"{'s' if result['count'] != 1 else ''}** · "
                         f"{result['tracks']:,} entries"]
                for item in result["lists"]:
                    lines.append(f"- **{_escape(item['name'])}** · "
                                 f"{item['count']:,} tracks · "
                                 f"{_escape(smart.describe(item['rules']))}")
                return Reply("\n".join(lines), tool="smart-playlist · refresh")

            if action == "delete":
                if not name:
                    return Reply("Which smart playlist should I delete?",
                                 error=True)
                # "remove" alone stays an edit of entries ("remove 2 from
                # Mix"), so deleting a whole generated list needs an
                # unambiguous word
                told = " " + fold(request) + " "
                clear = any(word in told for word in (
                    " delete", " drop", " discard", " throw", " loesche",
                    " losche", " loschen", " entsorg", " verwirf",
                    " schmeiss", " schmeiß", " wegwerf"))
                if not clear:
                    return Reply("Say “delete” if you really want to remove "
                                 f"**{_escape(name)}** — the music stays.",
                                 error=True)
                result = smart.remove(self.root, name)
                self.catalog.playlists(refresh=True)
                return Reply(f"Removed the smart playlist "
                             f"**{_escape(name)}**"
                             + (" and its file" if result["removed"] else "")
                             + ". The tracks themselves are untouched and the "
                               "list is in the playlist journal.",
                             tool="smart-playlist · delete")

            # -- build / rebuild ------------------------------------------
            rules = dict(args.get("rules") or {})
            for key in ("genre", "artist", "album", "grouping", "codec",
                        "not_genre", "not_artist", "decade", "year_from",
                        "year_to", "added_days", "min_duration",
                        "max_duration", "min_bitrate", "max_bitrate",
                        "axes", "sort", "limit"):
                if args.get(key) not in (None, "", [], {}):
                    rules[key] = args[key]
            try:
                clean = smart.normalise(rules)
            except ValueError as exc:
                return Reply(str(exc), error=True)
            name = name or smart.clean_name(smart.suggest_name(clean))
            existing = smart.definitions(self.root).get(name)
            if existing is not None and not (
                    bool(args.get("overwrite"))
                    or bool(args.get("rebuild"))):
                # the same rule under the same name is a rebuild, a different
                # rule replaces it only when the request says so
                if existing["rules"] != clean:
                    told = " " + fold(request) + " "
                    replaces = any(word in told for word in (
                        " replace", " change", " update", " ersetze",
                        " andere", " rebuild", " neu bauen", " neu erstellen"))
                    if not replaces:
                        return Reply(
                            f"**{_escape(name)}** already exists with "
                            f"different rules ({smart.describe(existing['rules'])})."
                            " Say “replace it” to change it, or give the list "
                            "another name.", error=True)
            try:
                result = smart.build(
                    self.root, name, clean, rows=self.catalog.rows,
                    axes=self._smart_axes(clean), dry_run=self.dry_run)
            except (OSError, ValueError, SystemExit) as exc:
                return Reply(f"The smart playlist could not be written: {exc}",
                             error=True)
            self.catalog.playlists(refresh=True)
            self.ctx.last_playlist = name
            head = (f"**{_escape(name)}** · {result['count']:,} tracks · "
                    f"{_escape(result['description'])}")
            if self.dry_run:
                return Reply(head + "\n\nDry run — nothing was written.",
                             tool="smart-playlist · dry run")
            shown = self.catalog.playlist_tracks(
                Playlist(name, self.root / result["written"], "smart",
                         result["count"]))
            body = ("\n\n" + _number_tracks(shown[:8], 8)) \
                if shown else "\n\n_No track matches those rules yet._"
            return Reply(head
                         + f"\n\nA smart list: it is rebuilt from the rules "
                           f"whenever it is played.  `{result['written']}`"
                         + body
                         + "\n\nSay “play it” to listen, or “show the smart "
                           "playlist” to see the rules.",
                         tracks=shown[:8], tool="smart-playlist · build")
        except SystemExit as exc:
            return Reply(str(exc), error=True)

    def skill_agent(self, args: dict, request: str) -> Reply:
        action = str(args.get("action") or "show").casefold()
        if action == "approve":
            ok, message = self.agent.approve()
            return Reply(message, error=not ok, tool="agent")
        if action == "reject":
            ok, message = self.agent.reject()
            return Reply(message, error=not ok, tool="agent")
        return Reply(self.agent.summary_markdown(), tool="agent")

    # -- skills: playlist editing -----------------------------------------

    def skill_edit_playlist(self, args: dict, request: str) -> Reply:
        action = str(args.get("action") or "list").casefold()
        if action == "discard":
            return self.discard_own_playlist()
        if action == "list":
            groups: dict[str, list[Playlist]] = {}
            for playlist in self.catalog.playlists(refresh=True):
                groups.setdefault(playlist.kind, []).append(playlist)
            lines = []
            for kind in ("custom", "own", "shelf", "mood", "focus"):
                rows = groups.get(kind, [])
                if not rows:
                    continue
                lines.append(f"**{kind}** ({len(rows)})")
                for playlist in rows:
                    lines.append(f"- {_escape(playlist.name)} · "
                                 f"{playlist.count:,} tracks")
            return Reply("\n".join(lines) or "No playlists yet.",
                         tool="edit-playlist")

        name = str(args.get("playlist") or "").strip()
        playlist = self.catalog.find_playlist(name)
        if action == "show":
            if playlist is None:
                return Reply(f"No playlist called “{_escape(name)}”.", error=True)
            tracks = self.catalog.playlist_tracks(playlist)
            self.ctx.last_tracks = tracks[:100]
            self.ctx.last_playlist = playlist.name
            return Reply(f"**{_escape(playlist.name)}** · {len(tracks)} tracks"
                         f"\n\n{_number_tracks(tracks, 40)}",
                         tracks=tracks, tool="edit-playlist")

        if action == "add":
            if playlist is not None and playlist.kind == "own":
                return Reply(
                    f"**{_escape(playlist.name)}** is an ad-hoc queue, not a "
                    "saved list. Say “save it” first and I will keep it under "
                    "a name you choose.", error=True)
            if playlist is not None and playlist.kind != "custom":
                return Reply(f"**{_escape(playlist.name)}** is generated from "
                             "`GROUPING` and would be rebuilt. Ask me for a "
                             "custom list instead, for example “create a "
                             f"{playlist.name.lower()} playlist”.", error=True)
            rels = self._resolve_track_args(args.get("tracks"))
            unresolved = [str(value) for value in (args.get("unresolved") or [])
                          if str(value).strip()]
            if not rels:
                track = self.ctx.current or (self.ctx.last_tracks[0]
                                             if self.ctx.last_tracks else None)
                rels = [track.rel] if track else []
            if not rels:
                hint = (" I could not find: "
                        + ", ".join(f"“{_escape(value)}”"
                                    for value in unresolved[:6]) + "."
                        if unresolved else "")
                return Reply("Which track should I add?" + hint, error=True)
            labels = []
            for rel in rels:
                found = self.catalog.track(rel)
                labels.append(found.label() if found is not None
                              else Path(rel).stem)
            try:
                from playlist_edit import at_root, append_custom, write_custom
                target = playlist or self.catalog.find_playlist(name)
                with at_root(self.root):
                    if target is None or not target.path.exists():
                        path = write_custom(name, rels)
                        added = len(rels)
                    else:
                        path, added = append_custom(target.name, rels)
            except SystemExit as exc:
                return Reply(str(exc), error=True)
            missing = len(rels) - added
            body = f"Added {added} track{'s' if added != 1 else ''} to " \
                   f"**{_escape(path.stem)}** " \
                   f"(`{path.relative_to(self.root)}`)."
            if labels:
                body += "\n\n" + "\n".join(
                    f"- {_escape(_short(label, 70))}" for label in labels[:10])
                if len(labels) > 10:
                    body += f"\n- … and {len(labels) - 10} more"
            if missing > 0:
                body += (f"\n\n{missing} "
                         f"{'was' if missing == 1 else 'were'} already in the "
                         "list.")
            if unresolved:
                body += ("\n\nNot found: "
                         + ", ".join(f"“{_escape(value)}”"
                                     for value in unresolved[:6]) + ".")
            return Reply(body, tool="edit-playlist")

        if action == "remove":
            if playlist is None:
                return Reply(f"No playlist called “{_escape(name)}”.", error=True)
            numbers = [int(n) for n in (args.get("indexes") or [])
                       if str(n).isdigit()]
            match = str(args.get("match") or "").strip()
            if not numbers and match:
                # a title instead of an entry number: find the entries whose
                # track matches, so "remove X from Y" works like a listener says it
                rels = set(self._resolve_track_args([match]))
                if rels:
                    hits = [index + 1 for index, track
                            in enumerate(self.catalog.playlist_tracks(playlist))
                            if track.rel in rels]
                    numbers = hits[:20]
                if not numbers:
                    return Reply(f"“{_escape(match)}” is not in "
                                 f"**{_escape(playlist.name)}**.", error=True)
            if not numbers:
                return Reply("Which entry number should I remove?", error=True)
            try:
                from playlist_edit import at_root, remove_from_custom
                with at_root(self.root):
                    path, removed = remove_from_custom(playlist.name, numbers)
            except SystemExit as exc:
                return Reply(str(exc), error=True)
            return Reply(f"Removed {removed} entr"
                         f"{'y' if removed == 1 else 'ies'} from "
                         f"**{_escape(path.stem)}**.",
                         tool="edit-playlist")

        if action in {"rename", "delete"}:
            if playlist is None:
                return Reply(f"No playlist called “{_escape(name)}”.", error=True)
            if playlist.kind == "own" and action == "delete":
                if not wants_delete(request):
                    return Reply(
                        f"To remove **{_escape(playlist.name)}**, say “delete "
                        f"the playlist {playlist.name}”. Its entries are kept "
                        "in `library-work/journal/playlists.jsonl` either way.",
                        error=True)
                try:
                    from playlist_edit import at_root, delete_own
                    with at_root(self.root):
                        path = delete_own(
                            playlist.path.relative_to(self.root).as_posix())
                except SystemExit as exc:
                    return Reply(str(exc), error=True)
                if str(getattr(self.ctx, "last_own", "")) == \
                        playlist.path.relative_to(self.root).as_posix():
                    self.ctx.last_own = ""
                    self.ctx.last_own_at = ""
                self.catalog.playlists(refresh=True)
                return Reply(
                    f"Removed the ad-hoc list **{_escape(path.stem)}** · "
                    f"{playlist.count:,} tracks. The content is in the "
                    "playlist journal, so `playlist_edit.py --rollback` "
                    "restores it.", tool="edit-playlist")
            if playlist.kind != "custom":
                if playlist.kind == "own":
                    return Reply(
                        f"**{_escape(playlist.name)}** is an ad-hoc queue, not "
                        "a saved list — say “save it” to keep it under a name "
                        "you choose.", error=True)
                return Reply(
                    f"**{_escape(playlist.name)}** is a {playlist.kind} list: "
                    "it is generated from the library, so it cannot be "
                    "renamed or deleted. Ask me for a custom list instead.",
                    error=True)
            if action == "rename":
                target_name = str(args.get("to") or "").strip()
                if not target_name:
                    return Reply("Rename it to what?", error=True)
            elif not wants_delete(request):
                return Reply(
                    f"To delete **{_escape(playlist.name)}**, say “delete the "
                    f"playlist {playlist.name}”. Its entries are kept in "
                    "`library-work/journal/playlists.jsonl` either way.",
                    error=True)
            try:
                from playlist_edit import (at_root, delete_custom,
                                           rename_custom)
                with at_root(self.root):
                    if action == "delete":
                        path = delete_custom(playlist.name)
                    else:
                        path = rename_custom(playlist.name, target_name)
            except SystemExit as exc:
                return Reply(str(exc), error=True)
            self.catalog.playlists(refresh=True)
            if action == "delete":
                return Reply(
                    f"Deleted **{_escape(path.stem)}** · "
                    f"{playlist.count:,} tracks. The content is in the "
                    "playlist journal, so `playlist_edit.py --rollback` "
                    "restores it.", tool="edit-playlist")
            return Reply(
                f"Renamed **{_escape(playlist.name)}** → "
                f"**{_escape(path.stem)}** "
                f"(`{path.relative_to(self.root)}`).", tool="edit-playlist")

        return Reply("Playlist actions: list, show, add, remove, rename, "
                     "delete.", error=True)

    def _resolve_track_args(self, values) -> list[str]:
        rels: list[str] = []
        for value in values or []:
            text = str(value).strip()
            if text.isdigit() and self.ctx.last_tracks:
                index = int(text) - 1
                if 0 <= index < len(self.ctx.last_tracks):
                    rels.append(self.ctx.last_tracks[index].rel)
                continue
            track = self.catalog.track(text)
            if track is not None:
                rels.append(track.rel)
                continue
            hits = self.catalog.search(text, 1)
            if hits:
                rels.append(hits[0].rel)
        return rels

    # -- skills: imports ---------------------------------------------------

    def _run_tool(self, name: str, argv: list[str],
                  timeout: int = 1800) -> tuple[int, str]:
        script = self.root / "scripts" / name
        if not script.is_file():
            return 2, (f"The library has no `scripts/{name}`. Imports need a "
                       "full checkout; a plain music folder can play and "
                       "build playlists, but not tag new files.")
        try:
            result = subprocess.run(
                [PY, str(script), *argv], cwd=str(self.root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 3, f"`{name}` did not finish within {timeout} seconds."
        return result.returncode, result.stdout or ""

    @staticmethod
    def _tail(text: str, lines: int = 14) -> str:
        rows = [line for line in text.splitlines() if line.strip()]
        return "\n".join(rows[-lines:])

    def skill_import(self, args: dict, request: str) -> Reply:
        if not youtube.available(self.root):
            return Reply(
                "This build has no import module installed, so it cannot "
                "search or import audio.  The player itself always works "
                "without one; a build that includes an optional import "
                "module adds the feature under Settings or the module's own "
                "entry point.",
                error=True, tool="import-audio · unavailable")
        action = str(args.get("action") or "").casefold()
        url = str(args.get("url") or "").strip()
        if action == "status":
            return self._youtube_status()
        if action == "retry":
            return self._youtube_retry()
        if action == "dismiss":
            return self._youtube_dismiss()
        if action in {"cancel", "abort"}:
            return self._youtube_cancel()
        if action == "move":
            return self._youtube_move(args)
        if action == "download":
            return self._import_download(args)
        if url and action != "search":
            return self._youtube_direct(url, args)
        return self._import_search(args, request)

    def _import_search(self, args: dict, request: str) -> Reply:
        query = " ".join(str(args.get("query") or "").split())
        similar = bool(args.get("similar"))
        if not query and similar:
            track = self.ctx.current or (self.ctx.last_tracks[0]
                                         if self.ctx.last_tracks else None)
            if track is not None:
                query = f"{track.artist} {track.name}".strip()
        if not query:
            track = self.ctx.current
            if track is None:
                return Reply("What should I look for? For example "
                             "“search for Portishead Glory Box”.", error=True)
            query = f"{track.artist} {track.name}".strip()
        try:
            results = youtube.search(query, int(args.get("count") or 6))
        except RuntimeError as exc:
            return Reply(str(exc), error=True)
        self.ctx.last_youtube = {
            "query": query,
            "at": datetime.now(timezone.utc).isoformat(),
            "results": results,
        }
        if not results:
            return Reply(f"The download module found nothing for "
                         f"“{_escape(query)}”.",
                         error=True, tool="import-audio · search")
        lines = []
        for number, row in enumerate(results, 1):
            bits = []
            if row.get("channel"):
                bits.append(_escape(row["channel"]))
            if row.get("duration_s"):
                bits.append(clock(row["duration_s"]))
            if row.get("views"):
                bits.append(_views(row["views"]))
            tail = (" · " + " · ".join(bits)) if bits else ""
            lines.append(f"{number}. **{_escape(row['title'] or row['url'])}**"
                         f"{tail}\n   {row['url']}")
        return Reply(
            f"**Download hits for “{_escape(query)}”**\n\n"
            + "\n".join(lines)
            + "\n\nSay “download 1” to file one into the library, or "
            "“download it” for the first hit.",
            tool="import-audio · search")

    def _import_download(self, args: dict) -> Reply:
        note = getattr(self.ctx, "last_youtube", None) or {}
        results = list(note.get("results") or [])
        if not results:
            return Reply("Search first — “search for …” — then say "
                         "“download 1”.", error=True)
        try:
            wanted = [int(value) for value in (args.get("indexes") or [])]
        except (TypeError, ValueError):
            wanted = []
        if args.get("all"):
            chosen = results
        elif wanted:
            chosen = [results[index - 1] for index in wanted
                      if 1 <= index <= len(results)]
        else:
            chosen = results[:1]
        if not chosen:
            return Reply(f"Pick a number between 1 and {len(results)}.",
                         error=True, tool="import-audio · download")
        if self.dry_run:
            names = ", ".join(f"“{_escape(row.get('title') or row['url'])}”"
                              for row in chosen)
            return Reply(f"Would queue {names} for import (dry run).",
                         tool="import-audio · download")
        try:
            started = youtube.start(
                self.root, chosen,
                artist=str(args.get("artist") or "").strip(),
                album=str(args.get("album") or "").strip(),
                genre=str(args.get("genre") or "").strip(),
                grouping=str(args.get("grouping") or "Downloads"))
        except (OSError, RuntimeError) as exc:
            return Reply(f"That import could not start: {exc}", error=True)
        for record in started:
            self._youtube_pending.add(str(record.get("id") or ""))
        names = ", ".join(f"**{_escape(record.get('display') or 'Download')}**"
                          for record in started[:6])
        if len(started) > 6:
            names += f" and {len(started) - 6} more"
        running = any(record.get("status") == "running" for record in started)
        state = ("the import runs in the background" if running
                 else "it is queued behind a running import")
        return Reply(
            f"Queued {names} for the library — {state}. I tag the file, file "
            "it into the tree, rescan the inventory and rebuild the "
            "playlists; that takes a few minutes.\n\n"
            "Ask “download status” to see how it went.",
            tool="import-audio · download")

    def _youtube_direct(self, url: str, args: dict) -> Reply:
        # several links may arrive in one sentence or as one pasted block
        links: list[str] = []
        for candidate in [*(args.get("urls") or []), url]:
            link = str(candidate or "").strip()
            if link and link not in links:
                links.append(link)
        links = [link for link in links
                 if re.match(r"^https?://", link, re.IGNORECASE)]
        if not links:
            return Reply("Give me a full link "
                         "(`https://…`).", error=True)
        if self.dry_run:
            names = ", ".join(f"`{link}`" for link in links[:4])
            more = f" and {len(links) - 4} more" if len(links) > 4 else ""
            return Reply(f"Would queue {names}{more} for import (dry run).",
                         tool="import-audio · download")
        # a link that is already waiting or running is not queued twice
        known = {str(record.get("url") or "")
                 for record in youtube.jobs(self.root)
                 if record.get("status") in {"running", "queued"}}
        fresh = [link for link in links if link not in known]
        skipped = [link for link in links if link in known]
        if not fresh:
            return Reply("That link is already in the download queue.",
                         tool="import-audio · download")
        # a bare link has no artist or title: ask the module once per link so
        # the import is filed under the performer instead of the URL
        candidates = [youtube.probe(link) or {
            "url": link, "title": "", "channel": "", "duration_s": 0}
            for link in fresh]
        try:
            started = youtube.start(
                self.root, candidates,
                artist=str(args.get("artist") or "").strip(),
                album=str(args.get("album") or "").strip(),
                genre=str(args.get("genre") or "").strip(),
                grouping=str(args.get("grouping") or "Downloads"))
        except (OSError, RuntimeError) as exc:
            return Reply(f"That import could not start: {exc}", error=True)
        for record in started:
            self._youtube_pending.add(str(record.get("id") or ""))
        if len(started) == 1 and not skipped:
            body = ("Queued the link for the library — I download, tag and "
                    "file it, then rescan and rebuild the playlists.")
        else:
            body = (f"Queued **{len(started)} import"
                    f"{'s' if len(started) != 1 else ''}** — one downloads "
                    "now, the rest wait in the downloads panel, where you can "
                    "reorder or cancel them.")
            if skipped:
                body += (f" {len(skipped)} link"
                         f"{'s were' if len(skipped) != 1 else ' was'} already "
                         "in the queue.")
        return Reply(body + "\n\nAsk “download status” to see how it went.",
                     tool="import-audio · download")

    def _youtube_cancel(self) -> Reply:
        """Drop a waiting import, or stop the running one."""
        try:
            state = youtube.cancel(self.root)
        except OSError as exc:
            return Reply(f"Could not reach the download queue: {exc}",
                         error=True)
        if not state.get("cancelled"):
            return Reply(str(state.get("note")
                             or "There is no download to cancel."),
                         tool="import-audio · cancel")
        name = _escape(_short(str(state.get("display") or state.get("title")
                                  or state.get("url") or "the download"), 70))
        return Reply(f"Cancelled **{name}**. {state.get('note') or ''}".strip()
                     + "\n\nNo library song was touched; the record and its "
                       "log stay in the downloads panel.",
                     tool="import-audio · cancel")

    def _youtube_move(self, args: dict) -> Reply:
        """Move a waiting import one place up or down the queue."""
        direction = str(args.get("direction") or "up").casefold()
        by = 1 if direction in {"down", "spaeter", "später", "later"} else -1
        job_id = str(args.get("id") or "").strip()
        if not job_id:
            # "move the download up" means the newest waiting one, which is
            # the entry that was just queued behind the others
            waiting = youtube._queued(self.root)
            if len(waiting) < 2:
                return Reply("There is nothing to reorder — "
                             f"{'one import is' if waiting else 'no import is'} "
                             "waiting.", tool="import-audio · queue")
            job_id = str(waiting[-1 if by < 0 else 0].get("id") or "")
        try:
            state = youtube.move(self.root, job_id, by)
        except OSError as exc:
            return Reply(f"Could not reach the download queue: {exc}",
                         error=True)
        if not state.get("moved"):
            return Reply(str(state.get("note")
                             or "That import cannot be moved."),
                         tool="import-audio · queue")
        name = _escape(_short(str(state.get("display") or state.get("title")
                                  or state.get("url") or "the import"), 70))
        return Reply(f"Moved **{name}** to position {state.get('position')} "
                     f"of {state.get('of')} in the queue.",
                     tool="import-audio · queue")

    def _youtube_retry(self) -> Reply:
        """Queue a failed import again, keeping the failed attempt recorded."""
        try:
            state = youtube.retry(self.root)
        except OSError as exc:
            return Reply(f"Could not reach the download queue: {exc}",
                         error=True)
        if not state.get("id"):
            return Reply(str(state.get("note")
                             or "There is no failed download to try again."),
                         tool="import-audio · retry")
        name = _escape(_short(str(state.get("display") or state.get("url")
                                  or "the download"), 70))
        attempts = len(state.get("attempts") or [])
        if state.get("state") in {"running", "queued"}:
            body = (f"Trying **{name}** again — it is "
                    f"{state['state']} in the download queue.")
        elif state.get("status") in {"done", "partial"}:
            body = f"**{name}** is already {youtube.status_line(state)}."
        else:
            body = f"Trying **{name}** again."
        if attempts:
            body += (f"\n\nThe earlier attempt"
                     f"{'s are' if attempts != 1 else ' is'} kept in "
                     "`library-work/app/chat/`.")
        return Reply(body, tool="import-audio · retry")

    def _youtube_dismiss(self) -> Reply:
        """Stop showing a failed import; its record and log stay on disk."""
        try:
            state = youtube.dismiss(self.root)
        except OSError as exc:
            return Reply(f"Could not reach the download queue: {exc}",
                         error=True)
        if not state.get("id"):
            return Reply(str(state.get("note")
                             or "There is no failed download to clear."),
                         tool="import-audio · dismiss")
        name = _escape(_short(str(state.get("display") or state.get("url")
                                  or "the download"), 70))
        return Reply(f"Cleared **{name}** from the footer. The job and its log "
                     "stay in `library-work/app/chat/`, so “download "
                     "status” still lists it.", tool="import-audio · dismiss")

    def _youtube_status(self) -> Reply:
        try:
            records = self.youtube_jobs()
        except OSError as exc:
            return Reply(f"Could not read the download queue: {exc}",
                         error=True)
        if not records:
            return Reply("No audio imports yet. Search the import source, "
                         "then say “download 1”.",
                         tool="import-audio · status")
        lines = []
        for record in records[:10]:
            state = youtube.status_line(record)
            detail = str(record.get("summary") or "").strip()
            name = str(record.get("display") or record.get("url") or "import")
            ago = self._ago(str(record.get("created") or ""))
            extra = f" · {_short(detail, 80)}" if detail else ""
            lines.append(f"- `{state}` · **{_escape(_short(name, 70))}**"
                         f" · {ago}{extra}")
        running = sum(1 for record in records
                      if record.get("status") in {"running", "queued"})
        head = (f"**Audio imports** · {len(records)} job"
                f"{'s' if len(records) != 1 else ''}"
                + (f", {running} pending" if running else ""))
        return Reply(head + "\n\n" + "\n".join(lines),
                     tool="import-audio · status")

    def youtube_jobs(self) -> list[dict]:
        """Job list, reloading the catalogue when one has just finished."""
        found = youtube.jobs(self.root)
        finished = False
        for record in found:
            job_id = str(record.get("id") or "")
            if not job_id:
                continue
            if record.get("status") in youtube.FINISHED:
                finished = finished or job_id in self._youtube_pending
                self._youtube_pending.discard(job_id)
            else:
                self._youtube_pending.add(job_id)
        if finished:
            self.catalog.reload()
        return found

    def skill_add_file(self, args: dict, request: str) -> Reply:
        path = str(args.get("path") or "").strip()
        if not path:
            return Reply("Which file should I add?", error=True)
        if not Path(path).expanduser().exists():
            return Reply(f"`{_escape(path)}` does not exist.", error=True)
        argv = [path]
        for key, flag in (("artist", "--artist"), ("album", "--album"),
                          ("genre", "--genre")):
            value = str(args.get(key) or "").strip()
            if value:
                argv.extend([flag, value])
        code, output = self._run_tool("add_file.py", argv, timeout=900)
        self.catalog.reload()
        body = f"```\n{self._tail(output, 14)}\n```"
        if code == 0:
            return Reply("Filed into the library.\n\n" + body,
                         tool="add-file")
        return Reply("The import did not finish cleanly.\n\n" + body,
                     error=True, tool="add-file")

    # -- skills: playlist import -------------------------------------------

    def skill_import_playlist(self, args: dict, request: str) -> Reply:
        """Import a folder of .m3u/.pls files, or a public Spotify playlist."""
        folder = str(args.get("folder") or args.get("path") or "").strip()
        spotify = str(args.get("spotify") or args.get("url") or "").strip()
        if not folder and not spotify:
            return Reply("Give me a folder of playlists, or a Spotify link "
                         "like `https://open.spotify.com/playlist/…`.",
                         error=True)
        apply = not self.dry_run
        try:
            from playlist_import import import_folder, import_spotify
            if folder:
                path = Path(folder).expanduser()
                if not path.is_dir():
                    return Reply(f"`{_escape(folder)}` is not a folder.",
                                 error=True)
                results = import_folder(
                    path, self.root,
                    recursive=bool(args.get("recursive", True)),
                    apply=apply, limit=int(args.get("limit") or 0))
            else:
                results = [import_spotify(spotify, self.root, apply=apply)]
        except SystemExit as exc:
            return Reply(str(exc), error=True)
        except (OSError, ValueError) as exc:
            return Reply(f"That import failed: {_escape(str(exc))}",
                         error=True)
        self.catalog.playlists(refresh=True)

        self.ctx.last_playlist = next(
            (item.name for item in results if item.resolved), None)
        lines = []
        written = 0
        total = found = 0
        for item in results:
            total += len(item.entries)
            found += len(item.resolved)
            head = (f"**{_escape(item.name)}** · {len(item.entries)} tracks, "
                    f"{len(item.resolved)} in the library")
            if item.target:
                written += 1
                head += f" → `{item.target}`"
            elif item.entries and not item.resolved:
                head += " → nothing matched"
            lines.append(head)
            if item.note:
                lines.append(f"*{_escape(item.note)}*")
            for entry in item.missing[:4]:
                label = entry.title or entry.raw
                who = f" — {entry.artist}" if entry.artist else ""
                lines.append(f"- missing: {_escape(label)}{_escape(who)}")
            if len(item.missing) > 4:
                lines.append(f"- … and {len(item.missing) - 4} more missing")
        if not results:
            return Reply("No `.m3u`, `.m3u8` or `.pls` file in that folder.",
                         error=True)
        body = "\n".join(lines)
        if self.dry_run:
            return Reply("Preview (dry run, nothing written):\n\n" + body,
                         tool="import-playlist")
        summary = (f"{written} list{'s' if written != 1 else ''} written to "
                   f"`Playlists/Custom/` · {found:,} of {total:,} entries "
                   "matched locally.")
        if total and found < total:
            summary += ("\n\nSay “search the import source for <artist> <title>” "
                        "to fetch a missing track; the import then adds it to "
                        "the list on the next run.")
        return Reply(summary + "\n\n" + body, tool="import-playlist")

    # -- skills: learning --------------------------------------------------

    def skill_learn(self, args: dict, request: str) -> Reply:
        kind = str(args.get("kind") or "show").casefold()
        value = str(args.get("value") or "").strip()
        if kind in {"show", "summary", "profile"}:
            return Reply("## What I learned\n\n" + self.profile.summary_markdown()
                         + "\n\nThe full picture — every weight, its bounded "
                         "influence, your notes and learned phrases — is in "
                         "**Settings → Profile**.", tool="profile")
        if kind == "note":
            if not value:
                return Reply("What should I remember?", error=True)
            self.profile.note(value)
            return Reply(f"Remembered: “{_escape(value)}”.", tool="profile")
        if kind == "mapping":
            moods = [mood for mood in (args.get("moods") or [])
                     if mood in {"Chill", "Happy", "Party", "Workout", "Dark",
                                 "Melancholy", "Epic", "Romantic", "Focus",
                                 "Retro"}]
            if not moods:
                return Reply("Which moods should that phrase select?",
                             error=True)
            self.profile.add_mapping(value, moods)
            return Reply(f"From now on “{_escape(value)}” means "
                         f"{', '.join(moods)}.", tool="profile")
        if kind == "forget":
            removed = self.profile.forget(value)
            return Reply("Forgot:\n" + "\n".join(f"- {_escape(item)}" for item in removed)
                         if removed else "Nothing matched.", tool="profile")

        track = self._resolve_seed(value) if value else None
        if track is None:
            track = self.ctx.current or (self.ctx.last_tracks[0]
                                         if self.ctx.last_tracks else None)
        if kind in {"like", "love"}:
            if track is None:
                return Reply("Tell me which track you like — play it first.",
                             error=True)
            changes = self.profile.like_track(track, 1.0)
            return Reply(f"Noted: you like **{_escape(track.label())}**."
                         + (f"\n\nI raised {', '.join(changes)}." if changes
                            else ""), tool="profile")
        if kind in {"dislike", "hate"}:
            if track is None:
                return Reply("Tell me which track you don't like — play it "
                             "first.", error=True)
            changes = self.profile.dislike_track(track, 1.0)
            return Reply(f"Noted: not **{_escape(track.label())}**. I'll "
                         "steer away from it."
                         + (f"\n\nLowered {', '.join(changes)}." if changes
                            else ""), tool="profile")
        return Reply(self.profile.summary_markdown(), tool="profile")

    # -- skills: psychoacoustic profile ------------------------------------

    def _playing_track(self) -> Track | None:
        """The track mpv really plays, else what the chat last selected."""
        try:
            current = self.player.current_file()
        except (OSError, ValueError):
            current = ""
        if current:
            try:
                rel = Path(current).resolve().relative_to(
                    self.root.resolve()).as_posix()
            except (OSError, ValueError):
                rel = library_rel(current)
            track = self.catalog.track(rel)
            if track is not None:
                return track
        return self.ctx.current or (self.ctx.last_tracks[0]
                                    if self.ctx.last_tracks else None)

    @staticmethod
    def _bar(value: float, width: int = 10) -> str:
        try:
            filled = int(round(max(0.0, min(1.0, float(value))) * width))
        except (TypeError, ValueError):
            filled = 0
        return "▰" * filled + "▱" * (width - filled)

    def _psycho_axes(self, rel: str, set_name: str | None = None
                     ) -> tuple[dict | None, str]:
        record = self.psycho.describe(rel, set_name)
        if record is None:
            return None, ("I have no profile for this track yet. Run "
                          "`scripts/psycho_features.py --apply` and then "
                          "`scripts/psycho_profile.py --apply`.")
        return record, ""

    def _psycho_markdown(self, track: Track, record: dict,
                         set_name: str | None = None) -> str:
        set_name = set_key(set_name)
        spec = next((item for item in SETS if item["key"] == set_name), SETS[0])
        label = f"{_escape(track.who)} — {_escape(track.name)}"
        family = record.get("family") or "unfiled"
        lines = [f"## Profile · {label}", f"`{track.rel}`", ""]
        lines.append(f"**Set** · {spec['label']} — {spec.get('note', '')}")
        lines.append(f"**Family** · {_escape(str(family))}"
                     + (" (psychology)" if set_name != "psychology" else ""))
        lines.append("")
        for axis in set_axes(set_name):
            value = record["axes"].get(axis["key"])
            if value is None:
                continue
            word = axis["high"] if value >= 0.5 else axis["low"]
            lines.append(f"- **{axis['label']}** {self._bar(value)} "
                         f"{value:.2f} · {word}")
        measured = record.get("measured") or {}
        for group in RAW_GROUPS:
            parts = []
            for item in RAW:
                if item["group"] != group:
                    continue
                value = measured.get(item["key"])
                if value is None:
                    continue
                number = f"{float(value):.{item['digits']}f}"
                parts.append(f"{item['label']} {number}"
                             + (f" {item['unit']}" if item["unit"] else ""))
            if parts:
                lines += ["", f"**{group}** · " + " · ".join(parts)]
        if spec.get("estimated"):
            lines += ["", "_This set is an estimated lens over band shares and "
                      "transients, not an instrument classifier._"]
        if record.get("estimated"):
            lines += ["", "_Some measurements are still estimated from the "
                      "mood cache; `scripts/psycho_features.py --apply` fills "
                      "them in._"]
        lines += ["", "_The **Profile** button shows the same numbers as a net "
                  "diagram you can bend and switch between sets._"]
        return "\n".join(lines)

    def skill_psycho(self, args: dict, request: str) -> Reply:
        action = str(args.get("action") or "show").casefold()
        if action in {"update", "measure", "refresh", "analyse", "analyze",
                      "recompute"}:
            return self._psycho_update(refresh=bool(args.get("refresh")))
        if action in {"status", "progress"}:
            return Reply(self._psycho_progress_markdown(), tool="psycho · status")
        if action in {"families", "family", "groups", "clusters"}:
            families = self.psycho.families()
            if not families:
                return Reply("No psychoacoustic families yet — run "
                             "`scripts/psycho_profile.py --apply`.", error=True)
            lines = [f"## Psychoacoustic families · {len(families)}"]
            for row in families:
                traits = ", ".join(row["traits"][:3])
                lines.append(f"- **{_escape(row['name'])}** · "
                             f"{row['size']:,} tracks · {traits}")
                if row.get("playlist"):
                    lines.append(f"  `{row['playlist']}`")
            lines += ["", "Each family is a playlist under `Playlists/Psycho/`."]
            return Reply("\n".join(lines), tool="psycho · families")
        if action in {"sets", "lenses", "dimensions"}:
            lines = ["## Profile sets", "",
                     "The net diagram can show the same measurements through "
                     "these lenses; 0.5 is average inside each one.", ""]
            for spec in SETS:
                span = ", ".join(axis["label"] for axis in spec["axes"])
                lines.append(f"- **{spec['label']}** — {spec.get('note', '')}  "
                             f"({span})")
            lines += ["", "Ask for “the timbre profile” or switch the set in "
                      "the Profile panel."]
            return Reply("\n".join(lines), tool="psycho · sets")

        set_name = set_key(args.get("set"))
        spec = next((item for item in SETS if item["key"] == set_name), SETS[0])
        keys = set_keys(set_name)

        track = self._resolve_seed(args.get("track")) if args.get("track") else None
        if track is None:
            track = self._playing_track()
        if track is None:
            return Reply("Play a track or name one — then I can show its "
                         "profile.", error=True)
        record, problem = self._psycho_axes(track.rel, set_name)
        if record is None:
            return Reply(problem, error=True)

        if action == "similar" or args.get("adjustments"):
            adjustments = {}
            for key, delta in (args.get("adjustments") or {}).items():
                key = str(key).casefold()
                if key not in keys:
                    continue
                try:
                    adjustments[key] = float(delta)
                except (TypeError, ValueError):
                    continue
            try:
                limit = int(args.get("count") or 12)
            except (TypeError, ValueError):
                limit = 12
            hits = self.psycho.similar(track.rel, adjustments=adjustments,
                                       limit=max(4, min(40, limit)),
                                       set_name=set_name)
            if not hits:
                return Reply("No other track has a profile yet.", error=True)
            moved = ", ".join(
                f"{key} {'+' if delta >= 0 else '−'}{abs(delta):.2f}"
                for key, delta in adjustments.items() if key in keys)
            heading = (f"## Fits “{_escape(track.who)} — {_escape(track.name)}”"
                       f" · {spec['label']}")
            if moved:
                heading += f" · {moved}"
            lines = [heading, ""]
            tracks = []
            for number, hit in enumerate(hits, 1):
                other = self.catalog.track(hit["rel"])
                if other is None:
                    continue
                tracks.append(other)
                changed = [key for key in keys
                           if key in (adjustments or {})]
                detail = " · ".join(
                    f"{key} {hit['axes'].get(key, 0):.2f}"
                    for key in (changed or keys[:3])
                    if key in hit["axes"])
                lines.append(f"{number}. **{_escape(other.who)} — "
                             f"{_escape(other.name)}**  ·  "
                             f"{_escape(str(hit.get('family') or ''))}  ·  "
                             f"distance {hit['distance']:.3f}")
                lines.append(f"   `{other.rel}`  ·  {detail}")
            lines += ["", f"Add one with “add 1 to <playlist>”, or play it."]
            return Reply("\n".join(lines), tracks=tracks,
                         tool="psycho · similar")

        return Reply(self._psycho_markdown(track, record, set_name),
                     tool="psycho · show")

    def _psycho_update(self, refresh: bool = False) -> Reply:
        """Measure the library from inside the app (one job at a time)."""
        if self.dry_run:
            state = psycho_job.counts(self.root)
            return Reply(
                "Dry run: would measure the library and rebuild the profile "
                f"axes for {state['total']:,} tracks.",
                tool="psycho · update")
        if not psycho_job.available(self.root):
            return Reply("This library has no `scripts/psycho_features.py`, so "
                         "there is nothing to run.", error=True)
        try:
            job = psycho_job.start(self.root, refresh=refresh)
        except (OSError, RuntimeError) as exc:
            return Reply(f"The measurement could not start: {exc}",
                         error=True)
        running = psycho_job.status(self.root, str(job.get("id") or ""))
        total = running.get("total") or 0
        queued = (f"{total:,} tracks to measure" if total
                  else "whatever is missing or out of date")
        return Reply(
            "## Measuring the library\n\n"
            f"Started in the background — {queued}, then the profile axes and "
            "families are rebuilt.  The window shows the progress in Settings "
            "→ Sound profiles; ask “how is the measurement going” for the log "
            "tail.",
            tool="psycho · update")

    def _psycho_progress_markdown(self) -> str:
        status = psycho_job.status(self.root)
        state = str(status.get("state") or "idle")
        if state == "idle":
            return ("No measurement has run in this install yet.  Ask me to "
                    "“measure the library” or use Settings → Sound profiles.")
        counts = status.get("counts") or {}
        lines = [f"## Sound profile · {state}"]
        if status.get("running"):
            lines.append(f"{status.get('step') or 'working'} · "
                         f"{status.get('percent', 0):.0f} % "
                         f"({status.get('done', 0):,} of "
                         f"{status.get('total', 0):,} · "
                         f"{status.get('elapsed', 0):.0f} s)")
        else:
            took = status.get("elapsed") or 0
            lines.append(f"finished in {took / 60:.1f} min"
                         if state == "done" else
                         f"stopped after {took / 60:.1f} min")
        if counts:
            lines.append(f"{counts.get('measured', 0):,} measured · "
                         f"{counts.get('profiles', 0):,} profiles · "
                         f"{counts.get('families', 0)} families")
        tail = status.get("lines") or []
        if tail:
            lines += ["", "```", *tail[-6:], "```"]
        return "\n".join(lines)

    # -- skills: the library index -----------------------------------------
    #
    # Indexing is the library's own pipeline (scan -> reports -> playlists),
    # run as a detached job like the sound-profile measurement.  It only
    # reads audio and tags; it writes the inventory, the reports and the
    # generated playlists.

    def skill_index(self, args: dict, request: str) -> Reply:
        action = str(args.get("action") or "status").casefold()
        if action in {"pause", "pausieren", "anhalten", "hold"}:
            return self._index_control("pause")
        if action in {"resume", "continue", "fortsetzen", "weiter",
                      "unpause"}:
            return self._index_control("resume")
        if action in {"cancel", "abort", "abbrechen", "beenden", "stop",
                      "halt", "quit"}:
            return self._index_control("cancel")
        if action in {"index", "rebuild", "reindex", "scan", "update",
                      "refresh", "rescan"}:
            return self._index_update()
        return Reply(self._index_progress_markdown(),
                     tool="library-overview · index")

    def _index_control(self, action: str) -> Reply:
        """Pause, resume or cancel the running index job from the chat."""
        verb = {"pause": "paused", "resume": "resumed",
                "cancel": "cancelled"}[action]
        if self.dry_run:
            return Reply(f"Dry run: the index is not {verb}.",
                         tool="library-overview · index")
        if not index_job.available(self.root):
            return Reply("This library has no `scripts/phase0_inventory.py`, "
                         "so there is no index job to control.", error=True)
        handler = {"pause": index_job.pause, "resume": index_job.resume,
                   "cancel": index_job.cancel}[action]
        try:
            status = handler(self.root)
        except OSError as exc:
            return Reply(f"The index could not be {verb}: {exc}", error=True)
        if status.get("error"):
            return Reply(str(status["error"]), error=True)
        if status.get("note"):
            return Reply(str(status["note"]),
                         tool="library-overview · index")
        return Reply(self._index_progress_markdown(),
                     tool="library-overview · index")

    def _index_update(self) -> Reply:
        if self.dry_run:
            state = index_job.counts(self.root)
            return Reply(
                "Dry run: would re-index the library "
                f"({state['tracks']:,} tracks in the inventory) and rebuild "
                "the playlists.",
                tool="library-overview · index")
        if not index_job.available(self.root):
            return Reply("This library has no `scripts/phase0_inventory.py`, "
                         "so there is nothing to run.", error=True)
        try:
            job = index_job.start(self.root)
        except (OSError, RuntimeError) as exc:
            return Reply(f"The index could not start: {exc}", error=True)
        running = index_job.status(self.root, str(job.get("id") or ""))
        if running.get("total"):
            queued = f"{running['total']:,} new files to read"
        else:
            queued = "whatever is new or changed"
        return Reply(
            "## Indexing the library\n\n"
            f"Started in the background — {queued}, then the reports and the "
            "playlists are rebuilt.  The window shows the progress in "
            "Settings → Library index; ask “how is the indexing going” for "
            "the log tail.",
            tool="library-overview · index")

    def _index_progress_markdown(self) -> str:
        status = index_job.status(self.root)
        state = str(status.get("state") or "idle")
        counts = status.get("counts") or index_job.counts(self.root)
        if state == "idle":
            return ("The library has not been indexed in this install yet.  "
                    "Ask me to “index the library” or use Settings → "
                    "Library index.\n\n"
                    f"{counts.get('tracks', 0):,} tracks · "
                    f"{counts.get('playlists', 0)} playlists")
        if state in {"done", "cancelled"}:
            self.catalog.reload()
        lines = [f"## Library index · {state}"]
        if status.get("active"):
            where = ("paused at" if state == "paused" else "")
            step = str(status.get("step") or "working")
            head = f"{where} {step}" if where else step
            lines.append(f"{head} · {status.get('percent', 0):.0f} %"
                         + (f" ({status.get('done', 0):,} of "
                            f"{status.get('total', 0):,} files)"
                            if status.get("phase") == "scan"
                            and status.get("total") else ""))
        else:
            took = status.get("elapsed") or 0
            lines.append(f"finished in {took:.0f} s" if state == "done"
                         else f"stopped after {took:.0f} s")
        if state == "paused":
            lines += ["", "The scan is held where it was and keeps its place.  "
                      "Say “resume the indexing” to carry on, or “cancel the "
                      "indexing” to drop it."]
        elif state == "cancelling":
            lines += ["", "Stopping the job; the scan ends at the next file "
                      "and the inventory keeps what it already wrote."]
        elif state == "cancelled":
            lines += ["", "The index was cancelled; the inventory keeps every "
                      "track it had written.  Ask me to “index the library” "
                      "to start again."]
        lines.append(f"{counts.get('tracks', 0):,} tracks · "
                     f"{counts.get('playlists', 0)} playlists")
        if counts.get("indexed_at"):
            lines.append(f"inventory written {counts['indexed_at']}")
        tail = status.get("lines") or []
        if tail:
            lines += ["", "```", *tail[-6:], "```"]
        return "\n".join(lines)

    # -- skills: the app's own library data --------------------------------
    #
    # The sound profile and the lyrics are hours of measuring and
    # transcription, so they are backed up once a day as plain text in
    # LibraryData/, and an import matches every record to a file by a hash of
    # its *audio* rather than its name.  Nothing here touches a music file.

    def skill_library_data(self, args: dict, request: str) -> Reply:
        action = str(args.get("action") or "status").casefold()
        if action in {"pause", "pausieren", "anhalten", "hold"}:
            return self._library_control("pause")
        if action in {"resume", "continue", "fortsetzen", "weiter",
                      "unpause"}:
            return self._library_control("resume")
        if action in {"import", "restore", "einlesen", "wiederherstellen",
                      "zurueckspielen", "zurückspielen"}:
            return self._library_import(args)
        if action in {"preview", "check", "pruefen", "prüfen", "test"}:
            return self._library_preview(args)
        if action in {"backup", "export", "save", "write", "sichere",
                      "exportiere", "start"}:
            return self._library_backup()
        return Reply(self._library_progress_markdown(),
                     tool="library-data · status")

    def _library_control(self, action: str) -> Reply:
        verb = {"pause": "paused", "resume": "resumed"}[action]
        if self.dry_run:
            return Reply(f"Dry run: the backup is not {verb}.",
                         tool="library-data")
        if not library_job.available(self.root):
            return Reply("This library has no `scripts/library_data.py`, so "
                         "there is no backup job to control.", error=True)
        handler = {"pause": library_job.pause,
                   "resume": library_job.resume}[action]
        try:
            status = handler(self.root)
        except OSError as exc:
            return Reply(f"The backup could not be {verb}: {exc}",
                         error=True)
        if status.get("error"):
            return Reply(str(status["error"]), error=True)
        if status.get("note"):
            return Reply(str(status["note"]), tool="library-data")
        return Reply(self._library_progress_markdown(), tool="library-data")

    def _library_backup(self) -> Reply:
        if self.dry_run:
            counts = library_job.counts(self.root)
            return Reply(
                "Dry run: would fingerprint what is new, write "
                f"`{Path(counts.get('export_dir') or 'LibraryData')}/"
                f"library-data-{date.today().isoformat()}.jsonl` and prune the "
                f"exports older than {self._backup_keep()} days.",
                tool="library-data · backup")
        if not library_job.available(self.root):
            return Reply("This library has no `scripts/library_data.py`, so "
                         "its data cannot be backed up here.", error=True)
        try:
            job = library_job.start(self.root, keep=self._backup_keep())
        except (OSError, RuntimeError) as exc:
            return Reply(f"The backup could not start: {exc}", error=True)
        status = library_job.status(self.root, str(job.get("id") or ""))
        if status.get("active"):
            return Reply(
                "## Backing up the library data\n\n"
                "Started in the background: the audio of every new or changed "
                "file is fingerprinted, one plain-text export is written and "
                "the oldest exports are pruned. The window shows the progress "
                "in Settings → Library data and in the footer.",
                tool="library-data · backup")
        return Reply(self._library_progress_markdown(),
                     tool="library-data · backup")

    def _library_import(self, args: dict) -> Reply:
        file = str(args.get("file") or "").strip() or self._library_file
        if not file:
            return Reply(
                "Name the export file to import, for example "
                "`LibraryData/library-data-2026-11-22.jsonl`. Every record is "
                "then matched to its file by a fingerprint of the audio, not "
                "by the stored path.", tool="library-data · import")
        self._library_file = file
        self.ctx.last_library_file = file
        if self.dry_run:
            return Reply(f"Dry run: would import `{file}` without writing.",
                         tool="library-data · import")
        try:
            job = library_job.start_import(
                self.root, file, overwrite=bool(args.get("overwrite")))
        except (OSError, RuntimeError, ValueError) as exc:
            return Reply(str(exc), error=True)
        status = library_job.status(self.root, str(job.get("id") or ""))
        if status.get("active"):
            return Reply(
                "## Importing the library data\n\n"
                f"Reading `{file}` in the background and matching every "
                "record by its audio fingerprint. The profiles and lyrics "
                "that exist already stay unless you ask to overwrite them. "
                "The databases are copied aside first, and the journal names "
                "the file and its MD5.",
                tool="library-data · import")
        return Reply(self._library_progress_markdown(),
                     tool="library-data · import")

    def _library_preview(self, args: dict) -> Reply:
        file = str(args.get("file") or "").strip() or self._library_file
        if not file:
            return Reply(
                "Name the export file to check, for example "
                "`LibraryData/library-data-2026-11-22.jsonl`.",
                tool="library-data · preview")
        self._library_file = file
        self.ctx.last_library_file = file
        answer = library_job.preview(self.root, file)
        if answer.get("error"):
            return Reply(str(answer["error"]), error=True)
        counts = answer.get("counts") or {}
        return Reply(
            f"## `{Path(file).name}`\n\n"
            f"{answer.get('matched', 0):,} of "
            f"{answer.get('records', 0):,} records match this library · "
            f"{answer.get('by_path', 0):,} only by path · "
            f"{answer.get('unmatched', 0):,} unmatched\n\n"
            f"{counts.get('tracks', 0):,} measured tracks · "
            f"{counts.get('profiles', 0):,} profiles · "
            f"{counts.get('sets', 0):,} profile sets · "
            f"{counts.get('lyrics', 0):,} lyric songs · "
            f"{counts.get('lines', 0):,} lines · "
            f"{counts.get('families', 0)} sound families\n\n"
            "Nothing was written. Say “import it” to fill the databases.",
            tool="library-data · preview")

    def _backup_keep(self) -> int:
        try:
            return max(1, int(self.profile.setting("library_backup_keep", 7)))
        except (TypeError, ValueError):
            return library_job.DEFAULT_KEEP

    def _library_progress_markdown(self) -> str:
        if not library_job.available(self.root):
            return ("This library has no `scripts/library_data.py`, so the "
                    "app cannot back its data up here.")
        status = library_job.status(self.root)
        counts = status.get("counts") or library_job.counts(self.root)
        state = str(status.get("state") or "idle")
        latest = counts.get("latest") or {}
        prints = counts.get("fingerprints") or {}
        lines = ["## Library data" if state == "idle"
                 else f"## Library data · {state}"]
        if status.get("active"):
            where = "paused at" if state == "paused" else ""
            step = str(status.get("step") or "working")
            head = f"{where} {step}" if where else step
            if status.get("total"):
                lines.append(f"{head} · {status.get('percent', 0):.0f} % "
                             f"({status.get('done', 0):,} of "
                             f"{status.get('total', 0):,} files)")
            else:
                lines.append(head)
        elif state == "idle":
            # no job record: the exports themselves say how current it is
            lines.append("today's export is still missing"
                         if counts.get("due") else
                         "today's export is written")
        else:
            took = status.get("elapsed") or 0
            lines.append(f"finished in {took:.0f} s" if state == "done"
                         else f"stopped after {took:.0f} s")
        if latest.get("name"):
            lines.append(f"latest export · `{latest['name']}` "
                         f"({(latest.get('bytes') or 0) / (1024 * 1024):.1f}"
                         " MB)")
        else:
            lines.append("no export yet")
        lines.append(f"{counts.get('exports', 0)} exports kept · "
                     f"{counts.get('psycho', 0):,} measured tracks · "
                     f"{counts.get('lyrics', 0):,} lyric songs · "
                     f"{prints.get('known', 0):,} fingerprinted")
        if state == "idle":
            lines += ["", "Ask me to “back up the library data” or press "
                      "*Back up now* in Settings → Library data; the app also "
                      "runs it by itself once a day."]
        elif state == "paused":
            lines += ["", "The backup is held where it was; say “resume the "
                      "backup” to carry on."]
        elif state == "failed":
            lines += ["", "It stopped; the last log lines are below."]
        tail = status.get("lines") or []
        if state in {"failed", "running"} and tail:
            lines += ["", "```", *tail[-6:], "```"]
        return "\n".join(lines)

    # -- skills: lyrics ----------------------------------------------------
    #
    # The transcription is local (faster-whisper) and runs as a detached job;
    # this reads the resulting database.  A lyric search can be narrowed by a
    # sound property ("something with a boat and fast"): the words pick the
    # songs, the measured axes order them.

    def skill_lyrics(self, args: dict, request: str) -> Reply:
        action = str(args.get("action") or "").casefold()
        text = str(args.get("text") or "").strip()
        if not action:
            action = "search" if text else "show"
        if action in {"status", "progress", "count"}:
            return Reply(self._lyrics_status_markdown(), tool="lyrics · status")
        if action in {"transcribe", "update", "refresh", "measure", "write"}:
            return self._lyrics_transcribe(args)
        if action in {"setup", "install", "prepare"}:
            return self._lyrics_setup(args)
        if action in {"export", "lrc", "save-lrc", "save_lrc", "sidecar"}:
            return self._lyrics_export(args)
        if action in {"similar", "like", "alike", "related", "verwandt"}:
            return self._lyrics_similar(args)
        if action in {"offset", "shift", "align", "timing", "nudge"}:
            return self._lyrics_offset(args)
        if action in {"now", "current", "line", "playing"}:
            return self._lyrics_now(args, ahead=False)
        if action in {"next", "following", "ahead"}:
            return self._lyrics_now(args, ahead=True)
        if action in {"search", "find", "mentions", "about", "which"} or text:
            return self._lyrics_search(args, text)
        return self._lyrics_show(args)

    def _lyrics_show(self, args: dict) -> Reply:
        track = self._resolve_seed(args.get("track")) if args.get("track") else None
        if track is None:
            track = self._playing_track()
        if track is None:
            return Reply("Play a song, or name one — then I can show its "
                         "lyrics.", error=True)
        row = self.lyrics.track(track.rel)
        path = self.root / track.rel
        if row is None:
            return Reply(
                f"**{_escape(track.who)} — {_escape(track.name)}** has no "
                "lyrics yet.\n\n"
                + self._lyrics_backend_hint()
                + "\n\nAsk me to “transcribe this song” and I will do it in "
                  "the background.", tool="lyrics · show")
        lines = self.lyrics.lines(track.rel)
        head = (f"## {_escape(row['title'] or track.name)} · "
                f"{_escape(row['artist'] or track.who)}")
        meta = [part for part in (
            row.get("language") or "", row.get("model") or "",
            f"{len(lines)} lines") if part]
        out = [head, "_" + " · ".join(meta) + "_", ""]
        for line in lines[:400]:
            out.append(f"`{lyric_clock(line['start'])}`  {_escape(line['text'])}")
        if len(lines) > 400:
            out.append(f"_… {len(lines) - 400} more lines_")
        if path.is_file():
            stat = path.stat()
            if not self.lyrics.is_current(track.rel, stat.st_size, stat.st_mtime):
                out += ["", "_The file changed since it was transcribed — ask "
                            "me to transcribe it again for the current audio._"]
        out += ["", "_The lyrics panel in the window follows the playback "
                    "position, line by line._"]
        return Reply("\n".join(out), tracks=[track], tool="lyrics · show")

    def _lyrics_now(self, args: dict, ahead: bool = False) -> Reply:
        """The line at the playhead, with its neighbours.

        Answers "which line is playing", "where are we in the lyrics" and
        "what is the next line".  The position comes from the player, so the
        answer follows the song exactly like the panel does; nothing is
        written and no lyric is invented when the track has none.
        """
        track = self._resolve_seed(args.get("track")) if args.get("track") else None
        playing = self._playing_track()
        if track is None:
            track = playing
        if track is None:
            return Reply("Play a song, or name one — then I can show its "
                         "lyrics.", error=True)
        row = self.lyrics.track(track.rel)
        lines = self.lyrics.lines(track.rel) if row is not None else []
        if not lines:
            return self._lyrics_show({"track": track.rel})

        position = 0.0
        status = None
        if playing is not None and playing.rel == track.rel:
            status = self.player.status()
        follows = bool(status) and not status.get("idle")
        if follows:
            try:
                position = float(status.get("time") or 0)
            except (TypeError, ValueError):
                position = 0.0
        index = 0
        for number, line in enumerate(lines):
            try:
                start = float(line.get("start") or 0)
            except (TypeError, ValueError):
                start = 0.0
            if start <= position + 0.35:
                index = number
            else:
                break
        if ahead:
            index = min(len(lines) - 1, index + 1)
        first = max(0, index - 2)
        last = min(len(lines), index + 3)

        title = (row.get("title") if row else "") or track.name
        head = (f"## {_escape(title)} · {_escape(track.who)} · "
                f"line {index + 1} of {len(lines)}")
        out = [head]
        if not follows:
            out += ["_Nothing is playing, so this is the beginning of the "
                    "song._"]
        out.append("")
        for number in range(first, last):
            line = lines[number]
            mark = ""
            if number == index:
                mark = "  ← **the line that is playing**" if follows \
                    else "  ← **start here**"
            out.append(f"`{lyric_clock(line['start'])}`  "
                       f"{_escape(line['text'])}{mark}")
        if last < len(lines):
            out.append(f"_… {len(lines) - last} more lines_")
        rest = "the whole lyrics" if ahead else "the next line"
        out += ["", f"_Ask for “{rest}”, or open the lyrics panel in the "
                    "window to follow the song._"]
        return Reply("\n".join(out), tracks=[track], tool="lyrics · line")

    def _lyrics_search(self, args: dict, text: str) -> Reply:
        words = str(text or "").strip()
        if not words:
            return Reply("Search the lyrics for what? Try “songs about a "
                         "boat”.", error=True)
        try:
            limit = int(args.get("limit") or 12)
        except (TypeError, ValueError):
            limit = 12
        limit = max(1, min(40, limit))
        hits = self.lyrics.find(words, limit=max(limit * 3, 24))
        if not hits:
            summary = self.lyrics.summary()
            if not summary["tracks"]:
                return Reply(
                    "No song has lyrics yet — nothing to search.\n\n"
                    + self._lyrics_backend_hint(), tool="lyrics · search")
            return Reply(
                f"No lyric line sings “{_escape(words)}”.  Words are matched "
                "whole, so “sea” does not match “seaside”.  "
                f"{summary['tracks']:,} of the library's songs are transcribed "
                "so far, so the line may simply not be covered yet.", tool="lyrics · search")

        # a sound property in the same sentence narrows the lyric hits
        terms = self._rank_terms(args) or rank_terms(words)
        ranking: list[dict] = []
        if terms and self.psycho.available() and self.psycho.count():
            pool = {hit["rel"] for hit in hits}
            ranking = self.psycho.rank(terms, limit=min(500, len(pool) or 1),
                                       rels=pool)
        ordered = lyrics_combine(hits, ranking, limit=limit)

        tracks: list[Track] = []
        for hit in ordered:
            track = self.catalog.track(hit["rel"])
            if track is not None:
                tracks.append(track)
        header = f"**Lyrics with “{_escape(words)}”**"
        if terms and ranking:
            header += " · " + " + ".join(concept_labels(terms))
        header += f" · {len(tracks)} song{'s' if len(tracks) != 1 else ''}"
        lines = [header, ""]
        if any(hit.get("fuzzy") for hit in ordered):
            lines += ["_No exact word matched; the closest stored words are "
                      "shown._", ""]
        for number, hit in enumerate(ordered, 1):
            title = hit.get("title") or Path(hit["rel"]).stem
            artist = hit.get("artist") or ""
            line_bits = [lyric_clock(hit.get("start") or 0)]
            if hit.get("sound") is not None:
                line_bits.append(f"sound {hit['sound']:.2f}")
            mentions = int(hit.get("mentions") or 1)
            if mentions > 1:
                line_bits.append(f"×{mentions}")
            lines.append(f"{number}. **{_escape(artist + ' — ' if artist else '')}"
                         f"{_escape(title)}**  ·  " + "  ·  ".join(line_bits))
            lines.append(f"   _“{_escape(hit.get('line') or '')}”_")
            lines.append(f"   `{hit['rel']}`")
        if tracks:
            lines += ["", "Say “play number 1” to hear one."]
        self.ctx.last_tracks = tracks
        self.ctx.last_query = words
        return Reply("\n".join(lines), tracks=tracks, tool="lyrics · search")

    def _lyrics_similar(self, args: dict) -> Reply:
        """Songs whose sung words resemble the seed's, without a model.

        The seed is the named track, else the one that is playing: "find
        songs with similar lyrics to X" and "more songs like this
        lyrically" both land here.  A song needs lyrics to be the seed and a
        second transcribed song to be a neighbour, so the answer says
        exactly what was compared instead of guessing.
        """
        track = self._lyric_track(args)
        if track is None:
            return Reply("Play a song, or name one — then I can look for songs "
                         "whose lyrics resemble it.", error=True)
        record = self.lyrics.track(track.rel)
        if record is None:
            return Reply(
                f"**{_escape(track.who)} — {_escape(track.name)}** has no "
                "lyrics yet, so there is nothing to compare.\n\n"
                + self._lyrics_backend_hint(), tool="lyrics · similar")
        try:
            limit = int(args.get("limit") or 12)
        except (TypeError, ValueError):
            limit = 12
        limit = max(1, min(40, limit))
        hits = self.lyrics.similar(track.rel, limit=limit)
        summary = self.lyrics.summary()
        head = (f"**Lyrics like {_escape(record['artist'] or track.who)} — "
                f"{_escape(record['title'] or track.name)}**")
        if not hits:
            return Reply(
                head + "\n\nNo other transcribed song shares enough words with "
                f"it.  {summary['tracks']:,} songs have lyrics so far; a "
                "closer match can appear once more of the library is "
                "transcribed.\n\n"
                "Ask me to “transcribe the missing lyrics” and the background "
                "job fills the rest in.", tool="lyrics · similar")

        tracks: list[Track] = []
        lines = [head + f" · {len(hits)} song{'s' if len(hits) != 1 else ''}",
                 "", "_Word-by-word comparison of the stored lyrics; a word "
                 "that appears everywhere counts little, a rare one a lot.  "
                 f"{summary['tracks']:,} songs were compared._", ""]
        for number, hit in enumerate(hits, 1):
            title = hit.get("title") or Path(hit["rel"]).stem
            artist = hit.get("artist") or ""
            percent = round(float(hit.get("score") or 0) * 100)
            lines.append(
                f"{number}. **{_escape(artist + ' — ' if artist else '')}"
                f"{_escape(title)}**  ·  {percent} % word overlap")
            if hit.get("shared"):
                lines.append("   shared: " + _escape(", ".join(hit["shared"])))
            if hit.get("line"):
                lines.append(f"   `{lyric_clock(hit.get('start') or 0)}`  "
                             f"_{_escape(hit['line'])}_")
            lines.append(f"   `{hit['rel']}`")
            found = self.catalog.track(hit["rel"])
            if found is not None:
                tracks.append(found)
        if tracks:
            lines += ["", "Say “play number 1” to hear one, or “save that as "
                          "<name>” to keep the list."]
        self.ctx.last_tracks = tracks
        return Reply("\n".join(lines), tracks=tracks, tool="lyrics · similar")

    # -- the .lrc sidecar and the timing shift ----------------------------

    def _lyric_track(self, args: dict) -> Track | None:
        """The song a lyrics sub-command is about: named, else playing."""
        if args.get("track"):
            track = self._resolve_seed(args.get("track"))
            if track is not None:
                return track
        return self._playing_track()

    def _lyrics_export(self, args: dict) -> Reply:
        """Write the timed lyrics next to the audio as a `.lrc` file."""
        track = self._lyric_track(args)
        if track is None:
            return Reply("Play a song, or name one, then I can export its "
                         "lyrics.", error=True)
        row = self.lyrics.track(track.rel)
        if row is None or not self.lyrics.lines(track.rel):
            return Reply(
                f"**{_escape(track.who)} — {_escape(track.name)}** has no timed "
                "lyrics yet, so there is nothing to export.\n\n"
                + self._lyrics_backend_hint(), error=True,
                tool="lyrics · export")
        out = str(args.get("path") or args.get("out") or "").strip()
        target = lrc.target_for(self.root, track.rel, out)
        if self.dry_run:
            return Reply(
                f"**Would write** `{_escape(str(target))}` "
                f"· {len(self.lyrics.lines(track.rel)):,} lines (dry run — "
                "nothing was written).", tracks=[track],
                tool="lyrics · export")
        result = lrc.export(self.root, self.lyrics, track.rel, out=out,
                            overwrite=bool(args.get("overwrite")))
        if result.get("error"):
            return Reply(f"**Not exported** · {_escape(result['error'])}",
                         error=True, tool="lyrics · export")
        note = ("replaced the old file" if result.get("replaced")
                else "written next to the song" if result.get("inside")
                else "written")
        return Reply(
            f"**Lyrics exported** · {_escape(result['name'])}\n\n"
            f"`{_escape(result['path'])}`\n\n"
            f"{result['lines']:,} lines · {note} · the previous state is in "
            "the journal, so a rollback can undo it.", tracks=[track],
            tool="lyrics · export")

    def _lyrics_offset(self, args: dict) -> Reply:
        """Nudge (or reset) the lyric timings of one song."""
        track = self._lyric_track(args)
        if track is None:
            return Reply("Play a song, or name one, then I can shift its "
                         "lyrics.", error=True)
        raw = args.get("value", args.get("seconds"))
        if raw is None or str(raw).strip() == "":
            current = self.lyrics.offset(track.rel)
            if not current:
                return Reply(
                    f"The lyrics of **{_escape(track.who)} — "
                    f"{_escape(track.name)}** are not shifted.  Say “shift the "
                    "lyrics half a second later” to move them.",
                    tracks=[track], tool="lyrics · offset")
            direction = "later" if current > 0 else "earlier"
            return Reply(
                f"The lyrics of **{_escape(track.who)} — {_escape(track.name)}** "
                f"are shifted {abs(current):.1f} s {direction}.",
                tracks=[track], tool="lyrics · offset")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return Reply("The shift has to be a number of seconds, like 0.5 "
                         "or -1.", error=True)
        mode = str(args.get("mode") or "").casefold()
        if mode in {"add", "by", "delta"} or args.get("relative"):
            value += self.lyrics.offset(track.rel)
        if self.dry_run:
            return Reply(
                f"**Would shift** the lyrics of {_escape(track.name)} by "
                f"{value:+.1f} s (dry run — nothing was stored).",
                tracks=[track], tool="lyrics · offset")
        try:
            stored = self.lyrics.set_offset(track.rel, value)
        except ValueError as exc:
            return Reply(str(exc), error=True)
        if not stored:
            return Reply(
                f"**Timings reset** · the lyrics of {_escape(track.name)} play "
                "exactly as transcribed.", tracks=[track],
                tool="lyrics · offset")
        direction = "later" if stored > 0 else "earlier"
        return Reply(
            f"**Lyrics shifted** {abs(stored):.1f} s {direction} · "
            f"{_escape(track.name)}.  The panel and an exported `.lrc` use the "
            "same timing.", tracks=[track], tool="lyrics · offset")

    def _lyrics_transcribe(self, args: dict) -> Reply:
        track = self._resolve_seed(args.get("track")) if args.get("track") else None
        rel = track.rel if track is not None else ""
        # "transcribe all songs without lyrics" means the whole library
        # (`--missing` without `--limit`), so a boolean `all` is not a count.
        raw = args.get("missing")
        if args.get("all") and not isinstance(raw, (int, float)):
            missing = 0
        else:
            try:
                missing = int(raw or 0)
            except (TypeError, ValueError):
                missing = 0
        refresh = bool(args.get("refresh"))
        if not rel and not (missing or args.get("all")):
            playing = self._playing_track()
            rel = playing.rel if playing is not None else ""
        if self.dry_run:
            what = rel or ("the whole library" if not missing
                           else "the next songs without lyrics")
            return Reply(f"Dry run: would transcribe {what} locally.",
                         tool="lyrics · transcribe")
        if not lyrics_job.available(self.root):
            return Reply("This library has no `scripts/transcribe_lyrics.py`, "
                         "so there is nothing to run.", error=True)
        try:
            job = lyrics_job.start(self.root, rel=rel, refresh=refresh,
                                   missing=missing)
        except (OSError, RuntimeError) as exc:
            return Reply(f"The transcription could not start: {exc}\n\n"
                         + self._lyrics_backend_hint(), error=True)
        status = lyrics_job.status(self.root, str(job.get("id") or ""))
        full = bool(not rel and not missing)
        target = (f"**{_escape(Path(rel).stem)}**" if rel
                  else ("the whole library" if full
                        else "the next songs without lyrics"))
        running = ("It is already running" if status.get("running")
                   else "Started in the background")
        hint = (" Every song without lyrics, one after the other — pause it "
                "with the *lyrics* chip in the footer." if full else "")
        return Reply(
            f"## Transcribing {target}\n\n{running} — everything stays on this "
            "machine (local Whisper)." + hint + "  The window shows it under "
            "*Lyrics*; ask “how is the transcription going” for the log tail.",
            tool="lyrics · transcribe")

    def _lyrics_setup(self, args: dict) -> Reply:
        if self.dry_run:
            return Reply("Dry run: would install the local transcriber with "
                         "the shipped `setup_asr.sh`.", tool="lyrics · setup")
        try:
            lyrics_job.start_setup(self.root, cpu=bool(args.get("cpu")))
        except (OSError, RuntimeError) as exc:
            return Reply(f"The setup could not start: {exc}", error=True)
        return Reply(
            "## Installing the local transcriber\n\n"
            "Running the shipped `setup_asr.sh` in the background — it creates "
            "a venv with faster-whisper (about 450 MB, one time) and uses the "
            "GPU when the driver offers one.  Nothing leaves this machine.  "
            "Ask “how is the transcription going” for the progress.",
            tool="lyrics · setup")

    def _lyrics_status_markdown(self) -> str:
        status = lyrics_job.status(self.root)
        counts = lyrics_job.counts(self.root)
        info = lyrics_job.backend_info(self.root)
        state = str(status.get("state") or "idle")
        songs, lines_n = int(counts["tracks"]), int(counts["lines"])
        lines = ["## Lyrics"]
        lines.append(
            f"**{songs:,} {'song' if songs == 1 else 'songs'}** transcribed · "
            f"{lines_n:,} {'line' if lines_n == 1 else 'lines'}"
            + (f" · {counts['last'][:10]}" if counts["last"] else ""))
        if counts.get("audio"):
            share = 100.0 * songs / max(1, counts["audio"])
            shown = f"{share:.2f}" if 0 < share < 1 else f"{share:.1f}"
            lines.append(f"that is {shown} % of the "
                         f"{counts['audio']:,} songs the transcriber can read")
        lines.append("")
        if info.get("ready"):
            where = "GPU" if info.get("gpu") == "yes" else "CPU"
            lines.append(f"backend · local faster-whisper on the {where} "
                         f"`{info.get('interpreter')}`")
        else:
            lines.append("backend · **not installed** — "
                         + self._lyrics_backend_hint())
        if state != "idle":
            if status.get("running"):
                lines.append(f"job · {status.get('step') or 'working'} · "
                             f"{status.get('percent', 0):.0f} %")
            else:
                lines.append(f"job · {state}")
            tail = status.get("tail") or []
            if tail:
                lines += ["", "```", *tail[-5:], "```"]
        return "\n".join(lines)

    def _lyrics_backend_hint(self) -> str:
        info = lyrics_job.backend_info(self.root)
        if info.get("ready"):
            return (f"A local transcriber is ready "
                    f"({info.get('gpu') == 'yes' and 'GPU' or 'CPU'}).")
        if info.get("setup_available"):
            return ("The local transcriber is not installed yet — ask me to "
                    "“set up the transcriber”, or run the shipped "
                    "`setup_asr.sh`.")
        return ("This library has no transcription tool "
                "(`scripts/transcribe_lyrics.py`).")

    # -- skills: overview --------------------------------------------------

    # -- settings ----------------------------------------------------------
    #
    # One table (`chat/settings.py`) is the single source of truth: the chat,
    # the settings window and the language model's tools all end up here, so a
    # key can never be validated in one place and written in another.  No
    # setting touches a music file; the folder switch is deferred to the web
    # service, which owns the container, the player and the catalogue.

    def _setting_shown(self, item: dict) -> str:
        value = setting_values(self.profile, self.root).get(item["key"],
                                                            item["default"])
        kind = item["kind"]
        if kind == "bool":
            return "on" if value else "off"
        if kind == "choice":
            return item["choices"].get(str(value), str(value))
        if kind == "secret":
            return "set (hidden)" if value else "not set"
        return str(value) or "—"

    def _settings_applied(self, item: dict) -> None:
        """Make the running engine match a setting that was just written."""
        key = item["key"]
        if key == "use_llm":
            self.llm.enabled = bool(self.profile.setting("use_llm", True))
        elif key.startswith("llm_"):
            self.llm.reconfigure()

    def _settings_library(self, value: str) -> Reply:
        target = Path(value).expanduser()
        if self.dry_run:
            return Reply(f"Dry run: would switch the music folder to "
                         f"`{_escape(str(target))}`.",
                         tool="settings · music folder")
        self.deferred["library"] = str(target)
        return Reply(f"Switching the music folder to "
                     f"`{_escape(str(target))}` …",
                     tool="settings · music folder")

    def skill_settings(self, args: dict, request: str) -> Reply:
        action = str(args.get("action") or "").strip().lower()
        key = str(args.get("key") or "").strip()
        if not key:
            key = setting_resolve(str(args.get("query") or request))
        group = str(args.get("group") or "").strip()
        if action not in {"list", "show", "set", "toggle", "reset"}:
            action = ""
        if not key:
            action = "list"
        if action == "list":
            return Reply(settings_markdown(self.profile, self.root,
                                           group=group),
                         tool="settings" + (f" · {_escape(group)}" if group
                                            else ""))
        item = setting_spec(key)
        if item is None:
            known = ", ".join(spec["key"] for spec in SPECS)
            return Reply(f"Unknown setting `{_escape(key)}`.  Known settings: "
                         f"{known}.", error=True)
        if action in {"", "show"}:
            return Reply(setting_describe(self.profile, item, self.root),
                         tool="settings · show")

        # a value is needed for set/toggle; reset uses the default
        raw = args.get("value")
        if action == "reset":
            current = setting_values(self.profile, self.root).get(key)
            if item["key"] in SERVICE_KEYS:
                return Reply("The music folder has no default; give a path, "
                             "for example “change the music folder to "
                             "/home/you/Musik”.", error=True)
            try:
                setting_reset(self.profile, item, self.root)
                self._settings_applied(item)
            except (OSError, ValueError) as exc:
                return Reply(f"That did not work: {exc}", error=True)
            return Reply(f"**{item['label']}** is back to its default: "
                         f"**{self._setting_shown(item)}**.",
                         tool="settings · reset")
        if action == "toggle" and item["kind"] == "bool":
            raw = not bool(setting_values(self.profile,
                                          self.root).get(key))
        if raw in (None, ""):
            if item["kind"] == "bool" and action == "set":
                raw = True
            elif item["kind"] == "secret":
                return Reply(f"Give the key itself, for example “set the API "
                             f"key to …”.", error=True)
            else:
                return Reply(setting_describe(self.profile, item, self.root)
                             + "\n\nAdd the value you want, for example "
                             f"“set the {item['label'].casefold()} to …”.",
                             error=False)
        try:
            value = setting_coerce(item, raw)
        except ValueError as exc:
            return Reply(f"That did not work: {exc}", error=True)

        if item["key"] in SERVICE_KEYS:
            return self._settings_library(value)

        # a sentence may configure several related settings at once
        extras: list[tuple[dict, object]] = []
        for other_key, other_value in (args.get("also") or {}).items():
            other = setting_spec(str(other_key))
            if other is None or other["key"] in SERVICE_KEYS \
                    or other["key"] == item["key"]:
                continue
            try:
                extras.append((other, setting_coerce(other, other_value)))
            except ValueError:
                continue
        extra_keys = {other["key"] for other, _value in extras}

        # a new provider must not keep the previous one's address or model
        if item["key"] == "llm_provider":
            for other_key in ("llm_base_url", "llm_model"):
                if other_key not in extra_keys:
                    setting_apply(self.profile, setting_spec(other_key), "",
                                  self.root)
            if not self.profile.setting("use_llm", True):
                setting_apply(self.profile, setting_spec("use_llm"), True,
                              self.root)
        # an explicit address decides the provider: a known preset names it,
        # anything else is a custom OpenAI-compatible server
        if item["key"] == "llm_base_url":
            preset = next((name for name, base in PROVIDER_BASE.items()
                           if str(value).rstrip("/") == base), "")
            extras = [(other, val) for other, val in extras
                      if other["key"] != "llm_provider"]
            extras.append((setting_spec("llm_provider"), preset or "custom"))
            extra_keys.add("llm_provider")

        changed = [(item, value)] + extras
        for other, other_value in changed:
            try:
                setting_apply(self.profile, other, other_value, self.root)
            except (OSError, ValueError) as exc:
                return Reply(f"Could not save **{other['label']}**: {exc}",
                             error=True)
            self._settings_applied(other)

        lines = [f"**{other['label']}** is now **{self._setting_shown(other)}**."
                 for other, _val in changed]
        if item["key"].startswith("llm_") or "llm_provider" in extra_keys:
            lines.append("")
            lines.append(f"Planner: {self.llm.summary()}.")
            lines.append(self._llm_help())
        return Reply("\n\n".join(part for part in lines if part),
                     tool="settings · set")

    def _llm_help(self) -> str:
        """A short, honest note about what the chosen planner still needs."""
        if not self.llm.enabled:
            return ("The planner is off — say “turn on the language model” "
                    "when the server is set up.")
        provider = str(self.profile.setting("llm_provider", "auto") or "auto")
        if provider in {"ollama", "gwdg", "custom"} \
                and not self.llm.available():
            if provider == "gwdg":
                return ("Add the key: “set the api key to …”, then choose a "
                        "model with “use <name> as the model”.")
            if provider == "custom":
                return ("Give the address (“set the server to …”) and the key "
                        "(“set the api key to …”).")
        models = self.llm.models()
        if models and not self.profile.setting("llm_model", ""):
            shown = ", ".join(f"`{name}`" for name in models[:6])
            more = "" if len(models) <= 6 else f" … ({len(models)} total)"
            return f"The server offers {shown}{more}.  Name one with “use " \
                   f"<name> as the model”."
        return ""

    def library_health(self) -> dict:
        """The read-only audit behind the health answer and the panel."""
        return health.summary(self.root)

    def library_stats(self, focus: str = "") -> dict:
        """The read-only statistics behind the panel and the chat."""
        from . import stats as stats_mod
        return stats_mod.summary(self.root)

    def library_duplicates(self) -> dict:
        """The read-only duplicate review behind the panel and the chat."""
        return duplicates_mod.summary(self.root)

    def skill_duplicates(self, args: dict, request: str) -> Reply:
        """The duplicate groups the reports still list — read-only, no delete."""
        action = str(args.get("action") or "list").casefold()
        data = self.library_duplicates()
        if action in {"status", "count", "summary"}:
            if not data["groups"]:
                return Reply("**No duplicate groups are open.**  Every copy "
                             "left in the tree is a different recording or a "
                             "different master.", tool="duplicates · status")
            reasons = " · ".join(f"{name} {count:,}"
                                 for name, count in
                                 sorted(data["reasons"].items()))
            return Reply(
                f"**{data['groups']:,} duplicate groups** · "
                f"{data['files']:,} files · {data['present']:,} still on disk "
                f"({reasons}).\n\nAll of them are `action=review`: the same "
                "recording on two albums, where both copies are wanted.  "
                "Nothing is deleted from this app.", tool="duplicates · status")
        groups = data.get("items") or []
        if action in {"show", "group", "one", "detail"}:
            group = self._duplicate_group(groups, args.get("target"))
            if group is None:
                return Reply("Which duplicate group?  Say “show duplicate 3” "
                             "or name the artist.", error=True,
                             tool="duplicates · show")
            tracks = [self.catalog.track(track["rel"])
                      for track in group["tracks"]]
            found = [track for track in tracks if track is not None]
            if found:
                self.ctx.last_tracks = found
            return Reply(duplicates_mod.describe(group), tracks=found,
                         tool="duplicates · show")
        if action in {"play", "hear"}:
            group = self._duplicate_group(groups, args.get("target"))
            if group is None:
                return Reply("Which duplicate group?  Say “show duplicate 3” "
                             "first.", error=True, tool="duplicates · play")
            wanted = str(args.get("number") or args.get("copy") or "1")
            try:
                index = max(0, int(wanted) - 1)
            except ValueError:
                index = 0
            copies = group["tracks"]
            index = min(index, len(copies) - 1)
            track = self.catalog.track(copies[index]["rel"])
            if track is None:
                return Reply("That copy is not in the current catalogue — the "
                             "file may have been quarantined.", error=True,
                             tool="duplicates · play")
            return self.skill_play({"target": track.rel}, request)
        return Reply(duplicates_mod.markdown(data), tool="duplicates")

    def _duplicate_group(self, groups: list[dict], target) -> dict | None:
        """A group by its number in the list, or by artist/title words."""
        if not groups:
            return None
        text = str(target or "").strip().casefold()
        if not text:
            return groups[0]
        number = re.match(r"^(?:duplicate\s+|group\s+)?(\d{1,3})$", text)
        if number:
            index = int(number.group(1)) - 1
            return groups[index] if 0 <= index < len(groups) else None
        for group in groups:
            first = group["tracks"][0] if group["tracks"] else {}
            haystack = " ".join([
                str(first.get("artist") or ""),
                str(first.get("title") or ""),
                " ".join(str(track.get("album") or "")
                         for track in group["tracks"]),
            ]).casefold()
            if text in haystack or all(word in haystack for word in text.split()):
                return group
        return None

    # -- what arrived lately ----------------------------------------------

    def library_arrivals(self, limit: int = arrivals_mod.DEFAULT_LIMIT,
                         days: int | None = None) -> dict:
        """The newest files in the tree — read-only, newest first."""
        return arrivals_mod.summary(self.root, limit=limit, days=days)

    def skill_recent(self, args: dict, request: str) -> Reply:
        """List the newest files; “play” queues them as an ad-hoc mix."""
        action = str(args.get("action") or "list").casefold()
        try:
            limit = int(args.get("limit") or arrivals_mod.DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = arrivals_mod.DEFAULT_LIMIT
        limit = max(1, min(200, limit))
        days = args.get("days")
        try:
            days = max(1, min(3650, int(days))) if days else None
        except (TypeError, ValueError):
            days = None
        data = self.library_arrivals(limit=limit, days=days)
        if action in {"status", "count"}:
            return Reply(arrivals_mod.status_markdown(data),
                         tool="recent · status")
        tracks = [self.catalog.track(entry["rel"])
                  for entry in data.get("items") or []]
        found = [track for track in tracks if track is not None]
        if action in {"play", "queue", "hear"}:
            if not found:
                return Reply("Nothing new to play — no file is newer than the "
                             "window you asked for.", error=True,
                             tool="recent · play")
            label = "newest"
            if days:
                label = f"added in the last {days} days"
            queue = self._temp_queue(found[:20], f"recent: {label}",
                                     name="Newest Arrivals")
            ok, message = self._play_path(queue, shuffle=False)
            self.ctx.last_tracks = found
            self.ctx.current = found[0]
            return Reply(
                f"**{len(found[:20])} newest tracks** · newest first"
                + ("\n\n▶ playing" if ok else f"\n\n_{message}_"),
                tracks=found[:20], played=ok, tool="recent · play")
        self.ctx.last_tracks = found
        return Reply(arrivals_mod.markdown(data, limit=min(limit, 20)),
                     tracks=found[:20], tool="recent")

    def skill_overview(self, args: dict, request: str) -> Reply:
        if args.get("help"):
            return Reply(help_markdown() + "\n\n### Examples\n\n"
                         + "\n".join(f"- “{example}”" for example in examples()),
                         tool="help")
        action = str(args.get("action") or "").casefold()
        if action in {"formats", "format", "capabilities", "support"}:
            # "can you play spc files?" — a format is not a title, so it is
            # answered from the capability table, never by searching names
            from . import formats as formats_mod
            return Reply(formats_mod.answer(self.root,
                                            str(args.get("format") or "")),
                         tool="library-overview · formats")
        if action == "health":
            return Reply(health.markdown(self.library_health()),
                         tool="library-overview · health")
        if action in {"stats", "statistics"}:
            from . import stats as stats_mod
            data = stats_mod.summary(self.root)
            focus = str(args.get("focus") or "").casefold()
            return Reply(stats_mod.markdown(data, focus),
                         tool="library-overview · stats")
        if action in {"index", "status", "progress"}:
            return self.skill_index(args, request)
        stats = self.catalog.stats()
        genres = " · ".join(f"{_escape(name)} {count:,}"
                            for name, count in stats["genres"]) or "—"
        kinds = stats.get("playlists", {})
        text = (
            f"**Library** · {stats['files']:,} audio files · "
            f"{stats['hours']:,.1f} h · {stats['artists']:,} artists\n\n"
            f"**Top genres** · {genres}\n\n"
            f"**Playlists** · {kinds.get('shelf', 0)} shelf · "
            f"{kinds.get('psycho', 0)} sound · {kinds.get('mood', 0)} mood · "
            f"{kinds.get('focus', 0)} focus · "
            f"{kinds.get('custom', 0)} custom\n\n"
            f"**Mood analysis** · {stats['features']:,} tracks measured\n\n"
            f"**Sound profiles** · {self.psycho.count():,} tracks in "
            f"{len(self.psycho.families())} families\n\n"
            f"`{stats['root']}`"
        )
        return Reply(text, tool="library-overview")

    # -- implicit learning -------------------------------------------------

    def poll(self) -> dict | None:
        """Called by the UI every couple of seconds."""
        now = time.monotonic()
        if now - self._youtube_at >= 8.0:
            self._youtube_at = now
            try:
                self.youtube_jobs()
            except OSError:
                pass
        status = self.player.status()
        if not status:
            return None
        current = str(status.get("file") or "")
        rel = ""
        if current.startswith(str(self.root)):
            rel = os.path.relpath(current, self.root).replace(os.sep, "/")
        if rel and rel != self._last_rel:
            if (self._last_rel and self._last_pos >= 20
                    and self._last_rel not in self._observed):
                track = self.catalog.track(self._last_rel)
                previous = self._last_duration or 0
                if track is not None and previous > 0:
                    fraction = min(1.0, self._last_pos / previous)
                    mood = (max(self.ctx.last_moods,
                                key=lambda key: self.ctx.last_moods[key])
                            if getattr(self.ctx, "last_moods", None) else "")
                    self.profile.observe(track, fraction, mood)
                    self._observed.add(self._last_rel)
            self._last_rel = rel
            self._last_pos = 0.0
            self._last_duration = 0.0
            if endless.active(self.root):
                endless.set_last(self.root, rel)
            track = self.catalog.track(rel)
            if track is not None:
                self.ctx.current = track
        try:
            self._last_pos = float(status.get("time") or 0)
            self._last_duration = float(status.get("duration") or 0)
        except (TypeError, ValueError):
            pass
        # levelling: an mpv audio filter derived from the stored measurement,
        # so a quiet rip and a loud master sit at the same level.  The audio
        # file is never rewritten; the filter is just playback state.
        self._apply_loudness(rel)
        # radio mode: a queue that never runs dry
        self._endless_tick(status)
        self._radio_tick(status)
        # remember where the playhead is, so a restart can pick it up (the
        # writer throttles itself: a few dozen bytes every 20 s, never a tag)
        self.save_session(status=status)
        return status

    def _apply_loudness(self, rel: str) -> None:
        """Keep mpv's audio filter in step with the track and the setting."""
        if not self.player.running():
            self._loudness_filter = None    # a fresh daemon starts clear
            return
        level = str(self.profile.setting("loudness_level", "off") or "off")
        wanted = (loudness.filter_for(self.root, rel)
                  if level == "track" and rel else "")
        if wanted == self._loudness_filter:
            return
        setter = getattr(self.player, "set_loudness", None)
        if setter is None:                  # a stub player in a test
            return
        if setter(wanted)[0]:
            self._loudness_filter = wanted
