"""The tool surface the optional language model may call.

The planner is an agent, but a fenced one.  It gets a fixed list of named tools
that map onto the app's own validated handlers, and every tool result is plain
JSON.  Read tools query the catalogue, the psychoacoustic profile database, the
playlists and — when the build has an optional download module — the
download queue.  The **only** writes are playlists (create, add, remove,
rename, delete a custom list) and filing new audio through that module.

There is deliberately no tool that deletes, moves or retags a music file, and
no tool takes a filesystem path: a track is always a library-relative
``rel`` path that the catalogue already knows.  So a language model can search
the library freely without ever being able to destroy it.

Two request-level guards keep the writes honest:

* ``delete_playlist`` only runs when the listener's own sentence asks for a
  deletion (``wants_delete``); a model that hallucinates a "cleanup" cannot.
* ``import_download`` only runs when the sentence asks to download/import
  something (``wants_download``), and the import itself still needs a search
  result from this conversation.

Nothing here is imported by the library tools; it is app code.
"""

from __future__ import annotations

from .catalog import fold
from .psycho import CONCEPTS, concept_labels

#: a playlist may be deleted only when the sentence says so
DELETE_WORDS = ("delete", "remove", "drop", "get rid of", "throw away",
                "loeschen", "löschen", "entfernen", "wegwerfen", "schmeiss",
                "schmeiß", "rauswerfen", "aufraeumen", "aufräumen")

#: new audio enters the library through the import module, and only when asked for
DOWNLOAD_WORDS = ("download", "import", "grab", "herunterladen", "runterladen",
                  "lade", "laden", "hole", "hol", "besorg", "integrate")

MAX_LIMIT = 80


def _has_word(text: str, words) -> bool:
    folded = " ".join(fold(text).split())
    padded = f" {folded} "
    return any(f" {fold(word)} " in padded for word in words)


def wants_delete(text: str) -> bool:
    """True when the listener's sentence asks to delete something."""
    return _has_word(text, DELETE_WORDS)


def wants_download(text: str) -> bool:
    """True when the listener's sentence asks to fetch new music."""
    return _has_word(text, DOWNLOAD_WORDS)


def _limit(value, default: int = 12, maximum: int = MAX_LIMIT) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, number))


def _rows(tracks) -> list[dict]:
    out = []
    for track in tracks:
        out.append({
            "rel": track.rel,
            "title": track.name,
            "artist": track.who,
            "album": track.album,
            "genre": track.genre,
            "duration_s": round(track.duration_s or 0),
            "year": track.year,
        })
    return out


class Toolbox:
    """The model's tools, bound to one engine and one user sentence."""

    def __init__(self, engine, request: str = ""):
        self.engine = engine
        self.request = request
        from . import downloads as download_modules
        self.downloads = download_modules.available(engine.root)
        self.calls: list[str] = []
        self.played = False
        self.last_error = ""
        self.last_tracks: list = []
        self.last_tool = ""
        self.last_args: dict = {}
        self.last_result: dict = {}
        self.notes: list[str] = []

    # -- catalogue ---------------------------------------------------------

    @staticmethod
    def build_specs(downloads: bool = True) -> list[dict]:
        """OpenAI-style function definitions, in the order shown to the model."""
        def tool(name: str, description: str, properties: dict,
                 required: list[str] | None = None) -> dict:
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required or [],
                    },
                },
            }

        tools = [
            tool("library_overview",
                 "How big the library is: number of files, hours, artists, top "
                 "genres and playlist counts. Read-only.",
                 {}, []),
            tool("search_library",
                 "Full-text search over title, artist, album, genre, the old "
                 "shelf (GROUPING) and the path; ignores case and umlauts. "
                 "Every returned track is identified by its library-relative "
                 "'rel' path — use that path, never an invented one.",
                 {"query": {"type": "string"},
                  "limit": {"type": "integer", "default": 12}}, ["query"]),
            tool("filter_library",
                 "Filter the inventory by metadata: genre, artist, year, "
                 "duration, extension or a word in the title/path. Use for "
                 "questions like 'which metal tracks are shorter than 3 "
                 "minutes'. Read-only.",
                 {"genre": {"type": "string"},
                  "artist": {"type": "string"},
                  "year": {"type": "string", "description": "e.g. 1998"},
                  "title": {"type": "string", "description": "word in title/path"},
                  "min_duration_s": {"type": "number"},
                  "max_duration_s": {"type": "number"},
                  "ext": {"type": "string", "description": "e.g. .mp3"},
                  "limit": {"type": "integer", "default": 30}}, []),
            tool("rank_library",
                 "Rank measured tracks by sound properties (slowest, most "
                 "boring, most aggressive, densest, ...). Each property is a "
                 "mix of the twelve measured axes. Use this instead of a text "
                 "search whenever the request names how the music should "
                 "sound. Read-only.",
                 {"properties": {"type": "array",
                                 "items": {"type": "string"}},
                  "least": {"type": "array",
                            "items": {"type": "string"},
                            "description": "properties to minimise"},
                  "within": {"type": "string",
                             "description": "genre or artist name to rank within"},
                  "limit": {"type": "integer", "default": 12}}, ["properties"]),
            tool("song_properties",
                 "The twelve measured sound axes of one track, plus family and "
                 "the raw measurements. Read-only.",
                 {"track": {"type": "string",
                            "description": "rel path, title, or 'current'"}},
                 ["track"]),
            tool("similar_songs",
                 "Find measured tracks near one track, optionally after moving "
                 "axes (e.g. {'speed': 0.2} = clearly faster). Read-only.",
                 {"track": {"type": "string"},
                  "adjustments": {"type": "object",
                                  "description": "axis -> delta, -1..1"},
                  "limit": {"type": "integer", "default": 12}},
                 ["track"]),
            tool("list_playlists",
                 "All playlists with kind (shelf, mood, focus, custom) and "
                 "track count. Only custom lists can be edited or deleted. "
                 "Read-only.", {}, []),
            tool("show_playlist",
                 "The tracks of one playlist, numbered. Read-only.",
                 {"name": {"type": "string"},
                  "limit": {"type": "integer", "default": 40}}, ["name"]),
            tool("make_playlist",
                 "Create a custom playlist from moods, a genre or a seed track. "
                 "This writes Playlists/Custom/<name>.m3u8 through the "
                 "journalled playlist editor.",
                 {"name": {"type": "string"},
                  "moods": {"type": "object",
                            "description": "mood -> weight, e.g. {'Chill': 0.7}"},
                  "count": {"type": "integer"},
                  "duration_min": {"type": "number"},
                  "seed": {"type": "string",
                           "description": "track or artist to sound like"},
                  "include_genres": {"type": "array",
                                     "items": {"type": "string"}},
                  "exclude_genres": {"type": "array",
                                     "items": {"type": "string"}},
                  "autoplay": {"type": "boolean"}}, ["name"]),
            tool("add_to_playlist",
                 "Append tracks to a custom playlist (creates it when missing). "
                 "'tracks' are rel paths, result numbers, titles or "
                 "'artist - title'. Writes through the journalled editor.",
                 {"playlist": {"type": "string"},
                  "tracks": {"type": "array", "items": {"type": "string"}}},
                 ["playlist", "tracks"]),
            tool("remove_from_playlist",
                 "Remove entries from a custom playlist by their 1-based "
                 "number. Writes through the journalled editor.",
                 {"playlist": {"type": "string"},
                  "indexes": {"type": "array", "items": {"type": "integer"}}},
                 ["playlist", "indexes"]),
            tool("rename_playlist",
                 "Rename a custom playlist. Writes through the journalled "
                 "editor.",
                 {"playlist": {"type": "string"},
                  "to": {"type": "string"}}, ["playlist", "to"]),
            tool("delete_playlist",
                 "Delete a custom playlist file. Its content is journalled "
                 "first, so it can be restored. Only allowed when the "
                 "listener's own sentence asks for a deletion; generated "
                 "shelf/mood/focus lists are never deleted.",
                 {"playlist": {"type": "string"}}, ["playlist"]),
            tool("play_music",
                 "Play a track, playlist, genre/artist queue or search words. "
                 "'target' is resolved by the app, never a raw path.",
                 {"target": {"type": "string"},
                  "shuffle": {"type": "boolean"},
                  "append": {"type": "boolean"},
                  "whole_library": {"type": "boolean"}}, ["target"]),
            tool("transport",
                 "Control playback: pause, resume, toggle, stop, next, prev, "
                 "volume, seek.",
                 {"action": {"type": "string"},
                  "value": {"type": "number"}}, ["action"]),
            tool("import_search",
                 "Search the installed download module without downloading. Returns "
                 "numbered hits with url, title and channel.",
                 {"query": {"type": "string"},
                  "count": {"type": "integer", "default": 6}}, ["query"]),
            tool("import_download",
                 "Download a hit from the last download-module search into the "
                 "library. Needs a download/import request from the listener. "
                 "The installed module tags and files it with a journal.",
                 {"indexes": {"type": "array", "items": {"type": "integer"}},
                  "all": {"type": "boolean"},
                  "url": {"type": "string",
                          "description": "a direct video URL instead"}}, []),
            tool("remember_preference",
                 "Record that the listener likes or dislikes something, or "
                 "store a phrase-to-mood mapping. App state only.",
                 {"kind": {"type": "string",
                           "description": "like|dislike|note|mapping|forget"},
                  "value": {"type": "string"},
                  "moods": {"type": "object"}}, ["kind", "value"]),
            tool("list_settings",
                 "Every app setting with its current value. Read-only; a "
                 "secret (the API key) is only reported as set or not set.",
                 {"group": {"type": "string",
                            "description": "only this group, e.g. Voice"}},
                 []),
            tool("change_setting",
                 "Change one app setting. No music file is touched. Keys "
                 "include visualizer, viz_style, autoplay, use_llm, "
                 "default_count, voice_enabled, voice_autosend, "
                 "voice_language, voice_model, psycho_auto, psycho_set, "
                 "lyrics_panel, llm_provider, llm_base_url, llm_model, "
                 "llm_api_key. 'library' changes the music folder.",
                 {"key": {"type": "string"},
                  "value": {"type": ["string", "number", "boolean"]}},
                 ["key", "value"]),
            tool("list_language_models",
                 "The models the configured language-model server offers. "
                 "Read-only; may take a second when the server is slow.",
                 {}, []),
        ]
        if not downloads:
            tools = [spec for spec in tools
                     if spec["function"]["name"] not in
                     {"import_search", "import_download"}]
        return tools

    def specs(self) -> list[dict]:
        """The tools this build offers (download tools need a module)."""
        return self.build_specs(self.downloads)

    # -- dispatch ----------------------------------------------------------

    def call(self, name: str, args: dict) -> dict:
        """Run one tool call and return a JSON-serialisable result."""
        if not isinstance(args, dict):
            args = {}
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None or name not in {spec["function"]["name"]
                                           for spec in self.specs()}:
            self.last_error = f"unknown tool {name!r}"
            return {"error": f"unknown tool {name!r}"}
        self.calls.append(name)
        try:
            result = handler(args)
        except SystemExit as exc:                 # playlist_edit refuses
            self.last_error = str(exc)
            return {"error": str(exc)}
        except Exception as exc:                   # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"error": f"{type(exc).__name__}: {exc}"}
        result = result if isinstance(result, dict) else {"result": result}
        self.last_tool = name
        self.last_args = dict(args)
        self.last_result = result
        return result

    # -- read tools --------------------------------------------------------

    def _tool_library_overview(self, args: dict) -> dict:
        stats = self.engine.catalog.stats()
        return {
            "files": stats.get("files", 0),
            "hours": round(float(stats.get("hours") or 0), 1),
            "artists": stats.get("artists", 0),
            "genres": [{"genre": name, "tracks": count}
                       for name, count in stats.get("genres", [])],
            "playlists": stats.get("playlists", {}),
            "measured_profiles": self.engine.psycho.count(),
        }

    def _tool_search_library(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "query is empty"}
        hits = self.engine.catalog.search(query, _limit(args.get("limit"), 12))
        self.engine.ctx.last_tracks = hits
        self.engine.ctx.last_query = query
        self.last_tracks = hits
        return {"query": query, "count": len(hits), "tracks": _rows(hits)}

    def _tool_filter_library(self, args: dict) -> dict:
        want_genre = fold(str(args.get("genre") or ""))
        want_artist = fold(str(args.get("artist") or ""))
        want_year = str(args.get("year") or "").strip()
        want_title = fold(str(args.get("title") or ""))
        want_ext = str(args.get("ext") or "").strip().lower()
        try:
            low = float(args.get("min_duration_s") or 0)
        except (TypeError, ValueError):
            low = 0.0
        try:
            high = float(args.get("max_duration_s") or 0)
        except (TypeError, ValueError):
            high = 0.0
        found = []
        for row in self.engine.catalog.rows:
            if want_genre and want_genre not in fold(row.get("genre") or ""):
                continue
            if want_artist and want_artist not in fold(
                    (row.get("artist") or "") + " " + (row.get("albumartist") or "")):
                continue
            if want_year and want_year not in str(row.get("date") or ""):
                continue
            if want_ext and (row.get("ext") or "").lower() != want_ext:
                continue
            if want_title and want_title not in fold(
                    (row.get("title") or "") + " " + (row.get("rel_path") or "")):
                continue
            duration = float(row.get("duration_s") or 0)
            if low and duration < low:
                continue
            if high and duration > high:
                continue
            found.append(self.engine.catalog.track(row.get("rel_path") or ""))
        found = [track for track in found if track is not None]
        limit = _limit(args.get("limit"), 30)
        if not found:
            return {"count": 0, "tracks": [],
                    "note": "no track matches those metadata filters"}
        self.engine.ctx.last_tracks = found[:limit]
        self.engine.ctx.last_query = "filtered library"
        self.last_tracks = found[:limit]
        return {"count": len(found), "tracks": _rows(found[:limit])}

    def _tool_rank_library(self, args: dict) -> dict:
        terms: list[tuple[str, float]] = []
        for name in args.get("properties") or []:
            terms.append((str(name), 1.0))
        for name in args.get("least") or []:
            terms.append((str(name), -1.0))
        cleaned = self.engine._rank_terms(
            {"terms": [list(pair) for pair in terms]})
        if not cleaned:
            return {"error": "none of those properties is known",
                    "known": sorted(CONCEPTS)}
        limit = _limit(args.get("limit"), 12)
        rows, tracks, used = self.engine._rank_rows(
            cleaned, limit, str(args.get("within") or "").strip())
        if not rows:
            return {"error": "nothing measured matches",
                    "within": used, "count": 0}
        labels = concept_labels(cleaned)
        self.last_tracks = tracks
        scores = {row["rel"]: row for row in rows}
        out = []
        for track in tracks:
            row = scores.get(track.rel) or {}
            out.append(dict(
                _rows([track])[0],
                score=row.get("score"),
                family=row.get("family"),
                axes={key: round(value, 2) for key, value
                      in (row.get("axes") or {}).items()},
            ))
        return {"properties": labels, "within": used or "whole library",
                "count": len(out), "tracks": out}

    def _tool_song_properties(self, args: dict) -> dict:
        rel = self._rel_for(args.get("track"), default_current=True)
        if not rel:
            return {"error": "which track?"}
        profile = self.engine.psycho.describe(rel)
        if profile is None:
            return {"error": f"{rel} is not measured yet"}
        track = self.engine.catalog.track(rel)
        return {
            "rel": rel,
            "title": track.name if track else "",
            "artist": track.who if track else "",
            "family": profile.get("family"),
            "estimated": profile.get("estimated"),
            "axes": {key: (None if value is None else round(value, 3))
                     for key, value in (profile.get("axes") or {}).items()},
            "measured": profile.get("measured") or {},
        }

    def _tool_similar_songs(self, args: dict) -> dict:
        rel = self._rel_for(args.get("track"), default_current=True)
        if not rel:
            return {"error": "which track?"}
        adjustments = {}
        for axis, delta in (args.get("adjustments") or {}).items():
            try:
                adjustments[str(axis)] = max(-1.0, min(1.0, float(delta)))
            except (TypeError, ValueError):
                continue
        hits = self.engine.psycho.similar(
            rel, adjustments=adjustments,
            limit=_limit(args.get("limit"), 12))
        out = []
        found = []
        for hit in hits:
            track = self.engine.catalog.track(hit.get("rel") or "")
            if track is None:
                continue
            found.append(track)
            out.append(dict(
                _rows([track])[0],
                distance=round(float(hit.get("distance") or 0), 3),
                family=hit.get("family") or "",
            ))
        self.last_tracks = found
        return {"rel": rel, "adjustments": adjustments, "count": len(out),
                "tracks": out}

    def _tool_list_playlists(self, args: dict) -> dict:
        playlists = self.engine.catalog.playlists(refresh=True)
        return {
            "count": len(playlists),
            "playlists": [{"name": playlist.name, "kind": playlist.kind,
                           "tracks": playlist.count}
                          for playlist in playlists],
        }

    def _tool_show_playlist(self, args: dict) -> dict:
        name = str(args.get("name") or "").strip()
        playlist = self.engine.catalog.find_playlist(name)
        if playlist is None:
            return {"error": f"no playlist called {name!r}"}
        tracks = self.engine.catalog.playlist_tracks(playlist)
        self.engine.ctx.last_tracks = tracks[:100]
        self.engine.ctx.last_playlist = playlist.name
        self.last_tracks = tracks
        limit = _limit(args.get("limit"), 40)
        return {"name": playlist.name, "kind": playlist.kind,
                "count": len(tracks), "tracks": _rows(tracks[:limit])}

    # -- write tools (playlists and imports only) --------------------------

    def _tool_make_playlist(self, args: dict) -> dict:
        moods = args.get("moods")
        if isinstance(moods, list):
            moods = {str(name): 1.0 for name in moods}
        payload = {
            "name": str(args.get("name") or "").strip(),
            "moods": moods if isinstance(moods, dict) else {},
            "count": args.get("count"),
            "duration_min": args.get("duration_min"),
            "seed": str(args.get("seed") or "").strip(),
            "include_genres": args.get("include_genres") or [],
            "exclude_genres": args.get("exclude_genres") or [],
            "autoplay": bool(args.get("autoplay")),
        }
        return self._reply("make-playlist", payload)

    def _tool_add_to_playlist(self, args: dict) -> dict:
        name = str(args.get("playlist") or "").strip()
        tracks = [str(value) for value in (args.get("tracks") or [])
                  if str(value).strip()]
        if not name:
            return {"error": "which playlist?"}
        if not tracks:
            return {"error": "which tracks?"}
        return self._reply("edit-playlist", {
            "action": "add", "playlist": name, "tracks": tracks})

    def _tool_remove_from_playlist(self, args: dict) -> dict:
        name = str(args.get("playlist") or "").strip()
        numbers = []
        for value in args.get("indexes") or []:
            try:
                numbers.append(int(value))
            except (TypeError, ValueError):
                continue
        if not name:
            return {"error": "which playlist?"}
        if not numbers:
            return {"error": "which entry numbers?"}
        return self._reply("edit-playlist", {
            "action": "remove", "playlist": name, "indexes": numbers})

    def _tool_rename_playlist(self, args: dict) -> dict:
        name = str(args.get("playlist") or "").strip()
        to = str(args.get("to") or "").strip()
        if not name or not to:
            return {"error": "which playlist, and to what name?"}
        return self._reply("edit-playlist", {
            "action": "rename", "playlist": name, "to": to})

    def _tool_delete_playlist(self, args: dict) -> dict:
        name = str(args.get("playlist") or "").strip()
        if not name:
            return {"error": "which playlist?"}
        if not wants_delete(self.request):
            return {"error": "the listener did not ask to delete anything — "
                             "confirm first, then delete"}
        return self._reply("edit-playlist", {
            "action": "delete", "playlist": name})

    def _tool_play_music(self, args: dict) -> dict:
        target = str(args.get("target") or "").strip()
        if not target:
            return {"error": "play what?"}
        result = self._reply("play-music", {
            "target": target,
            "shuffle": bool(args.get("shuffle")),
            "append": bool(args.get("append")),
            "whole_library": bool(args.get("whole_library")),
        })
        self.played = bool(result.get("played"))
        if str(result.get("tool") or "").startswith("search-library"):
            # the engine follows the sentence, not the plan: without a play
            # word the matches are listed, so say so instead of "playing"
            result["note"] = ("nothing started: the listener did not ask to "
                              "play it, so the matches were listed")
            self.notes.append("play_music: listed the matches, did not play")
        return result

    def _tool_transport(self, args: dict) -> dict:
        action = str(args.get("action") or "").strip()
        if not action:
            return {"error": "which transport action?"}
        return self._reply("transport", {"action": action,
                                          "value": args.get("value")})

    def _tool_import_search(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "search for what?"}
        result = self._reply("import-audio", {
            "action": "search", "query": query,
            "count": _limit(args.get("count"), 6, 20)})
        note = getattr(self.engine.ctx, "last_youtube", None) or {}
        result["results"] = note.get("results") or []
        return result

    def _tool_import_download(self, args: dict) -> dict:
        url = str(args.get("url") or "").strip()
        if not url and not wants_download(self.request):
            return {"error": "the listener did not ask to download anything"}
        payload: dict = {"action": "download"}
        if url:
            payload["url"] = url
        else:
            indexes = []
            for value in args.get("indexes") or []:
                try:
                    indexes.append(int(value))
                except (TypeError, ValueError):
                    continue
            if indexes:
                payload["indexes"] = indexes
            if args.get("all"):
                payload["all"] = True
        return self._reply("import-audio", payload)

    def _tool_remember_preference(self, args: dict) -> dict:
        kind = str(args.get("kind") or "").strip().casefold()
        value = str(args.get("value") or "").strip()
        if kind not in {"like", "dislike", "note", "mapping", "forget"}:
            return {"error": f"kind must be like, dislike, note, mapping or "
                             f"forget (got {kind!r})"}
        if not value:
            return {"error": "what should be remembered?"}
        return self._reply("learn-preferences", {
            "kind": kind, "value": value, "moods": args.get("moods") or {}})

    def _tool_list_settings(self, args: dict) -> dict:
        from .settings import api as settings_api

        group = str(args.get("group") or "").strip()
        rows = settings_api(self.engine.profile, self.engine.root)
        out = []
        for row in rows:
            if group and row["group"].casefold() != group.casefold():
                continue
            value = row["value"]
            if row["secret"]:
                value = "set" if row["has_value"] else ""
            out.append({"key": row["key"], "label": row["label"],
                        "group": row["group"], "kind": row["kind"],
                        "value": value, "choices": row["choices"]})
        return {"count": len(out), "settings": out}

    def _tool_change_setting(self, args: dict) -> dict:
        key = str(args.get("key") or "").strip()
        if not key:
            return {"error": "which setting?"}
        result = self._reply("settings", {"action": "set", "key": key,
                                          "value": args.get("value")})
        deferred = self.engine.deferred.get("library")
        if deferred:
            result["library"] = deferred
        return result

    def _tool_list_language_models(self, args: dict) -> dict:
        models = self.engine.llm.models()
        return {"count": len(models), "models": models,
                "planner": self.engine.llm.summary(),
                "error": self.engine.llm.last_error or ""}

    # -- plumbing ----------------------------------------------------------

    def _rel_for(self, value, default_current: bool = False) -> str:
        text = str(value or "").strip()
        if text.casefold() in {"current", "this", "now", "das", ""}:
            if default_current:
                track = (self.engine.ctx.current
                         or (self.engine.ctx.last_tracks[0]
                             if self.engine.ctx.last_tracks else None))
                return track.rel if track is not None else ""
            return ""
        track = self.engine.catalog.track(text)
        if track is not None:
            return track.rel
        hits = self.engine.catalog.search(text, 1)
        return hits[0].rel if hits else ""

    def _reply(self, skill: str, args: dict) -> dict:
        """Run a real skill with the engine's validation and report it."""
        from .nlu import Plan

        if self.engine.dry_run and skill != "transport":
            self.notes.append(f"dry run: {skill} was not executed")
            return {"dry_run": True, "skill": skill, "args": args}
        reply = self.engine._execute(Plan(skill, args, source="llm"), self.request)
        if reply.tracks:
            self.last_tracks = list(reply.tracks)
        result = {"skill": skill, "ok": not reply.error,
                  "say": reply.text,
                  "tracks": _rows(reply.tracks[:20]) if reply.tracks else [],
                  "played": bool(reply.played)}
        if reply.tool:
            result["tool"] = reply.tool
        if reply.error:
            result["error"] = reply.text
        return result

    def summary(self) -> str:
        """A short human line about what the tools actually did."""
        if not self.calls:
            return ""
        lines: list[str] = []
        for note in self.notes:
            lines.append(note)
        if self.played:
            lines.append("playback started")
        return "\n".join(lines)

    def report(self) -> str:
        """A readable answer built from the last tool result.

        Used when the model is done calling tools but sends no final text (some
        providers do that): the listener then sees what was found instead of a
        bare "Done.".
        """
        result = self.last_result or {}
        name = self.last_tool
        if not result:
            return self.summary()
        if result.get("error"):
            return f"The tool {name} failed: {result['error']}"
        if name == "library_overview":
            parts = [f"**{int(result.get('files') or 0):,}** audio files",
                     f"{float(result.get('hours') or 0):.0f} hours",
                     f"{int(result.get('artists') or 0):,} artists",
                     f"{int(result.get('measured_profiles') or 0):,} measured "
                     "profiles"]
            return "Your library: " + ", ".join(parts) + "."
        if name == "list_playlists":
            rows = result.get("playlists") or []
            by_kind: dict[str, int] = {}
            for row in rows:
                by_kind[row.get("kind") or "?"] = by_kind.get(
                    row.get("kind") or "?", 0) + 1
            kinds = ", ".join(f"{count} {kind}" for kind, count
                              in sorted(by_kind.items()))
            return f"{len(rows)} playlists: {kinds}."
        tracks = result.get("tracks") or []
        if name == "show_playlist" and not tracks:
            return (f"**{result.get('name')}** holds "
                    f"{int(result.get('count') or 0):,} tracks, none of which "
                    "this library still has.")
        if not tracks:
            if "count" in result:
                return f"{name.replace('_', ' ').capitalize()}: nothing found."
            return self.summary()

        head = name.replace("_", " ")
        where = self._args_line()
        if name == "rank_library":
            labels = ", ".join(result.get("properties") or []) or "your request"
            area = result.get("within") or "the whole library"
            lead = f"**{labels}** — ranked over {area}:"
        elif name in {"search_library", "filter_library"}:
            lead = (f"The library lookup returned "
                    f"**{int(result.get('count') or len(tracks)):,} tracks**"
                    f"{where}:")
        elif name == "similar_songs":
            lead = f"Tracks near **{result.get('rel')}**:"
        elif name == "show_playlist":
            lead = (f"**{result.get('name')}** · "
                    f"{int(result.get('count') or len(tracks)):,} tracks:")
        else:
            lead = f"{head.capitalize()}{where}:"
        lines = [lead]
        for number, row in enumerate(tracks[:8], 1):
            line = f"{number}. **{row.get('artist')} — {row.get('title')}**"
            album = row.get("album")
            if album:
                line += f"  ·  {album}"
            if row.get("score") is not None:
                line += f"  ·  score {float(row['score']):.2f}"
            if row.get("distance") is not None:
                line += f"  ·  distance {float(row['distance']):.2f}"
            if row.get("duration_s"):
                seconds = int(row["duration_s"])
                line += f"  ·  {seconds // 60}:{seconds % 60:02d}"
            lines.append(line)
        if len(tracks) > 8:
            lines.append(f"… and {len(tracks) - 8} more.")
        return "\n".join(lines)

    def _args_line(self) -> str:
        """The filters that were really applied, for an honest fallback."""
        args = self.last_args or {}
        parts = []
        for key in ("query", "genre", "artist", "year", "title", "ext"):
            value = str(args.get(key) or "").strip()
            if value:
                parts.append(f"{key} {value!r}")
        low, high = args.get("min_duration_s"), args.get("max_duration_s")
        if low:
            parts.append(f"longer than {float(low):g} s")
        if high:
            parts.append(f"shorter than {float(high):g} s")
        return f" ({', '.join(parts)})" if parts else ""
