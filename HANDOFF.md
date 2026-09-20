# evalup — session handoff / working state

**Purpose:** this file is the single place a new session reads to pick up work
without re-deriving anything. It is deliberately small. The *findings* live in
`AUDIT-2026-09-06.md`; this file holds **state, plan, and gotchas only**.

**Last updated:** 2026-09-20 (renamed `agent-eval` → `evalup`; the
`LAYER_OF_EXPECT` swap FIXED 2026-09-11 was the last confirmed defect)
**Update rule:** whenever you finish a step, edit §3 (mark it done, add what you
actually did + the commit sha) and bump the date above. Do not let this file
grow past ~200 lines; move detail into the audit doc or a commit message.

---

## 1. Where things stand

- **Repo:** `/path/to/workspace`;
  plugin `evalup/` (v0.1.0; **renamed from `agent-eval` 2026-09-14** — dated
  records and `field-test-qa/` keep the old name on purpose); root also holds
  `research/`, `field-test-qa/` and the review docs. Branch `rename-to-evalup`
  (off `run-layout-consistency`), main branch `main`.
- **Baseline commit `6c7d286`** holds the whole prior review wave, so every
  later diff is readable against it.
- **Health:** **706** tests pass (~2min), ruff clean, all **17** CLI scripts
  `--help` rc=0. Re-verified after the rename, 2026-09-20, on **3.13 and 3.9**
  (the blessed `unittest` runs on whatever `python3` is, so the uv 3.9 floor
  check below is not optional). **Known-red, and it predates the rename:** CI's
  `cli-help` job asserts **14** CLIs and there are 17.

### Documents, in the order a newcomer should read them
| File | What it is |
|---|---|
| `HANDOFF.md` (this) | state + plan. Start here. |
| `AUDIT-2026-09-06.md` | **current** audit. All findings, all verified. |
| `REVIEW-2026-08-08.md` | prior review; it was the source for the whole plan and is now **fully dispositioned and CLOSED**. §2 bugs, §3 packaging, §6 slimming, §9 metrics and §10's doc fixes are DONE; all TEN of §4's gaps are built or RESERVED (`AUDIT` §"Step 9 recon" has the table); **§5 is out of plan by decision**; §7's order is superseded by §3 below. §10's worked example was **built new** (not regenerated — it never could be). **Nothing in it is open.** |
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

- [x] **Steps 0–8b — CLOSED, 2026-09-07/10. Bodies in `AUDIT`; the shas are the
      readable diff.** *0–4* baseline, splits-as-a-field, one path convention,
      the three scorer bugs, a tighter `validate_cases.py` — `6c7d286`,
      `f03c960`, `05804ef`, `0d2aa91`, `201aa2a`. *5a–5c* the runner: spec in
      `docs/runner-contract.md`, engine in `run_cases.py`, skill rewired 318 →
      **164** — `77d3778`, `c9bc4f7`, `5e573c9`, `676ab23`. *6a–6e* slim the
      skills, D1 (**`≤120 lines` RETIRED**) and D2 (`help` loses its body),
      dedupe — `86b602e`, `d6bf421`, `6268cc3`, `4ede98f`, `a22ce8e`. *7*
      multi-turn **RESERVED** — `337181a`. *8a–8b* packaging; the stale
      artifacts **relabelled, not regenerated**, and `micro_f1` kept —
      `526e495`, `593aad1`.
      **The four lessons that outlived their steps:** paths are
      `${CLAUDE_PLUGIN_ROOT}/…` cross-skill, relative inside a skill, bare for
      app/state (README §"Path convention"); **ERROR = a claim nothing backs,
      WARN = thinner than the guidance recommends**; every bug was a *class*
      with siblings, so **grep, don't work a list** — the audit's offender list
      was wrong in both directions; and **a MENTION count is not a duplication
      count.**

**`REVIEW` §4 has TEN gaps and the old Step 9 bullet named four.** `AUDIT`
§"Step 9 recon" has the disposition table; six were in no step.

- [x] **Step 9a — the judge loop (`REVIEW` §4.4). DONE — `eebd318`.**
      `score_agreement.py` **+ a gate change in `run_cases.py`** (§4).
- [x] **Step 10 — RESERVE `REVIEW` §4's items 2, 5, 7, 8 and 10. DONE
      2026-09-10 — `d22bcaa`.** All five reserved, none built; body in `AUDIT`
      §"RESOLVED … (Step 10)". **§4.7 was the live one** and its rollup half hit
      every case, not just `state` — see §4's `http` gotcha. **§4.2 is a doc
      correction; §4.5 is a non-finding** (nothing declares a per-layer
      statistic, so there is nothing to reserve). 4 WARNs + 1 ERROR.
- [x] **Step 9b — run history (`REVIEW` §4.9). DONE — `2bbb40a`.**
      `run_history.py` + 58 tests, named in `analyze/SKILL.md`. **"All data
      already on disk" was WRONG a fifth time; the gap was COMPARABILITY**
      (`manifest.yaml`-only keys), so 9b had nothing to reserve.
- [x] **Step 9c — cost/latency (`REVIEW` §4.3). Step 9 and the WHOLE
      REMEDIATION WAVE CLOSED. DONE 2026-09-10 — `afc2650`.**
      `scripts/score_cost.py` + 35 tests, contract **§5.7**, named in
      `analyze/SKILL.md`. Body in `AUDIT` §"RESOLVED … (Step 9c)". Half build,
      half reserve: **`cost_latency` is not a per-case layer and never will be**
      (§5.7 + a test), while the *capability* flag now has a real reader.
      **§4.3's bootstrap is DECLINED by name** — see §4's gotcha. Finding six
      (the recon's `stages.json`) is corrected in place in `AUDIT`'s recon.

- [x] **The worked example (`REVIEW` §10) — BUILT NEW. The FIRST work after the
      wave, and it CLOSES `REVIEW` entirely. DONE 2026-09-10.**
      `evalup/examples/quickstart/` + `tests/test_example.py` (16 tests).
      Body in `AUDIT` §"RESOLVED … (the worked example)". **A SPLIT, and it is
      the answer to 8b:** the *inputs* are COMMITTED (readable before install,
      and they ship — a marketplace install copies the tree), the *run* is
      GENERATED into a temp dir on every test run against a real HTTP server,
      so it can never be stale. `validate_cases.py --strict` over the shipped
      suite must be **0 errors AND 0 warnings**. Two handed-down claims were
      wrong again: **only ONE test reads `field-test-qa/.agent-eval/`**, not
      two (the halt branch builds its shape synthetically), and it is **36
      ERROR / 8 WARN** today, not 7 — Step 10 added a WARN under 8b. Both
      corrected in place in that dir's `LAYOUT.md`; **no artifact under it was
      touched.** No new CLI script — the fixture app lives in `tests/`, so the
      count stays **17**.

**The plan is DONE and `REVIEW` is CLOSED. What is deliberately OUT** — decided,
not dropped: **§4.6 RAG/retrieval** and **§5's five "ideas worth stealing"** are
greenfield additions, not remediation (grep returns zero files; `AUDIT` §"Step 9
recon" DEMOTED them) — a new wave if anyone wants one. **Everything Steps 7 and
10 RESERVED** (multi-turn, `expect.state`, `seed_state`, `available_tools`,
`excluded_tools`, agent-scoped expectations, streaming/TTFT) plus 9c's
**cached-token pricing** each wait for the scorer that would read them; do not
wire one up without one.

**The `LAYER_OF_EXPECT` swap — FIXED 2026-09-11, and NOTHING IS OPEN.**
`validate_cases.py` mapped `expect.tools`/`expect.args` onto the OPPOSITE layers
from `run_cases.py` and contract §5; two map lines + the docstring repeating it.
Body in `AUDIT` §"RESOLVED … (the LAYER_OF_EXPECT swap)". **Both suites
re-measured and both HELD** — `field-test-qa/` 36/8, `quickstart/` 0/0, each
blind to it for a different reason. A test pins the pairing now (one
structural, against `run_cases.applicable_layers`) — nothing did, which is why
it survived.
**Number six:** `machine_accepted` fires 6× there, not 7, and `gating` is not
its condition. Anything further is a NEW WAVE — see just above.

---

## 4. Gotchas a new session will otherwise rediscover the hard way

- **Commands** (run from `evalup/`):
  - `python3 -m unittest discover -s tests` — 706 tests, ~2min, the blessed
    command. (`test_run_cases.py` is most of the time: a real HTTP server per
    test, and one test kills a runner mid-run.)
  - `ruff check --config ruff.toml .` — `--config` is not decoration:
    `pyproject.toml` is now a sibling, and when both exist **`ruff.toml` wins
    and a `[tool.ruff]` table in `pyproject.toml` is ignored silently**.
  - `uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q`
    — the only conclusive 3.9 floor check. Run before any release.
- **`validate_cases.py` and `run_cases.py` take JSON, not YAML**, deliberately
  — the skill's YAML→JSON conversion *is* the run's parse, not a second
  opinion. Read the docstrings before "fixing" that.
- **The runner's spec is `evalup/docs/runner-contract.md` and it wins.** Its
  nine decisions were confirmed 2026-09-08, with trade-offs — don't reopen one
  without reading it. **Three tests read the file at run time** (they exec
  §9(b)'s python block, parse §6's `# only when:` markers, and pin
  `--oos-route`'s precondition), so an edit fails the suite until the code
  follows — deliberate, not brittle.
- **Four rules the closed steps left behind — each has a full body in `AUDIT`;
  do not reopen one without reading it.** (a) **The judged gate is DERIVED**
  (9a): `judge.status: calibrated` is necessary, not sufficient — the
  `paths.judge_calibration` sidecar must back it, and every failure is a reason
  on `unjudged`, never an exit code. (b) **`http` no longer carries a case to
  `pass`** (Step 10): rollup rule 5 is non-`http`, so an all-`unscored` case
  rolls up `unscored`, never `fail`. (c) **A series is not a pair** (9b):
  `run_history.py` keys on `mode` + `selecting_split` + the enabled-layer set,
  and **not** on the app `git_sha`; only `status: "ok"` runs join, and it never
  says "better". (d) **Cost NEVER gates and its price table is a SIDECAR** (9c):
  `cost_latency` is a capability, not a layer (contract **§5.7**, permanent);
  `--prices` is per app and **not in `plan.json`**; an unknown model withholds
  the dollars and keeps the tokens; **a trace-less run cannot be priced at
  all**. Related: **§4.3's bootstrap is DECLINED** — never "add a CI" by
  resampling; three exact tests replaced it.
- **Five things are RESERVED and the validator WARNs on each:** `expect.state`,
  `seed_state`, `available_tools`, `excluded_tools` (+ `difficulty`, doc-only).
  `run_cases.py` calls `environment.seed/.reset/.snapshot_state` **nowhere** —
  seed out of band. Do not "wire up" one without a scorer to read it.
- **Three conventions that look arbitrary and are not:** the **holdout ledger**
  is a `.jsonl` sidecar (`paths.holdout_ledger`), never the YAML metadata a
  stdlib-only runner would corrupt, and its total is a LINE COUNT; **`__oos__`**
  is `score_routing.py`'s internal label, never a data value (`--oos-route`
  maps a profile's route onto it); and **the scorers' error contract** puts
  errors on **stdout** as JSON with exit 2 (`_common.die()`), not stderr.
- **`field-test-qa/.agent-eval/` is stale ON PURPOSE** — mixed layout, 36
  validator errors / 8 WARNs; **read its `LAYOUT.md` before touching it.** It
  keeps the OLD state-dir name on purpose (live apps use `.evalup/`) — its
  artifacts hold absolute `.agent-eval` paths.
  **Exactly ONE test reads it** (`test_run_cases.py`:1859, the smoke report dir,
  on a copy) — the "two fixtures" line was wrong. Its app under test is
  **READ-ONLY** and outside this repo
  (`/path/to/reference-app`) — never modify it.
- **The conforming example is `evalup/examples/quickstart/`**, driven by
  `tests/test_example.py`. Its inputs are committed and its run is NOT: edit any
  YAML there and you must regenerate `converted.json` (the one-liner is in its
  README) or a test fails. `validate_cases.py --strict` over it must stay at
  **0 errors and 0 warnings**.
- **`.github/workflows/ci.yml` has never run** — no git remote, so it is a
  specification (its header says so). The three commands above are the only
  checked claim; **never report CI as green.** The audit's token numbers are
  `words × 1.33` over `skills/**/*.md`, whole file — `wc -l` is not their meter.
  Every audit finding has a copy-pasteable repro: re-run it, don't re-derive it.
