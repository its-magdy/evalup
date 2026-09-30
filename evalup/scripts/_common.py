"""Shared input loading for the evalup scorers.

One error contract for every scorer: on bad input print {"error": "<message>"}
to stdout and exit 2. One TEXT contract too: every string comparison in this
package runs on NFC-normalized text (see nfc), so a case authored in one
Unicode form still matches an app that answers in the other.

Loaders are strict about presence (a missing required key is an error, never
an empty default — defaulting would score an unread trace as an empty one) and
lenient about wrapping (normalize_trace.py's full output and a bare trajectory
both work).
"""
import json
import re
import signal
import sys
import threading
import unicodedata
from collections.abc import Hashable

# The comparability key. run/SKILL.md refuses a baseline diff across differing
# harness versions (a scorer change alters what a number means), so the value
# has to come from the harness itself rather than being invented by the
# orchestrating skill. Bump this in lockstep with .claude-plugin/plugin.json —
# tests/test_scorers.py asserts the two match.
HARNESS_VERSION = "0.1.0"


def add_version_flag(ap):
    """Every scorer answers --version with the same string, so a run manifest
    can record the harness version mechanically."""
    ap.add_argument("--version", action="version",
                    version=f"evalup harness {HARNESS_VERSION}")
    return ap


def die(message):
    """The single exit path for bad input. Use this rather than hand-rolling
    the same print+exit: drift in the error SHAPE is invisible in tests that
    only assert the exit code, and the orchestrating skill parses this object.

    Errors go to STDOUT, not stderr, deliberately. clig.dev pulls both ways —
    "log messages, errors, and so on should all be sent to stderr", but also
    "anything that is machine readable should also go to stdout". This object
    is machine-readable output consumed by the skill that invoked the scorer,
    so it follows the second rule; exit 2 is what signals failure.

    Extra keys on top of "error" are allowed when a script's success output
    already carries them: normalize_trace.py emits {"status": "error", ...}
    because every one of its outputs carries a status ("ok"/"incomplete"), so
    a consumer switching on that field sees a coherent tri-state. "error" is
    the part every consumer can rely on."""
    print(json.dumps({"error": message}))
    sys.exit(2)


def require_range(flag, value, lo=None, hi=None, exclusive=False, note=""):
    """Bounds-check a numeric CLI flag, dying through the shared error contract.

    Validated here rather than via an argparse `type=` callable on purpose:
    argparse reports its own errors as usage text on STDERR and exits 2 without
    a payload, which breaks the machine-readable-JSON-on-stdout contract every
    scorer shares (see die). Out of range, these values reached the arithmetic
    and exited with a traceback instead.

    `note` appends the reason the bound exists, so the message says what to do
    rather than only what was rejected."""
    cmp = (lambda a, b: a < b) if exclusive else (lambda a, b: a <= b)
    if (lo is None or cmp(lo, value)) and (hi is None or cmp(value, hi)):
        return value
    if lo is not None and hi is not None:
        bound = (f"between {lo} and {hi}"
                 f"{' (exclusive)' if exclusive else ''}")
    elif lo is not None:
        bound = f"{'>' if exclusive else '>='} {lo}"
    else:
        bound = f"{'<' if exclusive else '<='} {hi}"
    die(f"{flag} must be {bound}{note}, got {value}")


def require_list(field, value, item="entry", plural=None, of_strings=True):
    """An expectation field that must be a list IS a list, or it is an input
    error — never coerced, never iterated as-is.

    A bare string is the plausible YAML slip (`forbidden_tools: delete_user`
    instead of `[delete_user]`), and a string IS a sequence — of CHARACTERS.
    Every consumer of these fields iterates them, so the slip does not raise;
    it silently scores a different, meaningless assertion. Both directions are
    reachable and both are unacceptable:

      - `expect.authz.forbidden_tools: delete_user` tested 'd','e','l',... as
        tool names against the call log. None is a real tool, so the authz
        gate reported PASS on a trajectory that DID call delete_user — the
        gate silently passing exactly what it exists to catch.
      - `expect.answer.must_not_contain: zebra` FAILED a correct answer,
        because some of 'z','e','b','r','a' appear in almost any prose — a
        manufactured failure, the one outcome this harness must never produce.

    So an off-shape expectation is an input error, not a mode. Same rule
    score_execution.py applies to expect.result.columns, written once here so
    the scorers cannot drift apart on it.

    `item` names the noun the message uses ("tool name", "record id", "route
    name"); it defaults to the domain-neutral "entry" so a caller that forgets
    it reports a vague message rather than a wrong one.

    `of_strings=False` for entry lists whose elements are legitimately not all
    strings (expect.answer.must_contain accepts an unquoted number and reports
    a bool as unscorable — see score_answer.normalize_entry, which owns the
    per-entry rule). This function validates the CONTAINER; the container is
    the level the slip happens at."""
    if not isinstance(value, list):
        leaf = field.rsplit(".", 1)[-1]
        hint = (f"; write `{leaf}: [{value}]` if you meant a single {item}"
                if isinstance(value, str) else "")
        die(f"{field} must be a list of {plural or item + 's'}, got "
            f"{type(value).__name__}{hint}")
    if of_strings:
        for i, entry in enumerate(value):
            if not isinstance(entry, str):
                die(f"{field}[{i}] must be a {item} (string), got "
                    f"{type(entry).__name__}")
    return value


def require_mapping(field, value):
    """The object-valued sibling of require_list. A non-object here reached
    .items() and raised AttributeError — a traceback and exit 1, which is
    neither the {"error": ...} payload this module promises nor the exit 2 the
    run skill branches on."""
    if not isinstance(value, dict):
        die(f"{field} must be an object, got {type(value).__name__}")
    return value


def optional_mapping(field, value):
    """An expectation block that may be absent: None is "nothing to score",
    anything else must be an object.

    Written once here because every scorer needs it and each one that
    re-derived it got it wrong the same way: `x or {}` tests TRUTHINESS, so a
    present-but-off-shape falsy value (`answer: []`, `args: []`) was silently
    dropped and the case reported "unscored" — an expectation the author wrote
    was never evaluated and nothing said so. Only ABSENCE may default."""
    return {} if value is None else require_mapping(field, value)


def optional_list(field, value, **kwargs):
    """The list-valued sibling of optional_mapping: absent means the check does
    not apply (empty list), a present but off-shape value is an input error.
    See require_list for why an off-shape list is never coerced."""
    return [] if value is None else require_list(field, value, **kwargs)


def nfc(value):
    """Unicode NFC, the harness's one canonical form for text comparison.

    Every string check in this package compares a HUMAN-AUTHORED expectation
    against APP-PRODUCED text, and those two strings reach the scorer through
    different keyboards, editors and filesystems. "cafe\u0301" (NFD) and
    "caf\u00e9" (NFC) render identically in every terminal and reviewer's eye,
    and compared as code points they are simply unequal — so a correct answer
    scored `fail` and a case author reading the report saw the expected and
    observed values print the SAME. A manufactured failure that is invisible in
    its own evidence is the worst shape this harness can produce.

    macOS filesystems and editors emit NFD while most web input arrives NFC, so
    the mismatch needs no exotic data to appear; `generate` lists language as a
    generation dimension, and scripts with heavy combining-mark use (Arabic
    diacritics, Vietnamese, Hebrew niqqud) hit it constantly.

    NFC rather than NFD or a casefold: NFC is the composed form the web,
    JSON and virtually every corpus already use, so it is the least surprising
    canonical form and the cheapest to reach from real inputs. This is a
    NORMALIZATION, never a fuzzy match — it makes two spellings of the SAME
    character compare equal and changes nothing else (it does not fold case,
    strip accents, or touch whitespace; those are each scorer's own business).

    Non-strings pass through untouched so call sites can normalize a cell, an
    argument value or a label without first testing its type."""
    return unicodedata.normalize("NFC", value) if isinstance(value, str) \
        else value


def stringify(value):
    """A tool-call result (or any JSON value) as text, for substring/provenance
    matching. One serialization for every scorer that searches results: drift
    here would make two scorers disagree about what a result "contains".

    NFC-normalized (see nfc) and serialized with ensure_ascii=False. The
    default ensure_ascii=True escapes every non-ASCII character, so a result
    holding {"customer": "Al Mu\u2018tasim"} became the literal text
    "Al Mu\\u2018tasim" — and a from_tool_result provenance check on an
    argument the app copied verbatim OUT of that result could never find it,
    reporting "possible hallucinated arg" for a value whose provenance was
    perfect. Normalizing would not have helped: the two sides were not in
    different Unicode forms, one of them was not Unicode text at all."""
    return nfc(value if isinstance(value, str)
               else json.dumps(value, ensure_ascii=False))


REGEX_TIMEOUT_S = 2.0


class RegexTimeout(Exception):
    pass


def run_bounded(fn):
    """fn() under a wall-clock bound, so one pathological author-supplied regex
    cannot hang a run.

    Here, not in score_answer.py where it started, because a second scorer
    takes a regex from the author -- score_authz.py's --id-pattern, which
    run_cases.py passes on EVERY authz-scorable case from the profile's
    record_id_pattern -- and it had no bound at all: `(a+)+$` stalled each case
    for the runner's full 30s scorer timeout and hung the standalone CLI for
    good (2026-09 audit). Same reason found_in lives here: two gates, one rule.

    SIGALRM is Unix-only and main-thread-only. Where either fails the call
    runs unguarded rather than not at all -- the docstring always said so, but
    the guard tested only for SIGALRM, so off the main thread signal.signal
    raised ValueError instead."""
    if not hasattr(signal, "SIGALRM") or \
            threading.current_thread() is not threading.main_thread():
        return fn()

    def on_alarm(signum, frame):
        raise RegexTimeout

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, REGEX_TIMEOUT_S)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


CASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# <mode>-<YYYYMMDDTHHMMSSZ>: run_cases.py mints it, make_plan.py accepts it,
# gate.py --latest parses it. Use fullmatch; the groups are (mode, stamp).
RUN_ID_RE = re.compile(r"([a-z]+)-(\d{8}T\d{6}Z)")

# runner layer (run_cases.LAYER_ORDER) -> the capability_matrix key that
# switches it off. The matrix is the profile's vocabulary and the layers are
# the runner's, and they differ in one place: `answer_quality` is ONE
# capability scored as three rows (`answer`, `rules`, `judged`). The runner
# used to look each row up under its own name, so `answer_quality: {enabled:
# false}` -- which is what `make_plan.py --layer X` writes -- disabled nothing
# and the answer layer went on deciding cases the linter had called ungraded.
# None = no key: `http` is the liveness row every case gets and `loops` is
# triggered by the run having a trace, so neither is a capability a profile
# declares and nothing switches either off. Written once here because the
# runner, make_plan.py and the linter's tests must agree on it.
MATRIX_KEY_OF_LAYER = {
    "http": None,
    "routing": "routing",
    "trajectory": "trajectory",
    "tool_selection": "tool_selection",
    "loops": None,
    "answer": "answer_quality",
    "execution": "execution",
    "authz": "authz",
    "rules": "answer_quality",
    "state": "state",
    "judged": "answer_quality",
}


def unsafe_case_id(case_id):
    """Why `case_id` cannot name a directory, or None when it can.

    A case id IS a path segment: the runner writes cases/<id>/, and every
    report and baseline diff keys on it. It was checked only for being a
    non-empty unique string, so `../../../../X` -- or an absolute path, which
    os.path.join lets replace the whole prefix -- wrote six files outside --out
    before the completeness check noticed (2026-09 audit). Written once here
    because the linter and the runner must refuse the same ids: the linter so
    the author hears at authoring time, the runner because it is the one that
    touches the disk and is not always preceded by the linter.

    generate's own ids are `c-<hash8>`; this is deliberately looser than that,
    so a hand-named `billing_refund.v2` stays legal."""
    if not CASE_ID_RE.fullmatch(case_id) or ".." in case_id:
        return ("must be 1-128 of [A-Za-z0-9._-], starting with a letter or "
                "digit, with no '..' -- the id names the cases/<id>/ directory")
    return None


def found_in(needle, haystacks):
    """Is `needle` present in any haystack as a whole token?

    Token-bounded: the needle must not continue into a longer identifier-like
    token on either side (letters, digits, '.', '-'), so INV-1 does not match
    inside INV-10. Written once here because two gates depend on it and they
    must not drift: score_args reads it as provenance for an argument value,
    score_authz reads it as evidence that a forbidden record leaked. A looser
    boundary in one of them turns a leak into a pass.

    Both sides are NFC-normalized (see nfc) — including the haystacks, which
    usually arrive through stringify already but not always (score_authz adds
    the raw answer text), so the guarantee is stated here rather than assumed
    of every caller."""
    pattern = r"(?<![\w.\-])" + re.escape(nfc(needle)) + r"(?![\w.\-])"
    return any(re.search(pattern, nfc(h)) for h in haystacks)


def load_text(path, on_error=None):
    """Read a whole text file through the shared error contract.

    Every read in this harness passes encoding="utf-8" explicitly rather than
    taking the locale default (PEP 597; Ruff PLW1514). The default is not
    UTF-8 everywhere, and both of its failure modes corrupt a verdict: under
    an ASCII locale a valid answer containing "€" or an em-dash raised
    UnicodeDecodeError — a traceback and exit 1, outside the exit-2 contract
    entirely — and under latin-1/cp1252 it did not raise at all, silently
    decoding to mojibake so a must_contain check on the euro sign FAILED a
    correct answer. A verdict must not depend on the reviewer's locale.

    UnicodeDecodeError is caught separately because it is a ValueError, not an
    OSError, so the OSError arm below never saw it.

    `on_error` overrides the exit path for scripts whose error payload carries
    extra keys (normalize_trace.py's {"status": "error", ...}); see die. Every
    other reader in this module goes through this one, so the encoding contract
    above is stated once rather than re-derived per format."""
    fail = on_error or die
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError as e:
        fail(f"bad input: {path}: not valid utf-8 text: {e}")
    except OSError as e:
        fail(f"bad input: {e}")


def write_output(path, text, on_error=None):
    """Write `text` to `path`, or to stdout when `path` is falsy.

    The mirror of load_text, and utf-8-explicit for the same reason: the write
    side of a report has the same locale-dependent failure the read side does,
    and an OSError here (unwritable directory, full disk) must land in the
    {"error": ...}/exit-2 contract rather than as a traceback."""
    if not path:
        sys.stdout.write(text)
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        (on_error or die)(f"cannot write output: {e}")


class BadJSON(ValueError):
    """Text that json.loads accepts or chokes on, and this package refuses."""


def _reject_constant(name):
    raise BadJSON(f"{name} is not JSON: a non-finite number has no strict-JSON "
                  "spelling, so it cannot be compared, and one reaching "
                  "verdict.json makes the review viewer's JSON.parse throw")


def loads_strict(text):
    """json.loads, minus the three inputs it handles in ways a caller that
    catches only JSONDecodeError never sees (2026-09 audit):

    - `NaN` / `Infinity` / `-Infinity` PARSE, by Python's default, and are then
      re-emitted into artifacts a strict parser rejects. One `NaN` in one
      expectation blanked the whole review viewer.
    - an integer literal past the interpreter's digit limit raises a bare
      ValueError, and
    - nesting past the recursion limit raises RecursionError --
    both of which left as a traceback, not as the {"error": ...}/exit-2 this
    module's docstring promises. Same shape as load_text's UnicodeDecodeError.

    Raises BadJSON (a ValueError) for all of them and for ordinary syntax
    errors, so one except clause is the whole contract."""
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except RecursionError:
        raise BadJSON("nested too deeply to parse") from None
    except BadJSON:
        raise
    except ValueError as e:     # JSONDecodeError, and the int digit limit
        raise BadJSON(str(e)) from None


def load_json(path, on_error=None, strict=True):
    """`strict=False` lets NaN/Infinity through, for the one caller whose job is
    to DISPLAY whatever a run holds (build_review_viewer.py): an app's response
    can carry a NaN no input check of ours polices, and a viewer that refuses
    the whole run over it is worse than one that shows it."""
    fail = on_error or die
    try:
        text = load_text(path, on_error=fail)
        return loads_strict(text) if strict else json.loads(text)
    except (ValueError, RecursionError) as e:
        # The path, not just the exception: JSONDecodeError carries no filename,
        # so a directory scan reported "Expecting value: line 1 column 1"
        # without naming which of its files was bad.
        fail(f"bad input: {path}: {e}")


def load_object(path):
    doc = load_json(path)
    if not isinstance(doc, dict):
        die(f"{path}: expected a JSON object, got "
            f"{type(doc).__name__}")
    return doc


def load_trajectory(path):
    """Return the tool_calls list from a trajectory file.

    Accepts either normalize_trace.py's full output ({"trajectory": {...}})
    or a bare trajectory ({"tool_calls": [...]}).
    """
    doc = load_json(path)
    if isinstance(doc, dict) and isinstance(doc.get("trajectory"), dict):
        doc = doc["trajectory"]
    if not isinstance(doc, dict) or not isinstance(doc.get("tool_calls"),
                                                   list):
        die(f"{path}: expected an object with a 'tool_calls' list "
            "(normalize_trace.py output or a bare trajectory)")
    # The ELEMENTS, not just the container. Every consumer reads a call with
    # c.get(...), so a non-object entry — the shape a miswritten adapter
    # mapping_shim emits — raised AttributeError in all four of them: a
    # traceback and exit 1, which is neither the {"error": ...} payload this
    # module promises nor the exit 2 the run skill branches on. Validated once
    # here so the four consumers cannot drift on it.
    for i, call in enumerate(doc["tool_calls"]):
        if not isinstance(call, dict):
            die(f"{path}: tool_calls[{i}] must be an object, got "
                f"{type(call).__name__}")
        # And the NAME. Every forbidden-tool check asks "is this name among the
        # calls?", so a call with no usable name matches nothing and the check
        # PASSES over the very call it exists to catch, with unscorable: 0
        # (2026-09-21 audit: an execute_tool span whose name sat under another
        # attribute key normalized to name: null, and score_authz reported
        # pass over a delete_user call). A call the harness cannot name is not
        # evidence of absence, so it is refused here, once, for all consumers.
        name = call.get("name")
        if not isinstance(name, str) or not name.strip():
            die(f"{path}: tool_calls[{i}].name must be a non-empty string, got "
                f"{name!r} -- a call with no name matches no forbidden-tool or "
                "expected-tool entry, so scoring it would pass by omission. "
                "From a trace this means the execute_tool span lacks "
                "gen_ai.tool.name (normalize_trace.py reports it as "
                "checks.unnamed_tool_spans); fix the instrumentation or the "
                "adapter's mapping_shim")
    return doc["tool_calls"]


def load_jsonl(path, unique_key=None, required_keys=()):
    rows = []
    seen = set()
    for lineno, line in enumerate(load_text(path).splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = loads_strict(line)
        except BadJSON as e:
            die(f"{path}:{lineno}: {e}")
        if not isinstance(row, dict):
            die(f"{path}:{lineno}: expected a JSON object, got "
                f"{type(row).__name__}")
        for required in required_keys:
            if required not in row:
                die(f"{path}:{lineno}: missing required key {required!r}")
        if unique_key is not None:
            dedupe_key = row.get(unique_key)
            # A list/dict id would raise TypeError out of the set, which is the
            # one shape of bad input this loader let escape its own error
            # contract.
            if not isinstance(dedupe_key, Hashable):
                die(f"{path}:{lineno}: {unique_key!r} must be a scalar id, got "
                    f"{type(dedupe_key).__name__}")
            if dedupe_key in seen:
                die(f"{path}:{lineno}: duplicate {unique_key} {dedupe_key!r}")
            seen.add(dedupe_key)
        rows.append(row)
    return rows
