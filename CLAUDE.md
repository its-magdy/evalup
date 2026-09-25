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
- **`.github/workflows/ci.yml`** — a specification. There is no remote, so **it
  has never run. Never report CI as green.**

## Working on the plugin

**Read `evalup/CONTRIBUTING.md` before changing anything under `evalup/`.** It
has the three blessed checks and the rules that look arbitrary until you break
one. The short version:

```sh
cd evalup
python3 -m unittest discover -s tests                    # 782 tests, ~2.5min
ruff check --config ruff.toml .                          # --config is required
uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q
```

## State

- **`main`** holds everything: the remediation wave, the worked example, the
  rename, the doc cleanup and all four September audits' fixes, the
  2026-09-24 prompt audit and its five follow-ups included
  (`evalup/CHANGELOG.md` lists them; merged 2026-09-24). It is the only
  branch. Nothing is pushed; **there is still no remote**, so nothing here
  has ever been checked by CI.
- **Health:** 782 tests pass on 3.14; 774 + 8 skips on the 3.9 floor (all 8
  skips need PyYAML: 782 pass with `--with pyyaml`), ruff clean, 21 CLIs
  answer `--help`, `claude plugin validate` clean for the plugin and the
  root marketplace. Tests, ruff and the 3.9 floor re-verified 2026-09-25 on
  `fieldtest-fixes`.
- **A live session works**, re-run twice 2026-09-21 after `make_plan.py`:
  headless `/evalup:start` on an unseen toy app reached a scored run in 4.5 min
  (was 14), 3/3 planted flaws found, no script failures. That run needed
  `--dangerously-skip-permissions`; after the permission fixes (uncommitted) a
  second run completed under `--permission-mode acceptEdits` + `Bash(curl *)`
  alone, 4 harmless denials. **Known limit, by platform design:** a skill's
  pre-approvals last one turn, so a headless `--resume` turn is denied — put
  the whole request in one prompt (README §Permissions). Toy app, single
  samples; not yet tried on a real LLM app.
- **The 2026-09-24 field test** (branch `fieldtest-fixes`, 2026-09-25): a
  first run on a real ASP.NET Core app with five planted flaws, nine headless
  sessions. Runs surfaced 3/5 flaws; headless `run` orphaned its runner twice
  (background shell killed at turn end), 48 permission denials, `help`
  promised Jaeger unlocks tool-use layers, generate never read discover's
  findings, a headless suite ended with nothing gating unsaid. Eleven fixes,
  one commit each plus one follow-up from the live proof session (C1–C11; `evalup/CHANGELOG.md` "the 2026-09-24 field
  test"): `wait_run.py`, the README permission recipe, app-path scoping,
  `--insecure-tls`, the out-of-tree state offer, the CI-gate recipe's first
  check, the traces claim, findings as a grid input, `nothing_gates`, and
  two doc-only items. Five eval cases live in `evalup/evals/`; the three
  app-bound ones need a Bash-granting sandbox, which this machine refuses
  (symlinks inside `~/.docker`; `DOCKER_CONFIG` does not bypass it), so they
  were proven by two live `claude -p` sessions instead: a full first session
  ($5.58, 13.6 min) and a slow smoke run that crossed the ten-minute cap and
  came back through `wait_run.py` ($1.79, 16 min). C5 took three
  placements and three more sessions (~$5 each): the out-of-tree statement
  finally appears, but in the closing summary, never before the first write
  — headless that is the same information, so it was left there. Later
  sessions had 0 denials. Not merged; the field-test folder is untouched.
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
- **History was rewritten 2026-09-20** to carry a personal author email rather
  than a work one. Every SHA therefore changed; SHAs quoted in commit messages
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
  without deleting one. New effort goes to the first ten minutes, not the
  engine.
- **Out of plan by decision:** RAG/retrieval scoring, and the five "ideas worth
  stealing" from the 2026-08 review. Those are a greenfield wave, not
  remediation. Five reserved case fields wait for a scorer that reads them —
  `CONTRIBUTING.md` names them; do not wire one up without one.

## History lives in git, not at the repo root

The audit, the review, the build plan and the session handoff were deleted
2026-09-20 once their work was closed — as was `field-test-qa/`, a 2026-07-18
QA archive (`git show de9641a` has it). Code comments refer to "the 2026-09
audit" or "the 2026-08 review"; those are the commit history, not files. Don't
recreate them as files. When a doc's job ends, delete it and let the commit
messages carry the reasoning.
