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
                               # each declined patch + its cost in findings.md
  git_pinned: true             # runs record the app's git SHA in the manifest

state_location: .agent-eval/   # where all eval state lives. Default: `.agent-eval/`
                               # inside the app repo. Any external path works —
                               # supported for read-only repos and QA sandboxes.

access_level: white | gray | black

invocation:
  mode: http | function | cli
  # http mode
  base_url: ${APP_BASE_URL}
  # auth — one of these shapes (env refs only, never literals):
  auth: { type: bearer, token_env: APP_TEST_TOKEN }
  # auth: { type: headers,                          # header-based trust (custom
  #         headers: { X-User-Id: ${TEST_USER},     # permission headers)
  #                    X-User-Permissions: ${TEST_PERMS} } }
  # auth: { type: cookie, cookie_env: APP_TEST_COOKIE }
  # auth: { type: none }
  # function mode (preferred when auth/HTTP is in the way)
  entrypoint: "app.chat:handle_message"   # module:callable
  # session contract — REQUIRED for multi-turn evals; without it, single-turn only
  session:
    start: {...}               # how to open a conversation (endpoint/args)
    send_turn: {...}           # send one user turn → returns final text
    end: {...}
  streaming: none | sse | websocket   # how to detect "response complete"; capture TTFT
  timeout_s: 60
  max_concurrency: 1           # serial by default; raise only if state-safe

traces:
  source: otlp-file | jaeger | tempo | clickhouse | view-only | none
  # view-only: traces exist but are not programmatically readable (e.g. a
  # human-only dashboard). Treated as trace-less for scoring; discover records
  # it as a finding with the unlock path (queryable exporter or trace-id echo).
  location: ${TRACE_URL_OR_PATH}
  convention: gen_ai | openinference | openllmetry-legacy   # declared, not guessed
  mapping_shim: null           # REQUIRED when convention != gen_ai: path (relative
                               # to the state location) of the script that rewrites
                               # spans to gen_ai.* keys, run before normalize_trace.py.
                               # No shim declared -> trajectory layers stay off; raw
                               # non-gen_ai spans are never fed to the normalizer.
  correlation: traceparent-echo | response-field:<name>     # heuristic matching is FORBIDDEN
  completeness:
    quiescence_ms: 3000        # trace considered complete after no new spans for this long
    max_wait_s: 30             # after which the case is INFRA_INCOMPLETE (never FAIL)
  eval_sampling_override: always_on   # eval runs must not be sampled away

tools:                          # side-effect classification — from discover, human-confirmed
  - { name: search_docs,    side_effects: safe-live }
  - { name: update_ticket,  side_effects: needs-mock }
  - { name: send_email,     side_effects: never-live }

environment:
  kind: seeded-staging | mocked | live-readonly
  # seeded-staging unlocks state-diff ground truth and simulated users.
  # These are YOUR app's scripts, relative to the app root:
  seed: "tools/seed_test_db.py"            # per-case seeding
  reset: "tools/reset_test_db.py"          # between cases
  snapshot_state: "tools/dump_state.py"    # for end-state assertions
  safe_to_attack: false        # red-team/chaos refuse to run unless true

prompts:                        # optimizable surfaces (white-box only);
                                # paths relative to the app root
  - { id: router_system, path: prompts/router.md }
  - { id: tools_schemas, path: app/tools.py, kind: tool-descriptions }

data:
  may_contain_pii: true         # gates trace-mining → dataset promotion behind redaction
  judge_may_see_production_data: false   # explicit governance decision, never implicit
```

## Hard rules the runner enforces
1. No heuristic trace correlation — `correlation` must be explicit or trajectory
   layers are disabled for the run (scored layers must never silently use the
   wrong trace).
2. `never-live` tools present + `environment.kind: live-*` → run refuses
   categories that could trigger them.
3. Missing `session` contract → multi-turn cases skipped and reported as
   skipped, not failed.
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
| **Invoke** | `start_session / send_turn(text, persona) / end_session → {text, trace_id}` — a plain function/HTTP call, regardless of transport | `invocation.*` |
| **Traces** | a trace-id-addressable span tree in `gen_ai.*` keys (native or shimmed) | `traces.*` (`convention`, `mapping_shim`, `correlation`) |
| **Content capture** | on/off flag; when off, prose/arg fields are absent, not guessed | `traces` implies it; adapter's own toggle is out-of-band (see per-adapter doc) |
| **Optimizable surfaces** | a list of `{id, path, kind}` the optimizer may edit, plus whether editing needs a rebuild | `prompts[]` |
| **Seed / reset / snapshot** | three callables: load fixture, wipe state, hash/dump state | `environment.seed` / `.reset` / `.snapshot_state` |
| **Oracle** | a read-only connection/handle the harness can query for ground truth | see `oracle:` block below |
| **Tool catalog + side-effect class** | `[{name, schema, side_effects: safe-live\|needs-mock\|never-live}]` | `tools[]` |
| **Access level + safe-to-attack** | `white\|gray\|black` + a boolean gate | `access_level`, `environment.safe_to_attack` |

If an adapter cannot fill a row, it says so explicitly (`traces.source: none`,
`oracle.kind: none`, empty `prompts:`) and the capability matrix in
`profile.yaml` (A7-owned) turns the dependent layers off with a
`blocked_by` reason — core code never infers absence from a missing key, it
checks the declared value.

---

## Oracle capability — read-only ground truth, never the write path

The oracle is how the runner computes `expected` for the execution layer
(`score_execution.py`, `expect.result` — see `case-format.md`) **offline**,
before the run, from a reference query against a seeded fixture or a
read-only data connection — never by asking the app under test what it
thinks the answer is (that would grade the app against itself).

```yaml
oracle:                          # ground-truth data access. NEVER the app's
                                  # write connection — a harness bug must not
                                  # be able to corrupt the app's data.
  kind: sql | http | none        # connection PROTOCOL. Distinct from
                                  # profile.yaml's oracle.provenance (seeded-fixture/
                                  # live-db-dual/api-endpoint), which records HOW ground
                                  # truth is constructed — deliberately different axes.
  connection_env: ORACLE_DSN     # env-var ref only (same rule as everywhere
                                  # else in this file: no inline secrets)
  read_only_enforced: true       # the connection's role/grant is structurally
                                  # SELECT-only (Postgres `GRANT SELECT`,
                                  # SQL Server `db_datareader`, a read replica,
                                  # or an HTTP oracle whose endpoints are all
                                  # GET). Pre-flight: attempt one write-shaped
                                  # probe and require it to be rejected before
                                  # trusting the oracle for a run.
  persona_scoping: set-role | http-headers | none
                                  # for authz cases (§ expect.authz): the oracle
                                  # must be queried UNDER the same access scope
                                  # the persona has (Postgres `SET ROLE`/
                                  # `SET app.current_user_id`, or persona
                                  # headers on an HTTP oracle) — never computed
                                  # by hand-filtering the full-access answer,
                                  # or the oracle stops exercising the same
                                  # enforcement layer the app does.
```

Dual-oracle option (recommended when there's no seeded fixture, only a live
DB): compute `expected` two independent ways (e.g. raw SQL vs. an ORM/pandas
query) and keep the case only if both agree — disagreement means the question
is ambiguous and belongs back in generate, not in the dataset.

---

## Result-extraction contract — how `actual` reaches the scorer

`score_execution.py` is pure comparison; it never talks to the app or the
oracle. Producing its two inputs is the runner's job, and this is the exact
handoff:

- **`expected`** — computed **offline**, before the run, from the oracle (see
  above) or a frozen fixture, and stored on the case as `expect.result.scalar`
  or `expect.result.rows` (+ optional `reference_query` as provenance-only
  documentation — never re-executed at score time). Regenerate it from the
  reference query whenever the fixture changes; never hand-edit a stored
  count.
- **`actual`** — extracted by the runner **after** the app responds, from
  whichever of these the adapter's capabilities support, in priority order:
  1. A tool-result span (`execute_tool` span's output attribute) matched to
     the case via the trace's `correlation` — the strongest source, since it's
     the same data the agent reasoned over.
  2. A structured field in the app's response (adapter `invoke` mode returns
     more than free text — e.g. a JSON payload alongside the chat reply).
  3. A parsed value from the prose answer, only if 1–2 are unavailable and the
     adapter declares a stable extraction pattern — the weakest source, use
     sparingly.
  The result is written as the tiny JSON file `score_execution.py` expects:
  `{"scalar": <v>}` or `{"rows": [...]}`.
- **Unscorable, not fail** — when none of the above yields a value (no content
  capture, no structured field, unparseable prose), the runner writes
  `{"missing": true, "reason": "<why>"}`. The scorer turns this into
  `verdict: unscored` with `unscorable: 1` — an unread result must never be
  scored as a mismatch (that manufactures a failure). This is the same
  `unscorable` discipline used everywhere else in the taxonomy
  (`pass | fail | unscored | infra_error | infra_incomplete`).

---

## Safe-to-attack + per-tool side-effect enforcement

Two independent gates, both enforced **in code, in the tool-execution
wrapper** — never left to a prompt instruction, and never inferred from
context:

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

The enforcement point is a single wrapper around every tool invocation (not
scattered checks): before dispatching a call, it looks up the tool's
`side_effects` class and the environment's `kind`/`safe_to_attack`, and
either passes the call through, substitutes the mock, or raises — and that
decision is recorded on the case result so a blocked call is visible, not
silently swallowed. Because the check lives in the wrapper rather than the
adapter's language runtime, this rule is identical for every adapter; only
*how* a given adapter's wrapper intercepts a call (middleware, decorator,
DI interceptor) differs, and that mechanics detail belongs in the per-adapter
doc (see `adapters/dotnet.md` for the .NET instance: static analysis of
`SaveChangesAsync`/`ExecuteUpdate`/`ExecuteDelete` or a `[SideEffect]`
attribute convention feeding the same wrapper contract).
