---
name: agent
description: Review skill requests the app could not recognise, and teach a learned phrase.
example: teach that
triggers: teach that, add that skill, learn that skill, skill requests, what did you not understand, agent status, übernimm das, lerne das
---
# Skill agent

When the offline planner and the optional language model both fail, the request
is not thrown away.  The agent writes it to
`library-work/app/chat/agent/requests.jsonl` (fsync'd) with

- the request text and the time,
- the closest existing skill and the arguments the model suggested,
- trigger phrases that should reach it next time.

With a language model configured, the proposal is concrete: it names one of the
existing skills plus validated arguments and 2–4 trigger phrases.  Without a
model, the request is recorded and the answer asks the listener to rephrase or
teach a phrase.

`teach that` (or "add that skill" / "übernimm das") **approves** the last
proposal.  Approval writes a small learned-skill file under
`library-work/app/chat/agent/skills/` — data, not code — and from then on those
phrases are planned before the fallback.  Nothing is executed automatically and
no source file is edited by the chat; `show skills requests` lists what is
waiting.  `reject` discards the last proposal.

Arguments: `action` = `show` | `approve` | `reject`.
