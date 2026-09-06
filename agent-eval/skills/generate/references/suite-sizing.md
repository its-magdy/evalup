# Suite sizing — budget geometry and what a suite this size can claim

Read this when setting or defending the suite's quotas (grid depth, smoke,
holdout, canary, metamorphic), or when writing the statistical caveats into
the dataset metadata. SKILL.md §1 carries the operational table; this file
carries the reasoning behind it and the statistics that bound what the
resulting pass rates mean.

## Budget geometry — ratios with a floor, never absolutes
Every quota in this skill (grid depth, smoke, holdout, canary, metamorphic) is
a **ratio of the current suite size plus a floor**, not a fixed count — a
fixed absolute (e.g. "always add 5 canaries") silently dominates or breaks a
12-case suite. Compute against the actual suite size being generated, apply
the floor, and note in the dataset metadata whenever the floor overrode the
ratio:
- **Grid depth per cell:** baseline 1 case/covered unit; risk-weighted cells
  scale to roughly 2x the baseline, never a fixed "+3" — at the 12-case
  minimum this collapses to 1 case/unit with no depth, which is correct
  (there isn't budget for depth yet).
- **Smoke:** ~1/3 of the full set, floor 5 (see SKILL.md §4).
- **Holdout:** 20% of the full set, floor: skip entirely below 25 cases
  rather than seal a token 1–2 cases (see SKILL.md §4).
- **Canary quota** (a small fixed-answer subset carried across runs to detect
  drift — distinct from the judge's own
  calibration set): ~10% of the full set, floor 2 once any data-Q&A layer
  exists, even in a 12-case suite.
- **INV/DIR (metamorphic) cases:** at most ~25% of the full set are
  perturbation variants; floor: **every template gets at least one INV**, and
  at the smallest budget the single highest-risk case still gets one. Treat
  this floor as the binding constraint rather than the ceiling — INV inherits
  its parent's expectation, so it is the cheapest cell in the grid, and a
  suite with 6% INV coverage has left the cheapest coverage unbought.
At the ~30-case default these read naturally (10 smoke, 6 holdout, 3 canary,
≤7 metamorphic); at the 12-case minimum every ratio still resolves to a small
positive integer via its floor instead of erroring out or silently vanishing.

## What a suite this size can and cannot claim
The ratios above are precise; the number they are dividing is not. Say this
out loud in the dataset metadata rather than letting a 30-case pass rate get
quoted as if it were a measurement. Pass rates are binomial —
SE = √(p(1−p)/n) — so at p≈0.8:

| n | 95% CI half-width |
|----|----|
| 12 | ±22.6 pp |
| 30 | ±14.3 pp |
| 100 | ±7.8 pp |

Minimum detectable effect at 80% power, α=0.05, n≈30: **~28 pp** unpaired,
**~16–28 pp** paired (McNemar, depending on the discordance rate). So:

- **Legitimate** at this size: paired before/after comparison on a FIXED suite,
  catching hard regressions on known-failure cases, and large early-stage
  effects (which is exactly why Anthropic's "20-50 tasks drawn from real
  failures is a great start" is sound advice — early changes have large effect
  sizes).
- **Not legitimate**: quoting an absolute pass rate as a number, or claiming
  any delta under ~15-20 pp.

Two refinements worth honoring rather than hand-waving:
- **Use the right interval.** At n=30, p=0.8, n(1−p)=6 sits where the normal
  approximation degrades. Report Wilson or Clopper-Pearson, not Wald.
- **Pairing helps but does not rescue.** McNemar's chi-square variant wants
  ≥25 DISCORDANT pairs; a 30-case suite will rarely have that, so use the
  exact binomial variant.
- **Grid-generated cases are CLUSTERED data.** Cases sharing a template are not
  independent, so effective n is below nominal n. Anthropic's "Adding Error
  Bars to Evals" (Miller, 2024) recommends clustered standard errors here and
  reports they can be over three times the naive ones. `template_id` is what
  makes the clustering computable — one more reason it is required in SKILL.md §2a.

