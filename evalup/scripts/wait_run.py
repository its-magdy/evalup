#!/usr/bin/env python3
"""Block until a run directory is finalized, in bounded slices a skill can loop.

Why this exists: under `claude -p` a Bash call is capped at ten minutes and a
background shell is killed about five seconds after the turn ends. The 2026-09
field test's `run` and `start` sessions launched run_cases.py with
`run_in_background`, ended the turn, and the CLI's exit killed the runner
mid-case -- twice -- leaving results.json at `summary.status: "running"`. No
single wait the platform offers outlasts a thirty-minute run, so the skill
needs a wait it can call repeatedly without ending the turn. This is that
call: it reads, it never writes, and it exits before the ten-minute cap so it
is never backgrounded itself.

  0  the run is finalized: results.json's summary.status is no longer
     "running" (ok, aborted or incomplete -- the runner's own exit code says
     which; gate.py gives the verdict)
  2  bad input ({"error": ...} on stdout, like every script here)
  3  still running when --timeout-s elapsed; call again
  4  still "running" but no run_cases.py process for this run exists: the
     runner was killed. Resume it with `run_cases.py --resume` under the SAME
     run id rather than starting a fresh run over the cases already paid for.

Prints one JSON object on stdout: the run id, the status, the runner's exit
code when finalized, the last run.log event, and whether a runner process was
seen ("alive" / "gone" / "unknown" when `ps` is unavailable).

Usage:
  wait_run.py <reports/<run-id>> [--timeout-s 540] [--interval-s 5]
"""
import argparse
import json
import os
import subprocess
import sys
import time

from _common import add_version_flag, die, load_text, require_range

STILL_RUNNING = 3
RUNNER_GONE = 4


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
        command = parts[1]
        if "run_cases.py" in command and (target in command
                                          or run_id in command):
            return "alive"
    return "gone"


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
                    help="how long to wait before exiting 3 (default "
                         "%(default)s: under the ten-minute headless cap on a "
                         "single Bash call)")
    ap.add_argument("--interval-s", type=float, default=5.0,
                    help="seconds between checks (default %(default)s)")
    a = ap.parse_args()
    require_range("--timeout-s", a.timeout_s, lo=0, hi=600)
    require_range("--interval-s", a.interval_s, lo=0.1, hi=60)

    run_dir = a.run_dir
    if not os.path.isdir(run_dir):
        die(f"bad input: {run_dir}: not a directory")
    if not (os.path.isfile(os.path.join(run_dir, "results.json"))
            or os.path.isfile(os.path.join(run_dir, "manifest.yaml"))):
        die(f"bad input: {run_dir}: no results.json or manifest.yaml; this "
            "is not a run directory run_cases.py has started")

    started = time.monotonic()
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
            print(json.dumps({
                "run_id": (results or {}).get("run_id")
                or os.path.basename(os.path.abspath(run_dir)),
                "status": status or "running", "exit_code": None,
                "runner_process": "gone",
                "last_event": last_log_event(run_dir),
                "waited_s": round(time.monotonic() - started, 1),
                "resume": "run_cases.py --plan <plan.json> --out "
                          f"{run_dir} --resume"}, ensure_ascii=False))
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
