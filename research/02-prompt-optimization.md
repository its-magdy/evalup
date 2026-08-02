# Research: Automatic Prompt Optimization for LLM Agents (2025–2026)

## 1. Algorithm landscape

| Algorithm | Core loop | Feedback signal | Notes |
|---|---|---|---|
| APE (arXiv:2211.01910) | LLM proposes candidates → score → resample around top | Scalar only | 2022; single-shot tasks |
| OPRO (arXiv:2309.03409, DeepMind) | Meta-prompt holds (prompt, score) history → LLM proposes next | Scalar, in-context history | +8% GSM8K, +50% some BBH |
| ProTeGi (arXiv:2305.03495) | Error minibatch → LLM writes NL critique ("textual gradient") → edit opposite direction → beam search + UCB | NL critique from concrete failures | +15.3% avg over original prompt |
| TextGrad (arXiv:2406.07496) | ProTeGi generalized to compound pipelines; textual feedback backpropagated through a computation graph | NL gradients per node | GPT-4o 51→55% GPQA-like |
| EvoPrompt (arXiv:2309.08532) | Evolutionary algorithm; LLM implements crossover/mutation | Scalar fitness | up to +25% BBH |
| PromptBreeder (arXiv:2309.16797) | Co-evolves task-prompts AND mutation-prompts | Scalar | Self-referential EA |
| DSPy MIPROv2 (arXiv:2406.11695) | Bootstrap few-shot demos from successful traces + Bayesian search over {instruction, demos} per module | Scalar on minibatches | Requires DSPy programming model |
| **GEPA** (arXiv:2507.19457, ICLR'26 oral) | Sample full trajectories (reasoning, tool calls) → **LLM reflects in NL on failures** → propose rewrite → Pareto frontier across instances | Rich NL reflection on trajectories | Beats GRPO by up to 20pp with **35× fewer rollouts**; beats MIPROv2 by ~6–14pp |

Trend: 2022→2025 moves from scalar-score search to NL reflection on failures, and from single prompts to whole pipelines. GEPA is the current best fit for agents.

## 2. Agents specifically
- GEPA verified on multi-hop tool-calling pipelines (HotpotQA 42→62–64 EM, HoVer 35→52 vs baseline; Qwen3-8B).
- DSPy MIPROv2 verified on a ReAct agent: recall 8% → ~42% for ~$5 API cost (dspy.ai/tutorials/agents).
- **Credit assignment is the core agent-specific difficulty**: which turn / which agent's prompt caused the failure (arXiv:2605.30227). Single-shot scalar optimizers (OPRO/APE) lack a mechanism for it; GEPA's trajectory reflection is explicitly the response.
- Open question: GEPA optimizes prompts within a fixed pipeline; not shown to fix cross-agent coordination/topology problems.
- Multi-agent optimization gains are real but highly task-sensitive; search space grows exponentially (MAS-PromptBench, arXiv:2606.23664).

## 3. Practitioner pragmatic loop (Hamel Husain, hamel.dev/blog/posts/evals-faq)
1. Gather representative traces.
2. Open coding: expert notes the FIRST failure per trace.
3. Axial coding (most important): cluster failures into a taxonomy, count frequency, prioritize.
4. Iterate to saturation: "if ~20 traces don't turn up a new category, stop (but review ≥100 to start)."

Sizes: 20–50 outputs after a significant change; 100+ fresh traces per full error-analysis cycle; 100+ purpose-built CI regression cases; 10–20 weekly outlier spot-checks. Binary pass/fail over Likert for decisive small-n comparisons.

Split discipline: rotating "discovery" sample (production traces) vs frozen CI regression set — functionally train/holdout. Rigorous small-n significance testing is UNDERSPECIFIED in public literature; apply paired bootstrap CIs yourself.

Anthropic: start with 20–50 realistic tasks from actual user failures; don't wait for perfect infra.

## 4. Tool descriptions matter as much as prompts (Anthropic, strongest guidance found)
- "Provide extremely detailed descriptions. This is by far the most important factor in tool performance." ≥3–4 sentences per tool; when to use / not use; parameter meanings; caveats. (platform.claude.com define-tools doc)
- Consolidate related operations into fewer, more capable tools; meaningful namespacing; responses return only high-signal info.
- SWE-bench team "spent more time optimizing our tools than the overall prompt" (Building Effective Agents).
- Their actual optimization method for tool schemas = failure-driven Claude Code loop: run realistic eval tasks → collect transcripts + metrics (tool-call frequency, errors, tokens) → paste transcripts into Claude Code → it refactors many tool descriptions at once. Measured accuracy gains on internal Slack/Asana tools. (anthropic.com/engineering/writing-tools-for-agents)
- No published *algorithm* specifically for tool-description optimization; GEPA's generic text-artifact interface could take a tool docstring as seed candidate (unevaluated in literature).

## 5. Anthropic prompt tooling
- Console Prompt Generator (metaprompt, claude-cookbooks/misc/metaprompt.ipynb) and Prompt Improver (4-step rewrite: find examples → XML draft → add CoT → refresh examples).
- Context engineering post: find the "right altitude"; start minimal with the best model, add instructions/examples driven by observed failure modes.

## 6. Standalone tooling (no DSPy adoption required)
- **`gepa` pip package** — works on raw strings + custom eval function: `gepa.optimize(seed_candidate={"system_prompt": ...}, trainset, valset, task_lm, reflection_lm)`; also `optimize_anything()` for any text artifact. github.com/gepa-ai/gepa. Also exposed via `mlflow.genai.optimize_prompts()`.
- **`promptfoo optimize`** — baseline eval → optimizer model proposes candidates from failures → re-eval → best; `--validation-split` flag guards overfitting. promptfoo.dev/docs/usage/prompt-optimization
- DSPy MIPROv2/dspy.GEPA require wrapping your program in DSPy modules — NOT usable on bare strings.
- Others: Microsoft PromptWizard, AdalFlow, Ax (TS DSPy port).

## Recommendation
1. Start with the failure-driven Claude-reflection loop (same mechanism as GEPA, human in the loop).
2. Graduate to standalone `gepa` package once the eval suite is trusted and can be called as a scoring function.
3. Apply the same loop to tool descriptions, not just system prompts.
4. Always: frozen holdout set, binary scoring, trajectory-aware reflection for credit assignment.
