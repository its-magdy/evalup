#!/usr/bin/env python3
"""Golden-fixture and boundary tests for the agent-eval scorers.

Stdlib only (unittest + subprocess), matching the scorers themselves.
Run: python3 -m unittest discover -s tests -v   (from the plugin root)

Each scorer is exercised as a real subprocess — argv, files, exit codes —
because that is its actual contract with the run skill.
"""
import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"

# The Python floor README.md advertises. 3.9 is past upstream end-of-life
# (October 2025), so the floor is a deliberate compatibility choice rather than
# a default: RHEL 9 ships 3.9 and Red Hat backports fixes for that distro's
# lifetime, and this harness is aimed at exactly those long-lived enterprise
# environments. Raise it here and in README.md together, never separately.
PY_FLOOR = (3, 9)


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
        p.write_text(json.dumps(obj), encoding="utf-8")
        return p

    def write_jsonl(self, name, rows):
        p = self.tmp / name
        p.write_text("".join(json.dumps(r) + "\n" for r in rows),
                     encoding="utf-8")
        return p

    def assert_clean_error(self, script, *argv):
        """Every scorer promises: exit 0 with a verdict, or exit 2 with a JSON
        {"error": ...} on stdout. Asserted through one helper so no caller can
        check three of the four and leave the traceback case open — the run
        skill reads a crash as an infra failure, not a data error."""
        rc, out, err = run_script(script, *argv)
        self.assertEqual(rc, 2, f"{script}: expected exit 2, got {rc}\n{err}")
        self.assertIsNotNone(out, f"{script}: stdout was not JSON\n{err}")
        self.assertIn("error", out, f"{script}: no error key\n{err}")
        self.assertNotIn("Traceback", err, f"{script}: crashed\n{err}")
        return out


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
        p.write_text("", encoding="utf-8")
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
        bad.write_text("{not json", encoding="utf-8")
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

    def test_list_arg_sourced_from_scalar_field_passes(self):
        # unitIds=[300] is legitimate provenance from a prior result's scalar
        # "id": 300 field — the list's serialized form "[300]" never appears
        # verbatim, so each element must be checked, not the whole list.
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "search_units", "args": {"search": "abha"},
             "result": "{\"id\": 300, \"name\": \"abha\"}"},
            {"name": "search_employees", "args": {"unitIds": [300]}},
        ]})
        expect = self.write_json("e.json", {"args": {
            "search_employees": {"unitIds": "from_tool_result"}}})
        rc, out, _ = run_script("score_args.py", traj, expect)
        self.assertEqual(out["verdict"], "pass")

    def test_list_arg_with_one_hallucinated_element_fails(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "search_units", "args": {},
             "result": "{\"id\": 300, \"name\": \"abha\"}"},
            {"name": "search_employees", "args": {"unitIds": [300, 999]}},
        ]})
        expect = self.write_json("e.json", {"args": {
            "search_employees": {"unitIds": "from_tool_result"}}})
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
        p.write_text("[]", encoding="utf-8")
        for script, argv in (("detect_loops.py", [p]),
                             ("trajectory_match.py", [p, p]),
                             ("score_args.py", [p, p])):
            rc, out, _ = run_script(script, *argv)
            self.assertEqual(rc, 2, script)
            self.assertIn("tool_calls", out["error"])

    def test_non_object_tool_call_is_a_clean_error(self):
        """The container was validated; its ELEMENTS were not. Every consumer
        reads a call with c.get(...), so one non-object entry — what a
        miswritten adapter mapping_shim emits — raised AttributeError in all
        four: exit 1 with an empty stdout, which is neither the {"error": ...}
        payload nor the exit 2 the run skill branches on."""
        traj = self.write_json("t.json",
                               {"tool_calls": [{"name": "a"}, 42]})
        expect = self.write_json("e.json", {
            "tools": {"subset": ["a"]},
            "args": {"a": {"x": "present"}},
            "authz": {"forbidden_tools": ["a"]}})
        for script, argv in (("detect_loops.py", [traj]),
                             ("trajectory_match.py", [traj, expect]),
                             ("score_args.py", [traj, expect]),
                             ("score_authz.py", [traj, expect])):
            out = self.assert_clean_error(script, *argv)
            self.assertIn("tool_calls[1]", out["error"], script)


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


class TestTrajectoryMatchShapeValidation(ScorerTest):
    """expect.tools.<key> must be a LIST of tool names.

    A string is a sequence too — of characters — so every matcher accepted one
    and scored nonsense from it. This is the manufactured-failure class, not
    the traceback class: exit 0, clean JSON, wrong verdict, nothing downstream
    able to tell. Same rule score_execution.py applies to
    expect.result.columns."""

    def traj(self):
        return self.write_json("t.json", {"tool_calls": [{"name": "foo"}]})

    def test_list_subset_still_passes(self):
        # The control the string cases are wrong against.
        rc, out, _ = run_script(
            "trajectory_match.py", self.traj(),
            self.write_json("e.json", {"tools": {"subset": ["foo"]}}))
        self.assertEqual(rc, 0)
        self.assertEqual(out["verdict"], "pass")

    def test_string_subset_is_a_clean_error_not_a_fail(self):
        # Was: missing ['f','o','o'] and verdict "fail" on a trajectory that
        # called foo — a passing case reported as broken.
        out = self.assert_clean_error(
            "trajectory_match.py", self.traj(),
            self.write_json("e.json", {"tools": {"subset": "foo"}}))
        self.assertIn("expect.tools.subset", out["error"])

    def test_string_order_is_a_clean_error_in_every_mode(self):
        for mode in ("in_order", "exact", "any_order"):
            out = self.assert_clean_error(
                "trajectory_match.py", self.traj(),
                self.write_json("e.json", {"tools": {
                    "order": "foo", "order_mode": mode}}))
            self.assertIn("expect.tools.order", out["error"], mode)

    def test_string_forbidden_is_a_clean_error(self):
        # The permissive direction: set("delete_user") holds single characters,
        # so no real tool name is ever in it and the gate passed everything it
        # exists to catch.
        out = self.assert_clean_error(
            "trajectory_match.py", self.traj(),
            self.write_json("e.json", {"tools": {
                "subset": ["foo"], "forbidden": "delete_user"}}))
        self.assertIn("expect.tools.forbidden", out["error"])

    def test_non_string_entry_is_a_clean_error(self):
        out = self.assert_clean_error(
            "trajectory_match.py", self.traj(),
            self.write_json("e.json", {"tools": {"subset": [1]}}))
        self.assertIn("expect.tools.subset[0]", out["error"])

    def test_a_written_but_empty_order_is_a_clean_error(self):
        # The vacuous pass: `order:` with nothing after it parses as None, and
        # `order: []` is already a list, so both reached match() with
        # expected == [] — is_subsequence of nothing is true, Counter() ==
        # Counter() is true — and EVERY trajectory passed an assertion the
        # author believed they had written. Absence is fine; the key being
        # present is a claim that something was asserted.
        for value in (None, []):
            for mode in ("in_order", "exact", "any_order"):
                out = self.assert_clean_error(
                    "trajectory_match.py", self.traj(),
                    self.write_json("e.json", {"tools": {
                        "order": value, "order_mode": mode}}))
                self.assertIn("expect.tools.order", out["error"],
                              f"{value!r}/{mode}")

    def test_a_written_but_empty_subset_is_a_clean_error(self):
        for value in (None, []):
            out = self.assert_clean_error(
                "trajectory_match.py", self.traj(),
                self.write_json("e.json", {"tools": {"subset": value}}))
            self.assertIn("expect.tools.subset", out["error"], repr(value))

    def test_an_absent_key_is_still_not_an_assertion(self):
        # The other half of the rule: an empty/absent tools block asserts
        # nothing and must stay a clean "none" pass, not become an error.
        for expect in ({}, {"tools": {}}, {"tools": None}):
            rc, out, err = run_script(
                "trajectory_match.py", self.traj(),
                self.write_json("e.json", expect))
            self.assertEqual(rc, 0, f"{expect}: {err}")
            self.assertEqual(out["mode"], "none")
            self.assertEqual(out["verdict"], "pass")

    def test_forbidden_alone_is_still_scorable(self):
        # forbidden is a gate, not an order assertion — it must keep working
        # with no order/subset key beside it.
        rc, out, _ = run_script(
            "trajectory_match.py", self.traj(),
            self.write_json("e.json", {"tools": {"forbidden": ["foo"]}}))
        self.assertEqual(rc, 0)
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual(out["forbidden_violations"], ["foo"])

    def test_an_empty_forbidden_list_is_not_an_error(self):
        # Deliberately NOT asserted_list: an empty forbidden list forbids
        # nothing, which is the permissive direction but also the honest
        # reading — unlike an empty `order`, it makes no claim that was
        # silently dropped.
        rc, out, _ = run_script(
            "trajectory_match.py", self.traj(),
            self.write_json("e.json", {"tools": {"subset": ["foo"],
                                                 "forbidden": []}}))
        self.assertEqual(rc, 0)
        self.assertEqual(out["verdict"], "pass")


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
        p.write_text(text, encoding="utf-8")
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
    — the run skill reads a crash as an infra failure, not a data error.
    (The assertion itself lives on ScorerTest, so every class asserts the same
    four things.)"""

    def test_non_dict_jsonl_row(self):
        p = self.tmp / "rows.jsonl"
        p.write_text("[1, 2]\n", encoding="utf-8")
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
        p.write_text(text, encoding="utf-8")
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

    def test_trailing_flag_suffix_is_unscorable_not_silently_wrong(self):
        # /pattern/i (Perl/JS-style) is not this convention's /.../ marker.
        # Before this check existed, must_contain read it as a literal
        # substring of the whole garbled string and always failed; here an
        # answer that plainly satisfies the intended pattern still must not
        # silently pass or fail — it must say the flag wasn't applied.
        rc, out, _ = run_script(
            "score_answer.py", self.answer("Yes, it does."),
            self.write_json("e.json", {"answer": {"must_contain": [r"/\byes\b/i"]}}))
        self.assertEqual(out["verdict"], "unscored")
        self.assertIn("flag suffix", out["checks"][0]["reason"])

    def test_must_not_contain_with_flag_suffix_is_unscorable_not_a_free_pass(self):
        # The must_not_contain mirror of the bug above: silently reading the
        # garbled string as a literal meant this NEVER matched, so the check
        # vacuously passed on every answer — including ones that clearly
        # violate the intended pattern. Must be unscorable, not a free pass.
        rc, out, _ = run_script(
            "score_answer.py", self.answer("I have renamed the unit."),
            self.write_json("e.json", {"answer": {
                "must_not_contain": [r"/I (have |'ve )?renamed/i"]}}))
        self.assertEqual(out["verdict"], "unscored")
        self.assertIn("flag suffix", out["checks"][0]["reason"])

    def test_path_like_literal_still_reads_as_regex_not_flag_suffix(self):
        # Regression guard: a path-like /usr/local/ already ends with "/" and
        # must keep parsing as the regex "usr/local", unaffected by the new
        # flag-suffix detection (which only fires when the string does NOT
        # end in "/").
        rc, out, _ = run_script(
            "score_answer.py", self.answer("found at usr/local/bin"),
            self.write_json("e.json", {"answer": {"must_contain": ["/usr/local/"]}}))
        self.assertEqual(out["verdict"], "pass")

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

# Everything that parses JSON owes the exit-2-with-{"error"} contract. The
# viewer generator does (it reads run records), so it belongs here even though
# it scores nothing.
JSON_CLI_SCRIPTS = SCORERS + ("build_review_viewer.py",)

# Every script with a CLI owes --version/--help — both non-scorers stamp the
# harness version into artifacts that runs get compared against, so a silent
# drift there is a comparability bug. md_to_html.py is only in THIS set: it
# reads Markdown, not JSON, so malformed JSON is not malformed input to it
# ("{not json" is valid Markdown and converting it is the correct behavior).
CLI_SCRIPTS = JSON_CLI_SCRIPTS + ("md_to_html.py",)


class TestHarnessVersion(ScorerTest):
    """run/SKILL.md refuses a baseline diff unless the harness versions match,
    so the version has to be obtainable FROM the harness — otherwise the
    orchestrating skill invents one and the comparability rule is decorative."""

    def test_every_scorer_reports_the_same_version(self):
        seen = set()
        for name in CLI_SCRIPTS:
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / name), "--version"],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{name}: {proc.stderr}")
            seen.add((proc.stdout + proc.stderr).strip())
        self.assertEqual(len(seen), 1, f"scorers disagree on version: {seen}")

    def test_version_matches_the_plugin_manifest(self):
        manifest = json.loads(
            (SCRIPTS.parent / ".claude-plugin" / "plugin.json")
            .read_text(encoding="utf-8"))
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "stats.py"), "--version"],
            capture_output=True, text=True)
        self.assertIn(manifest["version"], proc.stdout + proc.stderr,
                      "HARNESS_VERSION drifted from plugin.json")


def all_sources():
    """Every Python file in the package, scripts and tests alike. The two
    whole-source guards (the floor's grammar, the blank-line run) must agree on
    what "every source" means, or one of them silently stops covering a file the
    other checks."""
    root = SCRIPTS.parent
    return sorted([*SCRIPTS.glob("*.py"), *(root / "tests").glob("*.py")])


class TestDeclaredPythonFloor(unittest.TestCase):
    """README.md advertises a minimum Python; nothing checked it, so the claim
    could only ever be discovered wrong by a user on that version.

    ast.parse(feature_version=...) compiles under the floor's grammar on ANY
    interpreter, so this holds even where no old interpreter is installed. It
    catches syntax that silently arrives with a newer Python — match statements
    (3.10), except* (3.11), and so on.

    What it does NOT catch, stated so the guarantee is not overread: newer
    STDLIB APIs (str.removeprefix, itertools.pairwise, math.nextafter) and
    annotations that parse everywhere but only evaluate on newer runtimes
    (`int | None`). Those need a real interpreter of the floor version — a CI
    matrix job, which this repo does not have yet."""

    def test_every_source_parses_under_the_declared_floor(self):
        checked = 0
        for path in all_sources():
            src = path.read_text(encoding="utf-8")
            try:
                ast.parse(src, filename=str(path), feature_version=PY_FLOOR)
            except SyntaxError as e:
                self.fail(f"{path.name} needs newer than Python "
                          f"{'.'.join(map(str, PY_FLOOR))}: {e.msg} "
                          f"(line {e.lineno}). Either keep the floor and "
                          f"rewrite this, or raise PY_FLOOR and README.md "
                          f"together.")
            checked += 1
        self.assertGreater(checked, 10, "source glob matched almost nothing — "
                                        "the test would pass vacuously")

    def test_the_floor_check_actually_rejects_newer_syntax(self):
        # Guard against a vacuous guard: if feature_version ever stopped being
        # enforced, the test above would pass on anything.
        with self.assertRaises(SyntaxError):
            ast.parse("match x:\n    case 1: pass", feature_version=PY_FLOOR)


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
        bad.write_text("{not json", encoding="utf-8")
        for script in JSON_CLI_SCRIPTS:
            self.assert_clean_error(script, *self.bad_argv(script, bad))

    def test_normalize_trace_survives_a_list_of_scalars(self):
        # Regression: flatten_otlp probed doc[0] for "resourceSpans" without
        # checking dict-ness -> AttributeError on strings, TypeError on ints.
        for payload in ('["a","b"]', "[1,2]", '"just a string"', "42"):
            p = self.tmp / "s.json"
            p.write_text(payload, encoding="utf-8")
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
        p.write_text("a" * 40 + "b", encoding="utf-8")
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


class TestScoreRoutingOOSLabelInvariance(ScorerTest):
    """--oos-route only RENAMES a label to the __oos__ sentinel. Renaming must
    not change any verdict, so every metric has to be invariant under the flag
    for rows that pass either way."""

    ROWS = [{"case_id": "c1", "expected": "billing",
             "acceptable": ["refuse"], "observed": "refuse"},
            {"case_id": "c2", "expected": "billing", "observed": "billing"}]

    def test_oos_route_in_the_acceptable_set_still_passes(self):
        # The bug: expected/observed were rewritten to __oos__ but `acceptable`
        # was left holding the raw route name, so the membership test missed and
        # a case that passes without the flag was scored a mismatch WITH it —
        # accuracy 1.0 dropped to 0.5. An OOS route is a legitimate alternate
        # for an ambiguous in-scope query (the field-test corpus has exactly
        # this shape), and a manufactured failure is the one outcome this
        # harness must never produce.
        p = self.write_jsonl("r.jsonl", self.ROWS)
        rc, out, _ = run_script("score_routing.py", p, "--oos-route", "refuse")
        self.assertEqual(out["accuracy"], 1.0)
        self.assertEqual(out["accepted_alternates"]["count"], 1)
        self.assertNotIn("__oos__", out["confusion_matrix"].get("billing", {}))

    def test_verdicts_are_identical_with_and_without_the_flag(self):
        p = self.write_jsonl("r.jsonl", self.ROWS)
        _, without, _ = run_script("score_routing.py", p)
        _, with_flag, _ = run_script("score_routing.py", p,
                                     "--oos-route", "refuse")
        for key in ("accuracy", "confusion_matrix", "per_target"):
            self.assertEqual(without[key], with_flag[key], key)

    def test_route_acceptable_alias_is_mapped_too(self):
        # The case-format spelling goes through the same alias rewrite, so it
        # has to reach the same normalization.
        p = self.write_jsonl("r.jsonl", [
            {"case_id": "c1", "route": "billing",
             "route_acceptable": ["refuse"], "observed": "refuse"}])
        rc, out, _ = run_script("score_routing.py", p, "--oos-route", "refuse")
        self.assertEqual(out["accuracy"], 1.0)


class TestScoreExecutionUnits(ScorerTest):
    """A unit is content, not decoration. Stripping it from BOTH sides before
    comparing made the execution scorer pass the confidently-wrong answer it
    exists to catch."""

    score = TestScoreExecution.score

    def test_same_number_different_units_fails(self):
        # "12 hours" and "12 days" are not the same answer. Both sides had their
        # unit deleted, leaving 12.0 == 12.0 -> pass.
        rc, out, _ = self.score({"scalar": "12 days"},
                                {"result": {"scalar": "12 hours"}})
        self.assertEqual(out["verdict"], "fail")

    def test_unit_mismatch_reason_is_actionable(self):
        # "expected 12, got 12" reads as a harness bug; the reviewer needs to
        # know it was the unit, and what to do about it.
        rc, out, _ = self.score({"scalar": "12 days"},
                                {"result": {"scalar": "12 hours"}})
        self.assertEqual(out["verdict"], "fail")   # fail cleanly, not on KeyError
        reason = out["checks"][0]["reason"]
        self.assertIn("units differ", reason)
        self.assertIn("hours", reason)
        self.assertIn("days", reason)

    def test_currency_and_percent_are_units_too(self):
        rc, out, _ = self.score({"scalar": "5%"}, {"result": {"scalar": "$5"}})
        self.assertEqual(out["verdict"], "fail")

    def test_a_unit_on_only_the_actual_side_still_passes(self):
        # The documented tolerance: the oracle stores a bare number and the app
        # wrapped it for display. That is presentation, not disagreement, and
        # tightening the both-sides case must not cost it.
        for expected, actual in [(1200, "1200 hrs"), (5, "$5"), (45, "45%"),
                                 (12, "12 days")]:
            rc, out, _ = self.score({"scalar": actual},
                                    {"result": {"scalar": expected}})
            self.assertEqual(out["verdict"], "pass", f"{expected!r}/{actual!r}")

    def test_a_bare_expected_number_accepts_any_unit_back(self):
        """The full price of the one-sided tolerance, pinned rather than left
        implicit: with nothing to compare a unit against, the scorer cannot tell
        a presentational unit from a wrong one, so "5 apples" passes an expected
        5 exactly as "$5" does. Documented in the module docstring — write the
        unit into the expected value if it is part of the answer, which puts the
        strict both-sides rule back in play."""
        for actual in ("5 apples", "5 kg", "5 widgets"):
            rc, out, _ = self.score({"scalar": actual},
                                    {"result": {"scalar": 5}})
            self.assertEqual(out["verdict"], "pass", actual)
        rc, out, _ = self.score({"scalar": "5 apples"},
                                {"result": {"scalar": "5 kg"}})
        self.assertEqual(out["verdict"], "fail")

    def test_a_sign_before_the_currency_is_a_documented_gap(self):
        """Pins the known gap named in the docstring so a future fix has to
        notice this test rather than silently changing verdicts: the currency
        strip runs before any sign, so "-$5" is not numeric here and falls
        through to string comparison."""
        rc, out, _ = self.score({"scalar": "-$5"}, {"result": {"scalar": -5}})
        self.assertEqual(out["verdict"], "fail")

    def test_a_half_written_exponent_is_not_a_unit(self):
        """The boundary of the one-sided tolerance above. "5e" does not spell a
        quantity at all — it is a malformed number — but the trailing-unit regex
        read the exponent marker as a unit token, so the bare-expected-number
        rule handed back a pass for an unparseable answer. A separating space
        ("5 e") is a real, if odd, unit and still passes."""
        for actual in ("5e", "5E"):
            rc, out, _ = self.score({"scalar": actual},
                                    {"result": {"scalar": 5}})
            self.assertEqual(out["verdict"], "fail", actual)
        rc, out, _ = self.score({"scalar": "5 e"}, {"result": {"scalar": 5}})
        self.assertEqual(out["verdict"], "pass")

    def test_a_well_formed_exponent_is_still_a_number(self):
        # The guard keys off a TRAILING e, so scientific notation is untouched.
        for expected, actual in [("1e5", 100000), ("2.5e3", 2500),
                                 ("1E5", 100000)]:
            rc, out, _ = self.score({"scalar": actual},
                                    {"result": {"scalar": expected}})
            self.assertEqual(out["verdict"], "pass", f"{expected!r}")

    def test_matching_units_compare_numerically(self):
        # Same unit on both sides: fall through to the number, tolerance and all.
        rc, out, _ = self.score(
            {"scalar": "3.14 kg"},
            {"result": {"scalar": "3.1 kg", "float_tolerance": 0.05}})
        self.assertEqual(out["verdict"], "pass")

    def test_units_are_case_insensitive(self):
        rc, out, _ = self.score({"scalar": "12 HOURS"},
                                {"result": {"scalar": "12 hours"}})
        self.assertEqual(out["verdict"], "pass")

    def test_unit_check_applies_to_row_cells(self):
        rc, out, _ = self.score(
            {"rows": [{"role": "nurse", "shift": "12 days"}]},
            {"result": {"rows": [{"role": "nurse", "shift": "12 hours"}]}})
        self.assertEqual(out["verdict"], "fail")

    def test_an_empty_string_scalar_scores_instead_of_crashing(self):
        """The unit splitter tested `core[:1] in _CURRENCY`, and EVERY string
        contains the empty string — so the currency branch was taken for an
        empty core and indexed it. An app that answered "" (or a case whose
        ground truth is "") took the scorer down with an IndexError: exit 1,
        empty stdout, no {"error": ...}. The run skill reads that as an infra
        failure rather than the plain data mismatch it is."""
        for expected, actual, verdict in [
                ("no data", "", "fail"),
                ("", "something", "fail"),
                ("", "", "pass"),
                # Whitespace-only normalizes to empty on the string path, which
                # is the documented casefold+trim rule, not a unit.
                ("", "   ", "pass"),
                # The currency/unit branch itself must still be reachable, and
                # a lone symbol must not be read as a number.
                ("$", "$", "pass"),
                ("$5", "5", "pass")]:
            with self.subTest(expected=expected, actual=actual):
                rc, out, err = self.score({"scalar": actual},
                                          {"result": {"scalar": expected}})
                self.assertEqual(rc, 0, err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(out["verdict"], verdict)

    def test_empty_cells_in_rows_score_instead_of_crashing(self):
        """Same crash, reached through the row path — cells go through the
        same comparator, so an empty cell anywhere in a result set hit it."""
        rc, out, err = self.score(
            {"rows": [{"role": "nurse", "note": ""}]},
            {"result": {"rows": [{"role": "nurse", "note": ""}]}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")

        rc, out, err = self.score(
            {"rows": [{"role": "nurse", "note": ""}]},
            {"result": {"rows": [{"role": "nurse", "note": "on call"}]}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")


class TestCliArgumentValidation(ScorerTest):
    """Out-of-range numeric flags used to escape the JSON error contract as
    tracebacks. Validation lives after parse_args() rather than in an argparse
    `type=` callable so the error stays machine-readable on stdout."""

    def rows(self):
        return self.write_jsonl("v.jsonl", [
            {"case_id": "a", "verdict": "pass"},
            {"case_id": "b", "verdict": "fail"}])

    def assert_error_mentions(self, needle, script, *argv):
        """The shared error contract, plus the one thing extra these cases owe
        the reader: the message has to name the flag or key that was rejected,
        or a clean exit 2 is still a guessing game."""
        out = self.assert_clean_error(script, *argv)
        self.assertIn(needle, out["error"])

    def test_alpha_at_zero_is_a_clean_error(self):
        # Reached NormalDist().inv_cdf and exited with a StatisticsError.
        p = self.rows()
        self.assert_error_mentions("--alpha", "stats.py", p, p, "--alpha", 0)

    def test_alpha_above_one_is_a_clean_error(self):
        p = self.rows()
        self.assert_error_mentions("--alpha", "stats.py", p, p, "--alpha", 1.5)

    def test_bayes_threshold_out_of_range_is_a_clean_error(self):
        p = self.rows()
        self.assert_error_mentions("--bayes-threshold", "stats.py", p, p,
                                   "--bayes-threshold", 1.5)

    def test_repeat_threshold_below_two_is_a_clean_error(self):
        # At 0 or 1 every single call is a "repeat loop" and any tool at all is
        # "flailing", so a clean trajectory reported fail.
        traj = self.write_json("t.json",
                               {"tool_calls": [{"name": "t", "args": {}}]})
        for bad in (0, 1):
            self.assert_error_mentions("--repeat-threshold", "detect_loops.py",
                                       traj, "--repeat-threshold", bad)

    def test_call_budget_below_one_is_a_clean_error(self):
        traj = self.write_json("t.json", {"tool_calls": []})
        self.assert_error_mentions("--call-budget", "detect_loops.py", traj,
                                   "--call-budget", 0)

    def test_negative_float_tolerance_is_a_clean_error(self):
        """The other flags in this class escaped the contract as tracebacks; a
        negative tolerance is worse — it exits 0 with a manufactured failure.
        `abs(e - a) <= tol` is false at tol<0 even for identical values, so a
        correct answer scored `fail` with nothing to signal it."""
        actual = self.write_json("a.json", {"scalar": 7})
        expect = self.write_json("e.json", {"result": {"scalar": 7}})
        self.assert_error_mentions("--float-tolerance", "score_execution.py",
                                   actual, expect, "--float-tolerance", -1)

    def test_negative_per_case_float_tolerance_is_a_clean_error(self):
        actual = self.write_json("a.json", {"scalar": 7})
        expect = self.write_json("e.json", {
            "result": {"scalar": 7, "float_tolerance": -0.5}})
        self.assert_error_mentions("expect.result.float_tolerance",
                                   "score_execution.py", actual, expect)

    def test_zero_and_positive_tolerance_still_pass(self):
        # The bound must not swallow the legitimate values around it.
        actual = self.write_json("a.json", {"scalar": 7})
        expect = self.write_json("e.json", {"result": {"scalar": 7}})
        for tol in ("0", "0.5"):
            rc, out, err = run_script("score_execution.py", actual, expect,
                                      "--float-tolerance", tol)
            self.assertEqual(rc, 0, f"tol={tol}\n{err}")
            self.assertEqual(out["verdict"], "pass", f"tol={tol}")

    def test_non_scalar_case_id_is_a_clean_error(self):
        # load_jsonl put the id in a set to dedupe; a list id raised TypeError
        # straight out of the loader that exists to produce clean errors.
        p = self.write_jsonl("u.jsonl", [{"case_id": ["a"], "verdict": "pass"}])
        self.assert_error_mentions("case_id", "stats.py", p, p)


class TestEncodingContract(ScorerTest):
    """Every read passes encoding="utf-8" explicitly (PEP 597; Ruff PLW1514).

    Taking the locale default corrupted verdicts two different ways, and the
    quiet one is the dangerous one: under an ASCII locale a valid answer
    containing "€" raised UnicodeDecodeError (traceback, exit 1 — outside the
    exit-2 contract), while under latin-1/cp1252 it did not raise at all and
    decoded to mojibake, so a must_contain on the euro sign FAILED a correct
    answer. A verdict must not depend on the reviewer's locale.
    """

    # Disables PEP 540 UTF-8 mode and PEP 538 C-locale coercion, which would
    # otherwise quietly hand the interpreter UTF-8 back and make this a no-op.
    ASCII_LOCALE = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0",
                    "PYTHONCOERCECLOCALE": "0"}

    def write_bytes(self, name, data):
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def run_in_locale(self, env_overrides, script, *argv):
        env = dict(os.environ, **env_overrides)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / script), *[str(x) for x in argv]],
            capture_output=True, text=True, env=env)
        return proc

    def test_utf8_answer_scores_the_same_under_an_ascii_locale(self):
        answer = self.write_bytes(
            "a.txt", "Total: €1,200 — confirmed.".encode("utf-8"))
        expect = self.write_bytes("e.json", json.dumps(
            {"answer": {"must_contain": ["€1,200"]}}).encode("utf-8"))

        default = self.run_in_locale({}, "score_answer.py", answer, expect)
        ascii_ = self.run_in_locale(self.ASCII_LOCALE, "score_answer.py",
                                    answer, expect)

        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(ascii_.returncode, 0, ascii_.stderr)
        self.assertNotIn("Traceback", ascii_.stderr)
        self.assertEqual(json.loads(ascii_.stdout)["verdict"], "pass")
        # Byte-identical, not merely both-passing: the point is that the
        # locale is not an input to the verdict at all.
        self.assertEqual(default.stdout, ascii_.stdout)

    def test_non_utf8_bytes_are_a_clean_error_everywhere(self):
        """Genuinely undecodable input is still an error — just a reported one.
        Covers every reader through every entry point: the JSON loader, the
        JSONL loader and the plain text loader, exercised via the scorers, the
        viewer and normalize_trace. The viewer and normalize_trace each used to
        carry their own copy of the JSON read with the same gap in it —
        UnicodeDecodeError is a ValueError, so the OSError arm never saw it —
        and both now go through _common.load_json.

        normalize_trace.py was the last bare open() in the package and the one
        that mattered most: it is the FIRST script in the run pipeline, so its
        traceback landed before any scorer ran and the run skill read a data
        error as an infra failure. Its own -X warn_default_encoding run is the
        mechanical version of this check; this asserts the contract."""
        junk = b"\xff\xfe"
        traj = self.write_bytes("t.json", b'{"tool_calls":[],"x":"' + junk + b'"}')
        rows = self.write_bytes(
            "v.jsonl", b'{"case_id":"a","verdict":"pass","x":"' + junk + b'"}\n')
        answer = self.write_bytes("a.txt", junk)
        markdown = self.write_bytes("r.md", b"# T\n\n" + junk + b"\n")
        expect = self.write_json("e.json", {"answer": {"must_contain": ["x"]}})
        rundir = self.tmp / "run"
        rundir.mkdir()
        (rundir / "c.json").write_bytes(b'{"case_id":"' + junk + b'"}')

        for script, argv in (
                ("detect_loops.py", [traj]),
                ("score_args.py", [traj, expect]),
                ("score_answer.py", [answer, expect]),
                ("score_authz.py", [traj, expect]),
                ("stats.py", [rows, rows]),
                ("reduce_repeats.py", [rows]),
                ("score_routing.py", [rows]),
                ("md_to_html.py", [markdown]),
                ("build_review_viewer.py", [rundir]),
                ("normalize_trace.py", [traj, "--trace-id", "t1"])):
            out = self.assert_clean_error(script, *argv)
            self.assertIn("utf-8", out["error"], script)

    def test_no_script_reads_a_file_without_an_explicit_encoding(self):
        """The mechanical form of the rule, so a new script cannot reintroduce
        it. PEP 597's own detector (-X warn_default_encoding) is what found the
        one remaining bare open() in normalize_trace.py; grepping the source is
        the cheap always-on version of the same check."""
        # The builtin only: a preceding identifier character or dot means this
        # is something else entirely. urllib.request.urlopen() was the case
        # that made this matter -- it takes no encoding, cannot, and matched a
        # bare "open(" substring search, so run_cases.py failed a rule it does
        # not break.
        builtin_open = re.compile(r"(?<![\w.])open\(")
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if builtin_open.search(code) and "encoding=" not in code:
                    offenders.append(f"{path.name}:{lineno}")
        self.assertEqual(offenders, [],
                         "open() without encoding='utf-8' (PEP 597; the "
                         "locale default decodes to mojibake under "
                         "latin-1/cp1252 and corrupts verdicts silently)")

    def test_no_source_file_has_a_stranded_blank_line_run(self):
        """PEP 8 tops out at two blank lines (E303). Trivial on its own, but no
        linter runs in this package's stdlib-only setup, so the one thing that
        would ever catch it is a test — and the shape it catches is the residue
        of a DELETED definition (score_authz.stringify moving to _common left
        four), which is worth noticing while the move is still fresh."""
        offenders = []
        for path in all_sources():
            blanks = 0
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                blanks = blanks + 1 if not line.strip() else 0
                if blanks == 3:
                    offenders.append(f"{path.name}:{lineno}")
        self.assertEqual(offenders, [], "more than two consecutive blank lines")


class TestExpectationShapeValidation(ScorerTest):
    """Every list/object-valued `expect` field rejects a bare scalar.

    Written as one table rather than per-scorer cases because the bug it guards
    is a FAMILY, and the family is what kept recurring: trajectory_match.py had
    the guard and a test, and score_authz.py / score_answer.py / score_args.py
    each shipped the same hole because nothing checked them as a group. A new
    expect field is now one row here, not a new blind spot.

    The slip is `forbidden_tools: delete_user` instead of `[delete_user]` —
    plausible YAML, and silent, because a string IS a sequence (of characters).
    Both wrong-verdict directions were reachable in production code:
    score_authz reported PASS on a trajectory that called the forbidden tool,
    and score_answer FAILED a correct answer. See _common.require_list."""

    # (dotted field name, the expect object with exactly that field slipped)
    SLIPS = [
        # The TOP-LEVEL containers, not just the leaves inside them. Each of
        # these three guarded its children while leaving itself open, and every
        # probe on the parent happened to be valid on a string: `"subset" in
        # expect_tools` and `"scalar" not in spec` are SUBSTRING tests, and
        # `set(acceptable)` is a set of characters. So the slip got past the
        # gate and died deeper (TypeError/AttributeError, exit 1) or, for
        # routing, never raised at all.
        ("expect.tools", "trajectory_match.py", {"tools": "subset"}),
        ("expect.result", "score_execution.py", {"result": "scalar"}),
        ("expect.tools.subset", "trajectory_match.py",
         {"tools": {"subset": "get_invoice"}}),
        ("expect.tools.order", "trajectory_match.py",
         {"tools": {"order": "get_invoice"}}),
        ("expect.tools.forbidden", "trajectory_match.py",
         {"tools": {"subset": ["get_invoice"], "forbidden": "delete_user"}}),
        ("expect.authz", "score_authz.py", {"authz": "no deletes"}),
        ("expect.authz.forbidden_tools", "score_authz.py",
         {"authz": {"forbidden_tools": "delete_user"}}),
        ("expect.authz.forbidden_record_ids", "score_authz.py",
         {"authz": {"forbidden_record_ids": "INV-1"}}),
        ("expect.authz.allowed_record_ids", "score_authz.py",
         {"authz": {"allowed_record_ids": "INV-1"}}),
        ("expect.answer", "score_answer.py", {"answer": "seven"}),
        ("expect.answer.must_contain", "score_answer.py",
         {"answer": {"must_contain": "seven nurses"}}),
        ("expect.answer.must_not_contain", "score_answer.py",
         {"answer": {"must_not_contain": "seven nurses"}}),
        ("expect.format.json_schema", "score_answer.py",
         {"format": {"json_schema": "object"}}),
        ("expect.args", "score_args.py", {"args": "get_invoice"}),
        ("expect.args.get_invoice", "score_args.py",
         {"args": {"get_invoice": "invoice_id"}}),
        ("expect.result.columns", "score_execution.py",
         {"result": {"rows": [{"id": 1}], "columns": "id"}}),
        ("expect.result.rows", "score_execution.py",
         {"result": {"rows": "id"}}),
    ]

    def argv_for(self, script, expect_path):
        """Each scorer's first positional differs; the expect object is always
        the second."""
        if script == "score_answer.py":
            answer = self.tmp / "a.txt"
            answer.write_text("The invoice total is 500 dollars.",
                              encoding="utf-8")
            return [answer, expect_path]
        if script == "score_execution.py":
            return [self.write_json("actual.json", {"rows": []}), expect_path]
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "delete_user", "args": {"id": 1}, "result": "INV-1 gone"}]})
        return [traj, expect_path]

    def test_every_expect_field_rejects_a_bare_string(self):
        for field, script, expect in self.SLIPS:
            with self.subTest(field=field, script=script):
                path = self.write_json("e.json", expect)
                out = self.assert_clean_error(script, *self.argv_for(script,
                                                                     path))
                # The message must name the field. "must be a list" alone
                # sends the author hunting through the whole case file.
                self.assertIn(field, out["error"])

    def test_the_authz_slip_no_longer_passes_a_real_violation(self):
        """The specific verdict this guard exists for. `forbidden_tools` as a
        bare string tested 'd','e','l',... as tool names — none of them real —
        so the gate reported PASS on a trajectory that DID call delete_user.
        A security check that fails open on malformed input is worse than one
        that is absent, because the run reports green (CWE-1287/CWE-636)."""
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "delete_user", "args": {"id": 1}}]})
        slipped = self.write_json("bad.json",
                                  {"authz": {"forbidden_tools": "delete_user"}})
        self.assert_clean_error("score_authz.py", traj, slipped)

        # Control: written correctly, the same trajectory fails — so the
        # assertion above is about the SHAPE, not about an unreachable check.
        ok = self.write_json("ok.json",
                             {"authz": {"forbidden_tools": ["delete_user"]}})
        rc, out, err = run_script("score_authz.py", traj, ok)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")

    def test_the_answer_slip_no_longer_manufactures_a_failure(self):
        """The other direction, and the one the project's own comments call
        the outcome it must never produce. `must_not_contain: zebra` iterated
        as characters, and 'z','e','b','r','a' appear in ordinary prose, so a
        CORRECT answer was scored fail."""
        answer = self.tmp / "a.txt"
        answer.write_text("The invoice total is 500 dollars.", encoding="utf-8")
        slipped = self.write_json("bad.json",
                                  {"answer": {"must_not_contain": "zebra"}})
        self.assert_clean_error("score_answer.py", answer, slipped)

        ok = self.write_json("ok.json",
                             {"answer": {"must_not_contain": ["zebra"]}})
        rc, out, err = run_script("score_answer.py", answer, ok)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")

    def test_a_malformed_args_block_errors_even_when_the_tool_was_not_called(
            self):
        """Shape validation must not depend on the trajectory. Validated after
        the tool_not_called short-circuit, the same case file would error or
        not depending on what the app happened to do — so a broken case could
        sit green in the suite until the day the app started calling that
        tool."""
        traj = self.write_json("t.json", {"tool_calls": []})
        expect = self.write_json("e.json", {"args": {"never_called": "id"}})
        out = self.assert_clean_error("score_args.py", traj, expect)
        self.assertIn("expect.args.never_called", out["error"])

    def test_empty_and_null_expect_fields_are_still_no_ops(self):
        """The guard rejects wrong SHAPES, not absent ones. An omitted or null
        field means the check does not apply and must stay unscored — turning
        that into an error would break every case that scores only one layer."""
        traj = self.write_json("t.json", {"tool_calls": []})
        for expect in ({}, {"authz": None}, {"authz": {}},
                       {"authz": {"forbidden_tools": []}},
                       {"args": None}, {"args": {}}):
            with self.subTest(expect=expect):
                path = self.write_json("e.json", expect)
                for script in ("score_authz.py", "score_args.py"):
                    rc, out, err = run_script(script, traj, path)
                    self.assertEqual(rc, 0, f"{script} {expect}: {err}")
                    self.assertIn(out["verdict"], ("unscored", "pass"))

    def test_route_acceptable_scalar_is_an_error_not_a_mismatch(self):
        """score_routing takes a JSONL rather than an expect object, which is
        why it sat outside the table above and kept the hole longest — and it
        failed in the WORST direction: silently. `set("support")` is a set of
        characters, no real route name is ever in it, so a case whose observed
        route was a blessed alternate scored accuracy 0.0 and landed in the
        confusion matrix. Exit 0, a plausible-looking number, a wrong one."""
        slipped = self.write_jsonl("bad.jsonl", [
            {"case_id": "c1", "expected": "billing",
             "acceptable": "support", "observed": "support"}])
        out = self.assert_clean_error("score_routing.py", slipped)
        self.assertIn("route_acceptable", out["error"])

        # Control: written correctly, the same case PASSES as an accepted
        # alternate — so the assertion above is about the shape, and the 0.0
        # the slip produced really was manufactured.
        ok = self.write_jsonl("ok.jsonl", [
            {"case_id": "c1", "expected": "billing",
             "acceptable": ["support"], "observed": "support"}])
        rc, out, err = run_script("score_routing.py", ok)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["accuracy"], 1.0)
        self.assertEqual(out["accepted_alternates"]["count"], 1)

    # (dotted field name, script, expect object) — same family as SLIPS, but
    # with a FALSY off-shape value. These slipped through a second way: the
    # guards ran under `if value:` rather than `if value is not None`, so
    # exactly the off-shape values that are also falsy skipped validation and
    # were silently replaced by {}. The case then reported "unscored" — an
    # expectation the author wrote, never evaluated, and nothing said so.
    FALSY_SLIPS = [
        ("expect.tools", "trajectory_match.py", {"tools": []}),
        ("expect.result", "score_execution.py", {"result": []}),
        ("expect.answer", "score_answer.py", {"answer": []}),
        ("expect.format", "score_answer.py", {"format": []}),
        ("expect.format.json_schema", "score_answer.py",
         {"format": {"json_schema": []}}),
        ("expect.authz", "score_authz.py", {"authz": []}),
        ("expect.args", "score_args.py", {"args": []}),
    ]

    def test_falsy_off_shape_expect_fields_are_errors_not_silent_no_ops(self):
        for field, script, expect in self.FALSY_SLIPS:
            with self.subTest(field=field, script=script):
                path = self.write_json("e.json", expect)
                out = self.assert_clean_error(script,
                                              *self.argv_for(script, path))
                self.assertIn(field, out["error"])

    def test_absent_and_empty_object_expect_fields_stay_no_ops(self):
        """The companion to the test above, and the line it must not cross: an
        ABSENT key and an empty OBJECT are both legitimate ("this layer isn't
        scored here"), so only a wrong SHAPE may error. Getting this backwards
        would break every case that scores a single layer."""
        for expect in ({}, {"tools": None}, {"tools": {}},
                       {"result": None}, {"result": {}},
                       {"answer": None}, {"answer": {}},
                       {"format": {}}, {"format": {"json_schema": {}}}):
            with self.subTest(expect=expect):
                path = self.write_json("e.json", expect)
                for script in ("trajectory_match.py", "score_execution.py",
                               "score_answer.py"):
                    rc, out, err = run_script(script,
                                              *self.argv_for(script, path))
                    self.assertEqual(rc, 0, f"{script} {expect}: {err}")
                    self.assertIn(out["verdict"], ("unscored", "pass"))

    # Every way an expect.args entry can name a tool and assert nothing once
    # the reserved "calls" key is peeled off. Each parses as a perfectly valid
    # mapping, so the shape guards above all pass it.
    EMPTY_ARG_ENTRIES = [
        {"args": {"search": {"calls": "all"}}},
        {"args": {"search": {}}},
        {"args": {"search": None}},
    ]

    def test_arg_entry_asserting_nothing_is_an_input_error(self):
        """An entry with no arg specs scored every trajectory and left NO row
        in the output — not even tool_not_called. Alone it read "unscored",
        which the run skill catches; beside any other tool carrying a real spec
        the case reported a clean "pass" with the entry invisible. Same class
        as trajectory_match.asserted_list, one layer over."""
        for expect in self.EMPTY_ARG_ENTRIES:
            with self.subTest(expect=expect):
                path = self.write_json("e.json", expect)
                out = self.assert_clean_error(
                    "score_args.py", *self.argv_for("score_args.py", path))
                self.assertIn("expect.args.search", out["error"])

    def test_empty_arg_entry_error_does_not_depend_on_the_trajectory(self):
        """The guard sits before the tool_not_called short-circuit on purpose:
        a malformed expectation is an input error whether or not the agent
        happened to call the tool, so the same case must not pass on one trace
        and error on another."""
        called = self.write_json("called.json", {"tool_calls": [
            {"name": "search", "args": {"q": "invoices"}, "result": "ok"}]})
        never = self.write_json("never.json", {"tool_calls": [
            {"name": "other", "args": {}, "result": "ok"}]})
        path = self.write_json("e.json", {"args": {"search": {}}})
        for traj in (called, never):
            with self.subTest(traj=traj):
                self.assert_clean_error("score_args.py", traj, path)

    def test_empty_arg_entry_does_not_silently_pass_beside_a_real_spec(self):
        """The shape the bug actually took in a real case: one tool asserted
        properly, one asserted nothing, verdict "pass" and only one check row.
        Guards the regression at the verdict level, not just the exit code."""
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "search", "args": {"q": "invoices"}, "result": "ok"},
            {"name": "fetch", "args": {"id": "INV-77"}, "result": "ok"}]})
        path = self.write_json("e.json", {"args": {
            "fetch": {"id": "INV-77"}, "search": {"calls": "all"}}})
        self.assert_clean_error("score_args.py", traj, path)

    def test_real_arg_specs_alongside_calls_key_still_score(self):
        """The line the guard must not cross: "calls" beside an actual spec is
        the documented, common form and stays scorable."""
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "search", "args": {"q": "invoices"}, "result": "ok"}]})
        path = self.write_json("e.json",
                               {"args": {"search": {"calls": "all",
                                                    "q": "invoices"}}})
        rc, out, err = run_script("score_args.py", traj, path)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")

    def test_absent_args_block_and_empty_args_mapping_stay_no_ops(self):
        """expect.args: {} names no tools, so it asserts nothing about any of
        them — still a legitimate "this layer isn't scored here", unlike an
        entry that names one. The guard is per-ENTRY, not on the container."""
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "search", "args": {"q": "x"}, "result": "ok"}]})
        for expect in ({}, {"args": None}, {"args": {}}):
            with self.subTest(expect=expect):
                path = self.write_json("e.json", expect)
                rc, out, err = run_script("score_args.py", traj, path)
                self.assertEqual(rc, 0, err)
                self.assertEqual(out["verdict"], "unscored")

    def test_allowed_record_ids_still_distinguishes_absent_from_empty(self):
        """`allowed_record_ids: []` is a real allowlist meaning "nothing is in
        scope"; an absent key means "no allowlist". The shape guard must not
        collapse them — require_list returns the empty list unchanged."""
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "get", "args": {}, "result": "record INV-9"}]})
        absent = self.write_json("absent.json",
                                 {"authz": {"forbidden_tools": []}})
        empty = self.write_json("empty.json",
                                {"authz": {"allowed_record_ids": []}})

        rc, out, err = run_script("score_authz.py", traj, absent)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("allowed_record_ids",
                         [c.get("check") for c in out["checks"]])

        rc, out, err = run_script("score_authz.py", traj, empty)
        self.assertEqual(rc, 0, err)
        self.assertIn("allowed_record_ids",
                      [c.get("check") for c in out["checks"]])


class TestEmptyAllowlistIsEnforced(ScorerTest):
    """`allowed_record_ids: []` says nothing is in scope, so ANY id-shaped
    token in a tool result is out of scope. The check used to need a prefix
    from an allowed id to recognize candidates by, which an empty list cannot
    supply — so the strictest allowlist expressible was the only one that
    could never fail, on the gate whose stated purpose is that a permissive
    authz check hides a wide-open endpoint."""

    def expect(self, authz):
        return self.write_json("e.json", {"authz": authz})

    def traj(self, result):
        return self.write_json("t.json", {"tool_calls": [
            {"name": "get", "args": {}, "result": result}]})

    def test_empty_allowlist_fails_on_any_returned_record(self):
        rc, out, err = run_script(
            "score_authz.py", self.traj({"records": [{"id": "e_881"}]}),
            self.expect({"allowed_record_ids": []}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")
        check = out["checks"][0]
        self.assertEqual(check["status"], "fail")
        self.assertEqual(check["leaked_ids"], ["e_881"])

    def test_empty_allowlist_passes_when_no_record_came_back(self):
        rc, out, err = run_script(
            "score_authz.py", self.traj({"records": [], "count": 0}),
            self.expect({"allowed_record_ids": []}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")

    def test_empty_allowlist_still_unscorable_without_captured_results(self):
        # The no-evidence case is unchanged: absence cannot be verified from a
        # result that was never captured, and claiming otherwise would be the
        # manufactured verdict this scorer refuses in both directions.
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "get", "args": {}, "result": None}]})
        rc, out, err = run_script("score_authz.py", traj,
                                  self.expect({"allowed_record_ids": []}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["checks"][0]["status"], "unscorable")

    def test_non_id_shaped_allowlist_is_still_unscorable(self):
        # Unchanged, and deliberately distinct from the empty case: allowed ids
        # that are not id-shaped leave no prefix to recognize an out-of-scope
        # id BY, and free-text scanning would flag arbitrary tokens.
        rc, out, err = run_script(
            "score_authz.py", self.traj({"records": [{"id": "e_881"}]}),
            self.expect({"allowed_record_ids": ["everything"]}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["checks"][0]["status"], "unscorable")
        self.assertIn("not id-shaped", out["checks"][0]["reason"])


class TestErroredToolCalls(ScorerTest):
    """A tool call that FAILED must not be indistinguishable from one whose
    result simply was not captured. The GenAI convention records
    gen_ai.tool.call.result only "if execution was successful", so absence is
    the app's failure signal as well as the exporter's — and reading it only
    the second way reported a broken tool as a tracing misconfiguration, while
    the trajectory layer scored the errored call as a satisfied expectation."""

    @staticmethod
    def tool_span(span_id, tool, attrs=(), status=None):
        s = TestNormalizeTrace.span(
            "t1", span_id, "execute_tool", start=2,
            extra_attrs=[{"key": "gen_ai.tool.name",
                          "value": {"stringValue": tool}},
                         {"key": "gen_ai.tool.call.id",
                          "value": {"stringValue": f"call_{span_id}"}},
                         *attrs])
        if status is not None:
            s["status"] = status
        return s

    def doc(self, spans):
        return self.write_json("spans.json", {"resourceSpans": [
            {"scopeSpans": [{"spans": spans}]}]})

    def test_error_type_attribute_is_recorded_on_the_call(self):
        spans = [self.tool_span("b", "get_invoice", attrs=[
            {"key": "error.type", "value": {"stringValue": "500"}}])]
        rc, out, err = run_script("normalize_trace.py", self.doc(spans),
                                  "--trace-id", "t1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["trajectory"]["tool_calls"][0]["error"], "500")
        self.assertEqual(out["checks"]["tool_calls_errored"],
                         [{"call_id": "call_b", "tool": "get_invoice",
                           "error": "500"}])

    def test_errored_call_is_not_counted_as_uncaptured_content(self):
        # The whole point: a failed call must leave tool_calls_without_result
        # alone, or the run blames the exporter for the app's failure.
        spans = [self.tool_span("b", "get_invoice", attrs=[
            {"key": "error.type", "value": {"stringValue": "timeout"}}])]
        rc, out, _ = run_script("normalize_trace.py", self.doc(spans),
                                "--trace-id", "t1")
        self.assertEqual(out["checks"]["tool_calls_without_result"], [])

    def test_otlp_span_status_is_the_fallback(self):
        # Exporters set span status whether or not they populate the opt-in
        # GenAI attribute, so the status is read too — enum name or integer.
        for code in (2, "STATUS_CODE_ERROR"):
            with self.subTest(code=code):
                spans = [self.tool_span(
                    "b", "get_invoice",
                    status={"code": code, "message": "upstream 503"})]
                rc, out, _ = run_script("normalize_trace.py", self.doc(spans),
                                        "--trace-id", "t1")
                self.assertEqual(out["trajectory"]["tool_calls"][0]["error"],
                                 "upstream 503")

    def test_successful_call_has_no_error(self):
        spans = [self.tool_span("b", "get_invoice", attrs=[
            {"key": "gen_ai.tool.call.result",
             "value": {"stringValue": "ok"}}])]
        rc, out, _ = run_script("normalize_trace.py", self.doc(spans),
                                "--trace-id", "t1")
        self.assertIsNone(out["trajectory"]["tool_calls"][0]["error"])
        self.assertEqual(out["checks"]["tool_calls_errored"], [])

    def test_trajectory_layer_reports_errored_calls_without_failing(self):
        # Default stays "were these tools used": flipping it would re-score
        # every existing dataset. But it can no longer be invisible.
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "get_invoice", "args": {}, "result": None,
             "error": "500"}]})
        expect = self.write_json("e.json", {"tools": {
            "subset": ["get_invoice"]}})
        rc, out, err = run_script("trajectory_match.py", traj, expect)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["errored_calls"],
                         [{"index": 0, "tool": "get_invoice",
                           "error": "500"}])
        self.assertIn("not whether they succeeded", out["warning"])

    def test_trajectory_layer_can_be_gated_on_errors(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "get_invoice", "args": {}, "result": None,
             "error": "500"}]})
        expect = self.write_json("e.json", {"tools": {
            "subset": ["get_invoice"]}})
        rc, out, err = run_script("trajectory_match.py", traj, expect,
                                  "--fail-on-errored-calls")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")
        self.assertNotIn("warning", out)

    def test_score_args_names_the_error_not_the_exporter(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "lookup", "args": {"q": "x"}, "result": None,
             "error": "500"},
            {"name": "charge", "args": {"id": "INV-1042"}, "result": None}]})
        expect = self.write_json("e.json", {"args": {
            "charge": {"id": "from_tool_result"}}})
        rc, out, err = run_script("score_args.py", traj, expect)
        self.assertEqual(rc, 0, err)
        check = out["checks"][0]
        self.assertEqual(check["status"], "unscorable")
        self.assertIn("errored", check["reason"])
        self.assertNotIn("content capture", check["reason"])

    def test_content_capture_diagnosis_survives_when_nothing_errored(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "lookup", "args": {"q": "x"}, "result": None},
            {"name": "charge", "args": {"id": "INV-1042"}, "result": None}]})
        expect = self.write_json("e.json", {"args": {
            "charge": {"id": "from_tool_result"}}})
        rc, out, _ = run_script("score_args.py", traj, expect)
        self.assertIn("content capture", out["checks"][0]["reason"])


class TestTraceIdDiagnosability(ScorerTest):
    """Zero spans for the trace has two very different causes — the app never
    emitted one, or --trace-id was wrong — and both used to print exactly
    `spans_for_trace: 0` and score every case INFRA_INCOMPLETE."""

    def doc(self, spans):
        return self.write_json("spans.json", {"resourceSpans": [
            {"scopeSpans": [{"spans": spans}]}]})

    def test_wrong_trace_id_is_named_as_such(self):
        spans = [TestNormalizeTrace.span("t1", "a", "invoke_agent")]
        rc, out, err = run_script("normalize_trace.py", self.doc(spans),
                                  "--trace-id", "typo")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["status"], "incomplete")
        self.assertEqual(out["checks"]["spans_for_trace"], 0)
        self.assertEqual(out["checks"]["spans_in_file"], 1)
        self.assertIn("--trace-id", out["checks"]["trace_id_note"])

    def test_genuinely_empty_file_gets_no_misleading_hint(self):
        rc, out, err = run_script("normalize_trace.py", self.doc([]),
                                  "--trace-id", "t1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["status"], "incomplete")
        self.assertEqual(out["checks"]["spans_in_file"], 0)
        self.assertNotIn("trace_id_note", out["checks"])


class TestScoreRoutingMacroSupport(ScorerTest):
    """macro_f1 averages over TARGETS, and a prediction-only label is not a
    target. __no_route__ (and any route name the app invented) has no expected
    case behind it, so its F1 is a structural 0.0 that dragged the average
    down one slot per unrouted case and then tripped the macro/micro gap
    warning — sending the reader to hunt a weak minority target that does not
    exist."""

    def test_one_unrouted_case_does_not_crater_macro_f1(self):
        rows = [
            {"case_id": "c1", "expected": "billing", "observed": "billing"},
            {"case_id": "c2", "expected": "billing", "observed": None},
            {"case_id": "c3", "expected": "support", "observed": "support"},
            {"case_id": "c4", "expected": "support", "observed": "support"},
        ]
        rc, out, err = run_script("score_routing.py",
                                  self.write_jsonl("r.jsonl", rows))
        self.assertEqual(rc, 0, err)
        # billing: p=1.0 r=0.5 f1=0.6667; support: 1.0. __no_route__ carries a
        # row (it is a real finding) but no support, so it is not averaged.
        self.assertAlmostEqual(out["macro_f1"], 0.8333, places=3)
        self.assertEqual(out["spurious_labels"]["labels"], ["__no_route__"])
        self.assertIn("__no_route__", out["per_target"])
        self.assertEqual(out["per_target"]["__no_route__"]["support"], 0)
        self.assertNotIn("warning", out)

    def test_a_hallucinated_route_name_is_listed_not_averaged(self):
        rows = [
            {"case_id": "c1", "expected": "billing", "observed": "billing"},
            {"case_id": "c2", "expected": "billing", "observed": "wibble"},
        ]
        rc, out, err = run_script("score_routing.py",
                                  self.write_jsonl("r.jsonl", rows))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["spurious_labels"]["labels"], ["wibble"])
        self.assertAlmostEqual(out["macro_f1"], 0.6667, places=3)

    def test_labels_with_support_are_all_still_averaged(self):
        rows = [
            {"case_id": "c1", "expected": "billing", "observed": "billing"},
            {"case_id": "c2", "expected": "support", "observed": "billing"},
        ]
        rc, out, err = run_script("score_routing.py",
                                  self.write_jsonl("r.jsonl", rows))
        self.assertEqual(rc, 0, err)
        # support has support=1 (and f1 0.0): a real target that failed stays
        # in the average — only zero-support labels leave it.
        self.assertEqual(out["spurious_labels"]["labels"], [])
        self.assertAlmostEqual(out["macro_f1"], 0.3333, places=3)


class TestStatsCaseAttrition(ScorerTest):
    """The pairing is an intersection, and what it drops is not a random
    sample: a case the candidate crashed on is filtered upstream as infra and
    vanishes, grading the candidate on the subset it survived. A run that lost
    a third of its cases and one that lost none used to print identical
    output."""

    def rows(self, ids, verdict="pass"):
        return [{"case_id": i, "verdict": verdict} for i in ids]

    def test_dropped_cases_are_counted_and_named(self):
        base = self.write_jsonl("b.jsonl", self.rows(["c1", "c2", "c3"]))
        cand = self.write_jsonl("c.jsonl", self.rows(["c2", "c3", "c9"]))
        rc, out, err = run_script("stats.py", base, cand)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["n"], 2)
        self.assertEqual(out["cases_only_in_baseline"], 1)
        self.assertEqual(out["cases_only_in_candidate"], 1)
        self.assertEqual(out["cases_only_in_baseline_ids"], ["c1"])
        self.assertEqual(out["cases_only_in_candidate_ids"], ["c9"])

    def test_heavy_attrition_warns_about_selection_bias(self):
        base = self.write_jsonl("b.jsonl", self.rows([f"c{i}"
                                                      for i in range(10)]))
        cand = self.write_jsonl("c.jsonl", self.rows([f"c{i}"
                                                      for i in range(5)]))
        rc, out, err = run_script("stats.py", base, cand)
        self.assertEqual(rc, 0, err)
        self.assertIn("case_attrition_warning", out)
        self.assertIn("SELECTION BIAS", out["case_attrition_warning"])
        self.assertIn("infra", out["case_attrition_warning"])

    def test_matched_case_sets_carry_no_warning(self):
        p = self.write_jsonl("b.jsonl", self.rows([f"c{i}" for i in range(10)]))
        rc, out, err = run_script("stats.py", p, p)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("case_attrition_warning", out)
        self.assertEqual(out["cases_only_in_baseline"], 0)
        self.assertEqual(out["cases_only_in_candidate"], 0)

    def test_the_decision_rule_is_unchanged_by_attrition(self):
        # The gate still runs on the pairs it has; attrition is reported, not
        # acted on.
        base = self.write_jsonl("b.jsonl", [
            {"case_id": f"c{i}", "verdict": "fail"} for i in range(6)])
        cand = self.write_jsonl("c.jsonl", [
            {"case_id": f"c{i}", "verdict": "pass"} for i in range(5)])
        rc, out, err = run_script("stats.py", base, cand)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["n"], 5)
        self.assertTrue(out["keep"])
        self.assertIn("case_attrition_warning", out)

    def test_reported_ids_are_capped(self):
        base = self.write_jsonl("b.jsonl", self.rows(
            ["shared"] + [f"b{i:03d}" for i in range(50)]))
        cand = self.write_jsonl("c.jsonl", self.rows(["shared"]))
        rc, out, err = run_script("stats.py", base, cand)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["cases_only_in_baseline"], 50)
        self.assertEqual(len(out["cases_only_in_baseline_ids"]), 20)


class TestScoreArgsCallScope(ScorerTest):
    """An expectation about a tool call is not an expectation about every call
    of that tool. An agent that searches (q=invoices) and then pages
    (limit=10) calls the same tool twice with different arguments; scoring
    every call against every spec manufactured a failure out of correct
    behavior. Default scope is therefore "any"; "all" is the old semantics,
    still available to authors who mean it."""

    def two_calls(self):
        return self.write_json("t.json", {"tool_calls": [
            {"name": "search", "args": {"q": "invoices"}, "result": "ok"},
            {"name": "search", "args": {"limit": 10}, "result": "ok"}]})

    def expect(self, spec):
        return self.write_json("e.json", {"args": {"search": spec}})

    def test_default_any_passes_when_one_call_satisfies_it(self):
        rc, out, err = run_script("score_args.py", self.two_calls(),
                                  self.expect({"q": "invoices"}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual([c["status"] for c in out["checks"]], ["pass"])
        self.assertEqual(out["checks"][0]["call_index"], 0)
        self.assertEqual(out["checks"][0]["calls"], "any")

    def test_calls_all_preserves_the_old_strict_semantics(self):
        rc, out, err = run_script("score_args.py", self.two_calls(),
                                  self.expect({"q": "invoices",
                                               "calls": "all"}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual([c["status"] for c in out["checks"]],
                         ["pass", "fail"])

    def test_calls_first_checks_only_the_first_call(self):
        rc, out, err = run_script("score_args.py", self.two_calls(),
                                  self.expect({"q": "invoices",
                                               "calls": "first"}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual([c["call_index"] for c in out["checks"]], [0])

    def test_calls_first_can_still_fail(self):
        rc, out, err = run_script("score_args.py", self.two_calls(),
                                  self.expect({"limit": 10,
                                               "calls": "first"}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")

    def test_any_requires_one_call_to_satisfy_every_spec_together(self):
        # Not "each spec matched by some call": the specs are evaluated per
        # call, so "the call that searched for invoices also passed limit=10"
        # stays expressible and stays false here.
        rc, out, err = run_script("score_args.py", self.two_calls(),
                                  self.expect({"q": "invoices",
                                               "limit": 10}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual({c["call_index"] for c in out["checks"]}, {0})

    def test_any_reports_one_call_not_every_call(self):
        rc, out, err = run_script("score_args.py", self.two_calls(),
                                  self.expect({"q": "receipts"}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual(len(out["checks"]), 1)

    def test_a_single_call_is_unaffected_by_the_default(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "search", "args": {"q": "invoices"}, "result": "ok"}]})
        rc, out, err = run_script("score_args.py", traj,
                                  self.expect({"q": "invoices"}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")

    def test_an_unknown_scope_is_a_clean_error(self):
        # Three scopes give three different verdicts on one trajectory, so a
        # typo must never quietly pick one.
        self.assert_clean_error("score_args.py", self.two_calls(),
                                self.expect({"q": "x", "calls": "every"}))

    def test_calls_is_not_treated_as_an_argument_name(self):
        rc, out, err = run_script("score_args.py", self.two_calls(),
                                  self.expect({"calls": "all",
                                               "q": "invoices"}))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("calls", [c.get("arg") for c in out["checks"]])


class TestScoreExecutionLargeRowSets(ScorerTest):
    """Result-set size is app data, not case authoring. The recursive
    augmenting-path search recursed once per row along a path, so a large
    identical bag exited 1 with a RecursionError traceback — outside the
    exit-2 contract, and read by the run skill as an infra failure rather
    than as a comparison."""

    def score(self, actual, expect):
        return run_script("score_execution.py",
                          self.write_json("a.json", actual),
                          self.write_json("e.json", expect))

    def test_two_thousand_identical_rows_compare_without_error(self):
        rows = [{"id": 1, "name": "same"} for _ in range(2000)]
        rc, out, err = self.score({"rows": rows}, {"result": {"rows": rows}})
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out["verdict"], "pass")

    def test_a_long_augmenting_chain_still_matches(self):
        # Distinct rows in reverse order: a bag match that the greedy pass
        # cannot settle row-by-row, exercising the iterative path itself.
        expected = [{"id": i} for i in range(400)]
        actual = list(reversed(expected))
        rc, out, err = self.score({"rows": actual},
                                  {"result": {"rows": expected}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")

    def test_a_large_tolerant_match_runs_the_matching_itself(self):
        # Cell-for-cell unequal, so the identical-bag fast path cannot settle
        # it: this one goes through the augmenting-path search at a size that
        # used to exhaust the interpreter's stack.
        expected = [{"v": float(i)} for i in range(1200)]
        actual = [{"v": i + 0.4} for i in range(1200)]
        rc, out, err = self.score(
            {"rows": actual},
            {"result": {"rows": expected, "float_tolerance": 0.5}})
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(out["verdict"], "pass")

    def test_a_genuine_large_mismatch_is_still_a_fail(self):
        expected = [{"id": i} for i in range(500)]
        actual = [{"id": i} for i in range(1, 501)]
        rc, out, err = self.score({"rows": actual},
                                  {"result": {"rows": expected}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")


class TestNormalizeTraceCallIdentity(ScorerTest):
    """A tool call with no gen_ai.tool.call.id is still a tool call. Filtering
    the missing-results diagnostic on a truthy call_id dropped exactly the
    traces where NO call carries one, so the least instrumented trace produced
    the most confident possible report: "every result was captured"."""

    def doc(self, spans):
        return self.write_json("spans.json", {"resourceSpans": [
            {"scopeSpans": [{"spans": spans}]}]})

    def tool_span(self, span_id, tool, attrs=()):
        return TestNormalizeTrace.span(
            "t1", span_id, "execute_tool", start=2,
            extra_attrs=[{"key": "gen_ai.tool.name",
                          "value": {"stringValue": tool}}, *attrs])

    def test_calls_without_ids_still_appear_in_the_diagnostic(self):
        spans = [self.tool_span("b", "get_invoice"),
                 self.tool_span("c", "get_customer")]
        rc, out, err = run_script("normalize_trace.py", self.doc(spans),
                                  "--trace-id", "t1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["checks"]["tool_calls_without_result"],
                         ["index:0", "index:1"])

    def test_a_present_call_id_is_still_preferred(self):
        spans = [self.tool_span("b", "get_invoice", attrs=[
            {"key": "gen_ai.tool.call.id",
             "value": {"stringValue": "call_1"}}]),
            self.tool_span("c", "get_customer")]
        rc, out, err = run_script("normalize_trace.py", self.doc(spans),
                                  "--trace-id", "t1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["checks"]["tool_calls_without_result"],
                         ["call_1", "index:1"])

    def test_a_captured_result_still_stays_out_of_the_list(self):
        spans = [self.tool_span("b", "get_invoice", attrs=[
            {"key": "gen_ai.tool.call.result",
             "value": {"stringValue": "ok"}}])]
        rc, out, err = run_script("normalize_trace.py", self.doc(spans),
                                  "--trace-id", "t1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["checks"]["tool_calls_without_result"], [])


class TestScoreAnswerHonestVerdict(ScorerTest):
    """Two ways a verdict overstated what was measured: a "pass" sitting on
    top of checks that never ran, and an enum satisfied by bool/int
    confusion (True == 1 in Python, the same trap the type check already
    refuses)."""

    def score(self, answer, expect):
        return run_script("score_answer.py",
                          self.write_text("a.txt", answer),
                          self.write_json("e.json", expect))

    def write_text(self, name, text):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_pass_with_unscorable_checks_is_flagged(self):
        rc, out, err = self.score("the total is 42", {"answer": {
            "must_contain": ["42", "/(a+)+++/"]}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["unscorable"], 1)
        self.assertTrue(out["partially_unscored"])

    def test_a_fully_scored_pass_is_not_flagged(self):
        rc, out, err = self.score("the total is 42",
                                  {"answer": {"must_contain": ["42"]}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")
        self.assertFalse(out["partially_unscored"])

    def test_a_fail_is_never_flagged_partially_unscored(self):
        rc, out, err = self.score("the total is 42", {"answer": {
            "must_contain": ["99", "/(a+)+++/"]}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")
        self.assertFalse(out["partially_unscored"])

    def test_true_does_not_satisfy_an_integer_enum(self):
        rc, out, err = self.score('{"v": true}', {"format": {"json_schema": {
            "properties": {"v": {"enum": [1]}}}}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")
        self.assertIn("not in enum", out["checks"][0]["reason"])

    def test_one_does_not_satisfy_a_boolean_enum(self):
        rc, out, err = self.score('{"v": 1}', {"format": {"json_schema": {
            "properties": {"v": {"enum": [True]}}}}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "fail")

    def test_matching_enum_values_still_pass(self):
        for answer, allowed in (('{"v": 1}', [1]),
                                ('{"v": true}', [True]),
                                ('{"v": "a"}', ["a", "b"])):
            with self.subTest(answer=answer):
                rc, out, err = self.score(answer, {"format": {"json_schema": {
                    "properties": {"v": {"enum": allowed}}}}})
                self.assertEqual(rc, 0, err)
                self.assertEqual(out["verdict"], "pass")


class TestStatsSubMdeKeep(ScorerTest):
    """A keep whose observed delta is below the run's own minimum detectable
    effect must SAY so. Neither existing guard covers it: `gate_note` fires
    only when the sign test is not significant, and `advice` is gated behind
    `not keep` — so the audit's shape (90/100 -> 95/100, b01=5, b10=0) was
    kept, significant at p=0.031, delta 0.05 against an MDE of 0.056, and
    printed no caveat at all."""

    def paired(self, base_pass, cand_pass, n):
        base = [{"case_id": f"c{i:03d}",
                 "verdict": "pass" if i < base_pass else "fail"}
                for i in range(n)]
        cand = [{"case_id": f"c{i:03d}",
                 "verdict": "pass" if i < cand_pass else "fail"}
                for i in range(n)]
        return (self.write_jsonl("b.jsonl", base),
                self.write_jsonl("c.jsonl", cand))

    def test_the_audit_repro_now_carries_the_caveat(self):
        b, c = self.paired(90, 95, 100)
        rc, out, err = run_script("stats.py", b, c)
        self.assertEqual(rc, 0, err)
        # The exact shape from AUDIT-2026-09-06.md 9b(1), asserted so a future
        # change to the MDE or the posterior cannot quietly move this run out
        # of the sub-MDE band and leave the test passing vacuously.
        self.assertTrue(out["keep"])
        self.assertEqual(out["delta"], 0.05)
        self.assertEqual(out["min_detectable_effect_at_this_n"], 0.056)
        self.assertTrue(out["sign_test_significant_at_alpha"])
        self.assertNotIn("gate_note", out)
        self.assertNotIn("advice", out)
        self.assertIn("sub_mde_keep", out)
        self.assertIn("not proven", out["sub_mde_keep"].lower())

    def test_a_keep_above_the_mde_carries_no_caveat(self):
        # 60/100 -> 95/100: the effect is far larger than anything this n
        # cannot resolve, so the note must not fire on every keep.
        b, c = self.paired(60, 95, 100)
        rc, out, err = run_script("stats.py", b, c)
        self.assertEqual(rc, 0, err)
        self.assertTrue(out["keep"])
        self.assertGreater(abs(out["delta"]),
                           out["min_detectable_effect_at_this_n"])
        self.assertNotIn("sub_mde_keep", out)

    def test_a_sub_mde_reject_gets_advice_not_the_keep_caveat(self):
        # The other side of the same band: not kept, so `advice` owns it and
        # the two keys never both fire.
        b, c = self.paired(90, 91, 100)
        rc, out, err = run_script("stats.py", b, c)
        self.assertEqual(rc, 0, err)
        self.assertFalse(out["keep"])
        self.assertIn("advice", out)
        self.assertNotIn("sub_mde_keep", out)

    def test_the_caveat_never_changes_the_decision(self):
        # Emitted BESIDE keep, never instead of it: the gate is the exact
        # posterior, which the MDE (a normal-approximation planning figure)
        # does not enter.
        b, c = self.paired(90, 95, 100)
        rc, out, _ = run_script("stats.py", b, c)
        self.assertTrue(out["keep"])
        self.assertGreaterEqual(out["p_candidate_better"], out["threshold"])

    def test_optimize_skill_surfaces_it(self):
        # The number is only useful if the skill that reads stats.py reports
        # it; the audit's finding was as much about the silent report as the
        # missing key.
        spec = (pathlib.Path(__file__).resolve().parent.parent /
                "skills" / "optimize" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("sub_mde_keep", spec)
        self.assertIn("gate_note", spec)


class TestUnicodeNormalization(ScorerTest):
    """NFD vs NFC: two spellings of the same characters, identical on screen,
    unequal as code points. Every scorer that compares a case-authored string
    against app-produced text has to normalize, or it manufactures a failure
    whose own evidence shows the two sides as the same text (and, on the authz
    gate, manufactures a PASS). macOS emits NFD, most web input is NFC, so no
    exotic data is needed to hit this.

    NFD is written explicitly here rather than pasted, because a decomposed
    literal is invisible in a diff and any editor may silently recompose it.
    """

    NFC = "caf\u00e9"          # e-acute as one code point
    NFD = "cafe\u0301"         # e + combining acute
    ARABIC_NFC = "\u0623\u062d\u0645\u062f"       # alef-with-hamza + hmd
    ARABIC_NFD = "\u0627\u0654\u062d\u0645\u062f"  # alef + combining hamza

    def test_the_two_spellings_really_are_different_strings(self):
        # The premise of every assertion below. If this ever fails the rest
        # are testing nothing.
        self.assertNotEqual(self.NFC, self.NFD)
        self.assertNotEqual(self.ARABIC_NFC, self.ARABIC_NFD)

    # --- score_answer.py: the scorer the audit found it in -----------------

    def answer_case(self, answer, expect):
        a = self.tmp / "a.txt"
        a.write_text(answer, encoding="utf-8")
        return run_script("score_answer.py", a, self.write_json("e.json",
                                                                expect))

    def test_must_contain_nfd_matches_an_nfc_answer(self):
        # AUDIT-2026-09-06.md 9b(2), verbatim: this returned `fail`.
        rc, out, err = self.answer_case(
            f"We visited the {self.NFC} downtown.",
            {"answer": {"must_contain": [self.NFD]}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")

    def test_must_contain_nfc_matches_an_nfd_answer(self):
        # The mirror direction — which side is decomposed is an accident of
        # whose editor wrote it.
        rc, out, err = self.answer_case(
            f"We visited the {self.NFD} downtown.",
            {"answer": {"must_contain": [self.NFC]}})
        self.assertEqual(out["verdict"], "pass")

    def test_must_not_contain_still_catches_the_other_spelling(self):
        # The permissive direction: a forbidden phrase must not hide behind a
        # Unicode form.
        rc, out, err = self.answer_case(
            f"The password is {self.NFD}.",
            {"answer": {"must_not_contain": [self.NFC]}})
        self.assertEqual(out["verdict"], "fail")

    def test_arabic_hamza_form_matches(self):
        # The field-test app is Gulf-market; alef forms are the common case
        # and are far more frequent than the accent example.
        rc, out, err = self.answer_case(
            f"\u0627\u0644\u0639\u0645\u064a\u0644 {self.ARABIC_NFC}",
            {"answer": {"must_contain": [self.ARABIC_NFD]}})
        self.assertEqual(out["verdict"], "pass")

    def test_regex_entry_matches_a_decomposed_answer_and_says_so(self):
        # The haystack is normalized; the PATTERN deliberately is not, so the
        # check has to declare the asymmetry.
        rc, out, err = self.answer_case(
            f"visited {self.NFD} today",
            {"answer": {"must_contain": [f"/{self.NFC}/"]}})
        self.assertEqual(out["verdict"], "pass")
        self.assertIn("NFC-normalized", out["checks"][0]["note"])

    def test_a_decomposed_pattern_is_warned_about_by_name(self):
        # This one legitimately cannot match, and the note is the only place
        # the reader can learn why — "not found in answer" is true and
        # useless.
        rc, out, err = self.answer_case(
            f"visited {self.NFC} today",
            {"answer": {"must_contain": [f"/{self.NFD}/"]}})
        self.assertEqual(out["verdict"], "fail")
        self.assertIn("not in NFC form", out["checks"][0]["note"])

    def test_an_ascii_regex_still_gets_the_plain_note(self):
        rc, out, err = self.answer_case(
            "total: 42 units", {"answer": {"must_contain": ["/\\d+ units/"]}})
        self.assertEqual(out["verdict"], "pass")
        self.assertNotIn("WARNING", out["checks"][0]["note"])

    def test_json_schema_enum_and_required_key_normalize(self):
        rc, out, err = self.answer_case(
            json.dumps({self.NFC: self.NFC}, ensure_ascii=False),
            {"format": {"json_schema": {
                "type": "object", "required": [self.NFD],
                "properties": {self.NFD: {"enum": [self.NFD]}}}}})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass", out["checks"])

    # --- score_args.py ----------------------------------------------------

    def test_literal_arg_expectation_normalizes(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "search", "args": {"q": self.NFC}}]})
        rc, out, err = run_script(
            "score_args.py", traj,
            self.write_json("e.json", {"args": {"search": {"q": self.NFD}}}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass", out["checks"])

    def test_arg_name_normalizes(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "search", "args": {self.NFC: "x"}}]})
        rc, out, err = run_script(
            "score_args.py", traj,
            self.write_json("e.json",
                            {"args": {"search": {self.NFD: "present"}}}))
        self.assertEqual(out["verdict"], "pass", out["checks"])

    def test_tool_name_normalizes(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": f"get_{self.NFC}", "args": {"id": "INV-1"}}]})
        rc, out, err = run_script(
            "score_args.py", traj,
            self.write_json("e.json",
                            {"args": {f"get_{self.NFD}": {"id": "INV-1"}}}))
        self.assertNotEqual(out["checks"][0]["status"], "tool_not_called")
        self.assertEqual(out["verdict"], "pass")

    def test_provenance_finds_a_non_ascii_value_in_a_json_result(self):
        # Not an NFC bug: json.dumps defaults to ensure_ascii=True, so the
        # haystack held the escape "caf\u00e9" and an argument the app copied
        # verbatim out of that result was reported "possible hallucinated arg".
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "lookup", "args": {}, "result": {"name": self.NFC}},
            {"name": "book", "args": {"name": self.NFC}}]})
        rc, out, err = run_script(
            "score_args.py", traj,
            self.write_json("e.json",
                            {"args": {"book": {"name": "from_tool_result"}}}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass", out["checks"])

    def test_provenance_normalizes_across_the_two_forms(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": "lookup", "args": {}, "result": {"name": self.NFD}},
            {"name": "book", "args": {"name": self.NFC}}]})
        rc, out, err = run_script(
            "score_args.py", traj,
            self.write_json("e.json",
                            {"args": {"book": {"name": "from_tool_result"}}}))
        self.assertEqual(out["verdict"], "pass", out["checks"])

    # --- score_execution.py -----------------------------------------------

    def test_scalar_execution_answer_normalizes(self):
        # casefold does not compose, so this scorer's existing quasi-exact
        # normalization was not enough on its own.
        rc, out, err = run_script(
            "score_execution.py",
            self.write_json("a.json", {"scalar": self.NFC.upper()}),
            self.write_json("e.json", {"result": {"scalar": self.NFD}}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass", out["checks"])

    def test_row_cells_normalize(self):
        rc, out, err = run_script(
            "score_execution.py",
            self.write_json("a.json", {"rows": [{"city": self.NFC}]}),
            self.write_json("e.json",
                            {"result": {"rows": [{"city": self.NFD}]}}))
        self.assertEqual(out["verdict"], "pass", out["checks"])

    # --- score_routing.py -------------------------------------------------

    def test_route_labels_normalize(self):
        rows = [{"case_id": "c1", "expected": self.NFD, "observed": self.NFC}]
        rc, out, err = run_script("score_routing.py",
                                  self.write_jsonl("r.jsonl", rows))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["accuracy"], 1.0)
        # And one label, not two rows of the confusion matrix that print the
        # same.
        self.assertEqual(len(out["per_target"]), 1)

    # --- trajectory_match.py ----------------------------------------------

    def test_expected_tool_names_normalize(self):
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": f"get_{self.NFC}"}]})
        rc, out, err = run_script(
            "trajectory_match.py", traj,
            self.write_json("e.json",
                            {"tools": {"subset": [f"get_{self.NFD}"]}}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(out["recall"], 1.0)

    def test_forbidden_tool_names_normalize(self):
        # The permissive direction again: a forbidden tool that DID fire.
        traj = self.write_json("t.json", {"tool_calls": [
            {"name": f"delete_{self.NFD}"}]})
        rc, out, err = run_script(
            "trajectory_match.py", traj,
            self.write_json("e.json",
                            {"tools": {"forbidden": [f"delete_{self.NFC}"]}}))
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual(len(out["forbidden_violations"]), 1)


class TestOosRouteValidation(ScorerTest):
    """--oos-route is LLM-supplied from profile.yaml's oos_handling, and a
    name that matches nothing renamed nothing: the OOS leakage block reported
    precision/recall null and a note claiming "0 OOS case(s) in this run" over
    data holding two. Exit 0, a security-adjacent metric silently off, and a
    report factually wrong about its own input (AUDIT-2026-09-06.md 6)."""

    ROWS = [{"case_id": "r1", "expected": "billing", "observed": "billing"},
            {"case_id": "r2", "expected": "support", "observed": "support"},
            {"case_id": "r3", "expected": "none", "observed": "none"},
            {"case_id": "r4", "expected": "none", "observed": "none"}]

    def test_an_unmatched_route_is_a_clean_error_listing_the_labels(self):
        out = self.assert_clean_error(
            "score_routing.py", self.write_jsonl("r.jsonl", self.ROWS),
            "--oos-route", "TOTAL_NONSENSE_XYZ")
        self.assertIn("--oos-route", out["error"])
        # The labels present are the fix: with them in the message the correct
        # value is usually obvious.
        for label in ("billing", "support", "none"):
            self.assertIn(label, out["error"])

    def test_the_correct_route_still_measures_the_metric(self):
        rc, out, err = run_script("score_routing.py",
                                  self.write_jsonl("r.jsonl", self.ROWS),
                                  "--oos-route", "none")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["oos"]["precision"], 1.0)
        self.assertEqual(out["oos"]["recall"], 1.0)
        self.assertEqual(out["oos"]["support"], 2)

    def test_a_label_present_only_as_observed_is_accepted(self):
        # The app routed there even though no case expects it — a real run
        # (and a finding), not a typo.
        rows = [{"case_id": "r1", "expected": "billing", "observed": "refuse"}]
        rc, out, err = run_script("score_routing.py",
                                  self.write_jsonl("r.jsonl", rows),
                                  "--oos-route", "refuse")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["oos"]["support"], 0)

    def test_a_label_present_only_in_the_acceptable_set_is_accepted(self):
        rows = [{"case_id": "r1", "expected": "billing",
                 "acceptable": ["billing", "refuse"], "observed": "billing"}]
        rc, out, err = run_script("score_routing.py",
                                  self.write_jsonl("r.jsonl", rows),
                                  "--oos-route", "refuse")
        self.assertEqual(rc, 0, err)

    def test_the_case_format_aliases_are_recognized(self):
        # `route`/`route_acceptable` are accepted as input aliases, so the
        # check has to read them too — otherwise a correctly spelled route is
        # rejected on the alias spelling of the file.
        rows = [{"case_id": "r1", "route": "billing", "observed": "billing"},
                {"case_id": "r2", "route": "none", "observed": "none"}]
        rc, out, err = run_script("score_routing.py",
                                  self.write_jsonl("r.jsonl", rows),
                                  "--oos-route", "none")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["oos"]["support"], 1)

    def test_the_route_is_matched_in_nfc(self):
        rows = [{"case_id": "r1", "expected": "caf\u00e9", "observed": "caf\u00e9"}]
        rc, out, err = run_script("score_routing.py",
                                  self.write_jsonl("r.jsonl", rows),
                                  "--oos-route", "cafe\u0301")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["oos"]["support"], 1)

    def test_the_run_skill_documents_the_precondition(self):
        spec = RUN_SKILL.read_text(encoding="utf-8")
        self.assertIn("--oos-route", spec)
        self.assertIn("exits 2", spec)


if __name__ == "__main__":
    unittest.main()


RUN_SKILL = SCRIPTS.parent / "skills" / "run" / "SKILL.md"


class TestRunArtifactContract(ScorerTest):
    """run/SKILL.md requires every run to write BOTH `verdicts.jsonl` (the full
    per-case record) and `verdicts_for_stats.jsonl` (pass/fail rows only), and
    names the latter as the paired input for a baseline diff. Nothing checked
    that the two-file split was actually necessary, or that the reduced file
    is what stats.py accepts — and a spec whose only enforcement is a failed
    diff months later is the kind that quietly rots.

    The row shapes below are copied from the field-test run's real artifacts,
    not invented, so this pins the shape runs actually emit."""

    FULL_ROW = {"case_id": "units-happy", "set": "smoke", "category": "happy",
                "gating": True, "http_status": 200, "latency_s": 9.54,
                "layers": {"http_contract": "pass", "routing_proxy": "pass"},
                "verdict": "pass"}
    INFRA_ROW = {"case_id": "units-flaky", "set": "smoke", "category": "happy",
                 "gating": True, "layers": {"http_contract": "infra_error"},
                 "verdict": "infra_error"}

    def test_the_unreduced_record_is_rejected_not_silently_counted(self):
        """Why two files rather than one. Pointing stats.py at verdicts.jsonl
        is a hard error the moment it holds an infra row — exactly the design
        that keeps a crashed case from being scored as a failure."""
        p = self.write_jsonl("verdicts.jsonl", [self.FULL_ROW, self.INFRA_ROW])
        out = self.assert_clean_error("stats.py", p, p)
        self.assertIn("infra_error", out["error"])

    def test_the_reduced_row_shape_is_what_stats_pairs(self):
        rows = [{"case_id": "units-happy", "verdict": "pass"},
                {"case_id": "units-edge", "verdict": "fail"}]
        p = self.write_jsonl("verdicts_for_stats.jsonl", rows)
        rc, out, err = run_script("stats.py", p, p)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["n"], 2)

    def test_reduction_is_a_row_filter_not_a_key_strip(self):
        """The reduced file may carry the full row's extra keys — dropping
        infra/unscored ROWS is the whole of the reduction. Asserted so nobody
        "fixes" the writer to emit only two keys and calls the richer file
        non-conforming."""
        p = self.write_jsonl("reduced.jsonl", [self.FULL_ROW])
        rc, out, err = run_script("stats.py", p, p)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["n"], 1)

    def test_the_run_skill_still_requires_both_artifacts(self):
        spec = RUN_SKILL.read_text(encoding="utf-8")
        for name in ("verdicts.jsonl", "verdicts_for_stats.jsonl"):
            self.assertIn(name, spec,
                          f"run/SKILL.md no longer names {name}; stats.py's "
                          f"paired input would have no documented source")
