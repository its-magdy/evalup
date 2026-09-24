#!/usr/bin/env python3
"""Tests for scripts/run_history.py, the longitudinal view (REVIEW SS4.9).

Stdlib only (unittest + subprocess), and the script is driven as a real
subprocess because argv + stdout JSON + exit code is its contract -- except the
statistics, which are imported and checked against arithmetic that can be done
by hand.

Three groups carry the weight:
  TestExactNull      -- the drift test is EXACT at every n (stats.py's house
                        standard). These check the null distribution against
                        the Mahonian numbers and against factorials, and they
                        check the reason Cox-Stuart was rejected rather than
                        just asserting the choice.
  TestSeries         -- what counts as ONE series. SS5.6's three keys plus the
                        two a series of N needs, and the one key it must NOT
                        have (the app's git_sha is the independent variable).
  TestHoldoutSeal    -- results.json carries holdout as an aggregate only, so
                        no sealed case_id may appear anywhere in the output.

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
SCRIPT = SCRIPTS / "run_history.py"

sys.path.insert(0, str(SCRIPTS))
import run_history  # noqa: E402  - imported for the exact-null arithmetic


def run_script(*argv):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *[str(a) for a in argv]],
        capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stderr


DEFAULT_MATRIX = {"routing": {"enabled": True},
                  "trajectory": {"enabled": False, "blocked_by": "no trace"}}


class HistoryCase(unittest.TestCase):
    """A temp reports/ directory the tests fill with synthetic run dirs.

    Synthetic rather than runner-produced for everything except
    TestAgainstTheRealRunner, which drives run_cases.py end to end: a hundred
    real runs would take an hour, and one real pair proves the field names.
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.reports = self.tmp / "reports"
        self.reports.mkdir(parents=True)

    def write_run(self, run_id, cases=(), status="ok", started_at=None,
                  routing=None, reliability=None, holdout=None,
                  case_sha=None, summary_extra=None, **manifest_extra):
        """One run directory: manifest.yaml + results.json, plus optionals.

        `cases` is [(case_id, verdict), ...] -- exactly what results.json
        carries, which is the non-holdout set.
        """
        out = self.reports / run_id
        (out / "cases").mkdir(parents=True)
        manifest = {
            "run_id": run_id,
            "mode": "smoke",
            "k": 1,
            "selecting_split": "smoke",
            "started_at": started_at or "2026-09-01T00:00:00Z",
            "harness_version": "0.1.0",
            "dataset_version": 1,
            "capability_matrix": DEFAULT_MATRIX,
            "app": {"name": "app", "git_sha": "aaaa", "git_tree": "clean"},
            "traces": {"disabled_layers": ["trajectory"]},
            "cases": [{"id": cid, "set": "smoke",
                       "sha256": (case_sha or {}).get(cid, "sha-" + cid)}
                      for cid, _ in cases],
        }
        manifest.update(manifest_extra)
        passes = sum(1 for _, v in cases if v == "pass")
        summary = {
            "status": status,
            "n": len(cases), "attempted": len(cases),
            "passes": passes,
            "failures": sum(1 for _, v in cases if v == "fail"),
            "gating_failures": sum(1 for _, v in cases if v == "fail"),
            "unscored": sum(1 for _, v in cases if v == "unscored"),
            "skipped": 0,
            "infra_errors": 0, "infra_rate": 0.0, "crash_rate": 0.0,
            "scorer_errors": 0,
            "unscorable_layers": ["trajectory"],
            "unjudged": "mode: smoke",
            "canaries": {"n": 0, "passed": 0},
            "holdout": holdout,
            "missing_artifacts": [],
        }
        if holdout:
            # THE SEAL, as the runner writes it: summary.n and summary.passes
            # INCLUDE holdout cases; results.json.cases does not.
            summary["n"] += holdout["n"]
            summary["passes"] += holdout["passes"]
        summary.update(summary_extra or {})
        if status is None:
            summary.pop("status")
        results = {
            "run_id": run_id, "harness_version": "0.1.0",
            "dataset_version": 1, "mode": "smoke",
            "cases": [{"case_id": cid, "verdict": v, "gating": True,
                       "layers": {"http": "pass"}, "latency_s": 1.0}
                      for cid, v in cases],
            "summary": summary, "exit_code": 0,
        }
        (out / "manifest.yaml").write_text(json.dumps(manifest),
                                           encoding="utf-8")
        (out / "results.json").write_text(json.dumps(results),
                                          encoding="utf-8")
        if routing is not None:
            (out / "routing_report.json").write_text(json.dumps(routing),
                                                     encoding="utf-8")
        if reliability is not None:
            (out / "reliability.json").write_text(json.dumps(reliability),
                                                  encoding="utf-8")
        return out

    def history(self, *argv, expect=0):
        rc, payload, err = run_script(self.reports, *argv)
        self.assertEqual(rc, expect, f"rc={rc}\n{err}\n{payload}")
        return payload

    def only_series(self, *argv):
        payload = self.history(*argv)
        self.assertEqual(payload["series_count"], 1, payload["series"])
        return payload["series"][0]

    def ramp(self, verdicts_per_run, **kwargs):
        """One run per element; each element is the list of case verdicts."""
        for i, verdicts in enumerate(verdicts_per_run, 1):
            self.write_run(
                f"smoke-2026090{i}T120000Z",
                cases=[(f"c-{j:03d}", v) for j, v in enumerate(verdicts)],
                started_at=f"2026-09-0{i}T12:00:00Z", **kwargs)


class TestExactNull(unittest.TestCase):
    """The drift test is exact at every n. stats.py's standard, and 9a's
    precedent (kappa got Fisher's exact p, never an asymptotic interval)."""

    def test_the_null_for_distinct_values_is_the_mahonian_row(self):
        # Permutations of 5 by inversion count. Textbook, checkable by hand.
        self.assertEqual(run_history.inversion_distribution([1] * 5),
                         [1, 4, 9, 15, 20, 22, 20, 15, 9, 4, 1])

    def test_the_null_sums_to_the_number_of_orderings(self):
        for sizes, total in (([1, 1, 1], 6), ([2, 1], 3), ([2, 2], 6),
                             ([3, 1, 1], 20), ([1] * 6, 720)):
            self.assertEqual(sum(run_history.inversion_distribution(sizes)),
                             total, sizes)

    def test_ties_shrink_the_null_instead_of_being_corrected_for(self):
        # aab/aba/baa -- three orderings, one each at 0, 1 and 2 inversions.
        # A tie-corrected VARIANCE would be the approximate route; this is the
        # exact conditional one, the same move score_agreement.py makes when it
        # conditions Fisher's test on both margins.
        self.assertEqual(run_history.inversion_distribution([2, 1]), [1, 1, 1])

    def test_the_division_is_asserted_exact_not_rounded(self):
        with self.assertRaises(ArithmeticError):
            run_history.poly_divide_exact([1, 0, 1], [1, 1])

    def drift(self, values, alpha=0.05, **kw):
        return run_history.mann_kendall_exact(values, alpha, **kw)

    def test_a_perfect_increase_gets_the_exact_two_over_n_factorial(self):
        block = self.drift([0.1, 0.2, 0.3, 0.4, 0.5])
        self.assertEqual(block["direction"], "increasing")
        self.assertEqual(block["discordant_pairs"], 0)
        self.assertEqual(block["s"], 10)
        self.assertAlmostEqual(block["p_increasing_one_sided"], 1 / 120, 6)
        self.assertAlmostEqual(block["p_two_sided"], 2 / 120, 6)
        self.assertTrue(block["flagged"])

    def test_the_test_is_symmetric_under_reversing_time(self):
        up = self.drift([0.1, 0.3, 0.35, 0.4, 0.9])
        down = self.drift([0.9, 0.4, 0.35, 0.3, 0.1])
        self.assertEqual(up["p_two_sided"], down["p_two_sided"])
        self.assertEqual(up["direction"], "increasing")
        self.assertEqual(down["direction"], "decreasing")
        self.assertEqual(up["s"], -down["s"])

    def test_the_erosion_review_4_9_describes_is_what_this_catches(self):
        # "five consecutive -2% within-noise diffs hide a 10-point erosion":
        # every pairwise diff is tiny, the sequence is monotone, and no
        # pairwise gate would ever have said anything.
        block = self.drift([0.90, 0.88, 0.86, 0.84, 0.82, 0.80])
        self.assertEqual(block["direction"], "decreasing")
        self.assertTrue(block["flagged"])
        self.assertAlmostEqual(block["p_two_sided"], 2 / 720, 6)

    def test_cox_stuart_could_not_have_flagged_that_series_at_all(self):
        # The reason for the choice, as a check rather than a claim. At n=6
        # Cox-Stuart sign-tests floor(n/2)=3 paired comparisons, so its
        # smallest attainable two-sided p is 2*(1/2)**3 = 0.25 -- above any
        # conventional alpha, whatever the data does. Mann-Kendall uses all 15
        # pairs and reaches 2/720.
        cox_stuart_floor = 2 * 0.5 ** 3
        self.assertGreater(cox_stuart_floor, 0.05)
        block = self.drift([0.90, 0.88, 0.86, 0.84, 0.82, 0.80])
        self.assertLess(block["p_floor"], 0.05)
        self.assertLess(block["p_floor"], cox_stuart_floor)

    def test_a_flat_series_is_not_tested_rather_than_scored_p_one(self):
        block = self.drift([0.5, 0.5, 0.5, 0.5])
        self.assertFalse(block["tested"])
        self.assertIn("same value", block["reason"])

    def test_two_runs_report_that_rejection_was_impossible(self):
        block = self.drift([0.9, 0.1])
        self.assertTrue(block["tested"])
        self.assertEqual(block["p_two_sided"], 1.0)
        self.assertEqual(block["p_floor"], 1.0)
        self.assertTrue(block["underpowered"])
        self.assertFalse(block["flagged"])

    def test_underpowered_marks_the_n_where_no_flag_is_reachable(self):
        # n=4 distinct: floor is 2/24 = 0.083 > 0.05, so a perfectly monotone
        # four-run slide CANNOT flag. Saying so is the difference between "no
        # drift" and "this dataset cannot show drift".
        block = self.drift([0.4, 0.3, 0.2, 0.1])
        self.assertTrue(block["underpowered"])
        self.assertFalse(block["flagged"])
        self.assertAlmostEqual(block["p_floor"], 2 / 24, 6)
        # And n=5 is where it becomes reachable.
        self.assertFalse(self.drift([0.5, 0.4, 0.3, 0.2, 0.1])["underpowered"])

    def test_one_run_is_not_a_trend(self):
        self.assertFalse(self.drift([0.5])["tested"])

    def test_the_cap_refuses_rather_than_approximating(self):
        block = self.drift([i / 100 for i in range(10)], max_runs=5)
        self.assertFalse(block["tested"])
        self.assertIn("substitutes no approximation", block["reason"])

    def test_no_normal_approximation_appears_in_the_source(self):
        # The house rule, mechanically. NormalDist is how an asymptotic
        # Mann-Kendall would be written, and it is what this file refuses.
        source = (SCRIPTS / "run_history.py").read_text(encoding="utf-8")
        for banned in ("NormalDist", "inv_cdf", "sqrt"):
            self.assertNotIn(banned, source.split('"""', 2)[2],
                             f"{banned} outside the docstring")


class TestSeries(HistoryCase):
    """What counts as ONE series -- SS5.6's rules extended from a pair to N."""

    def two_runs_differing_by(self, **second):
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")],
                       started_at="2026-09-01T12:00:00Z")
        self.write_run("smoke-20260902T120000Z", cases=[("c-1", "pass")],
                       started_at="2026-09-02T12:00:00Z", **second)
        return self.history()

    def test_two_runs_of_the_same_shape_are_one_series(self):
        payload = self.two_runs_differing_by()
        self.assertEqual(payload["series_count"], 1)
        self.assertEqual(payload["series"][0]["n_runs"], 2)

    def test_a_different_k_is_a_different_series(self):
        # SS5.6/D7 verbatim: pass^k over different k are different quantities.
        self.assertEqual(self.two_runs_differing_by(k=3)["series_count"], 2)

    def test_a_different_dataset_or_harness_version_splits_the_series(self):
        for key, value in (("dataset_version", 2), ("harness_version", "0.2")):
            self.setUp()
            self.assertEqual(
                self.two_runs_differing_by(**{key: value})["series_count"], 2,
                key)

    def test_a_different_selecting_split_splits_the_series(self):
        # The key SS5.6's PAIR rule does not have. stats.py survives a split
        # mismatch by intersecting case ids and warning about attrition; a mean
        # has nothing to intersect, so a 4-case smoke rate and a 200-case full
        # rate would land on one line.
        payload = self.two_runs_differing_by(selecting_split="full",
                                             mode="full")
        self.assertEqual(payload["series_count"], 2)

    def test_disabling_a_layer_splits_the_series(self):
        # Since Step 10 a case whose real layers all come back unscored rolls
        # up `unscored` instead of riding `http` to a pass, so the same app
        # scores differently with trajectory off.
        payload = self.two_runs_differing_by(capability_matrix={
            "routing": {"enabled": False},
            "trajectory": {"enabled": False, "blocked_by": "no trace"}})
        self.assertEqual(payload["series_count"], 2)

    def test_rewording_blocked_by_does_not_split_the_series(self):
        payload = self.two_runs_differing_by(capability_matrix={
            "routing": {"enabled": True},
            "trajectory": {"enabled": False, "blocked_by": "different prose"}})
        self.assertEqual(payload["series_count"], 1)

    def test_the_app_sha_is_the_independent_variable_and_never_a_key(self):
        payload = self.two_runs_differing_by(
            app={"name": "app", "git_sha": "bbbb", "git_tree": "clean"})
        self.assertEqual(payload["series_count"], 1,
                         "keying on the app sha would give every series one "
                         "point and no trend could ever be shown")
        shas = [r["app_git_sha"] for r in payload["series"][0]["runs"]]
        self.assertEqual(shas, ["aaaa", "bbbb"])

    def test_an_unchanged_app_sha_is_said_out_loud(self):
        payload = self.two_runs_differing_by()
        self.assertTrue(any("same app git_sha" in w
                            for w in payload["series"][0]["warnings"]))

    def test_a_dirty_tree_is_flagged_because_the_sha_does_not_identify_it(self):
        payload = self.two_runs_differing_by(
            app={"name": "app", "git_sha": "aaaa", "git_tree": "dirty"})
        self.assertTrue(any("DIRTY" in w
                            for w in payload["series"][0]["warnings"]))

    def test_varying_run_time_degradation_warns_but_does_not_split(self):
        # An OUTCOME, not a declared capability. Splitting on it would hide
        # exactly the degradation the reader needs to see.
        payload = self.two_runs_differing_by(
            traces={"disabled_layers": ["trajectory", "tool_selection"]})
        self.assertEqual(payload["series_count"], 1)
        self.assertTrue(any("disabled at run time varies" in w
                            for w in payload["series"][0]["warnings"]))

    def test_runs_are_ordered_by_started_at_not_by_directory_listing(self):
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")],
                       started_at="2026-09-09T12:00:00Z")
        self.write_run("smoke-20260902T120000Z", cases=[("c-1", "fail")],
                       started_at="2026-09-02T12:00:00Z")
        series = self.only_series()
        self.assertEqual([r["run_id"] for r in series["runs"]],
                         ["smoke-20260902T120000Z", "smoke-20260901T120000Z"])

    def test_the_run_id_stamp_is_the_fallback_ordering(self):
        self.assertEqual(run_history.run_id_time("smoke-20260818T183920Z"),
                         "20260818T183920Z")
        self.assertEqual(run_history.run_id_time("nonsense"), "")
        # Lexicographic run-id order is wrong ACROSS modes, which is why the
        # stamp is pulled out rather than the id sorted directly.
        self.assertLess(run_history.run_id_time("smoke-20251201T000000Z"),
                        run_history.run_id_time("full-20260101T000000Z"))


class TestRunSelection(HistoryCase):
    """Only summary.status == "ok" counts, and every exclusion is named."""

    def test_an_incomplete_run_is_excluded_with_its_reason(self):
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")],
                       status="incomplete")
        payload = self.history()
        self.assertEqual(payload["runs_used"], 0)
        self.assertEqual(payload["runs_found"], 1)
        reason = payload["excluded_runs"][0]["reason"]
        self.assertIn("not quotable", reason)

    def test_a_pre_runner_run_directory_has_no_status_and_is_excluded(self):
        # The shipped field-test run is exactly this shape.
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")],
                       status=None)
        payload = self.history()
        self.assertEqual(payload["runs_used"], 0)
        self.assertIn("before run_cases.py existed",
                      payload["excluded_runs"][0]["reason"])

    def test_all_runs_excluded_is_a_report_not_an_error(self):
        # build_review_viewer.py's distinction: a path that matches nothing is
        # an error; a real thing that is empty is a fact to render.
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")],
                       status="running")
        payload = self.history()
        self.assertIn("note", payload)
        self.assertEqual(payload["series"], [])

    def test_one_corrupt_run_does_not_sink_the_others(self):
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")],
                       started_at="2026-09-01T12:00:00Z")
        bad = self.write_run("smoke-20260902T120000Z", cases=[("c-1", "pass")],
                             started_at="2026-09-02T12:00:00Z")
        (bad / "results.json").write_text("{not json", encoding="utf-8")
        payload = self.history()
        self.assertEqual(payload["runs_used"], 1)
        self.assertEqual(len(payload["excluded_runs"]), 1)

    def test_a_directory_with_no_manifest_is_excluded_not_crashed_on(self):
        (self.reports / "candidates").mkdir()
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")])
        payload = self.history()
        self.assertEqual(payload["runs_used"], 1)
        self.assertEqual(payload["excluded_runs"][0]["run"], "candidates")

    def test_baseline_json_and_gitignore_are_not_mistaken_for_runs(self):
        (self.reports / "baseline.json").write_text('{"run_id": "x"}',
                                                    encoding="utf-8")
        (self.reports / ".gitignore").write_text("*/cases/", encoding="utf-8")
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")])
        payload = self.history()
        self.assertEqual(payload["runs_found"], 1)

    def test_naming_a_reports_dir_and_a_run_inside_it_does_not_double_count(
            self):
        run = self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")])
        rc, payload, err = run_script(self.reports, run)
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["runs_found"], 1)

    def test_a_reports_dir_with_no_run_directories_is_an_error(self):
        rc, payload, _ = run_script(self.reports)
        self.assertEqual(rc, 2)
        self.assertIn("no run directory", payload["error"])


class TestMetrics(HistoryCase):
    def test_pass_rate_is_read_off_the_summary(self):
        self.ramp([["pass", "pass", "fail", "fail"],
                   ["pass", "fail", "fail", "fail"]])
        series = self.only_series()
        self.assertEqual(series["metrics"]["pass_rate"]["values"], [0.5, 0.25])
        self.assertEqual(series["metrics"]["pass_rate"]["change"], -0.25)
        self.assertEqual(series["metrics"]["gating_failure_rate"]["values"],
                         [0.5, 0.75])

    def test_a_metric_missing_from_one_run_is_not_plotted_ragged(self):
        # routing_report.json is SS6 `# only when: any case is
        # routing-scorable`. A line with holes would silently run the drift
        # test over a different subset of runs than the reader is looking at.
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")],
                       started_at="2026-09-01T12:00:00Z",
                       routing={"accuracy": 0.9, "macro_f1": 0.8})
        self.write_run("smoke-20260902T120000Z", cases=[("c-1", "pass")],
                       started_at="2026-09-02T12:00:00Z")
        series = self.only_series()
        self.assertNotIn("routing_accuracy", series["metrics"])
        self.assertIn("only when", series["metrics_not_one_series"]
                      ["routing_accuracy"])

    def test_routing_and_reliability_are_plotted_when_every_run_has_them(self):
        for i, (acc, gap) in enumerate([(0.9, 0.1), (0.8, 0.2)], 1):
            self.write_run(f"smoke-2026090{i}T120000Z",
                           cases=[("c-1", "pass")],
                           started_at=f"2026-09-0{i}T12:00:00Z", k=3,
                           routing={"accuracy": acc, "macro_f1": acc},
                           reliability={"pass_hat_k": acc,
                                        "flakiness_gap": gap})
        series = self.only_series()
        self.assertEqual(series["metrics"]["routing_accuracy"]["values"],
                         [0.9, 0.8])
        self.assertEqual(series["metrics"]["flakiness_gap"]["values"],
                         [0.1, 0.2])

    def test_there_is_no_flakiness_ledger_only_reliability_json(self):
        # REVIEW SS4.9's premise names an artifact that does not exist. The
        # signal is reliability.json, written only when k > 1 -- and k is a
        # series key, so a series has it on every point or on none.
        hits = subprocess.run(
            ["grep", "-rIl", "flakiness_ledger",
             str(SCRIPTS), str(SCRIPTS.parent / "docs"),
             str(SCRIPTS.parent / "skills")],
            capture_output=True, text=True)
        self.assertEqual(hits.stdout.strip(), "")

    def test_cost_and_latency_are_left_to_the_script_that_owns_them(self):
        # latency_s IS on disk per case in results.json and is deliberately
        # not aggregated here; cost needs a price table, which is DATED, so it
        # is not a series metric even in principle. Step 9c gave both an owner
        # (score_cost.py) and this string now names it rather than pointing at
        # a step nobody can look up.
        self.ramp([["pass"], ["fail"]])
        payload = self.history()
        names = set(payload["series"][0]["metrics"]) | set(
            payload["series"][0]["metrics_not_one_series"])
        self.assertEqual([n for n in names
                          if "latency" in n or "cost" in n], [])
        # Declared, not silently omitted: the reader is told where the line is.
        self.assertIn("latency_s", payload["not_measured"])
        self.assertIn("cost_usd", payload["not_measured"])
        self.assertIn("score_cost.py", payload["not_measured"])

    def test_it_states_no_second_definition_of_better(self):
        # stats.py owns the keep rule. A drift flag describes a sequence.
        self.ramp([["pass"], ["fail"], ["fail"]])
        blob = json.dumps(self.history()).lower()
        for banned in ('"keep"', "improved", "regressed", "worsened"):
            self.assertNotIn(banned, blob)

    def test_a_boolean_is_never_plotted_as_a_number(self):
        self.assertIsNone(run_history.number(True))
        self.assertEqual(run_history.number(0), 0)

    def test_a_flagged_drift_is_listed_at_series_level(self):
        self.ramp([["pass"] * 9 + ["fail"] * 1,
                   ["pass"] * 8 + ["fail"] * 2,
                   ["pass"] * 7 + ["fail"] * 3,
                   ["pass"] * 6 + ["fail"] * 4,
                   ["pass"] * 5 + ["fail"] * 5])
        series = self.only_series()
        self.assertIn("pass_rate", series["drift_flagged"])
        self.assertEqual(
            series["metrics"]["pass_rate"]["drift"]["direction"], "decreasing")
        self.assertGreater(series["metrics_tested"], 1)


class TestHoldoutSeal(HistoryCase):
    """results.json carries holdout as an AGGREGATE ONLY, so a history view
    built on it can name no sealed case. Trap 4."""

    def test_a_holdout_run_contributes_counts_and_no_ids(self):
        self.write_run("holdout-20260901T120000Z",
                       cases=[("visible-1", "pass"), ("visible-2", "fail")],
                       holdout={"n": 4, "passes": 3, "gating_failures": 1,
                                "looks_recorded": 2})
        payload = self.history()
        series = payload["series"][0]
        self.assertEqual(series["metrics"]["holdout_pass_rate"]["values"],
                         [0.75])
        # summary.n includes holdout; results.json.cases does not, so the
        # journal's denominator is reported beside the headline rate.
        self.assertEqual(series["metrics"]["pass_rate"]["values"],
                         [round(4 / 6, 6)])
        self.assertEqual(series["metrics"]["visible_pass_rate"]["values"],
                         [0.5])
        self.assertEqual(
            [r["case_id"] for r in series["case_journal"]["cases"]], [])
        self.assertEqual(series["case_journal"]["cases_ever_seen"], 2)

    def test_the_ledger_and_the_holdout_verdicts_file_are_never_read(self):
        source = (SCRIPTS / "run_history.py").read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]
        # verdicts.jsonl is the run's durable per-case record and it DOES
        # carry holdout ids; results.json is the shareable one and does not.
        self.assertNotIn("verdicts.jsonl", body)
        self.assertNotIn("holdout-looks", body)


class TestCaseJournal(HistoryCase):
    def test_a_case_that_breaks_mid_series_gets_one_transition(self):
        for i, verdict in enumerate(["pass", "pass", "pass", "fail"], 1):
            self.write_run(f"smoke-2026090{i}T120000Z",
                           cases=[("c-1", verdict), ("c-2", "pass")],
                           started_at=f"2026-09-0{i}T12:00:00Z",
                           app={"name": "app", "git_sha": f"sha{i}",
                                "git_tree": "clean"})
        journal = self.only_series()["case_journal"]
        rows = {r["case_id"]: r for r in journal["cases"]}
        self.assertEqual(list(rows), ["c-1"], "a stable case is not news")
        self.assertEqual(rows["c-1"]["verdicts"],
                         ["pass", "pass", "pass", "fail"])
        self.assertEqual(rows["c-1"]["transitions"],
                         [{"from": "pass", "to": "fail",
                           "at": "smoke-20260904T120000Z",
                           "app_git_sha": "sha4"}])
        self.assertEqual(journal["cases_with_a_transition"], 1)

    def test_all_cases_includes_the_stable_ones(self):
        self.ramp([["pass", "pass"], ["pass", "fail"]])
        journal = self.only_series("--all-cases")["case_journal"]
        self.assertEqual(len(journal["cases"]), 2)

    def test_an_absent_case_is_absent_and_never_imputed_as_a_failure(self):
        self.write_run("smoke-20260901T120000Z",
                       cases=[("c-1", "pass"), ("c-2", "pass")],
                       started_at="2026-09-01T12:00:00Z")
        self.write_run("smoke-20260902T120000Z", cases=[("c-1", "pass")],
                       started_at="2026-09-02T12:00:00Z")
        series = self.only_series()
        row = series["case_journal"]["cases"][0]
        self.assertEqual(row["case_id"], "c-2")
        self.assertEqual(row["verdicts"], ["pass"])
        self.assertEqual(row["absent_in"], ["smoke-20260902T120000Z"])
        self.assertEqual(row["transitions"], [])
        self.assertEqual(series["metrics"]["pass_rate"]["values"], [1.0, 1.0])

    def test_churn_past_the_ten_percent_bar_restates_the_attrition_warning(
            self):
        self.write_run("smoke-20260901T120000Z",
                       cases=[(f"c-{i}", "pass") for i in range(10)],
                       started_at="2026-09-01T12:00:00Z")
        self.write_run("smoke-20260902T120000Z",
                       cases=[(f"c-{i}", "pass") for i in range(8)],
                       started_at="2026-09-02T12:00:00Z")
        journal = self.only_series()["case_journal"]
        self.assertIn("SELECTION BIAS RISK",
                      journal["case_attrition_warning"])
        self.assertEqual(journal["cases_in_every_run"], 8)

    def test_an_edited_case_under_the_same_id_is_caught_by_its_sha256(self):
        # The signal the PAIRWISE diff never had: SS5.6 checks
        # dataset_version, so a case edited without bumping it compares
        # cleanly while the two verdicts answer different questions.
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")],
                       started_at="2026-09-01T12:00:00Z",
                       case_sha={"c-1": "aaa"})
        self.write_run("smoke-20260902T120000Z", cases=[("c-1", "fail")],
                       started_at="2026-09-02T12:00:00Z",
                       case_sha={"c-1": "bbb"})
        journal = self.only_series()["case_journal"]
        self.assertEqual(journal["cases"][0]["content_changed_at"],
                         ["smoke-20260902T120000Z"])
        self.assertIn("Bump dataset_version",
                      journal["content_changed_warning"])

    def test_an_unchanged_case_raises_no_content_warning(self):
        self.ramp([["pass"], ["fail"]])
        self.assertNotIn("content_changed_warning",
                         self.only_series()["case_journal"])

    def test_the_journal_truncates_loudly(self):
        self.write_run("smoke-20260901T120000Z",
                       cases=[(f"c-{i:03d}", "pass") for i in range(10)],
                       started_at="2026-09-01T12:00:00Z")
        self.write_run("smoke-20260902T120000Z",
                       cases=[(f"c-{i:03d}", "fail") for i in range(10)],
                       started_at="2026-09-02T12:00:00Z")
        journal = self.only_series("--max-cases", 3)["case_journal"]
        self.assertEqual(len(journal["cases"]), 3)
        self.assertIn("3 of 10", journal["truncated"])


class TestOutputAndErrors(HistoryCase):
    def test_it_writes_nothing_into_the_run_directories_it_reads(self):
        # SS6: the runner's tree is the complete list of files under --out, and
        # a test machine-diffs its `# only when:` markers against REQUIRED_IF.
        # History is a property of reports/, not of any one run.
        run = self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")])
        before = sorted(p.name for p in run.iterdir())
        self.history()
        self.assertEqual(sorted(p.name for p in run.iterdir()), before)

    def test_dash_o_writes_the_same_json_it_would_have_printed(self):
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")])
        target = self.tmp / "reports" / "history.json"
        rc, payload, err = run_script(self.reports, "-o", target)
        self.assertEqual(rc, 0, err)
        self.assertIsNone(payload, "stdout must be empty when -o is given")
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))
                         ["runs_used"], 1)

    def test_the_error_contract_is_json_on_stdout_exit_2(self):
        rc, payload, err = run_script(self.tmp / "nope")
        self.assertEqual(rc, 2)
        self.assertIn("error", payload)
        self.assertNotIn("Traceback", err)

    def test_a_run_directory_of_garbage_is_a_clean_error(self):
        bad = self.reports / "smoke-20260901T120000Z"
        bad.mkdir()
        (bad / "manifest.yaml").write_text("{not json", encoding="utf-8")
        rc, payload, err = run_script(self.reports)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(payload["runs_used"], 0)

    def test_flags_are_bounds_checked_through_the_shared_contract(self):
        self.write_run("smoke-20260901T120000Z", cases=[("c-1", "pass")])
        for argv in (("--alpha", "0"), ("--alpha", "1.5"),
                     ("--max-cases", "0"), ("--max-exact-runs", "1")):
            rc, payload, err = run_script(self.reports, *argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("error", payload or {}, argv)
            self.assertNotIn("Traceback", err, argv)

    def test_it_answers_help_and_version_like_every_other_script(self):
        for flag in ("--help", "--version"):
            proc = subprocess.run([sys.executable, str(SCRIPT), flag],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, flag)
        proc = subprocess.run([sys.executable, str(SCRIPT), "--version"],
                              capture_output=True, text=True)
        self.assertIn(run_history.HARNESS_VERSION, proc.stdout + proc.stderr)


class TestAgainstTheRealRunner(unittest.TestCase):
    """The field-by-field claim, checked against run_cases.py rather than
    against this file's own fixtures.

    "All the data is already on disk" has been the wrong sentence once before
    (Step 9a's recon). The synthetic fixtures above would keep passing if the
    runner renamed `summary.status` tomorrow, so one test drives the real
    runner twice and reads the result.
    """

    def test_two_real_runs_form_one_series_with_a_pass_rate(self):
        from test_run_cases import FakeApp, make_plan, ok_responder

        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        state = tmp / ".evalup"
        reports = state / "reports"
        reports.mkdir(parents=True)
        app = FakeApp(ok_responder)
        self.addCleanup(app.close)

        for run_id in ("smoke-20260901T120000Z", "smoke-20260902T120000Z"):
            plan = make_plan(state, app.base_url, run_id=run_id)
            plan_path = tmp / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "run_cases.py"),
                 "--plan", str(plan_path), "--out", str(reports / run_id)],
                capture_output=True, text=True,
                env=dict(**{k: v for k, v in __import__("os").environ.items()},
                         EVAL_USER_ID="1"))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        rc, payload, err = run_script(reports)
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["runs_used"], 2, payload)
        self.assertEqual(payload["series_count"], 1, payload["series"])
        series = payload["series"][0]
        self.assertEqual(series["metrics"]["pass_rate"]["values"], [1.0, 1.0])
        # Every comparability key came off a real manifest, not a fixture.
        self.assertEqual(series["key"]["k"], 1)
        self.assertEqual(series["key"]["selecting_split"], "smoke")
        self.assertEqual(series["key"]["dataset_version"], 1)
        self.assertEqual(series["key"]["capabilities"],
                         "answer_quality+routing")
        self.assertEqual(series["case_journal"]["cases_ever_seen"], 1)


if __name__ == "__main__":
    unittest.main()
