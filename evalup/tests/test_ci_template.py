"""ci/github-actions.yml, the workflow template users copy (#40, item 2).

ci.yml's `ci-template` job runs the template's shell steps for real
(tests/run_template.py). What that job cannot see is pinned here: the
`uses:` steps it skips must be the ones the job itself runs, and the
template must keep the shape the step runner can run faithfully.
"""
import json
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_template  # noqa: E402

try:
    import yaml
except ImportError:  # PyYAML is optional on the floor run, as for convert_suite
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "ci" / "github-actions.yml"
CI = ROOT.parent / ".github" / "workflows" / "ci.yml"
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+?)@(\S+)(.*)$", re.M)


def pins(text):
    return {action: (ref, comment.strip())
            for action, ref, comment in USES.findall(text)}


class TestTemplateText(unittest.TestCase):
    text = TEMPLATE.read_text(encoding="utf-8")
    ci = CI.read_text(encoding="utf-8")

    def test_every_action_is_sha_pinned_with_its_tag(self):
        found = pins(self.text)
        self.assertGreaterEqual(len(found), 3)
        for action, (ref, comment) in found.items():
            self.assertRegex(ref, r"^[0-9a-f]{40}$", action)
            self.assertRegex(comment, r"^# v\d", action)

    def test_its_actions_are_the_ones_ci_runs_at_the_same_pins(self):
        """Dependabot bumps ci.yml, not this file (it reads only
        .github/workflows/): a bump there fails here until the template
        follows. ci.yml's `ci-template` job uses every action the template
        does, which is also what keeps the step runner's skipped steps
        honest."""
        ci = pins(self.ci)
        for action, pin in pins(self.text).items():
            self.assertEqual(ci.get(action), pin, action)

    def test_the_plugin_is_pinned_to_this_versions_release_tag(self):
        # A version bump moves the pin in the same commit; publishing the
        # release on that commit, once it is on main, creates the tag. So a
        # template copied from main names a release (CI never fetches it:
        # run_template.py pins HEAD).
        version = json.loads((ROOT / ".claude-plugin" / "plugin.json")
                             .read_text(encoding="utf-8"))["version"]
        self.assertRegex(self.text,
                         rf"(?m)^  EVALUP_REF: v{re.escape(version)}$")

    def test_the_hardening_lines_are_present(self):
        for line in ("permissions:\n  contents: read",
                     "runs-on: ubuntu-24.04", "persist-credentials: false",
                     "timeout-minutes:", "shell: bash"):
            self.assertIn(line, self.text)

    def test_ci_runs_it_and_ci_ok_needs_that_job(self):
        self.assertIn("python tests/run_template.py", self.ci)
        needs = re.search(r"(?m)^    needs: \[(.*)\]$", self.ci).group(1)
        self.assertIn("ci-template", [n.strip() for n in needs.split(",")])


@unittest.skipUnless(yaml, "PyYAML not installed")
class TestTemplateLoads(unittest.TestCase):
    def test_the_step_runner_accepts_the_template(self):
        env, steps = run_template.load(TEMPLATE.read_text(encoding="utf-8"))
        self.assertIn("start-app", [s.get("id") for s in steps])
        for name in ("EVALUP_REPO_URL", "EVALUP_REF", "APP_HEALTH_URL"):
            self.assertIn(name, env)
        runs = [s["run"] for s in steps if "run" in s]
        self.assertTrue(any("gate.py" in r and "--baseline" in r
                            for r in runs))

    def test_the_suite_is_validated_before_it_is_planned(self):
        """#25: validate_cases.py is what refuses a gating case on a rule
        no human confirmed; a step that plans first lets it gate. It runs
        under `bash -e`, so its exit 1 stops the step."""
        _, steps = run_template.load(TEMPLATE.read_text(encoding="utf-8"))
        run = next(s["run"] for s in steps if "make_plan.py" in s.get("run", ""))
        order = [run.find(name) for name in
                 ("convert_suite.py", "validate_cases.py", "make_plan.py")]
        self.assertTrue(all(i >= 0 for i in order), order)
        self.assertEqual(order, sorted(order))
        call = run[order[1]:order[2]]
        for flag in ("--cases", "--capabilities", "--profile"):
            self.assertIn(f'{flag} "$RUNNER_TEMP/converted.json"', call)
        self.assertNotIn("||", call)

    def test_ci_yml_parses_and_ci_template_is_a_job(self):
        doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
        self.assertIn("ci-template", doc["jobs"])
        self.assertIn("ci-template", doc["jobs"]["ci-ok"]["needs"])

    def refused(self, step, shell="bash"):
        doc = {"defaults": {"run": {"shell": shell}},
               "jobs": {"j": {"steps": [step]}}}
        with self.assertRaises(run_template.TemplateError):
            run_template.load(yaml.safe_dump(doc))

    def test_what_it_cannot_run_faithfully_is_refused(self):
        self.refused({"run": "echo ${{ github.ref }}"})
        self.refused({"run": "echo hi", "if": "always()"})
        self.refused({"run": "echo hi"}, shell="sh")
        self.refused({"name": "neither"})


class TestEnvFile(unittest.TestCase):
    def test_both_github_env_forms(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "env"
            path.write_text("A=1=2\nB<<EOF\nx\ny\nEOF\nC=\n", encoding="utf-8")
            self.assertEqual(run_template.read_env_file(path),
                             {"A": "1=2", "B": "x\ny", "C": ""})


if __name__ == "__main__":
    unittest.main()
