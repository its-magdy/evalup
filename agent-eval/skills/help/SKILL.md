---
name: help
description: >-
  Explain the agent-eval plugin: what it is, which command to use next, how the
  workflow goes, and who does what (developer, QA, domain expert). Use when the
  user asks what agent-eval is, how to use it, where to start, which command
  fits their situation, or why a feature is locked.
---

# agent-eval — Help

You are helping a user understand and navigate the agent-eval plugin. Read
their question, then answer from the material below. Keep answers short and
point at the single next action. Load `${CLAUDE_PLUGIN_ROOT}/docs/concepts.md`,
`${CLAUDE_PLUGIN_ROOT}/docs/workflow.md`, or
`${CLAUDE_PLUGIN_ROOT}/skills/discover/references/adapter-contract.md`
only if the question needs that depth.

## What this plugin is

agent-eval evaluates and improves LLM chat/agent applications. It works on any
app it can invoke programmatically — simple chat, router→executor,
multi-agent — with capability graded by access (code, traces, or endpoint
only). Claude Code does the intelligent work (profiling the app, generating
test cases, diagnosing failures, proposing fixes); deterministic Python
scripts do the scoring so numbers are cheap and reproducible.

Vocabulary: a **route target** is whatever the app dispatches between before
acting — a *domain* (router→executor), a *node* (graph), or a *sub-agent*
(orchestrator). A single-LLM app has no route targets, so the routing layer
simply does not apply to it; everything else still does.

## Command map — "which command do I want?"

| Situation | Command |
|---|---|
| New here / not sure | `/agent-eval:start` — the wizard. Always safe; it detects state and routes you. |
| Point it at an app for the first time | `/agent-eval:discover <path-or-url>` |
| Need (more) test cases | `/agent-eval:generate` |
| Score the app now / before a merge | `/agent-eval:run` — modes: `--smoke` (fast subset, every edit), `--regression`/default (full, k≥3, pass^k, pre-merge), `--targeted` (rerun only what a change touched), `--holdout` (the optimizer's sealed decision set), `--full` (everything incl. judged, release) |
| Understand failures, label cases, mine traces | `/agent-eval:analyze` — clusters failures, drives judge calibration, and builds a hotkey-driven trace/annotation viewer for open→axial error analysis |
| Improve prompts / tool descriptions | `/agent-eval:optimize` (locked until preconditions are met — see below) |

## The workflow in one paragraph

`start` → `discover` profiles the app, patches missing instrumentation, and
writes `profile.yaml` + `adapter.yaml` to the state location (default
`.agent-eval/` in the app's repo; any external path works) →
`generate` creates ~30 cases and asks you to review only the suspicious ones →
`run` scores the deterministic layers (routing confusion matrix, tool
selection, trajectory, argument checks, loops, answer must-contain/format
checks, **execution accuracy** for data-Q&A — grading the actual result set,
not the prose around it — **authz/scope** for permission cases, and crash
rate; pass^k across repeats for reliability) and sets the
baseline → from then
on: edit prompts → smoke run → full run before merges → `analyze` turns real
failures into new cases → once the judge is calibrated and the suite is big
enough, `optimize` proposes evidence-backed improvements.

## Staged rigor — why features may be "locked"

The plugin matches its demands to the app's maturity. If the app's route
targets and tools are still changing week to week, only invariant checks run
(crash rate,
format, loops, refusals) — trajectory assertions would just rot. Trajectory
evals unlock when boundaries are stable; LLM-judged layers unlock after judge
calibration (~30 human-labeled cases); `optimize` unlocks only with a
calibrated judge AND enough cases (~100+) for its statistical gate to mean
anything. `discover` tells the user which stage they are in and what unlocks
next. Never encourage skipping a gate — each one exists because skipping it
produces confidently wrong numbers.

## Who does what

Three roles, even if one person wears all hats (common at the start):

- **Developer** — runs the commands, owns the adapter and CI wiring, fixes
  failures, reviews `optimize` proposals.
- **Reviewer (QA)** — reviews generated cases, adds cases from exploratory
  testing, owns release-run sign-off. Never needs Claude Code: datasets are
  plain YAML, reports are HTML/markdown.
- **Domain arbiter** — the ONE person who is the final word on answer quality:
  labels the ~30 judge-calibration cases, approves rubrics, states business
  rules ("never quote a price not in the catalog"). ~1–2 h/week. This role
  cannot be automated and is the most common silent failure point when
  skipped — judge numbers stay watermarked PROVISIONAL until this happens.

## Where things live

- Plugin (this directory): reusable methodology, scorers, agents, docs.
- The state location (default `<app-repo>/.agent-eval/`, or the adapter's
  `state_location`): everything app-specific — `adapter.yaml`,
  `profile.yaml`, `datasets/` (smoke/full/holdout), `baselines/`, `runs/`,
  `candidates/`, `reports/`, and optionally `scripts/` (e.g. `smoke.sh` for
  the edit-hook, created from the plugin's `docs/smoke.sh.example`, or a
  `traces.mapping_shim`). In-repo it is versioned with the app so eval
  history travels with the app's git history; read-only/out-of-tree setups
  (state at any external path, no app patches) are equally supported.

## Honest limits (tell users when relevant)

Evals sample — they never prove correctness. Judged scores are only as good as
their calibration. Synthetic datasets misestimate real usage until real
traffic feeds the suite. Prompt edits cannot fix architecture problems — the
harness flags those instead. Nothing auto-merges; a human gates every change.
