# agent-eval — session handoff / working state

**Purpose:** this file is the single place a new session reads to pick up work
without re-deriving anything. It is deliberately small. The *findings* live in
`AUDIT-2026-09-06.md`; this file holds **state, plan, and gotchas only**.

**Last updated:** 2026-09-10 (Step 9c DONE — **the remediation wave is CLOSED**)
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
- **Health:** **687** tests pass (~2min), ruff clean, all **17** CLI scripts
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
   silently dropped required outputs. **Fixed as of Step 5c**: spec in
   `docs/runner-contract.md`, engine in `scripts/run_cases.py`; the skill builds
   a plan and reads a result.
2. **7 skills is the right count; the weight was the problem.** Rationale to
   `references/`, procedure in `SKILL.md`. **Closed** — what `run` loads before
   a case executes fell ~12,800 → ~4,891 (`AUDIT` §1 addendum); total prose
   barely moved, which is fine: **≤120 lines is retired.**

**Do NOT rewrite the scorers.** They audited clean (statistics verified
correct, HTML escaping hardened, multiset trajectory semantics, regex
watchdog, authz honest-degradation). Step 3 touched only the three named bugs
and their siblings — hold that line.

---

## 3. Plan — one step per session, in order

Each step is sized for a single low-usage session. Mark done as you go.

- [x] **Steps 0–4 — baseline; splits are a field; one path convention; the
      three scorer bugs; tighten `validate_cases.py`. DONE 2026-09-07/08 —
      `6c7d286`, `f03c960`, `05804ef`, `0d2aa91`, `201aa2a`.** Paths:
      `${CLAUDE_PLUGIN_ROOT}/...` cross-skill, relative inside a skill,
      app/state bare (README §"Path convention"). **The audit's offender list
      was wrong in both directions** — grep, don't work a list. Each bug was a
      class with siblings (NFC in **six** scorers, via `_common.nfc`). The
      severity line — **ERROR = a claim nothing backs; WARN = thinner than the
      guidance recommends** — is in the docstring + `case-format.md`.

- [x] **Steps 5a–5c — spec and build `run_cases.py`, then rewire
      `run/SKILL.md`. DONE 2026-09-08/09 — `77d3778`, `c9bc4f7`, `5e573c9`,
      `676ab23`.** Body in `AUDIT` §2 + the contract: **it was not implementable
      as written**; all nine decisions were answered, none guessed. The skill
      went 318 → **164** and had to ADD **§1, building `plan.json`**.

- [x] **Steps 6a–6e — slim the skills; the two decisions; dedupe. Step 6
      CLOSED. DONE 2026-09-09 — `86b602e`, `d6bf421`, `6268cc3`, `4ede98f`,
      `a22ce8e`.** Body in `AUDIT` §1's re-measured block + addendum and §4.
      **D1 — `≤120 lines` is RETIRED; D2 — `help` loses its body.** Only `help`
      duplicated anything: **a MENTION count is not a duplication count**.

- [x] **Step 7 — Decide multi-turn: RESERVED. DONE 2026-09-10 — `337181a`.**
      Body in `AUDIT` §8's RESOLVED block; Step 10 rests on its precedent.

- [x] **Steps 8a–8b — packaging; stale artifacts + `micro_f1`. Step 8 CLOSED.
      DONE 2026-09-10 — `526e495`, `593aad1`.** Bodies in `AUDIT` §"RESOLVED …
      (Step 8a)"/"(Step 8b)". Two facts §4 rests on: **CI was written knowingly
      as a spec**, and **`ruff.toml` did NOT fold in**.

**`REVIEW` §4 has TEN gaps and the old Step 9 bullet named four.** `AUDIT`
§"Step 9 recon" has the disposition table; six were in no step.

- [x] **Step 9a — close the judge loop (`REVIEW` §4.4). DONE 2026-09-10 —
      `eebd318`.** `score_agreement.py` **+ a gate change in `run_cases.py`**
      (§4's gotcha). Body in `AUDIT` §"RESOLVED … (Step 9a)".
- [x] **Step 10 — RESERVE `REVIEW` §4's items 2, 5, 7, 8 and 10. DONE
      2026-09-10 — `d22bcaa`.** All five reserved, none built; body in `AUDIT`
      §"RESOLVED … (Step 10)". **§4.7 was the live one** and its rollup half hit
      every case, not just `state` — see §4's `http` gotcha. **§4.2 is a doc
      correction; §4.5 is a non-finding** (nothing declares a per-layer
      statistic, so there is nothing to reserve). 4 WARNs + 1 ERROR.
- [x] **Step 9b — run history (`REVIEW` §4.9). DONE 2026-09-10 — `2bbb40a`.**
      `scripts/run_history.py` + 58 tests, named in `analyze/SKILL.md`. Body in
      `AUDIT` §"RESOLVED … (Step 9b)". **"All data already on disk" was WRONG a
      fifth time; the gap was COMPARABILITY** (`manifest.yaml`-only keys), **no
      flakiness ledger exists**, and **nothing declared a trend view**, so 9b
      had nothing to reserve.
- [x] **Step 9c — cost/latency (`REVIEW` §4.3). Step 9 and the WHOLE
      REMEDIATION WAVE CLOSED. DONE 2026-09-10 — `SHA9C`.**
      `scripts/score_cost.py` + 35 tests, contract **§5.7**, named in
      `analyze/SKILL.md`. Body in `AUDIT` §"RESOLVED … (Step 9c)". Half build,
      half reserve: **`cost_latency` is not a per-case layer and never will be**
      (§5.7 + a test), while the *capability* flag now has a real reader.
      **§4.3's bootstrap is DECLINED by name** — see §4's gotcha. Finding six
      (the recon's `stages.json`) is corrected in place in `AUDIT`'s recon.

**The plan is DONE. What is deliberately OUT of it** — decided, not dropped:
**§4.6 RAG/retrieval** and **§5's five "ideas worth stealing"** are greenfield
additions, not remediation (grep returns zero files; `AUDIT` §"Step 9 recon"
DEMOTED them) — a new wave if anyone wants one. **Everything Steps 7 and 10
RESERVED** (multi-turn, `expect.state`, `seed_state`, `available_tools`,
`excluded_tools`, agent-scoped expectations, streaming/TTFT) plus 9c's
**cached-token pricing** each wait for the scorer that would read them; do not
wire one up without one. **The one genuine gap left is `REVIEW` §10's
"regenerate the worked example": the plugin ships NO conforming end-to-end
example** (Step 8b), and `field-test-qa/.agent-eval/` is stale on purpose with
two fixtures depending on it, so a new one must be BUILT, not carved out of it.

---

## 4. Gotchas a new session will otherwise rediscover the hard way

- **Commands** (run from `agent-eval/`):
  - `python3 -m unittest discover -s tests` — 687 tests, ~2min, the blessed
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
- **Cost NEVER gates, and the price table is a SIDECAR (9c).** `score_cost.py`
  is run-level; **`cost_latency` is a capability, not a layer** — no row in §5's
  table, none in `LAYER_ORDER`, permanently (contract **§5.7**). Exactly ONE
  keep rule remains (`stats.py`); `FORBIDDEN_VERDICT_WORDS` + a test enforce it.
  `--prices <state>/prices.json` is declared per app and **not in `plan.json`**
  — the runner prices nothing, so a key there would have no reader. Exact model
  match; an unknown model withholds the **dollars**, keeps the **tokens**.
  **A trace-less run cannot be priced at all** (no `trajectory.json`) — that
  includes the field test, whose latency is still complete.
- **§4.3's bootstrap is DECLINED (9c), deliberately.** A resampling
  approximation breaks `stats.py`'s house rule. Replaced by three exact tests:
  the sign test (imported from `stats.py`, so there is one exact binomial), that
  test **inverted over order statistics** for an exact distribution-free median
  CI, and an exact permutation test capped on **DP STATES, not m**
  (`min(2^m, span/gcd+1)`), declined past the cap. Never "add a CI" by
  resampling.
- **Five things are RESERVED and the validator WARNs on each:** `expect.state`,
  `seed_state`, `available_tools`, `excluded_tools` (+ `difficulty`, doc-only).
  `run_cases.py` calls `environment.seed/.reset/.snapshot_state` **nowhere** —
  seed out of band. Do not "wire up" one without a scorer to read it.
- **The holdout ledger is a `.jsonl` sidecar** (`datasets/holdout-looks.jsonl`,
  from `paths.holdout_ledger`), not the YAML dataset metadata — a stdlib-only
  runner would corrupt the YAML. The total is a LINE COUNT.
- **`__oos__` is `score_routing.py`'s canonical internal OOS label**, not a
  data value; `--oos-route <name>` maps the profile's route onto it. **The
  scorers' error contract:** errors go to **stdout** (not stderr) as JSON, exit
  2 on malformed input — deliberate, see `_common.die()`.
- **`field-test-qa/.agent-eval/` is stale ON PURPOSE** — mixed layout, 36
  validator errors, two fixtures depend on it; **read its `LAYOUT.md` before
  migrating or copying it.** Not a good example, and nothing else is one either.
- **The field test's app under test is READ-ONLY** and lives outside this repo
  (`/path/to/reference-app`). Never modify it.
- **The audit's token numbers are `words × 1.33` over `skills/**/*.md`, whole
  file — `wc -l` is not their meter** (`AUDIT` §1 has the figures).
- **`.github/workflows/ci.yml` has never run** — no git remote, so it is a
  specification (its header says so). The three commands above are the only
  checked claim; **never report CI as green.**
- Every audit finding has a copy-pasteable repro in `AUDIT-2026-09-06.md`.
  Re-run rather than re-deriving.
