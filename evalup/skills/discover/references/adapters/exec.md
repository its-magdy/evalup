# `invocation.mode: exec` — a command per call

Read this only when the app is driven through `exec`. The runner's rules
are `docs/runner-contract.md` §4 item 2d; this page is what discover writes.

## When to choose it

- The app **is** a CLI or script that reads JSON on stdin and prints a JSON
  reply. One that takes the message as an argument, or prints plain text,
  needs a shim too: `argv` is fixed, and stdout must be a JSON object.
- The app has no HTTP route the runner can call cleanly (an auth wall, a
  local-only process, a desktop or worker app), and it is not Python, so a
  `function` entrypoint cannot import it. A shim of about 20 lines in the
  app's own language calls its chat code in-process instead.

Prefer `http` when a JSON route works, and `function` for Python: exec
starts one process per call, so the app's start-up time is paid every call.

## The block

```yaml
invocation:
  mode: exec
  exec:
    argv: ["node", "scripts/eval-shim.mjs"]   # a LIST: no shell runs it
    cwd: .                    # relative to app.repo (else the state location)
    env: [OPENAI_API_KEY, DATABASE_URL]       # NAMES passed through
  request_body: '{"message": "<user turn>", "session_id": "<uuid>"}'
  response_body: '{"text": "<answer>"}'
  timeout_s: 60
```

- `argv` holds no `${REF}`: argv is visible to every process on the machine.
  A secret goes in `env` by name, or as a `${REF}` in `request_body`.
- `env` values of 4+ characters are scrubbed from quoted errors; shorter
  ones (a `DEBUG=1`) are settings and left alone.
- The command gets only the `env` names plus `PATH`, `HOME`, `LANG`,
  `TMPDIR` and the like (on Windows also `USERPROFILE`, `APPDATA`,
  `SYSTEMROOT` ...), never the runner's whole environment. List every
  other variable the app reads at start-up in `env`, or it starts without
  it — a runtime's own (`NODE_OPTIONS`, `PYTHONPATH`, `JAVA_HOME`) too.
- `argv[0]` with a `/` is relative to `cwd`; a bare name is found on `PATH`.
- No `auth`, `headers`, `identity_map` or `health_check` (exit 3). An
  `auth: {type: bearer, token_env: APP_TOKEN}` becomes `env: [APP_TOKEN]`,
  and the shim sends it. Carry a persona as `<persona>` in `request_body`.
- Conversations: `client-id` and `server-id` with `session_from.body` work
  as in http; `cookie` and `session_from.header` do not.
- Tool calls: print them in the stdout object (`{"text": ..., "tool_calls":
  [{"name": ..., "arguments": {...}}]}`) and declare `traces.source:
  response`; a different path is `traces.tool_calls_path`.

## The shim's contract

Read one JSON document from stdin (the rendered `request_body`). Write ONE
JSON object to stdout and exit 0. Logs go to stderr: anything else on
stdout makes the call `infra_error`. So does a non-zero exit, a timeout (the
process group is killed) or more than 16 MiB on stdout.

- **A refusal is an answer, not a failure.** Exit 0 with a JSON body that
  says so (`{"text": ..., "route": "denied"}`) and declare
  `route_from_response: route`. There is no status code: a non-zero exit is
  always a crash, and a `status` field is just body data. So
  `route_from_status` is refused, and a case expecting any
  `expect.http.status` but 200 is skipped — generate asserts the route
  instead.
- **The answer is recorded as printed.** A secret the shim echoes on
  stdout lands in `response.json` and `answer.txt`, as with an http body.
  Never print one.
- Do not leave a process running that keeps stdout open: the call waits
  2 s past the shim's exit for stdout to close, then fails.

```js
// scripts/eval-shim.mjs -- a sketch; call the app's own chat function.
import { answer } from "../src/chat.js";
let input = "";
for await (const chunk of process.stdin) input += chunk;
const req = JSON.parse(input);
const out = await answer(req.message, { sessionId: req.session_id });
process.stdout.write(JSON.stringify({ text: out.text, tool_calls: out.toolCalls }));
```

Writing the shim is a code patch: discover step 5's rules apply (clean
tree, the full diff, a yes per file). Verify it with `--probe` as any
adapter.
