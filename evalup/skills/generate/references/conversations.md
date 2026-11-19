# Conversations (`input.turns`)

Read this only when the adapter declares `invocation.conversation`
(adapter-contract.md); without it every conversation case is skipped, so
none gets written. Everything else about a case — ids, splits, the
top-level `expect` — is [case-format.md](case-format.md).

A **live scripted conversation**: the author writes only the user's turns, in
order; the runner sends them one at a time to the real app inside one
conversation, and the app's real replies are the history. Nobody writes the
assistant's lines. It tests the app's *own* memory — the bug class this exists
to catch. Spec: `${CLAUDE_PLUGIN_ROOT}/docs/multi-turn.md`.

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
expect:                                # the FINAL turn's checks, as for any case
  route: licences
  answer: { must_not_contain: ["which team"] }
gating: false                          # until a baseline exists (generate SS3)
```

- **Each turn is an object with a `user` key, never a bare string** — later
  phases add keys beside it (`bad_turn`). `assistant:` and `simulate:` are
  reserved for those phases and refused now (`turn_key_reserved`); any other
  key is a typo (`unknown_turn_key`).
- **At least two turns** (`single_turn_conversation`: write a single-turn
  case instead); a WARN above 8 (`long_conversation`). The plan's
  `execution.max_turns` (default 12) is the runner's hard cap: a longer case
  is skipped.
- **`input.turns` and `input.messages` are mutually exclusive**
  (`turns_and_messages`).
- **The top-level `expect` is the final turn's claim**, so every authoring
  rule reads it unchanged: gradedness, metamorphic parents, the reserved-field
  WARNs. A case whose only graded check sits on a checkpoint is a
  `no_graded_layer` ERROR ("put a graded check on the final turn"), and a
  final turn carrying its own `expect:` is `final_turn_expect`.
- **An earlier turn's `expect` is a checkpoint**, with the same keys and the
  same scorers. A turn without one still gets the implicit HTTP check: a 2xx
  passes, a 3xx or a 4xx other than 429 fails, a 5xx is `infra_error`.
- **The conversation stops at the first turn that rolls up `fail` or
  `infra_*`**; it continues past `unscored` (the harness could not look,
  which is not the app failing). The case verdict is the rollup over every
  sent turn's layers, so one failed checkpoint fails the case; a passing
  checkpoint never passes it on its own: the final turn must pass.
- **`every_turn`** copies its keys into every turn's expect, the final one
  included. Without it, `expect.tools.forbidden` checks only the turn it sits
  on, and "never call `update_licence` during the conversation" silently
  means "not on the last turn". Objects merge key by key (`every_turn.tools.
  forbidden` beside a turn's `tools.subset` checks both); a value both set is
  `every_turn_collision`. On a case without `input.turns` it is
  `every_turn_without_turns`.
- **A `clarify_ok` checkpoint cannot branch.** If the app *may* ask a
  clarifying question, the scripted next turn is either its answer or a
  non-sequitur, so use one only where the clarification is deterministic.
- **A conversation canary is an ERROR** (`multi_turn_canary`): conversations
  are flaky by construction, and a failing canary aborts the run.
- **It runs only when the adapter declares `invocation.conversation`**
  (adapter-contract.md). Without it the case is `skipped` with that reason.
- **One conversation is one case** for every count, interval and gate — never
  N turns. Its `latency_s` and token counts are sums over the turns it sent.
