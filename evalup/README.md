# evalup

> A Claude Code plugin that measures what your LLM chat or agent app gets wrong, and which prompt to fix.

evalup reads your app's code, writes test cases for it, runs them, and scores
each answer in layers — routing, tool use, answer checks — so a failure points
at one prompt or tool description. It works on anything it can call: a single
chat endpoint, a router→executor, a multi-agent orchestrator.

Claude Code does the judgment work: profiling the app, writing cases,
diagnosing failures, proposing edits. Stdlib-only Python scripts do the
scoring, so every number is cheap, fast and reproducible, and CI can gate on
it without Claude.

---

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Commands](#commands)
- [What gets measured](#what-gets-measured)
- [Limits](#limits)
- [Running tests](#running-tests)
- [Docs](#docs)
- [Contributing](#contributing)
- [License](#license)

---

## Prerequisites

- **Claude Code**
- **Python 3.9+**. No packages: the harness imports only the stdlib. The one
  exception, `scripts/convert_suite.py`, needs PyYAML to turn your YAML suite
  into JSON.
- **An app you can call**: an HTTP endpoint that returns one JSON body, or a
  Python `module:callable`.
- **Optional:** OpenTelemetry GenAI traces, exported to a file
  (`traces.source: otlp-file` — an OTLP collector's file exporter). A Jaeger
  or Tempo backend alone is not read in v1: you put a collector with a file
  exporter in front of it (the fan-out recipe is in `docs/traces-jaeger.md`)
  and `/evalup:discover` records what you declared — it installs nothing.
  Without traces, routing and answer checks still score; the tool-use,
  trajectory and cost layers need them.

No existing tests and no judge API key are needed — Claude Code subagents do
the judging.

Python 3.9 is past upstream end-of-life and is the floor on purpose: RHEL 9
ships it with backported fixes, and long-lived enterprise environments are
where this harness is meant to run.

---

## Installation

evalup is not published yet. Install it from a clone.

**Load it for one session:**

```bash
git clone <this-repo> evalup-repo
claude --plugin-dir ./evalup-repo/evalup
```

**Or install it through the repo's marketplace**, so every session has it:

```bash
claude plugin marketplace add ./evalup-repo
claude plugin install evalup@evalup
```

Check the install without starting a session:

```bash
python3 evalup-repo/evalup/scripts/stats.py --version
```

---

## Configuration

### Where state lives

The plugin holds methodology only. Everything about your app is written to
`your-app/.evalup/` as plain YAML, Markdown and JSON, to be versioned with the
app:

```
your-app/.evalup/
├── adapter.yaml          # how to call your app
├── profile.yaml          # what discover learned about it
├── datasets/             # test cases
└── reports/
    ├── baseline.json     # points at the pinned baseline run
    └── <run-id>/         # manifest, per-case raw material, results.json,
                          # review page; report.md/.html from /evalup:run
```

### Permissions

The skills pre-approve two things: running the plugin's own scripts
(`python3 <plugin>/scripts/*`), and `start`/`help` loading the skill they route
to. Anything that touches your side still asks: writing `.evalup/`, sending a
request to your app, editing a prompt.

- A pre-approval lasts until your next message. After you answer a question,
  the next script run may prompt once; "Yes, and don't ask again" settles it
  for the project.
- **Headless (`claude -p`, CI) with no permission host, or with
  `--permission-prompts none`: an unanswered prompt is a denial.** Allow what
  the run needs up front, or use the [scripts directly](#without-claude-ci).
  The recipe for a first session on a reachable app:

  ```bash
  claude -p "/evalup:start ./my-app — no transcripts, don't ask, run the first session" \
    --plugin-dir ./evalup-repo/evalup \
    --permission-mode acceptEdits --permission-prompts none \
    --allowedTools 'Bash(git -C *)' 'Bash(curl *)'
  ```

  `acceptEdits` covers the files the skills write under the app.
  `--permission-prompts none` denies anything that would still prompt and
  tells Claude not to retry it. `Bash(git -C *)` is the one shell command a
  skill mandates that the always-allowed read-only set does not cover
  (`discover`'s `git -C <app> status --porcelain`); `Bash(curl *)` is for
  reaching your app from the shell. Add rules only for what your adapter
  needs beyond that. The same two rules in the app's `.claude/settings.json`,
  for a project that runs this often:

  ```json
  { "permissions": { "allow": ["Bash(git -C *)", "Bash(curl *)"] } }
  ```

- **Headless: put the whole request in one prompt.** Pre-approvals belong to
  the turn that invoked the skill, so a `--resume` turn starts without them.
- **Run the recipe from a terminal or a CI job.** A Claude Code session in
  auto mode may refuse to spawn a nested `claude -p`; the classifier's reasons
  are not documented, so no allow rule is offered here — use a shell.
- Three facts about Bash rules that decide whether a headless run finishes
  (Claude Code's permission docs, checked 2026-09-25): a rule must match
  **every part** of a compound command, so `cd my-app && git status` prompts
  even with `Bash(git *)` allowed; `git -C <dir>` is not in the read-only set
  that never prompts; and `Grep`/`ls`/`cat` of the plugin directory prompt,
  because it is not a working directory — the skills `Read` it instead.

When a call is denied, the skills stop and say so rather than work around it.

---

## Usage

### See a run in 30 seconds (no app, no Claude)

From the plugin directory:

```bash
python3 examples/quickstart/demo.py            # a passing run
python3 examples/quickstart/demo.py --break    # a failing run and a closed gate
python3 examples/quickstart/demo.py --keep     # keep the run dir and its HTML review page
```

It starts a two-domain demo app on a local port and runs six cases through the
real runner and scorers. `--break` prints:

```
  c-2d7c6a11  fail       <- failed: answer
  c-3a5e47d9  pass
  ...
routing: accuracy 1.0, macro-F1 1.0

smoke-20260924T114421Z: FAIL
  6 cases: 5 pass, 1 fail (1 gating), 0 unscored, 0 skipped
  infra 0.0%, crash 0.0%, scorer errors 0
  NOT scored this run: authz, cost_latency, loops, multi_turn, tool_selection, trajectory
  judged layers: unjudged (mode: smoke)
  gate closed: 1 gating failure(s): c-2d7c6a11 (answer)
```

`examples/quickstart/README.md` walks through every file the run writes.

### First session on your app

```
> /evalup:start path/to/your/app
```

The first session ends in a scored run, not a to-do list. It:

1. reads your code and states what it can and cannot measure on the app as it stands
2. shows the top design findings — often worth more than the eval
3. writes about a dozen test cases
4. runs them and shows what failed and where

It does not interview you or ask you to change your source first. Every later
step — tracing, a fuller suite with a sealed holdout, judge calibration, the
optimizer — is one you ask for. `/evalup:start` always says what is unlocked,
what is locked, and what unlocking costs.

The **holdout** is a set of cases never looked at while you tune, so the final
check is honest.

### Start from real conversations

Skip synthetic cases and review what your app actually did:

```
> /evalup:analyze --transcripts path/to/logs
```

### Improve a prompt

```
> /evalup:optimize
> /evalup:optimize --surface <prompt-id|tool> --budget $5
```

It reads failing traces, names the cause, proposes one change at a time, and
keeps it only if the sealed holdout confirms the improvement statistically.
You approve every kept edit; nothing is committed or merged for you. It runs
only when you type it, and stays locked until its preconditions hold: a
calibrated judge (when any objective is judged), about 100 usable cases (50
for deterministic-only objectives), `stage: stable` or later, white-box prompt
access, and a budget. A first session is several sessions away from that;
`/evalup:start` says how far.

### Without Claude (CI)

Four scripts run a suite with no LLM involved. From the plugin directory:

```bash
python3 scripts/convert_suite.py your-app/.evalup -o converted.json
python3 scripts/make_plan.py converted.json --mode regression \
  --state-dir your-app/.evalup -o plan.json    # prints the exact run command next
python3 scripts/run_cases.py --plan plan.json --out your-app/.evalup/reports/<run-id>
python3 scripts/gate.py your-app/.evalup/reports --latest --mode regression
```

`gate.py` exits `0` open, `1` closed, `2` bad input, and prints a one-screen
summary naming what failed. The full headless recipe, including a
Claude-driven run with a tokenless gate step, is in
`skills/run/references/run-modes.md` ("Headless/CI gate").

---

## Commands

The three you will type:

| Command | Description |
|---|---|
| `/evalup:start [path]` | Detects where you are and does the next step: setup, first run, or what to do next |
| `/evalup:analyze` | Clusters failures (`--cluster`), reviews real transcripts (`--transcripts <path>`), calibrates the judge (`--label`), mines live traces (`--mine`); builds a hotkey review page |
| `/evalup:optimize` | Failure-driven prompt and tool-description edits with statistical keep/revert |

The steps `start` runs for you, callable directly:

| Command | Description |
|---|---|
| `/evalup:discover [path-or-url] [--diff]` | Profiles the app; writes the adapter and profile; patches instrumentation; `--diff` after a refactor |
| `/evalup:generate [--layer routing\|tools\|answer] [--count N]` | Builds or extends the dataset: coverage grid, targeted review, splits |
| `/evalup:run` | Executes and scores every applicable layer; diffs against the baseline |
| `/evalup:help` | Explains any of this |

`/evalup:run` modes:

| Flag | Runs |
|---|---|
| `--smoke` | A fast subset |
| `--regression` | The full suite with pass^k across repeats |
| `--targeted` | Only what changed |
| `--holdout` | The sealed holdout |
| `--full` | Release check (currently two runs) |

Other `run` flags: `--tag <component>`, `--filter-failing`, `--layer X`,
`--k N`, `--baseline`.

---

## What gets measured

Each layer isolates one part of the app, so a failure names the prompt to fix.

| Layer | Checks | Scored by |
|---|---|---|
| **Routing** | Accuracy, per-target F1 (macro and micro), confusion matrix, out-of-scope leakage | Script |
| **Tool use** | Selection precision/recall, argument correctness, trajectory subset/order match, forbidden tools, loop detection | Script (needs traces) |
| **Answer** | Must-contain, regex, JSON format | Script |
| **Execution accuracy** | Result-set comparison (order-insensitive, float-tolerant) and scalar quasi-exact match against a read-only oracle — catches the confident-but-wrong answer | Script |
| **Authorization / scope** | Forbidden tools, out-of-scope record IDs, scoped refusal — read from the tool-call log, never the chat text | Script |
| **Reliability & ops** | pass@k vs pass^k (the gap is flakiness), crash rate, latency and token cost per stage | Script (cost needs traces) |

Every run records a manifest — dataset version, app git SHA, model and judge
versions. Diffs between incomparable runs are refused.

---

## Limits

- **Single-turn only.** Multi-turn cases are skipped; a suite made only of them
  closes the gate. Streaming (SSE), WebSocket and CLI apps are not driven.
- **Judged answer quality and business rules have no run-time scorer.** The
  judge agent and its calibration flow (`/evalup:analyze --label`) exist; runs
  report those layers "not measured".
- **Tool expectations are whole-app.** In a multi-agent app, a call made by the
  wrong sub-agent still satisfies `expect.tools`.
- **No retrieval/RAG scoring.**
- **Runs are serial.** 100 cases × 3 repeats at 8 s a call is about 40 minutes.
- **Trace conventions move.** The OpenTelemetry `gen_ai.*` conventions are
  still Development status upstream, so `scripts/normalize_trace.py` tracks a
  moving spec.

---

## Running tests

From the plugin directory:

```bash
python3 -m unittest discover -s tests       # 799 tests, ~2.5 min
ruff check --config ruff.toml .             # --config is required
uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q
```

The third command is the only conclusive check of the Python 3.9 floor. Expect
791 passed and 8 skipped; add `--with pyyaml` to run the skipped eight.

`.github/workflows/ci.yml` has never run: the repository has no remote. These
local commands are the only checked claim.

---

## Docs

| File | Covers |
|---|---|
| `examples/quickstart/README.md` | The worked example: what `discover`, `generate` and `run` write, and what a trace-less app can score |
| `docs/workflow.md` | Day 1 → steady state → production, and who does what |
| `docs/concepts.md` | Eval layers, staged rigor, ground truth, the judge |
| `skills/discover/references/adapter-contract.md` | The language-agnostic adapter spec; `skills/discover/references/adapters/dotnet.md` is the first reference adapter (.NET) |
| `docs/runner-contract.md` | The plan and run-directory format `run_cases.py` reads and writes |
| `docs/rubric-format.md` | The decomposed-binary judge rubric and calibration |
| `docs/traces-jaeger.md` | Feeding `otlp-file` from a Jaeger/Tempo setup today, and the design note for a Jaeger client and header-based correlation |
| `skills/analyze/references/annotation-ux.md` | The open → axial error-analysis workflow |
| `docs/research.md` | The research behind the design decisions |
| `CHANGELOG.md` | What changed, release by release |

---

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) before changing the plugin. It has
the three checks, how the 3.9 floor is verified, the path convention for
skills, and the rules that look arbitrary until you break one.

---

## License

MIT — see [`LICENSE`](LICENSE).
