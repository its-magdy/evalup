#!/usr/bin/env python3
"""Statistical gate for agent-eval comparisons. Stdlib only.

Paired comparison of two runs over the SAME cases (matched by case id). All
statistics below are computed from the discordant pairs (b01 = fixed by the
candidate, b10 = broken by it) and all are EXACT at every n (no chi-square/
normal approximation anywhere in this file):

  keep decision - exact posterior P(candidate better) = P(p > 0.5) for
                  p ~ Beta(1+b01, 1+b10), in closed form. One rule at all n.
  reported      - exact one-sided sign test p-value, alongside.
  reported      - McNemar's exact test (two-sided), the textbook named test
                  for this exact same discordant-pairs binomial. See
                  mcnemar_exact_p_two_sided below for why "exact" matters:
                  the version most stats packages default to is a chi-square
                  (optionally continuity-corrected) approximation that is
                  unreliable once b01+b10 < 25 - common at agent-eval n. Both
                  the one-sided sign test and McNemar's test are the same
                  Binomial(b01+b10, 0.5) tail computation read one-sided vs.
                  two-sided; reporting both under their own names avoids
                  making the reader reverse-engineer that equivalence.

ONE decision rule at all n, deliberately. An earlier version gated on the
posterior below n=100 and on a two-sided McNemar test at or above it; because
those encode very different evidence bars (posterior >= 0.8 is roughly
one-sided p <= 0.2, vs 0.025 for two-sided alpha=0.05 plus a delta>0 filter),
the same b01/b10 could flip keep=True to keep=False when the dataset grew by a
single case. The split's stated rationale (CLT unreliable at small n) never
applied: the tests used here are exact, not normal-approximated, so there is
no n at which one method becomes valid and the other does not.

--alpha applies to the reported sign test, the reported McNemar test, and the
minimum-detectable-effect calculation. It does NOT gate keep; tighten
--bayes-threshold for that.

Also answers "what n do I need": minimum detectable effect at the current n,
stated in plain language alongside the number.

Usage: stats.py <baseline.jsonl> <candidate.jsonl> [--alpha 0.05]
                [--bayes-threshold 0.8]
Rows: {"case_id": ..., "verdict": "pass"|"fail"}. Any other verdict is a hard
error: infra verdicts must be excluded upstream, never counted as failures.

See also: reduce_repeats.py for pass^k/pass@k over repeated verdicts of the
SAME case (a reliability/flakiness signal, not a baseline-vs-candidate one).
"""
import argparse
import json
import math
from statistics import NormalDist

from _common import add_version_flag, die, load_jsonl

VERDICTS = ("pass", "fail")


def load(path):
    rows = load_jsonl(path, unique_key="case_id",
                      required_keys=("case_id", "verdict"))
    out = {}
    for r in rows:
        verdict = r["verdict"]
        if verdict not in VERDICTS:
            die(f"{path}: case {r['case_id']!r} has verdict {verdict!r}; "
                "only 'pass'/'fail' are comparable. Infra verdicts "
                "(infra_error/infra_incomplete) and 'unscored' must be "
                "filtered out upstream — counting them as failures would "
                "bias the gate toward revert.")
        out[r["case_id"]] = verdict == "pass"
    return out


def binom_one_sided_p(k, n):
    """Exact one-sided sign-test p-value: P(X >= k) for X ~ Binomial(n, 0.5).

    One-sided because the question is directional — we only ever keep a
    candidate that is BETTER, so the alternative is one-tailed. Reporting a
    two-sided p while separately requiring delta > 0 would silently halve the
    stated alpha (0.05 becomes 0.025)."""
    if n == 0:
        return 1.0
    return min(1.0, sum(math.comb(n, i) for i in range(k, n + 1)) * 0.5 ** n)


def mcnemar_exact_p_two_sided(b01, b10):
    """McNemar's exact test, two-sided: 2 * P(X <= min(b01,b10)) for
    X ~ Binomial(b01+b10, 0.5), capped at 1 - the same computation R's
    `exact2x2::mcnemar.exact` / a two-sided `scipy.stats.binomtest` on the
    discordant pairs would report. This is the EXACT binomial variant: valid
    at every n, and specifically correct where it matters most - the classic
    McNemar chi-square test (with or without Edwards' continuity correction)
    is a normal approximation that is unreliable once the discordant count
    b01+b10 < 25, which is the common case for small agent-eval datasets.
    binom_one_sided_p above computes the same tail one-sided (the directional
    question this gate actually needs); this is its two-sided sibling,
    reported under its conventional name so it reads as the named test it
    is, not just an alias for the sign test. P(X <= lo) is computed as
    P(X >= n-lo) by the binomial's symmetry around n/2, i.e. via
    binom_one_sided_p itself, rather than re-summing the tail by hand — one
    exact-tail implementation for both the one- and two-sided tests, and
    n==0 falls out of that same function's own guard for free."""
    n = b01 + b10
    lo = min(b01, b10)
    return min(1.0, 2 * binom_one_sided_p(n - lo, n))


def posterior_prob_improvement(b01, b10):
    """Exact P(p > 0.5) for p ~ Beta(1+b01, 1+b10): the posterior probability
    (uniform prior) that a discordant pair favors the candidate. Closed form
    for integer parameters — deterministic, no sampling:
    P(Beta(a,b) > 1/2) = sum_{k=0}^{a-1} C(a+b-1, k) / 2^(a+b-1)."""
    a, b = 1 + b01, 1 + b10
    n = a + b - 1
    return sum(math.comb(n, k) for k in range(a)) * 0.5 ** n


def z_sum(alpha, power):
    """One-sided z budget, matching the one-sided sign test --alpha also
    parameterizes. Using the two-sided inv_cdf(1 - alpha/2) here would be the
    mirror image of the sidedness mistake binom_one_sided_p warns about: it
    inflated the reported MDE by ~13% and the "cases you need" figure by
    ~27%, telling users to grow a dataset that was already large enough."""
    nd = NormalDist()
    return nd.inv_cdf(1 - alpha) + nd.inv_cdf(power)


def min_detectable_effect(n, p_disc, alpha=0.05, power=0.8):
    """MDE for paired binary outcomes: z * sqrt(p_disc / n), where p_disc is
    the observed discordant fraction (floored at 1/n so an all-concordant run
    doesn't claim an MDE of zero)."""
    if n == 0:
        return 1.0
    p_disc = max(p_disc, 1.0 / n)
    return min(1.0, z_sum(alpha, power) * math.sqrt(p_disc / n))


def main():
    ap = argparse.ArgumentParser(
        description="Paired statistical gate for two agent-eval runs over "
                    "the same cases (JSONL rows: case_id, verdict). Keeps "
                    "on an exact Beta posterior at all n; reports an exact "
                    "one-sided sign test alongside.")
    add_version_flag(ap)
    ap.add_argument("baseline", help="baseline verdicts JSONL")
    ap.add_argument("candidate", help="candidate verdicts JSONL")
    ap.add_argument("--alpha", type=float, default=0.05,
                    help="significance level for the REPORTED sign test, "
                         "the REPORTED McNemar exact test, and the "
                         "minimum-detectable-effect calc; does not gate "
                         "keep (default: 0.05)")
    ap.add_argument("--bayes-threshold", type=float, default=0.8,
                    help="P(candidate better) required to keep; raise to "
                         "~0.95 for frequentist-strength evidence "
                         "(default: 0.8)")
    a = ap.parse_args()

    base, cand = load(a.baseline), load(a.candidate)
    common = sorted(set(base) & set(cand))
    if not common:
        die("no common case ids - runs not comparable (check dataset_version)")

    b01 = sum(1 for c in common if not base[c] and cand[c])  # candidate fixed
    b10 = sum(1 for c in common if base[c] and not cand[c])  # candidate broke
    n = len(common)
    base_rate = sum(base[c] for c in common) / n
    cand_rate = sum(cand[c] for c in common) / n
    delta = cand_rate - base_rate
    p_disc = (b01 + b10) / n
    mde = min_detectable_effect(n, p_disc, a.alpha)

    out = {
        "n": n,
        "baseline_pass_rate": round(base_rate, 4),
        "candidate_pass_rate": round(cand_rate, 4),
        "delta": round(delta, 4),
        "fixed_by_candidate": b01,
        "broken_by_candidate": b10,
        "min_detectable_effect_at_this_n": round(mde, 3),
        "min_detectable_effect_explained": (
            f"at n={n} (alpha={a.alpha}, 80% power), only a true pass-rate "
            f"swing of about {mde:.0%} or larger is reliably distinguishable "
            "from noise here; an observed delta smaller than that may be "
            "real but this dataset is too small to tell it apart from "
            "chance - treat a sub-MDE 'keep' as not proven, not as no "
            "effect."),
    }

    prob = posterior_prob_improvement(b01, b10)
    sign_p = binom_one_sided_p(b01, b01 + b10)
    mcnemar_p = mcnemar_exact_p_two_sided(b01, b10)
    out.update({
        "method": "bayes-beta exact posterior (all n)",
        "decision_rule": f"P(candidate better) >= {a.bayes_threshold} "
                         "and delta > 0",
        "p_candidate_better": round(prob, 3),
        "threshold": a.bayes_threshold,
        "sign_test_p_one_sided": round(sign_p, 5),
        "sign_test_significant_at_alpha": bool(sign_p < a.alpha),
        "mcnemar_exact_p_two_sided": round(mcnemar_p, 5),
        "mcnemar_significant_at_alpha": bool(mcnemar_p < a.alpha),
        "mcnemar_note": (
            "exact binomial McNemar test on the discordant pairs "
            f"(b01={b01}, b10={b10}), not the chi-square approximation - "
            "exact and reportable even though the discordant count is "
            f"{'below' if (b01 + b10) < 25 else 'at or above'} the 25-pair "
            "threshold where that approximation would otherwise be "
            "unreliable."),
        "keep": bool(prob >= a.bayes_threshold and delta > 0),
    })
    if out["keep"] and not out["sign_test_significant_at_alpha"]:
        out["gate_note"] = (
            f"kept on the posterior bar (P={prob:.3f} >= {a.bayes_threshold}) "
            f"while the one-sided sign test is not significant at "
            f"alpha={a.alpha} (p={sign_p:.4f}). This is the intended, more "
            "permissive product bar for iterative optimization — raise "
            "--bayes-threshold (e.g. 0.95) if you want frequentist-strength "
            "evidence before keeping a change.")

    if abs(delta) < mde and not out["keep"]:
        if delta == 0:
            out["advice"] = (
                f"no observed difference; at n={n} only effects of ~{mde:.0%} "
                "or larger are detectable - grow the dataset "
                "(agent-eval:generate) rather than re-rolling candidates")
        else:
            need = math.ceil(z_sum(a.alpha, 0.8) ** 2 *
                             max(p_disc, 1.0 / n) / abs(delta) ** 2)
            out["advice"] = (
                f"observed delta {delta:+.1%} is below the ~{mde:.0%} "
                f"detectable at n={n}; to detect ~{abs(delta):.1%} reliably "
                f"you need ~{need} cases - grow the dataset "
                "(agent-eval:generate) rather than re-rolling candidates")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
