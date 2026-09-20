#!/usr/bin/env python3
"""Deterministic answer-content scorer for evalup.

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

Both sides of a plain-string check are NFC-normalized before comparing (see
_common.nfc), so an entry authored in one Unicode form matches an answer
written in the other. Regex entries get the ANSWER normalized but never the
PATTERN — normalizing a pattern would rewrite regex source the author chose,
and inside a character class it would silently change what the class means.
Write patterns in NFC (the form every editor produces by default) or match the
combining mark explicitly; every regex check carries a note saying so.

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

from _common import (
    REGEX_TIMEOUT_S,
    RegexTimeout,
    add_version_flag,
    load_object,
    load_text,
    nfc,
    optional_mapping,
    require_list,
    require_mapping,
    run_bounded,
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


def search_bounded(pattern, text):
    """re.search under _common.run_bounded's wall-clock bound."""
    return run_bounded(lambda: re.search(pattern, text))


FLAG_SUFFIX_RE = re.compile(r"^/(.*)/([aiLmsux]{1,4})$", re.DOTALL)


def is_regex_entry(entry):
    """The /pattern/ marker, named once: content_check must know whether an
    entry took the regex path in order to report the asymmetric normalization
    below, and re-deriving the test there is how the note and the behaviour
    would drift apart."""
    return len(entry) > 2 and entry.startswith("/") and entry.endswith("/")


def entry_matches(entry, text):
    """True/False, or None if the entry is an invalid or non-terminating
    regex (both reported "unscorable" — see content_check).

    `text` is already NFC (main normalizes the answer once). For a plain
    substring the ENTRY is normalized here too, so the two sides are compared
    in the same Unicode form. A regex pattern is deliberately left as the
    author wrote it — see the module docstring."""
    if is_regex_entry(entry):
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
    return nfc(entry) in text


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
    # `nfc(v)`, not `v`: the ANSWER side is already normalized (main normalizes
    # the whole answer text before parsing it), so only the schema's own
    # literals still need it. nfc passes non-strings through, so the bool/int
    # discipline above is untouched.
    if "enum" in schema and not any(
            nfc(v) == value and isinstance(v, bool) == isinstance(value, bool)
            for v in schema["enum"]):
        errors.append(f"{path}: {value!r} not in enum")
    if isinstance(value, dict):
        # Key names are text too, and a key that IS present reported "required
        # key missing" when the case spelled it in the other Unicode form.
        for key in schema.get("required", []):
            if nfc(key) not in value:
                errors.append(f"{path}.{key}: required key missing")
        for key, sub in (schema.get("properties") or {}).items():
            if nfc(key) in value and isinstance(sub, dict):
                errors.extend(validate_schema(value[nfc(key)], sub,
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


# Said in the OUTPUT, not only in the docstring: the normalization is
# asymmetric on this path (answer yes, pattern no), and a reader looking at a
# regex that "should" have matched has no other way to learn that the text it
# ran against is not byte-for-byte the text the app produced.
REGEX_NFC_NOTE = ("the answer was NFC-normalized before matching; the pattern "
                  "was not (normalizing regex source could change what a "
                  "character class means) — author patterns in NFC")
REGEX_NOT_NFC_NOTE = (
    " — WARNING: this pattern is not in NFC form (it contains decomposed "
    "characters, e.g. 'e' + a combining accent). It cannot match those "
    "characters in the normalized answer: rewrite it in NFC")


def regex_note(pattern):
    """The note every regex check carries, sharpened when the pattern itself
    is decomposed — that is the one case where the asymmetry above turns a
    correct expectation into a fail, so it must not read as boilerplate."""
    return REGEX_NFC_NOTE + (REGEX_NOT_NFC_NOTE if nfc(pattern) != pattern
                             else "")


def content_check(kind, entry, answer):
    normalized = normalize_entry(entry)
    if normalized is None:
        return {"check": kind, "entry": entry, "status": "unscorable",
                "reason": f"entry must be a string or number, got "
                          f"{type(entry).__name__}: quote it in the case YAML"}
    check = scored_entry(kind, entry, normalized, answer)
    if is_regex_entry(normalized):
        check["note"] = regex_note(normalized[1:-1])
    return check


def scored_entry(kind, entry, normalized, answer):
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
    # Normalized ONCE, here, rather than per check: every comparison below
    # (substring, regex, and the JSON parsed out of this same text) then runs
    # in one Unicode form, and no future check can be added that forgets to.
    # See _common.nfc for why an unnormalized comparison is a manufactured
    # failure rather than a cosmetic one.
    answer = nfc(load_text(a.answer))
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
