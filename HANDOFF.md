# agent-eval — session handoff / working state

**Purpose:** this file is the single place a new session reads to pick up work
without re-deriving anything. It is deliberately small. The *findings* live in
`AUDIT-2026-09-06.md`; this file holds **state, plan, and gotchas only**.

**Last updated:** 2026-09-08 (Step 5b-i done — runner skeleton, no scoring)
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
- **Health:** 513 tests pass (~2min), ruff clean, all 14 CLI scripts `--help`
  rc=0, Python 3.9 compatible (re-verified with `uv`). Verified 2026-09-08.

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
   silently dropped required outputs (proof: the last shipped run is missing
   `verdicts.jsonl` and `verdicts_for_stats.jsonl`). **Being fixed now**:
   `docs/runner-contract.md` is the spec, `scripts/run_cases.py` is the engine.
   Step 5b-i shipped everything but scoring; 5b-ii wires the scorers and closes
   the loop. Until 5b-ii lands the runner is not usable for a real run, and
   `run/SKILL.md` still describes the hand-orchestrated flow (that is 5c).
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
      **DONE 2026-09-07 — `6c7d286`.** 54 paths, +4547/-632. The shipped field
      test went in without its per-case dirs: `reports/.gitignore` ignores
      `*/cases/`.

- [x] **Step 1 — Fix the splits field-vs-directory break.**
      **DONE 2026-09-07 — `f03c960`.** Picked the field (`split: [full, smoke]`)
      and rewrote all five consumers that selected by directory. The only
      `datasets/*/` mentions left are generate's rationale for NOT using
      directories.

- [x] **Step 2 — One path convention.**
      **DONE 2026-09-07 — `05804ef`.** 18 files, +434/-388.
      `${CLAUDE_PLUGIN_ROOT}/...` for anything executed or read cross-skill;
      relative links for a `SKILL.md` into its own `references/`. App/state
      paths stay bare on purpose and README §"Path convention" says so.
      **The audit's offender list was wrong in both directions:** it named 6
      files, 17 needed changes, and one it named has zero path references.
      Grep; do not work down the list.

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

- [x] **Step 5a — Spec `run_cases.py`** (design only, no code).
      **DONE 2026-09-08 — `77d3778`.** `agent-eval/docs/runner-contract.md`: one
      JSON `plan.json` in (§2), the output tree (§6), every file's shape (§10),
      7 exit codes (§11), what stays in the skill (§13). The
      missing-`verdicts.jsonl` failure is closed structurally (§9) — both jsonl
      files are **regenerated from the case dirs after every case**, a declared
      `REQUIRED_*` table names every artifact, and a finalize check exits 6
      instead of writing `status: "ok"`; `--verify` re-asserts it on any old run
      dir. **Read the 9 Open decisions before 5b** — D2 (trace-less routing) and
      D3 (who runs `stats.py`) change scope. No code, `run/SKILL.md` untouched.

- [x] **Step 5b-i — `run_cases.py`, the skeleton** (no scoring).
      **DONE 2026-09-08 — `c9bc4f7`.** `scripts/run_cases.py` + 64 tests driving
      a real fake HTTP app in a thread. Live: §§2–4, 6–8, 11 and the
      `REQUIRED_ALWAYS`/`REQUIRED_PER_CASE` half of §9. **Every layer records
      `unscored`, reason "scoring not wired yet (5b-ii)"**, so every non-infra
      case rolls up to `unscored` and `verdicts_for_stats.jsonl` is legitimately
      empty. **All 9 open decisions answered; `runner-contract.md` rewritten in
      the same commit** — its "Open decisions" section is now a confirmed
      record. Three changed code: D5 refuses `max_concurrency > 1` (exit 2), D6
      implements `function` mode too, D8 makes the holdout ledger a **`.jsonl`
      sidecar** (stdlib-only: appending a JSON line to the YAML dataset metadata
      would corrupt it). D2/D3 are specced and land with the scoring half.
      **The contract could not be implemented as written** — it never said how
      an HTTP request is BUILT. `adapter-contract.md`'s `invocation` gained
      `endpoint`, `request_body`/`response_body` templates, `identity_map`,
      `health_check`, `route_from_status`; all declared, none guessed. Full
      accounting in the commit message.

- [ ] **Step 5b-ii — the scoring half.** §5's layer table (9 scorer
      shell-outs), run-level `score_routing.py` and `reduce_repeats.py`, D2's
      `route_from_status`, D3's `--baseline-verdicts` → `comparison.json` (with
      D7's k-mismatch check), then finalize's `REQUIRED_IF` conditions, §9(c)
      checks 3–4, and `--verify`. Two tests matter most: `REQUIRED_*` asserted
      against the contract's §6, and `--verify` on
      `field-test-qa/.agent-eval/reports/smoke-20260818T183920Z/` exiting 6 and
      naming both missing files — the audit finding as a regression test.

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
  - `python3 -m unittest discover -s tests` — 513 tests, ~2min. This is the
    blessed command. (`tests/test_run_cases.py` is most of the new time: it
    starts a real HTTP server per test and one test kills a runner mid-run.)
  - `ruff check --config ruff.toml .`
  - `uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q`
    — the only conclusive 3.9 floor check. Run before any release.
- **`validate_cases.py` takes JSON, not YAML**, deliberately — read its
  docstring before "fixing" that. Convert with the caller's own YAML parser.
  **`run_cases.py` takes JSON for the same reason** (`--plan`), and the skill
  doing the YAML→JSON conversion *is* the run's parse, not a second opinion.
- **The runner's spec is `agent-eval/docs/runner-contract.md` and it wins.**
  Its nine design decisions were confirmed 2026-09-08 and the section that used
  to hold them is now a record of the answers with the trade-offs — do not
  reopen one without reading it. Where the code and that file disagree, the
  file is right.
- **The holdout ledger is a `.jsonl` sidecar**, not the YAML dataset metadata:
  a stdlib-only runner cannot append a JSON line to a YAML mapping without
  corrupting it. `run/SKILL.md` §4 and `analyze/SKILL.md` `--unseal` still say
  "dataset metadata" and need pointing at the sidecar in 5c/Step 6.
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
