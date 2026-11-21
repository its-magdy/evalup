#!/usr/bin/env python3
"""The run-time judge pass: every deterministic step of it. Stdlib only.

docs/judge-run.md is the design, and this script is its SS5. The runner stays
Claude-free: it never calls a judge, and nothing here writes a file the
runner, gate.py, stats.py or run_history.py reads. The run skill does the
launches (one `evalup:judge` subagent per call); this script decides what to
launch, checks what came back, and folds it -- the skill never edits or
composes a verdict.

  plan    reports/<run-id> --rubrics <state>/rubrics
          Selects the cases (SS5), snapshots each rubric (id, version, sha256
          of the file bytes), materialises each rubric's self-test as files
          (SS3), prints the call and token estimate (SS11), and writes
          judge/plan.json with one entry per launch, self-tests first. Each
          entry carries the exact prompt to hand the judge and the record
          command to run after it. Exit 0 with nothing to judge is normal.
  record  reports/<run-id> (--case ID | --self-test RUBRIC:good|bad)
          --reply-file judge/inbox/<key>.txt
          Extracts the LAST top-level JSON object of the judge's message,
          checks it against the assignment (SS4), stamps it with the plan's
          rubric sha256 and writes judge/replies/<key>.json. Counts the launch
          against --max-calls: a record past the budget is refused.
  merge   reports/<run-id> [--calibration <state>/judge/calibration.json]
          Applies the self-test (SS3), the fold (SS1), the model-id rule (SS7),
          the canary rule (SS9) and the calibration conditions (SS6), and
          writes judge/judged.json. Rerunnable: derived from plan.json and
          replies/ only.
  diff    <judged A> <judged B>
          Counts per state on each side and the cases whose judged verdict
          changed -- never a keep, never a p-value. Refuses (exit 2) when the
          judge keys (_common.judge_key), the runs' scoring keys or their
          dataset versions differ.

THE WALL (SS8): a judged verdict is `unjudged`, `provisional` or `calibrated`,
and in v1 none of the three reaches a gate, stats.py or an optimize
objective. That is structural: judged.json is a file nothing on the decision
path opens.

Rubric files are YAML frontmatter; `plan` hands a directory to
`convert_suite.py --rubrics-dir`, the one script allowed PyYAML, the way
run_cases.py --probe hands it adapter.yaml. A JSON file -- convert_suite's
rubrics.json, or a converted.json carrying `rubrics` -- is read directly.

Exit 0 done; 2 bad input or a refused step ({"error": ...} on stdout,
_common.die).
"""
import argparse
import bisect
import contextlib
import datetime
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys

from _common import (
    HARNESS_VERSION,
    JUDGE_VERSION,
    RUN_ID_RE,
    BadJSON,
    add_version_flag,
    die,
    judge_key,
    load_json,
    loads_strict,
    nfc,
    scoring_key,
    unsafe_case_id,
)
from _rubric import is_criteria_form, rubric_findings, split_pin

PLAN_SCHEMA = "evalup/judge-plan/1"
RECORD_SCHEMA = "evalup/judge-record/1"
REPLY_SCHEMA = "evalup/judge-reply/1"
JUDGED_SCHEMA = "evalup/judged/1"
CALIBRATION_V2 = "evalup/judge-calibration/2"
MODEL_ID_RE = re.compile(r"claude-[a-z0-9.-]+")
DEFAULT_MAX_CALLS = 25
DEFAULT_MODEL = "opus"            # agents/judge.md's frontmatter `model:`
# The measured subagent overhead per launch, 2026-09 user tests (SS11).
LAUNCH_TOKENS = (17_000, 35_000)
OUTPUT_TOKENS = 1_000
# A case with any of these layer rows is not judged by default (SS5).
BLOCKING_ROWS = ("error", "infra_error", "infra_incomplete")
VERDICTS = ("pass", "fail", "unknown")
SIDES = ("good", "bad")
REDACTED = re.compile(r"\[REDACTED[^\]]*\]")
# A quote's unredacted text holds at least this many letters or digits, and
# one redaction stands for at most this many characters (quote_found).
MIN_QUOTE_CHARS = 2
MAX_REDACTED_SPAN = 4000
SAME_FAMILY_NOTE = (
    "the app and its judge are both Claude: a judge passes its own family's "
    "failing outputs more often (Panickssery et al. 2024, arXiv 2508.06709, "
    "arXiv 2604.06996); measured for this app only once labels exist")


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(text):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        die(f"cannot write output: {exc}")


def read_optional(path):
    """A JSON object, or None when the file is absent."""
    if not os.path.isfile(path):
        return None
    doc = load_json(path)
    return doc if isinstance(doc, dict) else None


def judge_dir(run_dir):
    return os.path.join(run_dir, "judge")


def load_run(run_dir):
    if not os.path.isdir(run_dir):
        die(f"bad input: {run_dir}: not a run directory")
    manifest = read_optional(os.path.join(run_dir, "manifest.yaml"))
    if manifest is None:
        die(f"bad input: {run_dir}: no manifest.yaml (is this a run "
            "directory run_cases.py wrote?)")
    return manifest


def load_plan(run_dir):
    path = os.path.join(judge_dir(run_dir), "plan.json")
    plan = read_optional(path)
    if plan is None or plan.get("schema") != PLAN_SCHEMA:
        die(f"bad input: {path}: no judge plan -- run `judge_run.py plan` "
            "first")
    return plan


# -- plan -------------------------------------------------------------------

def load_rubrics(source):
    """{"rubrics": [...]} from a rubrics directory or a JSON file."""
    if os.path.isdir(source):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "convert_suite.py")
        proc = subprocess.run(
            [sys.executable, script, "--rubrics-dir", source],
            capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            try:
                message = json.loads(proc.stdout)["error"]
            except (ValueError, KeyError, TypeError):
                message = (proc.stdout or proc.stderr).strip() \
                    or f"exit {proc.returncode}"
            die(f"--rubrics {source}: convert_suite.py: {message}")
        text = proc.stdout
    elif os.path.isfile(source):
        try:
            with open(source, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            die(f"bad input: --rubrics {source}: {exc}")
    else:
        # No rubrics directory is a state location without rubrics: every
        # case that pins one is `unjudged`, with that as the reason.
        return []
    try:
        doc = loads_strict(text)
    except BadJSON as exc:
        die(f"bad input: --rubrics {source}: {exc}")
    entries = doc.get("rubrics") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        die(f"bad input: --rubrics {source}: no `rubrics` list (convert_"
            "suite.py --rubrics-dir writes one)")
    return entries


def index_rubrics(entries):
    """rubric_id -> snapshot, with `invalid` naming the first ERROR."""
    by_id, seen = {}, {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        front = entry.get("frontmatter")
        file = str(entry.get("file") or "")
        stem = os.path.splitext(os.path.basename(file))[0] or None
        rid = front.get("rubric_id") if isinstance(front, dict) else None
        if not isinstance(rid, str):
            continue
        errors = [m for sev, _, m in rubric_findings(front, stem)
                  if sev == "ERROR"]
        snap = {"file": file, "sha256": entry.get("sha256"),
                "version": front.get("version"),
                "form": "criteria" if is_criteria_form(front) else "dag",
                "invalid": errors[0] if errors else None}
        if rid in seen:
            snap["invalid"] = f"rubric_id {rid!r} is in both {seen[rid]} " \
                              f"and {file}"
            by_id[rid]["invalid"] = snap["invalid"]
        seen[rid] = file
        if snap["form"] == "criteria" and not errors:
            snap["criteria"] = [
                {"id": c["id"], "question": " ".join(c["question"].split()),
                 "reference_guided": bool(c.get("reference_guided")),
                 "canary_guard": bool(c.get("canary_guard"))}
                for c in front["criteria"]]
            snap["self_test"] = front["self_test"]
        by_id[rid] = snap
    return by_id


def rubric_ref(case_dir):
    """The case's pinned rubric ref: the case-level expect.json, else the
    last turn's (a conversation's deciding turn)."""
    candidates = [os.path.join(case_dir, "expect.json")]
    turns = turn_numbers(case_dir)
    if turns:
        candidates.append(os.path.join(case_dir, "turns", str(turns[-1]),
                                       "expect.json"))
    for path in candidates:
        expect = read_optional(path)
        answer = (expect or {}).get("answer")
        ref = answer.get("rubric") if isinstance(answer, dict) else None
        if isinstance(ref, str) and ref.strip():
            return nfc(ref.strip()), expect
    return None, None


def turn_numbers(case_dir):
    turns_dir = os.path.join(case_dir, "turns")
    if not os.path.isdir(turns_dir):
        return []
    return sorted(int(n) for n in os.listdir(turns_dir) if n.isdigit()
                  and os.path.isdir(os.path.join(turns_dir, n)))


def layer_rows(verdict):
    layers = verdict.get("layers")
    if not isinstance(layers, dict):
        return []
    return [(name, row.get("verdict")) for name, row in layers.items()
            if isinstance(row, dict) and isinstance(row.get("verdict"), str)]


def not_selected_because(verdict, include_failed):
    """Why a case is not judged by default (SS5), or None.

    Read on the LAYER rows, not the rollup: a layer `error` rolls a case up
    `unscored` above a sibling `fail`, and a scorer error is a number nobody
    should quote."""
    if verdict.get("verdict") == "skipped":
        return "skipped: no answer to judge"
    rows = layer_rows(verdict)
    for name, value in rows:
        if value in BLOCKING_ROWS:
            return f"layer {name} is {value}"
    if not include_failed:
        for name, value in rows:
            if value == "fail":
                return f"already failing ({name}: fail); --include-failed " \
                       "judges it"
    return None


def evidence_files(case_dir):
    """The app-side files an assignment names, relative to the case dir."""
    names = ["request.json", "answer.txt", "response.json"]
    if os.path.isfile(os.path.join(case_dir, "trajectory.json")):
        names.append("trajectory.json")
    for t in turn_numbers(case_dir):
        for name in ("request.json", "answer.txt"):
            rel = f"turns/{t}/{name}"
            if os.path.isfile(os.path.join(case_dir, rel)):
                names.append(rel)
    return [n for n in names if os.path.isfile(os.path.join(case_dir, n))]


def assignment_prompt(key, rubric_id, snap, case_dir, expect_refusal):
    files = evidence_files(case_dir)
    lines = [
        "evalup judge assignment -- the criteria form (agents/judge.md "
        "\"The criteria form\"). Grade every criterion below in this one "
        "reply: reasoning first, then ONE JSON object, last, schema "
        f"{REPLY_SCHEMA}.",
        f"case_id: {key}",
        f"rubric_id: {rubric_id}",
        f"rubric_version: {snap['version']}",
        f"expect_refusal: {'true' if expect_refusal else 'false'}",
        f"case directory: {case_dir}",
        "Read only these files in it (rule 0):",
    ]
    for name in files:
        lines.append(f"  {name}")
    lines.append("  expect.json  (the reference, for reference_guided "
                 "criteria only; never evidence)")
    lines.append("Evidence `source` is one of the files above other than "
                 "expect.json, written exactly as listed.")
    lines.append("Criteria:")
    for crit in snap["criteria"]:
        tags = [t for t in ("reference_guided", "canary_guard") if crit[t]]
        tag = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"  - {crit['id']}{tag}: {crit['question']}")
    return "\n".join(lines)


def materialise_self_test(jdir, rubric_id, snap, side):
    """judge/selftest/<rubric>/<label>/: the same file names a case has, so
    SS4's checks apply to it unchanged. The label is opaque (self_test_key),
    so neither the directory nor the case_id tells the judge which answer is
    the bad one."""
    block = snap["self_test"][side]
    d = os.path.join(jdir, "selftest", rubric_id,
                     snap["self_test_labels"][side])
    os.makedirs(d, exist_ok=True)
    key = self_test_key(rubric_id, snap, side)
    write_json(os.path.join(d, "request.json"), {
        "case_id": key,
        "body": {"messages": [{"role": "user", "content": block["user"]}]}})
    write_json(os.path.join(d, "response.json"), {
        "case_id": key, "body": block.get("response")})
    write_json(os.path.join(d, "expect.json"),
               {"result": block.get("reference")}
               if "reference" in block else {})
    with open(os.path.join(d, "answer.txt"), "w", encoding="utf-8") as fh:
        fh.write(block["answer"])
    return d


def self_test_key(rubric_id, snap, side):
    """`selftest-<rubric>-<random label>`: the good/bad mapping lives only in
    plan.json, which no assignment names (SS3: two separate calls, so the
    judge cannot grade by contrast -- nor by a label)."""
    return f"selftest-{rubric_id}-{snap['self_test_labels'][side]}"


def input_tokens(case_dir):
    total = 0
    for name in [*evidence_files(case_dir), "expect.json"]:
        path = os.path.join(case_dir, name)
        if os.path.isfile(path):
            total += os.path.getsize(path)
    return total // 4


def previous_pass(run_dir):
    """The newest other run's judged.json beside this run, for SS11's
    measured line. Run ids sort by their timestamp, never by mtime."""
    parent = os.path.dirname(os.path.abspath(run_dir))
    here = os.path.basename(os.path.abspath(run_dir))
    best = None
    try:
        names = os.listdir(parent)
    except OSError:
        return None
    for name in names:
        m = RUN_ID_RE.fullmatch(name)
        if not m or name == here:
            continue
        path = os.path.join(parent, name, "judge", "judged.json")
        if os.path.isfile(path) and (best is None or m.group(2) > best[0]):
            best = (m.group(2), path)
    if best is None:
        return None
    try:
        with open(best[1], encoding="utf-8") as fh:
            doc = loads_strict(fh.read())
    except (OSError, UnicodeDecodeError, BadJSON):
        return None
    return doc if isinstance(doc, dict) else None


def has_answer(case_dir):
    try:
        path = os.path.join(case_dir, "answer.txt")
        with open(path, encoding="utf-8") as fh:
            return bool(fh.read().strip())
    except (OSError, UnicodeDecodeError):
        return False


def cmd_plan(a):
    run_dir = os.path.abspath(a.run_dir)
    manifest = load_run(run_dir)
    jdir = judge_dir(run_dir)
    plan_path = os.path.join(jdir, "plan.json")
    replies = os.path.join(jdir, "replies")
    if a.resume:
        plan = load_plan(run_dir)
        if a.max_calls is not None and a.max_calls != plan["max_calls"]:
            die(f"--resume keeps this pass's plan, budget included "
                f"(--max-calls {plan['max_calls']}); a different cap means "
                "a different selection, so plan it with --restart")
        print_plan(plan, run_dir)
        return
    if a.max_calls is None:
        a.max_calls = DEFAULT_MAX_CALLS
    has_replies = os.path.isdir(replies) and os.listdir(replies)
    if has_replies and not a.restart:
        die(f"{jdir} already holds a judge pass with replies: --resume "
            "continues it (judging only what has no reply), --restart "
            "discards it and plans a new one")
    if a.max_calls < 1:
        die("--max-calls must be at least 1")
    rubrics = index_rubrics(load_rubrics(a.rubrics))
    # A new pass, checked arguments first: nothing of an earlier one stays
    # (with no replies, that is unrecorded inbox files and old self-test
    # directories under other labels).
    for name in ("replies", "inbox", "selftest"):
        shutil.rmtree(os.path.join(jdir, name), ignore_errors=True)
    for name in ("launches.jsonl", "judged.json", "plan.json"):
        path = os.path.join(jdir, name)
        if os.path.isfile(path):
            os.remove(path)
    mode = manifest.get("mode")
    sealed = mode in ("holdout", "full") \
        or manifest.get("selecting_split") == "holdout"
    judge = manifest.get("judge") if isinstance(manifest.get("judge"),
                                                dict) else {}
    requested = judge.get("model") if isinstance(judge.get("model"), str) \
        and judge.get("model").strip() else DEFAULT_MODEL

    cases, holdout = [], 0
    for entry in manifest.get("cases") or []:
        cid = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(cid, str) or unsafe_case_id(cid):
            continue
        if sealed or entry.get("set") == "holdout":
            holdout += 1
            continue
        case_dir = os.path.join(run_dir, "cases", cid)
        verdict = read_optional(os.path.join(case_dir, "verdict.json"))
        if verdict is None:
            continue
        if verdict.get("holdout"):
            holdout += 1
            continue
        ref, expect = rubric_ref(case_dir)
        if ref is None:
            continue
        rid, pin = split_pin(ref)
        row = {"case_id": cid, "rubric": ref, "rubric_id": rid,
               "pinned_version": pin, "canary": bool(verdict.get("canary")),
               "selected": False, "reason": None}
        cases.append(row)
        snap = rubrics.get(rid)
        reason = not_selected_because(verdict, a.include_failed)
        if reason is None and not has_answer(case_dir):
            reason = "no answer to judge"
        if reason is None and snap is None:
            reason = f"rubric {rid!r} not found in {a.rubrics}"
        if reason is None and snap["invalid"]:
            reason = f"rubric {rid!r} is invalid: {snap['invalid']}"
        if reason is None and snap["form"] != "criteria":
            reason = "DAG rubrics are not judged at run time in v1"
        row["reason"] = reason
        authz = (expect or {}).get("authz")
        row["expect_refusal"] = isinstance(authz, dict) \
            and authz.get("expect_refusal") is True

    # The budget (SS11) counts every launch. Self-tests are spent first, and
    # only for a rubric at least one of whose cases fits under the cap, so a
    # self-test is never paid for a rubric nothing will be judged on.
    budget_reason = f"over the judge budget: --max-calls {a.max_calls}"
    planned, used_rubrics = 0, []
    for row in cases:
        if row["reason"] is not None:
            continue
        cost = 1 if row["rubric_id"] in used_rubrics else 3
        if planned + cost > a.max_calls:
            row["reason"] = budget_reason
            continue
        planned += cost
        if row["rubric_id"] not in used_rubrics:
            used_rubrics.append(row["rubric_id"])
        row["selected"] = True

    os.makedirs(jdir, exist_ok=True)
    launches = []
    for rid in used_rubrics:
        snap = rubrics[rid]
        # Drawn in order, never sorted (a sort made `good` the smaller
        # label), and never spelling a word a judge could read as a hint.
        labels = []
        while len(labels) < len(SIDES):
            label = secrets.token_hex(3)
            if label not in labels and not any(
                    w in label for w in ("bad", "good", "a11", "f00")):
                labels.append(label)
        snap["self_test_labels"] = dict(zip(SIDES, labels))
        for side in SIDES:
            d = materialise_self_test(jdir, rid, snap, side)
            key = self_test_key(rid, snap, side)
            launches.append({
                "kind": "self_test", "key": key, "rubric_id": rid,
                "side": side, "dir": d,
                "prompt": assignment_prompt(key, rid, snap, d, False),
                "input_tokens": input_tokens(d)})
    for row in cases:
        if not row["selected"]:
            continue
        d = os.path.join(run_dir, "cases", row["case_id"])
        launches.append({
            "kind": "case", "key": row["case_id"],
            "rubric_id": row["rubric_id"], "dir": d,
            "prompt": assignment_prompt(row["case_id"], row["rubric_id"],
                                        rubrics[row["rubric_id"]], d,
                                        row["expect_refusal"]),
            "input_tokens": input_tokens(d)})
    for launch in launches:
        launch["reply_file"] = os.path.join(jdir, "inbox",
                                            launch["key"] + ".txt")
        flag = (["--self-test", f"{launch['rubric_id']}:{launch['side']}"]
                if launch["kind"] == "self_test" else
                ["--case", launch["key"]])
        launch["record_args"] = ["record", run_dir, *flag, "--reply-file",
                                 launch["reply_file"]]

    lo = sum(LAUNCH_TOKENS[0] + x["input_tokens"] + OUTPUT_TOKENS
             for x in launches)
    hi = sum(LAUNCH_TOKENS[1] + x["input_tokens"] + OUTPUT_TOKENS
             for x in launches)
    estimate = {"calls": len(launches),
                "self_test_calls": 2 * len(used_rubrics),
                "case_calls": len(launches) - 2 * len(used_rubrics),
                "tokens_low": lo, "tokens_high": hi,
                "measured": None}
    prev = previous_pass(run_dir)
    if prev and isinstance(prev.get("cost"), dict):
        cost = prev["cost"]
        calls = cost.get("calls")
        if isinstance(calls, int) and calls > 0 \
                and isinstance(cost.get("wall_s"), (int, float)):
            estimate["measured"] = {
                "run_id": prev.get("run_id"),
                "wall_s_per_call": round(cost["wall_s"] / calls, 1),
                "tokens_per_call": (round(cost["tokens"] / calls)
                                    if isinstance(cost.get("tokens"), int)
                                    else None)}

    for snap in rubrics.values():
        if snap.get("invalid") is None:
            snap.pop("invalid", None)
    plan = {"schema": PLAN_SCHEMA, "judge_version": JUDGE_VERSION,
            "harness_version": HARNESS_VERSION,
            "run_id": manifest.get("run_id"), "run_dir": run_dir,
            "created_at": stamp(now()), "requested_model": requested,
            "max_calls": a.max_calls, "include_failed": a.include_failed,
            "rubrics_source": a.rubrics,
            "rubrics": {rid: rubrics[rid] for rid in sorted(
                {row["rubric_id"] for row in cases} & set(rubrics))},
            "cases": cases, "holdout_not_judged": holdout,
            "launches": launches, "estimate": estimate}
    write_json(plan_path, plan)
    print_plan(plan, run_dir)


def print_plan(plan, run_dir):
    cases = plan["cases"]
    selected = [c for c in cases if c["selected"]]
    reasons = {}
    for c in cases:
        if not c["selected"]:
            key = c["reason"].split(":")[0].split(" (")[0]
            reasons[key] = reasons.get(key, 0) + 1
    est = plan["estimate"]
    done = replied(run_dir)
    pending = [x for x in plan["launches"] if x["key"] not in done]
    lines = []
    if cases:
        n = len(cases)
        why = ", ".join(f"{v} {k}" for k, v in sorted(reasons.items()))
        lines.append(f"judge pass: {n} case(s) pin a rubric; "
                     f"{len(selected)} selected"
                     + (f" ({n - len(selected)} not: {why})" if why else "")
                     + (f"; {plan['holdout_not_judged']} sealed holdout "
                        "case(s) not read" if plan["holdout_not_judged"]
                        else ""))
        lines.append(f"  calls: {est['case_calls']} cases + "
                     f"{est['self_test_calls']} self-test "
                     f"({est['self_test_calls'] // 2} rubric(s)) = "
                     f"{est['calls']}; budget --max-calls "
                     f"{plan['max_calls']}")
        if est["calls"]:
            lines.append(
                f"  tokens: ~{est['tokens_low'] // 1000}k-"
                f"{est['tokens_high'] // 1000}k ({LAUNCH_TOKENS[0] // 1000}-"
                f"{LAUNCH_TOKENS[1] // 1000}k per launch, measured in the "
                "2026-09 user tests, + the named files' bytes / 4 + ~1k "
                "output per call)")
        m = est.get("measured")
        lines.append(
            f"  time: ~{m['wall_s_per_call']} s per call (measured on "
            f"{m['run_id']})" if m else
            "  time: not yet measured (the last pass's judged.json "
            "records it)")
    else:
        lines.append("judge pass: no case in this run pins a rubric")
    if plan["holdout_not_judged"] and not cases:
        lines[-1] = ("judge pass: nothing to judge -- "
                     f"{plan['holdout_not_judged']} sealed holdout case(s) "
                     "are never judged, and their rubric pins are not read")
    print(json.dumps({
        "plan": os.path.join(judge_dir(run_dir), "plan.json"),
        "summary": lines,
        "pending": [{"key": x["key"], "kind": x["kind"],
                     "rubric_id": x["rubric_id"], "side": x.get("side"),
                     "reply_file": x["reply_file"],
                     "record_args": x["record_args"],
                     "prompt": x["prompt"]} for x in pending],
    }, indent=2, ensure_ascii=False))


def replied(run_dir):
    d = os.path.join(judge_dir(run_dir), "replies")
    if not os.path.isdir(d):
        return set()
    return {n[:-5] for n in os.listdir(d) if n.endswith(".json")}


# -- record -----------------------------------------------------------------

def last_json_object(text):
    """The last top-level `{...}` in a message that parses strictly, or None.

    Once an object parses, the scan resumes after it, so an object nested
    inside a parsed one is never a candidate. A `{` that does not parse
    (prose, a NaN, a truncated reply) is skipped by one character. That
    means a whole reply wrapped in a broken outer object is still found --
    the judge's own reply, which must then pass every check of SS4. A
    balanced-extent skip was tried and lost real replies whose prose held an
    odd quote mark (the second review of #23's build), so it is not used."""
    decoder = json.JSONDecoder(parse_constant=_reject)
    found, i = None, 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return found
        try:
            obj, end = decoder.raw_decode(text, i)
        except (ValueError, RecursionError):
            i += 1
            continue
        if isinstance(obj, dict):
            found = obj
        i = end


def _reject(name):
    raise ValueError(f"{name} is not JSON")


def normalise(text):
    return " ".join(nfc(text).split())


def haystacks(path):
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except (OSError, UnicodeDecodeError):
        return []
    out = [normalise(raw)]
    if path.endswith(".json"):
        with contextlib.suppress(BadJSON):
            out.append(normalise(json.dumps(loads_strict(raw),
                                            ensure_ascii=False)))
    return out


def quote_found(quote, stacks):
    """Whitespace-collapsed, NFC, and a `[REDACTED <kind>]` piece (judge rule
    3) matches when the pieces around it appear in order, each redaction
    standing for at most MAX_REDACTED_SPAN characters of the file -- a
    secret (a PEM key included), not a stretch of it. The quote's unredacted
    text holds at least MIN_QUOTE_CHARS letters or digits, so `e` is not
    evidence; a piece with none (the `.` after a redaction) is not matched.

    Linear in the file per piece: the reachable end positions are carried
    forward as a sorted list, never by backtracking (a quote over a long
    `-----` rule took minutes that way)."""
    pieces = [p for p in (normalise(x) for x in REDACTED.split(quote)) if p]
    if sum(ch.isalnum() for p in pieces for ch in p) < MIN_QUOTE_CHARS:
        return False
    pieces = [p for p in pieces if any(ch.isalnum() for ch in p)]
    for hay in stacks:
        ends = sorted(m + len(pieces[0]) for m in occurrences(hay, pieces[0]))
        for piece in pieces[1:]:
            if not ends:
                break
            nxt = []
            for start in occurrences(hay, piece):
                k = bisect.bisect_right(ends, start)
                if k and start - ends[k - 1] <= MAX_REDACTED_SPAN:
                    nxt.append(start + len(piece))
            ends = nxt
        if ends:
            return True
    return False


def occurrences(hay, piece):
    at = hay.find(piece)
    while at >= 0:
        yield at
        at = hay.find(piece, at + 1)


def check_criterion(crit, case_dir, allowed):
    """(verdict, reason): a pass/fail survives only if every check holds."""
    verdict = crit.get("verdict")
    if verdict not in VERDICTS:
        return "unknown", f"verdict {verdict!r} is not pass, fail or unknown"
    if verdict == "unknown":
        return "unknown", None
    evidence = crit.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return "unknown", f"a {verdict} with no quoted evidence"
    for item in evidence:
        if not isinstance(item, dict):
            return "unknown", "an evidence item is not an object"
        source, quote = item.get("source"), item.get("quote")
        if not isinstance(source, str) or not isinstance(quote, str) \
                or not quote.strip():
            return "unknown", "an evidence item lacks a source or a quote"
        source = source.strip()
        if source.startswith("./"):
            source = source[2:]
        if os.path.basename(source) == "expect.json":
            return "unknown", ("evidence cites expect.json, the reference, "
                               "not the app")
        if source not in allowed:
            return "unknown", f"evidence source {source!r} is not a file " \
                              "the assignment named"
        if not quote_found(quote, haystacks(os.path.join(case_dir, source))):
            return "unknown", f"quote not found in {source}"
    return verdict, None


def check_reply(obj, launch, snap):
    """(status, reason, model_id, criteria): SS4's whole-reply checks first,
    then each criterion's."""
    if obj is None:
        return "no structured output", "no JSON object in the message", \
            None, []
    model_id = obj.get("model_id")
    model_id = model_id if isinstance(model_id, str) else None
    want = [c["id"] for c in snap["criteria"]]
    crits = obj.get("criteria")
    mismatch = None
    if obj.get("schema") != REPLY_SCHEMA:
        mismatch = "schema"
    elif obj.get("case_id") != launch["key"]:
        mismatch = "case_id"
    elif obj.get("rubric_id") != launch["rubric_id"]:
        mismatch = "rubric_id"
    elif obj.get("rubric_version") != snap["version"] \
            or not isinstance(obj.get("rubric_version"), int) \
            or isinstance(obj.get("rubric_version"), bool):
        mismatch = "rubric_version"
    elif not isinstance(crits, list) \
            or not all(isinstance(c, dict) for c in crits):
        mismatch = "criteria"
    else:
        got = [c.get("id") for c in crits]
        if sorted(map(str, got)) != sorted(want) or len(got) != len(want):
            mismatch = "criterion ids"
    if mismatch:
        return "unmatched", ("reply did not match the assignment "
                             f"({mismatch})"), model_id, []
    allowed = set(evidence_files(launch["dir"]))
    out = []
    for crit in crits:
        verdict, reason = check_criterion(crit, launch["dir"], allowed)
        row = {"id": crit["id"], "verdict": verdict,
               "judge_verdict": crit.get("verdict"),
               "reasoning": crit.get("reasoning")
               if isinstance(crit.get("reasoning"), str) else None,
               "evidence": crit.get("evidence")
               if isinstance(crit.get("evidence"), list) else []}
        if reason:
            row["check_failed"] = reason
        out.append(row)
    order = {cid: i for i, cid in enumerate(want)}
    out.sort(key=lambda r: order[r["id"]])
    return "ok", None, model_id, out


def self_test_side_ok(record, snap, side):
    """(ok, reason) for one side of a rubric's self-test (SS3)."""
    if record is None:
        return False, f"no reply recorded for the {side} answer"
    if record["status"] != "ok":
        return False, f"{record['reason']} on the {side} answer"
    got = {c["id"]: c["verdict"] for c in record["criteria"]}
    if side == "good":
        need = {c["id"]: "pass" for c in snap["criteria"]}
    else:
        need = {cid: "fail" for cid in snap["self_test"]["bad"]["fails"]}
    for cid, want in need.items():
        if got.get(cid) != want:
            return False, f"{cid} graded {got.get(cid)} on the {side} answer"
    return True, None


def launches_used(jdir):
    path = os.path.join(jdir, "launches.jsonl")
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    rows.append({})
    return rows


def cmd_record(a):
    run_dir = os.path.abspath(a.run_dir)
    plan = load_plan(run_dir)
    jdir = judge_dir(run_dir)
    if bool(a.case) == bool(a.self_test):
        die("record takes exactly one of --case ID or --self-test "
            "RUBRIC:good|bad")
    if a.self_test:
        rid, _, side = a.self_test.rpartition(":")
        if side not in SIDES or not rid:
            die(f"--self-test {a.self_test!r}: expected RUBRIC:good or "
                "RUBRIC:bad")
        snap = plan["rubrics"].get(rid) or {}
        if "self_test_labels" not in snap:
            die(f"--self-test {a.self_test!r}: rubric {rid!r} has no "
                "self-test in this pass's plan")
        key = self_test_key(rid, snap, side)
    else:
        key = a.case
    launch = next((x for x in plan["launches"] if x["key"] == key), None)
    if launch is None:
        die(f"{key!r} is not a launch in this pass's plan "
            f"({os.path.join(jdir, 'plan.json')})")
    used = launches_used(jdir)
    if len(used) >= plan["max_calls"]:
        die(f"over the judge budget: --max-calls {plan['max_calls']} "
            f"launches already recorded; {key} is not recorded (SS11: past "
            "the cap nothing is retried)")
    try:
        with open(a.reply_file, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        die(f"bad input: --reply-file {a.reply_file}: {exc}")
    snap = plan["rubrics"][launch["rubric_id"]]
    status, reason, model_id, criteria = check_reply(
        last_json_object(text), launch, snap)
    if a.tokens is not None and a.tokens < 0:
        die("--tokens must be >= 0")
    ledger = os.path.join(jdir, "launches.jsonl")
    with open(ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": key, "at": stamp(now()),
                             "tokens": a.tokens}) + "\n")
    record = {"schema": RECORD_SCHEMA, "key": key, "kind": launch["kind"],
              "rubric_id": launch["rubric_id"],
              "rubric_sha256": snap["sha256"], "recorded_at": stamp(now()),
              "reply_file": os.path.abspath(a.reply_file),
              "status": status, "reason": reason, "model_id": model_id,
              "criteria": criteria}
    write_json(os.path.join(jdir, "replies", key + ".json"), record)
    out = {"recorded": key, "status": status, "reason": reason,
           "criteria": {c["id"]: c["verdict"] for c in criteria},
           "calls_used": len(used) + 1,
           "calls_left": plan["max_calls"] - len(used) - 1}
    if launch["kind"] == "self_test":
        rid = launch["rubric_id"]
        sides = {}
        for side in SIDES:
            rec = read_optional(os.path.join(
                jdir, "replies", self_test_key(rid, snap, side) + ".json"))
            sides[side] = self_test_side_ok(rec, snap, side) \
                if rec is not None else None
        if all(v is not None for v in sides.values()):
            bad = [r for ok, r in sides.values() if not ok]
            out["self_test"] = "fail" if bad else "pass"
            if bad:
                out["self_test_reason"] = bad[0]
                out["next"] = (f"self-test failed: launch no case on "
                               f"{rid}; its cases stay unjudged")
            else:
                out["next"] = f"self-test passed: judge the cases on {rid}"
        else:
            out["self_test"] = "pending"
    print(json.dumps(out, indent=2, ensure_ascii=False))


# -- merge ------------------------------------------------------------------

def same_family(manifest):
    """yes / no / unknown, from the app's declared model (SS12)."""
    app = manifest.get("app") if isinstance(manifest.get("app"), dict) else {}
    model = app.get("model")
    leaves = []

    def walk(value):
        if isinstance(value, str):
            if value.strip():
                leaves.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(model)
    if not leaves:
        return "unknown"
    return "yes" if any("claude" in v.casefold() for v in leaves) else "no"


def load_calibration(path):
    """(sidecar, sha256, reason): the sidecar if it can back anything."""
    if not path:
        return None, None, "no calibration sidecar"
    try:
        raw = pathlib.Path(path).read_bytes()
    except OSError:
        return None, None, f"no calibration sidecar at {path}"
    sha = hashlib.sha256(raw).hexdigest()
    try:
        doc = loads_strict(raw.decode("utf-8"))
    except (BadJSON, UnicodeDecodeError) as exc:
        return None, sha, f"calibration sidecar unreadable: {exc}"
    if not isinstance(doc, dict) or doc.get("schema") != CALIBRATION_V2:
        schema = doc.get("schema") if isinstance(doc, dict) else None
        return None, sha, (f"calibration sidecar is {schema!r}, not "
                           f"{CALIBRATION_V2!r} (a /1 sidecar has no rubric "
                           "version or model id)")
    return doc, sha, None


def calibration_group(sidecar, rid, version, model_id):
    for group in sidecar.get("groups") or []:
        if not isinstance(group, dict):
            continue
        if group.get("rubric_id") == rid \
                and group.get("rubric_version") == version \
                and not isinstance(group.get("rubric_version"), bool) \
                and group.get("judge_model_id") == model_id:
            return group
    return None


def fold(criteria):
    """SS1: any checked fail -> fail; every criterion a checked pass -> pass;
    otherwise unjudged, naming what came back unknown."""
    if any(c["verdict"] == "fail" for c in criteria):
        return "fail", None
    if criteria and all(c["verdict"] == "pass" for c in criteria):
        return "pass", None
    names = [c["id"] + (f" ({c['check_failed']})" if c.get("check_failed")
                        else "")
             for c in criteria if c["verdict"] != "pass"]
    return None, "unknown on " + ", ".join(names)


def cmd_merge(a):
    run_dir = os.path.abspath(a.run_dir)
    manifest = load_run(run_dir)
    plan = load_plan(run_dir)
    jdir = judge_dir(run_dir)
    records = {}
    for launch in plan["launches"]:
        rec = read_optional(os.path.join(jdir, "replies",
                                         launch["key"] + ".json"))
        if rec is not None and rec.get("schema") == RECORD_SCHEMA:
            records[launch["key"]] = rec
    used = launches_used(jdir)
    over_budget = len(used) >= plan["max_calls"]

    # SS7: one self-reported id across every reply that parsed.
    # A reply with no JSON object reported no id and is unjudged on its own.
    ids = [rec.get("model_id") for rec in records.values()
           if rec["status"] != "no structured output"]
    pass_reason = None
    resolved = None
    if any(not isinstance(i, str) or not MODEL_ID_RE.fullmatch(i)
           for i in ids):
        pass_reason = "judge model unresolved"
    elif len(set(ids)) > 1:
        pass_reason = "judge model changed during the pass"
    elif ids:
        resolved = ids[0]

    rubrics_out, self_tests = {}, {}
    for rid, snap in plan["rubrics"].items():
        rubrics_out[rid] = {"version": snap["version"],
                            "sha256": snap["sha256"], "form": snap["form"],
                            "file": snap["file"]}
        if snap["form"] != "criteria" or "criteria" not in snap:
            continue
        if not any(x["rubric_id"] == rid for x in plan["launches"]):
            continue
        result, reason = "pass", None
        for side in SIDES:
            rec = records.get(self_test_key(rid, snap, side))
            if rec is not None and rec.get("rubric_sha256") != snap["sha256"]:
                rec = None
            ok, why = self_test_side_ok(rec, snap, side)
            if not ok:
                result, reason = "fail", why
                break
        self_tests[rid] = (result, reason)
        rubrics_out[rid]["self_test"] = result
        if reason:
            rubrics_out[rid]["self_test_reason"] = reason

    sidecar, sidecar_sha, sidecar_reason = load_calibration(a.calibration)
    family = same_family(manifest)
    judge_block = manifest.get("judge") if isinstance(manifest.get("judge"),
                                                      dict) else {}
    accepted = judge_block.get("same_family_accepted") is True

    out_cases = []
    for row in plan["cases"]:
        rid = row["rubric_id"]
        snap = plan["rubrics"].get(rid)
        case = {"case_id": row["case_id"], "rubric_id": rid,
                "pinned_version": row["pinned_version"],
                "graded_version": snap["version"] if snap else None,
                "canary": row["canary"], "state": "unjudged",
                "verdict": None, "reason": None, "criteria": []}
        out_cases.append(case)
        if not row["selected"]:
            case["reason"] = row["reason"]
            continue
        rec = records.get(row["case_id"])
        if pass_reason:
            case["reason"] = pass_reason
        elif rec is None and a.launch_denied:
            # SS10: a denied launch ends the pass; that, not the self-test
            # it may have stopped, is why every case without a reply is here.
            case["reason"] = "judge launch denied"
        elif self_tests.get(rid, ("fail",))[0] != "pass":
            why = self_tests.get(rid, ("fail", "no self-test ran"))[1]
            case["reason"] = f"self-test failed: {why}"
        elif rec is None:
            case["reason"] = (
                f"over the judge budget: --max-calls {plan['max_calls']}"
                if over_budget else "no reply recorded")
        elif rec.get("rubric_sha256") != snap["sha256"]:
            case["reason"] = "reply judged a different rubric file"
        elif rec["status"] != "ok":
            case["reason"] = rec["reason"] if rec["status"] == "unmatched" \
                else rec["status"]
        else:
            case["criteria"] = rec["criteria"]
            verdict, reason = fold(rec["criteria"])
            if verdict is None:
                case["reason"] = reason
            else:
                case["verdict"] = verdict
                case["state"] = "provisional"

    # SS9: a judge canary judged `fail` voids its rubric's judged verdicts.
    for canary in [c for c in out_cases if c["canary"]
                   and c["verdict"] == "fail"]:
        for case in out_cases:
            if case["rubric_id"] == canary["rubric_id"] \
                    and case["state"] != "unjudged":
                case["state"], case["reason"] = "unjudged", \
                    f"judge canary {canary['case_id']} failed"
                case["voided_verdict"], case["verdict"] = case["verdict"], None

    for case in out_cases:
        if case["state"] != "provisional":
            continue
        rid, version = case["rubric_id"], case["graded_version"]
        why = sidecar_reason
        group = None
        if why is None:
            group = calibration_group(sidecar, rid, version, resolved)
            if group is None or group.get("calibrated") is not True:
                why = (f"no calibration for {rid} v{version} on {resolved}")
        if why is None and case["pinned_version"] != version:
            why = (f"stale pin: the case pins v{case['pinned_version']}, "
                   f"the file is v{version}"
                   if case["pinned_version"] is not None else
                   f"the case pins no version (write {rid}-v{version})")
        if why is None and self_tests.get(rid, ("fail",))[0] != "pass":
            why = "self-test did not pass in this pass"
        if why is None and family == "unknown":
            why = "app model undeclared"
        if why is None and family == "yes" and not accepted:
            why = ("same-family judge, and profile.yaml's "
                   "judge.same_family_accepted is not true")
        if why is None:
            case["state"] = "calibrated"
            case["calibration"] = {k: group.get(k) for k in
                                   ("tpr", "tnr", "kappa", "labelled_pairs")}
        else:
            case["not_calibrated_because"] = why

    def count(state, verdict=None, canary=False):
        # SS9: canaries are counted apart, never in the cases' counts.
        return sum(1 for c in out_cases if c["state"] == state
                   and c["canary"] is canary
                   and (verdict is None or c["verdict"] == verdict))
    tokens = [u.get("tokens") for u in used]
    started = parse_stamp(plan.get("created_at"))
    last = max((parse_stamp(u.get("at")) for u in used
                if parse_stamp(u.get("at"))), default=None)
    judged = {
        "schema": JUDGED_SCHEMA, "judge_version": JUDGE_VERSION,
        "harness_version": HARNESS_VERSION,
        "run_id": manifest.get("run_id"),
        "run": {key: manifest.get(key) for key in
                ("mode", "scoring_version", "harness_version",
                 "dataset_version")},
        "judge": {"requested_model": plan["requested_model"],
                  "resolved_model_id": resolved,
                  "same_family": family},
        "rubrics": rubrics_out,
        "calibration": ({"sidecar": a.calibration, "sha256": sidecar_sha}
                        if sidecar_sha else None),
        "cases": out_cases,
        "summary": {
            "selected": sum(1 for r in plan["cases"] if r["selected"]
                            and not r["canary"]),
            "unjudged": count("unjudged"),
            "provisional": count("provisional"),
            "calibrated": count("calibrated"),
            "by_verdict": {s: {"pass": count(s, "pass"),
                               "fail": count(s, "fail")}
                           for s in ("provisional", "calibrated")},
            "holdout_not_judged": plan["holdout_not_judged"],
            "canaries": {
                "unjudged": count("unjudged", canary=True),
                "provisional": count("provisional", canary=True),
                "calibrated": count("calibrated", canary=True),
                "fail": sum(1 for c in out_cases if c["canary"] and (
                    c["verdict"] == "fail"
                    or c.get("voided_verdict") == "fail"))}},
        "cost": {"calls": len(used),
                 "wall_s": (round((last - started).total_seconds(), 1)
                            if started and last else None),
                 "tokens": (sum(tokens) if tokens and all(
                     isinstance(t, int) for t in tokens) else None)},
        "notes": ([SAME_FAMILY_NOTE] if family != "no" else []),
        "wall": ("No judged verdict here is in a pass rate, a gate, "
                 "stats.py or an optimize objective (docs/judge-run.md SS8)."),
    }
    if family == "unknown":
        judged["notes"].append("the app's model is undeclared in the "
                               "manifest, so same-family is unknown")
    if pass_reason:
        judged["judge"]["unresolved_because"] = pass_reason
    write_json(os.path.join(jdir, "judged.json"), judged)
    print(json.dumps({"judged": os.path.join(jdir, "judged.json"),
                      "judge": judged["judge"],
                      "summary": judged["summary"],
                      "notes": judged["notes"]}, indent=2,
                     ensure_ascii=False))


# -- diff -------------------------------------------------------------------

def cmd_diff(a):
    docs = []
    for path in (a.a, a.b):
        doc = load_json(path)
        if not isinstance(doc, dict) or doc.get("schema") != JUDGED_SCHEMA:
            die(f"{path}: not an {JUDGED_SCHEMA} file")
        docs.append(doc)
    left, right = docs
    field = None
    if left.get("judge_version") != right.get("judge_version"):
        field = "judge_version"
    elif (left.get("judge") or {}).get("resolved_model_id") != \
            (right.get("judge") or {}).get("resolved_model_id"):
        field = "resolved_model_id"
    elif judge_key(left) != judge_key(right):
        lr, rr = left.get("rubrics") or {}, right.get("rubrics") or {}
        field = "rubrics"
        for rid in sorted(set(lr) | set(rr)):
            if rid not in lr or rid not in rr:
                field = f"rubrics.{rid}"
                break
            for key in ("version", "sha256"):
                if lr[rid].get(key) != rr[rid].get(key):
                    field = f"rubrics.{rid}.{key}"
                    break
            if field != "rubrics":
                break
    elif scoring_key(left.get("run") or {}) != \
            scoring_key(right.get("run") or {}):
        field = "scoring_version"
    elif (left.get("run") or {}).get("dataset_version") != \
            (right.get("run") or {}).get("dataset_version"):
        field = "dataset_version"
    if field:
        die(f"{field} differs between {a.a} and {a.b}: the two judge passes "
            "are not comparable (docs/judge-run.md SS7)")

    def by_case(doc):
        return {c["case_id"]: c for c in doc.get("cases") or []
                if isinstance(c, dict)}
    lc, rc = by_case(left), by_case(right)
    changed = []
    for cid in sorted(set(lc) | set(rc)):
        lv = (lc.get(cid, {}).get("state"), lc.get(cid, {}).get("verdict"))
        rv = (rc.get(cid, {}).get("state"), rc.get(cid, {}).get("verdict"))
        if lv != rv:
            changed.append({"case_id": cid,
                            "a": {"state": lv[0], "verdict": lv[1]},
                            "b": {"state": rv[0], "verdict": rv[1]}})

    def counts(doc):
        s = doc.get("summary") or {}
        return {k: s.get(k) for k in ("unjudged", "provisional",
                                      "calibrated", "by_verdict")}
    print(json.dumps({"a": {"run_id": left.get("run_id"),
                            "counts": counts(left)},
                      "b": {"run_id": right.get("run_id"),
                            "counts": counts(right)},
                      "changed": changed,
                      "note": "counts only: a judged diff is never a keep "
                              "and never a p-value"}, indent=2))


def main():
    ap = argparse.ArgumentParser(
        description="The run-time judge pass (docs/judge-run.md): plan the "
                    "judge launches for a finished run, record and check "
                    "each reply, merge them into judge/judged.json, and diff "
                    "two passes. Never touches a file the runner, gate.py "
                    "or stats.py reads.")
    add_version_flag(ap)
    sub = ap.add_subparsers(dest="command")
    p = sub.add_parser("plan", help="select cases, snapshot rubrics, "
                                    "estimate, write judge/plan.json")
    p.add_argument("run_dir")
    p.add_argument("--rubrics", required=True,
                   help="<state>/rubrics, or a JSON file convert_suite.py "
                        "wrote (rubrics.json, or a converted.json)")
    p.add_argument("--include-failed", action="store_true",
                   help="also judge cases with a `fail` layer row (when "
                        "labelling); never a holdout case")
    p.add_argument("--max-calls", type=int, default=None,
                   help="cap on launches, self-tests and retries included "
                        f"(default {DEFAULT_MAX_CALLS})")
    p.add_argument("--resume", action="store_true",
                   help="keep the existing plan and list only the launches "
                        "with no reply")
    p.add_argument("--restart", action="store_true",
                   help="discard an earlier pass on this run and plan anew")
    r = sub.add_parser("record", help="check one judge reply and store it")
    r.add_argument("run_dir")
    r.add_argument("--case")
    r.add_argument("--self-test", metavar="RUBRIC:good|bad")
    r.add_argument("--reply-file", required=True,
                   help="the judge's message, verbatim")
    r.add_argument("--tokens", type=int,
                   help="the launch's reported total tokens, when known")
    m = sub.add_parser("merge", help="fold the replies into judge/judged.json")
    m.add_argument("run_dir")
    m.add_argument("--calibration",
                   help="the calibration sidecar (<state>/judge/"
                        "calibration.json), when it exists")
    m.add_argument("--launch-denied", action="store_true",
                   help="the judge launch was denied: cases with no reply "
                        "say so")
    d = sub.add_parser("diff", help="compare two judged.json files")
    d.add_argument("a")
    d.add_argument("b")
    a = ap.parse_args()
    if a.command is None:
        ap.print_help()
        sys.exit(2)
    if a.command == "plan" and a.resume and a.restart:
        die("--resume and --restart are opposites: pick one")
    {"plan": cmd_plan, "record": cmd_record, "merge": cmd_merge,
     "diff": cmd_diff}[a.command](a)


if __name__ == "__main__":
    main()
