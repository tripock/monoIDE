"""Offline stand-in for notion2api, used to test the agent loop without Notion.

It mimics the parts the IDE depends on: GET /v1/models, POST /v1/chat/completions
with SSE streaming, and - importantly - it can replay the identity refusal that
real Notion produces, so the repair path can be exercised.

    python tests/mock_upstream.py --port 8111 [--refuse-first]
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["claude-sonnet4.6", "claude-opus4.8", "gpt-5.6-sol", "gemini-3.1pro"]

REFUSAL = (
    "I'm Notion AI - I don't have tools like read_file or write_file. "
    "Those are tools of another coding assistant, not Notion."
)

STATE = {"turn": 0, "refuse_first": False}


def script(messages):
    """Return the assistant reply for this turn."""
    STATE["turn"] += 1
    last = messages[-1]["content"] if messages else ""

    if STATE["refuse_first"] and STATE["turn"] == 1:
        return REFUSAL

    if "RUNNER PROTOCOL" in last or "runner" in last.lower() and STATE["turn"] == 2:
        pass  # fall through to a real action

    if "[list_dir]" in last or "[read_file]" in last or "[write_file]" in last:
        return "Inspected the workspace. Everything checks out."

    if "handshake" in last.lower():
        return "Runner protocol acknowledged.\n\n```action\n{\"tool\": \"list_dir\", \"path\": \".\"}\n```"

    return (
        "Listing the workspace first.\n\n```action\n{\"tool\": \"list_dir\", \"path\": \".\"}\n```"
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            body = json.dumps({
                "object": "list",
                "data": [{"id": m, "object": "model", "owned_by": "notion"} for m in MODELS],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        text = script(payload.get("messages") or [])

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for index in range(0, len(text), 24):
            chunk = {
                "id": "mock",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": payload.get("model", MODELS[0]),
                "choices": [{"index": 0, "delta": {"content": text[index:index + 24]}}],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8111)
    parser.add_argument("--refuse-first", action="store_true")
    args = parser.parse_args()
    STATE["refuse_first"] = args.refuse_first
    print(f"[mock] http://127.0.0.1:{args.port}/v1  refuse_first={args.refuse_first}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
