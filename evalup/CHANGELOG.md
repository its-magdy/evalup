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

### Fixed — the 2026-09-20 audit

Security and correctness, each with a regression test:

- **XSS in `md_to_html.py`.** The `javascript:` filter was a three-scheme
  denylist behind `^\s*`; a leading control byte (`\x01javascript:`) walked
  past it and browsers strip that byte. Now an allowlist (`http`, `https`,
  `mailto`, relative), and any URL carrying a control or invisible character is
  refused.
- **Secrets written to disk.** Only headers were redacted, so `${API_KEY}`
  inside `request_body` landed verbatim in every `cases/<id>/request.json`.
  Recorded requests (body, url, manifest, `--dry-run`) now carry the `${NAME}`
  reference back.
- **Path traversal through a case id.** `../../X` or an absolute id wrote files
  outside `--out`. Ids are now validated as directory names by both
  `validate_cases.py` (`unsafe_id`) and `run_cases.py` (exit 2, nothing
  written).
- **`agents/judge.md` had no parseable frontmatter** — the block had been
  reflowed into a paragraph, so the judge agent never registered.
  `tests/test_plugin_layout.py` now checks every skill and agent file.
- **`score_cost.py` priced unusable token counts as $0.00** at
  `status: priced` (a negative count as negative dollars). Dollars are now
  withheld with a reason; an integral float (`1000.0`) is accepted as the count
  it spells.
- **`score_authz.py --id-pattern` had no ReDoS bound.** The regex watchdog
  moved to `_common.run_bounded` and covers both scorers (and no longer raises
  off the main thread).
- **Non-finite numbers.** `NaN`/`Infinity` in any JSON input is exit 2; the
  review viewer renders a run that holds one instead of a blank page. Deeply
  nested JSON and over-long integers are exit 2, not tracebacks.
- The runner honors `Retry-After` on 429/503 (capped at 60s); the review viewer
  recognizes a run directory without `--glob`; `start` no longer cites a
  non-existent `stage: invariant` and now routes an interrupted run to
  `--resume`.

### Added — the 2026-09-20 audit

- **`scripts/gate.py`** — the CI gate as a script: exit 0 open / 1 closed, a
  one-screen summary, every closing reason listed. The runner exits 0 on a red
  suite by design; until this, so did everything else.
- **`scripts/convert_suite.py`** — YAML → JSON for the whole state directory,
  rejecting duplicate YAML keys. The model no longer transcribes a suite by
  hand. The only script that imports outside the stdlib (PyYAML, lazily).
- **`examples/quickstart/demo.py`** — the worked example, runnable by a human
  in one command (`--break` shows a red run).
- **`/evalup:analyze --transcripts <path>`** — error analysis over real
  conversations with no profile, dataset or run.

### Changed — the 2026-09-20 audit

- **The first session is lean.** `/evalup:start` on a new app now runs a lean
  `discover` (no source patches, no interview), ~12 cases and a smoke run in
  one sitting, says up front what the app can and cannot have measured, and
  ends with a menu of what to unlock. The full setup is unchanged and one
  request away.
- **`/evalup:optimize` is user-invoked only** (`disable-model-invocation`).
- Skills read their arguments explicitly (`$ARGUMENTS`) rather than relying on
  the harness's fallback.
- The README leads with requirements, a 30-second demo and three commands;
  maintainer toolchain notes moved to `CONTRIBUTING.md`.

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
