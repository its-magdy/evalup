#!/usr/bin/env python3
"""Read a finished run and say, in an exit code, whether it may merge.

run_cases.py exits 0 on a red suite, deliberately: its exit code reports
whether the RUN completed, and "gating failures do not affect the exit code"
(runner-contract.md SS11). That is the right split, and until this script it was
half of one. The other half -- the step that turns results.json into pass/fail
-- existed only as a bash snippet in run-modes.md that needed `jq` and `yq` and
picked the run by mtime, the method analyze/SKILL.md forbids. So at the shell a
run where every case failed was byte-identical to a clean one: rc 0, nothing
printed (2026-09 audit). docs/workflow.md promised "a plain script (no Claude
needed)"; this is that script.

No LLM is in this path and none may be added: it is the gate.

  0  the run is complete and nothing gating failed
  1  the gate is closed -- every reason is listed, not the first
  2  bad input or usage ({"error": ...} on stdout, like every script here)

The gate closes when any of these holds:
  - summary.status is not "ok", or results.json's own exit_code is not 0
    (an aborted, incomplete or scorer-errored run has no quotable number, so
    "0 gating failures" from one is not a pass);
  - summary.gating_failures > 0 (holdout cases included: the sealed
    aggregate is a subset of that count, runner-contract.md SS10);
  - summary.infra_rate is above --max-infra-rate (a run that mostly could not
    reach the app passed nothing; default 0.05, the snippet's own number);
  - no case was scored pass or fail. `skipped` and `unscored` are never a
    failure (runner-contract.md SS5), and that is exactly why a suite made only
    of them must not open the gate: "0 gating failures" over zero measurements
    is not a pass, it is an absence (2026-09-21 audit: an all-multi-turn suite
    gated green forever);
  - a count the gate reads is missing or is not an integer. Reading
    `"gating_failures": "10"` as 0 opened the gate over ten failures;
  - the run directory fails run_cases.py --verify. results.json is the file
    being audited, so its own `missing_artifacts: []` is not evidence: the
    verdicts are recounted from cases/*/verdict.json. --no-verify skips this
    for a results.json shipped without its run directory, and the summary says
    so;
  - --require-canaries was passed and the harness was not verified: a canary
    did not pass, or the run carries none.

Canaries close nothing by default, deliberately. A canary that scored `fail`
already aborted the run (exit 4, status aborted_canary), which the first rule
closes on. One that never reached a verdict -- the provider dropped its call,
its only layer was disabled by `--layer X` -- says nothing about drift, sits
outside infra_rate, and closing on it would close on provider noise; and
"0/1 closes" beside "no canaries at all opens" is not a rule. So the summary
says how many were NOT VERIFIED, and --require-canaries is the strict form
for a release gate: no verified canary, no pass. (Not for a holdout run:
it selects the `holdout` split, which the linter keeps exclusive of `canary`,
so the flag always closes that gate.)

`--latest` takes a reports/ directory and picks the newest run by the
TIMESTAMP SEGMENT of its run id (<mode>-<YYYYMMDDTHHMMSSZ>), never by mtime,
which drifts when a report is regenerated or a directory is copied. `--mode`
narrows it, because the newest run is often a smoke and the gate wants the
regression.

Usage:
  gate.py <reports/<run-id>> [--max-infra-rate 0.05] [--require-canaries]
          [--no-verify] [--json]
  gate.py <reports/> --latest [--mode regression] [--json]
"""
import argparse
import json
import os
import subprocess
import sys

from _common import (
    RUN_ID_RE,
    add_version_flag,
    die,
    load_object,
    require_range,
)


def latest_run(reports_dir, mode):
    if not os.path.isdir(reports_dir):
        die(f"bad input: {reports_dir}: not a directory")
    runs = []
    for name in os.listdir(reports_dir):
        match = RUN_ID_RE.fullmatch(name)
        if not match or (mode and match.group(1) != mode):
            continue
        if os.path.isfile(os.path.join(reports_dir, name, "results.json")):
            runs.append((match.group(2), name))
    if not runs:
        wanted = f"{mode} " if mode else ""
        die(f"bad input: {reports_dir}: no {wanted}run directory holding a "
            "results.json -- a run id is <mode>-<YYYYMMDDTHHMMSSZ>")
    return os.path.join(reports_dir, max(runs)[1])


REQUIRED_COUNTS = ("n", "passes", "failures", "gating_failures")
# May be absent (an older run), but never a string or a float.
OPTIONAL_COUNTS = ("unscored", "skipped", "scorer_errors", "repeated_5xx")


def count(summary, key):
    value = summary.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0


def malformed_counts(summary):
    """A count that is absent or not an int closes the gate; it is never 0.

    count() reads such a value as 0 so the summary line can still print, which
    is only safe because this runs first. The optional counts may be absent
    (an older run) but may not be a string or a float either.
    """
    bad = []
    for key in REQUIRED_COUNTS + OPTIONAL_COUNTS:
        value = summary.get(key)
        if value is None and key in OPTIONAL_COUNTS:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            bad.append(f"summary.{key} is {value!r}, not a count")
    return bad


def verify_reasons(run_dir):
    """run_cases.py --verify, as a subprocess like every scorer call here: the
    runner owns the definition of a complete, self-consistent run directory."""
    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "run_cases.py")
    try:
        proc = subprocess.run([sys.executable, runner, "--verify", run_dir],
                              capture_output=True, text=True, timeout=120,
                              check=False)
        payload = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return [f"run_cases.py --verify could not check {run_dir}: {exc}"]
    if proc.returncode == 0:
        return []
    obj = payload if isinstance(payload, dict) else {}
    items = obj.get("missing_artifacts")
    detail = "; ".join(map(str, items)) if items else str(
        obj.get("error") if obj else payload)
    return [f"the run directory fails run_cases.py --verify: {detail}"]


def gating_rows(results, limit=5):
    """The first few gating failures, as "<case id> (<failed layers>)".

    In CI this line is often all a developer sees. Holdout cases have no row in
    results.json (the seal, runner-contract.md SS9), so nothing sealed can be
    named here.
    """
    rows = results.get("cases")
    named = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("verdict") != "fail" \
                or not row.get("gating"):
            continue
        layers = row.get("layers")
        failed = sorted(name for name, verdict in layers.items()
                        if verdict == "fail") if isinstance(layers, dict) else []
        named.append("{} ({})".format(row.get("case_id"),
                                      ", ".join(failed) or "no failed layer"))
    more = len(named) - limit
    return named[:limit] + ([f"and {more} more"] if more > 0 else [])


def evaluate(results, max_infra_rate, require_canaries=False):
    """(reasons, facts). An empty `reasons` is an open gate."""
    summary = results.get("summary")
    if not isinstance(summary, dict):
        die("bad input: results.json has no summary object")
    reasons = malformed_counts(summary)
    status = summary.get("status")
    if status != "ok":
        reasons.append(f"run status is {status!r}, not 'ok' -- its numbers are "
                       "not quotable")
    exit_code = results.get("exit_code")
    if exit_code not in (0, None):
        reasons.append(f"the runner exited {exit_code} (runner-contract.md "
                       "SS11)")
    gating = count(summary, "gating_failures")
    holdout = summary.get("holdout")
    holdout_gating = count(holdout, "gating_failures") \
        if isinstance(holdout, dict) else 0
    # SS10: holdout cases are inside summary's counts, and `holdout` is their
    # aggregate so a reader can take them OUT. Adding it back counted every
    # sealed failure twice -- "1 fail (2 gating)" (2026-09-30 review).
    if gating:
        rows = gating_rows(results)
        reasons.append(f"{gating} gating failure(s)"
                       + (": " + "; ".join(rows) if rows else "")
                       + (f" ({holdout_gating} of them in the sealed holdout, "
                          "aggregate only)" if holdout_gating else ""))
    if holdout_gating > gating:
        # A part larger than its whole: these counts were not written by the
        # runner's summary(), and the gate does not guess which one is right.
        reasons.append(f"summary.holdout.gating_failures ({holdout_gating}) "
                       f"exceeds summary.gating_failures ({gating}), which "
                       "includes it")
    infra_rate = summary.get("infra_rate")
    if isinstance(infra_rate, (int, float)) and not isinstance(infra_rate, bool) \
            and infra_rate > max_infra_rate:
        reasons.append(f"infra rate {infra_rate:.1%} is above "
                       f"{max_infra_rate:.1%}")
    missing = summary.get("missing_artifacts") or []
    if missing:
        reasons.append("missing artifacts: " + ", ".join(map(str, missing)))
    scored = count(summary, "passes") + count(summary, "failures")
    if not scored:
        reasons.append(
            "no case was scored pass or fail ({} skipped, {} unscored of {}) "
            "-- nothing was measured, so there is nothing to pass".format(
                count(summary, "skipped"), count(summary, "unscored"),
                count(summary, "n")))

    canaries = summary.get("canaries")
    if require_canaries:
        # Open only on 0 < passed == n, both real counts. Anything else --
        # absent, malformed, negative, passed > n -- is the closed reading:
        # nothing here verified the harness.
        block = canaries if isinstance(canaries, dict) else {}
        total, passed = block.get("n"), block.get("passed")
        why = None
        if canaries is None or (total == 0 and passed == 0
                                and is_count(total) and is_count(passed)):
            why = "the run carries no canary"
        elif not (is_count(total) and is_count(passed) and passed <= total):
            why = f"summary.canaries is {canaries!r}, not a pair of counts"
        elif passed < total:
            why = f"{passed}/{total} canaries passed"
        if why:
            reasons.append(f"harness not verified (--require-canaries): {why}")

    facts = {
        "run_id": results.get("run_id"), "mode": results.get("mode"),
        "status": status, "n": count(summary, "n"),
        "passes": count(summary, "passes"),
        "failures": count(summary, "failures"),
        "gating_failures": gating,
        "unscored": count(summary, "unscored"),
        "skipped": count(summary, "skipped"),
        "infra_errors": count(summary, "infra_errors"),
        "repeated_5xx": count(summary, "repeated_5xx"),
        "infra_rate": infra_rate, "crash_rate": summary.get("crash_rate"),
        "scorer_errors": count(summary, "scorer_errors"),
        "unscorable_layers": summary.get("unscorable_layers") or [],
        "unjudged": summary.get("unjudged"),
        "canaries": canaries, "require_canaries": require_canaries,
        "holdout": holdout,
    }
    return reasons, facts


def percent(value):
    return f"{value:.1%}" if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else "n/a"


def render(facts, reasons):
    """One screen. A green suite says what it could NOT see, because "6/6
    pass" over three scored layers and six unscorable ones is a smaller claim
    than it reads as."""
    verdict = "FAIL" if reasons else "PASS"
    failures, gating = facts["failures"], facts["gating_failures"]
    if isinstance(failures, int) and isinstance(gating, int) \
            and failures > gating:
        # A suite where nothing gates yet (every case still `pending`)
        # opens the gate on 2 of 4 failing; the field test (2026-09-25)
        # read the first word and stopped. Say on the same line what the
        # verdict is not counting.
        verdict += (f" ({failures - gating} failure(s) on non-gating cases,"
                    " not counted)")
    lines = [f"{facts['run_id']}: {verdict}",
             f"  {facts['n']} cases: {facts['passes']} pass, "
             f"{facts['failures']} fail ({facts['gating_failures']} gating), "
             f"{facts['unscored']} unscored, {facts['skipped']} skipped",
             f"  infra {percent(facts['infra_rate'])}, crash "
             f"{percent(facts['crash_rate'])}, scorer errors "
             f"{facts['scorer_errors']}"]
    if facts["repeated_5xx"]:
        # An exhausted 5xx is infra, never a fail -- but a case that got the
        # SAME 5xx on every attempt of every repeat may be the app's own
        # error (the 2026-09-26 user test's guard-blocked injection read as
        # provider noise, F-165). Re-running or raising --max-infra-rate
        # would hide it; say so where the infra number is read.
        # A holdout run's case directories are sealed: say what, not where.
        where = ("the cases are sealed; do not open them" if facts["holdout"]
                 else "read their response.json before re-running")
        lines.append(
            f"  {facts['repeated_5xx']} infra case(s) got the same 5xx on "
            "every attempt: possibly the app's own error, not the provider "
            f"-- {where}")
    canaries = facts["canaries"]
    if isinstance(canaries, dict) and canaries.get("n"):
        passed, total = canaries.get("passed"), canaries.get("n")
        line = f"  canaries {passed}/{total} pass"
        if facts["status"] == "ok" and facts["verify_clean"] \
                and is_count(passed) and is_count(total) and passed < total:
            # A bare "0/1" read as a failed canary, and in a run that
            # finished it never is one: a canary `fail` aborts, and --verify
            # refuses `ok` beside one. That second half is why the claim is
            # made only over a run directory that just verified -- under
            # --no-verify, or beside a verify failure, the counts stand alone.
            line += (f"; {total - passed} NOT VERIFIED (infra, unscored or "
                     "skipped -- no verdict either way; a canary `fail` "
                     "aborts the run)")
        lines.append(line)
    holdout = facts["holdout"]
    if isinstance(holdout, dict):
        # Over the SCORED holdout cases when the run records failures
        # (runner-contract SS10: `n` is never a pass-rate denominator); an
        # older run has only `n`.
        scored = holdout.get("n")
        if isinstance(holdout.get("failures"), int) \
                and isinstance(holdout.get("passes"), int):
            scored = holdout["passes"] + holdout["failures"]
        lines.append(f"  holdout (aggregate only): {holdout.get('passes')}/"
                     f"{scored} pass")
    if facts["unscorable_layers"]:
        lines.append("  NOT scored this run: "
                     + ", ".join(map(str, facts["unscorable_layers"])))
    if facts["unjudged"]:
        lines.append(f"  judged layers: unjudged ({facts['unjudged']})")
    if not facts["verified"]:
        lines.append("  artifacts NOT verified (--no-verify): this verdict "
                     "rests on results.json alone")
    lines.extend(f"  gate closed: {reason}" for reason in reasons)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Turn a finished run's results.json into a pass/fail exit "
                    "code (0 open, 1 closed, 2 bad input) and a one-screen "
                    "summary. No LLM involved: this is the CI gate.")
    add_version_flag(ap)
    ap.add_argument("path", help="a run directory (reports/<run-id>), or with "
                                 "--latest the reports/ directory")
    ap.add_argument("--latest", action="store_true",
                    help="treat path as reports/ and gate its newest run, by "
                         "the run id's timestamp segment (never mtime)")
    ap.add_argument("--mode", help="with --latest, only runs of this mode "
                                   "(e.g. regression)")
    ap.add_argument("--max-infra-rate", type=float, default=0.05,
                    help="close the gate above this infra_rate "
                         "(default: %(default)s)")
    ap.add_argument("--require-canaries", action="store_true",
                    help="also close the gate when the harness was not "
                         "verified: a canary did not pass, or the run has "
                         "none (for a release gate; not for a holdout run, "
                         "whose split holds no canary)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip run_cases.py --verify over the run directory "
                         "(for a results.json shipped without its cases/); "
                         "the summary then says the artifacts were not "
                         "checked")
    ap.add_argument("--json", action="store_true",
                    help="print the summary as JSON instead of text")
    a = ap.parse_args()
    require_range("--max-infra-rate", a.max_infra_rate, lo=0, hi=1)
    if a.mode and not a.latest:
        die("--mode only narrows --latest")

    run_dir = latest_run(a.path, a.mode) if a.latest else a.path
    results_path = os.path.join(run_dir, "results.json")
    if not os.path.isfile(results_path):
        die(f"bad input: {run_dir}: holds no results.json -- a run directory is "
            "reports/<run-id>; pass --latest to gate the newest run under a "
            "reports/ directory")
    reasons, facts = evaluate(load_object(results_path), a.max_infra_rate,
                              a.require_canaries)
    problems = [] if a.no_verify else verify_reasons(run_dir)
    reasons.extend(problems)
    facts["verified"] = not a.no_verify
    facts["verify_clean"] = facts["verified"] and not problems
    if a.json:
        print(json.dumps(dict(facts, gate="closed" if reasons else "open",
                              reasons=reasons), ensure_ascii=False))
    else:
        print(render(facts, reasons))
    raise SystemExit(1 if reasons else 0)


if __name__ == "__main__":
    main()
