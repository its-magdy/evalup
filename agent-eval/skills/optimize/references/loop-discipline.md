# Optimization Loop Discipline

Why each rule exists — cite these when a user pushes to skip one.

**Never optimize against the scoring set.** Edits fitted to the cases you
score against are memorization, not improvement. Training split proposes;
sealed holdout decides. (GEPA's gains failed to transfer in continual settings
exactly when regression control was absent — arXiv:2607.14004.)

**One candidate, one surface, one cluster.** Multi-edit candidates make credit
assignment impossible: when the score moves you won't know which change did
it, and reverting becomes all-or-nothing.

**Reflection over score-chasing.** The evidence (GEPA vs GRPO, 35× fewer
rollouts) says diagnosing WHY failures happen in the trajectory beats blind
candidate search. Read traces before writing edits — and with code access,
check whether the cause is even in the prompt (often it's a tool schema).

**Statistical gate, n-aware.** At n=50, only ~15pp effects are detectable with
paired tests; "no significance → no keep" without saying that reads as "the
tool doesn't work." Always translate: what effect size is detectable at this
n, and what n the user needs for the effect they care about (with the generate
command to get there). Below n≈100 use Bayesian P(improvement) with the
threshold stated; CLT-based intervals are unreliable there.

**Multiple-comparisons honesty.** Every holdout look spends validity (garden
of forking paths). Count looks; force reseal after 5. Re-rolling candidates
until one passes the gate is the failure mode this prevents.

**Tool descriptions first.** Independent evidence: description rewrites alone
produced +60% task success (arXiv:2602.20426); description defects cost
9–12pp selection accuracy at scale (arXiv:2602.18914). Cheapest lever, biggest
measured effect, zero architecture risk.

**Judge-gaming awareness.** Optimizing against a judge optimizes for the
judge's blind spots too. Calibration (TPR/TNR vs human labels) before, canary
cases during, spot-check after any large judged-score jump — a jump the
deterministic layers don't corroborate is a red flag, not a win.

**Prompts have a ceiling.** Two failed candidates on the same cluster usually
means the problem is structural (confusable route targets, missing tool, absent
OOS route). The honest output is the finding, not a third prompt tweak.
