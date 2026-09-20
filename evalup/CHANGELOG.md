# Changelog

Notable changes to **evalup**. Format follows [Keep a Changelog][kac];
versioning follows [SemVer][semver]. `0.x` means the interfaces may still
change — the case format, the adapter contract and the runner's CLI most of
all.

The harness version is recorded in every run's `manifest.yaml`, and a run is
only comparable to a baseline of the **same** version: a different harness
version changes what a verdict means. Expect a version bump to invalidate
baselines.

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html

## [0.1.0] — unreleased

Nothing has been published yet, so there is no release to upgrade *from*. This
entry describes what 0.1.0 contains, plus the pre-release changes that would
break anyone who has been tracking this repository directly.

### Added

- **Seven skills** — `start` (guided wizard), `discover` (profile the app,
  write the adapter, patch instrumentation), `generate` (build the dataset),
  `run`, `analyze`, `optimize`, `help`.
- **An execution engine.** `scripts/run_cases.py` runs a plan and writes a
  complete run directory. Its spec is `docs/runner-contract.md`, and the
  contract wins over any skill prose. Before this, execution was
  hand-orchestrated by the model on every run — not reproducible, and it could
  silently drop required outputs.
- **Layered deterministic scoring.** Per-case layers are `routing`,
  `tool_selection`, `trajectory`, `execution`, `authz`, `answer`, `rules`,
  `state` and `http`; `loops` and `reliability` roll up per run.
  `cost_latency` is a capability, not a layer, and never gates. Seventeen
  stdlib-only CLIs in `scripts/`.
- **A sealed statistical gate.** `scripts/stats.py` decides keep vs revert on
  an exact Beta posterior over the discordant pairs — one rule at every n — and
  reports an exact one-sided sign test and McNemar's exact test alongside it.
  No chi-square or normal approximation appears in any decision or reported
  test; the power figures below the decision are the one acknowledged
  exception, and are labelled as planning estimates.
- **A completeness check.** `run_cases.py --verify` re-asserts a run's required
  artifacts over any run directory, writes nothing, and exits 6 naming what is
  missing.
- **Run history.** `scripts/run_history.py` builds a comparable time series;
  only `status: "ok"` runs join it, and it never says "better".
- **A judge calibration loop.** `scripts/score_agreement.py` computes per-rubric
  TPR/TNR/κ; the runner derives the judged gate from that sidecar rather than
  trusting a hand-set flag.
- **A worked end-to-end example** — `examples/quickstart/`. Its inputs are
  committed so you can read them before installing anything; its *run* is
  generated against a live HTTP server on every test run, so it can never go
  stale.
- Python **3.9** floor, stdlib only, no runtime dependencies. Checked at three
  depths — see `CONTRIBUTING.md`.

### Changed — breaking, if you used this repo before 2026-09-20

- **The plugin was renamed `agent-eval` → `evalup`.** Commands are now
  `/evalup:*`; an app's state directory is `.evalup/`; schema ids are
  `evalup/…`; the version string is `evalup harness`; the state env var is
  `EVALUP_STATE`. **There is no automatic migration** — rename the state
  directory by hand.
- **Run output moved to a per-run layout**, `reports/<run-id>/`, replacing the
  older sibling `runs/` + `baselines/` directories. `run` halts rather than
  misreading an old state dir; `docs/migrate-run-layout.md` is the migration.
- **`split` is a case field, not a directory.** Suites that kept
  `datasets/{full,holdout,smoke}/` need the field added.
- **`judge.status: calibrated` is necessary but no longer sufficient** — a
  `paths.judge_calibration` sidecar must back it, or cases come back
  `unjudged` with a reason.
- **An `http` check no longer carries a case to `pass`.** A case whose every
  layer is `unscored` now rolls up `unscored`, never `pass` and never `fail`.
- **Cross-skill paths are `${CLAUDE_PLUGIN_ROOT}/…`**; a skill's own
  `references/` stay relative, and app/state paths stay bare.

### Reserved

`expect.state`, `seed_state`, `available_tools`, `excluded_tools` and
`difficulty` are accepted and **warned about**: nothing scores them yet.
Multi-turn, streaming/TTFT and cached-token pricing are likewise specified but
not built. Each waits for the scorer that would read it — see
`CONTRIBUTING.md`.

### Known limits

- `.github/workflows/ci.yml` **has never executed** — this repository has no
  git remote. It is a specification. The checks in `CONTRIBUTING.md` are the
  only verified claim.
- RAG/retrieval scoring is deliberately out of scope for 0.1.0.
