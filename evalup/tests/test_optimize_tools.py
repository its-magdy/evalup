"""optimize's writers (#143): candidate.py, seal_record.py, cluster_failures.py.

optimize pre-approves `Bash(evalup *)` and nothing that writes, yet its loop
snapshots and reverts the user's prompt files, keeps the seal record and
reads a run's clusters.json. A live re-run did all three by hand: git in
the shell, ad-hoc Python appending to holdout-seal.json, clustering in the
model's head. These tests pin the scripts that replace them, and above all
what they refuse: a path outside the app or inside the state location, a
second snapshot over an edit, a revert over someone else's change, a seal
record from a run that is not a finished holdout run, and clustering a run
that reached the seal.
"""
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
QUICKSTART = ROOT / "examples" / "quickstart"


def run(script, *argv):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *map(str, argv)],
        capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def rewrite(path, change):
    doc = json.loads(path.read_text(encoding="utf-8"))
    change(doc)
    path.write_text(json.dumps(doc), encoding="utf-8")


class TestCandidate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = pathlib.Path(os.path.realpath(tmp.name)) / "app"
        self.state = self.app / ".evalup"
        (self.state / "datasets").mkdir(parents=True)
        (self.app / "prompts").mkdir()
        self.prompt = self.app / "prompts" / "system.md"
        self.prompt.write_text("You are a helpful bot.\nBe brief.\n",
                               encoding="utf-8")
        self.prompt.chmod(0o640)

    def cand(self, command, *argv):
        return run("candidate.py", command, "--state-dir", self.state,
                   "--app", self.app, *argv)

    def test_begin_edit_diff_revert_round_trip(self):
        original = self.prompt.read_bytes()
        rc, out, _ = self.cand("begin", "--id", "c1", "prompts/system.md")
        self.assertEqual(rc, 0, out)
        cdir = self.state / "candidates" / "c1"
        self.assertEqual((cdir / "before" / "prompts" / "system.md")
                         .read_bytes(), original)
        record = json.loads((cdir / "before.json").read_text("utf-8"))
        self.assertEqual(record["files"][0]["path"], "prompts/system.md")
        self.assertEqual(record["schema_version"], 1)

        self.prompt.write_text("You are a helpful bot.\nBe very brief.\n",
                               encoding="utf-8")
        rc, out, _ = self.cand("diff", "--id", "c1")
        self.assertEqual(rc, 0, out)
        patch = (cdir / "diff.patch").read_text("utf-8")
        self.assertIn("-Be brief.\n+Be very brief.\n", patch)
        self.assertIn("a/prompts/system.md", patch)

        rc, out, _ = self.cand("revert", "--id", "c1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["restored"], ["prompts/system.md"])
        self.assertEqual(self.prompt.read_bytes(), original)
        self.assertEqual(self.prompt.stat().st_mode & 0o777, 0o640)
        # A second revert has nothing to restore and says so.
        rc, out, _ = self.cand("revert", "--id", "c1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["already_original"],
                         ["prompts/system.md"])

    def test_a_second_begin_never_snapshots_the_edit(self):
        self.assertEqual(self.cand("begin", "--id", "c1",
                                   "prompts/system.md")[0], 0)
        self.prompt.write_text("edited\n", encoding="utf-8")
        rc, out, _ = self.cand("begin", "--id", "c1", "prompts/system.md")
        self.assertEqual(rc, 2)
        self.assertIn("already exists", json.loads(out)["error"])
        snap = self.state / "candidates/c1/before/prompts/system.md"
        self.assertIn("Be brief.", snap.read_text("utf-8"))

    def test_refuses_paths_outside_the_app_or_inside_the_state(self):
        outside = self.app.parent / "elsewhere.md"
        outside.write_text("x\n", encoding="utf-8")
        case = self.state / "datasets" / "holdout.yaml"
        case.write_text("sealed\n", encoding="utf-8")
        link = self.app / "prompts" / "link.md"
        link.symlink_to(case)
        for path in (outside, "../elsewhere.md", ".evalup/datasets/holdout.yaml",
                     "prompts/link.md", "prompts", "prompts/missing.md"):
            with self.subTest(path=path):
                rc, out, _ = self.cand("begin", "--id", "c1", path)
                self.assertEqual(rc, 2, out)
                self.assertIn("refused", json.loads(out)["error"])
        self.assertFalse((self.state / "candidates").exists())

    def test_refuses_an_unsafe_id_and_another_app(self):
        rc, out, _ = self.cand("begin", "--id", "../x", "prompts/system.md")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.cand("begin", "--id", "c1",
                                   "prompts/system.md")[0], 0)
        other = self.app.parent / "other"
        other.mkdir()
        rc, out, _ = run("candidate.py", "revert", "--state-dir", self.state,
                         "--app", other, "--id", "c1")
        self.assertEqual(rc, 2, out)
        self.assertIn("not the app", json.loads(out)["error"])

    def test_diff_refuses_when_nothing_changed(self):
        self.assertEqual(self.cand("begin", "--id", "c1",
                                   "prompts/system.md")[0], 0)
        rc, out, _ = self.cand("diff", "--id", "c1")
        self.assertEqual(rc, 2, out)
        self.assertFalse((self.state / "candidates/c1/diff.patch").exists())

    def test_revert_refuses_over_someone_elses_change_unless_forced(self):
        original = self.prompt.read_bytes()
        self.cand("begin", "--id", "c1", "prompts/system.md")
        self.prompt.write_text("the candidate's edit\n", encoding="utf-8")
        self.cand("diff", "--id", "c1")
        self.prompt.write_text("the user's later edit\n", encoding="utf-8")
        rc, out, _ = self.cand("revert", "--id", "c1")
        self.assertEqual(rc, 2, out)
        self.assertIn("someone else", json.loads(out)["error"])
        self.assertEqual(self.prompt.read_text("utf-8"),
                         "the user's later edit\n")
        rc, out, _ = self.cand("revert", "--id", "c1", "--force")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.prompt.read_bytes(), original)

    def test_revert_refuses_a_tampered_snapshot(self):
        self.cand("begin", "--id", "c1", "prompts/system.md")
        self.prompt.write_text("edit\n", encoding="utf-8")
        self.cand("diff", "--id", "c1")
        (self.state / "candidates/c1/before/prompts/system.md").write_text(
            "not the original\n", encoding="utf-8")
        rc, out, _ = self.cand("revert", "--id", "c1")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.prompt.read_text("utf-8"), "edit\n")

    def test_revert_without_a_diff_and_of_a_deleted_file(self):
        # Grok on #152: the edit is the candidate's until a diff records it,
        # and a deleted surface file is recreated, never a block on the rest.
        two = self.app / "prompts" / "b.md"
        two.write_text("second\n", encoding="utf-8")
        self.cand("begin", "--id", "c1", "prompts/system.md", "prompts/b.md")
        self.prompt.write_text("edit, never diffed\n", encoding="utf-8")
        rc, out, _ = self.cand("revert", "--id", "c1")
        self.assertEqual(rc, 0, out)
        self.assertIn("Be brief.", self.prompt.read_text("utf-8"))
        self.cand("begin", "--id", "c2", "prompts/system.md", "prompts/b.md")
        self.prompt.write_text("edit\n", encoding="utf-8")
        two.write_text("edit b\n", encoding="utf-8")
        self.cand("diff", "--id", "c2")
        two.unlink()
        rc, out, _ = self.cand("revert", "--id", "c2")
        self.assertEqual(rc, 0, out)
        self.assertEqual(two.read_text("utf-8"), "second\n")
        self.assertIn("Be brief.", self.prompt.read_text("utf-8"))

    def test_symlinks_in_the_candidate_dir_or_the_live_path_are_refused(self):
        sealed = self.state / "datasets" / "holdout.yaml"
        sealed.write_text("SEALED-CASE-BODY\n", encoding="utf-8")
        # A link planted where begin would write the snapshot.
        planted = self.state / "candidates" / "c1" / "before" / "prompts"
        planted.mkdir(parents=True)
        (planted / "system.md").symlink_to(sealed)
        rc, out, _ = self.cand("begin", "--id", "c1", "prompts/system.md")
        self.assertEqual(rc, 2, out)
        self.assertEqual(sealed.read_text("utf-8"), "SEALED-CASE-BODY\n")
        # The candidates directory itself a link out of the state.
        elsewhere = self.app.parent / "elsewhere"
        elsewhere.mkdir()
        shutil.rmtree(self.state / "candidates")
        (self.state / "candidates").symlink_to(elsewhere)
        rc, out, _ = self.cand("begin", "--id", "c2", "prompts/system.md")
        self.assertEqual(rc, 2, out)
        self.assertEqual(list(elsewhere.iterdir()), [])
        (self.state / "candidates").unlink()
        # A snapshot swapped for a link after begin never reaches the diff.
        self.cand("begin", "--id", "c3", "prompts/system.md")
        snap = self.state / "candidates/c3/before/prompts/system.md"
        snap.unlink()
        snap.symlink_to(sealed)
        self.prompt.write_text("edit\n", encoding="utf-8")
        rc, out, _ = self.cand("diff", "--id", "c3")
        self.assertEqual(rc, 2, out)
        self.assertFalse((self.state / "candidates/c3/diff.patch").exists())
        # The live path swapped for a link: revert never writes through it.
        self.cand("begin", "--id", "c4", "prompts/system.md")
        other = self.app / "prompts" / "other.md"
        other.write_text("other\n", encoding="utf-8")
        self.prompt.unlink()
        self.prompt.symlink_to(other)
        rc, out, _ = self.cand("revert", "--id", "c4", "--force")
        self.assertEqual(rc, 2, out)
        self.assertEqual(other.read_text("utf-8"), "other\n")

    def test_a_hard_linked_surface_file_is_refused(self):
        sealed = self.state / "datasets" / "holdout.yaml"
        sealed.write_text("sealed\n", encoding="utf-8")
        linked = self.app / "prompts" / "linked.md"
        os.link(sealed, linked)
        rc, out, _ = self.cand("begin", "--id", "c1", "prompts/linked.md")
        self.assertEqual(rc, 2, out)
        self.assertIn("hard link", json.loads(out)["error"])

    def test_a_malformed_record_is_exit_2_not_a_traceback(self):
        self.cand("begin", "--id", "c1", "prompts/system.md")
        record = self.state / "candidates/c1/before.json"
        for text in ("[" * 100000, '{"files": NaN}'):
            with self.subTest(text=text[:12]):
                record.write_text(text, encoding="utf-8")
                rc, out, _ = self.cand("revert", "--id", "c1")
                self.assertEqual(rc, 2, out)
                self.assertIn("error", json.loads(out))

    def test_gate_saves_the_stats_decision_verbatim(self):
        self.cand("begin", "--id", "c1", "prompts/system.md")
        runs = {}
        for name, verdicts in (("champ", "pfpf"), ("cand", "pppf"),
                               ("na", "pfpf"), ("nb", "pfpf")):
            path = self.app.parent / f"{name}.jsonl"
            path.write_text("".join(
                json.dumps({"case_id": f"h{i}",
                            "verdict": "pass" if v == "p" else "fail"})
                + "\n" for i, v in enumerate(verdicts)), encoding="utf-8")
            runs[name] = path
        rc, out, _ = self.cand("gate", "--id", "c1", runs["champ"],
                               runs["cand"], "--noise-floor", runs["na"],
                               runs["nb"], "--candidate-looks", 1)
        self.assertEqual(rc, 0, out)
        saved = (self.state / "candidates/c1/holdout-gate.json").read_text(
            "utf-8")
        self.assertEqual(saved, out)
        self.assertIn("keep", json.loads(saved))
        # Not the decision call: nothing saved.
        (self.state / "candidates/c1/holdout-gate.json").unlink()
        rc, out, _ = self.cand("gate", "--id", "c1", runs["champ"],
                               runs["cand"])
        self.assertEqual(rc, 2, out)
        self.assertFalse((self.state / "candidates/c1/holdout-gate.json")
                         .exists())


class RunFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        proc = subprocess.run(
            [sys.executable, str(QUICKSTART / "demo.py"), "--keep"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        line = next(ln for ln in proc.stdout.splitlines()
                    if ln.startswith("run directory:"))
        cls.demo_run = pathlib.Path(line.split(":", 1)[1].strip())
        cls.addClassCleanup(shutil.rmtree, cls.demo_run.parents[2], True)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.reports = pathlib.Path(os.path.realpath(tmp.name)) / "reports"
        self.reports.mkdir()

    def make_run(self, run_id, mode, **manifest):
        run_dir = self.reports / run_id
        shutil.copytree(self.demo_run, run_dir)
        split = "holdout" if mode == "holdout" else "full"
        rewrite(run_dir / "manifest.yaml", lambda d: d.update(
            run_id=run_id, mode=mode, selecting_split=split, **manifest))
        rewrite(run_dir / "results.json",
                lambda d: d.update(run_id=run_id, mode=mode))
        return run_dir


class TestSealRecord(RunFixture):
    def seal(self, command, target):
        rc, out, err = run("seal_record.py", command, target)
        return rc, json.loads(out) if out.strip() else err

    def record(self):
        return json.loads((self.reports / "holdout-seal.json")
                          .read_text("utf-8"))

    def test_the_whole_seal_lifecycle_copies_the_manifest(self):
        a = self.make_run("holdout-a", "holdout")
        b = self.make_run("holdout-b", "holdout")
        c = self.make_run("holdout-c", "holdout")
        rc, out = self.seal("noise", a)
        self.assertEqual(rc, 0, out)
        manifest = json.loads((a / "manifest.yaml").read_text("utf-8"))
        rec = self.record()
        self.assertEqual(rec["noise_runs"], ["holdout-a"])
        self.assertEqual(rec["champion_run"], "holdout-a")
        self.assertEqual(rec["candidate_runs"], [])
        for key in ("dataset_version", "scoring_version", "k"):
            self.assertEqual(rec[key], manifest[key], key)
        self.assertFalse(out["pair_whole"])
        # A candidate before the pair is whole is refused.
        rc, out = self.seal("candidate", c)
        self.assertEqual(rc, 2)
        self.assertIn("not 2", out["error"])
        self.assertEqual(self.seal("noise", b)[0], 0)
        # Recording the same run again changes nothing.
        rc, out = self.seal("noise", b)
        self.assertEqual((rc, out["changed"]), (0, False))
        rc, out = self.seal("noise", c)
        self.assertEqual(rc, 2, "a third noise run")
        rc, out = self.seal("candidate", c)
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["candidate_looks"], 1)
        rc, out = self.seal("keep", c)
        self.assertEqual(rc, 0, out)
        rec = self.record()
        self.assertEqual(rec["champion_run"], "holdout-c")
        self.assertEqual(rec["noise_runs"], ["holdout-a", "holdout-b"])
        rc, out = self.seal("show", self.reports)
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["pair_whole"])
        self.assertEqual(out["missing_run_dirs"], [])
        self.assertTrue(out["scoring_version_current"])

    def test_refuses_what_is_not_a_finished_holdout_run(self):
        smoke = self.make_run("smoke-x", "smoke")
        running = self.make_run("holdout-r", "holdout")
        rewrite(running / "results.json",
                lambda d: d["summary"].update(status="running"))
        renamed = self.make_run("holdout-m", "holdout")
        rewrite(renamed / "manifest.yaml", lambda d: d.update(run_id="other"))
        for run_dir, says in ((smoke, "holdout runs only"),
                              (running, "resume it"),
                              (renamed, "does not match")):
            with self.subTest(run=run_dir.name):
                rc, out = self.seal("noise", run_dir)
                self.assertEqual(rc, 2)
                self.assertIn(says, out["error"])
        self.assertFalse((self.reports / "holdout-seal.json").exists())

    def test_a_new_seal_replaces_the_old_record_and_mixing_is_refused(self):
        self.seal("noise", self.make_run("holdout-a", "holdout"))
        self.seal("noise", self.make_run("holdout-b", "holdout"))
        manifest = json.loads((self.reports / "holdout-a" / "manifest.yaml")
                              .read_text("utf-8"))
        bumped = manifest["dataset_version"] + 1
        late = self.make_run("holdout-c", "holdout", dataset_version=bumped)
        rc, out = self.seal("candidate", late)
        self.assertEqual(rc, 2)
        self.assertIn("another seal", out["error"])
        rc, out = self.seal("noise", late)
        self.assertEqual(rc, 0, out)
        self.assertIn("replaced the record of another seal", out["notes"][0])
        self.assertEqual(self.record()["noise_runs"], ["holdout-c"])
        self.assertEqual(self.record()["dataset_version"], bumped)

    def test_it_never_opens_a_case_file(self):
        a = self.make_run("holdout-a", "holdout")
        shutil.rmtree(a / "cases")
        rc, out = self.seal("noise", a)
        self.assertEqual(rc, 0, out)


class TestClusterFailures(RunFixture):
    def test_groups_failing_rows_and_counts_the_population(self):
        run_dir = self.make_run("regression-x", "regression")
        rows = [json.loads(line) for line in
                (run_dir / "verdicts.jsonl").read_text("utf-8").splitlines()
                if line.strip()]
        base = dict(rows[0], set="full", canary=False, gating=True,
                    failed_turn=None)
        fail_answer = dict(base["layers"], answer="fail")
        fail_route = dict(base["layers"], routing="fail")
        made = [dict(base, case_id="p1", verdict="pass"),
                dict(base, case_id="p2", verdict="pass"),
                dict(base, case_id="f1", verdict="fail", layers=fail_answer),
                dict(base, case_id="f2", verdict="fail", layers=fail_route),
                dict(base, case_id="f3", verdict="fail", layers=fail_answer),
                dict(base, case_id="canary", verdict="fail", canary=True,
                     layers=fail_answer),
                dict(base, case_id="ungated", verdict="fail", gating=False,
                     layers=fail_answer),
                dict(base, case_id="infra", verdict="infra_error")]
        (run_dir / "verdicts.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in made), encoding="utf-8")
        rc, out, _ = run("cluster_failures.py", run_dir)
        self.assertEqual(rc, 0, out)
        doc = json.loads((run_dir / "clusters.json").read_text("utf-8"))
        self.assertEqual(doc["population"], 5)
        self.assertEqual(doc["source"], "cluster-failures")
        self.assertEqual(doc["run_id"], "regression-x")
        first, second = doc["clusters"]
        self.assertEqual((first["case_ids"], first["layers"]),
                         (["f1", "f3"], ["answer"]))
        self.assertEqual(first["rate"], 0.4)
        self.assertEqual(second["case_ids"], ["f2"])
        self.assertEqual((first["status"], first["surface"]),
                         ("unconfirmed", None))
        # Its own file is replaced on a second call.
        self.assertEqual(run("cluster_failures.py", run_dir)[0], 0)

    def test_refuses_a_holdout_run_and_an_analyze_file(self):
        holdout = self.make_run("holdout-x", "holdout")
        rc, out, _ = run("cluster_failures.py", holdout)
        self.assertEqual(rc, 2, out)
        self.assertIn("sealed", json.loads(out)["error"])
        self.assertFalse((holdout / "clusters.json").exists())

        reg = self.make_run("regression-y", "regression")
        theirs = {"schema_version": 1, "run_id": "regression-y",
                  "clusters": [{"id": "k1", "status": "real"}]}
        (reg / "clusters.json").write_text(json.dumps(theirs), "utf-8")
        rc, out, _ = run("cluster_failures.py", reg)
        self.assertEqual(rc, 2, out)
        self.assertIn("analyze wrote it", json.loads(out)["error"])
        self.assertEqual(json.loads((reg / "clusters.json")
                                    .read_text("utf-8")), theirs)
        self.assertEqual(run("cluster_failures.py", reg, "--force")[0], 0)

    def test_refuses_a_run_that_did_not_finish(self):
        reg = self.make_run("regression-z", "regression")
        rewrite(reg / "results.json",
                lambda d: d["summary"].update(status="running"))
        rc, out, _ = run("cluster_failures.py", reg)
        self.assertEqual(rc, 2, out)


class TestOptimizeCallsThem(unittest.TestCase):
    """The skill names each script where it used to do the step by hand."""

    def test_optimize_uses_the_scripts_not_git_or_hand_edits(self):
        text = (ROOT / "skills" / "optimize" / "SKILL.md").read_text("utf-8")
        for call in ("evalup candidate begin", "evalup candidate diff",
                     "evalup candidate revert", "evalup candidate gate",
                     "evalup seal-record noise",
                     "evalup seal-record candidate", "evalup seal-record keep",
                     "evalup seal-record show", "evalup cluster-failures"):
            with self.subTest(call=call):
                self.assertIn(call, text)
        for gone in ("git -C <app> checkout", "git hash-object",
                     "git -C <app> rev-parse"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, text)


if __name__ == "__main__":
    unittest.main()
