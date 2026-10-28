"""Loopback HTTP clients for the auxiliary llama-server services.

The knowledge pipeline talks to three OpenAI-compatible endpoints:

* ``/v1/embeddings`` - document and query vectors
* ``/rerank``        - optional second-stage ordering
* ``/v1/chat/completions`` with an image part - OCR for scanned PDF pages

Everything is local-only. These helpers deliberately do not read proxy
environment variables (a stray Clash/V2Ray setting must not redirect loopback
traffic) and never receive the launcher's management token: the services are
plain llama-server processes that do not require it.
"""
from __future__ import annotations

import base64
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx
import numpy as np

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LlamaServiceError(RuntimeError):
    pass


def _base_url(base_url: str) -> str:
    clean = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(clean)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise LlamaServiceError("knowledge services must be addressed over loopback HTTP")
    return clean


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(
        trust_env=False,
        timeout=httpx.Timeout(connect=10.0, read=timeout, write=timeout, pool=10.0),
    )


def _detail(response: httpx.Response) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - diagnostics must never mask the status
        text = ""
    return text.strip()[:300]


def _post_json(base_url: str, path: str, payload: dict, *, timeout: float) -> dict:
    url = _base_url(base_url) + path
    try:
        with _client(timeout) as client:
            response = client.post(url, json=payload)
    except httpx.HTTPError as error:
        raise LlamaServiceError(f"service unreachable at {url}: {error}") from error
    if response.status_code != 200:
        raise LlamaServiceError(
            f"{path} returned HTTP {response.status_code}: {_detail(response)}"
        )
    try:
        data = response.json()
    except ValueError as error:
        raise LlamaServiceError(f"{path} returned invalid JSON") from error
    if not isinstance(data, dict):
        raise LlamaServiceError(f"{path} returned an unexpected payload")
    return data


def embed_texts(
    base_url: str,
    texts: Sequence[str],
    *,
    timeout: float = 120.0,
) -> np.ndarray:
    """Embed a batch and return a float32 matrix in input order."""
    clean = [str(text or "") for text in texts]
    if not clean:
        return np.zeros((0, 0), dtype=np.float32)
    if any(not text.strip() for text in clean):
        raise ValueError("embedding input must not be empty")
    data = _post_json(
        base_url,
        "/v1/embeddings",
        {"input": clean, "encoding_format": "float"},
        timeout=timeout,
    )
    rows = data.get("data")
    if not isinstance(rows, list) or len(rows) != len(clean):
        raise LlamaServiceError("embedding service returned the wrong number of vectors")
    ordered: list[tuple[int, list[float]]] = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise LlamaServiceError("embedding service returned a malformed vector")
        index = row.get("index", position)
        vector = row.get("embedding")
        if not isinstance(index, int) or not isinstance(vector, list) or not vector:
            raise LlamaServiceError("embedding service returned a malformed vector")
        try:
            values = [float(value) for value in vector]
        except (TypeError, ValueError) as error:
            raise LlamaServiceError("embedding vector contains non-numeric values") from error
        ordered.append((index, values))
    ordered.sort(key=lambda pair: pair[0])
    if [index for index, _ in ordered] != list(range(len(clean))):
        raise LlamaServiceError("embedding service returned duplicate or missing indexes")
    matrix = np.asarray([values for _, values in ordered], dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] != len(clean):
        raise LlamaServiceError("embedding service returned vectors of inconsistent size")
    return matrix


def rerank(
    base_url: str,
    query: str,
    documents: Sequence[str],
    *,
    timeout: float = 120.0,
) -> list[float]:
    """Return one relevance score per input document, in input order."""
    if not documents:
        return []
    data = _post_json(
        base_url,
        "/rerank",
        {"query": str(query), "documents": [str(item) for item in documents]},
        timeout=timeout,
    )
    rows = data.get("results", data.get("data"))
    if not isinstance(rows, list) or len(rows) != len(documents):
        raise LlamaServiceError("rerank service returned the wrong number of scores")
    scores = [0.0] * len(documents)
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise LlamaServiceError("rerank service returned a malformed result")
        index = row.get("index")
        score = row.get("relevance_score", row.get("score"))
        if not isinstance(index, int) or not 0 <= index < len(documents) or index in seen:
            raise LlamaServiceError("rerank service returned an invalid index")
        try:
            scores[index] = float(score)
        except (TypeError, ValueError) as error:
            raise LlamaServiceError("rerank service returned a non-numeric score") from error
        seen.add(index)
    return scores


def ocr_image(
    base_url: str,
    png: bytes,
    prompt: str = "Text Recognition:",
    *,
    max_tokens: int = 4096,
    timeout: float = 300.0,
) -> str:
    """Run one rendered page through a multimodal llama-server."""
    if not png:
        raise ValueError("OCR image is empty")
    encoded = base64.b64encode(png).decode("ascii")
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": str(prompt or "Text Recognition:")},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"},
                    },
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": max(256, min(int(max_tokens), 16384)),
        "stream": False,
    }
    data = _post_json(base_url, "/v1/chat/completions", payload, timeout=timeout)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlamaServiceError("OCR service returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):  # some multimodal templates return content parts
        content = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    text = str(content or "").strip()
    if not text:
        raise LlamaServiceError("OCR service returned empty text")
    return text
