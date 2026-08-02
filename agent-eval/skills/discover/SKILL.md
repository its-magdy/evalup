---
name: discover
description: >-
  Profile a target LLM app: read its code and/or traces, determine architecture
  (agents, route targets, tools, flow), write .agent-eval/profile.yaml and
  adapter.yaml, patch missing instrumentation, and report design findings. Use
  when pointing agent-eval at an app for the first time, or with --diff after
  the app changed to find stale eval cases.
argument-hint: "[path-or-url] [--diff]"
---

# Discover — Profile the App

Output artifacts, written to the state location (default `<app>/.agent-eval/`,
or the adapter's `state_location` — any external path is fine for read-only repos):
`profile.yaml` (what the app is; schema in @references/profile-schema.md),
`adapter.yaml` (how to talk to it; contract in @references/adapter-contract.md),
and `findings.md` (design gaps found — often week one's most valuable output).

## Procedure

### 1. Determine access level
- Repo path given → **white-box**: full discovery + optimization possible.
- Endpoint + reachable traces → **gray-box**: trajectory evals, recommendations only.
- Endpoint only → **black-box**: answer-level evals only (probe the endpoint
  conversationally to map capabilities — a few exploratory conversations,
  note fallback behavior, infer route targets from responses).

### 2. Topology detection — an explicit enum, not a vibe
Set `architecture.kind` to exactly one of `single_llm | tool_agent |
router_executor | multi_agent | workflow` (schema in
@references/profile-schema.md). This enum **mechanically drives which eval
layers turn on** (recorded in `capability_matrix`, see bottom of this file):
`single_llm` → no trajectory/routing (asserting a trajectory over one bare
call is noise, not rigor); `tool_agent` → full trajectory (tool selection +
args + loop termination) applies; `router_executor` → routing is scored **as
a classifier** (precision/recall/confusion matrix per route target) kept
**separate** from conditional-executor quality per route; `multi_agent` →
per-handoff capture (full prompt+response at every agent-to-agent boundary,
not just aggregate spans); `workflow` → the path *set* is fixed at design
time, so plan per-node golden-behavior regression instead of open-ended
trajectory scoring.

Detect from three independent signals and cross-check them against each
other — trusting one alone is how a router gets misfiled as a workflow:
1. **Code fingerprints** (white-box): one model invocation, no tool schema,
   no loop = `single_llm`. One system prompt + tool set wrapped in a
   `while`-style loop that calls model → executes tool_calls → feeds results
   back until stop = `tool_agent`. An upfront classify step emitting a label
   then `if/elif`/dict dispatch into N fixed handlers, only one "real"
   generation per request = `router_executor`. Multiple agents with their own
   prompts/tools and handoffs (`transfer_to_X`, CrewAI
   `Crew.kickoff`/`Process.hierarchical`, AutoGen `GroupChatManager`, OpenAI
   Agents SDK `handoff`+`Runner`) = `multi_agent`. An explicit graph built at
   design time (LangGraph `StateGraph.add_node`/`add_edge`) where the path
   *set* is fixed even if edges are LLM-conditional = `workflow`.
2. **Prompt language**: orchestrator prompts say "delegate to / available
   agents:"; router prompts say "classify into one of the following"; narrow
   single-role prompts belong to a sub-agent, not a standalone `tool_agent`.
   Use this to corroborate — or contradict — the code-fingerprint read.
3. **OTel span shape**: count + nesting of `invoke_agent` spans (sample from
   the 1–3 live requests step 4 sends anyway). One `invoke_agent` with
   looping `execute_tool`/`chat` children = `tool_agent`; multiple siblings
   with distinct `gen_ai.agent.name` = `multi_agent`; identical trace shape
   across many different inputs = `workflow`; input-varying shape with a
   classify-then-dispatch pattern = `router_executor`.

**Framework fingerprint step** (a speed-up, never a substitute for the enum):
before reading line-by-line, grep for LangGraph / CrewAI / AutoGen /
OpenAI-Agents-SDK import + idiom signatures (or note none found). Record the
match in `architecture.framework` (`langgraph | crewai | autogen |
openai-agents-sdk | custom | none`). A framework hit narrows which
code-fingerprint pattern to expect next, but the topology enum is still set
from the three signals above — a team can hand-roll a router with no
framework at all, and a framework doesn't guarantee the topology you'd
assume from its name.

Record each signal's evidence under `architecture.detected_from` so a human
can audit *why* discover called it `router_executor` and not `multi_agent`.
Disagreement between signals is itself a finding for findings.md — never
silently pick one and move on.

### 3. Code archaeology (white-box)
Read the codebase for: agent definitions and their system prompts (files,
constants, DB references), tool schemas and descriptions, routing/dispatch
definitions (the route targets — domains, nodes, or sub-agents — the app
chooses between, if any), model configuration, conversation/session handling,
OTel setup.
Record every **optimizable surface** (prompt/tool-description locations) and
every **tool's side-effect class**: `safe-live` (read-only), `needs-mock`
(writes to shared state), `never-live` (external side effects: email, payments,
tickets).

Also detect the app's **data-access layer** for ground truth: which DB/ORM
(Postgres/MySQL/SQL Server + Entity Framework/SQLAlchemy/Prisma/etc.), where
schema/migrations live, and whether a seeded-staging environment or read
replica already exists. You can't offer a seeded fixture in step 5 without
first knowing what to seed — this is the input to the oracle offer below.

### 4. Validate the profile against reality — MANDATORY before generate
Static reading is confidently wrong for dynamic apps (DB prompts, runtime tool
registration, feature flags). Send 1–3 harmless requests through the adapter
and compare observed traces (agent names, tool names, `invoke_agent` span
shape) against the inferred profile — this is also where the OTel-span-shape
topology signal from step 2 gets its evidence. Mark every profile entry
`verified: true|false`. Generation must not proceed on unverified core
entries — say so and fix first.

### 5. Patch, don't assign homework
For each gap found, OFFER to fix it now (it's the user's code — get a yes, then
edit; a "no" is fine — see Read-only mode below):
- No GenAI spans → write instrumentation for their stack (span per LLM call,
  `invoke_agent` span per agent stage with `gen_ai.agent.name`, `execute_tool`
  span per tool with `gen_ai.tool.name`).
- Content capture off → enable it (env var or code), warn about PII implications.
- No trace-ID echo → add `traceparent` (or a trace-id field) to the app's response.
- Traces use a non-`gen_ai` convention (openinference / openllmetry-legacy) →
  write the per-adapter mapping shim (declared as `traces.mapping_shim` in
  adapter.yaml; converts spans to `gen_ai.*` keys before `normalize_trace.py`)
  and verify its output on one live trace. No shim → trajectory layers stay
  off; record as a finding.
- No clean invocation path (auth walls) → add a test-mode entry point or an
  internal-function shim; record in adapter.yaml which mode is used.
- Prompts as string literals/f-strings → offer the one-time extraction refactor
  (prompts to files, code loads them; verify no behavior change with 2–3 smoke
  requests). Pitch: "unlocks the optimizer later."
- **No ground-truth / oracle path** → OFFER to stand up a seeded fixture
  (frozen seed DB / Testcontainers / docker-compose seeded volume) plus a
  read-only oracle connection (`SELECT`-only DB role or read replica, never
  the app's write connection) so the execution layer can compute `expected`
  from a reference query against known state. The connection/seed/reset
  mechanics are the adapter's job — see adapter-contract.md's `environment:`
  block for the contract this wires into. This step only decides **whether**
  one exists and records it as `oracle:` in profile.yaml (schema in
  @references/profile-schema.md), which is what `capability_matrix.execution`
  checks as its precondition. Declining is normal — see Read-only mode below;
  record `oracle: { available: false }` plus the declined-patch entry and the
  copy-paste fixture/role-grant script in findings.md.
After each patch, re-verify with one live request.

### Read-only mode — declining patches is normal
Declining any or all patches is a normal, supported path (read-only repo
rules, change freezes, QA without write access, third-party audits) — not an
improvisation to fall back on when things go wrong. If `repo_access:
read-only` is set in adapter.yaml, don't offer code patches at all, ever
(this includes the oracle/fixture offer above: propose the read-only oracle
*role* against an existing replica if one exists, but never propose writing
seed/reset scripts into the app repo). Pairing `repo_access: read-only` with
an out-of-tree `state_location` (adapter-contract.md) — so all eval state
lives outside the app repo entirely — is the expected shape for a QA sandbox
or third-party audit; treat it as a first-class configuration, not a
degraded one. For every patch declined or not offered:
- Record each declined patch in findings.md as
  `declined: <patch> → costs <capability>` (e.g. "declined: no trace-id echo →
  trajectory layers unavailable", "declined: no oracle → execution layer
  unavailable, data-Q&A cases stay answer-graded only").
- Include the copy-paste patch text in findings.md so the app team can apply
  it themselves later.
- Continue the procedure with whatever capabilities remain; the capability
  matrix records what stayed off and why. Never stall on a "no".

### 6. Assess maturity → set the stage
Ask the user (and check git history): are the app's route-target and tool
boundaries stable, or still changing week to week?
- Churning → `stage: pre-stability`. Only invariant checks apply (crash rate,
  responds-always, format compliance, loop detection, refusal of the
  adversarial set). Say plainly: "full trajectory eval is too early; it would
  rot within a week. Here's what we track instead, and what unlocks when."
- Stable → `stage: stable`; trajectory layers apply.
- Real traffic exists → also enable trace-mining paths in analyze.

### 7. Interview the human
Code says what the app does; only humans know what it should do. Ask, and
record answers under `confirmed_by_human:` in profile.yaml:
- Route-target boundaries for ambiguous query types — give 3 concrete examples
  found (`router_executor`/`multi_agent`/`workflow` apps only; skip for
  `single_llm`/`tool_agent`, which have no dispatch step).
- What must the bot never do/say (→ business-rule oracles; also draft rules
  from any policy docs/READMEs found and ask for confirmation).
- What does a good answer look like, per route target (or overall, for a
  `single_llm`/`tool_agent` app) (→ seeds the rubric).
- Who is the domain arbiter — the person with the final word on answer quality
  (→ `roles:`; the role name is fixed regardless of architecture).

### 8. Report findings
`findings.md`: design gaps (no OOS route, overlapping route targets, tools with
vague descriptions, missing confirmation on destructive tools), each with
evidence and a suggested fix. Present top 3 in chat.

## `--diff` mode (after app changes)
Re-run steps 2–4 against the current code (topology detection included — a
refactor can move an app from `tool_agent` to `router_executor`, which flips
which layers apply), diff old vs new profile, list renamed/removed/added
agents/tools/route targets, then map to the dataset: which cases reference
stale names → propose bulk migrations (mechanical renames auto-fixable;
semantic changes flagged for human review). Never let the user discover
staleness via a wall of red — this command is the answer to "my app changed."

## Capability matrix (write into profile.yaml)
Layers on: answer quality (always) · routing (`route_targets` exist —
`router_executor`/`multi_agent`/`workflow` only; never `single_llm`, which has
no dispatch step) · tool selection/args/trajectory (tools exist + stage ≥
stable; `single_llm` forces this off regardless of stage) · multi-turn (app is
conversational + adapter supports sessions) · cost/latency (traces exist) ·
**execution** (an `oracle` is configured — see step 5 — and at least one case
carries a `reference_query`/seeded fixture; blocked otherwise, and the
connection mechanics live in adapter-contract.md, not here) · **authz** (an
identity/persona config exists for the app and the adapter captures tool-result
content, not just the chat text — blocked otherwise, since `expect.authz` is
scored against the tool-call log and returned record IDs, never a prose read)
· judged layers (judge calibrated) — each with its blocking precondition named.

The topology enum from step 2 drives this table mechanically, not just by
convention: `single_llm` forces `routing` and `trajectory` off outright;
`router_executor` turns `routing` on scored as a classifier and reports
conditional-executor quality per route as a separate number; `multi_agent`
turns on per-handoff capture in addition to per-agent trajectory; `workflow`
substitutes per-node golden-behavior regression for open-ended trajectory
scoring.
