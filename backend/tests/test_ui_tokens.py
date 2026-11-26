"""Token discipline for the 2.0 UI.

Companion to the 1.0 `test_design_tokens.py`, rewritten for a React + Tailwind
codebase. Three classes of defect are pinned here, each because it actually
shipped during the rebuild:

1. **Colour literals outside the token file.** If a component hard-codes
   `#1a1c1f`, a theme change silently misses it.
2. **Ambiguous arbitrary Tailwind values.** `text-[var(--text-sm)]` compiles to
   `color: var(--text-sm)` in Tailwind 4 — an invalid colour, so the text falls
   back to inherited colour. Measured consequence: a white label on a white button
   (1.01:1) that every unit test passed and only the axe gate caught. 183 call
   sites had to be rewritten to `text-[length:var(--text-sm)]`.
3. **Theme-dependent roles applied through `@theme` utilities.** `@theme inline`
   substitutes the value at build time, so a token that changes under
   `.theme-dark` stops tracking its override. Those roles must go through the
   plain classes in `app.css` (`.fill-primary`, `.fill-danger`).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_SRC = REPO_ROOT / "ui" / "src"
TOKENS = UI_SRC / "styles" / "tokens.css"
APP_CSS = UI_SRC / "styles" / "app.css"

SOURCES = sorted(
    [*(UI_SRC.rglob("*.ts")), *(UI_SRC.rglob("*.tsx"))]
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _sources() -> list[tuple[Path, str]]:
    return [(path, _read(path)) for path in SOURCES if path.is_file()]


def test_ui_sources_exist():
    """A gate that scans nothing must fail, not pass vacuously."""
    assert SOURCES, f"no UI sources found under {UI_SRC}"
    assert TOKENS.is_file(), "tokens.css is missing"
    assert APP_CSS.is_file(), "app.css is missing"


def test_every_var_reference_names_a_declared_token():
    """A typo'd token is an *invalid declaration*, so the browser drops it silently.

    `z-[var(--z-dropdown)]` shipped on the conversation menu: `--z-dropdown` was
    never declared, so the menu had no stacking order and was painted **under** the
    conversation rows. Nothing reported an error — the menu was visible but its
    items could not be clicked, and only an `elementFromPoint` probe showed a row
    button where "New project" should have been.

    Same failure shape as the two issues already pinned in this file (a size token
    used as a colour, a colour token used as a size): Tailwind emits the class, the
    CSS parser rejects the value, the element silently keeps its default.
    """
    declared = set(re.findall(r"(?m)^\s*(--[A-Za-z0-9-]+)\s*:", _read(TOKENS)))
    # `app.css` declares locally scoped tokens too (the fill classes).
    declared |= set(re.findall(r"(?m)^\s*(--[A-Za-z0-9-]+)\s*:", _read(APP_CSS)))
    assert declared, "no custom properties found; the scan is broken"

    missing: dict[str, set[str]] = {}
    # Only references **without** a fallback: `var(--border-faint, var(--border))`
    # is a deliberate optional token, and the browser falls back cleanly. A bare
    # `var(--z-dropdown)` has no such safety net.
    pattern = re.compile(r"var\(\s*(--[A-Za-z0-9-]+)\s*\)")
    for path, text in _sources():
        for name in pattern.findall(text):
            if name not in declared:
                missing.setdefault(name, set()).add(path.name)

    assert not missing, (
        "these custom properties are referenced but never declared (so the CSS "
        f"declaration is silently dropped): { {k: sorted(v) for k, v in missing.items()} }"
    )


def test_tokens_file_declares_the_required_families():
    declared = set(re.findall(r"(?m)^\s*(--[A-Za-z0-9-]+)\s*:", _read(TOKENS)))
    for name in (
        "--canvas",
        "--panel",
        "--surface",
        "--text",
        "--text-secondary",
        "--text-tertiary",
        "--border",
        "--border-control",
        "--on-primary",
        "--primary-fill",
        "--danger",
        "--radius-lg",
        "--radius-composer",
        "--space-4",
        "--text-base",
        "--font-sans",
        "--font-mono",
        "--shadow-md",
        "--duration-base",
        "--z-modal",
        "--thread-max",
    ):
        assert name in declared, f"tokens.css does not declare {name}"


# --------------------------------------------------------------------------
# 1. No colour literals outside the token file
# --------------------------------------------------------------------------

# `emerald-500` etc. would also be a colour, but the project uses tokens only.
_TAILWIND_COLOUR_UTILITY = re.compile(
    r"\b(?:bg|text|border|ring|outline|fill|from|to|via)-(?:red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|slate|gray|zinc|neutral|stone|white|black)\b"
)
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_RGB = re.compile(r"\brgba?\(")
_HSL = re.compile(r"\bhsla?\(")
_OKLCH = re.compile(r"\boklch\(")


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_colour_literals_outside_tokens(path: Path):
    """Component code must reference tokens, never literal colours.

    Tailwind palette utilities are rejected too: they bypass the token layer and
    will not follow the theme.
    """
    text = _read(path)
    offenders: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        # Comment lines may discuss colours (several do, explaining measurements).
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*")):
            continue
        for pattern in (_HEX, _RGB, _HSL, _OKLCH):
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}: {stripped[:100]}")
                break
        else:
            match = _TAILWIND_COLOUR_UTILITY.search(line)
            if match:
                offenders.append(f"{path.name}:{number}: Tailwind palette utility '{match.group(0)}'")
    assert not offenders, (
        "colour literals / palette utilities outside tokens.css:\n" + "\n".join(offenders[:20])
    )


# --------------------------------------------------------------------------
# 2. No ambiguous arbitrary Tailwind values
# --------------------------------------------------------------------------

# Only *size* tokens are ambiguous. `--text`, `--text-secondary` and friends are
# genuine colours, so `text-[var(--text)]` is correct and must not be flagged.
_SIZE_TOKENS = r"(?:text-(?:2xs|xs|sm|base|md|lg|xl|2xl|3xl)|font-size-[a-z0-9-]+)"
_AMBIGUOUS_TEXT = re.compile(r"text-\[var\(--" + _SIZE_TOKENS + r"\)\]")
_AMBIGUOUS_RADIUS = re.compile(r"rounded-\[var\(--text-")


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_arbitrary_text_values_declare_their_type(path: Path):
    """`text-[var(--text-sm)]` is a colour to Tailwind, not a font size.

    It compiles to `color: var(--text-sm)`, which is invalid, so the element falls
    back to inherited colour. Force the `length:` hint instead.
    """
    text = _read(path)
    offenders = [
        f"{path.name}:{number}: {line.strip()[:100]}"
        for number, line in enumerate(text.splitlines(), start=1)
        if _AMBIGUOUS_TEXT.search(line) or _AMBIGUOUS_RADIUS.search(line)
    ]
    assert not offenders, (
        "ambiguous arbitrary Tailwind value; use `text-[length:var(--text-sm)]`:\n"
        + "\n".join(offenders[:20])
    )


# The reverse mistake: a *colour* token forced into the font-size slot.
# `text-[length:var(--text-tertiary)]` compiles to `font-size: var(--text-tertiary)`,
# which is not a length, so the declaration is dropped and the element silently
# inherits the body font size. Measured 2026-11-24: 66 sites, including every help
# paragraph on the parameter page — which is a large part of why it read as
# "descriptions dumped inline, bloated interface": the text was full-size and
# full-contrast instead of a muted caption.
_COLOUR_TOKEN_IN_SIZE_SLOT = re.compile(
    r"text-\[length:var\(--(?!text-(?:2xs|xs|sm|base|md|lg|xl|2xl|3xl)\b)"
    r"text(?:-[a-z]+)*\)\]"
)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_colour_tokens_are_never_used_as_a_font_size(path: Path):
    """`text-[length:var(--text-tertiary)]` silently loses the font size.

    `--text-tertiary` is a colour. Used as a length it is invalid, so Tailwind emits
    a declaration the browser discards, and the element inherits the ambient size.
    The intent at those sites was always "small muted text", so both halves belong
    together: a size token for the size and a colour token for the colour.
    """
    text = _read(path)
    offenders = [
        f"{path.name}:{number}: {line.strip()[:100]}"
        for number, line in enumerate(text.splitlines(), start=1)
        if _COLOUR_TOKEN_IN_SIZE_SLOT.search(line)
    ]
    assert not offenders, (
        "a colour token was used as a font size; write "
        "`text-[length:var(--text-xs)] text-[var(--text-tertiary)]`:\n"
        + "\n".join(offenders[:20])
    )


def test_compiled_css_never_sets_colour_from_a_size_token():
    """Belt and braces for the above, checked against a real build when present.

    Skipped when `ui/dist` has not been built: the source-level test is the gate,
    this one catches a new spelling the regexes do not know about.
    """
    dist = REPO_ROOT / "ui" / "dist" / "assets"
    if not dist.is_dir():
        pytest.skip("ui/dist not built")
    css = "".join(p.read_text(encoding="utf-8", errors="replace") for p in dist.glob("*.css"))
    if not css:
        pytest.skip("ui/dist has no CSS bundle")
    bad = re.findall(r"color:var\(--(?:text-(?:xs|sm|base|md|lg|xl|2xl|3xl)|radius-[a-z0-9-]+)\)", css)
    assert not bad, f"CSS sets colour from a size/radius token: {sorted(set(bad))}"


# --------------------------------------------------------------------------
# 3. Theme-dependent roles go through classes, not @theme utilities
# --------------------------------------------------------------------------

# Tokens whose value changes under `.theme-dark`. Applying them with
# `bg-[var(--x)]` works only by luck: Tailwind may bake the value in.
THEME_SCOPED = ("--on-primary", "--primary-fill", "--primary-fill-hover", "--on-danger", "--danger-fill")


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_theme_scoped_roles_are_not_applied_through_utilities(path: Path):
    text = _read(path)
    offenders = [
        f"{path.name}:{number}: {line.strip()[:110]}"
        for number, line in enumerate(text.splitlines(), start=1)
        for token in THEME_SCOPED
        if f"bg-[var({token})]" in line or f"text-[var({token})]" in line
    ]
    assert not offenders, (
        "theme-dependent token applied through an arbitrary utility; use the "
        "`.fill-primary` / `.fill-danger` classes instead:\n" + "\n".join(offenders[:20])
    )


def test_fill_classes_are_declared_with_token_vars():
    app = _read(APP_CSS)
    for selector in (".fill-primary", ".fill-danger"):
        assert selector in app, f"{selector} is missing from app.css"
    # The declaration must reference the token, not a baked value.
    block = re.search(r"\.fill-primary\s*\{([^}]*)\}", app)
    assert block, ".fill-primary has no rule"
    assert "var(--primary-fill)" in block.group(1)
    assert "var(--on-primary)" in block.group(1)


# --------------------------------------------------------------------------
# Contrast floors, measured on the token values themselves
# --------------------------------------------------------------------------

def _srgb_channel(value: float) -> float:
    value /= 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_srgb_channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def _hex(block: str, name: str) -> tuple[float, float, float] | None:
    match = re.search(re.escape(name) + r":\s*(#[0-9A-Fa-f]{6})\s*;", block)
    if not match:
        return None
    raw = match.group(1)
    return tuple(float(int(raw[i : i + 2], 16)) for i in (1, 3, 5))  # type: ignore[return-value]


def _theme_blocks() -> list[tuple[str, str]]:
    tokens = _read(TOKENS)
    dark_index = tokens.find(":root.theme-dark {")
    assert dark_index > 0, "tokens.css has no :root.theme-dark block"
    return [("light", tokens[:dark_index]), ("dark", tokens[dark_index:])]


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("surface", ["--surface", "--panel", "--canvas"])
@pytest.mark.parametrize("foreground", ["--text", "--text-secondary", "--text-tertiary"])
def test_text_meets_wcag_aa_on_every_surface(theme: str, surface: str, foreground: str):
    blocks = dict(_theme_blocks())
    block = blocks[theme]
    fg = _hex(block, foreground)
    bg = _hex(block, surface)
    assert fg is not None and bg is not None, f"{theme}: missing {foreground} or {surface}"
    ratio = _contrast(fg, bg)
    assert ratio >= 4.5, f"{theme}: {foreground} on {surface} is {ratio:.2f}:1 (needs 4.5)"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_primary_button_label_is_readable(theme: str):
    """The exact pairing that shipped as a 1.01:1 white-on-white button."""
    block = dict(_theme_blocks())[theme]
    fg = _hex(block, "--on-primary")
    bg = _hex(block, "--primary-fill")
    assert fg is not None and bg is not None
    ratio = _contrast(fg, bg)
    assert ratio >= 4.5, f"{theme}: primary button label is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_control_border_reaches_three_to_one(theme: str):
    """WCAG 1.4.11: a control's boundary must be discernible (≥3:1).

    The hairline `--border` is decorative and sits near 1.3:1 by design; inputs
    must use `--border-control`.
    """
    block = dict(_theme_blocks())[theme]
    border = _hex(block, "--border-control")
    surface = _hex(block, "--surface")
    assert border is not None and surface is not None
    ratio = _contrast(border, surface)
    assert ratio >= 3.0, f"{theme}: control border on surface is {ratio:.2f}:1"


def test_code_panel_text_meets_wcag_aa_in_both_themes():
    """The code panel keeps a fixed dark ground in both themes, so its text tokens must
    be checked against that ground rather than against `--surface`.

    `--code-gutter` was #6b7078, which measured 3.56:1 on #16181d — the log timestamp and
    the "no logs yet" hint were both below AA in the light theme.
    """
    tokens = _read(TOKENS)
    dark_index = tokens.find(":root.theme-dark {")
    light_block, dark_block = tokens[:dark_index], tokens[dark_index:]
    for theme, block in (("light", light_block), ("dark", dark_block)):
        ground = _hex(block, "--code-bg")
        assert ground is not None, f"{theme}: missing --code-bg"
        for name in ("--code-fg", "--code-gutter"):
            fg = _hex(block, name)
            assert fg is not None, f"{theme}: missing {name}"
            ratio = _contrast(fg, ground)
            assert ratio >= 4.5, f"{theme}: {name} on --code-bg is {ratio:.2f}:1 (needs 4.5)"


def _accent_blocks() -> list[tuple[str, str, str]]:
    """Every `data-accent` preset block as `(theme, accent name, body)`.

    The blocks live at the end of tokens.css and are written as
    `:root[data-accent='x']` (light) and `:root.theme-dark[data-accent='x']` (dark).
    """
    tokens = _read(TOKENS)
    found: list[tuple[str, str, str]] = []
    # Two selector shapes: `[data-accent='x']` (any element — the light values and
    # the settings swatches) and `:root.theme-dark[data-accent='x']` (dark variants).
    # The dark ones are parsed first so the lighter match can skip their ranges.
    dark_pattern = re.compile(
        r":root\.theme-dark\[data-accent='([a-z]+)'\]\s*\{([^}]*)\}"
    )
    dark_ranges: list[tuple[int, int]] = []
    for match in dark_pattern.finditer(tokens):
        found.append(("dark", match.group(1), match.group(2)))
        dark_ranges.append((match.start(), match.end()))
    for match in re.finditer(r"\[data-accent='([a-z]+)'\]\s*\{([^}]*)\}", tokens):
        if any(start <= match.start() < end for start, end in dark_ranges):
            continue
        found.append(("light", match.group(1), match.group(2)))
    return found


def test_every_accent_preset_meets_wcag_aa_in_both_themes():
    """Accent colours are a **closed set** precisely so this can be proven.

    `--accent` is used two ways: as a fill with white/black text on it, and as text
    on `--surface`. Those need different lightnesses — the shipped blue is
    `#2c5fd6` for fills and a darker `#1f4fc0` for text — which is why a free colour
    picker cannot be made safe and the setting is a list of presets instead.

    This is the gate that keeps the list honest: add a preset and it must pass both
    checks in both themes, or the suite goes red. Measured failures here are real —
    `--text-quaternary` shipped below AA twice (AGENTS §2.44).
    """
    blocks = _accent_blocks()
    assert blocks, "no data-accent preset blocks found; the scan is broken"
    light_surface = _hex(dict(_theme_blocks())["light"], "--surface")
    dark_surface = _hex(dict(_theme_blocks())["dark"], "--surface")
    assert light_surface is not None and dark_surface is not None

    failures: list[str] = []
    for theme, name, body in blocks:
        surface = light_surface if theme == "light" else dark_surface
        fill = _hex(body, "--accent")
        text = _hex(body, "--accent-text")
        if fill is None or text is None:
            failures.append(f"{theme}/{name}: missing --accent or --accent-text")
            continue
        # Text use: the token must be readable on the page surface.
        text_ratio = _contrast(text, surface)
        if text_ratio < 4.5:
            failures.append(f"{theme}/{name}: --accent-text on surface {text_ratio:.2f}:1")
        # Fill use: a filled control carries `--on-primary`-like contrast. The accent
        # fill is used behind light text in the light theme and dark text in the dark
        # theme, so check against whichever of white/black is actually used there.
        on_fill = (255.0, 255.0, 255.0) if theme == "light" else (17.0, 17.0, 17.0)
        fill_ratio = _contrast(on_fill, fill)
        if fill_ratio < 4.5:
            failures.append(f"{theme}/{name}: text on --accent fill {fill_ratio:.2f}:1")

    assert not failures, "accent presets below WCAG AA:\n" + "\n".join(failures)


def test_every_font_preset_has_a_css_block_and_sets_the_body_token():
    """A preset offered in the UI with no CSS block is a control that does nothing.

    Same shape as the missing `--z-dropdown`: the button renders, `aria-pressed`
    flips, and the page never changes. This reconciles the list in `AppearanceTab`
    against the `[data-font='…']` blocks in tokens.css, in both directions.
    """
    appearance = (UI_SRC / "features" / "settings" / "tabs" / "AppearanceTab.tsx").read_text(
        encoding="utf-8"
    )
    match = re.search(r"const FONT_PRESETS = \[([^\]]+)\]", appearance)
    assert match, "cannot find FONT_PRESETS in AppearanceTab.tsx"
    offered = set(re.findall(r"'([a-z]+)'", match.group(1)))
    assert offered, "FONT_PRESETS is empty"

    tokens = _read(TOKENS)
    declared: dict[str, str] = {}
    for block in re.finditer(r"\[data-font='([a-z]+)'\]\s*\{([^}]*)\}", tokens):
        declared[block.group(1)] = block.group(2)

    assert not sorted(offered - set(declared)), (
        "font presets offered by the UI with no CSS block (picking them does "
        f"nothing): {sorted(offered - set(declared))}"
    )
    assert not sorted(set(declared) - offered), (
        f"font CSS blocks nothing offers: {sorted(set(declared) - offered)}"
    )
    # Each block must actually retarget the body token; setting some other property
    # would compile and have no effect.
    for name, body in declared.items():
        assert "--font-body" in body, f"font preset {name!r} does not set --font-body"


def test_high_contrast_actually_increases_contrast_in_both_themes():
    """The `data-contrast='high'` block must be strictly stronger than the defaults.

    A "high contrast" switch whose values happen to be equal — or worse, weaker — is
    a control that does nothing, which is the same failure as a typo'd CSS token: it
    compiles, it renders, and the user concludes their eyes are the problem.

    Each overriding block is compared against the theme it belongs to, on the surface
    that theme paints text on.
    """
    tokens = _read(TOKENS)
    blocks = dict(_theme_blocks())
    light_block, dark_block = blocks["light"], blocks["dark"]
    light_surface = _hex(light_block, "--surface")
    dark_surface = _hex(dark_block, "--surface")
    assert light_surface is not None and dark_surface is not None

    high_blocks = {
        "light": re.search(r":root\[data-contrast='high'\]\s*\{([^}]*)\}", tokens),
        "dark": re.search(r":root\.theme-dark\[data-contrast='high'\]\s*\{([^}]*)\}", tokens),
    }
    assert high_blocks["light"] and high_blocks["dark"], (
        "missing a data-contrast='high' block for one of the themes"
    )

    problems: list[str] = []
    for theme in ("light", "dark"):
        base = light_block if theme == "light" else dark_block
        surface = light_surface if theme == "light" else dark_surface
        high = high_blocks[theme]
        assert high is not None
        for name in ("--text-secondary", "--text-tertiary", "--text-quaternary"):
            before = _hex(base, name)
            after = _hex(high.group(1), name)
            if before is None or after is None:
                problems.append(f"{theme}: {name} missing from one of the blocks")
                continue
            before_ratio = _contrast(before, surface)
            after_ratio = _contrast(after, surface)
            if after_ratio <= before_ratio:
                problems.append(
                    f"{theme}: {name} went from {before_ratio:.2f}:1 to "
                    f"{after_ratio:.2f}:1 — high contrast must be stronger"
                )
            if after_ratio < 4.5:
                problems.append(
                    f"{theme}: {name} is only {after_ratio:.2f}:1 in high contrast"
                )

    assert not problems, "high-contrast overrides are not stronger:\n" + "\n".join(problems)


def test_the_accent_gate_rejects_a_low_contrast_preset():
    """Meta-check: the gate above must actually fail on a bad pair.

    A contrast test that cannot fail is worse than none — it reads as a guarantee.
    This feeds the same arithmetic a preset that is obviously wrong and asserts it
    is rejected, so a future refactor of `_rest`/`_contrast` cannot silently neuter
    the real check.
    """
    white = (255.0, 255.0, 255.0)
    surface = _hex(dict(_theme_blocks())["light"], "--surface")
    assert surface is not None
    # A pastel accent as *text* on a white surface: the exact mistake a free colour
    # picker invites.
    assert _contrast((255.0, 240.0, 200.0), surface) < 4.5
    # And the same value as a *fill* behind white text also fails.
    assert _contrast(white, (255.0, 240.0, 200.0)) < 4.5
