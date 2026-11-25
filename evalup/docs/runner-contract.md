# `run_cases.py` — Runner Contract

The spec of `scripts/run_cases.py` (tests: `tests/test_run_cases.py`). Where
this file and the code disagree, **this file is right** and the code is the
bug. It holds rules only; the reasons behind them are in the git history of
this file.

Two constraints hold throughout:

1. **The runner shells out to the existing scorers and reimplements no
   scoring.** It materializes each scorer's inputs, invokes it, and records
   its output verbatim.
2. **Stdlib-only, Python 3.9 floor.** The case list arrives as **JSON**; the
   calling skill converts YAML → JSON (`convert_suite.py`), and that
   conversion is the run's parse.

§9 (completeness) is the check every other section exists to make
well-defined.

---

## 1. Invocation

```
run_cases.py --plan <plan.json|-> --out <reports/<run-id>> [--resume] [--dry-run]
                             [--baseline-verdicts <prev>/verdicts_for_stats.jsonl]
                             [--not-before <ISO time>] [--cost-from <cost.json>]
run_cases.py --verify <reports/<run-id>>
run_cases.py --probe --adapter <state>/adapter.yaml --message "<text>"
                             [--insecure-tls] [--timeout SECONDS]
```

| Flag | Meaning |
|---|---|
| `--plan PATH` | the single JSON input document (§2). `-` reads stdin. Required except with `--verify`. |
| `--out DIR` | the run directory to write. Must equal `<state>/reports/<run_id>` where `run_id` is the plan's. Required except with `--verify`. |
| `--resume` | continue an interrupted run into an existing `--out` (§8). Without it, a non-empty `--out` is exit 2. |
| `--verify DIR` | run **only** the completeness check (§9) over an existing run directory and exit. No app calls, no scoring, no writes. |
| `--dry-run` | plan validation + pre-flight (§4) + print the resolved case list and cost inputs; write nothing, call the app zero times. The health check (§4.3) is the one pre-flight step it skips. The cost inputs: `app_calls_planned` (a call per case or conversation turn, × `k`, no retry), `app_calls_max` (that × `execution.max_attempts`: every attempt retried, a conversation from turn 1) and `wait_floor_s` (seconds waited beyond the app's latency with no retry: `quiescence_ms` after each call when traces come from a store — none for `traces.source: response` — plus whatever part of `turn_delay_s` that window has not already covered between turns, since the delay runs from the previous reply). With `invocation.rate_limit` declared (§7), also `rate_limit`, `pace_floor_s` and `pace_max_s`: the least seconds that budget lets `app_calls_planned` and `app_calls_max` calls span, the health check aside. Not added to `wait_floor_s`. Absent with no budget. A `pace_floor_s` ≥ 1800 s adds `resume_expected` (#144). |
| `--not-before TIME` | start no earlier than TIME (ISO 8601 with a UTC offset — `Z`, `+HH:MM` or `+HHMM`, any fraction ignored — at most 24 h ahead; else exit 2). The runner waits after plan validation and the `--out` check, before pre-flight, **writing nothing**: a runner killed while waiting leaves no run. `started_at` is when the wait ended. With `--dry-run`: no wait, and the report adds `not_before` and `start_wait_s`. `wait_run.py` treats an absent `--out` as `not_started` (exit 3) unless `ps` shows no runner naming it. |
| `--cost-from PATH` | `--dry-run` only (else exit 2): a run's `score_cost.py` output. The report's `estimate` always carries `messages`, `messages_max` and `models` (`manifest_extra.app.model`, or the cost file's); `usd`, `usd_max` and `usd_per_message` only when that run is `priced`: its `cost.all_repeats.usd` over `messages_with_usage` when `priced` (#129), else its `cost.usd` over the messages its priced cases sent (a case whose trajectories `score_cost.py` would price; a conversation counts its `turns_sent`), × this plan's messages (`rate_basis`). Otherwise null. No price is assumed. A rate from another k is caveated in `note`. |
| `--baseline-verdicts PATH` | a previous run's `verdicts_for_stats.jsonl`. Shells out to `stats.py` after scoring and writes `comparison.json` (§5.6). |
| `--probe` | send **one** request through the adapter and print what came back; no plan, no scoring, no writes (§1.1). Runs alone, with `--adapter` and `--message`, plus optional `--insecure-tls` and `--timeout`, which are probe-only flags. |
| `--version` | `evalup harness <HARNESS_VERSION> (scoring <SCORING_VERSION>)`, via `_common.add_version_flag`, same string as every scorer. |

There are no other flags. Mode, k, filters, and selection are **already
resolved** in the plan, and a run does not read `adapter.yaml`,
`profile.yaml`, or `datasets/**` off disk (`--probe` is the one mode that
reads an adapter, and it is not a run). Selection is the skill's job
(`run/SKILL.md` §0).

### 1.1 `--probe`: discover's live check (#49)

Discover verifies an adapter with 1–3 live requests through this flag, so
`cookie_env`/`token_env` and `${VAR}` refs are resolved by the runner (§3),
never in a shell command.

- **Input.** `--adapter` is the state location's `adapter.yaml`, handed to
  `convert_suite.py --adapter-file` in a subprocess so the runner still parses
  no YAML (§13), or an adapter JSON (a bare adapter, or a document whose
  `adapter` key holds one, like `convert_suite.py`'s output). Its directory
  is the state location for `invocation.app.repo` in `function` mode.
  `--message` is the one user turn.
- **Path.** The run's own pre-flight (§4) with `--dry-run`'s rules — so no
  health-check call and nothing written — then one `invoke_once` with
  `max_attempts: 1`: the same env resolution, auth, headers, body template,
  SSE decode and answer extraction a case gets, and with `traces.source:
  response` the same tool-call read. A trace store is neither checked nor
  joined; a declared correlation's trace id is still read and shown. An
  unimplemented store (jaeger, tempo, clickhouse) is still refused, as a run
  would. Under `conversation.history` it sends turn 1, then turn 2 (the
  same `--message`) carrying its rebuilt reply (§7.1), in `history_check`;
  a turn 2 not 2xx, or a turn 1 that cannot be carried, is exit 3. A declared `invocation.health_check` is not sent;
  `traces.health_check` says so.
- **Output**, on stdout, always JSON in one shape once the adapter is read
  — a pre-flight failure too, with `request`/`response` null and its own
  keys (`missing_env`) added: `ok`, `problems` (a list, empty when
  verified), `env_vars` (every env var the adapter needs: its `${NAME}` refs
  plus the `token_env`/`cookie_env` names), the request as a run records it
  (headers redacted, refs put back), and `response`: `status`, `latency_s`,
  `answer`, `trace_id` (a declared correlation's; null under `traces.source:
  response`, whose ids are minted), `tool_calls` (null unless `traces.source:
  response`), and `body_preview` (the first 4,000 characters of the body as
  JSON, `body_truncated` saying whether that was all of it).
- **Secrets.** Stricter than §3's record. Every resolved env value of four
  characters or more, and every auth value whatever its length, is put back
  as `${NAME}` *everywhere* in the output, the app's answer and body
  included. The body is scrubbed as a structure before it is dumped and cut,
  so JSON escaping and the 4,000-character cut cannot leave part of a value.
  A value bound for a header or the URL that holds a CR or LF is exit 3
  naming the variable, before any send, and so is any control character
  (tab, NUL…). The scrub also matches each value's JSON-escaped and `repr`
  forms; an auth value is any `${NAME}` under `invocation.auth` plus
  `token_env`/`cookie_env`. Function mode's own prints go to stderr, so
  stdout stays one report, and no outcome prints a traceback.
- **Exit.** 0 verified: a 2xx, a non-empty answer at the `<answer>` field, a
  whole stream when `response_format: sse`, a readable tool-call list when
  `traces.source: response`. 3 with the same report plus `error` when any of
  those fails, or when pre-flight does (an unset env var is §4's
  `missing_env` list, with no app call). 8 (#124) when the app refused the
  request: a 4xx other than 401, 403, 404, 405 and 407, not an origin/CSRF
  guard's, with a non-empty answer at the `<answer>` field and nothing else
  wrong. `problems` is then empty, `ok` false, and `warnings` names the
  status (and a refusal's unread tool-call list, which is not a problem
  here). The request reached the app and the response template read its
  reply, but the 2xx path is unverified: probe again with an in-scope
  question. A 429 or 5xx is a transport error, so exit 3. 2 for bad input.
  It scores nothing. Every report carries `warnings`, empty unless exit 8.

---

## 2. Input: `plan.json`

One document, all of it JSON, produced by `make_plan.py`. Unknown top-level
keys are a hard error (exit 2).

```jsonc
{
  "plan_version": 1,

  "run_id": "smoke-20260818T183920Z",   // <mode>-<UTC YYYYMMDDTHHMMSSZ>
  "mode": "smoke",                       // smoke|regression|targeted|holdout ("full" is refused: removed, #42)
  "k": 1,                                // repeats per case, >= 1
  "gate": "soft",                        // soft|hard|decision — recorded, never enforced here (§7)
  "selecting_split": "smoke",            // the split this mode selected on; becomes `set` in verdicts.jsonl

  "paths": {
    "scripts_dir": "/abs/path/to/evalup/scripts",   // ${CLAUDE_PLUGIN_ROOT}/scripts
    "state_dir": "/abs/path/to/.evalup",            // for reports/.gitignore and the holdout ledger
    "holdout_ledger": "datasets/holdout-looks.jsonl",   // .jsonl sidecar, relative to state_dir; null = runner refuses --holdout (§6)
    "judge_calibration": "judge/calibration.json"       // .json sidecar from score_agreement.py --write, relative to state_dir; null = the judged gate stays shut (§5.4)
  },

  "adapter": { ... },        // adapter.yaml converted to JSON, VERBATIM, env refs UNRESOLVED (§3)
  "capability_matrix": {     // profile.yaml's block, verbatim
    "routing":        {"enabled": true},
    "trajectory":     {"enabled": false, "blocked_by": "no trace-id correlation"},
    "tool_selection": {"enabled": false, "blocked_by": "no tool-call log"},
    "cost_latency":   {"enabled": false, "blocked_by": "..."},
    "multi_turn":     {"enabled": false, "blocked_by": "adapter declares no invocation.conversation"},
    "answer_quality": {"enabled": true, "judged": "provisional"}
  },

  "cases": [ { /* the full case object, case-format.md, YAML->JSON */ } ],

  "scoring": {
    "oos_route": "none",              // score_routing --oos-route; null = omit the flag
    "id_pattern": null,               // score_authz --id-pattern; null = built-in shape
    "float_tolerance": 0.0,           // score_execution --float-tolerance
    "repeat_threshold": 3,            // detect_loops --repeat-threshold
    "call_budget": 15,                // detect_loops --call-budget
    "fail_on_errored_calls": false,   // trajectory_match --fail-on-errored-calls
    "scorer_timeout_s": 30            // per scorer invocation; exceeded => layer `error` (§5)
  },

  "execution": {
    "timeout_s": 120,                 // per app call; default: adapter invocation.timeout_s
    "max_attempts": 3,                // total attempts per repeat, including the first (§7)
    "backoff_s": [1, 4],              // waits before attempts 2 and 3; len == max_attempts-1
    "infra_rate_abort": 0.25,         // abort the run when infra verdicts exceed this share of attempted cases
    "insecure_tls": false,            // opt-in only; self-signed local hosts (the field test's `curl -k`)
    "max_turns": 12                   // a conversation with more turns is `skipped` (§7.1); >= 2
  },

  "manifest_extra": {
    // values the runner cannot compute; these seven keys are copied into manifest.yaml
    // (environment_kind comes from adapter environment.kind); any other is dropped
    "dataset_version": 1,
    "app": {"name": "...", "repo": "...", "git_sha": "dccea614", "git_tree": "dirty",
            "model": {"classifier": "...", "executor": "..."}},
    "prompt_snapshot_hashes": {"classifier_system": "bd13...", "...": "..."},
    "judge": {"model": null, "status": "uncalibrated"},
    "rubric_versions": "n/a",
    "temperature": "app-side (0.0/0.1); harness sets none",
    "cost_estimate": {"note": "..."}
  }
}
```

**Required vs. defaulted.** `plan_version`, `run_id`, `mode`, `k`, `gate`,
`selecting_split`, `paths`, `adapter`, `capability_matrix` and `cases` are
required. `scoring`, `execution` and `manifest_extra` may be omitted and are
filled from the defaults shown above. `capability_matrix` has no default:
absent, every layer would count as enabled.

**Validation, all before any spend, all exit 2 with `{"error": ...}` on stdout:**
unknown top-level key; unknown key inside `scoring` or `execution`;
`plan_version != 1`; `run_id` not matching
`^[a-z]+-\d{8}T\d{6}Z$`; `--out` basename != `run_id`; `k < 1`; empty `cases`;
duplicate case `id`; a case whose `split` does not contain `selecting_split`;
`backoff_s` length mismatch; `infra_rate_abort` outside `(0, 1]`;
`max_turns` not an integer >= 2;
`scripts_dir` missing any of the nine scorer
scripts §5 names; a holdout-touching mode with `holdout_ledger: null`, or one
whose path is not `.jsonl` (§6); and
**`adapter.invocation.max_concurrency > 1`** (§7).

The runner does **not** re-lint the suite; `validate_cases.py` owns that. It
checks only what it needs to execute.

---

## 3. Secrets and env resolution

`adapter` arrives with `${VAR}` references **unresolved**; the runner
resolves them itself from `os.environ` at pre-flight, and a resolved value is
never written into the plan.

- Resolution is literal `${NAME}` substitution inside string values, anywhere in
  `adapter`. Unset or empty → exit 3, listing **every** missing name at once
  (not the first).
- A resolved value never reaches disk. `request.json` records header **names**
  and writes `"<redacted>"` for the value of any header whose value came from an
  env ref, or whose name appears in the adapter's auth block. Non-secret headers
  (`Content-Type`, an `invocation.headers` value written literally) are
  recorded literally.
- The same holds for what is sent **verbatim**: an env ref inside
  `request_body`, `base_url` or `endpoint` is resolved for the wire and put back
  as `${NAME}` in the record — `request.json`'s `body` and `url`, the manifest's
  `invocation.url`, and `--dry-run`'s output. The reference is restored in the
  *template*, before the case's placeholders are rendered, so a short value
  never rewrites a user turn that contains it.

---

## 4. Pre-flight (writes nothing but the manifest)

In order. Any failure here is exit 3 and **zero app calls have been billed**
beyond the health check.

1. Plan validation (§2) and env resolution (§3).
2. `invocation.mode`: `http`, `function` and `exec` are implemented (`exec`
   per item 2d). The reserved name `cli` exits 3 pointing at `exec`;
   any other name exits 3 with `runner v1 implements invocation.mode: http,
   function and exec only (got: …)`.
   **`function` mode does not enforce `execution.timeout_s`**: the callable
   owns its own timeout, and `manifest.invocation.timeout_enforced` records
   `false`.
2b. **`invocation.response_format`**: `json` (default) or `sse`, http mode
   only. With `sse`, `invocation.sse.protocol` is `ag-ui` or `data-path`
   (which needs `text_path`, the dotted path of the text delta in each frame,
   and takes an optional `done` sentinel such as `[DONE]`). A `sse` block
   without `response_format: sse`, the reverse, an unknown key, or `sse` in
   function mode is exit 3. At run time a `text/event-stream` reply is decoded
   by `scripts/_sse.py` into the body
   `{text, partial_text, tool_calls, error, finished, interrupted, usage,
   run_usage, sse}` (`tool_calls` for `ag-ui` only: `{id, name, arguments,
   result}`, arguments joined from their deltas; and `items`, §7.1's
   stream order). An AG-UI server may emit one
   `RUN_FINISHED` per model call: `run_usage` holds each `RUN_FINISHED` /
   `RUN_ERROR` usage that was reported, `usage` their sum (null when none
   was), and `traces.source: response` writes one chat span per entry, so
   `llm_calls` counts the calls. Cache counts (`cached_input_tokens`,
   `cache_write_input_tokens`, parts of `input_tokens` in AG-UI's
   accounting) appear only when the stream reported them; `normalize_trace.py`
   keeps them in the trajectory as `cache_read_input_tokens` /
   `cache_write_input_tokens`. An `ag-ui` stream read with `traces.source:
   response` is the one source whose cache counts `score_cost.py` splits out
   of the input price (§5.7); a `data-path` stream is not. `response_body` marks the answer in that body,
   usually `{"text": "<answer>"}`. Any other content type parses as JSON exactly
   as without the block, so an app's JSON 4xx still reaches the `http` layer.
   `text` is null unless the run finished without an error (`RUN_FINISHED` for
   AG-UI, the `done` sentinel or EOF for `data-path`). The manifest records
   `invocation.response_format` and `invocation.sse`.
2c. **`invocation.headers`** (#50): `{name: value}` every request carries
   beside the auth (each case, each turn, the health check, `--probe`).
   Http mode only. A
   name that is not an HTTP token, an empty or non-string value or one
   holding a control character, a name the runner, `auth` or `identity_map`
   already sets (`Content-Type`, `Host`, `Content-Length`,
   `Authorization`, `Cookie`, an `auth.headers` or `identity_map` name), or a
   credential-like name (`token`, `csrf`, `key`, …) with an inline value
   instead of a `${REF}`, or two names equal but for case, is exit 3:
   refused, never merged or dropped. So is an env var feeding a header or
   the URL that holds a control character (CR, LF, tab).
2d. **`invocation.mode: exec`** (#39): one process per app call, from
   `scripts/_exec.py`. `invocation.exec` is `{argv, cwd, env}`:
   - `argv` is a **list** of strings, never a shell string — no shell runs
     it. A `${REF}` in `argv` or `cwd` is exit 3, checked *before* env
     resolution.
   - `cwd` is **required** and resolves against the state location
     (adapter.yaml's directory), never `app.repo`: with `repo: .` and state in
     `<app>/.evalup/`, the app root is `cwd: ..`. It is made absolute; the
     probe reports it as `request.cwd`, and the health detail names it.
     `argv[0]` with a path separator is a file relative to `cwd`; a bare name
     is looked up on the PATH the child gets. Either must resolve to an
     executable at pre-flight (exit 3, nothing started) — exec's health
     check.
   - The child's environment is an **allowlist**: `PATH`, `HOME`, `USER`,
     `LOGNAME`, `SHELL`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `TMPDIR`/`TMP`/
     `TEMP` (and Windows' `SYSTEMROOT`, `COMSPEC`, `PATHEXT`, `USERPROFILE`,
     `APPDATA`, `LOCALAPPDATA`, `SYSTEMDRIVE`, `WINDIR`, `PROGRAMDATA`) when
     set, plus the names in `exec.env`. A listed name unset or empty is exit 3 with
     the others in `missing_env` (§3). A secret goes by name in `exec.env`
     or as a `${REF}` in `request_body` (recorded as `${NAME}`, §3), never
     in argv.
   - The request is `request_body`, rendered as for http, written as one JSON
     document and a newline to stdin, then EOF. stdout must hold exactly one
     JSON **object**; `response_body` marks its `<answer>` (and optionally
     `<trace id>`), and from there it is read as an http JSON body —
     `session_from.body`, `traces.source: response`, `result_from_response`.
     A success has `status` 200; a `status` field in its stdout is body
     data, never read as one. A deliberate refusal is exit 0 with a JSON
     body, observed through `route_from_response` (or
     `clarify_from_response`); a non-zero exit is always a crash. So
     `route_from_status` is exit 3 in exec mode, and a case whose
     `expect.http.status` (top-level or on any turn) is anything but 200 is
     **skipped** with that reason, as a case with an identity and no
     `identity_map` is (§7). A 200 check still runs — it means the
     command answered.
   - The answer is recorded as the command printed it: a secret the command
     echoes on stdout lands in `response.json` and `answer.txt`, as a secret
     an http app echoes in its body does. Only what the runner quotes
     itself (stderr, a bad stdout's start) is scrubbed.
   - `execution.timeout_s` is enforced (`timeout_enforced: true`): the call
     runs in its own process group (session), and the group is killed
     (SIGKILL) at the timeout and after the command exits. A process that
     left the group (`setsid`, a daemon) is out of reach and may outlive the
     call. Each pipe is drained by a thread that owns its descriptor, and
     the runner gives the pipes 2 s past the command's exit (or the kill) to
     close, so a call takes at most `timeout_s` + 2 s. A stdout still open
     then is a failed call; a held stderr is ignored. The wait after SIGKILL
     is bounded by the same grace; a process that still has not died is
     abandoned and the call fails. `latency_s` is measured at the command's
     exit. On Windows only the command itself is killed (untested there).
   - A **SIGTERM** to the runner during a call unwinds through the same
     cleanup (the group is killed), then is re-delivered, so the runner
     still dies by SIGTERM; only when SIGTERM is at its default action. A
     **SIGKILL** to the runner orphans the call's group, which runs until it
     exits on its own (`timeout_s` no longer applies) — kill it by hand.
   - stdout over 16 MiB is a failed call; stderr's last 64 KiB are kept and
     only its last 2,000 characters are quoted in an error, scrubbed — in
     plain, JSON-escaped and `repr` form — of every `exec.env` value and
     resolved `request_body` ref of 4 characters or more. The text is
     scrubbed before it is cut, and a stderr window that started
     mid-secret first loses as many characters as the longest secret.
   - Failures are transport errors (§7's infra, retried like any): a timeout
     is `timeout`; a non-zero exit or a signal is a `connection` error and
     feeds `crash_rate`; a command that cannot start, or stdout that is empty,
     oversized, not JSON, or JSON but not an object, is `transport`.
   - Refused in exec mode (exit 3): `auth` other than `none` (a `token_env`
     or `cookie_env` credential goes in `exec.env` by name; the message
     says which), `route_from_status`, a trace store as `traces.source`
     (`otlp-file` or a reserved one; tool calls come from `traces.source:
     response`, or the run is trace-less), `identity_map`,
     `headers`, `health_check`, `response_format: sse`, and
     `conversation.style: cookie` or `session_from.header`. `request.json`
     records `method: EXEC`, `url: exec:<argv, shell-quoted>` and no
     headers; the manifest adds `invocation.exec: {argv, cwd, env}` (env
     NAMES only).
3. **Health check**: one trivial request through the adapter. With no declared
   `invocation.health_check`, this is a `GET` of `base_url` and **any** response
   counts as reachable — including a 404. A transport failure or timeout
   is exit 3. An adapter that wants a real check declares
   `invocation.health_check: {method, path, expect_status: [...]}`, and a
   status outside `expect_status` is exit 3. In `function` mode the health
   check is the entrypoint import, so an unimportable entrypoint is exit 3
   with nothing billed.
4. **Trace branch**, off `adapter.traces`. The queryable set in v1 is
   **`otlp-file` only**; `jaeger`, `tempo` and `clickhouse` exit 3 with
   `runner v1 queries traces.source: otlp-file only (got: 'jaeger'); declare
   view-only to run trace-less instead`. An unimplemented store is never
   treated as "no trace".
   - `source` in the queryable set **and** `correlation` explicit → verify a
     trace arrives and joins on the health-check call. No join → exit 3. The
     join requires a declared `invocation.health_check`; without one the
     runner exits 3 asking for it.
   - `source: response` → tool calls are read off each response (an `sse`
     body's `tool_calls`, or the list at `traces.tool_calls_path`, default
     `tool_calls`) and written as `gen_ai` spans to the case's `trace.json`,
     which `normalize_trace.py` reads as usual. No location, correlation,
     convention or health-check join applies: the trace id is minted per
     response. A stream that reported a run error or ended before its
     terminal event, or a body with no list at the path, is
     `infra_incomplete` for the trace layers — an incomplete list never
     reads as "called nothing".
   - `source: view-only|none`, **or** `correlation: none`, **or**
     `convention != gen_ai` with no `mapping_shim` → **trace-less mode** for the
     whole run. Record `traces.collected: false` plus `disabled_layers` in the
     manifest. Raw non-`gen_ai` spans are never passed to `normalize_trace.py`
     (adapter hard rule 5).
4b. **Conversation block** (docs/multi-turn.md §2): when the adapter declares
   `invocation.conversation`, check it — `style` one of
   `client-id|server-id|cookie|none` (`none` only beside `history`); `session_from` exactly one of `body` (a
   dotted path) or `header` for `server-id` and absent otherwise; a request
   body carrying `<uuid>` (client-id) or `<session>` (server-id); `header`
   and `cookie` in `http` mode only; `turn_delay_s >= 0`; `memory` one of
   `session|user`; no unknown key. `history` (docs/client-history.md §2):
   `preset` (`ag-ui|openai`) or the `user`/`assistant` templates (JSON
   objects with `<text>`), not both; `tools` `text-only|carried`, where
   `carried` needs the three tool templates and an `ag-ui` stream or a JSON
   body under `traces.source: response`; no `${REF}`. `<history>` sits in
   the body exactly once, as a whole string value, and only beside
   `history`; `invocation.history` is refused. Any defect is exit 3. The
   block is recorded as `manifest.conversation` (null when undeclared;
   `history` expanded).
5. **Safety gates**: record `environment.safe_to_attack` and the per-tool
   `side_effects` classes. `never-live` tools present with
   `environment.kind: live-*` → categories that could trigger them are
   `skipped`, not run (adapter hard rule 2).
6. If `adapter.data.may_contain_pii` is true, write
   `<state>/reports/.gitignore` containing `*/cases/` **now** — before the first
   case file of the run exists.
7. `mkdir -p <out>/cases`, then write `<out>/manifest.yaml` (§10). The manifest
   is written before the first case, unconditionally, whether or not a baseline
   exists.
8. Write `<out>/results.json` with `summary.status: "running"` and an empty
   `cases` list. A run directory never exists without a results.json.

The cost/time estimate and the "Proceed?" prompt stay in the **skill**; the
runner never prompts.

---

## 5. Scoring: the layer table

For each case the runner walks this table top to bottom. A layer is
**applicable** iff its trigger field is present on the case; it is
**enabled** iff its capability-matrix entry's `enabled` is not `false`
(which entry is the second rule under the table).

| Layer | Trigger on the case | Script | Argv | Runner must materialize |
|---|---|---|---|---|
| `http` | always | *(none — runner compares)* | — | observed status vs `expect.http.status`; absent expectation ⇒ any 2xx passes. A 5xx never reaches this comparison: it is retried and, exhausted, is `infra_error` whatever the case expects (§7) |
| `trajectory` (normalize) | trace collected | `normalize_trace.py` | `--trace-id <id> spans.json` | `spans.json` from the trace store |
| `routing` | `expect.route` or `expect.route_acceptable` | `score_routing.py` | `routing_results.jsonl [--oos-route X]` | **run-level**, once, after all cases (§5.2) |
| `trajectory` | `expect.tools` | `trajectory_match.py` | `trajectory.json expect.json [--fail-on-errored-calls]` | `trajectory.json`, `expect.json` |
| `tool_selection` | `expect.args` | `score_args.py` | `trajectory.json expect.json` | same two files |
| `loops` | trace collected | `detect_loops.py` | `trajectory.json [--repeat-threshold N] [--call-budget N]` | `trajectory.json` |
| `answer` | `expect.answer` or `expect.format` | `score_answer.py` | `answer.txt expect.json` | `answer.txt` (final answer text), `expect.json` |
| `execution` | `expect.result` | `score_execution.py` | `actual.json expect.json [--float-tolerance F]` | `actual.json` per §5.3 |
| `authz` | `expect.authz` | `score_authz.py` | `trajectory.json expect.json [--answer answer.txt] [--id-pattern P]` | `trajectory.json`, `expect.json`, `answer.txt` |
| `rules` | `expect.answer.rules` | *(no script)* | — | `unscored`, `reason: "business rules are evaluated by the skill"`. After the run, `rule_report.py` attributes the `answer` row's checks and the judge pass's criteria to rule ids (#26); it writes `rules.json` beside the tree, never a file in it, and this row stays as written |
| `state` | `expect.state` non-null | *(no script)* | — | `unscored`, `reason: "state-diff is RESERVED: no scorer compares environment snapshots, and the runner never invokes environment.seed/.reset/.snapshot_state"` — on **every** path where the matrix leaves `state` enabled, whatever the adapter declares. The profile schema ships `state: {enabled: false}`, and a disabled row is `unscorable` with the matrix's own reserved reason first, like any other; §5.4 |
| judged | `expect.answer.rubric` | *(no script)* | — | `unjudged (mode: <mode>)` for smoke/targeted; else `unjudged (judge not calibrated)` unless `judge.status == calibrated` **and** `paths.judge_calibration` backs it (§5.4), in which case `unjudged (deferred to skill)` |
| `reliability` | `k > 1` | `reduce_repeats.py` | `repeats.jsonl [--k N]` | **run-level**, §5.5 |

**Not applicable ⇒ `"n/a"`. Applicable but disabled ⇒ `"unscorable"` with
`blocked_by` copied from the capability matrix. Applicable, enabled, but its
input could not be produced ⇒ `"unscored"` with a `reason`. None of these three
is ever `pass`, and none is ever `fail`.**

Four rules make that mechanical:

- **Every layer in the table gets a row on every case**, `n/a` included.
- **Enabled is `capability_matrix[<key>].enabled is not false`**, where
  `<key>` is the layer's own name with one exception: `answer`, `rules` and
  `judged` are all looked up under `answer_quality`. `http` and `loops` have
  no key, so nothing in the matrix, `make_plan.py --layer` included,
  switches them off. The map is `_common.MATRIX_KEY_OF_LAYER`, shared by the
  runner and `make_plan.py`. `blocked_by` is *copied*, never invented.
- The decision order per layer is **not applicable → disabled → input
  unavailable → invoke the scorer**, and a **refusal** (safety gate) or a
  **transport failure** stops before all of it.
- The scorer's object is stored **verbatim** under `layers.<name>`, so
  `layers.tool_selection` holds an object whose own `"layer"` field reads
  `"args"` — `score_args.py`'s name for itself. The runner never edits a
  scorer's output.

**A missing answer is `unscored`, not a content failure.** When the response
carries no value at the adapter's declared `<answer>` path, `score_answer.py`
is not invoked.
An SSE answer (`invocation.response_format: sse`, decoded by `scripts/_sse.py`)
is present only when its run finished without an error; a stream that reported
a run error or stopped before its terminal event is `unscored` with that
reason, and its partial text stays in `response.json` as `partial_text`.

**`authz` grades the tool-call log, never the chat text.** `answer.txt` is
passed only as secondary evidence of a forbidden id leaking into the prose; it
never decides `expect_refusal`, and an empty one counts as no evidence. The
default id recognizer sees only `letters[-_]digits` tokens (`INV-1042`), so an
app whose records are integer keys, UUIDs or separator-less ids scores its
`allowed_record_ids` checks `unscorable` (`forbidden_record_ids` is a literal
search and is unaffected). `scoring.id_pattern` comes from the profile's
`record_id_pattern`: when allowed-id checks come back uniformly `unscorable`
on an app that plainly returns ids, that field is unset or wrong.
`validate_cases.py` warns `unrecognizable_record_ids` on those cases at
generate time.

### 5.1 Invoking a scorer

```python
proc = subprocess.run(
    [sys.executable, str(scripts_dir / script), *argv],
    capture_output=True, text=True, timeout=plan["scoring"]["scorer_timeout_s"],
)
```

- **Parse `proc.stdout` as JSON on every return code, including 2.** The
  scorers' error contract puts errors on **stdout** as `{"error": "..."}` and
  signals failure with exit 2 (`_common.die`).
- `rc == 0` → store the parsed object verbatim as that layer's value in
  `verdict.json` under `layers.<name>`. The runner reads only `verdict` out of
  it for rollup; it invents no fields and drops none.
- `rc == 2` → layer verdict `error`, with `scorer`, `argv`, and the scorer's
  `error` string recorded. The case rolls up to `unscored`. Increment
  `summary.scorer_errors`. The run **continues** but exits 7 at the end (§11).
- Timeout or unparseable stdout → same as `rc == 2`, with the reason recorded.

### 5.2 `score_routing.py` is run-level

It scores a JSONL of all cases at once and reports macro/micro F1, a confusion
matrix, OOS metrics, and `spurious_labels`. (`micro_f1` is the confusion
matrix's accuracy — not the `accuracy` field, which counts accepted alternates
the matrix excludes; the scorer's docstring says why both stay.) The runner
accumulates one row per routing-applicable non-canary case as cases complete,
writes `<out>/routing_results.jsonl`, and invokes the scorer **once**, after
the last case, before finalize.

Row shape, mapping the case's `expect.route` / `expect.route_acceptable` onto
the scorer's `expected` / `acceptable`:
`{"case_id", "expected": <case.expect.route>, "observed": <observed>,
"acceptable": <case.expect.route_acceptable>, "clarify_ok", "clarified"}`.
A null `observed` is written as `null` and the scorer maps it to `__no_route__`.

The run-level report goes to `<out>/routing_report.json`; each case's
`layers.routing` gets the per-case row plus the verdict derived from it.

`--oos-route` is passed only when `scoring.oos_route` is non-null **and** at
least one selected case carries that route label — otherwise the scorer exits 2
and lists the labels present. The runner performs that check itself and omits
the flag rather than discovering it from an exit code.

**Where the observed route comes from**, in this order, and every source
declared by the adapter:

1. `invocation.route_from_response: "<dotted path>"` — a structured field in
   the response that names the route. Recorded as `observed_from: "response"`.
2. A collected trace: the **first `invoke_agent` span's `gen_ai.agent.name`**,
   recorded as `observed_from: "trace"` (the adapter's
   `traces.convention: gen_ai` declares where agent names live).
3. `invocation.route_from_status` (below), recorded as
   `observed_from: "status"`.

With none of the three, `layers.routing` is `unscored` and the case is excluded
from `routing_results.jsonl`.

**A conversation adds no row** (docs/multi-turn.md §4): `routing_report.json`
is single-turn routing only. Each turn's routing layer still scores
(`layers.t<n>.routing`), and its row stays in `turns/<t>/verdict.json`.
`observed_from` is on every scored routing layer.

**`clarify_ok` needs `invocation.clarify_from_response`.** A case with
`expect.clarify_ok: true` and no declared way to observe whether the app
clarified is `unscored`, with that as the reason.

**Trace-less routing.** With no trace the runner never *infers* a route. An
adapter may declare the inference per app:

```yaml
invocation:
  route_from_status: {200: "<answered>", 400: "__oos__", 403: "denied"}
```

and when it does, the runner maps the observed status through it and scores
routing normally, recording `layers.routing.observed_from: "status"`.
Statuses absent from the map yield no observed route. Without the block,
`layers.routing` is `unscored`, `reason: "no trace; observed route
unavailable"`, and the case is excluded from `routing_results.jsonl`.

The observed route is then the map's **value** — `<answered>`, `__oos__`,
`denied` — never a domain name, so on such an app a case must expect one of
those labels (its domain lives in `unit`), and `routing_report.json` measures
answered-vs-refused, not domain routing.
`validate_cases.py --adapter` warns (`route_not_observable`) on a domain label
the map cannot produce.

### 5.3 `actual.json` for the execution layer

Per the result-extraction contract (`skills/discover/references/oracle.md`), in priority order:
tool-result span → structured response field → declared prose pattern. The
runner writes exactly what the scorer expects:
`{"scalar": v}` | `{"rows": [...]}` | `{"missing": true, "reason": "..."}`.
`expected` is never computed at run time — it is already on the case as
`expect.result`, and `reference_query` is provenance the runner never executes.

The file is written for **every sent case whose `expect` has a `result` key**
— which covers every case §9(b) requires it of — including the two paths that
never reach `score_execution.py`: with the `execution` layer disabled the
result is still extracted, and when the app call failed (a transport error,
or a 5xx on every attempt) the file is `{"missing": true, "reason": …}`
naming that failure.

Each of the three priorities is an **adapter declaration**; the runner never
hunts the response for a likely field:

| Priority | Declaration | Reads |
|---|---|---|
| 1 | `invocation.result_from_tool: {tool, field}` | the last matching `execute_tool` span's result (JSON-parsed when the exporter recorded it as a string), optionally at a dotted path |
| 2 | `invocation.result_from_response: "<dotted path>"` | the response body |
| 3 | `invocation.result_pattern: "<regex>"` | the prose answer; group 1 if the pattern has one |

A list becomes `{"rows": [...]}`; anything else becomes `{"scalar": v}`. No
further coercion — `score_execution.py` owns the type-aware comparison
(`7 == "7" == "7.0"`). When nothing is declared, or nothing matches, the
runner writes `{"missing": true, "reason": ...}` naming which it was, and the
scorer turns that into `unscored`, never a mismatch.

### 5.4 What the runner refuses to do

Business rules, state-diff, and judged layers have no script. The runner records
them `unscored`/`unjudged` with a reason and **never** attempts them. (A row
whose matrix key is disabled is `unscorable` first, like any other: `rules` and
`judged` under `answer_quality`, `state` under `state` — §5's decision order.) No skill
fills those rows in afterward: the runner's artifacts stay as written, and
`verdict.json` is never rewritten. The run-time judge (`docs/judge-run.md`)
is a pass AFTER the run, in the run skill: `scripts/judge_run.py` writes its
own `judge/` directory beside these files (`judge/judged.json`, verdicts
`unjudged` / `provisional` / `calibrated`) and none of the files below, and
nothing on the decision path — `gate.py`, `stats.py`, `--verify`,
`run_history.py`, optimize — reads it.

**State-diff is RESERVED, not pending an adapter field.** The runner calls
`environment.seed`, `.reset` and `.snapshot_state` **nowhere** — an adapter may
declare all three and the `state` row is identical, so the row's reason names
the reservation. A case's `seed_state` is loaded by nobody (seed out of band
or accept ambient state), and `validate_cases.py` does not count
`expect.state` toward gradedness, so a state-only case is a
`no_graded_layer` ERROR rather than a `pass` off its `http` row.

**The judged gate is derived, not asserted.** `manifest_extra.judge.status`
(copied out of `profile.yaml`) is **necessary and not sufficient**: with
`judge.status: calibrated` the runner also requires
`paths.judge_calibration` to name a `score_agreement.py --write` sidecar
(`schema: evalup/judge-calibration/2`, or a `/1` written before it) whose own `status` is `calibrated`
and whose `rubrics_measured` covers every rubric a selected case pins in
`expect.answer.rubric` (matched with or without the `-v<version>` pin). Any
gap — path unset, file missing, wrong schema, uncalibrated, a rubric never
measured — yields that specific reason in place of `deferred to skill`.

Every one of those failures is a **reason string, never an exit code**. The
judged layer is `unjudged` in all of them and is never `pass`/`fail`.

### 5.5 Repeats (`k > 1`)

Each repeat is a full independent invoke + score. The runner accumulates
`<out>/repeats.jsonl` (`{"case_id", "verdict"}`, k rows per case) and invokes
`reduce_repeats.py` once at the end into `<out>/reliability.json` for the
pass@k / pass^k gap.

**A case contributes to `repeats.jsonl` only when all k of its repeats scored
`pass` or `fail`** (`reduce_repeats.py` refuses any other verdict, and a case
with fewer than `--k` rows). The excluded cases and their verdicts are named
in `reliability.json` under `excluded_cases`, never dropped quietly. When no
case qualifies, the runner writes `reliability.json` as `unscored` with that
reason rather than invoking the reducer.

Each repeat's scorer inputs are materialized in a **scratch directory outside
the run tree**, and only the representative repeat's are copied in: §6's
`repeats/<n>/` holds exactly three files.

A conversation's repeat re-runs the **whole** conversation, and the fold
below is unchanged: one conversation is one case (§7.1).

The **case-level** verdict for k>1 is `pass^k`: pass only if every repeat
passed. The gap is reported, never hidden. The fold, first match wins:

1. any repeat `fail` → `fail` (an observed failure already settles pass^k);
2. every repeat `pass` → `pass`;
3. any repeat `infra_error` → `infra_error`, then `infra_incomplete`;
4. otherwise `unscored`.

So `[pass, infra_error]` is `infra_error`, and so is `[infra_error, pass]`:
the order of the repeats never changes the verdict. A canary with an infra
repeat does not count as `passed`.

### 5.6 The baseline comparison

`--baseline-verdicts PATH` shells out to `stats.py` after scoring, pairing this
run's `verdicts_for_stats.jsonl` against the given one, and writes
`<out>/comparison.json`. Without the flag the runner emits
`verdicts_for_stats.jsonl` and stops. Its `keep` is `stats.py`'s posterior
bar alone (`noise_floor_applied: false`): the optimizer's holdout keep/revert
call is a separate `stats.py --noise-floor` run, never this file.

**The version and `k` checks run at plan validation, before any spend.**
They read the baseline's own `manifest.yaml` **beside** the verdicts file it
names; a baseline without one is exit 2.

The runner refuses the comparison (exit 2) when the `dataset_version` or the
scoring version differs, or when the two runs used a different `k`. It also
refuses a pair where exactly one side selected the `holdout` split (a
baseline manifest without `selecting_split` is not judged): the pair would
have zero common case ids.

The version key is `scoring_version` (`_common.SCORING_VERSION`, #38), bumped
only when a verdict's meaning changes; `harness_version` is the plugin's
release and is provenance only. A baseline manifest written before
`scoring_version` existed takes the scoring version its release implies
(0.1.0, the only release that wrote such runs, = `scoring_version` 1; an
unlisted release is compared on `harness_version` equality) — with a
`comparability_warning` on stderr and in `comparison.json` (or in the
exit-2 payload when it is refused); never a crash
(`_common.scoring_key`, the one rule `scoring_comparison` and
`run_history.py`'s series key share). A malformed field (`scoring_version`
not an integer, or a bool; no `scoring_version` and a `harness_version`
that is not a string) is never comparable, not even to itself: exit 2 here,
and `run_history.py` lists the run under `excluded_runs`.

Choosing the baseline (`reports/baseline.json`) stays a skill decision (§13);
the runner only ever compares the two files it is handed.

### 5.7 `cost_latency` is a run-level capability, NOT a layer

`capability_matrix.cost_latency` is a declared, enabled capability that has
**no row in §5's table and no entry in `LAYER_ORDER`**. The runner emits
nothing for it on any case; there is no `layers.cost_latency` row. It is
**run-level, like `score_routing.py` (§5.2)**, and computed *after* a run.

**What the matrix flag means.** `cost_latency.enabled` declares that this
app's traces carry `gen_ai.usage.*` and `gen_ai.request.model`, so its runs
can be priced. It is in `TRACE_DEPENDENT_LAYERS`, so a **trace-less run lists
it in `manifest.yaml`'s `traces.disabled_layers`**: such a run recorded no
token count, since `cases/<id>/trajectory.json` (§6, `# only when: a trace was
collected`) is the only cost input the tree has. Per-case `latency_s` is
recorded on every run, traced or not.

**Who computes it.** `scripts/score_cost.py`, invoked by `analyze` (§13: the
runner does not write reports). It reads a finished run directory — plus a
second one for the paired diff, and a **declared price-table sidecar**, which
is not in `plan.json`. Its output goes to stdout or `-o`, never into a run
directory, so §6's tree is unchanged. TTFT is **reserved**, not built.
**Cached tokens (#105):** a model's table entry may add
`cache_read_per_mtok` and `cache_write_per_mtok` (both or neither; none
ship). Only where the trace source declares cache counts to be parts of
`input_tokens` — today an AG-UI stream with `traces.source: response`
(§2b) — does a call cost uncached input, cache reads, cache writes and
output at their own rates (`cache_accounting: "priced"`); with the keys
absent it prices as before and says `"unpriced"` with a warning. Other
sources report their cache counts as tokens and stay `"none"`.
**Repeats (#129):** at k > 1, `cost.all_repeats` prices every
`repeats/<n>/response.json`'s usage (`traces.source: response` only);
`cost.usd` stays the representative repeat's. The
script's docstring states each limit and its payload carries it.

---

## 6. The output tree

Everything below is written under `--out`. This is the complete list; the
runner writes no other files.

```
reports/
  .gitignore                     # only when: data.may_contain_pii (contents: */cases/)
  <run-id>/
    manifest.yaml                # §10. Written at pre-flight, before any case.
    results.json                 # §10. Written at pre-flight (status: running), rewritten per case, finalized at end.
    verdicts.jsonl               # §9. Rewritten from the case dirs after every case.
    verdicts_for_stats.jsonl     # §9. Same, filtered.
    routing_results.jsonl        # only when: any non-canary single-turn case is routing-scorable
    routing_report.json          # only when: routing_results.jsonl exists
    repeats.jsonl                # only when: k > 1
    reliability.json             # only when: k > 1
    comparison.json              # only when: --baseline-verdicts was passed
    run.log                      # append-only, one JSON object per line (§12)
    cases/
      <case-id>/
        request.json             # §10
        response.json            # §10
        verdict.json             # §10 — written LAST, atomically. Its existence means "this case is done."
        expect.json              # the case's `expect` object, as fed to the scorers
        answer.txt               # the final answer text, as fed to score_answer/score_authz
        trajectory.json          # only when: a trace was collected for this case
        actual.json              # only when: expect.result is present and the case ran
        trace.json               # only when: a trace was collected for this case
        turns/<t>/request.json   # only when: the case is multi-turn
        turns/<t>/response.json  # only when: the case is multi-turn
        turns/<t>/verdict.json   # only when: the case is multi-turn
        turns/<t>/expect.json    # only when: the case is multi-turn
        turns/<t>/answer.txt     # only when: the case is multi-turn
        turns/<t>/trajectory.json  # only when: a trace was collected for this turn
        turns/<t>/trace.json     # only when: a trace was collected for this turn
        repeats/<n>/             # only when k > 1; n = 1..k
          request.json  response.json  verdict.json
        repeats/<n>/turns/<t>/   # only when k > 1 and the case is multi-turn: the same five files
```

**`# only when:` is machine-read.** Every conditional file above carries its
condition after that exact marker, and the string is the one `REQUIRED_IF`
(§9(b)) stores for that file; a test asserts the two are equal. The
`.gitignore` line is the one exception: it is written into `reports/`,
outside the tree the completeness check walks.

**Rules that make this tree unambiguous:**

- `repeats/` exists **iff** `k > 1`. When it exists, it holds every repeat
  including the first.
  Each `repeats/<n>/` holds three files, plus, for a conversation,
  `turns/<t>/` with the five files every sent turn has.
- **A conversation** (`input.turns`, docs/multi-turn.md) keeps every file
  above at case level, holding its **deciding turn** (the turn that stopped
  it, or the last) — `expect.json` included, which is that turn's expect with
  `every_turn` overlaid. Beside them, `turns/<t>/` is present **iff** the case is a
  conversation that sent a turn, numbered contiguously from 1, and holds
  exactly `verdict.json`'s `turns_sent` turns; each turn's `trace.json` /
  `trajectory.json` is there iff that turn's own `verdict.json` says a trace
  was collected for it. `REQUIRED_IF` keys these by path pattern
  (`turns/*/<name>`).
  `collect_trace` copies the whole span store per turn, so each
  `turns/<t>/trace.json` holds the full store.
- With `k > 1`, the case-level `request.json`/`response.json` are the
  **representative** repeat: the first repeat whose verdict is the case
  verdict under §5.5's fold — the first failing repeat if any failed, else
  repeat 1 of an all-pass case, else the first repeat carrying the case's
  `infra_error` / `infra_incomplete` / `unscored`. `verdict.json` at case level is always the
  reduced verdict and carries `repeats: [{n, verdict, layers}...]`.
- `verdict.json` is written last, via temp-then-rename. **A case directory
  containing `verdict.json` is complete, by definition.** Resume (§8) and the
  completeness check (§9) both key off exactly this fact.
- A **skipped** case (§7's safety gates) still writes `request.json` and
  `response.json`, marked `"sent": false` / `"sent_at": null`. `REQUIRED_PER_CASE`
  has no exceptions.
- Every write in this tree is `write to <name>.tmp.<pid> → os.replace()`. Never
  a partial file visible mid-write, never a batch at run end.
- When `trace.json` is absent, `verdict.json`'s `trace` block says whether a
  trace was expected and why there isn't one.
- Holdout cases get ordinary directories here, like any other case. The seal is
  enforced at §10's `results.json`, not by withholding files.
- Canary cases get ordinary directories too, with `canary: true` in
  `verdict.json`, and are excluded from every denominator in `results.json`
  and from `routing_results.jsonl` / `routing_report.json` (their own
  `routing` layer still scores).

**Holdout ledger.** When `mode` is `holdout`, or `selecting_split` is
`holdout`, the runner appends one line —
`{run_id, date, mode, reason: "run"}` — to `paths.holdout_ledger` and writes the
running count to **stderr** (it is also in `results.json`'s
`summary.holdout.looks_recorded`). The runner does this, not the skill; the
count is what the N=5 reseal trigger reads (`run/SKILL.md` §4,
`analyze/SKILL.md` §`--unseal`).
`holdout_ledger: null` with a holdout-touching mode is exit 2 at plan
validation. The **split** named `full` is not one: it is the unsealed set
`regression` selects, so a regression run records no look and needs no
ledger.

**The ledger is a `.jsonl` file**, and a path that is not is exit 2. Its
running total is its line count. Nothing here resets it: a reseal
(`analyze --unseal`) or a grow (`seal-record archive`) archives it to
`holdout-looks-v<old dataset_version>.jsonl` and empties it, so the new
seal's count starts at zero. The plan's `paths.holdout_ledger` names it
(`datasets/holdout-looks.jsonl` by convention).

The append happens once, at pre-flight, before the first case — a run that
aborts halfway has still spent its look. `--resume` does not append again:
the ledger holds one row per run id, and a resume appends only when that run
id has none (a first attempt that died before its append).

---

## 7. Execution semantics and the infra taxonomy

**Serial.** One case at a time, in the plan's case order, canaries first.
`adapter.invocation.max_concurrency > 1` is **refused at plan validation with
exit 2**, never warned about and downgraded.

**Paced to a declared rate budget (#76).** `adapter.invocation.rate_limit`
declares what the app's limiter allows: `min_interval_s` (the least gap
between calls), `requests` with `window_s` (at most N calls in any window of
that many seconds), or both, each up to a day. A malformed block is exit 2 at
plan validation.
Before **every** app call (the health check, each retry, and each turn of
a restarted conversation), the runner waits until both forms hold. Each
interval runs from the moment the earlier call *finished* (response or
transport error). A wait is logged as a `pace`
event in `run.log`, and the manifest records the block under `invocation`.
A request's `sent_at` is stamped after the wait. A retry waits its
`backoff_s`/`Retry-After` and then whatever of the pace is left, so the
longer of the two. **The budget is per process:** a run started right
after another one, or a `--resume` of a killed run, starts with no record
of the last run's calls and can share its last window; wait a window
between them. Pacing is transport only: no verdict, retry rule or
`SCORING_VERSION` changes.
Absent (or `null`), the runner sends each call as soon as the last finished,
and the manifest and dry run carry no trace of it.

**Verdict vocabulary**, unchanged from the rest of the harness:
`pass | fail | unscored | skipped | infra_error | infra_incomplete`.

**Attempt loop, per repeat:**

| Condition | Handling |
|---|---|
| connection error, timeout, HTTP 429, HTTP 5xx | retry up to `max_attempts` with `backoff_s` (a 429/503 `Retry-After: <seconds>` longer than the declared wait is honored, capped at 60s); on exhaustion → `infra_error`, `retry_count` recorded in `response.json` — whatever `expect.http.status` says. When every attempt of every repeat got the same 5xx and body (≥ 2 attempts in all), `verdict.json` carries `repeated_5xx: {status, attempts}` and `summary.repeated_5xx` counts the case: still infra, but possibly the app's own error rather than the provider's. Flagged, never rescored |
| HTTP 4xx other than 429 | **not** retried — it is a response. When every request of every repeat (every turn, for a conversation) got the same 4xx, `verdict.json` carries `same_4xx: <status>`; when every case the app answered (not `skipped` or `infra_error`), canaries included, carries the same one (≥ 2 cases), `summary.uniform_4xx` is `{status, cases}` and the runner's closing line and `gate.py` say so; a failing canary's abort message names its `same_4xx`. Flagged, never rescored, like `repeated_5xx` (#50) |
| app crash / connection reset on a `noise` or `adversarial-refusal` case | `infra_error` **and** increments `summary.crash_rate`'s numerator |
| trace requested, quiescence not reached by `max_wait_s` | `infra_incomplete`; the case's non-trace layers still score |
| `normalize_trace.py` reports missing spans / orphans | `infra_incomplete` for trace-dependent layers only |
| adversarial case with `environment.safe_to_attack: false` | `skipped`, reason recorded — never `fail` |
| a case that declares an identity under an adapter with no `identity_map` | `skipped`, reason recorded |
| `input.messages` holding anything besides exactly one user message (a second user turn, a system or assistant message, or none) | `skipped`, reason recorded (adapter hard rule 3, docs/multi-turn.md §0): the runner sends that one message alone |
| a conversation (`input.turns`) under an adapter with no `invocation.conversation` | `skipped`, reason recorded (adapter hard rule 3). The block is the only switch; a legacy `invocation.session` turns nothing on |
| a conversation with more turns than `execution.max_turns`, or a malformed `input.turns` (fewer than two turns, a turn with no `user` text) | `skipped`, reason recorded |

**Infra and `skipped` verdicts never enter pass/fail denominators.** They are
counted separately and reported.

### 7.1 Conversations (docs/multi-turn.md)

A case written as `input.turns` is a **live scripted conversation**. One
conversation attempt:

1. A fresh `<uuid>`, an empty cookie jar, no `<session>`.
2. For each turn: render the body with that turn's text, send it (one
   request, no retry of its own), read the answer and trace id with the
   adapter's existing paths, score the turn with the existing layers against
   its expect (a checkpoint, or the final turn's top-level `expect`, with
   `every_turn` overlaid), and `roll_up()` the turn. Before turns 2+, the runner
   waits until `turn_delay_s` has passed since the previous turn's response
   arrived: the quiescence wait and scoring since then count toward it, and
   the gap after the reply is never shorter than `turn_delay_s`.
3. **Stop at the first turn that rolls up `fail` or `infra_*`; continue past
   `unscored`.** `verdict.json` records `failed_turn` and `stop_reason`.

**The case verdict is `roll_up()` over the union of every sent turn's
layers**, keyed `t<n>.<layer>`. The rollup strips the prefix before checking
§10 rules 5-6, so `http` and `loops` still cannot carry a conversation to
`pass`. A single-turn case has no prefix and rolls up exactly as before.
**A checkpoint can veto, never certify:** a union `pass` stands only when the
final turn has a `pass` on a layer other than `http`/`loops`, or when no layer
but those is applicable on any turn; otherwise the case is `unscored`
(`final_turn_certifies`). A scorer error on any sent turn makes the case
`unscored` even beside a later turn's `fail`, by rule 4's order.

**Retries restart the whole conversation.** Any retryable outcome on any turn
(connection error, timeout, 429, 5xx) abandons the attempt and starts a new
one from turn 1, after `backoff_s`; the failed turn is never re-sent alone.
`max_attempts` counts conversation attempts; on
exhaustion the case is `infra_error`, the deciding turn is the one that kept
failing, and `repeated_5xx` additionally requires the same turn and records
it (`{status, attempts, turn}`). Turns 1..N-1 are replayed on every attempt.

**How the app keeps the conversation** is the adapter's
`invocation.conversation.style`. `client-id`: `<uuid>` once per attempt, on
every turn. `server-id`: `session_from` is read off turn 1's response (a body
path, or a header matched case-insensitively) and rendered into `<session>`
on turns 2+; an unset `<session>` as a dict entry's whole value **drops the
key** rather than sending `null`. Turn 1 without the id makes the case
`unscored` with that reason, and turn 2 is never sent. `cookie`: one
`CookieJar` per attempt, fed from **every** `Set-Cookie` header, and **one**
`Cookie` header composed by the runner from the auth cookie plus the jar
(`CookieJar.add_cookie_header` adds nothing when `build_headers` already set
a `Cookie` for `auth: {type: cookie}`); it is redacted in `request.json`.

**Client-sent history** (`history`, docs/client-history.md §3). Per
attempt the runner keeps a list `H`. A turn's `<history>` is `H` plus a
fresh user entry (`H` alone when the body also has `<user turn>`), a
single-turn case's its one user entry; `<request id>` is fresh per request
sent. After the stop rule, before turn n+1, the user entry and the rebuilt
reply join `H`: an entry per text message (AG-UI `messageId` as `<message
id>`), which joined by a blank line are `answer.txt`, and under `carried`
each tool call and its result. A reply with no finished text, an interrupt
with no text, or under `carried` a call with no id or result ends the
conversation `unscored` (`stop_reason`); turn n+1 is never sent. Inserted
text is never scanned for placeholders. Under `ag-ui`, `response.json`
records `snapshot_seen`.

**Traces per turn.** Each turn's trace is collected after its own quiescence
wait (none for `traces.source: response`, which is read off the turn's own
response) and read only by that turn's layers. When two turns of one attempt
return the **same trace id**, every
trace-dependent layer of the case (`TRAJECTORY_LAYERS`, `loops` included) is
`unscorable` with that reason, and the turn read with the shared id reads no
trace for routing or `result_from_tool` either.

**One conversation is one case** for every count, denominator, interval and
gate (§10).
`latency_s` is the sum over the sent turns. `memory: user` in the block means
the app's memory outlives a session, so any case may see an earlier one;
`results.json.memory` says so and the runner does not pretend to reset it.

**Abort conditions, mid-run:**

- A **canary** scoring wrong → stop immediately, exit 4. Everything completed so
  far stays on disk and finalize (§9) still runs, and `results.json`
  says `summary.status: "aborted_canary"`.
- Infra verdicts exceed `execution.infra_rate_abort` of attempted cases (checked
  after each case, once at least 8 cases have been attempted) → stop, exit 5,
  `summary.status: "aborted_infra"`.

**Gating failures do not affect the exit code.** A separate tokenless step
(`gate.py`) reads `results.json` and sets the merge gate (`run/SKILL.md` §6).
The plan's `gate` field is recorded in the manifest and otherwise unused here.

---

## 8. Resume

`--resume` into an existing `--out`:

1. Read `<out>/manifest.yaml`. If its `plan_sha256` differs from the SHA-256 of
   the current plan's canonical JSON, exit 2: `plan changed since <run-id>
   started; start a new run id`. The manifest's scoring key
   (`_common.scoring_key`, §5.6's rule) must also equal this runner's, else
   exit 2. A pre-field manifest whose release maps to
   the current version resumes, with a stderr line saying so.
2. Walk `<out>/cases/`. A case with a parseable `verdict.json` is **done** and is
   not re-invoked. A case directory without one is deleted and re-run. A
   conversation is one unit: an unfinished one is redone from turn 1.
3. Rebuild the in-memory verdict list from those directories, then rewrite
   `verdicts.jsonl` and `verdicts_for_stats.jsonl` from it (§9) before invoking
   anything.
4. If an adopted case is a **canary that scored `fail`**, stop here: exit 4,
   `summary.status: "aborted_canary"`, no case invoked (pre-flight has run, so
   the health check was made — and a down app is exit 3 first). The verdict
   is on disk and would be adopted again; fix the drift and start a new run
   id.
5. Continue with the first not-done case in plan order.

Without `--resume`, a non-empty `--out` is exit 2. The runner never silently
merges into or overwrites an existing run.

---

## 9. Completeness

A run never reports success while a required file is absent. Three
mechanisms, in order of strength:

**(a) Derived, not accumulated.** `verdicts.jsonl` and
`verdicts_for_stats.jsonl` are **regenerated in full from the completed case
directories after every case completes**, temp-then-rename. They are not built
up at run end, and they are not appended to.

- `verdicts.jsonl`, one row per case, the durable record — holdout and canary
  cases included:
  ```json
  {"case_id": "c-3f9a2c1d", "set": "smoke", "category": "happy", "canary": false,
   "gating": true, "verdict": "pass",
   "layers": {"http": "pass", "routing": "unscored", "answer": "pass",
              "trajectory": "unscorable", "execution": "n/a", "authz": "n/a"}}
  ```
  `layers` here is flattened to verdict **strings**; the full scorer objects live
  in the case's `verdict.json`. `set` is the plan's `selecting_split`.
- `verdicts_for_stats.jsonl`, the same cases reduced to
  `{"case_id", "verdict"}` with every row whose verdict is not exactly `pass` or
  `fail` dropped — what `stats.py` pairs (it treats a third value as a hard
  error).

**(b) A declared required-artifact table.** `run_cases.py` defines:

```python
REQUIRED_ALWAYS = ("manifest.yaml", "results.json", "verdicts.jsonl",
                   "verdicts_for_stats.jsonl")
REQUIRED_PER_CASE = ("request.json", "response.json", "verdict.json",
                     "expect.json", "answer.txt")
REQUIRED_IF = {"routing_results.jsonl": "any non-canary single-turn case is routing-scorable",
               "routing_report.json":   "routing_results.jsonl exists",
               "repeats.jsonl":         "k > 1",
               "reliability.json":      "k > 1",
               "comparison.json":       "--baseline-verdicts was passed",
               "trajectory.json":       "a trace was collected for this case",
               "actual.json":           "expect.result is present and the case ran",
               "trace.json":            "a trace was collected for this case",
               "turns/*/request.json":    "the case is multi-turn",
               "turns/*/response.json":   "the case is multi-turn",
               "turns/*/verdict.json":    "the case is multi-turn",
               "turns/*/expect.json":     "the case is multi-turn",
               "turns/*/answer.txt":      "the case is multi-turn",
               "turns/*/trajectory.json": "a trace was collected for this turn",
               "turns/*/trace.json":      "a trace was collected for this turn"}
```

A test execs the block above out of this file and diffs it against the
module's constants, then parses §6's tree and diffs the `# only when:`
strings against `REQUIRED_IF`'s values. `report.md` is **not** here: the
skill writes it (§13).

The per-case conditions are read off the **case directory**, never off the
plan, so `--verify` evaluates them with no plan in hand. `trajectory.json` and
`trace.json` key on `verdict.json`'s `trace.collected`, not on whether the
store is queryable. `actual.json` keys on `expect.json` plus "the case ran"
(a **skipped** case made no call). The `turns/*/…` conditions read the
case's `verdict.json` (`multi_turn`, `turns_sent`, and each repeat's
`turns_sent`) and each turn's own `verdict.json` (`trace.collected`). The
run-level conditions read the manifest (`k`, `baseline_verdicts`) and the
case verdicts (routing-scorable = at least one non-canary **single-turn** case
whose `layers.routing` scored `pass` or `fail`; a canary's row and a
conversation's never reach the run-level file, §5.2/§6).

**(c) A finalize check that can fail the run.** Before writing the terminal
`results.json` and returning, the runner:

1. verifies every `REQUIRED_ALWAYS` file exists, is non-empty, and parses;
2. verifies every completed case directory has every `REQUIRED_PER_CASE` file,
   plus each `REQUIRED_IF` file whose condition held for that case;
3. verifies `verdicts.jsonl` holds exactly one row per **completed case
   directory**, with the same ids and the same verdicts, and that
   `verdicts_for_stats.jsonl` is exactly the `pass`/`fail` subset of it;
4. verifies `results.json`'s per-case rows are the non-holdout completed cases
   with matching verdicts, and that its `summary` counts (`n`, `passes`,
   `failures`, `gating_failures`, `unscored`, `skipped`, `infra_errors`,
   `canaries`) agree with a recount over those case directories.

Check 3 counts completed case directories, not `summary.attempted`: a
skipped case has a `verdict.json` and a row but is not attempted (§10).
Checks 3 and 4 are derivable from the tree alone, so `--verify` runs them.
`infra_rate` is recounted too, on runs that carry `summary.repeated_5xx`,
and `uniform_4xx` from each `verdict.json`'s `same_4xx` on runs that carry
it. The run-time bookkeeping counters (`attempted`, `crash_rate`,
`scorer_errors`) are **not** recounted. One consistency rule rides along:
`summary.status: "ok"` beside a canary whose `verdict.json` says `fail` is a
discrepancy.

Any discrepancy → the missing/mismatched items are written to
`results.json.summary.missing_artifacts`, printed as JSON on stdout, and the
runner **exits 6**. `summary.status` becomes `"incomplete"`, never `"ok"`.

`summary.status: "ok"` is written **only** after this check passes, and
`--verify <dir>` re-asserts it on any run directory at any later time.

---

## 10. File shapes

### `manifest.yaml`

JSON bytes in a `.yaml` file.

```jsonc
{
  "schema_version": 1,                    // _common.SCHEMA_VERSION: this file's own layout (#38)
  "run_id": "smoke-20260818T183920Z",     // top-level key, not just the directory name (§5 CI gate reads it here)
  "mode": "smoke", "k": 1, "gate": "soft", "selecting_split": "smoke",
  "started_at": "2026-08-18T18:39:20Z",
  "harness_version": "0.1.0",             // from _common.HARNESS_VERSION, never invented by the caller; provenance
  "scoring_version": 1,                   // from _common.SCORING_VERSION: the comparability key (§5.6)
  "plan_sha256": "…",                     // canonical JSON of the plan; resume compares this
  "runner_version": 1,
  "baseline_verdicts": null,              // or the --baseline-verdicts path (§9)

  "dataset_version": 1,
  "cases": [{"id": "…", "set": "smoke", "sha256": "…"}],   // sha256 of the case's canonical JSON
  "app": { … },                           // from manifest_extra
  "prompt_snapshot_hashes": { … },
  "judge": { … }, "rubric_versions": "…",
  "environment_kind": "live-readonly", "safe_to_attack": false,
  "temperature": "…",
  "identity_headers": {"names": ["X-User-Id", "X-Role-Id"], "source": "env"},  // names only, never values
  "invocation": {"mode": "http", "url": "https://…/api/chat/ask",
                 "timeout_s": 120, "timeout_enforced": true,    // false in function mode (§4.2); exec adds "exec": {argv, cwd, env}
                 "rate_limit": {"requests": 20, "window_s": 600}},  // only when declared (§7)
  "traces": {"source": "otlp-file", "correlation": "none", "collected": false,
             "disabled_layers": ["trajectory", "tool_selection", "loops", "cost_latency"]},
  "capability_matrix": { … },
  "scoring": { … }, "execution": { … },
  "cost_estimate": { … }
}
```

### `cases/<id>/request.json`

```jsonc
{"case_id": "…", "repeat": 1, "persona": "…",
 "headers_sent": {"Content-Type": "application/json", "X-User-Id": "<redacted>"},
 "url": "https://…/api/chat/ask", "method": "POST",
 "body": { … },                       // the literal request body sent
 "sent_at": "2026-08-18T18:40:02Z", "sent": true}   // false on a skipped case (§6)
```

### `cases/<id>/response.json`

```jsonc
{"case_id": "…", "repeat": 1, "status": 200, "latency_s": 6.68,
 "attempts": 1, "retry_count": 0, "crashed": false,   // a DROPPED connection, not a timeout
 "body": { … },                       // raw parsed body, or {"raw": "<text>"} if not JSON
 "answer": "…",                       // the extracted final answer text (also written to answer.txt)
 "trace_id": null}
```

### `cases/<id>/verdict.json`

```jsonc
{"case_id": "…", "set": "smoke", "category": "happy", "canary": false,
 "holdout": false, "gating": true,
 "verdict": "pass",
 "k": 1,
 "latency_s": 6.68,                   // the representative repeat's
 "layers": { "http":   {"layer": "http", "verdict": "pass", "status": 200},
             "answer": { …verbatim score_answer.py output… },
             "trajectory": {"layer": "trajectory", "verdict": "unscorable",
                            "blocked_by": "no trace-id correlation"},
             "authz": {"layer": "authz", "verdict": "n/a"} },
 "trace": {"expected": true, "collected": false,
           "reason": "traces.correlation: none — heuristic matching forbidden"},
 "multi_turn": false,                 // true for an input.turns case (§7.1), skipped or not
 "turns_sent": 1,                     // 0 when skipped; a conversation's sent turns
 "failed_turn": null,                 // the turn that rolled up fail/infra_* and stopped it
 "stop_reason": null,                 // why a conversation stopped early, in one line
 "repeats": null,                     // or [{"n": 1, "verdict": "pass", "layers": {…}}, …] when k > 1
                                      // (a conversation's repeats add turns_sent and failed_turn)
 "repeated_5xx": null,                // or {"status": 500, "attempts": 4}: the same 5xx on every attempt (§7);
                                      // a conversation's adds "turn"
 "same_4xx": null,                    // or 403: the one 4xx every request of the case got (§7)
 "notes": ""}                         // free text for investigation context; runner writes "", humans may edit
```

**Case verdict rollup, in this order — the first match wins:**

1. any layer `infra_error` → `infra_error`
2. any layer `infra_incomplete` → `infra_incomplete`
3. the case was skipped by a safety gate → `skipped`
4. any applicable, enabled layer `fail` → `fail`
5. at least one layer other than `http` and `loops` `pass` → `pass`
6. `http` passed and no layer other than `http` and `loops` is applicable → `pass`
7. otherwise → `unscored`

A conversation's `layers` is the union of its turns' rows, keyed
`t<n>.<layer>`; rules 5-6 read the layer name after the prefix, and a union
`pass` then needs a graded `pass` on the final turn (§7.1).

`gating` is `false` for a canary and for a case that says `gating: false`
(the boolean); anything else, an absent key included, is `true`
(`case.get("gating", True) is not False`). A case authored `gating: false` (every
case until a human accepts it, generate §3) runs and is reported but cannot
close the gate. `summary.gating_failures` counts cases with `gating: true` and
`verdict: "fail"`.

### `results.json`

The machine-readable summary §5's CI gate reads. `run_id`, `harness_version`,
`scoring_version` and `dataset_version` are echoed from the manifest.

```jsonc
{"run_id": "…", "harness_version": "0.1.0", "scoring_version": 1, "dataset_version": 1, "mode": "smoke",
 "cases": [{"case_id": "…", "verdict": "pass", "gating": true,
            "layers": {"http": "pass", "answer": "pass", "trajectory": "unscorable"},
            "latency_s": 6.68,
            "multi_turn": false, "turns_sent": 1, "failed_turn": null}],
 "memory": null,                       // invocation.conversation.memory; "user" = verdicts may be contaminated (§7.1)
 "summary": {"status": "ok",           // running | ok | incomplete | aborted_canary | aborted_infra
             "n": 4, "attempted": 6, "passes": 4, "failures": 0,
             "gating_failures": 0,
             "unscored": 0, "skipped": 0,
             "infra_errors": 0, "infra_rate": 0.0,
             "repeated_5xx": 0,        // infra cases with the same 5xx on every attempt (§7)
             "uniform_4xx": null,      // or {"status": 403, "cases": 6}: every sent case got it (§7)
             "crash_rate": 0.0,
             "scorer_errors": 0,
             "unscorable_layers": ["trajectory", "tool_selection", "loops"],
             "unjudged": "mode: smoke",
             "canaries": {"n": 2, "passed": 2},
             "holdout": null,          // or {"n": 2, "passes": 1, "failures": 1, "gating_failures": 1} — aggregate ONLY
             "multi_turn": null,       // or {"n", "passes", "failures", "gating_failures"} over the conversations
             "missing_artifacts": []},
 "exit_code": 0}
```

**The counts, one definition each.** Every consumer — `gate.py`,
`run_history.py`, the skills' reports — reads them this way:

| Field | Counts | Canaries | Skipped | Infra |
|---|---|---|---|---|
| `n` | every non-canary case **selected** — the gate line's "N cases" | out | in | in |
| `attempted` | every case **sent** to the app, one per case however many repeats | in | out | in |
| `passes` + `failures` | the **scored** cases: the denominator of any pass or gating-failure rate | out | out | out |
| `infra_rate` | `infra_errors` ÷ the non-canary cases sent (`n` − `skipped`) | out | out | — |
| `crash_rate` | crashes ÷ `attempted` | in | out | in |
| `infra_rate_abort` (§7, mid-run) | every infra verdict so far ÷ `attempted` | in | out | — |
| `canaries` | `{n, passed}`; `passed` is pass^k for k > 1 (§5.5) | only | — | — |
| `routing_report.json` `n` | non-canary cases whose route was observed (a routing row) | out | out | `infra_error` out; `infra_incomplete` in when the route came from the response or status |

`n` is not a pass-rate denominator: a run of 3 pass, 1 fail and 1 skipped has
`n: 5` and a pass rate of 3 / 4. A conversation is **one** case in every
count above, never one per turn. `summary.multi_turn` repeats the counts over
the conversations alone; `--verify` recounts it. Holdout cases are inside
`n`, `passes` and `failures`; `holdout` carries their aggregate so a reader
can take them out.

**The holdout seal, concretely:** a holdout case contributes **no row** to
`results.json.cases`. Its `case_id` appears nowhere in this file. It appears only
inside `summary.holdout` as counts. `verdicts.jsonl` does carry it.

---

## 11. Exit codes

| Code | Meaning | Artifacts on disk |
|---|---|---|
| 0 | Ran to completion; §9's completeness check passed. Says **nothing** about pass/fail. | complete |
| 1 | Unhandled internal error. Traceback to stderr, `{"error": …}` to stdout. | whatever completed |
| 2 | Bad input or usage: malformed plan, unknown key, duplicate case id, non-empty `--out` without `--resume`, plan/manifest or scoring-version mismatch on resume. | none written |
| 3 | Pre-flight abort: unresolved env vars, health check failed, trace declared-but-not-joining or an unimplemented trace store, unimplemented `invocation.mode`, unimportable `function` entrypoint, malformed body template, malformed `invocation.conversation`. | manifest only, or nothing. The run directory is created **inside** pre-flight, so a pre-flight failure normally leaves nothing at all. |
| 4 | Canary failed — harness/judge drift; run stopped. | complete for cases finished |
| 5 | Infra rate exceeded `infra_rate_abort`; run stopped. | complete for cases finished |
| 6 | **Completeness check failed** — a required artifact is missing or inconsistent. `summary.missing_artifacts` names them. | incomplete, by definition |
| 7 | At least one scorer returned exit 2. The run finished and its artifacts are complete, but its numbers are not quotable. `summary.status` stays `"ok"` and `summary.scorer_errors` is the count. | complete |

**Precedence when more than one applies:** 6 outranks everything, then 4/5,
then 7.

Like the scorers, the runner prints `{"error": "..."}` as JSON **on stdout**
for every non-zero exit.

`--probe` exits 0, 1, 2, 3 or 8 (§1.1); 8, the app refused the request
with a 4xx, is the probe's alone.

A red suite exits **0** (§7).

---

## 12. `run.log`

Append-only JSONL, one object per line:
`{"ts", "event", …}`, with events `preflight`, `case_start`, `case_done`,
`retry`, `pace` (a wait for the rate budget, §7), `scorer`, `abort`,
`finalize`. For post-hoc debugging only: nothing reads it programmatically,
and it is not in `REQUIRED_ALWAYS`.

---

## 13. Explicitly out of scope for `run_cases.py`

These stay in the skills:

- **Case selection and mode semantics.** The skill resolves `--smoke` etc. into
  `cases` + `selecting_split` + `k` + `gate`.
- **YAML.** The skill converts; the runner reads JSON only. `--probe` hands a
  `.yaml` adapter to `convert_suite.py --adapter-file` and reads its JSON
  (§1.1).
- **Cost estimation and the "Proceed?" prompt.**
- **`report.md`.** The skill writes it; the runner produces every number it
  quotes.
- **Choosing** the baseline. The skill reads `reports/baseline.json` and decides
  *which* run to diff against; the diff itself is the runner's (§5.6).
- **Judged layers and business rules.** §5.4. (State-diff is RESERVED and
  nobody owns it — §5.4.)
- **Pinning `reports/baseline.json`.** A skill decision, not an execution step.
  The write itself is `pin_baseline.py`'s, from the run's manifest: never the
  runner's, never the model's by hand (#53).
