---
name: generate
description: >-
  Generate an eval dataset for the profiled app: coverage-gridded cases with
  expected routes/trajectories/rubrics, splits (full plus its smoke and canary
  subsets and a sealed holdout), and a targeted human-review pass. Use when
  the app has a verified profile and needs test cases, or to expand coverage
  for a specific layer or domain.
argument-hint: "[--layer routing|tools|answer] [--count N]"
---

# Generate — Build the Dataset

Arguments, when the user typed any: `$ARGUMENTS`

Precondition: `profile.yaml` exists with verified core entries. If not, stop
and route to discover. Delegate bulk generation to the `test-generator` agent (`evalup:test-generator`
when the plugin is installed);
this skill owns the plan, the review pass, and the splits. Size the suite to
the budget: ~30 cases is a good default, ~12 a legitimate minimum — and ~12 is
what `${CLAUDE_PLUGIN_ROOT}/skills/start/SKILL.md`'s first session asks for,
so a new user sees a result before they are asked to review thirty cases.

Four documents own what they name, and this file never restates their rules:
[case-format.md](references/case-format.md) (schema + `validate_cases.py`'s
error/warning line), [suite-sizing.md](references/suite-sizing.md) (quotas, and
what a suite this size may claim),
[generation-method.md](references/generation-method.md) (the evidence behind
§0–§3), `${CLAUDE_PLUGIN_ROOT}/agents/test-generator.md` (per-case authoring).

## 0. Prefer a different model family than the app under test
Generate cases — and drive user-simulator personas — with a different model
family than the app under test, whenever one is available. Record the
generator's model and family in the dataset metadata either way.

Same-family generation is a documented risk, not a measured effect. So when no
cross-family generator exists, **generate anyway and mitigate** rather than
blocking: human review on every case (§3), expectations grounded in state or an
oracle (§2), the caveat in the metadata. Evidence, and why the judge's
same-family rule is hard where this one is not: generation-method.md.

## 1. Build the coverage grid
The ROW axis is the app's **unit of dispatch** — whatever the app chooses
between before acting. Read it from `architecture.kind` in the profile; it is
not always "domain". Spell the five kinds exactly as
`${CLAUDE_PLUGIN_ROOT}/skills/discover/SKILL.md` §2 spells them, underscores
included, or the branch below matches nothing.

| `architecture.kind` | Rows the grid builds | Expectation / oracle handling |
|---|---|---|
| `single_llm` | out-of-scope + the answer-quality categories | answer layer only; the grid degrades to phrasing/persona variation. No routing or trajectory expectations to author. |
| `tool_agent` | every tool (+ out-of-scope) | trajectory expectations apply (end-state + policy per §2c); no route label to author. |
| `router_executor` | every domain × every tool (+ cross-domain + out-of-scope) | a route label per case *plus* executor expectations, kept separate. Hard-negative route labels are the least reliable — flag every one for review (§3). |
| `multi_agent` | every sub-agent × its tools (+ cross-unit + out-of-scope) | name the sub-agent in `unit:`, but write the expectation trace-wide: **agent-scoped expectations are RESERVED** — tool calls carry no owning agent, so `expect.tools.subset` passes when the WRONG sub-agent made the call, and `forbidden` fails on any agent's. Cross-unit cases are still worth authoring; just read them as whole-app assertions. |
| `workflow` | every node | per-node golden-behavior regression — pin each node's expected output rather than authoring open-ended trajectory coverage. |

The COLUMN axis is the **oracle type** — `test_type`: MFT, INV or DIR, defined
in case-format.md. `category` is the input flavor, and it sub-divides MFT
because those flavors share one oracle. Author each case against
case-format.md's whole schema, not just its grid cell.

The grid maps risk; it is a guide, not a quota. Cover every UNIT at least once,
then weight cells by risk — costly or frequent paths get depth, rare ones get
one case or none. Show the grid with counts before generating so the user can
rebalance, and list uncovered cells in the metadata as known gaps. Report
coverage BY COLUMN too: a suite can cover every unit and still be 40%
happy-path MFT with no INV (`validate_cases.py` warns above 40% in a category).

Quotas — grid depth, `smoke`, `holdout`, `canary`, INV/DIR — are each a **ratio
of the suite size plus a floor**, never a fixed count. Apply them to the size
actually being generated, and note in the metadata when a floor overrode its
ratio. suite-sizing.md holds both those numbers and the caveat on what this
size may claim, which goes in the metadata too.

## 2. Generation rules (pass to test-generator)

### 2a. Two phases, always — tuples first, prose second
Never let the generator emit finished case prose straight from a grid cell.
**Phase 1** names a template's dimensions (e.g. `filter`, `persona`,
`register`, `intent`), hand-writes the first ~20 tuples, then expands the tuple
SET — still symbolic, into `templates/<template_id>.yaml`. **Phase 2** realizes
one tuple at a time as a natural-language message, **in a separate prompt**.
Each realized case then carries `template_id` + `instantiation_params`, its
tuple; a one-off writes `template_id: null` and never omits the key. The split
is load-bearing because diversity is only checkable in the tuple space
(generation-method.md, with the two expansion strategies).

### 2b. Filter before review — assume half the goldens are junk
Filter mechanically BEFORE the human pass in §3; those 15 minutes must not go
on obvious junk. Drop a case, logging the reason in the template's `rejected:`
list, when it is:

1. **not self-contained** — score it, retry a fixed few times, then drop it;
2. **not grounded** — no oracle for the expectation, so none gets invented:
   drop the case or demote it to `pending`;
3. **a near-duplicate** — ROUGE-L ≥ 0.7 against any existing case;
4. **an incoherent cell** — cross products generate nonsense combinations.

The damage concentrates in the EXPECTED OUTPUT, not the input — see
generation-method.md.

### 2c. What to add to the agent's own rules
test-generator.md carries crossed dimensions, honest route labels, trajectory
defaults (end-state + policy, never an invented tool order), INV/DIR
derivation, the enabled-layer rule and adversarial refusals. case-format.md
carries the per-layer rules: `expect.result` with its `reference_query`,
`expect.authz` derived under the persona's real access scope, and which fields
are RESERVED (`excluded_tools` among them — documentation, not a measurement).
This skill adds two. **Real seeds first**: seed from real messages or traces
where they exist, and record which cases are synthetic-only — a regression
scaffold, not a measure of real-world quality. And INV/DIR cases are cheap but
still spend suite slots, so hold them to §1's ratio and floor.

## 3. Targeted review — 15 minutes, not 100 cases
Never ask the user to review everything; they won't, and silent label errors
become a permanent noise floor. Send only three kinds: hard-negative and OOS
labels (quarantined from scored runs until reviewed), cases the generator
marked uncertain, and cases a cheap probe run shows the app disagreeing with.
Present each as the message, the expected label, and a one-line why — accept /
fix / delete — and write the review status per case.

**Only a human may set `review.status: accepted`, and `review.by` must be that
human's name** — not "generate-review", not the agent, not the skill.
Everything the generator produces starts `pending`. **Never derive an
expectation by reading the app's source**, and prefer probing the app BEFORE
labeling. generation-method.md says why each of these three matters.

## 4. Splits
The four legal values of `split` are exactly `full`, `smoke`, `holdout`,
`canary`. `smoke` and `canary` are SUBSETS of `full`, so a case carries both.
`holdout` is mutually exclusive with `full` — a case is sealed or it is in the
training pool, never both, and that exclusivity is what the seal rests on.

- `full`: everything reviewed.
- `smoke` (~1/3 of full, floor 5): diversity-selected to maximize spread across
  grid cells and phrasings, never random. Used by everyday runs and hooks.
- `holdout` (20% at ≥25 cases; below that skip it and say so): SEALED. Written
  once, listed only by id+hash in reports, never displayed case-by-case.
  Unsealing requires `analyze --unseal`, which logs itself and warns that a
  reseal is due after N=5 looks. Optimization never trains on it.
- `canary` (~10% of full, floor 2): a small fixed subset with stable, versioned
  answers, reused across runs instead of regenerated with smoke/full. Movement
  there signals an app/model/judge change, not a traffic shift. **A canary MUST
  assert content** — `expect.result` or `expect.answer` (suite-sizing.md).

Splits are a **field on the case** (`split: [full, smoke]`), not a directory
copy, and case ids are **opaque and stable** — `c-<hash8>`, encoding neither
unit nor category. Content hash per case; `dataset_version` bumps on any change
because baselines pin it. case-format.md has both rules and what each prevents.

### Validate before handing the dataset over
First `${CLAUDE_PLUGIN_ROOT}/scripts/convert_suite.py <state-dir> --split-dir
<tmp>` — it writes those three JSON files from the YAML, and refuses a
duplicate YAML key or one id in two files; never transcribe a suite by hand.
Then run `${CLAUDE_PLUGIN_ROOT}/scripts/validate_cases.py --cases
<tmp>/suite.json --capabilities <tmp>/capabilities.json --manifest
<tmp>/manifest.json`. It fails the generate step on structural errors
and warns on the vacuity patterns; case-format.md has the field rules and the
error/warning line. `--capabilities` is required — pass `--no-capabilities`
only with genuinely no profile, and expect `capabilities_unchecked`.
`--manifest` catches a stale `dataset.yaml`, so re-run it after ANY later
hand-edit to a case file.

## 5. Suite adequacy (periodic, not per-generate)
Offer mutation testing of the suite: inject 3–5 deliberate bugs into a COPY of
a prompt (drop a constraint, invert a condition, swap a tool name), run the
suite against each, report mutation score (% caught). <80% → the suite has
holes where the mutations lived; generate targeted cases there.
