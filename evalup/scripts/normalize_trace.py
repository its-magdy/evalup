#!/usr/bin/env python3
"""OTel trace -> evalup trajectory normalizer.

Supports the current OTel GenAI semantic convention (gen_ai.*) from OTLP JSON
(file exporter format or a raw span list). Other conventions must be mapped by
a per-adapter shim (adapter.yaml `traces.mapping_shim`) before this script.

Conservation checks (critique #11/#7) test TRANSPORT integrity only: no spans
for the trace, or orphaned parent references -> status "incomplete" so the
runner scores the case INFRA_INCOMPLETE instead of manufacturing failures.
Missing result CONTENT (content capture off) is reported in checks and gates
only content-dependent scoring (args from_tool_result, faithfulness) — it does
not mark an otherwise intact trace incomplete. Zero matching spans is reported
alongside the file's total span count, so a mistyped --trace-id is
distinguishable from a trace the app never emitted.

A tool call that FAILED is a third thing, distinct from both: the convention
records gen_ai.tool.call.result only "if execution was successful", so a
failure otherwise looks exactly like content capture being off. error.type
(stable, conditionally required) and the OTLP span status are read onto each
call as "error" and summarized in checks.tool_calls_errored, so the scoring
layers can tell "the tool was called" from "the tool worked".

Attribute values follow OTLP AnyValue: scalars, arrays, and kv-lists are all
converted to native Python — a structured tool result is data, not None. A
span missing either timestamp gets duration_ms null (fabricating an end time
of 0 would poison every latency aggregate) and is listed in
checks.spans_missing_duration. Token usage is recorded per LLM call and
rolled up to the nearest ancestor invoke_agent span, so per-stage cost is
derivable from the output.

WHAT IS NOT ATTRIBUTED, since the rollup above makes it look like it might be:
tool_calls carry name/call_id/args/result/error/duration and NO owning agent,
no span_id and no parent, and llm_calls carry no prompt or response text. The
parent chain is walked for the token rollup and then discarded. So per-agent
COST ships and per-agent BEHAVIOUR does not: no scorer can say which sub-agent
made a call, which is why agent-scoped expectations are reserved
(case-format.md) and why per-handoff capture is not a thing this harness has.
Adding the owner field alone would only move the gap one layer along -- nothing
downstream reads it -- so it waits for the scorer that would.

Usage: normalize_trace.py <spans.json> --trace-id <id>
Output: {"status": "ok"|"incomplete", "trajectory": {...}, "checks": {...}}
"""
import argparse
import json
import sys

from _common import BadJSON, add_version_flag, load_text, loads_strict


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


def span_error(span, at):
    """The error a span ended with, or None.

    Reads `error.type` — STABLE and Conditionally Required in the OTel
    semantic conventions "if the operation ended in an error" — and falls back
    to the OTLP span status (code 2 = STATUS_CODE_ERROR, spelled either as the
    enum name or its integer), which every exporter sets whether or not it
    populates the GenAI attribute.

    Without this, a tool call that returned HTTP 500 was indistinguishable from
    a successful one: the spec records `gen_ai.tool.call.result` only "if
    execution was successful", so the failure showed up solely as a MISSING
    result — the same signal the opt-in content-capture flags use. The failure
    was then laundered into "content capture is off", which pointed every
    downstream diagnosis at exporter config instead of the broken tool, and a
    trajectory whose only tool call errored still scored a clean pass."""
    error_type = at.get("error.type")
    if isinstance(error_type, str) and error_type.strip():
        return error_type
    status = span.get("status")
    if isinstance(status, dict):
        code = status.get("code")
        if code in (2, "2", "STATUS_CODE_ERROR"):
            message = status.get("message")
            return message if isinstance(message, str) and message.strip() \
                else "STATUS_CODE_ERROR"
    return None


def int_or_zero(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def load_spans_file(path, fail):
    """One JSON document, or JSON Lines -- one document per line.

    The OpenTelemetry Collector's file exporter (`format: json`), the one
    producer the README and adapter-contract name for `traces.source:
    otlp-file`, writes one ExportTraceServiceRequest PER LINE, and this
    reader took a single document only: every such store failed with "Extra
    data: line 2" (found writing docs/traces-jaeger.md, 2026-09-26). The
    docstring of flatten_otlp always named "a list of TracesData (JSONL
    pre-parsed)" as an accepted shape; this is the parse it assumed. Each
    line goes through the same strict reader as a whole file (CONTRIBUTING:
    JSON in is strict), so a NaN on line 3 is still a data error."""
    text = load_text(path, on_error=fail)
    try:
        return loads_strict(text)
    except (BadJSON, ValueError, RecursionError) as whole:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            fail(f"bad input: {path}: {whole}")
        docs = []
        for n, line in enumerate(lines, 1):
            try:
                docs.append(loads_strict(line))
            except (BadJSON, ValueError, RecursionError) as e:
                fail(f"bad input: {path}: not one JSON document ({whole}) "
                     f"and not JSON Lines: line {n}: {e}")
        return docs


def main():
    ap = argparse.ArgumentParser(
        description="Normalize OTel gen_ai.* spans (OTLP JSON) into an "
                    "evalup trajectory, with transport-integrity checks. "
                    "Other conventions need an adapter mapping_shim first.")
    add_version_flag(ap)
    ap.add_argument("spans_file",
                    help="OTLP TracesData JSON, a list of them, a raw span "
                         "list, or JSON Lines (one TracesData per line, as "
                         "the collector's file exporter writes)")
    ap.add_argument("--trace-id", required=True,
                    help="the trace to extract; spans from other traces are "
                         "ignored")
    a = ap.parse_args()

    def fail(message):
        """This script's error shape. Every one of its outputs carries a
        status ("ok"/"incomplete"/"error"), so a consumer switching on that
        field sees a coherent tri-state — the documented exception to the
        plain {"error": ...} payload in _common.die."""
        print(json.dumps({"status": "error", "error": message}))
        sys.exit(2)

    # Through the shared reader, with this script's error shape: the encoding
    # contract _common.load_text spells out at length is asserted in ONE place
    # rather than re-derived here, which is how this script came to be the one
    # bare open() left in the package (PEP 597 / Ruff PLW1514). Its quiet
    # failure mode is the dangerous one — under latin-1/cp1252 a non-ASCII tool
    # name decodes to mojibake without raising, so every downstream scorer
    # compares against garbage.
    raw = load_spans_file(a.spans_file, fail)

    def bad_input(detail):
        fail(f"{a.spans_file}: expected OTLP TracesData, a list of them, or a "
             f"span list; {detail}")

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
                "error": span_error(s, at),
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

    # An errored call is NOT an uncaptured one. The spec records a tool result
    # only on success, so lumping the two together reported a broken tool as
    # "content capture off" — a true statement about the wrong subsystem, and
    # the reason a failing tool could look like a tracing misconfiguration.
    errored = [{"call_id": t["call_id"], "tool": t["name"],
                "error": t["error"]}
               for t in tool_calls if t["error"] is not None]
    # Identified positionally when the exporter set no gen_ai.tool.call.id.
    # Filtering on a truthy call_id dropped exactly the traces where NO call
    # carries an id, so the list came back empty and the run read
    # "every tool result was captured" — the most confident possible report of
    # the least instrumented possible trace, on the check whose job is to say
    # that content capture is off.
    missing_results = [t["call_id"] or f"index:{i}"
                       for i, t in enumerate(tool_calls)
                       if t["result"] is None and t["error"] is None]
    checks = {
        "spans_for_trace": len(spans),
        # Beside it, so the two ways to get zero stay distinguishable: a trace
        # that produced nothing, and a --trace-id that matched nothing in a
        # file full of spans (a typo, or the wrong run). Both used to report
        # exactly `spans_for_trace: 0` and score every case INFRA_INCOMPLETE,
        # which reads as "the app's tracing is broken" for what is a mistyped
        # argument.
        "spans_in_file": len(all_spans),
        "orphaned_parents": orphans,
        # Tool calls the app itself reported as failed (error.type / span
        # status). Informational here — whether a failed call fails the CASE is
        # the scoring layers' decision, not the normalizer's — but it must
        # never again be silently indistinguishable from success.
        "tool_calls_errored": errored,
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
    if not spans and all_spans:
        # Spell the likely cause out rather than leaving the reader to compare
        # two counts. This is the difference between "the run under test never
        # emitted a trace" (a real infra finding) and "you passed the wrong
        # id" (a typo), and only one of them is worth investigating.
        checks["trace_id_note"] = (
            f"no span carries trace_id {a.trace_id!r}, but the file holds "
            f"{len(all_spans)} span(s) across "
            f"{len({s.get('traceId') for s in all_spans})} other trace id(s) "
            "— check --trace-id (a mistyped or wrong-run id looks exactly "
            "like an empty trace here)")
    # An execute_tool span with no gen_ai.tool.name is a tool call the scorers
    # cannot name, and a nameless call matches no forbidden-tool entry: left
    # "ok" it scored authz and trajectory as PASS over the call itself
    # (2026-09-21 audit). The trace cannot support the tool layers, which is
    # what "incomplete" means -- infra_incomplete for them, never a verdict.
    unnamed = [i for i, call in enumerate(tool_calls)
               if not isinstance(call["name"], str) or not call["name"].strip()]
    if unnamed:
        checks["unnamed_tool_spans"] = len(unnamed)
        checks["unnamed_tool_note"] = (
            f"{len(unnamed)} execute_tool span(s) carry no gen_ai.tool.name, "
            "so the tool they called cannot be checked against any expected "
            "or forbidden tool -- the instrumentation (or the adapter's "
            "mapping_shim) records the tool name under another attribute")
    incomplete = (not spans) or bool(orphans) or bool(unnamed)

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
