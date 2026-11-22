# Discover — a later session

Read when discover runs against a state location that already has a
`profile.yaml`, or with `--diff`. A first session never needs this file.

## Existing profile, no `--diff`

Read findings.md's `deferred:` list —
after a lean session that is the to-do list — and run those steps plus any
the user names; with no `deferred:` list, run the full procedure. Either way
update `profile.yaml`, `adapter.yaml` and findings.md in place (Edit
findings.md: Write is refused on that name in a subagent): keep every
entry a live request already verified, never regenerate a working adapter
from scratch. Before the first write, say which files will change — nothing
else backs them up, so the user can copy them first — and list what changed
at the end (headless: in the closing summary).

A profile written before #25 keeps its rules under
`confirmed_by_human.business_rules`: move them to the top-level
`business_rules` list in profile-schema.md's shape, each `status: inferred`
until the interview re-confirms it — a confirmation nobody can date is not
one. Add the quote, source and date you can find; the validator names what
is missing (`rule_incomplete`), and warns `legacy_rules_location` while
the old block is left.

A profile written before #42 has a four-value `stage`: rewrite
`pre-stability` to `early`, and `traffic` or `production` to `stable`
(the stage that gated the same checks), and say so; the validator warns
`stage_renamed` while the old name is left.

## `--diff` mode (after app changes)
Re-run SKILL.md steps 2–4 against the current code, topology detection included: a
refactor can move an app from `tool_agent` to `router_executor`, which flips
which layers apply. Diff old profile against new, list renamed/removed/added
agents/tools/route targets, then map onto the dataset — which cases reference
stale names. Propose bulk migrations: mechanical renames are auto-fixable,
semantic changes get flagged for human review. This command is the answer to "my
app changed"; never let the user meet staleness as a wall of red instead.
