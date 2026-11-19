#!/usr/bin/env python3
"""Run the unittest suite as CI's four shards, side by side.

A faster local loop, NOT one of the three checks: the blessed command stays
`python3 -m unittest discover -s tests` (CONTRIBUTING.md), and that serial run
is the conclusive one. This mirrors CI's `tests` job: the shard globs are read
from `.github/workflows/ci.yml`'s SHARD_* env, so the two cannot drift, and
like CI it refuses to run when a test file is in no shard or in two.

Not pytest-xdist: its workers pickle subTest parameters, and one test passes a
pathlib.Path (ci.yml says so above the floor-3-9 job).

Usage, from evalup/:  python3 tests/run_shards.py
Exit 0 every shard passed; 1 a shard failed (its full output is printed);
2 the shards could not be read or do not cover the test files.
"""
import concurrent.futures
import fnmatch
import pathlib
import re
import subprocess
import sys

TESTS = pathlib.Path(__file__).resolve().parent
CI = TESTS.parent.parent / ".github" / "workflows" / "ci.yml"


def shards():
    try:
        text = CI.read_text(encoding="utf-8")
    except OSError as exc:
        sys.exit(f"cannot read {CI}: {exc}")
    found = dict(re.findall(r'^\s*(SHARD_\d+):\s*"([^"]+)"', text, re.M))
    if not found:
        sys.exit(f"no SHARD_* env in {CI}")
    return {name: globs.split() for name, globs in sorted(found.items())}


def check_cover(groups):
    bad = []
    for path in sorted(TESTS.glob("test*.py")):
        owners = [name for name, globs in groups.items()
                  if any(fnmatch.fnmatchcase(path.name, g) for g in globs)]
        if len(owners) != 1:
            bad.append(f"{path.name}: {', '.join(owners) or 'no shard'}")
    if bad:
        print("fix SHARD_* in ci.yml:\n  " + "\n  ".join(bad))
        sys.exit(2)


def run_shard(globs):
    out, rc = [], 0
    for pattern in globs:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(TESTS),
             "-p", pattern],
            cwd=TESTS.parent, capture_output=True, text=True)
        out.append(proc.stdout + proc.stderr)
        rc = rc or proc.returncode
    return rc, "".join(out)


def main():
    groups = shards()
    check_cover(groups)
    with concurrent.futures.ThreadPoolExecutor(len(groups)) as pool:
        futures = {name: pool.submit(run_shard, globs)
                   for name, globs in groups.items()}
        results = {name: f.result() for name, f in futures.items()}
    failed = False
    for name, (rc, out) in results.items():
        tail = [line for line in out.splitlines()
                if line.startswith(("Ran ", "OK", "FAILED"))]
        print(f"{name} {' '.join(groups[name])}: "
              f"{'ok' if rc == 0 else 'FAILED'} | {' | '.join(tail)}")
        if rc:
            failed = True
            print(out)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
