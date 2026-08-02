#!/usr/bin/env python3
"""Golden-fixture and boundary tests for the agent-eval scorers.

Stdlib only (unittest + subprocess), matching the scorers themselves.
Run: python3 -m unittest discover -s tests -v   (from the plugin root)

Each scorer is exercised as a real subprocess — argv, files, exit codes —
because that is its actual contract with the run skill.
"""
import json
import math
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


def run_script(name, *argv):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / name), *[str(a) for a in argv]],
        capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stderr


class ScorerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write_json(self, name, obj):
        p = self.tmp / name
        p.write_text(json.dumps(obj))
        return p

    def write_jsonl(self, name, rows):
        p = self.tmp / name
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return p


class TestScoreRouting(ScorerTest):
    def test_basic_accuracy_and_confusion(self):
        rows = [
            {"case_id": "c1", "expected": "billing", "observed": "billing"},
            {"case_id": "c2", "expected": "billing", "observed": "support"},
            {"case_id": "c3", "expected": "support", "observed": "support"},
        ]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows))
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(out["accuracy"], 2 / 3, places=3)
        self.assertEqual(out["confusion_matrix"]["billing"]["support"], 1)

    def test_expected_always_acceptable(self):
        # Incoherent label (acceptable omits expected) must not turn a
        # correct route into a diagonal fail: accuracy and F1 stay coherent.
        rows = [{"case_id": "c1", "expected": "billing",
                 "acceptable": ["support"], "observed": "billing"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows))
        self.assertEqual(out["accuracy"], 1.0)
        self.assertEqual(out["per_target"]["billing"]["f1"], 1.0)

    def test_accepted_alternate_excluded_from_matrix(self):
        rows = [{"case_id": "c1", "expected": "billing",
                 "acceptable": ["billing", "support"], "observed": "support"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows))
        self.assertEqual(out["accuracy"], 1.0)
        self.assertEqual(out["accepted_alternates"]["count"], 1)
        self.assertEqual(out["confusion_matrix"], {})

    def test_clarify_path(self):
        rows = [{"case_id": "c1", "expected": "billing", "clarify_ok": True,
                 "observed": "__clarify__", "clarified": True}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows))
        self.assertEqual(out["accuracy"], 1.0)

    def test_empty_input_exits_2(self):
        p = self.tmp / "empty.jsonl"
        p.write_text("")
        rc, out, _ = run_script("score_routing.py", p)
        self.assertEqual(rc, 2)


class TestTrajectoryMatch(ScorerTest):
    def traj(self, *names):
        return self.write_json(
            "t.json", {"tool_calls": [{"name": n, "args": {}} for n in names]})

    def test_subset_pass_with_extras(self):
        rc, out, _ = run_script(
            "trajectory_match.py", self.traj("list", "get", "extra"),
            self.write_json("e.json", {"tools": {"subset": ["list", "get"]}}))
        self.assertEqual(out["verdict"], "pass")

    def test_subset_is_duplicate_aware(self):
        # Expecting two searches, observing one, must fail.
        rc, out, _ = run_script(
            "trajectory_match.py", self.traj("search"),
            self.write_json("e.json",
                            {"tools": {"subset": ["search", "search"]}}))
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual(out["detail"]["missing"], ["search"])

    def test_order_modes(self):
        for mode, names, expected_pass in [
            ("in_order", ("a", "x", "b"), True),
            ("exact", ("a", "x", "b"), False),
            ("exact", ("a", "b"), True),
            ("any_order", ("b", "a"), True),
            ("any_order", ("b", "a", "a"), False),
        ]:
            rc, out, _ = run_script(
                "trajectory_match.py", self.traj(*names),
                self.write_json("e.json", {"tools": {
                    "order": ["a", "b"], "order_mode": mode}}))
            self.assertEqual(out["verdict"],
                             "pass" if expected_pass else "fail",
                             f"mode={mode} names={names}")

    def test_forbidden_overrides_pass(self):
        rc, out, _ = run_script(
            "trajectory_match.py", self.traj("get", "send_email"),
            self.write_json("e.json", {"tools": {
                "subset": ["get"], "forbidden": ["send_email"]}}))
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual(out["forbidden_violations"], ["send_email"])

    def test_precision_none_when_nothing_observed(self):
        rc, out, _ = run_script(
            "trajectory_match.py", self.traj(),
            self.write_json("e.json", {"tools": {"subset": ["get"]}}))
        self.assertIsNone(out["precision"])
        self.assertEqual(out["recall"], 0.0)

    def test_malformed_input_exits_2(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{not json")
        rc, out, _ = run_script("trajectory_match.py", bad, bad)
        self.assertEqual(rc, 2)


class TestDetectLoops(ScorerTest):
    def traj(self, calls):
        return self.write_json("t.json", {"tool_calls": calls})

    def test_identical_repeat_flags_at_threshold(self):
        # Boundary: exactly `threshold` identical calls must flag (>=).
        calls = [{"name": "get", "args": {"id": 1}}] * 2
        rc, out, _ = run_script("detect_loops.py", self.traj(calls),
                                "--repeat-threshold", 2)
        self.assertEqual(out["verdict"], "fail")
        self.assertIn("identical-repeat-loop", out["findings"])

    def test_below_threshold_passes(self):
        calls = [{"name": "get", "args": {"id": 1}}]
        rc, out, _ = run_script("detect_loops.py", self.traj(calls),
                                "--repeat-threshold", 2)
        self.assertEqual(out["verdict"], "pass")

    def test_default_tolerates_one_retry_flags_two(self):
        # Default threshold 3: a single retry (2 identical calls) is normal;
        # a third identical call is flailing.
        one_retry = [{"name": "get", "args": {"id": 1}}] * 2
        rc, out, _ = run_script("detect_loops.py", self.traj(one_retry))
        self.assertEqual(out["verdict"], "pass")
        two_retries = [{"name": "get", "args": {"id": 1}}] * 3
        rc, out, _ = run_script("detect_loops.py", self.traj(two_retries))
        self.assertEqual(out["verdict"], "fail")

    def test_flailing_and_budget(self):
        calls = [{"name": "search", "args": {"q": i}} for i in range(16)]
        rc, out, _ = run_script("detect_loops.py", self.traj(calls))
        self.assertIn("possible-flailing", out["findings"])
        self.assertIn("call-budget-exceeded", out["findings"])


class TestStats(ScorerTest):
    def runs(self, verdicts_base, verdicts_cand):
        base = [{"case_id": f"c{i}", "verdict": v}
                for i, v in enumerate(verdicts_base)]
        cand = [{"case_id": f"c{i}", "verdict": v}
                for i, v in enumerate(verdicts_cand)]
        return (self.write_jsonl("b.jsonl", base),
                self.write_jsonl("c.jsonl", cand))

    def test_exact_posterior_small_n(self):
        # 10 cases: candidate fixes 3, breaks 1. Beta(4,2): P(p>0.5)=0.8125.
        base = ["fail"] * 3 + ["pass"] * 7
        cand = ["pass"] * 3 + ["fail"] * 1 + ["pass"] * 6
        b, c = self.runs(base, cand)
        rc, out, _ = run_script("stats.py", b, c)
        self.assertEqual(out["fixed_by_candidate"], 3)
        self.assertEqual(out["broken_by_candidate"], 1)
        self.assertAlmostEqual(out["p_candidate_better"], 0.8125, places=3)
        self.assertTrue(out["keep"])

    def test_posterior_gate_rejects_weak_evidence(self):
        # b01=4, b10=2 -> exact posterior 0.773 < 0.8 -> no keep
        # (the old mirrored-sampling method wrongly reported ~0.86 here).
        base = ["fail"] * 4 + ["pass"] * 16
        cand = ["pass"] * 4 + ["fail"] * 2 + ["pass"] * 14
        b, c = self.runs(base, cand)
        rc, out, _ = run_script("stats.py", b, c)
        self.assertAlmostEqual(out["p_candidate_better"], 0.773, places=3)
        self.assertFalse(out["keep"])

    def test_large_n_reports_sign_test_alongside_posterior(self):
        base = (["fail"] * 20 + ["pass"] * 80)
        cand = (["pass"] * 20 + ["fail"] * 2 + ["pass"] * 78)
        b, c = self.runs(base, cand)
        rc, out, _ = run_script("stats.py", b, c)
        self.assertTrue(out["keep"])
        # One rule at all n, with the frequentist view reported alongside.
        self.assertGreaterEqual(out["p_candidate_better"], 0.8)
        self.assertLess(out["sign_test_p_one_sided"], 0.05)
        self.assertTrue(out["sign_test_significant_at_alpha"])

    def test_keep_does_not_flip_across_the_n100_boundary(self):
        # Regression: identical discordant counts (b01=6, b10=1) must yield
        # the same decision at n=99 and n=100. The old split gated on a
        # two-sided McNemar test at n>=100 and on the posterior below it, so
        # adding ONE case flipped keep=True -> keep=False on the same effect.
        decisions = {}
        for n in (99, 100):
            base = ["fail"] * 6 + ["pass"] * (n - 6)
            cand = ["pass"] * 6 + ["fail"] * 1 + ["pass"] * (n - 7)
            b, c = self.runs(base, cand)
            rc, out, _ = run_script("stats.py", b, c)
            self.assertEqual(out["fixed_by_candidate"], 6, f"n={n}")
            self.assertEqual(out["broken_by_candidate"], 1, f"n={n}")
            decisions[n] = out["keep"]
        self.assertEqual(decisions[99], decisions[100],
                         f"keep flipped across the n=100 boundary: {decisions}")

    def test_alpha_gates_the_reported_test_not_keep(self):
        # b01=6/b10=1: posterior 0.965 keeps, one-sided sign test p=0.0625
        # is not significant at 0.05. Both facts must be visible, and the
        # gate_note must explain which bar the keep came from.
        base = ["fail"] * 6 + ["pass"] * 44
        cand = ["pass"] * 6 + ["fail"] * 1 + ["pass"] * 43
        b, c = self.runs(base, cand)
        rc, out, _ = run_script("stats.py", b, c)
        self.assertTrue(out["keep"])
        self.assertFalse(out["sign_test_significant_at_alpha"])
        self.assertIn("gate_note", out)
        # Raising the posterior bar must be able to reverse it.
        rc, strict, _ = run_script("stats.py", b, c, "--bayes-threshold", 0.99)
        self.assertFalse(strict["keep"])

    def test_non_binary_verdict_is_rejected(self):
        # 'unscored'/infra rows must never be silently counted as failures.
        for bad in ("unscored", "infra_error", "PASS"):
            rows = [{"case_id": "c1", "verdict": bad}]
            p = self.write_jsonl("bad.jsonl", rows)
            rc, out, _ = run_script("stats.py", p, p)
            self.assertEqual(rc, 2, bad)
            self.assertIn("pass", out["error"])

    def test_duplicate_case_id_rejected(self):
        rows = [{"case_id": "c1", "verdict": "pass"},
                {"case_id": "c1", "verdict": "fail"}]
        p = self.write_jsonl("dup.jsonl", rows)
        rc, out, _ = run_script("stats.py", p, p)
        self.assertEqual(rc, 2)
        self.assertIn("duplicate", out["error"])

    def test_no_common_cases_exits_2(self):
        b = self.write_jsonl("b.jsonl", [{"case_id": "a", "verdict": "pass"}])
        c = self.write_jsonl("c.jsonl", [{"case_id": "z", "verdict": "pass"}])
        rc, out, _ = run_script("stats.py", b, c)
        self.assertEqual(rc, 2)


class TestNormalizeTrace(ScorerTest):
    @staticmethod
    def span(trace_id, span_id, op, parent=None, extra_attrs=(), start=1):
        attrs = [{"key": "gen_ai.operation.name",
                  "value": {"stringValue": op}}] + list(extra_attrs)
        s = {"traceId": trace_id, "spanId": span_id,
             "startTimeUnixNano": str(start * 1_000_000),
             "endTimeUnixNano": str(start * 1_000_000 + 500_000),
             "attributes": attrs}
        if parent:
            s["parentSpanId"] = parent
        return s

    def doc(self, spans):
        return self.write_json("spans.json", {"resourceSpans": [
            {"scopeSpans": [{"spans": spans}]}]})

    def test_intact_trace_without_content_capture_is_ok(self):
        # Transport-complete trace, no result content: must be "ok" so the
        # trajectory layers (which only need names) can score.
        spans = [
            self.span("t1", "a", "invoke_agent", extra_attrs=[
                {"key": "gen_ai.agent.name",
                 "value": {"stringValue": "router"}}]),
            self.span("t1", "b", "execute_tool", parent="a", start=2,
                      extra_attrs=[
                          {"key": "gen_ai.tool.name",
                           "value": {"stringValue": "get_invoice"}},
                          {"key": "gen_ai.tool.call.id",
                           "value": {"stringValue": "call_1"}}]),
        ]
        rc, out, _ = run_script("normalize_trace.py", self.doc(spans),
                                "--trace-id", "t1")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["checks"]["tool_calls_without_result"],
                         ["call_1"])
        self.assertEqual(out["trajectory"]["tool_calls"][0]["name"],
                         "get_invoice")

    def test_orphaned_parent_is_incomplete(self):
        spans = [self.span("t1", "b", "execute_tool", parent="missing",
                           extra_attrs=[{"key": "gen_ai.tool.name",
                                         "value": {"stringValue": "x"}}])]
        rc, out, _ = run_script("normalize_trace.py", self.doc(spans),
                                "--trace-id", "t1")
        self.assertEqual(out["status"], "incomplete")

    def test_no_spans_for_trace_is_incomplete(self):
        spans = [self.span("other", "a", "chat")]
        rc, out, _ = run_script("normalize_trace.py", self.doc(spans),
                                "--trace-id", "t1")
        self.assertEqual(out["status"], "incomplete")

    def test_falsy_attribute_values_survive(self):
        spans = [self.span("t1", "a", "chat", extra_attrs=[
            {"key": "gen_ai.request.model", "value": {"stringValue": "m"}},
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": 0}},
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": 7}}])]
        rc, out, _ = run_script("normalize_trace.py", self.doc(spans),
                                "--trace-id", "t1")
        self.assertEqual(out["trajectory"]["usage"],
                         {"input_tokens": 0, "output_tokens": 7})

    def test_result_content_is_captured(self):
        spans = [self.span("t1", "b", "execute_tool", extra_attrs=[
            {"key": "gen_ai.tool.name", "value": {"stringValue": "get"}},
            {"key": "gen_ai.tool.call.id", "value": {"stringValue": "c1"}},
            {"key": "gen_ai.tool.call.arguments",
             "value": {"stringValue": "{\"id\": \"INV-9\"}"}},
            {"key": "gen_ai.tool.call.result",
             "value": {"stringValue": "{\"total\": 42}"}}])]
        rc, out, _ = run_script("normalize_trace.py", self.doc(spans),
                                "--trace-id", "t1")
        call = out["trajectory"]["tool_calls"][0]
        self.assertEqual(call["args"], {"id": "INV-9"})
        self.assertEqual(call["result"], "{\"total\": 42}")
        self.assertEqual(out["status"], "ok")


class TestScoreArgs(ScorerTest):
    def test_value_present_and_hallucinated(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "list_invoices", "args": {"user": "u1"},
             "result": "invoices: INV-1, INV-2"},
            {"name": "get_invoice", "args": {"invoice_id": "INV-1"}},
        ]})
        expect = self.write_json("e.json", {"args": {
            "list_invoices": {"user": "u1"},
            "get_invoice": {"invoice_id": "from_tool_result"}}})
        rc, out, _ = run_script("score_args.py", traj, expect)
        self.assertEqual(out["verdict"], "pass")

    def test_hallucinated_arg_fails(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "list_invoices", "args": {}, "result": "INV-1 only"},
            {"name": "get_invoice", "args": {"invoice_id": "INV-99"}},
        ]})
        expect = self.write_json("e.json", {"args": {
            "get_invoice": {"invoice_id": "from_tool_result"}}})
        rc, out, _ = run_script("score_args.py", traj, expect)
        self.assertEqual(out["verdict"], "fail")

    def test_no_content_capture_is_unscorable_not_fail(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "list_invoices", "args": None, "result": None},
            {"name": "get_invoice", "args": {"invoice_id": "INV-1"},
             "result": None},
        ]})
        expect = self.write_json("e.json", {"args": {
            "get_invoice": {"invoice_id": "from_tool_result"}}})
        rc, out, _ = run_script("score_args.py", traj, expect)
        self.assertEqual(out["verdict"], "unscored")
        self.assertEqual(out["unscorable"], 1)

    def test_tool_not_called_is_not_an_args_fail(self):
        traj = self.write_json("t.json", {"tool_calls": []})
        expect = self.write_json("e.json", {"args": {
            "get_invoice": {"invoice_id": "present"}}})
        rc, out, _ = run_script("score_args.py", traj, expect)
        self.assertEqual(out["verdict"], "unscored")
        self.assertEqual(out["checks"][0]["status"], "tool_not_called")

    def test_empty_present_fails(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "get_invoice", "args": {"invoice_id": ""}}]})
        expect = self.write_json("e.json", {"args": {
            "get_invoice": {"invoice_id": "present"}}})
        rc, out, _ = run_script("score_args.py", traj, expect)
        self.assertEqual(out["verdict"], "fail")


class TestLoaderContract(ScorerTest):
    """Scorers accept normalize_trace.py output directly and reject wrong
    shapes loudly — a missing tool_calls key must never score as an empty
    trace."""

    def test_wrapped_normalize_output_accepted_by_detect_loops(self):
        wrapped = {"status": "ok", "trajectory": {
            "tool_calls": [{"name": "get", "args": {"id": 1}}] * 3}}
        rc, out, _ = run_script("detect_loops.py",
                                self.write_json("t.json", wrapped))
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual(out["total_calls"], 3)

    def test_wrapped_normalize_output_accepted_by_score_args(self):
        wrapped = {"status": "ok", "trajectory": {"tool_calls": [
            {"name": "get_invoice", "args": {"invoice_id": "INV-9"}}]}}
        rc, out, _ = run_script(
            "score_args.py", self.write_json("t.json", wrapped),
            self.write_json("e.json", {"args": {
                "get_invoice": {"invoice_id": "present"}}}))
        self.assertEqual(out["verdict"], "pass")

    def test_wrong_shape_exits_2(self):
        p = self.tmp / "bad.json"
        p.write_text("[]")
        for script, argv in (("detect_loops.py", [p]),
                             ("trajectory_match.py", [p, p]),
                             ("score_args.py", [p, p])):
            rc, out, _ = run_script(script, *argv)
            self.assertEqual(rc, 2, script)
            self.assertIn("tool_calls", out["error"])


class TestScoreArgsProvenance(ScorerTest):
    def test_substring_of_longer_token_fails(self):
        # INV-1 hallucinated; only INV-10 exists — substring must not pass.
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "list_invoices", "args": {}, "result": "found INV-10"},
            {"name": "get_invoice", "args": {"invoice_id": "INV-1"}}]})
        expect = self.write_json("e.json", {"args": {
            "get_invoice": {"invoice_id": "from_tool_result"}}})
        rc, out, _ = run_script("score_args.py", traj, expect)
        self.assertEqual(out["verdict"], "fail")

    def test_first_call_is_fail_not_unscorable(self):
        # Nothing precedes the first call: provenance is impossible, and
        # that is a fail even with content capture on.
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "get_invoice", "args": {"invoice_id": "INV-9"},
             "result": "total: 42"}]})
        expect = self.write_json("e.json", {"args": {
            "get_invoice": {"invoice_id": "from_tool_result"}}})
        rc, out, _ = run_script("score_args.py", traj, expect)
        self.assertEqual(out["verdict"], "fail")

    def test_short_value_is_unscorable(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "list_invoices", "args": {}, "result": "pages: 1 2 3"},
            {"name": "get_invoice", "args": {"invoice_id": 1}}]})
        expect = self.write_json("e.json", {"args": {
            "get_invoice": {"invoice_id": "from_tool_result"}}})
        rc, out, _ = run_script("score_args.py", traj, expect)
        self.assertEqual(out["verdict"], "unscored")
        self.assertEqual(out["unscorable"], 1)

    def test_non_string_result_is_searchable(self):
        # A structured (dict) tool result still counts as provenance.
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "list_invoices", "args": {},
             "result": {"invoice_id": "INV-7"}},
            {"name": "get_invoice", "args": {"invoice_id": "INV-7"}}]})
        expect = self.write_json("e.json", {"args": {
            "get_invoice": {"invoice_id": "from_tool_result"}}})
        rc, out, _ = run_script("score_args.py", traj, expect)
        self.assertEqual(out["verdict"], "pass")


class TestScoreRoutingRobustness(ScorerTest):
    def test_null_observed_is_scored_not_crash(self):
        rows = [{"case_id": "c1", "expected": "billing", "observed": None}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows))
        self.assertEqual(rc, 0)
        self.assertEqual(out["accuracy"], 0.0)
        self.assertEqual(out["confusion_matrix"]["billing"]["__no_route__"], 1)

    def test_duplicate_case_id_rejected(self):
        rows = [{"case_id": "c1", "expected": "a", "observed": "a"},
                {"case_id": "c1", "expected": "a", "observed": "b"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows))
        self.assertEqual(rc, 2)
        self.assertIn("duplicate", out["error"])

    def test_case_format_field_names_accepted(self):
        rows = [{"case_id": "c1", "route": "billing",
                 "route_acceptable": ["support"], "observed": "support"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows))
        self.assertEqual(out["accuracy"], 1.0)
        self.assertEqual(out["accepted_alternates"]["count"], 1)

    def test_no_phantom_confusion_rows(self):
        # 'support' appears only as observed: it must not gain an
        # expected-row, and real rows must not gain zero-count cells.
        rows = [{"case_id": "c1", "expected": "billing", "observed": "support"},
                {"case_id": "c2", "expected": "billing", "observed": "billing"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows))
        self.assertNotIn("support", out["confusion_matrix"])
        self.assertEqual(out["confusion_matrix"]["billing"],
                         {"support": 1, "billing": 1})

    def test_oos_route_flag_maps_to_sentinel(self):
        rows = [{"case_id": "c1", "expected": "refuse", "observed": "refuse"},
                {"case_id": "c2", "expected": "billing", "observed": "billing"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows),
                                "--oos-route", "refuse")
        self.assertIn("oos", out)
        self.assertEqual(out["oos"]["recall"], 1.0)

    def test_missing_expected_exits_2(self):
        rows = [{"case_id": "c1", "observed": "billing"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows))
        self.assertEqual(rc, 2)
        self.assertIn("expected", out["error"])


class TestTrajectoryMatchPrecedence(ScorerTest):
    def test_metrics_follow_order_when_both_given(self):
        # order wins for the verdict AND the metrics — they must agree.
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "a", "args": {}}, {"name": "b", "args": {}}]})
        expect = self.write_json("e.json", {"tools": {
            "order": ["a", "b"], "subset": ["x", "y", "z"]}})
        rc, out, _ = run_script("trajectory_match.py", traj, expect)
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["precision"], 1.0)
        self.assertEqual(out["recall"], 1.0)


class TestNormalizeTraceValues(ScorerTest):
    span = staticmethod(TestNormalizeTrace.span)

    def doc(self, spans):
        return self.write_json("spans.json", {"resourceSpans": [
            {"scopeSpans": [{"spans": spans}]}]})

    def test_missing_end_time_yields_null_duration(self):
        s = self.span("t1", "a", "chat")
        del s["endTimeUnixNano"]
        rc, out, _ = run_script("normalize_trace.py", self.doc([s]),
                                "--trace-id", "t1")
        self.assertIsNone(out["trajectory"]["llm_calls"][0]["duration_ms"])
        self.assertIn("a", out["checks"]["spans_missing_duration"])

    def test_array_result_is_captured(self):
        s = self.span("t1", "b", "execute_tool", extra_attrs=[
            {"key": "gen_ai.tool.name", "value": {"stringValue": "search"}},
            {"key": "gen_ai.tool.call.id", "value": {"stringValue": "c1"}},
            {"key": "gen_ai.tool.call.result",
             "value": {"arrayValue": {"values": [
                 {"stringValue": "chunk1"}, {"stringValue": "chunk2"}]}}}])
        rc, out, _ = run_script("normalize_trace.py", self.doc([s]),
                                "--trace-id", "t1")
        call = out["trajectory"]["tool_calls"][0]
        self.assertEqual(call["result"], ["chunk1", "chunk2"])
        self.assertEqual(out["checks"]["tool_calls_without_result"], [])

    def test_tokens_rolled_up_per_agent_stage(self):
        usage = lambda i, o: [
            {"key": "gen_ai.request.model", "value": {"stringValue": "m"}},
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": i}},
            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": o}}]
        spans = [
            self.span("t1", "a", "invoke_agent", extra_attrs=[
                {"key": "gen_ai.agent.name",
                 "value": {"stringValue": "router"}}]),
            self.span("t1", "b", "chat", parent="a", start=2,
                      extra_attrs=usage(11, 7)),
            self.span("t1", "c", "chat", parent="a", start=3,
                      extra_attrs=usage(5, 3)),
        ]
        rc, out, _ = run_script("normalize_trace.py", self.doc(spans),
                                "--trace-id", "t1")
        traj = out["trajectory"]
        self.assertEqual(traj["agents"][0]["usage"],
                         {"input_tokens": 16, "output_tokens": 10})
        self.assertEqual(traj["llm_calls"][0]["usage"],
                         {"input_tokens": 11, "output_tokens": 7})
        self.assertEqual(traj["usage"],
                         {"input_tokens": 16, "output_tokens": 10})


class TestScoreAnswer(ScorerTest):
    def answer(self, text):
        p = self.tmp / "a.txt"
        p.write_text(text)
        return p

    def test_must_contain_and_not_contain_pass(self):
        rc, out, _ = run_script(
            "score_answer.py", self.answer("Invoice INV-9 is due."),
            self.write_json("e.json", {"answer": {
                "must_contain": ["INV-9"], "must_not_contain": ["$"]}}))
        self.assertEqual(out["verdict"], "pass")

    def test_must_not_contain_violation_fails(self):
        rc, out, _ = run_script(
            "score_answer.py", self.answer("That costs $5."),
            self.write_json("e.json", {"answer": {
                "must_not_contain": ["$"]}}))
        self.assertEqual(out["verdict"], "fail")

    def test_regex_entry(self):
        rc, out, _ = run_script(
            "score_answer.py", self.answer("ref: INV-1234"),
            self.write_json("e.json", {"answer": {
                "must_contain": ["/INV-\\d{4}/"]}}))
        self.assertEqual(out["verdict"], "pass")

    def test_json_schema_minimal(self):
        schema = {"type": "object", "required": ["total"],
                  "properties": {"total": {"type": "number"}}}
        rc, out, _ = run_script(
            "score_answer.py", self.answer('{"total": 42}'),
            self.write_json("e.json", {"format": {"json_schema": schema}}))
        self.assertEqual(out["verdict"], "pass")
        rc, out, _ = run_script(
            "score_answer.py", self.answer('{"amount": "x"}'),
            self.write_json("e2.json", {"format": {"json_schema": schema}}))
        self.assertEqual(out["verdict"], "fail")

    def test_non_json_answer_fails_schema(self):
        rc, out, _ = run_script(
            "score_answer.py", self.answer("plain text"),
            self.write_json("e.json", {"format": {"json_schema":
                                                  {"type": "object"}}}))
        self.assertEqual(out["verdict"], "fail")

    def test_nothing_scorable_is_unscored(self):
        rc, out, _ = run_script(
            "score_answer.py", self.answer("hi"),
            self.write_json("e.json", {"answer": {}}))
        self.assertEqual(out["verdict"], "unscored")


class TestErrorContract(ScorerTest):
    """Every scorer promises: exit 0 with a verdict, or exit 2 with a JSON
    {"error": ...} on stdout. Malformed input must never produce a traceback
    — the run skill reads a crash as an infra failure, not a data error."""

    def assert_clean_error(self, script, *argv):
        rc, out, err = run_script(script, *argv)
        self.assertEqual(rc, 2, f"{script}: expected exit 2, got {rc}\n{err}")
        self.assertIsNotNone(out, f"{script}: stdout was not JSON\n{err}")
        self.assertIn("error", out, f"{script}: no error key\n{err}")
        self.assertNotIn("Traceback", err, f"{script}: crashed\n{err}")
        return out

    def test_non_dict_jsonl_row(self):
        p = self.tmp / "rows.jsonl"
        p.write_text("[1, 2]\n")
        self.assert_clean_error("score_routing.py", p)
        self.assert_clean_error("stats.py", p, p)

    def test_stats_row_missing_required_key(self):
        p = self.write_jsonl("b.jsonl", [{"case_id": "c1"}])
        out = self.assert_clean_error("stats.py", p, p)
        self.assertIn("verdict", out["error"])
        p2 = self.write_jsonl("b2.jsonl", [{"verdict": "pass"}])
        self.assert_clean_error("stats.py", p2, p2)

    def test_unknown_order_mode_is_loud(self):
        traj = self.write_json("t.json", {"tool_calls": [{"name": "a"}]})
        expect = self.write_json("e.json", {"tools": {
            "order": ["a"], "order_mode": "in-order"}})
        out = self.assert_clean_error("trajectory_match.py", traj, expect)
        self.assertIn("order_mode", out["error"])

    def test_valid_order_modes_still_accepted(self):
        traj = self.write_json("t.json", {"tool_calls": [{"name": "a"}]})
        for mode in ("in_order", "exact", "any_order"):
            rc, out, _ = run_script(
                "trajectory_match.py", traj,
                self.write_json("e.json",
                                {"tools": {"order": ["a"],
                                           "order_mode": mode}}))
            self.assertEqual(rc, 0, mode)
            self.assertEqual(out["verdict"], "pass", mode)


class TestScoreAnswerRobustness(ScorerTest):
    def answer(self, text):
        p = self.tmp / "a.txt"
        p.write_text(text)
        return p

    def test_unquoted_number_entry_is_coerced_not_crash(self):
        rc, out, err = run_script(
            "score_answer.py", self.answer("your total is 42 dollars"),
            self.write_json("e.json", {"answer": {"must_contain": [42]}}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")

    def test_bool_entry_is_unscorable_not_silent_mismatch(self):
        # YAML `true` -> Python True -> str 'True', which is NOT the token an
        # answer would contain. Report it rather than quietly failing.
        rc, out, err = run_script(
            "score_answer.py", self.answer("the value is true"),
            self.write_json("e.json", {"answer": {"must_contain": [True]}}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "unscored")
        self.assertEqual(out["unscorable"], 1)

    def test_invalid_regex_is_unscorable(self):
        rc, out, _ = run_script(
            "score_answer.py", self.answer("anything"),
            self.write_json("e.json", {"answer": {"must_contain": ["/[a-/"]}}))
        self.assertEqual(out["verdict"], "unscored")
        self.assertEqual(out["checks"][0]["reason"], "invalid regex")

    def test_fenced_json_validates_with_a_note(self):
        schema = {"type": "object", "required": ["total"],
                  "properties": {"total": {"type": "number"}}}
        rc, out, _ = run_script(
            "score_answer.py",
            self.answer('Here you go:\n```json\n{"total": 42}\n```\n'),
            self.write_json("e.json", {"format": {"json_schema": schema}}))
        self.assertEqual(out["verdict"], "pass")
        self.assertIn("code fence", out["checks"][0]["note"])

    def test_fenced_json_still_validated_against_schema(self):
        schema = {"type": "object", "required": ["total"]}
        rc, out, _ = run_script(
            "score_answer.py",
            self.answer('```json\n{"amount": 42}\n```'),
            self.write_json("e.json", {"format": {"json_schema": schema}}))
        self.assertEqual(out["verdict"], "fail")

    def test_prose_without_json_still_fails(self):
        rc, out, _ = run_script(
            "score_answer.py", self.answer("no json here at all"),
            self.write_json("e.json", {"format": {"json_schema":
                                                  {"type": "object"}}}))
        self.assertEqual(out["verdict"], "fail")


class TestNormalizeTraceRobustness(ScorerTest):
    span = staticmethod(TestNormalizeTrace.span)

    def doc(self, spans):
        return self.write_json("spans.json", {"resourceSpans": [
            {"scopeSpans": [{"spans": spans}]}]})

    def test_malformed_timestamp_does_not_crash(self):
        # Damaged transport must degrade to a null duration + a check entry,
        # not a ValueError from the sort key.
        s = self.span("t1", "a", "chat")
        s["startTimeUnixNano"] = "not-a-number"
        rc, out, err = run_script("normalize_trace.py", self.doc([s]),
                                  "--trace-id", "t1")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out["status"], "ok")
        self.assertIsNone(out["trajectory"]["llm_calls"][0]["duration_ms"])
        self.assertIn("a", out["checks"]["spans_missing_duration"])

    def test_malformed_timestamp_does_not_reorder_valid_spans(self):
        spans = [self.span("t1", "good", "chat", start=5),
                 self.span("t1", "bad", "chat", start=1)]
        spans[1]["startTimeUnixNano"] = "xyz"
        rc, out, err = run_script("normalize_trace.py", self.doc(spans),
                                  "--trace-id", "t1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(out["trajectory"]["llm_calls"]), 2)

    def test_capture_flags_distinguish_args_from_results(self):
        s = self.span("t1", "b", "execute_tool", extra_attrs=[
            {"key": "gen_ai.tool.name", "value": {"stringValue": "get"}},
            {"key": "gen_ai.tool.call.id", "value": {"stringValue": "c1"}},
            {"key": "gen_ai.tool.call.arguments",
             "value": {"stringValue": "{\"id\": \"INV-9\"}"}}])
        rc, out, _ = run_script("normalize_trace.py", self.doc([s]),
                                "--trace-id", "t1")
        # Arguments captured, results not — from_tool_result provenance
        # depends on the result side, so the two must not be one flag.
        self.assertTrue(out["checks"]["arg_capture_seen"])
        self.assertFalse(out["checks"]["result_capture_seen"])


class TestDetectLoopsSignals(ScorerTest):
    def traj(self, calls):
        return self.write_json("t.json", {"tool_calls": calls})

    def test_repeat_and_flailing_report_independently(self):
        # 3 identical + 12 distinct calls of one tool: previously the repeat
        # finding suppressed the flailing one and that signal was lost.
        calls = ([{"name": "search", "args": {"q": "x"}}] * 3 +
                 [{"name": "search", "args": {"q": i}} for i in range(12)])
        rc, out, _ = run_script("detect_loops.py", self.traj(calls))
        self.assertIn("identical-repeat-loop", out["findings"])
        self.assertIn("possible-flailing", out["findings"])
        self.assertEqual(out["heavy_tools"][0]["distinct_arg_sets"], 13)

    def test_pure_repeat_loop_is_not_called_flailing(self):
        # One arg set repeated is a repeat loop, not flailing.
        calls = [{"name": "get", "args": {"id": 1}}] * 8
        rc, out, _ = run_script("detect_loops.py", self.traj(calls))
        self.assertIn("identical-repeat-loop", out["findings"])
        self.assertNotIn("possible-flailing", out["findings"])


SCORERS = ("normalize_trace.py", "score_routing.py", "trajectory_match.py",
           "score_authz.py",
           "score_args.py", "score_answer.py", "detect_loops.py", "stats.py",
           "score_execution.py", "reduce_repeats.py")


class TestHarnessVersion(ScorerTest):
    """run/SKILL.md refuses a baseline diff unless the harness versions match,
    so the version has to be obtainable FROM the harness — otherwise the
    orchestrating skill invents one and the comparability rule is decorative."""

    def test_every_scorer_reports_the_same_version(self):
        seen = set()
        for name in SCORERS:
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / name), "--version"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{name}: {proc.stderr}")
            seen.add((proc.stdout + proc.stderr).strip())
        self.assertEqual(len(seen), 1, f"scorers disagree on version: {seen}")

    def test_version_matches_the_plugin_manifest(self):
        manifest = json.loads(
            (SCRIPTS.parent / ".claude-plugin" / "plugin.json").read_text())
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "stats.py"), "--version"],
            capture_output=True, text=True)
        self.assertIn(manifest["version"], proc.stdout + proc.stderr,
                      "HARNESS_VERSION drifted from plugin.json")


class TestErrorContractAllScorers(ScorerTest):
    """The exit-0-verdict / exit-2-JSON contract belongs to EVERY scorer.
    normalize_trace.py was the one omitted here, and that is exactly where an
    unguarded shape probe crashed with a traceback."""

    def bad_argv(self, script, path):
        if script == "normalize_trace.py":
            return [path, "--trace-id", "t1"]
        if script in ("trajectory_match.py", "score_args.py",
                      "score_answer.py", "stats.py", "score_execution.py",
                      "score_authz.py"):
            return [path, path]
        return [path]

    def test_all_scorers_reject_garbage_without_a_traceback(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{not json")
        for script in SCORERS:
            rc, out, err = run_script(script, *self.bad_argv(script, bad))
            self.assertEqual(rc, 2, f"{script}: expected exit 2, got {rc}\n{err}")
            self.assertIsNotNone(out, f"{script}: stdout not JSON\n{err}")
            self.assertIn("error", out, f"{script}: no error key\n{err}")
            self.assertNotIn("Traceback", err, f"{script}: crashed\n{err}")

    def test_normalize_trace_survives_a_list_of_scalars(self):
        # Regression: flatten_otlp probed doc[0] for "resourceSpans" without
        # checking dict-ness -> AttributeError on strings, TypeError on ints.
        for payload in ('["a","b"]', "[1,2]", '"just a string"', "42"):
            p = self.tmp / "s.json"
            p.write_text(payload)
            rc, out, err = run_script("normalize_trace.py", p,
                                      "--trace-id", "t1")
            self.assertEqual(rc, 2, f"{payload}: got rc={rc}\n{err}")
            self.assertNotIn("Traceback", err, payload)
            self.assertIn("error", out, payload)


class TestNormalizeTraceRootSpans(ScorerTest):
    span = staticmethod(TestNormalizeTrace.span)

    def doc(self, spans):
        return self.write_json("spans.json", {"resourceSpans": [
            {"scopeSpans": [{"spans": spans}]}]})

    def status_with_parent(self, parent):
        s = self.span("t1", "aaaa", "chat")
        if parent is not None:
            s["parentSpanId"] = parent
        rc, out, err = run_script("normalize_trace.py", self.doc([s]),
                                  "--trace-id", "t1")
        self.assertEqual(rc, 0, err)
        return out["status"]

    def test_zero_parent_ids_are_roots_not_orphans(self):
        # Spec-compliant OTLP omits parentSpanId on a root, but non-conformant
        # SDKs and adapter mapping_shims emit an all-zero id. Reading those as
        # a dangling parent marks an intact trace incomplete, which scores
        # every case INFRA_INCOMPLETE and blames the app's tracing.
        for parent in (None, "", "0000000000000000", "AAAAAAAAAAA="):
            self.assertEqual(self.status_with_parent(parent), "ok",
                             f"parentSpanId={parent!r}")

    def test_a_genuinely_dangling_parent_is_still_incomplete(self):
        self.assertEqual(self.status_with_parent("beef"), "incomplete")

    def test_hex_id_of_zeros_and_letters_is_not_mistaken_for_absent(self):
        # a0a0... is a legitimate id; a strip("0") style test would erase it.
        self.assertEqual(self.status_with_parent("a0a0a0a0a0a0a0a0"),
                         "incomplete")


class TestScoreRoutingOOSReporting(ScorerTest):
    def test_oos_block_present_even_when_not_measurable(self):
        # Regression: OOS cases passing via the acceptable set left per_target,
        # so the oos block vanished — indistinguishable from "no leakage".
        rows = [{"case_id": "c1", "expected": "refuse",
                 "acceptable": ["refuse", "billing"], "observed": "billing"},
                {"case_id": "c2", "expected": "billing", "observed": "billing"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows),
                                "--oos-route", "refuse")
        self.assertIn("oos", out)
        self.assertIsNone(out["oos"]["recall"])
        self.assertIn("not measurable", out["oos"]["note"])

    def test_oos_block_absent_without_the_flag(self):
        rows = [{"case_id": "c1", "expected": "billing", "observed": "billing"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows))
        self.assertNotIn("oos", out)

    def test_oos_recall_reports_the_cases_it_did_not_measure(self):
        # The field-test shape: two OOS cases route correctly, a third lands in
        # a route target but its acceptable set blessed that. Recall over the
        # measured two is a legitimate 1.0 — but reporting it alone reads as
        # "nothing leaked" when one OOS query did reach a route target. Rate
        # without coverage is the misreading; report both.
        rows = [{"case_id": "c1", "expected": "refuse", "observed": "refuse"},
                {"case_id": "c2", "expected": "refuse", "observed": "refuse"},
                {"case_id": "c3", "expected": "refuse",
                 "acceptable": ["refuse", "billing"], "observed": "billing"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows),
                                "--oos-route", "refuse")
        self.assertEqual(out["oos"]["recall"], 1.0)
        self.assertEqual(out["oos"]["support"], 2)
        self.assertEqual(out["oos"]["oos_cases"], 3)
        self.assertEqual(out["oos"]["excluded_accepted_alternates"], 1)
        self.assertIn("2 of 3", out["oos"]["note"])

    def test_oos_note_stays_clean_when_nothing_was_excluded(self):
        rows = [{"case_id": "c1", "expected": "refuse", "observed": "refuse"},
                {"case_id": "c2", "expected": "refuse", "observed": "billing"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows),
                                "--oos-route", "refuse")
        self.assertEqual(out["oos"]["excluded_accepted_alternates"], 0)
        self.assertNotIn("not measured here", out["oos"]["note"])
        self.assertEqual(out["oos"]["recall"], 0.5)

    def test_missing_case_id_names_the_real_problem(self):
        rows = [{"expected": "a", "observed": "a"},
                {"expected": "b", "observed": "b"}]
        rc, out, _ = run_script("score_routing.py",
                                self.write_jsonl("r.jsonl", rows))
        self.assertEqual(rc, 2)
        self.assertIn("case_id", out["error"])
        self.assertNotIn("duplicate", out["error"])


class TestScoreAnswerRegexBudget(ScorerTest):
    def test_catastrophic_backtracking_is_unscorable_not_a_hang(self):
        p = self.tmp / "a.txt"
        p.write_text("a" * 40 + "b")
        rc, out, err = run_script(
            "score_answer.py", p,
            self.write_json("e.json", {"answer": {
                "must_contain": ["/(a+)+$/"]}}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "unscored")
        self.assertEqual(out["unscorable"], 1)
        self.assertIn("backtracking", out["checks"][0]["reason"])


class TestStatsOneSidedZ(ScorerTest):
    def test_mde_uses_the_one_sided_z_of_the_test_it_reports(self):
        # n=10, no discordant pairs -> p_disc floors at 1/n, so
        # MDE = z_sum(0.05, 0.8) * sqrt(0.1/10) = z * 0.1.
        # one-sided 2.4865 -> 0.249;  two-sided 2.8016 -> 0.280 (the old bug).
        rows = [{"case_id": f"c{i}", "verdict": "pass"} for i in range(10)]
        p = self.write_jsonl("b.jsonl", rows)
        rc, out, err = run_script("stats.py", p, p)
        self.assertEqual(rc, 0, err)
        self.assertAlmostEqual(out["min_detectable_effect_at_this_n"],
                               0.249, places=3)


class TestStatsMcNemarAndMDE(ScorerTest):
    def runs(self, verdicts_base, verdicts_cand):
        base = [{"case_id": f"c{i}", "verdict": v}
                for i, v in enumerate(verdicts_base)]
        cand = [{"case_id": f"c{i}", "verdict": v}
                for i, v in enumerate(verdicts_cand)]
        return (self.write_jsonl("b.jsonl", base),
                self.write_jsonl("c.jsonl", cand))

    def test_mcnemar_two_sided_is_named_and_doubles_the_one_sided_tail(self):
        # Same fixture as test_alpha_gates_the_reported_test_not_keep:
        # b01=6, b10=1 -> one-sided sign test p=0.0625, so the two-sided
        # McNemar exact test must be exactly 2x that (both read the same
        # Binomial(7, 0.5) tail, one-sided vs two-sided).
        base = ["fail"] * 6 + ["pass"] * 44
        cand = ["pass"] * 6 + ["fail"] * 1 + ["pass"] * 43
        b, c = self.runs(base, cand)
        rc, out, _ = run_script("stats.py", b, c)
        self.assertAlmostEqual(out["mcnemar_exact_p_two_sided"], 0.125,
                               places=5)
        self.assertAlmostEqual(out["mcnemar_exact_p_two_sided"],
                               2 * out["sign_test_p_one_sided"], places=5)
        self.assertFalse(out["mcnemar_significant_at_alpha"])
        self.assertIn("mcnemar_note", out)
        self.assertIn("chi-square", out["mcnemar_note"])

    def test_mcnemar_caps_at_one_when_symmetric(self):
        # b01=2, b10=2: no directional evidence either way, two-sided exact
        # p must saturate at 1.0, not overflow past it.
        base = ["fail"] * 2 + ["pass"] * 8
        cand = ["pass"] * 2 + ["fail"] * 2 + ["pass"] * 6
        b, c = self.runs(base, cand)
        rc, out, _ = run_script("stats.py", b, c)
        self.assertEqual(out["fixed_by_candidate"], 2)
        self.assertEqual(out["broken_by_candidate"], 2)
        self.assertEqual(out["mcnemar_exact_p_two_sided"], 1.0)

    def test_mde_reported_in_plain_language(self):
        rows = [{"case_id": f"c{i}", "verdict": "pass"} for i in range(10)]
        p = self.write_jsonl("b.jsonl", rows)
        rc, out, _ = run_script("stats.py", p, p)
        self.assertIn("min_detectable_effect_explained", out)
        explained = out["min_detectable_effect_explained"]
        self.assertIn("n=10", explained)
        self.assertIn("%", explained)


class TestScoreExecution(ScorerTest):
    """Execution-accuracy: grade the result set, not the prose around it."""

    def score(self, actual, expect):
        return run_script("score_execution.py",
                          self.write_json("a.json", actual),
                          self.write_json("e.json", expect))

    def test_scalar_exact_match(self):
        rc, out, _ = self.score({"scalar": 7},
                                {"result": {"scalar": 7}})
        self.assertEqual(out["verdict"], "pass")

    def test_licences_stub_scenario_fails(self):
        # The flagship case: a stub tool returns [] and the bot confidently
        # says "0". A prose scorer waves it through; execution accuracy does
        # not — ground truth is 3 expiring.
        rc, out, _ = self.score({"scalar": 0},
                                {"result": {"scalar": 3}})
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual(out["checks"][0]["expected"], 3)
        self.assertEqual(out["checks"][0]["actual"], 0)

    def test_scalar_type_aware_matches(self):
        # 7 == "7" == "7.0"; "1,200" == 1200; "$5" == 5; "45%" == 45.
        for expected, actual in [(7, "7"), (7, "7.0"), (1200, "1,200"),
                                 (5, "$5"), (45, "45%"), (1200, "1200 hrs")]:
            rc, out, _ = self.score({"scalar": actual},
                                    {"result": {"scalar": expected}})
            self.assertEqual(out["verdict"], "pass",
                             f"{expected!r} vs {actual!r}")

    def test_string_scalar_is_case_insensitive(self):
        rc, out, _ = self.score({"scalar": "night"},
                                {"result": {"scalar": "Night"}})
        self.assertEqual(out["verdict"], "pass")

    def test_bool_is_not_coerced_to_number(self):
        # YAML/JSON true must not compare equal to 1 and silently pass.
        rc, out, _ = self.score({"scalar": True},
                                {"result": {"scalar": 1}})
        self.assertEqual(out["verdict"], "fail")

    def test_float_tolerance(self):
        rc, out, _ = self.score(
            {"scalar": 3.14},
            {"result": {"scalar": 3.1, "float_tolerance": 0.05}})
        self.assertEqual(out["verdict"], "pass")
        rc, out, _ = self.score(
            {"scalar": 3.14},
            {"result": {"scalar": 3.1, "float_tolerance": 0.01}})
        self.assertEqual(out["verdict"], "fail")

    def test_rows_bag_equal_ignores_order(self):
        expected = [{"role": "nurse", "n": 7}, {"role": "doctor", "n": 2}]
        actual = [{"role": "doctor", "n": 2}, {"role": "nurse", "n": 7}]
        rc, out, _ = self.score({"rows": actual},
                                {"result": {"rows": expected}})
        self.assertEqual(out["verdict"], "pass")

    def test_rows_ordered_fails_when_reordered(self):
        expected = [{"role": "nurse"}, {"role": "doctor"}]
        actual = [{"role": "doctor"}, {"role": "nurse"}]
        rc, out, _ = self.score(
            {"rows": actual}, {"result": {"rows": expected, "ordered": True}})
        self.assertEqual(out["verdict"], "fail")

    def test_rows_ambiguous_tolerance_still_matches(self):
        # Regression (R1 blocker): with float tolerance, "equal" is non-exclusive,
        # so greedy first-fit could consume a row a later expected row needs and
        # report a spurious mismatch. A valid perfect matching exists here
        # (1.0<->1.0, 1.4<->1.2 at tol 0.3) and must be found.
        expected = [{"v": 1.0}, {"v": 1.4}]
        actual = [{"v": 1.2}, {"v": 1.0}]
        rc, out, _ = self.score(
            {"rows": actual}, {"result": {"rows": expected, "float_tolerance": 0.3}})
        self.assertEqual(out["verdict"], "pass")

    def test_rows_multiplicity_matters(self):
        # [a,a,b] != [a,b,b] as multisets even though the sets are equal.
        expected = [{"x": "a"}, {"x": "a"}, {"x": "b"}]
        actual = [{"x": "a"}, {"x": "b"}, {"x": "b"}]
        rc, out, _ = self.score({"rows": actual},
                                {"result": {"rows": expected}})
        self.assertEqual(out["verdict"], "fail")

    def test_rows_extra_columns_in_actual_ignored(self):
        # Expected row set is the spec; columns the app also returned are noise.
        expected = [{"role": "nurse", "n": 7}]
        actual = [{"role": "nurse", "n": 7, "dept": "ICU", "id": 42}]
        rc, out, _ = self.score({"rows": actual},
                                {"result": {"rows": expected}})
        self.assertEqual(out["verdict"], "pass")

    def test_rows_missing_expected_column_fails(self):
        expected = [{"role": "nurse", "n": 7}]
        actual = [{"role": "nurse"}]      # dropped the counted column
        rc, out, _ = self.score({"rows": actual},
                                {"result": {"rows": expected}})
        self.assertEqual(out["verdict"], "fail")

    def test_scalar_row_lists(self):
        rc, out, _ = self.score({"rows": ["b", "a"]},
                                {"result": {"rows": ["a", "b"]}})
        self.assertEqual(out["verdict"], "pass")

    def test_empty_result_set_matches_empty(self):
        rc, out, _ = self.score({"rows": []}, {"result": {"rows": []}})
        self.assertEqual(out["verdict"], "pass")

    def test_missing_actual_is_unscored_not_fail(self):
        # Runner could not extract a result (no content capture / prose only) —
        # an unread result must never be scored as a mismatch.
        rc, out, _ = self.score({"missing": True, "reason": "no content capture"},
                                {"result": {"scalar": 3}})
        self.assertEqual(out["verdict"], "unscored")
        self.assertEqual(out["unscorable"], 1)

    def test_no_result_expectation_is_unscored(self):
        rc, out, _ = self.score({"scalar": 7}, {"answer": {"must_contain": []}})
        self.assertEqual(out["verdict"], "unscored")
        self.assertEqual(out["checks"], [])

    def test_both_scalar_and_rows_exits_2(self):
        rc, out, _ = self.score({"scalar": 7},
                                {"result": {"scalar": 7, "rows": []}})
        self.assertEqual(rc, 2)
        self.assertIn("scalar", out["error"])

    def test_explicit_columns_still_ignores_extra_actual_columns(self):
        # The regression: an explicit `columns` list used to collapse with the
        # omitted case, flipping to bare-scalar-row mode where whole rows are
        # compared as JSON. That FAILED this correct result set because the app
        # returned extra columns the spec says to ignore.
        expected = [{"role": "nurse", "n": 7}]
        actual = [{"role": "nurse", "n": 7, "dept": "ICU"}]
        rc, out, _ = self.score({"rows": actual},
                                {"result": {"rows": expected,
                                            "columns": ["role", "n"]}})
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["checks"][0]["columns"], ["role", "n"])

    def test_explicit_columns_narrows_the_comparison(self):
        # An explicit list is also a real narrowing: differ outside it -> pass.
        rc, out, _ = self.score(
            {"rows": [{"role": "nurse", "n": 999}]},
            {"result": {"rows": [{"role": "nurse", "n": 7}],
                        "columns": ["role"]}})
        self.assertEqual(out["verdict"], "pass")

    def test_empty_columns_list_exits_2(self):
        # Comparing zero columns makes every row equal — a vacuous pass. Loud
        # error beats a silently meaningless verdict.
        rc, out, _ = self.score({"rows": [{"role": "nurse"}]},
                                {"result": {"rows": [{"role": "nurse"}],
                                            "columns": []}})
        self.assertEqual(rc, 2)
        self.assertIn("columns", out["error"])

    def test_non_list_columns_exits_2(self):
        rc, out, _ = self.score({"rows": [{"role": "nurse"}]},
                                {"result": {"rows": [{"role": "nurse"}],
                                            "columns": "role"}})
        self.assertEqual(rc, 2)
        self.assertIn("columns", out["error"])


class TestReduceRepeats(ScorerTest):
    """pass^k / pass@k reliability reducer over repeats of the SAME case."""

    def repeats(self, case_repeats):
        rows = [{"case_id": cid, "verdict": v}
                for cid, verdicts in case_repeats.items() for v in verdicts]
        return self.write_jsonl("repeats.jsonl", rows)

    def test_pass_at_k_and_pass_hat_k_known_values(self):
        # n=4 repeats, c=3 passes, k=2:
        # pass@k = 1 - C(1,2)/C(4,2) = 1 - 0/6 = 1.0 (already succeeded more
        #          than k could possibly miss)
        # pass^k = C(3,2)/C(4,2) = 3/6 = 0.5
        p = self.repeats({"c1": ["pass", "pass", "pass", "fail"]})
        rc, out, _ = run_script("reduce_repeats.py", p, "--k", 2)
        self.assertEqual(rc, 0)
        self.assertEqual(out["k"], 2)
        case = out["per_case"][0]
        self.assertEqual(case["n"], 4)
        self.assertEqual(case["c"], 3)
        self.assertAlmostEqual(case["pass_at_k"], 1.0, places=4)
        self.assertAlmostEqual(case["pass_hat_k"], 0.5, places=4)
        self.assertAlmostEqual(out["flakiness_gap"], 0.5, places=4)

    def test_tau_bench_shape_high_pass_at_k_low_pass_hat_k(self):
        # Every case succeeds at least once (pass@k=1) but not on every
        # repeat, so pass^k < 1 - the tau-bench >60% pass^1 / <25% pass^8
        # pattern a single run would hide; the gap must be positive.
        p = self.repeats({
            "c1": ["pass", "fail", "pass", "pass"],
            "c2": ["pass", "pass", "fail", "pass"],
        })
        rc, out, _ = run_script("reduce_repeats.py", p, "--k", 4)
        self.assertEqual(out["pass_at_k"], 1.0)
        self.assertLess(out["pass_hat_k"], 1.0)
        self.assertGreater(out["flakiness_gap"], 0.0)

    def test_default_k_is_smallest_repeat_count(self):
        # No --k given: use the smallest repeat count present so every case
        # contributes, rather than erroring or silently dropping a case.
        p = self.repeats({"c1": ["pass"] * 4, "c2": ["pass", "fail"]})
        rc, out, _ = run_script("reduce_repeats.py", p)
        self.assertEqual(rc, 0)
        self.assertEqual(out["k"], 2)

    def test_k_larger_than_some_case_repeats_errors(self):
        p = self.repeats({"c1": ["pass"] * 4, "c2": ["pass", "fail"]})
        rc, out, _ = run_script("reduce_repeats.py", p, "--k", 3)
        self.assertEqual(rc, 2)
        self.assertIn("c2", out["error"])

    def test_non_binary_verdict_rejected(self):
        p = self.repeats({"c1": ["pass", "unscored"]})
        rc, out, _ = run_script("reduce_repeats.py", p, "--k", 1)
        self.assertEqual(rc, 2)
        self.assertIn("pass", out["error"])

    def test_all_pass_all_k_gives_zero_gap(self):
        p = self.repeats({"c1": ["pass"] * 3, "c2": ["pass"] * 3})
        rc, out, _ = run_script("reduce_repeats.py", p, "--k", 3)
        self.assertEqual(out["pass_at_k"], 1.0)
        self.assertEqual(out["pass_hat_k"], 1.0)
        self.assertEqual(out["flakiness_gap"], 0.0)


if __name__ == "__main__":
    unittest.main()
