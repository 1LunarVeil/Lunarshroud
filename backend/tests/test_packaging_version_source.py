"""打包脚本不得持有第二份版本号。

`test_regressions.py::test_release_version_is_consistent_across_manifests`
已经钉住了"tauri.conf.json / Cargo.toml / Cargo.lock / CHANGELOG / README
五处一致"。这里补的是它没有、而且性质不同的一条：

**版本号只能有机器可读的一处来源，打包脚本必须读它，不能自己写一份。**

为什么这不是重复：`tools/package_portable.py` 曾经硬编码
`VERSION = "0.9.0-beta.2"`。它不参与上面那条 manifest 比对，所以"改了
tauri.conf.json 却忘了改打包脚本"**不会让任何测试变红** —— 只会产出一个
文件名说 1.0.0、里面装 0.9 的 zip。这个缺陷只有在用户解压并看到版本不符时
才暴露，而那时包已经发出去了。

它现在改为从 `tauri.conf.json` 读取，并交叉校验 `Cargo.toml`；一旦再次出现
字面量赋值，这条断言就红。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGER = REPO_ROOT / "tools" / "package_portable.py"


def test_packager_reads_the_version_instead_of_hardcoding_it():
    source = PACKAGER.read_text(encoding="utf-8")
    assert "read_version()" in source, (
        "packager must read the version from src-tauri/tauri.conf.json"
    )
    hardcoded = re.search(r'(?m)^\s*VERSION\s*=\s*["\']', source)
    assert hardcoded is None, (
        "packager hardcodes a version literal again; a bumped tauri.conf.json "
        "would then produce a zip whose name contradicts its contents"
    )


def test_packager_cross_checks_cargo_toml():
    """包名来自 tauri.conf.json，而二进制版本来自 Cargo.toml。

    两者不一致时产出的分发包自相矛盾，所以打包必须**在写出 zip 之前**拒绝，
    而不是打出来以后再靠人发现。
    """
    source = PACKAGER.read_text(encoding="utf-8")
    assert "read_cargo_version()" in source
    assert "version mismatch" in source, "packager must refuse a mismatched version"


def test_packager_refuses_a_stale_launcher_binary():
    """清单一致 ≠ 内容一致：还必须比对 exe 自己内嵌的版本。

    真实踩坑：版本升到 1.0.0、后端重建、五个清单全绿，但仓库根的
    `LunarShroud.exe` 仍是上一版构建产物。打出来的包叫 `1.0.0-portable`，
    里面的壳报 `0.9.0.0`；安装完看到的版本与说明不符，而所有自动化检查都是
    绿的。这条检查就是为那次事故钉的。

    它必须 fail-closed：版本读不到时**不**静默放行成功路径，读得到但不一致
    时必须返回非 0 退出码。
    """
    source = PACKAGER.read_text(encoding="utf-8")
    assert "read_launcher_file_version" in source, (
        "packager must read the launcher's embedded FileVersion"
    )
    assert "stale launcher" in source, "packager must refuse a stale launcher binary"
    # 拒绝时必须用非 0 返回，而不是只打印一行警告继续打包。
    assert re.search(r"return\s+4", source), (
        "stale launcher must abort packaging with a non-zero exit"
    )


def test_file_version_comparison_tolerates_the_four_part_form():
    """VERSIONINFO 的 FileVersion 结构上是四段，所以 `1.0.0` 写成 `1.0.0.0`。

    用 `!=` 直接比会把**每一个合法构建**判成过期（实测就踩了：刚重建出 1.0.0
    的壳，打包器报 "reports 1.0.0.0 but this release is 1.0.0" 并拒绝出包）。
    但也不能放宽成前缀匹配，否则 `1.0` 会放过 `1.0.0.1` 这种真·旧版本。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("packager", PACKAGER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # 等价：只是第四段补零
    assert module.versions_equivalent("1.0.0.0", "1.0.0")
    assert module.versions_equivalent("1.0.0", "1.0.0.0")
    assert module.versions_equivalent("1.2.3.4", "1.2.3.4")
    # 不等价：真实差异必须被拒绝
    assert not module.versions_equivalent("0.9.0.0", "1.0.0")
    assert not module.versions_equivalent("1.0.0.1", "1.0.0")
    assert not module.versions_equivalent("1.0", "1.0.0.1")
    assert not module.versions_equivalent("1.0.0", "1.0.1")


def test_release_build_passes_brepro_so_the_launcher_is_reproducible():
    """MSVC 链接器必须收到 `/Brepro`，否则 launcher 每次构建哈希都不同。

    实测：只设 `SOURCE_DATE_EPOCH` 时，PE 的 COFF TimeDateStamp 仍是墙钟时间
    （两次构建分别 1793601298 / 实测漂移），加上 `-C link-arg=/Brepro` 后固定
    为内容派生值，两次构建得到同一个 SHA-256（7eae1d96…）。

    文档曾声称"两次构建哈希一致"，但那是在没有第二次实测的情况下写下的。
    这条断言把修复钉在**发布工作流**上：改了 RUSTFLAGS 却漏掉 /Brepro，发布
    路径就会重新变成不可重复，而构建本身依然全绿。
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert "link-arg=/Brepro" in workflow, (
        "release workflow must pass /Brepro or the launcher is not byte-reproducible"
    )
    # 身份重映射也不能丢（否则用户名会进二进制）。
    assert "remap-path-prefix" in workflow


def test_packager_copies_only_the_runtime_not_the_whole_source_tree():
    """便携包的 `backend/` 只能是 backend.exe + _internal/。

    实际事故（2026-11-02 对发布包逐条清点）：它此前用
    `shutil.copytree(backend.parent, ...)`，把整个源码目录搬进了分发包：

      * `tests/`（102 个文件）—— 含硬编码测试令牌，被安全复查直接报出
      * `data/`  —— **开发机的真实用户数据**：chats.db、history.db、
                    config.json、instance 日志、7 个本机预设
      * `.mimosa/`（41 个文件）—— AI 编码工具的内部状态与源码基线快照
      * `dist/`（769）、`build/`（16）、`__pycache__/`、backend-child.log

    包体积 136.7 MiB，其中绝大部分是这些不该发布的东西；清理后 38.8 MiB。
    把开发机的聊天记录随公开包发出去，是比"多了几个无关文件"严重得多的问题。
    """
    source = PACKAGER.read_text(encoding="utf-8")
    assert "backend_dest" in source, "packager must copy the runtime explicitly"

    # 只在**可执行代码**里找那个调用，不在注释里找：上面的说明文字为了讲清
    # 事故原因必须引用原来的写法，用裸字符串匹配会把注释当成违规（第一版
    # 测试就是这么误报的）。剥掉注释行再判断。
    code_lines = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code_only = "\n".join(code_lines)
    assert "copytree(backend.parent" not in code_only, (
        "packager must not copy the whole backend/ source tree into the package"
    )


def test_packager_inspects_the_finished_zip_and_refuses_leaks():
    """必须在**从 zip 读回**的条目上把关，而不是相信 staging 的构造过程。

    构造逻辑将来被改动时，只有"读回真实产物"的检查还能拦住泄漏；而且它
    必须 fail-closed（删掉 zip 并非 0 退出），否则会带着数据出包。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("packager_inspect", PACKAGER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.inspect_zip_contents)
    source = PACKAGER.read_text(encoding="utf-8")
    assert "refusing to publish" in source
    assert re.search(r"return\s+5", source), "leaking zip must abort with non-zero exit"

    # 用真实 zip 的形状构造两个最小样例，验证判定逻辑本身。
    import tempfile
    import zipfile

    def build(entries: dict[str, bytes]) -> Path:
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        handle.close()
        path = Path(handle.name)
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in entries.items():
                archive.writestr(name, payload)
        return path

    good = build(
        {
            "pkg/LunarShroud.exe": b"x",
            "pkg/backend/backend.exe": b"x",
            "pkg/backend/_internal/python311.dll": b"x",
            "pkg/使用说明.txt": b"x",
        }
    )
    try:
        assert module.inspect_zip_contents(good, "pkg") == []
    finally:
        good.unlink()

    for bad_entry in (
        "pkg/backend/tests/test_auth_coverage.py",
        "pkg/backend/data/chats.db",
        "pkg/backend/.mimosa/hook-state/x.json",
        "pkg/backend/main.py",
        "pkg/backend/build/x.txt",
        "pkg/backend/backend-child.log",
    ):
        dirty = build({bad_entry: b"x"})
        try:
            assert module.inspect_zip_contents(dirty, "pkg"), (
                f"inspector must reject {bad_entry}"
            )
        finally:
            dirty.unlink()

