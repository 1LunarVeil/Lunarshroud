"""Chat 代理的请求整形与上游连接（与路由层解耦）。

从 ``main.py`` 抽出的原因：这几块逻辑是**纯函数 + 一次 HTTP 建连**，不依赖
FastAPI 的 app/router，也不依赖任何模块级全局（会话令牌、端口、DATA_DIR）。
把它们留在 2532 行的 main.py 里，任何一次改动都要在大文件里定位；抽出来之后
``main.py`` 只保留路由声明，逻辑可以单独测试。

边界（刻意不搬）：
- 路由函数（``@app.post(...)``）留在 ``main.py``：它们是 API 面，搬走会让
  ``test_auth_coverage.py`` 的穷举失去意义，也让"哪些端点暴露"不再一处可见。
- 知识库检索注入（``_inject_knowledge_context``）留在 main：它依赖
  ``knowledge_store``/``knowledge_retrieval`` 两个 store 的装配顺序。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

import httpx
from fastapi import HTTPException

# 单次对话的形状上限。这些是**传输层/内存**护栏，与模型的上下文窗口无关
# （后者由 chat_context 按 token 计量并在转发前裁剪）。
CHAT_MAX_MESSAGES = 500
CHAT_MAX_MESSAGE_CHARS = 200_000
CHAT_MAX_TOTAL_CHARS = 2_000_000
# 一条消息最多带几张图、单张 data URL 多大。与 chat_store 的附件额度同量级，
# 但这里更紧：代理路径的每一字节都会进内存并转发给 llama-server，而存储路径
# 只是落盘。4 张 × 8 MB 是"能看图"与"不把后端撑爆"之间的折中。
CHAT_MAX_IMAGES_PER_MESSAGE = 4
CHAT_MAX_IMAGE_CHARS = 8 * 1024 * 1024
CHAT_MAX_TOTAL_IMAGE_CHARS = 32 * 1024 * 1024

# 只接受这几种图片 data URL。不信任客户端声明 —— 这里拦的是"把任意
# ``data:text/html`` 塞进请求体再原样转发给 llama-server"。
_CHAT_IMAGE_RE = re.compile(
    r"^data:image/(?:png|jpeg|jpg|webp|gif|bmp);base64,[A-Za-z0-9+/=\s]+$"
)

# 允许透传给 llama-server 的 OpenAI 兼容字段白名单。
#
# ``tools`` / ``tool_choice`` 在这里，但**执行**不在这里：代理只负责把工具声明
# 交给模型、把模型返回的 ``tool_calls`` 交回界面。真正的"允许执行"由人在界面上
# 决定（见 frontend/js/chat.js 的批准流程）。这条边界是刻意的：
#
#   llama-server 自己的 ``--tools read_file,grep_search`` 是在**宿主机上直接
#   执行**内置工具、没有批准环节（实测 b11929 的 --help 只有 ``--tools`` 与
#   ``--tools-runtime``，没有"先问用户"的开关）。把它照搬进启动器等于给模型
#   一个静默读了 %USERPROFILE% 就能把内容发出去的能力 —— 那与本项目"能力不是
#   越权入口"（AGENTS §2.3）的基调相反。
#
# ``image_url`` 是消息**内容块**的一种、不是顶层字段，它的放行在
# ``_normalize_content`` 里。
CHAT_ALLOWED_FIELDS = frozenset({
    "model",
    "messages",
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "seed",
    "stream",
    "stream_options",
    "presence_penalty",
    "frequency_penalty",
    "repeat_penalty",
    "chat_template_kwargs",
    "response_format",
    "reasoning_effort",
    "timings_per_token",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
})

# 一条工具声明的上限。工具是**声明**不是数据，正常也就几 KB；给到 256 KB 是
# 为了让"带完整 JSON Schema 的复杂工具"能用，同时挡住"把整个知识库塞进
# 工具描述"这种玩法（那会把每次请求都撑成 MB 级，且 token 预算无从预估）。
CHAT_MAX_TOOLS = 64
CHAT_MAX_TOOLS_BYTES = 256 * 1024
# 历史消息里单条工具调用的参数上限。模型生成的参数可能很长（整段文件内容），
# 但它是**已经发生过的事实**，不能像工具声明那样直接拒绝整条请求 ——
# 超长时截断，让对话继续。
#
# **必须远小于 chat_store.MAX_STATS_BYTES（16 KiB）。**
# 2026-11-02 实测缺陷：这里曾是 64 KiB，而前端把 `stats.tool_calls` 塞进
# stats 持久化；一条 64 KiB 参数的调用算出 68,190 bytes > 16,384，于是
# `append_message` 抛 `stats is too large`，**整条 assistant 消息不落库**
# （用户看到的是回复消失）。4 KiB 让 3 条并行调用也放得进 16 KiB 预算。
CHAT_MAX_TOOL_ARGS_CHARS = 4 * 1024
# tool_choice 的合法形状。
_TOOL_CHOICE_MODES = frozenset({"auto", "none", "required"})

# 采样数值字段的类型/有限性守卫。这些键以前只是被白名单"放行"，值本身不做
# 任何检查 —— 于是 `{"temperature": "hot"}`、`{"max_tokens": 1e999}`、
# `{"seed": {}}` 会一路走到 `int()` 或上游请求构造，在那里抛
# `OverflowError` / `TypeError`，表现成 500。注意 JSON 里的 `1e999` 会被
# `json.loads` 解析成 `inf`，所以这不需要构造畸形字节就能触发。
_NUMERIC_FIELDS = frozenset({
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "max_completion_tokens",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "repeat_penalty",
})

# 工具调用结果的回填消息的角色。OpenAI 里是 "tool"，模型据此知道"这是我刚才
# 那次调用的结果"。它不在原本的 user/assistant/system 三元组里 —— 支持它是因为
# 回填结果没有别的合法通道。
_TOOL_ROLES = frozenset({"tool"})


def _normalize_content(
    content: Any,
    role: str,
    budget: dict[str, int],
) -> tuple[Any, int]:
    """校验一条消息的 content，返回 ``(归一化后的 content, 计入的字符数)``。

    支持两种形状：

    * **字符串** —— 纯文本，历史行为。
    * **内容块数组** —— ``[{"type":"text","text":...}, {"type":"image_url",...}]``。
      这是 OpenAI 的多模态形状，前端 ``buildMessagePayloadContent`` 在有图片
      附件时产出的就是它。

    以前这里只接受字符串，于是**带图片的对话一发就 400**（"message content
    must be a string"）。这个缺陷长期被"缺少视觉模型所以没法验证"掩盖：
    没有带 mmproj 的模型时前端根本不会构造数组，测试也就碰不到。

    图片走**独立预算**：一张 4 MB 的图 base64 后约 5.3M 字符，用文本额度
    （200k）衡量会直接把合法请求拒掉；反过来把文本额度放宽到图片量级，又会
    让一个巨大日志附件挤爆内存。两条通道量级差一个数量级，必须分开。
    """
    if isinstance(content, str):
        if len(content) > CHAT_MAX_MESSAGE_CHARS:
            raise HTTPException(400, "message content is too long")
        return content, len(content)
    if not isinstance(content, list):
        raise HTTPException(400, "message content must be a string or a list of parts")

    parts: list[dict[str, Any]] = []
    text_chars = 0
    images = 0
    for part in content:
        if not isinstance(part, dict):
            raise HTTPException(400, "each content part must be an object")
        kind = part.get("type")
        if kind == "text":
            text = part.get("text")
            if not isinstance(text, str):
                raise HTTPException(400, "text part must carry a string 'text'")
            text_chars += len(text)
            if text_chars > CHAT_MAX_MESSAGE_CHARS:
                raise HTTPException(400, "message content is too long")
            parts.append({"type": "text", "text": text})
            continue
        if kind == "image_url":
            # 只有用户消息能带图。让 assistant 也能带图既没有语义，也给了
            # 一条"回填任意 data URL"的通道。
            if role != "user":
                raise HTTPException(400, "only user messages can carry images")
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else None
            if not isinstance(url, str):
                raise HTTPException(400, "image_url part must carry a url string")
            if not _CHAT_IMAGE_RE.match(url):
                raise HTTPException(400, "image_url must be a data:image URL")
            if len(url) > CHAT_MAX_IMAGE_CHARS:
                raise HTTPException(400, "image is too large")
            images += 1
            if images > CHAT_MAX_IMAGES_PER_MESSAGE:
                raise HTTPException(400, "too many images in one message")
            budget["image_chars"] += len(url)
            if budget["image_chars"] > CHAT_MAX_TOTAL_IMAGE_CHARS:
                raise HTTPException(400, "images in this conversation are too large")
            parts.append({"type": "image_url", "image_url": {"url": url}})
            continue
        raise HTTPException(400, f"unsupported content part type: {kind!r}")

    if not parts:
        raise HTTPException(400, "message content must not be empty")
    return parts, text_chars


def _normalize_tools(value: Any) -> list[dict[str, Any]]:
    """校验工具声明。只做形状与体积约束，不理解具体工具。

    工具是通过 chat template 交给模型的**声明**，执行在客户端（人批准之后）。
    所以这里既不能放行任意 blob（会让请求体无限膨胀、token 预算失控），也不该
    试图判断"这个工具安全不安全" —— 那是批准环节的事，不是整形层的事。
    """
    if not isinstance(value, list):
        raise HTTPException(400, "tools must be a list")
    if len(value) > CHAT_MAX_TOOLS:
        raise HTTPException(400, f"too many tools (limit {CHAT_MAX_TOOLS})")
    clean: list[dict[str, Any]] = []
    for tool in value:
        if not isinstance(tool, dict):
            raise HTTPException(400, "each tool must be an object")
        if tool.get("type") != "function":
            raise HTTPException(400, "only type='function' tools are supported")
        function = tool.get("function")
        if not isinstance(function, dict):
            raise HTTPException(400, "tool.function must be an object")
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "tool.function.name must be a non-empty string")
        if len(name) > 64:
            raise HTTPException(400, "tool.function.name is too long")
        # 只保留 OpenAI 已定义的字段：多出来的键会被 llama-server 的模板原样
        # 序列化进 prompt，等于一条把任意文本塞进系统提示的通道。
        entry: dict[str, Any] = {"type": "function", "function": {"name": name}}
        description = function.get("description")
        if isinstance(description, str) and description:
            entry["function"]["description"] = description[:4096]
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            entry["function"]["parameters"] = parameters
        clean.append(entry)
    encoded = json.dumps(clean, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > CHAT_MAX_TOOLS_BYTES:
        raise HTTPException(400, "tool definitions are too large")
    return clean


def _normalize_tool_calls_for_history(value: Any) -> list[dict[str, Any]]:
    """校验并裁剪历史消息里 assistant 的 tool_calls。

    与请求侧的 ``_normalize_tools`` 不是一回事：那里是**工具声明**（长什么样、
    怎么调），这里是**已经发生过的调用**（叫什么、参数是什么、id 是多少）。
    llama-server 的模板要靠 ``id`` 把随后的 tool 消息配对回来，所以 id 必须
    原样保留；参数是模型当时生成的，不做语义校验，只限制体积。

    刻意**不抛 400**：这是历史记录，一条脏数据不该让用户整个会话都发不出去。
    形状不认识就丢弃那一条，让对话继续。
    """
    if not isinstance(value, list):
        return []
    clean: list[dict[str, Any]] = []
    for call in value[:CHAT_MAX_TOOLS]:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            # 没有 id 的调用无法与 tool 结果配对，留着只会让模板困惑。
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        entry: dict[str, Any] = {
            "id": call_id.strip()[:128],
            "type": "function",
            "function": {"name": name.strip()[:64]},
        }
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            entry["function"]["arguments"] = arguments[:CHAT_MAX_TOOL_ARGS_CHARS]
        elif isinstance(arguments, dict):
            encoded = json.dumps(arguments, ensure_ascii=False)
            entry["function"]["arguments"] = encoded[:CHAT_MAX_TOOL_ARGS_CHARS]
        else:
            entry["function"]["arguments"] = "{}"
        clean.append(entry)
    encoded = json.dumps(clean, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > CHAT_MAX_TOOLS_BYTES:
        return []
    return clean


def _normalize_tool_choice(value: Any) -> Any:
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode not in _TOOL_CHOICE_MODES:
            raise HTTPException(
                400, "tool_choice must be 'auto', 'none', 'required' or an object"
            )
        return mode
    if isinstance(value, dict):
        # {"type":"function","function":{"name":"x"}} —— 强制调某个工具
        if value.get("type") != "function" or not isinstance(value.get("function"), dict):
            raise HTTPException(400, "tool_choice object must name a function")
        name = value["function"].get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "tool_choice.function.name must be a non-empty string")
        return {"type": "function", "function": {"name": name}}
    raise HTTPException(400, "tool_choice must be a string or an object")


def _validate_numeric_fields(payload: dict[str, Any]) -> None:
    """拒绝类型错误或非有限的采样参数（400，而不是等下游 500）。

    ``json.loads`` 接受 ``Infinity`` / ``NaN`` / ``1e999``，所以"客户端发来
    一个非有限数"根本不需要异常构造。这些值一旦进入
    ``chat_context.reply_reserve`` 的 ``int()`` 就抛 ``OverflowError``，
    而那条路径只捕获 ``(TypeError, ValueError)``。
    """
    for key in _NUMERIC_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(400, f"{key} must be a number")
        try:
            finite = math.isfinite(value) if isinstance(value, float) else True
        except (OverflowError, ValueError):
            # 超大整数（10**400）连 float 都转不过去。
            finite = False
        if not finite:
            raise HTTPException(400, f"{key} must be a finite number")


def normalize_chat_payload(body: Any) -> dict[str, Any]:
    """Validate and normalize the chat subset we forward to llama-server.

    Text is the default shape; multimodal content blocks and ``tools`` are
    accepted when the request actually carries them. Every path keeps a
    byte/char budget — the point of the whitelist is that an arbitrary client
    blob can never become an arbitrary upstream request.
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "chat body must be an object")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(400, "messages must be a non-empty list")
    if len(messages) > CHAT_MAX_MESSAGES:
        raise HTTPException(400, "too many messages")
    normalized: list[dict[str, Any]] = []
    total = 0
    budget = {"image_chars": 0}
    for item in messages:
        if not isinstance(item, dict):
            raise HTTPException(400, "each message must be an object")
        role = item.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise HTTPException(400, "unsupported message role")
        content = item.get("content")
        if content is None:
            content = ""
        clean, chars = _normalize_content(content, role, budget)
        total += chars
        if total > CHAT_MAX_TOTAL_CHARS:
            raise HTTPException(400, "conversation is too long")
        message: dict[str, Any] = {"role": role, "content": clean}
        # tool 角色必须带 tool_call_id：模型靠它把结果对回自己发起的调用。
        # 缺了它，llama-server 的模板会把这条当成一段匿名文本混进上下文。
        if role in _TOOL_ROLES:
            call_id = item.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                raise HTTPException(400, "tool messages require a tool_call_id")
            message["tool_call_id"] = call_id[:128]
        # assistant 的 tool_calls 必须原样带回去。模型在下一轮靠它认出
        # "我刚才请求过哪个调用"，tool 结果也是按 call id 配对的；把这一段
        # 丢掉，工具结果就变成"一条没人认领的 tool 消息"，多轮工具链直接断。
        # 这里仍走与请求侧相同的形状校验（只认 function 调用、限制条数与字节），
        # 避免历史里的一条脏记录把整个请求打成 400。
        if role == "assistant" and item.get("tool_calls"):
            calls = _normalize_tool_calls_for_history(item.get("tool_calls"))
            if calls:
                message["tool_calls"] = calls
        normalized.append(message)
    payload = {
        key: value for key, value in body.items() if key in CHAT_ALLOWED_FIELDS
    }
    _validate_numeric_fields(payload)
    payload["messages"] = normalized
    payload["stream"] = True
    if "tools" in payload:
        payload["tools"] = _normalize_tools(payload["tools"])
    if "tool_choice" in payload:
        payload["tool_choice"] = _normalize_tool_choice(payload["tool_choice"])
    if "parallel_tool_calls" in payload:
        if not isinstance(payload["parallel_tool_calls"], bool):
            raise HTTPException(400, "parallel_tool_calls must be a boolean")
    if "chat_template_kwargs" in payload:
        payload["chat_template_kwargs"] = _normalize_template_kwargs(
            payload["chat_template_kwargs"]
        )
    return payload


# 允许透传给对话模板的键。模板是第三方 GGUF 自带的 Jinja 源码，`chat_template_kwargs`
# 里的每个键都会变成模板变量 —— 一个未经约束的对象等于让客户端向模板注入任意变量。
# 这里只放行"控制思考"这一族（它们正是本项目的用途），外加少量已知安全的开关。
#
# 值的类型也限制成 bool / 有限的 str / int：模板会对它们做 `is true`、`is defined`
# 之类的判断，塞进嵌套对象或超长字符串只会让渲染行为不可预测。
_TEMPLATE_KWARG_KEYS = frozenset({
    "enable_thinking",
    "thinking_budget",
    "reasoning_effort",
    "add_generation_prompt",
    "continue_final_message",
})
_TEMPLATE_KWARG_MAX_STR = 64


def _normalize_template_kwargs(value: Any) -> dict[str, Any]:
    """校验 `chat_template_kwargs`：只留已知键 + 简单标量值。

    没有这层校验时，客户端可以把**任意**变量塞进 Jinja 模板。模板里只要有
    `{{ some_var }}` 之类的插值，就等于一条把用户可控文本注入 prompt 的通道
    （prompt 注入），而且不同模型的模板各不相同，无法一概而论。
    """
    if not isinstance(value, dict):
        raise HTTPException(400, "chat_template_kwargs must be an object")
    clean: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key not in _TEMPLATE_KWARG_KEYS:
            raise HTTPException(
                400, f"unsupported chat_template_kwargs key: {key!r}"
            )
        if isinstance(item, bool):
            clean[key] = item
        elif isinstance(item, int):
            clean[key] = item
        elif isinstance(item, str) and len(item) <= _TEMPLATE_KWARG_MAX_STR:
            clean[key] = item
        else:
            raise HTTPException(
                400, f"chat_template_kwargs[{key!r}] must be a boolean, integer or short string"
            )
    return clean


def instance_api_key(inst: Any) -> str:
    """Read the effective API key from params, then from the final argv.

    ``extra_args`` can append ``--api-key`` after the panel parameters; the
    instance's argv is the authoritative command line, so the proxy must use the
    same last-wins value llama-server received.
    """
    key = str((inst.params or {}).get("api_key") or "").strip()
    argv = getattr(inst, "argv", None) or []
    for index, token in enumerate(argv):
        if token == "--api-key" and index + 1 < len(argv):
            key = str(argv[index + 1]).strip()
        elif isinstance(token, str) and token.startswith("--api-key="):
            key = token.split("=", 1)[1].strip()
    return key


def upstream_base(inst: Any) -> str:
    """实例的 HTTP 基址；通配监听地址统一翻译成回环。"""
    host = inst.host
    if host in {"0.0.0.0", "::", "[::]", ""}:
        host = "127.0.0.1"
    # IPv6 字面量必须加方括号，否则 urlsplit/h11 拿到的是 `http://::1:8080`，
    # hostname 解析成 None、端口串变成 ":1:8080" —— 请求直接失败。
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{inst.port}"


async def open_chat_upstream(inst: Any, payload: dict[str, Any]):
    """Open a streaming request to the instance's OpenAI-compatible endpoint.

    返回 ``(client, response)``；调用方负责关闭两者。``trust_env=False``：
    回环流量不得经过用户配置的 HTTP 代理（Clash/V2Ray 未启动时会超时）。
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Accept-Encoding": "identity",
    }
    key = instance_api_key(inst)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    client = httpx.AsyncClient(
        trust_env=False,
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
    )
    try:
        request = client.build_request(
            "POST", f"{upstream_base(inst)}/v1/chat/completions", json=payload, headers=headers
        )
        response = await client.send(request, stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        raise HTTPException(502, f"llama-server unreachable: {error}") from error
    return client, response


async def count_chat_tokens(client: Any, base: str, inst: Any, text: str) -> int:
    """用 llama-server 自己的分词器数 token，而不是按字符猜。

    失败时抛错，由调用方决定降级方式。**刻意不返回估算值**：一个静默的估算会把
    "按 token 准入"重新变成按别的什么准入，而这条路径存在的理由正是单位要统一。
    """
    headers = {"Content-Type": "application/json"}
    key = instance_api_key(inst)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    response = await client.post(f"{base}/tokenize", json={"content": text}, headers=headers)
    if response.status_code != 200:
        raise HTTPException(
            502, f"llama-server /tokenize returned HTTP {response.status_code}"
        )
    tokens = response.json().get("tokens")
    if not isinstance(tokens, list):
        raise HTTPException(502, "llama-server /tokenize returned no token list")
    return len(tokens)


async def fit_payload_to_context(inst: Any, payload: dict[str, Any]) -> int:
    """把历史裁进模型的真实上下文窗口；返回被裁掉的轮数。

    上下文未知（``ctx_size`` 拿不到）时**不裁**：宁可按原样发出去让上游报错，
    也不要凭一个猜的窗口丢用户的内容。``/tokenize`` 失败会直接抛出——静默降级
    成估算值就等于悄悄放弃"按 token 计量"这个前提。
    """
    from core import chat_context

    context_size = chat_context.context_size_of(inst)
    if context_size <= 0:
        return 0
    budget = chat_context.token_budget(context_size, payload)
    if budget <= 0:
        return 0
    base = upstream_base(inst)
    client = httpx.AsyncClient(trust_env=False, timeout=15.0)
    try:
        async def count_text(text: str) -> int:
            try:
                return await count_chat_tokens(client, base, inst, text)
            except httpx.HTTPError as error:
                # 预检是"转发前的一次额外往返"，它失败不该变成裸 500：用户会看到
                # 一句与聊天无关的 "Internal Server Error"，而真正原因是本地
                # tokenizer 端点超时/断开。翻译成 502 并说清是哪一步。
                raise HTTPException(
                    502, f"could not measure the prompt against the model context: {error}"
                ) from error

        # 先量一次整体；只有可能超了才进入逐步裁剪，短对话不多花往返。
        #
        # **必须用 chat_context 的文本抽取**，不能用 `str(content)`：多模态消息
        # 的 content 是内容块数组，`str()` 会得到含完整 base64 data URL 的
        # Python repr。实测一张 4 MB 图 = 5.6 MB 的 /tokenize 请求、2.65 秒、
        # 返回约 280 万 token —— 每一轮都白付这笔钱，4 张图时逼近 15 秒超时。
        # chat_context._message_text 只取 text 块，图片由固定估算计入
        # （fit_messages_to_context 内部已经这么做）。同仓库里两套计量规则
        # 不一致，错的曾是入口这一处。
        original = await chat_context.measure_messages(
            payload["messages"], count_text
        )
        if original <= budget:
            return 0

        kept, dropped, final = await chat_context.fit_messages_to_context(
            payload["messages"], budget, count_text
        )
        # 裁到只剩"system + 当前问题"还装不下：这不是历史太长，是窗口或这条
        # 输入本身的问题。必须说清楚，而不是继续丢内容或让上游回一句看不懂的话。
        if final > budget and len(kept) <= 2:
            raise HTTPException(
                400,
                f"this prompt alone needs about {final} tokens but the context window is "
                f"{context_size}; raise ctx_size or shorten the message",
            )
        payload["messages"] = kept
        return dropped
    finally:
        await client.aclose()
