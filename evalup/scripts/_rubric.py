"""A rubric file's rules, shared by validate_cases.py and judge_run.py.

A rubric is Markdown with YAML frontmatter (docs/rubric-format.md). This
module never reads YAML: convert_suite.py does, and hands the frontmatter
over as JSON (`--rubrics-dir`, or the `rubrics` key of a conversion). What
lives here is what a frontmatter MEANS -- the v1 run-time form `form:
criteria` (docs/judge-run.md SS2) and the pin a case writes -- so the linter
and the judge pass cannot disagree about which rubric is valid.

Not a CLI: like _common.py it has no argparse and answers no --help.
"""
import re

# `<rubric_id>-v<version>` is a PIN, not part of the id (rubric-format.md
# SS'Ids'). The runner keeps its own copy (run_cases.RUBRIC_PIN) for its
# calibration check; the two patterns are the same text.
RUBRIC_PIN = re.compile(r"-v(\d+)$")
RUBRIC_ID_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
CRITERION_ID_RE = re.compile(r"[a-z][a-z0-9_]*")
CRITERIA_FORM = "criteria"
MAX_CRITERIA = 12
CRITERION_KEYS = ("id", "question", "reference_guided", "canary_guard")
SELF_TEST_KEYS = ("user", "answer", "response", "reference", "fails")


def split_pin(ref):
    """'grounded-answer-v1' -> ('grounded-answer', 1); no pin -> (ref, None)."""
    m = RUBRIC_PIN.search(ref)
    if not m:
        return ref, None
    return ref[:m.start()], int(m.group(1))


def is_criteria_form(front):
    return isinstance(front, dict) and front.get("form") == CRITERIA_FORM


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def rubric_findings(front, file_stem=None):
    """[(severity, code, message)] for one rubric's frontmatter.

    `severity` is ERROR or WARN, the linter's two. A DAG rubric (no `form:
    criteria`) is checked for its id and version only: nothing at run time
    reads its nodes, and rubric-format.md is its spec."""
    out = []

    def err(code, message):
        out.append(("ERROR", code, message))

    if not isinstance(front, dict):
        return [("ERROR", "rubric_not_a_mapping",
                 "the frontmatter is not a mapping")]
    rid = front.get("rubric_id")
    if not isinstance(rid, str) or not RUBRIC_ID_RE.fullmatch(rid):
        err("rubric_id", f"rubric_id {rid!r} must be lowercase words joined "
            "by hyphens (rubric-format.md SS'Ids')")
    elif RUBRIC_PIN.search(rid):
        err("rubric_id", f"rubric_id {rid!r} ends in -v<digits>, which a case "
            "pin would read as a version")
    elif file_stem is not None and file_stem != rid:
        err("rubric_file_name", f"rubric_id {rid!r} is in {file_stem}.md; "
            "the file is rubrics/<rubric_id>.md")
    if not _is_int(front.get("version")) or front.get("version") < 1:
        err("rubric_version", f"version {front.get('version')!r} must be an "
            "integer >= 1")
    form = front.get("form")
    if form is None:
        return out
    if form != CRITERIA_FORM:
        err("rubric_form", f"form {form!r} is not {CRITERIA_FORM!r}; leave "
            "it out for the DAG form")
        return out
    if "nodes" in front:
        err("rubric_form", "form: criteria replaces nodes: -- a file holds "
            "one form, not both")
    criteria = front.get("criteria")
    ids = []
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= \
            MAX_CRITERIA:
        err("rubric_criteria", f"criteria must be a list of 1-{MAX_CRITERIA} "
            "yes/no questions (one judge call grades them all)")
        criteria = criteria if isinstance(criteria, list) else []
    for i, crit in enumerate(criteria):
        where = f"criteria[{i}]"
        if not isinstance(crit, dict):
            err("rubric_criterion", f"{where} is not a mapping")
            continue
        cid = crit.get("id")
        if not isinstance(cid, str) or not CRITERION_ID_RE.fullmatch(cid):
            err("rubric_criterion", f"{where}.id {cid!r} must be snake_case")
        elif cid in ids:
            err("rubric_criterion", f"{where}.id {cid!r} is used twice")
        else:
            ids.append(cid)
        question = crit.get("question")
        if not isinstance(question, str) or not question.strip():
            err("rubric_criterion", f"{where}.question must be a non-empty "
                "yes/no question")
        for flag in ("reference_guided", "canary_guard"):
            if flag in crit and not isinstance(crit[flag], bool):
                err("rubric_criterion", f"{where}.{flag} must be true or "
                    "false")
        extra = sorted(set(crit) - set(CRITERION_KEYS))
        if extra:
            out.append(("WARN", "rubric_criterion_key",
                        f"{where} has keys nothing reads: {extra}"))
    st = front.get("self_test")
    if not isinstance(st, dict):
        err("rubric_self_test", "form: criteria needs a self_test with a "
            "good and a bad answer (docs/judge-run.md SS3): without one no "
            "case on this rubric is judged")
        return out
    for side in ("good", "bad"):
        block = st.get(side)
        if not isinstance(block, dict):
            err("rubric_self_test", f"self_test.{side} is missing or not a "
                "mapping")
            continue
        for key in ("user", "answer"):
            if not isinstance(block.get(key), str) or not block[key].strip():
                err("rubric_self_test", f"self_test.{side}.{key} must be a "
                    "non-empty string")
        if "response" not in block:
            err("rubric_self_test", f"self_test.{side}.response is missing: "
                "it is the app data the evidence may quote")
        extra = sorted(set(block) - set(SELF_TEST_KEYS))
        if extra:
            out.append(("WARN", "rubric_self_test_key",
                        f"self_test.{side} has keys nothing reads: {extra}"))
    good, bad = st.get("good"), st.get("bad")
    if isinstance(good, dict) and "fails" in good:
        err("rubric_self_test", "self_test.good has `fails`: the good answer "
            "passes every criterion")
    if isinstance(bad, dict):
        fails = bad.get("fails")
        if not isinstance(fails, list) or not fails:
            err("rubric_self_test", "self_test.bad.fails must name at least "
                "one criterion the bad answer fails")
        else:
            for name in fails:
                if name not in ids:
                    err("rubric_self_test", f"self_test.bad.fails names "
                        f"{name!r}, which is not a criterion id")
            if len(set(map(str, fails))) != len(fails):
                err("rubric_self_test", "self_test.bad.fails repeats an id")
    return out
