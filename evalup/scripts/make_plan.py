#!/usr/bin/env python3
"""Build run_cases.py's plan.json from convert_suite.py's output -- mechanically,
so the model never assembles a plan by hand.

The plan was the last big artifact still hand-built on every run. run/SKILL.md
described nine of its keys in prose, left six required ones to a 1,000-line
contract, and told the model to write the file "with python3 -c or jq". In the
first live session (2026-09-21) roughly half of 75 tool calls went to exactly
that: an ad-hoc script per attempt, five failed runner starts, six
re-conversions. Everything the plan needs is already in convert_suite.py's
document or is a fixed function of the mode, so it is a script's job --
the same finding that produced convert_suite.py and gate.py, one step later.

This script SELECTS and ASSEMBLES. It does not lint (validate_cases.py), does
not resolve `${VAR}` refs (the runner does, runner-contract.md SS3 -- so no
secret reaches the plan file), and does not run anything.

Mode -> selection, k, gate (run/SKILL.md SS0, references/run-modes.md):
  smoke       cases with `smoke` in split      k=1 (fixed)   soft
  regression  cases with `full` in split       k=3           hard
  targeted    --tag-filtered `full` (or        k=1           soft
              --split smoke) cases
  holdout     cases with `holdout` in split    k=1           decision
`--k` overrides the default except under smoke, where k is 1 by definition.

`full` (the `full` AND `holdout` splits in one run) is refused: the runner
labels every case with ONE selecting_split and rejects a case that does not
carry it, and the two splits are mutually exclusive, so no single plan can hold
both. Release validation is a `regression` run and a `holdout` run.

--tag matches a case's `unit`, or its `expect.route` -- never its id, which is
opaque on purpose. --failing-in <run-dir> narrows a targeted selection to the
cases that run scored `fail`, if that run is comparable (same dataset and
harness version); if it is not, the whole tagged set is kept and stderr says so.

--layer X disables every other capability-matrix layer with
`blocked_by: "--layer X"`, so they report `unscorable`, never `pass`.

Exit 0 and the plan on stdout, or with -o a one-object summary naming the run
id, the --out directory and the exact runner command. Exit 2 on bad input
({"error": ...} on stdout, like every script here).

Usage:
  make_plan.py <converted.json> --mode smoke --state-dir <app>/.evalup -o plan.json
  make_plan.py <converted.json> --mode targeted --tag billing --state-dir ... \
      -o plan.json
"""
import argparse
import datetime
import json
import os
import subprocess

from _common import (
    HARNESS_VERSION,
    RUN_ID_RE,
    add_version_flag,
    die,
    load_jsonl,
    load_object,
    write_output,
)

# mode -> (selecting_split, default k, gate)
MODES = {
    "smoke": ("smoke", 1, "soft"),
    "regression": ("full", 3, "hard"),
    "targeted": ("full", 1, "soft"),
    "holdout": ("holdout", 1, "decision"),
}
DEFAULT_LEDGER = "datasets/holdout-looks.jsonl"
DEFAULT_CALIBRATION = "judge/calibration.json"


def select(cases, split, tag):
    chosen = [c for c in cases
              if isinstance(c.get("split"), list) and split in c["split"]]
    if tag is not None:
        chosen = [c for c in chosen
                  if c.get("unit") == tag
                  or (c.get("expect") or {}).get("route") == tag]
    return chosen


def failing_ids(run_dir, dataset_version):
    """Ids that run scored `fail`, or None when it is not comparable.

    Same rule as the baseline diff (runner-contract.md SS5.6): a different
    dataset or harness version changes what a verdict means, so "the cases
    that failed last time" is not a set this run can inherit.
    """
    # manifest.yaml is written as JSON (a YAML subset) by the stdlib-only runner.
    manifest = load_object(os.path.join(run_dir, "manifest.yaml"))
    if manifest.get("dataset_version") != dataset_version \
            or manifest.get("harness_version") != HARNESS_VERSION:
        return None
    rows = load_jsonl(os.path.join(run_dir, "verdicts.jsonl"))
    return {row.get("case_id") for row in rows if row.get("verdict") == "fail"}


def git_state(repo):
    """(sha, "clean"|"dirty") of the app checkout, or (None, "n/a"). The
    manifest records what was evaluated; a repo that is not a git checkout, or
    a machine without git, is a fact to record, not an error."""
    def git(*argv):
        return subprocess.run(["git", "-C", repo, *argv], capture_output=True,
                              text=True, timeout=20, check=False)
    try:
        head = git("rev-parse", "HEAD")
        if head.returncode != 0:
            return None, "n/a"
        dirty = git("status", "--porcelain").stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None, "n/a"
    return head.stdout.strip(), "dirty" if dirty else "clean"


def oos_route(converted, flag):
    if flag is not None:
        return flag or None
    handling = (converted.get("scoring") or {}).get("oos_handling")
    if isinstance(handling, str) and handling.startswith("route:"):
        return handling.split(":", 1)[1].strip() or None
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Build run_cases.py's plan.json from convert_suite.py's "
                    "combined document: select the mode's cases, fill the "
                    "required keys, leave ${VAR} refs unresolved.")
    add_version_flag(ap)
    ap.add_argument("converted", help="convert_suite.py's combined document "
                                      "(its -o file)")
    ap.add_argument("--mode", required=True,
                    help="smoke | regression | targeted | holdout")
    ap.add_argument("--state-dir", required=True,
                    help="the state location (e.g. <app>/.evalup); the run is "
                         "written to <state-dir>/reports/<run-id>")
    ap.add_argument("-o", "--output", help="write the plan here and print a "
                    "summary (default: the plan itself on stdout)")
    ap.add_argument("--run-id", help="default: <mode>-<UTC now>")
    ap.add_argument("--k", type=int, help="repeats per case (not under smoke)")
    ap.add_argument("--tag", help="targeted: the unit / expect.route to select")
    ap.add_argument("--split", choices=("full", "smoke"),
                    help="targeted: the split to filter (default: full)")
    ap.add_argument("--failing-in", metavar="RUN_DIR",
                    help="targeted: only cases that run scored fail")
    ap.add_argument("--layer", help="score only this capability-matrix layer")
    ap.add_argument("--oos-route", help="the app's out-of-scope route name "
                    "(default: profile oos_handling `route:<name>`; '' = none)")
    ap.add_argument("--holdout-ledger", help="relative to --state-dir "
                    f"(default for holdout: {DEFAULT_LEDGER})")
    ap.add_argument("--judge-calibration", help="relative to --state-dir "
                    f"(default: {DEFAULT_CALIBRATION} when that file exists)")
    ap.add_argument("--dataset-version", type=int,
                    help="default: dataset.yaml's dataset_version, else 1")
    ap.add_argument("--timeout-s", type=float, help="per app call")
    ap.add_argument("--manifest-extra", metavar="FILE", help="a JSON object "
                    "merged over the computed manifest_extra (models, prompt "
                    "hashes, temperature, cost estimate -- what only the "
                    "caller knows)")
    a = ap.parse_args()

    if a.mode == "full":
        die("--mode full cannot be one plan: the runner labels every case with "
            "one selecting_split and the `full` and `holdout` splits are "
            "mutually exclusive. Run --mode regression, then --mode holdout")
    if a.mode not in MODES:
        die(f"--mode must be one of {', '.join(MODES)}, got {a.mode!r}")
    split, k, gate = MODES[a.mode]
    if a.mode == "targeted":
        if not a.tag:
            die("--mode targeted needs --tag <unit or route>: it has no "
                "'everything' form (that is regression)")
        split = a.split or split
    elif a.tag or a.split or a.failing_in:
        die("--tag, --split and --failing-in only narrow --mode targeted")
    if a.k is not None:
        if a.k < 1:
            die(f"--k must be >= 1, got {a.k}")
        if a.mode == "smoke":
            die("--k does not apply to --mode smoke (k is 1 by definition); "
                "use --mode regression --k N for repeats")
        k = a.k
    if a.run_id and not RUN_ID_RE.fullmatch(a.run_id):
        die(f"--run-id must match <mode>-<YYYYMMDDTHHMMSSZ>, got {a.run_id!r}")
    if not os.path.isdir(a.state_dir):
        die(f"bad input: {a.state_dir}: not a directory")
    state_dir = os.path.abspath(a.state_dir)

    converted = load_object(a.converted)
    for key in ("adapter", "capability_matrix", "cases"):
        if key not in converted:
            die(f"bad input: {a.converted}: no {key!r} -- this is not "
                "convert_suite.py's combined document (its -o file)")
    if not isinstance(converted["cases"], list):
        die(f"bad input: {a.converted}: cases must be a list")
    all_cases = [c for c in converted["cases"] if isinstance(c, dict)]

    manifest = converted.get("manifest") or {}
    dataset_version = a.dataset_version
    if dataset_version is None:
        declared = manifest.get("dataset_version", manifest.get("version"))
        dataset_version = declared if isinstance(declared, int) \
            and not isinstance(declared, bool) else 1

    cases = select(all_cases, split, a.tag)
    notes = []
    if a.failing_in:
        failed = failing_ids(a.failing_in, dataset_version)
        if failed is None:
            notes.append(f"{a.failing_in} is not comparable (dataset or "
                         "harness version differs): running every tagged case")
        else:
            narrowed = [c for c in cases if c.get("id") in failed]
            if narrowed:
                cases = narrowed
            else:
                notes.append(f"no tagged case failed in {a.failing_in}: "
                             "running every tagged case")
    if not cases:
        have = sorted({s for c in all_cases for s in (c.get("split") or [])
                       if isinstance(s, str)})
        die(f"no case selected: mode {a.mode} reads split {split!r}"
            + (f" and tag {a.tag!r}" if a.tag else "")
            + f"; the suite's splits are {have or 'absent'}")

    matrix = converted["capability_matrix"]
    if a.layer:
        if not isinstance(matrix, dict) or a.layer not in matrix:
            die(f"--layer {a.layer!r} is not in the capability matrix: "
                f"{sorted(matrix) if isinstance(matrix, dict) else matrix}")
        matrix = {name: body if name == a.layer else
                  {"enabled": False, "blocked_by": f"--layer {a.layer}"}
                  for name, body in matrix.items()}

    ledger = a.holdout_ledger
    if ledger is None and split == "holdout":
        ledger = DEFAULT_LEDGER
    calibration = a.judge_calibration
    if calibration is None and os.path.isfile(
            os.path.join(state_dir, DEFAULT_CALIBRATION)):
        calibration = DEFAULT_CALIBRATION

    adapter = converted["adapter"]
    app = (adapter.get("app") or {}) if isinstance(adapter, dict) else {}
    repo = app.get("repo")
    repo_path = repo if isinstance(repo, str) and os.path.isabs(repo) \
        else os.path.normpath(os.path.join(state_dir, repo or "."))
    sha, tree = git_state(repo_path)
    profile_judge = (converted.get("profile") or {}).get("judge")
    extra = {
        "dataset_version": dataset_version,
        "app": {"name": app.get("name"), "repo": repo, "git_sha": sha,
                "git_tree": tree},
        "judge": profile_judge if isinstance(profile_judge, dict)
        else {"model": None, "status": "uncalibrated"},
    }
    if a.manifest_extra:
        extra.update(load_object(a.manifest_extra))

    stamp = datetime.datetime.now(datetime.timezone.utc)
    run_id = a.run_id or f"{a.mode}-{stamp.strftime('%Y%m%dT%H%M%SZ')}"
    scoring = {"oos_route": oos_route(converted, a.oos_route),
               "id_pattern": (converted.get("scoring") or {}).get(
                   "record_id_pattern")}
    plan = {
        "plan_version": 1, "run_id": run_id, "mode": a.mode, "k": k,
        "gate": gate, "selecting_split": split,
        "paths": {"scripts_dir": os.path.dirname(os.path.abspath(__file__)),
                  "state_dir": state_dir, "holdout_ledger": ledger,
                  "judge_calibration": calibration},
        "adapter": adapter, "capability_matrix": matrix, "cases": cases,
        "scoring": scoring, "manifest_extra": extra,
    }
    if a.timeout_s is not None:
        plan["execution"] = {"timeout_s": a.timeout_s}

    text = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
    if not a.output:
        write_output(None, text)
        return
    write_output(a.output, text)
    out_dir = os.path.join(state_dir, "reports", run_id)
    runner = os.path.join(plan["paths"]["scripts_dir"], "run_cases.py")
    print(json.dumps({
        "plan": os.path.abspath(a.output), "run_id": run_id, "mode": a.mode,
        "selecting_split": split, "k": k, "gate": gate, "cases": len(cases),
        "out": out_dir, "notes": notes,
        "run": f'python3 "{runner}" --plan "{os.path.abspath(a.output)}" '
               f'--out "{out_dir}"',
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
