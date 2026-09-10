---
name: optimize
description: >-
  Improve the app's prompts and tool descriptions through a failure-driven
  loop: reflect on failure clusters, propose one targeted edit, measure on the
  training split, confirm on the sealed holdout with a statistical gate, keep
  or revert. Use when eval scores have plateaued or a failure cluster
  implicates a specific prompt or tool description.
argument-hint: "[--surface <prompt-id|tool>] [--budget $N]"
---

# Optimize — The Reflective Loop

Methodology = GEPA's discipline implemented natively: natural-language
reflection on trajectories, a candidate pool with per-case scores, a sealed
holdout, a hard budget. See
[references/loop-discipline.md](references/loop-discipline.md) for the rules that
make gains real instead of overfit.

## Preconditions — check, and refuse with the unlock path if unmet
1. `judge.status: calibrated` — if any objective dimension is judge-scored.
   An uncalibrated judge as an optimization target produces a confidently
   worse app; this is non-negotiable. Check the flag's **evidence**, not the
   flag: `paths.judge_calibration`'s sidecar (`score_agreement.py`) is what
   derives it, and a `calibrated` profile with no sidecar behind it is a
   hand-set flag — the runner already refuses it.
2. Usable case count ≥ ~100 for judged objectives (deterministic-only
   objectives may proceed at ≥ ~50 with Bayesian gating).
3. `stage:` ≥ stable (set by `discover` §6) — below that, route-target and
   tool boundaries are still moving, so a measured gain is indistinguishable
   from drift.
4. White-box prompt access (gray-box → recommendations only, written to
   findings).
5. A budget (`--budget` or ask).

## The loop (one iteration per session unless told otherwise)
1. **Ground first, edit never-first**: read the latest `analyze --cluster`
   output. Pick ONE cluster. Read its actual traces. Name the metric contract
   being violated and the surface implicated. If evidence is ambiguous between
   surfaces, say so and stop — more cases beat guessed edits.
2. **Choose the lever — full set, effort-tagged:**
   - tool descriptions (cheapest, empirically the biggest lever — start here
     when tool-selection errors exist; 3–4 sentences, when-to-use and
     when-NOT-to-use, unambiguous arg names),
   - system prompt sections, few-shot examples,
   - tool consolidation / route-target boundary / model tier / added OOS route —
     these are code or architecture changes: PROPOSE with evidence and effort
     tag, do not implement inside the loop.
3. **Propose ONE candidate** (one surface, one coherent change). Write it to
   `candidates/<id>/` with: the diff, the reflection (why this failure ←
   this cause ← this fix), predicted affected cases.
4. **Measure**: apply to a working copy and run the training split — that is
   `/agent-eval:run --regression` (whose selection is every case whose `split`
   field contains `full`, which by construction excludes the sealed holdout —
   the two splits are mutually exclusive), or `--targeted --tag <component>`
   when the edit is scoped to one surface and you want the faster loop — under
   a new manifest. Compare paired per-case vs current champion.
5. **Gate on holdout**: only if training looks positive, run the sealed
   holdout (aggregate) via `/agent-eval:run --holdout`. That run spends one of
   the N=5 looks and records itself in the holdout-look ledger (see
   `${CLAUDE_PLUGIN_ROOT}/skills/run/SKILL.md` §4) — which is why step 4's
   training measurement is a precondition, not a formality. `stats.py` decides
   on one rule at every n: exact Bayesian P(improvement) ≥ 0.8
   (`--bayes-threshold`) and delta > 0, with an exact one-sided sign test
   reported alongside. Two caveat keys ride beside that decision and both must
   be surfaced rather than reported as a bare "kept": `gate_note` (kept on the
   posterior bar while the sign test is not significant) and `sub_mde_keep`
   (the observed delta is smaller than the run's own minimum detectable
   effect — the direction is evidenced, the SIZE is not, so do not quote the
   delta as a measured improvement). Pass → keep (apply for real, pin the keeper
   run as the new baseline by overwriting `reports/baseline.json`, log
   candidate as champion). Fail → revert, keep the reflection (it prunes the
   next hypothesis). Always report regressions on any layer, not just the
   target metric.
6. **Stop conditions**: budget spent; two consecutive candidates rejected on
   the same cluster (→ the lever is probably wrong — escalate to a code/
   architecture proposal); or the honest message "these route targets are
   inherently confusable — no prompt fixes this" with the evidence.

## Candidate pool
Keep rejected-on-holdout-but-won-somewhere candidates in the pool with their
per-case score vectors (Pareto idea: complementary strengths may merge into a
later candidate). Pool is for generating better candidates — keep/ship
decisions come ONLY from the holdout gate. Holdout looks are counted; after 5,
analyze forces a reseal.

## Human gate
Every kept edit is presented as a diff with its evidence before it is left in
the working tree. Nothing is committed by this skill. Same-family-judge mode
(if accepted in profile) is restated in every kept-edit summary as a known
bias on the numbers.
