# agent-eval — session handoff / working state

**Purpose:** this file is the single place a new session reads to pick up work
without re-deriving anything. It is deliberately small. The *findings* live in
`AUDIT-2026-09-06.md`; this file holds **state, plan, and gotchas only**.

**Last updated:** 2026-09-08 (Step 4 done — validate_cases.py tightened)
**Update rule:** whenever you finish a step, edit §3 (mark it done, add what you
actually did + the commit sha) and bump the date above. Do not let this file
grow past ~200 lines; move detail into the audit doc or a commit message.

---

## 1. Where things stand

- **Repo:** `/path/to/workspace`
- **Plugin:** `agent-eval/` (v0.1.0). Root also holds `research/`,
  `field-test-qa/`, and the review docs.
- **Branch:** `run-layout-consistency`. Main branch is `main`.
- **Working tree is CLEAN** as of Step 0. The whole prior review wave is
  committed as the baseline `6c7d286`; every later diff is now readable
  against it.
- **Health:** 449 tests pass, ruff clean, all 13 CLI scripts `--help` rc=0,
  Python 3.9 compatible (re-verified with `uv`). Verified 2026-09-08.

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

1. **There is no execution engine.** Every script in `scripts/` is a pure
   JSON-in/JSON-out scorer. `run/SKILL.md` §2 "Execute" is hand-orchestrated by
   the LLM every run → not reproducible, expensive, and it silently drops
   required outputs (proof: the last shipped run is missing `verdicts.jsonl`
   and `verdicts_for_stats.jsonl`). Fix = build `run_cases.py`.
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

- [x] **Step 0 — Commit the current dirty tree** as a readable baseline.
      **DONE 2026-09-07 — `6c7d286`.** Single commit, 54 paths, +4547/-632.
      Re-verified the tree before committing: 399 tests OK (~36s), ruff clean.
      The shipped field-test run went in without its per-case dirs, since
      `field-test-qa/.agent-eval/reports/.gitignore` ignores `*/cases/`.
      Nothing else changed.

- [x] **Step 1 — Fix the splits field-vs-directory break.**
      **DONE 2026-09-07 — `f03c960`.** Picked the field (`split: [full, smoke]`,
      generate's newer decision, already enforced by `validate_cases.py`
      including the holdout/full mutual exclusion) and rewrote every consumer
      that selected by directory. 5 files, +38/-25:
      `run/references/run-modes.md` (mode table + all four `**Selection**`
      bullets), `run/SKILL.md` (mode table, canary bullet, plus a new
      three-sentence rule in §0 naming `case-format.md` as the owning doc),
      `optimize/SKILL.md` step 4, `help/SKILL.md` "Where things live",
      `analyze/SKILL.md` (the viewer-staging filter and the `--unseal` reseal
      procedure, which said "move viewed cases into full/" — now a `split`
      rewrite). Only `datasets/*/` mentions left are in generate's own
      rationale for *not* using directories. 399 tests OK, ruff clean.

- [x] **Step 2 — One path convention.**
      **DONE 2026-09-07 — `05804ef`.** 18 files, +434/-388. The rule as
      planned: `${CLAUDE_PLUGIN_ROOT}/...` for anything executed or read
      cross-skill; relative markdown links for a `SKILL.md` pointing into its
      own `references/`. All 7 `@references/...` uses converted. App/state
      paths (`datasets/`, `reports/`, `templates/`, the state dir's own
      `scripts/`) stay bare on purpose; `README.md` and script docstrings stay
      repo-relative, and README §"Path convention" now says all of this.
      **The audit's offender list was wrong in both directions again:** it
      named 6 files, 17 needed changes, and one it named
      (`agents/trace-analyzer.md`) has zero path references. Full accounting
      in the commit message. Grep; do not work down the list.
      399 tests OK, ruff clean.

- [x] **Step 3 — The three scorer bugs, each with a regression test.**
      **DONE 2026-09-08 — `0d2aa91`.** 13 files, +837/-49. All three audit
      repros re-ran failing before the fix and clean after. Each was a class
      with siblings, and the sibling hunt again found more than the audit
      named: (a) `sub_mde_keep` in `stats.py`, surfaced in `optimize` §5 —
      no siblings, it is the only script that decides keep/revert. (b) NFC
      normalization in **six** scorers, not one (`score_answer`, `_args`,
      `_authz`, `_execution`, `_routing`, `trajectory_match`) via a new shared
      `_common.nfc`; regex stays asymmetric on purpose (haystack yes, pattern
      never) and every regex check now says so. Found en route: `stringify`
      used `ensure_ascii=True`, so provenance could never match a non-ASCII
      value. (c) `--oos-route` exits 2 listing the labels present; its sibling
      is `score_authz --id-pattern`, silent in the same two directions. The
      other 22 CLI args were swept — `--trace-id`, `--glob` and `--k` already
      validate. Full accounting in the commit message. 438 tests pass
      (39 new), ruff clean, 3.9 floor re-verified.

- [x] **Step 4 — Tighten `validate_cases.py`.**
      **DONE 2026-09-08 — `201aa2a`.** 5 files, +390/-42. The severity line,
      written into the module docstring and case-format.md so it can't be
      re-created one level up: **ERROR = a claim nothing backs; WARN = a suite
      thinner than the guidance recommends.** New ERRORs: `missing_split`,
      `missing_test_type`, `missing_template_id` (checks the KEY'S PRESENCE —
      absent = nobody decided, `null` = declared one-off) and
      `missing_metamorphic_parent`, without which `test_type: INV` on an
      ordinary case would buy the new floor's credit for free. New WARNs:
      `no_metamorphic_coverage` and `all_one_off`. `--capabilities` is now
      required, with an explicit `--no-capabilities` that emits
      `capabilities_unchecked` and is therefore refused under `--strict`.
      **Re-validated the shipped 12-case field test:** before exit 0 / 0
      errors, after exit 1 / 36 — all true positives of the pre-2026-08 layout
      (no `split` field at all); a mechanically migrated copy validates
      **clean**, so no rule turns a good case red. Also wired the validator
      into `analyze/SKILL.md`, whose three branches all edit cases.
      449 tests (11 new), ruff clean, 3.9 floor re-verified.

- [ ] **Step 5a — Spec `run_cases.py`** (design only, no code). Write
      `agent-eval/docs/runner-contract.md`: inputs (adapter config + case list
      **as JSON**, using the same "the caller converts YAML" argument
      `validate_cases.py` already makes in its docstring, so stdlib-only
      survives), execution semantics (serial default, k repeats, bounded
      retries, infra taxonomy, resume-from-last-complete-case), the exact
      output tree, and the exit-code contract. Get this reviewed before coding.

- [ ] **Step 5b — Implement `run_cases.py`** + tests. It must emit, per
      `run/SKILL.md` §2/§4: `cases/<case-id>/{request,response,verdict}.json`
      written atomically (temp-then-rename), `manifest.yaml`, `results.json`,
      `verdicts.jsonl`, `verdicts_for_stats.jsonl`. Then shell out to the
      existing scorers — do not reimplement any scoring.

- [ ] **Step 5c — Rewire `run/SKILL.md`** around the runner. Target: 305 lines
      → ~60. The skill should decide *what to run and how to read the result*,
      not *how to execute*.

- [ ] **Step 6 — Slim the skills.** Target ≤120 lines and ≤18 words/sentence
      per `SKILL.md`; rationale moves to `references/`. Also dedupe: one owning
      document per load-bearing fact (`reports/baseline.json` appears in 7
      files, `PROVISIONAL` in 8, `state_location` in 8). Consider making `help`
      a thin pointer to the README.

- [ ] **Step 7 — Decide multi-turn.** It is promised in `adapter-contract.md`,
      `case-format.md`, and a `validate_cases.py` warning, but
      `normalize_trace.py` has no session/turn/agent attribution, no scorer is
      turn-aware, and `agents/simulated-user.md` is referenced by **zero**
      skills (and declares `tools: Read`, so it cannot call the app). Either
      build it (`REVIEW-2026-08-08.md` §4 item 2: add `span_id`/`parent`/owning
      agent to every tool call + a session envelope — it also unblocks authz
      scoping, per-agent loops, and cost attribution) or mark it reserved and
      remove the orphaned agent + the unactionable warning.

- [ ] **Step 8 — Housekeeping.** Regenerate the worked example in
      `field-test-qa/.agent-eval/` (it violates three current rules: ids encode
      unit+category, splits are directories, every case is
      `review.by: test-generator`, and since Step 4 it fails validation with
      36 errors — the migration is mechanical, see `201aa2a`) or label it "pre-2026-08 layout, kept for
      the migration doc". Add CI + `pyproject.toml` (`REVIEW-2026-08-08.md` §3,
      still open). Reconcile `.tool-versions` (pins 3.11) against the claimed
      3.9 floor. Drop or relabel `micro_f1` (≡ matrix accuracy).

- [ ] **Step 9 — Reopen `REVIEW-2026-08-08.md` §§4–5**: cost/latency as
      first-class continuous metrics, `score_agreement.py` to close the judge
      loop (the viewer exports annotation JSONL that nothing reads back), the
      run-history/longitudinal layer, and the RAG/retrieval layer.

---

## 4. Gotchas a new session will otherwise rediscover the hard way

- **Commands** (run from `agent-eval/`):
  - `python3 -m unittest discover -s tests` — 399 tests, ~32s. This is the
    blessed command.
  - `ruff check --config ruff.toml .`
  - `uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q`
    — the only conclusive 3.9 floor check. Run before any release.
- **`validate_cases.py` takes JSON, not YAML**, deliberately — read its
  docstring before "fixing" that. Convert with the caller's own YAML parser.
- **`__oos__` is `score_routing.py`'s canonical internal OOS label**, not a
  data value. `--oos-route <name>` maps the profile's OOS route name onto it.
  (I initially misread the shipped report as inconsistent; it is not.)
- **The scorers' error contract:** errors go to **stdout** (not stderr) as
  JSON, exit 2 on malformed input. Deliberate — see `_common.die()`.
- **`field-test-qa/.agent-eval/` is in a mixed layout** (`baselines/` + `runs/`
  old, `reports/` new, no `reports/baseline.json`) — the exact ambiguous state
  `run/SKILL.md` §4 says must halt and demand migration. Useful as a test
  fixture for that branch; confusing if you mistake it for a good example.
- **The app under test in the field test is READ-ONLY** and lives outside this
  repo (`/path/to/reference-app`). Never modify it.
- Reproducing any audit finding: every one has a copy-pasteable repro in
  `AUDIT-2026-09-06.md`. Re-run rather than re-deriving.
