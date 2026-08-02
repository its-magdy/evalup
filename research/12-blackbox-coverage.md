# Wave 3: Black-Box Evaluation & Test Selection/Coverage (2026-07-18)

## Access levels — validated three-tier framing
- Practitioner framing (testRigor): black-box = prompt-response behavior, functional flows, consistency, jailbreaks; gray-box = traces/architecture visible → trace validation, tool-invocation testing; white-box = code access → unit tests, decision paths. Recommended layering: white-box in dev → gray-box as features grow → black-box for final validation. https://testrigor.com/blog/black-gray-white-box-testing-for-ai-agents/
- Academic (arXiv:2401.14446): black-box query access is "unreliable for detecting failures that elude typical test sets" — honest ceiling for our black-box mode.
- Established black-box chatbot QA = Botium/Cyara: utterance banks per intent, scripted convo files with any-of assertions, regex/JSONPath payload checks, plus load testing. Prior art for our black-box mode's case format.

## Black-box structure discovery — precedent exists!
- **TRACER** (github.com/Chatbot-TRACER/TRACER): LLM-driven black-box explorer — probes fallback behavior, runs exploratory conversations, classifies bot as transactional vs informational, emits YAML user profiles + workflow graph. Reverse-engineers app structure from the endpoint alone → direct prior art for endpoint-only `discover`.
- **AgentEval** (arXiv:2607.06873): builds conversational workflow graph from dialogue turns; found 23–38 guard/prerequisite boundaries per agent black-box vs 12 for prompt-only baseline (τ³-bench).
- **Automated Capability Discovery** (arXiv:2502.07577): scientist-model generates open-ended probing tasks to map a subject model's capabilities/failure modes.

## Coverage models (conversational)
- Standard: **intent coverage, entity coverage** (arXiv:2503.05561 — CTG outperforms Botium/CHARM); **transitions coverage** (Dialogflow CX ships flow/path coverage natively); persona coverage emerging (Cekura, Coval maturity ladder: intent → adversarial → flow → regression → load → persona → A/B).
- No unified formal test-adequacy taxonomy for dialogue systems exists — intent/entity/transition coverage is the state of the art. Our profile-driven coverage map (domain × tool × category) is consistent with it.

## Test selection / suite curation
- **Diversity-based selection works**: Adaptive Random Testing with Normalized Compression Distance improved fault detection 7.24% avg (up to 34%) over random (arXiv:2501.13480).
- **Facility-location on embeddings is the winning curation method**: "Coresets Before Score Sets" (arXiv:2607.09739) — FL on semantic embeddings beat 12 baselines at preserving true scores across 35 benchmarks, works WITHOUT prior model scores → directly usable to pick a representative smoke set from a large generated pool.
- **tinyBenchmarks** (ICML'24): ~100 curated examples can reproduce full-benchmark results → supports small-but-curated suites.
- Production-traffic sampling caveats: naive sampling (one time-of-day cron) produces unrepresentative sets; static suites catch only ~40% of real-world failures → continuous augmentation from production, not a fixed "done" state.

## "When is the suite done" / eval-the-eval
- **Mutation testing for eval suites is real and adoptable**: `llm-mutation` (github.com/Rowusuduah/llm-mutation) — six operators inject deliberate prompt/spec bugs; mutation score = % caught; ≥90% strong, 80% recommended CI gate. This operationalizes our "inject a bug, verify the suite catches it" self-test.
- No formal saturation-criteria literature exists; practical proxies: mutation score threshold + coverage percentages + continuous production augmentation.

## Design adoption
1. Black-box mode gets a real methodology (Botium-style utterance/convo cases + TRACER-style exploratory discovery).
2. Smoke-set selection via embedding diversity (facility location) instead of random sampling.
3. Suite adequacy = mutation score (seeded-bug catching) as a periodic self-test + CI gate.
4. Coverage reporting: intent/domain × tool × category grid + transition coverage where flows exist.
