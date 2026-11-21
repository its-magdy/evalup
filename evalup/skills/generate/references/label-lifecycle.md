# After review: fixing a label, bumping the version, re-pinning

Read this when an **accepted** case looks wrong, when you edit any case
after its review, or when `reports/baseline.json` must change. Review
*before* acceptance is generate §3; this is what comes after.

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

**The bump rule.** Bump `dataset_version` by one for any edit that changes
what a case sends, expects, or where it counts: `input`, `expect`, `split`,
`gating`, `tags`, adding or deleting a case. A case moved to `gating: true`
(accepted) counts. An edit to **`notes` or `review` alone** (recording an
owner's confirmation, a reviewer's name) does not bump: the verdict means
the same thing. `run_history.py` will still flag that case's
`content_changed_at` once; say it was a note.

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

## Re-pinning

`baseline.json` is written by
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/pin_baseline.py reports/<run-id>`,
never by hand. It copies `run_id`, `dataset_version`, `scoring_version` and
`k` from the run's manifest and refuses (exit 2, nothing written) a run
whose status is not `ok` (`running` included), that fails `--verify`, that
selected `holdout`, or in which no case gates (`--allow-ungated` only on the
user's word). Quote every line of its `notes`.

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
