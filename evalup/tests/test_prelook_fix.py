"""prelook_fix.py (#99): a recorded fix to a sealed holdout before its first
look, and the seal's templates listed by id and hash only."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from run_cases import sha256_of  # noqa: E402


def have_yaml():
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


# Text a sealed case holds that must never reach the script's output.
SEALED_INPUT = "SEALED-QUESTION-about-the-quarterly-roster"
SEALED_ORACLE = "SEALED-ORACLE-FACT-42"
OLD_PATTERN = "/\\d{4}-\\d{2}-\\d{2}/"
NEW_PATTERN = "/\\d{4}-\\d{2}-\\d{2}( ?[-–] ?\\d{4}-\\d{2}-\\d{2})?/"


def case_yaml(case_id, split, must_contain, tools, review="accepted",
              message="How many shifts this week?", template="t_shifts"):
    return f"""# a comment the rewrite drops
id: {case_id}
schema_version: 1
split: [{", ".join(split)}]
test_type: MFT
template_id: {template}
instantiation_params: {{week: 1}}
review: {{status: {review}, by: owner, at: 2026-11-20}}
input:
  message: "{message}"
expect:
  answer:
    must_contain: {json.dumps(must_contain)}
  tools:
    subset: {json.dumps(tools)}
gating: true
notes: ""
"""


@unittest.skipUnless(have_yaml(), "PyYAML not installed; prelook_fix.py "
                     "reads the suite through convert_suite.py")
class TestPrelookFix(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = self.tmp / ".evalup"
        d = self.state / "datasets"
        (d / "templates").mkdir(parents=True)
        (self.state / "adapter.yaml").write_text("schema_version: 1\n",
                                                 encoding="utf-8")
        (self.state / "profile.yaml").write_text(
            "schema_version: 1\ncapability_matrix: {}\n", encoding="utf-8")
        # c-train already carries the fix (its review fixed it by hand).
        (d / "c-train.yaml").write_text(case_yaml(
            "c-train", ["full", "smoke"], [NEW_PATTERN], ["get_shifts"]),
            encoding="utf-8")
        (d / "c-train2.yaml").write_text(case_yaml(
            "c-train2", ["full"], [OLD_PATTERN], ["get_shifts", "search"]),
            encoding="utf-8")
        (d / "c-pending.yaml").write_text(case_yaml(
            "c-pending", ["full"], [NEW_PATTERN], ["get_shifts"],
            review="pending"), encoding="utf-8")
        (d / "c-ho1.yaml").write_text(case_yaml(
            "c-ho1", ["holdout"], [OLD_PATTERN], ["get_shifts", "search"],
            review="pending", message=SEALED_INPUT, template="ho_shifts"),
            encoding="utf-8")
        (d / "c-ho2.yaml").write_text(case_yaml(
            "c-ho2", ["holdout"], ["unrelated"], ["get_shifts"],
            review="pending", message=SEALED_INPUT, template="ho_shifts"),
            encoding="utf-8")
        (d / "templates" / "ho_shifts.yaml").write_text(
            f"oracle: {{fact: {SEALED_ORACLE}}}\ntuples: [{{week: 1}}]\n",
            encoding="utf-8")
        (d / "templates" / "t_shifts.yaml").write_text(
            "oracle: {fact: open}\ntuples: [{week: 1}]\n", encoding="utf-8")
        (d / "dataset.yaml").write_text(
            "schema_version: 1\ndataset_version: 3   # bumped on review\n"
            "cases: 5\n", encoding="utf-8")
        self.fix = {"statement": "the date check rejects a correct range "
                                 "answer; accept an optional second date",
                    "op": "replace_item", "path": "expect.answer.must_contain",
                    "from": OLD_PATTERN, "to": NEW_PATTERN}

    def run_fix(self, *argv, fix=None):
        env = dict(os.environ, EVALUP_NO_UV="1")
        args = [sys.executable, str(SCRIPTS / "prelook_fix.py"),
                str(self.state), *argv]
        if fix is not False:
            args += ["--transformation", json.dumps(fix or self.fix),
                     "--motivated-by", "c-train"]
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=120, env=env)
        return proc.returncode, proc.stdout, proc.stderr

    def converted(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "convert_suite.py"),
             str(self.state)], capture_output=True, text=True, timeout=60,
            check=True)
        doc = json.loads(proc.stdout)
        return {c["id"]: c for c in doc["cases"]}, doc["manifest"]

    def snapshot(self):
        return {p: p.read_bytes() for p in self.state.rglob("*")
                if p.is_file()}

    def assert_no_case_text(self, out):
        for sealed_text in (SEALED_INPUT, SEALED_ORACLE, "quarterly"):
            self.assertNotIn(sealed_text, out)

    def test_apply_fixes_every_split_and_records_hashes(self):
        before, _ = self.converted()
        rc, out, err = self.run_fix("--apply", "--approved-by", "the owner")
        self.assertEqual(rc, 0, out + err)
        self.assert_no_case_text(out + err)
        doc = json.loads(out)
        self.assertTrue(doc["applied"])
        self.assertEqual([r["id"] for r in doc["changed"]],
                         ["c-ho1", "c-train2"])
        self.assertEqual(doc["sealed_changed"], 1)
        self.assertEqual(doc["dataset_version"], {"from": 3, "to": 4})
        after, manifest = self.converted()
        self.assertEqual(after["c-ho1"]["expect"]["answer"]["must_contain"],
                         [NEW_PATTERN])
        self.assertEqual(after["c-train2"]["expect"]["answer"]["must_contain"],
                         [NEW_PATTERN])
        for cid in ("c-train", "c-pending", "c-ho2"):
            self.assertEqual(sha256_of(after[cid]), sha256_of(before[cid]))
        # input and everything outside expect are untouched.
        for cid in ("c-ho1", "c-train2"):
            strip = {k: v for k, v in before[cid].items() if k != "expect"}
            self.assertEqual(
                {k: v for k, v in after[cid].items() if k != "expect"}, strip)
        self.assertEqual(manifest["dataset_version"], 4)
        self.assertEqual(manifest["cases"], 5)
        entries = manifest["prelook_fixes"]
        self.assertEqual([e["case_id"] for e in entries],
                         ["c-ho1", "c-train2"])
        e = entries[0]
        self.assertEqual(e["old_sha256"], sha256_of(before["c-ho1"]))
        self.assertEqual(e["new_sha256"], sha256_of(after["c-ho1"]))
        self.assertEqual(e["approved_by"], "the owner")
        self.assertEqual(e["motivated_by"], "c-train")
        self.assertEqual(e["transformation"], self.fix)
        self.assertEqual(e["dataset_version"], 4)
        self.assertRegex(e["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("# bumped on review", (self.state / "datasets" /
                      "dataset.yaml").read_text(encoding="utf-8"))

    def test_only_the_expect_block_of_a_case_file_is_rewritten(self):
        # A diff of a sealed file must show the check, never the input.
        path = self.state / "datasets" / "c-ho1.yaml"
        old = path.read_text(encoding="utf-8").splitlines()
        rc, out, _ = self.run_fix("--apply", "--approved-by", "o")
        self.assertEqual(rc, 0, out)
        new = path.read_text(encoding="utf-8").splitlines()
        start = old.index("expect:")
        end = old.index("gating: true")
        self.assertEqual(new[:start], old[:start])
        self.assertEqual(new[start + 1:], old[end:])
        self.assertTrue(new[start].startswith("expect: {"))
        self.assertIn(SEALED_INPUT, "\n".join(new[:start]))
        self.assertNotIn(SEALED_INPUT, new[start])

    def test_a_second_fix_appends_and_bumps_again(self):
        rc, out, _ = self.run_fix("--apply", "--approved-by", "owner")
        self.assertEqual(rc, 0, out)
        # The tool requirement a correct answer does not need; c-train lacks
        # `search`, so it already carries the fix.
        second = {"statement": "a correct answer needs no search call",
                  "op": "remove_item", "path": "expect.tools.subset",
                  "value": "search"}
        rc, out, _ = self.run_fix("--apply", "--approved-by", "owner",
                                  fix=second)
        self.assertEqual(rc, 0, out)
        after, manifest = self.converted()
        self.assertEqual(after["c-ho1"]["expect"]["tools"]["subset"],
                         ["get_shifts"])
        self.assertEqual(manifest["dataset_version"], 5)
        self.assertEqual([e["dataset_version"]
                          for e in manifest["prelook_fixes"]], [4, 4, 5, 5])

    def test_refused_once_the_ledger_has_a_line(self):
        ledger = self.state / "datasets" / "holdout-looks.jsonl"
        for row in ('{"run_id": "holdout-20261122T100000Z", "reason": "run"}',
                    "not even json"):
            with self.subTest(row=row):
                ledger.write_text(row + "\n", encoding="utf-8")
                before = self.snapshot()
                for argv in ((), ("--apply", "--approved-by", "owner")):
                    rc, out, err = self.run_fix(*argv)
                    self.assertEqual(rc, 2, out + err)
                    self.assertIn("ledger", json.loads(out)["error"])
                    self.assertIn("1 line", json.loads(out)["error"])
                self.assertEqual(self.snapshot(), before)

    def test_an_override_ledger_cannot_hide_the_default_one(self):
        (self.state / "datasets" / "holdout-looks.jsonl").write_text(
            '{"run_id": "holdout-20261122T100000Z"}\n', encoding="utf-8")
        rc, out, _ = self.run_fix("--holdout-ledger", "datasets/other.jsonl")
        self.assertEqual(rc, 2, out)
        self.assertIn("1 line", json.loads(out)["error"])
        (self.state / "datasets" / "holdout-looks.jsonl").unlink()
        (self.state / "datasets" / "other.jsonl").write_text(
            '{"run_id": "holdout-20261122T100000Z"}\n', encoding="utf-8")
        rc, out, _ = self.run_fix("--holdout-ledger", "datasets/other.jsonl")
        self.assertEqual(rc, 2, out)

    def test_a_holdout_run_at_this_version_is_a_look_without_a_ledger(self):
        run = self.state / "reports" / "holdout-20261122T100000Z"
        run.mkdir(parents=True)
        manifest = {"mode": "holdout", "selecting_split": "holdout",
                    "dataset_version": 3}
        (run / "manifest.yaml").write_text(json.dumps(manifest),
                                           encoding="utf-8")
        rc, out, _ = self.run_fix()
        self.assertEqual(rc, 2, out)
        self.assertIn("holdout-20261122T100000Z", json.loads(out)["error"])
        # A holdout run on an earlier seal (before the reseal's bump) is not.
        manifest["dataset_version"] = 2
        (run / "manifest.yaml").write_text(json.dumps(manifest),
                                           encoding="utf-8")
        rc, out, _ = self.run_fix()
        self.assertEqual(rc, 0, out)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores directory permissions")
    def test_a_failed_write_restores_every_case_file(self):
        # Cases in a subdirectory, dataset.yaml's directory read-only: the
        # case file is written first, then dataset.yaml fails.
        d = self.state / "datasets"
        (d / "cases").mkdir()
        for p in d.glob("c-*.yaml"):
            p.rename(d / "cases" / p.name)
        before = self.snapshot()
        os.chmod(d, 0o555)
        self.addCleanup(os.chmod, d, 0o755)
        rc, out, err = self.run_fix("--apply", "--approved-by", "o")
        os.chmod(d, 0o755)
        self.assertEqual(rc, 2, out + err)
        self.assertNotIn("Traceback", err)
        self.assertIn("every file was restored", json.loads(out)["error"])
        self.assertEqual(self.snapshot(), before)

    def test_an_empty_or_blank_ledger_is_zero_looks(self):
        (self.state / "datasets" / "holdout-looks.jsonl").write_text(
            "\n  \n", encoding="utf-8")
        rc, out, _ = self.run_fix()
        self.assertEqual(rc, 0, out)

    def test_refused_for_a_change_outside_expect(self):
        before = self.snapshot()
        for path in ("input.message", "split", "expect", "notes",
                     ["input", "message"]):
            with self.subTest(path=path):
                fix = dict(self.fix, path=path, op="set",
                           **{"from": "x", "to": "y"})
                rc, out, _ = self.run_fix("--apply", "--approved-by", "o",
                                          fix=fix)
                self.assertEqual(rc, 2, out)
                self.assertIn("outside expect", json.loads(out)["error"])
        self.assertEqual(self.snapshot(), before)

    def test_dry_run_writes_nothing_and_shows_ids_and_hashes_only(self):
        before = self.snapshot()
        rc, out, err = self.run_fix()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.snapshot(), before)
        doc = json.loads(out)
        self.assertFalse(doc["applied"])
        self.assertEqual(doc["dataset_version"], {"from": 3, "to": 4})
        self.assert_no_case_text(out + err)
        for row in doc["changed"]:
            self.assertEqual(set(row), {"id", "sealed", "old_sha256",
                                        "new_sha256"})

    def test_motivation_must_be_an_accepted_training_case_with_the_fix(self):
        for case_id, needle in (("c-ho1", "sealed"),
                                ("c-pending", "not accepted"),
                                ("c-train2", "does not already carry"),
                                ("c-train", "does not already carry"),
                                ("c-nope", "not a case")):
            with self.subTest(case_id=case_id):
                # c-train has no expect.args at all: "the key is absent" is
                # not evidence that it was ever deleted there.
                fix = self.fix if case_id != "c-train" else {
                    "statement": "s", "op": "delete",
                    "path": "expect.args.search", "from": {"q": "x"}}
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS / "prelook_fix.py"),
                     str(self.state), "--transformation",
                     json.dumps(fix), "--motivated-by", case_id],
                    capture_output=True, text=True, timeout=120,
                    env=dict(os.environ, EVALUP_NO_UV="1"))
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertIn(needle, json.loads(proc.stdout)["error"])
                self.assert_no_case_text(proc.stdout + proc.stderr)

    def test_apply_needs_an_approver_and_a_statement(self):
        rc, _, err = self.run_fix("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("--approved-by", err)
        rc, out, _ = self.run_fix(fix={k: v for k, v in self.fix.items()
                                       if k != "statement"})
        self.assertEqual(rc, 2)
        self.assertIn("statement", json.loads(out)["error"])

    def test_list_names_sealed_cases_and_templates_by_id_and_hash(self):
        rc, out, err = self.run_fix("--list", fix=False)
        self.assertEqual(rc, 0, out + err)
        self.assert_no_case_text(out + err)
        doc = json.loads(out)
        self.assertEqual(doc["ledger_lines"], 0)
        self.assertEqual([c["id"] for c in doc["sealed_cases"]],
                         ["c-ho1", "c-ho2"])
        self.assertEqual([t["template_id"] for t in doc["sealed_templates"]],
                         ["ho_shifts"])
        self.assertRegex(doc["sealed_templates"][0]["sha256"],
                         r"^[0-9a-f]{64}$")
        self.assertEqual(doc["mixed_templates"], [])
        self.assertEqual(doc["missing_templates"], [])
        self.assertNotIn("error", doc)

    def pin_rubric(self, case_id, rubric_id):
        path = self.state / "datasets" / f"{case_id}.yaml"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(
            "  answer:\n", f"  answer:\n    rubric: {rubric_id}\n", 1),
            encoding="utf-8")

    def write_rubric(self, rid, stem=None):
        rubrics = self.state / "rubrics"
        rubrics.mkdir(exist_ok=True)
        front = f"rubric_id: {rid}\n" if rid else ""
        (rubrics / f"{stem or rid}.md").write_text(
            f"---\n{front}version: 1\n---\n{SEALED_ORACLE}\n",
            encoding="utf-8")

    def sealed_and_mixed(self, *argv, **kw):
        rc, out, err = self.run_fix(*argv, **kw)
        self.assertEqual(rc, 0, out + err)
        self.assert_no_case_text(out + err)
        doc = json.loads(out)
        for r in doc["sealed_rubrics"]:
            self.assertRegex(r["sha256"], r"^[0-9a-f]{64}$")
        return ([r["rubric_id"] for r in doc["sealed_rubrics"]],
                doc["mixed_rubrics"])

    def test_list_seals_rubrics_only_sealed_cases_pin(self):
        # #145: a rubric only a holdout case pins says what that case
        # tests; it is sealed like its template. One a training case pins
        # too is read in the training review, so it is named, not sealed.
        for rid in ("decline", "shared", "ho-5e1d0c2a", "train-only"):
            self.write_rubric(rid)
        self.pin_rubric("c-ho1", "decline-v1")
        self.pin_rubric("c-ho2", "shared")
        self.pin_rubric("c-train2", "shared")
        self.pin_rubric("c-pending", "train-only")
        self.assertEqual(self.sealed_and_mixed("--list", fix=False),
                         (["decline", "ho-5e1d0c2a"], ["shared"]))
        # The dry run lists the same seal.
        self.assertEqual(self.sealed_and_mixed(),
                         (["decline", "ho-5e1d0c2a"], ["shared"]))

    def test_the_opaque_rubric_id_is_a_legal_one(self):
        from _rubric import rubric_findings
        self.assertEqual(rubric_findings({"rubric_id": "ho-5e1d0c2a", "version": 1},
                                         "ho-5e1d0c2a"), [])

    def test_an_opaque_rubric_a_training_case_pins_is_mixed_not_sealed(self):
        self.write_rubric("ho-77aa01ff")
        self.pin_rubric("c-train2", "ho-77aa01ff")
        self.assertEqual(self.sealed_and_mixed("--list", fix=False),
                         ([], ["ho-77aa01ff"]))

    def test_a_conversation_pin_is_seen_on_every_turn(self):
        # every_turn and a checkpoint's expect are judged as the case's.
        self.write_rubric("decline")
        self.write_rubric("trap")
        path = self.state / "datasets" / "c-ho2.yaml"
        text = path.read_text(encoding="utf-8").replace(
            f'input:\n  message: "{SEALED_INPUT}"\n',
            "input:\n  turns:\n    - user: hi\n      expect: "
            "{answer: {rubric: trap-v1}}\n    - user: again\n")
        path.write_text(text + "every_turn: {answer: {rubric: decline}}\n",
                        encoding="utf-8")
        self.assertEqual(self.sealed_and_mixed("--list", fix=False),
                         (["decline", "trap"], []))

    def test_a_rubric_file_is_matched_by_its_stem_or_its_id(self):
        # No rubric_id, or a stem the pin names with a version suffix.
        self.write_rubric(None, stem="decline")
        self.write_rubric("trap-v1")
        self.pin_rubric("c-ho1", "decline-v1")
        self.pin_rubric("c-ho2", "trap")
        self.assertEqual(self.sealed_and_mixed("--list", fix=False),
                         (["decline", "trap-v1"], []))

    def test_list_without_rubrics_lists_none(self):
        rc, out, err = self.run_fix("--list", fix=False)
        self.assertEqual(rc, 0, out + err)
        doc = json.loads(out)
        self.assertEqual(doc["sealed_rubrics"], [])
        self.assertEqual(doc["mixed_rubrics"], [])

    def test_list_fails_on_a_sealed_case_whose_template_has_no_file(self):
        # #133: a sealed case's template the listing never saw is outside
        # the seal; --list says so and exits 2, the listing still printed.
        (self.state / "datasets" / "templates" / "ho_shifts.yaml").unlink()
        rc, out, err = self.run_fix("--list", fix=False)
        self.assertEqual(rc, 2, out + err)
        self.assert_no_case_text(out + err)
        doc = json.loads(out)
        self.assertEqual(doc["missing_templates"], ["ho_shifts"])
        self.assertEqual(doc["sealed_templates"], [])
        self.assertIn("datasets/templates/", doc["error"])
        self.assertEqual(len(doc["sealed_cases"]), 2)

    def test_templates_at_the_state_root_are_listed_too(self):
        # #133: a first session wrote <state>/templates/; it is listed (so
        # sealed) rather than missed.
        root = self.state / "templates"
        root.mkdir()
        shutil.move(str(self.state / "datasets" / "templates" /
                        "ho_shifts.yaml"), str(root / "ho_shifts.yaml"))
        rc, out, err = self.run_fix("--list", fix=False)
        self.assertEqual(rc, 0, out + err)
        self.assert_no_case_text(out + err)
        doc = json.loads(out)
        self.assertEqual(doc["missing_templates"], [])
        self.assertEqual([t["template_id"] for t in doc["sealed_templates"]],
                         ["ho_shifts"])
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "convert_suite.py"),
             str(self.state), "--templates"], capture_output=True, text=True,
            timeout=60, check=True)
        files = {t["template_id"]: t["file"]
                 for t in json.loads(proc.stdout)["templates"]}
        self.assertEqual(files["ho_shifts"],
                         os.path.join("templates", "ho_shifts.yaml"))
        self.assertEqual(files["t_shifts"],
                         os.path.join("datasets", "templates", "t_shifts.yaml"))

    def test_templates_mode_prints_hashes_never_content(self):
        nested = self.state / "datasets" / "templates" / "deep"
        nested.mkdir()
        (nested / "ho_nested.yaml").write_text(
            f"oracle: {{fact: {SEALED_ORACLE}}}\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "convert_suite.py"),
             str(self.state), "--templates"], capture_output=True, text=True,
            timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assert_no_case_text(proc.stdout + proc.stderr)
        ids = [t["template_id"] for t in json.loads(proc.stdout)["templates"]]
        self.assertEqual(ids, ["ho_nested", "ho_shifts", "t_shifts"])
        # A broken sealed template: the parser's message quotes the line.
        (nested / "ho_nested.yaml").write_text(
            f"oracle: {{fact: {SEALED_ORACLE}\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "convert_suite.py"),
             str(self.state), "--templates"], capture_output=True, text=True,
            timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("withheld", json.loads(proc.stdout)["error"])
        self.assert_no_case_text(proc.stdout + proc.stderr)

    def test_a_convert_error_is_not_relayed(self):
        # A YAML error message quotes the line; that line can be sealed.
        (self.state / "datasets" / "c-ho2.yaml").write_text(
            f"id: c-ho2\ninput: {{message: {SEALED_INPUT}\n", encoding="utf-8")
        rc, out, err = self.run_fix()
        self.assertEqual(rc, 2, out)
        self.assertIn("withheld", json.loads(out)["error"])
        self.assert_no_case_text(out + err)

    def test_no_match_changes_nothing(self):
        fix = dict(self.fix, **{"from": "/never-used/"})
        before = self.snapshot()
        rc, out, _ = self.run_fix("--apply", "--approved-by", "o", fix=fix)
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["changed"], [])
        self.assertEqual(self.snapshot(), before)


class TestSealedTemplatesStayOutOfReaders(unittest.TestCase):
    """The review viewer, the judge and the rule report read run trees and
    rubrics, never generation templates; pin that, so a sealed template
    cannot reach a page, a judge prompt or a report through them."""

    def test_no_reader_opens_a_templates_dir(self):
        for name in ("build_review_viewer.py", "judge_run.py",
                     "rule_report.py"):
            with self.subTest(script=name):
                text = (SCRIPTS / name).read_text(encoding="utf-8")
                self.assertNotIn("templates", text)


if __name__ == "__main__":
    unittest.main()
