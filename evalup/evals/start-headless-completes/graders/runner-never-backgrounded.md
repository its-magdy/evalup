---
type: tool_used
tool: Bash
input_match: 'run_cases\.py.*"run_in_background"\s*:\s*true|"run_in_background"\s*:\s*true.*run_cases\.py'
min: 0
max: 0
weight: 2
---
run/SKILL.md §2: the runner is launched in the foreground, never with run_in_background (field test 2026-09-24, ISSUES I-013: the CLI exit killed a backgrounded runner mid-case).
