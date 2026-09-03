---
name: start
description: >-
  Guided entry point for agent-eval: detects the current state of a target
  app's eval setup (profile.yaml, datasets, pinned baseline) and routes the
  user to exactly the next step — discover, generate, run, analyze, or
  optimize — telling them where they are ("step 2 of 5"). Use this whenever
  the user wants to begin or resume evaluating an LLM app, asks to set up
  agent-eval, says they are unsure what to do next, mentions eval setup /
  baseline / test cases for an agent or chatbot, or types a bare
  /agent-eval:start. Prefer this over guessing a specific sub-skill when the
  state of the app's eval setup is unknown.
argument-hint: "[path-to-app]"
---

# agent-eval Start Wizard

You are the wizard. Your job: figure out where the user is in the lifecycle,
do (or delegate) exactly the next step, and always end by telling them their
position ("step N of 5") and what comes next. Never dump all five steps of
work on them at once.

## Procedure

1. **Locate the target app.** Use `$ARGUMENTS` if given; otherwise ask for the
   app's repo path or endpoint URL. Check for an existing state location
   (default `<app>/.agent-eval/`, or the adapter's `state_location`).
   Read-only app repos are supported: state can live at any external path, and
   discover runs without patching — say so if the user mentions access limits.

2. **Route by state.** Run these checks **in order** and act on the first one
   that matches. The order is what makes the diagnosis correct: a state dir can
   satisfy several rows at once, and the cheap "nothing here yet" readings are
   the ones most likely to be wrong, so the checks that can *disprove* them run
   first. This is the same precedence `skills/run/SKILL.md` §4 uses when it
   decides whether a run is a first run.

   | # | Check | Action if it matches |
   |---|---|---|
   | 1 | No state location at all | Step 1: run the `discover` skill now (load it and follow it). |
   | 2 | State location exists but no `profile.yaml` | Treat as step 1 — an interrupted or partial setup. List what is already there and confirm before running `discover`, since you do not know whether it will reuse or overwrite an existing `adapter.yaml`. Don't promise the user it will be preserved — offer to back it up first if they care about it. |
   | 3 | `reports/baseline.json` absent, but a sibling `baselines/` or `runs/` directory exists | **Pre-per-run layout: this dir DOES have a baseline.** Say so and point at `docs/migrate-run-layout.md`. Never report it as "no baseline run yet" — that is the misdiagnosis this ordering exists to prevent, and acting on it would abandon real eval history. |
   | 4 | `reports/baseline.json` exists (it names the pinned run-id) | Steady state: summarize current scores, dataset size, judge calibration status, and stage; suggest the most valuable next action (usually `analyze` on recent failures, or labeling if the judge is PROVISIONAL). Read the scores from that run's `reports/<run-id>/results.json` — the pointer file itself holds no verdict data. If that directory is missing, say the pinned baseline's run was deleted and offer to pin a fresh one, rather than silently reading some other run. Also compare the pointer's `dataset_version`/`harness_version` against the current dataset and harness: if they differ, say the baseline's scores are not comparable to a run today and a fresh pin is needed — `run` §4 will refuse the diff anyway, so surfacing it here saves the user a wasted run. |
   | 5 | Datasets exist, no baseline (and check 3 did not match) | Step 3: run `run` to establish the baseline. |
   | 6 | `profile.yaml` exists, no datasets | Step 2: run `generate`. |

   Independent of the above, if `profile.yaml` looks older than the app's recent
   git history suggests, offer `discover --diff` to detect staleness before
   doing any of the work you just routed to — stale profiles produce cases that
   test an app that no longer exists.

3. **State the stage.** Read the `stage` field from `profile.yaml` (or the one
   discover just wrote) and tell the user plainly what is unlocked and what is
   locked and why (see help skill's "Staged rigor"). Frame locks as "unlocks
   when X," never as missing features.

   Naming the stage is not enough on its own — "stage: invariant" means nothing
   to someone who hasn't read the help skill, so always spell out the one or
   two things it currently gates. Do this on every reply, including the ones
   that route to a blocked or broken state (checks 2, 3, and a dangling
   baseline pointer in 4). Those are precisely the moments a user is deciding
   how much more to invest, and the stage tells them what the work will buy.

4. **First-session extras** (only when the state location was just created):
   - Tell the user where everything was written and that it should be
     committed with the app (or versioned wherever it lives, if out-of-tree).
   - Name the three roles (developer / reviewer / domain arbiter) and ask who
     will be the domain arbiter — record the answer in `profile.yaml` under
     `roles:`.
   - Estimate the time to first useful result: "~1 hour to your first routing
     confusion matrix" for a typical white-box app.

## Rules

- One step per session unless the user pushes to continue.
- Every reply ends with: current step, next command, and rough time/cost of
  that next step.
- If discover found design findings (e.g., "no out-of-scope route"), surface
  the top 3 immediately — they are often worth more than the eval itself.
