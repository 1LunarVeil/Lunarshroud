#!/usr/bin/env python3
"""发布镜像门禁：比对工作树与 ``github/`` 公开镜像。

为什么需要它
------------
``github/`` 是一份**手工维护**的公开源码镜像（见 ``docs/HANDOVER.md`` 的
"Public-source workflow"）。手工 ``cp`` 的下场已经发生过一次：镜像停在 P1
之前的状态，``backend/core`` 少了 5 个模块、整套 i18n 与测试夹具都没进去，
而外界拿到的就是那份旧代码（见 ``独立代码审计报告.md`` §3.3）。

本脚本把"镜像该长什么样"变成**可复跑的一条命令**：工作树里所有
"该发布"的文件，镜像里必须逐一存在且内容逐字节一致；镜像里不允许出现
任何"不该发布"的路径。任一条不满足 → 非 0 退出码。

检查项
------
    [1] 镜像缺失    工作树有、镜像里没有（含"新增文件没进镜像"）
    [2] 内容漂移    两边都有，但 SHA-256 不同
    [3] 镜像多余    镜像里有、工作树没有（陈旧文件，应删除）
    [4] 禁用路径    镜像里出现本该忽略的路径（config.json / *.bak* / *.exe / …）
    [5] 隐私面      镜像与工作树待发布侧含本机绝对路径或本机用户名；按
                    UTF-8 / UTF-16LE / UTF-16BE / latin-1 及 gzip、zlib 容器
                    逐一解码后匹配，读不到或超限的文件按未覆盖报出

用法（纯标准库，无第三方依赖）::

    py -X utf8 tools/check_release_mirror.py              # 检查，有问题则退出码 1
    py -X utf8 tools/check_release_mirror.py --json       # 机器可读
    py -X utf8 tools/check_release_mirror.py --apply      # 按同一套规则刷新镜像

``--apply`` 刻意与检查共用同一份规则：如果刷新和检查各写一套，迟早会
互相打架（"脚本说 clean、diff 说有差异"）。
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIRROR = ROOT / "github"

# --------------------------------------------------------------------------
# 规则 1：工作树里"不参与镜像"的路径
# --------------------------------------------------------------------------

# 任意层级都不进镜像的目录名
EXCLUDED_DIR_NAMES = frozenset({
    ".git", ".claude", ".mimosa", ".idea", ".vscode", ".eggs",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "env",
    "htmlcov", "shots", "wheelhouse",
    # 应用运行时目录：便携版从任意工作目录启动都可能在 exe 旁创建它们；
    # 规则按目录名在任意层级排除，而不是只认仓库根。
    "webview2-data", "runtime-temp", "appdata", "logs",
})

# 只在这些位置排除的目录（构建产物 / 运行时数据 / 镜像自身）
EXCLUDED_DIR_PREFIXES = (
    "github/",
    "tmp/",                 # 本地调试脚本与截图
    "backend/build/",
    "backend/dist/",
    "backend/data/",
    # PyInstaller onedir 的运行时目录：DLL/PYD/PYC 等构建产物，部署时与
    # backend.exe 放在一起，但绝不进源码镜像。
    "backend/_internal/",
    "src-tauri/target/",
    "src-tauri/gen/",
    # build.rs 的 staging 目录：backend.exe + _internal，安装包资源来源。
    "src-tauri/backend/",
    # data/ 整目录排除，不逐个列名：SQLite WAL 会产生 history.db-wal/-shm，
    # 配置损坏时 config.py:284 会写 config.json.corrupt（内含本机模型目录绝对路径）。
    # 这些派生名不可枚举，逐条列名必然漏 —— 审计已实证漏网并被 --apply 复制进镜像。
    "data/",
    # 应用在 exe 同目录创建的运行时目录。用户从源码树根直接运行便携版时，
    # 这些目录就落在仓库根：WebView2 缓存含本机路径/站点数据，launcher.log
    # 含环境变量，runtime-temp 可残留 PyInstaller 解压件 —— 都不是源码。
    "webview2-data/",
    "logs/",
    "runtime-temp/",
    "appdata/",
    # 外部 AI 工具导出的本地会话目录（dsh-session-*）：整段对话、附件与
    # 本机绝对路径都在里面，绝不能进公开镜像。
    "dsh-session-",
)

# 构建产物/归档后缀（注意：.png/.ico 是源码资源，不能按后缀一刀切）
EXCLUDED_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".pyd", ".dll", ".so", ".exe", ".msi", ".app",
    ".dmg", ".appimage", ".deb", ".log", ".zip", ".pkg", ".pyz",
    ".toc", ".o", ".d", ".rlib", ".rmeta", ".swp", ".swo",
    ".db", ".sqlite", ".sqlite3",
    # 调试/链接产物：PDB 内嵌**绝对构建路径**，而它又是二进制。与 .lnk 事故
    # 同一类 —— 那次靠枚举后缀补上，这次把同族的一起补齐。
    ".pdb", ".lib", ".exp",
})

# 按文件名/相对路径排除（含通配）
EXCLUDED_GLOBS = (
    ".coverage", ".coverage.*", "coverage.xml",
    "stdout.txt", "stderr.txt", "SHA256SUMS.txt",
    "Thumbs.db", ".DS_Store",
    "*.bak*",               # param_builder.py.bak-20260917 / param_schema.py.bak2-20260917
    # .lnk 存的是 shell 二进制结构，目标绝对路径以 UTF-16LE 编码，内容隐私扫描
    # 走 UnicodeDecodeError 分支整体跳过，所以按 ASCII 匹配的正则看不见它。
    # 唯一可靠的控制点是规则表本身 —— 工作区根目录一旦出现快捷方式，
    # 它就把本机路径带进公开镜像。
    "*.lnk",
    # data/ 已由 EXCLUDED_DIR_PREFIXES 整目录排除，此处不再逐条列名
    ".env", ".env.*",
    "*.pem", "*.key", "*.pfx", "*.p12",
    "credentials*.json", "secrets*.json",
    # Claude Code 的项目指令文件（任意层级）：含本机工具约定，公开约定见 AGENTS.md。
    "CLAUDE.md",
    # 外部工具在源码根掉落的 tempfile 风格残留（8 位小写字母/数字，无扩展名）。
    # 实测 2026-10-18 出现 backend/jy4s_b4h（4 字节），不是源码也不是数据；
    # 按 8 字符名精确匹配，避免把正常源码文件一起排掉。
    "backend/????????",
)

# 规则 2：**刻意**不进镜像的工作树文件 —— 每一条都要有理由。
# 镜像按 HANDOVER 的口径是 "curated public-source mirror"（只放批准的源码/文档），
# 不是工作树的完整快照；含本机私有信息的文件不能进去。
CURATED_EXCLUDES = {
    # AI 编码工具自动生成的忽略/配置文件。它们不是项目配置，
    # 且会泄露"这个项目用什么工具写的"；.zcodeignore 内部甚至自证
    # "以上为 ZCode 默认排除规则（从 .gitignore 同步）"。
    # 这一类按文件名列全，新增工具时补在这里。
    ".zcodeignore":
        "AI 编码工具（ZCode）自动生成的忽略文件，非项目配置",
    ".cursorignore": "AI 编码工具（Cursor）的项目级忽略文件，非项目配置",
    ".aiexclude": "AI 编码工具通用的排除文件，非项目配置",
    ".aiderignore": "AI 编码工具（Aider）的忽略文件，非项目配置",
    ".codeiumignore": "AI 编码工具（Codeium/Windsurf）的忽略文件，非项目配置",
    "全面审计文档.md":
        "含本机绝对路径与未修复漏洞的完整细节（内部审计件，不对外）",
    "独立代码审计报告.md":
        "含本机用户名与工程绝对路径（内部审计件，不对外）",
    "LunarShroud-演进规划.md":
        "含本机绝对路径（内部演进记录，不对外）",
    "docs/archive/":
        "历史交接/测试材料，含本机绝对路径；docs/ 是 0.9.0-beta.1 权威文档，不改不搬",
    "CLAUDE.md":
        "AI 编码工具（Claude Code）的项目指令，非面向用户的项目文档；公开约定见 AGENTS.md",
}

# 2026-09-18 起：原本还有 3 个 tools/*.py 因硬编码本机模型库绝对路径被排除。
# 它们已改为走 tools/_local_model.py 解析（环境变量 LUNARSHROUD_TEST_MODEL →
# 配置里的 default_model_dir），本机路径消失，因此现在可以正常进镜像。

# 规则 2b：允许**只存在于镜像**的文件（镜像自带内容，工作树里没有）。
# 目前为空。曾放行过 data/presets/builtin_*.json，2026-09-18 删除 ——
# 实测 preset_store.list_presets() 会把它们当活预设读出来（PRESETS_DIR 就是
# data/presets），克隆镜像一跑，P0 已删除的 3 个旧预设就会在 UI 里复活，
# 这与 STATUS.md "do not publish data\presets\" 的口径一致。机制保留，
# 以备将来镜像确需自带文件时使用（须写明理由）。
MIRROR_ONLY_ALLOWED = ()

# --------------------------------------------------------------------------
# 规则 3：镜像里**绝不能出现**的路径（同样覆盖 CURATED_EXCLUDES 的反向保护）
# --------------------------------------------------------------------------
FORBIDDEN_GLOBS = (
    ".claude/*", ".mimosa/*", "tmp/*", "github/*",
    "*__pycache__/*", "*.pytest_cache/*", "*.ruff_cache/*", "*.mypy_cache/*",
    "*.exe", "*.msi", "*.dll", "*.pyd", "*.so", "*.pyc", "*.pyo",
    "*.pdb", "*.lib", "*.exp",
    "*.bak*", "*.log", "*.db", "*.sqlite", "*.sqlite3", "*.lnk",
    "*.pem", "*.key", "*.pfx", "*.p12",
    "credentials*.json", "secrets*.json",
    ".env", "*/.env", ".env.*", "*/.env.*",
    # AI 编码工具的配置/状态目录与其忽略文件（与 .claude/.mimosa 同一类）
    ".zcodeignore", ".cursorignore", ".aiexclude", ".aiderignore",
    ".codeiumignore", ".cursor/*", ".aider*/*", ".continue/*",
    ".coverage", "*/.coverage", ".coverage.*", "*/.coverage.*", "coverage.xml",
    "*node_modules/*", "*htmlcov/*", "*wheelhouse/*", "*shots/*",
    "*src-tauri/target/*", "src-tauri/target/*",
    "src-tauri/backend/*", "*src-tauri/backend/*",
    "*backend/dist/*", "backend/dist/*", "*backend/build/*", "backend/build/*",
    "backend/_internal/*", "*backend/_internal/*",
    "data/*",
    # 应用运行时目录与本地 AI 会话：即使它们在镜像里出现也必须判违规。
    # 前缀 `*` 让规则覆盖任意层级（fnmatch 的 `*` 也匹配 `/`）。
    "webview2-data/*", "*webview2-data/*",
    "logs/*", "*logs/*",
    "runtime-temp/*", "*runtime-temp/*",
    "appdata/*", "*appdata/*",
    "dsh-session-*/*", "*dsh-session-*/*",
    "CLAUDE.md", "*CLAUDE.md",
    "backend/????????",
)

# 规则 4：镜像文本里不能出现本机痕迹。
# 口径与既往隐私扫描一致：工作目录名 / 本机用户名 + 任何 C:\Users 绝对路径。
# 刻意不用泛化的 "X:\\" —— 文档里的示例路径会误报。
#
# **不要把本机用户名/工作区名硬编码在这里。** 这个脚本本身会进公开镜像：
# 写成字面量等于把要藏的东西印在封面上（"拼一下就能读出来"也一样）。
# 标记在运行时从环境/路径推导：
#   - workdir  : LUNARSHROUD_PRIVACY_WORKDIR，否则工作区父目录名
#   - username : USERNAME / USER 环境变量
#   - 额外标记 : LUNARSHROUD_PRIVACY_MARKERS（逗号分隔，例如本机模型库路径片段）
# 门禁的意义是证明"当前这台机器"的痕迹没有外泄，所以推导比写死更准确；
# 公开仓库里跑（没有 github/）不会做比对，也就不需要这些标记。
def _local_markers() -> tuple[tuple[str, str], ...]:
    markers: list[tuple[str, str]] = []
    workdir = os.environ.get("LUNARSHROUD_PRIVACY_WORKDIR") or Path(ROOT).parent.name
    if workdir and len(workdir) >= 3:
        markers.append(("workdir", workdir))
    username = os.environ.get("USERNAME") or os.environ.get("USER")
    if username and len(username) >= 3:
        markers.append(("username", username))
    extras = os.environ.get("LUNARSHROUD_PRIVACY_MARKERS", "")
    for index, marker in enumerate(extras.split(",")):
        marker = marker.strip()
        if len(marker) >= 3:
            markers.append((f"extra{index}", marker))
    return tuple(markers)


def privacy_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """按**本次运行**的环境推导隐私匹配表（测试可经环境变量注入标记）。"""
    return tuple(
        (label, re.compile(re.escape(marker), re.I))
        for label, marker in _local_markers()
    ) + (
        # 分隔符允许 1~2 个：JSON 里的绝对路径是转义过的（盘符 + 两个反斜杠 + Users），
        # 只允许单个分隔符的正则会被它绕过（审计已实证）。
        ("userprofile", re.compile(r"[A-Za-z]:[\\/]{1,2}User[s][\\/]{1,2}", re.I)),
    )


def _matches_any(rel: str, patterns) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in patterns)


def _curated_reason(rel: str) -> str | None:
    for curated, reason in CURATED_EXCLUDES.items():
        if rel == curated or (curated.endswith("/") and rel.startswith(curated)):
            return reason
    return None


def is_excluded(rel: str) -> tuple[bool, str]:
    """工作树相对路径是否不进镜像；返回 (排除?, 原因)。"""
    for part in rel.split("/"):
        if part in EXCLUDED_DIR_NAMES:
            return True, f"缓存/IDE/运行时目录 {part}/"
        if part.startswith("dsh-session-"):
            return True, "本地 AI 会话目录 dsh-session-*/"
    for prefix in EXCLUDED_DIR_PREFIXES:
        if rel == prefix.rstrip("/") or rel.startswith(prefix):
            return True, f"构建/运行时路径 {prefix}"
    suffix = Path(rel).suffix.lower()
    if suffix in EXCLUDED_SUFFIXES:
        return True, f"构建产物后缀 {suffix}"
    if _matches_any(rel, EXCLUDED_GLOBS):
        return True, "忽略规则"
    reason = _curated_reason(rel)
    if reason:
        return True, f"刻意排除：{reason}"
    return False, ""


def _pruned_dir(rel_dir: str, name: str) -> bool:
    if name in EXCLUDED_DIR_NAMES or name.startswith("dsh-session-"):
        return True
    rel = f"{rel_dir}/{name}" if rel_dir else name
    return any(rel.startswith(prefix) for prefix in EXCLUDED_DIR_PREFIXES)


def is_forbidden(rel: str) -> bool:
    """镜像里出现这个路径就是违规。"""
    if _matches_any(rel, MIRROR_ONLY_ALLOWED):
        return False
    if _matches_any(rel, FORBIDDEN_GLOBS):
        return True
    return _curated_reason(rel) is not None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk(base: Path, prune_excluded: bool) -> dict[str, Path]:
    """列出目录下全部文件（相对 base 的 posix 路径）。

    ``prune_excluded=True`` 用于工作树（跳过 tmp/、target/ 等，避免遍历 8k+ 文件）；
    镜像侧**必须**不剪枝 —— 藏在 tmp/ 里的文件正是要抓的违规项。

    唯一的例外是 ``.git``。镜像一旦 ``git init`` 就会有自己的 ``.git/``，而那是仓库
    自身的元数据、不是要发布的内容（git 永不提交自己的 .git）。不跳过它的话，门禁
    会把上千个 git 对象当成"镜像多出的文件"、还要逐一送进隐私扫描 —— 也就是说
    **"初始化仓库"这个动作会把发布门禁本身弄坏**。这条在工作树侧同样要跳过，
    否则在仓库根 git init 之后门禁也会崩。
    """
    found: dict[str, Path] = {}
    if not base.is_dir():
        return found
    for current, dirs, files in os.walk(base):
        rel_dir = Path(current).relative_to(base).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        # .git 两侧都跳过（见上方 docstring）；其余目录镜像侧照旧不剪枝
        dirs[:] = [d for d in dirs if d != ".git"]
        if prune_excluded:
            dirs[:] = sorted(d for d in dirs if not _pruned_dir(rel_dir, d))
        else:
            dirs[:] = sorted(dirs)
        for name in sorted(files):
            full = Path(current) / name
            found[full.relative_to(base).as_posix()] = full
    return found


def expected_tree(root: Path) -> dict[str, Path]:
    """工作树里应当出现在镜像中的文件。"""
    result: dict[str, Path] = {}
    for rel, full in walk(root, prune_excluded=True).items():
        excluded, _ = is_excluded(rel)
        if not excluded:
            result[rel] = full
    return result


# 本机路径存在文件里的形态不止 UTF-8 一种：
#   - .lnk 把目标存成 UTF-16LE
#   - .pdb / .exe / .dll 里 ASCII 与 UTF-16LE 混排
#   - 一个 latin-1 / GBK 字节就能让整份文件在 UTF-8 解码时失败
# 原实现走 UnicodeDecodeError 分支整体 continue，于是上述全部隐身（审计已实证
# UTF-16LE、UTF-16BE、gzip、latin-1、JSON 转义 5 种绕过）。改成读**原始字节**、
# 对每种编码各解一次再匹配。latin-1 永不失败，所以每份文件至少有一个视图，
# 二进制里的 ASCII 路径也能被抓到。
_SCAN_ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be", "latin-1")

# 单文件扫描上限。镜像里最大的是文档（百余 KB），余量给足；设上限是为了有人
# 塞进一个几百 MB 的二进制时门禁不被拖死 —— 超限按"未能覆盖"报出而非静默跳过。
_MAX_SCAN_BYTES = 32 * 1024 * 1024


def _decoded_views(data: bytes) -> list[str]:
    """把原始字节按多种编码各解一次；另试 gzip / 裸 zlib 容器。"""
    views: list[str] = []

    def add(blob: bytes) -> None:
        for encoding in _SCAN_ENCODINGS:
            try:
                views.append(blob.decode(encoding))
            except UnicodeDecodeError:
                continue

    add(data)
    # 压缩容器同样要看得见：规则表漏一个归档后缀时，内容扫描是最后一道。
    for decompress in (gzip.decompress, zlib.decompress):
        try:
            add(decompress(data))
            break
        except Exception:
            continue
    return views


def privacy_hits(base: Path, files) -> list[list[str]]:
    """扫描每个文件的本机痕迹。**读不到 / 超限 也算命中**（fail closed）。

    刻意不按后缀白名单筛选：白名单会让 data/config.json.corrupt、history.db-wal
    这类后缀不在名单里的文本整份逃过扫描（审计已实证）。

    同样刻意**不静默跳过**任何文件：扫描不到就等于不能证明干净，而门禁的意义
    正是证明干净。原实现把 UnicodeDecodeError 与 OSError 一起 continue 掉，
    于是所有二进制都成了盲区。
    """
    hits: list[list[str]] = []
    for rel in files:
        try:
            data = (base / rel).read_bytes()
        except OSError as exc:
            hits.append([rel, "unreadable", f"无法读取，隐私扫描未覆盖：{exc}"])
            continue
        if len(data) > _MAX_SCAN_BYTES:
            hits.append([
                rel, "oversize",
                f"{len(data)} 字节超过上限 {_MAX_SCAN_BYTES}，隐私扫描未覆盖",
            ])
            continue
        for label, pattern in privacy_patterns():
            snippet = None
            for view in _decoded_views(data):
                match = pattern.search(view)
                if match:
                    snippet = view[max(0, match.start() - 30): match.start() + 40]
                    break
            if snippet is not None:
                clean = " ".join(snippet.replace(chr(0), " ").split())
                hits.append([rel, label, clean])
                break
    return hits



def inspect(root: Path = ROOT, mirror: Path = MIRROR) -> dict:
    expected = expected_tree(root)
    actual = walk(mirror, prune_excluded=False)

    missing = sorted(rel for rel in expected if rel not in actual)
    drift = sorted(
        rel for rel in expected
        if rel in actual and sha256(expected[rel]) != sha256(actual[rel])
    )
    extra = sorted(
        rel for rel in actual
        if rel not in expected and not _matches_any(rel, MIRROR_ONLY_ALLOWED)
    )
    forbidden = sorted(rel for rel in actual if is_forbidden(rel))
    # 两侧都扫：只扫镜像的话，工作树里已经有本机路径、但还没 --apply 过去
    # 这种状态要等到刷新之后才被发现 —— 而 --apply 正是把它公开的那一步。
    # 工作树侧只扫**将要发布**的那批文件（expected），否则 data/、tmp/ 里
    # 合法的本地数据会淹没结果。
    privacy = privacy_hits(mirror, sorted(actual))
    privacy += [
        ["worktree:" + hit[0], hit[1], hit[2]]
        for hit in privacy_hits(root, sorted(expected))
        if hit[0] not in actual
    ]

    return {
        "expectedCount": len(expected),
        "mirrorCount": len(actual),
        "missing": missing,
        "drift": drift,
        "extra": extra,
        "forbidden": forbidden,
        "privacy": privacy,
        "mirrorCarried": sorted(
            rel for rel in actual
            if rel not in expected and _matches_any(rel, MIRROR_ONLY_ALLOWED)
        ),
    }


def _problem_count(result: dict) -> int:
    return (
        len(result["missing"]) + len(result["drift"]) + len(result["extra"])
        + len(result["forbidden"]) + len(result["privacy"])
    )


def report(result: dict, stream) -> int:
    print("== LunarShroud 发布镜像门禁 (github/) ==", file=stream)
    print(f"工作树待镜像文件: {result['expectedCount']}    镜像文件: {result['mirrorCount']}",
          file=stream)
    print(file=stream)

    sections = (
        ("[1] 镜像缺失（工作树有、镜像没有）", result["missing"], "  - "),
        ("[2] 内容漂移（两边都有、SHA-256 不同）", result["drift"], "  ~ "),
        ("[3] 镜像多余（镜像有、工作树没有 → 陈旧，应删）", result["extra"], "  + "),
        ("[4] 禁用路径（镜像里出现本该忽略的路径）", result["forbidden"], "  ! "),
    )
    for title, items, bullet in sections:
        print(f"{title}：{len(items)} 项", file=stream)
        for rel in items:
            print(f"{bullet}{rel}", file=stream)
        print(file=stream)

    print(f"[5] 隐私面（镜像文本含本机绝对路径/用户名）：{len(result['privacy'])} 项",
          file=stream)
    for rel, label, snippet in result["privacy"]:
        print(f"  ! {rel}  [{label}]  …{snippet}…", file=stream)
    print(file=stream)

    carried = result.get("mirrorCarried") or []
    print(f"[i] 镜像自带（工作树没有、按规则允许保留）：{len(carried)} 项", file=stream)
    for rel in carried:
        print(f"  * {rel}", file=stream)
    print(file=stream)

    problems = _problem_count(result)
    if problems:
        print(f"FAIL：共 {problems} 处问题（缺失 {len(result['missing'])} / 漂移 "
              f"{len(result['drift'])} / 多余 {len(result['extra'])} / 禁用 "
              f"{len(result['forbidden'])} / 隐私 {len(result['privacy'])}）", file=stream)
        return 1
    print(f"CLEAN：镜像与工作树一致（{result['expectedCount']} 个文件，0 处问题）",
          file=stream)
    return 0


def apply(root: Path = ROOT, mirror: Path = MIRROR, stream=sys.stdout) -> int:
    """把镜像刷成工作树的样子（与检查共用同一套规则）。返回检查退出码。

    **先检查、后写入**：`--apply` 是发布动作，不能先把含本机路径的文件复制
    进镜像、再在报告里说它有问题 —— 那时私有内容已经落在公开树里了。
    """
    expected = expected_tree(root)
    forbidden = sorted(rel for rel in expected if is_forbidden(rel))
    if forbidden:
        print("--apply 中止：以下待发布路径命中禁用规则，未写入任何文件：", file=stream)
        for rel in forbidden:
            print(f"  ! {rel}", file=stream)
        return 2
    privacy = privacy_hits(root, sorted(expected))
    if privacy:
        print("--apply 中止：工作树待发布文件含本机痕迹，未写入任何文件：", file=stream)
        for rel, label, snippet in privacy:
            print(f"  ! {rel}  [{label}]  …{snippet}…", file=stream)
        return 2
    actual = walk(mirror, prune_excluded=False)

    added, changed, removed = [], [], []
    for rel, full in sorted(expected.items()):
        target = mirror / rel
        if rel not in actual:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(full, target)
            added.append(rel)
        elif sha256(full) != sha256(actual[rel]):
            shutil.copy2(full, target)
            changed.append(rel)
    for rel in sorted(actual):
        if rel not in expected and not _matches_any(rel, MIRROR_ONLY_ALLOWED):
            (mirror / rel).unlink()
            removed.append(rel)
    # 清掉因删除而空掉的目录（只处理镜像顶层之下的空目录）。
    # **必须跳过 .git**：镜像本身是 Git 仓库时，`git gc`/pack-refs 之后
    # `.git/refs/heads`、`.git/objects/info` 等目录可能是空的；把它们删掉
    # 会让 `git` 直接报 "not a git repository"（实测：一次 --apply 就把
    # 仓库的 refs 目录清空了）。`.git` 是仓库元数据，不是待发布内容。
    for current, _dirs, _files in os.walk(mirror, topdown=False):
        path = Path(current)
        if path != mirror and ".git" not in path.relative_to(mirror).parts:
            if not any(path.iterdir()):
                path.rmdir()

    print(f"--apply 完成：新增 {len(added)} / 修改 {len(changed)} / 删除 {len(removed)}",
          file=stream)
    for rel in added:
        print(f"  A {rel}", file=stream)
    for rel in changed:
        print(f"  M {rel}", file=stream)
    for rel in removed:
        print(f"  D {rel}", file=stream)
    print(file=stream)
    return report(inspect(root, mirror), stream)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="LunarShroud 发布镜像门禁：工作树 vs github/",
    )
    parser.add_argument("--apply", action="store_true",
                        help="按同一套规则刷新镜像（默认只检查、不写盘）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--allow-missing-mirror",
        action="store_true",
        help="镜像目录不存在时跳过（默认 fail closed）",
    )
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    parser.add_argument("--mirror", default=str(MIRROR), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    root, mirror = Path(args.root).resolve(), Path(args.mirror).resolve()
    if not mirror.is_dir():
        if args.apply:
            print(f"错误：镜像目录不存在，拒绝 --apply：{mirror}", file=sys.stderr)
            return 2
        if args.allow_missing_mirror:
            # 显式 opt-in（例如在公开镜像自身里运行）：这里没有可比的镜像。
            print(
                "== LunarShroud 发布镜像门禁 =="
                f"未找到镜像目录 {mirror}；已按 --allow-missing-mirror 跳过。",
                file=sys.stdout,
            )
            return 0
        # Fail closed：镜像丢失/路径写错时绝不能返回 0，否则发布流程会
        # 在"一个文件都没比"的情况下拿到假绿。公开仓库的 CI 用
        # `if: hashFiles('github/**') != ''` 跳过本步骤，不需要放行分支。
        print(
            f"错误：未找到发布镜像目录 {mirror}。"
            "工作树里该目录必须存在；若你就在公开镜像内运行，请显式加 "
            "--allow-missing-mirror。",
            file=sys.stderr,
        )
        return 2

    if args.apply:
        return apply(root, mirror)

    result = inspect(root, mirror)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if _problem_count(result) else 0
    return report(result, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
