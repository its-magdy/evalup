#!/usr/bin/env python3
"""Deterministic answer-content scorer for agent-eval.

Scores a case's expect.answer.must_contain / must_not_contain entries and
expect.format.json_schema against the app's final answer text.

Entry semantics (see case-format.md): a plain string is a substring check;
a string wrapped in slashes (/pattern/) is a regex applied with re.search.
Every must_contain entry must match; every must_not_contain entry must not.
An unquoted YAML number is coerced to its string form; a bool or container
is reported "unscorable" (not a crash, and not a silent mismatch). Note the
convention's edge: a path-like literal ("/usr/local/") is read as the REGEX
`usr/local`, not as a substring — express such literals without the leading
and trailing slash. A regex that is invalid, or that fails to terminate
within REGEX_TIMEOUT_S, is "unscorable" rather than a hang or a crash.

format.json_schema uses a minimal stdlib validator supporting: type,
required, properties, items, enum. Unsupported keywords are ignored and
listed in the check's note — passing this check is not full JSON Schema
compliance. The answer is parsed as bare JSON first, then from a markdown
code fence if that fails (wrapping is recorded in the note).

Business-rule oracles (expect.answer.rules) and state-diff assertions are
NOT scored here: rules need the profile's rule definitions and state-diff
needs environment snapshots — the run skill orchestrates both separately.
Judged dimensions (expect.answer.rubric) belong to the judge agent.

Usage: score_answer.py <answer.txt> <case_expect.json>
Verdict: pass (>=1 check passed, none failed) | fail (any check failed) |
unscored (nothing scorable). A "pass" that still has unscorable checks also
carries "partially_unscored": true — the verdict is a pass on the checks that
RAN, and a consumer reading only the verdict must not mistake that for a case
where everything was evaluated. Exit 0 always; exit 2 on malformed input.
"""
import argparse
import json
import re
import signal

from _common import (
    add_version_flag,
    load_object,
    load_text,
    optional_mapping,
    require_list,
    require_mapping,
)

SCHEMA_TYPES = {"object": dict, "array": list, "string": str,
                "integer": int, "number": (int, float), "boolean": bool,
                "null": type(None)}
SUPPORTED_KEYWORDS = {"type", "required", "properties", "items", "enum"}
FENCE_RE = re.compile(r"```(?:[A-Za-z0-9_+-]*)\s*\n(.*?)(?:\n)?```", re.DOTALL)
# Case regexes are authored by humans and by the generator agent, so a
# catastrophic-backtracking pattern (/(a+)+$/) is a plausible accident rather
# than an attack. `re` has no timeout, and nothing upstream bounds a scorer's
# runtime, so an unguarded search hangs the whole scored run on one case.
REGEX_TIMEOUT_S = 2.0


def normalize_entry(entry):
    """Case YAML is written by hand and by the generator agent, so an
    unquoted scalar arrives as int/float rather than str. Numbers coerce
    unambiguously. Bools do not — YAML `true` becomes Python True, whose str
    is 'True', which is not the token the answer would contain — so those
    (and containers) return None and are reported unscorable rather than
    silently mismatching."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, bool) or entry is None:
        return None
    if isinstance(entry, (int, float)):
        return str(entry)
    return None


def parse_json_answer(answer):
    """Return (parsed, note, error). Bare JSON first; failing that, the first
    parseable markdown code fence — chat apps routinely wrap structured
    output in prose, and failing those as 'not valid JSON' reports a format
    violation that isn't one. Wrapping is surfaced in the note so an app with
    a strict bare-JSON contract can still see that it wrapped."""
    try:
        return json.loads(answer), None, None
    except json.JSONDecodeError as bare_err:
        for m in FENCE_RE.finditer(answer):
            try:
                parsed = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            return parsed, ("answer was not bare JSON; validated the JSON "
                            "found inside a markdown code fence"), None
        return None, None, str(bare_err)


class RegexTimeout(Exception):
    pass


def search_bounded(pattern, text):
    """re.search with a wall-clock bound, so one pathological case regex
    cannot hang the run. SIGALRM is Unix-only and main-thread-only; where it
    is unavailable the search runs unguarded rather than not at all."""
    if not hasattr(signal, "SIGALRM"):
        return re.search(pattern, text)

    def on_alarm(signum, frame):
        raise RegexTimeout

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, REGEX_TIMEOUT_S)
    try:
        return re.search(pattern, text)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


FLAG_SUFFIX_RE = re.compile(r"^/(.*)/([aiLmsux]{1,4})$", re.DOTALL)


def entry_matches(entry, text):
    """True/False, or None if the entry is an invalid or non-terminating
    regex (both reported "unscorable" — see content_check)."""
    if len(entry) > 2 and entry.startswith("/") and entry.endswith("/"):
        try:
            return search_bounded(entry[1:-1], text) is not None
        except re.error:
            return None
        except RegexTimeout:
            return "timeout"
    if FLAG_SUFFIX_RE.match(entry):
        # /pattern/i (Perl/JS-style trailing flags) is not this convention:
        # /.../ with no suffix is the whole marker, so /pattern/i falls
        # through unmatched above and — before this check existed — was
        # silently read as a literal substring of the ENTIRE string
        # (backslashes and all), which can never occur in real answer text.
        # A must_contain entry written this way always failed; a
        # must_not_contain entry always vacuously passed, catching nothing.
        # Refuse to guess and say so, rather than repeat either silently.
        return "bad_flag_suffix"
    return entry in text


def validate_schema(value, schema, path="$"):
    errors = []
    t = schema.get("type")
    if t is not None:
        allowed = t if isinstance(t, list) else [t]

        def type_ok(name):
            py = SCHEMA_TYPES.get(name)
            if py is None:
                return False
            # bool is a subclass of int; don't let true pass as integer.
            if name in ("integer", "number") and isinstance(value, bool):
                return False
            return isinstance(value, py)

        if not any(type_ok(x) for x in allowed):
            return [f"{path}: expected type {t}, "
                    f"got {type(value).__name__}"]
    # `value not in enum` is an == test, and bool is a subclass of int in
    # Python: True == 1, so an answer holding `true` satisfied an enum of
    # [1] (and 1 satisfied [true]) — the same bool/int confusion the type
    # check above refuses, arriving through the other keyword. Membership is
    # therefore matched on type as well as value.
    if "enum" in schema and not any(
            v == value and isinstance(v, bool) == isinstance(value, bool)
            for v in schema["enum"]):
        errors.append(f"{path}: {value!r} not in enum")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required key missing")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value and isinstance(sub, dict):
                errors.extend(validate_schema(value[key], sub,
                                              f"{path}.{key}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errors.extend(validate_schema(item, schema["items"],
                                          f"{path}[{i}]"))
    return errors


def unsupported_keywords(schema, acc):
    acc.update(k for k in schema if k not in SUPPORTED_KEYWORDS)
    for sub in (schema.get("properties") or {}).values():
        if isinstance(sub, dict):
            unsupported_keywords(sub, acc)
    if isinstance(schema.get("items"), dict):
        unsupported_keywords(schema["items"], acc)


def content_check(kind, entry, answer):
    normalized = normalize_entry(entry)
    if normalized is None:
        return {"check": kind, "entry": entry, "status": "unscorable",
                "reason": f"entry must be a string or number, got "
                          f"{type(entry).__name__}: quote it in the case YAML"}
    matched = entry_matches(normalized, answer)
    if matched is None:
        return {"check": kind, "entry": entry, "status": "unscorable",
                "reason": "invalid regex"}
    if matched == "timeout":
        return {"check": kind, "entry": entry, "status": "unscorable",
                "reason": f"regex did not terminate within "
                          f"{REGEX_TIMEOUT_S:g}s (catastrophic backtracking): "
                          "simplify the pattern"}
    if matched == "bad_flag_suffix":
        return {"check": kind, "entry": entry, "status": "unscorable",
                "reason": "trailing /flag suffix (e.g. /i) is not supported by "
                          "this convention and was NOT applied — move the "
                          "flag inline as (?i:...) inside the slashes"}
    wanted = (kind == "must_contain")
    if matched == wanted:
        return {"check": kind, "entry": entry, "status": "pass"}
    return {"check": kind, "entry": entry, "status": "fail",
            "reason": ("not found in answer" if wanted
                       else "found in answer")}


def main():
    ap = argparse.ArgumentParser(
        description="Score expect.answer must_contain/must_not_contain and "
                    "expect.format.json_schema against the final answer "
                    "(see case-format.md).")
    add_version_flag(ap)
    ap.add_argument("answer", help="file holding the app's final answer text")
    ap.add_argument("expect", help="case 'expect' object JSON")
    a = ap.parse_args()
    answer = load_text(a.answer)
    expect = load_object(a.expect)
    # `is not None`, not truthiness: the truthy test conflated "key absent"
    # (nothing to score, correct) with "key present but off-shape AND falsy" —
    # `answer: []` was silently dropped and the case reported "unscored", so an
    # expectation the author wrote was never evaluated and nothing said so. Only
    # absence may default; a present value must be the right shape.
    answer_expect = optional_mapping("expect.answer", expect.get("answer"))
    fmt = optional_mapping("expect.format", expect.get("format"))
    json_schema = fmt.get("json_schema")
    if json_schema is not None:
        # Shape-checked on presence; the checks below still gate on truthiness,
        # so an empty `json_schema: {}` stays the no-op it has always been.
        require_mapping("expect.format.json_schema", json_schema)

    checks = []
    for kind in ("must_contain", "must_not_contain"):
        entries = answer_expect.get(kind)
        if entries is None:
            continue
        # The CONTAINER is validated here; normalize_entry owns the per-entry
        # rule (hence of_strings=False — an unquoted number legitimately
        # coerces, a bool is legitimately unscorable). The container is the
        # level the YAML slip actually happens at, and both directions of it
        # produced a wrong verdict rather than an error: a bare-string
        # must_contain PASSED an answer lacking the phrase, and a bare-string
        # must_not_contain FAILED an answer that was correct. See
        # _common.require_list.
        for entry in require_list(f"expect.answer.{kind}", entries,
                                  item="entry", plural="entries",
                                  of_strings=False):
            checks.append(content_check(kind, entry, answer))

    if json_schema:
        ignored = set()
        unsupported_keywords(json_schema, ignored)
        notes = [f"unsupported keywords ignored: {sorted(ignored)}"
                 ] if ignored else []
        parsed, fence_note, parse_error = parse_json_answer(answer)
        if parse_error is not None:
            checks.append({"check": "json_schema", "status": "fail",
                           "reason": f"answer is not valid JSON: "
                                     f"{parse_error}"})
        else:
            if fence_note:
                notes.append(fence_note)
            note = "; ".join(notes)
            errors = validate_schema(parsed, json_schema)
            checks.append({"check": "json_schema",
                           "status": "fail" if errors else "pass",
                           **({"reason": "; ".join(errors)} if errors else {}),
                           **({"note": note} if note else {})})

    statuses = [c["status"] for c in checks]
    if "fail" in statuses:
        verdict = "fail"
    elif "pass" in statuses:
        verdict = "pass"
    else:
        verdict = "unscored"
    unscorable = sum(s == "unscorable" for s in statuses)
    print(json.dumps({
        "layer": "answer",
        # A "pass" carrying unscorable checks is a pass on the checks that
        # RAN, and the count beside it has always said so — but every
        # consumer reads the verdict, and a case whose one real assertion was
        # an invalid regex read identically to a case that passed everything.
        # Flagged rather than downgraded: consumers key on the verdict string,
        # so turning such a case into "unscored" would re-score every existing
        # dataset. The flag is the honest middle.
        "verdict": verdict,
        "partially_unscored": bool(verdict == "pass" and unscorable),
        "checks": checks,
        "unscorable": unscorable,
    }, indent=2))


if __name__ == "__main__":
    main()
