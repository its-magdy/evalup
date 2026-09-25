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
import subprocess
import sys
import tempfile
import threading
import time
import unittest

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

    def test_help_and_version(self):
        for flag in ("--help", "--version"):
            proc = subprocess.run([sys.executable, str(SCRIPT), flag],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, flag)
            self.assertTrue((proc.stdout + proc.stderr).strip(), flag)
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
