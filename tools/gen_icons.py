"""从母图派生全部图标资产。

    py tools/gen_icons.py            # 重写所有产物
    py tools/gen_icons.py --check    # 只检查已提交资产是否与母图一致

为什么需要这个脚本：`src-tauri/icons/` 下的文件是成品，不能反复加工——圆角掩膜
套两次就会把构图再放大一次。所以母图单独存成 `icons/artwork.png`（不透明、未
掩膜），所有尺寸一律从它派生。改圆角请改下面的常量后重跑，不要手改 PNG。

两个几何参数都是实测出来的，不是审美选择：

* ``RADIUS_PCT`` = 17.2%。参照图标 116px 宽、圆角 20px。逐行拟合角弧，普通圆弧
  平均误差 1.84px，超椭圆（iOS squircle，n=2.5 或 4）误差 8.4~9.0px，因此用
  圆角矩形掩膜而不是 squircle。
* ``ZOOM`` = 1.16。母图自带一圈不透明深色画框；只做掩膜会把这圈框留下，贴在
  浅色桌面上表现为沿圆角边缘的暗带（像脏边或阴影）。放大 1.16 倍居中裁切把画框
  推出画布，1.24 倍开始把构图挤紧，故取 1.16。

小尺寸不做 alpha 重采样：先在目标尺寸上缩放开图（无 alpha），再按该尺寸的整数
半径套掩膜。对已带透明边的图做 resize 会在圆角处产生暗色镶边。
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]

ARTWORK = ROOT / "src-tauri" / "icons" / "artwork.png"

RADIUS_PCT = 0.172
ZOOM = 1.16

MASTER_SIZE = 512
PNG_SIZES = {
    "src-tauri/icons/icon.png": 512,
    "src-tauri/icons/128x128.png": 128,
    "src-tauri/icons/32x32.png": 32,
    "frontend/assets/logo.png": 512,
}
ICO_SIZES = (16, 32, 48, 64, 128, 256)
ICO_PATH = "src-tauri/icons/icon.ico"


def radius_for(size: int) -> int:
    """给定边长，返回按 RADIUS_PCT 取整的圆角半径。"""
    return max(1, round(size * RADIUS_PCT))


def _rounded_mask(size: int, r: int, supersample: int = 8) -> Image.Image:
    """构造圆角矩形 alpha 掩膜：两条矩形带 + 四个圆盘，超采样后降采样得到抗锯齿边。

    不用 ImageDraw.rounded_rectangle：它在 48px 和 128px 上半径会偏 1px，而显式
    构造在每个尺寸上都精确。超采样是为了让 16/32px 的圆弧不呈阶梯状。
    """
    s = size * supersample
    rr = r * supersample
    mask = Image.new("L", (s, s), 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle([0, rr, s - 1, s - 1 - rr], fill=255)  # horizontal band
    draw.rectangle([rr, 0, s - 1 - rr, s - 1], fill=255)  # vertical band
    for cx, cy in ((rr, rr), (s - 1 - rr, rr), (rr, s - 1 - rr), (s - 1 - rr, s - 1 - rr)):
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=255)  # corner disc
    return mask.resize((size, size), Image.LANCZOS)


def _master(artwork: Image.Image) -> Image.Image:
    """放大 ZOOM 倍后居中裁回 MASTER_SIZE，把母图自带的深色画框推出画布。"""
    rgb = artwork.convert("RGB")
    side = int(round(MASTER_SIZE * ZOOM))
    grown = rgb.resize((side, side), Image.LANCZOS)
    off = (side - MASTER_SIZE) // 2
    return grown.crop((off, off, off + MASTER_SIZE, off + MASTER_SIZE))


def _rounded(master: Image.Image, size: int) -> Image.Image:
    """缩放到 size 并在该尺寸上套圆角矩形 alpha 掩膜。"""
    im = master if size == MASTER_SIZE else master.resize((size, size), Image.LANCZOS)
    out = im.convert("RGBA")
    out.putalpha(_rounded_mask(size, radius_for(size)))
    return out


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _ico(images: dict[int, Image.Image]) -> bytes:
    """手工拼 ICO：保证每个条目都是 PNG 编码，且半径按各自尺寸取整。"""
    order = sorted(images)
    blobs = [_png(images[n]) for n in order]
    offset = 6 + 16 * len(order)
    entries = []
    for n, blob in zip(order, blobs, strict=True):
        # 256px 在目录里按 0 编码
        edge = n % 256
        entries.append(struct.pack("<BBBBHHII", edge, edge, 0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
    header = struct.pack("<HHH", 0, 1, len(order))
    return header + b"".join(entries) + b"".join(blobs)


def render_all() -> dict[str, bytes]:
    """返回 {仓库相对路径: 字节}，覆盖全部图标产物。"""
    with Image.open(ARTWORK) as artwork:
        master = _master(artwork)
        rendered = {rel: _png(_rounded(master, size)) for rel, size in PNG_SIZES.items()}
        ico_images = {size: _rounded(master, size) for size in ICO_SIZES}
        rendered[ICO_PATH] = _ico(ico_images)
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只比对，不写文件")
    args = parser.parse_args(argv)

    rendered = render_all()
    if args.check:
        stale = []
        for rel, blob in rendered.items():
            path = ROOT / rel
            if not path.exists() or path.read_bytes() != blob:
                stale.append(rel)
        if stale:
            print("stale (re-run gen_icons.py):", file=sys.stderr)
            for rel in stale:
                print("  -", rel, file=sys.stderr)
            return 1
        print(f"ok: {len(rendered)} assets match {ARTWORK.name}")
        return 0

    for rel, blob in rendered.items():
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        print(f"wrote {rel} ({len(blob)} bytes)")
    print(f"radius pct={RADIUS_PCT} zoom={ZOOM} ico sizes={ICO_SIZES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
