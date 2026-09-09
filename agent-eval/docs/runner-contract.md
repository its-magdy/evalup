# `run_cases.py` — Runner Contract

**Status:** the nine open decisions below were **confirmed 2026-09-08**; this
document has been rewritten to match them, and **every section is implemented**
by `scripts/run_cases.py` (Steps 5b-i and 5b-ii; tests in
`tests/test_run_cases.py`). Step 5c rewired `run/SKILL.md` around the result:
the skill now builds `plan.json` (§2), reads the seven exit codes (§11), and
keeps only §13's list.

Where 5b-ii found this document silent, the section says so and gives the
answer: how an observed route is read (§5.2), how `actual` is extracted
(§5.3), which repeats `reduce_repeats.py` may be handed (§5.5), and when the
baseline comparison's version and `k` checks run (§5.6). All four are settled
by **declaration in the adapter**, never by inference in the runner — the same
line 5b-i drew when the contract turned out not to say how an HTTP request is
built.

This document is the spec: where it and the code disagree, **this file is
right** and the code is the bug.

**What this is.** `run/SKILL.md` §2 "Execute" used to be hand-orchestrated by
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
                             [--baseline-verdicts <prev>/verdicts_for_stats.jsonl]
run_cases.py --verify <reports/<run-id>>
```

| Flag | Meaning |
|---|---|
| `--plan PATH` | the single JSON input document (§2). `-` reads stdin. Required except with `--verify`. |
| `--out DIR` | the run directory to write. Must equal `<state>/reports/<run_id>` where `run_id` is the plan's. Required except with `--verify`. |
| `--resume` | continue an interrupted run into an existing `--out` (§8). Without it, a non-empty `--out` is exit 2. |
| `--verify DIR` | run **only** the completeness check (§9) over an existing run directory and exit. No app calls, no scoring, no writes. |
| `--dry-run` | plan validation + pre-flight (§4) + print the resolved case list and cost inputs; write nothing, call the app zero times. The health check (§4.4) is an app call, so it is the one pre-flight step `--dry-run` skips; everything else still runs, so a dry run still fails on an unresolved env var, an old-layout state dir, or a malformed body template. |
| `--baseline-verdicts PATH` | a previous run's `verdicts_for_stats.jsonl`. Shells out to `stats.py` after scoring and writes `comparison.json` (§5.6). Decision **D3**. |
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
    "holdout_ledger": "datasets/holdout-looks.jsonl"    // .jsonl sidecar, relative to state_dir; null = runner refuses --holdout/--full (§6)
  },

  "adapter": { ... },        // adapter.yaml converted to JSON, VERBATIM, env refs UNRESOLVED (§3)
  "capability_matrix": {     // profile.yaml's block, verbatim
    "routing":        {"enabled": true},
    "trajectory":     {"enabled": false, "blocked_by": "no trace-id correlation"},
    "tool_selection": {"enabled": false, "blocked_by": "stage: pre-stability"},
    "cost_latency":   {"enabled": false, "blocked_by": "..."},
    "multi_turn":     {"enabled": false, "blocked_by": "reserved: no conversation driver in the harness"},
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

**Required vs. defaulted.** `plan_version`, `run_id`, `mode`, `k`, `gate`,
`selecting_split`, `paths`, `adapter`, `capability_matrix` and `cases` are
required. `scoring`, `execution` and `manifest_extra` may be omitted and are
filled from the defaults shown above. `capability_matrix` is **not** in that
second list, for the same reason `validate_cases.py` made `--capabilities`
required: absent, every layer counts as enabled and the run records a matrix
nobody chose.

**Validation, all before any spend, all exit 2 with `{"error": ...}` on stdout:**
unknown top-level key; unknown key inside `scoring` or `execution`;
`plan_version != 1`; `run_id` not matching
`^[a-z]+-\d{8}T\d{6}Z$`; `--out` basename != `run_id`; `k < 1`; empty `cases`;
duplicate case `id`; a case whose `split` does not contain `selecting_split`
(the skill selected wrong — the runner refuses to run a set it cannot label);
`backoff_s` length mismatch; `infra_rate_abort` outside `(0, 1]`;
`scripts_dir` missing any of the nine scorer
scripts §5 names; a holdout-touching mode with `holdout_ledger: null`, or one
whose path is not `.jsonl` (§6); and
**`adapter.invocation.max_concurrency > 1`** (decision **D5**).

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
2. `invocation.mode`: `http` and `function` are implemented (decision **D6** —
   `adapter-contract.md` calls `function` the *preferred* mode when auth or
   HTTP is in the way, so v1 honours the contract it advertises). `cli` exits 3
   with `runner v1 implements invocation.mode: http and function only (got:
   cli)`.
   **`function` mode does not enforce `execution.timeout_s`**: an in-process
   call cannot be interrupted from the stdlib without leaving the thread
   running behind it, and reporting "timeout" while the callable kept executing
   would be a lie about the state of the world. The callable owns its own
   timeout; `manifest.invocation.timeout_enforced` records `false` so no reader
   has to infer it.
3. **Old-layout check.** If `<state>/reports/baseline.json` is absent but a
   sibling `baselines/` or `runs/` exists, exit 3 with the migration message
   from `run/SKILL.md` §4. This check lives here, not in the skill, because it
   must fire before spend. (The field-test state dir is exactly this case.)
4. **Health check**: one trivial request through the adapter. With no declared
   `invocation.health_check`, this is a `GET` of `base_url` and **any** response
   counts as reachable — including a 404. The question here is "is the host
   up", and inventing a plausible API call instead would both spend money and
   guess at a shape the adapter never declared. A transport failure or timeout
   is exit 3. An adapter that wants a real check declares
   `invocation.health_check: {method, path, expect_status: [...]}`, and a
   status outside `expect_status` is exit 3. In `function` mode the health
   check is the entrypoint import, which is also why an unimportable entrypoint
   is exit 3 with nothing billed rather than N identical infra errors.
5. **Trace branch**, off `adapter.traces`. The queryable set in v1 is
   **`otlp-file` only**; `jaeger`, `tempo` and `clickhouse` each need their own
   client and exit 3 with
   `runner v1 queries traces.source: otlp-file only (got: 'jaeger'); declare
   view-only to run trace-less instead`. Refusing loudly matters more here than
   elsewhere: silently treating an unimplemented store as "no trace" would
   downgrade a fully instrumented app to trace-less scoring and report the
   result as normal.
   - `source` in the queryable set **and** `correlation` explicit → verify a
     trace arrives and joins on the health-check call. No join → exit 3. That
     verification needs a real app call to join *on*, so it requires a declared
     `invocation.health_check`; without one the runner exits 3 asking for it
     rather than recording an unverified join as verified.
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

Four rules make that mechanical:

- **Every layer in the table gets a row on every case**, `n/a` included. An
  absent row and an `n/a` row are not the same statement, and "this case does
  not assert authz" is worth saying out loud — a reader who has to infer it
  from an absence will eventually infer it wrongly.
- **Enabled is `capability_matrix[<layer>].enabled is not false`**, looked up
  under the layer's own name. `blocked_by` is *copied*; `unscorable` means
  somebody decided this layer is off and said why, so a reason the runner
  invented would let the matrix and the report disagree about that why.
- The decision order per layer is **not applicable → disabled → input
  unavailable → invoke the scorer**, and a **refusal** (safety gate) or a
  **transport failure** stops before all of it. Scoring a case the harness
  declined to run, or one the app never answered, would manufacture a failure
  the app never had.
- The scorer's object is stored **verbatim** under `layers.<name>`, so
  `layers.tool_selection` holds an object whose own `"layer"` field reads
  `"args"` — `score_args.py`'s name for itself. That is the scorer's word, and
  rewriting it here to match the key would be the runner editing a scorer's
  output, which is the one thing §5's first constraint forbids.

**A missing answer is `unscored`, not a content failure.** When the response
carries no value at the adapter's declared `<answer>` path, `score_answer.py`
is not invoked: scoring the empty string against `must_contain` would report a
content failure for what is an extraction gap.

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

Row shape, mapping the case's `expect.route` / `expect.route_acceptable` onto
the scorer's `expected` / `acceptable`:
`{"case_id", "expected": <case.expect.route>, "observed": <observed>,
"acceptable": <case.expect.route_acceptable>, "clarify_ok", "clarified"}`.
A null `observed` is written as `null` and the scorer maps it to `__no_route__`.

The run-level report goes to `<out>/routing_report.json`; each case's
`layers.routing` gets the per-case row plus the verdict derived from it.

`--oos-route` is passed only when `scoring.oos_route` is non-null **and** at
least one selected case carries that route label — otherwise the scorer exits 2
and lists the labels present (Step 3 made it do that). The runner performs that
check itself rather than discovering it from an exit code.

**Where the observed route comes from**, in this order, and every source
declared by the adapter:

1. `invocation.route_from_response: "<dotted path>"` — a structured field in
   the response that names the route. Recorded as `observed_from: "response"`.
2. A collected trace: the **first `invoke_agent` span's `gen_ai.agent.name`**,
   recorded as `observed_from: "trace"`. This is not a guess about the app's
   shape — the adapter declared `traces.convention: gen_ai`, and that
   declaration *is* the statement that agent names live in `gen_ai.agent.name`.
3. `invocation.route_from_status` (decision **D2**, below), recorded as
   `observed_from: "status"`.

With none of the three, `layers.routing` is `unscored` and the case is excluded
from `routing_results.jsonl`. `observed_from` is on every scored routing layer
so no reader has to work out which of the three produced a label.

**`clarify_ok` needs `invocation.clarify_from_response`.** A case with
`expect.clarify_ok: true` and no declared way to observe whether the app
clarified is `unscored`, with that as the reason. Writing `clarified: false`
would be a claim rather than an observation — and on a case whose whole point is
that clarifying is acceptable, that claim decides the verdict.

**Trace-less routing.** With no trace there is no observed route, and the
runner never *infers* one. The shipped run did infer it (`"route inferred from
HTTP status only (200=Units/answered, 400=none)"`), which is a defensible
run-specific judgement and exactly the kind of judgement a runner must not make
silently across every app.

Decision **D2** (confirmed 2026-09-08) makes that judgement **declared and
per-app** instead of universal and implicit: an adapter may carry

```yaml
invocation:
  route_from_status: {200: "<answered>", 400: "__oos__", 403: "denied"}
```

and when it does, the runner maps the observed status through it and scores
routing normally, recording `layers.routing.observed_from: "status"` so no
reader mistakes it for a trace-derived route. Statuses absent from the map
yield no observed route. Without the block, `layers.routing` is `unscored`,
`reason: "no trace; observed route unavailable"`, and the case is excluded from
`routing_results.jsonl`.

The alternative — leaving it unscored in v1 — would take the field-test app's
only interesting layer dark, since status *is* its sole routing observable.

### 5.3 `actual.json` for the execution layer

Per `adapter-contract.md`'s result-extraction contract, in priority order:
tool-result span → structured response field → declared prose pattern. The
runner writes exactly what the scorer expects:
`{"scalar": v}` | `{"rows": [...]}` | `{"missing": true, "reason": "..."}`.
`expected` is never computed at run time — it is already on the case as
`expect.result`, and `reference_query` is provenance the runner never executes.

Each of the three priorities is an **adapter declaration**, because "hunt the
response for the field that looks like a result" is a heuristic that works on
four cases and picks the wrong field on the fifth:

| Priority | Declaration | Reads |
|---|---|---|
| 1 | `invocation.result_from_tool: {tool, field}` | the last matching `execute_tool` span's result (JSON-parsed when the exporter recorded it as a string), optionally at a dotted path |
| 2 | `invocation.result_from_response: "<dotted path>"` | the response body |
| 3 | `invocation.result_pattern: "<regex>"` | the prose answer; group 1 if the pattern has one |

A list becomes `{"rows": [...]}`; anything else becomes `{"scalar": v}`. No
further coercion — `score_execution.py` owns the type-aware comparison
(`7 == "7" == "7.0"`), and a second normalization here would be a second answer
to what equality means. When nothing is declared, or nothing matches, the runner
writes `{"missing": true, "reason": ...}` naming which it was, and the scorer
turns that into `unscored`. An unread result must never be scored as a mismatch:
that manufactures a failure the app never had.

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

**A case contributes to `repeats.jsonl` only when all k of its repeats scored
`pass` or `fail`.** `reduce_repeats.py` refuses any other verdict outright —
counting an infra verdict as a failure would bias the reliability estimate —
and it refuses a case with fewer than `--k` rows. So a partial case would
either poison the reducer or silently lower `k` for every other case in the
run. The excluded cases and their verdicts are named in `reliability.json`
under `excluded_cases`, never dropped quietly: a flakiness number computed over
the cases that happened to answer every time is the one number a reliability
run must not report without saying so. When no case qualifies, the runner
writes `reliability.json` as `unscored` with that reason rather than invoking
the reducer — `reduce_repeats.py` exits 2 on empty input, and a run where every
repeat was an infra error is a run with nothing to reduce, not a scorer error.

Each repeat's scorer inputs are materialized in a **scratch directory outside
the run tree**, and only the representative repeat's are copied in. §6 declares
`repeats/<n>/` as exactly three files and says the runner writes no others, so
an `answer.txt` materialized to score repeat 3 must not land beside them.

The **case-level** verdict for k>1 is `pass^k`: pass only if every repeat
passed. Rationale: the hard-gated modes that use k>1 are asking for reliability,
and a case that passes 2 of 3 is not a case that passes. The gap is reported,
never hidden.

### 5.6 The baseline comparison (decision D3)

`--baseline-verdicts PATH` shells out to `stats.py` after scoring, pairing this
run's `verdicts_for_stats.jsonl` against the given one, and writes
`<out>/comparison.json`. Without the flag the runner emits
`verdicts_for_stats.jsonl` and stops.

**The version and `k` checks run at plan validation, before any spend.** Every
input to them is knowable up front, §11's exit-2 row promises "none written",
and refusing after a full run would spend the suite to learn the comparison was
never going to be legal. Reading them requires the baseline's own
`manifest.yaml` **beside** the verdicts file it names; a baseline without one is
exit 2, because version equality and the `k`-match are the comparison's rules
and there is nowhere else to read them from. Skipping the check when the
manifest is absent would be precisely the silent pass D3 folded this diff into
the runner to prevent.

It is folded in rather than left to the skill because the diff has **rules** —
same `dataset_version` *and* same `harness_version`, attrition warnings, the
"within noise" phrasing — and rules that decide whether a change ships are the
category of thing this whole step exists to take out of LLM hands. The runner
refuses the comparison (exit 2) when either version differs, or when the two
runs used a different `k` (decision **D7**): a baseline at k=1 and a candidate
at k=3 pair `pass^k` verdicts computed over different numbers of trials, so
they are not comparable even at the same dataset and harness version. The
k-mismatch check lives here, in the runner, for the same reason the rest of the
diff does — the manifest recording `k` is only useful if something checks it.

Choosing the baseline (`reports/baseline.json`) stays a skill decision (§13);
the runner only ever compares the two files it is handed.

---

## 6. The output tree

Everything below is written under `--out`. This is the complete list; the
runner writes no other files.

```
reports/
  .gitignore                     # only when: data.may_contain_pii (contents: */cases/)
  <run-id>/
    manifest.yaml                # §10. Written at pre-flight, before any case.
    results.json                 # §10. Written at pre-flight (status: running), rewritten per case, finalized at end.
    verdicts.jsonl               # §9. Rewritten from the case dirs after every case.
    verdicts_for_stats.jsonl     # §9. Same, filtered.
    routing_results.jsonl        # only when: any case is routing-scorable
    routing_report.json          # only when: routing_results.jsonl exists
    repeats.jsonl                # only when: k > 1
    reliability.json             # only when: k > 1
    comparison.json              # only when: --baseline-verdicts was passed
    run.log                      # append-only, one JSON object per line (§12)
    cases/
      <case-id>/
        request.json             # §10
        response.json            # §10
        verdict.json             # §10 — written LAST, atomically. Its existence means "this case is done."
        expect.json              # the case's `expect` object, as fed to the scorers
        answer.txt               # the final answer text, as fed to score_answer/score_authz
        trajectory.json          # only when: a trace was collected for this case
        actual.json              # only when: expect.result is present and the case ran
        trace.json               # only when: a trace was collected for this case
        repeats/<n>/             # only when k > 1; n = 1..k
          request.json  response.json  verdict.json
```

**`# only when:` is machine-read.** Every conditional file above carries its
condition after that exact marker, and the string is the one `REQUIRED_IF`
(§9(b)) stores for that file. A test parses this block and asserts the two are
equal, so a file added here without a check — or a check whose condition drifts
from the documented one — fails the suite. The `.gitignore` line is the one
exception: it is written into `reports/`, not into the run directory, so it is
outside the tree the completeness check walks.

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
- A **skipped** case (§7's safety gates) still writes `request.json` and
  `response.json`, marked `"sent": false` / `"sent_at": null`. `REQUIRED_PER_CASE`
  has no exceptions on purpose — an exception is how a required file rots — and
  the request the runner *would* have sent is the useful thing to read when
  arguing about whether the refusal was right.
- Every write in this tree is `write to <name>.tmp.<pid> → os.replace()`. Never
  a partial file visible mid-write, never a batch at run end.
- `trace.json` absent is never ambiguous: when it is absent, `verdict.json`'s
  `trace` block says whether a trace was expected and why there isn't one.
- Holdout cases get ordinary directories here, like any other case. The seal is
  enforced at §10's `results.json`, not by withholding files.
- Canary cases get ordinary directories too, with `canary: true` in
  `verdict.json`, and are excluded from every denominator in `results.json`.

**Holdout ledger** (decision **D8**). When `selecting_split` or `mode` is
`holdout` or `full`, the runner appends one line —
`{run_id, date, mode, reason: "run"}` — to `paths.holdout_ledger` and writes the
running count to **stderr** (stdout carries the machine-readable `{"error": …}`
payload, and a caller parsing one shape must not find prose there; the count is
also in `results.json`'s `summary.holdout.looks_recorded`). The runner does
this, not the skill: it is the only component that knows for certain the
selection touched sealed ids, and an uncounted holdout run makes the N=5 reseal
trigger a number nobody is keeping. `holdout_ledger: null` with a
holdout-touching mode is exit 2 at plan validation.

**The ledger is a `.jsonl` file**, and a path that is not is exit 2. The
harness's dataset metadata is YAML, but this runner is stdlib-only: appending a
JSON line to a YAML mapping corrupts the document it is meant to append to. A
JSONL sidecar is also what makes the second writer (`analyze --unseal`) safe
without a lock file, since a single short `O_APPEND` line write is atomic on
POSIX, and it is what lets a reader take the running total by counting lines.
`run/SKILL.md` §4 and `analyze/SKILL.md` §`--unseal` — the two writers of the
N=5 count — were pointed at the sidecar in Step 5c; the plan's
`paths.holdout_ledger` is what names it (`datasets/holdout-looks.jsonl` by
convention).

The append happens once, at pre-flight, before the first case — the *look* is
the spend, so a run that aborts halfway has still spent it. `--resume` does not
append again.

---

## 7. Execution semantics and the infra taxonomy

**Serial.** One case at a time, in the plan's case order, canaries first.
`adapter.invocation.max_concurrency > 1` is **refused at plan validation with
exit 2** (decision **D5**), not warned about and downgraded: a warning scrolls
past and leaves the adapter field looking supported while every run is serial.
Exit 2 makes the gap loud at the one moment someone can act on it.

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
| case with more than one user turn | `skipped`, reason recorded (adapter hard rule 3). Multi-turn is **reserved**: no conversation driver exists, so the gate ignores `invocation.session` entirely — declaring one would only buy a last-turn-only invocation scored as a real verdict. |

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

**Gating failures do not affect the exit code.** `run/SKILL.md` §6 is explicit
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
               "comparison.json":       "--baseline-verdicts was passed",
               "trajectory.json":       "a trace was collected for this case",
               "actual.json":           "expect.result is present and the case ran",
               "trace.json":            "a trace was collected for this case"}
```

A 5b test asserts this table matches §6 of this document — it execs the block
above out of this file and diffs it against the module's constants, then parses
§6's tree and diffs the `# only when:` strings against `REQUIRED_IF`'s values.
`report.md`/`.html` are deliberately **not** here: the skill writes them (§13).

Three of these conditions are evaluated per case, and all three are read off the
**case directory**, never off the plan — which is what lets `--verify` evaluate
them months later with no plan in hand. `trajectory.json` and `trace.json` key
on `verdict.json`'s `trace.collected`, not on the run-level "is the store
queryable": a queryable store that never produced this case's trace is
`infra_incomplete` (§7), and demanding the file anyway would turn one honest
infra row into a second, spurious completeness failure. `actual.json` keys on
`expect.json` plus "the case ran", because a case the safety gates **skipped**
made no call and has no result to extract. The run-level conditions read the
manifest (`k`, `baseline_verdicts`) and the case verdicts (routing-scorable =
at least one case whose `layers.routing` scored `pass` or `fail`).

**(c) A finalize check that can fail the run.** Before writing the terminal
`results.json` and returning, the runner:

1. verifies every `REQUIRED_ALWAYS` file exists, is non-empty, and parses;
2. verifies every completed case directory has every `REQUIRED_PER_CASE` file,
   plus each `REQUIRED_IF` file whose condition held for that case;
3. verifies `verdicts.jsonl` holds exactly one row per **completed case
   directory**, with the same ids and the same verdicts, and that
   `verdicts_for_stats.jsonl` is exactly the `pass`/`fail` subset of it;
4. verifies `results.json`'s per-case rows are the non-holdout completed cases
   with matching verdicts, and that its `summary` counts (`n`, `passes`,
   `failures`, `gating_failures`, `unscored`, `skipped`, `infra_errors`,
   `canaries`) agree with a recount over those case directories.

Check 3 says "completed case directory", not "cases attempted": a case the
safety gates **skipped** carries a `verdict.json` and a `verdicts.jsonl` row but
is deliberately not in `summary.attempted` (§7), so equating the two would fail
every run that refused a case. The case directories are the source of truth in
§9(a), so they are what both derived files are checked against here — and being
derivable from the tree alone is what makes checks 3 and 4 runnable under
`--verify`. The run-time bookkeeping counters (`attempted`, `crash_rate`,
`infra_rate`, `scorer_errors`) are **not** recounted: they are properties of the
execution, not of the tree, and a check that has to guess at them would fail
honest runs.

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
  "baseline_verdicts": null,              // or the --baseline-verdicts path. Recorded so §9's
                                          // comparison.json condition is readable off the tree:
                                          // --verify has no plan and no argv.

  "dataset_version": 1,
  "cases": [{"id": "…", "set": "smoke", "sha256": "…"}],   // sha256 of the case's canonical JSON
  "app": { … },                           // from manifest_extra
  "prompt_snapshot_hashes": { … },
  "judge": { … }, "rubric_versions": "…",
  "environment_kind": "live-readonly", "safe_to_attack": false,
  "temperature": "…",
  "identity_headers": {"names": ["X-User-Id", "X-Role-Id"], "source": "env"},  // names only, never values
  "invocation": {"mode": "http", "url": "https://…/api/chat/ask",
                 "timeout_s": 120, "timeout_enforced": true},   // false in function mode (§4.2)
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
 "sent_at": "2026-08-18T18:40:02Z", "sent": true}   // false on a skipped case (§6)
```

### `cases/<id>/response.json`

```jsonc
{"case_id": "…", "repeat": 1, "status": 200, "latency_s": 6.68,
 "attempts": 1, "retry_count": 0, "crashed": false,   // a DROPPED connection, not a timeout: it
                                                      // feeds summary.crash_rate, and it has to
                                                      // live on disk or a --resume loses the count
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
 "latency_s": 6.68,                   // the representative repeat's, so a resumed run rebuilds
                                      // results.json's latency column from verdicts alone (§8)
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
| 3 | Pre-flight abort: unresolved env vars, health check failed, trace declared-but-not-joining or an unimplemented trace store, old-layout state dir, unimplemented `invocation.mode`, unimportable `function` entrypoint, malformed body template. | manifest only, or nothing. The run directory is created **inside** pre-flight, so a pre-flight failure normally leaves nothing at all — an empty `cases/` would make the next attempt at the same run id look like a run in progress. |
| 4 | Canary failed — harness/judge drift; run stopped. | complete for cases finished |
| 5 | Infra rate exceeded `infra_rate_abort`; run stopped. | complete for cases finished |
| 6 | **Completeness check failed** — a required artifact is missing or inconsistent. `summary.missing_artifacts` names them. | incomplete, by definition |
| 7 | At least one scorer returned exit 2. The run finished and its artifacts are complete, but its numbers are not quotable. `summary.status` stays `"ok"` — the artifacts really are complete — and `summary.scorer_errors` is the count. | complete |

**Precedence when more than one applies:** 6 outranks everything (an incomplete
run's other findings are unreadable anyway), then 4/5 (an abort is the fact the
caller must act on, and the scorer errors are still in `results.json`), then 7.

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
- **Choosing** the baseline. The skill reads `reports/baseline.json` and decides
  *which* run to diff against; running the diff itself moved into the runner
  with decision **D3** (§5.6), because the diff's rules — version equality, the
  k-match, attrition, "within noise" — decide whether a change ships.
- **Judged layers, business rules, state-diff.** §5.4.
- **Pinning `reports/baseline.json`.** A skill decision, not an execution step.

---

## Decisions — confirmed 2026-09-08

The nine choices below were open when this contract was written; each is now
answered, and the sections above have been rewritten to match. Kept as a record
of *why*, so a later reader does not reopen a settled question without the
trade-off in front of them.

| # | Question | Answer |
|---|---|---|
| **D1** | `manifest.yaml` holds JSON bytes, or rename to `manifest.json`? | **Keep the name.** JSON is valid YAML 1.2, so `yq -r '.run_id'` in the CI example reads it unchanged and the stdlib (which has no YAML writer) can still produce it. Renaming costs ~6 references across `run/SKILL.md`, `run-modes.md` and `migrate-run-layout.md` for a cosmetic gain. The extension lies a little; the alternative churns three documents. |
| **D2** | Trace-less runs score no routing layer, or add a status→route adapter block? | **Add the block now** (§5.2). Refusing outright takes the field-test app's only interesting layer dark — status *is* its sole routing observable — and a real regression against what the shipped run reported by hand. `invocation.route_from_status` makes the inference **declared and per-app** rather than universal and implicit, which was the actual objection. |
| **D3** | Does the runner run `stats.py`? | **Fold it in** as `--baseline-verdicts` (§5.6). The diff has real rules and they decide whether a change ships; leaving them in LLM hands is the category of thing this step exists to end. ~30 lines, and it closes the loop. |
| **D4** | Seven exit codes, or collapse 4/5/7 into 1? | **Keep seven.** 6 is the one that makes the shipped run's failure mode nameable, and once 6 exists the rest cost nothing to distinguish. A CI script that only checks `!= 0` loses nothing. |
| **D5** | `max_concurrency > 1`: warn and serialize, or refuse? | **Refuse, exit 2** (§7). A warning scrolls past and leaves the adapter field looking supported while every run is serial. |
| **D6** | HTTP-only in v1, or HTTP + function? | **HTTP + function** (§4.2). `adapter-contract.md` calls `function` *preferred* when auth or HTTP is in the way, so shipping without it would mean v1 does not implement its own recommended path. `cli` still exits 3. |
| **D7** | `pass^k` rollup, and where does the k-mismatch check live? | **Keep `pass^k`; the check lives in the runner** (§5.6), alongside the rest of the comparison it guards. Recording `k` in the manifest is only useful if something reads it. |
| **D8** | Who appends to the holdout ledger? | **The runner** (§6). It is the only component that knows for certain the selection touched sealed ids. The self-contained-run-directory property does take a dent; the ledger is now a `.jsonl` sidecar so the append is atomic and stdlib-safe. |
| **D9** | Are `answer.txt` and `expect.json` required per-case artifacts? | **Yes.** Keeping the scorer inputs in the case directory is what lets a run be re-scored without re-invoking the app — which is also how a scorer bug fix reaches historical runs. `expect.json` can drift from an edited case file; `manifest.cases[].sha256` records what was actually run.
