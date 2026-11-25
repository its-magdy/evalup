#!/usr/bin/env python3
"""Tests for client-sent history conversations (docs/client-history.md).

The runner is driven as a real subprocess against a real HTTP server, as in
test_multi_turn.py, whose fixtures this module borrows. Each stub app is one
of SS7's: one that remembers only from the posted history (and refuses an
assistant line it never said), one that ignores the history, a strict AG-UI
server, a streaming tool agent, one that fails once at turn 2, and one that
takes the history beside the turn.

Run: python3 -m unittest discover -s tests -v   (from the plugin root)
"""
import http.server
import itertools
import json
import pathlib
import sys
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_multi_turn import ConversationCase, with_conversation  # noqa: E402
from test_run_cases import (  # noqa: E402 - the shared fixtures
    SCRIPTS,
    make_case,
    make_plan,
)
from test_sse import (  # noqa: E402
    EVENT_STREAM,
    FINISHED,
    INTERRUPT,
    STARTED,
    stream,
    text_message,
    tool_call,
)

sys.path.insert(0, str(SCRIPTS))
import _history  # noqa: E402
import run_cases  # noqa: E402

JSON = "application/json"


class _Handler(http.server.BaseHTTPRequestHandler):
    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else {}
        self.server.calls.append({"path": self.path, "body": body})
        status, content_type, payload = self.server.responder(self.path, body)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _serve
    do_POST = _serve

    def log_message(self, *args):
        pass


class StubApp:
    """A real server whose responder returns (status, content type, body)."""

    def __init__(self, responder):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      _Handler)
        self.server.responder = responder
        self.server.calls = []
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01},
                                       daemon=True)
        self.thread.start()

    @property
    def calls(self):
        return [c for c in self.server.calls if c["path"] != "/"]

    @property
    def base_url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def reply_to(text, seen):
    """The facts the stubs answer from: turn 1 states an order number, turn
    2 asks about it, which only the history can answer."""
    if "my order is" in text:
        return "Noted: order #42."
    if "arrive" in text:
        return "Order #42 arrives Friday." if any("#42" in s for s in seen) \
            else "Which order do you mean?"
    return "ok"


class HistoryApp:
    """Stateless, OpenAI-shaped (`messages`): remembers only what the posted
    array says. Every reply carries a nonce and is kept; a posted assistant
    entry that is not one of its own replies, verbatim, is a 400. That pins
    "never an invented line" and "carried is scored" (SS7).

    ignore: reads only the last user entry, so it fails at turn 2.
    beside: takes `question` + `chat_history` (SS2, the turn beside it).
    extra:  appended to every reply (a reply containing a placeholder)."""

    def __init__(self, ignore=False, beside=False, extra=""):
        self.ignore, self.beside, self.extra = ignore, beside, extra
        self.issued = set()
        self.nonce = itertools.count(1)
        self.lock = threading.Lock()

    def __call__(self, path, body):
        if path == "/":
            return 200, JSON, {"message": "up"}
        messages = body.get("chat_history" if self.beside else "messages")
        if not isinstance(messages, list):
            return 400, JSON, {"error": "no messages"}
        if self.beside:
            current, earlier = body.get("question"), messages
        else:
            if not messages or messages[-1].get("role") != "user":
                return 400, JSON, {"error": "last message is not the user's"}
            current, earlier = messages[-1]["content"], messages[:-1]
        for message in earlier:
            if message.get("role") not in ("user", "assistant"):
                return 400, JSON, {"error": "bad role"}
            if message["role"] == "assistant" \
                    and message.get("content") not in self.issued:
                return 400, JSON, {"error": "an assistant line I never said"}
        seen = [] if self.ignore else [
            m["content"] for m in earlier if m["role"] == "user"]
        with self.lock:
            nonce = next(self.nonce)
            reply = f"{reply_to(current, seen)} [n{nonce}]{self.extra}"
            self.issued.add(reply)
        return 200, JSON, {"message": reply}


class StrictAgUi:
    """Validates AG-UI's RunAgentInput (SS7): a threadId, a runId never seen
    before, an id and a known role on every message, no `system`, the last
    message the user's; and an assistant entry must be one of its own
    messages -- its messageId and its text. Answers with an AG-UI stream.

    fail_once_at: a turn index whose first request gets one 500.
    snapshot:     also emits a MESSAGES_SNAPSHOT."""

    def __init__(self, fail_once_at=None, snapshot=False):
        self.fail_once_at, self.snapshot = fail_once_at, snapshot
        self.run_ids, self.issued = set(), {}
        self.nonce = itertools.count(1)
        self.lock = threading.Lock()

    def __call__(self, path, body):
        if path == "/":
            return 200, JSON, {"message": "up"}
        messages = body.get("messages")
        problem = None
        with self.lock:
            if not isinstance(body.get("threadId"), str):
                problem = "no threadId"
            elif body.get("runId") in self.run_ids \
                    or not isinstance(body.get("runId"), str):
                problem = "runId seen before"
            self.run_ids.add(body.get("runId"))
        if problem is None and not isinstance(messages, list):
            problem = "no messages"
        for message in messages if problem is None else []:
            if not isinstance(message.get("id"), str):
                problem = "a message with no id"
            elif message.get("role") not in ("user", "assistant", "tool"):
                problem = f"role {message.get('role')!r}"
            elif message["role"] == "assistant" \
                    and self.issued.get(message["id"]) != message["content"]:
                problem = "an assistant message I never sent"
        if problem is None and messages[-1].get("role") != "user":
            problem = "last message is not the user's"
        if problem:
            return 400, JSON, {"error": problem}
        turn = sum(1 for m in messages if m["role"] == "user")
        if turn == self.fail_once_at:
            self.fail_once_at = None
            return 500, JSON, {"error": "boom"}
        seen = [m["content"] for m in messages[:-1] if m["role"] == "user"]
        with self.lock:
            mid = f"m-{next(self.nonce)}"
            text = reply_to(messages[-1]["content"], seen)
            self.issued[mid] = text
        events = [STARTED, *text_message(mid, text)]
        if self.snapshot:
            events.append({"type": "MESSAGES_SNAPSHOT", "messages": []})
        return 200, EVENT_STREAM, stream(*events, FINISHED)


class ToolAgent:
    """A streaming tool agent: two text messages around a tool call whose
    parentMessageId names the first (SS7). `broken` makes turn 2 end on a
    RUN_ERROR, a call with no result (pending), or an approval interrupt
    with no text."""

    def __init__(self, broken=None):
        self.broken = broken

    def __call__(self, path, body):
        if path == "/":
            return 200, JSON, {"message": "up"}
        turn = sum(1 for m in body.get("messages") or []
                   if m.get("role") == "user")
        first = text_message(f"a{turn}", "Looking that up.")
        call = tool_call(f"c{turn}", "list_licences", '{"team": "Cairo"}',
                         result='{"n": 3}')
        call[0]["parentMessageId"] = f"a{turn}"
        second = text_message(f"b{turn}", "Found 3 licences.")
        if turn == 2 and self.broken == "run_error":
            return 200, EVENT_STREAM, stream(
                STARTED, *first, {"type": "RUN_ERROR", "message": "provider",
                                  "code": "overloaded"})
        if turn == 2 and self.broken == "pending":
            pending = tool_call("p2", "commit_edits", "{}")
            pending[0]["parentMessageId"] = "a2"
            return 200, EVENT_STREAM, stream(STARTED, *first, *pending,
                                             FINISHED)
        if turn == 2 and self.broken == "interrupt":
            return 200, EVENT_STREAM, stream(
                STARTED, *tool_call("p2", "commit_edits", "{}"), INTERRUPT)
        return 200, EVENT_STREAM, stream(STARTED, *first, *call, *second,
                                         FINISHED)


def order_case(case_id="c-hist-01", turns=None):
    case = make_case(case_id)
    case["input"] = {"turns": turns or [
        {"user": "my order is #42",
         "expect": {"answer": {"must_contain": ["#42"]}}},
        {"user": "when does it arrive?"}]}
    case["expect"] = {"answer": {"must_contain": ["Friday"],
                                 "must_not_contain": ["Which order"]}}
    return case


def three_turns(case_id="c-hist-03"):
    case = make_case(case_id)
    case["input"] = {"turns": [{"user": "show me the licences"},
                               {"user": "only the expired ones"},
                               {"user": "and the active ones?"}]}
    case["expect"] = {"answer": {"must_contain": ["Found"]}}
    return case


OPENAI_BODY = '{"model": "m", "messages": "<history>"}'
AG_UI_BODY = ('{"threadId": "<uuid>", "runId": "<request id>", '
              '"messages": "<history>", "tools": [], "context": []}')


class HistoryCase(ConversationCase):
    """ConversationCase with history plans and a raw-body stub app."""

    def stub(self, responder):
        app = StubApp(responder)
        self.addCleanup(app.close)
        return app

    def history_plan(self, app, cases, history=None, body=OPENAI_BODY,
                     style="none", sse=False, **block):
        plan = make_plan(self.state, app.base_url, cases=cases)
        invocation = plan["adapter"]["invocation"]
        invocation["request_body"] = body
        if sse:
            invocation["response_format"] = "sse"
            invocation["sse"] = {"protocol": "ag-ui"}
            invocation["response_body"] = '{"text": "<answer>"}'
        return with_conversation(plan, style=style,
                                 history=history or {"preset": "openai"},
                                 **block)

    def ag_ui_plan(self, app, cases, tools="text-only", traces=False):
        plan = self.history_plan(app, cases, {"preset": "ag-ui",
                                              "tools": tools},
                                 body=AG_UI_BODY, style="client-id", sse=True)
        if traces:
            plan["adapter"]["traces"] = {"source": "response"}
            plan["capability_matrix"]["trajectory"] = {"enabled": True}
            plan["capability_matrix"]["tool_selection"] = {"enabled": True}
        return plan

    def turn_answer(self, case_id, turn):
        return (self.out_dir() / "cases" / case_id / "turns" / str(turn)
                / "answer.txt").read_text(encoding="utf-8")


class TestRemembersOnlyFromTheHistory(HistoryCase):
    def test_the_array_carries_the_conversation(self):
        app = self.stub(HistoryApp())
        self.run_ok(self.history_plan(app, [order_case()]))
        verdict = self.read("cases", "c-hist-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "pass", verdict)
        first, second = (c["body"]["messages"] for c in app.calls)
        self.assertEqual(first, [{"role": "user",
                                  "content": "my order is #42"}])
        # What is carried is what was scored: turn 1's answer.txt, byte for
        # byte, and the app accepted it as its own reply.
        self.assertEqual(second, [
            {"role": "user", "content": "my order is #42"},
            {"role": "assistant", "content": self.turn_answer("c-hist-01", 1)},
            {"role": "user", "content": "when does it arrive?"}])
        # request.json keeps the full array each turn sent.
        self.assertEqual(self.read("cases", "c-hist-01", "turns", "2",
                                   "request.json")["body"]["messages"],
                         second)

    def test_an_app_that_ignores_the_history_fails_at_turn_two(self):
        """The client-history twin of LicenceApp(forget=True)."""
        app = self.stub(HistoryApp(ignore=True))
        self.run_ok(self.history_plan(app, [order_case()]))
        verdict = self.read("cases", "c-hist-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "fail")
        self.assertEqual((verdict["turns_sent"], verdict["failed_turn"]),
                         (2, 2))

    def test_the_manifest_records_the_expanded_preset(self):
        app = self.stub(HistoryApp())
        self.run_ok(self.history_plan(app, [order_case()]))
        history = self.read("manifest.yaml")["conversation"]["history"]
        self.assertEqual(history["preset"], "openai")
        self.assertEqual(history["tools"], "text-only")
        self.assertEqual(history["user"],
                         {"role": "user", "content": "<text>"})
        self.assertEqual(history["tool_calls_at"], "tool_calls")

    def test_a_placeholder_in_a_reply_or_a_turn_goes_out_as_written(self):
        app = self.stub(HistoryApp(extra=" (<uuid> <message id>)"))
        case = order_case(turns=[
            {"user": "my order is #42, <uuid> <text>"},
            {"user": "when does it arrive?"}])
        self.run_ok(self.history_plan(app, [case]))
        self.assertEqual(self.read("cases", "c-hist-01",
                                   "verdict.json")["verdict"], "pass")
        user, assistant, _ = app.calls[1]["body"]["messages"]
        self.assertEqual(user["content"], "my order is #42, <uuid> <text>")
        self.assertTrue(assistant["content"].endswith(
            " (<uuid> <message id>)"))

    def test_history_beside_the_turn_holds_the_earlier_turns_only(self):
        app = self.stub(HistoryApp(beside=True))
        plan = self.history_plan(
            app, [order_case()],
            body='{"question": "<user turn>", "chat_history": "<history>"}')
        self.run_ok(plan)
        self.assertEqual(self.read("cases", "c-hist-01",
                                   "verdict.json")["verdict"], "pass")
        first, second = (c["body"] for c in app.calls)
        self.assertEqual(first["chat_history"], [])
        self.assertEqual(second["question"], "when does it arrive?")
        self.assertEqual([m["role"] for m in second["chat_history"]],
                         ["user", "assistant"])

    def test_explicit_templates(self):
        """The explicit form, for a shape no preset fits (SS2)."""
        def parts_app(path, body):
            if path == "/":
                return 200, JSON, {"message": "up"}
            texts = [m["parts"][0]["text"] for m in body["contents"]]
            roles = [m["role"] for m in body["contents"]]
            return 200, JSON, {"message": reply_to(texts[-1], texts[:-1])
                               + " " + "/".join(roles)}
        app = self.stub(parts_app)
        plan = self.history_plan(app, [order_case()], history={
            "user": '{"role": "user", "parts": [{"text": "<text>"}]}',
            "assistant": '{"role": "model", "parts": [{"text": "<text>"}]}'},
            body='{"contents": "<history>"}')
        self.run_ok(plan)
        verdict = self.read("cases", "c-hist-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "pass", verdict)
        self.assertTrue(self.turn_answer("c-hist-01", 2).endswith(
            "user/model/user"))


class TestStrictAgUi(HistoryCase):
    def test_the_preset_and_request_id_satisfy_run_agent_input(self):
        app = self.stub(StrictAgUi())
        self.run_ok(self.ag_ui_plan(app, [order_case()]))
        verdict = self.read("cases", "c-hist-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "pass", verdict)
        first, second = (c["body"] for c in app.calls)
        self.assertEqual(first["threadId"], second["threadId"])
        self.assertNotEqual(first["runId"], second["runId"])
        # The assistant entry carries the app's own messageId; the user
        # entry keeps the id it was minted with.
        self.assertEqual(second["messages"][0]["id"],
                         first["messages"][0]["id"])
        self.assertEqual(second["messages"][1]["id"], "m-1")
        response = self.read("cases", "c-hist-01", "turns", "1",
                             "response.json")
        self.assertIs(response["snapshot_seen"], False)

    def test_a_snapshot_is_recorded_never_adopted(self):
        app = self.stub(StrictAgUi(snapshot=True))
        self.run_ok(self.ag_ui_plan(app, [order_case()]))
        self.assertEqual(self.read("cases", "c-hist-01",
                                   "verdict.json")["verdict"], "pass")
        self.assertIs(self.read("cases", "c-hist-01", "turns", "2",
                                "response.json")["snapshot_seen"], True)
        self.assertEqual(len(app.calls[1]["body"]["messages"]), 3)

    def test_a_restart_carries_nothing_from_the_abandoned_attempt(self):
        """SS4, retries: one 500 at turn 2 restarts from turn 1, whose
        request holds no entry from attempt 1, and every id is fresh."""
        app = self.stub(StrictAgUi(fail_once_at=2))
        self.run_ok(self.ag_ui_plan(app, [order_case()]))
        verdict = self.read("cases", "c-hist-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "pass", verdict)
        bodies = [c["body"] for c in app.calls]
        self.assertEqual(len(bodies), 4)
        restart = bodies[2]
        self.assertEqual([m["role"] for m in restart["messages"]], ["user"])
        self.assertNotEqual(restart["threadId"], bodies[0]["threadId"])
        self.assertNotEqual(restart["messages"][0]["id"],
                            bodies[0]["messages"][0]["id"])
        self.assertEqual(len({b["runId"] for b in bodies}), 4)

    def test_a_single_turn_case_sends_its_one_user_entry(self):
        app = self.stub(StrictAgUi())
        case = make_case("c-0001", expect={"answer": {"must_contain": ["ok"]}})
        self.run_ok(self.ag_ui_plan(app, [case]))
        self.assertEqual(self.read("cases", "c-0001",
                                   "verdict.json")["verdict"], "pass")
        (call,) = app.calls
        self.assertEqual([(m["role"], m["content"])
                          for m in call["body"]["messages"]],
                         [("user", "how many?")])


class TestToolAgent(HistoryCase):
    def test_text_only_carries_two_entries_and_no_tool_parts(self):
        app = self.stub(ToolAgent())
        self.run_ok(self.ag_ui_plan(app, [order_case(turns=[
            {"user": "show me the licences"}, {"user": "only expired"}])],
            traces=True))
        messages = app.calls[1]["body"]["messages"]
        self.assertEqual([(m["role"], m.get("content")) for m in messages], [
            ("user", "show me the licences"),
            ("assistant", "Looking that up."),
            ("assistant", "Found 3 licences."),
            ("user", "only expired")])
        self.assertFalse(any("toolCalls" in m for m in messages))
        self.assertEqual("\n\n".join(m["content"] for m in messages[1:3]),
                         self.turn_answer("c-hist-01", 1))

    def test_carried_pairs_each_call_with_its_result(self):
        app = self.stub(ToolAgent())
        self.run_ok(self.ag_ui_plan(app, [order_case(turns=[
            {"user": "show me the licences"}, {"user": "only expired"}])],
            tools="carried", traces=True))
        messages = app.calls[1]["body"]["messages"]
        self.assertEqual([m["role"] for m in messages],
                         ["user", "assistant", "tool", "assistant", "user"])
        assistant, result = messages[1], messages[2]
        self.assertEqual(assistant["id"], "a1")
        self.assertEqual(assistant["toolCalls"], [{
            "id": "c1", "type": "function",
            "function": {"name": "list_licences",
                         "arguments": '{"team": "Cairo"}'}}])
        self.assertEqual((result["toolCallId"], result["content"]),
                         ("c1", '{"n": 3}'))
        self.assertEqual(messages[3]["id"], "b1")
        self.assertNotIn("toolCalls", messages[3])

    def assert_stops_at_turn_two(self, broken, tools="text-only"):
        app = self.stub(ToolAgent(broken))
        self.run_ok(self.ag_ui_plan(app, [three_turns()], tools=tools,
                                    traces=True))
        verdict = self.read("cases", "c-hist-03", "verdict.json")
        self.assertEqual(verdict["verdict"], "unscored", verdict)
        self.assertEqual(verdict["turns_sent"], 2)
        self.assertIsNone(verdict["failed_turn"])
        self.assertIn("cannot be carried into turn 3", verdict["stop_reason"])
        self.assertEqual(len(app.calls), 2)        # turn 3 never sent
        return verdict["stop_reason"]

    def test_a_run_error_stops_the_conversation(self):
        self.assertIn("run error (overloaded)",
                      self.assert_stops_at_turn_two("run_error"))

    def test_an_interrupt_with_no_text_stops_it(self):
        self.assertIn("approval interrupt with no text",
                      self.assert_stops_at_turn_two("interrupt"))

    def test_a_pending_call_stops_it_under_carried(self):
        self.assertIn("'commit_edits' has no result",
                      self.assert_stops_at_turn_two("pending",
                                                    tools="carried"))

    def test_a_pending_call_is_carried_as_text_under_text_only(self):
        app = self.stub(ToolAgent("pending"))
        self.run_ok(self.ag_ui_plan(app, [three_turns()], traces=True))
        self.assertEqual(len(app.calls), 3)

    def test_a_fail_on_the_same_turn_outranks_the_stop(self):
        app = self.stub(ToolAgent("run_error"))
        case = three_turns()
        case["input"]["turns"][1]["expect"] = {"http": {"status": 201}}
        self.run_ok(self.ag_ui_plan(app, [case], traces=True))
        verdict = self.read("cases", "c-hist-03", "verdict.json")
        self.assertEqual((verdict["verdict"], verdict["failed_turn"]),
                         ("fail", 2))


class TestCarriedJsonBody(HistoryCase):
    """tools: carried on a JSON body read through traces.tool_calls_path."""

    def test_calls_before_the_answer_each_with_its_result(self):
        def app_fn(path, body):
            if path == "/":
                return 200, JSON, {"message": "up"}
            return 200, JSON, {"message": "Found 3.", "tool_calls": [
                {"id": "x1", "name": "count", "arguments": "{}",
                 "result": "3"}]}
        app = self.stub(app_fn)
        plan = self.history_plan(app, [three_turns()],
                                 {"preset": "openai", "tools": "carried"})
        plan["adapter"]["traces"] = {"source": "response"}
        self.run_ok(plan)
        messages = app.calls[1]["body"]["messages"]
        self.assertEqual(messages[1], {"role": "assistant", "content": "",
                                       "tool_calls": [{
                                           "id": "x1", "type": "function",
                                           "function": {"name": "count",
                                                        "arguments": "{}"}}]})
        self.assertEqual(messages[2], {"role": "tool", "tool_call_id": "x1",
                                       "content": "3"})
        self.assertEqual(messages[3], {"role": "assistant",
                                       "content": "Found 3."})

    def test_no_string_at_the_answer_stops_it(self):
        def app_fn(path, body):
            if path == "/":
                return 200, JSON, {"message": "up"}
            return 200, JSON, {"message": None, "tool_calls": []}
        app = self.stub(app_fn)
        case = three_turns()
        case["expect"] = {"http": {"status": 200}}
        self.run_ok(self.history_plan(app, [case]))
        verdict = self.read("cases", "c-hist-03", "verdict.json")
        self.assertEqual((verdict["verdict"], verdict["turns_sent"]),
                         ("unscored", 1))
        self.assertIn("no finished text", verdict["stop_reason"])


class TestReviewFixes(HistoryCase):
    """PR #165's review: the cases its first suite did not hit."""

    def json_app(self, reply):
        def app_fn(path, body):
            if path == "/":
                return 200, JSON, {"message": "up"}
            return 200, JSON, reply
        return self.stub(app_fn)

    def carried_json_plan(self, app, case):
        plan = self.history_plan(app, [case],
                                 {"preset": "openai", "tools": "carried"})
        plan["adapter"]["traces"] = {"source": "response"}
        return plan

    def test_an_absent_tool_call_list_carries_the_text_alone(self):
        app = self.json_app({"message": "Noted: order #42."})
        case = three_turns()
        case["expect"] = {"http": {"status": 200}}
        self.run_ok(self.carried_json_plan(app, case))
        self.assertEqual(len(app.calls), 3)
        self.assertEqual(app.calls[1]["body"]["messages"][1],
                         {"role": "assistant",
                          "content": "Noted: order #42."})

    def test_an_integer_call_id_is_carried_as_a_string(self):
        app = self.json_app({"message": "Found 3.", "tool_calls": [
            {"id": 1, "name": "count", "arguments": "{}", "result": "3"}]})
        self.run_ok(self.carried_json_plan(app, three_turns()))
        messages = app.calls[1]["body"]["messages"]
        self.assertEqual(messages[1]["tool_calls"][0]["id"], "1")
        self.assertEqual(messages[2]["tool_call_id"], "1")

    def test_a_late_text_for_an_early_parent_keeps_the_stream_order(self):
        def app_fn(path, body):
            if path == "/":
                return 200, JSON, {"message": "up"}
            first = tool_call("c1", "count", "{}", result="3")
            first[0]["parentMessageId"] = "m1"
            return 200, EVENT_STREAM, stream(
                STARTED, *first, *text_message("m2", "Checking."),
                *text_message("m1", "Done."),
                *tool_call("c2", "count", "{}", result="4"), FINISHED)
        app = self.stub(app_fn)
        case = three_turns()
        case["expect"] = {"http": {"status": 200}}
        self.run_ok(self.ag_ui_plan(app, [case], tools="carried",
                                    traces=True))
        self.assertEqual(len(app.calls), 3)
        texts = [m["content"] for m in app.calls[1]["body"]["messages"]
                 if m["role"] == "assistant" and m["content"]]
        self.assertEqual("\n\n".join(texts),
                         self.turn_answer("c-hist-03", 1))
        self.assertEqual(texts, ["Checking.", "Done."])

    def test_a_final_turn_with_no_text_is_scored_not_stopped(self):
        def app_fn(path, body):
            if path == "/":
                return 200, JSON, {"message": "up"}
            turn = sum(1 for m in body.get("messages") or []
                       if m.get("role") == "user")
            if turn == 2:
                return 200, EVENT_STREAM, stream(STARTED, FINISHED)
            return 200, EVENT_STREAM, stream(
                STARTED, *text_message("m1", "Noted #42."), FINISHED)
        app = self.stub(app_fn)
        self.run_ok(self.ag_ui_plan(app, [order_case()]))
        verdict = self.read("cases", "c-hist-01", "verdict.json")
        # Scored by the answer layer as any final turn is, not stopped.
        self.assertEqual(verdict["turns_sent"], 2)
        self.assertIsNone(verdict["stop_reason"])
        self.assertEqual(verdict["layers"]["t2.answer"]["verdict"],
                         "unscored")

    def test_a_single_turn_retry_gets_fresh_ids(self):
        app = self.stub(StrictAgUi(fail_once_at=1))
        case = make_case("c-0001", expect={"answer": {"must_contain": ["ok"]}})
        self.run_ok(self.ag_ui_plan(app, [case]))
        first, retry = (c["body"] for c in app.calls)
        self.assertNotEqual(first["runId"], retry["runId"])
        self.assertNotEqual(first["messages"][0]["id"],
                            retry["messages"][0]["id"])


class TestPhaseOneRequestId(HistoryCase):
    def test_a_turn_mentioning_request_id_is_sent_as_written(self):
        app = self.json_app_plain()
        plan = make_plan(self.state, app.base_url, cases=[make_case(
            "c-0001", input={"messages": [
                {"role": "user", "content": "what is a <request id>?"}]})])
        plan["adapter"]["invocation"]["request_body"] = \
            '{"prompt": "User: <user turn>"}'
        self.run_ok(plan)
        self.assertEqual(app.calls[0]["body"]["prompt"],
                         "User: what is a <request id>?")

    def json_app_plain(self):
        def app_fn(path, body):
            return 200, JSON, {"message": "ok"}
        return self.stub(app_fn)


class TestOtherModesReceiveTheArray(HistoryCase):
    def test_function_mode(self):
        (self.tmp / "hist_entry.py").write_text(
            "def handle(request):\n"
            "    texts = [m['content'] for m in request['messages']]\n"
            "    if 'arrive' in texts[-1] and any('#42' in t for t in "
            "texts[:-1]):\n"
            "        return 'Order #42 arrives Friday.'\n"
            "    return 'Noted: order #42.' if '#42' in texts[-1] else '?'\n",
            encoding="utf-8")
        plan = make_plan(self.state, "http://127.0.0.1:9",
                         cases=[order_case()])
        plan["adapter"]["app"] = {"name": "fake", "repo": str(self.tmp)}
        plan["adapter"]["invocation"] = {
            "mode": "function", "entrypoint": "hist_entry:handle",
            "request_body": '{"messages": "<history>"}',
            "conversation": {"style": "none",
                             "history": {"preset": "openai"}},
            "timeout_s": 5, "max_concurrency": 1}
        self.run_ok(plan)
        self.assertEqual(self.read("cases", "c-hist-01",
                                   "verdict.json")["verdict"], "pass")

    def test_exec_mode(self):
        script = self.tmp / "hist_exec.py"
        script.write_text(
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "texts = [m['content'] for m in request['messages']]\n"
            "ok = 'arrive' in texts[-1] and any('#42' in t for t in "
            "texts[:-1])\n"
            "text = 'Order #42 arrives Friday.' if ok else 'Noted: order #42.'"
            "\n"
            "print(json.dumps({'text': text}))\n", encoding="utf-8")
        plan = make_plan(self.state, "http://127.0.0.1:9",
                         cases=[order_case()])
        plan["adapter"]["invocation"] = {
            "mode": "exec", "auth": {"type": "none"},
            "exec": {"argv": [sys.executable, str(script)],
                     "cwd": str(self.tmp)},
            "request_body": '{"messages": "<history>"}',
            "response_body": '{"text": "<answer>"}',
            "conversation": {"style": "none",
                             "history": {"preset": "openai"}},
            "timeout_s": 10, "max_concurrency": 1}
        self.run_ok(plan)
        self.assertEqual(self.read("cases", "c-hist-01",
                                   "verdict.json")["verdict"], "pass")


class TestSingleTurnUnchanged(HistoryCase):
    """SS4: a single-turn case scores as it does without a history."""

    def test_the_same_verdict_with_and_without_a_history(self):
        def app_fn(path, body):
            return 200, JSON, {"message": "ok"}
        app = self.stub(app_fn)
        verdicts = []
        for n, history in enumerate((True, False)):
            plan = make_plan(self.state, app.base_url,
                             run_id=f"smoke-20260908T12000{n}Z")
            if history:
                plan = self.history_plan(app, plan["cases"])
                plan["run_id"] = "smoke-20260908T120000Z"
            self.run_ok(plan)
            verdict = json.loads((self.out_dir(plan["run_id"]) / "cases"
                                  / "c-0001" / "verdict.json")
                                 .read_text(encoding="utf-8"))
            verdict.pop("latency_s")
            verdicts.append(verdict)
        self.assertEqual(verdicts[0], verdicts[1])
        self.assertEqual(verdicts[0]["verdict"], "pass")


class TestHistoryPreflight(HistoryCase):
    """SS2's pre-flight: every refusal is exit 3 with nothing billed."""

    def assert_refused(self, fragment, plan):
        rc, payload, proc = self.invoke(plan)
        self.assertEqual(rc, 3, proc.stdout + proc.stderr)
        self.assertIn(fragment, payload["error"])
        self.assertEqual(self.app.calls, [])

    def plan(self, history=None, body=OPENAI_BODY, style="none", **kw):
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["invocation"]["request_body"] = body
        if history is not None:
            with_conversation(plan, style=style, history=history)
        plan["adapter"]["invocation"].update(kw)
        return plan

    def test_history_outside_conversation(self):
        plan = self.plan()
        plan["adapter"]["invocation"]["history"] = {"preset": "openai"}
        self.assert_refused("belongs inside", plan)

    def test_unknown_key_and_bad_values(self):
        for history, fragment in (
                ({"preset": "openai", "tool": "carried"}, "unknown key"),
                ({"preset": "anthropic"}, "preset must be one of"),
                ({"preset": "openai", "tools": "all"}, "tools must be one of")):
            with self.subTest(history=history):
                self.assert_refused(fragment, self.plan(history))

    def test_preset_beside_a_template_or_neither(self):
        self.assert_refused("preset is declared beside", self.plan(
            {"preset": "openai", "user": '{"role": "user", "c": "<text>"}'}))
        self.assert_refused("needs a preset", self.plan({"tools": "text-only"}))

    def test_bad_templates(self):
        good = '{"role": "assistant", "content": "<text>"}'
        for user, fragment in (('[1]', "must be a JSON object"),
                               ('{"role": ', "is not valid JSON"),
                               ('{"role": "user"}', "no <text> placeholder")):
            with self.subTest(user=user):
                self.assert_refused(fragment, self.plan(
                    {"user": user, "assistant": good}))

    def test_history_marker_placement(self):
        for body, fragment in (
                ('{"messages": []}', "no <history> placeholder"),
                ('{"a": "<history>", "b": "<history>"}', "2 times"),
                ('{"messages": "x <history>"}', "whole text")):
            with self.subTest(body=body):
                self.assert_refused(fragment, self.plan(
                    {"preset": "openai"}, body=body))

    def test_history_marker_without_a_history(self):
        self.assert_refused("declares no invocation.conversation.history",
                            self.plan(body=OPENAI_BODY))
        plan = self.plan(body='{"sessionId": "<uuid>", "m": "<history>"}')
        with_conversation(plan, style="client-id")
        self.assert_refused("declares no invocation.conversation.history",
                            plan)

    def test_carried_needs_tool_templates_and_a_tool_source(self):
        templates = {"user": '{"role": "user", "content": "<text>"}',
                     "assistant": '{"role": "assistant", "content": "<text>"}',
                     "tools": "carried"}
        plan = self.plan(templates)
        plan["adapter"]["traces"] = {"source": "response"}
        self.assert_refused("needs the tool_calls_at", plan)
        # A JSON body with no tool-call source.
        self.assert_refused("needs a tool source",
                            self.plan({"preset": "openai", "tools": "carried"}))
        # A data-path stream carries no tool calls.
        plan = self.plan({"preset": "openai", "tools": "carried"},
                         response_format="sse",
                         sse={"protocol": "data-path",
                              "text_path": "choices.0.delta.content"},
                         response_body='{"text": "<answer>"}')
        self.assert_refused("data-path stream carries no tool calls", plan)

    def test_tool_templates_go_together(self):
        self.assert_refused("go together", self.plan({
            "user": '{"role": "user", "content": "<text>"}',
            "assistant": '{"role": "assistant", "content": "<text>"}',
            "tool_calls_at": "tool_calls"}))

    def test_an_env_ref_in_a_template(self):
        plan = self.plan({
            "user": '{"role": "user", "content": "<text>", '
                    '"k": "${EVAL_USER_ID}"}',
            "assistant": '{"role": "assistant", "content": "<text>"}'})
        self.assert_refused("holds a ${REF}", plan)


class TestRender(unittest.TestCase):
    """The message templates render in one pass (SS3.1)."""

    def test_inserted_text_is_never_scanned(self):
        out = _history.render({"t": "said: <text> (<message id>)",
                               "whole": "<text>"},
                              {"<text>": "<message id> <uuid>",
                               "<message id>": "m1"})
        self.assertEqual(out, {"t": "said: <message id> <uuid> (m1)",
                               "whole": "<message id> <uuid>"})

    def test_a_typed_value_stays_typed(self):
        out = _history.render({"args": "<arguments>", "s": "a=<arguments>"},
                              {"<arguments>": {"q": 1}})
        self.assertEqual(out, {"args": {"q": 1}, "s": 'a={"q": 1}'})

    def test_request_template_renders_history_as_a_typed_value(self):
        body = run_cases.render_template(
            {"messages": "<history>", "note": "<uuid>"},
            {"<history>": [{"role": "user", "content": "<uuid>"}],
             "<uuid>": "u-1"})
        self.assertEqual(body, {"messages": [{"role": "user",
                                              "content": "<uuid>"}],
                                "note": "u-1"})


if __name__ == "__main__":
    unittest.main()
