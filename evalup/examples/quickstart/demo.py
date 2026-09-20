#!/usr/bin/env python3
"""Run the quickstart for real, in one command, and show what a run is.

    python3 examples/quickstart/demo.py            # from evalup/
    python3 examples/quickstart/demo.py --break    # ...and see a red suite

Until this file the only way to execute the example was `python3 -m unittest
tests.test_example`, which proves it works and shows a reader a row of dots.
Reproducing one run by hand meant lifting the fixture app out of the test file
(2026-09 audit). This does the four things `/evalup:run` does, with the same
scripts and nothing else:

  1. start the app under test on a real local port
  2. build plan.json from plan.template.json + converted.json
  3. run_cases.py  -> reports/<run-id>/   (pre-flight, 6 app calls, every scorer)
  4. gate.py       -> the pass/fail line and exit code CI would see
     build_review_viewer.py -> viewer.html, the page a human reviews

The app is imported from tests/test_example.py rather than copied here, so the
example the suite checks and the example you just ran cannot drift apart; why
it lives there is argued at the top of that file. Nothing is written into the
repository: the run goes to a temp directory (--keep prints where, and leaves
it), because a committed run is a stored claim nobody re-checks.

Exit code is gate.py's: 0 on the shipped example, 1 under --break.
"""
import argparse
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT / "tests"))

from test_example import HelpdeskApp, strip_comments  # noqa: E402


def script(name, *argv, **kwargs):
    return subprocess.run([sys.executable, str(SCRIPTS / name), *argv],
                          text=True, **kwargs)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--break", dest="broken", action="store_true",
                    help="make one case's expectation wrong first, to see what "
                         "a failing run and a closed gate look like")
    ap.add_argument("--keep", action="store_true",
                    help="leave the run directory in place and print its path")
    a = ap.parse_args()
    # gate.py prints straight to the terminal; unbuffered, so its lines land
    # after ours rather than before them when stdout is a pipe.
    sys.stdout.reconfigure(line_buffering=True)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="evalup-quickstart-"))
    state = tmp / ".evalup"
    (state / "reports").mkdir(parents=True)
    app = HelpdeskApp()
    try:
        converted = json.loads((HERE / "converted.json").read_text("utf-8"))
        cases = converted["cases"]
        if a.broken:
            cases[0]["expect"]["answer"] = {"must_contain": ["$999.99"]}

        stamp = datetime.datetime.now(datetime.timezone.utc)
        run_id = "smoke-" + stamp.strftime("%Y%m%dT%H%M%SZ")
        plan = strip_comments(json.loads(
            (HERE / "plan.template.json").read_text("utf-8")))
        adapter = converted["adapter"]
        adapter["invocation"]["base_url"] = app.base_url
        plan.update(run_id=run_id, adapter=adapter, cases=cases,
                    capability_matrix=converted["capability_matrix"])
        plan["paths"].update(scripts_dir=str(SCRIPTS), state_dir=str(state))
        plan_path = tmp / "plan.json"
        plan_path.write_text(json.dumps(plan, indent=2), "utf-8")

        out = state / "reports" / run_id
        print(f"app under test: {app.base_url}  (2 domains, rule-based)")
        print(f"running {len(cases)} cases -> {out}\n")
        ran = script("run_cases.py", "--plan", str(plan_path), "--out",
                     str(out), capture_output=True,
                     env=dict(os.environ, HELPDESK_BASE_URL=app.base_url))
        if ran.returncode != 0:
            print(f"run_cases.py exited {ran.returncode}:\n{ran.stdout}"
                  f"{ran.stderr}")
            return ran.returncode

        results = json.loads((out / "results.json").read_text("utf-8"))
        for row in results["cases"]:
            failed = sorted(name for name, verdict in row["layers"].items()
                            if verdict == "fail")
            note = f"   <- failed: {', '.join(failed)}" if failed else ""
            print(f"  {row['case_id']}  {row['verdict']:<8}{note}")
        routing = json.loads((out / "routing_report.json").read_text("utf-8"))
        print(f"\nrouting: accuracy {routing.get('accuracy')}, "
              f"macro-F1 {routing.get('macro_f1')}\n")

        gate = script("gate.py", str(out))
        viewer = out / "viewer.html"
        script("build_review_viewer.py", str(out), "-o", str(viewer),
               capture_output=True)
        if a.keep:
            print(f"\nrun directory: {out}\nviewer:        {viewer}")
        else:
            print("\n(re-run with --keep to keep the run directory and open "
                  "viewer.html)")
        return gate.returncode
    finally:
        app.close()
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
