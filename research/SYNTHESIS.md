# Synthesis: General-Purpose Agent Eval & Optimization Harness (Claude Code plugin)

Date: 2026-07-18. Sources: research/01–04.

## Vision
A Claude Code plugin that can evaluate and optimize ANY LLM chat/agent application (simple chat, router→executor, graphs, orchestrators). Claude Code is the brain (discovery, generation, analysis, optimization); open-source runners/scorers are the muscle; OTel is the universal trace transport.

First target/reference app: the user's 2-agent chat feature (router picks domain → domain agent with tools answers), OTel-instrumented, under development.

## Core design decisions (research-backed)
1. **The trajectory is the central artifact.** Every testing mode produces or scores the same structured trace. OTel GenAI spans feed it, but a normalization layer maps whatever convention the target emits (spec-current gen_ai.*, OpenInference, legacy OpenLLMetry) to one internal format.
2. **Adapter per target app**: how to invoke it + where traces land + trace convention + where prompts/tool-descriptions live (the optimizable surfaces). Everything else is derived.
3. **Discovery infers architecture** (app profile: agents, tools, domains, flow) → determines which eval layers apply (capability matrix). Layers: answer quality (always) / routing / tool selection / trajectory order / tool args / multi-turn / cost-latency.
4. **Layered scoring for credit assignment** — tells you WHICH prompt to fix, and is what the optimizer needs.

## Adopt vs build
ADOPT:
- Trajectory matchers: LangSmith `agentevals` (strict/unordered/subset/superset) or ADK semantics (EXACT/IN_ORDER/ANY_ORDER + precision/recall); DeepEval Tool Correctness.
- promptfoo as runner option: trajectory assertions over OTel spans, llm-rubric, red team, `optimize` command w/ --validation-split. (OpenAI acquired promptfoo Mar 2026; still OSS.)
- Red-teaming: promptfoo redteam and/or DeepTeam (agentic attacks: goal theft, inter-agent compromise). OWASP LLM Top-10 as checklist.
- `gepa` pip package for algorithmic prompt optimization on raw strings (no DSPy adoption).
- Judge practices: binary pass/fail, one judge per dimension, CoT, "Unknown" option, swap-and-average pairwise, panel-of-small-judges option, ~30 human-labeled calibration cases (>90% agreement), judge model ≠ app model.

BUILD (the plugin's actual value):
- Discovery/app-profiling (Claude Code code archaeology) — nothing does this.
- Test generation: 5-category balanced (happy/multistep/edge/adversarial-refusal/noise) + router hard-negatives + OOS class + metamorphic variants (paraphrase→same route).
- Trace normalization layer.
- Loop/redundant-call detector: group execute_tool by (tool.name, hash(args)) per trace — vendor gap.
- Chaos proxy at the tool boundary (timeouts, malformed JSON, adversarial tool output) — genuine whitespace, no OSS tool exists.
- Failure-driven optimization loop (Claude reflection = GEPA mechanism with human in loop; graduate to gepa package later).
- Baseline store + regression diffing (scores attached to prompt snapshots).

## Key numbers to bake in
- Start 20–50 hand-checked cases; 100+ for error-analysis cycles; 200–2,000 golden ceiling.
- pass@k AND pass^k for nondeterminism; statistical (not 100%) pass criteria for metamorphic tests.
- Binary scoring for decisive small-n comparisons; error-analysis saturation heuristic (~20 traces no new category → stop).
- Routing: macro+micro F1, confusion matrix, OOS precision/recall with explicit none-of-the-above, false-switch rate; accuracy degrades sharply >50–200 route options.

## Critical checks on any target app (do FIRST)
1. Is OTel content capture opted in? (OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT or lib equivalent) — without it, no content to mine.
2. Which instrumentation convention is emitted?
3. Programmatic invocation path (HTTP/function/CLI) — can't test what can't be invoked.
4. Where prompts + tool descriptions live (files? DB?) — optimizer must read/write them.
5. Can a trace ID be correlated per request?

## Build order
- **Tier 1 (core)**: adapter config format → discover → generate → run (layered scoring, deterministic layers first) → baseline/regression diff → report.
- **Tier 2**: metamorphic variants, trajectory policy assertions ("never X without Y"), replay (with recorded tool responses — never live re-call), smoke vs full sets, loop detector.
- **Tier 3**: judge calibration workflow, simulated users (grounded in the app's tool/state world, end-state scored), chaos proxy, optimize loop (Claude reflection → gepa).
- **Tier 4 (production era)**: trace mining/online evals, red-team integration, scheduled self-improvement pipeline (auto-PR with eval evidence; human merges — never auto-merge).

## Decision (2026-07-18): Claude Code IS the optimizer — no gepa dependency
Keep GEPA's ideas (NL reflection on trajectories, multi-candidate pool with Pareto-style keep-if-wins-anywhere, frozen holdout, budget stop) but implement natively: a skill encodes the discipline, candidates + per-case scores live as files (git-diffable history), Claude reflects with full codebase context (can attribute failures to tool schemas, not just prompt text), human reviews each edit. The `gepa` pip package is optional far-future for unattended high-iteration runs only.
Reflection must propose from the FULL lever set: prompt text, tool consolidation/descriptions, few-shot examples, domain boundaries, model tier, context trimming — not prompt-polishing only.

Claude Code feature map: commands = entry points; skills = methodology; subagents = judges (pinned to a different model than the app), simulated users, parallel scorers; hooks = smoke eval on prompt-file edit; scheduled runs = nightly pipeline; plugin = packaging.

## Additional angles (added after coverage sweep)
- RAG evals (retrieval relevance, groundedness) as a profile capability when domains retrieve.
- PII redaction step before production traces become eval-dataset files.
- Language coverage as a generation dimension (multilingual/mixed-language routing + arg extraction).
- Model-upgrade insurance as a named use case of the suite.
- Format-compliance checks (structured output/JSON/citations) — cheap deterministic layer.
- Harness self-test: seed known-good/known-bad outputs through scorers before trusting numbers.
- Out of scope: load/concurrency testing (different discipline).

## Wave 2 reconciliation (2026-07-18, see research/06–08)
Changed from wave 1:
1. **Trajectory scoring = our own scripts + agentevals/DeepEval matchers.** promptfoo's named trajectory assertions were NOT found in verification — promptfoo demoted to red-teaming + optimize-baseline only.
2. **Judge default = ONE strong calibrated judge (different model family than app), not a panel** — correlated-error studies overturned PoLL. Calibrate with OpenAI's 20/40/40 split, measure TPR/TNR not accuracy, re-calibrate periodically (judge version drift is real). Binary pass/fail now triple-validated (Hamel, OpenAI, Azure "unit tests for agents").
3. **Deterministic replay with recorded tool responses: no vendor has it — we build it** (all platforms re-invoke live code).
4. **Statistics in the keep/revert gate**: paired tests on identical cases (McNemar / paired bootstrap), Bayesian CIs below n≈100. No significance → no keep.
5. **Rubrics are versioned, living artifacts** (EvalGen criteria drift): rubric-editing-while-grading is a first-class loop, not a setup step.
6. **Dataset format: OpenAI-schema conversation arrays** for interop (Azure standard).
7. Synthetic generation: Cartesian product over orthogonal dimensions (intent × persona × phrasing) + perturbations; blend real seeds ASAP (Coverage Illusion: synthetic misestimated real needs by 62pp; generator-collapse risk when generator shares model family with app).
8. Chaos layer vendor-validated: Google docs recommend mock tool failures (503s) in the STANDARD suite.
9. Tool descriptions confirmed as possibly the highest-leverage surface (+60% success from description rewrites alone, arXiv:2602.20426).
10. GEPA gains don't transfer without regression control → our frozen-holdout + baseline regime is mandatory, not optional.

Prior art to read before building (research/08): Quorum superpowers-evals (isolation: throwaway $HOME, static-vs-live safety split), Galileo eval-engineer (ground-before-edit 7-step loop), Caliper (baseline arm; expect:+assert: mixed YAML). No official Anthropic eval plugin exists; niche open but moving fast.

## DESIGN REVISION v2 (2026-07-18, after 3 independent critiques + wave 3 — see 09–13)
The critiques (09 practitioner, 10 first-user, 11 systematic) converge; these override earlier sections where they conflict.

**Reframing:**
- Position as "reference app + adapter SDK," not "evaluates any app day one." Generality is earned adapter by adapter.
- **Staged rigor is the core UX principle**: discover detects app maturity and gates features. Pre-stability mode (churning architecture) = invariants only: crash rate, always-responds, format compliance, loop detection, refusals — NO route/trajectory assertions. Trajectory evals unlock when tool/domain boundaries are stable; judge+calibration when ready; optimizer requires calibrated judge + n≥~100. "Too early for full trajectory eval" = before boundaries stabilize; discover says this explicitly.
- Golden path day 1: `/agent-eval:start` wizard → discover PATCHES the app (GenAI instrumentation + content capture + traceparent echo + invocation shim + prompt-extraction refactor offer) and outputs a design-findings list (e.g. "no OOS route") → ~30 cases → deterministic layers → confusion-matrix "aha" on day 1–2 → smoke hook (opt-in, debounced, async) as the habit loop.

**Hard contracts to build:**
- Adapter interface: `start_session/send_turn/end_session → {text, trace_id}`; declared capabilities: seed(case)/reset()/snapshot_state(), trace convention, per-tool side-effect classification (safe-live / needs-mock / never), access level (white/gray/black), safe-to-attack flag. Secrets env-var only; schema rejects inline literals.
- Runner: run manifest pinning (dataset ver + case IDs/hashes, app git SHA, prompt snapshot, app model ver, judge model + judge-prompt ver, rubric ver, harness ver, k, temperature); per-case durable results + resume; failure taxonomy {pass, fail, infra_error, infra_incomplete} with infra excluded from denominators; trace-completeness gate (quiescence/expected-span); HARD-FAIL on heuristic trace correlation; health pre-flight; pre-run cost estimate (3-case probe) + budget cap; judge-verdict caching; serial default when state-mutating.
- Reports: headline = top-3 failure clusters with expected-vs-actual example traces + implicated surface + effort tag; metrics = appendix; static HTML with failure→trace drill-down; trend history; flakiness ledger; canary cases (known-good/known-bad) in EVERY run; judge TPR/TNR displayed beside every judged metric; crash rate as own line; categories markable "tracked, not gating."
- Judge: PROVISIONAL watermark until calibrated; optimize REFUSES uncalibrated judge objectives; cross-family judge via script calling external API (subagents can't leave Claude family — the contradiction must be explicit); same-family allowed only as consciously-accepted degraded mode. Labeling = 15-min flow (judge pre-sorts, shows reasoning, agree/disagree/edit-rubric per case).
- Holdout: physically separated (aggregate score only in reports), logged `unseal` command, touch-budget with forced re-freeze. Stats: n-aware messages ("n=50 detects ~15pp; need ~200 for 5pp — here's the command"); Bayesian mode < n≈100.
- Dataset: stable case IDs + content hashes; `discover --diff` maps profile changes → affected cases → bulk migration proposals; disagreement-sampled review (~15–20 suspicious cases, not "review 100"); hard-negative/OOS labels human-reviewed or quarantined; frozen comparable-core subset as suite grows.
- Harness self-checks: profile validated against observed traces BEFORE generate; normalizer conservation checks (spans in/out, orphaned parents, unmatched tool_call IDs); matcher test corpus; **mutation testing of the suite** (llm-mutation-style seeded bugs; mutation score ≥80% as adequacy gate).
- Ground truth (13): τ-bench state-diff via seed/replay/hash; **BFCL subset-matching as the DEFAULT trajectory mode** (exact only when human-specified); structural invariance (key-set, 20% numeric tolerance) for live APIs; multistep cases default to end-state + policy assertions, not exact sequences; business rules are the cheap oracle (74% of policies symbolically enforceable — discover drafts rules from docs/code, human verifies); boundary = the tool call (backend data correctness is backend tests' job).
- Curation: smoke-set selection via embedding diversity/facility location, not random; suites of ~100 curated cases can carry full-signal (tinyBenchmarks).
- Black-box mode has real methodology (12): Botium-style utterance/convo cases; TRACER-style exploratory endpoint discovery.

**Cuts/de-scopes (from 09):** chaos → Tier 4, gated on MCP/HTTP tool boundary (in-process tools can't be intercepted — same precondition gates replay); simulated users gated on seeded-env capability (else scripted multi-turn only); red-team = document pointing promptfoo/DeepTeam at the adapter, no integration; DELETE panel-of-judges mode; Invariant DSL → ~20 lines of Python; Cartesian generation at scale deferred until real traffic; monitors optional.

**Still open:** headless/CI entrypoint spec (plain script for deterministic layers, no Claude required); PII redaction mechanics; multi-user/team concerns; plugin's own acceptance criteria.

## Honest limits (keep in the docs)
Evals sample, never prove. Optimizer inherits metric quality — bad rubric = confidently worse app. Judges can't grade facts beyond tool output without ground truth/mocks. Synthetic ≠ real users; feed real traces over time. Prompts have a ceiling — harness should flag architecture problems (inherently confusable domains) instead of polishing forever. Eval awareness: make eval traffic look like real traffic.
