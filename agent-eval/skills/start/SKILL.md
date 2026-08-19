---
name: start
description: >-
  Guided entry point for agent-eval. Detects the current state of the target
  app's eval setup and walks the user to the next step ("you are at step 2 of
  5"). Use when the user wants to begin evaluating an app, is unsure what to do
  next, or asks to set up agent-eval.
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

2. **Route by state:**

   | State found | Action |
   |---|---|
   | No state location found | Step 1: run the `discover` skill now (load it and follow it). |
   | `profile.yaml` exists, no datasets | Step 2: run `generate`. |
   | Datasets exist, no baseline run (no `reports/baseline.json`) | Step 3: run `run` to establish the baseline. |
   | No `reports/baseline.json`, but a `baselines/` or `runs/` directory exists | Pre-per-run layout: this dir DOES have a baseline. Say so and point at `docs/migrate-run-layout.md` — never report it as "no baseline run". |
   | Baseline exists (`reports/baseline.json` names the run-id) | Steady state: summarize current scores, dataset size, judge calibration status, and stage; suggest the most valuable next action (usually `analyze` on recent failures, or labeling if judge is PROVISIONAL). Read the scores from that run's `reports/<run-id>/results.json`. |
   | `profile.yaml` older than the app's recent git history suggests | Offer `discover --diff` to detect staleness before anything else. |

3. **State the stage.** Read the `stage` field from `profile.yaml` (or the one
   discover just wrote) and tell the user plainly what is unlocked and what is
   locked and why (see help skill's "Staged rigor"). Frame locks as "unlocks
   when X," never as missing features.

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
