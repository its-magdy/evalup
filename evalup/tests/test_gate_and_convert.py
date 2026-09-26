"""gate.py, convert_suite.py and make_plan.py: the steps that used to be the
model's.

Both exist because of the same finding (2026-09 audit): a mechanical step left
to prose. The CI gate was a bash snippet needing jq and yq, so at the shell a
red suite and a green one were both rc 0 with nothing printed; and the
YAML -> JSON conversion was the model re-typing the suite on every run.
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
    proc = subprocess.run([sys.executable, str(SCRIPTS / script), *argv],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)


class TestGate(TempDirTest):
    def write_run(self, run_id="regression-20260901T120000Z", exit_code=0,
                  **summary):
        base = {"status": "ok", "n": 6, "attempted": 6, "passes": 6,
                "failures": 0, "gating_failures": 0, "unscored": 0,
                "skipped": 0, "infra_errors": 0, "infra_rate": 0.0,
                "crash_rate": 0.0, "scorer_errors": 0,
                "unscorable_layers": ["trajectory"], "unjudged": "mode: smoke",
                "canaries": {"n": 2, "passed": 2}, "holdout": None,
                "missing_artifacts": []}
        base.update(summary)
        out = self.tmp / "reports" / run_id
        out.mkdir(parents=True, exist_ok=True)      # subtests rewrite one run
        (out / "results.json").write_text(json.dumps(
            {"run_id": run_id, "mode": run_id.split("-")[0], "cases": [],
             "summary": base, "exit_code": exit_code}), encoding="utf-8")
        return str(out)

    # write_run() fabricates a results.json with no run directory around it,
    # so these tests gate it with --no-verify; TestGateVerifiesTheRunDirectory
    # below covers the default.
    def test_a_green_run_is_0_and_says_what_it_could_not_see(self):
        rc, out, err = run("gate.py", "--no-verify", self.write_run())
        self.assertEqual(rc, 0, out + err)
        self.assertIn("PASS", out)
        self.assertIn("NOT scored this run: trajectory", out)

    def test_a_red_run_is_1(self):
        # The whole point: run_cases.py exits 0 on this, by design.
        rc, out, _ = run("gate.py", "--no-verify", self.write_run(
            passes=0, failures=6, gating_failures=6))
        self.assertEqual(rc, 1)
        self.assertIn("6 gating failure(s)", out)

    def test_every_closing_reason_is_listed_not_the_first(self):
        rc, out, _ = run("gate.py", "--json", "--no-verify", self.write_run(
            exit_code=7, status="incomplete", gating_failures=1,
            infra_rate=0.5, missing_artifacts=["verdicts.jsonl"],
            holdout={"n": 2, "passes": 1, "gating_failures": 1}))
        self.assertEqual(rc, 1)
        payload = json.loads(out)
        self.assertEqual(payload["gate"], "closed")
        self.assertEqual(len(payload["reasons"]), 6)
        self.assertEqual(payload["gating_failures"], 2)

    def test_zero_failures_from_an_aborted_run_is_not_a_pass(self):
        rc, _, _ = run("gate.py", "--no-verify",
                       self.write_run(status="aborted_infra", exit_code=5))
        self.assertEqual(rc, 1)

    def test_the_infra_threshold_is_a_flag(self):
        path = self.write_run(infra_rate=0.2)
        self.assertEqual(run("gate.py", "--no-verify", path)[0], 1)
        self.assertEqual(run("gate.py", "--no-verify", path,
                             "--max-infra-rate", "0.25")[0], 0)

    def test_latest_is_by_the_run_ids_timestamp_never_mtime(self):
        self.write_run("regression-20260902T120000Z", gating_failures=3)
        older = self.write_run("regression-20260901T120000Z")
        self.write_run("smoke-20260903T120000Z")
        # Touch the OLDER run last: an mtime pick would choose it and pass.
        pathlib.Path(older, "results.json").touch()
        reports = str(self.tmp / "reports")
        rc, out, _ = run("gate.py", "--no-verify", reports, "--latest",
                         "--mode", "regression")
        self.assertEqual(rc, 1)
        self.assertIn("regression-20260902T120000Z", out)
        rc, out, _ = run("gate.py", "--no-verify", reports, "--latest")
        self.assertIn("smoke-20260903T120000Z", out)

    def test_bad_input_is_the_shared_error_contract(self):
        for argv in ([str(self.tmp)], [str(self.tmp), "--latest"],
                     [self.write_run(), "--mode", "smoke"],
                     [self.write_run("smoke-20260901T120000Z"),
                      "--max-infra-rate", "7"]):
            with self.subTest(argv=argv[1:]):
                rc, out, err = run("gate.py", *argv)
                self.assertEqual(rc, 2, out + err)
                self.assertIn("error", json.loads(out))
                self.assertNotIn("Traceback", err)

    def test_nothing_measured_is_not_a_pass(self):
        # 2026-09-21 audit: a suite of multi-turn cases is skipped whole, by
        # design, and gated green forever. skipped/unscored are never a
        # failure -- which is why only they cannot be a pass either.
        for label, summary in (
                ("all skipped", {"passes": 0, "skipped": 6}),
                ("all unscored", {"passes": 0, "unscored": 6}),
                ("no cases", {"n": 0, "passes": 0})):
            with self.subTest(label):
                rc, out, _ = run("gate.py", "--no-verify",
                                 self.write_run(**summary))
                self.assertEqual(rc, 1, out)
                self.assertIn("nothing was measured", out)

    def test_a_count_that_is_not_an_int_closes_the_gate(self):
        # `"gating_failures": "10"` used to read as 0: ten failures, PASS.
        for value in ("10", 10.0, None, True, -1):
            with self.subTest(value=value):
                rc, out, _ = run("gate.py", "--no-verify", self.write_run(
                    passes=0, failures=10, gating_failures=value))
                self.assertEqual(rc, 1, out)
                self.assertIn("summary.gating_failures", out)

    def test_a_two_key_results_json_is_not_a_pass(self):
        out_dir = self.tmp / "reports" / "regression-20260901T120000Z"
        out_dir.mkdir(parents=True)
        (out_dir / "results.json").write_text(json.dumps(
            {"run_id": out_dir.name, "summary": {"status": "ok"}}),
            encoding="utf-8")
        self.assertEqual(run("gate.py", "--no-verify", str(out_dir))[0], 1)

    def test_a_closed_gate_names_the_failing_cases(self):
        path = self.write_run(passes=5, failures=1, gating_failures=1)
        results = json.loads(
            pathlib.Path(path, "results.json").read_text(encoding="utf-8"))
        results["cases"] = [
            {"case_id": "c-aaaaaaaa", "verdict": "pass", "gating": True,
             "layers": {"answer": "pass"}},
            {"case_id": "c-bbbbbbbb", "verdict": "fail", "gating": True,
             "layers": {"routing": "pass", "answer": "fail"}},
            {"case_id": "c-cccccccc", "verdict": "fail", "gating": False,
             "layers": {"answer": "fail"}}]
        pathlib.Path(path, "results.json").write_text(json.dumps(results),
                                                      encoding="utf-8")
        rc, out, _ = run("gate.py", "--no-verify", path)
        self.assertEqual(rc, 1)
        self.assertIn("c-bbbbbbbb (answer)", out)
        self.assertNotIn("c-cccccccc", out)

    def test_no_verify_says_so(self):
        _, out, _ = run("gate.py", "--no-verify", self.write_run())
        self.assertIn("artifacts NOT verified", out)


class TestGateVerifiesTheRunDirectory(TempDirTest):
    """results.json is the file under audit, so its own `missing_artifacts: []`
    is not evidence. The gate recounts from cases/*/verdict.json by calling
    run_cases.py --verify (2026-09-21 audit: --verify caught a doctored run and
    gate.py said PASS over the same directory)."""

    @classmethod
    def setUpClass(cls):
        # One demo run serves the class (it is a whole quickstart run); the
        # test that doctors it works on a copy.
        super().setUpClass()
        proc = subprocess.run(
            [sys.executable, str(QUICKSTART / "demo.py"), "--keep"],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        line = next(ln for ln in proc.stdout.splitlines()
                    if ln.startswith("run directory:"))
        cls.run_dir = pathlib.Path(line.split(":", 1)[1].strip())
        cls.addClassCleanup(shutil.rmtree, cls.run_dir.parents[2], True)

    def test_a_real_run_gates_green_with_verification_on(self):
        rc, out, err = run("gate.py", str(self.run_dir))
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("NOT verified", out)

    def test_a_verdict_that_disagrees_with_results_json_closes_the_gate(self):
        run_dir = self.tmp / self.run_dir.name
        shutil.copytree(self.run_dir, run_dir)
        verdict_path = next((run_dir / "cases").iterdir()) / "verdict.json"
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        verdict["verdict"] = "fail"
        verdict_path.write_text(json.dumps(verdict), encoding="utf-8")
        rc, out, _ = run("gate.py", str(run_dir))
        self.assertEqual(rc, 1, out)
        self.assertIn("fails run_cases.py --verify", out)

    def test_a_fabricated_results_json_does_not_verify(self):
        out_dir = self.tmp / "reports" / "regression-20260901T120000Z"
        out_dir.mkdir(parents=True)
        (out_dir / "results.json").write_text(json.dumps(
            {"run_id": out_dir.name, "mode": "regression", "cases": [],
             "exit_code": 0,
             "summary": {"status": "ok", "n": 6, "passes": 6, "failures": 0,
                         "gating_failures": 0, "missing_artifacts": []}}),
            encoding="utf-8")
        self.assertEqual(run("gate.py", str(out_dir))[0], 1)


class TestMakePlan(TempDirTest):
    """The plan was the last artifact a model assembled by hand: in the first
    live session (2026-09-21) about half of 75 tool calls were ad-hoc scripts
    and failed runner starts on plan.json. Runs off the committed
    converted.json, so it needs no PyYAML."""

    def setUp(self):
        super().setUp()
        self.state = self.tmp / ".evalup"
        self.state.mkdir()
        self.converted = str(QUICKSTART / "converted.json")

    def plan(self, *argv, converted=None):
        rc, out, err = run("make_plan.py", converted or self.converted,
                           "--state-dir", str(self.state), *argv)
        self.assertNotIn("Traceback", err)
        return rc, json.loads(out)

    def with_cases(self, mutate):
        doc = json.loads(pathlib.Path(self.converted).read_text("utf-8"))
        doc.pop("//", None)
        mutate(doc["cases"])
        path = self.tmp / "converted.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return str(path)

    def test_the_smoke_plan_is_one_the_runner_accepts(self):
        rc, plan = self.plan("--mode", "smoke", "--oos-route", "none")
        self.assertEqual(rc, 0)
        self.assertEqual((plan["mode"], plan["k"], plan["gate"],
                          plan["selecting_split"]), ("smoke", 1, "soft", "smoke"))
        self.assertEqual(len(plan["cases"]), 6)
        self.assertEqual(plan["paths"]["scripts_dir"], str(SCRIPTS))
        self.assertIsNone(plan["paths"]["holdout_ledger"])
        # A secret must not reach the plan file: refs stay as written.
        self.assertEqual(plan["adapter"]["invocation"]["base_url"],
                         "${HELPDESK_BASE_URL}")
        plan_path = self.tmp / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_cases.py"), "--plan",
             str(plan_path), "--out",
             str(self.state / "reports" / plan["run_id"]), "--dry-run"],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ,
                     HELPDESK_BASE_URL="http://127.0.0.1:9"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_insecure_tls_is_a_flag_not_a_hand_edit(self):
        # The field test (2026-09-24) hit a self-signed dev host, saw exit 3
        # CERTIFICATE_VERIFY_FAILED, and hand-edited plan.json twice because
        # the flag did not exist. Absent -> no execution block at all, so the
        # runner's defaults (insecure_tls false) apply untouched.
        rc, plan = self.plan("--mode", "smoke", "--oos-route", "none")
        self.assertEqual(rc, 0)
        self.assertNotIn("execution", plan)
        rc, plan = self.plan("--mode", "smoke", "--oos-route", "none",
                             "--insecure-tls")
        self.assertEqual(rc, 0)
        self.assertEqual(plan["execution"], {"insecure_tls": True})
        rc, plan = self.plan("--mode", "smoke", "--oos-route", "none",
                             "--insecure-tls", "--timeout-s", "150")
        self.assertEqual(plan["execution"],
                         {"timeout_s": 150.0, "insecure_tls": True})
        # The runner accepts the plan and reports the field back on --dry-run.
        plan_path = self.tmp / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_cases.py"), "--plan",
             str(plan_path), "--out",
             str(self.state / "reports" / plan["run_id"]), "--dry-run"],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, HELPDESK_BASE_URL="http://127.0.0.1:9"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIs(json.loads(proc.stdout)["execution"]["insecure_tls"],
                      True)

    def test_manifest_extra_merges_under_the_computed_provenance(self):
        # F-032 (field test 2026-09-25): `app: {model: ...}` -- the shape the
        # runner contract SS2 shows -- replaced the whole computed `app`
        # block, and the pinned baseline recorded no git SHA.
        extra = self.tmp / "extra.json"
        extra.write_text(json.dumps({
            "app": {"model": {"classifier": "m1"}, "git_sha": "deadbeef"},
            "dataset_version": 99, "prompt_hash": "abc"}), encoding="utf-8")
        rc, plan = self.plan("--mode", "smoke", "--oos-route", "none",
                             "--manifest-extra", str(extra))
        self.assertEqual(rc, 0)
        app = plan["manifest_extra"]["app"]
        self.assertEqual(app["model"], {"classifier": "m1"})
        self.assertEqual((app["name"], app["repo"], app["git_tree"]),
                         ("helpdesk-demo", ".", "n/a"))
        # The computed provenance wins over a caller value, both levels.
        self.assertIsNone(app["git_sha"])
        self.assertEqual(plan["manifest_extra"]["dataset_version"], 1)
        self.assertEqual(plan["manifest_extra"]["prompt_hash"], "abc")
        out_path = self.tmp / "plan.json"
        rc, summary = self.plan("--mode", "smoke", "--oos-route", "none",
                                "--manifest-extra", str(extra), "-o",
                                str(out_path))
        self.assertEqual(rc, 0)
        self.assertTrue(any("app.git_sha" in n for n in summary["notes"]))
        self.assertTrue(any("dataset_version" in n for n in summary["notes"]))
        # A scalar where the computed block is an object cannot merge.
        extra.write_text(json.dumps({"app": 3}), encoding="utf-8")
        rc, err = self.plan("--mode", "smoke", "--oos-route", "none",
                            "--manifest-extra", str(extra))
        self.assertEqual(rc, 2)
        self.assertIn("'app' must be an object", err["error"])

    def test_dash_o_prints_the_command_to_run_next(self):
        out_path = self.tmp / "plan.json"
        rc, summary = self.plan("--mode", "regression", "-o", str(out_path))
        self.assertEqual(rc, 0)
        self.assertEqual((summary["k"], summary["gate"]), (3, "hard"))
        self.assertIn("run_cases.py", summary["run"])
        self.assertTrue(summary["out"].endswith(summary["run_id"]))
        self.assertEqual(json.loads(out_path.read_text("utf-8"))["run_id"],
                         summary["run_id"])

    def test_targeted_selects_on_unit_or_route_never_on_id(self):
        rc, plan = self.plan("--mode", "targeted", "--tag", "shifts")
        self.assertEqual(rc, 0)
        self.assertTrue(plan["cases"])
        for case in plan["cases"]:
            self.assertIn("shifts", (case.get("unit"),
                                     case["expect"].get("route")))
        rc, payload = self.plan("--mode", "targeted")
        self.assertEqual(rc, 2)
        self.assertIn("--tag", payload["error"])

    def test_holdout_gets_the_ledger_and_regression_does_not(self):
        def seal_one(cases):
            cases[0]["split"] = ["holdout"]
        converted = self.with_cases(seal_one)
        rc, plan = self.plan("--mode", "holdout", converted=converted)
        self.assertEqual(rc, 0)
        self.assertEqual(len(plan["cases"]), 1)
        self.assertEqual(plan["gate"], "decision")
        self.assertEqual(plan["paths"]["holdout_ledger"],
                         "datasets/holdout-looks.jsonl")
        rc, plan = self.plan("--mode", "regression", converted=converted)
        self.assertEqual(len(plan["cases"]), 5)       # the sealed one is out
        self.assertIsNone(plan["paths"]["holdout_ledger"])

    def test_layer_disables_the_others_with_a_reason(self):
        rc, plan = self.plan("--mode", "smoke", "--layer", "routing")
        self.assertEqual(rc, 0)
        for name, body in plan["capability_matrix"].items():
            if name != "routing":
                self.assertEqual(body, {"enabled": False,
                                        "blocked_by": "--layer routing"})

    def test_what_it_refuses(self):
        def no_smoke(cases):
            for case in cases:
                case["split"] = ["full"]
        for label, argv, needle, converted in (
                ("full is two runs", ["--mode", "full"], "regression", None),
                ("unknown mode", ["--mode", "nightly"], "--mode", None),
                ("k under smoke", ["--mode", "smoke", "--k", "3"], "--k", None),
                ("unknown layer", ["--mode", "smoke", "--layer", "vibes"],
                 "capability matrix", None),
                ("tag outside targeted", ["--mode", "smoke", "--tag", "x"],
                 "targeted", None),
                ("nothing selected", ["--mode", "smoke"], "no case selected",
                 self.with_cases(no_smoke)),
                ("not a converted document", ["--mode", "smoke"], "cases",
                 str(QUICKSTART / "plan.template.json"))):
            with self.subTest(label):
                rc, payload = self.plan(*argv, converted=converted)
                self.assertEqual(rc, 2, payload)
                self.assertIn(needle, payload["error"])


def have_yaml():
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(have_yaml(), "PyYAML not installed; convert_suite.py is "
                                  "the one script that needs it")
class TestConvertSuite(TempDirTest):
    def state(self):
        state = self.tmp / "state"
        shutil.copytree(QUICKSTART / ".evalup", state)
        return state

    def test_it_reproduces_the_committed_conversion_exactly(self):
        """converted.json was maintained by a README one-liner; this script is
        that step, owned. Same cases, same order, same adapter, same matrix."""
        rc, out, err = run("convert_suite.py", str(self.state()))
        self.assertEqual(rc, 0, err)
        new = json.loads(out)
        committed = json.loads(
            (QUICKSTART / "converted.json").read_text(encoding="utf-8"))
        for key in ("adapter", "capability_matrix", "cases"):
            self.assertEqual(new[key], committed[key], key)
        self.assertEqual(sorted(new["sources"]),
                         [case["id"] for case in committed["cases"]])

    def test_the_split_dir_feeds_the_linter_clean(self):
        split = self.tmp / "split"
        rc, _, err = run("convert_suite.py", str(self.state()),
                         "-o", str(self.tmp / "c.json"),
                         "--split-dir", str(split))
        self.assertEqual(rc, 0, err)
        rc, out, err = run("validate_cases.py", "--strict",
                           "--cases", str(split / "suite.json"),
                           "--capabilities", str(split / "capabilities.json"))
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(json.loads(out)["findings"], [])

    def test_env_refs_are_left_unresolved(self):
        state = self.state()
        adapter = state / "adapter.yaml"
        adapter.write_text(adapter.read_text(encoding="utf-8")
                           + '\nnote: "${CONVERT_SUITE_SECRET}"\n',
                           encoding="utf-8")
        rc, out, err = run("convert_suite.py", str(state))
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["adapter"]["note"],
                         "${CONVERT_SUITE_SECRET}")

    def test_a_duplicate_key_is_an_error_not_the_last_value(self):
        state = self.state()
        (state / "datasets" / "dup.yaml").write_text(
            "id: c-aaaa0001\nsplit: [full]\nid: c-aaaa0002\n", encoding="utf-8")
        rc, out, err = run("convert_suite.py", str(state))
        self.assertEqual(rc, 2, err)
        self.assertIn("duplicate key 'id'", json.loads(out)["error"])

    def test_one_id_in_two_files_is_an_error(self):
        state = self.state()
        first = sorted((state / "datasets").glob("c-*.yaml"))[0]
        (state / "datasets" / "nested").mkdir()
        shutil.copy(first, state / "datasets" / "nested" / "copy.yaml")
        rc, out, _ = run("convert_suite.py", str(state))
        self.assertEqual(rc, 2)
        self.assertIn("is in both", json.loads(out)["error"])

    def test_the_manifest_and_templates_are_not_cases(self):
        state = self.state()
        (state / "datasets" / "dataset.yaml").write_text(
            "dataset_version: 3\nas_of: 2026-09-01\n", encoding="utf-8")
        (state / "datasets" / "templates").mkdir()
        (state / "datasets" / "templates" / "t.yaml").write_text(
            "dimensions: [a]\n", encoding="utf-8")
        rc, out, err = run("convert_suite.py", str(state))
        self.assertEqual(rc, 0, err)
        doc = json.loads(out)
        self.assertEqual(len(doc["cases"]), 6)
        # A YAML date is written as the ISO string the file spelled.
        self.assertEqual(doc["manifest"],
                         {"dataset_version": 3, "as_of": "2026-09-01"})

    def test_bad_input_is_the_shared_error_contract(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        broken = self.state()
        (broken / "adapter.yaml").write_text("a: [unclosed\n", encoding="utf-8")
        for path in (self.tmp / "missing", empty, broken):
            with self.subTest(path=path.name):
                rc, out, err = run("convert_suite.py", str(path))
                self.assertEqual(rc, 2, out + err)
                self.assertIn("error", json.loads(out))
                self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
