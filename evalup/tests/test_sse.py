"""SSE answers (#39): scripts/_sse.py on its own, then through the runner.

The AG-UI frames below follow @ag-ui/core 1.x's event schemas (field names
toolCallId, toolCallName, delta, messageId; RUN_ERROR's message/code;
RUN_FINISHED's usage list), checked against the published package on
2026-11-20 -- the wire format of the first live-proof app (#27).
"""
import http.server
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_run_cases import (  # noqa: E402 - the shared fixtures
    SCRIPTS,
    RunnerCase,
    make_case,
    make_plan,
)

sys.path.insert(0, str(SCRIPTS))
import _sse  # noqa: E402

AG_UI = {"protocol": "ag-ui"}


def stream(*events, terminate=True):
    """An event-stream body, one `data:` frame per event."""
    text = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return text if terminate else text.rstrip("\n")


def text_message(mid, *deltas):
    return ([{"type": "TEXT_MESSAGE_START", "messageId": mid,
              "role": "assistant"}]
            + [{"type": "TEXT_MESSAGE_CONTENT", "messageId": mid,
                "delta": d} for d in deltas]
            + [{"type": "TEXT_MESSAGE_END", "messageId": mid}])


def tool_call(cid, name, *arg_deltas, result=None):
    events = [{"type": "TOOL_CALL_START", "toolCallId": cid,
               "toolCallName": name}]
    events += [{"type": "TOOL_CALL_ARGS", "toolCallId": cid, "delta": d}
               for d in arg_deltas]
    events.append({"type": "TOOL_CALL_END", "toolCallId": cid})
    if result is not None:
        events.append({"type": "TOOL_CALL_RESULT", "messageId": "r-" + cid,
                       "toolCallId": cid, "content": result})
    return events


STARTED = {"type": "RUN_STARTED", "threadId": "t", "runId": "r"}
FINISHED = {"type": "RUN_FINISHED", "threadId": "t", "runId": "r"}


def agent_run(*parts, end=FINISHED):
    events = [STARTED]
    for part in parts:
        events += part
    if end is not None:
        events.append(end)
    return stream(*events)


class TestFrames(unittest.TestCase):
    def test_line_endings_multiline_data_and_comments(self):
        body = ": keep-alive\r\ndata: one\r\ndata: two\r\n\r\n" \
               "event: x\ndata:three\n\n\rdata: four"
        self.assertEqual(_sse.frames(body), ["one\ntwo", "three", "four"])

    def test_an_event_with_no_data_dispatches_nothing(self):
        self.assertEqual(_sse.frames("event: ping\n\nid: 3\n\n"), [])


class TestAgUi(unittest.TestCase):
    def test_text_and_tool_calls_are_joined_in_order(self):
        body = _sse.decode(agent_run(
            text_message("m1", "Let me ", "look."),
            tool_call("c1", "find_person", '{"name":', '"Nour"}',
                      result='[{"id": 1}]'),
            tool_call("c2", "get_seats", "{}"),
            text_message("m2", "Two ", "people."),
            end=dict(FINISHED, usage=[{"inputTokens": 100, "outputTokens": 9},
                                      {"inputTokens": 50, "outputTokens": 1}]),
        ), AG_UI)
        self.assertEqual(body["text"], "Let me look.\n\nTwo people.")
        self.assertIsNone(body["partial_text"])
        self.assertEqual(body["tool_calls"], [
            {"id": "c1", "name": "find_person",
             "arguments": '{"name":"Nour"}', "result": '[{"id": 1}]'},
            {"id": "c2", "name": "get_seats", "arguments": "{}",
             "result": None}])
        self.assertEqual(body["usage"],
                         {"input_tokens": 150, "output_tokens": 10})
        self.assertTrue(body["finished"])
        self.assertFalse(body["interrupted"])
        self.assertIsNone(body["error"])

    def test_usage_sums_over_every_terminal_event(self):
        # #46: a server that emits one RUN_FINISHED per model call (an agent
        # loop) reported only the last call's tokens. Every RUN_FINISHED and
        # RUN_ERROR counts once; one without usage adds nothing, not zeros.
        body = _sse.decode(stream(
            STARTED,
            *tool_call("c1", "lookup", "{}", result="x"),
            dict(FINISHED, usage=[{"inputTokens": 100, "outputTokens": 10,
                                   "cachedInputTokens": 80,
                                   "cacheWriteInputTokens": 15}]),
            STARTED,
            *text_message("m1", "Done."),
            dict(FINISHED, usage=[{"inputTokens": 200, "outputTokens": 20,
                                   "cachedInputTokens": 190}]),
            dict(FINISHED),
            dict(FINISHED, usage=[{"inputTokens": 300, "outputTokens": 30}]),
        ), AG_UI)
        self.assertEqual(body["text"], "Done.")
        self.assertEqual(body["usage"], {
            "input_tokens": 600, "output_tokens": 60,
            "cached_input_tokens": 270, "cache_write_input_tokens": 15})
        self.assertEqual(body["run_usage"], [
            {"input_tokens": 100, "output_tokens": 10,
             "cached_input_tokens": 80, "cache_write_input_tokens": 15},
            {"input_tokens": 200, "output_tokens": 20,
             "cached_input_tokens": 190},
            {"input_tokens": 300, "output_tokens": 30}])

    def test_a_run_error_usage_is_added_to_the_finished_runs(self):
        body = _sse.decode(stream(
            STARTED,
            dict(FINISHED, usage=[{"inputTokens": 7, "outputTokens": 1}]),
            {"type": "RUN_ERROR", "message": "x",
             "usage": [{"inputTokens": 3, "outputTokens": 2}]}), AG_UI)
        self.assertEqual(body["usage"],
                         {"input_tokens": 10, "output_tokens": 3})
        self.assertEqual(len(body["run_usage"]), 2)

    def test_no_usage_anywhere_is_none_not_zero(self):
        body = _sse.decode(stream(
            STARTED, FINISHED,
            dict(FINISHED, usage=[{"inputTokens": "9", "outputTokens": True,
                                   "cachedInputTokens": 1.5}, "junk"]),
            # A cache count is part of inputTokens: alone, it is no model
            # call with zero tokens.
            dict(FINISHED, usage=[{"cachedInputTokens": 80}])),
            AG_UI)
        self.assertIsNone(body["usage"])
        self.assertEqual(body["run_usage"], [])

    def test_a_run_error_leaves_no_answer_but_keeps_what_arrived(self):
        body = _sse.decode(agent_run(
            text_message("m1", "Half an ans"),
            end={"type": "RUN_ERROR", "message": "stopped",
                 "code": "budget_reached"}), AG_UI)
        self.assertIsNone(body["text"])
        self.assertEqual(body["partial_text"], "Half an ans")
        self.assertEqual(body["error"], {"message": "stopped",
                                         "code": "budget_reached"})

    def test_a_stream_without_its_terminal_event_is_not_finished(self):
        body = _sse.decode(agent_run(text_message("m1", "cut"), end=None),
                           AG_UI)
        self.assertFalse(body["finished"])
        self.assertIsNone(body["text"])
        self.assertEqual(body["partial_text"], "cut")

    def test_an_interrupt_still_finishes_the_run(self):
        body = _sse.decode(agent_run(
            text_message("m1", "Here is the change."),
            tool_call("c1", "apply_changes", "{}"),
            end=dict(FINISHED, outcome={"type": "interrupt",
                                        "interrupts": []})), AG_UI)
        self.assertTrue(body["finished"])
        self.assertTrue(body["interrupted"])
        self.assertEqual(body["text"], "Here is the change.")

    def test_chunk_events_continue_the_last_message_and_call(self):
        body = _sse.decode(agent_run([
            {"type": "TEXT_MESSAGE_CHUNK", "messageId": "m1", "delta": "a"},
            {"type": "TEXT_MESSAGE_CHUNK", "delta": "b"},
            {"type": "TOOL_CALL_CHUNK", "toolCallId": "c1",
             "toolCallName": "t", "delta": '{"x"'},
            {"type": "TOOL_CALL_CHUNK", "delta": ": 1}"},
        ]), AG_UI)
        self.assertEqual(body["text"], "ab")
        self.assertEqual(body["tool_calls"][0]["arguments"], '{"x": 1}')

    def test_a_finished_run_with_no_text_has_no_answer(self):
        body = _sse.decode(agent_run(tool_call("c1", "t", "{}")), AG_UI)
        self.assertIsNone(body["text"])
        self.assertEqual(len(body["tool_calls"]), 1)

    def test_bad_frames_are_counted_not_fatal(self):
        body = _sse.decode("data: not json\n\n" + agent_run(
            text_message("m1", "ok")), AG_UI)
        self.assertEqual(body["sse"]["bad_frames"], 1)
        self.assertEqual(body["text"], "ok")


class TestDataPath(unittest.TestCase):
    CONFIG = {"protocol": "data-path", "text_path": "choices.0.delta.content",
              "done": "[DONE]"}

    def frames(self, *deltas, done=True):
        text = "".join("data: " + json.dumps(
            {"choices": [{"delta": {"content": d}}]}) + "\n\n" for d in deltas)
        return text + ("data: [DONE]\n\n" if done else "")

    def test_deltas_join_until_the_sentinel(self):
        body = _sse.decode(self.frames("Hel", "lo"), self.CONFIG)
        self.assertEqual((body["text"], body["finished"]), ("Hello", True))
        self.assertNotIn("tool_calls", body)

    def test_no_sentinel_means_not_finished(self):
        body = _sse.decode(self.frames("Hel", done=False), self.CONFIG)
        self.assertEqual((body["text"], body["partial_text"]), (None, "Hel"))

    def test_without_a_declared_sentinel_eof_ends_the_stream(self):
        config = dict(self.CONFIG)
        del config["done"]
        body = _sse.decode(self.frames("Hi", done=False), config)
        self.assertEqual(body["text"], "Hi")


class TestConfig(unittest.TestCase):
    def test_sound_blocks(self):
        self.assertEqual(_sse.check_config({"protocol": "ag-ui"}), [])
        self.assertEqual(_sse.check_config(
            {"protocol": "data-path", "text_path": "a.b"}), [])

    def test_defects(self):
        cases = [
            ("not a mapping", "mapping"),
            ({"protocol": "sse"}, "protocol must be one of"),
            ({"protocol": "ag-ui", "colour": 1}, "unknown key"),
            ({"protocol": "data-path"}, "needs text_path"),
            ({"protocol": "ag-ui", "text_path": "a"}, "belong(s) to"),
            ({"protocol": "data-path", "text_path": "a", "done": 1},
             "must be a string"),
        ]
        for block, needle in cases:
            with self.subTest(block=block):
                self.assertIn(needle, "; ".join(_sse.check_config(block)))


class TestSpans(unittest.TestCase):
    def normalize(self, doc):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "trace.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "normalize_trace.py"), str(path),
             "--trace-id", "ab" * 16], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["status"], "ok")
        return out

    def test_normalize_trace_reads_the_spans_in_response_order(self):
        calls = [{"id": "c1", "name": "b_tool", "arguments": '{"k": 1}',
                  "result": "r"},
                 {"id": "c2", "name": "a_tool", "arguments": {"k": 2},
                  "result": None}]
        doc = _sse.tool_calls_to_otlp(calls, "ab" * 16,
                                      {"input_tokens": 5, "output_tokens": 2})
        out = self.normalize(doc)
        tools = out["trajectory"]["tool_calls"]
        self.assertEqual([t["name"] for t in tools], ["b_tool", "a_tool"])
        self.assertEqual([t["args"] for t in tools], [{"k": 1}, {"k": 2}])
        self.assertEqual(out["trajectory"]["usage"],
                         {"input_tokens": 5, "output_tokens": 2})
        # No invented timing: every duration is unknown.
        self.assertIsNone(tools[0]["duration_ms"])

    def test_one_llm_call_per_run_usage_entry(self):
        # #46: three RUN_FINISHED frames with usage are three llm_calls whose
        # tokens sum; cache counts ride on the chat span as attributes.
        body = _sse.decode(stream(
            STARTED,
            *tool_call("c1", "lookup", "{}"),
            dict(FINISHED, usage=[{"inputTokens": 100, "outputTokens": 10,
                                   "cachedInputTokens": 80}]),
            *tool_call("c2", "lookup", "{}"),
            dict(FINISHED, usage=[{"inputTokens": 200, "outputTokens": 20}]),
            *text_message("m1", "ok"),
            dict(FINISHED, usage=[{"inputTokens": 300, "outputTokens": 30,
                                   "cacheWriteInputTokens": 5}]),
        ), AG_UI)
        doc = _sse.tool_calls_to_otlp(body["tool_calls"], "ab" * 16,
                                      body["run_usage"])
        spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len({s["spanId"] for s in spans}), len(spans))
        chat = [{a["key"]: a["value"]["intValue"]
                 for a in s["attributes"] if "intValue" in a["value"]}
                for s in spans if s["name"] == "chat"]
        self.assertEqual(chat[0]["gen_ai.usage.cache_read.input_tokens"], 80)
        self.assertEqual(chat[2]["gen_ai.usage.cache_creation.input_tokens"],
                         5)
        self.assertNotIn("gen_ai.usage.cache_read.input_tokens", chat[1])
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "trace.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "normalize_trace.py"), str(path),
             "--trace-id", "ab" * 16], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)["trajectory"]
        # #105: the cache counts survive into the trajectory, per call.
        self.assertEqual([c["usage"] for c in out["llm_calls"]], [
            {"input_tokens": 100, "output_tokens": 10,
             "cache_read_input_tokens": 80},
            {"input_tokens": 200, "output_tokens": 20},
            {"input_tokens": 300, "output_tokens": 30,
             "cache_write_input_tokens": 5}])
        self.assertEqual(out["usage"],
                         {"input_tokens": 600, "output_tokens": 60,
                          "cache_read_input_tokens": 80,
                          "cache_write_input_tokens": 5})
        self.assertEqual([t["name"] for t in out["tool_calls"]],
                         ["lookup", "lookup"])

    def test_a_zero_argument_call_has_empty_args(self):
        # #47: a call with no arguments sends no TOOL_CALL_ARGS, so its
        # argument string is empty. That is "no arguments" ({}), not an
        # unparseable one; malformed JSON still is.
        body = agent_run(tool_call("c1", "no_args_tool"),
                         tool_call("c2", "blank_tool", "  "),
                         tool_call("c3", "bad_tool", '{"k":'))
        calls = _sse.decode(body, AG_UI)["tool_calls"]
        self.assertEqual([c["arguments"] for c in calls], ["", "  ", '{"k":'])
        out = self.normalize(_sse.tool_calls_to_otlp(calls, "ab" * 16))
        self.assertEqual([t["args"] for t in out["trajectory"]["tool_calls"]],
                         [{}, {}, {"_unparsed": True}])


# -- through the runner ---------------------------------------------------

class _RawHandler(http.server.BaseHTTPRequestHandler):
    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.calls.append(json.loads(raw) if raw else {})
        status, content_type, text = self.server.responder(self.path)
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


class RawApp:
    def __init__(self, responder):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      _RawHandler)
        self.server.responder = responder
        self.server.calls = []
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01},
                                       daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


EVENT_STREAM = "text/event-stream; charset=utf-8"


class SSERunnerCase(RunnerCase):
    """RunnerCase with an app that answers raw bodies."""

    body = agent_run(text_message("m1", "ok, ", "done"),
                     tool_call("c1", "list_invoices", '{"unit_id": "U-1"}',
                               result='{"n": 7}'))
    status = 200
    content_type = EVENT_STREAM

    def setUp(self):
        super().setUp()
        self.raw = RawApp(lambda path: (self.status, self.content_type,
                                        self.body))
        self.addCleanup(self.raw.close)

    def sse_plan(self, cases=None, traces=True):
        plan = make_plan(self.state, self.raw.base_url,
                         cases=cases or [make_case("c-0001")])
        invocation = plan["adapter"]["invocation"]
        invocation["response_format"] = "sse"
        invocation["sse"] = {"protocol": "ag-ui"}
        invocation["response_body"] = '{"text": "<answer>"}'
        if traces:
            plan["adapter"]["traces"] = {"source": "response"}
            plan["capability_matrix"] = {
                "routing": {"enabled": True},
                "trajectory": {"enabled": True},
                "tool_selection": {"enabled": True}}
        return plan


class TestRunnerSumsEveryModelCall(SSERunnerCase):
    """#46 through the runner: one RUN_FINISHED per model call."""

    body = stream(
        STARTED,
        *tool_call("c1", "list_invoices", '{"unit_id": "U-1"}'),
        dict(FINISHED, usage=[{"inputTokens": 100, "outputTokens": 10}]),
        *tool_call("c2", "list_invoices", '{"unit_id": "U-2"}'),
        dict(FINISHED, usage=[{"inputTokens": 200, "outputTokens": 20}]),
        *text_message("m1", "ok, ", "done"),
        dict(FINISHED, usage=[{"inputTokens": 300, "outputTokens": 30,
                               "cachedInputTokens": 250}]))

    def test_the_trajectory_has_every_call_and_their_sum(self):
        rc, _, proc = self.invoke(self.sse_plan())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        response = self.read("cases", "c-0001", "response.json")
        self.assertEqual(response["body"]["usage"], {
            "input_tokens": 600, "output_tokens": 60,
            "cached_input_tokens": 250})
        trajectory = self.read("cases", "c-0001",
                               "trajectory.json")["trajectory"]
        self.assertEqual(len(trajectory["llm_calls"]), 3)
        self.assertEqual(trajectory["usage"],
                         {"input_tokens": 600, "output_tokens": 60,
                          "cache_read_input_tokens": 250})

    def test_score_cost_splits_the_cache_off_this_runs_own_tree(self):
        """#105 end to end: stream -> manifest -> normalize_trace ->
        score_cost. The manifest is the runner's, so the split is made on
        what the run actually recorded about its source."""
        rc, _, proc = self.invoke(self.sse_plan())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        table = self.tmp / "prices.json"
        table.write_text(json.dumps({
            "schema": "evalup/price-table/1", "as_of": "2026-09-01",
            "source": "test", "aliases": {"<model attribute absent>": "m"},
            "models": {"m": {"input_per_mtok": "2", "output_per_mtok": "10",
                             "cache_read_per_mtok": "0.2",
                             "cache_write_per_mtok": "2.5"}}}),
            encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "score_cost.py"),
             str(self.out_dir()), "--prices", str(table)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        cost = json.loads(proc.stdout)["cost"]
        self.assertEqual(cost["cache_accounting"], "priced")
        # 350 uncached x 2 + 250 x 0.2 + 60 x 10, per million tokens.
        self.assertEqual(cost["usd"], round((700 + 50 + 600) / 1e6, 6))


class TestEveryRepeatIsPriced(SSERunnerCase):
    """#129 end to end: a k=3 run of a caching app, its first repeat a cache
    write and the next two cache reads, priced off the runner's own tree.
    The representative repeat's figure is unchanged; all_repeats is the
    spend, and the dry-run estimate takes its rate from it."""

    USAGES = (
        {"inputTokens": 1000, "outputTokens": 100,
         "cacheWriteInputTokens": 800},
        {"inputTokens": 1000, "outputTokens": 100, "cachedInputTokens": 800},
        {"inputTokens": 1000, "outputTokens": 50, "cachedInputTokens": 900},
    )

    def setUp(self):
        super().setUp()
        sent = []

        def respond(path):
            if not path.endswith("/api/chat/ask"):
                return 200, "application/json", "{}"
            sent.append(path)
            usage = self.USAGES[min(len(sent), 3) - 1]
            return 200, EVENT_STREAM, agent_run(
                text_message("m1", "ok"), end=dict(FINISHED, usage=[usage]))
        self.raw.server.responder = respond

    def test_all_three_repeats_are_priced_from_their_responses(self):
        plan = self.sse_plan()
        plan["k"] = 3
        plan["capability_matrix"]["cost_latency"] = {"enabled": True}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        table = self.tmp / "prices.json"
        table.write_text(json.dumps({
            "schema": "evalup/price-table/1", "as_of": "2026-09-01",
            "source": "test", "aliases": {"<model attribute absent>": "m"},
            "models": {"m": {"input_per_mtok": "2", "output_per_mtok": "10",
                             "cache_read_per_mtok": "0.2",
                             "cache_write_per_mtok": "2.5"}}}),
            encoding="utf-8")
        cost_json = self.tmp / "cost.json"
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "score_cost.py"),
             str(self.out_dir()), "--prices", str(table),
             "-o", str(cost_json)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        cost = json.loads(cost_json.read_text(encoding="utf-8"))["cost"]
        # Per million tokens:
        #   1: 200 x 2 + 800 x 2.5 + 100 x 10 = 3400
        #   2: 200 x 2 + 800 x 0.2 + 100 x 10 = 1560
        #   3: 100 x 2 + 900 x 0.2 +  50 x 10 =  880
        verdict = self.read("cases", "c-0001", "verdict.json")
        n = next(r["n"] for r in verdict["repeats"]
                 if r["verdict"] == verdict["verdict"])
        self.assertEqual(cost["usd"],
                         round((3400, 1560, 880)[n - 1] / 1e6, 6))
        every = cost["all_repeats"]
        self.assertEqual(every["cache_accounting"], "priced")
        self.assertEqual(every["usd"], round((3400 + 1560 + 880) / 1e6, 6))
        self.assertEqual((every["repeats_with_usage"],
                          every["messages_with_usage"]), (3, 3))
        # The estimate for the same plan takes the all-repeats rate.
        plan["run_id"] = "smoke-20260909T120000Z"
        rc, payload, proc = self.invoke(
            plan, out=self.out_dir(plan["run_id"]),
            extra_argv=["--dry-run", "--cost-from", str(cost_json)])
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        est = payload["estimate"]
        self.assertEqual(est["rate_basis"], "all_repeats")
        self.assertAlmostEqual(est["usd"], every["usd"], places=4)
        self.assertNotIn("overstates", est["note"])


class TestRunnerReadsStreams(SSERunnerCase):
    def test_the_answer_is_the_joined_text(self):
        rc, _, proc = self.invoke(self.sse_plan(traces=False))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["layers"]["answer"]["verdict"], "pass")
        response = self.read("cases", "c-0001", "response.json")
        self.assertEqual(response["answer"], "ok, done")
        self.assertEqual(response["body"]["sse"]["protocol"], "ag-ui")
        manifest = self.read("manifest.yaml")
        self.assertEqual(manifest["invocation"]["response_format"], "sse")
        self.assertEqual(manifest["invocation"]["sse"], {"protocol": "ag-ui"})

    def test_tool_calls_from_the_stream_feed_the_trace_layers(self):
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["list_invoices"]}
        case["expect"]["args"] = {"list_invoices": {"unit_id": "U-1"}}
        case["expect"]["authz"] = {"forbidden_tools": ["delete_invoice"]}
        rc, _, proc = self.invoke(self.sse_plan([case]))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layers = self.read("cases", "c-0001", "verdict.json")["layers"]
        for name in ("trajectory", "tool_selection", "loops", "authz"):
            self.assertEqual(layers[name]["verdict"], "pass", name)
        trajectory = self.read("cases", "c-0001", "trajectory.json")
        self.assertEqual(trajectory["status"], "ok")
        self.assertEqual(trajectory["trajectory"]["tool_calls"][0]["args"],
                         {"unit_id": "U-1"})
        manifest = self.read("manifest.yaml")
        self.assertEqual(manifest["traces"]["source"], "response")
        self.assertTrue(manifest["traces"]["collected"])

    def test_a_wrong_tool_is_a_fail(self):
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["get_rooms"]}
        rc, _, proc = self.invoke(self.sse_plan([case]))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layers = self.read("cases", "c-0001", "verdict.json")["layers"]
        self.assertEqual(layers["trajectory"]["verdict"], "fail")

    def test_an_expected_route_is_never_failed_on_a_nameless_agent(self):
        """The spans name no agent, so a route assertion has no observed
        route from the trace -- that must not read as the wrong route."""
        case = make_case("c-0001")
        case["expect"]["route"] = "billing"
        rc, _, proc = self.invoke(self.sse_plan([case]))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layer = self.read("cases", "c-0001", "verdict.json")["layers"]["routing"]
        self.assertNotEqual(layer["verdict"], "fail", layer)


class TestRunError(SSERunnerCase):
    body = agent_run(text_message("m1", "ok, half"),
                     tool_call("c1", "list_invoices", "{}"),
                     end={"type": "RUN_ERROR", "message": "x",
                          "code": "assistant_unavailable"})

    def test_no_answer_fail_and_no_trajectory_from_a_failed_run(self):
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["list_invoices"]}
        rc, _, proc = self.invoke(self.sse_plan([case]))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layers = self.read("cases", "c-0001", "verdict.json")["layers"]
        self.assertEqual(layers["answer"]["verdict"], "unscored")
        self.assertIn("run error (assistant_unavailable)",
                      layers["answer"]["reason"])
        self.assertEqual(layers["trajectory"]["verdict"], "infra_incomplete")
        self.assertIn("assistant_unavailable", layers["trajectory"]["reason"])
        body = self.read("cases", "c-0001", "response.json")["body"]
        self.assertEqual(body["partial_text"], "ok, half")


INTERRUPT = dict(FINISHED, outcome={"type": "interrupt", "interrupts": []})


class TestApprovalInterrupt(SSERunnerCase):
    """#103: a proposal that ends the run on an approval interrupt. Only the
    answer layer's reason names the interrupt; no verdict moves."""

    body = agent_run(tool_call("c1", "apply_changes", "{}"), end=INTERRUPT)

    def case(self):
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["apply_changes"]}
        return case

    def verdict_for(self, body, out):
        self.body = body
        plan = self.sse_plan([self.case()])
        out = out / plan["run_id"]
        out.parent.mkdir()
        rc, _, proc = self.invoke(plan, out=out)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        return json.loads((out / "cases" / "c-0001" / "verdict.json")
                          .read_text(encoding="utf-8"))

    def test_no_text_is_unscored_and_says_why(self):
        rc, _, proc = self.invoke(self.sse_plan([self.case()]))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layers = self.read("cases", "c-0001", "verdict.json")["layers"]
        self.assertEqual(layers["answer"]["verdict"], "unscored")
        self.assertEqual(layers["answer"]["reason"],
                         "the run ended on an approval interrupt with no "
                         "text; no answer to score")
        self.assertEqual(layers["trajectory"]["verdict"], "pass")

    def test_the_verdict_is_what_a_plain_finish_gets(self):
        """The interrupt run against the same run without the interrupt,
        which the runner scored before #103: byte-identical but the
        reason."""
        no_text = agent_run(tool_call("c1", "apply_changes", "{}"))
        plain = self.verdict_for(no_text, self.tmp / "plain")
        interrupted = self.verdict_for(type(self).body, self.tmp / "interrupted")
        self.assertNotEqual(plain["layers"]["answer"]["reason"],
                            interrupted["layers"]["answer"]["reason"])
        for verdict in (plain, interrupted):
            verdict["layers"]["answer"].pop("reason")
            verdict.pop("latency_s")
        self.assertEqual(plain, interrupted)
        self.assertEqual(interrupted["layers"]["answer"]["verdict"],
                         "unscored")

    def test_text_before_the_interrupt_is_scored(self):
        verdict = self.verdict_for(agent_run(
            text_message("m1", "ok, I propose this change."),
            tool_call("c1", "apply_changes", "{}"), end=INTERRUPT),
            self.tmp / "with-text")
        self.assertEqual(verdict["layers"]["answer"]["verdict"], "pass")
        self.assertEqual(verdict["layers"]["trajectory"]["verdict"], "pass")


class TestJsonErrorUnderSse(SSERunnerCase):
    status = 403
    content_type = "application/json"
    body = json.dumps({"error": "editors_only"})

    def test_a_json_error_parses_as_json(self):
        case = make_case("c-0001", expect={"http": {"status": 403}})
        rc, _, proc = self.invoke(self.sse_plan([case]))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        response = self.read("cases", "c-0001", "response.json")
        self.assertEqual(response["body"], {"error": "editors_only"})
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["layers"]["http"]["verdict"], "pass")


class TestJsonToolCalls(SSERunnerCase):
    """traces.source: response on a plain JSON body (#39 option 1)."""

    content_type = "application/json"
    body = json.dumps({"reply": "ok", "debug": {"calls": [
        {"name": "list_invoices", "arguments": {"unit_id": "U-1"}}]}})

    def test_a_declared_list_in_a_json_body(self):
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["list_invoices"]}
        plan = self.sse_plan([case])
        invocation = plan["adapter"]["invocation"]
        del invocation["response_format"], invocation["sse"]
        invocation["response_body"] = '{"reply": "<answer>"}'
        plan["adapter"]["traces"] = {"source": "response",
                                     "tool_calls_path": "debug.calls"}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layers = self.read("cases", "c-0001", "verdict.json")["layers"]
        self.assertEqual(layers["trajectory"]["verdict"], "pass")
        self.assertEqual(layers["answer"]["verdict"], "pass")

    def test_a_missing_list_is_infra_incomplete(self):
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["list_invoices"]}
        plan = self.sse_plan([case])
        invocation = plan["adapter"]["invocation"]
        del invocation["response_format"], invocation["sse"]
        invocation["response_body"] = '{"reply": "<answer>"}'
        plan["adapter"]["traces"] = {"source": "response",
                                     "tool_calls_path": "nowhere"}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layer = self.read("cases", "c-0001",
                          "verdict.json")["layers"]["trajectory"]
        self.assertEqual(layer["verdict"], "infra_incomplete")
        self.assertIn("nowhere", layer["reason"])


class TestPreflight(SSERunnerCase):
    def refused(self, mutate):
        plan = self.sse_plan(traces=False)
        mutate(plan["adapter"]["invocation"])
        rc, payload, proc = self.invoke(plan)
        self.assertEqual(rc, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.raw.server.calls, [])
        return payload["error"]

    def test_half_declared_and_malformed_blocks_exit_3(self):
        self.assertIn("response_format is not sse", self.refused(
            lambda inv: inv.pop("response_format")))
        self.assertIn("must be json or sse", self.refused(
            lambda inv: inv.update(response_format="ndjson")))
        self.assertIn("protocol must be one of", self.refused(
            lambda inv: inv.update(sse={"protocol": "openai"})))


if __name__ == "__main__":
    unittest.main()
