from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

BOM = b"\xef\xbb\xbf"
SCRATCH_DIRS = {"target", "node_modules", "dist", "build", "tmp", ".git", "__pycache__"}


def _source_ps1_files():
    root = Path(__file__).resolve().parents[2]
    for path in sorted(root.rglob("*.ps1")):
        if SCRATCH_DIRS & set(path.parts):
            continue
        yield path


def test_non_ascii_ps1_scripts_declare_utf8():
    """含非 ASCII 注释的 .ps1 必须带 UTF-8 BOM，否则 Windows PowerShell 5.1 会读错。

    5.1 没有 BOM 就按机器 ANSI 代码页（中文机器 = GBK）解码：UTF-8 中文被拆成
    乱码字节对，注释行可能吞掉紧随其后的**代码行**。实测症状是 param 块里
    `[string]$LauncherSource,` 整行消失、脚本报 NamedParameterNotFound，而不报
    任何语法错误 —— CI 用 shell: pwsh（强制 UTF-8）永远发现不了，只有装 5.1 的
    用户会遇到。纯 ASCII 脚本不受影响，所以只约束带非 ASCII 字节的那些。
    """
    offenders = [str(p) for p in _source_ps1_files() if not _is_utf8_safe(p)]
    assert not offenders, "缺少 UTF-8 BOM 的非 ASCII PowerShell 脚本: " + ", ".join(offenders)


def test_setup_script_installs_from_the_hash_locked_closure():
    """开发引导脚本必须装 CI 用的同一份哈希锁，而不是现场解析 requirements.txt。

    否则"全新 checkout 构建"与发布产物的传递依赖闭包可能不同，可重复构建与
    依赖审计都会失去意义。顺带钉住 rustup 的 PATH 兜底（安装器改了 PATH，
    但已经打开的 shell 看不到）。
    """
    root = Path(__file__).resolve().parents[2]
    text = (root / "setup.ps1").read_text(encoding="utf-8-sig")
    # 只看 "pip install ... -r <file>" 那一步；`pip install --upgrade pip`
    # 不装依赖，不该被当成闭包安装。
    install_lines = [
        line
        for line in text.splitlines()
        if "-m pip install" in line and " -r " in line
    ]
    assert install_lines, "setup.ps1 里找不到安装依赖的 pip install -r 步骤"
    assert all(
        "--require-hashes" in line and "requirements-dev.lock" in line
        for line in install_lines
    ), install_lines
    assert not any("requirements-dev.txt" in line for line in install_lines)
    assert ".cargo\\bin" in text


def _is_utf8_safe(path: Path) -> bool:
    data = path.read_bytes()
    if not any(byte > 127 for byte in data):
        return True
    return data.startswith(BOM)


def test_portable_runtime_deployer_stages_matching_launcher_and_backend(tmp_path):
    """A desktop shortcut must never point at a launcher without its sidecar."""
    root = Path(__file__).resolve().parents[2]
    deployer = root / "tools" / "Publish-PortableRuntime.ps1"
    launcher_source = tmp_path / "release-launcher.exe"
    runtime = tmp_path / "dist" / "backend"
    backend_source = runtime / "backend.exe"
    destination = tmp_path / "portable"
    launcher_source.write_bytes(b"new launcher")
    backend_source.parent.mkdir(parents=True)
    backend_source.write_bytes(b"new backend")
    # onedir 是唯一的产品布局：缺 _internal 的部署必须被拒绝。
    (runtime / "_internal").mkdir()
    (runtime / "_internal" / "python311.dll").write_bytes(b"runtime")

    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(deployer),
            "-LauncherSource",
            str(launcher_source),
            "-BackendSource",
            str(backend_source),
            "-Destination",
            str(destination),
            "-SkipDesktopShortcut",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert (destination / "LunarShroud.exe").read_bytes() == launcher_source.read_bytes()
    assert (destination / "backend" / "backend.exe").read_bytes() == backend_source.read_bytes()
    assert hashlib.sha256((destination / "LunarShroud.exe").read_bytes()).hexdigest() == hashlib.sha256(launcher_source.read_bytes()).hexdigest()


def test_portable_runtime_deployer_copies_the_onedir_runtime(tmp_path):
    """backend.exe 必须与 _internal/ 一起部署，否则 onedir 后端启动即失败。

    单独复制 exe 会得到一个找不到 Python 运行时的 backend.exe：启动器只会报
    “后端启动失败”，用户完全看不出是部署缺文件。部署脚本必须整棵复制并逐文件
    校验 SHA-256。
    """
    root = Path(__file__).resolve().parents[2]
    deployer = root / "tools" / "Publish-PortableRuntime.ps1"
    launcher_source = tmp_path / "release-launcher.exe"
    runtime = tmp_path / "dist" / "backend"
    backend_source = runtime / "backend.exe"
    internal = runtime / "_internal"
    destination = tmp_path / "portable"
    launcher_source.write_bytes(b"new launcher")
    backend_source.parent.mkdir(parents=True)
    backend_source.write_bytes(b"new onedir backend")
    internal.mkdir()
    (internal / "python311.dll").write_bytes(b"python runtime")
    (internal / "sub").mkdir()
    (internal / "sub" / "base_library.zip").write_bytes(b"stdlib")

    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(deployer),
            "-LauncherSource", str(launcher_source),
            "-BackendSource", str(backend_source),
            "-Destination", str(destination),
            "-SkipDesktopShortcut",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert (destination / "backend" / "backend.exe").read_bytes() == backend_source.read_bytes()
    assert (destination / "backend" / "_internal" / "python311.dll").read_bytes() == b"python runtime"
    assert (destination / "backend" / "_internal" / "sub" / "base_library.zip").read_bytes() == b"stdlib"


def test_portable_runtime_deployer_rejects_an_incomplete_onedir_build(tmp_path):
    """只有 backend.exe、没有 _internal 的构建必须拒绝部署。

    旧脚本把它当成 onefile 并删除目标上已有的 _internal —— 部署后后端
    找不到 Python 运行时，用户只看到"后端启动失败"。
    """
    root = Path(__file__).resolve().parents[2]
    deployer = root / "tools" / "Publish-PortableRuntime.ps1"
    launcher_source = tmp_path / "release-launcher.exe"
    backend_source = tmp_path / "release-backend.exe"
    destination = tmp_path / "portable"
    launcher_source.write_bytes(b"new launcher")
    backend_source.write_bytes(b"new backend")

    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(deployer),
            "-LauncherSource", str(launcher_source),
            "-BackendSource", str(backend_source),
            "-Destination", str(destination),
            "-SkipDesktopShortcut",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "incomplete" in (result.stdout + result.stderr).lower()
    assert not (destination / "backend" / "backend.exe").exists()


def test_deployer_resolves_relative_destination_from_powershell_location(tmp_path):
    r"""`-Destination .` must follow PowerShell's location, not .NET's cwd.

    2026-10-17 实测：在 `Set-Location <root>` 后运行部署脚本，`[IO.Path]::GetFullPath(".")`
    仍指向进程启动目录 src-tauri，脚本把新对复制到 src-tauri\ 并把桌面快捷方式
    指过去，而不是工作区根目录。这里让 PowerShell location 与进程 cwd 故意不同。
    """
    root = Path(__file__).resolve().parents[2]
    deployer = root / "tools" / "Publish-PortableRuntime.ps1"
    launcher_source = tmp_path / "release-launcher.exe"
    runtime = tmp_path / "dist" / "backend"
    backend_source = runtime / "backend.exe"
    launcher_source.write_bytes(b"new launcher")
    backend_source.parent.mkdir(parents=True)
    backend_source.write_bytes(b"new backend")
    (runtime / "_internal").mkdir()
    (runtime / "_internal" / "python311.dll").write_bytes(b"runtime")
    target_parent = tmp_path / "chosen-parent"
    elsewhere = tmp_path / "elsewhere"
    target_parent.mkdir()
    elsewhere.mkdir()
    command = (
        f"Set-Location -LiteralPath '{target_parent}'; "
        f"& '{deployer}' -LauncherSource '{launcher_source}' "
        f"-BackendSource '{backend_source}' -Destination 'portable' -SkipDesktopShortcut"
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-Command", command,
        ],
        cwd=str(elsewhere),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (target_parent / "portable" / "LunarShroud.exe").exists()
    assert not (elsewhere / "portable").exists(), "relative destination ignored Get-Location"


def _run_ps1(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_user_data_backup_and_restore_round_trip(tmp_path):
    """备份→篡改→恢复必须可逆，且恢复前保留 pre-restore 快照。"""
    root = Path(__file__).resolve().parents[2]
    backup_script = root / "tools" / "Backup-UserData.ps1"
    restore_script = root / "tools" / "Restore-UserData.ps1"
    data = tmp_path / "LunarShroud"
    backup = tmp_path / "backup"
    (data / "presets").mkdir(parents=True)
    (data / "logs").mkdir()
    original_config = '{"launcher_port":17520,"theme":"dark"}'
    (data / "config.json").write_text(original_config, encoding="utf-8")
    (data / "history.db").write_text("synthetic-history-db", encoding="utf-8")
    (data / "presets" / "a.json").write_text('{"name":"a"}', encoding="utf-8")
    (data / "logs" / "backend.log").write_text("synthetic log", encoding="utf-8")

    result = _run_ps1(
        backup_script,
        "-DataDirectory", str(data),
        "-Destination", str(backup),
        "-Force",
    )
    assert result.returncode == 0, result.stderr
    assert (backup / "SHA256SUMS.txt").exists()
    assert (backup / "config.json").read_text(encoding="utf-8") == original_config
    assert (backup / "presets" / "a.json").exists()
    assert not (backup / "logs").exists(), "logs must be excluded by default"

    (data / "config.json").write_text('{"launcher_port":9999}', encoding="utf-8")
    (data / "presets" / "a.json").unlink()

    result = _run_ps1(
        restore_script,
        "-BackupDirectory", str(backup),
        "-DataDirectory", str(data),
        "-Force",
    )
    assert result.returncode == 0, result.stderr
    assert (data / "config.json").read_text(encoding="utf-8") == original_config
    assert (data / "presets" / "a.json").exists()
    assert list(tmp_path.glob("LunarShroud.pre-restore-*")), "pre-restore snapshot missing"


def test_user_data_restore_rejects_tampered_backup(tmp_path):
    """备份文件被改动时必须在覆盖当前数据之前失败。"""
    root = Path(__file__).resolve().parents[2]
    backup_script = root / "tools" / "Backup-UserData.ps1"
    restore_script = root / "tools" / "Restore-UserData.ps1"
    data = tmp_path / "LunarShroud"
    backup = tmp_path / "backup"
    data.mkdir()
    (data / "config.json").write_text('{"launcher_port":17520}', encoding="utf-8")
    (data / "history.db").write_text("history", encoding="utf-8")

    result = _run_ps1(backup_script, "-DataDirectory", str(data), "-Destination", str(backup), "-Force")
    assert result.returncode == 0, result.stderr
    (backup / "config.json").write_text('{"tampered":true}', encoding="utf-8")

    result = _run_ps1(restore_script, "-BackupDirectory", str(backup), "-DataDirectory", str(data), "-Force")
    assert result.returncode != 0
    assert "checksum mismatch" in (result.stdout + result.stderr).lower()
    # 当前数据未被篡改过的备份覆盖
    assert (data / "config.json").read_text(encoding="utf-8") == '{"launcher_port":17520}'


def test_user_data_restore_rejects_manifest_traversal(tmp_path):
    """SHA256SUMS 里的 ../ 路径不能把恢复写出备份目录。"""
    root = Path(__file__).resolve().parents[2]
    restore_script = root / "tools" / "Restore-UserData.ps1"
    data = tmp_path / "LunarShroud"
    backup = tmp_path / "backup"
    data.mkdir()
    backup.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    (backup / "SHA256SUMS.txt").write_text(f"{digest} *../outside.json\n", encoding="utf-8")

    result = _run_ps1(restore_script, "-BackupDirectory", str(backup), "-DataDirectory", str(data), "-Force")
    assert result.returncode != 0
    assert "escaped" in (result.stdout + result.stderr).lower()


def _param_block_code_lines(text: str):
    """param(...) 块内的非注释行（多行与单行 param 块都识别）。"""
    lines = text.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("param(")),
        None,
    )
    if start is None:
        return []
    head = lines[start].split("param(", 1)[1]
    block = []
    if ")" in head:
        block.append(head.split(")", 1)[0])
    else:
        block.append(head)
        for line in lines[start + 1:]:
            stripped = line.strip()
            if stripped.startswith(")"):
                block.append(stripped.split(")", 1)[0])
                break
            block.append(line)
    return [line for line in block if line.strip() and not line.strip().startswith("#")]


def test_ps1_param_defaults_never_use_psscriptroot():
    """param() 的默认值里不得出现 $PSScriptRoot。

    Windows PowerShell 5.1 在求值 param() 默认值时 $PSScriptRoot 还是空字符串，
    Join-Path 会立刻抛 ParameterArgumentValidationErrorEmptyStringNotAllowed ——
    脚本第一步就死；而 CI 用的是 shell: pwsh（7.x 有值），永远无感。真实事故：
    tools/Publish-PortableRuntime.ps1 曾在默认值里这么写（审计文档 §12.10）。唯一
    正确的写法是进函数体后再解析。注释行不算 —— 讲解这个坑的注释恰恰应该留在
    param 块里。
    """
    offenders = []
    for path in _source_ps1_files():
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for line in _param_block_code_lines(text):
            if "$PSScriptRoot" in line:
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, "param() 默认值里出现 $PSScriptRoot: " + "; ".join(offenders)


def test_verify_checksums_tool_accepts_good_and_rejects_bad(tmp_path):
    """发布校验工具必须能独立复算，且对错误哈希返回非 0。

    第五轮核验指出"报告手抄哈希"是用户校验失败的第一号误判源；这个工具让
    报告与用户都直接读 SHA256SUMS.txt。
    """
    import hashlib
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"release payload")
    sums = tmp_path / "SHA256SUMS.txt"
    tool = root / "tools" / "verify_checksums.py"

    sums.write_text(
        f"{hashlib.sha256(b'release payload').hexdigest()} *artifact.bin\n",
        encoding="ascii",
    )
    good = subprocess.run(
        [sys.executable, str(tool), str(tmp_path)], capture_output=True, text=True
    )
    assert good.returncode == 0, good.stderr

    sums.write_text(f"{'0' * 64} *artifact.bin\n", encoding="ascii")
    bad = subprocess.run(
        [sys.executable, str(tool), str(tmp_path)], capture_output=True, text=True
    )
    assert bad.returncode == 1
    assert "FAIL" in bad.stdout + bad.stderr
