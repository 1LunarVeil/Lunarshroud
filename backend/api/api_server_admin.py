"""本地 API 服务的**管理面**路由（状态 / 请求日志 / 接入片段 / 启停）。

数据面（``/v1/*``）在另一个应用、另一个端口上，见 ``api/gateway.py``。这里
这些路由挂在启动器 app 上，因此自动被 ``session_auth_middleware`` 覆盖
（``test_auth_coverage`` 会穷举验证它们无令牌必须 401）—— 它们读的是
"网关转发过什么"，属于管理信息，不该给外部工具看。

**为什么状态要现算而不是读一个缓存的布尔值**：网关可能因为端口被占而启动
失败（uvicorn 的 ``serve()`` 在这种情况下正常返回，不抛异常）。只有把
``GatewayManager.running`` 与 ``error`` 一起报上来，界面才能显示
"已开启但没起来：端口被占用"，而不是一个绿色开关配一条无法连接的服务。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

# 接入片段引用的环境变量名。片段**从不**内联密钥：这段文字会被贴进聊天记录、
# 记事本和 issue，任何写进去的真实密钥都会随之扩散。统一从环境变量读。
KEY_ENV_VAR = "LUNARSHROUD_API_KEY"


def _cfg():
    from config import get_config

    return get_config()


@router.get("/api/api-server/status")
async def api_api_server_status():
    """网关状态：开关、地址、接入点、运行中的模型、日志容量。

    **不回传密钥本身**，只回传"是否已设置"。把密钥回传到页面等于让它进
    WebView 的内存与 devtools，而页面根本不需要它（只需要知道要别提）。
    """
    from api import gateway

    cfg = _cfg()
    runtime = gateway.gateway_manager().status()
    port = cfg.get("api_server_port", 1234)

    import main

    pm = main.get_process_manager()
    models = [
        {
            "id": str(getattr(inst, "model_path", "") or inst.name),
            "name": inst.name,
            "instance_id": inst.id,
            "port": getattr(inst, "port", None),
        }
        for inst in pm.list_instances()
        if inst.status == main.InstanceStatus.RUNNING
    ]

    return {
        "enabled": bool(cfg.get("api_server_enabled")),
        "running": runtime["running"],
        "error": runtime["error"],
        "port": port,
        "base_url": f"http://127.0.0.1:{port}/v1",
        "cors_origins": gateway.parse_origins(cfg.get("api_server_cors_origins", "")),
        "key_configured": bool(str(cfg.get("api_server_key") or "")),
        "log_requests": bool(cfg.get("api_server_log_requests", True)),
        "logs_capacity": gateway.LOG_CAPACITY,
        "models": models,
    }


@router.get("/api/api-server/logs")
async def api_api_server_logs(limit: int = 100):
    """最近经过网关的请求（最新在前）。"""
    from api import gateway

    entries = gateway.REQUEST_LOG.snapshot()
    take = max(1, min(int(limit or 100), gateway.LOG_CAPACITY))
    return {
        "entries": list(reversed(entries))[:take],
        "capacity": gateway.LOG_CAPACITY,
    }


@router.delete("/api/api-server/logs")
async def api_api_server_logs_clear():
    from api import gateway

    gateway.REQUEST_LOG.clear()
    return {"cleared": True}


@router.post("/api/api-server/restart")
async def api_api_server_restart():
    """按当前配置重新对齐网关（保存设置后立即生效，免重启启动器）。

    返回最新的运行状态而不是 ``{"ok": true}`` —— 绑定失败时用户必须看到原因。
    """
    from api import gateway

    cfg = _cfg()
    await gateway.gateway_manager().apply(cfg)
    return gateway.gateway_manager().status()


@router.get("/api/api-server/recipes")
async def api_api_server_recipes():
    """复制即用的接入片段（curl / Python / PowerShell）。

    服务端生成而不是前端拼装：端口与是否配置密钥都在服务端，前端拼会与实际
    状态漂移（与 §2.25「引用是响应契约」同一个理由）。

    所有片段都从环境变量取值（``KEY_ENV_VAR``），响应体里**没有**任何密钥
    字面量：这些代码会被贴进聊天、记事本与 issue。
    """
    from api import gateway

    cfg = _cfg()
    port = cfg.get("api_server_port", 1234)
    base = f"http://127.0.0.1:{port}/v1"
    has_key = bool(str(cfg.get("api_server_key") or ""))
    bearer = f"${KEY_ENV_VAR}"
    env_name = KEY_ENV_VAR

    return {
        "base_url": base,
        "enabled": bool(cfg.get("api_server_enabled")),
        "running": gateway.gateway_manager().status()["running"],
        "key_configured": has_key,
        "key_env_var": env_name,
        "chat_completions": f"{base}/chat/completions",
        "models": f"{base}/models",
        "curl": (
            f'curl "{base}/chat/completions" \\\n'
            '  -H "Content-Type: application/json" \\\n'
            f'  -H "Authorization: Bearer {bearer}" \\\n'
            '  -d "{\\"model\\":\\"<model>\\",\\"messages\\":'
            '[{\\"role\\":\\"user\\",\\"content\\":\\"hello\\"}],'
            '\\"stream\\":false}"'
        ),
        "python": (
            "import os\n"
            "from openai import OpenAI\n"
            f'client = OpenAI(base_url="{base}", api_key=os.environ["{env_name}"])\n'
            "print(client.chat.completions.create(\n"
            '    model="<model>",\n'
            '    messages=[{"role": "user", "content": "hello"}],\n'
            ").choices[0].message.content)"
        ),
        "powershell": (
            f'$headers = @{{ Authorization = "Bearer $env:{env_name}" }}\n'
            f'Invoke-RestMethod -Uri "{base}/chat/completions" '
            "-Method Post -Headers $headers -ContentType 'application/json' "
            '-Body \'{"model":"<model>","messages":[{"role":"user",'
            '"content":"hello"}]}\''
        ),
        "note": (
            f"把网关访问密钥放进环境变量 {env_name}；未设置密钥时该请求头可省略。"
            if has_key
            else "当前未设置访问密钥（仅本机可访问），请求头可省略。"
        ),
    }
