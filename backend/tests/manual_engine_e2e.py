"""End-to-end: register an external OpenAI-compatible engine and chat with it.

What this proves that the unit tests cannot: the whole chain works over real HTTP —
registration, a probe against ``GET /v1/models``, a streamed chat against
``POST /v1/chat/completions``, and the capability answer that tells the UI token counts
are estimates.

A **throwaway stand-in server** runs in this process on a loopback port. That is the
honest way to test this feature: the real vLLM/SGLang cannot run natively on Windows, and
a test that only asserted "the URL validates" would not touch the transport at all. The
stand-in speaks the same OpenAI wire format, so what is exercised is the launcher's
client, not a mock of it.

Run:  python backend/tests/manual_engine_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import config  # noqa: E402
from core import engine_client, engine_registry  # noqa: E402


class StandInEngine(BaseHTTPRequestHandler):
    """A minimal OpenAI-compatible server: models + a streamed chat completion."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # silence request logging
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") != "/v1/models":
            self.send_error(404)
            return
        body = json.dumps(
            {
                "object": "list",
                "data": [
                    {"id": "stand-in-model", "object": "model"},
                    {"id": "other-model", "object": "model"},
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for piece in ("hello", " from", " the", " engine"):
            chunk = {
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]
            }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(
            b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail else ""))

    # Isolate the registry so the run never touches the user's file.
    import tempfile

    config.DATA_DIR = Path(tempfile.mkdtemp(prefix="engine-e2e-"))

    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), StandInEngine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"stand-in engine on 127.0.0.1:{port}")

    try:
        engine = engine_registry.upsert_engine(
            {
                "kind": engine_registry.KIND_OPENAI_COMPAT,
                "label": "stand-in vLLM",
                "base_url": f"http://127.0.0.1:{port}",
                "model": "stand-in-model",
            }
        )
        check("registered", bool(engine.id), engine.id)

        probe = asyncio.run(engine_client.probe(engine))
        check("probe reached the engine", probe["ok"] is True, str(probe.get("error")))
        check("probe listed models", "stand-in-model" in (probe["models"] or []) and "other-model" in (probe["models"] or []))

        reply = asyncio.run(
            engine_client.collect_chat(
                engine, {"messages": [{"role": "user", "content": "hi"}]}
            )
        )
        check("chat returned streamed text", "hello from the engine" in reply["content"], reply["content"])
        check("chat reported a finish reason", reply["finish_reason"] == "stop")
        # No `usage` block was sent, so the reply must *say* the count is approximate
        # rather than presenting a precise-looking number.
        check("usage is flagged as estimated", reply["usage_estimated"] is True)

        caps = {
            "capabilities": sorted(engine.capabilities),
            "exact_token_count": engine.supports("tokenize"),
        }
        check("capabilities exclude tokenize", caps["exact_token_count"] is False)
        check("capabilities are exposed for the UI", "chat" in caps["capabilities"])

        estimate = engine_client.estimate_tokens("你好，世界")
        check("estimate is non-zero for CJK", estimate >= 1, str(estimate))
    finally:
        server.shutdown()
        server.server_close()

    failed = [item for item in results if not item[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
