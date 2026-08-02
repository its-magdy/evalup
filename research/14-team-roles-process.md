# Wave 3: Who Does Eval Work — Roles & Process (2026-07-18)

## Ownership models (the answer to "dev or QA?")
- **Small/medium teams: "benevolent dictator"** (Hamel Husain) — ONE domain expert is the final quality arbiter; eliminates annotation conflicts. Engineers/PMs establish context; the expert owns judgment. Only multi-domain orgs need multiple annotators + kappa-measured agreement ("add complexity only when your domain demands it").
- Larger orgs (FutureAGI RACI, Schneider Electric): Eval Platform Team (runners/tooling) + Product Teams (rubrics, golden datasets, triage) + Quality Council (safety rubrics, deploy-block authority). Schneider: 60+ agents, SMEs get scoped annotation access without dev tooling; formal maturity-gate sign-off owned by product leadership.
- Routing rule (Confident AI): domain judgment calls → SMEs; tool/retrieval failures → engineers; visible-quality/consistency → QA; PM routes and is accountable.
- **PM trend is real**: "Evals are the new PRD" (Kevin Weil, OpenAI CPO: "writing evals is the most important thing a PM can do"); Duolingo/Gusto: "engineers aren't allowed to edit prompts — only PMs and domain experts."
- **QA bifurcates, doesn't disappear**: mechanical regression execution → AI/CI; QA absorbs "Agent Oversight" (reviewing AI-generated tests for hallucinated assertions — "largest net-new responsibility"), rubric design, bias/safety auditing, exploratory testing (MORE important, not less). ISTQB now has CT-AI v2.0 (testing AI systems) vs CT-GenAI (AI as test tool) as separate certs.
- Hamel: expect **60–80% of dev time on error analysis and evaluation** — evals aren't a phase, they're most of the work.

## Labeling cadence numbers (adopt into judge-design skill)
- Initial error analysis: ≥100 traces; stop when ~20 consecutive traces yield no new failure category.
- Ongoing: 10–20 traces/week (outlier-sampled); full recalibration every 2–4 weeks on 100+ fresh traces.
- New judge: ~30 examples; Honeycomb hit >90% judge-expert agreement in 3 iterations.
- Imbalanced data: use precision/recall (TPR/TNR), never raw percent agreement.
- Braintrust 3-queue workflow: Triage (fast sort) → SME queue (fill ground truth) → Calibration queue (multi-reviewer re-score to check rubric alignment). Blind labeling; item reservations. Keep rubric short at first.

## CI/SDLC integration patterns
- Common shape: pre-commit (mocked, sub-second) → PR gate (real calls, threshold-blocked) → pre-deploy (full suite, blocks on >2% drop) → production monitoring (sampled scoring, auto-rollback windows). (Galileo, Braintrust, LangSmith, FutureAGI.)
- FutureAGI details worth stealing: hard exit-code contract (0=merge, 2=hard-fail, 3=warn); **statistical delta-gate (Welch's t-test vs 7-day baseline) to avoid "gate theater"**; cheap-deterministic-every-PR / frontier-judge-nightly / human-review-of-flagged-clusters cascade.
- Ramp: every user-flagged production error → eval case ("evals are the new unit tests").
- Decagon: tests auto-generated from production conversations; CX (non-engineer) teams own QA via dashboard; 93% of AI-proposed changes accepted by human reviewers.
- Anthropic: evals are "first line of defense," signal not sole gate; even non-engineers contribute eval tasks as PRs via Claude Code.

## Reporting for non-engineers
- Braintrust: THREE dashboards for three audiences — Leadership (one headline metric + trend + cost, readable in 30 seconds), Engineering (p95, errors, tokens), Product review (quality by segment). Traces re-rendered as domain objects (ticket cards), not JSON.
- Decagon QA Hub: scored conversations + rubric-backed evidence + plain-language auto-generated reports for CX teams.
- Zendesk consensus KPIs: AI resolution 50%+, CSAT 90%+, task accuracy 95%+, escalation <15%; human approval gates above value thresholds.

## Design adoption
1. Plugin docs define three ROLES (builder / reviewer / domain-arbiter) even when one person wears all hats — the labeling role is named and its time cost budgeted (~1–2 h/week steady state).
2. run's CI mode: exit-code contract + absolute floor + statistical delta-gate vs rolling baseline.
3. Reports: audience-split — one-page headline view + engineering appendix (extends v2 report spec).
4. Labeling cadences and 3-queue pattern → judge-design skill defaults.
5. analyze: production-error→eval-case promotion is confirmed standard practice (Ramp/Decagon).
