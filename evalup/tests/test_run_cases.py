#!/usr/bin/env python3
"""Tests for scripts/run_cases.py, the execution engine (docs/runner-contract.md).

Stdlib only (unittest + subprocess + http.server), matching the scripts
themselves, and the runner is driven as a REAL subprocess because its contract
with the calling skill is argv + stdout JSON + exit code, not a Python API.
The app under test is a fake HTTP app in a thread: every behaviour these tests
assert (a 500 storm, a deliberate 400, a hang) is a behaviour a real app has,
and none of them should need a real app to reproduce.

The two tests that matter most are TestCompleteness's pair (the REQUIRED_*
tables, asserted against the contract file itself rather than restated) and
TestVerify.test_the_shipped_run_is_the_regression_test (the audit finding, as
a check anyone can re-run).

Run: python3 -m unittest discover -s tests -v   (from the plugin root)
"""
import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
RUNNER = SCRIPTS / "run_cases.py"
CONTRACT = SCRIPTS.parent / "docs" / "runner-contract.md"
RUN_ID = "smoke-20260908T120000Z"

sys.path.insert(0, str(SCRIPTS))
import run_cases  # noqa: E402  - imported for its declared constants only


def contract_block(after, fence="```"):
    """The first fenced block following `after` in the contract."""
    text = CONTRACT.read_text(encoding="utf-8")
    start = text.index(after)
    open_at = text.index(fence, start)
    open_at = text.index("\n", open_at) + 1
    return text[open_at:text.index(fence, open_at)]


def exec_contract_block(after):
    """Execute a python block out of the contract and return its namespace.

    The contract says "where it and the code disagree, this file is right" --
    so the test reads the table from the file rather than restating it, and a
    doc edit that the code has not followed fails the suite.
    """
    namespace = {}
    exec(compile(contract_block(after), str(CONTRACT), "exec"), namespace)
    return {k: v for k, v in namespace.items() if not k.startswith("__")}


def contract_tree_conditions():
    """SS6's tree, as {filename: condition} off the `# only when:` markers."""
    block = contract_block("## 6. The output tree")
    found = {}
    for line in block.splitlines():
        match = re.match(r"\s*([\w.]+)\s+#\s*only when:\s*(.+?)\s*$", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


class _Handler(http.server.BaseHTTPRequestHandler):
    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        status, payload, headers = self.server.responder(
            self.path, body, dict(self.headers))
        if status is None:
            # Drop the connection without answering: the client sees
            # http.client.RemoteDisconnected, which is what an app crashing
            # mid-request actually looks like from the outside.
            self.close_connection = True
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _serve
    do_POST = _serve

    def log_message(self, *args):
        """Silence: the stdlib handler logs every request to stderr, which
        would bury the assertion output of a failing test."""


class FakeApp:
    """A real HTTP server on a real port, so urllib is exercised end to end."""

    def __init__(self, responder):
        self.calls = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      _Handler)

        def wrapped(path, body, headers):
            self.calls.append({"path": path, "body": body, "headers": headers})
            return responder(path, body, headers)

        self.server.responder = wrapped
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def ok_responder(path, body, headers):
    return 200, {"message": "ok"}, {}


def make_case(case_id, **overrides):
    case = {
        "id": case_id,
        "unit": "billing",
        "split": ["smoke", "full"],
        "category": "happy",
        "test_type": "MFT",
        "template_id": None,
        "input": {"messages": [{"role": "user", "content": "how many?"}]},
        "expect": {"http": {"status": 200},
                   "answer": {"must_contain": ["ok"]}},
        "gating": True,
    }
    case.update(overrides)
    return case


def make_adapter(base_url, **overrides):
    adapter = {
        "app": {"name": "fake-app"},
        "invocation": {
            "mode": "http",
            "base_url": base_url,
            "endpoint": "POST /api/chat/ask",
            "request_body": '{"sessionId": "<uuid>", "message": "<user turn>"}',
            "response_body": '{"message": "<answer>"}',
            "auth": {"type": "headers",
                     "headers": {"X-User-Id": "${EVAL_USER_ID}"}},
            "timeout_s": 5,
            "max_concurrency": 1,
        },
        "traces": {"source": "view-only", "convention": "gen_ai",
                   "correlation": "none"},
        "environment": {"kind": "live-readonly", "safe_to_attack": False},
        "tools": [],
        "data": {"may_contain_pii": False},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(adapter.get(key), dict):
            adapter[key] = dict(adapter[key], **value)
        else:
            adapter[key] = value
    return adapter


def make_plan(state_dir, base_url, cases=None, **overrides):
    plan = {
        "plan_version": 1,
        "run_id": RUN_ID,
        "mode": "smoke",
        "k": 1,
        "gate": "soft",
        "selecting_split": "smoke",
        "paths": {"scripts_dir": str(SCRIPTS), "state_dir": str(state_dir),
                  "holdout_ledger": "datasets/holdout-looks.jsonl"},
        "adapter": make_adapter(base_url),
        "capability_matrix": {
            "routing": {"enabled": True},
            "trajectory": {"enabled": False,
                           "blocked_by": "no trace-id correlation"},
            "answer_quality": {"enabled": True, "judged": "provisional"},
        },
        "cases": cases if cases is not None else [make_case("c-0001")],
        "execution": {"timeout_s": 5, "max_attempts": 2, "backoff_s": [0],
                      "infra_rate_abort": 0.25, "insecure_tls": False},
        "manifest_extra": {"dataset_version": 1,
                           "judge": {"model": None, "status": "uncalibrated"}},
    }
    plan.update(overrides)
    return plan


class RunnerCase(unittest.TestCase):
    """Shared fixture: a temp state dir, a fake app, and one invocation."""

    responder = staticmethod(ok_responder)

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / ".evalup"
        (self.state / "reports").mkdir(parents=True)
        self.app = FakeApp(self.responder)
        self.addCleanup(self.app.close)

    def out_dir(self, run_id=RUN_ID):
        return self.state / "reports" / run_id

    def invoke(self, plan, out=None, extra_argv=(), env=None, wait=True):
        plan_path = self.tmp / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        environ = dict(os.environ, EVAL_USER_ID="1")
        for key in (env or {}):
            if env[key] is None:
                environ.pop(key, None)
            else:
                environ[key] = env[key]
        argv = [sys.executable, str(RUNNER), "--plan", str(plan_path),
                "--out", str(out or self.out_dir(plan["run_id"])),
                *extra_argv]
        if not wait:
            return subprocess.Popen(argv, env=environ,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        proc = subprocess.run(argv, env=environ, capture_output=True,
                              text=True)
        try:
            payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        except json.JSONDecodeError:
            payload = None
        return proc.returncode, payload, proc

    def read(self, *parts):
        return json.loads((self.out_dir().joinpath(*parts))
                          .read_text(encoding="utf-8"))

    def jsonl(self, name):
        text = (self.out_dir() / name).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestPlanValidation(RunnerCase):
    """SS2. Every one of these is exit 2, before any spend, on stdout."""

    def assert_rejected(self, plan, fragment, code=2):
        rc, payload, proc = self.invoke(plan)
        self.assertEqual(rc, code, proc.stdout + proc.stderr)
        self.assertIsNotNone(payload, "non-zero exit must print JSON on stdout")
        self.assertIn(fragment, payload["error"])
        return payload

    def test_unknown_top_level_key_is_fatal(self):
        # Not ignored: a typo'd key must not silently disable a layer.
        plan = make_plan(self.state, self.app.base_url)
        plan["scorring"] = {}
        self.assert_rejected(plan, "unknown top-level key(s)")

    def test_missing_required_key(self):
        plan = make_plan(self.state, self.app.base_url)
        del plan["capability_matrix"]
        self.assert_rejected(plan, "missing required key(s): capability_matrix")

    def test_plan_version(self):
        plan = make_plan(self.state, self.app.base_url, plan_version=2)
        self.assert_rejected(plan, "plan_version must be 1")

    def test_run_id_shape(self):
        plan = make_plan(self.state, self.app.base_url, run_id="smoke-run-1")
        self.assert_rejected(plan, "run_id must match")

    def test_out_basename_must_equal_run_id(self):
        plan = make_plan(self.state, self.app.base_url)
        rc, payload, _ = self.invoke(plan, out=self.state / "reports" / "other")
        self.assertEqual(rc, 2)
        self.assertIn("--out basename must equal run_id", payload["error"])

    def test_k_below_one(self):
        plan = make_plan(self.state, self.app.base_url, k=0)
        self.assert_rejected(plan, "k must be an integer >= 1")

    def test_empty_cases(self):
        plan = make_plan(self.state, self.app.base_url, cases=[])
        self.assert_rejected(plan, "cases must be a non-empty list")

    def test_duplicate_case_id(self):
        # Two cases would collide in cases/<case-id>/ and the run would report
        # N cases from N-1 results.
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001"), make_case("c-0001")])
        self.assert_rejected(plan, "duplicate case id 'c-0001'")

    def test_case_id_that_is_not_a_directory_name(self):
        # The id becomes cases/<id>/. `../..` and an absolute path (which
        # os.path.join lets replace the whole prefix) both wrote six files
        # outside --out before exit 6 noticed (2026-09 audit). Exit 2 writes
        # nothing at all, which assert_rejected checks.
        escaped = pathlib.Path(self.state).parent / "ESCAPED"
        for bad in ("../../../../ESCAPED", str(escaped), "a/b", "a\\b", "..",
                    ".hidden", "has space", "x" * 129):
            with self.subTest(case_id=bad):
                plan = make_plan(self.state, self.app.base_url,
                                 cases=[make_case(bad)])
                self.assert_rejected(plan, "names the cases/<id>/ directory")
        self.assertFalse(escaped.exists())

    def test_case_not_in_selecting_split(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001", split=["full"])])
        self.assert_rejected(plan, "does not contain selecting_split 'smoke'")

    def test_backoff_length_must_match_attempts(self):
        plan = make_plan(self.state, self.app.base_url)
        plan["execution"]["max_attempts"] = 3
        plan["execution"]["backoff_s"] = [1]
        self.assert_rejected(plan, "exactly max_attempts-1 = 2 entries")

    def test_unknown_execution_key(self):
        plan = make_plan(self.state, self.app.base_url)
        plan["execution"]["retries"] = 3
        self.assert_rejected(plan, "unknown key(s) in execution: retries")

    def test_scripts_dir_must_hold_every_scorer(self):
        empty = self.tmp / "no-scripts"
        empty.mkdir()
        plan = make_plan(self.state, self.app.base_url)
        plan["paths"]["scripts_dir"] = str(empty)
        payload = self.assert_rejected(plan, "missing scorer script(s)")
        self.assertIn("score_routing.py", payload["error"])

    def test_holdout_mode_without_ledger(self):
        # An uncounted holdout run makes the N=5 reseal trigger a number
        # nobody is keeping.
        plan = make_plan(self.state, self.app.base_url,
                         run_id="holdout-20260908T120000Z", mode="holdout",
                         selecting_split="holdout",
                         cases=[make_case("c-0001", split=["holdout"])])
        plan["paths"]["holdout_ledger"] = None
        self.assert_rejected(plan, "paths.holdout_ledger is null")

    def test_max_concurrency_is_refused_not_downgraded(self):
        """Decision D5: refuse, rather than warn and serialize.

        A warning scrolls past and leaves the adapter field looking supported
        while every run is serial.
        """
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["invocation"]["max_concurrency"] = 4
        self.assert_rejected(plan, "max_concurrency is 4")
        self.assertEqual(self.app.calls, [], "refusal must precede any call")

    def test_nothing_is_written_on_a_rejected_plan(self):
        plan = make_plan(self.state, self.app.base_url, k=0)
        self.invoke(plan)
        self.assertFalse(self.out_dir().exists())


class TestEnvAndSecrets(RunnerCase):
    """SS3. Adapter hard rule 4, and the rule that no secret reaches disk."""

    def test_every_missing_env_var_is_named_at_once(self):
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["invocation"]["auth"]["headers"] = {
            "X-User-Id": "${EVAL_USER_ID}",
            "X-Role-Id": "${EVAL_ROLE_ID}",
            "X-User-Permissions": "${EVAL_PERMS}",
        }
        rc, payload, _ = self.invoke(plan, env={"EVAL_USER_ID": None})
        self.assertEqual(rc, 3)
        # All three, not the first: discovering them one run at a time is the
        # complaint the field test's identity_headers note records.
        for name in ("EVAL_USER_ID", "EVAL_ROLE_ID", "EVAL_PERMS"):
            self.assertIn(name, payload["error"])
        self.assertEqual(payload["missing_env"],
                         ["EVAL_PERMS", "EVAL_ROLE_ID", "EVAL_USER_ID"])

    def test_bearer_token_env_is_required_and_never_written(self):
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["invocation"]["auth"] = {"type": "bearer",
                                                 "token_env": "APP_TOKEN"}
        rc, payload, _ = self.invoke(plan, env={"APP_TOKEN": None})
        self.assertEqual(rc, 3)
        self.assertIn("APP_TOKEN", payload["error"])

        rc, _, proc = self.invoke(plan, env={"APP_TOKEN": "s3cret"})
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        request = self.read("cases", "c-0001", "request.json")
        self.assertEqual(request["headers_sent"]["Authorization"],
                         "<redacted>")
        tree = (self.out_dir()).rglob("*")
        for path in tree:
            if path.is_file():
                self.assertNotIn("s3cret",
                                 path.read_text(encoding="utf-8",
                                                errors="replace"),
                                 f"secret leaked into {path.name}")

    def test_env_ref_in_request_body_is_never_written(self):
        # redact() only ever covered headers, so `${API_KEY}` inside
        # request_body was resolved, sent, and written verbatim into every
        # request.json (2026-09 audit). The app must still RECEIVE the value;
        # the record must carry the reference back instead.
        plan = make_plan(self.state, self.app.base_url)
        invocation = plan["adapter"]["invocation"]
        template = json.loads(invocation["request_body"])
        template["api_key"] = "${APP_BODY_KEY}"
        invocation["request_body"] = json.dumps(template)
        rc, _, proc = self.invoke(plan, env={"APP_BODY_KEY": "sk-body-s3cret"})
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.app.calls[-1]["body"]["api_key"],
                         "sk-body-s3cret")
        request = self.read("cases", "c-0001", "request.json")
        self.assertEqual(request["body"]["api_key"], "${APP_BODY_KEY}")
        for path in self.out_dir().rglob("*"):
            if path.is_file():
                self.assertNotIn("sk-body-s3cret",
                                 path.read_text(encoding="utf-8",
                                                errors="replace"),
                                 f"secret leaked into {path.name}")

    def test_header_value_from_env_is_redacted_but_named(self):
        plan = make_plan(self.state, self.app.base_url)
        rc, _, proc = self.invoke(plan, env={"EVAL_USER_ID": "42"})
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        request = self.read("cases", "c-0001", "request.json")
        self.assertEqual(request["headers_sent"]["X-User-Id"], "<redacted>")
        # ...and it really was sent, resolved, to the app.
        self.assertEqual(self.app.calls[-1]["headers"]["X-User-Id"], "42")
        # The manifest records the NAMES so a run is reproducible without the
        # credential.
        manifest = self.read("manifest.yaml")
        self.assertEqual(manifest["identity_headers"],
                         {"names": ["X-User-Id"], "source": "env"})
        self.assertEqual(request["headers_sent"]["Content-Type"],
                         "application/json")


def health_only(inner):
    """Wrap a POST responder so GET / still answers the pre-flight check."""
    def responder(path, body, headers):
        if path == "/":
            return 200, {"ok": True}, {}
        return inner(path, body, headers)
    return responder


class TestOutputTree(RunnerCase):
    """SS6 + SS9(a): what a completed run leaves on disk."""

    responder = staticmethod(health_only(ok_responder))

    def test_happy_run_writes_the_whole_tree(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001"), make_case("c-0002")])
        rc, payload, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertIsNone(payload, "exit 0 prints nothing on stdout")

        out = self.out_dir()
        for name in ("manifest.yaml", "results.json", "verdicts.jsonl",
                     "verdicts_for_stats.jsonl", "run.log"):
            self.assertTrue((out / name).is_file(), name)
        for case_id in ("c-0001", "c-0002"):
            for name in ("request.json", "response.json", "verdict.json",
                         "expect.json", "answer.txt"):
                self.assertTrue((out / "cases" / case_id / name).is_file(),
                                f"{case_id}/{name}")
        # Files this run had no reason to write are absent, not empty.
        for name in ("routing_results.jsonl", "repeats.jsonl",
                     "reliability.json"):
            self.assertFalse((out / name).exists(), name)
        self.assertFalse((out / "cases" / "c-0001" / "trace.json").exists())

    def test_no_temp_files_survive(self):
        """SS6: every write is temp-then-rename, and the temp name is gone."""
        plan = make_plan(self.state, self.app.base_url)
        self.invoke(plan)
        leftovers = [p.name for p in self.out_dir().rglob("*")
                     if ".tmp." in p.name]
        self.assertEqual(leftovers, [])

    def test_verdicts_jsonl_is_derived_from_the_case_dirs(self):
        """SS9(a). The shipped run's failure, made unreachable."""
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001"), make_case("c-0002")])
        self.invoke(plan)
        rows = self.jsonl("verdicts.jsonl")
        self.assertEqual([r["case_id"] for r in rows], ["c-0001", "c-0002"])
        for row in rows:
            self.assertEqual(row["set"], "smoke")
            self.assertEqual(row["verdict"], "pass")
            self.assertTrue(row["gating"])
            self.assertIsInstance(row["layers"], dict)
            # Flattened to verdict STRINGS here; the scorer objects live in the
            # case's own verdict.json.
            for value in row["layers"].values():
                self.assertIsInstance(value, str)
        self.assertTrue((self.out_dir() / "verdicts_for_stats.jsonl").is_file())
        self.assertEqual(self.jsonl("verdicts_for_stats.jsonl"),
                         [{"case_id": "c-0001", "verdict": "pass"},
                          {"case_id": "c-0002", "verdict": "pass"}])

    def test_results_json_summary(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001"), make_case("c-0002")])
        self.invoke(plan)
        results = self.read("results.json")
        self.assertEqual(results["run_id"], RUN_ID)
        self.assertEqual(results["dataset_version"], 1)
        self.assertEqual(results["exit_code"], 0)
        summary = results["summary"]
        self.assertEqual(summary["status"], "ok")
        self.assertEqual((summary["n"], summary["attempted"]), (2, 2))
        self.assertEqual((summary["passes"], summary["failures"]), (2, 0))
        self.assertEqual(summary["unscored"], 0)
        self.assertEqual(summary["infra_errors"], 0)
        self.assertEqual(summary["scorer_errors"], 0)
        self.assertEqual(summary["missing_artifacts"], [])
        # The capability matrix's disabled layers, plus the trace-less ones.
        self.assertIn("trajectory", summary["unscorable_layers"])
        self.assertEqual(summary["unjudged"], "mode: smoke")

    def test_manifest_records_what_makes_a_run_comparable(self):
        plan = make_plan(self.state, self.app.base_url)
        self.invoke(plan)
        manifest = self.read("manifest.yaml")
        self.assertEqual(manifest["run_id"], RUN_ID)
        self.assertEqual(manifest["selecting_split"], "smoke")
        self.assertEqual(manifest["runner_version"], 1)
        self.assertEqual(len(manifest["plan_sha256"]), 64)
        self.assertEqual([c["id"] for c in manifest["cases"]], ["c-0001"])
        self.assertEqual(len(manifest["cases"][0]["sha256"]), 64)
        self.assertFalse(manifest["traces"]["collected"])
        self.assertIn("trajectory", manifest["traces"]["disabled_layers"])
        # D1: JSON bytes in a .yaml file, so `yq -r '.run_id'` still reads it.
        self.assertTrue((self.out_dir() / "manifest.yaml").is_file())

    def test_verdict_json_shape(self):
        plan = make_plan(self.state, self.app.base_url)
        self.invoke(plan)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["case_id"], "c-0001")
        self.assertEqual(verdict["category"], "happy")
        self.assertFalse(verdict["canary"])
        self.assertFalse(verdict["holdout"])
        self.assertEqual(verdict["k"], 1)
        self.assertIsNone(verdict["repeats"])
        self.assertEqual(verdict["notes"], "")
        self.assertFalse(verdict["trace"]["expected"])
        self.assertEqual(verdict["trace"]["reason"],
                         "traces.source: view-only")
        # SS5: every layer in the table gets a row on every case. An absent
        # row and an `n/a` row are not the same statement.
        self.assertEqual(sorted(verdict["layers"]),
                         sorted(run_cases.LAYER_ORDER))
        self.assertEqual(verdict["layers"]["http"],
                         {"layer": "http", "verdict": "pass", "status": 200,
                          "expected": 200})
        # score_answer.py's own object, verbatim -- the runner invents no
        # fields and drops none.
        self.assertEqual(verdict["layers"]["answer"]["verdict"], "pass")
        self.assertEqual(verdict["layers"]["answer"]["layer"], "answer")
        # Applicable but disabled by the capability matrix: `unscorable`, with
        # blocked_by COPIED from the matrix, never composed here.
        self.assertEqual(verdict["layers"]["trajectory"],
                         {"layer": "trajectory", "verdict": "n/a"})
        self.assertEqual(verdict["layers"]["authz"],
                         {"layer": "authz", "verdict": "n/a"})

    def test_answer_and_expect_are_kept_for_rescoring(self):
        """D9: a run can be re-scored without re-invoking the app."""
        plan = make_plan(self.state, self.app.base_url)
        self.invoke(plan)
        answer = (self.out_dir() / "cases" / "c-0001"
                  / "answer.txt").read_text(encoding="utf-8")
        self.assertEqual(answer, "ok")
        self.assertEqual(self.read("cases", "c-0001", "expect.json"),
                         make_case("c-0001")["expect"])

    def test_request_body_template_is_rendered_from_the_adapter(self):
        plan = make_plan(self.state, self.app.base_url)
        self.invoke(plan)
        sent = self.app.calls[-1]["body"]
        self.assertEqual(sent["message"], "how many?")
        self.assertEqual(len(sent["sessionId"]), 36)   # a real uuid4


class TestPreflight(RunnerCase):
    """SS4. Every failure here is exit 3 with zero app calls billed."""

    responder = staticmethod(health_only(ok_responder))

    def test_unimplemented_invocation_mode(self):
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["invocation"]["mode"] = "cli"
        rc, payload, _ = self.invoke(plan)
        self.assertEqual(rc, 3)
        self.assertIn("http and function only", payload["error"])
        self.assertEqual(self.app.calls, [])

    def test_health_check_failure(self):
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["invocation"]["base_url"] = "http://127.0.0.1:1"
        rc, payload, _ = self.invoke(plan)
        self.assertEqual(rc, 3)
        self.assertIn("health check", payload["error"])

    def test_a_failed_health_check_never_prints_a_resolved_credential(self):
        # 2026-09-21 audit: every recording path used record_url and this
        # message used the RESOLVED base_url -- on stdout, so in CI logs and an
        # agent's transcript. SS3 covers a message as much as a file.
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["invocation"]["base_url"] = \
            "http://user:${APP_URL_TOKEN}@127.0.0.1:1"
        rc, payload, proc = self.invoke(plan,
                                        env={"APP_URL_TOKEN": "tok-s3cret"})
        self.assertEqual(rc, 3)
        self.assertIn("${APP_URL_TOKEN}", payload["error"])
        self.assertNotIn("tok-s3cret", proc.stdout + proc.stderr)

    def test_base_url_must_be_http_or_https(self):
        # urlopen also reads file://, so a base_url could put a local file in
        # every response.json. An allowlist, as in md_to_html.py.
        secret = self.state / "local-secret.txt"
        secret.write_text("LOCAL-FILE-CONTENT", encoding="utf-8")
        for base in (secret.as_uri(), "ftp://127.0.0.1/x", "127.0.0.1:8080"):
            with self.subTest(base=base):
                plan = make_plan(self.state, self.app.base_url)
                plan["adapter"]["invocation"]["base_url"] = base
                rc, payload, proc = self.invoke(plan)
                self.assertEqual(rc, 3, proc.stdout + proc.stderr)
                self.assertIn("http:// or https://", payload["error"])
                self.assertNotIn("LOCAL-FILE-CONTENT", proc.stdout)

    def test_declared_health_check_status_is_enforced(self):
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["invocation"]["health_check"] = {
            "method": "GET", "path": "/healthz", "expect_status": [204]}
        rc, payload, _ = self.invoke(plan)
        self.assertEqual(rc, 3)
        self.assertIn("expected one of [204]", payload["error"])

    def test_unimplemented_trace_store_is_refused_not_downgraded(self):
        """An unimplemented store must not masquerade as `no trace`.

        Silently treating it as trace-less would downgrade a fully instrumented
        app to trace-less scoring and report the result as normal.
        """
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["traces"] = {"source": "jaeger", "convention": "gen_ai",
                                     "correlation": "traceparent-echo"}
        rc, payload, _ = self.invoke(plan)
        self.assertEqual(rc, 3)
        self.assertIn("traces.source: otlp-file only", payload["error"])

    def test_non_gen_ai_convention_without_a_shim_is_trace_less(self):
        """Adapter hard rule 5: raw non-gen_ai spans never reach the
        normalizer, which would silently produce empty trajectories."""
        spans = self.tmp / "spans.json"
        spans.write_text("[]", encoding="utf-8")
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["traces"] = {"source": "otlp-file",
                                     "location": str(spans),
                                     "convention": "openinference",
                                     "correlation": "traceparent-echo",
                                     "mapping_shim": None}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        manifest = self.read("manifest.yaml")
        self.assertFalse(manifest["traces"]["collected"])
        self.assertIn("hard rule 5", manifest["traces"]["reason"])

    def test_gitignore_is_written_before_the_first_case(self):
        """SS4.6. Raw per-case material stays local when PII is possible."""
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["data"] = {"may_contain_pii": True}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            (self.state / "reports" / ".gitignore").read_text(
                encoding="utf-8"), "*/cases/\n")

    def test_dry_run_writes_nothing_and_calls_nothing(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001"), make_case("c-0002")])
        rc, payload, proc = self.invoke(plan, extra_argv=["--dry-run"])
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["app_calls_planned"], 2)
        self.assertEqual([c["id"] for c in payload["cases"]],
                         ["c-0001", "c-0002"])
        self.assertFalse(self.out_dir().exists())
        self.assertEqual(self.app.calls, [])


class TestSkipGates(RunnerCase):
    """SS7. Every one of these is a REFUSAL recorded as `skipped`, never a
    fail: scoring a case the harness declined to run would manufacture a
    failure the app never had."""

    responder = staticmethod(health_only(ok_responder))

    def skip_reason_for(self, case, **adapter_overrides):
        plan = make_plan(self.state, self.app.base_url, cases=[case])
        for key, value in adapter_overrides.items():
            plan["adapter"][key] = value
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", case["id"], "verdict.json")
        self.assertEqual(verdict["verdict"], "skipped")
        return verdict

    def test_adversarial_case_against_an_unsafe_environment(self):
        verdict = self.skip_reason_for(
            make_case("c-0001", category="adversarial-refusal"))
        self.assertIn("safe_to_attack is false",
                      verdict["layers"]["http"]["reason"])
        # ...and nothing was sent.
        self.assertEqual([c for c in self.app.calls if c["path"] != "/"], [])
        request = self.read("cases", "c-0001", "request.json")
        self.assertFalse(request["sent"])
        self.assertIsNone(request["sent_at"])
        # F-126: the contract marks BOTH files (SS6).
        response = self.read("cases", "c-0001", "response.json")
        self.assertIs(response["sent"], False)
        self.assertIsNone(response["sent_at"])

    def test_never_live_tool_in_a_live_environment(self):
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["send_email"]}
        verdict = self.skip_reason_for(
            case, tools=[{"name": "send_email", "side_effects": "never-live"}])
        self.assertIn("never-live tool(s) send_email",
                      verdict["layers"]["http"]["reason"])

    def multi_turn_case(self):
        return make_case("c-0001", input={"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "and then?"}]})

    def test_multi_turn_case_without_a_session_contract(self):
        verdict = self.skip_reason_for(self.multi_turn_case())
        self.assertIn("hard rule 3", verdict["layers"]["http"]["reason"])

    def test_multi_turn_case_skips_even_with_a_session_contract(self):
        """Multi-turn is RESERVED, so the gate ignores invocation.session.

        The old gate skipped only when no session contract was declared. An
        adapter that declared one (adapters/dotnet.md shipped that block)
        therefore ran the case single-turn -- last user message only, earlier
        turns dropped -- and scored the truncated conversation as a real
        verdict. Nothing drives start/send_turn/end, so the declaration could
        never have made the run correct.
        """
        plan = make_plan(self.state, self.app.base_url,
                         cases=[self.multi_turn_case()])
        plan["adapter"]["invocation"]["session"] = {
            "start": {"via": "POST /chat/session"},
            "send_turn": {"via": "POST /chat/session/{id}/turn"},
            "end": {"via": "DELETE /chat/session/{id}"}}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["verdict"], "skipped")
        self.assertIn("RESERVED", verdict["layers"]["http"]["reason"])
        # And nothing was sent: a truncated turn is not a cheaper datapoint.
        self.assertEqual([c for c in self.app.calls if c["path"] != "/"], [])

    def test_identity_case_without_an_identity_map(self):
        """Running it under the default identity would score the wrong
        persona and report the number as if it meant something."""
        verdict = self.skip_reason_for(
            make_case("c-0001", identity={"permissions": [30030]}))
        self.assertIn("identity_map", verdict["layers"]["http"]["reason"])

    def test_identity_map_sends_the_declared_headers(self):
        plan = make_plan(self.state, self.app.base_url, cases=[
            make_case("c-0001", identity={"permissions": [30030, 30011],
                                          "role": "manager"})])
        plan["adapter"]["invocation"]["identity_map"] = {
            "permissions": "X-User-Permissions", "role": "X-Role-Id"}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        sent = [c for c in self.app.calls if c["path"] != "/"][-1]["headers"]
        self.assertEqual(sent["X-User-Permissions"], "30030,30011")
        self.assertEqual(sent["X-Role-Id"], "manager")
        # Not an env-sourced header, so its value is recorded literally.
        request = self.read("cases", "c-0001", "request.json")
        self.assertEqual(request["headers_sent"]["X-Role-Id"], "manager")

    def test_skipped_cases_stay_out_of_the_denominators(self):
        plan = make_plan(self.state, self.app.base_url, cases=[
            make_case("c-0001"),
            make_case("c-0002", category="adversarial-refusal")])
        self.invoke(plan)
        summary = self.read("results.json")["summary"]
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["infra_rate"], 0.0)


class TestInfraTaxonomy(RunnerCase):
    """SS7. Infra verdicts never enter pass/fail denominators."""

    responder = staticmethod(health_only(
        lambda path, body, headers: (500, {"error": "boom"}, {})))

    def test_5xx_is_retried_then_recorded_as_infra_error(self):
        plan = make_plan(self.state, self.app.base_url)
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        response = self.read("cases", "c-0001", "response.json")
        self.assertEqual(response["attempts"], 2)     # max_attempts
        self.assertEqual(response["retry_count"], 1)
        self.assertEqual(response["status"], 500)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["verdict"], "infra_error")
        summary = self.read("results.json")["summary"]
        self.assertEqual(summary["infra_errors"], 1)
        self.assertEqual((summary["passes"], summary["failures"]), (0, 0))
        self.assertEqual(summary["infra_rate"], 1.0)

    def test_the_same_5xx_on_every_attempt_is_flagged_not_rescored(self):
        """F-165 (user test round 2): the app's own 500 and a throttled
        provider's 500 carried byte-identical bodies, so an exhausted 5xx
        stays `infra_error`. A case that got the SAME 5xx on every attempt of
        every repeat is flagged, counted, and named by the gate -- the one
        honest thing the runner can say about it."""
        plan = make_plan(self.state, self.app.base_url, k=2)
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["verdict"], "infra_error")
        self.assertEqual(verdict["repeated_5xx"],
                         {"status": 500, "attempts": 4})
        summary = self.read("results.json")["summary"]
        self.assertEqual(summary["repeated_5xx"], 1)
        verify = subprocess.run(
            [sys.executable, str(RUNNER), "--verify", str(self.out_dir())],
            capture_output=True, text=True)
        self.assertEqual(verify.returncode, 0, verify.stdout)
        gate = subprocess.run(
            [sys.executable, str(SCRIPTS / "gate.py"), str(self.out_dir())],
            capture_output=True, text=True)
        self.assertIn("1 infra case(s) got the same 5xx on every attempt",
                      gate.stdout)

    def test_a_5xx_that_clears_on_the_next_repeat_is_not_flagged(self):
        """The provider pattern from the same run: one repeat got the same 500
        twice, the next repeat got a 200. Not every attempt of every repeat,
        so no flag -- and [infra_error, pass] is still infra (F-158)."""
        counts = {"n": 0}

        def blip(path, body, headers):
            if path == "/":
                return 200, {"message": "up"}, {}
            counts["n"] += 1
            if counts["n"] <= 2:
                return 500, {"error": "boom"}, {}
            return 200, {"message": "ok"}, {}

        self.app.server.responder = blip
        plan = make_plan(self.state, self.app.base_url, k=2)
        plan["execution"]["infra_rate_abort"] = 1.0
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual([r["verdict"] for r in verdict["repeats"]],
                         ["infra_error", "pass"])
        self.assertEqual(verdict["verdict"], "infra_error")
        self.assertIsNone(verdict["repeated_5xx"])
        self.assertEqual(self.read("results.json")["summary"]["repeated_5xx"],
                         0)

    def test_a_different_5xx_body_on_another_repeat_is_not_flagged(self):
        """SS7 says the SAME status and body on every attempt of every
        repeat; two different 500s are two errors, not one repeated one."""
        counts = {"n": 0}

        def two_errors(path, body, headers):
            if path == "/":
                return 200, {"message": "up"}, {}
            counts["n"] += 1
            return 500, {"error": "boom-{}".format((counts["n"] - 1) // 2)}, {}

        self.app.server.responder = two_errors
        plan = make_plan(self.state, self.app.base_url, k=2)
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["verdict"], "infra_error")
        self.assertIsNone(verdict["repeated_5xx"])

    def test_infra_rate_counts_and_divides_by_the_same_cases(self):
        """F-161's family, seen in the F-158 reproduction: one app case
        (infra) and one canary (pass) read "1 cases ... infra 50.0%" -- the
        numerator left the canary out and the denominator counted it. Both
        sides are now the non-canary cases that were sent (SS9)."""
        def canary_up(path, body, headers):
            if path == "/":
                return 200, {"message": "up"}, {}
            if body.get("message") == "canary?":
                return 200, {"message": "ok"}, {}
            return 500, {"error": "boom"}, {}

        self.app.server.responder = canary_up
        plan = make_plan(self.state, self.app.base_url, cases=[
            make_case("c-0001"),
            make_case("c-canary", split=["smoke", "canary"],
                      input={"messages": [{"role": "user",
                                           "content": "canary?"}]})])
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        summary = self.read("results.json")["summary"]
        self.assertEqual((summary["n"], summary["attempted"]), (1, 2))
        self.assertEqual(summary["infra_errors"], 1)
        self.assertEqual(summary["infra_rate"], 1.0)

    def test_verify_recounts_infra_rate(self):
        """Whole-branch review: infra_rate is now infra_errors / (n -
        skipped), every term recounted from the tree, yet --verify took the
        recorded number on trust -- a hand-lowered rate opened the gate."""
        plan = make_plan(self.state, self.app.base_url)
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        path = self.out_dir() / "results.json"
        results = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(results["summary"]["infra_rate"], 1.0)
        results["summary"]["infra_rate"] = 0.0
        path.write_text(json.dumps(results), encoding="utf-8")
        verify = subprocess.run(
            [sys.executable, str(RUNNER), "--verify", str(self.out_dir())],
            capture_output=True, text=True)
        self.assertNotEqual(verify.returncode, 0, verify.stdout)
        self.assertIn("summary.infra_rate", verify.stdout)

    def test_infra_rate_abort(self):
        """Burning a full suite against a down service is an expensive way of
        learning the service is down."""
        cases = [make_case(f"c-{n:04d}") for n in range(1, 13)]
        plan = make_plan(self.state, self.app.base_url, cases=cases)
        rc, payload, _ = self.invoke(plan)
        self.assertEqual(rc, 5)
        self.assertIn("infra_rate_abort", payload["error"])
        results = self.read("results.json")
        self.assertEqual(results["summary"]["status"], "aborted_infra")
        self.assertEqual(results["exit_code"], 5)
        # It stops at the check's floor, not after all 12.
        self.assertEqual(results["summary"]["attempted"], 8)
        # ...and the partial run is still a well-formed artifact.
        self.assertEqual(len(self.jsonl("verdicts.jsonl")), 8)


class TestNoRetryOn4xx(RunnerCase):
    responder = staticmethod(health_only(
        lambda path, body, headers: (400, {"message": "needs context"}, {})))

    def test_400_is_a_response_not_a_transport_failure(self):
        """The field test asserts a deliberate 400 on OOS; retrying it would
        triple the spend on every correctly-refused case."""
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001", category="oos")])
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        response = self.read("cases", "c-0001", "response.json")
        self.assertEqual(response["attempts"], 1)
        self.assertEqual(response["status"], 400)
        self.assertIsNone(response["error"])
        # Not infra: the app ANSWERED. This case asserts status 200, so the
        # 400 is a fail -- a real observation about the app, which is exactly
        # what an infra verdict would have thrown away.
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["verdict"], "fail")
        self.assertEqual(verdict["layers"]["http"]["verdict"], "fail")


class TestCrashRate(RunnerCase):
    """A crash on a noise/adversarial case is a first-class number, not just
    an excluded row."""

    responder = staticmethod(health_only(
        lambda path, body, headers: (None, None, None)))

    def test_dropped_connection_on_a_noise_case(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001", category="noise")])
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        response = self.read("cases", "c-0001", "response.json")
        self.assertTrue(response["crashed"])
        summary = self.read("results.json")["summary"]
        self.assertEqual(summary["crash_rate"], 1.0)
        self.assertEqual(summary["infra_errors"], 1)


class TestHoldoutSeal(RunnerCase):
    """SS6/SS10 + decision D8."""

    responder = staticmethod(health_only(ok_responder))

    def holdout_plan(self):
        return make_plan(
            self.state, self.app.base_url,
            run_id="holdout-20260908T120000Z", mode="holdout",
            selecting_split="holdout",
            cases=[make_case("c-0001", split=["holdout"]),
                   make_case("c-0002", split=["holdout"])])

    def test_holdout_ids_appear_nowhere_in_results_json(self):
        plan = self.holdout_plan()
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        out = self.state / "reports" / "holdout-20260908T120000Z"
        results = json.loads((out / "results.json").read_text(
            encoding="utf-8"))
        self.assertEqual(results["cases"], [])
        self.assertEqual(results["summary"]["holdout"]["n"], 2)
        # Aggregate only; `failures` lets a reader take holdout out of the
        # scored denominator (passes + failures) as well as out of `n`.
        self.assertEqual(results["summary"]["holdout"]["failures"], 0)
        self.assertNotIn("c-0001", (out / "results.json").read_text(
            encoding="utf-8"))
        # The durable record DOES carry them: a paired diff needs them.
        rows = [json.loads(line) for line
                in (out / "verdicts.jsonl").read_text(
                    encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([r["case_id"] for r in rows], ["c-0001", "c-0002"])
        # ...and so do their ordinary case directories (contract SS6).
        self.assertTrue((out / "cases" / "c-0001" / "verdict.json").is_file())

    def test_the_look_is_recorded_once_in_the_ledger(self):
        self.invoke(self.holdout_plan())
        ledger = self.state / "datasets" / "holdout-looks.jsonl"
        rows = [json.loads(line) for line
                in ledger.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "holdout")
        self.assertEqual(rows[0]["reason"], "run")
        self.assertEqual(rows[0]["run_id"], "holdout-20260908T120000Z")

    def test_an_everyday_regression_run_does_not_spend_a_look(self):
        # `regression` selects the split NAMED "full", and the seal check
        # compared that name against the MODE names ("holdout", "full"): every
        # pre-merge run recorded a holdout look without reading one sealed
        # case, so five ordinary CI runs forced a reseal (2026-09-21 audit).
        # The `full` split and the `holdout` split are mutually exclusive
        # (case-format.md); only the mode `full` reaches sealed cases.
        plan = make_plan(self.state, self.app.base_url,
                         run_id="regression-20260908T120000Z",
                         mode="regression", selecting_split="full",
                         cases=[make_case("c-0001", split=["full"])])
        plan["paths"]["holdout_ledger"] = None      # and none is needed
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.state / "datasets"
                          / "holdout-looks.jsonl").exists())

    def test_a_smoke_run_does_not_spend_a_look(self):
        self.invoke(make_plan(self.state, self.app.base_url))
        self.assertFalse((self.state / "datasets"
                          / "holdout-looks.jsonl").exists())

    def test_yaml_ledger_is_refused(self):
        """The runner is stdlib-only: appending a JSON line to a YAML
        document would corrupt the document it is supposed to append to."""
        plan = self.holdout_plan()
        plan["paths"]["holdout_ledger"] = "datasets/dataset.yaml"
        rc, payload, _ = self.invoke(plan)
        self.assertEqual(rc, 2)
        self.assertIn("must be a .jsonl file", payload["error"])


class TestCanariesAndRepeats(RunnerCase):
    responder = staticmethod(health_only(ok_responder))

    def test_canaries_run_first_and_stay_out_of_the_denominators(self):
        """A canary measures the HARNESS, not the app: folding it into the
        app's score moves the number for the wrong reason."""
        plan = make_plan(self.state, self.app.base_url, cases=[
            make_case("c-app"),
            make_case("c-canary", split=["smoke", "canary"])])
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        rows = self.jsonl("verdicts.jsonl")
        self.assertEqual([r["case_id"] for r in rows], ["c-canary", "c-app"])
        canary = self.read("cases", "c-canary", "verdict.json")
        self.assertTrue(canary["canary"])
        self.assertFalse(canary["gating"])
        summary = self.read("results.json")["summary"]
        self.assertEqual(summary["canaries"], {"n": 1, "passed": 1})
        self.assertEqual(summary["n"], 1)      # the canary is not counted

    def test_repeats_directory_holds_every_repeat(self):
        """SS6: repeats/ exists iff k > 1, and holds the first repeat too --
        no asymmetry between "the run" and "the extra runs"."""
        plan = make_plan(self.state, self.app.base_url, k=3)
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        case_dir = self.out_dir() / "cases" / "c-0001"
        for n in ("1", "2", "3"):
            for name in ("request.json", "response.json", "verdict.json"):
                self.assertTrue((case_dir / "repeats" / n / name).is_file(),
                                f"repeats/{n}/{name}")
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["k"], 3)
        self.assertEqual(len(verdict["repeats"]), 3)
        self.assertEqual(len(self.jsonl("repeats.jsonl")), 3)
        self.assertEqual(len([c for c in self.app.calls
                              if c["path"] != "/"]), 3)

    def test_repeats_file_is_absent_at_k_1(self):
        self.invoke(make_plan(self.state, self.app.base_url))
        self.assertFalse((self.out_dir() / "repeats.jsonl").exists())
        self.assertFalse((self.out_dir() / "cases" / "c-0001"
                          / "repeats").exists())


class TestFunctionMode(RunnerCase):
    """Decision D6: adapter-contract.md calls function mode PREFERRED when
    auth or HTTP is in the way, so v1 implements it rather than exiting 3."""

    responder = staticmethod(health_only(ok_responder))

    def write_module(self, source):
        (self.tmp / "fake_entry.py").write_text(source, encoding="utf-8")

    def function_plan(self, entrypoint="fake_entry:handle"):
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["app"] = {"name": "fake", "repo": str(self.tmp)}
        plan["adapter"]["invocation"] = {
            "mode": "function", "entrypoint": entrypoint,
            "request_body": '{"message": "<user turn>", "sid": "<uuid>"}',
            "timeout_s": 5, "max_concurrency": 1,
        }
        return plan

    def test_entrypoint_is_called_and_its_text_recorded(self):
        self.write_module(
            "CALLS = []\n"
            "def handle(request):\n"
            "    CALLS.append(request)\n"
            "    return {'text': 'from function', 'status': 200}\n")
        rc, _, proc = self.invoke(self.function_plan())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        answer = (self.out_dir() / "cases" / "c-0001"
                  / "answer.txt").read_text(encoding="utf-8")
        self.assertEqual(answer, "from function")
        request = self.read("cases", "c-0001", "request.json")
        self.assertEqual(request["method"], "CALL")
        self.assertEqual(request["body"]["message"], "how many?")
        manifest = self.read("manifest.yaml")
        # The manifest says the timeout was advisory rather than pretending it
        # was enforced -- an in-process call cannot be interrupted.
        self.assertFalse(manifest["invocation"]["timeout_enforced"])

    def test_a_bare_string_return_is_accepted(self):
        self.write_module("def handle(request):\n    return 'plain text'\n")
        rc, _, proc = self.invoke(self.function_plan())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual((self.out_dir() / "cases" / "c-0001"
                          / "answer.txt").read_text(encoding="utf-8"),
                         "plain text")

    def test_an_unimportable_entrypoint_is_a_preflight_failure(self):
        """Not N identical infra errors: exit 3 with nothing billed."""
        rc, payload, _ = self.invoke(self.function_plan("nope_missing:handle"))
        self.assertEqual(rc, 3)
        self.assertIn("cannot import entrypoint module", payload["error"])

    def test_a_raising_entrypoint_is_infra_not_fail(self):
        self.write_module(
            "def handle(request):\n    raise RuntimeError('app blew up')\n")
        rc, _, proc = self.invoke(self.function_plan())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["verdict"], "infra_error")
        self.assertIn("app blew up",
                      self.read("cases", "c-0001", "response.json")["error"])


def slow_responder(path, body, headers):
    if path == "/":
        return 200, {"ok": True}, {}
    time.sleep(0.25)
    return 200, {"message": "ok"}, {}


class TestResume(RunnerCase):
    """SS8, and the property SS9(a) exists for: an interrupted run is still a
    correct artifact, and resuming it cannot double-count."""

    responder = staticmethod(slow_responder)

    def cases(self, n=6):
        return [make_case(f"c-{i:04d}") for i in range(1, n + 1)]

    def wait_for_cases(self, count, timeout=20):
        # monotonic, not time.time(): a wall-clock step ends the wait at once,
        # which is how "only saw 1 in 20s" was reported by an 9-second run.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            done = list((self.out_dir() / "cases").glob("*/verdict.json")) \
                if (self.out_dir() / "cases").is_dir() else []
            if len(done) >= count:
                return len(done)
            time.sleep(0.05)
        self.fail(f"only saw {len(done)} completed cases in {timeout}s")

    def kill_mid_run(self, plan):
        proc = self.invoke(plan, wait=False)
        self.addCleanup(proc.kill)
        completed = self.wait_for_cases(2)
        proc.kill()
        proc.wait(timeout=10)
        # Close the pipes explicitly: an unclosed Popen reader raises a
        # ResourceWarning that buries the assertion output of a failing test.
        for stream in (proc.stdout, proc.stderr):
            stream.close()
        return completed

    def test_an_interrupted_run_still_has_both_derived_files(self):
        completed = self.kill_mid_run(
            make_plan(self.state, self.app.base_url, cases=self.cases()))
        # Not built at run end and not appended to: derived after every case,
        # so the kill cannot leave them absent the way the shipped run did.
        # The kill lands the instant a second verdict.json exists, and the
        # derived files are rebuilt just AFTER it — so the newest case may be
        # one row short. Every earlier case must already be there: demanding
        # `completed` rows made this test fail under load (2026-09-21).
        rows = self.jsonl("verdicts.jsonl")
        self.assertGreaterEqual(len(rows), completed - 1)
        self.assertGreaterEqual(len(rows), 1)
        self.assertLessEqual(len(rows), self.wait_for_cases(completed))
        self.assertTrue((self.out_dir() / "verdicts_for_stats.jsonl").is_file())
        self.assertEqual(self.read("results.json")["summary"]["status"],
                         "running")

    def test_resume_completes_the_run_without_duplicate_rows(self):
        plan = make_plan(self.state, self.app.base_url, cases=self.cases())
        self.kill_mid_run(plan)
        before = {row["case_id"] for row in self.jsonl("verdicts.jsonl")}

        rc, _, proc = self.invoke(plan, extra_argv=["--resume"])
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)

        rows = self.jsonl("verdicts.jsonl")
        ids = [row["case_id"] for row in rows]
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), 6, f"duplicate rows: {ids}")
        self.assertEqual(ids, [f"c-{i:04d}" for i in range(1, 7)],
                         "resumed rows must be rebuilt in PLAN order")
        self.assertTrue(before.issubset(set(ids)))
        results = self.read("results.json")
        self.assertEqual(results["summary"]["status"], "ok")
        self.assertEqual(results["summary"]["n"], 6)
        self.assertEqual(len(results["cases"]), 6)

    def test_a_half_written_case_is_redone_not_trusted(self):
        plan = make_plan(self.state, self.app.base_url, cases=self.cases())
        self.kill_mid_run(plan)
        partial = self.out_dir() / "cases" / "c-9999"
        partial.mkdir()
        (partial / "expect.json").write_text("{}", encoding="utf-8")

        rc, _, proc = self.invoke(plan, extra_argv=["--resume"])
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        # A case dir with no verdict.json is deleted and re-run; this one is
        # not in the plan at all, so it simply does not come back.
        self.assertFalse(partial.exists())
        for case_id in (f"c-{i:04d}" for i in range(1, 7)):
            self.assertTrue((self.out_dir() / "cases" / case_id
                             / "verdict.json").is_file(), case_id)

    def test_resume_refuses_a_changed_plan(self):
        """Resuming a different suite under an old manifest produces a run
        that is comparable to nothing -- and still carries the old run id."""
        plan = make_plan(self.state, self.app.base_url, cases=self.cases())
        self.kill_mid_run(plan)
        changed = make_plan(self.state, self.app.base_url,
                            cases=self.cases(5))
        rc, payload, _ = self.invoke(changed, extra_argv=["--resume"])
        self.assertEqual(rc, 2)
        self.assertIn("plan changed since", payload["error"])

    def test_a_non_empty_out_without_resume_is_refused(self):
        plan = make_plan(self.state, self.app.base_url, cases=self.cases())
        self.kill_mid_run(plan)
        rc, payload, _ = self.invoke(plan)
        self.assertEqual(rc, 2)
        self.assertIn("pass --resume", payload["error"])

    def test_resume_on_an_absent_run(self):
        plan = make_plan(self.state, self.app.base_url, cases=self.cases(1))
        rc, payload, _ = self.invoke(plan, extra_argv=["--resume"])
        self.assertEqual(rc, 2)
        self.assertIn("no run here to resume", payload["error"])


class TestCompleteness(RunnerCase):
    """SS9(c): `status: "ok"` is written ONLY after a check, so "the run
    finished" and "the run's required outputs exist" are one statement."""

    responder = staticmethod(health_only(ok_responder))

    def test_a_missing_required_artifact_exits_6(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001"), make_case("c-0002")])
        self.invoke(plan)
        # Simulate the shipped run's failure directly: delete a required file
        # and resume, which re-runs finalize over the same tree.
        (self.out_dir() / "cases" / "c-0002" / "answer.txt").unlink()
        (self.out_dir() / "cases" / "c-0002" / "verdict.json").unlink()
        rc, _, _ = self.invoke(plan, extra_argv=["--resume"])
        self.assertEqual(rc, 0, "the re-run case is simply redone")

        (self.out_dir() / "cases" / "c-0001" / "answer.txt").unlink()
        rc, payload, _ = self.invoke(plan, extra_argv=["--resume"])
        self.assertEqual(rc, 6)
        self.assertIn("cases/c-0001/answer.txt", payload["missing_artifacts"])
        results = self.read("results.json")
        self.assertEqual(results["summary"]["status"], "incomplete")
        self.assertEqual(results["exit_code"], 6)
        self.assertIn("cases/c-0001/answer.txt",
                      results["summary"]["missing_artifacts"])

    def test_required_tables_match_the_contract(self):
        """SS9(b): the declared table is the one the CONTRACT declares.

        Not a restatement of the constants -- the contract file is executed and
        parsed, so a table edited in one place and not the other fails here.
        This is the check that keeps SS6's tree, SS9(b)'s table and the module's
        constants from becoming three answers to one question.
        """
        declared = exec_contract_block(
            "**(b) A declared required-artifact table.**")
        self.assertEqual(run_cases.REQUIRED_ALWAYS, declared["REQUIRED_ALWAYS"])
        self.assertEqual(run_cases.REQUIRED_PER_CASE,
                         declared["REQUIRED_PER_CASE"])
        self.assertEqual(run_cases.REQUIRED_IF, declared["REQUIRED_IF"])
        # report.md/.html are deliberately NOT required: the skill writes them
        # (SS13), and a required file the runner does not produce would fail
        # every run.
        self.assertNotIn("report.md", run_cases.REQUIRED_ALWAYS)
        self.assertNotIn("report.md", run_cases.REQUIRED_IF)

    def test_required_if_matches_the_output_tree(self):
        """SS6: every conditional file in the tree is in REQUIRED_IF, with the
        SAME condition string. A file added to the tree without a check -- the
        exact way the shipped run lost verdicts.jsonl -- fails here."""
        tree = contract_tree_conditions()
        # reports/.gitignore is the one conditional file OUTSIDE the run dir,
        # so it is not part of the tree the completeness check walks.
        tree.pop(".gitignore", None)
        self.assertEqual(tree, dict(run_cases.REQUIRED_IF))


def span(trace_id, span_id, op, name, start=1, end=2, **attrs):
    """One OTel gen_ai span, in the OTLP/JSON shape normalize_trace.py reads."""
    values = {"gen_ai.operation.name": op}
    values.update(attrs)
    if op == "invoke_agent":
        values["gen_ai.agent.name"] = name
    elif op == "execute_tool":
        values["gen_ai.tool.name"] = name
    return {
        "traceId": trace_id, "spanId": span_id, "name": name,
        "startTimeUnixNano": str(start * 10 ** 9),
        "endTimeUnixNano": str(end * 10 ** 9),
        "attributes": [{"key": k, "value": {"stringValue": v}
                        if isinstance(v, str) else {"intValue": v}}
                       for k, v in values.items() if v is not None],
    }


def otlp(spans):
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


class TestLayerTable(RunnerCase):
    """SS5's three-way distinction, which is the load-bearing part.

    n/a (not applicable) vs unscorable (applicable but disabled, blocked_by
    copied from the matrix) vs unscored (enabled but its input could not be
    produced). None of the three is ever pass and none is ever fail: the
    shipped run's `trajectory: unscorable` rows are honest, a `pass` there
    would have been a lie, and in the authz row a dangerous one.
    """

    responder = staticmethod(health_only(ok_responder))

    def layers_of(self, case, **plan_kw):
        plan = make_plan(self.state, self.app.base_url, cases=[case],
                         **plan_kw)
        rc, payload, proc = self.invoke(plan)
        self.assertIn(rc, (0, 7), proc.stdout + proc.stderr)
        return self.read("cases", case["id"], "verdict.json")["layers"]

    def test_not_applicable_is_na(self):
        layers = self.layers_of(make_case("c-0001"))
        for name in ("routing", "trajectory", "tool_selection", "execution",
                     "authz", "rules", "state", "judged", "loops"):
            self.assertEqual(layers[name], {"layer": name, "verdict": "n/a"},
                             name)

    def test_applicable_but_disabled_is_unscorable_with_the_matrix_reason(self):
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["list_invoices"]}
        layers = self.layers_of(case)
        self.assertEqual(layers["trajectory"],
                         {"layer": "trajectory", "verdict": "unscorable",
                          # COPIED from the capability matrix, not composed by
                          # the runner: `unscorable` means somebody decided
                          # this layer is off and said why.
                          "blocked_by": "no trace-id correlation"})

    def test_answer_quality_switches_off_the_three_rows_it_names(self):
        """The matrix says `answer_quality`; SS5's table scores it as `answer`,
        `rules` and `judged`. Each row used to be looked up under its own
        name, so the documented key disabled nothing: `make_plan.py --layer
        routing` wrote `answer_quality: {enabled: false}` and the answer layer
        went on deciding cases the linter had already called ungraded."""
        case = make_case("c-0001")
        case["expect"]["answer"].update(rules=["cites the invoice id"],
                                        rubric="r-helpfulness")
        layers = self.layers_of(case, capability_matrix={
            "answer_quality": {"enabled": False,
                               "blocked_by": "--layer routing"}})
        for name in ("answer", "rules", "judged"):
            self.assertEqual(layers[name],
                             {"layer": name, "verdict": "unscorable",
                              "blocked_by": "--layer routing"}, name)
        # ...and the liveness row that is left does not carry the case.
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["layers"]["http"]["verdict"], "pass")
        self.assertEqual(verdict["verdict"], "unscored")

    def test_http_has_no_matrix_key(self):
        """Every case gets `http` and a collected trace triggers `loops`;
        neither is a capability a profile declares, so an entry under either
        name switches nothing off -- and every other row names its key.
        (TestTrajectoryLayers has the `loops` half: it needs a trace.)"""
        keys = run_cases.MATRIX_KEY_OF_LAYER
        self.assertEqual(set(keys), set(run_cases.LAYER_ORDER))
        self.assertEqual({name for name, key in keys.items() if key is None},
                         {"http", "loops"})
        layers = self.layers_of(make_case("c-0001"), capability_matrix={
            "http": {"enabled": False, "blocked_by": "nobody can say this"}})
        self.assertEqual(layers["http"]["verdict"], "pass")

    def test_applicable_and_enabled_but_no_input_is_unscored(self):
        case = make_case("c-0001")
        case["expect"]["authz"] = {"forbidden_tools": ["delete_user"]}
        layers = self.layers_of(case)
        self.assertEqual(layers["authz"]["verdict"], "unscored")
        self.assertIn("traces.source: view-only", layers["authz"]["reason"])
        # And never `pass`: an authz layer that reported pass without reading a
        # trajectory is the dangerous one.
        self.assertNotEqual(layers["authz"]["verdict"], "pass")

    def test_a_vacuous_case_is_not_a_pass(self):
        """SS10 rule 6, caught at run time as well as at authoring time.

        This test used to assert the opposite -- that the case below PASSES on
        its `http` row alone -- which is the defect Step 10 found rather than a
        rule anyone chose. `expect.state` is RESERVED (no scorer compares
        environment snapshots), so this case measured nothing about the app's
        behaviour, and a status code is a liveness check: `validate_cases.py`
        already refuses to count `expect.http` toward gradedness. Counting it
        one level up, in the rollup, is what turned every unscorable real layer
        into a pass -- a trace-less `expect.tools` case included.
        """
        case = make_case("c-0001", expect={"state": {"unchanged": True}})
        plan = make_plan(self.state, self.app.base_url, cases=[case])
        self.invoke(plan)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["layers"]["state"]["verdict"], "unscored")
        self.assertIn("RESERVED", verdict["layers"]["state"]["reason"])
        # The app answered, and that is recorded honestly...
        self.assertEqual(verdict["layers"]["http"]["verdict"], "pass")
        # ...but it does not carry the case: nothing behavioural was scored.
        self.assertEqual(verdict["verdict"], "unscored")

    def test_http_only_case_still_passes_on_liveness(self):
        """The other side of the same rule. When `http` is the ONLY applicable
        layer the liveness check IS the whole claim, so it still rolls up to
        `pass` -- excluding http from rule 5 must not turn a deliberate
        responds-always case into an unscored one."""
        case = make_case("c-0001", expect={"http": {"status": 200}})
        plan = make_plan(self.state, self.app.base_url, cases=[case])
        self.invoke(plan)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["layers"]["http"]["verdict"], "pass")
        self.assertEqual(verdict["verdict"], "pass")
        self.assertEqual(
            {name for name, layer in verdict["layers"].items()
             if layer["verdict"] == "pass"}, {"http"})

    def test_the_judged_row_says_why_it_is_unjudged(self):
        case = make_case("c-0001")
        case["expect"]["answer"]["rubric"] = "r-helpfulness"
        layers = self.layers_of(case)
        self.assertEqual(layers["judged"]["verdict"], "unjudged (mode: smoke)")

    def test_business_rules_are_the_skills_job(self):
        case = make_case("c-0001")
        case["expect"]["answer"]["rules"] = ["cites the invoice id"]
        layers = self.layers_of(case)
        self.assertEqual(layers["rules"]["verdict"], "unscored")
        self.assertIn("evaluated by the skill", layers["rules"]["reason"])

    def test_a_missing_answer_field_is_unscored_not_a_content_failure(self):
        """The response carried nothing at the declared <answer> path.

        Scoring the empty string against must_contain would report a CONTENT
        failure for what is an extraction gap -- a failure the app never had.
        """
        self.app.server.responder = lambda p, b, h: (200, {"reply": "ok"}, {})
        case = make_case("c-0001")
        layers = self.layers_of(case)
        self.assertEqual(layers["answer"]["verdict"], "unscored")
        self.assertIn("<answer>", layers["answer"]["reason"])


class TestScorerErrors(RunnerCase):
    """SS5.1: rc 2 is read off STDOUT, the run continues, and it exits 7."""

    responder = staticmethod(health_only(ok_responder))

    def test_a_malformed_expect_is_an_error_layer_not_a_failure(self):
        # score_answer.py exits 2 on a non-list must_contain. One malformed
        # expect block must not throw away the other case's spend...
        bad = make_case("c-0001")
        bad["expect"]["answer"] = {"must_contain": "ok"}
        plan = make_plan(self.state, self.app.base_url,
                         cases=[bad, make_case("c-0002")])
        rc, payload, proc = self.invoke(plan)
        # ...but the run's numbers are not quotable, so exit 7.
        self.assertEqual(rc, 7, proc.stdout + proc.stderr)
        self.assertIn("not quotable", payload["error"])

        layer = self.read("cases", "c-0001", "verdict.json")["layers"]["answer"]
        self.assertEqual(layer["verdict"], "error")
        self.assertEqual(layer["scorer"], "score_answer.py")
        # The scorers put errors on STDOUT (_common.die); a runner reading
        # stderr here would record an empty string and report nothing.
        self.assertTrue(layer["error"])
        # The scorer error outranks the sibling http `pass`: a case with a
        # broken scorer has a number nobody should quote.
        self.assertEqual(self.read("cases", "c-0001",
                                   "verdict.json")["verdict"], "unscored")
        self.assertEqual(self.read("cases", "c-0002",
                                   "verdict.json")["verdict"], "pass")
        summary = self.read("results.json")["summary"]
        self.assertEqual(summary["scorer_errors"], 1)
        # SS11 row 7: the ARTIFACTS are complete, so the status stays "ok".
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["missing_artifacts"], [])

    def test_a_scorer_timeout_is_recorded_as_an_error(self):
        plan = make_plan(self.state, self.app.base_url)
        plan["scoring"] = {"scorer_timeout_s": 0.001}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 7, proc.stdout + proc.stderr)
        layer = self.read("cases", "c-0001", "verdict.json")["layers"]["answer"]
        self.assertEqual(layer["verdict"], "error")
        self.assertIn("timed out", layer["error"])


def routing_responder(path, body, headers):
    """A trace-less app whose only routing observable is its status code --
    the field test's shape exactly."""
    if "refund" in (body.get("message") or ""):
        return 400, {"message": "out of scope"}, {}
    return 200, {"message": "ok", "route": "units"}, {}


class TestRoutingRunLevel(RunnerCase):
    """SS5.2. score_routing.py scores ALL cases at once, so it runs once."""

    responder = staticmethod(health_only(routing_responder))

    def routing_plan(self, cases, **invocation):
        plan = make_plan(self.state, self.app.base_url, cases=cases)
        plan["adapter"]["invocation"].update(invocation)
        return plan

    def test_route_from_status_is_declared_per_app_not_inferred(self):
        """Decision D2. The shipped run inferred routes from status by hand
        (200=answered, 400=none) -- defensible for that app, and exactly the
        judgement a runner must not make silently across every app."""
        cases = [
            make_case("c-units", input={"messages": [{"role": "user",
                                                      "content": "how many?"}]},
                      expect={"http": {"status": 200}, "route": "units"}),
            make_case("c-oos", input={"messages": [{"role": "user",
                                                    "content": "a refund?"}]},
                      expect={"http": {"status": 400}, "route": "__oos__"}),
        ]
        plan = self.routing_plan(cases, route_from_status={"200": "units",
                                                           "400": "__oos__"})
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        for case_id in ("c-units", "c-oos"):
            layer = self.read("cases", case_id, "verdict.json")["layers"]["routing"]
            self.assertEqual(layer["verdict"], "pass")
            # So no reader mistakes it for a trace-derived route.
            self.assertEqual(layer["observed_from"], "status")

        rows = self.jsonl("routing_results.jsonl")
        self.assertEqual({r["case_id"] for r in rows}, {"c-units", "c-oos"})
        report = self.read("routing_report.json")
        self.assertEqual(report["layer"], "routing")
        self.assertEqual(report["accuracy"], 1.0)
        self.assertEqual(report["n"], 2)

    def test_a_canary_stays_out_of_the_routing_report(self):
        """F-161 (user test round 2): routing_report.json counted the two
        canaries that results.json leaves out, so one run had a routing `n`
        of 4 beside a `summary.n` of 2. A canary measures the harness; it
        enters no denominator (SS9)."""
        cases = [
            make_case("c-units", expect={"http": {"status": 200},
                                         "route": "units"}),
            make_case("c-canary", split=["smoke", "canary"],
                      input={"messages": [{"role": "user",
                                           "content": "canary?"}]},
                      expect={"http": {"status": 200}, "route": "units"}),
        ]
        plan = self.routing_plan(cases, route_from_status={"200": "units"})
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        # The canary's own layer still scores; it is only kept out of the
        # run-level report.
        self.assertEqual(self.read("cases", "c-canary", "verdict.json")
                         ["layers"]["routing"]["verdict"], "pass")
        rows = self.jsonl("routing_results.jsonl")
        self.assertEqual([r["case_id"] for r in rows], ["c-units"])
        self.assertEqual(self.read("routing_report.json")["n"], 1)

    def test_a_run_whose_only_routed_case_is_a_canary_is_complete(self):
        """Review of the F-161 fix: with canary rows kept out, a run whose
        only routing-scorable case is a canary writes no routing file -- and
        the completeness check must not then call the run incomplete."""
        cases = [
            make_case("c-plain"),
            make_case("c-canary", split=["smoke", "canary"],
                      input={"messages": [{"role": "user",
                                           "content": "canary?"}]},
                      expect={"http": {"status": 200}, "route": "units"}),
        ]
        plan = self.routing_plan(cases, route_from_status={"200": "units"})
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.out_dir() / "routing_results.jsonl").exists())
        verify = subprocess.run(
            [sys.executable, str(RUNNER), "--verify", str(self.out_dir())],
            capture_output=True, text=True)
        self.assertEqual(verify.returncode, 0, verify.stdout)

    def test_without_the_block_routing_is_unscored_and_excluded(self):
        """Refusing to infer is the point: the runner never invents a route."""
        cases = [make_case("c-units", expect={"http": {"status": 200},
                                              "route": "units"})]
        rc, _, proc = self.invoke(self.routing_plan(cases))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layer = self.read("cases", "c-units", "verdict.json")["layers"]["routing"]
        self.assertEqual(layer["verdict"], "unscored")
        self.assertIn("route_from_status", layer["reason"])
        # Excluded from the run-level file, which therefore is not written.
        self.assertFalse((self.out_dir() / "routing_results.jsonl").exists())
        self.assertFalse((self.out_dir() / "routing_report.json").exists())

    def test_a_declared_response_field_wins(self):
        cases = [make_case("c-units", expect={"http": {"status": 200},
                                              "route": "units"})]
        plan = self.routing_plan(cases, route_from_response="route")
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layer = self.read("cases", "c-units", "verdict.json")["layers"]["routing"]
        self.assertEqual((layer["verdict"], layer["observed_from"]),
                         ("pass", "response"))

    def test_oos_route_is_pre_checked_not_discovered_from_exit_2(self):
        """Step 3 made score_routing.py exit 2 when --oos-route names a label
        the data does not carry. A run that simply selected no OOS case is not
        an error, so the runner performs the check itself."""
        cases = [make_case("c-units", expect={"http": {"status": 200},
                                              "route": "units"})]
        plan = self.routing_plan(cases, route_from_status={"200": "units"})
        plan["scoring"] = {"oos_route": "none"}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.read("results.json")["summary"]["scorer_errors"],
                         0)
        self.assertNotIn("oos", self.read("routing_report.json"))

    def test_oos_route_is_passed_when_the_data_carries_it(self):
        cases = [
            make_case("c-units", expect={"http": {"status": 200},
                                         "route": "units"}),
            make_case("c-oos", input={"messages": [{"role": "user",
                                                    "content": "a refund?"}]},
                      expect={"http": {"status": 400}, "route": "no-route"}),
        ]
        plan = self.routing_plan(cases, route_from_status={"200": "units",
                                                           "400": "no-route"})
        plan["scoring"] = {"oos_route": "no-route"}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        report = self.read("routing_report.json")
        self.assertIn("oos", json.dumps(report))

    def test_clarify_ok_without_a_way_to_observe_it_is_unscored(self):
        """`clarified: false` would be a CLAIM, not an observation -- and on a
        case whose whole point is that clarifying is acceptable, the claim
        decides the verdict."""
        cases = [make_case("c-amb", expect={"http": {"status": 200},
                                            "route": "units",
                                            "clarify_ok": True})]
        plan = self.routing_plan(cases, route_from_status={"200": "billing"})
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layer = self.read("cases", "c-amb", "verdict.json")["layers"]["routing"]
        self.assertEqual(layer["verdict"], "unscored")
        self.assertIn("clarify_from_response", layer["reason"])

    def test_routing_survives_a_resume(self):
        """SS9(a): the rows are read back off the case dirs, like everything
        else -- so a resumed run's routing report is not half a report."""
        cases = [make_case(f"c-{n}", expect={"http": {"status": 200},
                                             "route": "units"})
                 for n in range(1, 4)]
        plan = self.routing_plan(cases, route_from_status={"200": "units"})
        self.invoke(plan)
        shutil.rmtree(self.out_dir() / "cases" / "c-3")
        (self.out_dir() / "routing_results.jsonl").unlink()
        (self.out_dir() / "routing_report.json").unlink()
        rc, _, proc = self.invoke(plan, extra_argv=["--resume"])
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.jsonl("routing_results.jsonl")), 3)


class TestExecutionLayer(RunnerCase):
    """SS5.3. `actual` is EXTRACTED, never computed, and never guessed."""

    responder = staticmethod(health_only(
        lambda p, b, h: (200, {"message": "there are 7", "data": {"count": 7}},
                         {})))

    def execution_case(self):
        return make_case("c-0001", expect={"http": {"status": 200},
                                           "result": {"scalar": 7}})

    def test_a_declared_response_field_is_extracted(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[self.execution_case()])
        plan["adapter"]["invocation"]["result_from_response"] = "data.count"
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.read("cases", "c-0001", "actual.json"),
                         {"scalar": 7})
        self.assertEqual(self.read("cases", "c-0001",
                                   "verdict.json")["layers"]["execution"]
                         ["verdict"], "pass")

    def test_a_declared_prose_pattern_is_the_weakest_source(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[self.execution_case()])
        plan["adapter"]["invocation"]["result_pattern"] = r"there are (\d+)"
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.read("cases", "c-0001", "actual.json"),
                         {"scalar": "7"})

    def test_no_declared_extraction_is_unscored_never_a_mismatch(self):
        """An unread result must never be scored as a mismatch: that
        manufactures a failure the app never had."""
        plan = make_plan(self.state, self.app.base_url,
                         cases=[self.execution_case()])
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        actual = self.read("cases", "c-0001", "actual.json")
        self.assertTrue(actual["missing"])
        self.assertIn("result_from_response", actual["reason"])
        layer = self.read("cases", "c-0001",
                          "verdict.json")["layers"]["execution"]
        self.assertEqual(layer["verdict"], "unscored")

    def test_actual_json_is_required_only_when_the_case_asks_for_a_result(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[self.execution_case(), make_case("c-0002")])
        self.invoke(plan)
        self.assertTrue((self.out_dir() / "cases" / "c-0001"
                         / "actual.json").is_file())
        self.assertFalse((self.out_dir() / "cases" / "c-0002"
                          / "actual.json").exists())
        # ...and its absence where the condition HOLDS is exit 6.
        (self.out_dir() / "cases" / "c-0001" / "actual.json").unlink()
        rc, payload, _ = self.verify(self.out_dir())
        self.assertEqual(rc, 6)
        self.assertTrue(any("cases/c-0001/actual.json" in m
                            for m in payload["missing_artifacts"]),
                        payload["missing_artifacts"])

    def verify(self, directory):
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--verify", str(directory)],
            capture_output=True, text=True)
        return (proc.returncode,
                json.loads(proc.stdout) if proc.stdout.strip() else None, proc)


class TestTrajectoryLayers(RunnerCase):
    """The trace path end to end: collect, normalize once, then four scorers."""

    TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"

    def setUp(self):
        super().setUp()
        self.spans = self.tmp / "spans.json"
        self.spans.write_text(json.dumps(otlp([
            span(self.TRACE_ID, "aaaaaaaaaaaaaaa1", "invoke_agent", "units"),
            span(self.TRACE_ID, "aaaaaaaaaaaaaaa2", "execute_tool",
                 "list_invoices", **{"gen_ai.tool.call.arguments":
                                     '{"unit_id": "U-1"}',
                                     "gen_ai.tool.call.result": '{"n": 7}'}),
        ])), encoding="utf-8")

    @staticmethod
    def responder(path, body, headers):
        return 200, {"message": "ok", "traceId": TestTrajectoryLayers.TRACE_ID}, {}

    def traced_plan(self, cases):
        plan = make_plan(self.state, self.app.base_url, cases=cases)
        plan["adapter"]["traces"] = {
            "source": "otlp-file", "convention": "gen_ai",
            "correlation": "response-field:traceId",
            "location": str(self.spans),
            "completeness": {"quiescence_ms": 10, "max_wait_s": 2},
        }
        plan["adapter"]["invocation"]["health_check"] = {
            "method": "POST", "path": "/api/chat/ask", "expect_status": [200]}
        plan["capability_matrix"] = {"routing": {"enabled": True},
                                     "trajectory": {"enabled": True},
                                     "tool_selection": {"enabled": True}}
        return plan

    def test_loops_has_no_matrix_key(self):
        """A collected trace triggers `loops` and no capability switches it
        off: a matrix entry under its name is not one the runner reads."""
        plan = self.traced_plan([make_case("c-0001")])
        plan["capability_matrix"]["loops"] = {
            "enabled": False, "blocked_by": "nobody can say this"}
        rc, payload, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layers = self.read("cases", "c-0001", "verdict.json")["layers"]
        self.assertEqual(layers["loops"]["verdict"], "pass")

    def test_a_collected_trace_feeds_every_trajectory_layer(self):
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["list_invoices"]}
        case["expect"]["args"] = {"list_invoices": {"unit_id": "U-1"}}
        case["expect"]["authz"] = {"forbidden_tools": ["delete_invoice"]}
        rc, payload, proc = self.invoke(self.traced_plan([case]))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        case_dir = self.out_dir() / "cases" / "c-0001"
        # Both REQUIRED_IF files, because a trace was collected for this case.
        self.assertTrue((case_dir / "trace.json").is_file())
        self.assertTrue((case_dir / "trajectory.json").is_file())
        layers = self.read("cases", "c-0001", "verdict.json")["layers"]
        for name in ("trajectory", "tool_selection", "loops", "authz"):
            self.assertEqual(layers[name]["verdict"], "pass", name)
        # normalize_trace.py runs ONCE and its output is what the four read.
        self.assertEqual(self.read("cases", "c-0001",
                                   "trajectory.json")["status"], "ok")

    def test_the_route_comes_from_the_gen_ai_agent_name(self):
        case = make_case("c-0001", expect={"http": {"status": 200},
                                           "route": "units"})
        rc, _, proc = self.invoke(self.traced_plan([case]))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        layer = self.read("cases", "c-0001", "verdict.json")["layers"]["routing"]
        self.assertEqual((layer["verdict"], layer["observed_from"]),
                         ("pass", "trace"))

    def test_a_result_from_a_tool_span_is_the_strongest_source(self):
        case = make_case("c-0001", expect={"http": {"status": 200},
                                           "result": {"scalar": 7}})
        plan = self.traced_plan([case])
        plan["adapter"]["invocation"]["result_from_tool"] = {
            "tool": "list_invoices", "field": "n"}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.read("cases", "c-0001", "actual.json"),
                         {"scalar": 7})

    def test_a_trace_that_never_arrives_is_infra_not_unscored(self):
        """The difference between "this run never had traces" and "this run
        has traces and lost this one" is the difference between a known
        limitation and a thing to go fix. Only the second is infra."""
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["list_invoices"]}
        plan = self.traced_plan([case])
        # The health check joins (pre-flight passes), then the app stops
        # echoing a trace id, so the case's own trace never arrives.
        calls = {"n": 0}

        def once(path, body, headers):
            calls["n"] += 1
            if calls["n"] == 1:
                return 200, {"message": "ok", "traceId": self.TRACE_ID}, {}
            return 200, {"message": "ok"}, {}

        self.app.server.responder = once
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["layers"]["trajectory"]["verdict"],
                         "infra_incomplete")
        self.assertEqual(verdict["verdict"], "infra_incomplete")
        self.assertFalse(verdict["trace"]["collected"])
        self.assertIn("no trace id", verdict["trace"]["reason"])
        # ...and the two REQUIRED_IF files are correctly absent, which SS9
        # must not then demand.
        case_dir = self.out_dir() / "cases" / "c-0001"
        self.assertFalse((case_dir / "trace.json").exists())
        self.assertFalse((case_dir / "trajectory.json").exists())

    def test_an_empty_trace_is_infra_incomplete_never_a_fail(self):
        """SS7: missing spans are infra. The app's behaviour was not observed,
        so nothing about it was measured -- least of all a failure."""
        # A span whose declared parent is not in the trace: normalize_trace.py
        # reports it under orphaned_parents and returns status "incomplete".
        # The trace id still has to be PRESENT, or pre-flight's join check
        # would refuse the run before any of this (SS4.4).
        orphan = span(self.TRACE_ID, "bbbbbbbbbbbbbbb1", "execute_tool",
                      "list_invoices")
        orphan["parentSpanId"] = "cccccccccccccccc"
        self.spans.write_text(json.dumps(otlp([orphan])), encoding="utf-8")
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["list_invoices"]}
        rc, _, proc = self.invoke(self.traced_plan([case]))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual(verdict["layers"]["trajectory"]["verdict"],
                         "infra_incomplete")
        self.assertEqual(verdict["verdict"], "infra_incomplete")


class TestReliability(RunnerCase):
    """SS5.5. pass@k vs pass^k, reduced once at the end."""

    responder = staticmethod(health_only(ok_responder))

    def test_reliability_json_is_written_at_k_above_1(self):
        plan = make_plan(self.state, self.app.base_url, k=3)
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        report = self.read("reliability.json")
        self.assertEqual(report["pass_hat_k"], 1.0)
        self.assertEqual(len(self.jsonl("repeats.jsonl")), 3)

    def test_a_case_with_an_infra_repeat_is_excluded_and_named(self):
        """reduce_repeats.py refuses a non-pass/fail verdict outright, because
        counting an infra verdict as a failure biases the estimate. A partial
        case would poison the reducer or silently lower k for every other
        case -- so it is excluded, and SAID to be excluded."""
        state = {"n": 0}

        def flaky(path, body, headers):
            if path == "/":
                return 200, {"message": "up"}, {}
            state["n"] += 1
            if state["n"] == 2:
                return 500, {"message": "boom"}, {}
            return 200, {"message": "ok"}, {}

        self.app.server.responder = flaky
        plan = make_plan(self.state, self.app.base_url, k=3,
                         cases=[make_case("c-0001"), make_case("c-0002")])
        plan["execution"] = {"timeout_s": 5, "max_attempts": 1,
                             "backoff_s": [], "infra_rate_abort": 1.0,
                             "insecure_tls": False}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        report = self.read("reliability.json")
        self.assertEqual([e["case_id"] for e in report["excluded_cases"]],
                         ["c-0001"])
        self.assertIn("bias the estimate", report["excluded_note"])
        self.assertEqual(len(self.jsonl("repeats.jsonl")), 3)   # c-0002 only

    def test_an_infra_repeat_decides_the_case_in_either_order(self):
        """F-158 (user test round 2, `run/repro-f139`): repeats [pass,
        infra_error] rolled up to `pass` and [infra_error, pass] to
        `infra_error` -- the same evidence, two verdicts, and a canary that
        missed a repeat still read "passed". pass^k cannot be claimed for a
        repeat that was never observed, so infra wins over pass whatever the
        order, and the representative repeat is the one that decided it."""
        for first, later, label in ((200, 503, "pass-then-infra"),
                                    (503, 200, "infra-then-pass")):
            with self.subTest(label):
                self.setUp()
                counts = {}

                def per_message(path, body, headers,
                                counts=counts, first=first, later=later):
                    if path == "/":
                        return 200, {"message": "up"}, {}
                    message = body.get("message")
                    counts[message] = counts.get(message, 0) + 1
                    status = first if counts[message] == 1 else later
                    if status == 200:
                        return 200, {"message": "ok"}, {}
                    return status, {"error": "service unavailable"}, {}

                self.app.server.responder = per_message
                canary = make_case(
                    "c-canary", split=["smoke", "canary"],
                    input={"messages": [{"role": "user",
                                         "content": "canary question"}]})
                plan = make_plan(self.state, self.app.base_url, k=2,
                                 cases=[make_case("c-0001"), canary])
                plan["execution"] = {"timeout_s": 5, "max_attempts": 1,
                                     "backoff_s": [], "infra_rate_abort": 1.0,
                                     "insecure_tls": False}
                rc, _, proc = self.invoke(plan)
                self.assertEqual(rc, 0, proc.stdout + proc.stderr)
                for case_id in ("c-0001", "c-canary"):
                    verdict = self.read("cases", case_id, "verdict.json")
                    self.assertEqual(verdict["verdict"], "infra_error",
                                     case_id)
                    self.assertEqual(sorted(r["verdict"]
                                            for r in verdict["repeats"]),
                                     ["infra_error", "pass"])
                    # The case-level files are the repeat that decided it.
                    self.assertEqual(
                        self.read("cases", case_id, "response.json")
                        ["status"], 503)
                summary = self.read("results.json")["summary"]
                self.assertEqual(summary["infra_errors"], 1)
                self.assertEqual((summary["passes"], summary["failures"]),
                                 (0, 0))
                self.assertEqual(summary["canaries"], {"n": 1, "passed": 0})
                verify = subprocess.run(
                    [sys.executable, str(RUNNER), "--verify",
                     str(self.out_dir())], capture_output=True, text=True)
                self.assertEqual(verify.returncode, 0, verify.stdout)
                self.assertEqual(json.loads(verify.stdout)["status"], "ok")

    def test_an_observed_failure_outranks_an_infra_repeat(self):
        """[fail, infra_error] is a `fail`: one observed failure already makes
        "every repeat passed" false, and hiding it behind provider noise would
        lose the one thing the run did see."""
        counts = {"n": 0}

        def fail_then_503(path, body, headers):
            if path == "/":
                return 200, {"message": "up"}, {}
            counts["n"] += 1
            if counts["n"] == 1:
                return 200, {"message": "wrong"}, {}
            return 503, {"error": "service unavailable"}, {}

        self.app.server.responder = fail_then_503
        plan = make_plan(self.state, self.app.base_url, k=2)
        plan["execution"] = {"timeout_s": 5, "max_attempts": 1,
                             "backoff_s": [], "infra_rate_abort": 1.0,
                             "insecure_tls": False}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-0001", "verdict.json")
        self.assertEqual([r["verdict"] for r in verdict["repeats"]],
                         ["fail", "infra_error"])
        self.assertEqual(verdict["verdict"], "fail")
        self.assertEqual(self.read("cases", "c-0001", "response.json")
                         ["status"], 200)

    def test_the_fold_over_every_repeat_pair(self):
        """SS5.5's fold, pinned per pair and in both orders -- including
        [pass, unscored], which rolled up to `pass` before F-158's fix."""
        P, F, U = run_cases.PASS, run_cases.FAIL, run_cases.UNSCORED
        E, C = run_cases.INFRA_ERROR, run_cases.INFRA_INCOMPLETE
        expected = {(P, P): P, (P, F): F, (P, E): E, (P, C): C, (P, U): U,
                    (F, E): F, (F, U): F, (E, C): E, (E, U): E, (C, U): C,
                    (U, U): U}
        for (a, b), want in expected.items():
            for pair in ((a, b), (b, a)):
                with self.subTest(pair=pair):
                    self.assertEqual(run_cases.fold_repeats(list(pair)), want)

    def test_body_key_never_raises(self):
        self.assertEqual(run_cases.body_key({"x": 1}), '{"x": 1}')
        # Mixed key types cannot be sorted; the fallback still compares.
        self.assertEqual(run_cases.body_key({1: "a", "b": 2}),
                         run_cases.body_key({1: "a", "b": 2}))

    def test_nothing_to_reduce_is_not_a_scorer_error(self):
        self.app.server.responder = health_only(
            lambda p, b, h: (500, {"message": "boom"}, {}))
        plan = make_plan(self.state, self.app.base_url, k=2)
        plan["execution"] = {"timeout_s": 5, "max_attempts": 1,
                             "backoff_s": [], "infra_rate_abort": 1.0,
                             "insecure_tls": False}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        report = self.read("reliability.json")
        self.assertEqual(report["verdict"], "unscored")
        self.assertEqual(self.read("results.json")["summary"]["scorer_errors"],
                         0)


class TestBaselineComparison(RunnerCase):
    """SS5.6 / D3. The diff has RULES, and rules that decide whether a change
    ships are the category of thing this step exists to take out of LLM
    hands."""

    responder = staticmethod(health_only(ok_responder))

    def a_baseline(self, run_id="smoke-20260907T120000Z", **manifest_kw):
        directory = self.state / "reports" / run_id
        directory.mkdir(parents=True)
        manifest = {"run_id": run_id, "k": 1, "dataset_version": 1,
                    "harness_version": run_cases.HARNESS_VERSION}
        manifest.update(manifest_kw)
        (directory / "manifest.yaml").write_text(json.dumps(manifest),
                                                 encoding="utf-8")
        (directory / "verdicts_for_stats.jsonl").write_text(
            "\n".join(json.dumps({"case_id": f"c-{n:04d}", "verdict": "fail"})
                      for n in range(1, 3)) + "\n", encoding="utf-8")
        return directory / "verdicts_for_stats.jsonl"

    def test_comparison_json_pairs_the_two_runs(self):
        baseline = self.a_baseline()
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001"), make_case("c-0002")])
        rc, _, proc = self.invoke(
            plan, extra_argv=["--baseline-verdicts", str(baseline)])
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        comparison = self.read("comparison.json")
        self.assertEqual(comparison["n"], 2)
        self.assertEqual(comparison["baseline_pass_rate"], 0.0)
        self.assertEqual(comparison["candidate_pass_rate"], 1.0)
        self.assertEqual(comparison["candidate_run_id"], RUN_ID)
        # Recorded in the manifest so --verify can evaluate the REQUIRED_IF
        # condition later, with no plan and no argv in hand.
        self.assertEqual(self.read("manifest.yaml")["baseline_verdicts"],
                         str(baseline))
        rc, payload, proc = self.verify(self.out_dir())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        (self.out_dir() / "comparison.json").unlink()
        rc, payload, _ = self.verify(self.out_dir())
        self.assertEqual(rc, 6)
        self.assertTrue(any("comparison.json" in m
                            for m in payload["missing_artifacts"]))

    def test_a_k_mismatch_is_refused_before_any_spend(self):
        """Decision D7: a baseline at k=1 and a candidate at k=3 pair pass^k
        verdicts computed over different numbers of trials."""
        baseline = self.a_baseline()
        plan = make_plan(self.state, self.app.base_url, k=3)
        rc, payload, _ = self.invoke(
            plan, extra_argv=["--baseline-verdicts", str(baseline)])
        self.assertEqual(rc, 2)
        self.assertIn("k: baseline 1, this run 3", payload["error"])
        # SS11's exit-2 row promises "none written", and it holds: the check
        # runs at plan validation, so the suite is not spent to learn this.
        self.assertFalse(self.out_dir().exists())
        self.assertEqual([c for c in self.app.calls], [])

    def test_a_dataset_version_mismatch_is_refused(self):
        baseline = self.a_baseline(dataset_version=2)
        plan = make_plan(self.state, self.app.base_url)
        rc, payload, _ = self.invoke(
            plan, extra_argv=["--baseline-verdicts", str(baseline)])
        self.assertEqual(rc, 2)
        self.assertIn("dataset_version", payload["error"])

    def test_a_baseline_without_its_manifest_is_refused(self):
        """Version equality and the k-match are the comparison's rules, and
        there is nowhere else to read them from."""
        loose = self.tmp / "verdicts_for_stats.jsonl"
        loose.write_text('{"case_id": "c-0001", "verdict": "pass"}\n',
                         encoding="utf-8")
        plan = make_plan(self.state, self.app.base_url)
        rc, payload, _ = self.invoke(
            plan, extra_argv=["--baseline-verdicts", str(loose)])
        self.assertEqual(rc, 2)
        self.assertIn("must sit beside its run's manifest.yaml",
                      payload["error"])

    def verify(self, directory):
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--verify", str(directory)],
            capture_output=True, text=True)
        return (proc.returncode,
                json.loads(proc.stdout) if proc.stdout.strip() else None, proc)


class TestVerify(RunnerCase):
    """SS1/SS9: --verify re-asserts completeness over an existing run dir, with
    no plan, no app calls, no scoring and no writes."""

    responder = staticmethod(health_only(ok_responder))

    def verify(self, directory):
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--verify", str(directory)],
            capture_output=True, text=True)
        try:
            payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        except json.JSONDecodeError:
            payload = None
        return proc.returncode, payload, proc

    def test_a_run_missing_its_verdict_rollups_is_incomplete(self):
        """THE audit finding, as a test.

        The hand-orchestrated runs this engine replaced looked finished and
        had no verdicts.jsonl and no verdicts_for_stats.jsonl -- so stats.py
        had nothing to pair and they could never be a baseline. Nothing
        failed and nothing warned at the time. --verify must exit 6 and name
        both files, and name ONLY those two: everything else is complete,
        which is exactly why the gap stayed invisible.

        The shape is built by finishing a real run and deleting the two
        rollups. It used to be read from the 2026-07-18 field-test archive
        that shipped at the repo root; that archive was removed 2026-09-20
        (`git show de9641a` still has it) and the assertion got stricter in
        the move -- the archived run was missing other artifacts too, so it
        could only ever be checked with assertIn.
        """
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001"), make_case("c-0002")])
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        for name in ("verdicts.jsonl", "verdicts_for_stats.jsonl"):
            (self.out_dir() / name).unlink()

        rc, payload, proc = self.verify(self.out_dir())
        self.assertEqual(rc, 6, proc.stdout + proc.stderr)
        self.assertEqual(sorted(payload["missing_artifacts"]),
                         ["verdicts.jsonl", "verdicts_for_stats.jsonl"])

    def test_verify_passes_a_complete_run(self):
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001"), make_case("c-0002")])
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        rc, payload, proc = self.verify(self.out_dir())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(payload["missing_artifacts"], [])

    def test_verify_names_a_deleted_per_case_artifact(self):
        plan = make_plan(self.state, self.app.base_url)
        self.invoke(plan)
        (self.out_dir() / "cases" / "c-0001" / "expect.json").unlink()
        rc, payload, _ = self.verify(self.out_dir())
        self.assertEqual(rc, 6)
        self.assertIn("cases/c-0001/expect.json",
                      payload["missing_artifacts"])

    def test_verify_catches_a_doctored_verdicts_file(self):
        """SS9(c) check 3: the derived file must agree with the case dirs."""
        plan = make_plan(self.state, self.app.base_url,
                         cases=[make_case("c-0001"), make_case("c-0002")])
        self.invoke(plan)
        rows = self.jsonl("verdicts.jsonl")
        (self.out_dir() / "verdicts.jsonl").write_text(
            json.dumps(rows[0]) + "\n", encoding="utf-8")
        rc, payload, _ = self.verify(self.out_dir())
        self.assertEqual(rc, 6)
        self.assertTrue(any("c-0002" in item
                            for item in payload["missing_artifacts"]),
                        payload["missing_artifacts"])

    def test_verify_needs_a_directory(self):
        rc, payload, _ = self.verify(self.tmp / "nope")
        self.assertEqual(rc, 2)
        self.assertIn("not a directory", payload["error"])


class TestRetryWait(unittest.TestCase):
    """retry_wait is pure, so it is tested as a function: sleeping a real
    Retry-After in a subprocess run would cost the suite the seconds it
    asserts on. `run_cases` is imported at the top for exactly this."""

    def test_a_longer_retry_after_wins_on_429_and_503(self):
        for status in (429, 503):
            result = {"status": status, "headers": {"Retry-After": "7"}}
            self.assertEqual(run_cases.retry_wait(1, result), 7)

    def test_the_header_name_is_case_insensitive(self):
        result = {"status": 429, "headers": {"retry-after": " 7 "}}
        self.assertEqual(run_cases.retry_wait(1, result), 7)

    def test_the_declared_backoff_is_a_floor(self):
        result = {"status": 429, "headers": {"Retry-After": "1"}}
        self.assertEqual(run_cases.retry_wait(4, result), 4)

    def test_it_is_capped(self):
        result = {"status": 429, "headers": {"Retry-After": "86400"}}
        self.assertEqual(run_cases.retry_wait(1, result),
                         run_cases.RETRY_AFTER_CAP_S)

    def test_everything_else_keeps_the_declared_backoff(self):
        for result in (None, {"status": 500, "headers": {"Retry-After": "9"}},
                       {"status": 429, "headers": {}},
                       {"status": 429, "headers": {
                           "Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}},
                       {"status": 429, "headers": {"Retry-After": "-5"}}):
            with self.subTest(result=result):
                self.assertEqual(run_cases.retry_wait(2, result), 2)


if __name__ == "__main__":
    unittest.main()
