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
834 tests, ~2.5 min, the blessed command. `test_run_cases.py` is most of that: a
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
whatever `python3` happens to be. Expect 825 passed, 9 skipped, 273 subtests.
All nine skips need PyYAML (eight are `convert_suite.py`'s, one is the
example's fidelity check); add `--with pyyaml` and they run (834 passed, none
skipped) — do that before a release too, since it is the only floor check that
script gets. Eleven review-viewer tests also skip when `node` is absent: they
boot the page's JS, so check the skip count on a machine without it.
**Run it before any release.**

There are **21** CLIs in `scripts/` (everything not underscore-prefixed) and
each must answer `--help` with rc=0 *and* non-empty output. `scripts/_common.py`
is a shared module, not a CLI: it has no argparse, so `python _common.py --help`
exits 0 having printed nothing, which is why the check tests for output and not
just the exit code.

## The toolchain, and why it looks like this

The floor is checked at three depths, weakest to strongest:

```sh
python3 -m unittest discover -s tests   # grammar, on whatever interpreter you have
ruff check --config ruff.toml .         # target-version = py39 bounds every UP fix
uv run --python 3.9 --with pytest --with pytest-subtests \
    python -m pytest tests -q           # the real thing: stdlib APIs + runtime typing
```

Only the third is conclusive. `PY_FLOOR` in `tests/test_scorers.py` compiles the
scripts against the floor's *grammar*, and ruff's `target-version` stops `UP`
from proposing a 3.10+ form — but neither rejects a newer stdlib API or a typing
construct that only fails at runtime. `uv` fetches a real 3.9 in seconds, so
there is no reason to skip it before a release.

`ruff` and `uv` are development tools only — nothing the harness ships at
runtime imports outside the stdlib. `ruff.toml` keeps a deliberately small rule
set, and each entry carries a comment saying which defect class it catches or
which comment in the code already argues for it; read it there before adding to
it. `pyproject.toml` declares those dev tools and nothing else: it has no
`[project]` table, because this plugin is installed by copy from
`.claude-plugin/plugin.json`, never pip-installed, and the absent
`[project.dependencies]` list is the one slot a runtime dependency could
arrive through. Ruff's config deliberately stays in `ruff.toml` — note that
when both files sit side by side, `ruff.toml` wins and a `[tool.ruff]` table
in `pyproject.toml` is ignored silently.

`.tool-versions` pins **3.11.11**, above the 3.9 floor, on purpose: you edit on
a supported interpreter and check the floor with `uv`, which fetches a real 3.9
without making you install an end-of-life one. The file says so in a comment.

`.github/workflows/ci.yml` runs all three depths plus a `--help` check on a
real 3.9/3.11/3.13 matrix — **but it has never executed.** This repository has
no git remote, so nothing has ever evaluated that file; it is a specification
parked for the day one is added, and its header says so. Until then the
commands above are the only checked claim.

## Rules that look arbitrary and are not

- **`validate_cases.py` and `run_cases.py` take JSON, not YAML**, deliberately —
  the YAML→JSON conversion *is* the run's parse, not a second opinion. Read the
  docstrings before "fixing" that. And `convert_suite.py` does the converting,
  never the model — a suite re-typed by an LLM is the silent misparse those
  docstrings refuse, relocated.
- **`plan.json` is built by `make_plan.py`, never by the model.** Same rule,
  one step later: a plan assembled by hand cost the first live session half
  its tool calls. It refuses `--mode full` because one plan carries one
  `selecting_split`.
- **`gate.py` fails closed.** Zero scored cases, a count that is not an int, or
  a run directory that fails `--verify` closes the gate. `results.json` is the
  file under audit; never let the gate take its word for its own integrity.
  **Canaries are the one thing it does not close on by default**, and that is
  decided: a canary `fail` already aborts the run (and a `--resume` re-aborts),
  while one with no verdict is provider noise or a disabled layer — it prints
  NOT VERIFIED, and `--require-canaries` is the opt-in strict form.
- **A tool call the harness cannot name is never evidence of absence.**
  `_common.load_trajectory` refuses it, for every consumer at once.
- **Only the MODES `holdout`/`full` and the SPLIT `holdout` touch the seal**
  (`run_cases.touches_seal`). The split named `full` is the unsealed set.
- **Skills invoke scripts as `python3 <path>` and pre-approve them in
  `allowed-tools`, in both the quoted and unquoted spelling.**
  `tests/test_plugin_layout.py` pins it; a live session is what found that the
  unquoted rule alone matches nothing. The rule matches ONE literal command:
  a `cd … &&` prefix or a shell variable holding the path falls outside it, so
  every skill's preamble forbids both. The grant also lapses at the user's
  next message (documented behaviour, confirmed live).
- **A skill that loads another lists it as `Skill(evalup:<name>)`** in its
  `allowed-tools` — loading a skill that has its own `allowed-tools` needs
  approval, and headless a prompt is a silent denial. Pinned by the same test.
  `optimize` is never listed; only the user launches it.
- **`convert_suite.py` is the only script that may import outside the stdlib**
  (PyYAML, lazily, with an exit-2 message when absent). It is an authoring
  step; the runner and every scorer still import nothing. Do not let a second
  script follow it.
- **A resolved env value never reaches disk — headers *or* body.** Headers are
  redacted by name; `request_body`/`base_url`/`endpoint` refs are put back as
  `${NAME}` in the recorded template *before* placeholders render
  (`run_cases.unresolve`). A new place that records a request owes the same.
- **A case id is a directory name.** `_common.unsafe_case_id` is the one rule,
  enforced by both the linter and the runner; keep them on it.
- **JSON in is strict** (`_common.loads_strict`): `NaN`/`Infinity`, recursion
  blow-ups and the int-digit limit are all exit 2. Only the review viewer loads
  leniently, because its job is to show whatever a run holds.
- **`md_to_html.py` allowlists URL schemes.** Never go back to a denylist: the
  one it replaced was bypassed by a leading control byte.
- **`docs/runner-contract.md` is the runner's spec and it WINS** over any skill
  prose. Its nine decisions were confirmed 2026-09-08 with their trade-offs
  recorded; don't reopen one without reading it. **Four tests read that file at
  run time** — they exec §9(b)'s python block, parse §6's `# only when:`
  markers, pin `--oos-route`'s precondition, and assert §5.7 still names
  `score_cost.py` — so editing those passages fails the suite until the code
  follows. Prose elsewhere in the file is free to change. Deliberate, not
  brittle.
- **The holdout ledger is a `.jsonl` sidecar** (`paths.holdout_ledger`), never
  the YAML metadata a stdlib-only runner would corrupt, and its total is a LINE
  COUNT — so it holds one row per run id, and a `--resume` does not add one.
- **The matrix says `answer_quality`; the runner's rows are `answer`, `rules`
  and `judged`.** `_common.MATRIX_KEY_OF_LAYER` is the one translation, used
  by the runner's lookup and by `make_plan.py --layer`. `http` and `loops`
  map to `None`: no matrix key switches them off. A new layer row owes an
  entry there (a test diffs the map against `LAYER_ORDER`).
- **`actual.json` is written for every sent case carrying `expect.result`**,
  live `execution` layer or not — contract §9(b) requires it on exactly that
  condition, and a path that skips it ends the run with exit 6.
- **`__oos__` is `score_routing.py`'s internal label**, never a data value;
  `--oos-route` maps a profile's route onto it.
- **The scorers' error contract puts errors on STDOUT** as JSON with exit 2
  (`_common.die()`), not stderr.

## Path convention

Skills, references, and agents run with the CWD set to the **user's app**, not
to the plugin, so a bare `scripts/x.py` or `docs/x.md` inside them resolves to
the wrong place. One rule, applied throughout:

- Anything executed, or read across skills — a script, a `docs/` page, an
  `agents/*.md`, another skill's `SKILL.md` or `references/` — is written
  `${CLAUDE_PLUGIN_ROOT}/...`.
- A `SKILL.md` pointing into its own `references/` uses a relative markdown
  link, e.g. `[references/run-modes.md](references/run-modes.md)`.
- Paths that belong to the app or its state location (`datasets/`,
  `reports/`, the state dir's own `scripts/smoke.sh`) stay bare and relative
  — they are deliberately *not* plugin paths.

The paths in `README.md` and in the scripts' own docstrings are
repo-relative, for a human reading the source at the plugin root.

## Four decisions the remediation wave left behind

Each was argued out at length, with trade-offs, in the commit that made it.
Do not reopen one without reading that commit.

1. **The judged gate is DERIVED.** `judge.status: calibrated` is necessary, not
   sufficient — the `paths.judge_calibration` sidecar must back it, and every
   failure is a reason on `unjudged`, never an exit code.
2. **`http` alone never carries a case to `pass`.** Rollup rule 5 is non-`http`,
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
