# Client-sent history — design

**Status:** **built** (2026-11-25) to this design, with §10's decisions;
proven by stub apps (`tests/test_sent_history.py`), not yet on a live app
(§8). The rules are in `runner-contract.md` §1.1/§4/§7.1 and
`adapter-contract.md`; §11 says what the code settled that this left open.
It extends multi-turn Phase 1 ([multi-turn.md](multi-turn.md)) to apps whose **client** sends the whole conversation in every request
(OpenAI-style), where Phase 1 covers apps that keep the history themselves.
It is a change to how evalup **reaches** an app (transport and intake), which
the scope rules allow; no scorer changes, and no verdict means anything new.
`SCORING_VERSION` does not move.

A separate file, not a section of `multi-turn.md`, which already runs to
450 lines. The contracts hold the rules in short; where they and this file
differ, `runner-contract.md` wins.

## 1. The model in one paragraph

The case is unchanged: an `input.turns` conversation whose author writes only
the user's turns. The runner keeps, per conversation attempt, the list of
messages a real client would hold: each user turn it sent, followed by **the
app's own reply to it**, rebuilt from that reply's response. Each turn's
request carries that list, rendered into the app's shape by a declared
mapping. Nobody writes an assistant line; an assistant entry is the text the
runner scored for that turn, byte for byte. The same case therefore runs on a
server-side app (Phase 1) and on a client-history app without a change, and a
failure at turn 2 means the same thing on both: the app did not use what it
was given.

## 2. The adapter declaration

A `history` key inside Phase 1's `invocation.conversation` block, and a new
request placeholder, `<history>`, that marks where the array goes.

```yaml
invocation:
  request_body: '{"threadId": "<uuid>", "runId": "<request id>",
                  "messages": "<history>", "tools": [], "context": []}'
  response_format: sse
  sse: {protocol: ag-ui}
  conversation:
    style: client-id        # client-id | server-id | cookie, as Phase 1, or
                            # none: the app keeps no conversation state itself
                            # (none is legal only beside history)
    history:
      preset: ag-ui         # ag-ui | openai -- or the templates below, never both
      tools: text-only      # text-only (default) | carried (§3.3)
```

`history` is orthogonal to `style`. An AG-UI app takes a `threadId` *and* the
messages, so it declares `client-id` plus `history`; a stateless
OpenAI-compatible endpoint declares `none` plus `history`. `style` stays
required, so a block still cannot turn a driver on by omission (hard rule 3).

**The explicit form**, for any app a preset does not fit. Each entry is a JSON
template for one message, rendered with the placeholders below:

```yaml
    history:
      user:      '{"role": "user", "parts": [{"type": "text", "text": "<text>"}]}'
      assistant: '{"role": "model", "parts": [{"type": "text", "text": "<text>"}]}'
      # tools: carried also needs these three:
      # tool_calls_at: "tool_calls"   # key on an assistant entry holding its calls
      # tool_call:   '{"id": "<call id>", "type": "function",
      #                "function": {"name": "<tool name>", "arguments": "<arguments>"}}'
      # tool_result: '{"role": "tool", "tool_call_id": "<call id>", "content": "<result>"}'
```

**The two presets** expand to these templates; the manifest records the
expanded form, so a run says what shape it sent.

| | `ag-ui` (AG-UI `RunAgentInput.messages`) | `openai` (Chat Completions `messages`) |
|---|---|---|
| `user` | `{"id": "<message id>", "role": "user", "content": "<text>"}` | `{"role": "user", "content": "<text>"}` |
| `assistant` | `{"id": "<message id>", "role": "assistant", "content": "<text>"}` | `{"role": "assistant", "content": "<text>"}` |
| `tool_calls_at` | `toolCalls` | `tool_calls` |
| `tool_call` | `{"id": "<call id>", "type": "function", "function": {"name": "<tool name>", "arguments": "<arguments>"}}` | the same |
| `tool_result` | `{"id": "<message id>", "role": "tool", "toolCallId": "<call id>", "content": "<result>"}` | `{"role": "tool", "tool_call_id": "<call id>", "content": "<result>"}` |

**Placeholders.** In the request body, two new ones:

- `<history>`: the rendered array, as a typed value. It must be a string
  value's *whole* text, exactly once in the body.
- `<request id>`: a fresh UUID per request sent, for a field the protocol
  wants unique per call (AG-UI's `runId`). `<uuid>` stays once per
  conversation attempt.

In the message templates: `<text>`; `<message id>` (the app's own id for an
assistant entry when its reply gave one, AG-UI's `messageId`, else a UUID
minted when the entry is built and kept for the attempt); `<call id>`,
`<tool name>`, `<arguments>`, `<result>` (a tool call's, as the reply
carried them).

**Where the current turn goes** follows from the body template. If the body
also carries `<user turn>` (`{"question": "<user turn>", "chat_history":
"<history>"}`), `<history>` holds the earlier turns only. If not, it ends
with the current user entry.

**Pre-flight (exit 3, nothing billed):** `history` outside `conversation`, or
an unknown key in it; `preset` beside a template, or neither; a template that
is not a JSON object, or a `user`/`assistant` template with no `<text>`;
`<history>` missing, repeated, or inside a longer string; `<history>` in a
body whose adapter declares no `history`; `style: none` without `history`;
`tools: carried` without the three tool templates (a preset supplies them) or
without a tool source: an AG-UI stream, or a JSON body read through
`traces.tool_calls_path`. A `data-path` stream carries no tool calls, so it
is `text-only` only.

## 3. What the runner sends each turn

### 3.1 The loop

Per conversation attempt, `H` starts empty. For turn *n*:

1. Render the user entry from turn *n*'s text, with a fresh `<message id>`.
2. Render the body with `<history>` = `H` plus that entry (or `H` alone when
   the body carries `<user turn>`), and send it.
3. Score the turn exactly as Phase 1 does; the stop rule applies first.
4. If a turn *n+1* exists, rebuild the reply into entries (§3.2, §3.3) and
   append the user entry and them to `H`. A reply that cannot be rebuilt
   stops the conversation (§3.4).

Each entry is rendered once, when it is appended, and never again, so its
ids hold across turns. **Inserted text is never scanned for placeholders**:
a user turn or an app reply containing `<uuid>` goes out as written.
`request.json` keeps the full array each turn sent; the array holds case and
app text only, never a resolved env value.

### 3.2 Text-only (the default)

One assistant entry per text message in the reply: per AG-UI `messageId`,
in stream order, or one for a JSON body. Its `<text>` is that message's
finished text. Joined with a blank line, the entries are the turn's
`answer.txt`: **what is carried is what was scored.** Tool calls and results
are not sent. A server that rebuilds the model's context from the text alone
drops any tool parts a browser sends, so for it carrying them changes
nothing.

### 3.3 Carried tool calls

For an app whose server reads tool calls and results back from the history,
as some agent frameworks do (and an OpenAI-shaped history must keep each
call paired with its result, or the provider refuses it):

- The reply's tool calls come from the same list `traces.source: response`
  reads (`id`, `name`, `arguments`, `result`), in the order the reply gave
  them.
- A call joins the assistant entry its AG-UI `parentMessageId` names. With
  no parent, it joins the last assistant entry opened since the previous
  tool result, or a new one with `<text>` `""`. The entry's calls go under
  `tool_calls_at`.
- Each call's `tool_result` entry follows its assistant entry, in call order,
  before the next assistant entry.

The AG-UI decoder (`_sse.py`) gains one additive field for this, `items`:
the text messages and tool calls in stream order, each with its id and
`parentMessageId`. `text` and `tool_calls` are unchanged, so no reader moves.

### 3.4 A reply that cannot be carried

Phase 1 continues past an `unscored` turn. Here, a turn that has a next turn
but no reply to carry ends the conversation, `unscored`, with
`stop_reason` naming why (as a server-id turn 1 with no session id does).
Sending turn *n+1* would need an assistant line the app never finished, and
inventing one is the thing this design refuses. The cases:

- no finished text: a stream ending in `RUN_ERROR` or before its terminal
  event (its `partial_text` is never carried), or a JSON body with no string
  at `<answer>`;
- an approval interrupt with no text (#103's case);
- under `carried`, a call with no id or no result: a pending approval or a
  client-side tool, which only resuming the run could answer (§6).

A `fail` on the same turn still outranks it: the stop rule runs first. The
final turn needs no carrying, so a final turn with no text scores as it does
today.

## 4. How it fits Phase 1

| Part | With client-sent history |
|---|---|
| Case format, `every_turn`, checkpoints | Unchanged. No new case key; `assistant:` stays reserved. |
| Single-turn cases | `<history>` holds the one user entry, so the body has today's shape and the verdict is the same. |
| §0's skip | Unchanged: `input.messages` with a system or assistant entry is still `skipped`. Sending it would be a seeded history (§6). |
| Stop rule, union, `final_turn_certifies` | Unchanged, plus §3.4's stop, which forces `unscored` the way `session_missing` does. |
| Retries | Unchanged: any retryable outcome restarts from turn 1. `H` is per attempt, so a reply from an abandoned attempt is never carried, and every id is fresh. Re-sending only the failed turn would be safe for a truly stateless app, but `style: none` does not prove the app stores nothing (`memory: user`), and one rule is easier to trust. |
| Repeats, resume | Unchanged: a repeat or a redone case builds its own `H`. |
| Traces | Unchanged: each turn's trace comes from that turn's own response. |
| Holdout | Unchanged: a conversation is sealed whole, and its history is built at run time, so nothing new is sealed. |
| `make_plan.py` | Unchanged: `--single-turn` drops conversations whatever the style; `max_turns` bounds the array. Discover records the app's own message cap (§5). |
| Cost | Unchanged. The app's token usage already grows with the history; `case_usage` sums turns as it does today. |
| Validator | No new case rule (`validate_cases.py` reads cases, not the adapter). |
| Output tree, `--verify` | No new file: the array is in each turn's `request.json`. §6's `# only when:` markers and `REQUIRED_IF` do not move. |
| Manifest | `conversation.history` with the preset expanded. |

## 5. What `discover` detects and writes

It reads the code, as for Phase 1 (`discover/SKILL.md` §3), and declares
nothing on a guess:

1. **Which history the model reads:** the array the client posts, a server
   store keyed by an id, or both. A server that saves the chat by thread id
   but builds the model's context from the posted array is `client-id` plus
   `history`; the store is not memory. Signs: the client posts a `messages`
   (or `history`, `chat_history`) array that grows each turn; the server
   parses it (an AG-UI `RunAgentInput` parser, a Chat Completions route) and
   hands it to the model call.
2. **The shape:** where the array sits in the body, the role names, the text
   field or parts, required fields (AG-UI requires a message `id`), and
   whether the current turn is inside the array or beside it. A preset when
   it matches exactly, else the explicit templates.
3. **Tools:** whether the server's history parser keeps tool calls and
   results or drops them. Kept ⇒ `tools: carried`; dropped or unknown ⇒
   `text-only`.
4. **Refusals and caps**, recorded as findings: roles the server refuses
   (some 400 on a client `system` message; the runner never sends one), a
   cap on messages or body bytes, against two or more entries per turn and
   `max_turns`.
5. **`memory`**, as Phase 1: `user` when a user-keyed store outlives the
   conversation.

Then it writes the block and sets `capability_matrix.multi_turn` to
`enabled`. **The probe gains a two-request history check** (`--probe`): turn
1, then turn 2 carrying turn 1's rebuilt reply, passing when both are 2xx. A
wrong mapping shows up there as a 400, before a run spends a message on
every case.

## 6. Not in scope

- **A simulated user** (Phase 2 in `multi-turn.md` §9).
- **Seeded assistant turns** (`assistant:` in a turn, or §0's
  `input.messages` sent whole): they put lines in front of the app that it
  may never say. Not cheap: they need their own validity rule, and the
  "carried is scored" invariant would no longer hold.
- **Gold-history tests** (one final turn after a fixed history): built on
  seeded turns, so they wait for them.
- **Resuming a run**: answering an approval interrupt (AG-UI `resume`) or
  running a client-side tool. A reply that needs one cannot be carried
  (§3.4).
- **Adopting an AG-UI `MESSAGES_SNAPSHOT`** as the history: see open
  question 3.
- **Pair-shaped histories** (`[["user", "bot"], ...]`): a per-message
  template cannot say them. A client that trims the history to its last *N*
  messages: v1 always sends all of it. Attachments and non-text parts.

## 7. Tests

Stub apps, each a few lines beside `tests/test_multi_turn.py`'s
`LicenceApp`:

- **Remembers only from the history:** stateless; answers a turn-2 question
  from a fact found in the posted array, "which one?" otherwise. It signs
  each reply with a nonce and answers 400 if a posted assistant entry is not
  one of its own replies, verbatim. That pins "never an invented line" and
  "carried is scored".
- **Ignores the history:** reads only the last user entry, so it fails at
  turn 2 (the client-history twin of `LicenceApp(forget=True)`).
- **Strict AG-UI:** validates `RunAgentInput` (ids on every message, a role
  set, no `system`, a `runId` not seen before) and 400s otherwise. Covers the
  preset and `<request id>`.
- **Streaming tool agent:** two text messages around a tool call with a
  `parentMessageId`. Covers text-only (two assistant entries, no tool parts)
  and carried (an entry with `toolCalls`, its result, then the next entry).
  Variants: a `RUN_ERROR` mid-conversation, a pending call, and an interrupt
  with no text. Each stops `unscored` and turn 3 is never sent.
- **Fails once at turn 2** (one 500): the restart's turn-1 request carries no
  entry from attempt 1, and every id is fresh.
- **History beside the turn** (`question` + `chat_history`): `<history>`
  holds earlier turns only.
- **Regressions:** a single-turn case under a history adapter scores as it
  does without one; function and exec mode receive the array; a reply
  containing `<uuid>` is carried unchanged.

Pre-flight: one test per refusal in §2. **Tests that move:**
`TestConversationPreflight` (the new keys and `style: none`), `test_sse.py`
(the `items` field), `test_probe.py` (the history check). No contract-reading
test moves, since no file joins the tree. `adapter-contract.md` is read on a
first session, so its new lines count against the 25,000-token budget
(`test_context_budget.py`), and discover's pointer to this file needs a
condition ("client-sent history only").

## 8. A live proof

On a real TypeScript/Cloudflare app with a streaming tool agent (AG-UI over
SSE, `traces.source: response`). Read-only cases only, with `every_turn:
{tools: {forbidden: [<every write>]}}`, paced with `invocation.rate_limit`
where the app limits requests (the dry run's `pace_floor_s` gives the time).
Run by a subagent following the skills, from `discover` on, in whichever
`tools` mode discover finds.

| # | Conversation | Turns | What it proves |
|---|---|---|---|
| P1 | A fact stated on turn 1 that no tool can return, asked back on turn 2 | 2 | The array reaches the model: only the history can carry it |
| P2 | A lookup (a tool call), then "only the ones from X" | 3 | A tool-backed reply rebuilt from the stream (text-only), per-turn tool layers from each turn's own stream |
| P3 | A question about A, then "no, I meant B" | 2 | A correction: the final answer is about B, `must_not_contain` A |
| P4 | A request the persona is refused, then two rephrasings | 3 | Authz across turns, `every_turn` on a client-history app |
| P5 | A six-turn working session | 6 | A long array: stable ids, cost summed over turns, no cap hit |

**About 70 messages:** 16 turns at `k = 1`, then the same at `k = 3` (48),
the probe's 2 and the health check; at most × `max_attempts` with retries.
**The proof passes** when the probe and every turn are 2xx (the shape is
accepted), every turn *n+1*'s request carries turn *n*'s `answer.txt`
verbatim (checked from disk), and each verdict can be explained from the
answers. A failing app verdict is a finding, not a failed proof. If the app
drops tool parts, `carried` stays proven by stubs only, and the docs say so
until an app that reads them is run.

## 9. Effort

**About a week:** the history builder and its placeholders, pre-flight,
§3.4's stop, the decoder's `items`, the probe's check, the stubs and tests,
`adapter-contract.md`, `runner-contract.md` §4/§7.1, discover and the README
(Limits). Then the live proof and its triage.

## 10. Decided (2026-11-25)

The owner accepted all five recommendations below; build to them.

1. **Build `tools: carried` now, or wait for an app that reads tool parts?**
   Recommend now. The usual client-history frameworks do send them, an
   OpenAI-shaped server needs calls and results paired, and stubs pin the
   shape. If the first live app drops tool parts, it proves `text-only`
   only, and the docs say so.
2. **Ship the `openai` preset beside `ag-ui`?** Recommend yes: it is the
   commonest client shape and costs a table row and one test.
3. **When an AG-UI stream sends `MESSAGES_SNAPSHOT`, adopt it as the
   history?** A real AG-UI client would. Recommend not in v1: rebuild from
   events always, and record `snapshot_seen` on the turn's response, so a run
   shows whether apps emit one before evalup trusts a server's rewrite of
   its own history.
4. **The probe's two-request history check:** recommend yes. Two calls
   catch a wrong mapping before a run sends turn 2 of every case into a
   400 (today `uniform_4xx` flags that only afterwards).
5. **`style: none` as an explicit value, or `style` optional beside
   `history`?** Recommend explicit: hard rule 3's "never on by accident"
   reads better with a word than with a gap.

## 11. As built (2026-11-25)

Where the code had to settle something the design left open:

- **The decoder's `items`** lists tool results too (`{type: tool_result,
  id, message_id}`), so §3.3's "since the previous tool result" can be
  read off the stream. A tool result entry's `<message id>` is the app's
  `TOOL_CALL_RESULT` `messageId` when it gave one, else a minted UUID.
- **A call whose `parentMessageId` names no text message seen yet** opens
  an entry with that id and `<text>` `""`; a text message with that id
  arriving later fills it.
- **A JSON body under `carried`** gives no order between its calls and its
  text: the calls go first, on one entry with `<text>` `""`, then their
  results, then the answer's entry.
- **AG-UI's joined text messages must equal the scored answer**, or the
  reply cannot be carried (only when `response_body` marks a field other
  than the decoded `text`): "what is carried is what was scored" is
  checked, not assumed.
- **A `${REF}` in a history template is refused** at pre-flight: the array
  holds case and app text only, and a resolved value would be recorded in
  every `request.json`.
- **A single-turn retry** gets a fresh `<request id>` and a fresh user
  entry, as a restarted conversation does.
- **`snapshot_seen`** is a top-level key of `response.json`, under an
  `ag-ui` stream with a declared history only.
- **The probe's turn 2** re-sends the same `--message`; its report's
  `history_check` carries turn 2's status, answer and request body.
- **The validator is unchanged** (§4): it reads cases, and no case rule is
  new.
