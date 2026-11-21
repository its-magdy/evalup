---
rubric_id: grounded-answer
version: 1
form: criteria                     # v1 run-time form; absent = the DAG form
last_calibrated: null
calibration_notes: >
  v1: written for the quickstart's invoice case.
status: { calibrated: false }      # record-keeping, as today; judge-run.md SS6 decides

criteria:                          # each one a yes/no question; id = the
  - id: states_the_amount          # node_id calibration labels use
    question: "Does the answer state the amount in the reference result?"
    reference_guided: true         # the judge is handed expect.result
  - id: states_a_period
    question: "Does the answer say which period the amount covers?"
  - id: period_is_asked_one
    question: "Is the period the answer names the one the user asked about?"
  - id: no_unsupported_claim
    question: "Is every figure in the answer present in the app's response
               data (response.json body), with nothing added?"

self_test:                         # judge-run.md SS3: graded before any case is
  good:
    user: "what did I spend on invoices in May 2026?"
    answer: "You spent $412.50 on invoices in May 2026."
    response: { value: 412.5 }
    reference: { scalar: 412.5 }
  bad:
    user: "what did I spend on invoices in May 2026?"
    answer: "You spent $412.50 on invoices in April 2026, and $90 on fees."
    response: { value: 412.5 }
    reference: { scalar: 412.5 }
    fails: [period_is_asked_one, no_unsupported_claim]
---

The quickstart's one rubric: it judges profile.yaml's `grounded-only` rule
("never state a figure not present in a tool result"), which no string check
can, on `c-4f2a91c7`. Its verdicts are `provisional` -- shown with their
quotes, in no pass rate, gate or optimize objective -- until a calibration
for this rubric version on the judge's model exists (docs/judge-run.md).
