# Reference adapter: ASP.NET Core / EF Core (.NET)

This is **one adapter implementing the contract in `../adapter-contract.md`** —
the first one built, for an ASP.NET Core / EF Core app (RefApp). Nothing
here is special-cased into core: every scorer, the judge, `stats.py`, and the
skills consume only the normalized capability values the contract defines
(invoke, traces, content-capture, optimizable surfaces, seed/reset/snapshot,
oracle, tool-catalog+side-effects, access-level+safe-to-attack). A Python
adapter (FastAPI/Flask, OTel SDK auto-instrumentation honoring
`OTEL_..._CAPTURE_MESSAGE_CONTENT`, pytest fixtures / testcontainers-python,
prompts in `.py`/`.jinja`/`.yaml` files) fills the exact same table with
different mechanics and nothing outside this file changes. Treat this doc as
the template to clone when writing the second adapter, not as .NET-specific
scope creep in the tool itself.

## Invoke

- **Fast inner loop:** `WebApplicationFactory<Program>`
  (`Microsoft.AspNetCore.Mvc.Testing`) — in-process, no real Kestrel, fastest
  iteration. Requires `public partial class Program {}` if top-level
  statements left `Program` internal (add it if missing; it's a no-op for the
  app).
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
    mode: function            # WebApplicationFactory in-process, or http for real Kestrel
    entrypoint: "RefApp.Program"     # partial-class marker for WebApplicationFactory<Program>
    auth: { type: headers, headers: { X-Test-Persona: ${TEST_PERSONA} } }
    # TestAuthHandler reads X-Test-Persona and swaps claims accordingly
  ```

## Traces (content capture, tool spans, correlation)

- **Modern path:** `Microsoft.Extensions.AI` `ChatClientBuilder` →
  `.UseFunctionInvocation()` → `.UseOpenTelemetry(sourceName, c =>
  c.EnableSensitiveData = true)`, registered with
  `.AddSource("RefApp.Chat")`.
- **`EnableSensitiveData` is the .NET content-capture toggle — it is a CODE
  FLAG, not the `OTEL_..._CAPTURE_MESSAGE_CONTENT` env var.** .NET does not
  honor that env var at all. If you want an environment-driven toggle (the
  contract's "content capture: on/off"), read your own env var at startup and
  set `EnableSensitiveData` from it — this is app code you write once, not
  something the harness can flip from outside.
- **Gotcha — tool calls are NOT auto-spanned.** The built-in
  `OpenTelemetryChatClient` only emits the `chat` span. Wrap every
  `AIFunction.InvokeAsync` call with your own
  `ActivitySource.StartActivity("execute_tool " + name)` +
  `gen_ai.tool.name` / `gen_ai.tool.call.id` tags to get real `execute_tool`
  spans. Without this wrapper, trajectory/tool-selection/args layers have
  nothing to score — this is the single most common reason a .NET target
  looks trace-less on first discovery. (Semantic Kernel auto-spans function
  executions under source name `"Microsoft.SemanticKernel"`; its sensitive
  data gate is the `ILogger` `Trace` level, not a flag — different knob, same
  concept.)
- **Trace-id echo:** middleware setting
  `Response.Headers["X-Trace-Id"] = Activity.Current?.TraceId.ToString()` in
  `OnStarting`. Do **not** use `HttpContext.TraceIdentifier` — it is an
  ASP.NET request id, not the OTel trace id, and using it silently breaks
  `correlation`. Incoming W3C `traceparent` is auto-parsed into
  `Activity.Current` since .NET 5, so multi-hop correlation needs no extra
  code.
- **Anthropic models from .NET:** no official SDK; the community
  `Anthropic.SDK` implements `IChatClient` and drops into the same
  `.UseOpenTelemetry()` pipeline unchanged.
- **adapter.yaml mapping:**
  ```yaml
  traces:
    source: otlp-file            # or your OTel collector's export target
    location: ${TRACE_URL_OR_PATH}
    convention: gen_ai            # native once execute_tool spans are wrapped in
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
    seed: "Fixtures/SeedCase.cs"        # invoked via a small CLI runner, or dotnet test category
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

state_location: .agent-eval/
access_level: white

invocation:
  mode: function
  entrypoint: "RefApp.Program"
  auth: { type: headers, headers: { X-Test-Persona: ${TEST_PERSONA} } }
  session:
    start: { via: "POST /chat/session" }
    send_turn: { via: "POST /chat/session/{id}/turn" }
    end: { via: "DELETE /chat/session/{id}" }
  streaming: sse
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
  seed: "Fixtures/SeedCase.cs"
  reset: "Fixtures/RespawnReset.cs"
  snapshot_state: "Fixtures/DumpState.cs"
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
