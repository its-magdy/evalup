#!/usr/bin/env python3
"""Tool-loop and redundancy detector for agent-eval.

Flags, within one trajectory:
  - identical repeats: same tool called with identical args >= threshold times
  - near repeats: same tool called with different args >= 2x threshold times
    (possible flailing/retry loop)
  - total call count above budget

Usage: detect_loops.py <trajectory.json> [--repeat-threshold 3] [--call-budget 15]

Default threshold is 3: two identical calls is one retry (normal after a
transient tool error); three means the agent saw the same result twice and
tried the same thing anyway. Pass --repeat-threshold 2 for strict mode.
"""
import argparse
import hashlib
import json
from collections import Counter, defaultdict

from _common import add_version_flag, load_trajectory


def args_hash(args):
    return hashlib.sha256(
        json.dumps(args or {}, sort_keys=True).encode()).hexdigest()[:12]


def main():
    ap = argparse.ArgumentParser(
        description="Flag tool-call loops and redundancy in a normalized "
                    "trajectory: identical repeats, flailing (many distinct "
                    "arg sets on one tool), and call-budget overrun.")
    add_version_flag(ap)
    ap.add_argument("trajectory",
                    help="normalize_trace.py output, or {'tool_calls': [...]}")
    ap.add_argument("--repeat-threshold", type=int, default=3,
                    help="identical calls needed to flag a repeat loop; "
                         "flailing triggers at 2x this many distinct arg "
                         "sets (default: 3)")
    ap.add_argument("--call-budget", type=int, default=15,
                    help="total calls above which the budget is exceeded "
                         "(default: 15)")
    a = ap.parse_args()

    calls = load_trajectory(a.trajectory)

    exact = Counter((c.get("name"), args_hash(c.get("args"))) for c in calls)
    by_name = Counter(c.get("name") for c in calls)
    distinct_args = defaultdict(set)
    for c in calls:
        distinct_args[c.get("name")].add(args_hash(c.get("args")))

    identical_repeats = [
        {"tool": name, "args_hash": h, "count": n}
        for (name, h), n in exact.items() if n >= a.repeat_threshold
    ]
    # Flailing is measured in DISTINCT argument sets, not raw call count, so
    # the two findings stay independent: N identical calls are a repeat loop
    # (one arg set, not flailing), while a tool that both repeats itself AND
    # churns through many different args reports both — previously the repeat
    # finding suppressed the flailing one and that second signal was lost.
    heavy_tools = [
        {"tool": name, "count": by_name[name],
         "distinct_arg_sets": len(hashes)}
        for name, hashes in distinct_args.items()
        if len(hashes) >= a.repeat_threshold * 2
    ]

    findings = []
    if identical_repeats:
        findings.append("identical-repeat-loop")
    if heavy_tools:
        findings.append("possible-flailing")
    if len(calls) > a.call_budget:
        findings.append("call-budget-exceeded")

    print(json.dumps({
        "layer": "loops",
        "verdict": "fail" if findings else "pass",
        "total_calls": len(calls),
        "findings": findings,
        "identical_repeats": identical_repeats,
        "heavy_tools": heavy_tools,
    }, indent=2))


if __name__ == "__main__":
    main()
