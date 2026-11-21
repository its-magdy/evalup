"""pin_baseline.py: reports/baseline.json is written by a script, never by
hand (#53). A live session pinned three times in forty minutes by copying
manifest fields into a hand-written file; one wrong key breaks every later
diff silently."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
QUICKSTART = ROOT / "examples" / "quickstart"
sys.path.insert(0, str(SCRIPTS))

import pin_baseline  # noqa: E402


def pin(*argv):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "pin_baseline.py"), *map(str, argv)],
        capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


class TestPinBaseline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        proc = subprocess.run(
            [sys.executable, str(QUICKSTART / "demo.py"), "--keep"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        line = next(ln for ln in proc.stdout.splitlines()
                    if ln.startswith("run directory:"))
        cls.demo_run = pathlib.Path(line.split(":", 1)[1].strip())
        cls.addClassCleanup(shutil.rmtree, cls.demo_run.parents[2], True)

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.reports = self.tmp / "reports"
        self.reports.mkdir()
        # The demo is a smoke run; the same tree relabelled `regression` at
        # k=1 is a legal regression pin (an app whose rate limit cannot take
        # k=3) and still passes --verify.
        run_id = self.demo_run.name.replace("smoke-", "regression-")
        self.run_dir = self.reports / run_id
        shutil.copytree(self.demo_run, self.run_dir)
        for name in ("manifest.yaml", "results.json"):
            self.edit(name, lambda d: d.update(run_id=run_id,
                                               mode="regression"))
        self.pointer = self.reports / "baseline.json"

    def edit(self, name, change):
        path = self.run_dir / name
        doc = json.loads(path.read_text(encoding="utf-8"))
        change(doc)
        path.write_text(json.dumps(doc), encoding="utf-8")

    def test_pins_from_the_manifest_and_names_k(self):
        rc, out, err = pin(self.run_dir)
        self.assertEqual(rc, 0, out + err)
        manifest = json.loads((self.run_dir / "manifest.yaml")
                              .read_text(encoding="utf-8"))
        doc = json.loads(self.pointer.read_text(encoding="utf-8"))
        for key in ("run_id", "dataset_version", "scoring_version", "k",
                    "mode"):
            self.assertEqual(doc[key], manifest[key], key)
        self.assertEqual(doc["schema_version"], 1)
        notes = " ".join(json.loads(out)["notes"])
        self.assertIn("--k 1", notes)
        # The umask's mode, not mkstemp's 0600, and no temp file left over.
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual(self.pointer.stat().st_mode & 0o777,
                         0o666 & ~umask)
        self.assertEqual(sorted(p.name for p in self.reports.iterdir()),
                         ["baseline.json", self.run_dir.name])

    def test_a_smoke_run_is_refused(self):
        self.edit("manifest.yaml", lambda m: m.update(mode="smoke"))
        rc, out, _ = pin(self.run_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn("mode is 'smoke'", json.loads(out)["error"])

    def test_a_scorer_errored_run_is_refused(self):
        def errored(results):
            results["exit_code"] = 7
            results["summary"]["scorer_errors"] = 1
        self.edit("results.json", errored)
        rc, out, _ = pin(self.run_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn("exit 7", json.loads(out)["error"])
        self.assertFalse(self.pointer.exists())

    def test_a_run_id_that_is_not_the_directory_is_refused(self):
        self.edit("manifest.yaml",
                  lambda m: m.update(run_id="regression-20260101T000000Z"))
        rc, out, _ = pin(self.run_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn("does not match the directory",
                      json.loads(out)["error"])

    def test_a_non_integer_k_is_refused(self):
        self.edit("manifest.yaml", lambda m: m.update(k="3"))
        rc, out, _ = pin(self.run_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn("manifest k is '3'", json.loads(out)["error"])

    def test_dot_from_inside_the_run_directory(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "pin_baseline.py"), "."],
            capture_output=True, text=True, timeout=120, cwd=self.run_dir)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(self.pointer.exists())

    def test_re_pinning_the_same_run_and_an_unreadable_pointer(self):
        self.pointer.write_text("{not json", encoding="utf-8")
        rc, out, err = pin(self.run_dir)
        self.assertEqual(rc, 0, out + err)
        report = json.loads(out)
        self.assertIsNone(report["replaced"])
        self.assertIn("unreadable", " ".join(report["notes"]))
        rc, out, _ = pin(self.run_dir)
        self.assertEqual(rc, 0, out)
        self.assertIn("already the baseline", " ".join(json.loads(out)["notes"]))

    def test_a_running_run_is_refused_and_nothing_is_written(self):
        self.edit("results.json",
                  lambda r: r["summary"].update(status="running"))
        rc, out, _ = pin(self.run_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn("'running'", json.loads(out)["error"])
        self.assertFalse(self.pointer.exists())

    def test_a_missing_artifact_is_refused(self):
        (self.run_dir / "verdicts.jsonl").unlink()
        rc, out, _ = pin(self.run_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn("--verify", json.loads(out)["error"])
        self.assertFalse(self.pointer.exists())

    def test_no_results_json_is_refused(self):
        (self.run_dir / "results.json").unlink()
        rc, out, _ = pin(self.run_dir)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.pointer.exists())

    def test_a_holdout_run_is_refused(self):
        self.edit("manifest.yaml", lambda m: m.update(
            mode="holdout", selecting_split="holdout"))
        rc, out, _ = pin(self.run_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn("holdout run never becomes",
                      json.loads(out)["error"])

    def test_an_ungated_run_needs_the_override(self):
        def ungate(results):
            for row in results["cases"]:
                row["gating"] = False
        self.edit("results.json", ungate)
        rc, out, _ = pin(self.run_dir)
        self.assertEqual(rc, 2, out)
        self.assertIn("no case in this run gates", json.loads(out)["error"])
        rc, out, err = pin(self.run_dir, "--allow-ungated")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("unreviewed labels", " ".join(json.loads(out)["notes"]))

    def test_a_previous_pointer_is_named_when_replaced(self):
        self.pointer.write_text(json.dumps(
            {"schema_version": 1, "run_id": "regression-20260101T000000Z",
             "dataset_version": 1, "scoring_version": 1}), encoding="utf-8")
        rc, out, err = pin(self.run_dir)
        self.assertEqual(rc, 0, out + err)
        report = json.loads(out)
        self.assertEqual(report["replaced"], "regression-20260101T000000Z")
        self.assertIn("replaces baseline", " ".join(report["notes"]))
        self.assertEqual(json.loads(self.pointer.read_text(
            encoding="utf-8"))["run_id"], self.run_dir.name)

    def test_not_a_run_directory(self):
        rc, out, _ = pin(self.tmp)
        self.assertEqual(rc, 2, out)
        self.assertIn("manifest.yaml", json.loads(out)["error"])


class TestPinNotes(unittest.TestCase):
    """The notes, without a run on disk: a red run pins, and says so."""

    RESULTS = {"exit_code": 0, "cases": [{"case_id": "c-1", "gating": True,
                                          "verdict": "fail",
                                          "layers": {"answer": "fail"}}],
               "summary": {"status": "ok", "n": 1, "passes": 0,
                           "failures": 1, "gating_failures": 1}}

    def test_a_closed_gate_is_named(self):
        notes = pin_baseline.notes_for(
            {"run_id": "regression-20261120T000000Z", "k": 3,
             "mode": "regression", "dataset_version": 4},
            self.RESULTS, None, False)
        self.assertTrue(any("gate is CLOSED" in n and "c-1" in n
                            for n in notes), notes)
        self.assertIn("later diffs against this baseline need k=3", notes)
        self.assertFalse(any("--k 1" in n for n in notes))

    def test_an_older_dataset_than_the_replaced_pointer_is_named(self):
        notes = pin_baseline.notes_for(
            {"run_id": "regression-20261120T000000Z", "k": 3,
             "mode": "regression", "dataset_version": 2},
            self.RESULTS, {"run_id": "regression-20261121T000000Z",
                           "dataset_version": 3}, False)
        self.assertTrue(any("OLDER" in n for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
