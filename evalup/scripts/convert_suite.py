#!/usr/bin/env python3
"""Convert a state directory's YAML into the JSON validate_cases.py and
run_cases.py read -- mechanically, so the model never transcribes a suite.

Those two scripts take JSON on purpose, and that stays true: their docstrings
refuse to carry a partial YAML parser, because one MISPARSES the constructs it
does not implement instead of failing on them, and they conclude that "the
calling skill converts YAML -> JSON with whatever parser it already uses". That
reasoning is sound for a caller that HAS a parser. The caller here is a language
model, and telling it to convert "with your own parser" does not remove the
silent misparse the docstrings refuse; it moves it into an LLM re-typing ~1,900
lines of JSON for a 30-case suite (~6,300 at the 100 cases `optimize`
requires), twice per run. And because the linter and the runner both read that
one transcription, a drifted `scalar: 412.5` is undetectable by construction:
validate_cases.py lints meaning, not fidelity to a file it never sees. That is
the category run_cases.py's own header names: LLM-orchestrated work is not
reproducible, is expensive, and silently drops required outputs.

THE ONE SCRIPT HERE THAT IMPORTS OUTSIDE THE STDLIB, and only this one may.
PyYAML is a real, complete parser, which is the docstrings' own requirement;
it is imported lazily, and its absence is an exit-2 message naming the fix,
not a traceback. Nothing at RUN time depends on it: the runner and every scorer
still read JSON and still import nothing. A machine without PyYAML loses this
convenience and nothing else -- and with `uv` on PATH not even that: the script
re-runs itself under `uv run --no-project --with pyyaml` on the same
interpreter (same arguments, stdin, stdout, exit code), which covers every
caller, `bin/evalup`, `run_cases.py --probe` and `judge_run.py plan` alike.
`--self-check` then adds "via": "uv". EVALUP_NO_UV=1 turns that off.

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
`profile` holds profile.yaml's schema_version, stage, judge, business_rules,
risks and app_model -- enough to pass as validate_cases.py --profile on its
own, and make_plan.py's source for the manifest's app.model.
`sources` maps each case id to the file it came from, so a finding about
c-3f9a2c1d names a file to open. With --split-dir <dir> it ALSO writes the
files the other CLIs take as separate flags: suite.json (--cases),
capabilities.json (--capabilities), manifest.json (--manifest), adapter.json
(--adapter) and profile.json (--profile, validate_cases.py's schema_version
check, #38).

`${VAR}` references are left exactly as written: resolving them is the
runner's job (runner-contract.md SS3), and a secret must not reach this file.

Exit 0 converted; 2 bad input ({"error": ...} on stdout, like every script
here). It does NOT lint: run validate_cases.py on the output.

--adapter-file <path> converts ONE file, an adapter.yaml, and nothing else:
no profile.yaml, no datasets. It exists for `run_cases.py --probe`, which
discover runs before any case exists; the runner hands its adapter.yaml here
so it still parses no YAML itself (runner-contract.md SS1.1). Same strict
loader, same `${VAR}` refs left as written, same {"error": ...}/exit 2.

--self-check reads nothing and converts nothing: it only imports PyYAML, then
prints {"ok": true, "pyyaml": <version>, "python": <interpreter>} and exits 0,
or exits 2 with the same install hint a conversion would hit. It exists so
/evalup:start and the README's install check find a missing PyYAML in minute
one instead of at generate's conversion, about fifteen minutes in.

--rubrics-dir <dir> converts the rubric files there (`*.md`, docs/rubric-
format.md): each one's YAML frontmatter, the sha256 of its bytes and its path,
as {"rubrics": [{"file", "sha256", "frontmatter"}]}. A body without a
frontmatter block is exit 2. judge_run.py plan runs this, so the judge pass
parses no YAML either. A state directory with a rubrics/ folder adds the same
list to the combined document as `rubrics`, and --split-dir writes it as
rubrics.json (validate_cases.py --rubrics); without one, both stay absent.

Usage:
  convert_suite.py <state-dir> [-o converted.json] [--split-dir <dir>]
  convert_suite.py --rubrics-dir <state-dir>/rubrics [-o rubrics.json]
  convert_suite.py --adapter-file <state-dir>/adapter.yaml [-o adapter.json]
  convert_suite.py --self-check
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

from _common import add_version_flag, die, load_text, write_output

NOT_A_CASE = "dataset.yaml"
SCORING_FIELDS = ("oos_handling", "record_id_pattern")
# The profile keys the combined document carries under "profile": what
# make_plan.py and validate_cases.py --profile read. business_rules and risks
# are bookkeeping (#25) -- no scorer reads them. app_model is make_plan.py's
# source for the manifest's app.model (#104).
PROFILE_FIELDS = ("schema_version", "stage", "judge", "business_rules",
                  "risks", "app_model")


# Set on the re-run under uv, so a uv that still cannot give us PyYAML is the
# install hint, never a loop. EVALUP_NO_UV=1 turns the fallback off.
VIA_UV_ENV = "EVALUP_PYYAML_VIA_UV"
NO_UV_ENV = "EVALUP_NO_UV"


def install_hint(extra=""):
    return (f"convert_suite.py needs PyYAML and {sys.executable} cannot "
            "import it. Install it for that interpreter: `python3 -m pip "
            "install --user pyyaml`; if pip refuses with "
            "externally-managed-environment (Homebrew Python), make a venv "
            "(`python3 -m venv ~/.venvs/evalup && ~/.venvs/evalup/bin/pip "
            "install pyyaml`) and start Claude Code with its bin/ first on "
            "PATH. Or install uv (https://docs.astral.sh/uv/): with `uv` on "
            "PATH this script runs itself under `uv run --with pyyaml` and "
            "needs no install. Nothing else in evalup needs it -- the runner "
            "and scorers read the JSON this script writes." + extra)


def uv_command(uv, *argv):
    """`uv run` on this same interpreter, plus PyYAML in a throwaway env
    that uv caches. --no-project: never read or touch the app's own
    pyproject.toml or lockfile, since the CWD is the user's app."""
    return [uv, "run", "--no-project", "--quiet", "--python", sys.executable,
            "--with", "pyyaml", "python", *argv]


def rerun_under_uv():
    """No PyYAML here: run this same command again under uv, if uv is on
    PATH, with the same arguments, stdin, stdout and exit code. Returns
    only with the reason it could not; a successful re-run never returns."""
    if os.environ.get(VIA_UV_ENV):
        return " (Already re-run under uv, which could not provide it.)"
    if os.environ.get(NO_UV_ENV):
        return ""
    uv = shutil.which("uv")
    if uv is None:
        return ""
    # Check first, with output captured, so a uv that cannot fetch PyYAML
    # (offline, no cache) is this script's JSON error, not uv's stderr.
    try:
        probe = subprocess.run(uv_command(uv, "-c", "import yaml"),
                               stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f" (uv is on PATH but failed: {exc})"
    if probe.returncode != 0:
        tail = (probe.stderr or probe.stdout).strip().splitlines()
        return (" (uv is on PATH but could not provide PyYAML: "
                f"{tail[-1] if tail else f'exit {probe.returncode}'})")
    cmd = uv_command(uv, os.path.abspath(__file__), *sys.argv[1:])
    env = dict(os.environ, **{VIA_UV_ENV: "1"})
    sys.stdout.flush()
    if os.name == "posix":
        os.execve(uv, cmd, env)
    sys.exit(subprocess.call(cmd, env=env))


def yaml_module():
    try:
        import yaml
    except ImportError:
        die(install_hint(rerun_under_uv()))
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


def rubric_entries(yaml, loader, rubrics_dir):
    """Every rubric file under `rubrics_dir`, sorted by name: its path
    relative to the state dir, the sha256 of its bytes, and its frontmatter.

    The frontmatter is the block between a first line `---` and the next
    line that is exactly `---`; the Markdown body below it is prose and is
    not read."""
    base = os.path.dirname(os.path.abspath(rubrics_dir))
    entries = []
    for name in sorted(os.listdir(rubrics_dir)):
        path = os.path.join(rubrics_dir, name)
        if not name.endswith(".md") or not os.path.isfile(path):
            continue
        try:
            raw = pathlib.Path(path).read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            die(f"bad input: {path}: {exc}")
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            die(f"bad input: {path}: no frontmatter -- a rubric file starts "
                "with a `---` line (docs/rubric-format.md SS'File shape')")
        try:
            end = next(i for i in range(1, len(lines))
                       if lines[i].strip() == "---")
        except StopIteration:
            die(f"bad input: {path}: the frontmatter has no closing `---`")
        try:
            front = yaml.load("\n".join(lines[1:end]), Loader=loader)  # noqa: S506 - a SafeLoader subclass
        except yaml.YAMLError as exc:
            die(f"bad input: {path}: {' '.join(str(exc).split())}")
        if not isinstance(front, dict):
            die(f"bad input: {path}: the frontmatter is not a YAML mapping")
        entries.append({"file": os.path.relpath(path, base),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "frontmatter": front})
    return entries


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
    ap.add_argument("state_dir", nargs="?",
                    help="the state location, e.g. <app>/.evalup")
    ap.add_argument("--self-check", action="store_true",
                    help="only check that PyYAML imports, and exit: 0 with "
                         "its version, 2 with the install hint")
    ap.add_argument("--adapter-file", metavar="PATH",
                    help="convert only this adapter.yaml (no profile, no "
                         "datasets) -- what run_cases.py --probe reads")
    ap.add_argument("--rubrics-dir", metavar="DIR",
                    help="convert only the rubric files in DIR (their "
                         "frontmatter and sha256) -- what judge_run.py plan "
                         "reads")
    ap.add_argument("-o", "--output", help="write the combined document here "
                                           "(default: stdout)")
    ap.add_argument("--split-dir", help="also write suite.json, "
                    "capabilities.json, adapter.json, profile.json and "
                    "manifest.json here, one per flag the other CLIs take")
    a = ap.parse_args()

    if a.self_check:
        yaml = yaml_module()
        doc = {"ok": True, "pyyaml": getattr(yaml, "__version__", "unknown"),
               "python": sys.executable}
        if os.environ.get(VIA_UV_ENV):
            doc["via"] = "uv"
        print(json.dumps(doc))
        return
    if a.adapter_file is not None:
        if a.state_dir is not None or a.split_dir:
            ap.error("--adapter-file converts one file: it takes no state_dir "
                     "and no --split-dir")
        if not os.path.isfile(a.adapter_file):
            die(f"bad input: {a.adapter_file}: not a file")
        yaml = yaml_module()
        dump(load_mapping(yaml, strict_loader(yaml), a.adapter_file),
             a.output)
        return
    if a.rubrics_dir is not None:
        if a.state_dir is not None or a.split_dir or a.adapter_file:
            ap.error("--rubrics-dir converts one directory: it takes no "
                     "state_dir, --adapter-file or --split-dir")
        if not os.path.isdir(a.rubrics_dir):
            die(f"bad input: {a.rubrics_dir}: not a directory")
        yaml = yaml_module()
        dump({"rubrics": rubric_entries(yaml, strict_loader(yaml),
                                        a.rubrics_dir)}, a.output)
        return
    if a.state_dir is None:
        ap.error("the state_dir argument is required (or pass --self-check)")
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
           # make_plan.py copies `judge` into manifest_extra; the judged
           # gate is still DERIVED from the calibration sidecar, never this
           # word. The rest is what validate_cases.py --profile reads, so
           # the combined document alone can lint traceability (#25).
           "profile": {key: profile[key] for key in PROFILE_FIELDS
                       if key in profile},
           "manifest": manifest, "cases": cases, "sources": sources}
    # Only when the folder exists, so a conversion of a state dir with no
    # rubrics is byte-identical to what it was before the judge pass.
    rubrics = rubric_entries(yaml, loader, at("rubrics")) \
        if os.path.isdir(at("rubrics")) else None
    if rubrics is not None:
        doc["rubrics"] = rubrics
    if a.output and os.path.dirname(a.output):
        # Like --split-dir below: a missing parent is created, not an exit 2.
        try:
            os.makedirs(os.path.dirname(a.output), exist_ok=True)
        except OSError as exc:
            die(f"cannot write output: {exc}")
    dump(doc, a.output)
    if a.split_dir:
        os.makedirs(a.split_dir, exist_ok=True)
        parts = {"suite.json": cases, "capabilities.json": matrix,
                 "adapter.json": adapter, "profile.json": profile}
        if manifest is not None:
            parts["manifest.json"] = manifest
        if rubrics is not None:
            parts["rubrics.json"] = {"rubrics": rubrics}
        for name, part in parts.items():
            dump(part, os.path.join(a.split_dir, name))


if __name__ == "__main__":
    main()
