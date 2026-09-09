# Research Behind the Design

This plugin's design decisions were made through three waves of deep research,
adversarial cross-validation, and three independent design critiques
(2026-07-18), conducted in a research workspace external to this plugin. That
workspace (its `research/` directory of source documents, wave-by-wave
verification notes, and a `SYNTHESIS.md` master document) does **not** ship
with the plugin — it was the design-time scratch space, not a runtime
dependency, so a marketplace install of this plugin will not have it on disk.
What follows is the load-bearing subset of citations that survived that
process, reproducible against the public papers:

Selected load-bearing citations: τ-bench (arXiv:2406.12045) for state-diff
ground truth and pass^k; **agentevals** (LangChain) for the NAMED four-mode
trajectory-match matrix (strict/unordered/subset/superset) that
`expect.tools.order_mode` mirrors. BFCL also supports subset matching, but be
precise about WHICH BFCL: its **V3 multi-turn** response checker is explicitly
subset-with-extras ("The model result is considered correct if it contains the
ground truth as a subset, even if it contains additional function calls or
takes a different trajectory", against a ground truth defined as the minimal
viable execution path), while its **v1 executable** evaluation is
order-independent but requires every ground-truth output to be matched. Cite
BFCL for the per-case function list, irrelevance/relevance detection, and V3
multi-turn subset matching; agentevals for the named mode matrix;
GEPA (arXiv:2507.19457) for reflective optimization — with
arXiv:2607.14004 on why holdout regression control is mandatory;
arXiv:2605.29800 for single-judge > panel; arXiv:2604.15579 for symbolic
business-rule guardrails; EvalGen (arXiv:2404.12272) for criteria drift;
arXiv:2602.20426 (Trace-Free+ curriculum-learning framework) and
arXiv:2602.18914 for tool-description leverage; Anthropic's "Demystifying
evals for AI agents" and "Writing tools for agents" for the
agents-testing-agents loop.

Dataset-generation citations (added after a verification pass; each was
checked against its primary source rather than relayed):

- **CheckList** (Ribeiro et al., arXiv:2005.04118) for the grid's column axis:
  capabilities × TEST TYPES (MFT/INV/DIR), where columns are oracle types
  rather than input flavors. The paper frames the matrix as "a guide,
  prompting users to test each capability with different test types" and says
  to use the three types "(when possible)" — i.e. explicitly not a quota.
- **Hamel Husain's evals FAQ** for two-phase generation: hand-write ~20 tuples,
  expand the tuple set, then realize prose "In a separate prompt", because
  "This separation avoids repetitive phrasing." Also the cross-product-then-
  filter vs direct-generation trade (direct is "more realistic but tends
  toward generic outputs and misses rare scenarios") and "Error analysis is
  the most important activity in evals."
- **Self-Instruct** (arXiv:2212.10560) for the filter stage's expected junk
  rate: 92% valid instructions / 79% valid inputs / 58% valid OUTPUTS / 54%
  valid on all fields, and the ROUGE-L < 0.7 near-duplicate gate. Caveat: that
  audit was author self-assessment, not independent annotation.
- **WizardLM / Evol-Instruct** (arXiv:2304.12244) for elimination-evolving as
  a filter pattern (its "sorry" + <80-word rule is one of four heuristics, and
  the paper hedges it as "often indicates").
- **Verbalized Sampling** (arXiv:2510.01171) for mode collapse attributed to
  typicality bias in preference data, and k-candidates-with-probabilities as a
  training-free mitigation. Its 1.6–2.1× diversity gain is measured on
  CREATIVE WRITING; do not relay it as a general figure for eval authoring.
- **Preference leakage** (Li et al., arXiv:2502.01534) and Panickssery et al.
  2024 on self-preference, for §0's cross-family guidance — which is stated as
  a documented risk, not a measured effect for case authoring, because no
  study measures that. The full hedge is in
  generate/references/generation-method.md.
- **"Adding Error Bars to Evals"** (Miller, Anthropic 2024) for clustered
  standard errors on template-grouped cases, paired difference testing, and
  reporting SEM — the basis of generate/references/suite-sizing.md, which
  bounds what §1's suite may claim.

On file layout: one-file-per-case is NOT unprecedented — Terminal-Bench and
Harbor both use a directory per task (task.yaml/task.toml + Dockerfile +
tests/). The METR Task Standard is directory-per-task-FAMILY, where one
`TaskFamily` class's `get_tasks()` returns many tasks — that is closer to this
plugin's `template_id` than to a case, so it is precedent for the TEMPLATE
grouping rather than for file-per-case. The
distinguishing factor is whether a case carries an ENVIRONMENT (container,
fixtures, test scripts). For cases that are input/expected rows, flat-file or a
hosted dataset is universal across prompt-eval frameworks (promptfoo, Inspect,
DeepEval, Ragas, OpenAI Evals, LangSmith, Braintrust, BFCL, tau-bench). This
plugin's cases carry no environment today, which is why splits are a field and
ids are opaque (Braintrust's id-upsert semantics are the model). If cases ever
gain seeded per-case fixtures, revisit — directory-per-case becomes correct.
