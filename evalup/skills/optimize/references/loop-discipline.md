# Optimization Loop Discipline

Why each rule exists — cite these when a user pushes to skip one.

**Never optimize against the scoring set.** Edits fitted to the cases you
score against are memorization, not improvement. Training split proposes;
sealed holdout decides. (GEPA's gains failed to transfer in continual settings
exactly when regression control was absent — arXiv:2607.14004.)

**One candidate, one surface, one cluster.** Multi-edit candidates make credit
assignment impossible: when the score moves you won't know which change did
it, and reverting becomes all-or-nothing.

**Reflection over score-chasing.** The evidence (GEPA beats MIPROv2 by ~10%,
up to +12% on AIME-2025, while using up to 35× fewer rollouts than GRPO) says
diagnosing WHY failures happen in the trajectory beats blind candidate
search. Read traces before writing edits — and with code access, check
whether the cause is even in the prompt (often it's a tool schema).

**Posterior gate, n-aware.** At n=50, only ~15pp effects are detectable with
paired tests; "no keep" without saying that reads as "the
tool doesn't work." Always translate: what effect size is detectable at this
n, and what n the user needs for the effect they care about (with the generate
command to get there) — before the noise pair, not after a refused look:
`stats.py --holdout-power` (optimize step 4; with no holdout yet,
`--holdout-power 0 --cluster-rate R` still gives the n). `stats.py` applies one rule at every n — exact
Bayesian P(improvement) against the stated threshold, with an exact one-sided
sign test alongside — rather than switching methods at some n. That is
deliberate: CLT-based intervals are unreliable at small n, and a gate that
changes shape mid-suite invites picking whichever framing passes. A keep
claims "posterior probability ≥ 0.95"; call it significant only when
`sign_test_significant_at_alpha` is also true.

**Why 0.95, k = 3 and a noise floor.** At the old bar (0.8, holdout at k = 1)
an *unchanged* app with 10–30% flaky cases was kept on 6–20% of holdout looks
— a no-op edit, "confirmed". The bar is now 0.95 (0.8 only as an explicit
flag for an exploratory loop that is not spending the holdout), the holdout
runs at k = 3, and the champion is run on the holdout twice before the first
candidate: a candidate that changed no more cases than those two runs
disagree on is "no evidence" (`stats.py --noise-floor`, a count, not a
judgment). Simulated with `stats.py`'s own functions (holdouts of 20–200
cases, 10–30% of them flaky at the worst case), that keeps a no-op on at most
2.4% of looks (20 000 trials; 2.9% in the test's own 3 000-trial cells);
`stats.py` states 3% per look as its bound, 6% for the bar alone. **Both
are the model's numbers, not guarantees.** The model has independent flaky
cases (as `stats.py` treats every case) and a fixed champion. Six of twenty
cases that flip *together* — one shared dependency — give 12.5% per look and
21.9% over three, by exact enumeration (a pinned test); and after a keep the
noise pair describes the previous champion, so later looks are outside the
bound. Say so wherever the bound is quoted (`stats.py` prints it as
`multi_look_assumptions`).

**Multiple-comparisons honesty.** Every holdout look spends validity (garden
of forking paths). Count looks; force reseal after 5. Re-rolling candidates
until one passes the gate is the failure mode this prevents. The two champion
runs spend two of the five, leaving three candidate looks per seal; every
report states `multi_look_false_keep_bound` (`--candidate-looks`), at most 9%
across those three (18% when the floor did not apply) under the model above.

**Tool descriptions first.** Independent evidence: the full Trace-Free+
curriculum-learning framework — of which better tool descriptions are a
component — produced +60% task success (arXiv:2602.20426), not description
rewrites alone; description defects cost 8.8–11.6pp selection accuracy at
scale (arXiv:2602.18914). Even isolated, still the cheapest lever with the
biggest measured effect and zero architecture risk.

**Judge-gaming awareness.** Optimizing against a judge optimizes for the
judge's blind spots too. Calibration (TPR/TNR vs human labels) before, canary
cases during, spot-check after any large judged-score jump — a jump the
deterministic layers don't corroborate is a red flag, not a win.

**Prompts have a ceiling.** Two failed candidates on the same cluster usually
means the problem is structural (confusable route targets, missing tool, absent
OOS route). The honest output is the finding, not a third prompt tweak.
