#!/usr/bin/env python3
"""
Local mock Aiceberg API server for hook monitor testing.

Endpoint:
  POST /eap/v1/event

Behavior:
- CREATE (no event_id): returns event_id + pass/block based on input text.
- UPDATE (with event_id): returns pass/block based on output text.

Block markers (case-insensitive substring match):
- "jailbreak"
- "toxic"
- "malware"
- "rm -rf /"
- "[[block]]"
"""

import json
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


BLOCK_TOKENS = ("jailbreak", "toxic", "malware", "rm -rf /", "[[block]]")


def _contains_block_token(text: str) -> str | None:
    low = text.lower()
    for token in BLOCK_TOKENS:
        if token in low:
            return token
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{ts}] [mock-aiceberg] {fmt % args}", file=sys.stderr, flush=True)

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        if self.path != "/eap/v1/event":
            self._send(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self._send(400, {"error": "empty body"})
            return

        try:
            payload = json.loads(self.rfile.read(length))
        except Exception as exc:
            self._send(400, {"error": f"bad json: {exc}"})
            return

        is_update = bool(payload.get("event_id"))
        if is_update:
            text = str(payload.get("output", ""))
            token = _contains_block_token(text)
            if token:
                self._send(
                    200,
                    {
                        "event_id": payload["event_id"],
                        "event_result": "blocked",
                        "policy": "mock_policy",
                        "reason": f"blocked by token '{token}' in output",
                    },
                )
                return

            self._send(
                200,
                {
                    "event_id": payload["event_id"],
                    "event_result": "passed",
                    "reason": "mock pass (update)",
                },
            )
            return

        # CREATE
        text = str(payload.get("input", ""))
        token = _contains_block_token(text)
        event_id = str(uuid.uuid4())
        if token:
            self._send(
                200,
                {
                    "event_id": event_id,
                    "event_result": "blocked",
                    "policy": "mock_policy",
                    "reason": f"blocked by token '{token}' in input",
                },
            )
            return

        self._send(
            200,
            {
                "event_id": event_id,
                "event_result": "passed",
                "reason": "mock pass (create)",
            },
        )


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main() -> int:
    host = "127.0.0.1"
    port = 8787
    if len(sys.argv) >= 2:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    srv = Server((host, port), Handler)
    print(f"[mock-aiceberg] listening on http://{host}:{port}/eap/v1/event", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[mock-aiceberg] shutting down", flush=True)
        srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
