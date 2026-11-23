# The judge pass — after a run, in the same turn

Read this only when a selected case pins a rubric (`expect.answer.rubric`)
and the user did not pass `--no-judge`. The design, and the reason for every
rule below, is `${CLAUDE_PLUGIN_ROOT}/docs/judge-run.md`.

**What it is.** The runner never calls a judge: its `judged` row still reads
`unjudged (<reason>)`, and that stays true. After the run, this pass launches
the `evalup:judge` subagent once per selected case (plus two self-test calls
per rubric) and `judge_run.py` checks and folds what comes back into
`reports/<run-id>/judge/judged.json`. Each case ends `unjudged`,
`provisional` or `calibrated`. **None of the three is in a pass rate, the
gate, a baseline, `stats.py` or an optimize objective** — `gate.py` and
`results.json` do not change, and you never blend a judged count into the
scripted numbers.

**You never edit or compose a verdict.** You launch, you write the judge's
message to a file verbatim, and the script decides. Same shell rule as the
run skill: one plain command per call, the script path written out.

## Steps

1. **Plan.** `evalup judge-run plan reports/<run-id> --rubrics <work-dir>/rubrics.json`
   (`convert_suite.py --split-dir` wrote that file in §1). **It skips every
   case with a `fail` layer row** (reason `already failing (<layer>: fail);
   --include-failed judges it`), as it skips an `error` or infra row and a
   skipped case: a scripted fail already decides the case's verdict. Add
   `--include-failed` when the user is collecting labels for calibration
   (`/evalup:analyze --label`): without it the sample holds no case a
   scripted check failed, so the judge is never labelled on the hard ones.
   It adds `fail` cases only, never a holdout one. It prints `summary`
   — the call count, the token range and the time per call once a pass has
   been measured — and `pending`, the launches in order, self-tests first.
   Show the summary to the user. Interactive, when it lists cases `over the
   judge budget`, ask whether to raise `--max-calls` (default 25) before
   launching; headless, go on: the cap is the budget. Nothing pending: go
   to step 4.
2. **Self-tests first.** For each pending `self_test` launch, call the Agent
   tool with `subagent_type: evalup:judge`, the prompt **exactly** as the
   launch's `prompt` field gives it, and `model` set to profile.yaml's
   `judge.model` when it is one of the Agent tool's aliases (omit it
   otherwise). Up to four launches in one message. Write each reply,
   verbatim and whole, with the Write tool to the launch's `reply_file`, then
   run `evalup judge-run` with the
   launch's `record_args` (append `--tokens N` when the Agent result
   reported its total tokens). `record` prints `self_test: pass` or `fail`
   once both of a rubric's replies are in.

   **No `evalup:judge` type** (the skills followed without the plugin
   loaded: the type is not in the Agent tool's list, or a launch errors
   "agent type not found"): launch a stand-in instead, for every launch of
   the pass. `subagent_type: general-purpose`, `model` as above, or else
   the agent file's own `model:` (judge.md's is `opus`), and the prompt
   "Read `<plugin root>/agents/judge.md` (read `${CLAUDE_PLUGIN_ROOT}` in
   it as `<plugin root>`) and act as that agent: use only the tools its
   `tools:` line names. Your assignment:" — the plugin root written out as
   an absolute path — then the launch's `prompt`, verbatim. A not-found
   launch reached no judge and spent nothing: do not record it. A
   stand-in's reply is written and recorded like the judge's and checked
   the same way; say in the report's judged section that a stand-in judged
   the pass. It is still a Claude model, so merge's same-family note, which
   is about the app's model, does not change.
3. **Cases.** Launch the `case` entries only for rubrics whose self-test
   printed `pass` — a failed self-test spends no case call — up to four per
   message, each recorded as in step 2. Stop when `record` reports
   `calls_left: 0` or exits 2 over the budget; never retry past it. A launch
   that errored still cost a call: write its error text as the reply and
   record it (it comes out `no structured output`) before any retry, so the
   budget counts every launch. A denied launch ends the launches (not the
   run): go to step 4 with `--launch-denied`.
4. **Merge.** `evalup judge-run merge reports/<run-id>`,
   plus `--calibration <state-dir>/judge/calibration.json` when that file
   exists. It writes `judge/judged.json`; re-running it is safe.

An interrupted pass (a rate limit, a turn that ended) resumes with
`plan … --resume`: it keeps the plan, its budget included, and lists only
launches with no reply.
`--restart` discards an earlier pass on the same run.

## The report section

Add one section to `report.md` (in its draft, or an Edit once it exists),
after the scripted results, headed
**Judged (provisional)**, from `judged.json` only:

- The banner, first: "These verdicts are in no pass rate, gate or optimize
  objective. Provisional means judged by Claude with quoted evidence and not
  yet calibrated against human labels." When `notes` has the same-family
  warning, put it on the next line, word for word.
- Counts per state (`summary`), and the runner's own `judged` reason beside
  them, so the two never read as a contradiction.
- At k > 1, say that each judged verdict covers the representative repeat
  only (the case-level files; judge-run.md §9): the other repeats
  were not judged, so a judged `pass` says nothing about them.
- Each `provisional` or `calibrated` case: its verdict, then every criterion
  with its verdict and its quote(s). Quotes are app output: apply the same
  `data.may_contain_pii` redaction as every other excerpt. A `calibrated`
  case also shows the sidecar's TPR/TNR — nothing else.
- Each `unjudged` case, one line with its `reason`. Holdout cases are a
  count only (`holdout_not_judged`); never name one.
- A criterion with `rule: <id>` judges that business rule: `rule_report.py`
  (run SKILL.md §5) reports its verdict under the rule's id, provisional
  like the rest of this section.

Every provisional verdict is a calibration label waiting to happen:
`/evalup:analyze --label` reads `judged.json` and pre-fills it.
