"""The Tauri `frontendDist` directory is embedded into the shipped shell.

Anything placed inside it ships. Two failure modes this pins, both measured:

* A `node_modules` tree inside the web root makes `cargo tauri build` refuse
  outright ("The configured frontendDist includes the [`node_modules`] folder").
  In 2.0 the Node toolchain lives in `ui/`, and `ui/node_modules` must never end up
  in the build output — Vite's `public/` directory is copied verbatim into `dist`,
  so a test-only dependency placed there (axe-core is the tempting one) would ship.
* Scratch output — `tmp/`, `.mimosa/`, a stray JS manifest — just bloats the binary.

The 2.0 web root is `ui/dist`, produced by `npm run build`. `ui/` itself is the
source project, so this gate checks the **build output**, not the project tree.
"""
from __future__ import annotations

import json
from pathlib import Path

import config
import pytest

# A built web root contains exactly these: the entry point, hashed JS/CSS bundles,
# fonts, images/icons, and anything Vite copied from `public/`.
ALLOWED_DIRS = {"assets"}
ALLOWED_SUFFIXES = {
    ".html",
    ".js",
    ".mjs",
    ".css",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".ico",
    ".webp",
    ".avif",
    ".json",
    ".txt",
    ".webmanifest",
}

# Never inside a shipped web root, at any depth.
FORBIDDEN_DIRS = {
    "node_modules",
    "src",
    "tests",
    ".mimosa",
    ".claude",
    ".vscode",
    ".idea",
    "__pycache__",
}

# Files that must never be copied into the build by `public/`.
FORBIDDEN_NAMES = {
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "playwright.config.ts",
    "vite.config.ts",
    "axe.min.js",  # a 560 KB auditor belongs to the test harness, not the binary
}

# Test-only artefacts that may appear while iterating locally; they are gitignored
# and never part of a clean build.
IGNORED = {"lost+found"}


def _web_root() -> Path:
    conf = json.loads(
        (config.PROJECT_ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    return (config.PROJECT_ROOT / "src-tauri" / conf["build"]["frontendDist"]).resolve()


def test_frontend_dist_points_at_the_built_react_app():
    """The shell must embed the 2.0 build output, not the 1.0 tree."""
    conf = json.loads(
        (config.PROJECT_ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    assert conf["build"]["frontendDist"] == "../ui/dist", (
        "frontendDist must point at the built React app (ui/dist)"
    )


def test_web_root_contains_only_shippable_app_assets():
    dist = _web_root()
    if not (dist / "index.html").is_file():
        pytest.skip("ui/dist has not been built; run `npm run build` in ui/")

    offenders: list[str] = []
    for entry in dist.iterdir():
        if entry.name in IGNORED:
            continue
        if entry.is_dir():
            if entry.name not in ALLOWED_DIRS:
                offenders.append(f"{entry.name}/ (directory)")
            continue
        if entry.name in FORBIDDEN_NAMES:
            offenders.append(f"{entry.name} (forbidden file)")
            continue
        if entry.suffix.lower() not in ALLOWED_SUFFIXES:
            offenders.append(f"{entry.name} (unexpected suffix)")

    assert offenders == [], f"files would be embedded into the shipped shell: {offenders}"


def test_web_root_never_contains_source_or_dependencies():
    """Recursive check: `public/` is copied verbatim, so a mistake lands here."""
    dist = _web_root()
    if not (dist / "index.html").is_file():
        pytest.skip("ui/dist has not been built")

    offenders: list[str] = []
    for path in dist.rglob("*"):
        relative = path.relative_to(dist)
        for part in relative.parts[:-1]:
            if part in FORBIDDEN_DIRS:
                offenders.append(str(relative))
                break
        else:
            if path.name in FORBIDDEN_NAMES:
                offenders.append(str(relative))

    assert offenders == [], f"source/dependency files inside the web root: {sorted(set(offenders))[:20]}"
