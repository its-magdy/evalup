#!/usr/bin/env python3
"""Finding-by-finding tests for scripts/validate_cases.py.

Stdlib only (unittest + subprocess), matching the scripts themselves, and the
script is run as a REAL subprocess because its contract with the calling skill
is argv + stdout JSON + exit code, not a Python API.

Run: python3 -m unittest discover -s tests -v   (from the plugin root)
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = (pathlib.Path(__file__).resolve().parent.parent / "scripts"
          / "validate_cases.py")


def run_validate(*argv, stdin=None):
    proc = subprocess.run([sys.executable, str(SCRIPT), *[str(a) for a in argv]],
                          input=stdin, capture_output=True, text=True)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stderr


ALL_LAYERS_ENABLED = {"capability_matrix": {
    layer: {"enabled": True} for layer in
    ("routing", "tool_selection", "trajectory", "execution", "authz",
     "answer_quality")}}


def good_case(case_id="billing-happy-0000abcd", **overrides):
    """A minimal case with exactly one real graded assertion. Every fixture
    below is this case plus the one defect under test, so a finding can only
    come from that defect.

    `split`, `test_type` and the template pair are here because they are
    REQUIRED, not because the tests below need them: a fixture that omits a
    required field would make every one of those tests assert two findings and
    hide the one it is about."""
    case = {
        "id": case_id,
        "category": "happy",
        "split": ["full"],
        "test_type": "MFT",
        "template_id": "invoice_lookup",
        "instantiation_params": {"customer": "acme"},
        "input": {"messages": [{"role": "user", "content": "hi"}]},
        "expect": {"answer": {"must_contain": ["invoice"]}},
        "no_op_expectation": "fail",
        "gating": True,
        "review": {"status": "accepted", "by": "mohamed"},
    }
    case.update(overrides)
    return case


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write_json(self, name, obj):
        p = self.tmp / name
        p.write_text(json.dumps(obj), encoding="utf-8")
        return p

    def validate(self, cases, *extra):
        """--capabilities is required, so supply an all-enabled matrix unless
        the test is about that flag itself. Defaulting to --no-capabilities
        here instead would put a capabilities_unchecked WARN in every other
        test's findings list."""
        argv = [str(x) for x in extra]
        if not any(a.startswith("--capabilities") or a == "--no-capabilities"
                   for a in argv):
            extra = (*extra, "--capabilities",
                     self.write_json("all-enabled.json", ALL_LAYERS_ENABLED))
        return run_validate("--cases", self.write_json("cases.json", cases),
                            *extra)

    def codes(self, payload):
        return [f["code"] for f in payload["findings"]]

    def assert_finds(self, code, cases, *extra, severity="ERROR"):
        rc, out, err = self.validate(cases, *extra)
        self.assertNotIn("Traceback", err, err)
        self.assertIsNotNone(out, err)
        self.assertIn(code, self.codes(out),
                      f"expected {code}, got {self.codes(out)}")
        finding = next(f for f in out["findings"] if f["code"] == code)
        self.assertEqual(finding["severity"], severity)
        self.assertEqual(rc, 1 if severity == "ERROR" else 0)
        return finding


class TestCleanSuite(ValidateTest):
    def test_clean_suite_exits_0_with_no_findings(self):
        rc, out, err = self.validate([good_case()])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["error_count"], 0)
        self.assertEqual(out["warn_count"], 0)

    def test_summary_shape(self):
        cases = [
            good_case("a-happy-1", test_type="MFT", split=["full", "smoke"]),
            good_case("b-edge-2", category="edge", test_type="INV",
                      split=["full"], metamorphic_parent="a-happy-1",
                      expect={"result": {"scalar": 7}, "http": {"status": 200}}),
            good_case("c-oos-3", category="oos", split=["holdout"],
                      expect={"http": {"status": 400}}),
        ]
        rc, out, _ = self.validate(cases)
        summary = out["summary"]
        self.assertEqual(summary["cases"], 3)
        self.assertEqual(summary["by_category"],
                         {"edge": 1, "happy": 1, "oos": 1})
        self.assertEqual(summary["by_test_type"], {"INV": 1, "MFT": 2})
        # Counted per split, not per membership list: "how many are sealed"
        # has to be readable without re-adding the lists by hand.
        self.assertEqual(summary["by_split"],
                         {"full": 2, "holdout": 1, "smoke": 1})
        self.assertEqual(summary["by_graded_layer"]["answer_quality"], 1)
        self.assertEqual(summary["by_graded_layer"]["execution"], 1)
        self.assertEqual(summary["http_only"], 1)
        # The http-only case is the ERROR; the other two are clean.
        self.assertEqual(self.codes(out), ["no_graded_layer"])

    def test_accepts_jsonl_and_wrapper_and_stdin(self):
        case = good_case()
        caps = self.write_json("all-enabled.json", ALL_LAYERS_ENABLED)
        p = self.tmp / "cases.jsonl"
        p.write_text(json.dumps(case) + "\n", encoding="utf-8")
        self.assertEqual(run_validate("--cases", p, "--capabilities", caps)[0],
                         0)
        rc, _, _ = self.validate({"cases": [case]})
        self.assertEqual(rc, 0)
        rc, out, _ = run_validate("--cases", "-", "--capabilities", caps,
                                  stdin=json.dumps([case]))
        self.assertEqual(rc, 0)
        self.assertEqual(out["summary"]["cases"], 1)

    def test_version_flag(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--version"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("agent-eval harness", proc.stdout)


class TestErrorFindings(ValidateTest):
    def test_duplicate_id(self):
        f = self.assert_finds("duplicate_id",
                              [good_case("dup-happy-1"),
                               good_case("dup-happy-1")])
        self.assertEqual(f["case_id"], "dup-happy-1")

    def test_missing_id(self):
        case = good_case()
        del case["id"]
        self.assert_finds("missing_id", [case])

    def test_bad_category(self):
        self.assert_finds("bad_category", [good_case(category="hapy")])

    def test_missing_split(self):
        """Absent `split` used to pass silently — the case is then selected by
        no run mode, and a case meant to be sealed is sealed by nothing."""
        case = good_case()
        del case["split"]
        f = self.assert_finds("missing_split", [case])
        self.assertIn("FIELD", f["message"])
        # An empty list is the same statement, spelled differently.
        self.assert_finds("missing_split", [good_case(split=[])])

    def test_missing_test_type(self):
        case = good_case()
        del case["test_type"]
        f = self.assert_finds("missing_test_type", [case])
        self.assertIn("ORACLE", f["message"])

    def test_missing_template_id_key(self):
        """PRESENCE, not truthiness: absent means nobody decided, explicit
        null means the author declared a one-off (case-format.md)."""
        case = good_case()
        del case["template_id"]
        del case["instantiation_params"]
        self.assert_finds("missing_template_id", [case])
        # The declared one-off is clean.
        rc, out, err = self.validate([good_case(template_id=None,
                                                instantiation_params=None)])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["findings"], [])
        self.assertEqual(out["summary"]["one_off"], 1)

    def test_missing_metamorphic_parent(self):
        """Without this, `test_type: INV` on an ordinary case would buy the
        suite's metamorphic coverage credit for free."""
        for test_type in ("INV", "DIR"):
            f = self.assert_finds("missing_metamorphic_parent",
                                  [good_case(test_type=test_type)])
            self.assertIn(test_type, f["message"])
        # With a parent present it is clean.
        self.assertEqual(self.validate(
            [good_case("p-happy-1"),
             good_case("c-happy-2", test_type="INV",
                       metamorphic_parent="p-happy-1")])[0], 0)

    def test_bad_test_type(self):
        self.assert_finds("bad_test_type", [good_case(test_type="mft")])

    def test_no_graded_layer_http_only(self):
        f = self.assert_finds("no_graded_layer",
                              [good_case(expect={"http": {"status": 200}})])
        self.assertIn("liveness", f["message"])

    def test_no_graded_layer_when_layer_disabled(self):
        caps = self.write_json("caps.json", {"routing": {"enabled": False},
                                             "execution": {"enabled": True}})
        cases = [good_case(expect={"route": "billing"})]
        f = self.assert_finds("no_graded_layer", cases, "--capabilities", caps)
        self.assertIn("routing", f["message"])
        # The same case is fine while routing is enabled.
        self.assertEqual(self.validate(cases)[0], 0)

    def test_enabled_layer_with_http_still_passes(self):
        caps = self.write_json("caps.json", {"routing": {"enabled": True}})
        rc, out, _ = self.validate(
            [good_case(expect={"route": "billing", "http": {"status": 200}})],
            "--capabilities", caps)
        self.assertEqual(rc, 0)
        self.assertEqual(out["summary"]["http_only"], 0)

    def test_dangling_metamorphic_parent(self):
        self.assert_finds("dangling_metamorphic_parent",
                          [good_case(metamorphic_parent="nope-happy-9")])
        # Present parent -> clean.
        self.assertEqual(self.validate(
            [good_case("p-happy-1"),
             good_case("c-happy-2", metamorphic_parent="p-happy-1")])[0], 0)

    def test_template_without_params(self):
        self.assert_finds("template_without_params",
                          [good_case(template_id="shift_count",
                                     instantiation_params={})])

    def test_params_without_template(self):
        self.assert_finds("params_without_template",
                          [good_case(template_id=None,
                                     instantiation_params={"role": "nurse"})])

    def test_bad_order_mode(self):
        self.assert_finds("bad_order_mode", [good_case(expect={
            "tools": {"order": ["a", "b"], "order_mode": "ordered"}})])

    def test_bad_call_scope(self):
        self.assert_finds("bad_call_scope", [good_case(expect={
            "args": {"search": {"calls": "every", "q": "invoices"}}})])

    def test_empty_columns(self):
        self.assert_finds("empty_columns", [good_case(expect={
            "result": {"rows": [{"n": 1}], "columns": []}})])

    def test_vacuous_args_entry(self):
        self.assert_finds("vacuous_args_entry",
                          [good_case(expect={"args": {"search": {"calls": "all"}}})])

    def test_both_scalar_and_rows(self):
        self.assert_finds("both_scalar_and_rows", [good_case(expect={
            "result": {"scalar": 7, "rows": [{"n": 7}]}})])

    def test_neither_scalar_nor_rows(self):
        self.assert_finds("neither_scalar_nor_rows", [good_case(expect={
            "result": {"reference_query": "SELECT 1"}})])

    def test_unquoted_bool_or_number(self):
        f = self.assert_finds("unquoted_bool_or_number", [good_case(expect={
            "answer": {"must_contain": ["ok", True],
                       "must_not_contain": [7]}})])
        self.assertEqual(f["case_id"], "billing-happy-0000abcd")
        rc, out, _ = self.validate([good_case(expect={
            "answer": {"must_contain": [True], "must_not_contain": [7]}})])
        self.assertEqual(
            sum(1 for c in self.codes(out) if c == "unquoted_bool_or_number"), 2)

    def test_state_only_case_is_an_error_not_a_free_pass(self):
        """Step 10. `expect.state` used to map onto the trajectory layer, so a
        case whose only expectation was an end-state assertion earned the
        graded stamp -- and run_cases.py then scored `state` unscored (no
        state-diff scorer exists) and rolled the case up to `pass` off its
        `http` row alone. A case that cannot fail is precisely what
        no_graded_layer exists to stop, so the mapping is gone and the ERROR
        names the reserved key rather than claiming the case asserts nothing.
        """
        case = good_case("c-state", expect={"state": {"unchanged": True}})
        f = self.assert_finds("no_graded_layer", [case])
        self.assertIn("expect.state", f["message"])
        self.assertIn("RESERVED", f["message"])
        # ...and it is NOT reported as http-only: it did assert something.
        self.assertNotIn("expect.http", f["message"])


class TestExpectMapsToTheLayerTheRunnerScores(ValidateTest):
    """`expect.tools` -> trajectory, `expect.args` -> tool_selection.

    The pairing reads backwards and it is the contract's: runner-contract.md
    SS5's trigger column scores `expect.tools` with trajectory_match.py on the
    `trajectory` layer, and `expect.args` with score_args.py on
    `tool_selection`. LAYER_OF_EXPECT had the two SWAPPED, so no_graded_layer
    blamed a layer the runner never consults for that expectation -- the
    linter called a case ungraded that the runner grades, and vice versa.
    Nothing pinned the mapping, which is why the swap survived; these tests
    are that pin, in both directions and against the runner itself.
    """

    def caps(self, **enabled):
        matrix = {layer: {"enabled": False, "blocked_by": "off"} for layer in
                  ("routing", "tool_selection", "trajectory", "execution",
                   "authz", "answer_quality")}
        for layer, on in enabled.items():
            matrix[layer] = {"enabled": True} if on else matrix[layer]
        return self.write_json(f"caps-{sorted(enabled)}.json", matrix)

    def test_tools_is_graded_by_trajectory(self):
        case = good_case(expect={"tools": {"subset": ["list_invoices"]}})
        rc, out, _ = self.validate([case], "--capabilities",
                                   self.caps(trajectory=True))
        self.assertEqual(rc, 0, self.codes(out))
        self.assertEqual(out["summary"]["by_graded_layer"]["trajectory"], 1)
        self.assertEqual(out["summary"]["by_graded_layer"]["tool_selection"], 0)
        # Mirror: with trajectory off it IS ungraded, and the ERROR says so.
        f = self.assert_finds("no_graded_layer", [case], "--capabilities",
                              self.caps(tool_selection=True))
        self.assertIn("trajectory", f["message"])
        self.assertNotIn("tool_selection", f["message"])

    def test_args_is_graded_by_tool_selection(self):
        case = good_case(expect={"args": {"list_invoices": {"limit": 10}}})
        rc, out, _ = self.validate([case], "--capabilities",
                                   self.caps(tool_selection=True))
        self.assertEqual(rc, 0, self.codes(out))
        self.assertEqual(out["summary"]["by_graded_layer"]["tool_selection"], 1)
        self.assertEqual(out["summary"]["by_graded_layer"]["trajectory"], 0)
        f = self.assert_finds("no_graded_layer", [case], "--capabilities",
                              self.caps(trajectory=True))
        self.assertIn("tool_selection", f["message"])

    def test_the_map_agrees_with_run_cases_applicable_layers(self):
        """Structural, so the two cannot drift again: every expect key the
        linter maps must land on the layer the RUNNER makes applicable for a
        case carrying only that key. Imported for its declared behaviour, the
        way test_run_cases.py imports it for its constants."""
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            import run_cases
            import validate_cases
        finally:
            sys.path.pop(0)
        for key, layer in validate_cases.LAYER_OF_EXPECT.items():
            with self.subTest(expect_key=key):
                case = {"expect": {key: {}}}
                applicable = run_cases.applicable_layers(
                    case, trace_collected=True)
                # `answer_quality` is the linter's name for the runner's
                # `answer` row; every other layer name is shared verbatim.
                expected = "answer" if layer == "answer_quality" else layer
                self.assertIn(expected, applicable)


class TestWarnFindings(ValidateTest):
    def test_no_op_pass(self):
        f = self.assert_finds("no_op_pass", [good_case(no_op_expectation="pass")],
                              severity="WARN")
        self.assertIn("vacuous", f["message"])
        # A written justification silences it.
        self.assertEqual(self.validate([good_case(
            no_op_expectation="pass",
            no_op_justification="the correct behavior is to do nothing")])[0], 0)

    def test_machine_accepted(self):
        for by in (None, "", "generate-review", "Auto", "test-generator-2"):
            review = {"status": "accepted"}
            if by is not None:
                review["by"] = by
            f = self.assert_finds("machine_accepted",
                                  [good_case(review=review)], severity="WARN")
            self.assertIn("human", f["message"])
        # A human whose name merely starts with similar letters is fine.
        self.assertEqual(self.validate(
            [good_case(review={"status": "accepted", "by": "Generosa"})])[0], 0)

    def test_gating_unreviewed(self):
        for status in ("pending", "quarantined"):
            self.assert_finds("gating_unreviewed",
                              [good_case(gating=True,
                                         review={"status": status,
                                                 "by": "mohamed"})],
                              severity="WARN")
        # Not gating -> no warning.
        self.assertEqual(self.validate([good_case(
            gating=False, review={"status": "pending", "by": "m"})])[0], 0)

    def test_source_derived_expectation(self):
        for notes in ("verified against the seeded fixture",
                      "read ShiftGuard.cs to get the boundary",
                      "per the guard in the service layer"):
            self.assert_finds("source_derived_expectation",
                              [good_case(notes=notes)], severity="WARN")

    def test_multi_turn_case_reserved(self):
        """The inverse of the retired `single_turn_suite` warning.

        That one fired when a suite had NO multi-turn coverage — recommending
        the one thing run_cases.py refuses to run. Multi-turn is reserved, so
        the finding is now per-case and fires ON the multi-turn case.
        """
        cases = [good_case(f"c-happy-{i}") for i in range(11)]
        # A single-turn suite is exactly what the harness wants: silent.
        self.assertNotIn("multi_turn_case_reserved",
                         self.codes(self.validate(cases)[1]))
        self.assertNotIn("single_turn_suite", self.codes(self.validate(cases)[1]))
        cases[0]["input"]["messages"] = [{"role": "user", "content": "a"},
                                         {"role": "assistant", "content": "b"},
                                         {"role": "user", "content": "c"}]
        f = self.assert_finds("multi_turn_case_reserved", cases, severity="WARN")
        # Per-case: it names the case the author has to fix.
        self.assertEqual(f["case_id"], "c-happy-0")
        self.assertIn("2 user turns", f["message"])
        # Prior assistant/system turns as fixed context do NOT trip it: one
        # user message is one user turn regardless of what precedes it.
        cases[0]["input"]["messages"] = [{"role": "system", "content": "s"},
                                         {"role": "assistant", "content": "b"},
                                         {"role": "user", "content": "c"}]
        self.assertNotIn("multi_turn_case_reserved",
                         self.codes(self.validate(cases)[1]))

    def test_reserved_expectation_warns_beside_a_real_layer(self):
        """With a graded layer present the case is fine; the state key is dead
        weight, which is a WARN on Step 4's line (wasted authoring, no false
        number) exactly as multi_turn_case_reserved is."""
        case = good_case("c-mixed", expect={"answer": {"must_contain": ["x"]},
                                            "state": {"unchanged": True}})
        f = self.assert_finds("reserved_expectation", [case], severity="WARN")
        self.assertEqual(f["case_id"], "c-mixed")
        self.assertIn("expect.state", f["message"])
        # An empty/absent state key is not an assertion and stays silent.
        self.assertNotIn("reserved_expectation", self.codes(self.validate(
            [good_case("c-quiet", expect={"answer": {"must_contain": ["x"]},
                                          "state": None})])[1]))

    def test_reserved_case_fields_warn(self):
        """seed_state, excluded_tools and available_tools are declared by
        case-format.md and driven by nothing: run_cases.py never invokes
        environment.seed/.reset/.snapshot_state, no layer scores a tool the
        agent was told to avoid, and the tool catalog cannot be narrowed per
        case. Each one silently changes what the author thinks the case tests,
        so each says so once."""
        for field, value, code in (
                ("seed_state", "fixtures/base.sql", "seed_state_not_loaded"),
                ("excluded_tools", ["delete_user"],
                 "excluded_tools_not_scored"),
                ("available_tools", ["get_invoice"],
                 "available_tools_not_scoped")):
            with self.subTest(field=field):
                f = self.assert_finds(code, [good_case(**{field: value})],
                                      severity="WARN")
                self.assertIn(field, f["message"])
                # Empty is the documented default and must stay silent.
                self.assertNotIn(code, self.codes(self.validate(
                    [good_case(**{field: [] if isinstance(value, list)
                                  else None})])[1]))

    def test_no_metamorphic_coverage(self):
        cases = [good_case(f"c-happy-{i}") for i in range(11)]
        f = self.assert_finds("no_metamorphic_coverage", cases, severity="WARN")
        self.assertIsNone(f["case_id"])
        # One INV case is the whole floor.
        cases[0] = good_case("c-happy-0", test_type="INV",
                             metamorphic_parent="c-happy-1")
        self.assertNotIn("no_metamorphic_coverage",
                         self.codes(self.validate(cases)[1]))
        # At the threshold (10) it does not fire — a suite that small is a
        # fragment, and a coverage share over it is arithmetic, not signal.
        self.assertNotIn("no_metamorphic_coverage",
                         self.codes(self.validate(
                             [good_case(f"c-happy-{i}") for i in range(10)])[1]))

    def test_all_one_off(self):
        cases = [good_case(f"c-happy-{i}", template_id=None,
                           instantiation_params=None) for i in range(5)]
        f = self.assert_finds("all_one_off", cases, severity="WARN")
        self.assertIsNone(f["case_id"])
        # One templated case means the tuple phase happened at all.
        cases[0] = good_case("c-happy-0")
        self.assertNotIn("all_one_off", self.codes(self.validate(cases)[1]))
        # Four one-offs is below the floor.
        self.assertNotIn("all_one_off", self.codes(self.validate(cases[1:])[1]))

    def test_category_skew(self):
        # 3 happy / 1 edge / 1 oos -> happy is 60%.
        cases = [good_case("a-happy-1"), good_case("b-happy-2"),
                 good_case("c-happy-3"),
                 good_case("d-edge-4", category="edge"),
                 good_case("e-oos-5", category="oos")]
        f = self.assert_finds("category_skew", cases, severity="WARN")
        self.assertIn("happy", f["message"])
        # An even 2/2/1 mix (40% max) does not fire.
        even = [good_case("a-happy-1"), good_case("b-happy-2"),
                good_case("c-edge-3", category="edge"),
                good_case("d-edge-4", category="edge"),
                good_case("e-oos-5", category="oos")]
        self.assertEqual(self.validate(even)[0], 0)


class TestExitContract(ValidateTest):
    def test_warnings_alone_exit_0(self):
        rc, out, _ = self.validate([good_case(no_op_expectation="pass")])
        self.assertEqual(rc, 0)
        self.assertEqual(out["error_count"], 0)
        self.assertEqual(out["warn_count"], 1)

    def test_strict_promotes_warnings_to_errors(self):
        rc, out, _ = self.validate([good_case(no_op_expectation="pass")],
                                   "--strict")
        self.assertEqual(rc, 1)
        self.assertTrue(out["strict"])
        self.assertEqual(out["error_count"], 1)
        self.assertEqual(out["warn_count"], 0)
        self.assertEqual(out["findings"][0]["severity"], "ERROR")

    def test_strict_on_a_clean_suite_still_exits_0(self):
        self.assertEqual(self.validate([good_case()], "--strict")[0], 0)

    def test_bad_input_exits_2_with_error_json(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        for argv in (("--cases", bad),
                     ("--cases", self.tmp / "missing.json"),
                     ("--cases", self.write_json("empty.json", [])),
                     ("--cases", self.write_json("scalar.json", ["nope"])),
                     ("--cases", self.write_json("num.json", 7))):
            rc, out, err = run_validate(*argv)
            self.assertEqual(rc, 2, f"{argv}: {err}")
            self.assertIsNotNone(out, err)
            self.assertIn("error", out)
            self.assertNotIn("Traceback", err)

    def test_bad_capabilities_file_exits_2(self):
        caps = self.write_json("caps.json", ["routing"])
        rc, out, err = self.validate([good_case()], "--capabilities", caps)
        self.assertEqual(rc, 2, err)
        self.assertIn("error", out)


class TestCapabilitiesRequired(ValidateTest):
    """--capabilities is required because without it every layer counts as
    enabled, which turns OFF the no_graded_layer check for disabled layers —
    the most expensive failure mode this linter covers. The opt-out exists,
    but it has to be said out loud and it is visible in the report."""

    def test_missing_both_flags_exits_2(self):
        cases = self.write_json("cases.json", [good_case()])
        rc, out, err = run_validate("--cases", cases)
        self.assertEqual(rc, 2, err)
        self.assertIn("--capabilities", out["error"])
        self.assertIn("--no-capabilities", out["error"])

    def test_both_flags_exit_2(self):
        caps = self.write_json("caps.json", ALL_LAYERS_ENABLED)
        rc, out, err = self.validate([good_case()], "--capabilities", caps,
                                     "--no-capabilities")
        self.assertEqual(rc, 2, err)
        self.assertIn("mutually exclusive", out["error"])

    def test_opt_out_warns_and_is_recorded(self):
        rc, out, err = self.validate([good_case()], "--no-capabilities")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.codes(out), ["capabilities_unchecked"])
        self.assertEqual(out["findings"][0]["severity"], "WARN")
        self.assertFalse(out["capabilities_checked"])

    def test_opt_out_is_rejected_under_strict(self):
        rc, out, err = self.validate([good_case()], "--no-capabilities",
                                     "--strict")
        self.assertEqual(rc, 1, err)
        self.assertEqual(out["findings"][0]["severity"], "ERROR")

    def test_real_matrix_records_capabilities_checked(self):
        caps = self.write_json("caps.json", ALL_LAYERS_ENABLED)
        rc, out, err = self.validate([good_case()], "--capabilities", caps)
        self.assertEqual(rc, 0, err)
        self.assertTrue(out["capabilities_checked"])


class TestManifestCheck(ValidateTest):
    """dataset.yaml is hand-written prose, not regenerated from the case
    files, so nothing else catches it going stale after a direct edit to a
    cases/*.yaml file. --manifest cross-checks it."""

    def test_matching_manifest_is_silent(self):
        cases = [good_case("a-happy-1", split=["full", "smoke"]),
                 good_case("b-happy-2", split=["full"])]
        manifest = self.write_json("manifest.json",
                                   {"cases": 2, "splits": {"full": 2,
                                                            "smoke": 1}})
        rc, out, err = self.validate(cases, "--manifest", manifest)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("manifest_mismatch", self.codes(out))

    def test_stale_case_count(self):
        manifest = self.write_json("manifest.json", {"cases": 99})
        f = self.assert_finds("manifest_mismatch", [good_case()],
                              "--manifest", manifest)
        self.assertIn("99", f["message"])
        self.assertIn("1 case objects", f["message"])

    def test_stale_split_count(self):
        cases = [good_case("a-happy-1", split=["full"])]
        manifest = self.write_json("manifest.json",
                                   {"splits": {"full": 5}})
        f = self.assert_finds("manifest_mismatch", cases,
                              "--manifest", manifest)
        self.assertIn("splits.full", f["message"])

    def test_stale_coverage_grid(self):
        cases = [good_case("a-happy-1", unit="Billing", test_type="MFT")]
        manifest = self.write_json(
            "manifest.json",
            {"coverage_grid": {"by_unit": {"Billing": {"total": 3,
                                                        "MFT": 3}}}})
        f = self.assert_finds("manifest_mismatch", cases,
                              "--manifest", manifest)
        self.assertIn("coverage_grid.by_unit.Billing", f["message"])

    def test_no_manifest_flag_skips_the_check_entirely(self):
        rc, out, err = self.validate([good_case()])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("manifest_mismatch", self.codes(out))

    def test_bad_manifest_file_exits_2(self):
        manifest = self.write_json("manifest.json", ["not", "an", "object"])
        rc, out, err = self.validate([good_case()], "--manifest", manifest)
        self.assertEqual(rc, 2, err)
        self.assertIn("error", out)


class TestAuthzRecordIds(ValidateTest):
    """The allowed_record_ids check recognizes ids BY SHAPE, so an allowlist
    the default recognizer cannot see scores `unscorable` at run time — a case
    that reads like it passed. These pin the shape rule to score_authz.py's."""

    def authz_case(self, **authz):
        return good_case(expect={"authz": authz})

    def test_uuid_allowlist_warns(self):
        f = self.assert_finds(
            "unrecognizable_record_ids",
            [self.authz_case(
                allowed_record_ids=["550e8400-e29b-41d4-a716-446655440000"])],
            severity="WARN")
        self.assertIn("--id-pattern", f["message"])

    def test_integer_primary_keys_warn(self):
        self.assert_finds("unrecognizable_record_ids",
                          [self.authz_case(allowed_record_ids=[881, 412])],
                          severity="WARN")

    def test_id_shaped_allowlist_is_clean(self):
        rc, out, err = self.validate([self.authz_case(
            allowed_record_ids=["INV-1042", "e_881"])])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("unrecognizable_record_ids", self.codes(out))

    def test_one_id_shaped_entry_is_enough(self):
        # The default recognizer scopes by the prefixes it CAN see, so a mixed
        # allowlist is still partly scorable; warning there would cry wolf.
        rc, out, err = self.validate([self.authz_case(
            allowed_record_ids=["INV-1042", 881])])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("unrecognizable_record_ids", self.codes(out))

    def test_empty_allowlist_is_clean(self):
        # "No record is in scope" is the strictest allowlist and is scored
        # without prefix scoping at all — it needs no pattern.
        rc, out, err = self.validate([self.authz_case(
            allowed_record_ids=[], forbidden_record_ids=["99"])])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("unrecognizable_record_ids", self.codes(out))

    def test_forbidden_ids_alone_are_clean(self):
        # forbidden_record_ids is a literal search, not shape-based.
        rc, out, err = self.validate([self.authz_case(
            forbidden_record_ids=["550e8400-e29b-41d4-a716-446655440000"])])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("unrecognizable_record_ids", self.codes(out))


if __name__ == "__main__":
    unittest.main()
