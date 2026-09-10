#!/usr/bin/env python3
"""Tests for scripts/score_agreement.py and the judged gate it feeds.

Stdlib only (unittest + subprocess), matching the scorers themselves; the
scorer is exercised as a real subprocess because argv + stdout JSON + exit code
is its actual contract with the calling skill.

The gate is the point. Before this scorer, `judge.status: calibrated` was a
word an LLM wrote into profile.yaml -- `profile-schema.md` said the flag was
DERIVED and nothing derived it. So TestJudgedGate is not an add-on: it asserts
that the flag alone no longer opens the judged layer, which is the half of the
fix that lives in run_cases.py.

Run: python3 -m unittest discover -s tests -v   (from the plugin root)
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
SCORER = SCRIPTS / "score_agreement.py"

sys.path.insert(0, str(SCRIPTS))
import run_cases  # noqa: E402  - constructed directly for its gate method


def run_scorer(*argv):
    proc = subprocess.run(
        [sys.executable, str(SCORER), *[str(a) for a in argv]],
        capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stderr


def calibrated_rows(rubric="billing_answer", n_fail=60, n_pass=60,
                    misses=2, false_alarms=3):
    """A pass that clears every floor, built deterministically, not sampled."""
    rows = []
    for i in range(n_fail):
        rows.append({"case_id": f"{rubric}-f{i}", "rubric_id": rubric,
                     "node_id": "correct_amount", "label": "fail",
                     "judge_label": "pass" if i < misses else "fail"})
    for i in range(n_pass):
        rows.append({"case_id": f"{rubric}-p{i}", "rubric_id": rubric,
                     "node_id": "correct_amount", "label": "pass",
                     "judge_label": "fail" if i < false_alarms else "pass"})
    return rows


class ScorerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, rows, name="annotations.jsonl"):
        path = self.tmp / name
        path.write_text("".join(json.dumps(r) + "\n" for r in rows),
                        encoding="utf-8")
        return path

    def score(self, rows, *argv):
        return run_scorer(self.write(rows), *argv)


class TestStatistics(ScorerCase):
    def test_a_clean_pass_is_calibrated_and_reports_no_accuracy(self):
        rc, out, _ = self.score(calibrated_rows())
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "calibrated")
        block = out["rubrics"]["billing_answer"]
        self.assertEqual(block["matrix"],
                         {"tp": 58, "fn": 2, "fp": 3, "tn": 57})
        self.assertEqual(block["tpr"], 0.9667)
        self.assertEqual(block["tnr"], 0.95)
        self.assertTrue(block["calibrated"])
        self.assertEqual(block["blocking"], [])
        # Raw accuracy is the trap the whole calibration doc is about; it must
        # not appear as a number anywhere, only as the prose saying why.
        self.assertIsInstance(out["accuracy"], str)
        self.assertEqual(out["positive_class"], "fail")

    def test_positive_is_fail_so_an_always_pass_judge_scores_tpr_zero(self):
        """THE regression test for this scorer. Get the positive class
        backwards and an always-pass judge reads as near-perfect -- exactly the
        90%-pass-app trap rubric-format.md names."""
        rows = [{"case_id": f"f{i}", "label": "fail", "judge_label": "pass"}
                for i in range(12)]
        rows += [{"case_id": f"p{i}", "label": "pass", "judge_label": "pass"}
                 for i in range(108)]
        rc, out, _ = self.score(rows)
        self.assertEqual(rc, 0)
        block = out["rubrics"]["__unspecified__"]
        self.assertEqual(block["tpr"], 0.0)
        self.assertEqual(block["tnr"], 1.0)
        self.assertEqual(out["status"], "uncalibrated")
        self.assertTrue(any("tpr 0.0" in r for r in block["blocking"]))

    def test_clopper_pearson_matches_the_textbook_exact_interval(self):
        """58/60 and 9/10 against R's binom.test, to 4dp."""
        rc, out, _ = self.score(calibrated_rows())
        block = out["rubrics"]["billing_answer"]
        self.assertEqual(block["tpr_ci_exact"], [0.8847, 0.9959])
        rows = ([{"case_id": f"f{i}", "label": "fail",
                  "judge_label": "fail" if i else "pass"} for i in range(10)]
                + [{"case_id": f"p{i}", "label": "pass",
                    "judge_label": "pass"} for i in range(10)])
        rc, out, _ = self.score(rows)
        self.assertEqual(out["rubrics"]["__unspecified__"]["tpr_ci_exact"],
                         [0.555, 0.9975])

    def test_a_perfect_column_gets_a_one_sided_interval_not_a_point(self):
        rows = ([{"case_id": f"f{i}", "label": "fail", "judge_label": "fail"}
                 for i in range(20)]
                + [{"case_id": f"p{i}", "label": "pass", "judge_label": "pass"}
                   for i in range(20)])
        rc, out, _ = self.score(rows)
        block = out["rubrics"]["__unspecified__"]
        self.assertEqual(block["tpr"], 1.0)
        self.assertEqual(block["tpr_ci_exact"][1], 1.0)
        self.assertLess(block["tpr_ci_exact"][0], 1.0)

    def test_kappa_and_the_exact_chance_agreement_p(self):
        rows = ([{"case_id": f"f{i}", "label": "fail",
                  "judge_label": "fail" if i < 8 else "pass"}
                 for i in range(10)]
                + [{"case_id": f"p{i}", "label": "pass",
                    "judge_label": "fail" if i < 1 else "pass"}
                   for i in range(10)])
        rc, out, _ = self.score(rows)
        block = out["rubrics"]["__unspecified__"]
        self.assertEqual(block["matrix"], {"tp": 8, "fn": 2, "fp": 1, "tn": 9})
        self.assertEqual(block["kappa"], 0.7)
        # Hypergeometric right tail: (C(10,8)C(10,1) + C(10,9)C(10,0))/C(20,9).
        self.assertAlmostEqual(block["chance_agreement_p_exact"],
                               460 / 167960, places=7)

    def test_the_chance_agreement_p_keeps_significant_figures(self):
        """round(p, 6) printed 0.0 for a strongly-agreeing table -- an exact
        test reporting a literal zero probability."""
        rc, out, _ = self.score(calibrated_rows())
        p = out["rubrics"]["billing_answer"]["chance_agreement_p_exact"]
        self.assertGreater(p, 0.0)
        self.assertLess(p, 1e-20)

    def test_kappa_is_undefined_not_zero_on_a_degenerate_table(self):
        """Both raters put everything in one class: (po-pe)/(1-pe) is 0/0.
        Reporting 0.0 would call perfect agreement chance-level; reporting 1.0
        would open the gate on a rubric that never saw the other class."""
        rows = [{"case_id": f"p{i}", "label": "pass", "judge_label": "pass"}
                for i in range(120)]
        rc, out, _ = self.score(rows)
        block = out["rubrics"]["__unspecified__"]
        self.assertIsNone(block["kappa"])
        self.assertIsNone(block["tpr"])
        self.assertFalse(block["calibrated"])
        self.assertTrue(any("undefined" in n for n in block["notes"]))


class TestFloors(ScorerCase):
    def test_a_thin_sample_is_blocked_by_min_labeled(self):
        rc, out, _ = self.score(calibrated_rows(n_fail=15, n_pass=15,
                                                misses=0, false_alarms=0))
        self.assertEqual(out["status"], "uncalibrated")
        blocking = out["rubrics"]["billing_answer"]["blocking"]
        self.assertTrue(any("labelled pairs 30 < 100" in b for b in blocking))
        self.assertTrue(any("~100-200" in b for b in blocking))

    def test_a_perfect_tpr_over_nine_cases_is_still_blocked(self):
        """min-per-class floors the DENOMINATOR: 100+ pairs is not enough if
        almost all of them are one class, which is the default shape of agent
        traffic and the reason the docs say to oversample the failing class."""
        rows = calibrated_rows(n_fail=9, n_pass=111, misses=0, false_alarms=0)
        rc, out, _ = self.score(rows)
        block = out["rubrics"]["billing_answer"]
        self.assertEqual(block["tpr"], 1.0)
        self.assertEqual(block["labelled_pairs"], 120)
        self.assertFalse(block["calibrated"])
        self.assertEqual(block["blocking"],
                         ["only 9 case(s) the human called FAIL, under 10 -- "
                          "TPR is the failure-catch rate and this is its "
                          "whole denominator"])

    def test_nine_of_ten_does_clear_the_floor_and_the_exact_ci_says_so(self):
        """Not a hole being closed -- a limit being stated. The gate follows
        the number the docs set; the honest uncertainty is reported beside it
        rather than smuggled into the decision as a stricter secret floor."""
        rows = calibrated_rows(n_fail=10, n_pass=110, misses=1,
                               false_alarms=0)
        rc, out, _ = self.score(rows)
        block = out["rubrics"]["billing_answer"]
        self.assertEqual(block["tpr"], 0.9)
        self.assertTrue(block["calibrated"])
        self.assertEqual(block["tpr_ci_exact"][0], 0.5550)
        self.assertTrue(any("lower bound is 0.555" in n
                            for n in block["notes"]))

    def test_floors_are_overridable_and_bounds_checked(self):
        rows = calibrated_rows(n_fail=15, n_pass=15, misses=0, false_alarms=0)
        rc, out, _ = self.score(rows, "--min-labeled", 30)
        self.assertEqual(rc, 0)
        self.assertEqual(out["status"], "calibrated")
        rc, out, _ = self.score(rows, "--tpr-floor", 1.5)
        self.assertEqual(rc, 2)
        self.assertIn("--tpr-floor", out["error"])

    def test_the_exact_lower_bound_is_reported_when_it_undercuts_the_floor(self):
        rc, out, _ = self.score(calibrated_rows())
        notes = out["rubrics"]["billing_answer"]["notes"]
        self.assertTrue(any("lower bound is 0.8847" in n for n in notes))
        # Reported, never gating.
        self.assertTrue(out["rubrics"]["billing_answer"]["calibrated"])


class TestUnknownAndAggregation(ScorerCase):
    def test_unknown_judge_verdicts_are_excluded_and_flagged(self):
        rows = calibrated_rows()
        rows += [{"case_id": f"u{i}", "rubric_id": "billing_answer",
                  "label": "fail", "judge_label": "unknown"}
                 for i in range(30)]
        rc, out, _ = self.score(rows)
        block = out["rubrics"]["billing_answer"]
        self.assertEqual(block["judge_unknown"], 30)
        self.assertEqual(block["labelled_pairs"], 120)
        self.assertEqual(block["matrix"]["fn"], 2)  # unknown != fail
        self.assertTrue(any("'unknown'" in n for n in block["notes"]))

    def test_one_uncalibrated_rubric_holds_the_whole_harness(self):
        """profile-schema.md's rule, asserted rather than restated."""
        rows = calibrated_rows("billing_answer")
        rows += calibrated_rows("refund_policy", n_fail=12, n_pass=12,
                                misses=0, false_alarms=0)
        rc, out, _ = self.score(rows)
        self.assertTrue(out["rubrics"]["billing_answer"]["calibrated"])
        self.assertFalse(out["rubrics"]["refund_policy"]["calibrated"])
        self.assertEqual(out["status"], "uncalibrated")
        self.assertEqual(out["rubrics_measured"],
                         ["billing_answer", "refund_policy"])
        self.assertTrue(any(u.startswith("refund_policy:")
                            for u in out["unlock"]))


class TestInputContract(ScorerCase):
    def test_a_viewer_only_export_scores_nothing_and_says_why(self):
        """The viewer writes the human side only, and cannot do better: no
        artifact in this harness holds a judge verdict."""
        rows = [{"trace_id": f"t{i}", "case_id": f"c{i}", "label": "fail",
                 "category": "hallucinated-count", "critique": "...",
                 "reviewer": "priya", "ts": "2026-08-02T14:03:00Z"}
                for i in range(20)]
        rc, out, _ = self.score(rows)
        self.assertEqual(rc, 2)
        self.assertIn("judge_label", out["error"])
        self.assertIn("20 human-only annotation(s)", out["error"])

    def test_open_coding_and_calibration_lines_coexist_in_one_file(self):
        rows = calibrated_rows()
        rows += [{"case_id": f"open{i}", "label": "fail", "critique": "..."}
                 for i in range(7)]
        rc, out, _ = self.score(rows)
        self.assertEqual(rc, 0)
        self.assertEqual(out["unpaired_annotations"], 7)
        self.assertEqual(out["labelled_pairs"], 120)

    def test_a_re_review_appends_and_the_latest_line_wins(self):
        rows = calibrated_rows()
        # Same case, re-reviewed: the judge was right after all.
        rows.append({"case_id": "billing_answer-f0", "rubric_id":
                     "billing_answer", "node_id": "correct_amount",
                     "label": "pass", "judge_label": "pass"})
        rc, out, _ = self.score(rows)
        block = out["rubrics"]["billing_answer"]
        self.assertEqual(block["labelled_pairs"], 120)   # not 121
        self.assertEqual(block["matrix"], {"tp": 58, "fn": 1, "fp": 3,
                                           "tn": 58})

    def test_a_later_unpaired_re_annotation_drops_the_earlier_pairing(self):
        rows = calibrated_rows()
        rows.append({"case_id": "billing_answer-f0", "rubric_id":
                     "billing_answer", "node_id": "correct_amount",
                     "label": "fail", "critique": "looked again"})
        rc, out, _ = self.score(rows)
        self.assertEqual(out["labelled_pairs"], 119)
        self.assertEqual(out["unpaired_annotations"], 1)

    def test_the_same_case_under_two_nodes_is_two_pairs(self):
        """The judge runs ONE node per call, so node_id is part of identity."""
        rows = [{"case_id": "c1", "rubric_id": "r", "node_id": "a",
                 "label": "fail", "judge_label": "fail"},
                {"case_id": "c1", "rubric_id": "r", "node_id": "b",
                 "label": "pass", "judge_label": "pass"}]
        rc, out, _ = self.score(rows)
        self.assertEqual(out["labelled_pairs"], 2)

    def test_labels_are_compared_in_nfc(self):
        """Step 3 found NFC missing in six scorers; a seventh would split one
        rubric into two groups over an invisible difference."""
        rows = calibrated_rows("café_rubric")           # NFC
        rows += [{"case_id": "nfd-1", "rubric_id": "café_rubric",
                  "label": "fail", "judge_label": "fail"}]  # NFD, same name
        rc, out, _ = self.score(rows)
        self.assertEqual(out["rubrics_measured"], ["café_rubric"])
        self.assertEqual(out["labelled_pairs"], 121)

    def test_trace_id_is_the_fallback_identity_when_case_id_is_absent(self):
        rows = [{"trace_id": "4bf92f", "label": "fail", "judge_label": "fail"},
                {"trace_id": "4bf92f", "label": "pass", "judge_label": "pass"}]
        rc, out, _ = self.score(rows)
        self.assertEqual(out["labelled_pairs"], 1)

    def test_an_unidentifiable_line_is_an_error(self):
        rc, out, _ = self.score([{"label": "fail", "judge_label": "fail"}])
        self.assertEqual(rc, 2)
        self.assertIn("case_id or a trace_id", out["error"])

    def test_a_non_binary_human_label_is_an_error(self):
        rc, out, _ = self.score(
            [{"case_id": "c1", "label": "unknown", "judge_label": "fail"}])
        self.assertEqual(rc, 2)
        self.assertIn("label='unknown'", out["error"])

    def test_a_missing_label_is_an_error(self):
        rc, out, _ = self.score([{"case_id": "c1", "judge_label": "fail"}])
        self.assertEqual(rc, 2)
        self.assertIn("'label'", out["error"])

    def test_the_error_contract_is_json_on_stdout_exit_2(self):
        rc, out, err = run_scorer(self.tmp / "nope.jsonl")
        self.assertEqual(rc, 2)
        self.assertIn("error", out)
        self.assertEqual(err, "")


class TestSidecar(ScorerCase):
    def test_write_emits_the_schema_the_runner_checks(self):
        path = self.tmp / "calibration.json"
        rc, out, _ = self.score(calibrated_rows(), "--write", path)
        self.assertEqual(rc, 0)
        self.assertEqual(out["written"], str(path))
        sidecar = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(sidecar["schema"], run_cases.JUDGE_CALIBRATION_SCHEMA)
        self.assertEqual(sidecar["status"], "calibrated")
        self.assertEqual(sidecar["rubrics_measured"], ["billing_answer"])
        self.assertEqual(sidecar["harness_version"], run_cases.HARNESS_VERSION)

    def test_an_uncalibrated_pass_still_writes_its_sidecar(self):
        """Exit 0 either way: 'uncalibrated' is a measurement, not a failure --
        and the runner needs the file to say so."""
        path = self.tmp / "calibration.json"
        rc, out, _ = self.score(
            calibrated_rows(n_fail=12, n_pass=12, misses=0, false_alarms=0),
            "--write", path)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(path.read_text())["status"],
                         "uncalibrated")

    def test_the_sidecar_must_be_json_not_yaml(self):
        rc, out, _ = self.score(calibrated_rows(), "--write",
                                self.tmp / "profile.yaml")
        self.assertEqual(rc, 2)
        self.assertIn("stdlib-only", out["error"])


class TestJudgedGate(unittest.TestCase):
    """run_cases.py's half: `judge.status: calibrated` is necessary, not
    sufficient. Before this, the harness's central credibility gate opened on
    a word a model typed into profile.yaml."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def runner(self, sidecar=None, ledger_name="judge/calibration.json",
               rubric="billing_answer-v2", declared=...):
        if sidecar is not None:
            path = self.tmp / ledger_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(sidecar), encoding="utf-8")
        paths = {"scripts_dir": str(SCRIPTS), "state_dir": str(self.tmp),
                 "holdout_ledger": "datasets/holdout-looks.jsonl"}
        if declared is not ...:
            if declared is not None:
                paths["judge_calibration"] = declared
        else:
            paths["judge_calibration"] = ledger_name
        plan = {
            "plan_version": 1, "run_id": "r", "mode": "regression", "k": 1,
            "gate": "soft", "selecting_split": "regression", "paths": paths,
            "cases": [{"id": "c1", "expect": {"answer": {"rubric": rubric}}}],
            "execution": {}, "adapter": {}, "capability_matrix": {},
            "manifest_extra": {"judge": {"model": None,
                                         "status": "calibrated"}},
        }
        return run_cases.Runner(plan, str(self.tmp / "out"))

    def good_sidecar(self, **overrides):
        sidecar = {"schema": run_cases.JUDGE_CALIBRATION_SCHEMA,
                   "status": "calibrated",
                   "rubrics_measured": ["billing_answer"]}
        sidecar.update(overrides)
        return sidecar

    def test_a_hand_set_flag_no_longer_opens_the_gate(self):
        reason = self.runner(declared=None).unjudged_reason()
        self.assertIn("judge calibration not recorded", reason)
        self.assertIn("score_agreement.py", reason)

    def test_a_backed_flag_defers_to_the_skill(self):
        self.assertEqual(self.runner(self.good_sidecar()).unjudged_reason(),
                         "deferred to skill")

    def test_a_missing_sidecar_file_is_a_reason_not_an_exception(self):
        reason = self.runner().unjudged_reason()
        self.assertIn("unreadable", reason)

    def test_a_sidecar_that_says_uncalibrated_keeps_the_gate_shut(self):
        reason = self.runner(
            self.good_sidecar(status="uncalibrated")).unjudged_reason()
        self.assertIn("judge not calibrated per", reason)

    def test_a_foreign_schema_is_refused(self):
        reason = self.runner(
            self.good_sidecar(schema="something/else")).unjudged_reason()
        self.assertIn("schema", reason)

    def test_a_rubric_this_run_uses_but_nobody_measured_is_refused(self):
        """The half score_agreement.py cannot do: it sees the rubrics in its
        own input and cannot know which are ACTIVE."""
        reason = self.runner(self.good_sidecar(),
                             rubric="refund_policy-v1").unjudged_reason()
        self.assertIn("measured no rubric", reason)
        self.assertIn("refund_policy-v1", reason)

    def test_the_version_pin_does_not_break_the_match(self):
        """A case pins `<rubric_id>-v<version>`; an annotation may carry
        either form."""
        for measured in (["billing_answer"], ["billing_answer-v2"]):
            with self.subTest(measured=measured):
                runner = self.runner(
                    self.good_sidecar(rubrics_measured=measured))
                self.assertEqual(runner.unjudged_reason(), "deferred to skill")

    def test_smoke_mode_still_short_circuits_before_any_of_this(self):
        runner = self.runner(self.good_sidecar())
        runner.plan["mode"] = "smoke"
        runner._judge_gate = None
        self.assertEqual(runner.unjudged_reason(), "mode: smoke")

    def test_an_uncalibrated_profile_is_unchanged(self):
        runner = self.runner(self.good_sidecar())
        runner.plan["manifest_extra"]["judge"]["status"] = "uncalibrated"
        self.assertEqual(runner.unjudged_reason(), "judge not calibrated")

    def test_the_gate_is_computed_once_per_run(self):
        runner = self.runner(self.good_sidecar())
        self.assertEqual(runner.unjudged_reason(), "deferred to skill")
        (self.tmp / "judge" / "calibration.json").unlink()
        # A mid-run edit must not make case 40's reason differ from case 1's.
        self.assertEqual(runner.unjudged_reason(), "deferred to skill")


if __name__ == "__main__":
    unittest.main()
