# Case Format (`datasets/**/*.yaml`, under the state location)

One file per case. Conversation content uses the OpenAI-schema message array
(interop with Azure/OpenAI eval tooling). Everything a scorer needs is in the
case; everything a human reviews is readable without tooling.

```yaml
id: c-3f9a2c1d                      # OPAQUE and stable forever: c-<hash8>.
                                    # Do NOT encode unit or category in the id.
                                    # Both are mutable classifications, and an id
                                    # declared "stable forever" that spells one of
                                    # them WILL eventually lie: reclassify a case
                                    # from happy to oos and the id still claims
                                    # happy; rename a domain and every id spelling
                                    # the old name is stranded. Keep them as the
                                    # fields below, where a reclassification is a
                                    # normal edit. (Braintrust's id-upsert model:
                                    # the id identifies, it does not describe.)
unit: billing                       # route target (domain/node/sub-agent), or the
                                    # primary tool for a tool_agent, or "app".
                                    # A FIELD now — see the id note above.
split: [full, smoke]                # which splits this case belongs to. A FIELD,
                                    # not a directory copy. run-modes.md already
                                    # calls smoke membership "a durable tag"; this
                                    # makes it literally one. Copying the file into
                                    # smoke/ and canary/ creates two files with the
                                    # same id and no single source of truth — an
                                    # in-place rewrite updates one and silently
                                    # leaves the other stale. `holdout` stays
                                    # mutually exclusive with `full` (enforced by
                                    # the field, not by which folder the bytes are
                                    # in), so a sealed case is never also in full.
dataset_version_added: 3
category: happy | multistep | edge | ambiguous | oos | adversarial-refusal | noise
                                    # Input flavor. Sub-divides MFT; these all share
                                    # one oracle, which is why they are a field and
                                    # not the grid's column axis.
test_type: MFT                      # MFT | INV | DIR — the ORACLE type, and the
                                    # grid's real column axis (CheckList,
                                    # arXiv:2005.04118: capabilities x test types).
                                    # MFT = a direct case with its own expectation.
                                    # INV = perturbed input, expectation must NOT
                                    #       change (pair with metamorphic_parent);
                                    #       inherits the parent's labels, so it is
                                    #       the cheapest coverage in the grid.
                                    # DIR = perturbed input, expectation moves in a
                                    #       KNOWN direction (narrower filter must not
                                    #       increase row count; a permission removed
                                    #       must not add rows).
difficulty: medium                  # easy | medium | hard. HIDDEN from the agent — never
                                     # copy this into input.messages or any prompt text; it
                                     # exists only for stratified reporting (GAIA-style: report
                                     # pass rate per difficulty band, never blended). Canonical
                                     # schema field.
template_id: shift_count_by_role    # REQUIRED unless this is a declared one-off.
                                    # id shared by every instantiation of one
                                     # authored case template (e.g. "shift_count_by_role").
                                     # Groups variants for consistency scoring — does the app
                                     # behave the same across instances of the same template.
                                     # Per-template consistency reporting, an idea borrowed
                                     # from AppWorld's scenario-level goal-completion (SGC)
                                     # metric rather than a direct equivalent of it — AppWorld
                                     # doesn't itself define a template-consistency metric.
                                     # ALSO what makes the suite's clustering computable:
                                     # cases sharing a template are not independent samples,
                                     # so a pass rate over them needs CLUSTERED standard
                                     # errors ("Adding Error Bars to Evals", Miller 2024).
                                     # Set to null ONLY for a declared one-off case.
instantiation_params: {role: nurse} # REQUIRED whenever template_id is set: the tuple this
                                     # case was realized from — the PUBLIC params that vary
                                     # per instance (e.g. {role: nurse, week: "2026-W05"}),
                                     # safe to reflect in the instruction text.
                                     # Never put the gold answer here; that stays under `expect`
                                     # so the oracle can never leak into the prompt.
                                     # template_id without params, or params without
                                     # template_id, is a hard error — it means the two-phase
                                     # generation flow (generate/SKILL.md §2a) was short-
                                     # circuited and the case was written straight to prose.
origin: synthetic | real-trace:<trace_id> | exploratory | mutation-gap
review: { status: pending, by: null, at: null }
                                     # status: accepted | pending | quarantined.
                                     # `accepted` MAY ONLY BE SET BY A HUMAN, and `by` must
                                     # be that person's name. A generator writing
                                     # "accepted, by: generate-review" records that nobody
                                     # looked — strictly worse than `pending`, which is at
                                     # least visibly unreviewed. validate_cases.py warns
                                     # (machine_accepted) on an automated-looking `by`.
filter:                              # provenance from the §2b mechanical filter pass, so a
                                     # reviewer can see what was screened and tune it.
  self_contained: 0.91               # quality score; below threshold -> dropped, not kept
  answerable: true                   # could the expectation be derived from the oracle?
  nearest_neighbour: { id: c-2b8d4401, rouge_l: 0.34 }   # near-duplicate gate (< 0.7)
metamorphic_parent: null             # or a case id — the parent this INV/DIR case perturbs.
                                     # Required when test_type is INV or DIR.

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
  args:                              # scored by score_args.py:
    get_invoice: { invoice_id: from_tool_result }  # value | from_tool_result | present
                                     # from_tool_result needs content capture —
                                     # without it the check is "unscorable", not fail.
                                     # Matching is token-bounded (INV-1 never matches
                                     # inside INV-10); on the trace's FIRST call it
                                     # FAILS (nothing earlier to source from); values
                                     # under 3 chars are "unscorable".
                                     # A never-called tool is a trajectory finding,
                                     # not an args fail.
    # search: { calls: any, q: invoices }   # CALL SCOPE. A tool may legitimately be
                                     # called several times with different arguments
                                     # (page, refine, retry), so an expectation about
                                     # one call is not an assertion about all of them.
                                     # any   — DEFAULT: passes if AT LEAST ONE call
                                     #         satisfies every spec in the entry. Specs
                                     #         are evaluated together per call, so
                                     #         "the call that did X also did Y" stays
                                     #         expressible.
                                     # all   — every observed call must satisfy them.
                                     # first — only the tool's first call is checked.
                                     # An unrecognized scope is a hard error, never a
                                     # silent fallback (same discipline as order_mode).
                                     # NOTE ON RESERVED WORDS: "present" and
                                     # "from_tool_result" are reserved VALUES (you
                                     # cannot assert them as literal expected values);
                                     # "calls" is a reserved KEY (a tool argument
                                     # actually named `calls` cannot be asserted on).
                                     # An entry that asserts nothing once `calls` is
                                     # removed (`search: {calls: all}`, `search: {}`,
                                     # `search:`) is a HARD ERROR — it would score every
                                     # trajectory and leave no row saying so. To assert
                                     # only that a tool was called, use expect.tools.
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
                                     # keys present in the expected rows). OMIT the key to
                                     # get that default — an empty list is a hard error,
                                     # not "compare nothing" (zero columns would make every
                                     # row equal, a vacuous pass).
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
                                     # NO TRAILING /flag SUFFIX: "/pattern/i" (Perl/
                                     # JS-style) is NOT this convention — only bare
                                     # /pattern/ is recognized. Case-insensitivity
                                     # goes INLINE: "/(?i:pattern)/". A trailing /i
                                     # is caught and reported "unscorable" (found
                                     # live: 6 cases shipped with /i and the check
                                     # either always failed or vacuously always
                                     # passed before this was caught).
    rules: [no-uncatalogued-prices]  # business-rule oracle ids from profile.yaml —
                                     # evaluated by the run skill against the
                                     # profile's rule definitions, not by a script
    rubric: billing-answer-v2        # judged dimensions — only if calibrated. Format is
                                     # <rubric_id>-v<version>, resolved against the rubric
                                     # file's `rubric_id`/`version` fields (a version PIN, so
                                     # a run records which rubric revision graded it). The
                                     # rubric itself is a decomposed-binary DAG — see
                                     # ${CLAUDE_PLUGIN_ROOT}/docs/rubric-format.md.
  format: { json_schema: null }      # structured-output compliance — scored by
                                     # score_answer.py (minimal stdlib validator:
                                     # type/required/properties/items/enum)

no_op_expectation: fail              # pass | fail. Sanity check: if an agent calls no tools
                                     # and gives no real answer, does
                                     # this case correctly FAIL? Almost always `fail` — a case
                                     # that resolves to `pass` for a do-nothing agent is usually
                                     # vacuous (reconsider it rather than accepting `pass` as the
                                     # default). Not scored by a runner flag; it's a
                                     # generation-time and review-time sanity label — but
                                     # validate_cases.py DOES check it (warning: no_op_pass),
                                     # so it is no longer advisory-only prose.
no_op_justification: null            # REQUIRED when no_op_expectation is `pass`: one line on
                                     # why this case is the exception. Without it the `pass`
                                     # is just an assertion that the case need not work, and
                                     # a suite quietly accumulates them (a quarter of a real
                                     # 31-case suite carried `pass` before this check existed).
                                     # Writing the reason down is the whole gate — it forces
                                     # the author to notice they are exempting a case.
k: 1                                 # repeats; smoke=1, reliability runs=3+ (reports pass^k)
gating: true                         # false = tracked-not-gating (e.g. noise cases pre-launch)
notes: ""                            # reviewer's one-liner: why this case exists
```

Validation: run
`${CLAUDE_PLUGIN_ROOT}/scripts/validate_cases.py --cases <suite.json> --capabilities <capability_matrix.json>`
before handing a dataset over. It is the load-time schema check this format
previously lacked — the "hard error, never a silent fallback" rules below
(`order_mode`, args `calls` scope, empty `columns`) were enforced only inside
individual scorers at RUN time, which is long after a suite is authored and
reviewed. Exit 0 = clean, 1 = errors, 2 = bad input.

The check that matters most is `no_graded_layer`: a case must assert at least
one layer that is ENABLED in the capability matrix. `expect.http` alone never
counts — it is a liveness check, not a behavioral assertion. This is what
prevents a suite of `gating: true` cases whose expectations all sit in disabled
layers: such cases run, pass, and cannot fail, inflating the pass rate while
measuring nothing.

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
- A layer with zero failing checks but one or more `unscorable` ones reports
  `verdict: pass` **plus** a top-level `partially_unscored: true`. The verdict stays
  `pass` because consumers key on it, but a pass resting partly on checks that could
  not be evaluated is not the same as a fully-evidenced one — report the flag rather
  than letting the distinction vanish.
