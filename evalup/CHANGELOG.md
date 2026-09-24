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

### Fixed — the 2026-09-21 audit

Six auditors and the first live headless session. Every item below was
reproduced before it was fixed, and each has a test.

- **`gate.py` could pass a run that measured nothing.** It opened on zero
  scored cases (a suite of multi-turn cases is skipped whole, so it gated green
  forever), read `"gating_failures": "10"` as 0, and trusted `results.json`'s
  own `missing_artifacts: []`. It now closes on zero scored cases and on any
  count that is not an integer, runs `run_cases.py --verify` over the run
  directory (`--no-verify` opts out, and the summary says so), and names the
  failing cases and layers.
- **A tool call with no name passed the forbidden-tool checks.** It matched no
  entry, so `score_authz.py` and `trajectory_match.py` reported `pass` with
  `unscorable: 0` over the call itself — reachable from a real trace whose tool
  name sat under a non-GenAI attribute. The shared loader refuses a nameless
  call (exit 2) and `normalize_trace.py` reports such a trace `incomplete`.
- **`score_answer.py`'s `json_schema`** skipped a malformed sub-schema and
  passed, iterated `required: "email"` by character and failed a correct
  answer, and crashed on `properties: "x"`. The schema's shape is now checked
  at every depth; all are exit 2.
- **A failed health check printed the resolved `base_url`**, credential
  included. It prints the `${VAR}` form, as every recording path already did.
- **`base_url` accepted `file://`.** http/https allowlist.
- **Every `regression` run spent a holdout look.** The seal check compared one
  tuple against both the mode and the *split name*, and `regression` selects
  the split named `full`; five CI runs forced a reseal without reading a sealed
  case. `runner-contract.md` §6 said the same and is corrected with it.
- **The review viewer was empty on a real run** — `verdict.json` carries no
  request, answer or expected/actual pair. Pointed at a run directory it now
  joins each verdict with its sibling artifacts, shows why each check failed,
  and leaves sealed holdout cases off the page.
- 17 of 19 scripts were not executable while the skills invoked them as bare
  paths. All are, and every invocation is written `python3 <path>`.

### Added — the 2026-09-21 audit

- **`scripts/make_plan.py`** (the 20th CLI) builds `plan.json` from
  `convert_suite.py`'s document: mode → selection, `k`, gate; every required
  key; `${VAR}` refs left unresolved; and the exact runner command. The plan
  was the last artifact a model assembled by hand — about half the first live
  session's 75 tool calls. It refuses `--mode full`: one plan carries one
  `selecting_split`, so release validation is a `regression` and a `holdout`
  run.
- **`allowed-tools` on every skill**, scoped to the plugin's own scripts, in
  both the quoted and unquoted spelling (a live session showed an unquoted-only
  rule matching nothing when the path holds a space). Never a blanket `Bash`.
- `.claude-plugin/marketplace.json` and a `LICENSE` at the repository root.
- A completion line on **stderr** from `run_cases.py`; stdout stays reserved
  for the error payload.

### Changed — the 2026-09-21 audit

- **`start` asks for real conversations first** and routes to `analyze
  --transcripts` when there are any; `analyze`'s description no longer says
  "use after runs" for the one branch that needs no setup.
- **The docs now say what a run does not score**: the judged layer and business
  rules have no scorer, in the runner or any skill, and multi-turn cases are
  skipped. The README says both up front.
- **`optimize` and `discover` revert through git, never from memory**: a dirty
  surface file stops the edit, the pre-edit blob is recorded, revert is `git
  checkout` verified against it.
- The judge and trace-analyzer agents treat everything they read as untrusted
  data; `trace-analyzer` lost `Bash`; the judge's "temperature 0" claim, which
  nothing can set for a subagent, is gone.

### Fixed — the 2026-09-24 prompt audit

A pass over everything the model reads — seven skills, three agents, eleven
reference files, `CONTRIBUTING.md` — for text written against an earlier state
of the plugin, and for claims the scripts no longer back. No scorer changed.

- **Skill text that described flags and behaviour the scripts do not have**:
  `k` is plan-level (`make_plan.py`'s per-mode default, `--k N` overrides),
  never per-case; `--k` under `--smoke` is refused, not ignored, and nothing
  warns at `k < 3`; `run_cases.py` has no `--baseline` flag; `routing_report.json`,
  `reliability.json` and `comparison.json` are conditional artifacts; the
  judge is launched by `analyze --label` only, never by a run;
  `convert_suite.py` writes `manifest.json` only when `dataset.yaml` exists,
  so `--manifest` is bracketed wherever a skill spells the validator out;
  `difficulty` earns no validator warning; the viewer's seal filter reads only
  `verdict.json`, so a broader `--glob` is what leaks.
- **`adapters/dotnet.md` taught an adapter the runner rejects**: `mode:
  function` with a .NET entrypoint, when function mode imports a Python
  callable. Both blocks are `mode: http` now. Its "no official Anthropic SDK
  for .NET" claim was stale too.
- **`adapter-contract.md` described a per-call tool-execution wrapper the
  runner does not have**; the section now says what the runner enforces (case
  skips) and what the app's own wrapper would.
- **`annotation-ux.md` sized the calibration pass at 25–50** where
  `score_agreement.py` requires ≥100 labelled pairs; its example ids used the
  `<unit>-<category>-<hash>` shape case-format.md forbids.
- **The headless CI recipe in `run-modes.md` could not work as written**
  (checked against Claude Code 2.1.281): `--output-format json` has no
  `system/init` event, so its own plugin-load guard rejected every healthy
  run; `--bare` skips installed plugins, so `/evalup:run` did not resolve
  without `--plugin-dir`, and it accepts only `ANTHROPIC_API_KEY`;
  `--permission-mode dontAsk` denies the report write. The recipe now uses
  `stream-json --verbose`, names the plugin, states the key, and runs under
  `acceptEdits`.
- **`adapters/dotnet.md` described Microsoft.Extensions.AI as it was in
  early 2025**: tool calls have been auto-spanned as `execute_tool` since
  9.5.0, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` has been
  honoured since 9.10.0, and the `UseOpenTelemetry` sample did not compile.
  Each claim now carries the version it applies from and the section says
  when it was checked (2026-09-24, against 10.10.0).

### Changed — the 2026-09-24 prompt audit

- Migration-relative phrasing ("no longer", "used to", "A FIELD now", "the old
  unqualified default", "the pre-existing flag", "A7-owned") and incident
  parentheticals are rewritten as the current rule; the reasoning stays where
  it was load-bearing.
- The judge agent keeps its rules and loses what was addressed to the
  operator or the reader: research figures and arXiv ids (rubric-format.md
  holds them), the description of how its verdicts are scored, the
  same-family paperwork that `analyze --label` owns. Its reasoning length is
  qualitative, not "two to four sentences".
- Scattered capitalised emphasis in `test-generator.md`, one "MANDATORY", one
  "non-negotiable" and one "MUST" dialled back to plain statements with their
  reasons beside them.
- `profile-schema.md` no longer calls `domains:` a "legacy alias — still
  read": no script reads it, or `route_targets:`.

### Removed — the 2026-09-24 prompt audit

- **The old-layout compatibility branch**: `docs/migrate-run-layout.md`,
  `start`'s route check for a `baselines/`/`runs/` sibling, `run`'s "Old
  layout" bullet, `run_cases.py`'s pre-flight `check_old_layout` (exit 3) and
  the contract's §4 step for it, plus the two tests that pinned them. The
  layout was produced once, by this plugin's July 2026 version in the
  field test archived in the initial commit and since deleted; the plugin
  has never been published, so no state dir can hold it.

### Fixed — the 2026-09-21 shape audit

A third audit asked whether the plugin's *shape* was right, and re-ran a live
headless first session (unseen app to a scored run in 4.5 minutes, three planted
flaws found). Its verdict was to keep the seven skills; these are the defects it
reproduced.

- **`start` could not load `discover` without a prompt, and headless that was a
  silent denial** — it carried on by reading the skill file by hand. A skill
  that carries its own `allowed-tools` needs approval to load; `start` and
  `help` now pre-approve the skills they route to with `Skill(evalup:<name>)`
  (shown live to work). Never `optimize`.
- **Every skill now says how to call the plugin**: Read for its files, one
  script per Bash call with the path written out. The live session `cd`-ed into
  the plugin, chained with `&&` and held the scripts path in a shell variable —
  none of which the pre-approval can match — and a denied call is now a stop,
  not something to work around.
- **`analyze` told the model to stage a filtered copy of a holdout run by
  hand**, contradicting its own reference: the viewer already drops sealed
  cases, and a hand-built glob is the one way past that filter.
- **"Quarantined from scored runs" was not true** — no script drops a
  quarantined case. `generate` and the generator agent now say what the
  validator enforces: `gating: false` until a human accepts it; it still runs.
- **`dataset.yaml` had no documented format** — the live session read
  `validate_cases.py` to find one. `generate` §4 now lists the keys the scripts
  read.
- **A report said "8 ran, 2 failed" beside `summary.n: 6`.** `run` §5 now says
  which count is which (graded, canaries, attempted).
- **`TestResume` raced its own kill** and failed under load: the derived files
  are rebuilt just after the `verdict.json` the test waits on.
- The README gained a Permissions section (what is pre-approved, why a prompt
  can return mid-session, what headless needs) and no longer implies a
  scripts-only run writes `report.md`.

### Changed — breaking, if you used this repo before 2026-09-20

- **The plugin was renamed `agent-eval` → `evalup`.** Commands are now
  `/evalup:*`; an app's state directory is `.evalup/`; schema ids are
  `evalup/…`; the version string is `evalup harness`; the state env var is
  `EVALUP_STATE`. **There is no automatic migration** — rename the state
  directory by hand.
- **Run output moved to a per-run layout**, `reports/<run-id>/`, replacing the
  older sibling `runs/` + `baselines/` directories. The check that halted
  `run` on an old state dir, and its migration doc, were removed on
  2026-09-24 (see Removed): nothing outside the deleted field-test archive
  ever used the old layout.
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
