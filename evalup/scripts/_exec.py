"""`invocation.mode: exec` -- one process per app call (#39, option 2).

Shared module, not a CLI (no argparse, like _common.py and _sse.py).
run_cases.py checks the adapter's `invocation.exec` block with check_config()
at pre-flight and sends every call through run(): the command declared as an
argv LIST is started with the rendered request body as one JSON document on
its stdin, and the JSON object it prints on stdout is the response body. From
there the body is read exactly as an HTTP JSON body is -- the `<answer>` path,
`<trace id>`, `traces.source: response`, session_from -- so nothing
downstream knows which transport carried it.

The rules, each a decision rather than a default:

- argv is a list, never a shell string: no `shell=True`, so no quoting, no
  globbing and no variable expansion. A `${REF}` in argv or cwd is refused,
  because a value in argv is visible to every user of the machine (`ps`) and
  is recorded in the manifest. A secret reaches the command by NAME, through
  `exec.env`, or inside request_body (stdin, recorded with its ${REF} put
  back, as for http).
- The child's environment is an ALLOWLIST: a few base variables a process
  needs to start (BASE_ENV, those set), plus the names `exec.env` lists. The
  runner's own environment -- every credential the user exported for some
  other tool -- is not inherited.
- One process per call, in its own process group (session), from a fixed
  cwd. When it exits, or the timeout passes, the whole group is killed, so a
  call leaves nothing running behind it.
- stdout and stderr are drained in full and capped in memory: stdout over
  STDOUT_CAP_BYTES is a failed call (an answer cut short is no answer), and
  stderr keeps its last STDERR_KEEP_BYTES -- a traceback ends there. App
  output is untrusted data; only a scrubbed tail of stderr is ever quoted.

Failures map onto the runner's transport kinds (run_cases.TransportError):
a timeout is `timeout`; a non-zero exit or a signal is `connection` -- the
app fell over, as a function entrypoint raising is; a command that cannot be
started, or stdout that is empty, too large, not JSON or not one JSON object,
is `transport`. Each is infra_error for the case, never a fail.
"""
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time

EXEC_KEYS = ("argv", "cwd", "env")
# What a process needs to start at all, passed through when set. Nothing
# here is a credential; a variable the command needs beyond these goes in
# exec.env by name.
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
            "LC_CTYPE", "TZ", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT",
            "COMSPEC", "PATHEXT")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ENV_REF_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
STDOUT_CAP_BYTES = 16 * 1024 * 1024
STDERR_KEEP_BYTES = 64 * 1024
# How much of stderr (its end) and of a bad stdout (its start) an error
# message quotes. Messages land in response.json and run output.
QUOTE_CHARS = 2000
# A value shorter than this is not scrubbed out of quoted output: an
# `EVAL_TENANT=42` would otherwise rewrite every "42" (as the probe does).
SCRUB_MIN_LEN = 4
# After the process exits, how long to wait for its pipes to close. A
# grandchild that left the process group can hold them open forever.
DRAIN_GRACE_S = 2.0
_CHUNK = 65536


class ExecFailure(Exception):
    """A call that produced no response body. `kind` is a transport kind."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def check_config(block):
    """Problems with an UNRESOLVED `invocation.exec` block; [] when sound.

    Checked before env resolution, so a `${REF}` in argv is refused as
    such rather than resolved into the command line first."""
    where = "adapter.invocation.exec"
    if not isinstance(block, dict):
        return [f"{where} must be an object with an `argv` list "
                "(invocation.mode: exec)"]
    problems = []
    unknown = sorted(set(block) - set(EXEC_KEYS))
    if unknown:
        problems.append(f"unknown key(s) in {where}: " + ", ".join(unknown))
    argv = block.get("argv")
    if isinstance(argv, str):
        problems.append(f"{where}.argv must be a list of strings, never a "
                        "shell string: no shell runs it (write "
                        "[\"node\", \"shim.js\"], not \"node shim.js\")")
    elif not (isinstance(argv, list) and argv
              and all(isinstance(a, str) for a in argv) and argv[0]):
        problems.append(f"{where}.argv must be a non-empty list of strings "
                        f"whose first is the command (got: {argv!r})")
    else:
        if any("\x00" in a for a in argv):
            problems.append(f"{where}.argv holds a NUL character")
        if any(ENV_REF_RE.search(a) for a in argv):
            problems.append(f"{where}.argv holds a ${{REF}}: a value in argv "
                            "is visible to every process on the machine and "
                            "recorded in the manifest. Pass it by name in "
                            f"{where}.env, or in request_body")
    cwd = block.get("cwd")
    if cwd is not None:
        if not (isinstance(cwd, str) and cwd.strip()):
            problems.append(f"{where}.cwd must be a non-empty path")
        elif ENV_REF_RE.search(cwd):
            problems.append(f"{where}.cwd holds a ${{REF}}; write the path")
    names = block.get("env", [])
    if not (isinstance(names, list) and all(isinstance(n, str)
                                            for n in names)):
        problems.append(f"{where}.env must be a list of env var NAMES "
                        f"(got: {names!r})")
    else:
        bad = [n for n in names if not ENV_NAME_RE.fullmatch(n)]
        if bad:
            problems.append(f"{where}.env holds {', '.join(map(repr, bad))}: "
                            "each entry is a variable NAME, passed through "
                            "from the runner's environment, never a value")
    return problems


def env_names(block):
    """The names `exec.env` passes through, or [] on any malformed block."""
    names = block.get("env") if isinstance(block, dict) else None
    if not isinstance(names, list):
        return []
    return [n for n in names if isinstance(n, str) and ENV_NAME_RE.fullmatch(n)]


def child_env(names, environ=None):
    """The child's whole environment: BASE_ENV (those set) + `names`."""
    environ = os.environ if environ is None else environ
    return {name: environ[name] for name in (*BASE_ENV, *names)
            if environ.get(name)}


def resolve_cwd(cwd, base):
    """exec.cwd against `base` (the app root, else the state location)."""
    if cwd is None:
        return os.path.normpath(base)
    if os.path.isabs(cwd):
        return os.path.normpath(cwd)
    return os.path.normpath(os.path.join(base, cwd))


def resolve_command(command, cwd, env):
    """argv[0] as the absolute executable to start, or None.

    A name with a path separator is a file, relative to cwd; a bare name
    is looked up on the CHILD's PATH, so the run starts what pre-flight
    found whatever the runner's own PATH says."""
    if os.sep in command or (os.altsep and os.altsep in command):
        path = command if os.path.isabs(command) \
            else os.path.join(cwd, command)
        path = os.path.normpath(path)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) \
            else None
    found = shutil.which(command, path=env.get("PATH", os.defpath))
    return os.path.abspath(found) if found else None


def scrub(text, secrets):
    """Put ${NAME} back for every (value, NAME) in `secrets`, longest first."""
    for value, name in sorted(secrets, key=lambda p: len(p[0]), reverse=True):
        if len(value) >= SCRUB_MIN_LEN:
            text = text.replace(value, "${" + name + "}")
    return text


class _Drain(threading.Thread):
    """Read a pipe to EOF. `keep_head` bytes from its start, or (tail) the
    last `keep` bytes; `total` counts everything, kept or not, so the
    child never blocks on a full pipe."""

    def __init__(self, pipe, keep, tail):
        super().__init__(daemon=True)
        self.pipe, self.keep, self.tail = pipe, keep, tail
        self.data = bytearray()
        self.total = 0

    def run(self):
        try:
            while True:
                chunk = self.pipe.read(_CHUNK)
                if not chunk:
                    break
                self.total += len(chunk)
                if self.tail:
                    self.data += chunk
                    if len(self.data) > self.keep:
                        del self.data[:len(self.data) - self.keep]
                elif len(self.data) < self.keep:
                    self.data += chunk[:self.keep - len(self.data)]
        except (OSError, ValueError):
            pass


class _Feed(threading.Thread):
    """Write stdin and close it. A command that never reads its stdin is
    not an error here; its exit and its stdout decide the call."""

    def __init__(self, pipe, data):
        super().__init__(daemon=True)
        self.pipe, self.data = pipe, data

    def run(self):
        try:
            self.pipe.write(self.data)
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                self.pipe.close()


def _kill_group(proc):
    """SIGKILL the call's whole process group; just the process where
    there are no groups (Windows)."""
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(proc.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    with contextlib.suppress(OSError):
        proc.kill()


def _stderr_tail(drain, secrets):
    text = bytes(drain.data).decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    if len(text) > QUOTE_CHARS:
        text = "..." + text[-QUOTE_CHARS:]
    return "; stderr: " + scrub(text, secrets)


def run(argv, executable, cwd, env, body, timeout, secrets=()):
    """One call. Returns run_cases' result shape -- {status, body,
    latency_s, headers, set_cookies} -- with status 200, or raises
    ExecFailure. `secrets` are (value, NAME) pairs scrubbed out of every
    quoted byte of the command's output."""
    payload = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
    started = time.time()
    try:
        proc = subprocess.Popen(
            list(argv), executable=executable, cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        why = scrub(str(exc), secrets)
        raise ExecFailure("transport", f"could not start {argv[0]}: "
                          f"{type(exc).__name__}: {why}") from exc
    out = _Drain(proc.stdout, STDOUT_CAP_BYTES + 1, tail=False)
    err = _Drain(proc.stderr, STDERR_KEEP_BYTES, tail=True)
    feed = _Feed(proc.stdin, payload)
    for thread in (out, err, feed):
        thread.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        # The group goes whether the command finished or not: one process
        # per call, and nothing it started outlives it.
        _kill_group(proc)
        proc.wait()
    for thread in (out, err):
        thread.join(DRAIN_GRACE_S)
    for pipe in (proc.stdout, proc.stderr):
        with contextlib.suppress(OSError):
            pipe.close()
    latency = round(time.time() - started, 3)
    if timed_out:
        tail = _stderr_tail(err, secrets)
        raise ExecFailure("timeout", f"timed out after {timeout}s; killed "
                          f"its process group{tail}")
    code = proc.returncode
    if code != 0:
        how = f"killed by signal {-code}" if code < 0 else f"exited {code}"
        tail = _stderr_tail(err, secrets)
        raise ExecFailure("connection", f"the command {how}{tail}")
    if out.total > STDOUT_CAP_BYTES:
        raise ExecFailure("transport", f"stdout exceeded {STDOUT_CAP_BYTES} "
                          f"bytes ({out.total} written)")
    text = bytes(out.data).decode("utf-8", errors="replace")
    if not text.strip():
        raise ExecFailure("transport", "the command printed nothing on "
                          "stdout; it must print one JSON object"
                          + _stderr_tail(err, secrets))
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        start = scrub(text.strip()[:200], secrets)
        raise ExecFailure("transport", "stdout is not one JSON document "
                          f"({exc.__class__.__name__}: {exc}); it starts: "
                          f"{start!r}. Logs go to stderr") from exc
    if not isinstance(parsed, dict):
        raise ExecFailure("transport", "stdout is JSON but not an object "
                          f"(got {type(parsed).__name__}); print "
                          "{\"text\": ...}")
    return {"status": 200, "body": parsed, "latency_s": latency,
            "headers": {}, "set_cookies": []}
