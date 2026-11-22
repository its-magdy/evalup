# evalup

> Point it at an AI feature and, in one session, see which answers are wrong, with evidence you can check, and which prompt to fix.

evalup, a Claude Code plugin, reads your app's code, writes test cases for
it, runs them, and scores each answer in layers — routing, tool use, answer
checks — so a failure points at one prompt or tool description. It works on
anything it can call: a single chat endpoint, a router→executor, a
multi-agent orchestrator.

Claude Code does the judgment work: profiling the app, writing cases,
diagnosing failures, proposing edits. Stdlib-only Python scripts do the
scoring, so every number is cheap, fast and reproducible, and CI can gate on
it without Claude.

On top of the layered scores: business rules scored by id against their
sources, canaries that abort a drifting run, refused diffs between runs that
cannot be compared, and an optimizer that edits prompts in your app's own
source behind a keep/revert gate on a sealed holdout (not yet proven on a
live app, #27). How that sits beside deepeval, promptfoo, RELAI and
evals-skills is in the [repository README](../README.md#neighbours).

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

- **Claude Code.** evalup installs as a Claude Code plugin only: claude.ai
  and Cowork don't install a plugin that ships a `bin/` directory, and the
  skills call the scripts through `bin/evalup`.
- **Python 3.9+, and PyYAML or `uv`.** `scripts/convert_suite.py` turns your
  YAML test suite into JSON before every run, and it needs PyYAML. A stock
  macOS `python3` does not have it. With [`uv`](https://docs.astral.sh/uv/)
  on `PATH` you need nothing more: the script re-runs itself under
  `uv run --no-project --with pyyaml` (set `EVALUP_NO_UV=1` to stop that).
  Otherwise `python3 -m pip install --user pyyaml`, or a venv with PyYAML
  first on your `PATH` if pip refuses (Homebrew Python). Everything else
  imports only the stdlib.
- **An app you can call**: an HTTP endpoint that returns one JSON body or a
  server-sent event stream (AG-UI, or any JSON-frame stream with a declared
  text path: `invocation.response_format: sse`), a Python
  `module:callable`, or a command the runner starts per call with the
  request as JSON on stdin and a JSON reply on stdout
  (`invocation.mode: exec` — a CLI, or a small shim in the app's own
  language for an app behind auth or not reachable over HTTP).
- **Optional:** tool calls the response itself carries — an AG-UI stream's
  tool-call events, or a list in the JSON body (`traces.source: response`,
  no collector needed) — or OpenTelemetry GenAI traces exported to a file
  (`traces.source: otlp-file` — an OTLP collector's file exporter). A Jaeger
  or Tempo backend alone is not read in v1: you put a collector with a file
  exporter in front of it (the fan-out recipe is in `docs/traces-jaeger.md`)
  and `/evalup:discover` records what you declared — it installs nothing.
  Without traces, routing and answer checks still score; the tool-use,
  trajectory and cost layers need them.

No existing tests and no judge API key are needed. Judging and judge
calibration use Claude Code subagents. After a run, a case that pins a rubric
is judged in the same turn and shown as **provisional**, with quoted
evidence; no judged verdict reaches a pass rate, the gate or the optimizer
(`docs/judge-run.md`). CI can gate without Claude: it gates on the scripted
layers only.

Python 3.9 is past upstream end-of-life and is the floor on purpose: RHEL 9
ships it with backported fixes, and long-lived enterprise environments are
where this harness is meant to run.

---

## Installation

The latest release is **v0.1.0**. It is not listed in a public plugin
marketplace yet, so install it from a clone of the tag. Leave out
`--branch v0.1.0` to track `main` instead.

**Load it for one session:**

```bash
git clone --branch v0.1.0 https://github.com/its-magdy/evalup.git evalup-repo
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
python3 evalup-repo/evalup/scripts/convert_suite.py --self-check
```

The second line fails with exit 2 and an install hint when that `python3`
cannot import PyYAML and no `uv` can supply it (through `uv` it prints
`"via": "uv"`); `/evalup:start` runs the same check before it reads your
app.

---

## Configuration

### Where state lives

The plugin holds methodology only. Everything about your app is written to
`your-app/.evalup/` as plain YAML, Markdown and JSON, to be versioned with the
app:

```
your-app/.evalup/
├── adapter.yaml          # how to call your app
├── profile.yaml          # what discover learned about it, business rules included
├── findings.md           # design findings, docs-vs-code contradictions among them
├── quotes.local.md       # only for rules from outside sources; git-ignored
├── datasets/             # test cases
└── reports/
    ├── baseline.json     # points at the pinned baseline run
    └── <run-id>/         # manifest, per-case raw material, results.json,
                          # review page; report.md from /evalup:run
```

**What is versioned, and who commits it.** evalup never commits anything;
you do. Everything above is meant to be versioned with the app except
`work/` (derived files, rebuilt every run) and `quotes.local.md`. That
includes the business rules in `profile.yaml`, each with its source, date
and status (`inferred` until a human confirms it): a fresh clone or a CI
job that runs the validator (below) then refuses the same unconfirmed
rules you do, and confirming a rule is a reviewable diff. A rule read from outside the repo (a wiki, a shared
drive, an MCP server, read only with your yes) keeps its distilled text,
source, date and status in `profile.yaml`; its verbatim quote goes to
`quotes.local.md`, which discover adds to `.evalup/.gitignore` before
writing it, unless you ask for the quote to be versioned too. To keep all
of it out of the app's repo, point `state_location` outside it.

### Permissions

The skills pre-approve two things: running the plugin's own scripts
(`evalup *`, a command the plugin puts on Claude Code's PATH), and `start`/`help` loading the skill they route
to. Anything that touches your side still asks: writing `.evalup/`, sending a
request to your app, editing a prompt.

- `evalup` is on PATH only where Claude Code puts the plugin's `bin/`
  there; a subagent's shell or a `--plugin-dir` session has been seen
  without it. On `evalup: command not found`, the same script is
  `python3 <plugin root>/scripts/<name>.py …` (`_` for `-`), which
  `Bash(evalup *)` does not pre-approve: allow it, or answer its prompt.
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
    --allowedTools 'Bash(git -C *)'
  ```

  `acceptEdits` covers the files the skills write under the app.
  `--permission-prompts none` denies anything that would still prompt and
  tells Claude not to retry it. `Bash(git -C *)` is the one shell command a
  skill mandates that the always-allowed read-only set does not cover
  (`discover`'s `git -C <app> status --porcelain`). No `curl` rule is
  needed: discover checks your app with `evalup run-cases --probe`, a
  pre-approved script that reads `cookie_env`/`token_env` itself. A `curl`
  naming `$APP_TEST_COOKIE` would be denied anyway — Claude Code refuses a
  command whose variables it cannot check, allow rule or not. Export every
  env var your adapter references (its `${…}` refs, `cookie_env`,
  `token_env`) before you start; discover's summary lists them for next
  time. The same rule in the app's `.claude/settings.json`, for a
  project that runs this often:

  ```json
  { "permissions": { "allow": ["Bash(git -C *)"] } }
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

When a call the step needs is denied, the skills stop and say so rather than
work around it; a denied optional read (a shell `grep` they can do without)
does not end the session.

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
keeps it only if, on the sealed holdout, the posterior probability that it is
better is at least 0.95 and it changed more cases than re-running the
unchanged app does (a keep is not "statistically significant" unless the sign
test `stats.py` reports beside it also passes). You approve every kept edit; nothing is committed or merged for you. It runs
only when you type it, and stays locked until its preconditions hold: a
calibrated judge (when any objective is judged), about 100 usable cases (50
for deterministic-only objectives), `stage: stable`, white-box prompt
access, a budget, and an edit that can reach the running app (a read-only
repo gets recommendations only; a prompt compiled into the build waits for
you to rebuild). A first session is several sessions away from that;
`/evalup:start` says how far.

### Without Claude (CI)

Five scripts run a suite with no LLM involved: a script, run from the
plugin directory (`bash ci.sh`, not pasted into a terminal, which `set -e`
would close on the first failure):

```bash
#!/usr/bin/env bash
set -e   # stop at the first command that fails
python3 scripts/convert_suite.py your-app/.evalup -o converted.json
python3 scripts/validate_cases.py --cases converted.json \
  --capabilities converted.json --profile converted.json
run_id="regression-$(date -u +%Y%m%dT%H%M%SZ)"
python3 scripts/make_plan.py converted.json --mode regression --run-id "$run_id" \
  --state-dir your-app/.evalup -o plan.json
python3 scripts/run_cases.py --plan plan.json --out your-app/.evalup/reports/"$run_id"
python3 scripts/gate.py your-app/.evalup/reports --latest --mode regression
```

`validate_cases.py` stops the job on an error, among them a gating case or
canary covering a business rule no human confirmed. `gate.py` exits `0`
open, `1` closed, `2` bad input, and prints a one-screen
summary naming what failed. For a release pipeline add `--require-canaries`:
the gate then also closes when a canary did not pass or the run has none.
For an app whose answers vary, add `--baseline your-app/.evalup/reports/baseline.json`:
the gate then closes on a regression (a case that passed every trial in the
baseline now fails, a failing case the baseline never scored, or a failure
on a layer the case never failed there) and lists
the cases that were flaky or already failing there without gating on them.
`--min-pass-rate 0.9` adds an absolute floor; it counts `gating: false`
cases too, those on an unconfirmed rule included. The full headless recipe, including a
Claude-driven run with a tokenless gate step, is in
`skills/run/references/ci-gate.md`.

#### CI workflow template (GitHub Actions)

[`ci/github-actions.yml`](ci/github-actions.yml) is those five commands as a
workflow. Copy it to `.github/workflows/evalup.yml` in your app's repository
and edit the lines marked `EDIT`:

- **`EVALUP_STATE_DIR`**: where `.evalup/` lives, relative to the repo root.
- **The `start the app` step**: your app's setup and start command, run in
  the background; add its toolchain steps before it. **`APP_HEALTH_URL`** is
  what the next step polls until the app answers (`APP_READY_TIMEOUT_S`).
- **The job's `env:`**: one line for every `${NAME}` in `adapter.yaml`, with
  secrets from the repository's Actions secrets (`${{ secrets.NAME }}`).
  `run_cases.py` resolves them from the environment and never writes a
  resolved value to disk. evalup itself needs no model key: this runs the
  deterministic layers only, and judged verdicts never gate.

It checks out your app, fetches the plugin at **`EVALUP_REF`**, a release
tag (`v0.1.0`; an immutable release keeps its tag on one commit; a commit SHA also works),
installs PyYAML for `convert_suite.py` alone (everything else is stdlib, on
Python 3.9 or later), validates the suite, runs a `regression` run at `EVALUP_K` (default 3),
gates it, and uploads the run directory as the `evalup-run` artifact. Under
`data.may_contain_pii` that artifact holds raw prompts and answers: shorten
its retention or drop the step.

A validator error, or a run that aborts (a canary failed, an infra abort,
an incomplete run, a scorer error), already fails the `run the suite` step
with that script's exit code, and the gate does not run. Otherwise the `gate` step fails the job on exit `1` (closed, every reason listed in
the job log and summary) or `2` (bad input). It closes on any gating
failure, a run that scored nothing or fails `--verify`, an infra rate above
`EVALUP_MAX_INFRA_RATE` (default 0.05), a pass rate below
`EVALUP_MIN_PASS_RATE` (empty: no floor), and, with
`EVALUP_REQUIRE_CANARIES: "true"`, a canary that did not pass. With
`EVALUP_BASELINE: auto` and a `reports/baseline.json` in the repository it
gates `--baseline`: only a regression closes, and flaky or already-failing
cases are listed. That needs the pinned run on the runner, so commit
`reports/baseline.json` **and** the run directory it names (`pin_baseline.py`
writes the pointer). Without one, every gating failure closes (a notice
says so). A baseline of another dataset or scoring version is exit 2:
re-pin after bumping either. Under `data.may_contain_pii`, `reports/*/cases/`
is git-ignored, so a committed baseline cannot verify: set
`EVALUP_BASELINE: off`.

This repository's CI runs the template's own steps against
`examples/quickstart` on every push (`tests/run_template.py`): no baseline,
then a pinned one, then a broken case that must close the gate.

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
| `/evalup:discover [path-or-url] [--diff]` | Profiles the app; writes the adapter and profile, business rules (from the prompts and in-repo docs, with sources) included; patches instrumentation; `--diff` after a refactor |
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

A release check is two runs: `--regression`, then `--holdout`.

Other `run` flags: `--tag <component>`, `--filter-failing`, `--layer X`,
`--k N`, `--single-turn`. `scripts/pin_baseline.py` pins a finished run as
`reports/baseline.json`.

---

## What gets measured

Each layer isolates one part of the app, so a failure names the prompt to fix.

| Layer | Checks | Scored by |
|---|---|---|
| **Routing** | Accuracy, per-target F1 (macro and micro), confusion matrix, out-of-scope leakage | Script |
| **Tool use** | Selection precision/recall, argument correctness, trajectory subset/any-of/order match, forbidden tools, loop detection | Script (needs traces) |
| **Answer** | Must-contain, regex, JSON format | Script |
| **Execution accuracy** | Result-set comparison (order-insensitive, float-tolerant) and scalar quasi-exact match against a read-only oracle — catches the confident-but-wrong answer | Script |
| **Authorization / scope** | Forbidden tools, out-of-scope record IDs, scoped refusal — read from the tool-call log, never the chat text | Script |
| **Reliability & ops** | pass@k vs pass^k (the gap is flakiness), crash rate, latency and token cost per stage | Script (cost needs traces) |

Every run records a manifest — dataset version, app git SHA, model and judge
versions. Diffs between incomparable runs are refused.

---

## Limits

- **Conversations are scripted, Phase 1 only.** A case can be a live
  conversation (`input.turns`): the user's turns are scripted, the app's real
  replies are the history, and the run stops at the first failed turn. It runs
  only when the adapter declares `invocation.conversation`, and is skipped
  otherwise. Not built: a simulated user, client-sent history, pre-written
  assistant turns, and whole-conversation end-state checks
  (`docs/multi-turn.md` §9). A suite made only of skipped cases closes the
  gate. WebSocket apps are not driven, and a CLI only through
  `invocation.mode: exec` (JSON in, JSON out); streaming (SSE) answers are
  read (`invocation.response_format: sse`), but no time-to-first-token is
  recorded.
  Phase 1 has passed its tests but has not yet been run against a live app.
- **Conversations are slow and look worse.** Every turn waits for trace
  quiescence (3 s or more when traces come from a store; none for
  `traces.source: response`), or `turn_delay_s` from the reply when that
  is longer.
  20 conversations × 5 turns at `k=3` take about an hour, so a headless run
  needs several `wait_run.py` waits of 540 s each. pass^k over every turn
  makes a conversation look worse by construction (four turns at 0.9 pass
  about 0.66 of the time), so `generate` writes conversations
  `gating: false` until a baseline exists, and `gate.py` prints the
  multi-turn pass rate beside the single-turn one.
- **Judged verdicts never gate (v1).** The judge pass after a run shows
  `provisional` verdicts with their quotes, or `calibrated` ones once
  `/evalup:analyze --label` has measured the rubric on the same judge model,
  but none of them is in a pass rate, `gate.py` or an optimize objective
  until a live calibration has been measured (#27).
- **Business rules are reported by id, script first.** discover records
  each rule with its quote, source, date and status; a case lists the
  rules its answer is checked against in `expect.answer.rules`. A rule a
  script can check carries a `check:` (string or regex entries the case
  copies, scored by the ordinary answer layer); one that needs a judge is a
  rubric criterion tagged `rule: <id>`. After the run, `rule_report.py`
  says which rules were checked and which passed or failed, with each
  rule's source. A scripted rule's failure is its case's failure, so it
  gates only through a `gating: true` case; a judged rule is provisional
  and gates nothing; an `inferred` rule is reported, never gated (a gating
  case or canary on a rule no human confirmed is a validator error, and
  `gate.py --min-pass-rate` still counts a `gating: false` case on one).
  `validate_cases.py --profile` reports a rule with no case as a gap.
- **Tool expectations are whole-app.** In a multi-agent app, a call made by the
  wrong sub-agent still satisfies `expect.tools`.
- **No retrieval/RAG scoring.**
- **No red-teaming.** The adversarial cases `generate` writes check that the
  app refuses what its own scope and permissions forbid, read from the
  tool-call log; evalup does not generate attacks. For jailbreaks, prompt
  injection and wider red-teaming, use promptfoo's red-team tools
  (`promptfoo redteam`, <https://www.promptfoo.dev/docs/red-team/>), which
  are built for it.
- **Runs are serial.** 100 cases × 3 repeats at 8 s a call is about 40 minutes.
- **Trace conventions move.** The OpenTelemetry `gen_ai.*` conventions are
  still Development status upstream, so `scripts/normalize_trace.py` tracks a
  moving spec.

---

## Running tests

From the plugin directory:

```bash
python3 -m unittest discover -s tests       # 1229 tests, ~4 min
ruff check --config ruff.toml .             # --config is required
uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q
```

The third command is the only conclusive check of the Python 3.9 floor. Expect
1209 passed and 20 skipped; add `--with pyyaml` to run the skipped twenty.

`python3 tests/run_shards.py` runs the first one as CI's four shards side by
side (~50 s) for a faster loop; the serial command stays the conclusive one.

`.github/workflows/ci.yml` runs the same checks on Linux (3.9/3.11/3.13) on
every push.

---

## Docs

| File | Covers |
|---|---|
| `examples/quickstart/README.md` | The worked example: what `discover`, `generate` and `run` write, and what a trace-less app can score |
| `docs/workflow.md` | Day 1 → steady state → production, and who does what |
| `docs/concepts.md` | Eval layers, staged rigor, ground truth, the judge |
| `skills/discover/references/adapter-contract.md` | The language-agnostic adapter spec (its `oracle:` block and result extraction are `oracle.md` beside it); `skills/discover/references/adapters/dotnet.md` is the first reference adapter (.NET) |
| `docs/runner-contract.md` | The plan and run-directory format `run_cases.py` reads and writes |
| `docs/rubric-format.md` | The decomposed-binary judge rubric and calibration |
| `docs/traces-jaeger.md` | Feeding `otlp-file` from a Jaeger/Tempo setup today, and the design note for a Jaeger client and header-based correlation |
| `docs/multi-turn.md` | Multi-turn conversations (Phase 1 built): the scripted-turn driver, how app sessions are kept, and why a conversation counts as one case |
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
