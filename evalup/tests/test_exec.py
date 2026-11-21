"""invocation.mode: exec (#39, option 2): scripts/_exec.py on its own, then
the runner -- a run, a conversation, the dry run, pacing and --probe --
against tests/exec_shim.py, a stdlib stand-in for an app's own shim.
"""
import json
import os
import pathlib
import shutil
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
        self.assertEqual(_exec.check_config({"argv": ["./shim"]}), [])

    def test_defects(self):
        cases = [
            (None, "must be an object"),
            ({"argv": "node shim.js"}, "never a shell string"),
            ({"argv": []}, "non-empty list"),
            ({"argv": [""]}, "non-empty list"),
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

    def test_cwd_resolves_against_its_base(self):
        self.assertEqual(_exec.resolve_cwd(None, "/app"), "/app")
        self.assertEqual(_exec.resolve_cwd("tools/../shim", "/app"),
                         os.path.normpath("/app/shim"))
        self.assertEqual(_exec.resolve_cwd("/elsewhere", "/app"),
                         os.path.normpath("/elsewhere"))

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
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertIn("timed out after 1.5s", caught.exception.message)
        # The grandchild the shim started is dead too: nothing outlives a
        # call, and nothing holds its pipes open.
        self.assertTrue(gone(int(pidfile.read_text(encoding="utf-8"))))

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

    def test_a_secret_in_stderr_is_scrubbed(self):
        env = dict(_exec.child_env([]), SHIM_SECRET=SECRET)
        with self.assertRaises(_exec.ExecFailure) as caught:
            run_shim("--misbehave", "echo-secret", env=env,
                     secrets=[(SECRET, "SHIM_SECRET")])
        self.assertIn("auth failed for ${SHIM_SECRET}",
                      caught.exception.message)
        self.assertNotIn(SECRET, caught.exception.message)


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
            "exec": {"argv": [sys.executable, "exec_shim.py",
                              "--log", str(self.log), *flags],
                     "env": env or []},
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
            ({"exec": {"argv": " ".join(argv)}}, "never a shell string"),
            ({"exec": {"argv": [*argv, "--k", "${SHIM_SECRET}"]}},
             "holds a ${REF}"),
            ({"exec": {"argv": argv, "cwd": "missing"}}, "is not a directory"),
            ({"exec": {"argv": ["./no-such-shim"]}},
             "is not an executable file"),
            ({"exec": {"argv": ["no-such-command-39"]}},
             "is not an executable file"),
            ({"auth": {"type": "bearer", "token_env": "SHIM_SECRET"}},
             "pass a credential to an exec command by NAME"),
            ({"headers": {"Origin": "http://x"}}, "needs invocation.mode: http"),
            ({"identity_map": {"role": "X-Role"}}, "identity_map maps to"),
            ({"health_check": {"path": "/"}}, "health_check needs"),
            ({"response_format": "sse", "sse": {"protocol": "ag-ui"}},
             "response_format applies to invocation.mode: http only"),
            ({"response_body": '{"text": "x"}'}, "must mark the answer"),
            ({"conversation": {"style": "cookie"}}, "style: cookie needs"),
        ]
        for invocation, fragment in cases:
            with self.subTest(fragment):
                self.refused(self.exec_plan(**invocation), fragment,
                             env={"SHIM_SECRET": SECRET})

    def test_cwd_is_relative_to_the_app_root(self):
        (self.app_root / "sub").mkdir()
        shutil.move(str(self.app_root / "exec_shim.py"),
                    str(self.app_root / "sub" / "exec_shim.py"))
        plan = self.exec_plan()
        plan["adapter"]["invocation"]["exec"]["cwd"] = "sub"
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
        path = self.tmp / "adapter.json"
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
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse((self.state / "reports" / "probe").exists())

    def test_a_failing_command_is_a_named_problem(self):
        rc, report, _ = self.probe(self.exec_plan("--misbehave", "exit"))
        self.assertEqual(rc, 3)
        self.assertFalse(report["ok"])
        self.assertIn("exited 3", report["error"])


if __name__ == "__main__":
    unittest.main()
