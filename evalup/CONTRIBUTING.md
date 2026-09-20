# Working on evalup

The checks, and the rules that look arbitrary until you break one. Written for
whoever changes this plugin next.

These came out of a remediation wave that closed 2026-09-10. Its audit
document was deleted once every finding was fixed and verified — this file is
the part that outlived it. The full bodies are in the git history
(`git log --oneline` around Steps 0-10); each finding had a copy-pasteable
repro, so re-run one rather than re-deriving it.

## The three checks

Run all three from this directory. There is no fourth, and CI is not one of
them — see the bottom of this file.

```sh
python3 -m unittest discover -s tests
```
706 tests, ~2 min, the blessed command. `test_run_cases.py` is most of that: a
real HTTP server per test, and one test kills a runner mid-run.

```sh
ruff check --config ruff.toml .
```
`--config` is not decoration. `pyproject.toml` is a sibling, and when both
exist **`ruff.toml` wins and a `[tool.ruff]` table in `pyproject.toml` is
ignored silently.**

```sh
uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q
```
The only conclusive 3.9 floor check — the blessed `unittest` command runs on
whatever `python3` happens to be. Expect 705 passed, 1 skipped, 78 subtests.
**Run it before any release.**

There are **17** CLIs in `scripts/` (everything not underscore-prefixed) and
each must answer `--help` with rc=0 *and* non-empty output. `scripts/_common.py`
is a shared module, not a CLI: it has no argparse, so `python _common.py --help`
exits 0 having printed nothing, which is why the check tests for output and not
just the exit code.

## Rules that look arbitrary and are not

- **`validate_cases.py` and `run_cases.py` take JSON, not YAML**, deliberately —
  the skill's YAML→JSON conversion *is* the run's parse, not a second opinion.
  Read the docstrings before "fixing" that.
- **`docs/runner-contract.md` is the runner's spec and it WINS** over any skill
  prose. Its nine decisions were confirmed 2026-09-08 with their trade-offs
  recorded; don't reopen one without reading it. **Three tests read that file at
  run time** — they exec §9(b)'s python block, parse §6's `# only when:`
  markers, and pin `--oos-route`'s precondition — so editing it fails the suite
  until the code follows. Deliberate, not brittle.
- **The holdout ledger is a `.jsonl` sidecar** (`paths.holdout_ledger`), never
  the YAML metadata a stdlib-only runner would corrupt, and its total is a LINE
  COUNT.
- **`__oos__` is `score_routing.py`'s internal label**, never a data value;
  `--oos-route` maps a profile's route onto it.
- **The scorers' error contract puts errors on STDOUT** as JSON with exit 2
  (`_common.die()`), not stderr.

## Four decisions the remediation wave left behind

Each was argued out at length, with trade-offs, in the commit that made it.
Do not reopen one without reading that commit.

1. **The judged gate is DERIVED.** `judge.status: calibrated` is necessary, not
   sufficient — the `paths.judge_calibration` sidecar must back it, and every
   failure is a reason on `unjudged`, never an exit code.
2. **`http` no longer carries a case to `pass`.** Rollup rule 5 is non-`http`,
   so an all-`unscored` case rolls up `unscored`, never `fail`.
3. **A series is not a pair.** `run_history.py` keys on `mode` +
   `selecting_split` + the enabled-layer set, and **not** on the app `git_sha`;
   only `status: "ok"` runs join, and it never says "better".
4. **Cost NEVER gates, and its price table is a SIDECAR.** `cost_latency` is a
   capability, not a layer (contract **§5.7**, permanent); `--prices` is per app
   and **not in `plan.json`**; an unknown model withholds the dollars and keeps
   the tokens; a trace-less run cannot be priced at all. Related: **bootstrap
   resampling is DECLINED by name** — never "add a CI" that way; three exact
   tests replaced it.

## Five fields are RESERVED

`expect.state`, `seed_state`, `available_tools`, `excluded_tools` (plus
`difficulty`, doc-only). The validator WARNs on each. `run_cases.py` calls
`environment.seed` / `.reset` / `.snapshot_state` **nowhere** — seed out of
band. **Do not wire one up without a scorer that reads it.**

## The worked example

`examples/quickstart/`, driven by `tests/test_example.py`. Its inputs are
committed and its run is NOT: edit any YAML there and you must regenerate
`converted.json` (the one-liner is in its README) or a test fails.
`validate_cases.py --strict` over it must stay at **0 errors and 0 warnings**.

## CI has never run

`.github/workflows/ci.yml` (repo root) has no remote to run on, so it is a
*specification* — its own header says so. The three commands above are the only
checked claim. **Never report CI as green.**
