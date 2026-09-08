#!/usr/bin/env python3
"""Execution engine for agent-eval: run one resolved plan against the app.

This is the implementation of ${CLAUDE_PLUGIN_ROOT}/docs/runner-contract.md,
which is the spec. Read that first; this module implements it and does not
re-argue it. Section markers below (SS2, SS4, ...) point at it.

WHY THIS EXISTS. run/SKILL.md SS2 "Execute" used to be hand-orchestrated by the
LLM on every run: not reproducible, expensive, and it silently dropped required
outputs. The proof is the shipped run at
field-test-qa/.agent-eval/reports/smoke-20260818T183920Z/, which looks finished
and has no verdicts.jsonl and no verdicts_for_stats.jsonl -- so stats.py has
nothing to pair and that run can never be a baseline. The mechanism that makes
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

STEP 5b-i SCOPE. The scoring layer table (contract SS5) is NOT wired yet: every
layer records verdict "unscored" with reason "scoring not wired yet (5b-ii)",
so every non-infra case rolls up to `unscored` and verdicts_for_stats.jsonl is
legitimately empty. What IS live: plan validation (SS2), env resolution (SS3),
pre-flight (SS4), the output tree and atomic writes (SS6), the serial execute
loop with the retry/infra taxonomy and both aborts (SS7), resume (SS8), the
derived jsonl files and the REQUIRED_ALWAYS/REQUIRED_PER_CASE half of the
completeness check (SS9), and the exit-code table (SS11). Exit 7 (scorer
errors) is unreachable until 5b-ii, and --verify plus the REQUIRED_IF
conditions land with it.

Usage:
  run_cases.py --plan <plan.json|-> --out <state>/reports/<run-id> [--resume]
  run_cases.py --plan <plan.json|-> --out <dir> --dry-run
"""
import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from _common import HARNESS_VERSION, add_version_flag

RUNNER_VERSION = 1
PLAN_VERSION = 1

# SS11. Distinct codes rather than 0/1/2 because 6 is the one that makes the
# shipped run's failure mode nameable, and once 6 exists the others cost
# nothing to distinguish. A RED SUITE EXITS 0: gating is run/SKILL.md SS5's
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

# SS9(b). The declared required-artifact table. REQUIRED_IF's conditions are
# checked in 5b-ii, once the layers that produce those files exist; the table
# is declared here in full so the two halves cannot drift apart.
REQUIRED_ALWAYS = ("manifest.yaml", "results.json", "verdicts.jsonl",
                   "verdicts_for_stats.jsonl")
REQUIRED_PER_CASE = ("request.json", "response.json", "verdict.json",
                     "expect.json", "answer.txt")
REQUIRED_IF = {
    "routing_results.jsonl": "any case is routing-scorable",
    "routing_report.json": "routing_results.jsonl exists",
    "repeats.jsonl": "k > 1",
    "reliability.json": "k > 1",
    "trajectory.json": "a trace was normalized for this case",
    "actual.json": "expect.result is present on this case",
    "trace.json": "traces.source is queryable this run",
}

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
# SS6: the holdout ledger is appended for these, and holdout_ledger: null with
# one of them is exit 2 rather than an uncounted look at sealed cases.
SEAL_TOUCHING_SPLITS = ("holdout", "full")

RUN_ID_RE = re.compile(r"^[a-z]+-\d{8}T\d{6}Z$")
ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# SS7's vocabulary, unchanged from the rest of the harness.
PASS, FAIL = "pass", "fail"
UNSCORED, SKIPPED = "unscored", "skipped"
INFRA_ERROR, INFRA_INCOMPLETE = "infra_error", "infra_incomplete"

# Categories the environment.safe_to_attack gate refuses outright (SS4.6/SS7).
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
    "infra_rate_abort": 0.25, "insecure_tls": False,
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
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
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
    touches_seal = (selecting in SEAL_TOUCHING_SPLITS
                    or plan["mode"] in SEAL_TOUCHING_SPLITS)
    if touches_seal and not ledger:
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
    return out, secret_paths, auth_values


def secret_header_names(adapter, secret_paths):
    """Header names whose VALUE must never be written to disk (SS3).

    Two sources, union: a header whose value came from an env ref, and any
    header named by the adapter's auth block regardless of where its value came
    from. The second half matters because an auth header with an inline value
    is a schema violation the adapter validator catches, but this runner must
    not be the thing that writes it into every request.json in the meantime.
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

REQUEST_PLACEHOLDERS = ("<user turn>", "<uuid>", "<persona>", "<case id>")
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
    """
    if isinstance(node, dict):
        return {key: render_template(value, values)
                for key, value in node.items()}
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
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx is a RESPONSE, not a transport failure -- and often the
        # expected one (the field test asserts a deliberate 400 on OOS). It
        # must reach the taxonomy in SS7 as a status, not as an exception.
        status = exc.code
        raw = exc.read()
        response_headers = dict(exc.headers.items()) if exc.headers else {}
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
            "headers": response_headers}


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
# Layer applicability (contract SS5's trigger column). 5b-i records every
# applicable layer as `unscored`; 5b-ii replaces the placeholder with the
# scorer shell-outs. The trigger rules live here, once, so the two halves
# cannot disagree about which layers a case even claims to exercise.
# --------------------------------------------------------------------------

SCORING_NOT_WIRED = "scoring not wired yet (5b-ii)"


def applicable_layers(case, trace_collected):
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
    if trace_collected:
        layers.append("loops")
    return layers


def placeholder_layers(case, trace_collected):
    return {name: {"layer": name, "verdict": UNSCORED,
                   "reason": SCORING_NOT_WIRED}
            for name in applicable_layers(case, trace_collected)}


def roll_up(layers, skipped_reason):
    """SS10's case-verdict rollup, first match wins.

    Rule 6 is the point of the whole ordering: a case whose every layer came
    back n/a / unscorable / unscored is NOT a pass. That is the vacuous-case
    failure validate_cases.py lints for at authoring time, caught again here at
    run time -- and it is why 5b-i's placeholder run reports `unscored` for
    every case instead of a green board.
    """
    values = [layer.get("verdict") for layer in layers.values()]
    if INFRA_ERROR in values:
        return INFRA_ERROR
    if INFRA_INCOMPLETE in values:
        return INFRA_INCOMPLETE
    if skipped_reason:
        return SKIPPED
    if FAIL in values:
        return FAIL
    if PASS in values:
        return PASS
    return UNSCORED


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


def flatten_layers(layers):
    return {name: layer.get("verdict") for name, layer in layers.items()}


# --------------------------------------------------------------------------
# The runner itself.
# --------------------------------------------------------------------------

class Runner:
    def __init__(self, plan, out_dir, resume=False, dry_run=False):
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

        self.adapter = {}
        self.auth_values = {}
        self.secret_names = set()
        self.identity_map = {}
        self.entrypoint = None
        self.request_template = None
        self.answer_path = None
        self.trace_id_path = None
        self.url = None
        self.method = "POST"

        self.trace_state = {"source": None, "correlation": None,
                            "collected": False, "disabled_layers": []}
        self.never_live_tools = set()
        self.verdicts = []          # verdict.json objects, in completion order
        self.attempted = 0
        self.crashes = 0
        self.scorer_errors = 0
        self.holdout_looks = None
        self.status = "running"

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
        self.adapter, secret_paths, self.auth_values = resolve_env(adapter)
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

        self.check_old_layout()
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

    def check_old_layout(self):
        """SS4.3. Lives here, not in the skill, because it must fire before
        spend -- and the field-test state dir is exactly this case."""
        reports = os.path.join(self.state_dir, "reports")
        if os.path.exists(os.path.join(reports, "baseline.json")):
            return
        stale = [name for name in ("baselines", "runs")
                 if os.path.isdir(os.path.join(self.state_dir, name))]
        if stale:
            preflight_fail(
                "state dir uses the pre-reports/<run-id> layout ({} present, "
                "reports/baseline.json absent); migrate with "
                "${{CLAUDE_PLUGIN_ROOT}}/docs/migrate-run-layout.md, or pass "
                "--baseline to deliberately pin this run and abandon the old "
                "one".format(", ".join(name + "/" for name in stale)))

    def health_check(self, mode):
        """SS4.4. One trivial request, or the entrypoint import.

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
        try:
            result = send_http(url, method, headers, None,
                               self.timeout(), self.execution["insecure_tls"])
        except TransportError as exc:
            preflight_fail(f"health check {method} {url} failed: {exc.message}")
        if expect_status and result["status"] not in expect_status:
            preflight_fail(
                "health check {} {} returned {}, expected one of {}".format(
                    method, url, result["status"], expect_status))
        return {"ok": True, "detail": "{} {}".format(method, result["status"]),
                "trace_id": self.read_trace_id(result)}

    def timeout(self):
        declared = self.execution.get("timeout_s")
        if declared:
            return declared
        return (self.adapter.get("invocation") or {}).get("timeout_s") or 60

    def trace_branch(self, health):
        """SS4.5. Decide, once for the whole run, whether traces are usable.

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
        # SS4.5's join check needs a real app call to join ON. Without a
        # declared health_check there is no such call, and inventing one is
        # spend the adapter never authorized -- so demand the declaration
        # rather than recording an unverified join as verified.
        if not health.get("trace_id"):
            preflight_fail(
                "traces are declared queryable with correlation "
                f"{correlation!r}, but no trace id came back from the "
                "health check; declare "
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
        """SS4.6. Record what the environment forbids; SS7 acts on it."""
        environment = self.adapter.get("environment") or {}
        kind = environment.get("kind") or ""
        tools = self.adapter.get("tools") or []
        if kind.startswith("live"):
            self.never_live_tools = {
                tool.get("name") for tool in tools
                if isinstance(tool, dict)
                and tool.get("side_effects") == "never-live"}

    def write_gitignore(self):
        """SS4.7. Written BEFORE the first case file of the run exists.

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
        selecting = self.plan["selecting_split"]
        if selecting not in SEAL_TOUCHING_SPLITS \
                and self.plan["mode"] not in SEAL_TOUCHING_SPLITS:
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
        row = {"run_id": self.plan["run_id"], "date": utc_now(),
               "mode": self.plan["mode"], "reason": "run"}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        with open(path, encoding="utf-8") as fh:
            self.holdout_looks = sum(1 for line in fh if line.strip())
        # stderr, not stdout: stdout carries the machine-readable {"error":...}
        # payload, and a caller parsing one shape must not find prose there.
        sys.stderr.write(
            f"holdout look recorded: {self.holdout_looks} total in {path}\n")

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
                           "url": self.url,
                           "timeout_s": self.timeout(),
                           "timeout_enforced":
                               invocation.get("mode") != "function"},
            "traces": traces,
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
        """
        correlation = (self.adapter.get("traces") or {}).get("correlation")
        body = result.get("body")
        if isinstance(correlation, str) \
                and correlation.startswith("response-field:"):
            field = correlation.split(":", 1)[1]
            return (body or {}).get(field) if isinstance(body, dict) else None
        if correlation == "traceparent-echo":
            header = result.get("headers", {}).get("traceparent")
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
            return False, ("no trace id on the response; correlation is "
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
        """Canaries first (run/SKILL.md SS2), then the plan's own order.

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
        if self.never_live_tools:
            named = set(case.get("available_tools") or [])
            tools = (case.get("expect") or {}).get("tools") or {}
            for key in ("subset", "order", "forbidden"):
                named.update(tools.get(key) or [])
            blocked = sorted(named & self.never_live_tools)
            if blocked:
                return ("never-live tool(s) {} with environment.kind {!r} "
                        "(adapter hard rule 2)".format(
                            ", ".join(blocked), environment.get("kind")))
        if user_turn_count(case) > 1 \
                and not (self.adapter.get("invocation") or {}).get("session"):
            return ("multi-turn case with no invocation.session contract "
                    "(adapter hard rule 3)")
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
                  "<persona>": persona, "<case id>": case["id"]}
        body = render_template(self.request_template, values)
        headers = build_headers(self.adapter, case, self.identity_map,
                                self.auth_values)
        request = {"case_id": case["id"], "repeat": repeat, "persona": persona,
                   "headers_sent": redact(headers, self.secret_names),
                   "url": self.url, "method": self.method, "body": body,
                   "sent_at": utc_now(), "sent": True}

        max_attempts = self.execution["max_attempts"]
        backoff = self.execution["backoff_s"]
        result, error, crashed, attempts = None, None, False, 0
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
            if attempts < max_attempts:
                self.log("retry", case_id=case["id"], repeat=repeat,
                         attempt=attempts, error=error)
                time.sleep(backoff[attempts - 1])

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
        return {"request": request, "response": response, "error": error,
                "crashed": crashed and error is not None}

    def execute_case(self, case):
        """Run one case (k repeats) and write its directory. verdict.json LAST.

        SS6: a case directory containing verdict.json is complete, by
        definition. Resume (SS8) and the completeness check (SS9) both key off
        exactly that fact, so it is the last atomic write here and nothing is
        written after it.
        """
        case_id = case["id"]
        case_dir = os.path.join(self.cases_dir, case_id)
        os.makedirs(case_dir, exist_ok=True)
        expect = case.get("expect") or {}
        # SS9/D9: expect.json and answer.txt live in the case dir so any run
        # can be RE-SCORED without re-invoking the app -- which is also how a
        # scorer bug fix gets applied to historical runs.
        atomic_write_json(os.path.join(case_dir, "expect.json"), expect)
        self.log("case_start", case_id=case_id, k=self.k)

        skip = self.skip_reason(case)
        if skip:
            record = self.skipped_record(case)
        else:
            self.attempted += 1
            records = [self.invoke_once(case, n) for n in range(1, self.k + 1)]
            record = records[0]
            if self.k > 1:
                repeat_verdicts = self.write_repeats(case_dir, case, records)
                # SS6: the case-level request/response are the REPRESENTATIVE
                # repeat -- the first FAILING one if any repeat failed, else
                # repeat 1. Deterministic, and it makes the report's example
                # excerpt the informative one rather than an arbitrary one.
                record = next(
                    (records[i] for i, rv in enumerate(repeat_verdicts)
                     if rv["verdict"] == FAIL), records[0])
            if any(r["crashed"] for r in records) \
                    and case.get("category") in CRASH_RATE_CATEGORIES:
                self.crashes += 1

        trace_collected, trace_reason = False, None
        if not skip and self.trace_state["collected"]:
            trace_collected, trace_reason = self.collect_trace(
                case_dir, record["response"]["trace_id"])

        atomic_write_json(os.path.join(case_dir, "request.json"),
                          record["request"])
        atomic_write_json(os.path.join(case_dir, "response.json"),
                          record["response"])
        atomic_write(os.path.join(case_dir, "answer.txt"),
                     record["response"]["answer"] or "")

        verdict = self.build_verdict(case, record, skip, trace_collected,
                                     trace_reason)
        if not skip and self.k > 1:
            verdict["repeats"] = repeat_verdicts
            # pass^k (decision D7): a case passes only if EVERY repeat passed.
            # The modes that use k>1 are asking for reliability, and a case
            # that passes 2 of 3 is not a case that passes. The pass@k/pass^k
            # gap is reported by reduce_repeats.py, never hidden.
            if all(rv["verdict"] == PASS for rv in repeat_verdicts):
                verdict["verdict"] = PASS
            elif any(rv["verdict"] == FAIL for rv in repeat_verdicts):
                verdict["verdict"] = FAIL
        atomic_write_json(os.path.join(case_dir, "verdict.json"), verdict)
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
        values = {"<user turn>": case_text(case), "<uuid>": None,
                  "<persona>": persona, "<case id>": case["id"]}
        headers = build_headers(self.adapter, case, self.identity_map,
                                self.auth_values)
        request = {"case_id": case["id"], "repeat": 1, "persona": persona,
                   "headers_sent": redact(headers, self.secret_names),
                   "url": self.url, "method": self.method,
                   "body": render_template(self.request_template, values),
                   "sent_at": None, "sent": False}
        response = {"case_id": case["id"], "repeat": 1, "status": None,
                    "latency_s": None, "attempts": 0, "retry_count": 0,
                    "body": None, "answer": None, "trace_id": None,
                    "error": None, "crashed": False}
        return {"request": request, "response": response, "error": None,
                "crashed": False}

    def write_repeats(self, case_dir, case, records):
        """SS6: repeats/ exists iff k > 1, and holds EVERY repeat.

        Including the first -- no asymmetry between "the run" and "the extra
        runs", which is the shape that makes a repeat directory readable
        without knowing which index the case-level files came from.
        """
        verdicts = []
        for index, record in enumerate(records, 1):
            repeat_dir = os.path.join(case_dir, "repeats", str(index))
            os.makedirs(repeat_dir, exist_ok=True)
            atomic_write_json(os.path.join(repeat_dir, "request.json"),
                              record["request"])
            atomic_write_json(os.path.join(repeat_dir, "response.json"),
                              record["response"])
            layers = self.layers_for(case, record, None, False, None)
            verdict = {"case_id": case["id"], "repeat": index,
                       "verdict": roll_up(layers, None), "layers": layers}
            atomic_write_json(os.path.join(repeat_dir, "verdict.json"),
                              verdict)
            verdicts.append(verdict)
        return verdicts

    def layers_for(self, case, record, skip, trace_collected, trace_reason):
        """5b-i's placeholder layer table (contract SS5 lands in 5b-ii).

        The INFRA rows are real, though: SS7's taxonomy is this step's, so an
        exhausted retry budget really does produce infra_error, and a trace
        that never joined really does produce infra_incomplete on the
        trace-dependent layers and nothing else.
        """
        layers = placeholder_layers(case, trace_collected)
        if skip:
            for layer in layers.values():
                layer["verdict"] = SKIPPED
                layer["reason"] = skip
            return layers
        if record["error"]:
            for layer in layers.values():
                layer["verdict"] = INFRA_ERROR
                layer["reason"] = record["error"]
            return layers
        if trace_reason:
            for name in TRACE_DEPENDENT_LAYERS:
                if name in layers:
                    layers[name]["verdict"] = INFRA_INCOMPLETE
                    layers[name]["reason"] = trace_reason
        return layers

    def build_verdict(self, case, record, skip, trace_collected, trace_reason):
        layers = self.layers_for(case, record, skip, trace_collected,
                                 trace_reason)
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
            "verdict": roll_up(layers, skip),
            "k": self.k,
            # Carried on the verdict, not only in response.json, so a resumed
            # run can rebuild results.json's latency column from the same
            # source as everything else in it (SS8 step 3).
            "latency_s": record["response"]["latency_s"],
            "layers": layers,
            "trace": {"expected": expected_trace,
                      "collected": trace_collected,
                      "reason": trace_reason or (
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
            atomic_write_jsonl(os.path.join(self.out, "repeats.jsonl"), [
                {"case_id": v["case_id"], "verdict": repeat["verdict"]}
                for v in self.verdicts for repeat in (v["repeats"] or [])])

    def summary(self, status, missing):
        verdicts = self.verdicts
        graded = [v for v in verdicts if not v["canary"]]
        counts = {value: sum(1 for v in graded if v["verdict"] == value)
                  for value in (PASS, FAIL, UNSCORED, SKIPPED, INFRA_ERROR,
                                INFRA_INCOMPLETE)}
        infra = counts[INFRA_ERROR] + counts[INFRA_INCOMPLETE]
        canaries = [v for v in verdicts if v["canary"]]
        holdouts = [v for v in verdicts if v["holdout"]]
        disabled = sorted(
            {name for name, block in self.plan["capability_matrix"].items()
             if isinstance(block, dict) and block.get("enabled") is False}
            | set(self.trace_state["disabled_layers"]))
        return {
            "status": status,
            "n": len(graded), "attempted": self.attempted,
            "passes": counts[PASS], "failures": counts[FAIL],
            "gating_failures": sum(1 for v in graded
                                   if v["gating"] and v["verdict"] == FAIL),
            "unscored": counts[UNSCORED], "skipped": counts[SKIPPED],
            "infra_errors": infra,
            "infra_rate": round(infra / self.attempted, 4)
            if self.attempted else 0.0,
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
        if judge.get("status") == "calibrated":
            return "deferred to skill"
        return "judge not calibrated"

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
            if verdict["verdict"] != SKIPPED:
                self.attempted += 1
            if self.crashed_on_disk(case):
                self.crashes += 1
        self.refresh_derived()
        return {v["case_id"] for v in self.verdicts}

    def crashed_on_disk(self, case):
        if case.get("category") not in CRASH_RATE_CATEGORIES:
            return False
        path = os.path.join(self.cases_dir, case["id"], "response.json")
        try:
            return bool(read_json(path).get("crashed"))
        except (OSError, ValueError):
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
                        "infra rate {:.0%} exceeds infra_rate_abort {:.0%} "
                        "after {} attempted cases".format(
                            rate, self.execution["infra_rate_abort"],
                            self.attempted))
        return None

    def check_completeness(self):
        """SS9(c), the half 5b-i can honestly assert.

        REQUIRED_ALWAYS and REQUIRED_PER_CASE are checked here; the REQUIRED_IF
        conditions and the count-agreement checks (SS9(c) 3-4) land with the
        scoring layer in 5b-ii, together with --verify. What this already buys
        is the property the shipped run lacked: `status: "ok"` is written ONLY
        after a check, so "the run finished" and "the run's required outputs
        exist" are the same statement.
        """
        missing = []
        for name in REQUIRED_ALWAYS:
            path = os.path.join(self.out, name)
            if not os.path.isfile(path):
                missing.append(name)
                continue
            if name.endswith(".jsonl"):
                continue          # an empty jsonl is legitimate (0 pass/fail)
            if os.path.getsize(path) == 0:
                missing.append(f"{name} (empty)")
                continue
            try:
                read_json(path)
            except (OSError, ValueError) as exc:
                missing.append(f"{name} (unparseable: {exc})")
        for verdict in self.verdicts:
            for name in REQUIRED_PER_CASE:
                relative = os.path.join("cases", verdict["case_id"], name)
                if not os.path.isfile(os.path.join(self.out, relative)):
                    missing.append(relative)
        return missing

    def finalize(self, aborted):
        status, code = ("ok", EXIT_OK) if not aborted else aborted[:2]
        missing = self.check_completeness()
        if missing:
            status, code = "incomplete", EXIT_INCOMPLETE
        self.status = status
        self.write_results(status=status, missing=missing, exit_code=code)
        self.log("finalize", status=status, exit_code=code,
                 missing=len(missing))
        if missing:
            raise RunnerExit(
                EXIT_INCOMPLETE,
                "run {} is incomplete: {} required artifact(s) missing or "
                "unreadable".format(self.plan["run_id"], len(missing)),
                missing_artifacts=missing)
        if aborted:
            raise RunnerExit(code, aborted[2])
        return EXIT_OK


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
        return json.loads(text)
    except json.JSONDecodeError as exc:
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
                       .get("mode"), "url": runner.url,
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


def run(plan, out_dir, resume=False, dry_run=False):
    validate_plan(plan, out_dir)
    runner = Runner(plan, out_dir, resume=resume, dry_run=dry_run)

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
    aborted = runner.execute(already_done)
    return runner.finalize(aborted)


def main():
    ap = argparse.ArgumentParser(
        description="Execute a resolved agent-eval plan against the app: "
                    "invoke every selected case, write the per-case run "
                    "artifacts, and check that the run's required outputs "
                    "exist before reporting success.")
    add_version_flag(ap)
    ap.add_argument("--plan", required=True,
                    help="the plan.json document (docs/runner-contract.md "
                         "SS2); '-' reads stdin")
    ap.add_argument("--out", required=True,
                    help="the run directory to write; its basename must equal "
                         "the plan's run_id")
    ap.add_argument("--resume", action="store_true",
                    help="continue an interrupted run into an existing --out")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate, pre-flight, and print the resolved case "
                         "list; write nothing and call the app zero times")
    a = ap.parse_args()

    try:
        return run(load_plan(a.plan), a.out, resume=a.resume,
                   dry_run=a.dry_run)
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
