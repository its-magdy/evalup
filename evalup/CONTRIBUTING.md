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
1308 tests, ~4.5 min (263 s measured 2026-11-22), the blessed command.
`test_run_cases.py` is most of that: a real HTTP server per test, and one test
kills a runner mid-run.

For a faster loop while you work, run CI's four shards side by side (~50 s):

```sh
python3 tests/run_shards.py
```
It reads the shard globs from `.github/workflows/ci.yml`'s `SHARD_*` env, so
it cannot drift from CI, and refuses to run when a test file is in no shard or
two. It is **not** a fourth check: before you call a change done, run the
serial command above, which is the conclusive one. pytest-xdist stays out
(`ci.yml` says why: it cannot pickle a subTest's `PosixPath`).

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
whatever `python3` happens to be. Expect 1288 passed, 20 skipped, 997 subtests.
All twenty skips need PyYAML (thirteen are `convert_suite.py`'s, rubric
files included, two are the
probe's `adapter.yaml` path, one is the example's fidelity check, four
read the CI workflow template); add
`--with pyyaml` and they run (1308 passed, none
skipped) — do that before a release too, since it is the only floor check that
script gets. Eleven review-viewer tests also skip when `node` is absent: they
boot the page's JS, so check the skip count on a machine without it.
**Run it before any release.**

There are **23** CLIs in `scripts/` (everything not underscore-prefixed) and
each must answer `--help` with rc=0 *and* non-empty output. `scripts/_common.py`,
`_rubric.py`, `_sse.py` and `_exec.py` are shared modules, not CLIs: they have
no argparse, so `python _common.py --help` exits 0 having printed nothing,
which is why the check tests for output and not just the exit code.

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
real 3.9/3.11/3.13 matrix on every push to github.com/its-magdy/evalup (first
run 2026-09-30). Run the commands above anyway: CI checks what was pushed, and
it is Linux-only.

## Rules that look arbitrary and are not

- **`validate_cases.py` and `run_cases.py` take JSON, not YAML**, deliberately —
  the YAML→JSON conversion *is* the run's parse, not a second opinion. Read the
  docstrings before "fixing" that. And `convert_suite.py` does the converting,
  never the model — a suite re-typed by an LLM is the silent misparse those
  docstrings refuse, relocated.
- **`plan.json` is built by `make_plan.py`, never by the model.** Same rule,
  one step later: a plan assembled by hand cost the first live session half
  its tool calls. One plan carries one `selecting_split`, so a
  release check is two plans (`regression`, then `holdout`); the removed
  `--mode full` is refused with that hint (`_common.FULL_MODE_REMOVED`), by
  `make_plan.py` and by the runner reading an old `plan.json`.
- **`gate.py` fails closed.** Zero scored cases, a count that is not an int, or
  a run directory that fails `--verify` closes the gate. `results.json` is the
  file under audit; never let the gate take its word for its own integrity.
  **Canaries are the one thing it does not close on by default**, and that is
  decided: a canary `fail` already aborts the run (and a `--resume` re-aborts),
  while one with no verdict is provider noise or a disabled layer — it prints
  NOT VERIFIED, and `--require-canaries` is the opt-in strict form.
- **A tool call the harness cannot name is never evidence of absence.**
  `_common.load_trajectory` refuses it, for every consumer at once.
- **Only the MODE `holdout` and the SPLIT `holdout` touch the seal**
  (`run_cases.touches_seal`). The split named `full` is the unsealed set.
- **Skills invoke scripts as `evalup <name>` and pre-approve
  `Bash(evalup *)` in `allowed-tools`.** `bin/evalup` is on the Bash tool's
  PATH while the plugin is enabled. It runs `scripts/<name>.py`, with `-`
  for `_`, and passes everything through. A headless spike (#41, 2026-11-22)
  showed the rule approves it, both as a user rule and from a skill. It
  replaced two `python3 <path>` rules (quoted and unquoted) that matched one
  literal command only. `tests/test_plugin_layout.py` pins the rule and
  refuses a `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/…` call in a skill. A
  `cd … &&` prefix still falls outside any rule, and so does a variable
  Claude Code cannot check, so the preamble forbids both. The grant lapses at
  the user's next message (documented behaviour, confirmed live). `bin/`
  is only on PATH inside Claude Code: a README or CI command outside it
  keeps `python3 scripts/<name>.py`. `CLAUDE_PLUGIN_ROOT` is not set for the
  Bash tool, so the dispatcher finds `scripts/` from its own path.
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
- **The first session's plugin text has a budget: 25,000 tokens**, counted
  as words × 1.33 by `tests/test_context_budget.py` over the four SKILL.md
  files on the start → discover → generate → run path and the references
  they read unconditionally. A new pointer from that path to a plugin file
  fails the test until the file is counted or given the condition under
  which it is read; prose that is not needed on a first session goes into
  a reference read on that condition, not into the skills.
- **`docs/runner-contract.md` is the runner's spec and it WINS** over any skill
  prose. It holds rules only (≤ 15,000 tokens, `tests/test_prose_budget.py`); the reasons (its nine 2026-09-08 decisions
  among them) are in its git history — `git log -p docs/runner-contract.md`,
  before the #42 trim — so read that before reopening one. **Four tests read that file at
  run time** — they exec §9(b)'s python block, parse §6's `# only when:`
  markers, pin `--oos-route`'s precondition, and assert §5.7 still names
  `score_cost.py` — so editing those passages fails the suite until the code
  follows. Prose elsewhere in the file is free to change. Deliberate, not
  brittle.
- **The holdout ledger is a `.jsonl` sidecar** (`paths.holdout_ledger`), never
  the YAML metadata a stdlib-only runner would corrupt, and its total is a LINE
  COUNT — so it holds one row per run id, and a `--resume` does not add one.
  A reseal archives its lines (`holdout-looks-v<old dataset_version>.jsonl`)
  and empties it; no code resets it.
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
- **`SCORING_VERSION` is the comparability key; `HARNESS_VERSION` is not.**
  `HARNESS_VERSION` follows `plugin.json` on every release (a test pins it)
  and is provenance only. Bump `_common.SCORING_VERSION` by one, in the same
  commit, whenever a change alters what a recorded verdict MEANS: a scorer's
  meaning, a default threshold a verdict or a keep depends on (`stats.py`'s
  `--bayes-threshold`, a mode's default `k`), or the runner's verdict rules
  (roll-up, the pass^k fold, what counts as infra). Never bump it for prose,
  skills, transport, new flags or a refactor that leaves every verdict
  byte-identical — that would invalidate every user's baseline for nothing.
  Say the bump in CHANGELOG.
- **A release is one commit, then a tag on it.** That commit bumps
  `plugin.json` and `_common.HARNESS_VERSION`, renames the CHANGELOG's
  `[Unreleased]` section to the dated version (leaving a fresh, empty
  `[Unreleased]` above it) and condenses it to what the release contains
  (≤ 2,000 tokens, `tests/test_prose_budget.py`; the long form stays in
  git history), and moves `ci/github-actions.yml`'s `EVALUP_REF`
  to `v<version>` (`test_ci_template` refuses anything else). Once it is on
  `main`, publish a GitHub release `v<version>` on that commit, in the same
  sitting: until then, a template copied from `main` fails at `git fetch`.
  Releases are immutable here, so the tag cannot be redone: check the commit
  before you publish.
- **Every state file declares `schema_version`** (`_common.SCHEMA_VERSION`):
  case files, `adapter.yaml`, `profile.yaml`, `datasets/dataset.yaml`, the run
  manifest, `reports/baseline.json`, `reports/holdout-seal.json` (optimize's
  per-seal noise pair). Bump it when a file's layout changes,
  and teach `validate_cases.check_schema_version` the migration it names.
  Its findings, and `stage_renamed` (a pre-#42 stage name, read as the
  new one), are the WARNs `--strict` does not promote: both are migrations. `plan.json` keeps
  its own `plan_version` and is rebuilt every run, so it carries none.

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
2. **`http` or `loops` alone never carries a case to `pass`.** Rollup rules
   5-6 leave out both (the run triggers them, not the case), so an
   all-`unscored` case rolls up `unscored`, never `fail`.
3. **A series is not a pair.** `run_history.py` keys on `mode` +
   `selecting_split` + the enabled-layer set, and **not** on the app `git_sha`;
   only `status: "ok"` runs join, and it never says "better".
4. **Cost NEVER gates, and its price table is a SIDECAR.** `cost_latency` is a
   capability, not a layer (contract **§5.7**, permanent); `--prices` is per app
   and **not in `plan.json`**; an unknown model withholds the dollars and keeps
   the tokens; a trace-less run cannot be priced at all. Related: **bootstrap
   resampling is DECLINED by name** — never "add a CI" that way; three exact
   tests replaced it.

## Business rules are reported by id, never scored anew

A case's `covers` (#25) names profile.yaml `business_rules` / `risks` ids,
and `validate_cases.py --profile` resolves them, refuses a gating case or a
canary covering a rule that is not `confirmed` with a human's
`confirmed_by`, and reports the gaps. `expect.answer.rules` (#26) names the
rules a case's answer is **checked** against, and counts as covers for that
refusal. Each listed rule is bound to a check on the case, script first:

- **scripted:** the rule's `check:` in profile.yaml, `must_contain` /
  `must_not_contain` entries the case carries verbatim (the validator
  ERRORs `rule_check_missing` otherwise). The runner's answer layer scores
  them as it scores any entry; nothing new scores anything.
- **judged:** a `form: criteria` rubric criterion with `rule: <id>` on the
  case's pinned rubric, graded by the judge pass and provisional like every
  judged verdict. The tag never reaches the judge's prompt.

`scripts/rule_report.py` reads the finished run (and `judge/judged.json`)
and attributes those existing verdicts to rule ids in `rules.json`. Keep it
an attribution: **the runner's `rules` row stays `unscored`**, no rule's
`status` ever decides a verdict, and nothing on the decision path (`gate.py`,
`stats.py`, `run_history.py`, the runner, optimize) reads `rules.json`. A
scripted rule reaches a gate only through its case's own verdict; a judged
one never does in v1. `SCORING_VERSION` and `JUDGE_VERSION` did not move.
The quickstart's rule quotes are checked against the `file:line` their
source names, so moving the fixture's `SYSTEM_PROMPT_FRAGMENTS` in
`tests/test_example.py` means updating `profile.yaml` (and regenerating
`converted.json`). "Never gates" has one exception, by design of `gate.py`
(not to be changed for this): `--min-pass-rate` counts every scored
non-canary case, so a `gating: false` case on an unconfirmed rule moves
that floor; `summary.traceability.unconfirmed_non_gating` lists them.

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

## CI

`.github/workflows/ci.yml` (repo root) runs on every push since 2026-09-30.
**Never report CI as green without reading the run** (`gh run list`,
`gh run view`) for the commit in question.
