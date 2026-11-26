"""The plain-text chat — for a console without a full-screen terminal.

    music chat --plain
    music chat --plain "create a smooth playlist for work"

It runs the same ``Engine`` as the Textual app, so nothing behaves differently;
only the presentation is simpler.  Markdown is printed as-is (readable in a
terminal), and the implicit learning still runs because the player is polled
before every prompt.
"""

from __future__ import annotations

from pathlib import Path

from .engine import Engine
from .version import app_version


def _markdown_to_text(text: str) -> str:
    """Keep the message readable when ANSI is not available."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            continue
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        stripped = (stripped.replace("**", "").replace("`", "")
                    .replace("\\[", "[").replace("\\]", "]"))
        out.append(stripped)
    return "\n".join(out)


def run(root: Path | None = None, use_llm: bool = True, message: str | None = None,
        dry_run: bool = False) -> int:
    engine = Engine(root=root, use_llm=use_llm, dry_run=dry_run)

    if message:
        reply = engine.handle(message)
        print(_markdown_to_text(reply.text))
        if reply.planner:
            print(f"[{reply.planner}]")
        return 1 if reply.error else 0

    print(f"JulAIn Music Player {app_version()} — chat")
    print("-" * 26)
    print("Type a request, /skills for the list, /quit to leave.\n")

    while True:
        try:
            engine.poll()
            line = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return 0
        if not line:
            continue
        reply = engine.handle(line)
        if reply.clear:
            print("\033[2J\033[H", end="")
            continue
        if reply.text:
            print("\n" + _markdown_to_text(reply.text)
                  + (f"\n[{reply.planner}]" if reply.planner else "") + "\n")
        if reply.quit:
            print("bye")
            return 0
