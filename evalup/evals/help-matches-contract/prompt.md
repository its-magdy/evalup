---
name: help-matches-contract
tags: [help, docs, traces]
runs: 1
model: sonnet
max_turns: 15
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---
/evalup:help I have an ASP.NET Core chat app that calls tools and exports OpenTelemetry to Jaeger. Where do I start, what can you measure without traces, and what does --smoke mean?
