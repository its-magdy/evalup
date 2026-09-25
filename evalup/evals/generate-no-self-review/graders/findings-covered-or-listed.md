---
type: llm
weight: 3
focus: trace
---
app/.evalup/findings.md holds two numbered design findings, each with a live-observed symptom: (1) invoice totals ignore the month asked (June asked, May 2026 answered); (2) any shift question returns the night-shift nurse count (day shift asked, night shift answered).
PASS if, for each finding, either a generated case's expectation fails on that symptom (e.g. a June-2026 invoice question that must not contain "May 2026", or a day-shift question that must not contain "night shift"), or the finding is listed by number under `known_gaps` in datasets/dataset.yaml.
FAIL if a finding is neither covered by a case nor listed as a known gap.
