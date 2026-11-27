# Adapter Contract (`adapter.yaml`, in the state location)

The adapter is the ONLY thing the harness knows about an app. Everything else
derives from it. Secrets are env-var references — the schema rejects inline
literals; validation must fail loudly on any credential-looking string.

**Path resolution.** Every path below is relative to one of two roots, and each
field says which: the **state location** (this file's own directory), or the
**app root** (`app.repo`, itself resolved against the state location). Paths
never resolve against the plugin's own `${CLAUDE_PLUGIN_ROOT}/scripts/` —
those are the harness's files, not the app's.

```yaml
schema_version: 1              # evalup state-file layout; validate_cases.py warns without it
app:
  name: my-chat-app
  repo: .                      # relative to this file, or absolute; omit for endpoint-only
  repo_access: read-write | read-only   # read-only: no code patches; discover records
                               # each declined patch + its cost in findings.md,
                               # and optimize writes recommendations only
  git_pinned: true             # runs record the app's git SHA in the manifest

state_location: .evalup/   # where all eval state lives. Default: `.evalup/`
                               # inside the app repo. Any external path works —
                               # supported for read-only repos and QA sandboxes.

access_level: white | gray | black

invocation:
  mode: http | function | exec   # `cli` is refused (exit 3): use exec
  # http mode
  base_url: ${APP_BASE_URL}
  # No TLS field. An https base_url with a self-signed LOCAL dev certificate
  # needs `make_plan.py --insecure-tls` (plan execution.insecure_tls,
  # runner-contract §2); the adapter never turns verification off.
  # auth — one of these shapes (env refs only, never literals):
  auth: { type: bearer, token_env: APP_TEST_TOKEN }
  # auth: { type: headers,                          # header-based trust (custom
  #         headers: { X-User-Id: ${TEST_USER},     # permission headers)
  #                    X-User-Permissions: ${TEST_PERMS} } }
  # auth: { type: cookie, cookie_env: APP_TEST_COOKIE }
  # auth: { type: none }
  headers: { Origin: "${APP_ORIGIN}" }  # optional: sent on EVERY request (and
                                # the health check) beside auth -- the Origin
                                # (scheme://host[:port], no path) or CSRF header
                                # a guard checks. A secret as ${REF}; never a
                                # name auth or identity_map sets.

  # --- http mode: how a request is BUILT and a response is READ ---
  # These are TEMPLATES the runner substitutes into, not examples. The runner
  # never guesses an app's request shape or hunts a response for "the longest
  # string that looks like an answer" -- a heuristic that works on four cases
  # and silently picks the wrong field on the fifth. Declared here, once.
  endpoint: "POST /api/chat/ask"          # <METHOD> /<path>, appended to base_url
  request_body: '{"sessionId": "<uuid>", "message": "<user turn>"}'
  response_body: '{"message": "<answer>"}'
  # Request placeholders: <user turn> (the case's one user message, or the
  # current turn of a conversation), <uuid> (a fresh id per repeat -- per
  # conversation attempt, reused on every turn), <persona>, <case id>,
  # <session> (the id a server-id app returned on turn 1; see `conversation`
  # below), <request id> (fresh per request sent) and <history> (see
  # `history` below). A string that IS a placeholder becomes the typed value; one that
  # merely CONTAINS one gets textual substitution. An unset <session> as a
  # key's whole value DROPS the key (turn 1, every single-turn case) rather
  # than sending null.
  # Response placeholders: <answer> (REQUIRED -- which field carries the final
  # text) and optionally <trace id>. ONE template, looked up by exact,
  # case-sensitive key: an app whose error envelope spells the field
  # differently (`Message` on a 400, `message` on a 200) scores `answer:
  # unscored` on the error path, by design -- a second template tried "in
  # order" is the field-hunting this block refuses. Cases about a refusal
  # assert `expect.http.status` (and `route_from_status`), not its wording,
  # until the app returns one envelope.

  identity_map:                           # case `identity` -> headers. REQUIRED for
    permissions: X-User-Permissions       # authz cases: without it the runner SKIPS
    role: X-Role-Id                       # them rather than score the wrong persona
    user: X-User-Id                       # (the adapter's default identity).

  health_check:                           # optional. Pre-flight's one trivial call.
    method: GET                           # Undeclared -> GET base_url, and ANY response
    path: /healthz                        # (including 404) counts as "the host is up".
    expect_status: [200]                  # REQUIRED when traces are queryable: the
                                          # trace-join check needs a real call to join on.

  # --- what the runner may OBSERVE, all optional, all declared ---
  # A route, a clarification and a result are read only from paths named
  # here, never hunted for (same reason as above). Undeclared = the layer is
  # `unscored` with a reason: an honest gap, not a wrong number.
  route_from_response: "route"            # dotted path to the route the app took.
                                          # Highest priority. Next is a collected
                                          # trace's FIRST invoke_agent span name
                                          # (gen_ai.agent.name), then the map below.
                                          # "First" is by start time, so when an
                                          # orchestrator is itself an invoke_agent span
                                          # the fallback names IT, not the sub-agent.
                                          # Declare this field on any app with a
                                          # dispatching parent agent.
  route_from_status:                      # optional, trace-less apps only. Makes
    200: "<answered>"                     # status-derived routing DECLARED and per-app
    400: "__oos__"                        # instead of a runner-wide heuristic. Without
    403: "denied"                         # it, a trace-less run scores no routing layer.
                                          # Values are LITERAL labels the runner observes
                                          # verbatim: "<answered>" is that string, angle
                                          # brackets and all (write `answered` to get
                                          # `answered`); "__oos__" is score_routing.py's
                                          # out-of-scope label. Map only statuses that carry
                                          # app behaviour (2xx/4xx), never a 5xx: an
                                          # exhausted 5xx is infra_error before routing is
                                          # read (runner-contract §7).
  clarify_from_response: "needs_clarification"   # dotted path to a boolean-ish field.
                                          # REQUIRED for expect.clarify_ok cases: without
                                          # it `clarified: false` would be a claim, not
                                          # an observation, deciding those verdicts.

  # Result extraction for expect.result (oracle.md's result-extraction
  # contract), in priority order. All three optional; declare the strongest one
  # the app supports.
  result_from_tool: {tool: run_sql, field: rows}   # 1. the last matching execute_tool
                                          # span's result (JSON-parsed if the exporter
                                          # recorded it as a string); `field` is an
                                          # optional dotted path into it.
  result_from_response: "data.count"      # 2. a dotted path into the response body.
  result_pattern: "there are (\\d+)"       # 3. weakest: a regex over the prose answer,
                                          # group 1 if it has one. Use sparingly.

  # function mode (preferred when auth/HTTP is in the way)
  entrypoint: "app.chat:handle_message"   # module:callable, importable from app.repo.
                                          # Called with the rendered request_body;
                                          # returns the answer string, or
                                          # {text, status, trace_id, ...}.
                                          # NOTE: timeout_s is NOT enforced in this mode:
                                          # the callable owns its timeout.
  # exec mode (a command per call): [adapters/exec.md](adapters/exec.md)
  conversation:                           # optional: how the app keeps a conversation
    style: client-id                      # (hard rule 3). none: the app keeps no state
                                          # (legal only beside `history`).
                                          # client-id: the app takes an id the client
                                          #   invents -- the runner renders <uuid> once per
                                          #   conversation attempt and reuses it every turn;
                                          #   the request body must carry <uuid>.
                                          # server-id: the app mints the id and returns it
                                          #   on turn 1; the runner reads session_from and
                                          #   renders it into <session> on turns 2+ (the body
                                          #   must carry <session>). Turn 1 without it =>
                                          #   the case is `unscored`, never a turn 2 sent
                                          #   without a session.
                                          # cookie: the app keeps a session cookie; the runner
                                          #   keeps one jar per conversation attempt and merges
                                          #   it with auth: {type: cookie} into ONE Cookie
                                          #   header (http mode only). The jar keeps the
                                          #   stdlib's rules: a `Secure` cookie is not sent
                                          #   over http://, and a redirect's Set-Cookie is
                                          #   not kept -- point base_url at the final https
                                          #   URL, or turn 2 opens a new session.
    # session_from: { body: "conversationId" }   # server-id only, exactly one of:
    # session_from: { header: "X-Conversation-Id" }  # body (dotted path) | header
    turn_delay_s: 0                       # optional pause between turns, for an app that
                                          # writes history/memory AFTER replying
    memory: session                       # session | user: what discover found. `user` =
                                          # memory outlives the session (per user), so any
                                          # case can see an earlier one's turns; the manifest
                                          # and report say verdicts may be contaminated.
    # history: {preset: ag-ui, tools: text-only}  # client-sent history only: the
    #   body marks '"messages": "<history>"', built from the turns and the app's
    #   scored replies. Presets, templates: docs/client-history.md SS2.
  response_format: json        # | sse: a text/event-stream reply becomes {text,
  # tool_calls, error, finished, usage}; mark '{"text": "<answer>"}'. A failed
  # stream is `unscored`, as is `answer` after an approval interrupt with no
  # text; assert the text before it (case-format.md). runner-contract.md §4
  # item 2b.
  # sse: {protocol: ag-ui}       # | {protocol: data-path, text_path: a.b, done: "[DONE]"}
  timeout_s: 60
  max_concurrency: 1           # serial. > 1 is REFUSED (exit 2), never
                               # silently serialized.
  # rate_limit: {requests: 20, window_s: 600}  # and/or min_interval_s;
  # paces every call (runner-contract.md §7)

traces:
  source: otlp-file | response | view-only | none   # jaeger | tempo | clickhouse are reserved:
                                         # runner v1 refuses them (exit 3) rather than
                                         # silently scoring trace-less
  # response: tool calls read off the response (an sse body's tool_calls, or
  # a JSON list at `tool_calls_path`, default tool_calls) -- no location,
  # correlation or join. runner-contract.md §4 item 4.
  # view-only: traces exist but the runner cannot read them (a human
  # dashboard, or a store the runner has no client for -- see
  # docs/traces-jaeger.md). Treated as trace-less for scoring; discover records
  # it as a finding with the unlock path (queryable exporter or trace-id echo).
  location: ${TRACE_URL_OR_PATH}
  convention: gen_ai | openinference | openllmetry-legacy   # declared, not guessed
  mapping_shim: null           # REQUIRED when convention != gen_ai: path (relative
                               # to the state location) of the script that rewrites
                               # spans to gen_ai.* keys, run before normalize_trace.py.
                               # No shim declared -> trajectory layers stay off; raw
                               # non-gen_ai spans are never fed to the normalizer.
  correlation: traceparent-echo | response-field:<name> | none
                               # heuristic matching is FORBIDDEN.
                               # none: the app returns no trace id, so the runner
                               # cannot join -> trace-less mode for the whole run
                               # (manifest: traces.collected: false plus
                               # disabled_layers; runner-contract §4 item 4)
  completeness:
    quiescence_ms: 3000        # trace considered complete after no new spans for this long
    max_wait_s: 30             # after which the case is INFRA_INCOMPLETE (never FAIL)
  eval_sampling_override: always_on   # eval runs must not be sampled away

tools:                          # side-effect classification — from discover, human-confirmed
  - { name: search_docs,    side_effects: safe-live }
  - { name: update_ticket,  side_effects: needs-mock }
  - { name: send_email,     side_effects: never-live }

environment:
  kind: seeded-staging | mocked | live-readonly   # READ by run_cases.py (hard rule 2)
  # The three below are RESERVED: run_cases.py invokes none of them. Record
  # them if they exist, but drive them yourself, out of band, before the run
  # (validate_cases.py WARNs seed_state_not_loaded).
  seed: "tools/seed_test_db.py"            # RESERVED: per-case seeding, not called
  reset: "tools/reset_test_db.py"          # RESERVED: between cases, not called
  snapshot_state: "tools/dump_state.py"    # RESERVED: end-state assertions, not compared
  safe_to_attack: false        # READ: red-team/chaos refuse to run unless true

prompts:                        # optimizable surfaces (white-box only);
                                # paths relative to the app root
  - { id: router_system, path: prompts/router.md }
  - { id: tools_schemas, path: app/tools.py, kind: tool-descriptions }
  # kind: omit it for prompt text (system prompt, few-shot file); the one
  # value in use is `tool-descriptions`. No script reads `kind`; it tells
  # optimize which lever a surface is. No field records a rebuild: a surface
  # compiled into the app (adapters/dotnet.md, Optimizable surfaces) has that
  # cost written in findings.md, and optimize stops for a rebuild + restart
  # after each edit.

data:
  may_contain_pii: true         # gates trace-mining → dataset promotion behind redaction
  judge_may_see_production_data: false   # explicit governance decision, never implicit
```

## Hard rules the runner enforces
1. No heuristic trace correlation — `correlation` must be explicit or trajectory
   layers are disabled for the run (scored layers must never silently use the
   wrong trace). Which stores are read: the `traces.source` comment above.
2. `never-live` tools present + `environment.kind: live-*` → run refuses
   categories that could trigger them.
3. **A conversation runs only through `invocation.conversation`.** A case
   written as `input.turns` (case-format.md) is sent turn by turn in one
   conversation when the adapter declares that block, and is **skipped**,
   not failed, when it does not. The block is the only switch — a legacy `session:` block
   turns nothing on. And a case whose `input.messages` is not exactly one
   user message is skipped whatever the adapter says: the runner would send
   that message alone. (docs/multi-turn.md)
4. Env-var refs unresolved at runtime → pre-flight failure before any spend.
5. `traces.convention` other than `gen_ai` with no `traces.mapping_shim` →
   trajectory layers disabled for the run (raw spans must never reach
   `normalize_trace.py`, which would silently produce empty trajectories).

---

## Capability table — the language-agnostic seam

The contract boundary. Each row is a **capability** the harness needs; the
"adapter provides" column is the **normalized value** core code (scorers,
judge, stats, skills) reads — never the language or framework behind it, so
there is no `if language == "dotnet"` outside an adapter file, and a second
adapter fills the same table. `adapters/dotnet.md` is the first filling.

| Capability | Adapter provides to core | adapter.yaml field(s) |
|---|---|---|
| **Invoke** | `send(text, persona) → {text, trace_id}` — a plain function/HTTP call, regardless of transport. One call per turn; a conversation's turns share one session the app keeps (hard rule 3). | `invocation.*` (`invocation.conversation` for conversations) |
| **Traces** | a trace-id-addressable span tree in `gen_ai.*` keys (native or shimmed) | `traces.*` (`convention`, `mapping_shim`, `correlation`) |
| **Content capture** | on/off flag; when off, prose/arg fields are absent, not guessed | `traces` implies it; adapter's own toggle is out-of-band (see per-adapter doc) |
| **Optimizable surfaces** | a list of `{id, path, kind}` the optimizer may edit. Whether an edit needs a rebuild is not a field: discover records that cost in `findings.md`, and optimize asks the user to rebuild and restart | `prompts[]` |
| **Seed / reset / snapshot** | *(RESERVED — declared, never called; no state-diff scorer exists, so filling this row changes no verdict. Drive them out of band.)* | `environment.seed` / `.reset` / `.snapshot_state` |
| **Oracle** | a read-only connection/handle the harness can query for ground truth | `oracle:` — [oracle.md](oracle.md) |
| **Tool catalog + side-effect class** | `[{name, schema, side_effects: safe-live\|needs-mock\|never-live}]` | `tools[]` |
| **Access level + safe-to-attack** | `white\|gray\|black` + a boolean gate | `access_level`, `environment.safe_to_attack` |

If an adapter cannot fill a row, it says so explicitly (`traces.source: none`,
`oracle.kind: none`, empty `prompts:`) and the capability matrix in
`profile.yaml` (written by discover) turns the dependent layers off with a
`blocked_by` reason — core code never infers absence from a missing key, it
checks the declared value.

---

## Safe-to-attack + per-tool side-effect enforcement

Two independent gates. The runner enforces both **per case**, recording why
it skipped one: item 2 below, and hard rule 2 for a `never-live` tool under
`environment.kind: live-*`. Per-call interception — mock
substitution, refusing one call — is a wrapper the app's own team builds; the
runner neither provides nor observes it. Neither gate is ever left to a prompt
instruction or inferred from context:

1. **Per-tool side-effect class** (`tools[].side_effects`, discover-inferred,
   human-confirmed):
   - `safe-live` — read-only; free to call against a live/staging backend.
   - `needs-mock` — mutates shared state; the wrapper must substitute a mock/
     stub implementation whenever `environment.kind` is not a disposable
     seeded fixture the run owns exclusively.
   - `never-live` — has an external side effect (email, payment, ticket);
     the wrapper must refuse the call outright unless the adapter explicitly
     marks the destination as a sandboxed double (e.g. a test email provider).
2. **`environment.safe_to_attack`** — one boolean for the whole
   environment. `false` (the default) skips every attack-category case
   whatever the tool classes say: a backstop for a wrong `needs-mock`
   classification.

A per-call wrapper (pass, mock or raise, logged) is the per-adapter doc's
(`adapters/dotnet.md` for .NET).
