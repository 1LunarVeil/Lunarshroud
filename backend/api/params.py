"""参数 schema / 默认值 / 用途配方 / 命令预览的路由。

自 ``main.py`` 原样搬入（函数体未改）。结构照 ``api/presets.py`` 的模板。

``_runtime_for_launch`` 经 ``main`` 延迟取得：``test_manifest_runtime.py`` 用
``monkeypatch.setattr(main, "_runtime_for_launch", …)`` 替换它，本模块若持有独立
引用，那条 patch 就打不到，schema/配方会走真实注册表 —— 测试变假绿。
"""
from __future__ import annotations

from config import get_config
from core.param_builder import (
    build_argv,
    build_command_string,
    redact_argv,
    suppressed_params,
    validate_param_values,
)
from core.param_recipes import recipes_for_frontend
from core.param_schema import get_default_params, get_schema_for_frontend
from core.server_probe import probe_server_cached_only, resolve_server_path
from fastapi import APIRouter, HTTPException

router = APIRouter()


def _resolve_launch(runtime_id: str | None):
    import main

    return main._runtime_for_launch(runtime_id)


def _resolved_exe(runtime_path: str | None) -> str | None:
    """生效的可执行文件；相对名按应用目录/PATH 解析（AGENTS §2.1）。"""
    if runtime_path:
        return runtime_path
    configured = get_config().get("llama_server_path", "")
    return resolve_server_path(configured) if str(configured).strip() else None


@router.get("/api/params/schema")
async def api_param_schema(runtime_id: str | None = None):
    """获取参数 schema（前端动态渲染表单用）。

    带上当前 llama-server 的探测结果：每个参数多了 ``supported``（本机构建认不认
    这个 flag）与 ``upstream_options``（上游声明的合法取值）。路径由用户在设置里
    自由指定，构建代次不可控，schema 必须跟着当前构建走。

    ``runtime_id`` 可选：参数编辑器选择运行环境后，schema 必须跟着该构建走，
    而不是全局默认路径。
    """
    _, _, runtime_path = _resolve_launch(runtime_id)
    return get_schema_for_frontend(_resolved_exe(runtime_path))


@router.get("/api/params/defaults")
async def api_param_defaults():
    """获取默认参数"""
    return get_default_params()


@router.get("/api/params/recipes")
async def api_param_recipes(runtime_id: str | None = None):
    """用途配方：一键把参数面板切到某个用途（对话 / 嵌入 / 重排 / 只读工具 / Agent）。

    与 schema 同一口径：**只读探测缓存**（这是页面加载就要拉的接口，不许为它跑
    ``--help``），并逐键标出本机构建不支持的项，前端据此置灰。配方只写意图
    （"这是个嵌入服务"），不写调优数字、不写模型路径 —— 那些会随 llama.cpp
    版本漂移，见 ``core/param_recipes.py`` 的模块文档。
    """
    _, _, runtime_path = _resolve_launch(runtime_id)
    exe = _resolved_exe(runtime_path)
    probe = probe_server_cached_only(exe) if exe else None
    return {"recipes": recipes_for_frontend(probe)}


@router.post("/api/params/build-command")
async def api_build_command(body: dict):
    """根据参数构建命令行字符串（前端实时预览用）"""
    params = body.get("params", {})
    if not isinstance(params, dict):
        raise HTTPException(400, "params must be an object")
    _, _, runtime_path = _resolve_launch(body.get("runtime_id"))
    cfg = get_config()
    configured = runtime_path or cfg["llama_server_path"]
    # 预览路径同样不执行任何东西；但探测缓存必须按**解析后的**可执行文件取，
    # 否则裸名会相对 CWD 命中一个用户可写的同名文件（AGENTS §2.1）。
    resolved = runtime_path or (resolve_server_path(configured) if str(configured).strip() else None)
    # schema 声明的 min/max/options 在预览路径同样生效：不合法取值让预览变成
    # 错误行（走全局 ValueError 处理器 → 400），而不是给出一条跑不起来的命令。
    # 与启动路径 _validate_start 用的是同一份校验、同一类错误返回。
    # cached_only：预览是高频路径，绝不为了校验起子进程跑 --help。
    probe = probe_server_cached_only(resolved) if resolved else None
    validate_param_values(params, probe=probe)
    # 预览显示实际会执行的路径；未选运行时则仍显示用户配置的写法，
    # 真正启动时 _validate_start 会解析成绝对路径。
    argv = build_argv(params, configured)
    return {
        "argv": redact_argv(argv),
        "command": build_command_string(argv),
        # 本机构建不支持、因而被丢弃的参数。命令行里没有、也不报错是最糟的
        # 结果（用户以为自己勾的开关生效了），所以必须由前端显式提示。
        "suppressed": suppressed_params(params, resolved) if resolved else [],
    }
