# agent-eval — session handoff / working state

**Purpose:** this file is the single place a new session reads to pick up work
without re-deriving anything. It is deliberately small. The *findings* live in
`AUDIT-2026-09-06.md`; this file holds **state, plan, and gotchas only**.

**Last updated:** 2026-09-10 (Step 8a done — packaging; next is Step 8b)
**Update rule:** whenever you finish a step, edit §3 (mark it done, add what you
actually did + the commit sha) and bump the date above. Do not let this file
grow past ~200 lines; move detail into the audit doc or a commit message.

---

## 1. Where things stand

- **Repo:** `/path/to/workspace`
- **Plugin:** `agent-eval/` (v0.1.0). Root also holds `research/`,
  `field-test-qa/`, and the review docs.
- **Branch:** `run-layout-consistency`. Main branch is `main`.
- **Baseline commit `6c7d286`** holds the whole prior review wave, so every
  later diff is readable against it.
- **Health:** 552 tests pass (~2min), ruff clean, all 14 CLI scripts `--help`
  rc=0. Verified 2026-09-10 on **3.9, 3.11 and 3.13** — 552 each, so the floor
  is now checked against real interpreters, not only `ast.parse`'s grammar.

### Documents, in the order a newcomer should read them
| File | What it is |
|---|---|
| `HANDOFF.md` (this) | state + plan. Start here. |
| `AUDIT-2026-09-06.md` | **current** audit. All findings, all verified. |
| `REVIEW-2026-08-08.md` | prior review. Its §2 bugs + §10 doc fixes are DONE; **§§3–9 are still open** and are the source for later steps. |
| `field-test-qa/qa-report.md` | 2026-07-18 real-user QA field test. Most of its items are fixed; useful for the user's-eye view. |
| `EVAL-DESIGN-RECOMMENDATION.md` | 2026-08-02 design synthesis. Authoritative for *direction*, not for current state. |

---

## 2. The two conclusions that drive everything else

1. **There was no execution engine** — `run/SKILL.md` §2 "Execute" was
   hand-orchestrated by the LLM every run: not reproducible, expensive, and it
   silently dropped required outputs. **Fixed as of Step 5c**: the spec is
   `docs/runner-contract.md`, the engine is `scripts/run_cases.py`, and the skill
   builds a plan and reads a result instead of executing.
2. **7 skills is the right count; the weight was the problem.** Rationale to
   `references/`, procedure in `SKILL.md`. **Re-measured 2026-09-09 (`AUDIT` §1
   addendum): the part that mattered is closed** — what `run` loads before a
   case executes fell ~12,800 → ~4,891. Total prose barely moved, which is
   fine: see §3 Step 6d, and **≤120 lines is retired.**

**Do NOT rewrite the scorers.** They audited clean (statistics verified
correct, HTML escaping hardened, multiset trajectory semantics, regex
watchdog, authz honest-degradation). Step 3 touched only the three named bugs
and their siblings — hold that line.

---

## 3. Plan — one step per session, in order

Each step is sized for a single low-usage session. Mark done as you go.

- [x] **Steps 0–2 — baseline; splits are a field; one path convention.**
      **DONE 2026-09-07 — `6c7d286`, `f03c960`, `05804ef`.** Paths:
      `${CLAUDE_PLUGIN_ROOT}/...` cross-skill, relative inside a skill, app/state
      paths bare (README §"Path convention"). **The audit's offender list was
      wrong in both directions** — grep, don't work a list.

- [x] **Steps 3–4 — the three scorer bugs; tighten `validate_cases.py`.**
      **DONE 2026-09-08 — `0d2aa91`, `201aa2a`.** Each bug was a class with
      siblings: `sub_mde_keep`; NFC in **six** scorers via `_common.nfc`;
      `--oos-route`; `score_authz --id-pattern`; `stringify`'s `ensure_ascii`.
      The validator's severity line — **ERROR = a claim nothing backs; WARN = a
      suite thinner than the guidance recommends** — is in its docstring and
      `case-format.md`; four new ERRORs + required `--capabilities` took the
      shipped 12-case field test 0 → 36, all true.

- [x] **Steps 5a–5b — spec and build `run_cases.py`.** **DONE 2026-09-08/09 —
      `77d3778`, `c9bc4f7`, `5e573c9`.** Spec is `agent-eval/docs/runner-contract.md`;
      551 tests (+102) on a real fake HTTP app. Missing-jsonl closed
      structurally: both files are **regenerated from the case dirs after every
      case**, finalize exits 6 rather than write ok. **All nine open decisions
      were answered in the same commits** — the contract could not be
      implemented as written (nothing said how an HTTP request is BUILT), so
      `invocation` gained five declared fields and four more silences became
      **adapter declarations**, none guessed. `REQUIRED_*` tables are **exec'd
      out of the contract file**, not restated.

- [x] **Step 5c — Rewire `run/SKILL.md`** around the runner.
      **DONE 2026-09-09 — `676ab23`.** 318 → **164**. §§1–3 deleted whole (the
      runner's job verbatim), but the replacement had to ADD what nothing wrote
      — **§1, building `plan.json`** — and answer each of the seven exit codes
      (**6** = a missing artifact, so the run is neither quotable nor a
      baseline; **7** = artifacts complete, numbers not). Also **`--layer X`
      finally means something**.

- [x] **Steps 6a–6c — slim `generate`, `analyze`, `discover`.**
      **DONE 2026-09-09 — `86b602e`, `d6bf421`, `6268cc3`.** 401→159, 252→167,
      235→160; mean sentence 16.0→12.7, 20.4→15.6, 24.6→14.3. Each began with an
      **ownership pass**, and that pass — not fatigue — set the floor each time:
      the residue is procedure. Two new reference files where nothing owned the
      rationale; section numbers preserved in all three (cited BY NUMBER from
      `run-modes.md`, `generate`, `optimize`, `dotnet.md`).

- [x] **Step 6d — the two decisions.** **DONE 2026-09-09 — `4ede98f`.** No
      skill edited; the metric, the measurements and the argument are in
      `AUDIT-2026-09-06.md` §1's addendum. **D1 — `≤120 lines` is RETIRED and
      `SKILL.md` slimming is DONE**: 62% of what leaves a `SKILL.md` comes back
      as `references/`, so the total and "move rationale out" are in direct
      tension and the total is not the target; the headline number closed via
      Step 5c deleting a dependency, not via prose. **D2 — `help` keeps its
      slash command, loses its body** — executed in 6e.

- [x] **Step 6e — dedupe + execute D2. Step 6 CLOSED.** **DONE 2026-09-09 —
      `a22ce8e`.** Triage first, and it changed the job: of the three counts
      **only `help` duplicated anything** — the rest are *uses* or *owners*.
      **A MENTION count is not a duplication count.** `help` 124 → 42 lines
      (1,403 → 602 tokens); its `description` GREW (with no body it is the
      only thing that fires the skill). Detail: `AUDIT-2026-09-06.md` §1 + §4.

- [x] **Step 7 — Decide multi-turn: RESERVED.** **DONE 2026-09-10 —
      `337181a`.** Not delete-only: the skip gate tested `invocation.session`
      for **presence only** while `dotnet.md` shipped a worked `session:`
      block, so multi-turn cases ran **single-turn** and scored a truncated
      conversation as a real verdict — **declaring the contract made the
      harness less correct than omitting it.** Gate is now blind to
      `session`; `single_turn_suite` was **inverted, not dropped**, into
      per-case `multi_turn_case_reserved` (WARN). Argument + both corrections:
      `AUDIT-2026-09-06.md` §8's RESOLVED block.

- [x] **Step 8a — Packaging.** **DONE 2026-09-10 — `526e495`.** The recon's
      five claims all re-verified true (rare — say so). **The remote decision
      went to the user: write CI knowingly as a spec.** `.github/workflows/
      ci.yml` therefore opens by stating it has NEVER executed and that the
      checked claim is §4's local commands — do not cite it as evidence.
      `pyproject.toml` is **dev-tooling only, no `[project]` table** — that
      absent `[project.dependencies]` is the one slot a runtime import could
      enter through. **`ruff.toml` did NOT fold in**, so the blessed commands
      are UNCHANGED. `.tool-versions` keeps 3.11.11 and now argues why the dev
      pin sits above the floor. CI's matrix is REAL interpreters (3.9/3.11/
      3.13, 552 each), and its `--help` job skips `_common.py` by name.
      **`REVIEW` §3's other two items stay closed**, not re-fixed.

- [ ] **Step 8b — Stale artifacts.** Regenerate `field-test-qa/.agent-eval/`
      (36 errors since Step 4; migration is mechanical, `201aa2a`) or label it
      "pre-2026-08 layout" — note §4 already relies on it as the
      ambiguous-layout FIXTURE, so relabelling may be the honest call.
      `micro_f1`: **relabel, don't drop** — `score_routing.py`:296 uses the
      macro/micro gap as the minority-route skew warning.

- [ ] **Step 9 — Reopen `REVIEW-2026-08-08.md` §§4–5**: cost/latency as
      first-class metrics, `score_agreement.py` to close the judge loop (the
      viewer exports annotation JSONL nothing reads back), the longitudinal
      run-history layer, and the RAG/retrieval layer.

---

## 4. Gotchas a new session will otherwise rediscover the hard way

- **Commands** (run from `agent-eval/`):
  - `python3 -m unittest discover -s tests` — 551 tests, ~2min, the blessed
    command. (`test_run_cases.py` is most of the time: a real HTTP server per
    test, and one test kills a runner mid-run.)
  - `ruff check --config ruff.toml .` — `--config` is not decoration:
    `pyproject.toml` is now a sibling, and when both exist **`ruff.toml` wins
    and a `[tool.ruff]` table in `pyproject.toml` is ignored silently**.
  - `uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q`
    — the only conclusive 3.9 floor check. Run before any release.
- **`validate_cases.py` and `run_cases.py` take JSON, not YAML**, deliberately
  — read the docstrings before "fixing" that. The skill's YAML→JSON conversion
  *is* the run's parse, not a second opinion.
- **The runner's spec is `agent-eval/docs/runner-contract.md` and it wins.** Its
  nine decisions were confirmed 2026-09-08, with trade-offs — don't reopen one
  without reading it. **Three tests read the file at run time** (they exec
  §9(b)'s python block, parse §6's `# only when:` markers, and pin
  `--oos-route`'s precondition), so an edit fails the suite until the code
  follows — deliberate, not brittle.
- **The holdout ledger is a `.jsonl` sidecar** (`datasets/holdout-looks.jsonl`,
  from the plan's `paths.holdout_ledger`), not the YAML dataset metadata — a
  stdlib-only runner would corrupt the YAML. The running total is a LINE COUNT,
  not one skill's entries.
- **`__oos__` is `score_routing.py`'s canonical internal OOS label**, not a
  data value. `--oos-route <name>` maps the profile's route name onto it.
- **The scorers' error contract:** errors go to **stdout** (not stderr) as
  JSON, exit 2 on malformed input. Deliberate — see `_common.die()`.
- **`field-test-qa/.agent-eval/` is in a mixed layout** (`baselines/` + `runs/`
  old, `reports/` new, no `reports/baseline.json`) — the exact ambiguous state
  `run/SKILL.md` §4 halts on. A fixture for that branch, not a good example.
- **The app under test in the field test is READ-ONLY** and lives outside this
  repo (`/path/to/reference-app`). Never modify it.
- **The audit's token numbers are reproducible; `wc -l` is not their meter.**
  `words × 1.33` over `agent-eval/skills/**/*.md`, whole file. At `6c7d286` it
  returns 33,790 / 12,739 / 9,397 / 1,386 against the audit's ~33,900 / ~12,800
  / ~9,400 / ~1,400. Measure that way before claiming a size win.
- **`.github/workflows/ci.yml` has never run.** There is still no git remote,
  so it is a specification (its header says so). The three commands above are
  the only checked claim — never report CI as green.
- Reproducing any audit finding: every one has a copy-pasteable repro in
  `AUDIT-2026-09-06.md`. Re-run rather than re-deriving.
