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


# --------------------------------------------------------------------------
# 对比度下限：调暗“空态”必须用更淡的**令牌**，不能用 opacity。
#
# 实测事故（2026-11-05，CI a11y 门禁）：`.hardware-chip.is-empty` 用
# `opacity: 0.55` 表示“没有 GPU 数据”，tertiary(#63636C) 与 text(#1C1C1F) 混到
# 浅背景后只剩 2.34:1 / 2.81:1（WCAG AA 要求 4.5:1）。开发者机器上芯片显示
# 真实 GPU 数据（不走 is-empty 分支），所以本地永远绿；无显卡的 CI 与无显卡
# 的用户每次启动都看着一块几乎读不清的芯片。
# --------------------------------------------------------------------------


def _srgb_channel(value: float) -> float:
    value /= 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_srgb_channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    l1, l2 = _relative_luminance(fg), _relative_luminance(bg)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def _parse_hex(block: str, name: str):
    match = re.search(re.escape(name) + r":\s*(#[0-9A-Fa-f]{6})\s*;", block)
    if not match:
        return None
    raw = match.group(1)
    return tuple(float(int(raw[i : i + 2], 16)) for i in (1, 3, 5))


def test_is_empty_states_never_dim_with_opacity():
    """`.is-empty` 规则不得用 opacity 调暗整块容器。

    透明度会同时稀释前景与边框，把任何令牌组合都拉到 WCAG AA 之下；“看起来
    更淡”必须改用更淡的颜色令牌（另一条测试钉住它们的比值下限）。
    """
    style = _style_rules()
    offenders = []
    for match in re.finditer(r"(?ms)^([^{}]*\.is-empty[^{}]*)\{([^}]*)\}", style):
        selector, body = match.group(1).strip(), match.group(2)
        opacity = re.search(r"(?m)^\s*opacity\s*:\s*([0-9.]+)\s*;", body)
        if opacity and float(opacity.group(1)) < 1.0:
            offenders.append(f"{selector} -> opacity: {opacity.group(1)}")
    assert not offenders, (
        "空态选择器用 opacity 调暗整块容器（会击穿对比度下限），"
        f"请改用更淡的颜色令牌: {offenders}"
    )


def test_empty_chip_colors_meet_wcag_aa_in_both_themes():
    """芯片空态用的 tertiary 文字色，在两个主题下对 surface 背景都要 ≥ 4.5:1。

    这是 `.hardware-chip.is-empty` 修复后钉下的下限：将来把 tertiary 调淡或把
    surface 调深时这条先红，而不是等 CI 的 axe 门禁在无显卡 runner 上报一个
    读不出原因的 color-contrast。
    """
    tokens = _read(TOKENS)
    # 必须带 " {" 找**规则块**：文件头部有一行注释也写着 body.theme-dark，
    # 只按名字找会把浅色块整体切进"深色"一侧（实测：返回 None 而失败）。
    dark_index = tokens.find("body.theme-dark {")
    assert dark_index > 0, "tokens.css 缺少 theme-dark 块，测试失去意义"
    light_block, dark_block = tokens[:dark_index], tokens[dark_index:]

    for theme, block in (("light", light_block), ("dark", dark_block)):
        fg = _parse_hex(block, "--color-text-tertiary")
        bg = _parse_hex(block, "--color-surface")
        assert fg is not None and bg is not None, (
            f"{theme} 主题缺少 --color-text-tertiary 或 --color-surface 的十六进制值"
        )
        ratio = _contrast(fg, bg)
        assert ratio >= 4.5, (
            f"{theme} 主题下 tertiary 文字对 surface 背景只有 {ratio:.2f}:1 "
            f"（要求 4.5:1）；空态芯片文本将不可读"
        )
