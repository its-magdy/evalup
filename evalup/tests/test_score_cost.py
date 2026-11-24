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
                         indexed, never listed. TestHoldoutAggregate (#141)
                         prices the sealed cases as totals, never per case,
                         and withholds a total over too few to hide one.
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
    "schema": "evalup/price-table/1",
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
                    "scoring_version": 1, "dataset_version": 1,
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
            "scoring_version": 1,
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

    def test_a_token_count_that_is_not_a_count_withholds_the_dollars(self):
        """Each of these used to be priced as ZERO tokens at "status":
        "priced" -- a negative one as negative dollars (2026-09 audit). That is
        the zero-imputation case_usage's docstring forbids, one level down."""
        for bad in (1000.5, -1000, "1000", True):
            with self.subTest(input_tokens=bad):
                run = self.write_run("smoke-20260901T120000Z",
                                     [("c-1", "pass", 1.0, bad, 100)])
                rc, payload, err = run_script(run, "--prices", self.prices())
                self.assertEqual(rc, 0, err)
                cost = payload["cost"]
                self.assertEqual(cost["status"], "unpriced")
                self.assertEqual(cost["calls_with_unusable_token_count"], 1)
                self.assertNotIn("usd", cost)
                shutil.rmtree(run)

    def test_an_integral_float_is_the_count_it_spells(self):
        """1000.0 is a thousand tokens after a JS-number round trip."""
        run = self.write_run("smoke-20260901T120000Z",
                             [("c-1", "pass", 1.0, 1_000_000.0, 100_000.0)])
        rc, payload, err = run_script(run, "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["cost"]["status"], "priced")
        self.assertEqual(payload["cost"]["tokens"]["total"], 1_100_000)

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
        """This is the shape of an adapter declaring `correlation: none`:
        run_cases.py puts cost_latency in traces.disabled_layers and writes no
        trajectory.json at all. A real July 2026 field test did exactly this."""
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

    def test_max_exact_states_declines_the_permutation_test_from_the_cli(self):
        """#20: the flag reaches the DP. The same pair is tested exactly at
        the default cap and declined, with the cap named, under a tiny one;
        the sign test stands either way."""
        base, cand = self.pair()
        rc, payload, err = run_script(cand, "--baseline", base,
                                      "--prices", self.prices(),
                                      "--max-exact-states", "5")
        self.assertEqual(rc, 0, err)
        for metric in ("cost", "latency"):
            with self.subTest(metric):
                block = payload["paired"][metric]
                self.assertFalse(block["permutation_test"]["tested"])
                self.assertIn("more than 5 distinct subset sums",
                              block["permutation_test"]["reason"])
                self.assertTrue(block["sign_test"]["exact"])
        rc, payload, _ = run_script(cand, "--baseline", base,
                                    "--max-exact-states", "0")
        self.assertEqual(rc, 2)
        self.assertIn("--max-exact-states", payload["error"])

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

    def test_the_scoring_version_is_the_key_not_the_plugin_version(self):
        # #38: a plugin release alone is no caveat; a scoring change is, and
        # a run older than the field says it fell back to harness_version.
        def edit(run, **fields):
            for name in ("results.json", "manifest.yaml"):
                doc = json.loads((run / name).read_text())
                doc.update(fields)
                (run / name).write_text(json.dumps(doc))

        def warnings(base, cand):
            rc, payload, err = run_script(cand, "--baseline", base)
            self.assertEqual(rc, 0, err)
            return " ".join(payload["paired"]["comparability_warnings"])
        base, cand = self.pair()
        edit(cand, harness_version="0.2.0")
        self.assertNotIn("version", warnings(base, cand))
        edit(cand, scoring_version=2)
        self.assertIn("scoring_version: baseline 1, this run 2",
                      warnings(base, cand))
        edit(cand, scoring_version=None, harness_version="0.1.0")
        self.assertIn("this run predates scoring_version",
                      warnings(base, cand))
        # Two pre-field runs at different releases differ on harness_version,
        # and the note says so rather than blaming a scoring version.
        edit(base, scoring_version=None, harness_version="0.0.9")
        text = warnings(base, cand)
        self.assertIn("both runs predate scoring_version", text)
        self.assertIn("harness_version: baseline '0.0.9', this run '0.1.0'",
                      text)
        self.assertNotIn("a different scoring version", text)

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

    def test_the_files_carrying_holdout_ids_are_opened_in_one_place(self):
        """#141 reads verdicts.jsonl for the sealed aggregate, and only
        there: sealed_rows() alone opens it, sealed_cost() alone calls that,
        and nothing lists a directory."""
        source = SCRIPT.read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]
        for name in ("verdicts_for_stats.jsonl", "holdout-looks", "listdir",
                     "scandir", "glob", "os.walk"):
            self.assertNotIn(name, body, name)
        self.assertEqual(body.count('"verdicts.jsonl"'), 1)
        functions = body.split("\ndef ")
        readers = [f.split("(", 1)[0] for f in functions[1:]
                   if "VERDICTS)" in f]
        self.assertEqual(readers, ["sealed_rows"])
        callers = [f.split("(", 1)[0] for f in functions[1:]
                   if "sealed_rows(" in f.split(":", 1)[1]]
        self.assertEqual(callers, ["sealed_cost"])


class TestHoldoutAggregate(CostCase):
    """#141: a holdout run's every case is sealed, so `cost` priced nothing
    and the meter charged an estimate. cost.holdout_aggregate prices the
    sealed cases as totals, and withholds a total that would be one case's."""

    HOLDOUT_RUN = "holdout-20260901T120000Z"

    def sealed_run(self, sealed, visible=(), k=1, verdict_holdout=True,
                   summary_n=None, **manifest):
        """A run whose `sealed` cases [(id, input, output), ...] have case
        directories and verdicts.jsonl rows but no results.json row."""
        n = len(sealed) if summary_n is None else summary_n
        out = self.write_run(
            self.HOLDOUT_RUN, list(visible), k=k, mode="holdout",
            selecting_split="holdout",
            holdout={"n": n, "passes": n, "failures": 0,
                     "gating_failures": 0, "looks_recorded": 1},
            **manifest)
        rows = [{"case_id": c[0], "verdict": c[1], "set": "holdout",
                 "canary": False, "gating": True, "layers": {},
                 "multi_turn": False, "turns_sent": 1, "failed_turn": None}
                for c in visible]
        for case_id, n_in, n_out in sealed:
            rows.append({"case_id": case_id, "verdict": "pass",
                         "set": "holdout", "canary": False, "gating": True,
                         "layers": {}, "multi_turn": False, "turns_sent": 1,
                         "failed_turn": None})
            case = out / "cases" / case_id
            self.write_json(case / "verdict.json", {
                "case_id": case_id, "verdict": "pass",
                "holdout": verdict_holdout, "k": k})
            if n_in is not None:
                self.write_json(case / "trajectory.json", {
                    "status": "ok", "trajectory": {"llm_calls": [{
                        "model": "m-1", "usage": {
                            "input_tokens": n_in,
                            "output_tokens": n_out}}]}})
        (out / "verdicts.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return out

    def five(self):
        return [(f"sealed-{i}", 1000 * (i + 1), 100) for i in range(5)]

    def assert_no_sealed_id(self, payload, sealed):
        blob = json.dumps(payload)
        for case_id, *_ in sealed:
            self.assertNotIn(case_id, blob)

    def test_a_holdout_run_is_priced_as_one_total(self):
        sealed = self.five()
        rc, payload, err = run_script(self.sealed_run(sealed),
                                      "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        agg = payload["cost"]["holdout_aggregate"]
        self.assertEqual(agg["status"], "priced")
        self.assertEqual(agg["n"], 5)
        self.assertEqual(agg["cases_with_usage"], 5)
        # 15k input at 2.50 + 500 output at 10.00 per Mtok.
        self.assertEqual(agg["tokens"],
                         {"input": 15000, "output": 500, "total": 15500})
        self.assertEqual(agg["usd"], round(0.0375 + 0.005, 6))
        # Totals only: no per-case row or mean, no model, no id.
        for key in ("models", "usd_per_case_with_trace", "unpriced_models"):
            self.assertNotIn(key, agg)
        self.assert_no_sealed_id(payload, sealed)
        # The visible block says why it is empty, not "trace-less".
        self.assertEqual(payload["cost"]["status"], "unpriced")
        self.assertIn("every case in this run is sealed",
                      payload["cost"]["reason"])
        self.assertIn("holdout_aggregate",
                      payload["declared_limits"]["holdout"])

    def test_the_visible_cases_stay_out_of_the_aggregate(self):
        sealed = self.five()
        run = self.sealed_run(sealed,
                              visible=[("canary-1", "pass", 1.0, 7, 3)])
        rc, payload, err = run_script(run, "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["cost"]["tokens"]["total"], 10)
        self.assertEqual(
            payload["cost"]["holdout_aggregate"]["tokens"]["total"], 15500)
        self.assertNotIn("reason", payload["cost"])

    def test_a_total_over_too_few_cases_is_withheld(self):
        """A one-case total IS that case's usage; below the floor nothing
        but the count is said, tokens included."""
        for sealed in ([("sealed-only", 123457, 89)],
                       self.five()[:4],
                       self.five()[:4] + [("sealed-untraced", None, None)]):
            with self.subTest(n=len(sealed)):
                shutil.rmtree(self.reports / self.HOLDOUT_RUN, True)
                rc, payload, err = run_script(self.sealed_run(sealed),
                                              "--prices", self.prices())
                self.assertEqual(rc, 0, err)
                agg = payload["cost"]["holdout_aggregate"]
                self.assertEqual(agg["status"], "withheld")
                self.assertEqual(set(agg), {"n", "floor", "status",
                                            "reason", "cases_with_usage",
                                            "note"})
                self.assertEqual(agg["floor"],
                                 score_cost.HOLDOUT_AGGREGATE_FLOOR)
                self.assert_no_sealed_id(payload, sealed)
                self.assertNotIn("123457", json.dumps(payload))

    def test_the_floor_counts_only_cases_that_spent_something(self):
        """Five sealed traces, four empty, zero or unusable: the "total"
        would be the fifth case's own usage (Grok's review of #149)."""
        empties = {
            "no llm call": [],
            "zero tokens": [{"model": "m-1", "usage": {
                "input_tokens": 0, "output_tokens": 0}}],
            "unusable count": [{"model": "m-1", "usage": {
                "input_tokens": 1.5, "output_tokens": 1}}],
        }
        for name, calls in empties.items():
            with self.subTest(name):
                shutil.rmtree(self.reports / self.HOLDOUT_RUN, True)
                sealed = [("sealed-big", 123457, 89)] + [
                    (f"sealed-{i}", 1, 1) for i in range(4)]
                run = self.sealed_run(sealed)
                for case_id, *_ in sealed[1:]:
                    self.write_json(
                        run / "cases" / case_id / "trajectory.json",
                        {"status": "ok",
                         "trajectory": {"llm_calls": calls}})
                rc, payload, err = run_script(run, "--prices", self.prices())
                self.assertEqual(rc, 0, err)
                agg = payload["cost"]["holdout_aggregate"]
                self.assertEqual(agg["status"], "withheld")
                self.assertEqual(agg["cases_with_usage"], 1)
                self.assertNotIn("123457", json.dumps(payload))

    def test_no_model_name_or_per_case_mean_is_published(self):
        sealed = self.five()
        run = self.sealed_run(sealed)
        self.write_json(run / "cases" / "sealed-0" / "trajectory.json", {
            "status": "ok", "trajectory": {"llm_calls": [{
                "model": "only-sealed-model", "usage": {
                    "input_tokens": 10, "output_tokens": 1}}]}})
        rc, payload, err = run_script(run, "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        agg = payload["cost"]["holdout_aggregate"]
        self.assertEqual(agg["status"], "unpriced")
        self.assertEqual(agg["unpriced_model_count"], 1)
        self.assertNotIn("only-sealed-model", json.dumps(payload))
        self.assertNotIn("usd", agg)

    def test_a_sealed_conversation_is_priced_over_its_turns(self):
        sealed = self.five()
        run = self.sealed_run(sealed)
        rows = [json.loads(line) for line in
                (run / "verdicts.jsonl").read_text(
                    encoding="utf-8").splitlines()]
        rows[0].update(multi_turn=True, turns_sent=2)
        (run / "verdicts.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        for turn in (1, 2):
            self.write_json(
                run / "cases" / "sealed-0" / "turns" / str(turn)
                / "trajectory.json",
                {"status": "ok", "trajectory": {"llm_calls": [{
                    "model": "m-1", "usage": {"input_tokens": 500,
                                              "output_tokens": 50}}]}})
        rc, payload, err = run_script(run, "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        # sealed-0's 1000/100 single trajectory is replaced by 2 x 500/50.
        self.assertEqual(
            payload["cost"]["holdout_aggregate"]["tokens"]["total"], 15500)

    def test_a_sealed_set_it_cannot_reconcile_is_withheld_by_count(self):
        cases = {
            "not marked holdout": dict(verdict_holdout=False),
            "count disagrees": dict(summary_n=6),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.reports / self.HOLDOUT_RUN, True)
                sealed = self.five()
                rc, payload, err = run_script(self.sealed_run(sealed, **kw),
                                              "--prices", self.prices())
                self.assertEqual(rc, 0, err)
                agg = payload["cost"]["holdout_aggregate"]
                self.assertEqual(agg["status"], "withheld")
                self.assertIn("could not be reconciled", agg["reason"])
                self.assertNotIn("tokens", agg)
                self.assert_no_sealed_id(payload, sealed)

    def test_an_unreadable_record_withholds_and_names_no_id(self):
        sealed = self.five()
        run = self.sealed_run(sealed)
        for text in ("not json\n", '{"case_id": "../x"}\n'):
            with self.subTest(text=text):
                (run / "verdicts.jsonl").write_text(text, encoding="utf-8")
                rc, payload, err = run_script(run, "--prices", self.prices())
                self.assertEqual(rc, 0, err)
                agg = payload["cost"]["holdout_aggregate"]
                self.assertEqual(agg["status"], "withheld")
                self.assert_no_sealed_id(payload, sealed)
        (run / "verdicts.jsonl").unlink()
        rc, payload, err = run_script(run, "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["cost"]["holdout_aggregate"]["status"],
                         "withheld")

    def test_a_run_that_sealed_nothing_has_no_aggregate(self):
        rc, payload, err = run_script(self.simple("smoke-20260901T120000Z"),
                                      "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        self.assertNotIn("holdout_aggregate", payload["cost"])

    def test_every_repeat_of_the_sealed_cases_is_totalled(self):
        """k = 3 under traces.source: response, the holdout plan's shape:
        all_repeats is the look's spend, the number the meter wants."""
        sealed = [(f"sealed-{i}", 100_000, 1_000) for i in range(5)]
        run = self.sealed_run(sealed, k=3, **AG_UI)
        for case_id, *_ in sealed:
            case = run / "cases" / case_id
            for n, usage in enumerate(REPEAT_USAGE, 1):
                self.write_json(case / "repeats" / str(n) / "response.json",
                                {"case_id": case_id, "repeat": n,
                                 "body": sse_body(*usage)})
            verdict = json.loads((case / "verdict.json").read_text(
                encoding="utf-8"))
            verdict["repeats"] = [{"n": n, "verdict": "pass", "layers": {}}
                                  for n in (1, 2, 3)]
            self.write_json(case / "verdict.json", verdict)
        prices = self.prices(
            models={"m-1": dict(PRICES["models"]["m-1"], **CACHE_TIERS)},
            aliases={"<model attribute absent>": "m-1"})
        rc, payload, err = run_script(run, "--prices", prices)
        self.assertEqual(rc, 0, err)
        every = payload["cost"]["holdout_aggregate"]["all_repeats"]
        self.assertEqual(every["status"], "priced")
        self.assertEqual(every["repeats_sent"], 15)
        self.assertEqual(every["cases_with_usage"], 5)
        # The all-sealed run's visible figures say where the spend is.
        self.assertIn("every case in this run is sealed",
                      payload["cost"]["all_repeats"]["reason"])
        self.assertIn("holdout_aggregate",
                      payload["declared_limits"]["repeats"])
        self.assertNotIn("not priced here",
                         payload["declared_limits"]["repeats"])
        self.assertEqual(every["messages_with_usage"], 15)
        # 0.47 per case (TestEveryRepeat's arithmetic), five cases.
        self.assertEqual(every["usd"], 2.35)
        self.assertEqual(every["usd_per_message"], 0.156667)
        self.assertNotIn("models", every)
        self.assert_no_sealed_id(payload, sealed)
        # A stream that wrote no trajectory.json still leaves each repeat's
        # usage: the representative figure is withheld, all_repeats is not.
        for case_id, *_ in sealed:
            (run / "cases" / case_id / "trajectory.json").unlink()
        rc, payload, err = run_script(run, "--prices", prices)
        self.assertEqual(rc, 0, err)
        agg = payload["cost"]["holdout_aggregate"]
        self.assertEqual(agg["status"], "withheld")
        self.assertEqual(agg["all_repeats"]["usd"], 2.35)
        self.assertIn("all_repeats.usd", agg["note"])
        # Below the floor the all-repeats figure is withheld as well.
        shutil.rmtree(run / "cases" / "sealed-4" / "repeats")
        verdict = run / "cases" / "sealed-4" / "verdict.json"
        doc = json.loads(verdict.read_text(encoding="utf-8"))
        doc["repeats"] = [{"n": n, "verdict": "pass"} for n in (1, 2, 3)]
        self.write_json(verdict, doc)
        rc, payload, err = run_script(run, "--prices", prices)
        self.assertEqual(rc, 0, err)
        every = payload["cost"]["holdout_aggregate"]["all_repeats"]
        self.assertEqual(every["status"], "withheld")
        self.assertNotIn("usd", every)


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
        # #140: a visible id indexes response.json; what is missing is the
        # attempts before the last, which the runner does not keep.
        self.assertIn("NOT PRICED", limits["retries"])
        self.assertIn("LAST", limits["retries"])
        self.assertIn("one trace id", limits["retries"])
        self.assertNotIn("sealed", limits["retries"])
        self.assertIn("REPRESENTATIVE", limits["repeats"])
        self.assertEqual(payload["cost"]["cache_accounting"], "none")


# -- cache accounting (#105) ------------------------------------------------

# What a run's manifest says when its trace is an AG-UI stream read as its own
# trace: the one source whose cache counts are declared parts of input_tokens.
AG_UI = {"invocation": {"response_format": "sse",
                        "sse": {"protocol": "ag-ui"}},
         "traces": {"source": "response"}}
CACHE_TIERS = {"cache_read_per_mtok": "0.25", "cache_write_per_mtok": "3.125"}


class TestCacheAccounting(CostCase):
    """A cached agent was priced about 2x low (#105): normalize_trace.py
    dropped the cache counts, so every input token cost the input rate.
    The split is opt-in per model, and only where the source declares it."""

    def cached_run(self, run_id, usage, model="m-1", **manifest_extra):
        """One case whose single LLM call has exactly this usage dict."""
        out = self.write_run(run_id, [("c-1", "pass", 1.0, 0, 0)],
                             **manifest_extra)
        self.write_json(out / "cases" / "c-1" / "trajectory.json", {
            "status": "ok",
            "trajectory": {"trace_id": "t", "agents": [], "tool_calls": [],
                           "llm_calls": [{"model": model, "duration_ms": 10,
                                          "usage": usage}],
                           "usage": usage},
            "checks": {}})
        return out

    # 1M input, of which 600k cache reads and 100k cache writes; 100k output.
    USAGE = {"input_tokens": 1_000_000, "output_tokens": 100_000,
             "cache_read_input_tokens": 600_000,
             "cache_write_input_tokens": 100_000}

    def tiered(self, **extra):
        models = {"m-1": dict(PRICES["models"]["m-1"], **CACHE_TIERS)}
        return self.prices(models=models, **extra)

    def test_both_tiers_price_each_part_separately(self):
        run = self.cached_run("smoke-20260901T120000Z", self.USAGE, **AG_UI)
        rc, payload, err = run_script(run, "--prices", self.tiered())
        self.assertEqual(rc, 0, err)
        cost = payload["cost"]
        self.assertEqual(cost["status"], "priced")
        self.assertEqual(cost["cache_accounting"], "priced")
        # 300k uncached x 2.50 + 600k x 0.25 + 100k x 3.125 + 100k x 10.00
        # = 0.75 + 0.15 + 0.3125 + 1.00
        self.assertEqual(cost["usd"], 2.2125)
        self.assertEqual(cost["tokens"], {
            "input": 1_000_000, "output": 100_000, "total": 1_100_000,
            "cache_read_input": 600_000, "cache_write_input": 100_000,
            "calls_reporting_cache": 1})
        self.assertEqual(cost["models"]["m-1"]["cache_read_input_tokens"],
                         600_000)
        self.assertNotIn("cache_warning", cost)

    def test_absent_tiers_price_as_before_and_say_so(self):
        run = self.cached_run("smoke-20260901T120000Z", self.USAGE, **AG_UI)
        rc, payload, err = run_script(run, "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        cost = payload["cost"]
        self.assertEqual(cost["status"], "priced")
        # Every input token at the input rate, exactly today's figure.
        self.assertEqual(cost["usd"], 3.5)
        self.assertEqual(cost["cache_accounting"], "unpriced")
        for key in ("cache_read_per_mtok", "cache_write_per_mtok", "m-1"):
            self.assertIn(key, cost["cache_warning"])

    def test_a_table_without_the_new_keys_prices_exactly_as_before(self):
        """The same run, with and without cache counts, against the old
        table: the same dollars to the pico-USD, and a run with no cache
        counts carries no cache field at all. A tiered table does not move
        a run that reported no cache counts either."""
        plain = self.write_run("smoke-20260901T120000Z",
                               [("c-1", "pass", 1.0, 1_000_000, 100_000)],
                               **AG_UI)
        cached = self.cached_run("smoke-20260902T120000Z", self.USAGE,
                                 **AG_UI)
        old = self.prices()
        _, before, _ = run_script(plain, "--prices", old)
        _, after, _ = run_script(cached, "--prices", old)
        _, tiered, _ = run_script(plain, "--prices", self.tiered())
        self.assertEqual(before["cost"]["usd"], 3.5)
        self.assertEqual(after["cost"]["usd"], before["cost"]["usd"])
        self.assertEqual(tiered["cost"]["usd"], before["cost"]["usd"])
        self.assertEqual(before["cost"]["cache_accounting"], "none")
        self.assertEqual(before["cost"]["tokens"],
                         {"input": 1_000_000, "output": 100_000,
                          "total": 1_100_000})
        self.assertFalse({"cache_warning", "cache_note"} & set(before["cost"]))

    def test_an_undeclared_source_reports_cache_counts_and_does_not_split(self):
        """An OTLP store (or a JSON body, or a data-path stream) does not say
        whether a cache count sits inside input_tokens or beside it, so the
        split is not made."""
        for manifest in ({}, {"traces": {"source": "otlp-file"}},
                         {"invocation": {"response_format": "json"},
                          "traces": {"source": "response"}},
                         {"invocation": {"response_format": "sse",
                                         "sse": {"protocol": "data-path"}},
                          "traces": {"source": "response"}},
                         dict(AG_UI, traces={"source": "otlp-file"})):
            with self.subTest(manifest=manifest):
                shutil.rmtree(self.reports)
                self.reports.mkdir()
                run = self.cached_run("smoke-20260901T120000Z", self.USAGE,
                                      **manifest)
                rc, payload, err = run_script(run, "--prices", self.tiered())
                self.assertEqual(rc, 0, err)
                cost = payload["cost"]
                self.assertEqual(cost["usd"], 3.5)
                self.assertEqual(cost["cache_accounting"], "none")
                self.assertIn("cache_note", cost)
                self.assertEqual(cost["tokens"]["cache_read_input"], 600_000)

    def two_model_run(self, run_id, models):
        """Two cases, one call each, the same USAGE, on these two models."""
        out = self.write_run(run_id, [("c-1", "pass", 1.0, 0, 0),
                                      ("c-2", "pass", 1.0, 0, 0)], **AG_UI)
        for case_id, model in zip(("c-1", "c-2"), models):
            self.write_json(out / "cases" / case_id / "trajectory.json", {
                "status": "ok",
                "trajectory": {"trace_id": "t", "agents": [],
                               "tool_calls": [],
                               "llm_calls": [{"model": model,
                                              "duration_ms": 10,
                                              "usage": self.USAGE}],
                               "usage": self.USAGE},
                "checks": {}})
        return out

    def test_one_model_without_tiers_prices_the_whole_run_as_before(self):
        """All or nothing: m-1 split and m-2 unsplit would publish 5.7125, a
        figure that is neither the old price (7.0) nor a corrected one."""
        run = self.two_model_run("smoke-20260901T120000Z", ("m-1", "m-2"))
        models = {"m-1": dict(PRICES["models"]["m-1"], **CACHE_TIERS),
                  "m-2": PRICES["models"]["m-1"]}
        rc, payload, err = run_script(run, "--prices",
                                      self.prices(models=models))
        self.assertEqual(rc, 0, err)
        cost = payload["cost"]
        self.assertEqual(cost["status"], "priced")
        self.assertEqual(cost["usd"], 7.0)
        self.assertEqual(cost["cache_accounting"], "unpriced")
        self.assertIn("m-2", cost["cache_warning"])
        self.assertNotIn("m-1,", cost["cache_warning"])
        # Both tiered: the split, case by case.
        models["m-2"] = models["m-1"]
        rc, payload, err = run_script(run, "--prices",
                                      self.prices(models=models))
        self.assertEqual(payload["cost"]["usd"], 2 * 2.2125)
        self.assertEqual(payload["cost"]["cache_accounting"], "priced")

    def test_withheld_dollars_never_say_priced_or_warn(self):
        """No table, or an unknown model beside a tiered one: no figure
        was published, so nothing was split out of one."""
        run = self.two_model_run("smoke-20260901T120000Z", ("m-1", "m-x"))
        for argv in ((), ("--prices", self.tiered())):
            with self.subTest(argv=argv):
                rc, payload, err = run_script(run, *argv)
                self.assertEqual(rc, 0, err)
                cost = payload["cost"]
                self.assertEqual(cost["status"], "unpriced")
                self.assertEqual(cost["cache_accounting"], "unpriced")
                self.assertNotIn("cache_warning", cost)

    def test_an_empty_model_string_is_not_the_absent_sentinel(self):
        run = self.cached_run("smoke-20260901T120000Z", self.USAGE,
                              model="", **AG_UI)
        rc, payload, err = run_script(run, "--prices",
                                      self.tiered(aliases={"": "m-1"}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["cost"]["usd"], 2.2125)

    def test_one_tier_alone_is_refused(self):
        for field in CACHE_TIERS:
            with self.subTest(field=field):
                models = {"m-1": dict(PRICES["models"]["m-1"],
                                      **{field: CACHE_TIERS[field]})}
                run = self.simple("smoke-20260901T120000Z")
                rc, payload, _ = run_script(run, "--prices",
                                            self.prices(models=models))
                shutil.rmtree(run)
                self.assertEqual(rc, 2)
                for key in CACHE_TIERS:
                    self.assertIn(key, payload["error"])

    def test_a_float_tier_is_refused_like_any_price(self):
        models = {"m-1": dict(PRICES["models"]["m-1"],
                              cache_read_per_mtok=0.25,
                              cache_write_per_mtok="3.125")}
        rc, payload, _ = run_script(self.simple("smoke-20260901T120000Z"),
                                    "--prices", self.prices(models=models))
        self.assertEqual(rc, 2)
        self.assertIn("cache_read_per_mtok", payload["error"])

    def test_cache_counts_larger_than_input_withhold_the_dollars(self):
        usage = dict(self.USAGE, cache_read_input_tokens=950_000)
        run = self.cached_run("smoke-20260901T120000Z", usage, **AG_UI)
        rc, payload, err = run_script(run, "--prices", self.tiered())
        self.assertEqual(rc, 0, err)
        cost = payload["cost"]
        self.assertEqual(cost["status"], "unpriced")
        self.assertNotIn("usd", cost)
        self.assertEqual(cost["calls_with_cache_over_input"], 1)
        # Withheld dollars split nothing out: never "priced".
        self.assertEqual(cost["cache_accounting"], "unpriced")

    def test_a_bad_cache_count_is_an_unusable_count(self):
        usage = dict(self.USAGE, cache_write_input_tokens="lots")
        run = self.cached_run("smoke-20260901T120000Z", usage, **AG_UI)
        rc, payload, err = run_script(run, "--prices", self.tiered())
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["cost"]["status"], "unpriced")
        self.assertEqual(payload["cost"]["calls_with_unusable_token_count"], 1)

    def test_one_reported_tier_is_split_and_the_other_counts_zero(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0,
                 "cache_read_input_tokens": 400_000}
        run = self.cached_run("smoke-20260901T120000Z", usage, **AG_UI)
        rc, payload, err = run_script(run, "--prices", self.tiered())
        self.assertEqual(rc, 0, err)
        # 600k x 2.50 + 400k x 0.25 = 1.50 + 0.10
        self.assertEqual(payload["cost"]["usd"], 1.6)
        self.assertEqual(payload["cost"]["tokens"]["cache_write_input"], 0)

    def test_the_absent_model_can_be_declared_by_the_name_reported(self):
        """A `traces.source: response` span carries no gen_ai.request.model,
        so every such call is reported as `<model attribute absent>`. Naming
        that string in `aliases` prices it; before #105 nothing could."""
        run = self.cached_run("smoke-20260901T120000Z", self.USAGE,
                              model=None, **AG_UI)
        rc, payload, _ = run_script(run, "--prices", self.tiered())
        self.assertEqual(payload["cost"]["unpriced_models"],
                         ["<model attribute absent>"])
        rc, payload, err = run_script(run, "--prices", self.tiered(
            aliases={"<model attribute absent>": "m-1"}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(payload["cost"]["status"], "priced")
        self.assertEqual(payload["cost"]["usd"], 2.2125)

    def test_a_paired_diff_uses_each_runs_own_split(self):
        base = self.cached_run("smoke-20260901T120000Z", self.USAGE, **AG_UI)
        cand = self.cached_run("smoke-20260902T120000Z", dict(
            self.USAGE, cache_read_input_tokens=0,
            cache_write_input_tokens=0), **AG_UI)
        rc, payload, err = run_script(cand, "--baseline", base,
                                      "--prices", self.tiered())
        self.assertEqual(rc, 0, err)
        paired = payload["paired"]["cost"]
        self.assertEqual(paired["baseline_total_usd"], 2.2125)
        self.assertEqual(paired["candidate_total_usd"], 3.5)


# -- every repeat (#129) ----------------------------------------------------

def sse_body(*usages):
    """A decoded AG-UI response body as response.json records it: one
    run_usage entry per model call, in _sse's keys."""
    return {"sse": {"protocol": "ag-ui"}, "text": "ok",
            "run_usage": list(usages)}


# Three repeats of one cached case. Repeat 1 writes the cache, 2 and 3 read
# it (3 in two model calls). Priced with CACHE_TIERS (input 2.50, output
# 10.00, read 0.25, write 3.125 per Mtok):
#   1: 20k uncached 0.05 + 80k write 0.25 + 1k out 0.01 = 0.31
#   2: 20k uncached 0.05 + 80k read 0.02 + 1k out 0.01 = 0.08
#   3: 2 x (10k uncached 0.025 + 40k read 0.01 + 500 out 0.005) = 0.08
REPEAT_USAGE = (
    [{"input_tokens": 100_000, "output_tokens": 1_000,
      "cache_write_input_tokens": 80_000}],
    [{"input_tokens": 100_000, "output_tokens": 1_000,
      "cached_input_tokens": 80_000}],
    [{"input_tokens": 50_000, "output_tokens": 500,
      "cached_input_tokens": 40_000}] * 2,
)


class TestEveryRepeat(CostCase):
    """#129: a k=3 run on a cached app spent 0.47, not 3 x the 0.31 its
    representative repeat cost. Every repeat's response.json carries the
    usage, so it is priced, beside -- never instead of -- the old figure."""

    def tiered(self):
        return self.prices(models={"m-1": dict(PRICES["models"]["m-1"],
                                               **CACHE_TIERS)},
                           aliases={"<model attribute absent>": "m-1"})

    def k3_run(self, run_id="regression-20260901T120000Z", usages=REPEAT_USAGE,
               **manifest):
        manifest = dict(AG_UI, **manifest)
        manifest.setdefault("k", len(usages))
        out = self.write_run(run_id, [("c-1", "pass", 1.0, 0, 0)],
                             **manifest)
        case = out / "cases" / "c-1"
        # The case-level trajectory is the representative repeat's: repeat 1.
        first = REPEAT_USAGE[0][0]
        self.write_json(case / "trajectory.json", {
            "status": "ok",
            "trajectory": {"trace_id": "t", "agents": [], "tool_calls": [],
                           "llm_calls": [{"model": None, "duration_ms": None,
                                          "usage": {
                                              "input_tokens": 100_000,
                                              "output_tokens": 1_000,
                                              "cache_write_input_tokens":
                                                  first[
                                                  "cache_write_input_tokens"]}}],
                           "usage": {}},
            "checks": {}})
        for n, usage in enumerate(usages, 1):
            body = sse_body(*usage) if usage is not None else None
            self.write_json(case / "repeats" / str(n) / "response.json",
                            {"case_id": "c-1", "repeat": n, "body": body})
        self.write_json(case / "verdict.json", {
            "case_id": "c-1", "verdict": "pass",
            "repeats": [{"n": n, "verdict": "pass", "layers": {}}
                        for n in range(1, len(usages) + 1)]})
        return out

    def test_every_repeat_is_priced_and_the_representative_is_unchanged(self):
        rc, payload, err = run_script(self.k3_run(), "--prices", self.tiered())
        self.assertEqual(rc, 0, err)
        cost = payload["cost"]
        # The old figure keeps its meaning: one representative repeat.
        self.assertEqual(cost["usd"], 0.31)
        every = cost["all_repeats"]
        self.assertEqual(every["status"], "priced")
        self.assertEqual(every["cache_accounting"], "priced")
        self.assertEqual(every["usd"], 0.47)
        self.assertEqual((every["k"], every["repeats_sent"],
                          every["repeats_with_usage"],
                          every["repeats_without_usage"],
                          every["messages_with_usage"]), (3, 3, 3, 0, 3))
        self.assertEqual(every["usd_per_message"], round(0.47 / 3, 6))
        self.assertEqual(every["tokens"]["cache_read_input"], 160_000)
        self.assertEqual(every["tokens"]["cache_write_input"], 80_000)
        self.assertEqual(every["tokens"]["input"], 300_000)
        limit = payload["declared_limits"]["repeats"]
        self.assertIn("REPRESENTATIVE", limit)
        self.assertIn("all 3 repeats", limit)
        self.assertNotIn("roughly k", limit)
        self.assertNotIn("scratch directory that is deleted", limit)

    def test_a_repeat_with_no_usage_is_counted_not_priced_at_zero(self):
        usages = REPEAT_USAGE[:2] + (None,)
        rc, payload, err = run_script(self.k3_run(usages=usages),
                                      "--prices", self.tiered())
        self.assertEqual(rc, 0, err)
        every = payload["cost"]["all_repeats"]
        self.assertEqual((every["repeats_with_usage"],
                          every["repeats_without_usage"]), (2, 1))
        self.assertEqual(every["usd"], 0.39)
        limit = payload["declared_limits"]["repeats"]
        self.assertIn("priced 2 of 3 repeats", limit)
        self.assertIn("upper bound", limit)
        self.assertIn("cache", limit)

    def test_without_prices_the_tokens_of_every_repeat_still_report(self):
        rc, payload, err = run_script(self.k3_run())
        self.assertEqual(rc, 0, err)
        every = payload["cost"]["all_repeats"]
        self.assertEqual(every["status"], "unpriced")
        self.assertNotIn("usd", every)
        self.assertEqual(every["tokens"]["input"], 300_000)
        self.assertIn("withheld", payload["declared_limits"]["repeats"])

    def test_another_trace_source_says_why_and_prices_no_repeat(self):
        out = self.k3_run(traces={"source": "otlp-file"})
        rc, payload, err = run_script(out, "--prices", self.tiered())
        self.assertEqual(rc, 0, err)
        every = payload["cost"]["all_repeats"]
        self.assertEqual(every["status"], "unpriced")
        self.assertIn("scratch directory", every["reason"])
        self.assertNotIn("usd", every)
        limit = payload["declared_limits"]["repeats"]
        self.assertIn("overstates", limit)

    def test_k1_has_no_all_repeats_block(self):
        rc, payload, err = run_script(self.simple("smoke-20260901T120000Z"),
                                      "--prices", self.prices())
        self.assertEqual(rc, 0, err)
        self.assertNotIn("all_repeats", payload["cost"])
        self.assertIn("sent once", payload["declared_limits"]["repeats"])

    def test_a_conversation_repeat_sums_the_turns_it_sent(self):
        out = self.k3_run(usages=REPEAT_USAGE[:2])
        results = json.loads((out / "results.json").read_text())
        results["cases"][0].update(multi_turn=True, turns_sent=2)
        self.write_json(out / "results.json", results)
        case = out / "cases" / "c-1"
        for t in (1, 2):
            self.write_json(case / "turns" / str(t) / "trajectory.json",
                            json.loads((case / "trajectory.json")
                                       .read_text()))
        # Repeat 1 sent two turns (0.31 each), repeat 2 one turn (0.08).
        for n, turns in ((1, (REPEAT_USAGE[0], REPEAT_USAGE[0])),
                         (2, (REPEAT_USAGE[1],))):
            for t, usage in enumerate(turns, 1):
                self.write_json(
                    case / "repeats" / str(n) / "turns" / str(t)
                    / "response.json", {"body": sse_body(*usage)})
        self.write_json(case / "verdict.json", {
            "case_id": "c-1", "verdict": "pass",
            "repeats": [{"n": 1, "verdict": "pass", "turns_sent": 2},
                        {"n": 2, "verdict": "pass", "turns_sent": 1}]})
        rc, payload, err = run_script(out, "--prices", self.tiered())
        self.assertEqual(rc, 0, err)
        every = payload["cost"]["all_repeats"]
        self.assertEqual(every["usd"], 0.7)
        self.assertEqual(every["messages_with_usage"], 3)

    def test_a_skipped_case_sent_no_repeat(self):
        out = self.k3_run()
        self.write_json(out / "cases" / "c-1" / "verdict.json",
                        {"case_id": "c-1", "verdict": "skipped"})
        rc, payload, err = run_script(out, "--prices", self.tiered())
        self.assertEqual(rc, 0, err)
        every = payload["cost"]["all_repeats"]
        self.assertEqual(every["repeats_sent"], 0)
        self.assertEqual(every["status"], "unpriced")


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
        state = tmp / ".evalup"
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

    def test_a_real_holdout_run_is_priced_in_aggregate_only(self):
        """#141 on the runner's own artifacts: the sealed ids come from
        verdicts.jsonl, are confirmed by verdict.json, and stay unsaid."""
        from test_run_cases import FakeApp, make_case, make_plan, otlp, span

        trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        state = tmp / ".evalup"
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
        run_id = "holdout-20260901T120000Z"
        sealed = [f"sealed-{i:04d}" for i in range(5)]
        plan = make_plan(state, app.base_url, run_id=run_id, mode="holdout",
                         selecting_split="holdout",
                         cases=[make_case(c, split=["holdout"])
                                for c in sealed])
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
        agg = payload["cost"]["holdout_aggregate"]
        self.assertEqual(agg["status"], "priced", agg)
        self.assertEqual(agg["tokens"],
                         {"input": 5000, "output": 500, "total": 5500})
        self.assertEqual(agg["usd"], round(5 * (0.0025 + 0.001), 6))
        blob = json.dumps(payload)
        for case_id in sealed:
            self.assertNotIn(case_id, blob)

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
