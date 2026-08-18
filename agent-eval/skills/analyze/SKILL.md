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

## First run — no prior data
`analyze` reads the latest run's output and the dataset. Before assuming
either exists, check — and if not, say so plainly and name the actual next
step instead of clustering, labeling, or mining an empty set:
- **No run has ever completed** (no `reports/<run-id>/` directories, or the latest is
  interrupted/all-`infra_error`) → nothing to cluster or label yet. Say that,
  and point at `/agent-eval:run` (or `/agent-eval:start`, which routes to
  whichever of discover/generate/run hasn't happened yet — don't guess which
  is missing, ask or check `profile.yaml`/`datasets/`).
- **A run exists but no rubric/judge is configured** (`judge.status` absent
  or no `rubric:` referenced by any case) → `--label` has nothing to
  calibrate against yet. Say that plainly rather than launching the judge
  agent on zero cases; point at `docs/rubric-format.md` (see also `agents/
  judge.md`) for authoring the first rubric.
- **`--mine` finds zero candidate traces** → distinguish "adapter not wired
  to a trace backend yet" from "stage is pre-`traffic`, so mining isn't
  expected to produce anything" from "wired and eligible, genuinely nothing
  new" — three different situations that all look like an empty list if you
  don't say which one happened.
This is the same discipline as `run/SKILL.md`'s first-run branch (pin the
baseline automatically instead of diffing against nothing exists) — an empty
prior state is a normal milestone to name, not a failure to paper over or a
silence to leave the user parsing.

## `--cluster` (default after a run)
Read the latest run's failed cases (never the holdout details). Group by
failure mode, not by metric: same wrong-route pair, same tool confusion, same
rule violation, same missing-clarification. For each cluster: count, 2
exemplar traces (expected vs actual side by side), implicated surface, effort
tag (text-edit / code-change / architecture). Order by frequency × severity.
This output is what `optimize` consumes — and often the user just fixes the
top cluster by hand, which is success too.
For a hands-on look before or instead of the generated report, build the
interactive viewer: `${CLAUDE_PLUGIN_ROOT}/scripts/build_review_viewer.py
<run-path> [-a <annotations.jsonl>] -o <out.html>` — one self-contained HTML
file with the span tree, expected-vs-actual diff, per-stage cost, and a
hotkeyed pass/fail+critique bar per trace (see `references/annotation-ux.md`
for the full open-coding → axial-coding workflow this drives). If the run
nests its records (`cases/<case-id>/verdict.json`), add `--glob
'cases/*/verdict.json'` — a bare run root holds manifest/canary artifacts, not
cases, and the viewer will say so rather than render them. `--glob` takes one
pattern with no exclude syntax, so **on a `--full` or `--holdout` run, don't
point it at `cases/` directly** — first stage a filtered copy: symlink every
non-holdout `cases/<case-id>/verdict.json` (cross-referencing `datasets/
holdout/` case ids to know which to skip) into a scratch directory, then
`--glob` that directory instead. Holdout `request.json`/`response.json` are
exactly the sealed content `run/SKILL.md` §4 keeps out of the aggregate
report and `results.json`'s per-case rows — pointing the viewer at `cases/`
unfiltered renders them anyway, silently spending a look against the N=5
reseal budget without going through `--unseal`.
End by asking: which clusters are "real" vs "the case label is wrong"? Label
corrections feed back to the dataset (bump dataset_version).

Zero gating failures → nothing to cluster; produce insight, not silence:
- what the passing suite CANNOT yet detect: unjudged dimensions, layers
  skipped this run, known coverage gaps from the dataset metadata;
- the non-gating / tracked-only results worth a look;
- the suggested next investment: labeling (judge calibration), more cases in
  a named gap, or unlocking a skipped layer;
- a passing suite is not the same as a good one — offer to open-code a
  sample of PASSING traces through the viewer above (free-text critique, no
  categories yet). This is how silent quality issues a green suite can't see
  get caught, and it is the same open-coding discipline `--label` and
  `--mine` both use, just pointed at passing traffic instead of failures.

## `--label` — judge calibration, 15 minutes at a time
Goal: ~30 labeled cases, then TPR/TNR vs the judge, target >90% agreement
measured as TPR AND TNR (never raw accuracy — imbalanced data makes an
always-pass judge look 95% accurate). Launch the `judge` agent with
profile.yaml's `judge.model` as the model override (its frontmatter is only
the fallback default) — calibration must measure the model runs will
actually use.
Flow per case (one at a time, low friction):
1. Show: user message → app answer → tool results it drew on (compact).
2. Show the judge's provisional verdict + one-line reason.
3. Ask: agree / disagree / "the rubric is wrong here".
4. On "rubric is wrong": edit the rubric NOW (criteria drift is expected and
   normal — grading is what defines criteria), bump rubric version, mark prior
   labels for relabel-check.
Split labeled cases 20/40/40 (few-shot for judge prompt / iterate judge
prompt / touched once for the final agreement number). When agreement ≥90%
on the test split: before setting `judge.status: calibrated`, check the judge's
model family against the app's — same family requires the user to consciously
accept the self-preference bias risk (`judge.same_family_accepted: true`),
never silently. Then set `judge.status: calibrated` in profile.yaml, record
TPR/TNR + date. Recalibration: every 2–4 weeks or when canaries drift — 10–20
fresh outlier cases.
Cadence guidance to give the user: ≥100 traces for a first error analysis;
stop a review session when ~20 consecutive traces show no new failure
category.
This is **open coding → axial coding** in miniature: the first pass through
each case (agree/disagree/critique) is open coding — free-text judgment,
categories not required yet. Once patterns repeat, name them and the
disagreements start clustering into a small taxonomy — that's axial coding,
and it's the same clustering `--cluster` does for failures and the viewer's
sidebar does live. For a faster or asynchronous version of this same flow
(e.g. a domain arbiter without Claude Code open), generate the interactive
viewer instead: `build_review_viewer.py <run-path> -a
<calibration.jsonl> -o <out.html>` — same one-keystroke pass/fail, same
critique box, same taxonomy sidebar, and its export panel produces exactly
the JSONL this calibration pass consumes. Full workflow, stopping rules, and
the critique → few-shot/regression-case flywheel: `references/
annotation-ux.md`.

## `--mine` — real traffic → dataset (stage: traffic+)
Query the trace backend for recent conversations (adapter `traces` config).
Surface: failures users hit, routing flip-flops, tool loops, latency
outliers, and ordinary conversations unlike anything in the dataset
(embedding distance). For each candidate: show it, propose the case
(expected values from what SHOULD have happened, not what did), and — if
`may_contain_pii` — run redaction before anything is written to a dataset
file and show the redacted version for approval. Origin: `real-trace:<id>`.
Real-seeded cases progressively retire synthetic-only ones. A trace already
annotated through the viewer (category + critique on file) is most of this
review already done — reuse that annotation instead of re-reading the trace
from scratch (see the flywheel section of `references/annotation-ux.md`).

## `--unseal` — holdout access, deliberately inconvenient
Warn ("looking at holdout cases spends their validity"), log the access with
date and reason into the dataset metadata, show what was asked, and after 5
recorded unseals require a reseal: move viewed cases into full/, promote
fresh unviewed cases in, bump dataset_version.

## Rubric editing (any time)
Rubrics live in `rubrics/*.md` under the state location (default
`.agent-eval/`, or the adapter's `state_location`), versioned, binary criteria only
("cites the invoice id used: yes/no"), one dimension per criterion, an
explicit "Unknown" escape for the judge. Editing a rubric bumps its version;
runs record rubric versions; diffs across rubric versions are refused.
