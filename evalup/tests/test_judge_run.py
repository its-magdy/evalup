"""The run-time judge pass, scripts/judge_run.py (docs/judge-run.md SS16).

Most tests build a small run directory by hand -- the files the runner
writes, with only the fields the pass reads -- so each one states the case it
is about. The wall (SS16 test 2) runs the real quickstart instead: what it
proves is that a real run's decision outputs do not move.
"""
import copy
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_example import ROOT, ExampleCase  # noqa: E402

SCRIPTS = ROOT / "scripts"
EXAMPLE = ROOT / "examples" / "quickstart"
RID = "grounded-answer"
CRITERIA = ("states_the_amount", "states_a_period", "period_is_asked_one",
            "no_unsupported_claim")
ANSWER = "You spent $412.50 on invoices in May 2026, across 4 invoices."
MODEL = "claude-opus-5-5"


def script(name, *argv):
    proc = subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, argv)],
                          capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        out = None
    return proc.returncode, out, proc


def rubric_entry(**front):
    """The quickstart's rubric as convert_suite.py writes it."""
    entry = copy.deepcopy(json.loads(
        (EXAMPLE / "converted.json").read_text(encoding="utf-8"))["rubrics"][0])
    entry["frontmatter"].update(front)
    return entry


class JudgeRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run_dir = self.tmp / "reports" / "regression-20261121T100000Z"
        self.cases = []
        self.manifest = {"run_id": self.run_dir.name, "mode": "regression",
                         "selecting_split": "full", "scoring_version": 1,
                         "harness_version": "0.1.0", "dataset_version": 1,
                         "app": {"model": {"executor": "gpt-4o"}},
                         "judge": {"model": "opus"}, "cases": self.cases}
        self.rubrics = [rubric_entry()]

    # -- building a run ---------------------------------------------------

    def case(self, cid, rubric="grounded-answer-v1", verdict="pass",
             layers=None, canary=False, holdout=False, answer=ANSWER,
             refusal=False):
        self.cases.append({"id": cid, "set": "holdout" if holdout else "full"})
        d = self.run_dir / "cases" / cid
        d.mkdir(parents=True)
        rows = layers or {"http": "pass", "answer": "pass"}
        (d / "verdict.json").write_text(json.dumps({
            "case_id": cid, "verdict": verdict, "canary": canary,
            "holdout": holdout, "gating": True,
            "layers": {k: {"layer": k, "verdict": v}
                       for k, v in rows.items()}}), encoding="utf-8")
        expect = {"answer": {"must_contain": ["412.50"]}}
        if rubric:
            expect["answer"]["rubric"] = rubric
        if refusal:
            expect["authz"] = {"expect_refusal": True}
        (d / "expect.json").write_text(json.dumps(expect), encoding="utf-8")
        if answer is not None:
            (d / "answer.txt").write_text(answer, encoding="utf-8")
        (d / "request.json").write_text(json.dumps({
            "body": {"message": "what did I spend on invoices in May 2026?"}},
            indent=2), encoding="utf-8")
        (d / "response.json").write_text(json.dumps({
            "body": {"data": {"value": 412.5, "invoices": 4}}}, indent=2),
            encoding="utf-8")
        return d

    def write_run(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "manifest.yaml").write_text(
            json.dumps(self.manifest, indent=2), encoding="utf-8")
        path = self.tmp / "rubrics.json"
        path.write_text(json.dumps({"rubrics": self.rubrics}),
                        encoding="utf-8")
        return path

    def plan(self, *flags, ok=True):
        rubrics = self.write_run()
        rc, out, proc = script("judge_run.py", "plan", self.run_dir,
                               "--rubrics", rubrics, *flags)
        if ok:
            self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        return out

    def plan_doc(self):
        return json.loads((self.run_dir / "judge" / "plan.json")
                          .read_text(encoding="utf-8"))

    # -- replies ------------------------------------------------------------

    def reply(self, key, verdicts=None, model=MODEL, evidence=None, **over):
        verdicts = verdicts or {}
        crits = []
        for cid in CRITERIA:
            v = verdicts.get(cid, "pass")
            ev = (evidence or {}).get(cid, [{"source": "answer.txt",
                                             "quote": "You spent $412.50"}])
            crits.append({"id": cid, "verdict": v, "reasoning": "r",
                          "evidence": [] if v == "unknown" else ev})
        obj = {"schema": "evalup/judge-reply/1", "model_id": model,
               "case_id": key, "rubric_id": RID, "rubric_version": 1,
               "criteria": crits}
        obj.update(over)
        return "I read the files.\n\n" + json.dumps(obj) + "\n"

    def record(self, key, text, ok=True):
        path = self.tmp / f"{key}.txt"
        path.write_text(text, encoding="utf-8")
        flag = (["--self-test", key.split("selftest-", 1)[1].rsplit("-", 1)[0]
                 + ":" + key.rsplit("-", 1)[1]]
                if key.startswith("selftest-") else ["--case", key])
        rc, out, proc = script("judge_run.py", "record", self.run_dir, *flag,
                               "--reply-file", path)
        if ok:
            self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        return rc, out

    def self_test(self, good=None, bad=None, model=MODEL):
        """A passing self-test unless told otherwise. The bad answer's
        quotes come from its own answer.txt."""
        self.record("selftest-grounded-answer-good", self.reply(
            "selftest-grounded-answer-good", good, model=model,
            evidence={c: [{"source": "answer.txt", "quote": "You spent"}]
                      for c in CRITERIA}))
        bad = bad if bad is not None else {
            "period_is_asked_one": "fail", "no_unsupported_claim": "fail"}
        return self.record("selftest-grounded-answer-bad", self.reply(
            "selftest-grounded-answer-bad", bad, model=model,
            evidence={c: [{"source": "answer.txt", "quote": "April 2026"}]
                      for c in CRITERIA}))

    def merge(self, *flags):
        rc, out, proc = script("judge_run.py", "merge", self.run_dir, *flags)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        return json.loads((self.run_dir / "judge" / "judged.json")
                          .read_text(encoding="utf-8"))

    def judged_case(self, judged, cid):
        return next(c for c in judged["cases"] if c["case_id"] == cid)

    def one_case(self, text, **case):
        self.case("c-1", **case)
        self.plan()
        self.self_test()
        self.record("c-1", text)
        return self.judged_case(self.merge(), "c-1")


class TestUnknownIsUnjudged(JudgeRunTest):
    """SS16 test 1: unknown, a refusal and bad JSON never become a verdict."""

    def test_all_unknown(self):
        case = self.one_case(self.reply("c-1", dict.fromkeys(CRITERIA,
                                                             "unknown")))
        self.assertEqual((case["state"], case["verdict"]), ("unjudged", None))

    def test_one_unknown_beside_passes(self):
        case = self.one_case(self.reply("c-1", {"states_a_period": "unknown"}))
        self.assertEqual(case["state"], "unjudged")
        self.assertIn("states_a_period", case["reason"])

    def test_a_refusal_has_no_structured_output(self):
        case = self.one_case("I can't help grade this.")
        self.assertEqual(case["state"], "unjudged")
        self.assertEqual(case["reason"], "no structured output")

    def test_non_strict_json_is_no_structured_output(self):
        text = self.reply("c-1").replace('"rubric_version": 1',
                                         '"rubric_version": NaN')
        case = self.one_case(text)
        self.assertEqual(case["state"], "unjudged")

    def test_a_checked_fail_beside_an_unknown_is_fail(self):
        case = self.one_case(self.reply("c-1", {
            "states_a_period": "unknown", "no_unsupported_claim": "fail"}))
        self.assertEqual((case["state"], case["verdict"]),
                         ("provisional", "fail"))

    def test_a_reply_that_does_not_match_the_assignment(self):
        good = json.loads(self.reply("c-1").split("\n\n", 1)[1])
        missing = dict(good, criteria=good["criteria"][:-1])
        extra = dict(good, criteria=[*good["criteria"],
                                     dict(good["criteria"][0], id="extra")])
        dup = dict(good, criteria=[*good["criteria"][:-1],
                                   good["criteria"][0]])
        for name, obj in (("missing", missing), ("extra", extra),
                          ("duplicate", dup),
                          ("schema", dict(good, schema="evalup/other/1")),
                          ("case_id", dict(good, case_id="c-2"))):
            with self.subTest(name):
                self.setUp()
                case = self.one_case(json.dumps(obj))
                self.assertEqual(case["state"], "unjudged")
                self.assertTrue(case["reason"].startswith(
                    "reply did not match the assignment"), case["reason"])


class TestMessageParsing(JudgeRunTest):
    """SS16 test 1a: a real message is prose, the object, then a trailer."""

    def test_prose_then_object_then_a_trailing_line(self):
        case = self.one_case(self.reply("c-1") + "\nThat is my verdict.\n")
        self.assertEqual((case["state"], case["verdict"]),
                         ("provisional", "pass"))

    def test_two_objects_records_the_last(self):
        first = self.reply("c-1", {"states_a_period": "fail"})
        case = self.one_case(first + "\nOn reflection:\n" + self.reply("c-1"))
        self.assertEqual(case["verdict"], "pass")

    def test_braces_in_prose_do_not_derail_the_scan(self):
        case = self.one_case("A set {a, b} and a { stray.\n"
                             + self.reply("c-1"))
        self.assertEqual(case["verdict"], "pass")


class TestEvidenceIsChecked(JudgeRunTest):
    """SS16 test 5."""

    def crit(self, case, cid):
        return next(c for c in case["criteria"] if c["id"] == cid)

    def test_a_quote_not_in_the_file(self):
        case = self.one_case(self.reply("c-1", evidence={
            "states_a_period": [{"source": "answer.txt",
                                 "quote": "in June 2026"}]}))
        self.assertEqual(case["state"], "unjudged")
        self.assertEqual(self.crit(case, "states_a_period")["verdict"],
                         "unknown")
        self.assertIn("quote not found",
                      self.crit(case, "states_a_period")["check_failed"])

    def test_a_quote_citing_the_reference(self):
        case = self.one_case(self.reply("c-1", evidence={
            "states_the_amount": [{"source": "expect.json",
                                   "quote": "412.50"}]}))
        self.assertIn("expect.json", self.crit(
            case, "states_the_amount")["check_failed"])
        self.assertEqual(case["state"], "unjudged")

    def test_a_pass_with_no_quote(self):
        case = self.one_case(self.reply("c-1", evidence={
            "states_a_period": []}))
        self.assertEqual(self.crit(case, "states_a_period")["verdict"],
                         "unknown")

    def test_a_source_outside_the_assignment(self):
        case = self.one_case(self.reply("c-1", evidence={
            "states_a_period": [{"source": "../../manifest.yaml",
                                 "quote": "regression"}]}))
        self.assertEqual(case["state"], "unjudged")

    def test_a_redacted_piece_matches_and_whitespace_collapses(self):
        case = self.one_case(self.reply("c-1", evidence={
            "states_a_period": [{"source": "answer.txt",
                                 "quote": "You spent [REDACTED amount] on "
                                          "invoices in\n  May 2026"}],
            "no_unsupported_claim": [{"source": "response.json",
                                      "quote": '"invoices": 4'}]}))
        self.assertEqual((case["state"], case["verdict"]),
                         ("provisional", "pass"))

    def test_a_quote_that_is_only_a_redaction_is_no_evidence(self):
        case = self.one_case(self.reply("c-1", evidence={
            "states_a_period": [{"source": "answer.txt",
                                 "quote": "[REDACTED token]"}]}))
        self.assertEqual(case["state"], "unjudged")


class TestSelfTest(JudgeRunTest):
    """SS16 test 4: the self-test blocks a judge that passes its bad answer."""

    def test_passing_the_bad_answer_blocks_every_case(self):
        self.case("c-1")
        self.case("c-2")
        self.plan()
        _, out = self.self_test(bad={})
        self.assertEqual(out["self_test"], "fail")
        self.assertIn("launch no case", out["next"])
        self.record("c-1", self.reply("c-1"))
        judged = self.merge()
        for cid in ("c-1", "c-2"):
            case = self.judged_case(judged, cid)
            self.assertEqual(case["state"], "unjudged")
            self.assertTrue(case["reason"].startswith("self-test failed: "),
                            case["reason"])
        self.assertEqual(judged["summary"]["provisional"], 0)
        self.assertEqual(judged["rubrics"][RID]["self_test"], "fail")

    def test_a_missing_self_test_reply_blocks_too(self):
        self.case("c-1")
        self.plan()
        self.record("c-1", self.reply("c-1"))
        case = self.judged_case(self.merge(), "c-1")
        self.assertEqual(case["state"], "unjudged")
        self.assertIn("self-test failed", case["reason"])

    def test_the_good_answer_must_pass_every_criterion(self):
        self.case("c-1")
        self.plan()
        _, out = self.self_test(good={"states_a_period": "unknown"})
        self.assertEqual(out["self_test"], "fail")
        self.assertIn("states_a_period graded unknown on the good answer",
                      out["self_test_reason"])

    def test_the_self_test_is_materialised_as_case_files(self):
        self.case("c-1")
        self.plan()
        d = self.run_dir / "judge" / "selftest" / RID / "bad"
        self.assertEqual(sorted(p.name for p in d.iterdir()),
                         ["answer.txt", "expect.json", "request.json",
                          "response.json"])
        self.assertIn("April 2026", (d / "answer.txt").read_text("utf-8"))
        # The bad answer's `fails` list is never in what the judge reads:
        # its prompt names files, not the rubric.
        prompt = self.plan_doc()["launches"][1]["prompt"]
        self.assertNotIn("fails", prompt)
        self.assertNotIn("rubrics/", prompt)


class TestModelId(JudgeRunTest):
    """SS16 test 6, and SS7's unresolved id."""

    def test_mixed_model_ids_unjudge_every_case(self):
        self.case("c-1")
        self.plan()
        self.self_test()
        self.record("c-1", self.reply("c-1", model="claude-sonnet-5-5"))
        judged = self.merge()
        self.assertEqual(self.judged_case(judged, "c-1")["reason"],
                         "judge model changed during the pass")
        self.assertIsNone(judged["judge"]["resolved_model_id"])

    def test_an_unknown_id_is_unresolved(self):
        self.case("c-1")
        self.plan()
        self.self_test(model="unknown")
        self.record("c-1", self.reply("c-1", model="unknown"))
        self.assertEqual(self.judged_case(self.merge(), "c-1")["reason"],
                         "judge model unresolved")

    def test_the_resolved_id_and_the_alias_are_recorded(self):
        self.one_case(self.reply("c-1"))
        judged = self.merge()
        self.assertEqual(judged["judge"], {"requested_model": "opus",
                                           "resolved_model_id": MODEL,
                                           "same_family": "no"})


class TestSelection(JudgeRunTest):
    """SS16 tests 8 and 9."""

    def test_default_selection_reads_the_layer_rows(self):
        self.case("c-ok")
        self.case("c-fail", verdict="fail", layers={"answer": "fail"})
        self.case("c-err", verdict="unscored",
                  layers={"answer": "fail", "execution": "error"})
        self.case("c-infra", verdict="infra_error",
                  layers={"http": "infra_error"})
        self.case("c-inc", verdict="unscored",
                  layers={"trajectory": "infra_incomplete"})
        self.case("c-skip", verdict="skipped", layers={}, answer=None)
        self.case("c-turn", layers={"t1.answer": "fail", "answer": "pass"})
        self.plan()
        rows = {c["case_id"]: c for c in self.plan_doc()["cases"]}
        self.assertTrue(rows["c-ok"]["selected"])
        for cid in ("c-fail", "c-err", "c-infra", "c-inc", "c-skip",
                    "c-turn"):
            self.assertFalse(rows[cid]["selected"], cid)
        self.assertIn("execution is error", rows["c-err"]["reason"])

    def test_include_failed_adds_fail_only(self):
        self.case("c-fail", verdict="fail", layers={"answer": "fail"})
        self.case("c-err", verdict="unscored", layers={"answer": "error"})
        self.case("c-hold", holdout=True)
        self.plan("--include-failed")
        rows = {c["case_id"]: c for c in self.plan_doc()["cases"]}
        self.assertTrue(rows["c-fail"]["selected"])
        self.assertFalse(rows["c-err"]["selected"])
        self.assertNotIn("c-hold", rows)

    def test_holdout_is_never_judged_and_never_named(self):
        self.case("c-hold", holdout=True)
        self.case("c-1")
        self.plan("--include-failed")
        self.self_test()
        self.record("c-1", self.reply("c-1"))
        judged = self.merge()
        text = (self.run_dir / "judge" / "judged.json").read_text("utf-8")
        plan = (self.run_dir / "judge" / "plan.json").read_text("utf-8")
        self.assertNotIn("c-hold", text)
        self.assertNotIn("c-hold", plan)
        self.assertEqual(judged["summary"]["holdout_not_judged"], 1)

    def test_a_holdout_run_selects_nothing(self):
        self.manifest.update(mode="holdout", selecting_split="holdout")
        self.case("c-1")
        out = self.plan()
        self.assertEqual(self.plan_doc()["launches"], [])
        self.assertEqual(self.plan_doc()["holdout_not_judged"], 1)
        self.assertIn("1 sealed holdout", out["summary"][0])

    def test_rubric_reasons(self):
        self.case("c-missing", rubric="no-such-rubric-v1")
        self.case("c-none", rubric=None)
        dag = rubric_entry(rubric_id="dag-one", form=None)
        dag["file"] = "rubrics/dag-one.md"
        del dag["frontmatter"]["form"]
        self.rubrics.append(dag)
        self.case("c-dag", rubric="dag-one-v1")
        self.plan()
        rows = {c["case_id"]: c for c in self.plan_doc()["cases"]}
        self.assertNotIn("c-none", rows)
        self.assertIn("not found", rows["c-missing"]["reason"])
        self.assertEqual(rows["c-dag"]["reason"],
                         "DAG rubrics are not judged at run time in v1")

    def test_a_rubric_without_a_self_test_is_not_judged(self):
        entry = rubric_entry()
        del entry["frontmatter"]["self_test"]
        self.rubrics = [entry]
        self.case("c-1")
        self.plan()
        row = self.plan_doc()["cases"][0]
        self.assertFalse(row["selected"])
        self.assertIn("self_test", row["reason"])

    def test_nothing_to_judge_exits_0(self):
        self.case("c-1", rubric=None)
        out = self.plan()
        self.assertEqual(out["summary"],
                         ["judge pass: no case in this run pins a rubric"])


class TestBudget(JudgeRunTest):
    """SS16 test 10: --max-calls counts every launch, in plan order."""

    def test_over_the_cap_in_plan_order(self):
        for n in range(1, 5):
            self.case(f"c-{n}")
        self.plan("--max-calls", "4")
        rows = {c["case_id"]: c for c in self.plan_doc()["cases"]}
        self.assertEqual([c for c in rows if rows[c]["selected"]],
                         ["c-1", "c-2"])
        for cid in ("c-3", "c-4"):
            self.assertEqual(rows[cid]["reason"],
                             "over the judge budget: --max-calls 4")
        self.assertEqual(len(self.plan_doc()["launches"]), 4)

    def test_a_cap_too_small_for_the_self_test_pays_for_nothing(self):
        self.case("c-1")
        self.plan("--max-calls", "2")
        self.assertEqual(self.plan_doc()["launches"], [])

    def test_retries_count_and_past_the_cap_nothing_records(self):
        self.case("c-1")
        self.case("c-2")
        self.plan("--max-calls", "4")
        self.self_test()
        self.record("c-1", "no json")            # a failed launch...
        self.record("c-1", self.reply("c-1"))    # ...retried: call 4
        rc, out = self.record("c-2", self.reply("c-2"), ok=False)
        self.assertEqual(rc, 2)
        self.assertIn("over the judge budget", out["error"])
        judged = self.merge()
        self.assertEqual(self.judged_case(judged, "c-1")["state"],
                         "provisional")
        self.assertEqual(self.judged_case(judged, "c-2")["reason"],
                         "over the judge budget: --max-calls 4")
        self.assertEqual(judged["cost"]["calls"], 4)

    def test_a_denied_launch_says_so(self):
        self.case("c-1")
        self.plan()
        judged = self.merge("--launch-denied")
        case = self.judged_case(judged, "c-1")
        self.assertEqual((case["state"], case["reason"]),
                         ("unjudged", "judge launch denied"))
        self.assertEqual(judged["summary"]["unjudged"], 1)


class TestStaleness(JudgeRunTest):
    """SS16 test 9a."""

    def test_a_reply_stamped_with_another_rubric_file(self):
        self.case("c-1")
        self.plan()
        self.self_test()
        self.record("c-1", self.reply("c-1"))
        path = self.run_dir / "judge" / "replies" / "c-1.json"
        rec = json.loads(path.read_text("utf-8"))
        rec["rubric_sha256"] = "0" * 64
        path.write_text(json.dumps(rec), encoding="utf-8")
        self.assertEqual(self.judged_case(self.merge(), "c-1")["reason"],
                         "reply judged a different rubric file")

    def test_a_stale_pin_is_provisional_on_the_files_version(self):
        self.rubrics = [rubric_entry(version=2)]
        self.case("c-1")
        self.plan()
        for key in ("selftest-grounded-answer-good",
                    "selftest-grounded-answer-bad"):
            verdicts = ({} if key.endswith("good") else
                        {"period_is_asked_one": "fail",
                         "no_unsupported_claim": "fail"})
            self.record(key, self.reply(
                key, verdicts, rubric_version=2,
                evidence={c: [{"source": "answer.txt",
                               "quote": "You spent"}] for c in CRITERIA}))
        self.record("c-1", self.reply("c-1", rubric_version=2))
        case = self.judged_case(self.merge(), "c-1")
        self.assertEqual(case["state"], "provisional")
        self.assertEqual((case["pinned_version"], case["graded_version"]),
                         (1, 2))

    def test_resume_keeps_the_plan_and_lists_what_has_no_reply(self):
        self.case("c-1")
        self.plan()
        self.self_test()
        rc, _, proc = script("judge_run.py", "plan", self.run_dir,
                             "--rubrics", self.tmp / "rubrics.json")
        self.assertEqual(rc, 2, "a second plan must not overwrite replies")
        out = self.plan("--resume")
        self.assertEqual([p["key"] for p in out["pending"]], ["c-1"])


class TestCanary(JudgeRunTest):
    """SS9: a judge canary judged `fail` voids its rubric's verdicts."""

    def test_a_failed_canary_voids_the_rubric(self):
        self.case("c-can", canary=True)
        self.case("c-1")
        self.plan()
        self.self_test()
        self.record("c-can", self.reply("c-can", {"states_a_period": "fail"}))
        self.record("c-1", self.reply("c-1"))
        judged = self.merge()
        for cid in ("c-can", "c-1"):
            case = self.judged_case(judged, cid)
            self.assertEqual(case["state"], "unjudged")
            self.assertEqual(case["reason"], "judge canary c-can failed")


class TestCalibrated(JudgeRunTest):
    """SS16 test 7: calibrated needs all five conditions of SS6."""

    def sidecar(self, **group):
        g = {"rubric_id": RID, "rubric_version": 1, "judge_model_id": MODEL,
             "calibrated": True, "tpr": 0.93, "tnr": 0.95, "kappa": 0.8,
             "labelled_pairs": 120}
        g.update(group)
        path = self.tmp / "calibration.json"
        path.write_text(json.dumps({"schema": "evalup/judge-calibration/2",
                                    "status": "calibrated",
                                    "rubrics_measured": [RID],
                                    "groups": [g]}), encoding="utf-8")
        return path

    def judged(self, sidecar, **case):
        self.case("c-1", **case)
        self.plan()
        self.self_test()
        self.record("c-1", self.reply("c-1"))
        return self.judged_case(self.merge("--calibration", sidecar), "c-1")

    def test_all_five_hold(self):
        case = self.judged(self.sidecar())
        self.assertEqual(case["state"], "calibrated")
        self.assertEqual(case["calibration"]["tpr"], 0.93)

    def test_1_a_v1_sidecar_backs_nothing(self):
        path = self.tmp / "calibration.json"
        path.write_text(json.dumps({"schema": "evalup/judge-calibration/1",
                                    "status": "calibrated",
                                    "rubrics_measured": [RID]}),
                        encoding="utf-8")
        case = self.judged(path)
        self.assertEqual(case["state"], "provisional")
        self.assertIn("judge-calibration/1", case["not_calibrated_because"])

    def test_1_no_sidecar(self):
        case = self.judged(self.tmp / "absent.json")
        self.assertEqual(case["state"], "provisional")
        self.assertIn("no calibration sidecar", case["not_calibrated_because"])

    def test_2_another_model_or_version(self):
        for group in ({"judge_model_id": "claude-sonnet-5-5"},
                      {"rubric_version": 2}, {"calibrated": False}):
            with self.subTest(group):
                self.setUp()
                case = self.judged(self.sidecar(**group))
                self.assertEqual(case["state"], "provisional")
                self.assertEqual(case["not_calibrated_because"],
                                 f"no calibration for {RID} v1 on {MODEL}")

    def test_3_a_stale_pin(self):
        case = self.judged(self.sidecar(), rubric="grounded-answer")
        self.assertEqual(case["state"], "provisional")
        self.assertIn("stale pin", case["not_calibrated_because"])

    def test_4_a_failed_self_test_is_never_calibrated(self):
        """Condition 4 can never be the one that fails alone: a rubric whose
        self-test failed leaves its cases `unjudged` before SS6 is asked."""
        self.case("c-1")
        self.plan()
        self.self_test(bad={})
        self.record("c-1", self.reply("c-1"))
        case = self.judged_case(self.merge("--calibration", self.sidecar()),
                                "c-1")
        self.assertEqual(case["state"], "unjudged")

    def test_5_same_family(self):
        self.manifest["app"] = {"model": {"executor": "Claude Sonnet 5.5"}}
        case = self.judged(self.sidecar())
        self.assertEqual(case["state"], "provisional")
        self.assertIn("same_family_accepted", case["not_calibrated_because"])

    def test_5_same_family_accepted(self):
        self.manifest["app"] = {"model": "claude-sonnet-5-5"}
        self.manifest["judge"]["same_family_accepted"] = True
        self.assertEqual(self.judged(self.sidecar())["state"], "calibrated")
        notes = json.loads((self.run_dir / "judge" / "judged.json")
                           .read_text("utf-8"))["notes"]
        self.assertTrue(any("both Claude" in n for n in notes))

    def test_5_an_undeclared_app_model(self):
        self.manifest["app"] = {}
        case = self.judged(self.sidecar())
        self.assertEqual(case["not_calibrated_because"],
                         "app model undeclared")


class TestDiff(JudgeRunTest):
    """SS16 test 3: a model-id or rubric change refuses the diff."""

    def judged_file(self):
        self.one_case(self.reply("c-1"))
        doc = json.loads((self.run_dir / "judge" / "judged.json")
                         .read_text("utf-8"))
        return doc

    def write(self, name, doc):
        path = self.tmp / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def test_refusals_name_the_field(self):
        base = self.judged_file()
        a = self.write("a.json", base)
        changes = {
            "resolved_model_id": lambda d: d["judge"].update(
                resolved_model_id="claude-sonnet-5-5"),
            f"rubrics.{RID}.version": lambda d: d["rubrics"][RID].update(
                version=2),
            f"rubrics.{RID}.sha256": lambda d: d["rubrics"][RID].update(
                sha256="f" * 64),
            "judge_version": lambda d: d.update(judge_version=2),
            "scoring_version": lambda d: d["run"].update(scoring_version=2),
            "dataset_version": lambda d: d["run"].update(dataset_version=2),
        }
        for field, change in changes.items():
            with self.subTest(field):
                other = copy.deepcopy(base)
                change(other)
                rc, out, _ = script("judge_run.py", "diff", a,
                                    self.write("b.json", other))
                self.assertEqual(rc, 2)
                self.assertTrue(out["error"].startswith(field), out["error"])

    def test_a_legal_diff_counts_and_never_keeps(self):
        base = self.judged_file()
        other = copy.deepcopy(base)
        other["cases"][0].update(verdict="fail")
        rc, out, proc = script("judge_run.py", "diff",
                               self.write("a.json", base),
                               self.write("b.json", other))
        self.assertEqual(rc, 0, proc.stdout)
        self.assertEqual(out["changed"][0]["case_id"], "c-1")
        self.assertNotIn("keep", json.dumps(out["changed"]))

    def test_refuses_a_file_that_is_not_judged(self):
        rc, out, _ = script("judge_run.py", "diff",
                            self.write("a.json", {"schema": "x"}),
                            self.write("b.json", {"schema": "x"}))
        self.assertEqual(rc, 2)


class TestEstimate(JudgeRunTest):
    """SS11: the estimate prints before the first call."""

    def test_the_estimate_counts_bytes_of_the_named_files(self):
        self.case("c-1")
        out = self.plan()
        est = self.plan_doc()["estimate"]
        self.assertEqual((est["calls"], est["self_test_calls"]), (3, 2))
        self.assertGreater(est["tokens_low"], 3 * 17_000)
        self.assertIn("calls: 1 cases + 2 self-test", out["summary"][1])
        self.assertIn("not yet measured", out["summary"][-1])

    def test_the_last_pass_measures_time(self):
        prev = self.tmp / "reports" / "regression-20261120T100000Z" / "judge"
        prev.mkdir(parents=True)
        (prev / "judged.json").write_text(json.dumps({
            "run_id": "regression-20261120T100000Z",
            "cost": {"calls": 4, "wall_s": 80.0, "tokens": None}}),
            encoding="utf-8")
        self.case("c-1")
        out = self.plan()
        self.assertIn("~20.0 s per call", out["summary"][-1])


class TestRubricLint(JudgeRunTest):
    """SS2's linter rules, through validate_cases.py --rubrics."""

    def lint(self, *entries, cases=()):
        rubrics = self.tmp / "r.json"
        rubrics.write_text(json.dumps({"rubrics": list(entries)}),
                           encoding="utf-8")
        suite = self.tmp / "suite.json"
        base = json.loads((EXAMPLE / "converted.json").read_text("utf-8"))
        suite.write_text(json.dumps([*base["cases"], *cases]),
                         encoding="utf-8")
        rc, out, proc = script("validate_cases.py", "--cases", suite,
                               "--no-capabilities", "--rubrics", rubrics)
        self.assertIsNotNone(out, proc.stdout + proc.stderr)
        return {f["code"] for f in out["findings"]
                if f["severity"] == "ERROR"}, out

    def test_the_quickstart_rubric_is_clean(self):
        errors, out = self.lint(rubric_entry())
        self.assertEqual(errors, set())
        self.assertFalse([f for f in out["findings"]
                          if f["code"].startswith("rubric")])

    def test_each_rule(self):
        def broken(change):
            entry = rubric_entry()
            change(entry["frontmatter"])
            return entry
        cases = {
            "rubric_self_test": lambda f: f.pop("self_test"),
            "rubric_criteria": lambda f: f.update(criteria=[]),
            "rubric_criterion": lambda f: f["criteria"].append(
                dict(f["criteria"][0])),
            "rubric_id": lambda f: f.update(rubric_id="Bad_Id"),
            "rubric_version": lambda f: f.update(version=0),
        }
        for code, change in cases.items():
            with self.subTest(code):
                errors, _ = self.lint(broken(change))
                self.assertIn(code, errors)
        bad_fails = broken(lambda f: f["self_test"]["bad"].update(
            fails=["not_a_criterion"]))
        self.assertIn("rubric_self_test", self.lint(bad_fails)[0])
        thirteen = broken(lambda f: f.update(criteria=[
            {"id": f"c{i}", "question": "q?"} for i in range(13)]))
        self.assertIn("rubric_criteria", self.lint(thirteen)[0])

    def test_a_case_pin_without_a_file_or_stale_warns(self):
        case = {"id": "c-1", "expect": {"answer": {"rubric": "nope-v1"}}}
        stale = {"id": "c-2", "expect": {"answer": {
            "rubric": "grounded-answer-v3"}}}
        _, out = self.lint(rubric_entry(), cases=[case, stale])
        codes = {f["code"] for f in out["findings"]}
        self.assertIn("rubric_missing", codes)
        self.assertIn("rubric_pin_stale", codes)


class TestTheWall(ExampleCase):
    """SS16 test 2: no judged verdict moves a decision. The same real run,
    with and without a judge/ holding a `fail` on a gating case -- once
    provisional, once calibrated, once as a failed judge canary: gate.py,
    --verify, stats.py and every runner file are byte-identical."""

    def decisions(self, out):
        files = {name: (out / name).read_bytes() for name in
                 ("results.json", "verdicts.jsonl",
                  "verdicts_for_stats.jsonl", "manifest.yaml")}
        files.update({p.relative_to(out).as_posix(): p.read_bytes()
                      for p in (out / "cases").rglob("verdict.json")})
        gate = subprocess.run([sys.executable, str(SCRIPTS / "gate.py"),
                               str(out)], capture_output=True, text=True)
        verify = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_cases.py"), "--verify",
             str(out)], capture_output=True, text=True)
        vfs = str(out / "verdicts_for_stats.jsonl")
        stats = subprocess.run([sys.executable, str(SCRIPTS / "stats.py"),
                                vfs, vfs], capture_output=True, text=True)
        return files, (gate.returncode, gate.stdout), \
            (verify.returncode, verify.stdout), (stats.returncode, stats.stdout)

    def test_a_judged_fail_moves_nothing(self):
        rc, _, proc = self.invoke(self.build_plan())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        out = self.out_dir()
        before = self.decisions(out)
        for state, canary in (("provisional", False), ("calibrated", False),
                              ("unjudged", True)):
            with self.subTest(state=state, canary=canary):
                jdir = out / "judge"
                jdir.mkdir(exist_ok=True)
                (jdir / "judged.json").write_text(json.dumps({
                    "schema": "evalup/judged/1", "cases": [{
                        "case_id": "c-4f2a91c7", "state": state,
                        "verdict": "fail" if state != "unjudged" else None,
                        "canary": canary,
                        "reason": "judge canary c-4f2a91c7 failed"
                        if canary else None}]}), encoding="utf-8")
                self.assertEqual(self.decisions(out), before)
                shutil.rmtree(jdir)


if __name__ == "__main__":
    unittest.main()
