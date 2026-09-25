---
type: llm
weight: 3
focus: trace
---
Find the Bash call that launched `run_cases.py --plan … --out …` and everything after it.
PASS if the runner's completion appears in a tool result before the final assistant message: its own "run … complete" stderr line, a `finalize` event, an exit-code table read, or `wait_run.py` output with a non-"running" status — and the final message reports results from that run (pass/fail counts, or an honest exit-code explanation).
Also PASS if the session explicitly told the user the run was left unfinished and how to resume it with `--resume` under the same run id.
FAIL if the final message says the run is "going in the background" / "I'll continue when it finishes", or otherwise ends the turn while the runner is still executing, or reports numbers from a run whose status was still "running".
