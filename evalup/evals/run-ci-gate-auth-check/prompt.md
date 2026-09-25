---
name: run-ci-gate-auth-check
tags: [run, ci, docs]
runs: 1
model: sonnet
max_turns: 20
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---
/evalup:help How do I gate CI on a run? Read the run skill's reference section "Headless/CI gate — worked example" and, without changing anything, tell me exactly what the CI log shows when ANTHROPIC_API_KEY is absent or invalid: which command fails and how, which check in the recipe catches it, whether gate.py runs at all, and quote the lines of the recipe that decide it.
