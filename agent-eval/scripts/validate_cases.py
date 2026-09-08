#!/usr/bin/env python3
"""Suite-level linter for eval cases: does this dataset assert anything real?

The scorers in this directory each grade ONE case against ONE trace. Nothing
grades the SUITE — and the defects that make an eval suite lie are suite-shaped:
two cases sharing an id (the second silently overwrites the first in every
per-case report), a case whose only assertion sits in a capability layer the
profile has disabled (it runs, it "passes", it graded nothing), an
`expect.http: 200` case that proves the app is alive and calls that a
behavioral result, a case a do-nothing agent passes, a case whose "accepted"
review was stamped by the generator that wrote it. Every one of those inflates
a pass rate with cases that cannot fail. This script finds them before a run
spends tokens on them.

INPUT FORMAT — the deliberate decision:

Cases are authored as YAML (one file per case, see case-format.md). This
harness is stdlib-only on a Python 3.9 floor, and the stdlib has no YAML
parser. The options were (a) vendor a parser, (b) hand-roll a subset parser,
or (c) take JSON and make the caller convert. This script takes JSON.

(b) is the tempting one and it is the wrong one. A partial YAML parser does
not fail loudly on the constructs it does not implement — it MISPARSES them.
Anchors/aliases, block scalars, flow mappings, quoted-vs-bare scalars, `no` as
a bool: each silently yields a different case object than the runner will see.
A linter whose model of the case disagrees with the runner's is worse than no
linter, because it reports findings about cases that do not exist and misses
findings about the ones that do. So there is no --from-yaml flag, and adding
one later means vendoring a real parser, not writing a subset.

The calling skill converts YAML -> JSON with whatever parser it already uses
to LOAD the cases for the run (so the conversion is the runner's own parse,
not a second opinion) and pipes the result here. Accepted shapes:
  - a JSON list of case objects
  - a JSON object wrapping one under {"cases": [...]}
  - JSONL, one case object per line
  - a single case object (linted as a one-case suite)
`--cases -` reads stdin.

WHAT COUNTS AS GRADED (the no_graded_layer check):

A case's expect keys map onto capability layers:
  route, route_acceptable -> routing        result -> execution
  tools                   -> tool_selection authz  -> authz
  args, state             -> trajectory     answer, format -> answer_quality
`expect.http` maps to NOTHING. A status code is a liveness check: it says the
request reached the app, not that the app did the right thing. A case whose
only expectation is `http` is reported http-only and, having no graded layer,
is an ERROR.

With --capabilities (the profile's capability_matrix, {"routing": {"enabled":
false}, ...}), a layer only counts while it is enabled — a suite carried over
from a profile with routing on is full of cases that assert nothing once
routing is off, and they look like passes. Treating every layer as enabled
still catches http-only cases but can never catch those, which is why
--capabilities is REQUIRED: the alternative is --no-capabilities, which says
so out loud, emits capabilities_unchecked, and is rejected under --strict. An
optional flag would have made the most expensive check in this script default
to off.

ERROR vs WARN — the line this script draws:

ERROR = the suite makes a claim that is not backed. A case with no `split` is
in no run and its `holdout` seal rests on nothing; a case with no `test_type`
is counted by no coverage grid; a case with no `template_id` key removes
itself from the clustering the suite's error bars are computed over; an INV
with no parent asserts an invariance against nothing. Each of those makes some
number elsewhere untrue, so each fails the run.

WARN = the suite is thinner than the guidance recommends, which is a budget
judgement its author is allowed to make (no INV/DIR at all, an all-one-off
suite, a skewed category mix). --strict promotes these for a gating CI job.

A "required" field enforced by a warning is not required, so nothing in the
first list warns.

Output: one JSON report on stdout with `findings` (case_id, severity, code,
message), a `summary` object, and counts. Exit 0 when no ERROR finding was
emitted, 1 when any was, 2 on malformed input ({"error": ...} on stdout, the
shared contract in _common.die). Warnings alone never fail; --strict promotes
every WARN to ERROR for a gating CI job.

Usage: validate_cases.py --cases <file.json|->
                         (--capabilities <file.json> | --no-capabilities)
                         [--strict] [--manifest <file.json>]
"""
import argparse
import json
import re
import sys

from _common import HARNESS_VERSION, add_version_flag, die, load_text

CATEGORIES = ("happy", "multistep", "edge", "ambiguous", "oos",
              "adversarial-refusal", "noise")
TEST_TYPES = ("MFT", "INV", "DIR")
ORDER_MODES = ("in_order", "exact", "any_order")
CALL_SCOPES = ("any", "all", "first")
# The four legal splits (generate/SKILL.md §4). `smoke` and `canary` are
# subsets of `full`; `holdout` is mutually exclusive with `full` — that
# exclusivity is what the seal rests on, so it is checked, not assumed.
SPLITS = ("full", "smoke", "holdout", "canary")

# expect key -> the capability layer that grades it. `http` is deliberately
# absent; see the module docstring.
LAYER_OF_EXPECT = {
    "route": "routing",
    "route_acceptable": "routing",
    "tools": "tool_selection",
    "args": "trajectory",
    "state": "trajectory",
    "result": "execution",
    "authz": "authz",
    "answer": "answer_quality",
    "format": "answer_quality",
}
LAYERS = ("routing", "tool_selection", "trajectory", "execution", "authz",
          "answer_quality")

# A review stamped by the thing that generated the case. Anchored: a human
# reviewer named "Generosa" is not a machine.
MACHINE_REVIEWER = re.compile(
    r"^(generate|generate-review|test-generator|auto|machine)", re.I)
# Expectations copied out of the app's own source grade the app against itself:
# the case then encodes what the code DOES, so a bug in that code is baked into
# the ground truth and the suite can never see it.
SOURCE_DERIVED = re.compile(
    r"verified against|read .*\.(cs|py|ts|java|go)\b|"
    r"per the (guard|implementation|source)", re.I)

# The id shape score_authz.py's default recognizer can scope by; kept in
# sync with RECORD_ID_RE / id_prefix() there.
ID_SHAPED = re.compile(r"[A-Za-z]{1,8}[-_]")

CATEGORY_SKEW_THRESHOLD = 0.40
# Below this the share is an artifact of arithmetic, not a skew: in a 3-case
# suite every category present is at least 33% and one of them is always over
# 40%, so firing there would put a warning on every small suite and teach the
# reader to ignore the code.
CATEGORY_SKEW_MIN_CASES = 5
SINGLE_TURN_SUITE_MIN = 10
# Same floor, same reason: under ~10 cases a coverage SHARE is arithmetic
# rather than signal. generate/SKILL.md's budget table puts the metamorphic
# floor at "every template gets >=1 INV; at the smallest budget the highest-
# risk case still gets one" and says to treat the floor as binding and the
# <=25% ceiling as not — so at its own 12-case worked example, zero INV/DIR is
# a real gap.
METAMORPHIC_SUITE_MIN = 10
# A suite where every case declares itself a one-off is the per-case
# template_id rule answered "none" N times: phase 1 of the two-phase flow
# (generate/SKILL.md §2a, tuples before prose) never happened. Lower floor
# than the two above because this one is about the authoring process, not
# about a ratio — 5 straight one-offs is already a habit, not a rounding.
ALL_ONE_OFF_MIN_CASES = 5


def mapping(value):
    """The value if it is an object, else {} — for probing OPTIONAL blocks.

    Deliberately lenient where the scorers are strict: a linter that exits 2 on
    the first off-shape block stops reporting the other 18 findings in the
    suite, and off-shape input is exactly what the author needs the full list
    for. Shape errors that matter are reported as findings instead."""
    return value if isinstance(value, dict) else {}


def is_nonempty_str(value):
    return isinstance(value, str) and value.strip() != ""


class Report:
    """Findings plus the strict-promotion rule, in one place so no call site
    can emit a WARN that --strict forgets to promote."""

    def __init__(self, strict):
        self.strict = strict
        self.findings = []

    def error(self, case_id, code, message):
        self.findings.append({"case_id": case_id, "severity": "ERROR",
                              "code": code, "message": message})

    def warn(self, case_id, code, message):
        self.findings.append({
            "case_id": case_id,
            "severity": "ERROR" if self.strict else "WARN",
            "code": code, "message": message})

    def has_errors(self):
        return any(f["severity"] == "ERROR" for f in self.findings)


def load_cases(path):
    """Read the suite as JSON, then JSONL, then die. Both shapes are accepted
    because the converting skill may stream cases (one per file -> one per
    line) or collect them; guessing wrong should not be the author's problem."""
    text = sys.stdin.read() if path == "-" else load_text(path)
    if not text.strip():
        die(f"bad input: {path}: no cases (empty input)")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as whole_err:
        rows = []
        for lineno, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Not valid as one document and not valid as JSONL: report the
                # whole-document error, which is the one an author who meant to
                # pass JSON can act on.
                die(f"bad input: {path}: {whole_err}")
            if not isinstance(row, dict):
                die(f"bad input: {path}:{lineno}: expected a case object, got "
                    f"{type(row).__name__}")
            rows.append(row)
        doc = rows
    if isinstance(doc, dict):
        # {"cases": [...]} or a single bare case. The default is the wrapper
        # test, not a truthiness test: an explicitly empty `cases` list is a
        # real (and reportable) input, and reading it as a bare case would
        # lint the wrapper object itself.
        doc = doc.get("cases", [doc])
    if not isinstance(doc, list):
        die(f"bad input: {path}: expected a list of case objects, got "
            f"{type(doc).__name__}")
    for i, case in enumerate(doc):
        if not isinstance(case, dict):
            die(f"bad input: {path}: cases[{i}] must be an object, got "
                f"{type(case).__name__}")
    if not doc:
        die(f"bad input: {path}: no cases")
    return doc


def load_enabled_layers(path):
    """The set of layers currently graded, from a profile capability_matrix.

    A layer is enabled unless its entry says otherwise, so a matrix that simply
    omits a layer does not silently disable it — under-reporting a layer as off
    would flag every honest case in it as ungraded."""
    if path is None:
        return set(LAYERS)
    try:
        doc = json.loads(load_text(path))
    except json.JSONDecodeError as e:
        die(f"bad input: {path}: {e}")
    if not isinstance(doc, dict):
        die(f"{path}: expected a JSON object (a capability_matrix), got "
            f"{type(doc).__name__}")
    # PRESENCE, not truthiness: a profile carrying an EMPTY capability_matrix
    # means "nothing is enabled yet", and falling back to the wrapper there
    # would lint the profile's other keys and report every layer enabled —
    # the opposite of what the file says. Only ABSENCE may default.
    if "capability_matrix" in doc:
        doc = mapping(doc.get("capability_matrix"))
    enabled = set()
    for layer in LAYERS:
        entry = doc.get(layer)
        if entry is None:
            enabled.add(layer)
        elif isinstance(entry, dict):
            if entry.get("enabled", True):
                enabled.add(layer)
        elif entry:
            enabled.add(layer)
    return enabled


def asserts_something(value):
    """Whether an expect entry carries a real assertion.

    Emptiness is not the only way to assert nothing. Three shapes all have to
    be rejected here or a case earns a "graded" stamp it has not paid for:
    - absent/empty (`answer: {}`) — the same case as a missing key;
    - FALSY but present (`route: false`, `result: 0`). `false` is what a bare
      YAML `no`/`off` becomes, so this is a boolification artifact rather than
      a deliberate expectation, and counting it graded is a free pass;
    - the wrong TYPE (`args: [x]`, `tools: [a]`). The per-key shape checks
      below report these, but they must also not count toward gradedness —
      otherwise the case is simultaneously malformed and considered covered,
      which is exactly the defect this script exists to catch."""
    if isinstance(value, bool):
        return False
    return not (value is None or value == 0 or value == {}
                or value == [] or value == "")


# The shape each expect key must have to assert anything. A key of the wrong
# type is reported (bad_expect_shape) and never counted as a graded layer.
EXPECT_SHAPE = {
    "tools": dict, "args": dict, "result": dict, "authz": dict,
    "answer": dict, "state": dict, "format": dict,
    "route": str, "route_acceptable": list,
}


def asserted_layers(expect):
    """Every layer this case asserts something in, ignoring `http`."""
    layers = set()
    for key, layer in LAYER_OF_EXPECT.items():
        value = expect.get(key)
        if not asserts_something(value):
            continue
        shape = EXPECT_SHAPE.get(key)
        if shape is not None and not isinstance(value, shape):
            continue
        layers.add(layer)
    return layers


def check_expect_shapes(rep, case_id, expect):
    """Report any expect key whose VALUE is the wrong type. Without this a
    malformed expectation is silently inert: `expect.args: [x]` parses, asserts
    nothing, and (before this check) still marked the trajectory layer graded."""
    for key, shape in sorted(EXPECT_SHAPE.items()):
        value = expect.get(key)
        if value is None or isinstance(value, shape):
            continue
        rep.error(case_id, "bad_expect_shape",
                  f"expect.{key} must be {shape.__name__}, got "
                  f"{type(value).__name__}; as written it asserts nothing and "
                  "would leave the case looking graded while checking nothing")


def check_authz(rep, case_id, expect):
    """`allowed_record_ids` is scored by recognizing id-shaped tokens in the
    tool results and flagging any that is not on the list. score_authz.py's
    default recognizer only sees a letters+separator+digits lead ('INV-1042',
    'e_881'), so an allowlist of UUIDs or integer primary keys gives it no
    prefix to scope by and the check reports `unscorable` — the case runs,
    reports nothing, and reads like it passed. The fix is a run-time flag
    (`--id-pattern`, from the profile's `record_id_pattern`), not an edit to
    the case, so this is a warning about how the case must be RUN.

    Only `allowed_record_ids` is shape-dependent: `forbidden_record_ids` is a
    literal search for each id in the captured results, which works for any id
    shape."""
    authz = mapping(expect.get("authz"))
    allowed = authz.get("allowed_record_ids")
    # An EMPTY allowlist is the strictest one ("no record is in scope") and is
    # scored without prefix scoping at all — it needs no pattern.
    if not isinstance(allowed, list) or not allowed:
        return
    if any(ID_SHAPED.match(str(rid)) for rid in allowed):
        return
    rep.warn(case_id, "unrecognizable_record_ids",
             "expect.authz.allowed_record_ids holds no id-shaped "
             f"(letters[-_]digits) id, e.g. {str(allowed[0])!r}; unless the "
             "run passes --id-pattern (the profile's `record_id_pattern`) the "
             "allowed_record_ids check scores `unscorable` and the case "
             "silently verifies nothing")


def check_tools(rep, case_id, expect):
    tools = mapping(expect.get("tools"))
    mode = tools.get("order_mode")
    if mode is not None and mode not in ORDER_MODES:
        rep.error(case_id, "bad_order_mode",
                  f"expect.tools.order_mode {mode!r} is not one of "
                  f"{', '.join(ORDER_MODES)}; an unrecognized mode is a hard "
                  "error, never a silent fallback to the loosest one")


def check_args(rep, case_id, expect):
    args = mapping(expect.get("args"))
    for tool, spec in sorted(args.items()):
        spec = mapping(spec) if isinstance(spec, dict) else None
        if spec is None:
            rep.error(case_id, "vacuous_args_entry",
                      f"expect.args.{tool} is not an object, so it asserts "
                      "nothing about the call's arguments")
            continue
        scope = spec.get("calls")
        if scope is not None and scope not in CALL_SCOPES:
            rep.error(case_id, "bad_call_scope",
                      f"expect.args.{tool}.calls {scope!r} is not one of "
                      f"{', '.join(CALL_SCOPES)}")
        if not {k: v for k, v in spec.items() if k != "calls"}:
            rep.error(case_id, "vacuous_args_entry",
                      f"expect.args.{tool} asserts nothing once `calls` is "
                      "removed; it would score every trajectory and leave no "
                      "row saying so — use expect.tools to assert only that "
                      "the tool was called")


def check_result(rep, case_id, expect):
    result = expect.get("result")
    if result is None or result == {}:
        return
    result = mapping(result)
    has_scalar, has_rows = "scalar" in result, "rows" in result
    if has_scalar and has_rows:
        rep.error(case_id, "both_scalar_and_rows",
                  "expect.result has both 'scalar' and 'rows'; use exactly one")
    elif not has_scalar and not has_rows:
        rep.error(case_id, "neither_scalar_nor_rows",
                  "expect.result has neither 'scalar' nor 'rows'; use exactly "
                  "one, or omit expect.result entirely")
    if "columns" in result and isinstance(result["columns"], list) \
            and not result["columns"]:
        rep.error(case_id, "empty_columns",
                  "expect.result.columns is empty; zero columns makes every "
                  "row compare equal (a vacuous pass) — omit the key to "
                  "compare the columns the expected rows define")


def check_answer_entries(rep, case_id, expect):
    """must_contain/must_not_contain must be LISTS OF STRINGS.

    A bare string is the dangerous slip and gets its own code: `must_contain:
    "invoice"` is not an error in YAML or JSON, but a string is a sequence of
    CHARACTERS, so a scorer iterating it checks for "i", "n", "v"... and
    `must_not_contain: "zebra"` manufactures a failure on any answer containing
    the letter "a". It must never reach a run."""
    answer = mapping(expect.get("answer"))
    for field in ("must_contain", "must_not_contain"):
        entries = answer.get(field)
        if entries is None:
            continue
        if isinstance(entries, str):
            rep.error(case_id, "string_not_list",
                      f"expect.answer.{field} is a bare string ({entries!r}), "
                      "not a list; iterated as a sequence of characters it "
                      "checks single letters, so must_not_contain would fail "
                      "on nearly any answer — wrap it in a list")
            continue
        if not isinstance(entries, list):
            rep.error(case_id, "string_not_list",
                      f"expect.answer.{field} must be a list of strings, got "
                      f"{type(entries).__name__}")
            continue
        for i, entry in enumerate(entries):
            if isinstance(entry, str):
                continue
            if isinstance(entry, (bool, int, float)):
                rep.error(case_id, "unquoted_bool_or_number",
                          f"expect.answer.{field}[{i}] is a "
                          f"{type(entry).__name__} ({entry!r}), not a string; "
                          "an unquoted YAML scalar is unscorable or matches a "
                          "token the answer never spells — quote it")
            else:
                rep.error(case_id, "unquoted_bool_or_number",
                          f"expect.answer.{field}[{i}] is a "
                          f"{type(entry).__name__}, not a string; only strings "
                          "(plain substring, or /regex/) can be matched")


def check_review(rep, case_id, case):
    review = mapping(case.get("review"))
    status = review.get("status")
    by = review.get("by")
    if status == "accepted" and (not is_nonempty_str(by)
                                 or MACHINE_REVIEWER.match(by.strip())):
        rep.warn(case_id, "machine_accepted",
                 f"review.status is accepted but review.by is {by!r}; only a "
                 "human may set accepted — a case accepted by the generator "
                 "that wrote it has never been reviewed by anyone")
    if case.get("gating") and status in ("pending", "quarantined"):
        rep.warn(case_id, "gating_unreviewed",
                 f"gating is true while review.status is {status!r}; an "
                 "unreviewed case is deciding whether the suite passes")


def check_case(rep, case, all_ids, enabled, index=None):
    case_id = case.get("id") if is_nonempty_str(case.get("id")) else None
    # An id-less case still needs a distinguishable label, or two of them
    # produce two identical findings and the author cannot tell which is which.
    # The list position is the only handle that exists before an id does.
    label = case_id or (f"<no id: cases[{index}]>" if index is not None
                        else "<no id>")
    if case_id is None:
        rep.error(label, "missing_id",
                  "case has no non-empty string `id`; the id is what every "
                  "per-case report, baseline diff, and metamorphic parent "
                  "reference keys on")

    splits = case.get("split")
    if splits is None or splits == []:
        # Absent used to pass silently, which made every split-shaped claim
        # unbacked at once: no run mode selects this case (run-modes.md
        # selects ON THE FIELD), and a case sitting in a holdout/ DIRECTORY
        # is not sealed by anything a script can see — the seal is the
        # `holdout` membership, checked below against `full`.
        rep.error(label, "missing_split",
                  "case declares no `split`; splits are a FIELD, not a "
                  "directory (case-format.md), so a case with none is "
                  "selected by no run mode and, if it was meant to be held "
                  "out, is sealed by nothing")
    elif not isinstance(splits, list):
        rep.error(label, "bad_split",
                  f"split must be a list of {', '.join(SPLITS)}, got "
                  f"{type(splits).__name__}")
    else:
        for name in splits:
            if name not in SPLITS:
                rep.error(label, "bad_split",
                          f"split {name!r} is not one of "
                          f"{', '.join(SPLITS)}; a typo here silently "
                          "drops the case from the run that names it")
        if "holdout" in splits and "full" in splits:
            rep.error(label, "holdout_not_sealed",
                      "case is in both `holdout` and `full`; the seal "
                      "rests on those being mutually exclusive, so a case "
                      "in both is trained on and then measured as if held "
                      "out")

    category = case.get("category")
    if category not in CATEGORIES:
        rep.error(label, "bad_category",
                  f"category {category!r} is not one of "
                  f"{', '.join(CATEGORIES)}")

    test_type = case.get("test_type")
    if test_type is None:
        # The oracle type is the grid's column axis (CheckList, and
        # case-format.md says so). Absent, this case is counted by no column
        # of any coverage claim the suite makes — including the INV/DIR floor
        # checked at suite level, which cannot see a case that declines to say
        # what kind of oracle it has.
        rep.error(label, "missing_test_type",
                  f"case declares no `test_type`; one of "
                  f"{', '.join(TEST_TYPES)} is the ORACLE type and the "
                  "coverage grid's column axis, so a case without it is "
                  "counted by no coverage number this suite reports")
    elif test_type not in TEST_TYPES:
        rep.error(label, "bad_test_type",
                  f"test_type {test_type!r} is not one of "
                  f"{', '.join(TEST_TYPES)}")

    expect = mapping(case.get("expect"))
    layers = asserted_layers(expect)
    graded = layers & enabled
    http_only = not layers and "http" in expect
    if not graded:
        if http_only:
            detail = ("its only expectation is expect.http, which is a "
                      "liveness check (the app answered) and not a behavioral "
                      "assertion")
        elif layers:
            detail = ("every layer it asserts is disabled in the capability "
                      f"matrix ({', '.join(sorted(layers))})")
        else:
            detail = "it asserts nothing under `expect`"
        rep.error(label, "no_graded_layer",
                  f"case grades no enabled layer: {detail}. It runs and it "
                  "passes, but it cannot fail — which inflates the pass rate "
                  "with a case that measures nothing")

    parent = case.get("metamorphic_parent")
    if test_type in ("INV", "DIR") and not is_nonempty_str(parent):
        # Without this, the suite-level INV/DIR floor is satisfiable by
        # typing `test_type: INV` on an ordinary case: the label would buy
        # the coverage credit while the perturbation it names does not exist.
        rep.error(label, "missing_metamorphic_parent",
                  f"test_type is {test_type} but metamorphic_parent is "
                  f"{parent!r}; a perturbation with no parent has no "
                  "expectation to inherit (INV) or to move against (DIR), so "
                  "it asserts nothing while still counting as metamorphic "
                  "coverage")
    if is_nonempty_str(parent):
        if parent == case_id:
            rep.error(label, "self_metamorphic_parent",
                      "metamorphic_parent points at this case itself; an "
                      "invariance asserted against its own parent is vacuous "
                      "— it compares the case to itself and can never fail")
        elif parent not in all_ids:
            rep.error(label, "dangling_metamorphic_parent",
                      f"metamorphic_parent {parent!r} is not the id of any "
                      "case in this input; the invariance it asserts cannot "
                      "be checked")

    # generate/SKILL.md §2a and case-format.md both call this pair REQUIRED,
    # with exactly one escape: a one-off case sets template_id to null AND
    # SAYS SO. So the check is on the KEY's presence, not on its truthiness —
    # absent means the two-phase generation flow was short-circuited and
    # nobody decided anything, `template_id: null` means the author decided.
    # Checking only truthiness is what let a 12-case suite carrying neither
    # key validate clean while both docs called the pair required.
    template_id = case.get("template_id")
    params = case.get("instantiation_params")
    has_params = bool(params) and isinstance(params, dict)
    declared_one_off = "template_id" in case and not is_nonempty_str(template_id)
    if "template_id" not in case:
        rep.error(label, "missing_template_id",
                  "case has no `template_id` key: name the template this case "
                  "was realized from (with instantiation_params), or declare "
                  "the one-off by writing `template_id: null` explicitly. "
                  "Cases sharing a template are not independent samples, so a "
                  "case that never says which template it came from is "
                  "silently dropped from the clustering the suite's error "
                  "bars are computed over")
    if is_nonempty_str(template_id) and not has_params:
        rep.error(label, "template_without_params",
                  f"template_id {template_id!r} is set but "
                  "instantiation_params is missing or empty; nothing "
                  "distinguishes this instantiation from its siblings")
    if has_params and not is_nonempty_str(template_id):
        rep.error(label, "params_without_template",
                  "instantiation_params is non-empty but template_id is null; "
                  "the params cannot be grouped for per-template consistency "
                  "scoring")

    check_expect_shapes(rep, label, expect)
    check_tools(rep, label, expect)
    check_args(rep, label, expect)
    check_result(rep, label, expect)
    check_authz(rep, label, expect)
    check_answer_entries(rep, label, expect)

    if case.get("no_op_expectation") == "pass" \
            and not is_nonempty_str(case.get("no_op_justification")):
        rep.warn(label, "no_op_pass",
                 "no_op_expectation is `pass` with no no_op_justification; a "
                 "case that a do-nothing agent passes is usually vacuous — "
                 "reconsider the case, or write down why this one is the "
                 "exception")

    check_review(rep, label, case)

    notes = case.get("notes")
    if isinstance(notes, str) and SOURCE_DERIVED.search(notes):
        rep.warn(label, "source_derived_expectation",
                 "notes suggest the expectation was derived from the app's "
                 "own source; expectations read out of the implementation "
                 "grade the app against itself, so a bug in that code becomes "
                 "the ground truth and the suite can never see it")

    return {"id": case_id, "category": category, "test_type": test_type,
            "split": case.get("split"), "unit": case.get("unit"),
            "layers": layers, "graded": graded, "http_only": http_only,
            "one_off": declared_one_off}


def check_manifest(rep, cases, records, manifest):
    """Cross-check dataset.yaml's hand-written summary against the case files
    it describes. dataset.yaml is authored prose, not generated, and nothing
    else re-derives it — a hand-edit to a case file (a reclassification, a
    deleted case, a split change, exactly the kind of edit REVIEW.md documents
    happening directly on cases/*.yaml) can leave it stale with nothing to
    say so. This is the same stale-metadata failure the id scheme was
    designed to avoid, one level up, so it gets the same treatment: check it
    or don't claim it. `manifest` is JSON the caller extracted from
    dataset.yaml with its own YAML parser (mirrors --cases/--capabilities:
    this script never parses YAML itself)."""
    declared_total = manifest.get("cases")
    if isinstance(declared_total, int) and declared_total != len(cases):
        rep.error(None, "manifest_mismatch",
                  f"dataset.yaml declares cases: {declared_total} but "
                  f"{len(cases)} case objects were given; the manifest is "
                  "stale relative to the files it summarizes")

    declared_splits = manifest.get("splits")
    if isinstance(declared_splits, dict):
        actual_splits = {}
        for r in records:
            for name in (r["split"] or []):
                actual_splits[name] = actual_splits.get(name, 0) + 1
        for name, declared_n in declared_splits.items():
            if not isinstance(declared_n, int):
                continue
            actual_n = actual_splits.get(name, 0)
            if actual_n != declared_n:
                rep.error(None, "manifest_mismatch",
                          f"dataset.yaml declares splits.{name}: {declared_n} "
                          f"but {actual_n} cases actually carry split "
                          f"{name!r}")

    by_unit = mapping(manifest.get("coverage_grid", {})).get("by_unit") \
        if isinstance(manifest.get("coverage_grid"), dict) else None
    if isinstance(by_unit, dict):
        actual_by_unit = {}
        for r in records:
            unit = r["unit"]
            if not is_nonempty_str(unit):
                continue
            bucket = actual_by_unit.setdefault(unit, {"total": 0})
            bucket["total"] += 1
            if r["test_type"] is not None:
                bucket[r["test_type"]] = bucket.get(r["test_type"], 0) + 1
        for unit, declared in by_unit.items():
            if not isinstance(declared, dict):
                continue
            actual = actual_by_unit.get(unit, {})
            for key, declared_n in declared.items():
                if not isinstance(declared_n, int):
                    continue
                actual_n = actual.get(key, 0)
                if actual_n != declared_n:
                    rep.error(None, "manifest_mismatch",
                              f"dataset.yaml coverage_grid.by_unit.{unit}."
                              f"{key}: {declared_n} but actual is {actual_n}")


def count_turns(case):
    messages = mapping(case.get("input")).get("messages")
    if not isinstance(messages, list):
        return 0
    return sum(1 for m in messages
               if isinstance(m, dict) and m.get("role") == "user")


def tally(values):
    counts = {}
    for value in values:
        key = value if isinstance(value, str) else str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def check_suite(rep, cases, records):
    seen = {}
    for record in records:
        if record["id"] is not None:
            seen.setdefault(record["id"], 0)
            seen[record["id"]] += 1
    for case_id, n in sorted(seen.items()):
        if n > 1:
            # Reported once per id, not once per duplicate: the finding is
            # about the id, and n names how bad it is.
            rep.error(case_id, "duplicate_id",
                      f"id appears {n} times in this input; per-case reports "
                      "and baseline diffs key on the id, so all but one of "
                      "these cases silently vanish from the results")

    turns = [count_turns(case) for case in cases]
    if len(cases) > SINGLE_TURN_SUITE_MIN and all(t <= 1 for t in turns):
        rep.warn(None, "single_turn_suite",
                 f"all {len(cases)} cases are single-turn; the suite has no "
                 "multi-turn coverage, so context carry-over, correction, and "
                 "follow-up failures cannot be observed at all")

    metamorphic = sum(1 for r in records
                      if r["test_type"] in ("INV", "DIR"))
    if len(cases) > METAMORPHIC_SUITE_MIN and metamorphic == 0:
        rep.warn(None, "no_metamorphic_coverage",
                 f"none of the {len(cases)} cases is INV or DIR: the suite "
                 "tests only direct expectations. An INV case inherits its "
                 "parent's expectation, which makes it the cheapest cell in "
                 "the grid (generate/SKILL.md's budget table treats that "
                 "floor as binding), and without one the suite cannot see a "
                 "paraphrase, a reordering, or a narrowed filter changing an "
                 "answer that was supposed to hold")

    if len(cases) >= ALL_ONE_OFF_MIN_CASES and all(r["one_off"]
                                                   for r in records):
        rep.warn(None, "all_one_off",
                 f"all {len(cases)} cases declare `template_id: null`; a "
                 "suite of nothing but one-offs means the tuple phase never "
                 "happened (generate/SKILL.md §2a), so no variant can be "
                 "compared against its siblings and the pass rate has no "
                 "cluster structure to compute standard errors over")

    categories = tally(r["category"] for r in records)
    for category, n in (categories.items()
                        if len(cases) >= CATEGORY_SKEW_MIN_CASES else ()):
        share = n / len(cases)
        if share > CATEGORY_SKEW_THRESHOLD:
            rep.warn(None, "category_skew",
                     f"category {category!r} is {share:.0%} of the suite "
                     f"({n}/{len(cases)}); a skewed mix makes the headline "
                     "pass rate mostly a measurement of one category")
    return categories


def main():
    ap = argparse.ArgumentParser(
        description="Lint an eval case suite for defects that make it grade "
                    "nothing: duplicate ids, cases asserting only disabled "
                    "layers or bare HTTP liveness, vacuous expectations, and "
                    "unreviewed gating cases (see case-format.md). Cases are "
                    "read as JSON — the calling skill converts the YAML.")
    add_version_flag(ap)
    ap.add_argument("--cases", required=True,
                    help="JSON list of case objects, {\"cases\": [...]}, or "
                         "JSONL; '-' reads stdin")
    ap.add_argument("--capabilities",
                    help="JSON capability_matrix, e.g. {\"routing\": "
                         "{\"enabled\": false}, ...}; REQUIRED unless "
                         "--no-capabilities is given")
    ap.add_argument("--no-capabilities", action="store_true",
                    help="lint without a capability_matrix, treating every "
                         "layer as enabled; reported as capabilities_unchecked "
                         "and rejected under --strict")
    ap.add_argument("--strict", action="store_true",
                    help="promote every WARN to ERROR (a gating CI job)")
    ap.add_argument("--manifest",
                    help="JSON with dataset.yaml's cases/splits/coverage_grid "
                         "fields (caller extracts them with its own YAML "
                         "parser, same as --cases); cross-checked against "
                         "the actual case files so a hand-edit can't leave "
                         "the manifest silently stale")
    a = ap.parse_args()
    # Checked here rather than with argparse's required mutually-exclusive
    # group: argparse writes usage text to STDERR and exits 2 with no payload,
    # which breaks the machine-readable-JSON-on-stdout contract every other
    # bad-input path in this harness keeps (see _common.die).
    if a.capabilities and a.no_capabilities:
        die("--capabilities and --no-capabilities are mutually exclusive: "
            "pass the profile's capability_matrix, or say explicitly that "
            "there is none")
    if not a.capabilities and not a.no_capabilities:
        die("--capabilities <capability_matrix.json> is required (use "
            "--no-capabilities to lint without one). Treating every layer as "
            "enabled cannot catch the case this script exists for: a case "
            "whose only assertions sit in a layer the profile has DISABLED "
            "runs, passes, and grades nothing. That check must not be the "
            "thing a forgotten flag turns off")

    cases = load_cases(a.cases)
    enabled = load_enabled_layers(a.capabilities)
    rep = Report(a.strict)
    if a.no_capabilities:
        rep.warn(None, "capabilities_unchecked",
                 "linted with --no-capabilities: every layer is treated as "
                 "enabled, so a case asserting only DISABLED layers cannot be "
                 "reported here and a clean exit does not mean the suite "
                 "grades anything under this profile — re-run with "
                 "--capabilities before trusting it (--strict refuses this "
                 "mode outright)")

    all_ids = {c.get("id") for c in cases if is_nonempty_str(c.get("id"))}
    records = [check_case(rep, case, all_ids, enabled, index=i)
               for i, case in enumerate(cases)]
    categories = check_suite(rep, cases, records)
    if a.manifest:
        text = sys.stdin.read() if a.manifest == "-" else load_text(a.manifest)
        try:
            manifest = json.loads(text)
        except json.JSONDecodeError as e:
            die(f"bad input: {a.manifest}: {e}")
        if not isinstance(manifest, dict):
            die(f"{a.manifest}: expected a JSON object (dataset.yaml's "
                f"fields), got {type(manifest).__name__}")
        check_manifest(rep, cases, records, manifest)

    layer_counts = {layer: sum(1 for r in records if layer in r["graded"])
                    for layer in LAYERS}
    # Counted PER SPLIT, not per membership list: a reader checking the seal
    # or sizing a smoke run wants "how many cases are in holdout", and
    # tallying the lists answers a different question ({"['full', 'smoke']":
    # 7}) that has to be re-added by hand to answer this one.
    split_counts = {}
    for record in records:
        splits = record["split"] if isinstance(record["split"], list) else []
        for name in splits:
            key = name if isinstance(name, str) else str(name)
            split_counts[key] = split_counts.get(key, 0) + 1
    summary = {
        "cases": len(cases),
        "by_category": categories,
        "by_test_type": tally(r["test_type"] for r in records
                              if r["test_type"] is not None),
        "by_split": dict(sorted(split_counts.items())),
        "one_off": sum(1 for r in records if r["one_off"]),
        "by_graded_layer": layer_counts,
        "enabled_layers": sorted(enabled),
        "http_only": sum(1 for r in records if r["http_only"]),
    }
    errors = sum(1 for f in rep.findings if f["severity"] == "ERROR")
    print(json.dumps({
        "harness_version": HARNESS_VERSION,
        "strict": a.strict,
        "capabilities_checked": bool(a.capabilities),
        "summary": summary,
        "findings": rep.findings,
        "error_count": errors,
        "warn_count": len(rep.findings) - errors,
    }, indent=2))
    sys.exit(1 if rep.has_errors() else 0)


if __name__ == "__main__":
    main()
