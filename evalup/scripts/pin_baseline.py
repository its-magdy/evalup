#!/usr/bin/env python3
"""Write reports/baseline.json from a finished run's manifest (#53).

`baseline.json` names the run every later `--baseline-verdicts` diff pairs
against, and until this script the model wrote it by hand: four keys copied
from a manifest, where a typo or a value from memory silently breaks every
later diff. In one live session (2026-11-20) the suite went from dataset
version 1 to 4 with three hand pins in forty minutes. The pin is still the
skill's DECISION (runner-contract.md SS13); this script is the mechanical
step, as gate.py is for the verdict.

It refuses (exit 2, nothing written) a run that cannot be a baseline:
  - results.json's summary.status is not "ok" -- `running` (a run still in
    progress, or one whose runner was killed: wait_run.py says which),
    aborted or incomplete. run_history.py and run/SKILL.md SS3 call such a
    run "not quotable, not a baseline";
  - the run directory fails run_cases.py --verify (a missing or inconsistent
    artifact), or its manifest has no run_id matching the directory, or no
    integer dataset_version / scoring_version / k;
  - a holdout run (mode or selecting_split `holdout`): the seal's runs never
    become the baseline, which names a regression run;
  - a run where no case gates (every case still `gating: false`, so no label
    was accepted): run/SKILL.md SS4's "never pin a run with no selected case
    accepted". `--allow-ungated` overrides it for a suite the user decided
    to pin unreviewed; the output then says so.

It pins, and says so in `notes`, when:
  - the run's gate is closed (gate.py's reasons, at its defaults): a red run
    is a legitimate baseline -- a diff measures change, not health -- but the
    user should know which one they pinned;
  - the run used k=1: every later diff against it needs `--k 1`, because the
    runner refuses a pair whose k differs (runner-contract.md SS5.6, D7);
  - the run's mode is not `regression`;
  - it replaces an earlier pointer (named, with its dataset version).

baseline.json is {"schema_version", "run_id", "dataset_version",
"scoring_version", "k", "mode", "pinned_at"}: the first four are what
start/SKILL.md compares, `k` is what the next diff must pass. Written
temp-then-rename, so a reader never sees half a pointer.

  0  pinned; one JSON object on stdout (baseline, pointer, replaced, notes)
  2  refused, or bad input ({"error": ...} on stdout, like every script here)

Usage:
  pin_baseline.py <reports/<run-id>> [--reports <dir>] [--allow-ungated]
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from _common import SCHEMA_VERSION, add_version_flag, die, load_object
from gate import evaluate, verify_reasons


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def read_manifest(run_dir):
    path = os.path.join(run_dir, "manifest.yaml")
    if not os.path.isfile(path):
        die(f"not pinned: {run_dir} holds no manifest.yaml -- a run directory "
            "is reports/<run-id>")
    return load_object(path)


def refusals(run_dir, manifest, results, allow_ungated):
    """Every reason this run cannot be pinned, not just the first."""
    out = []
    summary = results.get("summary")
    status = summary.get("status") if isinstance(summary, dict) else None
    if status == "running":
        out.append("results.json says status 'running': the run is still in "
                   "progress or its runner was killed (wait_run.py tells "
                   "which; resume it with run_cases.py --resume)")
    elif status != "ok":
        out.append(f"run status is {status!r}, not 'ok': not quotable, not a "
                   "baseline")
    out.extend(verify_reasons(run_dir))
    run_id = manifest.get("run_id")
    if run_id != os.path.basename(os.path.normpath(run_dir)):
        out.append(f"manifest run_id {run_id!r} does not match the directory "
                   "name, so the pointer would not resolve to this run")
    for key in ("dataset_version", "scoring_version", "k"):
        if not is_int(manifest.get(key)):
            out.append(f"manifest {key} is {manifest.get(key)!r}, not an "
                       "integer; the diff's comparability check needs it")
    if "holdout" in (manifest.get("mode"), manifest.get("selecting_split")):
        out.append("a holdout run never becomes the baseline: it names a "
                   "regression run, which shares no case with the seal")
    rows = results.get("cases")
    gating = [r for r in rows if isinstance(r, dict) and r.get("gating")] \
        if isinstance(rows, list) else []
    if not gating and not allow_ungated:
        out.append("no case in this run gates (every label still pending): "
                   "accept labels and run `regression`, or pass "
                   "--allow-ungated if the user chose to pin unreviewed")
    return out


def notes_for(manifest, results, previous, allow_ungated):
    notes = []
    reasons, _ = evaluate(results, max_infra_rate=0.05)
    if reasons:
        notes.append("the gate is CLOSED on this run (" + "; ".join(reasons)
                     + "): pinned as the baseline anyway -- a diff measures "
                     "change, so later runs are compared against a red run")
    k = manifest.get("k")
    if k == 1:
        notes.append("k=1: every later diff against this baseline needs "
                     "--k 1 (the runner refuses a k mismatch); re-pin at the "
                     "mode's default k to diff at it")
    elif is_int(k):
        notes.append(f"later diffs against this baseline need k={k}")
    if manifest.get("mode") != "regression":
        notes.append(f"mode is {manifest.get('mode')!r}, not 'regression': "
                     "a diff pairs case ids, so later runs must select the "
                     "same cases")
    rows = results.get("cases")
    if allow_ungated and not any(isinstance(r, dict) and r.get("gating")
                                 for r in (rows if isinstance(rows, list)
                                           else [])):
        notes.append("--allow-ungated: no case in this run gates, so this "
                     "baseline rests on unreviewed labels")
    if previous:
        was = (previous.get("run_id"), previous.get("dataset_version"))
        if was[0] == manifest.get("run_id"):
            notes.append("this run was already the baseline; the pointer is "
                         "rewritten from its manifest")
        else:
            notes.append(f"replaces baseline {was[0]!r} (dataset_version "
                         f"{was[1]!r})")
        if is_int(was[1]) and was[1] > manifest.get("dataset_version"):
            notes.append(f"this run's dataset_version "
                         f"{manifest.get('dataset_version')} is OLDER than "
                         f"the replaced pointer's {was[1]}")
    return notes


def write_atomic(path, doc):
    directory = os.path.dirname(path) or "."
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".baseline.",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        die(f"cannot write {path}: {e}")


def main():
    ap = argparse.ArgumentParser(
        description="Pin a finished run as reports/baseline.json, copying the "
                    "pointer's fields from its manifest. Refuses a run that is "
                    "running, incomplete, holdout or ungated (exit 2); says "
                    "when the gate is closed or k=1.")
    add_version_flag(ap)
    ap.add_argument("run_dir", help="the run to pin, reports/<run-id>")
    ap.add_argument("--reports", metavar="DIR",
                    help="where baseline.json goes (default: the run "
                         "directory's parent, i.e. reports/)")
    ap.add_argument("--allow-ungated", action="store_true",
                    help="pin even though no case in the run gates (the "
                         "user chose to pin unreviewed labels)")
    a = ap.parse_args()

    run_dir = os.path.normpath(a.run_dir)
    if not os.path.isdir(run_dir):
        die(f"bad input: {run_dir}: not a directory")
    manifest = read_manifest(run_dir)
    results_path = os.path.join(run_dir, "results.json")
    if not os.path.isfile(results_path):
        die(f"not pinned: {run_dir} holds no results.json -- the run has not "
            "started writing it, or this is not a run directory")
    results = load_object(results_path)
    problems = refusals(run_dir, manifest, results, a.allow_ungated)
    if problems:
        die("not pinned: " + " | ".join(problems))

    reports = a.reports or os.path.dirname(os.path.abspath(run_dir))
    if not os.path.isdir(reports):
        die(f"bad input: --reports {reports}: not a directory")
    pointer = os.path.join(reports, "baseline.json")
    previous = None
    if os.path.isfile(pointer):
        try:
            with open(pointer, encoding="utf-8") as f:
                previous = json.load(f)
        except (OSError, ValueError):
            previous = None
        if not isinstance(previous, dict):
            previous = {"run_id": "(unreadable pointer)"}
    doc = {"schema_version": SCHEMA_VERSION,
           "run_id": manifest["run_id"],
           "dataset_version": manifest["dataset_version"],
           "scoring_version": manifest["scoring_version"],
           "k": manifest["k"],
           "mode": manifest.get("mode"),
           "pinned_at": datetime.now(timezone.utc).strftime(
               "%Y-%m-%dT%H:%M:%SZ")}
    notes = notes_for(manifest, results, previous, a.allow_ungated)
    write_atomic(pointer, doc)
    print(json.dumps({"pinned": doc["run_id"], "baseline": pointer,
                      "pointer": doc,
                      "replaced": previous.get("run_id") if previous else None,
                      "notes": notes}, indent=2, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
