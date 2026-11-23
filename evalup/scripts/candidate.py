#!/usr/bin/env python3
"""Make an optimize candidate's edit revertible: snapshot, diff, revert (#143).

optimize changes the user's own prompt files and then runs a suite, which
takes long enough for the original text to be summarized out of the model's
memory. So a revert is never a re-edit from memory. Before this script, it
was `git status` / `git rev-parse` / `git checkout` / `git hash-object` in
the shell, none of which optimize's `Bash(evalup *)` pre-approves. Here it
is three calls, and git is not needed at all:

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
      Restores every file from its snapshot, then checks its sha256 equals
      the recorded one, and names the files it restored. A file whose
      content is neither the snapshot nor the recorded post-edit text was
      changed by someone else since: refused, nothing restored, unless
      --force (that change is then lost). A file already at its snapshot is
      left alone.

Restoring from the copy rather than `git checkout` means a surface file with
uncommitted changes of the user's own is restored to exactly what it held,
those changes included.

Paths are relative to --app (absolute ones are allowed) and must resolve,
symlinks followed, to regular files inside it and OUTSIDE the state
location: a candidate edits the app's prompts, never a case file, a run's
records or anything sealed. --id follows the case-id rule (a directory
name). Each file is written temp-then-rename with its own mode kept.

Prints one JSON object on stdout. Exit 0 on success, 2 on bad input or a
refusal ({"error": ...} on stdout, like every script here).

Usage:
  candidate.py begin  --state-dir <state> --app <app> --id <id> <path>...
  candidate.py diff   --state-dir <state> --app <app> --id <id>
  candidate.py revert --state-dir <state> --app <app> --id <id> [--force]
"""
import argparse
import contextlib
import difflib
import hashlib
import json
import os
import pathlib
import shutil
import sys
from datetime import datetime, timezone

from _common import SCHEMA_VERSION, add_version_flag, die, unsafe_case_id

RECORD = "before.json"


def inside(root, path):
    """True when `path` is strictly below `root` (both already realpath'd)."""
    return path != root and os.path.commonpath([root, path]) == root


def sha256_file(path):
    return hashlib.sha256(read_bytes(path)).hexdigest()


def read_bytes(path):
    """Bytes, not text: a prompt file is restored byte for byte."""
    try:
        return pathlib.Path(path).read_bytes()
    except OSError as e:
        die(f"cannot read {path}: {e}")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def candidate_dir(state_dir, cid):
    if not os.path.isdir(state_dir):
        die(f"bad input: --state-dir {state_dir}: not a directory")
    why = unsafe_case_id(cid)
    if why:
        die(f"bad input: --id {cid!r} {why.replace('cases/<id>/', 'candidates/<id>/')}")
    return os.path.join(os.path.realpath(state_dir), "candidates", cid)


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
    if not os.path.isfile(path):
        die(f"refused: {path} does not exist -- run `evalup candidate begin` "
            "before the edit")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        die(f"bad input: {path}: {e}")
    if not isinstance(doc, dict) or not isinstance(doc.get("files"), list) \
            or not isinstance(doc.get("app"), str):
        die(f"bad input: {path} is not a candidate record")
    if os.path.realpath(app) != os.path.realpath(doc["app"]):
        die(f"refused: --app {app} is not the app this candidate was begun "
            f"on ({doc['app']})")
    return doc


def write_bytes_atomic(path, data, mode=None):
    """Replace `path` whole, keeping `mode` (or the umask's default)."""
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pathlib.Path(tmp).write_bytes(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        die(f"cannot write {path}: {e}")


def write_record(cdir, doc):
    write_bytes_atomic(os.path.join(cdir, RECORD),
                       (json.dumps(doc, indent=2) + "\n").encode("utf-8"))


def target_of(doc, entry, state_dir):
    """The live path of a recorded file, re-checked inside the app and
    outside the state location."""
    app_root = os.path.realpath(doc["app"])
    rel = entry.get("path")
    if not isinstance(rel, str) or os.path.isabs(rel):
        die(f"bad input: candidate record names {rel!r}, not a relative path")
    real = os.path.realpath(os.path.join(app_root, rel))
    state_root = os.path.realpath(state_dir)
    if not inside(app_root, real) or real == state_root \
            or inside(state_root, real):
        die(f"refused: recorded path {rel} resolves to {real}, outside the "
            "app or inside the state location")
    return real


def cmd_begin(a):
    cdir = candidate_dir(a.state_dir, a.id)
    if os.path.lexists(os.path.join(cdir, RECORD)):
        die(f"refused: {os.path.join(cdir, RECORD)} already exists: this "
            "candidate's originals are recorded, and a second snapshot would "
            "record the edit as the original. Use a new --id")
    app_root, files = surface_paths(a.app, a.state_dir, a.paths)
    entries = []
    for rel, real in files:
        copy = os.path.join(cdir, "before", rel)
        try:
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            shutil.copy2(real, copy)
        except OSError as e:
            die(f"cannot copy {real}: {e}")
        digest = sha256_file(real)
        if sha256_file(copy) != digest:
            die(f"the copy of {real} does not match it (changed while "
                "copying?): nothing recorded")
        entries.append({"path": rel, "sha256": digest,
                        "mode": os.stat(real).st_mode & 0o777})
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
        copy = os.path.join(cdir, "before", entry["path"])
        if not os.path.isfile(real):
            die(f"refused: {real} no longer exists")
        digest = sha256_file(real)
        entry["after_sha256"] = digest
        if digest == entry.get("sha256"):
            continue
        changed.append(entry["path"])
        old = read_bytes(copy).decode("utf-8", "replace").splitlines(True)
        new = read_bytes(real).decode("utf-8", "replace").splitlines(True)
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
        copy = os.path.join(cdir, "before", entry["path"])
        if not os.path.isfile(copy) or sha256_file(copy) != entry.get("sha256"):
            die(f"refused: the snapshot {copy} is missing or does not match "
                "its recorded sha256; nothing restored")
        current = sha256_file(real) if os.path.isfile(real) else None
        if current == entry["sha256"]:
            continue
        if current != entry.get("after_sha256") and not a.force:
            die(f"refused: {real} changed since the candidate's edit was "
                "recorded (it is neither the snapshot nor the diffed edit), "
                "so someone else edited it; nothing restored. --force "
                "restores the snapshot anyway, losing that change")
        plan.append((entry, real, copy))
    restored = []
    for entry, real, copy in plan:
        write_bytes_atomic(real, read_bytes(copy), entry.get("mode"))
        if sha256_file(real) != entry["sha256"]:
            die(f"restored {real}, but its sha256 does not match the "
                "snapshot's: check it by hand")
        restored.append(entry["path"])
    print(json.dumps({"candidate": a.id, "restored": restored,
                      "already_original": [e["path"] for e in doc["files"]
                                           if e["path"] not in restored],
                      "verified": True}, indent=2))
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
                           ("revert", "restore the snapshot and verify it")):
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
    a = ap.parse_args()
    return {"begin": cmd_begin, "diff": cmd_diff,
            "revert": cmd_revert}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
