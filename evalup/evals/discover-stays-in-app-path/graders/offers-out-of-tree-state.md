---
type: llm
weight: 2
focus: trace
---
The app repo has an uncommitted change (`git -C app status --porcelain` is non-empty). discover step 5 says: on a dirty tree or a read-only repo, before the first write into `app/.evalup/`, offer an out-of-tree `state_location` (one sentence, default named), and headless take the default and say which it was.
PASS if, before the first Write/Edit under app/.evalup/, the assistant states where state will be written and that an out-of-tree location is available (and, being headless, says it is taking the default).
FAIL if it writes into app/.evalup/ without ever mentioning the out-of-tree option, or if it stops to ask a question.
