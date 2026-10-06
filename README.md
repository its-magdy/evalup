# evalup

> A Claude Code plugin that measures what your LLM chat or agent app gets wrong, and which prompt to fix.

[![ci](https://github.com/its-magdy/evalup/actions/workflows/ci.yml/badge.svg)](https://github.com/its-magdy/evalup/actions/workflows/ci.yml)
![version](https://img.shields.io/badge/version-0.1.0-blue)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

evalup reads your app's code, writes test cases for it, runs them, and scores
each answer in layers: routing, tool use, answer checks. A failure points at
one prompt or tool description, not at "the model".

Claude Code does the judgment work: profiling the app, writing cases,
diagnosing failures, proposing edits. Stdlib-only Python scripts do the
scoring, so every number is reproducible and CI can gate on it without Claude.

---

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Usage](#usage)
- [Commands](#commands)
- [Compared with evals-skills](#compared-with-evals-skills)
- [Repository layout](#repository-layout)
- [Running tests](#running-tests)
- [Contributing](#contributing)
- [License](#license)

---

## Prerequisites

- **Claude Code**
- **Python 3.9+**, stdlib only. `scripts/convert_suite.py` alone needs PyYAML.
- **An app you can call**: an HTTP endpoint that returns one JSON body, or a
  Python `module:callable`.
- **Optional:** OpenTelemetry GenAI traces exported to a file. Without them,
  routing and answer checks still score; tool use, trajectory and cost need them.

No judge API key is needed: Claude Code subagents do the judging.

---

## Installation

Install through this repo's marketplace, so every session has the plugin:

```bash
claude plugin marketplace add its-magdy/evalup
claude plugin install evalup@evalup
```

Or load it from a clone for one session:

```bash
git clone https://github.com/its-magdy/evalup.git evalup-repo
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

It proposes one change at a time and keeps it only if the sealed holdout
confirms the improvement statistically. You approve every kept edit.

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

## Compared with evals-skills

[`hamelsmu/evals-skills`](https://github.com/hamelsmu/evals-skills) is the
closest neighbour: Claude Code skills for eval methodology.

| | evals-skills | evalup |
|---|---|---|
| Error analysis, synthetic data, judge prompts | Yes | Yes |
| Runs cases against your app | No | Yes |
| Deterministic scorers (routing, tools, trajectory, answer) | No | Yes |
| Statistics (pass^k, confidence gates) and a sealed holdout | No | Yes |
| CI gate that needs no LLM | No | Yes |

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
python3 -m unittest discover -s tests                    # 943 tests, ~4 min
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
