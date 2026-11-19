# Run Modes

One execution engine — `${CLAUDE_PLUGIN_ROOT}/scripts/run_cases.py`, spec in
`${CLAUDE_PLUGIN_ROOT}/docs/runner-contract.md`. These five modes are named
selections + gate policies the skill resolves into that runner's `plan.json`
(SKILL.md §1); nothing else about the engine changes per mode.

| Mode | Flag | Selection | k | Gate | When |
|---|---|---|---|---|---|
| smoke | `--smoke` | cases with `smoke` in `split` | 1 (fixed) | soft | every prompt/code edit |
| regression | `--regression` (or no mode flag) | cases with `full` in `split` | 3 by default (`--k N`), pass^k | hard | pre-merge / nightly |
| targeted | `--targeted --tag <component>` [+ `--filter-failing`] | tag-filtered subset of the `full` (or `smoke`) split | 1 by default (`--k N`) | soft | right after optimizing one surface |
| holdout | `--holdout` | sealed `holdout` split | 3 by default (`--k N`), pass^k | decision | optimizer's keep/revert step |
| full | `--full` | `full` + `holdout` splits as **two runs** (`regression`, then `holdout`) | 3 by default (`--k N`), pass^k | hard | release validation |

No mode flag behaves as `regression` (full suite, hard gate).

---

## `smoke`

- **Selection**: every case whose `split` field contains `smoke` — a **durable
  tag** written once by `/evalup:generate` (see
  `${CLAUDE_PLUGIN_ROOT}/skills/generate/SKILL.md` §4 Splits:
  diversity-selected, about a third of the full set, min 5) — never recomputed
  per run. Recomputing membership on the fly would make the smoke subset a
  moving target and the run non-diffable across edits; the whole point of a
  fixed tag is that "smoke got worse" means the app changed, not that the
  sample changed.
- **k**: fixed at 1. `--k` is refused under `--smoke` (`make_plan.py` exits 2:
  reliability isn't what this cadence buys you — speed is); pass
  `--regression --k N` instead if you want repeats.
- **Gate**: soft. The caller (a human, or the debounced PostToolUse hook —
  `${CLAUDE_PLUGIN_ROOT}/docs/hooks-example.json` +
  `${CLAUDE_PLUGIN_ROOT}/docs/smoke.sh.example`) proceeds regardless of the
  verdict; the run is advisory, meant to be looked at, not to block anything by
  itself.
- **Judged layers**: never scored at run time, in any mode — the runner has
  no judge (runner-contract §5.4). Only the reason string varies: here it is
  `unjudged (mode: smoke)`, even if `judge.status: calibrated`. That is
  deliberately a different string from the uncalibrated-judge banner, so a
  calibration gap does not hide behind a cadence choice.
- **Cost**: sub-dollar, minutes — see `${CLAUDE_PLUGIN_ROOT}/docs/workflow.md`
  "Steady state."

## `regression`

- **Selection**: every case whose `split` field contains `full` — everything
  reviewed (see
  `${CLAUDE_PLUGIN_ROOT}/skills/generate/SKILL.md` §4).
- **k**: ≥3 by default so `pass^k` (all k repeats succeed — the reliability
  number; a single-run pass rate hides flakiness) is meaningful; computed by
  `${CLAUDE_PLUGIN_ROOT}/scripts/reduce_repeats.py` over the per-case repeated
  verdicts (also emits pass@k and the pass@k−pass^k flakiness gap). `--k` may
  override with any k ≥ 1 and nothing warns; below 3, pass^k loses meaning,
  so say so in the report.
- **Gate**: hard. The CI step exits nonzero on gating failures — this is the
  mode CI runs pre-merge/nightly (SKILL.md §6; the runner itself always exits 0
  on a red suite).
- **Judged layers**: `unjudged`, like every mode. The reason is
  `judge not calibrated` (or the sidecar gap that blocks it), and
  `deferred to skill` once a calibrated judge is backed by its sidecar — no
  skill fills that row in yet, so it is still not measured.

## `targeted`

- **Selection**: filter the case set by **component tag** — each case's `unit`
  field (the route target: domain / node / sub-agent; see
  `${CLAUDE_PLUGIN_ROOT}/skills/generate/references/case-format.md`) or,
  equivalently, `expect.route` — to the surface you just touched. Match on the
  field, never on the id: case ids are opaque `c-<hash8>` and deliberately
  encode neither unit nor category
  (`${CLAUDE_PLUGIN_ROOT}/skills/generate/SKILL.md` §4), because both are
  mutable classifications and a glob like `billing-*` would silently select
  nothing the day a case is reclassified. `--tag <component>` is required for
  this mode; it has no default "everything" behavior (that is what
  `regression`/ `full` are for).
- **`--filter-failing`**: add this to further restrict the (already
  tag-filtered) set to cases that scored `fail` on the most recent
  *comparable* prior run for that tag (same `dataset_version` AND same
  harness version — identical comparability rule as the baseline diff in
  SKILL.md §4 and runner-contract §5.6; a scorer/dataset change invalidates
  "prior failures" the same way it invalidates a baseline diff). No comparable
  prior run exists → this is a variant of the first-run branch: fall back to
  the full tag-filtered set and say so ("no comparable prior run for tag
  `billing` — running all N tagged cases instead of just prior failures"),
  never error.
- **k**: 1 by default; `--k N` overrides. Targeted does not force repeats.
- **Gate**: soft. This is a developer feedback loop ("did my tool-description
  edit fix what I think it fixed?"), not a merge gate.
- **When**: immediately after `optimize` (or a manual edit) touches one
  prompt/tool description/route — see
  `${CLAUDE_PLUGIN_ROOT}/skills/optimize/SKILL.md` step 4 ("Measure"), which
  runs the training split under a fresh manifest; a developer sanity-checking
  the same edit outside the optimize loop reaches for `--targeted` instead of
  paying for the full suite.

## `holdout`

- **Selection**: every case whose `split` field contains `holdout` — sealed
  (see `${CLAUDE_PLUGIN_ROOT}/skills/generate/SKILL.md` §4: written once,
  listed only by id+hash in reports, never displayed case-by-case; unsealing
  for human inspection is `analyze --unseal`, a deliberately inconvenient,
  logged, counted action — see `${CLAUDE_PLUGIN_ROOT}/skills/analyze/SKILL.md`
  §"`--unseal`").
- **k**: 3 by default (`--k N` overrides), pass^k per case — the keep/revert
  call reads these folded verdicts, and at k=1 one flaky retry could decide
  a keep.
- **Gate**: **decision**, not pass/fail against a fixed floor. `stats.py`'s
  output on the aggregate paired verdicts (exact Bayesian P(improvement) +
  exact one-sided sign test) **is** the keep/revert call that
  `${CLAUDE_PLUGIN_ROOT}/skills/optimize/SKILL.md` step 5 ("Gate on holdout")
  consumes — there is no separate hard threshold beyond that posterior bar
  (≥ 0.95 by default) and optimize's noise floor.
- **Reporting**: aggregate only, always — no per-case trace excerpts, no
  page-one failure clusters (SKILL.md §5's holdout rule). This is the one
  place in the harness where "less detail in the report" is the correct,
  intentional behavior, not a gap. Per-case `cases/<case-id>/` folders still
  get written on disk (runner-contract §6 — that's what makes resume work), but
  their `request.json`/`response.json` content is never pulled into
  `report.md`/`.html` or `results.json`'s per-case rows; only the aggregate
  stats cross the seal.
- **Holdout-look budget**: running `--holdout` counts as one look toward the
  N=5 reseal trigger (`${CLAUDE_PLUGIN_ROOT}/skills/optimize/SKILL.md`
  "Candidate pool": "Holdout looks are counted; after 5, analyze forces a
  reseal"), the same as an `analyze --unseal`. Don't run `--holdout`
  speculatively — it spends the same budget as unsealing.
- **The ledger**: the runner appends `{run_id, date, mode, reason}` to the
  `.jsonl` sidecar (`paths.holdout_ledger`, default
  `datasets/holdout-looks.jsonl`) and prints the running total on stderr —
  surface that count. That sidecar, not the dataset YAML, is the ledger
  `${CLAUDE_PLUGIN_ROOT}/skills/analyze/SKILL.md` §`--unseal` and
  `${CLAUDE_PLUGIN_ROOT}/skills/optimize/SKILL.md` ("Candidate pool") read.
  Count its lines: it has two writers.
- **Who invokes it**: normally `optimize`, internally, once training looks
  positive — not a mode a developer reaches for directly in the everyday
  loop.

## `full`

- **Selection**: everything — the `full` **and** `holdout` splits, as **two
  runs**: `regression`, then `holdout`. One plan cannot carry both
  (`make_plan.py --mode full` refuses, exit 2: every case is labelled with one
  `selecting_split`). The holdout run counts against the same N=5 budget as any
  `--holdout` (see above) and is still reported aggregate-only — `full` does
  not weaken the seal.
- **k**: ≥3 default, same reasoning as `regression`.
- **Gate**: hard.
- **Judged layers**: `unjudged`, as in `regression` — not measured. If judged
  dimensions matter to the release decision, that row on page one means a
  human has to check them before shipping.
- **Adversarial/authz cases**: run if the adapter's `environment.safe_to_attack`
  is true (same gate as any other red-team probe; the runner applies it at
  pre-flight, runner-contract §4 item 5 and §7); still skipped (not failed) otherwise.
- **When**: release validation, before staged rigor moves an app toward
  `traffic`/`production` — a
  monitoring-window release habit sits on top of this, not instead of it
  (`${CLAUDE_PLUGIN_ROOT}/docs/workflow.md` "Production era").

---
## `make_plan.py` flags

Every flag fills the plan; never hand-edit `plan.json` to change one
(`make_plan.py --help` is the full list).

- `--tag <unit-or-route>` and `--failing-in <run-dir>` select for
  `targeted`; `--filter-failing` is `--failing-in reports/<last comparable
  run>`.
- `--k N` overrides the mode's default k (refused under `smoke`).
- `--layer X` scores one layer: every other capability-matrix layer becomes
  `unscorable` with `blocked_by: "--layer X"`, never `pass`. X is a matrix
  key or a report row's name — `answer`, `rules` and `judged` all mean
  `answer_quality` — and `http` and `loops` have no matrix key, so they
  still run. A canary that asserts only a disabled layer reads `unscored`:
  `make_plan.py` names each in `notes`, and the gate line counts it NOT
  VERIFIED, which is not a failure.
- `scoring.oos_route` comes from the profile's `oos_handling:
  route:<name>`; pass `--oos-route <name>` when the app's out-of-scope route
  is declared any other way, or OOS metrics go unreported.
- `--timeout-s N`, `--max-attempts N`, `--backoff-s 5,30` and
  `--insecure-tls` fill `execution`: the retry pair for a throttled provider
  or an app that already retries (the runner's default is 3 tries with 1 s
  and 4 s waits); the last flag for a self-signed local dev host only —
  pre-flight otherwise exits 3 with `CERTIFICATE_VERIFY_FAILED`.
- `--single-turn` leaves conversations out of the selection (refused under
  `holdout`); otherwise a selection with a conversation writes
  `execution.max_turns` (default 12, `--max-turns N`) and a longer case is
  skipped.
- For a pipeline, `--run-id`, `--dataset-version`, `--holdout-ledger` and
  `--judge-calibration`: [ci-gate.md](ci-gate.md).

The headless/CI gate — the `claude -p` recipe and `gate.py` — is
[ci-gate.md](ci-gate.md).
