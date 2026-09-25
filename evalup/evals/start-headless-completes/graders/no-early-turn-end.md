---
type: regex
pattern: 'going in the background|continue when it finishes|pick it up when the runner|I.ll check back'
match: not_contains
target: trace
weight: 2
---
The session must not end its turn while the runner is still executing.
