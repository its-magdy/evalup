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
the mapping the OOS precision/recall block cannot appear. A case passes if
observed is in acceptable (expected is always implicitly acceptable), or
(clarify_ok and clarified).

Outputs: accuracy, per-target precision/recall/F1, macro & micro F1,
confusion matrix, OOS precision/recall. Cases that pass via the acceptable
set or the clarify path (observed != expected) are excluded from the
confusion matrix and P/R/F1 and counted as accepted_alternates — they are
not mismatches. Accuracy still counts them as passes.
Usage: score_routing.py <results.jsonl> [--oos-route <name>]
"""
import argparse
import json
from collections import defaultdict

from _common import add_version_flag, die, load_jsonl

ALIASES = {"route": "expected", "route_acceptable": "acceptable"}


def f1(p, r):
    return 0.0 if (p + r) == 0 else 2 * p * r / (p + r)


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
        if oos_route:
            for k in ("expected", "observed"):
                if r[k] == oos_route:
                    r[k] = "__oos__"
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
    normalize_rows(rows, a.oos_route)

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

    macro_f1 = (sum(d["f1"] for d in per_target.values()) / len(per_target)
                if per_target else 0.0)
    micro_p = tp_total / (tp_total + fp_total) if (tp_total + fp_total) else 0
    micro_r = tp_total / (tp_total + fn_total) if (tp_total + fn_total) else 0

    out = {
        "layer": "routing",
        "n": len(rows),
        "accuracy": round(passes / len(rows), 4),
        "macro_f1": round(macro_f1, 4),
        "micro_f1": round(f1(micro_p, micro_r), 4),
        "per_target": per_target,
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
            out["oos"] = {
                "precision": per_target["__oos__"]["precision"],
                "recall": per_target["__oos__"]["recall"],
                "support": per_target["__oos__"]["support"],
                "note": "recall<1.0 means out-of-scope queries leaked into route targets",
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
