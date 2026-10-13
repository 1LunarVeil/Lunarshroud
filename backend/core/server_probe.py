"""llama-server 能力探测：解析 ``--help`` / ``--version``。

为什么必须探测而不是写死
------------------------
llama.cpp 的参数集合、合法取值、默认值都在持续变化，而启动器此前把这些
契约**硬编码成了快照**（43 项参数写死在 ``param_schema`` 里）。后果很具体：
schema 里的 ``draft-ngram`` 不是合法取值，用户一选就是启动失败。

同理，``--help`` 才是"本机构建支持什么"的权威来源。实测本机版本号是
``0 (unknown)``（自编译构建不带版本），**靠版本号判断能力根本不可行** ——
只能读 ``--help`` 本身。

输出格式的坑（都来自真实样本，不是推测）
----------------------------------------
列 0 开头 = 新选项，缩进 = 上一项的续行（本机 246 个选项，0 例外）。
``default:`` 的排版有六种变体::

    (default: -1)
    (default: -1, -1 = infinity)              -> -1
    (default: 64, 0 = disabled, -1 = ctx_size)
    (default: 'auto')                         -> 去引号
    (default: same as --threads)              -> 非字面量
    (-1 = disabled, default: 30)              -> default 不在括号开头
    (default:\\n none)                         -> 值在下一行

所以解析前先把整个块折叠成单行、再找第一个 ``default:``，最后只取第一个
逗号前的片段。

这个模块只做解析与 IO，**不含任何业务判断**（谁该联动谁、缺什么参数）——
那些属于 ``capabilities`` 层。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import BACKEND_DIR

DEFAULT_TIMEOUT = 15.0

# 探测结果要"完整"到什么程度，才敢据此下"这个 flag 不存在"的结论。
# 真实构建实测解析出 401 / 411 个选项；若 `--help` 解析被截断（只剩几十个），
# 逐条判定"不支持"会把整条命令行清空 —— 那种情况下宁可照原样输出。
MIN_COMPLETE_OPTIONS = 100

# 短选项**可以是多字符**的（-ctk / -ctkd / -ngl / -ub / -fa / -np / -ngld）。
# 只匹配 `-[A-Za-z0-9]` 会把 `-ctk,` 截成 `-c`，剩余 `tk, ...` 让解析当场中止：
# 该选项建不出键，而截断出的 `-c`/`-t` 还会**覆盖**别的条目。
_LEADING_FLAG = re.compile(r"\s*,?\s*(--[A-Za-z0-9][A-Za-z0-9-]*|-[A-Za-z0-9][A-Za-z0-9-]*)")
# 逗号分隔的小写标识符序列，例如 none,draft-simple,draft-mtp
_VALUE_LIST = re.compile(r"[a-z0-9][a-z0-9-]*(?:,[a-z0-9][a-z0-9-]*)*,?")
_DEFAULT = re.compile(r"default:\s*([^)]*)")
_ALLOWED = re.compile(r"allowed values:\s*([^\n(]+)")


@dataclass(frozen=True)
class OptionInfo:
    """单个选项的信息。"""

    flags: tuple[str, ...]
    arg_hint: str | None
    default: str | None
    allowed_values: tuple[str, ...]
    description: str
    deprecated: bool

    @property
    def long_flags(self) -> tuple[str, ...]:
        return tuple(f for f in self.flags if f.startswith("--"))

    @property
    def short_flags(self) -> tuple[str, ...]:
        return tuple(f for f in self.flags if not f.startswith("--"))


@dataclass(frozen=True)
class ServerProbe:
    """一次探测的完整结果。"""

    path: str
    probed: bool
    version: str | None = None
    build: str | None = None
    options: Mapping[str, OptionInfo] = field(default_factory=dict)
    spec_types: tuple[str, ...] = ()
    error: str | None = None

    @property
    def complete(self) -> bool:
        """探测结果是否**完整到足以**据此判定"某个选项不存在"。

        ``supports()`` 返回 False 有两种可能：构建真的没有这个选项，或者
        ``--help`` 没解析全。只有这个属性为 True 时，前者才可信。
        """
        return self.probed and len(self.options) >= MIN_COMPLETE_OPTIONS

    def supports(self, flag: str) -> bool:
        """本机构建是否支持该选项（长名或短名皆可）。"""
        return flag in self.options

    def default_of(self, flag: str) -> str | None:
        """本机构建该选项的默认值字面量；拿不到就返回 None。

        **上游没声明就不许替它声明** —— 调用方必须把 None 当作"未知"，
        而不是回落到自己印象里的值。
        """
        info = self.options.get(flag)
        return info.default if info else None

    def allowed_of(self, flag: str) -> tuple[str, ...]:
        info = self.options.get(flag)
        return info.allowed_values if info else ()

    def unsupported(self, flags: list[str]) -> list[str]:
        """给定一组选项，挑出本机构建**不支持**的那些。

        这是"参数体系从快照变成自适应"最直接的收益：以前只能等用户启动失败
        才发现某个参数在这个构建里不存在。
        """
        return [f for f in flags if f.startswith("-") and f not in self.options]


# ========== 纯解析（不碰 IO，便于测试） ==========


def split_blocks(text: str) -> list[str]:
    """按"列 0 = 新选项"切块，返回每块的原始文本。"""
    blocks: list[list[str]] = []
    for raw in text.splitlines():
        if raw and not raw[0].isspace():
            blocks.append([raw])
        elif blocks:
            blocks[-1].append(raw)
    return ["\n".join(b) for b in blocks]


def split_flags(line: str) -> tuple[list[str], str]:
    """从定义行行首取出全部选项别名，返回 (flags, 剩余文本)。

    别名既可能是逗号分隔（``--spec-draft-type-k, -ctkd, --cache-type-k-draft``），
    也可能是空白分隔。遇到第一个不是选项的 token 即停止 ——
    后面的可能是参数占位符（``N`` / ``TYPE`` / ``[on|off|auto]``）或描述文字。
    """
    flags: list[str] = []
    rest = line
    while rest:
        match = _LEADING_FLAG.match(rest)
        if not match:
            break
        flags.append(match.group(1))
        rest = rest[match.end() :]
    return flags, rest.strip()


def extract_default(block_text: str) -> str | None:
    """从块文本里取默认值字面量；不是字面量就返回 ``None``。

    见模块文档里的六种排版变体。关键是**宁可返回 None 也不编造值**：
    调用方拿 None 时会退回"未知"，而编造出来的值会被当成事实参与判断。

    逗号在这里有两种完全相反的含义，必须区分：

    * **列表分隔**：``(default: ngram-mod,ngram-cache)`` —— 逗号两边都是值
    * **注解分隔**：``(default: 1.00, 1.0 = disabled)`` —— 逗号后是"某值表示某含义"

    判据是"这一段是不是干净的值"：含 ``=`` 或空格的就是注解，取值列表到此为止。
    等号左侧才是值的那种（``(default: 0 = loaded from model)``）只可能出现在
    **第一段**上，所以第一段单独按等号切一次。
    """
    flat = " ".join(block_text.split())
    match = _DEFAULT.search(flat)
    if not match:
        return None

    kept: list[str] = []
    for index, piece in enumerate(p.strip() for p in match.group(1).split(",")):
        if index == 0:
            head = piece.split("=", 1)[0].strip()
            if head and " " not in head:
                kept.append(head)
            continue
        if "=" in piece or " " in piece:
            break
        if piece:
            kept.append(piece)

    value = ",".join(kept).strip().strip("'\"")
    return value or None


def extract_allowed_values(block_text: str) -> tuple[str, ...]:
    match = _ALLOWED.search(block_text)
    if not match:
        return ()
    return tuple(v.strip() for v in match.group(1).split(",") if v.strip())


def extract_value_list(block_text: str, flag: str) -> tuple[str, ...]:
    """取出"取值直接写在参数位"的选项的合法取值（``--spec-type`` 就是这种）。

    不能只读第一行：不同构建的换行折行方式不同（取值可能独占一行、也可能被
    折成两三行），只读第一行会**静默漏掉一半取值**。这里逐行累积，并且要能
    分辨两种写法：

    * 取值独占一行 → 继续读下一行（真实 llama.cpp 就是这种，取值与描述分行）
    * 取值与描述同行（用 2+ 空格隔开）→ 读完这一行就结束

    判据是先用 ``\\s{2,}`` 切出"参数位"再整体匹配取值语法。反过来只做前缀
    匹配的话，描述里的 ``comma-separated`` 会被当成一个取值混进来。
    """
    values: list[str] = []
    for line in block_text.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith(flag):
            text = text[len(flag) :].strip()
        elif not values:
            # 取值还没开始，这行不是取值行
            continue
        if not text:
            continue
        parts = re.split(r"\s{2,}", text, maxsplit=1)
        head = parts[0].strip()
        if not _VALUE_LIST.fullmatch(head):
            # 出现空格或括号 → 已经是描述文字，取值结束
            break
        values.extend(part for part in head.rstrip(",").split(",") if part)
        if len(parts) > 1:
            # 取值与描述同行：取值到此为止
            break
    return tuple(values)


def parse_help(text: str) -> dict[str, OptionInfo]:
    """把 ``--help`` 全文解析成 ``{选项名: OptionInfo}``。

    同一个选项的所有别名都会作为 key 指向同一份信息。
    """
    options: dict[str, OptionInfo] = {}
    for block in split_blocks(text):
        first_line = block.splitlines()[0]
        flags, rest = split_flags(first_line)
        if not flags:
            continue

        # 参数占位符与描述之间是 2 个以上空格；没有这个间隔说明该选项不收参数，
        # 剩下的全是描述（例如 --cache-list   show list of models in cache）
        pieces = re.split(r"\s{2,}", rest, maxsplit=1)
        arg_hint = pieces[0].strip() if len(pieces) == 2 and pieces[0].strip() else None
        # 行内描述 + 续行描述，一起拼成人读说明
        inline_desc = pieces[1].strip() if len(pieces) == 2 else pieces[0].strip()
        continuation = "\n".join(block.splitlines()[1:]).strip()
        description = "\n".join(p for p in (inline_desc, continuation) if p).strip()

        info = OptionInfo(
            flags=tuple(flags),
            arg_hint=arg_hint,
            default=extract_default(block),
            allowed_values=extract_allowed_values(block),
            description=description,
            deprecated="DEPRECATED" in block,
        )
        for flag in flags:
            options[flag] = info
    return options


def parse_version(text: str) -> tuple[str | None, str | None]:
    """从 ``--version`` 输出取 (版本, 构建信息)。

    本机样本::

        version: 0 (unknown)
        built with MSVC 19.51.36248.0 for Windows AMD64
    """
    version = build = None
    for raw in text.splitlines():
        line = raw.strip()
        lowered = line.lower()
        if version is None and lowered.startswith("version:"):
            version = line.split(":", 1)[1].strip() or None
        elif build is None and lowered.startswith("built with"):
            build = line[len("built with") :].strip() or None
    return version, build


def parse_spec_types(help_text: str) -> tuple[str, ...]:
    """从 ``--help`` 全文里取出 ``--spec-type`` 的合法取值。

    这是本模块最有价值的一项输出：schema 曾硬编码过一个**非法**取值
    （``draft-ngram``，选了就启动失败），只有探测才能保证它不再漂移。
    """
    for block in split_blocks(help_text):
        flags, _ = split_flags(block.splitlines()[0])
        if "--spec-type" in flags:
            return extract_value_list(block, "--spec-type")
    return ()


def build_probe_from_text(path: str, help_text: str, version_text: str = "") -> ServerProbe:
    """纯函数：从 ``--help`` / ``--version`` 文本构建完整 ServerProbe。

    与 IO 分开是为了可测 —— 否则想测聚合逻辑，就得在机器上准备一个真的
    llama-server 可执行文件。
    """
    version, build = parse_version(version_text)
    return ServerProbe(
        path=path,
        probed=True,
        version=version,
        build=build,
        options=parse_help(help_text),
        spec_types=parse_spec_types(help_text),
    )


# ========== IO 与缓存 ==========

def system_tool(name: str) -> str:
    """System32 里某个工具的绝对路径；拿不到 SystemRoot 时退回裸名。

    系统工具**必须**用绝对路径调用。裸名会走 CreateProcess 的搜索序，而
    "应用程序所在目录"与"当前目录"都排在 System32 之前 —— 后端的 CWD 由外壳
    设定为 ``<安装目录>\\backend``（lib.rs:539），在便携安装与 NSIS 的
    CurrentUser 模式下都是用户可写的。实测：在该目录植入 taskkill.exe 后，
    ``subprocess.run(["taskkill", ...])`` 执行的是植入副本，不是 System32 的那份。
    """
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if not root:
        return name
    return os.path.join(root, "System32", name)


def which_on_path(name: str) -> str | None:
    """``shutil.which``，但**绝不**把当前目录算作命中。

    CPython 的 ``which()`` 在 Windows 上会把空 PATH 项与隐式的 CWD 都当作
    候选，命中时返回 ``.\\name`` 这种相对路径（实测如此）。那正是我们要关掉的
    可写目录劫持，所以自己走 PATH，并要求结果是绝对路径。
    """
    pathext = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory.strip():
            continue  # 空项在 Windows 上等于 CWD —— 跳过
        for ext in ("", *pathext):
            candidate = os.path.join(directory, name + ext)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
    return None


def resolve_server_path(configured: str | Path | None) -> str | None:
    """把配置里的 llama-server 路径解析成实际存在的文件路径；找不到返回 None。

    支持三种写法：绝对路径 / 启动器目录下的相对路径 / PATH 里的命令名。

    抽到这里是为了**只有一份实现** —— 能力探测与启动前校验必须对
    "llama-server 在哪"给出同一个答案，否则会出现"界面说探测不到、进程却能启动"
    这种自相矛盾的状态。

    相对名**只认应用目录（BACKEND_DIR），不认 CWD**。这不是洁癖：外壳把后端的
    CWD 设成 ``<安装目录>\\backend``（lib.rs:539），而便携安装与 NSIS 的
    CurrentUser 模式下那个目录都是用户可写的；默认配置又恰恰是裸文件名
    ``llama-server.exe``。交给 CWD 解析，等于让 main.py:180 的预热线程在
    **每次启动、零用户交互**下执行可写目录里的一个文件。
    """
    raw = str(configured or "").strip()
    if not raw:
        return None
    if os.path.isabs(raw):
        return raw if os.path.isfile(raw) else None
    candidate = BACKEND_DIR / raw
    if candidate.is_file():
        return str(candidate)
    return which_on_path(raw)

_CacheKey = tuple[str, int, int]
_CACHE: dict[_CacheKey, ServerProbe] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(exe_path: str | Path | None) -> _CacheKey | None:
    """缓存键 = (路径, mtime_ns, 文件大小)；拿不到就返回 None（不可缓存）。"""
    path = Path(exe_path) if exe_path else None
    if not path:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _run(exe: str, args: list[str], timeout: float) -> str:
    result = subprocess.run(  # noqa: S603 - 参数来自本地配置，非用户输入拼接
        [exe, *args],
        capture_output=True,
        timeout=timeout,
        check=False,
        # 子进程的 CWD 设成 exe 自己的目录：继承后端的 CWD 会让 DLL 搜索序里
        # 的"当前目录"那一级落在启动器安装目录（用户可写）上。
        cwd=os.path.dirname(exe) or None,
        # 打包态 backend.exe 是无控制台程序：不带这个 flag，每次冷探测
        # （--help / --version）都会给 llama-server 弹一个可见控制台窗口
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    # 不同构建把帮助写到 stdout 或 stderr 的都有，两边都收
    for stream in (result.stdout, result.stderr):
        if stream:
            text = stream.decode("utf-8", errors="replace")
            if text.strip():
                return text
    return ""


def probe_server(exe_path: str | Path, *, timeout: float = DEFAULT_TIMEOUT) -> ServerProbe:
    """探测指定 llama-server 的能力。**永不抛异常**。

    路径不存在、执行超时、输出为空 —— 一律返回 ``probed=False`` 并带上原因，
    调用方据此降级（例如退回 schema 里的静态取值），而不是让接口 500。
    """
    path = Path(exe_path) if exe_path else None
    if not path or not path.is_file():
        return ServerProbe(
            path=str(exe_path or ""),
            probed=False,
            error="llama-server 路径不存在或不是一个文件",
        )

    try:
        help_text = _run(str(path), ["--help"], timeout)
        version_text = _run(str(path), ["--version"], timeout)
    except subprocess.TimeoutExpired:
        return ServerProbe(path=str(path), probed=False, error=f"执行超时（{timeout:.0f}s）")
    except OSError as exc:
        return ServerProbe(path=str(path), probed=False, error=f"无法执行：{exc}")

    probe = build_probe_from_text(str(path), help_text, version_text)
    if not probe.options:
        return ServerProbe(path=str(path), probed=False, error="--help 输出为空或无法解析")
    return probe


def probe_server_cached(exe_path: str | Path, **options: Any) -> ServerProbe:
    """带缓存的探测。缓存键 = (路径, mtime_ns, 文件大小)。

    跑一次 ``--help`` 要几百毫秒，而它会被反复用到（每次打开模型配置页）。
    换 exe 后 mtime/size 变化，缓存自然失效。

    **会起子进程**（缓存冷时）。高频路径请用 ``probe_server_cached_only``。
    """
    key = _cache_key(exe_path)
    if key is None:
        return probe_server(exe_path, **options)

    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit

    probe = probe_server(exe_path, **options)
    if probe.probed:
        with _CACHE_LOCK:
            if len(_CACHE) >= 64:
                _CACHE.clear()
            _CACHE[key] = probe
    return probe


def probe_server_cached_only(exe_path: str | Path | None) -> ServerProbe | None:
    """**只读缓存**：命中返回结果，未命中返回 ``None``，绝不起子进程。

    存在的原因很具体：``build_argv`` 有一条高频路径（命令预览，前端每次输入
    都会调）。冷缓存时直接探测意味着每次预览都要跑一次 ``llama-server --help``，
    而一旦那个"exe"是不会自行退出的东西（测试替身、包装脚本、卡住的构建），
    每一次调用都要等到超时 —— 预览接口和真实启动会被一起拖死。

    所以高频路径只接受**已经存在的**探测结果，没有就由调用方降级。
    """
    key = _cache_key(exe_path)
    if key is None:
        return None
    with _CACHE_LOCK:
        return _CACHE.get(key)


def clear_server_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
