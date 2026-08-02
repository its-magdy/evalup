# Wave 3: Ground Truth & Test Oracles for Agent Evals (2026-07-18)

## Seeded environments (τ-bench mechanics, verified from source code)
- τ-bench `calculate_reward()`: hash live DB state after the conversation; REPLAY the ground-truth action sequence against a fresh env copy → gt hash; reward=0 if hashes differ. PLUS a response check (required strings must appear in agent's reply) — state correctness necessary but agent must also COMMUNICATE it. (github.com/sierra-research/tau-bench, envs/base.py)
- Anthropic: "the outcome is whether a reservation exists in the SQL database," recommend `state_check: expect {tickets: {status: resolved}}` primitives; trial isolation from clean seeded env is first-class (leftover state → correlated failures).
- Seed principle: seed data must make every case's correct answer MECHANICALLY DERIVABLE (reference query or replayed ground-truth actions).

## Mocked tools & tolerant matching (BFCL)
- AST check (no execution) vs executable check (verify actual backend state after calls).
- **Subset matching**: trajectory passes if it CONTAINS the ground-truth calls — extra exploratory calls (ls before mkdir) or alternate valid paths OK. Adopt this; exact-sequence is over-strict by default.
- **Structural invariance for live APIs**: check response type, JSON key-set, numeric values within 20% tolerance — not exact equality. Pattern for any nondeterministic backend.
- Record/replay: vcrpy-style HTTP cassettes (YAML) is the standard pattern; no established "VCR for LLM tools" project exists — ours to adapt.

## Oracle taxonomy (TOSEM 2025, arXiv:2405.12766)
1. Test assertions (case-specific expected values)
2. Contracts (pre/postconditions + invariants holding across ALL executions)
3. Metamorphic relations (properties across related executions — no label needed: paraphrase A≈B ⇒ same answer)
- Failure modes: oracle DEFICIENCY (false positives worse than false negatives) and oracle LEAKAGE (LLM regurgitates memorized checks).
- Self-consistency/majority-vote = WEAK oracle (majority can be consistently wrong) — triage signal only.
- LogicHunter (arXiv:2607.06195): active "agentic oracle" — investigate-with-tools before verdict + 4-session consensus → 91% precision vs 29% for passive LLM judge. Pattern for silent semantic errors.

## Business rules as oracles — strongest new finding
- **Symbolic Guardrails (arXiv:2604.15579): 74% of real agent policies are symbolically enforceable** (deterministic checks, violations provably impossible); simple API-validation alone covers 47–81% of enforceable requirements; guardrails cut violation rates from 20–78% → 0% without hurting task success. Examples: precondition checks (cancel_ticket only by ticket owner), temporal gating (no tools before auth), schema constraints, mandatory human confirmation.
- Four categories resist symbolic encoding: persona/style, "no hallucination," procedure-following, common sense → those stay with judge/human.
- **Policy-as-Prompt (arXiv:2509.23994)**: extract rules from PRDs/specs/code → policy tree (acceptable/rejected inputs, correct/forbidden outputs) → input classifier + output auditor. Extraction recall ~53%, enforcement 70–73% — first line of defense, ambiguity escalates to humans. → discover can draft the rule set from docs; human verifies.

## Side-effect safety
- AISI Inspect: per-sample Docker sandboxes with isolated FS + auto-cleanup; scorers inspect final sandbox state. Reference implementation for sandboxed tool execution.
- Synthesis across sources: never eval against production with live side effects. Options: (a) mocked tools, (b) seeded resettable staging, (c) recorded replay. Always assert on the MUTATION itself, not the agent's claim about it.
- Dry-run mode pattern: log the decision the agent WOULD execute without executing.

## Verifiable-task design
- Anthropic: every eval prompt paired with a verifiable outcome; warn against over-specifying tool paths (multiple valid) and over-strict verifiers.
- Agent-RLVR: reduce verification to atomic auto-checkable final assertions (test pass, state diff, exact match), not diffuse quality judgments.

## The boundary (answers "correctness beyond tool returns")
Draw it at the tool call and its immediate result:
- Backend returns X correctly? → conventional backend tests + seeded fixtures. NOT the agent eval's job.
- Given X: right tool, right args, faithful synthesis, no fabrication beyond X? → agent-layer, ours (code graders + business rules + judge for residual fuzz).
- τ-bench state checks legitimately straddle both (correct end state needs both layers right) — that's why state mismatch is unconditional failure, with unit tests isolating which layer broke.
