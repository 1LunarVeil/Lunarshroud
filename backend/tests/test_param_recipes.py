"""用途配方（param_recipes）的守门测试。

配方是"一键把面板切到某个用途"，它离安全边界只有一步：如果配方能写
``host`` / ``api_key`` / ``extra_args``，那就等于把一条绕过"非回环必须配
api_key"的载荷做成了按钮。所以这里钉住的不只是"能不能用"，还有"不许碰什么"。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest  # noqa: E402
from core.param_builder import build_argv, validate_param_values  # noqa: E402
from core.param_recipes import RECIPES, recipe_values, recipes_for_frontend  # noqa: E402
from core.param_schema import PARAM_DEFINITIONS, get_default_params  # noqa: E402
from core.server_probe import MIN_COMPLETE_OPTIONS, build_probe_from_text  # noqa: E402

FAKE_EXE = "llama-server.exe"

# 配方**永远不许**写的键：前五个是启动器代管的安全边界与实例身份
# （见 AGENTS §2.3 / SECURITY_MODEL），后两个是"配方不写模型路径"这条约束。
FORBIDDEN_RECIPE_KEYS = {
    "host", "port", "api_key", "path", "log_file", "extra_args", "model", "model_dir",
}


def complete_probe(*extra: str, drop: tuple[str, ...] = ()):
    lines = [f"--filler-{i} N                          filler (default: 0)" for i in range(MIN_COMPLETE_OPTIONS + 5)]
    for meta in PARAM_DEFINITIONS:
        if meta.cli and meta.cli not in drop:
            lines.append(f"{meta.cli} N                          flag for {meta.key}")
    lines.extend(extra)
    return build_probe_from_text(FAKE_EXE, "\n".join(lines) + "\n")


def test_recipe_ids_are_unique_and_bilingual():
    ids = [r.id for r in RECIPES]
    assert len(ids) == len(set(ids)), ids
    for recipe in RECIPES:
        assert recipe.label.strip() and recipe.label_en.strip(), recipe.id
        assert recipe.hint.strip() and recipe.hint_en.strip(), recipe.id
        assert recipe.values, recipe.id


def test_recipes_only_reference_params_that_exist():
    """漂移守门：配方引用一个已被改名/删除的参数时**必须变红**。

    ``recipe_values`` 会把未知键过滤掉（免得塞进 params 让下游去猜），
    但静默过滤等于"按钮还在、点了没反应"，所以由这条测试负责喊出来。
    """
    known = {meta.key for meta in PARAM_DEFINITIONS}
    for recipe in RECIPES:
        unknown = sorted(set(recipe.values) - known)
        assert not unknown, f"{recipe.id} 引用了不存在的参数: {unknown}"
        assert recipe_values(recipe) == recipe.values, recipe.id


def test_recipes_never_touch_managed_or_model_keys():
    for recipe in RECIPES:
        touched = set(recipe.values) & FORBIDDEN_RECIPE_KEYS
        assert not touched, f"{recipe.id} 不许写这些键: {sorted(touched)}"


def test_recipe_values_pass_validation_and_reach_argv():
    """配方值必须合法（否则点一下就是一次 400），并且真的落到命令行上。"""
    probe = complete_probe()
    for recipe in RECIPES:
        params = {**get_default_params(), "model": "D:/m.gguf", **recipe_values(recipe)}
        validate_param_values(params, probe=probe)
        argv = build_argv(params, FAKE_EXE, probe=probe)
        if recipe.id == "embedding":
            assert "--embeddings" in argv
        if recipe.id == "rerank":
            assert "--rerank" in argv
        if recipe.id == "agent":
            assert "--agent" in argv
        if recipe.id == "tools_readonly":
            assert argv[argv.index("--tools") + 1] == "read_file,file_glob_search,grep_search,get_info"
            assert "--agent" not in argv
        if recipe.id == "chat":
            for flag in ("--embeddings", "--rerank", "--agent", "--tools"):
                assert flag not in argv, flag


def test_readonly_recipe_excludes_the_dangerous_tools():
    """只读配方之所以叫只读：这三个工具一个都不能出现。"""
    tools = next(r for r in RECIPES if r.id == "tools_readonly").values["tools"]
    for dangerous in ("exec_shell_command", "write_file", "edit_file"):
        assert dangerous not in tools


def test_unsupported_recipe_keys_are_reported_not_silently_applied():
    """本机构建不认某个 flag 时，配方要把它标出来（前端置灰），
    而不是让用户点了按钮、命令行里却没有、也不报错。"""
    probe = complete_probe(drop=("--rerank", "--agent"))
    payload = {r["id"]: r for r in recipes_for_frontend(probe)}
    assert payload["rerank"]["unsupported"] == ["rerank"]
    assert payload["agent"]["unsupported"] == ["agent"]
    # embedding / chat 配方只是把 rerank 与 agent **关掉**：构建没有这两个 flag
    # 恰好就是它们要的状态，报成"不支持"是假警报（会让按钮白白置灰）。
    assert payload["embedding"]["unsupported"] == []
    assert payload["chat"]["unsupported"] == []


def test_turning_a_flag_off_is_not_reported_as_unsupported():
    """"关掉一个本机构建根本没有的开关"不是问题，报成不支持就是假警报。

    两个方向都要钉住：``tools=""``（不要求）不报，``tools=read_file,...``
    （确实要求）必须报 —— 否则用户点了"只读工具"、命令行里却没有、也不提示。
    """
    probe = complete_probe(drop=("--tools",))
    payload = {r["id"]: r for r in recipes_for_frontend(probe)}
    assert payload["chat"]["unsupported"] == []
    assert payload["embedding"]["unsupported"] == []
    assert payload["tools_readonly"]["unsupported"] == ["tools"]


def test_without_a_probe_nothing_is_claimed_unsupported():
    """探测不可用 → ``unsupported`` 必须为空：没有依据就不许禁用控件。"""
    for item in recipes_for_frontend(None):
        assert item["unsupported"] == []


def test_frontend_payload_is_json_shaped():
    """前端直接消费这个结构，字段名是契约。"""
    import json

    payload = recipes_for_frontend(complete_probe())
    text = json.dumps(payload, ensure_ascii=False)
    assert text and isinstance(payload, list)
    for item in payload:
        assert set(item) == {"id", "label", "label_en", "hint", "hint_en", "values", "unsupported"}


@pytest.mark.parametrize("recipe_id", [r.id for r in RECIPES])
def test_every_recipe_survives_a_round_trip_through_json(recipe_id):
    import json

    payload = {r["id"]: r for r in recipes_for_frontend(complete_probe())}
    restored = json.loads(json.dumps(payload[recipe_id], ensure_ascii=False))
    assert restored["values"] == payload[recipe_id]["values"]
