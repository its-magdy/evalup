# Generation method — why the authoring rules are what they are

Read this when you want the evidence behind a rule in `SKILL.md`, or when you
are about to relax one. SKILL.md carries the procedure; this file carries the
sourcing and the hedges. The case schema lives in
[case-format.md](case-format.md); the quotas and their statistics live in
[suite-sizing.md](suite-sizing.md).

## §0 — cross-family generation, and how strong the claim actually is

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
removes a known-unknown. When it is not, generate anyway and mitigate rather
than blocking.

The one place this IS a hard rule is the judge, which is a measured effect —
see `${CLAUDE_PLUGIN_ROOT}/agents/judge.md`. Do not silently borrow that rule's
force for this one.

## §1 — the grid is a guide, not a quota

CheckList (Ribeiro et al., arXiv:2005.04118) itself says to test each
capability with the three test types "(when possible)" and that the matrix
"works as a guide, prompting users to test each capability with different test
types". Same here. Uniform cell counts assert uniform risk, which error
analysis almost always contradicts — BFCL, the most grid-like public agent
benchmark, has deliberately uneven per-category counts.

Why the columns are oracle types and not input flavors: `category` values
(`happy`, `multistep`, `edge`, `ambiguous`, `oos`, `adversarial-refusal`,
`noise`) all share ONE oracle, so they sub-divide MFT rather than forming an
axis. INV is the cheapest cell in the grid because it inherits its parent's
expectation and needs no new labeling — that is what `metamorphic_parent` is
for.

## §2a — why tuples come before prose

The two-phase flow follows Hamel Husain's dimension/tuple method.

Hand-writing the first ~20 tuples is not ceremony: it forces you through the
problem space before scaling, and the tuples become the few-shot anchors for
the expansion.

Realizing them in a **separate prompt**, one at a time, is Hamel's rule, and
his rationale is verbatim: "This separation avoids repetitive phrasing." When
one prompt emits N finished cases in a single context, each conditions on its
predecessors' surface form and collapses into a template — the exact failure
the "vary over orthogonal dimensions" rule asks for and cannot otherwise
enforce.

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

## §2b — why half the goldens are junk, and where the damage sits

An unfiltered generator produces roughly half-unusable cases, and the damage
concentrates in the EXPECTED OUTPUT, not the input. Self-Instruct's audit is
the reference point: 92% of generated instructions described a valid task, 79%
had an appropriate input, but only **58% had a correct output** and **54% were
valid on all three**. Your inputs will look fine and your labels will not.

On the individual filter steps:
- **Self-containedness / answerability.** DeepEval's defaults are a reasonable
  starting point (`synthetic_input_quality_threshold` 0.5,
  `max_quality_retries` 3). DeepEval keeps the best attempt when retries are
  exhausted — prefer DROPPING here, since a below-threshold eval case is worse
  than a missing one.
- **Near-duplicate rejection.** Self-Instruct's gate is ROUGE-L < 0.7. Cheap
  and lexical-only, so paraphrases still slip through — which is why phase 1's
  tuple-space check is the primary defense and this is the backstop.
- **Cell validity.** Cross products generate nonsense cells; that is expected
  and they get dropped.

A filter that leaves no evidence cannot be audited or tuned, which is what the
template's `rejected:` list is for.

## §2c — persona is not a dimension unless it is crossed

Persona tends to collapse into a dedicated authorization slice (one case per
persona, uncrossed with intent). That is not a dimension — it is a category
wearing a dimension's name. Cross it with intent or it buys no diversity.

Inferring an expected tool sequence from the app's own code grades the app
against itself, which is why exact sequences are authored only where a human
states one. Multiple valid paths are the norm, and subset matching accommodates
extras.

## §3 — why a machine may never write `accepted`

A machine writing `accepted: by generate-review` is the dataset recording that
nobody looked — which is worse than `pending`, because `pending` is visible and
false acceptance is not. The §2b filter is what keeps the human pass
affordable: it removes junk mechanically so the 15 minutes go to genuine label
judgment.

**Never derive an expectation by reading the app's source.** It is already in
`${CLAUDE_PLUGIN_ROOT}/agents/test-generator.md` rule 4, and it is repeated
because it is the rule most likely to be broken while feeling responsible:
reading the implementation to "verify" an expectation produces a case that
asserts what the code does, which passes by construction and can never detect
the code being wrong. The reliable tell is an assertion that restates an
implementation constant — e.g. a `must_not_contain` listing the exact phrases a
guard already string-matches and rewrites, which cannot fail no matter how the
model behaves.

**Prefer a probe before labeling, not after.** A synthetic grid is a good way
to produce INPUTS and a poor way to produce LABELS. Where a probe run is cheap,
run the generated inputs through the app first and label against what you see —
that is the cold-start workflow practitioners actually endorse (the grid as a
traffic generator, then error analysis on the resulting traces), rather than
shipping a fully-labeled synthetic set that has never touched the app.

How that squares with rule 4 above, once: a probe grounds the **data** of an
expectation (names, ids, counts, vocabulary the app really has) and shows you
which cases need a human eye; it never grounds the **behaviour** (status
codes, refusal paths, error wording). The SHOULD for behaviour is the profile
and discover's findings; an app answering 400 to a greeting today is an
observation to record in `notes`, not the label.

## §4 — what the disabled-layer check is really preventing

The most expensive failure mode in this skill is authoring
`expect.tools.subset` everywhere while `tool_selection` and `trajectory` are
disabled in the profile. That produces a suite of `gating: true` cases whose
only live assertion is that the app returned 200. Such a suite looks thorough,
passes reliably, and detects nothing. If a layer is disabled, either assert
something else or do not author the case yet. `validate_cases.py`'s
`no_graded_layer` error is the enforcement; case-format.md owns its definition.
