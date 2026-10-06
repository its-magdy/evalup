#!/usr/bin/env python3
"""Tests for scripted multi-turn conversations (docs/multi-turn.md).

The runner is driven as a real subprocess against a real HTTP server, as in
test_run_cases.py, whose fixtures this module borrows. Each stub app below is
one way a real app keeps -- or loses -- a conversation (SS11's list): one that
forgets history, one that stores a turn and then times out, one that reuses a
trace id, one that sets a session cookie under cookie auth, one that returns
the session id in a header.

Run: python3 -m unittest discover -s tests -v   (from the plugin root)
"""
import json
import pathlib
import sys
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_run_cases import (  # noqa: E402 - the shared fixtures
    SCRIPTS,
    FakeApp,
    RunnerCase,
    make_case,
    make_plan,
    otlp,
    span,
)

sys.path.insert(0, str(SCRIPTS))
import run_cases  # noqa: E402


def conversation_case(case_id="c-convo-01", turns=None, **overrides):
    """make_case as a two-turn conversation: the final turn's checks are the
    top-level expect, as case-format.md says."""
    case = make_case(case_id, **overrides)
    case["input"] = {"turns": turns if turns is not None else [
        {"user": "show me the licences", "expect": {"http": {"status": 200}}},
        {"user": "only the expired ones"}]}
    return case


def with_conversation(plan, **block):
    plan["adapter"]["invocation"]["conversation"] = block
    return plan


class TestConversationGate(RunnerCase):
    """SS2: invocation.conversation is the only switch."""

    def test_undeclared_conversation_is_skipped(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[conversation_case()])
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "skipped")
        self.assertIn("invocation.conversation",
                      verdict["layers"]["http"]["reason"])
        self.assertEqual([c for c in self.app.calls if c["path"] != "/"], [])

    def test_a_session_block_turns_nothing_on(self):
        """Hard rule 3's history: adapters/dotnet.md shipped a `session`
        block, and declaring one must never be enough to drive turns."""
        plan = make_plan(self.state, self.app.base_url,
                         cases=[conversation_case()])
        plan["adapter"]["invocation"]["session"] = {
            "start": {"via": "POST /chat/session"}}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "skipped")
        self.assertEqual([c for c in self.app.calls if c["path"] != "/"], [])

    def test_manifest_records_the_block(self):
        plan = with_conversation(
            make_plan(self.state, self.app.base_url),
            style="client-id", memory="user")
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.read("manifest.yaml")["conversation"],
                         {"style": "client-id", "session_from": None,
                          "turn_delay_s": 0, "memory": "user"})
        plan2 = make_plan(self.state, self.app.base_url,
                          run_id="smoke-20260908T120001Z")
        rc, _, proc = self.invoke(plan2)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertIsNone(self.read_run("smoke-20260908T120001Z",
                                        "manifest.yaml")["conversation"])

    def read_run(self, run_id, *parts):
        import json
        return json.loads(self.out_dir(run_id).joinpath(*parts)
                          .read_text(encoding="utf-8"))


class TestConversationPreflight(RunnerCase):
    """A malformed block is exit 3 before any spend, like a malformed body
    template: finding out per conversation would bill turn 1 of every case."""

    def assert_preflight(self, fragment, request_body=None, mode=None,
                         **block):
        plan = with_conversation(make_plan(self.state, self.app.base_url),
                                 **block)
        if request_body is not None:
            plan["adapter"]["invocation"]["request_body"] = request_body
        rc, payload, proc = self.invoke(plan)
        self.assertEqual(rc, 3, proc.stdout + proc.stderr)
        self.assertIn(fragment, payload["error"])
        self.assertEqual(self.app.calls, [])

    def test_bad_style(self):
        self.assert_preflight("style must be one of", style="sticky")

    def test_unknown_key(self):
        self.assert_preflight("unknown key(s)", style="client-id",
                              sesion_from={"body": "id"})

    def test_server_id_needs_exactly_one_source(self):
        body = '{"conversationId": "<session>", "message": "<user turn>"}'
        for source in (None, {}, {"body": "id", "header": "X-Id"},
                       {"cookie": "sid"}, {"body": ""}):
            with self.subTest(source=source):
                self.assert_preflight("exactly one of body", style="server-id",
                                      session_from=source, request_body=body)

    def test_server_id_needs_the_placeholder(self):
        self.assert_preflight("no <session> placeholder", style="server-id",
                              session_from={"body": "conversationId"})

    def test_session_from_is_server_id_only(self):
        self.assert_preflight("server-id only", style="client-id",
                              session_from={"body": "id"})

    def test_client_id_needs_uuid(self):
        self.assert_preflight("no <uuid> placeholder", style="client-id",
                              request_body='{"message": "<user turn>"}')

    def test_delay_and_memory(self):
        self.assert_preflight("turn_delay_s must be", style="client-id",
                              turn_delay_s=-1)
        self.assert_preflight("memory must be one of", style="client-id",
                              memory="forever")


class TestSessionPlaceholder(unittest.TestCase):
    """SS2: an unset <session> is OMITTED, not sent as null."""

    template = {"conversationId": "<session>", "message": "<user turn>",
                "note": "sid=<session>", "list": ["<session>"]}

    def test_unset_session_drops_the_key(self):
        body = run_cases.render_template(
            self.template, {"<user turn>": "hi", "<session>": None})
        self.assertNotIn("conversationId", body)
        self.assertEqual(body["note"], "sid=")
        self.assertEqual(body["message"], "hi")

    def test_set_session_is_rendered(self):
        body = run_cases.render_template(
            self.template, {"<user turn>": "hi", "<session>": "abc"})
        self.assertEqual(body["conversationId"], "abc")
        self.assertEqual(body["note"], "sid=abc")
        self.assertEqual(body["list"], ["abc"])


# -- stub apps (SS11) -------------------------------------------------------

class LicenceApp:
    """A chat app that keeps a conversation per session id, or forgets it.

    Turn 1 asks for the Cairo team's licences; turn 2 says "only the expired
    ones", which only an app that remembers turn 1 can answer. A message it
    sees twice in one session (a re-sent turn) gets "you already asked" --
    the corrupted history a whole-conversation restart exists to avoid."""

    def __init__(self, forget=False, session_key="sessionId"):
        self.forget = forget
        self.session_key = session_key
        self.history = {}
        self.lock = threading.Lock()

    def reply(self, sid, message):
        with self.lock:
            seen = self.history.setdefault(sid, [])
            repeated = message in seen
            seen.append(message)
            remembers = not self.forget and len(seen) > 1
        if repeated:
            return "you already asked that"
        if "licences" in message:
            return "Cairo team licences: A (expired), B (active)"
        if "expired" in message:
            return ("Cairo team expired licences: A" if remembers
                    else "which team do you mean?")
        return "hello"

    def __call__(self, path, body, headers):
        if path == "/":
            return 200, {"message": "up"}, {}
        return 200, {"message": self.reply(body.get(self.session_key),
                                           body.get("message", ""))}, {}


def licence_case(case_id="c-convo-01", **overrides):
    case = make_case(case_id, **overrides)
    case["input"] = {"turns": [
        {"user": "show me the licences for the Cairo team",
         "expect": {"answer": {"must_contain": ["Cairo"]}}},
        {"user": "only the expired ones"}]}
    case["expect"] = {"answer": {"must_contain": ["expired"],
                                 "must_not_contain": ["which team"]}}
    return case


class ConversationCase(RunnerCase):
    """RunnerCase with a stub app of the test's choosing."""

    def start(self, handler):
        app = FakeApp(handler)
        self.addCleanup(app.close)
        return app

    def asks(self, app):
        return [c for c in app.calls if c["path"] != "/"]

    def plan_for(self, app, cases, **block):
        plan = make_plan(self.state, app.base_url, cases=cases)
        return with_conversation(plan, **(block or {"style": "client-id"}))

    def run_ok(self, plan):
        rc, payload, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        return payload


class TestConversationDriver(ConversationCase):
    """SS3: live scripted turns in one conversation."""

    def test_an_app_that_remembers_passes(self):
        app = self.start(LicenceApp())
        self.run_ok(self.plan_for(app, [licence_case()]))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "pass")
        self.assertEqual((verdict["multi_turn"], verdict["turns_sent"],
                          verdict["failed_turn"]), (True, 2, None))
        # The union, keyed t<n>.<layer> (SS3).
        self.assertEqual(verdict["layers"]["t1.answer"]["verdict"], "pass")
        self.assertEqual(verdict["layers"]["t2.answer"]["verdict"], "pass")
        self.assertNotIn("answer", verdict["layers"])
        # client-id: one <uuid> for the whole conversation.
        sids = [c["body"]["sessionId"] for c in self.asks(app)]
        self.assertEqual(len(sids), 2)
        self.assertEqual(sids[0], sids[1])
        # The case-level files are the deciding (last) turn's.
        self.assertEqual((self.out_dir() / "cases" / "c-convo-01"
                          / "answer.txt").read_text(encoding="utf-8"),
                         "Cairo team expired licences: A")
        self.assertEqual(self.read("cases", "c-convo-01", "expect.json"),
                         licence_case()["expect"])
        self.assertEqual(self.read("cases", "c-convo-01",
                                   "request.json")["turn"], 2)

    def test_an_app_that_forgets_fails_at_turn_two(self):
        """The bug class this feature exists to catch."""
        app = self.start(LicenceApp(forget=True))
        self.run_ok(self.plan_for(app, [licence_case()]))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "fail")
        self.assertEqual((verdict["turns_sent"], verdict["failed_turn"]),
                         (2, 2))
        self.assertIn("turn 2", verdict["stop_reason"])
        self.assertEqual(self.jsonl("verdicts_for_stats.jsonl"),
                         [{"case_id": "c-convo-01", "verdict": "fail"}])

    def test_a_failed_checkpoint_stops_the_conversation(self):
        """Anything sent after a fail is built on a derailed conversation."""
        def refuse_turn_one(path, body, headers):
            if path == "/":
                return 200, {"message": "up"}, {}
            return 400, {"message": "no"}, {}
        app = self.start(refuse_turn_one)
        case = licence_case()
        case["input"]["turns"][0]["expect"] = {"http": {"status": 200}}
        self.run_ok(self.plan_for(app, [case]))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "fail")
        self.assertEqual((verdict["turns_sent"], verdict["failed_turn"]),
                         (1, 1))
        self.assertEqual(len(self.asks(app)), 1)
        self.assertIn("HTTP 400", verdict["stop_reason"])
        self.assertNotIn("t2.http", verdict["layers"])

    def test_an_unscored_checkpoint_does_not_stop_it(self):
        """`unscored` is the harness unable to look -- a trace-less adapter
        with no route source -- not the app failing. Stopping there would
        never send a final turn that CAN be scored."""
        app = self.start(LicenceApp())
        case = licence_case()
        case["input"]["turns"][0]["expect"] = {"route": "licences"}
        self.run_ok(self.plan_for(app, [case]))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["layers"]["t1.routing"]["verdict"],
                         "unscored")
        self.assertEqual(len(self.asks(app)), 2)
        self.assertEqual(verdict["verdict"], "pass")

    def test_every_turn_checks_a_turn_with_no_expect_of_its_own(self):
        app = self.start(LicenceApp())
        case = licence_case(every_turn={"answer": {
            "must_not_contain": ["(expired)"]}})
        del case["input"]["turns"][0]["expect"]
        self.run_ok(self.plan_for(app, [case]))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual((verdict["verdict"], verdict["failed_turn"]),
                         ("fail", 1))

    def test_repeats_rerun_the_whole_conversation(self):
        app = self.start(LicenceApp())
        plan = self.plan_for(app, [licence_case()])
        plan["k"] = 2
        self.run_ok(plan)
        sids = [c["body"]["sessionId"] for c in self.asks(app)]
        self.assertEqual(len(sids), 4)
        self.assertEqual(len(set(sids)), 2)
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual([(r["verdict"], r["turns_sent"])
                          for r in verdict["repeats"]],
                         [("pass", 2), ("pass", 2)])

    def test_latency_is_the_sum_of_the_turns(self):
        app = self.start(LicenceApp())
        self.run_ok(self.plan_for(app, [licence_case()]))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertIsInstance(verdict["latency_s"], float)
        self.assertGreater(verdict["latency_s"], 0)

    def test_max_turns_skips_a_longer_case(self):
        app = self.start(LicenceApp())
        case = licence_case()
        case["input"]["turns"].insert(0, {"user": "hello"})
        plan = self.plan_for(app, [case])
        plan["execution"]["max_turns"] = 2
        self.run_ok(plan)
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "skipped")
        self.assertIn("execution.max_turns 2",
                      verdict["layers"]["http"]["reason"])
        self.assertEqual(self.asks(app), [])

    def test_max_turns_is_validated(self):
        plan = make_plan(self.state, self.app.base_url)
        plan["execution"]["max_turns"] = 1
        rc, payload, _ = self.invoke(plan)
        self.assertEqual(rc, 2)
        self.assertIn("execution.max_turns", payload["error"])


class TestRestartOnRetry(ConversationCase):
    """SS3: a retryable outcome on any turn restarts the conversation."""

    def test_a_turn_stored_then_timed_out_restarts_from_turn_one(self):
        """The app stores turn 2 and then times out. Re-sending turn 2 into
        that session would show it twice ("you already asked that"); a
        restart opens a fresh session and the conversation passes."""
        licences = LicenceApp()
        first = {"sid": None}

        def slow_once(path, body, headers):
            status, payload, extra = licences(path, body, headers)
            if path != "/" and "expired" in body.get("message", "") \
                    and first["sid"] in (None, body["sessionId"]):
                first["sid"] = body["sessionId"]
                time.sleep(1.5)
            return status, payload, extra
        app = self.start(slow_once)
        plan = self.plan_for(app, [licence_case()])
        plan["execution"]["timeout_s"] = 1
        self.run_ok(plan)
        sids = [c["body"]["sessionId"] for c in self.asks(app)]
        self.assertEqual(len(sids), 4)
        self.assertEqual(sids[0], sids[1])
        self.assertEqual(sids[2], sids[3])
        self.assertNotEqual(sids[0], sids[2])
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "pass")
        self.assertEqual(self.read("cases", "c-convo-01",
                                   "response.json")["attempts"], 2)

    def test_the_same_5xx_at_the_same_turn_on_every_attempt(self):
        def boom_at_two(path, body, headers):
            if path == "/":
                return 200, {"message": "up"}, {}
            if "expired" in body.get("message", ""):
                return 500, {"error": "boom"}, {}
            return 200, {"message": "Cairo team licences"}, {}
        app = self.start(boom_at_two)
        self.run_ok(self.plan_for(app, [licence_case()]))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "infra_error")
        self.assertEqual(verdict["repeated_5xx"],
                         {"status": 500, "attempts": 2, "turn": 2})
        self.assertEqual(verdict["failed_turn"], 2)
        # Turn 1 is replayed on every attempt: the cost the spec states.
        self.assertEqual(len(self.asks(app)), 4)
        summary = self.read("results.json")["summary"]
        self.assertEqual(summary["repeated_5xx"], 1)

    def test_a_429_on_turn_one_restarts_and_passes(self):
        licences = LicenceApp()
        calls = {"n": 0}

        def throttle_once(path, body, headers):
            if path != "/":
                calls["n"] += 1
                if calls["n"] == 1:
                    return 429, {"error": "slow down"}, {}
            return licences(path, body, headers)
        app = self.start(throttle_once)
        self.run_ok(self.plan_for(app, [licence_case()]))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "pass")
        self.assertIsNone(verdict["repeated_5xx"])


class TestServerId(ConversationCase):
    """SS2: the app mints the id on turn 1; <session> carries it after."""

    BODY = '{"conversationId": "<session>", "message": "<user turn>"}'

    def server_app(self, mint_in="body", header="X-Conversation-ID"):
        licences = LicenceApp(session_key="conversationId")

        def handler(path, body, headers):
            if path == "/":
                return 200, {"message": "up"}, {}
            if body.get("conversationId") is None:
                body = dict(body, conversationId="conv-1")
                _, payload, _ = licences(path, body, headers)
                if mint_in == "body":
                    return 200, dict(payload, conversationId="conv-1"), {}
                if mint_in == "header":
                    return 200, payload, {header: "conv-1"}
                return 200, payload, {}
            return licences(path, body, headers)
        return self.start(handler)

    def server_plan(self, app, source):
        plan = self.plan_for(app, [licence_case()], style="server-id",
                             session_from=source)
        plan["adapter"]["invocation"]["request_body"] = self.BODY
        return plan

    def test_from_the_body_and_omitted_on_turn_one(self):
        app = self.server_app("body")
        self.run_ok(self.server_plan(app, {"body": "conversationId"}))
        bodies = [c["body"] for c in self.asks(app)]
        # Omitted, not null: an app binding a non-nullable id 400s on null.
        self.assertNotIn("conversationId", bodies[0])
        self.assertEqual(bodies[1]["conversationId"], "conv-1")
        self.assertEqual(self.read("cases", "c-convo-01",
                                   "verdict.json")["verdict"], "pass")

    def test_from_a_header_case_insensitively(self):
        app = self.server_app("header")
        self.run_ok(self.server_plan(app, {"header": "x-conversation-id"}))
        self.assertEqual(self.asks(app)[1]["body"]["conversationId"],
                         "conv-1")
        self.assertEqual(self.read("cases", "c-convo-01",
                                   "verdict.json")["verdict"], "pass")

    def test_no_session_on_turn_one_is_unscored_and_turn_two_unsent(self):
        app = self.server_app("nowhere")
        self.run_ok(self.server_plan(app, {"body": "conversationId"}))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "unscored")
        self.assertEqual(len(self.asks(app)), 1)
        self.assertIn("session_from.body", verdict["stop_reason"])
        self.assertIsNone(verdict["failed_turn"])


class TestSharedTraceId(ConversationCase):
    """SS5: two turns returning one trace id cannot be attributed."""

    TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
    OTHER = "5bf92f3577b34da6a3ce929d0e0e4737"

    def traced(self, ids):
        spans = self.tmp / "spans.json"
        spans.write_text(json.dumps(otlp([
            span(tid, f"aaaaaaaaaaaaaa{i}{n}", "execute_tool", "list_licences")
            for i, tid in enumerate((self.TRACE, self.OTHER))
            for n in range(1)])), encoding="utf-8")
        licences = LicenceApp()
        sequence = iter(ids)

        def handler(path, body, headers):
            status, payload, extra = licences(path, body, headers)
            tid = self.TRACE if path == "/" else next(sequence)
            return status, dict(payload, traceId=tid), extra
        app = self.start(handler)
        case = licence_case()
        case["expect"]["tools"] = {"subset": ["list_licences"]}
        plan = self.plan_for(app, [case])
        plan["adapter"]["traces"] = {
            "source": "otlp-file", "convention": "gen_ai",
            "correlation": "response-field:traceId", "location": str(spans),
            "completeness": {"quiescence_ms": 10, "max_wait_s": 2}}
        plan["adapter"]["invocation"]["health_check"] = {
            "method": "GET", "path": "/", "expect_status": [200]}
        plan["capability_matrix"]["trajectory"] = {"enabled": True}
        return plan

    def test_a_reused_trace_id_makes_every_trace_layer_unscorable(self):
        self.run_ok(self.traced([self.TRACE, self.TRACE]))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        layers = verdict["layers"]
        for name in ("t1.loops", "t2.loops", "t2.trajectory"):
            self.assertEqual(layers[name]["verdict"], "unscorable", name)
            self.assertIn("same trace id", layers[name]["blocked_by"])
        self.assertEqual(layers["t1.trajectory"]["verdict"], "n/a")
        # The answer still scores: honest degradation, not a lost case.
        self.assertEqual(verdict["verdict"], "pass")
        self.assertFalse(verdict["trace"]["collected"])

    def test_distinct_trace_ids_score_per_turn(self):
        self.run_ok(self.traced([self.TRACE, self.OTHER]))
        layers = self.read("cases", "c-convo-01", "verdict.json")["layers"]
        self.assertEqual(layers["t2.trajectory"]["verdict"], "pass")
        self.assertEqual(layers["t1.loops"]["verdict"], "pass")


class TestFunctionModeConversation(ConversationCase):
    def test_client_id_in_function_mode(self):
        (self.tmp / "convo_entry.py").write_text(
            "HISTORY = {}\n"
            "def handle(request):\n"
            "    seen = HISTORY.setdefault(request['sid'], [])\n"
            "    seen.append(request['message'])\n"
            "    if 'expired' in request['message'] and len(seen) > 1:\n"
            "        return 'Cairo team expired licences: A'\n"
            "    if 'expired' in request['message']:\n"
            "        return 'which team do you mean?'\n"
            "    return 'Cairo team licences'\n", encoding="utf-8")
        plan = make_plan(self.state, self.app.base_url, cases=[licence_case()])
        plan["adapter"]["app"] = {"name": "fake", "repo": str(self.tmp)}
        plan["adapter"]["invocation"] = {
            "mode": "function", "entrypoint": "convo_entry:handle",
            "request_body": '{"message": "<user turn>", "sid": "<uuid>"}',
            "conversation": {"style": "client-id"},
            "timeout_s": 5, "max_concurrency": 1}
        self.run_ok(plan)
        self.assertEqual(self.read("cases", "c-convo-01",
                                   "verdict.json")["verdict"], "pass")


class TestRollUpUnion(unittest.TestCase):
    """SS3: the rollup strips t<n>. before RUN_TRIGGERED, so http and loops
    still cannot carry a conversation to pass on their own."""

    def v(self, verdict):
        return {"verdict": verdict}

    def test_http_alone_on_every_turn_is_a_pass(self):
        self.assertEqual(run_cases.roll_up(
            {"t1.http": self.v("pass"), "t2.http": self.v("pass")}, None),
            "pass")

    def test_loops_and_http_do_not_carry_an_unscored_turn(self):
        self.assertEqual(run_cases.roll_up(
            {"t1.http": self.v("pass"), "t1.loops": self.v("pass"),
             "t2.http": self.v("pass"), "t2.answer": self.v("unscored")},
            None), "unscored")

    def test_a_checkpoint_fail_fails_the_union(self):
        self.assertEqual(run_cases.roll_up(
            {"t1.routing": self.v("fail"), "t2.answer": self.v("pass")},
            None), "fail")


if __name__ == "__main__":
    unittest.main()
