"""Talking to an external OpenAI-compatible engine.

Two operations, and both are deliberately smaller than the llama.cpp equivalents:

* `probe(engine)` — reachability and model list, via ``GET /v1/models``. There is no
  ``/health`` on a generic server, and inventing one (a HEAD to the chat endpoint, say)
  would report "healthy" for a server that cannot serve the configured model.
* `chat(engine, payload)` — forward a chat request with the engine's model name and
  optional bearer token, and relay the stream.

**What is missing on purpose.** llama.cpp's ``/tokenize`` does not exist here, so token
counts are *estimated* (see `estimate_tokens`) and every caller that surfaces a number
must label it as approximate. A precise-looking count derived from a character heuristic
is worse than an honest "about N": it makes the context meter and the trim decision look
authoritative when they are guesses.

The HTTP client uses ``trust_env=False`` for the same reason the loopback probes do —
a dead local proxy must not swallow traffic to a loopback engine.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from core import engine_registry
from core.engine_registry import Engine

# Local engines can be slow to answer their first request after a model load.
PROBE_TIMEOUT = 8.0

# Probe response budget. The reply comes from a host the user pointed at, and its
# model list is **persisted** to engines.json and served by every `GET /api/engines`.
# Without these caps a hostile `/v1/models` turns one click into a 200 MiB registry
# (audit F1). 2 MiB is orders of magnitude above a real model list; 200 ids of 256
# characters is far above any server's catalogue.
MAX_PROBE_BODY_BYTES = 2 * 1024 * 1024
MAX_MODEL_ID_CHARS = 256
MAX_MODELS_TOTAL_CHARS = 32_000
MAX_MODELS_SEEN = 200
# A generation can run for minutes; the caller owns cancellation.
CHAT_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)

# Rough characters-per-token ratios. English and code sit near 4; CJK is far denser
# per character because a Character is a token or more. These are *estimates* and are
# labelled as such wherever they surface.
CHARS_PER_TOKEN_LATIN = 4.0
CHARS_PER_TOKEN_CJK = 1.6


class EngineUnavailable(Exception):
    """The engine could not be reached, or refused the request."""


def _headers(engine: Engine) -> dict[str, str]:
    headers = {"Accept-Encoding": "identity"}
    key = engine_registry.api_key_for(engine)
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def _count_cjk(text: str) -> int:
    return sum(
        1
        for char in text
        if "\u4e00" <= char <= "\u9fff"
        or "\u3040" <= char <= "\u30ff"
        or "\uac00" <= char <= "\ud7af"
    )


def estimate_tokens(text: str) -> int:
    """Approximate a token count for an engine with no tokenizer endpoint.

    Splits by script rather than applying one ratio: CJK text is roughly 2.5× denser per
    character, and a single ratio under-counts a Chinese prompt badly enough to break a
    context budget. Still an estimate — callers must say so.
    """
    if not text:
        return 0
    cjk = _count_cjk(text)
    other = len(text) - cjk
    return max(1, int(cjk / CHARS_PER_TOKEN_CJK + other / CHARS_PER_TOKEN_LATIN))


def estimate_messages(messages: list[dict[str, Any]]) -> int:
    """Estimate a whole conversation, adding a small per-message overhead.

    Chat templates add role markers and separators; a flat few tokens per message keeps
    the estimate from drifting low on many short turns.
    """
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += estimate_tokens(part["text"])
        total += 4
    return total


async def probe(engine: Engine) -> dict[str, Any]:
    """Check reachability and list the models the engine reports.

    Returns ``{ok, models, error}``. The result is written back to the registry by the
    route, so a failed engine stays visibly failed after a restart.

    **Every failure is reported, never raised.** Two escapes were measured in the
    2026-11-23 audit: `httpx.InvalidURL` (a malformed `base_url` written straight into
    `engines.json`, which the load path does not re-validate) and `UnicodeEncodeError`
    (an `api_key_env` holding a non-ASCII value, which cannot become a header). Neither
    is an `HTTPError`, so both turned `POST /api/engines/{id}/probe` into a 500. A
    probe is a diagnostic: its whole job is to say what is wrong.
    """
    url = engine.base_url + "/v1/models"
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=PROBE_TIMEOUT) as client:
            response = await client.get(url, headers=_headers(engine))
    except Exception as error:  # noqa: BLE001 - a probe must never raise
        return {"ok": False, "models": [], "error": _describe(error)}

    if response.status_code == 401 or response.status_code == 403:
        return {
            "ok": False,
            "models": [],
            "error": "the engine rejected the credentials; check the API key variable",
        }
    if response.status_code >= 400:
        return {
            "ok": False,
            "models": [],
            "error": "the engine returned HTTP " + str(response.status_code),
        }
    # The body is untrusted and the result is persisted. Without a byte cap a hostile
    # `/v1/models` (200 ids of 1 MiB each) rewrote `engines.json` at 200 MiB and made
    # every later `GET /api/engines` serve that back (audit F1).
    if len(response.content) > MAX_PROBE_BODY_BYTES:
        return {
            "ok": False,
            "models": [],
            "error": "the engine's model list is unreasonably large; refusing to read it",
        }
    try:
        body = response.json()
    except ValueError:
        return {"ok": False, "models": [], "error": "the engine did not return JSON"}

    items = body.get("data") if isinstance(body, dict) else None
    models: list[str] = []
    spent = 0
    for item in items or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        # Cap the id length and the total: the count limit alone does not bound bytes,
        # and this list is written to disk and returned by every list call.
        name = str(item["id"])[:MAX_MODEL_ID_CHARS]
        if spent + len(name) > MAX_MODELS_TOTAL_CHARS:
            break
        spent += len(name)
        models.append(name)
        if len(models) >= MAX_MODELS_SEEN:
            break
    return {"ok": True, "models": models, "error": None}


def _describe(error: Exception) -> str:
    """Turn a transport error into an actionable sentence.

    The three cases users actually hit are: nothing listening, a wrong port behind
    something that is listening, and WSL not forwarding. They read very differently
    once named instead of surfaced as ``ConnectError``.
    """
    text = str(error)
    name = type(error).__name__
    if "Connection refused" in text or "ConnectError" in name:
        return (
            "nothing is listening at that address; start the engine, and if it runs in "
            "WSL forward the port to Windows"
        )
    if isinstance(error, httpx.ConnectTimeout) or "timed out" in text.lower():
        return "the engine did not answer in time; it may still be loading the model"
    if isinstance(error, httpx.RemoteProtocolError):
        return "the address answered but not with HTTP; check the port and scheme"
    return text or name


async def chat_stream(engine: Engine, payload: dict[str, Any]) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Open a streamed chat request against the engine.

    The caller owns both returned objects and must close them. The model name is taken
    from the engine record, not from the request body: the configured model is what the
    engine was probed with, and letting the body override it would produce a 404 the user
    cannot explain.
    """
    body = dict(payload)
    body["model"] = engine.model or body.get("model") or "default"
    body["stream"] = True

    client = httpx.AsyncClient(trust_env=False, timeout=CHAT_TIMEOUT)
    try:
        request = client.build_request(
            "POST",
            engine.base_url + "/v1/chat/completions",
            json=body,
            headers={"Content-Type": "application/json", **_headers(engine)},
        )
        response = await client.send(request, stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        raise EngineUnavailable(_describe(error)) from error
    return client, response


async def collect_chat(engine: Engine, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one chat request and collect the streamed reply into a single result.

    Mirrors `chat_collect.collect_reply` for the llama.cpp path so the agent loop can
    treat both engines identically.
    """
    import json

    client, response = await chat_stream(engine, payload)
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    try:
        if response.status_code >= 400:
            detail = (await response.aread()).decode("utf-8", "replace")[:500]
            raise EngineUnavailable(
                "the engine returned HTTP " + str(response.status_code) + ": " + detail
            )
        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            try:
                chunk = json.loads(raw)
            except ValueError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content.append(delta["content"])
            piece = delta.get("reasoning_content")
            if not isinstance(piece, str):
                piece = delta.get("reasoning")
            if isinstance(piece, str):
                reasoning.append(piece)
            calls = delta.get("tool_calls")
            if isinstance(calls, list):
                _merge_calls(tool_calls, calls)
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])
    finally:
        await response.aclose()
        await client.aclose()

    return {
        "content": "".join(content),
        "reasoning": "".join(reasoning),
        "tool_calls": tool_calls,
        "usage": usage,
        "finish_reason": finish_reason,
        # No tokenizer endpoint exists on a generic server, so this is flagged.
        "usage_estimated": usage is None,
    }


def _merge_calls(accumulated: list[dict[str, Any]], deltas: list[Any]) -> None:
    for delta in deltas:
        if not isinstance(delta, dict):
            continue
        index = delta.get("index")
        position = index if isinstance(index, int) else 0
        while len(accumulated) <= position:
            accumulated.append(
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
        target = accumulated[position]
        function = target.setdefault("function", {"name": "", "arguments": ""})
        if isinstance(delta.get("id"), str) and delta["id"]:
            target["id"] = delta["id"]
        piece = delta.get("function") or {}
        if isinstance(piece.get("name"), str) and piece["name"]:
            function["name"] = piece["name"]
        if isinstance(piece.get("arguments"), str) and piece["arguments"]:
            function["arguments"] += piece["arguments"]


def probe_sync(engine: Engine) -> dict[str, Any]:
    """Blocking wrapper for callers that are not async (the agent worker thread)."""
    return asyncio.run(probe(engine))
