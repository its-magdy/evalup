# Design Critique 2: First-User Friction Log (2026-07-18)

Persona: mid-level dev, half-finished router+domain app (FastAPI, prompts as Python strings, partial OTel, no staging, no eval experience, 4–6 h/week). Walked day 1 → week 4.

## The one structural insight
**The design front-loads week-4 rigor onto a day-1 user whose app changes daily.** Rigor must be SEQUENCED by app maturity, not offered all at once. Explicit "too early" answer needed: full trajectory eval before domain/tool boundaries are stable is too early — discover should say so to the user's face.

## Three abandonment points (in probability order)
- **Exit A (day 1, high prob):** discover diagnoses OTel/invocation gaps but makes the user fix them = unbounded side quest, zero reward. FIX: discover PATCHES the app (writes GenAI instrumentation, content capture, trace-ID echo, invocation shim) and verifies with one smoke request. It's Claude Code — it can edit the app.
- **Exit B (day 3–4, medium):** first run costs unknown money, returns a metrics table with no actions. FIX: cost dry-run estimate; report headline = top-3 failure clusters with expected-vs-actual example traces + implicated surface; metrics are the appendix. And sequence so the deterministic confusion-matrix "aha" arrives day 1–2.
- **Exit C (week 2, high without fixes):** normal refactor (domains merged, tools renamed) → dataset stale → red wall → hour of triage → "harness is maintenance load," delete. FIX: `discover --diff` + automated dataset migration + pre-stability mode.

## Key frictions and required fixes
1. Five entry points = toolbox, not path. → `/agent-eval:start` wizard: "step 1 of 5, next is X."
2. No GenAI spans at all is the COMMON first state (partial OTel = HTTP spans only). → remediation is discover's job, not the user's.
3. Console exporter / no collector locally. → ship a blessed zero-infra local mode (file exporter snippet).
4. Discover's routing questions expose DESIGN GAPS (no OOS route, fuzzy boundaries). → output a findings list ("you have no out-of-scope handling") as a first-class artifact — possibly week-1's most valuable output.
5. App needs JWT + DB row to invoke. → "call the internal function below HTTP" as first-class adapter mode, trade-offs stated.
6. 15 tools with side effects + zero traffic to record = replay chicken-and-egg. → "record one seeding pass" flow; discover reads code and NAMES which tools need mocking before any run.
7. Pre-launch synthetic dataset: be explicit — "scaffold for regression, NOT a measure of real-world quality" + standing re-seed reminder.
8. **User will not review 100 cases (9pm, skims 15, accepts all → ~10-15% wrong labels = permanent noise floor).** → disagreement sampling: surface only ~20 suspicious cases (generator uncertain OR app's answer disagrees with label); 15-minute review users actually do.
9. Expected trajectories are a spec the user doesn't possess; generator inferring them from code = grading app against itself. → default multistep cases to end-state/answer + policy assertions; exact trajectories only where human-specified.
10. Adversarial cases 500-crash the half-built app → sea of red, demoralizing. → "crash rate" as its own metric (valuable pre-launch!); categories markable "tracked, not gating."
11. No pre-run cost/time estimate. → 3-case probe → "$~14, ~25min, proceed?"; --smoke as everyday default.
12. Timestamp-based trace matching = plausible wrong numbers. → HARD-FAIL on heuristic correlation; offer to patch app to echo traceparent.
13. "Trajectory precision 0.72" is decoration without threshold/comparison/exemplars + which matcher produced it.
14. **Judge calibration will be deferred indefinitely; nothing visibly changes when skipped.** → watermark every judge number PROVISIONAL until calibrated; `/optimize` REFUSES uncalibrated judge objectives; labeling = 15-min flow (judge pre-sorts, shows reasoning, agree/disagree/edit-rubric per case). Rubric-editing-while-grading must be discoverable, not homework.
15. Cross-family judge needs a second provider account. → state risk plainly; allow conscious acceptance of same-family as degraded mode; never block or silently default.
16. **What worked: deterministic layers** — confusion matrix immediately showed domain bleed. That moment buys patience for everything else; sequence it first.
17. Refactor week: see Exit C. Also need explicit pre-stability mode: invariants only (never crash, always respond, valid JSON, no loops, refuses adversarial), NO route/trajectory assertions while architecture churns.
18. **Holdout discipline lasted 4 days** (curiosity at n=20). → physical separation: holdout details not in browsable report, aggregate only; explicit logged "unseal" command. Make the wrong thing a deliberate act.
19. Prompts as f-strings built by helper functions break the candidate-file model. → discover offers one-time mechanical refactor (extract to template files, smoke-verify no behavior change), pitched DAY 1 as "unlocks the optimizer."
20. Statistical gate says no to everything at n=50. → translate stats to instructions ("n=50 detects ~15pp; for 5pp you need ~200 — here's the generate command"); Bayesian "80% prob of improvement" mode with looseness stated.
21. Optimizer proposals span text-edit → code change → architecture. → tag by effort tier; auto-apply-and-measure only text edits; tool-description rewrites are the advertised entry point (cheapest, proven biggest lever).
22. Half the plugin is for a later life stage. → profile-gated docs/skills; hide what doesn't apply yet.

## The golden path (80% subset for an in-development app)
1. `/discover` that patches (instrumentation, trace echo, invocation shim, prompt extraction) and lists design findings.
2. ~30 cases, disagreement-sampled review of ~15; routing + OOS + few adversarial.
3. Deterministic scoring only (confusion matrix, tool selection, format, loops, crash rate). No judge yet.
4. `--smoke` wired to prompt-edit hook: sub-dollar, sub-5-min — the habit-forming loop.
5. Baseline diff + dataset migration.
Everything else unlocks when discover detects preconditions (stable schema 2+ weeks, real traffic, 200+ cases).
