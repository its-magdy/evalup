#!/usr/bin/env python3
"""Report a finished run by business rule (#26). Stdlib only.

    rule_report.py reports/<run-id> --profile <work-dir>/profile.json
                   [--judged reports/<run-id>/judge/judged.json] [-o FILE]

A case says which profile.yaml `business_rules` its answer is checked against
in `expect.answer.rules`. This script reads the run the runner left and, for
each rule, says which cases checked it, how, and what came back. It scores
nothing itself: every verdict it reports is one a scorer or the judge pass
already wrote, attributed to a rule. Two ways a rule is checked on a case,
script first:

  scripted  the rule's `check:` in profile.yaml (must_contain /
            must_not_contain entries, score_answer.py's grammar). The case
            carries the same entries in its expect.answer -- validate_cases.py
            --profile refuses one that does not (rule_check_missing) -- so the
            runner's answer layer scores them as it scores every entry, and
            this script finds them in that layer's `checks` by entry. With
            k > 1 every repeat is read: a rule broken in 2 of 3 repeats is a
            `fail` with `failed_repeats: "2/3"`. A conversation's turns are
            read turn by turn, each with its own expect.
  judged    a `form: criteria` rubric criterion with `rule: <id>` on the
            case's pinned rubric. The verdict is that criterion's, from the
            judge pass's judged.json, with the case's state: `provisional` or
            `calibrated` when the criterion came back pass/fail with checked
            evidence, else `unjudged` with the reason (docs/judge-run.md SS1).

What a rule's result may decide (the walls):

  - A scripted check is the runner's answer layer, so its `fail` is already
    in its case's verdict, and reaches gate.py through that case when the
    case is `gating: true`. That is the only way a rule touches a gate, and
    validate_cases.py lets only a `confirmed` rule (status confirmed, a
    human's confirmed_by) sit on a gating case or a canary.
  - An `inferred` rule is reported, never gated: its cases are `gating:
    false` (gate.py --min-pass-rate still counts them, as #25 records).
  - A judged verdict never gates, never reaches stats.py, a baseline or an
    optimize objective, `calibrated` included, until a live calibration has
    been measured (judge-run.md SS8). rules.json is a file nothing on the
    decision path opens: gate.py, stats.py, run_history.py and the runner
    do not read it, and it never rewrites a file they do.

Sealed holdout cases are counts only: no case id, no entry, no quote
(contract SS10), and they are never judged.

Writes <run-dir>/rules.json (or -o), schema evalup/rules/1, and prints
{"rules_json": <path>, "summary": [lines]} -- the lines the run skill puts
in report.md. Rerunnable; derived from the run directory, the profile and
judged.json only.

Exit 0 done (a failing rule is a result, not an error); 2 bad input
({"error": ...} on stdout, _common.die).
"""
import argparse
import hashlib
import json
import os
import pathlib

from _common import add_version_flag, die, load_json, unsafe_case_id
from _rubric import split_pin
from validate_cases import profile_rules, rule_check, rule_status

RULES_SCHEMA = "evalup/rules/1"
JUDGED_SCHEMA = "evalup/judged/1"
# How many cases a rule's report line names before "and N more".
SHOWN = 5


def sha256_of(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def read_object(path, what):
    doc = load_json(path)
    if not isinstance(doc, dict):
        die(f"{path}: {what} must be a JSON object, got {type(doc).__name__}")
    return doc


def read_optional(path):
    if not os.path.isfile(path):
        return None
    doc = load_json(path)
    return doc if isinstance(doc, dict) else None


def load_profile(path):
    doc = read_object(path, "the profile")
    if "cases" in doc and isinstance(doc.get("profile"), dict):
        # convert_suite.py's combined document, as validate_cases.py reads it.
        doc = doc["profile"]
    return doc


def case_dirs(run_dir):
    root = os.path.join(run_dir, "cases")
    if not os.path.isdir(root):
        die(f"{run_dir}: no cases/ directory; is this a run directory?")
    out = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if os.path.isfile(os.path.join(path, "verdict.json")) \
                and unsafe_case_id(name) is None:
            out.append((name, path))
    return out


def turn_numbers(case_dir):
    root = os.path.join(case_dir, "turns")
    if not os.path.isdir(root):
        return []
    return sorted(int(n) for n in os.listdir(root) if n.isdigit())


def listed_rules(expect):
    answer = expect.get("answer") if isinstance(expect, dict) else None
    rules = answer.get("rules") if isinstance(answer, dict) else None
    out = []
    for rid in rules if isinstance(rules, list) else []:
        if isinstance(rid, str) and rid.strip() and rid.strip() not in out:
            out.append(rid.strip())
    return out


def expects_of(case_dir, verdict):
    """[(layer prefix, expect, is the deciding expect)]: one per turn for a
    conversation (`t<n>.` keys its union rows, docs/multi-turn.md SS3), else
    the case's own. The deciding expect is the one the judge grades."""
    if verdict.get("multi_turn"):
        turns = turn_numbers(case_dir)
        out = []
        for n in turns:
            path = os.path.join(case_dir, "turns", str(n), "expect.json")
            expect = read_optional(path) or {}
            out.append((f"t{n}.", expect, n == turns[-1]))
        return out
    expect = read_optional(os.path.join(case_dir, "expect.json")) or {}
    return [("", expect, True)]


def attempts_of(verdict):
    """Every repeat's layer rows (k > 1), else the case's own."""
    repeats = verdict.get("repeats")
    if isinstance(repeats, list) and repeats:
        return [r.get("layers") if isinstance(r.get("layers"), dict) else {}
                for r in repeats if isinstance(r, dict)]
    layers = verdict.get("layers")
    return [layers if isinstance(layers, dict) else {}]


def scripted_attempt(layer, check):
    """(status, failed entries, reason) for one rule check on one answer
    layer row: `fail` when any of the rule's entries failed, `pass` when
    each of them passed, `unscored` otherwise (the layer did not run, or an
    entry was unscorable or is not in its checks)."""
    if not isinstance(layer, dict):
        return "unscored", [], "no answer row"
    if layer.get("verdict") not in ("pass", "fail"):
        return "unscored", [], f"answer layer {layer.get('verdict')}"
    rows = [c for c in layer.get("checks") or [] if isinstance(c, dict)]
    failed, unscored = [], []
    for kind, entries in check.items():
        for entry in entries:
            row = next((c for c in rows if c.get("check") == kind
                        and c.get("entry") == entry), None)
            if row is None:
                unscored.append(f"{kind} {entry!r} not in the answer checks")
            elif row.get("status") == "fail":
                failed.append({"check": kind, "entry": entry,
                               "reason": row.get("reason")})
            elif row.get("status") != "pass":
                unscored.append(f"{kind} {entry!r} {row.get('status')}")
    if failed:
        return "fail", failed, None
    if unscored:
        return "unscored", [], "; ".join(unscored)
    return "pass", [], None


def scripted_result(verdict, prefix, check):
    """One case's scripted result for one rule, over every repeat."""
    statuses, failed, reasons = [], [], []
    for layers in attempts_of(verdict):
        status, bad, reason = scripted_attempt(layers.get(prefix + "answer"),
                                               check)
        statuses.append(status)
        for item in bad:
            if item not in failed:
                failed.append(item)
        if reason and reason not in reasons:
            reasons.append(reason)
    if "fail" in statuses:
        result = {"verdict": "fail", "failed": failed}
        if len(statuses) > 1:
            result["failed_repeats"] = \
                f"{statuses.count('fail')}/{len(statuses)}"
        return result
    if statuses and all(s == "pass" for s in statuses):
        return {"verdict": "pass"}
    return {"verdict": "unscored", "reason": "; ".join(reasons)
            or "not scored"}


def judged_index(judged):
    """case_id -> judged.json case row, and rubric_id -> {criterion: rule}."""
    if judged is None:
        return {}, {}
    cases = {c.get("case_id"): c for c in judged.get("cases") or []
             if isinstance(c, dict)}
    rubrics = judged.get("rubrics") if isinstance(judged.get("rubrics"),
                                                  dict) else {}
    rules = {rid: snap.get("criterion_rules") or {}
             for rid, snap in rubrics.items() if isinstance(snap, dict)}
    return cases, rules


def judged_result(rid, expect, case_id, cases, criterion_rules, judged,
                  scripted):
    """One case's judged result for one rule, or None when the case's
    rubric has no criterion for it (its binding is scripted only). With no
    judge pass the criteria are unknown: a rule with a scripted check is
    then left to it, and one without is `unjudged` (validate_cases.py
    --profile lets such a rule on a case only through a criterion)."""
    answer = expect.get("answer") if isinstance(expect.get("answer"),
                                                dict) else {}
    ref = answer.get("rubric")
    if not isinstance(ref, str) or not ref.strip():
        return None
    rubric_id = split_pin(ref.strip())[0]
    if judged is None:
        if scripted:
            return None
        return {"state": "unjudged", "verdict": None,
                "reason": "no judge pass ran"}
    crit_id = next((c for c, r in criterion_rules.get(rubric_id, {}).items()
                    if r == rid), None)
    if crit_id is None:
        return None
    row = cases.get(case_id)
    if row is None:
        return {"state": "unjudged", "verdict": None, "criterion": crit_id,
                "reason": "not in judged.json"}
    out = {"criterion": crit_id, "rubric_id": rubric_id}
    if row.get("state") not in ("provisional", "calibrated"):
        return dict(out, state="unjudged", verdict=None,
                    reason=row.get("reason") or "unjudged")
    crit = next((c for c in row.get("criteria") or []
                 if isinstance(c, dict) and c.get("id") == crit_id), None)
    if crit is None or crit.get("verdict") not in ("pass", "fail"):
        why = (crit or {}).get("reason") or "the judge said unknown"
        return dict(out, state="unjudged", verdict=None,
                    reason=f"criterion {crit_id}: {why}")
    quotes = [e.get("quote") for e in crit.get("evidence") or []
              if isinstance(e, dict) and isinstance(e.get("quote"), str)]
    return dict(out, state=row["state"], verdict=crit["verdict"],
                quotes=quotes)


def new_row(rid, rule):
    row = {"id": rid, "status": rule_status(rule)}
    for key in ("rule", "quote", "source", "date", "contradiction"):
        if isinstance(rule.get(key), (str, int, float)) and \
                not isinstance(rule.get(key), bool):
            row[key] = str(rule[key])
    check = rule_check(rule)
    row["check"] = check
    row["scripted"] = {"pass": 0, "fail": 0, "unscored": 0, "cases": []}
    row["judged"] = {"provisional": {"pass": 0, "fail": 0},
                     "calibrated": {"pass": 0, "fail": 0},
                     "unjudged": 0, "cases": []}
    row["holdout"] = {"checked": 0, "fail": 0}
    row["gating_failures"] = []
    return row


def finish(row):
    s, j = row["scripted"], row["judged"]
    checked_by = [kind for kind, n in (
        ("scripted", s["pass"] + s["fail"] + s["unscored"]),
        ("judged", sum(j["provisional"].values())
         + sum(j["calibrated"].values()) + j["unjudged"]))
        if n]
    if row["holdout"]["checked"] and "scripted" not in checked_by:
        checked_by.insert(0, "scripted")
    row["checked_by"] = checked_by
    # `verdict` is the SCRIPTED verdict: the one that is also in a case's
    # verdict. A rule only the judge checked says `judged`, and its
    # judged_verdict below is the whole of what is known.
    if s["fail"] or row["holdout"]["fail"]:
        row["verdict"] = "fail"
    elif s["pass"]:
        row["verdict"] = "pass"
    elif "scripted" in checked_by:
        row["verdict"] = "unscored"
    elif checked_by:
        row["verdict"] = "judged"
    else:
        row["verdict"] = "not checked"
    # The judged verdict is apart, always: provisional/calibrated, never in
    # `verdict`, never a gate (judge-run.md SS8).
    jv = None
    for state in ("calibrated", "provisional"):
        if j[state]["fail"]:
            jv = {"state": state, "verdict": "fail"}
            break
    if jv is None:
        for state in ("calibrated", "provisional"):
            if j[state]["pass"]:
                jv = {"state": state, "verdict": "pass"}
                break
    row["judged_verdict"] = jv
    return row


def case_label(entry):
    bits = [entry["case_id"]]
    if entry.get("failed_repeats"):
        bits.append(f"failed {entry['failed_repeats']} repeats")
    return " ".join(bits)


def summary_lines(rows, run_id):
    checked = [r for r in rows if r["verdict"] != "not checked"]
    failing = [r for r in rows if r["verdict"] == "fail"]
    lines = [f"business rules: {len(rows)} in profile.yaml, {len(checked)} "
             f"listed by the cases of run {run_id}, {len(failing)} failing"]
    for r in rows:
        tags = [r["status"]] + r["checked_by"]
        head = r["verdict"].upper() if r["verdict"] == "fail" else r["verdict"]
        if r["verdict"] == "judged":
            jv = r["judged_verdict"]
            head = f"{jv['state']} {jv['verdict']}" if jv else "unjudged"
        source = f"  source: {r['source']}" if r.get("source") else ""
        lines.append(f"  {head:<16} {r['id']}  [{', '.join(tags)}]{source}")
        if r["verdict"] == "fail" and r.get("quote"):
            lines.append(f"    quote: {json.dumps(r['quote'], ensure_ascii=False)}")
        s = r["scripted"]
        if s["pass"] + s["fail"] + s["unscored"]:
            lines.append(f"    scripted: {s['fail']} fail, {s['pass']} pass, "
                         f"{s['unscored']} unscored")
        for entry in [c for c in s["cases"] if c["verdict"] == "fail"][:SHOWN]:
            what = "; ".join(f"{f['check']} '{f['entry']}' {f['reason']}"
                             for f in entry["failed"])
            gate = "" if entry["closes_gate"] else " (not gating)"
            lines.append(f"      {case_label(entry)}: {what}{gate}")
        extra = s["fail"] - SHOWN
        if extra > 0:
            lines.append(f"      and {extra} more")
        h = r["holdout"]
        if h["checked"]:
            lines.append(f"    sealed holdout: {h['fail']} of {h['checked']} "
                         "case(s) fail (counts only)")
        for entry in r["judged"]["cases"]:
            if entry["state"] == "unjudged":
                lines.append(f"    judged: {entry['case_id']} unjudged "
                             f"({entry['reason']})")
            else:
                quote = "  " + json.dumps(entry["quotes"][0],
                                          ensure_ascii=False) \
                    if entry.get("quotes") else ""
                lines.append(f"    judged: {entry['case_id']} "
                             f"{entry['state'].upper()} {entry['verdict']} "
                             f"({entry['criterion']}; never gates){quote}")
        if r["status"] == "inferred" and r["verdict"] != "not checked":
            lines.append("    inferred: reported only; it gates nothing until "
                         "a human confirms it")
        if r["status"] == "inferred" and r["gating_failures"]:
            lines.append("    WARNING: a gating case failed on this inferred "
                         "rule; validate_cases.py --profile refuses that "
                         "suite (unconfirmed_rule_gates)")
        if r["verdict"] == "not checked":
            lines.append("    no case in this run lists it in "
                         "expect.answer.rules")
    return lines


def main():
    ap = argparse.ArgumentParser(
        description="Report a finished run by business rule: which "
                    "profile.yaml rules its cases checked (expect.answer."
                    "rules), by script or by the judge, and which passed or "
                    "failed. Scores nothing; gates nothing.")
    add_version_flag(ap)
    ap.add_argument("run_dir", help="reports/<run-id>")
    ap.add_argument("--profile", required=True,
                    help="JSON profile (convert_suite.py --split-dir writes "
                         "profile.json; a converted.json works too)")
    ap.add_argument("--judged",
                    help="the judge pass's judged.json (default: "
                         "<run-dir>/judge/judged.json when present)")
    ap.add_argument("-o", "--out",
                    help="where to write (default <run-dir>/rules.json)")
    a = ap.parse_args()

    if not os.path.isdir(a.run_dir):
        die(f"{a.run_dir}: not a directory")
    results = read_object(os.path.join(a.run_dir, "results.json"),
                          "results.json")
    profile = load_profile(a.profile)
    rules = profile_rules(profile)
    judged_path = a.judged or os.path.join(a.run_dir, "judge", "judged.json")
    judged = None
    if a.judged or os.path.isfile(judged_path):
        judged = read_object(judged_path, "judged.json")
        if judged.get("schema") != JUDGED_SCHEMA:
            die(f"{judged_path}: schema {judged.get('schema')!r} is not "
                f"{JUDGED_SCHEMA!r}")
        if judged.get("run_id") != results.get("run_id"):
            die(f"{judged_path}: judged.json is for run "
                f"{judged.get('run_id')!r}, this run is "
                f"{results.get('run_id')!r}")
    jcases, jrules = judged_index(judged)

    rows = {rid: new_row(rid, rule) for rid, rule in rules.items()}
    unknown = set()
    for case_id, case_dir in case_dirs(a.run_dir):
        verdict = read_object(os.path.join(case_dir, "verdict.json"),
                              "verdict.json")
        holdout = verdict.get("holdout") is True
        gating = verdict.get("gating") is True and not verdict.get("canary")
        for prefix, expect, deciding in expects_of(case_dir, verdict):
            for rid in listed_rules(expect):
                row = rows.get(rid)
                if row is None:
                    unknown.add(rid)
                    continue
                if row["check"] is not None:
                    res = scripted_result(verdict, prefix, row["check"])
                    if holdout:
                        row["holdout"]["checked"] += 1
                        row["holdout"]["fail"] += res["verdict"] == "fail"
                    else:
                        row["scripted"][res["verdict"]] += 1
                        res = dict({"case_id": case_id}, **res)
                        if prefix:
                            res["turn"] = int(prefix[1:-1])
                        res["closes_gate"] = gating and \
                            res["verdict"] == "fail"
                        res["canary"] = verdict.get("canary") is True
                        row["scripted"]["cases"].append(res)
                        if res["closes_gate"] and \
                                case_id not in row["gating_failures"]:
                            row["gating_failures"].append(case_id)
                if holdout or not deciding:
                    continue
                res = judged_result(rid, expect, case_id, jcases, jrules,
                                    judged, row["check"] is not None)
                if res is None:
                    continue
                j = row["judged"]
                if res["state"] == "unjudged":
                    j["unjudged"] += 1
                else:
                    j[res["state"]][res["verdict"]] += 1
                j["cases"].append(dict({"case_id": case_id}, **res))

    rows = [finish(r) for r in rows.values()]
    lines = summary_lines(rows, results.get("run_id"))
    if unknown:
        lines.append("  cases list rules profile.yaml does not have: "
                     + ", ".join(sorted(unknown))
                     + " (validate_cases.py --profile: dangling_cover)")
    doc = {
        "schema": RULES_SCHEMA,
        "run_id": results.get("run_id"),
        "scoring_version": results.get("scoring_version"),
        "profile": {"file": a.profile, "sha256": sha256_of(a.profile)},
        "judged": ({"file": judged_path, "sha256": sha256_of(judged_path)}
                   if judged is not None else None),
        "rules": rows,
        "unknown_rules": sorted(unknown),
        "summary": {
            "rules": len(rows),
            "listed": sum(r["verdict"] != "not checked" for r in rows),
            "by_verdict": {v: sum(r["verdict"] == v for r in rows)
                           for v in ("fail", "pass", "unscored", "judged",
                                     "not checked")},
            "judged": {state: {v: sum(r["judged"][state][v] for r in rows)
                               for v in ("pass", "fail")}
                       for state in ("provisional", "calibrated")},
        },
        "lines": lines,
    }
    out = a.out or os.path.join(a.run_dir, "rules.json")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        die(f"cannot write output: {exc}")
    print(json.dumps({"rules_json": out, "summary": lines}, indent=2,
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
