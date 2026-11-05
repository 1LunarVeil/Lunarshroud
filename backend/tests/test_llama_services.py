from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from core import llama_services


class _Handler(BaseHTTPRequestHandler):
    server_version = "fake-llama"
    records: list[tuple[str, dict]] = []
    response_factory = staticmethod(lambda path, payload: {})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).records.append((self.path, payload))
        body = json.dumps(type(self).response_factory(self.path, payload)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def service():
    _Handler.records = []
    _Handler.response_factory = staticmethod(lambda path, payload: {})
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _Handler
    finally:
        server.shutdown()
        server.server_close()
        # 等 serve_forever 真正退出再放行：全量并发跑时，下一个用例可能立刻
        # 复用同一个端口/线程，shutdown() 只保证"停止受理"而不保证线程已退出。
        thread.join(timeout=10)


def test_embed_texts_preserves_input_order(service):
    base, handler = service

    def factory(path, payload):
        return {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        }

    handler.response_factory = staticmethod(factory)
    matrix = llama_services.embed_texts(base, ["第一条", "第二条"])
    assert matrix.dtype.name == "float32"
    assert matrix.tolist() == [[1.0, 0.0], [0.0, 1.0]]
    assert handler.records[0][0] == "/v1/embeddings"
    assert handler.records[0][1]["input"] == ["第一条", "第二条"]

    with pytest.raises(ValueError):
        llama_services.embed_texts(base, ["ok", "  "])


def test_rerank_returns_scores_in_input_order(service):
    base, handler = service
    handler.response_factory = staticmethod(
        lambda path, payload: {
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.1},
            ]
        }
    )
    scores = llama_services.rerank(base, "问题", ["甲", "乙"])
    assert scores == pytest.approx([0.1, 0.9])
    assert handler.records[0][1] == {"query": "问题", "documents": ["甲", "乙"]}


def test_ocr_sends_an_image_part_and_reads_text_content(service):
    base, handler = service
    handler.response_factory = staticmethod(
        lambda path, payload: {
            "choices": [{"message": {"content": [{"text": "第一条 总则"}]}}]
        }
    )
    text = llama_services.ocr_image(base, b"\x89PNG fake", "Text Recognition:")
    assert text == "第一条 总则"
    content = handler.records[0][1]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Text Recognition:"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert handler.records[0][1]["temperature"] == 0


def test_errors_are_readable_and_non_loopback_hosts_are_rejected(service):
    base, handler = service

    def factory(path, payload):
        return {}

    class Broken(_Handler):
        def do_POST(self) -> None:  # noqa: N802
            body = b'{"error":"model is not loaded"}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    handler.response_factory = staticmethod(factory)
    # Swap the handler class while keeping the port: start a second server.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Broken)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(llama_services.LlamaServiceError, match="503"):
            llama_services.embed_texts(f"http://127.0.0.1:{server.server_port}", ["x"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    with pytest.raises(llama_services.LlamaServiceError, match="loopback"):
        llama_services.embed_texts("http://192.0.2.10:8080", ["x"])


def test_requests_ignore_environment_proxies(service, monkeypatch):
    base, handler = service
    handler.response_factory = staticmethod(
        lambda path, payload: {"data": [{"index": 0, "embedding": [1.0]}]}
    )
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    assert llama_services.embed_texts(base, ["x"]).tolist() == [[1.0]]
