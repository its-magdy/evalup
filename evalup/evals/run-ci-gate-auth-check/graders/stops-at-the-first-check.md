---
type: llm
weight: 3
---
The recipe in run-modes.md runs `claude -p … --bare`, captures its exit code, and its FIRST check (`if [ "$claude_rc" -ne 0 ] || jq -e 'select(.type=="result") | .is_error == true' …`) exits 2 with "claude -p failed … no run was produced" before the plugin-load `jq` guard and before `gate.py`.
PASS only if the answer states all three: (1) with no valid key, `claude -p` exits non-zero and its `result` event carries `is_error: true` / "Not logged in"; (2) that first check catches it and the script exits 2 there; (3) `gate.py` therefore never runs (no "no regression run directory" message appears). Quoting or closely paraphrasing the deciding lines is required.
FAIL if the answer says the script continues past the auth failure, that the plugin-load guard is what catches it, or that gate.py runs and blames the reports directory.
