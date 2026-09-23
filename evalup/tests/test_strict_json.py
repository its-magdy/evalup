"""Inputs json.loads handles in ways a JSONDecodeError handler never sees.

All three came out of the 2026-09 audit, and all three are closed in ONE place
(`_common.loads_strict`), so they are tested through real CLIs: the promise is
the `{"error": ...}` / exit-2 contract at the process boundary, not a helper's
return value.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


def run(script, *argv):
    proc = subprocess.run([sys.executable, str(SCRIPTS / script), *argv],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


class TestStrictJson(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, name, text):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def assert_clean_exit_2(self, script, *argv):
        rc, out, err = run(script, *argv)
        self.assertNotIn("Traceback", err, err)
        self.assertEqual(rc, 2, out + err)
        self.assertIn("error", json.loads(out))
        return json.loads(out)["error"]

    def test_non_finite_numbers_are_refused_at_the_door(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                suite = self.write(
                    "s.json", '[{"id": "c-1", "expect": {"result": {"scalar": '
                    + constant + "}}}]")
                error = self.assert_clean_exit_2(
                    "validate_cases.py", "--cases", suite, "--no-capabilities")
                self.assertIn("is not JSON", error)

    def test_the_runner_refuses_a_plan_carrying_one(self):
        plan = self.write("plan.json", '{"run_id": NaN}')
        error = self.assert_clean_exit_2(
            "run_cases.py", "--plan", plan, "--out", str(self.tmp / "out"))
        self.assertIn("is not JSON", error)
        self.assertFalse((self.tmp / "out").exists())

    def test_a_jsonl_row_carrying_one_is_refused_with_its_line_number(self):
        rows = self.write(
            "r.jsonl", '{"case_id": "c-1", "verdict": "pass"}\n'
            '{"case_id": "c-2", "verdict": "pass", "score": NaN}\n')
        error = self.assert_clean_exit_2("reduce_repeats.py", rows)
        self.assertIn("is not JSON", error)
        self.assertIn(":2:", error)

    def test_nesting_past_the_recursion_limit_is_not_a_traceback(self):
        suite = self.write("deep.json", "[" * 200_000 + "]" * 200_000)
        self.assert_clean_exit_2(
            "validate_cases.py", "--cases", suite, "--no-capabilities")

    def test_an_integer_past_the_digit_limit_is_not_a_traceback(self):
        suite = self.write("big.json", '[{"id": ' + "9" * 100_000 + "}]")
        self.assert_clean_exit_2(
            "validate_cases.py", "--cases", suite, "--no-capabilities")

    def test_the_viewer_renders_a_run_that_holds_a_nan(self):
        """An app RESPONSE can carry a NaN no input check polices. Emitted bare
        it made the page's JSON.parse throw and the whole viewer render
        nothing; the embedded block must parse STRICTLY."""
        case = self.tmp / "cases" / "c-1"
        case.mkdir(parents=True)
        (case / "verdict.json").write_text(
            '{"case_id": "c-1", "trace_id": "t1", "verdict": "fail", '
            '"layers": {"execution": "fail"}, '
            '"answer": {"expected": NaN, "actual": -Infinity}}',
            encoding="utf-8")
        # manifest.yaml + cases/ is a run directory, so no --glob is needed:
        # the viewer used to fail by default on the layout `run` writes.
        (self.tmp / "manifest.yaml").write_text("{}", encoding="utf-8")
        out = self.tmp / "v.html"
        rc, stdout, err = run("build_review_viewer.py", str(self.tmp),
                              "-o", str(out))
        self.assertEqual(rc, 0, stdout + err)
        page = out.read_text(encoding="utf-8")
        block = re.search(r'id="trace-data">(.*?)</script>', page, re.S).group(1)

        def refuse(name):
            raise AssertionError(f"bare {name} in the embedded JSON")
        records = json.loads(block, parse_constant=refuse)["records"]
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main()
