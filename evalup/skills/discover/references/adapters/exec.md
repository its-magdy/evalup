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
- The command gets only the `env` names plus `PATH`, `HOME`, `LANG`,
  `TMPDIR` and the like, never the runner's whole environment. List every
  variable the app reads at start-up, or it starts without it.
- `argv[0]` with a `/` is relative to `cwd`; a bare name is found on `PATH`.
- No `auth`, `headers`, `identity_map` or `health_check` (exit 3). Carry a
  persona as `<persona>` in `request_body`.
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
