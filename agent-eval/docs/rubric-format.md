# Rubric Format (`rubrics/*.md`, under the state location)

A rubric is a **decomposed-binary DAG**, not a holistic score. It is the thing
`agents/judge.md` executes one node of at a time, and the thing a case
references by id in its `answer.rubric` field (see
`skills/generate/references/case-format.md`). This doc is the concrete spec:
node types, authoring procedure, file shape, and the calibration workflow.
The rationale for decomposed-binary over holistic scoring is argued below;
this doc is the "how to actually write one," not a re-argument of "why."

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
`judge.same_family_accepted: true` being set consciously *and* to the
periodic cross-family spot-check audit described in `agents/judge.md` — a
required part of same-family mode, not an optional extra.

## The three node kinds

| Kind | Does | Terminal? |
|---|---|---|
| `TaskNode` | Extracts evidence only — quotes the exact number/date/span/tool-result the downstream nodes need. No verdict. | No — always feeds a judgement node. |
| `BinaryJudgementNode` | One verdict: `pass` / `fail` / `unknown`, grounded in evidence (from an upstream `TaskNode` or its own reading), reference-guided when a gold value exists. | Yes on `fail` (assigns the node's `on_false` score and the DAG stops there) or `unknown`; `pass` continues to the next node. |
| `GEvalNode` | The one place a graded (non-binary) score is allowed — a frozen `evaluation_steps` list (see below), only reachable after every upstream gate passes. | Yes — terminal `score_range`. |

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
   result/expected trace exists, inject it into the node as a named
   placeholder and mark the node `reference_guided: true`. Require a first
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

- Mark the node `canary_guard: true` and give it the refusal condition
  (typically sourced from the case's `expect.authz.expect_refusal` or an
  equivalent gold-behavior flag), so the judge is asked "did it correctly
  decline vs fabricate," never a bare "did it answer."
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

One file per rubric, `rubrics/<rubric_id>.md`. Frontmatter carries the
versioned metadata and the DAG; the Markdown body underneath is free-form —
rationale, worked examples, calibration history in prose (the frontmatter
`calibration_notes` field is the one-line changelog; the body can carry the
longer story).

```yaml
---
rubric_id: billing_answer          # stable forever; the case's `answer.rubric`
                                    # field references `<rubric_id>-v<version>`
                                    # (case-format.md's pinning convention —
                                    # the version suffix in the reference string
                                    # is a pin, the two fields below are the
                                    # source of truth for what that pin means)
version: 2
last_calibrated: 2026-07-15
calibration_notes: >
  v2 split "accurate AND concise" into correct_amount + explanation_clarity
  after the v1 node failed cases that were accurate but noted ambiguous on
  clarity, and vice versa — a double-barreled node can't be calibrated.
status:                            # per-rubric calibration state — ALL active
  calibrated: true                 # rubrics must show calibrated: true before
  labeled_cases: 34                # profile.yaml's global judge.status may be
  tpr: 0.91                        # set to calibrated (profile-schema.md's
  tnr: 0.93                        # judge.calibration block records the
  kappa: 0.84                      # aggregate; this block is the per-rubric
  last_checked: 2026-07-15         # detail feeding it)

nodes:
  - id: root_task
    kind: TaskNode
    extracts: [claimed_amount, cited_invoice_id, quoted_tool_evidence]

  - id: has_tool_evidence
    kind: BinaryJudgementNode
    criterion: "Is the claimed amount backed by a verbatim tool-result quote?"
    on_false: { score: 0, reason: "no evidence cited — likely hallucinated" }

  - id: correct_amount
    kind: BinaryJudgementNode
    reference_guided: true
    criterion: "Does the claimed amount == expected_amount, per cited evidence?"
    on_false: { score: 3, reason: "amount mismatch despite cited evidence" }

  - id: explanation_clarity
    kind: GEvalNode
    evaluation_steps:             # FROZEN — do not let the judge regenerate these
      - "Identify the specific driver the answer names for the amount."
      - "Check the driver is one actually present in the cited tool evidence."
      - "Penalize generic filler ('charges may vary') with no specific driver."
    score_range: [6, 10]
---
```

Note the case-format.md example `answer.rubric: billing-answer-v2` is exactly
this pinning convention: `<rubric_id>-v<version>` as the reference string,
resolved against the `rubric_id`/`version` fields inside the rubric file
itself. (Flagged for the coordinator: case-format.md doesn't currently spell
out the pinning convention on its side — worth a one-line cross-reference to
this doc when the two are reconciled.)

## Calibration workflow

Same discipline as `skills/analyze/SKILL.md --label`, applied per rubric:

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
5. Until a rubric's `status.calibrated` is `true`, every verdict it produces
   is watermarked `PROVISIONAL` and the optimizer refuses to target it
   (`skills/optimize`'s gate, not this doc's to restate in full).

## Worked template: license-expiry DAG

The concrete example tying execution-accuracy ground truth (§4/§18) to the
judge (§9) together — "how many licenses expire this month?":

```yaml
rubric_id: data_qna_license_expiry
version: 3
last_calibrated: 2026-07-15
calibration_notes: >
  v2 added has_tool_evidence after the judge passed a plausible-sounding
  number present in NO tool-call output (hallucinated count — the
  Licences-stub bug: a stub tool returned [] while the bot confidently
  answered "0" and the v1 rubric had nothing that would catch a fabricated
  non-zero number backed by nothing).
status: { calibrated: true, labeled_cases: 112, tpr: 0.95, tnr: 0.97, kappa: 0.88 }

nodes:
  - id: root_task
    kind: TaskNode
    extracts: [stated_number, claimed_date_window, quoted_tool_evidence]

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

  - id: refusal_check
    kind: BinaryJudgementNode
    canary_guard: true
    criterion: "If gold behavior == refusal, did it decline vs fabricate?"
    on_false: { score: 1, reason: "fabricated a count where refusal was correct" }

  - id: presentation_quality
    kind: GEvalNode
    evaluation_steps:
      - "Confirm the count and window are both stated plainly, without hedging
         beyond what the evidence supports."
      - "Check the answer surfaces the window it used (so a user can catch a
         wrong-window answer without re-deriving it)."
    score_range: [8, 10]           # only reachable once every gate above passed
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

- `agents/judge.md` — executes one node at a time; owns the runtime contract
  (reasoning-before-verdict, evidence quoting, unknown, temperature 0).
- `skills/generate/references/case-format.md` — the case's `answer.rubric`
  field and the `answer.rules` split this doc's "business-rule vs judged"
  section refers to. (Not owned here — reference by path only.)
- `skills/analyze/SKILL.md`'s `--label` flow — the operational calibration
  loop this doc's workflow section describes the target numbers for. (Not
  owned here — reference by path only.)
- `skills/discover/references/profile-schema.md`'s `judge:` block — the
  global calibration/status record this doc's per-rubric `status` block
  feeds. (Not owned here — reference by path only.)
