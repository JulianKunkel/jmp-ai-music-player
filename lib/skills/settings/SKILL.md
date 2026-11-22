---
name: settings
description: Show or change any setting of the app — the music folder, the visualizer, voice input, or the language model server, model and key.
example: use the gwdg server with my api key
triggers: settings, einstellungen, options, optionen, preferences, konfiguration, turn on, turn off, schalte ein, schalte aus, enable, disable, set the, setze, change the, reset the, music folder, musikordner, visualizer, autoplay, voice input, spracheingabe, profile set, ollama, gwdg, saia, api key, model
---
# Settings

Every switch in the app is one row of `lib/chat/settings.py`, and this skill is
the only writer.  The settings window (`GET/POST /api/settings`) and the
language model's own tools (`list_settings`, `change_setting`) use the same
table, so a value can never be validated in one place and stored in another.

## What the skill answers

- **list** — the whole table, grouped (Library, Chat, Playback, Sound profile,
  Lyrics, Voice, Language model), each line with its current value.
  "show the settings", "einstellungen", "which settings are there".
  "voice settings" lists only that group.
- **show** — one value: "is the visualizer on?", "which model is used",
  "where is my music folder".
- **set** — "turn off the visualizer", "set the default count to 60",
  "set the music folder to /home/you/Musik", "use the timbre profile set",
  "use german for the voice language", "schalte den visualizer aus".
- **toggle** — "toggle autoplay", "switch the lyrics panel".
- **reset** — "reset the visualizer", "set the profile lens back to default".

## Voice

`voice_enabled` shows the microphone button, `voice_autosend` runs a finished
recording at once (off: it lands in the text box first), `voice_language` is
`auto`/`de`/`en`, and `voice_model` is `auto`/`small`/`medium`.  The
transcription itself is local (see the `lyrics` skill); no audio is uploaded.

## Language model

Three ways to reach a model, all OpenAI-compatible:

| provider | address |
|---|---|
| `ollama` | `http://127.0.0.1:11434/v1` (any key, a local server) |
| `gwdg` | `https://chat-ai.academiccloud.de/v1` (GWDG / SAIA) |
| `custom` | whatever `llm_base_url` says |

- `llm_provider` chooses one; `auto` keeps an installed configuration
  (`MUSIC_LLM_CONFIG`, `library-work/app/llm.json`, environment).
- `llm_base_url` overrides the address (an explicit address its own provider is
  `custom` unless it is exactly a preset).
- `llm_model` names the model; empty takes the first the server lists.
- `llm_api_key` is stored **only** in `library-work/app/chat/secrets.json` with
  mode 0600 — never in `profile.json`, never echoed in a reply, never logged.
  Setting `gwdg` without a key is allowed; the reply then asks for one.

Examples: "use ollama", "use the gwdg server", "set the api key to …",
"use `qwen2.5:7b` as the model", "which server is used", "disable the language
model".

The offline parser stays the default planner: turning the model off leaves
every skill working without a network.

## The music folder

`library` is the one service-level setting.  The engine only validates the
path and defers the switch; the web service stops the player, builds a fresh
engine and moves the catalogue.  A folder the process can see is opened at
once; one it cannot see (the usual case in the container, where the host path
does not exist) is saved in `library-work/app/chat/settings.json` for the next
start, because the launcher reads that file and mounts what it names.  A path
that is not a music library root — or not a folder at all — is refused with
that reason.

## Safety

No setting writes a tag, moves a file or deletes music.  State lives under
`library-work/app/chat/` (`profile.json`, `secrets.json`).  Unknown keys and
invalid values are refused with the allowed range, so a typo cannot store a
broken value.
