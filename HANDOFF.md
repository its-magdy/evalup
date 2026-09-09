# agent-eval — session handoff / working state

**Purpose:** this file is the single place a new session reads to pick up work
without re-deriving anything. It is deliberately small. The *findings* live in
`AUDIT-2026-09-06.md`; this file holds **state, plan, and gotchas only**.

**Last updated:** 2026-09-09 (Step 5c done — the skill is wired to the runner)
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
   silently dropped required outputs (proof: the last shipped run is missing
   `verdicts.jsonl` and `verdicts_for_stats.jsonl`). **Fixed, as of Step 5c**:
   `docs/runner-contract.md` is the spec, `scripts/run_cases.py` is the engine,
   and the skill now builds a plan and reads a result instead of executing.
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
- [x] **Step 1 — Splits are a field, not a directory.**
      **DONE 2026-09-07 — `f03c960`.** All five directory-selecting consumers
      rewritten; the `datasets/*/` mentions left are deliberate.
- [x] **Step 2 — One path convention.**
      **DONE 2026-09-07 — `05804ef`.** `${CLAUDE_PLUGIN_ROOT}/...` cross-skill,
      relative links inside a skill, app/state paths bare on purpose (README
      §"Path convention"). **The audit's offender list was wrong in both
      directions** — grep, do not work down a list.

- [x] **Step 3 — The three scorer bugs, each with a regression test.**
      **DONE 2026-09-08 — `0d2aa91`.** Each was a class with siblings and the
      hunt found more than the audit named: `sub_mde_keep` in `stats.py`; NFC
      normalization in **six** scorers via a new shared `_common.nfc` (regex
      stays asymmetric on purpose: haystack yes, pattern never); `--oos-route`
      and its silent sibling `score_authz --id-pattern`; `stringify`'s
      `ensure_ascii=True`, which made provenance unmatchable on non-ASCII.

- [x] **Step 4 — Tighten `validate_cases.py`.**
      **DONE 2026-09-08 — `201aa2a`.** The severity line, in the module
      docstring and case-format.md: **ERROR = a claim nothing backs; WARN = a
      suite thinner than the guidance recommends.** New ERRORs:
      `missing_split`, `missing_test_type`, `missing_template_id` (checks the
      KEY'S PRESENCE — absent = nobody decided, `null` = declared one-off),
      `missing_metamorphic_parent`. `--capabilities` is now required
      (`--no-capabilities` is refused under `--strict`). **The shipped 12-case
      field test:** 0 errors before, 36 after — all true positives of the
      pre-2026-08 layout; a migrated copy validates clean.

- [x] **Step 5a — Spec `run_cases.py`** (design only).
      **DONE 2026-09-08 — `77d3778`.** `agent-eval/docs/runner-contract.md`.
      The missing-`verdicts.jsonl` failure is closed structurally (§9): both
      jsonl files are **regenerated from the case dirs after every case**, and
      a finalize check exits 6 instead of writing `status: "ok"`.

- [x] **Step 5b-i — `run_cases.py`, the skeleton** (no scoring).
      **DONE 2026-09-08 — `c9bc4f7`.** §§2–4, 6–8, 11 + 64 tests driving a real
      fake HTTP app in a thread. **All 9 open decisions answered and the
      contract rewritten in the same commit** — D5 refuses `max_concurrency >
      1`, D6 implements `function` mode, D8 makes the ledger a `.jsonl`
      sidecar. **The contract could not be implemented as written**: it never
      said how an HTTP request is BUILT, so `adapter-contract.md`'s
      `invocation` gained `endpoint`, `request_body`/`response_body`,
      `identity_map`, `health_check`, `route_from_status` — all declared,
      none guessed.

- [x] **Step 5b-ii — the scoring half.**
      **DONE 2026-09-09 — `5e573c9`.** All of §5, the run-level scorers, §9(c)'s
      four checks and `--verify`; 551 tests (+38). The two that matter: the
      `REQUIRED_*` tables are **exec'd and parsed out of the contract file**
      rather than restated, and `--verify` on the shipped run exits 6 naming
      both missing jsonl files. **The contract was silent in four places and
      each is now answered by an ADAPTER DECLARATION, never by inference**
      (`route_from_response`, `clarify_from_response`, `result_from_tool`,
      `result_from_response`, `result_pattern`, new in `adapter-contract.md`) —
      plus the one real contract bug, §9(c)3's `len(verdicts.jsonl) == cases
      attempted`, which fails every run that skips a case.

- [x] **Step 5c — Rewire `run/SKILL.md`** around the runner.
      **DONE 2026-09-09 — `676ab23`.** 318 → **164** lines, not the ~60
      estimated: §§1–3 were deleted whole (the runner's job verbatim), but the
      replacement had to ADD the part nothing wrote — **§1, building
      `plan.json`** — and give each of the seven exit codes a written response
      (**6** = an artifact is missing, so the run is neither quotable nor a
      baseline; **7** = artifacts complete, numbers not). §4 keeps the baseline
      decisions the runner refuses to make; §5 keeps `report.md` + the
      failure-cluster narrative and still names both jsonl files. Also here:
      **`--layer X` finally means something** (it disables the other
      capability-matrix layers), the skill now runs `validate_cases.py`
      (contract §2 assigns it there; nothing did it), and **both writers of the
      N=5 holdout count point at the `.jsonl` sidecar** with "count the lines"
      as the rule. Contract §2's example ledger path was a `.yaml` its own §6
      exits 2 on — fixed in the same commit. Six documents cite this skill by
      §number: §0/§1/§4 kept theirs, the rest were retargeted by grep. One
      doc-pinning test changed **on purpose** (`--oos-route`'s precondition is
      now asserted against the contract, which owns that check since 5b-ii).
      Remaining compression is Step 6's: §4's branches and §5's report rules
      belong in `references/`.

- [ ] **Step 6 — Slim the skills.** Target ≤120 lines and ≤18 words/sentence
      per `SKILL.md`; rationale moves to `references/` (start with `run` §§4–5,
      left at 164 lines by 5c). Also dedupe: one owning document per
      load-bearing fact (`reports/baseline.json` in 7 files, `PROVISIONAL` in
      8, `state_location` in 8). Consider making `help` a README pointer.

- [ ] **Step 7 — Decide multi-turn.** Promised in `adapter-contract.md`,
      `case-format.md` and a `validate_cases.py` warning, but
      `normalize_trace.py` has no session/turn/agent attribution, no scorer is
      turn-aware, and `agents/simulated-user.md` is referenced by **zero**
      skills (and declares `tools: Read`, so it cannot call the app). Either
      build it (`REVIEW-2026-08-08.md` §4 item 2: `span_id`/`parent`/owning
      agent per tool call + a session envelope — it also unblocks authz
      scoping, per-agent loops and cost attribution) or mark it reserved and
      remove the orphan agent + the unactionable warning.

- [ ] **Step 8 — Housekeeping.** Regenerate the worked example in
      `field-test-qa/.agent-eval/` (three current rules broken: ids encode
      unit+category, splits are directories, every case is
      `review.by: test-generator`; since Step 4 it fails validation with 36
      errors — the migration is mechanical, see `201aa2a`) or label it
      "pre-2026-08 layout, kept for the migration doc". Add CI +
      `pyproject.toml` (`REVIEW-2026-08-08.md` §3). Reconcile `.tool-versions`
      (pins 3.11) against the claimed 3.9 floor. Drop or relabel `micro_f1`
      (≡ matrix accuracy).

- [ ] **Step 9 — Reopen `REVIEW-2026-08-08.md` §§4–5**: cost/latency as
      first-class metrics, `score_agreement.py` to close the judge loop (the
      viewer exports annotation JSONL nothing reads back), the longitudinal
      run-history layer, and the RAG/retrieval layer.

---

## 4. Gotchas a new session will otherwise rediscover the hard way

- **Commands** (run from `agent-eval/`):
  - `python3 -m unittest discover -s tests` — 551 tests, ~2min. This is the
    blessed command. (`tests/test_run_cases.py` is most of the new time: it
    starts a real HTTP server per test and one test kills a runner mid-run.)
  - `ruff check --config ruff.toml .`
  - `uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q`
    — the only conclusive 3.9 floor check. Run before any release.
- **`validate_cases.py` and `run_cases.py` take JSON, not YAML**, deliberately
  — read the docstrings before "fixing" that. The skill's YAML→JSON conversion
  *is* the run's parse, not a second opinion.
- **The runner's spec is `agent-eval/docs/runner-contract.md` and it wins.**
  Its nine decisions were confirmed 2026-09-08 and that section is now a record
  of the answers with their trade-offs — do not reopen one without reading it.
  **Three tests read the file at run time** (they exec §9(b)'s python block,
  parse §6's `# only when:` markers, and pin `--oos-route`'s precondition), so
  an edit fails the suite until the code follows — deliberate, not brittle.
- **The holdout ledger is a `.jsonl` sidecar** (`datasets/holdout-looks.jsonl`,
  from the plan's `paths.holdout_ledger`), not the YAML dataset metadata — a
  stdlib-only runner would corrupt the YAML. Both writers say so as of 5c, and
  the running total is a LINE COUNT, not one skill's entries.
- **`__oos__` is `score_routing.py`'s canonical internal OOS label**, not a
  data value. `--oos-route <name>` maps the profile's route name onto it.
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
