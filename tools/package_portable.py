"""Stage and zip the portable runtime.

The zip contains exactly what the repository-root pair contains — the shell
(which embeds the frontend) plus the PyInstaller onedir backend — and nothing
else. It never bundles llama-server or any model: the user supplies those.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TAURI_CONF = REPO / "src-tauri" / "tauri.conf.json"
CARGO_TOML = REPO / "src-tauri" / "Cargo.toml"


def read_version() -> str:
    """版本号**只有一处真相**：`src-tauri/tauri.conf.json`。

    此前版本号在 tauri.conf.json / Cargo.toml / package.json / 本文件里各写
    一份。改漏一处就会产出"文件名说 1.0.0、里面却是 0.9"的包，而每一步构建都
    成功 —— 这类缺陷发布之后才被发现，用户拿到的东西与说明对不上。
    """
    data = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    version = str(data.get("version") or "").strip()
    if not version:
        raise SystemExit(f"no version in {TAURI_CONF}")
    return version


def read_cargo_version() -> str:
    for line in CARGO_TOML.read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("no version in src-tauri/Cargo.toml")


VERSION = read_version()
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


def read_launcher_file_version(path: Path) -> str:
    """读 launcher exe 里内嵌的 FileVersion（Windows VERSIONINFO）。

    为什么需要这道检查：清单一致性只保证"配置文件们互相同意"，它**看不见
    二进制本身**。一次真实踩坑：版本升到 1.0.0、后端重建了、清单全绿，但
    仓库根的 `LunarShroud.exe` 还是上一版的构建产物 —— 打出来的包叫
    1.0.0-portable，里面的壳却报 0.9.0-beta.2。用户装完看到的版本与说明不符，
    而所有自动化检查都是绿的。

    非 Windows 或读不到版本时返回空串（不阻断，见调用方）。
    """
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        version_dll = ctypes.WinDLL("version")
        size = version_dll.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(str(path), 0, size, buffer):
            return ""
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version_dll.VerQueryValueW(
            buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
        ):
            return ""
        # VS_FIXEDFILEINFO: 13 个 DWORD；第 2/3 个是文件版本 MS/LS
        fixed = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD * 13)).contents
        ms, ls = int(fixed[2]), int(fixed[3])
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (OSError, AttributeError, ValueError):
        return ""


def versions_equivalent(embedded: str, declared: str) -> bool:
    """比较 Windows VERSIONINFO 的四段版本与语义版本。

    VERSIONINFO 的 FileVersion 结构上是**四段**（VS_FIXEDFILEINFO 里两个
    DWORD），所以 `1.0.0` 一定被写成 `1.0.0.0`。直接用 `!=` 比较，任何
    合法构建都会被判成"过期"—— 那是把工具实现细节当成了版本差异。

    只把 missing 的第四段补 0 再比；**不做**前缀放宽（`1.0` 不等于 `1.0.0.1`），
    否则真·旧版本会被放过去，这道门就白加了。
    """
    def pad(parts: list[str]) -> list[str]:
        padded = list(parts)
        while len(padded) < 4:
            padded.append("0")
        return padded

    left = pad(embedded.split("."))
    right = pad(declared.split("."))
    return left == right


def inspect_zip_contents(zip_path: Path, name: str) -> list[str]:
    """读回 zip 的真实条目清单，返回"不该出现的东西"列表（空 = 通过）。

    校验分两面：
      * **禁止项**：源码、测试、用户数据、工具状态、日志、缓存。这些一旦进包
        就是隐私泄露。`data/` 是最严重的一类——它装的是开发机上真实的
        chats.db / history.db / config.json / instance 日志。
      * **白名单结构**：`backend/` 下只许有 `backend.exe` 与 `_internal/...`。
        白名单比黑名单可靠：将来新增的目录（例如今天才出现的 `.mimosa/`）
        不会因为没人补排除规则而漏出去。

    同时在包名层面拒绝 `__pycache__`、`.pyc`、`.log`、`.db`、`tests`、`build`、
    `dist` 这些在任何位置出现都不属于运行时的东西。
    """
    import zipfile

    forbidden_dir_names = {
        "tests", "test", "build", "dist", ".mimosa", "data",
        "__pycache__", ".git", ".venv", "node_modules", ".pytest_cache",
    }
    forbidden_suffixes = (".pyc", ".pyo", ".log", ".db", ".db-wal", ".db-shm", ".py")
    problems: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        for entry in archive.infolist():
            full = entry.filename.replace("\\", "/")
            if not full.startswith(f"{name}/"):
                problems.append(f"entry outside the package root: {full}")
                continue
            rel = full[len(name) + 1:]
            if not rel or rel.endswith("/"):
                continue  # directory marker
            parts = rel.split("/")

            for part in parts[:-1]:
                if part.lower() in forbidden_dir_names:
                    problems.append(f"forbidden directory '{part}' in {rel}")
                    break
            if any(part.lower() in forbidden_dir_names for part in parts[:-1]):
                continue
            if rel.lower().endswith(forbidden_suffixes):
                problems.append(f"forbidden file type in {rel}")
                continue

            # backend/ 下只能是 backend.exe 或 _internal/...（白名单）。
            if len(parts) >= 2 and parts[0] == "backend":
                if parts[1] == "backend.exe":
                    continue
                if parts[1] == "_internal":
                    continue
                problems.append(f"backend/ may only contain backend.exe and _internal/, found {rel}")
            elif len(parts) >= 2:
                # 包根只允许"使用说明.txt"这类顶层文件，不允许额外目录。
                problems.append(f"unexpected top-level directory in package: {rel}")

    # 去重并保持顺序，避免同一目录下每个文件报一条同样的错。
    seen: set[str] = set()
    unique: list[str] = []
    for item in problems:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def main() -> int:
    # 版本一致性是打包的**前提**，不是事后检查：包名由 tauri.conf.json 决定，
    # 而 launcher 二进制由 Cargo.toml 决定。两者不一致时产出的是一个自相矛盾的
    # 分发包，所以在这里直接拒绝，而不是打出来再让用户发现。
    cargo_version = read_cargo_version()
    if cargo_version != VERSION:
        print(
            f"version mismatch: tauri.conf.json={VERSION} but Cargo.toml={cargo_version}; "
            "refusing to build a package whose name contradicts its contents"
        )
        return 3

    launcher = REPO / "LunarShroud.exe"
    backend = REPO / "backend" / "backend.exe"
    internal = REPO / "backend" / "_internal"
    for path in (launcher, backend, internal):
        if not path.exists():
            print(f"missing: {path}; deploy the pair first")
            return 2

    # 二进制自己的版本号也必须对得上。清单一致但 exe 是旧构建时，包名会撒谎。
    embedded = read_launcher_file_version(launcher)
    if embedded and not versions_equivalent(embedded, VERSION):
        print(
            f"stale launcher: {launcher.name} reports {embedded} but this release is "
            f"{VERSION}; rebuild the shell (cargo tauri build) and re-deploy, "
            "or the package name will contradict its contents"
        )
        return 4
    if not embedded:
        print("note: could not read the launcher's embedded version; skipping that check")

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
    # 便携包的 `backend/` 目录**只允许**装运行时必需的两项：
    #   backend.exe + _internal/
    #
    # 这里以前是 `shutil.copytree(backend.parent, ...)`，即把仓库的整个
    # `backend/` 源码目录搬进分发包。实测后果（2026-11-02 对发布包逐条清点）：
    #   tests/   102 个文件，含 test_api_integration.py 等**硬编码测试令牌**
    #   data/    用户真实数据：chats.db、history.db、config.json、instance 日志、
    #            7 个本机预设 —— 等于把开发机的聊天记录随公开包发出去
    #   .mimosa/ 41 个文件，AI 编码工具的内部状态与源码基线快照
    #   dist/    769 个文件（dist/backend 副本的二次嵌套）、build/ 16、
    #            __pycache__/ 4、backend-child.log
    # 这些既不是运行时，也绝不能出现在用户下载的 zip 里。
    #
    # 用白名单而不是黑名单：新增的目录（今天的 .mimosa 就是）不会因为有人忘了
    # 更新排除规则而泄漏。漏掉必需文件会在下面的自检里立刻被发现。
    backend_dest = STAGE / "backend"
    backend_dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backend, backend_dest / "backend.exe")
    shutil.copytree(internal, backend_dest / "_internal")

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

    # ── 发布前自检（fail-closed）──
    #
    # 白名单复制已经排除了源码目录，但这一步要独立地把关：**从 zip 自己**读回
    # 条目清单再判断，而不是相信 staging 目录的构造过程。构造逻辑将来被改动时，
    # 只有"读回真实产物"的检查还能拦住泄漏。
    problems = inspect_zip_contents(ZIP_PATH, NAME)
    if problems:
        print("refusing to publish: the zip contains entries it must never contain:")
        for item in problems:
            print(f"  - {item}")
        try:
            ZIP_PATH.unlink()
        except OSError:
            pass
        return 5

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
