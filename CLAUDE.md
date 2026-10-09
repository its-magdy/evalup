# evalup

This repo builds **evalup** — a Claude Code plugin that evaluates and improves
any LLM chat/agent application: discover the app's architecture, generate eval
datasets, run layered scoring, analyze failures, optimize prompts with
statistical keep/revert gates.

Kept short on purpose: this file loads into every session. Depth goes in the
documents named below, not here.

## Layout

- **`evalup/`** — the plugin (v0.1.0). 7 skills (`start`, `discover`,
  `generate`, `run`, `analyze`, `optimize`, `help`), 3 agents, 21 CLI scripts
  in `scripts/`, specs in `docs/`, the worked example in `examples/quickstart/`,
  five `claude plugin eval` cases in `evals/`.
- **`EVAL-DESIGN-RECOMMENDATION.md`** + **`research/`** — why the plugin works
  the way it does. Authoritative for *direction*, not for current state.
- **`.github/workflows/ci.yml`** — ~1 min since 2026-10-09 (was ~4): the
  suite runs as four shards in `parallel:` steps, guarded so a test file in no
  shard fails the job. Actions SHA-pinned (the repo requires it), Dependabot
  bumps them monthly, every job on `ubuntu-24.04` (26.04 has no 3.9 build).
  actionlint cannot parse `parallel:` yet (rhysd/actionlint#693). **Never
  report CI as green without reading the run** (`gh run list`).

## Working on the plugin

**Read `evalup/CONTRIBUTING.md` before changing anything under `evalup/`.** It
has the three blessed checks and the rules that look arbitrary until you break
one. The short version:

```sh
cd evalup
python3 -m unittest discover -s tests                    # 943 tests, ~4min
ruff check --config ruff.toml .                          # --config is required
uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q
```

## State

- **Remote:** `https://github.com/its-magdy/evalup.git` (public; HTTPS via
  `gh`, because the SSH key is another account). Changes land as PRs,
  fast-forwarded onto `main`.
- **Repo settings** (2026-10-09): ruleset `main-guard` blocks deleting or
  force-pushing `main` — **a history rewrite like the two below needs it
  disabled first** — and requires the `ci-ok` check, so a commit reaches
  `main` only after CI passed on it on a branch: push the branch, wait, then
  fast-forward. `ci-ok` gathers the other jobs; a new job goes in its
  `needs`, never into the ruleset. Also on: private vulnerability reporting
  (`.github/SECURITY.md`), secret scanning with push protection, Dependabot
  alerts and security updates, CodeQL default setup (Python + Actions),
  required SHA pinning, immutable releases, delete-branch-on-merge. Secret
  scanning's generic patterns and validity checks need a paid plan; a
  personal account's PATCH is accepted and ignored. Community files
  (code of conduct, bug-report form, PR template) are in `.github/`.
- **`main`** holds everything but multi-turn: through the 2026-09-30 refactor
  review, code review and canary work, then the 2026-10-09 CI and repo-settings
  PRs (#1, #3, #5). First pushed 2026-09-30.
- **Branch `feat/multi-turn`** (2026-10-06; PR #2, open, rebased on main
  2026-10-09, CI green): multi-turn
  Phase 1 built per `evalup/docs/multi-turn.md` — §0 bugfix, `input.turns`,
  `invocation.conversation`, the driver, cookie style, per-turn tree, stats/
  cost/gate split, skills/docs. Live proof on RefApp still owed (the user runs
  it). Decided 2026-10-06 (after a second opinion): a checkpoint can veto a
  conversation but never certify it — a union `pass` needs a graded `pass`
  on the final turn (`run_cases.final_turn_certifies`).
- **Health** (2026-10-06, feat/multi-turn): 943 tests pass on 3.14;
  934 + 9 skips on the 3.9 floor (all 9 skips need PyYAML: 943 pass with
  `--with pyyaml`), ruff clean, 21 CLIs answer `--help`, `claude plugin
  validate` clean for the plugin and the root marketplace.
- **What live sessions have shown.** Headless `/evalup:start` reaches a scored
  run on an unseen toy app (4.5 min, 3/3 planted flaws) and on a real ASP.NET
  Core app (3/5 planted flaws); two first-time-user rounds ran on RefApp
  (trace-less, Gemma on Google's free tier). **Known limit, by platform
  design:** a skill's pre-approvals last one turn, so a headless `--resume`
  turn is denied — put the whole request in one prompt (README §Permissions).
  A headless `run` that outlives its turn comes back through `wait_run.py`.
- **`claude plugin eval`:** five cases in `evalup/evals/`. The three app-bound
  ones need a Bash-granting sandbox, which this machine refuses (symlinks
  inside `~/.docker`; `DOCKER_CONFIG` does not bypass it); they were proven by
  live `claude -p` sessions instead.
- **Where the evidence is.** `evalup/CHANGELOG.md` has one section per wave
  (four September audits, the field test, two user tests, the refactor
  review) saying what was fixed and why. The user tests' findings are in
  `~/Documents/Personal/Sandboxes/evalup-usertest/` (42) and
  `evalup-usertest-2/` (66) — **read-only**; each REPORT.md §2 is the ranked
  list. The field-test folder is untouched. `evalup/docs/traces-jaeger.md` is
  the Jaeger design note (not built).
- **Decided — do not re-propose:**
  - An app's own 5xx stays `infra_error`: its body was byte-identical to a
    provider outage's. A same-5xx-every-attempt case is flagged instead
    (`repeated_5xx`, F-165).
  - Declined as freeze-crossing: `--env-file` (F-159), `redact.py` (F-162), a
    probe script (F-103), scorer-side routing notes (F-123/F-140/F-141).
  - The capability matrix keeps the key `answer_quality`; the runner
    translates (`_common.MATRIX_KEY_OF_LAYER`). Renaming it to `answer` would
    break every profile and split `run_history.py`'s series key.
  - `--verify` recounts from disk with its own code; only the `infra_rate`
    definition is shared (`_common.infra_rate`).
  - `gate.py` does not close on a canary that reached no verdict (infra, or
    a layer `--layer` disabled): that is provider noise, not drift, and a
    canary `fail` already aborts. It prints NOT VERIFIED;
    `--require-canaries` is the opt-in strict form. Not split by gate kind —
    `gate.py` never reads it, and `--layer` is legal under `regression`.
  - F-166 (`wait_run.py` overran the 600 s cap) was not changed: a reviewer's
    reading, not the tester's, is that the script cannot overrun on its own
    and macOS sleep stops its clock.
  - The out-of-tree state statement appears in the closing summary, never
    before the first write; headless that is the same information.
  - Left open: F-001 (`$ARGUMENTS` backticks, all seven skills), F-037
    (headless open questions have no home), F-014/F-020/F-030, F-106, F-125
    (friction), F-129 (the bench's agents), F-133, F-144, F-147, F-149
    (nits). Deferred: the validator's profile-name check (F-155).
- **Known gaps, stated in the README, not bugs:** single-turn only (multi-turn
  cases are skipped); the judged layer and business rules have no run-time
  scorer; `--mode full` is two runs.
- **Renamed `agent-eval` → `evalup` on 2026-09-14** (committed 2026-09-20).
  A cursory 2026-09-20 web check found PyPI, npm and the GitHub handle free,
  but **a live French SaaS trades as "EvalUp" (evalup.fr)** and `evalup.com` is
  parked. No trademark register was searched — do INPI/EUIPO/USPTO before any
  branded launch. The neighbour that matters is **`hamelsmu/evals-skills`**
  (checked via the GitHub API 2026-09-21: 1.7k stars, 7 Claude Code skills —
  error analysis, synthetic data, judge prompts, evaluator validation, RAG,
  review UI, eval audit). It overlaps evalup's methodology skills and ships no
  runner, scorers, statistics or holdout — which is where evalup is alone.
  (`evalloop`/`evalview` exist only as tiny GitHub projects; an earlier note
  calling them marketplace plugins was never confirmed.)
- **History was rewritten 2026-09-30** before the first public push: every
  commit is authored as `its-magdy` (GitHub noreply email, set repo-locally),
  the reference app's real name, paths and ports became `RefApp` /
  `/path/to/…`, and `field-test-qa/` was purged. Pre-rewrite state is in
  `../evalup-backup-2026-09-30.bundle` (local only — never push it). An
  earlier **rewrite 2026-09-20** replaced a work author email. Every SHA therefore changed; SHAs quoted in commit messages
  written before that date refer to the pre-rewrite history and will not
  resolve.

## Scope — decided, not forgotten

- **The remediation wave is CLOSED** (2026-09-10) and the worked example
  shipped. Nothing from it is open.
- **Do NOT rewrite the scorers.** Two audits found their design sound —
  statistics verified, multiset trajectory semantics, authz
  honest-degradation. The 2026-09-20 audit fixed defects at their edges
  (strict JSON in, a shared regex watchdog, token-count handling, a URL-scheme
  allowlist) without touching what a number means. Hold that line.
- **The runner and scorers are feature-frozen**: bugfix-only, and no new layer
  without deleting one. The owner lifted the freeze for multi-turn ONLY
  (2026-10-06); Phase 1 is built, later phases (§9 of the spec) are not. New effort goes to the first ten minutes, not the
  engine.
- **Out of plan by decision:** RAG/retrieval scoring, and the five "ideas worth
  stealing" from the 2026-08 review. Those are a greenfield wave, not
  remediation. Five reserved case fields wait for a scorer that reads them —
  `CONTRIBUTING.md` names them; do not wire one up without one.

## History lives in git, not at the repo root

The audit, the review, the build plan and the session handoff were deleted
2026-09-20 once their work was closed — as was `field-test-qa/`, a 2026-07-18
QA archive. It was then purged from history (below); only the local bundle
`../evalup-backup-2026-09-30.bundle` still has it. Code comments refer to "the 2026-09
audit" or "the 2026-08 review"; those are the commit history, not files. Don't
recreate them as files. When a doc's job ends, delete it and let the commit
messages carry the reasoning.
