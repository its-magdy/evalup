# agent-eval — session handoff / working state

**Purpose:** this file is the single place a new session reads to pick up work
without re-deriving anything. It is deliberately small. The *findings* live in
`AUDIT-2026-09-06.md`; this file holds **state, plan, and gotchas only**.

**Last updated:** 2026-09-10 (Step 9b DONE — run history ships; one session left: 9c)
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
- **Health:** **652** tests pass (~2min), ruff clean, all **16** CLI scripts
  `--help` rc=0. Verified 2026-09-10 on **3.14 and 3.9** (the blessed
  `unittest` command runs on whatever `python3` is — here 3.14 — so the uv 3.9
  floor check below is not optional).

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
   `references/`, procedure in `SKILL.md`. **The part that mattered is closed**
   — what `run` loads before a case executes fell ~12,800 → ~4,891 (`AUDIT` §1
   addendum). Total prose barely moved, which is fine: **≤120 lines is retired.**

**Do NOT rewrite the scorers.** They audited clean (statistics verified
correct, HTML escaping hardened, multiset trajectory semantics, regex
watchdog, authz honest-degradation). Step 3 touched only the three named bugs
and their siblings — hold that line.

---

## 3. Plan — one step per session, in order

Each step is sized for a single low-usage session. Mark done as you go.

- [x] **Steps 0–2 — baseline; splits are a field; one path convention. DONE
      2026-09-07 — `6c7d286`, `f03c960`, `05804ef`.** Paths:
      `${CLAUDE_PLUGIN_ROOT}/...` cross-skill, relative inside a skill, app/state
      bare (README §"Path convention"). **The audit's offender list was wrong in
      both directions** — grep, don't work a list.

- [x] **Steps 3–4 — the three scorer bugs; tighten `validate_cases.py`. DONE
      2026-09-08 — `0d2aa91`, `201aa2a`.** Each bug was a class with siblings
      (NFC in **six** scorers, via `_common.nfc`). The severity line — **ERROR =
      a claim nothing backs; WARN = thinner than the guidance recommends** — is
      in the docstring + `case-format.md`.

- [x] **Steps 5a–5b — spec and build `run_cases.py`. DONE 2026-09-08/09 —
      `77d3778`, `c9bc4f7`, `5e573c9`.** Body in `AUDIT` §2 + the contract:
      **it was not implementable as written**; all nine decisions were answered,
      none guessed.

- [x] **Step 5c — Rewire `run/SKILL.md`** around the runner. **DONE
      2026-09-09 — `676ab23`.** 318 → **164**; the replacement had to ADD
      **§1, building `plan.json`**, and answer all seven exit codes.

- [x] **Steps 6a–6c — slim `generate`, `analyze`, `discover`. DONE 2026-09-09
      — `86b602e`, `d6bf421`, `6268cc3`.** Body in `AUDIT` §1's re-measured block.

- [x] **Step 6d — the two decisions. DONE 2026-09-09 — `4ede98f`.** No skill
      edited. **D1 — `≤120 lines` is RETIRED; D2 — `help` loses its body** (6e).
      Argument in `AUDIT` §1's addendum and §12 rec 5.

- [x] **Step 6e — dedupe + execute D2. Step 6 CLOSED. DONE 2026-09-09 —
      `a22ce8e`.** Only `help` duplicated anything — **a MENTION count is not a
      duplication count**. `AUDIT` §1 + §4.

- [x] **Step 7 — Decide multi-turn: RESERVED. DONE 2026-09-10 — `337181a`.**
      Body in `AUDIT` §8's RESOLVED block; Step 10 rests on its precedent.

- [x] **Step 8a — Packaging. DONE 2026-09-10 — `526e495`.** Body in `AUDIT`
      §"RESOLVED … (Step 8a)". Two facts §4 rests on: **CI was written knowingly
      as a spec**, and **`ruff.toml` did NOT fold in**.

- [x] **Step 8b — Stale artifacts + `micro_f1`. Step 8 CLOSED. DONE
      2026-09-10 — `593aad1`.** Body in `AUDIT` §"RESOLVED … (Step 8b)". Live
      warning: **`REVIEW` §10's "regenerate the worked example" is NOT DONE —
      the plugin ships no conforming end-to-end example**, and a later step
      must build a NEW one rather than spend the field-test fixture.

**`REVIEW` §4 has TEN gaps and the old Step 9 bullet named four.** Reconned
2026-09-10 — `AUDIT` §"Step 9 recon" has the disposition table; six were in no
step. **Order: 9a → 10 → 9b → 9c, then STOP.**

- [x] **Step 9a — close the judge loop (`REVIEW` §4.4). DONE 2026-09-10 —
      `eebd318`.** `score_agreement.py` **plus a gate change in `run_cases.py`**
      (§4's gotcha). Body in `AUDIT` §"RESOLVED … (Step 9a)": why "all data
      already on disk" was FALSE, and why κ gets Fisher's exact p and no CI.
- [x] **Step 10 — RESERVE `REVIEW` §4's items 2, 5, 7, 8 and 10. DONE
      2026-09-10 — `d22bcaa`.** All five reserved, none built; body in `AUDIT`
      §"RESOLVED … (Step 10)". **§4.7 was the live one** and its rollup half hit
      every case, not just `state` — see §4's `http` gotcha. **§4.2 is a doc
      correction; §4.5 is a non-finding** (nothing declares a per-layer
      statistic, so there is nothing to reserve). 4 WARNs + 1 ERROR.
- [x] **Step 9b — run history (`REVIEW` §4.9). DONE 2026-09-10 — `2bbb40a`.**
      `scripts/run_history.py` + 58 tests, named in `analyze/SKILL.md`. Body in
      `AUDIT` §"RESOLVED … (Step 9b)". **"All data already on disk" was WRONG a
      fifth time; the gap was COMPARABILITY** — `results.json` has no timestamp,
      `k`, `selecting_split` or `capability_matrix` (`manifest.yaml`-only).
      **No flakiness ledger exists** (the signal is `reliability.json`, k>1
      only), and **nothing declared a trend view**, so 9b had nothing to
      reserve. Output is stdout/`-o`, never inside a run dir, so §6 and the
      three doc-reading tests were untouched.
- [ ] **Step 9c — cost/latency (`REVIEW` §4.3).** `cost_usd` is RENDERED by
      the viewer (:379, :384) and written by nothing; `stats.py` is binary-only.
      Needs a price table + a continuous statistic — a design decision. Two
      gifts left for it: `capability_matrix.cost_latency` is enabled with NO row
      in the layer table (Step 10), and per-case `latency_s` IS on disk in
      `results.json` and 9b deliberately aggregated none of it. When this lands
      the remediation wave is DONE.

**Not steps.** `REVIEW` §4.6 (RAG/retrieval) is greenfield — grep returns zero
files — so it is a new capability, **out of the plan**; §5's five "ideas worth
stealing" likewise.

---

## 4. Gotchas a new session will otherwise rediscover the hard way

- **Commands** (run from `agent-eval/`):
  - `python3 -m unittest discover -s tests` — 594 tests, ~2min, the blessed
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
- **The judged gate is DERIVED as of 9a.** A plan's `judge.status: calibrated`
  is necessary, not sufficient: `run_cases.py` also demands
  `paths.judge_calibration`'s sidecar (`score_agreement.py --write`). Every
  failure is a reason string on the `unjudged` layer, never an exit code.
- **`http` no longer carries a case to `pass` (Step 10).** Rollup rule 5 is
  **non-`http`**; rule 6 keeps a case that asserts nothing else a pass. So a
  case whose real layers are all `unscored`/`unscorable` now rolls up
  `unscored`. Never `fail` — the change can only be more conservative.
- **A series is not a pair (Step 9b).** §5.6's three keys suffice for
  `stats.py` because a pair is joined by case-id INTERSECTION; a trend over
  aggregates has none, so `run_history.py` also keys on `mode` +
  `selecting_split` + the enabled-layer set — and **not** on the app `git_sha`,
  the independent variable. Only `summary.status: "ok"` runs join a series, and
  it never says "better": that is `stats.py`'s keep rule, and this repo refuses
  a second one.
- **Five things are RESERVED and the validator WARNs on each:** `expect.state`,
  `seed_state`, `available_tools`, `excluded_tools` (+ `difficulty`, doc-only).
  `run_cases.py` calls `environment.seed/.reset/.snapshot_state` **nowhere** —
  seed out of band. Do not "wire up" one without a scorer to read it.
- **The holdout ledger is a `.jsonl` sidecar** (`datasets/holdout-looks.jsonl`,
  from the plan's `paths.holdout_ledger`), not the YAML dataset metadata — a
  stdlib-only runner would corrupt the YAML. The running total is a LINE COUNT,
  not one skill's entries.
- **`__oos__` is `score_routing.py`'s canonical internal OOS label**, not a
  data value. `--oos-route <name>` maps the profile's route name onto it.
- **The scorers' error contract:** errors go to **stdout** (not stderr) as
  JSON, exit 2 on malformed input. Deliberate — see `_common.die()`.
- **`field-test-qa/.agent-eval/` is stale ON PURPOSE** — mixed layout, 36
  validator errors, two fixtures depend on it; **read its `LAYOUT.md` before
  migrating, regenerating or copying it.** Not a good example, and nothing else
  in the repo is one either.
- **The app under test in the field test is READ-ONLY** and lives outside this
  repo (`/path/to/reference-app`). Never modify it.
- **The audit's token numbers are `words × 1.33` over
  `agent-eval/skills/**/*.md`, whole file — `wc -l` is not their meter.**
  Measure that way before claiming a size win (`AUDIT` §1 has the figures).
- **`.github/workflows/ci.yml` has never run.** There is still no git remote,
  so it is a specification (its header says so). The three commands above are
  the only checked claim — never report CI as green.
- Reproducing any audit finding: every one has a copy-pasteable repro in
  `AUDIT-2026-09-06.md`. Re-run rather than re-deriving.
