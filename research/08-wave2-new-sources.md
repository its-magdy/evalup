# Wave 2: Best Practices from New Sources (2026-07-18)

## OpenAI (cookbook/evals guide)
- Eval flywheel = Analyze → Measure → Improve (explicitly cites Hamel Husain/Shreya Shankar — convergent, not independent).
- **Judge calibration split: Train 20% (few-shot examples for judge prompt) / Validation 40% (iterate judge prompt) / Test 40% (touched once).**
- **Judge alignment measured via TPR/TNR against SME labels, NOT accuracy** (imbalanced pass/fail: always-pass judge looks 95% accurate).
- Synthetic generation: define orthogonal dimensions (channel × intent × persona), generate over the Cartesian product, then apply perturbations (typos, slang, irrelevant info). Warns "generate N examples" produces homogeneous data.
- Cookbook has tools-evaluation.ipynb and mcp_eval_notebook.ipynb.

## Google (Gemini Enterprise Agent Platform docs — note: moved off vertex-ai paths)
- **Three-tier cadence: Rapid Evaluation (every change) → Test Case Evaluation (scheduled regression, fixed dataset) → Online Monitoring (production sampling).** Maps to our smoke/full/online.
- Multi-turn AutoRaters over the ENTIRE conversation trace as default (MULTI_TURN_TASK_SUCCESS, MULTI_TURN_TOOL_USE_QUALITY).
- Mix reference-based and reference-free metric families.
- "Loss clusters" — auto-group failed cases by similarity → feeds prompt optimization.
- **Recommends simulating tool failures (mock HTTP 503) as part of the STANDARD suite** — vendor validation of our chaos layer.

## Microsoft (Azure AI Foundry, docs dated 2026-06)
- 11 named agent evaluators in two families: **System** (Task Completion, Task Adherence, Intent Resolution, Task Navigation Efficiency, Customer Satisfaction) vs **Process** (Tool Call Accuracy, Tool Selection, Tool Input Accuracy — 6 strict sub-criteria, Tool Output Utilization, Tool Call Success).
- **Everything reduces to binary Pass/Fail at the reporting layer** ("unit tests for agentic systems") — third independent validation of binary>Likert.
- Task Navigation Efficiency: exact_match / in_order_match / any_order_match + precision/recall/F1 vs ground-truth trajectory.
- **Interchange format: OpenAI-schema conversation array (role/content with typed tool_call/tool_result)** — align our dataset format to this for interop.

## Academia
- Survey 2507.21504: enterprise gaps academia ignores — role-based data access, reliability guarantees, compliance.
- **Bias-reliability tradeoff (arXiv:2607.00304): judge coupling γ, strategy diversity, and small-sample noise can't be jointly minimized** — a generic weakly-coupled judge is noisy at small n; a low-noise judge at n=5 is implicitly biased toward a specific model's style. Expose as a knob, don't hide.
- **Judge version drift is real**: GPT-4o behavior silently shifted (June 2026) without version-string change → periodically re-run judge calibration against the human-labeled set as a drift check.

## Shreya Shankar / EvalGen (arXiv:2404.12272) — architecturally important
- **Criteria drift**: grading outputs is what DEFINES the criteria; some criteria are observation-dependent and impossible to specify a priori. A harness that does "define rubric → collect dataset → grade" linearly is architecturally wrong.
- → Rubrics must be VERSIONED artifacts, and rubric-editing-while-grading must be a first-class loop (judge-design skill + analyze command).
- PROMPTEVALS (2504.14738): fine-tuned small models beat GPT-4o by 20.9% at generating assertions — assertion generation may deserve a cheap specialized path.
- DocWrangler: UI prior art for human-in-the-loop trace review.

## Production case study: Intercom (Jun–Jul 2026 posts)
- Replaced CSAT (<10% coverage, skews extreme) with LLM-judge "CX Score" on EVERY conversation; decomposed into answer quality / customer effort / product feedback.
- **Targets set by correlating judge scores against operational metrics** (response time, time-to-close), not by fiat: 80% AI-handled / 70% human / 78% overall.
- Optimize the MIDDLE of the distribution (fine-but-forgettable 3s), not just failures.
- Pre-ship: generated FAQs blended with REAL customer language from beta; launch validation = 2–4 WEEKS of production monitoring, not a single pre-merge gate.
- (Coverage gap: other company blogs unreachable this session — absence ≠ nothing exists.)

## Coding-agent-as-harness prior art (read before building!)
- **Quorum `prime-radiant-inc/superpowers-evals`**: drives real agent CLIs through a QA "Gauntlet" agent. Steal: hard split static-checks (safe for public CI, no model calls) vs live evals (trusted-maintainer only); **throwaway isolated $HOME per run** (agent under test never sees real ~/.claude); subscription-vs-API-key modes with concurrency warnings.
- **Galileo `eval-engineer`**: installable Claude Code skills (/eval-setup, /eval-measure, /eval-diagnose...). Steal the principle: **"grounds the problem before editing code"** — fixed 7-step loop: evidence source → read debug packet → name metric contract → compare expected vs actual trace → choose fix surface → smallest useful change → verify with fresh run.
- **Caliper (`edonadei/caliper`)**: YAML specs mixing `expect:` (LLM-judged) + `assert:` (deterministic Python) per task; **always runs a --baseline arm without the skill** — answers "would the bare agent have passed anyway."
- **No official Anthropic eval/prompt-opt skill exists** (checked anthropics/skills directly). Third-party space is hot (many repos <2 weeks old) but nothing matches our discover→generate→run→analyze→optimize + OTel + optimization design. Niche still open, moving fast.
