# Reading traces from Jaeger — what works today, and a design note

**Status:** design note, not a feature. Written 2026-09-26 after the first
field test on a real app (RefApp, ASP.NET Core, OTLP → Jaeger / Aspire)
found that the runner reads `traces.source: otlp-file` only and that the docs
sent a Jaeger user to a `discover` step that does not install anything
(findings F-007, F-009, F-040). The runner is feature-frozen (CLAUDE.md), so
this records the shape of the work and its cost; it does not authorize it.

## 1. What a Jaeger user can do today — no plugin change

The runner reads one growing file of OTLP/JSON spans (`traces.location`),
copies it into each case's `trace.json` once the case's trace id appears in
it, and `normalize_trace.py` filters by trace id. Any app
that already exports OTLP can feed that file by putting an OpenTelemetry
Collector in front of the backend it has and fanning out:

```yaml
# otel-collector.yaml — one receiver, two exporters
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4327 }   # the app's OTEL_EXPORTER_OTLP_ENDPOINT
      http: { endpoint: 0.0.0.0:4328 }
exporters:
  file:                                  # contrib `fileexporter`
    path: /var/tmp/evalup-spans.jsonl
    format: json                         # one OTLP ExportTraceServiceRequest per line;
                                         # normalize_trace.py reads that JSON Lines shape
    rotation: { max_megabytes: 100, max_backups: 3 }
  otlp/jaeger:                           # keep the dashboard you have
    endpoint: localhost:4317             # Jaeger accepts OTLP natively since 1.35
    tls: { insecure: true }
service:
  pipelines:
    traces: { receivers: [otlp], exporters: [file, otlp/jaeger] }
```

Then, in the adapter: `traces.source: otlp-file`, `traces.location: <that
path>`, a **declared** correlation (§3), and an `invocation.health_check` —
pre-flight verifies the trace join on that call, and a queryable source with
an explicit correlation but no health check exits 3 asking for one (runner
contract §4, item 4 "Trace branch"). Sources, checked 2026-09-26:
fileexporter keys (`path`, `format`, `rotation`) from the
opentelemetry-collector-contrib README; Jaeger's native OTLP on 4317/4318
from the Jaeger 1.35 announcement. `discover` records these declarations in
`adapter.yaml`; the user places the collector — that is why the README and
`help` no longer say discover "sets up the file exporter".

A port note from the field test: the app was already pointed at `:4317`,
which on that host was the Aspire dashboard, not Jaeger. Whoever sets this up
checks what listens on the OTLP port before assuming spans reached Jaeger.

## 2. A Jaeger client in the runner — the design

If the runner ever queries Jaeger directly (`traces.source: jaeger`), the
shape is:

- **API.** Jaeger's `GET /api/traces?service=&start=&end=&limit=&tags=`
  (port 16686, microsecond epochs, `tags` a JSON map) is the one the field
  test used by hand. Jaeger's own docs call it internal: "This JSON API is
  intentionally undocumented and subject to change" (jaegertracing.io,
  docs/1.76/architecture/apis). The supported interface is `api_v3` — gRPC on
  16685 and, via grpc-gateway on 16686,
  `GET /api/v3/traces?query.service_name=…&query.start_time_min=<RFC3339>&
  query.start_time_max=<RFC3339>` — whose responses we expect to be
  OTLP-shaped `resourceSpans`, the format `normalize_trace.py` already
  parses; that shape is to be confirmed on a live api_v3 call. Whether
  Jaeger v2 still serves the v1 path is unconfirmed; target v3, keep v1 as a
  documented fallback for `all-in-one` v1 hosts.
- **Selection.** Never "the most recent trace" (adapter hard rule 1). The
  runner fetches by service name and the case's time window, then keeps only
  the trace whose id matches the case's correlation value. A tag join (a
  hypothetical `traces.select_tag: <attribute>`) is valid only on a value the
  runner generated, sent with the call and recorded in `request.json`, and
  that the app copied onto the span — a tag the app mints server-side proves
  nothing the runner sent, and joining on it is the time-window guess rule 1
  forbids. Zero or more than one matching trace ⇒ `infra_incomplete`, never a
  pick. `select_tag` would be a new correlation mode, so it needs the same
  hard-rule-1 amendment as §3's proposal. Nothing else joins.
- **Where it lands.** `run_cases.py` pre-flight (`trace_branch`): a v3 probe
  for the health-check trace replaces the file-presence check;
  `collect_trace` fetches by id instead of copying the file; the quiescence
  wait becomes "re-fetch until the span count is stable". Runner contract §4
  item 4 ("Trace branch") and adapter-contract `traces:` gain `jaeger` with
  `location: <query base URL>`.
- **Effort.** stdlib `urllib` client with RFC3339 formatting and one retry:
  ~150 lines in the runner; a stub Jaeger HTTP server for tests plus v1 and
  v3 fixture payloads: ~250 lines; contract and adapter-contract edits; one
  live proof against `jaeger-all-in-one`. About two focused days, one of
  them the live proof, since v3's attribute-filter parameters are still
  being clarified upstream (jaegertracing/jaeger#3108).

## 3. Correlation — the three options, and the one that needs no patch

The runner joins a response to a trace only through a declared field
(`traceparent-echo` or `response-field:<name>`). ASP.NET Core does **not**
echo `traceparent` by default, and RefApp's `refapp.turn.id` tag is
minted server-side, so the field test had to declare `correlation: none`.

| Option | App change | Runner change | Notes |
|---|---|---|---|
| `traceparent-echo` | ~5-line middleware copying `Activity.Current.Id` to a response header | none | already supported; the patch `discover` §5 offers |
| `response-field:<name>` | return the trace id in the body | none | already supported |
| **`request-header:traceparent`** (proposed) | **none** | mint a W3C `traceparent` per call (`00-<32 hex>-<16 hex>-01`), send it, correlate on the trace id the runner chose | ASP.NET Core adopts an inbound `traceparent` as the request's trace id (W3C is the default `ActivityIdFormat` since .NET 5), so every server span joins on it. Still explicit, still per-call, never a time-window guess — hard rule 1 holds. |

The third row is the one that unlocks trajectory scoring for every ASP.NET
Core app without touching its code, and it would work against `otlp-file`
as well as against a future Jaeger client — once implemented; today the
runner does not know the value. It is a runner change: ~40 lines
(`read_trace_id` learns the new value; the HTTP client sets the header;
`request.json` records it), a contract amendment to hard rule 1 naming it as
explicit correlation, and a test with a stub app that copies the inbound
trace id onto a span. Half a day. Sources: W3C Trace Context (header format);
learn.microsoft.com, "Default ActivityIdFormat is W3C" (.NET 5 breaking
change) and "Distributed tracing concepts" (inbound header adoption).

## 4. What this does not change

Trace-less scoring stays honest as it is: routing and answer checks score,
the trace-dependent layers report `unscorable` with the `blocked_by` reason
copied from the capability matrix, and
`score_cost.py` reports a trace-less run as `status: "unpriced"` with the reason. None of the above makes a
number mean something different; it only makes more runs have the number.
