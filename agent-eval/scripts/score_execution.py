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
  with type-aware normalization: 7 == "7" == "7.0", "1,200" == 1200, "$5" == 5,
  1200 == "1200 hrs" (a unit/currency/percent on ONE side is presentation, not
  content), strings casefold+trim. Numbers use --float-tolerance (default
  exact). bool is never coerced to a number. A unit on BOTH sides must agree:
  "12 hours" != "12 days". Units are compared literally, so an expected value
  written with a unit demands that unit back — write the bare number instead if
  the unit is not part of the answer.
  Read the one-sided rule as strictly as it is written: a BARE expected number
  accepts ANY unit back, because this scorer cannot tell a presentational unit
  from a wrong one without a second unit to compare against — 5 == "5 apples"
  and 5 == "5 kg" both pass. That is the deliberate trade for 1200 == "1200
  hrs". If the unit is part of the answer, write it into the expected value and
  the both-sides rule applies. Known gap: a sign before the currency ("-$5") is
  not recognized as numeric and falls through to string comparison.
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
import math
import re
from collections import Counter

from _common import (
    add_version_flag,
    die,
    load_object,
    optional_mapping,
    require_list,
    require_range,
)

# A negative tolerance makes `abs(expected - actual) <= tol` false even when the
# two values are identical, so EVERY numeric comparison fails — a clean exit 0
# carrying a manufactured failure, which is the one outcome this scorer exists
# to prevent. Both the flag and the per-case override are bounded against it.
_TOL_NOTE = " (a negative tolerance fails even exact matches)"

# Leading currency/symbol or trailing unit/percent that a correct numeric answer
# routinely carries ("$1,200", "1200 hrs", "45%"). Stripped only when what's left
# parses as a number, so a genuine string answer ("Night") is never mangled.
_THOUSANDS = re.compile(r"(?<=\d),(?=\d\d\d\b)")
_NUM_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_TRAILING_UNIT = re.compile(r"\s*([A-Za-z%]+)$")
# A bare `e`/`E` welded to the digits is a HALF-WRITTEN EXPONENT, not a unit:
# "5e" is a malformed number. Read as a unit it made the bare-expected-number
# rule below accept it as 5 (5 == "5e"), which is the one-sided-unit trade
# applied to a string that does not spell a quantity at all. A separating space
# ("5 e") is left alone — that shape is a real, if odd, unit.
_EXPONENT_FRAGMENT = re.compile(r"\d[eE]$")
_CURRENCY = tuple("$€£¥")


def split_unit(s):
    """Split "$1,200" / "45%" / "1200 hrs" into (numeric core, unit token). The
    unit is None when the string carries no unit at all — that one value is the
    whole answer, so callers test it rather than the core.

    The unit is RETURNED, not discarded. Deleting it here is what let
    "12 hours" compare equal to "12 days" — see cells_equal."""
    unit_parts, core = [], s
    # startswith, not `core[:1] in _CURRENCY`: every string CONTAINS the empty
    # string, so the membership form was true for core == "" and the append
    # below then indexed an empty string. An empty scalar on either side (an app
    # that answered with "", a case whose ground truth is "") took the scorer
    # down with an IndexError — exit 1 and a traceback, outside the exit-2
    # contract. startswith is empty-safe by construction.
    if core.startswith(_CURRENCY):
        unit_parts.append(core[0])
        core = core[1:].strip()
    trailing = _TRAILING_UNIT.search(core)
    if trailing and not _EXPONENT_FRAGMENT.search(core):
        unit_parts.append(trailing.group(1))
        core = core[:trailing.start()].strip()
    return (core, " ".join(unit_parts).casefold() if unit_parts else None)


def as_number(value):
    """Return (number, unit) if `value` is (or spells) a number, else
    (None, None). `unit` is None for a bare number and the normalized
    symbol/word otherwise ("$5" -> (5.0, "$"), "1200 hrs" -> (1200.0, "hrs")).

    bool is not a number here — YAML/JSON `true` must not compare equal to 1,
    which would silently pass a wrong answer."""
    if isinstance(value, bool):
        return (None, None)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return (None, None)
        return (float(value), None)
    if isinstance(value, str):
        s = _THOUSANDS.sub("", value.strip())
        if _NUM_RE.match(s):
            return (float(s), None)
        core, unit = split_unit(s)
        if unit is not None and _NUM_RE.match(core):
            return (float(core), unit)
    return (None, None)


def norm_string(value):
    """Casefold + collapse whitespace, for the non-numeric scalar path."""
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


_MISMATCH = "answer value does not match ground truth"


def compare(expected, actual, tol):
    """One value vs one value, type-aware: returns the reason they differ, or
    None when they are equal. Numbers (or numeric strings) compare with
    absolute tolerance; everything else compares as normalized strings. None
    matches only None (a NULL cell is a real, distinct value).

    The verdict IS the reason — one value, so a caller cannot report "fail"
    beside a reason from another branch. A unit-only disagreement needs its own
    message ("7 does not match 7" reads like a harness bug otherwise, and the
    fix — drop the unit from the case, or correct the app — depends on knowing
    which it was), and re-deriving which branch had fired from outside is how a
    reason ends up contradicting the verdict it explains."""
    if expected is None or actual is None:
        return None if expected is None and actual is None else _MISMATCH
    (en, eu), (an, au) = as_number(expected), as_number(actual)
    if en is not None and an is not None:
        near = abs(en - an) <= tol
        # A unit on ONE side is tolerated: the oracle stores a bare 1200 and the
        # app answered "1200 hrs" — same answer, different presentation, which
        # is the whole point of quasi-exact match. DIFFERENT units on BOTH sides
        # are a real disagreement, and stripping them before comparing made this
        # scorer pass "12 days" against an expected "12 hours" — precisely the
        # confidently-wrong answer it exists to catch.
        if eu is not None and au is not None and eu != au:
            return ((f"numeric values match but the units differ "
                     f"({eu!r} vs {au!r}); if the unit is not part of "
                     "the answer, write expect.result.scalar as a bare "
                     "number") if near else _MISMATCH)
        return None if near else _MISMATCH
    if (en is None) != (an is None):
        # one is a number, the other a genuine string -> unequal
        return _MISMATCH
    return None if norm_string(expected) == norm_string(actual) else _MISMATCH


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
    # The row paths never report a per-cell reason, so they test only whether
    # compare found one.
    return all(compare(expected_row.get(c), actual_row.get(c), tol) is None
               for c in columns)


def canonical_bag(rows, columns):
    """A hashable multiset key for the projected rows. json with sort_keys, so
    unhashable cells (nested objects/lists) are covered too; default=str keeps
    an exotic cell from raising rather than comparing."""
    return Counter(json.dumps([row.get(c) for c in columns], sort_keys=True,
                              default=str)
                   for row in rows)


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
    equal. n is tiny (eval result sets), so the O(n^3) worst case is irrelevant.

    The search is ITERATIVE, with its own explicit stack. The natural recursive
    phrasing recurses once per row along an augmenting path, so a result set of
    ~1000 identical rows blew CPython's default recursion limit and exited 1
    with a RecursionError traceback — outside the exit-2 error contract every
    scorer shares, and reported to the run skill as an infra failure rather
    than as a comparison. Result-set size is app data, not case authoring: it
    must not be able to crash the scorer."""
    if len(expected) != len(actual):
        return False
    n = len(expected)
    # Fast path for the overwhelmingly common shape: the two bags are already
    # equal cell-for-cell. Comparison is reflexive, so identical multisets
    # always have a perfect matching, and this settles them in O(n) instead of
    # the O(n^2) adjacency build below — the difference between a millisecond
    # and minutes once a result set runs to thousands of rows. It is a
    # SUFFICIENT condition only; anything else falls through to real matching.
    if canonical_bag(expected, columns) == canonical_bag(actual, columns):
        return True
    adj = [[j for j in range(n) if rows_equal(expected[i], actual[j], columns, tol)]
           for i in range(n)]
    match_to = [-1] * n  # actual index -> the expected index it's paired with

    def augment(start):
        """DFS for an augmenting path from expected row `start`. Each stack
        frame is [expected row, iterator over its remaining candidates, the
        actual row it is currently trying]. Reaching a free actual row rewires
        the whole path from the stack — the same assignments the recursive
        version made as its frames unwound."""
        seen = [False] * n
        stack = [[start, iter(adj[start]), -1]]
        while stack:
            frame = stack[-1]
            j = next((c for c in frame[1] if not seen[c]), None)
            if j is None:
                stack.pop()  # row exhausted; back up to its predecessor
                continue
            seen[j] = True
            frame[2] = j
            if match_to[j] == -1:
                for i, _, taken in stack:
                    match_to[taken] = i
                return True
            stack.append([match_to[j], iter(adj[match_to[j]]), -1])
        return False

    for i in range(n):
        if not augment(i):
            return False  # expected row i cannot be paired -> not a perfect match
    return True


def score_scalar(expected, actual, tol):
    reason = compare(expected, actual, tol)
    check = {"check": "scalar", "status": "fail" if reason else "pass",
             "expected": expected, "actual": actual}
    if reason:
        check["reason"] = reason
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
        require_list("expect.result.columns", columns, item="column name")
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
    # Validated after parse_args() rather than through an argparse `type=`
    # callable, for the reason in _common.require_range: argparse reports its
    # own errors as usage text on stderr with no JSON payload.
    require_range("--float-tolerance", a.float_tolerance, lo=0, note=_TOL_NOTE)

    expect = load_object(a.expect)
    # `or {}` guarded against null but not against a scalar, and every probe
    # below happens to be valid on a string: `"scalar" not in spec` became a
    # SUBSTRING test, so `result: "scalar"` slipped past this gate and died on
    # spec.get() with an AttributeError, while `result: "nope"` returned a quiet
    # "unscored" for a case that does assert something. Same require_mapping the
    # sibling scorers apply to their own expectation blocks.
    spec = optional_mapping("expect.result", expect.get("result"))
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
    require_range("expect.result.float_tolerance", tol, lo=0, note=_TOL_NOTE)

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
