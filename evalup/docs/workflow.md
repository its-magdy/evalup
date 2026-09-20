# The evalup Workflow

## Who does what

Three roles. On a small team one person wears all hats — the roles still
matter because they fail differently when skipped.

| Role | Owns | Time |
|---|---|---|
| **Developer** | Adapter & CI wiring, running commands, fixing failures, reviewing optimize proposals | most of it |
| **Reviewer (QA)** | Generated-case review, exploratory testing (findings become cases), release-run sign-off, red-team suite | hours/week |
| **Domain arbiter** | The final word on answer quality: labels judge-calibration cases, approves rubrics, states business rules | ~1–2 h/week |

The domain arbiter is the role teams skip — and the reason judge numbers stay
watermarked PROVISIONAL until the labeling happens. QA and the arbiter never
need Claude Code: datasets are YAML, reports are HTML/Markdown.

## Day 1 — first contact (≈1 hour to first insight)

Before step 1, if you want to see the shape of what each step writes:
`${CLAUDE_PLUGIN_ROOT}/examples/quickstart/` holds a complete, validated
example of all of it — one profile, one adapter, six cases, the plan, and the
run's file list — for a small trace-less app. It is worth ten minutes.

1. `/evalup:start <app>` → runs **discover**: profiles the app, patches
   missing instrumentation (GenAI spans, content capture, trace-ID echo,
   invocation shim — each with your approval), classifies tool side effects,
   interviews you for what code can't say (route-target boundaries for router/
   multi-agent apps, never-rules),
   and writes `findings.md` — design gaps like "no out-of-scope route."
2. Note the **stage** it assigned. `pre-stability` is normal for an app under
   active development: you get crash rate, format, loops, refusals — and an
   explicit list of what unlocks when boundaries stabilize.
3. `/evalup:generate` → ~30 cases. Review the ~15 it flags (15 minutes).
4. `/evalup:run --baseline` → first scores. The routing confusion matrix
   is usually the first "I didn't know that" moment.

## Steady state — the loop you actually live in

- Edit a prompt → `run --smoke` (sub-dollar, minutes; optionally wired to a
  debounced PostToolUse hook — see
  `${CLAUDE_PLUGIN_ROOT}/docs/hooks-example.json`, and create the hook's target
  from `${CLAUDE_PLUGIN_ROOT}/docs/smoke.sh.example` first; it is not created
  automatically).
- Before merging: `run` (full) → baseline diff with significance verdicts —
  "improved / worsened / within noise at this n," never a bare percentage.
- Weekly-ish: `analyze --cluster` on failures; fix the top cluster or feed it
  to `optimize`. Add cases from anything exploratory testing finds.
- When judge dimensions matter: `analyze --label` — 15-minute sessions,
  repeated: ~30 labels to discover the criteria, then a stratified 100–200 to
  measure them, until ≥90% agreement (TPR **and** TNR, plus Cohen's κ — never
  raw accuracy). Rubric edits mid-labeling are expected, not failure.

## After a refactor (route targets/tools changed)

`/evalup:discover --diff` — diffs the new profile against the old, lists
which cases went stale, bulk-proposes migrations. Do this BEFORE running, or
a red wall of stale expectations will waste your morning.

## When scores plateau — optimize

`/evalup:optimize` (unlocks with: calibrated judge if judged objectives,
~100+ cases, stable stage). One cluster → one evidence-grounded edit →
training split → sealed holdout gate → keep or revert. Tool descriptions are
the advertised first lever: cheapest change, empirically the largest measured
effect. Proposals that need code or architecture changes arrive effort-tagged
for your backlog instead of being silently attempted.

## Production era (stage: traffic → production)

- `analyze --mine` promotes real conversations (redacted if PII) into the
  dataset — synthetic cases progressively retire. This is mandatory hygiene:
  synthetic-only suites drift from reality.
- Release habit worth copying from industry: a launch is validated by a
  monitoring window on real traffic (days–weeks), not by the merge-time run.
- CI: run the deterministic layers on PRs via a plain script (no Claude
  needed); judged layers nightly; gate on absolute floor + statistical delta
  vs the rolling baseline.

## Failure modes this workflow prevents (why the rules exist)

| If you skip… | What silently breaks |
|---|---|
| Reviewing flagged cases | Wrong labels become a permanent ~10-point noise floor |
| Judge labeling | Optimizer optimizes toward an unvalidated judge's biases |
| discover --diff after refactor | Baseline diffs scream regression when the app actually improved |
| The holdout seal | "Improvements" are memorized answers to the test |
| Real-trace mining | The suite measures an imaginary user population |
