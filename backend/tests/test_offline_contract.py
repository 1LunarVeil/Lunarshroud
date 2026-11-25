"""Offline contract: the launcher must work with no internet connection.

Rewritten for the 2.0 React app. These are static checks, so they run fast and need
no browser or network. They pin the properties that would otherwise regress
silently:

* no remote scripts, styles or fonts in the built bundle;
* no remote module imports — a CDN import would blank the UI when offline;
* fonts are **self-hosted files**, not an `@import` from a font CDN;
* the Tauri CSP only permits `self`, Tauri's local IPC bridge, and loopback;
* external HTTP(S) links exist only where a user clicks them.

They do not claim that *llama-server* or a model is available; the contract is that
the launcher never needs the network to start or manage local processes.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "ui"
UI_SRC = UI / "src"
UI_DIST = UI / "dist"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _is_remote(url: str) -> bool:
    return url.startswith(("http://", "https://", "//"))


def _src_files(suffixes: set[str]) -> list[Path]:
    return [path for path in UI_SRC.rglob("*") if path.suffix in suffixes and path.is_file()]


def test_index_has_no_remote_assets():
    html = _read(UI / "index.html")
    references = re.findall(r"""\b(?:src|href)\s*=\s*["']([^"']+)""", html)
    remote = [url for url in references if _is_remote(url)]
    assert remote == [], f"remote assets in ui/index.html: {remote}"


def test_css_declares_no_remote_imports_or_fonts():
    """Fonts must be bundled files.

    1.0 was dependency-free so this was easy. 2.0 adds Inter and JetBrains Mono:
    they come from `@fontsource-variable/*` packages and are emitted into
    `dist/assets/*.woff2`. A `@import url(https://fonts.googleapis.com/...)` here
    would be an outbound request in an app whose whole premise is "no network",
    and the UI would render in a fallback face offline.
    """
    offenders: list[tuple[str, str]] = []
    for css in UI_SRC.rglob("*.css"):
        text = _read(css)
        for match in re.finditer(r"""@import\s+(?:url\()?['"]?([^'")\s;]+)""", text):
            specifier = match.group(1)
            if _is_remote(specifier):
                offenders.append((css.name, specifier))
        for match in re.finditer(r"""url\(\s*['"]?([^'")]+)""", text):
            if _is_remote(match.group(1).strip()):
                offenders.append((css.name, match.group(1)))
    assert offenders == [], f"remote CSS resources: {offenders}"


def test_theme_boot_is_a_local_classic_script():
    """The pre-paint theme script must be same-origin, and must not be a module.

    An inline script would force `'unsafe-inline'` on `script-src`, the strictest
    directive in the policy. A *module* is deferred and would run after the first
    paint — which is exactly the white flash it exists to prevent.
    """
    html = _read(UI / "index.html")
    assert re.search(r'<script\s+src="\.\/theme-boot\.js"', html), (
        "index.html must load theme-boot.js as a same-origin classic script"
    )
    # Strip comments before looking for inline code: the file *explains* the policy
    # in a comment, and a naive substring check matches the explanation rather than
    # the markup.
    markup = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", markup, re.S)
    assert scripts, "index.html has no script tags"
    # No script may be inline: every one must have a `src`. The application entry
    # is a module (`/src/main.tsx`), which is correct and deferred; only the theme
    # bootstrap must be classic so it runs before the first paint.
    for attributes, body in scripts:
        assert "src=" in attributes, f"inline script would need 'unsafe-inline': {body[:80]}"
    theme = [attributes for attributes, _ in scripts if "theme-boot.js" in attributes]
    assert theme, "theme-boot.js is not loaded"
    assert "type=\"module\"" not in theme[0], (
        "theme-boot must be a classic script so it runs before the first paint"
    )


def test_ts_imports_never_resolve_to_a_remote_module():
    """Specifiers are relative, `@/` aliases, or bare package names — never URLs."""
    offenders: list[tuple[str, str]] = []
    pattern = re.compile(r"""(?:from\s+|import\s*\()\s*['"]([^'"]+)['"]""")
    for source in _src_files({".ts", ".tsx"}):
        for specifier in pattern.findall(_read(source)):
            if _is_remote(specifier):
                offenders.append((source.name, specifier))
    assert offenders == [], f"remote module imports: {offenders}"


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
    # Tauri's injected IPC client first tries http://ipc.localhost and falls back
    # to postMessage when CSP blocks it. Allowing the bridge avoids a CSP error on
    # every invoke; it is not a network endpoint.
    assert set(directives["connect-src"]) <= {
        "'self'",
        "ipc:",
        "http://ipc.localhost",
        "http://127.0.0.1:*",
        "ws://127.0.0.1:*",
    }
    # The shell embeds the built React app.
    assert config["build"]["frontendDist"] == "../ui/dist"
    # The window is created in Rust (`lib.rs`) so it can carry the WebView2 data
    # directory; an entry here would create a second, unconfigured window.
    assert config["app"]["windows"] == []


def test_self_hosted_fonts_come_from_bundled_packages():
    entry = _read(UI_SRC / "styles" / "app.css")
    tokens = _read(UI_SRC / "styles" / "tokens.css")
    assert "--font-sans" in tokens and "--font-mono" in tokens
    assert "@fontsource-variable/inter" in entry, (
        "Inter must be imported from the bundled @fontsource package"
    )
    assert "@fontsource-variable/jetbrains-mono" in entry
    assert "fonts.googleapis.com" not in entry
    assert "fonts.gstatic.com" not in entry


def test_built_html_references_no_remote_asset():
    """Bundler output check: source-level rules cannot see inlined URLs."""
    if not (UI_DIST / "index.html").is_file():
        pytest.skip("ui/dist has not been built")
    html = _read(UI_DIST / "index.html")
    remote = [
        url
        for url in re.findall(r"""\b(?:src|href)\s*=\s*["']([^"']+)""", html)
        if _is_remote(url)
    ]
    assert remote == [], f"built index.html references remotes: {remote}"


def test_remote_urls_exist_only_as_clicked_documentation():
    """A remote URL in app code may be a link the user clicks, never a dependency."""
    offenders: list[tuple[str, str]] = []
    for source in _src_files({".ts", ".tsx"}):
        text = _read(source)
        for url in re.findall(r"https?://[^\s'\"<>)]+", text):
            if "${" in url:
                continue  # loopback template built at runtime
            host = urlparse(url).hostname or ""
            if host in LOOPBACK_HOSTS or host.endswith(".localhost"):
                continue
            # Documentation links live on the help page and are opened externally.
            if source.name in {"HelpPage.tsx", "PendingPage.tsx"}:
                continue
            offenders.append((source.name, url))
    assert offenders == [], f"non-documentation remote URLs in app code: {offenders}"


def test_web_search_is_the_only_network_feature_and_it_is_off_by_default():
    """The one opted-in exception must not quietly become the default.

    `docs/OFFLINE.md` now says: off by default, endpoint supplied by the user,
    every call approved. Those are three separate claims that can drift
    independently, so each is pinned here rather than only in the docs.

    The "no baked-in provider" assertion matters most: a hard-coded search URL in
    `web_search.py` would make "where do my queries go" a fact only the source code
    knows, which is exactly the property this contract exists to prevent.
    """
    backend_dir = REPO_ROOT / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import config as backend_config  # noqa: E402

    defaults = backend_config.DEFAULT_CONFIG
    assert defaults["web_search_enabled"] is False, (
        "web search must be off on a fresh install"
    )
    assert defaults["web_search_endpoint"] == "", (
        "no endpoint may be pre-filled; the user supplies it"
    )

    search_source = (REPO_ROOT / "backend" / "core" / "web_search.py").read_text(encoding="utf-8")
    # Only loopback literals may appear in the module: a real provider host here
    # would be a built-in service the user never chose.
    for url in re.findall(r"https?://[^\s'\"<>)]+", search_source):
        host = urlparse(url).hostname or ""
        assert host in LOOPBACK_HOSTS, (
            f"web_search.py hard-codes a remote endpoint ({host}); the endpoint must "
            "come from configuration"
        )
    assert "trust_env=False" in search_source, (
        "the search client must not inherit a proxy from the environment"
    )
    assert "follow_redirects=False" in search_source, (
        "a redirect would silently move the user's query to another host"
    )
