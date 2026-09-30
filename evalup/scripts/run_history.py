#!/usr/bin/env python3
"""Longitudinal view over a state dir's runs for evalup. Stdlib only.

Every comparison this harness makes is PAIRWISE: `stats.py` pairs one candidate
against one baseline, and `run_cases.py` SS5.6 refuses that pair unless the two
runs agree on `dataset_version`, `harness_version` and `k`. Nothing ever looked
at more than two runs at once, so five consecutive "within noise" diffs could
hide a ten-point erosion and no artifact would say so. This script is that
missing view: it groups a `reports/` directory into COMPARABLE SERIES, emits a
time series per metric, tests each for monotone drift, and builds a per-case
journal ("passed runs 1-7, failed after run 8").

It reads. It writes nothing except the JSON it prints (or `-o`), invokes no
scorer, and re-derives no verdict -- every number below is copied out of an
artifact some other component already computed.

WHAT IT DOES NOT DECIDE. There is exactly one definition of "this change is
better" in this package and it lives in `stats.py`'s keep rule (an exact Beta
posterior over discordant pairs). A drift flag here is a DESCRIPTION of a
sequence, never a verdict on it: the output carries no `keep`, no "improved",
no "regressed", and the drift test is two-sided precisely so this script never
has to encode which direction is good for which metric (down is bad for
`pass_rate`, up is bad for `infra_rate`). Read the direction, then go run
`stats.py` on the pair you care about.

WHAT COUNTS AS ONE SERIES. SS5.6 enforces three keys on a PAIR; a series of N
needs those and two more, because a pair is joined by case-id INTERSECTION and
a series of aggregates has no intersection step to absorb a mismatch:
  dataset_version, harness_version, k  -- SS5.6/D7 verbatim. A different
      dataset or harness changes what a verdict MEANS; a different k pairs
      pass^k verdicts computed over different numbers of trials.
  mode + selecting_split -- NOT in SS5.6's pair rule, and it has to be here.
      `selecting_split` decides which cases are selected at all (the runner
      refuses a case whose `split` does not contain it), so a 4-case smoke run
      and a 200-case full run produce two pass rates that are not two points on
      one line. `stats.py` survives that mismatch because it intersects and
      warns about attrition; a mean has nothing to intersect.
  capability_matrix -- the set of ENABLED layers. Turning a layer off changes
      what `pass` means: since Step 10, a case whose real layers all come back
      `unscored` rolls up `unscored` rather than riding `http` to a pass, so a
      run with `trajectory` disabled scores the same app differently.
The app's own `git_sha` is deliberately NOT a series key, though SS5.6-style
reasoning would suggest it: the app changing is the INDEPENDENT VARIABLE. A
series keyed on it would have one point per key and could never show a trend.
It is recorded per point instead, so a step change can be attributed.
Two things that are NOT keys because they are OUTCOMES rather than declared
configuration: `traces.disabled_layers` (a run where trace collection failed)
and `summary.unscorable_layers`. Those vary within a series and are reported as
a warning, not used to split it -- splitting on them would hide the very
degradation the reader needs to see.

WHICH RUNS COUNT. Only `summary.status == "ok"`. That is not a new rule: SS9(c)
writes `"ok"` only after the completeness check passes, and `run/SKILL.md` SS3
says an incomplete run is "not quotable, not a baseline". A run that predates
the runner has no `summary.status` at all, and the ones this engine replaced
were also missing `selecting_split` and `capability_matrix` from their
manifests, so they could not be placed in a series even if they were quotable.
Excluded runs are NAMED with their reason, never dropped silently.

WHAT A TIME SERIES CAN ACTUALLY READ, per run, field by field. `REVIEW` SS4.9
says the data is "already on disk"; it is, but not in one file and not all of
it. `results.json` carries no timestamp, no `k`, no `selecting_split` and no
`capability_matrix`, so the comparability keys come from `manifest.yaml` and
the numbers from `results.json` -- both are REQUIRED_ALWAYS (SS9(b)), so this
needs no new artifact and no contract change:
  manifest.yaml  -> run_id, started_at, mode, k, selecting_split,
                    dataset_version, harness_version, capability_matrix,
                    app.git_sha/.git_tree, cases[].sha256, traces
  results.json   -> summary.{n,passes,failures,gating_failures,unscored,
                    skipped,infra_rate,crash_rate,scorer_errors,canaries,
                    holdout}, cases[].{case_id,verdict}
  routing_report.json -> accuracy, macro_f1   (only when a case was
                    routing-scorable; SS6's `# only when:`)
  reliability.json    -> pass_hat_k, flakiness_gap  (only when k > 1)
`REVIEW` SS4.9 also calls one of these a "flakiness ledger". There is no such
artifact and never was: the flakiness signal is `reliability.json`, written by
`reduce_repeats.py` and present ONLY when k > 1. Since k is a series key, a
series either has it on every point or on none, and a metric present on some
runs and not others is skipped with its reason rather than plotted ragged.
NOT read, and each for a stated reason: per-case `latency_s` IS on disk in
`results.json`, but a mean over a heavy-tailed, retry-contaminated distribution
is the weakest possible summary of it, and cost is not on this line at all --
`score_cost.py` owns both. Cost in particular cannot be a series metric even in
principle: the price table is DATED and the runs are not, so two points on one
line could differ only because the vendor changed its price sheet between them.

THE HOLDOUT SEAL holds by construction. `results.json` carries holdout cases as
an AGGREGATE ONLY (`summary.holdout`) and no holdout `case_id` appears in the
file at all, so the per-case journal -- which reads `results.json.cases` -- can
name none. Note the asymmetry this script has to respect: `summary.passes` and
`summary.failures` DO include holdout cases while `results.json.cases` does
not, so `visible_pass_rate` is reported beside `pass_rate` by subtracting
`summary.holdout`'s passes and failures. Both rates divide by the SCORED cases
(passes + failures), never by `summary.n`, which counts skipped and infra
cases too (runner-contract SS9). `verdicts.jsonl` is the other
per-case record and it is NOT read here, precisely because it carries holdout
ids. The `datasets/holdout-looks.jsonl` ledger is likewise untouched: reading
it is not a look, but this script has no use for the count either.

THE DRIFT STATISTIC: EXACT MANN-KENDALL, conditioned on the observed ties.
`stats.py` sets the house standard -- exact at every n, approximations only
BELOW a decision and labelled -- and `score_agreement.py` followed it by giving
kappa Fisher's exact p rather than an asymptotic interval. So:
  S = (concordant - discordant) over all C(n,2) pairs taken in time order.
  Under H0 "no trend", every ordering of the observed multiset is equally
  likely, and the number of orderings with exactly d inversions is the
  coefficient of q^d in the Gaussian multinomial [n; m1..mr]_q -- computed here
  as an exact integer polynomial division of q-factorials. That IS the exact
  null distribution, ties included; no variance formula, no normal
  approximation, no continuity correction anywhere in this file.
  p is a tail sum of that distribution. The two-sided p doubles the smaller
  tail (the distribution is symmetric about its mean by construction).
WHY MANN-KENDALL AND NOT THE ALTERNATIVES, at the n an eval history has (five
to fifteen runs before a dataset_version bump resets the series):
  Cox-Stuart splits the series in half and sign-tests floor(n/2) paired
  comparisons, discarding the middle point and all ordering within each half.
  At n=6 that is 3 pairs, whose smallest attainable one-sided p is 0.125 -- it
  cannot reject at alpha=0.05 no matter what the data does. Exact Mann-Kendall
  uses all 15 pairs and reaches 1/720. A test that cannot produce a flag at the
  n we have is not a conservative choice, it is a decorative one.
  Page's trend test needs replication within each ordered condition. There is
  one aggregate per run, so applied to the series it degenerates. Applied
  instead to the per-case matrix (cases as blocks, runs as conditions) it needs
  the SAME case set in every run, which SS5.6's attrition rules say we may not
  assume -- and the journal below reports attrition rather than requiring its
  absence.
WHAT MANN-KENDALL DOES NOT TEST, stated so the flag is not overread:
  - Not magnitude. It detects a monotone TENDENCY; a 0.2pp-per-run slide and a
    20pp collapse both flag. Read `first`, `last` and `change` beside it.
  - Not a change point. A suite that dropped once and stayed flat is not
    monotone, and this test is entitled to miss it. SS4.9's own scenario (five
    consecutive small erosions) is monotone, which is why this is the right
    test for it and not a universal one.
  - Not stability. A non-flag is not evidence of no drift; `p_floor` says
    whether rejection was attainable at this n at all, and `underpowered` says
    so outright when it was not.
  - Not weighted by each run's own precision. A pass rate over 4 cases and one
    over 400 are one observation each. The test is still correctly calibrated
    under H0 (order-exchangeability is all it assumes), but it spends no extra
    evidence on the larger run.
  - No multiplicity correction across the metrics tested, matching `stats.py`'s
    declared limits. `metrics_tested` is reported so the reader can do it.

THE PER-CASE JOURNAL keys on `case_id` and treats absence as ABSENCE -- never
as a failure, which is the rule SS5.6 applies to attrition. Added and removed
cases are reported (`absent_in`, `runs_present`), and a series whose case set
churns past `stats.py`'s own 10% attrition threshold gets that script's warning
restated at series level. It also uses a signal the pairwise diff never had:
`manifest.yaml` records a `sha256` per case, so the SAME id with DIFFERENT
content across two runs of one series is detectable. That is a silently-broken
comparison -- the case was edited without a `dataset_version` bump, so SS5.6's
version check let the pair through -- and it is reported as
`content_changed_at`. The journal lives here and NOT in
`build_review_viewer.py` (which SS4.9 also suggests): that script takes ONE run
path, and its whole job is annotating traces for calibration. Teaching it to
walk N run directories would give it a second input model and put "which run
comes next" in a UI file.

OUTPUT: JSON on stdout, or to `-o PATH`. Nothing is written into a run
directory, on purpose. SS6 says the runner's tree is the complete list of files
under `--out` and a test machine-diffs its `# only when:` markers against
`REQUIRED_IF`, so a history file inside a run dir would need a contract edit --
and would be wrong anyway: history is a property of `reports/`, not of any one
run, so run N's copy goes stale the moment run N+1 lands and `--verify` months
later would demand a file about runs that did not exist yet. `-o
reports/history.json` puts it beside `reports/baseline.json`, which is the
existing precedent for a non-run file in that directory.
Errors go to STDOUT as {"error": ...} with exit 2 (_common.die).

Usage: run_history.py <reports-dir | run-dir...> [--alpha 0.05]
                      [--all-cases] [--max-cases N] [--max-exact-runs N]
                      [-o history.json]

See also: stats.py (two runs, one decision), reduce_repeats.py (one run, k
repeats), score_agreement.py (the judge against a human).
"""
import argparse
import json
import os

from _common import (
    HARNESS_VERSION,
    add_version_flag,
    die,
    infra_rate,
    load_json,
    require_range,
    write_output,
)

# SS9(b): both are REQUIRED_ALWAYS, so a directory with neither is not a run.
MANIFEST = "manifest.yaml"
RESULTS = "results.json"
# Conditional per SS6's `# only when:` markers. Read when present, and a metric
# is emitted only when EVERY run in the series has it (see series_metrics).
ROUTING_REPORT = "routing_report.json"
RELIABILITY = "reliability.json"

# SS9(c) writes this only after the completeness check passes, and run/SKILL.md
# SS3 calls anything else "not quotable, not a baseline".
QUOTABLE_STATUS = "ok"

# stats.py's ATTRITION_WARN_FRACTION, restated at series scope rather than
# redefined: the same 10% of the case set going missing means the same thing.
ATTRITION_WARN_FRACTION = 0.1
# stats.py's MAX_REPORTED_IDS, in spirit. A journal is for reading, not for
# holding every id in a 400-case suite.
MAX_REPORTED_CASES = 40
# The exact null costs one integer polynomial per series: ~0.1s at 60 runs, ~2s
# at 120, ~22s at 200. Past the cap the series is still emitted and the drift
# block says it was not tested -- this file substitutes no approximation for an
# exact test it declined to run.
MAX_EXACT_RUNS = 100


class BadRun(Exception):
    """One unreadable run must not sink the report; SS9's whole point is that a
    missing artifact is NAMED rather than silently absorbed."""


def _raise(message):
    raise BadRun(message)


def read_run_json(path):
    """Read one run artifact through _common's error contract, as an object."""
    doc = load_json(path, on_error=_raise)
    if not isinstance(doc, dict):
        _raise(f"{os.path.basename(path)}: expected a JSON object, got "
               f"{type(doc).__name__}")
    return doc


def read_optional(path):
    """A conditional artifact (SS6). Absent is a fact, not an error."""
    if not os.path.isfile(path):
        return None
    try:
        return read_run_json(path)
    except BadRun:
        return None


# -- run discovery ---------------------------------------------------------

def discover(paths):
    """Expand each argument into run directories, in no particular order.

    A path holding a manifest.yaml IS a run directory; anything else is scanned
    one level for children that are. One level, not a walk: `cases/` sits
    inside every run and a recursive scan would rediscover the same run through
    its own subdirectories.
    """
    found, roots = [], []
    for path in paths:
        if not os.path.isdir(path):
            die(f"{path} is not a directory (expected a reports/ directory or "
                "a run directory)")
        if os.path.isfile(os.path.join(path, MANIFEST)):
            found.append(os.path.abspath(path))
            continue
        roots.append(path)
        for name in sorted(os.listdir(path)):
            child = os.path.join(path, name)
            # reports/ also holds baseline.json and .gitignore (SS6); those are
            # files, so they are not candidates.
            if os.path.isdir(child):
                found.append(os.path.abspath(child))
    if not found:
        die("no run directory found under {} -- a run directory is one "
            "containing {}.".format(
                ", ".join(roots) or ", ".join(paths), MANIFEST))
    # De-duplicated because `reports/ reports/<id>` names the same run twice.
    return sorted(set(found))


def run_id_time(run_id):
    """The `<mode>-<UTC>` stamp in the id, as a sortable string.

    An ordering fallback only, for a run whose manifest predates `started_at`.
    Lexicographic run-id order is WRONG across modes (`full-20260101...` sorts
    before `smoke-20251201...`), which is the whole reason the timestamp is
    pulled out rather than the id sorted directly.
    """
    if isinstance(run_id, str) and "-" in run_id:
        stamp = run_id.rsplit("-", 1)[1]
        if len(stamp) == 16 and stamp.endswith("Z") and stamp[8] == "T":
            return stamp
    return ""


def load_point(run_dir):
    """One run -> one series point, or BadRun with the reason it is excluded."""
    manifest = read_run_json(os.path.join(run_dir, MANIFEST))
    results = read_run_json(os.path.join(run_dir, RESULTS))
    summary = results.get("summary")
    if not isinstance(summary, dict):
        _raise("results.json has no summary object")
    status = summary.get("status")
    if status != QUOTABLE_STATUS:
        _raise(
            "summary.status is {!r}, not {!r}; SS9(c) writes {!r} only after "
            "the completeness check passes, and run/SKILL.md SS3 calls "
            "anything else not quotable and not a baseline{}".format(
                status, QUOTABLE_STATUS, QUOTABLE_STATUS,
                " (a run directory written before run_cases.py existed has no "
                "status at all)" if status is None else ""))
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        _raise("manifest.yaml has no run_id")
    app = manifest.get("app") if isinstance(manifest.get("app"), dict) else {}
    traces = manifest.get("traces") \
        if isinstance(manifest.get("traces"), dict) else {}
    return {
        "run_id": run_id,
        "dir": run_dir,
        "started_at": manifest.get("started_at"),
        "order": (manifest.get("started_at") or run_id_time(run_id), run_id),
        "app_git_sha": app.get("git_sha"),
        "app_git_tree": app.get("git_tree"),
        "disabled_layers": traces.get("disabled_layers"),
        "key": series_key(manifest),
        "case_sha256": {c.get("id"): c.get("sha256")
                        for c in (manifest.get("cases") or [])
                        if isinstance(c, dict)},
        "summary": summary,
        "case_verdicts": {row.get("case_id"): row.get("verdict")
                          for row in (results.get("cases") or [])
                          if isinstance(row, dict) and row.get("case_id")},
        "routing": read_optional(os.path.join(run_dir, ROUTING_REPORT)),
        "reliability": read_optional(os.path.join(run_dir, RELIABILITY)),
    }


def capability_fingerprint(matrix):
    """The set of ENABLED layers, as a stable string.

    Only the `enabled` flag: `blocked_by` is prose explaining a disabled layer,
    and two runs that disagree on its wording measured the same thing.
    """
    if not isinstance(matrix, dict):
        return None
    return "+".join(sorted(
        name for name, block in matrix.items()
        if isinstance(block, dict) and block.get("enabled") is not False)) \
        or "(none)"


def series_key(manifest):
    """SS5.6's three keys, plus the two a series of N needs and a pair does
    not. See the module docstring for the argument."""
    return {
        "mode": manifest.get("mode"),
        "selecting_split": manifest.get("selecting_split"),
        "k": manifest.get("k"),
        "dataset_version": manifest.get("dataset_version"),
        "harness_version": manifest.get("harness_version"),
        "capabilities": capability_fingerprint(
            manifest.get("capability_matrix")),
    }


# -- the exact Mann-Kendall null -------------------------------------------

def poly_mul(a, b):
    out = [0] * (len(a) + len(b) - 1)
    for i, x in enumerate(a):
        if x:
            for j, y in enumerate(b):
                out[i + j] += x * y
    return out


def q_factorial(n):
    """[n]_q! = prod_{i=1..n} (1 + q + ... + q^(i-1)), exact integer coefficients.

    Its coefficient of q^d counts the permutations of n distinct items with
    exactly d inversions (the Mahonian numbers).
    """
    poly = [1]
    for i in range(1, n + 1):
        poly = poly_mul(poly, [1] * i)
    return poly


def poly_divide_exact(num, den):
    """num / den for integer polynomials, from the LOW end. den[0] must be 1.

    Exact by construction: every q-factorial has constant term 1, and the
    quotient here is a Gaussian multinomial, which is known to be a polynomial
    with non-negative integer coefficients. The remainder is ASSERTED zero
    rather than assumed -- a silent rounding here would corrupt the null
    distribution every p-value below is a tail of.
    """
    quotient = [0] * (len(num) - len(den) + 1)
    rest = list(num)
    for i in range(len(quotient)):
        coefficient = rest[i]
        quotient[i] = coefficient
        if coefficient:
            for j, d in enumerate(den):
                rest[i + j] -= coefficient * d
    if any(rest):
        raise ArithmeticError("q-multinomial division left a remainder")
    return quotient


def inversion_distribution(group_sizes):
    """Exact counts of orderings by inversion number, for a multiset.

    The Gaussian multinomial [n; m1..mr]_q = [n]_q! / prod [mi]_q!. Conditioning
    on the observed tie pattern is the same move `score_agreement.py` makes when
    it conditions Fisher's exact test on both margins: it is what makes the null
    exact in the presence of ties instead of needing a tie-corrected variance.
    """
    denominator = [1]
    for size in group_sizes:
        denominator = poly_mul(denominator, q_factorial(size))
    return poly_divide_exact(q_factorial(sum(group_sizes)), denominator)


def mann_kendall_exact(values, alpha, max_runs=MAX_EXACT_RUNS):
    """Exact two-sided Mann-Kendall over `values` in time order.

    `flagged` is two-sided on purpose: this script does not know which
    direction is good for which metric, and encoding that would be a second
    definition of "better" beside stats.py's keep rule.
    """
    n = len(values)
    if n < 2:
        return {"tested": False,
                "reason": "a trend needs at least two runs; this series has "
                          f"{n}"}
    if n > max_runs:
        return {"tested": False,
                "reason": f"{n} runs exceeds --max-exact-runs {max_runs}; the "
                          "exact null distribution is an integer polynomial of "
                          f"degree {n * (n - 1) // 2} and this script "
                          "substitutes no approximation for it -- raise the "
                          "flag to spend the time"}
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    sizes = sorted(counts.values())
    pairs = n * (n - 1) // 2 - sum(m * (m - 1) // 2 for m in sizes)
    if not pairs:
        return {"tested": False,
                "reason": "every run has the same value, so no pair is "
                          "comparable and no ordering is more extreme than "
                          "another"}
    discordant = sum(1 for i in range(n) for j in range(i + 1, n)
                     if values[i] > values[j])
    s = pairs - 2 * discordant
    dist = inversion_distribution(sizes)
    total = sum(dist)
    # An increasing trend means FEW inversions, so it is the left tail on d.
    p_increasing = sum(dist[:discordant + 1]) / total
    p_decreasing = sum(dist[discordant:]) / total
    two_sided = min(1.0, 2 * min(p_increasing, p_decreasing))
    # The smallest p this n and this tie pattern can produce at all. Without
    # it, a non-flag reads as evidence of stability when rejection may have
    # been arithmetically impossible.
    p_floor = min(1.0, 2 * min(dist[0], dist[-1]) / total)
    return {
        "tested": True,
        "test": "exact Mann-Kendall (Gaussian-multinomial null, conditioned "
                "on ties)",
        "s": s,
        "concordant_pairs": pairs - discordant,
        "discordant_pairs": discordant,
        "comparable_pairs": pairs,
        "orderings": total,
        "direction": "increasing" if s > 0
                     else ("decreasing" if s < 0 else "flat"),
        "p_increasing_one_sided": round(p_increasing, 6),
        "p_decreasing_one_sided": round(p_decreasing, 6),
        "p_two_sided": round(two_sided, 6),
        "p_floor": round(p_floor, 6),
        "underpowered": bool(p_floor >= alpha),
        "alpha": alpha,
        "flagged": bool(two_sided < alpha),
        "note": "a monotone TENDENCY, not a magnitude and not a change point; "
                "read `change` beside it, and see stats.py for whether a "
                "specific pair of runs is a keep",
    }


# -- metrics ---------------------------------------------------------------

def ratio(numerator, denominator):
    if not isinstance(numerator, (int, float)) \
            or not isinstance(denominator, (int, float)) or not denominator:
        return None
    return round(numerator / denominator, 6)


def number(value):
    """A metric value, or None when the artifact did not record one.

    bool is excluded because it is an int subclass and no metric here is a
    flag; a True silently plotted as 1.0 would be a fabricated data point.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def point_metrics(point):
    """Every metric this run contributes, name -> value or None.

    None means "this run did not record it", which is what makes a metric
    present on some runs and absent on others detectable rather than ragged.
    """
    summary = point["summary"]
    n = number(summary.get("n"))
    passes = number(summary.get("passes"))
    failures = number(summary.get("failures"))
    # runner-contract SS9: `n` is every non-canary case SELECTED, skipped
    # and infra included; pass/fail rates divide by the SCORED cases
    # (passes + failures) because SS7 keeps skipped and infra out of every
    # pass/fail denominator. Dividing by `n` printed 0.6 for a 3-pass,
    # 1-fail, 1-skipped run (F-135). The shares of what was selected --
    # unscored, skipped -- stay over `n`.
    scored = None if None in (passes, failures) else passes + failures
    metrics = {
        "pass_rate": ratio(passes, scored),
        "gating_failure_rate": ratio(number(summary.get("gating_failures")),
                                     scored),
        "unscored_rate": ratio(number(summary.get("unscored")), n),
        "skipped_rate": ratio(number(summary.get("skipped")), n),
        # Recomputed, not read: runs before the 2026-09-26 user-test fix
        # divided by `attempted` (canaries in) inside the same harness
        # version, and a series mixing the two would show the definition
        # change as drift. Every run carries these counts (SS10's table).
        "infra_rate": recomputed_infra_rate(summary, n),
        "crash_rate": number(summary.get("crash_rate")),
        "scorer_errors": number(summary.get("scorer_errors")),
        "cases_scored": scored,
    }
    canaries = summary.get("canaries")
    if isinstance(canaries, dict):
        metrics["canary_pass_rate"] = ratio(number(canaries.get("passed")),
                                            number(canaries.get("n")))
    # THE SEAL: aggregate counts, never ids. summary.passes and .failures
    # INCLUDE holdout cases while results.json.cases excludes them, so the
    # visible rate is reported separately rather than left to differ from
    # pass_rate without saying why. A run older than holdout.failures has no
    # scored holdout denominator, and gets neither rate rather than a
    # different one.
    holdout = summary.get("holdout")
    if isinstance(holdout, dict):
        h_pass = number(holdout.get("passes"))
        h_fail = number(holdout.get("failures"))
        if None not in (h_pass, h_fail):
            metrics["holdout_pass_rate"] = ratio(h_pass, h_pass + h_fail)
            if scored is not None:
                metrics["visible_pass_rate"] = ratio(
                    passes - h_pass, scored - h_pass - h_fail)
    routing = point["routing"]
    if isinstance(routing, dict):
        metrics["routing_accuracy"] = number(routing.get("accuracy"))
        metrics["routing_macro_f1"] = number(routing.get("macro_f1"))
    reliability = point["reliability"]
    if isinstance(reliability, dict):
        metrics["pass_hat_k"] = number(reliability.get("pass_hat_k"))
        metrics["flakiness_gap"] = number(reliability.get("flakiness_gap"))
    return metrics


def recomputed_infra_rate(summary, n):
    infra, skipped = number(summary.get("infra_errors")), \
        number(summary.get("skipped"))
    if None in (infra, skipped, n):
        return number(summary.get("infra_rate"))
    return infra_rate(infra, n, skipped, ndigits=6)


def undefined_reason(point, name):
    """Why a run holds no value for a metric it did record the inputs of.

    None means the plain case -- the run's files do not carry it (SS6's
    `# only when:`). The two others are not that, and saying "see SS6" for
    them sent the reader to the wrong section: a rate over zero scored cases
    is 0/0, and a holdout run older than summary.holdout.failures has no
    scored holdout denominator.
    """
    summary = point["summary"]
    passes, failures = number(summary.get("passes")), \
        number(summary.get("failures"))
    holdout = summary.get("holdout")
    if name in ("holdout_pass_rate", "visible_pass_rate"):
        if not isinstance(holdout, dict):
            return None
        if number(holdout.get("failures")) is None:
            return ("its summary.holdout has no `failures` (a run older than "
                    "the 2026-09-26 user-test fixes), so it has no scored "
                    "holdout denominator")
    elif name not in ("pass_rate", "gating_failure_rate"):
        return None
    if None in (passes, failures):
        return None
    return "no case was scored pass or fail there, so the rate is 0/0"


def series_metrics(points, alpha, max_runs):
    """Time series + drift per metric, and the metrics that are not one series.

    A metric is emitted only when EVERY run in the series recorded it. A line
    with holes is not a time series: the drift test would silently run on a
    different subset of runs than the one the reader is looking at.
    """
    per_run = [point_metrics(p) for p in points]
    names = sorted({name for m in per_run for name in m})
    out, skipped = {}, {}
    for name in names:
        values = [m.get(name) for m in per_run]
        absent = [points[i]["run_id"]
                  for i, v in enumerate(values) if v is None]
        if len(absent) == len(values):
            # Not applicable rather than ragged: no canaries in the suite, no
            # routing-scorable case, k=1 for the whole series.
            skipped[name] = ("no run in this series recorded it -- see SS6's "
                             "`# only when:` conditions")
            continue
        if absent:
            first = next(i for i, v in enumerate(values) if v is None)
            why = undefined_reason(points[first], name) or (
                "see SS6's `# only when:` conditions")
            skipped[name] = (
                f"not recorded by {len(absent)} of {len(values)} run(s) "
                f"(e.g. {absent[0]}: {why}), so it is not one series")
            continue
        out[name] = {
            "values": values,
            "first": values[0],
            "last": values[-1],
            "change": round(values[-1] - values[0], 6),
            "min": min(values),
            "max": max(values),
            "drift": mann_kendall_exact(values, alpha, max_runs),
        }
    return out, skipped


# -- the per-case journal --------------------------------------------------

def case_journal(points, all_cases, max_cases):
    """Per-case verdict history over one series. Absence is absence.

    Keyed on `case_id` out of `results.json.cases`, which holds NO holdout row
    (SS10's seal), so this can name no sealed case. A case missing from a run
    is recorded in `absent_in` and never imputed as a failure -- SS5.6's
    attrition rule, applied to N runs instead of two.
    """
    ever = sorted({cid for p in points for cid in p["case_verdicts"]})
    rows, changed_content, transitions_seen = [], [], 0
    for case_id in ever:
        history, absent, content, previous_sha = [], [], [], None
        for point in points:
            sha = point["case_sha256"].get(case_id)
            if sha is not None:
                if previous_sha is not None and sha != previous_sha:
                    content.append(point["run_id"])
                previous_sha = sha
            verdict = point["case_verdicts"].get(case_id)
            if verdict is None:
                absent.append(point["run_id"])
                continue
            history.append({"run_id": point["run_id"], "verdict": verdict,
                            "app_git_sha": point["app_git_sha"]})
        transitions = [
            {"from": history[i - 1]["verdict"], "to": history[i]["verdict"],
             "at": history[i]["run_id"],
             "app_git_sha": history[i]["app_git_sha"]}
            for i in range(1, len(history))
            if history[i]["verdict"] != history[i - 1]["verdict"]]
        transitions_seen += len(transitions)
        if content:
            changed_content.append(case_id)
        row = {
            "case_id": case_id,
            "runs_present": len(history),
            "runs_absent": len(absent),
            "first_seen": history[0]["run_id"] if history else None,
            "last_seen": history[-1]["run_id"] if history else None,
            "verdicts": [h["verdict"] for h in history],
            "transitions": transitions,
        }
        if absent:
            row["absent_in"] = absent[:MAX_REPORTED_CASES]
        if content:
            row["content_changed_at"] = content
        # The default is the readable journal SS4.9 asked for -- "case X passed
        # runs 1-7, failed after Y" -- not a dump of every stable case.
        if all_cases or transitions or absent or content:
            rows.append(row)
    complete = sum(1 for cid in ever
                   if all(cid in p["case_verdicts"] for p in points))
    journal = {
        "cases_ever_seen": len(ever),
        "cases_in_every_run": complete,
        "cases_with_a_transition": sum(1 for r in rows if r["transitions"]),
        "transitions": transitions_seen,
        "reported": "every case" if all_cases else
                    "only cases that changed verdict, appeared/disappeared, "
                    "or were edited",
        "cases": rows[:max_cases],
    }
    if len(rows) > max_cases:
        journal["truncated"] = (
            f"{max_cases} of {len(rows)} rows shown (--max-cases)")
    if ever and (len(ever) - complete) / len(ever) > ATTRITION_WARN_FRACTION:
        journal["case_attrition_warning"] = (
            f"SELECTION BIAS RISK: {len(ever) - complete} of {len(ever)} "
            "case(s) are missing from at least one run in this series, so the "
            "aggregate metrics above are not computed over one fixed case "
            "set. The dropped cases are not a random sample -- a case the app "
            "crashed on is filtered upstream as an infra verdict and "
            "disappears from the denominator -- which is stats.py's attrition "
            "warning applied over N runs instead of two.")
    if changed_content:
        named = ", ".join(changed_content[:MAX_REPORTED_CASES])
        journal["content_changed_warning"] = (
            f"{len(changed_content)} case(s) have the SAME case_id and a "
            f"DIFFERENT sha256 across this series ({named}). These runs share "
            "a dataset_version, so SS5.6's version check let them compare, "
            "but the case itself was edited between them and the two verdicts "
            "answer different questions. Bump dataset_version when a case's "
            "content changes.")
    return journal


# -- assembly --------------------------------------------------------------

def varying(points, field):
    """The values a per-point field takes across a series, when it takes more
    than one. Used for facts that are OUTCOMES (a failed trace collection),
    which are warned about rather than used to split the series."""
    seen = []
    for point in points:
        value = point[field]
        if isinstance(value, list):
            value = "+".join(str(v) for v in value)
        if value not in seen:
            seen.append(value)
    return seen if len(seen) > 1 else []


def build_series(points, args):
    points = sorted(points, key=lambda p: p["order"])
    metrics, skipped = series_metrics(points, args.alpha, args.max_exact_runs)
    warnings = []
    degraded = varying(points, "disabled_layers")
    if degraded:
        warnings.append(
            "the set of layers disabled at run time varies across this series "
            "({}). That is an OUTCOME, not a declared capability, so it does "
            "not split the series -- but a run whose trace collection failed "
            "scores the same app differently, and since Step 10 an "
            "all-unscored case no longer rides `http` to a pass.".format(
                "; ".join(repr(d) for d in degraded)))
    if len(points) > 1 and not varying(points, "app_git_sha"):
        warnings.append(
            "every run in this series is the same app git_sha, so any drift "
            "below is harness or environment variation, not the app changing.")
    dirty = [p["run_id"] for p in points if p["app_git_tree"] == "dirty"]
    if dirty:
        warnings.append(
            "{} run(s) were taken against a DIRTY app tree ({}), so their "
            "git_sha does not identify what was measured.".format(
                len(dirty), ", ".join(dirty[:MAX_REPORTED_CASES])))
    return {
        "key": points[0]["key"],
        "n_runs": len(points),
        "runs": [{"run_id": p["run_id"], "started_at": p["started_at"],
                  "app_git_sha": p["app_git_sha"],
                  "app_git_tree": p["app_git_tree"]} for p in points],
        "metrics": metrics,
        "metrics_not_one_series": skipped,
        "metrics_tested": sum(1 for b in metrics.values()
                              if b["drift"].get("tested")),
        "drift_flagged": sorted(name for name, block in metrics.items()
                                if block["drift"].get("flagged")),
        "case_journal": case_journal(points, args.all_cases, args.max_cases),
        "warnings": warnings,
    }


def main():
    ap = argparse.ArgumentParser(
        description="Longitudinal view over evalup runs: per-metric time "
                    "series grouped into comparable series, an exact "
                    "Mann-Kendall monotone-drift flag, and a per-case verdict "
                    "journal. Reads run directories; writes nothing but its "
                    "own JSON.")
    add_version_flag(ap)
    ap.add_argument("paths", nargs="+",
                    help="a reports/ directory, or one or more run directories")
    ap.add_argument("--alpha", type=float, default=0.05,
                    help="two-sided significance level for the drift flag; it "
                         "describes a sequence and gates no decision "
                         "(default: 0.05)")
    ap.add_argument("--all-cases", action="store_true",
                    help="include cases whose verdict never changed (default: "
                         "only cases that changed, churned, or were edited)")
    ap.add_argument("--max-cases", type=int, default=MAX_REPORTED_CASES,
                    help="journal rows per series (default: "
                         f"{MAX_REPORTED_CASES})")
    ap.add_argument("--max-exact-runs", type=int, default=MAX_EXACT_RUNS,
                    help="refuse the exact drift test above this many runs in "
                         "one series rather than approximate it (default: "
                         f"{MAX_EXACT_RUNS})")
    ap.add_argument("-o", "--out", help="write JSON here instead of stdout")
    a = ap.parse_args()
    # After parse_args, not an argparse `type=`: argparse writes usage text to
    # STDERR with no JSON payload (see _common.require_range).
    require_range("--alpha", a.alpha, 0, 1, exclusive=True)
    require_range("--max-cases", a.max_cases, lo=1)
    require_range("--max-exact-runs", a.max_exact_runs, lo=2)

    points, excluded = [], []
    for run_dir in discover(a.paths):
        try:
            points.append(load_point(run_dir))
        except BadRun as exc:
            excluded.append({"run": os.path.basename(run_dir),
                             "reason": str(exc)})

    groups = {}
    for point in points:
        groups.setdefault(json.dumps(point["key"], sort_keys=True),
                          []).append(point)
    series = [build_series(g, a) for _, g in sorted(groups.items())]
    series.sort(key=lambda s: (-s["n_runs"],
                               json.dumps(s["key"], sort_keys=True)))

    out = {
        "harness_version": HARNESS_VERSION,
        "runs_found": len(points) + len(excluded),
        "runs_used": len(points),
        # NAMED, never dropped silently: an excluded run is usually the one the
        # reader was looking for.
        "excluded_runs": sorted(excluded, key=lambda e: e["run"]),
        "series_count": len(series),
        "series": series,
        "alpha": a.alpha,
        "comparability": (
            "runner-contract.md SS5.6 requires dataset_version, "
            "harness_version and k to match for a PAIR; a series of N adds "
            "mode + selecting_split (which decides the case set, and a mean "
            "has no intersection step to absorb a mismatch) and the "
            "enabled-layer set (which decides what `pass` means). The app's "
            "git_sha is the independent variable and is deliberately not a "
            "key."),
        "not_a_decision": (
            "stats.py owns the one definition of `better` in this package. "
            "The drift flag is two-sided and describes a sequence; it says "
            "nothing about whether a change should ship."),
        "not_measured": (
            "per-case latency_s and cost_usd are not plotted here. Both are "
            "owned by score_cost.py, which prices a run against a DECLARED "
            "price table and reports latency as order statistics: a MEAN "
            "latency on this line would be the weakest possible summary of a "
            "heavy-tailed retry-contaminated distribution, and cost cannot be "
            "a series metric even in principle, because the price table is "
            "dated and the runs are not."),
    }
    if not points:
        out["note"] = (
            "every run directory found was excluded; see excluded_runs. A "
            "reports/ directory whose runs are all incomplete is a real state "
            "of the world, not a bad argument.")
    write_output(a.out, json.dumps(out, indent=2) + "\n")


if __name__ == "__main__":
    main()
