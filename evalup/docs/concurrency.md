# Concurrent case execution — design

**Status:** spec only, written 2026-11-21 for issue #30. Nothing is built.
It follows the decision recorded on #30 that day: concurrency comes after
pacing (#76, PR #79), applies to **response-sourced or trace-less runs behind
a declared rate budget**, and reverses decision D5, so it is specified before
any code. #17 item 2 (PR #77) has landed.

**What does not change.** No scorer, verdict rule, fold, denominator or
`SCORING_VERSION`. With `max_concurrency` absent or 1, nothing sent or
written changes. Concurrency is transport (how evalup reaches an app), which
the 2026-11-19 rule allows; §5 says how that is proven.

Line numbers are `evalup/scripts/run_cases.py` at `1e16839` unless a file
is named.

## 1. Scope

### 1.1 Phase 1 runs only where there is nothing to wait on

`adapter.invocation.max_concurrency: w` with `w > 1` is accepted only when
every condition below holds. Each failing one is **exit 2 at plan
validation**, in `validate_adapter_block` (L535), where D5 refuses today
(L545-555). Refused, never silently serialized: D5's reason still holds for
every configuration Phase 1 does not cover.

| Refused when | Why |
|---|---|
| no `rate_limit.requests` + `window_s` | #30's decision: concurrency only behind a declared budget. A loose budget is how an app with headroom says so |
| `rate_limit.min_interval_s` is set | the interval runs from the previous call's *finish* (`RateBudget`, L599-608), so no two calls may overlap: `w > 1` could only serialize |
| `traces.source` is a queryable store (`QUERYABLE_TRACE_SOURCES`, L275: `otlp-file`) | the quiescence wait watches the whole store (§1.2). Keyed on the declared `source` alone, so an `otlp-file` adapter that would run trace-less is refused too; declare `view-only` instead |
| `invocation.mode: function` | an in-process callable of unknown thread safety (`send_function`, L1276), whose `timeout_s` is not enforced (manifest `timeout_enforced`, L2235) |
| `invocation.conversation.memory: user` | memory outlives a session (`multi-turn.md` §6), so concurrent repeats would contaminate each other in an order no one controls |
| `w` is not an integer in `[1, 8]` | a bound against a typo. A small behaviour change: today a non-integer or 0 passes silently (L549-550) |

Eligible: `traces.source: response` (L278; the trace id is minted per
response, L2272-2275, and `collect_trace` never waits, L2344-2345), and
`view-only`, `none` or absent (L280).

All repeats of a case are sent as one identity (`build_headers` per case,
L2495). An app that allows one request in flight per user may answer the
second with a 409 or 423, which is a non-429 4xx and so scored as the app's
answer (L2517-2522). Open question 2.

### 1.2 Store-traced apps are deferred

`wait_for_quiescence` (L2314-2335) polls the store file's size and mtime until
it is unchanged for `quiescence_ms`. With concurrent writers it never goes
quiet: every call waits the full `max_wait_s`, and spans that land after it
are lost to `infra_incomplete`. `collect_trace` also copies the **whole**
store into `trace.json` (L2356-2361), so a concurrent run's file would hold
other repeats' spans.

A later phase needs, specified there:

- **Quiescence per trace id:** wait until this id's spans are present and
  the lines carrying it have not grown for `quiescence_ms`, capped by
  `max_wait_s`. A new completeness rule, so it needs evidence that it marks
  `infra_incomplete` no more often than the whole-store wait does serially.
- **`trace.json` holds only this id's lines**, which changes serial runs'
  artifact too, so it ships with the per-id wait.
- Then a case's k repeats are sent together and waited on once each, which
  is what #17 item 1 asked for; #17 item 1 closes as superseded then.

### 1.3 The first step: a case's k repeats, not N cases

**Decision: Phase 1 runs a case's k repeats concurrently, `min(w, k)` at a
time. Cases stay serial.**

| | k repeats at once (Phase 1) | N cases at once (later) |
|---|---|---|
| Speedup ceiling | `min(w, k)`: 3× at regression/holdout's default k=3; **none at k=1** (smoke, targeted; `run-modes.md`) | `w`, at any k |
| Case-level writes, `verdicts`, `refresh_derived`, `results.json`, abort check | unchanged: `execute_case` (L2845) still returns one finished case to `execute` (L3905-3915) | need a plan-order commit buffer |
| Overspend after an abort | **none beyond serial**: serial already sends all k repeats before `abort_check` (L3917) | up to `w − 1` cases in flight (§3 rule 3) |
| Canary barrier | automatic: canaries are cases and run first (`ordered_cases`, L2390-2399) | must be explicit |
| Verdict aggregation | `fold_repeats` (L1450) and `repeated_5xx`/`same_4xx` (L1479-1527) read the attempts as a set; `chosen` (L2898) needs repeat order, which a gather by index keeps | the same, plus an order-dependent infra-abort point |
| Conversations | k independent conversations, each serial within | the same |
| Resume unit | the case, unchanged (§3 rule 9) | the case, but completed cases past an abort must be discarded |

Repeats are already independent by contract: "Each repeat is a full
independent invoke + score" (`runner-contract.md` §5.5), each with a fresh
`<uuid>` (L2489, L2671). N cases at once comes only after Phase 1's live
evidence (§7).

## 2. Composition with the rate budget (#76)

### 2.1 Rules

1. **One pacer per run, shared by every worker.** `RateBudget` (L599) gains
   a lock; its docstring's "The runner is serial, so no lock" (L608) goes.
   §1.1 makes a budget mandatory under `w > 1`, so the pacer always exists
   there.
2. **A slot is reserved atomically, and `sent_at` follows it.** Today
   `sent_at` is stamped after a bare `self.pace()` (L2497-2502,
   L2703-2713), and the slot is taken later, in `send_one` → `paced` →
   `RateBudget.call` (L2557-2564, L628-637). Under workers another worker
   could take the slot in between, and `sent_at` would be early, against
   contract §7 ("stamped after the wait"). So `acquire()` (check and take,
   one locked step) moves to those two sites, `sent_at` is stamped after it,
   and `send_one` uses the slot already held; the finish is recorded in
   `release()`.
3. **In-flight calls count against the window.** A call may start at `t`
   only when (calls that finished after `t − window_s`) + (calls in flight)
   `< requests`. That keeps #79's tested property — no window holds more
   than `requests` calls at either end of a call's span.
4. **Retries and conversation restarts take slots the same way.**
   Concurrency adds no call: a repeat still makes at most `max_attempts`
   attempts (× turns for a conversation).
5. **A 429, or a 503 carrying `Retry-After`, pauses every worker.** In the
   same locked `release()` that records the call's finish, the pacer sets
   `paused_until = max(paused_until, finish + wait)` and grants no slot
   before it. `wait` is `retry_wait` (L1086-1104; evalup caps `Retry-After`
   at 60 s, `RETRY_AFTER_CAP_S`, L199) with the attempt's `backoff_s` entry,
   or, on the last attempt (`backoff_s` has `max_attempts − 1` entries,
   L517), the header alone, else none. The worker's own retry waits the
   same deadline. Other 5xx, timeouts and connection errors back off only
   their own worker, as today (L2531, L2801).
6. **Bound per 429:** because rule 5's pause is set inside the locked
   release, no slot is granted after it; only the at most `w − 1` calls
   already in flight can reach the window the app has closed.
7. **The pause never changes a verdict.** Exhausting `max_attempts` is
   `infra_error` as today; `infra_rate_abort` stays the backstop.
8. **Waits are logged after the fact.** A wait blocked on calls in flight
   has no length up front, so a `pace` event's `wait_s` is the time
   `acquire()` actually blocked, and a new `pause` event records rule 5.

### 2.2 When the budget binds, concurrency buys nothing

Under rule 3 a slot is held from its call's start until its finish plus
`window_s`, so the budget's ceiling is about `requests / (window_s + L)`
calls a second, `L` the latency. With `c` one repeat's cycle (latency plus
scoring), serial runs at `1 / c`, and the speedup is about
`min(w, c · requests / (window_s + L))`, which is **1 whenever the budget
already binds the serial run**.

Live evidence: a real app rate-limited to 20 messages per 10 minutes.
Serial is budget-bound for any cycle under about 30 s. 100 cases × k=3 is
300 calls; `RateBudget.floor_s(300)` (L639-651) is `(299 // 20) × 600 =
8400 s` (140 min) at any `w`. Concurrency only compresses each window's
burst of 20, which the window waits out anyway: the run saves at most the
last burst. A 60 s cycle gains about 1.8×.

It helps on **latency-bound runs whose declared budget has headroom**.
`run`'s estimate (`run/SKILL.md` L136-140) already takes `c` from the
per-request latency discover recorded in `findings.md`; it states the
regime by comparing `pace_floor_s` (dry run, L4552-4568) with
`app_calls_planned × c / min(w, k)`.

## 3. Invariants

| # | Rule | Enforced by |
|---|---|---|
| 1 | **Artifact and verdict order is plan order.** Cases run in `ordered_cases` order (L2390); `verdicts` is appended by the case thread (L2923). Repeats are gathered by repeat `n`, never by completion, so `chosen` (L2898), `repeats/<n>/` and `verdict.repeats` are as serial. | gather by index; §6.2 |
| 2 | **Canaries are a serial barrier.** No non-canary repeat is sent before every canary case has a `verdict.json` and passed `abort_check` (L3917-3936). | cases stay serial in Phase 1 |
| 3 | **Bounded overspend after an abort.** Phase 1: zero calls beyond serial, since the abort check follows all k repeats (L3905-3915). A later N-case phase: the pacer closes; at most `w − 1` cases in flight finish, each at most `k × max_attempts × turns` calls, and their directories are deleted. | pacer `close()`; §6.7 |
| 4 | **Every worker is joined before the case thread moves on or re-raises.** On a worker exception or Ctrl-C (the runner has no signal handler) the pacer closes, so no new slot is granted, every future is joined, and only then does `execute_case`'s `finally` remove the scratch dirs (L2919-2921). The case has no `verdict.json` and `--resume` redoes it. | join in `execute_case`; §6.6 |
| 5 | **Shared state is locked; case state stays on one thread.** Locked: `run.log` appends (`Runner.log`, L1768-1780), `scorer_errors` (L3124), the pacer. The judge-gate memo, filled lazily today (`score_layers` L3317 → `unjudged_reason` L3684-3685), is filled before the pool starts. Not shared: `attempted` (L2879), `crashes` (L2890), `verdicts`, `routing_rows` (L3501), `refresh_derived`/`write_results` (L3910-3911), `abort_check`. Each repeat's scratch list is its own, merged by the case thread. | code review; §6.4 |
| 6 | **Turns stay serial within a conversation.** A worker owns one conversation repeat: `converse` (L2771) and `conversation_attempt` (L2659) run unchanged inside it, `turn_delay_s` included (L2685-2691). | one repeat per worker |
| 7 | **Cookie jars are per attempt**, a local of `conversation_attempt` (L2677); the static auth cookie is read-only. | unchanged code |
| 8 | **Each worker writes only its own scratch dir; the case thread does every run-tree write.** `tempfile.mkdtemp` (L2617, L2961) is thread-safe, but `atomic_write`'s temp name `{path}.tmp.{pid}` (L357) is the same in every thread, so two threads writing one path would collide. | `publish`/`write_repeats` stay on the case thread |
| 9 | **Resume is unchanged.** `verdict.json` is a case's last write (L2918), a directory without one is deleted and re-run (L3836-3846), adoption is in plan order (L3847-3858). | unchanged code |
| 10 | **The holdout ledger and seals are unaffected.** The ledger is appended once at pre-flight (L1829, L2134), before any worker; the runner writes no seal file. | pre-flight is serial |
| 11 | **Trace-id attribution is unaffected.** Response-sourced ids are minted per response (L2272-2275) and the spans built from it; trace-less runs read no trace; attribution was never by time (`read_trace_id`, L2259). | Phase 1 scope |
| 12 | **The dry run's `app_calls_max` stays an upper bound**: every planned call × `max_attempts` (L4540), and concurrency adds no call (§2.1 rule 4). `pace_floor_s` is unchanged. The dry run adds `max_concurrency`. | §6.5 |

## 4. Reversing D5

The code PR edits these; this PR does not.

**`docs/runner-contract.md` §2 (L227).** Old:
> **`adapter.invocation.max_concurrency > 1`** (decision **D5**).

New:
> **`adapter.invocation.max_concurrency`** not an integer in `[1, 8]`, or
> `> 1` without `rate_limit.requests`/`window_s`, or with
> `rate_limit.min_interval_s`, a queryable trace store, `mode: function` or
> `conversation.memory: user` (decision **D5**, revised 2026-11-21, §7).

**§7, first paragraph (L912-916).** Old:
> **Serial.** One case at a time, in the plan's case order, canaries first.
> `adapter.invocation.max_concurrency > 1` is **refused at plan validation
> with exit 2** (decision **D5**), not warned about and downgraded: a
> warning scrolls past and leaves the adapter field looking supported while
> every run is serial. Exit 2 makes the gap loud at the one moment someone
> can act on it.

New:
> **Cases are serial.** One case at a time, in the plan's case order,
> canaries first. **A case's k repeats run up to `max_concurrency` at once**
> (`docs/concurrency.md`), each on its own worker (a conversation's turns
> stay in order), gathered in repeat order. Every worker takes its calls
> from the one declared rate budget below; a 429 pauses all of them. Where
> concurrency is not supported, `max_concurrency > 1` is refused at plan
> validation with exit 2 (decision **D5**, §2's list), never downgraded: a
> warning scrolls past and leaves the field looking supported.

**D5 row (L1465).** New answer: "**Refuse, exit 2, where concurrency is not
supported** (§7); since 2026-11-21 a case's repeats run concurrently on
response-sourced and trace-less runs behind a declared budget (#30)."

Also: §7's budget paragraph gains "Under concurrency the budget counts calls
in flight, and a 429 or a `Retry-After` 503 pauses every worker"; §12 adds
`pause`; §10's manifest gains `invocation.max_concurrency`, written only when
`> 1`, like `rate_limit` (L2244-2245). Elsewhere:
`skills/discover/references/adapter-contract.md` L175-176,
`examples/quickstart/.evalup/adapter.yaml` L45, README L375 ("Runs are
serial."), and `score_cost.py` if open question 4 changes it.

## 5. No new scoring semantics

Concurrency must not change what any number means. The proof is a test: the
same plan at `w = 1` and `w = 3` against a deterministic fake app gives **the
same run tree after normalization**. Normalized:

- **Timing:** `request.json` `sent_at`; `latency_s` in `response.json`,
  `verdict.json` and `results.json`; manifest `started_at`; the ledger row's
  `date`; all of `run.log` (interleaved; nothing reads it, contract §12).
- **Per-run random values:** the rendered `<uuid>`; the minted trace id in
  `response.json`, `trace.json` and `trajectory.json` (L2275); scratch paths
  and minted trace ids anywhere in a `layer_error` row (its `argv` carries
  both, L3127, L3199).
- **Provenance:** the manifest's `invocation.max_concurrency` and
  `plan_sha256`, which covers it.

Everything else (every `verdict.json`, `repeats/`, `turns/`, the jsonl
files, `reliability.json`, the routing report, `results.json` `summary`) is
byte-identical. `SCORING_VERSION` does not move.

**Load can still move a value, never a meaning.** More calls at once can
push a call past `execution.timeout_s` (`infra_error`, `send_http` L1197),
concurrent scorers past `scorer_timeout_s` (`LAYER_ERROR`, L3091-3093), a
one-in-flight-per-user app into a 409/423 (§1.1), and every latency up. §7
checks each against a `w = 1` noise floor.

## 6. Test plan

The fake app is the suite's real HTTP server (`test_run_cases.py`), made
threaded and counting calls in flight.

1. **Byte-compatibility (§5):** single-turn and conversation plans, k=3,
   one answer per case (pass, fail, 4xx, 5xx, 429 across cases), at `w = 1`
   and `w = 3`: normalized trees equal. Repeated with a canary and a resume
   from a kill.
2. **Repeat order:** the fake holds the first arrival for a case so
   completion order reverses, and answers by arrival index (pass, fail,
   pass). Each `repeats/<n>/request.json`'s `<uuid>` matches its
   `response.json`, and `chosen` and `verdict.repeats` follow repeat order.
3. **It is concurrent:** k=3, `w = 3`: peak in-flight is 3 and the three
   send spans overlap; at `w = 2` the peak is 2. No wall-time assert.
4. **The budget holds across workers:** `RateBudget` on #79's fake clock,
   driven by a simulated scheduler of `w = 3` workers with random
   latencies, failures and 429s, `{requests: N, window_s}`: no window holds
   more than N calls at either end of a call's span, retries included.
5. **Shared 429 pause:** one worker gets `429 Retry-After: 2`; no call
   starts before its deadline, at most `w − 1` overlap it, and on the last
   attempt the pause is the header alone. `run.log` has `pause`.
6. **Interrupt:** a worker raising, and a `KeyboardInterrupt` in the case
   thread: no call starts after it, every worker is joined before the
   scratch dirs go, the case has no `verdict.json`, and `--resume` redoes it.
7. **Aborts and the dry run:** a failing canary at `w = 3` aborts, exit 4,
   with no non-canary call; an infra abort sends the calls a serial run
   sends; `app_calls_max` and `pace_floor_s` equal at `w = 1` and `w = 3`.
8. **Refusals (§1.1):** each row is exit 2 before any call; `w = 1` and an
   absent field behave as today. `test_max_concurrency_is_refused_not_downgraded`
   (`tests/test_run_cases.py` L374) becomes the no-budget case.
9. **Turn order:** at `w = 3`, every turn's `sent_at` follows the previous
   turn's response by at least `turn_delay_s`.
10. The three blessed checks, and `uv run --python 3.9`.

## 7. Live evidence before it ships

1. **Headroom:** a latency-bound app with a loose declared budget, k=3, the
   same plan at `w = 1` twice (the noise floor) and at `w = 3`. Shows: wall
   time near `1 / min(w, k)` of serial; per-case verdict changes, timeouts,
   scorer timeouts and `same_4xx`/`repeated_5xx` counts within the floor;
   zero 429s.
2. **Binding budget:** the real app rate-limited to 20 messages per 10
   minutes, `{requests: 20, window_s: 600}`, a 10-case plan at `w = 3`,
   started after a quiet window (the budget is per process, contract §7
   L931-933). Shows: zero 429s, wall time near `pace_floor_s` plus latency.
   No serial rerun.

## 8. Open questions for the owner

The code PR follows each recommendation unless the owner says otherwise.

1. **Repeats first, though k=1 gains nothing?** Recommended: yes (§1.3).
2. **One identity for concurrent repeats.** Recommended: accept it, and
   let discover record a one-in-flight-per-user app as `w = 1`; a per-repeat
   identity would need `identity_map` to grow, out of Phase 1.
3. **Ever allow `w > 1` with no budget?** Recommended: no; a loose budget
   costs one line and keeps the 429 path bounded.
4. **Latency diff across differing `max_concurrency`.** Recommended: warn
   in `score_cost.py`, not refuse; latency never gates.
5. **Where `w` comes from.** It is in the plan, so `plan_sha256` covers it
   and a `--resume` keeps it. Recommended: the adapter field only, no
   `--concurrency` flag.
6. **The cap.** Recommended: 8; Phase 1 never uses more than k.
7. **`min_interval_s` with `w > 1`.** Recommended: refuse (§1.1).
8. **Drop to one worker after a 429?** Recommended: no: under a declared
   budget a 429 means the budget is wrong, and the report should say so.
9. **#17 item 1.** Recommended: keep on hold; close as superseded by the
   store-traced phase (§1.2), not by Phase 1, which has no wait to save.

## 9. Not in Phase 1

N cases at once; store-traced apps and per-trace-id quiescence; function
mode; a budget shared across processes; adaptive worker counts.
