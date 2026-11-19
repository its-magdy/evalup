# Headless/CI gate — worked example

Read from `${CLAUDE_PLUGIN_ROOT}/skills/run/SKILL.md` §6 when wiring a run
into CI; the modes themselves are [run-modes.md](run-modes.md).

The hard-gated modes (`regression`, `full`) are the ones wired into CI. The
LLM produces the run and nothing more; a separate, tokenless shell step
owns the actual gate. Flags and event fields below were checked against
Claude Code 2.1.282 and its headless and permission-mode docs on 2026-09-25;
re-check them when the CLI's major version changes.

```bash
EVALUP_ROOT=/path/to/the/installed/evalup   # CLAUDE_PLUGIN_ROOT is not set outside a Claude session
export ANTHROPIC_API_KEY=...                # --bare never uses a subscription login

# Agent-driven part: no auto-discovery surprises, no prompt nobody can answer.
# `< /dev/null` closes stdin: left open, the CLI waits 3 s for piped input
# and warns before continuing.
claude -p "/evalup:run --regression" \
  --bare --plugin-dir "$EVALUP_ROOT" \
  --permission-mode acceptEdits --permission-prompts none \
  --output-format stream-json --verbose \
  < /dev/null > run-output.jsonl
claude_rc=$?

# First: did the session itself succeed? A missing or invalid API key exits
# non-zero and ends the stream with a `result` event whose `is_error` is true
# ("Not logged in"), after one turn and no run. Check the exit code and that
# flag -- not `subtype`, which reads "success" on that event. Without this
# step the plugin-load guard below passes (nothing loaded wrongly) and
# gate.py then reports "no regression run directory", which is true and
# blames the wrong thing.
if [ "$claude_rc" -ne 0 ] || \
   jq -e 'select(.type=="result") | .is_error == true' run-output.jsonl >/dev/null; then
  echo "claude -p failed (rc=$claude_rc) -- no run was produced"
  jq -r 'select(.type=="result") | .result // .error // empty' run-output.jsonl | tail -1
  exit 2
fi

# Second: confirm the plugin loaded clean. The init event omits both error
# keys when there is nothing to report, hence `// []`; and it must name this
# plugin, or `/evalup:run` resolved to nothing.
jq -e 'select(.type=="system" and .subtype=="init")
       | (.plugin_errors // [] | length == 0)
         and (.mcp_server_errors // [] | length == 0)
         and ((.plugins // []) | map(.name) | index("evalup") != null)' \
  run-output.jsonl >/dev/null || { echo "plugin/mcp load errors, or evalup not loaded -- run untrusted"; exit 2; }

# The actual gate: a plain script reads the verdict, no LLM involved. It
# finds the run this invocation just wrote by the TIMESTAMP in the run id
# (never mtime, which drifts when a directory is copied or a report
# regenerated), reads that run's results.json -- the durable file that
# survives past this one CI invocation -- and exits 0 open / 1 closed / 2 bad
# input. Stdlib Python: no jq, no yq. --max-infra-rate is stricter than the
# plan's infra_rate_abort (0.25) on purpose: a run may finish and still close
# here. Raise it for one job on a throttled provider; never lower the abort
# (run/SKILL.md SS3).
python3 "$EVALUP_ROOT/scripts/gate.py" reports/ --latest --mode regression \
  --max-infra-rate 0.05
```

`gate.py` lists **every** reason it closed; the docstring at the top of
`${CLAUDE_PLUGIN_ROOT}/scripts/gate.py` owns the list of closing conditions (a gating failure is one; so are an aborted run, a run that
scored nothing, and a run directory that fails `run_cases.py --verify`).
Canaries close nothing by default — a canary `fail` already aborted the run,
and one that reached no verdict is printed as NOT VERIFIED — so a release
pipeline that wants "no verified canary, no pass" adds `--require-canaries`
(not on a holdout run: `holdout` is exclusive of `canary`, so it always closes).
`--json` prints the same facts for a dashboard. Deterministic-only CI needs no
`claude -p` at all: `convert_suite.py` → `make_plan.py` → `run_cases.py` →
`gate.py` is four plain commands (`${CLAUDE_PLUGIN_ROOT}/examples/quickstart/demo.py` is that
pipeline, runnable). A pipeline may want `make_plan.py`'s overrides:
`--run-id <mode>-<YYYYMMDDTHHMMSSZ>` names the run directory (default: the mode
and UTC now; any other shape is refused), `--dataset-version N` labels the run
(default: `dataset.yaml`'s), and `--holdout-ledger` / `--judge-calibration`,
relative to `--state-dir`, move the two sidecars from
`datasets/holdout-looks.jsonl` and `judge/calibration.json`.

- `--bare` skips auto-discovery of hooks, skills, custom commands,
  subagents, installed plugins, MCP servers, auto memory and CLAUDE.md, so the
  same command behaves the same on every CI runner — reproducibility over
  convenience. Two consequences the flag does not advertise: installed
  plugins are skipped too, so this one must be named with `--plugin-dir`
  (without it `/evalup:run` does not resolve); and auth is strictly
  `ANTHROPIC_API_KEY` (or an `apiKeyHelper` passed via `--settings`) — an
  OAuth login fails with "Not logged in" before any token is spent, which is
  what the first check catches. One oddity seen on 2.1.282: the init event's
  `skills`/`slash_commands` arrays can still enumerate skills from
  `~/.claude`. Nothing shows they were loadable; the check that matters is
  the one above — `plugins` names `evalup`.
- `--output-format stream-json --verbose` is what makes the `system`/`init`
  event exist. `--output-format json` is a single final `result` object with
  no init event at all — the `jq` above finds nothing in it and exits 4, so
  the guard would reject every healthy run. Print mode refuses `stream-json`
  without `--verbose`.
- Permissions: headless, a prompt nobody answers is a denial, and
  `--permission-prompts none` makes that explicit — anything that would
  prompt is denied and Claude is told not to retry it. The skill
  pre-approves its own scripts for this one turn, and `acceptEdits` covers the
  two files the skill itself writes, `baseline.json` and `report.md`. Add
  `--allowedTools` only for what your adapter needs beyond that (README
  §Permissions). `--permission-mode dontAsk` is not a substitute: it denies
  every call that would prompt, the report write included, so the run stops
  there. SKILL.md §2's cost/time confirmation is skipped under `-p` — there
  is no human on the other end to answer it.
- The two checks before `gate.py` are in that order for a reason. The exit
  code and `result.is_error` say whether a session happened at all; the
  `system/init` check says whether the plugin it ran under loaded clean. A
  run launched under a broken plugin load can produce a clean-looking
  verdict for the wrong reason (e.g. every case silently skipped a layer),
  and the gate script would happily green-light it. `plugin_errors`,
  `mcp_server_errors` and `plugins` are documented on the init event; the
  two error keys are present only when non-empty.
- Run this recipe from a terminal or a CI job. A Claude Code session in auto
  mode may refuse to spawn the nested `claude -p` (README §Permissions).
- `--latest` assumes this CI job is the only writer of that mode at that
  moment — a shared `reports/` directory with concurrent runs needs the run
  directory passed explicitly (`gate.py reports/<run-id>`) rather than
  inferred by recency.
- Note the zero-failure branch (SKILL.md §5) surfaces here as
  `gating_failures == 0` — a legitimate, common, good outcome. The one
  special case is `gate.py`'s own: zero failures over zero scored cases is an
  absence, not a pass, and closes the gate.
