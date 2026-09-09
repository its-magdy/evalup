# Topology Detection — the pattern catalogue

`discover` SKILL.md §2 sets `architecture.kind` from three independent signals
and cross-checks them. This file is the lookup table for all three. It decides
nothing on its own: what each `kind` turns on is in
[profile-schema.md](profile-schema.md)'s `architecture.kind` block, and the
cross-check rule is the skill's.

Why three signals and not one: each is wrong in a different, predictable way.
Code fingerprints miss anything registered at runtime. Prompt language reflects
what a team *meant* to build. Span shape only shows the paths the sample
happened to take. A router gets misfiled as a workflow when you trust one alone.

## Signal 1 — code fingerprints (white-box only)

| Pattern in the code | `kind` |
|---|---|
| One model invocation, no tool schema, no loop | `single_llm` |
| One system prompt + a tool set in a `while`-style loop: call model → execute `tool_calls` → feed results back until stop | `tool_agent` |
| An upfront classify step emitting a label, then `if`/`elif` or dict dispatch into N fixed handlers, with one "real" generation per request | `router_executor` |
| Multiple agents with their own prompts and tools, plus handoffs | `multi_agent` |
| An explicit graph built at design time, where the path *set* is fixed even when individual edges are LLM-conditional | `workflow` |

Handoff idioms that mark `multi_agent`: `transfer_to_X`, CrewAI
`Crew.kickoff` / `Process.hierarchical`, AutoGen `GroupChatManager`, OpenAI
Agents SDK `handoff` + `Runner`. Graph idioms that mark `workflow`: LangGraph
`StateGraph.add_node` / `add_edge`.

## Signal 2 — prompt language

- Orchestrator prompts say "delegate to" / "available agents:".
- Router prompts say "classify into one of the following".
- A narrow single-role prompt belongs to a sub-agent inside a larger topology,
  not to a standalone `tool_agent`.

Use this to corroborate — or to contradict — the code-fingerprint read.

## Signal 3 — OTel span shape

Count and nest the `invoke_agent` spans from the live requests step 4 sends
anyway.

| Shape | `kind` |
|---|---|
| One `invoke_agent` with looping `execute_tool`/`chat` children | `tool_agent` |
| Multiple siblings with distinct `gen_ai.agent.name` | `multi_agent` |
| Identical trace shape across many different inputs | `workflow` |
| Input-varying shape with a classify-then-dispatch pattern | `router_executor` |

## The framework fingerprint

Grep for LangGraph / CrewAI / AutoGen / OpenAI-Agents-SDK import and idiom
signatures before reading line by line, and record the hit (or `none`) in
`architecture.framework`.

It is a speed-up, never a substitute. A hit narrows which code fingerprint to
expect next, but the enum is still set from the three signals above: a team can
hand-roll a router with no framework at all, and a framework does not guarantee
the topology its name suggests.
