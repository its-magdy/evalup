# evalup

A Claude Code plugin that **evaluates and improves LLM chat/agent
applications** — any app it can invoke programmatically: simple chat,
router→executor, multi-agent orchestrators.

Claude Code does the intelligent work (profiling your app, generating test
cases, diagnosing failures, proposing prompt/tool-description fixes).
Deterministic Python scripts do the scoring, so numbers are cheap, fast, and
reproducible. OpenTelemetry traces are the evidence.

## What you need

- **An app you can call**: an HTTP endpoint that answers with one JSON body,
  or a Python `module:callable`. That is the only hard requirement.
  **Single-turn only:** a test case with more than one user turn is skipped,
  and streaming (SSE), WebSocket and CLI apps are not driven.
- **Claude Code**, and Python 3.9+ on the machine. No packages to install; the
  scoring scripts are stdlib-only.
- **Nothing else to start.** No tracing, no existing tests, no API keys for a
  judge (Claude Code subagents do the judging). Without OpenTelemetry GenAI
  traces you still get routing and answer-level scores; tool-use, trajectory
  and cost layers need traces, and `discover` can add them later if you want.

## See it run first (30 seconds, no app, no Claude)

```sh
python3 examples/quickstart/demo.py            # a green run
python3 examples/quickstart/demo.py --break    # a red one, and a closed gate
```

It starts a small two-domain demo app on a local port, runs six cases through
the real runner and scorers, and prints the verdicts, the routing score, and
the pass/fail line CI would see. `--keep` leaves the run directory and an HTML
review page to open. `examples/quickstart/README.md` walks through every file.

## Quick start

```
claude --plugin-dir ./evalup     # from a clone of this repo
> /evalup:start path/to/your/app
```

**The first session ends in a result, not a to-do list.** It reads your code,
tells you what it can and cannot measure on your app as it stands, shows the
top design findings (often worth more than the eval), writes about a dozen test
cases, runs them, and shows what failed and where. It does not interview you or
ask to change your source first.

Everything rigorous is a later step you ask for: tracing (tool-use and cost
layers), a fuller suite with a sealed holdout — test cases set aside and never
looked at while you tune, so the final check is honest — (regression gating), judge
calibration (judged answer quality), and the optimizer. `/evalup:start` always
tells you what is unlocked, what is locked, and what unlocking costs.

**Already have real conversations?** Skip the synthetic cases:
`/evalup:analyze --transcripts path/to/logs` goes straight to looking at what
your app actually did — no setup at all.

Lost at any point: `/evalup:help`.

### Permissions

The skills pre-approve two things and nothing else: running the plugin's own
scripts (`python3 <plugin>/scripts/*`) and `start`/`help` loading the skill
they route to. Everything that touches *your* side still asks: writing
`.evalup/`, sending a request to your app, editing a prompt. That is
deliberate. Two things worth knowing:

- A skill's pre-approval lasts until your next message, so after you answer
  a question the next script run may prompt once. "Yes, and don't ask again"
  settles it for the project.
- **Headless (`claude -p`, CI): a prompt nobody answers is a denial.** Allow
  what the run needs up front — `--permission-mode acceptEdits` plus
  `--allowedTools` for the script rule above and for reaching your app — or
  use the scripts directly (`docs/workflow.md`), which need no Claude at all.
  Put everything in the one prompt (`/evalup:start <app> — no transcripts,
  don't ask, run the first session`): the pre-approvals belong to the turn
  that invoked the skill, so a second `--resume` turn starts without them.
  The skills stop and say so when a call is denied rather than working
  around it.

## Commands

Three you will type:

| Command | What it does |
|---|---|
| `/evalup:start` | Detects where you are and does the next step — setup, first run, or "here is what to do next" |
| `/evalup:analyze` | Look at failures: cluster them, review real transcripts (`--transcripts`), calibrate the judge (`--label`), mine live traces (`--mine`); builds a hotkey review page |
| `/evalup:optimize` | Failure-driven prompt/tool-description improvement with statistical keep/revert. Runs only when you type it; locked until its preconditions hold |

And the steps `start` runs for you, callable directly once you know them:

| Command | What it does |
|---|---|
| `/evalup:discover` | Profile the app; write adapter + profile; patch instrumentation; `--diff` after refactors |
| `/evalup:generate` | Build/extend the eval dataset (coverage grid, targeted review, splits) |
| `/evalup:run` | Execute + score all applicable layers; baseline diff. Modes: `--smoke` (fast subset) · `--regression` (full, pass^k) · `--targeted` (only what changed) · `--holdout` (sealed) · `--full` (release) |
| `/evalup:help` | Explain any of this |

Without Claude at all — CI, or a colleague who never opens it:
`scripts/convert_suite.py` (YAML → JSON) → `scripts/make_plan.py` (the run's
plan, and the exact command to run next) → `scripts/run_cases.py` →
`scripts/gate.py` (exit 0 pass / 1 fail, one-screen summary naming what
failed).

**What this does not measure yet**, so you hear it here and not after setup:
**multi-turn conversations** (such cases are skipped, and a suite made only of
them closes the gate rather than passing it); **judged answer quality and
business rules inside a run** — the judge agent and its calibration flow exist,
but no run scores those two layers yet, so they are reported "not measured".
In a multi-agent app, tool-call expectations are whole-app, not per-agent — a
call made by the *wrong* sub-agent still satisfies `expect.tools`. Retrieval/RAG
quality is not scored. Runs are serial: ~100 cases × 3 repeats at 8s a call is
about 40 minutes.

## What gets measured

Layered, so a failure tells you *which prompt to fix*:

- **Routing** (router/multi-agent apps) — accuracy, per-target F1
  (macro+micro), confusion matrix, out-of-scope leakage (deterministic)
- **Tool use** — selection precision/recall, argument correctness,
  trajectory subset/order matching, forbidden-tool policy, loop detection
  (deterministic)
- **Answer quality** — must-contain/regex and JSON-format checks
  (deterministic), faithfulness to tool results, completeness, business
  rules. Only the deterministic checks are scored in a run today; the
  decomposed-binary, reference-guided judge is used in the labeling and
  calibration flow (`analyze --label`), and its run-time layer is not built
- **Execution accuracy** (data-Q&A) — grades the actual result set, not the
  prose: result-set comparison (order-insensitive, float-tolerant) and GAIA-style
  scalar quasi-exact-match, against ground truth from a read-only oracle
  (deterministic) — catches the confident-but-wrong answer a prose check waves through
- **Authorization / scope** — deterministic checks over the tool-call log and
  returned record IDs (forbidden tools, out-of-scope leakage, scoped refusal) —
  never a chat-text read, so a polite refusal over an open endpoint still fails
- **Reliability & ops** — pass@k / pass^k across repeats (the gap is the
  flakiness signal), crash rate, latency and token cost per agent stage

## Principles (the short version)

- **Staged rigor.** The harness matches its demands to your app's maturity.
  Churning architecture → invariant checks only; trajectory evals, judged
  layers, and the optimizer unlock as preconditions are met. It tells you
  what's locked and why.
- **Evidence before edits.** The optimizer reads failing traces and names the
  cause before proposing anything, proposes ONE change at a time, and keeps
  it only if a sealed holdout confirms improvement with statistical honesty.
- **Comparability or nothing.** Every run records a manifest (dataset
  version, app git SHA, model and judge versions…). Diffs across
  incomparable runs are refused, not fudged.
- **Humans stay in the loop where it matters**: reviewing suspicious
  generated cases, labeling ~30 judge-calibration cases, approving every
  kept edit. Nothing is committed or merged automatically.

## Where state lives

The plugin is reusable methodology. Everything about *your* app lives in
`your-app/.evalup/` (adapter, profile, datasets, and `reports/` — one
`reports/<run-id>/` folder per run with its manifest, per-case raw material,
and results (`/evalup:run` adds the written `report.md`/`.html`; a scripts-only
run has `results.json` and the review page), plus `reports/baseline.json` pointing at the pinned baseline
run) — plain YAML/Markdown/JSON, versioned with your app, readable by
teammates who never open Claude Code.

## Docs

- `examples/quickstart/README.md` — the worked end-to-end example: what
  `discover`, `generate` and `run` actually write, and which layers a
  trace-less app can still score
- `docs/workflow.md` — day 1 → steady state → production, and who does what
- `docs/concepts.md` — eval layers, staged rigor, ground truth, the judge
- `skills/discover/references/adapter-contract.md` — the language-agnostic adapter
  spec (the seam that keeps the tool general);
  `skills/discover/references/adapters/dotnet.md` is
  the first reference adapter (any other stack implements the same contract)
- `docs/rubric-format.md` — the decomposed-binary DAG judge rubric + calibration
- `skills/analyze/references/annotation-ux.md` — the open→axial error-analysis workflow
- `docs/migrate-run-layout.md` — moving a pre-`reports/<run-id>` state dir onto
  the per-run layout (and what `run` does when it finds the old one)
- `docs/research.md` — pointer to the research behind the design decisions
- `CONTRIBUTING.md` — **read this before changing the plugin**: the three
  blessed checks, and the conventions that look arbitrary until you break one
- `CHANGELOG.md` — what changed, release by release

### Path convention

Skills, references, and agents run with the CWD set to the **user's app**, not
to the plugin, so a bare `scripts/x.py` or `docs/x.md` inside them resolves to
the wrong place. One rule, applied throughout:

- Anything executed, or read across skills — a script, a `docs/` page, an
  `agents/*.md`, another skill's `SKILL.md` or `references/` — is written
  `${CLAUDE_PLUGIN_ROOT}/...`.
- A `SKILL.md` pointing into its own `references/` uses a relative markdown
  link, e.g. `[references/run-modes.md](references/run-modes.md)`.
- Paths that belong to the app or its state location (`datasets/`,
  `reports/`, the state dir's own `scripts/smoke.sh`) stay bare and relative
  — they are deliberately *not* plugin paths.

The paths listed in this README and in the scripts' own docstrings are
repo-relative, for a human reading the source at the plugin root.

## Requirements

Python 3.9+, stdlib only — nothing the harness runs imports a package. 3.9 is
past upstream end-of-life (October 2025) and is kept as the floor deliberately:
RHEL 9 ships it with vendor-backported fixes, and long-lived enterprise
environments are where this harness is meant to run. The one exception is a
convenience, not a runtime dependency: `scripts/convert_suite.py` uses PyYAML
to turn your YAML into the JSON everything else reads.

An app you can invoke programmatically. OpenTelemetry with GenAI spans is
recommended — without traces, trajectory layers are unavailable and only
routing and answer-level evals run. The `gen_ai.*` conventions are still
Development-status upstream, so `normalize_trace.py` tracks a moving spec.

Changing the plugin? `CONTRIBUTING.md` has the three blessed checks, how the
3.9 floor is verified, and why the toolchain files look the way they do.
`scripts/stats.py --version` reports the harness version a run manifest
records. `.github/workflows/ci.yml` **has never executed** — this repository
has no git remote — so those local checks are the only checked claim.
