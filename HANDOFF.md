# agent-eval — session handoff / working state

**Purpose:** this file is the single place a new session reads to pick up work
without re-deriving anything. It is deliberately small. The *findings* live in
`AUDIT-2026-09-06.md`; this file holds **state, plan, and gotchas only**.

**Last updated:** 2026-09-10 (Step 9a DONE — the judge gate is derived; next is Step 10)
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
- **Health:** **590** tests pass (~2min), ruff clean, all **15** CLI scripts
  `--help` rc=0. Verified 2026-09-10 on **3.9, 3.11, 3.13 and 3.14** — 590
  each. The blessed `unittest` command runs on whatever `python3` is (here
  3.14), so the uv 3.9 floor check below is not optional.

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

- [x] **Steps 0–2 — baseline; splits are a field; one path convention.**
      **DONE 2026-09-07 — `6c7d286`, `f03c960`, `05804ef`.** Paths:
      `${CLAUDE_PLUGIN_ROOT}/...` cross-skill, relative inside a skill, app/state
      paths bare (README §"Path convention"). **The audit's offender list was
      wrong in both directions** — grep, don't work a list.

- [x] **Steps 3–4 — the three scorer bugs; tighten `validate_cases.py`.**
      **DONE 2026-09-08 — `0d2aa91`, `201aa2a`.** Each bug was a class with
      siblings (NFC needed fixing in **six** scorers via `_common.nfc`). The
      severity line — **ERROR = a claim nothing backs; WARN = a suite thinner
      than the guidance recommends** — is in the docstring + `case-format.md`.

- [x] **Steps 5a–5b — spec and build `run_cases.py`.** **DONE 2026-09-08/09 —
      `77d3778`, `c9bc4f7`, `5e573c9`.** Body in `AUDIT` §2 + the contract
      itself: **the contract was not implementable as written**, and all nine
      open decisions were answered in the same commits — none guessed.

- [x] **Step 5c — Rewire `run/SKILL.md`** around the runner. **DONE
      2026-09-09 — `676ab23`.** 318 → **164**. §§1–3 deleted whole, but the
      replacement had to ADD what nothing wrote — **§1, building `plan.json`**
      — and answer each of the seven exit codes (**6** = a missing artifact, so
      the run is neither quotable nor a baseline; **7** = artifacts complete,
      numbers not).

- [x] **Steps 6a–6c — slim `generate`, `analyze`, `discover`.**
      **DONE 2026-09-09 — `86b602e`, `d6bf421`, `6268cc3`.** 401→159, 252→167,
      235→160. Each began with an **ownership pass**, and that pass — not
      fatigue — set the floor: the residue is procedure. Section numbers
      preserved in all three (cited BY NUMBER from four other files).

- [x] **Step 6d — the two decisions.** **DONE 2026-09-09 — `4ede98f`.** No
      skill edited; argument in `AUDIT` §1's addendum. **D1 — `≤120 lines` is
      RETIRED**: 62% of what leaves a `SKILL.md` returns as `references/`, so
      the total is not the target. **D2 — `help` loses its body** — done in 6e.

- [x] **Step 6e — dedupe + execute D2. Step 6 CLOSED.** **DONE 2026-09-09 —
      `a22ce8e`.** Of the three counts **only `help` duplicated anything** — **a
      MENTION count is not a duplication count**. `AUDIT` §1 + §4.

- [x] **Step 7 — Decide multi-turn: RESERVED.** **DONE 2026-09-10 —
      `337181a`.** Body in `AUDIT` §8's RESOLVED block — read it before Step
      10, which rests on its precedent.

- [x] **Step 8a — Packaging.** **DONE 2026-09-10 — `526e495`.** Body in
      `AUDIT` §"RESOLVED … (Step 8a)". Two facts §4 depends on: **CI was
      written knowingly as a spec**, and **`ruff.toml` did NOT fold in**.

- [x] **Step 8b — Stale artifacts + the `micro_f1` relabel. Step 8 CLOSED.**
      **DONE 2026-09-10 — `593aad1`.** Both **relabelled, neither
      regenerated**; §10's "every case accepted by `test-generator`" was
      **WRONG** (6 of 12). **`REVIEW` §10's "regenerate the worked example" is
      NOT DONE — the plugin ships no conforming end-to-end example**, and a
      later step must build a NEW one rather than spend the fixture.
      `micro_f1`'s **key is unchanged**. `AUDIT` §"RESOLVED … (Step 8b)".

**`REVIEW` §4 has TEN gaps and the old Step 9 bullet named four.** Reconned
2026-09-10 — `AUDIT` §"Step 9 recon" has the full disposition table; six were
in no step. **Order: 9a → 10 → 9b → 9c, then STOP.** Four sessions, not nine.

- [x] **Step 9a — close the judge loop (`REVIEW` §4.4). DONE 2026-09-10 —
      `eebd318`.** `score_agreement.py` **plus a gate change in
      `run_cases.py`** — a scorer that only emitted numbers would leave
      `judge.status` exactly as hand-set as it was, so `calibrated` is now
      **necessary and not sufficient**: it must be backed by
      `paths.judge_calibration`, the scorer's `.json` sidecar, whose
      `rubrics_measured` must cover every rubric a selected case pins. **The
      recon was wrong once — "all data already on disk" is FALSE**: the runner
      writes the judged layer `unjudged` on *every* path, so nothing holds a
      judge verdict and the judge/human pairing must be recorded at labeling
      time (`judge_label` on the annotation line). κ gets **no confidence
      interval** — the usual one is a normal approximation, so Fisher's exact
      p instead. `AUDIT` §"RESOLVED … (Step 9a)".
- [ ] **Step 10 — RESERVE `REVIEW` §4's items 2, 5, 7, 8 and 10.** Multi-agent
      attribution, per-layer stats, environment lifecycle, streaming, and the
      schema fields with no scorer. **A reserve sweep, not a build wave** — all
      five are the same shape Step 7 fixed (a field or declaration with no
      scorer behind it), and §4 item 10 says "or mark the fields reserved"
      itself. Use 7's `multi_turn_case_reserved` WARN pattern. Cheapest real
      work left; do it before 9b/9c inherit the overclaims.
- [ ] **Step 9b — run history (`REVIEW` §4.9).** `run_history.py`: per-metric
      time series + monotone-drift flag. Cheap aggregation, data already on disk.
- [ ] **Step 9c — cost/latency (`REVIEW` §4.3).** `cost_usd` is RENDERED by
      the viewer (:379, :384) and written by nothing; `stats.py` is binary-only.
      Needs a price table + a continuous statistic — a design decision. **Keep
      out of 9a's session.** When this lands the remediation wave is DONE.

**Not steps.** `REVIEW` §4.6 (RAG/retrieval) is greenfield — grep returns zero
files — so it is a new capability, not a defect, and it is **out of the plan**;
pick it up only if wanted. `REVIEW` §5's five "ideas worth stealing" are the
same: optional, none is a defect.

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
- **The judged gate is DERIVED as of 9a.** A plan's `judge.status: calibrated`
  is necessary, not sufficient: `run_cases.py` also demands
  `paths.judge_calibration`'s sidecar (`score_agreement.py --write`). Every
  failure is a reason string on the `unjudged` layer, never an exit code.
- **The holdout ledger is a `.jsonl` sidecar** (`datasets/holdout-looks.jsonl`,
  from the plan's `paths.holdout_ledger`), not the YAML dataset metadata — a
  stdlib-only runner would corrupt the YAML. The running total is a LINE COUNT,
  not one skill's entries.
- **`__oos__` is `score_routing.py`'s canonical internal OOS label**, not a
  data value. `--oos-route <name>` maps the profile's route name onto it.
- **The scorers' error contract:** errors go to **stdout** (not stderr) as
  JSON, exit 2 on malformed input. Deliberate — see `_common.die()`.
- **`field-test-qa/.agent-eval/` is stale ON PURPOSE** — mixed layout, 36
  validator errors. Two fixtures depend on it; **read its `LAYOUT.md` before
  migrating, regenerating or copying it.** Not a good example, and nothing
  else in the repo is one either.
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
