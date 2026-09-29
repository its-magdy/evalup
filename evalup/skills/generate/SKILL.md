---
name: generate
description: >-
  Generate an eval dataset for the profiled app: coverage-gridded cases with
  expected routes/trajectories/rubrics, splits (full plus its smoke and canary
  subsets and a sealed holdout), and a targeted human-review pass. Use when
  the app has a verified profile and needs test cases, or to expand coverage
  for a specific layer or domain.
argument-hint: "[--layer routing|tools|answer] [--count N]"
allowed-tools: >-
  Read Grep Glob Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/*) Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*)
---

# Generate — Build the Dataset

> **Plugin root:** `${CLAUDE_PLUGIN_ROOT}`. Reference files and docs write that
> placeholder literally (it is only substituted here), so read every
> `${CLAUDE_PLUGIN_ROOT}/…` path you meet in them as this absolute path, and
> quote it in shell commands.
>
> **Calling the plugin.** Open its files with Read — never `cd` into the
> plugin, and never `ls`, `grep` or `cat` it from the shell: the plugin is not
> a working directory, so those prompt, and headless a prompt is a denial.
> Run each script as its own Bash call, spelled
> `python3 "<that path>/scripts/<name>.py" …` with the path written out: no
> `cd`, no `&&` or `; echo $?` tail, no shell variable holding the path. The pre-approval matches
> that literal form only, and it lapses when the user next replies — a prompt
> after that is expected, not a fault. If a call is **denied**, stop and tell
> the user which permission is missing; never work around it by hand.
>
> **Every shell call, not only script calls.** One plain command per Bash
> call — no `cd`, `&&`, `|`, heredoc, `$( )` or `/tmp` — so an allow rule can
> match it. A denial of **any** call is a stop, never a retry in another form.

Arguments, when the user typed any: `$ARGUMENTS`

Precondition: `profile.yaml` exists with its core entries verified
(`architecture.kind`, the invocation shape, one route target or tool seen
live — discover §4 defines the set). If not, stop and route to discover; route
targets still `verified: false` are inferred, which is normal after a lean
first session and no reason to bounce. Delegate bulk generation to the `test-generator` agent (`evalup:test-generator`
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

**Design findings are the grid's third input.** Read `findings.md` from the
state location (discover writes it beside the profile). Every numbered design
finding with an observable symptom — an answer the app got wrong live, an
inverted flag, a date window read backwards — gets at least one case whose
expectation fails on that symptom: it is the one failure the suite is already
known to be able to catch, and a grid that covers every unit can still miss
it. Pass those findings to the generator with the grid assignment, and list
the findings left uncovered, by number, under `known_gaps` in `dataset.yaml`.

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
This skill adds three. **Real seeds first**: seed from real messages or traces
where they exist, and record which cases are synthetic-only — a regression
scaffold, not a measure of real-world quality. INV/DIR cases are cheap but
still spend suite slots, so hold them to §1's ratio and floor. And **read the
adapter's `environment.safe_to_attack` before authoring
`adversarial-refusal` cases**: while it is not true, every run skips them
(runner-contract §7) and the validator warns `attack_category_will_skip`.
Author them when findings name an attack surface and the owner has not been
asked — they document the defect — but tell the user, where the suite is
handed over, that they will not run until the owner sets the flag, and ask
the owner if one is present. When the owner has said no, leave them out and
record the decision in findings.md: `--strict` promotes the warning to an
error, so a CI job validating with `--strict --adapter` would fail on them
for good.
Never relabel such a case as `edge` or `oos` to get it past the gate; the flag
is the owner's call, not the suite's.

## 3. Targeted review — 15 minutes, not 100 cases
Never ask the user to review everything; they won't, and silent label errors
become a permanent noise floor. Send only three kinds: hard-negative and OOS
labels (`review.status: quarantined` with `gating: false` — the case still
runs and is reported, but cannot close the gate until a human accepts it and
flips `gating`; no script drops it for you), cases the generator
marked uncertain, and cases a cheap probe run shows the app disagreeing with.
Present each as the message, the expected label, and a one-line why — accept /
fix / delete — and write the review status per case.

**Only a human may set `review.status: accepted`, and `review.by` must be that
human's name** — not "generate-review", not the agent, not the skill.
Everything the generator produces starts `pending`. **Never derive an
expectation by reading the app's source**, and prefer probing the app BEFORE
labeling. generation-method.md says why each of these three matters.

**The generator emits `gating: false` on every case** (its brief, rule 3):
an unreviewed label must not close a gate, which is what
`validate_cases.py`'s `gating_unreviewed` warns about per case. A reviewer
who accepts a case flips `gating` to true in the same edit. The 2026-09-26
user test saw the agent write `gating: true` on 6 of 12 cases under an older
brief, so check its output before validating anyway: among the cases this
delegation just wrote — never a case a human already accepted — find every
one whose `gating` is not `false` (an absent key means true), set it to
`false`, and list their ids in chat. The validator's per-case warning is the
backstop, not the instruction.

Only the cases a human accepted gate, so the handover always says how many
do. **No reviewer in the session** (headless, or the user said not to ask):
every case stays `pending` and `gating: false`. Say what that means, as the
last line of the handover and under a free-text `review:` key in
`dataset.yaml`: "N cases, 0 accepted: this suite cannot fail a build until a
human accepts cases and flips `gating`." The validator states the same fact at
suite level (`nothing_gates`); never edit a warning away without naming, case
by case, what changed and why.

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
  there signals an app/model/judge change, not a traffic shift. A canary
  asserts content — `expect.result` or `expect.answer` — because drift shows
  up in the answer and `http: 200` comes back either way (suite-sizing.md,
  which also says when the floor applies). **Every canary also carries
  `smoke`**: a run selects on its mode's split alone, so a canary tagged only
  `[full, canary]` never runs under `--smoke` (the validator warns,
  `canary_not_in_smoke`). Canaries sit outside every denominator; what the
  tag adds to a smoke run is the call, and the abort (exit 4) when the canary
  scores wrong — which is the point: a drifted harness stops the run before
  it spends the suite producing numbers nobody should read.

Splits are a **field on the case** (`split: [full, smoke]`), not a directory
copy, and case ids are **opaque and stable** — `c-<hash8>`, encoding neither
unit nor category. Content hash per case; `dataset_version` bumps on any change
because baselines pin it. case-format.md has both rules and what each prevents.

### Validate before handing the dataset over
First `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/convert_suite.py <state-dir> --split-dir
<work-dir>` — it writes the JSON inputs from the YAML (`suite.json`,
`capabilities.json`, `adapter.json`, and `manifest.json` only when
`datasets/dataset.yaml` exists), and refuses a duplicate YAML key or one id in
two files; never transcribe a suite by hand. Then run
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/validate_cases.py --cases <work-dir>/suite.json
--capabilities <work-dir>/capabilities.json --adapter <work-dir>/adapter.json
[--manifest <work-dir>/manifest.json]` — pass `--manifest` only when that file was
written, since a missing path is exit 2. It fails the generate step on
structural errors and warns on the vacuity patterns; case-format.md has the
field rules and the error/warning line. `--adapter` is what catches a label
the runner could never observe: `clarify_ok` with no `clarify_from_response`
(`clarify_unobservable`), or a domain `expect.route` on a trace-less app whose
adapter maps the HTTP status (`route_not_observable`) — there the observed
route is the map's value, `<answered>` or `__oos__`, so expect that label and
keep the domain in `unit`. It also warns `attack_category_will_skip` on an
`adversarial-refusal` case while `environment.safe_to_attack` is not true. `--capabilities` is required — pass `--no-capabilities`
only with genuinely no profile, and expect `capabilities_unchecked`.
`--manifest` catches a stale `dataset.yaml`, so re-run it after ANY later
hand-edit to a case file.

`datasets/dataset.yaml` is the suite's hand-written summary, and it is
optional (absent → `dataset_version` 1, nothing cross-checked). Every key is
optional too; the ones the scripts read are exactly these, and each count is
checked against the case files:

```yaml
dataset_version: 1          # int; make_plan.py pins it into the run manifest
cases: 12                   # total case files
splits: { full: 12, smoke: 5 }
coverage_grid:
  by_unit:
    billing: { total: 4, MFT: 3, INV: 1 }   # `total`, then test_type values
```

## 5. Suite adequacy (periodic, not per-generate)
Offer mutation testing of the suite: inject 3–5 deliberate bugs into a COPY of
a prompt (drop a constraint, invert a condition, swap a tool name), run the
suite against each, report mutation score (% caught). <80% → the suite has
holes where the mutations lived; generate targeted cases there.
