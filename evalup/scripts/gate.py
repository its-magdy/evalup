#!/usr/bin/env python3
"""Read a finished run and say, in an exit code, whether it may merge.

run_cases.py exits 0 on a red suite, deliberately: its exit code reports
whether the RUN completed, and "gating failures do not affect the exit code"
(runner-contract.md SS11). That is the right split, and this script is its
other half: the step that turns results.json into pass/fail. Without it, at
the shell a run where every case failed is byte-identical to a clean one: rc
0, nothing printed. It is a plain Python script -- no `jq`/`yq` to install,
and it picks a run by its id's timestamp, never by mtime (the method
analyze/SKILL.md forbids) -- so CI can run it with no Claude in the loop
(docs/workflow.md).

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
    is not a pass, it is an absence (an all-multi-turn suite would otherwise
    gate green forever);
  - a count the gate reads is missing or is not an integer. Reading
    `"gating_failures": "10"` as 0 would open the gate over ten failures;
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

`--baseline` makes the gating-failure rule RELATIVE (#40). At k = 3 a case
passes only if all three repeats pass (pass^k), so a non-deterministic app
with a hundred gating cases almost always has one flaky case, and the rule
above keeps its gate red most days. With `--baseline <run>` (repeatable: a
run directory, or the reports/baseline.json pointer), a gating failure closes
the gate only when the baseline gives no reason to excuse it:
  - the case passed every pass/fail trial it had in the baseline runs
    (reliable): a REGRESSION, closes;
  - the case is not in the baseline, or never reached pass/fail there: no
    evidence it is flaky, so it closes too -- the gate fails closed;
  - the case passed some trials and failed others (flaky), or failed every
    one (already failing): REPORTED, by name, and does not gate -- unless it
    now fails a layer it never failed in any baseline trial (flaky on
    `routing`, failing `authz` now), which closes. The excuse covers the
    layers the baseline saw fail, not the whole case. It does not weigh how
    often: a case that passed 2/3 trials there and fails 3/3 now is excused.
A trial is one repeat (cases/<id>/verdict.json's `repeats`), so a k = 3
baseline gives three per case, and two pinned k = 1 runs give two. Every
other rule above still closes the gate. This is a gate POLICY over verdicts
the runner already wrote: no verdict, no pass^k fold and no SCORING_VERSION
changes. The baseline must be comparable (same scoring and dataset version,
status ok, its directory verifies) and neither run may carry sealed holdout
cases, which have no per-case row to compare -- each is exit 2.

`--min-pass-rate R` is the optional absolute floor, in either mode: the gate
closes when passes / (passes + failures) -- the summary's own counts, over
the scored non-canary cases, gating or not -- is below R.

`--latest` takes a reports/ directory and picks the newest run by the
TIMESTAMP SEGMENT of its run id (<mode>-<YYYYMMDDTHHMMSSZ>), never by mtime,
which drifts when a report is regenerated or a directory is copied. `--mode`
narrows it, because the newest run is often a smoke and the gate wants the
regression.

Usage:
  gate.py <reports/<run-id>> [--max-infra-rate 0.05] [--require-canaries]
          [--baseline <run>]... [--min-pass-rate R] [--no-verify] [--json]
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
    scoring_comparison,
    unsafe_case_id,
)

PASS, FAIL = "pass", "fail"


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


def describe(row):
    """"<case id> (<failed layers>)", the form every named failure takes."""
    layers = row.get("layers")
    failed = sorted(name for name, verdict in layers.items()
                    if verdict == FAIL) if isinstance(layers, dict) else []
    return "{} ({})".format(row.get("case_id"),
                            ", ".join(failed) or "no failed layer")


def gating_fail_rows(results):
    rows = results.get("cases")
    return [row for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and row.get("verdict") == FAIL
            and row.get("gating")]


def capped(named, limit=5):
    more = len(named) - limit
    return named[:limit] + ([f"and {more} more"] if more > 0 else [])


def gating_rows(results, limit=5):
    """The first few gating failures, as "<case id> (<failed layers>)".

    In CI this line is often all a developer sees. Holdout cases have no row in
    results.json (the seal, runner-contract.md SS9), so nothing sealed can be
    named here.
    """
    return capped([describe(row) for row in gating_fail_rows(results)], limit)


# -- --baseline (#40): a gate policy over verdicts already written ----------

def resolve_baseline(path):
    """A run directory, or a baseline.json pointer (pin_baseline.py's), which
    names a run directory beside it."""
    if os.path.isdir(path):
        return path
    if os.path.isfile(path):
        run_id = load_object(path).get("run_id")
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            die(f"bad input: --baseline {path}: run_id is {run_id!r}, not a "
                "run id (<mode>-<YYYYMMDDTHHMMSSZ>)")
        return os.path.join(os.path.dirname(os.path.abspath(path)), run_id)
    die(f"bad input: --baseline {path}: not a run directory or a "
        "baseline.json pointer")


def failed_layers(layers):
    """The layers a row or a repeat failed. A results.json row's layers are
    flat ({name: verdict}); a verdict.json repeat's are not ({name: {verdict,
    ...}})."""
    if not isinstance(layers, dict):
        return set()
    return {name for name, layer in layers.items()
            if (layer.get("verdict") if isinstance(layer, dict) else layer)
            == FAIL}


def case_trials(run_dir, row):
    """The pass/fail trials one run holds for one case, as (verdict, failed
    layers) pairs: its repeats (cases/<id>/verdict.json) when k > 1, else its
    one verdict. A repeat that reached no verdict (infra, unscored) is no
    trial either way."""
    case_id = row.get("case_id")
    if isinstance(case_id, str) and unsafe_case_id(case_id) is None:
        path = os.path.join(run_dir, "cases", case_id, "verdict.json")
        if os.path.isfile(path):
            repeats = load_object(path).get("repeats")
            if isinstance(repeats, list) and repeats:
                return [(r["verdict"], failed_layers(r.get("layers")))
                        for r in repeats if isinstance(r, dict)
                        and r.get("verdict") in (PASS, FAIL)]
    if row.get("verdict") in (PASS, FAIL):
        return [(row["verdict"], failed_layers(row.get("layers")))]
    return []


def sealed(results):
    holdout = (results.get("summary") or {}).get("holdout") \
        if isinstance(results.get("summary"), dict) else None
    return results.get("mode") == "holdout" or isinstance(holdout, dict)


def load_baselines(paths, results, verify, run_dir):
    """{"runs": [run ids], "trials": {case id: [(verdict, failed layers),
    ...]}, "warnings": [...], "run_dir": the gated run's directory}, or exit
    2 on a baseline that cannot be one."""
    current_dir = run_dir
    if sealed(results):
        die("bad input: --baseline cannot gate a run holding sealed holdout "
            "cases: they have no per-case row in results.json to compare, "
            "so a regression among them could not be seen")
    runs, trials, warnings = [], {}, []
    for path in paths:
        run_dir = resolve_baseline(path)
        results_path = os.path.join(run_dir, "results.json")
        if not os.path.isfile(results_path):
            die(f"bad input: --baseline {path}: {run_dir} holds no "
                "results.json")
        base = load_object(results_path)
        run_id = base.get("run_id")
        summary = base.get("summary")
        status = summary.get("status") if isinstance(summary, dict) else None
        if status != "ok" or base.get("exit_code") not in (0, None):
            die(f"bad input: --baseline {run_id!r}: status {status!r}, exit "
                f"code {base.get('exit_code')!r} -- a run that did not finish "
                "clean is not quotable, so it is not a baseline")
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            die(f"bad input: --baseline {path}: results.json's run_id is "
                f"{run_id!r}, not a run id")
        if run_id == results.get("run_id") or run_id in runs:
            die(f"bad input: --baseline {run_id!r} is the run being gated, "
                "or was passed twice")
        if sealed(base):
            die(f"bad input: --baseline {run_id!r} holds sealed holdout "
                "cases, which have no per-case row to compare")
        comparable, mismatch, warning = scoring_comparison(base, results)
        if not comparable:
            die(f"bad input: --baseline {run_id!r} is not comparable to this "
                f"run -- {mismatch}")
        if base.get("dataset_version") != results.get("dataset_version"):
            die(f"bad input: --baseline {run_id!r} is not comparable to this "
                f"run -- dataset_version: baseline "
                f"{base.get('dataset_version')!r}, this run "
                f"{results.get('dataset_version')!r}")
        if warning:
            warnings.append(warning)
        if verify:
            problems = verify_reasons(run_dir)
            if problems:
                die(f"bad input: --baseline {run_id!r}: " + "; ".join(problems))
        rows = base.get("cases")
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("case_id"), str):
                trials.setdefault(row["case_id"], []).extend(
                    case_trials(run_dir, row))
        runs.append(run_id)
    return {"runs": runs, "trials": trials, "warnings": warnings,
            "run_dir": current_dir}


def standing(trials):
    """A case's standing in the baseline, from its pass/fail trials."""
    if not trials:
        return "unknown"
    if all(verdict == PASS for verdict, _ in trials):
        return "reliable"
    return "failing" if all(verdict == FAIL for verdict, _ in trials) \
        else "flaky"


def baseline_reasons(results, gating, baseline):
    """(reasons, facts) for the relative rule: which of this run's gating
    failures the baseline excuses, and which it does not."""
    groups = {"reliable": [], "unknown": [], "flaky": [], "failing": [],
              "new_layer": []}
    rows = gating_fail_rows(results)
    counts = []
    for row in rows:
        case_id = row.get("case_id")
        found = baseline["trials"].get(case_id, []) \
            if isinstance(case_id, str) else []
        passed = sum(1 for verdict, _ in found if verdict == PASS)
        group = standing(found)
        # An excuse covers the layers the baseline saw fail, not the case: a
        # case flaky on `routing` that now fails `authz` has a new failure.
        # The layers failing now are every repeat's, not only the row's.
        now = failed_layers(row.get("layers"))
        for _, layers in case_trials(baseline["run_dir"], row):
            now |= layers
        seen = set().union(*(layers for _, layers in found)) if found \
            else set()
        new = sorted(now - seen)
        if group in ("flaky", "failing") and new:
            group = "new_layer"
        if found:
            counts.append(len(found))
        groups[group].append(
            f"{describe(row)} [{passed}/{len(found)} baseline trials passed"
            + (f"; never failed {', '.join(new)} there"
               if group == "new_layer" else "") + "]")
    reasons = []
    if groups["reliable"]:
        reasons.append("{} regression(s): passed every baseline trial, fail "
                       "now: {}".format(len(groups["reliable"]),
                                        "; ".join(capped(groups["reliable"]))))
    if groups["unknown"]:
        reasons.append(
            "{} gating failure(s) with no pass/fail verdict in the baseline "
            "(new, or never scored there -- not known to be flaky, so they "
            "gate): {}".format(len(groups["unknown"]),
                               "; ".join(capped(groups["unknown"]))))
    if groups["new_layer"]:
        reasons.append(
            "{} gating failure(s) on a layer the case never failed in the "
            "baseline (flaky or failing there on other layers only): "
            "{}".format(len(groups["new_layer"]),
                        "; ".join(capped(groups["new_layer"]))))
    if len(rows) != gating:
        # The rows are what this rule reads; the count is what the default
        # rule reads. A gap means one of them was not written by the runner.
        reasons.append(f"results.json's case rows hold {len(rows)} gating "
                       f"failure(s), summary.gating_failures says {gating}")
    facts = {"runs": baseline["runs"],
             "regressions": groups["reliable"],
             "no_baseline_verdict": groups["unknown"],
             "new_layer_failures": groups["new_layer"],
             "flaky_excused": groups["flaky"],
             "failing_excused": groups["failing"],
             "min_trials": min(counts) if counts else 0,
             "warnings": baseline["warnings"]}
    return reasons, facts


def evaluate(results, max_infra_rate, require_canaries=False, baseline=None,
             min_pass_rate=None):
    """(reasons, facts). An empty `reasons` is an open gate. `baseline` is
    load_baselines()'s, and swaps the gating-failure rule for the relative
    one; nothing else changes with it."""
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
    # aggregate so a reader can take them OUT. Adding it back would count
    # every sealed failure twice -- "1 fail (2 gating)".
    baseline_facts = None
    if baseline is not None:
        relative, baseline_facts = baseline_reasons(results, gating, baseline)
        reasons.extend(relative)
    elif gating:
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
    pass_rate = count(summary, "passes") / scored if scored else None
    if min_pass_rate is not None and pass_rate is not None \
            and pass_rate < min_pass_rate:
        reasons.append(f"pass rate {pass_rate:.1%} ({count(summary, 'passes')}"
                       f"/{scored} scored) is below the --min-pass-rate floor "
                       f"{min_pass_rate:.1%}")

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
        "uniform_4xx": uniform_4xx_fact(summary),
        "infra_rate": infra_rate, "crash_rate": summary.get("crash_rate"),
        "scorer_errors": count(summary, "scorer_errors"),
        "unscorable_layers": summary.get("unscorable_layers") or [],
        "unjudged": summary.get("unjudged"),
        "canaries": canaries, "require_canaries": require_canaries,
        "holdout": holdout,
        "multi_turn": summary.get("multi_turn"),
        "memory": results.get("memory"),
        "pass_rate": pass_rate, "min_pass_rate": min_pass_rate,
        "baseline": baseline_facts,
    }
    return reasons, facts


def uniform_4xx_fact(summary):
    """summary.uniform_4xx when it is {status, cases} counts, else None.
    Printed, never a reason: it changes no verdict (#50)."""
    found = summary.get("uniform_4xx")
    if isinstance(found, dict) and is_count(found.get("status")) \
            and is_count(found.get("cases")):
        return found
    return None


def percent(value):
    return f"{value:.1%}" if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else "n/a"


def conversation_lines(facts):
    """docs/multi-turn.md SS4/SS6: conversations' pass rate BESIDE the
    single-turn one, never only the blend. pass^k over every turn makes a
    conversation look worse by construction (four turns at 0.9 pass about
    0.66 of the time), so a blended rate falling is not, by itself, the app
    getting worse. Rates are over the SCORED cases, like every rate here."""
    lines = []
    multi = facts.get("multi_turn")
    if isinstance(multi, dict) and all(
            is_count(multi.get(key)) for key in ("n", "passes", "failures")):
        single_pass = facts["passes"] - multi["passes"] \
            if isinstance(facts["passes"], int) else None
        single_fail = facts["failures"] - multi["failures"] \
            if isinstance(facts["failures"], int) else None
        lines.append(
            "  multi-turn: {}/{} pass ({} conversation(s)); single-turn: "
            "{}/{} pass".format(
                multi["passes"], multi["passes"] + multi["failures"],
                multi["n"], single_pass,
                None if single_pass is None or single_fail is None
                else single_pass + single_fail))
    if facts.get("memory") == "user":
        lines.append(
            "  conversation memory outlives the session (memory: user): a "
            "case may see an earlier case's turns, so any verdict may be "
            "contaminated")
    return lines


def baseline_lines(facts):
    lines = []
    if facts.get("min_pass_rate") is not None:
        lines.append(f"  pass rate {percent(facts['pass_rate'])}, floor "
                     f"{percent(facts['min_pass_rate'])}")
    base = facts.get("baseline")
    if not base:
        return lines
    lines.append("  baseline {}: {} regression(s), {} with no baseline "
                 "verdict, {} on a new layer, {} flaky and {} already failing "
                 "(excused)".format(
                     ", ".join(map(str, base["runs"])),
                     len(base["regressions"]),
                     len(base["no_baseline_verdict"]),
                     len(base["new_layer_failures"]),
                     len(base["flaky_excused"]), len(base["failing_excused"])))
    for label, key in (("flaky in the baseline, not gating",
                        "flaky_excused"),
                       ("already failing in the baseline, not gating",
                        "failing_excused")):
        if base[key]:
            lines.append(f"  {label}: " + "; ".join(capped(base[key])))
    if 0 < base["min_trials"] < 3:
        # One lucky pass reads as "reliable". Said, not refused: a k = 1
        # baseline is what most suites have.
        lines.append(
            f"  a failing case's baseline rests on {base['min_trials']} "
            "trial(s): a "
            "flaky case that passed them counts as reliable -- pass more "
            "--baseline runs, or pin a k >= 3 run, to tell flaky from lucky")
    lines.extend(f"  baseline comparability warning: {w}"
                 for w in base["warnings"])
    return lines


def render(facts, reasons):
    """One screen. A green suite says what it could NOT see, because "6/6
    pass" over three scored layers and six unscorable ones is a smaller claim
    than it reads as."""
    verdict = "FAIL" if reasons else "PASS"
    failures, gating = facts["failures"], facts["gating_failures"]
    if isinstance(failures, int) and isinstance(gating, int) \
            and failures > gating:
        # A suite where nothing gates yet (every case still `pending`)
        # opens the gate on 2 of 4 failing, and a reader who stops at the
        # first word misses that. Say on the same line what the verdict is
        # not counting.
        verdict += (f" ({failures - gating} failure(s) on non-gating cases,"
                    " not counted)")
    base = facts.get("baseline")
    excused = len(base["flaky_excused"]) + len(base["failing_excused"]) \
        if base else 0
    if excused:
        # Same reason as above: an excused failure is still a failure.
        verdict += (f" ({excused} gating failure(s) excused by the baseline:"
                    " flaky or already failing there)")
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
        # error (F-165: a guard that blocks an injection with a 5xx reads
        # exactly like provider noise). Re-running or raising
        # --max-infra-rate would hide it; say so where the infra number is
        # read.
        # A holdout run's case directories are sealed: say what, not where.
        where = ("the cases are sealed; do not open them" if facts["holdout"]
                 else "read their response.json before re-running")
        lines.append(
            f"  {facts['repeated_5xx']} infra case(s) got the same 5xx on "
            "every attempt: possibly the app's own error, not the provider "
            f"-- {where}")
    if facts["uniform_4xx"]:
        # Every sent case got one 4xx on every request. Each verdict stands
        # as scored, and under route_from_status {403: denied} they can all
        # read as correct refusals -- but a guard refusing the harness (a
        # same-origin or CSRF check, a lapsed credential) looks exactly like
        # this (#50). Say so beside the counts it would otherwise explain.
        found = facts["uniform_4xx"]
        lines.append(
            f"  all {found['cases']} answered case(s), canaries included, "
            f"got HTTP {found['status']} on every request: likely the "
            "harness refused (same-origin/CSRF guard, auth) or misaddressed, "
            "not the app's answer -- check invocation.headers, auth, "
            "endpoint and request_body before reading these verdicts")
    lines.extend(conversation_lines(facts))
    lines.extend(baseline_lines(facts))
    canaries = facts["canaries"]
    if isinstance(canaries, dict) and canaries.get("n"):
        passed, total = canaries.get("passed"), canaries.get("n")
        line = f"  canaries {passed}/{total} pass"
        if facts["status"] == "ok" and facts["verify_clean"] \
                and is_count(passed) and is_count(total) and passed < total:
            # A bare "0/1" reads as a failed canary, and in a run that
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
    ap.add_argument("--baseline", action="append", metavar="RUN",
                    help="gate relative to this run (a reports/<run-id> "
                         "directory or the reports/baseline.json pointer; "
                         "repeatable): a gating failure closes only if the "
                         "case passed every baseline trial or has none -- "
                         "one flaky or already failing there is reported, "
                         "not gating")
    ap.add_argument("--min-pass-rate", type=float, metavar="R",
                    help="also close the gate when passes / (passes + "
                         "failures) is below R (0-1): the absolute floor")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip run_cases.py --verify over the run directory "
                         "(for a results.json shipped without its cases/); "
                         "the summary then says the artifacts were not "
                         "checked")
    ap.add_argument("--json", action="store_true",
                    help="print the summary as JSON instead of text")
    a = ap.parse_args()
    require_range("--max-infra-rate", a.max_infra_rate, lo=0, hi=1)
    if a.min_pass_rate is not None:
        require_range("--min-pass-rate", a.min_pass_rate, lo=0, hi=1)
    if a.mode and not a.latest:
        die("--mode only narrows --latest")

    run_dir = latest_run(a.path, a.mode) if a.latest else a.path
    results_path = os.path.join(run_dir, "results.json")
    if not os.path.isfile(results_path):
        die(f"bad input: {run_dir}: holds no results.json -- a run directory is "
            "reports/<run-id>; pass --latest to gate the newest run under a "
            "reports/ directory")
    results = load_object(results_path)
    baseline = load_baselines(a.baseline, results, not a.no_verify, run_dir) \
        if a.baseline else None
    reasons, facts = evaluate(results, a.max_infra_rate, a.require_canaries,
                              baseline, a.min_pass_rate)
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
