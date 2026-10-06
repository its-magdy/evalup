# Case Format (`datasets/**/*.yaml`, under the state location)

One file per case. Conversation content uses the OpenAI-schema message array
(interop with Azure/OpenAI eval tooling). Everything a scorer needs is in the
case; everything a human reviews is readable without tooling.

```yaml
id: c-3f9a2c1d                      # OPAQUE and stable forever: c-<hash8>.
                                    # It names the cases/<id>/ directory, so it
                                    # must be 1-128 of [A-Za-z0-9._-], start
                                    # with a letter or digit, and hold no `..`:
                                    # validate_cases.py errors (unsafe_id) and
                                    # run_cases.py refuses the plan (exit 2).
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
                                    # A field, never part of the id — see the id note above.
split: [full, smoke]                # which splits this case belongs to. A FIELD,
                                    # not a directory copy. Copying the file into
                                    # smoke/ and canary/ creates two files with the
                                    # same id and no single source of truth — an
                                    # in-place rewrite updates one and silently
                                    # leaves the other stale. `holdout` stays
                                    # exclusive of every other split (enforced by
                                    # the field, not by which folder the bytes are
                                    # in), so a sealed case is never also in full.
                                    # REQUIRED: validate_cases.py errors
                                    # (missing_split) on a case with no split, or
                                    # an empty one. Such a case is selected by no
                                    # run mode, and if it was meant to be held out
                                    # it is sealed by nothing.
dataset_version_added: 3
category: happy | multistep | edge | ambiguous | oos | adversarial-refusal | noise
                                    # Input flavor. Sub-divides MFT; these all share
                                    # one oracle, which is why they are a field and
                                    # not the grid's column axis.
test_type: MFT                      # MFT | INV | DIR — the ORACLE type, and the
                                    # grid's real column axis (CheckList,
                                    # arXiv:2005.04118: capabilities x test types).
                                    # REQUIRED: validate_cases.py errors
                                    # (missing_test_type) when it is absent — a
                                    # case that does not name its oracle type is
                                    # counted by no coverage number the suite
                                    # reports, including the INV/DIR floor.
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
                                     # copy this into input.messages or any prompt text.
                                     # RESERVED for stratified reporting (GAIA-style: pass rate
                                     # per band, never blended) — no script stratifies today, so
                                     # every rate the harness prints, `summary.passes` included,
                                     # IS blended across bands. Band it by hand off
                                     # verdicts.jsonl, or read the number as the mix-weighted
                                     # average it is. Canonical schema field: record it now, or
                                     # the banding is unrecoverable later.
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
                                     # Set to null ONLY for a declared one-off case
                                     # — and WRITE THE KEY. validate_cases.py checks
                                     # the key's PRESENCE, not its truthiness
                                     # (missing_template_id): absent means nobody
                                     # decided, `null` means the author declared a
                                     # one-off. A suite of nothing but one-offs
                                     # warns (all_one_off).
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
# filter:                            # RESERVED for a script that computes it. The §2b
#   self_contained: 0.91             # pass (self-containedness, answerability, ROUGE-L
#   answerable: true                 # near-duplicate gate) is done by the generating
#   nearest_neighbour: { id: c-2b8d4401, rouge_l: 0.34 }   # session, and what it
                                     # dropped goes in the template's `rejected:` list.
                                     # NO script writes this block, so a generator or a
                                     # hand must not either -- a number that was never
                                     # computed reads as a measurement. validate_cases.py
                                     # WARNs (filter_unattested) when the block is present.
metamorphic_parent: null             # or a case id — the parent this INV/DIR case perturbs.
                                     # Required when test_type is INV or DIR, and
                                     # enforced (missing_metamorphic_parent):
                                     # otherwise `test_type: INV` on an ordinary case
                                     # buys the suite's metamorphic coverage credit
                                     # while perturbing nothing.

input:
  messages:                          # OpenAI-schema. EXACTLY ONE user message and
    - { role: user, content: "why was my last invoice higher than usual?" }
                                     # nothing else. run_cases.py sends that message
                                     # alone, so a case holding a second user turn
                                     # (adapter-contract.md hard rule 3) or a
                                     # system/assistant message is SKIPPED -- it never
                                     # scores. Context in a system/assistant message
                                     # would never reach the app; fold it into the
                                     # user message (validate_cases.py WARNs:
                                     # not_one_user_message). A conversation is
                                     # `input.turns` instead -- see Conversations
                                     # below; the two are mutually exclusive.
  session: fresh                     # `fresh` only. A named seeded conversation
                                     # state is reserved (docs/multi-turn.md SS9).

available_tools: null                # optional, RESERVED: tool names the agent was meant to see
                                     # for this case (sampled subset, BFCL/ToolBench style).
                                     # THIS HARNESS CANNOT SCOPE THE CATALOG — run_cases.py sends
                                     # one request and the app exposes whatever it always exposes,
                                     # so a relevance test authored against a narrowed catalog is
                                     # not the test that runs. The field is read only by the
                                     # never-live-tool skip gate (adapter hard rule 2).
                                     # Omit = full catalog, which is also what setting it means.
excluded_tools: []                   # optional, RESERVED: tools deliberately NOT needed to solve
                                     # this case — a relevance/irrelevance test (does the agent
                                     # avoid a plausible-but-wrong tool). NOTHING SCORES IT: no
                                     # layer reports that the agent reached for one, and
                                     # trajectory_match.py knows only `forbidden`, which is a
                                     # different (hard-fail) semantic. To make reaching for one
                                     # FAIL, list it in expect.tools.forbidden; leave it here only
                                     # as documentation. validate_cases.py WARNs either way.

seed_state: null                     # optional, RESERVED: fixture the environment is meant to
                                     # hold for this case. NOBODY LOADS IT — run_cases.py never
                                     # invokes environment.seed/.reset/.snapshot_state, so the
                                     # case runs against whatever state is already there. Seed it
                                     # out of band before the run; a stored `expect.result`
                                     # computed against a fixture nobody loaded is an artifact of
                                     # ambient state, not a measurement.

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
  route: billing                     # omit if no router / not asserted.
                                     # Trace-less app whose adapter declares only
                                     # `route_from_status`? Then the runner observes
                                     # the map's VALUE (`<answered>`, `__oos__`, ...),
                                     # never a domain: expect that label, keep the
                                     # domain in `unit`, and let validate_cases.py
                                     # --adapter say so (route_not_observable).
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
  state:                             # end-state assertion. RESERVED: no scorer compares
    null                             # environment snapshots, so this key is graded by NOTHING.
                                     # It counts toward no layer: a case whose ONLY expectation
                                     # is `state` is a `no_graded_layer` ERROR (its http row alone
                                     # would otherwise pass it). Alongside real expectations it
                                     # is a WARN and simply contributes nothing to the verdict.
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
                                     # Never an entry the user message already contains
                                     # (validate_cases.py WARNs echo_assertion): an app
                                     # that repeats the request passes it. Assert the
                                     # fact that goes WITH the name — the leave type,
                                     # the date, the count — not the name itself.
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
                                     # is caught and reported "unscorable".
                                     # UNICODE: entry and answer are compared in
                                     # NFC, so an accented or Arabic string matches
                                     # whichever form the app produced. A REGEX
                                     # pattern is NOT normalized (that would rewrite
                                     # your pattern) — write patterns in NFC; the
                                     # check's note warns when one is not.
    rules: [no-uncatalogued-prices]  # business-rule oracle ids from profile.yaml.
                                     # NOT SCORED TODAY: no script evaluates them and
                                     # the runner reports the layer `unscored`. Write a
                                     # rule you can state as must_contain /
                                     # must_not_contain / json_schema there instead.
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
                                     # validate_cases.py checks it (warning: no_op_pass when
                                     # `pass` has no justification).
no_op_justification: null            # Expected when no_op_expectation is `pass` (validate_cases.py
                                     # warns `no_op_pass` without it): one line on
                                     # why this case is the exception. Without it the `pass`
                                     # is just an assertion that the case need not work, and
                                     # a suite quietly accumulates them.
                                     # Writing the reason down is the whole gate — it forces
                                     # the author to notice they are exempting a case.
k: 1                                 # repeats; smoke=1, reliability runs=3+ (reports pass^k)
gating: true                         # false = tracked-not-gating (e.g. noise cases pre-launch)
notes: ""                            # reviewer's one-liner: why this case exists
```

## Conversations (`input.turns`)

A **live scripted conversation**: the author writes only the user's turns, in
order; the runner sends them one at a time to the real app inside one
conversation, and the app's real replies are the history. Nobody writes the
assistant's lines. It tests the app's *own* memory — the bug class this exists
to catch. Spec: `${CLAUDE_PLUGIN_ROOT}/docs/multi-turn.md`.

```yaml
id: c-followup-01
persona: team_manager                  # one identity for the whole conversation
input:
  turns:
    - user: "Show me the licences for the Cairo team"
      expect: { route: licences }      # optional checkpoint on this turn
    - user: "only the expired ones"
every_turn:                            # optional: copied into each turn's expect
  tools: { forbidden: [update_licence] }
expect:                                # the FINAL turn's checks, as for any case
  route: licences
  answer: { must_not_contain: ["which team"] }
gating: false                          # until a baseline exists (generate SS3)
```

- **Each turn is an object with a `user` key, never a bare string** — later
  phases add keys beside it (`bad_turn`). `assistant:` and `simulate:` are
  reserved for those phases and refused now (`turn_key_reserved`); any other
  key is a typo (`unknown_turn_key`).
- **At least two turns** (`single_turn_conversation`: write a single-turn
  case instead); a WARN above 8 (`long_conversation`). The plan's
  `execution.max_turns` (default 12) is the runner's hard cap: a longer case
  is skipped.
- **`input.turns` and `input.messages` are mutually exclusive**
  (`turns_and_messages`).
- **The top-level `expect` is the final turn's claim**, so every authoring
  rule reads it unchanged: gradedness, metamorphic parents, the reserved-field
  WARNs. A case whose only graded check sits on a checkpoint is a
  `no_graded_layer` ERROR ("put a graded check on the final turn"), and a
  final turn carrying its own `expect:` is `final_turn_expect`.
- **An earlier turn's `expect` is a checkpoint**, with the same keys and the
  same scorers. A turn without one still gets the implicit HTTP check: a 2xx
  passes, a 3xx or a 4xx other than 429 fails, a 5xx is `infra_error`.
- **The conversation stops at the first turn that rolls up `fail` or
  `infra_*`**; it continues past `unscored` (the harness could not look,
  which is not the app failing). The case verdict is the rollup over every
  sent turn's layers, so one failed checkpoint fails the case.
- **`every_turn`** copies its keys into every turn's expect, the final one
  included. Without it, `expect.tools.forbidden` checks only the turn it sits
  on, and "never call `update_licence` during the conversation" silently
  means "not on the last turn". Objects merge key by key (`every_turn.tools.
  forbidden` beside a turn's `tools.subset` checks both); a value both set is
  `every_turn_collision`. On a case without `input.turns` it is
  `every_turn_without_turns`.
- **A `clarify_ok` checkpoint cannot branch.** If the app *may* ask a
  clarifying question, the scripted next turn is either its answer or a
  non-sequitur, so use one only where the clarification is deterministic.
- **A conversation canary is an ERROR** (`multi_turn_canary`): conversations
  are flaky by construction, and a failing canary aborts the run.
- **It runs only when the adapter declares `invocation.conversation`**
  (adapter-contract.md). Without it the case is `skipped` with that reason.
- **One conversation is one case** for every count, interval and gate — never
  N turns. Its `latency_s` and token counts are sums over the turns it sent.

Validation: run
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/validate_cases.py --cases <suite.json> --capabilities <capability_matrix.json> --adapter <adapter.json>`
before handing a dataset over. It checks at load time the "hard error, never a
silent fallback" rules above (`order_mode`, args `calls` scope, empty
`columns`), which the scorers would otherwise only hit at RUN time — long after
a suite is authored and reviewed. Exit 0 = clean, 1 = errors, 2 = bad input.

`--capabilities` is **required**. Without the profile's matrix every layer
counts as enabled, which turns off the one check that catches a case asserting
only DISABLED layers — it runs, it passes, it grades nothing. Pass
`--no-capabilities` if you genuinely have no profile: it says so out loud,
reports `capabilities_unchecked`, and is refused under `--strict`.

Which findings are ERRORS and which are WARNINGS, as one rule: an **error** is
a claim the suite makes that nothing backs (no `split`, no `test_type`, no
`template_id` key, an INV with no parent, a case grading no enabled layer, a
duplicate id). A **warning** is a suite thinner than the guidance recommends
(no INV/DIR at all, all one-offs, a skewed category mix, a machine-stamped
`accepted`, a suite in which no non-canary case gates, a `must_contain` the
input already satisfies, a canary outside `smoke`, an authored `filter:`
block, a capability-matrix entry under a report row's name such as `answer`
(the key is `answer_quality`), and — with `--adapter` — a label the runner cannot observe or an
attack case `environment.safe_to_attack` will skip) — a budget judgement
the author is allowed to make, and one `--strict` promotes for a gating CI
job. A required field enforced by a
warning is not required, so nothing in the first list warns.

The check that matters most is `no_graded_layer`: a case must assert at least
one layer that is ENABLED in the capability matrix. `expect.http` alone never
counts — it is a liveness check, not a behavioral assertion. This is what
prevents a suite of `gating: true` cases whose expectations all sit in disabled
layers: such cases run, pass, and cannot fail, inflating the pass rate while
measuring nothing.

Scoring semantics:
- Layers score independently; a case reports per-layer verdicts, not one blob.
  There is no per-LAYER statistic: `stats.py` gates one verdict per case, so a
  layer that regressed while the case-level rate held flat is visible only by
  reading the per-case rows.
- **Reserved fields, in one place** — `expect.state`, `seed_state`,
  `available_tools`, `excluded_tools` and `difficulty` are all declared here and
  scored by nothing. Each is safe to record (the provenance is unrecoverable
  later); the first four earn a `validate_cases.py` WARN so no one mistakes
  them for a measurement, and `difficulty` is read by no script at all.
  A case whose `input.messages` holds anything besides its one user message
  is SKIPPED: only that message would reach the app. A conversation is
  `input.turns` (Conversations, above).
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
