# Concurrent case execution — design

**Status:** spec only, written 2026-11-21 for issue #30. Nothing is built.
It follows the decision recorded on #30 that day: concurrency comes after
pacing (#76, PR #79), applies to **response-sourced or trace-less runs behind
a declared rate budget**, and reverses decision D5, so it is specified before
any code. #17 item 2 (PR #77) has landed; #17 item 1 stays on hold (§1.2, §8 question 8).

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
| `traces.source` is a queryable store (`QUERYABLE_TRACE_SOURCES`, L275: `otlp-file`) | the quiescence wait watches the whole store (§1.2) |
| `invocation.mode: function` | the entrypoint is an in-process callable of unknown thread safety (`send_function`, L1276); `execution.timeout_s` is not even enforced there (manifest `timeout_enforced`, L2235) |
| `invocation.conversation.memory: user` | memory outlives a session (`multi-turn.md` §6), so concurrent repeats would contaminate each other in an order no one controls |
| `invocation.rate_limit.min_interval_s` is set | the interval runs from the previous call's *finish* (`RateBudget`, L599-608), so no two calls may overlap: `w > 1` could only serialize |
| `w` is not an integer in `[1, 8]` | a bound against a typo; 8 is open question 3 |

Eligible: `traces.source: response` (`RESPONSE_TRACE_SOURCE`, L278; the trace
id is minted per response, L2272-2275, and `collect_trace` never waits,
L2344-2345), and every trace-less run (`view-only`, `none`, absent; L280, L2062-2066).

### 1.2 Store-traced apps are deferred

`wait_for_quiescence` (L2314-2335) polls the store file's size and mtime until
it is unchanged for `quiescence_ms`. With concurrent writers it never goes
quiet: every call waits the full `max_wait_s`, and spans that land after it
are lost to `infra_incomplete`. `collect_trace` then copies the **whole**
store into `trace.json` (L2356-2361), so a concurrent run's `trace.json` would
also hold other repeats' spans (`normalize_trace.py` filters by id, but the
artifact would differ from a serial run's).

What a later phase needs, specified there, not here:

- **Quiescence per trace id:** wait until *this* id's spans are present and
  the set of lines carrying this id has not grown for `quiescence_ms`,
  capped by `max_wait_s`. That is a new completeness rule, so it needs its
  own evidence that it marks `infra_incomplete` no more often than the
  whole-store wait on a serial run.
- **`trace.json` holds only this id's lines**, which changes the artifact for
  serial runs too, so it ships with the per-id wait, not before.
- Then a case's k repeats can be sent together and waited on once each,
  which is what #17 item 1 asked for; #17 item 1 closes as superseded then.

### 1.3 The first step: a case's k repeats, not N cases

**Decision: Phase 1 runs a case's k repeats concurrently, `min(w, k)` at a
time. Cases stay serial.**

| | k repeats at once (Phase 1) | N cases at once (later) |
|---|---|---|
| Speedup ceiling | `min(w, k)`: 3× at regression/holdout's default k=3; **none at k=1** (smoke, targeted; `run-modes.md`) | `w`, at any k |
| Case-level writes, `verdicts`, `refresh_derived`, `results.json`, abort check | unchanged: `execute_case` (L2845) still returns one finished case to `execute` (L3905-3915) | need a plan-order commit buffer |
| Overspend after an abort | **none beyond serial**: serial already sends all k repeats before `abort_check` (L3917) runs | up to `(w−1)` cases in flight (§3 rule 3) |
| Canary barrier | automatic: canaries are cases and run first (`ordered_cases`, L2390-2399) | must be explicit |
| Verdict aggregation | `fold_repeats` (L1450) and `repeated_5xx`/`same_4xx` (L1479-1528) read the attempts as a set; `chosen` (L2898) needs the list in repeat order, which a gather by index keeps | the same, plus an order-dependent infra-abort point |
| Conversations | k independent conversations, each serial within | the same |
| Resume unit | the case, unchanged (§3 rule 9) | the case, but completed cases past an abort must be discarded |
| #17 item 1 | nothing to replace yet: Phase 1 has no quiescence wait. The same design on store-traced runs replaces it (§1.2) | does not replace it |

Repeats are already independent by contract: "Each repeat is a full
independent invoke + score" (`runner-contract.md` §5.5), each with a fresh
`<uuid>` (L2489, L2671). N cases at once is the next step only after Phase 1's
live evidence (§7) shows the shared pacer holding.

## 2. Composition with the rate budget (#76)

### 2.1 Rules

1. **One pacer per run, shared by every worker.** `RateBudget` (L599) gains
   a lock; its docstring's "The runner is serial, so no lock" (L608) goes.
   There is never a per-worker budget.
2. **A slot is reserved atomically.** Today `Runner.pace` (L1750-1758) waits,
   then `RateBudget.call` (L628-637) re-checks and records the finish. Under
   workers the check and the take must be one locked step, `acquire()`,
   returning when a slot is free; `sent_at` is stamped after it (L2497-2502,
   L2703-2713, as now); the finish is recorded on `release()`.
3. **In-flight calls count against the window.** A call may start at `t`
   only when (calls that finished after `t − window_s`) + (calls in flight)
   `< requests`. That keeps #79's tested property — no window holds more than
   `requests` calls at either end of a call's span — with overlapping calls.
4. **Retries and conversation restarts go through the same pacer**, as
   today (`send_one`, L2557-2564). Concurrency adds no call: a repeat still
   makes at most `max_attempts` attempts (× turns for a conversation).
5. **A 429, or a 503 carrying `Retry-After`, pauses every worker.** The
   worker that got it sets the pacer's `paused_until` to
   `max(paused_until, finish + retry_wait(...))` (`retry_wait`, L1086-1104,
   capped at `RETRY_AFTER_CAP_S` = 60, L199); no slot is granted before it.
   The worker's own retry waits the same deadline, so its wait is unchanged.
   Other 5xx, timeouts and connection errors back off only their own worker,
   as today (L2531, L2801): they say nothing about a shared limiter.
6. **Bound per 429:** calls already in flight cannot be recalled, so at most
   `w − 1` further calls reach a window the app has just closed.
7. **The pause never changes a verdict.** Exhausting `max_attempts` is
   `infra_error` exactly as today, and `infra_rate_abort` stays the backstop.
8. **`pace` events are unchanged**; a new `pause` event (`run.log`,
   contract §12) records rule 5's waits.

### 2.2 When the budget binds, concurrency buys nothing

Throughput is `min(w / c, requests / window_s)` calls per second, where `c`
is one repeat's cycle (latency plus scoring) and no budget means
`requests / window_s = ∞`. Serial is `w = 1`. The speedup is
`min(w, c · requests / window_s)`, and **1 whenever `c · requests /
window_s ≤ 1`**: the budget already binds the serial run.

Live evidence: a real app rate-limited to 20 messages per 10 minutes, with
`Retry-After` capped at 60 s. Its budget allows one call per 30 s on
average, so any app answering in under 30 s is budget-bound serially. 100
cases × k=3 is 300 calls; `RateBudget.floor_s(300)` (L639-652) is
`(299 // 20) × 600 = 8400 s` (140 min) at any `w`. Concurrency only compresses
each window's burst of 20, which the window waits out anyway: the whole run
saves at most the last burst. Without the declared budget, `w` workers would
fill the window `w` times faster, and the 60 s cap cannot outlast a 600 s
window, so the extra calls become `infra_error` (#76's problem, multiplied).

It helps on **latency-bound runs with budget headroom**: no `rate_limit`, or
one with `c · requests / window_s > 1`. The README's example (L375: 100 ×
3 at 8 s a call, about 40 min) at `w = 3` with no budget is about a third of
that, the app and its provider permitting. An app that takes 60 s a call
under the 20/10-min budget gains `min(3, 2) = 2×`.

`run`'s time estimate states which regime a plan is in from `pace_floor_s`
(dry run, L4553-4568) against `app_calls_planned × c / min(w, k)`.

## 3. Invariants

Each is a rule; the right column says how it holds.

| # | Rule | Enforced by |
|---|---|---|
| 1 | **Artifact and verdict order is plan order.** Cases run in `ordered_cases` order (L2390) and `verdicts` is appended by the case thread (L2923). Repeats are gathered into a list indexed by repeat `n`, never by completion, so `chosen` (L2898), `repeats/<n>/` and `verdict.repeats` are as serial. | gather by index; test §6.1 |
| 2 | **Canaries are a serial barrier.** No non-canary repeat is sent before every canary case has a `verdict.json` and passed `abort_check` (L3917-3936). | cases stay serial in Phase 1; a later phase must keep it explicitly |
| 3 | **Bounded overspend after an abort.** Phase 1: zero calls beyond what serial spends, since the abort check already follows all k repeats (L3905-3915). A later N-case phase: on an abort the pacer grants no new slot; at most `w − 1` cases already in flight finish, each at most `k × max_attempts × turns` calls, and their directories are deleted so the tree is serial's. | pacer `close()`; test §6.6 |
| 4 | **Shared state is locked; case state stays on one thread.** Locked: `run.log` appends (`Runner.log`, L1768-1780), `scorer_errors` (L3124), the pacer (§2.1). The judge-gate memo (L1707-1710) is filled before the pool starts. Not shared, so not locked: `attempted` (L2879), `crashes` (L2890), `verdicts`, `routing_rows` (L3501), `refresh_derived`/`write_results` (L3908-3909), `abort_check`. Each repeat's scratch list is its own and merged by the case thread. | code review; test §6.4 |
| 5 | **Refused under `memory: user`, in function mode, with a store, with `min_interval_s`.** | §1.1, exit 2 |
| 6 | **Turns stay serial within a conversation.** A worker owns one conversation repeat end to end: `converse` (L2771) and `conversation_attempt` (L2659) run unchanged inside it, `turn_delay_s` included (L2685-2691). | one repeat per worker |
| 7 | **Cookie jars are per attempt.** The jar is a local of `conversation_attempt` (L2677), never shared; the static auth cookie is read-only. | unchanged code |
| 8 | **Scratch dirs are unique.** `tempfile.mkdtemp` (L2617, L2961) is safe across threads; cleanup stays in `execute_case`'s `finally` (L2919-2921). | unchanged code |
| 9 | **Resume is unchanged.** The unit is still the case: `verdict.json` is its last write (L2918), a directory without one is deleted and re-run (L3836-3846), and adoption is in plan order (L3847-3858). A kill mid-case loses its in-flight repeats, as serial loses its earlier ones. | unchanged code |
| 10 | **The holdout ledger and seals are unaffected.** The ledger is appended once at pre-flight (L1829, `append_holdout_ledger` L2134), before any worker; the runner writes no seal file. | pre-flight is serial |
| 11 | **Trace-id attribution is unaffected.** Response-sourced ids are minted per response (L2272-2275) and the spans are built from that response (`trace_from_response`); trace-less runs read no trace. Attribution was never by time window (`read_trace_id`, L2259). | Phase 1 scope |
| 12 | **The dry run's `app_calls_max` stays an upper bound.** It is every planned call × `max_attempts` (L4540); concurrency adds no call (§2.1 rule 4), and a later phase's post-abort calls are calls of planned cases. `pace_floor_s` is unchanged (§2.2). The dry run adds `max_concurrency` and the effective `min(w, k)`. | test §6.5 |

## 4. Reversing D5 in `runner-contract.md`

Not edited in this PR; the code PR makes these changes, plus the matching
lines in `adapter-contract.md` (L175-176) and the README (L375).

**§2, plan validation (L227).** Old:
> **`adapter.invocation.max_concurrency > 1`** (decision **D5**).

New:
> **`adapter.invocation.max_concurrency`** not an integer in `[1, 8]`, or
> `> 1` with a queryable trace store, `mode: function`,
> `conversation.memory: user` or `rate_limit.min_interval_s` (decision
> **D5**, revised 2026-11-21, §7).

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
> (`docs/concurrency.md`), each a full repeat on its own worker (a
> conversation's turns stay in order), gathered in repeat order. Every
> worker takes its calls from the one rate budget below; a 429 pauses all
> of them. Where concurrency is not supported (a queryable trace store,
> function mode, `memory: user`, `min_interval_s`) `max_concurrency > 1`
> is refused at plan validation with exit 2 (decision **D5**), never
> downgraded: a warning scrolls past and leaves the field looking
> supported.

**Decisions table, D5 row (L1465).** Old answer: "**Refuse, exit 2** (§7). A
warning scrolls past …". New answer: "**Refuse, exit 2, where concurrency is
not supported** (§7); since 2026-11-21 a case's repeats run concurrently on
response-sourced and trace-less runs (#30). The refusal's reason is
unchanged."

§7's rate-budget paragraph gains one sentence: "Under concurrency the budget
counts calls in flight, and a 429 or a `Retry-After` 503 pauses every
worker." §12 adds the `pause` event. §10's manifest gains
`invocation.max_concurrency`, written only when `> 1`, like `rate_limit`
(L2244-2245).

## 5. No new scoring semantics

Concurrency must not change what any number means. The proof is a test, not
an argument: the same plan, run at `w = 1` and at `w = 3` against a
deterministic fake app, gives **the same run tree after normalization**.

Normalized (differ between two serial runs too, or are timing):

- **Timing:** `request.json` `sent_at`; `response.json` and `verdict.json`
  `latency_s`; `results.json` `cases[].latency_s`; manifest `started_at`;
  the ledger row's `date`; all of `run.log` (interleaved, `ts`, `wait_s`;
  nothing reads it, contract §12).
- **Per-run random values:** the rendered `<uuid>` in `request.json`'s body;
  the minted trace id in `response.json`, `trace.json` and `trajectory.json`
  (L2275); scratch paths in a scorer error's `argv` (L3127).
- **Provenance:** the manifest's `invocation.max_concurrency`.

Everything else — every `verdict.json`, `repeats/`, `turns/`,
`verdicts.jsonl`, `verdicts_for_stats.jsonl`, `repeats.jsonl`,
`reliability.json`, the routing report, `results.json` `summary` — is
byte-identical. `SCORING_VERSION` does not move.

**Latency is the one number whose value concurrency can move.** Its meaning
does not change (whole-request wall time), but an app serving three calls at
once may answer each more slowly, so a `score_cost.py` latency diff between a
serial baseline and a concurrent run compares two loads. The manifest records
`max_concurrency`; whether the latency diff warns or refuses across differing
values is open question 4. Cost never gates (CONTRIBUTING, decision 4).

## 6. Test plan

The fake app is the suite's real HTTP server (`test_run_cases.py`), made
threaded, deterministic per request body, and counting calls in flight.

1. **Byte-compatibility (§5):** single-turn and conversation plans, k=3,
   mixed pass/fail/4xx/5xx/429 answers, run at `w = 1` and `w = 3`; the
   normalized trees are equal. Repeated with a canary and with a resume
   from a kill.
2. **It is concurrent:** with a 0.5 s latency, k=3, `w = 3`, peak in-flight
   is 3 and a case takes under 2 × latency; with `w = 2`, peak is 2.
3. **The budget holds across workers:** on #79's fake clock with random
   latencies, failures and 429s, `w = 3`, `{requests: N, window_s}`: no
   window holds more than N calls at either end of a call's span, retries
   included.
4. **Shared 429 pause:** one worker gets `429 Retry-After: 2`; no worker's
   next call starts before its deadline, and at most `w − 1` calls overlap
   it. `run.log` has the `pause` event.
5. **Dry run:** `app_calls_max` and `pace_floor_s` are equal at `w = 1` and
   `w = 3`; the report carries `max_concurrency`.
6. **Aborts:** a failing canary at `w = 3` aborts with exit 4 and no
   non-canary call; an infra abort sends exactly the calls a serial run
   sends.
7. **Refusals (§1.1):** each row is exit 2 with its reason, before any call;
   `w = 1` and an absent field behave as today. The existing
   `test_max_concurrency_is_refused_not_downgraded`
   (`tests/test_run_cases.py` L374) becomes the store-traced case.
8. **Turn order:** in a conversation at `w = 3`, every turn's `sent_at`
   follows the previous turn's response by at least `turn_delay_s`.
9. The three blessed checks, and `uv run --python 3.9` (thread APIs on the
   floor).

## 7. Live evidence before it ships

1. **Headroom:** a latency-bound app with no budget or a loose one, k=3,
   one plan at `w = 1` then `w = 3`. Shows: wall time ratio near
   `min(w, k)`; per-case verdicts equal or, where not, explained by the
   app's own nondeterminism (a second `w = 1` run is the noise floor);
   zero 429s.
2. **Binding budget:** the real app rate-limited to 20 messages per 10
   minutes, its declared `{requests: 20, window_s: 600}`, a small plan at
   `w = 3`. Shows: zero 429s, and wall time within one window of the serial
   run. This costs the app's quota, so it is open question 5.

## 8. Open questions for the owner

Each has a recommendation; the code PR follows it unless the owner says
otherwise.

1. **Repeats first (§1.3) when it is worthless at k=1?** Smoke and targeted
   gain nothing. **Recommended: yes**: it carries no abort, order or resume
   risk, and N-case concurrency is designed on its evidence.
2. **Where `w` comes from.** `adapter.invocation.max_concurrency` is in the
   plan, so it is in `plan_sha256` and a `--resume` cannot change it.
   **Recommended: the adapter field only** (what the app tolerates is the
   app's fact), and accept that a resume keeps its `w`. A `--concurrency`
   flag would be a second source.
3. **The cap.** **Recommended: 8**; Phase 1 never uses more than k.
4. **Latency diff across differing `max_concurrency`** (§5).
   **Recommended: warn, not refuse**: latency never gates.
5. **Run §7.2 on the rate-limited app?** It proves safety, not speed.
   **Recommended: yes, a 10-case plan** (30 calls, two windows).
6. **`min_interval_s` with `w > 1`: refuse, or allow and serialize?**
   **Recommended: refuse** (§1.1), D5's own reasoning.
7. **Adaptive backoff.** After a 429 under a declared budget, drop to one
   worker for the rest of the run? **Recommended: no** in Phase 1: a 429
   under a correct budget means the budget is wrong, and the report should
   say so rather than the runner hiding it.
8. **#17 item 1.** **Recommended: keep on hold, close as superseded by the
   store-traced phase (§1.2)**, not by Phase 1, which has no wait to save.

## 9. Not in Phase 1

N cases at once; store-traced apps and per-trace-id quiescence (§1.2);
function-mode concurrency; a budget shared across processes (the #76 budget
is per process, contract §7); adaptive worker counts.
