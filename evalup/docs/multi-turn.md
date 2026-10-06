# Multi-turn conversations — design

**Status:** design, not built. Written 2026-10-06. The runner is
feature-frozen (CLAUDE.md); the owner lifted the freeze for this one feature
on 2026-10-06. Nothing here changes what an existing number means: a
single-turn case scores exactly as it does today, and no scorer is rewritten.

Today a case with more than one user turn is `skipped` (adapter-contract hard
rule 3, `run_cases.skip_reason`). This note is the shape of the conversation
driver that replaces that skip, the decisions that keep its numbers honest,
and what Phase 1 leaves out.

## 0. A prerequisite bugfix — ships first, on its own

`case-format.md` says prior assistant/system messages are "fine as fixed
context" in a single-user-turn case. They are not sent: `case_text()` sends
the last user message and drops everything else, and the case scores as if
the context had reached the app. That is the truncated-conversation verdict
hard rule 3 exists to refuse, one door over. Fix: a case whose
`input.messages` holds anything besides exactly one user message is
`skipped` with that reason, and `validate_cases.py` says so at authoring
time. This is a bugfix and is legal under the freeze; it does not wait for
the rest of this note.

## 1. What a multi-turn case is

A **live scripted conversation.** The case author writes only the user's
turns, in order. The runner sends them one at a time to the real app, inside
one conversation, and the app's real replies are the history. Nobody writes
the assistant's lines.

```yaml
id: c-followup-01
persona: team_manager                  # one identity for the whole conversation
input:
  turns:
    - user: "Show me the licences for the Cairo team"
      expect: { route: licences }      # optional checkpoint on this turn
    - user: "only the expired ones"
expect:                                # the FINAL turn's checks, as today
  route: licences
  answer: { not_contains: ["which team"] }
```

- `input.turns` and `input.messages` are mutually exclusive.
  `validate_cases.py` errors on both, and on a `turns` list of length 1
  (write a single-turn case).
- **The top-level `expect` is the final turn's claim.** It keeps every
  existing authoring rule working unchanged: gradedness
  (`no_graded_layer`), metamorphic parents, the reserved-field WARNs.
- An earlier turn's `expect` is a **checkpoint**, using the same keys and
  the same scorers. A turn without one still has the implicit check every
  case has today: `score_http` passes a 2xx and fails anything else.
- Pre-written assistant turns are not part of Phase 1 (§9).

## 2. How the app keeps the conversation

The adapter declares it, in a new block. It is named `conversation`, not
`session`, on purpose: an older `adapters/dotnet.md` shipped a `session`
block, and hard rule 3's history is that declaring one must never be enough
to turn on a driver. An adapter without `invocation.conversation` keeps
today's behaviour: multi-turn cases are `skipped`.

```yaml
invocation:
  conversation:
    style: client-id | server-id | cookie
    session_from_response: "conversationId"   # server-id only: dotted path in turn 1's body
    turn_delay_s: 0                            # optional pause between turns (§5)
    memory: session | user                     # what discover found (§6)
```

| Style | The app | The runner |
|---|---|---|
| `client-id` | takes a session id the client invents (RefApp's `sessionId`) | renders `<uuid>` once per conversation attempt and reuses it on every turn; today it is already once per repeat |
| `server-id` | mints the id and returns it in turn 1's reply | reads `session_from_response` from turn 1's body and renders it into a new `<session>` placeholder on turns 2+. On turn 1 `<session>` is `null`. Turn 1 without the field ⇒ the case is `unscored` with that reason, never a turn 2 sent without a session |
| `cookie` | keeps state in a session cookie | keeps one `http.cookiejar` per conversation attempt; a static `auth: cookie` is still sent first |

Function mode: the callable is called once per turn with the rendered body;
`<uuid>`/`<session>` work the same way.

**Client-sent history (OpenAI-style, the whole history in each request) is
not Phase 1.** Every app wants a different shape (`role/content`, `parts`,
`text`, a nested `history` field), so it needs a declared mapping, and
inventing that mapping well deserves its own pass.

`discover` reads the conversation handling it already looks for
(`discover/SKILL.md` step on "conversation/session handling") and writes the
block; `profile.yaml`'s `capability_matrix.multi_turn` becomes `enabled`
when it is declared, otherwise its `blocked_by` names the missing block, a
reason the user can now act on.

## 3. Running a conversation

One conversation attempt:

1. Fresh `<uuid>`, empty cookie jar, no `<session>`.
2. For each turn: render, send, read the answer and trace id with the
   adapter's existing paths, score the turn with the existing layers.
3. **Stop at the first turn that does not roll up `pass`.** A `fail` makes
   the case `fail` at turn N; an `unscored` makes it `unscored` at turn N.
   Later turns are not sent: once a checkpoint fails, the conversation is off
   script and anything scored after it is a number built on a derailed
   conversation. `verdict.json` records `failed_turn` and that turn's reason
   ("expected route licences, got a clarification"). A per-turn
   "continue even if this fails" opt-in is left for later (§9).

The case verdict is the stopping turn's roll-up, or the final turn's when
every turn passed. `roll_up()` is unchanged and runs per turn.

**Retries restart the whole conversation.** Today a timeout, 429 or 5xx is
retried by re-sending the same request (`invoke_once`). Inside a server-side
conversation that is unsafe: the app may already have stored the turn it
timed out on, and a re-send makes it see the turn twice, so everything after
it is scored on a corrupted history. So any retryable outcome on any turn
abandons the attempt and starts a new one from turn 1 with a fresh `<uuid>`
and cookie jar, after `backoff_s`. `max_attempts` counts conversation
attempts. On exhaustion the case is `infra_error`, as today. `repeated_5xx`
(F-165) applies when every attempt ended on the same 5xx and body at the same
turn.

**Repeats** (`k > 1`) re-run the whole conversation; `fold_repeats()` and
pass^k are unchanged (§5.5).

**Resume** (§8): a conversation is one unit. A case directory without
`verdict.json` is redone from turn 1, as any unfinished case is today.

## 4. The unit of measurement is the conversation

**One conversation is one case, never N.** Turns of one conversation are not
independent samples: a wrong turn 1 drags turns 2–5 with it. Counting them as
separate trials would shrink every interval and let the keep/revert gates
call noise a win. So:

- `verdicts.jsonl`, `verdicts_for_stats.jsonl`, `stats.py`, `gate.py` and
  `run_history.py` see one row per conversation, exactly as for a single-turn
  case. Nothing downstream of the case verdict changes.
- **Routing's run-level report** (§5.2) gets one row per routing-scorable
  case today. Phase 1 adds **no** turn rows to it: the confusion matrix and
  its F1s keep meaning "single-turn routing", and a multi-turn case's routing
  is decided per turn, inside the case. A separate per-turn routing report is
  possible later, kept apart so it cannot change a number that exists.
- Multi-turn cases are flaky by construction (four turns at 0.9 each pass
  about 0.66 of the time), so `generate` recommends repeats for them, and the
  report shows a multi-turn pass rate beside the single-turn one, never only
  a blend.

## 5. Traces, timing and cost

- **Traces per turn.** Each turn's trace id comes from that turn's response,
  and the existing quiescence wait runs per turn. Each turn's tool and
  trajectory checks read only that turn's trace.
- **One trace for the whole conversation.** Some instrumentations reuse one
  trace id across turns. When two turns of one attempt return the same trace
  id, per-turn tool attribution is impossible, so the trace-dependent layers
  of that case are `unscorable` with that reason: honest degradation, the
  same rule the authz scorer follows. Slicing spans by turn is not attempted.
- **Background writes.** Some apps store history or summarise memory after
  replying. A turn sent too soon reads as "forgot the context". The optional
  `turn_delay_s` waits between turns; `discover` sets it when it sees async
  memory writes.
- **Cost grows faster than turns:** each turn re-sends a longer context.
  `score_cost.py` sums a case's turns, so a conversation is priced as one
  case. The per-turn token growth curve is useful and is later (§9). Suites
  stay small: `generate` defaults to 10–20 conversations, and the run's
  dry-run estimate counts turns, not cases.

## 6. Memory that outlives the session

If the app remembers per user, not per session ("remember my preferences"),
repeat 2 sees what repeat 1 said and case B sees case A. That is
contamination, and no per-case verdict can detect it. `discover` records
`conversation.memory: user` when it sees a user-keyed memory store. The
runner then:

- renders a fresh value into the user field when the adapter maps the user
  identity from a placeholder the runner controls; otherwise
- writes `memory: user` into the manifest and `results.json`, and the report
  says multi-turn verdicts may be contaminated. It is not silent, and the
  runner does not pretend to reset state (it calls `environment.reset`
  nowhere, CONTRIBUTING "Five fields are RESERVED").

## 7. The output tree

The case directory keeps every file §6 requires today, holding the
**deciding turn** (the stopping turn, or the last), so `--verify`, the review
viewer and `analyze` read a multi-turn case without knowing it is one. The
turns are added beside them:

```
cases/<case-id>/
  request.json  response.json  verdict.json  expect.json  answer.txt  ...
  turns/<t>/                     # only when: the case is multi-turn; t = 1..sent turns
    request.json  response.json  verdict.json  expect.json  answer.txt
    trajectory.json  trace.json  # only when: a trace was collected for this turn
  repeats/<n>/turns/<t>/         # only when: k > 1 and the case is multi-turn
```

Every turn's answer, trace and `expect.json` is on disk, so a scorer fix
still re-scores a historical run without re-invoking the app (D9). `--verify`
learns the `turns/` rule: present iff the case is multi-turn, numbered
contiguously from 1, and its count equals `verdict.json`'s `turns_sent`.
Runner-contract §6's block and `REQUIRED_IF` gain the new lines together; a
test already diffs them.

## 8. The rest of the plugin

| Part | Change |
|---|---|
| `generate` | Writes turns that do not depend on the reply's exact wording ("only the expired ones" works whatever the app said, so long as it showed licences). Puts a checkpoint on turn 1 so a wrong start is caught where it happens. Near-duplicate detection compares whole conversations. |
| `validate_cases.py` | `multi_turn_case_reserved` WARN becomes rules for `turns`: shape, length (WARN above 8), no assistant entries, the mutual exclusion in §1. |
| `analyze` | "Failed at turn N" is a diagnosis of its own: memory, follow-up routing, or confirmation handling. |
| `optimize` | Multi-turn cases are slow, so the inner keep/revert loop uses the single-turn selecting split and multi-turn cases run in the final check. Open question (§10). |
| Holdout | Unchanged: the case is the conversation, so a conversation is sealed whole. |
| Docs | Adapter-contract hard rule 3, runner-contract §5.5/§6/§7/§8/§9/§10, case-format, profile-schema, the README Limits section. The four contract-reading tests move with the text. |

## 9. Not in Phase 1

- **Client-sent history** (§2): a declared shape mapping first.
- **A simulated user**: an LLM with a hidden goal that improvises turns and
  can answer the app's clarifying question
  (`EVAL-DESIGN-RECOMMENDATION.md`, interactive cases). Non-deterministic,
  and it needs the judge, which has no run-time scorer.
- **Pre-written assistant turns** as seeded context: they only work where
  the client sends the history, and the app may never say those lines.
- **Whole-conversation checks**: "the order was looked up once", "never
  asked the same question twice".
- **Continue after a failed checkpoint** (§3).
- **Per-turn token growth** in `score_cost.py`.
- **Judged quality per turn**: arrives with the judge's run-time scorer, for
  single-turn and multi-turn together.

What Phase 1 is for: memory ("my order is #42" … "when does it arrive?"),
follow-up routing, corrections ("no, I meant Alexandria"), topic switches,
confirmation flows ("shall I cancel it?" "yes"), multi-turn jailbreaks
(crescendo) and authz across turns (turn 3 must not leak what turn 1 was
refused).

## 10. Open questions

1. `server-id`: is `null` on turn 1 right, or do apps need the field left out
   (a separate first-turn template)? Decide on the first real app.
2. `optimize`: are multi-turn cases out of the inner loop by default, or only
   when the suite is large?
3. The length limit: WARN at 8 turns, or a hard cap?

## 11. Effort

Runner: the conversation loop, the restart-on-retry rule, `server-id` and
the cookie jar, the per-turn tree, about 400 lines in `run_cases.py`. Tests:
a stub app per style (one that forgets history, one that stores a turn
before timing out, one that reuses a trace id), about 700 lines. The
validator, `discover`, `generate` and docs. One live proof on RefApp, whose
no-op multi-turn history is the bug this should catch on the first run.
Roughly a week of focused work, the §0 fix excluded.
