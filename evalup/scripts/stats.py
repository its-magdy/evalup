#!/usr/bin/env python3
"""Statistical gate for evalup comparisons. Stdlib only.

Paired comparison of two runs over the SAME cases (matched by case id). All
statistics below are computed from the discordant pairs (b01 = fixed by the
candidate, b10 = broken by it) and all are EXACT at every n — no chi-square or
normal approximation is used in any DECISION or reported test. (The power
figures below the decision — min_detectable_effect and the "cases you need"
advice — are the exception, and are approximations by construction: they use
NormalDist z-quantiles, so read them as planning estimates, never as exact
bounds.)

The exact pieces:

  keep decision - exact posterior P(candidate better) = P(p > 0.5) for
                  p ~ Beta(1+b01, 1+b10), in closed form. One rule at all n.
  reported      - exact one-sided sign test p-value, alongside.
  reported      - McNemar's exact test (two-sided), the textbook named test
                  for this exact same discordant-pairs binomial. See
                  mcnemar_exact_p_two_sided below for why "exact" matters:
                  the version most stats packages default to is a chi-square
                  (optionally continuity-corrected) approximation that is
                  unreliable once b01+b10 < 25 - common at evalup n. Both
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
stated in plain language alongside the number — and when a KEEP lands under
that number, says so in `sub_mde_keep` rather than leaving the caller to
compare two fields (optimize/SKILL.md §5 surfaces it beside `gate_note`).

Usage: stats.py <baseline.jsonl> <candidate.jsonl> [--alpha 0.05]
                [--bayes-threshold 0.95]
                [--noise-floor <champion_a.jsonl> <champion_b.jsonl>]
                [--candidate-looks N]
       stats.py --holdout-power H --headroom C [--noise-disagreements D]
                [--alpha 0.05] [--bayes-threshold 0.95]
The second form (issue #110) is planning only, before any look: the MDE at
the discordance a whole-cluster fix would produce, and the fewest fixed /
smallest net fixed-minus-broken that keep_decision could keep at H, searched
with keep_decision itself. It changes no number the first form prints.
Rows: {"case_id": ..., "verdict": "pass"|"fail"}. Any other verdict is a hard
error: infra verdicts must be excluded upstream, never counted as failures.

THE DEFAULT BAR IS 0.95 (issue #36). At 0.8 with a k=1 holdout an UNCHANGED
app with 10-30% flaky cases is kept 6-20% of the time per look (simulated,
holdouts of 20-200 cases) and 16-49% across the five looks the ledger allows.
tests/test_scorers.py TestNoOpKeepRate is that simulation, over the functions
below; it pins the 0.8 rule's n=20 cell (13.5% per look, 35% across five) and
the 0.95 rule's bounds. 0.8 stays available as an explicit
`--bayes-threshold 0.8` for an exploratory loop that is not spending the
sealed holdout; never as the holdout's keep bar. What a keep
claims is "posterior probability >= the threshold", nothing stronger: only
`sign_test_significant_at_alpha` licenses the word "significant".

THE NOISE FLOOR (`--noise-floor A B`). A and B are two holdout runs of the
SAME unchanged champion. The number of cases on which they disagree is what
re-running alone moves; a candidate that changed no more cases than that
(fixed + broken <= disagreements, counted over the paired cases both noise
runs cover) has shown nothing a re-run would not, so keep is forced false
and `no_evidence` says why. COVERAGE RULE: the noise runs must cover EVERY
case the candidate changed. Then both counts are taken on one case set (the
covered paired cases, which hold every change), and a paired case the noise
runs miss is one the candidate did not change, so it is in neither count.
A changed case the noise runs miss (an infra drop in
a champion run, say) means the floor was not measured where the candidate
moved: keep is forced false, `no_evidence` names how many, and the
multi-look bound is the no-floor one. Not "every paired case": an infra drop
on a case nobody changed would then block every candidate of the seal. The
posterior is computed and reported exactly as without the flag; the floor
only ever turns a keep into a no-keep, never the reverse. It is a
deterministic count, so the rule needs no judgment from whoever reads the
output.

LOOKS (`--candidate-looks N`, N = candidate comparisons made against this
holdout seal, this one included). Prints `multi_look_false_keep_bound`: a
union bound N x the per-look false-keep rate simulated for an unchanged app
(PER_LOOK_FALSE_KEEP below; pinned by TestNoOpKeepRate). The bound exists
only at the default bar or stricter; below it the output says it is not
bounded rather than inventing a number.

THE BOUND IS A MODEL'S, AND ITS TWO ASSUMPTIONS ARE STATED IN THE OUTPUT
(`multi_look_assumptions`):
  independent cases -- the simulation draws each flaky case on its own, the
    same independence the first item below names. Flaky cases that fail
    TOGETHER (one shared tool, one rate-limited dependency) break it:
    20 cases, 6 of them flipping all at once per run as a coin flip, gives
    12.5% per look and 21.9% across three by exact enumeration of these same
    functions (TestNoOpKeepRate pins that counter-example).
  a fixed champion -- the noise pair measures the app as it was when the
    pair ran. After a keep, the champion is the kept edit, and its noise is
    not what the pair measured: a later look's floor describes a prior app,
    so its rate is not covered by the bound.

THREE THINGS THIS GATE DOES NOT DO, stated here so no reader infers them from
how exact the rest of it is:

  cases are treated as INDEPENDENT. They are not: cases sharing a `template_id`
    are clustered data, and suite-sizing.md ("Grid-generated cases are
    CLUSTERED data") cites clustered standard errors running over three times
    the naive ones. Nothing here reads `template_id`. So the tail probabilities
    are exact for the model, and the model is optimistic on a template-heavy
    suite. Read a marginal keep on a suite built from a few templates as
    weaker than its p-value looks.
  the verdict is CASE-LEVEL, one row per case. There is no per-layer gate: a
    routing regression that a tool-selection gain cancels out is invisible here
    (evalup scores layers independently per case, but nothing aggregates or
    tests them separately). Look at the per-case layer rows before concluding a
    flat rate means nothing moved.
  no multiplicity correction of any kind. Run this once per comparison; every
    extra look at the same holdout is an extra chance to be fooled, which is
    what the N=5 holdout-look ledger exists to bound. `--candidate-looks`
    REPORTS that cost as a union bound; it does not adjust the decision.

See also: reduce_repeats.py for pass^k/pass@k over repeated verdicts of the
SAME case (a reliability/flakiness signal, not a baseline-vs-candidate one).
"""
import argparse
import json
import math
from statistics import NormalDist

from _common import add_version_flag, die, load_jsonl, require_range

VERDICTS = ("pass", "fail")
# Enough ids to start debugging the mismatch by hand, not so many that a run
# with two disjoint datasets buries the statistics under a case list.
MAX_REPORTED_IDS = 20
# Attrition above this fraction of either side is called out in the output.
ATTRITION_WARN_FRACTION = 0.1
# The holdout keep bar. 0.8 keeps no-op edits 6-20% of the time per look
# (issue #36); 0.8 remains an explicit flag for exploratory loops only.
DEFAULT_BAYES_THRESHOLD = 0.95
# Per-look false-keep rate for an UNCHANGED app at DEFAULT_BAYES_THRESHOLD or
# stricter, keyed by whether the noise floor was applied. Simulated with this
# file's own functions (tests/test_scorers.py TestNoOpKeepRate, which asserts
# these are upper bounds on every cell it runs: n = 20/50/200 holdout cases,
# 10%/30% of them flaky at the worst case, a 50/50 folded verdict, runs
# independent, 3000 trials). At 20000 trials over n = 20/50/100/200 x
# 10/20/30% flaky the maxima were 2.4% with the floor and 4.9% without; the
# test's own 3000-trial cells reach 2.9% and 5.3%. A 0.95 posterior bar alone
# is about a one-sided 5% test, so sampling noise lands cells just above 5%:
# hence 6%, not 5%, for the no-floor rate. Both assume independent flaky
# cases and the champion the noise pair measured (docstring, THE BOUND IS A
# MODEL'S): correlated flips or a promoted champion exceed them.
PER_LOOK_FALSE_KEEP = {True: 0.03, False: 0.06}


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
    b01+b10 < 25, which is the common case for small evalup datasets.
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
    would inflate the reported MDE by ~13% and the "cases you need" figure by
    ~27%, and tell users to grow a dataset that is already large enough."""
    nd = NormalDist()
    return nd.inv_cdf(1 - alpha) + nd.inv_cdf(power)


def min_detectable_effect(n, p_disc, alpha=0.05, power=0.8):
    """APPROXIMATE MDE for paired binary outcomes — the one normal-theory
    figure in this file, and it gates nothing: z * sqrt(p_disc / n), where
    p_disc is the observed discordant fraction (floored at 1/n so an
    all-concordant run doesn't claim an MDE of zero). Normal-approximation
    based, so it is a planning estimate, not an exact bound like the tests
    above."""
    if n == 0:
        return 1.0
    p_disc = max(p_disc, 1.0 / n)
    return min(1.0, z_sum(alpha, power) * math.sqrt(p_disc / n))


def noise_floor(run_a, run_b, paired):
    """Disagreements between two runs of the SAME unchanged champion, counted
    over the cases in `paired` (the baseline/candidate pairing) that both
    noise runs cover. Returns (disagreements, cases_counted)."""
    covered = [c for c in paired if c in run_a and c in run_b]
    return sum(1 for c in covered if run_a[c] != run_b[c]), len(covered)


def keep_decision(prob, delta, threshold, changed=None, noise=None,
                  uncovered_changed=0):
    """The keep rule, in one place so the simulation test calls the same code
    the CLI does. Returns (keep, no_evidence_reason_or_None).

    keep = P(candidate better) >= threshold and delta > 0 -- the exact
    posterior rule -- and, when a noise floor is given, the noise
    runs must cover every case the candidate changed (`uncovered_changed` is
    how many they miss) and the candidate must have changed MORE cases
    (fixed + broken) than two runs of the unchanged champion disagree on.
    `changed`/`noise` are both None or both ints."""
    keep = bool(prob >= threshold and delta > 0)
    if keep and noise is not None and uncovered_changed:
        return False, (
            f"no evidence: the two champion runs do not cover "
            f"{uncovered_changed} of the {changed} case(s) the candidate "
            "changed, so the noise floor was not measured where the "
            "candidate moved; this is not kept, whatever the posterior says. "
            "Re-run the champion noise pair only at a new seal; here, treat "
            "it as no evidence")
    if keep and noise is not None and changed <= noise:
        return False, (
            f"no evidence: the candidate changed {changed} case(s) "
            f"(fixed + broken) and two runs of the unchanged champion "
            f"disagree on {noise}; a re-run alone moves that many, so this "
            "is not kept, whatever the posterior says")
    return keep, None


def smallest_keeps(n, threshold, noise=0):
    """Every (fixed, broken) split of n paired cases that keep_decision would
    keep with the fewest fixed for its broken count: for each broken b, the
    smallest fixed f with f + b <= n that clears the posterior bar, delta > 0
    and, with a noise floor of `noise` disagreements, f + b > noise. Breaking
    cases can LOWER the fixed count needed (more changed cases clear the
    floor), which is why every b is tried. Same keep_decision the CLI calls."""
    out = []
    for b in range(n + 1):
        for f in range(b + 1, n - b + 1):
            prob = posterior_prob_improvement(f, b)
            if keep_decision(prob, (f - b) / n, threshold, f + b, noise)[0]:
                out.append({"fixed": f, "broken": b, "net": f - b})
                break
    return out


def holdout_power(n, headroom, noise, alpha, threshold):
    """Before a holdout look: can a candidate on this cluster keep at all?

    `headroom` is the cluster's plausible holdout headroom — how many sealed
    cases a candidate that fixed its whole cluster could turn from fail to
    pass (optimize step 4 estimates it). No new statistic: the counts are
    keep_decision's own rule searched over every split, and the MDE is
    min_detectable_effect at the discordant fraction such a candidate would
    produce (headroom / n). `can_keep` is the hard bar (even fixing every
    headroom case, breaking none of the rest, cannot keep), `powered` the
    80%-power planning figure."""
    keeps = smallest_keeps(n, threshold, noise)
    fewest = min(keeps, key=lambda k: (k["fixed"], k["broken"]), default=None)
    net = min(keeps, key=lambda k: (k["net"], k["broken"]), default=None)
    p_disc = min(1.0, headroom / n)
    mde = min_detectable_effect(n, p_disc, alpha)
    can_keep = fewest is not None and headroom >= fewest["fixed"]
    powered = can_keep and p_disc >= mde
    needed = None
    if headroom > 0 and fewest is not None:
        # A holdout of m cases holds headroom * m / n cluster cases; it needs
        # that to reach the fewest fixed, and m >= z^2 / p_disc for the MDE
        # (the `advice` formula below with delta = p_disc).
        needed = max(math.ceil(fewest["fixed"] * n / headroom),
                     math.ceil(z_sum(alpha, 0.8) ** 2 / p_disc), n)
    if not can_keep:
        least = fewest["fixed"] if fewest else f"more than {n}"
        verdict = (
            f"CANNOT KEEP: a candidate needs at least {least} fixed holdout "
            f"case(s) to keep (fewest_fixed_to_keep), and this cluster's "
            f"plausible headroom is {headroom:g} of {n}")
    elif not powered:
        verdict = (
            f"UNDERPOWERED: a keep is possible, but the expected effect "
            f"{p_disc:.0%} is below the ~{mde:.0%} minimum detectable effect "
            f"at n={n}, so a real gain will usually not keep")
    else:
        verdict = (f"powered: the expected effect {p_disc:.0%} is at or "
                   f"above the ~{mde:.0%} minimum detectable effect")
    return {
        "mode": "holdout_power",
        "holdout_cases": n,
        "headroom": headroom,
        "noise_disagreements": noise,
        "threshold": threshold,
        "expected_effect": round(p_disc, 3),
        "min_detectable_effect": round(mde, 3),
        "fewest_fixed_to_keep": fewest,
        "smallest_net_to_keep": net,
        "can_keep": can_keep,
        "powered": powered,
        "holdout_cases_needed": needed,
        "explained": verdict + (
            f"; a holdout of ~{needed} case(s) would clear both"
            if needed and not powered else "") + (
            ". Planning estimate: the MDE is a normal approximation and "
            "the headroom an estimate; the keep rule is the exact one."),
    }


def main():
    ap = argparse.ArgumentParser(
        description="Paired statistical gate for two evalup runs over "
                    "the same cases (JSONL rows: case_id, verdict). Keeps "
                    "on an exact Beta posterior at all n; reports an exact "
                    "one-sided sign test alongside.")
    add_version_flag(ap)
    ap.add_argument("baseline", nargs="?", help="baseline verdicts JSONL")
    ap.add_argument("candidate", nargs="?", help="candidate verdicts JSONL")
    ap.add_argument("--alpha", type=float, default=0.05,
                    help="significance level for the REPORTED sign test, "
                         "the REPORTED McNemar exact test, and the "
                         "minimum-detectable-effect calc; does not gate "
                         "keep (default: 0.05)")
    ap.add_argument("--bayes-threshold", type=float,
                    default=DEFAULT_BAYES_THRESHOLD,
                    help="posterior P(candidate better) required to keep "
                         f"(default: {DEFAULT_BAYES_THRESHOLD}, the holdout "
                         "bar). 0.8 is for exploratory loops only: at 0.8 "
                         "an unchanged app with flaky cases is kept 6-20%% "
                         "of the time per look")
    ap.add_argument("--noise-floor", nargs=2, metavar=("CHAMPION_A",
                                                         "CHAMPION_B"),
                    help="two verdicts JSONL files from holdout runs of the "
                         "SAME unchanged champion; when the candidate "
                         "changed no more cases than they disagree on, keep "
                         "is forced false ('no evidence')")
    ap.add_argument("--candidate-looks", type=int, metavar="N",
                    help="candidate comparisons made against this holdout "
                         "seal, this one included; prints a multi-look "
                         "false-keep bound")
    ap.add_argument("--holdout-power", type=int, metavar="H",
                    help="planning mode, no verdict files: for a sealed "
                         "holdout of H cases, print the minimum detectable "
                         "effect and the smallest fixed/broken split that "
                         "could keep (optimize step 4, before the noise pair)")
    ap.add_argument("--headroom", type=float, metavar="C",
                    help="with --holdout-power: holdout cases a candidate "
                         "fixing its whole cluster could turn to pass "
                         "(an estimate; may be fractional)")
    ap.add_argument("--noise-disagreements", type=int, default=0,
                    metavar="D",
                    help="with --holdout-power: the noise pair's "
                         "disagreements once measured (default: 0, before "
                         "the pair)")
    a = ap.parse_args()
    # Out of range, --alpha reached NormalDist().inv_cdf and exited with a
    # StatisticsError traceback (see _common.require_range for why this is not
    # an argparse `type=` callable).
    require_range("--alpha", a.alpha, 0, 1, exclusive=True)
    require_range("--bayes-threshold", a.bayes_threshold, 0, 1,
                  note=" (it is a probability)")

    if a.holdout_power is not None:
        if a.baseline or a.noise_floor or a.candidate_looks is not None:
            die("--holdout-power is a planning mode: it takes no verdict "
                "files, --noise-floor or --candidate-looks")
        if a.holdout_power < 1:
            die(f"--holdout-power must be >= 1 (got {a.holdout_power})")
        if a.headroom is None:
            die("--holdout-power needs --headroom C (optimize step 4 says "
                "how to estimate it)")
        require_range("--headroom", a.headroom, 0, a.holdout_power,
                      note=" (it is a count of holdout cases)")
        if a.noise_disagreements < 0:
            die("--noise-disagreements must be >= 0 (got "
                f"{a.noise_disagreements})")
        print(json.dumps(holdout_power(
            a.holdout_power, a.headroom, a.noise_disagreements, a.alpha,
            a.bayes_threshold), indent=2))
        return
    if a.headroom is not None or a.noise_disagreements:
        die("--headroom and --noise-disagreements belong to --holdout-power")
    if not a.candidate:
        die("two verdict files are required (baseline, candidate), or "
            "--holdout-power H for the planning mode")

    if a.candidate_looks is not None and a.candidate_looks < 1:
        die(f"--candidate-looks must be >= 1 (got {a.candidate_looks}): it "
            "counts this comparison too")

    base, cand = load(a.baseline), load(a.candidate)
    common = sorted(set(base) & set(cand))
    if not common:
        die("no common case ids - runs not comparable (check dataset_version)")
    # Attrition is REPORTED, never silently absorbed. The pairing above is an
    # intersection, and the cases it drops are not a random sample: a case the
    # candidate crashed on is filtered upstream as infra and simply vanishes
    # from the comparison, so the candidate is graded on the subset it survived.
    # A run that dropped a third of the dataset and one that dropped nothing
    # would otherwise print byte-identical output. The decision rule ignores
    # attrition — the gate runs on the pairs it has — but the reader is told
    # what is missing.
    base_only = sorted(set(base) - set(cand))
    cand_only = sorted(set(cand) - set(base))

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
        "cases_only_in_baseline": len(base_only),
        "cases_only_in_candidate": len(cand_only),
        "cases_only_in_baseline_ids": base_only[:MAX_REPORTED_IDS],
        "cases_only_in_candidate_ids": cand_only[:MAX_REPORTED_IDS],
        "min_detectable_effect_at_this_n": round(mde, 3),
        "min_detectable_effect_explained": (
            f"at n={n} (alpha={a.alpha}, 80% power) — a normal-approximation "
            "planning estimate, not an exact bound like the tests below; "
            "only a true pass-rate "
            f"swing of about {mde:.0%} or larger is reliably distinguishable "
            "from noise here; an observed delta smaller than that may be "
            "real but this dataset is too small to tell it apart from "
            "chance - treat a sub-MDE 'keep' as not proven, not as no "
            "effect."),
    }

    base_drop = len(base_only) / len(base)
    cand_drop = len(cand_only) / len(cand)
    if max(base_drop, cand_drop) > ATTRITION_WARN_FRACTION:
        out["case_attrition_warning"] = (
            f"SELECTION BIAS RISK: {len(base_only)} of {len(base)} baseline "
            f"case(s) ({base_drop:.0%}) and {len(cand_only)} of {len(cand)} "
            f"candidate case(s) ({cand_drop:.0%}) are missing from the other "
            f"run, so this comparison covers only {n} paired case(s). The "
            "dropped cases are not a random sample: a case the candidate "
            "crashed or timed out on is filtered upstream as an infra "
            "verdict and disappears from the gate entirely, which grades the "
            "candidate on the subset it survived and biases the result toward "
            "keep. Reconcile the two case sets (check dataset_version and the "
            "upstream infra filter) before trusting this decision.")

    prob = posterior_prob_improvement(b01, b10)
    sign_p = binom_one_sided_p(b01, b01 + b10)
    mcnemar_p = mcnemar_exact_p_two_sided(b01, b10)
    changed = noise = None
    uncovered = []
    if a.noise_floor:
        run_a, run_b = load(a.noise_floor[0]), load(a.noise_floor[1])
        noise, covered = noise_floor(run_a, run_b, common)
        if not covered:
            die("--noise-floor: the two champion runs share no case id with "
                "the paired baseline/candidate cases - they are not runs of "
                "this holdout")
        changed = b01 + b10
        uncovered = [c for c in common if base[c] != cand[c]
                     and not (c in run_a and c in run_b)]
    keep, no_evidence = keep_decision(prob, delta, a.bayes_threshold,
                                      changed, noise, len(uncovered))
    floor_applied = bool(a.noise_floor) and not uncovered
    rule = f"P(candidate better) >= {a.bayes_threshold} and delta > 0"
    if a.noise_floor:
        rule += (" and every changed case is in both champion runs and "
                 "cases changed (fixed + broken) > the champion's run-to-run "
                 "disagreements")
    out.update({
        "method": "bayes-beta exact posterior (all n)",
        "decision_rule": rule,
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
        "keep": keep,
    })
    if a.noise_floor:
        out["noise_floor"] = {
            "champion_disagreements": noise,
            "cases_counted": covered,
            "candidate_changed": changed,
            "changed_cases_not_in_champion_runs": len(uncovered),
            "applied": floor_applied,
        }
        if uncovered:
            out["noise_floor"]["coverage_warning"] = (
                f"the champion runs miss {len(uncovered)} of the {changed} "
                "case(s) the candidate changed: the floor does not apply, so "
                "no keep")
        elif covered < n:
            out["noise_floor"]["coverage_warning"] = (
                f"the champion runs cover {covered} of the {n} paired "
                "case(s), including every case the candidate changed; both "
                "counts are taken on those")
    if no_evidence:
        out["no_evidence"] = no_evidence
    if a.candidate_looks is not None:
        if a.bayes_threshold >= DEFAULT_BAYES_THRESHOLD:
            per_look = PER_LOOK_FALSE_KEEP[floor_applied]
            bound = min(1.0, a.candidate_looks * per_look)
            out["multi_look_false_keep_bound"] = round(bound, 4)
            out["multi_look_explained"] = (
                f"{a.candidate_looks} candidate look(s) at this seal x a "
                f"simulated per-look false-keep rate of at most {per_look:.0%} "
                f"({'with' if floor_applied else 'without'} the noise floor): "
                f"an unchanged app is kept by at least one of these looks at "
                f"most {bound:.0%} of the time (union bound), under the "
                "simulation's assumptions (multi_look_assumptions).")
            out["multi_look_assumptions"] = (
                "independent cases: flaky cases that fail together exceed "
                "the bound (6 of 20 cases flipping at once: 12.5% per look, "
                "21.9% over three); and the champion the noise pair "
                "measured: after a keep, the floor describes the previous "
                "app, so later looks are not covered.")
        else:
            out["multi_look_false_keep_bound"] = None
            out["multi_look_explained"] = (
                f"not bounded: the per-look rate was simulated at "
                f"--bayes-threshold {DEFAULT_BAYES_THRESHOLD} or stricter; at "
                f"{a.bayes_threshold} an unchanged app with flaky cases can be "
                "kept on 6-20% of looks.")
    if out["keep"] and not out["sign_test_significant_at_alpha"]:
        out["gate_note"] = (
            f"kept on the posterior bar (P={prob:.3f} >= {a.bayes_threshold}) "
            f"while the one-sided sign test is not significant at "
            f"alpha={a.alpha} (p={sign_p:.4f}). The posterior is the "
            "permissive bar; say 'posterior probability >= "
            f"{a.bayes_threshold}', never 'statistically significant'.")

    # The gate's own MDE text ends "treat a sub-MDE 'keep' as not proven, not
    # as no effect" — and this tells the caller which runs those are. The
    # other two guards miss this shape: gate_note fires only when the
    # sign test is NOT significant (a 5-for-0 discordant split is significant,
    # p=0.031), and `advice` below is gated behind `not keep`. So without
    # this, the one decision where the reader most needs the caveat — keep, on
    # an observed effect smaller than the smallest this dataset can resolve —
    # would print no caveat at all, and optimize §5 would pin it as the new
    # baseline reporting a bare "kept".
    #
    # Emitted BESIDE keep, never instead of it. The decision rule does not use
    # it (the posterior is exact and does not depend on the MDE, which is a
    # normal-approximation planning figure); it only makes the run say out
    # loud that its evidence is thinner than its own resolution.
    if out["keep"] and abs(delta) < mde:
        out["sub_mde_keep"] = (
            f"KEPT BUT NOT PROVEN: the observed delta {delta:+.1%} is smaller "
            f"than the ~{mde:.0%} minimum detectable effect at n={n}, so this "
            "dataset cannot distinguish it from noise. The posterior bar it "
            "was kept on is a real signal about DIRECTION "
            f"(P(candidate better)={prob:.3f}), not proof of SIZE: do not "
            "quote the delta as a measured improvement, and grow the dataset "
            "(evalup:generate) before treating it as one.")

    if abs(delta) < mde and not out["keep"]:
        if delta == 0:
            out["advice"] = (
                f"no observed difference; at n={n} only effects of ~{mde:.0%} "
                "or larger are detectable - grow the dataset "
                "(evalup:generate) rather than re-rolling candidates")
        else:
            need = math.ceil(z_sum(a.alpha, 0.8) ** 2 *
                             max(p_disc, 1.0 / n) / abs(delta) ** 2)
            out["advice"] = (
                f"observed delta {delta:+.1%} is below the ~{mde:.0%} "
                f"detectable at n={n}; to detect ~{abs(delta):.1%} reliably "
                f"you need ~{need} cases - grow the dataset "
                "(evalup:generate) rather than re-rolling candidates")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
