# helpdesk-demo

A tiny rule-based helpdesk assistant that answers questions about invoices and
shifts and refuses everything else. It is a fixture for evaluation tooling.

- Start: `python3 server.py 18731`
- Chat endpoint: `POST http://127.0.0.1:18731/api/chat/ask` with body
  `{"message": "<text>"}`; the reply is `{"message": "<answer>", "domain":
  "invoices|shifts|none", "data": {...}}`. Out-of-scope questions return 400.
- Health: `GET /healthz` -> 200.
- No traces, no database, no authentication. The assistant is read-only.
