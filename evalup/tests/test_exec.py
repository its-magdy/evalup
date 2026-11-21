"""invocation.mode: exec (#39, option 2): scripts/_exec.py on its own, then
the runner -- a run, a conversation, the dry run, pacing and --probe --
against tests/exec_shim.py, a stdlib stand-in for an app's own shim.
"""
import contextlib
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_multi_turn import conversation_case  # noqa: E402
from test_run_cases import (  # noqa: E402 - the shared fixtures
    RUNNER,
    SCRIPTS,
    RunnerCase,
    make_case,
    make_plan,
)

sys.path.insert(0, str(SCRIPTS))
import _exec  # noqa: E402

SHIM = pathlib.Path(__file__).resolve().parent / "exec_shim.py"
SECRET = "s3cret-value-0123456789"
POSIX = os.name == "posix"


def shim_argv(*flags):
    return [sys.executable, str(SHIM), *flags]


def run_shim(*flags, body=None, timeout=10, secrets=(), env=None):
    return _exec.run(shim_argv(*flags), sys.executable, str(SHIM.parent),
                     env if env is not None else _exec.child_env([]),
                     body if body is not None else {"message": "hi"},
                     timeout, secrets)


def tempdir(test):
    path = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, path, True)
    return path


def gone(pid, within_s=5.0):
    """Has `pid` exited (and been reaped by whoever owns it)?"""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


class TestCheckConfig(unittest.TestCase):
    def test_a_sound_block(self):
        self.assertEqual(_exec.check_config(
            {"argv": ["node", "shim.js"], "cwd": "app", "env": ["API_KEY"]}),
            [])
        self.assertEqual(_exec.check_config({"argv": ["./shim"],
                                             "cwd": ".."}), [])

    def test_defects(self):
        cases = [
            (None, "must be an object"),
            ({"argv": "node shim.js"}, "never a shell string"),
            ({"argv": []}, "non-empty list"),
            ({"argv": [""]}, "non-empty list"),
            ({"argv": ["node"]}, "cwd is required"),
            ({"argv": ["node", 3]}, "non-empty list"),
            ({"argv": ["node", "--key=${API_KEY}"]}, "holds a ${REF}"),
            ({"argv": ["node", "a\x00b"]}, "NUL"),
            ({"argv": ["node"], "cwd": "${APP_DIR}"}, "cwd holds a ${REF}"),
            ({"argv": ["node"], "cwd": ""}, "non-empty path"),
            ({"argv": ["node"], "env": "API_KEY"}, "list of env var NAMES"),
            ({"argv": ["node"], "env": ["API_KEY=abc"]}, "never a value"),
            ({"argv": ["node"], "shell": True}, "unknown key(s)"),
        ]
        for block, fragment in cases:
            with self.subTest(block=block):
                problems = _exec.check_config(block)
                self.assertTrue(any(fragment in p for p in problems),
                                problems)


class TestEnvAndCommand(unittest.TestCase):
    def test_the_child_env_is_an_allowlist(self):
        environ = {"PATH": "/bin", "HOME": "/h", "API_KEY": "k",
                   "AWS_SECRET_ACCESS_KEY": "nope", "EMPTY": ""}
        self.assertEqual(_exec.child_env(["API_KEY", "EMPTY", "UNSET"],
                                         environ),
                         {"PATH": "/bin", "HOME": "/h", "API_KEY": "k"})

    def test_cwd_is_absolute_even_from_a_relative_base(self):
        self.assertEqual(_exec.resolve_cwd("sub", "rel/state"),
                         os.path.abspath("rel/state/sub"))
        self.assertEqual(_exec.resolve_cwd(None, "."), os.getcwd())

    def test_cwd_resolves_against_its_base(self):
        self.assertEqual(_exec.resolve_cwd(None, "/app"),
                         os.path.abspath("/app"))
        self.assertEqual(_exec.resolve_cwd("tools/../shim", "/app"),
                         os.path.abspath("/app/shim"))
        self.assertEqual(_exec.resolve_cwd("/elsewhere", "/app"),
                         os.path.abspath("/elsewhere"))

    @unittest.skipUnless(POSIX, "executable bits and PATH lookup")
    def test_the_command_resolves_on_the_childs_path_or_cwd(self):
        tmp = pathlib.Path(tempdir(self))
        tool = tmp / "bin" / "shimtool"
        tool.parent.mkdir()
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o755)
        env = {"PATH": str(tool.parent)}
        self.assertEqual(_exec.resolve_command("shimtool", "/", env),
                         str(tool))
        self.assertIsNone(_exec.resolve_command("shimtool", "/",
                                                {"PATH": "/nowhere"}))
        self.assertEqual(_exec.resolve_command("bin/shimtool", str(tmp), {}),
                         str(tool))
        (tmp / "plain").write_text("x", encoding="utf-8")
        self.assertIsNone(_exec.resolve_command("./plain", str(tmp), {}))


class TestRun(unittest.TestCase):
    """_exec.run: one process per call, every failure a transport kind."""

    def test_stdin_in_json_out(self):
        result = run_shim("--tools", body={"message": "hello"})
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"]["text"], "ok: hello")
        self.assertEqual(result["body"]["cwd"], str(SHIM.parent))
        self.assertEqual(result["body"]["tool_calls"][0]["name"],
                         "list_invoices")
        self.assertEqual(result["headers"], {})

    def test_failures_map_to_transport_kinds(self):
        cases = [
            ("exit", "connection", "exited 3; stderr: Traceback: boom"),
            ("badjson", "transport", "not one JSON document"),
            ("notobject", "transport", "not an object (got list)"),
            ("empty", "transport", "printed nothing"),
        ]
        for flag, kind, fragment in cases:
            with self.subTest(flag):
                with self.assertRaises(_exec.ExecFailure) as caught:
                    run_shim("--misbehave", flag)
                self.assertEqual(caught.exception.kind, kind)
                self.assertIn(fragment, caught.exception.message)

    def test_a_command_that_cannot_start(self):
        with self.assertRaises(_exec.ExecFailure) as caught:
            _exec.run(["missing-cmd"], "/nonexistent/missing-cmd", "/",
                      {}, {}, 5)
        self.assertEqual(caught.exception.kind, "transport")
        self.assertIn("could not start missing-cmd", caught.exception.message)

    @unittest.skipUnless(POSIX, "process groups")
    def test_a_timeout_kills_the_whole_process_group(self):
        pidfile = pathlib.Path(tempdir(self)) / "pid"
        started = time.monotonic()
        with self.assertRaises(_exec.ExecFailure) as caught:
            run_shim("--misbehave", "sleep", "--pidfile", str(pidfile),
                     timeout=1.5)
        # Generous on purpose: a loaded CI runner is slow, and the hang
        # this guards against was 60 s, not 2.
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertIn("timed out after 1.5s", caught.exception.message)
        # The grandchild the shim started in its group is dead too.
        self.assertTrue(gone(int(pidfile.read_text(encoding="utf-8")),
                             within_s=20))

    def test_huge_stderr_neither_blocks_nor_floods(self):
        result = run_shim("--misbehave", "stderr")
        self.assertEqual(result["body"]["text"], "ok: hi")
        with self.assertRaises(_exec.ExecFailure) as caught:
            run_shim("--misbehave", "stderr-exit")
        message = caught.exception.message
        # The END of stderr, cut to QUOTE_CHARS.
        self.assertIn("the end of stderr", message)
        self.assertLess(len(message), _exec.QUOTE_CHARS + 200)

    def test_stdout_over_the_cap_is_no_answer(self):
        with mock.patch.object(_exec, "STDOUT_CAP_BYTES", 64), \
                self.assertRaises(_exec.ExecFailure) as caught:
            run_shim()
        self.assertEqual(caught.exception.kind, "transport")
        self.assertIn("stdout exceeded 64 bytes", caught.exception.message)

    def test_scrub_catches_every_quoted_form_whatever_the_length(self):
        value = 'ab"c\\d\u00e9'
        text = " | ".join(["plain " + value,
                           "json " + json.dumps(value),
                           "json8 " + json.dumps(value, ensure_ascii=False),
                           "repr " + repr(value), "short 42"])
        out = _exec.scrub(text, [(value, "K"), ("42", "TENANT")])
        self.assertNotIn("ab", out)
        self.assertEqual(out.count("${K}"), 4, out)
        self.assertIn("short ${TENANT}", out)

    def test_a_bad_stdout_is_scrubbed_before_it_is_cut(self):
        # The secret straddles character 200, where the quote is cut. No
        # dictionary words in it, so no 5 characters of it occur by chance.
        secret = "Q7zK9wX2vB8nM4pL5rT1"
        env = dict(_exec.child_env([]), SHIM_SECRET=secret)
        with self.assertRaises(_exec.ExecFailure) as caught:
            run_shim("--misbehave", "raw", "--raw-stdout",
                     "{pad:190}{secret} not json", env=env,
                     secrets=[(secret, "SHIM_SECRET")])
        message = caught.exception.message
        self.assertIn("not one JSON document", message)
        for i in range(len(secret) - 4):
            self.assertNotIn(secret[i:i + 5], message)

    def test_a_secret_cut_by_the_stderr_window_does_not_leak_its_tail(self):
        env = dict(_exec.child_env([]), SHIM_SECRET=SECRET)
        with mock.patch.object(_exec, "STDERR_KEEP_BYTES", 40), \
                self.assertRaises(_exec.ExecFailure) as caught:
            # The 40-byte window starts 10 characters before the
            # secret's end.
            run_shim("--misbehave", "raw", "--raw-stderr",
                     "{pad:100}{secret}{pad:30}", "--code", "1", env=env,
                     secrets=[(SECRET, "SHIM_SECRET")])
        message = caught.exception.message
        self.assertIn("exited 1", message)
        self.assertNotIn(SECRET[-6:], message)

    @unittest.skipUnless(hasattr(sys, "set_int_max_str_digits"),
                         "the int-digit limit is 3.11+")
    def test_an_integer_past_the_digit_limit_is_transport(self):
        with self.assertRaises(_exec.ExecFailure) as caught:
            run_shim("--misbehave", "raw", "--raw-stdout",
                     '{"text": "x", "n": 1' + "0" * 5000 + "}")
        self.assertEqual(caught.exception.kind, "transport")
        self.assertIn("not one JSON document", caught.exception.message)

    def test_a_secret_in_stderr_is_scrubbed(self):
        env = dict(_exec.child_env([]), SHIM_SECRET=SECRET)
        with self.assertRaises(_exec.ExecFailure) as caught:
            run_shim("--misbehave", "echo-secret", env=env,
                     secrets=[(SECRET, "SHIM_SECRET")])
        self.assertIn("auth failed for ${SHIM_SECRET}",
                      caught.exception.message)
        self.assertNotIn(SECRET, caught.exception.message)


@unittest.skipUnless(POSIX, "setsid and process groups")
class TestDetachedGrandchild(unittest.TestCase):
    """A process that left the call's group (setsid) and holds its pipes
    may outlive the call, but must never hang it: run() is bounded by
    timeout + DRAIN_GRACE_S on every path (review of #39)."""

    def setUp(self):
        self.pidfile = pathlib.Path(tempdir(self)) / "pid"
        self.addCleanup(self.kill_grandchild)

    def kill_grandchild(self):
        if self.pidfile.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(self.pidfile.read_text(encoding="utf-8")),
                        signal.SIGKILL)

    def timed(self, *flags, timeout=10):
        started = time.monotonic()
        try:
            return run_shim("--pidfile", str(self.pidfile), *flags,
                            timeout=timeout), time.monotonic() - started
        except _exec.ExecFailure as exc:
            return exc, time.monotonic() - started

    def test_exit_with_stdout_held_is_bounded_and_no_answer(self):
        outcome, wall = self.timed("--detach", "both")
        # The grandchild holds the pipes for 30 s: anything well under
        # that is the bound working, with room for a loaded runner.
        self.assertLess(wall, _exec.DRAIN_GRACE_S + 12, wall)
        self.assertIsInstance(outcome, _exec.ExecFailure)
        self.assertEqual(outcome.kind, "transport")
        self.assertIn("stdout was still open", outcome.message)

    def test_timeout_with_pipes_held_is_bounded(self):
        outcome, wall = self.timed("--detach", "both", "--hang", timeout=1)
        self.assertLess(wall, 1 + _exec.DRAIN_GRACE_S + 12, wall)
        self.assertIsInstance(outcome, _exec.ExecFailure)
        self.assertEqual(outcome.kind, "timeout")

    def test_stderr_held_still_answers_with_latency_at_exit(self):
        outcome, wall = self.timed("--detach", "stderr")
        self.assertLess(wall, _exec.DRAIN_GRACE_S + 12, wall)
        self.assertIsInstance(outcome, dict, outcome)
        self.assertEqual(outcome["body"]["text"], "ok: hi")
        # Measured at exit, not after the grace the held stderr cost.
        self.assertLess(outcome["latency_s"], _exec.DRAIN_GRACE_S)


@unittest.skipUnless(POSIX, "signals and process groups")
class TestSigterm(unittest.TestCase):
    """A SIGTERM to the runner mid-call kills the call's group first, then
    still ends the runner by SIGTERM (#39 review)."""

    def test_the_call_is_killed_and_the_runner_dies_by_sigterm(self):
        pidfile = pathlib.Path(tempdir(self)) / "pid"
        code = (
            "import sys; sys.path.insert(0, {scripts!r}); import _exec; "
            "_exec.run({argv!r}, sys.executable, {cwd!r}, "
            "_exec.child_env([]), {{'message': 'hi'}}, 60)").format(
                scripts=str(SCRIPTS),
                argv=shim_argv("--misbehave", "sleep", "--pidfile",
                               str(pidfile)),
                cwd=str(SHIM.parent))
        runner = subprocess.Popen([sys.executable, "-c", code])
        self.addCleanup(runner.kill)
        deadline = time.monotonic() + 30
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pidfile.exists(), "the shim never started")
        grandchild = int(pidfile.read_text(encoding="utf-8"))
        runner.send_signal(signal.SIGTERM)
        self.assertEqual(runner.wait(timeout=30), -signal.SIGTERM)
        # The shim's own child, in the call's group, went with it.
        self.assertTrue(gone(grandchild, within_s=20))
        with contextlib.suppress(ProcessLookupError):
            os.kill(grandchild, signal.SIGKILL)


class ExecRunnerCase(RunnerCase):
    """A run whose app is exec_shim.py, copied into an app root."""

    def setUp(self):
        super().setUp()
        self.app_root = self.tmp / "app"
        self.app_root.mkdir()
        shutil.copy(SHIM, self.app_root / "exec_shim.py")
        self.log = self.tmp / "calls.jsonl"

    def exec_plan(self, *flags, cases=None, tools=False, env=None,
                  **invocation):
        plan = make_plan(self.state, "http://unused.invalid",
                         cases=cases or [make_case("c-0001")])
        plan["adapter"]["app"] = {"name": "shim", "repo": str(self.app_root)}
        plan["adapter"]["invocation"] = dict({
            "mode": "exec",
            # Relative to the state location (tmp/.evalup), as discover
            # writes it: the app root is a sibling here, `..` in-tree.
            "exec": {"argv": [sys.executable, "exec_shim.py",
                              "--log", str(self.log), *flags],
                     "cwd": "../app", "env": env or []},
            "request_body": '{"message": "<user turn>", '
                            '"session_id": "<uuid>"}',
            "response_body": '{"text": "<answer>"}',
            "timeout_s": 5, "max_concurrency": 1}, **invocation)
        if tools:
            plan["adapter"]["traces"] = {"source": "response"}
            plan["capability_matrix"] = {
                "routing": {"enabled": True},
                "trajectory": {"enabled": True},
                "tool_selection": {"enabled": True},
                "answer_quality": {"enabled": True,
                                   "judged": "provisional"}}
        return plan

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in
                self.log.read_text(encoding="utf-8").splitlines()]

    def refused(self, plan, fragment, env=None):
        rc, payload, proc = self.invoke(plan, env=env)
        self.assertEqual(rc, 3, proc.stdout + proc.stderr)
        self.assertIn(fragment, payload["error"])
        self.assertEqual(self.calls(), [], "pre-flight must call nothing")
        return payload


class TestEndToEnd(ExecRunnerCase):
    def test_answer_and_tool_layers_score_through_exec(self):
        case = make_case("c-0001")
        case["expect"]["answer"] = {"must_contain": ["ok: how many?"]}
        case["expect"]["tools"] = {"subset": ["list_invoices"]}
        case["expect"]["args"] = {"list_invoices": {"unit_id": "U-1"}}
        rc, _, proc = self.invoke(self.exec_plan("--tools", cases=[case],
                                                 tools=True))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        for name in ("http", "answer", "trajectory", "tool_selection"):
            self.assertEqual(verdict["layers"][name]["verdict"], "pass",
                             (name, verdict["layers"][name]))
        self.assertEqual(verdict["verdict"], "pass")
        trajectory = self.read("cases", "c-0001", "trajectory.json")
        self.assertEqual(trajectory["trajectory"]["tool_calls"][0]["args"],
                         {"unit_id": "U-1"})
        request = self.read("cases", "c-0001", "request.json")
        self.assertEqual(request["method"], "EXEC")
        self.assertEqual(request["headers_sent"], {})
        self.assertTrue(request["url"].startswith("exec:"))
        self.assertEqual(request["body"]["message"], "how many?")
        # What the command read on stdin is the rendered request body.
        (sent,) = self.calls()
        self.assertEqual(sent["message"], "how many?")
        self.assertEqual(len(sent["session_id"]), 36)
        manifest = self.read("manifest.yaml")
        self.assertEqual(manifest["invocation"]["mode"], "exec")
        self.assertTrue(manifest["invocation"]["timeout_enforced"])
        self.assertEqual(manifest["invocation"]["exec"]["cwd"],
                         str(self.app_root))
        self.assertEqual(manifest["traces"]["source"], "response")

    def test_a_wrong_tool_fails_and_a_wrong_answer_fails(self):
        case = make_case("c-0001")
        case["expect"]["answer"] = {"must_contain": ["nowhere"]}
        case["expect"]["tools"] = {"subset": ["get_rooms"]}
        rc, _, proc = self.invoke(self.exec_plan("--tools", cases=[case],
                                                 tools=True))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layers = self.read("cases", "c-0001", "verdict.json")["layers"]
        self.assertEqual(layers["answer"]["verdict"], "fail")
        self.assertEqual(layers["trajectory"]["verdict"], "fail")


class TestEnvPassthrough(ExecRunnerCase):
    def test_only_listed_names_reach_the_command(self):
        rc, _, proc = self.invoke(
            self.exec_plan(env=["SHIM_TOKEN"]),
            env={"SHIM_TOKEN": "t-123", "SHOULD_NOT_LEAK": "x"})
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        seen = self.read("cases", "c-0001", "response.json")["body"]["env"]
        self.assertIn("SHIM_TOKEN", seen)
        self.assertNotIn("SHOULD_NOT_LEAK", seen)
        self.assertNotIn("EVAL_USER_ID", seen)
        manifest = self.read("manifest.yaml")
        self.assertEqual(manifest["invocation"]["exec"]["env"],
                         ["SHIM_TOKEN"])
        self.assertNotIn("t-123", json.dumps(manifest))

    def test_an_unset_listed_name_is_exit_3_naming_it(self):
        payload = self.refused(self.exec_plan(env=["SHIM_TOKEN"]),
                               "unresolved env var(s)",
                               env={"SHIM_TOKEN": None})
        self.assertEqual(payload["missing_env"], ["SHIM_TOKEN"])

    def test_a_body_secret_goes_by_stdin_and_is_recorded_as_its_name(self):
        plan = self.exec_plan(request_body='{"message": "<user turn>", '
                                           '"key": "${SHIM_SECRET}"}')
        rc, _, proc = self.invoke(plan, env={"SHIM_SECRET": SECRET})
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.calls()[0]["key"], SECRET)
        request = self.read("cases", "c-0001", "request.json")
        self.assertEqual(request["body"]["key"], "${SHIM_SECRET}")

    def test_stderr_quoted_in_an_error_is_scrubbed(self):
        plan = self.exec_plan("--misbehave", "echo-secret",
                              env=["SHIM_SECRET"])
        rc, _, proc = self.invoke(plan, env={"SHIM_SECRET": SECRET})
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        text = (self.out_dir() / "cases" / "c-0001"
                / "response.json").read_text(encoding="utf-8")
        self.assertIn("auth failed for ${SHIM_SECRET}", text)
        self.assertNotIn(SECRET, text)


class TestFailuresAreInfra(ExecRunnerCase):
    def one(self, *flags, **execution):
        plan = self.exec_plan(*flags)
        plan["execution"].update(execution)
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        response = self.read("cases", "c-0001", "response.json")
        return verdict, response

    def test_a_non_zero_exit_is_infra_error_and_a_crash(self):
        verdict, response = self.one("--misbehave", "exit")
        self.assertEqual(verdict["verdict"], "infra_error")
        self.assertIn("exited 3", response["error"])
        self.assertIn("boom", response["error"])
        self.assertTrue(response["crashed"])
        self.assertEqual(response["attempts"], 2)   # retried like any infra

    def test_bad_json_is_infra_error_not_a_crash(self):
        verdict, response = self.one("--misbehave", "badjson")
        self.assertEqual(verdict["verdict"], "infra_error")
        self.assertIn("not one JSON document", response["error"])
        self.assertFalse(response["crashed"])

    @unittest.skipUnless(POSIX, "process groups")
    def test_a_timeout_is_infra_error(self):
        verdict, response = self.one("--misbehave", "sleep", timeout_s=1,
                                     max_attempts=1, backoff_s=[])
        self.assertEqual(verdict["verdict"], "infra_error")
        self.assertIn("timed out after 1s", response["error"])


class TestPreflight(ExecRunnerCase):
    def test_refusals(self):
        argv = [sys.executable, "exec_shim.py"]
        cases = [
            ({"exec": {"argv": " ".join(argv), "cwd": "../app"}},
             "never a shell string"),
            ({"exec": {"argv": [*argv, "--k", "${SHIM_SECRET}"],
                       "cwd": "../app"}}, "holds a ${REF}"),
            ({"exec": {"argv": argv}}, "exec.cwd is required"),
            ({"exec": {"argv": argv, "cwd": "missing"}}, "is not a directory"),
            ({"exec": {"argv": ["./no-such-shim"], "cwd": "../app"}},
             "is not an executable file"),
            ({"exec": {"argv": ["no-such-command-39"], "cwd": "../app"}},
             "is not an executable file"),
            ({"auth": {"type": "bearer", "token_env": "SHIM_SECRET"}},
             "list the credential's env var in exec.env (exec.env: "
             "[SHIM_SECRET])"),
            ({"headers": {"Origin": "http://x"}}, "needs invocation.mode: http"),
            ({"identity_map": {"role": "X-Role"}}, "identity_map maps to"),
            ({"health_check": {"path": "/"}}, "health_check needs"),
            ({"route_from_status": {"403": "denied"}},
             "route_from_status needs invocation.mode: http"),
            ({"response_format": "sse", "sse": {"protocol": "ag-ui"}},
             "response_format applies to invocation.mode: http only"),
            ({"response_body": '{"text": "x"}'}, "must mark the answer"),
            ({"conversation": {"style": "cookie"}}, "style: cookie needs"),
        ]
        for invocation, fragment in cases:
            with self.subTest(fragment):
                self.refused(self.exec_plan(**invocation), fragment,
                             env={"SHIM_SECRET": SECRET})

    def test_a_status_exec_cannot_report_skips_the_case(self):
        refused = make_case("c-0001", expect={"http": {"status": 403}})
        answered = make_case("c-0002")      # expect.http.status 200
        rc, _, proc = self.invoke(self.exec_plan(cases=[refused, answered]))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["verdict"], "skipped")
        self.assertIn("expect.http.status 403 cannot be observed",
                      json.dumps(verdict))
        self.assertEqual(self.read("cases", "c-0002",
                                   "verdict.json")["verdict"], "pass")
        self.assertEqual(len(self.calls()), 1)

    def test_a_trace_store_is_refused_with_one_message(self):
        plan = self.exec_plan()
        plan["adapter"]["traces"] = {"source": "otlp-file",
                                     "location": "x.jsonl",
                                     "correlation": "traceparent-echo",
                                     "convention": "gen_ai"}
        payload = self.refused(plan, "traces.source 'otlp-file' cannot be "
                                     "joined in invocation.mode: exec")
        self.assertIn("traces.source: response", payload["error"])

    def test_cwd_is_relative_to_the_state_location_not_app_repo(self):
        # app.repo is `.` (the state dir) here, as the templates write it;
        # exec.cwd still resolves against the state location alone.
        (self.app_root / "sub").mkdir()
        shutil.move(str(self.app_root / "exec_shim.py"),
                    str(self.app_root / "sub" / "exec_shim.py"))
        plan = self.exec_plan()
        plan["adapter"]["app"]["repo"] = "."
        plan["adapter"]["invocation"]["exec"]["cwd"] = "../app/sub"
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        body = self.read("cases", "c-0001", "response.json")["body"]
        # realpath: a macOS temp dir is reached through a symlink.
        self.assertEqual(os.path.realpath(body["cwd"]),
                         os.path.realpath(self.app_root / "sub"))


class TestConversation(ExecRunnerCase):
    def test_client_id_keeps_one_session_id(self):
        plan = self.exec_plan(cases=[conversation_case()])
        plan["adapter"]["invocation"]["conversation"] = {"style": "client-id"}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-convo-01", "verdict.json")
        self.assertEqual(verdict["verdict"], "pass")
        self.assertEqual(verdict["turns_sent"], 2)
        sids = [call["session_id"] for call in self.calls()]
        self.assertEqual(len(sids), 2)
        self.assertEqual(sids[0], sids[1])

    def test_server_id_reads_the_session_off_stdout(self):
        plan = self.exec_plan(
            cases=[conversation_case()],
            request_body='{"message": "<user turn>", '
                         '"conversation": "<session>"}',
            conversation={"style": "server-id",
                          "session_from": {"body": "conversationId"}})
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        first, second = self.calls()
        self.assertNotIn("conversation", first)
        self.assertEqual(second["conversation"], "conv-1")


class TestPacingAndDryRun(ExecRunnerCase):
    def test_every_call_is_paced(self):
        plan = self.exec_plan(cases=[make_case(f"c-000{i}")
                                     for i in range(1, 4)],
                              rate_limit={"min_interval_s": 0.4})
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        times = [call["_t"] for call in self.calls()]
        self.assertEqual(len(times), 3)
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later - earlier, 0.4)

    def test_a_dry_run_starts_nothing_and_counts_calls(self):
        plan = self.exec_plan(cases=[make_case("c-0001"),
                                     conversation_case()],
                              conversation={"style": "client-id"})
        rc, payload, proc = self.invoke(plan, extra_argv=["--dry-run"])
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(payload["invocation"]["mode"], "exec")
        self.assertTrue(payload["invocation"]["url"].startswith("exec:"))
        self.assertEqual(payload["app_calls_planned"], 3)
        self.assertEqual(payload["app_calls_max"], 6)

    def test_a_dry_run_still_resolves_the_command(self):
        plan = self.exec_plan()
        plan["adapter"]["invocation"]["exec"]["argv"] = ["no-such-cmd-39"]
        rc, payload, _ = self.invoke(plan, extra_argv=["--dry-run"])
        self.assertEqual(rc, 3)
        self.assertIn("is not an executable file", payload["error"])


class TestProbe(ExecRunnerCase):
    def probe(self, plan, env=None):
        # In the state location, as discover writes it: exec.cwd is
        # relative to this file's directory.
        path = self.state / "adapter.json"
        path.write_text(json.dumps(plan["adapter"]), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--probe", "--adapter", str(path),
             "--message", "hi"],
            env=dict(os.environ, **(env or {})), capture_output=True,
            text=True, check=False)
        return proc.returncode, json.loads(proc.stdout), proc

    def test_one_call_answer_tool_calls_and_scrubbed_env(self):
        plan = self.exec_plan("--tools", "--misbehave", "leak", tools=True,
                              env=["SHIM_SECRET"])
        rc, report, proc = self.probe(plan, env={"SHIM_SECRET": SECRET})
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["response"]["answer"],
                         "ok: hi (token ${SHIM_SECRET})")
        self.assertEqual(report["response"]["tool_calls"][0]["name"],
                         "list_invoices")
        self.assertIn("SHIM_SECRET", report["env_vars"])
        self.assertEqual(report["request"]["method"], "EXEC")
        # Where the command ran, resolved, so a wrong cwd is obvious.
        self.assertEqual(os.path.realpath(report["request"]["cwd"]),
                         os.path.realpath(self.app_root))
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse((self.state / "reports" / "probe").exists())

    def test_a_short_plain_setting_is_not_scrubbed(self):
        # exec.env carries settings too: a "1" scrubbed at any length would
        # rewrite the report's own keys and numbers.
        plan = self.exec_plan("--misbehave", "exit", env=["DEBUG"])
        rc, report, proc = self.probe(plan, env={"DEBUG": "3"})
        self.assertEqual(rc, 3, proc.stdout)
        self.assertIn("ok", report)
        self.assertIn("exited 3", report["error"])
        self.assertIn("DEBUG", report["env_vars"])

    def test_a_failing_command_is_a_named_problem(self):
        rc, report, _ = self.probe(self.exec_plan("--misbehave", "exit"))
        self.assertEqual(rc, 3)
        self.assertFalse(report["ok"])
        self.assertIn("exited 3", report["error"])


if __name__ == "__main__":
    unittest.main()
