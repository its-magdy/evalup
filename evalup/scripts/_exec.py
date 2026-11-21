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
  cwd. When it exits, or the timeout passes, the whole group is killed. A
  process that left the group (setsid, a daemon) is out of reach and may
  outlive the call, but cannot hang it: run() is bounded by the timeout
  plus DRAIN_GRACE_S whatever holds the pipes. Windows has no group kill
  here: only the command itself is killed.
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

from _common import secret_forms

EXEC_KEYS = ("argv", "cwd", "env")
# What a process needs to start at all, passed through when set. Nothing
# here is a credential; a variable the command needs beyond these goes in
# exec.env by name.
BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
            "LC_CTYPE", "TZ", "TMPDIR", "TMP", "TEMP",
            # Windows: what a process there expects to find set.
            "SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA",
            "LOCALAPPDATA", "SYSTEMDRIVE", "WINDIR", "PROGRAMDATA")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ENV_REF_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
STDOUT_CAP_BYTES = 16 * 1024 * 1024
STDERR_KEEP_BYTES = 64 * 1024
# How much of stderr (its end) and of a bad stdout (its start) an error
# message quotes. Messages land in response.json and run output.
QUOTE_CHARS = 2000
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
    """exec.cwd against `base` (the app root, else the state location),
    always ABSOLUTE: a relative state_dir would otherwise resolve against
    whatever directory the runner happens to be started from."""
    if cwd is None:
        return os.path.abspath(base)
    if os.path.isabs(cwd):
        return os.path.abspath(cwd)
    return os.path.abspath(os.path.join(base, cwd))


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
    """Put ${NAME} back for every (value, NAME) in `secrets`, in each form
    the value takes in quoted text (_common.secret_forms: plain, JSON-
    escaped, repr), longest first. The caller chooses which values count;
    every one given is scrubbed, whatever its length."""
    pairs = {}
    for value, name in secrets:
        if value:
            for form in secret_forms(value):
                pairs.setdefault(form, name)
    for form, name in sorted(pairs.items(), key=lambda p: len(p[0]),
                             reverse=True):
        text = text.replace(form, "${" + name + "}")
    return text


class _Drain(threading.Thread):
    """Read one pipe to EOF, from a file descriptor this thread OWNS.

    os.read on a raw fd, never a buffered file object: closing a
    BufferedReader blocks on the lock a reader blocked in read() holds, so
    a pipe a detached grandchild kept open used to hang run() until that
    grandchild exited. The thread closes its fd at EOF and nothing else
    ever does, so run() can give up on a pipe without touching it.
    `keep` bytes from the start, or (tail) the last `keep`; `total` counts
    everything, so the child never blocks on a full pipe."""

    def __init__(self, fd, keep, tail):
        super().__init__(daemon=True)
        self.fd, self.keep, self.tail = fd, keep, tail
        self.data = bytearray()
        self.total = 0
        self.lock = threading.Lock()

    def run(self):
        try:
            while True:
                chunk = os.read(self.fd, _CHUNK)
                if not chunk:
                    break
                with self.lock:
                    self.total += len(chunk)
                    if self.tail:
                        self.data += chunk
                        if len(self.data) > self.keep:
                            del self.data[:len(self.data) - self.keep]
                    elif len(self.data) < self.keep:
                        self.data += chunk[:self.keep - len(self.data)]
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                os.close(self.fd)

    def snapshot(self):
        with self.lock:
            return bytes(self.data), self.total


class _Feed(threading.Thread):
    """Write stdin and close it, from an fd this thread owns. A command
    that never reads its stdin is not an error here; its exit and its
    stdout decide the call. Never joined: a write a detached grandchild
    blocks stays in this daemon thread, not in run()."""

    def __init__(self, fd, data):
        super().__init__(daemon=True)
        self.fd, self.data = fd, data

    def run(self):
        try:
            view = memoryview(self.data)
            while view:
                view = view[os.write(self.fd, view):]
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                os.close(self.fd)


def _own_fd(pipe):
    """A duplicate of a Popen pipe's fd for a thread to own, and the file
    object closed at once -- before any thread reads, so no lock is held."""
    fd = os.dup(pipe.fileno())
    pipe.close()
    return fd


def _wait_unreaped(proc, deadline):
    """Wait for the child to exit WITHOUT reaping it (waitid + WNOWAIT),
    until `deadline`. True when it exited. Unreaped, its pid -- and so its
    process group id -- cannot be reused, so the killpg after this cannot
    hit an unrelated group. Where waitid is missing, Popen.wait (reaps)."""
    waitid = getattr(os, "waitid", None)
    if waitid is None or not hasattr(os, "WNOWAIT"):
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            return True
        except subprocess.TimeoutExpired:
            return False
    pause = 0.002
    while True:
        try:
            info = waitid(os.P_PID, proc.pid,
                          os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return True
        if info is not None:
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(pause, left))
        pause = min(pause * 2, 0.05)


def _kill_group(proc):
    """SIGKILL the call's whole process group. Windows has no groups here:
    only the process itself is killed, and what it started may live on."""
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(proc.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    with contextlib.suppress(OSError):
        proc.kill()


def _longest_form(secrets):
    return max((len(form) for value, _ in secrets if value
                for form in secret_forms(value)), default=0)


def _stderr_tail(drain, secrets):
    data, total = drain.snapshot()
    text = data.decode("utf-8", errors="replace")
    if total > len(data):
        # The kept window starts wherever the last STDERR_KEEP_BYTES began,
        # possibly inside a secret, whose tail no scrub can recognise. Drop
        # as many characters as the longest secret form, so whatever the
        # window cut through is gone before anything is quoted.
        text = text[_longest_form(secrets):]
    text = text.strip()
    if not text:
        return ""
    # Scrubbed before the quote is cut, so the cut never splits a value.
    text = scrub(text, secrets)
    if len(text) > QUOTE_CHARS:
        text = "..." + text[-QUOTE_CHARS:]
    return "; stderr: " + text


class _Terminated(BaseException):
    """SIGTERM arrived during a call: unwinds run() like a Ctrl-C does."""


@contextlib.contextmanager
def _sigterm_cleans_up():
    """While a call runs, a SIGTERM to the runner unwinds through run()'s
    `finally` -- which kills the call's process group -- and is then
    re-delivered under its default action, so the runner still dies by
    SIGTERM, as it would have. Without this the command, in its own
    session, was orphaned. Only from the main thread, and only when
    SIGTERM is at its default (an ignored or handled SIGTERM is someone
    else's decision). SIGKILL cannot be caught: it orphans the command."""
    sigterm = getattr(signal, "SIGTERM", None)
    if sigterm is None or threading.current_thread() \
            is not threading.main_thread() \
            or signal.getsignal(sigterm) is not signal.SIG_DFL:
        yield
        return

    def handler(signum, frame):
        raise _Terminated()

    signal.signal(sigterm, handler)
    try:
        yield
    except _Terminated:
        signal.signal(sigterm, signal.SIG_DFL)
        os.kill(os.getpid(), sigterm)
        raise KeyboardInterrupt from None
    finally:
        signal.signal(sigterm, signal.SIG_DFL)


def run(argv, executable, cwd, env, body, timeout, secrets=()):
    """One call; see _run. A SIGTERM during it kills the call's group
    first (_sigterm_cleans_up)."""
    with _sigterm_cleans_up():
        return _run(argv, executable, cwd, env, body, timeout, secrets)


def _run(argv, executable, cwd, env, body, timeout, secrets=()):
    """One call. Returns run_cases' result shape -- {status, body,
    latency_s, headers, set_cookies} -- with status 200, or raises
    ExecFailure. `secrets` are (value, NAME) pairs scrubbed out of every
    quoted byte of the command's output.

    Bounded in every case by `timeout` + DRAIN_GRACE_S (plus the kill):
    the command is waited for until the timeout, then its group is
    killed, and its pipes get the grace to reach EOF. A pipe still open
    then (a detached grandchild holds it) is abandoned to its thread."""
    payload = (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(argv), executable=executable, cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        why = scrub(str(exc), secrets)
        raise ExecFailure("transport", f"could not start {argv[0]}: "
                          f"{type(exc).__name__}: {why}") from exc
    out = _Drain(_own_fd(proc.stdout), STDOUT_CAP_BYTES + 1, tail=False)
    err = _Drain(_own_fd(proc.stderr), STDERR_KEEP_BYTES, tail=True)
    feed = _Feed(_own_fd(proc.stdin), payload)
    for thread in (out, err, feed):
        thread.start()
    exited = False
    try:
        exited = _wait_unreaped(proc, started + timeout)
    finally:
        # Measured when the command exited (or timed out), not after the
        # kill and the drain grace.
        latency = round(time.monotonic() - started, 3)
        # The group goes whether the command finished or not: one process
        # per call, and nothing it started IN ITS GROUP outlives it. A
        # process that left the group (setsid) is out of reach; it cannot
        # hang the call (below), but it can outlive it.
        _kill_group(proc)
        try:
            proc.wait(timeout=DRAIN_GRACE_S)
            reaped = True
        except subprocess.TimeoutExpired:
            # SIGKILLed and still not gone (an uninterruptible sleep, a
            # stuck filesystem): abandoned, never waited on forever.
            reaped = False
    if not reaped:
        raise ExecFailure("timeout" if not exited else "transport",
                          f"the command did not exit within {DRAIN_GRACE_S}s "
                          "of SIGKILL; abandoned (pid {})".format(proc.pid))
    grace_end = time.monotonic() + DRAIN_GRACE_S
    for thread in (out, err):
        thread.join(max(0.0, grace_end - time.monotonic()))
    if not exited:
        tail = _stderr_tail(err, secrets)
        raise ExecFailure("timeout", f"timed out after {timeout}s; killed "
                          f"its process group{tail}")
    code = proc.returncode
    if code != 0:
        how = f"killed by signal {-code}" if code < 0 else f"exited {code}"
        tail = _stderr_tail(err, secrets)
        raise ExecFailure("connection", f"the command {how}{tail}")
    if out.is_alive():
        # A process that left the group still holds stdout open: what was
        # read so far may be half an object, and is never taken for one.
        raise ExecFailure("transport", "stdout was still open "
                          f"{DRAIN_GRACE_S}s after the command exited (a "
                          "process it started outside its group holds it)")
    data, total = out.snapshot()
    if total > STDOUT_CAP_BYTES:
        raise ExecFailure("transport", f"stdout exceeded {STDOUT_CAP_BYTES} "
                          f"bytes ({total} written)")
    text = data.decode("utf-8", errors="replace")
    if not text.strip():
        raise ExecFailure("transport", "the command printed nothing on "
                          "stdout; it must print one JSON object"
                          + _stderr_tail(err, secrets))
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        # Scrubbed whole, THEN cut: a cut first would leave half a secret
        # that straddles character 200 unrecognisable to the scrub.
        start = scrub(text.strip(), secrets)[:200]
        raise ExecFailure("transport", "stdout is not one JSON document "
                          f"({exc.__class__.__name__}: {exc}); it starts: "
                          f"{start!r}. Logs go to stderr") from exc
    if not isinstance(parsed, dict):
        raise ExecFailure("transport", "stdout is JSON but not an object "
                          f"(got {type(parsed).__name__}); print "
                          "{\"text\": ...}")
    return {"status": 200, "body": parsed, "latency_s": latency,
            "headers": {}, "set_cookies": []}
