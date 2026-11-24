#!/usr/bin/env python3
"""Block until a run directory is finalized, in bounded slices a skill can loop.

Why this exists: under `claude -p` a Bash call is capped at ten minutes and a
background shell is killed about five seconds after the turn ends. A session
that launches run_cases.py with `run_in_background` and ends the turn has the
runner killed mid-case by the CLI's exit, leaving results.json at
`summary.status: "running"`. No single wait the platform offers outlasts a
thirty-minute run, so the skill needs a wait it can call repeatedly without
ending the turn. This is that
call: it reads, it never writes, and it exits before the ten-minute cap so it
is never backgrounded itself.

  0  the run is finalized: results.json's summary.status is no longer
     "running" (ok, aborted or incomplete -- the runner's own exit code says
     which; gate.py gives the verdict)
  2  bad input ({"error": ...} on stdout, like every script here)
  3  still running when --timeout-s elapsed; call again. Also a run not
     yet started (`status: "not_started"`): a runner launched with
     `--not-before` writes nothing while it waits, so its directory may be
     absent -- waited on while that runner is alive (#111)
  4  still "running" but no run_cases.py process for this run exists: the
     runner was killed. Resume it with `run_cases.py --resume` under the SAME
     run id rather than starting a fresh run over the cases already paid for.
     The printed `resume` command carries `--not-before` one rate-limit
     window on when the manifest declares a budget (#144), and a resume
     spends no new holdout look. Its `--plan` is `<state>/work/plan.json`
     when that file is still the plan the run started from (its hash is the
     manifest's `plan_sha256`); otherwise it is the placeholder
     `<plan.json>` and `plan_note` says what to put there (#153).

Prints one JSON object on stdout: the run id, the status, the runner's exit
code when finalized, the last run.log event, and whether a runner process was
seen ("alive" / "gone" / "unknown" when `ps` is unavailable).

Usage:
  wait_run.py <reports/<run-id>> [--timeout-s 540] [--interval-s 5]

--timeout-s is at most 600: one Bash call is capped at ten minutes, and a
call past its cap is moved to the background, where this wait no longer
holds the turn open. Wait longer by calling it again on exit 3.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from _common import (
    add_version_flag,
    die,
    load_text,
    require_range,
    resume_command,
    resume_gap_s,
)

STILL_RUNNING = 3
RUNNER_GONE = 4
PLAN_NOTE = ("<plan.json> is a placeholder, not a path: replace it with the "
             "plan file this run was started from. <state>/work/plan.json "
             "is missing or no longer hashes to the manifest's plan_sha256 "
             "(rebuilt since?), and --resume refuses any other plan")


def read_results(run_dir):
    """results.json, or None while it is unreadable. The runner writes it
    atomically, so an unreadable file is absence, not a torn write."""
    path = os.path.join(run_dir, "results.json")
    if not os.path.isfile(path):
        return None
    try:
        doc = json.loads(load_text(path, on_error=lambda _m: None) or "")
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def last_log_event(run_dir):
    path = os.path.join(run_dir, "run.log")
    if not os.path.isfile(path):
        return None
    text = load_text(path, on_error=lambda _m: None) or ""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except ValueError:
            return {"event": "<unparseable line>"}
    return None


def runner_process(run_dir):
    """Best effort: is a run_cases.py writing this run directory still alive?
    `ps` is the one portable-enough probe the stdlib offers; where it is
    missing the answer is "unknown" and the caller keeps waiting."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    me = str(os.getpid())
    target = os.path.abspath(run_dir).rstrip(os.sep)
    run_id = os.path.basename(target)
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[0] == me:
            continue
        # Whole tokens, not substrings: a shell whose command line merely
        # MENTIONS run_cases.py and this run id (a script, a heredoc, a
        # combined Bash call) is not a runner, and since #111 a false
        # "alive" keeps an absent directory waited on.
        tokens = [t.rstrip(os.sep) for t in parts[1].split()]
        # An absolute token must be this directory; a relative one (the
        # runner's own cwd is unknown here) is matched on the run id.
        if any(os.path.basename(t) == "run_cases.py" for t in tokens) and \
                any(t == target if os.path.isabs(t)
                    else os.path.basename(t) == run_id for t in tokens):
            return "alive"
    return "gone"


def plan_of(run_dir, doc):
    """#153: the plan file this run started from, or None. A run's --out is
    `<state>/reports/<run-id>` (runner-contract SS1) and the run skill builds
    its plan at `<state>/work/plan.json`, so that is the one candidate. It
    is named only while its hash is still the manifest's `plan_sha256`: the
    file is rebuilt every run, and a resume with another plan is refused."""
    want = doc.get("plan_sha256")
    if not isinstance(want, str):
        return None
    state = os.path.dirname(os.path.dirname(
        os.path.normpath(run_dir)))
    candidate = os.path.join(state, "work", "plan.json")
    text = load_text(candidate, on_error=lambda _m: None)
    if text is None:
        return None
    try:
        plan = json.loads(text)
    except ValueError:
        return None
    # The runner's own hash, defaults filled in as it does, not a copy.
    from run_cases import plan_sha256_of
    return candidate if plan_sha256_of(plan) == want else None


def resume_of(run_dir, now=None):
    """#144: the command that resumes a killed run, and the --not-before it
    carries. Under a declared rate budget (the manifest records it) that is
    one window after now, rounded up to the second: the runner is gone by
    now, so its last call finished earlier, and a resume started at once
    would share that call's window. The manifest's baseline is carried
    over. Resuming spends no new holdout look (runner-contract SS6).
    Returns (command, not_before, plan): plan None when the command holds
    the `<plan.json>` placeholder (#153)."""
    manifest = load_text(os.path.join(run_dir, "manifest.yaml"),
                         on_error=lambda _m: None) or ""
    try:
        doc = json.loads(manifest)
    except ValueError:
        doc = None
    doc = doc if isinstance(doc, dict) else {}
    plan = plan_of(run_dir, doc)
    invocation = doc.get("invocation")
    gap = resume_gap_s(invocation.get("rate_limit")
                       if isinstance(invocation, dict) else None)
    baseline = doc.get("baseline_verdicts")
    baseline = baseline if isinstance(baseline, str) else None
    when = None
    if gap is not None:
        at = (now or datetime.now(timezone.utc)) + timedelta(seconds=gap)
        if at.microsecond:
            at = at.replace(microsecond=0) + timedelta(seconds=1)
        when = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (resume_command(run_dir, plan, baseline=baseline,
                           not_before=when), when, plan)


def started_run(run_dir):
    """Has run_cases.py written this run directory yet?"""
    return any(os.path.isfile(os.path.join(run_dir, name))
               for name in ("results.json", "manifest.yaml"))


def status_of(results):
    summary = results.get("summary") if results else None
    if isinstance(summary, dict):
        return summary.get("status")
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Wait, in one bounded slice, for run_cases.py to finalize "
                    "a run directory. Exit 0 finalized, 3 still running "
                    "(call again), 4 the runner is gone (--resume it).")
    add_version_flag(ap)
    ap.add_argument("run_dir", help="the run directory (reports/<run-id>)")
    ap.add_argument("--timeout-s", type=float, default=540.0,
                    help="how long to wait before exiting 3, at most 600 "
                         "(default %(default)s: under the ten-minute cap on a "
                         "single Bash call; past it the call is moved to the "
                         "background). To wait longer, call again on exit 3")
    ap.add_argument("--interval-s", type=float, default=5.0,
                    help="seconds between checks (default %(default)s)")
    a = ap.parse_args()
    require_range("--timeout-s", a.timeout_s, lo=0, hi=600,
                  note=" (one Bash call is capped at ten minutes; to "
                       "wait longer, call wait-run again while it exits 3)")
    require_range("--interval-s", a.interval_s, lo=0.1, hi=60)

    run_dir = a.run_dir
    started = time.monotonic()
    # #111: a runner launched with --not-before writes nothing until its time
    # comes, so its directory is absent or empty while it waits. That is a
    # run not yet started, and waited on like a running one -- as long as a
    # runner naming it is alive. With none, it is the bad input it was.
    while not started_run(run_dir):
        process = runner_process(run_dir)
        # Only "gone" is fatal: with `ps` unavailable ("unknown") a runner
        # may still be waiting, and exit 2 would invite a second launch --
        # on a holdout, a second look.
        if process == "gone":
            if not os.path.isdir(run_dir):
                die(f"bad input: {run_dir}: not a directory, and no "
                    "run_cases.py process names it (a --not-before runner "
                    "killed while it waited leaves nothing: relaunch it "
                    "with the SAME --not-before, so it keeps its window)")
            die(f"bad input: {run_dir}: no results.json or manifest.yaml; "
                "this is not a run directory run_cases.py has started")
        if time.monotonic() - started >= a.timeout_s:
            print(json.dumps({
                "run_id": os.path.basename(os.path.abspath(run_dir)),
                "status": "not_started", "exit_code": None,
                "runner_process": process, "last_event": None,
                "waited_s": round(time.monotonic() - started, 1)},
                ensure_ascii=False))
            return STILL_RUNNING
        time.sleep(min(a.interval_s,
                       max(0.0, a.timeout_s - (time.monotonic() - started))))

    while True:
        results = read_results(run_dir)
        status = status_of(results)
        if status is not None and status != "running":
            report = {"run_id": results.get("run_id"), "status": status,
                      "exit_code": results.get("exit_code"),
                      "last_event": last_log_event(run_dir),
                      "waited_s": round(time.monotonic() - started, 1)}
            print(json.dumps(report, ensure_ascii=False))
            return 0
        process = runner_process(run_dir)
        if process == "gone":
            # Re-read: the runner may have finalized between the two probes.
            results = read_results(run_dir)
            status = status_of(results)
            if status is not None and status != "running":
                continue
            command, not_before, plan = resume_of(run_dir)
            print(json.dumps({
                "run_id": (results or {}).get("run_id")
                or os.path.basename(os.path.abspath(run_dir)),
                "status": status or "running", "exit_code": None,
                "runner_process": "gone",
                "last_event": last_log_event(run_dir),
                "waited_s": round(time.monotonic() - started, 1),
                "resume": command,
                **({"not_before": not_before} if not_before else {}),
                **({} if plan else {"plan_note": PLAN_NOTE}),
                "holdout_look": "a resume spends no new one: the ledger "
                                "holds one row per run id"}, ensure_ascii=False))
            return RUNNER_GONE
        if time.monotonic() - started >= a.timeout_s:
            print(json.dumps({
                "run_id": (results or {}).get("run_id")
                or os.path.basename(os.path.abspath(run_dir)),
                "status": status or "running", "exit_code": None,
                "runner_process": process,
                "last_event": last_log_event(run_dir),
                "waited_s": round(time.monotonic() - started, 1)},
                ensure_ascii=False))
            return STILL_RUNNING
        time.sleep(min(a.interval_s,
                       max(0.0, a.timeout_s - (time.monotonic() - started))))


if __name__ == "__main__":
    sys.exit(main())
