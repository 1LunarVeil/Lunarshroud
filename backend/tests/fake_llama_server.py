"""Fake llama-server for E2E tests.

Parses ``--port`` from argv and serves 200 on every GET so the launcher's
health probe can reach the RUNNING state. Run via the wrapper generated in
test_e2e_smoke.py.
"""
from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def main() -> None:
    port = 0
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            port = int(args[i + 1])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass  # 保持测试输出干净

    print(f"fake llama-server listening on {port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
