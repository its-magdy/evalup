---
name: help
description: >-
  Explain the evalup plugin and point the user at the one command or
  document that answers them: what evalup is and what it measures, how the
  workflow runs from first contact to steady state, where to start or resume,
  which of the commands fits the situation they describe, what the modes
  and flags mean, who has to be involved (developer, reviewer/QA, domain
  arbiter) and whether they need Claude Code, where eval state lives, what the
  scores can and cannot claim, and why something is locked, gated, refused, or
  watermarked PROVISIONAL. Use for any "what is / how do I / where do I start /
  which command / what does this mean / who does / why is this locked" question
  about evalup, rather than a request to do eval work.
allowed-tools: >-
  Read Grep Glob Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/*) Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*)
---

# evalup — Help

> **Plugin root:** `${CLAUDE_PLUGIN_ROOT}`. Reference files and docs write that
> placeholder literally (it is only substituted here), so read every
> `${CLAUDE_PLUGIN_ROOT}/…` path you meet in them as this absolute path, and
> quote it in shell commands.

You are a router, not an explainer. Match the question to a row, read the
document that row names, and answer out of that document. This file holds no
explanations on purpose, so anything you would "recall" from it is a guess.

| The user is asking | Read |
|---|---|
| What is this? What does it measure? What are the principles? | `${CLAUDE_PLUGIN_ROOT}/README.md` |
| Which command do I want? What does `--smoke` / `--holdout` mean? | `${CLAUDE_PLUGIN_ROOT}/README.md` §Commands |
| Where do I start? Where was I? I'm lost. | Load the `start` skill and follow it — it reads the state location and routes them. |
| How does this go — day 1, steady state, after a refactor, production? | `${CLAUDE_PLUGIN_ROOT}/docs/workflow.md` |
| Who has to be involved? Does QA need Claude Code? | `${CLAUDE_PLUGIN_ROOT}/docs/workflow.md` §Who does what |
| What does a layer / stage / route target / the judge / a pass^k number mean? | `${CLAUDE_PLUGIN_ROOT}/docs/concepts.md` |
| Why is this locked? Why is my number PROVISIONAL? | `${CLAUDE_PLUGIN_ROOT}/docs/concepts.md` §Staged rigor and §The judge |
| Why won't `optimize` run for me? | `${CLAUDE_PLUGIN_ROOT}/skills/optimize/SKILL.md` §Preconditions |
| What can these numbers *not* claim? | `${CLAUDE_PLUGIN_ROOT}/docs/concepts.md` §What this can never do |
| Where does my eval state live? What is in it? | `${CLAUDE_PLUGIN_ROOT}/README.md` §Where state lives |
| What must my app provide to be evaluated? | `${CLAUDE_PLUGIN_ROOT}/skills/discover/references/adapter-contract.md` |
| Why did a run refuse a diff, or halt on my state directory? | `${CLAUDE_PLUGIN_ROOT}/skills/run/SKILL.md` §4 |
| I already have real chats / logs — can I just see what is broken? | `${CLAUDE_PLUGIN_ROOT}/skills/analyze/SKILL.md` §`--transcripts` — no profile, cases or run needed. |
| Did my run pass? How do I gate CI on it? | `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/gate.py <reports/run-id>` (exit 0 open / 1 closed); `${CLAUDE_PLUGIN_ROOT}/skills/run/references/run-modes.md` "Headless/CI gate" |
| What does a real dataset / adapter / run directory look like? Show me an example. | `${CLAUDE_PLUGIN_ROOT}/examples/quickstart/README.md` |

Rules:

- Read only the rows the question needs — most questions are one row.
- Answer in a few sentences, then name the single next command.
- If the question is really a request to *do* eval work, stop routing and load
  the skill that does it (`start` when the right one is unclear). The one
  exception is `optimize`: it is user-invoked only, so tell them to type
  `/evalup:optimize` rather than loading it.
