#!/usr/bin/env python3
"""Run the CI workflow template's shell steps, verbatim, against the quickstart.

#40's acceptance criterion is that the template users copy
(ci/github-actions.yml) runs green in this repository's CI against
examples/quickstart. A hand-kept copy of its steps in ci.yml would pass while
the shipped file rotted, and a reusable workflow (`on: workflow_call`) cannot
take the step a real app needs most -- its own toolchain and start command --
from the caller. So the template stays a plain file, and this reads it and
runs every `run:` step as Actions would: `bash --noprofile --norc -eo
pipefail`, the workflow env then the job env, `$GITHUB_ENV` carried from step
to step, stopping at the first step that fails.

What it overrides, and nothing else:
  - the env values a real app's copy edits: the plugin URL and ref (the
    checkout under test, at HEAD, fetched exactly as the template fetches a
    pin), the health URL, and the adapter's ${HELPDESK_BASE_URL};
  - the step with id `start-app`, which starts examples/quickstart/demo.py
    --serve from the fetched plugin instead of `your_app`.
`uses:` steps are the job's to provide: ci.yml's `ci-template` job checks out
and sets up the same Python itself (tests/test_ci_template.py pins the two
jobs' actions to the same SHAs), and the app checkout is the temp repository
built below. The loader refuses what it cannot run faithfully: a `run:` step
holding `${{ }}`, an `if:` on one, a shell other than bash.

The scenario, three passes, each on a fresh RUNNER_TEMP:
  1. no baseline yet: the regression run gates absolutely, and opens;
  2. that run pinned (pin_baseline.py) and committed, the app repository
     cloned fresh -- so only committed files reach the run -- and gated
     --baseline: opens, and the gate names the baseline;
  3. one case's expectation broken in that clone: the gate closes on a
     regression and the job fails;
  4. a gating case made to cover the quickstart's inferred rule
     (dispute-window): validate_cases.py refuses it, so the job fails
     before any run and the gate never writes its summary.

Steps call `python`, which setup-python provides; here it is this
interpreter, whose directory leads PATH. The template's own step pip-installs
PyYAML into it, so run this from an interpreter that has pip (a venv, or the
setup-python one in CI), and with PyYAML already present to read the
template. Needs git and curl.

Usage, from evalup/:  python3 tests/run_template.py [--keep]
Exit 0 every pass behaved; 1 one did not; 2 the template could not be loaded.
"""
import argparse
import contextlib
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile

TESTS = pathlib.Path(__file__).resolve().parent
ROOT = TESTS.parent
TEMPLATE = ROOT / "ci" / "github-actions.yml"
QUICKSTART = ROOT / "examples" / "quickstart" / ".evalup"
RUN_KEYS = {"name", "id", "run", "env"}


class TemplateError(Exception):
    pass


def load(text):
    """(env, steps): the workflow env merged under the one job's env, and its
    steps. Refuses what run_steps() could not run as Actions does."""
    import yaml  # the reader's one dependency; the steps install their own

    doc = yaml.safe_load(text)
    shell = ((doc.get("defaults") or {}).get("run") or {}).get("shell")
    if shell != "bash":
        raise TemplateError(f"defaults.run.shell must be bash, got {shell!r}")
    jobs = doc.get("jobs") or {}
    if len(jobs) != 1:
        raise TemplateError(f"expected one job, got {sorted(jobs)}")
    job = next(iter(jobs.values()))
    env = {**(doc.get("env") or {}), **(job.get("env") or {})}
    steps = job.get("steps") or []
    for n, step in enumerate(steps, 1):
        if "uses" in step:
            continue
        if "run" not in step:
            raise TemplateError(f"step {n}: neither uses: nor run:")
        extra = set(step) - RUN_KEYS
        if extra:
            raise TemplateError(f"step {n} ({step.get('name')}): "
                                f"{sorted(extra)} is not run here")
        if "${{" in step["run"]:
            raise TemplateError(f"step {n} ({step.get('name')}): ${{{{ }}}} "
                                "in a run: step; read it from env instead")
    return {k: str(v) for k, v in env.items()}, steps


def read_env_file(path):
    """$GITHUB_ENV's two forms: NAME=value and NAME<<DELIM ... DELIM."""
    out, lines = {}, pathlib.Path(path).read_text("utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if "<<" in line and ("=" not in line
                             or line.index("<<") < line.index("=")):
            name, delim = line.split("<<", 1)
            body = []
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            i += 1
            out[name] = "\n".join(body)
        elif "=" in line:
            name, value = line.split("=", 1)
            out[name] = value
    return out


def run_steps(env, steps, workdir, runner_temp, overrides, replace):
    """Run the `run:` steps in order; the first nonzero exit code, or 0.
    Every process a step left running (the app) is stopped at the end."""
    declared = {**env, **overrides}
    for name, value in declared.items():
        if "${{" in value:
            raise TemplateError(f"env {name} needs an override: {value}")
    base = dict(os.environ, **declared, RUNNER_TEMP=str(runner_temp))
    base["PATH"] = os.path.dirname(sys.executable) + os.pathsep + base["PATH"]
    summary = runner_temp / "step-summary.md"
    carried, groups = {}, []
    try:
        for n, step in enumerate(steps, 1):
            label = step.get("name") or step.get("uses")
            if "uses" in step:
                print(f"--- step {n}: {label} (provided by the job)")
                continue
            script = replace.get(step.get("id"), step["run"])
            print(f"--- step {n}: {label}", flush=True)
            env_file = runner_temp / f"env-{n}"
            env_file.write_text("", "utf-8")
            step_file = runner_temp / f"step-{n}.sh"
            step_file.write_text(script, "utf-8")
            proc = subprocess.Popen(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail",
                 str(step_file)],
                cwd=workdir, start_new_session=True,
                env=dict(base, **carried,
                         **{k: str(v) for k, v in
                            (step.get("env") or {}).items()},
                         GITHUB_ENV=str(env_file),
                         GITHUB_STEP_SUMMARY=str(summary)))
            groups.append(proc.pid)
            rc = proc.wait()
            carried.update(read_env_file(env_file))
            if rc != 0:
                print(f"--- step {n} exited {rc}; later steps skipped")
                return rc
        return 0
    finally:
        for pgid in groups:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGTERM)


def git(cwd, *argv):
    subprocess.run(["git", "-c", "user.name=evalup-ci",
                    "-c", "user.email=evalup-ci@localhost", *argv],
                   cwd=cwd, check=True, capture_output=True, text=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--keep", action="store_true",
                    help="leave the temp directory and print its path")
    a = ap.parse_args()
    try:
        env, steps = load(TEMPLATE.read_text("utf-8"))
    except (TemplateError, ImportError) as exc:
        print(f"cannot run {TEMPLATE}: {exc}")
        return 2
    if not any(s.get("id") == "start-app" for s in steps):
        print(f"{TEMPLATE}: no step with id start-app to replace")
        return 2

    repo = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT,
                          check=True, capture_output=True,
                          text=True).stdout.strip()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    overrides = {"EVALUP_REPO_URL": repo, "EVALUP_REF": head,
                 "APP_HEALTH_URL": f"{url}/healthz",
                 "HELPDESK_BASE_URL": url}
    replace = {"start-app": (
        'nohup python "$EVALUP_SCRIPTS/../examples/quickstart/demo.py" '
        f'--serve --port {port} > "$RUNNER_TEMP/app.log" 2>&1 &\n')}
    # Under RUNNER_TEMP in CI, so the job can upload what --keep leaves.
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="evalup-template-",
                                        dir=os.environ.get("RUNNER_TEMP")))
    print(f"plugin under test: {repo} at {head}\nworking in {tmp}")
    failures = []

    def run_pass(n, workdir, want_rc, want_text, unwanted_text=()):
        runner_temp = tmp / f"runner-{n}"
        runner_temp.mkdir()
        print(f"\n=== pass {n}: {workdir.name}, expecting exit {want_rc}")
        rc = run_steps(env, steps, workdir, runner_temp, overrides, replace)
        summary = runner_temp / "step-summary.md"
        text = summary.read_text("utf-8") if summary.is_file() else ""
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a",
                      encoding="utf-8") as out:
                out.write(f"#### template pass {n}\n{text}\n")
        if rc != want_rc:
            failures.append(f"pass {n}: exit {rc}, wanted {want_rc}")
        for want in want_text:
            if want not in text:
                failures.append(f"pass {n}: the gate summary lacks {want!r}")
        for unwanted in unwanted_text:
            if unwanted in text:
                failures.append(f"pass {n}: the summary has {unwanted!r}")

    try:
        dev = tmp / "app-dev"
        shutil.copytree(QUICKSTART, dev / ".evalup")
        git(dev, "init", "-q")
        git(dev, "add", "-A")
        git(dev, "commit", "-q", "-m", "the app, with its evalup state")
        run_pass(1, dev, 0, ["PASS"])
        runs = sorted((dev / ".evalup" / "reports").glob("regression-*"))
        if len(runs) != 1:
            failures.append(f"pass 1 left {len(runs)} run directories")
            return 1
        baseline = runs[0].name
        pin = subprocess.run([sys.executable, str(ROOT / "scripts" /
                                                  "pin_baseline.py"),
                              str(runs[0])], capture_output=True, text=True)
        if pin.returncode != 0:
            failures.append(f"pin_baseline.py exited {pin.returncode}: "
                            f"{pin.stdout}{pin.stderr}")
            return 1
        git(dev, "add", "-A")
        git(dev, "commit", "-q", "-m", f"pin {baseline} as the baseline")

        app = tmp / "app"
        git(tmp, "clone", "-q", str(dev), str(app))
        run_pass(2, app, 0, ["PASS", f"baseline {baseline}: 0 regression(s)"])

        case = app / ".evalup" / "datasets" / "c-4f2a91c7.yaml"
        text = case.read_text("utf-8")
        case.write_text(text.replace('must_contain: ["412.50"]',
                                     'must_contain: ["999.99"]'), "utf-8")
        run_pass(3, app, 1, [f"baseline {baseline}: 1 regression(s)"])

        case = app / ".evalup" / "datasets" / "c-6c1f82ab.yaml"
        text = case.read_text("utf-8")
        covers = "covers: [plain-text, read-only]"
        if covers not in text:
            failures.append(f"pass 4: {case.name} lacks {covers!r}")
            return 1
        case.write_text(text.replace(
            covers, "covers: [plain-text, read-only, dispute-window]"),
            "utf-8")
        run_pass(4, app, 1, [], ["evalup gate"])
    finally:
        if a.keep:
            print(f"\nkept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
        print("\n" + ("\n".join(failures) if failures
                      else "the template ran: no baseline -> open, "
                           "baseline -> open, regression -> closed, "
                           "unconfirmed rule gating -> refused"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
