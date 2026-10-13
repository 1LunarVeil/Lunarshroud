"""图标资产几何回归测试：圆角必须有真正的透明通道。

历史缺陷：`src-tauri/icons/` 下全部资产（含 `icon.ico` 的 6 个尺寸）每一个像素
alpha 都是 255——美术里画了圆角，但圆角之外填的是不透明深色。Windows 合成时拿到
的是一整块方形贴片，所以桌面快捷方式和任务栏呈现为"正正方方"。这些测试钉住
"四角必须透明"和圆角半径，防止再次退化成假圆角。

半径 17.2% 是实测参照值而非审美偏好：对参照图标逐行拟合，普通圆弧平均误差
1.84px，超椭圆（iOS squircle，n=2.5/4）误差 8.4~9.0px，所以用圆角矩形掩膜。
"""
from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import gen_icons  # noqa: E402

# 半径比例取生成器的常量，避免测试与实现各写一个数然后悄悄分叉
RADIUS_PCT = gen_icons.RADIUS_PCT

# 圆角矩形在对角方向上，弧线与对角线交点距角点 r*(1 - 1/sqrt(2))。这是精确的，
# 所以用对角线量半径；直边不行——真圆弧在 y=0 那一行已向内弯了约 sqrt(r)，
# 首个不透明像素落在 r-sqrt(r) 而不是 r 上。
DIAGONAL_FACTOR = 1.0 - 2.0**-0.5

STANDALONE = [
    "src-tauri/icons/icon.png",
    "src-tauri/icons/128x128.png",
    "src-tauri/icons/32x32.png",
    "frontend/assets/logo.png",
]

ICO_SIZES = {16, 32, 48, 64, 128, 256}


def _ico_entries(blob: bytes) -> list[tuple[int, int, bytes]]:
    """解析 ICO 目录，返回 (宽, 高, 图像数据)。"""
    _reserved, _type, count = struct.unpack("<HHH", blob[:6])
    entries = []
    for n in range(count):
        head = blob[6 + 16 * n : 6 + 16 * n + 16]
        w, h, _clr, _rsv, _planes, _bits, size, offset = struct.unpack("<BBBBHHII", head)
        entries.append((w or 256, h or 256, blob[offset : offset + size]))
    return entries


def _corner_arc_start(px, w: int, h: int, corner: str) -> int:
    """沿对角线自角点向外扫描，返回第一个不透明像素的偏移步数。"""
    for i in range(min(w, h)):
        x = i if corner in ("TL", "BL") else w - 1 - i
        y = i if corner in ("TL", "TR") else h - 1 - i
        if px[x, y][3] >= 128:
            return i
    raise AssertionError(f"{corner}: no opaque pixel along the diagonal")


def _assert_rounded(img: Image.Image, label: str) -> None:
    assert img.mode == "RGBA", f"{label}: expected RGBA, got {img.mode}"
    w, h = img.size
    px = img.load()

    # 1. 四角必须近乎透明——这正是历史缺陷的判据（当时每个角都是 255）。上界不是 0
    #    是因为掩膜带抗锯齿：16px 圆角最外那个像素几何上就被覆盖了约 2%。
    for name, (x, y) in {
        "TL": (0, 0),
        "TR": (w - 1, 0),
        "BL": (0, h - 1),
        "BR": (w - 1, h - 1),
    }.items():
        assert px[x, y][3] <= 16, f"{label}: corner {name} alpha={px[x, y][3]}, expected ~0"

    # 2. 中心与四边中点必须不透明：证明是圆角矩形，不是整圆也不是整块透明
    assert px[w // 2, h // 2][3] == 255, f"{label}: center is not opaque"
    assert px[w // 2, 0][3] == 255, f"{label}: top edge midpoint is not opaque"
    assert px[0, h // 2][3] == 255, f"{label}: left edge midpoint is not opaque"

    # 3. 四个角的圆弧必须都落在声明半径上
    radius = max(1, round(min(w, h) * RADIUS_PCT))
    expected = round(radius * DIAGONAL_FACTOR)
    for corner in ("TL", "TR", "BL", "BR"):
        got = _corner_arc_start(px, w, h, corner)
        assert abs(got - expected) <= 1, (
            f"{label}: {corner} arc starts at {got}, expected ~{expected} (radius {radius})"
        )


@pytest.mark.parametrize("rel", STANDALONE)
def test_standalone_icons_have_real_transparent_corners(rel: str) -> None:
    with Image.open(ROOT / rel) as im:
        _assert_rounded(im.convert("RGBA"), rel)


def test_ico_entries_all_have_real_transparent_corners() -> None:
    entries = _ico_entries((ROOT / "src-tauri/icons/icon.ico").read_bytes())
    assert len(entries) == 6, f"expected 6 ICO sizes, got {len(entries)}"
    seen = set()
    for w, h, data in entries:
        assert data[:4] == b"\x89PNG", f"{w}x{h} entry is not PNG-encoded"
        with Image.open(io.BytesIO(data)) as im:
            _assert_rounded(im.convert("RGBA"), f"icon.ico:{w}x{h}")
        seen.add(w)
    assert seen == ICO_SIZES, f"unexpected ICO size set: {sorted(seen)}"


def test_small_sizes_have_antialiased_corners() -> None:
    """16/32px 的圆弧必须有中间灰度，否则是二值掩膜留下的阶梯锯齿。"""
    entries = dict((w, data) for w, _h, data in _ico_entries(
        (ROOT / "src-tauri/icons/icon.ico").read_bytes()))
    for size in (16, 32):
        with Image.open(io.BytesIO(entries[size])) as im:
            hist = im.convert("RGBA").getchannel("A").histogram()
        soft = sum(hist[1:255])
        assert soft > 8, f"{size}px ICO entry has {soft} partial-alpha px; mask is not smoothed"


def test_generator_radius_constant_matches_measured_reference() -> None:
    """17.2% 来自参照图标实测（116px 宽、r=20px），改动必须是有意决定。"""
    assert abs(gen_icons.RADIUS_PCT - 0.172) < 0.005, gen_icons.RADIUS_PCT


def test_generator_is_deterministic() -> None:
    """同一母图跑两次必须逐字节一致，否则无法判断资产有没有被手改。"""
    a = gen_icons.render_all()
    b = gen_icons.render_all()
    assert a.keys() == b.keys()
    for name in a:
        assert a[name] == b[name], f"{name}: generator output is not deterministic"


def test_committed_assets_match_generator() -> None:
    """提交的资产必须等于生成器输出。按像素比较，跨 Pillow 版本的压缩差异不算漂移。"""
    for rel, blob in gen_icons.render_all().items():
        path = ROOT / rel
        assert path.exists(), f"{rel} missing from the repository"
        with Image.open(io.BytesIO(blob)) as new, Image.open(path) as old:
            n, o = new.convert("RGBA"), old.convert("RGBA")
            assert n.size == o.size, f"{rel}: size {o.size} != generated {n.size}"
            # tobytes() 而非 getdata()：后者在 Pillow 12 已弃用，前者跨版本稳定
            assert n.tobytes() == o.tobytes(), (
                f"{rel}: committed asset differs from tools/gen_icons.py output; "
                "regenerate instead of hand-editing"
            )
