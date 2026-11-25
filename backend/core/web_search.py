"""Optional web search — off by default, and honest about being a network call.

This product's contract is offline-first: every launcher feature works with no
internet. Search is the one **explicitly opt-in** exception, and the conditions
are all enforced here rather than by the UI:

1. `web_search_enabled` must be true **and** an endpoint must be configured. One
   without the other does nothing, so a half-filled settings form cannot quietly
   start sending queries.
2. The endpoint comes from config, never from the source. There is no built-in
   search provider, so "where do my queries go" always has a visible answer.
3. The request is a plain GET to that one host with `follow_redirects=False` —
   a redirect to another host would silently change where the user's text goes.
4. No system proxy: `trust_env=False`, and the loopback check is on the parsed
   host. A search query is the user's text; sending it through an unconfigured
   proxy would be exactly the kind of leak this project keeps a scanner for.
5. Every call is approved individually in the transcript (`web_search` is a
   `command`-class tool for that reason), and the approval card shows the query.

The response is bounded by both bytes and result count: a hostile or broken
endpoint must not be able to fill the message budget that the tool result has to
fit into.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

LOGGER = logging.getLogger("lunarshroud.backend.web_search")

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_QUERY_CHARS = 512
MAX_RESULTS = 10
MAX_TITLE_CHARS = 200
MAX_SNIPPET_CHARS = 500
DEFAULT_TIMEOUT_SECONDS = 10.0
# Loopback-only by default: a search endpoint on another machine is allowed only
# with the same opt-in the engine registry uses.
ALLOW_REMOTE_ENV = "LUNARSHROUD_ALLOW_REMOTE_ENGINES"


class WebSearchError(RuntimeError):
    """A readable reason the search could not run."""


def _allow_remote() -> bool:
    import os

    return os.environ.get(ALLOW_REMOTE_ENV, "").strip() in ("1", "true", "yes")


def _is_loopback(host: str) -> bool:
    if host in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_endpoint(value: Any, *, allow_remote: bool = False) -> str:
    """Normalise and check the configured search endpoint.

    Refuses everything that is not a plain http(s) URL to an allowed host. Query
    strings and fragments are refused too: the endpoint is a base, and a base with
    `?format=json` already in it would collide with the parameters we append.
    """
    if not isinstance(value, str) or not value.strip():
        raise WebSearchError("no search endpoint is configured")
    text = value.strip()
    try:
        parsed = httpx.URL(text)
    except Exception as error:  # noqa: BLE001 - httpx.InvalidURL is not an HTTPError
        raise WebSearchError(f"the search endpoint is not a valid URL: {error}") from error
    if parsed.scheme not in ("http", "https"):
        raise WebSearchError("the search endpoint must start with http:// or https://")
    host = parsed.host or ""
    if not host:
        raise WebSearchError("the search endpoint has no host")
    if parsed.userinfo:
        raise WebSearchError("credentials must not be part of the endpoint URL")
    if parsed.query or parsed.fragment:
        raise WebSearchError("the search endpoint must not contain a query or fragment")
    if not allow_remote and not _is_loopback(host):
        raise WebSearchError(
            "only a loopback search endpoint is allowed; set "
            f"{ALLOW_REMOTE_ENV}=1 to permit a remote one"
        )
    return text.rstrip("/")


def search(query: Any, endpoint: str, *, limit: int = 5) -> dict[str, Any]:
    """Run one query against the configured endpoint.

    The endpoint is expected to be a SearXNG-compatible JSON API
    (`GET <endpoint>/search?q=…&format=json`). A different server is fine as long
    as it answers `{"results": [{"title", "url", "content"}]}` — the shape is read
    defensively and anything else reads as "no results", not a crash.
    """
    text = str(query or "").strip()
    if not text:
        raise WebSearchError("the search query is empty")
    if len(text) > MAX_QUERY_CHARS:
        raise WebSearchError(f"the search query is longer than {MAX_QUERY_CHARS} characters")

    base = validate_endpoint(endpoint, allow_remote=_allow_remote())
    count = max(1, min(MAX_RESULTS, int(limit) if isinstance(limit, int) else 5))

    try:
        with httpx.Client(
            timeout=DEFAULT_TIMEOUT_SECONDS,
            # A search query is user text. It must not travel through a proxy the
            # user did not configure for this app (Clash/V2Ray leftovers are the
            # common case on Windows), and a redirect must not silently move it.
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = client.get(
                f"{base}/search",
                params={"q": text, "format": "json"},
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as error:
        raise WebSearchError(f"the search endpoint could not be reached: {error}") from error

    if response.status_code != 200:
        raise WebSearchError(
            f"the search endpoint returned HTTP {response.status_code}"
        )
    raw = response.content
    if len(raw) > MAX_RESPONSE_BYTES:
        raise WebSearchError("the search endpoint returned an oversized response")

    try:
        payload = response.json()
    except ValueError as error:
        raise WebSearchError("the search endpoint did not return JSON") from error
    if not isinstance(payload, dict):
        raise WebSearchError("the search endpoint returned an unexpected document")

    results: list[dict[str, Any]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            # A `javascript:` or `file:` "result" would be rendered as a link.
            continue
        results.append(
            {
                "title": str(item.get("title") or url)[:MAX_TITLE_CHARS],
                "url": url[:2048],
                "snippet": str(item.get("content") or "")[:MAX_SNIPPET_CHARS],
            }
        )
        if len(results) >= count:
            break

    return {
        "tool": "web_search",
        "query": text,
        "endpoint_host": urlparse(base).hostname or "",
        "results": results,
        "truncated": len(results) >= count,
    }


def declaration() -> dict[str, Any]:
    """The tool schema, offered only when the feature is enabled."""
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web through the endpoint the user configured. Requires "
                "explicit approval for every call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    }
                },
                "required": ["query"],
            },
        },
    }
