"""命令行构建器

将 params 字典转换为 llama-server 的 argv 列表。
处理布尔型/枚举型/路径型/反向 flag 等各种情况。

「默认值能否省略」由**本机探测**裁决
-----------------------------------
``skip_when_default`` 想表达"这个值的写法与 llama-server 自己的默认值一致，
不必显式传"。但它成立的前提是**两者真的相等** —— 不等时它就是一次静默覆盖：
命令行上少写一个参数，用户以为拿到的是上游默认值，实际拿到的是我们的值
（``flash_attn=on`` vs 上游 ``auto`` 正是这种）。

所以这里不再由 schema 单方面声明，而是问 ``capabilities.compare_param_default``
拿三态结论：

=============  ==========================  =========================
``agrees``     含义                        取舍
=============  ==========================  =========================
``True``       schema 默认 == 上游默认       相等时可以省略
``False``      我们有意覆盖上游              必须始终输出
``None``       上游没声明 / 本机构建不支持     保持显式输出（不为上游代言）
=============  ==========================  =========================

``build_argv`` 有一条**高频路径**（命令预览，前端每次输入都会调），因此它
**绝不**同步跑 ``llama-server --help`` —— 只查 ``probe_server_cached_only``
的缓存（带锁、按 exe 的 mtime/size 失效）。缓存里没有结果就**降级**成"按
``skip_when_default`` 标记处理"：宁可这一轮不裁决，也不能让预览和真实启动
卡住（llama-server 未必会为 ``--help`` 立即退出，包装脚本/卡住的构建都会
让每一次调用等到超时）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from core.capabilities import compare_param_default
from core.param_schema import PARAM_DEFINITIONS, ParamMeta, split_multi_value
from core.server_probe import ServerProbe, probe_server_cached_only

LOGGER = logging.getLogger(__name__)



SENSITIVE_PARAM_KEYS = frozenset({"api_key"})
SENSITIVE_CLI_FLAGS = frozenset({"--api-key"})
REDACTED_VALUE = "[REDACTED]"

# 两种写法都要盖住：`--api-key secret` 与 `--api-key=secret`
# （后者来自附加参数那种自由文本，很容易带出来）
_API_KEY_RE = re.compile(
    r"(?i)(--api-key[=\s]+)(?:\"(?:\\.|[^\"])*\"|\S+)"
)

# 启动器**自己**要据以做安全或身份判定的参数。extra_args 按声明顺序追加在
# 命令行末尾、因而能覆盖它们，而校验读的是 params[...] —— 两者一旦脱节，
# 校验通过而事实相反（实测 extra_args="--host 0.0.0.0" 即可绕过"非回环必须
# 配 api_key"）。所以这些 flag 不允许从自由文本里给。
SECURITY_CONTROLLED_PARAM_KEYS = frozenset({
    "host",       # 安全边界：非回环必须配 api_key
    "api_key",    # 上面那条边界的另一半
    "port",       # 实例身份：健康探测 / Chat iframe / 端口预留全读它
    "path",       # --path 让 llama-server 把任意目录当静态文件服务出去
    "log_file",   # --log-file 是任意路径写入
    # 下面这组是"面板值 ≠ 实际生效值"的身份/默认策略类参数。
    # 同一个 last-wins 机制：预检、_validate_start、历史记录、显存预估读的都是
    # params["model"]，而 extra_args 落在命令行末尾 —— 于是校验通过、界面显示 A、
    # 实际加载 B。cors_origins 更直接：本机构建的 --help 写着
    # ``--cors-origins ORIGINS (default: *)``，面板把它收紧成 localhost，
    # 末尾一条 `--cors-origins *` 就原样退回"任意站点可跨源读本地模型 API"。
    #
    # 这一组的边界取 ``config_resolver.MANAGED_KEYS``，不是自己另起一套：那份
    # 清单已经回答了"哪些键不许被模型作用域的默认值重定向"，而 extra_args 是
    # 同一条防线的最后一个缺口。mmproj / chat_template 刻意**不在**这里 ——
    # 它们不在 MANAGED_KEYS 里，且 test_extra_args 明确允许高级用户经附加参数
    # 挂多模态投影；要收紧得先改那份清单，而不是在这里单方面扩大黑名单。
    "model",
    "cors_origins",
})

# schema 只声明**启动器自己写出来**的那种拼写（model → `-m`），而 extra_args 是
# 自由文本、上游还认长别名。黑名单少了任何一个拼写都等于没拦。
# 逐条对照本机 llama-server --help 的选项名列（tmp 里的真实样本，不是凭印象）：
#   -m,   --model FNAME
#   -mu,  --model-url MODEL_URL
#   --cors-origins ORIGINS
# ``--model-url`` 一并拦：它同样让"实际加载的模型"偏离面板里那个已通过存在性
# 与 GGUF 校验的路径，还顺带把一次远程拉取伪装成本地启动。
_EXTRA_CLI_ALIASES: dict[str, tuple[str, ...]] = {
    "model": ("--model", "-mu", "--model-url"),
}


def security_controlled_flags() -> frozenset[str]:
    """受管控参数的全部 CLI 拼写（正向名 + 反向名 + 上游别名），从 schema 派生以免漂移。"""
    flags: set[str] = set()
    by_key = {meta.key: meta for meta in PARAM_DEFINITIONS}
    for key in SECURITY_CONTROLLED_PARAM_KEYS:
        meta = by_key.get(key)
        aliases = _EXTRA_CLI_ALIASES.get(key, ())
        if meta is None:
            flags.update(aliases)
            continue
        if meta.cli:
            flags.add(meta.cli)
            if meta.cli_reverse and meta.cli.startswith("--"):
                flags.add("--no-" + meta.cli[2:])
        flags.update(aliases)
    return frozenset(flags)


def find_forbidden_extra_args(tokens: list[str]) -> list[str]:
    """挑出 extra_args 里试图覆盖受管控 flag 的片段（`--x=v` 形式按 `--x` 判定）。"""
    controlled = security_controlled_flags()
    return [t for t in tokens if t.partition("=")[0] in controlled]


def effective_host_and_key(params: dict[str, Any], extra_tokens: list[str]) -> tuple[str, str]:
    """按**命令行实际顺序**求出最终生效的 host 与 api_key。

    真正的防线在这里：不看 params 声明了什么，只看 argv 上最后一次赋值是
    什么 —— llama-server 自己就是这么解析的。即便将来出现某个我们没列进
    黑名单的别名，只要它写的是 `--host`，这里就会看见。
    """
    host = str(params.get("host", "") or "").strip()
    key = str(params.get("api_key", "") or "")
    for token in extra_tokens:
        head, sep, tail = token.partition("=")
        if head == "--host":
            host = tail if sep else ""
        elif head == "--api-key":
            key = tail if sep else ""
    # `--host <value>` 的两段写法
    for index, token in enumerate(extra_tokens[:-1]):
        if token == "--host":
            host = extra_tokens[index + 1]
        elif token == "--api-key":
            key = extra_tokens[index + 1]
    return host, key


def scrub_sensitive_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return a persistence-safe copy with session secrets removed.

    按键名清空 ``api_key`` 还不够：密钥同样可能被写进 ``extra_args`` 这类
    自由文本（``--api-key=xxx``），而那份文本会原样进入 history.db 的
    params_json 与预设 JSON，并经 ``/api/presets/{id}/export`` 作为附件外发。
    所以对所有字符串值再跑一遍内容级脱敏。
    """
    scrubbed = dict(params)
    for key in SENSITIVE_PARAM_KEYS:
        if key in scrubbed:
            scrubbed[key] = ""
    for key, value in scrubbed.items():
        if key in SENSITIVE_PARAM_KEYS:
            continue
        if isinstance(value, str) and "--api-key" in value.lower():
            scrubbed[key] = redact_command_text(value)
    return scrubbed


def redact_argv(argv: list[str]) -> list[str]:
    """Mask secret CLI values before returning or logging a command."""
    redacted = list(argv)
    for index, value in enumerate(redacted[:-1]):
        if value in SENSITIVE_CLI_FLAGS:
            redacted[index + 1] = REDACTED_VALUE
    # `--api-key=xxx` 是**一个** token（自由文本参数里很常见），上面的相邻判断
    # 抓不到它 —— 密钥会原样出现在命令预览与日志里。
    for index, value in enumerate(redacted):
        flag, separator, _ = value.partition("=")
        if separator and flag in SENSITIVE_CLI_FLAGS:
            redacted[index] = f"{flag}={REDACTED_VALUE}"
    # MCP 服务器定义里几乎必然带凭据（`{"env":{"API_KEY":"sk-…"}}`、
    # `{"url":"https://…?token=…"}`）。这些 JSON 在 argv 里是**独立元素**，
    # 上面按 flag 相邻判断的规则完全不适用 —— 实测 `--mcp-servers-json` 的内容
    # 会原样穿过 redact_argv。Chat 页要展示实例的 MCP 配置，这一处不修就等于
    # 把密钥直接印在界面上（并随导出外发）。
    for index, value in enumerate(redacted):
        lowered = value.lower()
        if "--mcp-servers" not in lowered:
            continue
        if "=" in value:
            # `--mcp-servers-json={...}`
            redacted[index] = _redact_mcp_payload(value)
        elif index + 1 < len(redacted):
            # `--mcp-servers-json {...}`：JSON 是下一个 argv 元素。只对
            # `--mcp-servers-json` 这么做；`--mcp-servers-config` 的值是**路径**，
            # 里面不会有凭据，套正则只会白改一个文件名。
            if "json" in lowered:
                redacted[index + 1] = _MCP_SECRET_KEY_RE.sub(
                    r'\1"' + REDACTED_VALUE + '"', redacted[index + 1]
                )
    return redacted


# MCP 定义里可能装凭据的键名（大小写无关，覆盖 env 块与常见顶层写法）。
_MCP_SECRET_KEY_RE = re.compile(
    r'(?i)(["\']?(?:api[_-]?key|token|secret|password|passwd|authorization|'
    r'access[_-]?token|bearer|credentials?)["\']?\s*:\s*)("(?:[^"\\]|\\.)*"|[^,}\]]+)'
)


def _redact_mcp_payload(value: str) -> str:
    """把一个 `--mcp-servers-json <json>` 元素里的凭据替换成占位符。

    刻意用正则而不是 `json.loads` + 递归：这个值可能已经被截断或本身就不是
    合法 JSON（用户手填），解析失败时正则仍能盖住明显的 `"apiKey": "…"`。
    脱敏宁可多盖，也不能因为"解析不了"就原样放行。
    """
    flag, separator, payload = value.partition("=")
    # 注意必须是原始字符串：`"\1"` 在普通字符串里不是组引用，替换会静默失效
    # （实测踩过 —— 正则匹配到了、替换却没发生，密钥照原样输出）。
    replacement = r'\1"' + REDACTED_VALUE + '"'
    # 两种写法：`--mcp-servers-json={...}` 与 `--mcp-servers-json {...}`
    if separator:
        return f"{flag}={_MCP_SECRET_KEY_RE.sub(replacement, payload)}"
    space_index = value.find(" ")
    if space_index == -1:
        return value  # 只有 flag、没有值
    head, body = value[: space_index + 1], value[space_index + 1 :]
    return head + _MCP_SECRET_KEY_RE.sub(replacement, body)


def redact_command_text(command: str) -> str:
    """Mask supported secret flags in an already-rendered command string."""
    return _API_KEY_RE.sub(rf"\1{REDACTED_VALUE}", command)


def _is_empty(value: Any) -> bool:
    """判断值是否为空（None/空字符串/空列表/空字典）"""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (list, tuple, set, dict)) and len(value) == 0:
        return True
    return False


# 参数默认值表：用于"键缺失"时回退，见 _lookup 的说明
_DEFAULT_BY_KEY: dict[str, Any] = {p.key: p.default for p in PARAM_DEFINITIONS}


def _lookup(params: dict[str, Any], key: str) -> Any:
    """取依赖参数的当前值；键缺失时回退到 schema 默认值。

    前端判定联动用的是「合并了默认值的 currentValues」，而实际提交给后端的
    params 会先过滤掉空值 —— 两边并不是同一个字典。若不做回退，仅"键是否
    存在"就能让同一个参数在前后端得到相反的激活判定（--no-mmproj 即因此
    静默失效）。回退后，只有真实取值差异才会影响联动。
    """
    if key in params:
        return params[key]
    return _DEFAULT_BY_KEY.get(key)


def _cached_probe(llama_server_path: str) -> ServerProbe | None:
    """只**读缓存**的探测结果；没有就降级成"不可用"，绝不起子进程。

    起子进程在这里是被禁止的：``build_argv`` 有一条高频路径（命令预览，前端
    每次输入都会调），而 llama-server 未必会为 ``--help`` 立即退出（包装脚本、
    卡住的构建、测试替身都会让它等到超时）。宁可这一轮不做裁决，也不能让
    预览和真实启动一起卡住。
    """
    if not llama_server_path:
        return None
    try:
        return probe_server_cached_only(llama_server_path)
    except Exception as exc:  # noqa: BLE001 - 探测失败必须降级，不能改写命令行
        LOGGER.warning("llama-server 探测不可用，默认值省略判定降级为 schema 标记：%s", exc)
        return None


class DefaultGate:
    """裁决一个参数该不该出现在命令行上，每个参数只裁决一次。

    两个**相互独立**的维度，不要混为一谈：

    * :meth:`supported` —— 这个 flag 在本机构建里**存不存在**。
      不存在还写下去是 100% 的启动失败（实测 llama.cpp build 10960 删掉了
      ``--mlock`` / ``--mmap``，传进去就是 ``invalid argument``）。
    * :meth:`omit` —— 值等于默认值时**能不能省**。
      省掉的前提是"schema 默认值 == 上游默认值"，由三态裁决。

    读缓存是**惰性**的：只有真的需要裁决时才去查；缓存里没有结果就不补探，
    直接降级（探测不可用时两个维度都返回"别改现状"）。
    """

    def __init__(self, llama_server_path: str, probe: ServerProbe | None = None) -> None:
        self._path = llama_server_path
        self._probe = probe
        self._resolved = probe is not None
        self._cache: dict[str, bool] = {}
        self._support: dict[str, bool] = {}
        # 因"本机构建不支持"而被丢弃的参数（供调用方提示，见 suppressed_params）
        self.dropped: list[str] = []

    def is_openvino(self) -> bool:
        """这份构建是不是 OpenVINO 后端（决定设备怎么传给后端）。

        判据是同级的 ``ggml-openvino.dll``：OpenVINO 后端不认 ``--device``，
        设备只经 ``GGML_OPENVINO_DEVICE`` 环境变量传入（实测 b11929）。
        """
        try:
            from pathlib import Path as _Path

            return (_Path(self._path).parent / "ggml-openvino.dll").is_file()
        except (OSError, TypeError, ValueError):
            return False

    @property
    def probe(self) -> ServerProbe | None:
        if not self._resolved:
            self._resolved = True
            self._probe = _cached_probe(self._path)
        return self._probe

    @property
    def probes_available(self) -> bool:
        """探测是否可用；不可用时全部判定走降级分支。"""
        probe = self.probe
        return bool(probe and probe.probed)

    def knows_flag(self, flag: str) -> bool:
        """**确实**知道本机构建有这个 flag（探测不可用/不完整时一律 False）。

        与 :meth:`supported` 的默认方向**正好相反**，因为用途不同：
        那个是"没依据就放行"（保持原有输出）；这个是"没依据就不敢写"
        —— 用于补一个上游反向名这种**新增**输出，猜错就是启动失败。
        """
        probe = self.probe
        if not probe or not probe.complete:
            return False
        return probe.supports(flag)

    def supported(self, meta: ParamMeta) -> bool:
        """本机构建是否认这个 flag。

        llama-server 的路径由用户在设置里自由指定，构建代次不可控（实测两个
        构建之间就有 flag 被删除），所以"这个参数存在吗"必须每次按当前构建判定。

        探测不可用时返回 ``True`` —— 没有依据就别拦，退回到改动前的行为。
        """
        cli = meta.cli
        if not cli:
            return True
        if cli in self._support:
            return self._support[cli]
        if not self.probes_available:
            self._support[cli] = True
            return True

        probe = self.probe
        if not probe.complete:
            # 探测结果不完整 → 没有依据说"不支持"，一律放行
            self._support[cli] = True
            return True

        ok = probe.supports(cli)
        if not ok and meta.cli_reverse and cli.startswith("--"):
            # 反向 flag 真正写到命令行上的是 --no-xxx（--mmap → --no-mmap）
            ok = probe.supports("--no-" + cli[2:])
        self._support[cli] = ok
        return ok

    def silence_is_safe(self, meta: ParamMeta, value: Any) -> bool:
        """开关类参数**不写任何东西**时，上游默认行为是否就等于用户想要的？

        switch 分支有两种"沉默"：``cli_no_value`` 且 value=False（不传 flag），
        ``cli_reverse`` 且 value=True（不传 --no-xxx）。沉默 = 让上游默认值生效，
        可我们从来没验证过上游默认值是什么。

        实测反例：``--jinja`` 的上游默认是 **enabled**，而 schema 默认值也是 True，
        于是用户把「启用 Jinja」关掉时**什么都不写** → 上游仍然是开的 → 开关失效。
        修法是显式写 ``--no-jinja``（该构建有这个反向名）。

        真机反例（2026-09-18 启动实测）：`--kv-unified` 的 `--help` 只写
        "default: enabled if number of slots is auto"，我们无法从中判定行为，
        于是保持沉默 —— 而实际因为总是显式传 `-np 1`（slots 非 auto），
        llama-server 打印的是 `kv_unified = 'false'`，与界面显示的"开"不符。

        所以规则是：**上游没声明时，只要能显式表达意图就显式写**；
        实在表达不了（没有反向名可用）才沉默，并降级为 debug 日志。
        """
        probe = self.probe
        if not probe or not probe.complete:
            # 总原则：没有探测依据时一律降级成改动前的行为（沉默）。
            # 探测不可用不是"上游没声明"，而是"我们什么都不知道"。
            return True
        if not self.supported(meta):
            # 本机构建根本没这个 flag → 谈不上"显式表达"，沉默即可
            # （"被丢弃"由 _emit 记帐，不在这里重复报）
            return True

        want = bool(value)
        if meta.cli and meta.cli.startswith("--no-"):
            want = not want  # 否定式 flag：不传它才是"关"
        upstream = self.upstream_behavior(meta)
        if upstream is None:
            return False  # 交调用方尝试显式表达；表达不了它自己会退回沉默
        return upstream == want

    def upstream_behavior(self, meta: ParamMeta) -> bool | None:
        """上游对该开关声明的**行为**默认值；没声明就返回 None。"""
        if not self.probes_available or not meta.cli:
            return None
        info = self.probe.options.get(meta.cli)
        if info is None or info.default is None:
            return None
        word = str(info.default).strip().lower()
        if word == "enabled":
            return True
        if word == "disabled":
            return False
        return None

    def omit(self, meta: ParamMeta, value: Any) -> bool:
        """返回 True = 该参数此时可以不写进命令行。"""
        # 没标 skip_when_default（怎么改都要写）或用户改过值（写）→ 不必裁决
        if not meta.skip_when_default or value != meta.default:
            return False
        if meta.key in self._cache:
            return self._cache[meta.key]

        if not self.probes_available:
            # 降级：没有探测依据时，退回改动前的行为（信 schema 的标记）
            decision = True
        else:
            agrees = compare_param_default(meta, self.probe)["agrees"]
            # 只有"确实证明两者相等"才可省；有意覆盖（False）与未知（None）都照写
            decision = agrees is True
        self._cache[meta.key] = decision
        return decision


def _is_param_active(meta: ParamMeta, params: dict[str, Any]) -> bool:
    """检查参数是否处于激活状态（满足 depends_on 联动条件）。

    未激活的参数不输出到命令行。
    """
    # depends_on: 要求某个参数等于特定值
    if meta.depends_on:
        for dep_key, dep_val in meta.depends_on.items():
            if _lookup(params, dep_key) != dep_val:
                return False
    # depends_on_any: 多值参数要求命中给定集合中的任意一个值
    if meta.depends_on_any:
        for dep_key, allowed in meta.depends_on_any.items():
            selected = split_multi_value(_lookup(params, dep_key))
            if not set(selected) & set(allowed):
                return False
    # depends_on_nonempty: 要求某个参数非空且非 none
    if meta.depends_on_nonempty:
        dep_key = meta.depends_on_nonempty
        dep_val = _lookup(params, dep_key)
        if dep_key == "rope_scaling":
            # 特殊处理：rope_scaling 的"非空"指非 "none"
            if dep_val == "none" or _is_empty(dep_val):
                return False
        elif _is_empty(dep_val):
            return False
    return True


def _prefer_load_mode(
    params: dict[str, Any], probe: ServerProbe | None
) -> tuple[dict[str, Any], frozenset[str]]:
    """Translate the deprecated --mmap switch on builds that have --load-mode.

    Newer llama.cpp prints ``DEPRECATED: --mmap ... use --load-mode``. When the
    selected build supports ``--load-mode`` and the user left load_mode=auto,
    the default (mmap=true, mlock=false) becomes ``--load-mode mmap`` instead.
    Non-default mmap/mlock combinations are left untouched so this is a narrow
    compatibility rewrite, not a second interpretation layer.

    Returns the rewritten params plus keys that must be skipped entirely. A
    key cannot simply be deleted: ``_build_argv`` falls back to ``meta.default``
    for missing keys, which would re-emit ``--mmap``.
    """
    if probe is None or not probe.complete or not probe.supports("--load-mode"):
        return params, frozenset()
    if "mmap" not in params and "mlock" not in params:
        return params, frozenset()
    mode = str(params.get("load_mode") or "auto")
    if mode != "auto":
        # An explicit load_mode wins; legacy switches would contradict it.
        return params, frozenset({"mmap", "mlock"})
    mmap = bool(params.get("mmap", True))
    mlock = bool(params.get("mlock", False))
    if mmap and not mlock:
        rewritten = dict(params)
        rewritten["load_mode"] = "mmap"
        return rewritten, frozenset({"mmap", "mlock"})
    return params, frozenset()


def build_argv(
    params: dict[str, Any],
    llama_server_path: str,
    probe: ServerProbe | None = None,
) -> list[str]:
    """将 params 字典转换为 llama-server 命令行 argv 列表。

    Args:
        params: 参数字典（符合 param_schema 定义）
        llama_server_path: llama-server.exe 的完整路径
        probe: 已有的探测结果（可选）。不给 → 惰性读 ``probe_server_cached_only``
            的缓存（**不起子进程**；没有缓存就降级）

    Returns:
        argv 列表，可直接传给 subprocess.Popen
    """
    effective_probe = probe if probe is not None else _cached_probe(llama_server_path)
    params, skip_keys = _prefer_load_mode(params, effective_probe)
    return _build_argv(
        params,
        llama_server_path,
        DefaultGate(llama_server_path, probe),
        skip_keys=skip_keys,
    )


def suppressed_params(
    params: dict[str, Any],
    llama_server_path: str,
    probe: ServerProbe | None = None,
) -> list[str]:
    """返回"本该写进命令行、却因本机构建不支持而被丢弃"的参数 key。

    ``build_argv`` 必须丢弃它们（写下去必然启动失败），但**不能静默** ——
    否则会出现"UI 里勾了锁定内存、命令行里没有、也不报错"，功能悄悄消失。
    命令预览与启动前都应当把这个列表展示出来。
    """
    gate = DefaultGate(llama_server_path, probe)
    _build_argv(params, llama_server_path, gate)
    return list(gate.dropped)


def _emit(argv: list[str], gate: DefaultGate, meta: ParamMeta, *tokens: str) -> None:
    """把一个参数写进 argv —— 唯一的输出口。

    本机构建不认这个 flag 时**丢弃并记帐**：用户在设置里可以指向任意
    llama-server，构建代次之间会有 flag 增删（实测 build 10960 删掉了
    ``--mlock`` 与 ``--mmap``），写下去必然 ``invalid argument``。
    丢弃必须留痕，见 :func:`suppressed_params`。
    """
    # OpenVINO 的 `--device` 由启动器翻译成 GGML_OPENVINO_DEVICE 环境变量
    # （该后端不认 -dev，实测传了会直接报错退出）。写进 argv 必然启动失败，
    # 所以这里静默跳过；翻译与校验在 ProcessManager._launch_process 之前完成，
    # 用户仍然会看到"设备不存在"之类的明确错误。**不记入 dropped**：它不是
    # "本机构建不支持"，而是"换了一条传递通道"。
    if meta.key == "device" and gate.is_openvino():
        return
    if gate.supported(meta):
        argv.extend(tokens)
    else:
        gate.dropped.append(meta.key)


def split_cli_snippet(text: str) -> list[str]:
    """把用户手填的一行附加参数拆成 argv 片段。

    只做**引号感知的空白拆分**，不解释反斜杠转义 —— Windows 路径里的反斜杠是
    字面量，用 ``shlex`` 的 POSIX 模式会把 ``"C:\\models\\a.gguf"`` 里的反斜杠
    当转义符吃掉，路径就坏了。

    未闭合的引号**必须报错**：静默丢掉一半参数会让用户拿着一条看着正常、
    实际残缺的命令去启动，比直接报错难查得多。
    """
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
            else:
                current.append(char)
        elif char in ("\"", "'"):
            quote = char
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if quote is not None:
        raise ValueError(f"附加参数里的引号没有闭合：{text}")
    if current:
        tokens.append("".join(current))
    return tokens


def _ensure_opposite(argv: list[str], gate: DefaultGate, meta: ParamMeta) -> None:
    """开关类参数"沉默不安全"时，尽量显式写出反向名（--jinja → --no-jinja）。

    构建没提供反向名时**不硬凑**：既不能用带值形式（`--jinja off` 不一定被接受），
    也不能就此放弃 —— 记一条警告，让"这个开关在当前构建上关不掉"这件事可被追查。
    """
    cli = meta.cli or ""
    # 用 debug 而不是 warning：命令预览是高频路径，而"这个开关没有反向名"
    # 是既有事实、不是每次都要喊一遍的故障。
    if not cli.startswith("--"):
        LOGGER.debug("开关 %s 无法显式关闭（无反向名），保持沉默", meta.key)
        return
    reverse = "--no-" + cli[2:]
    if not gate.knows_flag(reverse):
        LOGGER.debug("开关 %s 无法显式关闭：本机构建没有 %s（或探测不可用）", meta.key, reverse)
        return
    argv.extend([reverse])


def _build_argv(
    params: dict[str, Any],
    llama_server_path: str,
    gate: DefaultGate,
    *,
    skip_keys: frozenset[str] = frozenset(),
) -> list[str]:
    argv: list[str] = [llama_server_path]

    # 按 schema 顺序处理每个参数
    for meta in PARAM_DEFINITIONS:
        if meta.key in skip_keys:
            continue
        # 原样追加的自由文本参数（如 extra_args）：按声明顺序落在命令行末尾，
        # 因此用户手填的 flag 可以覆盖前面面板写出来的值。
        if meta.append_raw:
            snippet = params.get(meta.key, meta.default)
            if isinstance(snippet, str) and snippet.strip():
                argv.extend(split_cli_snippet(snippet))
            continue

        # 跳过非 CLI 参数（如 model_dir 仅用于扫描）
        if meta.cli is None:
            continue

        # 跳过未激活参数（联动条件不满足）
        if not _is_param_active(meta, params):
            continue

        value = params.get(meta.key, meta.default)
        cli = meta.cli

        # 多值参数（如 --spec-type）：列表拼成逗号分隔的单个值；未选任何值即不输出
        if meta.multiple:
            selected = split_multi_value(value)
            if not selected:
                continue
            _emit(argv, gate, meta, cli, ",".join(selected))
            continue

        # 布尔型参数处理
        if meta.type == "switch":
            # 预设/导入路径可能带来整型布尔（1/0）。开关分支用身份比较
            # （is True / is False），不归一化的话同一个"开"会因 1 与 True
            # 之差被静默丢弃，甚至被"沉默不安全"分支反向输出成 --no-xxx。
            # bool 是 int 的子类，必须先排除再转换。
            if isinstance(value, int) and not isinstance(value, bool):
                value = bool(value)
            if meta.cli_no_value:
                # 无值 flag：True 时输出 flag
                if value is True:
                    _emit(argv, gate, meta, cli)
                elif not gate.silence_is_safe(meta, value):
                    # 上游默认是"开"而我们想要"关" —— 沉默等于关不掉，必须显式写反向名
                    _ensure_opposite(argv, gate, meta)
            elif meta.cli_reverse:
                # 反向 flag：False 时输出 --no-xxx
                if not cli.startswith("--"):
                    # 短参数不支持反向，跳过
                    continue
                if value is False:
                    _emit(argv, gate, meta, "--no-" + cli[2:])
                elif not gate.silence_is_safe(meta, value):
                    # 上游默认是"关"而我们想要"开" —— 显式写出正向名
                    _emit(argv, gate, meta, cli)
            else:
                # 普通布尔带值：--flag on/off（总是显式，不涉及沉默）
                if value is True:
                    _emit(argv, gate, meta, cli, "on")
                elif value is False:
                    _emit(argv, gate, meta, cli, "off")
            continue

        # 空值跳过
        if _is_empty(value):
            continue

        # 哨兵取值：值落在 schema 声明的 omit_values 里就不写（如 n_gpu_layers=-1 表示
        # "交给上游判断"、rope_scaling=none 表示"明确不缩放"）。声明式，代码里没有
        # 按参数名的 if —— 新增此类参数只需在 schema 上标注。
        if meta.omit_values and any(str(value) == str(item) for item in meta.omit_values):
            continue

        # 取值与 schema 默认值相同 → 交给 DefaultGate 裁决（三态，见模块文档）。
        # seed / rope_scale / yarn_orig_ctx 原先各有一条硬编码 if，已统一到这里：
        # 它们只需在 schema 上标 skip_when_default，能不能省略由本机探测说了算。
        if gate.omit(meta, value):
            continue

        # 通用处理：输出 [cli, str(value)]
        _emit(argv, gate, meta, cli, str(value))

    return argv


def allowed_option_values(meta: ParamMeta, probe: ServerProbe | None = None) -> set[str]:
    """select 的合法取值：schema 快照 ∪ **本机构建声明的取值**。

    前端下拉框已经把上游取值并进选项（``mergeUpstreamOptions``，数据来自
    ``upstream_options_for``），校验却只认 ``meta.options`` 的话，用户能在界面里
    选到一个值、点启动才被 400 拒掉。上游才是新增取值的一方（实测 build 10960
    的 ``--spec-type`` 多了 ``draft-dspark``），所以以本机构建为准。

    放宽的方向是安全的：多接受的只有"这份构建自己声明的取值"，与
    :meth:`DefaultGate.supported` 同一个信任源；探测不可用（或不完整）时退回
    快照，绝不"猜着放行"。
    """
    allowed = {str(option) for option in (meta.options or [])}
    if probe is None or not probe.complete or not meta.cli:
        return allowed
    info = probe.options.get(meta.cli)
    if info is not None:
        allowed.update(info.allowed_values)
    if meta.multiple and meta.cli == "--spec-type" and probe.spec_types:
        # 与 upstream_options_for 同一口径：取值写在参数位，且 none 不是可选项
        allowed.update(v for v in probe.spec_types if v != "none")
    return allowed


def validate_param_values(
    params: dict[str, Any], probe: ServerProbe | None = None
) -> None:
    """按 schema 的 min/max/options 校验取值；不合法抛 ValueError。

    为什么必须在这里做：ParamMeta 声明了 min/max/options，但 _build_argv 只是
    str(value) 直接 emit，约束此前只存在于前端 HTML 属性上。直连 API 或导入
    外部预设就能把 -c 1000000000000 / --top-k -5 / -fa banana 送上命令行，
    用户拿到的是 llama-server 的莫名崩溃，而不是一条说明白哪儿错了的 400。

    错误信息用英文（与 _validate_start 一致，前端 localizeBackendError 会本地化）。

    ``probe`` 用于 select 取值：给了就以本机构建声明的取值为准（见
    :func:`allowed_option_values`），不给就只认 schema 快照。调用方应当传
    ``probe_server_cached_only(path)`` —— 校验在命令预览路径上，**不许**因此
    起子进程跑 ``--help``。
    """
    for meta in PARAM_DEFINITIONS:
        # extra_args 是唯一的"原样追加"特例，它有自己的引号校验，不走范围检查
        if meta.append_raw:
            continue
        if meta.key not in params:
            continue  # 未提供 = 用 schema 默认值，默认值定义上就是合法的

        value = params[meta.key]
        if _is_empty(value):
            continue
        # 联动条件不满足的参数不会被 emit，也就不该拿它挡住启动
        # （导入的旧预设里常带着当前已失效的字段）
        if not _is_param_active(meta, params):
            continue
        # 哨兵值（如 n_gpu_layers=-1 表示"交给上游"、rope_scaling=none）
        # 刻意在声明范围之外，必须放行
        if meta.omit_values and any(str(value) == str(item) for item in meta.omit_values):
            continue

        if meta.type == "switch":
            # 与 _build_argv 的开关分支同一套归一化：预设/导入可能带来 1/0。
            # bool 是 int 的子类，必须先排除再转换。
            if isinstance(value, int) and not isinstance(value, bool):
                value = bool(value)
            if not isinstance(value, bool):
                raise ValueError(f"{meta.key} must be a boolean, got: {value!r}")
            continue

        values = split_multi_value(value) if meta.multiple else [value]

        if meta.type in ("number", "float"):
            for item in values:
                try:
                    number = float(item)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{meta.key} must be a number, got: {item!r}") from exc
                if meta.min is not None and number < float(meta.min):
                    raise ValueError(f"{meta.key} is below the minimum {meta.min}: {number}")
                if meta.max is not None and number > float(meta.max):
                    raise ValueError(f"{meta.key} exceeds the maximum {meta.max}: {number}")

        if meta.options:
            allowed = allowed_option_values(meta, probe)
            for item in values:
                if str(item) not in allowed:
                    raise ValueError(
                        f"{meta.key} must be one of {sorted(allowed)}, got: {item!r}"
                    )





def _quote_arg(arg: str) -> str:
    """值含空格或引号时加引号（Windows 风格，内部引号用 \\\" 转义）。"""
    if " " in arg or "\"" in arg:
        escaped = arg.replace("\"", "\\\"")
        return f'"{escaped}"'
    return arg


def build_command_string(argv: list[str]) -> str:
    """将 argv 列表转为单行可读命令行字符串。

    单行形式可直接复制进 cmd.exe、PowerShell 或其他终端执行。
    （此前的多行 "^" 续行写法只对 cmd.exe 有效，复制到 PowerShell
    会因为 "^" 不是续行符而语法错误。）
    """
    argv = redact_argv(argv)
    if not argv:
        return ""
    return " ".join(_quote_arg(arg) for arg in argv)
