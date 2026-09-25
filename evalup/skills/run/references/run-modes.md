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
| holdout | `--holdout` | sealed `holdout` split | 1 by default (`--k N`) | decision | optimizer's keep/revert step |
| full | `--full` | `full` + `holdout` splits as **two runs** (`regression`, then `holdout`), judged layers forced on | 3 by default (`--k N`), pass^k | hard | release validation |

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
  override with any k ≥ 1 and nothing warns; below 3, pass^k loses meaning,
  so say so in the report.
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
- **k**: 1 by default; `--k N` overrides.
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

- **Selection**: everything — the `full` **and** `holdout` splits, as **two
  runs**: `regression`, then `holdout`. One plan cannot carry both
  (`make_plan.py --mode full` refuses, exit 2: every case is labelled with one
  `selecting_split`). The holdout run counts against the same N=5 budget as any
  `--holdout` (see above) and is still reported aggregate-only — `full` does
  not weaken the seal.
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
LLM produces the run and nothing more; a separate, tokenless shell step
owns the actual gate. Flags and event fields below were checked against
Claude Code 2.1.282 and its headless and permission-mode docs on 2026-09-25;
re-check them when the CLI's major version changes.

```bash
EVALUP_ROOT=/path/to/the/installed/evalup   # CLAUDE_PLUGIN_ROOT is not set outside a Claude session
export ANTHROPIC_API_KEY=...                # --bare never uses a subscription login

# Agent-driven part: no auto-discovery surprises, no prompt nobody can answer.
# `< /dev/null` closes stdin: left open, the CLI waits 3 s for piped input
# and warns before continuing.
claude -p "/evalup:run --regression" \
  --bare --plugin-dir "$EVALUP_ROOT" \
  --permission-mode acceptEdits --permission-prompts none \
  --output-format stream-json --verbose \
  < /dev/null > run-output.jsonl
claude_rc=$?

# First: did the session itself succeed? A missing or invalid API key exits
# non-zero and ends the stream with a `result` event whose `is_error` is true
# ("Not logged in"), after one turn and no run. Check the exit code and that
# flag -- not `subtype`, which reads "success" on that event. Without this
# step the plugin-load guard below passes (nothing loaded wrongly) and
# gate.py then reports "no regression run directory", which is true and
# blames the wrong thing.
if [ "$claude_rc" -ne 0 ] || \
   jq -e 'select(.type=="result") | .is_error == true' run-output.jsonl >/dev/null; then
  echo "claude -p failed (rc=$claude_rc) -- no run was produced"
  jq -r 'select(.type=="result") | .result // .error // empty' run-output.jsonl | tail -1
  exit 2
fi

# Second: confirm the plugin loaded clean. The init event omits both error
# keys when there is nothing to report, hence `// []`; and it must name this
# plugin, or `/evalup:run` resolved to nothing.
jq -e 'select(.type=="system" and .subtype=="init")
       | (.plugin_errors // [] | length == 0)
         and (.mcp_server_errors // [] | length == 0)
         and ((.plugins // []) | map(.name) | index("evalup") != null)' \
  run-output.jsonl >/dev/null || { echo "plugin/mcp load errors, or evalup not loaded -- run untrusted"; exit 2; }

# The actual gate: a plain script reads the verdict, no LLM involved. It
# finds the run this invocation just wrote by the TIMESTAMP in the run id
# (never mtime, which drifts when a directory is copied or a report
# regenerated), reads that run's results.json -- the durable file that
# survives past this one CI invocation -- and exits 0 open / 1 closed / 2 bad
# input. Stdlib Python: no jq, no yq.
python3 "$EVALUP_ROOT/scripts/gate.py" reports/ --latest --mode regression \
  --max-infra-rate 0.05
```

`gate.py` lists **every** reason it closed; the docstring at the top of
`${CLAUDE_PLUGIN_ROOT}/scripts/gate.py` owns the list of closing conditions (a gating failure is one; so are an aborted run, a run that
scored nothing, and a run directory that fails `run_cases.py --verify`).
`--json` prints the same facts for a dashboard. Deterministic-only CI needs no
`claude -p` at all: `convert_suite.py` → `make_plan.py` → `run_cases.py` →
`gate.py` is four plain commands (`${CLAUDE_PLUGIN_ROOT}/examples/quickstart/demo.py` is that
pipeline, runnable).

- `--bare` skips auto-discovery of hooks, skills, custom commands,
  subagents, installed plugins, MCP servers, auto memory and CLAUDE.md, so the
  same command behaves the same on every CI runner — reproducibility over
  convenience. Two consequences the flag does not advertise: installed
  plugins are skipped too, so this one must be named with `--plugin-dir`
  (without it `/evalup:run` does not resolve); and auth is strictly
  `ANTHROPIC_API_KEY` (or an `apiKeyHelper` passed via `--settings`) — an
  OAuth login fails with "Not logged in" before any token is spent, which is
  what the first check catches. One oddity seen on 2.1.282: the init event's
  `skills`/`slash_commands` arrays can still enumerate skills from
  `~/.claude`. Nothing shows they were loadable; the check that matters is
  the one above — `plugins` names `evalup`.
- `--output-format stream-json --verbose` is what makes the `system`/`init`
  event exist. `--output-format json` is a single final `result` object with
  no init event at all — the `jq` above finds nothing in it and exits 4, so
  the guard would reject every healthy run. Print mode refuses `stream-json`
  without `--verbose`.
- Permissions: headless, a prompt nobody answers is a denial, and
  `--permission-prompts none` makes that explicit — anything that would
  prompt is denied and Claude is told not to retry it. The skill
  pre-approves its own scripts for this one turn, and `acceptEdits` covers the
  two files the skill itself writes, `baseline.json` and `report.md`. Add
  `--allowedTools` only for what your adapter needs beyond that (README
  §Permissions). `--permission-mode dontAsk` is not a substitute: it denies
  every call that would prompt, the report write included, so the run stops
  there. SKILL.md §2's cost/time confirmation is skipped under `-p` — there
  is no human on the other end to answer it.
- The two checks before `gate.py` are in that order for a reason. The exit
  code and `result.is_error` say whether a session happened at all; the
  `system/init` check says whether the plugin it ran under loaded clean. A
  run launched under a broken plugin load can produce a clean-looking
  verdict for the wrong reason (e.g. every case silently skipped a layer),
  and the gate script would happily green-light it. `plugin_errors`,
  `mcp_server_errors` and `plugins` are documented on the init event; the
  two error keys are present only when non-empty.
- `--latest` assumes this CI job is the only writer of that mode at that
  moment — a shared `reports/` directory with concurrent runs needs the run
  directory passed explicitly (`gate.py reports/<run-id>`) rather than
  inferred by recency.
- Note the zero-failure branch (SKILL.md §5) surfaces here as
  `gating_failures == 0` — a legitimate, common, good outcome. The one
  special case is `gate.py`'s own: zero failures over zero scored cases is an
  absence, not a pass, and closes the gate.
