"""Offline contract: the launcher must work with no internet connection.

These are static checks, so they run fast and do not need a browser or network.
They pin the properties that would otherwise regress silently:

* no remote scripts/styles/fonts in the shipped frontend;
* no remote ES-module imports (a CDN import would blank the UI offline);
* Tauri CSP only permits ``self``, Tauri's local IPC bridge, and loopback for
  runtime connections;
* the only external HTTP(S) links are user-clicked documentation links.

They do not claim that *llama-server* or a model is available; the launcher's
core contract is that it never needs the network to start or manage local
processes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _is_remote(url: str) -> bool:
    return url.startswith(("http://", "https://", "//"))


def test_index_has_no_remote_assets():
    html = _read(FRONTEND / "index.html")
    references = re.findall(r"""\b(?:src|href)\s*=\s*["']([^"']+)""", html)
    remote = [url for url in references if _is_remote(url)]
    assert remote == [], f"remote assets in index.html: {remote}"


def test_css_has_no_remote_imports_or_fonts():
    offenders: list[tuple[str, str]] = []
    for css in (FRONTEND / "css").glob("*.css"):
        text = _read(css)
        if "@import" in text:
            offenders.append((css.name, "@import"))
        for match in re.finditer(r"""url\(\s*['"]?([^'")]+)""", text):
            if _is_remote(match.group(1).strip()):
                offenders.append((css.name, match.group(1)))
    assert offenders == [], f"remote CSS resources: {offenders}"


def test_js_imports_are_relative():
    offenders: list[tuple[str, str]] = []
    pattern = re.compile(r"""(?:from\s+|import\s*\()\s*['"]([^'"]+)['"]""")
    for js in (FRONTEND / "js").rglob("*"):
        if js.suffix not in {".js", ".mjs"}:
            continue
        for specifier in pattern.findall(_read(js)):
            if not specifier.startswith(("./", "../")):
                offenders.append((js.name, specifier))
    assert offenders == [], f"remote/non-relative ES imports: {offenders}"


def test_tauri_csp_allows_only_self_ipc_and_loopback():
    config = json.loads(_read(REPO_ROOT / "src-tauri" / "tauri.conf.json"))
    csp = config["app"]["security"]["csp"]
    directives = {
        part.split()[0]: part.split()[1:]
        for part in csp.split(";")
        if part.strip()
    }
    assert "default-src" in directives
    assert set(directives["default-src"]) <= {"'self'", "asset:", "https://asset.localhost"}
    # Tauri's injected IPC client first tries http://ipc.localhost and falls
    # back to postMessage when CSP blocks it. Allowing the bridge avoids a
    # CSP error on every invoke; it is not a network endpoint.
    assert set(directives["connect-src"]) <= {
        "'self'",
        "ipc:",
        "http://ipc.localhost",
        "http://127.0.0.1:*",
        "ws://127.0.0.1:*",
    }
    assert config["build"]["frontendDist"] == "../frontend"
    assert config["app"]["windows"] == []


def test_only_help_links_to_the_public_internet():
    offenders: list[tuple[str, str]] = []
    for js in (FRONTEND / "js").rglob("*"):
        if js.suffix not in {".js", ".mjs"} or js.name == "help.js":
            continue
        for url in re.findall(r"https?://[^\s'\"<>)]+", _read(js)):
            # The Chat iframe URL is a template over a host already validated
            # as loopback by runtime.mjs; it is not a remote dependency.
            if "${" in url:
                continue
            host = urlparse(url).hostname or ""
            if host not in LOOPBACK_HOSTS:
                offenders.append((js.name, url))
    assert offenders == [], f"unexpected external URLs outside help.js: {offenders}"


def test_help_external_link_is_documentation_only():
    text = _read(FRONTEND / "js" / "help.js")
    urls = re.findall(r"https?://[^\s'\"<>)]+", text)
    assert urls, "the help page should point users at llama.cpp releases"
    for url in urls:
        assert urlparse(url).hostname == "github.com", f"unexpected help URL: {url}"
        assert "target=\"_blank\"" in text
