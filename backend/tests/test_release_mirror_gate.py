"""发布门禁（tools/check_release_mirror.py）的规则表回归测试。

门禁自身长期没有测试，是审计发现 P0 的直接原因：它的排除规则用后缀黑名单、
隐私扫描用后缀白名单，两个名单之间的缝隙让 data/config.json.corrupt 与
data/history.db-wal 既能被 --apply 复制进公开镜像、又不会被内容扫描发现。
这些测试把缝隙钉死，并加反向保护，避免"把该发布的也一起排除了"。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import check_release_mirror as gate  # noqa: E402

# 本机标记不写死：门禁从环境变量推导，测试用中性 fixture 标记注入
_WORKDIR = "fixture" + "_workspace"


@pytest.fixture(autouse=True)
def _privacy_marker(monkeypatch):
    """把"本机工作区标记"注入门禁；公开源码里不出现真实工作区名。"""
    monkeypatch.setenv("LUNARSHROUD_PRIVACY_WORKDIR", _WORKDIR)

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

# 便携版从源码树根启动时，启动器会在 exe 同目录创建这些运行时目录；
# 外部 AI 工具还会在根目录留下会话导出。它们都含本机路径/缓存，必须在
# 规则表层面同时"不参与镜像"且"出现在镜像里就算违规"。
APP_RUNTIME_ARTIFACTS = [
    "webview2-data/EBWebView/Default/Cache/Cache_Data/data_0",
    "logs/state.json",
    "runtime-temp/leftover.txt",
    "appdata/LunarShroud/config.json",
    "dsh-session-session-abc123/media/shot.png",
    "CLAUDE.md",
    # 嵌套形式：规则必须按目录名/前缀在任意层级生效，而不是只认仓库根。
    "backend/dsh-session-session-abc123/session.v4.jsonl",
    "backend/webview2-data/EBWebView/Cache/data_0",
    "sub/CLAUDE.md",
    # 外部工具曾在 backend/ 根掉落 8 字符 tempfile 风格残留（jy4s_b4h）。
    "backend/jy4s_b4h",
    # AI 编码工具（ZCode）在仓库根生成的状态目录。2026-10-28 实测它曾被
    # `--apply` 复制进 github/：EXCLUDED_DIR_NAMES 与 FORBIDDEN_GLOBS 只列了
    # .claude/.mimosa 而没有 .zcode，于是公开镜像里出现了一份"本项目用什么
    # 工具开发"的自述配置。规则必须与 .gitignore 同口径覆盖。
    ".zcode/config.json",
    "sub/.zcode/config.json",
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


@pytest.mark.parametrize("rel", APP_RUNTIME_ARTIFACTS)
def test_app_runtime_artifacts_are_excluded_and_forbidden(rel):
    """从源码树根运行便携版/导入 AI 会话不得把门禁弄红。

    实测：部署对在仓库根启动一次后，`webview2-data/`（287 个文件）、
    `dsh-session-*`（11 个）和 `CLAUDE.md` 让门禁报 299 项缺失 —— 而这些
    文件都不该发布。规则表必须同时覆盖"工作树侧不参与镜像"与"镜像侧禁止出现"。
    """
    excluded, reason = gate.is_excluded(rel)
    assert excluded, f"{rel} 会被 --apply 复制进公开镜像"
    assert reason
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


def test_every_workflow_action_is_pinned_to_a_full_commit_sha():
    """CI 里每个 action 都必须固定到完整 40 位 commit SHA。

    这条约定一直写在 workflow 的注释风格里（`# v4.2.2` 之类），但**没有任何东西
    守着** —— 结果 ci.yml 的 setup-node 曾固定在一个 39 位的截断 SHA 上：GitHub
    解析不了，test job 从那一步起整个失败，而后面的前端测试、Rust、镜像门禁全部
    跟着跑不到。约定不被强制，就等于没有约定。
    """
    import re

    root = Path(__file__).resolve().parents[2]
    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))
    assert workflows, "找不到 workflow 文件，本测试会假绿"

    bad = []
    checked = 0
    for wf in workflows:
        text = wf.read_text(encoding="utf-8")
        for match in re.finditer(r"uses:\s*(\S+)", text):
            ref = match.group(1)
            name, _, pin = ref.partition("@")
            assert pin, f"{wf.name}: {ref} 没有固定版本"
            checked += 1
            if not re.fullmatch(r"[0-9a-f]{40}", pin):
                bad.append(f"{wf.name}: {name}@{pin} (长度 {len(pin)})")
    assert checked >= 6, f"只解析到 {checked} 个 uses，正则可能已失效"
    assert not bad, ("以下 action 没有固定到完整 40 位 SHA：" + chr(10) + chr(10).join(bad))


def test_release_workflow_publishes_the_portable_zip_and_no_installer():
    """release.yml 必须把便携包发到可匿名下载的 URL，且**不得**再产安装包。

    只 upload-artifact 不等于发布：workflow artifact 会过期、必须登录 GitHub 并
    点进具体某次运行才能下载、仓库首页没有入口，所以必须落到 GitHub Release。

    安装包通道已于 2026-11-05 移除（AGENTS §4）：它面向的人群与便携包完全相同，
    却要额外维护签名、卸载钩子和沙箱目录权限三条链路，而安装/卸载往返验收需要
    干净机器、长期做不到。因此这条测试是**双向**的 —— 既要钉住便携包发布，
    也要钉住"不许再出现安装包"，否则那次移除会在某次重构里被悄悄还原。
    """
    root = Path(__file__).resolve().parents[2]
    text = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "gh release create" in text, "没有任何 GitHub Release 发布步骤"
    # 必须只在打 tag 时发布，workflow_dispatch 试跑不得误发公开版本
    assert "startsWith(github.ref, 'refs/tags/v')" in text
    # 构建 job 的最小权限不该被写权限稀释
    assert "contents: read" in text
    build_job = text.split("  publish-release:")[0]
    assert "contents: write" not in build_job, "写权限泄漏到了构建 job"

    # 便携包是唯一分发物
    assert "*-portable-x64.zip" in text, "发布物里没有便携包"
    # 构建命令必须显式 --no-bundle，让"不产安装包"成为命令的一部分
    assert "--no-bundle" in text, "tauri build 没有禁用 bundle"

    # 安装包通道不得复活：既不能有产出它的步骤，也不能有认领它的校验
    assert "Build NSIS installer" not in text, "安装包构建步骤被加回来了"
    assert "bundle/nsis" not in text, "发布流程又把 NSIS 目录当成产物来源了"
    assert "installerHooks" not in text

    # 配置层面也必须关掉 bundle：只改 workflow 而留 targets: ["nsis"]，
    # 手工 `cargo tauri build` 仍会产出安装包。
    conf = json.loads((root / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    assert conf["bundle"]["active"] is False, "tauri.conf.json 又打开了 bundle"
    assert "targets" not in conf["bundle"], "bundle.targets 被加回来了（安装包通道已移除）"
    assert not (root / "src-tauri" / "nsis").exists(), "nsis/ 目录（安装脚本）被加回来了"


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


def test_apply_never_deletes_mirror_git_metadata(tmp_path):
    """`--apply` 的空目录清理不得进入 `.git/`。

    实测事故：镜像仓库 `git gc`/pack-refs 之后 `.git/refs/heads` 是空目录，
    `--apply` 的清理把它删了，下一次 git 命令直接报 "not a git repository"。
    `.git` 是仓库元数据，不属于待发布内容，必须整棵跳过。
    """
    root = tmp_path / "work"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")

    mirror = tmp_path / "mirror"
    for relative in (".git/refs/heads", ".git/refs/tags", ".git/objects/info"):
        (mirror / relative).mkdir(parents=True)
    (mirror / "stale.txt").write_text("stale", encoding="utf-8")

    gate.apply(root, mirror, stream=io.StringIO())

    assert (mirror / ".git" / "refs" / "heads").is_dir(), "refs 目录被空目录清理删除了"
    assert (mirror / ".git" / "refs" / "tags").is_dir()
    assert (mirror / ".git" / "objects" / "info").is_dir()
    assert (mirror / "a.txt").is_file()
    assert not (mirror / "stale.txt").exists()


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

def test_missing_mirror_fails_closed(tmp_path, capsys):
    """镜像目录丢失/路径写错时不能返回 0（CLAUDE.md 明确要求退出错误）。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "AGENTS.md").write_text("x", encoding="utf-8")
    missing = tmp_path / "no-such-mirror"

    code = gate.main(["--root", str(root), "--mirror", str(missing)])
    captured = capsys.readouterr()
    assert code == 2, "缺镜像时必须 fail closed"
    assert "未找到发布镜像目录" in captured.err

    # 显式 opt-in 才允许跳过（公开镜像自身运行时的语义）。
    assert gate.main(
        ["--root", str(root), "--mirror", str(missing), "--allow-missing-mirror"]
    ) == 0


def test_apply_aborts_before_copying_private_content(tmp_path):
    """--apply 是发布动作：发现本机痕迹时必须先中止，不能先复制再报错。"""
    root, mirror = tmp_path / "root", tmp_path / "mirror"
    (root / "backend").mkdir(parents=True)
    mirror.mkdir()
    (root / "backend" / "main.py").write_text(
        "MODEL_DIR = 'D:/" + _WORKDIR + "/models'", encoding="utf-8"
    )
    (mirror / "keep.txt").write_text("keep", encoding="utf-8")

    code = gate.apply(root, mirror, stream=io.StringIO())

    assert code == 2
    assert not (mirror / "backend" / "main.py").exists(), "私有内容在检查前被复制进镜像"
    assert (mirror / "keep.txt").exists()


def test_apply_never_overwrites_the_intentionally_different_gitignore(tmp_path):
    """`--apply` 不得把工作树的 `.gitignore` 覆盖到镜像上。

    `.gitignore` 是唯一一个**就该两边不同**的文件：工作树版写明本机装了哪些
    AI 编码工具、哪几个目录放着它们的会话状态；镜像版会随仓库公开，只能保留
    保护能力、不能点名工具链。

    2026-11-03 实测：一次 `--apply` 就把工作树那份复制进公开树，17 处工具名
    （具体产品名与内部审计文档的文件名）全部落进镜像。检查层已经放行这个
    差异，但写入层没有 —— 于是"检查通过"反而让人以为同步是对的。

    红-绿：删掉 `apply()` 里的 `INTENTIONALLY_DIFFERENT` 跳过分支，本测试必红。
    """
    root = tmp_path / "root"
    mirror = tmp_path / "mirror"
    (root / "backend").mkdir(parents=True)
    mirror.mkdir()

    (root / "backend" / "main.py").write_text("x = 1", encoding="utf-8")
    # 工作树版：含本机工具链细节，绝不能出现在公开树。
    (root / ".gitignore").write_text(
        "# 本机使用某 AI 工具，其会话状态在 .agent-state/\n.agent-state/\n",
        encoding="utf-8",
    )
    # 镜像版：同样的保护，措辞中性。
    clean = "# Local tool state\n.*-session/\n"
    (mirror / ".gitignore").write_text(clean, encoding="utf-8")

    code = gate.apply(root, mirror, stream=io.StringIO())

    assert code == 0
    assert (mirror / ".gitignore").read_text(encoding="utf-8") == clean, (
        "--apply 覆盖了刻意不同的 .gitignore，把本机工具链写进了公开树"
    )
    # 其余文件照常同步，跳过规则只作用于这一份。
    assert (mirror / "backend" / "main.py").exists()


def test_the_mirrored_gitignore_names_no_local_toolchain():
    """镜像里的 `.gitignore` 不得点名具体工具或内部文档。

    与上一条互补：上一条防 `--apply` 覆盖，这一条防有人手工把工作树版粘进去。
    名单盯的是**产品名**与**内部文件名**这两类，不是某个固定字符串。

    **在公开仓库里跑时 `gate.MIRROR` 不存在**：那棵树本身就是镜像（根目录
    即仓库根），没有 `github/` 子目录可比。此时检查的对象就是根 `.gitignore`
    —— 换成它继续断言，而不是跳过：公开仓库里的这一份恰恰是最该被盯住的。
    """
    candidate = gate.MIRROR / ".gitignore"
    if not candidate.exists():
        # 双树布局不存在 → 这是公开仓库自身，检查仓库根的 .gitignore。
        candidate = Path(__file__).resolve().parents[2] / ".gitignore"
    text = candidate.read_text(encoding="utf-8")
    lowered = text.lower()
    for marker in (
        "lm studio",
        "lmstudio",
        "mimosa",
        "zcode",
        "claude",
        "codex",
        "cursor",
        "copilot",
        "全面审计",
        "独立代码审计",
        "本机",
    ):
        assert marker not in lowered, (
            f"镜像的 .gitignore 出现了 {marker!r}；"
            "公开文件里不该出现本机工具链或内部材料的名字"
        )
