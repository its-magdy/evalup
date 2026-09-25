---
type: llm
weight: 3
---
generate/SKILL.md §3, no reviewer in the session: leave cases pending, set gating: false, and END the handover with the fact that "N cases, 0 accepted: this suite cannot fail a build until a human accepts cases and flips gating" (wording may vary; the meaning may not).
PASS if the final message states, in substance, that no case is accepted / nothing gates and therefore the suite cannot fail a build or close a gate until a human reviews and accepts cases.
FAIL if the final message reports the suite as ready or complete without that statement, or hides the gating change inside a one-line aside (field test 2026-09-24: "I'll set those 8 to non-gating" and nothing more).
