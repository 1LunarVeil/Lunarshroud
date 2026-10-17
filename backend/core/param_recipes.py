"""用途配方（use-case recipes）。

**为什么不是"内置预设"**：``core/preset_store.py`` 的模块文档记着一条既有决定 ——
内置的推测解码组合、显存档位会随 llama.cpp 版本漂移，必然过时。配方遵守同一条
约束，做法是把"会漂移的东西"全部排除在外：

* 只写**意图**（这是个嵌入服务 / 这是个只读工具服务），不写调优数字
  （ctx、层数、批大小、显存档位一律不碰 —— 那些留给参数面板与显存预估）；
* 不写模型路径，配方永远只是"把面板切到某个用途"，不是"能直接启动的东西"；
* 键必须存在于 ``PARAM_DEFINITIONS``（有守门测试），值必须过
  ``validate_param_values``；
* "本机这份构建认不认这些 flag"仍由 capability 层判定：不认就在输出里标成
  ``unsupported``，前端置灰、``build_argv`` 丢弃并留痕 —— 与既有参数同一口径。

所以配方能过时的部分只有"哪些用途值得单列"，而不会给出错误的参数值。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.param_schema import PARAM_DEFINITIONS, param_is_supported
from core.server_probe import ServerProbe

# 只读工具集：llama.cpp 内置工具里不含 exec_shell_command / write_file / edit_file
# 的那部分（取值照抄 v0.7.0 ``--tools`` 的 help 原文）。
READONLY_TOOLS = "read_file,file_glob_search,grep_search,get_info"


@dataclass(frozen=True)
class Recipe:
    """一个用途 = 一组意图值 + 双语文案。"""

    id: str
    label: str
    label_en: str
    hint: str
    hint_en: str
    values: dict[str, Any]


RECIPES: list[Recipe] = [
    Recipe(
        id="chat",
        label="对话生成",
        label_en="Chat / completion",
        hint="普通对话与补全：关闭嵌入、重排序与 Agent 工具，思考模式交回对话模板判断。",
        hint_en=(
            "Plain chat and completion: embeddings, reranking and agent tools off; "
            "thinking is left to the chat template."
        ),
        values={
            "embeddings": False,
            "rerank": False,
            "agent": False,
            "tools": "",
            "reasoning": "auto",
        },
    ),
    Recipe(
        id="embedding",
        label="嵌入服务（RAG 检索）",
        label_en="Embeddings (RAG retrieval)",
        hint=(
            "RAG 的检索侧：启动后 /v1/embeddings 可用。建议给嵌入模型单开一个实例 —— "
            "v0.7.0 起开启池化时会跳过输出层，嵌入的显存占用约为原来的十分之一。"
            "池化方式留空即用模型自己的默认值。"
        ),
        hint_en=(
            "The retrieval side of RAG: /v1/embeddings becomes available. Give embedding "
            "models their own instance - since v0.7.0 pooling skips the output layer, so "
            "embedding VRAM is roughly a tenth of what it was. Leaving pooling empty uses "
            "the model's own default."
        ),
        values={
            "embeddings": True,
            "rerank": False,
            "agent": False,
            "tools": "",
        },
    ),
    Recipe(
        id="rerank",
        label="重排序（RAG 精排）",
        label_en="Reranking (RAG rescoring)",
        hint=(
            "启用 /rerank 精排端点，需要一个重排序模型。检索侧另开一个嵌入实例，"
            "两者可以同时运行 —— 端口由启动器自动分配。"
        ),
        hint_en=(
            "Enables the /rerank endpoint; requires a reranker model. Run the embedding "
            "instance alongside it - the launcher auto-assigns ports, so both can stay up."
        ),
        values={
            "rerank": True,
            "embeddings": False,
            "agent": False,
            "tools": "",
        },
    ),
    Recipe(
        id="tools_readonly",
        label="只读工具（引用本地文件）",
        label_en="Read-only tools (local files)",
        hint=(
            f"只给模型读取与检索本地文件的能力（{READONLY_TOOLS}），不含 "
            "exec_shell_command / write_file / edit_file。为安全上游会把 "
            "--cors-origins 限制在本机。"
        ),
        hint_en=(
            "Read-only access to local files (read_file, file_glob_search, grep_search, "
            "get_info) without exec_shell_command, write_file or edit_file. Upstream pins "
            "--cors-origins to localhost for safety."
        ),
        values={
            "tools": READONLY_TOOLS,
            "agent": False,
            "embeddings": False,
            "rerank": False,
        },
    ),
    Recipe(
        id="agent",
        label="Agent 全功能（含 MCP）",
        label_en="Full agent (tools + MCP)",
        hint=(
            "--agent = CORS 代理 + 全部内置工具，其中包含 exec_shell_command、write_file、"
            "edit_file —— 仅在可信环境使用。需要 MCP 服务器时再填「MCP 服务器配置」"
            "或内联 JSON。"
        ),
        hint_en=(
            "--agent = CORS proxy plus every built-in tool, including exec_shell_command, "
            "write_file and edit_file - trusted environments only. Fill in the MCP servers "
            "config (or the inline JSON) when you need MCP."
        ),
        values={"agent": True},
    ),
]


_KNOWN_KEYS = {meta.key: meta for meta in PARAM_DEFINITIONS}


def _intent_is_noop(value: Any) -> bool:
    """这个值是不是"什么也没要求"（关掉 / 留空）。

    配方把某个开关设成 ``False`` 时，本机构建**没有**这个 flag 恰恰就是它想要的
    状态 —— 报成"不支持"是假警报，还会让前端白白置灰一个本来就能用的按钮。
    与 ``compare_param_default`` 的三态同理：没有依据/无关紧要的，不要说成问题。
    """
    if value is False or value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


def recipe_values(recipe: Recipe) -> dict[str, Any]:
    """配方里**确实存在**于 schema 的键值对。

    过滤不是为了宽容写错键，而是为了让"配方引用了一个已被删掉的参数"退化成
    "这一项不再生效"，而不是把未知键塞进 params 再让校验或 build_argv 去猜。
    写错键由守门测试拦（见 tests/test_param_recipes.py）。
    """
    return {k: v for k, v in recipe.values.items() if k in _KNOWN_KEYS}


def recipes_for_frontend(probe: ServerProbe | None = None) -> list[dict[str, Any]]:
    """输出前端可直接消费的配方列表（双语文案 + 本机可用性）。

    ``unsupported`` 用的是与界面置灰、命令行丢弃**同一条**判定
    （``param_is_supported``）；探测不可用时它是 ``None`` 而不是 ``False``，
    前端此时不得禁用任何控件。只报"确实要求了某个能力"的键 —— 把开关设成
    False 而构建没有这个 flag，本来就是配方想要的结果（见 :func:`_intent_is_noop`）。
    """
    out: list[dict[str, Any]] = []
    for recipe in RECIPES:
        values = recipe_values(recipe)
        unsupported: list[str] = []
        for key, value in values.items():
            if _intent_is_noop(value):
                continue
            if param_is_supported(_KNOWN_KEYS[key], probe) is False:
                unsupported.append(key)
        out.append({
            "id": recipe.id,
            "label": recipe.label,
            "label_en": recipe.label_en,
            "hint": recipe.hint,
            "hint_en": recipe.hint_en,
            "values": values,
            "unsupported": unsupported,
        })
    return out
