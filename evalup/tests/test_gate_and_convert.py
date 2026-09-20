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
        out.mkdir(parents=True)
        (out / "results.json").write_text(json.dumps(
            {"run_id": run_id, "mode": run_id.split("-")[0], "cases": [],
             "summary": base, "exit_code": exit_code}), encoding="utf-8")
        return str(out)

    def test_a_green_run_is_0_and_says_what_it_could_not_see(self):
        rc, out, err = run("gate.py", self.write_run())
        self.assertEqual(rc, 0, out + err)
        self.assertIn("PASS", out)
        self.assertIn("NOT scored this run: trajectory", out)

    def test_a_red_run_is_1(self):
        # The whole point: run_cases.py exits 0 on this, by design.
        rc, out, _ = run("gate.py", self.write_run(
            passes=0, failures=6, gating_failures=6))
        self.assertEqual(rc, 1)
        self.assertIn("6 gating failure(s)", out)

    def test_every_closing_reason_is_listed_not_the_first(self):
        rc, out, _ = run("gate.py", "--json", self.write_run(
            exit_code=7, status="incomplete", gating_failures=1,
            infra_rate=0.5, missing_artifacts=["verdicts.jsonl"],
            holdout={"n": 2, "passes": 1, "gating_failures": 1}))
        self.assertEqual(rc, 1)
        payload = json.loads(out)
        self.assertEqual(payload["gate"], "closed")
        self.assertEqual(len(payload["reasons"]), 6)
        self.assertEqual(payload["gating_failures"], 2)

    def test_zero_failures_from_an_aborted_run_is_not_a_pass(self):
        rc, _, _ = run("gate.py", self.write_run(status="aborted_infra",
                                                 exit_code=5))
        self.assertEqual(rc, 1)

    def test_the_infra_threshold_is_a_flag(self):
        path = self.write_run(infra_rate=0.2)
        self.assertEqual(run("gate.py", path)[0], 1)
        self.assertEqual(run("gate.py", path, "--max-infra-rate", "0.25")[0], 0)

    def test_latest_is_by_the_run_ids_timestamp_never_mtime(self):
        self.write_run("regression-20260902T120000Z", gating_failures=3)
        older = self.write_run("regression-20260901T120000Z")
        self.write_run("smoke-20260903T120000Z")
        # Touch the OLDER run last: an mtime pick would choose it and pass.
        pathlib.Path(older, "results.json").touch()
        reports = str(self.tmp / "reports")
        rc, out, _ = run("gate.py", reports, "--latest", "--mode", "regression")
        self.assertEqual(rc, 1)
        self.assertIn("regression-20260902T120000Z", out)
        rc, out, _ = run("gate.py", reports, "--latest")
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
