"""Shared input loading for the agent-eval scorers.

One error contract for every scorer: on bad input print {"error": "<message>"}
to stdout and exit 2. Loaders are strict about presence (a missing required
key is an error, never an empty default — defaulting would score an unread
trace as an empty one) and lenient about wrapping (normalize_trace.py's full
output and a bare trajectory both work).
"""
import json
import sys

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
                    version=f"agent-eval harness {HARNESS_VERSION}")
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


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        die(f"bad input: {e}")


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
    return doc["tool_calls"]


def load_jsonl(path, unique_key=None, required_keys=()):
    rows = []
    seen = set()
    try:
        with open(path) as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as e:
                    die(f"{path}:{lineno}: {e}")
                if not isinstance(row, dict):
                    die(f"{path}:{lineno}: expected a JSON object, got "
                        f"{type(row).__name__}")
                for required in required_keys:
                    if required not in row:
                        die(f"{path}:{lineno}: missing required key "
                            f"{required!r}")
                if unique_key is not None:
                    dedupe_key = row.get(unique_key)
                    if dedupe_key in seen:
                        die(f"{path}:{lineno}: duplicate {unique_key} "
                            f"{dedupe_key!r}")
                    seen.add(dedupe_key)
                rows.append(row)
    except OSError as e:
        die(f"bad input: {e}")
    return rows
