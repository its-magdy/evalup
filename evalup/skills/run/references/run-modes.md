# Run Modes

One execution engine — `${CLAUDE_PLUGIN_ROOT}/scripts/run_cases.py`, spec in
`${CLAUDE_PLUGIN_ROOT}/docs/runner-contract.md`. These five modes are named
selections + gate policies the skill resolves into that runner's `plan.json`
(SKILL.md §1); nothing else about the engine changes per mode. This run-mode
taxonomy maps onto the plugin's pre-existing `--smoke` flag.

| Mode | Flag | Selection | k | Gate | When |
|---|---|---|---|---|---|
| smoke | `--smoke` | cases with `smoke` in `split` | 1 (fixed) | soft | every prompt/code edit |
| regression | `--regression` (or no mode flag) | cases with `full` in `split` | ≥3, pass^k | hard | pre-merge / nightly |
| targeted | `--targeted --tag <component>` [+ `--filter-failing`] | tag-filtered subset of the `full` (or `smoke`) split | per-case `k` | soft | right after optimizing one surface |
| holdout | `--holdout` | sealed `holdout` split | per-case `k` | decision | optimizer's keep/revert step |
| full | `--full` | `full` + `holdout` splits, judged layers forced on | ≥3, pass^k | hard | release validation |

No mode flag behaves as `regression` (the pre-existing unqualified default:
full suite, hard gate) so old invocations without a mode flag keep working.

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
- **k**: fixed at 1. `--k` is ignored under `--smoke` (reliability isn't what
  this cadence buys you — speed is); pass `--regression --k N` instead if you
  want repeats.
- **Gate**: soft. The caller (a human, or the debounced PostToolUse hook —
  `${CLAUDE_PLUGIN_ROOT}/docs/hooks-example.json` +
  `${CLAUDE_PLUGIN_ROOT}/docs/smoke.sh.example`) proceeds regardless of the
  verdict; the run is advisory, meant to be looked at, not to block anything by
  itself.
- **Judged layers**: skipped outright, even if `judge.status: calibrated` —
  report `unjudged (mode: smoke)`. This is deliberately a different string
  from the uncalibrated-judge banner: "no judged score this run" has two
  distinct causes (mode chose to skip it for cost/speed vs. the judge isn't
  trustworthy yet) and conflating them would hide a calibration gap behind a
  cadence choice.
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
  override, but `--k` < 3 under `--regression` prints a warning that pass^k
  loses meaning below that, it does not refuse.
- **Gate**: hard. The CI step exits nonzero on gating failures — this is the
  mode CI runs pre-merge/nightly (SKILL.md §6; the runner itself always exits 0
  on a red suite).
- **Judged layers**: included if `judge.status: calibrated`, `unjudged` with
  the calibration banner otherwise (the ordinary rule — regression does not
  force judged layers the way `full` does; that keeps the everyday merge gate
  fast even before calibration exists).

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
- **k**: whatever each case specifies; targeted does not force repeats.
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
- **k**: per-case, unchanged.
- **Gate**: **decision**, not pass/fail against a fixed floor. `stats.py`'s
  output on the aggregate paired verdicts (exact Bayesian P(improvement) +
  exact one-sided sign test) **is** the keep/revert call that
  `${CLAUDE_PLUGIN_ROOT}/skills/optimize/SKILL.md` step 5 ("Gate on holdout")
  consumes — there is no separate hard threshold beyond that statistical test.
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
- **Who invokes it**: normally `optimize`, internally, once training looks
  positive — not a mode a developer reaches for directly in the everyday
  loop.

## `full`

- **Selection**: everything — the `full` **and** `holdout` splits combined. This is
  the one mode where a holdout look is implied by scope rather than asked
  for explicitly; it still counts against the same N=5 budget as `--holdout`
  (see above), and holdout-split cases inside a `--full` run are still
  reported aggregate-only, never per-case — running `--full` does not weaken
  the seal.
- **k**: ≥3 default, same reasoning as `regression`.
- **Gate**: hard.
- **Judged layers**: forced on regardless of mode-for-speed skipping —
  `unjudged` with the calibration banner if `judge.status` isn't
  `calibrated`, but never silently skipped the way `smoke`/`targeted` skip
  them. `full` is the release-validation sweep; if judged dimensions matter
  to the release decision, an `unjudged` banner on page one is the signal to
  go calibrate before shipping, not to ship blind.
- **Adversarial/authz cases**: run if the adapter's `environment.safe_to_attack`
  is true (same gate as any other red-team probe; the runner applies it at
  pre-flight, runner-contract §4 item 6 and §7); still skipped (not failed) otherwise.
- **When**: release validation, before staged rigor moves an app toward
  `traffic`/`production` — a
  monitoring-window release habit sits on top of this, not instead of it
  (`${CLAUDE_PLUGIN_ROOT}/docs/workflow.md` "Production era").

---

## Headless/CI gate — worked example

The hard-gated modes (`regression`, `full`) are the ones wired into CI. The
LLM produces a JSON verdict and nothing more; a separate, tokenless shell step
owns the actual gate:

```bash
# Agent-driven part: no approval prompts, no plugin auto-discovery surprises.
claude -p "/evalup:run --regression" \
  --output-format json --bare --permission-mode dontAsk \
  > run-output.json

# Before trusting anything below, confirm the plugin loaded clean.
jq -e 'select(.type=="system" and .subtype=="init")
       | (.plugin_errors // [] | length == 0)
         and (.mcp_server_errors // [] | length == 0)' \
  run-output.json >/dev/null || { echo "plugin/mcp load errors — run untrusted"; exit 2; }

# The actual gate: a plain script reads the verdict, no LLM involved. It
# finds the run this invocation just wrote by the TIMESTAMP in the run id
# (never mtime, which drifts when a directory is copied or a report
# regenerated), reads that run's results.json -- the durable file that
# survives past this one CI invocation -- and exits 0 open / 1 closed / 2 bad
# input. Stdlib Python: no jq, no yq.
python3 "$EVALUP_ROOT/scripts/gate.py" reports/ --latest --mode regression \
  --max-infra-rate 0.05
```

`gate.py` closes on any gating failure (the sealed holdout's aggregate
included), on a run whose `summary.status` is not `ok` or whose own
`exit_code` is non-zero — "0 gating failures" from an aborted run is not a
pass — and on `infra_rate` above the threshold, and lists **every** reason.
`--json` prints the same facts for a dashboard. Deterministic-only CI needs no
`claude -p` at all: `convert_suite.py` → `run_cases.py` → `gate.py` is three
plain commands (`${CLAUDE_PLUGIN_ROOT}/examples/quickstart/demo.py` is that
pipeline, runnable).

- `--bare` skips plugin/skill auto-discovery, so the same command produces
  the same behavior on every CI runner — reproducibility over convenience.
- `--permission-mode dontAsk` removes every approval prompt, including the
  cost/time confirmation in SKILL.md §2 — there is no human on the other end
  to answer it in CI.
- Checking `system/init` for `plugin_errors`/`mcp_server_errors` first is not
  optional: a run that launched under a broken plugin load can produce a
  clean-looking JSON verdict for the wrong reason (e.g. every case silently
  skipped a layer), and the gate script would happily green-light it.
- `--latest` assumes this CI job is the only writer of that mode at that
  moment — a shared `reports/` directory with concurrent runs needs the run
  directory passed explicitly (`gate.py reports/<run-id>`) rather than
  inferred by recency.
- Honesty check on both practices above: `--bare` is a real, working flag but
  is currently absent from the official CLI reference (an open documentation
  gap, not a plugin quirk), and gating on `system/init`'s `plugin_errors`
  field is not an officially documented pattern — undocumented plugin load
  failures currently tend to surface as generic hook errors instead. Both are
  kept here as best-effort practice; re-verify against the current Claude
  Code docs when actually wiring this into CI.
- Note the zero-failure branch (SKILL.md §5) surfaces here as
  `gating_failures == 0` — a legitimate, common, good outcome, not an edge
  case the gate script needs to special-case beyond "0 is not greater than
  0."
