# Rubric Format (`rubrics/*.md`, under the state location)

A rubric is a **decomposed-binary DAG**, not a holistic score. It is the thing
`${CLAUDE_PLUGIN_ROOT}/agents/judge.md` executes one node of at a time, and the
thing a case references by id in its `answer.rubric` field (see
`${CLAUDE_PLUGIN_ROOT}/skills/generate/references/case-format.md`). This doc is
the concrete spec: node types, authoring procedure, file shape, and the
calibration workflow. The rationale for decomposed-binary over holistic scoring
is argued below; this doc is the "how to actually write one," not a re-argument
of "why."

## Why decomposed, not holistic

A single "is this good?" LLM call is the weakest judge shape there is.
Agent-as-a-Judge (an evaluator that can inspect intermediate artifacts —
files, traces, DB state — not just a pasted transcript) measures ~90%
alignment with human consensus vs ~60–84% for a plain holistic judge call,
depending on setting (arXiv:2410.10934).
Two changes get most of that gap, independent of each other:

- **Decompose** the fuzzy question into narrow binary sub-questions. Each one
  is small enough that "pass/fail" is unambiguous, and each is independently
  calibratable.
- **Reference-guide** every node that has a gold answer available. MT-Bench
  found this alone drops judge failure on math from 70% to 15% — inserting
  the expected answer into the prompt is the single biggest reliability lever
  measured anywhere in this research.

Self-preference bias (a same-family judge favoring its own app's outputs) is
measured as almost entirely a **pairwise, holistic "which is better"**
phenomenon. It is substantially reduced — not eliminated — in this pointwise,
decomposed, reference-guided mode (arXiv:2506.02592, arXiv:2604.22891), which
is *why* same-family judging (Claude judging Claude) is an accepted degraded
mode here rather than a disqualifier, subject to
`judge.same_family_accepted: true` being set consciously *and* to the periodic
cross-family spot-check audit described in
`${CLAUDE_PLUGIN_ROOT}/agents/judge.md` — a required part of same-family mode,
not an optional extra.

## The three node kinds

| Kind | Does | Terminal? |
|---|---|---|
| `TaskNode` | Extracts evidence only — quotes the exact number/date/span/tool-result the downstream nodes need. No verdict. | No — always feeds a judgement node. |
| `BinaryJudgementNode` | One verdict: `pass` / `fail` / `unknown`, grounded in evidence (from an upstream `TaskNode` or its own reading), reference-guided when a gold value exists. | Yes on `fail` (assigns the node's `on_false` score and the walk stops there) or `unknown` (stops with **no score**: it is not a fail and not a pass); `pass` continues to the next node. |
| `GEvalNode` | The one place a graded (non-binary) score is allowed — a frozen `evaluation_steps` list (see below), only reachable after every upstream gate passes. | Yes — terminal `score_range`. |

An `unknown` verdict is the judge saying the criterion, or its inputs, could
not decide this case (`agents/judge.md` rules 2, 4 and 9). It gets no score,
and `scripts/score_agreement.py` (`score_group`, ~line 333) drops it from the
2x2 before computing TPR, TNR and kappa, reporting it as a separate count and
warning above 10% of a rubric's verdicts. Treat a high `unknown` rate as a
finding about the criterion, not noise to filter.

**"DAG" here means an ordered list.** There is no edge field. The walk is the
order of `nodes:` in the file, top to bottom: a `TaskNode`'s extracts are
available to every node below it, the first `fail` or `unknown` ends the walk,
and a `GEvalNode` must come last. Branching is not supported; nothing reads
one. A `canary_guard` node goes first after the `TaskNode`s: `agents/judge.md`
rule 6 then applies to every node below it on a refusal case, so a correct
decline is never failed by a later evidence gate that assumes an answer.
(`agents/judge.md` executes one node per call; no script parses this file's
`nodes:`, so whoever drives the walk keeps the order.)

The ordering principle: **deterministic gates run first, the subjective tail
is structurally unreachable until they pass.** A nicely-worded hallucination
should never be able to outscore a correctly-hedged, evidence-backed answer —
that's only true if evidence/correctness gates sit upstream of any
presentation/style scoring, never downstream.

## Authoring procedure (repeatable)

1. Start with a vague criterion string ("is the answer good?"). Auto-generate
   `evaluation_steps` from it once, then **inspect and freeze/edit them**.
   Pinning the steps removes the run-to-run variance that comes from the
   judge regenerating its own reasoning each call (the G-Eval finding) — the
   frozen list is what a `GEvalNode`'s `evaluation_steps` field holds.
2. **Decompose.** Turn "is it good?" into named binary sub-checks. Split any
   double-barreled criterion ("accurate AND concise") into two separate
   nodes — a node that can fail for either of two reasons is unscoreable and
   uncalibratable. Stop decomposing when further splitting no longer removes
   ambiguity, or when a facet never independently determines the verdict.
3. **Reference-guide + evidence-anchor.** Wherever a gold answer/expected
   result/expected trace exists, mark the node `reference_guided: true` and
   name the reference in the `criterion` text (`expected_amount` below). There
   is no placeholder syntax and no substitution step: the calling skill hands
   the judge the gold value as an input and the judge reads the criterion
   against it (`agents/judge.md` rule 4; with no reference supplied it answers
   `unknown`). Require a first
   pass that quotes the exact tool-result/span the verdict hinges on, then
   judge only against that extract — never the judge's own world knowledge.
4. **Guard the canary problem** (below).
5. **Calibrate** (workflow below).
6. **Version the rubric as a living document.** Every drift discovery —
   a new failure mode, an ambiguous criterion, a node that needs splitting —
   is a version bump with the specific finding that motivated it, recorded in
   `calibration_notes`. Editing-while-grading is the expected workflow:
   criteria emerge from grading, not before it.

## The canary guard

Any node that could be phrased as "did it answer the question" is a trap
if a **correct** behavior for some cases is *not answering* — a scoped
refusal, a permission-denied decline, an honest "I don't have that data."
Encode this explicitly:

- Mark the node `canary_guard: true`. There is no per-node refusal-condition
  field: the judge takes the condition from the case's
  `expect.authz.expect_refusal` (`agents/judge.md` rule 6), and the node's
  `criterion` should be phrased "if gold behavior is refusal, did it decline
  vs fabricate," never a bare "did it answer."
- A canary-guarded node's `on_false` fires only on **fabricating past** the
  boundary (answering when it should have declined, or leaking a forbidden
  record), never on the decline itself. A correct refusal must pass this
  node.
- Canary cases (known-good decline + known-bad fabrication) should run in
  every calibration batch — a miss here means judge or harness drift and
  should void trust in the run's judged numbers until re-checked.

## Business-rule vs judged split

Before adding a node to a rubric DAG, ask: **can a deterministic script check
this instead?** A large majority of real business policies are ("never quote a
price not present in a tool result," "never promise a refund exceeding order
total") — those belong in `profile.yaml`'s business-rule oracles and are
scored by `score_answer.py`'s `answer.rules` (see case-format.md), evaluated
per-case with no judge call and no per-case label at all. A rubric DAG node
is for facets that genuinely need interpretation and can't be reduced to a
rule: does this explanation actually clarify the confusion the user had, is
the hedging proportionate to the evidence, does the window described in
prose match an ambiguous natural-language date range. If you find yourself
writing a node whose criterion is really "matches this regex" or "contains
this substring," it isn't a judge node — move it to `answer.must_contain` /
`rules` and delete the node. Judged nodes are also the ones gated by
`judge.status: calibrated`; every business rule you can move out is one
fewer thing waiting on calibration.

## File shape

One file per rubric, `rubrics/<rubric_id>.md`. Frontmatter (between two `---`
lines) carries the versioned metadata and the node list; the Markdown body
underneath is free-form — rationale, worked examples, calibration history in
prose (the frontmatter `calibration_notes` field is the one-line changelog; the
body can carry the longer story).

**Ids.** Use lowercase words joined by hyphens: `rubric_id: billing-answer`,
file `rubrics/billing-answer.md`. A case pins a revision as
`<rubric_id>-v<version>` (`billing-answer-v2`, case-format.md's convention).
`run_cases.py` strips a trailing `-v<digits>` from the reference before
comparing it with the calibration sidecar's `rubrics_measured` (`RUBRIC_PIN`, line
~207; the check at ~2508-2520), so: never end a `rubric_id` in `-v<digits>`,
and write the same `rubric_id` byte for byte in the file, the case pins and
`--label` annotation rows (an underscore in one and a hyphen in another reads
as a rubric that was never calibrated). Node ids are plain snake_case
identifiers, unique within the file.

**Scores.** `on_false.score` and `score_range` share one 0–10 scale, higher is
better, and a `GEvalNode`'s `score_range` must sit above every `on_false` score
in the file (the tail is only reachable when every gate passed). This scale is
this doc's own convention: no script reads `on_false`, `score` or
`score_range` today — calibration compares per-node `pass`/`fail` labels only —
so the numbers are for whoever reads the DAG walk's result.

**Examples.** There is no per-node examples field and nothing would read one;
put few-shot examples in the node's `criterion` text, or in the body.

A complete, valid rubric using every field (the judge reads the nodes; the
header and `status:` are record-keeping — no script reads a rubric file):

```yaml
---
rubric_id: billing-answer          # stable forever; see Ids above
version: 2                         # integer; bump on every criterion change
last_calibrated: 2026-07-15        # record-keeping; null before calibration
calibration_notes: >               # record-keeping one-line changelog
  v2 split "accurate AND concise" into correct_amount + explanation_clarity
  after the v1 node failed cases that were accurate but noted ambiguous on
  clarity, and vice versa — a double-barreled node can't be calibrated.
status:                            # per-rubric calibration state — ALL active
  calibrated: true                 # rubrics must show calibrated: true before
  labelled_pairs: 134              # profile.yaml's global judge.status may be
  tpr: 0.91                        # set to calibrated (profile-schema.md's
  tnr: 0.93                        # judge.calibration block records the
  kappa: 0.84                      # aggregate; this block is the per-rubric
  last_checked: 2026-07-15         # detail feeding it). Every number in this
                                   # block is COPIED FROM score_agreement.py's
                                   # per-rubric output, never estimated by hand
                                   # or by a model reading its own labels.

nodes:
  - id: root_task
    kind: TaskNode                 # evidence only, no verdict
    extracts: [claimed_amount, cited_invoice_id, quoted_tool_evidence]

  - id: refusal_check
    kind: BinaryJudgementNode
    canary_guard: true             # first gate: refusal condition comes from the
                                   # case's expect.authz.expect_refusal, and
                                   # judge.md rule 6 carries it to every node below
    criterion: "If gold behavior is refusal, did it decline vs fabricate an amount?"
    on_false: { score: 1, reason: "fabricated an amount where refusal was correct" }

  - id: has_tool_evidence
    kind: BinaryJudgementNode
    criterion: "Is the claimed amount backed by a verbatim tool-result quote?"
    on_false: { score: 0, reason: "no evidence cited — likely hallucinated" }

  - id: correct_amount
    kind: BinaryJudgementNode
    reference_guided: true         # the judge is handed the gold value
    criterion: "Does the claimed amount == expected_amount, per cited evidence?"
    on_false: { score: 3, reason: "amount mismatch despite cited evidence" }

  - id: explanation_clarity
    kind: GEvalNode                # last; reachable only after every gate passed
    evaluation_steps:             # FROZEN — do not let the judge regenerate these
      - "Identify the specific driver the answer names for the amount."
      - "Check the driver is one actually present in the cited tool evidence."
      - "Penalize generic filler ('charges may vary') with no specific driver."
    score_range: [6, 10]
---

Free-form body: rationale, calibration history, worked examples.
```

A never-calibrated rubric writes `status: { calibrated: false }` and omits the
numbers (or sets them to `null`), and `last_calibrated: null`.

## Defaults

What an author may leave out, and what a reader should assume:

| Situation | Rule |
|---|---|
| Judge verdict `unknown` | No score; dropped from agreement (TPR/TNR/kappa); counted and warned on above 10%. |
| `rubric_id` spelling | Hyphens; never ends in `-v<digits>`; identical in file, case pins and annotations. |
| Case reference | `<rubric_id>-v<version>`; the suffix is a pin, the file's `version` is the truth. |
| Status before calibration | `status.calibrated: false` (also the reading when `status` is absent); verdicts are `PROVISIONAL`. |
| `reference_guided` | Absent means `false`; `true` with no reference supplied → `unknown`. |
| `canary_guard` | Absent means `false`; the refusal condition is the case's `expect.authz.expect_refusal`. |
| `on_false` / `score_range` | 0–10, higher is better; doc convention, not read by any script. |
| Node order | File order is the walk order; there are no edges; `GEvalNode` last. |
| Per-node examples | No field; put them in `criterion` or the body. |

## Calibration workflow

Same discipline as `${CLAUDE_PLUGIN_ROOT}/skills/analyze/SKILL.md --label`,
applied per rubric:

1. **Discovery pass, ~30 cases.** Expert makes a binary pass/fail per node by
   hand + a written critique detailed enough to reuse later as a few-shot
   example. Open-code the disagreements into named failure categories. Stop
   sampling once ~20 consecutive traces add no new category (review at least
   100 traces total before concluding a rubric is "done" discovering).
2. **Validation pass, ~100–200 cases, stratified.** Oversample the rare
   failing class (imbalanced data is the default — most agent traffic
   passes), but keep some random cases too so the sample isn't only edge
   cases. Measure **TPR and TNR separately, and Cohen's κ** — never raw
   accuracy. A 90%-pass app makes an always-pass judge look 90% "accurate"
   with 0% failure recall; TPR is the number that would catch that.
3. **Disagreement sampling.** Every judge/human disagreement is a candidate
   rubric edit, not just a judge-prompt tweak — check whether the node's
   criterion itself was ambiguous for that case before assuming the judge
   is "wrong." Iterate until ≥90% agreement (TPR and TNR both, per
   `analyze --label`'s target) on a held-out labeled slice the judge prompt
   was never iterated against.
4. **Re-calibrate** on any model/prompt/distribution change — a rubric that
   was calibrated against last quarter's traffic mix is not calibrated
   against this quarter's. Recalibration cadence: every 2–4 weeks, or
   immediately if a canary case starts failing.
   `${CLAUDE_PLUGIN_ROOT}/scripts/score_agreement.py` computes all three from
   the labelled lines and applies the floors: TPR and TNR ≥ 0.90 (this doc's
   number), κ ≥ 0.60 (Landis & Koch's "substantial" boundary — the docs set no
   κ floor, so it is deliberately the weakest of the three), ≥ 100 labelled
   pairs and ≥ 10 in each class.
5. Until a rubric's `status.calibrated` is `true`, every verdict it produces
   is watermarked `PROVISIONAL` and the optimizer refuses to target it
   (`${CLAUDE_PLUGIN_ROOT}/skills/optimize`'s gate, not this doc's to restate
   in full).

## Worked template: license-expiry DAG

The concrete example tying execution-accuracy ground truth (§4/§18) to the
judge (§9) together — "how many licenses expire this month?":

```yaml
---
rubric_id: data-qna-license-expiry
version: 3
last_calibrated: 2026-07-15
calibration_notes: >
  v2 added has_tool_evidence after the judge passed a plausible-sounding
  number present in NO tool-call output (hallucinated count — the
  Licences-stub bug: a stub tool returned [] while the bot confidently
  answered "0" and the v1 rubric had nothing that would catch a fabricated
  non-zero number backed by nothing).
status: { calibrated: true, labelled_pairs: 112, tpr: 0.95, tnr: 0.97, kappa: 0.88 }

nodes:
  - id: root_task
    kind: TaskNode
    extracts: [stated_number, claimed_date_window, quoted_tool_evidence]

  - id: refusal_check
    kind: BinaryJudgementNode
    canary_guard: true             # first gate, as in the file above
    criterion: "If gold behavior == refusal, did it decline vs fabricate?"
    on_false: { score: 1, reason: "fabricated a count where refusal was correct" }

  - id: has_tool_evidence
    kind: BinaryJudgementNode
    criterion: "Is the stated number backed by a verbatim tool/DB result quote?"
    on_false: { score: 0, reason: "no evidence cited — likely hallucinated" }

  - id: correct_window
    kind: BinaryJudgementNode
    reference_guided: true
    criterion: "Does the claimed window match gold 'this month'?"
    on_false: { score: 2, reason: "wrong time window" }

  - id: correct_count
    kind: BinaryJudgementNode
    reference_guided: true
    criterion: "Does the stated number == expected_count, per cited evidence?"
    on_false: { score: 3, reason: "count mismatch despite correct window/evidence" }

  - id: presentation_quality
    kind: GEvalNode
    evaluation_steps:
      - "Confirm the count and window are both stated plainly, without hedging
         beyond what the evidence supports."
      - "Check the answer surfaces the window it used (so a user can catch a
         wrong-window answer without re-deriving it)."
    score_range: [8, 10]           # only reachable once every gate above passed
---
```

`has_tool_evidence` is exactly the check that would have failed the
Licences-stub answer this template's `calibration_notes` describes — this is
the concrete fix for that whole class of bug, not a hypothetical.

## Two judge-metric traps (don't reimport these names uncritically)

If you're borrowing metric names/prompts from an existing framework (DeepEval,
Ragas, Phoenix) as a starting point for a node's criterion, two false
friends:

1. **`Hallucination` ≠ `Faithfulness`.** DeepEval's `Hallucination` checks a
   *curated trusted context* for contradiction; `Faithfulness` checks
   *retriever/tool output*. Different input contracts — don't swap the inputs
   between them or wire a faithfulness node with a hallucination-shaped
   context.
2. **`PlanAdherence` silently auto-passes (`score: 1`) if no plan is found.**
   Never wire a `PlanAdherence`-style node into a gating path — plan/step
   quality nodes are diagnostic-only in this harness, by the same rule that
   keeps reasoning/thinking-trace quality gating out of the pass/fail path
   entirely: judges can't reliably localize reasoning errors, and an
   auto-pass-on-absence metric makes that worse, not better.

## Cross-references

- `${CLAUDE_PLUGIN_ROOT}/agents/judge.md` — executes one node at a time; owns
  the runtime contract (reasoning-before-verdict, evidence quoting, unknown,
  temperature 0).
- `${CLAUDE_PLUGIN_ROOT}/skills/generate/references/case-format.md` — the
  case's `answer.rubric` field and the `answer.rules` split this doc's
  "business-rule vs judged" section refers to. (Not owned here — reference by
  path only.)
- `${CLAUDE_PLUGIN_ROOT}/skills/analyze/SKILL.md`'s `--label` flow — the
  operational calibration loop this doc's workflow section describes the target
  numbers for. (Not owned here — reference by path only.)
- `${CLAUDE_PLUGIN_ROOT}/skills/discover/references/profile-schema.md`'s
  `judge:` block — the global calibration/status record this doc's per-rubric
  `status` block feeds. (Not owned here — reference by path only.)
