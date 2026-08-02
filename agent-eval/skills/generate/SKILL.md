---
name: generate
description: >-
  Generate an eval dataset for the profiled app: coverage-gridded cases with
  expected routes/trajectories/rubrics, splits (smoke/full/holdout), and a
  targeted human-review pass. Use when the app has a verified profile and needs
  test cases, or to expand coverage for a specific layer or domain.
argument-hint: "[--layer routing|tools|answer] [--count N]"
---

# Generate — Build the Dataset

Precondition: `profile.yaml` exists with verified core entries. If not, stop
and route to discover. Case format: @references/case-format.md.
Delegate bulk generation to the `test-generator` agent; this skill owns the
plan, the review pass, and the splits.
Size the suite to the budget: ~30 cases is a good default, ~12 is a legitimate
minimum — the split ratios below scale down with it.

## 0. Hard rule: generate with a different model family than the app under test
Never generate cases (or drive user-simulator personas) with the same model
family that powers the app under test — if the app runs on Claude, generate
with a non-Claude model/script; if it runs on GPT/Gemini/etc., Claude
generating is fine. This is a HARD RULE, not a style preference: same-family
generation inflates scores — the generating model prefers the phrasing,
structure, and reasoning patterns its own family produces, so the resulting
cases skew toward what the app-under-test's family already does well
("home-field advantage" / preference leakage, EVAL-DESIGN-RECOMMENDATION.md
§2). If the `test-generator` agent would run in-session as a Claude subagent
and the app under test is Claude-based, route generation through an external
non-Claude model or script instead — a same-family subagent cannot dodge this
constraint from inside the session (mirrors the cross-family-judge rule: it
must be an external script, never a subagent). Record the generator's model +
family in the dataset metadata so a reviewer can spot a violation later.

## 1. Build the coverage grid
The grid's ROW axis is the app's **unit of dispatch** — whatever the app
chooses between before acting — read from `architecture.kind` in the profile.
It is NOT always "domain"; domain is only the router-executor case:

- `single-llm` (no dispatch step): rows = every tool (+ out-of-scope). The
  routing layer does not apply and is not scored.
- `router-executor`: rows = every domain × every tool (+ cross-domain
  + out-of-scope).
- `graph` / `orchestrator`: rows = every node / sub-agent × its tools
  (+ cross-unit + out-of-scope).
- No tools either (pure chat/generation): rows = out-of-scope + the
  answer-quality categories alone; the grid degrades to phrasing/persona
  variation over the answer layer.

Columns (categories): `happy`, `multistep`, `edge`, `ambiguous`, `oos`,
`adversarial-refusal`, `noise`. The grid maps risk; it is not a quota. Cover
every UNIT (domain/node/sub-agent — or, for single-llm, every tool) at least
once; within a unit, prioritize cells by risk (frequent/costly paths get
depth; rare/harmless get one case or none). List uncovered cells in the
dataset metadata as known gaps. Show the grid with counts before generating —
the user can rebalance.

Every generated case is authored against the **canonical benchmark-derived
schema** (case-format.md, EVAL-DESIGN-RECOMMENDATION.md §19), not just the
grid cell: set `difficulty` (hidden from the agent — stratified reporting
only, never copied into the prompt); set `template_id` + `instantiation_params`
when a case is one instance of a repeatable template, so variant-consistency
can be scored later; set `excluded_tools` when the app's tool catalog can be
scoped per case, to exercise relevance/irrelevance detection; and decide
`no_op_expectation` for every case (does a do-nothing agent correctly fail
it — a case that doesn't is usually vacuous, reconsider it).

### Budget geometry — ratios with a floor, never absolutes
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
- **Smoke:** ~1/3 of the full set, floor 5 (see §4).
- **Holdout:** 20% of the full set, floor: skip entirely below 25 cases
  rather than seal a token 1–2 cases (see §4).
- **Canary quota** (a small fixed-answer subset carried across runs to detect
  drift — EVAL-DESIGN-RECOMMENDATION.md §21 — distinct from the judge's own
  calibration set): ~10% of the full set, floor 2 once any data-Q&A layer
  exists, even in a 12-case suite.
- **Metamorphic pairs:** at most ~25% of the full set are perturbation
  variants; floor: the single highest-risk accepted case always gets at
  least one pair even at the smallest budget.
At the ~30-case default these read naturally (10 smoke, 6 holdout, 3 canary,
≤7 metamorphic); at the 12-case minimum every ratio still resolves to a small
positive integer via its floor instead of erroring out or silently vanishing.

## 2. Generation rules (pass to test-generator)
- Vary over orthogonal dimensions (intent × persona × phrasing × language if
  applicable) — never "generate N examples" flat, it produces homogeneous data.
- Derive perturbed variants of accepted cases: typos, slang, irrelevant
  detail, paraphrase — as **metamorphic pairs** (expectation: same route/
  behavior as the parent case, `metamorphic_parent` set). No new labeling
  needed, but each variant still spends a suite slot — keep to the metamorphic
  ratio+floor in §1 (at small budgets, only the highest-risk pairs).
- Router hard-negatives: queries that look like domain A but belong to B or to
  nobody. These labels are the least reliable — flag every one for review.
- Expected trajectories: DEFAULT to end-state/answer + policy assertions.
  Exact tool sequences ONLY where a human states one. Multiple valid paths are
  the norm (subset matching accommodates extras), and inferring the expected
  sequence from the app's own code grades the app against itself — forbidden.
- **Data-Q&A cases → emit `expect.result`.** Compute the reference query
  against the seeded fixture / read-only oracle and store it as
  `reference_query` (provenance only) plus the resulting `scalar`/`rows`.
  Never hand-type the expected number — the runner re-derives `expected`
  offline from the reference query via the oracle at run time (see
  case-format.md), so a fixture change never leaves a stale hand-typed answer
  behind. If the runner can't extract `actual` from the trace, that's
  `unscorable`, never a manufactured fail.
- **Permission/scope cases → emit `expect.authz`.** For every case built from
  the (question × persona) matrix, set `allowed_record_ids` /
  `forbidden_record_ids` / `forbidden_tools` / `expect_refusal`
  (case-format.md), derived by running the SAME oracle query under that
  persona's real access scope (RLS `SET ROLE` / equivalent) — never by
  hand-filtering the full-access answer. This is scored deterministically by
  `score_authz.py` against the trace's tool-call log and returned record IDs,
  not an LLM read of the chat text, so a politely-worded refusal over an open
  endpoint underneath still fails.
- Tool-catalog sampling: where the app profile supports scoping the exposed
  catalog per case, leave 1+ present-but-irrelevant tools in and record them
  as `excluded_tools` — the relevance/irrelevance test (does the agent avoid
  reaching for a plausible-but-wrong tool).
- Real seeds first: if any real user messages/traces exist, seed from them and
  say in the dataset metadata which cases are synthetic-only. Record the
  standing caveat: synthetic-only datasets are a regression scaffold, not a
  measure of real-world quality.

## 3. Targeted review — 15 minutes, not 100 cases
Never ask the user to review everything; they won't, and silent label errors
become a permanent noise floor. Select for review only:
- all hard-negative/OOS labels (quarantined from scored runs until reviewed),
- cases the generator marked uncertain,
- cases where a quick probe run shows the app disagreeing with the label
  (disagreement sampling — run the smoke-sized probe if cheap).
Present each as: the message, the expected label, one-line why — accept /
fix / delete. Write review status per case.

## 4. Splits
- `smoke/` (about a third of the full set, min 5 — see the budget-geometry
  ratio+floor in §1): diversity-selected (maximize spread across grid cells
  and phrasings), not random — small sets must earn their representativeness.
  Used by the everyday/smoke runs and hooks.
- `full/`: everything reviewed.
- `holdout/` (20% when the set is ≥25 cases; below that, skip the holdout and
  say so — optimize stays locked at that size anyway): SEALED. Written once,
  listed only by id+hash in reports,
  never displayed case-by-case. Unsealing requires `analyze --unseal`, which
  logs itself and warns that a reseal (swap with fresh cases) is due after
  N=5 looks. Optimization never trains on it.
- `canary/` (~10% of the full set, floor 2 — see §1): a small fixed subset
  with stable, versioned expected answers, reused across runs rather than
  regenerated each time smoke/full are rebuilt — movement on these fixed
  inputs signals an app/model/judge change, not a traffic-distribution shift.
Stable case ids (`<unit>-<category>-<hash8>`, where `unit` is the route
target — domain/node/sub-agent — or, for a single-LLM app, the primary tool
exercised, falling back to `app` when there is neither); content hash per case;
`dataset_version` bumped on any change (baselines pin it).

## 5. Suite adequacy (periodic, not per-generate)
Offer mutation testing of the suite: inject 3–5 deliberate bugs into a COPY of
a prompt (drop a constraint, invert a condition, swap a tool name), run the
suite against each, report mutation score (% caught). <80% → the suite has
holes where the mutations lived; generate targeted cases there.
