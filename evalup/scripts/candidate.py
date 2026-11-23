#!/usr/bin/env python3
"""Make an optimize candidate's edit revertible, and save its decision (#143).

optimize changes the user's own prompt files and then runs a suite, which
takes long enough for the original text to be summarized out of the model's
memory. So a revert is never a re-edit from memory. Before this script, it
was `git status` / `git rev-parse` / `git checkout` / `git hash-object` in
the shell, none of which optimize's `Bash(evalup *)` pre-approves. Here it
is a few calls, and git is not needed at all:

  evalup candidate begin --state-dir <state> --app <app> --id <id> <path>...
      BEFORE the edit. Copies each surface file to
      <state>/candidates/<id>/before/<path> and records its sha256 in
      <state>/candidates/<id>/before.json. Refused when that record already
      exists: a second `begin` after the edit would snapshot the edit as the
      original.
  (the edit itself: the Edit tool, which asks -- editing a prompt is the
   user's call, README §Permissions)
  evalup candidate diff --state-dir <state> --app <app> --id <id>
      AFTER the edit. Writes <state>/candidates/<id>/diff.patch (a unified
      diff of each snapshot against the file now) and records each file's
      post-edit sha256. Refused when nothing changed.
  evalup candidate revert --state-dir <state> --app <app> --id <id>
      Restores every file from its snapshot (a deleted one is recreated),
      then checks its sha256 equals the recorded one, and names the files it
      restored. After a `diff`, a file whose content is neither the snapshot
      nor the diffed edit was changed by someone else since: refused,
      nothing restored, unless --force (that change is then lost). Before
      any `diff` the edit is not on record, so whatever the file holds is
      taken as the candidate's. A file already at its snapshot is left
      alone.
  evalup candidate gate --state-dir <state> --app <app> --id <id> <stats args>
      Runs `evalup stats` with the arguments given (the holdout decision:
      they must include --noise-floor) and saves its JSON output, verbatim,
      to <state>/candidates/<id>/holdout-gate.json, then prints it. Nothing
      is saved when stats fails.

Restoring from the copy rather than `git checkout` means a surface file with
uncommitted changes of the user's own is restored to exactly what it held,
those changes included.

Paths are relative to --app (absolute ones are allowed) and must resolve to
regular files inside it and OUTSIDE the state location: a candidate edits
the app's prompts, never a case file, a run's records or anything sealed. A
file with a second hard link is refused too (it could be a state file under
another name). A recorded path is re-checked on every call and refused once
any part of it is a symlink, and so is a symlink anywhere under
<state>/candidates/<id>/: a snapshot is read and written only where it was
made. --id follows the case-id rule (a directory name). Each file is written
to a fresh temp file and renamed over the old one, with its own mode kept.

Prints one JSON object on stdout. Exit 0 on success, 2 on bad input or a
refusal ({"error": ...} on stdout, like every script here).

Usage:
  candidate.py begin  --state-dir <state> --app <app> --id <id> <path>...
  candidate.py diff   --state-dir <state> --app <app> --id <id>
  candidate.py revert --state-dir <state> --app <app> --id <id> [--force]
  candidate.py gate   --state-dir <state> --app <app> --id <id> <stats args>
"""
import argparse
import contextlib
import difflib
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from _common import (
    SCHEMA_VERSION,
    BadJSON,
    add_version_flag,
    die,
    load_object,
    loads_strict,
    unsafe_case_id,
)

RECORD = "before.json"
GATE = "holdout-gate.json"
STATS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.py")


def inside(root, path):
    """True when `path` is strictly below `root` (both already realpath'd)."""
    return path != root and os.path.commonpath([root, path]) == root


def no_symlink(path):
    """True when no existing component of the absolute `path` is a symlink:
    realpath resolves only what exists, so it changes the path exactly
    when a component on it is a link."""
    return os.path.realpath(path) == os.path.normpath(path)


def read_bytes(path):
    """Bytes, not text: a prompt file is restored byte for byte."""
    try:
        return pathlib.Path(path).read_bytes()
    except OSError as e:
        die(f"cannot read {path}: {e}")


def sha256_file(path):
    return hashlib.sha256(read_bytes(path)).hexdigest()


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def candidate_dir(state_dir, cid):
    if not os.path.isdir(state_dir):
        die(f"bad input: --state-dir {state_dir}: not a directory")
    why = unsafe_case_id(cid)
    if why:
        die(f"bad input: --id {cid!r} "
            + why.replace("cases/<id>/", "candidates/<id>/"))
    cdir = os.path.join(os.path.realpath(state_dir), "candidates", cid)
    if not no_symlink(cdir):
        die(f"refused: {cdir} is or passes through a symlink; a candidate's "
            "files stay inside the state location")
    return cdir


def snapshot_path(cdir, rel):
    """<cdir>/before/<rel>, refused when any part of it is a symlink."""
    copy = os.path.normpath(os.path.join(cdir, "before", rel))
    if not inside(cdir, copy) or not no_symlink(copy):
        die(f"refused: the snapshot path {copy} is or passes through a "
            "symlink, or leaves the candidate's directory")
    return copy


def surface_paths(app, state_dir, paths):
    """(relative path, real path) per surface file, or die saying why."""
    if not os.path.isdir(app):
        die(f"bad input: --app {app}: not a directory")
    app_root = os.path.realpath(app)
    state_root = os.path.realpath(state_dir)
    out, seen = [], set()
    for p in paths:
        real = os.path.realpath(p if os.path.isabs(p)
                                else os.path.join(app_root, p))
        if not inside(app_root, real):
            die(f"refused: {p} resolves to {real}, outside the app {app_root}")
        if real == state_root or inside(state_root, real):
            die(f"refused: {p} resolves to {real}, inside the state location "
                f"{state_root}: a candidate edits the app's prompts, never "
                "evalup's own files (cases, runs, the seal)")
        if not os.path.isfile(real):
            die(f"refused: {real} is not a regular file")
        if os.stat(real).st_nlink > 1:
            die(f"refused: {real} has more than one hard link, so it may be "
                "another file under a second name (a state file among them)")
        rel = os.path.relpath(real, app_root)
        if rel in seen:
            continue
        seen.add(rel)
        out.append((rel, real))
    if not out:
        die("bad input: name at least one surface file")
    return app_root, out


def load_record(cdir, app):
    path = os.path.join(cdir, RECORD)
    if os.path.islink(path) or not os.path.isfile(path):
        die(f"refused: {path} does not exist -- run `evalup candidate begin` "
            "before the edit")
    doc = load_object(path)
    if not isinstance(doc.get("files"), list) \
            or not isinstance(doc.get("app"), str) \
            or not all(isinstance(e, dict) for e in doc["files"]):
        die(f"bad input: {path} is not a candidate record")
    if os.path.realpath(app) != os.path.realpath(doc["app"]):
        die(f"refused: --app {app} is not the app this candidate was begun "
            f"on ({doc['app']})")
    return doc


def default_mode():
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def write_bytes_atomic(path, data, mode=None, context=""):
    """Replace `path` whole through a fresh temp file (mkstemp: never an
    existing name, never a symlink), with `mode` or the umask's default."""
    parent = os.path.dirname(path)
    fd = tmp = None
    try:
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".candidate-", dir=parent)
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(data)
        os.chmod(tmp, default_mode() if mode is None else mode)
        os.replace(tmp, path)
        tmp = None
    except OSError as e:
        die(f"cannot write {path}: {e}{context}")
    finally:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def write_record(cdir, doc):
    write_bytes_atomic(os.path.join(cdir, RECORD),
                       (json.dumps(doc, indent=2) + "\n").encode("utf-8"))


def target_of(doc, entry, state_dir):
    """The live path of a recorded file: inside the app, outside the state
    location, and with no symlink on it (begin recorded a resolved path, so
    a link there now was put in since, and would aim the restore elsewhere).
    """
    app_root = os.path.realpath(doc["app"])
    rel = entry.get("path")
    if not isinstance(rel, str) or os.path.isabs(rel):
        die(f"bad input: candidate record names {rel!r}, not a relative path")
    path = os.path.normpath(os.path.join(app_root, rel))
    state_root = os.path.realpath(state_dir)
    if not inside(app_root, path) or path == state_root \
            or inside(state_root, path):
        die(f"refused: recorded path {rel} resolves to {path}, outside the "
            "app or inside the state location")
    if not no_symlink(path):
        die(f"refused: {path} is or passes through a symlink now, which "
            "begin did not record; nothing restored")
    return path


def checked_snapshot(cdir, entry):
    """The snapshot's bytes, refused unless they hash to the record."""
    copy = snapshot_path(cdir, entry["path"])
    if not os.path.isfile(copy):
        die(f"refused: the snapshot {copy} is missing; nothing restored")
    data = read_bytes(copy)
    if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
        die(f"refused: the snapshot {copy} does not match its recorded "
            "sha256; nothing restored")
    return data


def cmd_begin(a):
    cdir = candidate_dir(a.state_dir, a.id)
    if os.path.lexists(os.path.join(cdir, RECORD)):
        die(f"refused: {os.path.join(cdir, RECORD)} already exists: this "
            "candidate's originals are recorded, and a second snapshot would "
            "record the edit as the original. Use a new --id")
    app_root, files = surface_paths(a.app, a.state_dir, a.paths)
    entries = []
    for rel, real in files:
        data = read_bytes(real)
        mode = os.stat(real).st_mode & 0o777
        write_bytes_atomic(snapshot_path(cdir, rel), data)
        entries.append({"path": rel,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "mode": mode})
    doc = {"schema_version": SCHEMA_VERSION, "id": a.id, "app": app_root,
           "began_at": now(), "files": entries}
    write_record(cdir, doc)
    print(json.dumps({"candidate": a.id, "record": os.path.join(cdir, RECORD),
                      "snapshotted": [e["path"] for e in entries]}, indent=2))
    return 0


def cmd_diff(a):
    cdir = candidate_dir(a.state_dir, a.id)
    doc = load_record(cdir, a.app)
    chunks, changed = [], []
    for entry in doc["files"]:
        real = target_of(doc, entry, a.state_dir)
        before = checked_snapshot(cdir, entry)
        if not os.path.isfile(real):
            die(f"refused: {real} no longer exists")
        after = read_bytes(real)
        entry["after_sha256"] = hashlib.sha256(after).hexdigest()
        if entry["after_sha256"] == entry.get("sha256"):
            continue
        changed.append(entry["path"])
        old = before.decode("utf-8", "replace").splitlines(True)
        new = after.decode("utf-8", "replace").splitlines(True)
        for line in difflib.unified_diff(old, new, "a/" + entry["path"],
                                         "b/" + entry["path"]):
            chunks.append(line if line.endswith("\n")
                          else line + "\n\\ No newline at end of file\n")
    if not changed:
        die("refused: no surface file differs from its snapshot -- apply the "
            "edit first (nothing written)")
    patch = os.path.join(cdir, "diff.patch")
    write_bytes_atomic(patch, "".join(chunks).encode("utf-8"))
    doc["diffed_at"] = now()
    write_record(cdir, doc)
    print(json.dumps({"candidate": a.id, "diff": patch, "changed": changed},
                     indent=2))
    return 0


def cmd_revert(a):
    cdir = candidate_dir(a.state_dir, a.id)
    doc = load_record(cdir, a.app)
    plan = []
    for entry in doc["files"]:
        real = target_of(doc, entry, a.state_dir)
        data = checked_snapshot(cdir, entry)
        if os.path.lexists(real) and not os.path.isfile(real):
            die(f"refused: {real} is no longer a regular file; nothing "
                "restored")
        current = sha256_file(real) if os.path.isfile(real) else None
        if current == entry["sha256"]:
            continue
        after = entry.get("after_sha256")
        if current is not None and after is not None and current != after \
                and not a.force:
            die(f"refused: {real} changed since the candidate's edit was "
                "diffed (it is neither the snapshot nor the diffed edit), "
                "so someone else edited it; nothing restored. --force "
                "restores the snapshot anyway, losing that change")
        plan.append((entry, real, data))
    restored = []
    for entry, real, data in plan:
        mode = entry.get("mode")
        if not isinstance(mode, int) or isinstance(mode, bool) \
                or not 0 <= mode <= 0o777:
            mode = None
        write_bytes_atomic(real, data, mode, context=(
            f" (already restored and verified: {restored})" if restored
            else " (nothing restored)"))
        if sha256_file(real) != entry["sha256"]:
            die(f"restored {real}, but its sha256 does not match the "
                f"snapshot's: check it by hand (restored before it: "
                f"{restored})")
        restored.append(entry["path"])
    print(json.dumps({"candidate": a.id, "restored": restored,
                      "already_original": [e["path"] for e in doc["files"]
                                           if e["path"] not in restored],
                      "verified": True}, indent=2))
    return 0


def cmd_gate(a):
    cdir = candidate_dir(a.state_dir, a.id)
    load_record(cdir, a.app)
    args = list(a.stats_args)
    if args[:1] == ["--"]:
        args = args[1:]
    if "--noise-floor" not in args:
        die("refused: the holdout decision is `evalup stats <champion> "
            "<candidate> --noise-floor <noise-a> <noise-b> "
            "--candidate-looks <N>`; these arguments have no --noise-floor")
    proc = subprocess.run([sys.executable, STATS, *args],
                          capture_output=True, text=True, encoding="utf-8",
                          check=False)
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode
    try:
        doc = loads_strict(proc.stdout)
    except BadJSON as e:
        die(f"stats printed no JSON object ({e}); nothing saved")
    if not isinstance(doc, dict):
        die("stats printed no JSON object; nothing saved")
    path = os.path.join(cdir, GATE)
    write_bytes_atomic(path, proc.stdout.encode("utf-8"))
    sys.stdout.write(proc.stdout)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_version_flag(ap)
    sub = ap.add_subparsers(dest="command", required=True)
    for name, helptext in (("begin", "snapshot the surface files, before "
                                     "the edit"),
                           ("diff", "write diff.patch, after the edit"),
                           ("revert", "restore the snapshot and verify it"),
                           ("gate", "run the holdout decision and save it "
                                    "as holdout-gate.json")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--state-dir", required=True,
                       help="the state location, e.g. <app>/.evalup")
        p.add_argument("--id", required=True,
                       help="the candidate id: <state>/candidates/<id>/")
        p.add_argument("--app", required=True, help="the app's root")
        if name == "begin":
            p.add_argument("paths", nargs="+", metavar="path",
                           help="a surface file, relative to --app")
        if name == "revert":
            p.add_argument("--force", action="store_true",
                           help="restore even a file someone else changed "
                                "since the edit")
        if name == "gate":
            p.add_argument("stats_args", nargs=argparse.REMAINDER,
                           help="the arguments for `evalup stats`")
    a = ap.parse_args()
    return {"begin": cmd_begin, "diff": cmd_diff, "revert": cmd_revert,
            "gate": cmd_gate}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
