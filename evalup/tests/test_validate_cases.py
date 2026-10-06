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
        self.assertIn("evalup harness", proc.stdout)


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

    def test_unsafe_id(self):
        # Same rule the runner enforces at exit 2 (_common.unsafe_case_id).
        for bad in ("../../etc/x", "/abs/path", "a/b", ".hidden", "has space"):
            with self.subTest(case_id=bad):
                self.assert_finds("unsafe_id", [good_case(bad)])
        for fine in ("c-9e05b3f4", "billing_refund.v2", "dup-happy-1"):
            with self.subTest(case_id=fine):
                _, out, err = self.validate([good_case(fine)])
                self.assertNotIn("unsafe_id", self.codes(out), err)

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
            import _common
            import run_cases
            import validate_cases
        finally:
            sys.path.pop(0)
        for key, layer in validate_cases.LAYER_OF_EXPECT.items():
            with self.subTest(expect_key=key):
                case = {"expect": {key: {}}}
                applicable = run_cases.applicable_layers(
                    case, trace_collected=True)
                # The linter speaks the capability matrix's names and the
                # runner its own rows'; MATRIX_KEY_OF_LAYER is the one
                # translation, and the runner's own lookup goes through it.
                self.assertIn(layer, {_common.MATRIX_KEY_OF_LAYER[name]
                                      for name in applicable})
        # Every layer the linter counts as graded is a key the runner honours:
        # one it did not would be disabled in the lint and scored in the run.
        self.assertLessEqual(set(validate_cases.LAYERS),
                             set(_common.MATRIX_KEY_OF_LAYER.values()))


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

    def test_multi_turn_in_messages(self):
        """The inverse of the retired `single_turn_suite` warning.

        That one fired when a suite had NO multi-turn coverage. The finding
        is per-case and fires ON a case that packs two user turns into
        input.messages: the runner sends one user message and skips it. A
        conversation is written as input.turns (TestConversationCases).
        """
        cases = [good_case(f"c-happy-{i}") for i in range(11)]
        # A single-turn suite is exactly what the harness wants: silent.
        self.assertNotIn("multi_turn_in_messages",
                         self.codes(self.validate(cases)[1]))
        self.assertNotIn("single_turn_suite", self.codes(self.validate(cases)[1]))
        cases[0]["input"]["messages"] = [{"role": "user", "content": "a"},
                                         {"role": "assistant", "content": "b"},
                                         {"role": "user", "content": "c"}]
        f = self.assert_finds("multi_turn_in_messages", cases, severity="WARN")
        # Per-case: it names the case the author has to fix.
        self.assertEqual(f["case_id"], "c-happy-0")
        self.assertIn("2 user turns", f["message"])
        # Prior assistant/system messages do NOT trip it: one user message
        # is one user turn. They trip not_one_user_message instead (SS0).
        cases[0]["input"]["messages"] = [{"role": "system", "content": "s"},
                                         {"role": "assistant", "content": "b"},
                                         {"role": "user", "content": "c"}]
        self.assertNotIn("multi_turn_in_messages",
                         self.codes(self.validate(cases)[1]))

    def test_context_messages_are_reported(self):
        """docs/multi-turn.md SS0: run_cases.py sends exactly one user
        message and SKIPS a case holding anything else. This used to say
        prior system/assistant messages were "fine as fixed context" -- they
        were never sent, and the case scored as if they had been."""
        self.assertNotIn("not_one_user_message",
                         self.codes(self.validate([good_case()])[1]))
        for messages, fragment in (
                ([{"role": "system", "content": "s"},
                  {"role": "user", "content": "c"}], "system"),
                ([{"role": "assistant", "content": "b"},
                  {"role": "user", "content": "c"}], "assistant"),
                ([], "no user message")):
            with self.subTest(messages=messages):
                case = good_case(input={"messages": messages})
                f = self.assert_finds("not_one_user_message", [case],
                                      severity="WARN")
                self.assertEqual(f["case_id"], case["id"])
                self.assertIn(fragment, f["message"])
                self.assertIn("SKIP", f["message"])
        # Two user turns keep their own finding, not this one as well.
        case = good_case(input={"messages": [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"}]})
        codes = self.codes(self.validate([case])[1])
        self.assertIn("multi_turn_in_messages", codes)
        self.assertNotIn("not_one_user_message", codes)

    def test_reserved_expectation_warns_beside_a_real_layer(self):
        """With a graded layer present the case is fine; the state key is dead
        weight, which is a WARN on Step 4's line (wasted authoring, no false
        number) exactly as multi_turn_in_messages is."""
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

    def test_an_absent_gating_key_gates_as_the_runner_reads_it(self):
        """Review of the F-119 fix: case-format.md and the runner default an
        absent `gating` to true, but the linter read it as false -- so a
        pending case with no key raised no `gating_unreviewed`, and
        `nothing_gates` said nothing could fail while every case could."""
        pending = {"status": "pending", "by": None}
        cases = [good_case(f"c-happy-{i}", review=pending) for i in range(2)]
        for case in cases:
            del case["gating"]
        rc, out, _ = self.validate(cases)
        codes = self.codes(out)
        self.assertEqual(codes.count("gating_unreviewed"), 2)
        self.assertNotIn("nothing_gates", codes)

    def test_a_canary_never_gates_so_never_warns_unreviewed(self):
        """The runner forces a canary's gating false (build_verdict), so a
        pending canary with no key is not an unreviewed gate."""
        canary = good_case("c-canary-0", split=["full", "canary"],
                           review={"status": "pending", "by": None})
        del canary["gating"]
        self.assertNotIn("gating_unreviewed",
                         self.codes(self.validate([canary])[1]))

    def test_nothing_gates(self):
        # The field test's headless generate flipped every case to gating
        # false (correct: nobody had reviewed them) and the run then closed
        # its gate on infra rate alone, with six real failures that could
        # not count. Suite-level, so the fact is stated once.
        pending = {"status": "pending", "by": None}
        cases = [good_case(f"c-happy-{i}", gating=False, review=pending)
                 for i in range(3)]
        f = self.assert_finds("nothing_gates", cases, severity="WARN")
        self.assertIsNone(f["case_id"])
        self.assertIn("cannot close a gate", f["message"])
        # One accepted, gating, non-canary case is enough.
        cases[0] = good_case("c-happy-0")
        self.assertNotIn("nothing_gates", self.codes(self.validate(cases)[1]))
        # A gating canary alone does not count: it watches the harness.
        canary = good_case("c-canary-9", split=["full", "canary"])
        self.assertIn("nothing_gates", self.codes(self.validate(
            [canary, *cases[1:]])[1]))
        # A suite of canaries only has nothing to say here.
        self.assertNotIn("nothing_gates", self.codes(self.validate(
            [canary])[1]))


def conversation(case_id="c-convo-0001", turns=None, **overrides):
    """good_case as a two-turn conversation: input.turns, final checks on
    the top-level expect."""
    case = good_case(case_id, **overrides)
    case["input"] = {"turns": turns if turns is not None else [
        {"user": "show me the licences for the Cairo team",
         "expect": {"route": "licences"}},
        {"user": "only the expired ones"}]}
    return case


class TestConversationCases(ValidateTest):
    """docs/multi-turn.md SS1 / SS8's validator row."""

    def test_a_well_formed_conversation_is_clean(self):
        rc, out, err = self.validate(
            [conversation(every_turn={"tools": {"forbidden": ["delete"]}})])
        self.assertEqual((rc, out["findings"]), (0, []), err)

    def test_turns_and_messages_are_exclusive(self):
        case = conversation()
        case["input"]["messages"] = [{"role": "user", "content": "x"}]
        self.assert_finds("turns_and_messages", [case])

    def test_one_turn_is_not_a_conversation(self):
        for turns in ([{"user": "only one"}], []):
            with self.subTest(turns=turns):
                f = self.assert_finds("single_turn_conversation",
                                      [conversation(turns=turns)])
                self.assertIn("single-turn case", f["message"])

    def test_a_turn_is_an_object_never_a_bare_string(self):
        f = self.assert_finds("bad_turn", [conversation(
            turns=["show me the licences", {"user": "only expired"}])])
        self.assertIn("never a bare string", f["message"])
        self.assert_finds("bad_turn", [conversation(
            turns=[{"user": ""}, {"user": "only expired"}])])

    def test_assistant_turns_are_reserved(self):
        f = self.assert_finds("turn_key_reserved", [conversation(turns=[
            {"user": "a", "assistant": "seeded reply"}, {"user": "b"}])])
        self.assertIn("assistant", f["message"])
        self.assert_finds("unknown_turn_key", [conversation(turns=[
            {"user": "a", "expcet": {"route": "x"}}, {"user": "b"}])])

    def test_the_final_turn_is_the_top_level_expect(self):
        self.assert_finds("final_turn_expect", [conversation(turns=[
            {"user": "a"}, {"user": "b", "expect": {"route": "x"}}])])

    def test_long_conversation_warns(self):
        turns = [{"user": f"turn {n}"} for n in range(9)]
        f = self.assert_finds("long_conversation",
                              [conversation(turns=turns)], severity="WARN")
        self.assertIn("9 turns", f["message"])
        self.assertNotIn("long_conversation", self.codes(self.validate(
            [conversation(turns=turns[:8])])[1]))

    def test_a_conversation_canary_is_an_error(self):
        self.assert_finds("multi_turn_canary", [conversation(
            split=["smoke", "full", "canary"], gating=False)])

    def test_only_a_checkpoint_graded_says_so(self):
        """The top-level expect is the final turn's claim: a checkpoint does
        not make the case graded, and the message says where to put one."""
        f = self.assert_finds("no_graded_layer",
                              [conversation(expect={"http": {"status": 200}})])
        self.assertIn("put a graded check on the final turn", f["message"])

    def test_every_turn_counts_toward_the_final_turn(self):
        """every_turn is copied into the final turn too, so its graded check
        grades the case."""
        case = conversation(expect={},
                            every_turn={"answer": {"must_not_contain": ["x"]}})
        self.assertNotIn("no_graded_layer",
                         self.codes(self.validate([case])[1]))

    def test_every_turn_collision(self):
        case = conversation(
            every_turn={"tools": {"forbidden": ["delete"]}},
            expect={"answer": {"must_contain": ["invoice"]},
                    "tools": {"forbidden": ["update"]}})
        f = self.assert_finds("every_turn_collision", [case])
        self.assertIn("every_turn.tools.forbidden", f["message"])
        self.assertIn("final turn", f["message"])
        # Different keys under one object merge, and that is not a collision.
        case["expect"]["tools"] = {"subset": ["lookup"]}
        self.assertNotIn("every_turn_collision",
                         self.codes(self.validate([case])[1]))

    def test_every_turn_needs_turns(self):
        self.assert_finds("every_turn_without_turns", [good_case(
            every_turn={"tools": {"forbidden": ["delete"]}})])

    def test_checkpoints_get_the_per_expect_checks(self):
        """A malformed checkpoint is as inert as a malformed expect, and the
        finding names the turn rather than `expect`."""
        f = self.assert_finds("string_not_list", [conversation(turns=[
            {"user": "a", "expect": {"answer": {"must_contain": "x"}}},
            {"user": "b"}])])
        self.assertTrue(f["message"].startswith("input.turns[0].expect"),
                        f["message"])
        f = self.assert_finds("bad_order_mode", [conversation(
            every_turn={"tools": {"order_mode": "loose"}})])
        self.assertTrue(f["message"].startswith("every_turn"), f["message"])

    def test_undeclared_conversation_warns_with_an_adapter(self):
        adapter = self.write_json("adapter.json", {"invocation": {}})
        f = self.assert_finds("conversation_undeclared", [conversation()],
                              "--adapter", adapter, severity="WARN")
        self.assertIn("invocation.conversation", f["message"])
        adapter = self.write_json("adapter2.json", {"invocation": {
            "conversation": {"style": "client-id"}}})
        self.assertNotIn("conversation_undeclared", self.codes(self.validate(
            [conversation()], "--adapter", adapter)[1]))

    def test_echo_counts_every_user_turn(self):
        case = conversation(
            expect={"answer": {"must_contain": ["Cairo"]}})
        self.assert_finds("echo_assertion", [case], severity="WARN")


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


class TestEchoAssertion(ValidateTest):
    """F-028 (field test 2026-09-25): a must_contain entry the case's own
    input already satisfies passed on an echo ("I couldn't find any
    employees named Mohammed")."""

    def test_a_substring_of_the_input_warns(self):
        case = good_case(
            input={"messages": [{"role": "user",
                                 "content": "show me the invoice for acme"}]},
            expect={"answer": {"must_contain": ["Invoice", "total"]}})
        finding = self.assert_finds("echo_assertion", [case], severity="WARN")
        self.assertIn("must_contain[0]", finding["message"])
        self.assertEqual(sum(1 for f in self.validate([case])[1]["findings"]
                             if f["code"] == "echo_assertion"), 1)

    def test_a_regex_that_matches_the_input_warns(self):
        case = good_case(
            input={"messages": [{"role": "user",
                                 "content": "is Mohammed on leave today?"}]},
            expect={"answer": {"must_contain": ["/(?i:mohammed)/"]}})
        self.assert_finds("echo_assertion", [case], severity="WARN")

    def test_an_entry_only_a_real_answer_contains_is_clean(self):
        case = good_case(
            input={"messages": [{"role": "user",
                                 "content": "is Mohammed on leave today?"}]},
            expect={"answer": {"must_contain": ["annual leave", "/until/"],
                               "must_not_contain": ["Mohammed"]}})
        rc, out, _ = self.validate([case])
        self.assertNotIn("echo_assertion", self.codes(out))

    def test_strict_promotes_it(self):
        case = good_case(
            input={"messages": [{"role": "user", "content": "acme invoice"}]},
            expect={"answer": {"must_contain": ["invoice"]}})
        self.assert_finds("echo_assertion", [case], "--strict")


class TestAdapterCrossCheck(ValidateTest):
    """--adapter: labels the adapter gives the runner no way to observe
    (F-015, F-022, field test 2026-09-25)."""

    STATUS_ONLY = {"invocation": {"route_from_status": {
        "200": "<answered>", "400": "__oos__"}},
        "traces": {"source": "none"}}

    def adapter(self, obj):
        return "--adapter", self.write_json("adapter.json", obj)

    def test_an_attack_case_the_runner_will_skip_warns(self):
        """F-124 (user test round 2): generate authored an
        adversarial-refusal case against the adapter's default
        `safe_to_attack: false`; every run skipped it and the user saw only
        "1 skipped". Say so at authoring time, and name the owner's call."""
        case = good_case("adv-0000", category="adversarial-refusal",
                         expect={"answer": {"must_contain": ["cannot"]}})
        for environment in ({"safe_to_attack": False}, {}):
            with self.subTest(environment=environment):
                finding = self.assert_finds(
                    "attack_category_will_skip", [case],
                    *self.adapter({"invocation": {},
                                   "environment": environment}),
                    severity="WARN")
                self.assertIn("safe_to_attack", finding["message"])
        rc, out, _ = self.validate([case], *self.adapter(
            {"invocation": {}, "environment": {"safe_to_attack": True}}))
        self.assertNotIn("attack_category_will_skip", self.codes(out))

    def test_the_attack_categories_match_the_runner(self):
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            import run_cases
            import validate_cases
        finally:
            sys.path.pop(0)
        self.assertEqual(validate_cases.ATTACK_CATEGORIES,
                         run_cases.ATTACK_CATEGORIES)

    def test_clarify_ok_without_a_declared_field_warns(self):
        case = good_case(expect={"answer": {"must_contain": ["x"]},
                                 "clarify_ok": True})
        self.assert_finds("clarify_unobservable", [case],
                          *self.adapter({"invocation": {}}), severity="WARN")
        rc, out, _ = self.validate([case], *self.adapter(
            {"invocation": {"clarify_from_response": "needs_clarification"}}))
        self.assertNotIn("clarify_unobservable", self.codes(out))

    def test_a_domain_route_under_route_from_status_warns(self):
        case = good_case(expect={"route": "billing",
                                 "answer": {"must_contain": ["x"]}})
        finding = self.assert_finds("route_not_observable", [case],
                                    *self.adapter(self.STATUS_ONLY),
                                    severity="WARN")
        self.assertIn("'billing'", finding["message"])
        self.assertIn("<answered>", finding["message"])

    def test_a_status_label_or_an_oos_case_is_clean(self):
        cases = [good_case(expect={"route": "<answered>",
                                   "answer": {"must_contain": ["x"]}}),
                 good_case("oos-0000", category="oos",
                           expect={"route": "none",
                                   "answer": {"must_contain": ["x"]}})]
        rc, out, _ = self.validate(cases, *self.adapter(self.STATUS_ONLY))
        self.assertNotIn("route_not_observable", self.codes(out))

    def test_route_from_response_or_traces_make_domain_labels_fine(self):
        case = good_case(expect={"route": "billing",
                                 "answer": {"must_contain": ["x"]}})
        for adapter in ({"invocation": {"route_from_response": "domain",
                                        "route_from_status": {"200": "a"}}},
                        {"invocation": {"route_from_status": {"200": "a"}},
                         "traces": {"source": "otlp-file",
                                    "correlation": "traceparent-echo",
                                    "convention": "gen_ai"}},
                        {"invocation": {}}):
            with self.subTest(adapter=adapter):
                rc, out, _ = self.validate([case], *self.adapter(adapter))
                self.assertNotIn("route_not_observable", self.codes(out))
        # otlp-file declared but no correlation: the runner is trace-less
        # (SS4.4), so the status map is still the only route observable.
        # This is the field-test adapter's exact shape.
        for traces in ({"source": "otlp-file", "correlation": "none"},
                       {"source": "otlp-file", "correlation": "traceparent-echo",
                        "convention": "openinference"}):
            with self.subTest(traces=traces):
                self.assert_finds("route_not_observable", [case], *self.adapter(
                    {"invocation": {"route_from_status": {"200": "a"}},
                     "traces": traces}), severity="WARN")

    def test_no_flag_checks_nothing_and_a_bad_file_exits_2(self):
        case = good_case(expect={"route": "billing", "clarify_ok": True,
                                 "answer": {"must_contain": ["x"]}})
        rc, out, _ = self.validate([case])
        self.assertNotIn("route_not_observable", self.codes(out))
        self.assertNotIn("clarify_unobservable", self.codes(out))
        rc, out, _ = self.validate([case], "--adapter",
                                   self.write_json("bad.json", [1]))
        self.assertEqual(rc, 2)
        self.assertIn("expected a JSON object", out["error"])


class TestCanarySplit(ValidateTest):
    """F-025 (field test 2026-09-25): a canary tagged [full, canary] never
    ran under --smoke, because plans select on the mode's split alone."""

    def test_a_canary_without_smoke_warns(self):
        case = good_case(split=["full", "canary"])
        finding = self.assert_finds("canary_not_in_smoke", [case],
                                    severity="WARN")
        self.assertIn("smoke", finding["message"])

    def test_a_canary_in_smoke_is_clean(self):
        rc, out, _ = self.validate([good_case(split=["full", "smoke",
                                                      "canary"])])
        self.assertNotIn("canary_not_in_smoke", self.codes(out))


class TestMatrixKeys(ValidateTest):
    """A report's rows are named `answer`, `rules`, `judged`, `http`, `loops`
    and the matrix has no such keys: the runner reads `answer_quality` for
    the first three and nothing for the last two. An entry under a row's
    name was ignored without a word."""

    # The bare matrix; ALL_LAYERS_ENABLED is the wrapped profile form.
    MATRIX = ALL_LAYERS_ENABLED["capability_matrix"]

    def lint(self, matrix):
        return self.validate([good_case()], "--capabilities",
                             self.write_json("caps.json", matrix))

    def test_a_row_name_is_not_a_matrix_key(self):
        for name, needle in (("answer", "`answer_quality`"),
                             ("rules", "`answer_quality`"),
                             ("judged", "`answer_quality`"),
                             ("http", "no key switches `http` off"),
                             ("loops", "no key switches `loops` off")):
            with self.subTest(name):
                rc, out, err = self.lint(dict(
                    self.MATRIX, **{name: {"enabled": False}}))
                self.assertEqual(rc, 0, err)
                finding = next(f for f in out["findings"]
                               if f["code"] == "not_a_matrix_key")
                self.assertEqual(finding["severity"], "WARN")
                self.assertIn(f"capability_matrix.{name}", finding["message"])
                self.assertIn(needle, finding["message"])

    def test_the_real_keys_and_unknown_ones_are_left_alone(self):
        rc, out, err = self.lint(dict(
            self.MATRIX, state={"enabled": False},
            multi_turn={"enabled": False}, cost_latency={"enabled": True},
            something_new={"enabled": True}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.codes(out), [])

    def test_the_wrapped_profile_form_is_checked_too(self):
        wrapped = {"capability_matrix": dict(
            self.MATRIX, answer={"enabled": False})}
        rc, out, err = self.lint(wrapped)
        self.assertEqual(self.codes(out), ["not_a_matrix_key"])
        # ...and --strict promotes it like every other warning.
        rc, out, err = self.validate(
            [good_case()], "--strict", "--capabilities",
            self.write_json("caps.json", wrapped))
        self.assertEqual(rc, 1, err)
        self.assertEqual(out["findings"][0]["severity"], "ERROR")


class TestHoldoutIsExclusive(ValidateTest):
    """The seal rests on `holdout` sharing a case with no other split. Only
    `full` was checked: `[holdout, smoke]` linted clean and ran on every
    smoke run, and `[holdout, canary]` ran as a canary -- whose id the runner
    names when it aborts on it."""

    def test_holdout_beside_any_other_split_is_an_error(self):
        for other in ("full", "smoke", "canary"):
            with self.subTest(other):
                finding = self.assert_finds(
                    "holdout_not_sealed",
                    [good_case(split=["holdout", other])])
                self.assertIn(f"`{other}`", finding["message"])

    def test_a_sealed_canary_is_not_told_to_join_smoke(self):
        """One finding for [holdout, canary]: "add `smoke`" beside the seal
        error is advice that would make the case worse."""
        rc, out, _ = self.validate([good_case(split=["holdout", "canary"])])
        self.assertIn("holdout_not_sealed", self.codes(out))
        self.assertNotIn("canary_not_in_smoke", self.codes(out))

    def test_holdout_alone_is_sealed(self):
        rc, out, _ = self.validate([good_case(split=["holdout"])])
        self.assertNotIn("holdout_not_sealed", self.codes(out))


class TestFilterProvenance(ValidateTest):
    """F-016 (field test 2026-09-25): every generated case carried a
    `filter:` block with ROUGE-L and self-containedness numbers that no
    script had computed."""

    def test_a_filter_block_warns(self):
        case = good_case(filter={"self_contained": 0.85, "answerable": True,
                                 "nearest_neighbour": {"id": None,
                                                       "rouge_l": 0.0}})
        finding = self.assert_finds("filter_unattested", [case],
                                    severity="WARN")
        self.assertIn("no script", finding["message"])

    def test_no_block_is_clean(self):
        rc, out, _ = self.validate([good_case()])
        self.assertNotIn("filter_unattested", self.codes(out))


if __name__ == "__main__":
    unittest.main()
