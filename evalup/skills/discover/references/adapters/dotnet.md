# Reference adapter: ASP.NET Core / EF Core (.NET)

This is **one adapter implementing the contract in `../adapter-contract.md`** —
the first one built, for an ASP.NET Core / EF Core app (RefApp). Nothing
here is special-cased into core: every scorer, the judge, `stats.py`, and the
skills consume only the normalized capability values the contract defines
(invoke, traces, content-capture, optimizable surfaces, seed/reset/snapshot,
oracle, tool-catalog+side-effects, access-level+safe-to-attack). A Python
adapter (FastAPI/Flask, OTel SDK auto-instrumentation, pytest fixtures /
testcontainers-python, prompts in `.py`/`.jinja`/`.yaml` files) fills the
exact same table with
different mechanics and nothing outside this file changes. Treat this doc as
the template to clone when writing the second adapter, not as .NET-specific
scope creep in the tool itself.

## Invoke

- **Test host:** the harness's `function` mode imports a Python callable, so
  a .NET app is always `invocation.mode: http` against something listening on
  a port — real Kestrel, or a test host started from
  `WebApplicationFactory<Program>` (`Microsoft.AspNetCore.Mvc.Testing`) with
  a bound URL. The latter needs `public partial class Program {}` if top-level
  statements left `Program` internal (a no-op for the app). In-process
  `WebApplicationFactory` without a port is for the team's own .NET tests, not
  for the harness.
- **Per-persona auth without minting real JWTs:** register a
  `TestAuthHandler : AuthenticationHandler<AuthenticationSchemeOptions>` via
  `builder.ConfigureTestServices(...)` so each authz case (§ `expect.authz`)
  runs as a specific permission persona by construction, not by faking a
  token. This is the mechanism the RLS/permission-matrix cases in
  `expect.authz` run through.
- **Freeze-clock requirement:** pin `TimeProvider` (or the app's `IClock`
  abstraction) via DI substitution in the test factory, so
  `reference_query`/`snapshot_timestamp` date-window cases ("expiring this
  month") are reproducible — see `case-format.md`'s absolute-date-range
  normalization.
- **Staging/e2e path:** real Kestrel + `HttpClient`, same `invocation.mode:
  http` shape, no adapter-code difference — only how the test host is started.
- **adapter.yaml mapping:**
  ```yaml
  invocation:
    mode: http                # the harness's `function` mode imports a Python
                              # callable, so a .NET app is always http: real
                              # Kestrel, or a test host listening on a port
    base_url: ${APP_BASE_URL}
    endpoint: "POST /api/chat/ask"
    request_body: '{"sessionId": "<uuid>", "message": "<user turn>"}'
    response_body: '{"message": "<answer>"}'
    auth: { type: headers, headers: { X-Test-Persona: ${TEST_PERSONA} } }
    # TestAuthHandler reads X-Test-Persona and swaps claims accordingly
  ```

## Traces (content capture, tool spans, correlation)

The three bullets below were verified on 2026-09-24 against
`Microsoft.Extensions.AI` 10.10.0 (released 2026-09-14) and the
`dotnet/extensions` sources at tagged releases; the version numbers say when
each behaviour arrived, so an app pinned to an older package reads differently.

- **Modern path:** `Microsoft.Extensions.AI` `ChatClientBuilder` →
  `.UseFunctionInvocation()` → `.UseOpenTelemetry(sourceName: "RefApp.Chat",
  configure: c => c.EnableSensitiveData = true)`, with the tracer provider
  subscribed via `.AddSource("RefApp.Chat")`. Name the arguments:
  `UseOpenTelemetry`'s first positional parameter is an `ILoggerFactory`, so
  passing the source name positionally does not compile. Keep this builder
  order — `FunctionInvokingChatClient` takes its `ActivitySource` from the
  client inside it, so `UseOpenTelemetry` after `UseFunctionInvocation` is
  what gives the tool spans below a source; reverse it and they vanish.
- **Content capture: `EnableSensitiveData`, which since 9.10.0 (2025-10)
  defaults from `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`.** Only
  the literal value `true` is honored, it is read once at startup, and setting
  the property in code overrides it. So the contract's "content capture:
  on/off" can be an environment toggle after all — provided nothing in the
  app sets the property explicitly. On 9.9.0 or earlier the env var is
  ignored and the property is the only switch.
- **Tool spans come built in since 9.5.0 (2025-05).** `OpenTelemetryChatClient`
  itself emits only the `chat` span, but `FunctionInvokingChatClient` starts
  an `execute_tool <name>` activity per call, tagged `gen_ai.operation.name`,
  `gen_ai.tool.name` and `gen_ai.tool.call.id` (arguments and result only
  under `EnableSensitiveData`), and since 9.10.0 wraps the loop in an
  `orchestrate_tools` span. Do not add your own `execute_tool` wrapper on
  those versions — it duplicates every span. A .NET target that still looks
  trace-less on first discovery usually has the builder order above reversed,
  a source name that `AddSource` does not match, or a pre-9.5.0 package.
  (Semantic Kernel auto-spans function executions under source name
  `"Microsoft.SemanticKernel"`; function arguments/results are gated by the
  `ILogger` `Trace` level and model prompts/completions by the
  `Microsoft.SemanticKernel.Experimental.GenAI.EnableOTelDiagnosticsSensitive`
  AppContext switch — different knobs, same concept.)
- **Trace-id echo:** middleware setting
  `Response.Headers["X-Trace-Id"] = Activity.Current?.TraceId.ToString()` in
  `OnStarting`. Do **not** use `HttpContext.TraceIdentifier` — it is an
  ASP.NET request id, not the OTel trace id, and using it silently breaks
  `correlation`. Incoming W3C `traceparent` is auto-parsed into
  `Activity.Current` since .NET 5, so multi-hop correlation needs no extra
  code.
- **Anthropic models from .NET:** any `IChatClient` implementation (the
  official Anthropic C# SDK, or the community `Anthropic.SDK`) drops into the
  same `.UseOpenTelemetry()` pipeline unchanged.
- **adapter.yaml mapping:**
  ```yaml
  traces:
    source: otlp-file            # or your OTel collector's export target
    location: ${TRACE_URL_OR_PATH}
    convention: gen_ai            # native: MEAI >= 9.5.0 emits execute_tool spans
    correlation: response-field:trace_id   # the X-Trace-Id echo above
  ```

## Optimizable surfaces

- **No rebuild needed (ideal):** Semantic Kernel `skprompt.txt` / prompt-YAML
  files, or any runtime-loaded `.txt`/`.md` prompt file the app reads from
  disk at startup/request time.
- **Rebuild required:** prompts embedded as C# string literals,
  `[Description]` attributes on tool methods, or compiled embedded resources.
  Editing these needs a Roslyn-aware edit (not a blind text patch — attribute
  values and interpolated strings are easy to corrupt) followed by
  `dotnet build`.
- **Check this on day one of discovery** — it sets the optimize loop's
  iterate-cycle time (file-edit vs. file-edit-plus-rebuild). If the team
  hasn't externalized prompts, discover's patch offer (see `discover/SKILL.md`
  step 5) should pitch the one-time extraction-to-file refactor specifically
  because it collapses this cost.
- **adapter.yaml mapping:**
  ```yaml
  prompts:
    - { id: router_system, path: Prompts/router.skprompt.txt }
    - { id: tool_descriptions, path: Tools/DomainTools.cs, kind: tool-descriptions }
      # kind: tool-descriptions + a .cs path is the tell that this surface
      # needs a rebuild — discover should record that cost, not hide it.
  ```

## Seed / reset / snapshot (ground truth mechanics)

- **Real disposable engine, not EF InMemory.** `Testcontainers.PostgreSql` /
  `Testcontainers.MsSql` spin up an actual engine per test run.
  **Do not use EF Core's InMemory provider** — it can't enforce transactions,
  constraints, or real date/time semantics, and a roster app's correctness is
  exactly date-and-constraint-heavy (shift overlaps, license expiry windows,
  unique-per-period assignments). InMemory-based "ground truth" would diverge
  from what production actually enforces.
- **Reset between cases:** **Respawn** (`Respawner.ResetAsync`) — cheaper than
  a container restart. Set `TablesToIgnore` to protect reference/lookup
  tables that don't need re-seeding every case.
- **Seed per case:** plain `dbContext.AddRange(...)` +
  `SaveChangesAsync()` with fixed IDs/dates — **not** EF's `HasData` (that's
  migration-time seed data, not per-case fixture data, and doesn't compose
  with Respawn resets).
- **Oracle connection = a second, separate DB role.** `GRANT SELECT`
  (Postgres) or `db_datareader` (SQL Server), pointed at the same seeded
  database but structurally unable to write — this is what
  `oracle.read_only_enforced` in the contract cashes out to concretely. Never
  reuse the app's write connection string for the oracle, even read-only by
  convention; use a role the database itself enforces.
- **adapter.yaml mapping:**
  ```yaml
  environment:
    kind: seeded-staging
    # RESERVED: recorded here, invoked by nothing (adapter-contract.md). Run
    # the seed/reset yourself before the suite -- run_cases.py will not.
    seed: "Fixtures/SeedCase.cs"        # a small CLI runner, or a dotnet test category
    reset: "Fixtures/RespawnReset.cs"
    snapshot_state: "Fixtures/DumpState.cs"
    safe_to_attack: false
  oracle:
    kind: sql
    connection_env: ORACLE_DSN          # SELECT-only role, same seeded DB
    read_only_enforced: true
    persona_scoping: set-role           # Postgres SET ROLE / SQL Server EXECUTE AS per persona
  ```

## Tool catalog + side-effect classification

- **Discovery without a model call:**
  `chatOptions.Tools.OfType<AIFunction>()` exposes `.Name`, `.Description`,
  `.JsonSchema` directly. Semantic Kernel equivalent:
  `kernel.Plugins.SelectMany(p => p).Select(f => f.Metadata)`.
- **No built-in read/write flag** — .NET tool metadata doesn't carry a
  side-effect marker, unlike some frameworks. Infer it one of two ways and
  record which:
  1. Static analysis: grep each tool method body for
     `SaveChangesAsync` / `ExecuteUpdate` / `ExecuteDelete` (or any DbContext
     write call) → `needs-mock` at minimum, `never-live` if it also calls an
     external service (email/payment/ticket client).
  2. A `[SideEffect(SideEffectClass.NeedsMock)]`-style attribute convention
     the team adopts going forward, read reflectively — more reliable than
     grep once adopted, worth pitching during the discover patch-offer step.
- Either way, feed the result into the same `tools[].side_effects` field the
  contract defines and the tool-execution wrapper enforces — the
  classification mechanism is .NET-specific, the enforcement is not (see
  `../adapter-contract.md`'s "Safe-to-attack + per-tool side-effect
  enforcement" section).

## Access level + safe-to-attack

- Persona identity travels as either a bearer token (`TEST_USER_TOKEN` env,
  real staging) or a trust header consumed by `TestAuthHandler` (fast inner
  loop) — both map to the contract's `auth: { type: bearer | headers }`
  shapes, nothing new needed.
- `access_level` (white/gray/black) is a discovery-time judgment call, not
  computed from .NET specifics — set by the same rule every adapter uses
  (repo path given → white-box, endpoint+traces → gray-box, endpoint only →
  black-box; see `discover/SKILL.md` step 1).

## Interop option — don't reinvent, shell out

`Microsoft.Extensions.AI.Evaluation` is a real first-party package with
`ToolCallAccuracyEvaluator` / `IntentResolutionEvaluator` /
`TaskAdherenceEvaluator` — directly on point for a router→executor shape —
plus a `dotnet aieval` CLI for reports. Two legitimate uses:
- Shell out to it for a `dotnet test`-native smoke path the .NET team already
  trusts, in parallel with (not instead of) this harness's scorers/judge.
- At minimum, mirror its `IEvaluator` / `EvaluationResult` / `NumericMetric`
  shapes if you ever need to hand a result to a .NET-side consumer — it's
  cheap interoperability, not a dependency.
This does not replace the execution-accuracy scorer, the judge, or the authz
scorer — it's a second, .NET-native data point you can cross-check against,
useful for building trust with a team that already uses the Microsoft
tooling.

## Full worked adapter.yaml (illustrative — RefApp shape)

```yaml
adapter_version: 1
app:
  name: refapp
  repo: .
  repo_access: read-write
  git_pinned: true

state_location: .evalup/
access_level: white

invocation:
  mode: http
  base_url: ${APP_BASE_URL}
  endpoint: "POST /api/chat/ask"
  request_body: '{"sessionId": "<uuid>", "message": "<user turn>"}'
  response_body: '{"message": "<answer>"}'
  auth: { type: headers, headers: { X-Test-Persona: ${TEST_PERSONA} } }
  # No `streaming:`. The app streams SSE to its real UI; the harness reads a
  # complete response and measures no TTFT, so declaring it here would teach a
  # key nothing reads (adapter-contract.md). Point `invocation` at the
  # non-streaming route the tests already use.
  timeout_s: 60
  max_concurrency: 1

traces:
  source: otlp-file
  location: ${TRACE_URL_OR_PATH}
  convention: gen_ai
  correlation: response-field:trace_id   # X-Trace-Id echo middleware
  completeness: { quiescence_ms: 3000, max_wait_s: 30 }
  eval_sampling_override: always_on

tools:
  - { name: get_licence_expirations, side_effects: safe-live }
  - { name: assign_shift,            side_effects: needs-mock }
  - { name: send_expiry_notice,      side_effects: never-live }

environment:
  kind: seeded-staging
  seed: "Fixtures/SeedCase.cs"          # RESERVED -- see above; you run these, not the harness
  reset: "Fixtures/RespawnReset.cs"     # RESERVED
  snapshot_state: "Fixtures/DumpState.cs"   # RESERVED
  safe_to_attack: false

oracle:
  kind: sql
  connection_env: ORACLE_DSN
  read_only_enforced: true
  persona_scoping: set-role

prompts:
  - { id: router_system, path: Prompts/router.skprompt.txt }
  - { id: tool_descriptions, path: Tools/DomainTools.cs, kind: tool-descriptions }

data:
  may_contain_pii: true
  judge_may_see_production_data: false
```

## The generality point, restated

Every field above is just this adapter's filling of the capability table in
`../adapter-contract.md`. A Python adapter for an equivalent FastAPI/LangGraph
app would set `invocation.entrypoint: "app.chat:handle_message"`,
`traces.convention: gen_ai` via OTel SDK auto-instrumentation (env-var content
capture, unlike .NET), `oracle.connection_env` to a read-only Postgres DSN via
`testcontainers-python`, and `prompts:` to `.py`/`.jinja` file paths — and
every scorer, the judge, `stats.py`, and every skill in this plugin would run
against it completely unchanged. That is the whole point of the seam: .NET
knowledge lives here and in discover's stack-detection step, nowhere else.
