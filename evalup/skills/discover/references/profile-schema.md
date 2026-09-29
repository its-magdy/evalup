# Profile Schema (`.evalup/profile.yaml`)

The profile is the harness's model of the app — and a contract the human
confirms. Every inferred entry carries `verified` (checked against live
traces) so downstream skills know what they can trust.

Two neighbouring fields readers look for here live in `adapter.yaml`, not the
profile: `app.repo_access: read-write | read-only` and `state_location`
(any path, in or out of the app tree) — both defined in
[adapter-contract.md](adapter-contract.md)'s first block.

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
  #                       executor quality per route. Only when a route is
  #                       observable per target (traces or route_from_response):
  #                       under route_from_status alone it measures
  #                       answered-vs-refused, never the targets.
  #   multi_agent     -> per-agent trajectory. Per-HANDOFF capture (prompt+response at
  #                       each agent-to-agent boundary) is RESERVED: normalize_trace.py
  #                       records no prompt or response text at all, and tool calls carry
  #                       no owning agent, so no expectation can be scoped to one
  #                       sub-agent. What DOES ship per agent: the `agents[]` stage list
  #                       with duration and rolled-up tokens (normalize_trace.py's `agents[]` output).
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

# No `conversation:` block. Multi-turn is RESERVED (capability_matrix below).

record_id_pattern: "INV-[0-9]+"  # regex for the app's own record identifiers.
                                 # Passed by run to `score_authz.py --id-pattern`.
                                 # Omit only if the default `letters[-_]digits`
                                 # recognizer already matches; UUID-keyed apps
                                 # MUST set it or their authz
                                 # `allowed_record_ids` checks score `unscorable`.
                                 # Plain-integer ids with no prefix: leave it
                                 # unset and say why in authz `blocked_by` --
                                 # any pattern for a bare number also matches
                                 # every count, page and year in an answer, so
                                 # the id-leak checks stay unscorable by design.

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
                                       # single_llm/tool_agent: no dispatch step -> N/A.
                                       # Trace-less + route_from_status: enabled, but it
                                       # measures answered-vs-refused (runner-contract
                                       # SS5.2); per-target numbers need route_from_response
                                       # or traces.
  tool_selection:  { enabled: true }
  trajectory:      { enabled: false, blocked_by: "stage: pre-stability" }
                                       # also forced off outright when kind: single_llm
  multi_turn:      { enabled: false, blocked_by: "reserved: no conversation driver in the harness. NOT fixable from the adapter -- cases with >1 user turn are skipped" }
  state:           { enabled: false, blocked_by: "reserved: no state-diff scorer in the harness. NOT fixable from the adapter -- run_cases.py never invokes environment.seed/.reset/.snapshot_state, so expect.state scores nothing and a state-only case is a validator ERROR" }
  cost_latency:    { enabled: true }
                                       # run-level, NOT a per-case layer: no row in
                                       # runner-contract.md SS5's table and none in
                                       # LAYER_ORDER, so no case gets a cost verdict
                                       # (that would need a per-case budget nothing
                                       # declares). `true` asserts the traces carry
                                       # gen_ai.usage.* + gen_ai.request.model, so
                                       # scripts/score_cost.py can price a finished
                                       # run against a DECLARED price table. Forced
                                       # off in effect on a trace-less run: no
                                       # trajectory.json means no token count at all.
                                       # Latency needs no trace and is always there.
                                       # TTFT and cached tokens are RESERVED.
  execution:       { enabled: false, blocked_by: "no oracle configured (see oracle: above) + no case carries expect.result (scalar/rows)" }
  authz:           { enabled: false, blocked_by: "no identity/persona config; the id-leak checks additionally need tool-result capture and a matching record_id_pattern (forbidden_tools scores without either)" }
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
                                 # in its rubric file (see ${CLAUDE_PLUGIN_ROOT}/docs/rubric-format.md); a
                                 # harness can hold several rubrics at different maturity,
                                 # and one uncalibrated active rubric keeps this global
                                 # status `uncalibrated` (the optimizer refuses judged
                                 # objectives until it flips).
  calibration: { labeled_cases: 0, tpr: null, tnr: null, kappa: null, last_checked: null }
                                 # WHAT derives it: ${CLAUDE_PLUGIN_ROOT}/scripts/
                                 # score_agreement.py, over the calibration lines of an
                                 # annotation JSONL. Its --write sidecar (paths.
                                 # judge_calibration) is what run_cases.py checks; this
                                 # block is the human-readable copy, and on its own it
                                 # opens nothing.
  model: ""                      # must differ from app's model family, or
  same_family_accepted: false    # ...user consciously accepted the bias risk
                                 # model is applied as the override whenever the
                                 # judge agent is launched (run + analyze --label)
```
