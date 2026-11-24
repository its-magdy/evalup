"""scripts/wait_run.py: the bounded wait `run` loops on under `claude -p`.

The 2026-09 field test's headless `run` and `start` sessions launched
run_cases.py with `run_in_background`, ended the turn, and the CLI's exit
killed the runner mid-case, leaving results.json at `status: "running"`. The
skill now waits in the foreground and, past the ten-minute cap, loops on this
script. These tests pin its four exits: finalized (0), still running (3), the
runner is gone (4), and bad input (2) -- and that it never writes.
"""
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
SCRIPT = SCRIPTS / "wait_run.py"


def wait_run(*argv):
    proc = subprocess.run([sys.executable, str(SCRIPT), *[str(a) for a in argv]],
                          capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = None
    return proc.returncode, payload, proc


def results(run_id, status, exit_code=None):
    return {"run_id": run_id, "mode": "smoke", "cases": [],
            "summary": {"status": status}, "exit_code": exit_code}


class WaitRunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_id = "smoke-20260924T143704Z"
        self.run_dir = pathlib.Path(self._tmp.name) / "reports" / self.run_id
        self.run_dir.mkdir(parents=True)

    def write(self, name, obj):
        (self.run_dir / name).write_text(json.dumps(obj), encoding="utf-8")

    def reap_later(self, proc):
        # LIFO: kill first, then wait, so no ResourceWarning about a
        # still-running child leaks out of the test.
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)

    def snapshot(self):
        return sorted((p.name, p.stat().st_mtime_ns)
                      for p in self.run_dir.iterdir())

    def test_a_finalized_run_exits_0_with_its_status_and_exit_code(self):
        self.write("results.json", results(self.run_id, "ok", 0))
        (self.run_dir / "run.log").write_text(
            json.dumps({"event": "case_done"}) + "\n"
            + json.dumps({"event": "finalize", "status": "ok",
                          "exit_code": 0}) + "\n", encoding="utf-8")
        before = self.snapshot()
        rc, out, proc = wait_run(self.run_dir, "--timeout-s", "5")
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual((out["run_id"], out["status"], out["exit_code"]),
                         (self.run_id, "ok", 0))
        self.assertEqual(out["last_event"]["event"], "finalize")
        # It reads; it never writes.
        self.assertEqual(self.snapshot(), before)

    def test_an_aborted_run_is_finalized_too(self):
        # exit 5 (infra) or 4 (canary) still writes a final results.json;
        # "finalized" is "no longer running", not "passed".
        self.write("results.json", results(self.run_id, "aborted", 5))
        rc, out, _ = wait_run(self.run_dir, "--timeout-s", "5")
        self.assertEqual(rc, 0)
        self.assertEqual((out["status"], out["exit_code"]), ("aborted", 5))

    def test_still_running_with_a_live_runner_exits_3_at_the_timeout(self):
        self.write("results.json", results(self.run_id, "running"))
        # A stand-in for run_cases.py: its command line names the script and
        # this run's --out, which is what the process probe matches on.
        fake = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)",
             "run_cases.py", "--out", str(self.run_dir)])
        self.reap_later(fake)
        started = time.monotonic()
        rc, out, proc = wait_run(self.run_dir, "--timeout-s", "1",
                                 "--interval-s", "0.2")
        self.assertEqual(rc, 3, proc.stdout + proc.stderr)
        self.assertEqual(out["status"], "running")
        self.assertEqual(out["runner_process"], "alive")
        self.assertGreaterEqual(time.monotonic() - started, 1.0)
        self.assertLess(time.monotonic() - started, 8.0)

    def test_running_with_no_runner_process_exits_4_and_names_resume(self):
        self.write("results.json", results(self.run_id, "running"))
        (self.run_dir / "run.log").write_text(
            json.dumps({"event": "case_start", "case_id": "c-1"}) + "\n",
            encoding="utf-8")
        rc, out, proc = wait_run(self.run_dir, "--timeout-s", "30")
        self.assertEqual(rc, 4, proc.stdout + proc.stderr)
        self.assertEqual(out["runner_process"], "gone")
        self.assertEqual(out["status"], "running")
        self.assertEqual(out["last_event"]["event"], "case_start")
        self.assertIn("--resume", out["resume"])
        self.assertLess(out["waited_s"], 10)

    def test_a_rate_limited_resume_carries_not_before_one_window_on(self):
        # #144: --resume at once would share the killed run's last window.
        self.write("results.json", results(self.run_id, "running"))
        self.write("manifest.yaml", {"invocation": {
            "mode": "http", "rate_limit": {"requests": 20, "window_s": 2400}}})
        before = datetime.now(timezone.utc).replace(microsecond=0)
        rc, out, proc = wait_run(self.run_dir, "--timeout-s", "30")
        after = datetime.now(timezone.utc)
        self.assertEqual(rc, 4, proc.stdout + proc.stderr)
        when = datetime.strptime(out["not_before"], "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=timezone.utc)
        self.assertLessEqual(before + timedelta(seconds=2400), when)
        self.assertLessEqual(when, after + timedelta(seconds=2401))  # rounded up
        self.assertEqual(shlex.split(out["resume"]),
                         ["evalup", "run-cases", "--plan", "<plan.json>",
                          "--out", str(self.run_dir), "--resume",
                          "--not-before", out["not_before"]])
        self.assertIn("no new one", out["holdout_look"])
        # #153: no plan_sha256, so no plan is named, and the note says the
        # placeholder must be substituted.
        self.assertIn("placeholder", out["plan_note"])

    def plan_at_work(self, plan):
        work = self.run_dir.parent.parent / "work"
        work.mkdir(exist_ok=True)
        path = work / "plan.json"
        path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        return path

    def test_the_resume_names_the_plan_the_run_started_from(self):
        # #153: `--plan '<plan.json>'` copied as printed fails. The
        # conventional <state>/work/plan.json is named when it still hashes
        # to the manifest's plan_sha256 -- the runner's own canonical hash,
        # so re-serializing the plan with other spacing does not matter.
        sys.path.insert(0, str(SCRIPTS))
        from run_cases import sha256_of
        plan = {"run_id": self.run_id, "k": 3, "cases": [{"id": "c-1"}]}
        path = self.plan_at_work(plan)
        self.write("results.json", results(self.run_id, "running"))
        self.write("manifest.yaml", {"plan_sha256": sha256_of(plan),
                                     "invocation": {"mode": "http"}})
        rc, out, proc = wait_run(self.run_dir, "--timeout-s", "30")
        self.assertEqual(rc, 4, proc.stdout + proc.stderr)
        argv = shlex.split(out["resume"])
        self.assertEqual(argv[argv.index("--plan") + 1], str(path))
        self.assertNotIn("plan_note", out)

    def test_a_rebuilt_plan_is_not_named(self):
        # The file is rebuilt every run: a plan that no longer matches would
        # be refused by --resume, so the placeholder and its note stay.
        sys.path.insert(0, str(SCRIPTS))
        from run_cases import sha256_of
        self.plan_at_work({"run_id": "smoke-20990101T000000Z", "k": 1})
        self.write("results.json", results(self.run_id, "running"))
        self.write("manifest.yaml", {
            "plan_sha256": sha256_of({"run_id": self.run_id, "k": 3}),
            "invocation": {"mode": "http"}})
        rc, out, _ = wait_run(self.run_dir, "--timeout-s", "30")
        self.assertEqual(rc, 4)
        argv = shlex.split(out["resume"])
        self.assertEqual(argv[argv.index("--plan") + 1], "<plan.json>")
        self.assertIn("plan_sha256", out["plan_note"])

    def test_the_resume_keeps_the_baseline_and_quotes_its_paths(self):
        # #144: a resume without --baseline-verdicts rewrites the manifest
        # with none and scores no comparison; a space must not split a path.
        spaced = pathlib.Path(self._tmp.name) / "My App" / self.run_id
        spaced.mkdir(parents=True)
        self.run_dir = spaced
        self.write("results.json", results(self.run_id, "running"))
        baseline = "My App/reports/base/verdicts_for_stats.jsonl"
        self.write("manifest.yaml", {"baseline_verdicts": baseline,
                                     "invocation": {"mode": "http"}})
        rc, out, _ = wait_run(spaced, "--timeout-s", "30")
        self.assertEqual(rc, 4)
        argv = shlex.split(out["resume"])
        self.assertEqual(argv[argv.index("--out") + 1], str(spaced))
        self.assertEqual(argv[argv.index("--baseline-verdicts") + 1], baseline)

    def test_not_before_rounds_up_to_the_second(self):
        sys.path.insert(0, str(SCRIPTS))
        import wait_run as module
        self.write("manifest.yaml", {"invocation": {
            "rate_limit": {"min_interval_s": 0.4}}})
        now = datetime(2026, 11, 23, 10, 0, 0, 700000, tzinfo=timezone.utc)
        _, when, _ = module.resume_of(str(self.run_dir), now=now)
        self.assertEqual(when, "2026-11-23T10:00:02Z")

    def test_with_no_budget_the_resume_has_no_not_before(self):
        self.write("results.json", results(self.run_id, "running"))
        self.write("manifest.yaml", {"invocation": {"mode": "http"}})
        rc, out, _ = wait_run(self.run_dir, "--timeout-s", "30")
        self.assertEqual(rc, 4)
        self.assertNotIn("--not-before", out["resume"])
        self.assertNotIn("not_before", out)

    def test_returns_0_as_soon_as_the_run_finalizes_during_the_wait(self):
        self.write("results.json", results(self.run_id, "running"))
        fake = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)",
             "run_cases.py", "--out", str(self.run_dir)])
        self.reap_later(fake)

        def finalize_later():
            time.sleep(1.0)
            self.write("results.json", results(self.run_id, "ok", 0))
        threading.Thread(target=finalize_later, daemon=True).start()
        started = time.monotonic()
        rc, out, proc = wait_run(self.run_dir, "--timeout-s", "20",
                                 "--interval-s", "0.2")
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(out["status"], "ok")
        self.assertLess(time.monotonic() - started, 10.0)

    def test_a_manifest_without_results_yet_counts_as_a_started_run(self):
        # results.json is written at pre-flight, so this window is short, but
        # a run directory with only manifest.yaml is a run, not bad input.
        (self.run_dir / "manifest.yaml").write_text("run_id: x\n",
                                                    encoding="utf-8")
        rc, out, _ = wait_run(self.run_dir, "--timeout-s", "30")
        self.assertEqual(rc, 4)   # no results, no runner: killed at pre-flight

    def test_a_run_not_yet_started_is_waited_on_while_its_runner_lives(self):
        # #111: run_cases.py --not-before writes nothing while it waits, so
        # the directory is absent; a live runner naming it makes that a run
        # not started yet (exit 3, call again), not bad input.
        pending = self.run_dir.parent / "regression-20260924T150000Z"
        fake = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)",
             "run_cases.py", "--not-before", "2026-09-24T15:00:00Z",
             "--out", str(pending)])
        self.reap_later(fake)
        rc, out, proc = wait_run(pending, "--timeout-s", "1",
                                 "--interval-s", "0.2")
        self.assertEqual(rc, 3, proc.stdout + proc.stderr)
        self.assertEqual((out["status"], out["runner_process"]),
                         ("not_started", "alive"))
        self.assertFalse(pending.exists())   # it reads; it never writes

        def start_and_finish():
            time.sleep(0.8)
            pending.mkdir()
            (pending / "results.json").write_text(json.dumps(
                results(pending.name, "ok", 0)), encoding="utf-8")
        threading.Thread(target=start_and_finish, daemon=True).start()
        rc, out, proc = wait_run(pending, "--timeout-s", "20",
                                 "--interval-s", "0.2")
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(out["status"], "ok")
        # With the runner gone, an absent directory is bad input again.
        fake.kill()
        fake.wait()
        rc, out, _ = wait_run(self.run_dir.parent / "smoke-20260924T160000Z")
        self.assertEqual(rc, 2)
        self.assertIn("relaunch", out["error"])

    def test_without_ps_a_run_not_started_is_still_waited_on(self):
        # Review of #111: "unknown" (no ps) is not "gone" -- an exit 2 there
        # would invite a second launch beside a runner still waiting.
        pending = self.run_dir.parent / "regression-20260924T170000Z"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), str(pending), "--timeout-s", "1",
             "--interval-s", "0.2"], capture_output=True, text=True,
            env=dict(os.environ, PATH=""))
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out["status"], out["runner_process"]),
                         ("not_started", "unknown"))

    def test_bad_input_exits_2_with_the_error_object(self):
        rc, out, _ = wait_run(self.run_dir / "missing")
        self.assertEqual(rc, 2)
        self.assertIn("not a directory", out["error"])
        empty = pathlib.Path(self._tmp.name) / "empty"
        empty.mkdir()
        rc, out, _ = wait_run(empty)
        self.assertEqual(rc, 2)
        self.assertIn("not a run directory", out["error"])
        self.write("results.json", results(self.run_id, "ok", 0))
        rc, out, _ = wait_run(self.run_dir, "--timeout-s", "601")
        self.assertEqual(rc, 2)
        self.assertIn("--timeout-s", out["error"])
        # #153: the cap is said, with the way to wait longer.
        self.assertIn("call wait-run again", out["error"])

    def test_help_and_version(self):
        for flag in ("--help", "--version"):
            proc = subprocess.run([sys.executable, str(SCRIPT), flag],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, flag)
            self.assertTrue((proc.stdout + proc.stderr).strip(), flag)
            if flag == "--help":
                self.assertIn("at most 600", " ".join(proc.stdout.split()))
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
