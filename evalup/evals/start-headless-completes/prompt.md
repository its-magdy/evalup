---
name: start-headless-completes
tags: [start, headless, runner]
runs: 1
model: sonnet
max_turns: 120
timeout_seconds: 1800
allowed_tools: [Read, Glob, Grep, Skill]
append_system_prompt: "You are running headless (claude -p). Nobody will answer a question and nothing continues after your final message."
env:
  EVAL_APP_URL: http://127.0.0.1:18731
---
/evalup:start ./app — no transcripts, do not ask me anything, run the first session end to end. The app is already running at http://127.0.0.1:18731 (app/README.md documents it). The chat endpoint is POST /api/chat/ask with body {"message": "<text>"}; GET /healthz answers 200.
