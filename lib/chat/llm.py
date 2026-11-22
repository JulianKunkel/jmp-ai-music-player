"""Optional language-model planner (OpenAI-compatible, no SDK).

The app is fully usable without this module: the offline parser in ``nlu.py``
handles every built-in skill.  A model is only asked when the offline parser is
unsure, and it can only choose one of the known skills with JSON arguments —
it never gets a shell, a file path it invented, or permission to run anything.

Configuration, highest priority first:

1. the app's own settings (`library-work/app/chat/profile.json` +
   `secrets.json`): ``llm_provider`` (``ollama``/``gwdg``/``custom``),
   ``llm_base_url``, ``llm_model`` and ``llm_api_key`` — this is how the
   listener picks a local Ollama server, the GWDG/SAIA endpoint or any other
   OpenAI-compatible API, and it is the only place a key is stored (0600);
2. ``MUSIC_LLM_CONFIG`` -> a JSON file:
   ``{"base_url": "...", "api_key": "...", "model": "..."}``
3. ``library-work/app/llm.json`` (travels with the library)
4. ``~/.local/share/julians-music-app/llm.json`` (written by the installer)
5. ``MUSIC_LLM_BASE_URL`` / ``MUSIC_LLM_API_KEY`` / ``MUSIC_LLM_MODEL``
6. OpenCode's own configuration, only as a last-resort fallback

``MUSIC_LLM=off`` disables the whole thing.  The key is never logged and never
returned to the page.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from .settings import PROVIDER_BASE, PROVIDER_KEYS, read_secrets
from .skills import REGISTRY, skills_prompt

TIMEOUT = 45

#: how many tool rounds one request may take before the model must answer
TOOL_ROUNDS = 6


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _opencode_config() -> dict:
    """Last-resort fallback: reuse a local OpenCode provider when one exists.

    This is *not* the normal route any more.  The listener picks a server in
    the app's settings (Ollama, GWDG/SAIA or any OpenAI-compatible API) and the
    key is stored locally; nothing is mounted into the container for this.
    When no setting is made and no ``llm.json`` exists, a host OpenCode
    configuration is read read-only so an existing setup keeps working.
    """
    config_home = Path(os.environ.get("XDG_CONFIG_HOME")
                       or Path.home() / ".config")
    data_home = Path(os.environ.get("XDG_DATA_HOME")
                     or Path.home() / ".local" / "share")
    config_path = Path(os.environ.get("MUSIC_OPENCODE_CONFIG")
                       or config_home / "opencode" / "opencode.json")
    auth_path = Path(os.environ.get("MUSIC_OPENCODE_AUTH")
                     or data_home / "opencode" / "auth.json")
    config = _read_json(config_path)
    auth = _read_json(auth_path)
    provider = config.get("provider") or {}
    # follow the active model's provider first, then the usual suspects
    active = str(config.get("model") or "").split("/", 1)[0]
    order = [active] if active else []
    order += [name for name in ("gwdg", "openai") if name not in order]
    for name in order:
        options = (provider.get(name) or {}).get("options") or {}
        key = (auth.get(name) or {}).get("key") or options.get("apiKey")
        if key and options.get("baseURL"):
            return {
                "base_url": options["baseURL"],
                "api_key": key,
                "model": str(config.get("model") or "").split("/", 1)[-1]
                or "gpt-4o-mini",
                "source": f"opencode:{name}",
            }
    return {}


def _refused_tools(exc: Exception) -> bool:
    """True when the endpoint rejected the ``tools`` field itself."""
    text = str(exc)
    match = re.search(r"HTTP (\d{3})", text)
    if not match or not match.group(1).startswith("4"):
        return False
    low = text.casefold()
    return any(word in low for word in ("tool", "function", "unsupported",
                                        "unknown field", "not supported"))


class LLM:
    def __init__(self, library=None, config_path: str | None = None,
                 enabled: bool = True, profile=None):
        self.library = library
        self.profile = profile
        self.enabled = enabled and os.environ.get("MUSIC_LLM", "").lower() not in {
            "off", "0", "false", "no"}
        self.config = self._resolve(config_path)
        self.last_error = ""
        #: latched when the endpoint rejects OpenAI-style tool calling
        self.tools_unsupported = False
        #: installed models, cached for a minute so a chat turn is not slower
        #: by one network round trip per sentence
        self._models: list[str] | None = None
        self._models_at = 0.0

    # -- configuration -----------------------------------------------------

    def _settings_config(self) -> dict:
        """The planner the listener configured in the app's settings."""
        if self.profile is None:
            return {}
        provider = str(self.profile.setting("llm_provider", "auto") or "auto")
        base = str(self.profile.setting("llm_base_url", "") or "").strip()
        model = str(self.profile.setting("llm_model", "") or "").strip()
        key = ""
        if self.library is not None:
            key = str(read_secrets(self.library).get("llm_api_key") or "").strip()
        if provider == "auto":
            # a full address + key typed into the settings is a custom server
            if not (base and key):
                return {}
            provider = "custom"
        base = base or PROVIDER_BASE.get(provider, "")
        if not base:
            return {}
        return {
            "base_url": base.rstrip("/"),
            "api_key": key or PROVIDER_KEYS.get(provider, ""),
            "model": model,
            "provider": provider,
            "source": f"settings:{provider}",
        }

    def _resolve(self, explicit: str | None) -> dict:
        configured = self._settings_config()
        if configured:
            return configured
        candidates: list[Path] = []
        if explicit:
            candidates.append(Path(explicit).expanduser())
        if os.environ.get("MUSIC_LLM_CONFIG"):
            candidates.append(Path(os.environ["MUSIC_LLM_CONFIG"]).expanduser())
        if self.library is not None:
            candidates.append(self.library / "library-work" / "app" / "llm.json")
        candidates.append(Path.home() / ".local" / "share" /
                          "julians-music-app" / "llm.json")
        for path in candidates:
            data = _read_json(path)
            if data.get("api_key") and data.get("base_url"):
                data.setdefault("model", "gpt-4o-mini")
                data["source"] = str(path)
                return data
        env = {
            "base_url": os.environ.get("MUSIC_LLM_BASE_URL", ""),
            "api_key": os.environ.get("MUSIC_LLM_API_KEY", ""),
            "model": os.environ.get("MUSIC_LLM_MODEL", ""),
        }
        if env["base_url"] and env["api_key"]:
            env["model"] = env["model"] or "gpt-4o-mini"
            env["source"] = "environment"
            return env
        return _opencode_config()

    def reconfigure(self) -> None:
        """Re-read the settings after one of them changed."""
        self.config = self._resolve(None)
        self.last_error = ""
        self.tools_unsupported = False
        self._models = None
        self._models_at = 0.0

    def available(self) -> bool:
        return bool(self.enabled and self.config.get("base_url")
                    and self.config.get("api_key"))

    def summary(self) -> str:
        if not self.enabled:
            return "off (local planner only)"
        if not self.available():
            if self.config.get("base_url"):
                return ("configured, but the server has no key yet "
                        "(set the api key)")
            return "not configured — the local planner handles everything"
        model = str(self.config.get("model") or "").strip()
        if not model:
            # the list is only cached after a request; say so honestly instead
            # of promising a model the server may not offer
            listed = self._models or []
            model = listed[0] if listed else "(no model chosen yet)"
        source = self.config.get("source") or "?"
        mode = "chat only" if self.tools_unsupported else "tools on"
        return f"on · {model} · {source} · {mode}"

    # -- models ------------------------------------------------------------

    def _fetch_json(self, url: str, key: str) -> dict:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "JulAIn-Music-Player/1.0",
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.load(response)
        return data if isinstance(data, dict) else {}

    def models(self, force: bool = False) -> list[str]:
        """The models the configured server offers (empty when unreachable)."""
        if not force and self._models is not None \
                and time.time() - self._models_at < 60:
            return self._models
        base = str(self.config.get("base_url") or "").rstrip("/")
        key = str(self.config.get("api_key") or "")
        names: list[str] = []
        if base and key:
            try:
                data = self._fetch_json(base + "/models", key)
                for row in data.get("data") or []:
                    name = str((row or {}).get("id") or "").strip()
                    if name:
                        names.append(name)
            except (OSError, ValueError, urllib.error.URLError):
                names = []
            if not names:
                # Ollama also answers /api/tags next to its /v1 endpoint
                root = base[:-3] if base.endswith("/v1") else base
                try:
                    data = self._fetch_json(root.rstrip("/") + "/api/tags", key)
                    for row in data.get("models") or []:
                        name = str((row or {}).get("name") or "").strip()
                        if name:
                            names.append(name)
                except (OSError, ValueError, urllib.error.URLError):
                    names = []
        self._models = sorted(dict.fromkeys(names))
        self._models_at = time.time()
        return self._models

    def _model(self) -> str:
        """The configured model, else the first the server lists."""
        model = str(self.config.get("model") or "").strip()
        if model:
            return model
        models = self.models()
        model = models[0] if models else ""
        if model:
            self.config["model"] = model
        return model

    # -- planning ----------------------------------------------------------

    def plan(self, message: str, ctx, profile, stats: dict,
             history: list[dict] | None = None):
        from .nlu import Plan

        if not self.available():
            return None
        model = self._model()
        if not model:
            self.last_error = "no model is configured"
            return None
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": self._system(stats, profile)},
                {"role": "user", "content": self._user(message, ctx, history)},
            ],
            "temperature": 0.1,
            "max_tokens": 900,
        }
        try:
            data = self._post(payload)
        except Exception as exc:  # network, timeout, bad JSON
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        return self._plan_from_text(self._content(data))

    def _plan_from_text(self, text: str):
        from .nlu import Plan

        parsed = self._parse(text)
        if not parsed:
            self.last_error = "the model returned no usable JSON"
            return None
        skill = str(parsed.get("skill") or "").strip()
        args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
        say = str(parsed.get("say") or "").strip()
        if skill == "answer":
            return Plan("answer", {"text": say or text.strip()[:2000]},
                        source="llm")
        if skill not in REGISTRY:
            self.last_error = f"unknown skill {skill!r}"
            return None
        clean = self._clean_args(skill, args)
        return Plan(skill, clean, say=say, confidence=0.8, source="llm")

    # -- the agent: the planner with tools ---------------------------------

    def agent(self, message: str, ctx, profile, stats: dict, toolbox,
              history: list[dict] | None = None) -> dict | None:
        """Run a short tool-calling loop and return ``{"text"|"plan"}``.

        Unlike :meth:`plan` the model may look things up before it answers: it
        gets the tool surface from ``chat/tools.py`` (catalogue search, the
        profile database, playlists, the optional import module) and every call
        is executed by the app's own validated handlers.  There is no tool that deletes, moves or
        retags a music file.

        A provider that does not understand ``tools`` is detected once
        (``tools_unsupported``) and the loop degrades to the JSON planner.
        Returns None when nothing usable came back, so the engine can fall back.
        """
        if not self.available() or toolbox is None:
            return None
        model = self._model()
        if not model:
            self.last_error = "no model is configured"
            return None
        messages = [
            {"role": "system", "content": self._agent_system(stats, profile)},
            {"role": "user", "content": self._user(message, ctx, history)},
        ]
        used_tools = False
        nudged = False
        for _round in range(TOOL_ROUNDS):
            payload = {
                "model": model,
                "messages": messages,
                "temperature": 0.1,
                "max_tokens": 900,
            }
            if not self.tools_unsupported:
                payload["tools"] = toolbox.specs()
                payload["tool_choice"] = "auto"
            try:
                data = self._post(payload)
            except Exception as exc:  # network, timeout, bad JSON
                self.last_error = f"{type(exc).__name__}: {exc}"
                if not self.tools_unsupported and _refused_tools(exc):
                    # the endpoint does not speak OpenAI tool calling: ask the
                    # same question again as a plain JSON planner
                    self.tools_unsupported = True
                    if used_tools:
                        return {"text": toolbox.report(),
                                "tools": list(toolbox.calls)}
                    continue
                return None

            choice = ((data.get("choices") or [{}])[0] or {})
            reply = choice.get("message") or {}
            calls = reply.get("tool_calls") or []
            if calls:
                # keep the assistant turn (with its calls) so the tool results
                # have something to answer to
                messages.append({
                    "role": "assistant",
                    "content": reply.get("content") or "",
                    "tool_calls": calls,
                })
                for call in calls:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "").strip()
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except (TypeError, ValueError):
                        args = {}
                    result = toolbox.call(name, args)
                    used_tools = True
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "content": json.dumps(result, ensure_ascii=False,
                                              default=str)[:6000],
                    })
                continue

            text = str(reply.get("content") or "").strip()
            if not text:
                # Some providers return an empty content field after the last
                # tool round.  Ask once more, then answer from what the tools
                # really returned instead of a bare "Done."
                if used_tools and not nudged:
                    nudged = True
                    messages.append({
                        "role": "user",
                        "content": "Now answer the listener briefly, using the "
                                   "tool results above. Do not call another "
                                   "tool unless it is really needed.",
                    })
                    continue
                if used_tools:
                    return {"text": toolbox.report(),
                            "tools": list(toolbox.calls)}
                return None
            parsed = self._parse(text)
            if parsed and isinstance(parsed.get("skill"), str):
                plan = self._plan_from_text(text)
                if plan is not None:
                    return {"plan": plan, "tools": list(toolbox.calls)}
            return {"text": text, "tools": list(toolbox.calls)}

        self.last_error = "the model kept calling tools without answering"
        if used_tools:
            return {"text": toolbox.report(), "tools": list(toolbox.calls)}
        return None

    def _agent_system(self, stats: dict, profile) -> str:
        genres = ", ".join(f"{name} {count}" for name, count in
                           stats.get("genres", [])[:6]) or "unknown"
        return f"""You are the assistant of "JulAIn Music Player", a music library chat.
You have tools that read this library and act on it. Use them before you
answer — never invent a track, an album or a playlist, and quote the `rel`
path a tool returned when you name a file.

What you can do:
- search the catalogue and filter it by metadata (genre, artist, year, length)
- rank measured tracks by sound (slowest, most boring, most aggressive ...)
- show a track's twelve measured axes and find similar tracks
- list, show, create, edit, rename and delete custom playlists
- play music and control the player
- search the optional import module, and import a hit into the library

Hard rules — these are not negotiable:
- A music file can never be deleted, moved or retagged. No tool does that; do
  not claim otherwise.
- New audio only enters through the optional import module, and only when the
  listener asks for an import.
- The shelf, mood, focus and psycho playlists are generated from the library:
  never edit or delete them, only `custom` lists.

Answer in the listener's language (German or English), briefly and concretely.
When you list several tracks, number them so the listener can say "play 2".
When you did something, say what you did and where it went.

Library: {stats.get('files', 0):,} audio files, {stats.get('hours', 0):.0f} hours,
{stats.get('artists', 0):,} artists, top genres: {genres}.
Measured profiles: {stats.get('profiles', 0) or 'see library_overview'}.
Learned profile: {profile.compact()}"""

    def _system(self, stats: dict, profile) -> str:
        genres = ", ".join(f"{name} {count}" for name, count in
                           stats.get("genres", [])[:6]) or "unknown"
        return f"""You are the planner for "JulAIn Music Player", a music library chat.
Choose exactly one skill and return ONE JSON object, nothing else.

JSON shape:
{{"skill": "<name>", "args": {{...}}, "say": "<one short sentence>"}}

Skills (name: description; arguments):
{skills_prompt()}

Rules:
- Answer in the listener's language (German or English), in "say".
- For a new playlist choose moods from: Chill, Happy, Party, Workout, Dark,
  Melancholy, Epic, Romantic, Focus, Retro. A blend is allowed, e.g. smooth =
  {{"Chill": 0.6, "Romantic": 0.25, "Focus": 0.15}}.
- "target" may be a playlist name, a file path, a URL or search words. Never
  invent a library path.
- If the message is conversation, a greeting, thanks or a question about the
  app, use {{"skill": "answer", "say": "..."}}.
- If the request is ambiguous, use "answer" with one short clarifying question.
- Do not delete anything. The app has no delete skill.

Library: {stats.get('files', 0):,} audio files, {stats.get('hours', 0):.0f} hours,
{stats.get('artists', 0):,} artists, top genres: {genres}.
Learned profile: {profile.compact()}"""

    def _user(self, message: str, ctx, history: list[dict] | None) -> str:
        lines = []
        if ctx.current is not None:
            lines.append(f"Current track: {ctx.current.label()} "
                         f"({ctx.current.rel})")
        if ctx.last_playlist:
            lines.append(f"Last playlist made: {ctx.last_playlist}")
        if ctx.last_tracks:
            lines.append("Last search results (numbered):")
            for number, track in enumerate(ctx.last_tracks[:8], 1):
                lines.append(f"  {number}. {track.label()} ({track.rel})")
        if history:
            lines.append("Recent conversation:")
            for turn in history[-6:]:
                who = turn.get("role", "?")
                text = str(turn.get("text", ""))[:300]
                lines.append(f"  {who}: {text}")
        lines.append("")
        lines.append(f"Request: {message}")
        return "\n".join(lines)

    # -- validation and HTTP ----------------------------------------------

    @staticmethod
    def _clean_args(skill: str, args: dict) -> dict:
        meta = REGISTRY[skill]["args"]
        clean: dict = {}
        for key, value in args.items():
            if key not in meta:
                continue
            if isinstance(value, str):
                clean[key] = value.strip()[:300]
            elif isinstance(value, (int, float, bool)):
                clean[key] = value
            elif isinstance(value, list):
                clean[key] = [str(item)[:80] for item in value[:12]]
            elif isinstance(value, dict):
                clean[key] = {str(k)[:40]: float(v)
                              for k, v in list(value.items())[:10]
                              if isinstance(v, (int, float))}
        return clean

    def _post(self, payload: dict) -> dict:
        base = str(self.config.get("base_url") or "").rstrip("/")
        url = base + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config['api_key']}",
                "Content-Type": "application/json",
                "User-Agent": "JulAIn-Music-Player/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    @staticmethod
    def _content(data: dict) -> str:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return ""
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) for part in content
                               if isinstance(part, dict))
        return str(content or "").strip()

    @staticmethod
    def _parse(text: str) -> dict | None:
        if not text:
            return None
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                      flags=re.IGNORECASE | re.MULTILINE)
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            value = json.loads(text[start:end + 1])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
