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
adapter_version: 1
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
  mode: http | function       # `cli` is reserved: runner v1 refuses it (exit 3)
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
  # conversation attempt, reused on every turn), <persona>, <case id>, and
  # <session> (the id a server-id app returned on turn 1; see `conversation`
  # below). A string that IS a placeholder becomes the typed value; one that
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
    role: X-Role-Id                       # them rather than running them under the
    user: X-User-Id                       # adapter's default identity, which would
                                          # score the wrong persona and report the
                                          # number as if it meant something.

  health_check:                           # optional. Pre-flight's one trivial call.
    method: GET                           # Undeclared -> GET base_url, and ANY response
    path: /healthz                        # (including 404) counts as "the host is up";
    expect_status: [200]                  # inventing an API call would spend money on a
                                          # shape you never declared. REQUIRED when
                                          # traces are queryable, since the trace-join
                                          # check needs a real call to join on.

  # --- what the runner may OBSERVE, all optional, all declared ---
  # The runner reads a route, a clarification and a result only from paths
  # named here. It never hunts a response for "the field that looks like a
  # route": that heuristic works on four cases and picks the wrong field on
  # the fifth. Undeclared means the layer is `unscored` with a reason, which
  # is an honest gap; a wrong field is a number that reads as a measurement.
  route_from_response: "route"            # dotted path to the route the app took.
                                          # Highest priority. Next is a collected
                                          # trace's FIRST invoke_agent span name
                                          # (gen_ai.agent.name), then the map below.
                                          # "First" is by span start time, so on a
                                          # multi_agent app whose orchestrator is itself
                                          # an invoke_agent span the trace fallback names
                                          # the ORCHESTRATOR, not the sub-agent that
                                          # handled the turn -- and no attribution exists
                                          # to tell them apart. Declare this field on any
                                          # app with a dispatching parent agent.
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
                                          # REQUIRED for cases with expect.clarify_ok:
                                          # without it the runner cannot tell whether the
                                          # app clarified, and writing `clarified: false`
                                          # would be a claim, not an observation -- one
                                          # that decides the verdict on exactly those cases.

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
                                          # NOTE: timeout_s is NOT enforced in this mode
                                          # (an in-process call cannot be interrupted from
                                          # the stdlib) -- the callable owns its timeout.
  conversation:                           # optional: how the app keeps a conversation
    style: client-id                      # (docs/multi-turn.md SS2). WITHOUT this block
                                          # every input.turns case is SKIPPED (hard rule 3).
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
  # NO `session` block. It is NOT this one: an earlier adapters/dotnet.md
  # shipped `session: {start, send_turn, end}`, and declaring a block must
  # never be enough to turn on a driver by accident (hard rule 3). The runner
  # reads `conversation` only, and checks it at pre-flight (exit 3).
  # NO streaming field: completion is EOF (http) or return (function), and
  # no TTFT is recorded anywhere. If the app answers ONLY over SSE,
  # the runner reads the raw event-stream text as the body, so point the
  # adapter at a non-streaming route or expect answer extraction to fail
  # `unscored`. Wall-clock `latency_s` IS recorded per case; TTFT is not.
  timeout_s: 60
  max_concurrency: 1           # serial by default. > 1 is REFUSED (exit 2) by
                               # run_cases.py rather than silently serialized:
                               # a field that looks supported and is not is
                               # worse than one that says no.

traces:
  source: otlp-file | view-only | none   # jaeger | tempo | clickhouse are reserved:
                                         # runner v1 refuses them (exit 3) rather than
                                         # silently scoring trace-less
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
   wrong trace). `run_cases.py` v1 can query `traces.source: otlp-file`; the
   other queryable stores exit 3 rather than being treated as trace-less, which
   would downgrade a fully instrumented app without saying so.
2. `never-live` tools present + `environment.kind: live-*` → run refuses
   categories that could trigger them.
3. **A conversation runs only through `invocation.conversation`.** A case
   written as `input.turns` (case-format.md) is sent turn by turn in one
   conversation when the adapter declares that block, and is **skipped**,
   not failed, when it does not (every turn would land in a fresh
   conversation). The block is the only switch — a legacy `session:` block
   turns nothing on. And a case whose `input.messages` holds anything besides
   exactly one user message is skipped whatever the adapter says: the runner
   would send that one message alone. (docs/multi-turn.md)
4. Env-var refs unresolved at runtime → pre-flight failure before any spend.
5. `traces.convention` other than `gen_ai` with no `traces.mapping_shim` →
   trajectory layers disabled for the run (raw spans must never reach
   `normalize_trace.py`, which would silently produce empty trajectories).

---

## Capability table — the language-agnostic seam

This is the actual contract boundary. Every row is a **capability** the harness
needs; every cell in the "adapter provides" column is a **normalized value**
core code (scorers, judge, stats, skills) is allowed to touch. Core code reads
these normalized values and NEVER the language/framework that produced them —
there is no `if language == "dotnet"` anywhere outside an adapter file. A
second adapter (Python, Go, ...) fills the same table with different mechanics
and every scorer/skill keeps working unmodified. See
`adapters/dotnet.md` for the first concrete filling of this table.

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

Two independent gates. The runner enforces both **at the case level**: an
attack-category case is skipped unless `environment.safe_to_attack` is true,
and a case naming a `never-live` tool under `environment.kind: live-*` is
skipped (hard rule 2), with the reason recorded. Per-call interception — mock
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
2. **`environment.safe_to_attack`** — a single boolean gate on the whole
   environment. `false` (the default) makes adversarial/red-team/chaos case
   categories refuse to run, full stop, regardless of individual tool
   classes — belt-and-suspenders for the case where a `needs-mock`
   classification turns out to be wrong.

When the app does build a per-call wrapper, put it in one place (not
scattered checks): before dispatching a call, look up the tool's
`side_effects` class and the environment's `kind`/`safe_to_attack`, and
either pass the call through, substitute the mock, or raise — and log that
decision so a blocked call is visible, not silently swallowed. The classes
above are identical for every adapter; only *how* a wrapper intercepts a call
(middleware, decorator, DI interceptor) differs, and that mechanics detail
belongs in the per-adapter doc (see `adapters/dotnet.md` for the .NET instance: static analysis of
`SaveChangesAsync`/`ExecuteUpdate`/`ExecuteDelete` or a `[SideEffect]`
attribute convention feeding the same wrapper contract).
