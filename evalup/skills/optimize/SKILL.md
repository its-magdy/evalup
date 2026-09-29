---
name: optimize
description: >-
  Improve the app's prompts and tool descriptions through a failure-driven
  loop: reflect on failure clusters, propose one targeted edit, measure on the
  training split, confirm on the sealed holdout with a statistical gate, keep
  or revert. Use when eval scores have plateaued or a failure cluster
  implicates a specific prompt or tool description.
argument-hint: "[--surface <prompt-id|tool>] [--budget $N]"
disable-model-invocation: true
allowed-tools: >-
  Read Grep Glob Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/*) Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*)
---

# Optimize — The Reflective Loop

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

User-invoked only (`disable-model-invocation`): this skill edits the user's
prompts and spends sealed holdout looks, so it starts when they type
`/evalup:optimize`, never because another step decided it was time. Other
skills may recommend it; none may launch it.

Methodology = GEPA's discipline implemented natively: natural-language
reflection on trajectories, a candidate pool with per-case scores, a sealed
holdout, a hard budget. See
[references/loop-discipline.md](references/loop-discipline.md) for the rules that
make gains real instead of overfit.

## Preconditions — check, and refuse with the unlock path if unmet
1. `judge.status: calibrated` — if any objective dimension is judge-scored.
   An uncalibrated judge as an optimization target produces a confidently
   worse app. Check the flag's **evidence**, not the
   flag: `paths.judge_calibration`'s sidecar (`score_agreement.py`) is what
   derives it, and a `calibrated` profile with no sidecar behind it is a
   hand-set flag — the runner already refuses it.
2. Usable case count ≥ ~100 for judged objectives (deterministic-only
   objectives may proceed at ≥ ~50 with Bayesian gating).
3. `stage:` ≥ stable (set by `discover` §6) — below that, route-target and
   tool boundaries are still moving, so a measured gain is indistinguishable
   from drift.
4. White-box prompt access (gray-box → recommendations only, written to
   findings).
5. A budget (`--budget` or ask).
6. The edit can reach the running app. Read `app.repo_access` in
   `adapter.yaml`: `read-only` → recommendations only, written to findings,
   no edit applied — the same outcome as gray-box. Then, per surface, read
   `findings.md` and the adapter's `prompts[]`: a surface whose edit only takes
   effect after a rebuild or restart (compiled-in or embedded prompts — e.g.
   `kind: tool-descriptions` on a `.cs` path;
   `${CLAUDE_PLUGIN_ROOT}/skills/discover/references/adapters/dotnet.md`
   §Optimizable surfaces) is allowed, but step 4's measure waits on the
   user. No adapter field records this; if neither file says, ask before the
   first edit.

## The loop (one iteration per session unless told otherwise)
1. **Ground first, edit never-first**: read the latest `analyze --cluster`
   output. Pick ONE cluster. Read its actual traces. Name the metric contract
   being violated and the surface implicated. If evidence is ambiguous between
   surfaces, say so and stop — more cases beat guessed edits.
2. **Choose the lever — full set, effort-tagged:**
   - tool descriptions (cheapest, empirically the biggest lever — start here
     when tool-selection errors exist; 3–4 sentences, when-to-use and
     when-NOT-to-use, unambiguous arg names),
   - system prompt sections, few-shot examples,
   - tool consolidation / route-target boundary / model tier / added OOS route —
     these are code or architecture changes: PROPOSE with evidence and effort
     tag, do not implement inside the loop.
3. **Propose ONE candidate** (one surface, one coherent change). Write it to
   `candidates/<id>/` with: the diff, the reflection (why this failure ←
   this cause ← this fix), predicted affected cases.
4. **Make the edit revertible BEFORE you make it.** You are about to change
   the user's prompts and then run a suite — long enough for your memory of
   the original text to be summarized away. Revert is never a re-edit from
   memory:
   - Run `git -C <app> status --porcelain -- <surface file(s)>`. **If a
     surface file has uncommitted changes, stop and say so**: a later
     `git checkout` would destroy the user's own work along with your edit.
     Ask them to commit or stash it, or to let you work on a copy.
   - Record the pre-edit blob in `candidates/<id>/before.sha`
     (`git -C <app> rev-parse HEAD:<path>`), one line per file.
   - **Not a git checkout?** Copy each file you will touch to
     `candidates/<id>/before/` first, and restore from there.
   - Revert is `git -C <app> checkout -- <path>` (or the copy back), then
     confirm `git hash-object <path>` equals the recorded blob. Say which
     files you restored.

   **Then measure**: apply the edit and run the training split — that is
   `/evalup:run --regression` (whose selection is every case whose `split`
   field contains `full`, which by construction excludes the sealed holdout —
   the two splits are mutually exclusive), or `--targeted --tag <component>`
   when the edit is scoped to one surface and you want the faster loop — under
   a new manifest. Compare paired per-case vs current champion.

   **Rebuild-bound surface (precondition 6)?** Between applying the edit and
   running anything, stop: ask the user to rebuild and restart the instance
   at `invocation.base_url` and to confirm the running build carries the edit
   — and again after any revert. Never measure, and never spend a holdout
   look, on an edit nobody has confirmed is live: an unchanged binary
   measures as "no gain", and noise can spend a look on it.
5. **Gate on holdout**: only if training looks positive, run the sealed
   holdout (aggregate) via `/evalup:run --holdout`. That run spends one of
   the N=5 looks and records itself in the holdout-look ledger (see
   `${CLAUDE_PLUGIN_ROOT}/skills/run/SKILL.md` §4) — which is why step 4's
   training measurement is a precondition, not a formality. `stats.py` decides
   on one rule at every n: exact Bayesian P(improvement) ≥ 0.8
   (`--bayes-threshold`) and delta > 0, with an exact one-sided sign test
   reported alongside. Two caveat keys ride beside that decision and both must
   be surfaced rather than reported as a bare "kept": `gate_note` (kept on the
   posterior bar while the sign test is not significant) and `sub_mde_keep`
   (the observed delta is smaller than the run's own minimum detectable
   effect — the direction is evidenced, the SIZE is not, so do not quote the
   delta as a measured improvement). Pass → keep: leave the edit in the working
   tree, log the candidate as champion, and pin the keeper run by writing
   `reports/baseline.json` as exactly `{"run_id", "dataset_version",
   "harness_version"}` — copy the last two from that run's `manifest.yaml`,
   never from memory (a partial object silently breaks every later diff).
   Fail → revert as step 4 says, verify the blob, keep the reflection (it
   prunes the next hypothesis). Always report regressions on any layer, not just the
   target metric.
6. **Stop conditions**: budget spent; two consecutive candidates rejected on
   the same cluster (→ the lever is probably wrong — escalate to a code/
   architecture proposal); or the honest message "these route targets are
   inherently confusable — no prompt fixes this" with the evidence.

## Candidate pool
Keep rejected-on-holdout-but-won-somewhere candidates in the pool with their
per-case score vectors (Pareto idea: complementary strengths may merge into a
later candidate). Pool is for generating better candidates — keep/ship
decisions come ONLY from the holdout gate. Holdout looks are counted; after 5,
analyze forces a reseal.

## Human gate
Every kept edit is presented as a diff with its evidence before it is left in
the working tree. Nothing is committed by this skill. Same-family-judge mode
(if accepted in profile) is restated in every kept-edit summary as a known
bias on the numbers.
