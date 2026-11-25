"""Optional web search.

The feature is an explicitly opted-in network call in an offline-first product, so
the tests are mostly about the **guards**: disabled means "no tool", a remote
endpoint is refused by default, a redirect is not followed, and the response is
bounded in both bytes and results.
"""
from __future__ import annotations

import json

import httpx
import pytest
from core import web_search


class _FakeResponse:
    def __init__(self, status: int, payload: object, *, oversized: bool = False):
        self.status_code = status
        self._payload = payload
        self.content = b"x" * (web_search.MAX_RESPONSE_BYTES + 10) if oversized else json.dumps(payload).encode()

    def json(self):
        return self._payload


def test_validate_refuses_a_remote_endpoint_by_default():
    with pytest.raises(web_search.WebSearchError, match="loopback"):
        web_search.validate_endpoint("http://192.168.1.20:8080", allow_remote=False)
    assert web_search.validate_endpoint("http://192.168.1.20:8080", allow_remote=True)


def test_validate_refuses_credentials_query_and_bad_scheme():
    for bad in (
        "http://user:pw@127.0.0.1:8080",
        "http://127.0.0.1:8080/?format=json",
        "ftp://127.0.0.1:8080",
        "not a url",
        "http://127.0.0.1:abc",
    ):
        with pytest.raises(web_search.WebSearchError):
            web_search.validate_endpoint(bad, allow_remote=True)


def test_search_returns_bounded_normalised_results(monkeypatch):
    payload = {
        "results": [
            {"title": "A", "url": "https://example.com/a", "content": "alpha"},
            {"title": "B", "url": "javascript:alert(1)", "content": "dropped"},
            {"title": "C", "url": "https://example.com/c", "content": "gamma"},
        ]
    }

    def fake_get(self, url, params=None, headers=None):
        assert url == "http://127.0.0.1:8080/search"
        assert params["q"] == "hello" and params["format"] == "json"
        return _FakeResponse(200, payload)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    result = web_search.search("hello", "http://127.0.0.1:8080", limit=5)

    assert result["tool"] == "web_search"
    assert result["endpoint_host"] == "127.0.0.1"
    assert [hit["url"] for hit in result["results"]] == [
        "https://example.com/a",
        "https://example.com/c",
    ], "a javascript: URL must never be returned as a result"


def test_search_never_follows_a_redirect(monkeypatch):
    seen = {}

    def fake_get(self, url, params=None, headers=None):
        seen["follow_redirects"] = self.follow_redirects
        return _FakeResponse(302, {})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    with pytest.raises(web_search.WebSearchError, match="302"):
        web_search.search("q", "http://127.0.0.1:8080")
    assert seen["follow_redirects"] is False


def test_search_never_uses_an_environment_proxy(monkeypatch):
    seen = {}

    def fake_get(self, url, params=None, headers=None):
        seen["trust_env"] = self.trust_env
        return _FakeResponse(200, {"results": []})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    web_search.search("q", "http://127.0.0.1:8080")
    assert seen["trust_env"] is False, "a search query must not travel through an unconfigured proxy"


def test_search_refuses_an_oversized_response(monkeypatch):
    monkeypatch.setattr(
        httpx.Client, "get", lambda self, url, params=None, headers=None: _FakeResponse(200, {}, oversized=True)
    )
    with pytest.raises(web_search.WebSearchError, match="oversized"):
        web_search.search("q", "http://127.0.0.1:8080")


def test_search_refuses_an_empty_or_huge_query():
    with pytest.raises(web_search.WebSearchError, match="empty"):
        web_search.search("   ", "http://127.0.0.1:8080")
    with pytest.raises(web_search.WebSearchError, match="longer"):
        web_search.search("x" * (web_search.MAX_QUERY_CHARS + 1), "http://127.0.0.1:8080")


def test_the_declaration_is_only_offered_when_enabled():
    from core import agent_declarations

    off = {tool["function"]["name"] for tool in agent_declarations.tool_declarations()}
    on = {
        tool["function"]["name"]
        for tool in agent_declarations.tool_declarations(web_search_enabled=True)
    }
    assert "web_search" not in off
    assert "web_search" in on


def test_the_execute_route_refuses_search_when_the_setting_is_off(monkeypatch):
    """A model can echo a tool name from an earlier turn; config must still win."""
    import asyncio

    import config as backend_config
    from api import chat as chat_api
    from fastapi import HTTPException

    monkeypatch.setitem(backend_config.get_config(), "web_search_enabled", False)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            chat_api.api_execute_chat_tool(
                {"name": "web_search", "arguments": {"query": "x"}, "root": ""}
            )
        )
    assert caught.value.status_code == 400
    assert "not enabled" in str(caught.value.detail)


def test_search_is_absent_from_the_config_by_default(monkeypatch):
    """The offline promise: a fresh install must not be able to reach the network."""
    import config as backend_config

    defaults = backend_config.DEFAULT_CONFIG
    assert defaults["web_search_enabled"] is False
    assert defaults["web_search_endpoint"] == ""
