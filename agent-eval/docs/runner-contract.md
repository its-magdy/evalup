# `run_cases.py` — Runner Contract

**Status:** design, awaiting review. No code exists yet. This document is the
input to Step 5b (implement) and Step 5c (rewire `run/SKILL.md`).

**What this is.** `run/SKILL.md` §2 "Execute" is currently hand-orchestrated by
the LLM on every run. That is the audit's first conclusion: not reproducible,
expensive, and it silently drops required outputs. The proof is the shipped run
at `field-test-qa/.agent-eval/reports/smoke-20260818T183920Z/`, which has
`manifest.yaml`, `results.json`, `report.md`, `report.html`, and four complete
`cases/<id>/` folders — and **no `verdicts.jsonl` and no
`verdicts_for_stats.jsonl`**, both of which §4 marks required. Nothing failed.
Nothing warned. The run looks finished. `stats.py` simply has nothing to pair,
so that run can never be a baseline for anything.

`run_cases.py` is the execution engine that makes that outcome unreachable.
§9 ("Completeness") is the part of this contract that does that work; every
other section exists so §9 has something well-defined to check.

**Two constraints that shape everything below.**

1. **The runner shells out to the existing scorers. It reimplements no
   scoring.** The scorers audited clean and are the definition of what a number
   means; a second implementation inside the runner would be a second, divergent
   definition. The runner's job is to materialize each scorer's inputs, invoke
   it, and record its output verbatim.
2. **Stdlib-only, Python 3.9 floor.** No `requests`, no `yaml`, no third-party
   anything. This is why the case list arrives as **JSON**, on exactly the
   argument `validate_cases.py` already makes in its docstring: the stdlib has
   no YAML parser, a hand-rolled subset parser misparses rather than failing
   loudly, and a runner whose model of a case disagrees with the linter's is
   worse than no runner. The calling skill converts YAML → JSON with the parser
   it already uses, and that conversion *is* the run's parse — not a second
   opinion.

---

## 1. Invocation

```
run_cases.py --plan <plan.json|-> --out <reports/<run-id>> [--resume] [--dry-run]
run_cases.py --verify <reports/<run-id>>
```

| Flag | Meaning |
|---|---|
| `--plan PATH` | the single JSON input document (§2). `-` reads stdin. Required except with `--verify`. |
| `--out DIR` | the run directory to write. Must equal `<state>/reports/<run_id>` where `run_id` is the plan's. Required except with `--verify`. |
| `--resume` | continue an interrupted run into an existing `--out` (§8). Without it, a non-empty `--out` is exit 2. |
| `--verify DIR` | run **only** the completeness check (§9) over an existing run directory and exit. No app calls, no scoring, no writes. |
| `--dry-run` | pre-flight (§4) + plan validation + print the resolved case list and cost inputs; write nothing, call the app zero times. |
| `--version` | `agent-eval harness <HARNESS_VERSION>`, via `_common.add_version_flag`, same string as every scorer. |

There are no other flags. Mode, k, filters, and selection are **already
resolved** in the plan — the runner does not know what `--smoke` means, and does
not read `adapter.yaml`, `profile.yaml`, or `datasets/**` off disk. Selection is
the skill's job (`run/SKILL.md` §0); execution is the runner's. Keeping the
mode taxonomy out of the runner is what lets 5c shrink the skill without
re-teaching the runner five modes.

---

## 2. Input: `plan.json`

One document, all of it JSON, produced by the calling skill. Unknown top-level
keys are a hard error (exit 2) — a typo'd key must not silently disable a layer.

```jsonc
{
  "plan_version": 1,

  "run_id": "smoke-20260818T183920Z",   // <mode>-<UTC YYYYMMDDTHHMMSSZ>
  "mode": "smoke",                       // smoke|regression|targeted|holdout|full
  "k": 1,                                // repeats per case, >= 1
  "gate": "soft",                        // soft|hard|decision — recorded, never enforced here (§7)
  "selecting_split": "smoke",            // the split this mode selected on; becomes `set` in verdicts.jsonl

  "paths": {
    "scripts_dir": "/abs/path/to/agent-eval/scripts",   // ${CLAUDE_PLUGIN_ROOT}/scripts
    "state_dir": "/abs/path/to/.agent-eval",            // for reports/.gitignore and the holdout ledger
    "holdout_ledger": "datasets/dataset.yaml"           // relative to state_dir; null = runner refuses --holdout/--full (§6)
  },

  "adapter": { ... },        // adapter.yaml converted to JSON, VERBATIM, env refs UNRESOLVED (§3)
  "capability_matrix": {     // profile.yaml's block, verbatim
    "routing":        {"enabled": true},
    "trajectory":     {"enabled": false, "blocked_by": "no trace-id correlation"},
    "tool_selection": {"enabled": false, "blocked_by": "stage: pre-stability"},
    "cost_latency":   {"enabled": false, "blocked_by": "..."},
    "multi_turn":     {"enabled": false, "blocked_by": "..."},
    "answer_quality": {"enabled": true, "judged": "provisional"}
  },

  "cases": [ { /* the full case object, case-format.md, YAML->JSON */ } ],

  "scoring": {
    "oos_route": "none",              // score_routing --oos-route; null = omit the flag
    "id_pattern": null,               // score_authz --id-pattern; null = built-in shape
    "float_tolerance": 0.0,           // score_execution --float-tolerance
    "repeat_threshold": 3,            // detect_loops --repeat-threshold
    "call_budget": 15,                // detect_loops --call-budget
    "fail_on_errored_calls": false,   // trajectory_match --fail-on-errored-calls
    "scorer_timeout_s": 30            // per scorer invocation; exceeded => layer `error` (§5)
  },

  "execution": {
    "timeout_s": 120,                 // per app call; default: adapter invocation.timeout_s
    "max_attempts": 3,                // total attempts per repeat, including the first (§7)
    "backoff_s": [1, 4],              // waits before attempts 2 and 3; len == max_attempts-1
    "infra_rate_abort": 0.25,         // abort the run when infra verdicts exceed this share of attempted cases
    "insecure_tls": false             // opt-in only; self-signed local hosts (the field test's `curl -k`)
  },

  "manifest_extra": {
    // values the runner cannot compute; copied verbatim into manifest.yaml
    "dataset_version": 1,
    "app": {"name": "...", "repo": "...", "git_sha": "dccea614", "git_tree": "dirty",
            "model": {"classifier": "...", "executor": "..."}},
    "prompt_snapshot_hashes": {"classifier_system": "bd13...", "...": "..."},
    "judge": {"model": null, "status": "uncalibrated"},
    "rubric_versions": "n/a",
    "temperature": "app-side (0.0/0.1); harness sets none",
    "environment_kind": "live-readonly",
    "cost_estimate": {"note": "..."}
  }
}
```

**Validation, all before any spend, all exit 2 with `{"error": ...}` on stdout:**
unknown top-level key; `plan_version != 1`; `run_id` not matching
`^[a-z]+-\d{8}T\d{6}Z$`; `--out` basename != `run_id`; `k < 1`; empty `cases`;
duplicate case `id`; a case whose `split` does not contain `selecting_split`
(the skill selected wrong — the runner refuses to run a set it cannot label);
`backoff_s` length mismatch; `scripts_dir` missing any of the nine scorer
scripts §5 names.

The runner does **not** re-lint the suite. `validate_cases.py` owns that, the
skill runs it, and duplicating its rules here would create a second definition
of a valid case. The runner checks only what it needs to execute.

---

## 3. Secrets and env resolution

`adapter` arrives with `${VAR}` references **unresolved**, and the runner
resolves them itself from `os.environ` at pre-flight. Two reasons: the plan is a
file on disk (and a plausible thing to attach to a bug report), so resolved
tokens must never be written into it; and adapter hard rule 4 — "env-var refs
unresolved at runtime → pre-flight failure before any spend" — is a runtime
property, so the thing at runtime has to check it.

- Resolution is literal `${NAME}` substitution inside string values, anywhere in
  `adapter`. Unset or empty → exit 3, listing **every** missing name at once
  (not the first), because discovering them one run at a time is the whole
  complaint the field test's `identity_headers.source` note records.
- A resolved value never reaches disk. `request.json` records header **names**
  and writes `"<redacted>"` for the value of any header whose value came from an
  env ref, or whose name appears in the adapter's auth block. Non-secret headers
  (`Content-Type`) are recorded literally. This matches what the shipped run did
  by hand.

---

## 4. Pre-flight (writes nothing but the manifest)

In order. Any failure here is exit 3 and **zero app calls have been billed**
beyond the health check.

1. Plan validation (§2) and env resolution (§3).
2. `invocation.mode`: `http` is implemented. `function` and `cli` exit 3 with
   `runner v1 implements invocation.mode: http only (got: function)` — see
   Open decision D6.
3. **Old-layout check.** If `<state>/reports/baseline.json` is absent but a
   sibling `baselines/` or `runs/` exists, exit 3 with the migration message
   from `run/SKILL.md` §4. This check lives here, not in the skill, because it
   must fire before spend. (The field-test state dir is exactly this case.)
4. **Health check**: one trivial request through the adapter. Non-2xx/timeout →
   exit 3.
5. **Trace branch**, off `adapter.traces`:
   - `source` in the queryable set **and** `correlation` explicit → verify a
     trace arrives and joins on the health-check call. No join → exit 3.
   - `source: view-only|none`, **or** `correlation: none`, **or**
     `convention != gen_ai` with no `mapping_shim` → **trace-less mode** for the
     whole run. Record `traces.collected: false` plus `disabled_layers` in the
     manifest. Raw non-`gen_ai` spans are never passed to `normalize_trace.py`
     (adapter hard rule 5).
6. **Safety gates**: record `environment.safe_to_attack` and the per-tool
   `side_effects` classes. `never-live` tools present with
   `environment.kind: live-*` → categories that could trigger them are
   `skipped`, not run (adapter hard rule 2).
7. If `adapter.data.may_contain_pii` is true, write
   `<state>/reports/.gitignore` containing `*/cases/` **now** — before the first
   case file of the run exists.
8. `mkdir -p <out>/cases`, then write `<out>/manifest.yaml` (§10). The manifest
   is written before the first case, unconditionally, whether or not a baseline
   exists.
9. Write `<out>/results.json` with `summary.status: "running"` and an empty
   `cases` list. A run directory therefore never exists without a results.json;
   §9's check has something to fail on from the first second.

The cost/time estimate and the "Proceed?" prompt stay in the **skill**. The
runner has no user to ask and must be safe to call from CI.

---

## 5. Scoring: the layer table

This is the mechanical core. For each case the runner walks this table top to
bottom. A layer is **applicable** iff its trigger field is present on the case;
it is **enabled** iff `capability_matrix[<layer>].enabled` is not `false`.

| Layer | Trigger on the case | Script | Argv | Runner must materialize |
|---|---|---|---|---|
| `http` | always | *(none — runner compares)* | — | observed status vs `expect.http.status`; absent expectation ⇒ any 2xx passes, 5xx is `infra_error` |
| `trajectory` (normalize) | trace collected | `normalize_trace.py` | `--trace-id <id> spans.json` | `spans.json` from the trace store |
| `routing` | `expect.route` or `expect.route_acceptable` | `score_routing.py` | `routing_results.jsonl [--oos-route X]` | **run-level**, once, after all cases (§5.2) |
| `trajectory` | `expect.tools` | `trajectory_match.py` | `trajectory.json expect.json [--fail-on-errored-calls]` | `trajectory.json`, `expect.json` |
| `tool_selection` | `expect.args` | `score_args.py` | `trajectory.json expect.json` | same two files |
| `loops` | trace collected | `detect_loops.py` | `trajectory.json [--repeat-threshold N] [--call-budget N]` | `trajectory.json` |
| `answer` | `expect.answer` or `expect.format` | `score_answer.py` | `answer.txt expect.json` | `answer.txt` (final answer text), `expect.json` |
| `execution` | `expect.result` | `score_execution.py` | `actual.json expect.json [--float-tolerance F]` | `actual.json` per §5.3 |
| `authz` | `expect.authz` | `score_authz.py` | `trajectory.json expect.json [--answer answer.txt] [--id-pattern P]` | `trajectory.json`, `expect.json`, `answer.txt` |
| `rules` | `expect.answer.rules` | *(no script)* | — | `unscored`, `reason: "business rules are evaluated by the skill"` |
| `state` | `expect.state` non-null | *(no script)* | — | `unscored`, `reason: "state-diff needs environment.snapshot_state"` unless the adapter declares one; §5.4 |
| judged | `expect.answer.rubric` | *(no script)* | — | `unjudged (mode: <mode>)` for smoke/targeted; else `unjudged (judge not calibrated)` unless `judge.status == calibrated`, in which case `unjudged (deferred to skill)` |
| `reliability` | `k > 1` | `reduce_repeats.py` | `repeats.jsonl [--k N]` | **run-level**, §5.5 |

**Not applicable ⇒ `"n/a"`. Applicable but disabled ⇒ `"unscorable"` with
`blocked_by` copied from the capability matrix. Applicable, enabled, but its
input could not be produced ⇒ `"unscored"` with a `reason`. None of these three
is ever `pass`, and none is ever `fail`.** The distinction is load-bearing: the
shipped run's `trajectory: unscorable` rows are honest; a `pass` there would
have been a lie, and in the `authz` row it would have been a dangerous one.

### 5.1 Invoking a scorer

```python
proc = subprocess.run(
    [sys.executable, str(scripts_dir / script), *argv],
    capture_output=True, text=True, timeout=plan["scoring"]["scorer_timeout_s"],
)
```

- **Parse `proc.stdout` as JSON on every return code, including 2.** The
  scorers' error contract puts errors on **stdout** as `{"error": "..."}` and
  signals failure with exit 2 (`_common.die`). A runner that reads stderr on
  failure gets an empty string and reports nothing.
- `rc == 0` → store the parsed object verbatim as that layer's value in
  `verdict.json` under `layers.<name>`. The runner reads only `verdict` out of
  it for rollup; it invents no fields and drops none.
- `rc == 2` → layer verdict `error`, with `scorer`, `argv`, and the scorer's
  `error` string recorded. The case rolls up to `unscored`. Increment
  `summary.scorer_errors`. The run **continues** (one malformed `expect` must
  not throw away 200 cases of spend) but exits 7 at the end (§11), because a run
  with scorer errors has numbers nobody should quote.
- Timeout or unparseable stdout → same as `rc == 2`, with the reason recorded.

### 5.2 `score_routing.py` is run-level

It scores a JSONL of all cases at once and reports macro/micro F1, a confusion
matrix, OOS metrics, and `spurious_labels`. So: the runner accumulates one row
per routing-applicable case as cases complete, writes
`<out>/routing_results.jsonl`, and invokes the scorer **once**, after the last
case, before finalize.

Row shape, with the field mapping `run/SKILL.md` §3 specifies:
`{"case_id", "expected": <case.expect.route>, "observed": <observed>,
"acceptable": <case.expect.route_acceptable>, "clarify_ok", "clarified"}`.
A null `observed` is written as `null` and the scorer maps it to `__no_route__`.

The run-level report goes to `<out>/routing_report.json`; each case's
`layers.routing` gets the per-case row plus the verdict derived from it.

`--oos-route` is passed only when `scoring.oos_route` is non-null **and** at
least one selected case carries that route label — otherwise the scorer exits 2
and lists the labels present (Step 3 made it do that). The runner performs that
check itself rather than discovering it from an exit code.

**Trace-less routing.** With no trace there is no observed route. The runner
does **not** infer one from the HTTP status. The shipped run did infer it
(`"route inferred from HTTP status only (200=Units/answered, 400=none)"`), which
is a defensible run-specific judgement and exactly the kind of judgement a
runner must not make silently across every app. Instead: `layers.routing` is
`unscored`, `reason: "no trace; observed route unavailable"`, and the case is
excluded from `routing_results.jsonl`. An app that wants status-derived routing
declares it in the adapter — see Open decision **D4**.

### 5.3 `actual.json` for the execution layer

Per `adapter-contract.md`'s result-extraction contract, in priority order:
tool-result span → structured response field → declared prose pattern. The
runner writes exactly what the scorer expects:
`{"scalar": v}` | `{"rows": [...]}` | `{"missing": true, "reason": "..."}`.
`expected` is never computed at run time — it is already on the case as
`expect.result`, and `reference_query` is provenance the runner never executes.

### 5.4 What the runner refuses to do

Business rules, state-diff, and judged layers have no script. The runner records
them `unscored`/`unjudged` with a reason and **never** attempts them. An LLM
step may fill them in afterward by rewriting `verdict.json`; the runner's own
output is deterministic and reproducible without a model.

### 5.5 Repeats (`k > 1`)

Each repeat is a full independent invoke + score. The runner accumulates
`<out>/repeats.jsonl` (`{"case_id", "verdict"}`, k rows per case) and invokes
`reduce_repeats.py` once at the end into `<out>/reliability.json` for the
pass@k / pass^k gap.

The **case-level** verdict for k>1 is `pass^k`: pass only if every repeat
passed. Rationale: the hard-gated modes that use k>1 are asking for reliability,
and a case that passes 2 of 3 is not a case that passes. The gap is reported,
never hidden.

---

## 6. The output tree

Everything below is written under `--out`. This is the complete list; the
runner writes no other files.

```
reports/
  .gitignore                     # only when data.may_contain_pii; contents: */cases/
  <run-id>/
    manifest.yaml                # §10. Written at pre-flight, before any case.
    results.json                 # §10. Written at pre-flight (status: running), rewritten per case, finalized at end.
    verdicts.jsonl               # §9. Rewritten from the case dirs after every case.
    verdicts_for_stats.jsonl     # §9. Same, filtered.
    routing_results.jsonl        # only when >=1 case is routing-scorable
    routing_report.json          # only when routing_results.jsonl exists
    repeats.jsonl                # only when k > 1
    reliability.json             # only when k > 1
    run.log                      # append-only, one JSON object per line (§12)
    cases/
      <case-id>/
        request.json             # §10
        response.json            # §10
        verdict.json             # §10 — written LAST, atomically. Its existence means "this case is done."
        expect.json              # the case's `expect` object, as fed to the scorers
        answer.txt               # the final answer text, as fed to score_answer/score_authz
        trajectory.json          # only when a trace was collected and normalized
        actual.json              # only when expect.result is present
        trace.json               # only when traces.source is queryable this run
        repeats/<n>/             # only when k > 1; n = 1..k
          request.json  response.json  verdict.json
```

**Rules that make this tree unambiguous:**

- `repeats/` exists **iff** `k > 1`. When it exists, it holds every repeat
  including the first — no asymmetry between "the run" and "the extra runs".
- With `k > 1`, the case-level `request.json`/`response.json` are the
  **representative** repeat: the first *failing* repeat if any repeat failed,
  else repeat 1. That is deterministic, and it means the report's example
  excerpt is the informative one. `verdict.json` at case level is always the
  reduced verdict and carries `repeats: [{n, verdict, layers}...]`.
- `verdict.json` is written last, via temp-then-rename. **A case directory
  containing `verdict.json` is complete, by definition.** Resume (§8) and the
  completeness check (§9) both key off exactly this fact.
- Every write in this tree is `write to <name>.tmp.<pid> → os.replace()`. Never
  a partial file visible mid-write, never a batch at run end.
- `trace.json` absent is never ambiguous: when it is absent, `verdict.json`'s
  `trace` block says whether a trace was expected and why there isn't one.
- Holdout cases get ordinary directories here (per `run/SKILL.md` §2/§4). The
  seal is enforced at §10's `results.json`, not by withholding files.
- Canary cases get ordinary directories too, with `canary: true` in
  `verdict.json`, and are excluded from every denominator in `results.json`.

**Holdout ledger.** When `selecting_split` is `holdout` or `full`, the runner
appends one line — `{run_id, date, mode, reason: "run"}` — to
`paths.holdout_ledger` and prints the running count. The runner does this, not
the skill: it is the only component that knows for certain the selection touched
sealed ids, and an uncounted holdout run makes the N=5 reseal trigger a number
nobody is keeping. `holdout_ledger: null` with a holdout-touching mode is exit 2
at plan validation.

---

## 7. Execution semantics and the infra taxonomy

**Serial.** One case at a time, in the plan's case order, canaries first.
`adapter.invocation.max_concurrency > 1` is **ignored with a logged warning** in
v1 — see Open decision **D5**.

**Verdict vocabulary**, unchanged from the rest of the harness:
`pass | fail | unscored | skipped | infra_error | infra_incomplete`.

**Attempt loop, per repeat:**

| Condition | Handling |
|---|---|
| connection error, timeout, HTTP 429, HTTP 5xx | retry up to `max_attempts` with `backoff_s`; on exhaustion → `infra_error`, `retry_count` recorded in `response.json` |
| HTTP 4xx other than 429 | **not** retried — it is a response, and often the expected one (the field test asserts a deliberate 400 on OOS) |
| app crash / connection reset on a `noise` or `adversarial-refusal` case | `infra_error` **and** increments `summary.crash_rate`'s numerator — a first-class number, not just an excluded row |
| trace requested, quiescence not reached by `max_wait_s` | `infra_incomplete`; the case's non-trace layers still score |
| `normalize_trace.py` reports missing spans / orphans | `infra_incomplete` for trace-dependent layers only |
| adversarial case with `environment.safe_to_attack: false` | `skipped`, reason recorded — never `fail` |
| multi-turn case with no `invocation.session` contract | `skipped`, reason recorded (adapter hard rule 3) |

**Infra and `skipped` verdicts never enter pass/fail denominators.** They are
counted separately and reported.

**Abort conditions, mid-run:**

- A **canary** scoring wrong → stop immediately, exit 4. Everything completed so
  far stays on disk and finalize (§9) still runs, so the partial run is a
  well-formed artifact — but nothing from it is trustworthy and `results.json`
  says so in `summary.status: "aborted_canary"`.
- Infra verdicts exceed `execution.infra_rate_abort` of attempted cases (checked
  after each case, once at least 8 cases have been attempted) → stop, exit 5,
  `summary.status: "aborted_infra"`. Burning a full suite against a down
  service produces an expensive way of learning the service is down.

**Gating failures do not affect the exit code.** `run/SKILL.md` §5 is explicit
that a separate tokenless shell step reads `results.json` and sets the merge
gate. A runner that exited 1 on a red suite would make the gate script's own
check dead code and quietly move the gate into the runner. The plan's `gate`
field is recorded in the manifest and otherwise unused here.

---

## 8. Resume

`--resume` into an existing `--out`:

1. Read `<out>/manifest.yaml`. If its `plan_sha256` differs from the SHA-256 of
   the current plan's canonical JSON, exit 2: `plan changed since <run-id>
   started; start a new run id`. Resuming a different suite under an old
   manifest produces a run that is comparable to nothing.
2. Walk `<out>/cases/`. A case with a parseable `verdict.json` is **done** and is
   not re-invoked. A case directory without one is deleted and re-run — a
   half-written case is cheaper to redo than to reason about.
3. Rebuild the in-memory verdict list from those directories, then rewrite
   `verdicts.jsonl` and `verdicts_for_stats.jsonl` from it (§9) before invoking
   anything.
4. Continue with the first not-done case in plan order.

Without `--resume`, a non-empty `--out` is exit 2. The runner never silently
merges into or overwrites an existing run.

---

## 9. Completeness — the section that exists because of the shipped run

The failure this contract must make impossible is not "the LLM forgot". It is
"the run reported success while a required file was absent, and nothing looked".
Three mechanisms, in order of strength:

**(a) Derived, not accumulated.** `verdicts.jsonl` and
`verdicts_for_stats.jsonl` are **regenerated in full from the completed case
directories after every case completes**, temp-then-rename. They are not built
up at run end, and they are not appended to. Consequences: an interrupted run
still has both files, correct as of its last completed case; a resumed run
cannot double-count; and the two files cannot disagree with the case dirs,
because the case dirs are their only source. The cost is O(n²) writes over a run;
n is tens to hundreds of cases and each row is small, so the correctness is worth
more than the writes. (The rows are held in memory during a single run; disk is
re-read only at `--resume` start.)

- `verdicts.jsonl`, one row per case, the durable record — holdout and canary
  cases included:
  ```json
  {"case_id": "c-3f9a2c1d", "set": "smoke", "category": "happy", "canary": false,
   "gating": true, "verdict": "pass",
   "layers": {"http": "pass", "routing": "unscored", "answer": "pass",
              "trajectory": "unscorable", "execution": "n/a", "authz": "n/a"}}
  ```
  `layers` here is flattened to verdict **strings**; the full scorer objects live
  in the case's `verdict.json`. `set` is the plan's `selecting_split`.
- `verdicts_for_stats.jsonl`, the same cases reduced to
  `{"case_id", "verdict"}` with every row whose verdict is not exactly `pass` or
  `fail` dropped. This is precisely what `stats.py` pairs, and `stats.py` treats
  a third value as a hard error rather than filtering silently — so the filtering
  stays a visible step and the unfiltered record survives beside it.

**(b) A declared required-artifact table.** 5b defines one constant:

```python
REQUIRED_ALWAYS = ("manifest.yaml", "results.json", "verdicts.jsonl",
                   "verdicts_for_stats.jsonl")
REQUIRED_PER_CASE = ("request.json", "response.json", "verdict.json",
                     "expect.json", "answer.txt")
REQUIRED_IF = {"routing_results.jsonl": "any case is routing-scorable",
               "routing_report.json":   "routing_results.jsonl exists",
               "repeats.jsonl":         "k > 1",
               "reliability.json":      "k > 1",
               "trajectory.json":       "a trace was normalized for this case",
               "actual.json":           "expect.result is present on this case",
               "trace.json":            "traces.source is queryable this run"}
```

A 5b test asserts this table matches §6 of this document. `report.md`/`.html`
are deliberately **not** here: the skill writes them (§13).

**(c) A finalize check that can fail the run.** Before writing the terminal
`results.json` and returning, the runner:

1. verifies every `REQUIRED_ALWAYS` file exists, is non-empty, and parses;
2. verifies every completed case directory has every `REQUIRED_PER_CASE` file,
   plus each `REQUIRED_IF` file whose condition held for that case;
3. verifies `len(verdicts.jsonl) == len(cases attempted)` and that
   `verdicts_for_stats.jsonl` is exactly the `pass`/`fail` subset;
4. verifies `results.json`'s per-case rows and `summary` counts agree with
   `verdicts.jsonl`.

Any discrepancy → the missing/mismatched items are written to
`results.json.summary.missing_artifacts`, printed as JSON on stdout, and the
runner **exits 6**. `summary.status` becomes `"incomplete"`, never `"ok"`.

`summary.status: "ok"` is written **only** after this check passes. So "the run
finished" and "the run's required outputs exist" become the same statement, and
`--verify <dir>` re-asserts it on any run directory at any later time — including
the shipped one, which is expected to exit 6 and name both missing files.

---

## 10. File shapes

### `manifest.yaml`

JSON bytes in a `.yaml` file — JSON is a subset of YAML 1.2, so `yq -r '.run_id'`
in the CI example reads it unchanged, and stdlib has no YAML writer. This is what
the shipped run already does. See Open decision **D1**.

```jsonc
{
  "run_id": "smoke-20260818T183920Z",     // top-level key, not just the directory name (§5 CI gate reads it here)
  "mode": "smoke", "k": 1, "gate": "soft", "selecting_split": "smoke",
  "started_at": "2026-08-18T18:39:20Z",
  "harness_version": "0.1.0",             // from _common.HARNESS_VERSION, never invented by the caller
  "plan_sha256": "…",                     // canonical JSON of the plan; resume compares this
  "runner_version": 1,
  "dataset_version": 1,
  "cases": [{"id": "…", "set": "smoke", "sha256": "…"}],   // sha256 of the case's canonical JSON
  "app": { … },                           // from manifest_extra
  "prompt_snapshot_hashes": { … },
  "judge": { … }, "rubric_versions": "…",
  "environment_kind": "live-readonly", "safe_to_attack": false,
  "temperature": "…",
  "identity_headers": {"names": ["X-User-Id", "X-Role-Id"], "source": "env"},  // names only, never values
  "traces": {"source": "otlp-file", "correlation": "none", "collected": false,
             "disabled_layers": ["trajectory", "tool_selection", "loops", "cost_latency"]},
  "capability_matrix": { … },
  "scoring": { … }, "execution": { … },
  "cost_estimate": { … }
}
```

### `cases/<id>/request.json`

```jsonc
{"case_id": "…", "repeat": 1, "persona": "…",
 "headers_sent": {"Content-Type": "application/json", "X-User-Id": "<redacted>"},
 "url": "https://…/api/chat/ask", "method": "POST",
 "body": { … },                       // the literal request body sent
 "sent_at": "2026-08-18T18:40:02Z"}
```

### `cases/<id>/response.json`

```jsonc
{"case_id": "…", "repeat": 1, "status": 200, "latency_s": 6.68,
 "attempts": 1, "retry_count": 0,
 "body": { … },                       // raw parsed body, or {"raw": "<text>"} if not JSON
 "answer": "…",                       // the extracted final answer text (also written to answer.txt)
 "trace_id": null}
```

### `cases/<id>/verdict.json`

```jsonc
{"case_id": "…", "set": "smoke", "category": "happy", "canary": false,
 "holdout": false, "gating": true,
 "verdict": "pass",
 "k": 1,
 "layers": { "http":   {"layer": "http", "verdict": "pass", "status": 200},
             "answer": { …verbatim score_answer.py output… },
             "trajectory": {"layer": "trajectory", "verdict": "unscorable",
                            "blocked_by": "no trace-id correlation"},
             "authz": {"layer": "authz", "verdict": "n/a"} },
 "trace": {"expected": true, "collected": false,
           "reason": "traces.correlation: none — heuristic matching forbidden"},
 "repeats": null,                     // or [{"n": 1, "verdict": "pass", "layers": {…}}, …] when k > 1
 "notes": ""}                         // free text for investigation context; runner writes "", humans may edit
```

**Case verdict rollup, in this order — the first match wins:**

1. any layer `infra_error` → `infra_error`
2. any layer `infra_incomplete` → `infra_incomplete`
3. the case was skipped by a safety gate → `skipped`
4. any applicable, enabled layer `fail` → `fail`
5. at least one layer `pass` → `pass`
6. otherwise → `unscored`

Rule 6 is the point: a case whose every layer came back `n/a`/`unscorable`/
`unscored` is **not** a pass. That is the vacuous-case failure
`validate_cases.py` lints for at authoring time, caught again at run time.

`gating` is `true` unless the case is a canary. `summary.gating_failures` counts
cases with `gating: true` and `verdict: "fail"`.

### `results.json`

The machine-readable summary §5's CI gate reads. `run_id`, `harness_version` and
`dataset_version` are echoed from the manifest so a gate script never opens two
files to check they match.

```jsonc
{"run_id": "…", "harness_version": "0.1.0", "dataset_version": 1, "mode": "smoke",
 "cases": [{"case_id": "…", "verdict": "pass", "gating": true,
            "layers": {"http": "pass", "answer": "pass", "trajectory": "unscorable"},
            "latency_s": 6.68}],
 "summary": {"status": "ok",           // running | ok | incomplete | aborted_canary | aborted_infra
             "n": 4, "attempted": 4, "passes": 4, "failures": 0,
             "gating_failures": 0,
             "unscored": 0, "skipped": 0,
             "infra_errors": 0, "infra_rate": 0.0,
             "crash_rate": 0.0,
             "scorer_errors": 0,
             "unscorable_layers": ["trajectory", "tool_selection", "loops"],
             "unjudged": "mode: smoke",
             "canaries": {"n": 2, "passed": 2},
             "holdout": null,          // or {"n": 2, "passes": 1, "gating_failures": 1} — aggregate ONLY
             "missing_artifacts": []},
 "exit_code": 0}
```

**The holdout seal, concretely:** a holdout case contributes **no row** to
`results.json.cases`. Its `case_id` appears nowhere in this file. It appears only
inside `summary.holdout` as counts. `verdicts.jsonl` (the run's own durable
record, not the shareable summary) does carry it, since a paired diff needs it.

---

## 11. Exit codes

| Code | Meaning | Artifacts on disk |
|---|---|---|
| 0 | Ran to completion; §9's completeness check passed. Says **nothing** about pass/fail. | complete |
| 1 | Unhandled internal error. Traceback to stderr, `{"error": …}` to stdout. | whatever completed |
| 2 | Bad input or usage: malformed plan, unknown key, duplicate case id, non-empty `--out` without `--resume`, plan/manifest mismatch on resume. | none written |
| 3 | Pre-flight abort: unresolved env vars, health check failed, trace declared-but-not-joining, old-layout state dir, unimplemented `invocation.mode`. | manifest only, or nothing |
| 4 | Canary failed — harness/judge drift; run stopped. | complete for cases finished |
| 5 | Infra rate exceeded `infra_rate_abort`; run stopped. | complete for cases finished |
| 6 | **Completeness check failed** — a required artifact is missing or inconsistent. `summary.missing_artifacts` names them. | incomplete, by definition |
| 7 | At least one scorer returned exit 2. The run finished and its artifacts are complete, but its numbers are not quotable. | complete |

2 matches the scorers' own "bad input" code, and like them the runner prints
`{"error": "..."}` as JSON **on stdout** for every non-zero exit, so a caller
parses one shape regardless of outcome.

A red suite exits **0**. That is deliberate (§7).

---

## 12. `run.log`

Append-only JSONL, one object per line:
`{"ts", "event", …}`, with events `preflight`, `case_start`, `case_done`,
`retry`, `scorer`, `abort`, `finalize`. Written for post-hoc debugging of a run
that went wrong; nothing reads it programmatically, and nothing in §9 requires
it. Keeping it out of `REQUIRED_ALWAYS` is intentional — a required file nobody
consumes is how required files rot.

---

## 13. Explicitly out of scope for `run_cases.py`

Named here so 5b does not grow them, and so 5c knows what stays in the skill:

- **Case selection and mode semantics.** The skill resolves `--smoke` etc. into
  `cases` + `selecting_split` + `k` + `gate`.
- **YAML.** The skill converts; the runner reads JSON only.
- **Cost estimation and the "Proceed?" prompt.**
- **`report.md` / `report.html`.** The skill writes the markdown and runs
  `md_to_html.py`. The runner produces every number those documents quote, but
  a failure-cluster narrative is a judgement call, which is the one thing the
  LLM in this loop is actually for.
- **The baseline comparison.** The runner emits `verdicts_for_stats.jsonl`; the
  skill reads `reports/baseline.json`, checks dataset/harness version equality,
  and runs `stats.py`. (See Open decision **D3**.)
- **Judged layers, business rules, state-diff.** §5.4.
- **Pinning `reports/baseline.json`.** A skill decision, not an execution step.

---

## Open decisions

These are the choices I made where a different one is defensible. I would like
each confirmed (or overridden) before 5b writes code, since several change the
shape of the tests as much as the shape of the runner.

**D1 — `manifest.yaml` holds JSON bytes.** *Chosen:* keep the filename
`manifest.yaml` (named by `run/SKILL.md` §1/§5, the CI example's
`yq -r '.run_id'`, and the shipped run) and write JSON into it, since JSON is
valid YAML 1.2 and stdlib cannot emit YAML. *Trade-off:* a file whose extension
lies a little; anyone opening it expecting block YAML is briefly confused.
*Alternative:* rename to `manifest.json` and update ~6 references across
`run/SKILL.md`, `run-modes.md`, `migrate-run-layout.md`. **Confirm: keep the
name, or rename?**

**D2 — Trace-less runs score no routing layer.** *Chosen:* refuse to infer a
route from the HTTP status. *Trade-off:* the field-test app is trace-less and
status *is* its only routing observable, so this makes its most interesting
layer unscorable — a real regression against what the shipped run reported by
hand. *Alternative:* an adapter block, e.g.
`invocation.route_from_status: {200: "<answered>", 400: "__oos__", 403: "denied"}`,
which makes the inference declared and per-app rather than universal and
implicit. I lean toward adding it, but it is an adapter-contract change and
therefore not free. **Confirm: unscored in v1, or add the adapter block now?**

**D3 — The runner does not run `stats.py`.** *Chosen:* it emits
`verdicts_for_stats.jsonl` and stops. *Trade-off:* the baseline diff — a thing
with real rules (same dataset_version *and* harness_version, attrition warnings,
"within noise" phrasing) — stays in LLM hands, which is the category of thing
this whole step exists to take out of LLM hands. *Alternative:* a
`--baseline-verdicts PATH` flag that shells out to `stats.py` and writes
`comparison.json`. That is maybe 30 lines and closes the loop. **Confirm: leave
comparison to the skill, or fold it in?**

**D4 — Seven exit codes.** *Chosen:* distinct codes for pre-flight (3), canary
(4), infra (5), incompleteness (6), and scorer errors (7). *Trade-off:* more
surface than a 0/1/2 contract, and a CI script that only checks `!= 0` gains
nothing. *Why anyway:* 6 is the one that makes the shipped run's failure
mode nameable, and once 6 exists the others cost nothing to distinguish.
**Confirm, or collapse 4/5/7 into 1?**

**D5 — Concurrency is ignored, not honored, in v1.** *Chosen:* always serial;
`max_concurrency > 1` logs a warning and proceeds serially. *Trade-off:* a
50-case regression run against a slow app is slow, and the adapter field looks
supported when it is not. *Alternative:* exit 2 on `max_concurrency > 1` so the
gap is loud rather than quiet. **Confirm: warn-and-serialize, or refuse?**

**D6 — HTTP-only invocation in v1.** *Chosen:* `function` and `cli` modes exit 3
with a clear message. *Trade-off:* `adapter-contract.md` advertises all three,
and `function` mode is described there as *preferred* when auth/HTTP is in the
way — so v1 does not implement the recommended path. *Mitigation:* `function`
mode is a `runpy`/`importlib` call and is genuinely small; it could go in 5b if
you want the contract honored on day one. **Confirm: HTTP-only, or HTTP +
function?**

**D7 — `k > 1` rolls up as pass^k at case level.** *Chosen:* strict — a case
passes only if all k repeats pass; pass@k is reported alongside via
`reduce_repeats.py`. *Trade-off:* `verdicts_for_stats.jsonl` then pairs pass^k
verdicts, so a baseline run at k=1 and a candidate at k=3 are not comparable
even at the same dataset and harness version. Should the runner refuse that
comparison outright, or is recording `k` in the manifest enough for the skill to
catch it? **Confirm: pass^k rollup, and where the k-mismatch check lives.**

**D8 — The runner appends to the holdout ledger.** *Chosen:* the runner writes
it, since it is the only component that knows the selection touched sealed ids.
*Trade-off:* the runner now writes outside its own `--out` directory, which
breaks the otherwise-clean "a run directory is self-contained" property and adds
a second writer to a file `analyze --unseal` also appends to (no locking; two
concurrent writers could interleave). **Confirm: runner writes the ledger, or
runner reports `holdout_look: true` in `results.json` and the skill appends?**

**D9 — `answer.txt` and `expect.json` are required per-case artifacts.** *Chosen:*
keep the scorer input files in the case directory rather than in a temp dir, so
any run can be re-scored without re-invoking the app — which is also how a
scorer bug fix gets applied to historical runs. *Trade-off:* `expect.json`
duplicates the case file's content into every run directory, and the two can
drift if someone edits the case afterward (mitigated: `manifest.cases[].sha256`
records what was actually run). The shipped run already wrote both files.
**Confirm.**
