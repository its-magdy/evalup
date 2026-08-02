#!/usr/bin/env python3
"""OTel trace -> agent-eval trajectory normalizer.

Supports the current OTel GenAI semantic convention (gen_ai.*) from OTLP JSON
(file exporter format or a raw span list). Other conventions must be mapped by
a per-adapter shim (adapter.yaml `traces.mapping_shim`) before this script.

Conservation checks (critique #11/#7) test TRANSPORT integrity only: no spans
for the trace, or orphaned parent references -> status "incomplete" so the
runner scores the case INFRA_INCOMPLETE instead of manufacturing failures.
Missing result CONTENT (content capture off) is reported in checks and gates
only content-dependent scoring (args from_tool_result, faithfulness) — it does
not mark an otherwise intact trace incomplete.

Attribute values follow OTLP AnyValue: scalars, arrays, and kv-lists are all
converted to native Python — a structured tool result is data, not None. A
span missing either timestamp gets duration_ms null (fabricating an end time
of 0 would poison every latency aggregate) and is listed in
checks.spans_missing_duration. Token usage is recorded per LLM call and
rolled up to the nearest ancestor invoke_agent span, so per-stage cost is
derivable from the output.

Usage: normalize_trace.py <spans.json> --trace-id <id>
Output: {"status": "ok"|"incomplete", "trajectory": {...}, "checks": {...}}
"""
import argparse
import json
import sys

from _common import add_version_flag


def any_value(v):
    """OTLP AnyValue -> native Python. 0 and false are real values; an
    unrecognized or empty AnyValue -> None."""
    for t in ("stringValue", "intValue", "doubleValue", "boolValue",
              "bytesValue"):
        if t in v:
            return v[t]
    if "arrayValue" in v:
        return [any_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {kv.get("key"): any_value(kv.get("value", {}))
                for kv in v["kvlistValue"].get("values", [])}
    return None


def attr_dict(span):
    return {kv.get("key"): any_value(kv.get("value", {}))
            for kv in span.get("attributes", [])}


def nano(value):
    """OTLP encodes int64 timestamps as JSON strings. Anything unparseable is
    damaged transport, not a crash: return None so the span lands in
    checks.spans_missing_duration like any other span of unknown duration."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def start_key(span):
    """Sort key: spans with an unusable start time sort first rather than
    raising — ordering is best-effort, integrity is reported in checks."""
    value = nano(span.get("startTimeUnixNano"))
    return value if value is not None else 0


def duration_ms(span):
    """None when either timestamp is missing, unparseable, or the pair is
    inconsistent — the span never completed (or was truncated) and its
    duration is unknown."""
    start, end = (nano(span.get("startTimeUnixNano")),
                  nano(span.get("endTimeUnixNano")))
    if start is None or end is None:
        return None
    dur = (end - start) / 1e6
    return round(dur, 1) if dur >= 0 else None


def flatten_otlp(doc):
    """Accept OTLP TracesData, a list of TracesData (JSONL pre-parsed), or a
    raw span list. Every shape probe guards on dict-ness first: the input may
    be any JSON a mapping_shim emitted, and a scalar where an object belongs
    is a data error to report, not an AttributeError to raise."""
    if isinstance(doc, list) and doc and isinstance(doc[0], dict) \
            and "resourceSpans" not in doc[0]:
        return [s for s in doc if isinstance(s, dict)]
    docs = doc if isinstance(doc, list) else [doc]
    spans = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        for rs in d.get("resourceSpans", []):
            if not isinstance(rs, dict):
                continue
            for ss in rs.get("scopeSpans", []):
                if not isinstance(ss, dict):
                    continue
                spans.extend(s for s in ss.get("spans", [])
                             if isinstance(s, dict))
    return spans


def has_parent(span):
    """True when the span declares a real parent.

    OTLP/JSON spells "no parent" as an omitted or empty parentSpanId, and
    spec-compliant exporters do exactly that. Non-conformant SDKs and
    adapter mapping_shims (whose output this script also accepts) instead
    emit an all-zero id, in hex or in the base64 the Protobuf JSON mapping
    would use. Treating those as a dangling parent marks an intact trace
    "incomplete", which scores every case INFRA_INCOMPLETE and misattributes
    the failure to the app's tracing."""
    parent = span.get("parentSpanId")
    if not isinstance(parent, str) or not parent.strip():
        return False
    chars = set(parent.strip())
    # Character-set tests, not a strip(): stripping "0aA=" would also erase a
    # legitimate hex id like a0a0a0a0a0a0a0a0.
    if chars <= {"0"}:                # all-zero hex
        return False
    if chars <= {"A", "="}:           # all-zero bytes, base64
        return False
    return True


def int_or_zero(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def main():
    ap = argparse.ArgumentParser(
        description="Normalize OTel gen_ai.* spans (OTLP JSON) into an "
                    "agent-eval trajectory, with transport-integrity checks. "
                    "Other conventions need an adapter mapping_shim first.")
    add_version_flag(ap)
    ap.add_argument("spans_file",
                    help="OTLP TracesData JSON, a list of them, or a raw "
                         "span list")
    ap.add_argument("--trace-id", required=True,
                    help="the trace to extract; spans from other traces are "
                         "ignored")
    a = ap.parse_args()

    try:
        with open(a.spans_file) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(2)

    def bad_input(detail):
        print(json.dumps({"status": "error", "error": (
            f"{a.spans_file}: expected OTLP TracesData, a list of them, or a "
            f"span list; {detail}")}))
        sys.exit(2)

    # A shape this script cannot read is a DATA error, reported loudly. It is
    # deliberately not degraded to an empty span list: "zero spans" already
    # means "incomplete trace", so silently reusing it here would blame the
    # app's tracing for what is actually an unreadable file.
    if not isinstance(raw, (dict, list)):
        bad_input(f"got {type(raw).__name__}")
    if isinstance(raw, list) and raw and not any(isinstance(d, dict)
                                                 for d in raw):
        bad_input("got a list containing no JSON objects")

    all_spans = flatten_otlp(raw)
    spans = [s for s in all_spans if s.get("traceId") == a.trace_id]

    span_ids = {s.get("spanId") for s in spans}
    orphans = [s.get("spanId") for s in spans
               if has_parent(s) and s["parentSpanId"] not in span_ids]

    spans.sort(key=start_key)
    by_id = {s.get("spanId"): s for s in spans}

    # First pass: agent stages, so LLM-call tokens can roll up to them.
    agents, agent_entry_by_id = [], {}
    for s in spans:
        at = attr_dict(s)
        if at.get("gen_ai.operation.name") == "invoke_agent":
            entry = {"name": at.get("gen_ai.agent.name"),
                     "duration_ms": duration_ms(s),
                     "usage": {"input_tokens": 0, "output_tokens": 0}}
            agents.append(entry)
            agent_entry_by_id[s.get("spanId")] = entry

    llm_calls, tool_calls = [], []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for s in spans:
        at = attr_dict(s)
        op = at.get("gen_ai.operation.name")
        if op == "execute_tool":
            args_raw = at.get("gen_ai.tool.call.arguments")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except json.JSONDecodeError:
                args = {"_unparsed": True}
            tool_calls.append({
                "name": at.get("gen_ai.tool.name"),
                "call_id": at.get("gen_ai.tool.call.id"),
                "args": args,
                "result": at.get("gen_ai.tool.call.result"),
                "duration_ms": duration_ms(s),
            })
        elif op in ("chat", "text_completion", "generate_content"):
            call_usage = {
                "input_tokens": int_or_zero(at.get("gen_ai.usage.input_tokens")),
                "output_tokens": int_or_zero(at.get("gen_ai.usage.output_tokens")),
            }
            llm_calls.append({
                "model": at.get("gen_ai.request.model"),
                "duration_ms": duration_ms(s),
                "usage": call_usage,
            })
            for k in usage:
                usage[k] += call_usage[k]
            # Attribute to the nearest ancestor agent stage.
            parent, hops = s.get("parentSpanId"), set()
            while parent and parent in by_id and parent not in hops:
                hops.add(parent)
                if parent in agent_entry_by_id:
                    for k in usage:
                        agent_entry_by_id[parent]["usage"][k] += call_usage[k]
                    break
                parent = by_id[parent].get("parentSpanId")

    missing_results = [t["call_id"] for t in tool_calls
                       if t["result"] is None and t["call_id"]]
    checks = {
        "spans_for_trace": len(spans),
        "orphaned_parents": orphans,
        # Informational: gates content-dependent scoring only (args
        # from_tool_result, faithfulness), never completeness. The two
        # capture flags are separate on purpose — an exporter can record
        # arguments while dropping results, and from_tool_result provenance
        # depends on the RESULT side.
        "tool_calls_without_result": missing_results,
        "arg_capture_seen": any(t["args"] not in (None, {})
                                for t in tool_calls),
        "result_capture_seen": any(t["result"] is not None
                                   for t in tool_calls),
        "spans_missing_duration": [s.get("spanId") for s in spans
                                   if duration_ms(s) is None],
    }
    incomplete = (not spans) or bool(orphans)

    print(json.dumps({
        "status": "incomplete" if incomplete else "ok",
        "trajectory": {
            "trace_id": a.trace_id,
            "agents": agents,
            "llm_calls": llm_calls,
            "tool_calls": tool_calls,
            "usage": usage,
        },
        "checks": checks,
    }, indent=2))


if __name__ == "__main__":
    main()
