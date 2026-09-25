# Concepts

## The trajectory is the central artifact

Every testing mode — static cases, trace mining, policy
rules — produces or scores the same thing: a structured record of what the
app did (route chosen, tools called with which arguments, what they returned,
what was answered). OTel GenAI spans feed it, read from an OTLP file export
(`traces.source: otlp-file`; a Jaeger or Tempo backend alone is not queried
in v1 — the adapter contract's `traces:` block); `normalize_trace.py` converts
spans to the internal form and refuses to score transport-lossy traces —
missing spans or orphaned parents (`INFRA_INCOMPLETE`) — because a partial
trace scored as "missing tool call" is a manufactured failure. Missing result
*content* (content capture off) is not loss: the trace still scores, and only
content-dependent checks (argument faithfulness, answer faithfulness) are
reported unscorable.

## Route targets — the vocabulary

A **route target** is whatever the app dispatches between before it acts: a
*domain* (router→executor), a *node* (graph), or a *sub-agent* (orchestrator).
The three are the same object under three architectures, which is why one
routing layer scores all of them. An app that never dispatches — a single LLM
call with tools — has no route targets, so the routing layer simply does not
apply to it; every other layer still does.

## Layers and credit assignment

A single "was the answer good?" score can't tell you which prompt to fix.
Layered scoring can: routing 95% but argument accuracy 60% → work on the
domain agent's argument extraction, not the router. This layering is also
what the optimizer consumes — failures are attributed to a surface before
anything is edited.

## Access levels

- **White-box** (repo): full discovery, all layers, optimization.
- **Gray-box** (endpoint + traces): trajectory evals; optimization becomes
  recommendations (can't edit unseen prompts).
- **Black-box** (endpoint only): answer quality, consistency/metamorphic
  checks, safety probing, latency. Honest ceiling: black-box misses failures
  that elude typical test sets.

## Staged rigor

Eval demands scale with app maturity — asserting exact trajectories on an app
whose tools get renamed weekly produces only maintenance pain.

| Stage | What runs |
|---|---|
| `pre-stability` | Invariants: crash rate, responds-always, format, loops, refusals |
| `stable` | + routing, tool selection/args, trajectory (subset default) |
| `traffic` | + real-trace mining, judged layers (after calibration) |
| `production` | + online sampling, monitoring-window release validation |

## Ground truth — how correctness is actually graded

You cannot judge your way to factual correctness; ground truth is
**constructed**:

1. **Seeded environment + state diff** (strongest *in principle*): you planted
   the data, so the correct answer — and correct end state — is mechanically
   derivable. Also the only safe way to eval side-effectful tools. **The
   end-state half is RESERVED in this harness**: seeding and snapshotting are
   yours to drive out of band, `expect.state` is compared by no scorer, and
   `run_cases.py` never calls `environment.seed/.reset/.snapshot_state`. What
   a seeded environment buys you today is a trustworthy `expect.result`
   (execution layer), not a state diff.
2. **Mocked/recorded tools**: scripted returns make answers computable and
   runs deterministic.
3. **Business rules** (cheapest — most real policies are deterministically
   checkable): "never quote a price not in a tool result" fails any violating
   answer with no per-case label.
4. **Golden labels** for the rest.

The boundary: whether the *backend* returns correct data is backend-test
territory. evalup verifies the agent doesn't add errors on top: right
tool, right args, faithful synthesis, nothing fabricated beyond tool results.

## Trajectory matching: subset by default

Multiple valid paths are the norm (an exploratory `list` before a `get` is
fine). Cases assert a **subset** of required calls plus **forbidden** calls;
exact order only where a human explicitly said order matters. Over-strict
matching fails correct agents and teaches people to ignore the suite.

## The judge

**What a run does not score today.** `run_cases.py` has no scorer for the
**judged** layer (`expect.answer.rubric`) or for **business rules**
(`expect.answer.rules`): it records them as `unjudged (…)` and `unscored`, and
nothing else scores them either. The judge agent runs only inside `analyze
--label`, where a human labels beside it; its verdicts live in the calibration
record, never in a run's pass/fail. Calibration is therefore groundwork: it
tells you whether the judge could be trusted, and the run-time judged layer it
would unlock is not built.

- One strong judge, one rubric dimension per call, binary verdicts,
  reasoning first, "unknown" allowed.
- **Calibrated against a human** in two passes: ~30 labeled cases to discover
  the rubric's criteria, then ~100–200 stratified cases (oversampling the rare
  failing class) to measure agreement as TPR, TNR and Cohen's κ — never raw
  accuracy, since imbalanced data makes an always-pass judge look accurate.
  Until then every judged number is PROVISIONAL and the optimizer refuses to
  target it.
- Different model family than the app where possible (self-preference bias is
  measured and real); same-family is a consciously accepted degraded mode.
- Canary cases (known-good + known-bad) run in every scored run — a canary
  miss means judge or harness drift, and voids the run.
- Rubrics are versioned living documents: editing-while-grading is the
  expected workflow (criteria emerge from grading — "criteria drift").

## Statistical honesty

Agents are nondeterministic and eval sets are small; a +3% on 40 cases is
usually noise. Paired comparisons on identical cases; the keep decision is an
exact Bayesian P(improvement) at every n (an exact one-sided sign test is
reported alongside), so growing the dataset never flips a decision by
crossing a method boundary; every diff states what effect size was detectable
at its n (that detectable-effect figure is a normal-approximation planning
estimate — the decision statistics themselves are exact). A comparison also
reports how many cases it *couldn't* pair: cases present in only one of the
two runs are counted, and losing more than 10% raises a warning, because
crashed cases are filtered upstream as infra and would otherwise silently
leave the comparison to be measured on the survivors. Comparability requires the same dataset version AND
the same harness version — a scorer change alters what a number means just
like a dataset change does, so a harness upgrade means pinning a fresh
baseline. pass^k (all k repeats succeed) is the reliability
number — single-run pass rates hide flakiness. Repeated holdout looks are
counted and force a reseal (multiple-comparisons discipline).

## What this can never do

Prove correctness (evals sample), grade facts beyond constructed ground
truth, fix architecture problems via prompts (it flags them instead),
substitute for real users forever (mine traces or drift), or ship changes
without a human (by design).
