"""The plugin's worked example, executed rather than asserted about.

A 2026-08 review asked for a conforming end-to-end example. The artifact it
asked to "regenerate" could not be: the field test's app was read-only,
outside this repo, and pinned at a dirty sha, so no rerun could ever reproduce
it. This builds a new one instead, and splits it the way the staleness
argument demands:

  COMMITTED, because a user must be able to READ it before installing anything
  -- examples/quickstart/{.evalup/*.yaml, converted.json,
  plan.template.json}. Every one of them is INPUT: authored by a human, or the
  skill's mechanical conversion of what a human authored.

  GENERATED, because a committed run directory is precisely the artifact that
  rots -- the run itself. This module produces it into a temp dir on every test
  run, against a real HTTP server on a real port, and then re-asserts SS9's
  completeness check on the result.

What keeps each half fresh is a test here, not a habit:
  * the committed inputs are linted by validate_cases.py --strict (zero
    findings, not "zero errors") and fed to the real plan validator;
  * the committed tree listing is diffed against the tree a real run writes;
  * the run can never be stale because it is never stored.

Deliberately NOT exercised, and each for a reason worth knowing: k > 1
(repeats), holdout (a holdout run appends a real line to a real ledger -- an
example must not spend a look), judge calibration (needs a judge and the
score_agreement.py sidecar), run history (needs two comparable runs) and cost
(9c: a trace-less run cannot be priced at all). The example's app is trace-less
by construction, which is what puts tool_selection and trajectory out of reach
too. See examples/quickstart/README.md, which says all of this in prose.
"""

import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "quickstart"
STATE = EXAMPLE / ".evalup"
RUNNER = ROOT / "scripts" / "run_cases.py"
VALIDATOR = ROOT / "scripts" / "validate_cases.py"

RUN_ID = "smoke-20260910T120000Z"

# The tree listing the example ships, normalized: the run id is a timestamp, so
# the committed file names it as a placeholder rather than pinning a date that
# only one run could ever have.
TREE_FILE = EXAMPLE / "expected-run-tree.txt"


# -- the app under test ---------------------------------------------------
#
# It lives HERE, not under examples/, on purpose. A server shipped as a script
# would owe the scorers' error contract, --version/--help parity and a slot in
# test_scorers.py's JSON_CLI_SCRIPTS, and it is none of those things: it is a
# target, not part of the harness. Keeping it in tests/ also puts it inside
# all_sources(), so the 3.9 floor guard covers it like every other module.

ANSWERS = [
    # (matcher, status, domain, answer, data)
    (re.compile(r"\bdelete|remove|cancel\b", re.I), 200, "invoices",
     "I can't do that -- this assistant is read-only. I can look invoice "
     "INV-1200 up for you instead.", {}),
    (re.compile(r"invoice|spend|billing", re.I), 200, "invoices",
     "You spent $412.50 on invoices in May 2026, across 4 invoices.",
     {"value": 412.5}),
    (re.compile(r"surgeon", re.I), 200, "shifts",
     "There are no surgeons on the night shift.", {"value": 0}),
    (re.compile(r"nurse|shift|roster|headcount", re.I), 200, "shifts",
     "There are 7 nurses on the night shift.", {"value": 7}),
]
OUT_OF_SCOPE = (400, "none",
                "I can only help with invoices and shifts.", {})


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            message = (json.loads(raw) or {}).get("message") or ""
        except ValueError:
            message = ""
        self.server.seen.append(message)
        status, domain, answer, data = OUT_OF_SCOPE
        for pattern, st, dom, text, payload in ANSWERS:
            if pattern.search(message):
                status, domain, answer, data = st, dom, text, payload
                break
        self._send(status, {"message": answer, "domain": domain,
                            "data": data})


class HelpdeskApp:
    """A rule-based two-domain assistant. Deterministic on purpose: an example
    whose expected answers depend on a model is an example that fails for
    reasons that have nothing to do with the harness."""

    def __init__(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                      _Handler)
        self.server.seen = []
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def load_converted():
    return json.loads((EXAMPLE / "converted.json").read_text(encoding="utf-8"))


def strip_comments(doc):
    """`//` keys are documentation. SS2 makes any unknown top-level key exit 2,
    so they must come out before the plan is a plan -- which is itself worth
    demonstrating, and TestPlanTemplate does."""
    return {k: v for k, v in doc.items() if not k.startswith("//")}


def run_tree(directory):
    """Every file under a run directory, POSIX-relative, sorted."""
    return sorted(p.relative_to(directory).as_posix()
                  for p in directory.rglob("*") if p.is_file())


class ExampleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / ".evalup"
        (self.state / "reports").mkdir(parents=True)
        self.app = HelpdeskApp()
        self.addCleanup(self.app.close)
        self.converted = load_converted()

    def build_plan(self, **overrides):
        """Exactly the substitution run/SKILL.md SS1 performs: five values the
        template cannot commit, and nothing else."""
        plan = strip_comments(json.loads(
            (EXAMPLE / "plan.template.json").read_text(encoding="utf-8")))
        adapter = json.loads(json.dumps(self.converted["adapter"]))
        adapter["invocation"]["base_url"] = self.app.base_url
        plan["run_id"] = RUN_ID
        plan["paths"]["scripts_dir"] = str(ROOT / "scripts")
        plan["paths"]["state_dir"] = str(self.state)
        plan["adapter"] = adapter
        plan["capability_matrix"] = self.converted["capability_matrix"]
        plan["cases"] = self.converted["cases"]
        plan.update(overrides)
        return plan

    def invoke(self, plan, extra_argv=()):
        plan_path = self.tmp / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        out = self.state / "reports" / plan["run_id"]
        argv = [sys.executable, str(RUNNER), "--plan", str(plan_path),
                "--out", str(out), *extra_argv]
        proc = subprocess.run(argv, capture_output=True, text=True,
                              env=dict(os.environ,
                                       HELPDESK_BASE_URL=self.app.base_url))
        try:
            payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        except ValueError:
            payload = None
        return proc.returncode, payload, proc

    def verify(self, directory):
        proc = subprocess.run(
            [sys.executable, str(RUNNER), "--verify", str(directory)],
            capture_output=True, text=True)
        try:
            payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        except ValueError:
            payload = None
        return proc.returncode, payload, proc

    def out_dir(self):
        return self.state / "reports" / RUN_ID

    def read(self, *parts):
        return json.loads(self.out_dir().joinpath(*parts)
                          .read_text(encoding="utf-8"))


# -- the committed half ---------------------------------------------------

class TestCommittedInputsConform(unittest.TestCase):
    """The half that can rot. Each of these is the check that notices."""

    def test_the_suite_is_clean_under_strict(self):
        """Not "zero errors" -- zero FINDINGS. The example is the one suite in
        this repo that has no excuse for a WARN, and --strict is what makes a
        new warning code fail here the day it is added rather than the day
        somebody reads the example and copies the defect."""
        caps = pathlib.Path(self.tmp_caps())
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR),
             "--cases", str(EXAMPLE / "converted.json"),
             "--capabilities", str(caps), "--strict"],
            capture_output=True, text=True)
        payload = json.loads(proc.stdout)
        self.assertEqual(
            (payload["error_count"], payload["warn_count"]), (0, 0),
            "the shipped example no longer validates:\n" +
            "\n".join("{severity} {code} {case_id}: {message}".format(**f)
                      for f in payload["findings"]))
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def tmp_caps(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "caps.json"
        path.write_text(json.dumps(load_converted()["capability_matrix"]),
                        encoding="utf-8")
        return path

    def test_every_case_file_is_in_the_conversion_and_nothing_else_is(self):
        """The stdlib drift guard. Nothing in this repo parses YAML -- by
        design, since the runner is stdlib-only -- so the strongest check that
        runs on a bare interpreter is that the two sides describe the same set
        of cases. A case added to one side and not the other is the drift that
        actually happens; TestConversionIsFaithful catches the rest wherever
        PyYAML is installed."""
        on_disk = sorted(p.stem for p in (STATE / "datasets").glob("c-*.yaml"))
        converted = sorted(c["id"] for c in load_converted()["cases"])
        self.assertEqual(on_disk, converted)
        self.assertEqual(len(on_disk), len(set(on_disk)), "duplicate case id")
        for case_id in on_disk:
            text = (STATE / "datasets" / (case_id + ".yaml")).read_text(
                encoding="utf-8")
            self.assertIn("id: " + case_id, text)

    def test_case_ids_are_opaque(self):
        """generate/SKILL.md SS4: `c-<hash8>`, encoding neither unit nor
        category. The shipped field-test suite violates exactly this rule
        (`units-happy-54f8f914`) and no validator code catches it -- so the
        example's own ids are checked here, where a copied-in id would fail."""
        for case in load_converted()["cases"]:
            self.assertRegex(case["id"], r"^c-[0-9a-f]{8}$")
            self.assertNotIn(case["unit"], case["id"])
            self.assertNotIn(case["category"], case["id"])

    def test_splits_are_a_field_and_not_a_directory(self):
        """Steps 0-2 made `split` a field. A directory named after a split is
        the pre-2026-08 layout, and it is why every field-test case reports
        missing_split."""
        subdirs = [p.name for p in (STATE / "datasets").iterdir() if p.is_dir()]
        self.assertEqual(subdirs, [], "splits are a field, not a directory")
        for case in load_converted()["cases"]:
            self.assertIn("smoke", case["split"])

    def test_no_case_is_accepted_by_a_machine(self):
        """generate/SS3 calls a generator-stamped `accepted` worse than
        `pending`. These cases were hand-authored and hand-checked, and the
        review block says so with a person's name."""
        for case in load_converted()["cases"]:
            review = case["review"]
            self.assertEqual(review["status"], "accepted")
            self.assertNotRegex(review["by"],
                                r"(?i)^(generate|test-generator|auto|machine)")


class TestConversionIsFaithful(unittest.TestCase):
    """The deep check, where a YAML parser exists. It is SKIPPED rather than
    required because the blessed commands install no PyYAML and the harness
    must keep running on a bare interpreter -- so this strengthens the guard
    above on a dev machine and never becomes a dependency."""

    def setUp(self):
        try:
            import yaml
        except ImportError:  # pragma: no cover - depends on the environment
            self.skipTest("PyYAML not installed; the stdlib pairing guard in "
                          "TestCommittedInputsConform still ran")
        self.yaml = yaml

    def load(self, path):
        return self.yaml.safe_load(path.read_text(encoding="utf-8"))

    def normalize(self, value):
        """dates round-trip through the skill as ISO strings, which is what a
        YAML->JSON conversion produces and what the runner sees."""
        return json.loads(json.dumps(value, default=str))

    def test_adapter_and_matrix_and_cases_match_their_yaml(self):
        converted = load_converted()
        self.assertEqual(self.normalize(self.load(STATE / "adapter.yaml")),
                         converted["adapter"])
        self.assertEqual(
            self.normalize(self.load(STATE / "profile.yaml")
                           ["capability_matrix"]),
            converted["capability_matrix"])
        cases = [self.normalize(self.load(p))
                 for p in sorted((STATE / "datasets").glob("c-*.yaml"))]
        self.assertEqual(cases, converted["cases"])


class TestPlanTemplate(ExampleCase):
    def test_the_template_is_refused_until_its_comments_come_out(self):
        """SS2: an unknown top-level key is exit 2, so a typo cannot silently
        disable a layer. The `//` documentation key is subject to that rule
        like anything else, and a reader who copies the template verbatim
        should find out immediately rather than run a plan they did not mean."""
        plan = self.build_plan()
        plan["//"] = ["a comment"]
        rc, payload, proc = self.invoke(plan)
        self.assertEqual(rc, 2, proc.stdout + proc.stderr)
        self.assertIn("//", payload["error"])

    def test_the_five_substituted_values_are_the_only_placeholders(self):
        """A sixth placeholder that nothing substitutes would reach the runner
        as a literal `<ANGLE_BRACKET>` string -- valid JSON, and wrong."""
        raw = strip_comments(json.loads(
            (EXAMPLE / "plan.template.json").read_text(encoding="utf-8")))
        found = set(re.findall(r"<[A-Z_]+>", json.dumps(raw)))
        self.assertEqual(found, {"<RUN_ID>", "<PLUGIN_ROOT>", "<STATE_DIR>",
                                 "<ADAPTER_YAML_AS_JSON>",
                                 "<PROFILE_CAPABILITY_MATRIX>",
                                 "<SUITE_JSON_CASES>"})
        # `<uuid>`, `<user turn>` and `<answer>` are the ADAPTER's own request
        # and response placeholders (adapter-contract.md), substituted by the
        # runner rather than by the skill -- so the pattern is deliberately
        # narrow, and only SHOUTING placeholders are the template's.
        self.assertEqual(re.findall(r"<[A-Z_]+>",
                                    json.dumps(self.build_plan())), [],
                         "a plan placeholder survived into the built plan")


# -- the generated half ---------------------------------------------------

class TestTheExampleRuns(ExampleCase):
    """End-to-end, and the phrase is meant literally: a real socket, the real
    runner, the real scorers, and SS9's completeness check on what came out."""

    def test_the_smoke_run_passes_and_verifies(self):
        rc, _, proc = self.invoke(self.build_plan())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        # A clean run prints NOTHING: stdout carries the machine-readable
        # {"error": ...} payload and nothing else, so the durable record is
        # results.json (SS10) -- which is also the file a CI gate reads.
        self.assertEqual(proc.stdout.strip(), "")
        summary = self.read("results.json")["summary"]
        self.assertEqual(summary["status"], "ok")
        self.assertEqual((summary["n"], summary["attempted"],
                          summary["passes"], summary["failures"],
                          summary["gating_failures"], summary["skipped"]),
                         (6, 6, 6, 0, 0, 0))
        self.assertEqual(summary["missing_artifacts"], [])

        # SS9's own re-assertion, with months-later semantics: --verify runs
        # ALONE, takes no plan and no --out, and derives every check off the
        # tree. That is what makes it runnable on a run directory nobody has
        # the plan for any more -- including this one.
        rc, payload, proc = self.verify(self.out_dir())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        self.assertEqual(payload["missing_artifacts"], [])

    def test_the_app_saw_one_request_per_case(self):
        """k is 1, so six cases are six calls. A case scored without a call is
        the failure this harness exists to make impossible."""
        self.invoke(self.build_plan())
        self.assertEqual(len(self.app.server.seen), 6)
        self.assertEqual(self.read("results.json")["summary"]["attempted"], 6)

    def test_four_layers_score_and_the_disabled_ones_say_why(self):
        """The point of the example: on a trace-less app, four layers still
        produce a verdict and the rest are `unscorable` carrying the matrix's
        own blocked_by -- an honest gap, never a pass."""
        self.invoke(self.build_plan())
        verdict = self.read("cases", "c-4f2a91c7", "verdict.json")
        layers = {name: row["verdict"]
                  for name, row in verdict["layers"].items()}
        self.assertEqual(layers["http"], "pass")
        self.assertEqual(layers["routing"], "pass")
        self.assertEqual(layers["execution"], "pass")
        self.assertEqual(layers["answer"], "pass")
        # `n/a` and `unscorable` are different statements and the example
        # shows both. This case asserts no tools, so the trajectory layers are
        # `n/a` -- not applicable. A case that DID assert them would get
        # `unscorable` carrying the matrix's own blocked_by, which is what
        # test_a_disabled_layer_is_unscorable_not_absent pins below.
        for name in ("trajectory", "tool_selection", "authz"):
            self.assertEqual(layers[name], "n/a")

    def test_a_disabled_layer_is_unscorable_and_copies_the_matrix_reason(self):
        """The distinction the layer table is built on: `unscorable` means
        somebody turned the layer off and said why, and the reason is COPIED
        from the capability matrix rather than composed by the runner -- so the
        report and the matrix can never disagree about that why."""
        plan = self.build_plan()
        case = [c for c in plan["cases"] if c["id"] == "c-4f2a91c7"][0]
        case["expect"]["tools"] = {"subset": ["get_invoice"]}
        self.invoke(plan)
        # `expect.tools` triggers the TRAJECTORY layer, not tool_selection --
        # SS5's table. validate_cases.py once believed the inverse; its
        # expect-key -> layer map now agrees with the runner, so this is a
        # statement of the contract and no longer a note about a divergence.
        row = self.read("cases", "c-4f2a91c7",
                        "verdict.json")["layers"]["trajectory"]
        self.assertEqual(row["verdict"], "unscorable")
        self.assertEqual(
            row["blocked_by"],
            self.converted["capability_matrix"]["trajectory"]["blocked_by"])

    def test_layer_routing_scores_routing_and_nothing_else(self):
        """make_plan.py and the runner, joined: each half was tested and the
        seam was not. `--layer routing` wrote `answer_quality: {enabled:
        false}`, the runner looked for `answer`, and the answer layer went on
        scoring -- and could fail -- a run that had asked for routing only."""
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "make_plan.py"),
             str(EXAMPLE / "converted.json"), "--mode", "smoke",
             "--state-dir", str(self.state), "--layer", "routing"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        plan = json.loads(proc.stdout)
        rc, payload, run = self.invoke(plan)
        self.assertEqual(rc, 0, run.stdout + run.stderr)
        verdict = json.loads(
            (self.state / "reports" / plan["run_id"] / "cases" / "c-4f2a91c7"
             / "verdict.json").read_text(encoding="utf-8"))
        layers = verdict["layers"]
        self.assertEqual(layers["routing"]["verdict"], "pass")
        for name in ("answer", "execution"):
            self.assertEqual(layers[name],
                             {"layer": name, "verdict": "unscorable",
                              "blocked_by": "--layer routing"}, name)
        # `http` has no matrix key, so the liveness row is still there.
        self.assertEqual(layers["http"]["verdict"], "pass")
        self.assertEqual(verdict["verdict"], "pass")

    def test_the_out_of_scope_case_routes_to_the_canonical_oos_label(self):
        """`none` is the app's route name; `__oos__` is score_routing.py's
        internal label. scoring.oos_route is the mapping between them, and it
        is what makes the OOS metrics appear at all."""
        self.invoke(self.build_plan())
        report = self.read("routing_report.json")
        self.assertIn("oos", json.dumps(report).lower())
        verdict = self.read("cases", "c-9e05b3f4", "verdict.json")
        self.assertEqual(verdict["layers"]["http"]["verdict"], "pass")
        self.assertEqual(verdict["layers"]["routing"]["verdict"], "pass")

    def test_the_tree_matches_the_listing_the_example_ships(self):
        """The one committed thing derived from the run. It records WHICH of
        SS6's conditional files this particular run produced -- the generic
        tree is in docs/runner-contract.md SS6 and is checked against the code
        there; what an example adds is which `# only when:` conditions actually
        fired."""
        rc, _, proc = self.invoke(self.build_plan())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        actual = run_tree(self.out_dir())
        expected = [line for line in TREE_FILE.read_text(encoding="utf-8")
                    .splitlines()
                    if line.strip() and not line.startswith("#")]
        self.assertEqual(actual, expected,
                         "the run's tree and examples/quickstart/"
                         "expected-run-tree.txt disagree")

    def test_no_holdout_look_is_spent(self):
        """The ledger is a LINE COUNT, and a look is a real cost. An example
        that ran a holdout mode would append to a real ledger every test run,
        so this one declares holdout_ledger: null and never selects one."""
        plan = self.build_plan()
        self.assertIsNone(plan["paths"]["holdout_ledger"])
        self.assertEqual(plan["mode"], "smoke")
        self.invoke(plan)
        self.assertFalse((self.state / "datasets").exists())

    def test_the_judged_gate_stays_shut_without_a_calibration_sidecar(self):
        """Step 9a: `judge.status` is necessary, not sufficient. The example
        ships no judge, so the judged layer must report a reason rather than a
        verdict -- and never an exit code."""
        rc, _, proc = self.invoke(self.build_plan())
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        verdict = self.read("cases", "c-4f2a91c7", "verdict.json")
        judged = verdict["layers"].get("judged")
        self.assertNotIn(judged.get("verdict"), ("pass", "fail"))


class TestDemo(unittest.TestCase):
    """examples/quickstart/demo.py is the example a HUMAN runs. It sits outside
    scripts/ and tests/, so nothing else executes it -- including the 3.9 floor
    run, which only reaches it through here."""

    def demo(self, *argv):
        return subprocess.run(
            [sys.executable, str(EXAMPLE / "demo.py"), *argv],
            capture_output=True, text=True, timeout=120)

    def test_the_shipped_example_is_green_and_exits_0(self):
        proc = self.demo()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS", proc.stdout)
        self.assertIn("6 pass, 0 fail", proc.stdout)

    def test_a_broken_expectation_closes_the_gate(self):
        proc = self.demo("--break")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("<- failed: answer", proc.stdout)
        self.assertIn("gate closed: 1 gating failure(s)", proc.stdout)


if __name__ == "__main__":
    unittest.main()
