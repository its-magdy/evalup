# After review: fixing a label, bumping the version, re-pinning

Read this when an **accepted** case looks wrong, when you edit any case
after its review, when `reports/baseline.json` must change, when a human
wants to accept many cases at once, or when the owner is not in this
session (a relay, headless) or canaries are to be chosen. Review *before* acceptance is generate
§3; this is what comes after.

## What the hash and `dataset_version` cover

- **The case hash** (`manifest.yaml`'s `cases[].sha256`) is the SHA-256 of
  the whole case as `convert_suite.py` read it: **every field, `notes` and
  `review` included**. Nothing is left out, so a note-only edit changes it.
- **`dataset_version`** is the integer in `datasets/dataset.yaml`. No script
  derives it from the hashes: `make_plan.py` copies it into the plan, and
  the baseline diff refuses a pair whose two values differ. It never refuses
  on a hash.
- **`run_history.py`** is the one reader of the hashes: the same case id with
  a different hash inside one `dataset_version` is reported as
  `content_changed_at`.

**The bump rule.** Bump `dataset_version` by one for an edit to any case
field except `notes` and `review` (`input`, `expect`, `split`, `gating`,
`unit`, …), and for adding or deleting a case. Accepting a case flips
`gating`, so it bumps. An edit to **`notes` or `review` alone** (recording
an owner's confirmation, a reviewer's name) does not bump: the verdict means
the same thing. `run_history.py` still reports that case once, as
`content_changed_at` with a `content_changed_warning` that says to bump.
For a note-only edit that warning does not apply: say it was a note.

## A proposed fix waits in `label_fixes:`

A fix you propose, or one the owner approved but nobody has applied yet,
goes in `datasets/dataset.yaml`, never only in chat (the next session cannot
read your last reply):

```yaml
label_fixes:
  - case: c-3f9a2c1d
    status: pending            # pending | approved | applied | rejected
    change: "drop answer.must_not_match ^I can't: it matches the app's correct refusal"
    evidence: reports/regression-20261120T101500Z/cases/c-3f9a2c1d/
    proposed: { by: trace-analyzer, at: 2026-11-20 }
    decided: { by: <owner>, at: 2026-11-20 }   # once approved or rejected
    applied_in: 4              # the dataset_version that carries it
```

`validate_cases.py --manifest` checks each entry (a known `case`, a legal
`status`, `applied_in` on an applied fix) and lists the open ones (pending or
approved) as `label_fixes_open` in its summary. Read that list at the start
of a session and say what waits. The arbiter is the owner. Never apply a
`pending` fix yourself.

## Correcting an accepted label

1. A failure on an accepted case may be the label's error: show the owner the
   expected-vs-actual and the proposed change, and record it as `pending`.
2. On the owner's yes, edit the case's `expect` (or `input`). **`review`
   stays `accepted`** with its original `by`/`at`. Append a dated line to
   `notes`: `2026-11-20: label fixed (<what>), approved by <owner>`.
3. Bump `dataset_version`, set the entry to `applied` with `applied_in`, then
   convert and validate (run §1).
4. The pinned baseline is now on the old version and no diff will pair with
   it. Re-pin (below), and say so.

Several fixes from one review sitting go in **one** bump, not one each.

## When the review changes a case's `covers`

A case's `notes` may say what it covers or why a rule is "not confirmed, not
in covers". When a rule's confirm / fix / strike (or a label fix) changes
`covers` or `expect.answer.rules`, refresh or drop those `notes` lines in the
same edit: a stale one contradicts the case. A `notes`-only edit does not
bump `dataset_version` (above).

## Accepting in bulk

Generate §3 sends a human the few cases worth a look. Optimize needs more:
about 50 usable training cases, each human-accepted with `gating` true
(optimize SKILL.md, precondition 2). Between the two, a human may accept a
batch in one decision, and the decision stays theirs:

1. **List the batch** in chat: per case its id, the message, the expected
   label and a one-line why. Leave out every `holdout` case (reading one
   unseals it), every canary, every conversation (`input.turns`: it stays
   `gating: false` until a baseline exists and the user promotes it) and
   every case still `quarantined`: those go through §3 one by one. Mark
   each case covering a rule that is not `confirmed` (or listing it in
   `expect.answer.rules`): it may be accepted but stays `gating: false`,
   since the validator refuses it gating (`unconfirmed_rule_gates`).
2. **The human answers in this session**, in their own words: "accept all",
   or "accept all but c-…, c-…". A yes relayed by another agent, or one you
   infer from silence, accepts nothing. Headless there is no such answer.
3. **Write each accepted case**: `review: {status: accepted, by: <their
   name>, at: <date>}` and, unless step 1 marked it, `gating: true` in the
   same edit. A case they
   excluded stays `pending`. Then **one** `dataset_version` bump for the
   batch, convert and validate (run §1), and re-pin (below).

Say in the handover how many were accepted this way. A bulk accept is a
lighter review than §3's, so a wrong label found later is fixed as above
(Correcting an accepted label), not by un-accepting the batch.

## The owner is not in this session

Read this when the owner's answers reach you through someone else: a
coordinating agent that drives the skills, a delegate, a brief or message
that quotes the owner (verbatim or not), or a headless run. Each is a
relay, and a relay accepts no case, confirms no rule and chooses no canary
(#114). Quoting it more exactly changes nothing.

**What you do.** Stop the review. Write no `accepted`, no `confirmed`, no
`canary` tag and no owner's name: leave every case and rule as it is. Then
tell whoever you report to that the sign-off is the owner's, name the two
routes below, and list, per case and per rule, exactly what the owner would
write (the shapes below, with their name and date left for them). Carry on
with what needs no sign-off; a run on `pending` cases is still a run.

**Route 1: the owner edits the state themselves**, by hand or with a
script they run themselves (an agent's write in their name is the relay
again). Per case they accept, in its case file:

```yaml
review: { status: accepted, by: <their name>, at: <YYYY-MM-DD> }
gating: true        # leave false if a rule it covers is still unconfirmed
```

A case they reject: fix it, or delete the file. A `quarantined` case is
accepted the same way. Per canary they choose (an accepted case only, never
a conversation or a `holdout` case): add `smoke` and `canary` to its
`split`, e.g. `split: [full, smoke, canary]`. Per rule they confirm, in
profile.yaml's `business_rules`:

```yaml
status: confirmed
confirmed_by: <their name>
```

A struck rule is deleted, with a dated line in findings.md. Then one
`dataset_version` bump in `datasets/dataset.yaml`, then
`evalup convert-suite` and `evalup validate-cases` as run §1 spells them.
The bump, the convert and the validate sign
nothing, so you may run them once the owner says the edit is done; then
re-pin (below).

**Route 2: the owner runs the review in their own Claude Code session**
in the app's repo (`/evalup:generate` §3, or §Accepting in bulk above),
answering in person. That session writes the fields above itself.

**What the validator accepts.** `review.status` is `accepted`, `pending` or
`quarantined`. `review.by` and `confirmed_by` must be a person's name or
handle, a non-empty string. Refused, case by case: empty or null; a
generator's stamp (starting `generate`, `test-generator`, `auto`,
`machine`); and any whole word naming an agent, a model or a relay
(`agent`, `assistant`, `bot`, `ai`, `llm`, `model`, `evalup`, `discover`,
`system`, `delegate`, `relayed`, `proxy`, `on behalf`, `subagent`,
`claude`, `gpt`, `gemini`, `fable`, …). A refused `review.by` on an accepted
case warns `machine_accepted`; a refused `confirmed_by` warns
`rule_confirmed_unattributed` and the rule is read as inferred, so a
gating case or a canary covering it is an ERROR (`unconfirmed_rule_gates`).
Nothing reads `review.at`; write the date anyway.

## Choosing canaries is the owner's

A canary's `fail` aborts the run, so the tag acts as a gate: choosing one
is the owner's, like accepting a case. You propose candidates (generate §3
lists them with the review batch); only the owner adds `canary`, in this
session or by their own edit (Route 1). No script records who chose a
canary, so this rule is yours to keep.

## A sealed holdout case, before its first look

A sealed case is never read, so a check defect a training review found can
sit in it too (a sibling template). While the ledger
(`datasets/holdout-looks.jsonl`) has **no line** for this seal, and the
noise pair's runs are lines, the fix is a recorded pre-look fix; after
that, it is `analyze --unseal` and a reseal. Never edit a sealed case by hand.

1. State the fix as a generic transformation on `expect.*` (never `input`),
   in words that name no holdout case, and make it by hand on the training
   case first, through its review, until that case is `accepted`.
2. Dry-run it, show the owner the output, and on the owner's yes apply it:
   `evalup prelook-fix <state-dir> --transformation <fix.json>
   --motivated-by <training id>`, then the same with
   `--apply --approved-by <owner>`. It applies the fix to every case it
   matches, in every split, and refuses once the ledger has a line or
   `reports/` holds a holdout run at this `dataset_version`. It rewrites
   only each case's `expect:` block, so a diff never shows an input.
3. It prints ids and hashes, never case text, and records each changed case
   in `dataset.yaml`'s `prelook_fixes:` (id, old and new hash, date,
   approver, transformation, motivating case) with one `dataset_version`
   bump. Then convert, validate and re-pin, as below.

The hash is the run's: `manifest.yaml`'s `cases[].sha256`. The seal's
templates (`templates/ho_*`) are sealed with it:
`evalup prelook-fix <state-dir> --list` names them, and the sealed cases,
by id and hash. Never open one, and never pass one to a reviewer, the
judge or a report.

## Re-pinning

`baseline.json` is written by
`evalup pin-baseline reports/<run-id>`,
never by hand. It copies `run_id`, `dataset_version`, `scoring_version` and
`k` from the run's manifest into `baseline.json` beside the run, and refuses
(exit 2, nothing written) a run whose status is not `ok` (`running`
included), that has scorer errors, fails `--verify`, is not a `regression`
run, or in which no case gates (`--allow-ungated` only on the user's word).
A gating case is not proof of review, since the run does not record
`review.status`, so check for an accepted case yourself. Quote every line of
its `notes`. It cannot see `dataset.yaml`: pin a run made on the current
`dataset_version`.

- **After a bump**: run `regression` *without* `--baseline-verdicts` (a diff
  across versions is refused), then pin that run. It replaces the old pointer,
  and the script names the one it replaced.
- **A red run** (gate closed) can be pinned: a diff measures change, not
  health. Pin it when the user chooses to, and say the baseline is red. The
  script's note names the gate's reasons.
- **k carries over.** The pointer's `k` is every later diff's `k`: pass
  `--k <its k>` to `make_plan.py`, or the runner refuses the pair. A pin at
  `--k 1` (an app whose rate limit cannot take k=3) means every regression
  diff runs at `--k 1` until a k=3 run is pinned.
