"""A stand-in app for invocation.mode: exec (#39), driven by test_exec.py.

Stdlib only. Reads ONE JSON request on stdin, writes ONE JSON object on
stdout -- the contract an app's own shim keeps -- unless --misbehave asks
for one of the failures the runner must map:

  exit        stderr "boom", exit 3
  badjson     prints text that is not JSON
  notobject   prints a JSON list
  empty       prints nothing
  sleep       starts a grandchild (pid to --pidfile), then sleeps 60 s
  stderr      writes 1 MiB to stderr, then answers normally
  stderr-exit writes 1 MiB to stderr, then exits 1
  echo-secret writes $SHIM_SECRET to stderr, then exits 2
  leak        answers normally, with $SHIM_SECRET in the text

--detach both|stderr first starts a grandchild in its OWN session
(setsid: out of the call's process group) that holds stdout and stderr,
or stderr only, open for 30 s; its pid goes to --pidfile. --hang then
sleeps 60 s instead of answering.

--tools adds a tool_calls list to the answer; --log appends each request
it read (with the time it read it, as `_t`) to a JSONL file. The answer
echoes the message, the session fields it was sent, its cwd and the names
of the env vars it can see.
"""
import argparse
import json
import os
import subprocess
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--misbehave", default="none")
    ap.add_argument("--tools", action="store_true")
    ap.add_argument("--log")
    ap.add_argument("--pidfile")
    ap.add_argument("--detach", choices=("both", "stderr"))
    ap.add_argument("--hang", action="store_true")
    a = ap.parse_args()

    request = json.loads(sys.stdin.read())
    if a.log:
        with open(a.log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(request, _t=time.time())) + "\n")

    if a.detach:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL, start_new_session=True,
            stdout=None if a.detach == "both" else subprocess.DEVNULL)
        if a.pidfile:
            with open(a.pidfile, "w", encoding="utf-8") as fh:
                fh.write(str(child.pid))
        if a.hang:
            time.sleep(60)
            return 0

    if a.misbehave == "exit":
        sys.stderr.write("Traceback: boom\n")
        return 3
    if a.misbehave == "badjson":
        print("Loading model... done")
        return 0
    if a.misbehave == "notobject":
        print(json.dumps([1, 2]))
        return 0
    if a.misbehave == "empty":
        return 0
    if a.misbehave == "sleep":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        if a.pidfile:
            with open(a.pidfile, "w", encoding="utf-8") as fh:
                fh.write(str(child.pid))
        time.sleep(60)
        return 0
    if a.misbehave in ("stderr", "stderr-exit"):
        sys.stderr.write("x" * (1024 * 1024) + "\nthe end of stderr\n")
        if a.misbehave == "stderr-exit":
            return 1
    if a.misbehave == "echo-secret":
        sys.stderr.write("auth failed for " + os.environ.get("SHIM_SECRET", "")
                         + "\n")
        return 2

    message = request.get("message", "")
    text = "ok: " + str(message)
    if a.misbehave == "leak":
        text += " (token " + os.environ.get("SHIM_SECRET", "") + ")"
    answer = {"text": text,
              "session_id": request.get("session_id"),
              "conversationId": request.get("conversation") or "conv-1",
              "cwd": os.getcwd(),
              "env": sorted(os.environ)}
    if a.tools:
        answer["tool_calls"] = [
            {"id": "c1", "name": "list_invoices",
             "arguments": {"unit_id": "U-1"}, "result": {"n": 7}}]
    sys.stdout.write(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
