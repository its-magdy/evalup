#!/usr/bin/env python3
"""Argument-correctness scorer for agent-eval.

Checks a case's expect.args assertions against the observed tool calls in a
normalized trajectory. Expectation kinds (see case-format.md):
  present          - the arg exists on the call with a non-empty value
  from_tool_result - the arg's value appears in the result content of an
                     EARLIER tool call (requires content capture; without
                     captured results the check is "unscorable", never a fail)
  <anything else>  - literal: observed must equal it (str-compared as fallback)

from_tool_result matching is token-bounded (the value must not be a fragment
of a longer token: INV-1 does not match inside INV-10) and non-string results
are matched against their JSON serialization. On the trajectory's FIRST tool
call the check FAILS — no earlier result exists to source the value from, so
provenance is impossible regardless of content capture. Values shorter than
3 characters carry no provenance signal and are "unscorable".

Every observed call of an asserted tool must satisfy its assertions. A tool
that was never called is reported "tool_not_called" and not scored here —
missing tools are the trajectory layer's finding, not an argument error.

Usage: score_args.py <trajectory.json> <case_expect.json>
  trajectory.json: normalize_trace.py output, or {"tool_calls": [...]}
  case_expect.json: the case's "expect" object; reads its "args" mapping
Verdict: pass (>=1 check passed, none failed) | fail (any check failed) |
unscored (nothing scorable). Exit 0 always; exit 2 on malformed input.
"""
import argparse
import json
import re

from _common import add_version_flag, load_object, load_trajectory


def value_matches(observed, expected):
    return observed == expected or str(observed) == str(expected)


def found_in(needle, haystacks):
    # Token-bounded: the needle must not continue into a longer
    # identifier-like token on either side (letters, digits, '.', '-').
    pattern = r"(?<![\w.\-])" + re.escape(needle) + r"(?![\w.\-])"
    return any(re.search(pattern, h) for h in haystacks)


def check_call(idx, call, arg_name, expectation, prior_results):
    args = call.get("args")
    if not isinstance(args, dict) or args.get("_unparsed"):
        return ("unscorable", "call arguments not captured/parseable")
    if arg_name not in args:
        return ("fail", f"arg {arg_name!r} absent from call #{idx}")
    val = args[arg_name]
    if expectation == "present":
        if val in (None, "", [], {}):
            return ("fail", f"arg {arg_name!r} empty on call #{idx}")
        return ("pass", None)
    if expectation == "from_tool_result":
        if idx == 0:
            return ("fail", f"arg {arg_name!r}={val!r} on call #{idx}: no "
                            "earlier tool call exists to source this value "
                            "from (provenance impossible)")
        if not prior_results:
            return ("unscorable",
                    "no prior tool results captured (content capture off?)")
        needle = val if isinstance(val, str) else json.dumps(val)
        if len(needle) < 3:
            return ("unscorable", f"value {val!r} too short to carry "
                                  "provenance signal")
        if found_in(needle, prior_results):
            return ("pass", None)
        return ("fail", f"arg {arg_name!r}={val!r} on call #{idx} not found "
                        "in any prior tool result (possible hallucinated arg)")
    if value_matches(val, expectation):
        return ("pass", None)
    return ("fail", f"arg {arg_name!r} on call #{idx}: "
                    f"expected {expectation!r}, observed {val!r}")


def main():
    ap = argparse.ArgumentParser(
        description="Score expect.args assertions against a normalized "
                    "trajectory (see case-format.md).")
    add_version_flag(ap)
    ap.add_argument("trajectory", help="normalized trajectory JSON")
    ap.add_argument("expect", help="case 'expect' object JSON")
    a = ap.parse_args()
    calls = load_trajectory(a.trajectory)
    expect_args = load_object(a.expect).get("args") or {}

    checks = []
    for tool, assertions in expect_args.items():
        indexed = [(i, c) for i, c in enumerate(calls)
                   if c.get("name") == tool]
        if not indexed:
            checks.append({"tool": tool, "status": "tool_not_called",
                           "note": "missing tool is a trajectory-layer "
                                   "finding, not scored here"})
            continue
        for arg_name, expectation in (assertions or {}).items():
            for i, call in indexed:
                prior_results = [
                    r if isinstance(r, str) else json.dumps(r)
                    for r in (c.get("result") for c in calls[:i])
                    if r is not None]
                status, reason = check_call(i, call, arg_name, expectation,
                                            prior_results)
                checks.append({"tool": tool, "arg": arg_name,
                               "expectation": expectation, "call_index": i,
                               "status": status,
                               **({"reason": reason} if reason else {})})

    statuses = [c["status"] for c in checks]
    if "fail" in statuses:
        verdict = "fail"
    elif "pass" in statuses:
        verdict = "pass"
    else:
        verdict = "unscored"
    print(json.dumps({
        "layer": "args",
        "verdict": verdict,
        "checks": checks,
        "unscorable": sum(s == "unscorable" for s in statuses),
    }, indent=2))


if __name__ == "__main__":
    main()
