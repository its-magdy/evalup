#!/usr/bin/env python3
"""Convert a state directory's YAML into the JSON validate_cases.py and
run_cases.py read -- mechanically, so the model never transcribes a suite.

Those two scripts take JSON on purpose, and that stays true: their docstrings
refuse to carry a partial YAML parser, because one MISPARSES the constructs it
does not implement instead of failing on them, and they conclude that "the
calling skill converts YAML -> JSON with whatever parser it already uses". That
reasoning is sound for a caller that HAS a parser. The caller here is a language
model, and run/SKILL.md told it to convert "with your own parser" -- so the
silent misparse the docstring refused was not removed, it was moved into an LLM
re-typing ~1,900 lines of JSON for a 30-case suite (~6,300 at the 100 cases
`optimize` requires), twice per run. And because the linter and the runner both
read that one transcription, a drifted `scalar: 412.5` was undetectable by
construction: validate_cases.py lints meaning, not fidelity to a file it never
sees. run_cases.py's own header names the category -- work that "used to be
hand-orchestrated by the LLM on every run: not reproducible, expensive, and it
silently dropped required outputs" (2026-09 audit).

THE ONE SCRIPT HERE THAT IMPORTS OUTSIDE THE STDLIB, and only this one may.
PyYAML is a real, complete parser, which is the docstrings' own requirement;
it is imported lazily, and its absence is an exit-2 message naming the fix,
not a traceback. Nothing at RUN time depends on it: the runner and every scorer
still read JSON and still import nothing. A machine without PyYAML loses this
convenience and nothing else (`uv run --with pyyaml python convert_suite.py ...`
needs no install at all).

Two things a bare yaml.safe_load gets wrong for this job, both closed here:
  - a DUPLICATE KEY is legal YAML to PyYAML, which keeps the last one silently.
    In a case file that is an expectation that vanished. It is an error here.
  - a date (`as_of: 2026-09-01`) loads as a datetime.date, which json cannot
    write. It is written as its ISO string, which is what the file said.

Reads, under <state-dir>:
  adapter.yaml                      -> adapter
  profile.yaml                      -> capability_matrix (+ the scoring fields
                                       run/SKILL.md SS1 copies into the plan)
  datasets/**/*.yaml                -> cases, one per file, sorted by id.
                                       `dataset.yaml` is the manifest, not a
                                       case; anything under a templates/ dir is
                                       a generation template, not a case.

Writes one JSON document (stdout, or -o <file>):
  {"adapter", "capability_matrix", "scoring", "profile", "manifest", "cases",
   "sources"}
`sources` maps each case id to the file it came from, so a finding about
c-3f9a2c1d names a file to open. With --split-dir <dir> it ALSO writes the
four files the other CLIs take as separate flags: suite.json (--cases),
capabilities.json (--capabilities), manifest.json (--manifest), adapter.json.

`${VAR}` references are left exactly as written: resolving them is the
runner's job (runner-contract.md SS3), and a secret must not reach this file.

Exit 0 converted; 2 bad input ({"error": ...} on stdout, like every script
here). It does NOT lint: run validate_cases.py on the output.

Usage:
  convert_suite.py <state-dir> [-o converted.json] [--split-dir <dir>]
"""
import argparse
import datetime
import json
import os

from _common import add_version_flag, die, load_text, write_output

NOT_A_CASE = "dataset.yaml"
SCORING_FIELDS = ("oos_handling", "record_id_pattern")


def yaml_module():
    try:
        import yaml
    except ImportError:
        die("convert_suite.py needs PyYAML and it is not installed: "
            "`python3 -m pip install pyyaml`, or with no install at all "
            "`uv run --with pyyaml python <this script> ...`. Nothing else in "
            "evalup needs it -- the runner and scorers read the JSON this "
            "script writes.")
    return yaml


def strict_loader(yaml):
    """SafeLoader that refuses a duplicate mapping key instead of keeping the
    last one."""
    class Loader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
                seen.add(key)
            except TypeError:       # an unhashable key; safe_load rejects it next
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}: YAML keeps only the "
                    "last, so the earlier value would vanish silently",
                    key_node.start_mark)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    Loader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    return Loader


def load_yaml(yaml, loader, path):
    try:
        return yaml.load(load_text(path), Loader=loader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        die(f"bad input: {path}: {' '.join(str(exc).split())}")


def load_mapping(yaml, loader, path):
    doc = load_yaml(yaml, loader, path)
    if not isinstance(doc, dict):
        die(f"bad input: {path}: expected a YAML mapping, got "
            f"{type(doc).__name__}")
    return doc


def case_files(datasets_dir):
    found = []
    for root, dirs, files in os.walk(datasets_dir):
        dirs[:] = sorted(d for d in dirs if d != "templates")
        found.extend(os.path.join(root, name) for name in sorted(files)
                     if name.endswith((".yaml", ".yml")) and name != NOT_A_CASE)
    return found


def jsonable(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} has no JSON form")


def dump(doc, path):
    try:
        text = json.dumps(doc, indent=2, ensure_ascii=False, default=jsonable,
                          allow_nan=False)
    except (TypeError, ValueError) as exc:
        die(f"bad input: a value cannot be written as JSON: {exc} (a bare "
            ".nan/.inf has no JSON spelling -- quote it if it is text)")
    write_output(path, text + "\n")


def main():
    ap = argparse.ArgumentParser(
        description="Convert <state-dir>'s adapter.yaml, profile.yaml and "
                    "datasets/**/*.yaml into the JSON validate_cases.py and "
                    "run_cases.py read. Needs PyYAML; nothing else here does.")
    add_version_flag(ap)
    ap.add_argument("state_dir", help="the state location, e.g. <app>/.evalup")
    ap.add_argument("-o", "--output", help="write the combined document here "
                                           "(default: stdout)")
    ap.add_argument("--split-dir", help="also write suite.json, "
                    "capabilities.json, adapter.json and manifest.json here, "
                    "one per flag the other CLIs take")
    a = ap.parse_args()

    if not os.path.isdir(a.state_dir):
        die(f"bad input: {a.state_dir}: not a directory")
    yaml = yaml_module()
    loader = strict_loader(yaml)

    def at(*parts):
        return os.path.join(a.state_dir, *parts)

    for required in ("adapter.yaml", "profile.yaml"):
        if not os.path.isfile(at(required)):
            die(f"bad input: {a.state_dir}: no {required} -- discover writes "
                "it; is this the state location?")
    adapter = load_mapping(yaml, loader, at("adapter.yaml"))
    profile = load_mapping(yaml, loader, at("profile.yaml"))
    matrix = profile.get("capability_matrix")
    if not isinstance(matrix, dict):
        die(f"bad input: {at('profile.yaml')}: no capability_matrix mapping")

    files = case_files(at("datasets")) if os.path.isdir(at("datasets")) else []
    if not files:
        die(f"bad input: {at('datasets')}: no case files (*.yaml) -- generate "
            "writes them")
    cases, sources = [], {}
    for path in files:
        case = load_mapping(yaml, loader, path)
        case_id = case.get("id")
        if isinstance(case_id, str) and case_id in sources:
            die(f"bad input: case id {case_id!r} is in both {sources[case_id]} "
                f"and {os.path.relpath(path, a.state_dir)}")
        if isinstance(case_id, str):
            sources[case_id] = os.path.relpath(path, a.state_dir)
        cases.append(case)
    # By id, not by path: the order is the run's order, and a file moved
    # between directories must not reorder a run. Id-less cases sort last and
    # validate_cases.py reports them.
    cases.sort(key=lambda c: (not isinstance(c.get("id"), str),
                              str(c.get("id"))))

    manifest_path = at("datasets", NOT_A_CASE)
    manifest = load_mapping(yaml, loader, manifest_path) \
        if os.path.isfile(manifest_path) else None

    doc = {"adapter": adapter, "capability_matrix": matrix,
           "scoring": {key: profile[key] for key in SCORING_FIELDS
                       if key in profile},
           # make_plan.py copies this into manifest_extra; the judged gate
           # is still DERIVED from the calibration sidecar, never this word.
           "profile": {key: profile[key] for key in ("stage", "judge")
                       if key in profile},
           "manifest": manifest, "cases": cases, "sources": sources}
    dump(doc, a.output)
    if a.split_dir:
        os.makedirs(a.split_dir, exist_ok=True)
        parts = {"suite.json": cases, "capabilities.json": matrix,
                 "adapter.json": adapter}
        if manifest is not None:
            parts["manifest.json"] = manifest
        for name, part in parts.items():
            dump(part, os.path.join(a.split_dir, name))


if __name__ == "__main__":
    main()
