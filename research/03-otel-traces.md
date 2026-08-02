# Research: OpenTelemetry Traces as Eval/Debug Data Source (2025–2026)

## 1. GenAI semantic conventions — state
- Status: still "Development" (not stable). **July 2026: all gen_ai.* conventions moved to a new repo `open-telemetry/semantic-conventions-genai`** — old opentelemetry.io semconv URLs are stale redirects.
- Breaking renames in the last year: `gen_ai.system` → `gen_ai.provider.name` (v1.37); prompt/completion capture went through 3 generations:
  1. deprecated span attrs `gen_ai.prompt`/`gen_ai.completion` (2024)
  2. log-based events (`gen_ai.user.message`, ...)
  3. **current (v1.41, Apr 2026): span attrs `gen_ai.system_instructions`, `gen_ai.input.messages`, `gen_ai.output.messages`**

## 2. Key attributes for eval extraction
- LLM call span: `gen_ai.operation.name` (chat, execute_tool, invoke_agent, create_agent, ...), `gen_ai.request.model`, `gen_ai.usage.input_tokens`/`output_tokens` (+ cache/reasoning token variants), `gen_ai.response.finish_reasons`, `gen_ai.conversation.id` (multi-turn correlation).
- Agent span: `invoke_agent` with `gen_ai.agent.name`/`id`/`description` → per-stage (router vs domain agent) cost/latency = GROUP BY gen_ai.agent.name.
- Tool span: `execute_tool` with `gen_ai.tool.name` (required), `gen_ai.tool.call.id`, opt-in `gen_ai.tool.call.arguments`/`result`. Tool decision appears as tool_call content part in output.messages; execution is a separate span; result comes back as tool_call_response part, linked by call_id.
- Metrics: gen_ai.client.token.usage, operation.duration, invoke_agent.duration/inference_calls/tool_calls, execute_tool.duration.

## 3. CRITICAL GOTCHA: content capture is OPT-IN
Prompts, completions, and tool arguments are NOT captured by default (PII/cost). E.g. Python: `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`. Without it traces have structure/timing/tokens but NO content to mine. **First thing to check in any target app.**

## 4. Instrumentation libraries emit DIFFERENT conventions
| Library | Convention | Aligned to spec? |
|---|---|---|
| OpenLLMetry (Traceloop) | legacy llm.*/traceloop.* + deprecated gen_ai.prompt | Lagging |
| OpenInference (Arize) | own namespace (openinference.span.kind, llm.token_count.*) | Not aligned |
| OpenLIT | gen_ai.* primary | Closest |
| LangSmith/Langfuse ingestion | accept multiple namespaces | Backend-side mapping |

→ Harness needs a **normalization layer**: whatever the target emits → one internal trajectory format. Trace convention is part of per-app adapter config.

## 5. Trace→eval pipelines (established pattern everywhere)
- Langfuse: datasets-from-traces first-class (DatasetItem.sourceTraceId), hosted online LLM-judge evaluators with % sampling + filters.
- Braintrust: logs and datasets share one schema; online scoring rules with sampling_rate; BTQL SQL-like query → JSON/Parquet.
- Phoenix (OSS): manual get_spans_dataframe → evaluate → log annotations; continuous online evals are paid Arize AX only.
- Converging names: "eval flywheel", "Evaluation Driven Development".

## 6. Trajectory analysis
- Reconstruct: group spans by trace_id, tree via parent_span_id, sort children by start_time, DFS. (Phoenix createSpanTree is a reference impl; note OpenInference puts TOOL spans as *siblings* of the LLM span under the AGENT span.)
- **Loop/redundant-call detection is a gap no vendor fills**: group execute_tool spans by (tool.name, hash(arguments)) within a trace, flag counts over threshold. Langfuse Agent Graphs visualizes loops; LangGraph has recursion_limit=25 guardrail; Arize names "infinite retry" a canonical failure mode.
- Latency: inclusive vs exclusive (self-time) per span; TTFT via gen_ai.response.time_to_first_chunk.

## 7. Querying raw traces programmatically
- OTel Collector file exporter: JSONL, one TracesData per line — cleanest for jq/pandas.
- Jaeger: api_v3 (OTLP-shaped JSON, port 16686); legacy /api/traces for bulk.
- Tempo: TraceQL, e.g. `{ span.gen_ai.operation.name = "execute_tool" && span.gen_ai.tool.name = "search_docs" }`.
- ClickHouse exporter: `SpanAttributes['gen_ai.tool.name']` map lookups, self-join on TraceId/ParentSpanId for trees.
- Zero-infra: otlp2parquet + DuckDB.

```sql
SELECT SpanAttributes['gen_ai.tool.name'] AS tool, Duration
FROM otel_traces
WHERE SpanAttributes['gen_ai.operation.name'] = 'execute_tool'
  AND Timestamp >= now() - INTERVAL 7 DAY;
```

Sources: github.com/open-telemetry/semantic-conventions-genai (gen-ai-spans.md, gen-ai-agent-spans.md, gen-ai-metrics.md), langfuse.com/docs/evaluation, braintrust.dev/docs/reference/sql, arize.com/docs/phoenix, jaegertracing.io/docs, grafana.com/docs/tempo/latest/traceql, clickhouse.com/docs/observability/integrating-opentelemetry
