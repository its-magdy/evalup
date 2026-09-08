---
name: run
description: >-
  Execute the eval dataset against the app and score every applicable layer,
  including execution accuracy and authorization. Produces per-case verdicts,
  a run manifest, a baseline diff with statistical honesty, and a report
  headlined by failure clusters. Five modes — smoke, regression, targeted,
  holdout, full (see [references/run-modes.md](references/run-modes.md)) — cover everyday edits through
  release validation, plus a tokenless CI gate.
argument-hint: "[--smoke|--regression|--targeted|--holdout|--full] [--tag <component>] [--filter-failing] [--layer X] [--k N] [--baseline]"
---

# Run — Execute and Score

Scripts in `${CLAUDE_PLUGIN_ROOT}/scripts/` do all deterministic work. You
orchestrate; you do not eyeball-score anything a script can score.

## 0. Run modes

Five modes select a case subset and a gate policy over the same execution
engine (§§1–4 below never change). Full mechanics, flags, and worked examples:
[references/run-modes.md](references/run-modes.md). Summary:

| Mode | Selection | k | Gate | When |
|---|---|---|---|---|
| `smoke` | cases tagged `smoke` in `split` | 1 | soft | every prompt/code edit (hook) |
| `regression` | full suite (cases tagged `full`) | ≥3, pass^k | hard | pre-merge / nightly |
| `targeted` | filter by component tag (+ `--filter-failing`) | per-case | soft | right after optimizing one surface |
| `holdout` | sealed `holdout`-tagged set | per-case | decision | optimizer's keep/revert step |
| `full` | everything, incl. judged layers | ≥3, pass^k | hard | release validation |

`--smoke` is the existing flag and is unchanged; the other four are new
flags mapped onto the same taxonomy. No
mode flag given → behave as `regression` (the old unqualified default: full
suite, hard gate).

**Selection is by the case's `split` field**, never by directory — splits are a
list on each case (`split: [full, smoke]`); one case is one file. See
`${CLAUDE_PLUGIN_ROOT}/skills/generate/references/case-format.md` for the
field, which owns this rule. `full` and `holdout` are mutually exclusive, so
the seal is a property of the case, not of where its bytes live.

## 1. Pre-flight (before any spend)
- Resolve adapter env vars; fail loudly if missing.
- Health check: one trivial request through the adapter. Then branch on the
  adapter's `traces.source`:
  - Traces declared available → verify a trace arrives and joins. Claimed
    available but no join → stop; do not run 50 cases to learn traces are broken.
  - Traces declared `none` or `view-only` → proceed in **trace-less mode**:
    score HTTP status, answer-level deterministic checks, business rules, and
    latency from wall-clock. Print once which layers are skipped (trajectory,
    tool selection/args, loops, per-stage cost/latency) and why.
  - `traces.convention` other than `gen_ai` → require `traces.mapping_shim`
    in adapter.yaml, run it on the health-check trace, and verify `gen_ai.*`
    keys appear before proceeding. No shim declared → trajectory layers off
    for the run (adapter hard rule 5); never feed raw non-`gen_ai` spans to
    `normalize_trace.py`.
- Safety check: environment kind vs tool side-effect classes (adapter hard
  rules). Refuse what must be refused, say why. This is also where the
  **authz layer's adversarial gate** is decided (see §3): note whether
  `environment.safe_to_attack` is true so the scoring step below knows
  whether adversarial `expect.authz` cases may run.
- **Cost/time estimate**: probe 2–3 cases, extrapolate (n × k × avg tokens ×
  prices + judge calls if calibrated). Present "≈ $X, ~Y min. Proceed?" unless
  the user pre-approved with an explicit budget. Skip the prompt entirely
  under the headless CI gate (§5) — a human isn't there to answer it.
- Generate the **run id** first — `<mode>-<UTC timestamp, YYYYMMDDTHHMMSSZ>`
  (e.g. `smoke-20260818T170500Z`) — it names `reports/<run-id>/` for every
  file this run writes (§2, §4). Then write the **run manifest** to
  `reports/<run-id>/manifest.yaml` with `run_id` as its own top-level key
  (not just embedded in the directory name — §5's CI gate reads it back off
  this field, not off the path), plus: mode, dataset_version + case id/hash
  list, app git SHA, prompt snapshot hashes, app model id, judge model +
  judge-prompt version, rubric versions, harness version, k, temperature,
  environment kind. A run without a manifest is not comparable to anything.
  Write it the same way whether or not a baseline exists yet — the first-run
  branch (§4) is a comparison-stage decision, not a pre-flight one.

## 2. Execute
- Serial by default; adapter's max_concurrency only when state-safe.
- Per case: (seed state if defined) → invoke via adapter (k times if k>1) →
  collect trace (respect completeness gate: quiescence, then max_wait →
  `infra_incomplete`) → write `reports/<run-id>/cases/<case-id>/{request,
  response,verdict}.json` **immediately, as that case completes, via
  write-to-temp-then-rename** — never batched at run end and never a partial
  file visible mid-write, since **resume** depends on it: an interrupted run
  continues from the last case-id with a fully-written directory under the
  same manifest.
  `request.json`: persona, headers actually sent (redact values per
  adapter.yaml `data.may_contain_pii`), the literal input message(s).
  `response.json`: raw HTTP status + body + `latency_s` (+ retry count if
  `infra_error` fired above). `verdict.json`: the per-layer verdict
  (http/execution/authz/answer/etc., §3) plus a free-text `notes` field for
  investigation context that doesn't fit a structured verdict.
  `trace.json` is written alongside them only when `traces.source` is
  `queryable` this run (§1 pre-flight); when it's `view-only` or `none`, no
  `trace.json` is written and `verdict.json` says why — so a reader can't
  mistake an absent trace file for nobody having looked.
  If `adapter.yaml` declares `data.may_contain_pii: true`, write
  `reports/.gitignore` containing `*/cases/` before the first case file of
  the run is written — raw per-case material stays local; `manifest.yaml`,
  `report.md`/`.html`, and `results.json` (§4) still commit normally.
- Failure taxonomy: timeouts/429/5xx/app-crash → `infra_error` (bounded
  retries, then recorded); crash on `noise`/`adversarial` categories also
  increments the **crash-rate metric** (a first-class number, especially
  pre-launch). Infra verdicts never enter pass/fail denominators.
- **Canaries every run**: the `canary` split
  (`${CLAUDE_PLUGIN_ROOT}/skills/generate/SKILL.md` §4 — ~10% of the full set,
  floor 2, stable versioned expected
  answers), minimum 1 known-good + 1 known-bad, run first. They are ordinary
  cases with ordinary ids, so they get ordinary `cases/<case-id>/` folders —
  but mark them `canary: true` in `verdict.json` and keep them out of the
  pass/fail denominators the report quotes: a canary measures the harness, not
  the app, and folding it into the app's score moves the number for the wrong
  reason. A canary scoring wrong → abort and flag harness/judge drift; nothing
  else from this run is trustworthy.

## 3. Score (cheap → expensive)
1. `normalize_trace.py` — raw spans → trajectory (+ transport conservation
   checks; missing spans/orphans → `infra_incomplete`. Absent result CONTENT
   is not loss — it only makes content-dependent checks unscorable). Its full
   output can be passed to the scorers as-is: they unwrap the `trajectory`
   key themselves, and a file without a `tool_calls` list is a loud exit-2
   error, never an empty trace.
2. Deterministic layers: `score_routing.py` (accuracy, per-target P/R/F1
   macro+micro, confusion matrix, OOS precision/recall, set-valued/clarify
   handling — build its JSONL rows by mapping case fields `route`→`expected`
   and `route_acceptable`→`acceptable` (both accepted as aliases), plus
   `observed`/`clarified` from the run; pass `--oos-route <name>` with the
   profile's `oos_handling` route so OOS metrics appear — the name must be a
   route label the selected cases actually carry, or the scorer exits 2 and
   lists the labels present, so omit the flag on a split with no out-of-scope
   cases; a null `observed`
   is scored as `__no_route__`, a fail, never a crash. `macro_f1` averages
   only over labels the dataset actually asks for; prediction-only labels
   the app invented — `__no_route__`, a hallucinated route name — are
   reported in a separate `spurious_labels` block and excluded from the
   macro, so one unrouted case cannot crater the headline number or
   misdirect the macro/micro gap warning. Surface `spurious_labels` in the
   report: a route the app emits that no case asks for is a finding),
   `trajectory_match.py` (subset default; order/exact where
   asserted; forbidden-tool violations; trajectory precision/recall),
   `score_args.py` (value/present/from_tool_result assertions; unscorable
   without content capture — reported, never failed; each tool's expectation
   is scoped by its `calls` key — `any` (default) / `all` / `first`, see
   `${CLAUDE_PLUGIN_ROOT}/skills/generate/references/case-format.md`),
   `detect_loops.py` (repeated tool+args-hash),
   `score_answer.py` (must_contain/must_not_contain — plain string or
   `/regex/` — and `format.json_schema`), latency/tokens per stage from the
   normalizer's per-agent usage rollup. Business-rule oracles and state-diff
   (if seeded) have no generic script — you evaluate them against the
   profile's rule definitions and the environment's snapshot scripts, and
   record per-rule verdicts in the run output.
3. **Execution layer** — every case carrying `expect.result` goes through
   `score_execution.py`: you produce both `actual` (extracted from the trace
   per the adapter's result-extraction contract) and `expected` (computed
   offline from the case's `reference_query` against the seeded fixture), and
   the scorer only compares them. No extractable result → `unscored`, never
   `fail`.
4. **Authz layer** — every case carrying `expect.authz` goes through
   `score_authz.py`, which grades the tool-call log and the record IDs
   actually returned — never an LLM read of the chat text, since a prompt-only
   "I can't share that" can hide an open endpoint underneath. Adversarial
   cases run only when `environment.safe_to_attack` is true (§1); otherwise
   `skipped` with the reason, never silently failed.

   Invocation mechanics for both — argument shapes, the unscorable states,
   `--id-pattern` for apps whose record ids aren't `letters[-_]digits`, and
   how each is reported: [references/scoring-layers.md](references/scoring-layers.md). Read it before
   calling either scorer; both are reported as their own layer in the
   per-case verdict and the run report, never folded into `answer` or
   `trajectory`.
5. Judged layers ONLY if `judge.status: calibrated` — via the `judge` agent,
   launched with the profile's `judge.model` as an explicit model override
   (the agent frontmatter is only the fallback default); record the model
   actually used in the manifest. One call per dimension, binary + reason;
   verdicts cached by (case-id,
   output-hash, rubric-version). Uncalibrated → dimensions reported `unjudged`
   with a visible "judge not calibrated — run /agent-eval:analyze to label"
   banner. `smoke`/`targeted` skip judged layers outright for speed/cost —
   report `unjudged (mode: <mode>)`, distinct from the uncalibrated banner so
   the two reasons for "no judged score this run" are never confused;
   `regression`/`full`/`holdout` run them when calibrated.

## 4. Compare and report
- **Baseline pointer**: `reports/baseline.json` — `{"run_id": ..., "dataset_version":
  ..., "harness_version": ...}` — is the one file that says which run is "the
  baseline"; every diff in this section reads it, never the newest-by-mtime
  `reports/` entry (mtime drifts once anyone re-runs a report or copies a
  directory). Pinning a baseline (first-run branch below, or an explicit
  `--baseline`) means overwriting this file, nothing else.
  Because the pointer holds no verdict data — the paired input `stats.py`
  reads lives in the run's own directory (report step below) — a
  `reports/<run-id>/` folder is self-contained: archiving or copying it
  carries everything needed to diff against that run. The same property makes
  the pinned baseline's folder load-bearing: **never prune `reports/` by age
  or size without checking `baseline.json` first**, since deleting the pinned
  run's directory leaves the pointer aimed at nothing and silently costs every
  future diff. If it is already gone, say so and pin a fresh baseline rather
  than quietly falling back to another run.
- **Old-layout check, before the first-run branch**: if
  `reports/baseline.json` is absent but a sibling `baselines/` or `runs/`
  directory exists, this state dir predates the per-run layout and *does* have
  a baseline. Stop and say so — "state dir uses the pre-`reports/<run-id>`
  layout; migrate with `${CLAUDE_PLUGIN_ROOT}/docs/migrate-run-layout.md`, or
  pass `--baseline` to deliberately pin this run and abandon the old one" —
  rather than falling through. Treating it as a first run would silently
  discard a real baseline and report "baseline established" over the top of it.
- **First-run branch** (`reports/baseline.json` doesn't exist yet, and no old
  layout was detected above): this is a branch, not an error — skip the diff,
  write `reports/baseline.json`
  pointing at this run (`--baseline` semantics are implied on a first run, no
  flag needed), and print "baseline established (run <id>); future runs diff
  against this." Every other step in §§1–3 runs exactly as normal; only the
  comparison step is short-circuited.
- Baseline diff (same dataset_version AND same harness version only — a
  scorer change alters what a number means just like a dataset change does.
  Refuse cross-version diffs; for a harness upgrade, tell the user to pin a
  fresh baseline; for dataset drift, suggest the comparable-core subset). Significance via `stats.py`, whose two paired
  inputs are `reports/<baseline-run-id>/verdicts_for_stats.jsonl` and this
  run's — the baseline pointer names the run-id and the run directory holds
  the file, so no verdict data ever lives beside the pointer:
  exact Bayesian P(improvement) on paired verdicts at every n, with an exact
  one-sided sign test reported alongside (feed it only `pass`/`fail` rows —
  infra and `unscored` verdicts are a hard error, never silent failures).
  It compares the two runs' shared cases, and reports what that cost:
  `cases_only_in_baseline` / `cases_only_in_candidate` counts, plus a
  `case_attrition_warning` when either side loses more than 10%. Read that
  warning before the verdict — because infra verdicts are filtered out
  upstream, the cases a candidate *crashed on* are exactly the ones that
  drop out of the comparison, so an unexamined attrition warning can mean
  "improved" was measured only on the cases that survived. Say so in the
  report rather than quoting the delta alone.
  Report deltas as
  "improved / worsened / within noise (n=52 can only detect ~14pp)" — never a
  bare percentage.
- pass^k alongside pass@k when k>1
  (`${CLAUDE_PLUGIN_ROOT}/scripts/reduce_repeats.py` over the per-case repeated
  verdicts — pass@k = can-succeed, pass^k = all-k-succeed reliability, and
  their gap is the flakiness signal); flakiness ledger updated per case
  (newly-broken vs known-flaky distinguished in the diff).
- **Zero-failure branch**: 0 gating failures is a valid, reportable outcome,
  not a signal something is broken — never crash or skip the report because
  the failure-cluster step has nothing to cluster. Page one instead states
  "0 gating failures (n=<N>)" plus the metrics tables and skips the clusters
  section with that one-line reason (still print any `unscored`/`skipped`/
  non-gating counts so a quiet run isn't mistaken for a fully green one).
- Report, written to `reports/<run-id>/` alongside the `cases/` material from
  §2 and the `manifest.yaml` from §1: `report.md` + `report.html`, and
  **required alongside them, `results.json`** — the machine-readable summary
  (`run_id`, `harness_version`, `dataset_version` — echoed from the manifest so
  a gate script never has to open two files to check they match — plus per-case
  verdict rows and `summary.gating_failures`/`summary.infra_rate`) that §5's
  headless CI gate reads; it is not optional scaffolding, the gate has nothing
  else to read. **Also required: `verdicts.jsonl`** — one row per case
  (`case_id`, `set`, `category`, `gating`, per-layer `layers`, top-level
  `verdict`), the run's durable verdict record; and
  **`verdicts_for_stats.jsonl`** — the same cases reduced to
  `{"case_id", "verdict"}` with `infra_*`/`unscored` rows dropped, which is
  exactly the input `stats.py` pairs. Two files rather than one because
  `stats.py` treats a non-`pass`/`fail` verdict as a hard error instead of
  filtering silently: the filtering stays a visible step here, and the
  unfiltered record survives beside it. Both sit directly at
  `reports/<run-id>/`, not under `cases/`, so they still commit when
  `reports/.gitignore` excludes raw per-case material. **Page one = top-3
  failure clusters** (or the zero-failure line above), each with 1–2
  expected-vs-actual trace examples and the implicated surface (router prompt /
  tool description X / missing OOS route) + effort tag. Metrics tables and the
  confusion matrix follow, including the `execution` and `authz` layers as
  their own rows/sections — never merged into `answer` or `trajectory`.
  Holdout-split cases appear as aggregate only, whichever mode's selection
  happened to include them (see
  [references/run-modes.md](references/run-modes.md) `holdout` — same rule
  covers their `cases/` folders: they exist, per §2, but their
  `request.json`/`response.json` content is never surfaced in
  `report.md`/`.html` **or in `results.json`'s per-case rows** — both are
  aggregate-only for holdout ids, matching the "no per-case trace excerpts"
  seal). **A run whose selection included holdout ids (`--holdout`, `--full`)
  spends one holdout look**: append it — run id, date, mode, reason — to the
  same dataset-metadata ledger `analyze --unseal` writes to, and print the
  running count. That ledger is the only record behind the N=5 reseal trigger
  `${CLAUDE_PLUGIN_ROOT}/skills/analyze/SKILL.md` §`--unseal` and
  `${CLAUDE_PLUGIN_ROOT}/skills/optimize/SKILL.md` ("Candidate pool") both
  enforce, and this is its only writer besides `--unseal` itself — an uncounted
  holdout run makes the seal a number nobody is keeping. `--baseline` pins this
  run as the new baseline. You write the `.md`; produce the `.html` from it
  with
  `${CLAUDE_PLUGIN_ROOT}/scripts/md_to_html.py reports/<run-id>/report.md reports/<run-id>/report.html`
  — do not hand-write HTML. It emits one self-contained file (no external
  assets), which is what makes a report shareable with teammates who never open
  Claude Code.

## 5. Headless / CI gate
The hard-gated modes (`regression`, `full`) run unattended in CI. The LLM is
NEVER in the final gate path — it produces a JSON verdict, a separate
tokenless shell step reads it and sets the exit code:

```
claude -p "/agent-eval:run --regression" --output-format json --bare --permission-mode dontAsk
```

- `--bare` skips plugin/skill auto-discovery for a reproducible machine run.
- `--permission-mode dontAsk` — no approval prompts; the cost/time confirmation
  in §1 is skipped for the same reason.
- Before trusting the run, check the `system/init` event in the JSON stream
  for `plugin_errors` / `mcp_server_errors` — a run that launched under a
  broken plugin load is not a real result. Note: `--bare` currently isn't in
  the official CLI reference (open doc gap) and gating on `plugin_errors` is
  not an officially documented pattern — both are best-effort here; re-verify
  against current docs when wiring CI.
- The shell step then reads the run's verdict (gating failures = 0 and no
  `infra_error`/`infra_incomplete` above the agreed threshold) and sets the
  process exit code accordingly — this script, not Claude, is what actually
  gates the merge. Full example and the `results.json` shape:
  [references/run-modes.md](references/run-modes.md).

Full flag reference, selection mechanics, and worked examples for all five
modes: [references/run-modes.md](references/run-modes.md).
