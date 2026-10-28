"""Stage and zip the portable runtime.

The zip contains exactly what the repository-root pair contains — the shell
(which embeds the frontend) plus the PyInstaller onedir backend — and nothing
else. It never bundles llama-server or any model: the user supplies those.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VERSION = "0.9.0-beta.2"
NAME = f"LunarShroud-{VERSION}-portable"
STAGE = REPO / "release" / NAME
ZIP_PATH = REPO / "release" / f"{NAME}-x64.zip"

README = """LunarShroud {version} 便携版（Windows x64）
==========================================

免安装：解压后直接运行 LunarShroud.exe。不要只复制其中一个 exe ——
backend\\backend.exe 与它旁边的 backend\\_internal\\ 是一整套运行时，
拆开就会启动失败。

首次使用需要两样东西（本包不包含，也不自动下载）：
  1. 对应显卡的 llama-server.exe（llama.cpp 官方 Releases）
  2. 一个 GGUF 模型文件
打开「设置 → 路径」分别指定，然后在「模型工作区」里扫描并启动。

llama-server 按显卡选包（文件名里含）：
  NVIDIA RTX 30/40/50         bin-win-cuda12.x-x64
  AMD 独显（ROCm 支持列表内）  bin-win-rocm-*-x64
  AMD 其它（含核显）           bin-win-vulkan-x64
  Intel 核显 / Arc / NPU      bin-win-openvino-*-x64
  只有 CPU                    bin-win-cpu-x64
装好后到「模型工作区 → 运行环境」查看这份构建在本机实际枚举到的设备；
多卡机器可在参数面板的「计算设备」里指定设备名。

数据存放位置：
  配置、聊天、历史与知识库默认在 %APPDATA%\\LunarShroud\\。
  如果该目录不可写（受限账户 / 沙箱），会自动回退到
  backend\\data\\ —— 这让便携版可以在没有安装权限的机器上运行。
  想做到"整包带走"，把上面两个目录一起拷贝即可。

安全说明：
  - 管理端口只监听 127.0.0.1，并带一次性会话令牌；
  - 不联网、不检查更新、不上传遥测；
  - 不打包、不下载 llama-server，模型与运行时都由你自己提供。

卸载：删除整个文件夹即可。若使用过受限回退，再删掉 backend\\data\\。
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    launcher = REPO / "LunarShroud.exe"
    backend = REPO / "backend" / "backend.exe"
    internal = REPO / "backend" / "_internal"
    for path in (launcher, backend, internal):
        if not path.exists():
            print(f"missing: {path}; deploy the pair first")
            return 2

    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    shutil.copy2(launcher, STAGE / "LunarShroud.exe")
    for extra in ("LICENSE", "README.md"):
        source = REPO / extra
        if source.exists():
            shutil.copy2(source, STAGE / extra)
    (STAGE / "使用说明.txt").write_text(
        README.format(version=VERSION), encoding="utf-8", newline="\r\n"
    )
    shutil.copytree(backend.parent, STAGE / "backend", dirs_exist_ok=True)

    ZIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    # Deterministic entries: a release hash is only useful if rebuilding the
    # same inputs produces the same bytes. File mtimes would otherwise leak in.
    stamp = (2026, 10, 24, 0, 0, 0)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(STAGE.rglob("*")):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(str(Path(NAME) / path.relative_to(STAGE)), date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())

    total = sum(1 for _ in STAGE.rglob("*") if _.is_file())
    print(f"staged files: {total}")
    print(f"zip: {ZIP_PATH}")
    print(f"zip size: {ZIP_PATH.stat().st_size / (1024*1024):.1f} MiB")
    print(f"zip sha256: {sha256(ZIP_PATH)}")
    print(f"launcher sha256: {sha256(launcher)}")
    print(f"backend sha256: {sha256(backend)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
