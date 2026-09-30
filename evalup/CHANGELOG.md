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
  rather than resume across this change; the harness version is unchanged
  because 0.1.0 is unreleased.
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

### Added — the canary question (2026-09-30)

- **`gate.py --require-canaries`**, opt-in: closes the gate when any canary
  did not pass or the run carries none ("no verified canary, no pass"). For a
  release pipeline; a holdout run selects the `holdout` split, where
  canaries do not normally live, so expect the flag to close that gate.
- **`make_plan.py --layer X` names each canary it leaves with no enabled
  layer**, on stderr and in the summary's `notes`: that canary will read
  `unscored` and the gate will count it not verified.

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
