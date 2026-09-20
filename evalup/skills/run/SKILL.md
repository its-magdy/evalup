---
name: run
description: >-
  Resolve a run mode into a plan, execute it with run_cases.py, and turn the
  result into a report. The runner owns pre-flight, every app call, every
  scorer, the artifacts and the baseline diff; this skill decides what to run
  and what the numbers mean. Five modes — smoke, regression, targeted, holdout,
  full — cover everyday edits through release validation, plus a CI gate.
argument-hint: "[--smoke|--regression|--targeted|--holdout|--full] [--tag <component>] [--filter-failing] [--layer X] [--k N] [--baseline]"
---

# Run — Execute and Score

`${CLAUDE_PLUGIN_ROOT}/scripts/run_cases.py` executes the run;
`${CLAUDE_PLUGIN_ROOT}/docs/runner-contract.md` is its spec. It owns pre-flight,
every app call, every scorer, the artifacts, the completeness check and the
baseline diff. **Never hand-orchestrate a run; never eyeball-score anything.**
Yours is that contract's §13, below.

Arguments, when the user typed any: `$ARGUMENTS` — the mode flag is in there.
`--holdout` and `--full` spend a sealed look (§4), so read it before you plan.

## 0. Run modes

Five selections + gate policies over one engine. Mechanics and worked examples:
[references/run-modes.md](references/run-modes.md).

| Mode | Selection | k | Gate | When |
|---|---|---|---|---|
| `smoke` | cases with `smoke` in `split` | 1 | soft | every prompt/code edit (hook) |
| `regression` | cases with `full` in `split` | ≥3, pass^k | hard | pre-merge / nightly |
| `targeted` | component-tag filter (+ `--filter-failing`) | per-case | soft | after optimizing one surface |
| `holdout` | sealed `holdout` split | per-case | decision | optimizer's keep/revert step |
| `full` | `full` + `holdout` | ≥3, pass^k | hard | release validation |

No mode flag → `regression`, the old unqualified default. **Selection reads the
case's `split` field, never a directory** — the field is owned by
`${CLAUDE_PLUGIN_ROOT}/skills/generate/references/case-format.md`, and `full`
and `holdout` are mutually exclusive there, so the seal travels with the case.

## 1. Build `plan.json`

The plan is the whole interface. The runner reads no YAML, no `adapter.yaml`,
no `datasets/**`, and does not know what `--smoke` means. Full shape and
defaults: runner-contract §2. **The runner does not re-lint the suite** — run
`${CLAUDE_PLUGIN_ROOT}/scripts/validate_cases.py --cases <suite.json>
--capabilities <matrix.json>` over the converted JSON first; it owns what a
valid case is.

- **`run_id`**: `<mode>-<UTC YYYYMMDDTHHMMSSZ>`, e.g. `smoke-20260818T183920Z`.
  It names `reports/<run-id>/`, which is `--out`.
- **`cases`, `selecting_split`, `k`, `gate`**: resolved from the mode flag via
  §0. `selecting_split` becomes `set` in `verdicts.jsonl`, and a selected case
  that does not carry it is exit 2.
- **YAML → JSON is a script's job, never yours**:
  `${CLAUDE_PLUGIN_ROOT}/scripts/convert_suite.py <state-dir> -o
  <tmp>/converted.json --split-dir <tmp>` writes the combined document plus
  `suite.json`, `capabilities.json`, `adapter.json` and `manifest.json` — the
  files the flags above and below take. **That conversion is the run's parse,
  not a second opinion**, which is exactly why you do not re-type it: a suite
  transcribed by hand is unreproducible, and the linter reads the same
  transcription, so a drifted number is invisible to it. Build `plan.json` by
  loading that JSON with a script (`python3 -c` or `jq`), filtering `cases` to
  the selected split, and writing the file — do not paste case bodies through
  your own output. It leaves `${VAR}` refs unresolved; the runner resolves
  them, so no secret reaches the plan file. It needs PyYAML (the only script
  that does); if it is absent the error names the one-line fix, and `uv run
  --with pyyaml python …` needs no install.
- **`scoring`**: `oos_route` from the profile's `oos_handling` route,
  `id_pattern` from its `record_id_pattern`, rest defaulted. `--layer X` sets
  every other `capability_matrix` layer to `{"enabled": false, "blocked_by":
  "--layer X"}`, so those layers report `unscorable`, never `pass`.
- **`paths.holdout_ledger`**: the **`.jsonl` sidecar** (e.g.
  `datasets/holdout-looks.jsonl`), relative to `state_dir` — not the dataset
  YAML, which a stdlib-only appender would corrupt. Required by `--holdout`
  and `--full` (§4).
- **`paths.judge_calibration`**: the **`.json` sidecar** `score_agreement.py
  --write` produced (e.g. `judge/calibration.json`), relative to `state_dir`.
  `judge.status: calibrated` alone no longer opens the judged gate — without
  this file the runner reports `unjudged (judge calibration not recorded)`,
  because that flag is DERIVED and nothing but this sidecar derives it.
- **`manifest_extra`**: what only you know — `dataset_version`, app repo, git
  SHA, models, prompt snapshot hashes, judge model + status, rubric versions,
  temperature, environment kind, cost estimate. Without it the run compares to
  nothing.

## 2. Run it

```
${CLAUDE_PLUGIN_ROOT}/scripts/run_cases.py --plan plan.json --out reports/<run-id> \
  [--baseline-verdicts reports/<baseline-run-id>/verdicts_for_stats.jsonl] [--resume]
```

`--dry-run` validates the plan and pre-flight without calling the app; use it
on anything unfamiliar. Then estimate cost: probe 2–3 cases, extrapolate
(n × k × tokens × price + judge calls), ask "≈ $X, ~Y min. Proceed?" — skipping
the prompt only under a pre-approved budget or the headless gate (§6). Resume
an interrupted run with `--resume` under the same run id; re-check a finished
one any time with `--verify reports/<run-id>`.

## 3. Read the exit code

Non-zero always prints `{"error": ...}` as JSON on **stdout**. React to the
code, not to "non-zero".

| Code | Meaning | Response |
|---|---|---|
| 0 | Complete. Says nothing about pass/fail. | Report (§5). |
| 1 | Internal error. | Harness bug; report the traceback, don't retry blind. |
| 2 | Bad plan or usage. **Nothing was written.** | Fix the plan, re-run. Free. |
| 3 | Pre-flight abort: env var, health check, trace store, old layout, `cli` mode. | Fix the app, adapter or state dir. Only the health check was billed. |
| 4 | A canary failed — harness or judge drift. | **Quote no number from this run.** The app's score is meaningless until the canary passes. |
| 5 | Infra rate above `infra_rate_abort`. | The service is degraded. Re-run when healthy; never report the partial pass rate. |
| 6 | Completeness check failed: **a required artifact is missing or inconsistent**. | Read `summary.missing_artifacts`. Not quotable, not a baseline. `--resume` or re-run; never write a report over it. |
| 7 | A scorer exited 2: artifacts complete, **numbers not**. | Read `summary.scorer_errors` and the `error` layers, fix the malformed `expect` or scorer input, re-score. Report anyway only while naming the layers left unscored. |

A red suite exits **0** — gating is §6's job, not the runner's.

## 4. Baseline and the holdout ledger

- **`reports/baseline.json`** (`{"run_id", "dataset_version", "harness_version"}`)
  is the one file naming the baseline. Read it; never take the newest
  `reports/` entry by mtime. Pass `reports/<its run_id>/verdicts_for_stats.jsonl`
  to `--baseline-verdicts` and the runner enforces the diff's rules — same
  dataset and harness version, same `k` — before any spend. **Never prune
  `reports/` by age without reading this pointer**: deleting the pinned run
  costs every future diff.
- **Old layout, checked before you conclude "first run"**: no `baseline.json`
  but a sibling `baselines/` or `runs/` means the state dir predates the
  per-run layout and does have a baseline. Say so — migrate with
  `${CLAUDE_PLUGIN_ROOT}/docs/migrate-run-layout.md`, or pass `--baseline` to
  pin this run and abandon the old one. (The runner also refuses it, exit 3.)
- **First run** — no pointer, no old layout — is a branch, not an error. Run
  without `--baseline-verdicts`, write `baseline.json` at this run, print
  "baseline established (run <id>); future runs diff against this."
  `--baseline` does that deliberately. **Pinning is yours; the runner never
  writes that file.**
- **The holdout look**: `--holdout` and `--full` spend one of the N=5. The
  runner appends `{run_id, date, mode, reason}` to the `.jsonl` sidecar and
  prints the running total on stderr — surface that count. That sidecar, not
  the dataset YAML, is the ledger `${CLAUDE_PLUGIN_ROOT}/skills/analyze/SKILL.md`
  §`--unseal` and `${CLAUDE_PLUGIN_ROOT}/skills/optimize/SKILL.md` ("Candidate
  pool") read. Count its lines: it has two writers.

## 5. Report

The runner leaves `manifest.yaml`, `results.json`, `verdicts.jsonl` (the durable
per-case record), `verdicts_for_stats.jsonl` (its `pass`/`fail` subset, exactly
what `stats.py` pairs), `routing_report.json`, `reliability.json` and
`comparison.json`. Every number you quote comes from those. You add `report.md`
beside them, then `report.html` via
`${CLAUDE_PLUGIN_ROOT}/scripts/md_to_html.py reports/<run-id>/report.md
reports/<run-id>/report.html` — self-contained, shareable with people who never
open Claude Code. Never hand-write HTML.

- **Page one = the top-3 failure clusters**, each with 1–2 expected-vs-actual
  excerpts, the implicated surface (router prompt, tool description X, missing
  OOS route) and an effort tag. That narrative is what the LLM here is for.
  Metrics tables and the confusion matrix follow, with `execution` and `authz`
  as their own rows — never folded into `answer` or `trajectory`. Surface
  `spurious_labels` (a route no case asks for is a finding), the pass@k /
  pass^k gap, and `reliability.json`'s `excluded_cases`.
- Deltas read "improved / worsened / within noise (n=52 can only detect ~14pp)",
  never a bare percentage. Read `comparison.json`'s attrition warning **before**
  the verdict: crashed cases are exactly the ones a paired diff drops, so
  "improved" can mean "improved on the cases that survived".
- **Zero gating failures is a result, not a broken step.** State "0 gating
  failures (n=<N>)", skip the clusters with that one-line reason, and still
  print the `unscored`/`skipped`/`unscorable` counts.
- **Holdout cases stay aggregate-only** in `report.md`/`.html`, whichever mode
  selected them (`results.json` already enforces it).

## 6. Headless / CI gate

`regression` and `full` run unattended, and **the LLM is never in the gate
path**: it produces the run, then a separate tokenless step —
`${CLAUDE_PLUGIN_ROOT}/scripts/gate.py reports/<run-id>` (or `reports/
--latest --mode regression`) — reads `results.json`, prints a one-screen
summary and exits 0 open / 1 closed. Run it after every interactive run too:
it is the fastest honest summary, and the runner's own exit 0 says nothing
about pass/fail (§3). That step, the
`claude -p ... --bare --permission-mode dontAsk` invocation, and the
`system/init` `plugin_errors` check that must precede trusting either:
[references/run-modes.md](references/run-modes.md) "Headless/CI gate". Under
that mode §2's cost prompt is skipped — nobody is there to answer it.
