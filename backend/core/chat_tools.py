"""只读工具的**执行**侧（read_file / file_glob_search / grep_search）。

为什么执行在后端而不是前端：WebView 侧的 Tauri capability 只放行几个自定义
命令，没有任何 ``fs:`` 权限（见 ``src-tauri/capabilities/default.json``）。
把文件读取塞进前端需要新开一条 fs 通道，那条通道的授权粒度是"整个磁盘或用户
选的几个路径"，而这里的工具要的是"**在用户显式授权的目录内**读"。放在后端
才能把这条边界实现成一次 ``resolve()`` 比较。

安全边界（每一条都对应一个真实的越界路径）：

1. **只读**。没有任何写入/执行能力，所以"批准"一次的最坏后果是"读了一份本来
   就在这个目录里的文件"。
2. **根目录是显式授权**，不是"用户主目录"。调用方必须在每个请求里带上
   ``root``；本模块不读任何全局配置来决定能看哪里。
3. **``resolve()`` 之后再比较**：符号链接、junction、``..``、盘符相对路径
   全部在真实路径上失效。只做字符串比较会被一条 ``link -> C:\\Users`` 绕过。
4. **体积与条数上限**：读取截断要显式标注（不能给半截内容还装作完整），
   搜索结果有上限，避免一次 grep 扫出几十万行把响应撑爆。

这个模块**不**决定"要不要执行" —— 那是人点"允许"的事。它只回答"如果允许了，
最多能读到什么"。
"""
from __future__ import annotations

import fnmatch
import os
import stat as _stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# 单次读取上限。超过就截断并在结果里写明，绝不静默返回半截。
#
# **必须小于 chat_store.MAX_CONTENT_CHARS（200,000）。**
# 2026-11-02 实测缺陷：这里曾是 256 KiB，而工具结果要以 `role="tool"` 的
# 消息落库 —— 超过 200,000 字符时 `append_message` 抛
# `ValueError: content is too long`，于是"工具真的读了文件、结果写不进库"，
# 刷新后模型那条 tool_calls 永远没有配对结果（前端只会 toast 一句保存失败）。
# 180,000 留出 10% 余量给结果头部的描述文本。
MAX_READ_BYTES = 180_000
# 搜索结果条数上限。
MAX_SEARCH_RESULTS = 100
# 单行长度上限：一条压缩过的 JS 可能是一整行几 MB。
MAX_LINE_CHARS = 2000
# grep 结果的**累计字符预算**。条数上限（100）单独不成立：100 × 2000 字符
# = 200,000，正好等于 `chat_store.MAX_CONTENT_CHARS`，而前端渲染时每条还要
# 加 `path:line: ` 前缀。实测 100 条 1999 字符的行渲染成 201,089 字符 →
# `append_message` 抛 "content is too long" → 工具结果写不进库。
# 160,000 给前缀与头部描述留出约 20% 余量。
MAX_RESULT_CHARS = 160_000
# glob / grep 遍历的条目上限，防止在大目录上卡死。
MAX_WALK_ENTRIES = 20_000

READ_ONLY_TOOLS = frozenset({"read_file", "file_glob_search", "grep_search"})


class ToolError(Exception):
    """可预期的工具执行失败（路径越界、文件不存在等），带用户可读原因。"""


def _normalize_root(root: Any) -> Path:
    if not isinstance(root, (str, os.PathLike)) or not str(root).strip():
        raise ToolError("no authorized root directory was provided")
    try:
        resolved = Path(root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ToolError(f"authorized root is not usable: {error}") from error
    if not resolved.is_dir():
        raise ToolError("authorized root must be a directory")
    return resolved


def resolve_inside(root: Path, candidate: Any) -> Path:
    """把模型给的路径解析成 root 内的真实路径，否则抛 ``ToolError``。

    先 ``resolve(strict=False)`` 再比较前缀：``strict=False`` 让"还不存在的
    文件"也能算出规范路径（read_file 随后自己判断存在性），但 symlink/junction
    在 resolve 时就已展开，所以逃逸会在**真实**路径上被抓到。

    **硬链接无法靠 resolve 防住，必须单独判。** 2026-11-23 安全审计实测：
    `mklink /H <root>\\report.md <outside>\\secret.txt` 之后，`read_file(root,
    "report.md")` 读出了 root 之外的内容——硬链接不是一个"链接"，`resolve()`
    会原样返回 root 内的那个名字，前缀检查自然通过。而读工具**不需要审批**，
    所以这条等于"授权一个目录，模型就能读任何被硬链接进来的文件"。

    因此这里对**已存在的文件**检查 ``st_nlink > 1`` 并拒绝。这会误伤用户自己
    在授权目录里手动建的硬链接（罕见），但那个方向的代价是"多拒绝一次"，另一
    个方向的代价是"静默读走整块磁盘"。fail closed。
    """
    raw = "" if candidate is None else str(candidate).strip()
    if not raw:
        raise ToolError("path must not be empty")
    if raw.startswith(("\\\\", "//")):
        raise ToolError("UNC paths are not allowed")
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":"):
        raise ToolError("absolute paths are not allowed; use a path inside the root")
    try:
        target = (root / raw).resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ToolError(f"could not resolve path: {error}") from error
    # 前缀比较用真实路径：`C:\data-evil` 不能被当成 `C:\data` 的子路径。
    if target != root and root not in target.parents:
        raise ToolError("path escapes the authorized root")
    if target == root:
        raise ToolError("path must name something inside the root, not the root itself")
    return target


def assert_single_link(root: Path, target: Path) -> None:
    """拒绝一个在授权根内、但存在多个硬链接的文件。

    调用点：``read_file`` / ``grep_search`` 这类**不需要审批**的读路径。写路径
    不必调用——`_write_atomic` 的 ``os.replace`` 会断开链接，写穿不成立（实测）。

    ``st_nlink`` 只对已存在的文件有意义；不存在的目标交给调用方判断存在性。
    ``st_nlink`` 在 Windows 上对目录恒为 1，所以这里只判文件。
    """
    try:
        stat_result = target.stat()
    except OSError:
        return
    if not _stat.S_ISREG(stat_result.st_mode):
        return
    if stat_result.st_nlink > 1:
        raise ToolError(
            "this file has multiple hard links, so its content may live outside the "
            "authorized folder; refusing to read it"
        )


def sanitize_text(text: str) -> str:
    """Make a string safe to store as SQLite TEXT and to JSON-encode.

    Two classes of input break the pipeline rather than the reader:

    * **U+0000** — SQLite's `TEXT` cannot hold it; `append_message` raises
      `ValueError: the query contains a null character`, which aborts the whole
      agent run with an "internal error" the user cannot act on. A text file with a
      stray NUL (a truncated log, a mixed-format export) triggers it.
    * **lone surrogates** — Python strings can hold `\\ud800`, but UTF-8 encoding
      raises `UnicodeEncodeError`. They reach us when a model emits an escaped
      surrogate pair or a file is decoded with `surrogateescape`.

    Both are replaced with U+FFFD: the content stays readable and the transcript
    keeps the fact that *something* was there, which is more useful to the model
    than a dropped line or a dead run.

    2026-11-23 audit finding L1.
    """
    if not text:
        return text
    if "\x00" in text:
        text = text.replace("\x00", "\ufffd")
    # `encode("utf-8", "replace")` maps lone surrogates to `?`, not U+FFFD, and also
    # rewrites nothing else — the explicit pass is what keeps the marker consistent.
    return "".join(
        "\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in text
    )


def _read_text(path: Path) -> tuple[str, bool]:
    """读文件，返回 ``(文本, 是否被截断)``。二进制文件明确拒绝。"""
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ToolError(f"could not stat file: {error}") from error
    if not path.is_file():
        raise ToolError("not a file")
    truncated = size > MAX_READ_BYTES
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_READ_BYTES + 1)
    except OSError as error:
        raise ToolError(f"could not read file: {error}") from error
    if b"\x00" in raw[:8192]:
        raise ToolError("refusing to read a binary file")
    text = sanitize_text(raw[:MAX_READ_BYTES].decode("utf-8", "replace"))
    return text, truncated


_FILE_ATTRIBUTE_REPARSE_POINT = getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link_entry(path: Path) -> bool:
    """符号链接或重解析点（junction / mount point）。

    ``os.walk(followlinks=False)`` **管不住 Windows junction** —— 它把 junction
    当成普通目录下钻，``entry.is_symlink()`` 也返回 False。可靠的判据是
    ``stat(follow_symlinks=False)`` 上的 ``FILE_ATTRIBUTE_REPARSE_POINT``
    （``model_scanner._is_link_entry`` 出于同一原因用了它）。

    实测 2026-11-03：``root/portal -> outside`` 的 junction 下有一份
    ``secret.txt``，``read_file`` 正确地报 "path escapes the authorized root"，
    而 ``grep_search`` / ``file_glob_search`` 把这个文件列了出来并读到了内容 ——
    边界只挡住了一个工具。取不到属性时按链接处理（fail closed）。
    """
    try:
        if path.is_symlink():
            return True
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def _iter_files(root: Path) -> Iterable[Path]:
    """遍历 root 下的文件，**不穿越**任何符号链接或 junction。

    两道必须同时存在的防线：

    1. ``os.walk(followlinks=False)`` 挡符号链接目录；
    2. 剪掉 ``dirnames`` 里的重解析点，并把落在重解析点下的文件一并跳过 ——
       否则 junction 会被当成普通目录下钻，遍历会跑到授权根之外。

    第三道兜底在调用方：``grep_search`` / ``file_glob_search`` 对每个候选再做
    一次 ``resolve_inside``，即使今后这里漏了什么，结果也不会离开授权根。
    """
    root_resolved = root.resolve()
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in {".git", "node_modules", "__pycache__", ".venv", "venv"}
            and not _is_link_entry(Path(dirpath) / name)
        ]
        for name in filenames:
            path = Path(dirpath) / name
            if _is_link_entry(path):
                continue
            # walk 的 dirpath 本身可能已经穿过 junction，这里是最后一道确认。
            try:
                path.resolve().relative_to(root_resolved)
            except (OSError, ValueError, RuntimeError):
                continue
            seen += 1
            if seen > MAX_WALK_ENTRIES:
                return
            yield path


def read_file(root: Any, path: Any) -> dict[str, Any]:
    base = _normalize_root(root)
    target = resolve_inside(base, path)
    # A hard link inside the root can point at a file outside it; `resolve()` cannot see
    # that, and reads need no approval, so it is checked here.
    assert_single_link(base, target)
    text, truncated = _read_text(target)
    return {
        "tool": "read_file",
        "path": str(target.relative_to(base)).replace("\\", "/"),
        "content": text,
        "truncated": truncated,
        "bytes": len(text.encode("utf-8")),
    }


def _safe_relative(base: Path, path: Path) -> str | None:
    """``path`` 相对 ``base`` 的展示路径；落在根之外时返回 ``None``。

    ``_iter_files`` 已经过滤过链接，这里是**逐条**的兜底：遍历本身可能因为
    junction、竞态（遍历中途被替换成链接）或今后改动的疏漏而拿到根外路径。
    拿不到相对路径就丢弃该条目，绝不用绝对路径兜底 —— 那等于把越界文件
    的名字直接交给模型。
    """
    try:
        return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
    except (OSError, ValueError, RuntimeError):
        return None


def file_glob_search(root: Any, pattern: Any, subpath: Any = None) -> dict[str, Any]:
    base = _normalize_root(root)
    pat = str(pattern or "").strip()
    if not pat:
        raise ToolError("pattern must not be empty")
    if ".." in pat:
        raise ToolError("pattern must not contain '..'")
    start = resolve_inside(base, subpath) if subpath else base
    if not start.is_dir():
        raise ToolError("search path must be a directory")
    matches: list[str] = []
    for path in _iter_files(start):
        # Skip files with multiple hard links: their content may live outside the root.
        try:
            assert_single_link(base, path)
        except ToolError:
            continue
        relative = _safe_relative(base, path)
        if relative is None:
            continue
        if fnmatch.fnmatch(path.name, pat) or fnmatch.fnmatch(relative, pat):
            matches.append(relative)
            if len(matches) >= MAX_SEARCH_RESULTS:
                break
    return {
        "tool": "file_glob_search",
        "pattern": pat,
        "matches": sorted(matches),
        "truncated": len(matches) >= MAX_SEARCH_RESULTS,
    }


def grep_search(root: Any, query: Any, subpath: Any = None, pattern: Any = None) -> dict[str, Any]:
    base = _normalize_root(root)
    needle = str(query or "").strip()
    if not needle:
        raise ToolError("query must not be empty")
    if len(needle) > 500:
        raise ToolError("query is too long")
    start = resolve_inside(base, subpath) if subpath else base
    if not start.is_dir():
        raise ToolError("search path must be a directory")
    limit = int(MAX_SEARCH_RESULTS)
    hits: list[dict[str, Any]] = []
    # 结果必须以 `role="tool"` 落库，而 `chat_store.MAX_CONTENT_CHARS` 是
    # 200,000 字符。100 条 × 2000 字符 = 200,000 **还没算**前端渲染时加的
    # `path:line: ` 前缀 —— 实测 100 条 1999 字符的行渲染成 201,089 字符，
    # 于是"工具真的执行了、结果写不进库"。这里按累计字符数收口，
    # 保证渲染后的正文留有余量（前端前缀约 60 字符/条）。
    budget = MAX_RESULT_CHARS
    spent = 0
    for path in _iter_files(start):
        relative = _safe_relative(base, path)
        if relative is None:
            continue
        # A hardlinked file inside the root may hold content from outside it.
        try:
            assert_single_link(base, path)
        except ToolError:
            continue
        if pattern and not fnmatch.fnmatch(path.name, str(pattern)):
            continue
        try:
            with path.open("rb") as handle:
                chunk = handle.read(MAX_READ_BYTES)
        except OSError:
            continue
        if b"\x00" in chunk[:8192]:
            continue
        text = chunk.decode("utf-8", "replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if needle not in line:
                continue
            body = line[:MAX_LINE_CHARS]
            cost = len(body) + len(relative) + 16
            if spent + cost > budget:
                return {
                    "tool": "grep_search",
                    "query": needle,
                    "matches": hits,
                    "truncated": True,
                }
            spent += cost
            hits.append({"path": relative, "line": number, "text": body})
            if len(hits) >= limit:
                return {
                    "tool": "grep_search",
                    "query": needle,
                    "matches": hits,
                    "truncated": True,
                }
    return {"tool": "grep_search", "query": needle, "matches": hits, "truncated": False}


def execute_tool(name: Any, arguments: dict[str, Any] | None, root: Any) -> dict[str, Any]:
    """执行一个只读工具。未知工具一律拒绝（不猜、不降级）。"""
    tool = str(name or "").strip()
    if tool not in READ_ONLY_TOOLS:
        raise ToolError(f"tool {tool!r} is not allowed to run")
    args = arguments if isinstance(arguments, dict) else {}
    if tool == "read_file":
        return read_file(root, args.get("path"))
    if tool == "file_glob_search":
        return file_glob_search(root, args.get("pattern"), args.get("path"))
    return grep_search(root, args.get("query"), args.get("path"), args.get("pattern"))


# ---------------------------------------------------------------------------
# Path verdicts for callers that need to *describe* a call before running it.
#
# The agent workbench shows a resolved target in the approval card, which means it
# needs the sandbox's verdict as data rather than as an exception. These wrap the
# same primitives used above — one implementation of the boundary, two presentation
# shapes.
# ---------------------------------------------------------------------------


def path_verdict(root: Any, candidate: Any) -> dict[str, Any]:
    """Resolve a model-supplied path, returning a verdict instead of raising.

    ``{ok: True, path}`` or ``{ok: False, reason}``. `reason` is a short slug
    (`outside-root`, `unc-path`, …) so the UI can localize it.
    """
    try:
        base = _normalize_root(root)
    except ToolError as error:
        return {"ok": False, "reason": str(error)}
    raw = "" if candidate is None else str(candidate).strip()
    if not raw:
        return {"ok": False, "reason": "empty-path"}
    if raw.startswith(("\\\\", "//")):
        return {"ok": False, "reason": "unc-path"}
    if Path(raw).is_absolute() or (len(raw) > 1 and raw[1] == ":"):
        return {"ok": False, "reason": "absolute-path"}
    try:
        target = resolve_inside(base, raw)
    except ToolError as error:
        return {"ok": False, "reason": str(error)}
    try:
        relative = target.relative_to(base)
    except ValueError:
        return {"ok": False, "reason": "outside-root"}
    if not str(relative):
        return {"ok": False, "reason": "is-root"}
    return {"ok": True, "path": str(target)}


def search_root_verdict(root: Any, candidate: Any) -> dict[str, Any]:
    """Like `path_verdict`, but the root itself is a legal target.

    Search tools default to "the whole authorized folder", which is the common case,
    not an escape. Reading a directory, by contrast, is meaningless — hence two
    verdicts instead of one with a flag.
    """
    raw = "" if candidate is None else str(candidate).strip()
    if raw in ("", "."):
        try:
            return {"ok": True, "path": str(_normalize_root(root))}
        except ToolError as error:
            return {"ok": False, "reason": str(error)}
    return path_verdict(root, raw)
