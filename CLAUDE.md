# evalup

This repo builds **evalup** — a Claude Code plugin that evaluates and improves
any LLM chat/agent application: discover the app's architecture, generate eval
datasets, run layered scoring, analyze failures, optimize prompts with
statistical keep/revert gates.

Kept short on purpose: this file loads into every session. Depth goes in the
documents named below, not here.

## Layout

- **`evalup/`** — the plugin (v0.1.0). 7 skills (`start`, `discover`,
  `generate`, `run`, `analyze`, `optimize`, `help`), 3 agents, 28 CLIs in
  `scripts/` (each an `evalup <name>` subcommand; `_*.py` are shared
  modules), specs in `docs/`, the worked example in `examples/quickstart/`,
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
python3 -m unittest discover -s tests                    # 1531 tests, ~5 min
python3 tests/run_shards.py      # CI's 4 shards in parallel, ~50 s; not conclusive
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
- **`main`** holds everything: through the 2026-09-30 refactor review, code
  review and canary work, the 2026-10-09 CI and repo-settings PRs (#1, #3,
  #4, #5), and multi-turn Phase 1 (#2, squash-merged 2026-11-19 as
  `796c8d3`) built per `evalup/docs/multi-turn.md`. First pushed 2026-09-30.
  **Multi-turn is merged but not yet proven on a live app**: the user merged
  it before the planned RefApp run, and decided 2026-11-19 to prove it on a
  different app instead. Decided 2026-10-06 (after a second opinion): a
  checkpoint can veto a conversation but never certify it — a union `pass`
  needs a graded `pass` on the final turn (`run_cases.final_turn_certifies`).
  Since then (roadmap #32, PRs #44–#93): the keep rule and schema versions
  (#36–#38), the run-time judge, provisional only (#23), business rules
  (#25, #26), reach — `traces.source: response`, SSE, `invocation.mode:
  exec` (#39; Jaeger not built), pacing (#76, #30), the **v0.1.0 tag**
  (#40), `bin/evalup` (#41), two stages (#42), the prose budget (#92).
  #96: `convert_suite` re-runs under uv without PyYAML (#95 stays open).
  On 2026-11-22, fixes for the #27 re-proof's issues (#97–#112):
  - #113: criteria rubrics on a first session, rule confirmation in the
    label review, the dirty-tree check ignores evalup's state (#97, #101, #102).
  - #114: a delegate's relayed answer neither confirms a rule nor accepts a
    case (#100).
  - #115: holdout share of the whole suite, a power check before the noise
    pair, `clusters.json` (#98, #110, #112).
  - #116: word checks bounded for clitics, the judge pass's skip said, nits
    (#107–#109).
  - #117: app model to the manifest, `--not-before`, unknown
    `--manifest-extra` keys warned, a priced-run estimate (#104, #111,
    part of #105).
  - #118: the cost layer prices AG-UI cache reads/writes from a user table
    (#105, the other half).
  - #119: an approval interrupt with no text is named and warned on (#103).
  - #120: `evalup prelook-fix`, and holdout templates sealed (#99).
  - #121: `evalup write-artifact` for findings.md/report.md from a subagent
    (#106).
  - #122: cross-lane fixes after merging #113–#121; #123: the quickstart
    e2e's estimate keeps the declared app model.
  2026-11-23–25, fixes from the #27 re-run, run 3 and step 8 (#124–#162):
  probe exit 8, cost per repeat, H = 0 power check, seal hygiene, the
  optimize scripts (snapshot/revert, `seal-record`, clusters), judge and
  agent types; then `seal-record archive` (a grown seal gets its 5 looks
  back, #157), a sealed id never on `answer_after_interrupt` (#158), and
  the power check's `headroom_note` (#159).
- **Health** (2026-11-25, `main` at `b35d00e`): 1531 tests pass on 3.14
  and on the 3.9 floor `--with pyyaml` (without PyYAML, the tests that need
  it skip; last counted 2026-11-22), ruff clean, all 28 CLIs answer `--help`,
  `claude plugin validate` passes for the plugin (one warning: no icon) and
  the root marketplace. #117's
  `TestNotBefore::test_the_runner_waits_writing_nothing_then_runs` is flaky:
  `wait_run.py` exited nonzero in 2 of ~20 runs on 3.9.
- **What live sessions have shown.** Headless `/evalup:start` reaches a scored
  run on an unseen toy app (4.5 min, 3/3 planted flaws) and on a real ASP.NET
  Core app (3/5 planted flaws); two first-time-user rounds ran on RefApp
  (trace-less, Gemma on Google's free tier). **Known limit, by platform
  design:** a skill's pre-approvals last one turn, so a headless `--resume`
  turn is denied — put the whole request in one prompt (README §Permissions).
  A headless `run` that outlives its turn comes back through `wait_run.py`.
  The 2026-11-22 re-proof (#27), every step a subagent following the skills,
  on a real TypeScript/Cloudflare app with a streaming tool agent: trace
  layers scored live from the stream; the judge gave provisional verdicts
  only (4/4 agreement with blind labels, one model family, n = 4); one
  optimize loop was refused at P = 0.938 < 0.95 for want of holdout power
  (13 cases; ~27 needed), which led to #110. **2026-11-24, the first kept
  optimize loop on a live app:** the seal grown to 45, candidate c2 kept
  on look 3 of 5 at P = 0.994 (38/45 vs 29/45), its pin 23/28 vs 19/28.
  Caveats: its training run was not positive (P = 0.773; the owner spent
  the look anyway), and the 0.20 gain only just clears the MDE of 0.199.
  Judge checks so far are model-vs-model, one family (7/7 twice; 19/20 on
  2026-11-25, the one miss a conditional offer the judge called a pass;
  the owner ruled it a fail). Decided 2026-11-25: the model labels stand as
  #27's judge check, with no human round; the judge stays provisional
  (calibration needs ≥ 100 pairs). #27 stays open for a keep with a
  positive training run and conversations with client-side history.
- **`claude plugin eval`:** five cases in `evalup/evals/`. The three app-bound
  ones need a Bash-granting sandbox, which this machine refuses (symlinks
  inside `~/.docker`; `DOCKER_CONFIG` does not bypass it); they were proven by
  live `claude -p` sessions instead.
- **Where the evidence is.** The pre-release CHANGELOG had one section per
  wave (four September audits, the field test, two user tests, the refactor
  review) saying what was fixed and why; #42 cut it to a summary, so read
  the long form at the tag (`git show
  v0.1.0:evalup/CHANGELOG.md`). The runner contract's rationale went the
  same way (its history before the #42 trim). The user tests' findings are in
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
  - #99 (2026-11-22): a sealed case gets a pre-look fix only while the look
    ledger is empty (`prelook-fix`), and holdout templates are sealed too.
  - #103: an approval interrupt with no text stays `unscored`; documented
    and warned on (`answer_after_interrupt`), no scoring change.
  - #105: no price table ships; the cost layer prices the cache split only
    from the user's `prices.json`.
  - The out-of-tree state statement appears in the closing summary, never
    before the first write; headless that is the same information.
  - Left open: F-001 (`$ARGUMENTS` backticks, all seven skills), F-037
    (headless open questions have no home), F-014/F-020/F-030, F-106, F-125
    (friction), F-129 (the bench's agents), F-133, F-144, F-147, F-149
    (nits). Deferred: the validator's profile-name check (F-155).
- **Known gaps, stated in the README, not bugs:** conversations are scripted,
  Phase 1 only (no simulated user, §9 of the spec); judged verdicts (and
  judged business rules) are provisional and never gate until a live
  calibration (#27); a release check is two runs, regression then holdout.
- **Renamed `agent-eval` → `evalup` on 2026-09-14** (committed 2026-09-20).
  A cursory 2026-09-20 web check found PyPI, npm and the GitHub handle free,
  but **a live French SaaS trades as "EvalUp" (evalup.fr)** and `evalup.com` is
  parked. No trademark register was searched — do INPI/EUIPO/USPTO before any
  branded launch. **Neighbours (2026-11-19):** `hamelsmu/evals-skills` is
  deprecated in favour of **`ai-evals-course/evals-skills`** (9 Claude Code
  skills on eval methodology; still no runner, statistics or optimizer). The
  closest new ones: RELAI **`agent-eval-loop`** (a Claude Code/Codex plugin,
  created 2026-11-11, skills start/discover/eval/improve/loop), AWS
  **`Agent-EvalKit`**, and the official **deepeval** and **promptfoo** Claude
  Code plugins. The sealed holdout, canaries, refusing incomparable diffs and
  optimizing in the app's own source are still evalup's alone (roadmap #32).
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
- **No new scoring semantics without evidence from a live app** (decided
  2026-11-19; it replaced the blanket runner/scorer freeze of 2026-09).
  Changes to how evalup reaches an app (transport, intake) are allowed —
  that is what lets the run-time judge (#23) and reach (#39) proceed.
  Multi-turn Phase 1 is built; later phases (§9 of the spec) are not. The
  roadmap is issue #32, worked in step order.
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
