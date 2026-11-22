"""Server-sent event (SSE) answers, decoded into a JSON-shaped body.

Shared module, not a CLI (no argparse, like _common.py). run_cases.py calls
decode() when the adapter declares `invocation.response_format: sse` and the
response really is `text/event-stream`. The result replaces the raw stream
text as the response body, so every reader downstream -- the `<answer>`
path, response.json, the scorers -- sees one more JSON body and nothing
else changes.

Two protocols, declared in `invocation.sse.protocol`, never guessed:

- `ag-ui` -- the AG-UI event protocol (docs.ag-ui.com, @ag-ui/core 1.x):
  every frame is `data: {"type": ..., ...}`. TEXT_MESSAGE_CONTENT /
  TEXT_MESSAGE_CHUNK deltas join into the answer, TOOL_CALL_START / ARGS /
  END / RESULT (and TOOL_CALL_CHUNK) into tool calls with their arguments,
  RUN_FINISHED closes the run and may carry token usage, RUN_ERROR fails it.
  A server running an agent loop may emit one RUN_FINISHED per model call;
  usage is then summed over every RUN_FINISHED and RUN_ERROR, never the
  last one's alone (#46), and kept per event as `run_usage`. Summing across
  runs is the protocol's own accounting too: each run reports only its own
  calls (a subagent's run under parentRunId, a resumed run), never another
  run's, so distinct runs add without double-counting.
- `data-path` -- any other stream of JSON frames: the text delta is read at
  a declared dotted path in each frame (`choices.0.delta.content` for an
  OpenAI-style stream), and an optional `done` sentinel frame (`[DONE]`)
  marks the end. No tool calls: a generic stream has no agreed shape for
  them.

The answer is the text of a run that FINISHED. A stream that ends in
RUN_ERROR, or stops before its terminal event, has `text: null` -- the
runner scores a null answer `unscored`, never `fail`, so a dropped stream or
a provider error mid-answer is not reported as a wrong answer. What did
arrive is kept as `partial_text` for a person to read.
"""
import json

PROTOCOLS = ("ag-ui", "data-path")
SSE_KEYS = ("protocol", "text_path", "done")

_TEXT = ("TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_CHUNK")


def frames(text):
    """The `data` payload of each event in an SSE body, in order.

    The WHATWG event-stream rules, minus what an answer never needs (retry,
    id, event names): lines end in LF, CRLF or CR; a blank line ends an
    event; `data:` lines in one event join with LF; one leading space after
    the colon is dropped; a line starting with `:` is a comment. An event
    with no data line dispatches nothing, as in a browser.
    """
    out, data = [], None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line == "":
            if data is not None:
                out.append("\n".join(data))
            data = None
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data = (data or []) + [value]
    if data is not None:
        # A last event the server never terminated with a blank line. A
        # browser drops it; it is kept here because the run's verdict comes
        # from the terminal EVENT (RUN_FINISHED / done), not from framing.
        out.append("\n".join(data))
    return out


def check_config(sse):
    """Problems with an `invocation.sse` block, as strings; [] when sound."""
    if not isinstance(sse, dict):
        return ["invocation.sse must be a mapping with a `protocol`"]
    problems = []
    unknown = sorted(set(sse) - set(SSE_KEYS))
    if unknown:
        problems.append("unknown key(s) in invocation.sse: "
                        + ", ".join(unknown))
    protocol = sse.get("protocol")
    if protocol not in PROTOCOLS:
        problems.append("invocation.sse.protocol must be one of "
                        f"{'|'.join(PROTOCOLS)} (got: {protocol!r})")
    if protocol == "data-path":
        path = sse.get("text_path")
        if not (isinstance(path, str) and path.strip()):
            problems.append("invocation.sse.protocol: data-path needs "
                            "text_path, the dotted path of the text delta "
                            "in each frame")
        done = sse.get("done")
        if done is not None and not isinstance(done, str):
            problems.append("invocation.sse.done must be a string")
    elif protocol == "ag-ui":
        extra = [k for k in ("text_path", "done") if k in sse]
        if extra:
            problems.append("invocation.sse: {} belong(s) to protocol "
                            "data-path, not ag-ui".format(", ".join(extra)))
    return problems


def decode(text, sse):
    """The stream as a body: {text, tool_calls?, error, finished, ...}."""
    payloads = frames(text)
    if sse.get("protocol") == "ag-ui":
        return _ag_ui(payloads)
    return _data_path(payloads, sse["text_path"], sse.get("done"))


def _loads(payload):
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return None
    return value


def _at(node, path):
    for key in path.split("."):
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() \
                and int(key) < len(node):
            node = node[int(key)]
        else:
            return None
    return node


def _data_path(payloads, text_path, done):
    parts, bad, finished = [], 0, done is None
    for payload in payloads:
        if done is not None and payload.strip() == done:
            finished = True
            break
        frame = _loads(payload)
        if frame is None:
            bad += 1
            continue
        delta = _at(frame, text_path)
        if isinstance(delta, str):
            parts.append(delta)
    joined = "".join(parts)
    # No sentinel declared: EOF is the only end there is, so the stream is
    # finished when it closed -- the same rule a JSON body gets.
    return {"text": joined if finished and parts else None,
            "partial_text": None if finished else joined,
            "error": None, "finished": finished,
            "sse": {"protocol": "data-path", "frames": len(payloads),
                    "bad_frames": bad}}


def _ag_ui(payloads):
    messages, order = {}, []
    calls, call_order = {}, []
    error, finished, interrupted, run_usage = None, False, False, []
    types, bad = {}, 0
    last_message, last_call = None, None
    for payload in payloads:
        event = _loads(payload)
        if not isinstance(event, dict) or not isinstance(event.get("type"),
                                                         str):
            bad += 1
            continue
        kind = event["type"]
        types[kind] = types.get(kind, 0) + 1
        if kind == "TEXT_MESSAGE_START":
            last_message = event.get("messageId")
            if last_message not in messages:
                messages[last_message] = []
                order.append(last_message)
        elif kind in _TEXT:
            # A CHUNK may omit its messageId: it continues the last message.
            mid = event.get("messageId", last_message)
            if mid not in messages:
                messages[mid] = []
                order.append(mid)
            last_message = mid
            if isinstance(event.get("delta"), str):
                messages[mid].append(event["delta"])
        elif kind in ("TOOL_CALL_START", "TOOL_CALL_CHUNK"):
            cid = event.get("toolCallId", last_call)
            if cid not in calls:
                calls[cid] = {"id": cid, "name": None, "arguments": [],
                              "result": None}
                call_order.append(cid)
            last_call = cid
            if isinstance(event.get("toolCallName"), str):
                calls[cid]["name"] = event["toolCallName"]
            if kind == "TOOL_CALL_CHUNK" and isinstance(event.get("delta"),
                                                        str):
                calls[cid]["arguments"].append(event["delta"])
        elif kind == "TOOL_CALL_ARGS":
            call = calls.get(event.get("toolCallId"))
            if call is not None and isinstance(event.get("delta"), str):
                call["arguments"].append(event["delta"])
        elif kind == "TOOL_CALL_RESULT":
            call = calls.get(event.get("toolCallId"))
            content = event.get("content")
            if call is not None:
                call["result"] = content if isinstance(content, str) \
                    else json.dumps(content, ensure_ascii=False)
        elif kind == "RUN_FINISHED":
            finished = True
            outcome = event.get("outcome")
            interrupted = isinstance(outcome, dict) \
                and outcome.get("type") == "interrupt"
            _add_usage(run_usage, event)
        elif kind == "RUN_ERROR":
            error = {"message": event.get("message"),
                     "code": event.get("code")}
            _add_usage(run_usage, event)
    pieces = ["".join(messages[mid]) for mid in order]
    joined = "\n\n".join(piece for piece in pieces if piece)
    complete = finished and error is None
    tool_calls = []
    for cid in call_order:
        call = calls[cid]
        tool_calls.append({"id": call["id"], "name": call["name"],
                           "arguments": "".join(call["arguments"]),
                           "result": call["result"]})
    return {"text": joined if complete and pieces else None,
            "partial_text": None if complete else joined,
            "tool_calls": tool_calls, "error": error,
            "finished": finished, "interrupted": interrupted,
            "usage": _sum_usage(run_usage), "run_usage": run_usage,
            "sse": {"protocol": "ag-ui", "frames": len(payloads),
                    "bad_frames": bad, "types": types}}


# AG-UI TokenUsage field -> body key. In the protocol's accounting the two
# cache counts are PARTS of inputTokens, never additions to it, so they are
# carried beside input_tokens and never added into it.
_USAGE_FIELDS = (("inputTokens", "input_tokens"),
                 ("outputTokens", "output_tokens"))
_CACHE_FIELDS = (("cachedInputTokens", "cached_input_tokens"),
                 ("cacheWriteInputTokens", "cache_write_input_tokens"))


def _usage(entries):
    """AG-UI usage is a list (one entry per model); summed, or None.

    input_tokens and output_tokens are both present once either total is
    (as before); a cache count only when some entry reported it, so "not
    reported" stays distinct from zero. A cache count is a PART of
    inputTokens, so an event reporting one with no total is not a usage
    report: None, never a zero-token model call. Only a real int counts --
    not a bool, a float or a numeric string.
    """
    if not isinstance(entries, list):
        return None
    total = {"input_tokens": 0, "output_tokens": 0}
    seen = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for src, dst in _USAGE_FIELDS + _CACHE_FIELDS:
            value = entry.get(src)
            if isinstance(value, int) and not isinstance(value, bool):
                total[dst] = total.get(dst, 0) + value
                seen = seen or (src, dst) in _USAGE_FIELDS
    return total if seen else None


def _add_usage(run_usage, event):
    """Append a terminal event's usage, when it reported any, to the list."""
    usage = _usage(event.get("usage"))
    if usage is not None:
        run_usage.append(usage)


def _sum_usage(run_usage):
    """The response's usage: every terminal event's, summed; None if none."""
    if not run_usage:
        return None
    total = {}
    for usage in run_usage:
        for key, value in usage.items():
            total[key] = total.get(key, 0) + value
    return total


def tool_calls_to_otlp(calls, trace_id, usage=None, agent_name=None):
    """OTLP/JSON gen_ai spans for a response's tool calls.

    What normalize_trace.py reads from a real exporter: one invoke_agent root,
    one execute_tool child per call (gen_ai.tool.name, .call.id,
    .call.arguments, .call.result), and a chat span per model call carrying
    its token usage when the response reported any. `usage` is one dict (a
    JSON body's) or a list of them (a stream's `run_usage`, one per
    RUN_FINISHED / RUN_ERROR), so normalize_trace.py sees one llm_calls entry
    per call. Cache counts ride on the span as gen_ai.usage.cache_read /
    cache_creation .input_tokens, which normalize_trace.py keeps (#105).
    No timestamps: the response gives none per call, and an invented one
    would be a latency nobody measured.
    normalize_trace.py sorts stably, so the calls keep the response's order,
    and reports their durations as unknown.
    """
    root = f"{1:016x}"

    def attrs(values):
        out = []
        for key, value in values.items():
            if value is None:
                continue
            kind = "intValue" if isinstance(value, int) else "stringValue"
            out.append({"key": key, "value": {kind: value if kind ==
                                              "intValue" else str(value)}})
        return out

    spans = [{"traceId": trace_id, "spanId": root, "name": "invoke_agent",
              "attributes": attrs({"gen_ai.operation.name": "invoke_agent",
                                   "gen_ai.agent.name": agent_name})}]
    usages = usage if isinstance(usage, list) else [usage]
    for index, call_usage in enumerate(u for u in usages
                                       if isinstance(u, dict) and u):
        # Chat span ids sit far above the tool spans' (index + 16), so any
        # number of either stays unique.
        spans.append({
            "traceId": trace_id, "spanId": f"{(1 << 32) + index:016x}",
            "parentSpanId": root, "name": "chat",
            "attributes": attrs({
                "gen_ai.operation.name": "chat",
                "gen_ai.usage.input_tokens": call_usage.get("input_tokens"),
                "gen_ai.usage.output_tokens": call_usage.get("output_tokens"),
                "gen_ai.usage.cache_read.input_tokens":
                    call_usage.get("cached_input_tokens"),
                "gen_ai.usage.cache_creation.input_tokens":
                    call_usage.get("cache_write_input_tokens")})})
    for index, call in enumerate(calls):
        arguments = call.get("arguments")
        if arguments is not None and not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        spans.append({
            "traceId": trace_id, "spanId": f"{index + 16:016x}",
            "parentSpanId": root,
            "name": "execute_tool {}".format(call.get("name")),
            "attributes": attrs({
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": call.get("name"),
                "gen_ai.tool.call.id": call.get("id"),
                "gen_ai.tool.call.arguments": arguments,
                "gen_ai.tool.call.result": call.get("result")})})
    return {"resourceSpans": [{"resource": {"attributes": []},
                               "scopeSpans": [{"scope": {"name": "evalup.response"},
                                               "spans": spans}]}]}
