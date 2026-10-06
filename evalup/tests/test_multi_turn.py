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
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_run_cases import (  # noqa: E402 - the shared fixtures
    SCRIPTS,
    RunnerCase,
    make_case,
    make_plan,
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


if __name__ == "__main__":
    unittest.main()
