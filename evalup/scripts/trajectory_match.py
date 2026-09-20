#!/usr/bin/env python3
"""Trajectory matching for evalup.

Compares an observed tool-call trajectory against a case's expectations.
Modes (per case, not global):
  subset   (default) - expected tools must all appear; extras allowed
  in_order           - expected tools appear in this relative order; extras allowed
  exact              - exactly these calls, in this order, nothing else
  any_order          - exactly these calls, any order

Tool names are compared in NFC on both sides (see _common.nfc).

This layer scores which tools were CALLED, not whether they succeeded. Calls
that reported an error are always listed in "errored_calls" (with a warning),
and --fail-on-errored-calls gates the verdict on them; the default is off so
that adding the signal does not silently re-score existing datasets.

Also computes trajectory precision/recall and forbidden-tool violations.
Precision/recall are computed from the SAME expectation list the verdict
used (order when present, else subset) — metrics never contradict the mode.

Usage: trajectory_match.py <trajectory.json> <case.json>
  trajectory.json: normalize_trace.py output, or {"tool_calls": [...]}
  case.json: the case's "expect" object (see case-format.md)
Prints a JSON verdict to stdout. Exit 0 always (verdict carries pass/fail);
exit 2 on malformed input.
"""
import argparse
import json
from collections import Counter

from _common import (
    add_version_flag,
    die,
    load_object,
    load_trajectory,
    nfc,
    optional_list,
    optional_mapping,
    require_list,
)

ORDER_MODES = ("in_order", "exact", "any_order")


def is_subsequence(needle, haystack):
    it = iter(haystack)
    return all(item in it for item in needle)


def asserted_list(field, value):
    """A tool-list key the author WROTE, so it must actually assert something.

    optional_list is the wrong validator behind an `in` guard: `order:` with no
    value parses as None and it returned [], and `order: []` was a list so it
    passed untouched. Either way match() scored `expected == []` — is_subsequence
    of nothing is vacuously true, Counter() == Counter() is true — and every
    trajectory PASSED an assertion the author believed they had written. That is
    the same confidently-wrong-verdict class as the order_mode typo guard below,
    one step earlier: a malformed expectation silently becoming the loosest
    possible check instead of an input error. Absence is still fine (the key is
    simply not asserted); presence-without-content is not."""
    entries = require_list(field, value, item="tool name")
    if not entries:
        die(f"{field} is present but empty; an empty list asserts nothing and "
            f"passes every trajectory — remove the key instead")
    # NFC, matching the observed names in main (see _common.nfc). Tool names
    # are usually ASCII identifiers, but normalize_trace's own docstring warns
    # that a non-ASCII tool name is a shape this harness has to survive — and
    # here the failure is total: the expected tool is reported missing from a
    # trajectory that called it, spelled the other way.
    return [nfc(e) for e in entries]


def match(observed_names, expect_tools):
    """Returns (mode, passed, detail, expected_used) — expected_used is the
    list the verdict was scored against, so P/R derive from the same list."""
    if not expect_tools:
        return ("none", True, "no tool expectations", [])

    # A bare string is the plausible YAML slip — `subset: get_invoice` instead
    # of `subset: [get_invoice]` — and it must not degrade silently: `subset:
    # "foo"` reported missing ['f','o','o'] and FAILED a trajectory that called
    # foo. This was the scorer the slip was first found in; the rule and the
    # rest of the reasoning now live in _common.require_list, shared with
    # score_authz.py / score_answer.py. Absent means the key was not asserted,
    # which is not a slip — hence the `in` guards, and asserted_list once past
    # them.
    if "order" in expect_tools:
        expected = asserted_list("expect.tools.order", expect_tools["order"])
        mode = expect_tools.get("order_mode", "in_order")
        # A typo must never silently degrade to in_order — that is the
        # LOOSEST mode, so `order_mode: Exact` would quietly turn a strict
        # assertion permissive and the case would pass for the wrong reason.
        if mode not in ORDER_MODES:
            die(f"unknown order_mode {mode!r}; expected one of "
                f"{', '.join(ORDER_MODES)}")
        if mode == "exact":
            passed = observed_names == expected
        elif mode == "any_order":
            passed = Counter(observed_names) == Counter(expected)
        else:  # in_order
            passed = is_subsequence(expected, observed_names)
        detail = {"expected": expected, "observed": observed_names}
        if "subset" in expect_tools:
            detail["warning"] = ("both 'order' and 'subset' present; "
                                 "'order' takes precedence, 'subset' ignored")
        return (mode, passed, detail, expected)

    if "subset" in expect_tools:
        expected = asserted_list("expect.tools.subset", expect_tools["subset"])
        obs = Counter(observed_names)
        need = Counter(expected)
        missing = [t for t in sorted(need)
                   for _ in range(max(0, need[t] - obs[t]))]
        return ("subset", not missing,
                {"expected_subset": expected, "missing": missing,
                 "observed": observed_names}, expected)

    return ("none", True, "no order/subset key", [])


def precision_recall(observed_names, expected_names):
    if not expected_names:
        return (None, None)
    obs, exp = Counter(observed_names), Counter(expected_names)
    tp = sum(min(obs[t], exp[t]) for t in exp)
    # No observed calls -> precision is undefined, not 0.
    precision = round(tp / sum(obs.values()), 4) if obs else None
    recall = round(tp / sum(exp.values()), 4)
    return (precision, recall)


def main():
    ap = argparse.ArgumentParser(
        description="Match an observed tool-call trajectory against a case's "
                    "'expect' object (see case-format.md).")
    add_version_flag(ap)
    ap.add_argument("trajectory", help="trajectory JSON ({'tool_calls': [...]})")
    ap.add_argument("expect", help="case 'expect' object JSON")
    ap.add_argument("--fail-on-errored-calls", action="store_true",
                    help="fail the case when any observed tool call reported "
                         "an error (default: report them, do not fail — "
                         "whether a failed tool means a failed CASE is a "
                         "case-format decision, and flipping it silently "
                         "would re-score every existing dataset)")
    a = ap.parse_args()
    calls = load_trajectory(a.trajectory)
    # Normalized once, here, so every consumer below (the mode match, the
    # forbidden set, precision/recall) compares the same form. errored_calls
    # keeps the raw name on purpose: it is evidence quoted from the trace, not
    # a comparison.
    observed = [nfc(c.get("name")) for c in calls]
    # A tool that was CALLED and a tool that WORKED are different claims, and
    # this layer only ever checked the first. With no record of the error at
    # all (normalize_trace dropped it until recently), a trajectory whose only
    # tool call returned HTTP 500 scored "pass" at precision 1.0 — the same
    # confidently-wrong-verdict class this harness exists to catch, one layer
    # up from the stub-tool case in score_execution's docstring. Reported
    # unconditionally so it can never again be invisible; gated behind a flag
    # for the verdict, so the default stays a pure "were these tools used".
    errored = [{"index": i, "tool": c.get("name"), "error": c.get("error")}
               for i, c in enumerate(calls) if c.get("error") is not None]
    # The CONTAINER, before optional_list validates what is inside it. `or {}` let
    # any non-mapping through, and match() then probed it with `"order" in
    # expect_tools` — a SUBSTRING test on a string, an element test on a list —
    # so `tools: "subset"` passed the probe and raised TypeError on the indexing
    # that followed. Every non-dict shape exited 1 with a traceback.
    expect_tools = optional_mapping("expect.tools",
                                    load_object(a.expect).get("tools"))

    mode, passed, detail, expected_flat = match(observed, expect_tools)

    # Validated for the same reason, with the opposite blast radius: `forbidden:
    # "delete_user"` becomes the character set {'d','e','l',...}, so no real tool
    # name is ever in it and the gate silently passes everything it exists to
    # catch. A too-permissive authz check is the worse direction to fail in.
    forbidden = {nfc(t) for t in
                 optional_list("expect.tools.forbidden",
                               expect_tools.get("forbidden"),
                               item="tool name")}
    violations = [t for t in observed if t in forbidden]
    if violations:
        passed = False
    if errored and a.fail_on_errored_calls:
        passed = False

    precision, recall = precision_recall(observed, expected_flat)

    out = {
        "layer": "trajectory",
        "mode": mode,
        "verdict": "pass" if passed else "fail",
        "forbidden_violations": violations,
        "errored_calls": errored,
        "precision": precision,
        "recall": recall,
        "detail": detail,
    }
    if errored and not a.fail_on_errored_calls:
        out["warning"] = (f"{len(errored)} observed tool call(s) reported an "
                          "error; this layer scores which tools were CALLED, "
                          "not whether they succeeded — pass "
                          "--fail-on-errored-calls to gate on it")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
