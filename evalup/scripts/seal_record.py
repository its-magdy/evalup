#!/usr/bin/env python3
"""Write optimize's seal record, reports/holdout-seal.json (#143).

The record names the runs one seal's holdout gate compares: the noise pair
(two runs of the unedited champion, measured once per seal), the current
champion's holdout run and every candidate's (optimize/SKILL.md step 5).
Written by hand, a live session appended to it with ad-hoc Python; a wrong
id or a version copied from memory makes a later session reuse a pair that
measured another seal, or spend two more looks on one it already has. So
every write here copies the versions from the run's own manifest.yaml:

  evalup seal-record noise reports/<run-id>
      After each noise run. The first makes a fresh record: noise_runs
      [<run-id>], champion_run <run-id>, candidate_runs [], and
      dataset_version / scoring_version / k from the manifest. A record for
      another seal (any of the three differs) is replaced, and the output
      names it. The second appends to noise_runs. A third is refused.
  evalup seal-record candidate reports/<run-id>
      After a candidate's holdout run. Refused before the pair is whole.
      Appends to candidate_runs and prints `candidate_looks` (that list's
      length): stats.py's --candidate-looks.
  evalup seal-record keep reports/<run-id>
      After a keep: champion_run becomes that candidate run.
  evalup seal-record show reports
      Prints the record, whether its pair is whole, which named runs have no
      directory, and whether its scoring_version is this harness's. It does
      not know the next run's dataset_version (that is datasets/dataset.yaml,
      YAML this script does not read): compare it there.
  evalup seal-record archive <state-dir> [--holdout-ledger <path>]
                             [--from-version <N>]
      After a grow made a new seal (#157): sealed cases added and
      `dataset_version` bumped in datasets/dataset.yaml. Copies the look
      ledger (datasets/holdout-looks.jsonl) byte for byte to
      holdout-looks-v<old>.jsonl beside it, verifies the copy, then empties
      the ledger, so the new seal starts with all 5 looks. It moves
      reports/holdout-seal.json, the old seal's record, to
      reports/holdout-seal-v<old>.json the same way, so `show` then says
      there is no record and the new seal's noise pair is measured. <old>
      is the highest dataset_version among the manifests of the ledger's
      runs and the seal record; --from-version names it when neither has
      one, and must agree with them when they do. It is --unseal's archive
      step without the unseal line, and it shows no case. A ledger with no
      line is nothing to archive: it says so, writes nothing, exit 0.
      Refused, nothing written: an archive file that already exists, a
      current dataset_version not above <old> and every archived version, a
      dataset.yaml without one plain `dataset_version: <int>` line, or a
      ledger that changed while it ran.

Each write refuses (exit 2, nothing written) a run that is not a finished
holdout run: no manifest.yaml, a run_id that does not match its directory,
a mode that is not `holdout`, a results.json status other than "ok" (a run
cut off at `running` is resumed under its id first), non-integer versions,
or versions that differ from the record's. A run already in the record is a
no-op that says so. The record always sits at the run's parent directory's
holdout-seal.json, i.e. reports/holdout-seal.json.

It reads a holdout run's manifest.yaml and results.json summary only:
never a case file, a verdict or anything else under cases/, so a
pre-approved call cannot open sealed content. It is not the ledger: the
looks spent are still the ledger's line count.

Prints one JSON object on stdout. Exit 0 on success, 2 on bad input or a
refusal ({"error": ...} on stdout, like every script here).

Usage:
  seal_record.py {noise,candidate,keep} <reports/<run-id>>
  seal_record.py show <reports>
  seal_record.py archive <state-dir> [--holdout-ledger <path>]
                 [--from-version <N>]
"""
import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile

from _common import (
    SCHEMA_VERSION,
    SCORING_VERSION,
    BadJSON,
    add_version_flag,
    die,
    load_object,
    load_text,
    loads_strict,
)

NAME = "holdout-seal.json"
DEFAULT_LEDGER = os.path.join("datasets", "holdout-looks.jsonl")
DATASET_YAML = os.path.join("datasets", "dataset.yaml")
VERSIONS = ("dataset_version", "scoring_version", "k")


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def holdout_run(run_dir):
    """(run_id, versions) of a finished holdout run, or die saying why."""
    if not os.path.isdir(run_dir):
        die(f"bad input: {run_dir}: not a directory")
    manifest_path = os.path.join(run_dir, "manifest.yaml")
    results_path = os.path.join(run_dir, "results.json")
    for path in (manifest_path, results_path):
        if not os.path.isfile(path):
            die(f"not recorded: {run_dir} holds no {os.path.basename(path)} "
                "-- a run directory is reports/<run-id>")
    manifest = load_object(manifest_path)
    problems = []
    run_id = manifest.get("run_id")
    if run_id != os.path.basename(run_dir):
        problems.append(f"manifest run_id {run_id!r} does not match the "
                        "directory name")
    if manifest.get("mode") != "holdout":
        problems.append(f"mode is {manifest.get('mode')!r}: the seal record "
                        "names holdout runs only")
    for key in VERSIONS:
        if not is_int(manifest.get(key)):
            problems.append(f"manifest {key} is {manifest.get(key)!r}, not "
                            "an integer")
    summary = load_object(results_path).get("summary")
    status = summary.get("status") if isinstance(summary, dict) else None
    if status != "ok":
        problems.append(f"results.json status is {status!r}, not 'ok'"
                        + (": the run is still going or was cut off -- "
                           "resume it under the same id (no new look), then "
                           "record it" if status == "running" else ""))
    if problems:
        die("not recorded: " + " | ".join(problems))
    return run_id, {key: manifest[key] for key in VERSIONS}


def read_record(path):
    if not os.path.isfile(path):
        return None
    try:
        doc = loads_strict(load_text(path))
    except BadJSON as e:
        die(f"bad input: {path} is unreadable ({e}); move it aside, then "
            "record the noise runs again")
    if not isinstance(doc, dict) or not isinstance(doc.get("noise_runs"),
                                                   list) \
            or not isinstance(doc.get("candidate_runs"), list):
        die(f"bad input: {path} is not a seal record; move it aside, then "
            "record the noise runs again")
    return doc


def write_atomic(path, doc, new_name=False):
    """Replace `path` whole through a fresh mkstemp file (never an existing
    name, never a symlink), with the mode a plain open would give it. `doc`
    is JSON, or bytes written as they are. With new_name, an existing
    `path` is refused instead of replaced (os.link never overwrites)."""
    fd = tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".evalup-", dir=os.path.dirname(path))
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(doc if isinstance(doc, bytes)
                    else (json.dumps(doc, indent=2) + "\n").encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        if new_name:
            try:
                os.link(tmp, path)
            except FileExistsError:
                die(f"refused: {path} already exists; nothing written")
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
        tmp = None
    except OSError as e:
        die(f"cannot write {path}: {e}")
    finally:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def same_seal(doc, versions):
    return all(doc.get(key) == versions[key] for key in VERSIONS)


def pair_whole(doc):
    return len(doc.get("noise_runs") or []) == 2


def require_this_seal(doc, path, versions, what):
    if doc is None:
        die(f"not recorded: {path} does not exist -- record the noise pair "
            f"(`evalup seal-record noise`) before a {what}")
    if not same_seal(doc, versions):
        die(f"not recorded: this run's {versions} differ from the record's "
            f"{ {k: doc.get(k) for k in VERSIONS} }: it is another seal's "
            "record, so measure this seal's noise pair first")
    if not pair_whole(doc):
        die(f"not recorded: the record holds {len(doc['noise_runs'])} noise "
            "run(s), not 2 -- finish the pair, on the unedited app, first")


def cmd_noise(path, run_id, versions):
    doc = read_record(path)
    notes = []
    if doc is not None and not same_seal(doc, versions):
        notes.append(f"replaced the record of another seal "
                     f"({ {k: doc.get(k) for k in VERSIONS} }, noise runs "
                     f"{doc.get('noise_runs')})")
        doc = None
    if doc is None:
        doc = dict({"schema_version": SCHEMA_VERSION}, **versions,
                   noise_runs=[run_id], champion_run=run_id,
                   candidate_runs=[])
    elif run_id in doc["noise_runs"]:
        notes.append(f"{run_id} is already a noise run: nothing changed")
        return doc, notes, False
    elif run_id in doc["candidate_runs"]:
        die(f"not recorded: {run_id} is a candidate run of this seal, not a "
            "noise run")
    elif pair_whole(doc):
        die(f"not recorded: the pair is whole ({doc['noise_runs']}); a seal "
            "has one noise pair")
    else:
        doc["noise_runs"].append(run_id)
    return doc, notes, True


def cmd_candidate(path, run_id, versions):
    doc = read_record(path)
    require_this_seal(doc, path, versions, "candidate run")
    if run_id in doc["noise_runs"]:
        die(f"not recorded: {run_id} is a noise run of this seal")
    if run_id in doc["candidate_runs"]:
        return doc, [f"{run_id} is already a candidate run: nothing "
                     "changed"], False
    doc["candidate_runs"].append(run_id)
    return doc, [], True


def cmd_keep(path, run_id, versions):
    doc = read_record(path)
    require_this_seal(doc, path, versions, "keep")
    if run_id not in doc["candidate_runs"]:
        die(f"not recorded: {run_id} is not a candidate run of this seal "
            "(`evalup seal-record candidate` it first)")
    if doc.get("champion_run") == run_id:
        return doc, [f"{run_id} is already the champion: nothing changed"], \
            False
    doc["champion_run"] = run_id
    return doc, ["the noise pair measured the previous champion: later "
                 "looks of this seal are outside multi_look_false_keep_bound"
                 ], True


def cmd_show(reports):
    if not os.path.isdir(reports):
        die(f"bad input: {reports}: not a directory")
    path = os.path.join(reports, NAME)
    doc = read_record(path)
    if doc is None:
        return {"record": path, "exists": False, "pair_whole": False}
    named = list(doc["noise_runs"]) + list(doc["candidate_runs"]) \
        + [doc.get("champion_run")]
    missing = sorted({r for r in named if not isinstance(r, str)
                      or not os.path.isdir(os.path.join(reports, r))},
                     key=str)
    return {"record": path, "exists": True, "seal": doc,
            "pair_whole": pair_whole(doc),
            "candidate_looks": len(doc["candidate_runs"]),
            "missing_run_dirs": missing,
            "scoring_version_current":
                doc.get("scoring_version") == SCORING_VERSION}


def read_bytes(path):
    try:
        return pathlib.Path(path).read_bytes()
    except OSError as e:
        die(f"cannot read {path}: {e}")


def current_dataset_version(state_dir):
    path = os.path.join(state_dir, DATASET_YAML)
    if not os.path.isfile(path):
        die(f"refused: {path} does not exist, so dataset_version is 1: a "
            "grown seal bumps it first. Nothing written")
    found = re.findall(r"(?m)^dataset_version:[ \t]*(\d+)[ \t]*(?:#.*)?$",
                       load_text(path))
    if len(found) != 1:
        die(f"refused: {path} does not hold one plain `dataset_version: "
            "<int>` line; set it so, then re-run. Nothing written")
    return int(found[0])


def ledger_run_ids(data):
    """The run ids of a ledger's lines (an unseal line has none)."""
    ids = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("run_id"), str):
            ids.append(row["run_id"])
    return ids


def old_versions(reports, run_ids, record):
    """{source: dataset_version} the old seal's witnesses give."""
    seen = {}
    for run_id in run_ids:
        path = os.path.join(reports, run_id, "manifest.yaml")
        if os.path.basename(run_id) != run_id or not os.path.isfile(path):
            continue
        try:
            version = loads_strict(load_text(path)).get("dataset_version")
        except (BadJSON, AttributeError):
            continue
        if is_int(version):
            seen[f"reports/{run_id}/manifest.yaml"] = version
    if record is not None and is_int(record.get("dataset_version")):
        seen[f"reports/{NAME}"] = record["dataset_version"]
    return seen


def cmd_archive(state_dir, ledger_arg, from_version):
    if not os.path.isdir(state_dir):
        die(f"bad input: {state_dir}: not a directory")
    ledger = ledger_arg if os.path.isabs(ledger_arg) \
        else os.path.join(state_dir, ledger_arg)
    if not ledger.endswith(".jsonl"):
        die(f"bad input: --holdout-ledger must be a .jsonl file (got "
            f"{ledger_arg!r})")
    data = read_bytes(ledger) if os.path.isfile(ledger) else b""
    looks = sum(1 for line in data.decode("utf-8", "replace").splitlines()
                if line.strip())
    if not looks:
        return {"archived": False, "ledger": ledger, "looks": 0,
                "notes": ["the ledger has no line: nothing to archive, "
                          "nothing written. This seal starts with all 5 "
                          "looks"]}
    current = current_dataset_version(state_dir)
    reports = os.path.join(state_dir, "reports")
    record_path = os.path.join(reports, NAME)
    record = read_record(record_path) if os.path.isdir(reports) else None
    seen = old_versions(reports, ledger_run_ids(data), record)
    if from_version is not None:
        wrong = {k: v for k, v in seen.items() if v > from_version}
        if wrong:
            die(f"refused: --from-version {from_version} is below what "
                f"{wrong} record; nothing written")
        old = from_version
    elif seen:
        old = max(seen.values())
    else:
        die("refused: no manifest of the ledger's runs and no seal record "
            "names the old seal's dataset_version; pass --from-version <N> "
            "(the dataset_version before the grow). Nothing written")
    stem = ledger[:-len(".jsonl")]
    directory = os.path.dirname(ledger)
    archived = sorted(
        int(m.group(1)) for m in (
            re.fullmatch(re.escape(os.path.basename(stem)) + r"-v(\d+)\.jsonl",
                         name) for name in os.listdir(directory)) if m)
    if current <= max([old] + archived):
        die(f"refused: datasets/dataset.yaml's dataset_version is {current}, "
            f"not above the old seal's {old} and every archived version "
            f"{archived}: bump it with the grow first (a new seal is a new "
            "dataset_version). Nothing written")
    archive = f"{stem}-v{old}.jsonl"
    record_archive = os.path.join(reports, f"holdout-seal-v{old}.json")
    move_record = record is not None and is_int(
        record.get("dataset_version")) and record["dataset_version"] < current
    for path in [archive] + ([record_archive] if move_record else []):
        if os.path.lexists(path):
            die(f"refused: {path} already exists: this seal was archived "
                "already, or the name is taken. Nothing written")

    digest = hashlib.sha256(data).hexdigest()
    write_atomic(archive, data, new_name=True)
    if read_bytes(archive) != data:
        os.unlink(archive)
        die(f"refused: {archive} did not read back as the ledger; removed "
            "it, the ledger is unchanged")
    if read_bytes(ledger) != data:
        os.unlink(archive)
        die("refused: the ledger changed while archiving (a holdout run "
            "appended?); removed the archive, the ledger is unchanged. "
            "Re-run once no run is going")
    write_atomic(ledger, b"")
    if read_bytes(ledger) != b"":
        die(f"cannot empty {ledger}; the archive {archive} is whole")
    notes = [f"the ledger's {looks} look(s) of dataset_version {old} are in "
             f"{archive}; the ledger is empty, so this seal (dataset_version "
             f"{current}) starts with all 5 looks"]
    out = {"archived": True, "ledger": ledger, "archive": archive,
           "looks": looks, "sha256": digest, "from_version": old,
           "dataset_version": current, "seal_record": None}
    if move_record:
        write_atomic(record_archive, read_bytes(record_path), new_name=True)
        os.unlink(record_path)
        out["seal_record"] = record_archive
        notes.append(f"the old seal's record is {record_archive}: `evalup "
                     "seal-record show` now finds none, so this seal's "
                     "noise pair comes first")
    elif record is not None:
        notes.append(f"{record_path} is at dataset_version "
                     f"{record.get('dataset_version')!r}, not an older "
                     "seal's: left in place")
    out["notes"] = notes
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_version_flag(ap)
    ap.add_argument("command",
                    choices=("noise", "candidate", "keep", "show", "archive"))
    ap.add_argument("target", help="reports/<run-id> (show: reports; "
                                   "archive: the state dir)")
    ap.add_argument("--holdout-ledger", default=DEFAULT_LEDGER,
                    help="archive: the run plan's paths.holdout_ledger, "
                         f"relative to the state dir (default {DEFAULT_LEDGER})")
    ap.add_argument("--from-version", type=int, metavar="N",
                    help="archive: the dataset_version before the grow, when "
                         "no manifest of the ledger's runs names it")
    a = ap.parse_args()
    target = os.path.abspath(a.target)
    if a.command != "archive" and (a.from_version is not None
                                   or a.holdout_ledger != DEFAULT_LEDGER):
        ap.error("--holdout-ledger and --from-version are archive's only")
    if a.command == "archive":
        print(json.dumps(cmd_archive(target, a.holdout_ledger,
                                     a.from_version), indent=2))
        return 0
    if a.command == "show":
        print(json.dumps(cmd_show(target), indent=2))
        return 0
    run_id, versions = holdout_run(target)
    path = os.path.join(os.path.dirname(target), NAME)
    doc, notes, changed = {"noise": cmd_noise, "candidate": cmd_candidate,
                           "keep": cmd_keep}[a.command](path, run_id,
                                                        versions)
    if changed:
        write_atomic(path, doc)
    print(json.dumps({"record": path, "seal": doc, "changed": changed,
                      "pair_whole": pair_whole(doc),
                      "candidate_looks": len(doc["candidate_runs"]),
                      "notes": notes}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
