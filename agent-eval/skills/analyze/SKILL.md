---
name: analyze
description: >-
  Understand failures and improve the eval assets: cluster failures, label
  judge-calibration cases (15-minute flow), edit rubrics while grading, mine
  traces for new cases, and manage the sealed holdout. Use after runs to triage
  results, to calibrate the judge, or to promote real conversations into the
  dataset.
argument-hint: "[--label] [--cluster] [--mine] [--unseal]"
---

# Analyze — Failures, Labels, and the Living Dataset

Every branch here reads a run and edits the dataset. Four documents own what
they name, and this file never restates their rules:
[references/annotation-ux.md](references/annotation-ux.md) (the viewer, the
annotation JSONL, open→axial coding and its stopping rule),
`${CLAUDE_PLUGIN_ROOT}/docs/rubric-format.md` (rubric shape and the calibration
numbers), `${CLAUDE_PLUGIN_ROOT}/agents/judge.md` (judge runtime, model family)
and `${CLAUDE_PLUGIN_ROOT}/skills/generate/references/case-format.md` (what a
valid case is).

## First run — no prior data
Check before clustering, labeling or mining an empty set, and name which empty
state you found. Same discipline as `run/SKILL.md` §4's first-run branch. Rule
out the state that only *looks* empty before calling it a start.
- **No completed run** — no `reports/<run-id>/`, or the latest is all
  `infra_error`. Point at `/agent-eval:run`, or `/agent-eval:start` when you
  can't tell which of discover/generate/run is missing.
- **No rubric or judge** — `judge.status` absent, no case carries `rubric:`.
  `--label` has nothing to calibrate; point at rubric-format.md rather than
  launching the judge on zero cases.
- **`--mine` finds nothing.** Say which: no trace backend wired, stage is
  pre-`traffic`, or wired and genuinely nothing new.

## `--cluster` (default after a run)
Read the latest run's failed cases from `reports/<run-id>/`, never the holdout
details. Pick the latest by the **timestamp segment** of the run id
(`<mode>-<YYYYMMDDTHHMMSSZ>`, `run/SKILL.md` §1). A whole-string sort ranks by
mode and hands you a stale `smoke-*`. Use the timestamp over mtime, which
drifts when a report is regenerated or a directory copied.

Launch the `trace-analyzer` agent per failed case, on its normalized trajectory
(raw spans when that view is insufficient) and its violated expectations. Group
its outputs by failure mode, not by metric: same wrong-route pair, same tool
confusion, same rule violation, same missing clarification. Per cluster: count,
2 exemplar traces (expected vs actual), implicated surface, effort tag
(text-edit / code-change / architecture). Order by frequency × severity.
`optimize` consumes this output. The user just fixing the top cluster by hand
is success too. End by asking which clusters are real and which mean the case
label is wrong. Label corrections feed back to the dataset (bump
`dataset_version`).

For a hands-on look, build the viewer:
`${CLAUDE_PLUGIN_ROOT}/scripts/build_review_viewer.py <run-path> [-a <annotations.jsonl>] -o <out.html>`
— add `--glob 'cases/*/verdict.json'` for `run`'s nested records. On a
`--full` or `--holdout` run, stage a filtered copy that excludes the sealed
ids instead of pointing it at `cases/`. Both, and why: annotation-ux.md
§"Which files it reads", worth reading before the first invocation. Its
§"Theoretical-saturation stopping rule" owns the cadence — ≥100 traces, and
stop a sitting after ~20 that add nothing. `--label` runs on that same rule.

Zero gating failures → produce insight, not silence:
- what the passing suite CANNOT detect: unjudged dimensions, layers skipped
  this run, coverage gaps named in the dataset metadata;
- the non-gating / tracked-only results worth a look;
- the next investment: labeling, cases in a named gap, or a skipped layer;
- an offer to open-code a sample of PASSING traces in the viewer. A passing
  suite is not the same as a good one. That is how the issues a green suite
  cannot see get caught.

### Across runs, not just this one
`${CLAUDE_PLUGIN_ROOT}/scripts/run_history.py reports/ [-o reports/history.json]`
— every other comparison here is pairwise, so five consecutive "within noise"
diffs can hide a ten-point erosion. It groups `reports/` into comparable series
(`run/SKILL.md` §4's diff rules, plus the split and the enabled layers), flags a
monotone drift per metric, and journals each case's verdict history. Quote
`drift_flagged` and the journal's transitions, never a "better"/"worse" — that
is `stats.py`'s call on a specific pair, and this script deliberately makes
none. Read `excluded_runs` first: a run without `summary.status: ok` is not in
the series, which is usually why a series looks short.

### What it cost, beside whether it worked
`${CLAUDE_PLUGIN_ROOT}/scripts/score_cost.py <run> [--baseline <run>]
[--prices <state>/prices.json] [-o reports/cost.json]` — `stats.py` compares
binary verdicts only, so "2% better but 3× more expensive" reads there exactly
like a free win. This prices the tokens the trace already recorded and
summarizes latency as order statistics; with `--baseline` it adds the paired
diff (an exact sign test, an exact distribution-free interval for the median
difference, and an exact permutation test on the total — no bootstrap, per
`stats.py`'s no-approximation rule).

**It is not a second gate.** There is one definition of "better" in this
package and it is `stats.py`'s keep rule; cost is not in it and there is no
budget anywhere. Report the two side by side and let the user make the trade.
Read `by_verdict_change.unchanged` — that is the spend that bought nothing
measurable.

**The price table is declared, never guessed.** Prices go stale silently, so
nothing is bundled: write `<state>/prices.json` (shape in the script's
docstring; `as_of` and `source` are required) and match `gen_ai.request.model`
exactly, adding an `aliases` entry rather than hoping a prefix matches. A model
the table does not name withholds the **dollars** and keeps the **tokens**.
A **trace-less app cannot be priced at all** — `cases/<id>/trajectory.json` is
the only token source — and its latency block is still complete. Quote
`declared_limits` with any figure: cached tokens are not accounted for, TTFT is
not measured, and at `k > 1` the cost is one representative repeat's.

## `--label` — judge calibration, 15 minutes at a time
The 15 minutes is the **sitting**, not the total: labeling runs as repeated
short sessions until the numbers hold, so optimize for low friction per case.
Run rubric-format.md's two passes: discovery, ~30 cases, then a stratified
validation pass of ~100–200. Take every number from there — TPR and TNR and
Cohen's κ, never raw accuracy, ≥90% on a slice the judge prompt never saw.
Launch the `judge` agent with profile.yaml's `judge.model` as the model
override (agents/judge.md). Calibration must measure the model runs will use.

The judge executes one rubric node per call, so label **per node**. A
case-level agree/disagree cannot say which criterion drifted. Per case:
1. Show: user message → app answer → the tool results it drew on (compact).
2. Show that node's provisional verdict, its quoted evidence and its one-line
   reason. `unknown` is signal, not a skip — that criterion is usually
   ambiguous for the case.
3. Ask: agree / disagree / "the rubric is wrong here".
4. On "rubric is wrong": edit the rubric NOW, bump its version, mark prior
   labels for a relabel-check. Split the validation labels **20/40/40** —
   few-shot examples, judge-prompt iteration, and one touched once at the end
   for the final number. That split is honest only because the prompt was never
   tuned against it. Every disagreement you resolve on it silently converts it
   into a training split.

Do not compute the numbers yourself. Append one JSONL line per labelled node —
`{case_id, rubric_id, node_id, label, judge_label, critique, reviewer, ts}`
(annotation-ux.md §"The calibration fields") — then run
`${CLAUDE_PLUGIN_ROOT}/scripts/score_agreement.py <annotations.jsonl> --write
<state>/judge/calibration.json`. That sidecar is what the runner reads; a
`calibrated` profile with no sidecar behind it no longer opens the judged gate.

Record the result **per rubric first**: `status.calibrated` and that rubric's
own numbers **as the scorer computed them**, in its own file (rubric-format.md
§File shape). Until it flips its
verdicts stay watermarked `PROVISIONAL`, and `optimize` refuses to target it.
Then roll the aggregate into profile.yaml's `judge.calibration`. Never set
`judge.status` by hand — it is derived, and one uncalibrated active rubric
holds the whole harness at `uncalibrated`
(`${CLAUDE_PLUGIN_ROOT}/skills/discover/references/profile-schema.md`
§`judge:`). Same-family judging is accepted consciously or not at all
(`judge.same_family_accepted: true`). When you record that flag, say that the
required cross-family spot-check audit comes with it (agents/judge.md). It is
not a surprise obligation to discover later.

Canaries run in every batch, and one scoring wrong voids that batch's numbers
(rubric-format.md §"The canary guard"). Recalibrate on that doc's cadence, with
10–20 fresh outlier cases. An asynchronous pass — a domain arbiter without
Claude Code open — takes the viewer instead, built with
`-a <calibration.jsonl>`. Its export panel produces exactly the JSONL this
flow consumes.

## `--mine` — real traffic → dataset (stage: traffic+)
Query the trace backend for recent conversations (adapter `traces` config).
Surface: failures users hit, routing flip-flops, tool loops, latency outliers,
and ordinary conversations unlike anything in the dataset (embedding distance).
Per candidate: show it, then propose the case with expected values from what
SHOULD have happened. When the adapter declares `may_contain_pii`, redact
before anything reaches a dataset file and show the redacted version for
approval. Origin: `real-trace:<id>`. Real-seeded cases progressively retire
synthetic-only ones. A trace already annotated through the viewer is most of
this review already done — reuse it (annotation-ux.md, "the flywheel").

## `--unseal` — holdout access, deliberately inconvenient
Warn that looking spends the holdout's validity, append one JSON line —
`{"date", "mode": "unseal", "reason": "<why you looked>"}` — to the ledger,
then show what was asked. After 5 recorded looks, require a reseal. Rewrite
each viewed case's `split` from `holdout` to `full`, flip fresh unviewed cases
the other way, and bump `dataset_version`.

The ledger is a **`.jsonl` sidecar** beside the dataset
(`datasets/holdout-looks.jsonl`, from the run plan's `paths.holdout_ledger`),
**not** the dataset YAML — `${CLAUDE_PLUGIN_ROOT}/docs/runner-contract.md` §6
owns why. N=5 counts **looks**, not unseals: `run --holdout` and `run --full`
append to this same file (`${CLAUDE_PLUGIN_ROOT}/skills/run/SKILL.md` §4). Take
the running total by **counting its lines**, not the entries this skill wrote,
or the optimizer's repeated holdout gates spend the seal invisibly.

## Re-validate after any case edit
All three branches edit case files. `--cluster` ends in label corrections,
`--mine` writes new cases, and `--unseal` rewrites `split` on both the cases it
reseals and the ones it flips in. Those are exactly the edits `generate` §4
validates before handing a dataset over — done here later, by hand, with no
validation step in sight. So run the same check, after the edit and before the
next run:

```
${CLAUDE_PLUGIN_ROOT}/scripts/validate_cases.py --cases <suite.json> \
  --capabilities <profile.capability_matrix.json> --manifest <dataset-fields.json>
```

You convert the cases and both JSON inputs from YAML; the script takes JSON on
purpose (its docstring says why), and case-format.md owns the rules it
enforces. `--manifest` matters most here: it catches the `dataset.yaml` counts
that a reclassification, a deleted case or a reseal leaves stale. A reseal must
also come back clean on `holdout_not_sealed` and `missing_split`, the only
mechanical evidence the seal still holds.

## Rubric editing (any time)
Rubrics live in `rubrics/*.md` under the state location (default
`.agent-eval/`, or the adapter's `state_location`), versioned. Editing one
while grading is the expected workflow, not a detour. Criteria drift is what
grading real cases discovers. Bump the version and record the motivating
finding in `calibration_notes`. Runs record rubric versions, and a diff across
two of them is refused. Node kinds, ordering, the canary guard, the authoring
procedure and the file shape: rubric-format.md.
