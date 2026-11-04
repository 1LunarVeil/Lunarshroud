from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

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
            "-SkipSigning",
            # 目标是临时目录里的假运行时；见 _run_deployer 的说明。
            "-AllowRunning",
        ],
        capture_output=True,
        text=True, errors="replace",
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
            "-SkipSigning",
            # 目标是临时目录里的假运行时；见 _run_deployer 的说明。
            "-AllowRunning",
        ],
        capture_output=True,
        text=True, errors="replace",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert (destination / "backend" / "backend.exe").read_bytes() == backend_source.read_bytes()
    assert (destination / "backend" / "_internal" / "python311.dll").read_bytes() == b"python runtime"
    assert (destination / "backend" / "_internal" / "sub" / "base_library.zip").read_bytes() == b"stdlib"


def _run_deployer(deployer, launcher_source, backend_source, destination):
    # -AllowRunning：这些用例的目标是**临时目录里的一个假运行时**，与用户机器上
    # 真正在跑的应用毫无关系；但部署器的"别覆盖正在运行的安装"守卫是按**进程名**
    # 全局检查的（Get-Process LunarShroud, backend），所以用户只要开着应用，
    # 这 5 条用例就全红。2026-10-28 实测：安装版跑着一个实例（含 6.8 GB 的
    # llama-server）时套件报 5 failed，而代码没有任何改动。
    # 守卫本身是对的（真实部署确实不能覆盖正在使用的文件），所以这里不放松
    # 部署器，只让测试显式声明"我知道有进程在跑，但我的目标是临时目录"。
    return subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(deployer),
            "-LauncherSource", str(launcher_source),
            "-BackendSource", str(backend_source),
            "-Destination", str(destination),
            "-SkipDesktopShortcut",
            "-SkipSigning",
            "-AllowRunning",
        ],
        capture_output=True,
        text=True, errors="replace",
        encoding="utf-8",
        timeout=120,
    )


def _make_new_release(tmp_path):
    root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "dist" / "backend"
    (runtime / "_internal" / "sub").mkdir(parents=True)
    launcher_source = tmp_path / "release-launcher.exe"
    launcher_source.write_bytes(b"NEW launcher")
    backend_source = runtime / "backend.exe"
    backend_source.write_bytes(b"NEW onedir backend")
    (runtime / "_internal" / "python311.dll").write_bytes(b"NEW python runtime")
    (runtime / "_internal" / "sub" / "base_library.zip").write_bytes(b"NEW stdlib")
    return root / "tools" / "Publish-PortableRuntime.ps1", launcher_source, backend_source


def _make_installed_pair(destination):
    (destination / "backend" / "_internal" / "sub").mkdir(parents=True)
    (destination / "LunarShroud.exe").write_bytes(b"OLD launcher")
    (destination / "backend" / "backend.exe").write_bytes(b"OLD onedir backend")
    (destination / "backend" / "_internal" / "python311.dll").write_bytes(b"OLD runtime")
    (destination / "backend" / "_internal" / "sub" / "base_library.zip").write_bytes(
        b"OLD stdlib"
    )


def test_deployer_recovers_from_a_stale_next_leftover(tmp_path):
    """上一次崩溃留下的 ``.next`` 残留不能把后续每次部署都拖垮。

    ``Copy-Item -Destination <已存在的目录>`` 会把文件**复制进目录里**，于是
    ``.next`` 自己还是那个目录，紧随其后的暂存哈希校验去 ``File.OpenRead`` 一个
    目录就抛异常。旧实现在换入前失败时又无条件删除三个目的地，而那时旧的三段还
    原地未动、``.rollback`` 一个都不存在 —— 实测部署后目录里**只剩 .next**，
    旧安装整个没了。
    """
    deployer, launcher_source, backend_source = _make_new_release(tmp_path)
    destination = tmp_path / "portable"
    _make_installed_pair(destination)
    # 复现崩溃残留：.next 是个目录，不是文件
    (destination / "LunarShroud.exe.next").mkdir()

    result = _run_deployer(deployer, launcher_source, backend_source, destination)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (destination / "LunarShroud.exe").read_bytes() == b"NEW launcher"
    assert (destination / "backend" / "backend.exe").read_bytes() == b"NEW onedir backend"
    assert (
        destination / "backend" / "_internal" / "python311.dll"
    ).read_bytes() == b"NEW python runtime"
    leftovers = sorted(p.name for p in destination.rglob("*") if ".next" in p.name)
    assert leftovers == [], f"暂存件没被清理: {leftovers}"


def test_deployer_keeps_the_old_install_when_a_slot_cannot_be_moved(tmp_path):
    """换入只能挪到一半时，没被动过的那几格必须原样留着。

    真实触发方式是"用户还开着应用"：正在运行的 exe 无法改名，Windows 直接拒绝。
    三段是逐个 ``Move-Item`` 的，launcher 已经进了 ``.rollback``、backend 移动
    失败、``_internal`` 还没轮到 —— 此时旧的 ``_internal`` 仍好好地待在目的地。
    旧实现的 catch 无条件删掉三个目的地再按 ``.rollback`` 是否存在来恢复，
    于是那份**从没被动过**的旧运行时被删了，只剩下旧 launcher 被搬回来：
    一个"新 launcher + 没有 _internal"的不可启动混合对，比部署失败更糟。
    """
    import subprocess
    import time

    deployer, launcher_source, backend_source = _make_new_release(tmp_path)
    destination = tmp_path / "portable"
    _make_installed_pair(destination)
    locked = destination / "backend" / "backend.exe"

    # 真实触发方式是"用户还开着应用"：文件被以 FileShare.None 打开时，
    # Move-Item 直接 IOException（实测确认，比"跑一个复制出来的 exe"可靠 ——
    # 后者并不总能挡住改名）。
    holder = subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "$s = [System.IO.File]::Open("
            f"'{str(locked)}', 'Open', 'Read', 'None'); "
            "Start-Sleep -Seconds 60",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3.0)  # 等句柄真的建立起来，否则测的就是一次正常部署
    try:
        result = _run_deployer(deployer, launcher_source, backend_source, destination)
    finally:
        holder.terminate()
        try:
            holder.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - 清理兜底
            holder.kill()
            holder.wait(timeout=20)

    assert result.returncode != 0, "旧 backend.exe 被占用，部署不可能成功"

    # 旧的三段都还在，且字节没变
    assert (destination / "LunarShroud.exe").read_bytes() == b"OLD launcher", (
        "换入失败后旧 launcher 没能恢复"
    )
    assert locked.exists(), "从没被动过的旧 backend.exe 被删了"
    internal = destination / "backend" / "_internal"
    assert (
        internal / "python311.dll"
    ).read_bytes() == b"OLD runtime", "从没被动过的旧 _internal 被删了"
    assert (internal / "sub" / "base_library.zip").read_bytes() == b"OLD stdlib"
    # 失败路径上不许有换入一半的新文件
    assert (destination / "LunarShroud.exe").read_bytes() != b"NEW launcher"
    stale = sorted(
        str(p.relative_to(destination))
        for p in destination.rglob("*")
        if ".next" in p.name or ".rollback" in p.name
    )
    assert stale == [], f"失败后残留了暂存/回滚件: {stale}"


def test_verify_checksums_generates_and_flags_unlisted_artifacts(tmp_path):
    """校验和必须能"按当前产物重新生成"，且未被清单覆盖的产物要让门禁变红。

    实测的失效形态：``SHA256SUMS.txt`` 写于 10-18 21:20，而
    beta.2 的 setup.exe 是 10-20 19:46 重建的 —— 清单是**最后一次构建之前**写的，
    之后就再没人管。手抄/手改校验和的流程必然出现这种漂移。

    另一半是"目录里躺着上一个版本"：清单只校验列出来的条目，所以旧的
    ``0.9-setup.exe`` 不在清单里也**不会**报错 —— 核对全绿，发布时上传错文件。
    """
    import hashlib
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    tool = root / "tools" / "verify_checksums.py"
    (tmp_path / "app_1.0-setup.exe").write_bytes(b"current build")
    (tmp_path / "app_0.9-setup.exe").write_bytes(b"previous build")

    generated = subprocess.run(
        [
            sys.executable, str(tool), "--generate", str(tmp_path),
            "--artifact", "app_1.0-setup.exe",
        ],
        capture_output=True, text=True, errors="replace", timeout=60,
    )
    assert generated.returncode == 0, generated.stderr
    sums = (tmp_path / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert hashlib.sha256(b"current build").hexdigest() in sums
    assert "app_0.9" not in sums

    # 只登记当前版本 = 旧产物成了"没人认领的发布物"，必须失败
    stale = subprocess.run(
        [sys.executable, str(tool), str(tmp_path)], capture_output=True, text=True, errors="replace", timeout=60
    )
    assert stale.returncode == 1
    assert "app_0.9-setup.exe" in stale.stdout + stale.stderr

    # 构建之后不改清单，清单就永远描述的是上一次构建
    (tmp_path / "app_1.0-setup.exe").write_bytes(b"rebuilt after sums")
    drifted = subprocess.run(
        [sys.executable, str(tool), str(tmp_path)], capture_output=True, text=True, errors="replace", timeout=60
    )
    assert drifted.returncode == 1
    assert "expected" in drifted.stdout + drifted.stderr

    # 重新生成后恢复一致
    subprocess.run(
        [sys.executable, str(tool), "--generate", str(tmp_path)],
        capture_output=True, text=True, errors="replace", timeout=60, check=True,
    )
    fixed = subprocess.run(
        [sys.executable, str(tool), str(tmp_path)], capture_output=True, text=True, errors="replace", timeout=60
    )
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr


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
            "-SkipSigning",
            # 目标是临时目录里的假运行时；见 _run_deployer 的说明。
            "-AllowRunning",
        ],
        capture_output=True,
        text=True, errors="replace",
        timeout=120,
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
        f"-BackendSource '{backend_source}' -Destination 'portable' "
        f"-SkipDesktopShortcut -SkipSigning -AllowRunning"
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
        text=True, errors="replace",
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
        text=True, errors="replace",
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
    tools/Publish-PortableRuntime.ps1 曾在默认值里这么写。唯一
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
        [sys.executable, str(tool), str(tmp_path)], capture_output=True, text=True, errors="replace"
    )
    assert good.returncode == 0, good.stderr

    sums.write_text(f"{'0' * 64} *artifact.bin\n", encoding="ascii")
    bad = subprocess.run(
        [sys.executable, str(tool), str(tmp_path)], capture_output=True, text=True, errors="replace"
    )
    assert bad.returncode == 1
    assert "FAIL" in bad.stdout + bad.stderr


def _local_signing_cert_thumbprint():
    """本机是否有部署脚本会用的那张自签证书；没有则返回空串。"""
    import subprocess

    script = (
        "Get-ChildItem Cert:\\CurrentUser\\My -CodeSigningCert -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Subject -like '*CN=LunarShroud Local Build*' -and $_.HasPrivateKey } | "
        "Select-Object -First 1 -ExpandProperty Thumbprint"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True, errors="replace",
        timeout=60,
    )
    return (result.stdout or "").strip()


def test_deployer_fails_loudly_when_signing_cannot_succeed(tmp_path):
    """要签名却签不了，必须中止部署，而不是悄悄部署一个未签名产物。

    这条盯的是"静默降级"：如果 Set-AuthenticodeSignature 失败被吞掉，用户会以为
    产物已签名，结果第一次双击仍然弹 SmartScreen——而且没有任何地方提示过。

    只在装过本机签名证书的机器上跑：没有证书时脚本的预期行为就是跳过签名，
    那是另一条路径（由所有 -SkipSigning 用例与无证书环境自然覆盖）。
    """
    if not _local_signing_cert_thumbprint():
        pytest.skip("no local signing certificate on this machine")

    root = Path(__file__).resolve().parents[2]
    deployer = root / "tools" / "Publish-PortableRuntime.ps1"
    # 12 字节的假 exe 不是合法 PE，签名必然失败
    launcher_source = tmp_path / "fake-launcher.exe"
    runtime = tmp_path / "dist" / "backend"
    backend_source = runtime / "backend.exe"
    internal = runtime / "_internal"
    launcher_source.write_bytes(b"not a PE")
    backend_source.parent.mkdir(parents=True)
    backend_source.write_bytes(b"backend")
    internal.mkdir()
    (internal / "x.dll").write_bytes(b"dll")
    destination = tmp_path / "portable"

    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(deployer),
            "-LauncherSource", str(launcher_source),
            "-BackendSource", str(backend_source),
            "-Destination", str(destination),
            "-SkipDesktopShortcut",
            # 刻意不传 -SkipSigning：本用例要验证"签名失败必须中止部署"。
            # 但必须传 -AllowRunning：部署器的"别覆盖正在运行的安装"守卫按进程名
            # 全局检查，用户开着应用时会在签名之前就 throw，于是本用例测到的是
            # 进程守卫而不是签名路径（2026-10-28 实测 5 条部署器用例因此全红）。
            "-AllowRunning",
        ],
        capture_output=True,
        text=True, errors="replace",
        encoding="utf-8",
        timeout=120,
    )

    assert result.returncode != 0, (
        "签名失败却仍然报告部署成功——用户会以为产物已签名:\n" + result.stdout[-800:]
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert "sign" in combined.lower(), combined[-800:]
    # 失败时不能留下半成品的新 launcher
    assert not (destination / "LunarShroud.exe").exists()


def test_user_data_backup_and_restore_round_trip_with_non_ascii_names(tmp_path):
    """Chinese preset filenames must survive the PowerShell backup/restore round trip.

    The old manifest was written with ``Set-Content -Encoding ASCII`` and read
    back with the default encoding, so a filename such as ``测试.json`` became
    ``??.json`` after copying and the verification step aborted.
    """
    root = Path(__file__).resolve().parents[2]
    backup_script = root / "tools" / "Backup-UserData.ps1"
    restore_script = root / "tools" / "Restore-UserData.ps1"
    data = tmp_path / "LunarShroud"
    backup = tmp_path / "backup"
    (data / "presets").mkdir(parents=True)
    original_config = '{"launcher_port":17520,"theme":"dark"}'
    (data / "config.json").write_text(original_config, encoding="utf-8")
    preset_name = "测试.json"
    (data / "presets" / preset_name).write_text('{"name":"测试"}', encoding="utf-8")

    result = _run_ps1(
        backup_script,
        "-DataDirectory", str(data),
        "-Destination", str(backup),
        "-Force",
    )
    assert result.returncode == 0, result.stderr
    assert (backup / "presets" / preset_name).is_file()

    (data / "config.json").write_text('{"launcher_port":9999}', encoding="utf-8")
    (data / "presets" / preset_name).unlink()

    result = _run_ps1(
        restore_script,
        "-BackupDirectory", str(backup),
        "-DataDirectory", str(data),
        "-Force",
    )
    assert result.returncode == 0, result.stderr
    assert (data / "config.json").read_text(encoding="utf-8") == original_config
    assert (data / "presets" / preset_name).read_text(encoding="utf-8") == '{"name":"测试"}'


def test_user_data_backup_refuses_while_backend_process_is_running(tmp_path):
    """A surviving backend.exe keeps SQLite/WAL open; raw copy is not safe then."""
    import shutil
    import subprocess
    import time

    root = Path(__file__).resolve().parents[2]
    backup_script = root / "tools" / "Backup-UserData.ps1"
    data = tmp_path / "LunarShroud"
    data.mkdir()
    (data / "config.json").write_text('{"launcher_port":17520}', encoding="utf-8")
    fake_backend = tmp_path / "backend.exe"
    shutil.copy2(Path(r"C:\Windows\System32\ping.exe"), fake_backend)

    process = subprocess.Popen(
        [str(fake_backend), "-n", "30", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.3)
        result = _run_ps1(
            backup_script,
            "-DataDirectory", str(data),
            "-Destination", str(tmp_path / "backup"),
        )
        assert result.returncode != 0
        assert "backend" in (result.stdout + result.stderr).lower()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def test_cli_restore_reads_an_in_app_utf8_backup_without_bom(tmp_path):
    """The UI writes UTF-8 without BOM; the PowerShell restore path must read it."""
    from core import user_backup

    root = Path(__file__).resolve().parents[2]
    restore_script = root / "tools" / "Restore-UserData.ps1"
    data = tmp_path / "source-data"
    (data / "presets").mkdir(parents=True)
    (data / "config.json").write_text('{"launcher_port":17520}', encoding="utf-8")
    (data / "presets" / "中文预设.json").write_text('{"name":"中文"}', encoding="utf-8")
    user_backup.create_backup(name="ui-backup", data_dir=data)
    backup = data / "backups" / "ui-backup"
    target = tmp_path / "restored"

    result = _run_ps1(
        restore_script,
        "-BackupDirectory", str(backup),
        "-DataDirectory", str(target),
        "-Force",
    )
    assert result.returncode == 0, result.stderr
    assert (target / "presets" / "中文预设.json").is_file()


def test_deployer_refuses_while_a_backend_process_is_running(tmp_path):
    """Replacing _internal under a live backend can mix versions; fail closed.

    **本用例不能传 -AllowRunning**：它验证的正是那个守卫。它自己起一个名为
    ``backend.exe`` 的假进程触发拦截，因此也依赖"机器上没有别的真实实例"。
    若用户正开着 LunarShroud，本用例仍应通过（守卫照样拒绝），但副作用是
    目标目录的 OLD launcher 断言不受影响 —— 因为拒绝发生在任何写入之前。
    """
    import shutil
    import subprocess
    import time

    deployer, launcher_source, backend_source = _make_new_release(tmp_path)
    destination = tmp_path / "portable"
    _make_installed_pair(destination)
    fake_backend = tmp_path / "backend.exe"
    shutil.copy2(Path(r"C:\Windows\System32\ping.exe"), fake_backend)
    process = subprocess.Popen(
        [str(fake_backend), "-n", "30", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.3)
        # 刻意用不带 -AllowRunning 的调用方式（不能复用 _run_deployer，后者带该开关）。
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(deployer),
                "-LauncherSource", str(launcher_source),
                "-BackendSource", str(backend_source),
                "-Destination", str(destination),
                "-SkipDesktopShortcut",
                "-SkipSigning",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            encoding="utf-8",
            timeout=120,
        )
        assert result.returncode != 0
        assert "backend" in (result.stdout + result.stderr).lower()
        assert (destination / "LunarShroud.exe").read_bytes() == b"OLD launcher"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
