# Concurrent case execution — design

**Status:** spec only, written 2026-11-21 for issue #30. Nothing is built.
It follows the decision recorded on #30 that day: concurrency comes after
pacing (#76, PR #79), applies to **response-sourced or trace-less runs behind
a declared rate budget**, and reverses decision D5, so it is specified before
any code. #17 item 2 (PR #77) has landed. Two departures from #30: its body
said "serialize function mode", and this spec refuses it (§1.1); its decision
said concurrent repeats replace #17 item 1, and Phase 1 does not (Q9).

**What does not change.** No scorer, verdict rule, fold, denominator or
`SCORING_VERSION`. Every new pacer path (§2.1) runs only when `w > 1`; with
`max_concurrency` absent, `null` or 1 the runner takes today's code, so
nothing sent or written changes. Concurrency is transport, which the
2026-11-19 rule allows; §5 says how that is proven.

Line numbers are `evalup/scripts/run_cases.py` at `1e16839` unless a file
is named.

## 1. Scope

### 1.1 Phase 1 runs only where there is nothing to wait on

`adapter.invocation.max_concurrency: w` with `w > 1` is accepted only when
every condition below holds. Each failing one is **exit 2 at plan
validation**, in `validate_adapter_block` (L535), where D5 refuses today
(L545-555). Refused, never silently serialized: D5's reason still holds for
every configuration Phase 1 does not cover. `null` is absent, as today
(L549-551) and as for `rate_limit` (contract L936).

| Refused when | Why |
|---|---|
| no `rate_limit.requests` + `window_s` | #30's decision: concurrency only behind a declared budget. A loose budget is how an app with headroom says so |
| `rate_limit.min_interval_s` is set | the interval runs from the previous call's *finish* (`RateBudget`, L599-608), so no two calls may overlap |
| `traces.source` is anything but `response`, `view-only`, `none` or absent (L278, L280; absent is `not source`, L2062) | an allow-list: `otlp-file`'s quiescence wait watches the whole store (§1.2), and `jaeger`/`tempo`/`clickhouse` fail pre-flight anyway (L2055-2060). Keyed on the declared `source` alone; declare `view-only` to run a store trace-less |
| `invocation.mode: function` | an in-process callable of unknown thread safety (`send_function`, L1276), whose `timeout_s` is not enforced (L2235) |
| `invocation.conversation.memory: user` | memory outlives a session (`multi-turn.md` §6), so concurrent repeats would contaminate each other in an order no one controls |
| `w` is not an integer in `[1, 8]` | a bound against a typo. A small behaviour change: today a non-integer or 0 passes silently (L549-550) |

On `response` the trace id is minted per response (L2272-2275) and
`collect_trace` never waits (L2344-2345).

All repeats of a case are sent as one identity (`build_headers`, L2495). An
app that allows one request in flight per user may answer the second with
a 409 or 423, a non-429 4xx and so scored as the app's answer (L2517-2522).
Open question 2.

### 1.2 Store-traced apps are deferred

`wait_for_quiescence` (L2314-2335) polls the store file's size and mtime until
it is unchanged for `quiescence_ms`. With concurrent writers it never goes
quiet: every call waits the full `max_wait_s`, and late spans become
`infra_incomplete`. `collect_trace` also copies the **whole** store into
`trace.json` (L2356-2361), so a concurrent run's file would hold other
repeats' spans. A later phase needs:

- **Quiescence per trace id:** wait until this id's spans are present and
  the lines carrying it have not grown for `quiescence_ms`, capped by
  `max_wait_s`. A new completeness rule, so it needs evidence that it marks
  `infra_incomplete` no more often than the whole-store wait does serially.
- **`trace.json` holds only this id's lines**, which changes serial runs'
  artifact too, so it ships with the per-id wait.
- Then a case's k repeats are sent together and each repeat's trace is
  awaited concurrently, so the case pays one quiescence window, not k: what
  #17 item 1 asked for. #17 item 1 closes as superseded then.

### 1.3 The first step: a case's k repeats, not N cases

**Decision: Phase 1 runs a case's k repeats concurrently, `min(w, k)` at a
time. Cases stay serial.**

| | k repeats at once |
|---|---|
| Speedup ceiling | `min(w, k)`: 3× at regression/holdout's default k=3; **none at k=1** (smoke, targeted; `run-modes.md`) |
| Case-level writes, `verdicts`, `refresh_derived`, `results.json`, abort check | unchanged: `execute_case` (L2845) still returns one finished case to `execute` (L3905-3915) |
| Overspend after an abort | **none beyond serial**: serial already sends all k repeats before `abort_check` (L3917) |
| Canary barrier | automatic: canaries are cases and run first (`ordered_cases`, L2390-2399) |
| Verdict aggregation | `fold_repeats` (L1450) and `repeated_5xx`/`same_4xx` (L1479-1527) read the attempts as a set; `chosen` (L2898) needs repeat order, which a gather by index keeps |
| Conversations | k independent conversations, each serial within |

N cases at once would reach `w` at any k, but needs a plan-order commit
buffer, an explicit canary barrier, an order-dependent infra-abort point
and discarding completed cases past an abort; it waits for Phase 1's live
evidence (§7). Repeats are already independent by contract: "Each repeat
is a full independent invoke + score" (`runner-contract.md` §5.5), each with
a fresh `<uuid>` (L2489, L2671).

## 2. Composition with the rate budget (#76)

### 2.1 Rules (all only when `w > 1`)

1. **One pacer per run, shared by every worker.** `RateBudget` (L599) gains
   a lock; its docstring's "The runner is serial, so no lock" (L608) goes.
   §1.1 makes a budget mandatory, so the pacer always exists.
2. **A slot is reserved atomically, and `sent_at` follows it.** Today
   `sent_at` is stamped after a bare `self.pace()` (L2497-2502,
   L2703-2713), and the slot is taken later, in `send_one` → `paced` →
   `RateBudget.call` (L2557-2564, L628-637); another worker could take it in
   between, making `sent_at` early (contract §7: "stamped after the wait").
   So `acquire()` (check and take, one locked step) moves to those two
   sites, `sent_at` is stamped after it, and `send_one` uses the slot held;
   a retry, holding none, acquires inside `send_one`. The health check
   (L2008) runs before any worker and stays on `paced`. `release()`, in a
   `finally`, records the finish.
3. **In-flight calls count against the window.** A call may start at `t`
   only when (calls that finished after `t − window_s`) + (calls in flight)
   `< requests`. That keeps #79's tested property — no window holds more
   than `requests` calls at either end of a call's span.
4. **Retries and conversation restarts take slots the same way.**
   Concurrency adds no call: a repeat still makes at most `max_attempts`
   attempts (× turns for a conversation).
5. **A 429, or a 503 carrying `Retry-After`, pauses every worker.** In the
   same locked `release()` that records the finish, the pacer sets
   `paused_until = max(paused_until, finish + wait)` and grants no slot
   before it. `wait` is `retry_wait` (L1086-1104) over the attempt's
   `backoff_s` entry; on the last attempt (`backoff_s` has
   `max_attempts − 1` entries, L517) it is the header alone, still capped
   at 60 s (`RETRY_AFTER_CAP_S`, L199), else none. The worker's own retry
   waits the same deadline. A 429 without the header, other 5xx, timeouts
   and connection errors back off only their own worker (L2531, L2801).
6. **Bound per 429:** set inside the locked release, the pause stops every
   later grant; only the at most `w − 1` calls already in flight reach the
   window the app has closed. When it lifts, up to `w` calls restart
   together (open question 8).
7. **The pause never changes a verdict.** Exhausting `max_attempts` is
   `infra_error` as today; `infra_rate_abort` stays the backstop.
8. **Waits are logged after the fact.** A wait blocked on calls in flight
   has no length up front, so `pace`'s `wait_s` is the time `acquire()`
   blocked, and a new `pause` event records rule 5.
9. **`close()` stops everything that waits.** It notifies every waiter in
   `acquire()` (one can be parked up to `window_s`, at most a day,
   `RATE_LIMIT_MAX_S`), which raises `PacerClosed`. That is not a
   `TransportError`, so the handlers at L2512 and L2716 do not catch it and
   retry it as infra. The retry and turn-delay sleeps (L2531, L2801, L2691)
   wait on the pacer's close event instead of `time.sleep`. A join then
   takes at most one call's `timeout_s` plus the running scorer's
   `scorer_timeout_s`.

### 2.2 When the budget binds, concurrency buys nothing

Under rule 3 a slot is held from its call's start until its finish plus
`window_s`, so the budget allows about `requests / (window_s + L)` calls a
second, `L` the latency. With `c ≈ L + scoring` one repeat's cycle, serial
runs at `1 / c`, and the speedup is about
`max(1, min(min(w, k), c · requests / (window_s + L)))`: **1 whenever the
budget already binds the serial run**.

Live evidence: a real app rate-limited to 20 messages per 10 minutes.
Serial is budget-bound for any cycle under about 30 s. 100 cases × k=3 is
300 calls; `RateBudget.floor_s(300)` (L639-651) is `(299 // 20) × 600 =
8400 s` (140 min) at any `w`. Concurrency only compresses each window's
burst of 20, which the window waits out anyway. With `L ≈ c ≈ 60 s` it gains
about 1.8×.

It helps on **latency-bound runs whose declared budget has headroom**.
`run`'s estimate (`run/SKILL.md` L136-140) takes `c` from the latency
discover recorded in `findings.md`, and overstates the time at `w > 1`; it
gains the comparison of `pace_floor_s` (L4552-4568) with
`app_calls_planned × c / min(w, k)`.

## 3. Invariants

| # | Rule | Enforced by |
|---|---|---|
| 1 | **Artifact and verdict order is plan order.** Cases run in `ordered_cases` order (L2390); `verdicts` is appended by the case thread (L2923). Repeats are gathered by repeat `n`, never by completion, so `chosen` (L2898), `repeats/<n>/` and `verdict.repeats` are as serial. | gather by index; §6.2 |
| 2 | **Canaries are a serial barrier.** No non-canary repeat is sent before every canary case has a `verdict.json` and passed `abort_check` (L3917-3936). | cases stay serial |
| 3 | **No overspend after an abort beyond serial**, since the abort check follows all k repeats (L3905-3915). | §6.7 |
| 4 | **Every worker is joined before the case thread moves on or re-raises.** On a worker exception or Ctrl-C (the runner has no signal handler) the pacer closes (§2.1 rule 9), every future is joined, and only then does `execute_case`'s `finally` remove the scratch dirs (L2919-2921). The case has no `verdict.json`; `--resume` redoes it. | join in `execute_case`; §6.6 |
| 5 | **Shared state is locked; case state stays on one thread.** Locked: `run.log` appends (`Runner.log`, L1768-1780), `scorer_errors` (L3124), the pacer. A change: the judge-gate memo, filled lazily today (`score_layers` L3317 → `unjudged_reason` L3684-3685), is filled before the pool starts. Not shared: `attempted` (L2879), `crashes` (L2890), `verdicts`, `routing_rows` (L3501), `refresh_derived`/`write_results` (L3910-3911), `abort_check`. The case thread creates each repeat's scratch list before submitting it, so a worker that raises leaks no dir. | code review; §6.4, §6.6 |
| 6 | **Turns stay serial within a conversation.** A worker owns one conversation repeat: `converse` (L2771) and `conversation_attempt` (L2659) run inside it, `turn_delay_s` included (L2685-2691). | one repeat per worker |
| 7 | **Cookie jars are per attempt**, a local of `conversation_attempt` (L2677); the static auth cookie is read-only. | unchanged code |
| 8 | **Each worker writes only its own scratch dir; the case thread does every run-tree write.** `tempfile.mkdtemp` (L2617, L2961) is thread-safe, but `atomic_write`'s temp name `{path}.tmp.{pid}` (L357) is the same in every thread. | `publish`/`write_repeats` stay on the case thread |
| 9 | **Resume is unchanged.** `verdict.json` is a case's last write (L2918), a directory without one is deleted and re-run (L3836-3846), adoption is in plan order (L3847-3858). | unchanged code |
| 10 | **The holdout ledger and seals are unaffected.** The ledger is appended once at pre-flight (L1829, L2134), before any worker; the runner writes no seal file. | pre-flight is serial |
| 11 | **Trace-id attribution is unaffected.** Response-sourced ids are minted per response (L2272-2275); trace-less runs read no trace; attribution was never by time (`read_trace_id`, L2259). | Phase 1 scope |
| 12 | **The dry run's `app_calls_max` stays an upper bound**: every planned call × `max_attempts` (L4540), and concurrency adds no call. `pace_floor_s` and `pace_max_s` are unchanged; `wait_floor_s` (L4542) stays a serial sum, so it overstates at `w > 1`, and its note says so. The dry run adds `max_concurrency`. | §6.7 |

## 4. Reversing D5

The code PR edits these; this PR does not.

- **`docs/runner-contract.md`:** §2's refusal list (L227) becomes "`max_concurrency`
  not an integer in `[1, 8]`, or `> 1` without `rate_limit.requests`/`window_s`,
  with `min_interval_s`, a trace source outside the allow-list, `mode:
  function` or `memory: user` (D5, revised 2026-11-21)". §7's "Serial."
  paragraph (L912-916) becomes: cases are serial, a case's k repeats run up
  to `max_concurrency` at once, gathered in repeat order, all taking slots
  from the one declared budget, a 429 pausing all; unsupported
  configurations are still exit 2, never downgraded, for D5's reason. The
  D5 row (L1465) says the same. §7's budget paragraph adds the in-flight
  count and the shared pause; §12 adds `pause`; §10's manifest gains
  `invocation.max_concurrency`, only when `> 1`, like `rate_limit`
  (L2244-2245).
- **`skills/discover/references/adapter-contract.md` L175-176:** the comment
  is rewritten at about the same length and points at
  `runner-contract.md` §7, not at this doc. The file is in `FIRST_SESSION`
  (`tests/test_context_budget.py` L46-48), so its words count toward the
  25k budget, and a pointer here would need a conditional entry
  (CONTRIBUTING.md L148-154).
- **Who sets `w`:** the user. Discover keeps writing 1 and records a
  one-in-flight-per-user limit in `findings.md`; `skills/discover/SKILL.md`
  gains that one line beside its rate-limit line (L135).
- **`skills/run/SKILL.md` L136-140:** the estimate divides by `min(w, k)`
  (§2.2).
- `examples/quickstart/.evalup/adapter.yaml` L45, README L375 ("Runs are
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
concurrent scorers past `scorer_timeout_s` (`LAYER_ERROR`, L3093-3100), a
one-in-flight-per-user app into a 409/423 (§1.1), and every latency up. §7
checks each against a `w = 1` noise floor.

## 6. Test plan

The fake app is the suite's `ThreadingHTTPServer` (`tests/test_run_cases.py`
L117), plus an in-flight counter.

1. **Byte-compatibility (§5):** single-turn and conversation plans, k=3,
   one answer per case (pass, fail, 4xx, 5xx, 429 across cases), at `w = 1`
   and `w = 3`, each with a fresh state dir (`summary.holdout.looks_recorded`
   counts ledger lines, L3670, L2174-2175): normalized trees equal.
   Repeated with a canary and a resume from a kill.
2. **Repeat order:** the fake records a `<uuid>` → arrival-index map,
   answers by arrival (pass, fail, pass), and holds arrival 1 and arrival 2
   so they finish last. Expected per-repeat verdicts come from the map:
   each `repeats/<n>/request.json`'s `<uuid>` matches its `response.json`
   and its answer, and `chosen` and `verdict.repeats` follow repeat `n`.
3. **It is concurrent:** k=3, `w = 3`: peak in-flight is 3 and the three
   send spans overlap; at `w = 2` the peak is 2. No wall-time assert.
4. **The budget holds across workers:** `RateBudget` on #79's fake clock,
   driven by a simulated scheduler of `w = 3` workers with random
   latencies, failures and 429s; and a real-thread stress test, 8 threads on
   a real `{requests: 3, window_s: 0.3}`. Neither lets a window hold more
   than N calls at either end of a call's span, retries included.
5. **Shared pause:** `429 Retry-After: 2` and `503 Retry-After: 2` pause
   every worker to their deadline, with at most `w − 1` calls overlapping;
   a 503 or a 429 without the header backs off only its own worker
   (`backoff_s`); on the last attempt the pause is the header alone, capped
   at 60. `run.log` has `pause`.
6. **Interrupt:** a worker raising; a `KeyboardInterrupt` in the case
   thread; and one while a worker is parked in `acquire()` on a long window.
   Each: the parked worker gets `PacerClosed` at once, no call starts
   after, every worker is joined before the scratch dirs go, none leaks,
   the case has no `verdict.json`, and `--resume` redoes it.
7. **Aborts and the dry run:** a failing canary at `w = 3` aborts, exit 4,
   with no non-canary call; an infra abort sends the calls a serial run
   sends; `app_calls_max`, `pace_floor_s` and `pace_max_s` equal at `w = 1`
   and `w = 3`.
8. **Refusals (§1.1):** each row is exit 2 before any call; `w = 1`,
   `null` and an absent field behave as today, byte for byte, including a
   last-attempt `Retry-After`. `test_max_concurrency_is_refused_not_downgraded`
   (`tests/test_run_cases.py` L374) becomes the no-budget case.
9. **Turn order:** at `w = 3`, the gap from each reply to the next turn's
   arrival, measured on the app side per session `<uuid>` (as
   `tests/test_multi_turn.py` L1030-1045 does), is at least `turn_delay_s`.
10. The three blessed checks, and `uv run --python 3.9`.

## 7. Live evidence before it ships

1. **Headroom:** a latency-bound app with a loose declared budget (which app
   is open question 10), k=3, the same plan at `w = 1` twice (the noise
   floor) and at `w = 3`. Shows: wall time near `1 / min(w, k)` of serial;
   per-case verdict changes, timeouts, scorer timeouts and
   `same_4xx`/`repeated_5xx` counts within the floor; zero 429s.
2. **Binding budget:** the real app rate-limited to 20 messages per 10
   minutes, `{requests: 20, window_s: 600}`, a 10-case plan at `w = 3`,
   started after a quiet window (the budget is per process, contract
   L931-933). Shows: zero 429s, wall time near `pace_floor_s` plus latency.
   No serial rerun.

## 8. Open questions for the owner

The code PR follows each recommendation unless the owner says otherwise.

1. **Repeats first, though k=1 gains nothing?** Recommended: yes (§1.3).
2. **One identity for concurrent repeats.** Recommended: accept it; the
   user leaves `w = 1` for an app discover found one-in-flight-per-user. A
   per-repeat identity would need `identity_map` to grow, out of Phase 1.
3. **Ever allow `w > 1` with no budget?** Recommended: no; a loose budget
   costs one line and keeps the 429 path bounded.
4. **Latency diff across differing `max_concurrency`.** Recommended: warn
   in `score_cost.py`, not refuse; latency never gates.
5. **Where `w` comes from.** Recommended: the adapter field only, no
   `--concurrency` flag; `plan_sha256` covers it, so a `--resume` keeps it.
6. **The cap, and `min_interval_s`.** Recommended: 8, and refuse (§1.1).
7. **Drop to one worker after a 429?** Recommended: no: under a declared
   budget a 429 means the budget is wrong, and the report should say so.
8. **Stagger the restart after a pause?** Up to `w` calls go out together
   when it lifts. Recommended: no, while §7.2 shows zero 429s.
9. **#17 item 1.** Recommended: keep on hold; close as superseded by the
   store-traced phase (§1.2), not by Phase 1, which has no wait to save.
10. **Which app proves §7.1?** It needs real latency and budget headroom;
    the rate-limited one cannot show a speedup. Recommended: the owner
    names one before the code PR merges.

## 9. Not in Phase 1

N cases at once; store-traced apps and per-trace-id quiescence; function
mode; a budget shared across processes; adaptive worker counts.
