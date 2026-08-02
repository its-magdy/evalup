# Case Format (`datasets/**/*.yaml`, under the state location)

One file per case. Conversation content uses the OpenAI-schema message array
(interop with Azure/OpenAI eval tooling). Everything a scorer needs is in the
case; everything a human reviews is readable without tooling.

```yaml
id: billing-happy-3f9a2c1d          # <unit>-<category>-<hash8>, stable forever.
                                    # unit = route target (domain/node/sub-agent),
                                    # or the primary tool / "app" for single-LLM.
dataset_version_added: 3
category: happy | multistep | edge | ambiguous | oos | adversarial-refusal | noise
difficulty: medium                  # easy | medium | hard. HIDDEN from the agent — never
                                     # copy this into input.messages or any prompt text; it
                                     # exists only for stratified reporting (GAIA-style: report
                                     # pass rate per difficulty band, never blended). Canonical
                                     # schema field, EVAL-DESIGN-RECOMMENDATION.md §19.
template_id: null                   # optional: id shared by every instantiation of one
                                     # authored case template (e.g. "shift_count_by_role").
                                     # Groups variants for consistency scoring — does the app
                                     # behave the same across instances of the same template
                                     # (AppWorld "SGC"). Omit for one-off cases.
instantiation_params: {}            # optional, paired with template_id: the PUBLIC params
                                     # that vary per instance (e.g. {role: nurse, week:
                                     # "2026-W05"}) — safe to reflect in the instruction text.
                                     # Never put the gold answer here; that stays under `expect`
                                     # so the oracle can never leak into the prompt.
origin: synthetic | real-trace:<trace_id> | exploratory | mutation-gap
review: { status: accepted | pending | quarantined, by: <name>, at: <date> }
metamorphic_parent: null            # or a case id — expectation: same behavior

input:
  messages:                          # OpenAI-schema; single- or multi-turn
    - { role: user, content: "why was my last invoice higher than usual?" }
  session: fresh                     # or a named seeded conversation state

available_tools: null                # optional: tool names actually exposed to the agent for
                                     # this case, if the harness can scope the catalog per case
                                     # (sampled subset, BFCL/ToolBench style). Omit = full catalog.
excluded_tools: []                   # optional: tools left IN the exposed catalog (or listed
                                     # here purely for documentation if the catalog can't be
                                     # scoped) that are deliberately NOT needed to solve this
                                     # case — a relevance/irrelevance test (does the agent avoid
                                     # reaching for a plausible-but-wrong tool). Calling one is a
                                     # trajectory finding, not automatically a hard fail unless
                                     # it also appears in expect.tools.forbidden.

seed_state: null                     # optional: fixture the environment must be
                                     # seeded with (enables state-diff scoring)

identity: null                       # optional: who the request is made as —
                                     # use for authorization cases (403s,
                                     # permission-scoped answers). Maps onto the
                                     # adapter's auth shape, e.g.:
# identity:
#   permissions: [30030]             # → permission headers
#   role: manager                    # or token_env: APP_MANAGER_TOKEN

expect:
  http:
    status: 200                      # expected HTTP status. Use when status codes
                                     # carry behavior (deliberate 400 on OOS, 403
                                     # on permission) — for trace-less apps often
                                     # the only routing observable.
  route: billing                     # omit if no router / not asserted
  route_acceptable: [billing]        # set-valued: any of these passes (ambiguous
                                     # cases). `route` is always implicitly
                                     # acceptable — no need to repeat it here.
                                     # Scored by score_routing.py: the runner maps
                                     # route→expected, route_acceptable→acceptable
                                     # (accepted as aliases) and passes
                                     # --oos-route <name> so `oos` cases map to
                                     # the __oos__ label and OOS metrics appear.
  clarify_ok: false                  # true → asking a clarifying question also passes
  tools:
    subset: [list_invoices, get_invoice]   # DEFAULT mode: these must appear
                                     # (duplicate-aware); extras OK
    # order: [a, b]                  # only when order genuinely matters (human-stated)
    # order_mode: in_order           # in_order (default: relative order, extras OK)
                                     # | exact (exactly these calls, this order)
                                     # | any_order (exactly these calls, any order)
                                     # If both order and subset are given, order wins.
                                     # Exactly these three spellings — an
                                     # unrecognized mode is a hard error, never a
                                     # silent fallback to the loosest mode.
    forbidden: [update_ticket]       # policy: must NOT be called
  args:                              # scored by score_args.py per observed call:
    get_invoice: { invoice_id: from_tool_result }  # value | from_tool_result | present
                                     # from_tool_result needs content capture —
                                     # without it the check is "unscorable", not fail.
                                     # Matching is token-bounded (INV-1 never matches
                                     # inside INV-10); on the trace's FIRST call it
                                     # FAILS (nothing earlier to source from); values
                                     # under 3 chars are "unscorable".
                                     # A never-called tool is a trajectory finding,
                                     # not an args fail.
                                     # NOTE: "present" and "from_tool_result" are
                                     # reserved — you cannot assert those two as
                                     # literal expected VALUES.
  state:                             # end-state assertion (needs seed_state + snapshot)
    null
  authz:                             # PERMISSION/SCOPE cases — scored by score_authz.py
                                     # DETERMINISTICALLY against the trace's tool-call log and
                                     # returned record IDs. The refusal verdict is NEVER an LLM
                                     # read of the chat — a politely-worded refusal over an open
                                     # endpoint underneath must still fail. (The answer text is
                                     # used only as SECONDARY string-match evidence for a
                                     # forbidden id leaking into the prose, never to decide
                                     # whether a refusal occurred.) Pair with `identity` above:
                                     # derive allowed/forbidden ids by running the oracle query
                                     # under that persona's real access scope — the adapter's
                                     # oracle.persona_scoping (RLS `SET ROLE` / equivalent) is
                                     # what makes that ground truth trustworthy — not by
                                     # hand-filtering the full answer.
    allowed_record_ids: [e_881]      # IDs the caller's scope MAY see (from identity)
    forbidden_record_ids: [e_412]    # IDs that MUST NOT appear in the answer or any tool result
    forbidden_tools: [approve_swap]  # privileged tools this identity must not invoke
    expect_refusal: false            # true = the correct behavior is a scoped refusal
  result:                            # EXECUTION ACCURACY for data-Q&A — scored by
                                     # score_execution.py against a ground-truth result
                                     # set (grade the DATA, not the prose around it —
                                     # this is what catches a stub tool returning [] while
                                     # the bot confidently answers "0"). Provide exactly
                                     # one of scalar | rows. Omit for non-data cases.
    scalar: 7                        # single-value answer ("how many nurses on nights?").
                                     # Type-aware (GAIA quasi-exact-match): 7 == "7" ==
                                     # "7.0", "1,200" == 1200, "$5" == 5, "45%" == 45;
                                     # strings casefold+trim; a bool is never == a number.
    # rows: [{role: nurse, n: 7}]    # a result set. Compared as a BAG (order-insensitive
                                     # multiset — SELECT without ORDER BY has no order) by
                                     # default; multiplicity matters ([a,a,b] != [a,b,b]).
                                     # Rows are projected to the columns the EXPECTED rows
                                     # define — extra columns the app returned are ignored.
    # ordered: false                 # rows only: true = list-equal (a defined ORDER BY)
    # columns: null                  # rows only: compare only these keys (default: the
                                     # keys present in the expected rows)
    # float_tolerance: 0.0           # absolute tolerance for numeric cells (default exact)
    # reference_query: "SELECT ..."  # provenance only (not read by the scorer): how the
                                     # runner computed `expected` offline from the seeded
                                     # fixture / read-only oracle. Regenerate expected from
                                     # this if the fixture changes — never hand-edit counts.
                                     # The runner extracts `actual` from the trace/tool
                                     # result; if it can't, the check is "unscorable" (the
                                     # layer reports unscored), never a fail.
  answer:
    must_contain: []                 # scored by score_answer.py — plain string =
    must_not_contain: ["$"]          # substring, "/.../" = regex (re.search).
                                     # ALWAYS quote entries: an unquoted number is
                                     # coerced to its string form, and an unquoted
                                     # yes/no/true/false becomes a YAML bool, which
                                     # is reported "unscorable" (not a silent fail).
                                     # WATCH THE SLASHES: a path-like literal such
                                     # as "/usr/local/" is read as the REGEX
                                     # `usr/local`, not as a substring — drop the
                                     # outer slashes to match it literally.
                                     # A regex that cannot finish in 2s is reported
                                     # "unscorable"; it never hangs the run.
    rules: [no-uncatalogued-prices]  # business-rule oracle ids from profile.yaml —
                                     # evaluated by the run skill against the
                                     # profile's rule definitions, not by a script
    rubric: billing-answer-v2        # judged dimensions — only if calibrated. Format is
                                     # <rubric_id>-v<version>, resolved against the rubric
                                     # file's `rubric_id`/`version` fields (a version PIN, so
                                     # a run records which rubric revision graded it). The
                                     # rubric itself is a decomposed-binary DAG — see
                                     # docs/rubric-format.md.
  format: { json_schema: null }      # structured-output compliance — scored by
                                     # score_answer.py (minimal stdlib validator:
                                     # type/required/properties/items/enum)

no_op_expectation: fail              # pass | fail. Sanity check (AppWorld no_op_pass/no_op_fail):
                                     # if an agent calls no tools and gives no real answer, does
                                     # this case correctly FAIL? Almost always `fail` — a case
                                     # that resolves to `pass` for a do-nothing agent is usually
                                     # vacuous (reconsider it rather than accepting `pass` as the
                                     # default). Not itself scored by a runner flag; it's a
                                     # generation-time and review-time sanity label.
k: 1                                 # repeats; smoke=1, reliability runs=3+ (reports pass^k)
gating: true                         # false = tracked-not-gating (e.g. noise cases pre-launch)
notes: ""                            # reviewer's one-liner: why this case exists
```

Scoring semantics:
- Layers score independently; a case reports per-layer verdicts, not one blob.
- `infra_error` / `infra_incomplete` verdicts never count in pass/fail denominators.
- Judged dimensions are skipped (reported as `unjudged`) while the judge is
  uncalibrated — never silently included.
- `expect.authz` is scored deterministically by `score_authz.py` against the trace's
  tool-call log and returned record IDs — the refusal verdict never derives from the
  chat text (the answer is only secondary string-match evidence for a leaked id), so a
  case cannot pass by virtue of a nicely-worded refusal alone if the underlying tool
  call/record leak still happened.
- `difficulty` never appears in `input.messages` or any text sent to the agent; it
  exists purely so reports can be segmented per band instead of blended into one
  average (a blended pass rate hides a band that's actually broken).
