"""Client-sent history: the messages a real client would hold.

Shared module, not a CLI (no argparse, like _sse.py). run_cases.py uses it
when the adapter declares `invocation.conversation.history`
(docs/client-history.md): an app whose CLIENT sends the whole conversation in
every request. The runner keeps, per conversation attempt, the list a client
would hold -- each user turn it sent, followed by the app's own reply to it,
rebuilt from that reply's response -- and renders it into the request body at
`<history>`.

Nobody writes an assistant line: an assistant entry is the text the runner
scored for that turn, byte for byte ("what is carried is what was scored").
A reply that cannot be rebuilt is never patched up; reply_entries() returns
the reason instead, and the runner ends the conversation `unscored` (SS3.4).

Each entry is rendered once, when it is built, in ONE pass over its
template: text put in (a user turn, an app reply) is never scanned for
placeholders, so a reply containing `<uuid>` goes out as written.
"""
import json
import re
import uuid

HISTORY_MARKER = "<history>"
REQUEST_ID_MARKER = "<request id>"

KEYS = ("preset", "tools", "user", "assistant", "tool_calls_at",
        "tool_call", "tool_result")
TEMPLATE_KEYS = ("user", "assistant", "tool_call", "tool_result")
TOOL_KEYS = ("tool_calls_at", "tool_call", "tool_result")
TOOLS_MODES = ("text-only", "carried")
TEXT_ONLY, CARRIED = TOOLS_MODES

ENTRY_MARKERS = ("<text>", "<message id>", "<call id>", "<tool name>",
                 "<arguments>", "<result>")
_ENTRY_RE = re.compile("|".join(re.escape(m) for m in ENTRY_MARKERS))
_ENV_REF_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")

_TOOL_CALL = ('{"id": "<call id>", "type": "function", "function": '
              '{"name": "<tool name>", "arguments": "<arguments>"}}')
# SS2's table. The manifest records the expanded form, so a run says what
# shape it sent whichever way the adapter spelled it.
PRESETS = {
    "ag-ui": {
        "user": '{"id": "<message id>", "role": "user", "content": "<text>"}',
        "assistant": '{"id": "<message id>", "role": "assistant", '
                     '"content": "<text>"}',
        "tool_calls_at": "toolCalls",
        "tool_call": _TOOL_CALL,
        "tool_result": '{"id": "<message id>", "role": "tool", '
                       '"toolCallId": "<call id>", "content": "<result>"}',
    },
    "openai": {
        "user": '{"role": "user", "content": "<text>"}',
        "assistant": '{"role": "assistant", "content": "<text>"}',
        "tool_calls_at": "tool_calls",
        "tool_call": _TOOL_CALL,
        "tool_result": '{"role": "tool", "tool_call_id": "<call id>", '
                       '"content": "<result>"}',
    },
}


def _strings(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


def _mentions(node, marker):
    return any(marker in s for s in _strings(node))


def marker_problems(body_template, declared):
    """`<history>` in the request body: exactly once, as a string value's
    whole text, and only beside a declared `history` (SS2)."""
    strings = list(_strings(body_template))
    whole = sum(1 for s in strings if s == HISTORY_MARKER)
    inside = sum(1 for s in strings if HISTORY_MARKER in s and s != HISTORY_MARKER)
    where = "adapter.invocation.request_body"
    if not declared:
        if whole or inside:
            return [f"{where} carries {HISTORY_MARKER} but the adapter "
                    "declares no invocation.conversation.history, so there "
                    "is no history to put there"]
        return []
    problems = []
    if inside:
        problems.append(f"{where}: {HISTORY_MARKER} must be a string value's "
                        "whole text (\"messages\": \"<history>\"), never "
                        "inside a longer string: the array is a typed value")
    if whole > 1:
        problems.append(f"{where} carries {HISTORY_MARKER} {whole} times; "
                        "the array goes in exactly one place")
    if not whole and not inside:
        problems.append(f"{where} has no {HISTORY_MARKER} placeholder, so "
                        "the history conversation.history declares would "
                        "never be sent")
    return problems


def _template(block, key, where, problems):
    raw = block.get(key)
    if not isinstance(raw, str) or not raw.strip():
        problems.append(f"{where}.{key} must be a JSON object template, as a "
                        "string")
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        problems.append(f"{where}.{key} is not valid JSON: {exc}")
        return None
    if not isinstance(value, dict):
        problems.append(f"{where}.{key} must be a JSON object, got "
                        f"{type(value).__name__}")
        return None
    return value


def check_config(block, raw_block, sse, json_tool_source):
    """(config, problems) for `invocation.conversation.history`.

    `raw_block` is the block BEFORE env resolution: a `${REF}` in a template
    would put a resolved value into every request.json, and the history
    holds case and app text only. `sse` is the adapter's invocation.sse (or
    None for a JSON body); `json_tool_source` is whether a JSON body's tool
    calls are read (traces.source: response). config is None when there
    are problems."""
    where = "adapter.invocation.conversation.history"
    if not isinstance(block, dict):
        return None, [f"{where} must be a mapping (preset: ag-ui | openai, "
                      "or the user/assistant templates)"]
    problems = []
    unknown = sorted(set(block) - set(KEYS))
    if unknown:
        problems.append(f"unknown key(s) in {where}: {', '.join(unknown)}")
    tools = block.get("tools", TEXT_ONLY)
    if tools not in TOOLS_MODES:
        problems.append(f"{where}.tools must be one of "
                        f"{'|'.join(TOOLS_MODES)}, got {tools!r}")
    if any(_ENV_REF_RE.search(s) for s in _strings(raw_block)):
        problems.append(f"{where} holds a ${{REF}}: the history carries case "
                        "and app text only, and a resolved value would be "
                        "recorded in every request.json")
    preset = block.get("preset")
    given = [k for k in TEMPLATE_KEYS + ("tool_calls_at",) if k in block]
    if preset is not None and given:
        problems.append(f"{where}: preset is declared beside template(s) "
                        f"{', '.join(given)}; declare one or the other")
        return None, problems
    if preset is not None:
        if preset not in PRESETS:
            problems.append(f"{where}.preset must be one of "
                            f"{'|'.join(PRESETS)}, got {preset!r}")
            return None, problems
        source = PRESETS[preset]
    elif not given:
        problems.append(f"{where} needs a preset (ag-ui | openai) or the "
                        "user and assistant templates")
        return None, problems
    else:
        source = block
    config = {"preset": preset, "tools": tools}
    for key in ("user", "assistant"):
        value = _template(source, key, where, problems)
        if value is not None and not _mentions(value, "<text>"):
            problems.append(f"{where}.{key} has no <text> placeholder, so "
                            "the message would carry no text")
        config[key] = value
    have_tools = all(k in source for k in TOOL_KEYS)
    if have_tools:
        at = source.get("tool_calls_at")
        if not isinstance(at, str) or not at.strip():
            problems.append(f"{where}.tool_calls_at must name the key on an "
                            "assistant entry that holds its calls")
        config["tool_calls_at"] = at
        config["tool_call"] = _template(source, "tool_call", where, problems)
        config["tool_result"] = _template(source, "tool_result", where,
                                          problems)
    elif preset is None and any(k in source for k in TOOL_KEYS):
        problems.append(f"{where}: tool_calls_at, tool_call and tool_result "
                        "go together (got {})".format(", ".join(
                            k for k in TOOL_KEYS if k in source)))
    if tools == CARRIED:
        if not have_tools:
            problems.append(f"{where}.tools: carried needs the tool_calls_at, "
                            "tool_call and tool_result templates (a preset "
                            "supplies them)")
        protocol = (sse or {}).get("protocol")
        if protocol == "data-path":
            problems.append(f"{where}.tools: carried needs a tool source, "
                            "and a data-path stream carries no tool calls; "
                            "it is text-only only")
        elif protocol is None and not json_tool_source:
            problems.append(f"{where}.tools: carried needs a tool source: an "
                            "ag-ui stream, or a JSON body read through "
                            "traces.tool_calls_path (traces.source: "
                            "response)")
    return (None if problems else config), problems


def manifest_view(config):
    """The expanded form the manifest records."""
    return {key: value for key, value in config.items() if key != "turn_beside"}


def render(node, values):
    """One pass over a message template. A string that IS a placeholder
    becomes the typed value; one that CONTAINS placeholders gets each
    replaced once, left to right, so inserted text is never scanned."""
    if isinstance(node, dict):
        return {key: render(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [render(value, values) for value in node]
    if not isinstance(node, str):
        return node
    if node in values:
        return values[node]

    def put(match):
        if match.group(0) not in values:
            return match.group(0)
        value = values[match.group(0)]
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False)
    return _ENTRY_RE.sub(put, node)


def _id(value):
    return value if isinstance(value, str) and value else str(uuid.uuid4())


def user_entry(config, text):
    """Turn n's user entry, with a fresh <message id> (SS3.1 step 1)."""
    return render(config["user"], {"<text>": text,
                                   "<message id>": str(uuid.uuid4())})


def history_value(config, history, entry):
    """What `<history>` holds for this request: the earlier entries, plus
    the current user entry unless the body carries <user turn> beside it."""
    return list(history) if config["turn_beside"] else list(history) + [entry]


def _assistant(config, text, message_id, calls):
    entry = render(config["assistant"], {"<text>": text,
                                         "<message id>": _id(message_id)})
    if calls:
        entry[config["tool_calls_at"]] = [
            render(config["tool_call"], {
                "<call id>": call["id"], "<tool name>": call.get("name"),
                "<arguments>": call.get("arguments")})
            for call in calls]
    return entry


def _results(config, calls, message_ids):
    return [render(config["tool_result"], {
        "<call id>": call["id"], "<result>": call.get("result"),
        "<tool name>": call.get("name"),
        "<message id>": _id(message_ids.get(call["id"]))})
        for call in calls]


def _call_id(call):
    """A call's id as a string: a non-empty string, or an int (a JSON body
    may number its calls). None when it has neither."""
    cid = call.get("id") if isinstance(call, dict) else None
    if isinstance(cid, str) and cid:
        return cid
    if isinstance(cid, int) and not isinstance(cid, bool):
        return str(cid)
    return None


def _uncarriable(call):
    if _call_id(call) is None:
        return "a tool call in the reply carried no id"
    if call.get("result") is None:
        return (f"tool call {call.get('name')!r} has no result: a pending "
                "approval or a client-side tool, which only resuming the run "
                "could answer (docs/client-history.md SS6)")
    return None


def reply_entries(config, body, answer, gap, protocol, json_calls=None):
    """The reply rebuilt into history entries: (entries, None), or (None, why
    it cannot be carried). SS3.2-SS3.4.

    `answer` is the value at the response template's <answer>, `gap` the
    runner's stream_gap(body, answer=True), `protocol` the stream's
    (ag-ui | data-path) or None for a JSON body. `json_calls` is a JSON
    body's tool calls under tools: carried, as run_cases.response_tool_calls
    read them: (the list, None) or (None, why there is none)."""
    if not isinstance(answer, str) or not answer:
        if gap:
            return None, gap
        return None, ("no finished text at the response template's <answer> "
                      "field")
    carried = config["tools"] == CARRIED
    if protocol == "ag-ui":
        return _ag_ui_entries(config, body, answer, carried)
    if protocol is None and carried:
        calls, why = json_calls or (None, "no tool-call list was read")
        if calls is None:
            return None, why
        for call in calls:
            why = _uncarriable(call)
            if why:
                return None, why
        calls = [dict(call, id=_call_id(call)) for call in calls]
        entries = []
        if calls:
            # A JSON body gives no order between its calls and its text: the
            # calls came first (an OpenAI-shaped history pairs each with its
            # result before the answer), on one entry with <text> "".
            entries.append(_assistant(config, "", None, calls))
            entries += _results(config, calls, {})
        entries.append(_assistant(config, answer, None, []))
        return entries, None
    return [_assistant(config, answer, None, [])], None


def _ag_ui_entries(config, body, answer, carried):
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None, "the decoded stream has no items list"
    texts = [i for i in items if i.get("type") == "text" and i.get("text")]
    if "\n\n".join(i["text"] for i in texts) != answer:
        return None, ("the stream's text messages do not join to the scored "
                      "answer (response_body's <answer> is not the decoded "
                      "`text`), so carrying them would send what was not "
                      "scored")
    if not carried:
        return [_assistant(config, i["text"], i.get("id"), [])
                for i in texts], None
    calls = {c.get("id"): c for c in body.get("tool_calls") or []
             if isinstance(c, dict)}
    result_ids = {i.get("id"): i.get("message_id") for i in items
                  if i.get("type") == "tool_result"}
    groups, by_id, current = [], {}, None

    def open_group(message_id, text):
        group = {"id": message_id, "text": text, "calls": []}
        groups.append(group)
        if isinstance(message_id, str):
            by_id[message_id] = group
        return group

    for item in items:
        kind = item.get("type")
        if kind == "text":
            if not item.get("text"):
                continue
            named = by_id.get(item.get("id"))
            if (named is not None and not named["text"]
                    and groups and groups[-1] is named):
                # A call named this message before its text arrived, and
                # nothing came between: the text fills that entry. Text that
                # arrives after other entries opens its own, so the history
                # keeps the stream's order.
                named["text"] = item["text"]
                current = named
            else:
                current = open_group(item.get("id"), item["text"])
        elif kind == "tool_call":
            call = calls.get(item.get("id"))
            why = _uncarriable(call)
            if why:
                return None, why
            call = dict(call, id=_call_id(call))
            parent = item.get("parent_message_id")
            if parent in by_id:
                group = by_id[parent]
            elif parent is not None:
                group = current = open_group(parent, "")
            elif current is not None:
                group = current
            else:
                group = current = open_group(None, "")
            group["calls"].append(call)
        elif kind == "tool_result":
            current = None
    if "\n\n".join(g["text"] for g in groups if g["text"]) != answer:
        # The guard above read the items; this one reads what is carried.
        return None, ("the rebuilt history's assistant texts do not join to "
                      "the scored answer in the stream's order, so carrying "
                      "them would send what was not scored")
    entries = []
    for group in groups:
        entries.append(_assistant(config, group["text"], group["id"],
                                  group["calls"]))
        entries += _results(config, group["calls"], result_ids)
    return entries, None
