"""run_cases.py --probe (#49): discover's live check, one request through the
adapter with no shell command expanding a secret.

Headless, Claude Code denies a `curl -H "Cookie: $APP_TEST_COOKIE"` whatever
the allow rules say, so no app needing auth got past discover. The probe is
the runner reading cookie_env/token_env itself, sending one request through
the path real cases take, and printing a report with every secret put back
as ${NAME}. These tests drive it as a subprocess against a stub app, like
test_run_cases.py, and assert the two promises that matter: the secret never
reaches stdout or stderr, and nothing is written.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_run_cases import (  # noqa: E402 - the shared fixtures
    RUNNER,
    SCRIPTS,
    FakeApp,
    make_adapter,
)
from test_sse import (  # noqa: E402
    EVENT_STREAM,
    RawApp,
    agent_run,
    stream,
    text_message,
    tool_call,
)

PLUGIN = SCRIPTS.parent
COOKIE = "sid=probe-cookie-0123456789"
TOKEN = "tok-abcdef-0123456789"


def have_yaml():
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def cookie_adapter(base_url, **overrides):
    adapter = make_adapter(
        "${APP_BASE_URL}",
        invocation={"auth": {"type": "cookie",
                             "cookie_env": "APP_TEST_COOKIE"}},
        data={"may_contain_pii": True})
    adapter["invocation"].update(overrides)
    return adapter


def echo_responder(path, body, headers):
    """An app that accepts only the right cookie and echoes it back, the
    worst case for a transcript: the secret comes back in the reply."""
    if headers.get("Cookie") != COOKIE:
        return 401, {"message": "login required"}, {}
    return 200, {"message": "hi, you sent " + headers["Cookie"],
                 "debug": {"cookie": headers["Cookie"]}}, {}


class ProbeCase(unittest.TestCase):
    responder = staticmethod(echo_responder)

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / ".evalup"
        self.state.mkdir()
        self.app = FakeApp(self.responder)
        self.addCleanup(self.app.close)

    def write_adapter(self, adapter, name="adapter.json"):
        path = self.state / name
        path.write_text(json.dumps(adapter), encoding="utf-8")
        return path

    def probe(self, adapter_path, message="hello", extra=(), env=None):
        environ = dict(os.environ, APP_BASE_URL=self.app.base_url,
                       APP_TEST_COOKIE=COOKIE, EVAL_USER_ID="1")
        for key, value in (env or {}).items():
            if value is None:
                environ.pop(key, None)
            else:
                environ[key] = value
        argv = [sys.executable, str(RUNNER), "--probe",
                "--adapter", str(adapter_path)]
        if message is not None:
            argv += ["--message", message]
        proc = subprocess.run([*argv, *extra], env=environ,
                              capture_output=True, text=True, check=False)
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = None
        return proc.returncode, payload, proc

    def tree(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*"))


class TestCookieAuth(ProbeCase):
    def test_sends_the_cookie_and_never_prints_it(self):
        path = self.write_adapter(cookie_adapter(self.app.base_url))
        rc, report, proc = self.probe(path)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertTrue(report["ok"])
        self.assertEqual(report["problems"], [])
        # It reached the app, with the cookie, in one request: no health GET.
        self.assertEqual(len(self.app.calls), 1)
        self.assertEqual(self.app.calls[0]["headers"].get("Cookie"), COOKIE)
        self.assertEqual(self.app.calls[0]["path"], "/api/chat/ask")
        self.assertEqual(self.app.calls[0]["body"]["message"], "hello")
        # The secret is nowhere in what a transcript would show, even though
        # the app echoed it into the answer and the body.
        self.assertNotIn(COOKIE, proc.stdout + proc.stderr)
        self.assertNotIn("probe-cookie", proc.stdout + proc.stderr)
        self.assertEqual(report["request"]["headers"]["Cookie"], "<redacted>")
        self.assertEqual(report["response"]["answer"],
                         "hi, you sent ${APP_TEST_COOKIE}")
        self.assertIn("${APP_TEST_COOKIE}", report["response"]["body_preview"])
        self.assertEqual(report["request"]["url"],
                         "${APP_BASE_URL}/api/chat/ask")

    def test_lists_every_env_var_the_adapter_needs(self):
        path = self.write_adapter(cookie_adapter(self.app.base_url))
        _, report, _ = self.probe(path)
        self.assertEqual(report["env_vars"], ["APP_BASE_URL", "APP_TEST_COOKIE"])

    def test_writes_nothing(self):
        # may_contain_pii: true would make a run write reports/.gitignore.
        path = self.write_adapter(cookie_adapter(self.app.base_url))
        before = self.tree()
        rc, report, _ = self.probe(path)
        self.assertEqual(rc, 0)
        self.assertEqual(report["writes"], "none")
        self.assertEqual(self.tree(), before)

    def test_a_wrong_cookie_is_exit_3_with_the_status(self):
        path = self.write_adapter(cookie_adapter(self.app.base_url))
        rc, report, proc = self.probe(path, env={"APP_TEST_COOKIE": "sid=nope"})
        self.assertEqual(rc, 3)
        self.assertFalse(report["ok"])
        self.assertIn("HTTP 401", report["error"])
        self.assertNotIn("sid=nope", proc.stdout + proc.stderr)

    def test_an_unset_env_var_is_exit_3_naming_it_with_no_app_call(self):
        path = self.write_adapter(cookie_adapter(self.app.base_url))
        rc, report, _ = self.probe(path, env={"APP_TEST_COOKIE": None})
        self.assertEqual(rc, 3)
        self.assertEqual(report["missing_env"], ["APP_TEST_COOKIE"])
        self.assertEqual(self.app.calls, [])
        # The same report shape as a sent probe: discover lists every var
        # the adapter needs, not only the missing ones.
        self.assertIs(report["ok"], False)
        self.assertEqual(report["env_vars"],
                         ["APP_BASE_URL", "APP_TEST_COOKIE"])
        self.assertIn("unresolved env var", report["problems"][0])
        self.assertIsNone(report["response"])


class TestScrubEdges(ProbeCase):
    """The review's three leaks (PR #60): each printed part of the secret."""

    pad = 0

    def responder(self, path, body, headers):
        return 200, {"message": "ok", "pad": "x" * self.pad,
                     "echo": headers.get("Cookie")}, {}

    def setUp(self):
        super().setUp()
        self.app.server.responder = lambda p, b, h: (
            self.app.calls.append({"path": p, "body": b, "headers": h})
            or self.responder(p, b, h))

    def leaked(self, proc, secret):
        out = proc.stdout + proc.stderr
        return secret in out or secret[4:12] in out

    def test_a_secret_across_the_preview_cut_is_not_half_printed(self):
        path = self.write_adapter(cookie_adapter(self.app.base_url))
        for pad in range(3940, 3975, 3):
            with self.subTest(pad=pad):
                self.pad = pad
                rc, report, proc = self.probe(path)
                self.assertEqual(rc, 0, proc.stdout)
                self.assertFalse(self.leaked(proc, COOKIE))

    def test_a_value_json_escapes_is_still_scrubbed(self):
        quoted = 'sid="quoted-secret-9876\\x"'
        path = self.write_adapter(cookie_adapter(self.app.base_url))
        rc, report, proc = self.probe(path, env={"APP_TEST_COOKIE": quoted})
        self.assertNotIn("quoted-secret-9876", proc.stdout + proc.stderr)

    def test_a_cr_in_the_cookie_is_exit_3_naming_only_the_var(self):
        path = self.write_adapter(cookie_adapter(self.app.base_url))
        rc, report, proc = self.probe(
            path, env={"APP_TEST_COOKIE": "sid=crlf-secret-5555\r"})
        self.assertEqual(rc, 3, proc.stdout)
        self.assertIn("APP_TEST_COOKIE", report["error"])
        self.assertNotIn("crlf-secret", proc.stdout + proc.stderr)
        self.assertEqual(self.app.calls, [])

    def test_a_short_auth_value_is_scrubbed_a_short_ref_is_not(self):
        adapter = cookie_adapter(self.app.base_url)
        adapter["invocation"]["request_body"] = \
            '{"message": "<user turn>", "tenant": "${EVAL_TENANT}"}'
        path = self.write_adapter(adapter)
        rc, report, proc = self.probe(
            path, message="say 42",
            env={"APP_TEST_COOKIE": "s=1", "EVAL_TENANT": "42"})
        self.assertEqual(rc, 0, proc.stdout)
        self.assertNotIn('"s=1"', proc.stdout)
        self.assertEqual(report["request"]["body"]["message"], "say 42")


class TestSecondReview(ProbeCase):
    """Findings of the second review of PR #60."""

    @staticmethod
    def responder(path, body, headers):
        key = headers.get("X-Api-Key", "")
        return 200, {"message": "ok " + key, "calls": [
            {"name": "t", "result": json.dumps({"seen": headers.get(
                "Cookie", "")})}]}, {}

    def test_a_tab_in_a_url_secret_is_named_not_printed(self):
        adapter = make_adapter(self.app.base_url, invocation={
            "endpoint": "POST /chat?key=${APP_KEY}"})
        rc, report, proc = self.probe(
            self.write_adapter(adapter),
            env={"APP_KEY": "super\tsecretvalue"})
        self.assertEqual(rc, 3, proc.stdout)
        self.assertIn("APP_KEY", report["error"])
        self.assertNotIn("secretvalue", proc.stdout + proc.stderr)
        self.assertEqual(self.app.calls, [])

    def test_a_quoted_cookie_inside_a_json_string_is_scrubbed(self):
        adapter = cookie_adapter(self.app.base_url)
        adapter["traces"] = {"source": "response", "tool_calls_path": "calls"}
        rc, report, proc = self.probe(
            self.write_adapter(adapter),
            env={"APP_TEST_COOKIE": 'sid="nested-secret-4242"'})
        self.assertEqual(rc, 0, proc.stdout)
        self.assertNotIn("nested-secret-4242", proc.stdout + proc.stderr)

    def test_a_short_headers_auth_value_echoed_back_is_scrubbed(self):
        adapter = make_adapter(self.app.base_url, invocation={
            "auth": {"type": "headers", "headers": {"X-Api-Key": "${K}"}}})
        rc, report, proc = self.probe(self.write_adapter(adapter),
                                      env={"K": "zq7"})
        self.assertEqual(rc, 0, proc.stdout)
        self.assertEqual(report["response"]["answer"], "ok ${K}")

    def test_a_malformed_adapter_is_the_report_not_a_traceback(self):
        for adapter in ({"invocation": "x"},
                        make_adapter(self.app.base_url,
                                     invocation={"auth": "cookie"})):
            with self.subTest(adapter=adapter):
                rc, report, proc = self.probe(self.write_adapter(adapter))
                # exit 1: the run's own pre-flight raises on auth: "cookie"
                self.assertIn(rc, (1, 2, 3), proc.stdout + proc.stderr)
                self.assertIs(report["ok"], False)
                self.assertNotIn("Traceback", proc.stderr)

    def test_an_unimplemented_trace_source_is_refused(self):
        adapter = make_adapter(self.app.base_url, traces={
            "source": "jaeger", "correlation": "traceparent-echo"})
        rc, report, _ = self.probe(self.write_adapter(adapter))
        self.assertEqual(rc, 3)
        self.assertIn("jaeger", report["error"])
        self.assertEqual(self.app.calls, [])

    def test_an_infinite_timeout_is_bad_input(self):
        path = self.write_adapter(make_adapter(self.app.base_url))
        rc, report, _ = self.probe(path, extra=["--timeout", "inf"])
        self.assertEqual(rc, 2)
        self.assertIn("--timeout", report["error"])

    def test_function_mode_keeps_stdout_one_report(self):
        (self.tmp / "probe_entry.py").write_text(
            "print('import noise')\n"
            "def handle(request):\n"
            "    print('call noise')\n"
            "    return {'text': 'from function', 'status': 200}\n",
            encoding="utf-8")
        adapter = {"app": {"name": "f", "repo": str(self.tmp)},
                   "invocation": {"mode": "function",
                                  "entrypoint": "probe_entry:handle",
                                  "request_body": '{"message": "<user turn>"}'}}
        rc, report, proc = self.probe(self.write_adapter(adapter))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(report["response"]["answer"], "from function")
        self.assertIn("noise", proc.stderr)

    def test_function_mode_refusing_with_a_400_is_exit_8(self):
        (self.tmp / "probe_refuse.py").write_text(
            "def handle(request):\n"
            "    return {'text': 'only invoices', 'status': 400}\n",
            encoding="utf-8")
        adapter = {"app": {"name": "f", "repo": str(self.tmp)},
                   "invocation": {"mode": "function",
                                  "entrypoint": "probe_refuse:handle",
                                  "request_body": '{"message": "<user turn>"}'}}
        rc, report, proc = self.probe(self.write_adapter(adapter))
        self.assertEqual(rc, 8, proc.stdout + proc.stderr)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["response"]["answer"], "only invoices")


class TestBearerAndUrlSecrets(ProbeCase):
    @staticmethod
    def responder(path, body, headers):
        return 200, {"message": "ok " + headers.get("Authorization", "")}, {}

    def test_a_token_in_the_header_and_the_url_is_scrubbed(self):
        adapter = make_adapter(
            "${APP_BASE_URL}",
            invocation={"auth": {"type": "bearer", "token_env": "APP_TOKEN"},
                        "endpoint": "POST /api/chat/ask?key=${APP_KEY}"})
        path = self.write_adapter(adapter)
        rc, report, proc = self.probe(
            path, env={"APP_TOKEN": TOKEN, "APP_KEY": "url-key-987654"})
        self.assertEqual(rc, 0, proc.stdout)
        out = proc.stdout + proc.stderr
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("url-key-987654", out)
        self.assertEqual(report["request"]["headers"]["Authorization"],
                         "<redacted>")
        self.assertIn("?key=${APP_KEY}", report["request"]["url"])
        self.assertEqual(report["response"]["answer"],
                         "ok Bearer ${APP_TOKEN}")
        self.assertEqual(report["env_vars"],
                         ["APP_BASE_URL", "APP_KEY", "APP_TOKEN"])

    def test_an_unreachable_host_is_exit_3_and_its_error_is_scrubbed(self):
        adapter = make_adapter("http://127.0.0.1:${APP_PORT}/x${APP_KEY}")
        path = self.write_adapter(adapter)
        rc, report, proc = self.probe(path, env={"APP_PORT": "9",
                                                 "APP_KEY": "url-key-987654"})
        self.assertEqual(rc, 3, proc.stdout)
        self.assertIn("no usable response", report["error"])
        self.assertNotIn("url-key-987654", proc.stdout + proc.stderr)


def origin_guard(path, body, headers):
    """A same-origin guard (#50) behind cookie auth."""
    if headers.get("Origin") != "http://app.test":
        return 403, {"error": "Cross-origin request refused"}, {}
    if headers.get("Cookie") != COOKIE:
        return 401, {"message": "login required"}, {}
    return 200, {"message": "hi"}, {}


class TestRequiredHeaders(ProbeCase):
    """#50's acceptance criteria, through discover's live check."""

    responder = staticmethod(origin_guard)

    def test_a_403_from_an_origin_guard_is_named_as_one(self):
        rc, report, _ = self.probe(self.write_adapter(
            cookie_adapter(self.app.base_url)))
        self.assertEqual(rc, 3)
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["problems"]), 2)   # + no answer
        self.assertIn("same-origin or CSRF guard", report["problems"][0])
        self.assertIn("invocation.headers", report["problems"][0])

    def test_cookie_auth_plus_a_declared_origin_sends_both(self):
        adapter = cookie_adapter(self.app.base_url,
                                 headers={"Origin": "${APP_ORIGIN}"})
        rc, report, proc = self.probe(self.write_adapter(adapter),
                                      env={"APP_ORIGIN": "http://app.test"})
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertTrue(report["ok"])
        headers = self.app.calls[0]["headers"]
        self.assertEqual(headers.get("Origin"), "http://app.test")
        self.assertEqual(headers.get("Cookie"), COOKIE)
        self.assertEqual(report["request"]["headers"]["Origin"], "<redacted>")
        self.assertIn("APP_ORIGIN", report["env_vars"])

    def test_laravels_419_is_named_as_a_guard_too(self):
        def laravel(path, body, headers):
            return 419, {"message": "CSRF token mismatch."}, {}

        self.app.server.responder = laravel
        rc, report, _ = self.probe(self.write_adapter(
            cookie_adapter(self.app.base_url)))
        self.assertEqual(rc, 3)
        self.assertIn("HTTP 419 naming origin/CSRF", report["problems"][0])

    def test_a_plain_403_still_says_check_auth(self):
        def plain(path, body, headers):
            return 403, {"message": "forbidden"}, {}

        self.app.server.responder = plain
        rc, report, _ = self.probe(self.write_adapter(
            cookie_adapter(self.app.base_url)))
        self.assertEqual(rc, 3)
        self.assertIn("check auth", report["problems"][0])


class TestVerdictOfTheProbe(ProbeCase):
    @staticmethod
    def responder(path, body, headers):
        if path.endswith("/500"):
            return 500, {"message": "boom"}, {}
        return 200, {"reply": "wrong field"}, {}

    def test_no_answer_at_the_marker_is_exit_3(self):
        path = self.write_adapter(make_adapter(self.app.base_url))
        rc, report, _ = self.probe(path)
        self.assertEqual(rc, 3)
        self.assertIn("<answer>", report["error"])
        self.assertIn("wrong field", report["response"]["body_preview"])

    def test_a_5xx_is_sent_once_not_retried(self):
        adapter = make_adapter(self.app.base_url,
                               invocation={"endpoint": "POST /500"})
        rc, report, _ = self.probe(self.write_adapter(adapter))
        self.assertEqual(rc, 3)
        self.assertEqual(len(self.app.calls), 1)
        self.assertEqual(report["response"]["status"], 500)

    def test_a_converted_json_wrapper_is_accepted(self):
        adapter = make_adapter(self.app.base_url,
                               invocation={"response_body":
                                           '{"reply": "<answer>"}'})
        path = self.write_adapter({"adapter": adapter, "cases": []},
                                  name="converted.json")
        rc, report, _ = self.probe(path)
        self.assertEqual(rc, 0)
        self.assertEqual(report["response"]["answer"], "wrong field")


def scoped_app(path, body, headers):
    """An app that refuses out-of-scope questions with a deliberate 400 and
    a body the adapter reads (#124), like the quickstart's."""
    if "weather" in json.dumps(body):
        return 400, {"message": "I only help with invoices and shifts",
                     "domain": "none"}, {}
    return 200, {"message": "you have 2 open invoices", "calls": []}, {}


class TestAppRefusedWithA4xx(ProbeCase):
    """#124: a 4xx with an answer at the marker is the app's refusal, not an
    adapter failure -- exit 8, a warning, no problems. The statuses that name
    the adapter's own request keep exit 3 and "check auth"."""

    responder = staticmethod(scoped_app)

    def test_an_out_of_scope_400_is_exit_8_not_an_adapter_failure(self):
        rc, report, proc = self.probe(self.write_adapter(
            make_adapter(self.app.base_url)), message="weather in Paris")
        self.assertEqual(rc, 8, proc.stdout + proc.stderr)
        self.assertFalse(report["ok"])
        self.assertEqual(report["problems"], [])
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("HTTP 400: the app answered", report["warnings"][0])
        self.assertIn("in-scope question", report["warnings"][0])
        self.assertNotIn("check auth", report["error"])
        self.assertIn("refused the request with HTTP 400", report["error"])
        self.assertEqual(report["response"]["status"], 400)
        self.assertEqual(report["response"]["answer"],
                         "I only help with invoices and shifts")

    def test_the_in_scope_question_still_verifies(self):
        rc, report, _ = self.probe(self.write_adapter(
            make_adapter(self.app.base_url)), message="my invoices")
        self.assertEqual(rc, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["warnings"], [])
        self.assertNotIn("error", report)

    def test_a_400_with_no_answer_at_the_marker_is_still_exit_3(self):
        adapter = make_adapter(self.app.base_url,
                               invocation={"response_body":
                                           '{"reply": "<answer>"}'})
        rc, report, _ = self.probe(self.write_adapter(adapter),
                                   message="weather")
        self.assertEqual(rc, 3)
        self.assertEqual(report["warnings"], [])
        self.assertIn("check auth", report["problems"][0])

    def test_statuses_naming_the_request_keep_check_auth(self):
        # A user id longer than "1", which as an auth value is scrubbed at
        # any length and would turn "HTTP 401" into "HTTP 40${EVAL_USER_ID}".
        for status in (401, 403, 404, 405, 407):
            with self.subTest(status=status):
                self.app.server.responder = \
                    lambda path, body, headers, s=status: (
                        s, {"message": "nope"}, {})
                rc, report, _ = self.probe(self.write_adapter(
                    make_adapter(self.app.base_url)),
                    env={"EVAL_USER_ID": "user-42"})
                self.assertEqual(rc, 3)
                self.assertEqual(report["warnings"], [])
                self.assertIn(f"HTTP {status}: the app did not accept",
                              report["problems"][0])

    def test_a_csrf_guard_with_an_answer_is_still_the_guard(self):
        self.app.server.responder = lambda path, body, headers: (
            419, {"message": "CSRF token mismatch."}, {})
        rc, report, _ = self.probe(self.write_adapter(
            make_adapter(self.app.base_url)))
        self.assertEqual(rc, 3)
        self.assertEqual(report["warnings"], [])
        self.assertIn("same-origin or CSRF guard", report["problems"][0])

    def test_a_refusal_without_a_tool_call_list_is_said_not_failed(self):
        adapter = make_adapter(self.app.base_url, traces={
            "source": "response", "tool_calls_path": "calls"})
        rc, report, proc = self.probe(self.write_adapter(adapter),
                                      message="weather")
        self.assertEqual(rc, 8, proc.stdout)
        self.assertEqual(report["problems"], [])
        self.assertEqual(len(report["warnings"]), 2)
        self.assertIn("tool_calls_path", report["warnings"][1])
        rc, report, _ = self.probe(self.write_adapter(adapter),
                                   message="invoices")
        self.assertEqual(rc, 0, report)

    def test_a_5xx_or_429_with_an_answer_is_still_exit_3(self):
        for status in (429, 503):
            with self.subTest(status=status):
                self.app.server.responder = \
                    lambda path, body, headers, s=status: (
                        s, {"message": "try later"}, {})
                rc, report, _ = self.probe(self.write_adapter(
                    make_adapter(self.app.base_url)))
                self.assertEqual(rc, 3)
                self.assertEqual(report["warnings"], [])
                self.assertIn("no usable response", report["problems"][0])


class TestJsonToolCalls(ProbeCase):
    @staticmethod
    def responder(path, body, headers):
        return 200, {"message": "ok", "calls": [{"name": "lookup",
                                                 "arguments": {}}]}, {}

    def test_a_tool_call_list_in_a_json_body(self):
        adapter = make_adapter(self.app.base_url, traces={
            "source": "response", "tool_calls_path": "calls"})
        rc, report, proc = self.probe(self.write_adapter(adapter))
        self.assertEqual(rc, 0, proc.stdout)
        self.assertEqual(report["response"]["tool_calls"][0]["name"], "lookup")

    def test_a_wrong_tool_call_path_is_not_verified(self):
        adapter = make_adapter(self.app.base_url, traces={
            "source": "response", "tool_calls_path": "nope"})
        rc, report, _ = self.probe(self.write_adapter(adapter))
        self.assertEqual(rc, 3)
        self.assertIn("tool_calls_path", report["error"])


def history_app(path, body, headers):
    """Stateless and OpenAI-shaped; refuses an assistant entry that has no
    `content`, the shape a wrong history mapping sends."""
    messages = body.get("messages") or []
    if any(m.get("role") == "assistant" and "content" not in m
           for m in messages):
        return 400, {"message": "assistant message needs content"}, {}
    # Words, not a count: the probe scrubs the auth value "1" everywhere.
    return 200, {"message": "re: " + " / ".join(
        m.get("content", "") for m in messages)}, {}


class TestHistoryCheck(ProbeCase):
    """docs/client-history.md SS5: under a declared history the probe sends
    turn 1, then turn 2 carrying turn 1's rebuilt reply, and passes when
    both are 2xx -- a wrong mapping is a 400 here, not on every case."""

    responder = staticmethod(history_app)

    def history_adapter(self, assistant='{"role": "assistant", '
                                        '"content": "<text>"}'):
        adapter = make_adapter(self.app.base_url)
        adapter["invocation"]["request_body"] = '{"messages": "<history>"}'
        adapter["invocation"]["conversation"] = {"style": "none", "history": {
            "user": '{"role": "user", "content": "<text>"}',
            "assistant": assistant}}
        return self.write_adapter(adapter)

    def test_two_requests_and_the_second_carries_the_first_reply(self):
        rc, report, proc = self.probe(self.history_adapter())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.app.calls), 2)
        check = report["history_check"]
        self.assertTrue(check["ok"])
        self.assertEqual(check["turn2"]["status"], 200)
        self.assertEqual(check["turn2"]["request_body"]["messages"], [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "re: hello"},
            {"role": "user", "content": "hello"}])
        self.assertEqual(report["response"]["answer"], "re: hello")
        self.assertEqual(check["turn2"]["answer"],
                         "re: hello / re: hello / hello")
        self.assertEqual(self.tree(), [".evalup", ".evalup/adapter.json"])

    def test_a_wrong_mapping_is_caught_by_the_second_request(self):
        rc, report, _ = self.probe(self.history_adapter(
            assistant='{"role": "assistant", "text": "<text>"}'))
        self.assertEqual(rc, 3)
        self.assertEqual(len(self.app.calls), 2)
        self.assertFalse(report["history_check"]["ok"])
        self.assertIn("history check: turn 2", report["error"])
        self.assertIn("HTTP 400", report["error"])

    def test_no_history_still_sends_one_request(self):
        adapter = make_adapter(self.app.base_url)
        rc, report, _ = self.probe(self.write_adapter(adapter))
        self.assertEqual(rc, 0, report)
        self.assertEqual(len(self.app.calls), 1)
        self.assertNotIn("history_check", report)


class TestStreams(unittest.TestCase):
    body = agent_run(text_message("m1", "hello ", "there"),
                     tool_call("c1", "lookup", '{"q": "x"}', result="{}"))

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.raw = RawApp(lambda path: (200, EVENT_STREAM, self.body))
        self.addCleanup(self.raw.close)

    def probe(self):
        adapter = make_adapter(self.raw.base_url)
        adapter["invocation"].update(
            response_format="sse", sse={"protocol": "ag-ui"},
            response_body='{"text": "<answer>"}')
        adapter["traces"] = {"source": "response"}
        path = self.tmp / "adapter.json"
        path.write_text(json.dumps(adapter), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--probe", "--adapter", str(path),
             "--message", "hi"],
            env=dict(os.environ, EVAL_USER_ID="1"), capture_output=True,
            text=True, check=False)
        return proc.returncode, json.loads(proc.stdout)

    def test_answer_and_tool_calls_from_the_stream(self):
        rc, report = self.probe()
        self.assertEqual(rc, 0, report)
        self.assertEqual(report["response"]["answer"], "hello there")
        self.assertEqual([c["name"] for c in report["response"]["tool_calls"]],
                         ["lookup"])
        self.assertEqual(report["traces"]["source"], "response")

    def test_a_stream_cut_short_is_not_verified(self):
        self.body = stream({"type": "RUN_STARTED", "threadId": "t",
                            "runId": "r"},
                           *text_message("m1", "partial"))
        rc, report = self.probe()
        self.assertEqual(rc, 3)
        self.assertIn("terminal event", report["error"])
        self.assertEqual(len(report["problems"]), 2)  # gap once, + no answer
        self.assertIsNone(report["response"]["tool_calls"])


class TestStreamedRefusal(unittest.TestCase):
    """#124 through an SSE adapter: a whole 400 stream is the app's
    refusal (exit 8); a 400 that is not a stream, or one cut short, is an
    adapter problem (exit 3) whose report carries no "adapter works"
    warning beside it."""

    status, content_type = 400, EVENT_STREAM

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.body = agent_run(text_message("m1", "out of ", "scope"))
        self.raw = RawApp(
            lambda path: (self.status, self.content_type, self.body))
        self.addCleanup(self.raw.close)

    probe = TestStreams.probe

    def test_a_whole_400_stream_is_exit_8(self):
        rc, report = self.probe()
        self.assertEqual(rc, 8, report)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["response"]["answer"], "out of scope")
        self.assertTrue(report["warnings"])

    def test_a_json_400_to_an_sse_adapter_is_exit_3_with_no_warning(self):
        self.content_type = "application/json"
        self.body = json.dumps({"text": "out of scope"})
        rc, report = self.probe()
        self.assertEqual(rc, 3, report)
        self.assertIn("not text/event-stream", report["error"])
        self.assertEqual(report["warnings"], [])

    def test_a_400_stream_cut_short_is_exit_3_with_no_warning(self):
        self.body = stream({"type": "RUN_STARTED", "threadId": "t",
                            "runId": "r"},
                           *text_message("m1", "partial"))
        rc, report = self.probe()
        self.assertEqual(rc, 3, report)
        self.assertIn("terminal event", report["error"])
        self.assertEqual(report["warnings"], [])
        self.assertNotIn("refused the request", report["error"])


class TestTraceStore(ProbeCase):
    @staticmethod
    def responder(path, body, headers):
        return 200, {"message": "ok", "traceId": "ab" * 16}, {}

    def test_a_store_that_does_not_exist_yet_is_not_read(self):
        adapter = make_adapter(self.app.base_url, traces={
            "source": "otlp-file", "convention": "gen_ai",
            "correlation": "response-field:traceId",
            "location": str(self.tmp / "not-yet.jsonl")})
        rc, report, proc = self.probe(self.write_adapter(adapter))
        self.assertEqual(rc, 0, proc.stdout)
        self.assertEqual(report["response"]["trace_id"], "ab" * 16)
        self.assertIsNone(report["response"]["tool_calls"])


class TestFlags(ProbeCase):
    def test_probe_runs_alone(self):
        path = self.write_adapter(make_adapter(self.app.base_url))
        for extra in (["--plan", "p.json"], ["--out", "o"], ["--dry-run"],
                      ["--verify", "d"]):
            with self.subTest(extra=extra):
                rc, report, _ = self.probe(path, extra=extra)
                self.assertEqual(rc, 2)
                self.assertIn("--probe runs alone", report["error"])
        self.assertEqual(self.app.calls, [])

    def test_probe_flags_need_probe(self):
        for message in ("hi", ""):
            with self.subTest(message=message):
                proc = subprocess.run(
                    [sys.executable, str(RUNNER), "--plan", "p", "--out",
                     "o", "--message", message],
                    capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("--probe flags",
                              json.loads(proc.stdout)["error"])

    def test_a_message_is_required(self):
        path = self.write_adapter(make_adapter(self.app.base_url))
        rc, report, _ = self.probe(path, message=None)
        self.assertEqual(rc, 2)
        self.assertIn("--message", report["error"])
        rc, _, _ = self.probe(path, message="  ")
        self.assertEqual(rc, 2)

    def test_a_missing_adapter_is_exit_2(self):
        rc, report, _ = self.probe(self.state / "nope.yaml")
        self.assertEqual(rc, 2)
        self.assertIn("not a file", report["error"])


@unittest.skipUnless(have_yaml(), "needs PyYAML (convert_suite.py)")
class TestYamlAdapter(ProbeCase):
    def test_adapter_yaml_goes_through_convert_suite(self):
        path = self.state / "adapter.yaml"
        path.write_text(
            "schema_version: 1\n"
            "invocation:\n"
            "  mode: http\n"
            "  base_url: ${APP_BASE_URL}\n"
            "  endpoint: POST /api/chat/ask\n"
            "  request_body: '{\"message\": \"<user turn>\"}'\n"
            "  response_body: '{\"message\": \"<answer>\"}'\n"
            "  auth: {type: cookie, cookie_env: APP_TEST_COOKIE}\n",
            encoding="utf-8")
        before = self.tree()
        rc, report, proc = self.probe(path)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertNotIn(COOKIE, proc.stdout + proc.stderr)
        self.assertEqual(self.tree(), before)

    def test_a_yaml_error_is_exit_2(self):
        path = self.state / "adapter.yaml"
        path.write_text("invocation: [unclosed\n", encoding="utf-8")
        rc, report, _ = self.probe(path)
        self.assertEqual(rc, 2)
        self.assertIn("convert_suite.py", report["error"])


class TestConvertSuiteAdapterFile(unittest.TestCase):
    def test_adapter_file_takes_no_state_dir(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "convert_suite.py"), "somedir",
             "--adapter-file", "adapter.yaml"],
            capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--adapter-file", proc.stderr)


class TestDiscoverUsesTheProbe(unittest.TestCase):
    """The skill side of #49: discover's live check is the pre-approved
    script call, never a shell command that expands a secret."""

    text = (PLUGIN / "skills" / "discover" / "SKILL.md").read_text(
        encoding="utf-8")

    def step(self, heading):
        start = self.text.index(heading)
        match = re.compile(r"\n##+ ").search(self.text, start + 1)
        return self.text[start:match.start() if match else None]

    def test_step_4_calls_the_runner_probe(self):
        step = self.step("### 4. Validate the profile")
        self.assertIn('`evalup run-cases --probe --adapter', step)
        self.assertIn("Never `curl`", step)
        self.assertNotIn("curl -", self.text)
        # No `$VAR` a permission check would have to expand.
        self.assertIsNone(re.search(r"\$[A-Z_]{2,}", step))

    def test_the_closing_summary_names_the_env_vars(self):
        self.assertIn("env_vars", self.step("### 8. Report findings"))

    def test_readme_recipe_no_longer_needs_curl(self):
        readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("Bash(curl *)", readme)


if __name__ == "__main__":
    unittest.main()
