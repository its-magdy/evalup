#!/usr/bin/env python3
"""Argument-correctness scorer for agent-eval.

Checks a case's expect.args assertions against the observed tool calls in a
normalized trajectory. Expectation kinds (see case-format.md):
  present          - the arg exists on the call with a non-empty value
  from_tool_result - the arg's value appears in the result content of an
                     EARLIER tool call (requires content capture; without
                     captured results the check is "unscorable", never a fail)
  <anything else>  - literal: observed must equal it (str-compared as fallback)

from_tool_result matching is token-bounded (the value must not be a fragment
of a longer token: INV-1 does not match inside INV-10) and non-string results
are matched against their JSON serialization. Every comparison here — literal
values, argument names, and provenance needles/haystacks — runs on
NFC-normalized text (see _common.nfc and _common.stringify). On the
trajectory's FIRST tool call the check FAILS — no earlier result exists to
source the value from, so provenance is impossible regardless of content
capture. Values shorter than 3 characters carry no provenance signal and are
"unscorable".

Call scope. A tool is routinely called more than once in a trajectory (search
q=invoices, then search limit=10 to page), and an expectation written about
one of those calls is not an assertion about all of them. Each tool's
expectation entry therefore accepts a reserved key "calls":

  any   - (default) the check passes if AT LEAST ONE call to the tool
          satisfies every arg spec in the entry. The specs are evaluated
          together, per call, so "the call that did X also did Y" stays
          expressible.
  all   - every observed call must satisfy every spec.
  first - only the tool's first call is checked.

"any" is the default deliberately: "all" was, and produced a fail for any
agent that legitimately called the same tool twice with different arguments —
a manufactured failure, which this harness must never produce. Case authors
who mean "every call" now say so.

"calls" joins "present"/"from_tool_result" in the reserved-word set: those two
are reserved as expectation VALUES (an arg literally expected to equal the
string "present" is not expressible), "calls" is reserved as a KEY (a tool
argument actually named "calls" cannot be asserted on). Both are documented
in case-format.md.

An entry that asserts nothing once "calls" is peeled off (search: {calls: all},
search: {}) is an INPUT ERROR, not an empty check: it would score every
trajectory while leaving no row in the output to say so. "This tool must be
called" is expect.tools' assertion, not this layer's.

A tool that was never called is reported "tool_not_called" and not scored
here — missing tools are the trajectory layer's finding, not an argument
error.

Usage: score_args.py <trajectory.json> <case_expect.json>
  trajectory.json: normalize_trace.py output, or {"tool_calls": [...]}
  case_expect.json: the case's "expect" object; reads its "args" mapping
Verdict: pass (>=1 check passed, none failed) | fail (any check failed) |
unscored (nothing scorable). Exit 0 always; exit 2 on malformed input.
"""
import argparse
import json
from itertools import islice

from _common import (
    add_version_flag,
    die,
    found_in,
    load_object,
    load_trajectory,
    nfc,
    optional_mapping,
    stringify,
)

CALL_SCOPES = ("any", "all", "first")
CALLS_KEY = "calls"


def value_matches(observed, expected):
    """The literal-expectation comparison. Both sides NFC-normalized (see
    _common.nfc): the expectation is authored in a case file and the observed
    value comes back from the app, so the two arrive through different editors
    and input methods, and an unnormalized comparison reported `expected 'café',
    observed 'café'` — a fail whose own evidence shows the two as identical.
    nfc passes non-strings through, so numbers and containers compare exactly as
    before."""
    observed, expected = nfc(observed), nfc(expected)
    return observed == expected or nfc(str(observed)) == nfc(str(expected))


def check_call(idx, call, arg_name, expectation, results,
               prior_count=0, prior_errors=0):
    """`prior_count` is how many captured results precede this call and
    `prior_errors` how many earlier calls errored. The count indexes into
    `results` rather than slicing it, so the provenance evidence for each call
    costs no copy."""
    args = call.get("args")
    if not isinstance(args, dict) or args.get("_unparsed"):
        return ("unscorable", "call arguments not captured/parseable")
    # Arg NAMES are compared in NFC too, on both sides. Rare — parameter names
    # are usually ASCII identifiers — but the failure it prevents is the loudest
    # one this scorer has: "arg 'X' absent from call #0" for an argument that is
    # right there in the call, spelled the other way.
    args = {nfc(k): v for k, v in args.items()}
    arg_name = nfc(arg_name)
    if arg_name not in args:
        return ("fail", f"arg {arg_name!r} absent from call #{idx}")
    val = args[arg_name]
    if expectation == "present":
        if val in (None, "", [], {}):
            return ("fail", f"arg {arg_name!r} empty on call #{idx}")
        return ("pass", None)
    if expectation == "from_tool_result":
        if idx == 0:
            return ("fail", f"arg {arg_name!r}={val!r} on call #{idx}: no "
                            "earlier tool call exists to source this value "
                            "from (provenance impossible)")
        if not prior_count:
            # Two different worlds, and guessing "content capture off" for both
            # sent the reader to the exporter config when the actual finding
            # was that every earlier tool call had FAILED. normalize_trace
            # records the failures now (checks.tool_calls_errored), so the
            # reason can name the one that applies.
            if prior_errors:
                return ("unscorable",
                        f"no prior tool result to source from: "
                        f"{prior_errors} earlier tool call(s) errored")
            return ("unscorable",
                    "no prior tool results captured (content capture off?)")
        # A list-valued arg (e.g. unitIds=[3]) commonly sources each element
        # from a separate scalar field in a prior result (e.g. {"id": 3}),
        # never from the bracketed literal "[3]" itself. Check element-wise
        # so a list built from legitimate scalars isn't flagged as
        # hallucinated just because its serialized form never appears verbatim.
        elements = val if isinstance(val, list) else [val]
        if not elements:
            return ("unscorable", f"value {val!r} too short to carry "
                                  "provenance signal")
        prior_results = list(islice(results, prior_count))
        for element in elements:
            needle = stringify(element)
            if len(needle) < 3:
                return ("unscorable", f"value {element!r} too short to carry "
                                      "provenance signal")
            if not found_in(needle, prior_results):
                return ("fail", f"arg {arg_name!r}={val!r} on call #{idx}: "
                                f"element {element!r} not found in any prior "
                                "tool result (possible hallucinated arg)")
        return ("pass", None)
    if value_matches(val, expectation):
        return ("pass", None)
    return ("fail", f"arg {arg_name!r} on call #{idx}: "
                    f"expected {expectation!r}, observed {val!r}")


def split_scope(tool, assertions):
    """Peel the reserved "calls" key off a tool's expectation entry, returning
    (scope, arg specs). An unknown scope is an input error, not a silent
    fallback to the default: the three scopes give three different verdicts on
    the same trajectory, so a typo'd one must never quietly pick one."""
    scope = assertions.get(CALLS_KEY, "any")
    if not isinstance(scope, str) or scope not in CALL_SCOPES:
        die(f"expect.args.{tool}.{CALLS_KEY} must be one of "
            f"{list(CALL_SCOPES)}, got {scope!r}")
    specs = {k: v for k, v in assertions.items() if k != CALLS_KEY}
    # Nothing left after the peel means the author named a tool and asserted
    # nothing about it: `search: {calls: all}`, or `search: {}` / `search:` via
    # optional_mapping. score_tool would emit zero checks, so the entry
    # contributes nothing to the verdict AND leaves no row in the output — not
    # even tool_not_called. Alone that reads "unscored" (which the run skill
    # treats as a hard error), but beside any other tool carrying a real spec
    # the case reports a clean "pass" with the entry invisible. Same class as
    # trajectory_match.asserted_list, and the same discipline the scope check
    # above already applies: a malformed expectation is an input error, never
    # the loosest possible check.
    if not specs:
        die(f"expect.args.{tool} asserts nothing; an entry with no arg specs "
            f"is never evaluated and passes every trajectory — give it an arg "
            f"expectation, or remove the key (a tool that must merely be "
            f"called belongs in expect.tools)")
    return scope, specs


def score_one_call(idx, call, specs, captured, prefix):
    """Every arg spec evaluated against ONE call, in expectation order."""
    return [(arg, expectation,
             *check_call(idx, call, arg, expectation, captured, *prefix[idx]))
            for arg, expectation in specs.items()]


def best_effort_call(scored):
    """When no call satisfies every spec under scope "any", one call's results
    still have to be reported — reporting all of them would print N failures
    for what is a single unmet expectation. Pick the call that came closest:
    most passes first, then fewest outright fails, so a call whose args simply
    weren't captured (unscorable) is preferred as the explanation over one that
    genuinely contradicted the expectation only when it matched more specs."""
    def rank(entry):
        rows = entry[1]
        return (sum(s == "pass" for _, _, s, _ in rows),
                -sum(s == "fail" for _, _, s, _ in rows))

    return max(scored, key=rank)


def score_tool(tool, scope, specs, indexed, captured, prefix):
    scored = [(i, score_one_call(i, call, specs, captured, prefix))
              for i, call in indexed]
    if scope == "first":
        chosen = [scored[0]]
    elif scope == "all":
        chosen = scored
    else:
        satisfied = next((e for e in scored
                          if all(s == "pass" for _, _, s, _ in e[1])), None)
        chosen = [satisfied or best_effort_call(scored)]
    note = None
    if scope == "any" and len(scored) > 1:
        note = (f"scope 'any': {len(scored)} calls to {tool!r}; reporting the "
                "call that best satisfies this expectation")
    checks = []
    for i, rows in chosen:
        for arg, expectation, status, reason in rows:
            checks.append({"tool": tool, "arg": arg,
                           "expectation": expectation, "call_index": i,
                           "calls": scope, "status": status,
                           **({"reason": reason} if reason else {}),
                           **({"note": note} if note else {})})
    return checks


def main():
    ap = argparse.ArgumentParser(
        description="Score expect.args assertions against a normalized "
                    "trajectory (see case-format.md).")
    add_version_flag(ap)
    ap.add_argument("trajectory", help="normalized trajectory JSON")
    ap.add_argument("expect", help="case 'expect' object JSON")
    a = ap.parse_args()
    calls = load_trajectory(a.trajectory)
    # `is not None`, not truthiness: the truthy test skipped validation for the
    # off-shape values that are also falsy, so `args: []` was silently dropped
    # and the case reported "unscored" rather than naming the bad shape.
    expect_args = optional_mapping("expect.args",
                                   load_object(a.expect).get("args"))

    # Serialized once per call, not once per (tool, arg, call) triple: the
    # serialization depends on nothing the assertion loop below does.
    # `prefix[i]` is (how much of `captured` was captured strictly before call
    # i, how many calls before i errored) — one index-aligned list instead of
    # three, and the count is handed to check_call as a bound rather than as a
    # slice, so the evidence for each call costs no copy.
    captured, prefix = [], []
    errors = 0
    for c in calls:
        prefix.append((len(captured), errors))
        if c.get("error") is not None:
            errors += 1
        result = c.get("result")
        if result is not None:
            captured.append(stringify(result))

    checks = []
    for tool, assertions in expect_args.items():
        # Validated BEFORE the tool_not_called short-circuit below: a malformed
        # expectation is an input error whether or not the tool happened to be
        # called, so the error must not depend on the trajectory. Previously
        # `(assertions or {}).items()` raised AttributeError on a scalar — a
        # traceback and exit 1, outside the exit-2 contract this scorer shares.
        assertions = optional_mapping(f"expect.args.{tool}", assertions)
        scope, specs = split_scope(tool, assertions)
        # NFC on both sides (see _common.nfc): the key is written in the case
        # file, the name comes back from the app. Mismatched forms report
        # "tool_not_called" for a tool the trajectory clearly contains.
        indexed = [(i, c) for i, c in enumerate(calls)
                   if nfc(c.get("name")) == nfc(tool)]
        if not indexed:
            checks.append({"tool": tool, "status": "tool_not_called",
                           "note": "missing tool is a trajectory-layer "
                                   "finding, not scored here"})
            continue
        checks += score_tool(tool, scope, specs, indexed, captured, prefix)

    statuses = [c["status"] for c in checks]
    if "fail" in statuses:
        verdict = "fail"
    elif "pass" in statuses:
        verdict = "pass"
    else:
        verdict = "unscored"
    print(json.dumps({
        "layer": "args",
        "verdict": verdict,
        "checks": checks,
        "unscorable": sum(s == "unscorable" for s in statuses),
    }, indent=2))


if __name__ == "__main__":
    main()
