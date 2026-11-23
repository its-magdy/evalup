#!/usr/bin/env python3
"""Write a run's clusters.json from its verdicts.jsonl (#143).

optimize starts from `reports/<run-id>/clusters.json`, which analyze
`--cluster` writes after reading traces with the trace-analyzer. Straight
after a run, no analyze session has written it, and optimize used to group
the failures itself, in its head. This is that grouping, mechanical and
written down, so the cluster optimize picks and the rate its power check
uses come from one file:

  evalup cluster-failures reports/<run-id>

Population: the rows of verdicts.jsonl with `gating` true, `canary` false
and `verdict` pass or fail (the denominator of a cluster's rate R in
optimize step 4). Clusters: its `fail` rows, grouped by which `layers` rows
are `fail`, then by `category`, then by `failed_turn`; largest first, ties
in first-seen order. Each cluster is {"id", "failure_mode", "case_ids",
"layers", "category", "failed_turn", "rate", "surface", "effort",
"status"}, with `surface` and `effort` null (no trace was read: naming the
surface is optimize's step 1, reading the traces) and `status`
"unconfirmed". The file carries "source": "cluster-failures", so a reader
knows no trace and no human stood behind it. No verdict changes.

Refused (exit 2, nothing written):
  - a holdout run (mode `holdout`, selecting_split `holdout` or any row in
    the `holdout` set): its rows name sealed cases, and nothing reads them
    to cluster (analyze/SKILL.md). Only verdicts.jsonl, manifest.yaml and
    results.json's summary are read, never cases/;
  - a run whose results.json status is not "ok" (still running, cut off or
    aborted): its rows are partial;
  - an existing clusters.json that analyze wrote (no "source":
    "cluster-failures"): it holds trace-read failure modes and the user's
    answers. --force replaces it. One this script wrote is replaced.

Prints one JSON object on stdout: {"wrote", "population", "clusters"}.
Exit 0 on success (zero failures writes an empty "clusters" list), 2 on bad
input or a refusal ({"error": ...} on stdout, like every script here).

Usage:
  cluster_failures.py <reports/<run-id>> [--force]
"""
import argparse
import contextlib
import json
import os
import sys

from _common import SCHEMA_VERSION, add_version_flag, die, load_jsonl, load_object

SOURCE = "cluster-failures"
HOLDOUT = "holdout"


def read_run(run_dir):
    if not os.path.isdir(run_dir):
        die(f"bad input: {run_dir}: not a directory")
    for name in ("manifest.yaml", "results.json", "verdicts.jsonl"):
        if not os.path.isfile(os.path.join(run_dir, name)):
            die(f"not clustered: {run_dir} holds no {name} -- a run "
                "directory is reports/<run-id>")
    manifest = load_object(os.path.join(run_dir, "manifest.yaml"))
    if HOLDOUT in (manifest.get("mode"), manifest.get("selecting_split")):
        die("not clustered: a holdout run names sealed cases; cluster the "
            "latest regression run instead")
    summary = load_object(os.path.join(run_dir, "results.json")) \
        .get("summary")
    status = summary.get("status") if isinstance(summary, dict) else None
    if status != "ok":
        die(f"not clustered: results.json status is {status!r}, not 'ok': "
            "the run's rows are partial")
    rows = load_jsonl(os.path.join(run_dir, "verdicts.jsonl"))
    for row in rows:
        if not isinstance(row, dict):
            die("bad input: verdicts.jsonl holds a row that is not an object")
        if HOLDOUT in str(row.get("set")).split(","):
            die("not clustered: verdicts.jsonl holds a holdout row; a run "
                "that reached the seal is never clustered")
    return manifest, rows


def counted(row):
    return row.get("gating") is True and row.get("canary") is not True \
        and row.get("verdict") in ("pass", "fail")


def cluster(rows):
    population = [r for r in rows if counted(r)]
    groups = {}
    for row in population:
        if row.get("verdict") != "fail":
            continue
        layers = row.get("layers") if isinstance(row.get("layers"),
                                                 dict) else {}
        failing = sorted(name for name, v in layers.items() if v == "fail")
        key = (tuple(failing), row.get("category"), row.get("failed_turn"))
        groups.setdefault(key, []).append(row.get("case_id"))
    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    out = []
    for i, ((failing, category, turn), ids) in enumerate(ordered, 1):
        mode = (f"fails {', '.join(failing) or 'no single layer'}"
                f" in category {category!r}"
                + (f" at turn {turn}" if turn is not None else ""))
        out.append({"id": f"c{i}", "failure_mode": mode, "case_ids": ids,
                    "layers": list(failing), "category": category,
                    "failed_turn": turn,
                    "rate": round(len(ids) / len(population), 4),
                    "surface": None, "effort": None,
                    "status": "unconfirmed"})
    return len(population), out


def existing_is_ours(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and doc.get("source") == SOURCE


def write_atomic(path, doc):
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        die(f"cannot write {path}: {e}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_version_flag(ap)
    ap.add_argument("run_dir", help="the run to cluster, reports/<run-id>")
    ap.add_argument("--force", action="store_true",
                    help="replace a clusters.json analyze wrote")
    a = ap.parse_args()
    run_dir = os.path.abspath(a.run_dir)
    manifest, rows = read_run(run_dir)
    path = os.path.join(run_dir, "clusters.json")
    if os.path.lexists(path) and not a.force and not existing_is_ours(path):
        die(f"not clustered: {path} exists and analyze wrote it (trace-read "
            "failure modes, the user's answers): read it, or pass --force "
            "to replace it")
    population, clusters = cluster(rows)
    if population == 0 and clusters == []:
        note = "no gating, non-canary pass/fail row: nothing to cluster"
    else:
        note = None
    doc = {"schema_version": SCHEMA_VERSION,
           "run_id": manifest.get("run_id"), "source": SOURCE,
           "population": population, "clusters": clusters}
    write_atomic(path, doc)
    print(json.dumps({"wrote": path, "population": population,
                      "clusters": clusters, "note": note}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
