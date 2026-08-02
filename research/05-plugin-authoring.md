# Research: Claude Code Plugin Authoring Best Practices (official docs, 2026-07)

Docs: code.claude.com/docs/en/plugins.md, plugins-reference.md, skills.md, sub-agents.md, hooks.md

## Structure corrections vs our draft
- **Skills ARE slash commands**: `skills/discover/SKILL.md` → `/eval:discover`. No separate `commands/` directory needed — collapse commands into skills (frontmatter supports `argument-hint`, `$ARGUMENTS`/`$0`/`$1`).
- Only `plugin.json` lives in `.claude-plugin/`; everything else (skills/, agents/, hooks/, scripts/) at plugin ROOT.
- Manifest: name (kebab-case → command namespace), displayName, version (semver), description, `skills: "./skills/"`, `agents`, `hooks`, `experimental.monitors`.

## Layout (validated)
```
agent-eval/
├── .claude-plugin/plugin.json
├── skills/
│   ├── discover/SKILL.md (+references/, templates/, scripts/)
│   ├── generate/  run/  analyze/  optimize/
├── agents/
│   ├── judge.md  simulated-user.md  test-generator.md  trace-analyzer.md
├── hooks/hooks.json
├── scripts/           # hook executables
└── monitors/monitors.json   # experimental: background progress streaming
```

## Skill authoring
- Description formula: [verb] [what] [output format] "Use when [intent]" — under 2-3 sentences; this drives reliable triggering.
- Progressive disclosure: short SKILL.md; heavy material in reference files loaded on invoke (@file references).
- Dynamic context injection: `` !`cmd` `` runs a command and inlines output before Claude sees the skill.

## Subagents (agents/*.md frontmatter)
- `model: opus|sonnet|haiku` — PINS the model (key for judge ≠ app model). Also `effort`, `maxTurns`, `tools` allowlist, `disallowedTools`.
- Judge: read-only tools (Read/Grep/Glob), no Write/Edit/Bash.
- Use subagents when: separate context needed (noisy eval logs), tool restrictions, different model, long/expensive ops. Not for simple one-off helpers.

## Hooks
- Smoke-eval-on-prompt-edit is directly supported: PostToolUse with `matcher: "Write|Edit"` + `if: "Edit|Write(*.prompt.md)"` → run `${CLAUDE_PLUGIN_ROOT}/scripts/run-smoke-eval.sh`.
- Rules: exit 2 blocks the action (not exit 1); `async: true` for long hooks; keep SessionStart fast; use ${CLAUDE_PLUGIN_ROOT}/${CLAUDE_PROJECT_DIR}.

## Long-running eval runs
- `monitors/monitors.json` (experimental): background process per session, each stdout line delivered to Claude as a notification; `when: "on-skill-invoke:run"` starts it when /eval:run is invoked. Pattern: skill starts the run, returns early with status; monitor streams progress.
- `${CLAUDE_PLUGIN_DATA}`: persistent per-plugin data dir surviving updates — use for caches; app-specific eval state still lives in target repo (.agent-eval/) per our design.

## Tooling
- Scaffold: `claude plugin init eval --with skills agents hooks`
- Validate: `claude plugin validate ./agent-eval --strict`
- Local test: `claude --plugin-dir ./agent-eval`
