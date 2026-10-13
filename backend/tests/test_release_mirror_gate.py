"""发布门禁（tools/check_release_mirror.py）的规则表回归测试。

门禁自身长期没有测试，是审计发现 P0 的直接原因：它的排除规则用后缀黑名单、
隐私扫描用后缀白名单，两个名单之间的缝隙让 data/config.json.corrupt 与
data/history.db-wal 既能被 --apply 复制进公开镜像、又不会被内容扫描发现。
这些测试把缝隙钉死，并加反向保护，避免"把该发布的也一起排除了"。
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import check_release_mirror as gate  # noqa: E402

# 本机标记必须用拼接写，否则本测试文件自己会被隐私扫描命中
_WORKDIR = "agent" + "_try"

RUNTIME_DERIVED = [
    "data/config.json.corrupt",
    "data/history.db-wal",
    "data/history.db-shm",
    "data/config.json",
    "data/history.db",
    "data/_batch.json",
    "data/logs/backend.log",
    "data/logs/instances/abc.log",
    "data/presets/mine.json",
    "data/backend_index.html",
]

INTERNAL_DOCS = [
    "LunarShroud-演进规划.md",
    "独立代码审计报告.md",
    "docs/archive/HANDOVER-2026-08-02-legacy.md",
    "docs/archive/plans/x.md",
]

# Windows 快捷方式：目标绝对路径以 UTF-16LE 存在二进制里，内容隐私扫描走
# UnicodeDecodeError 分支整体跳过，所以规则表是唯一拦截点。实际触发过一次：
# 在工作区根目录右键"创建快捷方式"落下一个 LunarShroud.exe - 快捷方式.lnk。
SHELL_ARTIFACTS = [
    "LunarShroud.exe - 快捷方式.lnk",
    "sub/dir/App.lnk",
]

MUST_STILL_MIRROR = [
    "backend/main.py",
    "backend/core/param_builder.py",
    "backend/tests/test_release_mirror_gate.py",
    "frontend/js/app.js",
    "frontend/index.html",
    "tools/check_release_mirror.py",
    "README.md",
    "CHANGELOG.md",
    ".gitignore",
    "src-tauri/src/lib.rs",
]


@pytest.mark.parametrize("rel", RUNTIME_DERIVED)
def test_runtime_data_never_enters_mirror(rel):
    excluded, reason = gate.is_excluded(rel)
    assert excluded, f"{rel} 必须被排除，否则 --apply 会把它复制进公开镜像"
    assert reason


@pytest.mark.parametrize("rel", RUNTIME_DERIVED)
def test_runtime_data_is_forbidden_inside_mirror(rel):
    assert gate.is_forbidden(rel), f"{rel} 出现在镜像里必须被判违规"


@pytest.mark.parametrize("rel", INTERNAL_DOCS)
def test_internal_docs_are_excluded_and_forbidden(rel):
    assert gate.is_excluded(rel)[0], rel
    assert gate.is_forbidden(rel), rel


@pytest.mark.parametrize("rel", SHELL_ARTIFACTS)
def test_shortcuts_are_excluded_and_forbidden(rel):
    assert gate.is_excluded(rel)[0], f"{rel} 会被 --apply 复制进公开镜像"
    assert gate.is_forbidden(rel), f"{rel} 出现在镜像里必须被判违规"


def test_shortcut_leak_is_caught_by_content_scan_and_by_rules(tmp_path):
    """UTF-16LE 路径现在**两层都拦得住**：内容扫描 + 规则表。

    这条原本是"记录已知盲区"的测试：privacy_hits 走 UnicodeDecodeError 分支整体
    跳过二进制，于是 .lnk 里以 UTF-16LE 存放的目标路径完全隐身，只能靠规则表
    枚举后缀兜底（真实事故：仓库根目录落下过 LunarShroud.exe - 快捷方式.lnk）。
    privacy_hits 已改为按 UTF-8 / UTF-16LE / UTF-16BE / latin-1 多视图解码，
    盲区关闭 —— 按原测试 docstring 的指示放宽第一条断言，规则表那两条保留为纵深。
    """
    target = "D:\\" + _WORKDIR + "\\x"
    (tmp_path / "App.lnk").write_bytes(target.encode("utf-16-le"))
    hits = gate.privacy_hits(tmp_path, ["App.lnk"])
    assert hits, "UTF-16LE 编码的本机路径必须被内容扫描命中"
    assert hits[0][1] == "workdir"
    # 规则表仍是第一道：内容扫描是兜底，不是唯一控制点
    assert gate.is_excluded("App.lnk")[0]
    assert gate.is_forbidden("App.lnk")


@pytest.mark.parametrize("rel", [
    ".zcodeignore", ".cursorignore", ".aiexclude", ".aiderignore", ".codeiumignore",
])
def test_ai_tool_config_files_are_excluded_and_forbidden(rel):
    """AI 编码工具自己生成的忽略/配置不得进公开镜像。

    .zcodeignore 曾实际被 --apply 复制进 github/ —— 它不是项目配置，内部还自证
    "以上为 ZCode 默认排除规则（从 .gitignore 同步）"。与 .claude/ .mimosa/ 同一类，
    都属于"本机工具状态"，公开它等于告诉外界这个项目是怎么写出来的。
    """
    assert gate.is_excluded(rel)[0], f"{rel} 会被 --apply 复制进公开镜像"
    assert gate.is_forbidden(rel), f"{rel} 出现在镜像里必须被判违规"


def test_the_project_gitignore_still_mirrors():
    """反向保护：排除 AI 工具忽略文件时，别把项目自己的 .gitignore 也排掉。"""
    assert not gate.is_excluded(".gitignore")[0]
    assert not gate.is_forbidden(".gitignore")
    assert not gate.is_excluded(".editorconfig")[0]
    assert not gate.is_excluded(".coveragerc")[0]


@pytest.mark.parametrize("rel", MUST_STILL_MIRROR)
def test_publishable_sources_are_not_over_excluded(rel):
    """反向保护：整目录排除 data/ 之后，别把该发布的源码也一起排掉。"""
    assert not gate.is_excluded(rel)[0], f"{rel} 应该进镜像"
    assert not gate.is_forbidden(rel), f"{rel} 不该被判违规"


def test_privacy_scan_reads_suffixes_outside_the_old_allowlist(tmp_path):
    """.corrupt 后缀必须被内容扫描 —— 这正是原先漏网的那一类。"""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.json.corrupt").write_text(
        '{"model_dir":"D:/' + _WORKDIR + '/models"}', encoding="utf-8"
    )
    hits = gate.privacy_hits(tmp_path, ["data/config.json.corrupt"])
    assert hits, "含本机路径的 .corrupt 文件必须被隐私扫描命中"
    assert hits[0][1] == "workdir"


def test_privacy_scan_reads_wal_sidecar(tmp_path):
    """WAL 边车文件是文本可解码时也必须扫；不可解码则由 UnicodeDecodeError 跳过。"""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "history.db-wal").write_text("path=D:/" + _WORKDIR, encoding="utf-8")
    hits = gate.privacy_hits(tmp_path, ["data/history.db-wal"])
    assert hits and hits[0][1] == "workdir"


def test_privacy_scan_reads_ascii_markers_inside_binaries(tmp_path):
    """二进制不再被整体跳过：里面的 ASCII 本机路径必须被抓到。

    原实现对不可 UTF-8 解码的文件直接 continue，于是 .pdb / .exe / .dll 里内嵌的
    绝对构建路径全部隐身（.pdb 尤其如此，它必然内嵌构建机路径）。latin-1 视图
    永不失败，所以任何文件都至少被扫一遍。没有标记的真二进制仍然安静通过。
    """
    clean = b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01binary"
    (tmp_path / "logo.png").write_bytes(clean)
    assert gate.privacy_hits(tmp_path, ["logo.png"]) == []

    dirty = clean + ("D:/" + _WORKDIR + "/models").encode("ascii")
    (tmp_path / "with_path.pdb").write_bytes(dirty)
    hits = gate.privacy_hits(tmp_path, ["with_path.pdb"])
    assert hits, "二进制里的 ASCII 本机路径必须被命中"
    assert hits[0][1] == "workdir"


def test_apply_never_publishes_runtime_data(tmp_path):
    """端到端：--apply 之后镜像里只许留下源码，且门禁必须报 0 问题。"""
    root, mirror = tmp_path / "root", tmp_path / "mirror"
    (root / "data").mkdir(parents=True)
    (root / "backend").mkdir(parents=True)
    mirror.mkdir()
    (root / "backend" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (root / "data" / "config.json.corrupt").write_text(
        '{"model_dir":"D:/' + _WORKDIR + '"}', encoding="utf-8"
    )
    (root / "data" / "history.db-wal").write_bytes(b"\x00\x01wal")
    (root / "LunarShroud-演进规划.md").write_text("内部记录 " + _WORKDIR, encoding="utf-8")
    # 真实触发过的场景：根目录落下一个指向本机绝对路径的快捷方式
    (root / "LunarShroud.exe - 快捷方式.lnk").write_bytes(
        ("D:\\" + _WORKDIR + "\\LunarShroud.exe").encode("utf-16-le")
    )

    gate.apply(root, mirror, stream=io.StringIO())

    published = {p.relative_to(mirror).as_posix() for p in mirror.rglob("*") if p.is_file()}
    assert published == {"backend/main.py"}, published
    assert gate._problem_count(gate.inspect(root, mirror)) == 0
