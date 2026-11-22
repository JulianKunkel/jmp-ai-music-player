"""GNOME global media-key bindings, as data — host side only.

The app server runs inside a container with no session bus, so it cannot read
or write dconf.  The *window* runs on the host, and this module is what it
calls for the Settings section and for the one-time notice: it reads the GNOME
custom keybindings with ``gsettings``, installs the four keys of a function-key
media row, and removes them again.

It has no third-party imports on purpose — ``window.py`` loads it by path with
the host's own ``python3``, next to the installed copy in ``lib/chat/``.

Nothing here touches a music file; a key press runs one transport command.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM = SCHEMA + ".custom-keybinding"
ROOT = ("/org/gnome/settings-daemon/plugins/media-keys/"
        "custom-keybindings")
#: the row as printed on the JMP machine: stop, rewind, play/pause, forward
BINDINGS = (("F5", "stop"), ("F6", "prev"), ("F7", "toggle"), ("F8", "next"))
#: how an entry this module owns is recognised again
PREFIX = "JMP "
#: slots it prefers; a used one is skipped instead of overwritten
FIRST_SLOT = 90
SLOT_COUNT = 40


def _binding(key: str) -> str:
    """The GTK accelerator for an unmodified key.

    The angle brackets in an accelerator string introduce a *modifier*
    (``<Control>``), so ``<F5>`` is not a valid accelerator and GNOME ignores
    it.  A bare ``F5`` is the key name and works globally.
    """
    return str(key).strip()


def _binding_forms(key: str) -> set[str]:
    """The valid spelling and the old, broken ``<F5>`` spelling."""
    key = _binding(key)
    return {key, f"<{key}>"}


def _run(args: list[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return done.returncode, done.stdout.strip(), done.stderr.strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, "", str(error)


def _bus_ok() -> bool:
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    runtime = (os.environ.get("XDG_RUNTIME_DIR")
               or f"/run/user/{os.getuid()}")
    return os.path.exists(os.path.join(runtime, "bus"))


def available() -> bool:
    """True when this host can read and write GNOME keybindings."""
    return bool(shutil.which("gsettings")) and _bus_ok()


def default_cli() -> str:
    """The absolute music-cli path a binding should call."""
    found = shutil.which("music-cli")
    if found:
        return found
    for candidate in (os.path.expanduser("~/.local/bin/music-cli"),
                      "/usr/local/bin/music-cli", "/usr/bin/music-cli"):
        if os.access(candidate, os.X_OK):
            return candidate
    return ""


def _unpack(text: str) -> list[str]:
    text = (text or "").strip()
    if text.startswith("@as "):
        text = text[4:].strip()
    try:
        values = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return []
    return [str(v) for v in values] if isinstance(values, (list, tuple)) else []


def read_list() -> list[str]:
    code, out, _err = _run(["gsettings", "get", SCHEMA, "custom-keybindings"])
    return _unpack(out) if code == 0 else []


def read_entry(path: str) -> dict:
    entry = {"path": path}
    for key in ("name", "binding", "command"):
        code, out, _err = _run(["gsettings", "get", f"{CUSTOM}:{path}", key])
        value = _unpack(out)
        if code == 0 and isinstance(value, list) and value:
            entry[key] = value[0]
        else:
            entry[key] = out.strip().strip("'\"") if code == 0 else ""
    return entry


def _ours(entry: dict) -> bool:
    name = str(entry.get("name") or "")
    command = str(entry.get("command") or "")
    return name.startswith(PREFIX.strip()) or (
        name.upper().startswith("JMP") and "music-cli" in command)


def _write_list(paths: list[str]) -> None:
    text = "[" + ", ".join("'" + p.replace("'", "") + "'" for p in paths) + "]"
    _run(["gsettings", "set", SCHEMA, "custom-keybindings", text])


def _set(path: str, name: str, binding: str, command: str) -> None:
    _run(["gsettings", "set", f"{CUSTOM}:{path}", "name", name])
    _run(["gsettings", "set", f"{CUSTOM}:{path}", "binding", binding])
    _run(["gsettings", "set", f"{CUSTOM}:{path}", "command", command])


def _reset(path: str) -> None:
    for key in ("name", "binding", "command"):
        _run(["gsettings", "reset", f"{CUSTOM}:{path}", key])


def _pairs(bindings=None) -> list[tuple[str, str]]:
    wanted = []
    for item in (bindings or BINDINGS):
        if isinstance(item, (list, tuple)) and len(item) == 2:
            wanted.append((str(item[0]).strip(), str(item[1]).strip()))
    return [p for p in wanted if p[0] and p[1]]


def status(bindings=None, cli: str = "") -> dict:
    """What is installed, per key, plus the entries that are not ours."""
    pairs = _pairs(bindings)
    cli = cli or default_cli()
    can = available()
    listed = read_list() if can else []
    entries = {path: read_entry(path) for path in listed}
    rows = []
    for key, action in pairs:
        binding = _binding(key)
        owner = ""
        old_owner = ""
        works = False
        for path, entry in entries.items():
            value = str(entry.get("binding") or "")
            if value in _binding_forms(key) and _ours(entry):
                if value == binding:
                    owner, works = path, True
                    break
                old_owner = old_owner or path
        owner = owner or old_owner
        rows.append({
            "key": key,
            "action": action,
            "binding": binding,
            "installed": works,
            "path": owner,
            "command": str(entries.get(owner, {}).get("command") or
                           (f"{cli} {action}" if cli else f"music-cli {action}")),
            "name": str(entries.get(owner, {}).get("name") or f"{PREFIX}{action}"),
        })
    installed = bool(rows) and all(row["installed"] for row in rows)
    # another binding that already grabs one of these keys would fight ours
    conflicts = []
    for path, entry in entries.items():
        if _ours(entry):
            continue
        if str(entry.get("binding") or "") in {row["binding"] for row in rows}:
            conflicts.append({"path": path, "name": entry.get("name", ""),
                              "binding": entry.get("binding", "")})
    return {
        "available": can,
        "installed": installed,
        "rows": rows,
        "bindings": [f"{key}={action}" for key, action in pairs],
        "cli": cli,
        "entries": listed,
        "others": [path for path, entry in entries.items() if not _ours(entry)],
        "conflicts": conflicts,
        "exclusive": True,
        "error": "" if can else (
            "This window cannot change desktop shortcuts: gsettings or the "
            "session bus is not reachable."),
    }


def _free_slots(listed: list[str], pairs) -> list[str]:
    """One unused custom slot per key, so nothing is overwritten."""
    taken = set(listed)
    free = []
    for number in range(FIRST_SLOT, FIRST_SLOT + SLOT_COUNT):
        path = f"{ROOT}/custom{number}/"
        if path not in taken:
            free.append(path)
    result = []
    for _key, _action in pairs:
        result.append(free.pop(0) if free else "")
    return result


def install(bindings=None, cli: str = "") -> dict:
    """Add the four bindings, keeping every unrelated one."""
    pairs = _pairs(bindings)
    cli = cli or default_cli()
    if not available():
        answer = status(pairs, cli)
        answer["ok"] = False
        return answer
    if not cli:
        answer = status(pairs, cli)
        answer["ok"] = False
        answer["error"] = ("music-cli was not found, so a key could not call "
                           "the player.")
        return answer
    listed = read_list()
    entries = {path: read_entry(path) for path in listed}
    result: list[str] = list(listed)
    spare = _free_slots(listed, pairs)
    chosen: set[str] = set()
    for index, (key, action) in enumerate(pairs):
        owner = ""
        for path, entry in entries.items():
            if (str(entry.get("binding") or "") in _binding_forms(key)
                    and _ours(entry)):
                owner = path
                break
        path = owner or spare[index]
        if not path:
            continue
        chosen.add(path)
        if path not in result:
            result.append(path)
        _set(path, f"{PREFIX}{action}", _binding(key), f"{cli} {action}")
    # Drop our own entries that no longer belong: a changed key set, or an
    # old entry that duplicated a repaired one.
    for path, entry in entries.items():
        if _ours(entry) and path not in chosen:
            result = [item for item in result if item != path]
            _reset(path)
    _write_list(result)
    answer = status(pairs, cli)
    answer["ok"] = bool(answer.get("installed"))
    return answer


def remove(bindings=None, cli: str = "") -> dict:
    """Remove only the bindings this module owns."""
    pairs = _pairs(bindings)
    cli = cli or default_cli()
    if not available():
        answer = status(pairs, cli)
        answer["ok"] = False
        return answer
    listed = read_list()
    entries = {path: read_entry(path) for path in listed}
    keep = [path for path in listed if not _ours(entries.get(path, {}))]
    for path in listed:
        if _ours(entries.get(path, {})):
            _reset(path)
    _write_list(keep)
    answer = status(pairs, cli)
    answer["ok"] = True
    return answer


def describe(state: dict) -> str:
    """One line for a log or a chat answer."""
    state = state or {}
    if not state.get("available"):
        return str(state.get("error") or "Global media keys are not reachable.")
    if state.get("installed"):
        parts = [f"{row['key']} {row['action']}" for row in state.get("rows", [])]
        return "Global media keys installed: " + ", ".join(parts)
    return ("Global media keys are not installed; the keys work only while "
            "the window has focus.")
