# Research Behind the Design

This plugin's design decisions were made through three waves of deep research,
adversarial cross-validation, and three independent design critiques
(2026-07-18). The full evidence lives in the sibling `research/` directory of
the workspace this plugin was developed in:

- `01-eval-methodology.md` — eval taxonomy, framework comparison, judge practices
- `02-prompt-optimization.md` — GEPA/MIPROv2/OPRO mechanics; why reflection wins
- `03-otel-traces.md` — GenAI semantic conventions, trace→eval pipelines
- `04-beyond-static-evals.md` — simulated users, red-teaming, metamorphic, replay
- `05-plugin-authoring.md` — Claude Code plugin best practices
- `06/07/08` — wave-2 verification, cross-validation, and vendor/industry sources
- `09/10/11` — the three design critiques (practitioner, first-user, systematic)
- `12/13/14` — black-box & coverage, ground-truth oracles, team roles/process
- `SYNTHESIS.md` — the master document; its "DESIGN REVISION v2" section is
  the authoritative spec this plugin implements

Selected load-bearing citations: τ-bench (arXiv:2406.12045) for state-diff
ground truth and pass^k; BFCL for subset matching and structural invariance;
GEPA (arXiv:2507.19457) for reflective optimization — with arXiv:2607.14004
on why holdout regression control is mandatory; arXiv:2605.29800 for
single-judge > panel; arXiv:2604.15579 for symbolic business-rule guardrails;
EvalGen (arXiv:2404.12272) for criteria drift; arXiv:2602.20426/2602.18914
for tool-description leverage; Anthropic's "Demystifying evals for AI agents"
and "Writing tools for agents" for the agents-testing-agents loop.
