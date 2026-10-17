"""Fake llama-server for E2E tests.

Parses ``--port`` from argv and serves 200 on every GET so the launcher's
health probe can reach the RUNNING state. Run via the wrapper generated in
test_e2e_smoke.py.
"""
from __future__ import annotations

import sys
import time
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

    # 端口窗口是"探测后释放、再让子进程 bind"，外部进程可能在高负载时抢走
    # （第五轮核验指出测试侧 TOCTOU）。给测试替身一个短暂的绑定重试，避免把
    # 环境竞态伪装成产品失败；产品侧的同名竞态在 STATUS 里如实记录。
    server = None
    for _ in range(40):
        try:
            server = HTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            time.sleep(0.05)
    if server is None:
        raise SystemExit(f"fake llama-server could not bind port {port}")
    print(f"fake llama-server listening on {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
