---
name: generate
description: >-
  Generate an eval dataset for the profiled app: coverage-gridded cases with
  expected routes/trajectories/rubrics, splits (full plus its smoke and canary
  subsets and a sealed holdout), and a targeted human-review pass. Use when
  the app has a verified profile and needs test cases, or to expand coverage
  for a specific layer or domain.
argument-hint: "[--layer routing|tools|answer] [--count N]"
---

# Generate — Build the Dataset

Precondition: `profile.yaml` exists with verified core entries. If not, stop
and route to discover. Case format:
[references/case-format.md](references/case-format.md).
Delegate bulk generation to the `test-generator` agent; this skill owns the
plan, the review pass, and the splits.
Size the suite to the budget: ~30 cases is a good default, ~12 is a legitimate
minimum — the split ratios below scale down with it.

## 0. Prefer a different model family than the app under test
Prefer generating cases (and driving user-simulator personas) with a different
model family than the one powering the app under test. Record the generator's
model + family in the dataset metadata either way, so a reviewer can weigh it
later.

**Be honest about the strength of this.** It is a documented *risk*, not a
measured effect for case authoring, and the skill states it that way on
purpose:

- Self-preference in JUDGING is well evidenced: Panickssery et al. 2024
  ("LLM Evaluators Recognize and Favor Their Own Generations") links
  self-recognition causally to self-preference, at family level too.
- Preference LEAKAGE (Li et al., arXiv:2502.01534) is the closest result that
  touches generation, and its mechanism is generator → synthetic training data
  → student, scored by a related judge. Relatedness includes same family.
- No controlled study shows that a same-family generator inflates agent eval
  pass rates through test-case authoring. There is usually no training step,
  so the leakage mechanism does not transfer directly. The plausible paths are
  (a) the generator writing tasks inside its own family's phrasing/competence
  distribution, and (b) a same-family judge, where the leakage result does
  apply.
- Anthropic's docs do carry "generally best practice to use a different model
  to evaluate than the model used to generate" — but as a hedged comment in
  LLM-grader sample code, about judging, not as a prescription about authoring.

So: when a cross-family generator is available, use it — it costs nothing and
removes a known-unknown. When it is not, **generate anyway and mitigate**,
rather than blocking: every case gets human validation (§3), and expectations
must be grounded in system state or an oracle rather than model opinion (§2).
Note the same-family generation in the dataset metadata as a caveat.

The one place this IS a hard rule is the judge, which is a measured effect —
see `${CLAUDE_PLUGIN_ROOT}/agents/judge.md`. Do not silently borrow that rule's
force for this one.

## 1. Build the coverage grid
The grid's ROW axis is the app's **unit of dispatch** — whatever the app
chooses between before acting — read from `architecture.kind` in the profile.
It is NOT always "domain"; domain is only the router-executor case.

`architecture.kind` holds exactly one of five values, and their definitions
live in `${CLAUDE_PLUGIN_ROOT}/skills/discover/SKILL.md` §2 (schema in
`${CLAUDE_PLUGIN_ROOT}/skills/discover/references/profile-schema.md`)
— read that if you need to know what a kind *means*. Spell them the same way
here, underscores included, or the branch silently matches nothing. What
GENERATE does differently per topology:

| `architecture.kind` | Rows the grid builds | Expectation / oracle handling |
|---|---|---|
| `single_llm` | out-of-scope + the answer-quality categories | answer layer only; the grid degrades to phrasing/persona variation. No routing or trajectory expectations to author. |
| `tool_agent` | every tool (+ out-of-scope) | trajectory expectations apply (end-state + policy per §2c); no route label to author. |
| `router_executor` | every domain × every tool (+ cross-domain + out-of-scope) | a route label per case *plus* executor expectations, kept separate. Hard-negative route labels are the least reliable — flag every one for review (§3). |
| `multi_agent` | every sub-agent × its tools (+ cross-unit + out-of-scope) | expectations named at the sub-agent / handoff boundary, not only on the final answer. |
| `workflow` | every node | per-node golden-behavior regression — pin each node's expected output rather than authoring open-ended trajectory coverage. |

Columns: the **oracle type**, not the input flavor — borrowed from CheckList
(Ribeiro et al., arXiv:2005.04118), whose matrix is capabilities (rows) ×
test types (columns):

- **MFT** (minimum functionality): a direct case with its own expectation.
  The `category` field (`happy`, `multistep`, `edge`, `ambiguous`, `oos`,
  `adversarial-refusal`, `noise`) sub-divides MFT — those are input flavors
  sharing one oracle, which is why they are a field and not the column axis.
- **INV** (invariance): perturb the input, the expectation must NOT change.
  Cheapest coverage in the grid — it inherits the parent's expectation, so it
  needs no new labeling. This is what `metamorphic_parent` is for.
- **DIR** (directional): perturb the input, the expectation must move in a
  known direction (narrow the filter → row count must not increase; drop the
  caller's permission → the answer must lose rows, never gain them).

CheckList itself says to test each capability with the three types
"(when possible)" and that the matrix "works as a guide, prompting users to
test each capability with different test types" — a guide, explicitly not a
quota. Same here: the grid maps risk. Cover every UNIT at least once; within a
unit, prioritize cells by risk (frequent/costly paths get depth; rare/harmless
get one case or none). Uniform cell counts assert uniform risk, which error
analysis almost always contradicts — BFCL, the most grid-like public agent
benchmark, has deliberately uneven per-category counts. List uncovered cells
in the dataset metadata as known gaps. Show the grid with counts before
generating — the user can rebalance.

Report coverage BY COLUMN as well as by row. "Every unit covered" is the least
informative view of a grid: a suite can cover every unit and still be 40%+
happy-path MFT with no INV anywhere. `validate_cases.py` warns at >40% in any
one category.

Every generated case is authored against the **canonical benchmark-derived
schema** (case-format.md), not just the
grid cell: set `difficulty` (hidden from the agent — stratified reporting
only, never copied into the prompt); set `template_id` + `instantiation_params`
when a case is one instance of a repeatable template, so variant-consistency
can be scored later; set `excluded_tools` when the app's tool catalog can be
scoped per case, to exercise relevance/irrelevance detection; and decide
`no_op_expectation` for every case (does a do-nothing agent correctly fail
it — a case that doesn't is usually vacuous, reconsider it).

### Budget geometry — ratios with a floor, never absolutes
Every quota here is a **ratio of the current suite size plus a floor**, never a
fixed count: a fixed absolute ("always add 5 canaries") silently dominates or
breaks a 12-case suite. Compute each against the suite size actually being
generated, apply the floor, and note in the dataset metadata whenever a floor
overrode its ratio.

| Quota | Ratio | Floor |
|---|---|---|
| Grid depth per cell | 1 case/covered unit; risk-weighted cells ~2x | 1 case/unit, no depth (correct at 12 cases) |
| `smoke` (§4) | ~1/3 of full | 5 |
| `holdout` (§4) | 20% of full | skip entirely below 25 cases — don't seal a token 1–2 |
| `canary` (§4) | ~10% of full | 2, once any data-Q&A layer exists |
| INV/DIR metamorphic | ≤25% of full | every template gets ≥1 INV; at the smallest budget the highest-risk case still gets one |

At the ~30-case default these read naturally (10 smoke, 6 holdout, 3 canary,
≤7 metamorphic); at 12 every ratio still resolves to a small positive integer
via its floor. For metamorphic cases treat the **floor** as binding rather than
the ceiling — INV inherits its parent's expectation, so it is the cheapest cell
in the grid, and a suite at 6% INV has left free coverage unbought.

### What a suite this size can and cannot claim
The ratios are precise; the number they divide is not. Say so in the dataset
metadata rather than letting a 30-case pass rate get quoted as a measurement.
At n=30, p≈0.8 the 95% CI is roughly ±14 pp, and the minimum detectable
effect is ~28 pp unpaired / ~16–28 pp paired. So a fixed-suite paired
before/after comparison, hard-regression catching, and large early-stage
effects are legitimate; quoting an absolute pass rate as a number, or claiming
any delta under ~15–20 pp, is not. Use Wilson or Clopper-Pearson intervals (not
Wald), McNemar's exact variant, and clustered standard errors — cases sharing a
`template_id` are not independent, which is one more reason `template_id` is
required (§2a). Derivations, the n/CI table, and sourcing:
[references/suite-sizing.md](references/suite-sizing.md).

## 2. Generation rules (pass to test-generator)

### 2a. Two phases, always — tuples first, prose second
Never let the generator emit finished case prose straight from a grid cell.
Generation is TWO passes with a checkpoint between them, following Hamel
Husain's dimension/tuple method:

**Phase 1 — tuples.** Define the dimensions for a template (e.g. `filter`,
`persona`, `register`, `intent`), then **hand-write the first ~20 tuples**,
one value per dimension. Hand-writing them is not ceremony: it forces you
through the problem space before scaling, and the tuples become the few-shot
anchors for the expansion. Then have the model expand the tuple SET — still
symbolic, no prose. Write these to `templates/<template_id>.yaml`.

**Phase 2 — realization.** In a **separate prompt**, convert each tuple to a
natural-language message, one tuple at a time. Hamel's rationale, verbatim:
"This separation avoids repetitive phrasing." When one prompt emits N finished
cases in a single context, each conditions on its predecessors' surface form
and collapses into a template — the exact failure this skill's "vary over
orthogonal dimensions" rule asks for and cannot otherwise enforce.

Why the split is load-bearing: **diversity is only checkable in the tuple
space.** Tuples are a low-entropy symbolic representation where spread is a set
operation (are these dimension combinations distinct?); prose is not. A
generator that skips phase 1 has no stage at which diversity can be verified,
and the collapse is invisible until a human reads all N cases side by side.

Two expansion strategies, and Hamel gives a real trade rather than a winner:
- **cross product then filter** — generate all dimension combinations, filter
  with an LLM. Guarantees coverage including rare cells. Use "when most
  combinations are valid".
- **direct generation** — ask for tuples directly. "More realistic but tends
  toward generic outputs and misses rare scenarios."
Default to cross-product-then-filter for the first pass of a unit (you want the
rare cells), then direct generation to top up realism.

At the realization step, consider asking for k candidate phrasings **with their
probabilities** rather than one, and taking a spread rather than the mode —
Verbalized Sampling (arXiv:2510.01171) attributes mode collapse to typicality
bias in preference data and reports a 1.6–2.1× diversity gain doing this.
Scope caveat: that figure is measured on creative writing, not eval authoring,
so treat it as a promising technique rather than a proven one here.

Every realized case therefore carries `template_id` + `instantiation_params`
(the tuple it came from). These are **required**, not optional: a case with no
template is a one-off that must say so explicitly — write `template_id: null`,
don't omit the key. `validate_cases.py` errors on a MISSING key
(`missing_template_id`), on `instantiation_params` without `template_id`, and
vice versa; a suite where every case declares itself a one-off warns
(`all_one_off`), because that is the tuple phase never having happened.

### 2b. Filter before review — assume half the goldens are junk
An unfiltered generator produces roughly half-unusable cases, and the damage
concentrates in the EXPECTED OUTPUT, not the input. Self-Instruct's audit is
the reference point: 92% of generated instructions described a valid task, 79%
had an appropriate input, but only **58% had a correct output** and **54% were
valid on all three**. Your inputs will look fine and your labels will not.

So run a mechanical filter pass BEFORE the human review pass in §3 — the human
budget is 15 minutes and must not be spent rejecting obvious junk:

1. **Self-containedness / answerability.** Score each case; drop below
   threshold, retrying a small fixed number of times. DeepEval's defaults are
   a reasonable starting point (`synthetic_input_quality_threshold` 0.5,
   `max_quality_retries` 3). Note DeepEval keeps the best attempt when retries
   are exhausted — prefer DROPPING here, since a below-threshold eval case is
   worse than a missing one.
2. **Ground-truth validity.** Can the expectation be derived from the oracle?
   If not, the case does not get an expectation invented for it — it gets
   dropped or demoted to `review.status: pending`.
3. **Near-duplicate rejection.** Self-Instruct's gate is ROUGE-L < 0.7 against
   every existing case. Cheap and lexical-only, so paraphrases still slip
   through — which is why phase 1's tuple-space check is the primary defense
   and this is the backstop.
4. **Cell validity.** Is this dimension combination coherent at all? Cross
   products generate nonsense cells; that is expected and they get dropped.

Record what was rejected and why in the template's `rejected:` list. A filter
that leaves no evidence cannot be audited or tuned.

### 2c. Other generation rules
- Vary over orthogonal dimensions (intent × persona × phrasing × language if
  applicable) — never "generate N examples" flat, it produces homogeneous data.
  **Cross the dimensions.** Persona in particular tends to collapse into a
  dedicated authorization slice (one case per persona, uncrossed with intent),
  which is not a dimension — it is a category wearing a dimension's name.
- Derive perturbed variants of accepted cases: typos, slang, irrelevant
  detail, paraphrase — as **INV cases** (`test_type: INV`, expectation: same
  route/behavior as the parent, `metamorphic_parent` set). No new labeling
  needed, which makes these the cheapest coverage in the grid: every template
  should yield at least one. They still spend suite slots — keep to the ratio
  +floor in §1 (at small budgets, only the highest-risk pairs).
- **DIR cases** where a direction is knowable: narrowing a filter must not
  increase the row count; removing a permission must not add rows. Like INV,
  the expectation is relative to a parent and needs no fresh oracle pull.
- Router hard-negatives: queries that look like domain A but belong to B or to
  nobody. These labels are the least reliable — flag every one for review.
- Expected trajectories: DEFAULT to end-state/answer + policy assertions.
  Exact tool sequences ONLY where a human states one. Multiple valid paths are
  the norm (subset matching accommodates extras), and inferring the expected
  sequence from the app's own code grades the app against itself — forbidden.
- **Data-Q&A cases → emit `expect.result`.** Compute the reference query
  against the seeded fixture / read-only oracle and store it as
  `reference_query` (provenance only) plus the resulting `scalar`/`rows`.
  Never hand-type the expected number — the runner re-derives `expected`
  offline from the reference query via the oracle at run time (see
  case-format.md), so a fixture change never leaves a stale hand-typed answer
  behind. If the runner can't extract `actual` from the trace, that's
  `unscorable`, never a manufactured fail.
- **Permission/scope cases → emit `expect.authz`.** For every case built from
  the (question × persona) matrix, set `allowed_record_ids` /
  `forbidden_record_ids` / `forbidden_tools` / `expect_refusal`
  (case-format.md), derived by running the SAME oracle query under that
  persona's real access scope (RLS `SET ROLE` / equivalent) — never by
  hand-filtering the full-access answer. This is scored deterministically by
  `score_authz.py` against the trace's tool-call log and returned record IDs,
  not an LLM read of the chat text, so a politely-worded refusal over an open
  endpoint underneath still fails.
- Tool-catalog sampling: where the app profile supports scoping the exposed
  catalog per case, leave 1+ present-but-irrelevant tools in and record them
  as `excluded_tools` — the relevance/irrelevance test (does the agent avoid
  reaching for a plausible-but-wrong tool).
- Real seeds first: if any real user messages/traces exist, seed from them and
  say in the dataset metadata which cases are synthetic-only. Record the
  standing caveat: synthetic-only datasets are a regression scaffold, not a
  measure of real-world quality.

## 3. Targeted review — 15 minutes, not 100 cases
Never ask the user to review everything; they won't, and silent label errors
become a permanent noise floor. Select for review only:
- all hard-negative/OOS labels (quarantined from scored runs until reviewed),
- cases the generator marked uncertain,
- cases where a quick probe run shows the app disagreeing with the label
  (disagreement sampling — run the smoke-sized probe if cheap).
Present each as: the message, the expected label, one-line why — accept /
fix / delete. Write review status per case.

**Only a human may set `review.status: accepted`, and `review.by` must be that
human's name.** Not "generate-review", not the agent, not the skill. A machine
writing `accepted: by generate-review` is the dataset recording that nobody
looked — which is worse than `pending`, because `pending` is visible and false
acceptance is not. Everything the generator produces starts `pending`;
`validate_cases.py` warns (`machine_accepted`) on any `accepted` whose `by`
looks automated. The §2b filter is what keeps this affordable: it removes junk
mechanically so the human's 15 minutes go to genuine label judgment.

**Never derive an expectation by reading the app's source.** It is already in
test-generator.md rule 4, and it is repeated here because it is the rule most
likely to be broken while feeling responsible: reading the implementation to
"verify" an expectation produces a case that asserts what the code does, which
passes by construction and can never detect the code being wrong. The reliable
tell is an assertion that restates an implementation constant — e.g. a
`must_not_contain` listing the exact phrases a guard already string-matches
and rewrites, which cannot fail no matter how the model behaves. Expectations
come from the profile's `confirmed_by_human` section, the oracle, and what the
USER needs. `validate_cases.py` warns (`source_derived_expectation`) when a
case's notes cite an app source file.

**Prefer a probe before labeling, not after.** A synthetic grid is a good way
to produce INPUTS and a poor way to produce LABELS. Where a probe run is cheap,
run the generated inputs through the app first and label against what you see —
that is the cold-start workflow practitioners actually endorse (the grid as a
traffic generator, then error analysis on the resulting traces), rather than
shipping a fully-labeled synthetic set that has never touched the app.

## 4. Splits
The four legal values of the `split` field are exactly `full`, `smoke`,
`holdout`, `canary`. `smoke` and `canary` are SUBSETS of `full` (a case
carries both). `holdout` is mutually exclusive with `full` — a case is sealed
or it is in the training pool, never both, and that exclusivity is what the
seal rests on.

- `smoke` (about a third of the full set, min 5 — see the budget-geometry
  ratio+floor in §1): diversity-selected (maximize spread across grid cells
  and phrasings), not random — small sets must earn their representativeness.
  Used by the everyday/smoke runs and hooks.
- `full`: everything reviewed.
- `holdout` (20% when the set is ≥25 cases; below that, skip the holdout and
  say so — optimize stays locked at that size anyway): SEALED. Written once,
  listed only by id+hash in reports,
  never displayed case-by-case. Unsealing requires `analyze --unseal`, which
  logs itself and warns that a reseal (swap with fresh cases) is due after
  N=5 looks. Optimization never trains on it.
- `canary` (~10% of the full set, floor 2 — see §1): a small fixed subset
  with stable, versioned expected answers, reused across runs rather than
  regenerated each time smoke/full are rebuilt — movement on these fixed
  inputs signals an app/model/judge change, not a traffic-distribution shift.
  **A canary MUST assert content** — `expect.result` or `expect.answer`. A
  canary whose only live assertion is `http: 200` cannot detect drift, because
  drift shows up in the answer, and 200 is returned either way. This is the
  single most common way a canary set silently becomes decorative.

Case ids are **opaque and stable**: `c-<hash8>`. Do NOT encode the unit or the
category in the id. Both are mutable classifications, and baking them into a
field declared "stable forever" guarantees the id eventually lies — a case
reclassified from `happy` to `oos` keeps an id claiming `happy`, and a renamed
domain strands every id that spelled the old name. Keep `unit` and `category`
as fields, where a reclassification is an edit. Content hash per case;
`dataset_version` bumped on any change (baselines pin it).

Splits are a **field on the case** (`split: [full, smoke]`), not a directory
copy. run-modes.md already describes smoke membership as "a durable tag";
this makes it literally one. Copying a case file into `smoke/` and `canary/`
creates two files with the same id and no single source of truth — an in-place
rewrite then updates one and silently leaves the other stale. `holdout`
remains genuinely disjoint (a case is in holdout or in full, never both), but
that is enforced by the field, not by which folder the bytes live in.

### Validate before handing the dataset over
Run `${CLAUDE_PLUGIN_ROOT}/scripts/validate_cases.py --cases <suite.json>
--capabilities <profile.capability_matrix.json> --manifest
<dataset.yaml-fields.json>`. It
fails the generate step on structural errors and warns on the vacuity
patterns. `--capabilities` is required — pass `--no-capabilities` only if
there is genuinely no profile, and expect the `capabilities_unchecked` warning
that says the disabled-layer check did not run.

It errors on a case missing `split`, `test_type`, or the `template_id` key,
and on an INV/DIR case with no `metamorphic_parent` — every one of those is a
field some later number is computed from, so an absent one makes that number
untrue rather than merely incomplete. It warns when the whole suite has no
INV/DIR case at all (the metamorphic floor in the budget table above is
binding; the ≤25% ceiling is not). The check that matters most is `no_graded_layer`: **a case must
assert at least one layer that is currently ENABLED in the capability
matrix.** `expect.http` alone never counts — it is a liveness check, not a
behavioral assertion.

`--manifest` cross-checks `dataset.yaml`'s `cases`/`splits`/`coverage_grid`
counts against the case files themselves (pass the same three fields as
JSON, extracted with whatever YAML parser already loaded `dataset.yaml`).
`dataset.yaml` is hand-written prose written once at generation time —
nothing regenerates or otherwise re-derives it, so a later hand-edit to a
`cases/*.yaml` file (a reclassification, a deleted case, a split change —
exactly what the targeted review in `analyze` does directly on case files)
can leave it silently stale. Run this flag again after any such edit, not
only at initial generation.

This is what stops the most expensive failure mode in this whole skill:
authoring `expect.tools.subset` everywhere while `tool_selection` and
`trajectory` are disabled in the profile, producing a suite of `gating: true`
cases whose only live assertion is that the app returned 200. Such a suite
looks thorough, passes reliably, and detects nothing. If a layer is disabled,
either assert something else or do not author the case yet.

## 5. Suite adequacy (periodic, not per-generate)
Offer mutation testing of the suite: inject 3–5 deliberate bugs into a COPY of
a prompt (drop a constraint, invert a condition, swap a tool name), run the
suite against each, report mutation score (% caught). <80% → the suite has
holes where the mutations lived; generate targeted cases there.
