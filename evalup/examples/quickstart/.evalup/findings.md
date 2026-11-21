# Findings: helpdesk-demo

Hand-authored for the quickstart, like the rest of this state directory: the
findings.md a discover run writes beside the profile.

## F1. The dispute window: the policy says 30 days, the prompt says 14

- SHOULD: "An invoice may be disputed within 30 days of its invoice date."
  [from doc: examples/quickstart/docs/billing-policy.md §Disputes, revised
  2026-11-01]
- IS: "Invoices can be disputed within 14 days of the invoice date."
  [from code: tests/test_example.py:83, a fragment of the system prompt]

One of the two is wrong: a bug in the prompt or a stale policy. Recorded as
business rule `dispute-window`, `status: inferred`, with this contradiction
on it. It gets no case until the owner says which is right, so the
validator reports it as a gap (`rule dispute-window has no case`) and no
gate depends on it. Suggested fix: ask billing operations; then correct the
prompt or the policy, confirm the rule, and generate its case.
