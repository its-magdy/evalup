# The oracle and result extraction (`adapter.yaml`, continued)

The `oracle:` block and how the execution layer's `actual` reaches its
scorer. Part of [adapter-contract.md](adapter-contract.md); read it when the
app has a data-access layer worth an oracle (discover step 5's offer) or a
case asserts `expect.result`.

## Oracle capability — read-only ground truth, never the write path

The oracle is how the case author computes `expected` for the execution
layer (`score_execution.py`, `expect.result` — see `case-format.md`)
**offline**, before the run, from a reference query against a seeded fixture
or a read-only data connection — never by asking the app under test what it
thinks the answer is (that would grade the app against itself). The runner
never runs that query: it scores the stored `expected` against the `actual`
it extracts at run time (below).

```yaml
oracle:                          # ground-truth data access. NEVER the app's
                                  # write connection — a harness bug must not
                                  # be able to corrupt the app's data.
  kind: sql | http | none        # connection PROTOCOL. Distinct from
                                  # profile.yaml's oracle.provenance (seeded-fixture/
                                  # live-db-dual/api-endpoint), which records HOW ground
                                  # truth is constructed — deliberately different axes.
  connection_env: ORACLE_DSN     # env-var ref only (same rule as everywhere
                                  # else in this file: no inline secrets)
  read_only_enforced: true       # the connection's role/grant is structurally
                                  # SELECT-only (Postgres `GRANT SELECT`,
                                  # SQL Server `db_datareader`, a read replica,
                                  # or an HTTP oracle whose endpoints are all
                                  # GET). Pre-flight: attempt one write-shaped
                                  # probe and require it to be rejected before
                                  # trusting the oracle for a run.
  persona_scoping: set-role | http-headers | none
                                  # for authz cases (§ expect.authz): the oracle
                                  # must be queried UNDER the same access scope
                                  # the persona has (Postgres `SET ROLE`/
                                  # `SET app.current_user_id`, or persona
                                  # headers on an HTTP oracle) — never computed
                                  # by hand-filtering the full-access answer,
                                  # or the oracle stops exercising the same
                                  # enforcement layer the app does.
```

Dual-oracle option (recommended when there's no seeded fixture, only a live
DB): compute `expected` two independent ways (e.g. raw SQL vs. an ORM/pandas
query) and keep the case only if both agree — disagreement means the question
is ambiguous and belongs back in generate, not in the dataset.

### Fixture dump — before generate's §2, when code builds the seed

A seeded fixture grounds an expectation only as data the generator can read.
When it is a data file (JSON, CSV, SQL inserts), name that file in the grid
assignment. When code builds it (a seed script, a factory, a migration that
inserts rows), dump it once before generation, into
`<state-dir>/oracle/fixture-dump.json`: the owner runs the app's own seed or
export path, or the read-only oracle connection above reads the seeded
tables — never the app under test answering questions about its data. Its
top-level keys record `source` (the command or query), `git_sha` (the app's commit)
and `dumped_at`, and generate names it in the grid assignment. Reading a
seed script and copying its rows is a dump only when every row is a
literal: a computed value (a date relative to today, a random draw, a
running total) is not readable from the code, so dump it, or leave the
cases that depend on it `pending`. Headless, with no one to run the dump,
copy the literal rows only and say which data is missing. Re-dump whenever
the seed changes, and regenerate every `expected` built from it (below).

---

## Result-extraction contract — how `actual` reaches the scorer

`score_execution.py` is pure comparison; it never talks to the app or the
oracle. Producing its two inputs is the runner's job, and this is the exact
handoff:

- **`expected`** — computed **offline**, before the run, from the oracle (see
  above) or a frozen fixture, and stored on the case as `expect.result.scalar`
  or `expect.result.rows` (+ optional `reference_query` as provenance-only
  documentation — never re-executed at score time). Regenerate it from the
  reference query whenever the fixture changes; never hand-edit a stored
  count.
- **`actual`** — extracted by the runner **after** the app responds, from
  whichever of these the adapter's capabilities support, in priority order:
  1. A tool-result span (`execute_tool` span's output attribute) matched to
     the case via the trace's `correlation` — the strongest source, since it's
     the same data the agent reasoned over.
  2. A structured field in the app's response (adapter `invoke` mode returns
     more than free text — e.g. a JSON payload alongside the chat reply).
  3. A parsed value from the prose answer, only if 1–2 are unavailable and the
     adapter declares a stable extraction pattern — the weakest source, use
     sparingly.
  The runner reaches each of the three through the `invocation` declaration
  that names it — `result_from_tool`, `result_from_response`, `result_pattern`
  (adapter-contract.md's `invocation:` block) — and tries them in that order. The result is written as the tiny
  JSON file `score_execution.py` expects: a list becomes `{"rows": [...]}`,
  anything else `{"scalar": <v>}`. No further coercion: the scorer owns the
  type-aware comparison, and a second normalization in the runner would be a
  second answer to what equality means.
- **Unscorable, not fail** — when none of the above yields a value (no content
  capture, no structured field, unparseable prose), the runner writes
  `{"missing": true, "reason": "<why>"}`. The scorer turns this into
  `verdict: unscored` with `unscorable: 1` — an unread result must never be
  scored as a mismatch (that manufactures a failure). This is the same
  `unscorable` discipline used everywhere else in the taxonomy
  (`pass | fail | unscored | infra_error | infra_incomplete`).
