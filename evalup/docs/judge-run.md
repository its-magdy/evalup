# The run-time judge — design

**Status:** written 2026-11-21 for issue #23 and built in the same issue's
code PR; §19 lists where the build settled a detail this design left open.
The issue body's "Changes after two
independent reviews" list is the brief, and every item on it is honoured
here: three states, one judge call per case behind an estimate, a grader
self-test, no Rogan–Gladen correction, no CI gating until a live calibration
is measured, and yes/no criteria as the v1 rubric form. One item of the
issue's "keep" list is met differently: the resolved model id cannot go in
`manifest.yaml`, which the runner writes before any judge runs and nobody
rewrites, so it goes in the judge pass's own manifest (§7, open question 8).
The runner's §5.6 comparability key stays judge-free on purpose. The proof on
a live app is #27.

Today every judged layer is `unjudged (<reason>)` and nothing reads a rubric
at run time (`runner-contract.md` §5.4). This note is the shape of the pass
that judges after a run, the files it writes, and the walls that keep its
verdicts out of every number a decision is made on.

**What does not change.** `run_cases.py` stays deterministic and Claude-free:
it does not call the judge, its `judged` row and reason strings stay as they
are, `verdict.json` is never rewritten, and `verdicts_for_stats.jsonl` is
still built from the runner's own case verdicts only. No scorer changes, and
`SCORING_VERSION` stays 1. `gate.py`, `stats.py` and `run_history.py` do not
read anything this pass writes in v1. The README's "CI can gate without
Claude" stays true.

## 1. Three states

Every case that pins a rubric (`expect.answer.rubric`) ends the pass in
exactly one state:

| State | When | Shown | Counts in `stats.py`, `gate.py`, optimize |
|---|---|---|---|
| `unjudged` | no judge pass ran; the case was not selected (§5); the rubric failed its self-test (§3); the judge said `unknown`, refused, or returned output that fails §4's checks; the model id could not be resolved (§7) | the reason, one line | never |
| `provisional` | judged, every check in §4 held, but §6's calibration conditions do not all hold | the verdict, per-criterion verdicts and the quoted evidence, under a PROVISIONAL banner | never |
| `calibrated` | judged, and §6 holds: a calibration record for the **same rubric id, rubric version and resolved model id** | the same, plus the sidecar's TPR/TNR | never in v1 (§8); a later, opt-in gate only |

A `provisional` verdict is the normal outcome for every user for a long
time: calibration needs ~30 labels to discover criteria and then 100–200
stratified ones (`rubric-format.md` §"Calibration workflow"). That is why it
is shown at all, with its evidence, rather than hidden as `unjudged` — and
why it is walled off from every decision (§8).

The case's judged verdict is a fold over its criteria, first match wins,
applied only to a reply that matched its assignment (§4: a reply that did
not is `unjudged` as a whole, before the fold):

1. any criterion `fail` that passed §4's checks → `fail`;
2. every criterion `pass`, each passing §4's checks → `pass`;
3. otherwise → `unjudged`, reason naming the criteria that came back
   `unknown` or failed a check.

An observed failure settles the case, as in the runner's pass^k fold
(contract §5.5 rule 1). `unknown` is never a pass and never a fail.

## 2. The v1 rubric form: yes/no criteria

A v1 rubric is a flat list of yes/no questions, all graded in one call.
The DAG form (`TaskNode` / `BinaryJudgementNode` / `GEvalNode`) stays the
advanced form for `analyze --label`; a case that pins a DAG rubric is
`unjudged ("DAG rubrics are not judged at run time in v1")`. The file keeps
`rubric-format.md`'s place, ids, versioning and frontmatter; `form: criteria`
replaces `nodes:` with `criteria:`.

```yaml
---
rubric_id: grounded-answer
version: 1
form: criteria                     # v1 run-time form; absent = the DAG form
last_calibrated: null
calibration_notes: >
  v1: written for the quickstart's invoice case.
status: { calibrated: false }      # record-keeping, as today; §6 decides

criteria:                          # each one a yes/no question; id = the
  - id: states_the_amount          # node_id calibration labels use
    question: "Does the answer state the amount in the reference result?"
    reference_guided: true         # the judge is handed expect.result
  - id: states_a_period
    question: "Does the answer say which period the amount covers?"
  - id: period_is_asked_one
    question: "Is the period the answer names the one the user asked about?"
  - id: no_unsupported_claim
    question: "Is every figure in the answer present in the app's response
               data (response.json body), with nothing added?"

self_test:                         # §3: graded before any case is
  good:
    user: "what did I spend on invoices in May 2026?"
    answer: "You spent $412.50 on invoices in May 2026."
    response: { value: 412.5 }
    reference: { scalar: 412.5 }
  bad:
    user: "what did I spend on invoices in May 2026?"
    answer: "You spent $412.50 on invoices in April 2026, and $90 on fees."
    response: { value: 412.5 }
    reference: { scalar: 412.5 }
    fails: [period_is_asked_one, no_unsupported_claim]
---
```

Rules the code PR's linter (`validate_cases.py`, rubric files) enforces:

- `criteria` is 1–12 entries; ids are snake_case and unique; a `question`
  is one question (a criterion that can fail for either of two reasons is
  `rubric-format.md`'s double-barreled node — split it).
- `self_test.good` and `self_test.bad` are both present; `bad.fails` names
  at least one criterion id, each one real. A rubric without a self-test is
  a linter ERROR for `form: criteria`.
- A criterion is never a substring or regex check; that belongs in
  `answer.must_contain` (`rubric-format.md` §"Business-rule vs judged
  split"), and the linter cannot tell, so `generate` and the doc say it.
- `canary_guard` keeps its meaning: a criterion marked `canary_guard: true`
  is judged under `agents/judge.md` rule 6 when the case's
  `expect.authz.expect_refusal` is true.

## 3. The grader self-test (#29 item 1)

Before any case on a rubric is judged, the judge grades the rubric's
`good` answer and its `bad` answer, as two separate calls (one call with
both would let the judge grade by contrast). The self-test passes when the
good answer comes back `pass` on every criterion and the bad answer comes
back `fail` on every criterion in `bad.fails`, each verdict passing §4's
checks against the self-test's own inputs.

**A missing or unchecked self-test reply is a failed self-test**, and the
skill launches no case call on a rubric until both of its self-test replies
are recorded and `judge_run.py record` has reported them passing, so a
failed self-test spends no case calls. `plan` materialises each answer as
`selftest/<rubric_id>/<good|bad>/` holding `request.json`, `answer.txt`,
`response.json` and `expect.json` (reference only), the same file names a
case has, so §4's checks apply to it unchanged.

A rubric whose self-test fails sends every case on it to
`unjudged ("self-test failed: <criterion> graded <verdict> on the <good|bad>
answer")`. It runs once per pass, not once per rubric version, because the
model behind an alias can move between passes. Cost: two calls per rubric
used by the run.

The self-test is what the issue asks for instead of 100 labels to start. It
does not certify a judge (two answers are not a calibration); it blocks a
judge that cannot tell its own rubric's obvious cases apart, before that
judge's verdicts are shown to anyone.

## 4. One call per case, and what the call returns

**One judge call per selected case grades all of the rubric's criteria**,
never one call per criterion per case per repeat. A subagent launch cost
~17–35k tokens in the user tests; per-node launches over a 100-case run
would be millions. The calling skill launches `evalup:judge` once per case
with an assignment that names files, not pasted text:

- the user message(s): `cases/<id>/request.json`'s body, and for a
  conversation every `turns/<t>/request.json` (§9);
- the answer: `cases/<id>/answer.txt`;
- the app's own data: `cases/<id>/response.json`, and `trajectory.json`
  when the case has one;
- the reference, for `reference_guided` criteria: `cases/<id>/expect.json`;
- the rubric file, its id and version.

`agents/judge.md` gains a v1 section for this form (the same rules 0–10,
one verdict per criterion instead of one node) and the reply contract
below. Its rule 0 already restricts it to the named paths. Rule 1 keeps
reasoning first, so the message is prose followed by one JSON object, and
`record` extracts **the last JSON object in the message** (a top-level
`{…}` that parses strictly with `_common.loads_strict`); a message with no
such object is `unjudged ("no structured output")`. `model_id` is copied
from the judge's own environment, the line Claude Code's system prompt
gives a subagent stating its exact model id; a judge that has no such line
writes `"unknown"` (§7).

```json
{"schema": "evalup/judge-reply/1",
 "model_id": "claude-opus-5-5",
 "case_id": "c-4f2a91c7", "rubric_id": "grounded-answer", "rubric_version": 1,
 "criteria": [
   {"id": "states_the_amount", "verdict": "pass",
    "reasoning": "The reference scalar is 412.5; the answer states $412.50.",
    "evidence": [{"source": "answer.txt", "quote": "You spent $412.50"}]},
   {"id": "states_a_period", "verdict": "unknown",
    "reasoning": "...", "evidence": []}]}
```

`judge_run.py record` (§5) checks each reply before it counts. **A
criterion verdict survives only if every check holds**, and a failed check
turns that criterion into `unknown` with the check's name as its reason:

- **whole-reply checks, before the fold:** `schema`, `case_id`,
  `rubric_id` and `rubric_version` match the assignment, and the criterion
  ids are exactly the rubric's, each once. Any mismatch — a missing, extra
  or duplicated id included — makes the whole case `unjudged ("reply did
  not match the assignment")`, never a fold over the criteria that parsed;
- `verdict` is `pass`, `fail` or `unknown`; any other value is `unknown`;
- **every `pass` or `fail` quotes at least one span, and every quote is
  found in the file it names.** The source must be one of the assignment's
  app-side files (`answer.txt`, `response.json`, `trajectory.json`, a turn's
  `request.json`/`answer.txt`); `expect.json` is the reference, not
  evidence about the app. Matching is after whitespace collapsing and NFC
  normalisation; a quote containing `[REDACTED <kind>]` (judge rule 3)
  matches when its pieces appear in order. This is the deterministic half
  of "evidence-citing": a quote the judge invented, or the calling skill
  altered while recording it, fails here.

The skill hands the reply to the script verbatim; the script decides. The
skill never edits a verdict or composes one.

## 5. The pass, step by step, and where it lives on disk

The pass runs **after** the run, in the run skill, in the same turn (headless
one-turn, §10). One new CLI, `scripts/judge_run.py`, stdlib-only, does every
deterministic step; the skill does only the launches.

1. **`judge_run.py plan reports/<run-id> --rubrics <state>/rubrics`**
   selects the cases, snapshots each rubric (id, version, sha256 of the
   file bytes), checks the self-test blocks, prints the estimate (§11) and
   writes `judge/plan.json`. Exit 0 with nothing to judge is normal.
2. The skill launches the self-test calls, records them (step 3), and only
   then one call per selected case on a rubric whose self-test passed, up to
   four in one message.
3. The skill writes each judge message, verbatim, to
   `judge/inbox/<case-id>.txt` (a self-test's as
   `selftest-<rubric_id>-<good|bad>.txt`) with the Write tool, then runs
   **`judge_run.py record reports/<run-id> --case <id>|--self-test <rubric>:<good|bad> --reply-file <that path>`**,
   which extracts and checks the reply (§4), stamps it with the plan's
   rubric sha256, and writes it under `judge/replies/`. A re-record of the
   same case replaces its file (last wins, as a resumed pass needs).
4. **`judge_run.py merge reports/<run-id> [--calibration <sidecar>]`**
   applies §3, the fold in §1 and §6, and writes `judge/judged.json`.
   It is rerunnable and derived from `plan.json` + `replies/` only, like
   `verdicts.jsonl` from the case dirs (contract §9(a)). A reply whose
   stamped sha256 differs from the plan's snapshot is `unjudged ("reply
   judged a different rubric file")`, and `plan --resume` never rewrites a
   snapshot that already has replies.

**Default selection, on the layers, not the rollup.** A case is judged when
it pins a rubric, has an answer, and none of its layer rows is `fail`,
`error`, `infra_error` or `infra_incomplete`: its scripted layers passed or
could not check the answer, so a judge verdict is new information. Reading
the rollup is not enough: a layer `error` rolls the case up `unscored` above
a sibling `fail` (`run_cases.py`'s rollup), and a scorer error is a number
nobody should quote. A `fail` case is already failing; `infra_*` and
`skipped` cases have no answer to judge. The others are listed in
`plan.json` as `unjudged` with that reason.
`--include-failed` adds the `fail` cases (useful when labelling, since a
calibration sample oversamples failures). Holdout cases are never selected
(§9), `--include-failed` or not.

The files, all under the run directory, none of them the runner's:

```
reports/<run-id>/
  judge/
    plan.json                     # judge_run.py plan: selection, rubric snapshots, estimate
    selftest/<rubric_id>/good/    # the self-test's inputs, materialised as files
    selftest/<rubric_id>/bad/     #   so the judge reads paths and §4 can check quotes
    inbox/<case-id>.txt           # the judge's message, verbatim, as the skill wrote it
    replies/<case-id>.json        # one checked reply per judged case
    replies/selftest-<rubric_id>-<good|bad>.json
    judged.json                   # judge_run.py merge: the pass's one result file
```

`judged.json`, schema `evalup/judged/1`:

```jsonc
{"schema": "evalup/judged/1",
 "judge_version": 1,                       // _common.JUDGE_VERSION: this pass's own rules (§7)
 "run_id": "smoke-20261121T101500Z",
 "judge": {"requested_model": "opus",      // the alias the skill launched with
           "resolved_model_id": "claude-opus-5-5",   // §7
           "same_family": "yes"},          // yes | no | unknown (§12)
 "rubrics": {"grounded-answer": {"version": 1, "sha256": "…", "form": "criteria",
                                  "self_test": "pass"}},
 "calibration": {"sidecar": "judge/calibration.json", "sha256": "…"},   // or null
 "cases": [{"case_id": "c-4f2a91c7", "rubric_id": "grounded-answer",
            "pinned_version": 1, "graded_version": 1,
            "state": "provisional", "verdict": "pass",
            "not_calibrated_because": "no calibration for grounded-answer v1 on claude-opus-5-5",
            "criteria": [ /* the checked reply's criteria */ ]}],
 "summary": {"selected": 1, "unjudged": 0, "provisional": 1, "calibrated": 0,
             "by_verdict": {"provisional": {"pass": 1, "fail": 0},
                            "calibrated": {"pass": 0, "fail": 0}}},
 "cost": {"calls": 3, "wall_s": 41.0, "tokens": null}}   // tokens when the launches reported them
```

`judge/` is outside the runner's tree on purpose: contract §6 lists the
files the runner writes and `--verify` checks those, the same way
`report.md` sits beside them unlisted. The code PR adds a test that
`--verify` and `gate.py` pass byte-identically with a `judge/` directory
present.

## 6. When a verdict is `calibrated`

A judged case is `calibrated` only when **all** of these hold, and the first
that fails is written as `not_calibrated_because`:

1. the calibration sidecar exists, parses, and is schema
   `evalup/judge-calibration/2` (below); a `/1` sidecar has no rubric
   version or model id, so it backs nothing at run time;
2. it holds a `calibrated` entry for this rubric id **and** this rubric's
   file `version` **and** `judged.json`'s `resolved_model_id`;
3. the case's `pinned_version` (its `-v<N>`) equals its `graded_version`,
   the file's `version` (a stale pin is judged on the file's current
   version, but cannot be calibrated);
4. the rubric's self-test passed in this pass;
5. `same_family` is `no`, or it is `yes` and profile.yaml's
   `judge.same_family_accepted` is `true`. `unknown` fails this condition
   with `app model undeclared`: a Claude app whose model never reached the
   manifest must not skip the check.

**The sidecar grows two keys** (`score_agreement.py --write`, schema `/2`):
annotation rows gain `rubric_version` and `judge_model_id`, and both join
**the dedupe key** (today `(rubric_id, node_id, case_id or trace_id)`,
latest wins) **and the group key** (today `rubric_id` alone). Without the
first, a later label on a new model would replace the earlier pair and the
sidecar could never hold both. Each `(rubric_id, rubric_version,
judge_model_id)` group is scored with the existing floors (TPR and TNR ≥
0.90, κ ≥ 0.60, ≥ 100 pairs, ≥ 10 per class). The label flow copies both
fields from `judged.json` — `graded_version`, never the pin — when it
pairs a human label with a judged verdict (§13). Rows without them are
counted as today and calibrate nothing at run time. `/2` keeps the
top-level `status` and `rubrics_measured` the runner's
`check_judge_calibration` already reads, so `/1` and `/2` both serve it; it
only picks a reason string.

No Rogan–Gladen correction in v1: at ~100 labels its interval is too wide
to report. A `calibrated` verdict shows the sidecar's TPR/TNR beside the raw
count, and nothing else.

## 7. The model id, and the judged comparability key

The skill launches the judge with an **alias** (profile.yaml's
`judge.model`, falling back to the agent's `model: opus`); the Agent tool
takes aliases only. The alias is what the run's `manifest.yaml` can record,
because the runner writes the manifest at pre-flight, before any judge runs
— so `manifest_extra.judge.model` carries the alias, and the resolved id
lives in `judged.json`, the judge pass's own manifest.

**Resolving it:** each reply carries `model_id`, copied from the judge's
system prompt (§4), never inferred from the alias. `merge` requires every reply of the pass,
self-tests included, to report the same non-empty id matching
`^claude-[a-z0-9.-]+$`. A missing id, or two ids in one pass, makes every
case `unjudged ("judge model unresolved" | "judge model changed during the
pass")`. A self-reported id is weaker than an API-reported one; open
question 1 says why it is still the v1 choice and how #27 checks it.

**The key.** `_common.judge_key(judged)` is the one comparability rule for
judged verdicts, like `scoring_key` for the runner's:
`(judge_version, resolved_model_id, sorted (rubric_id, version, sha256))`.
`judge_run.py diff <judged A> <judged B>` compares two passes: it refuses
(exit 2, `{"error": …}`, naming the field) when the judge keys differ, when
the two runs' `scoring_key`s or `dataset_version`s differ, or when either
side is not `evalup/judged/1`. When it is legal it reports, per state, the
counts on each side and the cases whose judged verdict changed — counts
only, never a `keep`, never a p-value. So a model change behind the alias,
or any edit to a rubric file, refuses the diff: a judged series cannot drift
silently.

`JUDGE_VERSION` is bumped when a rule in §1, §4 or §6 changes what a judged
verdict means, by the same discipline as `SCORING_VERSION`
(`CONTRIBUTING.md`). It is a separate number because the runner's verdicts
do not move when the judge's rules do.

## 8. The walls: no judged verdict reaches a decision in v1

`provisional` must never gate, feed `stats.py` or feed an optimize
objective. In v1 the same holds for `calibrated`, because no live
calibration has been measured yet (issue item 5). The walls are structural,
not a flag someone could flip:

- **`stats.py`** reads `verdicts_for_stats.jsonl`, which the runner
  regenerates from the case directories (contract §9(a)). The judge pass
  writes nothing the runner reads and never touches that file, `verdicts.jsonl`,
  `results.json` or any `verdict.json`. The runner's `comparison.json` is
  computed before the pass exists.
- **`gate.py`** reads `results.json` and `--verify`. It does not open
  `judge/`. Its "judged layers: unjudged (…)" line keeps reading the
  runner's reason.
- **optimize** keeps precondition #1 and its refusal of judged objectives
  (`optimize/SKILL.md`). The code PR adds one sentence there: `judged.json`
  is never an objective, a keep signal or a reason to revert. Its holdout
  gate is `stats.py --noise-floor`, which the first wall already covers.
- **`run_history.py`** keys and counts on manifests and `results.json`; it
  does not read `judge/`.
- **CI** never runs the pass: it needs Claude. CI's gate is unchanged.

The later, opt-in gate (not v1): `gate.py --judged <judged.json>` closing
on a `calibrated` `fail` of a `gating: true` case, once #27 has measured a
live calibration and the owner has decided its bar. Nothing in v1 reads a
`calibrated` verdict to decide anything; it is a label on a report.

## 9. Conversations, repeats, canaries, holdout, modes

- **Conversations.** The judge reads every sent turn's `request.json` and
  `answer.txt`. Criteria apply to the case-level `answer.txt`, which is the
  deciding turn (contract §6) and for a selected conversation the last one,
  unless a question names an earlier turn. The runner's
  `final_turn_certifies` is a rollup rule and plays no part in the judge's
  fold. A conversation that stopped early has a `fail` or `infra_*` row and
  is not selected by default.
- **Repeats (`k > 1`).** The case-level files are the representative
  repeat (contract §6), and that is what is judged: one call per case, not
  per repeat. Judge disagreement across repeats is not measured in v1.
- **Canaries.** A canary that pins a rubric is judged like any case and
  counted apart. A canary judged `fail` sends every case on its rubric to
  `unjudged ("judge canary <case-id> failed")`, naming the canary so its
  reply can be found. Unlike the runner's exit 4 it voids only that
  rubric's judged verdicts: the runner's exit, `gate.py` and the scripted
  counts stay as the runner wrote them. The self-test is the canary that
  always runs.
- **Holdout.** Never selected: `unjudged ("sealed holdout")`. Judging a
  sealed case would put its answer text and quotes into a report, and the
  seal's point is that nobody reads them (contract §10). A holdout run's
  judged layer stays as the runner wrote it.
- **Modes.** The pass runs in every mode, `smoke` included — the quickstart
  is a smoke run. The runner's row still reads `unjudged (mode: smoke)`:
  that string says the runner did not judge, which stays true. The report
  shows the runner's reason and the pass's counts side by side.

## 10. Headless: one turn

The run skill does the whole pass in the turn that ran the suite: run
(or `wait_run.py`), `judge_run.py plan`, the launches, `record` per reply,
`merge`, then the report. Every script call is a pre-approved
`evalup *` command, one plain command per
call: the run skill forbids a heredoc or a pipe, so the reply reaches
`record` as `--reply-file`, a file the skill writes under
`reports/<run-id>/judge/inbox/` — a write the README's headless recipe
(`--permission-mode acceptEdits`) already covers, like `report.md`. The
one new grant is the subagent launch: run's `allowed-tools` gains
`Agent(evalup:judge)` (open question 3). A denied launch ends the pass, not
the run: every case is `unjudged ("judge launch denied")`, `merge` still
writes `judged.json`, and the report says so.

`--no-judge` skips the pass; a run with no rubric case prints nothing about
it. A run that outlives its turn and comes back through `wait_run.py` is
judged in the turn that sees it finish.

## 11. Cost, printed before the first call

`plan` prints, and writes into `plan.json`:

```
judge pass: 6 cases pin a rubric; 4 selected (2 not: 1 fail, 1 sealed holdout)
  calls: 4 cases + 2 self-test (1 rubric) = 6
  tokens: ~120k–230k (17–35k per launch, measured in the 2026-09 user tests,
          + ~2k input (this run's named files, bytes ÷ 4) + ~1k output per call)
  time: not yet measured (the last pass's judged.json records it)
```

The per-launch range is the user tests' measured subagent overhead; the
input term is the bytes ÷ 4 of every file each assignment names, turn
files and `trajectory.json` included, summed per call — never a fixed
figure, since a conversation or a long trace is most of it. Once a pass has run, `plan`
reads the previous `judged.json` beside it and prints that pass's measured
`wall_s` per call, and its tokens when the launches reported them.

**A budget, not only an estimate.** `--max-calls` (default 25) caps the
pass, counting **every launch**: self-test calls (spent first) and every
retry included. Past the cap nothing is retried. Interactive, the skill shows the estimate and asks before going over
it. Headless, it judges the first selected cases in plan order up to the
cap, and the rest are `unjudged ("over the judge budget: --max-calls 25")`.
The default is open question 2. Launches go at most four to a message, so a
rate limit stops a batch, not a hundred; a limit mid-pass leaves the
recorded replies in place, and rerunning the pass judges only cases with no
reply (`plan --resume`).

## 12. The same-family warning

`same_family` is `yes` when any string leaf of the app's declared model
(`manifest.yaml`'s `app.model`, a string or a dict of strings) contains
`claude` after case-folding, `no` when every declared
value names another family, and `unknown` when the app declares none. The
report says it on the first judged line whenever it is not `no`: "the app
and its judge are both Claude: a judge passes its own family's failing
outputs more often (Panickssery et al. 2024, arXiv 2508.06709, arXiv
2604.06996); measured for this app only once labels exist." A Claude Code
subagent cannot leave the Claude family (`agents/judge.md`), so the warning
is the remedy available, plus §6's condition 5.

## 13. The report, and labels

The run skill's `report.md` gains one section, **Judged (provisional)**:
counts per state, then each judged case with its verdict, each criterion's
verdict and its quotes, under a banner that these verdicts are in no pass
rate, gate or optimize objective. It never blends a judged count into the
scripted pass rate. Quotes pass the same `data.may_contain_pii` redaction
as every other excerpt.

**Every provisional verdict is a calibration label waiting to happen.**
`analyze --label` reads `judged.json` and pre-fills each criterion's
`judge_label`, with `rubric_id`, `node_id` (the criterion id),
`rubric_version` (the case's `graded_version`) and `judge_model_id`; the
human's call fills `label`. **When a case has a recorded reply, `--label`
copies it and launches no second judge**: another launch would cost
17–35k tokens per criterion and would label a different judge's verdict.
**Agreements are recorded too, not only disagreements**: a sample of only
the cases a human contested would make TPR and TNR meaningless. How labels
are reviewed and pinned over time is #53's; this note fixes only the fields
a judged verdict carries into a label.

## 14. The quickstart's rubric case

The quickstart's `profile.yaml` lists `grounded-only` as the rule that
"needs a judge". The code PR closes that row:

- `.evalup/rubrics/grounded-answer.md`: §2's file, verbatim.
- `c-4f2a91c7` (the invoices happy case) gains
  `expect.answer.rubric: grounded-answer-v1`. It keeps its scripted layers,
  which pass, so it is selected by default. Its `gating` and its runner
  verdict do not change: the runner's `judged` row reads
  `unjudged (mode: smoke)` as it would for any smoke run.
- `examples/quickstart/README.md`'s rules table: `grounded-only` → covered
  by `c-4f2a91c7`, judged, provisional.
- `demo.py --judge-replies <dir>` feeds committed fixture replies through
  `record` and `merge`, so `tests/test_example.py` shows a provisional
  `pass` with its quote end to end without Claude. The live half — the
  same run headless in one turn with a real judge — is checked by hand once
  and recorded in CHANGELOG.

## 15. What the code PR changes

- `scripts/judge_run.py` (new CLI: `plan`, `record`, `merge`, `diff`), so 22
  CLIs; `_common.JUDGE_VERSION` and `_common.judge_key`.
- `scripts/score_agreement.py`: sidecar `/2` (§6); reads `rubric_version`
  and `judge_model_id` from rows.
- `scripts/run_cases.py`: `check_judge_calibration` accepts `/2`. Nothing
  else in the runner.
- `scripts/validate_cases.py`: rubric files of `form: criteria` (§2).
- `agents/judge.md`: the criteria form and the reply contract (§4); its
  "a run never launches it" becomes true of the runner only.
- `skills/run/SKILL.md`: the pass (§10) in as few lines as it takes,
  with the detail in a reference read only when a selected case pins a
  rubric (the first-session budget, `tests/test_context_budget.py`).
  `allowed-tools` gains `Agent(evalup:judge)`.
- `skills/analyze/SKILL.md` `--label`: pre-fill from `judged.json` (§13);
  `skills/analyze/references/annotation-ux.md`, whose "no artifact holds a
  judge verdict" stops being true.
- `skills/optimize/SKILL.md`: §8's sentence.
- The docs #8 corrected, now describing the real behaviour:
  `run-modes.md`, `concepts.md` §"The judge", `runner-contract.md` §5.4,
  `rubric-format.md` (the criteria form), `case-format.md`'s `rubric:`
  comment, the README's judged lines, `profile-schema.md`'s `judge.model`
  comment.
- The quickstart (§14); CHANGELOG. `SCORING_VERSION` stays 1.

## 16. Acceptance tests the code PR needs

From #23, plus the ones this design's walls owe:

1. **`unknown` ⇒ `unjudged`.** A reply with every criterion `unknown`,
   one with a single `unknown` and the rest `pass`, a refusal (no JSON
   object), and a reply that fails strict JSON all leave the case
   `unjudged`, never `pass` or `fail`. A single `fail` with valid evidence
   beside an `unknown` is `fail`. A reply with a missing, an extra or a
   duplicated criterion id, or a wrong `schema`, is `unjudged` even when
   every criterion present is `pass`.
1a. **A real message parses.** A fixture whose bytes are reasoning prose,
   then the JSON object, then a trailing line, records; one with two
   objects records the last.
2. **No judged verdict moves a decision.** The same run directory, with and
   without a `judge/` holding a `fail` on a `gating: true` case, run twice —
   once with `state: "provisional"`, once with `state: "calibrated"`:
   `gate.py`'s exit code and output, `verdicts_for_stats.jsonl`,
   `results.json`, `--verify` and `stats.py`'s output on the runner's
   `verdicts_for_stats.jsonl` are byte-identical. The same for a judge
   canary `fail`.
3. **A model-id change refuses the diff.** Two `judged.json` alike but for
   `resolved_model_id`: `judge_run.py diff` exits 2 naming
   `resolved_model_id`. The same for a rubric's `version` and its `sha256`.
4. **The self-test blocks a judge that passes its known-bad answer.** A
   fixture reply grading `bad` `pass` on a `fails` criterion: every case on
   that rubric `unjudged ("self-test failed: …")`, and none provisional.
   A **missing** self-test reply does the same.
5. **Evidence is checked.** A `pass` whose quote is not in `answer.txt`, a
   quote citing `expect.json`, and a `pass` with no quote each make that
   criterion `unknown`. A quote with a `[REDACTED …]` piece matches.
6. **Mixed model ids in one pass** ⇒ every case `unjudged`.
7. **Calibrated needs all five conditions** (§6): one test per condition
   failing alone, each leaving the case `provisional` with that reason.
8. **Holdout is never judged**, and no holdout case id appears in
   `judged.json` outside the count.
9. **Selection:** cases with a `fail`, `error` or `infra_*` layer row, and
   `skipped` cases, are not selected by default (an `unscored` rollup over
   a layer `error` included); `--include-failed` adds `fail` only, and never
   a holdout case.
9a. **A stale reply.** A reply stamped with another rubric sha256 is
   `unjudged`; a stale pin records `pinned_version` ≠ `graded_version` and
   stays `provisional`.
10. **`--max-calls`**: over the cap, the extra cases are `unjudged` with the
    budget reason, in plan order; self-test calls and retries count.
11. **Quickstart end to end without Claude** (§14), plus the existing
    `validate_cases.py --strict` 0/0 over the example with the rubric file.
12. **Plugin layout:** run's `allowed-tools` lists `Agent(evalup:judge)`;
    the first-session budget test still passes.
13. **Live, by hand, once:** the quickstart headless in one turn shows a
    provisional verdict with a quote. Recorded in CHANGELOG; #27 repeats it
    on a real app with a calibration.

## 17. Open questions for the owner

Each has a recommended answer; the code PR follows the recommendation
unless the owner says otherwise.

1. **Subagent in the session, or `claude -p` per case?** `claude -p --bare`
   is reproducible and reports the exact model id, but needs
   `ANTHROPIC_API_KEY`; without `--bare` it loads the user's CLAUDE.md,
   hooks and plugins, and a nested `claude -p` may be refused in auto mode
   (README §Permissions). **Recommended: the subagent, with the
   self-reported id checked across the pass (§7).** It needs no key, and
   the plugin already runs the judge this way for `--label`. #27 checks the
   self-report against `/status` once; if they ever disagree, `-p --bare`
   becomes the opt-in for runs that need the stronger id.
2. **The default call budget.** **Recommended: `--max-calls 25`** (~0.5–0.95M
   tokens at the measured launch cost), raised by flag; #27 measures a real
   pass and the default moves to what it shows.
3. **`Agent(evalup:judge)` in run's `allowed-tools`.** Without it a headless
   launch may be a silent denial; with it, a run skill can start a model
   call the user did not name. **Recommended: add it**, because the estimate
   prints first, `--no-judge` turns the pass off, and the budget bounds it.
   The reply file is written under `acceptEdits` (§10); §16 test 13 proves
   the launch and the write headless.
4. **Business rules (`expect.answer.rules`).** **Recommended: they stay
   `unscored` in v1.** A rule that needs a judge becomes a criterion in a
   rubric; a checkable one becomes `must_contain` / `must_not_contain`
   (#52 did that for the prompt's rules). One judge call per rule would undo
   §4's one-call budget.
5. **How `gate.py` treats judged layers at first.** **Recommended: not at
   all in v1** (§8), including `calibrated`. After #27 measures a live
   calibration, `gate.py --judged` as an opt-in, decided then with its
   numbers in hand.
6. **Judge the `fail` cases by default?** They are what a calibration sample
   needs most, but they cost calls and cannot change a verdict that already
   failed. **Recommended: no; `--include-failed` when labelling.**
7. **A stale case pin (`-v1` against a `version: 2` file).** **Recommended:
   judge on the file's version, never `calibrated`, and say so** (§6
   condition 3), rather than `unjudged`: every rubric edit would otherwise
   blank the judged layer until every case is re-pinned.

8. **Where the resolved model id lives.** #23 says "in the manifest". The
   runner writes `manifest.yaml` at pre-flight and §5.4 says its artifacts
   stay as written (resume compares its `plan_sha256`), so a later write
   into it would make the runner's record depend on a model call.
   **Recommended: `judged.json` is the judge pass's manifest and holds it;
   `manifest.yaml` holds the alias**, and `judge_key` (§7), not the
   runner's `scoring_key`, is the comparability key it enters.

## 18. Not in v1

- Any gate on a judged verdict (§8); any optimize objective on one.
- Rogan–Gladen correction and its interval.
- Judging the DAG form at run time; `GEvalNode` scores at run time.
- Judge disagreement across repeats; per-repeat judging.
- A cross-family judge (a Claude Code subagent cannot leave the family).
- `run_history.py` series over judged verdicts.
- Judging holdout cases.

## 19. As built: details the design left open

- **Rubric files reach the pass as JSON.** Every script but
  `convert_suite.py` is stdlib-only, so it parses the frontmatter
  (`--rubrics-dir`; a state dir with `rubrics/` also gets a `rubrics` key and,
  under `--split-dir`, `rubrics.json`). `plan --rubrics` takes a directory
  (it runs `convert_suite.py --rubrics-dir`, as `--probe` does with the
  adapter) or that JSON; the run skill passes `<work-dir>/rubrics.json`, the
  run's own parse. The rules of §2 live in `scripts/_rubric.py`, shared by
  the linter and `plan`.
- **The judge never reads the rubric file.** `plan.json` carries each
  launch's prompt, with the criteria inline and the case's files listed.
  A self-test's `case_id` and directory carry a random label
  (`selftest-<rubric_id>-<6 hex>`), never `good` or `bad`; the mapping
  lives only in `plan.json`, which no assignment names (rule 0 is what keeps
  the judge out of it). `record --self-test RUBRIC:good|bad` and `pending`'s
  `side` are for the skill; the labels are drawn in order, never sorted.
  A quote's unredacted text holds at least two letters or digits, and one
  redaction stands for at most 4000 characters of the file (a PEM key,
  not a stretch of it); the match is linear, never backtracking.
- **The reply scan is §4's, with one consequence stated:** a `{` that does
  not parse is skipped by one character, so a whole reply wrapped in a
  broken outer object is still found, and must pass every check. Skipping a
  broken object's balanced extent instead lost real replies whose prose
  held an odd quote mark.
- **Flags §5 did not name:** `plan --resume` (keep the plan and its
  budget, list launches with no reply; a different `--max-calls` is exit 2) and `--restart` (discard an earlier pass; a plain `plan`
  over existing replies is exit 2), `record --tokens N`, `merge
  --launch-denied`. `record` counts each launch in `judge/launches.jsonl`
  and refuses one past `--max-calls`; `plan` pays for a rubric's self-test
  only when at least one of its cases fits under the cap.
- **The sidecar's `/2` shape:** the `/1` keys, with `rubrics_measured`
  still bare rubric ids, plus `groups`, one entry per `(rubric_id,
  rubric_version, judge_model_id)`; that is what `merge` matches.
- **§6 condition 4 never fails alone:** a rubric whose self-test failed
  leaves its cases `unjudged` before §6 is asked, so §16 test 7 checks it
  as `unjudged`, not `provisional`.
- **`cost.wall_s`** is the time from `plan` to the last recorded launch.
- **Canaries are counted apart**: `summary.canaries` holds their own state
  counts, and `summary`'s other counts are the non-canary cases only.
- **Sealed holdout cases' rubric pins are not read**: `holdout_not_judged`
  counts every holdout case of the run, whether it pins a rubric or not.
- **The quickstart's fixture app** returns `invoices: 4` beside the total,
  so the answer's "across 4 invoices" is grounded and the case's
  provisional `pass` is the right verdict.
