#!/usr/bin/env python3
"""Apply a recorded pre-look fix to a sealed holdout, and list the seal (#99).

A sealed holdout is written once (generate SS4) and reading a case unseals
it (`analyze --unseal`, then a reseal). That leaves no way to fix a check
defect the training review found, when the same defect sits in a sealed case
from a sibling template. A pre-look fix is that way, and it is narrow:

  1. Only while the seal's look ledger (datasets/holdout-looks.jsonl, the
     run plan's `paths.holdout_ledger`) has ZERO lines. Every `run --holdout`
     appends one -- the noise pair's two runs included -- and so does an
     `analyze --unseal`, so one line means someone has seen holdout verdicts.
     A reseal archives the ledger and empties it, which is what "this seal"
     means.
  2. The fix is a GENERIC TRANSFORMATION, written by a human without holdout
     content, and already applied to at least one accepted training case
     (--motivated-by). This script applies it to every case it matches, in
     every split; nobody chooses which sealed cases get it.
  3. It changes `expect.*` only, never `input` or anything else.
  4. It prints case ids and hashes only. Never case text, and never a
     convert_suite.py error, which can quote a case file's line.
  5. datasets/dataset.yaml records each changed case under `prelook_fixes:`
     (case id, old and new hash, date, the approving human, the
     transformation, the motivating training case, the new dataset_version),
     and `dataset_version` goes up by one.

THE HASH is the one a run records: sha256 of the case's canonical JSON
(run_cases.canonical_json, i.e. manifest.yaml's cases[].sha256) as
convert_suite.py reads it. Templates are hashed the same way.

THE TRANSFORMATION is a JSON object (a file path, or the object inline):
  {"statement": "<what is wrong and why, in words that name no holdout case>",
   "op": "replace_item" | "remove_item" | "set" | "delete",
   "path": "expect.<key>[.<key>...]"   (or a list of keys),
   ... the op's operands}
  replace_item  {"from": X, "to": Y}  every item == X in the list at path -> Y
  remove_item   {"value": X}          every item == X leaves the list at path
  set           {"from": X, "to": Y}  the value at path, when == X, becomes Y
  delete        {"from": X}           the key at path, when its value == X, goes
A case matches when the op would change it. The motivating case must be a
non-holdout case whose review.status is `accepted`, which the op leaves
unchanged and which already shows the fixed form on the same shape (the list
holds `to` and not `from`; a non-empty list lacks `value`; the key holds `to`;
the key is absent from a non-empty mapping). The post-state is all a script
can see: that the training case was fixed by hand in its review is the
approver's word, recorded with their name.

Looks are read from the default ledger, any --holdout-ledger beside it, and
reports/*/manifest.yaml: a holdout run at the current dataset_version is a
look even when no ledger line survives.

Case files are YAML, and this script is stdlib-only, so it replaces only a
changed case file's top-level `expect:` block, as one JSON flow mapping
(which is YAML; the block's comments go), and edits dataset.yaml as text. A
diff of a sealed file therefore shows the check, never the input. It then
re-reads everything through convert_suite.py and restores every file it wrote
unless each hash is the one it computed.

Usage:
  prelook_fix.py <state-dir> --list
  prelook_fix.py <state-dir> --transformation <fix.json> --motivated-by <id>
                 [--apply --approved-by <name>] [--holdout-ledger <path>]
Without --apply it is a dry run: what would change, nothing written. After an
apply, convert and validate (run/SKILL.md SS1), then re-pin: the baseline is
on the old dataset_version.

--list also names `missing_templates`: a template_id a sealed case carries
that no listed template file has (#133). Its oracle and tuple are then
outside the seal, wherever the file is; --list prints the listing with an
`error` and exits 2.

Exit 0 done (or nothing to do on --list); 2 refused or bad input, with
{"error": ...} on stdout and nothing written.
"""
import argparse
import copy
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from _common import BadJSON, add_version_flag, die, load_text, loads_strict
from run_cases import atomic_write, sha256_of

DEFAULT_LEDGER = "datasets/holdout-looks.jsonl"
MANIFEST = os.path.join("datasets", "dataset.yaml")
OPERANDS = {"replace_item": ("from", "to"), "remove_item": ("value",),
            "set": ("from", "to"), "delete": ("from",)}
SEALED_TEMPLATE_PREFIX = "ho_"


# -- reading the suite, through convert_suite.py only ------------------------

def convert(state_dir, *flags):
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "convert_suite.py")
    proc = subprocess.run([sys.executable, script, state_dir, *flags],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        # Withheld on purpose: a YAML error quotes the offending line, and
        # that line can be a sealed case's.
        die(f"convert_suite.py refused {state_dir} (exit {proc.returncode}); "
            "its message is withheld because it can quote a sealed case "
            "file or template. Fix the suite first: `evalup convert-suite` "
            "on it (with --templates for a template) shows why -- read it "
            "yourself; never paste it into a review of a sealed case")
    try:
        return loads_strict(proc.stdout)
    except BadJSON:
        die("convert_suite.py wrote no JSON")


def suite_cases(doc):
    cases = doc.get("cases") or []
    ids = [c.get("id") if isinstance(c, dict) else None for c in cases]
    if not all(isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        die("refused: a case has no id, or two share one; validate the suite "
            "first (run/SKILL.md SS1)")
    return cases


def is_holdout(case):
    split = case.get("split")
    return split == "holdout" or (isinstance(split, list)
                                  and "holdout" in split)


def template_ids(entries):
    """[{template_id, sha256}] from convert_suite.py --templates."""
    return [{"template_id": e["template_id"], "sha256": e["sha256"]}
            for e in entries]


def seal_listing(cases, templates):
    """What the seal covers, by id and hash: never a field's value."""
    sealed = sorted(c["id"] for c in cases if is_holdout(c))
    by_id = {c["id"]: c for c in cases}
    sealed_tids = {by_id[i].get("template_id") for i in sealed}
    training_tids = {c.get("template_id") for c in cases if not is_holdout(c)}
    sealed_templates = [t for t in template_ids(templates)
                        if t["template_id"].startswith(SEALED_TEMPLATE_PREFIX)
                        or t["template_id"] in sealed_tids]
    return {
        "sealed_cases": [{"id": i, "sha256": sha256_of(by_id[i])}
                         for i in sealed],
        "sealed_templates": sealed_templates,
        # A template that realizes a sealed case and a training one: the
        # training case's review shows what the sealed one was built from.
        "mixed_templates": sorted(
            t["template_id"] for t in sealed_templates
            if t["template_id"] in training_tids),
        # A sealed case's template that no listed file holds (#133): the
        # file sits outside the seal, or is gone. A one-off's null is none.
        "missing_templates": sorted(
            t for t in sealed_tids if isinstance(t, str) and t.strip()
            and t not in {e["template_id"] for e in templates}),
    }


def ledger_lines(state_dir, ledgers):
    """(paths, looks) over the default ledger and any --holdout-ledger: an
    override can only add a file to read, never hide the default one."""
    paths, looks = [], 0
    for ledger in ledgers:
        path = ledger if os.path.isabs(ledger) \
            else os.path.join(state_dir, ledger)
        if not path.endswith(".jsonl"):
            die(f"--holdout-ledger must be a .jsonl file (got {ledger!r})")
        if path in paths:
            continue
        paths.append(path)
        if os.path.exists(path):
            # Every non-blank line is a look, malformed or not: the runner
            # counts lines the same way, and a line nobody can parse is
            # still a look.
            looks += sum(1 for line in load_text(path).splitlines()
                         if line.strip())
    return paths, looks


def holdout_runs(state_dir, dataset_version):
    """Run ids under reports/ that touched the seal at this dataset_version.

    A second witness beside the ledger: a holdout run's manifest records the
    dataset_version, and a reseal bumps it, so a holdout run at the current
    version is a look at this seal even if a ledger was lost or moved. A
    manifest that cannot be read is counted, not skipped."""
    reports = os.path.join(state_dir, "reports")
    found = []
    if not os.path.isdir(reports):
        return found
    for name in sorted(os.listdir(reports)):
        path = os.path.join(reports, name, "manifest.yaml")
        if not os.path.isfile(path):
            continue
        try:
            manifest = loads_strict(load_text(path))
        except BadJSON:
            found.append(name)
            continue
        sealed = isinstance(manifest, dict) and "holdout" in (
            manifest.get("mode"), manifest.get("selecting_split"))
        if not isinstance(manifest, dict) or (sealed and manifest.get(
                "dataset_version") in (dataset_version, None)):
            found.append(name)
    return found


# -- the transformation ---------------------------------------------------------

def load_transformation(source):
    text = source if source.lstrip().startswith("{") else load_text(source)
    try:
        spec = loads_strict(text)
    except BadJSON as exc:
        die(f"bad input: --transformation: {exc}")
    if not isinstance(spec, dict):
        die("bad input: --transformation must be a JSON object")
    op = spec.get("op")
    if op not in OPERANDS:
        die(f"bad input: --transformation op {op!r} is not one of "
            + ", ".join(OPERANDS))
    allowed = {"statement", "op", "path", *OPERANDS[op]}
    extra = sorted(set(spec) - allowed)
    if extra:
        die(f"bad input: --transformation: {op} takes no {', '.join(extra)}")
    missing = [k for k in OPERANDS[op] if k not in spec]
    if missing:
        die(f"bad input: --transformation: {op} needs {', '.join(missing)}")
    statement = spec.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        die("bad input: --transformation needs a `statement`: what is wrong "
            "and why, stated without holdout content")
    path = spec.get("path")
    keys = path.split(".") if isinstance(path, str) else path
    if not isinstance(keys, list) or not keys \
            or not all(isinstance(k, str) and k for k in keys):
        die("bad input: --transformation path must be a dotted string or a "
            "list of keys")
    if keys[0] != "expect" or len(keys) < 2:
        die(f"refused: path {path!r} is outside expect.*: a pre-look fix "
            "changes a sealed case's checks, never its input or anything "
            "else")
    if op in ("replace_item", "set") and spec["from"] == spec["to"]:
        die("bad input: --transformation: `from` and `to` are equal")
    return spec, keys


def parent_of(case, keys):
    node = case
    for key in keys[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def apply(spec, keys, case):
    """A transformed deep copy of `case` (equal to it when the op does not
    match)."""
    out = copy.deepcopy(case)
    parent = parent_of(out, keys)
    if parent is None or keys[-1] not in parent:
        return out
    key, op, value = keys[-1], spec["op"], parent[keys[-1]]
    if op == "replace_item" and isinstance(value, list):
        parent[key] = [spec["to"] if item == spec["from"] else item
                       for item in value]
    elif op == "remove_item" and isinstance(value, list):
        parent[key] = [item for item in value if item != spec["value"]]
    elif op == "set" and value == spec["from"]:
        parent[key] = spec["to"]
    elif op == "delete" and value == spec["from"]:
        del parent[key]
    return out


def shows_fixed_form(spec, keys, case):
    """The motivating case shows the fixed form AND the shape the fix acts
    on: a non-empty list where an item was replaced or removed, the key
    itself where a value was set, the enclosing mapping where a key was
    deleted. That is as far as a post-state can go; that the training case
    was edited by hand in its review is the approver's word, recorded."""
    parent = parent_of(case, keys)
    if parent is None:
        return False
    op, present = spec["op"], keys[-1] in parent
    value = parent.get(keys[-1])
    if op == "replace_item":
        return isinstance(value, list) and spec["to"] in value \
            and spec["from"] not in value
    if op == "remove_item":
        return isinstance(value, list) and bool(value) \
            and spec["value"] not in value
    if op == "set":
        return present and value == spec["to"]
    return not present and bool(parent)


def check_motivation(cases, case_id, spec, keys):
    case = next((c for c in cases if c.get("id") == case_id), None)
    if case is None:
        die(f"refused: --motivated-by {case_id!r} is not a case in the suite")
    if is_holdout(case):
        die(f"refused: --motivated-by {case_id!r} is sealed; the fix must "
            "come from a training case")
    review = case.get("review")
    if not (isinstance(review, dict) and review.get("status") == "accepted"):
        die(f"refused: --motivated-by {case_id!r} is not accepted: the fix "
            "must already be on a case a human reviewed")
    if apply(spec, keys, case) != case or not shows_fixed_form(spec, keys,
                                                               case):
        die(f"refused: {case_id!r} does not already carry this fix: apply "
            "it to the training case by hand in its review, then re-run")


# -- writing -------------------------------------------------------------------

def bump_manifest_text(text, old_version, entries):
    """dataset.yaml with dataset_version + 1 and `entries` appended under
    `prelook_fixes:`, edited as text (no YAML writer here). Anything this
    cannot place with certainty is a refusal; convert_suite.py re-reads the
    result either way."""
    new = old_version + 1
    line = re.compile(r"(?m)^dataset_version:[ \t]*(\d+)([ \t]*(#.*)?)$")
    found = line.findall(text)
    if len(found) == 1:
        text = line.sub(lambda m: f"dataset_version: {new}{m.group(2)}", text)
    elif not found and not re.search(r"(?m)^(dataset_)?version:", text):
        text = f"dataset_version: {new}\n" + text
    else:
        die("refused: datasets/dataset.yaml's dataset_version is not one "
            "plain `dataset_version: <int>` line; set it so, then re-run")
    items = "".join(
        "  - " + json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n"
        for e in entries)
    if not text.endswith("\n"):
        text += "\n"
    head = re.compile(r"(?m)^prelook_fixes:[ \t]*(\[\])?[ \t]*$")
    heads = list(head.finditer(text))
    if not heads:
        return text + "prelook_fixes:\n" + items
    if len(heads) > 1:
        die("refused: datasets/dataset.yaml has two prelook_fixes keys")
    match = heads[0]
    after = text[match.end() + 1:]
    # The block runs to the next line that starts at column 0 with a key.
    stop = re.search(r"(?m)^(?=[^\s#-])", after)
    block, rest = (after[:stop.start()], after[stop.start():]) if stop \
        else (after, "")
    if match.group(1) and block.strip():
        die("refused: datasets/dataset.yaml's prelook_fixes is not a list "
            "this script wrote")
    block = block.rstrip("\n") + "\n" if block.strip() else ""
    return text[:match.start()] + "prelook_fixes:\n" + block + items + rest


EXPECT_BLOCK = re.compile(r"(?m)^expect:[^\n]*\n(?:(?:[ \t]+[^\n]*|[ \t]*)\n)*")


def case_file_text(text, fixed):
    """The case file with only its top-level `expect:` block replaced (as
    one JSON flow mapping, which is YAML), so a diff of a sealed file shows
    the check that changed and never its input. None when that block cannot
    be found exactly once: the fix is then refused, never a whole-file
    rewrite."""
    blocks = list(EXPECT_BLOCK.finditer(text if text.endswith("\n")
                                        else text + "\n"))
    if len(blocks) != 1:
        return None
    text = text if text.endswith("\n") else text + "\n"
    m = blocks[0]
    line = "expect: " + json.dumps(fixed["expect"], ensure_ascii=False) + "\n"
    # Blank lines the block swallowed at its end stay where they were.
    tail = m.group(0)[len(m.group(0).rstrip("\n")) + 1:]
    return text[:m.start()] + line + tail + text[m.end():]


def write_all(state_dir, writes):
    """Write every (path, text); return the old text (None = new file).
    A write that fails restores what was written before it and exits 2:
    a sealed case must never be left changed without its record."""
    saved = {}
    for path, text in writes:
        full = os.path.join(state_dir, path)
        old = load_text(full) if os.path.exists(full) else None
        try:
            atomic_write(full, text)
        except OSError as exc:
            with_tmp = f"{full}.tmp.{os.getpid()}"
            if os.path.exists(with_tmp):
                os.remove(with_tmp)
            problems = restore(saved)
            die(f"refused: cannot write {path} ({exc.strerror or exc}); "
                + ("every file was restored" if not problems else
                   "COULD NOT RESTORE " + ", ".join(problems)
                   + " -- restore them from git before anything else"))
        saved[full] = old
    return saved


def restore(saved):
    """Put back every saved file; the paths that could not be restored."""
    problems = []
    for full, text in saved.items():
        try:
            if text is None:
                if os.path.exists(full):
                    os.remove(full)
            else:
                atomic_write(full, text)
        except OSError:
            problems.append(full)
    return problems


def main():
    ap = argparse.ArgumentParser(
        description="Apply a recorded pre-look fix to a sealed holdout "
                    "(expect.* only, before its first look), or --list the "
                    "seal by id and hash. Prints ids and hashes, never case "
                    "text.")
    add_version_flag(ap)
    ap.add_argument("state_dir", help="the state location, e.g. <app>/.evalup")
    ap.add_argument("--list", action="store_true",
                    help="list the sealed cases and templates by id and "
                         "sha256, and the ledger's line count; change nothing")
    ap.add_argument("--transformation", metavar="FILE|JSON",
                    help="the fix: a JSON object (see the module docstring)")
    ap.add_argument("--motivated-by", metavar="CASE_ID",
                    help="the accepted training case that already carries "
                         "the fix")
    ap.add_argument("--apply", action="store_true",
                    help="write the fix (default: a dry run)")
    ap.add_argument("--approved-by", metavar="NAME",
                    help="the human who approved the fix (required with "
                         "--apply)")
    ap.add_argument("--holdout-ledger", default=DEFAULT_LEDGER,
                    help="the run plan's paths.holdout_ledger, relative to "
                         f"state_dir; read in addition to {DEFAULT_LEDGER}")
    a = ap.parse_args()

    if not os.path.isdir(a.state_dir):
        die(f"bad input: {a.state_dir}: not a directory")
    ledger_paths, looks = ledger_lines(a.state_dir,
                                       [DEFAULT_LEDGER, a.holdout_ledger])
    if a.list:
        if a.transformation or a.apply:
            ap.error("--list changes nothing: it takes no --transformation "
                     "or --apply")
        doc = convert(a.state_dir)
        listing = seal_listing(suite_cases(doc),
                               convert(a.state_dir, "--templates")["templates"])
        version = (doc.get("manifest") or {}).get("dataset_version", 1)
        out = {"ledger_lines": looks,
               "holdout_runs": holdout_runs(a.state_dir, version), **listing}
        if listing["missing_templates"]:
            out["error"] = (
                f"{len(listing['missing_templates'])} sealed case "
                "template(s) have no listed template file (see "
                "missing_templates): wherever that file is, its oracle and "
                "tuples are outside the seal. Move it to "
                "datasets/templates/<template_id>.yaml without opening it")
        print(json.dumps(out, indent=2))
        if "error" in out:
            sys.exit(2)
        return
    if not a.transformation or not a.motivated_by:
        ap.error("--transformation and --motivated-by are required (or "
                 "--list)")
    if a.apply and not (a.approved_by or "").strip():
        ap.error("--apply needs --approved-by: the human who approved the fix")
    spec, keys = load_transformation(a.transformation)
    if looks:
        die(f"refused: the holdout ledger ({', '.join(ledger_paths)}) has "
            f"{looks} line(s): this seal has been looked at (a noise-pair "
            "run counts), so a pre-look fix is no longer possible. The path "
            "is `analyze --unseal` and a reseal")

    doc = convert(a.state_dir)
    cases, sources = suite_cases(doc), doc.get("sources") or {}
    manifest = doc.get("manifest") or {}
    old_version = manifest.get("dataset_version", 1)
    if not isinstance(old_version, int) or isinstance(old_version, bool):
        die("refused: datasets/dataset.yaml's dataset_version is not an int")
    runs = holdout_runs(a.state_dir, old_version)
    if runs:
        die(f"refused: reports/ holds {len(runs)} holdout run(s) at "
            f"dataset_version {old_version} ({', '.join(runs)}): this seal "
            "has been looked at even though the ledger is empty. The path "
            "is `analyze --unseal` and a reseal")
    check_motivation(cases, a.motivated_by, spec, keys)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    changed = []
    for case in cases:
        fixed = apply(spec, keys, case)
        if fixed == case:
            continue
        # By construction only expect.* moved; checked anyway, since this is
        # the promise the whole path rests on.
        if {k: v for k, v in fixed.items() if k != "expect"} != \
                {k: v for k, v in case.items() if k != "expect"}:
            die(f"internal: the fix touched {case.get('id')!r} outside expect")
        if case.get("id") not in sources:
            die(f"refused: case {case.get('id')!r} has no source file")
        changed.append((case, fixed))
    listing = seal_listing(cases, convert(a.state_dir, "--templates")
                           ["templates"])
    rows = [{"id": c["id"], "sealed": is_holdout(c),
             "old_sha256": sha256_of(c), "new_sha256": sha256_of(f)}
            for c, f in changed]
    out = {"applied": False, "ledger_lines": 0,
           "transformation": spec, "motivated_by": a.motivated_by,
           "changed": rows,
           "sealed_changed": sum(r["sealed"] for r in rows),
           "sealed_templates": listing["sealed_templates"],
           "mixed_templates": listing["mixed_templates"],
           "dataset_version": {"from": old_version,
                               "to": old_version + 1 if rows else old_version}}
    if not rows:
        out["note"] = "no case matches: nothing to change"
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    if not a.apply:
        out["note"] = "dry run: pass --apply --approved-by <name> to write"
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    entries = [{"case_id": r["id"], "old_sha256": r["old_sha256"],
                "new_sha256": r["new_sha256"], "date": date,
                "approved_by": a.approved_by.strip(),
                "transformation": spec, "motivated_by": a.motivated_by,
                "dataset_version": old_version + 1} for r in rows]
    manifest_full = os.path.join(a.state_dir, MANIFEST)
    manifest_text = load_text(manifest_full) \
        if os.path.exists(manifest_full) else "schema_version: 1\n"
    writes = []
    for c, f in changed:
        text = case_file_text(load_text(os.path.join(a.state_dir,
                                                     sources[c["id"]])), f)
        if text is None:
            die(f"refused: case {c['id']!r}'s file has no single top-level "
                "`expect:` block to replace; nothing was written")
        writes.append((sources[c["id"]], text))
    writes.append((MANIFEST, bump_manifest_text(manifest_text, old_version,
                                                entries)))
    before = {c["id"]: sha256_of(c) for c in cases}
    expected = dict(before, **{r["id"]: r["new_sha256"] for r in rows})
    prior = manifest.get("prelook_fixes") or []
    saved = write_all(a.state_dir, writes)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "convert_suite.py")
    proc = subprocess.run([sys.executable, script, a.state_dir],
                          capture_output=True, text=True, check=False)
    problem = None
    if proc.returncode != 0:
        problem = "convert_suite.py refused the rewritten files"
    else:
        after = loads_strict(proc.stdout)
        got = {c.get("id"): sha256_of(c) for c in after["cases"]}
        meta = after.get("manifest") or {}
        if got != expected:
            problem = "a re-read case hash differs from the computed one"
        elif meta.get("dataset_version") != old_version + 1:
            problem = "dataset.yaml's dataset_version did not re-read"
        elif meta.get("prelook_fixes") != [*prior, *entries]:
            problem = "dataset.yaml's prelook_fixes did not re-read"
    if problem:
        failed = restore(saved)
        die(f"refused: {problem}; " + ("every file was restored" if not failed
            else "COULD NOT RESTORE " + ", ".join(failed)))
    out.update(applied=True, approved_by=a.approved_by.strip(), date=date,
               next="convert and validate (run/SKILL.md SS1), then re-pin: "
                    "the baseline is on the old dataset_version")
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
