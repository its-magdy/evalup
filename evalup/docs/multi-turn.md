# Multi-turn conversations — design

**Status:** design, not built. Written 2026-10-06, revised the same day after
an independent review whose code claims were re-checked line by line. The
runner is feature-frozen (CLAUDE.md); the owner lifted the freeze for this one
feature on 2026-10-06. Nothing here changes what an existing number means: a
single-turn case scores exactly as it does today, and no scorer is rewritten.

Today a case with more than one user turn is `skipped` (adapter-contract hard
rule 3, `run_cases.skip_reason`). This note is the shape of the conversation
driver that replaces that skip, the decisions that keep its numbers honest,
and what Phase 1 leaves out.

## 0. A prerequisite bugfix — ships first, on its own

`case-format.md` says prior assistant/system messages are "fine as fixed
context" in a single-user-turn case. They are not sent: `case_text()`
(`run_cases.py:1178`) sends the last user message and drops everything else,
and the case scores as if the context had reached the app. That is the
truncated-conversation verdict hard rule 3 exists to refuse, one door over.
`validate_cases.count_turns` counts only user turns, so nothing warns today.

Fix: a case whose `input.messages` holds anything besides exactly one user
message is `skipped` with that reason, and `validate_cases.py` reports it at
authoring time. This is a bugfix and is legal under the freeze; it does not
wait for the rest of this note.

**It changes existing suites' numbers:** a case carrying a system or assistant
message scores today and is `skipped` after the fix, so `n` drops. The
CHANGELOG entry says so, and the quickstart example must stay at 0 errors and
0 warnings.

## 1. What a multi-turn case is

A **live scripted conversation.** The case author writes only the user's
turns, in order. The runner sends them one at a time to the real app, inside
one conversation, and the app's real replies are the history. Nobody writes
the assistant's lines. This is the only model that tests the app's *own*
memory, which is the bug class this feature exists to catch.

```yaml
id: c-followup-01
persona: team_manager                  # one identity for the whole conversation
input:
  turns:
    - user: "Show me the licences for the Cairo team"
      expect: { route: licences }      # optional checkpoint on this turn
    - user: "only the expired ones"
every_turn:                            # optional: copied into each turn's expect
  tools: { forbidden: [update_licence] }
expect:                                # the FINAL turn's checks, as today
  route: licences
  answer: { not_contains: ["which team"] }
```

- `input.turns` and `input.messages` are mutually exclusive.
  `validate_cases.py` errors on both, and on a `turns` list of length 1
  (write a single-turn case).
- **Each turn is an object with a `user` key, never a bare string.** Later
  phases add keys beside it (`assistant:` for seeded history, `simulate:` for
  a simulated user, §9); a bare string would need a migration.
- **The top-level `expect` is the final turn's claim.** Every existing
  authoring rule keeps working unchanged: gradedness (`no_graded_layer`),
  metamorphic parents, the reserved-field WARNs. A case whose only graded
  check sits on a checkpoint is a `no_graded_layer` ERROR, and the message
  says "put a graded check on the final turn".
- An earlier turn's `expect` is a **checkpoint**, using the same keys and the
  same scorers. A turn without one still has the implicit HTTP check every
  case has today (`score_http`, `run_cases.py:2237`): a 2xx passes, a 3xx or a
  4xx other than 429 fails, and a 5xx is `infra_error`.
- **`every_turn`** copies its keys into every turn's expect, the final one
  included. Without it, `expect.tools.forbidden` checks only the turn it sits
  on, and "never call `update_licence` during the conversation" silently
  means "not on the last turn". It is a copy made by the runner, not a new
  scorer.
- **A `clarify_ok` checkpoint cannot branch.** If the app *may* ask a
  clarifying question, the scripted next turn is either its answer or a
  non-sequitur. `generate` writes `clarify_ok` checkpoints only where the
  clarification is deterministic, and its SKILL says so.
- **A multi-turn canary is a validator ERROR.** Conversations are flaky by
  construction (§4) and a failing canary aborts the run (exit 4).

## 2. How the app keeps the conversation

The adapter declares it, in a new block. It is named `conversation`, not
`session`, on purpose: an earlier `adapters/dotnet.md` shipped a `session`
block, and hard rule 3's history is that declaring one must never be enough
to turn on a driver. An adapter without `invocation.conversation` keeps
today's behaviour: multi-turn cases are `skipped`.

```yaml
invocation:
  conversation:
    style: client-id | server-id | cookie
    session_from:                         # server-id only, exactly one of:
      body: "conversationId"              #   a dotted path in turn 1's body
      # header: "X-Conversation-Id"       #   or a response header
    turn_delay_s: 0                       # optional pause between turns (§5)
    memory: session | user                # what discover found (§6)
```

| Style | The app | The runner |
|---|---|---|
| `client-id` | takes a session id the client invents (RefApp's `sessionId`) | renders `<uuid>` once per conversation attempt and reuses it on every turn; today it is already once per repeat (`invoke_once`, `run_cases.py:1749`) |
| `server-id` | mints the id and returns it in turn 1's reply, in the body or a header | reads `session_from` off turn 1's response and renders it into a new `<session>` placeholder on turns 2+ (see below for turn 1). Turn 1 without the field ⇒ the case is `unscored` with that reason, never a turn 2 sent without a session |
| `cookie` | keeps state in a session cookie | keeps one cookie jar per conversation attempt and composes the `Cookie` header itself (see below) |

**`<session>` on turn 1 is omitted, not `null`.** `render_template` turns a
string that *is* a placeholder into a typed value, so an unset `<session>`
would go out as JSON `null`, and an ASP.NET Core app binding a non-nullable
`Guid` answers that with a 400. An app that accepts `null` also accepts a
missing key; the reverse is false. The rule: a dict entry whose value is
exactly `<session>` is dropped while it is unset; a string that merely
contains it gets `""`. There is no second, first-turn template.

**Cookies are merged by hand, for three reasons found in the code:**

1. `build_headers` sets `headers["Cookie"]` for `auth: {type: cookie}`
   (`run_cases.py:783`), and the stdlib's `CookieJar.add_cookie_header` adds
   nothing when a `Cookie` header is already present. Used as-is, the app's
   session cookie would never reach turn 2, and every cookie-style app with
   cookie auth would score "forgot the context". So the runner builds one
   `Cookie` header from the static auth cookie plus the jar's cookies.
2. `send_http` keeps response headers as `dict(response.headers.items())`
   (`run_cases.py:860`), which keeps only the last of several `Set-Cookie`
   headers. The driver reads the full list (`response.headers.get_all`).
3. `send_http` calls `urlopen` directly; the jar needs its own handling per
   conversation attempt. That is new plumbing, counted in §11.

Function mode: the callable is called once per turn with the rendered body;
`<uuid>`/`<session>` work the same way (`session_from.body` reads the
returned object).

**Client-sent history (OpenAI-style, the whole history in each request) is
not Phase 1.** Every app wants a different shape (`role/content`, `parts`,
`text`, a nested `history` field), so it needs a declared mapping, and
inventing that mapping well deserves its own pass.

`discover` reads the conversation handling it already looks for
(`discover/SKILL.md` §3, "conversation/session handling") and writes the
block; `profile.yaml`'s `capability_matrix.multi_turn` becomes `enabled` when
it is declared, otherwise its `blocked_by` names the missing block, a reason
the user can now act on.

## 3. Running a conversation

One conversation attempt:

1. Fresh `<uuid>`, empty cookie jar, no `<session>`.
2. For each turn: render, send, read the answer and trace id with the
   adapter's existing paths, score the turn with the existing layers, and
   `roll_up()` the turn (unchanged, `run_cases.py:1064`).
3. **Stop at the first turn that rolls up `fail` or `infra_*`. Continue past
   `unscored`.** A `fail` means the app went off script, and anything sent
   after it is a turn built on a derailed conversation. `unscored` means the
   harness could not look (no trace, no `route_from_response`), not that the
   app failed; stopping there would never send a final turn that *can* be
   scored, and would punish the author for adding a checkpoint. That is the
   first-time-user path (a trace-less adapter), so it is not a corner case.
   `verdict.json` records `failed_turn` and that turn's reason ("expected
   route licences, got a clarification"). A per-turn "continue even if this
   fails" opt-in is left for later (§9).

**The case verdict is `roll_up()` over the union of every sent turn's
layers**, keyed `t<n>.<layer>` (`t1.routing`, `t2.answer`, …). The rollup
strips the prefix before checking `RUN_TRIGGERED`, so `http` and `loops`
still cannot carry a case to `pass` on their own. This is a strict
generalisation: on fail, infra or all-pass it gives the stop rule's answer,
and on `unscored` it gives today's single-turn semantics.

**Retries restart the whole conversation.** Today a timeout, 429 or 5xx is
retried by re-sending the same request (`invoke_once`, `run_cases.py:1764`).
Inside a server-side conversation that is unsafe: the app may already have
stored the turn it timed out on, and a re-send makes it see the turn twice,
so everything after it is scored on a corrupted history. So any retryable
outcome on any turn abandons the attempt and starts a new one from turn 1
with a fresh `<uuid>` and cookie jar, after `backoff_s`. `max_attempts`
counts conversation attempts; on exhaustion the case is `infra_error`, as
today. promptfoo reaches the same rule for stateful targets (backtracking is
disabled when `stateful: true`), and Inspect splits request retries from
whole-sample re-runs the same way (§12).

`repeated_5xx` (F-165) applies when every attempt ended on the same 5xx and
body **at the same turn**, and records that turn. The cost is stated: an app
that 5xxs deterministically at turn N has turns 1..N-1 replayed on every
attempt.

**A hard cap, in the plan:** `execution.max_turns` (default 12, set by
`make_plan.py`). A case with more turns is `skipped` with that reason. The
runner needs a bound for cost and loop safety whatever the author wrote; the
validator's WARN above 8 turns is authoring guidance and may move.

**Repeats** (`k > 1`) re-run the whole conversation; `fold_repeats()` and
pass^k are unchanged (contract §5.5).

**Resume** (§8): a conversation is one unit. A case directory without
`verdict.json` is redone from turn 1, as any unfinished case is today.

## 4. The unit of measurement is the conversation

**One conversation is one case, never N.** Turns of one conversation are not
independent samples: a wrong turn 1 drags turns 2–5 with it. Counting them as
separate trials shrinks every interval and lets the keep/revert gates call
noise a win (a 2026 study found 42% of turn-pooled significant results vanish
under a cluster-robust correction, §12). So:

- `verdicts.jsonl`, `verdicts_for_stats.jsonl`, `stats.py`, `gate.py` and
  `run_history.py` see one row per conversation, exactly as for a single-turn
  case. The row gains `multi_turn`, `turns_sent` and `failed_turn`; nothing
  downstream of the case verdict changes meaning.
- **Routing's run-level report** (contract §5.2) keeps meaning single-turn
  routing. Two changes, made together, or every run with a multi-turn case
  ends `incomplete`:
  - `record_routing_row` (`run_cases.py:2417`) appends no row for a
    multi-turn case. Today it reads the row off the case verdict, so the
    deciding turn's row would land in the confusion matrix.
  - `REQUIRED_IF["routing_results.jsonl"]` and `verify_run_dir`'s condition
    (`run_cases.py:3070`) become "any non-canary **single-turn** case is
    routing-scorable". Otherwise a suite of route-less single-turn cases plus
    multi-turn cases with a final `expect.route` demands a file nobody wrote,
    exits 6, and closes the gate.

  Per-turn routing rows stay on disk in `turns/<t>/verdict.json`, so a later
  per-turn routing report can be built from a historical run without
  re-running it.
- **pass^k makes conversations look worse, and that is decided, not fixed.**
  Four turns at 0.9 each pass about 0.66 of the time; at the regression
  mode's `k=3` with a hard gate (`make_plan.py:20`) that is about 0.28. Ten
  healthy multi-turn cases would close the gate on the first regression run.
  The fold (D7) stays. Instead:
  - `generate` writes multi-turn cases `gating: false` (an existing field,
    "tracked-not-gating") until a baseline exists; the user promotes them.
  - `summary` and the report carry the multi-turn pass rate beside the
    single-turn one, never only a blend; `gate.py` prints the split.
- **Paired comparisons refuse pairs that reached different turns.** A
  case's `latency_s` and token counts are the sums over its sent turns. If
  the baseline fails at turn 1 (1.2 s) and the candidate passes through
  turn 4 (4.8 s), a paired sign test calls the candidate 3.6 s slower when it
  merely got further. So `results.json` rows carry `turns_sent`, and
  `score_cost.py`'s paired tests exclude, and name, pairs whose `turns_sent`
  differ.

## 5. Traces, timing and cost

- **Traces per turn.** Each turn's trace id comes from that turn's response;
  the existing quiescence wait (3 s quiet, up to 30 s, `run_cases.py:1645`)
  runs per turn, and each turn's tool and trajectory checks read only that
  turn's trace. `collect_trace` copies the whole span store per turn, so each
  `turns/<t>/trace.json` holds the full store; acceptable, and stated.
- **One trace for the whole conversation.** Some instrumentations reuse one
  trace id across turns. When two turns of one attempt return the same trace
  id, per-turn attribution is impossible, so every trace-dependent layer of
  that case (`loops` included, being in `TRAJECTORY_LAYERS`) is `unscorable`
  with that reason: honest degradation, the rule the authz scorer follows.
  Slicing spans by turn is not attempted.
- **Background writes.** Some apps store history or summarise memory after
  replying, so a turn sent too soon reads as "forgot the context". The
  optional `turn_delay_s` waits between turns; `discover` sets it when it
  sees async memory writes.
- **Session expiry.** The per-turn quiescence wait plus `turn_delay_s` can
  outlive a server-side session TTL, and the resulting "new conversation"
  reads as "forgot the context". Each turn's `request.json` keeps `sent_at`;
  `analyze` reads the gaps before calling a failure a memory bug.
- **Cost grows faster than turns:** each turn re-sends a longer context.
  `score_cost.py`'s `case_usage` reads only `cases/<id>/trajectory.json`
  today (`score_cost.py:351`); it learns to sum `turns/<t>/trajectory.json`,
  so a conversation is priced as one case. The per-turn token growth curve is
  later (§9).
- **Run time.** 20 conversations × 5 turns × (model latency + at least 3 s
  quiescence) at `k=3` is about an hour. The dry run's `app_calls_planned`
  (`run_cases.py:3260`) counts turns, not cases, and adds the quiescence
  floor; `generate` defaults to 10–20 conversations; the README says a
  headless run will need several `wait_run.py` waits (540 s each).

## 6. Memory that outlives the session

If the app remembers per user, not per session ("remember my preferences"),
repeat 2 sees what repeat 1 said and case B sees case A. That is
contamination, and no per-case verdict can detect it. It **already affects
single-turn cases** ("remember I'm in Cairo" in case A, then case B); multi-turn
makes it likelier, not new.

`discover` records `conversation.memory: user` when it sees a user-keyed
memory store. The runner then writes `memory: user` into the manifest and
`results.json`, and the report says verdicts may be contaminated, for
single-turn and multi-turn cases alike. It is not silent, and the runner
does not pretend to reset state (it calls `environment.reset` nowhere;
CONTRIBUTING "Five fields are RESERVED"). Minting a fresh user per
conversation would need a placeholder in the identity headers, which
`build_headers` does not render; that is not Phase 1.

## 7. The output tree

The case directory keeps every file contract §6 requires today, holding the
**deciding turn** (the stopping turn, or the last), so `--verify`, the review
viewer and `analyze` read a multi-turn case without knowing it is one. The
turns are added beside them, one file per line so the contract test can
parse every condition:

```
cases/<case-id>/
  ...                                  # today's files, from the deciding turn
  turns/<t>/request.json               # only when: the case is multi-turn
  turns/<t>/response.json              # only when: the case is multi-turn
  turns/<t>/verdict.json               # only when: the case is multi-turn
  turns/<t>/expect.json                # only when: the case is multi-turn
  turns/<t>/answer.txt                 # only when: the case is multi-turn
  turns/<t>/trajectory.json            # only when: a trace was collected for this turn
  turns/<t>/trace.json                 # only when: a trace was collected for this turn
  repeats/<n>/turns/<t>/               # only when k > 1 and the case is multi-turn: the same five files
```

- `REQUIRED_IF` is keyed by bare filename today, so `turns/<t>/trajectory.json`
  cannot carry its own condition. It becomes keyed by path pattern
  (`turns/*/trajectory.json`), and the test's regex
  (`tests/test_run_cases.py:64`, which today matches one bare filename per
  line) learns those paths. The `repeats/` line keeps today's colon-less
  form on purpose, as `repeats/<n>/` does now.
- `--verify` learns the `turns/` rule: present iff the case is multi-turn,
  numbered contiguously from 1, and its count equals `verdict.json`'s
  `turns_sent`. Contract §6's "repeats/<n>/ holds three files" rule is
  updated for multi-turn repeats.
- Every turn's answer, trace and `expect.json` is on disk, so a scorer fix
  still re-scores a historical run without re-invoking the app (D9).

## 8. The rest of the plugin

| Part | Change |
|---|---|
| `generate` | Turns that do not depend on the reply's exact wording ("only the expired ones" works whatever the app said, so long as it showed licences). A checkpoint on turn 1, so a wrong start is caught where it happens. `clarify_ok` checkpoints only where the clarification is deterministic. Multi-turn cases `gating: false` until a baseline; repeats recommended. Near-duplicate detection compares whole conversations. |
| `validate_cases.py` | `multi_turn_case_reserved` becomes rules for `turns`: shape (objects with `user`), length (WARN above 8), no assistant entries, the mutual exclusion, multi-turn canary ERROR, the final-turn gradedness message, and §0's check. |
| `run_cases.py` | The conversation loop, the stop rule and union rollup, restart-on-retry, `<session>`, the cookie merge, `max_turns`, `every_turn`, the routing-row and `REQUIRED_IF` changes, the per-turn tree, row fields, the dry-run count. |
| `make_plan.py` | `execution.max_turns`. |
| `score_cost.py` | Sum turns in `case_usage`; refuse pairs with different `turns_sent`. |
| `gate.py` / report | Print the multi-turn pass rate beside the single-turn one. |
| `analyze` | "Failed at turn N" is a diagnosis of its own: memory, follow-up routing, confirmation handling. Reads inter-turn gaps (§5). |
| `optimize` | Multi-turn cases stay out of the inner keep/revert loop by default; `--include-multi-turn` opts in. They always run in the final check, and the optimize report states the candidate was selected on single-turn cases only. The blind spot (a prompt change that regresses context handling is seen only at the final check) is accepted: a loop that never finishes is worse. |
| `discover` | Writes `invocation.conversation`, including `turn_delay_s` and `memory`. |
| Holdout | Unchanged: the case is the conversation, so a conversation is sealed whole. |
| Docs | Adapter-contract hard rule 3, runner-contract §5.2/§5.5/§6/§7/§8/§9/§10, case-format, profile-schema, README (Limits, headless waits), CHANGELOG (§0's drop in `n`). The four contract-reading tests move with the text. |

## 9. Not in Phase 1

- **Client-sent history** (§2): a declared shape mapping first. It is also
  what makes "gold history" tests possible (MT-Bench-101, MultiChallenge):
  the same final turn after a fixed history, isolating one turn from
  everything before it.
- **A simulated user**: an LLM that improvises turns and can answer the app's
  clarifying question (`EVAL-DESIGN-RECOMMENDATION.md`, interactive cases).
  The natural Phase 2 is the hybrid other tools use: a scripted opening, then
  the simulator. It needs the judge's run-time scorer first, and simulators
  err often (τ²-bench measured 40–47% simulator error, §12).
- **Pre-written assistant turns** as seeded context: they only work where
  the client sends the history, and the app may never say those lines.
- **Whole-conversation and end-state checks**: "the order was looked up
  once", "the ticket ended resolved in under 10 turns" (τ-bench's database
  check, Ragas goal accuracy).
- **Continue after a failed checkpoint** (§3).
- **Per-turn token growth** in `score_cost.py`.
- **Judged quality per turn**: arrives with the judge's run-time scorer, for
  single-turn and multi-turn together.
- **Adaptive red-teaming.** Phase 1 runs *scripted* crescendo only. Attacks
  that adapt to the reply (crescendo with backtracking, GOAT) belong to
  promptfoo's red-team strategies or PyRIT, as
  `EVAL-DESIGN-RECOMMENDATION.md` §20 already says.

What Phase 1 is for: memory ("my order is #42" … "when does it arrive?"),
follow-up routing, corrections ("no, I meant Alexandria"), topic switches,
confirmation flows ("shall I cancel it?" "yes"), scripted multi-turn
jailbreaks, and authz across turns (turn 3 must not leak what turn 1 was
refused).

## 10. Decided (were open questions)

1. **`server-id` turn 1:** the `<session>` key is omitted, not sent as
   `null` (§2).
2. **`optimize`:** multi-turn cases are out of the inner loop by default,
   whatever the suite size; `--include-multi-turn` opts in (§8).
3. **Length:** the validator WARNs above 8 turns; the plan's
   `execution.max_turns` (default 12) is the hard cap (§3). The field's
   defaults sit in the same range (DeepEval recommends 4–8, PyRIT's crescendo
   runs 7, MultiChallenge caps at 10).

## 11. Effort

**One and a half to two weeks of focused work**, §0 excluded:

- Runner: the conversation loop, the stop rule and union rollup,
  restart-on-retry, `<session>` omission, the cookie jar and header merge,
  `every_turn`, `max_turns`, the routing-row and `REQUIRED_IF`/`--verify`
  changes, the per-turn tree, row fields and the dry-run count.
- `score_cost.py` (summing turns, the `turns_sent` pair rule), `gate.py`'s
  split, `make_plan.py`.
- Tests with stub apps: one that forgets history, one that stores a turn and
  then times out, one that reuses a trace id, one that sets a session cookie
  under cookie auth, one that returns the session id in a header.
- The validator, `discover`, `generate`, `analyze`, `optimize`, and the docs
  in §8.
- One live proof on RefApp, whose no-op multi-turn history should make the
  first run a wall of memory failures. That is the point, and its triage is
  budgeted.

## 12. How the field does it

Checked in the 2026-10-06 review; links as found then.

- **The same model as Phase 1** (scripted user turns replayed live in one
  session): Google ADK eval sets (https://adk.dev/evaluate/) and promptfoo's
  `stateful: true` with a `sessionParser` reading the body, a header or
  `set-cookie` (https://www.promptfoo.dev/docs/providers/http/). Both average
  per-turn scores; evalup treats the conversation as one pass/fail unit.
- **Restart, don't re-send, on a stateful target:** promptfoo disables
  backtracking when `stateful: true`
  (https://www.promptfoo.dev/docs/red-team/strategies/hydra/); Inspect
  separates request retries from whole-sample re-runs
  (https://inspect.aisi.org.uk/options.html).
- **The conversation as the statistical unit:** τ-bench's pass^k over
  episodes (https://arxiv.org/html/2406.12045); Inspect's epoch reducers
  (https://inspect.aisi.org.uk/metrics.html); "The Autocorrelation Blind
  Spot" on turn pooling (https://arxiv.org/abs/2604.14414). Many tools pool
  turns instead (DeepEval, Azure AI Foundry's per-turn mean).
- **Stop at the first failed turn:** no tool does this for scripted
  conversations; the nearest is MT-Bench-101's minimum over turns
  (https://arxiv.org/html/2402.14762), which for binary verdicts is the same
  rule. Models that take a wrong turn rarely recover
  (https://arxiv.org/html/2505.06120).
- **What evalup rightly avoids for now:** LLM user simulators (τ²-bench,
  https://arxiv.org/html/2506.07982) and averaging per-turn scores into one
  number.
