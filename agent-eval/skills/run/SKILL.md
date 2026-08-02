---
name: run
description: >-
  Execute the eval dataset against the app and score every applicable layer,
  including execution accuracy and authorization. Produces per-case verdicts,
  a run manifest, a baseline diff with statistical honesty, and a report
  headlined by failure clusters. Five modes — smoke, regression, targeted,
  holdout, full (see references/run-modes.md) — cover everyday edits through
  release validation, plus a tokenless CI gate.
argument-hint: "[--smoke|--regression|--targeted|--holdout|--full] [--tag <component>] [--filter-failing] [--layer X] [--k N] [--baseline]"
---

# Run — Execute and Score

Scripts in `${CLAUDE_PLUGIN_ROOT}/scripts/` do all deterministic work. You
orchestrate; you do not eyeball-score anything a script can score.

## 0. Run modes

Five modes select a case subset and a gate policy over the same execution
engine (§§1–4 below never change). Full mechanics, flags, and worked examples:
`references/run-modes.md`. Summary:

| Mode | Selection | k | Gate | When |
|---|---|---|---|---|
| `smoke` | tagged `datasets/smoke/` subset | 1 | soft | every prompt/code edit (hook) |
| `regression` | full suite (`datasets/full/`) | ≥3, pass^k | hard | pre-merge / nightly |
| `targeted` | filter by component tag (+ `--filter-failing`) | per-case | soft | right after optimizing one surface |
| `holdout` | sealed `datasets/holdout/` set | per-case | decision | optimizer's keep/revert step |
| `full` | everything, incl. judged layers | ≥3, pass^k | hard | release validation |

`--smoke` is the existing flag and is unchanged; the other four are new
flags mapped onto the same taxonomy (EVAL-DESIGN-RECOMMENDATION.md §7). No
mode flag given → behave as `regression` (the old unqualified default: full
suite, hard gate).

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
- Write the **run manifest** first: run id, mode, dataset_version + case id/hash
  list, app git SHA, prompt snapshot hashes, app model id, judge model +
  judge-prompt version, rubric versions, harness version, k, temperature,
  environment kind. A run without a manifest is not comparable to anything.
  Write it the same way whether or not a baseline exists yet — the first-run
  branch (§4) is a comparison-stage decision, not a pre-flight one.

## 2. Execute
- Serial by default; adapter's max_concurrency only when state-safe.
- Per case: (seed state if defined) → invoke via adapter (k times if k>1) →
  collect trace (respect completeness gate: quiescence, then max_wait →
  `infra_incomplete`) → persist raw response + normalized trace + verdict
  durably per case (**resume**: an interrupted run continues from the last
  completed case under the same manifest).
- Failure taxonomy: timeouts/429/5xx/app-crash → `infra_error` (bounded
  retries, then recorded); crash on `noise`/`adversarial` categories also
  increments the **crash-rate metric** (a first-class number, especially
  pre-launch). Infra verdicts never enter pass/fail denominators.
- **Canaries every run**: ~10% of the run, minimum 1 known-good + 1 known-bad
  canned case, run first. A canary scoring wrong → abort and flag harness/judge
  drift; nothing else from this run is trustworthy.

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
   profile's `oos_handling` route so OOS metrics appear; a null `observed`
   is scored as `__no_route__`, a fail, never a crash),
   `trajectory_match.py` (subset default; order/exact where
   asserted; forbidden-tool violations; trajectory precision/recall),
   `score_args.py` (value/present/from_tool_result assertions; unscorable
   without content capture — reported, never failed),
   `detect_loops.py` (repeated tool+args-hash),
   `score_answer.py` (must_contain/must_not_contain — plain string or
   `/regex/` — and `format.json_schema`), latency/tokens per stage from the
   normalizer's per-agent usage rollup. Business-rule oracles and state-diff
   (if seeded) have no generic script — you evaluate them against the
   profile's rule definitions and the environment's snapshot scripts, and
   record per-rule verdicts in the run output.
3. **Execution layer** — for every case carrying `expect.result`, invoke
   `score_execution.py` (see `scripts/score_execution.py`; do not edit it,
   only call it). You produce both sides, the scorer only compares them:
   extract `actual` from the trace/tool-result/structured response per the
   adapter's result-extraction contract (see
   `skills/discover/references/adapter-contract.md`) — no extractable result
   → pass `{"missing": true, "reason": "<why>"}` and it scores `unscored`/
   unscorable, never `fail`; compute `expected` offline by running the
   case's `reference_query` (see `skills/generate/references/case-format.md`
   `expect.result`) against the seeded fixture / read-only oracle — never
   hand-typed, never recomputed by the scorer. Report `execution` as its own
   layer in the per-case verdict and the run report, alongside routing/args/
   trajectory — do not fold it into `answer`.
4. **Authz layer** — for every case carrying `expect.authz`, invoke
   `score_authz.py <trajectory.json> <case_expect.json> [--answer FILE]`
   (see `scripts/score_authz.py`; call it, do not implement scoring logic
   here). Feed it `normalize_trace.py`'s output (or `{"tool_calls": [...]}`)
   as `trajectory.json` and the case's `expect` object as `case_expect.json`;
   the optional `--answer FILE` (final answer text) is accepted only as
   *secondary* evidence for a forbidden id leaking into the prose — it never
   decides `expect_refusal`. It grades the tool-call log and the record IDs
   actually returned against `expect.authz`'s `allowed_record_ids` /
   `forbidden_record_ids` / `forbidden_tools` / `expect_refusal` — **never an
   LLM read of the chat text** (a prompt-only "I can't share that" can hide
   an open endpoint underneath; the log and the IDs are the ground truth).
   Missing tool-result content → the record-id checks report `unscorable`
   (never `fail`); `forbidden_tools` has no unscorable state (which tools
   were invoked is structural, independent of content capture). Gate: if the
   case's `category` is `adversarial-*` and the adapter's
   `environment.safe_to_attack` is not true (§1 pre-flight), skip the case
   for this layer and report it `skipped` (with the reason), the same
   never-invoke-an-uncleared-probe discipline as any other red-team check —
   do not silently fail it and do not run it anyway. Non-adversarial
   `expect.authz` cases (ordinary permission-scoped answers) run regardless.
   Report `authz` as its own layer, same shape as every other scorer:
   `{"layer": "authz", "verdict": ..., "checks": [...]}`.
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
- **First-run branch** (no baseline exists yet): this is a branch, not an
  error — skip the diff, pin this run as the baseline automatically
  (`--baseline` semantics are implied on a first run, no flag needed), and
  print "baseline established (run <id>); future runs diff against this."
  Every other step in §§1–3 runs exactly as normal; only the comparison step
  is short-circuited.
- Baseline diff (same dataset_version AND same harness version only — a
  scorer change alters what a number means just like a dataset change does.
  Refuse cross-version diffs; for a harness upgrade, tell the user to pin a
  fresh baseline; for dataset drift, suggest the comparable-core subset). Significance via `stats.py`:
  exact Bayesian P(improvement) on paired verdicts at every n, with an exact
  one-sided sign test reported alongside (feed it only `pass`/`fail` rows —
  infra and `unscored` verdicts are a hard error, never silent failures).
  Report deltas as
  "improved / worsened / within noise (n=52 can only detect ~14pp)" — never a
  bare percentage.
- pass^k alongside pass@k when k>1 (`scripts/reduce_repeats.py` over the per-case
  repeated verdicts — pass@k = can-succeed, pass^k = all-k-succeed reliability, and
  their gap is the flakiness signal); flakiness ledger updated per case
  (newly-broken vs known-flaky distinguished in the diff).
- **Zero-failure branch**: 0 gating failures is a valid, reportable outcome,
  not a signal something is broken — never crash or skip the report because
  the failure-cluster step has nothing to cluster. Page one instead states
  "0 gating failures (n=<N>)" plus the metrics tables and skips the clusters
  section with that one-line reason (still print any `unscored`/`skipped`/
  non-gating counts so a quiet run isn't mistaken for a fully green one).
- Report (`reports/<run-id>.md` + `.html`): **page one = top-3 failure
  clusters** (or the zero-failure line above), each with 1–2 expected-vs-actual
  trace examples and the implicated surface (router prompt / tool
  description X / missing OOS route) + effort tag. Metrics tables and the
  confusion matrix follow, including the `execution` and `authz` layers as
  their own rows/sections — never merged into `answer` or `trajectory`.
  Holdout-split cases appear as aggregate only, whichever mode's selection
  happened to include them (see references/run-modes.md `holdout`). `--baseline`
  pins this run as the new baseline.
  You write the `.md`; produce the `.html` from it with
  `${CLAUDE_PLUGIN_ROOT}/scripts/md_to_html.py reports/<run-id>.md
  reports/<run-id>.html` — do not hand-write HTML. It emits one self-contained
  file (no external assets), which is what makes a report shareable with
  teammates who never open Claude Code.

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
  broken plugin load is not a real result.
- The shell step then reads the run's verdict (gating failures = 0 and no
  `infra_error`/`infra_incomplete` above the agreed threshold) and sets the
  process exit code accordingly — this script, not Claude, is what actually
  gates the merge. Full example and the `results.json` shape:
  `references/run-modes.md`.

Full flag reference, selection mechanics, and worked examples for all five
modes: `references/run-modes.md`.
