#!/usr/bin/env python3
"""Execution engine for evalup: run one resolved plan against the app.

This is the implementation of ${CLAUDE_PLUGIN_ROOT}/docs/runner-contract.md,
which is the spec. Read that first; this module implements it and does not
re-argue it. Section markers below (SS2, SS4, ...) point at it.

WHY THIS EXISTS. run/SKILL.md SS2 "Execute" used to be hand-orchestrated by the
LLM on every run: not reproducible, expensive, and it silently dropped required
outputs. The proof was a real run from July 2026 that looked finished and had
no verdicts.jsonl and no verdicts_for_stats.jsonl -- so stats.py had nothing to
pair and that run could never be a baseline. The mechanism that makes
that unreachable is SS9: both jsonl files are DERIVED from the completed case
directories after every case, and a required-artifact table is checked before
the terminal status is written. "The run finished" and "the run's required
outputs exist" are the same statement here, or the exit code is 6.

TWO CONSTRAINTS, both load-bearing:

1. This module SHELLS OUT to the existing scorers and reimplements no scoring.
   The scorers are the definition of what a number means; a second
   implementation here would be a second, divergent definition.
2. Stdlib only, Python 3.9 floor. urllib.request rather than requests, no yaml.
   The case list therefore arrives as JSON (SS2) on validate_cases.py's own
   argument: the stdlib has no YAML parser, a hand-rolled subset parser
   misparses rather than failing loudly, and a runner whose model of a case
   disagrees with the linter's is worse than no runner. The calling skill
   converts YAML -> JSON with the parser it already uses, and that conversion
   IS the run's parse, not a second opinion.

THE THREE NON-VERDICTS (contract SS5) are the load-bearing part of the scoring
half, and they are what this module spends most of its care on:

  n/a        - not applicable: the case does not carry the layer's trigger.
  unscorable - applicable, but the capability matrix disabled the layer.
  unscored   - applicable and enabled, but the input could not be produced.

None of the three is ever `pass` and none is ever `fail`. A refusal, a
transport failure and an unread result are all in that space too: scoring a
case the harness declined to run, or one the app never answered, would
manufacture a failure the app never had -- the one outcome this harness must
never produce. Anywhere the input is not observable, the runner says so and
names the DECLARATION that would make it observable, rather than inferring.

Usage:
  run_cases.py --plan <plan.json|-> --out <state>/reports/<run-id> [--resume]
  run_cases.py --plan <plan.json|-> --out <dir> --dry-run
  run_cases.py --plan <plan.json|-> --out <dir> --baseline-verdicts <prev>.jsonl
  run_cases.py --verify <reports/<run-id>>
"""
import argparse
import contextlib
import email.message
import hashlib
import http.client
import http.cookiejar
import importlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from _common import (
    HARNESS_VERSION,
    MATRIX_KEY_OF_LAYER,
    REGEX_TIMEOUT_S,
    RUN_ID_RE,
    BadJSON,
    RegexTimeout,
    add_version_flag,
    conversation_turns,
    infra_rate,
    loads_strict,
    nfc,
    run_bounded,
    unsafe_case_id,
)

RUNNER_VERSION = 1
PLAN_VERSION = 1

# SS11. Distinct codes rather than 0/1/2 because 6 is the one that makes the
# shipped run's failure mode nameable, and once 6 exists the others cost
# nothing to distinguish. A RED SUITE EXITS 0: gating is run/SKILL.md SS6's
# separate tokenless shell step, and a runner that exited non-zero on failures
# would quietly move the gate in here (contract SS7).
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INPUT = 2
EXIT_PREFLIGHT = 3
EXIT_CANARY = 4
EXIT_INFRA = 5
EXIT_INCOMPLETE = 6
EXIT_SCORER = 7

# SS2: plan validation checks these exist under paths.scripts_dir before any
# spend. A run that discovers a missing scorer after 200 app calls has already
# paid for the whole suite.
REQUIRED_SCORERS = (
    "detect_loops.py", "normalize_trace.py", "reduce_repeats.py",
    "score_answer.py", "score_args.py", "score_authz.py",
    "score_execution.py", "score_routing.py", "trajectory_match.py",
)

# SS9(b). The declared required-artifact table. Two tests assert it against
# the contract itself -- one execs SS9(b)'s python block out of the file, the
# other parses SS6's tree and diffs the `# only when:` strings against
# REQUIRED_IF's values. A file added to the tree without a check here is how
# the shipped run lost verdicts.jsonl.
REQUIRED_ALWAYS = ("manifest.yaml", "results.json", "verdicts.jsonl",
                   "verdicts_for_stats.jsonl")
REQUIRED_PER_CASE = ("request.json", "response.json", "verdict.json",
                     "expect.json", "answer.txt")
REQUIRED_IF = {
    "routing_results.jsonl":
        "any non-canary single-turn case is routing-scorable",
    "routing_report.json": "routing_results.jsonl exists",
    "repeats.jsonl": "k > 1",
    "reliability.json": "k > 1",
    "comparison.json": "--baseline-verdicts was passed",
    "trajectory.json": "a trace was collected for this case",
    "actual.json": "expect.result is present and the case ran",
    "trace.json": "a trace was collected for this case",
    # docs/multi-turn.md SS7: a conversation's turns, beside the case-level
    # files (which hold its deciding turn). Keyed by PATH PATTERN, `*` the
    # turn number, since a bare filename cannot carry a turn's condition.
    "turns/*/request.json": "the case is multi-turn",
    "turns/*/response.json": "the case is multi-turn",
    "turns/*/verdict.json": "the case is multi-turn",
    "turns/*/expect.json": "the case is multi-turn",
    "turns/*/answer.txt": "the case is multi-turn",
    "turns/*/trajectory.json": "a trace was collected for this turn",
    "turns/*/trace.json": "a trace was collected for this turn",
}
# The three per-case conditions above, as predicates over a case DIRECTORY --
# never over the plan, which is what lets --verify evaluate them months later
# with no plan in hand. The turns/ entries are checked by check_turn_tree.
REQUIRED_IF_PER_CASE = ("trajectory.json", "actual.json", "trace.json")
# The five files every sent turn has, case level and under repeats/<n>/.
TURN_FILES = ("request.json", "response.json", "verdict.json", "expect.json",
              "answer.txt")
TURN_TRACE_FILES = ("trajectory.json", "trace.json")

TOP_LEVEL_KEYS = (
    "plan_version", "run_id", "mode", "k", "gate", "selecting_split", "paths",
    "adapter", "capability_matrix", "cases", "scoring", "execution",
    "manifest_extra",
)
# capability_matrix is required for the same reason validate_cases.py made
# --capabilities required: absent, every layer counts as enabled, and the run
# records a matrix nobody chose.
REQUIRED_KEYS = (
    "plan_version", "run_id", "mode", "k", "gate", "selecting_split", "paths",
    "adapter", "capability_matrix", "cases",
)

MODES = ("smoke", "regression", "targeted", "holdout", "full")
GATES = ("soft", "hard", "decision")
# SS6: the holdout ledger is appended for a run that reaches sealed cases, and
# holdout_ledger: null with one is exit 2 rather than an uncounted look.
SEAL_TOUCHING_MODES = ("holdout", "full")
HOLDOUT_SPLIT = "holdout"


def touches_seal(plan):
    """Does this run read a sealed case?

    The MODES `holdout` and `full` do, and so does any selection on the
    `holdout` split. The SPLIT named "full" does not: it is the reviewed,
    unsealed set that `regression` selects, mutually exclusive with `holdout`
    (case-format.md). One tuple used to be compared against both the mode and
    the split name, so every everyday regression run -- selecting_split "full"
    -- spent a holdout look and needed a ledger, and five pre-merge CI runs
    forced a reseal without one sealed case having been read (2026-09-21).
    """
    return (plan["mode"] in SEAL_TOUCHING_MODES
            or plan["selecting_split"] == HOLDOUT_SPLIT)


RETRY_AFTER_CAP_S = 60
ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# The adapter fields whose resolved text is sent as-is AND recorded per case
# (request.json's body and url, the manifest's url). Headers are not here: they
# are redacted by name, in redact().
SENT_VERBATIM_PATHS = ("adapter.invocation.request_body",
                       "adapter.invocation.base_url",
                       "adapter.invocation.endpoint")

# SS7's vocabulary, unchanged from the rest of the harness.
PASS, FAIL = "pass", "fail"
UNSCORED, SKIPPED = "unscored", "skipped"
INFRA_ERROR, INFRA_INCOMPLETE = "infra_error", "infra_incomplete"

# SS5's layer table, in the order it is walked and in the order the layers are
# written into verdict.json. Every layer gets a row on every case: an absent
# row and an `n/a` row are not the same statement, and "this case does not
# assert authz" is worth saying out loud.
LAYER_ORDER = ("http", "routing", "trajectory", "tool_selection", "loops",
               "answer", "execution", "authz", "rules", "state", "judged")

# The three non-verdicts SS5 makes load-bearing. None of them is ever `pass`
# and none is ever `fail`:
#   n/a        - not applicable: the case does not carry the trigger.
#   unscorable - applicable, but the capability matrix disabled the layer.
#                `blocked_by` is COPIED from the matrix, never invented.
#   unscored   - applicable and enabled, but the input could not be produced.
# The distinction is the difference between an honest gap and a lie; in the
# authz row a `pass` here would be a dangerous one.
NA, UNSCORABLE = "n/a", "unscorable"
# ...and the fourth: the scorer itself failed (SS5.1). Distinct from `unscored`
# because a broken scorer is a harness bug, not a property of the app, and it
# is what exit 7 counts.
LAYER_ERROR = "error"
# The sidecar score_agreement.py writes, named by plan paths.judge_calibration.
# JSON, not YAML, for the reason the holdout ledger is .jsonl: this package is
# stdlib-only and cannot safely rewrite the YAML that holds profile.yaml's
# judge block.
JUDGE_CALIBRATION_SCHEMA = "evalup/judge-calibration/1"
# `<rubric_id>-v<version>` is a PIN, not part of the id (rubric-format.md).
RUBRIC_PIN = re.compile(r"-v\d+$")

# Layers whose input is the normalized trajectory. Broader than
# TRACE_DEPENDENT_LAYERS below, which is the capability taxonomy the manifest
# reports: authz is not a trace CAPABILITY, but score_authz.py reads a
# trajectory, so a trace that never arrived leaves it infra_incomplete rather
# than pass.
TRAJECTORY_LAYERS = ("trajectory", "tool_selection", "loops", "authz")

# Categories the environment.safe_to_attack gate refuses outright (SS4.5/SS7).
# adversarial-refusal is the only attack-shaped category case-format.md
# defines; red-team/chaos are run kinds, not case categories.
ATTACK_CATEGORIES = ("adversarial-refusal",)
# ...and the categories whose crash increments crash_rate's numerator (SS7).
CRASH_RATE_CATEGORIES = ("noise", "adversarial-refusal")

SCORING_DEFAULTS = {
    "oos_route": None, "id_pattern": None, "float_tolerance": 0.0,
    "repeat_threshold": 3, "call_budget": 15,
    "fail_on_errored_calls": False, "scorer_timeout_s": 30,
}
EXECUTION_DEFAULTS = {
    "timeout_s": None, "max_attempts": 3, "backoff_s": [1, 4],
    "infra_rate_abort": 0.25, "insecure_tls": False, "max_turns": 12,
}
# SS7: checked after each case, but only once the denominator means something.
INFRA_ABORT_MIN_CASES = 8

# Trace sources this runner can query. view-only and none are trace-less by
# declaration; jaeger/tempo/clickhouse are declared in adapter-contract.md but
# each needs its own client, so v1 refuses them loudly at pre-flight rather
# than treating an unimplemented store as "no trace" (which would silently
# downgrade a fully instrumented app to trace-less scoring).
QUERYABLE_TRACE_SOURCES = ("otlp-file",)
UNIMPLEMENTED_TRACE_SOURCES = ("jaeger", "tempo", "clickhouse")
TRACELESS_TRACE_SOURCES = ("view-only", "none")
# The capability taxonomy the manifest reports as disabled on a trace-less run.
# `cost_latency` is in here and deliberately NOT in LAYER_ORDER (SS5.7): it is a
# run-level CAPABILITY, not a per-case layer, so this runner emits no row for it
# on any case. A per-case cost verdict would need a per-case budget and the case
# format has no such field. What the flag buys is the true statement that a
# trace-less run recorded no token count anywhere -- `cases/<id>/trajectory.json`
# is the only cost input in the tree -- which is what `scripts/score_cost.py`
# reads afterwards. Per-case `latency_s` is recorded either way.
TRACE_DEPENDENT_LAYERS = ("trajectory", "tool_selection", "loops",
                          "cost_latency")


class RunnerExit(Exception):
    """A terminating condition with a contract exit code (SS11).

    Raised rather than sys.exit()ed so main() can print the {"error": ...}
    payload on STDOUT for every non-zero exit -- the scorers' convention (see
    _common.die), which lets a caller parse one shape regardless of outcome.
    _common's own loaders still exit 2 directly, which is the same code this
    module uses for bad input, so the two paths agree.
    """

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def payload(self):
        return dict(self.extra, error=self.message)


def bad_input(message, **extra):
    raise RunnerExit(EXIT_INPUT, message, **extra)


def preflight_fail(message, **extra):
    raise RunnerExit(EXIT_PREFLIGHT, message, **extra)


def utc_now():
    """One timestamp format everywhere: RFC3339, UTC, second resolution.

    datetime.now(timezone.utc) rather than utcnow(), which returns a NAIVE
    datetime and is deprecated in 3.12 -- a naive timestamp compared against an
    aware one raises, and this string ends up in the manifest that a later
    comparison reads.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(obj):
    """The byte string plan_sha256 and every case sha256 are taken over.

    sort_keys + tight separators + ensure_ascii=False so the hash depends on
    the VALUES, not on the caller's key order or its json.dumps settings; a
    resume check that failed because the skill re-serialized the same plan with
    different spacing would be useless.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256_of(obj):
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def atomic_write(path, text):
    """SS6: every write in the run tree is temp-then-rename.

    os.replace is atomic within a filesystem, so a reader (or a resumed run, or
    --verify) never sees a partial file, and a kill mid-write leaves either the
    old bytes or the new ones -- never half of either. The pid in the temp name
    keeps two processes writing the same tree from colliding on the temp file
    itself.
    """
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def atomic_write_json(path, obj):
    atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def atomic_write_jsonl(path, rows):
    atomic_write(path, "".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# SS2. Plan validation. Everything here runs BEFORE any spend, and every
# failure is exit 2 with {"error": ...} on stdout.
# --------------------------------------------------------------------------

def validate_plan(plan, out_dir):
    """Check only what the runner needs in order to execute.

    Deliberately NOT a second linter. validate_cases.py owns what a valid case
    is, the skill runs it, and re-deriving its rules here would create a second
    definition of a valid case that drifts from the first. The checks below are
    the ones whose violation makes execution itself meaningless: a run that
    cannot be labelled, cannot be compared, or cannot be scored.
    """
    if not isinstance(plan, dict):
        bad_input(f"plan must be a JSON object, got {type(plan).__name__}")

    # A typo'd top-level key must not silently disable a layer -- the whole
    # reason unknown keys are fatal rather than ignored.
    unknown = sorted(set(plan) - set(TOP_LEVEL_KEYS))
    if unknown:
        bad_input("unknown top-level key(s) in plan: {}".format(
            ", ".join(unknown)))
    missing = [key for key in REQUIRED_KEYS if key not in plan]
    if missing:
        bad_input("plan is missing required key(s): {}".format(
            ", ".join(missing)))

    if plan["plan_version"] != PLAN_VERSION:
        bad_input("plan_version must be {}, got {!r}".format(
            PLAN_VERSION, plan["plan_version"]))

    run_id = plan["run_id"]
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        bad_input("run_id must match <mode>-<UTC YYYYMMDDTHHMMSSZ>, got "
                  f"{run_id!r}")
    # The directory name IS the run id everywhere else in the harness (the CI
    # example greps it out of the path), so a mismatch here would produce a run
    # whose id depends on which file you read it from.
    if os.path.basename(os.path.normpath(out_dir)) != run_id:
        bad_input(f"--out basename must equal run_id {run_id!r}, got "
                  f"{os.path.basename(os.path.normpath(out_dir))!r}")

    if plan["mode"] not in MODES:
        bad_input("mode must be one of {}, got {!r}".format(
            "|".join(MODES), plan["mode"]))
    if plan["gate"] not in GATES:
        bad_input("gate must be one of {}, got {!r}".format(
            "|".join(GATES), plan["gate"]))
    if not isinstance(plan["k"], int) or isinstance(plan["k"], bool) \
            or plan["k"] < 1:
        bad_input("k must be an integer >= 1, got {!r}".format(plan["k"]))

    if not isinstance(plan["capability_matrix"], dict):
        bad_input("capability_matrix must be an object, got {}".format(
            type(plan["capability_matrix"]).__name__))

    paths = plan["paths"]
    if not isinstance(paths, dict):
        bad_input(f"paths must be an object, got {type(paths).__name__}")
    for key in ("scripts_dir", "state_dir"):
        if not isinstance(paths.get(key), str) or not paths[key]:
            bad_input(f"paths.{key} is required and must be a path")
    absent = [name for name in REQUIRED_SCORERS
              if not os.path.isfile(os.path.join(paths["scripts_dir"], name))]
    if absent:
        bad_input("paths.scripts_dir {!r} is missing scorer script(s): "
                  "{}".format(paths["scripts_dir"], ", ".join(absent)))

    validate_cases_block(plan)
    validate_execution_block(plan)
    validate_adapter_block(plan)
    return plan


def validate_cases_block(plan):
    cases = plan["cases"]
    if not isinstance(cases, list) or not cases:
        bad_input("cases must be a non-empty list")
    selecting = plan["selecting_split"]
    if not isinstance(selecting, str) or not selecting:
        bad_input("selecting_split is required and must be a split name")

    seen = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            bad_input(f"cases[{index}] must be an object, got {type(case).__name__}")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            bad_input(f"cases[{index}] has no id")
        # Exit 2, before any spend and before any write: the id becomes a
        # directory name a few hundred lines down.
        reason = unsafe_case_id(case_id)
        if reason:
            bad_input(f"cases[{index}] id {case_id!r} {reason}")
        # A duplicate id would collide in cases/<case-id>/, so the second case
        # would overwrite the first's directory and the run would report N
        # cases from N-1 results.
        if case_id in seen:
            bad_input(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        splits = case.get("split")
        if not isinstance(splits, list) or selecting not in splits:
            # The runner refuses to run a set it cannot label: `set` in
            # verdicts.jsonl is selecting_split, and writing it over a case
            # that is not in that split would mislabel the durable record.
            bad_input(f"cases[{index}] ({case_id}) has split {splits!r}, "
                      "which does not contain "
                      f"selecting_split {selecting!r} -- the skill selected wrong")

    ledger = plan["paths"].get("holdout_ledger")
    if touches_seal(plan) and not ledger:
        bad_input("paths.holdout_ledger is null but selecting_split/mode is "
                  f"{selecting!r}: an uncounted holdout run makes the reseal trigger a "
                  "number nobody is keeping")


def validate_execution_block(plan):
    for name, defaults in (("scoring", SCORING_DEFAULTS),
                           ("execution", EXECUTION_DEFAULTS)):
        block = plan.get(name)
        if block is None:
            block = {}
        if not isinstance(block, dict):
            bad_input(f"{name} must be an object, got {type(block).__name__}")
        unknown = sorted(set(block) - set(defaults))
        if unknown:
            bad_input("unknown key(s) in {}: {}".format(
                name, ", ".join(unknown)))
        plan[name] = dict(defaults, **block)

    execution = plan["execution"]
    attempts = execution["max_attempts"]
    if not isinstance(attempts, int) or isinstance(attempts, bool) \
            or attempts < 1:
        bad_input("execution.max_attempts must be an integer >= 1, got "
                  f"{attempts!r}")
    backoff = execution["backoff_s"]
    if not isinstance(backoff, list) or len(backoff) != attempts - 1:
        # One wait per retry, named individually rather than derived, so the
        # schedule is readable in the plan; a length mismatch means the caller
        # changed one and not the other and the last retry would wait a
        # duration nobody wrote.
        bad_input("execution.backoff_s must have exactly max_attempts-1 = "
                  f"{attempts - 1} entries (one per retry), got {backoff!r}")
    rate = execution["infra_rate_abort"]
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) \
            or not 0 < rate <= 1:
        bad_input("execution.infra_rate_abort must be a fraction in (0, 1], "
                  f"got {rate!r}")
    cap = execution["max_turns"]
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 2:
        bad_input("execution.max_turns must be an integer >= 2 (a "
                  f"conversation has at least two turns), got {cap!r}")


def validate_adapter_block(plan):
    adapter = plan["adapter"]
    if not isinstance(adapter, dict):
        bad_input(f"adapter must be an object, got {type(adapter).__name__}")
    invocation = adapter.get("invocation")
    if not isinstance(invocation, dict):
        bad_input("adapter.invocation is required and must be an object")

    # Decision D5, confirmed 2026-09-08: REFUSE rather than warn-and-serialize.
    # A warning that scrolls past leaves the adapter field looking supported
    # while every run is serial; exit 2 makes the gap loud at the one moment
    # someone can act on it.
    concurrency = invocation.get("max_concurrency", 1)
    if isinstance(concurrency, int) and not isinstance(concurrency, bool) \
            and concurrency > 1:
        bad_input(f"adapter.invocation.max_concurrency is {concurrency} "
                  "but this runner "
                  "is serial-only; set it to 1 or wait for concurrent "
                  "execution")


# --------------------------------------------------------------------------
# SS3. Secrets and env resolution.
# --------------------------------------------------------------------------

def resolve_env(adapter):
    """Resolve ${VAR} refs in the adapter from os.environ, at pre-flight.

    The adapter arrives UNRESOLVED for two reasons the contract states: the
    plan is a file on disk and a plausible attachment to a bug report, so
    resolved tokens must never be written into it; and adapter hard rule 4
    ("env-var refs unresolved at runtime -> pre-flight failure before any
    spend") is a RUNTIME property, so the thing at runtime has to check it.

    Returns (resolved_adapter, secret_paths, auth_values). secret_paths is the
    set of dotted paths whose string value contained at least one env ref --
    the input to redaction (SS3: a resolved value never reaches disk).
    auth_values holds the bearer token / cookie, which are named by
    auth.token_env / auth.cookie_env rather than written as ${refs}; they are
    returned SEPARATELY and never written back into the adapter, so no later
    dump of the adapter object can leak them.

    Every missing name is reported at once, not the first: discovering them one
    run at a time is the exact complaint the field test's identity_headers note
    records.
    """
    missing = []
    secret_paths = set()
    sent_refs = {}

    def walk(node, path):
        if isinstance(node, dict):
            return {key: walk(value, f"{path}.{key}")
                    for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value, f"{path}[{i}]")
                    for i, value in enumerate(node)]
        if not isinstance(node, str):
            return node
        names = ENV_REF_RE.findall(node)
        if not names:
            return node
        secret_paths.add(path)
        resolved = node
        for name in names:
            value = os.environ.get(name)
            if not value:
                missing.append(name)
                continue
            resolved = resolved.replace("${" + name + "}", value)
            if path.startswith(SENT_VERBATIM_PATHS):
                sent_refs[name] = value
        return resolved

    out = walk(adapter, "adapter")

    # auth.token_env / auth.cookie_env name an env var directly instead of
    # spelling a ${ref}, so the walk above never sees them. Same hard rule 4
    # applies: an unset one is a pre-flight failure, not an empty Authorization
    # header the app answers 401 to on every case.
    auth = (out.get("invocation") or {}).get("auth") or {}
    auth_values = {}
    for kind, field in (("bearer", "token_env"), ("cookie", "cookie_env")):
        if auth.get("type") != kind:
            continue
        name = auth.get(field)
        if not isinstance(name, str) or not name:
            preflight_fail(f"adapter.invocation.auth.{field} is required for "
                           f"auth.type: {kind}")
        value = os.environ.get(name)
        if not value:
            missing.append(name)
        else:
            auth_values[kind] = value

    if missing:
        preflight_fail(
            "unresolved env var(s) referenced by adapter: {}".format(
                ", ".join(sorted(set(missing)))),
            missing_env=sorted(set(missing)))
    return out, secret_paths, auth_values, sent_refs


def unresolve(node, sent_refs):
    """Put `${NAME}` back wherever a resolved env value sits, for the RECORD of
    a request -- never for the request itself.

    SS3 says a resolved value never reaches disk, and redact() only ever kept
    that promise for headers. `${API_KEY}` inside `request_body` was resolved
    into the template, sent, and then written verbatim into every
    cases/<id>/request.json (2026-09 audit) -- and an API key in the body is an
    ordinary way to authenticate. It is applied to the TEMPLATE before the
    case's placeholders are rendered into it, never to the rendered body: a
    short value (`EVAL_TENANT=42`) must not rewrite a user turn that happens to
    say "42". Longest value first, so one value that contains another is not
    split by it.
    """
    if isinstance(node, dict):
        return {key: unresolve(value, sent_refs) for key, value in node.items()}
    if isinstance(node, list):
        return [unresolve(value, sent_refs) for value in node]
    if not isinstance(node, str):
        return node
    for name, value in sorted(sent_refs.items(),
                              key=lambda item: len(item[1]), reverse=True):
        node = node.replace(value, "${" + name + "}")
    return node


def secret_header_names(adapter, secret_paths):
    """Header names whose VALUE must never be written to disk (SS3).

    Two sources, union: a header whose value came from an env ref, and any
    header named by the adapter's auth block regardless of where its value came
    from. The second half matters because an auth header with an inline value
    breaks adapter-contract.md's rule and NOTHING checks that rule -- there is
    no adapter validator -- so this runner must not be the thing that writes
    it into every request.json. The same is not true of request_body: an
    inline literal there is recorded as written, because the runner cannot
    tell a secret from a tenant id. Only `${VAR}` refs are protected.
    """
    names = set()
    auth = adapter.get("invocation", {}).get("auth") or {}
    if auth.get("type") == "headers":
        names.update(auth.get("headers") or {})
    elif auth.get("type") == "bearer":
        names.add("Authorization")
    elif auth.get("type") == "cookie":
        names.add("Cookie")
    for path in secret_paths:
        if ".invocation.auth.headers." in path:
            names.add(path.rsplit(".", 1)[-1])
    return names


# --------------------------------------------------------------------------
# Invocation. Two modes (decision D6, confirmed 2026-09-08): http and
# function. `cli` exits 3 -- adapter-contract.md advertises it, and refusing
# loudly is the only honest thing to do with a mode nothing implements.
# --------------------------------------------------------------------------

REQUEST_PLACEHOLDERS = ("<user turn>", "<uuid>", "<persona>", "<case id>",
                        "<session>")
# docs/multi-turn.md SS2. The id a `server-id` app minted on turn 1, rendered
# into turns 2+. Unset (turn 1, every single-turn case) a dict entry that IS
# this placeholder is DROPPED rather than sent as null: an app binding a
# non-nullable id answers null with a 400, and an app that accepts null also
# accepts a missing key -- the reverse is false.
SESSION_MARKER = "<session>"
# The adapter's invocation.conversation block (SS2), checked at pre-flight.
CONVERSATION_KEYS = ("style", "session_from", "turn_delay_s", "memory")
CONVERSATION_STYLES = ("client-id", "server-id", "cookie")
CONVERSATION_MEMORY = ("session", "user")
ANSWER_MARKER = "<answer>"
TRACE_ID_MARKER = "<trace id>"


class TransportError(Exception):
    """The app was not reached, or did not answer in time.

    `kind` separates a connection reset (which is the app CRASHING, and feeds
    crash_rate on noise/adversarial cases per SS7) from a timeout or a refused
    connection (which is not). Both are infra; only one is the app falling
    over.
    """

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def parse_endpoint(invocation):
    """`endpoint: "POST /api/chat/ask"` -> ("POST", "/api/chat/ask")."""
    endpoint = invocation.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.strip():
        preflight_fail("adapter.invocation.endpoint is required for "
                       "invocation.mode: http (e.g. \"POST /api/chat/ask\")")
    parts = endpoint.split(None, 1)
    if len(parts) != 2 or not parts[1].startswith("/"):
        preflight_fail("adapter.invocation.endpoint must be "
                       f"\"<METHOD> /<path>\", got {endpoint!r}")
    return parts[0].upper(), parts[1]


def load_body_template(invocation, field):
    """Parse a declared JSON body template off the adapter.

    These are TEMPLATES, not examples: the runner substitutes the placeholders
    below and sends the result, so an app's request shape is declared by its
    adapter rather than guessed by the runner. A malformed template is a
    pre-flight failure, because the alternative is discovering it one case at a
    time after the run has started spending.
    """
    raw = invocation.get(field)
    if not isinstance(raw, str) or not raw.strip():
        preflight_fail(f"adapter.invocation.{field} is required for "
                       "invocation.mode: http")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        preflight_fail(f"adapter.invocation.{field} is not valid JSON: {exc}")


def render_template(node, values):
    """Substitute REQUEST_PLACEHOLDERS through a parsed template.

    A string that IS a placeholder becomes the typed value; a string that
    merely CONTAINS one gets textual substitution, so both
    `"message": "<user turn>"` and `"prompt": "User says: <user turn>"` work.
    The one exception is an unset `<session>` as a dict entry's whole value:
    the entry is dropped (SESSION_MARKER says why).
    """
    if isinstance(node, dict):
        return {key: render_template(value, values)
                for key, value in node.items()
                if not (value == SESSION_MARKER
                        and values.get(SESSION_MARKER) is None)}
    if isinstance(node, list):
        return [render_template(value, values) for value in node]
    if not isinstance(node, str):
        return node
    if node in values:
        return values[node]
    out = node
    for marker, value in values.items():
        if marker in out:
            out = out.replace(marker, "" if value is None else str(value))
    return out


def template_mentions(node, marker):
    """Does any string in a parsed template contain `marker`?"""
    if isinstance(node, dict):
        return any(template_mentions(v, marker) for v in node.values())
    if isinstance(node, list):
        return any(template_mentions(v, marker) for v in node)
    return isinstance(node, str) and marker in node


def find_marker_path(node, marker, path=()):
    """Where does `marker` sit in the response template? Returns a key path.

    The response template declares WHICH field carries the answer
    (`{"message": "<answer>"}`), so extraction is a declared lookup rather than
    a search for the longest string in the body -- the kind of heuristic that
    works on four cases and silently picks the wrong field on the fifth.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            found = find_marker_path(value, marker, path + (key,))
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for index, value in enumerate(node):
            found = find_marker_path(value, marker, path + (index,))
            if found is not None:
                return found
        return None
    return path if node == marker else None


def extract_path(body, path):
    node = body
    for key in path:
        in_dict = isinstance(node, dict) and key in node
        in_list = (isinstance(node, list) and isinstance(key, int)
                   and 0 <= key < len(node))
        if not (in_dict or in_list):
            return None
        node = node[key]
    return node


def build_headers(adapter, case, identity_map, auth_values):
    """Request headers, plus the per-header record of what was redacted.

    Case `identity` (case-format.md) reaches the app only through a DECLARED
    invocation.identity_map -- {"permissions": "X-User-Permissions", ...}. A
    case that declares an identity with no map is skipped, not run: sending it
    under the adapter's default identity would score an authorization case
    against the wrong persona and report the result as if it meant something.
    """
    invocation = adapter.get("invocation", {})
    headers = {"Content-Type": "application/json"}
    auth = invocation.get("auth") or {}
    if auth.get("type") == "headers":
        headers.update(auth.get("headers") or {})
    elif auth.get("type") == "bearer":
        headers["Authorization"] = "Bearer {}".format(auth_values["bearer"])
    elif auth.get("type") == "cookie":
        headers["Cookie"] = auth_values["cookie"]
    identity = case.get("identity")
    if isinstance(identity, dict):
        for key, value in identity.items():
            header = identity_map.get(key)
            if header:
                headers[header] = ",".join(str(v) for v in value) \
                    if isinstance(value, list) else str(value)
    return headers


def redact(headers, secret_names):
    return {name: ("<redacted>" if name in secret_names else value)
            for name, value in headers.items()}


class _CookieResponse:
    """What CookieJar.extract_cookies reads off a response: its headers."""

    def __init__(self, set_cookies):
        self.message = email.message.Message()
        for value in set_cookies:
            self.message["Set-Cookie"] = value

    def info(self):
        return self.message


def absorb_cookies(jar, url, set_cookies):
    """Store a response's Set-Cookie headers in the conversation's jar, by
    the stdlib's own rules (domain, path, expiry, Max-Age=0 deletes)."""
    if set_cookies:
        jar.extract_cookies(_CookieResponse(set_cookies),
                            urllib.request.Request(url))


def merged_cookie_header(jar, url, static):
    """ONE Cookie header: the auth cookie, then the jar's (SS2).

    The runner composes it itself because the stdlib will not:
    CookieJar.add_cookie_header adds nothing when a Cookie header is already
    present, and build_headers sets one for auth: {type: cookie}. Used as-is,
    the app's session cookie would never reach turn 2, and every cookie-style
    app with cookie auth would score "forgot the context"."""
    probe = urllib.request.Request(url)
    jar.add_cookie_header(probe)
    from_jar = probe.get_header("Cookie")
    parts = [part for part in (static, from_jar) if part]
    return "; ".join(parts) if parts else None


def ssl_context(insecure):
    if not insecure:
        return None
    # Opt-in only, and only reachable through execution.insecure_tls: the field
    # test's app is a self-signed localhost dev host (its notes say `curl -k`).
    # Defaulting this on would silently accept a MITM on any run.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def retry_wait(declared, result):
    """The wait before the next attempt: the plan's backoff, or the server's
    Retry-After when that is LONGER, capped at RETRY_AFTER_CAP_S.

    A rate-limited app says how long to wait, and the fixed backoff ignored
    it: three attempts inside five seconds cannot outlast a per-minute window,
    so every case went to infra_error, infra_rate_abort fired, and the run
    exited 5 -- "the service is degraded" -- when the truth was that the
    runner would not wait (2026-09 audit). Only the delay-seconds form is
    read; an HTTP-date is rare from an API and not worth a clock comparison.
    Capped, so a hostile or mistaken header cannot park a run for an hour."""
    if not result or result.get("status") not in (429, 503):
        return declared
    headers = {name.lower(): value
               for name, value in (result.get("headers") or {}).items()}
    value = str(headers.get("retry-after", "")).strip()
    if not value.isdigit():
        return declared
    return max(declared, min(int(value), RETRY_AFTER_CAP_S))


def transport_kind(exc):
    """(kind, message) for a failure while reading a reply.

    A body cut short, like a reset, is the app dropping the connection
    mid-answer -- the crash SS7 counts. A reply that is not HTTP at all is
    something else listening on the port: transport, not a crash."""
    if isinstance(exc, socket.timeout):
        kind = "timeout"
    elif isinstance(exc, (ConnectionError, http.client.IncompleteRead)):
        kind = "connection"
    else:
        kind = "transport"
    return kind, f"{type(exc).__name__}: {exc}"


def send_http(url, method, headers, body, timeout, insecure):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") \
        if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=ssl_context(insecure)) as response:
            status = response.getcode()
            raw = response.read()
            response_headers = dict(response.headers.items())
            # Every Set-Cookie, not the last: the dict above keeps one value
            # per name, and an app setting two cookies would lose its
            # session cookie to the second (docs/multi-turn.md SS2).
            set_cookies = response.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx is a RESPONSE, not a transport failure -- and often the
        # expected one (the field test asserts a deliberate 400 on OOS). It
        # must reach the taxonomy in SS7 as a status, not as an exception.
        status = exc.code
        try:
            raw = exc.read()
        except (http.client.HTTPException, OSError) as read_exc:
            # Raised inside this handler, so the siblings below never see it.
            raise TransportError(*transport_kind(read_exc)) from read_exc
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        set_cookies = (exc.headers.get_all("Set-Cookie") or []) \
            if exc.headers else []
    except socket.timeout as exc:
        raise TransportError(
            "timeout", f"timed out after {timeout}s: {exc}") from exc
    except ConnectionError as exc:
        raise TransportError("connection", str(exc)) from exc
    except urllib.error.URLError as exc:
        # urllib wraps every OSError from the connection in URLError, so the
        # ORIGINAL class is the only place the distinction survives: a dropped
        # connection (http.client.RemoteDisconnected, a ConnectionError) is the
        # app crashing and feeds crash_rate; a timeout is not.
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            kind = "timeout"
        elif isinstance(reason, ConnectionError):
            kind = "connection"
        else:
            kind = "transport"
        raise TransportError(kind, str(reason)) from exc
    except http.client.HTTPException as exc:
        # urllib wraps only OSError, so a response that is not HTTP
        # (BadStatusLine, LineTooLong) or a body cut short of its
        # Content-Length (IncompleteRead) arrives here unwrapped -- and used to
        # escape to main() as exit 1, killing the run over one case's reply.
        # (RemoteDisconnected is also a ConnectionError: caught above.)
        raise TransportError(*transport_kind(exc)) from exc
    except OSError as exc:
        raise TransportError("transport", str(exc)) from exc
    latency = round(time.time() - started, 3)
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        # Recorded as {"raw": ...} rather than dropped: a non-JSON body is
        # usually the error page that explains the run, and a response.json
        # with no body is the shape nobody can debug afterwards.
        parsed = {"raw": text}
    return {"status": status, "body": parsed, "latency_s": latency,
            "headers": response_headers, "set_cookies": set_cookies}


def load_entrypoint(invocation, app_root):
    """function mode: import "module:callable" once, at pre-flight.

    Importing here rather than per case means an ImportError is a pre-flight
    failure (exit 3, nothing billed) instead of N identical infra errors, and
    it doubles as function mode's health check -- adapter-contract.md calls
    this the PREFERRED mode when auth or HTTP is in the way, so it must not be
    the mode with the worse failure reporting.

    NOTE: execution.timeout_s is NOT enforced in function mode. An in-process
    call cannot be interrupted from the stdlib without leaving the thread
    running behind it, and a runner that reported "timeout" while the callable
    kept executing would be lying about the state of the world. The callable
    owns its own timeout; the manifest records that this run's timeout was
    advisory.
    """
    spec = invocation.get("entrypoint")
    if not isinstance(spec, str) or ":" not in spec:
        preflight_fail("adapter.invocation.entrypoint must be "
                       "\"module:callable\" for invocation.mode: function, "
                       f"got {spec!r}")
    module_name, _, attribute = spec.partition(":")
    if app_root and os.path.isdir(app_root) and app_root not in sys.path:
        sys.path.insert(0, app_root)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - any import failure is pre-flight
        preflight_fail(f"cannot import entrypoint module {module_name!r}: "
                       f"{type(exc).__name__}: {exc}")
    target = getattr(module, attribute, None)
    if not callable(target):
        preflight_fail(f"entrypoint {spec!r} is not callable")
    return target


def send_function(target, request_body):
    """Call the entrypoint with the rendered request body.

    The return shape is either the answer text, or an object that may also
    carry a status and a trace id:
      "the answer"  |  {"text": ..., "status": 200, "trace_id": ..., ...}
    Anything else is a transport error rather than a guess, for the same
    reason find_marker_path exists.
    """
    started = time.time()
    try:
        result = target(request_body)
    except Exception as exc:  # noqa: BLE001 - the app raising IS an infra event
        raise TransportError(
            "connection", f"{type(exc).__name__}: {exc}") from exc
    latency = round(time.time() - started, 3)
    if isinstance(result, str):
        result = {"text": result}
    if not isinstance(result, dict):
        raise TransportError(
            "transport",
            f"entrypoint returned {type(result).__name__}, expected str "
            "or dict")
    return {"status": int(result.get("status", 200)), "body": result,
            "latency_s": latency, "headers": {}}


# --------------------------------------------------------------------------
# Layer applicability (contract SS5's trigger column). The trigger rules live
# here, once, so nothing else in the module can disagree about which layers a
# case even claims to exercise.
# --------------------------------------------------------------------------

def applicable_layers(case, trace_collected):
    """SS5's trigger column: which layers this case even CLAIMS to exercise.

    A layer is applicable iff its trigger field is present on the case. `loops`
    is the one whose trigger is the run rather than the case -- there is
    nothing to detect a loop in without a trajectory.
    """
    expect = case.get("expect") or {}
    answer = expect.get("answer") or {}
    layers = ["http"]
    if "route" in expect or "route_acceptable" in expect:
        layers.append("routing")
    if "tools" in expect:
        layers.append("trajectory")
    if "args" in expect:
        layers.append("tool_selection")
    if "answer" in expect or "format" in expect:
        layers.append("answer")
    if "result" in expect:
        layers.append("execution")
    if "authz" in expect:
        layers.append("authz")
    if answer.get("rules"):
        layers.append("rules")
    if expect.get("state") is not None:
        layers.append("state")
    if answer.get("rubric"):
        layers.append("judged")
    if trace_collected:
        layers.append("loops")
    return layers


def na(name):
    return {"layer": name, "verdict": NA}


def unscorable(name, blocked_by):
    return {"layer": name, "verdict": UNSCORABLE, "blocked_by": blocked_by}


def unscored(name, reason):
    return {"layer": name, "verdict": UNSCORED, "reason": reason}


def shape_actual(value):
    """SS5.3: the tiny JSON file score_execution.py expects.

    A list of objects is a row set; anything else is a scalar. The runner does
    not coerce types beyond that -- score_execution.py owns the type-aware
    comparison (7 == "7" == "7.0"), and a second normalization here would be a
    second answer to what equality means.
    """
    if isinstance(value, list):
        return {"rows": value}
    return {"scalar": value}


def dotted_get(node, path):
    """Read a dotted path out of a parsed body. Missing -> None.

    Used only for values an ADAPTER declared the path of (the route field, the
    clarify flag, a structured result). The runner never hunts a response for
    "the field that looks like a route": a heuristic that works on four cases
    and picks the wrong field on the fifth is the failure this whole contract
    is written against.
    """
    for part in str(path).split("."):
        if isinstance(node, list):
            try:
                node = node[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# SS10 rules 5-6: rows every case gets from the run, not from what it asserts.
RUN_TRIGGERED = ("http", "loops")


def roll_up(layers, skipped_reason):
    """SS10's case-verdict rollup, first match wins.

    Rule 7 is the point of the whole ordering: a case whose every layer came
    back n/a / unscorable / unscored is NOT a pass. That is the vacuous-case
    failure validate_cases.py lints for at authoring time, caught again here at
    run time -- and rules 5-6 are what make it bite (see below): `http` alone
    is a pass only for a case that asserts nothing else. `loops` is left out
    of both for the same reason: the run triggers it, not the case.

    The scorer-error rule sits ABOVE `fail` deliberately (SS5.1: "the case rolls
    up to unscored"). A case with a broken scorer has a number nobody should
    quote, and letting a sibling layer's `pass` carry it to `pass` would put
    exactly that number in the report -- while the run still exits 7.
    """
    values = [layer.get("verdict") for layer in layers.values()]
    # A conversation's union is keyed `t<n>.<layer>` (docs/multi-turn.md
    # SS3); rules 5-6 judge the LAYER, so the turn prefix comes off first.
    # A single-turn case has no prefix and reads exactly as before.
    base = {name: name.rsplit(".", 1)[-1] for name in layers}
    if INFRA_ERROR in values:
        return INFRA_ERROR
    if INFRA_INCOMPLETE in values:
        return INFRA_INCOMPLETE
    if skipped_reason:
        return SKIPPED
    if LAYER_ERROR in values:
        return UNSCORED
    if FAIL in values:
        return FAIL
    # Rule 5, and `http` is excluded from it on purpose (Step 10). A status
    # code is a LIVENESS check -- validate_cases.py refuses to count it toward
    # gradedness for exactly this reason -- so letting it be the one `pass`
    # that carries a case made every case whose real layers came back
    # unscored/unscorable a `pass`: a trace-less run of an expect.tools case,
    # an answer the adapter's <answer> path could not reach, and every
    # RESERVED expectation (expect.state) alike. SS5 says a `pass` on an
    # unscorable trajectory row "would have been a lie"; this is where the lie
    # was told, one level up. A case that asserts nothing BUT http is still a
    # pass -- there the liveness check is the whole claim.
    #
    # `loops` is excluded for the same reason (2026-09-30 review): a collected
    # trace triggers it, not anything the case asserts, so its `pass` let a
    # traced run carry a rubric-only or state-only case to `pass` -- and made
    # an http-only case `pass` or `unscored` by whether loops scored. Its
    # `fail` still fails the case (rule 4): a loop is a real pathology.
    if any(base[name] not in RUN_TRIGGERED and layer.get("verdict") == PASS
           for name, layer in layers.items()):
        return PASS
    http = [layer for name, layer in layers.items() if base[name] == "http"]
    if http and all(layer.get("verdict") == PASS for layer in http) and not [
            name for name, layer in layers.items()
            if base[name] not in RUN_TRIGGERED and layer.get("verdict") != NA]:
        return PASS
    return UNSCORED


def fold_repeats(values):
    """SS5.5's fold of k repeat verdicts into one case verdict.

    pass^k (decision D7): a case passes only if EVERY repeat passed. The modes
    that use k>1 are asking for reliability, and a case that passes 2 of 3 is
    not a case that passes; the pass@k/pass^k gap is reported by
    reduce_repeats.py, never hidden.

    An observed `fail` wins first: it already makes "every repeat passed"
    false, and filing it under provider noise would lose the one thing the run
    saw. Otherwise any repeat that was not a `pass` decides, in roll_up()'s
    order -- a repeat the app never answered cannot count toward pass^k. The
    fold used to stop at pass/fail, so [pass, infra_error] kept repeat 1's
    `pass` while [infra_error, pass] kept its `infra_error`: one pair, two
    verdicts, and a canary that missed a repeat read "passed" (F-158, the
    2026-09-26 user test).
    """
    if len(values) == 1:
        return values[0]
    if FAIL in values:
        return FAIL
    if all(v == PASS for v in values):
        return PASS
    for value in (INFRA_ERROR, INFRA_INCOMPLETE):
        if value in values:
            return value
    return UNSCORED


def repeated_5xx(attempts):
    """The one 5xx (status and body) every attempt of every repeat got.

    An exhausted 5xx is `infra_error` (SS7) and stays so: the app's own
    500 and a throttled provider's 500 can carry byte-identical bodies (the
    2026-09-26 user test's guard-blocked injection and its Gemma outage did),
    so nothing in one response can tell them apart. What CAN be said is that
    the same status came back on every attempt, which is how a deterministic
    app error looks and a passing blip does not -- so it is recorded, counted
    in summary.repeated_5xx and printed by gate.py, and never rescored (F-165).
    Fewer than two observations prove nothing and give None.
    """
    records = [a["record"] for a in attempts]
    answers = {r.get("same_5xx") for r in records}
    observed = sum(r["response"]["attempts"] for r in records)
    if len(answers) != 1 or observed < 2:
        return None
    answer = answers.pop()
    if answer is None:
        return None
    found = {"status": answer[0], "attempts": observed}
    if len(answer) > 2:
        # A conversation (docs/multi-turn.md SS3): the same 5xx and body at
        # the SAME turn on every attempt, and that turn is recorded -- turns
        # 1..N-1 were replayed on every attempt to reach it.
        found["turn"] = answer[2]
    return found


def body_key(body):
    """A comparable form of a response body, for repeated_5xx(). Never raises:
    a function-mode entrypoint may return a dict json.dumps cannot sort (mixed
    key types), and a comparison helper must not be what kills a run."""
    try:
        return json.dumps(body, sort_keys=True, ensure_ascii=False,
                          default=repr)
    except (TypeError, ValueError):
        return repr(body)


def case_text(case):
    """The user turn the app is asked. Last user message, per case-format.md."""
    messages = ((case.get("input") or {}).get("messages")) or []
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return message.get("content") or ""
    return ""


def user_turn_count(case):
    messages = ((case.get("input") or {}).get("messages")) or []
    return sum(1 for m in messages
               if isinstance(m, dict) and m.get("role") == "user")


def context_messages_reason(case):
    """Why `input.messages` is not exactly one user message, or None.

    case_text() sends the user message and nothing else. A system or
    assistant message beside it never reached the app, and the case used to
    score as if it had -- the truncated-conversation verdict hard rule 3
    refuses, one door over (docs/multi-turn.md SS0). So such a case is
    skipped with this reason, and validate_cases.py reports it first.
    """
    messages = ((case.get("input") or {}).get("messages")) or []
    if not isinstance(messages, list):
        messages = [messages]
    users = user_turn_count(case)
    if users == 0:
        return ("input.messages holds no user message, so there is nothing "
                "to send")
    others = sorted({str(m.get("role")) if isinstance(m, dict)
                     else type(m).__name__ for m in messages
                     if not (isinstance(m, dict) and m.get("role") == "user")})
    if users == 1 and others:
        return ("input.messages holds {} message(s) besides the user message "
                "({}); only the user message would be sent, so the case "
                "would score as if that context had reached the app".format(
                    len(messages) - 1, ", ".join(others)))
    return None


def flatten_layers(layers):
    return {name: layer.get("verdict") for name, layer in layers.items()}


STOPPING = (FAIL, INFRA_ERROR, INFRA_INCOMPLETE)


def layer_reason(name, layer):
    """One line on why a layer did not pass, for a conversation's stop
    reason (docs/multi-turn.md SS3: "expected route licences, got a
    clarification"). Read off the layer's own object -- never re-scored."""
    if layer.get("reason"):
        return f"{name}: {layer['reason']}"
    row = layer.get("row")
    if isinstance(row, dict):
        return "{}: expected route {!r}, got {!r}".format(
            name, row.get("expected"), row.get("observed"))
    if "status" in layer:
        expected = layer.get("expected")
        return "{}: HTTP {}{}".format(
            name, layer["status"],
            f", expected {expected}" if expected is not None else "")
    for check in layer.get("checks") or []:
        if isinstance(check, dict) and check.get("status") == FAIL:
            return "{}: {} {!r}: {}".format(
                name, check.get("check"), check.get("entry"),
                check.get("reason"))
    return f"{name}: {layer.get('verdict')}"


def turn_stop_reason(turn):
    """Why a turn stopped its conversation: the first layer, in table order,
    whose verdict is the turn's own."""
    for name in LAYER_ORDER:
        layer = turn["layers"].get(name) or {}
        if layer.get("verdict") == turn["verdict"]:
            return "turn {}: {}".format(turn["turn"], layer_reason(name, layer))
    return "turn {}: {}".format(turn["turn"], turn["verdict"])


# --------------------------------------------------------------------------
# The runner itself.
# --------------------------------------------------------------------------

class Runner:
    def __init__(self, plan, out_dir, resume=False, dry_run=False,
                 baseline_verdicts=None):
        self.plan = plan
        self.out = out_dir
        self.cases_dir = os.path.join(out_dir, "cases")
        self.resume = resume
        self.dry_run = dry_run
        self.state_dir = plan["paths"]["state_dir"]
        self.k = plan["k"]
        self.execution = plan["execution"]
        self.started_at = utc_now()
        self.plan_sha256 = sha256_of(plan)
        # Memoized: unjudged_reason() is called per case AND in the summary,
        # and the answer is run-level -- re-reading the sidecar per case would
        # let a mid-run edit change the reason from case to case.
        self._judge_gate = None

        self.adapter = {}
        self.auth_values = {}
        self.sent_refs = {}
        self.secret_names = set()
        self.identity_map = {}
        self.entrypoint = None
        self.request_template = None
        self.answer_path = None
        self.trace_id_path = None
        self.url = None
        self.record_url = None
        self.record_template = None
        self.method = "POST"
        # invocation.conversation, checked at pre-flight; None = undeclared,
        # and every input.turns case is skipped (docs/multi-turn.md SS2).
        self.conversation = None

        self.trace_state = {"source": None, "correlation": None,
                            "collected": False, "disabled_layers": []}
        self.never_live_tools = set()
        self.verdicts = []          # verdict.json objects, in completion order
        self.attempted = 0
        self.crashes = 0
        self.scorer_errors = 0
        self.holdout_looks = None
        self.status = "running"
        self.routing_rows = []      # SS5.2's run-level input, accumulated
        self.baseline_verdicts = baseline_verdicts

    # -- logging (SS12) ---------------------------------------------------
    def log(self, event, **fields):
        """Append-only JSONL for post-hoc debugging.

        Deliberately NOT in REQUIRED_ALWAYS: nothing reads it programmatically,
        and a required file nobody consumes is how required files rot. Opened
        per write so a killed run still has every line up to the kill.
        """
        if self.dry_run:
            return
        row = dict(fields, ts=utc_now(), event=event)
        path = os.path.join(self.out, "run.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # -- SS4. Pre-flight --------------------------------------------------
    def preflight(self):
        """In order, and any failure is exit 3 with zero app calls billed."""
        adapter = self.plan["adapter"]
        (self.adapter, secret_paths, self.auth_values,
         self.sent_refs) = resolve_env(adapter)
        self.secret_names = secret_header_names(self.adapter, secret_paths)
        invocation = self.adapter.get("invocation", {})
        self.identity_map = invocation.get("identity_map") or {}

        mode = invocation.get("mode")
        if mode == "http":
            self.prepare_http(invocation)
        elif mode == "function":
            self.prepare_function(invocation)
        else:
            preflight_fail(
                "runner v1 implements invocation.mode: http and function "
                f"only (got: {mode!r})")
        self.prepare_conversation(invocation, mode)
        # What request.json and the manifest RECORD, as opposed to what is
        # sent: the same template and url with every env ref put back (SS3).
        self.record_template = unresolve(self.request_template, self.sent_refs)
        self.record_url = unresolve(self.url, self.sent_refs)

        health = self.health_check(mode)
        self.trace_branch(health)
        self.safety_gates()

        if self.dry_run:
            return
        self.write_gitignore()
        os.makedirs(self.cases_dir, exist_ok=True)
        atomic_write_json(os.path.join(self.out, "manifest.yaml"),
                          self.manifest())
        self.write_results()
        self.append_holdout_ledger()
        self.log("preflight", mode=mode,
                 traces_collected=self.trace_state["collected"],
                 cases=len(self.plan["cases"]))

    def prepare_http(self, invocation):
        base = invocation.get("base_url")
        if not isinstance(base, str) or not base:
            preflight_fail("adapter.invocation.base_url is required for "
                           "invocation.mode: http")
        # An ALLOWLIST, like md_to_html.py's: urlopen also speaks file:// and
        # ftp://, so `base_url: file:///etc/...` read a local file into every
        # response.json (2026-09-21 audit). Only the scheme is quoted back --
        # by now the value is resolved and may hold a credential.
        scheme = urllib.parse.urlsplit(base).scheme.lower()
        if scheme not in ("http", "https"):
            preflight_fail("adapter.invocation.base_url must be an http:// or "
                           f"https:// URL (its scheme is {scheme!r})")
        self.method, path = parse_endpoint(invocation)
        self.url = base.rstrip("/") + path
        self.request_template = load_body_template(invocation, "request_body")
        response_template = load_body_template(invocation, "response_body")
        self.answer_path = find_marker_path(response_template, ANSWER_MARKER)
        if self.answer_path is None:
            preflight_fail(
                "adapter.invocation.response_body must mark the answer field "
                f"with {ANSWER_MARKER!r}, e.g. '{{\"message\": \"{ANSWER_MARKER}\"}}'")
        self.trace_id_path = find_marker_path(response_template,
                                              TRACE_ID_MARKER)

    def prepare_function(self, invocation):
        app_root = (self.adapter.get("app") or {}).get("repo")
        if app_root and not os.path.isabs(app_root):
            app_root = os.path.normpath(os.path.join(self.state_dir, app_root))
        self.entrypoint = load_entrypoint(invocation, app_root)
        self.url = "function:{}".format(invocation.get("entrypoint"))
        self.method = "CALL"
        # A function entrypoint returns {"text": ...}; the request body is
        # still declared, so a callable that wants the session id or the
        # persona gets them under the names its adapter chose.
        raw = invocation.get("request_body")
        self.request_template = json.loads(raw) if isinstance(raw, str) \
            else {"message": "<user turn>", "session_id": "<uuid>"}
        self.answer_path = ("text",)

    def prepare_conversation(self, invocation, mode):
        """docs/multi-turn.md SS2: the adapter's invocation.conversation.

        A malformed block is a pre-flight failure, like a malformed body
        template: discovering it one conversation at a time would bill turn
        1 of every case to learn turn 2 can never be sent. The block is the
        ONLY switch that turns the driver on. It is named `conversation`,
        not `session`, because adapters/dotnet.md once shipped a `session`
        block, and hard rule 3's history is that declaring one must never be
        enough to turn a driver on by accident.
        """
        block = invocation.get("conversation")
        if block is None:
            return
        where = "adapter.invocation.conversation"
        if not isinstance(block, dict):
            preflight_fail(f"{where} must be an object, got "
                           f"{type(block).__name__}")
        unknown = sorted(set(block) - set(CONVERSATION_KEYS))
        if unknown:
            preflight_fail("unknown key(s) in {}: {}".format(
                where, ", ".join(unknown)))
        style = block.get("style")
        if style not in CONVERSATION_STYLES:
            preflight_fail("{}.style must be one of {}, got {!r}".format(
                where, "|".join(CONVERSATION_STYLES), style))
        source = block.get("session_from")
        if style == "server-id":
            if not isinstance(source, dict) or len(source) != 1 \
                    or not set(source) <= {"body", "header"} \
                    or not isinstance(next(iter(source.values())), str) \
                    or not next(iter(source.values())).strip():
                preflight_fail(
                    f"{where}.session_from must name exactly one of body: "
                    "<dotted path> or header: <name> for style: server-id, "
                    f"got {source!r}")
            if "header" in source and mode != "http":
                preflight_fail(f"{where}.session_from.header needs "
                               "invocation.mode: http; a function returns "
                               "no headers")
            if not template_mentions(self.request_template, SESSION_MARKER):
                preflight_fail(
                    f"{where}.style is server-id but the request body "
                    f"template has no {SESSION_MARKER} placeholder, so the "
                    "id turn 1 returns would never be sent on turn 2")
        elif source is not None:
            preflight_fail(f"{where}.session_from is for style: server-id "
                           f"only (style is {style!r})")
        if style == "client-id" \
                and not template_mentions(self.request_template, "<uuid>"):
            preflight_fail(
                f"{where}.style is client-id but the request body template "
                "has no <uuid> placeholder, so no turn would carry the "
                "conversation's id")
        if style == "cookie" and mode != "http":
            preflight_fail(f"{where}.style: cookie needs invocation.mode: "
                           "http")
        delay = block.get("turn_delay_s", 0)
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) \
                or delay < 0:
            preflight_fail(f"{where}.turn_delay_s must be a number >= 0, got "
                           f"{delay!r}")
        memory = block.get("memory")
        if memory is not None and memory not in CONVERSATION_MEMORY:
            preflight_fail("{}.memory must be one of {}, got {!r}".format(
                where, "|".join(CONVERSATION_MEMORY), memory))
        self.conversation = {"style": style, "session_from": source,
                             "turn_delay_s": delay, "memory": memory}

    def health_check(self, mode):
        """SS4.3. One trivial request, or the entrypoint import.

        With no declared `invocation.health_check`, this is a GET of base_url
        and ANY response counts as reachable -- including a 404. The check the
        contract needs here is "is the host up", and inventing a plausible API
        call to make instead would both spend money and guess at a shape the
        adapter never declared.
        """
        if self.dry_run:
            # SS1 is explicit that --dry-run calls the app ZERO times, and the
            # health check is an app call. Everything else in pre-flight still
            # runs, so a dry run still fails on an unresolved env var, an old
            # layout, or a malformed body template.
            return {"ok": None, "detail": "skipped (--dry-run)"}
        if mode == "function":
            return {"ok": True, "detail": "entrypoint imported"}
        declared = (self.adapter.get("invocation") or {}).get("health_check")
        if isinstance(declared, dict):
            method = str(declared.get("method", "GET")).upper()
            base = self.adapter["invocation"]["base_url"].rstrip("/")
            url = base + declared.get("path", "/")
            expect_status = declared.get("expect_status") or []
        else:
            method, url, expect_status = "GET", \
                self.adapter["invocation"]["base_url"], []
        headers = redact({}, set())
        # `url` is RESOLVED and these messages go to stdout, CI logs and an
        # agent's transcript: a credential in base_url
        # (https://user:${TOKEN}@host, ?key=${KEY}) was printed in clear by a
        # failed health check while every other path used record_url
        # (2026-09-21 audit). SS3 covers a message as much as a file.
        shown = unresolve(url, self.sent_refs)
        try:
            result = send_http(url, method, headers, None,
                               self.timeout(), self.execution["insecure_tls"])
        except TransportError as exc:
            preflight_fail(f"health check {method} {shown} failed: "
                           + unresolve(exc.message, self.sent_refs))
        if expect_status and result["status"] not in expect_status:
            preflight_fail(
                "health check {} {} returned {}, expected one of {}".format(
                    method, shown, result["status"], expect_status))
        return {"ok": True, "detail": "{} {}".format(method, result["status"]),
                "trace_id": self.read_trace_id(result)}

    def timeout(self):
        declared = self.execution.get("timeout_s")
        if declared:
            return declared
        return (self.adapter.get("invocation") or {}).get("timeout_s") or 60

    def trace_branch(self, health):
        """SS4.4. Decide, once for the whole run, whether traces are usable.

        The three trace-less triggers are declarations, not guesses: a
        view-only/absent store, no correlation (adapter hard rule 1 -- heuristic
        matching is forbidden), or a non-gen_ai convention with no mapping_shim
        (hard rule 5 -- raw spans must never reach normalize_trace.py, which
        would silently produce empty trajectories).
        """
        traces = self.adapter.get("traces") or {}
        source = traces.get("source")
        correlation = traces.get("correlation")
        convention = traces.get("convention")
        self.trace_state["source"] = source
        self.trace_state["correlation"] = correlation

        if source in UNIMPLEMENTED_TRACE_SOURCES:
            preflight_fail(
                "runner v1 queries traces.source: {} only (got: {!r}); "
                "declare view-only to run trace-less instead".format(
                    "|".join(QUERYABLE_TRACE_SOURCES), source))

        traceless = (source in TRACELESS_TRACE_SOURCES or not source
                     or correlation in (None, "none")
                     or (convention != "gen_ai" and not traces.get("mapping_shim")))
        if traceless:
            self.trace_state["collected"] = False
            self.trace_state["disabled_layers"] = list(TRACE_DEPENDENT_LAYERS)
            self.trace_state["reason"] = self.traceless_reason(
                source, correlation, convention, traces)
            return

        location = traces.get("location")
        if not location or not os.path.isfile(location):
            preflight_fail(f"traces.source: {source} declares location "
                           f"{location!r}, which is not a readable file")
        if self.dry_run:
            self.trace_state["collected"] = True
            self.trace_state["location"] = location
            self.trace_state["join_verified"] = False
            return
        # SS4.4's join check needs a real app call to join ON. Without a
        # declared health_check there is no such call, and inventing one is
        # spend the adapter never authorized -- so demand the declaration
        # rather than recording an unverified join as verified.
        if not health.get("trace_id"):
            preflight_fail(
                "traces are declared queryable with correlation "
                f"{correlation!r}, but no trace id (a non-empty string) "
                "came back from the health check; declare "
                "invocation.health_check as a real app call so the join can "
                "be verified before spend")
        if not self.trace_present(location, health["trace_id"]):
            preflight_fail(
                "health-check trace {} did not arrive in {} -- traces are "
                "declared queryable but do not join".format(
                    health["trace_id"], location))
        self.trace_state["collected"] = True
        self.trace_state["location"] = location

    @staticmethod
    def traceless_reason(source, correlation, convention, traces):
        if source in TRACELESS_TRACE_SOURCES or not source:
            return f"traces.source: {source}"
        if correlation in (None, "none"):
            return ("traces.correlation: none -- heuristic matching "
                    "forbidden (adapter hard rule 1)")
        return (f"traces.convention: {convention} with no mapping_shim (adapter hard "
                "rule 5)")

    def safety_gates(self):
        """SS4.5. Record what the environment forbids; SS7 acts on it."""
        environment = self.adapter.get("environment") or {}
        kind = environment.get("kind") or ""
        tools = self.adapter.get("tools") or []
        if kind.startswith("live"):
            self.never_live_tools = {
                tool.get("name") for tool in tools
                if isinstance(tool, dict)
                and tool.get("side_effects") == "never-live"}

    def write_gitignore(self):
        """SS4.6. Written BEFORE the first case file of the run exists.

        Raw per-case material (prompts, answers, traces) stays local when the
        adapter says the data may contain PII; manifest.yaml, results.json and
        the reports still commit normally.
        """
        if not (self.adapter.get("data") or {}).get("may_contain_pii"):
            return
        reports = os.path.join(self.state_dir, "reports")
        os.makedirs(reports, exist_ok=True)
        atomic_write(os.path.join(reports, ".gitignore"), "*/cases/\n")

    def append_holdout_ledger(self):
        """SS6 / decision D8: the runner records the look, not the skill.

        It is the only component that knows for certain the selection touched
        sealed ids, and an uncounted holdout run makes the N=5 reseal trigger a
        number nobody is keeping. One line, one O_APPEND write -- which is the
        best available answer to the second-writer problem (`analyze --unseal`)
        without a lock file, since a single short append is atomic on POSIX.
        """
        if not touches_seal(self.plan):
            return
        ledger = self.plan["paths"]["holdout_ledger"]
        path = ledger if os.path.isabs(ledger) \
            else os.path.join(self.state_dir, ledger)
        if not path.endswith(".jsonl"):
            bad_input(
                f"paths.holdout_ledger must be a .jsonl file (got {ledger!r}): the "
                "runner is stdlib-only and cannot safely append to a YAML "
                "document")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # SS6: "--resume does not append again". The look is the spend and
        # it was spent when this run id first reached pre-flight; every
        # resume used to append another, so a run resumed twice cost three
        # of the five looks before a reseal. Keyed on the run id's own row
        # rather than on the flag, so a first attempt that died before its
        # append still gets counted once.
        recorded = False
        if self.resume and os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    with contextlib.suppress(ValueError):
                        row = json.loads(line)
                        recorded = recorded or (
                            isinstance(row, dict)
                            and row.get("run_id") == self.plan["run_id"])
        if not recorded:
            row = {"run_id": self.plan["run_id"], "date": utc_now(),
                   "mode": self.plan["mode"], "reason": "run"}
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        with open(path, encoding="utf-8") as fh:
            self.holdout_looks = sum(1 for line in fh if line.strip())
        # stderr, not stdout: stdout carries the machine-readable {"error":...}
        # payload, and a caller parsing one shape must not find prose there.
        sys.stderr.write(
            "holdout look {}: {} total in {}\n".format(
                "already recorded for this run" if recorded else "recorded",
                self.holdout_looks, path))

    # -- SS10. manifest.yaml ----------------------------------------------
    def manifest(self):
        """JSON bytes in a .yaml file (decision D1, confirmed 2026-09-08).

        JSON is a subset of YAML 1.2, so the CI example's `yq -r '.run_id'`
        reads this unchanged and the stdlib -- which has no YAML writer -- can
        still produce it. The shipped run already does exactly this; renaming
        the file would touch six references for a cosmetic gain.
        """
        extra = self.plan.get("manifest_extra") or {}
        invocation = self.adapter.get("invocation") or {}
        environment = self.adapter.get("environment") or {}
        traces = dict(self.trace_state)
        return {
            "run_id": self.plan["run_id"],
            "mode": self.plan["mode"],
            "k": self.k,
            "gate": self.plan["gate"],
            "selecting_split": self.plan["selecting_split"],
            "started_at": self.started_at,
            # From the harness itself, never invented by the caller: run/SKILL
            # refuses a baseline diff across differing harness versions, so the
            # value has to come from the thing whose change invalidates it.
            "harness_version": HARNESS_VERSION,
            "plan_sha256": self.plan_sha256,
            "runner_version": RUNNER_VERSION,
            # Recorded so SS9's comparison.json condition is readable off the
            # tree: --verify has no plan and no argv, and a conditional
            # artifact whose condition is unknowable is a check that never runs.
            "baseline_verdicts": self.baseline_verdicts,
            "dataset_version": extra.get("dataset_version"),
            "cases": [{"id": case["id"],
                       "set": self.plan["selecting_split"],
                       "sha256": sha256_of(case)}
                      for case in self.plan["cases"]],
            "app": extra.get("app"),
            "prompt_snapshot_hashes": extra.get("prompt_snapshot_hashes"),
            "judge": extra.get("judge"),
            "rubric_versions": extra.get("rubric_versions"),
            "environment_kind": environment.get("kind"),
            "safe_to_attack": bool(environment.get("safe_to_attack")),
            "temperature": extra.get("temperature"),
            # Names only, never values (SS3). This block is what makes a run
            # reproducible by a colleague without handing them a credential.
            "identity_headers": {"names": sorted(self.secret_names),
                                 "source": "env"},
            "invocation": {"mode": invocation.get("mode"),
                           "url": self.record_url,
                           "timeout_s": self.timeout(),
                           "timeout_enforced":
                               invocation.get("mode") != "function"},
            "traces": traces,
            # docs/multi-turn.md SS2/SS6: how conversations were kept, and
            # whether memory outlives a session (`user`): then any case's
            # verdict may be contaminated by an earlier one, and the report
            # says so. null = undeclared, and no conversation ran.
            "conversation": self.conversation,
            "capability_matrix": self.plan["capability_matrix"],
            "scoring": self.plan["scoring"],
            "execution": self.execution,
            "cost_estimate": extra.get("cost_estimate"),
        }

    # -- trace collection --------------------------------------------------
    def read_trace_id(self, result):
        """Explicit correlation only -- adapter hard rule 1.

        There is no fallback that guesses a trace from timing or from the only
        recent trace in the store: a scored layer that silently used the WRONG
        trace is worse than a layer that reports it has none.

        A trace id is a string (W3C: 32 lowercase hex). A number or an object
        at the declared field is not one, and it used to reach `in` and
        normalize_trace.py's argv as-is -- a TypeError, exit 1. Stringifying
        it would be a guess (a numeric id has already lost any leading zero),
        so it is no trace id: infra_incomplete, like any other missing join.
        """
        value = self.raw_trace_id(result)
        return value if isinstance(value, str) and value else None

    def raw_trace_id(self, result):
        correlation = (self.adapter.get("traces") or {}).get("correlation")
        body = result.get("body")
        if isinstance(correlation, str) \
                and correlation.startswith("response-field:"):
            field = correlation.split(":", 1)[1]
            return (body or {}).get(field) if isinstance(body, dict) else None
        if correlation == "traceparent-echo":
            # Field names are case-insensitive (RFC 9110 SS5.1), and W3C
            # Trace Context says a receiver MUST accept any case: Go's
            # net/http sends `Traceparent`. The dict keeps the sender's case.
            header = {name.lower(): value for name, value
                      in (result.get("headers") or {}).items()}.get(
                          "traceparent")
            parts = header.split("-") if isinstance(header, str) else []
            return parts[1] if len(parts) >= 3 else None
        if self.trace_id_path and isinstance(body, dict):
            return extract_path(body, self.trace_id_path)
        return None

    @staticmethod
    def trace_present(location, trace_id):
        """A TRANSPORT check: are this trace's bytes in the store yet?

        Deliberately not a span-model check. normalize_trace.py owns what a
        usable trace is (it does its own trace-id filtering and its own
        conservation checks), and a second, weaker model of the same thing here
        would eventually disagree with it about whether a trace arrived.
        """
        try:
            with open(location, encoding="utf-8") as fh:
                return trace_id in fh.read()
        except OSError:
            return False

    def wait_for_quiescence(self, location):
        """Poll the store until it stops growing, or give up at max_wait_s."""
        completeness = (self.adapter.get("traces") or {}).get("completeness") \
            or {}
        quiet_s = (completeness.get("quiescence_ms") or 3000) / 1000.0
        max_wait = completeness.get("max_wait_s") or 30
        deadline = time.time() + max_wait
        last = None
        stable_since = None
        while time.time() < deadline:
            try:
                stat = os.stat(location)
                current = (stat.st_size, stat.st_mtime)
            except OSError:
                current = None
            now = time.time()
            if current != last:
                last, stable_since = current, now
            elif stable_since is not None and now - stable_since >= quiet_s:
                return True
            time.sleep(min(0.05, quiet_s))
        return False

    def collect_trace(self, case_dir, trace_id):
        """Copy the store's bytes for this case and say whether it joined.

        Returns (collected, reason). A trace that never arrives is
        infra_incomplete for the trace-dependent layers only -- never a fail
        (SS7). The case's other layers still score.
        """
        location = self.trace_state.get("location")
        if not trace_id:
            return False, ("no trace id (a non-empty string) on the "
                           "response; correlation is "
                           "{!r}".format(self.trace_state["correlation"]))
        quiet = self.wait_for_quiescence(location)
        if not self.trace_present(location, trace_id):
            return False, ("trace {} not in {} after {}".format(
                trace_id, location,
                "quiescence" if quiet else "max_wait_s"))
        try:
            with open(location, encoding="utf-8") as fh:
                spans = fh.read()
        except OSError as exc:
            return False, f"cannot read trace store: {exc}"
        atomic_write(os.path.join(case_dir, "trace.json"), spans)
        return True, None

    # -- SS7. The serial execute loop -------------------------------------
    def ordered_cases(self):
        """Canaries first (contract SS7), then the plan's own order.

        Canaries measure the HARNESS, not the app; running them first means a
        drifted harness stops the run before it spends the suite's budget
        producing numbers nobody should read.
        """
        cases = self.plan["cases"]
        canaries = [case for case in cases if is_canary(case)]
        return canaries + [case for case in cases if not is_canary(case)]

    def skip_reason(self, case):
        """SS7's `skipped` rows. Every one of these is a REFUSAL, not a fail.

        Scoring a case the harness declined to run would manufacture a failure
        the app never had -- the one outcome this harness must never produce.
        """
        environment = self.adapter.get("environment") or {}
        category = case.get("category")
        if category in ATTACK_CATEGORIES \
                and not environment.get("safe_to_attack"):
            return (f"environment.safe_to_attack is false; category {category!r} "
                    "refuses to run")
        turns = conversation_turns(case)
        if self.never_live_tools:
            named = set(case.get("available_tools") or [])
            # A conversation names tools on every turn's expect, not only
            # the final one's.
            expects = [e for _, e in turns] if turns is not None \
                else [case.get("expect") or {}]
            for expect in expects:
                tools = expect.get("tools") or {}
                for key in ("subset", "order", "forbidden"):
                    named.update(tools.get(key) or [])
            blocked = sorted(named & self.never_live_tools)
            if blocked:
                return ("never-live tool(s) {} with environment.kind {!r} "
                        "(adapter hard rule 2)".format(
                            ", ".join(blocked), environment.get("kind")))
        if turns is not None:
            if self.conversation is None:
                # docs/multi-turn.md SS2: the block is the only switch. An
                # adapter that never said how the app keeps a conversation
                # would have every turn land in a fresh one, and the case
                # would score "forgot the context" for a harness gap.
                return (f"multi-turn case ({len(turns)} turns) but the "
                        "adapter declares no invocation.conversation, so "
                        "there is no way to keep the turns in one "
                        "conversation (adapter hard rule 3); discover "
                        "writes the block")
            if len(turns) < 2 or not all(text for text, _ in turns):
                # Not a second linter: the one shape that would SEND
                # something nobody wrote (an empty user turn) is refused.
                return ("input.turns is malformed (fewer than two turns, or "
                        "a turn with no `user` text); validate_cases.py "
                        "names the defect")
            if len(turns) > self.execution["max_turns"]:
                # SS3: a bound for cost and loop safety whatever the author
                # wrote. The validator's WARN above 8 is the guidance.
                return (f"multi-turn case ({len(turns)} turns) is above "
                        f"execution.max_turns {self.execution['max_turns']}")
        elif user_turn_count(case) > 1:
            # Two user messages in input.messages are not a conversation:
            # case_text() would send the LAST one and the earlier turns would
            # vanish, scoring a truncated conversation as an ordinary pass
            # or fail. A conversation is input.turns (case-format.md), which
            # is the one shape the driver sends turn by turn.
            return (f"multi-turn case ({user_turn_count(case)} user turns in "
                    "input.messages): only the last turn would reach the "
                    "app (adapter hard rule 3) -- write the conversation as "
                    "input.turns")
        else:
            context = context_messages_reason(case)
            if context:
                return context
        if case.get("identity") and not self.identity_map:
            return ("case declares an identity but the adapter has no "
                    "invocation.identity_map; running it under the default "
                    "identity would score the wrong persona")
        return None

    def invoke_once(self, case, repeat):
        """One repeat: build, send with bounded retries, extract, record."""
        session_id = str(uuid.uuid4())
        persona = case.get("persona") or (case.get("identity") or {}).get("role")
        values = {"<user turn>": case_text(case), "<uuid>": session_id,
                  "<persona>": persona, "<case id>": case["id"],
                  SESSION_MARKER: None}
        body = render_template(self.request_template, values)
        headers = build_headers(self.adapter, case, self.identity_map,
                                self.auth_values)
        request = {"case_id": case["id"], "repeat": repeat, "persona": persona,
                   "headers_sent": redact(headers, self.secret_names),
                   "url": self.record_url, "method": self.method,
                   "body": render_template(self.record_template, values),
                   "sent_at": utc_now(), "sent": True}

        max_attempts = self.execution["max_attempts"]
        backoff = self.execution["backoff_s"]
        result, error, crashed, attempts = None, None, False, 0
        answers_5xx = []        # (status, body) of each 5xx attempt
        while attempts < max_attempts:
            attempts += 1
            try:
                if self.entrypoint is not None:
                    result = send_function(self.entrypoint, body)
                else:
                    result = send_http(self.url, self.method, headers, body,
                                       self.timeout(),
                                       self.execution["insecure_tls"])
            except TransportError as exc:
                result, error = None, exc.message
                crashed = crashed or exc.kind == "connection"
            else:
                status = result["status"]
                # A 4xx other than 429 is NOT retried: it is a response, and
                # often the expected one (the field test asserts a deliberate
                # 400 on OOS). Retrying it would triple the spend on every
                # correctly-refused case.
                if status != 429 and not 500 <= status < 600:
                    error = None
                    break
                error = f"HTTP {status}"
                if status != 429:
                    answers_5xx.append((status, body_key(result["body"])))
            if attempts < max_attempts:
                wait = retry_wait(backoff[attempts - 1], result)
                self.log("retry", case_id=case["id"], repeat=repeat,
                         attempt=attempts, error=error, wait_s=wait)
                time.sleep(wait)

        response = {"case_id": case["id"], "repeat": repeat,
                    "status": result["status"] if result else None,
                    "latency_s": result["latency_s"] if result else None,
                    "attempts": attempts, "retry_count": attempts - 1,
                    "body": result["body"] if result else None,
                    "answer": None, "trace_id": None, "error": error,
                    "crashed": crashed and error is not None}
        if result is not None:
            answer = extract_path(result["body"], self.answer_path)
            response["answer"] = answer if isinstance(answer, str) else \
                (None if answer is None else json.dumps(answer,
                                                        ensure_ascii=False))
            response["trace_id"] = self.read_trace_id(result)
        # Every attempt got the same 5xx, body and all -- a timeout, a 429 or
        # a different 5xx on any one of them breaks the run. repeated_5xx()
        # reads this across the repeats (F-165); it is not in response.json.
        same_5xx = answers_5xx[0] if (
            error is not None and len(answers_5xx) == attempts
            and len(set(answers_5xx)) == 1) else None
        return {"request": request, "response": response, "error": error,
                "crashed": crashed and error is not None,
                "same_5xx": same_5xx}

    # -- docs/multi-turn.md SS3. One conversation, turn by turn ------------
    def send_one(self, body, headers):
        """One request, no retry: a conversation retries WHOLE (SS3)."""
        if self.entrypoint is not None:
            return send_function(self.entrypoint, body)
        return send_http(self.url, self.method, headers, body, self.timeout(),
                         self.execution["insecure_tls"])

    def read_session(self, result):
        """A server-id app's session id, off turn 1's response, or None.

        Read ONLY from the declared place (session_from): a body path or a
        response header, matched case-insensitively (RFC 9110 SS5.1). A
        string or an integer id; anything else is no id, never a guess."""
        source = self.conversation["session_from"]
        if "header" in source:
            value = {name.lower(): v for name, v in
                     (result.get("headers") or {}).items()}.get(
                         source["header"].lower())
        else:
            value = dotted_get(result.get("body"), source["body"])
        if isinstance(value, bool):
            return None
        if isinstance(value, int) or (isinstance(value, str)
                                      and value.strip()):
            return value
        return None

    def session_source(self):
        source = self.conversation["session_from"]
        kind = "header" if "header" in source else "body"
        return f"session_from.{kind} {source[kind]!r}"

    def turn_response(self, case, repeat, turn, result, error, crashed,
                      attempts):
        """A turn's response.json, in invoke_once's shape plus `turn`."""
        response = {"case_id": case["id"], "repeat": repeat, "turn": turn,
                    "status": result["status"] if result else None,
                    "latency_s": result["latency_s"] if result else None,
                    "attempts": attempts, "retry_count": attempts - 1,
                    "body": result["body"] if result else None,
                    "answer": None, "trace_id": None, "error": error,
                    "crashed": crashed and error is not None}
        if result is not None:
            answer = extract_path(result["body"], self.answer_path)
            response["answer"] = answer if isinstance(answer, str) else \
                (None if answer is None else json.dumps(answer,
                                                        ensure_ascii=False))
            response["trace_id"] = self.read_trace_id(result)
        return response

    def score_turn(self, case, turn, expect, record, scratches, shared):
        """Score one sent turn with the existing layers (SS3 step 2).

        The turn's expect -- a checkpoint, or the final turn's top-level
        expect, with every_turn overlaid -- is written into the turn's own
        scratch dir and handed to the scorers exactly as a single-turn
        case's is. `shared` is the reason this conversation's turns share a
        trace id (SS5); then this turn reads no trace at all."""
        scratch = tempfile.mkdtemp(prefix="evalup-run-")
        scratches.append(scratch)
        expect_path = os.path.join(scratch, "expect.json")
        atomic_write_json(expect_path, expect)
        atomic_write(os.path.join(scratch, "answer.txt"),
                     record["response"]["answer"] or "")
        turn_case = dict(case, expect=expect)
        trace_collected, trace_reason = False, None
        if record["error"] is None and self.trace_state["collected"] \
                and not shared:
            trace_collected, trace_reason = self.collect_trace(
                scratch, record["response"]["trace_id"])
        layers = self.score_layers(turn_case, record, None, scratch,
                                   expect_path, trace_collected, trace_reason)
        scored = {"turn": turn, "record": record, "scratch": scratch,
                  "expect": expect, "layers": layers,
                  "trace_collected": trace_collected,
                  "trace_reason": trace_reason}
        if shared:
            scored["trace_reason"] = shared
            self.unscorable_trace_layers(scored, shared)
        scored["verdict"] = roll_up(layers, None)
        return scored

    @staticmethod
    def unscorable_trace_layers(scored, reason):
        """SS5: turns that share a trace id cannot be told apart in it, so
        every trace-dependent row of the case is `unscorable` with that
        reason -- honest degradation, the rule score_authz follows. `loops`
        is in TRAJECTORY_LAYERS and goes too: this runs only on a run that
        collects traces, where every turn would have had a `loops` row. A
        row the matrix already disabled keeps the matrix's own reason, and a
        layer the turn does not assert stays n/a."""
        layers = scored["layers"]
        for name in TRAJECTORY_LAYERS:
            verdict = layers[name].get("verdict")
            if verdict == UNSCORABLE or (verdict == NA and name != "loops"):
                continue
            layers[name] = unscorable(name, reason)
        scored["verdict"] = roll_up(layers, None)

    def conversation_attempt(self, case, repeat, turns, attempt, scratches):
        """One attempt at the whole conversation: fresh <uuid>, no
        <session>, turn by turn until the end or a stop (SS3).

        Returns {"turns": [scored turn...], "retry": None | {...},
        "stop_reason": str|None, "session_missing": bool}. A retryable
        outcome on ANY turn (a transport failure, a 429, a 5xx) abandons
        the attempt: the app may already have stored the turn it failed on,
        so re-sending that turn alone would score the rest on a corrupted
        history."""
        conversation = self.conversation
        persona = case.get("persona") or (case.get("identity") or {}).get("role")
        session_id = str(uuid.uuid4())
        session = None
        base_headers = build_headers(self.adapter, case, self.identity_map,
                                     self.auth_values)
        # SS2: one cookie jar per conversation ATTEMPT -- a restart opens a
        # new conversation, so it must not carry the abandoned one's cookie.
        jar = http.cookiejar.CookieJar() \
            if conversation["style"] == "cookie" else None
        static_cookie = base_headers.get("Cookie")
        sent, seen, shared = [], {}, None
        out = {"turns": sent, "retry": None, "stop_reason": None,
               "session_missing": False}
        for index, (text, expect) in enumerate(turns, 1):
            if index > 1 and conversation["turn_delay_s"]:
                time.sleep(conversation["turn_delay_s"])
            values = {"<user turn>": text, "<uuid>": session_id,
                      "<persona>": persona, "<case id>": case["id"],
                      SESSION_MARKER: session}
            body = render_template(self.request_template, values)
            headers = dict(base_headers)
            if jar is not None:
                # Recomposed every turn: a Max-Age=0 deletes from the jar.
                headers.pop("Cookie", None)
                cookie = merged_cookie_header(jar, self.url, static_cookie)
                if cookie:
                    headers["Cookie"] = cookie
            request = {"case_id": case["id"], "repeat": repeat, "turn": index,
                       "attempt": attempt, "persona": persona,
                       # Cookie is redacted under the cookie style whatever
                       # the auth: the app's session cookie is a credential.
                       "headers_sent": redact(
                           headers, self.secret_names
                           | ({"Cookie"} if jar is not None else set())),
                       "url": self.record_url, "method": self.method,
                       "body": render_template(self.record_template, values),
                       "sent_at": utc_now(), "sent": True}
            try:
                result = self.send_one(body, headers)
            except TransportError as exc:
                out["retry"] = {"turn": index, "request": request,
                                "result": None, "error": exc.message,
                                "crashed": exc.kind == "connection",
                                "five": None}
                return out
            if jar is not None:
                absorb_cookies(jar, self.url, result.get("set_cookies"))
            status = result["status"]
            if status == 429 or 500 <= status < 600:
                out["retry"] = {"turn": index, "request": request,
                                "result": result, "error": f"HTTP {status}",
                                "crashed": False,
                                "five": None if status == 429 else
                                (status, body_key(result["body"]), index)}
                return out
            record = {"request": request,
                      "response": self.turn_response(case, repeat, index,
                                                     result, None, False,
                                                     attempt),
                      "error": None, "crashed": False, "same_5xx": None}
            trace_id = record["response"]["trace_id"]
            if self.trace_state["collected"] and trace_id:
                if trace_id in seen and not shared:
                    shared = (f"turns {seen[trace_id]} and {index} returned "
                              f"the same trace id {trace_id}, so no span in "
                              "it can be attributed to one turn "
                              "(docs/multi-turn.md SS5)")
                    for earlier in sent:
                        self.unscorable_trace_layers(earlier, shared)
                seen.setdefault(trace_id, index)
            scored = self.score_turn(case, index, expect, record, scratches,
                                     shared)
            sent.append(scored)
            if scored["verdict"] in STOPPING:
                # A fail means the app went off script, and anything sent
                # after it is a turn built on a derailed conversation.
                # `unscored` is NOT here: the harness could not look, which
                # is not the app failing.
                out["stop_reason"] = turn_stop_reason(scored)
                return out
            if conversation["style"] == "server-id" and index == 1 \
                    and index < len(turns):
                session = self.read_session(result)
                if session is None:
                    out["session_missing"] = True
                    out["stop_reason"] = (
                        "turn 1's response carried no session id at "
                        f"{self.session_source()}, so turn 2 was never "
                        "sent: without the session it would open a new "
                        "conversation and score 'forgot the context'")
                    return out
        return out

    def converse(self, case, repeat, scratches):
        """One repeat of a conversation, with whole-conversation retries.

        `max_attempts` counts CONVERSATION attempts, with `backoff_s`
        between them; on exhaustion the case is `infra_error`, as today.
        The case verdict is roll_up() over the union of every sent turn's
        layers, keyed `t<n>.<layer>` -- which on fail, infra or all-pass is
        the stop rule's answer, and on `unscored` is today's single-turn
        semantics. Returns the attempt dict execute_case folds, carrying
        the DECIDING turn (the stopping turn, or the last) as its record."""
        turns = conversation_turns(case)
        max_attempts = self.execution["max_attempts"]
        backoff = self.execution["backoff_s"]
        attempt, crashed, endings = 0, False, []
        while True:
            attempt += 1
            outcome = self.conversation_attempt(case, repeat, turns, attempt,
                                                scratches)
            retry = outcome["retry"]
            if retry is None:
                break
            crashed = crashed or retry["crashed"]
            endings.append(retry["five"])
            if attempt >= max_attempts:
                break
            wait = retry_wait(backoff[attempt - 1], retry["result"])
            self.log("retry", case_id=case["id"], repeat=repeat,
                     attempt=attempt, turn=retry["turn"],
                     error=retry["error"], wait_s=wait,
                     restart="conversation")
            time.sleep(wait)

        sent = outcome["turns"]
        if retry is not None:
            # Exhausted: the turn that kept failing is the deciding one, and
            # every layer it claims is infra_error (score_layers' own rule
            # for a record carrying an error).
            same = endings[0] if len(endings) == attempt and endings[0] \
                and len(set(endings)) == 1 else None
            record = {"request": retry["request"],
                      "response": self.turn_response(
                          case, repeat, retry["turn"], retry["result"],
                          retry["error"], crashed, attempt),
                      "error": retry["error"], "crashed": crashed,
                      "same_5xx": same}
            sent.append(self.score_turn(case, retry["turn"],
                                        turns[retry["turn"] - 1][1], record,
                                        scratches, None))
            outcome["stop_reason"] = "turn {}: {} on all {} conversation " \
                "attempt(s)".format(retry["turn"], retry["error"], attempt)
        for turn in sent:
            # Every turn's response says how many attempts the conversation
            # took, so repeated_5xx() and a reader both see it on any file.
            turn["record"]["response"]["attempts"] = attempt
            turn["record"]["response"]["retry_count"] = attempt - 1
        deciding = sent[-1]
        union = {f"t{turn['turn']}.{name}": layer
                 for turn in sent for name, layer in turn["layers"].items()}
        verdict = UNSCORED if outcome["session_missing"] \
            else roll_up(union, None)
        latencies = [turn["record"]["response"]["latency_s"] for turn in sent
                     if turn["record"]["response"]["latency_s"] is not None]
        return {"n": repeat, "record": deciding["record"],
                "scratch": deciding["scratch"], "layers": union,
                "verdict": verdict,
                "trace_collected": deciding["trace_collected"],
                "trace_reason": deciding["trace_reason"],
                "multi_turn": True, "turns": sent, "turns_sent": len(sent),
                "failed_turn": deciding["turn"]
                if deciding["verdict"] in STOPPING else None,
                "stop_reason": outcome["stop_reason"],
                "latency_s": round(sum(latencies), 3) if latencies else None}

    def execute_case(self, case):
        """Run one case (k repeats), score it, and write its directory.

        SS6: a case directory containing verdict.json is complete, by
        definition. Resume (SS8) and the completeness check (SS9) both key off
        exactly that fact, so it is the last atomic write here and nothing is
        written after it.

        Every repeat is scored in its OWN scratch directory, outside the run
        tree, and only the representative repeat's artifacts are copied in.
        SS6 says the run tree holds no other files, and repeats/<n>/ is
        declared as three files -- so a scorer input materialized for repeat 3
        must not land beside them.
        """
        case_id = case["id"]
        case_dir = os.path.join(self.cases_dir, case_id)
        os.makedirs(case_dir, exist_ok=True)
        expect = case.get("expect") or {}
        # SS9/D9: expect.json and answer.txt live in the case dir so any run
        # can be RE-SCORED without re-invoking the app -- which is also how a
        # scorer bug fix gets applied to historical runs. Written FIRST because
        # it is also the scorers' own argv for every repeat: one file, so the
        # k repeats cannot be scored against k copies of it.
        expect_path = os.path.join(case_dir, "expect.json")
        atomic_write_json(expect_path, expect)
        self.log("case_start", case_id=case_id, k=self.k)

        scratches = []
        try:
            skip = self.skip_reason(case)
            if skip:
                attempts = [self.scored_attempt(case, self.skipped_record(case),
                                                expect_path, skip, scratches)]
            else:
                self.attempted += 1
                if conversation_turns(case) is not None:
                    attempts = [self.converse(case, n, scratches)
                                for n in range(1, self.k + 1)]
                else:
                    attempts = [
                        self.scored_attempt(case, self.invoke_once(case, n),
                                            expect_path, None, scratches)
                        for n in range(1, self.k + 1)]
                if any(a["record"]["crashed"] for a in attempts) \
                        and case.get("category") in CRASH_RATE_CATEGORIES:
                    self.crashes += 1

            # SS6: the case-level files are the REPRESENTATIVE repeat -- the
            # first repeat whose verdict IS the case verdict (SS5.5's fold),
            # so response.json shows the evidence that decided the case.
            # Deterministic, and it makes the report's example excerpt the
            # informative one rather than an arbitrary one.
            case_verdict = fold_repeats([a["verdict"] for a in attempts])
            chosen = next(a for a in attempts if a["verdict"] == case_verdict)
            if not skip and self.k > 1:
                self.write_repeats(case_dir, case, attempts)
            self.publish(case_dir, chosen)
            if chosen.get("multi_turn"):
                self.write_turns(os.path.join(case_dir, "turns"), case,
                                 chosen, traces=True)

            verdict = self.build_verdict(case, chosen, skip)
            if not skip and self.k > 1:
                verdict["repeats"] = [
                    dict({"n": a["n"], "verdict": a["verdict"],
                          "layers": a["layers"]},
                         **({"turns_sent": a["turns_sent"],
                             "failed_turn": a["failed_turn"]}
                            if a.get("multi_turn") else {}))
                    for a in attempts]
            verdict["repeated_5xx"] = None if skip else repeated_5xx(attempts)
            self.record_routing_row(verdict)
            atomic_write_json(os.path.join(case_dir, "verdict.json"), verdict)
        finally:
            for scratch in scratches:
                shutil.rmtree(scratch, ignore_errors=True)

        self.verdicts.append(verdict)
        self.log("case_done", case_id=case_id, verdict=verdict["verdict"])
        return verdict

    def skipped_record(self, case):
        """A refused case still writes request/response, marked `sent: false`.

        REQUIRED_PER_CASE has no exceptions on purpose (SS9): an exception is
        how a required file rots. So the request the runner WOULD have sent is
        recorded -- which is also the useful thing to read when arguing about
        whether the refusal was right -- and the response says plainly that
        nothing was sent.
        """
        persona = case.get("persona") or (case.get("identity") or {}).get("role")
        turns = conversation_turns(case)
        text = turns[0][0] if turns else case_text(case)
        values = {"<user turn>": text, "<uuid>": None,
                  "<persona>": persona, "<case id>": case["id"],
                  SESSION_MARKER: None}
        headers = build_headers(self.adapter, case, self.identity_map,
                                self.auth_values)
        request = {"case_id": case["id"], "repeat": 1, "persona": persona,
                   "headers_sent": redact(headers, self.secret_names),
                   "url": self.record_url, "method": self.method,
                   "body": render_template(self.record_template, values),
                   "sent_at": None, "sent": False}
        response = {"case_id": case["id"], "repeat": 1, "status": None,
                    "latency_s": None, "attempts": 0, "retry_count": 0,
                    "body": None, "answer": None, "trace_id": None,
                    "error": None, "crashed": False,
                    # SS6 marks both files; the response used to carry only
                    # attempts: 0 and leave the reader to infer it (F-126).
                    "sent_at": None, "sent": False}
        return {"request": request, "response": response, "error": None,
                "crashed": False}

    def scored_attempt(self, case, record, expect_path, skip, scratches):
        """One repeat: materialize its scorer inputs, then score it (SS5)."""
        scratch = tempfile.mkdtemp(prefix="evalup-run-")
        scratches.append(scratch)
        atomic_write(os.path.join(scratch, "answer.txt"),
                     record["response"]["answer"] or "")
        trace_collected, trace_reason = False, None
        if not skip and self.trace_state["collected"]:
            trace_collected, trace_reason = self.collect_trace(
                scratch, record["response"]["trace_id"])
        layers = self.score_layers(case, record, skip, scratch, expect_path,
                                   trace_collected, trace_reason)
        return {"n": record["response"]["repeat"], "record": record,
                "scratch": scratch, "layers": layers,
                "verdict": roll_up(layers, skip),
                "trace_collected": trace_collected,
                "trace_reason": trace_reason}

    @staticmethod
    def publish(case_dir, attempt):
        """Copy the representative repeat's artifacts into the case dir.

        answer.txt is REQUIRED_PER_CASE and the three conditional files are
        REQUIRED_IF, so this is the one place their presence is decided --
        which is what lets SS9's check read the same condition off the tree
        later without a plan.
        """
        record = attempt["record"]
        atomic_write_json(os.path.join(case_dir, "request.json"),
                          record["request"])
        atomic_write_json(os.path.join(case_dir, "response.json"),
                          record["response"])
        # A conversation's case-level files are its DECIDING turn's
        # (docs/multi-turn.md SS7), expect.json included: that turn's expect
        # is what its answer and actual were scored against.
        names = ("answer.txt", "trace.json", "trajectory.json", "actual.json")
        if attempt.get("multi_turn"):
            names += ("expect.json",)
        for name in names:
            source = os.path.join(attempt["scratch"], name)
            if os.path.isfile(source):
                shutil.copyfile(source, os.path.join(case_dir, name))

    def write_repeats(self, case_dir, case, attempts):
        """SS6: repeats/ exists iff k > 1, and holds EVERY repeat.

        Including the first -- no asymmetry between "the run" and "the extra
        runs", which is the shape that makes a repeat directory readable
        without knowing which index the case-level files came from.
        """
        for attempt in attempts:
            repeat_dir = os.path.join(case_dir, "repeats", str(attempt["n"]))
            os.makedirs(repeat_dir, exist_ok=True)
            atomic_write_json(os.path.join(repeat_dir, "request.json"),
                              attempt["record"]["request"])
            atomic_write_json(os.path.join(repeat_dir, "response.json"),
                              attempt["record"]["response"])
            atomic_write_json(os.path.join(repeat_dir, "verdict.json"),
                              {"case_id": case["id"], "repeat": attempt["n"],
                               "verdict": attempt["verdict"],
                               "layers": attempt["layers"]})
            if attempt.get("multi_turn"):
                self.write_turns(os.path.join(repeat_dir, "turns"), case,
                                 attempt, traces=False)

    def write_turns(self, turns_dir, case, attempt, traces):
        """docs/multi-turn.md SS7: every sent turn's own five files, so a
        scorer fix re-scores a historical conversation without re-invoking
        the app (D9), plus its trace under the case-level turns/ (`traces`).
        A repeat's turns hold the five, as its own directory holds three."""
        expected = self.trace_state["collected"]
        for turn in attempt["turns"]:
            turn_dir = os.path.join(turns_dir, str(turn["turn"]))
            os.makedirs(turn_dir, exist_ok=True)
            record = turn["record"]
            atomic_write_json(os.path.join(turn_dir, "request.json"),
                              record["request"])
            atomic_write_json(os.path.join(turn_dir, "response.json"),
                              record["response"])
            names = ("expect.json", "answer.txt") \
                + (TURN_TRACE_FILES if traces else ())
            for name in names:
                source = os.path.join(turn["scratch"], name)
                if os.path.isfile(source):
                    shutil.copyfile(source, os.path.join(turn_dir, name))
            atomic_write_json(os.path.join(turn_dir, "verdict.json"), {
                "case_id": case["id"], "repeat": attempt["n"],
                "turn": turn["turn"], "verdict": turn["verdict"],
                "layers": turn["layers"],
                "trace": {"expected": expected,
                          "collected": turn["trace_collected"],
                          "reason": turn["trace_reason"] or (
                              None if turn["trace_collected"]
                              else self.trace_state.get("reason"))}})

    # -- SS5. The layer table ---------------------------------------------
    def capability_blocked_by(self, layer):
        """SS5: a layer is enabled iff its capability_matrix entry's `enabled`
        is not false. Returns the matrix's own `blocked_by` when it is not.

        The entry is found through MATRIX_KEY_OF_LAYER, not under the layer's
        own name: the matrix says `answer_quality` where this table has
        `answer`, `rules` and `judged`, and `http` and `loops` have no entry
        to find.

        The string is COPIED, never composed here: `unscorable` means "somebody
        decided this layer is off and said why", and a reason the runner made
        up would let the matrix and the report disagree about that why.
        """
        key = MATRIX_KEY_OF_LAYER[layer]
        if key is None:
            return None
        block = self.plan["capability_matrix"].get(key)
        if not isinstance(block, dict) or block.get("enabled") is not False:
            return None
        return block.get("blocked_by") or f"capability_matrix.{key}.enabled: false"

    def run_scorer(self, script, argv, layer):
        """SS5.1. Shell out, parse stdout as JSON on EVERY return code.

        The scorers' error contract puts errors on STDOUT as {"error": ...} and
        signals failure with exit 2 (_common.die). A runner that read stderr on
        failure would get an empty string and report nothing -- which is how a
        malformed expect block becomes a silent hole in a report.

        Returns the scorer's object VERBATIM on success: the runner reads only
        `verdict` out of it for the rollup, and invents no fields and drops
        none. The scorers are the definition of what a number means; a second
        implementation here would be a second, divergent definition.
        """
        path = os.path.join(self.plan["paths"]["scripts_dir"], script)
        command = [sys.executable, path, *[str(x) for x in argv]]
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True,
                timeout=self.plan["scoring"]["scorer_timeout_s"])
        except subprocess.TimeoutExpired:
            return self.scorer_error(
                layer, script, argv,
                "scorer timed out after {}s".format(
                    self.plan["scoring"]["scorer_timeout_s"]))
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            noise = (proc.stdout or proc.stderr)[:400]
            return self.scorer_error(
                layer, script, argv,
                f"scorer exited {proc.returncode} and its stdout is not "
                f"JSON: {noise!r}")
        self.log("scorer", layer=layer, scorer=script, rc=proc.returncode)
        if proc.returncode != 0:
            return self.scorer_error(
                layer, script, argv,
                payload.get("error") if isinstance(payload, dict)
                else str(payload), rc=proc.returncode)
        return payload

    def scorer_error(self, layer, script, argv, message, rc=2):
        """A scorer that failed is counted, recorded, and does NOT stop the run.

        One malformed expect block must not throw away 200 cases of spend --
        but the run exits 7 at the end (SS11), because a run with scorer errors
        has numbers nobody should quote.
        """
        self.scorer_errors += 1
        self.log("scorer", layer=layer, scorer=script, rc=rc, error=message)
        return {"layer": layer, "verdict": LAYER_ERROR, "scorer": script,
                "argv": [str(x) for x in argv], "error": message}

    def score_layers(self, case, record, skip, scratch, expect_path,
                     trace_collected, trace_reason):
        """SS5's table, walked top to bottom for one repeat.

        Order of decision, per layer, and the order matters:
          not applicable        -> n/a
          capability disabled   -> unscorable, blocked_by from the matrix
          input unavailable     -> unscored, with a reason
          otherwise             -> the scorer's own object, verbatim
        """
        expect = case.get("expect") or {}
        applicable = set(applicable_layers(case, trace_collected))
        layers = {name: na(name) for name in LAYER_ORDER}

        blocked = {}
        for name in applicable:
            reason = self.capability_blocked_by(name)
            if reason:
                blocked[name] = reason
                layers[name] = unscorable(name, reason)

        # A refusal and a transport failure both stop before scoring. Neither
        # is ever a `fail`: scoring a case the harness declined to run, or one
        # the app never answered, would manufacture a failure the app never had
        # -- the one outcome this harness must never produce.
        if skip:
            for name in applicable - set(blocked):
                layers[name] = {"layer": name, "verdict": SKIPPED,
                                "reason": skip}
            return layers
        # SS9(b) requires actual.json of every SENT case carrying expect.result,
        # whatever became of the execution layer -- so it is written on the
        # two paths below that never reach the scorer as well. It used to be
        # written only for a live layer, and a run with one execution case
        # whose app call failed, or with `execution` disabled (every
        # `make_plan.py --layer X` plan), exited 6 over a file nothing wrote.
        actual_path = os.path.join(scratch, "actual.json")
        if record["error"]:
            for name in applicable - set(blocked):
                layers[name] = {"layer": name, "verdict": INFRA_ERROR,
                                "reason": record["error"]}
            if "execution" in applicable:
                atomic_write_json(actual_path, {
                    "missing": True,
                    "reason": "the app call failed, so there is no result "
                              "to extract: {}".format(record["error"])})
            return layers

        trajectory_path = os.path.join(scratch, "trajectory.json")
        answer_path = os.path.join(scratch, "answer.txt")
        scoring = self.plan["scoring"]

        def live(name):
            return name in applicable and name not in blocked

        # -- normalize, once: every trajectory layer reads its output --------
        trajectory_gap = trace_reason
        if trace_reason:
            # A trace was EXPECTED for this case and did not arrive. That is
            # infra (SS7), not `unscored`: the difference between "this run
            # never had traces" and "this run has traces and lost this one" is
            # the difference between a known limitation and a thing to go fix.
            for name in TRAJECTORY_LAYERS:
                if live(name):
                    layers[name] = {"layer": name,
                                    "verdict": INFRA_INCOMPLETE,
                                    "reason": trace_reason}
        elif trace_collected:
            result = self.run_scorer(
                "normalize_trace.py",
                ["--trace-id", record["response"]["trace_id"],
                 os.path.join(scratch, "trace.json")], "trajectory")
            if result.get("verdict") == LAYER_ERROR:
                for name in TRAJECTORY_LAYERS:
                    if live(name):
                        layers[name] = dict(result, layer=name)
                trajectory_gap = result["error"]
                trace_collected = False
            else:
                atomic_write_json(trajectory_path, result)
                if result.get("status") != "ok":
                    # SS7: missing spans / orphaned parents are infra, not a
                    # fail -- the app's behaviour was not observed, so nothing
                    # about it was measured.
                    checks = result.get("checks") or {}
                    trajectory_gap = (
                        "normalize_trace.py reports status {!r} "
                        "(spans_for_trace={}, orphaned_parents={})".format(
                            result.get("status"),
                            checks.get("spans_for_trace"),
                            len(checks.get("orphaned_parents") or [])))
                    for name in TRAJECTORY_LAYERS:
                        if live(name):
                            layers[name] = {"layer": name,
                                            "verdict": INFRA_INCOMPLETE,
                                            "reason": trajectory_gap}
                    trace_collected = False
        else:
            # No trace was expected at all: the run is trace-less by
            # declaration (SS4.4), so the gap is that declaration.
            trajectory_gap = self.trace_state.get(
                "reason", "no trace collected for this run")

        # -- http: the runner compares; there is no scorer -------------------
        if live("http"):
            layers["http"] = self.score_http(expect, record)

        # -- routing: the per-case row and the verdict derived from it -------
        if live("routing"):
            layers["routing"] = self.score_routing_row(case, record,
                                                       trajectory_path
                                                       if trace_collected
                                                       else None,
                                                       trajectory_gap)

        # -- the trajectory-fed layers ---------------------------------------
        for name, script, argv in (
                ("trajectory", "trajectory_match.py",
                 [trajectory_path, expect_path]
                 + (["--fail-on-errored-calls"]
                    if scoring["fail_on_errored_calls"] else [])),
                ("tool_selection", "score_args.py",
                 [trajectory_path, expect_path]),
                ("loops", "detect_loops.py",
                 [trajectory_path, "--repeat-threshold",
                  scoring["repeat_threshold"], "--call-budget",
                  scoring["call_budget"]]),
                ("authz", "score_authz.py",
                 [trajectory_path, expect_path, "--answer", answer_path]
                 + (["--id-pattern", scoring["id_pattern"]]
                    if scoring["id_pattern"] else [])),
        ):
            if not live(name) or layers[name]["verdict"] != NA:
                continue
            if not trace_collected:
                layers[name] = unscored(name, trajectory_gap)
                continue
            layers[name] = self.run_scorer(script, argv, name)

        # -- answer -----------------------------------------------------------
        if live("answer"):
            if record["response"]["answer"] is None:
                # The response carried nothing at the declared <answer> path.
                # Scoring the empty string against must_contain would report a
                # content failure for what is an extraction gap.
                layers["answer"] = unscored(
                    "answer",
                    "the response carried no value at the adapter's declared "
                    "<answer> path; nothing to score")
            else:
                layers["answer"] = self.run_scorer(
                    "score_answer.py", [answer_path, expect_path], "answer")

        # -- execution --------------------------------------------------------
        if "execution" in applicable:
            # Extracted even when the matrix disabled the layer: reading the
            # app's result is not scoring it, and the file is what lets the
            # run be re-scored later without re-invoking the app (SS9/D9).
            atomic_write_json(actual_path, self.extract_actual(
                record, trajectory_path if trace_collected else None))
        if live("execution"):
            argv = [actual_path, expect_path]
            if scoring["float_tolerance"]:
                argv += ["--float-tolerance", scoring["float_tolerance"]]
            layers["execution"] = self.run_scorer("score_execution.py", argv,
                                                  "execution")

        # -- SS5.4: the three the runner REFUSES to attempt --------------------
        if live("rules"):
            layers["rules"] = unscored(
                "rules", "business rules are evaluated by the skill")
        if live("state"):
            # RESERVED, and the reason says so rather than naming a field the
            # user could go declare. It used to read "state-diff needs
            # environment.snapshot_state", which is a to-do the adapter cannot
            # discharge: nothing in this runner calls seed, reset or
            # snapshot_state, so declaring all three changes nothing here.
            # Same correction Step 7 made to the multi_turn blocked_by string.
            layers["state"] = unscored(
                "state", "state-diff is RESERVED: no scorer compares "
                         "environment snapshots, and the runner never invokes "
                         "environment.seed/.reset/.snapshot_state")
        if live("judged"):
            layers["judged"] = {"layer": "judged",
                                "verdict": f"unjudged ({self.unjudged_reason()})"}
        return layers

    @staticmethod
    def score_http(expect, record):
        """The one layer with no script: the runner compares (SS5).

        An exhausted 5xx never gets here -- score_layers() files it as
        infra_error on every layer first (SS7) -- so the 5xx branch below only
        guards a caller that bypasses that path.
        """
        status = record["response"]["status"]
        expected = (expect.get("http") or {}).get("status")
        if expected is not None:
            return {"layer": "http", "verdict": PASS if status == expected
                    else FAIL, "status": status, "expected": expected}
        if status is not None and 500 <= status < 600:
            return {"layer": "http", "verdict": INFRA_ERROR, "status": status,
                    "reason": "5xx with no expect.http.status: the app failed, "
                              "it did not answer wrongly"}
        return {"layer": "http",
                "verdict": PASS if status and 200 <= status < 300 else FAIL,
                "status": status}

    def observed_route(self, record, trajectory_path):
        """SS5.2. Where an observed route may come from, in declared order.

        Every source is something the ADAPTER declared. The runner never
        infers a route: the shipped run did (200=answered, 400=none), which is
        a defensible run-specific judgement and exactly the kind of judgement a
        runner must not make silently across every app. Decision D2 makes it
        declared and per-app instead.

        Returns (route, source). A null route is written as null and the
        scorer maps it to __no_route__.
        """
        invocation = self.adapter.get("invocation") or {}
        field = invocation.get("route_from_response")
        if field:
            value = dotted_get(record["response"]["body"], field)
            return (value if isinstance(value, str) else None), "response"
        if trajectory_path:
            # gen_ai's own meaning of "which agent handled this". The adapter
            # declared `convention: gen_ai`, which IS the declaration that
            # agent names live in gen_ai.agent.name -- so this reads a declared
            # convention rather than guessing at a shape.
            try:
                agents = (read_json(trajectory_path).get("trajectory")
                          or {}).get("agents") or []
            except (OSError, ValueError):
                agents = []
            names = [a.get("name") for a in agents if a.get("name")]
            if names:
                return names[0], "trace"
        status_map = invocation.get("route_from_status")
        if isinstance(status_map, dict):
            # JSON object keys are strings; a YAML->JSON adapter may carry
            # either, so look both up rather than making the author guess.
            status = record["response"]["status"]
            value = status_map.get(str(status), status_map.get(status))
            return (value if isinstance(value, str) else None), "status"
        return None, None

    def score_routing_row(self, case, record, trajectory_path, gap):
        """The per-case routing row plus the verdict derived from it (SS5.2).

        The row is accumulated for the run-level scorer; the verdict here
        applies score_routing.py's own documented pass rule (observed in
        acceptable, expected always implicitly acceptable, or clarify_ok and
        clarified) so the two cannot disagree about a single case.
        """
        expect = case.get("expect") or {}
        invocation = self.adapter.get("invocation") or {}
        clarify_ok = bool(expect.get("clarify_ok"))
        clarify_field = invocation.get("clarify_from_response")
        if clarify_ok and not clarify_field:
            # Writing `clarified: false` here would be a claim, not an
            # observation -- and on a case whose whole point is that clarifying
            # is acceptable, the claim decides the verdict.
            return unscored(
                "routing",
                "expect.clarify_ok is set but the adapter declares no "
                "invocation.clarify_from_response, so whether the app "
                "clarified was never observed")
        observed, source = self.observed_route(record, trajectory_path)
        if source is None:
            return unscored("routing", "{}; no invocation.route_from_response, "
                                       "trace or route_from_status to read an "
                                       "observed route from".format(
                                           gap or "no observed route"))
        clarified = bool(dotted_get(record["response"]["body"], clarify_field)) \
            if clarify_field else False
        row = {"case_id": case["id"], "expected": expect.get("route"),
               "observed": observed,
               "acceptable": expect.get("route_acceptable"),
               "clarify_ok": clarify_ok, "clarified": clarified}
        if row["expected"] is None:
            # score_routing.py requires a non-null `expected`; a case with only
            # route_acceptable has nothing for the confusion matrix to be about.
            return unscored("routing",
                            "expect.route is absent, so there is no expected "
                            "label; score_routing.py requires one")
        acceptable = [nfc(v) for v in (row["acceptable"] or [])
                      if isinstance(v, str)] + [nfc(row["expected"])]
        passed = (isinstance(observed, str) and nfc(observed) in acceptable) \
            or (clarify_ok and clarified)
        return {"layer": "routing", "verdict": PASS if passed else FAIL,
                "observed_from": source, "row": row}

    def extract_actual(self, record, trajectory_path):
        """SS5.3 / the adapter's result-extraction contract, in priority order.

        Every source is DECLARED. When none yields a value the runner writes
        {"missing": true, "reason": ...} and score_execution.py turns that into
        `unscored` -- an unread result must never be scored as a mismatch,
        because that manufactures a failure the app never had.
        """
        invocation = self.adapter.get("invocation") or {}
        body = record["response"]["body"]

        from_tool = invocation.get("result_from_tool")
        if isinstance(from_tool, dict) and trajectory_path:
            try:
                calls = (read_json(trajectory_path).get("trajectory")
                         or {}).get("tool_calls") or []
            except (OSError, ValueError):
                calls = []
            matches = [c for c in calls if c.get("name") == from_tool.get("tool")
                       and c.get("result") is not None]
            if matches:
                value = matches[-1]["result"]
                if isinstance(value, str):
                    # The gen_ai convention carries the tool result as a
                    # string attribute, so a structured result arrives as JSON
                    # text. Parsed here rather than in normalize_trace.py,
                    # which deliberately records the attribute as the exporter
                    # set it.
                    with contextlib.suppress(ValueError):
                        value = json.loads(value)
                if from_tool.get("field"):
                    value = dotted_get(value, from_tool["field"])
                if value is not None:
                    return shape_actual(value)

        field = invocation.get("result_from_response")
        if field:
            value = dotted_get(body, field)
            if value is not None:
                return shape_actual(value)

        pattern = invocation.get("result_pattern")
        if pattern:
            answer = record["response"]["answer"] or ""
            try:
                # The scorers' own watchdog: this regex is the author's and
                # the text is the model's, and here a catastrophic backtrack
                # would stall the runner itself, not a scorer subprocess.
                match = run_bounded(lambda: re.search(pattern, answer))
            except re.error as exc:
                return {"missing": True,
                        "reason": f"invocation.result_pattern is not a valid "
                                  f"regex: {exc}"}
            except RegexTimeout:
                return {"missing": True,
                        "reason": "invocation.result_pattern did not finish "
                                  f"within {REGEX_TIMEOUT_S}s on this answer "
                                  "(catastrophic backtracking?)"}
            if match:
                return shape_actual(match.group(1) if match.groups()
                                    else match.group(0))

        declared = [name for name in ("result_from_tool",
                                      "result_from_response", "result_pattern")
                    if invocation.get(name)]
        if not declared:
            return {"missing": True,
                    "reason": "the adapter declares no result extraction "
                              "(invocation.result_from_tool / "
                              "result_from_response / result_pattern), so the "
                              "app's result was never read"}
        return {"missing": True,
                "reason": "declared result extraction ({}) matched nothing in "
                          "this response".format(", ".join(declared))}

    def record_routing_row(self, verdict):
        """Accumulate SS5.2's run-level input as cases complete.

        Read back off the VERDICT rather than kept in a parallel list, so a
        --resume rebuilds it from the case dirs like everything else in SS9(a).
        """
        # A canary measures the harness and enters no denominator (SS9) --
        # the routing report's included (F-161). Its own layer still scores.
        # A conversation contributes no row (docs/multi-turn.md SS4): the
        # run-level report keeps meaning single-turn routing, and its turns'
        # rows stay in turns/<t>/verdict.json for a later per-turn report.
        # Its union is keyed t<n>.routing, so the lookup below would find
        # nothing anyway; the flag says so on purpose rather than by luck.
        if verdict.get("multi_turn"):
            return
        row = (verdict["layers"].get("routing") or {}).get("row")
        if row and not verdict["canary"]:
            self.routing_rows.append(row)

    def build_verdict(self, case, attempt, skip):
        record = attempt["record"]
        layers = attempt["layers"]
        trace_collected = attempt["trace_collected"]
        canary = is_canary(case)
        expected_trace = self.trace_state["collected"]
        return {
            "case_id": case["id"],
            "set": self.plan["selecting_split"],
            "category": case.get("category"),
            "canary": canary,
            "holdout": is_holdout(case),
            # gating is true unless the case is a canary: a canary measures the
            # harness, so folding it into the app's gate moves the number for
            # the wrong reason.
            "gating": (not canary) and case.get("gating", True) is not False,
            "verdict": attempt["verdict"],
            "k": self.k,
            # Carried on the verdict, not only in response.json, so a resumed
            # run can rebuild results.json's latency column from the same
            # source as everything else in it (SS8 step 3).
            "latency_s": attempt["latency_s"] if "latency_s" in attempt
            else record["response"]["latency_s"],
            "layers": layers,
            # docs/multi-turn.md SS4: one conversation is one case. These say
            # how far it got; nothing downstream of `verdict` changes meaning.
            "multi_turn": conversation_turns(case) is not None,
            "turns_sent": attempt.get("turns_sent",
                                      0 if skip else 1),
            "failed_turn": attempt.get("failed_turn"),
            "stop_reason": attempt.get("stop_reason"),
            "trace": {"expected": expected_trace,
                      "collected": trace_collected,
                      # SS6: trace.json absent is never ambiguous, because this
                      # block says whether a trace was expected and why there
                      # isn't one.
                      "reason": attempt["trace_reason"] or (
                          None if trace_collected
                          else self.trace_state.get("reason"))},
            "repeats": None,
            "notes": "",
        }

    # -- SS9(a). Derived, not accumulated ---------------------------------
    def refresh_derived(self):
        """Rewrite both jsonl files IN FULL from the completed cases.

        This is the mechanism the whole contract exists for. They are not
        appended to and not built at run end, so: an interrupted run still has
        both files, correct as of its last completed case; a resumed run cannot
        double-count; and the two cannot disagree with the case dirs, because
        the case dirs are their only source. The cost is O(n^2) small writes
        over a run, which is worth less than the correctness.
        """
        atomic_write_jsonl(os.path.join(self.out, "verdicts.jsonl"), [
            {"case_id": v["case_id"], "set": v["set"],
             "category": v["category"], "canary": v["canary"],
             "gating": v["gating"], "verdict": v["verdict"],
             "layers": flatten_layers(v["layers"])}
            for v in self.verdicts])
        # Exactly what stats.py pairs. It treats a third verdict value as a
        # hard error rather than filtering silently, so the filtering stays a
        # VISIBLE step here and the unfiltered record survives beside it.
        atomic_write_jsonl(
            os.path.join(self.out, "verdicts_for_stats.jsonl"),
            [{"case_id": v["case_id"], "verdict": v["verdict"]}
             for v in self.verdicts if v["verdict"] in (PASS, FAIL)])
        if self.k > 1:
            atomic_write_jsonl(os.path.join(self.out, "repeats.jsonl"),
                               self.repeat_rows()[0])

    def repeat_rows(self):
        """SS5.5's input to reduce_repeats.py, and what had to be left out.

        reduce_repeats.py refuses any verdict that is not pass/fail (counting
        an infra verdict as a failure would bias the reliability estimate) and
        refuses a case with fewer than k rows. So a case contributes ONLY when
        all k of its repeats are pass/fail: a partial case would either poison
        the reducer or silently lower k for every other case. The excluded
        cases are named in reliability.json rather than dropped quietly.
        """
        rows, excluded = [], []
        for verdict in self.verdicts:
            repeats = verdict["repeats"] or []
            values = [r["verdict"] for r in repeats]
            if len(values) == self.k and all(v in (PASS, FAIL) for v in values):
                rows.extend({"case_id": verdict["case_id"], "verdict": v}
                            for v in values)
            elif repeats:
                excluded.append({"case_id": verdict["case_id"],
                                 "verdicts": values})
        return rows, excluded

    def summary(self, status, missing):
        verdicts = self.verdicts
        graded = [v for v in verdicts if not v["canary"]]
        counts = {value: sum(1 for v in graded if v["verdict"] == value)
                  for value in (PASS, FAIL, UNSCORED, SKIPPED, INFRA_ERROR,
                                INFRA_INCOMPLETE)}
        infra = counts[INFRA_ERROR] + counts[INFRA_INCOMPLETE]
        canaries = [v for v in verdicts if v["canary"]]
        holdouts = [v for v in verdicts if v["holdout"]]
        # A row's name used as a matrix key (`answer`, `http`) switches
        # nothing -- capability_blocked_by never reads it, and the row still
        # scores -- so it is not listed as unscored either. Same test as
        # validate_cases.py's not_a_matrix_key warning.
        disabled = sorted(
            {name for name, block in self.plan["capability_matrix"].items()
             if isinstance(block, dict) and block.get("enabled") is False
             and MATRIX_KEY_OF_LAYER.get(name, name) == name}
            | set(self.trace_state["disabled_layers"]))
        return {
            "status": status,
            "n": len(graded), "attempted": self.attempted,
            "passes": counts[PASS], "failures": counts[FAIL],
            "gating_failures": sum(1 for v in graded
                                   if v["gating"] and v["verdict"] == FAIL),
            "unscored": counts[UNSCORED], "skipped": counts[SKIPPED],
            "infra_errors": infra,
            # Of those, the cases that got one 5xx on every attempt of every
            # repeat: still infra, but possibly the app's own error (F-165).
            "repeated_5xx": sum(1 for v in graded if v.get("repeated_5xx")),
            # SS10's denominators: infra_rate is over the non-canary cases
            # that were SENT (n - skipped), the same cases its count comes
            # from. `attempted` counts canaries too, and dividing the
            # canary-free infra count by it read one infra case beside one
            # passing canary as 50% (F-161). crash_rate counts every sent
            # case, so `attempted` is its.
            "infra_rate": infra_rate(infra, len(graded), counts[SKIPPED]),
            "crash_rate": round(self.crashes / self.attempted, 4)
            if self.attempted else 0.0,
            "scorer_errors": self.scorer_errors,
            "unscorable_layers": disabled,
            "unjudged": self.unjudged_reason(),
            "canaries": {"n": len(canaries),
                         "passed": sum(1 for v in canaries
                                       if v["verdict"] == PASS)},
            # THE HOLDOUT SEAL, concretely: aggregate counts only. A holdout
            # case_id appears nowhere in this file (see write_results).
            "holdout": None if not holdouts else {
                "n": len(holdouts),
                "passes": sum(1 for v in holdouts if v["verdict"] == PASS),
                "failures": sum(1 for v in holdouts if v["verdict"] == FAIL),
                "gating_failures": sum(1 for v in holdouts
                                       if v["gating"]
                                       and v["verdict"] == FAIL),
                "looks_recorded": self.holdout_looks},
            "missing_artifacts": missing or [],
        }

    def unjudged_reason(self):
        judge = (self.plan.get("manifest_extra") or {}).get("judge") or {}
        if self.plan["mode"] in ("smoke", "targeted"):
            return "mode: {}".format(self.plan["mode"])
        if judge.get("status") != "calibrated":
            return "judge not calibrated"
        if self._judge_gate is None:
            self._judge_gate = self.check_judge_calibration()
        return self._judge_gate

    def check_judge_calibration(self):
        """Is `judge.status: calibrated` backed by measurements? (SS5.4)

        `manifest_extra.judge.status` is copied from profile.yaml, which a
        model edits -- and profile-schema.md has always said the flag is
        DERIVED, "not set by hand", from each rubric's own tpr/tnr/kappa. Until
        score_agreement.py existed nothing derived it, so the harness's central
        credibility gate opened on a word an LLM typed. This is the half of the
        fix that lives in the runner: the flag is now necessary and NOT
        sufficient, and the sufficient part is a sidecar of numbers.

        Every failure degrades to a REASON STRING, never an exit code. The
        judged layer is `unjudged` either way -- it has no script and is never
        pass/fail (SS5.4) -- so a stricter gate here can only make a run more
        conservative, and an unreadable sidecar must not sink a run whose other
        eleven layers scored fine.

        The sidecar cannot know which rubrics are ACTIVE; the runner can see
        which ones the selected cases reference, so it closes that half here.
        """
        declared = (self.plan["paths"] or {}).get("judge_calibration")
        if not declared:
            return ("judge calibration not recorded -- paths."
                    "judge_calibration is unset, and a hand-set "
                    "judge.status does not open this gate on its own "
                    "(score_agreement.py --write)")
        path = declared if os.path.isabs(declared) \
            else os.path.join(self.state_dir, declared)
        try:
            with open(path, encoding="utf-8") as fh:
                sidecar = json.load(fh)
            if not isinstance(sidecar, dict):
                raise ValueError("expected a JSON object")
        except (OSError, ValueError) as exc:
            return f"judge calibration unreadable ({path}): {exc}"
        if sidecar.get("schema") != JUDGE_CALIBRATION_SCHEMA:
            return "judge calibration schema is {!r}, expected {!r}".format(
                sidecar.get("schema"), JUDGE_CALIBRATION_SCHEMA)
        if sidecar.get("status") != "calibrated":
            return f"judge not calibrated per {path}"
        measured = sidecar.get("rubrics_measured")
        if not isinstance(measured, list):
            return f"judge calibration lists no rubrics_measured ({path})"
        # Compare on the bare rubric_id, with the pin still accepted: a case
        # references `<rubric_id>-v<version>` (case-format.md's convention,
        # rubric-format.md SS'File shape'), while an annotation carries whichever
        # of the two the labelling flow wrote. Matching both ways means a
        # correctly calibrated rubric is never refused over a version suffix.
        have = set()
        for name in measured:
            if isinstance(name, str):
                have.add(nfc(name))
                have.add(RUBRIC_PIN.sub("", nfc(name)))
        missing = sorted({
            ref for ref in self.referenced_rubrics()
            if ref not in have and RUBRIC_PIN.sub("", ref) not in have})
        if missing:
            return ("judge calibration measured no rubric {} used by this "
                    "run".format(", ".join(repr(m) for m in missing)))
        return "deferred to skill"

    def referenced_rubrics(self):
        """The rubric each selected case pins, per applicable_layers's trigger."""
        refs = set()
        for case in self.plan["cases"]:
            rubric = ((case.get("expect") or {}).get("answer") or {}).get("rubric")
            if isinstance(rubric, str) and rubric.strip():
                refs.add(nfc(rubric.strip()))
        return refs

    def write_results(self, status=None, missing=None, exit_code=None):
        """results.json: written at pre-flight, rewritten per case, finalized.

        A run directory therefore never exists without a results.json, so SS9's
        check has something to fail on from the first second -- rather than the
        shipped run's shape, where absence looked like success.
        """
        rows = [{"case_id": v["case_id"], "verdict": v["verdict"],
                 "gating": v["gating"],
                 "layers": flatten_layers(v["layers"]),
                 "latency_s": v.get("latency_s")}
                for v in self.verdicts if not v["holdout"]]
        atomic_write_json(os.path.join(self.out, "results.json"), {
            "run_id": self.plan["run_id"],
            "harness_version": HARNESS_VERSION,
            "dataset_version": (self.plan.get("manifest_extra")
                                or {}).get("dataset_version"),
            "mode": self.plan["mode"],
            "cases": rows,
            "summary": self.summary(status or self.status, missing),
            "exit_code": exit_code,
        })

    # -- SS8. Resume -------------------------------------------------------
    def adopt_existing(self):
        """Continue an interrupted run into an existing --out.

        A case with a parseable verdict.json is DONE and is not re-invoked; a
        case directory without one is deleted and re-run, because a half-written
        case is cheaper to redo than to reason about.
        """
        manifest_path = os.path.join(self.out, "manifest.yaml")
        if not os.path.isfile(manifest_path):
            bad_input(f"--resume: {self.out} has no manifest.yaml; there is no run "
                      "here to resume")
        try:
            manifest = read_json(manifest_path)
        except (OSError, ValueError) as exc:
            bad_input(f"--resume: cannot read {manifest_path}: {exc}")
        if manifest.get("plan_sha256") != self.plan_sha256:
            # Resuming a DIFFERENT suite under an old manifest produces a run
            # that is comparable to nothing -- and it would still be labelled
            # with the old run id.
            bad_input("plan changed since {} started; start a new run "
                      "id".format(self.plan["run_id"]))

        done = {}
        listing = sorted(os.listdir(self.cases_dir)) \
            if os.path.isdir(self.cases_dir) else []
        for case_id in listing:
            case_dir = os.path.join(self.cases_dir, case_id)
            if not os.path.isdir(case_dir):
                continue
            try:
                done[case_id] = read_json(
                    os.path.join(case_dir, "verdict.json"))
            except (OSError, ValueError):
                shutil.rmtree(case_dir)
        # Rebuilt in PLAN order, not directory order, so a resumed run's
        # verdicts.jsonl has the same row order a straight-through run has.
        for case in self.ordered_cases():
            verdict = done.get(case["id"])
            if verdict is None:
                continue
            self.verdicts.append(verdict)
            self.record_routing_row(verdict)
            if verdict["verdict"] != SKIPPED:
                self.attempted += 1
            if self.crashed_on_disk(case):
                self.crashes += 1
        self.refresh_derived()
        return {v["case_id"] for v in self.verdicts}

    def adopted_abort(self):
        """SS8: a run that aborted on a canary is still aborted when resumed.

        abort_check() runs after a case EXECUTES and an adopted case never
        does, so `--resume` walked past the failed canary, ran every case
        after it and finalized `ok`, exit 0 -- and gate.py opened on a run
        whose own abort had said nothing from it was trustworthy. Drift is
        not fixed by continuing: the canary's verdict is on disk and would be
        adopted again, so the answer is a new run id once it is.
        """
        for verdict in self.verdicts:
            if verdict["canary"] and verdict["verdict"] == FAIL:
                self.log("abort", reason="canary", case_id=verdict["case_id"],
                         adopted=True)
                return ("aborted_canary", EXIT_CANARY,
                        "canary {} scored {} in the run being resumed; "
                        "harness/judge drift, nothing else from this run is "
                        "trustworthy -- fix the drift and start a new run "
                        "id".format(verdict["case_id"], verdict["verdict"]))
        return None

    def crashed_on_disk(self, case):
        """execute_case's rule, read back: a crash on ANY repeat counts.

        The case-level response.json is only the representative repeat's, so
        with k > 1 reading it alone let [fail, crash, pass] resume with no
        crash -- one set of artifacts, two crash rates."""
        if case.get("category") not in CRASH_RATE_CATEGORIES:
            return False
        case_dir = os.path.join(self.cases_dir, case["id"])
        paths = [os.path.join(case_dir, "response.json")]
        if self.k > 1:
            paths += [os.path.join(case_dir, "repeats", str(n), "response.json")
                      for n in range(1, self.k + 1)]
        for path in paths:
            try:
                if read_json(path).get("crashed"):
                    return True
            except (OSError, ValueError, AttributeError):
                continue
        return False

    # -- the run -----------------------------------------------------------
    def execute(self, already_done):
        for case in self.ordered_cases():
            if case["id"] in already_done:
                continue
            verdict = self.execute_case(case)
            self.refresh_derived()
            self.write_results()
            abort = self.abort_check(case, verdict)
            if abort:
                return abort
        return None

    def abort_check(self, case, verdict):
        """SS7's two mid-run aborts. Both leave a well-formed partial run.

        Everything completed so far stays on disk and finalize still runs, so
        the artifact is readable -- but nothing from it is trustworthy and
        results.json says so in summary.status.
        """
        if verdict["canary"] and verdict["verdict"] == FAIL:
            self.log("abort", reason="canary", case_id=case["id"])
            return ("aborted_canary", EXIT_CANARY,
                    "canary {} scored {}; harness/judge drift, nothing else "
                    "from this run is trustworthy".format(
                        case["id"], verdict["verdict"]))
        if self.attempted >= INFRA_ABORT_MIN_CASES:
            infra = sum(1 for v in self.verdicts
                        if v["verdict"] in (INFRA_ERROR, INFRA_INCOMPLETE))
            rate = infra / self.attempted
            if rate > self.execution["infra_rate_abort"]:
                self.log("abort", reason="infra", rate=round(rate, 4))
                return ("aborted_infra", EXIT_INFRA,
                        # Over every attempted case, canaries included --
                        # not summary.infra_rate's denominator (SS10's
                        # counts), so it says which share it is.
                        "infra verdicts are {:.0%} of all cases attempted "
                        "(canaries included), above infra_rate_abort {:.0%}, "
                        "after {} attempted cases".format(
                            rate, self.execution["infra_rate_abort"],
                            self.attempted))
        return None

    # -- SS5.2 / SS5.5 / SS5.6. The run-level scorers ----------------------
    def score_run_level(self):
        """Everything that scores the RUN rather than a case.

        Runs before the completeness check, and runs even after an abort: the
        partial run is still a well-formed artifact, and half a routing report
        is more useful than none for working out why the run stopped.
        """
        self.score_routing_run()
        self.score_reliability()
        self.score_comparison()

    def score_routing_run(self):
        """SS5.2. score_routing.py scores a JSONL of ALL cases at once.

        It reports macro/micro F1, a confusion matrix, OOS metrics and
        spurious_labels -- none of which is defined for a single case -- so the
        rows accumulate as cases complete and the scorer runs once, here.
        (micro_f1 is the matrix's accuracy and is kept deliberately: the
        macro-vs-micro gap is the minority-route skew warning.)
        """
        if not self.routing_rows:
            return
        results = os.path.join(self.out, "routing_results.jsonl")
        atomic_write_jsonl(results, self.routing_rows)
        argv = [results]
        oos = self.plan["scoring"]["oos_route"]
        if oos:
            # --oos-route only RENAMES a label, so a name matching nothing
            # renames nothing and the scorer exits 2 listing the labels present
            # (Step 3 made it do that). The runner performs the check itself
            # rather than discovering it from an exit code, because a run that
            # simply has no OOS cases selected is not an error.
            present = set()
            for row in self.routing_rows:
                for key in ("expected", "observed"):
                    if isinstance(row.get(key), str):
                        present.add(nfc(row[key]))
                present.update(nfc(v) for v in (row.get("acceptable") or [])
                               if isinstance(v, str))
            if nfc(oos) in present:
                argv += ["--oos-route", oos]
            else:
                self.log("scorer", layer="routing", scorer="score_routing.py",
                         skipped_flag="--oos-route",
                         reason=f"no selected case carries route {oos!r}")
        report = self.run_scorer("score_routing.py", argv, "routing")
        atomic_write_json(os.path.join(self.out, "routing_report.json"), report)

    def score_reliability(self):
        """SS5.5. pass@k vs pass^k over the repeats, once, at the end."""
        if self.k <= 1:
            return
        rows, excluded = self.repeat_rows()
        path = os.path.join(self.out, "reliability.json")
        if not rows:
            # reduce_repeats.py exits 2 on empty input, and an all-infra run is
            # not a scorer error -- it is a run with nothing to reduce.
            atomic_write_json(path, unscored(
                "reliability",
                f"no case has {self.k} pass/fail repeats to reduce"))
            return
        report = self.run_scorer(
            "reduce_repeats.py",
            [os.path.join(self.out, "repeats.jsonl"), "--k", self.k],
            "reliability")
        if excluded:
            # Named, not dropped quietly: a flakiness number computed over the
            # cases that happened to answer every time is the one number a
            # reliability run must not report without saying so.
            report = dict(report, excluded_cases=excluded, excluded_note=(
                "excluded from pass@k/pass^k: a case contributes only when all "
                "k repeats scored pass or fail (reduce_repeats.py refuses "
                "infra/unscored verdicts, and counting them as failures would "
                "bias the estimate)"))
        atomic_write_json(path, report)

    def score_comparison(self):
        """SS5.6 / decision D3. The baseline diff, with its rules.

        Folded into the runner rather than left to the skill because the diff
        HAS rules -- version equality, the k-match, attrition, "within noise" --
        and rules that decide whether a change ships are the category of thing
        this whole step exists to take out of LLM hands. The version and k
        checks already ran at plan validation, before any spend.
        """
        if not self.baseline_verdicts:
            return
        report = self.run_scorer(
            "stats.py",
            [self.baseline_verdicts,
             os.path.join(self.out, "verdicts_for_stats.jsonl")],
            "comparison")
        atomic_write_json(os.path.join(self.out, "comparison.json"), dict(
            report, baseline_verdicts=self.baseline_verdicts,
            candidate_run_id=self.plan["run_id"]))

    def finalize(self, aborted):
        status, code = ("ok", EXIT_OK) if not aborted else aborted[:2]
        self.score_run_level()
        missing = verify_run_dir(self.out)
        if missing:
            status, code = "incomplete", EXIT_INCOMPLETE
        elif not aborted and self.scorer_errors:
            # SS11 row 7: the run finished and its artifacts are complete, so
            # the status stays "ok" -- but its numbers are not quotable, and an
            # exit code is the only part of that a CI script reads.
            code = EXIT_SCORER
        self.status = status
        self.write_results(status=status, missing=missing, exit_code=code)
        self.log("finalize", status=status, exit_code=code,
                 missing=len(missing), scorer_errors=self.scorer_errors)
        if missing:
            raise RunnerExit(
                EXIT_INCOMPLETE,
                "run {} is incomplete: {} required artifact(s) missing or "
                "unreadable".format(self.plan["run_id"], len(missing)),
                missing_artifacts=missing)
        if aborted:
            # An abort outranks exit 7: "the canary drifted" is the fact the
            # caller has to act on, and the scorer errors are in results.json.
            raise RunnerExit(code, aborted[2])
        if code == EXIT_SCORER:
            raise RunnerExit(
                EXIT_SCORER,
                f"{self.scorer_errors} scorer invocation(s) failed; the "
                "run's artifacts are complete but its numbers are not "
                "quotable")
        # stderr, like the holdout-look count: stdout stays reserved for the
        # machine-readable {"error": ...} payload, so a clean run still prints
        # nothing THERE. But exit 0 and total silence read as "did that work?"
        # at a terminal (2026-09-21 audit), and exit 0 is not a verdict -- a
        # red suite exits 0 too -- so the line names the step that gives one.
        sys.stderr.write(
            "run {} complete: {} case(s) in {} -- exit 0 means the RUN "
            "finished, not that it passed; gate.py {} gives the "
            "verdict\n".format(self.plan["run_id"], len(self.plan["cases"]),
                               self.out, self.out))
        return EXIT_OK


# --------------------------------------------------------------------------
# SS9(c). The completeness check, as a function of a RUN DIRECTORY.
#
# Deliberately not a method: finalize and --verify must be the same check, or
# "the run finished" and "this old run's outputs exist" become two different
# statements again -- which is the failure this whole contract is written
# against. Everything below is read off the tree, so it works months later
# with no plan, no adapter and no app.
# --------------------------------------------------------------------------

def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def completed_cases(out_dir):
    """SS6: a case directory containing a parseable verdict.json is complete,
    by definition. This function is that definition, in one place."""
    cases_dir = os.path.join(out_dir, "cases")
    listing = sorted(os.listdir(cases_dir)) if os.path.isdir(cases_dir) else []
    found = {}
    for case_id in listing:
        try:
            found[case_id] = read_json(
                os.path.join(cases_dir, case_id, "verdict.json"))
        except (OSError, ValueError):
            continue
    return found


def case_requires(name, out_dir, case_id, verdict):
    """REQUIRED_IF's three per-case conditions, evaluated off the case dir."""
    if name in ("trajectory.json", "trace.json"):
        # Keyed on the trace this case actually got, not on the run-level "is
        # the store queryable": a queryable store that never produced this
        # case's trace is infra_incomplete (SS7), and demanding the file anyway
        # would turn one honest infra row into a spurious completeness failure.
        return bool((verdict.get("trace") or {}).get("collected"))
    if name == "actual.json":
        if verdict.get("verdict") == SKIPPED:
            return False        # nothing was sent, so there is no result
        try:
            expect = read_json(os.path.join(out_dir, "cases", case_id,
                                            "expect.json"))
        except (OSError, ValueError):
            return False        # expect.json itself is already reported missing
        return isinstance(expect, dict) and expect.get("result") is not None
    return False


def turn_numbers(turns_dir):
    """The entries of a turns/ directory, or [] when it is absent."""
    return sorted(os.listdir(turns_dir)) if os.path.isdir(turns_dir) else []


def check_turn_tree(case_dir, verdict, shown):
    """docs/multi-turn.md SS7's turns/ rule, read off the case directory.

    turns/ is present iff the case is a conversation that sent a turn,
    numbered contiguously from 1, and holds exactly verdict.json's
    turns_sent turns, each with TURN_FILES -- plus its trace files when its
    own verdict.json says a trace was collected for it. A repeat's turns
    follow the same count rule against that repeat's turns_sent, holding
    the five. A run older than the feature has no multi_turn key and no
    turns/, so it passes unchanged."""
    problems = []
    multi = bool(verdict.get("multi_turn"))
    sent = verdict.get("turns_sent", 0) if multi else 0
    if not isinstance(sent, int) or isinstance(sent, bool) or sent < 0:
        return [f"{shown}/verdict.json (turns_sent is {sent!r}, not a "
                "count)"]
    trees = [(os.path.join(case_dir, "turns"), sent, f"{shown}/turns", True)]
    for repeat in (verdict.get("repeats") or []) if multi else []:
        if isinstance(repeat, dict):
            trees.append((os.path.join(case_dir, "repeats",
                                       str(repeat.get("n")), "turns"),
                          repeat.get("turns_sent"),
                          f"{shown}/repeats/{repeat.get('n')}/turns", False))
    for turns_dir, count, label, traces in trees:
        if not isinstance(count, int) or isinstance(count, bool):
            problems.append(f"{label} (turns_sent is {count!r}, not a count)")
            continue
        want = [str(n) for n in range(1, count + 1)]
        found = turn_numbers(turns_dir)
        if found != sorted(want):
            problems.append(
                "{} (holds {}, expected turns 1..{}: verdict.json's "
                "turns_sent{})".format(
                    label, found or "nothing", count,
                    "" if multi else "; a single-turn case has no turns/"))
        for number in want:
            turn_dir = os.path.join(turns_dir, number)
            for name in TURN_FILES:
                if not os.path.isfile(os.path.join(turn_dir, name)):
                    problems.append("{}/{}/{} ({})".format(
                        label, number, name,
                        REQUIRED_IF[f"turns/*/{name}"]))
            if not traces:
                continue
            try:
                collected = bool((read_json(os.path.join(
                    turn_dir, "verdict.json")).get("trace") or {})
                    .get("collected"))
            except (OSError, ValueError, AttributeError):
                continue        # the verdict itself is reported missing
            for name in TURN_TRACE_FILES if collected else ():
                if not os.path.isfile(os.path.join(turn_dir, name)):
                    problems.append("{}/{}/{} ({})".format(
                        label, number, name,
                        REQUIRED_IF[f"turns/*/{name}"]))
    return problems


SEALED_CASE = "<a sealed holdout case>"


def sealed_ids(cases, rows=None):
    """The case ids THE HOLDOUT SEAL keeps out of every message below.

    Those messages are not private: finalize writes them into results.json's
    summary.missing_artifacts -- the file the seal is about -- and gate.py
    prints --verify's verbatim. A verdicts.jsonl row stands in for a case
    whose directory is gone."""
    sealed = {cid for cid, v in cases.items() if v.get("holdout")}
    sealed |= {row.get("case_id") for row in rows or []
               if isinstance(row, dict) and row.get("holdout")}
    return sealed


def named(case_id, sealed):
    return SEALED_CASE if case_id in sealed else case_id


def verify_run_dir(out_dir):
    """Return the list of missing or inconsistent artifacts. Empty == complete.

    Writes nothing, calls nothing, scores nothing.
    """
    missing = []
    docs = {}
    for name in REQUIRED_ALWAYS:
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path):
            missing.append(name)
            continue
        if name.endswith(".jsonl"):
            # An empty jsonl is legitimate (a run with zero pass/fail cases);
            # an unparseable one is not.
            try:
                docs[name] = read_jsonl(path)
            except (OSError, ValueError) as exc:
                missing.append(f"{name} (unparseable: {exc})")
            continue
        if os.path.getsize(path) == 0:
            missing.append(f"{name} (empty)")
            continue
        try:
            docs[name] = read_json(path)
        except (OSError, ValueError) as exc:
            missing.append(f"{name} (unparseable: {exc})")

    cases = completed_cases(out_dir)
    manifest = docs.get("manifest.yaml") or {}
    sealed = sealed_ids(cases, docs.get("verdicts.jsonl"))

    # (2) every completed case dir, with its conditionals.
    for case_id, verdict in cases.items():
        shown = f"cases/{named(case_id, sealed)}"
        for name in REQUIRED_PER_CASE:
            if not os.path.isfile(os.path.join(out_dir, "cases", case_id,
                                               name)):
                missing.append(f"{shown}/{name}")
        for name in REQUIRED_IF_PER_CASE:
            if not case_requires(name, out_dir, case_id, verdict):
                continue
            if not os.path.isfile(os.path.join(out_dir, "cases", case_id,
                                               name)):
                missing.append(f"{shown}/{name} ({REQUIRED_IF[name]})")
        missing.extend(check_turn_tree(
            os.path.join(out_dir, "cases", case_id), verdict, shown))

    # ...and the run-level ones. Each condition is read off the tree too, so
    # --verify evaluates exactly what finalize evaluated.
    # Canaries and conversations write no routing row (record_routing_row),
    # so neither can make the run-level file required: a suite of
    # route-less single-turn cases plus conversations asserting a route
    # would otherwise demand a file nobody wrote (docs/multi-turn.md SS4).
    routing_scorable = any(
        not v.get("canary") and not v.get("multi_turn")
        and ((v.get("layers") or {}).get("routing") or {}).get("verdict")
        in (PASS, FAIL) for v in cases.values())
    conditions = {
        "routing_results.jsonl": routing_scorable,
        "routing_report.json": os.path.isfile(
            os.path.join(out_dir, "routing_results.jsonl")),
        "repeats.jsonl": (manifest.get("k") or 1) > 1,
        "reliability.json": (manifest.get("k") or 1) > 1,
        "comparison.json": bool(manifest.get("baseline_verdicts")),
    }
    for name, required in conditions.items():
        if required and not os.path.isfile(os.path.join(out_dir, name)):
            missing.append(f"{name} ({REQUIRED_IF[name]})")

    # (3) the derived files against the case dirs, which are their only source.
    rows = docs.get("verdicts.jsonl")
    if rows is not None:
        by_id = {}
        for row in rows:
            case_id = row.get("case_id")
            if case_id in by_id:
                missing.append(
                    f"verdicts.jsonl (duplicate row for "
                    f"{named(case_id, sealed)})")
            by_id[case_id] = row.get("verdict")
        for case_id in sorted(set(cases) - set(by_id)):
            missing.append(
                f"verdicts.jsonl (no row for completed case "
                f"{named(case_id, sealed)})")
        for case_id in sorted(set(by_id) - set(cases)):
            missing.append(
                f"verdicts.jsonl (row for {named(case_id, sealed)}, which has "
                "no completed case directory)")
        for case_id in sorted(set(by_id) & set(cases)):
            if by_id[case_id] != cases[case_id].get("verdict"):
                missing.append(
                    "verdicts.jsonl ({}: {!r}, but its verdict.json says "
                    "{!r})".format(named(case_id, sealed), by_id[case_id],
                                   cases[case_id].get("verdict")))
        stats_rows = docs.get("verdicts_for_stats.jsonl")
        if stats_rows is not None:
            want = [{"case_id": r.get("case_id"), "verdict": r.get("verdict")}
                    for r in rows if r.get("verdict") in (PASS, FAIL)]
            if stats_rows != want:
                # stats.py treats a third verdict value as a hard error rather
                # than filtering silently, so this filtering stays a visible
                # step -- and a visible step is one something can check.
                missing.append(
                    "verdicts_for_stats.jsonl (not the pass/fail subset of "
                    f"verdicts.jsonl: {len(stats_rows)} rows, expected {len(want)})")

    # (4) results.json against the same case dirs.
    results = docs.get("results.json")
    if results is not None and rows is not None:
        missing.extend(check_results_json(results, cases, sealed))
    return missing


def check_results_json(results, cases, sealed=None):
    """SS9(c) check 4. Only what is derivable from the tree.

    The run-time bookkeeping counters (attempted, crash_rate, scorer_errors)
    are properties of the EXECUTION, not of the tree, so they are recorded and
    not recounted -- a check that had to guess at them would fail honest runs.
    """
    problems = []
    shown = {row.get("case_id"): row for row in (results.get("cases") or [])}
    # THE HOLDOUT SEAL: a holdout case contributes no row here, and its
    # case_id must appear nowhere in this file.
    # These messages are themselves written into results.json and printed by
    # gate.py, so a sealed id is never spelled in one (sealed_ids).
    expected_ids = {cid for cid, v in cases.items() if not v.get("holdout")}
    holdout_ids = {cid for cid, v in cases.items() if v.get("holdout")}
    sealed = holdout_ids | (sealed or set())
    for case_id in sorted(expected_ids - set(shown)):
        problems.append(
            f"results.json (no row for completed case {case_id})")
    for case_id in sorted(set(shown) - expected_ids - sealed):
        problems.append(
            f"results.json (row for {case_id}, which has no completed "
            "case directory)")
    leaked = sealed & set(shown)
    if leaked:
        problems.append(
            f"results.json ({len(leaked)} holdout case(s) named in the "
            "shareable summary; the seal is broken)")
    for case_id in sorted(expected_ids & set(shown)):
        if shown[case_id].get("verdict") != cases[case_id].get("verdict"):
            problems.append(
                "results.json ({}: {!r}, but its verdict.json says {!r})"
                .format(case_id, shown[case_id].get("verdict"),
                        cases[case_id].get("verdict")))

    summary = results.get("summary") or {}
    graded = [v for v in cases.values() if not v.get("canary")]
    canaries = [v for v in cases.values() if v.get("canary")]

    def count(value):
        return sum(1 for v in graded if v.get("verdict") == value)

    recounted = {
        "n": len(graded),
        "passes": count(PASS), "failures": count(FAIL),
        "unscored": count(UNSCORED), "skipped": count(SKIPPED),
        "infra_errors": count(INFRA_ERROR) + count(INFRA_INCOMPLETE),
        "gating_failures": sum(1 for v in graded if v.get("gating")
                               and v.get("verdict") == FAIL),
    }
    if "repeated_5xx" in summary:
        # Absent from runs older than the 2026-09-26 user-test fixes -- and
        # so is SS10's infra_rate, whose denominator those fixes changed
        # (every term is on disk now: infra_errors over n - skipped). The key
        # marks a run written under the current definition, so an older run
        # is not failed for the definition it was written under.
        recounted["repeated_5xx"] = sum(1 for v in graded
                                        if v.get("repeated_5xx"))
        recounted["infra_rate"] = infra_rate(
            recounted["infra_errors"], recounted["n"], recounted["skipped"])
    for key, value in recounted.items():
        if summary.get(key) != value:
            problems.append(
                f"results.json (summary.{key} is {summary.get(key)!r}; the "
                f"case directories count {value})")
    if summary.get("status") == "ok":
        # SS7: a canary `fail` aborts the run, so `ok` beside one is a run
        # that walked past its own abort (a --resume did, until 2026-09-30)
        # or a status edited by hand. Either way its numbers are the ones SS7
        # calls untrustworthy, and gate.py reads this check.
        for case_id in sorted(cid for cid, v in cases.items()
                              if v.get("canary") and v.get("verdict") == FAIL):
            # THE HOLDOUT SEAL: gate.py prints this line, so a sealed id
            # stays out of it.
            name = "a holdout canary" if cases[case_id].get("holdout") \
                else f"canary {case_id}"
            problems.append(
                f"results.json (summary.status is 'ok' but {name} scored "
                "fail; a canary failure aborts the run, SS7)")
    seen = summary.get("canaries") or {}
    want = {"n": len(canaries),
            "passed": sum(1 for v in canaries if v.get("verdict") == PASS)}
    if (seen.get("n"), seen.get("passed")) != (want["n"], want["passed"]):
        problems.append(
            f"results.json (summary.canaries is {seen!r}; the case directories "
            f"count {want})")
    return problems


def is_canary(case):
    """Canary membership is a SPLIT, like every other selection in this
    harness since Step 1 -- not a directory and not a naming convention."""
    return "canary" in (case.get("split") or [])


def is_holdout(case):
    return "holdout" in (case.get("split") or [])


def load_plan(source):
    try:
        if source == "-":
            text = sys.stdin.read()
        else:
            with open(source, encoding="utf-8") as fh:
                text = fh.read()
    except OSError as exc:
        bad_input(f"cannot read plan: {exc}")
    try:
        return loads_strict(text)
    except BadJSON as exc:
        bad_input(f"plan is not valid JSON: {exc}")


def dry_run_report(runner):
    """SS1: pre-flight and the resolved inputs, zero app calls, zero writes."""
    plan = runner.plan
    return {
        "dry_run": True,
        "run_id": plan["run_id"], "mode": plan["mode"], "k": plan["k"],
        "selecting_split": plan["selecting_split"],
        "out": runner.out,
        "invocation": {"mode": (runner.adapter.get("invocation") or {})
                       .get("mode"), "url": runner.record_url,
                       "timeout_s": runner.timeout()},
        "traces": runner.trace_state,
        "cases": [{"id": case["id"], "category": case.get("category"),
                   "canary": is_canary(case), "holdout": is_holdout(case),
                   "skip_reason": runner.skip_reason(case)}
                  for case in runner.ordered_cases()],
        "app_calls_planned": sum(
            plan["k"] for case in plan["cases"]
            if runner.skip_reason(case) is None),
        "execution": runner.execution,
    }


def validate_baseline(plan, path):
    """SS5.6 (D3) and the k-match (D7), checked at PLAN VALIDATION.

    Before any spend, because SS11's exit-2 row promises "none written" and
    every input to this check is knowable up front. Refusing after a full run
    would spend the suite to learn the comparison was never going to be legal.

    The baseline must sit beside its own manifest.yaml: version equality and
    the matching k are the comparison's rules, there is nowhere else to read
    them from, and skipping the check when the manifest is absent is exactly
    the silent-pass failure D3 folded this diff into the runner to prevent.
    """
    if not os.path.isfile(path):
        bad_input(f"--baseline-verdicts {path} is not a readable file")
    # stats.py is the tenth scorer and the only one this flag needs, so it is
    # checked here rather than in REQUIRED_SCORERS -- a run without the flag
    # must not be refused for a script it will never invoke.
    stats = os.path.join(plan["paths"]["scripts_dir"], "stats.py")
    if not os.path.isfile(stats):
        bad_input(f"--baseline-verdicts needs stats.py, which is not in "
                  f"{plan['paths']['scripts_dir']}")
    manifest_path = os.path.join(
        os.path.dirname(os.path.abspath(path)), "manifest.yaml")
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError) as exc:
        bad_input(
            "--baseline-verdicts must sit beside its run's manifest.yaml "
            f"({manifest_path}): {exc}; the comparison's rules are version "
            "equality and a matching k, and there is nowhere else to read "
            "them from")
    mismatches = []
    for key, ours in (
            ("dataset_version",
             (plan.get("manifest_extra") or {}).get("dataset_version")),
            ("harness_version", HARNESS_VERSION),
            ("k", plan["k"])):
        if manifest.get(key) != ours:
            mismatches.append(
                f"{key}: baseline {manifest.get(key)!r}, this run {ours!r}")
    if mismatches:
        bad_input(
            "baseline run {!r} is not comparable to this one -- {}. A "
            "different dataset or harness version changes what a verdict "
            "MEANS, and a different k pairs pass^k verdicts computed over "
            "different numbers of trials.".format(
                manifest.get("run_id"), "; ".join(mismatches)),
            baseline=manifest_path)


def verify_command(out_dir):
    """SS1: the completeness check alone, over an existing run directory.

    No app calls, no scoring, no writes -- so it can be pointed at any run,
    including one produced before this runner existed. A hand-orchestrated run
    missing its verdict rollups exits 6 here and names both files; that is the
    audit finding turned into a check anyone can re-run.
    """
    if not os.path.isdir(out_dir):
        bad_input(f"--verify {out_dir} is not a directory")
    missing = verify_run_dir(out_dir)
    if missing:
        raise RunnerExit(
            EXIT_INCOMPLETE,
            f"run directory {out_dir} is incomplete: {len(missing)} "
            "required artifact(s) missing or inconsistent",
            verified=out_dir, missing_artifacts=missing)
    print(json.dumps({"verified": out_dir, "status": "ok",
                      "missing_artifacts": []}, indent=2))
    return EXIT_OK


def run(plan, out_dir, resume=False, dry_run=False, baseline_verdicts=None):
    validate_plan(plan, out_dir)
    if baseline_verdicts:
        validate_baseline(plan, baseline_verdicts)
    runner = Runner(plan, out_dir, resume=resume, dry_run=dry_run,
                    baseline_verdicts=baseline_verdicts)

    if dry_run:
        runner.preflight()
        print(json.dumps(dry_run_report(runner), indent=2,
                         ensure_ascii=False))
        return EXIT_OK

    existing = os.path.isdir(out_dir) and os.listdir(out_dir)
    if resume:
        if not existing:
            bad_input(f"--resume: {out_dir} is empty or absent; there is no run here "
                      "to resume")
    elif existing:
        # The runner never silently merges into or overwrites an existing run:
        # the merged result would carry one run id over two selections.
        bad_input(f"--out {out_dir} is not empty; pass --resume to continue that run, "
                  "or choose a new run id")

    # The run directory is created inside pre-flight, not here: a pre-flight
    # failure is exit 3 with "manifest only, or nothing" on disk (SS11), and an
    # empty cases/ left behind by a failed pre-flight would make the next
    # attempt at the same run id look like a run in progress.
    already_done = runner.adopt_existing() if resume else set()
    runner.preflight()
    aborted = runner.adopted_abort() or runner.execute(already_done)
    return runner.finalize(aborted)


def main():
    ap = argparse.ArgumentParser(
        description="Execute a resolved evalup plan against the app: "
                    "invoke every selected case, write the per-case run "
                    "artifacts, and check that the run's required outputs "
                    "exist before reporting success.")
    add_version_flag(ap)
    ap.add_argument("--plan",
                    help="the plan.json document (docs/runner-contract.md "
                         "SS2); '-' reads stdin. Required except with --verify")
    ap.add_argument("--out",
                    help="the run directory to write; its basename must equal "
                         "the plan's run_id. Required except with --verify")
    ap.add_argument("--resume", action="store_true",
                    help="continue an interrupted run into an existing --out")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate, pre-flight, and print the resolved case "
                         "list; write nothing and call the app zero times")
    ap.add_argument("--baseline-verdicts", metavar="PATH",
                    help="a previous run's verdicts_for_stats.jsonl; pairs it "
                         "against this run's with stats.py and writes "
                         "comparison.json. Must sit beside its own "
                         "manifest.yaml, and refused up front when the "
                         "dataset version, harness version or k differ")
    ap.add_argument("--verify", metavar="DIR",
                    help="run ONLY the completeness check (SS9) over an "
                         "existing run directory and exit; no app calls, no "
                         "scoring, no writes")
    a = ap.parse_args()

    try:
        if a.verify:
            if any((a.plan, a.out, a.resume, a.dry_run, a.baseline_verdicts)):
                bad_input("--verify runs alone: it re-checks a finished run "
                          "directory and takes no plan, no output directory "
                          "and no scoring flags")
            return verify_command(a.verify)
        if not a.plan or not a.out:
            bad_input("--plan and --out are both required (or --verify DIR)")
        return run(load_plan(a.plan), a.out, resume=a.resume,
                   dry_run=a.dry_run,
                   baseline_verdicts=a.baseline_verdicts)
    except RunnerExit as exc:
        # Every non-zero exit prints {"error": ...} as JSON on STDOUT, so a
        # caller parses ONE shape regardless of outcome -- the scorers'
        # contract (_common.die), extended to a runner that has seven codes
        # instead of one.
        print(json.dumps(exc.payload(), indent=2, ensure_ascii=False))
        return exc.code
    except Exception as exc:  # noqa: BLE001 - exit 1 is the contract's own row
        import traceback
        traceback.print_exc()
        print(json.dumps({"error": f"internal error: {type(exc).__name__}: {exc}"}))
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
