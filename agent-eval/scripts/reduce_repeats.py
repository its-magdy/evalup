#!/usr/bin/env python3
"""Reliability reducer for repeated runs of the SAME case. Stdlib only.

Inspect's `--epochs N` + score-reducer model: run each case k times (same
inputs, temperature > 0) and reduce the k verdicts to a reliability signal,
rather than reporting one lucky/unlucky trial. Computes both:

  pass@k (Codex/HumanEval, unbiased estimator) - "does it succeed AT LEAST
          ONCE in k tries": pass@k = E[1 - C(n-c,k) / C(n,k)]
  pass^k (tau-bench)                            - "does it succeed on ALL k
          tries" - the actual reliability number: pass^k = E[C(c,k) / C(n,k)]

for n observed repeats with c passes per case, then averaged (the
expectation E[.]) across cases. Both formulas are exact combinatorics, no
sampling. tau-bench's finding is the reason to always report both together:
a model can clear >60% pass^1 but <25% pass^8 on the SAME tasks - a single
run hides this, and the pass@k - pass^k gap IS the flakiness signal (a large
gap = capable but inconsistent; a small gap = consistently right or
consistently wrong, not flaky).

This is a companion to stats.py, not a replacement: stats.py compares
baseline vs. candidate over DIFFERENT run pairs of the SAME cases (one
verdict each); this reduces MANY repeats of the SAME case under ONE run to a
single reliability number. Different question, different input shape -
duplicate case_id across rows is the point here, not an error.

Usage: reduce_repeats.py <repeats.jsonl> [--k K]
Rows: {"case_id": ..., "verdict": "pass"|"fail"}, one row per repeat/epoch.
Multiple rows sharing a case_id are the repeats of that case; every case
must have at least K repeats (default: the smallest repeat count present,
so nothing is silently dropped). Any verdict other than pass/fail is a hard
error, same rule as stats.py: infra verdicts must be excluded upstream.
"""
import argparse
import json
import math

from _common import add_version_flag, die, load_jsonl

VERDICTS = ("pass", "fail")


def comb0(n, k):
    """math.comb, but 0 outside its domain rather than a ValueError. This
    matches the definitional limit the formulas rely on: C(n-c,k)=0 when
    n-c<k so pass@k saturates at 1 (it already succeeded more times than k
    could miss); C(c,k)=0 when c<k so pass^k floors at 0 (not enough
    successes to fill k slots). n is always >=0 here (a repeat count)."""
    if k < 0 or k > n:
        return 0
    return math.comb(n, k)


def load_groups(path):
    """Group repeat rows by case_id. Unlike stats.py's loader, duplicate
    case_id is expected and required here - each case needs >1 row to have
    anything to reduce."""
    rows = load_jsonl(path, required_keys=("case_id", "verdict"))
    groups = {}
    for r in rows:
        verdict = r["verdict"]
        if verdict not in VERDICTS:
            die(f"{path}: case {r['case_id']!r} has verdict {verdict!r}; "
                "only 'pass'/'fail' are comparable. Infra verdicts "
                "(infra_error/infra_incomplete) and 'unscored' must be "
                "filtered out upstream - counting them as failures would "
                "bias the reliability estimate.")
        groups.setdefault(r["case_id"], []).append(verdict == "pass")
    return groups


def pass_at_k(n, c, k):
    """Unbiased pass@k estimator: probability at least one success shows up
    among k samples drawn without replacement from the n observed repeats."""
    return 1.0 - comb0(n - c, k) / math.comb(n, k)


def pass_hat_k(n, c, k):
    """pass^k: probability all k drawn samples are successes - the
    reliability number, always <= pass@k for the same n, c, k."""
    return comb0(c, k) / math.comb(n, k)


def main():
    ap = argparse.ArgumentParser(
        description="pass^k / pass@k reliability reducer over repeated "
                    "verdicts of the SAME case (JSONL rows: case_id, "
                    "verdict; multiple rows per case_id = repeats/epochs). "
                    "Reports the pass@k - pass^k gap as a flakiness signal.")
    add_version_flag(ap)
    ap.add_argument("repeats", help="repeated-verdicts JSONL")
    ap.add_argument("--k", type=int, default=None,
                    help="repeats to draw per case (default: the smallest "
                         "repeat count present across cases, so every case "
                         "contributes)")
    a = ap.parse_args()

    groups = load_groups(a.repeats)
    if not groups:
        die("no rows - nothing to reduce")

    ns = {cid: len(v) for cid, v in groups.items()}
    k = a.k if a.k is not None else min(ns.values())
    if k < 1:
        die(f"--k must be >= 1, got {k}")
    short = {cid: n for cid, n in ns.items() if n < k}
    if short:
        worst = sorted(short)[0]
        die(f"--k={k} exceeds the repeat count for {len(short)} case(s), "
            f"e.g. {worst!r} has only {short[worst]} - lower --k or add more "
            "repeats for those cases")

    per_case = []
    for cid in sorted(groups):
        n = ns[cid]
        c = sum(groups[cid])
        per_case.append({
            "case_id": cid,
            "n": n,
            "c": c,
            "pass_at_k": round(pass_at_k(n, c, k), 4),
            "pass_hat_k": round(pass_hat_k(n, c, k), 4),
        })

    mean_pass_at_k = sum(r["pass_at_k"] for r in per_case) / len(per_case)
    mean_pass_hat_k = sum(r["pass_hat_k"] for r in per_case) / len(per_case)
    gap = mean_pass_at_k - mean_pass_hat_k

    out = {
        "k": k,
        "n_cases": len(per_case),
        "repeats_per_case": ns,
        "pass_at_k": round(mean_pass_at_k, 4),
        "pass_hat_k": round(mean_pass_hat_k, 4),
        "flakiness_gap": round(gap, 4),
        "flakiness_note": (
            "pass@k ('succeeds at least once in k tries') minus pass^k "
            "('succeeds on ALL k tries'): a large gap means the agent CAN "
            "succeed but is inconsistent across repeats (flaky); a gap "
            "near 0 means it is consistently right or consistently wrong, "
            "not flaky - and a low pass^k with a low gap means the failure "
            "is a real bug, not noise."),
        "per_case": per_case,
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
