# Changelog

Notable changes to **evalup**. Format follows [Keep a Changelog][kac];
versioning follows [SemVer][semver]. `0.x` means the interfaces may still
change — the case format, the adapter contract and the runner's CLI most of
all.

Every run's `manifest.yaml` records the harness (plugin) version and the
**scoring version**, and a run is only comparable to a baseline of the
**same scoring version**: that number is bumped only when a verdict's meaning
changes (a scorer, a default threshold, the runner's verdict rules), so a
release that changes no verdict leaves baselines comparable. A section below
says when a release bumps it.

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html
[0.1.0]: https://github.com/its-magdy/evalup/releases/tag/v0.1.0

## [Unreleased]

### Changed
- `convert_suite.py` without PyYAML re-runs itself under `uv run --no-project
  --with pyyaml` when `uv` is on PATH, so a stock macOS `python3` needs no
  install (#95's smaller fix). `--self-check` then reports `"via": "uv"`;
  `EVALUP_NO_UV=1` turns it off. No verdict changes.

## [0.1.0] — 2026-11-22

The first release, tagged `v0.1.0`. Pin it in CI with `EVALUP_REF: v0.1.0`
(`ci/github-actions.yml`). `SCORING_VERSION`, `JUDGE_VERSION` and every
state file's `schema_version` are 1. The long-form notes on each
pre-release wave (the September audits, the field and user tests, the
reviews) are in the git history: `git show v0.1.0:evalup/CHANGELOG.md`.

### Added

- **Seven skills** — `start` (guided wizard), `discover` (profile the app,
  write the adapter, patch instrumentation), `generate` (build the dataset),
  `run`, `analyze`, `optimize`, `help` — and three agents
  (`test-generator`, `judge`, `trace-analyzer`). App output is data, never
  instructions, in every skill.
- **One `evalup` command** (#41). `bin/evalup`, on the Bash tool's PATH
  while the plugin is enabled, runs `scripts/<name>.py` (`evalup run-cases`
  is `run_cases.py`; `evalup --help` lists them), and the skills
  pre-approve `Bash(evalup *)`. Claude Code only: claude.ai and Cowork
  don't install a plugin with a `bin/`, and commands run by hand or in CI
  use `python3 scripts/<name>.py`.
- **An execution engine.** `scripts/run_cases.py` runs a plan built by
  `make_plan.py` and writes a complete run directory; its spec is
  `docs/runner-contract.md`, which wins over any skill prose. `--verify`
  re-checks a run's required artifacts (exit 6 naming what is missing),
  `--dry-run` states the calls and waits a run will need, and `--probe`
  sends one request so discover can verify an authenticated app headless.
- **Reaching the app.** `invocation.mode` `http`, `function` or `exec` (a
  command per call, JSON on stdin and stdout). Streamed answers
  (`response_format: sse`, AG-UI or a data path), tool calls read off the
  response (`traces.source: response`) or an OTLP file, required headers
  (`invocation.headers`), and a declared rate budget the runner paces to
  (`invocation.rate_limit`).
- **Scripted multi-turn conversations** (Phase 1, `docs/multi-turn.md`):
  `input.turns` with `invocation.conversation` (`client-id`, `server-id`,
  `cookie`). Each turn is scored with the existing layers; a checkpoint can
  veto a conversation but never certify it, and one conversation is one case
  for every count and gate.
- **Layered deterministic scoring.** Per-case layers are `routing`,
  `tool_selection`, `trajectory`, `execution`, `authz`, `answer`, `rules`,
  `state` and `http`; `loops` and `reliability` roll up per run. A case is
  never carried to `pass` by `http` or `loops` alone. `cost_latency` is a
  capability, not a layer, and never gates (`score_cost.py`). 23 stdlib-only
  CLIs in `scripts/`.
- **A sealed holdout gate.** `scripts/stats.py` decides keep vs revert on an
  exact Beta posterior over the discordant pairs (keep at P ≥ **0.95**, the
  holdout at k = 3), with an exact sign test and McNemar's exact test beside
  it. `--noise-floor` turns a candidate that changed no more cases than two
  runs of the unedited app disagree on into "no evidence"; that pair is run
  once per seal and recorded in `reports/holdout-seal.json`.
  `--candidate-looks` prints the multi-look false-keep bound. Every holdout
  look is counted in a `.jsonl` ledger; a reseal archives and empties it.
  Holdout runs never use or become `reports/baseline.json`.
- **A CI gate.** `gate.py` fails closed; `--baseline <run>` excuses a case
  that was already flaky or failing in the baseline, `--min-pass-rate` sets
  an absolute floor, and `--require-canaries` closes on a canary with no
  verdict. A run where every case got one 5xx or one 4xx is flagged
  (`repeated_5xx`, `uniform_4xx`), never rescored. `ci/github-actions.yml`
  is a workflow template to copy.
- **A run-time judge.** `scripts/judge_run.py` grades `form: criteria`
  rubrics after a run into `judge/judged.json` (`unjudged`, `provisional`,
  `calibrated`), with a grader self-test. Nothing on the decision path reads
  it. `score_agreement.py` computes per-rubric TPR/TNR/κ, and the runner
  derives the judged gate from that sidecar, never from a hand-set flag.
- **Business knowledge.** Discover records the prompt's and the repo's
  rules in `profile.yaml` `business_rules`, each with a verbatim `quote`, a
  `source` and a human's `confirmed_by`. Cases name what they cover
  (`covers`), and `expect.answer.rules` binds a rule to a script check or a
  judged criterion; `rule_report.py` reports verdicts by rule id and decides
  nothing.
- **Labels and baselines.** `label_fixes:` in `datasets/dataset.yaml` and
  `generate/references/label-lifecycle.md` for a wrong accepted label;
  `pin_baseline.py` writes `reports/baseline.json`. `run_history.py` builds
  a comparable series; only `status: "ok"` runs join it, and it never says
  "better".
- **Versioning.** Runs record `scoring_version`, the comparability key,
  bumped only when a verdict's meaning changes; `harness_version` is
  provenance, so a release that changes no verdict leaves baselines
  comparable. Every state file declares `schema_version`, and the validator
  names a migration as a WARN `--strict` does not promote.
- **A worked example**, `examples/quickstart/`: inputs committed, its run
  generated against a live HTTP server on every test run.
- Python **3.9** floor, stdlib only, no runtime dependencies
  (`convert_suite.py` imports PyYAML lazily). Checked at three depths — see
  `CONTRIBUTING.md`.

### Changed — if you tracked this repository before the release

- **Renamed `agent-eval` → `evalup`** (2026-09-20). Commands are
  `/evalup:*`; an app's state directory is `.evalup/`; schema ids are
  `evalup/…`; the version string is `evalup harness`; the state env var is
  `EVALUP_STATE`. **There is no automatic migration** — rename the state
  directory by hand.
- **Run output is per run**, `reports/<run-id>/`, replacing the older
  sibling `runs/` + `baselines/` directories.
- **`split` is a case field, not a directory.** Suites that kept
  `datasets/{full,holdout,smoke}/` need the field added.
- **`judge.status: calibrated` is necessary but not sufficient** — a
  `paths.judge_calibration` sidecar must back it, or cases come back
  `unjudged` with a reason.
- **Two stages, `early` and `stable`** (#42). The old names keep working:
  `pre-stability` reads as `early`, `traffic` and `production` as `stable`.
  `validate_cases.py --profile` prints a WARN `stage_renamed` naming the new
  value (`--strict` leaves it a WARN); change the line by hand or re-run
  `/evalup:discover`. A value that is neither old nor new is a WARN
  `unknown_stage` and reads as `early`. Each stage gates what its old name
  did, so no verdict moves.
- **Cross-skill paths are `${CLAUDE_PLUGIN_ROOT}/…`**; a skill's own
  `references/` stay relative, and app/state paths stay bare.

### Removed

- **The `full` run mode** (#42). A release check is `run --regression`,
  then `run --holdout`; `make_plan.py --mode full` and a kept `plan.json`
  with `mode: full` exit 2 saying so (`_common.FULL_MODE_REMOVED`). The
  split named `full` is unchanged, and a recorded run whose `mode` says
  `full` still reads.
- **`report.html`** and `scripts/md_to_html.py` (#21). `report.md` is the
  run's report; `build_review_viewer.py` is the shareable HTML page.

### Reserved

`expect.state`, `seed_state`, `available_tools`, `excluded_tools` and
`difficulty` are accepted and **warned about**: nothing scores them yet.
Streaming/TTFT and cached-token pricing are likewise specified but not built;
so are multi-turn's later phases (`docs/multi-turn.md` §9). Each waits for the scorer that would read it — see
`CONTRIBUTING.md`.

### Known limits

- The plugin is not listed in a public plugin marketplace; install it from a
  clone of the tag (README §Installation).
- RAG/retrieval scoring is deliberately out of scope for 0.1.0.
