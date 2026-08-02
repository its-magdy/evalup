#!/usr/bin/env python3
"""Deterministic execution-accuracy scorer for data-Q&A agents.

Grades WHAT the agent answered against a ground-truth result set, not the prose
it wrapped around it. This is the text-to-SQL "execution accuracy" idea (Spider/
BIRD): compare the *result set*, never the query string, so any correct path to
the right data passes — plus GAIA-style "quasi exact match" for scalar answers
(normalize case/whitespace/thousands-separators/units before comparing). It is
what catches the confidently-wrong answer a prose scorer waves through (e.g. a
stub tool returning [] while the bot says "no licences are expiring").

The scorer is PURE COMPARISON. Producing the two sides is the run skill's job,
kept out here on purpose so this stays deterministic and fixture-testable:
- expected: computed offline from the case's reference query against a seeded
  fixture / read-only oracle (see case-format.md expect.result), stored in the
  case. Never recomputed here.
- actual: extracted by the runner from the trace (a tool result), the app's
  structured response, or a parsed answer, and handed in as a small JSON file.
  If the runner could not extract it (no content capture, unstructured prose),
  it passes {"missing": true, "reason": ...} and the check is "unscorable" —
  an unread result scored as a mismatch would be a manufactured failure, the
  same rule the other scorers follow.

Two shapes, exactly one per case (see case-format.md):
- scalar: a single value answer ("how many nurses on nights?" -> 7). Compared
  with type-aware normalization: 7 == "7" == "7.0", "1,200" == 1200, "$5" == 5
  (units/currency/percent/commas stripped), strings casefold+trim. Numbers use
  --float-tolerance (default exact). bool is never coerced to a number.
- rows: a result set (list of row objects). Compared as a BAG by default
  (order-insensitive multiset — SELECT without ORDER BY has no defined order),
  or as an ordered list when result.ordered is true. Rows are projected to the
  expected columns first (extra columns the app returned are ignored — the
  expected row set is the spec), each cell normalized like a scalar, and matched
  with float tolerance. Multiplicity matters: [a,a,b] != [a,b,b].

Usage: score_execution.py <actual_result.json> <case_expect.json>
  actual_result.json : {"scalar": <v>} | {"rows": [ {..}, .. ]}
                       | {"missing": true, "reason": "<why>"}
  case_expect.json   : the case 'expect' object (reads expect.result)
Verdict: pass | fail | unscored (case has no expect.result) . Exit 0 always;
exit 2 on malformed input. A present-but-unextractable actual yields verdict
"unscored" with unscorable=1, never "fail".
"""
import argparse
import json
import re
import math

from _common import add_version_flag, die, load_object

# Leading currency/symbol or trailing unit/percent that a correct numeric answer
# routinely carries ("$1,200", "1200 hrs", "45%"). Stripped only when what's left
# parses as a number, so a genuine string answer ("Night") is never mangled.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d\d\d\b)")
_NUM_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def as_number(value):
    """Return a float if `value` is (or spells) a number, else None. bool is not
    a number here — YAML/JSON `true` must not compare equal to 1, which would
    silently pass a wrong answer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and math.isnan(value) else float(value)
    if isinstance(value, str):
        s = _THOUSANDS.sub("", value.strip())
        # Strip one leading currency-ish symbol and/or one trailing unit token,
        # then re-test: "$5" -> "5", "45%" -> "45", "1200 hrs" -> "1200".
        s_core = s.lstrip("$€£¥").rstrip("%").strip()
        s_core = re.sub(r"\s*[A-Za-z]+$", "", s_core).strip() or s_core
        for candidate in (s, s_core):
            if _NUM_RE.match(candidate):
                return float(candidate)
    return None


def norm_string(value):
    """Casefold + collapse whitespace, for the non-numeric scalar path."""
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def cells_equal(expected, actual, tol):
    """One value vs one value, type-aware. Numbers (or numeric strings) compare
    with absolute tolerance; everything else compares as normalized strings.
    None matches only None (a NULL cell is a real, distinct value)."""
    if expected is None or actual is None:
        return expected is None and actual is None
    en, an = as_number(expected), as_number(actual)
    if en is not None and an is not None:
        return abs(en - an) <= tol
    if (en is None) != (an is None):
        return False  # one is a number, the other a genuine string -> unequal
    return norm_string(expected) == norm_string(actual)


def project(row, columns):
    """Restrict a row object to the expected columns. A row missing an expected
    column keeps the key as None so a dropped column fails rather than being
    skipped. A non-dict row (the app returned a bare scalar where an object
    was expected) projects to all-None for the same reason — fail the
    comparison, don't skip it."""
    if not isinstance(row, dict):
        return {c: None for c in columns}
    return {c: row.get(c) for c in columns}


def rows_equal(expected_row, actual_row, columns, tol):
    return all(cells_equal(expected_row.get(c), actual_row.get(c), tol)
               for c in columns)


def bag_match(expected, actual, columns, tol):
    """Multiset equality: every expected row pairs with a DISTINCT actual row.

    Float tolerance makes cells unhashable (no Counter) AND makes "equal"
    non-exclusive — one expected row can be within tolerance of several actual
    rows. Greedy first-fit is therefore WRONG: it can consume an actual row that
    a later expected row is the only match for, and report a spurious mismatch on
    a set that has a valid perfect matching (e.g. expected [1.0, 1.4] vs actual
    [1.2, 1.0] at tol 0.3 — 1.0↔1.0 / 1.4↔1.2 is valid, but greedy pairs
    1.0↔1.2 first and then fails 1.4 against 1.0). Use maximum bipartite matching
    (Kuhn's augmenting-path); a perfect matching exists iff the multisets are
    equal. n is tiny (eval result sets), so the O(n^3) worst case is irrelevant."""
    if len(expected) != len(actual):
        return False
    n = len(expected)
    adj = [[j for j in range(n) if rows_equal(expected[i], actual[j], columns, tol)]
           for i in range(n)]
    match_to = [-1] * n  # actual index -> the expected index it's paired with

    def augment(i, seen):
        for j in adj[i]:
            if not seen[j]:
                seen[j] = True
                if match_to[j] == -1 or augment(match_to[j], seen):
                    match_to[j] = i
                    return True
        return False

    for i in range(n):
        if not augment(i, [False] * n):
            return False  # expected row i cannot be paired -> not a perfect match
    return True


def score_scalar(expected, actual, tol):
    ok = cells_equal(expected, actual, tol)
    check = {"check": "scalar", "status": "pass" if ok else "fail",
             "expected": expected, "actual": actual}
    if not ok:
        check["reason"] = "answer value does not match ground truth"
    return check


def score_rows(spec, expected, actual, tol):
    if not isinstance(actual, list):
        return {"check": "rows", "status": "fail",
                "reason": f"actual.rows must be a list, got "
                          f"{type(actual).__name__}"}
    # Columns to compare: explicit override, else the union of keys the expected
    # rows define (the expected set is the spec; columns the app added are noise).
    # OMITTED and EXPLICITLY EMPTY must stay distinguishable: `not columns` is
    # true for both, and collapsing them made an explicit `columns: []` silently
    # switch to bare-scalar-row mode, where whole rows compare as JSON — so the
    # app's extra columns stopped being ignored and a correct result set FAILED.
    # A manufactured failure is the one outcome this harness must never produce,
    # so an empty list is a loud error, not a mode.
    columns = spec.get("columns")
    if columns is not None:
        if not isinstance(columns, list):
            die("expect.result.columns must be a list of column names")
        if not columns:
            die("expect.result.columns is empty; omit the key to compare every "
                "column the expected rows define, or name at least one column")
        scalar_rows = False
    else:
        columns = sorted({k for r in expected if isinstance(r, dict)
                          for k in r})
        # Only an omitted `columns` can mean this: no expected row is an object,
        # so the rows are bare scalars rather than column-keyed records.
        scalar_rows = not columns
    if scalar_rows:
        columns = ["_v"]
        exp = [{"_v": r} for r in expected]
        act = [{"_v": r} for r in actual]
    else:
        if not all(isinstance(r, dict) for r in expected):
            die("expect.result.rows mixes object and scalar rows; use one shape")
        exp = expected
        act = [project(r, columns) for r in actual]

    ordered = bool(spec.get("ordered"))
    if ordered:
        ok = (len(exp) == len(act) and
              all(rows_equal(e, a, columns, tol) for e, a in zip(exp, act)))
    else:
        ok = bag_match(exp, act, columns, tol)
    check = {"check": "rows", "status": "pass" if ok else "fail",
             "expected_count": len(exp), "actual_count": len(act),
             "columns": columns, "ordered": ordered}
    if not ok:
        check["reason"] = ("row sets differ (order-sensitive)" if ordered
                           else "row sets differ (as multisets)")
    return check


def main():
    ap = argparse.ArgumentParser(
        description="Score expect.result (scalar or row set) against the "
                    "result the app actually produced — execution accuracy for "
                    "data-Q&A agents (see case-format.md).")
    add_version_flag(ap)
    ap.add_argument("actual", help="JSON: {\"scalar\":..} | {\"rows\":[..]} | "
                                    "{\"missing\":true,\"reason\":..}")
    ap.add_argument("expect", help="case 'expect' object JSON")
    ap.add_argument("--float-tolerance", type=float, default=0.0,
                    help="absolute tolerance for numeric cell comparison "
                         "(default 0.0 = exact); overridden per-case by "
                         "expect.result.float_tolerance")
    a = ap.parse_args()

    expect = load_object(a.expect)
    spec = (expect.get("result") or {})
    if not spec or ("scalar" not in spec and "rows" not in spec):
        # Nothing to score for this layer — not every case is a data question.
        print(json.dumps({"layer": "execution", "verdict": "unscored",
                          "checks": [], "unscorable": 0}, indent=2))
        return
    if "scalar" in spec and "rows" in spec:
        die("expect.result has both 'scalar' and 'rows'; use exactly one")

    tol = spec.get("float_tolerance", a.float_tolerance)
    if not isinstance(tol, (int, float)) or isinstance(tol, bool):
        die("expect.result.float_tolerance must be a number")

    actual = load_object(a.actual)
    if actual.get("missing"):
        reason = actual.get("reason", "runner could not extract a result set")
        print(json.dumps({
            "layer": "execution", "verdict": "unscored",
            "checks": [{"check": "scalar" if "scalar" in spec else "rows",
                        "status": "unscorable", "reason": reason}],
            "unscorable": 1}, indent=2))
        return

    if "scalar" in spec:
        if "scalar" not in actual:
            die("case expects a scalar result but actual has no 'scalar' key")
        check = score_scalar(spec["scalar"], actual["scalar"], tol)
    else:
        if "rows" not in actual:
            die("case expects rows but actual has no 'rows' key")
        if not isinstance(spec["rows"], list):
            die("expect.result.rows must be a list")
        check = score_rows(spec, spec["rows"], actual["rows"], tol)

    print(json.dumps({
        "layer": "execution",
        "verdict": check["status"] if check["status"] in ("pass", "fail")
        else "unscored",
        "checks": [check],
        "unscorable": int(check["status"] == "unscorable"),
    }, indent=2))


if __name__ == "__main__":
    main()
