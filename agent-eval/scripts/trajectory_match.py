#!/usr/bin/env python3
"""Trajectory matching for agent-eval.

Compares an observed tool-call trajectory against a case's expectations.
Modes (per case, not global):
  subset   (default) - expected tools must all appear; extras allowed
  in_order           - expected tools appear in this relative order; extras allowed
  exact              - exactly these calls, in this order, nothing else
  any_order          - exactly these calls, any order

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

from _common import add_version_flag, die, load_object, load_trajectory

ORDER_MODES = ("in_order", "exact", "any_order")


def is_subsequence(needle, haystack):
    it = iter(haystack)
    return all(item in it for item in needle)


def match(observed_names, expect_tools):
    """Returns (mode, passed, detail, expected_used) — expected_used is the
    list the verdict was scored against, so P/R derive from the same list."""
    if not expect_tools:
        return ("none", True, "no tool expectations", [])

    if "order" in expect_tools:
        expected = expect_tools["order"]
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
        expected = expect_tools["subset"]
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
    a = ap.parse_args()
    observed = [c.get("name") for c in load_trajectory(a.trajectory)]
    expect_tools = load_object(a.expect).get("tools") or {}

    mode, passed, detail, expected_flat = match(observed, expect_tools)

    forbidden = set(expect_tools.get("forbidden") or [])
    violations = [t for t in observed if t in forbidden]
    if violations:
        passed = False

    precision, recall = precision_recall(observed, expected_flat)

    print(json.dumps({
        "layer": "trajectory",
        "mode": mode,
        "verdict": "pass" if passed else "fail",
        "forbidden_violations": violations,
        "precision": precision,
        "recall": recall,
        "detail": detail,
    }, indent=2))


if __name__ == "__main__":
    main()
