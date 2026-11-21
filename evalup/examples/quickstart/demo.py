#!/usr/bin/env python3
"""Run the quickstart for real, in one command, and show what a run is.

    python3 examples/quickstart/demo.py            # from evalup/
    python3 examples/quickstart/demo.py --break    # ...and see a red suite

Until this file the only way to execute the example was `python3 -m unittest
tests.test_example`, which proves it works and shows a reader a row of dots.
Reproducing one run by hand meant lifting the fixture app out of the test file
(2026-09 audit). This does what `/evalup:run` does, with the same scripts
and nothing else:

  1. start the app under test on a real local port
  2. validate_cases.py on converted.json -- stops on any ERROR, such as a
     gating case covering a business rule no human confirmed (#25)
  3. build plan.json from plan.template.json + converted.json
  4. run_cases.py  -> reports/<run-id>/   (pre-flight, 6 app calls, every scorer)
  5. gate.py       -> the pass/fail line and exit code CI would see
     build_review_viewer.py -> viewer.html, the page a human reviews

The app is imported from tests/test_example.py rather than copied here, so the
example the suite checks and the example you just ran cannot drift apart; why
it lives there is argued at the top of that file. Nothing is written into the
repository: the run goes to a temp directory (--keep prints where, and leaves
it), because a committed run is a stored claim nobody re-checks.

--judge-replies <dir> adds the judge pass after the run (docs/judge-run.md
SS14): judge_run.py plan, then `record` for every reply file in <dir> named
<launch-key>.txt, then merge -- the steps /evalup:run takes after a run, with
committed fixture replies standing in for the judge, so it needs no Claude.
`examples/quickstart/judge-replies/` holds the shipped ones: the case comes
out a PROVISIONAL pass with its quotes, and the gate line above it does not
move.

--serve starts the app and nothing else, on 127.0.0.1:--port (default 8765),
until SIGTERM or Ctrl-C: the app a CI job starts before its eval steps. The
repository's own CI runs the workflow template (ci/github-actions.yml)
against it.

Exit code is gate.py's: 0 on the shipped example, 1 under --break; the
validator's when it finds an error.
"""
import argparse
import datetime
import json
import os
import pathlib
import shutil
import signal
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


def judge_pass(out, replies):
    """plan -> record each fixture reply -> merge, then the report lines."""
    def judge(*argv):
        proc = script("judge_run.py", *argv, capture_output=True)
        if proc.returncode != 0:
            raise SystemExit(f"judge_run.py {argv[0]} exited "
                             f"{proc.returncode}: {proc.stdout}{proc.stderr}")
        return json.loads(proc.stdout)
    planned = judge("plan", str(out), "--rubrics",
                    str(HERE / "converted.json"))
    print("\n" + "\n".join(planned["summary"]))
    inbox = out / "judge" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for launch in planned["pending"]:
        # A self-test's key carries a random label (judge-run.md SS19), so
        # its fixture is named by side, and the reply's case_id is put back
        # to the key this plan drew -- the one thing a real judge copies
        # from its assignment.
        name = (f"selftest-{launch['rubric_id']}-{launch['side']}"
                if launch["kind"] == "self_test" else launch["key"])
        reply = replies / (name + ".txt")
        if reply.is_file():
            text = reply.read_text("utf-8").replace(
                f'"case_id": "{name}"', f'"case_id": "{launch["key"]}"')
            target = inbox / (launch["key"] + ".txt")
            target.write_text(text, encoding="utf-8")
            judge(*launch["record_args"][:-1], str(target))
    judge("merge", str(out))
    judged = json.loads((out / "judge" / "judged.json").read_text("utf-8"))
    print("\nJudged (PROVISIONAL: in no pass rate, gate or optimize "
          "objective)")
    for case in judged["cases"]:
        verdict = case["verdict"] or "-"
        print(f"  {case['case_id']}  {case['state']} {verdict}"
              + (f"  ({case['reason']})" if case["reason"] else ""))
        for crit in case["criteria"]:
            quote = crit["evidence"][0]["quote"] if crit["evidence"] else ""
            print(f"    {crit['id']}: {crit['verdict']}  \"{quote}\"")


def serve(port):
    """Run the app in the foreground until SIGTERM or Ctrl-C."""
    app = HelpdeskApp(port)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"helpdesk-demo serving on {app.base_url}", flush=True)
    try:
        while app.thread.is_alive():
            app.thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--break", dest="broken", action="store_true",
                    help="make one case's expectation wrong first, to see what "
                         "a failing run and a closed gate look like")
    ap.add_argument("--judge-replies", metavar="DIR",
                    help="after the run, feed DIR's fixture judge replies "
                         "through judge_run.py plan/record/merge")
    ap.add_argument("--keep", action="store_true",
                    help="leave the run directory in place and print its path")
    ap.add_argument("--serve", action="store_true",
                    help="only start the app, on 127.0.0.1:--port, until "
                         "SIGTERM or Ctrl-C (what a CI job runs its suite "
                         "against)")
    ap.add_argument("--port", type=int, default=8765,
                    help="with --serve (default 8765; 0 picks a free port)")
    a = ap.parse_args()
    if a.serve:
        return serve(a.port)
    # gate.py prints straight to the terminal; unbuffered, so its lines land
    # after ours rather than before them when stdout is a pipe.
    sys.stdout.reconfigure(line_buffering=True)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="evalup-quickstart-"))
    state = tmp / ".evalup"
    (state / "reports").mkdir(parents=True)
    app = HelpdeskApp()
    try:
        # The combined document serves every flag (#25). Not --strict: a
        # WARN is the author's to read, an ERROR makes the run untrue.
        combined = str(HERE / "converted.json")
        lint = script("validate_cases.py", "--cases", combined,
                      "--capabilities", combined, "--profile", combined,
                      "--rubrics", combined, capture_output=True)
        if lint.returncode != 0:
            print(f"validate_cases.py exited {lint.returncode}:\n"
                  f"{lint.stdout}{lint.stderr}")
            return lint.returncode
        report = json.loads(lint.stdout)
        print(f"validate_cases.py: {report['error_count']} errors, "
              f"{report['warn_count']} warnings; gaps: "
              + ("; ".join(report["summary"]["traceability"]["gaps"])
                 or "none"))
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
        if a.judge_replies:
            judge_pass(out, pathlib.Path(a.judge_replies))
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
