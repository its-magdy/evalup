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
  agent on zero cases; point at `${CLAUDE_PLUGIN_ROOT}/docs/rubric-format.md`
  (see also `${CLAUDE_PLUGIN_ROOT}/agents/judge.md`) for authoring the first
  rubric.
- **`--mine` finds zero candidate traces** → distinguish "adapter not wired
  to a trace backend yet" from "stage is pre-`traffic`, so mining isn't
  expected to produce anything" from "wired and eligible, genuinely nothing
  new" — three different situations that all look like an empty list if you
  don't say which one happened.
This is the same discipline as `run/SKILL.md` §4's first-run branch, which
treats a missing `reports/baseline.json` as a branch rather than an error:
it skips the diff, pins this run as the baseline, and prints "baseline
established" — after first ruling out the old-layout case, where a legacy
`baselines/`/`runs/` directory means a baseline really does exist and
"first run" would silently discard it. Same shape here: name the empty prior
state, and rule out the version of it that only *looks* empty (a run
directory that exists but is all-`infra_error`, an adapter with no trace
backend wired) before calling it a genuine start. An empty prior state is a
normal milestone to name, not a failure to paper over or a silence to leave
the user parsing.

## `--cluster` (default after a run)
Read the latest run's failed cases (never the holdout details) from
`reports/<run-id>/`. Pick the latest by the **timestamp segment** of the run id
(`<mode>-<YYYYMMDDTHHMMSSZ>`, see `run/SKILL.md` §1) — sort on what follows the
first `-`, not on the whole string, since a whole-string lexical sort ranks by
mode first and would hand you the newest `smoke-*` over a much later `full-*`.
Use the timestamp rather than mtime, which drifts when a report is regenerated
or a directory copied. For each failed case, launch the `trace-analyzer` agent
on its normalized trajectory (plus raw spans when the normalized view is
insufficient) and its violated expectations — it reconstructs the actual path,
locates the first divergence, and attributes it to a surface, one trace at a
time. Group its outputs by failure mode, not by metric: same wrong-route pair,
same tool confusion, same rule violation, same missing-clarification. For each
cluster: count, 2 exemplar traces (expected vs actual side by side), implicated
surface, effort tag (text-edit / code-change / architecture). Order by
frequency × severity. This output is what `optimize` consumes — and often the
user just fixes the top cluster by hand, which is success too. For a hands-on
look before or instead of the generated report, build the interactive viewer:
`${CLAUDE_PLUGIN_ROOT}/scripts/build_review_viewer.py <run-path> [-a <annotations.jsonl>] -o <out.html>`
— one self-contained HTML file with the span tree, expected-vs-actual diff,
per-stage cost, and a hotkeyed pass/fail+critique bar per trace (see
[references/annotation-ux.md](references/annotation-ux.md) for the full
open-coding → axial-coding workflow this drives). Since `run` nests its records
(`cases/<case-id>/verdict.json`), you will need
`--glob 'cases/*/verdict.json'`;
[references/annotation-ux.md](references/annotation-ux.md) §"Which files it
reads" owns why, and is worth reading before the first invocation. The one
thing that section does not cover is the seal: `--glob` takes one pattern with
no exclude syntax, so **on a `--full` or `--holdout` run, don't point it at
`cases/` directly** — first stage a filtered copy: symlink every non-holdout
`cases/<case-id>/verdict.json` (cross-referencing the case files to find the
ids whose `split` contains `holdout`, so you know which to skip) into a scratch
directory, then `--glob` that directory instead. Holdout
`request.json`/`response.json` are exactly the sealed content `run/SKILL.md` §4
keeps out of the aggregate report and `results.json`'s per-case rows — pointing
the viewer at `cases/` unfiltered renders them anyway, silently spending a look
against the N=5 reseal budget without going through `--unseal`. Cadence: review
**≥100 traces** before treating a failure taxonomy as discovered, and stop any
single sitting once **~20 consecutive traces** add no category you haven't
already written down. Fewer and you're generalizing from anecdotes; more with
nothing new is time better spent labeling or writing regression cases. This is
one rule, not three — `--label`'s calibration cadence and
[references/annotation-ux.md](references/annotation-ux.md)
§"Theoretical-saturation stopping rule" (which owns it) are the same rule
pointed at different traffic. End by asking: which clusters are "real" vs "the
case label is wrong"? Label corrections feed back to the dataset (bump
dataset_version).

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
Goal: judge-human agreement you can defend, measured as **TPR and TNR and
Cohen's κ — never raw accuracy** (imbalanced data makes an always-pass judge
look 95% accurate while catching 0% of real failures). The 15 minutes is the
**sitting**, not the total: labeling runs as repeated short sessions until the
numbers below hold, which is why the flow optimizes for low friction per case
rather than for finishing in one go. Two passes, per
`${CLAUDE_PLUGIN_ROOT}/docs/rubric-format.md`'s calibration workflow — don't
collapse them, the first discovers criteria and the second measures them:
- **Discovery, ~30 cases.** Hand-label per node, write a critique on every
  disagreement, open-code them into named categories. Numbers from this pass
  are not the calibration numbers — the rubric is still moving under them.
- **Validation, ~100–200 cases, stratified.** Oversample the rare failing
  class (most traffic passes), keep some random cases so the sample isn't only
  edge cases, and measure TPR/TNR/κ on this pass. Reviewing ≥100 cases before
  calling a rubric "discovered" is the same saturation rule `--cluster` states
  above. Launch the `judge` agent with profile.yaml's `judge.model` as the
  model override (its frontmatter is only the fallback default) — calibration
  must measure the model runs will actually use. Flow per case (one at a time,
  low friction) — the judge executes one rubric node per call
  (`${CLAUDE_PLUGIN_ROOT}/agents/judge.md`), so label **per node**, not one
  verdict per case; a case-level agree/disagree can't tell you which criterion
  drifted:
1. Show: user message → app answer → tool results it drew on (compact).
2. Show the judge's provisional per-node verdict, its quoted evidence, and
   its one-line reason. A node answered `unknown` is signal, not a skip — it
   usually means the criterion is ambiguous for that case.
3. Ask: agree / disagree / "the rubric is wrong here".
4. On "rubric is wrong": edit the rubric NOW (criteria drift is expected and
   normal — grading is what defines criteria), bump rubric version, mark prior
   labels for relabel-check. Split the validation labels 20/40/40 (few-shot
   examples for the judge prompt / iterate the judge prompt / touched once, at
   the end, for the final agreement number). The last split is the only honest
   number precisely because the prompt was never tuned against it — every
   disagreement you resolve on it silently converts it into a training split.
   Treat every disagreement as a candidate **rubric** edit before a
   judge-prompt tweak: check whether the node's criterion was ambiguous for
   that case before concluding the judge was wrong. When TPR and TNR are both
   ≥90% on that untouched split, write the result to the rubric — **not** to
   profile.yaml's `judge.status`, which is *derived, never set by hand*: it
   reads `calibrated` only once EVERY active rubric is individually calibrated,
   and one uncalibrated active rubric holds the whole harness at `uncalibrated`
   (`${CLAUDE_PLUGIN_ROOT}/skills/discover/references/profile-schema.md`
   §`judge:`). So:
1. **Per-rubric first.** Record `status.calibrated: true` plus this rubric's
   own tpr/tnr/kappa/labeled_cases/last_checked in the rubric file
   (`${CLAUDE_PLUGIN_ROOT}/docs/rubric-format.md` §File shape). Until that
   flips, every verdict the rubric produces stays watermarked `PROVISIONAL` and
   `optimize` refuses to target it. Then roll the aggregate into profile.yaml's
   `judge.calibration` block (`labeled_cases`, `tpr`, `tnr`, `kappa`,
   `last_checked`) and let `judge.status` follow from whether any active rubric
   is still short.
2. **Canaries in every batch.** Every calibration batch runs a known-good
   decline and a known-bad fabrication. One scoring wrong means judge or
   harness drift: void that batch's numbers and re-check before they count
   toward anything.
3. **Model family.** If the effective judge model and the app are both
   Claude-based, the user must consciously accept the self-preference bias
   (`judge.same_family_accepted: true`), never silently — and same-family mode
   comes with a required periodic cross-family spot-check audit
   (`${CLAUDE_PLUGIN_ROOT}/agents/judge.md`), not an optional one. Say that
   when you record the flag, so it isn't discovered later as a surprise
   obligation. Recalibration: every 2–4 weeks, immediately if a canary starts
   failing, and on any model/prompt/traffic-distribution change — a rubric
   calibrated against last quarter's traffic mix is not calibrated against this
   quarter's. 10–20 fresh outlier cases. Stop any single sitting once ~20
   consecutive cases add no category you haven't already written down. This is
   **open coding → axial coding** in miniature: the first pass over each node
   (agree/disagree/critique) is open coding — free-text judgment, categories
   not required yet. Once patterns repeat, name them and the disagreements
   start clustering into a small taxonomy — that's axial coding, and it's the
   same clustering `--cluster` does for failures and the viewer's sidebar does
   live. For a faster or asynchronous version of this same flow (e.g. a domain
   arbiter without Claude Code open), generate the interactive viewer instead:
   `${CLAUDE_PLUGIN_ROOT}/scripts/build_review_viewer.py <run-path> -a
   <calibration.jsonl> -o <out.html>` —
   same one-keystroke pass/fail, same critique box, same taxonomy sidebar, and
   its export panel produces exactly the JSONL this calibration pass consumes.
   Full workflow, stopping rules, and the critique → few-shot/regression-case
   flywheel: [references/annotation-ux.md](references/annotation-ux.md).

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
from scratch (see the flywheel section of [references/annotation-ux.md](references/annotation-ux.md)).

## `--unseal` — holdout access, deliberately inconvenient
Warn ("looking at holdout cases spends their validity"), log the access with
date and reason into the dataset metadata, show what was asked, and after 5
recorded looks require a reseal: rewrite each viewed case's `split` from
`holdout` to `full`, flip fresh unviewed cases the other way, bump
dataset_version. The N=5 counts **looks**, not just unseals: a `run --holdout`
or `run --full` appends to this same ledger
(`${CLAUDE_PLUGIN_ROOT}/skills/run/SKILL.md` §4), so read the ledger's running
total rather than counting only the entries this skill wrote — otherwise the
optimizer's repeated holdout gates spend the seal invisibly.

## Rubric editing (any time)
Rubrics live in `rubrics/*.md` under the state location (default
`.agent-eval/`, or the adapter's `state_location`), versioned. A rubric is a
**decomposed-binary DAG**, not a list of criteria and not a holistic score:
binary gates ("cites the invoice id used: yes/no") with one dimension per node
and an explicit `unknown` escape, ordered so the evidence and correctness gates
sit upstream of any presentation scoring. The one graded node kind
(`GEvalNode`, frozen `evaluation_steps`) is terminal and reachable only after
every gate above it passes — a nicely-worded hallucination must not be able to
outscore a hedged, evidence-backed answer. Before adding any node, ask whether
a deterministic script could check it instead; if the criterion is really
"matches this regex," it belongs in `answer.must_contain`/`rules`, not in the
DAG. Full spec — node kinds, authoring procedure, canary guard, file shape:
`${CLAUDE_PLUGIN_ROOT}/docs/rubric-format.md`. Editing a rubric bumps its
version and records the finding that motivated it in `calibration_notes`; runs
record rubric versions; diffs across rubric versions are refused.
