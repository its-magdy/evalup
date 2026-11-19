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
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_run_cases import (  # noqa: E402 - the shared fixtures
    RUNNER,
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


TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
OTHER = "5bf92f3577b34da6a3ce929d0e0e4737"


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

    def traced(self, ids):
        """A traced plan whose app returns the trace ids in `ids`, one per
        turn (the health check gets TRACE), over a store holding spans for
        TRACE and OTHER."""
        spans = self.tmp / "spans.json"
        spans.write_text(json.dumps(otlp([
            span(tid, f"aaaaaaaaaaaaaa{i}{n}", "execute_tool", "list_licences")
            for i, tid in enumerate((TRACE, OTHER))
            for n in range(1)])), encoding="utf-8")
        licences = LicenceApp()
        sequence = iter(ids)

        def handler(path, body, headers):
            status, payload, extra = licences(path, body, headers)
            tid = TRACE if path == "/" else next(sequence)
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

    @staticmethod
    def no_answer_on_turn_two(licences):
        """The app answers turn 2 with a 200 whose body lacks the declared
        <answer> field: the final claim cannot be scored."""
        def handler(path, body, headers):
            status, payload, extra = licences(path, body, headers)
            if "expired" in body.get("message", ""):
                return 200, {"error": "boom"}, {}
            return status, payload, extra
        return handler

    def test_a_checkpoint_cannot_certify_an_unscored_final_turn(self):
        """SS1: the final turn is the claim. A passing turn-1 checkpoint
        used to carry this case to `pass` (2026-10-06 review)."""
        app = self.start(self.no_answer_on_turn_two(LicenceApp()))
        self.run_ok(self.plan_for(app, [licence_case()]))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["layers"]["t1.answer"]["verdict"], "pass")
        self.assertEqual(verdict["verdict"], "unscored")
        self.assertEqual(self.read("cases", "c-convo-01", "turns", "2",
                                   "verdict.json")["verdict"], "unscored")
        self.assertIsNone(verdict["failed_turn"])

    def test_an_every_turn_copy_cannot_certify_it_either(self):
        app = self.start(self.no_answer_on_turn_two(LicenceApp()))
        case = licence_case(every_turn={"answer": {
            "must_not_contain": ["zzz"]}})
        del case["input"]["turns"][0]["expect"]
        case["expect"] = {}
        self.run_ok(self.plan_for(app, [case]))
        self.assertEqual(self.read("cases", "c-convo-01",
                                   "verdict.json")["verdict"], "unscored")

    def test_an_http_only_final_turn_beside_a_graded_checkpoint(self):
        """The final turn's own rollup is `pass` by rule 6 here, so "the
        final turn passed" alone would not have caught it."""
        app = self.start(LicenceApp(forget=True))
        case = licence_case()
        case["expect"] = {"http": {"status": 200}}
        self.run_ok(self.plan_for(app, [case]))
        self.assertEqual(self.read("cases", "c-convo-01",
                                   "verdict.json")["verdict"], "unscored")

    def test_a_liveness_only_conversation_still_passes(self):
        app = self.start(LicenceApp())
        case = licence_case()
        del case["input"]["turns"][0]["expect"]
        case["expect"] = {"http": {"status": 200}}
        self.run_ok(self.plan_for(app, [case]))
        self.assertEqual(self.read("cases", "c-convo-01",
                                   "verdict.json")["verdict"], "pass")

    def test_a_demoted_repeat_folds_to_unscored(self):
        licences = LicenceApp()
        calls = {"n": 0}

        def second_repeat_loses_answer(path, body, headers):
            status, payload, extra = licences(path, body, headers)
            if path != "/":
                calls["n"] += 1
                if calls["n"] == 4:
                    return 200, {"error": "boom"}, {}
            return status, payload, extra
        app = self.start(second_repeat_loses_answer)
        plan = self.plan_for(app, [licence_case()])
        plan["k"] = 2
        self.run_ok(plan)
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual([r["verdict"] for r in verdict["repeats"]],
                         ["pass", "unscored"])
        self.assertEqual(verdict["verdict"], "unscored")

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
        turns = [self.read("cases", "c-convo-01", "turns", turn,
                           "response.json")["latency_s"]
                 for turn in ("1", "2")]
        # Not `> 0`: each turn is rounded to the millisecond, and a stub on
        # localhost can answer both in under half of one (a CI flake,
        # 2026-10-09).
        self.assertIsInstance(verdict["latency_s"], float)
        self.assertEqual(verdict["latency_s"], round(sum(turns), 3))

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

    def test_a_claim_the_runner_would_drop_is_refused(self):
        """Each shape below is a validator ERROR; run unvalidated, the
        runner used to drop the claim and score an http-only pass on an app
        that forgets (2026-10-06 review)."""
        def final_turn_expect(case):
            case["input"]["turns"][1]["expect"] = case.pop("expect")

        def bare_string(case):
            case["input"]["turns"][0] = "show me the licences"

        def both(case):
            case["input"]["messages"] = [{"role": "user", "content": "x"}]
        for mutate in (final_turn_expect, bare_string, both):
            with self.subTest(mutate=mutate.__name__):
                shutil.rmtree(self.out_dir(), ignore_errors=True)
                app = self.start(LicenceApp(forget=True))
                case = licence_case()
                mutate(case)
                self.run_ok(self.plan_for(app, [case]))
                verdict = self.read("cases", "c-convo-01", "verdict.json")
                self.assertEqual(verdict["verdict"], "skipped")
                self.assertIn("malformed",
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

    def test_a_passing_checkpoint_does_not_rescue_a_missing_session(self):
        app = self.server_app("nowhere")
        plan = self.server_plan(app, {"body": "conversationId"})
        self.run_ok(plan)
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["layers"]["t1.answer"]["verdict"], "pass")
        self.assertEqual(verdict["verdict"], "unscored")

    def test_no_session_on_turn_one_is_unscored_and_turn_two_unsent(self):
        app = self.server_app("nowhere")
        self.run_ok(self.server_plan(app, {"body": "conversationId"}))
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "unscored")
        self.assertEqual(len(self.asks(app)), 1)
        self.assertIn("session_from.body", verdict["stop_reason"])
        self.assertIsNone(verdict["failed_turn"])


class MultiHeaders(dict):
    """Response headers that may repeat a name (two Set-Cookie lines).

    FakeApp sends `headers.items()`; a plain dict can hold one Set-Cookie,
    and the bug this exists to pin is the runner keeping only the last."""

    def __init__(self, pairs):
        super().__init__(pairs)
        self.pairs = list(pairs)

    def items(self):
        return iter(self.pairs)


class TestCookieStyle(ConversationCase):
    """SS2: a session cookie, under cookie AUTH, reaches turn 2."""

    def cookie_app(self, fail_turn_two_once=False):
        licences = LicenceApp()
        state = {"failed": False}

        def handler(path, body, headers):
            if path == "/":
                return 200, {"message": "up"}, {}
            cookies = dict(part.strip().split("=", 1) for part in
                           (headers.get("Cookie") or "").split(";")
                           if "=" in part)
            if cookies.get("auth") != "secret-auth":
                return 401, {"message": "no auth cookie"}, {}
            sid = cookies.get("sid")
            if sid is None:
                # Turn 1: a fresh session. The session cookie is the FIRST
                # of two Set-Cookie headers, so last-one-wins loses it.
                sid = f"s{len(licences.history) + 1}"
                _, payload, _ = licences(path, dict(body, sessionId=sid),
                                         headers)
                return 200, payload, MultiHeaders([
                    ("Set-Cookie", f"sid={sid}; Path=/"),
                    ("Set-Cookie", "theme=dark; Path=/")])
            if fail_turn_two_once and not state["failed"]:
                state["failed"] = True
                return 503, {"error": "busy"}, {}
            return licences(path, dict(body, sessionId=sid), headers)
        return self.start(handler)

    def cookie_plan(self, app):
        plan = self.plan_for(app, [licence_case()], style="cookie")
        plan["adapter"]["invocation"]["auth"] = {
            "type": "cookie", "cookie_env": "APP_COOKIE"}
        return plan

    def invoke_cookie(self, plan):
        rc, _, proc = self.invoke(plan, env={"APP_COOKIE": "auth=secret-auth"})
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)

    def test_session_cookie_and_auth_cookie_in_one_header(self):
        app = self.cookie_app()
        self.invoke_cookie(self.cookie_plan(app))
        asks = self.asks(app)
        self.assertEqual(len(asks), 2)
        self.assertEqual(asks[0]["headers"]["Cookie"], "auth=secret-auth")
        turn_two = asks[1]["headers"]["Cookie"]
        self.assertTrue(turn_two.startswith("auth=secret-auth; "), turn_two)
        self.assertIn("sid=s1", turn_two)
        self.assertIn("theme=dark", turn_two)
        self.assertEqual(self.read("cases", "c-convo-01",
                                   "verdict.json")["verdict"], "pass")
        # A session cookie is a credential: never on disk.
        sent = self.read("cases", "c-convo-01", "request.json")
        self.assertEqual(sent["headers_sent"]["Cookie"], "<redacted>")
        tree = "".join(path.read_text(encoding="utf-8", errors="replace")
                       for path in self.out_dir().rglob("*")
                       if path.is_file())
        self.assertNotIn("sid=s1", tree)
        self.assertNotIn("secret-auth", tree)

    def test_a_restart_gets_a_fresh_jar(self):
        app = self.cookie_app(fail_turn_two_once=True)
        self.invoke_cookie(self.cookie_plan(app))
        cookies = [c["headers"]["Cookie"] for c in self.asks(app)]
        self.assertEqual(len(cookies), 4)
        self.assertIn("sid=s1", cookies[1])
        # Attempt 2's turn 1 carries no cookie from the abandoned attempt.
        self.assertEqual(cookies[2], "auth=secret-auth")
        self.assertIn("sid=s2", cookies[3])
        self.assertEqual(self.read("cases", "c-convo-01",
                                   "verdict.json")["verdict"], "pass")

    def test_cookie_style_needs_http(self):
        plan = with_conversation(make_plan(self.state, self.app.base_url),
                                 style="cookie")
        (self.tmp / "cookie_entry.py").write_text(
            "def handle(request):\n    return 'hi'\n", encoding="utf-8")
        plan["adapter"]["app"] = {"name": "fake", "repo": str(self.tmp)}
        plan["adapter"]["invocation"].update(
            mode="function", entrypoint="cookie_entry:handle")
        rc, payload, _ = self.invoke(plan)
        self.assertEqual(rc, 3)
        self.assertIn("style: cookie needs invocation.mode: http",
                      payload["error"])


class TestSharedTraceId(ConversationCase):
    """SS5: two turns returning one trace id cannot be attributed."""

    def test_a_reused_trace_id_makes_every_trace_layer_unscorable(self):
        self.run_ok(self.traced([TRACE, TRACE]))
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
        self.run_ok(self.traced([TRACE, OTHER]))
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


class TestTurnTree(ConversationCase):
    """SS7: every sent turn on disk, and --verify holding the tree to it."""

    def verify(self):
        proc = subprocess.run([sys.executable, str(RUNNER), "--verify",
                               str(self.out_dir())],
                              capture_output=True, text=True)
        return proc.returncode, json.loads(proc.stdout)

    def run_licences(self, k=1, cases=None):
        app = self.start(LicenceApp())
        plan = self.plan_for(app, cases or [licence_case()])
        plan["k"] = k
        self.run_ok(plan)
        return self.out_dir() / "cases" / "c-convo-01"

    def test_every_sent_turn_has_its_five_files(self):
        case_dir = self.run_licences()
        for turn in ("1", "2"):
            for name in run_cases.TURN_FILES:
                self.assertTrue((case_dir / "turns" / turn / name).is_file(),
                                f"turns/{turn}/{name}")
        turn_one = json.loads((case_dir / "turns" / "1" / "verdict.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual((turn_one["turn"], turn_one["verdict"]), (1, "pass"))
        self.assertEqual(
            json.loads((case_dir / "turns" / "1" / "expect.json")
                       .read_text(encoding="utf-8")),
            {"answer": {"must_contain": ["Cairo"]}})
        self.assertEqual(self.verify()[0], 0)

    def test_verify_names_a_missing_turn_file(self):
        case_dir = self.run_licences()
        (case_dir / "turns" / "2" / "answer.txt").unlink()
        rc, payload = self.verify()
        self.assertEqual(rc, 6)
        self.assertIn("cases/c-convo-01/turns/2/answer.txt (the case is "
                      "multi-turn)", payload["missing_artifacts"])

    def test_verify_counts_turns_against_turns_sent(self):
        case_dir = self.run_licences()
        shutil.rmtree(case_dir / "turns" / "2")
        rc, payload = self.verify()
        self.assertEqual(rc, 6)
        self.assertTrue(any("expected turns 1..2" in m
                            for m in payload["missing_artifacts"]),
                        payload)
        shutil.copytree(case_dir / "turns" / "1", case_dir / "turns" / "2")
        shutil.copytree(case_dir / "turns" / "1", case_dir / "turns" / "3")
        self.assertEqual(self.verify()[0], 6)

    def test_a_stray_file_is_not_a_turn(self):
        case_dir = self.run_licences()
        (case_dir / "turns" / ".DS_Store").write_text("x", encoding="utf-8")
        self.assertEqual(self.verify()[0], 0)

    def test_a_single_turn_case_has_no_turns(self):
        plan = make_plan(self.state, self.app.base_url)
        self.run_ok(plan)
        case_dir = self.out_dir() / "cases" / "c-0001"
        self.assertFalse((case_dir / "turns").exists())
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual((verdict["multi_turn"], verdict["turns_sent"]),
                         (False, 1))
        self.assertEqual(self.verify()[0], 0)
        (case_dir / "turns" / "1").mkdir(parents=True)
        rc, payload = self.verify()
        self.assertEqual(rc, 6)
        self.assertTrue(any("a single-turn case has no turns/" in m
                            for m in payload["missing_artifacts"]))

    def test_a_skipped_conversation_sent_no_turn(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[licence_case()])
        self.run_ok(plan)
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual((verdict["verdict"], verdict["multi_turn"],
                          verdict["turns_sent"]), ("skipped", True, 0))
        self.assertEqual(self.verify()[0], 0)

    def test_repeats_hold_their_turns(self):
        case_dir = self.run_licences(k=2)
        for n in ("1", "2"):
            for turn in ("1", "2"):
                for name in run_cases.TURN_FILES:
                    self.assertTrue(
                        (case_dir / "repeats" / n / "turns" / turn / name)
                        .is_file(), f"repeats/{n}/turns/{turn}/{name}")
        self.assertEqual(self.verify()[0], 0)
        (case_dir / "repeats" / "2" / "turns" / "2" / "request.json").unlink()
        rc, payload = self.verify()
        self.assertEqual(rc, 6)
        self.assertIn("cases/c-convo-01/repeats/2/turns/2/request.json (the "
                      "case is multi-turn)", payload["missing_artifacts"])

    def test_a_turns_trace_is_required_when_collected(self):
        self.run_ok(self.traced([TRACE, OTHER]))
        case_dir = self.out_dir() / "cases" / "c-convo-01"
        for turn in ("1", "2"):
            for name in run_cases.TURN_TRACE_FILES:
                self.assertTrue((case_dir / "turns" / turn / name).is_file())
        (case_dir / "turns" / "1" / "trace.json").unlink()
        rc, payload = self.verify()
        self.assertEqual(rc, 6)
        self.assertIn("cases/c-convo-01/turns/1/trace.json (a trace was "
                      "collected for this turn)", payload["missing_artifacts"])


class TestRoutingStaysSingleTurn(ConversationCase):
    """SS4: the run-level routing report keeps meaning single-turn routing,
    and its file is required only by a single-turn routing row. The two
    changes together, or every run with a conversation ends incomplete."""

    def routed_app(self):
        licences = LicenceApp()

        def handler(path, body, headers):
            status, payload, extra = licences(path, body, headers)
            return status, dict(payload, route="licences"), extra
        return self.start(handler)

    def routed_conversation(self):
        case = licence_case()
        case["expect"]["route"] = "licences"
        return case

    def plan_routed(self, app, cases):
        plan = self.plan_for(app, cases)
        plan["adapter"]["invocation"]["route_from_response"] = "route"
        return plan

    def test_a_conversation_alone_requires_no_routing_file(self):
        app = self.routed_app()
        plain = make_case("c-plain", expect={"answer": {
            "must_contain": ["hello"]}})
        plain["input"] = {"messages": [{"role": "user", "content": "hi"}]}
        rc, payload, proc = self.invoke(self.plan_routed(
            app, [plain, self.routed_conversation()]))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["layers"]["t2.routing"]["verdict"], "pass")
        self.assertFalse((self.out_dir() / "routing_results.jsonl").exists())

    def test_only_single_turn_rows_reach_the_report(self):
        app = self.routed_app()
        routed = make_case("c-routed", expect={"route": "licences"})
        self.run_ok(self.plan_routed(app, [routed,
                                           self.routed_conversation()]))
        self.assertEqual([row["case_id"] for row in
                          self.jsonl("routing_results.jsonl")], ["c-routed"])


class TestOneConversationOneRow(ConversationCase):
    """SS4: verdicts.jsonl, results.json and the gate see one row per
    conversation; the row says how far it got; the summary keeps the
    conversations' own rate beside the blend."""

    def mixed_run(self, forget=True, memory=None):
        app = self.start(LicenceApp(forget=forget))
        plain = make_case("c-plain", expect={"answer": {
            "must_contain": ["hello"]}})
        block = {"style": "client-id"}
        if memory:
            block["memory"] = memory
        self.run_ok(self.plan_for(app, [plain, licence_case()], **block))

    def test_rows_carry_the_conversation_fields(self):
        self.mixed_run()
        for rows in (self.jsonl("verdicts.jsonl"),
                     self.read("results.json")["cases"]):
            by_id = {row["case_id"]: row for row in rows}
            self.assertEqual(
                {k: by_id["c-convo-01"][k] for k in
                 ("multi_turn", "turns_sent", "failed_turn")},
                {"multi_turn": True, "turns_sent": 2, "failed_turn": 2})
            self.assertEqual(
                {k: by_id["c-plain"][k] for k in
                 ("multi_turn", "turns_sent", "failed_turn")},
                {"multi_turn": False, "turns_sent": 1, "failed_turn": None})
        # stats.py's input is unchanged: one {case_id, verdict} per case.
        self.assertEqual(
            sorted(self.jsonl("verdicts_for_stats.jsonl"),
                   key=lambda r: r["case_id"]),
            [{"case_id": "c-convo-01", "verdict": "fail"},
             {"case_id": "c-plain", "verdict": "pass"}])

    def test_summary_splits_the_conversations_out(self):
        self.mixed_run()
        summary = self.read("results.json")["summary"]
        self.assertEqual(summary["multi_turn"],
                         {"n": 1, "passes": 0, "failures": 1,
                          "gating_failures": 1})
        self.assertEqual((summary["n"], summary["passes"],
                          summary["failures"]), (2, 1, 1))

    def test_a_single_turn_run_has_no_split(self):
        self.run_ok(make_plan(self.state, self.app.base_url))
        self.assertIsNone(self.read("results.json")["summary"]["multi_turn"])
        self.assertIsNone(self.read("results.json")["memory"])

    def test_verify_recounts_the_split(self):
        self.mixed_run()
        path = self.out_dir() / "results.json"
        results = json.loads(path.read_text(encoding="utf-8"))
        results["summary"]["multi_turn"]["failures"] = 0
        path.write_text(json.dumps(results), encoding="utf-8")
        proc = subprocess.run([sys.executable, str(RUNNER), "--verify",
                               str(self.out_dir())],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 6)
        self.assertIn("summary.multi_turn", proc.stdout)

    def gate(self):
        proc = subprocess.run([sys.executable, str(SCRIPTS / "gate.py"),
                               str(self.out_dir())],
                              capture_output=True, text=True)
        return proc.stdout + proc.stderr

    def test_gate_prints_both_rates(self):
        self.mixed_run()
        text = self.gate()
        self.assertIn("multi-turn: 0/1 pass (1 conversation(s)); "
                      "single-turn: 1/1 pass", text)
        self.assertNotIn("memory: user", text)

    def test_user_memory_is_said(self):
        """SS6: memory that outlives the session can contaminate ANY case,
        and no per-case verdict can see it -- so the run says it."""
        self.mixed_run(forget=False, memory="user")
        self.assertEqual(self.read("results.json")["memory"], "user")
        self.assertIn("memory: user", self.gate())


class TestDryRunCountsTurns(ConversationCase):
    """SS5: app calls are turns, not cases, and a wait floor is stated."""

    def test_calls_and_floor(self):
        case = licence_case()
        case["input"]["turns"].append({"user": "and the active ones?"})
        plan = self.plan_for(self.app, [case, make_case("c-plain")],
                             style="client-id", turn_delay_s=2)
        plan["k"] = 2
        rc, payload, proc = self.invoke(plan, extra_argv=["--dry-run"])
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(payload["app_calls_planned"], 2 * 3 + 2 * 1)
        # Trace-less: no quiescence; two gaps of 2 s per repeat.
        self.assertEqual(payload["wait_floor_s"], 2 * 2 * 2)
        convo = next(c for c in payload["cases"] if c["id"] == "c-convo-01")
        self.assertEqual(convo["turns"], 3)


class TestCostOfAConversation(unittest.TestCase):
    """SS4/SS5 in score_cost.py: a conversation is priced as one case, the
    sum of its turns; a pair that sent different numbers of turns is
    refused and named."""

    PRICES = {"schema": "evalup/price-table/1", "as_of": "2026-10-06",
              "currency": "USD", "source": "test",
              "models": {"m-1": {"input_per_mtok": "1.00",
                                 "output_per_mtok": "1.00"}}}

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_json(self, path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")

    @staticmethod
    def trajectory(n_in, n_out):
        return {"status": "ok", "trajectory": {
            "trace_id": "t", "agents": [], "tool_calls": [],
            "llm_calls": [{"model": "m-1", "duration_ms": 1,
                           "usage": {"input_tokens": n_in,
                                     "output_tokens": n_out}}],
            "usage": {"input_tokens": n_in, "output_tokens": n_out}},
            "checks": {}}

    def write_run(self, run_id, turn_usage, latency, verdict="pass",
                  drop_turn=None):
        """One run with a single conversation, c-convo, that sent
        len(turn_usage) turns."""
        out = self.tmp / run_id
        self.write_json(out / "manifest.yaml", {
            "run_id": run_id, "mode": "smoke", "k": 1,
            "selecting_split": "smoke", "harness_version": "0.1.0",
            "dataset_version": 1})
        case_dir = out / "cases" / "c-convo"
        for turn, (n_in, n_out) in enumerate(turn_usage, 1):
            if turn != drop_turn:
                self.write_json(case_dir / "turns" / str(turn)
                                / "trajectory.json",
                                self.trajectory(n_in, n_out))
        # The case-level file is the deciding turn's only.
        self.write_json(case_dir / "trajectory.json",
                        self.trajectory(*turn_usage[-1]))
        rows = [{"case_id": "c-convo", "verdict": verdict, "gating": True,
                 "layers": {}, "latency_s": latency, "multi_turn": True,
                 "turns_sent": len(turn_usage), "failed_turn": None},
                {"case_id": "c-plain", "verdict": "pass", "gating": True,
                 "layers": {}, "latency_s": 1.0, "multi_turn": False,
                 "turns_sent": 1, "failed_turn": None}]
        self.write_json(out / "results.json", {
            "run_id": run_id, "harness_version": "0.1.0",
            "dataset_version": 1, "mode": "smoke", "cases": rows,
            "summary": {"status": "ok", "n": 2, "passes": 2,
                        "holdout": None}, "exit_code": 0})
        return out

    def cost(self, *argv):
        prices = self.tmp / "prices.json"
        self.write_json(prices, self.PRICES)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "score_cost.py"),
             *[str(a) for a in argv], "--prices", str(prices)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_a_conversation_is_the_sum_of_its_turns(self):
        run = self.write_run("smoke-20261006T000000Z",
                             [(100, 10), (200, 20), (300, 30)], 3.0)
        out = self.cost(run)
        self.assertEqual(out["cost"]["tokens"]["input"], 600)
        self.assertEqual(out["cost"]["cases_with_trace"], 1)

    def test_one_turn_without_a_trace_withholds_the_case(self):
        run = self.write_run("smoke-20261006T000000Z",
                             [(100, 10), (200, 20)], 2.0, drop_turn=1)
        out = self.cost(run)
        self.assertEqual(out["cost"]["cases_without_trace"], 2)

    def test_pairs_that_reached_different_turns_are_refused(self):
        base = self.write_run("smoke-20261006T000000Z", [(100, 10)], 1.2,
                              verdict="fail")
        cand = self.write_run("smoke-20261006T000001Z",
                              [(100, 10)] * 4, 4.8)
        out = self.cost(cand, "--baseline", base)
        paired = out["paired"]
        self.assertEqual(paired["turns_sent_mismatch"]["case_ids"],
                         ["c-convo"])
        # Only the single-turn case pairs; the conversation is named above
        # and is not counted as a case dropped for a missing trace either.
        # (c-plain has no trajectory in this fixture, so cost pairs none.)
        self.assertEqual(paired["latency"]["n_paired"], 1)
        self.assertEqual(paired["cost"]["n_paired"], 0)
        self.assertEqual(paired["cost"]["cases_dropped_for_missing_trace"], 1)

    def test_pairs_that_sent_the_same_turns_are_paired(self):
        base = self.write_run("smoke-20261006T000000Z", [(100, 10)] * 2, 2.0)
        cand = self.write_run("smoke-20261006T000001Z", [(100, 10)] * 2, 3.0)
        paired = self.cost(cand, "--baseline", base)["paired"]
        self.assertNotIn("turns_sent_mismatch", paired)
        self.assertEqual(paired["latency"]["n_paired"], 2)


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
