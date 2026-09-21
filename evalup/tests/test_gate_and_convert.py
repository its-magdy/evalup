"""gate.py and convert_suite.py: the two steps that used to be the model's.

Both exist because of the same finding (2026-09 audit): a mechanical step left
to prose. The CI gate was a bash snippet needing jq and yq, so at the shell a
red suite and a green one were both rc 0 with nothing printed; and the
YAML -> JSON conversion was the model re-typing the suite on every run.
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
QUICKSTART = ROOT / "examples" / "quickstart"


def run(script, *argv):
    proc = subprocess.run([sys.executable, str(SCRIPTS / script), *argv],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)


class TestGate(TempDirTest):
    def write_run(self, run_id="regression-20260901T120000Z", exit_code=0,
                  **summary):
        base = {"status": "ok", "n": 6, "attempted": 6, "passes": 6,
                "failures": 0, "gating_failures": 0, "unscored": 0,
                "skipped": 0, "infra_errors": 0, "infra_rate": 0.0,
                "crash_rate": 0.0, "scorer_errors": 0,
                "unscorable_layers": ["trajectory"], "unjudged": "mode: smoke",
                "canaries": {"n": 2, "passed": 2}, "holdout": None,
                "missing_artifacts": []}
        base.update(summary)
        out = self.tmp / "reports" / run_id
        out.mkdir(parents=True, exist_ok=True)      # subtests rewrite one run
        (out / "results.json").write_text(json.dumps(
            {"run_id": run_id, "mode": run_id.split("-")[0], "cases": [],
             "summary": base, "exit_code": exit_code}), encoding="utf-8")
        return str(out)

    # write_run() fabricates a results.json with no run directory around it,
    # so these tests gate it with --no-verify; TestGateVerifiesTheRunDirectory
    # below covers the default.
    def test_a_green_run_is_0_and_says_what_it_could_not_see(self):
        rc, out, err = run("gate.py", "--no-verify", self.write_run())
        self.assertEqual(rc, 0, out + err)
        self.assertIn("PASS", out)
        self.assertIn("NOT scored this run: trajectory", out)

    def test_a_red_run_is_1(self):
        # The whole point: run_cases.py exits 0 on this, by design.
        rc, out, _ = run("gate.py", "--no-verify", self.write_run(
            passes=0, failures=6, gating_failures=6))
        self.assertEqual(rc, 1)
        self.assertIn("6 gating failure(s)", out)

    def test_every_closing_reason_is_listed_not_the_first(self):
        rc, out, _ = run("gate.py", "--json", "--no-verify", self.write_run(
            exit_code=7, status="incomplete", gating_failures=1,
            infra_rate=0.5, missing_artifacts=["verdicts.jsonl"],
            holdout={"n": 2, "passes": 1, "gating_failures": 1}))
        self.assertEqual(rc, 1)
        payload = json.loads(out)
        self.assertEqual(payload["gate"], "closed")
        self.assertEqual(len(payload["reasons"]), 6)
        self.assertEqual(payload["gating_failures"], 2)

    def test_zero_failures_from_an_aborted_run_is_not_a_pass(self):
        rc, _, _ = run("gate.py", "--no-verify",
                       self.write_run(status="aborted_infra", exit_code=5))
        self.assertEqual(rc, 1)

    def test_the_infra_threshold_is_a_flag(self):
        path = self.write_run(infra_rate=0.2)
        self.assertEqual(run("gate.py", "--no-verify", path)[0], 1)
        self.assertEqual(run("gate.py", "--no-verify", path,
                             "--max-infra-rate", "0.25")[0], 0)

    def test_latest_is_by_the_run_ids_timestamp_never_mtime(self):
        self.write_run("regression-20260902T120000Z", gating_failures=3)
        older = self.write_run("regression-20260901T120000Z")
        self.write_run("smoke-20260903T120000Z")
        # Touch the OLDER run last: an mtime pick would choose it and pass.
        pathlib.Path(older, "results.json").touch()
        reports = str(self.tmp / "reports")
        rc, out, _ = run("gate.py", "--no-verify", reports, "--latest",
                         "--mode", "regression")
        self.assertEqual(rc, 1)
        self.assertIn("regression-20260902T120000Z", out)
        rc, out, _ = run("gate.py", "--no-verify", reports, "--latest")
        self.assertIn("smoke-20260903T120000Z", out)

    def test_bad_input_is_the_shared_error_contract(self):
        for argv in ([str(self.tmp)], [str(self.tmp), "--latest"],
                     [self.write_run(), "--mode", "smoke"],
                     [self.write_run("smoke-20260901T120000Z"),
                      "--max-infra-rate", "7"]):
            with self.subTest(argv=argv[1:]):
                rc, out, err = run("gate.py", *argv)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("error", json.loads(out))
                self.assertNotIn("Traceback", err)

    def test_nothing_measured_is_not_a_pass(self):
        # 2026-09-21 audit: a suite of multi-turn cases is skipped whole, by
        # design, and gated green forever. skipped/unscored are never a
        # failure -- which is why only they cannot be a pass either.
        for label, summary in (
                ("all skipped", {"passes": 0, "skipped": 6}),
                ("all unscored", {"passes": 0, "unscored": 6}),
                ("no cases", {"n": 0, "passes": 0})):
            with self.subTest(label):
                rc, out, _ = run("gate.py", "--no-verify",
                                 self.write_run(**summary))
                self.assertEqual(rc, 1, out)
                self.assertIn("nothing was measured", out)

    def test_a_count_that_is_not_an_int_closes_the_gate(self):
        # `"gating_failures": "10"` used to read as 0: ten failures, PASS.
        for value in ("10", 10.0, None, True, -1):
            with self.subTest(value=value):
                rc, out, _ = run("gate.py", "--no-verify", self.write_run(
                    passes=0, failures=10, gating_failures=value))
                self.assertEqual(rc, 1, out)
                self.assertIn("summary.gating_failures", out)

    def test_a_two_key_results_json_is_not_a_pass(self):
        out_dir = self.tmp / "reports" / "regression-20260901T120000Z"
        out_dir.mkdir(parents=True)
        (out_dir / "results.json").write_text(json.dumps(
            {"run_id": out_dir.name, "summary": {"status": "ok"}}),
            encoding="utf-8")
        self.assertEqual(run("gate.py", "--no-verify", str(out_dir))[0], 1)

    def test_a_closed_gate_names_the_failing_cases(self):
        path = self.write_run(passes=5, failures=1, gating_failures=1)
        results = json.loads(
            pathlib.Path(path, "results.json").read_text(encoding="utf-8"))
        results["cases"] = [
            {"case_id": "c-aaaaaaaa", "verdict": "pass", "gating": True,
             "layers": {"answer": "pass"}},
            {"case_id": "c-bbbbbbbb", "verdict": "fail", "gating": True,
             "layers": {"routing": "pass", "answer": "fail"}},
            {"case_id": "c-cccccccc", "verdict": "fail", "gating": False,
             "layers": {"answer": "fail"}}]
        pathlib.Path(path, "results.json").write_text(json.dumps(results),
                                                      encoding="utf-8")
        rc, out, _ = run("gate.py", "--no-verify", path)
        self.assertEqual(rc, 1)
        self.assertIn("c-bbbbbbbb (answer)", out)
        self.assertNotIn("c-cccccccc", out)

    def test_no_verify_says_so(self):
        _, out, _ = run("gate.py", "--no-verify", self.write_run())
        self.assertIn("artifacts NOT verified", out)


class TestGateVerifiesTheRunDirectory(TempDirTest):
    """results.json is the file under audit, so its own `missing_artifacts: []`
    is not evidence. The gate recounts from cases/*/verdict.json by calling
    run_cases.py --verify (2026-09-21 audit: --verify caught a doctored run and
    gate.py said PASS over the same directory)."""

    def real_run(self):
        proc = subprocess.run(
            [sys.executable, str(QUICKSTART / "demo.py"), "--keep"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        line = next(ln for ln in proc.stdout.splitlines()
                    if ln.startswith("run directory:"))
        run_dir = pathlib.Path(line.split(":", 1)[1].strip())
        self.addCleanup(shutil.rmtree, run_dir.parents[2], True)
        return run_dir

    def test_a_real_run_gates_green_with_verification_on(self):
        rc, out, err = run("gate.py", str(self.real_run()))
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("NOT verified", out)

    def test_a_verdict_that_disagrees_with_results_json_closes_the_gate(self):
        run_dir = self.real_run()
        verdict_path = next((run_dir / "cases").iterdir()) / "verdict.json"
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        verdict["verdict"] = "fail"
        verdict_path.write_text(json.dumps(verdict), encoding="utf-8")
        rc, out, _ = run("gate.py", str(run_dir))
        self.assertEqual(rc, 1, out)
        self.assertIn("fails run_cases.py --verify", out)

    def test_a_fabricated_results_json_does_not_verify(self):
        out_dir = self.tmp / "reports" / "regression-20260901T120000Z"
        out_dir.mkdir(parents=True)
        (out_dir / "results.json").write_text(json.dumps(
            {"run_id": out_dir.name, "mode": "regression", "cases": [],
             "exit_code": 0,
             "summary": {"status": "ok", "n": 6, "passes": 6, "failures": 0,
                         "gating_failures": 0, "missing_artifacts": []}}),
            encoding="utf-8")
        self.assertEqual(run("gate.py", str(out_dir))[0], 1)


def have_yaml():
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(have_yaml(), "PyYAML not installed; convert_suite.py is "
                                  "the one script that needs it")
class TestConvertSuite(TempDirTest):
    def state(self):
        state = self.tmp / "state"
        shutil.copytree(QUICKSTART / ".evalup", state)
        return state

    def test_it_reproduces_the_committed_conversion_exactly(self):
        """converted.json was maintained by a README one-liner; this script is
        that step, owned. Same cases, same order, same adapter, same matrix."""
        rc, out, err = run("convert_suite.py", str(self.state()))
        self.assertEqual(rc, 0, err)
        new = json.loads(out)
        committed = json.loads(
            (QUICKSTART / "converted.json").read_text(encoding="utf-8"))
        for key in ("adapter", "capability_matrix", "cases"):
            self.assertEqual(new[key], committed[key], key)
        self.assertEqual(sorted(new["sources"]),
                         [case["id"] for case in committed["cases"]])

    def test_the_split_dir_feeds_the_linter_clean(self):
        split = self.tmp / "split"
        rc, _, err = run("convert_suite.py", str(self.state()),
                         "-o", str(self.tmp / "c.json"),
                         "--split-dir", str(split))
        self.assertEqual(rc, 0, err)
        rc, out, err = run("validate_cases.py", "--strict",
                           "--cases", str(split / "suite.json"),
                           "--capabilities", str(split / "capabilities.json"))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out)["findings"], [])

    def test_env_refs_are_left_unresolved(self):
        state = self.state()
        adapter = state / "adapter.yaml"
        adapter.write_text(adapter.read_text(encoding="utf-8")
                           + '\nnote: "${CONVERT_SUITE_SECRET}"\n',
                           encoding="utf-8")
        rc, out, err = run("convert_suite.py", str(state))
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["adapter"]["note"],
                         "${CONVERT_SUITE_SECRET}")

    def test_a_duplicate_key_is_an_error_not_the_last_value(self):
        state = self.state()
        (state / "datasets" / "dup.yaml").write_text(
            "id: c-aaaa0001\nsplit: [full]\nid: c-aaaa0002\n", encoding="utf-8")
        rc, out, err = run("convert_suite.py", str(state))
        self.assertEqual(rc, 2, err)
        self.assertIn("duplicate key 'id'", json.loads(out)["error"])

    def test_one_id_in_two_files_is_an_error(self):
        state = self.state()
        first = sorted((state / "datasets").glob("c-*.yaml"))[0]
        (state / "datasets" / "nested").mkdir()
        shutil.copy(first, state / "datasets" / "nested" / "copy.yaml")
        rc, out, _ = run("convert_suite.py", str(state))
        self.assertEqual(rc, 2)
        self.assertIn("is in both", json.loads(out)["error"])

    def test_the_manifest_and_templates_are_not_cases(self):
        state = self.state()
        (state / "datasets" / "dataset.yaml").write_text(
            "dataset_version: 3\nas_of: 2026-09-01\n", encoding="utf-8")
        (state / "datasets" / "templates").mkdir()
        (state / "datasets" / "templates" / "t.yaml").write_text(
            "dimensions: [a]\n", encoding="utf-8")
        rc, out, err = run("convert_suite.py", str(state))
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertEqual(len(doc["cases"]), 6)
        # A YAML date is written as the ISO string the file spelled.
        self.assertEqual(doc["manifest"],
                         {"dataset_version": 3, "as_of": "2026-09-01"})

    def test_bad_input_is_the_shared_error_contract(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        broken = self.state()
        (broken / "adapter.yaml").write_text("a: [unclosed\n", encoding="utf-8")
        for path in (self.tmp / "missing", empty, broken):
            with self.subTest(path=path.name):
                rc, out, err = run("convert_suite.py", str(path))
                self.assertEqual(rc, 2, out + err)
                self.assertIn("error", json.loads(out))
                self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
