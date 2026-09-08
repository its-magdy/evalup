#!/usr/bin/env python3
"""Tests for scripts/run_cases.py, the execution engine (docs/runner-contract.md).

Stdlib only (unittest + subprocess + http.server), matching the scripts
themselves, and the runner is driven as a REAL subprocess because its contract
with the calling skill is argv + stdout JSON + exit code, not a Python API.
The app under test is a fake HTTP app in a thread: every behaviour these tests
assert (a 500 storm, a deliberate 400, a hang) is a behaviour a real app has,
and none of them should need a real app to reproduce.

Step 5b-i scope: no scoring, so every non-infra case rolls up to `unscored`.
The pass/fail rollup, the REQUIRED_* table assertion and --verify are 5b-ii.

Run: python3 -m unittest discover -s tests -v   (from the plugin root)
"""
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
RUNNER = SCRIPTS / "run_cases.py"
RUN_ID = "smoke-20260908T120000Z"


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
        self.state = self.tmp / ".agent-eval"
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
            self.assertEqual(row["verdict"], "unscored")   # 5b-i: no scoring
            self.assertTrue(row["gating"])
            self.assertIsInstance(row["layers"], dict)
            # Flattened to verdict STRINGS here; the scorer objects live in the
            # case's own verdict.json.
            for value in row["layers"].values():
                self.assertIsInstance(value, str)
        # Nothing is `pass` or `fail` yet, so the stats file is legitimately
        # empty -- and it EXISTS, which is the whole point.
        self.assertTrue((self.out_dir() / "verdicts_for_stats.jsonl").is_file())
        self.assertEqual(self.jsonl("verdicts_for_stats.jsonl"), [])

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
        self.assertEqual(summary["unscored"], 2)
        self.assertEqual(summary["infra_errors"], 0)
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
        # 5b-i: every applicable layer says so out loud rather than passing.
        for name in ("http", "answer"):
            self.assertEqual(verdict["layers"][name]["verdict"], "unscored")
            self.assertIn("5b-ii", verdict["layers"][name]["reason"])

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

    def test_old_layout_state_dir_halts_before_spend(self):
        """SS4.3. The field-test state dir is exactly this shape."""
        (self.state / "baselines").mkdir()
        plan = make_plan(self.state, self.app.base_url)
        rc, payload, _ = self.invoke(plan)
        self.assertEqual(rc, 3)
        self.assertIn("pre-reports/<run-id> layout", payload["error"])
        self.assertIn("migrate-run-layout.md", payload["error"])
        self.assertEqual(self.app.calls, [])
        self.assertFalse(self.out_dir().exists())

    def test_old_layout_is_not_flagged_once_a_baseline_pointer_exists(self):
        (self.state / "runs").mkdir()
        (self.state / "reports" / "baseline.json").write_text(
            '{"run_id": "x"}', encoding="utf-8")
        rc, _, proc = self.invoke(make_plan(self.state, self.app.base_url))
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)

    def test_health_check_failure(self):
        plan = make_plan(self.state, self.app.base_url)
        plan["adapter"]["invocation"]["base_url"] = "http://127.0.0.1:1"
        rc, payload, _ = self.invoke(plan)
        self.assertEqual(rc, 3)
        self.assertIn("health check", payload["error"])

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
        """SS4.7. Raw per-case material stays local when PII is possible."""
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

    def test_never_live_tool_in_a_live_environment(self):
        case = make_case("c-0001")
        case["expect"]["tools"] = {"subset": ["send_email"]}
        verdict = self.skip_reason_for(
            case, tools=[{"name": "send_email", "side_effects": "never-live"}])
        self.assertIn("never-live tool(s) send_email",
                      verdict["layers"]["http"]["reason"])

    def test_multi_turn_case_without_a_session_contract(self):
        case = make_case("c-0001", input={"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "and then?"}]})
        verdict = self.skip_reason_for(case)
        self.assertIn("hard rule 3", verdict["layers"]["http"]["reason"])

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
        # Not infra: the app answered. 5b-i has no scoring, so `unscored`.
        self.assertEqual(self.read("cases", "c-0001",
                                   "verdict.json")["verdict"], "unscored")


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
        self.assertNotIn("c-0001", (out / "results.json").read_text(
            encoding="utf-8"))
        # The durable record DOES carry them: a paired diff needs them.
        rows = [json.loads(line) for line
                in (out / "verdicts.jsonl").read_text(
                    encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([r["case_id"] for r in rows], ["c-0001", "c-0002"])
        # ...and so do their ordinary case directories (run/SKILL.md SS2).
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
        self.assertEqual(summary["canaries"], {"n": 1, "passed": 0})
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
        deadline = time.time() + timeout
        while time.time() < deadline:
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
        rows = self.jsonl("verdicts.jsonl")
        self.assertGreaterEqual(len(rows), 2)
        self.assertLessEqual(len(rows), completed)
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
    """SS9(c), the half 5b-i asserts: `status: "ok"` is written only after a
    check. The REQUIRED_IF conditions and --verify land with 5b-ii."""

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
        """SS9(b): the declared table is the one SS6 documents."""
        sys.path.insert(0, str(SCRIPTS))
        try:
            import run_cases
        finally:
            sys.path.pop(0)
        self.assertEqual(run_cases.REQUIRED_ALWAYS,
                         ("manifest.yaml", "results.json", "verdicts.jsonl",
                          "verdicts_for_stats.jsonl"))
        self.assertEqual(run_cases.REQUIRED_PER_CASE,
                         ("request.json", "response.json", "verdict.json",
                          "expect.json", "answer.txt"))
        # report.md/.html are deliberately NOT required here: the skill writes
        # them (SS13).
        self.assertNotIn("report.md", run_cases.REQUIRED_ALWAYS)


if __name__ == "__main__":
    unittest.main()
