---
name: generate-no-self-review
tags: [generate, review, gating, findings]
runs: 1
model: sonnet
max_turns: 100
timeout_seconds: 1500
allowed_tools: [Read, Glob, Grep, Skill]
env:
  EVAL_APP_URL: http://127.0.0.1:18731
---
/evalup:generate --count 12 for the app at ./app (its profile, adapter and discover findings are in app/.evalup/). I am not available to review cases or answer questions. The app is running at http://127.0.0.1:18731 (POST /api/chat/ask, body {"message": "<text>"}); do not send more than 3 live requests.
