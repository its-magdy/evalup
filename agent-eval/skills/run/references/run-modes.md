# Run Modes

One execution engine (SKILL.md §§1–4: pre-flight → execute → score →
compare/report). These five modes are named selections + gate policies layered
over it — nothing else about the engine changes per mode. This run-mode
taxonomy maps onto the plugin's pre-existing `--smoke` flag.

| Mode | Flag | Selection | k | Gate | When |
|---|---|---|---|---|---|
| smoke | `--smoke` | tagged `datasets/smoke/` subset | 1 (fixed) | soft | every prompt/code edit |
| regression | `--regression` (or no mode flag) | `datasets/full/` | ≥3, pass^k | hard | pre-merge / nightly |
| targeted | `--targeted --tag <component>` [+ `--filter-failing`] | tag-filtered subset of `full/` (or `smoke/`) | per-case `k` | soft | right after optimizing one surface |
| holdout | `--holdout` | sealed `datasets/holdout/` | per-case `k` | decision | optimizer's keep/revert step |
| full | `--full` | `full/` + `holdout/`, judged layers forced on | ≥3, pass^k | hard | release validation |

No mode flag behaves as `regression` (the pre-existing unqualified default:
full suite, hard gate) so old invocations without a mode flag keep working.

---

## `smoke`

- **Selection**: `datasets/smoke/`, a **durable tag** written once by
  `/agent-eval:generate` (see `skills/generate/SKILL.md` §4 Splits:
  diversity-selected, about a third of the full set, min 5) — never
  recomputed per run. Recomputing membership on the fly would make the smoke
  subset a moving target and the run non-diffable across edits; the whole
  point of a fixed tag is that "smoke got worse" means the app changed, not
  that the sample changed.
- **k**: fixed at 1. `--k` is ignored under `--smoke` (reliability isn't what
  this cadence buys you — speed is); pass `--regression --k N` instead if you
  want repeats.
- **Gate**: soft. The caller (a human, or the debounced PostToolUse hook —
  `docs/hooks-example.json` + `docs/smoke.sh.example`) proceeds regardless of
  the verdict; the run is advisory, meant to be looked at, not to block
  anything by itself.
- **Judged layers**: skipped outright, even if `judge.status: calibrated` —
  report `unjudged (mode: smoke)`. This is deliberately a different string
  from the uncalibrated-judge banner: "no judged score this run" has two
  distinct causes (mode chose to skip it for cost/speed vs. the judge isn't
  trustworthy yet) and conflating them would hide a calibration gap behind a
  cadence choice.
- **Cost**: sub-dollar, minutes — see `docs/workflow.md` "Steady state."

## `regression`

- **Selection**: `datasets/full/` — everything reviewed (see
  `skills/generate/SKILL.md` §4).
- **k**: ≥3 by default so `pass^k` (all k repeats succeed — the reliability
  number; a single-run pass rate hides flakiness) is meaningful; computed by
  `scripts/reduce_repeats.py` over the per-case repeated verdicts (also emits
  pass@k and the pass@k−pass^k flakiness gap). `--k` may override, but `--k` < 3
  under `--regression` prints a warning that pass^k loses meaning below that, it
  does not refuse.
- **Gate**: hard. Nonzero exit on gating failures — this is the mode CI runs
  pre-merge/nightly (§5 Headless/CI gate in SKILL.md).
- **Judged layers**: included if `judge.status: calibrated`, `unjudged` with
  the calibration banner otherwise (the ordinary rule — regression does not
  force judged layers the way `full` does; that keeps the everyday merge gate
  fast even before calibration exists).

## `targeted`

- **Selection**: filter the case set by **component tag** — each case's
  `unit` field (the route target: domain / node / sub-agent; see
  `skills/generate/references/case-format.md`) or, equivalently,
  `expect.route` — to the surface you just touched. Match on the field, never
  on the id: case ids are opaque `c-<hash8>` and deliberately encode neither
  unit nor category (`skills/generate/SKILL.md` §4), because both are mutable
  classifications and a glob like `billing-*` would silently select nothing
  the day a case is reclassified. `--tag <component>` is required for this
  mode; it has no default "everything" behavior (that is what `regression`/
  `full` are for).
- **`--filter-failing`**: add this to further restrict the (already
  tag-filtered) set to cases that scored `fail` on the most recent
  *comparable* prior run for that tag (same `dataset_version` AND same
  harness version — identical comparability rule as the baseline diff in
  SKILL.md §4; a scorer/dataset change invalidates "prior failures" the same
  way it invalidates a baseline diff). No comparable prior run exists → this
  is a variant of the first-run branch: fall back to the full tag-filtered
  set and say so ("no comparable prior run for tag `billing` — running all
  N tagged cases instead of just prior failures"), never error.
- **k**: whatever each case specifies; targeted does not force repeats.
- **Gate**: soft. This is a developer feedback loop ("did my tool-description
  edit fix what I think it fixed?"), not a merge gate.
- **When**: immediately after `optimize` (or a manual edit) touches one
  prompt/tool description/route — see `skills/optimize/SKILL.md` step 4
  ("Measure"), which runs the training split under a fresh manifest; a
  developer sanity-checking the same edit outside the optimize loop reaches
  for `--targeted` instead of paying for the full suite.

## `holdout`

- **Selection**: `datasets/holdout/` — sealed (see
  `skills/generate/SKILL.md` §4: written once, listed only by id+hash in
  reports, never displayed case-by-case; unsealing for human inspection is
  `analyze --unseal`, a deliberately inconvenient, logged, counted action —
  see `skills/analyze/SKILL.md` §"`--unseal`").
- **k**: per-case, unchanged.
- **Gate**: **decision**, not pass/fail against a fixed floor. `stats.py`'s
  output on the aggregate paired verdicts (exact Bayesian P(improvement) +
  exact one-sided sign test) **is** the keep/revert call that
  `skills/optimize/SKILL.md` step 5 ("Gate on holdout") consumes — there is
  no separate hard threshold beyond that statistical test.
- **Reporting**: aggregate only, always — no per-case trace excerpts, no
  page-one failure clusters (per SKILL.md §4's zero-failure/holdout
  reporting rule). This is the one place in the harness where "less detail in
  the report" is the correct, intentional behavior, not a gap. Per-case
  `cases/<case-id>/` folders still get written on disk (SKILL.md §2 — that's
  what makes resume work), but their `request.json`/`response.json` content
  is never pulled into `report.md`/`.html` or `results.json`'s per-case rows;
  only the aggregate stats cross the seal.
- **Holdout-look budget**: running `--holdout` counts as one look toward the
  N=5 reseal trigger (`skills/optimize/SKILL.md` "Candidate pool": "Holdout
  looks are counted; after 5, analyze forces a reseal"), the same as an
  `analyze --unseal`. Don't run `--holdout` speculatively — it spends the
  same budget as unsealing.
- **Who invokes it**: normally `optimize`, internally, once training looks
  positive — not a mode a developer reaches for directly in the everyday
  loop.

## `full`

- **Selection**: everything — `full/` **and** `holdout/` combined. This is
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
  is true (same gate as any other red-team probe; see SKILL.md §3's authz
  layer); still skipped (not failed) otherwise.
- **When**: release validation, before staged rigor moves an app toward
  `traffic`/`production` — a
  monitoring-window release habit sits on top of this, not instead of it
  (`docs/workflow.md` "Production era").

---

## Headless/CI gate — worked example

The hard-gated modes (`regression`, `full`) are the ones wired into CI. The
LLM produces a JSON verdict and nothing more; a separate, tokenless shell step
owns the actual gate:

```bash
# Agent-driven part: no approval prompts, no plugin auto-discovery surprises.
claude -p "/agent-eval:run --regression" \
  --output-format json --bare --permission-mode dontAsk \
  > run-output.json

# Before trusting anything below, confirm the plugin loaded clean.
jq -e 'select(.type=="system" and .subtype=="init")
       | (.plugin_errors // [] | length == 0)
         and (.mcp_server_errors // [] | length == 0)' \
  run-output.json >/dev/null || { echo "plugin/mcp load errors — run untrusted"; exit 2; }

# The actual gate: a plain script reads the verdict, no LLM involved. Find
# the run this invocation just wrote — its manifest.yaml is the newest one
# under reports/, and manifest.yaml's own `run_id` field (SKILL.md §1) is the
# source of truth, not stdout-parsing this CLI call's own text reply — then
# pull results.json from that same run directory (SKILL.md §4), since
# results.json is the durable file that also survives past this one CI
# invocation.
manifest=$(ls -t reports/*/manifest.yaml | head -1)
run_id=$(yq -r '.run_id' "$manifest")   # or python -c 'import yaml,sys; ...' if yq isn't available
gating_failures=$(jq '.summary.gating_failures' "reports/$run_id/results.json")
infra_rate=$(jq '.summary.infra_rate' "reports/$run_id/results.json")
if [ "$gating_failures" -gt 0 ] || awk "BEGIN{exit !($infra_rate > 0.05)}"; then
  exit 1
fi
exit 0
```

- `--bare` skips plugin/skill auto-discovery, so the same command produces
  the same behavior on every CI runner — reproducibility over convenience.
- `--permission-mode dontAsk` removes every approval prompt, including the
  §1 pre-flight cost/time confirmation — there is no human on the other end
  to answer it in CI.
- Checking `system/init` for `plugin_errors`/`mcp_server_errors` first is not
  optional: a run that launched under a broken plugin load can produce a
  clean-looking JSON verdict for the wrong reason (e.g. every case silently
  skipped a layer), and the gate script would happily green-light it.
- `ls -t reports/*/manifest.yaml | head -1` assumes this CI job has an
  otherwise-empty `reports/` (or is the only writer racing at that moment) —
  a shared `reports/` directory with concurrent runs needs the run id passed
  through explicitly (e.g. captured from the invoking job's own arguments)
  rather than inferred by recency.
- Honesty check on both practices above: `--bare` is a real, working flag but
  is currently absent from the official CLI reference (an open documentation
  gap, not a plugin quirk), and gating on `system/init`'s `plugin_errors`
  field is not an officially documented pattern — undocumented plugin load
  failures currently tend to surface as generic hook errors instead. Both are
  kept here as best-effort practice; re-verify against the current Claude
  Code docs when actually wiring this into CI.
- Note the zero-failure branch (SKILL.md §4) surfaces here as
  `gating_failures == 0` — a legitimate, common, good outcome, not an edge
  case the gate script needs to special-case beyond "0 is not greater than
  0."
