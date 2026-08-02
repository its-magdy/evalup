# Research: Evaluating Multi-Agent LLM Systems (2025–2026)

Focus: router → domain-agent architectures, generalized to any agent app.

## 1. Eval taxonomy

### Routing / classification accuracy
- Standard multiclass metrics: accuracy, per-domain precision/recall/F1, confusion matrix.
- Report **macro-F1 and micro-F1 both** — a large gap reveals neglected minority domains.
  - https://metricgate.com/blogs/macro-vs-micro-average-classification/
- Router accuracy degrades with option-set size: reported 94% @ 50 options → 64% @ 200 → 20% @ 417. Motivates hierarchical routing at scale.
  - https://tianpan.co/blog/2026-04-16-intent-classification-agent-routers
- Enterprise reference numbers: >90% routing accuracy, <3% false agent-switch rate; routing latency (~350ms classify) as first-class metric — arXiv:2412.05449 (NOT Anthropic; a misattribution was corrected during verification).
- Benchmarks: RouterBench (arXiv:2403.12031), LLMRouterBench (arXiv:2601.07206).

### Tool selection
- DeepEval Tool Correctness: correctly-used tools / total called; name-match default, flags for params/order/exact. https://deepeval.com/docs/metrics-tool-correctness
- BFCL AST-based check (function name + required params, order-independent) + explicit irrelevance/abstention category. https://gorilla.cs.berkeley.edu/leaderboard.html

### Trajectory (tool-call order)
- **Google ADK** — most formal: EXACT / IN_ORDER / ANY_ORDER match + trajectory_precision, trajectory_recall. https://google.github.io/adk-docs/evaluate/criteria
- **LangSmith agentevals** — strict / unordered / subset / superset matchers + LLM-judge trajectory eval. https://github.com/langchain-ai/agentevals
- **Ragas** ToolCallAccuracy (order-strict by default) and ToolCallF1 (set-based). https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/agents/
- **promptfoo** `trajectory:tool-sequence` (exact/in-order/any-order) **over OpenTelemetry spans**. https://www.promptfoo.dev/docs/tracing/
- **BFCL V3** multi-turn: compare backend END-STATE, not call sequence (multiple valid paths). https://gorilla.cs.berkeley.edu/blogs/13_bfcl_v3_multi_turn.html
- AgentBench failure taxonomy: Invalid Format / Invalid Action / Task Limit Exceeded — diagnose *why* trajectories diverge.

### Tool arguments
- Exact match (DeepEval INPUT_PARAMETERS, Ragas, BFCL AST) vs LLM-judged reference-free (DeepEval ArgumentCorrectnessMetric) vs executable check (BFCL runs the call).

### Answer quality
- Anthropic multi-agent research system: single LLM-judge call, 5-criterion rubric (factual accuracy, citation accuracy, completeness, source quality, tool efficiency), 0–1 + pass/fail; single judge more consistent than ensembles in their setup; ~20 hand-picked queries + human review for judge blind spots. https://www.anthropic.com/engineering/multi-agent-research-system
- Pairwise comparison for experiments (LangSmith pairwise evaluators).
- ADK: ROUGE match vs LLM-judge semantic equivalence (majority vote) vs rubric-based.

### End-to-end
- Ragas AgentGoalAccuracy (with/without reference), pass@k (≥1 of k) and **pass^k (all k succeed)** for nondeterminism — Anthropic. https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents

### Multi-turn
- Ragas TopicAdherenceScore — did the domain agent stay in its assigned boundary (precision/recall/F1 incl. wrong-refusals).
- DeepEval: Knowledge Retention, Conversation Completeness, Role Adherence (drift detection).

## 2. Generating eval datasets from scratch
- Anthropic: let Claude explore actual tool definitions → generate dozens of prompt/response pairs; avoid toy tasks; multi-tool-call tasks with verifiable outcomes. https://www.anthropic.com/engineering/writing-tools-for-agents
- Phoenix 5-category balanced generation: happy-path / complex-multistep / edge-case / adversarial-refusal / noise-robustness, each with expected_action + expected_outcome. https://arize.com/docs/phoenix/cookbook/tracing/generating-synthetic-datasets-for-llm-evaluators-and-agents
- ADK generates conversation scenarios from the agent's own instructions, incl. expected trajectories.
- Hard negatives for routers: queries that *look like* domain A but belong elsewhere (SyNeg, arXiv:2412.17250); OOS benchmarks ELOQ (arXiv:2410.14567); mutation fuzzing HASTE (arXiv:2601.19051).
- Seeding from traces: LangSmith/Braintrust — promote flawed production replies with corrected expected outputs into a regression suite.
- **Sizing**: start 20–100 hand-checked cases → grow to hundreds from real traffic → 200–2,000 versioned golden set is the ceiling, not the start (Arize; Hamel Husain saturation heuristic; Eugene Yan: "a few hundred" surfaces obvious errors).

## 3. Framework comparison (condensed)
| Framework | Trajectory assertions | Judge built-in | Standalone? |
|---|---|---|---|
| promptfoo | Yes, over OTel spans | Yes (llm-rubric) | Yes, OSS. **OpenAI acquired promptfoo Mar 2026, staying open source** (verified) |
| DeepEval | Yes (Tool Correctness) | Yes (G-Eval) | Yes, Apache 2.0 |
| Ragas | Yes (ToolCallAccuracy/F1) | Yes | Yes |
| LangSmith agentevals | Yes (4 match modes) | Yes | Lib is OSS; platform proprietary |
| Langfuse | Partial | Yes | MIT core (since Jun 2025, not re-verified) |
| Braintrust | No trajectory primitive | Yes (Autoevals) | Platform proprietary |
| Phoenix | Single tool-selection judge | Yes | Self-host, Elastic License 2.0 |
| Google ADK | Yes — most formal | Yes | Yes, OSS |
| OpenAI hosted Evals | Yes (Trace Grading) | Yes | **Sunset Nov 2026** — avoid |
| Anthropic | Guidance only, no framework | — | — |

## 4. LLM-as-judge best practices
- **Binary pass/fail > Likert** (Hamel Husain). One isolated judge call per dimension (Anthropic). CoT reasoning-before-score. Give the judge an "Unknown" escape.
- Reference-guided grading materially improves judges (Prometheus ablation).
- Biases: position (swap-and-average), verbosity (>90% prefer-longer in some studies), self-preference (Panickssery, arXiv:2410.21819 — root cause: perplexity). Foundational: MT-Bench (arXiv:2306.05685, GPT-4↔human ~80–85% ≈ human↔human).
- **PoLL**: panel of 3 smaller diverse-family judges beats one big judge on human correlation at ~1/7 cost (arXiv:2404.18796).
- Calibration: expert labels ~30 examples, target >90% judge-human agreement, sample error cases not random (Hamel's 7-step workflow). Grade the output, not the path; partial credit on sub-goals (Anthropic).
- Judge ≠ generator model (self-preference); OpenAI warns about grader hacking when optimizing against a judge.

## 5. Router-specific
- Pre-LLM intent-classification literature transfers wholesale: CLINC150 (arXiv:1909.02027) shows models ace in-scope while failing out-of-scope — **OOS detection is its own tested behavior**, with "none of the above" as an explicit class (OOS precision/recall as metrics).
- Ambiguous queries: set-valued prediction (arXiv:2606.28925) — accept a *set* of valid routes; or route to clarifying-question fallback. Rasa FallbackClassifier precedent: confidence-threshold fallback, thresholds tuned per app on held-out distributions.
- Gap: Anthropic/OpenAI write-ups describe router architecture but are light on quantitative routing evals — rigor comes from enterprise papers + NLU-era methods.
