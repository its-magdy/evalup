"""Business rules scored by id (#26): rule_report.py, the validator's rule
bindings, and a rubric criterion's `rule:`.

The quickstart tests run the real example, so what they prove is end to
end: a planted rule violation fails with the rule id and its source, and the
report moves no decision. The rest build a run directory by hand -- the files
the runner writes, with only the fields the report reads -- so each one
states the case it is about.
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
CONVERTED = EXAMPLE / "converted.json"
sys.path.insert(0, str(SCRIPTS))
from _rubric import criterion_rules, rubric_findings  # noqa: E402

PLAIN = "/(?m)^[ \\t]*([-*+\\u2022]|\\d+[.)]|#+)[ \\t]|\\*\\*/"


def script(name, *argv):
    proc = subprocess.run([sys.executable, str(SCRIPTS / name),
                           *map(str, argv)],
                          capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        out = None
    return proc.returncode, out, proc


def converted():
    return json.loads(CONVERTED.read_text(encoding="utf-8"))


def write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def rule(rid, status="confirmed", check=None, **extra):
    out = {"id": rid, "rule": f"the {rid} rule", "quote": f"{rid} quote",
           "source": "prompts/main.md:3", "date": "2026-11-21",
           "status": status,
           "confirmed_by": "app-owner" if status == "confirmed" else None}
    if check is not None:
        out["check"] = check
    out.update(extra)
    return out


# -- the real quickstart --------------------------------------------------

class QuickstartRules(ExampleCase):
    def report(self, *extra):
        rc, out, proc = script("rule_report.py", self.out_dir(), "--profile",
                               CONVERTED, *extra)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        doc = json.loads((self.out_dir() / "rules.json")
                         .read_text(encoding="utf-8"))
        return out, {r["id"]: r for r in doc["rules"]}, doc

    def run_example(self):
        rc, _, proc = self.invoke(self.build_plan())
        self.assertIn(rc, (0, 1), proc.stdout + proc.stderr)

    def judge(self):
        """The quickstart's fixture replies through plan, record, merge."""
        out = self.out_dir()
        rc, planned, proc = script("judge_run.py", "plan", out, "--rubrics",
                                   CONVERTED)
        self.assertEqual(rc, 0, proc.stdout)
        replies = EXAMPLE / "judge-replies"
        for launch in planned["pending"]:
            name = (f"selftest-{launch['rubric_id']}-{launch['side']}"
                    if launch["kind"] == "self_test" else launch["key"])
            text = (replies / (name + ".txt")).read_text("utf-8").replace(
                f'"case_id": "{name}"', f'"case_id": "{launch["key"]}"')
            path = out / "judge" / "inbox" / (launch["key"] + ".txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            rc, _, proc = script("judge_run.py", *launch["record_args"])
            self.assertEqual(rc, 0, proc.stdout)
        rc, _, proc = script("judge_run.py", "merge", out)
        self.assertEqual(rc, 0, proc.stdout)

    def decisions(self):
        """Every file and output a decision is made on (judge-run.md SS8's
        list), so a test can say the report moved none of them."""
        out = self.out_dir()
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
            (verify.returncode, verify.stdout), \
            (stats.returncode, stats.stdout)


class TestPlantedViolation(QuickstartRules):
    def test_a_bulleted_answer_fails_plain_text_by_id_with_its_source(self):
        """#26's acceptance check: a planted violation of the prompt's
        plain-text rule fails with the rule id and its source in the
        report, on every case that lists the rule, and nothing else fails
        with it."""
        self.app.server.render = lambda text: "Here you go:\n- " + text
        self.run_example()
        out, rules, _ = self.report()
        plain = rules["plain-text"]
        self.assertEqual(plain["verdict"], "fail")
        self.assertEqual(plain["status"], "confirmed")
        self.assertEqual(plain["source"], "tests/test_example.py:82")
        self.assertEqual(plain["scripted"]["fail"], 6)
        self.assertEqual(len(plain["gating_failures"]), 6)
        for entry in plain["scripted"]["cases"]:
            self.assertEqual(entry["failed"][0]["entry"], PLAIN)
            self.assertTrue(entry["closes_gate"])
        self.assertEqual(rules["read-only"]["verdict"], "pass")
        line = next(x for x in out["summary"] if "plain-text" in x)
        self.assertIn("FAIL", line)
        self.assertIn("source: tests/test_example.py:82", line)
        self.assertIn("1 failing", out["summary"][0])

    def test_the_clean_run_reports_each_rule_and_its_check(self):
        self.run_example()
        _, rules, doc = self.report()
        self.assertEqual(rules["plain-text"]["verdict"], "pass")
        self.assertEqual(rules["plain-text"]["scripted"]["pass"], 6)
        self.assertEqual(rules["read-only"]["scripted"]["pass"], 1)
        self.assertEqual(rules["dispute-window"]["verdict"], "not checked")
        self.assertEqual(rules["dispute-window"]["status"], "inferred")
        # grounded-only has no script: only the judge checks it, and no
        # judge pass ran.
        grounded = rules["grounded-only"]
        self.assertEqual(grounded["verdict"], "judged")
        self.assertIsNone(grounded["judged_verdict"])
        self.assertEqual(grounded["judged"]["unjudged"], 1)
        self.assertEqual(grounded["judged"]["cases"][0]["reason"],
                         "no judge pass ran")
        self.assertEqual(doc["summary"]["listed"], 3)

    def test_an_inferred_rule_is_reported_and_never_gated(self):
        """With the rule left inferred, the report says it gates nothing;
        and a gating case failing on it (a suite validate_cases.py
        refuses) is flagged, never silently counted."""
        self.app.server.render = lambda text: "- " + text
        self.run_example()
        doc = converted()
        for r in doc["profile"]["business_rules"]:
            if r["id"] == "plain-text":
                r["status"], r["confirmed_by"] = "inferred", None
        profile = self.tmp / "profile.json"
        write(profile, doc["profile"])
        rc, out, proc = script("rule_report.py", self.out_dir(), "--profile",
                               profile)
        self.assertEqual(rc, 0, proc.stdout)
        text = "\n".join(out["summary"])
        self.assertIn("inferred: reported only", text)
        self.assertIn("unconfirmed_rule_gates", text)


class TestJudgedRules(QuickstartRules):
    def test_the_judge_pass_reports_grounded_only_provisionally(self):
        self.run_example()
        self.judge()
        judged = json.loads((self.out_dir() / "judge" / "judged.json")
                            .read_text("utf-8"))
        self.assertEqual(judged["rubrics"]["grounded-answer"]
                         ["criterion_rules"],
                         {"no_unsupported_claim": "grounded-only"})
        out, rules, doc = self.report()
        grounded = rules["grounded-only"]
        self.assertEqual(grounded["judged_verdict"],
                         {"state": "provisional", "verdict": "pass"})
        self.assertEqual(grounded["judged"]["provisional"]["pass"], 1)
        entry = grounded["judged"]["cases"][0]
        self.assertEqual(entry["criterion"], "no_unsupported_claim")
        self.assertTrue(entry["quotes"])
        self.assertIsNotNone(doc["judged"]["sha256"])
        line = next(x for x in out["summary"] if "grounded-only" in x)
        self.assertIn("provisional pass", line)
        self.assertTrue(any("never gates" in x for x in out["summary"]))

    def test_a_judged_rule_fail_is_provisional_and_moves_no_decision(self):
        """A provisional, then a calibrated, `fail` on the grounded-only
        criterion: the rule reports it under judged_verdict, its scripted
        `verdict` stays apart, and gate.py, --verify, stats.py and every
        runner file are byte-identical with rules.json written."""
        self.run_example()
        self.judge()
        before = self.decisions()
        path = self.out_dir() / "judge" / "judged.json"
        judged = json.loads(path.read_text("utf-8"))
        for state in ("provisional", "calibrated"):
            with self.subTest(state=state):
                doc = copy.deepcopy(judged)
                case = doc["cases"][0]
                case["state"], case["verdict"] = state, "fail"
                for crit in case["criteria"]:
                    if crit["id"] == "no_unsupported_claim":
                        crit["verdict"] = "fail"
                path.write_text(json.dumps(doc), encoding="utf-8")
                _, rules, _ = self.report()
                grounded = rules["grounded-only"]
                self.assertEqual(grounded["judged_verdict"],
                                 {"state": state, "verdict": "fail"})
                self.assertEqual(grounded["verdict"], "judged")
                self.assertEqual(grounded["gating_failures"], [])
                self.assertEqual(self.decisions(), before)

    def test_the_other_criteria_do_not_decide_the_rule(self):
        """The case's judged verdict folds every criterion; the rule's is
        its own criterion's. A fail on another criterion leaves the rule's
        pass standing."""
        self.run_example()
        self.judge()
        path = self.out_dir() / "judge" / "judged.json"
        doc = json.loads(path.read_text("utf-8"))
        case = doc["cases"][0]
        case["verdict"] = "fail"
        for crit in case["criteria"]:
            if crit["id"] == "states_a_period":
                crit["verdict"] = "fail"
        path.write_text(json.dumps(doc), encoding="utf-8")
        _, rules, _ = self.report()
        self.assertEqual(rules["grounded-only"]["judged_verdict"],
                         {"state": "provisional", "verdict": "pass"})

    def test_an_unknown_criterion_is_unjudged_with_its_reason(self):
        self.run_example()
        self.judge()
        path = self.out_dir() / "judge" / "judged.json"
        doc = json.loads(path.read_text("utf-8"))
        for crit in doc["cases"][0]["criteria"]:
            if crit["id"] == "no_unsupported_claim":
                crit["verdict"], crit["reason"] = "unknown", "quote_not_found"
        path.write_text(json.dumps(doc), encoding="utf-8")
        _, rules, _ = self.report()
        entry = rules["grounded-only"]["judged"]["cases"][0]
        self.assertEqual(entry["state"], "unjudged")
        self.assertIn("quote_not_found", entry["reason"])
        # An unjudged case passes its reason through.
        doc["cases"][0].update(state="unjudged", verdict=None,
                               reason="self-test failed: x")
        path.write_text(json.dumps(doc), encoding="utf-8")
        _, rules, _ = self.report()
        self.assertEqual(rules["grounded-only"]["judged"]["cases"][0]
                         ["reason"], "self-test failed: x")

    def test_a_judged_json_of_another_run_is_refused(self):
        self.run_example()
        self.judge()
        path = self.out_dir() / "judge" / "judged.json"
        doc = json.loads(path.read_text("utf-8"))
        doc["run_id"] = "smoke-20200101T000000Z"
        path.write_text(json.dumps(doc), encoding="utf-8")
        rc, out, _ = script("rule_report.py", self.out_dir(), "--profile",
                            CONVERTED)
        self.assertEqual(rc, 2)
        self.assertIn("is for run", out["error"])
        doc["run_id"], doc["schema"] = self.out_dir().name, "evalup/x/1"
        path.write_text(json.dumps(doc), encoding="utf-8")
        rc, out, _ = script("rule_report.py", self.out_dir(), "--profile",
                            CONVERTED)
        self.assertEqual(rc, 2)
        self.assertIn("schema", out["error"])


# -- hand-built runs ------------------------------------------------------

class HandRun(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run_dir = self.tmp / "reports" / "regression-20261122T100000Z"
        write(self.run_dir / "results.json",
              {"run_id": self.run_dir.name, "scoring_version": 1})
        self.profile = self.tmp / "profile.json"
        write(self.profile, {"business_rules": [
            rule("plain", check={"must_not_contain": [PLAIN]}),
            rule("polite", check={"must_contain": ["please"]})]})

    @staticmethod
    def answer(*statuses, entries=(PLAIN,), verdict=None):
        checks = [{"check": "must_not_contain", "entry": e, "status": s}
                  for e, s in zip(entries, statuses)]
        if verdict is None:
            verdict = "fail" if "fail" in statuses else "pass"
        return {"layer": "answer", "verdict": verdict, "checks": checks}

    def case(self, case_id, layers, expect, **verdict):
        cdir = self.run_dir / "cases" / case_id
        write(cdir / "expect.json", expect)
        doc = {"case_id": case_id, "gating": True, "canary": False,
               "holdout": False, "verdict": "pass", "layers": layers,
               "multi_turn": False, "repeats": None}
        doc.update(verdict)
        write(cdir / "verdict.json", doc)
        return cdir

    def report(self):
        rc, out, proc = script("rule_report.py", self.run_dir, "--profile",
                               self.profile)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        doc = json.loads((self.run_dir / "rules.json").read_text("utf-8"))
        return {r["id"]: r for r in doc["rules"]}, doc, out


EXPECT_PLAIN = {"answer": {"must_not_contain": [PLAIN], "rules": ["plain"]}}


class TestRepeatsTurnsHoldout(HandRun):
    def test_repeats_count_the_breaks(self):
        """The #27 evidence: the rule broken in 2 of 3 repeats."""
        reps = [{"n": n, "verdict": v, "layers": {"answer": self.answer(v)}}
                for n, v in ((1, "fail"), (2, "pass"), (3, "fail"))]
        self.case("c-1", {"answer": self.answer("fail")}, EXPECT_PLAIN,
                  repeats=reps, verdict="fail")
        rules, _, out = self.report()
        entry = rules["plain"]["scripted"]["cases"][0]
        self.assertEqual(entry["verdict"], "fail")
        self.assertEqual(entry["failed_repeats"], "2/3")
        self.assertIn("failed 2/3 repeats", "\n".join(out["summary"]))

    def test_a_conversation_is_read_turn_by_turn(self):
        cdir = self.case("c-2", {"t1.answer": self.answer("pass"),
                                 "t2.answer": self.answer("fail")},
                         {}, multi_turn=True)
        write(cdir / "turns" / "1" / "expect.json", EXPECT_PLAIN)
        write(cdir / "turns" / "2" / "expect.json", EXPECT_PLAIN)
        rules, _, _ = self.report()
        got = [(c["turn"], c["verdict"])
               for c in rules["plain"]["scripted"]["cases"]]
        self.assertEqual(got, [(1, "pass"), (2, "fail")])

    def test_holdout_cases_are_counts_only(self):
        self.case("c-sealed", {"answer": self.answer("fail")}, EXPECT_PLAIN,
                  holdout=True)
        rules, doc, out = self.report()
        self.assertEqual(rules["plain"]["holdout"], {"checked": 1, "fail": 1})
        self.assertEqual(rules["plain"]["scripted"]["cases"], [])
        self.assertEqual(rules["plain"]["verdict"], "fail")
        self.assertNotIn("c-sealed", json.dumps(doc))
        self.assertNotIn("c-sealed", json.dumps(out))

    def test_a_non_gating_failure_is_said_not_to_close_the_gate(self):
        self.case("c-3", {"answer": self.answer("fail")}, EXPECT_PLAIN,
                  gating=False)
        rules, _, out = self.report()
        self.assertEqual(rules["plain"]["gating_failures"], [])
        self.assertIn("(not gating)", "\n".join(out["summary"]))


class TestUnscored(HandRun):
    def test_no_verdict_is_never_a_pass_or_a_fail(self):
        polite = {"answer": {"must_contain": ["please"], "rules": ["polite"]}}
        self.case("c-a", {"answer": {"layer": "answer",
                                     "verdict": "unscorable"}}, EXPECT_PLAIN)
        # The entry is not in the layer's checks: the case's expect moved.
        self.case("c-b", {"answer": self.answer("pass", entries=("x",))},
                  polite)
        self.case("c-c", {"answer": self.answer(
            "unscorable", entries=(PLAIN,), verdict="pass")}, EXPECT_PLAIN)
        rules, _, _ = self.report()
        self.assertEqual(rules["plain"]["scripted"]["unscored"], 2)
        self.assertEqual(rules["plain"]["verdict"], "unscored")
        self.assertEqual(rules["polite"]["scripted"]["unscored"], 1)
        reasons = [c["reason"] for c in rules["plain"]["scripted"]["cases"]]
        self.assertIn("answer layer unscorable", reasons)

    def test_a_rule_no_case_lists_is_not_checked(self):
        self.case("c-e", {"answer": self.answer("pass")},
                  {"answer": {"must_not_contain": [PLAIN]}})
        rules, doc, _ = self.report()
        self.assertEqual({r["verdict"] for r in rules.values()},
                         {"not checked"})
        self.assertEqual(doc["summary"]["listed"], 0)

    def test_a_listed_rule_the_profile_lacks_is_named(self):
        self.case("c-d", {"answer": self.answer("pass")},
                  {"answer": {"must_not_contain": [PLAIN],
                              "rules": ["plain", "ghost"]}})
        _, doc, out = self.report()
        self.assertEqual(doc["unknown_rules"], ["ghost"])
        self.assertIn("ghost", out["summary"][-1])

    def test_malformed_json_exits_2_without_a_traceback(self):
        self.case("c-f", {"answer": self.answer("pass")}, EXPECT_PLAIN)
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        for argv in (["--profile", bad], ["--profile", self.profile,
                                          "--judged", bad]):
            with self.subTest(argv=argv[-1]):
                rc, out, proc = script("rule_report.py", self.run_dir, *argv)
                self.assertEqual(rc, 2, proc.stdout)
                self.assertIn("error", out)
                self.assertNotIn("Traceback", proc.stderr)
        (self.run_dir / "cases" / "c-f" / "verdict.json").write_text(
            "{not json", encoding="utf-8")
        rc, out, proc = script("rule_report.py", self.run_dir, "--profile",
                               self.profile)
        self.assertEqual(rc, 2, proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_bad_input_exits_2(self):
        rc, out, _ = script("rule_report.py", self.tmp / "nope", "--profile",
                            self.profile)
        self.assertEqual((rc, "error" in out), (2, True))
        rc, out, _ = script("rule_report.py", self.run_dir, "--profile",
                            self.profile)
        self.assertEqual(rc, 2)
        self.assertIn("cases/", out["error"])


# -- the validator --------------------------------------------------------

class TestValidatorBindings(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.doc = converted()

    def lint(self, rubrics=True):
        path = self.tmp / "c.json"
        write(path, self.doc)
        argv = ["--cases", path, "--capabilities", path, "--profile", path]
        if rubrics:
            argv += ["--rubrics", path]
        rc, out, proc = script("validate_cases.py", *argv)
        self.assertIsNotNone(out, proc.stdout + proc.stderr)
        return {f["code"] for f in out["findings"]}, out

    def case(self, case_id):
        return next(c for c in self.doc["cases"] if c["id"] == case_id)

    def profile_rule(self, rid):
        return next(r for r in self.doc["profile"]["business_rules"]
                    if r["id"] == rid)

    def test_the_quickstart_binds_every_rule_it_lists(self):
        codes, out = self.lint()
        self.assertEqual(codes, set())

    def test_rules_must_be_a_list_of_ids(self):
        for bad in ("plain-text", [], [3]):
            with self.subTest(bad=bad):
                self.case("c-8b13d0e5")["expect"]["answer"]["rules"] = bad
                codes, _ = self.lint()
                self.assertIn("bad_answer_rules", codes)

    def test_a_case_missing_the_rules_check_is_an_error(self):
        self.case("c-8b13d0e5")["expect"]["answer"]["must_not_contain"] = []
        codes, out = self.lint()
        self.assertIn("rule_check_missing", codes)
        self.assertGreater(out["error_count"], 0)

    def test_a_rule_with_no_check_and_no_criterion_is_unbound(self):
        self.case("c-8b13d0e5")["expect"]["answer"]["rules"] = [
            "plain-text", "grounded-only"]
        codes, _ = self.lint()
        self.assertIn("rule_unbound", codes)
        # The same on a case whose rubric has no criterion for it.
        self.case("c-8b13d0e5")["expect"]["answer"]["rules"] = ["plain-text"]
        c = self.case("c-4f2a91c7")
        c["expect"]["answer"]["rules"] = ["plain-text", "grounded-only"]
        for crit in self.doc["rubrics"][0]["frontmatter"]["criteria"]:
            crit.pop("rule", None)
        codes, _ = self.lint()
        self.assertIn("rule_unbound", codes)

    def test_without_rubrics_a_judged_binding_is_unchecked(self):
        codes, _ = self.lint(rubrics=False)
        self.assertEqual(codes, {"rule_binding_unchecked"})

    def test_listing_an_inferred_rule_on_a_gating_case_is_an_error(self):
        """expect.answer.rules counts as covers: the #25 gating check
        reads it, so listing a rule cannot route around it."""
        r = self.profile_rule("plain-text")
        r["status"], r["confirmed_by"] = "inferred", None
        for c in self.doc["cases"]:
            c["covers"] = [x for x in c["covers"] if x != "plain-text"] \
                or ["risk-oos-answered"]
        codes, _ = self.lint()
        self.assertIn("unconfirmed_rule_gates", codes)

    def test_a_listed_id_the_profile_lacks_dangles(self):
        self.case("c-8b13d0e5")["expect"]["answer"]["rules"] = ["nope"]
        codes, _ = self.lint()
        self.assertIn("dangling_cover", codes)

    def test_an_off_shape_check_is_an_error(self):
        for bad in ({"must_contain": "x"}, {"regex": ["x"]}, {},
                    {"must_contain": []}):
            with self.subTest(bad=bad):
                self.profile_rule("read-only")["check"] = bad
                codes, _ = self.lint()
                self.assertIn("bad_rule_check", codes)

    def test_a_judged_rule_the_case_does_not_list_warns(self):
        self.case("c-4f2a91c7")["expect"]["answer"]["rules"] = ["plain-text"]
        codes, _ = self.lint()
        self.assertIn("rule_judged_unlisted", codes)

    def test_a_criterion_naming_no_rule_warns(self):
        crits = self.doc["rubrics"][0]["frontmatter"]["criteria"]
        crits[0]["rule"] = "no-such-rule"
        codes, _ = self.lint()
        self.assertIn("criterion_rule_unknown", codes)

    def test_rules_without_a_profile_warn(self):
        path = self.tmp / "c.json"
        write(path, self.doc)
        rc, out, _ = script("validate_cases.py", "--cases", path,
                            "--capabilities", path)
        self.assertIn("rules_unchecked",
                      {f["code"] for f in out["findings"]})


class TestCriterionRule(unittest.TestCase):
    def front(self):
        return copy.deepcopy(converted()["rubrics"][0]["frontmatter"])

    def test_the_quickstart_criterion_names_its_rule(self):
        self.assertEqual(criterion_rules(self.front()),
                         {"no_unsupported_claim": "grounded-only"})
        self.assertEqual([f for f in rubric_findings(self.front())
                          if f[0] == "ERROR"], [])

    def test_a_rule_is_one_id_and_one_criterion_per_rule(self):
        for bad in (3, "", "two ids"):
            with self.subTest(bad=bad):
                front = self.front()
                front["criteria"][0]["rule"] = bad
                self.assertIn("rubric_criterion",
                              {f[1] for f in rubric_findings(front)
                               if f[0] == "ERROR"})
        front = self.front()
        front["criteria"][0]["rule"] = "grounded-only"
        messages = [f[2] for f in rubric_findings(front) if f[0] == "ERROR"]
        self.assertTrue(any("one criterion per rule" in m for m in messages))


class TestTheJudgeNeverSeesTheRule(QuickstartRules):
    def test_tagging_a_criterion_leaves_the_prompt_unchanged(self):
        """The tag is for the report: the prompt a judge reads is the same
        with and without it, so tagging a criterion changes no grading."""
        self.run_example()
        prompts = []
        for tagged in (True, False):
            doc = converted()
            if not tagged:
                for crit in doc["rubrics"][0]["frontmatter"]["criteria"]:
                    crit.pop("rule", None)
            rubrics = self.tmp / f"rubrics-{tagged}.json"
            write(rubrics, {"rubrics": doc["rubrics"]})
            rc, out, proc = script("judge_run.py", "plan", self.out_dir(),
                                   "--rubrics", rubrics, "--restart")
            self.assertEqual(rc, 0, proc.stdout)
            # Self-test keys carry a random label (judge-run.md SS19), so
            # only the case launches compare byte for byte.
            prompts.append([p["prompt"] for p in out["pending"]
                            if p["kind"] == "case"])
        self.assertTrue(prompts[0])
        self.assertEqual(prompts[0], prompts[1])
        self.assertNotIn("grounded-only", "".join(prompts[0]))


if __name__ == "__main__":
    unittest.main()
