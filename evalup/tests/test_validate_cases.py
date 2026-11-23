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
        "schema_version": 1,
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

    def test_usable_by_split_counts_only_accepted_gating_cases(self):
        """optimize/SKILL.md precondition 2 reads `full`'s count: a human
        accepted the case, it gates, and it is not a canary. Every split is
        a key, zero included. A sealed pending case reads 0 here, which is
        why optimize counts the holdout by `by_split`, never by this."""
        cases = [
            good_case("a-ok-1", split=["full", "smoke"]),
            good_case("b-pending-2", gating=False,
                      review={"status": "pending"}),
            good_case("c-not-gating-3", gating=False),
            good_case("d-machine-4", review={"status": "accepted",
                                             "by": "generate-review"}),
            good_case("e-canary-5", split=["full", "smoke", "canary"],
                      expect={"result": {"scalar": 7}}),
            good_case("f-sealed-6", split=["holdout"], gating=False,
                      review={"status": "pending"}),
            good_case("g-quarantined-7", review={"status": "quarantined"}),
            good_case("h-no-reviewer-8", review={"status": "accepted"}),
        ]
        _, out, _ = self.validate(cases)
        summary = out["summary"]
        self.assertEqual(summary["by_split"],
                         {"canary": 1, "full": 7, "holdout": 1, "smoke": 2})
        self.assertEqual(summary["usable_by_split"],
                         {"canary": 0, "full": 1, "holdout": 0, "smoke": 1})
        # The gap to by_split, explained: one reason per case, per split.
        self.assertEqual(summary["unusable_by_split"], {
            "canary": {"canary": 1},
            "full": {"canary": 1, "machine_signed": 2, "not_gating": 1,
                     "review_pending": 1, "review_quarantined": 1},
            "holdout": {"review_pending": 1},
            "smoke": {"canary": 1},
        })
        for name, n in summary["by_split"].items():
            self.assertEqual(
                n, summary["usable_by_split"][name]
                + sum(summary["unusable_by_split"][name].values()))

    def test_a_quarantined_sealed_case_is_counted_apart(self):
        """optimize step 4 takes Q, the holdout cases out of its headroom,
        from `unusable_by_split.holdout.review_quarantined` (#142): every
        sealed case is pending, so the key is absent until one is
        quarantined, and a pending one is never in it."""
        sealed = {"split": ["holdout"], "gating": False}
        cases = [good_case("a-sealed-1", review={"status": "pending"},
                           **sealed),
                 good_case("b-sealed-2", review={"status": "pending"},
                           **sealed)]
        _, out, _ = self.validate(cases)
        self.assertEqual(out["summary"]["unusable_by_split"]["holdout"],
                         {"review_pending": 2})
        cases[1] = good_case("b-sealed-2", review={"status": "quarantined"},
                             **sealed)
        _, out, _ = self.validate(cases)
        self.assertEqual(out["summary"]["unusable_by_split"]["holdout"],
                         {"review_pending": 1, "review_quarantined": 1})

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

    def test_any_of_shapes(self):
        """#51: any_of is a list of non-empty lists; the scorer exits 2 on
        every shape below, so the linter names each one first."""
        for any_of, code in ((["a", "b"], "any_of_flat"),
                             ([["a"], "b"], "any_of_flat"),
                             ("a", "bad_any_of"),
                             ([], "bad_any_of"),
                             (None, "bad_any_of"),
                             ([["a"], []], "bad_any_of"),
                             ([[1]], "bad_any_of")):
            with self.subTest(any_of=any_of):
                self.assert_finds(code, [good_case(expect={
                    "tools": {"any_of": any_of}})])
        self.assert_finds("any_of_with_order", [good_case(expect={
            "tools": {"any_of": [["a"]], "order": ["a"]}})])
        # Split across every_turn and a turn, they merge into the same pair.
        f = self.assert_finds("any_of_with_order", [conversation(
            every_turn={"tools": {"any_of": [["a"], ["b"]]}},
            turns=[{"user": "x", "expect": {"tools": {"order": ["c"]}}},
                   {"user": "y"}])])
        self.assertIn("input.turns[0].expect", f["message"])
        f = self.assert_finds("any_of_with_order", [conversation(
            every_turn={"tools": {"order": ["c"]}},
            expect={"tools": {"any_of": [["a"]]}})])
        self.assertIn("final turn", f["message"])
        rc, out, err = self.validate([good_case(expect={"tools": {
            "subset": ["s"], "any_of": [["a"], ["b", "c"]],
            "forbidden": ["w"]}})])
        self.assertEqual((rc, out["findings"]), (0, []), err)

    def test_order_refused_before_stable(self):
        """#51: the trajectory layer is on at `early` for which tools were
        called; a tool SEQUENCE waits for stage: stable."""
        case = good_case(expect={"tools": {"order": ["a", "b"]}})
        early = self.write_json("early.json", {"schema_version": 1,
                                               "stage": "early"})
        f = self.assert_finds("order_before_stable", [case], "--profile",
                              early)
        self.assertIn("stage: early", f["message"])
        # On a checkpoint and on every_turn too, named by where it sits.
        f = self.assert_finds("order_before_stable", [conversation(
            every_turn={"tools": {"order": ["a"]}})], "--profile", early)
        self.assertTrue(f["message"].startswith("every_turn.tools.order"))
        ok = self.write_json("ok.json", {"schema_version": 1,
                                         "stage": "stable"})
        rc, out, err = self.validate([case], "--profile", ok)
        self.assertEqual((rc, out["findings"]), (0, []), err)
        # subset/forbidden/any_of are not order: fine at early.
        rc, out, err = self.validate([good_case(expect={"tools": {
            "subset": ["a"], "any_of": [["b"], ["c"]],
            "forbidden": ["w"]}})], "--profile", early)
        self.assertEqual((rc, out["findings"]), (0, []), err)
        # No stage, or no profile: nothing to check against.
        rc, out, err = self.validate([case])
        self.assertEqual(rc, 0, err)

    def test_legacy_stage_names_still_work(self):
        """#42/#38: a pre-#42 profile keeps its meaning. pre-stability reads
        as early (order refused), traffic/production as stable (order
        allowed), each with a stage_renamed WARN that --strict leaves a
        WARN, because it is a migration, not a defect in the suite."""
        case = good_case(expect={"tools": {"order": ["a", "b"]}})
        for old, new in (("pre-stability", "early"), ("traffic", "stable"),
                         ("production", "stable")):
            prof = self.write_json("old.json", {"schema_version": 1,
                                                "stage": old})
            for strict in ((), ("--strict",)):
                with self.subTest(stage=old, strict=bool(strict)):
                    rc, out, err = self.validate([case], "--profile", prof,
                                                 *strict)
                    self.assertNotIn("Traceback", err, err)
                    renamed = [f for f in out["findings"]
                               if f["code"] == "stage_renamed"]
                    self.assertEqual(len(renamed), 1, out["findings"])
                    self.assertEqual(renamed[0]["severity"], "WARN")
                    self.assertIn(f"stage: {new}", renamed[0]["message"])
                    refused = [f for f in out["findings"]
                               if f["code"] == "order_before_stable"]
                    self.assertEqual(bool(refused), new == "early", out)
                    if refused:
                        # Names what the file says, not only the mapping.
                        self.assertIn(f"stage: {old} (read as {new})",
                                      refused[0]["message"])
                    self.assertEqual(rc, 1 if new == "early" else 0, out)
        # The quickstart's validator run must not grow a finding: the new
        # names produce none.
        for stage in ("early", "stable"):
            prof = self.write_json("new.json", {"schema_version": 1,
                                                "stage": stage})
            rc, out, err = self.validate([good_case()], "--profile", prof,
                                         "--strict")
            self.assertEqual((rc, out["findings"]), (0, []), stage)

    def test_unknown_stage_warns_and_reads_as_early(self):
        prof = self.write_json("bad.json", {"schema_version": 1,
                                            "stage": "beta"})
        f = self.assert_finds("unknown_stage", [good_case()], "--profile",
                              prof, severity="WARN")
        self.assertIn("early, stable", f["message"])
        # Read as early: order is still refused.
        self.assert_finds("order_before_stable", [good_case(expect={
            "tools": {"order": ["a"]}})], "--profile", prof)
        # --strict promotes it like any other WARN.
        self.assert_finds("unknown_stage", [good_case()], "--profile", prof,
                          "--strict")

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

    def test_a_delegate_or_a_model_never_accepts_a_case(self):
        """#100: the rule-confirmation rule holds for acceptance too
        (generate SKILL.md): a model's name, or an owner's answer a delegate
        relayed, is not a review -- and it is not counted as usable."""
        for by in RELAYED_SIGNERS + ("Claude", "gpt-4o"):
            with self.subTest(by=by):
                case = good_case(review={"status": "accepted", "by": by})
                self.assert_finds("machine_accepted", [case], severity="WARN")
                out = self.validate([case])[1]
                self.assertEqual(out["summary"]["usable_by_split"]["full"], 0)
        for by in HUMAN_SIGNERS:
            with self.subTest(by=by):
                rc, out, _ = self.validate([good_case(
                    review={"status": "accepted", "by": by})])
                self.assertEqual((rc, out["findings"]), (0, []))
                self.assertEqual(out["summary"]["usable_by_split"]["full"], 1)

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

    def test_coverage_grid_counts_cases_outside_the_holdout(self):
        # #145: by_unit minus the training cases would be the sealed
        # spread by unit, so the holdout stays out of it.
        cases = [good_case("a-happy-1", unit="Billing", test_type="MFT"),
                 good_case("a-happy-2", unit="Billing", test_type="MFT",
                           split=["holdout"]),
                 good_case("a-happy-3", unit="Refunds", test_type="MFT",
                           split=["holdout"])]
        manifest = self.write_json(
            "manifest.json",
            {"coverage_grid": {"by_unit": {"Billing": {"total": 1,
                                                        "MFT": 1}}}})
        rc, out, err = self.validate(cases, "--manifest", manifest)
        self.assertNotIn("manifest_mismatch", self.codes(out), str(out) + err)
        manifest = self.write_json(
            "manifest.json",
            {"coverage_grid": {"by_unit": {"Billing": {"total": 2}}}})
        f = self.assert_finds("manifest_mismatch", cases,
                              "--manifest", manifest)
        self.assertIn("outside the holdout", f["message"])

    def test_no_manifest_flag_skips_the_check_entirely(self):
        rc, out, err = self.validate([good_case()])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("manifest_mismatch", self.codes(out))

    def test_bad_manifest_file_exits_2(self):
        manifest = self.write_json("manifest.json", ["not", "an", "object"])
        rc, out, err = self.validate([good_case()], "--manifest", manifest)
        self.assertEqual(rc, 2, err)
        self.assertIn("error", out)


class TestLabelFixes(ValidateTest):
    """dataset.yaml's `label_fixes:` (#53): a proposed fix waits there for
    the owner instead of in the last session's chat. The validator accepts
    the section, rejects a malformed entry, and lists the open ones."""

    FIX = {"case": "billing-happy-0000abcd", "status": "pending",
           "change": "drop the refusal regex: it matches the correct refusal"}

    def manifest(self, fixes, **extra):
        return self.write_json("manifest.json",
                               dict({"schema_version": 1, "cases": 1,
                                     "label_fixes": fixes}, **extra))

    def test_open_fixes_are_accepted_and_listed(self):
        approved = dict(self.FIX, status="approved")
        applied = dict(self.FIX, status="applied", applied_in=2)
        rc, out, err = self.validate(
            [good_case()], "--strict", "--manifest",
            self.manifest([self.FIX, approved, applied], dataset_version=2))
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(out["summary"]["label_fixes_open"],
                         [{"case": "billing-happy-0000abcd",
                           "status": "pending"},
                          {"case": "billing-happy-0000abcd",
                           "status": "approved"}])
        self.assertEqual(out["error_count"], 0, out["findings"])

    def test_none_open_is_an_empty_list_and_no_manifest_no_key(self):
        _, out, _ = self.validate([good_case()], "--manifest",
                                  self.write_json("manifest.json",
                                                  {"cases": 1}))
        self.assertEqual(out["summary"]["label_fixes_open"], [])
        _, out, _ = self.validate([good_case()])
        self.assertNotIn("label_fixes_open", out["summary"])

    def test_a_malformed_entry_is_an_error(self):
        for fixes in ({"case": "x"}, ["not a mapping"],
                      [dict(self.FIX, status="done")],
                      [dict(self.FIX, change="")],
                      [dict(self.FIX, status="applied")],
                      [dict(self.FIX, status="applied", applied_in=5)]):
            with self.subTest(fixes=fixes):
                self.assert_finds("label_fix_invalid", [good_case()],
                                  "--manifest",
                                  self.manifest(fixes, dataset_version=2))

    def test_applied_in_is_bounded_by_the_older_version_key_too(self):
        manifest = self.write_json("manifest.json", {
            "cases": 1, "version": 2,
            "label_fixes": [dict(self.FIX, status="applied", applied_in=5)]})
        self.assert_finds("label_fix_invalid", [good_case()],
                          "--manifest", manifest)

    def test_an_open_fix_for_an_unknown_case_is_an_error(self):
        self.assert_finds("label_fix_unknown_case", [good_case()],
                          "--manifest",
                          self.manifest([dict(self.FIX, case="c-gone")]))
        # A rejected fix for a case since deleted is history, not a defect.
        rc, out, _ = self.validate(
            [good_case()], "--manifest",
            self.manifest([dict(self.FIX, case="c-gone",
                                status="rejected")]))
        self.assertNotIn("label_fix_unknown_case", self.codes(out))


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

    def test_the_combined_document_is_an_adapter(self):
        """#125: convert_suite.py's combined document passed to --adapter is
        read through its `adapter` key, as --profile reads `profile`; linted
        whole, it warned schema_version_missing and
        attack_category_will_skip, both falsely."""
        case = good_case("adv-0000", category="adversarial-refusal",
                         expect={"answer": {"must_contain": ["cannot"]}})
        doc = self.write_json("converted.json", {
            "adapter": {"schema_version": 1, "invocation": {},
                        "environment": {"safe_to_attack": True}},
            "capability_matrix": ALL_LAYERS_ENABLED["capability_matrix"],
            "cases": [case]})
        rc, out, err = run_validate("--cases", doc, "--capabilities", doc,
                                    "--adapter", doc)
        self.assertNotIn("Traceback", err, err)
        codes = self.codes(out)
        self.assertNotIn("schema_version_missing", codes)
        self.assertNotIn("attack_category_will_skip", codes)
        # The adapter's own keys are still read: an unsafe one still warns.
        doc = self.write_json("converted.json", {
            "adapter": {"schema_version": 1, "invocation": {},
                        "environment": {"safe_to_attack": False}},
            "capability_matrix": ALL_LAYERS_ENABLED["capability_matrix"],
            "cases": [case]})
        rc, out, _ = run_validate("--cases", doc, "--capabilities", doc,
                                  "--adapter", doc)
        self.assertIn("attack_category_will_skip", self.codes(out))
        self.assertNotIn("schema_version_missing", self.codes(out))

    def test_a_suite_without_an_adapter_object_is_refused(self):
        for adapter in (None, "adapter.yaml", []):
            with self.subTest(adapter=adapter):
                body = {"cases": [good_case()]}
                if adapter is not None:
                    body["adapter"] = adapter
                doc = self.write_json("suite.json", body)
                rc, out, _ = run_validate("--cases", doc, "--no-capabilities",
                                          "--adapter", doc)
                self.assertEqual(rc, 2)
                self.assertIn("--split-dir", out["error"])

    def test_a_real_adapter_carrying_cases_is_still_an_adapter(self):
        """The Grok review of #125: only a document with no adapter key of
        its own is refused; a flat adapter that also has `cases` is linted
        as itself, as before."""
        case = good_case("adv-0000", category="adversarial-refusal",
                         expect={"answer": {"must_contain": ["cannot"]}})
        for safe, warns in ((True, False), (False, True)):
            with self.subTest(safe_to_attack=safe):
                rc, out, _ = self.validate([case], *self.adapter({
                    "schema_version": 1, "invocation": {},
                    "environment": {"safe_to_attack": safe},
                    "cases": [case]}))
                self.assertIn(rc, (0, 1))
                codes = self.codes(out)
                self.assertNotIn("schema_version_missing", codes)
                self.assertEqual("attack_category_will_skip" in codes, warns)

    # #103: an approval interrupt with no text leaves `answer` unscored.
    AG_UI = {"invocation": {"response_format": "sse",
                            "sse": {"protocol": "ag-ui"}},
             "tools": [{"name": "apply_changes", "side_effects": "needs-mock"},
                       {"name": "send_email", "side_effects": "never-live"},
                       {"name": "search_docs", "side_effects": "safe-live"}]}

    def proposal(self, tools, answer=None):
        return good_case(expect={
            "tools": tools,
            "answer": {"must_not_contain": ["/(?i:done)/"]}
            if answer is None else answer})

    def test_an_answer_beside_a_gated_tool_warns_under_ag_ui(self):
        for tools in ({"subset": ["apply_changes"]},
                      {"order": ["search_docs", "send_email"]},
                      {"any_of": [["search_docs"], ["apply_changes"]]}):
            with self.subTest(tools=tools):
                finding = self.assert_finds(
                    "answer_after_interrupt", [self.proposal(tools)],
                    *self.adapter(self.AG_UI), severity="WARN")
                self.assertIn("safe-live", finding["message"])
                self.assertIn("unscored", finding["message"])

    def test_the_interrupt_warning_stays_a_warning_under_strict(self):
        rc, out, _ = self.validate(
            [self.proposal({"subset": ["apply_changes"]})],
            *self.adapter(self.AG_UI), "--strict")
        finding = next(f for f in out["findings"]
                       if f["code"] == "answer_after_interrupt")
        self.assertEqual(finding["severity"], "WARN")

    def test_no_interrupt_warning_without_all_three(self):
        json_adapter = dict(self.AG_UI, invocation={})
        data_path = dict(self.AG_UI, invocation={
            "response_format": "sse",
            "sse": {"protocol": "data-path", "text_path": "t"}})
        quiet = (
            ([self.proposal({"subset": ["search_docs"]})], self.AG_UI),
            ([self.proposal({"forbidden": ["apply_changes"]})], self.AG_UI),
            ([self.proposal({"subset": ["unknown_tool"]})], self.AG_UI),
            ([self.proposal({"subset": ["apply_changes"]},
                            answer={"must_contain": []})], self.AG_UI),
            ([self.proposal({"subset": ["apply_changes"]})], json_adapter),
            ([self.proposal({"subset": ["apply_changes"]})], data_path),
        )
        for cases, adapter in quiet:
            with self.subTest(cases=cases, adapter=adapter["invocation"]):
                rc, out, _ = self.validate(cases, *self.adapter(adapter))
                self.assertNotIn("answer_after_interrupt", self.codes(out))

    def test_a_conversation_turn_or_every_turn_counts(self):
        ag_ui = dict(self.AG_UI, invocation=dict(
            self.AG_UI["invocation"], conversation={"memory": "session"}))
        on_turn = conversation(turns=[
            {"user": "change it", "expect": {
                "tools": {"subset": ["apply_changes"]},
                "answer": {"must_not_contain": ["done"]}}},
            {"user": "thanks"}])
        every = conversation(
            "c-convo-0002",
            turns=[{"user": "change it",
                    "expect": {"tools": {"subset": ["apply_changes"]}}},
                   {"user": "thanks"}],
            every_turn={"answer": {"must_not_contain": ["done"]}})
        two = [{"user": "change it"}, {"user": "thanks"}]
        # The runner overlays every_turn on each turn (conversation_turns),
        # so a tool required only there is on the scored block too.
        tool_on_every = conversation(
            "c-convo-0003", turns=two,
            every_turn={"tools": {"subset": ["apply_changes"]}},
            expect={"answer": {"must_not_contain": ["done"]}})
        both_on_every = conversation(
            "c-convo-0004", turns=two, expect={},
            every_turn={"tools": {"order": ["apply_changes"]},
                        "answer": {"must_not_contain": ["done"]}})
        answer_on_checkpoint = conversation(
            "c-convo-0005",
            turns=[{"user": "change it", "expect": {
                "answer": {"must_not_contain": ["done"]}}},
                {"user": "thanks"}],
            every_turn={"tools": {"any_of": [["apply_changes"]]}})
        for case in (on_turn, every, tool_on_every, both_on_every,
                     answer_on_checkpoint):
            with self.subTest(case=case["id"]):
                self.assert_finds("answer_after_interrupt", [case],
                                  *self.adapter(ag_ui), severity="WARN")

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

    def test_a_non_200_status_under_an_exec_adapter_warns(self):
        # #39: run_cases skips it, since an exec command has no status.
        case = good_case(expect={"http": {"status": 403},
                                 "answer": {"must_contain": ["x"]}})
        finding = self.assert_finds(
            "status_unobservable_in_exec", [case],
            *self.adapter({"invocation": {"mode": "exec"}}), severity="WARN")
        self.assertIn("route_from_response", finding["message"])
        ok = good_case(expect={"http": {"status": 200},
                               "answer": {"must_contain": ["x"]}})
        for cases, adapter in (([ok], {"invocation": {"mode": "exec"}}),
                               ([case], {"invocation": {"mode": "http"}})):
            rc, out, _ = self.validate(cases, *self.adapter(adapter))
            self.assertNotIn("status_unobservable_in_exec", self.codes(out))

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


class TestSchemaVersion(ValidateTest):
    """#38: every state file declares schema_version. A missing, older or
    newer value WARNs and names the migration -- never an error, and the one
    WARN --strict leaves alone, so an old state dir is a migration to do, not
    a broken CI job."""

    def test_a_case_without_it_warns_and_names_the_migration(self):
        case = good_case()
        del case["schema_version"]
        finding = self.assert_finds("schema_version_missing", [case],
                                    severity="WARN")
        self.assertIn("schema_version: 1", finding["message"])
        self.assertIn("/evalup:generate", finding["message"])

    def test_strict_does_not_promote_it(self):
        case = good_case()
        del case["schema_version"]
        rc, out, err = self.validate([case], "--strict")
        self.assertEqual(rc, 0, out)
        finding = next(f for f in out["findings"]
                       if f["code"] == "schema_version_missing")
        self.assertEqual(finding["severity"], "WARN")

    def test_older_and_newer_values_warn(self):
        for value, code in ((0, "schema_version_old"),
                            (99, "schema_version_newer"),
                            ("1", "schema_version_invalid")):
            with self.subTest(value=value):
                self.assert_finds(code, [good_case(schema_version=value)],
                                  severity="WARN")

    def test_the_current_version_is_silent(self):
        rc, out, err = self.validate([good_case()])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("schema_version_missing", self.codes(out))

    def test_adapter_profile_and_manifest_are_checked(self):
        adapter = self.write_json("adapter.json", {"adapter_version": 1,
                                                   "invocation": {}})
        profile = self.write_json("profile.json", {"profile_version": 1})
        manifest = self.write_json("manifest.json", {})
        rc, out, err = self.validate([good_case()], "--adapter", adapter,
                                     "--profile", profile,
                                     "--manifest", manifest)
        self.assertNotIn("Traceback", err)
        messages = [f["message"] for f in out["findings"]
                    if f["code"] == "schema_version_missing"]
        self.assertEqual(len(messages), 3, out["findings"])
        joined = " ".join(messages)
        # The pre-#38 keys are named as the old spelling of the field.
        self.assertIn("`adapter_version: 1`", joined)
        self.assertIn("`profile_version: 1`", joined)
        self.assertIn("/evalup:discover", joined)
        self.assertIn("datasets/dataset.yaml", joined)
        profile = self.write_json("profile.json", {"schema_version": 1})
        rc, out, err = self.validate([good_case()], "--profile", profile)
        self.assertNotIn("schema_version_missing", self.codes(out))


# #100: signatures that name a person but carry another's word.
RELAYED_SIGNERS = (
    "billing-owner (delegate: Fable, 2026-11-22)",
    "billing-owner (delegated to a subagent)", "billing-owner, delegation",
    "billing-owner (relayed)", "billing-owner, relayed",
    "billing-owner by proxy", "Fable on behalf of billing-owner",
    "opus", "Sonnet 4.5", "haiku", "grok-4.7", "llama-3", "Mistral",
    "gemma", "qwen2", "llama3.1", "gpt4", "DeepSeek", "codex", "o3-mini",
    "Kimi", "gpt4o", "gpt4o-mini", "chatgpt4o", "claude3.5", "o3mini",
    "mixtral8x7b", "billing-owner (subagent)", "billing-owner (via subagents)",
    "billing-owner (delegating)", "billing-owner (relaying)",
    "billing-owner (proxied)", "billing-owner (on-behalf)")
# ...and people's names that merely contain those letters.
HUMAN_SIGNERS = ("billing-owner", "billing-owner (product lead)",
                 "Aisha (2026-11-22)", "Delegato", "Grokowski", "Haikuson",
                 "Opusz", "Relayne", "Relay Smith", "Nora (tier o1)",
                 "priority o3")


def a_rule(rule_id="refund-cap", **overrides):
    """A complete rule entry (#25): quote, source, date, status."""
    rule = {"id": rule_id, "rule": "never promise a refund above the total",
            "quote": "Refunds never exceed the order total.",
            "source": "docs/policy.md §Refunds", "date": "2026-11-01",
            "status": "confirmed", "confirmed_by": "billing-owner"}
    rule.update(overrides)
    return rule


class TestTraceability(ValidateTest):
    """#25: cases link to profile rules and risks; an unconfirmed rule never
    gates; a rule with no case is a gap. Nothing here scores a rule."""

    def profile(self, rules=(), risks=()):
        return self.write_json("profile.json", {
            "schema_version": 1, "business_rules": list(rules),
            "risks": list(risks)})

    def lint(self, cases, rules=(), risks=(), *extra):
        rc, out, err = self.validate(cases, "--profile",
                                     self.profile(rules, risks), *extra)
        self.assertNotIn("Traceback", err, err)
        return rc, out

    def test_a_linked_suite_is_clean_and_reports_its_coverage(self):
        rc, out = self.lint(
            [good_case(covers=["refund-cap", "risk-1"])], [a_rule()],
            [{"id": "risk-1", "risk": "leaks another tenant's invoice"}])
        self.assertEqual((rc, out["findings"]), (0, []))
        trace = out["summary"]["traceability"]
        self.assertEqual(trace["rules"], {"refund-cap": {
            "status": "confirmed", "cases": ["billing-happy-0000abcd"],
            "holdout_cases": 0}})
        self.assertEqual(trace["risks"]["risk-1"]["cases"],
                         ["billing-happy-0000abcd"])
        self.assertEqual((trace["gaps"], trace["unconfirmed"],
                          trace["contradictions"]), ([], [], []))

    def test_a_sealed_case_is_a_count_never_an_id(self):
        # #133: a holdout id beside the rule it covers says what the sealed
        # case is about; rule_report.py gives counts only, and so does this.
        sealed = good_case("billing-sealed-0000beef", split=["holdout"],
                           covers=["refund-cap", "refund-cap", "risk-1"])
        rc, out = self.lint(
            [good_case(covers=["refund-cap"]), sealed,
             good_case("billing-sealed-0000cafe", split=["holdout"],
                       covers=["plain-text"])],
            [a_rule(), a_rule("plain-text")],
            [{"id": "risk-1", "risk": "leaks another tenant's invoice"}])
        trace = out["summary"]["traceability"]
        self.assertNotIn("sealed", json.dumps(
            {k: trace[k] for k in ("rules", "risks", "gaps")}))
        self.assertEqual(trace["rules"]["refund-cap"],
                         {"status": "confirmed",
                          "cases": ["billing-happy-0000abcd"],
                          "holdout_cases": 1})
        self.assertEqual(trace["risks"]["risk-1"],
                         {"cases": [], "holdout_cases": 1})
        # Covered by a sealed case alone is covered: no gap.
        self.assertEqual(trace["rules"]["plain-text"]["holdout_cases"], 1)
        self.assertEqual(trace["gaps"], [])

    def test_a_rule_with_no_case_is_a_gap_not_a_finding(self):
        rc, out = self.lint([good_case(covers=["refund-cap"])],
                            [a_rule(), a_rule("plain-text")],
                            [{"id": "risk-1", "risk": "x"}], "--strict")
        self.assertEqual((rc, out["findings"]), (0, []))
        self.assertEqual(out["summary"]["traceability"]["gaps"],
                         ["rule plain-text has no case",
                          "risk risk-1 has no case"])

    def test_no_traceability_summary_without_a_profile(self):
        rc, out, _ = self.validate([good_case()])
        self.assertNotIn("traceability", out["summary"])

    def test_an_unconfirmed_rule_never_gates(self):
        for label, rule in (
                ("inferred", a_rule(status="inferred", confirmed_by=None)),
                ("no status", {k: v for k, v in a_rule().items()
                               if k != "status"}),
                ("a typo", a_rule(status="confimred")),
                ("confirmed by nobody", a_rule(confirmed_by=None)),
                ("confirmed by a machine", a_rule(confirmed_by="generate")),
                ("confirmed by an agent", a_rule(confirmed_by="claude"))):
            with self.subTest(label):
                rc, out = self.lint([good_case(covers=["refund-cap"])],
                                    [rule])
                self.assertIn("unconfirmed_rule_gates", self.codes(out))
                self.assertEqual(rc, 1)
                self.assertEqual(
                    out["summary"]["traceability"]["unconfirmed"],
                    ["refund-cap"])

    def test_a_model_or_agent_never_confirms_a_rule(self):
        """A rule is confirmed in the interview, by a person: the names an
        agent signs with are refused as whole words, any case."""
        for by in ("claude", "Claude Code", "discover", "evalup",
                   "evalup-discover", "assistant", "agent", "AI", "llm",
                   "model", "gpt-4o", "gemini", "copilot", "auto",
                   "test-generator", "generate-review", "machine"):
            with self.subTest(by=by):
                rc, out = self.lint([good_case(covers=["refund-cap"])],
                                    [a_rule(confirmed_by=by)])
                self.assertIn("rule_confirmed_unattributed", self.codes(out))
                self.assertIn("unconfirmed_rule_gates", self.codes(out))
                self.assertEqual(rc, 1)
        # Whole words only: these are people's names or handles.
        for by in ("billing-owner", "Aisha", "Gaia Rossi", "j.agentson",
                   "modelina", "Bottomley"):
            with self.subTest(by=by):
                rc, out = self.lint([good_case(covers=["refund-cap"])],
                                    [a_rule(confirmed_by=by)])
                self.assertEqual((rc, out["findings"]), (0, []))

    def test_a_delegate_never_confirms_a_rule(self):
        """#100: "<owner> (delegate: <model>, <date>)" names a person who
        said nothing; the delegate did. A relay marker or a model family's
        name anywhere in confirmed_by is read as unconfirmed, as a model's
        own name is, so a gating case on the rule is refused."""
        for by in RELAYED_SIGNERS:
            with self.subTest(by=by):
                rc, out = self.lint([good_case(covers=["refund-cap"])],
                                    [a_rule(confirmed_by=by)])
                self.assertIn("rule_confirmed_unattributed", self.codes(out))
                self.assertIn("unconfirmed_rule_gates", self.codes(out))
                self.assertEqual(rc, 1)
        for by in HUMAN_SIGNERS:
            with self.subTest(by=by):
                rc, out = self.lint([good_case(covers=["refund-cap"])],
                                    [a_rule(confirmed_by=by)])
                self.assertEqual((rc, out["findings"]), (0, []))

    def test_a_delegated_answered_by_is_documented_not_flagged(self):
        """profile-schema.md's confirmed_by_human.answered_by records that a
        delegate answered the interview's routine questions. It is a record,
        not a confirmation: the rules still need the owner's own name."""
        profile = self.write_json("profile.json", {
            "schema_version": 1, "business_rules": [a_rule()],
            "confirmed_by_human": {
                "domain_boundaries": ["cancel -> billing"],
                "answered_by": "billing-owner (delegate: Fable, 2026-11-22)"}})
        rc, out, _ = self.validate([good_case(covers=["refund-cap"])],
                                   "--profile", profile)
        self.assertEqual((rc, out["findings"]), (0, []))
        # answered_by never stands in for the rule's own confirmed_by.
        profile = self.write_json("profile.json", {
            "schema_version": 1,
            "business_rules": [a_rule(confirmed_by=None)],
            "confirmed_by_human": {"answered_by": "billing-owner"}})
        rc, out, _ = self.validate([good_case(covers=["refund-cap"])],
                                   "--profile", profile)
        self.assertIn("rule_confirmed_unattributed", self.codes(out))
        self.assertIn("unconfirmed_rule_gates", self.codes(out))
        self.assertEqual(rc, 1)

    def test_an_unconfirmed_rule_may_back_a_non_gating_case(self):
        rule = a_rule(status="inferred", confirmed_by=None)
        rc, out = self.lint([good_case(covers=["refund-cap"], gating=False)],
                            [rule])
        self.assertNotIn("unconfirmed_rule_gates", self.codes(out))
        # Listed: gate.py --min-pass-rate still counts a non-gating case.
        self.assertEqual(
            out["summary"]["traceability"]["unconfirmed_non_gating"],
            ["billing-happy-0000abcd"])
        # An absent `gating` gates (case-format.md): still refused.
        case = good_case(covers=["refund-cap"])
        del case["gating"]
        rc, out = self.lint([case], [rule])
        self.assertIn("unconfirmed_rule_gates", self.codes(out))
        self.assertEqual(
            out["summary"]["traceability"]["unconfirmed_non_gating"], [])

    def test_a_canary_may_not_cover_an_unconfirmed_rule(self):
        """A canary closes no gate, but its fail aborts the run (EXIT_CANARY),
        so an inferred rule in one would decide the run."""
        canary = good_case(covers=["refund-cap"], gating=False,
                           split=["full", "smoke", "canary"])
        rc, out = self.lint([canary], [a_rule(status="inferred",
                                              confirmed_by=None)])
        finding = next(f for f in out["findings"]
                       if f["code"] == "unconfirmed_rule_gates")
        self.assertIn("canary", finding["message"])
        self.assertEqual(rc, 1)
        rc, out = self.lint([canary], [a_rule()])
        self.assertNotIn("unconfirmed_rule_gates", self.codes(out))

    def test_a_case_on_a_contradicted_rule_warns(self):
        rule = a_rule(contradiction="prompts/billing.md:9 says 14 days")
        rc, out = self.lint([good_case(covers=["refund-cap"])], [rule])
        self.assertIn("covers_contradicted_rule", self.codes(out))
        self.assertEqual(rc, 0)
        rc, out = self.lint([good_case(covers=["refund-cap"])], [a_rule()])
        self.assertNotIn("covers_contradicted_rule", self.codes(out))

    def test_rules_in_the_retired_location_warn(self):
        profile = self.write_json("profile.json", {
            "schema_version": 1, "confirmed_by_human": {
                "business_rules": ["never promise a refund"]}})
        rc, out, _ = self.validate([good_case()], "--profile", profile)
        self.assertIn("legacy_rules_location", self.codes(out))
        self.assertEqual(rc, 0)

    def test_rules_that_are_not_a_list_warn(self):
        profile = self.write_json("profile.json", {
            "schema_version": 1, "business_rules": {"refund-cap": "x"},
            "risks": "none"})
        rc, out, _ = self.validate([good_case()], "--profile", profile)
        self.assertEqual(self.codes(out).count("rules_not_a_list"), 2)

    def test_a_non_string_id_says_so(self):
        rc, out = self.lint([good_case()], [a_rule(3)])
        finding = next(f for f in out["findings"]
                       if f["code"] == "rule_incomplete")
        self.assertIn("id must be a string", finding["message"])

    def test_the_combined_document_is_a_profile(self):
        """convert_suite.py's one output serves --cases, --capabilities,
        --profile and --rubrics alike: its profile keys sit under
        `profile`."""
        doc = self.write_json("converted.json", {
            "capability_matrix": ALL_LAYERS_ENABLED["capability_matrix"],
            "profile": {"schema_version": 1, "business_rules": [
                a_rule(status="inferred", confirmed_by=None)]},
            "cases": [good_case(covers=["refund-cap"])]})
        rc, out, err = run_validate("--cases", doc, "--capabilities", doc,
                                    "--profile", doc)
        self.assertNotIn("Traceback", err, err)
        self.assertIn("unconfirmed_rule_gates", self.codes(out))
        self.assertEqual(out["summary"]["traceability"]["unconfirmed"],
                         ["refund-cap"])

    def test_a_link_to_nothing_is_an_error(self):
        rc, out = self.lint([good_case(covers=["refund-capp"])], [a_rule()])
        self.assertIn("dangling_cover", self.codes(out))
        self.assertEqual(rc, 1)

    def test_covers_must_be_a_list_of_ids(self):
        for bad in ("refund-cap", [], [""], [3]):
            with self.subTest(covers=bad):
                self.assert_finds("bad_covers", [good_case(covers=bad)])

    def test_a_case_covering_nothing_warns_once_the_profile_lists_rules(self):
        rc, out = self.lint([good_case()], [a_rule()])
        self.assertIn("case_covers_nothing", self.codes(out))
        self.assertEqual(rc, 0)
        self.assertEqual(
            out["summary"]["traceability"]["cases_covering_nothing"],
            ["billing-happy-0000abcd"])
        # Nothing to link to: no warning with no fix.
        rc, out = self.lint([good_case()])
        self.assertEqual(out["findings"], [])

    def test_duplicate_ids_across_rules_and_risks(self):
        rc, out = self.lint([good_case(covers=["refund-cap"])], [a_rule()],
                            [{"id": "refund-cap", "risk": "x"}])
        self.assertIn("duplicate_rule_id", self.codes(out))
        self.assertEqual(rc, 1)

    def test_rule_shape_warnings(self):
        cases = [good_case(covers=["refund-cap"], gating=False)]
        for code, rule in (
                ("rule_incomplete", {"id": "refund-cap", "rule": "x"}),
                ("rule_bad_status", a_rule(status="maybe")),
                ("rule_confirmed_unattributed", a_rule(confirmed_by="")),
                ("rule_source_vague", a_rule(source="the system prompt")),
                ("rule_source_vague", a_rule(
                    source="the prompt, e.g. the billing part")),
                ("outside_quote_versioned", a_rule(
                    outside=True, source="https://wiki.example/billing"))):
            with self.subTest(code):
                rc, out = self.lint(cases, [rule])
                finding = next(f for f in out["findings"]
                               if f["code"] == code)
                self.assertEqual(finding["severity"], "WARN")
                self.assertEqual(rc, 0)
        missing = next(f for f in self.lint(cases, [
            {"id": "refund-cap", "rule": "x"}])[1]["findings"]
            if f["code"] == "rule_incomplete")
        for key in ("source", "date", "quote", "status"):
            self.assertIn(key, missing["message"])

    def test_an_outside_quote_lives_in_the_local_file_or_is_opted_in(self):
        for rule in (
                a_rule(outside=True, quote=None, quote_in="local",
                       source="https://wiki.example/billing §Refunds"),
                a_rule(outside=True, quote_versioned=True,
                       source="https://wiki.example/billing §Refunds")):
            with self.subTest(rule=rule):
                rc, out = self.lint([good_case(covers=["refund-cap"])],
                                    [rule], (), "--strict")
                self.assertEqual((rc, out["findings"]), (0, []))

    def test_a_url_source_is_outside_without_the_flag(self):
        """`outside: true` forgotten on a wiki URL still keeps its quote out
        of the versioned profile."""
        for source in ("https://wiki.example/billing §Refunds",
                       "http://intranet/policy",
                       "Confluence page https://wiki.example/x"):
            with self.subTest(source=source):
                rc, out = self.lint([good_case(covers=["refund-cap"])],
                                    [a_rule(source=source)])
                self.assertIn("outside_quote_versioned", self.codes(out))
                self.assertNotIn("rule_source_vague", self.codes(out))
                rc, out = self.lint(
                    [good_case(covers=["refund-cap"])],
                    [a_rule(source=source, quote=None, quote_in="local")],
                    (), "--strict")
                self.assertEqual((rc, out["findings"]), (0, []))

    def test_code_sources_name_the_fragment(self):
        for source in ("prompts/billing.md:4", "app/rules.py:12",
                       "GLOSSARY.md §Refund", "docs/adr/0003-refunds.md"):
            with self.subTest(source=source):
                rc, out = self.lint([good_case(covers=["refund-cap"])],
                                    [a_rule(source=source)])
                self.assertNotIn("rule_source_vague", self.codes(out))

    def test_a_contradiction_is_listed(self):
        rule = a_rule(status="inferred", confirmed_by=None,
                      contradiction="prompts/billing.md:9 says 14 days")
        rc, out = self.lint([good_case()], [rule])
        self.assertEqual(
            out["summary"]["traceability"]["contradictions"], ["refund-cap"])
        self.assertEqual(out["summary"]["traceability"]["gaps"],
                         ["rule refund-cap has no case"])

    def test_covers_without_a_profile_is_unchecked(self):
        f = self.assert_finds("covers_unchecked",
                              [good_case(covers=["refund-cap"])],
                              severity="WARN")
        self.assertIn("--profile", f["message"])
        rc, out, _ = self.validate([good_case(covers=["x"])], "--strict")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
