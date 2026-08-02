# Research: Beyond-Static-Evals Testing for LLM Agents (2025–2026)

Note: items tagged [recall] were not re-verified live (doc pages mid-restructure) — spot-check before hard-coding.

## 1. Simulated users
- **τ-bench** (Sierra, arXiv:2406.12045): LLM plays the customer against the agent, real tools + policy docs; scored by final DB state vs annotated goal state. Metric **pass^k** (ALL k trials succeed) exposes reliability collapse — GPT-4o-class agents <50% success, pass^8 <25% retail.
- **τ²-bench** (arXiv:2506.07982): dual-control (user can also act on the world); user simulator grounded in the same state machine as the agent (reduces simulator hallucination); failure attribution split into reasoning vs communication errors.
- **ToolSandbox** (Apple, arXiv:2408.04682): stateful, on-policy user simulator; "Insufficient Information" category tests clarifying-question behavior; milestone-based scoring.
- **Snowglobe** (Guardrails AI): commercial simulated-user product; personas incl. adversarial tactics; exports JSONL eval/finetune datasets; arbitrary chat APIs.
- LangSmith historically shipped a dialog-simulator pattern [recall — docs mid-restructure].
- Design lessons: ground the simulator in the same tool/state world; score end-state; report pass@k AND pass^k; adversarial personas in the same simulator (not a separate system).

## 2. Red-teaming (buy, don't build)
| Tool | Highlights | Arbitrary target? |
|---|---|---|
| promptfoo redteam | injection, jailbreaks, PII leak, BOLA/BFLA for tool agents; multi-turn (Crescendo-style); CI-native | Yes — HTTP/custom providers |
| garak (NVIDIA) | 20+ probe families incl. GCG suffixes, encoding attacks | Yes — rest.RestGenerator |
| PyRIT (Microsoft) | orchestrators, converters (obfuscation) | Yes via PromptTarget [recall] |
| Giskard | OWASP-mapped scan + RAGET for RAG | Python-callable wrapper |
| **DeepTeam** (Confident AI) | 50+ vulns; **agentic-specific: goal theft, recursive hijacking, excessive agency, inter-agent compromise** | Yes — model_callback, fully generic |

Reference taxonomy: OWASP Top 10 for LLM Applications (incl. Excessive Agency) — use as coverage checklist.

## 3. Chaos / fault injection — GENUINE WHITESPACE
- No widely-adopted "chaos engineering for LLM agents" tool exists.
- Closest: **ToolEmu** (arXiv:2309.15817) — LM-emulated tool sandbox + LM risk evaluator; ~69% of flagged failures judged real; safest agent still failed ~24%.
- Build recommendation: fault-injecting proxy at the tool-call boundary — timeouts, 500s, truncated/malformed JSON, wrong schema, adversarial tool output, partial results, rate limits — per test case. Can reuse red-team tools' generic-target plumbing to orchestrate.

## 4. Property-based / metamorphic testing
- **CheckList** (arXiv:2005.04118, foundational): MFT (minimal functionality), **INV (perturbation → output unchanged: paraphrase/typo/name-swap)**, DIR (perturbation → known direction of change). Users found ~3× more bugs.
- **Giskard metamorphic tests**: invariance/increasing/decreasing with STATISTICAL pass criteria (t-test/Wilcoxon, threshold fraction not 100%) — the right way to handle LLM stochasticity.
- **promptfoo**: trajectory:tool-used / tool-args-match / tool-sequence / step-count, `not-` negation on every assertion → policy rules like "never delete_account without prior confirm_action"; custom JS/Python assertions as escape hatch.
- **Invariant Labs** (acquired by Snyk 2026): policy DSL over agent traces ("if tool X called, Y must precede") applied offline and at runtime [recall for mechanics].

## 5. Online / production evals
- Anthropic "Swiss Cheese Model": production monitoring + user feedback + A/B + manual transcript review + systematic human eval as overlapping layers. Capability evals (hard, low pass) vs regression evals (must stay ~100%); capability graduates into regression.
- TheFork×Arize case study: online evals filter spans by attribute (latency, node, errors) — score highest-leverage slices, not everything; same evals re-run on historical windows = lightweight regression/A-B; caught duplicated embedding calls (infra bug, not just quality).
- Guardrails (NeMo, Guardrails AI) = runtime blocking; evals = sampled measurement. Mature setups run both.

## 6. Regression replay
- Phoenix Span Replay: replay recorded LLM calls with modified inputs/prompts.
- Braintrust: logs ≡ datasets schema → filter logs into dataset → run against new prompt → diff.
- **Critical design choice: replay recorded TOOL RESPONSES (mocked), don't re-call live tools** — isolates the prompt/model change from tool nondeterminism. [Implied across tools, not explicitly documented — verify per-tool.]

## 7. Agents testing agents (Anthropic's own practice)
- "Writing effective tools for agents — with agents": Claude Code generates eval cases from tool docs → runs them → transcripts pasted back into Claude Code → it analyzes failures and refactors tool definitions en masse. Measured gains on internal Slack/Asana tools.
- "Demystifying evals": trajectory/transcript analysis as first-class eval mode; judges calibrated against human experts (precision/recall) before gating CI.
- "Effective harnesses for long-running agents": agents must test end-to-end as a human user would; cannot mark passing without it; cannot delete tests to pass.
- Caution: **eval awareness** — models can behave differently when they detect they're being evaluated (Anthropic /engineering/eval-awareness-browsecomp); make eval traffic look like real traffic.
- Also: infrastructure noise inflates failure counts (why pass^k matters) — /engineering/infrastructure-noise.

## Cross-cutting conclusion
The reusable primitive across ALL seven modes is the **full structured trajectory**. Treat it as the harness's core artifact; every testing mode is a different producer or scorer of that same format.
