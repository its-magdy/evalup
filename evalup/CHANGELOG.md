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

## [0.1.0] — 2026-11-22

The first release, tagged `v0.1.0`. There is no earlier release to upgrade
*from*. This entry describes what 0.1.0 contains, plus the pre-release changes
that would break anyone who has been tracking this repository directly. Pin
it in CI with `EVALUP_REF: v0.1.0` (`ci/github-actions.yml`).

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
- **A sealed holdout gate.** `scripts/stats.py` decides keep vs revert on
  an exact Beta posterior over the discordant pairs — one rule at every n — and
  reports an exact one-sided sign test and McNemar's exact test alongside it.
  The keep bar is posterior P ≥ **0.95** (it was 0.8, which kept a no-op edit
  on an app with flaky cases on 6–20% of holdout looks; #36), the holdout runs
  at k = 3, `--noise-floor` turns a candidate that changed no more cases than
  two champion runs disagree on — or any case those runs do not cover — into
  "no evidence", and `--candidate-looks` prints the multi-look false-keep
  bound (3% per look with the floor, 6% without; simulated, the test pins
  both) with `multi_look_assumptions`: the bound assumes independent flaky
  cases and the champion the noise pair measured, and a pinned exact
  counter-example (6 of 20 cases flipping together: 12.5% per look) shows
  what correlation does. The noise pair is run on the **unedited** app,
  before a seal's first edit (it used to run after the edit was applied,
  making the candidate its own baseline), once per seal, after a check that
  at least three looks are left, and recorded in `reports/holdout-seal.json`
  after each run, so a later or interrupted session reuses it instead of
  spending more looks. A reseal archives and empties the ledger: its count
  is a line count, and bumping `dataset_version` alone left the new seal
  with no looks. Holdout runs never use or become `reports/baseline.json`,
  and the runner refuses a holdout/non-holdout baseline pair before the
  look is spent (they share no case id). The runner's `comparison.json` says
  `noise_floor_applied: false`: it is never the optimizer's decision. A keep
  claims a posterior probability, never significance unless the sign test
  also passes.
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

### Fixed — the 2026-09-24 field test

A first run on a real ASP.NET Core app (five planted flaws, nine headless
sessions). Every item was re-derived from the session transcripts before it
was changed; the runner and scorers are untouched.

- **Headless `run` and `start` ended the turn while `run_cases.py` ran in the
  background**, and the CLI's exit killed the runner mid-case, twice. `run`
  now launches the runner in the foreground with the Bash timeout at its
  maximum and, past the ten-minute headless cap, loops on the new
  `scripts/wait_run.py` (exit 0 finalized / 3 still running / 4 the runner is
  gone, offer `--resume`); nothing is reported from a `running` run.
- **The headless permission surface was never named.** README §Permissions
  gives the recipe (`acceptEdits`, `--permission-prompts none`,
  `Bash(git -C *)`, `Bash(curl *)`), the same rules for `settings.json`, and
  the three facts that decide whether a headless run finishes (compound
  commands need every part allowed; `git -C` is not read-only; the plugin
  directory is not a working directory). Every skill preamble now applies the
  one-plain-command / stop-on-denial rule to every shell call, not only script
  calls — sessions had retried a denied shape up to four times.
- **`start` and `discover` read files beside the app** — owner notes, a
  sibling handoff, the tester's issue log. Both now read and list only under
  the app path, its state location and the plugin.
- **`make_plan.py` could not set `execution.insecure_tls`**, so sessions
  hand-edited `plan.json` for a self-signed dev host. It is `--insecure-tls`
  now; `run`, the adapter contract and `adapters/dotnet.md` name it.
- **No session offered an out-of-tree state location** on a dirty, read-only
  worktree. `discover` offers it before the first write when the tree is dirty
  or `repo_access: read-only`; `start` says where state goes up front;
  `profile-schema.md` points at `repo_access`/`state_location`.
- **The CI-gate recipe passed its own guard on an authentication failure**
  and `gate.py` then blamed `reports/`. The recipe checks the session's exit
  code and `result.is_error` first, closes stdin, uses
  `--permission-prompts none`, and asserts the init event names the plugin.
- **`help` promised that Jaeger unlocks the tool-use layers**; the contract
  reads `traces.source: otlp-file` only. A routing row for the question, and
  the README and `concepts.md` say traces come from a file export.
- **`generate` never covered discover's live-observed findings.** `findings.md`
  is the grid's third input: a case per finding with a symptom, or a
  `known_gaps` entry.
- **A headless `generate` left nothing gating and did not say so.**
  `validate_cases.py` warns `nothing_gates` at suite level; `generate` §3 has
  the no-reviewer rule and the closing sentence ("N cases, 0 accepted: this
  suite cannot fail a build until a human accepts cases").
- **Docs:** the plan's `infra_rate_abort` (0.25) and the gate's
  `--max-infra-rate` (0.05) are two thresholds by design, with the
  throttled-provider answer; the headless recipe is for a terminal or CI, since
  an auto-mode Claude Code session may refuse to spawn it.

### Added — the 2026-09-24 field test

- **`scripts/wait_run.py`** (the 21st CLI) — see above.
- **`evals/`** — five `claude plugin eval` cases, one per behaviour the field
  test found weakest: `help-matches-contract`, `run-ci-gate-auth-check` (no
  app), and `start-headless-completes`, `discover-stays-in-app-path`,
  `generate-no-self-review`, which scaffold the quickstart app in the run's
  workspace. The app-bound three need a Bash grant on the command line.

### Fixed — the 2026-09-25 user test

A first-time-user session on a real ASP.NET Core app (RefApp, trace-less,
a free-tier model behind it) wrote 42 findings. The engine held: no scorer
or runner semantics changed. What did:

- **`make_plan.py --manifest-extra` replaced the computed `app` block**, so
  passing `app: {model: …}` — the shape the contract shows — pinned the
  baseline to a run with no git SHA. Objects now merge one level deep and
  the computed `git_sha`, `git_tree` and `dataset_version` always win (a
  caller value is reported and ignored). (F-032)
- **`gate.py` said `PASS` over 2 of 4 failing** when none gated. The verdict
  word is unchanged — nothing gating failed — but the same line now says
  `(2 failure(s) on non-gating cases, not counted)`. (F-024)
- **No flag set the retry schedule**, so a throttled provider got the
  runner's 1 s / 4 s retries or a hand-edited plan. `make_plan.py
  --max-attempts N --backoff-s 5,30`; either flag implies the other. (F-027)
- **`validate_cases.py` learned four vacuity patterns** the session hit:
  `echo_assertion` (a `must_contain` the case's own input satisfies — the
  app passed one by echoing the request), `canary_not_in_smoke` (a canary
  tagged `[full, canary]` never runs under `--smoke`, since plans select on
  the mode's split and the runner refuses a case without it),
  `filter_unattested` (a `filter:` block nobody computed), and — with the
  new `--adapter <adapter.json>` — `clarify_unobservable` and
  `route_not_observable` (under `route_from_status` the observed route is
  the map's value, never a domain). The three skills that lint pass
  `--adapter`; `case-format.md` and the runner contract §5.2 say what a
  status-routed app can expect. (F-028, F-025, F-016, F-022, F-015)
- **The trace-analyzer graded the app against itself**: it read the
  profile's `verified: true` on a 400 as "correct" and called the case
  wrong. Its brief now says `verified` means observed, discover's
  `findings.md` is the SHOULD source, and a "label wrong" verdict cites
  one; `analyze` names every state file by path and clusters trace-less
  failures mechanically before launching an agent. (F-036, F-035)
- **Two labeling rules read as contradictory** ("never derive from the
  app's behaviour" vs "probe before labeling"). Stated once: a probe grounds
  the data of an expectation, never the behaviour. (F-021)
- **"Verified core entries" was defined nowhere.** It is `architecture.kind`,
  the invocation shape and one live route or tool; the rest may stay
  inferred after a lean first session. The no-reviewer `gating: false` rule
  is now told to the generator agent, not applied after it. (F-013, F-019)
- **README and `help` said `discover` "sets up the file exporter"**; it
  never did. Both now say the user places a collector with a file exporter
  and discover records the declaration; `docs/traces-jaeger.md` holds the
  fan-out recipe and a design note (Jaeger `api_v3` client, runner-minted
  `traceparent` correlation, effort). The README names the .NET adapter's
  real path and optimize's five preconditions; the adapter contract says
  the answer path is one exact key and refusals assert the status.
  (F-040, F-007, F-042, F-039, F-026)
- **`normalize_trace.py` could not read what that exporter writes.** The
  collector's file exporter (`format: json`) emits one document per line,
  and the reader took one document: every such store failed with "Extra
  data: line 2". It reads JSON Lines now, each line through the same strict
  parser; a bad line is a data error naming the line. Found writing the
  recipe above; nothing in the trajectory's meaning changed. (F-040)

### Fixed — the 2026-09-26 user test (round 2)

A second first-time-user round on the same app (two sessions, 66 findings,
F-101…F-166) re-ran the 2026-09-25 fixes and found two runner defects that
gave wrong numbers. Each claim was reproduced or read in the source before a
change; fixes below are bugfixes inside the runner freeze.

- **A k>1 case with an infra repeat could read `pass`.** The pass^k fold
  knew only "all passed" and "some failed", so `[pass, infra_error]` kept
  repeat 1's `pass` while `[infra_error, pass]` kept its `infra_error` — one
  pair, two verdicts. `summary.infra_errors` hid the provider failure,
  `--verify` agreed (it recounts the same files), and a canary that missed a
  repeat read "canaries 1/1". The fold is now written into the contract
  (§5.5): any `fail` → `fail`; all `pass` → `pass`; else `infra_error`, then
  `infra_incomplete`, else `unscored`. The case-level files are the first
  repeat whose verdict is the case's. A test ports the session's stub-server
  reproduction and runs both orders. (F-158, F-139, F-146)
- **An app's own 5xx read as provider noise, and the contract contradicted
  itself about it.** §5's `http` row implied a declared `expect.http.status`
  is compared against a 5xx; §7 and the code file every exhausted 5xx as
  `infra_error` before the comparison runs. The verdict is unchanged —
  the session's guard-blocked injection (an app defect) and its Gemma outage
  returned byte-identical 500 bodies, so no rule on the response can separate
  them, and HTTP defines a 500 as a failure to answer. What is new: a case
  whose every attempt of every repeat got the same 5xx carries
  `repeated_5xx: {status, attempts}` in `verdict.json`, `summary.repeated_5xx`
  counts them (`--verify` recounts it when present), and `gate.py` prints
  "N infra case(s) got the same 5xx on every attempt: possibly the app's own
  error". `run/SKILL.md` §3 says to read that line before re-running or
  raising `--max-infra-rate`, both of which would hide it; the contract's
  `http` row now says what the code does. (F-165)
- **A run interrupted before these two fixes and `--resume`d after them**
  keeps its already-finished cases as written: their k>1 verdicts used the
  old fold, and they carry no `repeated_5xx`, so the summary undercounts it.
  `--verify` cannot see either (it recounts, it does not re-fold). Re-run
  rather than resume across this change; the harness version stayed 0.1.0
  because nothing had been released yet.
- **One run, four denominators.** `run_history.py` divided passes by
  `summary.n`, which counts skipped and infra cases, and printed 0.6 for a
  3-pass, 1-fail, 1-skipped run (and labelled `n` "cases_scored");
  `infra_rate` counted non-canary infra cases but divided by `attempted`,
  which counts canaries, so one infra case beside one canary read 50 %;
  `routing_report.json` counted canaries that `results.json` leaves out; and
  the contract never defined `n` or `attempted` (its example was impossible).
  `n` keeps its meaning (every non-canary case selected — nothing that reads
  it changes). What changed: the contract's §10 has one counts table;
  `pass_rate` and `gating_failure_rate` in `run_history.py` divide by
  `passes + failures`; `infra_rate` divides by the non-canary cases sent;
  canaries stay out of the routing rows (their own layer still scores);
  `summary.holdout` gains an aggregate `failures`, so `holdout_pass_rate` and
  `visible_pass_rate` use the same scored denominator (a run older than it
  reports neither); and `run/SKILL.md` §5 no longer calls `n` "the graded
  cases". `run_history.py` recomputes `infra_rate` from the counts, so a
  series mixing runs from either side of the change does not read the new
  denominator as drift; a rate over zero scored cases, or a holdout run
  older than `holdout.failures`, now says so instead of pointing at §6. The
  smaller denominator makes `infra_rate` equal or higher than before, so a
  run near `--max-infra-rate` can now close the gate. The mid-run abort
  keeps its own share (every case attempted, canaries in), and its message
  says so. (F-122, F-135, F-161)
- **The generator wrote `gating: true` on 6 of 12 cases** although the
  delegation said none may gate: its brief set `gating: false` only on
  quarantined cases, and that rule won. `test-generator` now emits
  `gating: false` on every case — a human flips it when accepting — and
  generate §3 has the skill reset, among the cases it just wrote, any whose
  `gating` is not `false`. `validate_cases.py` read an absent `gating` as
  false while the runner and case-format.md read it as true, so a pending
  case with no key gated unwarned and `nothing_gates` fired on a suite that
  could fail; it now reads the key as the runner does. analyze's promotion
  and `--mine` paths write `gating: false` too. The 2026-09-25 entry's F-019
  fix held only for the wording of the delegation. (F-119)
- **An attack case was authored, then skipped by every run, and nothing in
  between said so.** `validate_cases.py --adapter` warns
  `attack_category_will_skip` on an `adversarial-refusal` case while
  `environment.safe_to_attack` is not true (a test keeps its category list
  equal to the runner's). generate §2c reads the flag and tells the user;
  discover's interview asks the owner, and lean mode lists it as deferred.
  Relabelling the case to get past the gate is ruled out: the flag is the
  owner's. (F-124)
- **A finding read from code was presented as observed behaviour.** Discover
  §8 tags every claim about what the app does `[observed: …]` or
  `[from code: file:line]`, with the pipeline conditions a code reading
  needs — the finding in question described a guard a real request never
  reached. (F-163)
- **Status-routed routing numbers read as domain-routing accuracy.** The
  contract already said `route_from_status` measures answered-vs-refused;
  `profile-schema.md` (the `router_executor` and `routing` comments) and
  run §5 now say it where the number is written and reported (doc half of
  F-123: the scorer-side marker it proposed would cross the freeze). (F-123,
  F-110)
- **`optimize` never checked that its edit could reach the running app.** A
  new precondition: `app.repo_access: read-only` → recommendations only; a
  surface whose edit needs a rebuild or restart (compiled-in or embedded
  prompts) stops between edit and measure until the user confirms the
  running build carries it, and again after a revert — an unchanged binary
  would measure as "no gain" and could spend a holdout look. The adapter
  contract no longer promises a rebuild field that nothing carries; no field
  was added. (F-151, F-111)
- **Discover after a lean session had no resume path**, and its checklist
  never asked where the app logs its own turns. An existing profile without
  `--diff` now runs findings.md's `deferred:` list and updates the files in
  place; §3 records the app's turn log and how a response joins to it.
  (F-152, F-153)
- **`docs/rubric-format.md` needed guesses to author a first rubric.** One
  complete worked file, a defaults table, and the seven gaps a first author
  hit, each resolved against what the scripts and the judge agent read:
  `unknown` scores nothing and leaves agreement, ids are hyphenated and never
  end in `-v<n>` (that suffix is the pin `run_cases.py` strips), the node
  list is ordered, and fields no script reads are said to be conventions.
  (F-157)
- **Doc drift.** The adapter contract gains `correlation: none` (the
  runner's own spelling), says `view-only` means "the runner cannot read
  them", that `route_from_status` values are literal labels and 5xx is never
  mapped, and lists `prompts[].kind`. The Jaeger note cites runner-contract
  §4 item 4, adds the mandatory `health_check` to its recipe (and to help's
  row), makes its tag join require a runner-generated value, quotes Jaeger
  on the v1 API, and uses `unscorable`/`blocked_by`. The runner contract says
  `gating` is copied from the case. profile-schema says what a plain-integer
  id app does with `record_id_pattern`. The skills write `<work-dir>`, never
  `<tmp>`. README's headless sentence names its condition. run §3 says how
  to supply a missing env var without breaking the shell rule; §5 allows
  "redaction pass, 0 redactions"; analyze `--transcripts` says what to do
  with no reviewer and that promotion bumps `dataset_version`. (F-104,
  F-105, F-108, F-109, F-112–F-116, F-127, F-131, F-148, F-161; the doc
  halves of F-159 and F-162 — their `--env-file` and `redact.py` would be
  new runner/script features and were declined. F-161's `unjudged: "mode:
  targeted"` is the runner's deliberate reason and was left)
- **Two small script fixes.** A skipped case's `response.json` carries
  `sent: false` / `sent_at: null` as the contract says (F-126).
  `convert_suite.py -o` creates a missing parent directory, as its
  `--split-dir` already did (F-161).
- **A whole-branch review of the above** found the `record_id_pattern`
  advice first written here was wrong (score_authz.py requires the pattern to
  match each allowed id on its own, so a key-anchored pattern exits 2; plain
  integers leave it unset), and that `agents/judge.md` rule 6 read a
  first-placed `canary_guard` as making every case a refusal case — it now
  keys only on `expect.authz.expect_refusal`. `--verify` recounts
  `infra_rate` on runs that carry `repeated_5xx` (a hand-lowered rate had
  opened the gate); the validator no longer warns `gating_unreviewed` on a
  canary, which the runner never lets gate; `gate.py`'s holdout line divides
  by the scored holdout cases.

### Fixed — the 2026-09-30 refactor review

A review asking what was worth refactoring found little, and two runner
defects at one seam: between the capability matrix's names and the runner's.
Both were reproduced before a change and are bugfixes inside the runner
freeze.

- **`answer_quality: {enabled: false}` disabled nothing.** The matrix, the
  linter and every profile say `answer_quality`; the runner scores that one
  capability as three rows (`answer`, `rules`, `judged`) and looked each up
  under its own name. So the documented key switched nothing off — which is
  what `make_plan.py --layer X` writes for every other layer — and the answer
  layer went on deciding cases the linter had already called ungraded.
  `_common.MATRIX_KEY_OF_LAYER` is now the one translation: the runner's
  lookup goes through it, and `--layer` accepts a report row's name
  (`--layer answer` used to die as "not in the capability matrix"). `http`
  and `loops` have no matrix key and are never switched off; `--layer http`
  says so. Two consequences of the key now working: with `answer_quality`
  off, `rules` and `judged` read `unscorable` rather than `unscored` /
  `unjudged (…)`, and a canary asserting only a disabled layer rolls up
  `unscored` (the canary entries below are what the gate and `make_plan.py`
  say about that). Contract §5 said "looked up under the layer's own name"
  beside an example matrix keyed `answer_quality`; it now states the map.
- **A run exited 6 over an `actual.json` nothing wrote.** §9(b) requires the
  file of every sent case carrying `expect.result`, and the runner wrote it
  only for a live `execution` layer. One execution case whose app call failed
  every attempt, or any plan with `execution` disabled — so every `--layer X`
  plan over a suite with result cases, the shipped example included — ended
  "incomplete". The file is now written on both paths: the extracted result
  under a disabled layer, `{"missing": true, "reason": …}` naming the
  failure when the app call failed (contract §5.3). `--resume` does not
  repair a run that ended this way on the old code — adopted case
  directories are not rewritten — so re-run it. Found by a test that joins
  `make_plan.py --layer routing` to a real run of the example; each half had
  been tested and the seam had not.
- **`infra_rate` has one definition.** The runner's summary, its `--verify`
  recount and `run_history.py` each carried "infra ÷ (`n` − `skipped`)", and
  the one time it changed (F-161) it changed in three places.
  `_common.infra_rate` is that definition; precision stays the caller's
  (4 places in `results.json`, 6 in a history series like its other ratios).
  No number changes.
- **The contract said the `state` row is `unscored` "on every path"**, and
  the profile schema ships `state: {enabled: false}`, which makes it
  `unscorable` with the matrix's reserved reason — §5's own decision order.
  Both are non-pass and the runner is unchanged; the §5 table and the
  linter's reserved-field message now say what it does.

### Fixed — the canary question (2026-09-30)

"Should `gate.py` close when a canary did not pass?" had been open since the
round-2 user test. Answering it found a runner defect first.

- **`--resume` turned a canary abort into `ok`.** A canary `fail` aborts the
  run (exit 4, `aborted_canary`: "nothing else from this run is
  trustworthy"), but the abort was checked only after a case executed, and a
  resumed run adopts its finished cases without executing them. So `--resume`
  walked past the failed canary, ran the rest, finalized `ok` with exit 0,
  and `gate.py` opened on it. Reproduced. A resume now re-aborts before
  invoking anything (contract §8 step 4), and `--verify` reports `ok` beside
  a failed canary as a discrepancy, so the gate also closes on a run the old
  code left that way.
- **The gate line said "canaries 0/1" for a canary nobody got an answer
  about**, which read as a failed one. In a finished run it never is — a
  `fail` aborts — so over a run directory that verifies, the line now reads
  `canaries 1/2 pass; 1 NOT VERIFIED (infra, unscored or skipped …)`; under
  `--no-verify` or beside a verify failure it stays at the bare counts, which
  make no claim. The exit code is unchanged, by decision:
  a canary with no verdict says nothing about drift and sits outside
  `infra_rate`, so closing on it would close on provider noise, and "0/1
  closes" beside "no canaries at all opens" is not a rule.

- **Every `--resume` of a holdout run spent another look.** Contract §6
  says "`--resume` does not append again"; the ledger append never checked.
  A run resumed twice cost three of the five looks a reseal allows. The
  ledger now holds one row per run id. Found by the review of the resume
  fix above.

- **The linter sealed `holdout` against `full` only.** `[holdout, smoke]`
  linted clean and ran on every smoke run; `[holdout, canary]` ran as a
  canary, whose id the runner names when it aborts on it.
  `holdout_not_sealed` now fires for holdout beside any other split. It had
  no test; it has one per pairing.

- **A matrix entry under a report row's name was ignored silently.** A
  report shows rows `answer`, `rules`, `judged`, `http` and `loops`; the
  matrix has no such keys, so `answer: {enabled: false}` switched nothing
  off (before the 2026-09-30 key map it did, by accident). The linter now
  warns `not_a_matrix_key` and names the key that does the job.
- **`agents/judge.md` used `${CLAUDE_PLUGIN_ROOT}` in its frontmatter
  `description`**, where Claude Code does not document substitution (it
  does for the body). The description names the doc without the
  placeholder; a test keeps agent frontmatter free of it.

### Added — the canary question (2026-09-30)

- **`gate.py --require-canaries`**, opt-in: closes the gate when any canary
  did not pass or the run carries none ("no verified canary, no pass"). For a
  release pipeline; not for a holdout run, which selects the `holdout` split
  — the linter keeps it exclusive of `canary` — and so always closes.
- **`make_plan.py --layer X` names each canary it leaves with no enabled
  layer**, on stderr and in the summary's `notes`: that canary will read
  `unscored` and the gate will count it not verified.

### Fixed — the 2026-09-30 security review

- **XSS in `md_to_html.py` through a code span used as a link URL.**
  ``[t](`" onfocus="alert(1)" autofocus x="`)`` rendered a live event handler:
  code spans are stashed before links are parsed, so `safe_href` checked only
  the placeholder, and the unstash put the span's raw quotes inside
  `href="..."`. A URL holding a code span now stays literal text; code-span
  quotes are escaped; and the page carries a CSP that allows no script
  (`default-src 'none'; style-src 'unsafe-inline'`). Reachable from an app
  answer quoted into `report.md`. The rest of the whole-plugin review came
  back clean.

### Fixed — the 2026-09-30 code review

Nine of ten findings fixed, each verified against the code first and pinned by
a test that fails on the old runner.

- **A `loops` pass carried a case it does not grade.** Rollup rules 5-6
  excluded `http` but not `loops`, which a collected trace adds to every
  case. A traced rubric-only or `expect.state`-only case rolled up to `pass`,
  and an http-only case was `pass` or `unscored` depending on whether `loops`
  scored. Both rules now exclude `loops` (runner-contract §10). A `loops`
  `fail` still fails the case. The change can only turn `pass` into
  `unscored`.
- **`gate.py` counted a sealed gating failure twice.**
  `summary.gating_failures` already includes holdout cases (§10), and the
  gate added `summary.holdout.gating_failures` on top of it. One holdout
  failure read "1 fail (2 gating)" and gave two closing reasons. The gate now
  counts it once, notes how many of those failures are sealed, and closes
  when the holdout figure exceeds the total.
- **Sealed holdout ids leaked through `--verify`.** Its missing-artifact
  lines named cases by id. `finalize` writes those lines into `results.json`
  (`summary.missing_artifacts`) and `gate.py` prints them verbatim. Sealed
  cases now appear as `<a sealed holdout case>`.
- **A malformed reply killed the run.** urllib wraps only `OSError`, so
  `http.client`'s `IncompleteRead`, `BadStatusLine` and `LineTooLong`
  escaped `send_http` and the runner exited 1. They are now a
  `TransportError`: a body cut short counts as `connection`, the same as a
  crash, and a reply that is not HTTP counts as `transport`.
- **`traceparent-echo` was case-sensitive.** Go's net/http sends
  `Traceparent`, so pre-flight exited 3 even though the traces joined.
  RFC 9110 §5.1 and W3C Trace Context both require accepting any case.
- **A non-string trace id raised `TypeError`** (exit 1). This happened, for
  example, with a number at `response-field:`. Such an id is now treated as
  no trace id at all.
- **`invocation.result_pattern` had no regex watchdog.** It ran in the
  runner's own process, so `(a+)+$` against a near-miss hung the whole run.
  It now runs under `_common.run_bounded`, and a timeout counts as a missing
  actual.
- **`--resume` lost a crash on a non-representative repeat.** Only the
  case-level `response.json` was read, so with k > 1 the same artifacts gave
  two different `crash_rate`s. Resume now reads every repeat.
- **`unscorable_layers` listed row names such as `answer`** used as matrix
  keys. Those keys switch nothing, so the report said a layer went unscored
  when it had been scored.

Left as is: `collect_trace` copies the whole trace store into each case's
`trace.json`. It costs disk space and time, but no number is wrong. Filtering
it safely would mean parsing the unshimmed format that `mapping_shim`
exists to handle, and it would change what `normalize_trace.py`'s
`spans_in_file` diagnostic reports.

### Added — multi-turn conversations, Phase 1 (2026-10-06)

The runner freeze was lifted for this one feature. A single-turn case scores
exactly as before, and no scorer was rewritten. Spec: `docs/multi-turn.md`.

- **Scripted conversations.** A case can be `input.turns`: the user's turns
  are scripted, the app's real replies are the history, and each turn is
  scored with the existing layers (an earlier turn's `expect` is a
  checkpoint; `every_turn` is copied into every turn). The run stops at the
  first turn that rolls up `fail` or `infra_*` and continues past `unscored`.
  The case verdict is the rollup over the union of the turns' rows, keyed
  `t<n>.<layer>`, and a checkpoint can veto it but never certify it: a `pass`
  needs a graded `pass` on the final turn (a review proved the plain union
  passed a case whose final turn was `unscored`). One conversation is one
  case for every count and gate.
- **`invocation.conversation`** in the adapter (`client-id`, `server-id`
  with `session_from`, or `cookie`; `turn_delay_s`; `memory`) is the only
  switch. Without it a conversation is skipped. Checked at pre-flight.
- **Retries restart the whole conversation** with a fresh `<uuid>` and
  cookie jar: re-sending one turn into a session that may already have
  stored it would score the rest on a corrupted history. `repeated_5xx`
  records the turn.
- **Per-turn artifacts** in `cases/<id>/turns/<t>/` (and under
  `repeats/<n>/`); the case-level files are the deciding turn's. `--verify`
  checks the turn tree; `REQUIRED_IF` is keyed by path pattern.
- **Rows and summary.** `verdicts.jsonl` and `results.json` rows gain
  `multi_turn`, `turns_sent` and `failed_turn`; `summary.multi_turn` and
  `gate.py` show the conversations' pass rate beside the single-turn one;
  `results.json.memory` says when memory outlives a session.
- **Cost.** `score_cost.py` prices a conversation as the sum of its turns,
  and refuses and names a paired case whose `turns_sent` differ.
- **`make_plan.py`** writes `execution.max_turns` (default 12, `--max-turns`)
  for a selection holding a conversation, and `--single-turn` leaves them out
  (how `optimize` keeps them out of its inner loop; refused under holdout).
  The dry run counts app calls per turn and states a wait floor.
- **Validator rules** for conversations: shape, length (WARN above 8), the
  mutual exclusion with `input.messages`, no conversation canary, the
  final-turn gradedness message, `every_turn` collisions, and
  `conversation_undeclared` with `--adapter`.

### Changed — multi-turn conversations (2026-10-06)

- `multi_turn_case_reserved` is now `multi_turn_in_messages`, and its advice
  is to write the conversation as `input.turns`.
- `routing_results.jsonl` is required only by a single-turn routing row: a
  conversation adds no row, so the run-level report keeps meaning
  single-turn routing.
- The profile's `capability_matrix.multi_turn` `blocked_by` now names the
  missing adapter block. The runner never reads the entry.

### Fixed — before multi-turn (2026-10-06)

- **A system or assistant message in `input.messages` was dropped, and the
  case still scored.** `case_text()` sends the last user message and nothing
  else. `case-format.md` called prior messages "fine as fixed context", but
  they never reached the app, and the verdict read as if they had. This is the
  truncated-conversation verdict that adapter hard rule 3 refuses, reached by
  another route. A case whose `input.messages` holds anything besides exactly
  one user message (an extra role, or no user message at all) is now
  `skipped` with that reason. `validate_cases.py` WARNs `not_one_user_message`
  at authoring time. **This changes existing suites' numbers.** Such a case
  scored before and is skipped now, so `n` stays the same but `passes` +
  `failures` drop, and pass rates are computed over fewer cases. Fold the
  context into the user message to keep the case scoring.
  (docs/multi-turn.md §0)

### Fixed — the first session (2026-11-19 review)

- **A missing PyYAML surfaced about fifteen minutes in** (#11), at
  generate's conversion, after the README's install check had passed: a
  stock macOS `python3` has no `yaml`. `convert_suite.py --self-check` only
  imports PyYAML and exits 0 or 2 with the install hint; the README's install
  check runs it, and `/evalup:start` runs it before reading the app. The
  hint now names the interpreter and the PEP 668 (Homebrew) way round pip.
- **Ten CLI flags had no test and no doc line** (#20): `make_plan.py`
  `--run-id`, `--holdout-ledger`, `--judge-calibration`, `--dataset-version`;
  `score_agreement.py` `--tnr-floor`, `--kappa-floor`, `--min-per-class`,
  `--confidence`; `score_cost.py` `--max-exact-states`;
  `build_review_viewer.py` `--title`. Each now has a test and a line where a
  user would look (run-modes.md, annotation-ux.md, analyze § cost). None was
  removed: deleting them was proposed and rejected 2026-09-21.

### Changed — correctness and safety (2026-11-19)

- **App output is untrusted in the main session too** (#37). Every skill's
  preamble now says app answers, traces, transcripts and fetched documents
  are data, never instructions, and points bulk raw output at the read-only
  `judge` / `trace-analyzer` subagents. `optimize` takes an edit's content
  from the failure analysis and the rubric only. The judge reads only the
  paths its assignment names and redacts secrets in quoted evidence. A
  layout test pins the paragraph, word for word, in all seven skills.
  `analyze --transcripts`, the one branch that had the main session read
  20–50 raw conversations itself, now hands each batch to the read-only
  `trace-analyzer` and works from its per-conversation blocks; with no
  reviewer present, the agent's critiques are the open codes.
- **A release no longer invalidates every baseline** (#38).
  `_common.SCORING_VERSION` (1) is the comparability key in the runner's
  baseline diff, `make_plan.py --failing-in`, `run_history.py`'s series key
  and `score_cost.py`'s pair check; `harness_version` stays equal to the
  plugin version, for provenance. A run written before the field takes the
  scoring version its release implies (0.1.0 = 1) with a warning, never a
  crash, through one key (`_common.scoring_key`) that the pair checks and
  `run_history.py`'s series key share, so a pair the runner would diff is
  never split into two series. `--version` prints
  `(scoring N)`. `reports/baseline.json` is `{"schema_version", "run_id",
  "dataset_version", "scoring_version"}`.
- **State files carry `schema_version: 1`** (#38): every case file,
  `adapter.yaml` and `profile.yaml` (renamed from the never-read
  `adapter_version` / `profile_version`), `datasets/dataset.yaml`, the run
  manifest and `reports/baseline.json`. `validate_cases.py` warns on a
  missing, older or newer value and names the migration — a WARN that
  `--strict` does not promote. It gains `--profile`, and `convert_suite.py
  --split-dir` writes the `profile.json` it reads.
- **One scoring version per run, and no crash on a bad one** (#38, second
  review). `--resume` refused only a changed plan, so a run interrupted under
  one `SCORING_VERSION` adopted its verdicts, graded the rest under the next
  and wrote the new version into the manifest. It now refuses (exit 2) a
  manifest whose scoring key differs; a pre-field manifest that maps to the
  current version resumes, with a stderr line. A malformed version field
  (`scoring_version: true`, which compared equal to 1; a `harness_version`
  of `[]`, which raised TypeError in every caller) is never comparable: the
  runner refuses it as a baseline, and `run_history.py` excludes that run
  instead of aborting the report. A series holding a pre-field run now says
  so. `analyze`'s revalidation passes `--profile`, so an old profile gets
  its migration warning there too.

### Added — reach: streamed answers and tool calls from the response (2026-11-20)

From the first live-proof app (#27): a TypeScript/Cloudflare app whose agent
answers only over SSE in the AG-UI protocol and, by its own design decision
(a log budget and no personal data to third parties), emits no OTel. Before
this, every answer from it was `unscored` and every tool layer dark (#39).

- **`invocation.response_format: sse`** with `invocation.sse.protocol:
  ag-ui | data-path`. `scripts/_sse.py` (stdlib) decodes a
  `text/event-stream` reply into a JSON-shaped body — `text`, `tool_calls`
  with streamed arguments joined, `error`, `finished`, `interrupted`, token
  `usage`, `partial_text` — and `response_body` marks the answer in it, so
  everything downstream reads one more JSON body. A non-stream reply (a 403's
  JSON error) parses as before. `data-path` covers other JSON-frame streams
  with a declared text path and optional `[DONE]` sentinel. Half a
  declaration, or `sse` in function mode, is exit 3.
- **`traces.source: response`**: tool calls read off the response itself —
  the stream's `tool_calls`, or a list in a JSON body at
  `traces.tool_calls_path` (#39 option 1) — written as `gen_ai` spans that
  `normalize_trace.py` reads like any trace. No collector, correlation,
  health-check join or quiescence wait. Durations are null (none is
  measured); token usage comes from the stream when it reports any.
- **No scoring semantics changed** (`SCORING_VERSION` stays 1). A stream that
  reported a run error or stopped before its terminal event has no answer —
  `unscored`, with that reason, as any missing answer already was — and is
  `infra_incomplete` for the trace layers, as a trace that never arrived
  already was: an incomplete tool list must never read as "called nothing".
  The manifest records `invocation.response_format` and `sse`.

### Fixed — AG-UI usage over several model calls (2026-11-21)

- **Tokens from a multi-call agent were undercounted** (#46). `_sse.py` kept
  only the last `RUN_FINISHED`'s usage, but an AG-UI server running an agent
  loop emits one per model call: on a real TypeScript/Cloudflare app with a
  streaming tool agent, a stream with three of them recorded one `llm_calls`
  entry and one call's tokens. Usage is now summed over every `RUN_FINISHED`
  and `RUN_ERROR`, kept per event as the body's `run_usage`, and
  `traces.source: response` writes one chat span per entry, so the
  trajectory has one `llm_calls` entry per call. A terminal event without
  usage adds nothing (absent stays distinct from zero); only real ints
  count, as before.
- **Cache counts are carried, not priced.** AG-UI's `cachedInputTokens` and
  `cacheWriteInputTokens` (parts of `inputTokens`, never additions) ride
  into `usage` / `run_usage` and onto the chat span when reported.
  `score_cost.py` still does not read them; its `cache_accounting` limit now
  says where they are. No scoring semantics changed (`SCORING_VERSION`
  stays 1): the same counts are read, from every event instead of one.

### Fixed — zero-argument tool calls (2026-11-21)

- **A tool call with no arguments reads as `args: {}`, not
  `{"_unparsed": true}`** (#47). An AG-UI stream sends no `TOOL_CALL_ARGS`
  for one, so its joined argument string is empty, and `normalize_trace.py`
  failed `json.loads("")`. On the live-proof run every zero-argument call was
  recorded that way, and `score_args.py` calls `_unparsed` unscorable, so an
  args check on such a call could never pass. Fixed in the normalizer, the
  one place both trace sources meet, so an exporter that writes
  `gen_ai.tool.call.arguments` as `""` is covered too. An empty or
  whitespace-only string is `{}`; malformed JSON is still `_unparsed`, and a
  missing attribute is still `null`. No scorer changed (`SCORING_VERSION`
  stays 1).

### Fixed — the dry-run estimate on a rate-limited app (2026-11-21)

From the same live-proof run (#48), on an app limited to 20 messages per 10
minutes: the estimate was wrong in both directions.

- **`--dry-run` left retries out.** `app_calls_planned` is the no-retry
  floor, but 12 cases at the default `max_attempts: 3` can send 36 requests,
  and a 429 often counts against the limit too. The dry run now also prints
  `app_calls_max` (planned × `max_attempts`; a conversation's retry replays
  every turn), and `run` §2 names it when findings or the adapter record an
  app-side rate limit.
- **`wait_floor_s` counted a wait that never happens.** It added the 3 s
  trace-quiescence window per call for `traces.source: response`, which the
  runner never waits for; a 12-case run printed a 36 s floor. That source
  now adds no quiescence, only a conversation's `turn_delay_s`. No verdict
  changes.

### Added — generate: the prompt's checkable rules become cases (2026-11-21)

From the same live-proof run (#27): the app's prompt said "plain text only",
discover recorded that as a business rule with its source line, and one
case's answer started lines with `- ` in two of three regression runs and
passed, because no case checked the rule. Its other checkable rules got no
case either, while most cases asked about the one record a probe had
grounded (#52).

- **A rules row in the coverage grid** (`generate` §1). Every
  `business_rules` entry, and every rule the app's prompt states, whose
  breach is deterministic gets a case that fails on it, built from checks
  that already score: a `must_not_contain` regex for a formatting rule (on
  every case when it binds every answer), `must_contain` for a reply
  language or one answer per ambiguous candidate, `expect.tools.subset`
  (duplicate-aware) for a lookup that must run twice before "not found",
  `expect.result` for a stated count. `coverage_grid.rules` in
  `dataset.yaml` lists each rule's case ids or why it has none. A rule the
  prompt states is owner-stated, never "common sense", and the grid's cases
  spread over every tool rather than the one record a probe grounded.
- **Discover records the prompt's rules** under `business_rules`, each with
  `source:` its file:line. The test-generator's rule 8 writes the checks
  and reports the uncovered rules with their reason.
- **The quickstart plants one**: its fixture's `SYSTEM_PROMPT` says plain
  text only, `profile.yaml` records `plain-text`, and every case carries the
  regex. `tests/test_example.py` runs the example against the same app
  answering as a bulleted list: all six cases fail, on that check alone.
- **No scoring semantics changed** (`SCORING_VERSION` stays 1);
  `expect.answer.rules` stays unscored (#26). The first-session text stays
  inside its 25k-token budget: generate's SKILL.md paid for the row by
  trimming restatements, and the conversation authoring tips moved into
  `conversations.md`, which is read only when an app keeps conversations.

### Changed — optimize's preconditions and its cost of one iteration (2026-11-21)

From the first live-proof run (#27): `optimize` refused a 12-case suite
correctly, but its preconditions left the agent to guess what they meant (#54).

- **Two terms defined.** An *objective dimension* is a layer row that fails
  on the chosen cluster's cases in `verdicts.jsonl`, not the profile's
  matrix: a `must_not_contain` miss is the deterministic `answer` row even
  when the matrix says `judged: provisional`. A *usable* training case is
  human-accepted, gating and not a canary; the usable count adds every
  sealed case, since nobody may read one to accept it.
  `validate_cases.py` now prints the accepted count per split
  (`summary.usable_by_split`), so nobody counts it by hand.
- **A sealed-holdout precondition.** `generate` skips the holdout below 25
  cases, so a suite past the 50-case floor could still have none; the
  holdout now needs ≥ 20% of the usable count, and a seal with no looks left
  counts as none.
- **Each refusal names its unlock as a command** (`analyze --label`,
  `generate --layer … --count`, `analyze --unseal`, `discover`), and a test
  checks each flag against that skill's `argument-hint`.
- **An iteration is estimated before its first app call**: messages from the
  dry runs' `app_calls_planned` (R + 3H on a seal's first iteration, R + H
  after; `app_calls_max` for the worst case), and wall time. A rate limit recorded
  in findings or the adapter is checked against each run: the serial runner
  does not pace to one (a 429 gets `max_attempts` tries, each wait capped
  at 60 s), so a run that can send more calls inside a window than the
  limit allows would burn a holdout look on `infra_error`s — optimize
  refuses it until the limit is raised. The plain regression run a keep
  needs is dry-run and counted too. At 20 messages per 10 minutes, a 30 + 10-case suite at
  k = 3 (180 messages) is refused.
- **`--budget $N` says what meters it**: `score_cost.py`'s dollars × `k`,
  holdout runs charged per app call. With the cost capability off or unpriced
  nothing meters dollars, and the budget is taken as messages or iterations.
- **No scoring semantics changed** (`SCORING_VERSION` stays 1): the gate's
  statistics are untouched, and the usable count is a precondition only —
  `stats.py` still pairs every pass/fail case it is given.

### Added — reach: discover verifies an authenticated app headless (2026-11-21)

From the same live-proof app (#27), plugin-only: `/evalup:start`, `discover`
and `generate` all stopped at discover's live check, a shell `curl -H
"Cookie: $APP_TEST_COOKIE"`. Claude Code denies a command whose variables it
cannot check, allow rule or not, and the adapter contract puts every auth
value in an env var — so headless, no app needing auth got past step 1 (#49).

- **`run_cases.py --probe --adapter <state>/adapter.yaml --message "…"`**
  (runner-contract §1.1): one request through the run's own pre-flight and
  `invoke_once` — env refs, `cookie_env`/`token_env`, headers, body
  template, SSE decode, answer extraction, and with `traces.source:
  response` the tool calls. No plan, no health-check call, no retries, no
  writes, no scoring. It prints JSON (`ok`, `problems`, the redacted
  request, status, answer, tool calls, a body preview, `env_vars`) and exits
  0 when that verifies the adapter, 3 when it does not, in the same shape
  when pre-flight fails. Every resolved env value of four characters or
  more, and every auth value, is put back as `${NAME}` everywhere in the
  output — the app's own answer included, since an app may echo a header —
  and the body is scrubbed before its preview is cut, so neither the cut
  nor JSON escaping (also inside a tool result) can leave part of a
  secret. An auth or URL value holding a control character (CR, LF, tab)
  is exit 3 naming the variable: `http.client` would have quoted the value
  in its error. No outcome prints a traceback.
  The YAML goes through **`convert_suite.py --adapter-file`**, new, so the
  runner still parses no YAML. The tool-call read is now one helper
  (`response_tool_calls`) shared by a case's trace and the probe.
- **Discover step 4 is that script call**, pre-approved like the others;
  never `curl`. Its closing summary lists the probe's `env_vars` — the
  adapter's `base_url: ${APP_BASE_URL}` was first mentioned by `generate`.
  The README's headless recipe drops `Bash(curl *)`.
- **A denied optional read no longer ends a session.** Every skill's "a
  denial of any call is a stop" now reads: never retried in another form,
  a stop only if the step needs it (a denied optional `grep`: use Read or
  Grep, or skip it); the earlier "if a call is denied, stop" is about
  script calls. A multi-file `grep` the session could do
  without had ended a whole first session.
- Discover's re-run prose (an existing profile, `--diff`) moved to
  `references/rerun.md`, read only then: the first session's 25k-token
  budget had five tokens left.
- **No scoring semantics changed** (`SCORING_VERSION` stays 1): the probe
  scores nothing, and the shared helper returns exactly what
  `trace_from_response` computed before.

### Added — the run-time judge's design (2026-11-21)

`docs/judge-run.md`, the design half of #23, reviewed before any code.
Nothing is built: every judged layer is still `unjudged`.

- **Three states**: `unjudged`, `provisional` (shown with quoted evidence,
  never in `stats.py`, `gate.py` or an optimize objective) and `calibrated`
  (a calibration for the same rubric id, version and resolved model id). In
  v1 not even `calibrated` gates, until #27 measures a live calibration.
- **One judge call per case** over a flat list of yes/no criteria, behind a
  printed token estimate and a call budget; a two-answer **self-test** per
  rubric (#29 item 1); every quote checked against the file it names.
- **The seams**: a `judge/` directory beside the run (the runner's files
  are untouched), a new `judge_run.py`, calibration sidecar `/2` with the
  rubric version and model id, a judged comparability key, the quickstart's
  rubric case, the code PR's acceptance tests, and eight open questions
  with a recommended answer each.
- Pointers from `runner-contract.md` §5.4, `concepts.md` and
  `rubric-format.md`. **No scoring semantics changed** (`SCORING_VERSION`
  stays 1).

### Removed — report.html (2026-11-21)

- **`scripts/md_to_html.py` and its tests are gone** (#21, decided in #32).
  `run` no longer renders `report.html` beside `report.md`: two independent
  reviews recommended dropping it, the converter had already needed two XSS
  fixes (above), and the review viewer (`build_review_viewer.py`) remains
  the shareable HTML page. `report.md` is the run's written report. 20 CLIs
  remain in `scripts/`.
- **No scoring semantics changed** (`SCORING_VERSION` stays 1): the runner
  never produced or required `report.html`.

### Added — the label lifecycle after review, and a pin script (2026-11-21)

From the plugin-only live-proof run (#27): on a 12-case suite the dataset
went from version 1 to 4, with three hand-written pins, in about 40 minutes,
and every step after the first review was decided ad hoc (#53).

- **`scripts/pin_baseline.py <reports/<run-id>>`** writes
  `reports/baseline.json` from the run's manifest (`run_id`,
  `dataset_version`, `scoring_version`, `k`, `mode`, `pinned_at`),
  temp-then-rename, always beside the run so the pointer resolves. It
  refuses, exit 2 and nothing written, a run whose status is not `ok`
  (`running` included), one with scorer errors (exit 7), one that fails
  `run_cases.py --verify`, any mode but `regression` (a smoke pin hides
  every failure outside the smoke split from later diffs), and one where no
  case gates (`--allow-ungated` overrides that on the user's word). Its
  `notes` say when the gate is closed (gate.py's reasons), when the run used
  k=1 (every later diff needs `--k 1`), and which pointer it replaced. `run`,
  `analyze` and `optimize` no longer write the file by hand. 21 CLIs.
- **`label_fixes:` in `datasets/dataset.yaml`**: a proposed label fix waits
  there for the owner (`pending`, `approved`, `applied`, `rejected`)
  instead of in a past session's chat. `validate_cases.py --manifest`
  errors on a malformed entry, an open fix naming no case in the suite, and
  an applied one without `applied_in`. It lists the open ones as
  `summary.label_fixes_open`, a summary line rather than a WARN, so a
  `--strict` job does not fail while the owner decides.
- **`generate/references/label-lifecycle.md`**, read only after a review:
  what the case hash covers (the whole case, `notes` and `review`
  included) and that no script derives `dataset_version` from it; the bump
  rule (any edit but one to `notes` or `review` alone); correcting an
  accepted label (`review` stays accepted, a dated `notes` line, one bump
  per sitting); re-pinning after a bump, pinning a red run, and how `k`
  carries over. `run` §4 says each in one line and drops the `--baseline`
  flag no script has.
- **No scoring semantics changed** (`SCORING_VERSION` stays 1), and case
  hashing is unchanged: excluding `notes`/`review` from the hash would
  change every recorded `sha256`, so it is left open on the PR.

### Added — the run-time judge: provisional verdicts after a run (2026-11-21)

The build half of #23, per `docs/judge-run.md` (§19 lists the details the
design left open). The runner is unchanged and still Claude-free; the judge
is a pass after the run, in the run skill's turn.

- **`scripts/judge_run.py`** (`plan`, `record`, `merge`, `diff`), so 22 CLIs.
  `plan` selects the cases that pin a rubric and whose layer rows hold no
  `fail`, `error` or `infra_*` (`--include-failed` adds `fail`; holdout is
  never judged or named), snapshots each rubric's sha256, materialises its
  self-test, prints a call and token estimate and caps the pass at
  `--max-calls` (default 25, every launch counted). `record` takes the
  judge's message verbatim, keeps the last JSON object, and turns any
  criterion whose quote is not found in the file it names into `unknown`;
  a reply that does not match its assignment is `unjudged` whole. `merge`
  writes `judge/judged.json`: each case `unjudged`, `provisional` or
  `calibrated`, the resolved model id (one id across the pass, or every
  case `unjudged`), and the same-family warning. `diff` refuses two passes
  whose judge key (`_common.judge_key`), scoring key or dataset version
  differ.
- **The grader self-test** (#29 item 1): each `form: criteria` rubric ships a
  known-good and a known-bad answer, graded before any case; a judge that
  gets either wrong judges no case on that rubric.
- **The walls**: nothing the pass writes is read by `gate.py`, `stats.py`,
  `--verify`, `run_history.py` or optimize. A test runs the quickstart with
  a judged `fail` on a gating case (provisional, calibrated, and as a failed
  judge canary), and with a real `judge/` tree from plan, record and merge,
  and finds `gate.py`, `--verify`, `stats.py` and every runner file
  byte-identical. `run_history.py` and optimize are walled by not reading
  `judge/` (no test runs them against one).
- **The self-test's two answers are anonymous**: each gets a random label
  in its directory and `case_id`, and the good/bad mapping lives only in
  `plan.json`, so a judge cannot pass it by reading a name. A quote needs
  two letters or digits, a redaction stands for at most 4000 characters,
  and the match is linear in the file.
- **The criteria form** of a rubric (`rubric-format.md`): a flat list of
  yes/no questions plus `self_test`, parsed by `convert_suite.py`
  (`--rubrics-dir`, `rubrics.json`) and linted by `validate_cases.py
  --rubrics`, which also warns on a case pin with no file or a stale version.
- **Calibration sidecar `/2`**: `score_agreement.py` adds `rubric_version`
  and `judge_model_id` to the dedupe and group keys and writes a `groups`
  list; rows without them read as before. `run_cases.py` accepts `/1` and
  `/2`, its only change.
- **Skills and agent**: `run` gains `Agent(evalup:judge)` in
  `allowed-tools`, `--no-judge`, and `references/judge-pass.md` (read only
  when a case pins a rubric; the first-session budget went down by 38
  tokens); `analyze --label` pre-fills from `judged.json` and launches no
  second judge; `optimize` never reads it; `agents/judge.md` gains the
  criteria form and its reply contract.
- **The quickstart's `grounded-only` rule is judged**:
  `.evalup/rubrics/grounded-answer.md`, pinned by `c-4f2a91c7`, and
  `demo.py --judge-replies examples/quickstart/judge-replies` shows a
  provisional `pass` with its quotes, without Claude. The fixture app now
  returns the invoice count its answer states.
- **Checked live once, in part (2026-11-21)**: three real Opus judge calls
  (two self-test answers and `c-4f2a91c7`, each a subagent told to follow
  `agents/judge.md`) on a fresh quickstart run. Every reply met the reply
  contract and every quote was found; the self-test passed, the case came
  out a provisional `pass`, the self-reported model id resolved to
  `claude-opus-5-5`, and each launch cost ~29k tokens, inside the 17–35k
  estimate. One judge flagged `no_unsupported_claim` as ambiguous on whether
  a date counts as a figure: a v2 of the rubric should say. **Not checked**:
  the run skill driving the pass headless in one turn, through
  `Agent(evalup:judge)` (§16 test 13), which is #27's first step.
- **No scoring semantics changed** (`SCORING_VERSION` stays 1;
  `JUDGE_VERSION` 1 is the judge's own).

### Added — analyze and optimize: never-pass cases, rejected attempts, two suites (2026-11-21)

Three of #29's borrowed ideas, all skill text; no script changed.

- **A case that never passes is suspected first** (`analyze --cluster`).
  At k > 1 a `reliability.json` row with no passing repeat (a case verdict
  is pass^k there, so a flaky case fails every run), at k = 1 a
  `run_history.py --all-cases` row with a `fail` and no `pass`, or a
  `category` with failures and no pass — `skipped` and infra rows count as
  neither — is reported as its own "case or app?" group: is the expected
  route or tool real, could any correct answer satisfy the check? The user
  answers before the cluster reaches `optimize`.
- **Optimize keeps a rejected-attempts log**: every decision writes
  `candidates/<id>/outcome.md` beside the candidate's diff and
  `holdout-gate.json` — kept or reverted, the training
  `fixed_by_candidate`/`broken_by_candidate`, the gate's numbers when a look
  was spent, and what it rules out. Step 3 reads them all before proposing,
  so a reverted idea is not tried again without new evidence, and the
  session report lists what was ruled out.
- **Capability and regression suites are named** (`docs/concepts.md`) on
  the existing `gating` field: accepted `gating: true` cases are the
  regression suite, accepted `gating: false` ones the capability suite.
  `generate` lets a reviewer keep an accepted case `gating: false` for it.
  `analyze` suggests graduating a capability case that passed every run of
  its series (three at least); the user flips `gating`.
- **No scoring semantics changed** (`SCORING_VERSION` stays 1): each item
  reads numbers the scripts already write and changes no verdict.

### Added — reach: required headers, and a run on one 4xx is flagged (2026-11-21)

#50. On a live TypeScript/Cloudflare app the first live check got `403
Cross-origin request refused`: the adapter had no way to send an `Origin`
except by misusing `auth: {type: headers}`, and under `route_from_status:
{403: denied}` a run would have scored every case as a correct refusal.

- **`invocation.headers`**: `{name: value}` sent on every request beside the
  auth — each case, each turn, the health check and `--probe`. Pre-flight
  (exit 3) refuses a non-token name, an empty value, function mode, a name
  the runner, `auth` or `identity_map` already sets, and a credential-like
  name (`token`, `csrf`, `key`, …) with an inline value instead of a
  `${REF}`, or two names equal but for case. A `${REF}` value is redacted
  in `request.json`, like auth. A run's pre-flight now also refuses an env
  var feeding a header or the URL that holds a control character, as the
  probe did: `http.client` printed the value in a traceback.
- **Discover** looks for same-origin, Origin or CSRF middleware on the chat
  route and writes the header it checks (§3).
- **`--probe`** names a 4xx whose body says origin, CSRF or cross-site as a
  guard, and points at `invocation.headers` instead of "check auth".
- **A run where every sent case got the same 4xx on every request** is
  flagged, the `repeated_5xx` (F-165) way: `verdict.json` carries
  `same_4xx`, `summary.uniform_4xx` is `{status, cases}` (≥ 2 answered
  cases, canaries included; `infra_error` ones left out), `--verify`
  recounts it, the runner's closing line and `gate.py` print it, and a
  failing canary's abort message names its 4xx. Print-only in the gate: no verdict, reason
  or exit code changes, so `SCORING_VERSION` stays 1.

### Added — a baseline-relative gate (2026-11-21)

#40, items 1 and 4. At k = 3 a case passes only if all three repeats pass,
so a non-deterministic app with a hundred gating cases almost always has one
flaky case, and `gate.py` closed on it.

- **`gate.py --baseline <run>`** (repeatable; a run directory or the
  `reports/baseline.json` pointer): a gating failure closes the gate only
  when the case passed every pass/fail trial it had in the baseline runs
  (a trial is one repeat), or had none there — a new case fails closed. A
  case that was flaky (some trials passed) or already failing (none did)
  in the baseline is named, with its baseline count, and does not gate —
  unless it now fails a layer it failed in no baseline trial, which closes;
  the verdict line says how many were excused. Every other closing rule is
  unchanged. A baseline that did not finish clean, has another scoring or
  dataset version, fails `--verify`, or holds sealed holdout cases is exit 2,
  and so is a holdout run under `--baseline`. A baseline case resting on
  fewer than three trials is flagged: one lucky pass reads as reliable.
- **`gate.py --min-pass-rate R`**: an absolute floor on the summary's
  passes / (passes + failures), in either mode.
- **`docs/workflow.md`** no longer promises "absolute floor + statistical
  delta vs the rolling baseline"; it describes these two flags.

A gate policy over verdicts the runner already wrote: no verdict, the pass^k
fold and `SCORING_VERSION` (still 1) are unchanged.

### Removed — the `full` run mode (2026-11-21)

- **`--full` is gone** (#42, item 1). It was always two runs and never one
  plan: `make_plan.py` already refused it. Release validation is documented
  as what it was: `run --regression`, then `run --holdout`. The runner's
  `MODES` drop `full`; `make_plan.py --mode full` and a kept `plan.json` with
  `mode: full` both exit 2 with that migration (`_common.FULL_MODE_REMOVED`)
  rather than the generic unknown-mode error. The SPLIT named `full` is
  unchanged. `judge_run.py` still treats a run recorded under `mode: full`
  as sealed, so an old run never has holdout cases judged.
- **No scoring semantics changed** (`SCORING_VERSION` stays 1, no
  `schema_version` bump): no verdict depended on the mode and no file's
  layout changed. A recorded run's `mode` field may still say `full`
  (manifest, results, baseline, ledger rows); every reader keeps working,
  since only a value `make_plan.py` always refused is retired.

### Changed — tool checks on a first session, and `expect.tools.any_of` (2026-11-21)

#51. On a first session (`stage: pre-stability`) a tool agent got no tool
checks: `expect.tools` is graded only by the `trajectory` layer, which
discover turned off until `stage: stable`. That included `forbidden`, so the
"never call the write tool" guard was off exactly when the suite was newest.
In the live #27 run, enabling it found a real rule break the answer layer
had missed.

- **discover enables `trajectory` at every stage** (off only for
  `single_llm` or when no tool call is observable). The stage now holds back
  only tool **order**: `validate_cases.py --profile` ERRORs
  `order_before_stable` on `expect.tools.order` (top-level, `every_turn` or a
  checkpoint) while the profile's stage is not `stable`, `traffic` or
  `production`. Which tools were called does not churn the way a sequence
  does. The checks stay in `trajectory`; `tool_selection` still means
  arguments only, so its history series does not split.
- **discover no longer turns `authz` off for "no identity/persona config"**:
  `forbidden_tools` needs neither that nor result capture. The layer is on
  with the degradation named in `blocked_by`, and off only when no tool call
  is observable.
- **One spelling for "never call X"**: generate writes
  `expect.tools.forbidden`. `expect.authz.forbidden_tools` is the same check
  on another layer and is still scored, so existing suites keep working.
- **`expect.tools.any_of: [[a], [b]]`**: passes when one alternative is met.
  Each alternative is a non-empty multiset like `subset`; it is ANDed with
  `subset`, and `forbidden` still applies. A flat list, an empty
  alternative or `any_of` beside `order` is exit 2 in `trajectory_match.py`
  and an ERROR in the validator (`any_of_flat`, `bad_any_of`,
  `any_of_with_order`). Precision and recall come from the alternative the
  verdict used (the first one met, or the closest when none is), and
  `pr_alternative` names it. The runner's never-live-tool skip reads
  `any_of` too.
- generate's grid, discover's step 6, `profile-schema.md`, `case-format.md`
  and `docs/concepts.md` now say the above.

**Existing baselines need a re-pin.** Nothing changes for a case without
`any_of`: `tests/trajectory_golden.json` pins the matcher's previous output
over 264 inputs, byte for byte, so `SCORING_VERSION` stays 1. But a profile
that discover rewrites with `trajectory` and `authz` on grades more cases
than the baseline did, and `gate.py --baseline` closes on a case that now
fails a layer it never failed there. Run a regression on the new profile
and `pin_baseline.py` it before you gate against it.

### Changed — `turn_delay_s` runs from the previous reply (2026-11-21)

#17, item 2. The runner slept the whole `turn_delay_s` after the previous
turn's quiescence wait and scoring, so a 2 s delay on a traced app cost 5 s
or more per turn. It now sleeps only what is left of `turn_delay_s` since
the previous turn's response arrived; the gap after the reply is never
shorter than `turn_delay_s`, which is what the delay is for (async memory
writes). The dry run's `wait_floor_s` follows: a gap between turns is the
larger of the quiescence window and the delay, no longer their sum.
Timing only: no verdict, and `SCORING_VERSION` (still 1), changes.

### Added — reach: a declared rate budget the runner paces to (2026-11-21)

#76. An app that allows 20 messages per 10 minutes, and counts failed
requests against that, turned a long suite into `infra_error`s: the
runner's only defence was `Retry-After`, capped at 60 s, which cannot
outlast a 10-minute window.

- **`adapter.invocation.rate_limit`**: `min_interval_s`, `requests` with
  `window_s`, or both. A malformed block is exit 2 before any call.
- **The runner paces every app call to it**: the health check, every
  retry, and every turn of a restarted conversation. Each interval runs
  from when the earlier call finished, so the app never sees a shorter gap
  whatever the latency. A wait is a `pace` event in `run.log`. The
  manifest records the block under `invocation`.
- **`--dry-run`** reports `pace_floor_s` and `pace_max_s`, the least time
  the budget lets `app_calls_planned` / `app_calls_max` span; `run`'s
  time estimate uses it.
- **Per process:** a run started right after another, or a `--resume`,
  does not know the last run's calls; wait a window between them.
- **`discover`** fills the block in when it finds a limit; `optimize` no
  longer refuses a run that outpaces a limit the adapter declares.

With no block, nothing the runner sends or writes changes. Transport only:
no verdict, retry rule or `SCORING_VERSION` (still 1) changes.

### Added — a CI workflow template (2026-11-21)

#40, item 2. Running the gate in an app's CI meant cloning the plugin and
wiring four scripts by hand.

- **`ci/github-actions.yml`**, a GitHub Actions workflow to copy into the
  app's repository: it fetches the plugin at `EVALUP_REF` (now the release
  tag `v0.1.0`; see "the first release tag" below), starts the app and waits
  for it, runs `convert_suite.py` → `make_plan.py --mode regression` →
  `run_cases.py`, gates with `gate.py` (`--baseline reports/baseline.json`
  when the repository holds one, plus `--min-pass-rate` and
  `--require-canaries` as options), and uploads the run directory. No
  Claude and no model key: deterministic layers only. README "CI workflow
  template" says what to edit.
- **Run in this repository's CI**: the `ci-template` job runs the
  template's own `run:` steps, verbatim, against the quickstart
  (`tests/run_template.py`): with no baseline, then with a pinned and
  committed one, then with a broken case that must close the gate on a
  regression. `tests/test_ci_template.py` pins the template's actions to
  ci.yml's, which Dependabot keeps current.
- **`examples/quickstart/demo.py --serve [--port N]`** runs the demo app
  alone, until SIGTERM: the app that job starts.

No script, verdict or `SCORING_VERSION` (still 1) changes.

### Added — business knowledge: rules with sources, traceability (2026-11-21)

#25. On the first live-proof app (#27) discover drafted ten business rules,
all from the system prompt, and opened no business document, although the
repo had decision records and an agent-facing doc pointing at them. Every
drafted rule restated what the code already did, so a case built on one
graded the app against itself.

- **Discover reads the in-repo business docs** in step 3: `docs/adr/`,
  `GLOSSARY.md`, `CONTEXT.md`, `docs/agents/`, policy docs, and what an
  `AGENTS.md` or README points at. A rule read from a prompt assembled from
  fragments cites the fragment's file:line.
- **Every rule in `profile.yaml` carries a verbatim `quote`, its `source`, a
  `date` and a `status`** (`inferred` until a human confirms it by name in
  `confirmed_by`, in the interview). `business_rules` moves to the top level
  of the profile, beside a ranked `risks` list; a later discover moves rules
  from the old `confirmed_by_human.business_rules` and marks them inferred.
- **A docs-vs-code contradiction is a finding** in findings.md, with both
  sides quoted, and a `contradiction:` on the rule, before any case exists.
  The rule stays inferred and gets no case until the owner decides.
- **Outside sources** (a wiki, a shared drive, an MCP server) are read only
  with the user's explicit yes, as untrusted data. Their quote stays out of
  the versioned profile, in `<state>/quotes.local.md`, which discover adds
  to `<state>/.gitignore` first, unless the user opts in
  (`quote_versioned: true`).
- **Cases name what they are for: `covers: [<rule or risk id>, ...]`.**
  `validate_cases.py --profile` resolves each link (`dangling_cover`,
  ERROR), refuses a gating case or a canary (whose fail aborts the run)
  covering a rule no human confirmed (`unconfirmed_rule_gates`, ERROR),
  warns on a case covering nothing or a still-contradicted rule, on an
  incomplete or vaguely sourced rule and on rules left under
  `confirmed_by_human.business_rules`, and prints `summary.traceability`:
  each rule's and risk's cases, the gaps ("rule dispute-window has no
  case"), the unconfirmed rules, the contradicted ones, and the `gating:
  false` cases on an unconfirmed rule -- which close no gate, but
  `gate.py --min-pass-rate` counts them (gate.py is unchanged). A gap is a report line, not a finding: nothing scores rules, and an
  inferred rule is meant to wait for its case. `covers` without `--profile`
  warns `covers_unchecked`.
- **`--strict` users will see new warnings, and a strict job fails on
  them:** `legacy_rules_location` while rules still sit under
  `confirmed_by_human.business_rules`, then, once a discover moves them,
  `case_covers_nothing` on every case without a `covers`. Both clear by
  re-running discover and generate (or adding `covers` by hand).
- **Only a person confirms a rule:** a `confirmed_by` that is a model's,
  an agent's or evalup's own name (`claude`, `discover`, `gpt-4o`, whole
  words) is read as unconfirmed (`rule_confirmed_unattributed`), so a
  case on it cannot gate. An `http(s)://` source counts as outside the
  repo even without `outside: true`.
- **`convert_suite.py`'s combined document** carries `schema_version`,
  `business_rules` and `risks` under `profile`, and `--profile` unwraps
  it, so the one file serves `--cases`, `--capabilities`, `--profile` and
  `--rubrics`.
- **The deterministic CI pipeline validates before it plans:** README and
  `ci-gate.md` give five commands (convert, validate, plan, run, gate) as
  a `set -e` script; `demo.py` runs the same step and prints the gaps; the
  CI workflow template (#82) runs it in `run the suite` before the plan,
  and `tests/run_template.py` adds a fourth pass, a gating case on the
  quickstart's inferred rule, which must fail the job before any run.
  The template's commit pin at the time predated #25, so the validator only
  warned `schema_version_missing`; the `v0.1.0` pin includes this merge
  and enforces it.
- **generate, the test-generator and analyze** link every case, keep an
  unconfirmed rule out of the gate, and report the gaps; the trace-analyzer
  reads only confirmed rules as a SHOULD.
- **The commit policy, stated in the README and discover:** evalup never
  commits. `profile.yaml`, rule statuses included, is meant to be versioned
  with the app like the cases, so a fresh clone or a CI job running the
  validator refuses the same unconfirmed rules and a confirmation is a
  reviewable diff; an outside quote
  is not versioned.
- **The quickstart shows all of it:** prompt fragments one per line, each
  rule's quote checked against the line its source names, a small
  `docs/billing-policy.md` whose 30-day dispute window contradicts the
  prompt's 14 (findings.md F1, and the one gap), every case's `covers`, and
  a test that a gating case covering the inferred rule is refused.
  `--strict` stays at 0 errors, 0 warnings.

Rules are linked, never scored: `covers` reaches no scorer, the runner
never reads it, `expect.answer.rules` is still "not measured" (#26), and
`SCORING_VERSION` stays 1.

### Added — reach: `invocation.mode: exec`, a command per call (2026-11-21)

#39 option 2. An app that is a CLI, or that is hard to call over HTTP (an
auth wall, a local-only process, a non-Python app with no route), is
reached through a command the runner starts once per call: the rendered
`request_body` as JSON on stdin, one JSON object on stdout, read through
`response_body` exactly as an http body is.

- **`invocation.exec: {argv, cwd, env}`** (`scripts/_exec.py`). `argv` is a
  list, never a shell string, and a `${REF}` in it or in `cwd` is exit 3:
  argv is visible to every process. The child gets an env **allowlist** —
  a few base variables plus the names in `exec.env` (unset → exit 3,
  `missing_env`) — never the runner's whole environment. `cwd` is required
  and relative to the state location (`..` for state in `<app>/.evalup/`);
  `argv[0]` must resolve at pre-flight (exec's health check). Tool calls
  come from `traces.source: response`; a trace store is refused.
- **One process per call**, in its own process group, killed at
  `timeout_s` (enforced) and after it exits. A process that left the group
  (`setsid`) may outlive the call but cannot hang it: a call takes at most
  `timeout_s` + 2 s, and a stdout still held open then is no answer. No
  group kill on Windows (untested there). stdout is capped at 16 MiB;
  stderr's last 64 KiB are kept and a tail is quoted in errors, scrubbed
  (before any cut) of passed env values of 4+ characters in plain, JSON
  and `repr` form. The answer itself
  is recorded as printed, as an http body is.
- **No status from a command**: a success is 200 and a non-zero exit is
  always a crash, so a deliberate refusal is exit 0 with a JSON body read
  through `route_from_response`. `route_from_status` is refused in exec
  mode, and a case expecting a status other than 200 is skipped.
- A SIGTERM to the runner mid-call kills the call's group first and still
  ends the runner by SIGTERM; a SIGKILL orphans the call.
- **Failures are infra**, as http's transport failures are: a timeout; a
  non-zero exit (a crash, like a raising function entrypoint); a command
  that cannot start, or stdout that is empty, oversized, not JSON or not an
  object. Each is `infra_error`, retried like any.
- **Same choke points**: pacing, retries, `--dry-run`'s call counts,
  `--probe`, conversations (`client-id`, `server-id` with
  `session_from.body`) and `traces.source: response` (tool calls in the
  stdout object). `auth`, `headers`, `identity_map`, `health_check`,
  `response_format: sse` and the `cookie` style are refused in exec mode;
  a `token_env` credential goes in `exec.env` by name instead.
- **`cli` stays reserved** (exit 3), now pointing at `exec`: it was never
  specified, so there was no `cli` adapter to keep, and an alias would give
  one shape two names.
- `discover` detects it (step 3) and writes it from
  `references/adapters/exec.md`, read only for an exec app.

Transport only: no verdict or `SCORING_VERSION` (still 1) changes. Not yet
run against a live app (#27).

### Changed — two stages, `early` and `stable` (2026-11-22)

#42 item 2. `stage` had four values, `pre-stability | stable | traffic |
production`, and only one boundary meant anything: the validator refuses
`expect.tools.order` before `stable` (#51). No script reads `stage` for
anything else (gating goes through `capability_matrix`), and nothing was
built behind `traffic` or `production`. The stages are now **`early`** and
**`stable`**.

- **What each does is unchanged.** `early` is what `pre-stability` was;
  `stable` is what `stable`, `traffic` and `production` were. Trace mining
  (`analyze --mine`) needs a wired trace backend, which it always checked
  itself; it no longer names a stage.
- **Migration — existing `profile.yaml` files keep working.** The old names
  are read as the new ones: `pre-stability` → `early`, `traffic` and
  `production` → `stable`. `validate_cases.py --profile` prints a WARN
  `stage_renamed` naming the new value; like the `schema_version` findings,
  it is a migration and `--strict` leaves it a WARN. To clear it, change
  the line by hand (`stage: early` or `stage: stable`) or re-run
  `/evalup:discover`, which writes the new names. A value that is neither
  old nor new is a WARN `unknown_stage` and is read as `early`.
- **No scoring semantics changed** (`SCORING_VERSION` stays 1, no
  `schema_version` bump): every stage maps onto the one that gated the same
  checks, so no finding and no verdict moves.

### Added — business rules reported by id, script first (2026-11-22)

#26. The live evidence that permits it: on the first live-proof app (#27,
2026-11-20) discover recorded a "plain text, no bullets" rule with its
source, one case's answer broke it in 2 of 3 regression runs, and the case
passed, because nothing read the rule. #52 made generate write the check;
this makes a run say, by rule id, which rules it checked and which failed.

- **`expect.answer.rules` is wired up.** It lists the profile.yaml rules a
  case's answer is checked against; its ids count as `covers`, so the #25
  refusal of a gating case or canary on an unconfirmed rule reads them too.
- **Script first.** A rule a script can check carries a `check:` in
  profile.yaml: `must_contain` / `must_not_contain` entries, the grammar
  `score_answer.py` already scores. Every case listing the rule copies them
  verbatim; `validate_cases.py --profile` refuses one that does not
  (`rule_check_missing`). The runner's answer layer scores them like any
  entry: no runner or scorer change.
- **The judge for the rest.** A rule that needs interpretation is a `form:
  criteria` criterion tagged `rule: <id>` on the case's rubric, graded in
  the case's one judge call. The tag never reaches the judge's prompt (a
  test compares the prompts); `judged.json` carries each rubric's
  `criterion_rules` from the plan's snapshot. A rule's judged verdict is
  `provisional` (or `calibrated`) like every judged verdict, and gates
  nothing until a live calibration has been measured.
- **`scripts/rule_report.py`** (the 23rd CLI) reads a finished run and its
  `judge/judged.json`, and writes `rules.json`: per rule, its status,
  source and quote, each case's scripted result (with `failed_repeats`
  such as `2/3` at k > 1, turn by turn in a conversation), each judged
  criterion's state, verdict and quote, and the gating cases it failed.
  Sealed holdout cases are counts only. The run skill pastes its
  `summary` lines under **Business rules** in `report.md`.
- **What a rule may decide.** Only through its case: a scripted rule's
  `fail` is its case's `fail`, which closes the gate when the case is
  `gating: true`, and only a `confirmed` rule may sit on one. An `inferred`
  rule is reported, never gated. A judged rule never gates. Nothing on the
  decision path reads `rules.json`; a test runs the quickstart and finds
  `gate.py`, `--verify`, `stats.py` and every runner file byte-identical
  with it written, with a provisional and with a calibrated judged `fail`.
- **New validator findings:** `bad_answer_rules`, `bad_rule_check`,
  `rule_check_missing`, `rule_unbound` (a listed rule nothing on the case
  checks) as ERRORs; `rule_binding_unchecked` (no `--rubrics` to see a
  judged binding), `rule_judged_unlisted`, `criterion_rule_unknown` and
  `rules_unchecked` as WARNs. A rubric criterion's `rule:` must be one id,
  and one criterion per rule.
- **Quickstart.** Every case lists its rules; `plain-text` and `read-only`
  have a `check:`; `grounded-answer`'s `no_unsupported_claim` judges
  `grounded-only`. `demo.py` prints the rule report after every run, and
  `demo.py --break-rule` plants the violation: the app answers in bullets,
  and `plain-text` fails by id with its source (`tests/test_example.py:82`),
  its quote and the six failing cases.
- **No scoring semantics changed.** The runner's `rules` row still reads
  `unscored`, every verdict is byte-identical, and `SCORING_VERSION` and
  `JUDGE_VERSION` stay 1; no `schema_version` bump (`check:` and `rule:`
  are optional keys).

### Changed — the first release tag (2026-11-22)

- **The CI template pins a release tag.** `ci/github-actions.yml` fetches
  the plugin at `EVALUP_REF: v0.1.0` instead of a commit SHA. The repo's
  releases are immutable: while the release exists its tag stays on one
  commit, and the name cannot be reused. A SHA still works.
  `tests/test_ci_template.py` now requires the pin to be the tag of
  `plugin.json`'s version, so a version bump moves the pin in the same
  commit (#40, item 3).
- **README §Installation** clones the tag.

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
Streaming/TTFT and cached-token pricing are likewise specified but not built;
so are multi-turn's later phases (`docs/multi-turn.md` §9). Each waits for the scorer that would read it — see
`CONTRIBUTING.md`.

### Known limits

- The plugin is not listed in a public plugin marketplace; install it from a
  clone of the tag (README §Installation).
- RAG/retrieval scoring is deliberately out of scope for 0.1.0.
