# evalup

> Point it at an AI feature and, in one session, see which answers are wrong, with evidence you can check, and which prompt to fix.

[![ci](https://github.com/its-magdy/evalup/actions/workflows/ci.yml/badge.svg)](https://github.com/its-magdy/evalup/actions/workflows/ci.yml)
![version](https://img.shields.io/badge/version-0.1.0-blue)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

evalup is a Claude Code plugin for LLM chat and agent apps. It reads your
app's code, writes test cases, runs them against the app, and scores each
answer in layers, so a failure points at one prompt or tool description, not
at "the model". Claude does the judgment work; stdlib-only Python scripts do
the scoring, so CI can gate on the numbers without Claude.

What it is built around:

- **Deterministic, layered scores.** Routing F1 with a confusion matrix, tool
  and trajectory matching, answer checks, execution accuracy against a
  read-only oracle, authorization read from the tool-call log. Scripts
  produce every gated number, so the same replies always score the same.
- **Business rules with their sources.** `discover` records each rule with a
  quote, source and date; a run reports by rule id which rules passed,
  checked by script where a script can check them.
- **Runs it can compare.** Canaries, a fixed subset with versioned answers,
  abort a run when one fails, so drift stops the run instead of skewing it.
  A diff against a baseline with another dataset, scoring version, repeat
  count or judge is refused.
- **A keep/revert gate on a sealed holdout.** `/evalup:optimize` edits one
  prompt at a time in your app's own source and keeps the edit only if, on a
  holdout it never tuned on, the posterior probability that it is better is
  at least 0.95 and it moved more cases than re-running the unchanged app
  does. Otherwise git reverts it. You approve every kept edit.

**Not proven yet:** the live proof
([#27](https://github.com/its-magdy/evalup/issues/27)) is still running. The
tool-use layers have produced their first verdicts on a live app; the
run-time judge and a full optimize loop have not. Judged verdicts are
provisional and never gate. Known gaps are in
[`evalup/README.md`](evalup/README.md#limits); how evalup sits beside
deepeval, promptfoo, RELAI and evals-skills is [below](#neighbours).

---

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Usage](#usage)
- [Commands](#commands)
- [Neighbours](#neighbours)
- [Repository layout](#repository-layout)
- [Running tests](#running-tests)
- [Contributing](#contributing)
- [License](#license)

---

## Prerequisites

- **Claude Code**
- **Python 3.9+ with PyYAML**: `scripts/convert_suite.py` converts the YAML
  suite before every run and needs it (`python3 -m pip install --user pyyaml`);
  everything else imports only the stdlib.
- **An app you can call**: an HTTP endpoint that returns one JSON body, or a
  Python `module:callable`.
- **Optional:** OpenTelemetry GenAI traces exported to a file. Without them,
  routing and answer checks still score; tool use, trajectory and cost need them.

No judge API key is needed. Judging and judge calibration use Claude Code
subagents; runs report judged layers as not measured.

---

## Installation

Clone the latest release, **v0.1.0** (leave out `--branch v0.1.0` to track
`main` instead):

```bash
git clone --branch v0.1.0 https://github.com/its-magdy/evalup.git evalup-repo
```

Then install it through the clone's marketplace, so every session has the
plugin:

```bash
claude plugin marketplace add ./evalup-repo
claude plugin install evalup@evalup
```

Or load it for one session:

```bash
claude --plugin-dir ./evalup-repo/evalup
```

Check a clone without starting a session:

```bash
python3 evalup-repo/evalup/scripts/stats.py --version
```

```
evalup harness 0.1.0
```

Configuration, where state lives (`your-app/.evalup/`), and the permission
rules a headless run needs are in [`evalup/README.md`](evalup/README.md#configuration).

---

## Usage

### See a run in 30 seconds (no app, no Claude)

```bash
cd evalup-repo/evalup
python3 examples/quickstart/demo.py --break
```

It starts a two-domain demo app on a local port and runs six cases through the
real runner and scorers. `--break` plants a flaw, so the gate closes:

```
  c-2d7c6a11  fail       <- failed: answer
  c-3a5e47d9  pass
  ...
routing: accuracy 1.0, macro-F1 1.0

smoke-20260930T165304Z: FAIL
  6 cases: 5 pass, 1 fail (1 gating), 0 unscored, 0 skipped
  infra 0.0%, crash 0.0%, scorer errors 0
  NOT scored this run: authz, cost_latency, loops, multi_turn, tool_selection, trajectory
  judged layers: unjudged (mode: smoke)
  gate closed: 1 gating failure(s): c-2d7c6a11 (answer)
```

Drop `--break` for a passing run; add `--keep` to keep the run directory and its HTML review page.

### First session on your app

```
> /evalup:start path/to/your/app
```

The first session ends in a scored run. It:

1. reads your code and states what it can and cannot measure
2. shows the top design findings
3. writes about a dozen test cases
4. runs them and shows what failed and where

Tracing, a fuller suite with a sealed holdout, judge calibration and the
optimizer are later steps you ask for. `/evalup:start` says what each one costs.

### Improve a prompt

```
> /evalup:optimize --surface <prompt-id|tool> --budget $5
```

It proposes one change at a time and keeps it only if, on the sealed holdout,
the posterior probability that it is better is at least 0.95 and it changed
more cases than re-running the unchanged app does. You approve every kept edit.

### In CI, without Claude

```bash
python3 scripts/gate.py your-app/.evalup/reports --latest --mode regression
```

Exits `0` open, `1` closed, `2` bad input. The four-script recipe that builds
and runs the suite first is in [`evalup/README.md`](evalup/README.md#without-claude-ci).

---

## Commands

| Command | Description |
|---|---|
| `/evalup:start [path]` | Detects where you are and does the next step: setup, first run, or what to do next |
| `/evalup:discover [path-or-url]` | Profiles the app; writes the adapter and profile |
| `/evalup:generate` | Builds or extends the dataset: coverage grid, targeted review, splits |
| `/evalup:run` | Executes and scores every applicable layer; diffs against the baseline |
| `/evalup:analyze` | Clusters failures, reviews real transcripts, calibrates the judge |
| `/evalup:optimize` | Failure-driven prompt and tool-description edits with statistical keep/revert |
| `/evalup:help` | Explains any of this |

Flags, run modes, the scored layers and known limits are in
[`evalup/README.md`](evalup/README.md#commands).

---

## Neighbours

Checked 2026-11-22. Each of these also ships as a Claude Code plugin or skills.

| Project | What it is |
|---|---|
| [deepeval](https://github.com/confident-ai/deepeval) | A Python evaluation framework: metrics, many of them LLM-judged, run like unit tests; an official plugin for evals and tracing. |
| [promptfoo](https://github.com/promptfoo/promptfoo) | Declarative-config evals and red-teaming from the command line and CI; an official plugin. evalup does no red-teaming and points to promptfoo for it. |
| [RELAI agent-eval-loop](https://github.com/relai-ai/agent-eval-loop) | The closest neighbour: a Claude Code and Codex plugin that discovers risks, builds frozen end-to-end evals (Harbor or native runners) with reviewed graders, and improves an agent on a branch with before-and-after scores and optional held-out evals. |
| [ai-evals-course/evals-skills](https://github.com/ai-evals-course/evals-skills) | Skills for eval methodology: error analysis, synthetic data, judge prompts (replaces the deprecated `hamelsmu/evals-skills`). It does not run cases against an app. |

evalup's difference is in the list at the top: script-only gated scores in
named layers, rules scored by id against their sources, canaries and refused
diffs, and a stated Bayesian keep rule over a measured noise floor.

---

## Repository layout

| Path | Contents |
|---|---|
| `evalup/` | The plugin: 7 skills, 3 agents, 21 CLI scripts, specs in `docs/`, the worked example in `examples/quickstart/` |
| `evalup/evals/` | `claude plugin eval` cases |
| `.claude-plugin/marketplace.json` | The marketplace that `claude plugin marketplace add` reads |
| `EVAL-DESIGN-RECOMMENDATION.md`, `research/` | Why the plugin works the way it does |

---

## Running tests

From `evalup/`:

```bash
python3 -m unittest discover -s tests                    # 993 tests, ~2.5 min
ruff check --config ruff.toml .                          # --config is required
uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q
```

The third command is the only conclusive check of the Python 3.9 floor.
CI runs the same checks on Linux (3.9, 3.11, 3.13) on every push.

---

## Contributing

Read [`evalup/CONTRIBUTING.md`](evalup/CONTRIBUTING.md) before changing the
plugin. It has the three checks and the rules that look arbitrary until you
break one.

---

## License

MIT. See [`LICENSE`](LICENSE).
