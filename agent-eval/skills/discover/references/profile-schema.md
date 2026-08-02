# Profile Schema (`.agent-eval/profile.yaml`)

The profile is the harness's model of the app — and a contract the human
confirms. Every inferred entry carries `verified` (checked against live
traces) so downstream skills know what they can trust.

```yaml
profile_version: 1
generated: 2026-07-18            # updated by discover; --diff compares against this
app_git_sha: abc1234             # code state this profile describes

stage: pre-stability | stable | traffic | production
# pre-stability: invariant checks only. stable: + trajectory layers.
# traffic: + trace mining/real-seeded data. production: + online evals.

architecture:
  kind: single_llm | tool_agent | router_executor | multi_agent | workflow
  # This enum mechanically drives capability_matrix below:
  #   single_llm      -> no routing, no trajectory (nothing to assert a path over).
  #   tool_agent      -> full trajectory: tool selection + args + loop termination.
  #   router_executor -> routing scored AS A CLASSIFIER (precision/recall/confusion
  #                       matrix per route target), kept separate from conditional
  #                       executor quality per route.
  #   multi_agent     -> per-handoff capture (full prompt+response at every
  #                       agent-to-agent boundary), in addition to per-agent trajectory.
  #   workflow        -> path *set* fixed at design time -> per-node golden-behavior
  #                       regression instead of open-ended trajectory scoring.
  framework: langgraph | crewai | autogen | openai-agents-sdk | custom | none
  # Speed-up signal only, never the source of truth for `kind` above — a
  # hand-rolled router with no framework is still `router_executor`.
  detected_from:              # evidence for each of discover's 3 signals (audit trail)
    code_fingerprint: "LangGraph StateGraph.add_node/add_edge in graph.py:40"
    prompt_language: "orchestrator prompt: 'delegate to: billing, support'"
    otel_span_shape: "2 sibling invoke_agent spans, distinct gen_ai.agent.name"
  agents:
    - { name: router, role: "picks domain", prompt_ref: router_system, verified: true }
    - { name: domain_agent, role: "answers with domain tools", verified: true }
  flow: "user → router → domain_agent → answer"

route_targets:                   # the things the app dispatches between, if any.
                                 # kind depends on architecture: domains
                                 # (router_executor), nodes (workflow), sub-agents
                                 # (multi_agent). OMIT entirely for single_llm and
                                 # tool_agent — no dispatch step, routing is N/A.
                                 # (legacy alias: `domains:` — still read.)
  - name: billing               # e.g. a domain, for a router_executor app
    description: "...",
    tools: [get_invoice, list_invoices]
    verified: true
  - name: support
    ...
oos_handling: none | route:<name> | clarify   # none is a design finding, not config
                                 # route:<name> → the run skill passes <name> to
                                 # score_routing.py as --oos-route so OOS metrics report

tools:
  - name: get_invoice
    description_quality: ok | vague | missing   # vague/missing feed optimize
    args: [invoice_id]
    verified: true

conversation: { multi_turn: true, streaming: sse }

oracle:                           # ground-truth capability for the execution layer
  available: true | false
  provenance: seeded-fixture | live-db-dual | api-endpoint | none
                                 # HOW ground truth is constructed (the method).
                                 # Distinct from adapter-contract.md's oracle.kind,
                                 # which records the connection PROTOCOL (sql|http):
                                 # different axes, deliberately different field names.
  db: { engine: "<engine>", orm: "<detected ORM>" }   # detected during code
                                 # archaeology; free-text, stack-neutral (e.g.
                                 # postgres/"EF Core", or postgres/"SQLAlchemy").
  # Connection/seed/reset/snapshot mechanics (SELECT-only role, read replica,
  # ephemeral-container, transaction-rollback) are the adapter's contract, not
  # repeated here — see adapter-contract.md's `oracle:` block for the actual
  # wiring. This block only records WHETHER discover set one up, of what
  # provenance, and is what capability_matrix.execution checks as its precondition.
  verified: true | false

capability_matrix:               # which eval layers apply and what blocks them
  answer_quality:  { enabled: true,  judged: provisional }
  routing:         { enabled: true }   # router_executor/multi_agent/workflow only;
                                       # single_llm/tool_agent: no dispatch step -> N/A
  tool_selection:  { enabled: true }
  trajectory:      { enabled: false, blocked_by: "stage: pre-stability" }
                                       # also forced off outright when kind: single_llm
  multi_turn:      { enabled: false, blocked_by: "no session contract in adapter" }
  cost_latency:    { enabled: true }
  execution:       { enabled: false, blocked_by: "no oracle configured (see oracle: above) + no case carries expect.result (scalar/rows)" }
  authz:           { enabled: false, blocked_by: "no identity/persona config; the id-leak checks also need tool-result capture (forbidden_tools alone does not)" }
                                       # expect.authz is scored against the tool-call
                                       # log + returned record IDs, never a prose read.
                                       # forbidden_tools is structural (which tools fired)
                                       # and scores even without result-content capture;
                                       # forbidden/allowed record-id checks need it.

confirmed_by_human:              # from the discover interview — the app's SHOULD
  domain_boundaries:
    - "cancel subscription → billing (not support)"
  business_rules:                # become deterministic oracles in scoring
    - id: no-uncatalogued-prices
      rule: "never state a price not present in a tool result"
    - id: refund-cap
      rule: "never promise a refund exceeding order total"
  answer_expectations:
    billing: "cites the invoice id it used; no speculation about amounts"

roles:
  domain_arbiter: "<name>"       # the one person whose quality judgment is final
  reviewer: "<name or same>"

judge:
  status: uncalibrated | calibrated
                                 # DERIVED, not set by hand: `calibrated` only when
                                 # EVERY active rubric is individually calibrated. Each
                                 # rubric carries its own status.calibrated + tpr/tnr/kappa
                                 # in its rubric file (see docs/rubric-format.md); a
                                 # harness can hold several rubrics at different maturity,
                                 # and one uncalibrated active rubric keeps this global
                                 # status `uncalibrated` (the optimizer refuses judged
                                 # objectives until it flips).
  calibration: { labeled_cases: 0, tpr: null, tnr: null, kappa: null, last_checked: null }
  model: ""                      # must differ from app's model family, or
  same_family_accepted: false    # ...user consciously accepted the bias risk
                                 # model is applied as the override whenever the
                                 # judge agent is launched (run + analyze --label)
```
