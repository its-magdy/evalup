#!/usr/bin/env python3
"""Routing scorer for agent-eval.

Input: JSONL of per-case routing results:
  {"case_id": ..., "expected": "billing", "acceptable": ["billing"],
   "clarify_ok": false, "observed": "support", "clarified": false}
The case-format field names are accepted as aliases: "route" -> "expected",
"route_acceptable" -> "acceptable". "expected" must be non-null; "observed"
may be null (the app produced no route — crash/refusal), which is scored as
the "__no_route__" label, a fail that appears in the confusion matrix.

Out-of-scope cases use the "__oos__" label. Pass --oos-route <name> (the
app's concrete OOS/refusal route from profile oos_handling) and the scorer
maps that route name to "__oos__" in both expected and observed — without
the mapping the OOS precision/recall block cannot appear. The name must match
a label the data carries or it is an input error (exit 2), listing the labels
that are present; see require_oos_route_present. All labels are compared in
NFC (see _common.nfc). A case passes if
observed is in acceptable (expected is always implicitly acceptable), or
(clarify_ok and clarified).

Outputs: accuracy, per-target precision/recall/F1, macro & micro F1,
confusion matrix, OOS precision/recall. macro_f1 averages over labels with
support > 0 only; prediction-only labels (__no_route__, hallucinated route
names) keep their per_target row but are listed under spurious_labels and
excluded from the average. Cases that pass via the acceptable
set or the clarify path (observed != expected) are excluded from the
confusion matrix and P/R/F1 and counted as accepted_alternates — they are
not mismatches. Accuracy still counts them as passes.
Usage: score_routing.py <results.jsonl> [--oos-route <name>]
"""
import argparse
import json
from collections import defaultdict

from _common import add_version_flag, die, load_jsonl, nfc, require_list

ALIASES = {"route": "expected", "route_acceptable": "acceptable"}


def f1(p, r):
    return 0.0 if (p + r) == 0 else 2 * p * r / (p + r)


def label_universe(rows):
    """Every route label the data actually carries, in NFC, across all three
    label-bearing fields.

    Read BEFORE normalize_rows, because it is what --oos-route is validated
    against and normalize_rows is where the rename happens — so it reads the
    case-format ALIASES too (route/route_acceptable). Reading only the
    canonical names here would reject every correctly-spelled --oos-route on
    the alias spelling of the input, turning one silent wrong answer into a
    loud wrong error."""
    labels = set()
    for r in rows:
        for k in ("expected", "route", "observed"):
            if isinstance(r.get(k), str):
                labels.add(nfc(r[k]))
        for k in ("acceptable", "route_acceptable"):
            if isinstance(r.get(k), list):
                labels.update(nfc(v) for v in r[k] if isinstance(v, str))
    return labels


def require_oos_route_present(rows, oos_route):
    """--oos-route must name a label the data contains, or it is an input
    error.

    The flag only RENAMES a route to the __oos__ sentinel, so a name that
    matches nothing renames nothing — and every downstream consequence of that
    was silent. The OOS precision/recall block, a security-adjacent number,
    reported precision/recall null with the note "0 OOS case(s) in this run"
    over data holding two of them: not merely missing, but affirmatively wrong
    about the run, at exit 0. The value is LLM-supplied (the run skill fills it
    from the profile's oos_handling), so a typo or a stale profile is the
    expected way to get here, and the report gives the reader no way to tell
    this run from one that genuinely has no out-of-scope cases.

    Listing the labels PRESENT is most of the fix: with them in the message the
    correct value is usually obvious ("refuse" vs "refusal") and the second
    cause — a run whose cases contain no OOS at all — is equally visible, which
    is why the message names both."""
    present = label_universe(rows)
    if oos_route in present:
        return
    die(f"--oos-route {oos_route!r} matches no label in the data (labels "
        f"present: {sorted(present)}). It renames one of those to the "
        "__oos__ sentinel, so an unmatched name silently disables the OOS "
        "leakage metric: check the spelling against the profile's "
        "oos_handling route, or omit --oos-route entirely if this run has no "
        "out-of-scope cases.")


def normalize_rows(rows, oos_route):
    for i, r in enumerate(rows, 1):
        for alias, canon in ALIASES.items():
            if alias in r and canon not in r:
                r[canon] = r.pop(alias)
        if r.get("expected") is None:
            die(f"row {i} (case_id {r.get('case_id')!r}): 'expected' (or "
                "'route') must be present and non-null")
        if "observed" not in r:
            die(f"row {i} (case_id {r.get('case_id')!r}): missing "
                "'observed' key (null is allowed: no route produced)")
        # The bare-string slip (`route_acceptable: billing` instead of
        # `[billing]`), and the one place in this harness it stayed silent:
        # main() below builds `set(acceptable)`, so a string became a set of its
        # CHARACTERS. No real route name is ever in that set, so a case whose
        # observed route was a blessed alternate scored a mismatch — accuracy
        # 0.0, a confusion-matrix entry, and exit 0. A manufactured failure, the
        # one outcome this harness must never produce. The rule lives in
        # _common.require_list, which every sibling scorer already shares; the
        # field name is spelled as the case-format key the human actually wrote,
        # and trails the row context so require_list's `[...]` hint still reads
        # cleanly when a case_id itself contains a dot.
        if r.get("acceptable") is not None:
            require_list(
                f"row {i} (case_id {r.get('case_id')!r}): "
                "expect.route_acceptable",
                r["acceptable"], item="route name")
        # Route labels are text that crosses a boundary: `expected` and
        # `acceptable` are authored in the case file, `observed` comes back
        # from the app. NFC on all three (see _common.nfc) so the same route
        # name written in two Unicode forms is one label — otherwise a correct
        # route scores a mismatch AND splits the confusion matrix into two rows
        # that print identically.
        for k in ("expected", "observed"):
            r[k] = nfc(r[k])
        if isinstance(r.get("acceptable"), list):
            r["acceptable"] = [nfc(v) for v in r["acceptable"]]
        if oos_route:
            for k in ("expected", "observed"):
                if r[k] == oos_route:
                    r[k] = "__oos__"
            # The acceptable set is label-bearing too, and canonicalizing only
            # SOME label fields is worse than canonicalizing none: a case whose
            # acceptable set blesses the OOS route (refusing is a fine answer
            # for an ambiguous in-scope query) had its observed route rewritten
            # to __oos__ while its acceptable list still said "refuse", so the
            # membership test missed and a passing case was scored a mismatch.
            # --oos-route only RENAMES labels, so it must be verdict-invariant.
            if isinstance(r.get("acceptable"), list):
                r["acceptable"] = ["__oos__" if v == oos_route else v
                                   for v in r["acceptable"]]
        if r["observed"] is None:
            r["observed"] = "__no_route__"


def main():
    ap = argparse.ArgumentParser(
        description="Score routing results (JSONL: case_id, expected, "
                    "observed, [acceptable], [clarify_ok], [clarified]).")
    add_version_flag(ap)
    ap.add_argument("results", help="per-case routing results JSONL")
    ap.add_argument("--oos-route",
                    help="the app's OOS/refusal route name; mapped to the "
                         "__oos__ label so OOS metrics are reported")
    a = ap.parse_args()
    # case_id is required, not merely unique: without it every row dedupes
    # against None and the second row reports "duplicate case_id None",
    # which points the user at the wrong problem.
    rows = load_jsonl(a.results, unique_key="case_id",
                      required_keys=("case_id",))
    if not rows:
        die("no rows")
    # Before normalize_rows: the check reads the labels as the data spells
    # them, and the rename it guards happens in there.
    if a.oos_route:
        require_oos_route_present(rows, nfc(a.oos_route))
    normalize_rows(rows, nfc(a.oos_route) if a.oos_route else a.oos_route)

    confusion = defaultdict(lambda: defaultdict(int))
    passes = 0
    accepted_alternates = 0
    matrix_rows = []
    for r in rows:
        # expected is always acceptable — a label edit that drops it from the
        # acceptable set must not turn a correct route into a diagonal "fail".
        acceptable = set(r.get("acceptable") or []) | {r["expected"]}
        ok = bool(r["observed"] in acceptable or
                  (r.get("clarify_ok") and r.get("clarified")))
        passes += ok
        if ok and r["observed"] != r["expected"]:
            # Passed via the acceptable set or the clarify path: not a
            # mismatch — keep it out of the confusion matrix and P/R/F1.
            accepted_alternates += 1
            continue
        matrix_rows.append(r)
        confusion[r["expected"]][r["observed"]] += 1

    # Freeze before computing metrics: reading a defaultdict materializes
    # entries, which would emit phantom zero-count rows.
    confusion = {k: dict(v) for k, v in confusion.items()}
    labels = sorted({r["expected"] for r in matrix_rows} |
                    {r["observed"] for r in matrix_rows})
    per_target = {}
    tp_total = fp_total = fn_total = 0
    for lab in labels:
        row = confusion.get(lab, {})
        tp = row.get(lab, 0)
        fn = sum(v for k, v in row.items() if k != lab)
        fp = sum(confusion.get(other, {}).get(lab, 0)
                 for other in labels if other != lab)
        tp_total += tp
        fp_total += fp
        fn_total += fn
        p = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        per_target[lab] = {
            "precision": round(p, 4), "recall": round(rec, 4),
            "f1": round(f1(p, rec), 4),
            "support": tp + fn,
        }

    # Macro averages run over labels with SUPPORT only. Prediction-only labels
    # — __no_route__, or a route name the app hallucinated — have no expected
    # case behind them, so their recall is 0/0 and their F1 is a structural
    # 0.0, not a measurement. Averaging those in dragged macro_f1 down by one
    # slot per unrouted case (4 cases, 3 correct, 1 null observed: 0.5 instead
    # of 0.833) and then tripped the macro/micro gap warning below, which sent
    # the reader hunting for an underperforming minority target that does not
    # exist. The rows stay visible, under spurious_labels, because a
    # hallucinated route name is itself a finding — it just is not a target the
    # macro average is defined over.
    supported = {lab: d for lab, d in per_target.items() if d["support"]}
    spurious = sorted(lab for lab in per_target if lab not in supported)
    macro_f1 = (sum(d["f1"] for d in supported.values()) / len(supported)
                if supported else 0.0)
    micro_p = tp_total / (tp_total + fp_total) if (tp_total + fp_total) else 0
    micro_r = tp_total / (tp_total + fn_total) if (tp_total + fn_total) else 0

    out = {
        "layer": "routing",
        "n": len(rows),
        "accuracy": round(passes / len(rows), 4),
        "macro_f1": round(macro_f1, 4),
        "micro_f1": round(f1(micro_p, micro_r), 4),
        "per_target": per_target,
        "spurious_labels": {
            "labels": spurious,
            "note": "prediction-only labels (no expected case has this "
                    "target): __no_route__ or a route name the app produced "
                    "that no case asks for. Their per_target rows are "
                    "reported above but excluded from macro_f1, which is "
                    "averaged over labels with support > 0 only.",
        },
        "confusion_matrix": confusion,
        "accepted_alternates": {
            "count": accepted_alternates,
            "note": "cases that passed via the acceptable set or the clarify "
                    "path (observed != expected); excluded from the confusion "
                    "matrix and P/R/F1 — they are not mismatches. Accuracy "
                    "counts them as passes.",
        },
    }
    # Emitted whenever --oos-route was passed, measurable or not. Keying this
    # block on per_target alone hid a real distinction: per_target is built
    # from matrix_rows, so OOS cases that passed via the acceptable set or the
    # clarify path drop out of it, and the block silently vanished. "No OOS
    # leakage" and "OOS leakage not measured" then read identically.
    if a.oos_route:
        oos_expected = sum(1 for r in rows if r["expected"] == "__oos__")
        if "__oos__" in per_target:
            # Report COVERAGE beside the rate. An OOS case whose acceptable set
            # blessed a route target is excluded from the matrix (correctly — it
            # is not a mismatch), but that shrinks the denominator this recall is
            # computed over. Without the excluded count, a run where an OOS query
            # did land in a route target still reads "recall 1.0", and the note
            # below then reads as proof of something it did not measure.
            excluded = oos_expected - per_target["__oos__"]["support"]
            out["oos"] = {
                "precision": per_target["__oos__"]["precision"],
                "recall": per_target["__oos__"]["recall"],
                "support": per_target["__oos__"]["support"],
                "oos_cases": oos_expected,
                "excluded_accepted_alternates": excluded,
                "note": "recall<1.0 means out-of-scope queries leaked into "
                        "route targets"
                        + ("" if not excluded else
                           f" — measured over {per_target['__oos__']['support']} of "
                           f"{oos_expected} OOS case(s); {excluded} passed via the "
                           "acceptable set or the clarify path and are not measured "
                           "here (see accepted_alternates)"),
            }
        else:
            out["oos"] = {
                "precision": None, "recall": None, "support": 0,
                "note": (
                    "not measurable: no out-of-scope case reached the "
                    f"confusion matrix ({oos_expected} OOS case(s) in this "
                    "run" + (", all passed via the acceptable set or the "
                             "clarify path" if oos_expected else "") + "). "
                    "This is not evidence of zero OOS leakage."),
            }
    if abs(out["macro_f1"] - out["micro_f1"]) > 0.1:
        out["warning"] = ("macro/micro F1 gap >0.1: minority route targets are "
                          "underperforming — check per_target")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
