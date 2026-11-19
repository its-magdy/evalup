# Quickstart — a conforming end-to-end example

This is the plugin's worked example: a complete, current, **validated** eval
setup for a small two-domain chat app, plus the run it produces. Read it before
you point the harness at anything of your own.

Everything here conforms to the rules the skills enforce *today*. That claim is
not a promise in prose — `tests/test_example.py` lints this directory with
`validate_cases.py --strict` and gets **zero errors and zero warnings**, then
runs it for real against a live HTTP server and re-checks the output with the
runner's own completeness check. If a rule changes and this example stops
conforming, that test fails.

---

## The app

`helpdesk-demo` answers questions about **invoices** and **shifts**, refuses
anything else with HTTP 400, and mutates nothing. It is a rule-based fixture
living in `tests/test_example.py` — deterministic, so the example never fails
for reasons that have nothing to do with the harness.

It is **trace-less**: no collector, no queryable span store. That is the most
common position a real app starts from, and it is the honest setting for an
example, because it forces the question every profile has to answer — *which
layers can this app actually support?*

## What is here

| File | What it is | Who writes it |
|---|---|---|
| `.evalup/profile.yaml` | the app's capabilities and business rules | `discover` |
| `.evalup/adapter.yaml` | the only thing the harness knows about the app | `discover` |
| `.evalup/datasets/c-*.yaml` | six cases, one file each | `generate` |
| `converted.json` | those three, as the JSON the scripts read | `run`, mechanically |
| `plan.template.json` | the plan the runner executes | `run` §1 |
| `expected-run-tree.txt` | every file the run writes | `run_cases.py` |

`converted.json` exists because **`validate_cases.py` and `run_cases.py` take
JSON, not YAML** — deliberately: the skill's YAML→JSON conversion *is* the
run's parse, not a second opinion. The YAML stays the single source of truth.
Regenerate the JSON after editing any YAML here:

```bash
python3 scripts/convert_suite.py examples/quickstart/.evalup > /tmp/c.json   # from evalup/
```

and copy its `adapter`, `capability_matrix` and `cases` into `converted.json`
(the committed file also carries a `//` comment block the script does not
write). `tests/test_gate_and_convert.py` asserts the script reproduces the
committed file exactly, and `TestConversionIsFaithful` checks the file against
the YAML, so forgetting fails wherever PyYAML is installed; on a bare
interpreter the stdlib pairing guard still catches a case added to one side and
not the other. `convert_suite.py` is the **only** script that imports PyYAML —
the runner and scorers never do.

## Run it

```bash
python3 examples/quickstart/demo.py            # from evalup/ — six cases, a verdict each, the gate line
python3 examples/quickstart/demo.py --break    # the same run with one wrong expectation: exit 1
python3 examples/quickstart/demo.py --keep     # leave the run directory + viewer.html to open
python3 -m unittest tests.test_example         # the same example, as the suite checks it
```

`demo.py` starts the demo app on a local port, builds the plan from
`plan.template.json` + `converted.json`, runs `run_cases.py`, then `gate.py`
and the review viewer — the pipeline `/evalup:run` drives, with nothing else.
It writes to a temp directory: the run directory is still **not** committed —
see "Why nothing here is a stored run" below.

---

## What the six cases cover

Six cases, `split: [full, smoke]`, `k: 1`, mode `smoke`, soft gate.

| Case | Category | Shows |
|---|---|---|
| `c-4f2a91c7` | happy | four layers scoring at once: http, routing, execution, answer |
| `c-8b13d0e5` | happy | a second route, and the parent of the INV case below |
| `c-2d7c6a11` | edge (INV) | a metamorphic pair: the phrasing moves, the expectation must not |
| `c-3a5e47d9` | edge | a zero that is real, graded as **data** rather than as prose |
| `c-9e05b3f4` | oos | a deliberate HTTP 400, and `__oos__` routing |
| `c-6c1f82ab` | adversarial-refusal | a read-only app refusing a mutation |

Ids are `c-<hash8>` and **opaque**: they encode neither unit nor category, both
of which are mutable classifications. Splits are a **field**, never a
directory. Both rules exist because the field-test suite broke them, and both
are checked here by a test — the id rule has no validator code, so this
example is where it is enforced.

### Four layers score; the rest say why they cannot

On a trace-less app the layer table still produces real verdicts:

- **`http`** — the one layer with no script; the runner compares the status.
  `c-9e05b3f4` declares `status: 400`, which is how a deliberate refusal passes
  instead of looking like a failure.
- **`routing`** — from `adapter.invocation.route_from_response: "domain"`. A
  *declared* dotted path, never a guess. Without that one line, routing would
  be `unscored` on every case.
- **`execution`** — from `result_from_response: "data.value"`. This grades the
  **data**, which is what catches a stub returning nothing while the assistant
  confidently answers "0".
- **`answer`** — `score_answer.py`, string and regex assertions. No judge, no
  rubric, no calibration needed.

The other layers are off, each with a `blocked_by` in `profile.yaml` naming the
concrete missing thing. The runner copies that string into the report rather
than composing its own, so the matrix and the report can never disagree about
why. Note the three-way distinction, which the example shows on purpose:

- **`n/a`** — the case does not assert this layer.
- **`unscorable`** — it asserts it, and somebody turned the layer off *and said
  why*.
- **`unscored`** — asserted and enabled, but the input could not be produced.

None of the three is ever `pass` and none is ever `fail`. That is the whole
discipline: an honest gap, never a number that reads as a measurement.

### Two declarations that are easy to miss

`environment.safe_to_attack: true` in `adapter.yaml` is why `c-6c1f82ab` runs
at all. Without it the runner **skips** every `adversarial-refusal` case —
which is the right default against anything real, and is exactly why the field
has to be declared rather than assumed.

`data.may_contain_pii: false` is why no `reports/.gitignore` appears in the
output tree.

---

## Deliberately left out

An example that exercised every feature would be a second test suite nobody
maintains. These are omitted, each for a reason:

| Left out | Why |
|---|---|
| `k > 1` (repeats, reliability) | the fixture is deterministic; repeats would measure nothing |
| holdout | a holdout run appends a real line to a real ledger, and the total is a **line count**. An example must not spend a look. |
| judge calibration (`score_agreement.py`) | needs a judge and a real human-label set; the judged gate is *derived*, so `calibrated` alone would not open it anyway |
| run history (`run_history.py`) | needs two comparable runs, and a trend is not a golden path |
| cost (`score_cost.py`) | a trace-less run cannot be priced **at all** — no `trajectory.json`, no tokens |
| `tool_selection`, `trajectory`, `authz` | all three read the trace's tool-call log, and there is no trace |
| conversations (`input.turns`) | the demo app keeps no conversation, so its adapter declares no `invocation.conversation`; `tests/test_multi_turn.py` drives the stub apps that do |
| `expect.state`, `seed_state`, `available_tools`, `excluded_tools` | RESERVED: declared in the case format, scored by nothing |

## Why nothing here is a stored run

The example ships its **inputs**, not its **output**.

A committed run directory is the artifact that rots. The plugin already had
one: the July field test's, which today would exit 6 on the runner's own
completeness check. Committing a fresh one would repeat the bet, and a stale
run directory is worse than none, because `--verify` failing on the plugin's
own example *is* the original finding, shipped a second time.

So the run is regenerated on every test invocation, into a temp directory,
against a real socket — and then re-checked with `--verify`, which takes no
plan and derives every check off the tree. It cannot be stale because it is
never stored.

What *is* committed from the run is `expected-run-tree.txt`: the list of files
this run writes, diffed against a real run every time. `docs/runner-contract.md`
§6 has the complete tree with its `# only when:` conditions; what an example
adds is **which of those conditions actually fired**.

## Then what?

This is `run --smoke` and stops there. The rest of the loop —
`analyze` over the verdicts, `optimize` with a statistical keep/revert gate,
`stats.py`'s one and only keep rule — needs a real app with real failures.
`docs/workflow.md` is the map.
