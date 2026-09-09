# agent-eval — session handoff / working state

**Purpose:** this file is the single place a new session reads to pick up work
without re-deriving anything. It is deliberately small. The *findings* live in
`AUDIT-2026-09-06.md`; this file holds **state, plan, and gotchas only**.

**Last updated:** 2026-09-09 (Step 6c done — `discover` slimmed and split)
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
- **Health:** 551 tests pass (~2min), ruff clean, all 14 CLI scripts `--help`
  rc=0, Python 3.9 compatible (re-verified with `uv`). Verified 2026-09-09.

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
2. **7 skills is the right count; ~33.9k tokens of skill prose at 22–29
   words/sentence is the problem.** Move rationale to `references/`, keep
   procedure in `SKILL.md`.

**Do NOT rewrite the scorers.** They audited clean (statistics verified
correct, HTML escaping hardened, multiset trajectory semantics, regex
watchdog, authz honest-degradation). Step 3 touched only the three named bugs
and their siblings — hold that line.

---

## 3. Plan — one step per session, in order

Each step is sized for a single low-usage session. Mark done as you go.

- [x] **Step 0 — Baseline commit.** **DONE 2026-09-07 — `6c7d286`.**
- [x] **Step 1 — Splits are a field, not a directory.** **DONE 2026-09-07 —
      `f03c960`.** All five directory-selecting consumers rewritten; the
      `datasets/*/` mentions left are deliberate.
- [x] **Step 2 — One path convention.** **DONE 2026-09-07 — `05804ef`.**
      `${CLAUDE_PLUGIN_ROOT}/...` cross-skill, relative links inside a skill, app
      and state paths bare on purpose (README §"Path convention"). **The audit's
      offender list was wrong in both directions** — grep, don't work a list.

- [x] **Step 3 — The three scorer bugs + a regression test each.**
      **DONE 2026-09-08 — `0d2aa91`.** Each was a class with siblings, and the
      hunt found more than the audit named: `sub_mde_keep`; NFC in **six**
      scorers via `_common.nfc` (regex stays asymmetric on purpose);
      `--oos-route` and `score_authz --id-pattern`; `stringify`'s `ensure_ascii`.

- [x] **Step 4 — Tighten `validate_cases.py`.**
      **DONE 2026-09-08 — `201aa2a`.** The severity line, in the docstring and
      case-format.md: **ERROR = a claim nothing backs; WARN = a suite thinner
      than the guidance recommends.** Four new ERRORs (`missing_split`,
      `_test_type`, `_template_id` — the KEY'S PRESENCE — `_metamorphic_parent`)
      + required `--capabilities`. The shipped 12-case field test: 0 → 36, all true.

- [x] **Step 5a — Spec `run_cases.py`** (design only). **DONE 2026-09-08 —
      `77d3778`.** `agent-eval/docs/runner-contract.md`. The missing-jsonl
      failure is closed structurally (§9): both files are **regenerated from the
      case dirs after every case**, and finalize exits 6 rather than write ok.

- [x] **Step 5b-i — `run_cases.py`, the skeleton** (no scoring). **DONE
      2026-09-08 — `c9bc4f7`.** §§2–4, 6–8, 11 + 64 tests on a real fake HTTP
      app; **all 9 open decisions answered and the contract rewritten in the same
      commit**. It could not be implemented as written — nothing said how an HTTP
      request is BUILT — so `invocation` gained five declared fields, none guessed.

- [x] **Step 5b-ii — the scoring half.** **DONE 2026-09-09 — `5e573c9`.** All
      of §5, the run-level scorers, §9(c) and `--verify`; 551 tests (+38). The
      `REQUIRED_*` tables are **exec'd out of the contract file**, not restated.
      Four more contract silences became ADAPTER DECLARATIONS, and its one real
      bug (§9(c)3 failing any run that skips a case) is fixed.

- [x] **Step 5c — Rewire `run/SKILL.md`** around the runner.
      **DONE 2026-09-09 — `676ab23`.** 318 → **164**. §§1–3 were deleted whole
      (the runner's job verbatim), but the replacement had to ADD what nothing
      wrote — **§1, building `plan.json`** — and answer each of the seven exit
      codes (**6** = a missing artifact, so the run is neither quotable nor a
      baseline; **7** = artifacts complete, numbers not). Also: **`--layer X`
      finally means something**, the skill runs `validate_cases.py`, and both
      writers of the N=5 holdout count point at the `.jsonl` sidecar. §0/§1/§4
      kept their numbers; one doc-pinning test changed on purpose.

- [x] **Step 6a — Slim `generate/SKILL.md`.**
      **DONE 2026-09-09 — `86b602e`.** 401 → **159** against a ≤120 target; mean
      sentence 16.0 → 12.7. Rationale to a new `references/generation-method.md`;
      `suite-sizing.md`, `case-format.md`, `agents/test-generator.md` already
      OWNED the budget table, `expect.*`, id opacity, split-as-a-field. **Why
      159:** procedure residue; `run-modes.md` cites §4. Numbers kept.

- [x] **Step 6b — Slim `analyze/SKILL.md`.**
      **DONE 2026-09-09 — `d6bf421`.** 252 → **167**, mean sentence 20.4 → 15.6.
      **No new reference file: every paragraph that left had an owner already** —
      the cadence rule and `--glob`'s seal hazard to `annotation-ux.md`, most of
      `--label` and all of "Rubric editing" to `rubric-format.md` /
      `agents/judge.md` / `profile-schema.md`, the sidecar argument to
      runner-contract §6, the first-run re-argument to `run` §4. Cited by FLAG
      name, never §number, so nothing renumbered; no test pins it. **Why 167:**
      procedure residue (empty-state branches, cluster recipe, 20/40/40 labeling,
      reseal steps, the re-validate command AUDIT §187 says it must own).

- [x] **Step 6c — Slim `discover/SKILL.md`.**
      **DONE 2026-09-09 — `6268cc3`.** 235 → **160**; mean sentence 24.6 → 14.3,
      longest 128 → 35. Ownership pass first, as in 6a/6b: `profile-schema.md`
      already owned every consequence of `architecture.kind`, all of
      `record_id_pattern`'s rationale, and **the whole capability-matrix table**
      — that section is now "work down the schema's block" plus the three calls
      the schema leaves open; `adapter-contract.md` owned the side-effect classes
      and the oracle/seed/reset mechanics. **One new file** for the rationale
      nothing owned: `references/topology-detection.md` (59), the three-signal
      fingerprint catalogue. **§§1–8 kept their numbers** (`generate` §1 → §2's
      five spellings, `optimize` → §6, `dotnet.md` → step 1); no test reads this
      skill. `dotnet.md`'s "step 4" for the patch offer was already stale — now
      step 5. **Why 160:** procedure residue — white/gray/black, the
      read-only/declined-patch path the field test asked for, the patch offers,
      the interview, `--diff`.

- [ ] **Step 6d — the rest of the slimming.** `discover` 160, `analyze` 167,
      `run` 164, `generate` 159, `help` 124; `optimize` 88 and `start` 80 pass.
      **Four skills have now landed at 159–167 for the same reason** (procedure
      residue, each checked against its owners) — so decide whether **≤120 is the
      right target at all** before slimming a fifth. `help` escapes that
      question: it is a delete-or-keep call (README pointer?), not a slimming
      one. Then dedupe: one owning document per load-bearing fact
      (`reports/baseline.json` in 8 files, `PROVISIONAL` 8, `state_location` 6).

- [ ] **Step 7 — Decide multi-turn.** Promised in `adapter-contract.md`,
      `case-format.md` and a `validate_cases.py` warning, but
      `normalize_trace.py` has no session/turn/agent attribution, no scorer is
      turn-aware, and `agents/simulated-user.md` is referenced by **zero** skills
      (and declares `tools: Read`, so it cannot call the app). Either build it
      (`REVIEW-2026-08-08.md` §4 item 2 — it also unblocks authz scoping,
      per-agent loops, cost attribution) or mark it reserved and delete the
      orphan agent + the unactionable warning.

- [ ] **Step 8 — Housekeeping.** Regenerate the worked example in
      `field-test-qa/.agent-eval/` (36 validation errors since Step 4; the
      migration is mechanical, see `201aa2a`) or label it "pre-2026-08 layout,
      kept for the migration doc". Add CI + `pyproject.toml`
      (`REVIEW-2026-08-08.md` §3). Reconcile `.tool-versions` (pins 3.11) against
      the claimed 3.9 floor. Drop or relabel `micro_f1` (≡ matrix accuracy).

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
  - `ruff check --config ruff.toml .`
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
- Reproducing any audit finding: every one has a copy-pasteable repro in
  `AUDIT-2026-09-06.md`. Re-run rather than re-deriving.
