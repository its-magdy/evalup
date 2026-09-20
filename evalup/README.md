# evalup

A Claude Code plugin that **evaluates and improves LLM chat/agent
applications** — any app it can invoke programmatically: simple chat,
router→executor, multi-agent orchestrators.

Claude Code does the intelligent work (profiling your app, generating test
cases, diagnosing failures, proposing prompt/tool-description fixes).
Deterministic Python scripts do the scoring, so numbers are cheap, fast, and
reproducible. OpenTelemetry traces are the evidence.

## Quick start

```
claude --plugin-dir ./evalup     # try locally, or install via marketplace
> /evalup:start path/to/your/app
```

The wizard takes it from there: it profiles your app, patches missing
instrumentation (with your approval), generates ~30 test cases, has you review
only the ~15 suspicious ones, and gives you your first scored run — typically
a routing confusion matrix that tells you something you didn't know — in
about an hour.

Want to see what all of that produces before you install anything?
**`examples/quickstart/`** is a complete, validated eval setup for a small
two-domain app — profile, adapter, six cases, the plan, and the exact list of
files a run writes. It is checked by `tests/test_example.py` on every test run,
so it conforms to the current rules rather than to the rules of the day it was
written.

Lost at any point: `/evalup:help`.

## Commands

| Command | What it does |
|---|---|
| `/evalup:start` | Guided wizard — detects where you are, does the next step |
| `/evalup:discover` | Profile the app; write adapter + profile; patch instrumentation; `--diff` after refactors |
| `/evalup:generate` | Build/extend the eval dataset (coverage grid, targeted review, splits) |
| `/evalup:run` | Execute + score all applicable layers; baseline diff. Modes: `--smoke` (fast subset) · `--regression` (full, pass^k) · `--targeted` (only what changed) · `--holdout` (sealed) · `--full` (release) |
| `/evalup:analyze` | Cluster failures, calibrate the judge (`--label`), mine traces (`--mine`); builds a hotkey trace/annotation viewer for open→axial error analysis |
| `/evalup:optimize` | Failure-driven prompt/tool-description improvement with statistical keep/revert |
| `/evalup:help` | Explain any of this |

## What gets measured

Layered, so a failure tells you *which prompt to fix*:

- **Routing** (router/multi-agent apps) — accuracy, per-target F1
  (macro+micro), confusion matrix, out-of-scope leakage (deterministic)
- **Tool use** — selection precision/recall, argument correctness,
  trajectory subset/order matching, forbidden-tool policy, loop detection
  (deterministic)
- **Answer quality** — must-contain/regex and JSON-format checks
  (deterministic), faithfulness to tool results, completeness, business
  rules (rules are deterministic; judged dimensions use a decomposed-binary,
  reference-guided judge, watermarked PROVISIONAL until calibrated)
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
and report, plus `reports/baseline.json` pointing at the pinned baseline
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

Python 3.9+, stdlib only. 3.9 is past upstream end-of-life (October 2025) and
is kept as the floor deliberately, not by default: RHEL 9 ships it with
vendor-backported fixes, and long-lived enterprise environments are where this
harness is meant to run. Run the suite any time with
`python3 -m unittest discover -s tests`; `scripts/stats.py --version` reports
the harness version a run manifest records.

The floor is checked at three depths, weakest to strongest:

```sh
python3 -m unittest discover -s tests   # grammar, on whatever interpreter you have
ruff check --config ruff.toml .         # target-version = py39 bounds every UP fix
uv run --python 3.9 --with pytest --with pytest-subtests \
    python -m pytest tests -q           # the real thing: stdlib APIs + runtime typing
```

Only the third is conclusive, and `CONTRIBUTING.md` is where these three live
authoritatively. `PY_FLOOR` in `tests/test_scorers.py` compiles the
scripts against the floor's *grammar*, and ruff's `target-version` stops `UP`
from proposing a 3.10+ form — but neither rejects a newer stdlib API or a typing
construct that only fails at runtime. `uv` fetches a real 3.9 in seconds, so
there is no reason to skip it before a release.

`ruff` and `uv` are development tools only — nothing the harness ships at
runtime imports outside the stdlib. `ruff.toml` keeps a deliberately small rule
set, and each entry carries a comment saying which defect class it catches or
which comment in the code already argues for it; read it there before adding to
it. `pyproject.toml` declares those dev tools and nothing else: it has no
`[project]` table, because this plugin is installed by copy from
`.claude-plugin/plugin.json`, never pip-installed, and the absent
`[project.dependencies]` list is the one slot a runtime dependency could
arrive through. Ruff's config deliberately stays in `ruff.toml` — note that
when both files sit side by side, `ruff.toml` wins and a `[tool.ruff]` table
in `pyproject.toml` is ignored silently.

`.tool-versions` pins **3.11.11**, above the 3.9 floor, on purpose: you edit on
a supported interpreter and check the floor with `uv`, which fetches a real 3.9
without making you install an end-of-life one. The file says so in a comment.

`.github/workflows/ci.yml` runs all three depths plus a `--help` check on a
real 3.9/3.11/3.13 matrix — **but it has never executed.** This repository has
no git remote, so nothing has ever evaluated that file; it is a specification
parked for the day one is added, and its header says so. Until then the
commands above are the only checked claim.

An app you can invoke programmatically. OpenTelemetry with GenAI spans is
strongly recommended (discover offers to add it) — without traces, trajectory
layers are unavailable and only answer-level evals run.
