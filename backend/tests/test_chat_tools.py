"""只读工具执行器的安全边界。

这些测试的重点不是"功能能用"，而是"越界读不到"。工具执行是模型触发的、
用户批准的路径，最坏情况是"模型编一个路径把整个磁盘读出来然后发到对话里"，
所以每一条路径约束都必须被钉住。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core import chat_tools  # noqa: E402


@pytest.fixture
def root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("hello world\nsecond line\n", encoding="utf-8")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "deep.txt").write_text("needle inside\n", encoding="utf-8")
    # 授权根**之外**的敏感文件
    (tmp_path / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")
    return workspace


def test_read_file_returns_content_inside_the_root(root):
    result = chat_tools.read_file(root, "notes.txt")
    # Windows 上写 "a\nb" 落盘是 CRLF；断言按行比较而不是按字面，避免把
    # 平台的换行差异当成失败。
    assert result["content"].splitlines() == ["hello world", "second line"]
    assert result["truncated"] is False
    assert result["path"] == "notes.txt"
    # 嵌套路径也允许
    assert chat_tools.read_file(root, "sub/deep.txt")["content"].strip() == "needle inside"


@pytest.mark.parametrize(
    "candidate",
    [
        "../secret.txt",
        "sub/../../secret.txt",
        "..",
        ".",
        "",
        "C:/Windows/win.ini",
        "C:\\Windows\\win.ini",
        "/etc/passwd",
        "\\\\server\\share\\file.txt",
        "//server/share/file.txt",
        "D:relative-on-another-drive.txt",
        None,
    ],
)
def test_read_file_refuses_every_escape_shape(root, candidate):
    """每一条都是真实可行的越界写法，不是凑数。

    ``D:foo`` 在 Windows 上是"D 盘当前目录下的 foo"，含义取决于进程 CWD ——
    所以必须当成绝对路径拒绝，而不是当相对名拼到 root 后面。
    """
    with pytest.raises(chat_tools.ToolError):
        chat_tools.read_file(root, candidate)


def test_a_prefix_sibling_is_not_treated_as_inside_the_root(tmp_path):
    """`C:\\data-evil` 不能被当成 `C:\\data` 的子路径。

    纯字符串前缀比较的经典缺陷：只比 `startswith(root)` 就会放行同名前缀的
    兄弟目录。这里用真实路径 + 分隔符比较。
    """
    workspace = tmp_path / "data"
    workspace.mkdir()
    evil = tmp_path / "data-evil"
    evil.mkdir()
    (evil / "x.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(chat_tools.ToolError):
        chat_tools.read_file(workspace, "../data-evil/x.txt")


def test_symlink_escape_is_resolved_not_string_matched(root, tmp_path):
    """符号链接必须在**解析后**被抓到，字符串比较看不到它。"""
    link = root / "escape"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable in this environment")
    with pytest.raises(chat_tools.ToolError):
        chat_tools.read_file(root, "escape/secret.txt")


def test_read_file_truncates_large_files_and_says_so(root, monkeypatch):
    """截断必须显式标注：给半截内容还装作完整是最坏的一种"看起来成功"。"""
    big = root / "big.txt"
    big.write_text("x" * 5000, encoding="utf-8")
    monkeypatch.setattr(chat_tools, "MAX_READ_BYTES", 1000)
    result = chat_tools.read_file(root, "big.txt")
    assert result["truncated"] is True
    assert len(result["content"]) == 1000


def test_read_file_refuses_binary_content(root):
    (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    with pytest.raises(chat_tools.ToolError, match="binary"):
        chat_tools.read_file(root, "blob.bin")


def test_read_file_refuses_a_directory(root):
    with pytest.raises(chat_tools.ToolError):
        chat_tools.read_file(root, "sub")


def test_glob_search_lists_matches_and_refuses_traversal_patterns(root):
    result = chat_tools.file_glob_search(root, "*.txt")
    assert "notes.txt" in result["matches"]
    assert "sub/deep.txt" in result["matches"]
    # 模式里的 `..` 没有意义，且可能让某些 glob 实现走出根
    with pytest.raises(chat_tools.ToolError):
        chat_tools.file_glob_search(root, "../*")


def test_grep_search_finds_lines_with_numbers(root):
    result = chat_tools.grep_search(root, "needle")
    assert len(result["matches"]) == 1
    hit = result["matches"][0]
    assert hit["path"] == "sub/deep.txt"
    assert hit["line"] == 1
    assert "needle" in hit["text"]


def test_grep_search_respects_the_result_cap(root, monkeypatch):
    for index in range(20):
        (root / f"f{index}.txt").write_text("needle\nneedle\n", encoding="utf-8")
    monkeypatch.setattr(chat_tools, "MAX_SEARCH_RESULTS", 5)
    result = chat_tools.grep_search(root, "needle")
    assert len(result["matches"]) == 5
    assert result["truncated"] is True


def test_search_respects_the_subpath_argument(root):
    only_sub = chat_tools.grep_search(root, "needle", subpath="sub")
    assert all(hit["path"].startswith("sub/") for hit in only_sub["matches"])
    # subpath 同样不能逃逸
    with pytest.raises(chat_tools.ToolError):
        chat_tools.grep_search(root, "TOP", subpath="..")


def test_execute_tool_rejects_unknown_names(root):
    """未知工具一律拒绝 —— 不猜、不降级到"随便跑一个"。"""
    for name in ("exec", "write_file", "delete_file", "", None, 123):
        with pytest.raises(chat_tools.ToolError, match="not allowed"):
            chat_tools.execute_tool(name, {}, root)


def test_execute_tool_dispatches_the_three_read_only_tools(root):
    assert chat_tools.execute_tool("read_file", {"path": "notes.txt"}, root)["tool"] == "read_file"
    assert chat_tools.execute_tool("file_glob_search", {"pattern": "*.txt"}, root)["tool"] == (
        "file_glob_search"
    )
    assert chat_tools.execute_tool("grep_search", {"query": "needle"}, root)["tool"] == "grep_search"


def test_missing_root_is_refused_not_defaulted(tmp_path):
    """没有授权根就拒绝，绝不回落到 CWD 或用户主目录。

    "没传参数就用当前目录"在这条路径上等于"模型可以读写进程工作目录"，
    而那个目录通常是安装目录。
    """
    for bad in (None, "", "   ", 123):
        with pytest.raises(chat_tools.ToolError):
            chat_tools.read_file(bad, "x.txt")


def test_root_must_exist_and_be_a_directory(root):
    with pytest.raises(chat_tools.ToolError):
        chat_tools.read_file(root / "does-not-exist", "x.txt")
    (root / "file.txt").write_text("x", encoding="utf-8")
    with pytest.raises(chat_tools.ToolError):
        chat_tools.read_file(root / "file.txt", "x.txt")
