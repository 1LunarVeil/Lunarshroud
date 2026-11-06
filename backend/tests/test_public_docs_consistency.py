"""公开文档不得提到这个仓库没有的东西。

2026-11-05 的审计里，四类"文档说 A、代码做 B"的静默漂移一次性全中：
`CHANGELOG.md` 指向被排除的 `docs/STATUS.md`、`KNOWLEDGE_BASE.md` 让读者跑
未发布的 `tools/eval_knowledge_retrieval.py`、`FRONTEND_CONTRACT.md` 让贡献者跑
`tools/ui_preview.py`、`backend/api/*.py` 的注释写"见 `docs/ROADMAP.md`"。

它们都不会让任何测试变红——因为当时没有任何测试读过这些引用。这个模块补的
就是那层"读引用"的检查：**公开树里写出来的路径必须真的存在**。

设计上的两个刻意选择：

* 只扫**公开会发布**的文件。`archive/`、`docs/STATUS.md` 这类内部材料本来就
  允许引用别的东西，扫它们只会制造噪音。
* 用 `git ls-files` 取候选，而不是遍历目录。判据是"读者会拿到什么"，而不是
  "磁盘上有什么"——未跟踪的临时产物不该让断言变绿或变红。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _published_files() -> list[Path]:
    """当前工作树里会随源码一起交给读者的文件。

    双树布局下用 `github/` 的清单（它就是镜像本身）；公开仓库里没有
    `github/`，直接用仓库自己的 `git ls-files`。
    """
    mirror = REPO_ROOT / "github"
    if mirror.is_dir():
        cmd = ["git", "-C", str(mirror), "ls-files"]
        base = mirror
    else:
        cmd = ["git", "-C", str(REPO_ROOT), "ls-files"]
        base = REPO_ROOT
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=60
        ).stdout
    except (subprocess.CalledProcessError, OSError) as error:  # pragma: no cover
        pytest.skip(f"git ls-files 不可用，无法判定公开树: {error}")
    return [base / rel for rel in out.split() if rel]


# 会写进公开文档、读者会照着做的路径形态。刻意只认这三类：
#   docs/*.md、tools/<name>.<ext>、以及根目录的已知文件由下面的显式清单覆盖。
# 放宽到"任何 xxx/yyy"会命中大量正文示例（`backend/api/foo.py` 之类），
# 那些是说明性文字而不是让人去打开的路径。
DOC_PATH = re.compile(r"docs/[A-Za-z0-9_\-]+\.md")
TOOL_PATH = re.compile(r"tools/[A-Za-z0-9_\-]+\.(?:py|js|mjs|ps1|sh)")


def _candidates() -> list[Path]:
    """会写进公开文档、读者可能照着找的文件。

    两类刻意排除：

    * **测试文件**：它们大量出现 `docs/a.md`、`tmp/x.yaml` 这类 **fixture
      数据**——是喂给被测函数的输入，不是给读者的指路。
    * **`check_release_mirror.py`**：它逐个列出被排除的文件名，那是它的
      职责（规则表）。这份清单是"这些路径不得进镜像"的断言对象，本身不是
      指路；把它算进来等于要求规则表不许写出它要拦的文件名。
    """
    return [
        p
        for p in _published_files()
        if p.suffix.lower() in {".md", ".py", ".mjs", ".js", ".yml", ".yaml", ".ps1"}
        and not p.name.startswith("test_")
        and p.name not in {"conftest.py", "check_release_mirror.py"}
    ]


def test_published_docs_and_code_only_reference_paths_that_exist():
    """文档与源码注释里提到的 `docs/*.md` / `tools/*` 必须真实存在。

    反例（都真实发生过）：`CHANGELOG.md` 里的 `docs/RISK_ASSESSMENT.md`、
    `docs/KNOWLEDGE_BASE.md` 里的 `tools/eval_knowledge_retrieval.py`、
    `FRONTEND_CONTRACT.md` 里的 `tools/ui_preview.py`。
    """
    missing: dict[str, list[str]] = {}
    for path in _candidates():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern in (DOC_PATH, TOOL_PATH):
                for match in pattern.finditer(line):
                    ref = match.group(0)
                    if (path.parent / ref).exists() or (REPO_ROOT / ref).exists():
                        continue
                    if (REPO_ROOT / "github" / ref).exists():
                        # 双树布局下镜像根就是判据
                        continue
                    missing.setdefault(ref, []).append(
                        f"{path.name}:{lineno}"
                    )
    assert not missing, (
        "公开树里引用了不存在的路径（读者会照着找）:\n"
        + "\n".join(f"  {ref}  <- {', '.join(where[:3])}"
                    for ref, where in sorted(missing.items()))
    )


def test_no_published_file_names_a_deliberately_excluded_document():
    """除了门禁的规则表，公开文件不该点名那些被刻意排除的内部材料。

    这条与上面那条互补：上一条管"这不存在的路径能不能打开"，这一条管"该不该
    在封面上写出私有材料的名字"——即便只是顺口一提，也等于告诉读者这个项目有
    哪些内部文档。

    门禁脚本本身豁免：它必须逐个列出要拦的文件名，那是规则表的全部意义。
    中文文件名（`独立代码审计报告.md` 之类）不在这里列：它们出现在门禁规则表与
    本测试自身的断言里，属于同一豁免逻辑，重复列举只会让这条断言更难维护。
    """
    internal_names = [
        "docs/STATUS.md",
        "docs/HISTORY.md",
        "docs/ROADMAP.md",
        "docs/RISK_ASSESSMENT.md",
        "docs/HANDOVER.md",
        "docs/EVOLUTION.md",
    ]
    offenders: list[str] = []
    for path in _candidates():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover
            continue
        for name in internal_names:
            if name in text:
                offenders.append(f"{path.name}: {name}")
    assert not offenders, (
        "除门禁规则表外，还有公开文件点名内部材料:\n  " + "\n  ".join(offenders)
    )


def test_the_two_operational_guides_exist_and_are_referenced_by_agents():
    """`GIT_PUSH_GUIDE.md` / `RELEASE_GUIDE.md` 必须存在，且 AGENTS.md 指向它们。

    这两份是操作手册：发布链路平时不跑，它的缺陷（`$LASTEXITCODE` 为 $null、
    发布 job 没 checkout 导致 gh 推不出仓库名）不会出现在日常 CI 红点里。
    2026-11-05 首次发版连挂三次，每次都花掉一整轮构建时间。

    指针比文件本身更重要：文件躺在那儿没人读，等于没有。所以这里断言的是
    **AGENTS.md 里确实引用了两份手册**（AGENTS.md 是开局必读的规约文件）。
    """
    root = Path(__file__).resolve().parents[2]
    for name in ("GIT_PUSH_GUIDE.md", "RELEASE_GUIDE.md"):
        assert (root / name).is_file(), f"缺少操作手册 {name}"

    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    for name in ("GIT_PUSH_GUIDE.md", "RELEASE_GUIDE.md"):
        assert name in agents, (
            f"AGENTS.md 里没有指向 {name} 的入口 —— 手册存在但没人会被引导去读"
        )


def test_operational_guides_never_hardcode_a_local_absolute_path():
    """两份手册不得写死本机绝对路径。

    它们会进公开镜像，写死路径既触发隐私门禁，也让读者照抄到一个不存在的目录。
    用 `<仓库根>` 这样的占位符代替。

    marker 用**拼接**构造：这个测试文件本身也会被镜像门禁扫描，把待检字符串
    写成字面量会让门禁把它当成真的泄漏（实测：`--apply` 直接中止，报
    "工作树待发布文件含本机痕迹"）。
    """
    root = Path(__file__).resolve().parents[2]
    markers = (
        "D:" + chr(92),          # 盘符 + 反斜杠
        "/d/" + "agent_tr" + "y",  # 本机工作区路径片段
        "agent_tr" + "y",
    )
    offenders = []
    for name in ("GIT_PUSH_GUIDE.md", "RELEASE_GUIDE.md"):
        text = (root / name).read_text(encoding="utf-8")
        for marker in markers:
            if marker in text:
                offenders.append(f"{name}: {marker}")
    assert not offenders, (
        "操作手册里出现了本机绝对路径，请改用 <仓库根> 占位符:\n  "
        + "\n  ".join(offenders)
    )
