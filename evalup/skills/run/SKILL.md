---
name: run
description: >-
  Resolve a run mode into a plan, execute it with run_cases.py, and turn the
  result into a report. The runner owns pre-flight, every app call, every
  scorer, the artifacts and the baseline diff; this skill decides what to run
  and what the numbers mean. Five modes — smoke, regression, targeted, holdout,
  full — cover everyday edits through release validation, plus a CI gate.
argument-hint: "[--smoke|--regression|--targeted|--holdout|--full] [--tag <component>] [--filter-failing] [--layer X] [--k N] [--baseline]"
allowed-tools: >-
  Read Grep Glob Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/*) Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*)
---

# Run — Execute and Score

> **Plugin root:** `${CLAUDE_PLUGIN_ROOT}`. Reference files and docs write that
> placeholder literally (it is only substituted here), so read every
> `${CLAUDE_PLUGIN_ROOT}/…` path you meet in them as this absolute path, and
> quote it in shell commands.
>
> **Calling the plugin.** Open its files with Read — never `cd` into the
> plugin, and never `ls`, `grep` or `cat` it from the shell: the plugin is not
> a working directory, so those prompt, and headless a prompt is a denial.
> Run each script as its own Bash call, spelled
> `python3 "<that path>/scripts/<name>.py" …` with the path written out: no
> `cd`, no `&&` or `; echo $?` tail, no shell variable holding the path. The pre-approval matches
> that literal form only, and it lapses when the user next replies — a prompt
> after that is expected, not a fault. If a call is **denied**, stop and tell
> the user which permission is missing; never work around it by hand.
>
> **Every shell call, not only script calls.** One plain command per Bash
> call — no `cd`, `&&`, `|`, heredoc, `$( )` or `/tmp` — so an allow rule can
> match it. A denial of **any** call is a stop, never a retry in another form.

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
| `regression` | cases with `full` in `split` | 3 by default (`--k N`), pass^k | hard | pre-merge / nightly |
| `targeted` | component-tag filter (+ `--filter-failing`) | 1 by default (`--k N`) | soft | after optimizing one surface |
| `holdout` | sealed `holdout` split | 1 by default (`--k N`) | decision | optimizer's keep/revert step |
| `full` | `full` + `holdout`, as **two runs**: `regression`, then `holdout` | 3 by default (`--k N`), pass^k | hard | release validation |

No mode flag → `regression`. `k` is a plan-level value: `make_plan.py` sets
the mode's default and `--k N` overrides it; no script reads a per-case `k`.
**Selection reads the
case's `split` field, never a directory** — the field is owned by
`${CLAUDE_PLUGIN_ROOT}/skills/generate/references/case-format.md`, and `full`
and `holdout` are mutually exclusive there, so the seal travels with the case.

## 1. Build `plan.json`

The plan is the whole interface. The runner reads no YAML, no `adapter.yaml`,
no `datasets/**`, and does not know what `--smoke` means. **You never write the
plan, or any case body, by hand** — three scripts do it, in this order:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/convert_suite.py <state-dir> -o <tmp>/converted.json --split-dir <tmp>
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/validate_cases.py --cases <tmp>/suite.json --capabilities <tmp>/capabilities.json --adapter <tmp>/adapter.json [--manifest <tmp>/manifest.json]
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/make_plan.py <tmp>/converted.json --mode <mode> --state-dir <state-dir> -o <tmp>/plan.json
```

- **`convert_suite.py`** is the run's parse of the YAML, not a second opinion,
  which is why you do not re-type it. It leaves `${VAR}` refs unresolved (the
  runner resolves them, so no secret reaches a file). It needs PyYAML, the only
  script that does; if absent, the error names the fix, and `uv run --with
  pyyaml python …` needs no install.
- **`validate_cases.py`** owns what a valid case is; the runner does not
  re-lint. Fix errors in the YAML and re-convert — never in the JSON.
- **`make_plan.py`** resolves the mode via §0 into `cases`, `selecting_split`,
  `k` and `gate`, fills every required key, and prints the `run_id`, the
  `--out` directory and the exact runner command. Its flags are the mode's:
  `--tag <unit-or-route>` and `--failing-in <run-dir>` for `targeted`, `--k N`
  (not under smoke), `--layer X` (every other layer becomes `unscorable` with
  `blocked_by: "--layer X"`, never `pass`). `scoring.oos_route` comes from the
  profile's `oos_handling: route:<name>`; pass `--oos-route <name>` when the
  app's out-of-scope route is declared any other way, or OOS metrics go
  unreported. `--filter-failing` is `--failing-in reports/<last comparable
  run>`. `--timeout-s N`, `--max-attempts N`, `--backoff-s 5,30` and
  `--insecure-tls` fill `execution` (the retry pair for a throttled provider
  or an app that already retries — the runner's default is 3 tries with 1 s
  and 4 s waits; the last flag for a self-signed local dev host only —
  pre-flight otherwise exits 3 with `CERTIFICATE_VERIFY_FAILED`). Never
  hand-edit the plan to change any of these. It refuses `--mode full`:
  one plan carries one `selecting_split`, so release validation is a
  `regression` run and a `holdout` run.
- **`paths.holdout_ledger`** is set for `holdout` (the `.jsonl` sidecar,
  `datasets/holdout-looks.jsonl`); **`paths.judge_calibration`** is set when
  `judge/calibration.json` exists. `judge.status: calibrated` alone does not
  open the judged gate — that flag is DERIVED and only the sidecar derives it.
- **`--manifest-extra <file.json>`** carries what only you know — models,
  prompt snapshot hashes, rubric versions, temperature, environment kind, cost
  estimate. The script fills `dataset_version`, the app's git SHA and
  clean/dirty itself. Without these the run compares to nothing.

Full shape and defaults, if you need them: runner-contract §2.

## 2. Run it

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run_cases.py --plan plan.json --out reports/<run-id> \
  [--baseline-verdicts reports/<baseline-run-id>/verdicts_for_stats.jsonl] [--resume]
```

`--dry-run` validates the plan and pre-flight without calling the app; use it
on anything unfamiliar. Then estimate cost: probe 2–3 cases, extrapolate
(n × k × tokens × price + judge calls), ask "≈ $X, ~Y min. Proceed?" — skipping
the prompt only under a pre-approved budget or the headless gate (§6). Resume
an interrupted run with `--resume` under the same run id; re-check a finished
one any time with `--verify reports/<run-id>`.

**Wait for the runner; never end the turn on a `running` run.** Launch
`run_cases.py` in the **foreground** of one Bash call with `timeout` at its
600000 ms maximum — never `run_in_background`: headless, a background shell is
killed seconds after the turn ends, and a killed runner leaves `results.json`
at `summary.status: "running"` with its cases half paid for. If the harness
moves the call to the background at that cap, do not stop: call
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/wait_run.py reports/<run-id>` (same
600000 ms timeout) and repeat it while it exits 3. It exits 0 once the run is
finalized (printing `summary.status`, the runner's exit code and the last
`run.log` event — read §3 from that exit code); 4 when the run is still
`running` but no runner process exists — say so and offer `--resume` under
the same run id. Report nothing from a run whose status is `running`.

## 3. Read the exit code

Non-zero always prints `{"error": ...}` as JSON on **stdout**. React to the
code, not to "non-zero".

| Code | Meaning | Response |
|---|---|---|
| 0 | Complete. Says nothing about pass/fail. | Report (§5). |
| 1 | Internal error. | Harness bug; report the traceback, don't retry blind. |
| 2 | Bad plan or usage. **Nothing was written.** | Fix the plan, re-run. Free. |
| 3 | Pre-flight abort: env var, health check, trace store, `cli` mode. | Fix the app, adapter or state dir. Only the health check was billed. |
| 4 | A canary failed — harness or judge drift. | **Quote no number from this run.** The app's score is meaningless until the canary passes. |
| 5 | Infra rate above `infra_rate_abort` (plan default 0.25). | The service is degraded. Re-run when healthy; never report the partial pass rate. |
| 6 | Completeness check failed: **a required artifact is missing or inconsistent**. | Read `summary.missing_artifacts`. Not quotable, not a baseline. `--resume` or re-run; never write a report over it. |
| 7 | A scorer exited 2: artifacts complete, **numbers not**. | Read `summary.scorer_errors` and the `error` layers, fix the malformed `expect` or scorer input, re-score. Report anyway only while naming the layers left unscored. |

A red suite exits **0** — gating is §6's job, not the runner's.

Two infra thresholds, on purpose, over two denominators. The plan aborts a
run at `infra_rate_abort` (0.25) of every case attempted so far, canaries
included, so a degraded service does not burn the whole budget; `gate.py`
refuses a *finished* run whose `summary.infra_rate` — the non-canary cases
sent — is above `--max-infra-rate` (0.05). A
run can therefore finish and still close the gate on infra alone — finishing
preserves the verdicts already paid for, and the gate stays strict. On a
throttled provider (429/503 walls; each 5xx is retried and counts as
`infra_error` on exhaustion, whatever the case expects) fix or wait out the
provider, or raise `--max-infra-rate` for that one job and say so in the
report. **First read `gate.py`'s "same 5xx on every attempt" line**
(`summary.repeated_5xx`; the case's `verdict.json` names the status): a case
that got one 5xx on every attempt of every repeat may be the app's own error
— a guard that throws looks exactly like this — and re-running or raising
the threshold would hide it. Open its `response.json`, and report it as a
possible app defect, still counted as infra. Never lower the abort, and never
read a partial pass rate as the app's score.

## 4. Baseline and the holdout ledger

- **`reports/baseline.json`** (`{"run_id", "dataset_version", "harness_version"}`)
  is the one file naming the baseline. Read it; never take the newest
  `reports/` entry by mtime. Pass `reports/<its run_id>/verdicts_for_stats.jsonl`
  to `--baseline-verdicts` and the runner enforces the diff's rules — same
  dataset and harness version, same `k` — before any spend. **Never prune
  `reports/` by age without reading this pointer**: deleting the pinned run
  costs every future diff.
- **First run** — no pointer — is a branch, not an error. Run
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

The runner always leaves `manifest.yaml`, `results.json`, `verdicts.jsonl` (the
durable per-case record) and `verdicts_for_stats.jsonl` (its `pass`/`fail`
subset, exactly what `stats.py` pairs), and, when they apply,
`routing_report.json` (a non-canary case's routing was scored),
`reliability.json` (k > 1, so never under `smoke`) and `comparison.json` (`--baseline-verdicts` was given). Every
number you quote comes from those. You add `report.md`
beside them, then `report.html` via
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/md_to_html.py reports/<run-id>/report.md
reports/<run-id>/report.html` — self-contained, shareable with people who never
open Claude Code. Never hand-write HTML.

- **What a run does not score today.** `run_cases.py` has no scorer for the
  **judged** layer (`expect.answer.rubric`) or for **business rules**
  (`expect.answer.rules`): it records them as `unjudged (…)` and `unscored`,
  and nothing else scores them either. The judge agent runs only inside
  `analyze --label`, where a human labels beside it; its verdicts live in the
  calibration record, never in a run's pass/fail. So
  **report both as "not measured in this run"**, even when the rubric is
  calibrated and the runner's reason reads `deferred to skill` — there is no
  step that picks that deferral up, and scoring them yourself is the
  eyeball-scoring this file forbids.
- **Before quoting any app response**, check the adapter's
  `data.may_contain_pii`. If true, redact the excerpts and say they are
  redacted: reports are written to be shared and committed.
- **Count cases the way `results.json` does, and say which count you mean.**
  `summary.n` is every non-canary case SELECTED — skipped and infra cases
  included — so it is never a pass-rate denominator: divide `passes` by
  `passes + failures` (a run of 3 pass, 1 fail, 1 skipped is 3/4, not 3/5).
  Canaries are counted apart under `summary.canaries`, and
  `summary.attempted` is every case that was sent, canaries included. The
  contract's §10 counts table defines each. "6 test cases and 2 canaries
  ran; 1 failed, 1 skipped" — never "8 ran" beside an `n` of 6. A failure on a `gating: false` case is reported
  as a failure that did not close the gate.
- **Page one = the top-3 failure clusters**, each with 1–2 expected-vs-actual
  excerpts, the implicated surface (router prompt, tool description X, missing
  OOS route) and an effort tag. That narrative is what the LLM here is for.
  Metrics tables and the confusion matrix follow, with `execution` and `authz`
  as their own rows — never folded into `answer` or `trajectory`. **Say what
  routing measured:** when a case's `layers.routing.observed_from` in
  `verdict.json` is `"status"` (the adapter's `route_from_status`), its
  routing verdict is whether the HTTP status mapped to the expected label —
  answered-vs-refused — and says nothing about which route target handled
  the request (runner-contract §5.2). If every scored case says `status`,
  head the routing section with that sentence and never quote the number as
  routing accuracy; if only some do, say how many. Surface
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
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/gate.py reports/<run-id>` (or `reports/
--latest --mode regression`) — reads `results.json`, prints a one-screen
summary and exits 0 open / 1 closed. Run it after every interactive run too:
it is the fastest honest summary, and the runner's own exit 0 says nothing
about pass/fail (§3). Its `--max-infra-rate` (0.05) is stricter than the
plan's abort (0.25) by design — §3 says what to do on a throttled provider. That step, the
`claude -p ... --bare --plugin-dir` invocation, and the two checks that must
precede trusting either — the session's exit code with `result.is_error`,
then `system/init`'s plugin load:
[references/run-modes.md](references/run-modes.md) "Headless/CI gate". Under
that mode §2's cost prompt is skipped — nobody is there to answer it.
