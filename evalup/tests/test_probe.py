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
        self.assertIsNone(report["response"]["tool_calls"])


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
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--plan", "p", "--out", "o",
             "--message", "hi"], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--probe flags", json.loads(proc.stdout)["error"])

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
        self.assertIn('scripts/run_cases.py" --probe --adapter', step)
        self.assertIn("never `curl`", step)
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
