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

# profile.yaml's `plain-text` rule as the check every case carries.
PLAIN_TEXT = r"/(?m)^[ \t]*([-*+\u2022]|\d+[.)]|#+)[ \t]|\*\*/"

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

# The app's system prompt, as discover would find it. The fixture is
# rule-based, so nothing reads it: it is the cited source of profile.yaml's
# rules, the way a real prompt's rule is cited (issue #52), and every answer
# below obeys it. It is assembled from fragments, one per line, the way real
# apps build theirs (#25): a rule's `source:` is its fragment's file:line,
# never "the prompt", and TestBusinessRules checks each quote is on it. The
# last fragment disagrees with examples/quickstart/docs/billing-policy.md
# (14 days against 30) on purpose: that is the planted contradiction.
SYSTEM_PROMPT_FRAGMENTS = (
    "You answer questions about invoices and shifts, and you are read-only.",
    "Never state a figure that is not in a tool result.",
    "Reply in plain text: no Markdown, and no line starting with a bullet or a number.",
    "Invoices can be disputed within 14 days of the invoice date.",
)
SYSTEM_PROMPT = " ".join(SYSTEM_PROMPT_FRAGMENTS)

ANSWERS = [
    # (matcher, status, domain, answer, data)
    (re.compile(r"\bdelete|remove|cancel\b", re.I), 200, "invoices",
     "I can't do that -- this assistant is read-only. I can look invoice "
     "INV-1200 up for you instead.", {}),
    (re.compile(r"invoice|spend|billing", re.I), 200, "invoices",
     "You spent $412.50 on invoices in May 2026, across 4 invoices.",
     # `invoices` grounds the answer's "4": the rubric's no_unsupported_claim
     # asks that every figure in it be in the response data.
     {"value": 412.5, "invoices": 4}),
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
        self._send(status, {"message": self.server.render(answer),
                            "domain": domain,
                            "data": data})


class HelpdeskApp:
    """A rule-based two-domain assistant. Deterministic on purpose: an example
    whose expected answers depend on a model is an example that fails for
    reasons that have nothing to do with the harness."""

    def __init__(self, port=0):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                                      _Handler)
        self.server.seen = []
        # A hook on the answer text, so a test can make the app break a
        # rule it otherwise keeps (the bulleted answer of issue #52).
        self.server.render = lambda text: text
        # poll_interval: shutdown() waits for serve_forever's next poll,
        # 0.5 s by default -- paid by every test that closes a stub.
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01},
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

    def test_the_plain_text_regex_catches_markup_and_spares_prose(self):
        """What the pattern is for, branch by branch, and the near-misses
        a plain-text answer really contains (a negative, a price, a dash
        mid-sentence) that it must leave alone."""
        pattern = re.compile(PLAIN_TEXT[1:-1])
        for text in ("- a", "* a", "+ a", "\u2022 a", "  - nested",
                     "1. a", "2) a", "# a", "### a", "x **bold** y",
                     "intro:\n- a"):
            with self.subTest(caught=text):
                self.assertIsNotNone(pattern.search(text))
        # Known false positive, said in test-generator rule 8: "# of" at a
        # line start reads as a heading.
        self.assertIsNotNone(pattern.search("# of nurses: 7"))
        for text in ("-5 degrees tonight", "You spent $412.50.",
                     "seven nurses - all on nights", "#1 priority",
                     "2026 was busy", "a * b"):
            with self.subTest(spared=text):
                self.assertIsNone(pattern.search(text))

    def test_the_answer_wide_rule_is_checked_on_every_case(self):
        """generate/SKILL.md SS1's rules row: an answer-wide formatting rule
        goes on every case, not one, and with the regex the skill names --
        so the pattern a user copies is the one this example proves."""
        skill = (ROOT / "skills" / "generate" / "SKILL.md").read_text(
            encoding="utf-8")
        self.assertIn("`'" + PLAIN_TEXT + "'`", skill)
        profile = (STATE / "profile.yaml").read_text(encoding="utf-8")
        self.assertIn("- id: plain-text", profile)
        for case in load_converted()["cases"]:
            with self.subTest(case=case["id"]):
                self.assertIn(PLAIN_TEXT,
                              case["expect"]["answer"]["must_not_contain"])

    def test_the_suite_is_clean_under_strict(self):
        """Not "zero errors" -- zero FINDINGS. The example is the one suite in
        this repo that has no excuse for a WARN, and --strict is what makes a
        new warning code fail here the day it is added rather than the day
        somebody reads the example and copies the defect."""
        caps = pathlib.Path(self.tmp_caps())
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR),
             "--cases", str(EXAMPLE / "converted.json"),
             "--capabilities", str(caps), "--strict",
             # The profile's rules and risks (#25): the combined document
             # itself, as the CI recipe and demo.py pass it -- its `profile`
             # key is unwrapped, so the traceability check really runs.
             "--profile", str(EXAMPLE / "converted.json"),
             # converted.json carries the rubric too (docs/judge-run.md
             # SS14), so the criteria form and the case's pin are linted.
             "--rubrics", str(EXAMPLE / "converted.json")],
            capture_output=True, text=True)
        payload = json.loads(proc.stdout)
        self.assertEqual(
            (payload["error_count"], payload["warn_count"]), (0, 0),
            "the shipped example no longer validates:\n" +
            "\n".join("{severity} {code} {case_id}: {message}".format(**f)
                      for f in payload["findings"]))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(payload["summary"]["traceability"]["gaps"],
                         ["rule dispute-window has no case"])

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


class TestBusinessRules(unittest.TestCase):
    """#25's acceptance, on the example: rules recorded with quotes and
    sources, every case linked to a rule or a risk, a rule with no case
    reported as a gap, a docs-vs-code contradiction reported, and an
    unconfirmed rule unable to gate. Rules are linked, never scored."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.converted = load_converted()
        self.rules = {r["id"]: r for r in
                      self.converted["profile"]["business_rules"]}

    def lint(self, cases=None, *extra):
        suite = self.tmp / "suite.json"
        suite.write_text(json.dumps(cases if cases is not None
                                    else self.converted["cases"]),
                         encoding="utf-8")
        caps = self.tmp / "caps.json"
        caps.write_text(json.dumps(self.converted["capability_matrix"]),
                        encoding="utf-8")
        profile = self.tmp / "profile.json"
        profile.write_text(json.dumps(self.converted["profile"]),
                           encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(VALIDATOR), "--cases", str(suite),
             "--capabilities", str(caps), "--profile", str(profile),
             *extra], capture_output=True, text=True)
        return proc.returncode, json.loads(proc.stdout)

    def quoted_text(self, source):
        """The text a rule's source points at: one line for `path:line`,
        the section's body for `path §Section`. Paths are from the plugin
        root, as profile.yaml says."""
        if " §" in source:
            path, section = source.split(" §", 1)
            text = (ROOT / path).read_text(encoding="utf-8")
            body = text.split("## " + section + "\n", 1)
            self.assertEqual(len(body), 2, f"{path} has no ## {section}")
            return body[1].split("\n## ", 1)[0]
        path, line = source.rsplit(":", 1)
        return (ROOT / path).read_text(encoding="utf-8").splitlines()[
            int(line) - 1]

    def test_every_rule_is_recorded_with_its_quote_source_date_status(self):
        self.assertEqual(sorted(self.rules), ["dispute-window",
                                              "grounded-only", "plain-text",
                                              "read-only"])
        for rid, rule in self.rules.items():
            with self.subTest(rule=rid):
                for key in ("rule", "quote", "source", "date", "status"):
                    self.assertTrue(rule.get(key), key)
                self.assertRegex(rule["date"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertIn(rule["status"], ("confirmed", "inferred"))
                # The quote is VERBATIM at the source it names.
                self.assertIn(rule["quote"],
                              self.quoted_text(rule["source"]))

    def test_a_code_rule_cites_its_prompt_fragment(self):
        """A prompt built from fragments has no single "the prompt": each
        code rule's source is its fragment's line."""
        for rid in ("read-only", "grounded-only", "plain-text"):
            source = self.rules[rid]["source"]
            self.assertRegex(source, r"^tests/test_example\.py:\d+$")
            self.assertIn(self.quoted_text(source).strip().strip('",'),
                          SYSTEM_PROMPT_FRAGMENTS)

    def test_every_case_covers_a_rule_or_a_risk(self):
        known = set(self.rules) | {
            r["id"] for r in self.converted["profile"]["risks"]}
        for case in self.converted["cases"]:
            with self.subTest(case=case["id"]):
                self.assertTrue(case.get("covers"))
                self.assertLessEqual(set(case["covers"]), known)
        rc, out = self.lint()
        self.assertEqual(rc, 0, out["findings"])
        self.assertEqual(
            out["summary"]["traceability"]["cases_covering_nothing"], [])

    def test_a_rule_with_no_case_is_reported_as_a_gap(self):
        rc, out = self.lint(None, "--strict")
        self.assertEqual((rc, out["error_count"], out["warn_count"]),
                         (0, 0, 0))
        trace = out["summary"]["traceability"]
        self.assertEqual(trace["gaps"], ["rule dispute-window has no case"])
        self.assertEqual(trace["rules"]["dispute-window"]["cases"], [])
        # A case dropped from the suite opens a gap on what it alone covered.
        rest = [c for c in self.converted["cases"] if c["id"] != "c-6c1f82ab"]
        rc, out = self.lint(rest)
        self.assertIn("rule read-only has no case",
                      out["summary"]["traceability"]["gaps"])

    def test_the_planted_contradiction_is_reported(self):
        """The policy says 30 days, the prompt's fragment says 14: the rule
        carries the contradiction, findings.md reports both sides with their
        sources, and the validator lists it."""
        rule = self.rules["dispute-window"]
        self.assertIn("30 days", self.quoted_text(rule["source"]))
        code_side = re.search(r"tests/test_example\.py:\d+",
                              rule["contradiction"]).group(0)
        fragment = self.quoted_text(code_side)
        self.assertIn("14 days", fragment)
        self.assertIn(fragment.strip().strip('",'), SYSTEM_PROMPT_FRAGMENTS)
        findings = (STATE / "findings.md").read_text(encoding="utf-8")
        for needle in (rule["quote"], rule["source"].split(" §")[0],
                       code_side, "Invoices can be disputed within 14 days"):
            self.assertIn(needle, " ".join(findings.split()))
        self.assertEqual(rule["status"], "inferred")
        rc, out = self.lint()
        self.assertEqual(out["summary"]["traceability"]["contradictions"],
                         ["dispute-window"])

    def test_an_unconfirmed_rule_never_gates(self):
        self.assertEqual(
            {rid for rid, r in self.rules.items()
             if r["status"] != "confirmed"}, {"dispute-window"})
        cases = json.loads(json.dumps(self.converted["cases"]))
        case = next(c for c in cases if c["id"] == "c-4f2a91c7")
        case["covers"].append("dispute-window")
        self.assertIs(case["gating"], True)
        rc, out = self.lint(cases)
        self.assertEqual(rc, 1)
        self.assertEqual([(f["case_id"], f["code"]) for f in out["findings"]
                          if f["severity"] == "ERROR"],
                         [("c-4f2a91c7", "unconfirmed_rule_gates")])
        case["gating"] = False
        rc, out = self.lint(cases)
        self.assertEqual(rc, 0, out["findings"])
        # Every shipped gating case covers confirmed rules (or risks) only.
        for case in self.converted["cases"]:
            for rid in case["covers"]:
                if rid in self.rules:
                    self.assertEqual(self.rules[rid]["status"], "confirmed")


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
        profile = self.normalize(self.load(STATE / "profile.yaml"))
        for key in ("schema_version", "business_rules", "risks"):
            self.assertEqual(profile[key], converted["profile"][key], key)


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

    def test_a_bulleted_answer_fails_the_plain_text_rule_on_every_case(self):
        """Issue #52's acceptance check. The prompt plants a plain-text rule
        and profile.yaml records it; generate puts its check on every case,
        because it binds every answer. The same app, answering the same
        facts as a bulleted list, must fail every case, and on that check
        alone: every other check still passes, so nothing else explains it.
        Before #52 a bulleted answer passed, because no case checked it."""
        self.app.server.render = lambda text: "Here you go:\n- " + text
        self.invoke(self.build_plan())
        summary = self.read("results.json")["summary"]
        self.assertEqual((summary["n"], summary["failures"]), (6, 6))
        for case in self.converted["cases"]:
            with self.subTest(case=case["id"]):
                layers = self.read("cases", case["id"],
                                   "verdict.json")["layers"]
                self.assertEqual(layers["answer"]["verdict"], "fail")
                failed = [c["entry"] for c in layers["answer"]["checks"]
                          if c["status"] != "pass"]
                self.assertEqual(failed, [PLAIN_TEXT])
                for name in ("http", "routing", "execution"):
                    if name in layers and layers[name]["verdict"] != "n/a":
                        self.assertEqual(layers[name]["verdict"], "pass",
                                         name)


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
        # The validator runs first, as in the CI recipe (ci-gate.md).
        self.assertIn("validate_cases.py: 0 errors, 0 warnings; gaps: rule "
                      "dispute-window has no case", proc.stdout)

    def test_the_judge_pass_shows_a_provisional_pass_and_moves_no_gate(self):
        """docs/judge-run.md SS14: the committed fixture replies go through
        plan, record and merge, without Claude. The case comes out a
        PROVISIONAL pass with its quotes, and the gate line is the same one
        the run printed without the pass."""
        plain = self.demo()
        proc = self.demo("--judge-replies", str(EXAMPLE / "judge-replies"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("c-4f2a91c7  provisional pass", proc.stdout)
        self.assertIn('states_the_amount: pass  "You spent $412.50"',
                      proc.stdout)
        self.assertIn("calls: 1 cases + 2 self-test", proc.stdout)
        gate = [line for line in plain.stdout.splitlines()
                if ": PASS" in line or "cases:" in line]
        for line in gate:
            self.assertIn(line.split(": ", 1)[1], proc.stdout)

    def test_a_broken_expectation_closes_the_gate(self):
        proc = self.demo("--break")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("<- failed: answer", proc.stdout)
        self.assertIn("gate closed: 1 gating failure(s)", proc.stdout)

    def test_serve_answers_until_sigterm(self):
        """--serve is the app the CI template's self-test starts
        (tests/run_template.py): it answers the adapter's health check and
        chat endpoint, and SIGTERM stops it cleanly."""
        import urllib.request
        proc = subprocess.Popen(
            [sys.executable, str(EXAMPLE / "demo.py"), "--serve", "--port",
             "0"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            self.assertIn("serving on http://127.0.0.1:", line)
            url = line.split("serving on ", 1)[1].strip()
            with urllib.request.urlopen(url + "/healthz", timeout=10) as r:
                self.assertEqual(r.status, 200)
            ask = urllib.request.Request(
                url + "/api/chat/ask", method="POST",
                data=json.dumps({"message": "nurses on the night shift?"})
                .encode("utf-8"))
            with urllib.request.urlopen(ask, timeout=10) as r:
                self.assertEqual(json.load(r)["domain"], "shifts")
            proc.terminate()
            self.assertEqual(proc.wait(timeout=10), 0, proc.stderr.read())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
