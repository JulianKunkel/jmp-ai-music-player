"""The Textual chat UI — the OpenCode-style front end.

One scrollable conversation, a prompt at the bottom, a live player status line
and a collapsible skill drawer.  All behaviour lives in ``engine.py``; this
module only presents it, which is what makes the headless test possible.

    music chat              # this app
    music chat --plain      # the line-based fallback
    music chat "play Bands" # one turn, print, exit
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Header, Input, Markdown, Static

from .engine import Engine, Reply
from .skills import ORDER, examples, load


class UserMessage(Static):
    """The listener's line, visually distinct from the answer."""

    def __init__(self, text: str) -> None:
        super().__init__(Text(text, style="bold"), classes="user")
        self.add_class("message")


class AssistantMessage(Vertical):
    """One answer, rendered as Markdown."""

    def __init__(self, markdown: str, *, error: bool = False) -> None:
        super().__init__(classes="assistant")
        self.add_class("message")
        if error:
            self.add_class("error")
        self._markdown = markdown

    def compose(self) -> ComposeResult:
        yield Markdown(self._markdown, open_links=False)


class ChatApp(App):
    CSS = """
    Screen { layout: vertical; }

    #body { height: 1fr; }

    #transcript {
        width: 1fr;
        padding: 1 2;
        scrollbar-size-vertical: 1;
    }

    #skills {
        width: 34;
        padding: 1;
        border-left: solid $panel;
        background: $surface;
    }

    .message { margin-bottom: 1; }

    .user {
        background: $boost;
        padding: 0 1;
        width: auto;
    }

    .assistant { padding: 0 1; width: 1fr; }
    .assistant.error { border-left: thick $error; }

    #thinking {
        color: $text-muted;
        padding: 0 2;
    }

    #prompt {
        dock: bottom;
        margin: 0 1 1 1;
    }

    #status {
        height: 1;
        color: $text-muted;
        padding: 0 2;
        background: $panel;
    }

    .hidden { display: none; }

    .skill-name { color: $accent; text-style: bold; }
    .skill-desc { color: $text-muted; margin-bottom: 1; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+l", "clear", "Clear"),
        Binding("ctrl+p", "focus_prompt", "Prompt"),
        Binding("f1", "help", "Help"),
        Binding("f2", "toggle_skills", "Skills"),
        Binding("f3", "toggle_play", "Pause/Play"),
    ]

    def __init__(self, root: Path | None = None, use_llm: bool = True,
                 dry_run: bool = False, engine: Engine | None = None,
                 welcome: bool = True) -> None:
        super().__init__()
        self.engine = engine or Engine(root=root, use_llm=use_llm,
                                       dry_run=dry_run)
        self.welcome = welcome
        self.title = "JulAIn Music Player"
        self.sub_title = "chat"
        self._busy = False

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            yield VerticalScroll(id="transcript")
            yield VerticalScroll(id="skills", classes="hidden")
        yield Static("", id="status")
        yield Input(placeholder="Ask for music, or /skills …", id="prompt",
                    suggester=SuggestFromList(
                        examples() + [f"/{name}" for name in ORDER],
                        case_sensitive=False))
        yield Footer()

    def on_mount(self) -> None:
        self._fill_skills()
        if self.welcome:
            self._add_assistant(self.engine.welcome())
        self.refresh_status()
        self.set_interval(2.0, self.refresh_status)
        self.query_one("#prompt", Input).focus()

    # -- messages ---------------------------------------------------------

    def _transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _append(self, widget) -> None:
        transcript = self._transcript()
        transcript.mount(widget)
        self.call_after_refresh(transcript.scroll_end, animate=False)

    def _add_user(self, text: str) -> None:
        self._append(UserMessage(text))

    def _add_assistant(self, text: str, error: bool = False) -> None:
        self._append(AssistantMessage(text, error=error))

    def _fill_skills(self) -> None:
        panel = self.query_one("#skills", VerticalScroll)
        catalog = load()
        blocks = [Static("Skills", classes="skill-name")]
        for name in ORDER:
            skill = catalog[name]
            blocks.append(Static(skill.title, classes="skill-name"))
            blocks.append(Static(skill.description or "", classes="skill-desc"))
        panel.mount(*blocks)

    # -- the turn ---------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.run_turn(text)

    @work(thread=True, exclusive=True, group="turn")
    def run_turn(self, text: str) -> None:
        """Handle one message off the UI thread, then show the reply."""
        self._busy = True
        self.call_from_thread(self._add_user, text)
        self.call_from_thread(self._show_thinking, True)
        try:
            reply = self.engine.handle(text)
        except Exception as exc:  # the UI must survive any engine bug
            reply = Reply(f"`{type(exc).__name__}: {exc}`", error=True)
        self.call_from_thread(self._show_thinking, False)
        self.call_from_thread(self._apply_reply, reply)
        self._busy = False

    def _show_thinking(self, visible: bool) -> None:
        existing = self.query("#thinking")
        if visible and not existing:
            self._append(Static("▌ thinking…", id="thinking"))
        elif not visible:
            for widget in existing:
                widget.remove()

    def _apply_reply(self, reply: Reply) -> None:
        if reply.clear:
            self.action_clear()
            return
        if reply.text:
            text = reply.text
            if reply.planner:
                text += f"\n\n*{reply.planner}*"
            self._add_assistant(text, error=reply.error)
        if reply.quit:
            self.exit()
            return
        self.refresh_status()

    # -- actions ----------------------------------------------------------

    def action_clear(self) -> None:
        transcript = self._transcript()
        for child in list(transcript.children):
            child.remove()
        if self.welcome:
            self._add_assistant(self.engine.welcome())

    def action_focus_prompt(self) -> None:
        self.query_one("#prompt", Input).focus()

    def action_help(self) -> None:
        reply = self.engine.handle("/help")
        self._apply_reply(reply)

    def action_toggle_skills(self) -> None:
        panel = self.query_one("#skills", VerticalScroll)
        panel.toggle_class("hidden")

    def action_toggle_play(self) -> None:
        reply = self.engine.handle("/status")
        self._apply_reply(reply)

    # -- live status ------------------------------------------------------

    def refresh_status(self) -> None:
        line = self.engine.player.status_line()
        self.query_one("#status", Static).update(line)

    async def on_unmount(self) -> None:
        try:
            self.engine.close()
        except Exception:
            pass


def run(root: Path | None = None, use_llm: bool = True,
        dry_run: bool = False) -> int:
    app = ChatApp(root=root, use_llm=use_llm, dry_run=dry_run)
    app.run()
    return 0
