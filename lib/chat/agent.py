"""The skill agent: keep what the planner did not understand, learn safely.

The offline planner in ``nlu.py`` and the optional language model both answer
with one of the *known* skills.  When neither can, this module writes the
request down instead of discarding it:

    library-work/app/chat/agent/requests.jsonl

With a language model configured it also drafts a **proposal**: which existing
skill should have handled the request, with which validated arguments, and
which phrases should reach it next time.  The proposal is app state, not code.

``teach that`` approves the last proposal and stores it as a learned skill in

    library-work/app/chat/agent/skills/<name>.json

The conversation engine consults those files between the offline planner and
the language model, so the next occurrence is recognised.  Nothing here edits
a source file or runs generated code — a skill that would need new behaviour is
reported as a request for the coding agent instead, not executed.

Every write is fsync'd before the file is replaced, and no music file is ever
touched.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .catalog import fold
from .skills import REGISTRY

#: skills the agent may not learn: it would recurse into itself
BLOCKED = {"agent", "answer"}

#: a learned trigger must be at least this long, so "it" matches nothing
MIN_TRIGGER = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _append_jsonl(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _clean_text(text: str, limit: int = 80) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit]


class Agent:
    def __init__(self, root: Path, llm=None):
        self.root = Path(root)
        self.base = self.root / "library-work" / "app" / "chat" / "agent"
        self.requests = self.base / "requests.jsonl"
        self.last_path = self.base / "last-proposal.json"
        self.skills_dir = self.base / "skills"
        self.llm = llm

    # -- reading -----------------------------------------------------------

    def count(self) -> int:
        try:
            return sum(1 for line in self.requests.read_bytes().split(b"\n")
                       if line.strip())
        except OSError:
            return 0

    def last(self) -> dict:
        return _read_json(self.last_path)

    def learned(self) -> list[dict]:
        found: list[dict] = []
        if not self.skills_dir.is_dir():
            return found
        for path in sorted(self.skills_dir.glob("*.json")):
            data = _read_json(path)
            if data.get("skill") in REGISTRY and data["skill"] not in BLOCKED:
                found.append(data)
        return found

    # -- planning ----------------------------------------------------------

    def plan(self, text: str):
        """A plan from a learned skill, or None.  Longest trigger wins."""
        from .nlu import Plan

        folded = " ".join(fold(text).split())
        if not folded:
            return None
        best: tuple[int, dict] | None = None
        for data in self.learned():
            for trigger in data.get("triggers") or []:
                key = " ".join(fold(trigger).split())
                if len(key) < MIN_TRIGGER:
                    continue
                if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", folded):
                    if best is None or len(key) > best[0]:
                        best = (len(key), data)
                    break
        if best is None:
            return None
        data = best[1]
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        return Plan(str(data["skill"]), dict(args), confidence=0.72,
                    source="learned")

    # -- proposing ---------------------------------------------------------

    def propose(self, text: str) -> dict:
        """Record an unrecognised request and draft a learned skill."""
        proposal = {
            "id": secrets.token_hex(4),
            "at": _now(),
            "request": _clean_text(text, 300),
            "skill": "",
            "args": {},
            "name": "",
            "description": "",
            "triggers": [],
            "source": "recorded",
        }
        guessed = self._ask_llm(text)
        if guessed:
            proposal.update(guessed)
            proposal["source"] = "llm"
        _append_jsonl(self.requests, proposal)
        _write_json(self.last_path, proposal)
        return proposal

    def _ask_llm(self, text: str) -> dict | None:
        """Ask the optional model for a *mapping onto an existing skill*.

        The answer is validated exactly like a planner answer: an unknown skill
        is ignored, arguments are filtered through the registry, and no path or
        command is accepted.
        """
        if self.llm is None or not getattr(self.llm, "available", lambda: False)():
            return None
        from .skills import skills_prompt

        skills = ", ".join(name for name in REGISTRY if name not in BLOCKED)
        system = (
            "You map an unrecognised music-app request onto one existing "
            "skill. Return ONE JSON object, nothing else:\n"
            '{"skill": "<one of the skills>", "args": {...}, '
            '"name": "<short skill title>", "description": "<one sentence>", '
            '"triggers": ["2-4 short phrases that should match later"]}\n\n'
            "Rules:\n"
            "- Choose only from the listed skill names; never invent one.\n"
            "- Arguments must belong to that skill; never invent a library "
            "path or a shell command.\n"
            "- Triggers are short imperative phrases, not the whole request.\n"
            "- If no skill fits, return {\"skill\": \"\"}.\n\n"
            f"Skills:\n{skills_prompt()}\n\n"
            f"Available names: {skills}."
        )
        payload = {
            "model": self.llm.config.get("model"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text[:600]},
            ],
            "temperature": 0.1,
            "max_tokens": 500,
        }
        try:
            data = self.llm._post(payload)
        except Exception:
            return None
        parsed = self.llm._parse(self.llm._content(data))
        if not isinstance(parsed, dict):
            return None
        skill = str(parsed.get("skill") or "").strip()
        if skill not in REGISTRY or skill in BLOCKED:
            return None
        raw_args = parsed.get("args")
        args = self.llm._clean_args(skill, raw_args) \
            if isinstance(raw_args, dict) else {}
        triggers = []
        for item in (parsed.get("triggers") or [])[:4]:
            phrase = _clean_text(item, 60)
            if len(fold(phrase)) >= MIN_TRIGGER:
                triggers.append(phrase)
        name = _clean_text(parsed.get("name") or skill, 40) or skill
        return {
            "skill": skill,
            "args": args,
            "name": name,
            "description": _clean_text(parsed.get("description") or "", 160),
            "triggers": triggers,
        }

    # -- approving ---------------------------------------------------------

    def approve(self) -> tuple[bool, str]:
        proposal = self.last()
        if not proposal:
            return False, ("There is no proposal to teach. Ask for something "
                           "I did not understand first.")
        if proposal.get("approved"):
            return False, "That proposal is already learned."
        skill = str(proposal.get("skill") or "")
        if skill not in REGISTRY or skill in BLOCKED:
            return False, (
                "I recorded that request, but without a language model I "
                "cannot guess which skill should handle it. Teach a phrase "
                "instead: “when I say <phrase> I mean chill and jazz”.")
        triggers = [str(t) for t in (proposal.get("triggers") or [])
                    if len(fold(str(t))) >= MIN_TRIGGER]
        if not triggers:
            triggers = [str(proposal.get("request") or "")[:60]]
        record = {
            "name": proposal.get("name") or skill,
            "description": proposal.get("description") or "",
            "triggers": triggers,
            "skill": skill,
            "args": proposal.get("args") or {},
            "created": _now(),
            "source_request": proposal.get("request") or "",
        }
        slug = re.sub(r"[^a-z0-9]+", "-", fold(record["name"])).strip("-")
        slug = (slug or skill)[:50]
        base = self.skills_dir / f"{slug}.json"
        path = base
        for number in range(2, 100):
            if not path.exists():
                break
            path = self.skills_dir / f"{slug}-{number}.json"
        _write_json(path, record)
        proposal["approved"] = True
        proposal["learned_as"] = path.stem
        _write_json(self.last_path, proposal)
        phrases = ", ".join(f"“{t}”" for t in triggers)
        return True, (f"Learned **{record['name']}** → `{skill}` for {phrases}. "
                      "Say it again and I will handle it.")

    def reject(self) -> tuple[bool, str]:
        proposal = self.last()
        if not proposal:
            return False, "There is no proposal to discard."
        proposal["rejected"] = True
        _write_json(self.last_path, proposal)
        return True, "Discarded the last proposal."

    # -- rendering ---------------------------------------------------------

    def summary_markdown(self) -> str:
        lines = [f"**Skill agent** · {self.count():,} recorded request"
                 f"{'s' if self.count() != 1 else ''} · "
                 f"{len(self.learned())} learned skill"
                 f"{'s' if len(self.learned()) != 1 else ''}"]
        learned = self.learned()
        if learned:
            lines.append("")
            lines.append("**Learned**")
            for data in learned:
                phrases = ", ".join(f"“{t}”" for t in
                                    (data.get("triggers") or [])[:4])
                lines.append(f"- {data.get('name') or data.get('skill')} → "
                             f"`{data.get('skill')}` · {phrases}")
        proposal = self.last()
        if proposal and not proposal.get("approved") \
                and not proposal.get("rejected"):
            lines.append("")
            lines.append("**Waiting for approval**")
            lines.append(f"- “{proposal.get('request', '')}” → "
                         f"`{proposal.get('skill') or 'unclear'}`")
            if proposal.get("triggers"):
                lines.append("  triggers: " + ", ".join(
                    f"“{t}”" for t in proposal["triggers"]))
            lines.append("  Say “teach that” to keep it, or “discard that”.")
        lines.append("")
        lines.append(f"Requests: `{self.requests.relative_to(self.root)}`")
        return "\n".join(lines)
