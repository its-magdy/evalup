# The discover interview (step 7)

Run by `${CLAUDE_PLUGIN_ROOT}/skills/discover/SKILL.md` step 7 — never on a
lean first session, where it waits under findings.md's `deferred:`.

Code says what the app does; only humans know what it should do. Ask, and record
the answers under `confirmed_by_human:` in profile.yaml:
- Route-target boundaries for ambiguous query types, with 3 concrete examples you
  found. Skip for `single_llm`/`tool_agent`, which have no dispatch step.
- What must the bot never do or say → business-rule oracles. Draft rules from any
  policy docs or READMEs found, and ask for confirmation.
- What a good answer looks like, per route target (or overall) → seeds the rubric.
- Who is the domain arbiter, the final word on answer quality → `roles:`. That
  role name is fixed regardless of architecture.
- May the eval send attack prompts to this environment →
  `environment.safe_to_attack` in adapter.yaml. It is a governance decision,
  never a default you pick: while it is false every `adversarial-refusal` case
  is skipped (runner-contract §7). Name the environment's `kind` and the tools'
  side effects when you ask.
