# LunarShroud 2.0 — design decisions

This is the decision record for the 2.0 interface. Read it before changing styles;
check §6 afterwards.

## 1. Brief

**Subject.** A Windows desktop app for running local language models. The user brings
their own llama.cpp build (CPU / Vulkan / OpenVINO / ROCm) and their own GGUF files.
Nothing leaves the machine; there is no account and no telemetry.

**Audience.** People who already understand "a model is a file, the backend is a
process". They keep the app open for hours, run several instances at once, and watch
VRAM while chatting.

**The main screen's job.** Chat. Users come here to read long model output and to
accumulate conversations as assets. The dashboard, logs and knowledge base are *support
surfaces*, an order of magnitude less frequent than chat.

**Density.** Tight. Desktop tool: 14px body, 12px metadata, a 4px spacing step.

**Direction.** "月蚀界面" — the **look** of a macOS native tool, the **behaviour** of a
Windows native window.

This is an explicit deviation from polishkit's "one platform per window" rule, so the
reasoning is recorded: what is wanted is macOS *appearance* (type, whitespace, restrained
materials, control shapes), not macOS *behaviour*. So the window keeps its system chrome
(`decorations: true` — no hand-drawn traffic lights, which is the classic tell), keeps
Ctrl shortcuts, and keeps the 32px control-height floor. Type, layering, radii, motion
and the surface ladder follow the macOS language.

**Three refusals.** This design must not look like:

1. **An AI SaaS landing page** — no gradient text, no floating glow, no identical
   rounded cards with the same soft shadow around every block.
2. **Fake macOS** — no painted traffic lights, no imitation Mica, no ⌘ in Windows
   shortcut hints.
3. **1.0** — no three columns each with its own background and border; and no split
   visual language where the user message is a bright bubble and the assistant's answer
   is bare text.

## 2. The signature element

**The floating composer.** A 28px outer radius with an 18px send circle inset 10px
(concentric by construction), plus the model capsule, attachments, tools and a context
ring.

Everything else stays quiet so this can be the one memorable thing. It is not decoration:
it is the most-used control in the product, so it must be zero-latency, fully
keyboard-operable, and become "stop" while streaming without moving.

## 3. Surfaces

Three tiers, separated by **lightness** rather than by borders:

| Role | Light | Dark |
|---|---|---|
| canvas — the window ground | `#F2F2F5` | `#111113` |
| panel — sidebar, inspector, status bar | `#FBFBFD` | `#18181B` |
| surface — conversation area, popovers, cards | `#FFFFFF` | `#1F1F23` |

1.0 had only two tiers, so a card inside a panel had nowhere to sit and had to be
outlined — which is where the "every component gets a box" look came from.

## 4. Colour

The accent is deliberately narrow: **selection, focus ring, links, live status**. The
primary button is a near-black fill (`#1A1C1F` light / `#F2F2F5` dark), the macOS System
Settings vocabulary. This also avoids 1.0's flat blue everywhere.

**Measured pairs.** Every text pair was checked with the WCAG relative-luminance formula
against all three surfaces in both themes:

| Token | Light on canvas / panel / surface | Dark on canvas / panel / surface |
|---|---|---|
| `--text` | 15.28 / 16.52 / 17.08 | 16.58 / 15.58 / 14.44 |
| `--text-secondary` | 7.35 / 7.94 / 8.21 | 9.39 / 8.82 / 8.18 |
| `--text-tertiary` | 5.03 / 5.43 / 5.61 | 5.90 / 5.54 / 5.13 |
| `--success` | 5.11 / 5.52 / 5.71 | 10.97 / 10.30 / 9.55 |
| `--warning` | 4.86 / 5.25 / 5.43 | 10.22 / 9.60 / 8.90 |
| `--danger` | 5.04 / 5.44 / 5.63 | 7.47 / 7.02 / 6.51 |

Three candidates were **changed** during verification because they failed:
`--text-tertiary` (4.46 → 5.03 on canvas), `--success` (4.41 → 5.11), and
`--code-gutter` (3.56 → 5.54 on `--code-bg`). `--border-control` was added as a separate
token because `--border` is 1.30:1 and may only be decorative; a control's boundary needs
3:1 (WCAG 1.4.11).

`--text-quaternary` is below AA by design. It carries no meaning of its own — separators,
middots, counts inside a label the reader already has.

## 5. Type, shape, motion

- **UI**: Inter Variable (self-hosted, OFL). **Mono**: JetBrains Mono Variable. CJK falls
  back to `Microsoft YaHei UI`.
- **Scale**: 11 / 12 / 13 / 14 / 15 / 17 / 20 / 24 / 30. Body 14px; long-form reading
  15px at `line-height 1.78` for CJK.
- **Tabular figures** on every numeric column (`font-variant-numeric: tabular-nums`),
  otherwise the VRAM readout jitters while updating.
- **Radii** from one scale: 5 / 7 / 9 / 12 / 16 / 22 / 28 / pill. Concentric nesting:
  outer = inner + inset.
- **Motion**: 110ms for hover/focus, 170ms for expand/collapse, 260ms for overlay entry.
  Only `background-color`, `color`, `opacity`, `transform`. `prefers-reduced-motion`
  zeroes the durations.

## 6. Two rules that are gates, not advice

**Colour lives in `tokens.css` only.** `backend/tests/test_ui_tokens.py` fails on any hex,
`rgb()`, `hsl()` or `oklch()` literal in `ui/src`, and on any Tailwind palette utility
(`bg-blue-600`). A theme change cannot silently miss a component.

**A colour that changes between themes must not be applied as a Tailwind arbitrary
utility.** `@theme inline` substitutes the value at build time, so
`bg-[var(--primary-fill)]` stops tracking the `.theme-dark` override. Those roles go
through the plain classes in `app.css` (`.fill-primary`, `.fill-danger`, `.fill-success`).

Two related traps, both of which shipped during the rebuild and are now gated:

- `text-[var(--text-sm)]` compiles to **`color: var(--text-sm)`** — an invalid colour. The
  element falls back to inherited colour, and a white-on-white button (1.01:1) went
  unnoticed by every unit test. Use `text-[length:var(--text-sm)]`.
- `disabled:opacity-45` fades the label along with the fill, dropping a disabled button to
  3.1:1. Disabled state uses explicit neutral fill and text (4.81:1 / 6.07:1).

## 7. Self-check before shipping a visual change

- [ ] Every text pair passes WCAG AA over all three surfaces, in both themes
      (`python -m pytest backend/tests/test_ui_tokens.py`)
- [ ] Focus rings are visible in both themes and differ from hover
      (`polishkit/scripts/keyboard_audit.py`, plus a full-window capture)
- [ ] Colour never carries meaning alone: a status chip states its state in text
- [ ] CJK and the longest English string do not overflow (sidebar labels, model names)
- [ ] Every visual value comes from `tokens.css`
- [ ] Chat looks like the same app in all three states (empty / streaming / settled)
- [ ] `prefers-reduced-motion` disables positional animation
- [ ] `node ui/tests/visual/acceptance.mjs` reports 0 errors (100 page×width×theme
      combinations, plus the four forced states)
