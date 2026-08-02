# Wave 2: Verification of Flagged Claims (2026-07-18)

Method: direct fetches of primary sources only (docs, repos, source files).

| # | Claim | Verdict |
|---|---|---|
| 1 | LangSmith multi-turn user simulation | CONFIRMED — current API: `create_llm_simulated_user()` + `run_multiturn_simulation()` + trajectory_evaluators. docs.langchain.com/langsmith/multi-turn-simulation |
| 2 | PyRIT generic targets + Crescendo/TAP/PAIR | CONFIRMED — PromptTarget ABC, http_target/, executor/attack/multi_turn/{crescendo,tree_of_attacks,pair}.py |
| 3 | Invariant policy DSL | CONFIRMED — github.com/invariantlabs-ai/invariant; `raise ... if (call: ToolCall) -> (call2: ToolCall)` syntax; offline (LocalPolicy over trace) + runtime (invariant-gateway proxy). Snyk acquisition was **June 2025** (date correction) |
| 4 | NeMo Guardrails taxonomy | CHANGED — now five rail types: Input, Dialog, Retrieval, Execution, Output ("topical rails" is dead terminology) |
| 5 | Braintrust regression replay | CONFIRMED as composed workflow: Dataset Pipelines (spans→dataset rows, declarative + version-controlled) → Eval() → immutable Experiments diffing |
| 6 | Phoenix drift detection | CHANGED — effectively deprecated (no drift/embeddings-monitoring pages remain; only an embedding-distance evaluator). Span Replay real but single-LLM-span only (Prompt Playground) |
| 7 | Langfuse MIT since Jun 2025 | CONFIRMED — repo LICENSE: MIT except ee/ dirs |
| 8 | **Replay uses recorded tool responses?** | **REFUTED as an existing feature** — Phoenix/Braintrust/LangSmith all re-invoke your LIVE code (tool calls run fresh unless you stub them yourself). No platform ships recorded-tool-response replay. → deterministic replay-with-recorded-tools is a BUILD item and a differentiator |
| 9a | promptfoo OpenAI acquisition | CONFIRMED ("Promptfoo is now part of OpenAI") |
| 9b | `promptfoo optimize` + --validation-split | CONFIRMED (src/commands/optimize.ts; 0 < n <= 0.5) |
| 9c | **promptfoo `trajectory:tool-sequence` assertions over OTel spans** | **NOT FOUND / treat as inaccurate** — only TracingConfig + a TraceData interface for trace-aware custom assertions exist; no named trajectory assertion types located in docs or code search. → do NOT plan on promptfoo as the trajectory scorer; use agentevals/DeepEval matchers or our own scripts over normalized traces |
| 10 | `gepa` pip package | CONFIRMED — v0.1.4 (Jul 2026), active, MIT; optimize()/optimize_anything(); adapters incl. **MCP tools** (closest tie-in to tool-description optimization; no public case study yet) |

## Design impact
1. **Trajectory scoring: own scripts + agentevals/DeepEval, not promptfoo.** Wave-1's promptfoo trajectory claim didn't survive verification. Our normalized-trace + Python matcher approach (already planned as skill scripts) is the primary path; promptfoo remains useful for red-teaming and as optimize-baseline only.
2. **Deterministic replay is ours to build.** No vendor replays recorded tool responses; all re-run live code. Confirms the mock/record-replay tool layer as a real gap AND means eval determinism requires our own tool-stubbing design in the adapter.
3. Invariant's open-source policy DSL is adoptable for trajectory policy rules ("never X without prior Y") instead of inventing a syntax — evaluate `invariant-ai` package in Tier 2.
