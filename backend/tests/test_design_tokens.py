"""Design-token discipline: no orphan variables, no raw spacing/font/radius px."""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKENS = REPO_ROOT / "frontend" / "css" / "tokens.css"
STYLE = REPO_ROOT / "frontend" / "css" / "style.css"

TOKENIZED_PROPERTIES = {
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "gap", "row-gap", "column-gap",
    "font-size",
    "border-radius",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _declared_tokens() -> set[str]:
    return set(re.findall(r"(?m)^\s*(--[A-Za-z0-9-]+)\s*:", _read(TOKENS)))


def _style_rules() -> str:
    return _read(STYLE)


def test_style_only_references_declared_tokens():
    declared = _declared_tokens()
    referenced = set(re.findall(r"var\((--[A-Za-z0-9-]+)", _style_rules()))
    missing = sorted(referenced - declared)
    assert missing == [], f"style.css references undeclared tokens: {missing}"


def test_spacing_font_and_radius_values_are_tokenized():
    offenders: list[tuple[str, str]] = []
    for match in re.finditer(r"(?m)^\s*([a-z-]+)\s*:\s*([^;]+);", _style_rules()):
        prop, value = match.group(1), match.group(2)
        if prop not in TOKENIZED_PROPERTIES:
            continue
        if re.search(r"(?<![\w-])\d+px", value):
            offenders.append((prop, value.strip()))
    assert offenders == [], f"raw px in tokenized properties: {offenders}"


def test_required_token_families_exist():
    declared = _declared_tokens()
    for family in (
        "--color-primary-fill",
        "--color-on-primary",
        "--space-1",
        "--space-8",
        "--radius-md",
        "--font-size-sm",
        "--font-mono",
        "--z-modal",
    ):
        assert family in declared, family
