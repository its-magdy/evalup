#!/usr/bin/env python3
"""Judge/human agreement scorer for agent-eval — the calibration gate. Stdlib only.

This is the script that makes `judge.status: calibrated` a DERIVED fact.
`profile-schema.md` §`judge:` has always said the flag is "DERIVED, not set by
hand", and `run_cases.py`'s judged layer, `optimize`'s precondition #1 and
`case-format.md`'s judged dimensions all gate on it — but until this script
existed nothing derived it, so the harness's central credibility gate could
only be opened by a model typing the word into a YAML file. That is precisely
what the deterministic-first split (LLM reasons, scripts score) exists to
prevent. Here the LLM records what it OBSERVED per case; this script computes
the numbers and the flag.

POSITIVE = `fail`, and getting that backwards inverts the whole point. TPR is
the FAILURE-CATCH rate: P(judge says fail | human says fail). A 90%-pass app
makes an always-pass judge look 90% "accurate" while catching 0% of real
failures (rubric-format.md §Calibration workflow, docs/concepts.md §The judge),
and TPR is the number that catches that. TNR is P(judge says pass | human says
pass). Raw accuracy is never reported, on purpose.

INPUT: the annotation JSONL of annotation-ux.md §"Storage format" — the same
file `build_review_viewer.py -a` loads and its export panel produces, and the
one `analyze --label` writes. Read directly rather than through a converted
"labeled case" format: `analyze/SKILL.md` §`--label` already promises that file
IS the calibration interchange ("Its export panel produces exactly the JSONL
this flow consumes"), so a second format would contradict a shipped doc and add
a conversion step that can silently drop labels.

Per line, on top of that doc's fields:
  label       -- REQUIRED, the human verdict: "pass" | "fail".
  judge_label -- REQUIRED for a line to be counted: the judge's verdict for the
                 SAME case/node: "pass" | "fail" | "unknown".
  rubric_id   -- optional; groups the numbers, since calibration is per rubric.
  node_id     -- optional; part of the identity key, since the judge executes
                 ONE rubric node per call and `analyze/SKILL.md` §`--label`
                 says to label per node.

`judge_label` is not a field the viewer exports today, and it cannot be: the
runner NEVER writes a judge verdict — `run_cases.py`'s judged layer is always
`unjudged (<reason>)` and runner-contract.md §5.4 leaves filling it in to an
LLM step. So there is no artifact on disk pairing a judge verdict with a case,
and this script does not pretend to join one. The pairing is recorded at
labeling time, when the flow has both sides in hand, on one line. Lines with no
`judge_label` are ordinary open-coding annotations; they are counted under
`unpaired_annotations` and skipped, so one file can hold both kinds of pass.

Append-only, latest-wins: annotation-ux.md says re-reviewing a trace APPENDS.
Rows are deduped by (rubric_id, node_id, case_id or trace_id) with the LAST
occurrence in FILE ORDER winning — file order, not `ts`, because that is what
the viewer itself does and a second rule would disagree with the page the
reviewer read. `case_id` is preferred over `trace_id` as the identity, per that
doc's join rule (a `#N` trace-id suffix is the viewer's and is not stable).
A later un-paired re-annotation therefore SUPERSEDES an earlier paired one and
drops it from the numbers -- conservative on purpose: the judge verdict that
was paired at the earlier look may have come from a rubric version since
edited, and this script cannot know that it did not.

EXACTNESS (stats.py sets the house standard: exact at every n, approximations
only BELOW the decision and labelled as such):
  - TPR/TNR decisions compare POINT ESTIMATES against floors. That is exact
    arithmetic, no distribution involved, and it is the bar the docs state
    ("iterate until >=90% agreement, TPR and TNR both").
  - Reported beside them, never gating: Clopper-Pearson exact binomial
    confidence intervals, obtained by bisection on the exact binomial tail
    (math.comb, no normal approximation anywhere). When a floor is met but the
    exact lower bound is under it, `notes` says so -- the reader is told the
    estimate is thin rather than left to compare two fields.
  - KAPPA HAS NO EXACT SMALL-n INTERVAL. The usual one is a normal
    approximation on an asymptotic variance formula, so this script does not
    compute it at all rather than smuggle an approximation under a DECISION.
    Cohen's kappa is reported as a point estimate and gated as one. The single
    exact inferential statement available about a 2x2 agreement table with
    these margins IS computed and reported: Fisher's exact one-sided p for
    "agreement better than chance" (hypergeometric, conditioning on both
    margins). It tests kappa > 0, never kappa >= a floor; nothing tests that
    exactly, and the docstring says so instead of the output implying it.

THRESHOLDS (all overridable; the defaults are the house numbers, not invented):
  --tpr-floor / --tnr-floor 0.90 -- stated four times: analyze/SKILL.md §--label,
    docs/workflow.md, docs/concepts.md, rubric-format.md §Calibration workflow.
  --kappa-floor 0.60 -- the docs give kappa as a number to REPORT and set no
    floor, so this is the weakest of the three gates by design: Landis & Koch's
    "substantial agreement" boundary, there to reject a degenerate table rather
    than to be the bar. TPR/TNR do the real work. Raising it to 0.80 would
    exceed every threshold the docs actually state.
  --min-labeled 100 -- rubric-format.md §Calibration workflow step 2's
    validation pass is "~100-200 cases, stratified", per rubric.
  --min-per-class 10 -- each of TPR and TNR needs a denominator, and a rate is
    not a measurement of the class it was computed over when that class holds
    four cases. Below 10 the 0.90 floor also rounds to "perfect or fail" (at 5
    negatives it demands 5/5), an accidental bar nobody chose. It is a floor on
    the DENOMINATOR, not a fix for thin evidence in general: 9 of 10 does clear
    0.90, and it is `tpr_ci_exact` -- reported, never gating -- that says how
    little that is worth.
Known doc inconsistency, NOT silently accommodated: rubric-format.md's §File
shape example carries `calibrated: true` with `labeled_cases: 34`, under 100.
Its own §Calibration workflow and its §Worked template (112) say otherwise, so
the floor follows the prose and the example is wrong. Flagged, not lowered.

OUTPUT: JSON on stdout. Per rubric: the 2x2 counts, tpr/tnr/kappa, exact CIs,
and `calibrated` with the reasons it is not. Plus an aggregate `status`, which
is `calibrated` only when EVERY rubric measured here is -- profile-schema.md's
rule. Exit 0 either way: "uncalibrated" is a measurement, not a failure. Errors
go to STDOUT as {"error": ...} with exit 2 (_common.die).

--write <path> emits the sidecar `run_cases.py` reads (paths.judge_calibration,
a .json file). It is a SIDECAR for the same reason the holdout ledger is one:
this package is stdlib-only, profile.yaml and the rubric frontmatter are YAML,
and a hand-rolled YAML writer would corrupt documents that hold the harness's
configuration. Writing numbers this script computed into a file the runner
reads is what makes the gate machine-derived; transcribing them into YAML by
hand would leave it exactly as hand-set as it is today.
LIMIT, declared rather than papered over: this script sees the rubrics present
in ITS input. It cannot know which rubrics are ACTIVE, so the sidecar attests
only to `rubrics_measured`. run_cases.py closes that half by refusing the gate
when a case in the run references a rubric the sidecar never measured.

Usage: score_agreement.py <annotations.jsonl> [--tpr-floor F] [--tnr-floor F]
                          [--kappa-floor F] [--min-labeled N]
                          [--min-per-class N] [--confidence C]
                          [--write calibration.json]

See also: stats.py (baseline-vs-candidate, a different question entirely --
that one compares two RUNS, this one compares a judge against a human).
"""
import argparse
import datetime
import json
import math

from _common import (
    HARNESS_VERSION,
    add_version_flag,
    die,
    load_jsonl,
    nfc,
    require_range,
    write_output,
)

HUMAN_LABELS = ("pass", "fail")
JUDGE_LABELS = ("pass", "fail", "unknown")
# Rubrics are grouped by `rubric_id`; a file that names none is still scorable
# as one group, and this is what that group is called in the output.
UNGROUPED = "__unspecified__"
# Above this share of `unknown` judge verdicts the numbers below are computed
# on a filtered minority of the labels and the criterion itself is the finding.
UNKNOWN_WARN_RATE = 0.10
# Bisection depth for the Clopper-Pearson bounds. 2^-60 on a [0,1] interval is
# far below the 4 decimal places the output rounds to, so the reported bound is
# the exact one to every digit printed.
CP_ITERATIONS = 60


def binom_tail_ge(k, n, p):
    """Exact P(X >= k) for X ~ Binomial(n, p), summed with math.comb."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i)
               for i in range(k, n + 1))


def binom_tail_le(k, n, p):
    """Exact P(X <= k) for X ~ Binomial(n, p)."""
    if k >= n:
        return 1.0
    if k < 0:
        return 0.0
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i)
               for i in range(0, k + 1))


def _bisect(fn, target, lo=0.0, hi=1.0, increasing=True):
    """Solve fn(p) == target on [lo, hi] for a monotone fn, by bisection.

    Bisection rather than a closed form because the closed form is the inverse
    incomplete beta function, which the stdlib does not have. This is not an
    approximation of the STATISTIC -- the Clopper-Pearson bound is DEFINED as
    the root of an exact binomial tail equation, and this finds that root to
    2^-CP_ITERATIONS. Contrast a normal approximation, which would be solving a
    different equation exactly.
    """
    for _ in range(CP_ITERATIONS):
        mid = (lo + hi) / 2
        if (fn(mid) < target) == increasing:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clopper_pearson(k, n, confidence):
    """Exact two-sided binomial CI for k of n. Returns (lo, hi), or None if n=0.

    The k=0 and k=n edges are the closed-form ones (a one-sided interval); the
    general case solves the two tail equations. Reported only, never gating --
    see the module docstring on where approximations are allowed to live.
    """
    if n == 0:
        return None
    alpha = 1 - confidence
    lo = 0.0 if k == 0 else _bisect(
        lambda p: binom_tail_ge(k, n, p), alpha / 2, increasing=True)
    hi = 1.0 if k == n else _bisect(
        lambda p: binom_tail_le(k, n, p), alpha / 2, increasing=False)
    return round(lo, 4), round(hi, 4)


def fisher_exact_one_sided(tp, fn_, fp, tn):
    """One-sided (right-tail) Fisher exact p for the 2x2 agreement table.

    Rows are the human's label (fail, pass), columns the judge's. Conditioning
    on both margins, TP is hypergeometric under independence, and the right
    tail is the exact probability of agreement this strong or stronger by
    chance. This is the ONLY exact inferential statement available about this
    table: it tests kappa > 0, NOT kappa >= any floor. Nothing tests the
    latter exactly, which is why kappa is gated as a point estimate.
    """
    n = tp + fn_ + fp + tn
    if n == 0:
        return None
    row_fail, row_pass = tp + fn_, fp + tn
    col_fail = tp + fp
    denom = math.comb(n, col_fail)
    if denom == 0:
        return None
    total = 0
    for x in range(tp, min(row_fail, col_fail) + 1):
        rest = col_fail - x
        if 0 <= rest <= row_pass:
            total += math.comb(row_fail, x) * math.comb(row_pass, rest)
    # Six SIGNIFICANT figures, not six decimal places: this p runs to 1e-25
    # on a table that agrees strongly, and round(x, 6) printed that as 0.0 --
    # an exact test reporting a literal zero probability.
    return float(f"{total / denom:.6g}")


def cohens_kappa(tp, fn_, fp, tn):
    """Cohen's kappa for the 2x2 table, or (None, reason) when it is undefined.

    Undefined -- not zero -- when expected agreement is 1: both raters put
    every case in the same one class, so (po - pe) / (1 - pe) is 0/0. Returning
    0.0 there would report "chance-level agreement" for a table showing perfect
    agreement, and returning 1.0 would open the gate on a rubric that has never
    seen the other class. Either way the min-per-class floor also rejects it;
    this just refuses to print a number that is not defined.
    """
    n = tp + fn_ + fp + tn
    if n == 0:
        return None, "no labelled pairs"
    po = (tp + tn) / n
    pe = ((tp + fn_) / n) * ((tp + fp) / n) + ((fp + tn) / n) * ((fn_ + tn) / n)
    if pe >= 1.0:
        return None, ("undefined: expected agreement is 1 (every case fell in "
                      "one class for both raters)")
    return round((po - pe) / (1 - pe), 4), None


def identity(row, lineno, path):
    """annotation-ux.md's join rule: case_id first, trace_id as the fallback.

    A `trace_id` can carry the viewer's `#N` disambiguation suffix, which is the
    record's position under whatever --glob built the page and is not stable
    between sessions; `case_id` is never suffixed. Joining on the unstable id
    would resurrect an already-labelled case as a second, distinct pair and
    double-count it.
    """
    for key in ("case_id", "trace_id"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return nfc(value)
    die(f"{path}:{lineno}: needs a case_id or a trace_id to identify the "
        "annotated record (annotation-ux.md §'Storage format')")


def label_of(row, field, allowed, lineno, path):
    value = row.get(field)
    if not isinstance(value, str):
        die(f"{path}:{lineno}: {field!r} must be a string, got "
            f"{type(value).__name__}")
    # NFC before comparing, like every other string check in this package
    # (_common.nfc) -- these are values a human typed into a browser or a model
    # wrote into a file, and two spellings of one label must not split a class.
    value = nfc(value).strip().lower()
    if value not in allowed:
        die(f"{path}:{lineno}: {field}={value!r} is not one of "
            f"{list(allowed)}. A human label is binary by construction "
            "(annotation-ux.md: one-keystroke pass/fail); the judge may also "
            "answer 'unknown' (agents/judge.md rule 2).")
    return value


def optional_id(row, field):
    value = row.get(field)
    return nfc(value).strip() if isinstance(value, str) and value.strip() \
        else None


def load_pairs(path):
    """Annotation JSONL -> (paired rows, unpaired count).

    NOT load_jsonl(unique_key=...): that dies on a duplicate key, and this file
    is an APPEND-ONLY log in which a repeated id is the normal shape of a
    re-review, not an error. Latest-wins is applied here instead.
    """
    rows = load_jsonl(path, required_keys=("label",))
    latest = {}
    for lineno, row in enumerate(rows, 1):
        key = (optional_id(row, "rubric_id"), optional_id(row, "node_id"),
               identity(row, lineno, path))
        # Dedupe over EVERY line, paired or not, before filtering for
        # judge_label -- so a later open-coding re-annotation supersedes an
        # earlier calibration pairing rather than being invisible to it.
        latest[key] = (lineno, row)
    paired, unpaired = [], 0
    for (rubric_id, node_id, ident), (lineno, row) in latest.items():
        if "judge_label" not in row:
            unpaired += 1
            continue
        paired.append({
            "rubric_id": rubric_id or UNGROUPED,
            "node_id": node_id,
            "id": ident,
            "human": label_of(row, "label", HUMAN_LABELS, lineno, path),
            "judge": label_of(row, "judge_label", JUDGE_LABELS, lineno, path),
        })
    return paired, unpaired


def score_group(rows, opts):
    """One rubric's 2x2, its statistics, and whether it clears every floor."""
    resolved = [r for r in rows if r["judge"] != "unknown"]
    unknown = len(rows) - len(resolved)
    tp = sum(1 for r in resolved if r["human"] == "fail" and r["judge"] == "fail")
    fn_ = sum(1 for r in resolved if r["human"] == "fail" and r["judge"] == "pass")
    fp = sum(1 for r in resolved if r["human"] == "pass" and r["judge"] == "fail")
    tn = sum(1 for r in resolved if r["human"] == "pass" and r["judge"] == "pass")
    n_fail, n_pass = tp + fn_, fp + tn
    n = len(resolved)

    tpr = round(tp / n_fail, 4) if n_fail else None
    tnr = round(tn / n_pass, 4) if n_pass else None
    kappa, kappa_note = cohens_kappa(tp, fn_, fp, tn)

    reasons = []
    if n < opts.min_labeled:
        reasons.append(
            f"labelled pairs {n} < {opts.min_labeled} (rubric-format.md's "
            "validation pass is ~100-200, stratified)")
    if n_fail < opts.min_per_class:
        reasons.append(
            f"only {n_fail} case(s) the human called FAIL, under "
            f"{opts.min_per_class} -- TPR is the failure-catch rate and this "
            "is its whole denominator")
    if n_pass < opts.min_per_class:
        reasons.append(
            f"only {n_pass} case(s) the human called PASS, under "
            f"{opts.min_per_class} -- TNR has no denominator to speak of")
    if tpr is None or tpr < opts.tpr_floor:
        reasons.append(f"tpr {tpr} < {opts.tpr_floor}")
    if tnr is None or tnr < opts.tnr_floor:
        reasons.append(f"tnr {tnr} < {opts.tnr_floor}")
    if kappa is None or kappa < opts.kappa_floor:
        reasons.append(f"kappa {kappa if kappa is not None else kappa_note} "
                       f"< {opts.kappa_floor}")

    notes = []
    if kappa_note:
        notes.append("kappa " + kappa_note)
    if unknown and rows and unknown / len(rows) > UNKNOWN_WARN_RATE:
        notes.append(
            f"{unknown} of {len(rows)} judge verdicts are 'unknown' and are "
            "excluded from the table (counting one as pass or fail would "
            "manufacture agreement it never expressed). Above "
            f"{UNKNOWN_WARN_RATE:.0%} the criterion's ambiguity is the finding, "
            "not the numbers below it -- see analyze/SKILL.md §--label step 2.")
    tpr_ci = clopper_pearson(tp, n_fail, opts.confidence)
    tnr_ci = clopper_pearson(tn, n_pass, opts.confidence)
    for name, value, floor, ci in (("tpr", tpr, opts.tpr_floor, tpr_ci),
                                   ("tnr", tnr, opts.tnr_floor, tnr_ci)):
        if value is not None and ci and value >= floor and ci[0] < floor:
            notes.append(
                f"{name} {value} clears {floor} but its exact "
                f"{opts.confidence:.0%} lower bound is {ci[0]} -- the estimate "
                "is thin at this n. Reported, not gated (see the module "
                "docstring on where approximations may live).")

    return {
        "labelled_pairs": n,
        "matrix": {"tp": tp, "fn": fn_, "fp": fp, "tn": tn},
        "human_fail": n_fail, "human_pass": n_pass,
        "judge_unknown": unknown,
        "unknown_rate": round(unknown / len(rows), 4) if rows else 0.0,
        "tpr": tpr, "tnr": tnr, "kappa": kappa,
        "tpr_ci_exact": tpr_ci, "tnr_ci_exact": tnr_ci,
        "chance_agreement_p_exact": fisher_exact_one_sided(tp, fn_, fp, tn),
        "calibrated": not reasons,
        "blocking": reasons,
        "notes": notes,
    }


def sidecar(out, opts):
    """The artifact run_cases.py reads (plan paths.judge_calibration)."""
    return {
        "schema": "agent-eval/judge-calibration/1",
        "harness_version": HARNESS_VERSION,
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": out["status"],
        "thresholds": out["thresholds"],
        "rubrics_measured": sorted(out["rubrics"]),
        "rubrics": {name: {"calibrated": block["calibrated"],
                           "labelled_pairs": block["labelled_pairs"],
                           "tpr": block["tpr"], "tnr": block["tnr"],
                           "kappa": block["kappa"],
                           "blocking": block["blocking"]}
                    for name, block in out["rubrics"].items()},
        "note": ("Attests only to the rubrics in this file's annotations. It "
                 "cannot know which rubrics are ACTIVE; run_cases.py refuses "
                 "the judged gate when a case in the run references a rubric "
                 "absent from rubrics_measured."),
    }


def main():
    ap = argparse.ArgumentParser(
        description="Judge/human agreement from an annotation JSONL: TPR "
                    "(failure-catch rate), TNR and Cohen's kappa per rubric, "
                    "and the derived judge.status. Never raw accuracy.")
    add_version_flag(ap)
    ap.add_argument("annotations", help="annotation JSONL (annotation-ux.md "
                                        "§'Storage format'), with judge_label")
    ap.add_argument("--tpr-floor", type=float, default=0.90)
    ap.add_argument("--tnr-floor", type=float, default=0.90)
    ap.add_argument("--kappa-floor", type=float, default=0.60)
    ap.add_argument("--min-labeled", type=int, default=100,
                    help="labelled pairs required per rubric (default: 100)")
    ap.add_argument("--min-per-class", type=int, default=10,
                    help="human PASS and human FAIL cases each required, so "
                         "TPR and TNR both have a denominator (default: 10)")
    ap.add_argument("--confidence", type=float, default=0.95,
                    help="level for the REPORTED exact Clopper-Pearson "
                         "intervals; does not gate (default: 0.95)")
    ap.add_argument("--write", metavar="PATH",
                    help="also write the .json calibration sidecar the runner "
                         "reads (plan paths.judge_calibration)")
    a = ap.parse_args()
    # After parse_args, not via an argparse `type=` callable: argparse writes
    # usage text to STDERR with no JSON payload (see _common.require_range).
    for flag, value in (("--tpr-floor", a.tpr_floor),
                        ("--tnr-floor", a.tnr_floor),
                        ("--kappa-floor", a.kappa_floor)):
        require_range(flag, value, 0, 1, note=" (it is a rate)")
    require_range("--confidence", a.confidence, 0, 1, exclusive=True)
    require_range("--min-labeled", a.min_labeled, lo=1)
    require_range("--min-per-class", a.min_per_class, lo=1)
    if a.write and not a.write.endswith(".json"):
        die(f"--write must name a .json file (got {a.write!r}): this package "
            "is stdlib-only and cannot safely write the YAML that holds "
            "profile.yaml's judge block -- the sidecar exists for that reason")

    paired, unpaired = load_pairs(a.annotations)
    if not paired:
        die(f"{a.annotations}: no annotation carries a 'judge_label', so there "
            f"is nothing to compare ({unpaired} human-only annotation(s) "
            "read). A viewer export holds the human side only; the judge's "
            "verdict is written by no artifact in this harness "
            "(run_cases.py's judged layer is always 'unjudged'), so the "
            "labelling flow must record both sides on one line -- see this "
            "script's docstring and annotation-ux.md.")

    groups = {}
    for row in paired:
        groups.setdefault(row["rubric_id"], []).append(row)
    rubrics = {name: score_group(rows, a) for name, rows in sorted(groups.items())}
    # profile-schema.md §`judge:`: `calibrated` only when EVERY active rubric
    # is individually calibrated -- one uncalibrated rubric holds the whole
    # harness at `uncalibrated`.
    status = "calibrated" if all(b["calibrated"] for b in rubrics.values()) \
        else "uncalibrated"

    out = {
        "status": status,
        "rubrics_measured": sorted(rubrics),
        "labelled_pairs": sum(b["labelled_pairs"] for b in rubrics.values()),
        "unpaired_annotations": unpaired,
        "thresholds": {"tpr": a.tpr_floor, "tnr": a.tnr_floor,
                       "kappa": a.kappa_floor, "min_labeled": a.min_labeled,
                       "min_per_class": a.min_per_class},
        "rubrics": rubrics,
        "positive_class": "fail",
        "accuracy": "not reported by design -- an always-pass judge scores "
                    "0.90 on a 90%-pass app while catching no failures at all",
    }
    if status == "uncalibrated":
        out["unlock"] = sorted(
            f"{name}: {reason}" for name, block in rubrics.items()
            for reason in block["blocking"])
    if a.write:
        write_output(a.write, json.dumps(sidecar(out, a), indent=2) + "\n")
        out["written"] = a.write
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
