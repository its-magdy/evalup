#!/usr/bin/env python3
"""Tests for scripts/score_cost.py, cost + latency (REVIEW SS4.3).

Stdlib only (unittest + subprocess). The script is driven as a real subprocess
because argv + stdout JSON + exit code is its contract -- except the three
statistics, which are imported and checked against arithmetic that can be done
by hand or against a brute-force enumeration.

Five groups carry the weight:
  TestExactStatistics -- SS4.3 asks for a BOOTSTRAP and this file refuses one.
                         These check the sign test against binomial tails, the
                         median interval against its own coverage claim, and
                         the permutation DP against literal enumeration of all
                         2^m sign vectors. Nothing here may resample.
  TestPriceTable      -- the table is DECLARED, dated, exact-integer, and
                         matched verbatim. An unknown model withholds the
                         DOLLARS and keeps the TOKENS.
  TestWhichRunsPrice  -- a trace-less run has no cost input at all and a full
                         latency block. This is the field test's shape.
  TestHoldoutSeal     -- ids come from results.json.cases only; cases/ is
                         indexed, never listed.
  TestNotADecision    -- there is exactly ONE keep rule in this package and it
                         is stats.py's. This output may not contain a second.

Run: python3 -m unittest discover -s tests -v   (from the plugin root)
"""
import itertools
import json
import math
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
SCRIPT = SCRIPTS / "score_cost.py"

sys.path.insert(0, str(SCRIPTS))
import score_cost  # noqa: E402  - imported for the exact statistics


def run_script(*argv):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *[str(a) for a in argv]],
        capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stderr


PRICES = {
    "schema": "agent-eval/price-table/1",
    "as_of": "2026-09-01",
    "currency": "USD",
    "source": "vendor pricing page, read 2026-09-01",
    "models": {"m-1": {"input_per_mtok": "2.50", "output_per_mtok": "10.00"}},
}


class CostCase(unittest.TestCase):
    """A temp reports/ directory the tests fill with synthetic run dirs."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.reports = self.tmp / "reports"
        self.reports.mkdir(parents=True)

    def write_json(self, path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
        return path

    def prices(self, **overrides):
        doc = json.loads(json.dumps(PRICES))
        doc.update(overrides)
        return self.write_json(self.tmp / "prices.json", doc)

    def write_run(self, run_id, cases, status="ok", holdout=None,
                  trace=True, model="m-1", **manifest_extra):
        """One run directory.

        `cases` is [(case_id, verdict, latency_s, input_tokens,
        output_tokens), ...] -- exactly the visible (non-holdout) set, which is
        what results.json.cases carries.
        """
        out = self.reports / run_id
        (out / "cases").mkdir(parents=True)
        manifest = {"run_id": run_id, "mode": "smoke", "k": 1,
                    "selecting_split": "smoke", "harness_version": "0.1.0",
                    "dataset_version": 1,
                    "app": {"name": "app", "git_sha": "aaaa"}}
        manifest.update(manifest_extra)
        self.write_json(out / "manifest.yaml", manifest)
        rows = []
        for case_id, verdict, latency, n_in, n_out in cases:
            rows.append({"case_id": case_id, "verdict": verdict,
                         "gating": True, "layers": {"http": "pass"},
                         "latency_s": latency})
            if trace:
                self.write_json(
                    out / "cases" / case_id / "trajectory.json",
                    {"status": "ok",
                     "trajectory": {"trace_id": "t", "agents": [],
                                    "tool_calls": [],
                                    "llm_calls": [{
                                        "model": model, "duration_ms": 10,
                                        "usage": {"input_tokens": n_in,
                                                  "output_tokens": n_out}}],
                                    "usage": {"input_tokens": n_in,
                                              "output_tokens": n_out}},
                     "checks": {}})
        self.write_json(out / "results.json", {
            "run_id": run_id, "harness_version": "0.1.0",
            "dataset_version": 1, "mode": "smoke", "cases": rows,
            "summary": {"status": status, "n": len(rows),
                        "passes": sum(1 for r in rows
                                      if r["verdict"] == "pass"),
                        "holdout": holdout},
            "exit_code": 0})
        return out

    def simple(self, run_id, n=8, base_in=1000, base_out=100, latency=1.0,
               **kw):
        cases = [(f"c-{i:04d}", "pass", round(latency + i * 0.1, 3),
                  base_in + i, base_out + i) for i in range(n)]
        return self.write_run(run_id, cases, **kw)


# -- the statistics --------------------------------------------------------

class TestExactStatistics(unittest.TestCase):
    """SS4.3 names a BOOTSTRAP; stats.py's standing rule forbids an
    approximation inside a decision or a reported test. These are the tests
    that make the substitution honest rather than merely asserted."""

    def test_the_permutation_null_matches_literal_enumeration(self):
        """The DP is a shortcut for enumerating 2^m sign vectors. At m small
        enough to enumerate, the two must agree EXACTLY -- this is the test
        that would catch a DP that quietly became an approximation."""
        for diffs in ([3, -1, 4, 1, -5], [2, 2, 2], [7, -7, 1, 1],
                      [10, -3, -3, -3, 1, 9]):
            observed = sum(diffs)
            values = [abs(d) for d in diffs if d]
            brute = sum(
                1 for signs in itertools.product((1, -1), repeat=len(values))
                if abs(sum(s * a for s, a in zip(signs, values)))
                >= abs(observed))
            expected = brute / 2 ** len(values)
            got = score_cost.permutation_test_exact(diffs)["p_two_sided"]
            self.assertAlmostEqual(got, expected, places=12, msg=diffs)

    def test_the_permutation_test_weights_magnitude_and_the_sign_test_does_not(self):
        """The reason BOTH are reported. Here the candidate is CHEAPER on
        three cases and dearer on five, which the sign test can barely
        distinguish from a coin -- but the five it loses it loses by ten
        times as much, and SS4.3's "3x more expensive" is a magnitude claim."""
        diffs = [-1, -1, -1, 10, 10, 10, 10, 10]
        sign = score_cost.sign_test_two_sided(diffs)
        perm = score_cost.permutation_test_exact(diffs)
        self.assertEqual((sign["candidate_higher"], sign["candidate_lower"]),
                         (5, 3))
        self.assertGreater(sign["p_two_sided"], 0.7)
        self.assertLess(perm["p_two_sided"], 0.07)

    def test_one_dominating_case_makes_the_permutation_test_powerless(self):
        """The other side of weighting magnitude, and a real limit rather than
        a bug: when a single |difference| exceeds the sum of all the others,
        EVERY sign vector puts |S| at or above the observed total, so the exact
        p is 1.0 no matter how lopsided the data looks. The sign test is
        unaffected, which is the second reason both are reported."""
        diffs = [-1] * 7 + [300]
        self.assertEqual(sum(diffs), 293)
        self.assertEqual(
            score_cost.permutation_test_exact(diffs)["p_two_sided"], 1.0)
        self.assertLess(
            score_cost.sign_test_two_sided(diffs)["p_two_sided"], 0.08)

    def test_the_sign_test_is_the_same_tail_stats_py_computes(self):
        """Imported from stats.py rather than re-derived, so the package
        cannot end up with two exact binomials that disagree."""
        import stats
        diffs = [1] * 9 + [-1] * 3
        got = score_cost.sign_test_two_sided(diffs)["p_two_sided"]
        self.assertAlmostEqual(got, stats.mcnemar_exact_p_two_sided(9, 3), 12)
        self.assertAlmostEqual(
            got, 2 * sum(math.comb(12, i) for i in range(4)) / 2 ** 12, 12)

    def test_zero_differences_are_dropped_from_the_sign_test_and_counted(self):
        """The sign test's standard conditioning. Counting a pair that did not
        move as half a vote each way is an approximation, which is the thing
        this file does not do."""
        block = score_cost.sign_test_two_sided([0, 0, 5, -5, 5])
        self.assertEqual((block["n_zero"], block["n_nonzero"]), (2, 3))

    def test_the_median_interval_delivers_the_coverage_it_claims(self):
        """The interval is the sign test INVERTED, so its stated coverage must
        equal the exact binomial probability that the true median falls
        inside. Checked against the binomial directly, not against itself."""
        for m in (6, 7, 12, 24, 41):
            block = score_cost.median_diff_ci(list(range(1, m + 1)), 0.05)
            lo = block["interval"][0]
            k = lo - 1                      # values are 1..m, so lo == d_(k+1)
            exact = 1 - 2 * sum(math.comb(m, i)
                                for i in range(k)) / 2 ** m
            self.assertAlmostEqual(block["coverage"], exact, places=12)
            self.assertGreaterEqual(block["coverage"], 0.95, m)

    def test_below_six_pairs_no_interval_attains_the_coverage(self):
        """A statement about n, not about the data: the WIDEST possible
        interval at m=5 covers with probability 1 - 2^-4 = 0.9375. Reporting a
        narrower one under a 95% label is the failure this guards."""
        for m in (1, 2, 3, 4, 5):
            block = score_cost.median_diff_ci(list(range(m)), 0.05)
            self.assertIsNone(block["interval"], m)
            self.assertTrue(block["coverage_unattainable"], m)
            self.assertIn("statement about n", block["reason"])
        self.assertIsNotNone(
            score_cost.median_diff_ci([1, 2, 3, 4, 5, 6], 0.05)["interval"])

    def test_the_exact_test_is_declined_past_the_cap_not_approximated(self):
        """run_history.py's rule. When the exact null is too big, the answer is
        'not tested', never a normal approximation and never a bootstrap."""
        block = score_cost.permutation_test_exact(
            [10 ** 6 + i for i in range(40)], max_states=500)
        self.assertFalse(block["tested"])
        self.assertIn("declines the test", block["reason"])
        self.assertNotIn("p_two_sided", block)

    def test_the_cap_binds_on_spread_not_on_m(self):
        """The state space is min(2^m, span/gcd + 1), so 200 identical
        differences are cheap to test exactly and 30 wildly different ones are
        not. A cap on m alone would refuse the first and accept the second."""
        wide = score_cost.permutation_test_exact([2 ** i for i in range(20)],
                                                 max_states=5000)
        narrow = score_cost.permutation_test_exact([400] * 200,
                                                   max_states=5000)
        self.assertFalse(wide["tested"])
        self.assertTrue(narrow["tested"])
        self.assertEqual(narrow["null_states"], 201)

    def test_no_sampling_and_no_normal_approximation_anywhere(self):
        """Read as an AST, not as a substring scan: the docstring DECLINES a
        bootstrap by name, so the word is supposed to appear. What must not
        appear is an import of a random source or a call into the normal
        distribution -- the two ways an approximation gets in."""
        import ast
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported, names = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
        for module in ("random", "statistics", "secrets", "numpy"):
            self.assertNotIn(module, imported, module)
        for name in ("NormalDist", "inv_cdf", "sqrt", "erf", "gauss",
                     "sample", "choices", "shuffle"):
            self.assertNotIn(name, names, name)

    def test_pricing_is_integer_arithmetic_so_a_no_change_pair_is_exactly_zero(self):
        """Why the price table is decimal STRINGS. 2.5e-6 USD per token is not
        representable in binary; priced through floats, differencing a run
        against itself leaves a residue that reads as a real cost change."""
        rate = score_cost.pico_per_token("input_per_mtok", "m", "2.50")
        self.assertEqual(rate, 2_500_000)
        per_case = [n * rate for n in (1013, 7717, 90011)]
        self.assertEqual(sum(a - b for a, b in zip(per_case, per_case)), 0)


# -- the price table -------------------------------------------------------

class TestPriceTable(CostCase):

    def test_a_run_is_priced_against_the_declared_table(self):
        run = self.write_run("smoke-20260901T120000Z",
                             [("c-1", "pass", 1.0, 1_000_000, 100_000)])
        rc, payload, err = run_script(run, "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        cost = payload["cost"]
        self.assertEqual(cost["status"], "priced")
        # 1M input at $2.50/Mtok + 100k output at $10/Mtok = 2.50 + 1.00
        self.assertEqual(cost["usd"], 3.5)
        self.assertEqual(cost["tokens"],
                         {"input": 1_000_000, "output": 100_000,
                          "total": 1_100_000})

    def test_an_unknown_model_withholds_the_dollars_and_keeps_the_tokens(self):
        """A partial total is an undercount that reads exactly like a cheap
        run, which is the failure SS4.3 is about. Tokens are MEASURED and
        dollars are DERIVED, so only the derived half is withheld."""
        run = self.write_run("smoke-20260901T120000Z",
                             [("c-1", "pass", 1.0, 1000, 100)], model="m-9")
        rc, payload, err = run_script(run, "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        cost = payload["cost"]
        self.assertEqual(cost["status"], "unpriced")
        self.assertEqual(cost["unpriced_models"], ["m-9"])
        self.assertNotIn("usd", cost)
        self.assertEqual(cost["tokens"]["total"], 1100)

    def test_the_model_match_is_exact_and_an_alias_is_a_declaration(self):
        """Decision D2: inference is DECLARED per app, never guessed. `m-1` and
        `m-1-2026-08-06` are different prices, so no prefix match -- but an
        `aliases` entry is the user saying so, and that is honoured."""
        run = self.write_run("smoke-20260901T120000Z",
                             [("c-1", "pass", 1.0, 1_000_000, 0)],
                             model="m-1-2026-08-06")
        rc, payload, _ = run_script(run, "--prices", self.prices())
        self.assertEqual(payload["cost"]["status"], "unpriced")
        table = self.prices(aliases={"m-1-2026-08-06": "m-1"})
        rc, payload, err = run_script(run, "--prices", table)
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["cost"]["usd"], 2.5)

    def test_the_table_must_be_dated_and_sourced(self):
        """Prices go stale silently; a table nobody has to date is a table
        nobody notices rotting. `age_days` rides in the output so a figure
        copied out of here carries its staleness with it."""
        run = self.simple("smoke-20260901T120000Z")
        for missing in ("as_of", "source"):
            doc = json.loads(json.dumps(PRICES))
            del doc[missing]
            path = self.write_json(self.tmp / "bad.json", doc)
            rc, payload, _ = run_script(run, "--prices", path)
            self.assertEqual(rc, 2)
            self.assertIn(missing, payload["error"])
        rc, payload, _ = run_script(run, "--prices", self.prices())
        self.assertIsInstance(payload["cost"]["price_table"]["age_days"], int)

    def test_a_float_price_is_refused_not_rounded(self):
        table = self.prices(models={"m-1": {"input_per_mtok": 2.5,
                                            "output_per_mtok": "10.00"}})
        run = self.simple("smoke-20260901T120000Z")
        rc, payload, _ = run_script(run, "--prices", table)
        self.assertEqual(rc, 2)
        self.assertIn("decimal STRING", payload["error"])

    def test_without_a_table_tokens_are_reported_and_dollars_are_not(self):
        run = self.write_run("smoke-20260901T120000Z",
                             [("c-1", "pass", 1.0, 1000, 100)])
        rc, payload, err = run_script(run)
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["cost"]["status"], "unpriced")
        self.assertIn("no price table declared", payload["cost"]["reason"])
        self.assertEqual(payload["cost"]["tokens"]["total"], 1100)


# -- which runs can be priced ---------------------------------------------

class TestWhichRunsPrice(CostCase):
    """SS6 writes trajectory.json `# only when: a trace was collected`, so the
    cost input is conditional and the latency input is not."""

    def test_a_traceless_run_has_no_cost_input_and_a_full_latency_block(self):
        """This is the field test's shape: `field-test-qa/.agent-eval/`
        declares `correlation: none`, so run_cases.py puts cost_latency in
        traces.disabled_layers and writes no trajectory.json at all."""
        run = self.simple("smoke-20260901T120000Z", n=6, trace=False)
        rc, payload, err = run_script(run, "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["cost"]["status"], "unpriced")
        self.assertEqual(payload["cost"]["cases_without_trace"], 6)
        self.assertIn("trace-less", payload["cost"]["reason"])
        # NOT reported as zero tokens: nothing was recorded, not nothing spent.
        self.assertNotIn("tokens", payload["cost"])
        self.assertEqual(payload["latency"]["n"], 6)
        self.assertEqual(payload["latency"]["min"], 1.0)

    def test_a_case_that_lost_its_trace_is_counted_not_priced_at_zero(self):
        """A case priced at zero because its trace went missing is
        indistinguishable in the total from a genuinely free case."""
        run = self.write_run("smoke-20260901T120000Z",
                             [("c-1", "pass", 1.0, 1000, 100),
                              ("c-2", "pass", 1.0, 1000, 100)])
        shutil.rmtree(run / "cases" / "c-2")
        rc, payload, err = run_script(run, "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        self.assertEqual((payload["cost"]["cases_with_trace"],
                          payload["cost"]["cases_without_trace"]), (1, 1))
        self.assertEqual(payload["cost"]["tokens"]["input"], 1000)

    def test_a_run_that_is_not_quotable_is_refused_by_name(self):
        """SS9(c) writes 'ok' only after the completeness check passes; the
        shipped pre-runner directory has no status at all."""
        run = self.simple("smoke-20260901T120000Z", status="incomplete")
        rc, payload, _ = run_script(run)
        self.assertEqual(rc, 2)
        self.assertIn("not quotable", payload["error"])

    def test_a_directory_that_is_not_a_run_names_the_marker_it_wants(self):
        rc, payload, _ = run_script(self.reports)
        self.assertEqual(rc, 2)
        self.assertIn("manifest.yaml", payload["error"])

    def test_latency_percentiles_are_nearest_rank_and_saturation_is_named(self):
        """An interpolated p90 is a number no case took. At small n a high
        percentile IS the maximum, and reporting the two as separate evidence
        is how a reader concludes the tail is characterized when it is not."""
        run = self.write_run("smoke-20260901T120000Z",
                             [(f"c-{i}", "pass", float(i + 1), 10, 1)
                              for i in range(10)])
        rc, payload, err = run_script(run)
        self.assertEqual(rc, 0, err)
        lat = payload["latency"]
        self.assertEqual((lat["p50"], lat["p90"], lat["max"]), (5.0, 9.0, 10.0))
        self.assertEqual(lat["saturated"], ["p95", "p99"])

    def test_a_case_with_no_latency_is_absent_never_imputed(self):
        run = self.write_run("smoke-20260901T120000Z",
                             [("c-1", "pass", 2.0, 10, 1),
                              ("c-2", "skipped", None, 10, 1)])
        rc, payload, err = run_script(run)
        self.assertEqual(rc, 0, err)
        self.assertEqual((payload["latency"]["n"], payload["latency"]["missing"]),
                         (1, 1))
        self.assertEqual(payload["latency"]["min"], 2.0)


# -- the paired comparison -------------------------------------------------

class TestPaired(CostCase):

    def pair(self, **kw):
        base = self.write_run("smoke-20260901T120000Z",
                              [(f"c-{i}", "pass", 1.0, 1000, 100)
                               for i in range(8)])
        cand = self.write_run("smoke-20260902T120000Z",
                              [(f"c-{i}", "pass", 1.5, 3000, 100)
                               for i in range(8)], **kw)
        return base, cand

    def test_the_paired_block_reports_all_three_exact_tests(self):
        base, cand = self.pair()
        rc, payload, err = run_script(cand, "--baseline", base,
                                      "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        cost = payload["paired"]["cost"]
        self.assertEqual(cost["status"], "priced")
        # (3000*2.50 + 100*10.00) / (1000*2.50 + 100*10.00) = 8500/3500
        self.assertEqual(cost["ratio"], round(8500 / 3500, 4))
        self.assertTrue(cost["permutation_test"]["exact"])
        self.assertTrue(cost["sign_test"]["exact"])
        self.assertIn("interval", cost["median_delta_ci"])
        self.assertAlmostEqual(cost["sign_test"]["p_two_sided"],
                               2 * 0.5 ** 8, places=12)

    def test_the_partition_by_verdict_change_is_not_a_rate(self):
        """The closest this script comes to SS4.3's "2% better but 3x more
        expensive", and deliberately a partition: dollars-per-net-fixed-case
        needs the discordant-pair count, and computing that here would be a
        second implementation of stats.py's pairing."""
        base = self.write_run("smoke-20260901T120000Z",
                              [("c-1", "fail", 1.0, 1000, 0),
                               ("c-2", "pass", 1.0, 1000, 0)])
        cand = self.write_run("smoke-20260902T120000Z",
                              [("c-1", "pass", 1.0, 5000, 0),
                               ("c-2", "pass", 1.0, 5000, 0)])
        rc, payload, err = run_script(cand, "--baseline", base,
                                      "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        groups = payload["paired"]["by_verdict_change"]
        self.assertEqual((groups["changed"]["n"], groups["unchanged"]["n"]),
                         (1, 1))
        self.assertEqual(groups["unchanged"]["cost_usd_delta"], 0.01)
        self.assertNotIn("per_net", json.dumps(groups))

    def test_a_mismatched_pair_warns_and_still_reports(self):
        """SS5.6's rule restated. This script makes no decision, so a
        mismatched pair is a caveat on a number rather than a refusal."""
        base, cand = self.pair()
        results = json.loads((cand / "results.json").read_text())
        results["dataset_version"] = 2
        (cand / "results.json").write_text(json.dumps(results))
        rc, payload, err = run_script(cand, "--baseline", base)
        self.assertEqual(rc, 0, err)
        self.assertIn("dataset_version",
                      " ".join(payload["paired"]["comparability_warnings"]))
        self.assertEqual(payload["paired"]["n_paired"], 8)

    def test_two_runs_sharing_no_case_is_a_fact_not_an_error(self):
        base = self.write_run("smoke-20260901T120000Z",
                              [("a-1", "pass", 1.0, 10, 1)])
        cand = self.write_run("smoke-20260902T120000Z",
                              [("b-1", "pass", 1.0, 10, 1)])
        rc, payload, err = run_script(cand, "--baseline", base)
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["paired"]["n_paired"], 0)
        self.assertEqual(payload["paired"]["only_in_baseline"], ["a-1"])

    def test_one_side_unpriced_withholds_the_paired_dollars(self):
        base, cand = self.pair(model="m-9")
        rc, payload, err = run_script(cand, "--baseline", base,
                                      "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["paired"]["cost"]["status"], "unpriced")
        # ...and the latency half is unaffected by the pricing gap.
        self.assertEqual(payload["paired"]["latency"]["n_paired"], 8)


# -- the seal --------------------------------------------------------------

class TestHoldoutSeal(CostCase):
    """The look ledger is a LINE COUNT and the seal is aggregate-only, so a
    cost report that lists holdout ids breaks both."""

    def test_no_sealed_id_can_appear_because_cases_is_never_listed(self):
        run = self.write_run("smoke-20260901T120000Z",
                             [("visible-1", "pass", 1.0, 1000, 100)],
                             holdout={"n": 2, "passes": 1,
                                      "gating_failures": 1})
        # The runner gives holdout cases ordinary directories (SS6); the seal
        # is enforced at results.json, so a script that LISTS cases/ leaks.
        self.write_json(run / "cases" / "sealed-9" / "trajectory.json",
                        {"status": "ok",
                         "trajectory": {"llm_calls": [
                             {"model": "m-1", "usage": {
                                 "input_tokens": 999999,
                                 "output_tokens": 0}}]}})
        rc, payload, err = run_script(run, "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        blob = json.dumps(payload)
        self.assertNotIn("sealed-9", blob)
        # ...and the sealed case's tokens are not in the total either.
        self.assertEqual(payload["cost"]["tokens"]["input"], 1000)
        self.assertEqual(payload["run"]["holdout_excluded"], 2)

    def test_the_two_files_carrying_holdout_ids_are_never_opened(self):
        source = SCRIPT.read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]
        for name in ("verdicts.jsonl", "verdicts_for_stats.jsonl",
                     "holdout-looks"):
            self.assertNotIn(name, body, name)


# -- one keep rule ---------------------------------------------------------

class TestNotADecision(CostCase):
    """`stats.py` owns the only definition of "better" in this package. SS4.3's
    fix implies a second one; this file reports beside it instead."""

    def test_the_output_carries_no_verdict_word_and_no_threshold(self):
        base = self.write_run("smoke-20260901T120000Z",
                              [("c-1", "pass", 1.0, 1000, 100)])
        cand = self.write_run("smoke-20260902T120000Z",
                              [("c-1", "pass", 9.0, 90000, 100)])
        rc, payload, err = run_script(cand, "--baseline", base,
                                      "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        keys = set()

        def walk(node):
            if isinstance(node, dict):
                keys.update(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(payload)
        for word in score_cost.FORBIDDEN_VERDICT_WORDS:
            self.assertNotIn(word, keys, word)
        self.assertIn("stats.py", payload["not_a_decision"])

    def test_the_bootstrap_is_declined_by_name_in_the_payload(self):
        """SS4.3 is the only SS4 item that names a METHOD, and the method it
        names contradicts stats.py's standard. Declining it silently would
        leave a reader thinking it was overlooked."""
        run = self.simple("smoke-20260901T120000Z")
        rc, payload, err = run_script(run)
        self.assertEqual(rc, 0, err)
        self.assertIn("bootstrap", payload["declined"])
        self.assertIn("Monte Carlo", payload["declined"])

    def test_the_limits_ride_in_the_payload_not_only_the_docstring(self):
        """A number copied into a report loses its docstring. cached tokens,
        TTFT, retries and k>1 all change what the figure means."""
        run = self.simple("smoke-20260901T120000Z")
        rc, payload, err = run_script(run)
        self.assertEqual(rc, 0, err)
        limits = payload["declared_limits"]
        self.assertIn("cache", json.dumps(limits).lower())
        self.assertIn("NOT MEASURED", limits["ttft"])
        self.assertIn("retry_count", limits["retries"])
        self.assertIn("REPRESENTATIVE", limits["repeats"])
        self.assertEqual(payload["cost"]["cache_accounting"], "none")


# -- against the real runner ----------------------------------------------

class TestAgainstTheRealRunner(unittest.TestCase):
    """The field names, checked against run_cases.py and normalize_trace.py
    rather than against this file's fixtures. "The data is already on disk" has
    been the wrong sentence five times in this wave; a synthetic trajectory.json
    would keep passing if the normalizer renamed `llm_calls[].usage`."""

    def test_a_real_traced_run_is_priced_from_its_own_artifacts(self):
        from test_run_cases import FakeApp, make_case, make_plan, otlp, span

        trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        state = tmp / ".agent-eval"
        (state / "reports").mkdir(parents=True)
        spans = tmp / "spans.json"
        spans.write_text(json.dumps(otlp([
            span(trace_id, "aaaaaaaaaaaaaaa1", "invoke_agent", "units"),
            span(trace_id, "aaaaaaaaaaaaaaa2", "chat", "llm",
                 **{"gen_ai.request.model": "m-1",
                    "gen_ai.usage.input_tokens": 1000,
                    "gen_ai.usage.output_tokens": 100}),
        ])), encoding="utf-8")

        app = FakeApp(lambda path, body, headers:
                      (200, {"message": "ok", "traceId": trace_id}, {}))
        self.addCleanup(app.close)
        run_id = "smoke-20260901T120000Z"
        plan = make_plan(state, app.base_url, cases=[make_case("c-0001")],
                         run_id=run_id)
        plan["adapter"]["traces"] = {
            "source": "otlp-file", "convention": "gen_ai",
            "correlation": "response-field:traceId", "location": str(spans),
            "completeness": {"quiescence_ms": 10, "max_wait_s": 2}}
        plan["adapter"]["invocation"]["health_check"] = {
            "method": "POST", "path": "/api/chat/ask", "expect_status": [200]}
        plan["capability_matrix"] = {"routing": {"enabled": True},
                                     "cost_latency": {"enabled": True}}
        plan_path = tmp / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        import os
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_cases.py"),
             "--plan", str(plan_path),
             "--out", str(state / "reports" / run_id)],
            capture_output=True, text=True,
            env=dict(**os.environ, EVAL_USER_ID="1"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        table = tmp / "prices.json"
        table.write_text(json.dumps(PRICES), encoding="utf-8")
        rc, payload, err = run_script(state / "reports" / run_id,
                                      "--prices", table)
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["cost"]["status"], "priced")
        self.assertEqual(payload["cost"]["tokens"],
                         {"input": 1000, "output": 100, "total": 1100})
        self.assertEqual(payload["cost"]["usd"], round(0.0025 + 0.001, 6))
        self.assertEqual(payload["latency"]["n"], 1)

    def test_cost_latency_is_declared_but_is_not_a_per_case_layer(self):
        """The Step 10 question, answered the other way. cost_latency is
        `enabled: true` in profile-schema.md and is in TRACE_DEPENDENT_LAYERS,
        but it has NO row in the contract's SS5 layer table and no entry in
        LAYER_ORDER -- deliberately, because a per-case cost VERDICT would need
        a per-case budget and the case format has no such field. It is
        run-level, like score_routing.py (SS5.2)."""
        import run_cases
        self.assertIn("cost_latency", run_cases.TRACE_DEPENDENT_LAYERS)
        self.assertNotIn("cost_latency", run_cases.LAYER_ORDER)
        contract = (SCRIPTS.parent / "docs" / "runner-contract.md").read_text(
            encoding="utf-8")
        self.assertIn("### 5.7", contract)
        self.assertIn("score_cost.py", contract)


if __name__ == "__main__":
    unittest.main()
