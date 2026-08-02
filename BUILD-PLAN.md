# Build Plan — agent-eval plugin (execution + integration wave)

Source of truth for design: `EVAL-DESIGN-RECOMMENDATION.md` (23 sections). This file
is the coordination contract for the parallel build agents: shared vocabulary +
strict file ownership so no two agents edit the same file (the repo is NOT git —
overlapping writes race and corrupt).

Plugin root: `/path/to/workspace/agent-eval/`

## Canonical vocabulary — every agent MUST use these exact names

- **The tool is GENERAL.** Core (scorers, judge, stats, skills) is language-agnostic and
  MUST NOT branch on language. Only the per-target **adapter** knows the stack. .NET is the
  first reference adapter, never the scope.
- **Verdict taxonomy:** `pass | fail | unscored | infra_error | infra_incomplete`.
  `unscorable` checks are reported but NEVER counted as `fail` (an unread result scored as a
  mismatch is a manufactured failure). infra_* never counts in pass/fail denominators.
- **Scorer output shape (stdlib-only Python, one per layer):**
  `{"layer": <name>, "verdict": ..., "checks": [...], ...}`. Error contract: on malformed
  input print `{"error": "..."}` to stdout and exit 2; otherwise exit 0. Every scorer answers
  `--version` via `_common.add_version_flag` and `--help` cleanly. Read `scripts/_common.py`
  and `scripts/score_answer.py` before writing any scorer; match their style and comment
  density (comments explain WHY an edge case is handled).
- **Eval layers:** answer · routing · tool-selection · args · trajectory · **execution**
  (new, `score_execution.py`, `expect.result`) · **authz** (new, `score_authz.py`,
  `expect.authz`) · cost/latency · judged.
- **`expect.result`** (already built + documented in case-format.md): exactly one of
  `scalar` | `rows`, plus `ordered`/`columns`/`float_tolerance`/`reference_query`.
- **`expect.authz`** (NEW — schema all agents must use):
  ```yaml
  authz:
    allowed_record_ids: [e_881]        # IDs the caller's scope MAY see (from identity)
    forbidden_record_ids: [e_412]      # IDs that MUST NOT appear in answer or tool results
    forbidden_tools: [approve_swap]    # privileged tools this identity must not invoke
    expect_refusal: false              # true = the correct behavior is a scoped refusal
  ```
  Scored deterministically against the trace's tool-call log + returned record IDs, NOT an
  LLM read of the chat text (a prompt-only "refusal" can hide an open endpoint underneath).
- **Run modes:** `smoke | regression | targeted | holdout | full` (§7 of the recommendation).
- **Adapter capabilities:** invoke · traces(+convention/shim) · content-capture · optimizable
  surfaces · seed/reset/snapshot · **oracle** (read-only data access) · tool-catalog+side-effect
  class (safe-live/needs-mock/never-live) · access level (white/gray/black) + safe-to-attack.
- **Judge:** decomposed-binary DAG, reference-guided, evidence-citing, `PROVISIONAL` until
  calibrated (TPR/TNR + Cohen's κ, never raw accuracy). Cross-family judge = an external
  **script**, never a subagent (subagents can't leave the Claude family).
- **Staged rigor:** `pre-stability → stable → traffic → production`.
- **Ground truth (execution layer):** runner computes `expected` offline from a reference query
  against a seeded fixture / read-only oracle; extracts `actual` from the trace/tool-result;
  `{"missing": true}` → unscorable, never fail.
- **DO NOT build:** reasoning/thinking-trace quality GATING (diagnostic-only), same-family judge
  panels, string-overlap correctness metrics (BLEU/ROUGE/BERTScore), deep chaos/red-team
  integration (doc pointer only).

## Rules for every agent
1. Edit ONLY the files in your ownership list. Do NOT touch any other file — another agent owns
   it. If you feel you need a file you don't own, reference it by path with a `see <path>`
   pointer instead of editing it.
2. Read `EVAL-DESIGN-RECOMMENDATION.md` (the cited sections for your task) and the existing
   files you own before changing them. Match existing tone/conventions.
3. Keep the tool general — never hardcode .NET/Python specifics into a core script or skill.
4. If you add/edit a scorer or stats script, run `python3 -m unittest discover -s tests` from
   the plugin root and confirm green before finishing.
5. Update/extend the MD docs you own so they stay accurate. Do not update help/SKILL.md or
   README.md — those get a final consolidation pass by the coordinator.
6. Report back: files changed, what you added, and anything the coordinator must reconcile.

## File ownership map (no overlaps)

- **A1 Adapter contract + .NET adapter** — OWNS: `skills/discover/references/adapter-contract.md`
  (extend), NEW `skills/discover/references/adapters/dotnet.md`. NOT discover/SKILL.md,
  NOT profile-schema.md.
- **A2 Judge + DAG rubric** — OWNS: `agents/judge.md`, NEW `docs/rubric-format.md`. NOT
  analyze/SKILL.md, NOT docs/workflow.md, NOT help.
- **A3 stats hardening** — OWNS: `scripts/stats.py`, `tests/test_scorers.py` (sole owner this
  wave). NOT other scorers.
- **A4 authz scorer** — OWNS: NEW `scripts/score_authz.py`, NEW `tests/test_authz.py`. NOT
  test_scorers.py, NOT case-format.md (A6 adds the expect.authz doc block per the schema above).
- **A5 run skill** — OWNS: `skills/run/SKILL.md`, NEW `skills/run/references/run-modes.md`. NOT
  scorer scripts, NOT adapter-contract.md, NOT case-format.md.
- **A6 generate skill + case format** — OWNS: `skills/generate/SKILL.md`,
  `skills/generate/references/case-format.md`. Adds the `expect.authz` block (schema above) +
  canonical case schema fields (§19) + generate-with-non-app-model rule + small-budget geometry.
- **A7 discover skill** — OWNS: `skills/discover/SKILL.md`,
  `skills/discover/references/profile-schema.md`. NOT adapter-contract.md, NOT adapters/.
- **A8 analyze + review UX + report tooling** — OWNS: `skills/analyze/SKILL.md`, NEW
  `scripts/md_to_html.py` (+ tiny test in `tests/test_md_to_html.py`), NEW annotation-viewer
  generator script + its reference doc under `skills/analyze/references/`. NOT help, NOT README,
  NOT docs/rubric-format.md.

## Coordinator (me) — final pass after agents land
- `skills/help/SKILL.md` — reflect all new layers/commands/modes; fix the broken `docs/adapter.md`
  pointer.
- `README.md` — reflect execution + authz layers, run modes, annotation viewer.
- Full-suite test run + cross-doc consistency check.
