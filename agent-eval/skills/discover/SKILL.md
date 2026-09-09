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

Write three artifacts to the state location (default `<app>/.agent-eval/`, or
the adapter's `state_location`): `profile.yaml` (what the app is — fields in
[references/profile-schema.md](references/profile-schema.md)), `adapter.yaml`
(how to talk to it — fields in
[references/adapter-contract.md](references/adapter-contract.md)), and
`findings.md` (design gaps, often week one's most valuable output). Those two
schemas own what each field *means*; this file owns how to decide its value
from evidence.

## Procedure

### 1. Determine access level
Set `access_level` in adapter.yaml:
- Repo path given → **white**: full discovery and optimization.
- Endpoint + reachable traces → **gray**: trajectory evals, recommendations only.
- Endpoint only → **black**: answer-level evals only. Map capabilities by probing
  — a few exploratory conversations, note fallback behavior, infer route targets
  from responses.

### 2. Topology detection — an explicit enum, not a vibe
Set `architecture.kind` to exactly one of `single_llm | tool_agent |
router_executor | multi_agent | workflow`, underscores included. This enum
mechanically drives the capability matrix below; profile-schema.md's
`architecture.kind` block says what each spelling turns on.

Detect from three independent signals and cross-check them against each other:
1. **Code fingerprints** (white-box).
2. **Prompt language** — orchestrator, router, or narrow single-role.
3. **OTel span shape**, from the 1–3 live requests step 4 sends anyway.

The catalogue for all three, plus the framework-signature grep that speeds them
up, is [references/topology-detection.md](references/topology-detection.md).
Record each signal's evidence under `architecture.detected_from`, so a human can
audit why you said `router_executor` and not `multi_agent`. Disagreement between
signals is itself a finding — never silently pick one and move on.

### 3. Code archaeology (white-box)
Read the code for: agent definitions and their system prompts (files, constants,
DB references), tool schemas and descriptions, routing/dispatch definitions (the
route targets the app chooses between, if any), model configuration,
conversation/session handling, OTel setup. Record as you go:
- Every **optimizable surface** — prompt and tool-description locations → `prompts[]`.
- Every tool's **side-effect class**, `safe-live | needs-mock | never-live` as
  adapter-contract.md defines them. Confirm each with the human.
- `record_id_pattern` — the regex matching the app's own record identifiers
  (order ids, invoice ids, primary keys).
- The **data-access layer** → `oracle.db`: DB and ORM, where schema/migrations
  live, and whether a seeded staging environment or read replica already exists.
  You cannot offer a fixture in step 5 without knowing what there is to seed.

### 4. Validate the profile against reality — MANDATORY before generate
Static reading is confidently wrong for dynamic apps (DB-held prompts, runtime
tool registration, feature flags). Send 1–3 harmless requests through the adapter
and compare observed agent names, tool names and `invoke_agent` span shape
against the inferred profile — this is also step 2's span-shape evidence. Mark
every profile entry `verified: true|false`. Generation must not proceed on
unverified core entries: say so, and fix first.

### 5. Patch, don't assign homework
For each gap found, OFFER to fix it now. It is the user's code: get a yes, then
edit. A "no" is fine — see Read-only mode below.
- No GenAI spans → instrument their stack: a span per LLM call, `invoke_agent`
  per agent stage with `gen_ai.agent.name`, `execute_tool` per tool with
  `gen_ai.tool.name`.
- Content capture off → enable it (env var or code), warn about the PII
  implications, record the answer as `data.may_contain_pii`.
- No trace-ID echo → add `traceparent`, or a trace-id field, to the response.
- Non-`gen_ai` trace convention → write the `traces.mapping_shim` and verify its
  output on one live trace. No shim keeps the trajectory layers off.
- No clean invocation path (auth walls) → add a test-mode entry point or an
  internal-function shim, and record which in adapter.yaml.
- Prompts as string literals or f-strings → offer the one-time extraction
  refactor: prompts to files, code loads them, 2–3 smoke requests to show no
  behavior change. Pitch it as what unlocks the optimizer later.
- **No ground-truth / oracle path** → offer a seeded fixture plus a read-only
  oracle connection, never the app's write connection. Connection, seed and reset
  mechanics are the adapter's job (adapter-contract.md's `environment:` and
  `oracle:` blocks); this step decides only **whether** one exists, and records
  `oracle:` in profile.yaml.

After each patch, re-verify with one live request.

### Read-only mode — declining patches is normal
Read-only repos, change freezes, QA without write access and third-party audits
are supported paths, not improvisations for when things go wrong. With
`repo_access: read-only` set, never offer a code patch at all. That includes
step 5's oracle: propose the read-only *role* against an existing replica if
there is one, never seed or reset scripts written into the app repo. Pairing it
with an out-of-tree `state_location` is the expected shape for a QA sandbox or
third-party audit. For every patch declined or not offered:
- Record it in findings.md as `declined: <patch> → costs <capability>`, e.g.
  "declined: no oracle → execution layer unavailable, data-Q&A cases stay
  answer-graded only".
- Include the copy-paste patch text, fixture and role-grant scripts included.
- Continue with whatever capabilities remain; the capability matrix records what
  stayed off and why. Never stall on a "no".

### 6. Assess maturity → set the stage
Ask the user, and check git history: are the app's route-target and tool
boundaries stable, or still changing week to week?
- Churning → `stage: pre-stability`. Only invariant checks apply: crash rate,
  responds-always, format compliance, loop detection, refusal of the adversarial
  set. Say plainly that full trajectory eval would rot within a week, then name
  what you track instead and what unlocks when.
- Stable → `stage: stable`; trajectory layers apply.
- Real traffic exists → also enable analyze's trace-mining paths.

### 7. Interview the human
Code says what the app does; only humans know what it should do. Ask, and record
the answers under `confirmed_by_human:` in profile.yaml:
- Route-target boundaries for ambiguous query types, with 3 concrete examples you
  found. Skip for `single_llm`/`tool_agent`, which have no dispatch step.
- What must the bot never do or say → business-rule oracles. Draft rules from any
  policy docs or READMEs found, and ask for confirmation.
- What a good answer looks like, per route target (or overall) → seeds the rubric.
- Who is the domain arbiter, the final word on answer quality → `roles:`. That
  role name is fixed regardless of architecture.

### 8. Report findings
`findings.md`: design gaps (no OOS route, overlapping route targets, vague tool
descriptions, missing confirmation on destructive tools), each with evidence and
a suggested fix. Present the top 3 in chat.

## `--diff` mode (after app changes)
Re-run steps 2–4 against the current code, topology detection included: a
refactor can move an app from `tool_agent` to `router_executor`, which flips
which layers apply. Diff old profile against new, list renamed/removed/added
agents/tools/route targets, then map onto the dataset — which cases reference
stale names. Propose bulk migrations: mechanical renames are auto-fixable,
semantic changes get flagged for human review. This command is the answer to "my
app changed"; never let the user meet staleness as a wall of red instead.

## Capability matrix (write into profile.yaml)
Give every layer an `enabled`, and when false a named `blocked_by`. The layers
and their preconditions are profile-schema.md's `capability_matrix:` block —
work down it and check each precondition against what steps 1–7 actually found,
rather than restating the block from memory.

Three calls that block leaves to you:
- The step 2 enum drives routing and trajectory mechanically, not by convention:
  `single_llm` forces both off whatever else you found.
- Answer quality is always on; cost/latency follows purely from traces existing.
- `authz` enables *partly* when the id-leak preconditions are missing — enable it
  and name the degradation in `blocked_by`, rather than calling the layer off.

Never leave a layer off without a `blocked_by`. `run` reads this matrix, and an
unnamed blocker reaches the user as an unexplained `unscorable`.
