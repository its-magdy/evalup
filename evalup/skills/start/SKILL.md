---
name: start
description: >-
  Guided entry point for evalup: detects the current state of a target
  app's eval setup (profile.yaml, datasets, pinned baseline) and routes the
  user to exactly the next step — discover, generate, run, analyze, or
  optimize (which it names; the user invokes that one) — telling them where
  they are ("step 2 of 5"). Use this whenever the user wants eval work DONE on
  an app: to begin or resume evaluating it, to set up evalup, to get test
  cases or a baseline for an agent or chatbot, to find out what is wrong from
  exported chats or logs, or when they type a bare /evalup:start. Prefer this
  over guessing a specific sub-skill when the state of the app's eval setup is
  unknown. For a QUESTION about evalup itself (what is it, which command, what
  does a term mean, why is something locked) use help instead.
argument-hint: "[path-to-app]"
allowed-tools: >-
  Read Grep Glob Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/*) Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*)
  Skill(evalup:discover) Skill(evalup:generate) Skill(evalup:run) Skill(evalup:analyze)
---

# evalup Start Wizard

> **Plugin root:** `${CLAUDE_PLUGIN_ROOT}`. Reference files and docs write that
> placeholder literally (it is only substituted here), so read every
> `${CLAUDE_PLUGIN_ROOT}/…` path you meet in them as this absolute path, and
> quote it in shell commands.
>
> **Calling the plugin.** Open its files with Read — never `cd` into the
> plugin or `cat` them. Run each script as its own Bash call, spelled
> `python3 "<that path>/scripts/<name>.py" …` with the path written out: no
> `cd`, no `&&` or `; echo $?` tail, no shell variable holding the path. The pre-approval matches
> that literal form only, and it lapses when the user next replies — a prompt
> after that is expected, not a fault. If a call is **denied**, stop and tell
> the user which permission is missing; never work around it by hand.
>
> **Every shell call, not only script calls.** One plain command per Bash
> call — no `cd`, `&&`, `|`, heredoc, `$( )` or `/tmp` — so an allow rule can
> match it. A denial of **any** call is a stop, never a retry in another form.

You are the wizard. Your job: figure out where the user is in the lifecycle,
do (or delegate) exactly the next step, and always end by telling them their
position ("step N of 5") and what comes next. Never dump all five steps of
work on them at once.

Arguments, when the user typed any: `$ARGUMENTS`

## First session — a result before any homework

A user with no state location has seen nothing yet, so nothing has earned an
interview, a patch to their source, or a vocabulary lesson. **The first
session's only goal is a first scored run and the top findings, fast.** Rigor
is what the second session is for, and the user asks for it by name. When check
1 below matches, run the steps in this shape, in ONE session, without stopping
between them:

0. **Ask one question first: "Do you have real conversations with this app —
   exported chats, support logs, saved traces, bug reports?"** Real failures
   beat imagined ones, and it is the one thing you cannot find in the code.
   **If the user's message already answers it, or says not to ask, do not
   ask** — take the answer and go: stopping ends the turn, this skill's
   pre-approvals end with it, and a headless session then cannot continue.
   **Yes →** run `analyze --transcripts <path>` FIRST (it needs no profile, no
   cases and no run): the failure taxonomy it produces is a result in its own
   right, and its conversations seed step 4 in place of synthetic cases. Then
   continue from step 1. **No →** carry on; say that synthetic cases test the
   failures you can imagine, and that real ones can be added any time.
1. **`discover`, lean.** Steps 1–4 and 8 of that skill only: access level,
   topology, code archaeology, the 1–3 live validation requests, findings.
   **Skip step 5 (patches), step 6's maturity question (default
   `stage: pre-stability`) and step 7 (the interview)** — record each under
   `deferred:` in findings.md so the next session offers them. The one thing
   you may need from the user is how to reach the app (URL, or a command that
   starts it); ask only that, and only if the code does not say.
2. **Say what this app can and cannot have measured — before spending their
   time.** One short paragraph from the capability matrix: "No traces, so I can
   score routing and answers today; tool use and cost need tracing, which I can
   add later if you want it." A user who came to evaluate tool calls must hear
   that in minute two, not after the run.
3. **Show the top 3 design findings now.** They are often worth more than the
   eval, they cost the user nothing, and they are the reason to keep going.
4. **`generate`, small: ~12 cases** (that skill's stated minimum), no holdout
   (it says to skip one below 25), smoke + canary only. If the user has real
   messages or transcripts to hand, take them first — 10 real ones beat 30
   synthetic. Review stays targeted: only the hard-negative/OOS labels and the
   cases the generator marked uncertain, in one batch, accept/fix/delete.
5. **`run --smoke`** — wait for the runner as run/SKILL.md §2 says
   (foreground, then `wait_run.py` while it exits 3; never end the turn while
   `results.json` says `running`) — then report per run/SKILL.md §5: what
   failed, where, and what the suite could not see.
6. **Close with the menu, not a lecture**: what unlocks next and what each
   costs — tracing (tool/trajectory/cost layers), the interview (business
   rules, rubric), a full suite with a sealed holdout (regression gating),
   judge calibration (judged layers, then `optimize`). One line each. Be
   straight about the distance to `optimize`: it needs ~100 cases, a sealed
   holdout and a stable app — several sessions away from a 12-case first run.

Vocabulary rule for this session: say "test cases", "a quick run", "checks that
could not run", not `split`, `selecting_split`, `capability matrix`,
`unscorable`. Introduce a term when the user first needs it, with its one-line
meaning. `${CLAUDE_PLUGIN_ROOT}/docs/concepts.md` has them all; do not recite it.

A user who asks for the full setup up front ("do it properly", a team with a
QA owner) gets the full `discover` and a ~30-case suite instead — say what the
lean path would have skipped and let them choose.

## Procedure

1. **Locate the target app.** Use `$ARGUMENTS` if given; otherwise ask for the
   app's repo path or endpoint URL. **Read and list only under the app path,
   its state location and the plugin.** Files the user did not name — the
   parent directory, sibling folders, notes and logs beside the app — are out
   of scope even when they look relevant; a file the user names in the
   request is in scope. Check for an existing state location
   (default `<app>/.evalup/`, or the adapter's `state_location`).
   Before anything is written, say where state will go (the default is
   `<app>/.evalup/`) and, in the same sentence, that a read-only repo, a
   change freeze or a dirty tree can keep it out of the app entirely: the
   adapter's `state_location` takes any external path, and discover runs
   without patching (`repo_access: read-only`). A statement, not a question
   — the first session stays interview-free; discover step 5 makes the
   offer when the tree is dirty or read-only.

2. **Route by state.** Run these checks **in order** and act on the first one
   that matches. The order is what makes the diagnosis correct: a state dir can
   satisfy several rows at once, and the cheap "nothing here yet" readings are
   the ones most likely to be wrong, so the checks that can *disprove* them run
   first. This is the same precedence
   `${CLAUDE_PLUGIN_ROOT}/skills/run/SKILL.md` §4 uses when it decides whether
   a run is a first run.

   | # | Check | Action if it matches |
   |---|---|---|
   | 0 | A `reports/<run-id>/` directory with a `manifest.yaml` but no `results.json`, or whose `results.json` has `summary.status: "running"` | **An interrupted run.** A killed session leaves exactly this. Say so, and offer `run --resume` under the SAME run id (run/SKILL.md §2) before anything else — routing on to a fresh run abandons the cases already paid for. If the user declines, leave the directory alone and continue down the table. |
   | 1 | No state location at all | Step 1: the first session, above — `discover` (load it and follow it), lean. |
   | 2 | State location exists but no `profile.yaml` | Treat as step 1 — an interrupted or partial setup. List what is already there and confirm before running `discover`, since you do not know whether it will reuse or overwrite an existing `adapter.yaml`. Don't promise the user it will be preserved — offer to back it up first if they care about it. |
   | 3 | `reports/baseline.json` exists (it names the pinned run-id) | Steady state: summarize current scores, dataset size, judge calibration status, and stage; suggest the most valuable next action (usually `analyze` on recent failures, or labeling if the judge is PROVISIONAL). Read the scores from that run's `reports/<run-id>/results.json` — the pointer file itself holds no verdict data. If that directory is missing, say the pinned baseline's run was deleted and offer to pin a fresh one, rather than silently reading some other run. Also compare the pointer's `dataset_version`/`harness_version` against the current dataset and harness: if they differ, say the baseline's scores are not comparable to a run today and a fresh pin is needed — `run` §4 will refuse the diff anyway, so surfacing it here saves the user a wasted run. |
   | 4 | Datasets exist, no baseline | Step 3: run `run` to establish the baseline. |
   | 5 | `profile.yaml` exists, no datasets | Step 2: run `generate`. |

   Independent of the above, if `profile.yaml` looks older than the app's recent
   git history suggests, offer `discover --diff` to detect staleness before
   doing any of the work you just routed to — stale profiles produce cases that
   test an app that no longer exists.

3. **State the stage.** Read the `stage` field from `profile.yaml` (or the one
   discover just wrote) and tell the user plainly what is unlocked and what is
   locked and why (`${CLAUDE_PLUGIN_ROOT}/docs/concepts.md` §Staged rigor has
   the stage table). Frame locks as "unlocks when X," never as missing features.

   Naming the stage is not enough on its own — "stage: pre-stability" means nothing
   to someone who has not read that table, so always spell out the one or
   two things it currently gates. Do this on every reply, including the ones
   that route to a blocked or broken state (check 2 and a dangling
   baseline pointer in 3). Those are precisely the moments a user is deciding
   how much more to invest, and the stage tells them what the work will buy.

4. **First-session extras** (only when the state location was just created):
   - Tell the user where everything was written and that it should be
     committed with the app (or versioned wherever it lives, if out-of-tree).
   - On the full setup, name the three roles (developer / reviewer / domain
     arbiter) and ask who will be the domain arbiter — record the answer in
     `profile.yaml` under `roles:`. On the lean first session this is one of
     the deferred interview questions, not a gate on the first run.
   - Estimate the time to first useful result honestly: minutes for the lean
     first session on a reachable app; about an hour for the full setup.

## Rules

- One step per session unless the user pushes to continue — **except the
  first session**, which runs discover → generate → run through to a result.
- Every reply ends with: current step, next command, and rough time/cost of
  that next step.
- If discover found design findings (e.g., "no out-of-scope route"), surface
  the top 3 immediately — they are often worth more than the eval itself.
