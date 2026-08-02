# Eval + Optimize System — Design Recommendation

Date: 2026-08-02. Author: research synthesis (9 subagents, 2 waves) + review of the
existing `agent-eval/` plugin, `research/`, and the `field-test-qa/` QA report.

Goal restated: **the ability to evaluate and optimize an AI feature inside an app**,
using **Claude Code as the intelligent driver and judge**, deterministic scripts for
scoring, and OTel traces as evidence — for any agent shape (single LLM, tool-agent,
router→executor, multi-agent, workflow).

This document does three things:
1. Answers each question you raised, grounded in what the field actually does.
2. Gives **keep / cut / change / enhance** verdicts on the current plugin.
3. Names the one architectural reframing worth making, and the concrete next steps.

---

## 0. Headline

**Your existing design is not naive — it is close to state of the art.** Almost every
core bet is independently confirmed by 2025–2026 practice:

- "Trajectory is the central artifact" → matches Phoenix (OTel/OpenInference-native eval),
  agentevals, Inspect's `EvalLog`.
- Subset trajectory matching by default → matches LangSmith agentevals
  (`strict`/`unordered`/`subset`/`superset`) and BFCL.
- Verifier-first, LLM-judge only where language understanding is genuinely required →
  matches Harbor's explicit philosophy ("judging is done with a script") and Caliper's
  "when both `expect:` and `assert:` are present, both must pass".
- Layered scoring for credit assignment → matches DeepEval's end-to-end / trajectory /
  component levels and Phoenix's per-component judge templates.
- Calibrated judge with TPR/TNR (not accuracy), canaries, holdout → matches Braintrust's
  Cohen's-κ calibration workflow and Hamel Husain's TPR/TNR guidance.
- Statistical keep/revert with paired tests → matches the GEPA/DSPy + eval-stats literature
  (McNemar exact, item-level cluster bootstrap, MDE at small n).

**Two things in your design are genuine differentiators no platform ships:**
(a) **refuse-to-diff-incomparable-runs** and (b) a **sealed holdout**. Keep both; they
are the strongest reasons this is worth building rather than adopting a platform.

So the work is **not** a redesign. It is: fix the gaps the QA report already found,
absorb ~8 specific techniques from the research, and make one architectural reframing
(**Agent-as-a-Judge**). Details below.

---

## 1. Knowing the agent(s) — topology detection

**Your question:** is it one agent, multi-agent, a workflow? How do I truly know, so I can
test correctly?

**What the research says.** Topology is detectable from **three independent signal
sources**, and you should use all three and cross-check (this is exactly your discover
skill's "read code → validate against live traces" loop, which is correct):

1. **Code fingerprints** (white-box):
   - Single LLM call: one model invocation, no tool schema, no loop.
   - Single tool-agent: one system prompt + tool set, wrapped in a `while` loop that
     calls model → executes tool_calls → feeds results back until stop.
   - Router→executor: an upfront classify step emitting a label, then `if/elif`/dict
     dispatch into N fixed handlers; only one "real" generation per request.
   - Multi-agent (dynamic): multiple agents with own prompts/tools, handoffs
     (`transfer_to_X`, CrewAI `Crew.kickoff`/`Process.hierarchical`, AutoGen
     `GroupChatManager`, OpenAI Agents SDK `handoff`+`Runner`).
   - Fixed workflow/graph: explicit graph built at design time (LangGraph `StateGraph`
     `add_node`/`add_edge`); path *set* is fixed even if edges are LLM-conditional.
2. **Prompt language**: orchestrator prompts say "delegate to / available agents:";
   router prompts say "classify into one of the following"; sub-agent prompts are narrow
   single-role.
3. **Trace/span shape** (OTel GenAI): count + nesting of `invoke_agent` spans. One
   `invoke_agent` with looping `execute_tool`/`chat` children = tool-agent; multiple
   siblings with distinct `gen_ai.agent.name` = multi-agent; identical trace shape across
   many inputs = fixed workflow; input-varying shape = dynamic.

**What each topology implies for eval (this is the important part):**

| Topology | How you must evaluate it |
|---|---|
| Single LLM call | Output-quality only; **no trajectory eval** — asserting one would be noise. |
| Single tool-agent | Full trajectory: tool selection, args, loop termination + output quality. |
| Router→executor | **Evaluate the router as a classifier** (precision/recall/confusion matrix per route) **separately** from **executor quality conditioned on route**. End-to-end ≈ routing accuracy × conditional executor quality. |
| Multi-agent | Per-agent trajectory + **inter-agent handoff correctness** + orchestrator decomposition quality + **failure attribution across the "space between agents."** |
| Fixed workflow | Path space is known → write **per-node golden behavior** and do node-level regression. |

**Verdict on current plugin:** discover already infers architecture and sets a capability
matrix — **keep**. **Enhance** with: (a) an explicit topology enum in `profile.yaml`
(`single_llm | tool_agent | router_executor | multi_agent | workflow`) that mechanically
drives which layers turn on; (b) framework fingerprint detection (LangGraph/CrewAI/AutoGen/
OpenAI-Agents-SDK/custom) to speed discovery; (c) for multi-agent, capture full
prompt+response at **every handoff boundary**, not just aggregate spans.

---

## 2. Understanding capabilities → generating tests + simulating users

**Your question:** how do I understand the system/capabilities well enough to generate good
questions and simulate users that truly test it?

**Test generation — what the research says:**
- DeepEval Synthesizer and Ragas both **evolve** simple questions into harder ones
  (multi-hop, comparative, constrained). Ragas builds a **knowledge graph** and weights
  toward multi-hop (single-hop factoids are the main source of trivial evals). Anti-triviality
  is the core discipline.
- **Generate with a different/stronger model than the one under test** — same-model
  generation inflates scores ("home-field advantage", preference leakage).
- **Coverage Illusion** (from your own research/06–08): synthetic misestimated real needs by
  62pp; blend real seeds ASAP; generator-collapse risk when generator shares the app's model
  family.
- Cartesian product over orthogonal dimensions (intent × persona × phrasing) + perturbations
  — this is already in your generate skill.

**User simulation — what the research says (this is where to be careful):**
- τ-bench pattern: give the simulator a **private goal**, instruct gradual disclosure, never
  reveal the answer/rubric. Keep user-simulator, agent-under-test, tool-simulator, and judge
  as **4 separate contexts** so no single context holds both the goal and the grading criteria.
- **Success is scored on end-state, not the agent's words** (τ-bench hashes resulting DB
  state). This is the robust anti-bluffing check.
- **"Lost in Simulation" (arXiv 2601.17087) — the caution:** LLM-simulated users are
  **unreliable proxies** — success rates vary up to **9pp** depending on which model plays
  the user, are miscalibrated (underestimate hard tasks, overestimate medium), and show
  demographic bias (worse for AAVE personas). **Implication:** don't trust a single-simulator
  success number; sample across simulator models/personas and treat variance as a signal;
  periodically validate against real human transcripts.
- **"Beyond Cooperative Simulators"**: default simulators are unrealistically cooperative;
  use diverse persona policies (impatient, vague, reluctant) while preserving the goal.

**Verdict on current plugin:** simulated-user subagent exists and is correctly gated on the
seeded-env capability — **keep**. **Enhance** with: end-state scoring as the primary success
signal (not text), multi-persona sampling with reported variance, and a "provisional until
validated against ≥N real transcripts" watermark mirroring the judge's PROVISIONAL discipline.
**Change** the generate skill to make "generate with a non-app model family" an explicit
requirement, not an aside (currently under-emphasized).

---

## 3. Deterministic vs LLM-as-judge — what to test how

**Your question:** is there something deterministic to test? Something for LLM-as-judge?

**The dividing line the whole field agrees on:**

**Deterministic (cheap, fast, reproducible — do these first, gate on these):**
- Routing: accuracy, per-target precision/recall/F1, confusion matrix, OOS leakage.
- Tool selection: precision/recall vs expected tools (DeepEval `ToolCorrectnessMetric` is
  a pure set comparison — no LLM).
- Tool arguments: exact/schema match for structured args.
- Trajectory: exact / in-order-subset / any-order-subset / superset matching
  (agentevals modes) + forbidden-call policy.
- Loop / redundant-call detection: group tool calls by `(name, hash(args))`.
- Format: JSON schema, regex, must-contain/must-not-contain.
- **Data-Q&A correctness via execution accuracy** (see §4) — compare result sets, not prose.
- Business-rule oracles: "never quote a price not in a tool result" — ~74% of policies are
  symbolically enforceable (your research/13 number, confirmed as sound).
- Stats: pass@k, pass^k, McNemar, bootstrap CIs.

**LLM-as-judge (only where language understanding is irreducible):**
- Faithfulness to tool results / groundedness (did it fabricate beyond what tools returned).
- Answer completeness / relevance against a rubric.
- Semantic argument equivalence (free-text args).
- Open-ended trajectory quality (was the path sensible), reflection quality.
- Refusal correctness (soft refusals evade keyword matching — needs a judge).

**Key nuance from research:** prefer **DAG / decomposed-binary judging** (DeepEval `DAGMetric`,
G-Eval `evaluation_steps`) over one holistic score — you assign the scores at terminal nodes,
the LLM only answers narrow binary sub-questions. This is both more reproducible *and* the
primary mitigation for same-family judge bias (see §9-judge). Binary > graded at small n;
pointwise+reference-guided > pairwise for single-output grading.

**Verdict:** your five scorers (`score_routing`, `score_args`, `score_answer`,
`trajectory_match`, `detect_loops`) + `stats` are exactly the right deterministic core —
**keep**. **Enhance** with an **execution-accuracy scorer for data-Q&A** (result-set diff)
and a **DAG-style decomposed judge** rubric format instead of holistic G-Eval-only.

---

## 4. Getting the data / ground truth — DB, API, seeded state

**Your question:** how do I show it how to get the data — from the database directly? from API
endpoints? This is the deepest question and the research answered it concretely.

**Four ground-truth strategies, ranked by robustness (your concepts.md already has this
ordering right — this adds the mechanics):**

1. **Seeded fixture + state-diff (strongest).** The tau-bench pattern, verified from source:
   ship a **frozen seed DB**; the eval stores the **gold tool-call actions** and expected
   output strings; grade by replaying gold actions against a fresh copy, taking a
   **canonical hash of resulting DB state**, and requiring the agent's post-run state hash
   to match exactly (+ grep for expected output substrings). *You don't need to know the
   answer — only what correct end-state looks like.* This is also the only safe way to eval
   side-effectful tools.
   - Fixture mechanics: factory libs (factory_boy/FactoryBot) with fixed IDs/dates;
     `pg_dump`/restore or docker-compose seeded volume; **Testcontainers** for ephemeral
     per-run DBs; **`BEGIN…ROLLBACK`** per case for fast isolation.
   - Keep **ground truth as code, not hand-typed numbers** — compute once from the frozen
     fixture via a script; regenerate if the fixture changes.

2. **Live-DB / dual oracle.** Compute the expected answer by running the canonical query
   directly — but compute it **two independent ways** (SQL vs pandas, or two query
   formulations) and **keep the case only if both agree**. Disagreement flags an ambiguous
   question to fix before it enters the set.
   - **Time-sensitive answers** ("expiring this month"): **freeze the clock** (freezegun/
     time-machine) at both generation and eval time; **normalize relative dates to absolute
     ranges** in the stored case (`"this month"` → `[2026-08-01, 2026-08-31]`); store a
     `snapshot_timestamp` the harness injects as "now".

3. **API-endpoint oracle.** Use the endpoint when it enforces business logic the agent must
   also respect (soft-deletes, computed fields, permission filters) — raw DB gives a
   "more correct but wrong-for-the-product" answer. Use raw DB to bulk-generate or to verify
   the API itself. Best practice: endpoint as primary oracle, DB as occasional cross-check
   (dual-oracle at the API layer). VCR-style cassettes (vcrpy/nock) snapshot responses for
   deterministic replay.

4. **Text-to-SQL execution accuracy (directly applies to any data-Q&A agent).** Spider/BIRD
   grade by **comparing result sets, not query strings** (credits semantically-equivalent
   queries), with float-rounding tolerance and subset/exact modes. **Test-suite accuracy**:
   run the same question against **several mutated fixture variants** so a lucky-coincidence
   wrong answer doesn't pass. Reference impl to fork: `defog-ai/sql-eval`.

**Permission / row-level-security testing (you have this app — it matters):** build cases as
a **(question × persona) matrix** against the same fixture; derive each persona's expected
answer by **running the oracle under that persona's real access scope** (Postgres RLS
`SET ROLE` / `SET app.current_user_id`), not by hand-filtering the full answer — so the oracle
exercises the same enforcement layer the app does. Keep a negative set where the correct
behavior is refusal/redaction, graded as structured refusal (this is exactly your app's
`X-User-Permissions` finding #4).

**Safety (non-negotiable):** dedicated **read-only DB role** (`GRANT SELECT` only);
`BEGIN…ROLLBACK` + `statement_timeout` as defense in depth; prefer a **read replica/snapshot**
over primary; **network-egress allowlist** so only read endpoints are reachable — making
"accidentally destructive" structurally impossible, not policy-dependent. Classify every tool
**safe-live / needs-mock / never-live** and enforce the allowlist in the **tool-execution
wrapper**, not the prompt.

**Verdict:** your adapter contract already declares `seed(case)/reset()/snapshot_state()` and
per-tool side-effect classes — **keep, this is the right interface.** **Enhance** discover to
actively help wire up ground-truth: detect the DB/ORM, offer to stand up a seeded fixture
(Testcontainers/docker-compose), and write an **execution-accuracy oracle** for data-Q&A.
**Add** the freeze-clock + absolute-date-range normalization to the case format. **Add** the
RLS-per-persona recipe to the permission-test generator.

---

## 5. Trajectory / tools / thinking — and multi-agent

**Your question:** how do I evaluate the thinking / the tools used / the orchestration / the
multi-domain? How to get the info faster?

**Trajectory & tools:** covered in §3 (deterministic matchers) — subset by default, exact
only where a human said order matters. **"Getting the info faster"** = OTel GenAI spans are
the fast path: a well-instrumented agent emits `invoke_agent` (per stage, `gen_ai.agent.name`),
`execute_tool` (per call, `gen_ai.tool.name`, `.call.id` correlating call→result), `chat`
spans (tokens, finish reason), correlated by `gen_ai.conversation.id`. That span tree *is* the
raw material for trajectory/precision/recall/loop/cost — no re-derivation needed. **If no
traces:** fall back to state-diff (request+prior-state → new-state+output) via the DB, or
reconstruct a pseudo-trajectory from timestamped API/DB-write logs. You lose per-step tool
precision, arg correctness, loop detection, and reliable attribution — so push for the minimal
1-line trace-id echo (your existing patch offer).

**Thinking / reasoning — the honest answer:** evaluate it as a **separate axis** from the
final answer (a right answer can hide broken reasoning and vice-versa), BUT it is the
**least reliable** thing to judge:
- Judges systematically overestimate reasoning completeness and **can't localize** where a
  reasoning error occurred.
- Visible chain-of-thought/extended-thinking is **not guaranteed to reflect actual
  computation** (the "monitorability gap") — evaluating scratchpad text as if it were a
  polished explanation is a category error.
- Verbosity bias: longer traces score higher regardless of correctness.
- **Recommendation:** don't gate on reasoning-quality judgments. Use reasoning eval only
  diagnostically (perturbation testing: delete/corrupt a step, see if the answer changes —
  if not, the visible reasoning wasn't load-bearing). Keep it out of the pass/fail path.

**Multi-agent / orchestration (your "how to handle all of that"):**
- **Test the orchestrator/router in isolation** as a classifier/decomposer — score only its
  decision against a labeled set, independent of what sub-agents do.
- **Test each sub-agent in isolation** by mocking the orchestrator — feed it the sub-task
  inputs it would realistically receive, evaluate as a standalone agent.
- **Compose:** end-to-end ≈ routing accuracy × conditional executor quality per branch;
  deviations tell you whether loss is upstream (routing/decomposition) or downstream
  (execution).
- **Failure attribution** ("the space between agents"): capture full prompt/response + tool
  I/O at every handoff, correlated by conversation id; for a failing run compare the three
  levels (orchestrator decision / sub-agent execution / tool result). Alert per-agent and
  per-tool-per-agent, not global aggregates (a tool can fail only for one agent's call style).
  **Sample near 100%** for multi-agent — failures hide in rare interaction combinations.
- Braintrust's **`SubtaskCoverage`** metric (did the final result cover every subtask the
  orchestrator assigned, each routed to the right sub-agent) is a concrete adoptable pattern.
- Phoenix's multi-agent methodology distinguishes **Handoff / System-level / Coordination**
  eval by architecture (Network / Supervisor / Hierarchical) — useful framing for the
  profile.

**Verdict:** your layered/credit-assignment model already encodes "which prompt to fix" —
**keep**. **Enhance** with explicit orchestrator-in-isolation and sub-agent-in-isolation test
modes, per-handoff capture, and a `SubtaskCoverage`-style deterministic check. **Cut** any
ambition to gate on reasoning-trace quality — demote to diagnostic-only.

---

## 6. Holding many questions, runs, and results — the data model

**Your question:** how to hold all these questions and runs and results — there'll be a lot,
across different angles?

**What mature platforms converge on** (LangSmith, Braintrust, Weave, Langfuse, Inspect):

- **Entities:** `Dataset` → `Example/Case` (input, expected, **metadata/tags**, split) →
  `Experiment/Run` (pinned to a dataset version) → per-case `Result` → `Score` → `Trace`.
- **Organize by angle via tags + named splits.** Two adoptables:
  - LangSmith **named splits** (durable `smoke`/`regression`/`adversarial` tags on subsets).
  - openai/evals **ID-encoded splits** (`<name>.<split>.<version>` in the filename) — more
    discoverable in a file-based system.
- **Comparability manifest — this is where you already win.** Braintrust's `RepoInfo`
  (git SHA + branch + dirty + diff + dataset_version + params_version per run) is the best
  template. **No platform refuses to diff incomparable runs** — they record enough to detect
  it but never gate. Your **refuse-to-diff** gate (same dataset version + harness version +
  prompt version, explicit `--force` override) is a real, non-trivial improvement over all of
  them. Keep it.
- **Repeats/flakiness:** adopt **Inspect's `epochs` + score-reducers** model directly — it's
  the only one that treats "repeat N times and reduce (mean/median/pass@k/pass^k)" as a
  composable primitive. Everyone else dumps raw per-trial rows.
- **Cost/latency:** track per-call tokens+latency+cost, and **tag every span by stage**
  (retrieval/tool/generation/judge) so cost rolls up **both per-run and per-stage**.
  Braintrust's trick — **exclude judge/scorer spans from task cost** — cleanly separates
  "cost to run the agent" from "cost to grade it". Build the rollup explicitly; no platform
  gives it free.

**File-based layout (git-native, your constraint — state lives as files in the app repo):**
```
.agent-eval/
  profile.yaml  adapter.yaml  findings.md          # discover output
  datasets/<angle>/*.yaml|jsonl                     # cases: id + split + tags + expect
  fixtures/                                          # seed DB dumps / factory scripts
  holdout/  + holdout.lock                           # sealed set, checksum-locked
  runs/<run-id>/                                     # per-case durable verdicts + manifest
  baselines/current.json                             # pinned baseline pointer
  reports/<run-id>.md                                # human-facing
```
Precedent: promptfoo (YAML config, SQLite results), openai/evals (registry YAML + JSONL),
Inspect (`.eval` structured logs). Pros of file-based: PR-reviewable, zero hosting, reproducible
by clone+rerun, readable by teammates who never open Claude Code. Cons: no live multi-user
dashboard, querying needs ad-hoc tooling — acceptable for your use case.

**Verdict:** your `.agent-eval/` layout + run manifest + baseline store already match this —
**keep**. **Enhance** with: named-split tags baked into filenames/IDs; adopt Inspect's
epochs+reducer vocabulary for pass^k; per-stage cost rollup with judge-cost excluded from
task-cost. **The refuse-to-diff gate and sealed holdout stay — they are your moat.**

---

## 7. Run modes — smoke / regression / post-optimize / full

**Your question:** ability to select what to run — full agent, after optimizing, smoke
regression, when updating the agent.

**What the research shows teams actually do:**
- **Smoke** (every change): ~10–20 cases, one representative per dimension, deterministic
  (fixed seed for any sampling), **membership is a durable tag** (not recomputed each run so
  it's diffable). Precedent: Braintrust `bt eval --first N`, promptfoo `--filter-sample N
  --filter-sample-seed`.
- **Full regression** (merge/nightly): run everything; the design is in *composition via
  tags/splits*, not a separate mode.
- **Targeted after optimizing one component:** promptfoo `--filter-failing` (rerun only prior
  failures) + filter by the tag for the component you touched. This directly serves your
  "run after optimizing X" ask.
- **CI gate:** deterministic script reads a JSON score file and sets the exit code — **the LLM
  is never in the final gate path** (see §9-harness). Braintrust `Reporter()`/exit-code +
  GitHub Action; promptfoo GitHub Action; Caliper `run … --output results.json` then a
  separate `compare` step.
- **Repeats:** Inspect `--epochs N` with `pass_at(k)` / `pass^k` reducers.

**A clean run-mode taxonomy to expose** (maps onto your existing `--smoke`):

| Mode | Selection | When | Gate? |
|---|---|---|---|
| `smoke` | tagged smoke subset, k=1 | every prompt/code edit (hook) | soft |
| `regression` | full suite, k≥3, pass^k | pre-merge / nightly | hard |
| `targeted` | filter by component tag + `--filter-failing` | after optimizing one surface | soft |
| `holdout` | sealed set, aggregate-only | optimizer keep/revert decision | decision |
| `full` | everything incl. judged layers | release validation | hard |

**Verdict:** you have `--smoke` and baseline diff — **keep**. **Enhance** into the explicit
5-mode taxonomy above; **add** `--filter-failing` and component-tag filtering for the
targeted-after-optimize case; **adopt** epochs+reducers for pass^k. The QA report's
first-run and zero-failure branch gaps (friction #28/#29/#32) are must-fixes here.

---

## 8. Optimization

**Your question:** something to run after optimizing; how to improve the agent.

**What the research says (strongly validates your 2026-07-18 decision to make Claude Code the
optimizer, keeping GEPA's *ideas* without the dependency):**
- **GEPA is the SOTA precedent** and it is exactly your mechanism: sample full execution
  traces → an LLM **reflects in natural language on *why* it failed** → propose a targeted
  mutation to **one** module → keep a **Pareto frontier** of candidates (not single-best) →
  validate. Beats RL (GRPO) by 6–19pp with up to 35× fewer rollouts; beats MIPROv2 by >10pp.
  DeepEval now ships GEPA as its default `PromptOptimizer` — so "Claude reflects on failing
  traces and proposes edits" is a validated design, not a guess.
- **The loop:** diagnose from traces → propose ONE change → evaluate on train → validate on
  **sealed holdout** → keep/revert with a **statistical test** → full regression before
  accepting.
- **Statistical rigor at small n:** paired **McNemar exact** (discordant pairs < 25) for
  pass/fail; **item-level cluster bootstrap** (resample items carrying all k runs, don't
  collapse to mode-per-item); Wilson interval for a single rate; compute the **MDE / noise
  floor** and treat sub-MDE "keeps" as *not proven*, not "no effect". At n=10–50 only
  ~15–25pp effects are distinguishable — say so.
- **What to optimize, by leverage:** decomposition/task-structure > instructions > few-shot
  > **tool descriptions** (underrated — arXiv reports +60% from description rewrites alone;
  your research/09 already flags this) > routing thresholds.
- **Multi-agent optimization = the credit-assignment trap:** never re-optimize the whole
  pipeline at once. Freeze all components except the one under test; evaluate **end-to-end**
  (a locally-better prompt can globally regress — documented RAG-citation collapse from an
  extraction-prompt improvement); run the **full** regression suite before accepting.
- **When manual > automated:** very small n (a human reading 20 transcripts beats a p=0.31
  "win"); early-stage systems still discovering their failure taxonomy (Hamel's critique of
  premature eval-driven dev); cross-component multi-agent changes.
- **Cross-task regression is real** — an accepted change must pass the whole existing suite,
  not just the failing slice.

**Verdict:** your optimize skill (Claude reflection = GEPA mechanism, one change at a time,
sealed holdout, statistical keep/revert, full lever set incl. tool descriptions, human
approves every edit, refuses uncalibrated-judge objectives) is **well-designed — keep**.
**Enhance** with: explicit McNemar-exact + cluster-bootstrap in `stats.py`; the Pareto-frontier
candidate pool (keep-if-wins-somewhere) rather than single-best; and the "freeze all but one
component + evaluate end-to-end + full regression" discipline for multi-agent apps.

---

## 9. The one architectural reframing: Agent-as-a-Judge

This is the highest-value new idea from the research, and it reframes your whole harness
favorably.

**The finding.** "Agent-as-a-Judge" (arXiv 2410.10934): an *agentic* evaluator that can
inspect intermediate artifacts (files, traces, DB state) — not a single LLM-judge call — hit
**~88–90% alignment with human consensus vs ~60–65% for a plain LLM-judge call** on agentic
tasks, at **~2% of human cost**. **Claude Code is structurally an Agent-as-a-Judge**: it can
read the code, the trace, and the before/after DB state while judging. Anchoring verdicts to
inspectable evidence (quote the tool result, diff the DB state) closes most of the reliability
gap **independent of the same-family concern**.

**Why this matters for "use Claude Code as the judge":** the self-preference worry is real but
narrow. Measured self-preference (Claude-v1 +25pp favoring its own outputs) is almost entirely
a **pairwise, holistic "which is better"** phenomenon. It **largely vanishes** for the mode you
should use: **pointwise, binary-decomposed, reference-guided grading against ground truth**.
So the judge design that both dodges the bias and matches SOTA is:

- **Decomposed binary rubric** (DAG / G-Eval `evaluation_steps`), not one holistic score.
- **Reference-guided** (give the judge the gold answer / expected trace).
- **CoT-before-verdict + forced evidence citation** (quote the exact span/tool-result/log line).
- **"Unknown" allowed; temperature 0.**
- **Pairwise + position-swap ONLY** for version-vs-version comparison, never as default grading.
- **Calibrate against humans:** ~30 examples to discover failure modes (open coding), then
  ~100–200 to measure **TPR/TNR and Cohen's κ separately** (never accuracy — a 90%-pass app
  makes an always-pass judge look 90% accurate with 0% failure recall). Re-calibrate on any
  model/prompt/distribution change. PROVISIONAL watermark until calibrated.
- **Cross-family judge (GPT/Gemini) is a separate CLI/script, NOT an in-session subagent** —
  Claude Code subagents cannot use a non-Claude model unless the whole session routes through
  Bedrock/Vertex. Reserve cross-family for **periodic 5–10% spot-check audits** and
  "did-the-model-game-its-own-eval" verification, not as the default (you'd lose the
  agent-can-inspect-evidence advantage and have to rebuild tool-calling for that provider).

**The deterministic/LLM division of labor (confirmed across Harbor, Caliper, Galileo,
Anthropic's own dynamic-workflows):**
- **Claude (the agent):** discovery, interviewing the human, test generation, failure
  diagnosis, evidence-anchored judging, proposing fixes.
- **Deterministic code (scorers as `bin/` CLIs on PATH):** all scoring, statistics,
  orchestration, isolation, and **the CI gate** (a script reads JSON and sets the exit code —
  no tokens, no approval, LLM never in the gate path).
- **Reward-hacking defenses (the central risk when one agent runs *and* grades):** keep the
  grader/scorer code **physically out of the agent-under-test's tool scope** (separate dir /
  worktree it can't `Read`) — Anthropic's own sabotage research found models with eval access
  interfered ~12% of the time; make `cheat` (e.g., reading forbidden files/answers) a
  first-class **typed outcome** (Caliper does this) that counts against the score, not a pass;
  treat every discovered hack as a new regression case.

**Headless/CI (so deterministic layers gate a PR without burning tokens):**
`claude -p "/agent-eval:run --smoke" --output-format json --bare --permission-mode dontAsk`
for the agent-driven part; a plain shell step reads `results.json` and sets the exit code for
the gate. `--bare` skips plugin/skill auto-discovery for reproducible machine runs; check the
`system/init` event for `plugin_errors`/`mcp_server_errors` before trusting a run.

---

## 10. Keep / Cut / Change / Enhance — the current plugin

**KEEP (validated by research, don't touch the core):**
- Trajectory-as-central-artifact + OTel normalization.
- The 5 deterministic scorers + `stats.py` (this is the right muscle).
- Subset trajectory matching by default.
- Staged rigor (pre-stability → stable → traffic → production) — this is your best UX idea
  and nothing else has it.
- Layered scoring for credit assignment.
- Calibrated judge with TPR/TNR + canaries + PROVISIONAL watermark.
- **Refuse-to-diff-incomparable-runs** and **sealed holdout** — your two true differentiators.
- Claude-Code-is-the-optimizer (GEPA ideas, no dependency), one change at a time, human
  approves every edit.
- Adapter contract with seed/reset/snapshot + per-tool side-effect classes + access levels.
- Read-only mode existing as a supported path.

**CUT / DE-SCOPE:**
- Gating on **reasoning/thinking-trace quality** → demote to diagnostic-only (judges can't
  localize reasoning errors; monitorability gap). Don't put it in the pass/fail path.
- Any same-family **panel-of-judges** as a bias fix → an all-Claude panel reduces variance but
  NOT self-preference; use decomposition + reference-guidance instead (you already cut PoLL in
  wave 2 — this confirms it).
- Holistic single-score G-Eval judging → replace with decomposed-binary DAG rubric.

**CHANGE:**
- Judge default → **decomposed-binary, reference-guided, evidence-citing** (Agent-as-a-Judge
  framing), not holistic.
- Test generation → make **"generate with a non-app model family"** an explicit hard rule.
- Cross-family judge → document clearly as a **separate CLI/script**, never a subagent
  (the constraint is now confirmed, not theoretical).
- Data-Q&A grading → **execution accuracy (result-set diff)**, not must-contain on prose.

**ENHANCE (absorb these specific techniques):**
1. Topology enum in profile + framework fingerprinting (§1).
2. Execution-accuracy scorer + seeded-fixture/Testcontainers helper + freeze-clock/
   absolute-date normalization + RLS-per-persona ground truth (§4). **This is the biggest
   functional gap** — it's what makes data-Q&A evals real.
3. Orchestrator-in-isolation / sub-agent-in-isolation modes + per-handoff capture +
   `SubtaskCoverage` check for multi-agent (§5).
4. Inspect-style **epochs + score-reducers** for pass^k; per-stage cost rollup excluding
   judge cost (§6).
5. Explicit **5-mode run taxonomy** + `--filter-failing` + component-tag filtering (§7).
6. McNemar-exact + item-level cluster-bootstrap + MDE reporting in `stats.py`; Pareto-frontier
   candidate pool in optimize (§8).
7. `cheat` as a typed outcome + scorer code physically isolated from the agent-under-test (§9).
8. Headless `--bare --permission-mode dontAsk` entrypoint + JSON-verdict → exit-code gate (§9).

**MECHANICAL FIXES (from the QA report — do these first, they're cheap and block adoption):**
- Read-only / out-of-tree mode as first-class (state-location override, per-gap no-patch
  fallback, schema fields).
- Trace-less operation as a supported profile (`expect.http_status`/`expect.outcome`,
  per-case identity slot, N/A-marking, no-traces pre-flight branch).
- Small-budget geometry: make grid/smoke/canary counts **fractions with a floor**, not
  absolutes that break below ~30 cases.
- First-run and zero-failure branches in run + analyze.
- Broken `docs/adapter.md` pointer; `--help` crashes in `score_routing.py`/`trajectory_match.py`;
  `route_acceptable` polluting the confusion matrix; missing `.md→.html` converter; define
  hash8/content-hash.

---

## 11. Adopt vs build — the short version

**Build (your actual value — nothing does these well):**
- Discovery / app-profiling via code archaeology + topology detection.
- Refuse-to-diff comparability gate + sealed holdout.
- Staged rigor as the gating UX.
- Claude-as-Agent-Judge with evidence anchoring.
- Failure-driven optimize loop with human-in-loop.
- Ground-truth wiring helpers (fixture/execution-accuracy/RLS).

**Adopt ideas from (don't reinvent):**
- **agentevals** — trajectory match modes (you already mirror these).
- **Inspect** — epochs + score-reducers vocabulary; EvalLog structure.
- **Braintrust** — RepoInfo manifest fields; SubtaskCoverage; κ-calibration workflow; judge-cost
  separation.
- **Phoenix** — per-component judge templates (Planning/Tool-Calling/Tool-Selection/
  Parameter-Extraction/Reflection); multi-agent methodology by architecture.
- **tau-bench** — state-hash oracle; pass^k.
- **defog-ai/sql-eval** — execution-accuracy reference implementation.
- **Caliper** — `expect:`/`assert:` dual gate, typed `cheat` outcome, isolated temp-home,
  independent judge model (this is the closest existing thing to what you're building).

**Do NOT build around:** OpenAI Evals hosted (shutting down Nov 2026); OpenAI Evals OSS
(stagnant). Verify Ragas version before depending (mid v0.3→v0.4 migration, repo moved to
`vibrantlabsai/ragas`).

---

## 12. Recommended next steps (in order)

1. **Mechanical fixes + read-only/trace-less/small-budget** (QA report punch list) — unblocks
   team adoption, cheap.
2. **Ground-truth wiring** (§4): execution-accuracy scorer + seeded-fixture helper +
   freeze-clock case format + RLS-per-persona. This turns "grades prose" into "grades
   correctness" — the single biggest capability jump, and directly answers your data question.
3. **Judge reframe** (§9): decomposed-binary + reference-guided + evidence citation; wire the
   κ/TPR-TNR calibration flow; document cross-family judge as a script.
4. **Run-mode taxonomy + headless gate** (§7, §9): the 5 modes, `--filter-failing`, and the
   tokenless CI exit-code gate.
5. **Multi-agent modes** (§5): orchestrator/sub-agent isolation + per-handoff capture +
   SubtaskCoverage — only when you have a multi-agent target to exercise them on.
6. **Stats hardening** (§8): McNemar-exact, cluster-bootstrap, MDE reporting, Pareto pool.

Everything above is additive to a fundamentally sound design. The reference app (RefApp)
is a router→executor with permission-scoped data-Q&A — steps 2 and 3 will pay off immediately
on it (the QA run already found the Licences-stub grounding bug and the holdout coherence
defect that only a calibrated judge would catch).

---

## 13. First concrete adapter: .NET (RefApp) — Wave 3

**The tool is general — this is one adapter, not the scope.** The methodology, the deterministic
scorers, the judge, the optimizer, the data model are all **language-agnostic**. What is
per-stack is only the thin **adapter**: (a) how to invoke the app, (b) where traces land + trace
convention, (c) where prompts/tool-descriptions live (optimizable surfaces), (d) how to seed/
reset the DB and get a read-only oracle. Python/TS/Go targets are just more adapter
implementations of the same contract. Below is the .NET instance because it's the first real
target (RefApp, ASP.NET Core / EF Core); it proves the adapter boundary is real. The
equivalent Python adapter would be: OTel GenAI auto-instrumentation (honors the
`OTEL_..._CAPTURE_MESSAGE_CONTENT` env var), FastAPI/Flask TestClient or live HTTP, pytest
fixtures / testcontainers-python / a read-only DB role, prompts in .py/.jinja/.yaml files.

The clean .NET path exists — none of this is a blocker:

**Instrumentation (get OTel GenAI spans out):**
- Modern path: `Microsoft.Extensions.AI` `ChatClientBuilder` → `.UseFunctionInvocation()`
  → `.UseOpenTelemetry(..., c => c.EnableSensitiveData = true)`. **`EnableSensitiveData`
  is the .NET content-capture toggle** — .NET does NOT honor the
  `OTEL_..._CAPTURE_MESSAGE_CONTENT` env var; set it in code (read your own env var at startup
  if you want a toggle). Register the source with `.AddSource("RefApp.Chat")`.
- **Gotcha:** the built-in `OpenTelemetryChatClient` only emits the `chat` span — it does NOT
  auto-span tool calls. Wrap `AIFunction.InvokeAsync` with your own
  `ActivitySource.StartActivity("execute_tool " + name)` + `gen_ai.tool.name`/`.call.id` tags
  to get proper `execute_tool` spans. (Semantic Kernel, source `"Microsoft.SemanticKernel"`,
  auto-spans function executions; sensitive data gated by `ILogger` Trace level, not a flag.)
- **Trace-id echo** (so the harness pulls the exact trace per request): middleware setting
  `Response.Headers["X-Trace-Id"] = Activity.Current?.TraceId.ToString()` in `OnStarting`.
  (`HttpContext.TraceIdentifier` is NOT the OTel trace-id.) Incoming `traceparent` is
  auto-parsed into `Activity.Current` (W3C default since .NET 5).
- Anthropic in .NET: no official SDK; the community `Anthropic.SDK` implements `IChatClient`,
  so it drops into the same `.UseOpenTelemetry()` pipeline.

**Headless invocation:** `WebApplicationFactory<Program>` (in-process, `Microsoft.AspNetCore
.Mvc.Testing`) is the fast inner loop; register a `TestAuthHandler : AuthenticationHandler<>`
via `ConfigureTestServices` to run each permission-persona without minting JWTs (ideal for
the RLS matrix). Pin `TimeProvider`/`IClock` via DI for the freeze-clock requirement. (Needs
`public partial class Program {}` if top-level statements made it internal.) Real Kestrel +
`HttpClient` for staging e2e.

**DB seed/reset (ground truth):** Testcontainers (`Testcontainers.PostgreSql`/`.MsSql`) for a
real disposable engine — **do NOT use EF InMemory** (can't test transactions/constraints/dates,
diverges from prod; a roster app's correctness is date/constraint-heavy). **Respawn**
(`Respawner.ResetAsync`) to wipe between cases (cheaper than container restart;
`TablesToIgnore` protects reference tables). Seed per-case with plain
`AddRange`+`SaveChangesAsync`, not `HasData`. **Oracle connection = a second `SELECT`-only
DB role** (`db_datareader` / `GRANT SELECT`), separate from the app's write connection — the
harness structurally cannot mutate what it grades.

**Tool discovery:** `chatOptions.Tools.OfType<AIFunction>()` exposes `.Name`/`.Description`/
`.JsonSchema` without a model call; SK: `kernel.Plugins.SelectMany(p=>p).Select(f=>f.Metadata)`.
No built-in read/write flag → infer side-effects by static analysis (grep tool methods for
`SaveChangesAsync`/`ExecuteUpdate`/`ExecuteDelete`) or a `[SideEffect]` convention.

**Optimizable-surface locations:** SK `skprompt.txt` / prompt-YAML files and runtime-loaded
`.txt` prompt files = ideal (patch without rebuild). Embedded resources / C# string literals /
`[Description]` attributes = need Roslyn-aware edit + `dotnet build`. **Expect a `dotnet build`
step in the optimize loop** unless the team externalized prompts to loose files — check this
early, it sets your iterate-cycle time.

**Interop, don't reinvent:** `Microsoft.Extensions.AI.Evaluation` is a real first-party package
with `ToolCallAccuracyEvaluator` / `IntentResolutionEvaluator` / `TaskAdherenceEvaluator`
(directly on-point for router→executor) + `dotnet aieval` CLI reports. Option: shell out to it
for the .NET-native `dotnet test` smoke path; keep your Claude judge for cross-language
consistency. At minimum mirror its `IEvaluator`/`EvaluationResult`/`NumericMetric` shapes.

---

## 14. Operating economics — what makes eval tools live or die (Wave 3)

The practitioner consensus (Hamel Husain, Eugene Yan, Shreya Shankar, Braintrust, OpenAI/
Anthropic cookbooks) reframes priorities:

- **The adoption hook is the error-analysis session, not the harness.** Teams that spend
  30 min open-coding 20–50 real traces get a ranked failure taxonomy ("3 issues = 60% of
  problems", the NurtureBoss case) — the "told me something I didn't know" moment — *before*
  any judge/CI exists. Teams that build infra first get "a pipeline that doesn't tell them
  anything they didn't already suspect, so it stops getting checked." **Your discover skill
  already IS this hook** (10 findings in 30 min in the field test) — lead with it.
- **Cost is tiered; deterministic-first is the lever.** Exact-match/code ≈ free; embedding =
  medium; LLM-judge = expensive tier, reserved for what code can't catch. Cadence: L1
  deterministic on every change → L2 cheap/sampled judge nightly → L3 full/expensive judge on
  release or suspected regression. Judge-result caching + Haiku-prefilter/Opus-on-disagreement
  keep it cheap. A cheap judge that misgrades is not cheap (Braintrust cost-efficiency point).
- **Calibration is ongoing weekly maintenance, not one-time.** Criteria drift is fundamental
  (you discover criteria *while* grading); alignment must be re-checked as production drifts.
  Budget for it or the judge silently decays.
- **What kills suites:** metric sprawl (many 1–5 scores nobody trusts), 100% pass rates (means
  the suite is too weak), aggregate scores hiding regressions, judges nobody validated,
  outsourcing annotation away from the people who understand the product, building harness
  before understanding failures (Hamel: eval-driven dev is premature until you know what
  success looks like). **60–80% of real eval effort is error analysis, not tooling.**
- **Minimum viable eval** (when the heavy harness is overkill): a spreadsheet + 20–50
  spot-checked outputs on each change; escalate only as failure surface grows. Don't let the
  harness's sophistication outrun the team's error-analysis habit.

**Implication for the plugin:** its staged rigor already encodes "don't demand more than the
app's maturity warrants" — extend that principle to the *team's* maturity: ship the
error-analysis/discover value first; make judged layers and the optimizer opt-in, later
unlocks, exactly as designed. The single fastest annotation-UX win (Hamel: "~10x faster
iteration") is a fast trace viewer with binary pass/fail + hotkeys + written critique — worth
building as the analyze skill's companion.

---

## 15. Authoring a decomposed judge rubric — the procedure + template (Wave 3)

**Procedure (repeatable):**
1. Start with a vague criteria string; auto-generate `evaluation_steps` once; **inspect and
   freeze/edit them** — pinning steps removes the run-to-run variance from the LLM
   regenerating its own reasoning (G-Eval finding).
2. **Decompose** "is it good?" into named binary sub-checks (DeepEval DAG: `TaskNode` extracts
   evidence → `BinaryJudgementNode` per facet → terminal scores you assign). Split
   double-barreled criteria ("accurate AND concise") into separate nodes. Stop decomposing when
   further splitting no longer removes ambiguity / when a facet never independently determines
   the verdict.
3. **Reference-guided + evidence-anchored:** inject the gold answer as a named placeholder;
   have a first pass quote the exact tool-result/span the verdict hinges on, then judge only
   against that extract. Reasoning before verdict, always.
4. **Guard the canary problem:** encode "refusal/decline is the correct behavior" explicitly
   where it applies, so a generic "did it answer?" criterion can't fail a correct refusal.
5. **Calibrate** (15–30 min): expert makes binary pass/fail + *written critiques* detailed
   enough to reuse as few-shot examples; measure TPR/TNR + Cohen's κ (not accuracy);
   iterate the judge to >~90% agreement / 75–90% match bar. Sample ~30 for failure-mode
   discovery (stop when ~20 traces add no new category, review ≥100 to start), then ~100–200
   stratified across failure modes, oversampling the rare failing class + keeping some random.
6. **Version the rubric as a living document** — every drift discovery is a version bump with
   the specific failure that motivated it (`version` / `last_calibrated` / `calibration_notes`).

**Concrete template — grading "How many licenses expire this month?"** (ties execution-accuracy
§4 + judge §9 together): a DAG where deterministic gates run first and the subjective tail is
structurally unreachable until they pass, so a nicely-worded hallucination can never outscore a
correctly-hedged evidence-backed answer:

```yaml
rubric_id: data_qna_license_expiry_v1
version: 3
last_calibrated: 2026-07-15
calibration_notes: >
  Iteration 2 added "has_tool_evidence" after the judge passed a plausible-sounding
  number present in NO tool-call output (hallucinated count — the Licences-stub bug).
nodes:
  - id: root_task            # TaskNode: extract stated number, claimed date window, quoted tool evidence
  - id: has_tool_evidence    # BinaryJudgement: is the number backed by a verbatim tool/DB result quote?
      on_false: {score: 0, reason: "no evidence cited — likely hallucinated"}
  - id: correct_window       # BinaryJudgement (reference-guided): does the window match gold "this month"?
      on_false: {score: 2, reason: "wrong time window"}
  - id: correct_count        # BinaryJudgement: does stated number == expected_count, per cited evidence?
      on_false: {score: 3, reason: "count mismatch despite correct window/evidence"}
  - id: refusal_check        # CANARY GUARD: if gold behavior == refusal, did it decline vs fabricate?
      on_false: {score: 1, reason: "fabricated a count where refusal was correct"}
  - id: presentation_quality # GEvalNode w/ frozen evaluation_steps — only reachable after gates pass
      score_range: [8, 10]
```

Note how `has_tool_evidence` is exactly the check that would have *failed* the Licences-stub
answer your baseline run passed — this template is the concrete fix for that class of bug.

---

## 16. Final recommendation (my opinion, sequenced)

The design is de-risked as far as research can take it. The remaining risk is entirely in
execution and only retires by building. Concretely, what I'd do:

1. **Stop researching.** You have 14 research docs + this. The marginal doc is now worth less
   than the marginal line of working code. Nothing above changed a core decision — it sharpened
   *how*, not *whether*.
2. **Keep the architecture general; prove it on ONE vertical slice first.** The tool stays
   general-purpose (any language, any topology) — but generality is *earned adapter-by-adapter*,
   validated by a real end-to-end run, not asserted up front (your own critique/09 said this).
   So: build the general core + the language-agnostic adapter CONTRACT, then implement the
   FIRST adapter (.NET/RefApp, §13) and run the whole loop through it: discover → ~15
   deterministic cases → one confusion matrix + **one execution-accuracy data-Q&A check** →
   a report with a fast trace viewer. The discipline that keeps it general: everything
   .NET-specific lives behind the adapter interface; the scorers, judge, stats, data model,
   and skills never mention .NET. When the second target is Python/TS, you write a second
   adapter and nothing else changes. Do NOT build 3 half-adapters in parallel — one working
   end-to-end adapter proves the boundary; the rest are then mechanical.
3. **Lead with the error-analysis payoff, not the optimizer.** Discover already delivers the
   "told me something I didn't know" moment. The optimizer is premature (no calibrated judge,
   n<100) and seductive — resist it until the measurement it optimizes toward is trustworthy.
4. **Make execution-accuracy ground truth the #1 feature.** It's the difference between
   grading prose and grading correctness, and it's the exact thing that catches the
   Licences-stub bug your current harness passed. This is where the tool stops being a linter.
5. **Lean into Agent-as-a-Judge — it's your unfair advantage.** A library makes one judge call
   over a transcript; your harness can read the code, trace, and DB while judging (~88–90% vs
   ~60–65% human alignment), and pointwise evidence-anchored grading also dodges the
   same-family self-preference bias. Build the judge as an evidence-citing agent, decomposed-
   binary, calibrated with TPR/TNR.
6. **Budget for the operating reality:** deterministic-first for cost, judge-result caching,
   `discover --diff` to fight rot, a named domain arbiter, and the fast trace-viewer annotation
   UX. These decide whether the tool is used in month 3 or abandoned.

**What NOT to build (protect your time):** reasoning-trace quality gating (diagnostic-only),
same-family judge panels (don't fix the bias), chaos/red-team integration (Tier 4), and any
further generality before the RefApp slice works end to end.

The one-line version: **your thinking is done; keep it general via a clean adapter boundary
but prove it end-to-end through the first (.NET) adapter, lead with discover's error-analysis
payoff, and make execution-accuracy the feature that turns it from a linter into a correctness
checker.**

---

## 17. Keeping it general — the adapter contract is the seam

Since the goal is a general tool (not .NET-only), the single most important architectural
discipline is a **clean adapter contract** so language/framework specifics never leak into the
core. The contract each target implements (your `adapter.yaml` + a thin shim):

| Capability | What the adapter provides | .NET example | Python example |
|---|---|---|---|
| **Invoke** | `start_session / send_turn(text, persona) / end_session → {text, trace_id}` | `WebApplicationFactory<Program>` + `TestAuthHandler`; `X-Trace-Id` header | FastAPI `TestClient` / live HTTP; `traceparent` |
| **Traces** | where spans land + convention + optional mapping shim | OTLP from `UseOpenTelemetry`; gen_ai.* | OTel SDK exporter; gen_ai.* / OpenInference |
| **Content capture** | on/off | `EnableSensitiveData=true` (code flag) | `OTEL_..._CAPTURE_MESSAGE_CONTENT` (env) |
| **Optimizable surfaces** | file paths of prompts/tool-descriptions + edit mode | skprompt/YAML (no rebuild) or C# literal (rebuild) | .py/.jinja/.yaml files |
| **Seed / reset / snapshot** | fixture load, state wipe, state hash | Testcontainers + Respawn + `SaveChanges` | testcontainers-python + transaction rollback |
| **Oracle** | read-only data access for ground truth | `SELECT`-only DB role / read replica | read-only DSN |
| **Tool catalog + side-effects** | list tool names/schemas + safe-live/needs-mock/never-live | `AIFunction.JsonSchema` / SK metadata | function/tool registry introspection |
| **Access level + safe-to-attack** | white/gray/black; permission model | bearer / trust headers | bearer / cookies |

Everything else — scorers, judge, stats, run manifest, reports, generate/analyze/optimize
skills — is written against this contract and **must never branch on language**. That is what
makes it "general, not only .NET": the .NET knowledge lives in one adapter implementation +
the discover skill's stack-detection, and adding a language = adding one adapter. Your existing
`adapter-contract.md` already defines most of this; the work is to (1) confirm every core script
consumes only the normalized/contract form, and (2) ship the .NET adapter as the reference
implementation others are cloned from.

---

## 18. Metrics reference (verified formulas) — Wave 4

A dedicated pass catalogued the metric landscape across DeepEval, Ragas, Phoenix,
Microsoft.Extensions.AI.Evaluation, BFCL, τ-bench, Spider/BIRD, and the classic IR/NLP metrics,
with primary-source-verified formulas. **The governing rule first, because it matters more than
the catalog:**

> **Metric sprawl is a failure mode, not thoroughness.** The ~50 named metrics across frameworks
> collapse to ~7 real constructs (below). Jason Liu: 20+-metric frameworks are "complexity
> theater." Hamel/Eugene Yan: generic metrics (ROUGE, BERTScore, even G-Eval) "barely correlate
> with application-specific performance." Pick metrics **from your error-analysis failure modes**,
> not from this list. Target ~3–7 checks + one top-level outcome metric. **Segment by failure
> mode; never report one blended average** (it hides regressions in the slice that matters).

### 18a. Deterministic metrics (compute in code — cheap, reliable, gate on these)

**Routing / classification**
- Precision `TP/(TP+FP)`, Recall `TP/(TP+FN)`, `F1 = 2PR/(P+R)`.
- **Macro-F1** = unweighted mean of per-class F1 (every route counts equally — use this for
  routing, so a rare-but-important route can't be hidden). **Micro-F1** = pool TP/FP/FN then
  compute once (= accuracy in single-label multiclass — hides the broken route).
  **Weighted-F1** = per-class F1 weighted by support. Worked example (routes A,B perfect,
  C fully broken, traffic 900/90/10): Macro=0.667 (exposes C), Weighted=Micro=Accuracy=0.99
  (hide C). → **report macro-F1 + the full confusion matrix; never lead with accuracy.**
- **MCC** `(TP·TN−FP·FN)/sqrt((TP+FP)(TP+FN)(TN+FP)(TN+FN))` — robust single number under
  imbalance (undefined if a class is never predicted — special-case it).
- **OOS / none-of-the-above recall** as its own headline number (CLINC150 prioritizes OOS
  recall — routing a true-OOS query into a real tool is worse than over-triggering fallback).
- Pitfall: accuracy on imbalanced intents is a vanity number (950/50 → "always majority" = 95%
  with 0% recall on what matters). Use PR-AUC not ROC-AUC for rare-event guardrail classifiers.

**Tool calls** (deterministic when you have an expected-call trace — prefer this over a judge)
- Tool-selection `Precision = |matched|/|predicted|`, `Recall = |matched|/|gold|` (ToolTalk
  greedy match). Success = `(matched==gold) ∧ (no incorrect side-effecting action)`.
- **BFCL matching** (the rigorous reference): AST accuracy (name + required params + strict
  per-type value match; lists order-exact; extra params = hallucination flag), executable
  accuracy (run it; exact / 20%-numeric-tolerance / structural match), and
  relevance/irrelevance detection (does it correctly call NO tool when none apply).
- **Order-sensitivity is a real choice:** order-insensitive is right for commutative tools,
  wrong when a call depends on a prior call's output (fetch id → then cancel). Hash calls by
  `(name, args)`, not name alone.
- Pitfall: partial-credit averaging (DeepEval ToolCorrectness `correct/total`) can mask one
  wrong-but-critical arg (the payment amount) — add an all-or-nothing check for safety-critical
  calls.

**Trajectory** (agentevals modes, verified names): `strict` (same calls, same order),
`unordered` (same set, any order), `subset` (no extra calls), `superset` (all reference calls +
extras allowed). Arg comparison is a separate axis (`exact`/`ignore`/`subset`). Google ADK's
`tool_trajectory_avg_score` defaults to exact-order. **Subset is your right default** (multiple
valid paths). Efficiency = `optimal_steps/actual_steps` but **gate it on task success first**
(else you reward "confused but lucky"). Loop detection = repeated `(tool, args)` with no state
change (not raw repetition — that false-flags legit retries/pagination).

**Execution / data-Q&A (the one to build — turns linter into correctness checker)**
- **Execution Accuracy (EX, BIRD):** `EX = (1/N)Σ 𝟙(result_set(pred) == result_set(gold))` —
  compare **result sets**, not query strings. Row-bag equality (order-insensitive unless
  `ORDER BY`), **float tolerance / rounding required** (exact float equality spuriously fails
  correct queries), execution error = automatic 0.
- **Exact Set Match (Spider):** structural per-clause set comparison — but penalizes
  semantically-equivalent queries (JOIN vs subquery); measured 2.5% avg / 8.1% worst-case
  false-negatives. Prefer EX.
- **Test-suite accuracy:** require the answer to match gold across **several fixture DB
  variants**, not one — kills the "lucky coincidental match on one DB" false positive. This is
  the §4 "run against several seeded fixtures" idea, formalized.
- Pitfall: EX on a single DB has false positives (two non-equivalent queries can coincide on
  one instance) → use the test-suite / multi-fixture variant for anything you gate on.

**String/overlap metrics — know them so you can REJECT them.** EM, token-F1, BLEU, ROUGE-1/2/L,
METEOR, BERTScore, Levenshtein all measure lexical/embedding overlap, **not correctness**. Four
documented failure modes: paraphrase penalized, hallucination rewarded (copies n-grams + inserts
one false fact → still scores high), bag-of-words insensitivity ("cat chased dog" ≈ "dog chased
cat"), reference-set dependency. **Do not use these for correctness gating** — use execution
accuracy or a decomposed judge instead. (Fine only as loose drift signals.)

**Reliability / sampling (cheap, always worth it)**
- **pass@k** (Codex/HumanEval, unbiased estimator): `pass@k = E[1 − C(n−c,k)/C(n,k)]` from n
  samples with c correct — "can it succeed at least once." Needs temperature>0.
- **pass^k** (τ-bench): `pass^k = E[C(c,k)/C(n,k)]` — "does it succeed on ALL k" = the
  reliability number. τ-bench: gpt-4o >60% pass^1 but <25% pass^8 on the same tasks — single
  runs hide this. **The pass@k − pass^k gap is itself your flakiness metric.**
- Temperature tension: higher T raises pass@k (more shots) but lowers pass^k (less consistency)
  → report both. Even T=0 isn't bit-reproducible (batching/MoE routing).

### 18b. LLM-judge metrics — the redundancy map (~50 names → ~7 constructs)

| Real construct | Aliases across frameworks | The one thing that matters |
|---|---|---|
| **Groundedness / faithfulness** | DeepEval Faithfulness, Ragas Faithfulness, Phoenix Faithfulness, .NET Groundedness | **Claim-decomposition versions (DeepEval/Ragas) are meaningfully more reliable** than single-holistic-call (Phoenix/.NET). Build the decomposed one. |
| **Answer relevance** | DeepEval/Ragas AnswerRelevancy, .NET Relevance | Checks topicality NOT factuality — a relevant-but-wrong answer scores high. Cheap pre-filter only. |
| **Context relevance/precision/recall** | DeepEval Contextual*, Ragas Context* | RAG-only; skip entirely for structured SQL/API data layers. |
| **Tool correctness** | DeepEval ToolCorrectness, Ragas ToolCallAccuracy, Phoenix Tool Selection, .NET ToolCallAccuracy | Ragas + DeepEval-core are **deterministic exact-match, not judges** — prefer those. |
| **Goal/task completion** | DeepEval TaskCompletion, Ragas AgentGoalAccuracy, .NET IntentResolution | The business "did the user get what they needed" signal. Use with-reference variant. |
| **Plan/step quality** | DeepEval PlanAdherence/PlanQuality/StepEfficiency, Phoenix Path Convergence | Debug-only. ⚠️ DeepEval PlanAdherence **silently auto-passes score=1 if no plan is found** — unsafe to gate on. |
| **Reference-grounded correctness** | OpenAI fact.yaml/closedqa, Anthropic rubric, DeepEval G-Eval, Ragas FactualCorrectness | The offline-regression workhorse (needs gold answers). |

**Two false-friend traps:** (1) DeepEval `Hallucination` ≠ `Faithfulness` — Hallucination checks
a *curated trusted context* for contradiction, Faithfulness checks *retriever output*; different
inputs, don't swap them. (2) Phoenix "Agent Planning" and "Reflection" templates **don't exist**
(confirmed); "Path Convergence" is deterministic, not a judge.

**The single biggest reliability lever (measured):** reference-guided grading. MT-Bench found
judge failure on math dropped **70% → 15%** just by inserting the gold answer into the prompt.
Always give the judge the reference when you have one.

### 18c. What to actually use — router→executor data-Q&A shortlist

In priority order (matches the redundancy map to *your* app):
1. **Route/tool-selection correctness — deterministic** (macro-F1 + confusion matrix + OOS
   recall; tool P/R vs expected trace). Cheapest, highest-signal, catches the #1 router failure.
2. **Groundedness against the executor's actual query result — decomposed-claim judge.** Your
   single highest-value custom metric; the exact check that catches the Licences-stub bug.
3. **Execution accuracy (result-set diff, multi-fixture)** for data-Q&A correctness.
4. **Task/goal completion (with reference)** — the end-to-end business signal.
5. **AnswerRelevancy** — cheap referenceless pre-filter for off-topic/evasive answers.
6. **Reference-guided correctness** (reimplement OpenAI `closedqa` cot-classify pattern) for
   offline regression where gold answers exist.
- **Reliability layer (always on):** pass^k, crash rate, latency, cost-per-stage.
- **Deprioritize:** all Context*/RAG metrics (structured data, not docs), Bias/Toxicity/PII
  (unless compliance), conversational metrics (unless truly multi-turn), Plan* (debug-only,
  and the auto-pass trap), string-overlap metrics (reject for correctness), pairwise (offline
  model/prompt comparison only, with position-swap).

**Numbers worth remembering:** reference grounding 70%→15% judge-failure; pass@k vs pass^k gap
= flakiness; macro-F1 exposes the broken route that accuracy/micro/weighted hide; claim-
decomposed groundedness > holistic; EX compares result sets not query strings with float
tolerance.

---

## 19. Canonical case schema — stolen from the best benchmarks (Wave 5)

A pass over how τ-bench, τ²-bench, BFCL, AppWorld, WebArena, SWE-bench(-Verified), ToolTalk,
API-Bank, MINT, and GAIA *structure* their cases (not their scores) yields a convergent design.
Adopt this for the generate skill's output format:

**Design principles every rigorous benchmark shares:**
- **State/execution oracle over trajectory-string matching.** τ-bench hashes resulting DB
  state and never diffs the gold trajectory against actual calls — *any path to the correct
  end-state passes*. AppWorld/WebArena/BFCL-multiturn all do state-or-result diff. → your data-Q&A
  oracle = **compare the result set**, gold trajectory is audit-only with just load-bearing
  args marked (τ²-bench `compare_args`).
- **Composable, independently-gated oracle components, AND-ed.** τ²-bench multiplies DB-hash ×
  env-assertion × communicate-substring × action-match; WebArena multiplies string × URL ×
  program_html. Each component gates independently → precise failure attribution.
- **Type-aware normalization for the communicated answer** (GAIA "quasi exact match": infer
  type, normalize case/whitespace/punctuation/units) + **per-field tolerance** (ToolTalk: dates
  by `.date()`, free text by embedding-similarity 0.8–0.9, lists as sets, IDs exact).
- **Read/write asymmetry:** tolerate harmless extra reads, hard-fail wrong mutations (ToolTalk
  `success = recall==1 AND bad_action_rate==0`); check for **collateral damage** (AppWorld —
  did it change *only* what it should).
- **No-op sanity label:** does a do-nothing agent correctly *fail* this case? Catches vacuous
  cases (AppWorld `no_op_pass`/`no_op_fail`).
- **Sampled tool subset + deliberately-excluded/irrelevant tools** to test relevance detection
  (BFCL, ToolBench retriever split).
- **Template + instantiation_dict** → many realistic variants from one authored case, with a
  **public/private split** so the oracle never leaks into the prompt (AppWorld); score
  consistency across all variants of a template (AppWorld SGC).
- **Difficulty is hidden from the agent**, used only for stratified reporting (GAIA).
- **Living-issues log embedded in the case** (τ²-bench `issues:[{status}]`) + **suite
  self-validation** (multi-annotator convergence like SWE-bench Verified which filtered 68% of
  instances; ground-truth-executes checks like ToolTalk).

**Canonical case schema (for the generate skill):**
```jsonc
{
  "id": "roster_qa_0142",
  "category": "aggregation",              // single_lookup|filter|aggregation|multi_hop|mutation|refusal
  "difficulty": "medium",                  // HIDDEN from agent; stratified reporting only
  "template_id": "shift_count_by_role",    // groups variants → consistency scoring (SGC)
  "instantiation_params": {"role":"nurse","week":"2026-W05"},  // public, safe in instruction
  "setup": {
    "seed_db_fixture": "roster_seed_142.sql",   // or generator fn ref
    "as_of_datetime": "2026-08-02T09:00:00Z",   // freeze-clock
    "actor": {"role":"scheduler","employee_id":"e_881","permissions":["roster.read"]}
  },
  "instruction": {"text":"How many nurses are on the night shift next week?",
                  "known_info": null, "unknown_info": null},   // tau2 asymmetry for interactive
  "available_tools": ["query_shifts","query_employees","get_current_date"],  // sampled subset
  "excluded_tools": ["assign_shift"],     // present in catalog but irrelevant → relevance test
  "gold": {
    "end_state_check": {"type":"db_query_result_diff",
      "reference_query":"SELECT COUNT(*) FROM shifts WHERE role='nurse' AND shift_type='night' AND week='2026-W05'",
      "compare":{"mode":"set_equal","float_tolerance":0.0}},
    "communicated_answer": {"type":"quasi_exact_match","value":"7",
      "normalize":["case","whitespace","strip_units"]},
    "gold_trajectory": [{"tool":"query_shifts","compare_args":["role","shift_type","week"]}]  // audit-only
  },
  "forbidden_actions": ["mutate_*"],       // hard fail if any write tool fires on a read task
  "no_op_expectation": "fail",             // sanity: must a do-nothing agent fail this?
  "scoring": {"components":["end_state_check","communicated_answer"],"rule":"all_must_pass",
              "action_penalty":"hard_fail_on_forbidden_action"},
  "interactive": {"is_multi_turn": false, "termination": null, "user_simulator": null},
  "known_issues": []                       // tau2-style living annotation log
}
```
For interactive/multi-turn cases: user = **scripted LLM with a hidden, partitioned goal**
(known vs unknown info) that drip-feeds and emits a termination signal (`###STOP###` / turn
budget / `complete_task()`); feedback is a **separate axis** from turns (MINT), and the
simulator must never see the gold answer.

---

## 20. Security / red-team layer (Wave 5) — directly load-bearing for this app

The app's real findings (caller-supplied permission headers, no `[Authorize]`, committed keys,
stub tool) map onto a concrete, testable threat model. **The single most important design rule:**

> **Authz testing must hit the TOOL layer directly, not just the chat front door.** A prompt-only
> test can PASS (agent politely "refuses" in text) while the underlying endpoint stays wide open.
> Grade on **deterministic assertions over the actual tool-calls and returned record IDs**, not an
> LLM-judge reading the chat.

**OWASP LLM Top-10 (2025) → testable case types.** Highest-priority for this app: **LLM06
Excessive Agency** (out-of-scope action attempts, grade refuse-vs-comply), **LLM02 Sensitive
Info Disclosure** + authz, **LLM01 Prompt Injection** (direct + indirect), **LLM07 System-Prompt
Leakage**. Also testable: improper output handling, misinformation, unbounded consumption.

**Agentic-specific attacks (build these):**
- **Confused-deputy / privilege escalation — #1 risk.** The agent holds broad DB/tool access;
  a low-priv caller asserting elevated scope in a header/prompt is the whole exploit. Test with
  a **persona matrix** (anon/staff/manager/admin, same seeded data) + assert **monotonic
  scoping** (a lower-priv persona never sees a superset).
- **BOLA** (another user's record by id/name while authed as someone else) — including the
  leak-in-the-refusal failure ("I can't show User 4821's schedule, *who's on leave Tuesday*").
- **BFLA** (invoke privileged functions from a low-priv persona via social framing).
- **Indirect/second-order injection via app data** — a poisoned shift-note/comment field the
  agent later reads and obeys (worse here because of the stub tool).
- Memory poisoning (if session memory exists), goal hijacking, multi-step chains.

**Prompt-injection test design:** direct + indirect; **canary tokens** (deterministic) but pair
with a semantic-leakage check — a canary only fires if the model complies, so a correct refusal
*and* a broken harness both show 0% trip (need a known-vulnerable positive control). Report
**Attack-Success-Rate AND over-refusal rate side-by-side** (ASR-only rewards refusing
everything). Layer encoding/obfuscation (base64/hex/homoglyph) and **multi-turn crescendo**
(single-turn undercounts real risk by 60–90pp).

**Tooling:** **promptfoo redteam** is the best fit — its `bola`/`bfla`/`rbac`/`tool-discovery`/
`prompt-extraction`/`excessive-agency` plugins map 1:1 to the app's findings; point it at the
endpoint via an `http` provider. PyRIT for custom chained/confused-deputy scripts, Garak for a
broad first-pass scan, DeepTeam if Python-native. Position red-team as **"document + point these
at the adapter,"** not deep integration (matches your research/09 de-scope).

**Safety gate (non-negotiable):** `SAFE_TO_ATTACK` opt-in (default off), **staging-only env
allowlist** (runtime hostname check), seeded synthetic data + canary records (never real
employee PII), isolated low-priv test identities, turn/volume caps on adaptive attackers,
separate audit log, and human review before a generated payload enters an unattended CI suite.

---

## 21. Production / online evaluation (Wave 5) — the `traffic`/`production` stages

Your staged rigor already names `traffic` and `production` stages; here's what fills them.
**One instrumentation, two consumers:** the same trace schema feeds offline Evals and the
production Logs view, so **production traces seamlessly become eval cases** — this is the whole
payoff of the OTel-normalized trajectory being the central artifact.

- **Online evaluators = the same judges on sampled live traffic, async, off the request path.**
  Sampling is the cost lever, **per-evaluator**: 1–10% on high-volume happy-path, up to 100% on
  error-flagged/negative-feedback traces; LLM-judge scorers sampled lower than code scorers; hard
  weekly spend caps with auto-pause (LangSmith pattern).
- **Guardrails ≠ evaluators.** Guardrails are inline, synchronous, deterministic, and *block*
  (request path). Evaluators are async and only *measure*. Different tiers — don't conflate.
- **The flywheel** (Shreya Shankar): production traces → sample + label → timestamped case DB →
  judge pulls recent/divergent few-shots from it → low-scoring traces mined for new failure
  patterns → promoted to the regression suite. **Auto-clustering** (Braintrust "Topics": summarize
  each trace → score Task/Sentiment/Issues → cluster ≥100) surfaces **blind spots and silent
  failures** an offline suite never anticipated. This is the automated version of §22's
  open-coding.
- **PII redaction before a trace becomes a stored case** (Microsoft Presidio — NER+regex — as a
  pre-promotion transform; no vendor ships this first-party).
- **Drift:** distinguish **data drift** (input distribution moved) from **concept drift**
  (input→output relationship moved) — KS/Chi-square tests, PSI / Wasserstein / JS-divergence for
  a continuous score, embedding-space drift for text. **To tell "did the app change or the
  traffic change," run fixed synthetic canary queries on a schedule** — movement on *fixed* inputs
  = app/model/judge change; movement only on live traffic = distribution change.
- **Judge drift is its own failure mode** — pin judge model versions, re-run the TPR/TNR
  calibration periodically, treat a judge-score drop as ambiguous until the judge is re-validated
  against a frozen calibration set.
- **Holdout contamination warning:** the flywheel's few-shot mining and any fine-tuning MUST
  exclude sealed-holdout cases — otherwise a held-out query reappears via live traffic. (This is
  exactly why your sealed-holdout discipline is load-bearing, not ceremony.)
- **Monitoring dashboard (small + segmented):** leading = latency p95/p99, crash/error/timeout
  rate, refusal rate, judge-flagged-failure rate on the sample, cost/token burn, guardrail-trigger
  rate. Lagging = task-success proxy (deterministic where possible — execution-accuracy, not
  "looks plausible"), user-corrected / thumbs-down rate, containment/deflection. **Segment every
  metric by route/topic** — a blended average hides the broken minority route (same lesson as
  macro-vs-micro F1).
- **Release validation:** shadow (score new version on mirrored traffic, don't serve it) →
  canary (small % real users, watch leading indicators over a fixed window) → A/B (same online
  scorers on both arms). **Rollback triggers on sustained-window degradation, not single-sample
  noise.**

---

## 22. Annotation / trace-viewer UX (Wave 5) — the adoption engine

The operating-economics finding (§14) was "the error-analysis session is the adoption hook."
This is how to build the surface that makes it a habit — and it's cheap now ("hours with AI
assistance," which is what flips the ROI vs a vendor tool).

**What makes review ~10x faster (Hamel):** everything on ONE screen (no tab-switching between
trace, user data, and a labeling form), **one-keystroke binary pass/fail with a free-text
critique box right beside it**, hotkey navigation, and **domain-specific rendering** (rendered
SQL/query diff and chat bubbles, not raw JSON). Binary > Likert (reviewers can't distinguish a 3
from a 4; a 10pp pass-rate swing is interpretable, a 0.5-point Likert shift isn't).

**The workflow as a UI (open → axial coding):**
1. **Open coding:** show full trace + a single free-text box + next-hotkey. Force qualitative
   observation *before* categories exist.
2. **Axial coding:** cluster notes into a taxonomy (LLM-assisted), assign each trace ≥1 label,
   live count-per-category sidebar. Stop at ~20 traces with no new category; ≥100 to start;
   100+ fresh every 2–4 weeks + 10–20/week spot-check.

**Trace drill-down shows:** the span tree (router decision → tool call+args → result → answer),
**expected-vs-actual diff at the leaf that matters**, the **implicated prompt/surface** attached
to the failing span, per-stage latency+cost on each span, and a **deep-link from a failing metric
straight into the offending span**.

**Calibration UX:** 25–50 examples, judge's pre-label beside a blank human column, agree/disagree
+ critique-on-disagree, live TPR/TNR (not raw agreement). **EvalGen's rule:** the rubric-edit box
sits *next to* the grading action — editing the rubric IS part of labeling (criteria drift).

**What to build for this git-native plugin (hybrid):**
1. **Canonical storage = plain JSONL, one line per trace** (`{trace_id, label, category, critique,
   reviewer, ts}`) — diffs cleanly, PR-reviewable, mergeable. (JSONL over YAML — one changed line
   = one diff line, no re-indentation cascade.)
2. **A generated self-contained static HTML viewer** (`analyze` skill output) reading the run's
   traces + annotations: span tree, expected-vs-actual diff, per-stage latency/cost, hotkey
   pass/fail/critique, live taxonomy sidebar.
3. **Writes via a tiny `localhost` sidecar** spun up by the CLI, appending to the same JSONL the
   repo tracks — interactive session and git storage are one file.
4. **A TUI / slash-command mode** sharing the identical JSONL for calibration meetings.
5. **Don't build a custom web framework** — mirror Hamel's "Shiny-in-a-day" pattern; the whole
   value is that it's hours to build.

**Feed annotations back:** critiques as `<input><output><critique>` few-shots spliced into the
judge prompt (**+15–20% human-judge agreement**); every axial category mints a permanent
regression case; critiques seed synthetic generation; semantic search (Lilac-style) resurfaces
every trace in a newly-named category so one hand-labeled failure becomes a full frequency count.

---

## 23. Research status: COMPLETE (2026-08-02)

Five waves, twelve subagents, cross-checked against the existing plugin. **No finding changed a
core design decision — every wave sharpened *how*, not *whether*.** The design is de-risked as
far as research can take it. Coverage now spans: framework landscape, DeepEval, topology
detection, test generation, user simulation, optimization, data model, ground-truth mechanics,
Claude-as-judge, coding-agent-as-harness, .NET adapter, operating economics, rubric authoring,
the full metrics catalog, production/online eval, security/red-team, benchmark case design, and
annotation UX. **The remaining risk is entirely in execution. Next step is code, not research** —
start with the language-agnostic adapter contract + the execution-accuracy scorer (§16).
