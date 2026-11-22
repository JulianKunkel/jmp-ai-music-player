"""The skill catalogue: OpenCode-style folders with a SKILL.md each.

The markdown is the documentation (shown by `/skills`, sent to the optional
LLM).  The executor lives in ``engine.py``; this module only reads and
describes, so adding a skill means adding a folder and a small handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills"

#: name -> (title, short argument help, engine handler)
REGISTRY: dict[str, dict] = {
    "search-library": {
        "title": "Search the library",
        "args": {"query": "words to look for", "limit": "how many results"},
        "handler": "skill_search",
    },
    "rank-library": {
        "title": "Rank by sound property",
        "args": {"terms": "list of [property, sign] pairs, e.g. [[\"slow\", 1]]",
                 "properties": "alternative: names of properties",
                 "limit": "how many", "filter": "genre or artist to rank within",
                 "play": "bool — play the top track"},
        "handler": "skill_rank",
    },
    "play-music": {
        "title": "Play music",
        "args": {"target": "playlist, file, URL or search words",
                 "shuffle": "bool", "append": "bool",
                 "whole_library": "bool"},
        "handler": "skill_play",
    },
    "radio": {
        "title": "Radio mode",
        "args": {"action": "start|stop|status|more",
                 "target": "seed track, artist or genre (default: what plays)"},
        "handler": "skill_radio",
    },
    "transport": {
        "title": "Control the player",
        "args": {"action": "pause|resume|toggle|stop|next|prev|volume|seek",
                 "value": "number for volume/seconds"},
        "handler": "skill_transport",
    },
    "endless": {
        "title": "Endless mode",
        "args": {"action": "start|stop|status|more"},
        "handler": "skill_endless",
    },
    "make-playlist": {
        "title": "Make a playlist",
        "args": {"name": "playlist name", "moods": "list of moods",
                 "count": "number of tracks", "duration_min": "target minutes",
                 "seed": "artist/track to sound like",
                 "include_genres": "list", "exclude_genres": "list",
                 "autoplay": "bool", "overwrite": "bool"},
        "handler": "skill_make_playlist",
    },
    "recommend": {
        "title": "Recommend music",
        "args": {"moods": "list of moods", "include_genres": "list",
                 "exclude_genres": "list", "seed": "artist/track",
                 "count": "how many", "autoplay": "bool"},
        "handler": "skill_recommend",
    },
    "show-playlists": {
        "title": "Show the playlists",
        "args": {},
        "handler": "skill_show_playlists",
    },
    "smart-playlist": {
        "title": "A playlist that rebuilds itself",
        "args": {"action": "list|show|build|refresh|delete",
                 "name": "list name",
                 "rules": "genre, artist, album, grouping, codec, year_from, "
                          "year_to, decade, added_days, min_duration, "
                          "max_duration, min_bitrate, max_bitrate, axes, "
                          "sort, limit"},
        "handler": "skill_smart",
    },
    "save-playlist": {
        "title": "Save the last selection",
        "args": {"name": "playlist name", "overwrite": "bool"},
        "handler": "skill_save_playlist",
    },
    "activity": {
        "title": "Show recent activity",
        "args": {"limit": "how many entries"},
        "handler": "skill_activity",
    },
    "agent": {
        "title": "Skill agent",
        "args": {"action": "show|approve|reject"},
        "handler": "skill_agent",
    },
    "edit-playlist": {
        "title": "Edit a playlist",
        "args": {"action": "list|show|add|remove|rename|delete",
                 "playlist": "name", "to": "new name for rename",
                 "tracks": "paths or result numbers", "indexes": "numbers"},
        "handler": "skill_edit_playlist",
    },
    "import-audio": {
        "title": "Import audio (optional module)",
        "args": {"action": "search|download|status", "query": "what to look for",
                 "url": "video URL", "indexes": "result numbers",
                 "all": "every hit", "count": "how many hits",
                 "similar": "use the current track", "artist": "override",
                 "album": "override", "genre": "override"},
        "handler": "skill_import",
    },
    "psycho-profile": {
        "title": "Show or reshape a song's properties",
        "args": {"action": "show|similar|families|sets|update|status",
                 "track": "artist/track", "adjustments": "axis -> delta",
                 "set": "psychology|timbre|rhythm|dynamics|instrument",
                 "count": "how many results"},
        "handler": "skill_psycho",
    },
    "lyrics": {
        "title": "Show, search or transcribe lyrics",
        "args": {"action": "show|now|next|search|transcribe|setup|status",
                 "track": "artist/track or a file name",
                 "text": "words from the lyrics",
                 "terms": "sound properties to combine, e.g. [[\"fast\", 1]]",
                 "limit": "how many", "refresh": "bool", "all": "bool"},
        "handler": "skill_lyrics",
    },
    "add-file": {
        "title": "Add a local file",
        "args": {"path": "file on disk", "artist": "override",
                 "album": "override", "genre": "override"},
        "handler": "skill_add_file",
    },
    "import-playlist": {
        "title": "Import playlists",
        "args": {"folder": "folder with .m3u/.m3u8/.pls files",
                 "spotify": "Spotify playlist link or id",
                 "recursive": "bool", "apply": "bool"},
        "handler": "skill_import_playlist",
    },
    "learn-preferences": {
        "title": "Learn a preference",
        "args": {"kind": "like|dislike|note|mapping|forget",
                 "value": "what was said", "moods": "list for a mapping"},
        "handler": "skill_learn",
    },
    "recent": {
        "title": "What arrived lately",
        "args": {"action": "list|status|play",
                 "limit": "how many", "days": "only the last N days"},
        "handler": "skill_recent",
    },
    "duplicates": {
        "title": "Review the duplicate groups",
        "args": {"action": "list|status|show|play",
                 "target": "group number or artist/title",
                 "number": "which copy to play"},
        "handler": "skill_duplicates",
    },
    "library-overview": {
        "title": "Library overview",
        "args": {"action": "show|index|status|health|stats",
                 "focus": "genres|artists|albums|decades|formats|added|"
                          "length|quality|playlists|coverage"},
        "handler": "skill_overview",
    },
    "index-library": {
        "title": "Index the library",
        "args": {"action": "index|status|progress"},
        "handler": "skill_index",
    },
    "library-data": {
        "title": "Back up or restore the app's library data",
        "args": {"action": "status|backup|import|preview|pause|resume",
                 "file": "path to a library-data-….jsonl export"},
        "handler": "skill_library_data",
    },
    "settings": {
        "title": "Show or change an app setting",
        "args": {"action": "list|show|set|toggle|reset",
                 "key": "setting key, e.g. visualizer or llm_provider",
                 "value": "the new value",
                 "group": "list only this group"},
        "handler": "skill_settings",
    },
}

ORDER = ["make-playlist", "recommend", "play-music", "search-library",
         "rank-library", "lyrics", "show-playlists", "save-playlist",
         "edit-playlist", "radio", "endless", "transport", "activity",
         "psycho-profile",
         "recent", "duplicates", "index-library", "library-data",
         "import-audio",
         "import-playlist", "add-file", "learn-preferences", "settings",
         "agent", "library-overview"]


@dataclass
class Skill:
    name: str
    title: str
    description: str = ""
    triggers: list[str] = field(default_factory=list)
    args: dict[str, str] = field(default_factory=dict)
    body: str = ""
    handler: str = ""
    example: str = ""

    def prompt(self, with_body: bool = False) -> str:
        args = ", ".join(f"{key}: {value}" for key, value in self.args.items())
        text = f"- {self.name}: {self.description or self.title}"
        if args:
            text += f"\n  arguments: {args}"
        if with_body and self.body:
            text += "\n" + "\n".join("  " + line
                                     for line in self.body.splitlines())
        return text


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head = text[3:end].strip().splitlines()
    body = text[end + 4:].lstrip("\n")
    meta: dict[str, str] = {}
    for line in head:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return meta, body


def load() -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for name, info in REGISTRY.items():
        path = SKILL_DIR / name / "SKILL.md"
        meta: dict[str, str] = {}
        body = ""
        try:
            meta, body = _frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            pass
        triggers = [part.strip() for part in
                    (meta.get("triggers") or "").split(",") if part.strip()]
        skills[name] = Skill(
            name=name,
            title=info["title"],
            description=meta.get("description", ""),
            triggers=triggers,
            args=info["args"],
            body=body.strip(),
            handler=info["handler"],
            example=meta.get("example", "").strip(),
        )
    return skills


def skills_prompt() -> str:
    catalog = load()
    return "\n".join(catalog[name].prompt() for name in ORDER)


def help_markdown() -> str:
    catalog = load()
    lines = ["## Skills", ""]
    for name in ORDER:
        skill = catalog[name]
        lines.append(f"**{skill.title}** · `{name}`")
        lines.append(skill.description or "")
        if skill.triggers:
            lines.append("*say:* " + ", ".join(f"“{t}”" for t in skill.triggers[:8]))
        lines.append("")
    lines.append("Tip: `/` shows every command, `Ctrl+P` jumps to the prompt, "
                 "`Ctrl+L` clears the conversation.")
    return "\n".join(lines)


def examples() -> list[str]:
    return [
        "what do you recommend for aggressive music?",
        "create a smooth playlist for work",
        "show my playlists",
        "save that playlist as Late Night",
        "what did I listen to today?",
        "play the whole library shuffled",
        "find everything by Die Ärzte",
        "make me a two-hour party playlist",
        "I like this — remember it",
        "when I say sunday I mean chill and jazz",
        "what is playing?",
    ]
