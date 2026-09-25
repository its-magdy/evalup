#!/usr/bin/env python3
"""Eval fixture: the quickstart helpdesk demo as a standalone server.
Usage: server.py <port>  (POST anything -> JSON; GET /healthz)."""
import http.server
import json
import re
import sys

ANSWERS = [
    (re.compile(r"\bdelete|remove|cancel\b", re.I), 200, "invoices",
     "I can't do that -- this assistant is read-only. I can look invoice "
     "INV-1200 up for you instead.", {}),
    (re.compile(r"invoice|spend|billing", re.I), 200, "invoices",
     "You spent $412.50 on invoices in May 2026, across 4 invoices.",
     {"value": 412.5}),
    (re.compile(r"surgeon", re.I), 200, "shifts",
     "There are no surgeons on the night shift.", {"value": 0}),
    (re.compile(r"nurse|shift|roster|headcount", re.I), 200, "shifts",
     "There are 7 nurses on the night shift.", {"value": 7}),
]
OUT_OF_SCOPE = (400, "none", "I can only help with invoices and shifts.", {})


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            message = (json.loads(raw) or {}).get("message") or ""
        except ValueError:
            message = ""
        status, domain, answer, data = OUT_OF_SCOPE
        for pattern, st, dom, text, payload in ANSWERS:
            if pattern.search(message):
                status, domain, answer, data = st, dom, text, payload
                break
        self._send(status, {"message": answer, "domain": domain, "data": data})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18731
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
