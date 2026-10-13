"""LunarShroud 后端 API 冒烟测试（8/2 复核用）

对 17520 端口全部端点断言状态码与关键字段。
运行方式：python tools/smoke_test.py
"""
import asyncio
import json
import sys

import httpx
import websockets

B = "http://127.0.0.1:17520"
WS = "ws://127.0.0.1:17520"

passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = ""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


async def main():
    async with httpx.AsyncClient(base_url=B, timeout=10) as c:
        # ── 系统 ──
        print("== 系统接口 ==")
        r = await c.get("/api/system/config")
        check("GET /api/system/config", r.status_code == 200 and "launcher_port" in r.json())
        r = await c.get("/api/system/overview")
        j = r.json()
        check("GET /api/system/overview", r.status_code == 200
              and "cpu_percent" in j and "memory_percent" in j and "gpus" in j)

        # ── 参数 ──
        print("== 参数接口 ==")
        r = await c.get("/api/params/schema")
        j = r.json()
        n = len(j) if isinstance(j, (list, dict)) else -1
        if isinstance(j, dict):
            n = len(j.get("params", j))
        check("GET /api/params/schema", r.status_code == 200 and n >= 40, f"count={n}")
        r = await c.get("/api/params/defaults")
        check("GET /api/params/defaults", r.status_code == 200 and "model" in r.json())

        # build-command：反向 flag、联动、默认值跳过
        print("== build-command ==")
        r = await c.post("/api/params/build-command", json={"params": {
            "model": r"D:\m.gguf", "ctx_size": 8192, "mmap": False}})
        cmd = r.json().get("command", "")
        check("mmap=False → --no-mmap", r.status_code == 200 and "--no-mmap" in cmd, cmd)
        r = await c.post("/api/params/build-command", json={"params": {
            "model": r"D:\m.gguf", "ctx_size": 8192, "mmap": True}})
        cmd = r.json().get("command", "")
        check("mmap=True 不输出 --no-mmap", "--no-mmap" not in cmd, cmd)
        r = await c.post("/api/params/build-command", json={"params": {
            "model": r"D:\m.gguf", "ctx_size": 8192, "threads": -1, "n_predict": -1, "seed": -1}})
        cmd = r.json().get("command", "")
        check("默认值 -1 全部跳过", "-t -1" not in cmd and "--seed -1" not in cmd, cmd)
        r = await c.post("/api/params/build-command", json={"params": {
            "model": r"D:\m.gguf", "ctx_size": 8192, "threads": 8}})
        cmd = r.json().get("command", "")
        check("threads=8 输出 -t 8", "-t 8" in cmd, cmd)
        # 联动：rope_scaling
        r = await c.post("/api/params/build-command", json={"params": {
            "model": r"D:\m.gguf", "ctx_size": 8192, "rope_scaling": "yarn"}})
        cmd = r.json().get("command", "")
        check("rope_scaling=yarn 输出", "--rope-scaling yarn" in cmd, cmd)

        # ── 预设 ──
        print("== 预设 CRUD ==")
        r = await c.get("/api/presets")
        presets = r.json()
        check("GET /api/presets", r.status_code == 200 and len(presets) >= 3,
              f"count={len(presets)}")
        pid0 = presets[0]["id"]
        r = await c.get(f"/api/presets/{pid0}")
        check("GET /api/presets/{id}", r.status_code == 200 and "params" in r.json())
        # 创建（model 为必填设计语义，用虚拟路径）
        test_preset = {"name": "冒烟测试预设", "description": "smoke", "tags": ["smoke"],
                       "params": {"model": "D:\\smoke_model.gguf", "ctx_size": 4096}}
        r = await c.post("/api/presets", json={**test_preset, "params": {"model": "", "ctx_size": 4096}})
        check("POST /api/presets 无模型→400(设计语义)", r.status_code == 400)
        r = await c.post("/api/presets", json=test_preset)
        check("POST /api/presets", r.status_code == 200 and "id" in r.json())
        new_id = r.json()["id"]
        # 更新
        r = await c.put(f"/api/presets/{new_id}", json={**test_preset, "ctx_size": 8192})
        check("PUT /api/presets/{id}", r.status_code == 200)
        # 克隆
        r = await c.post(f"/api/presets/{new_id}/clone", json={"name": "冒烟克隆"})
        check("POST /api/presets/{id}/clone", r.status_code == 200 and "id" in r.json())
        clone_id = r.json()["id"]
        # diff
        r = await c.post("/api/presets/diff", json={"id1": pid0, "id2": new_id})
        check("POST /api/presets/diff", r.status_code == 200)
        r = await c.post("/api/presets/diff", json={"id1": pid0})
        check("POST /api/presets/diff 缺参→400", r.status_code == 400)
        # export
        r = await c.get(f"/api/presets/{pid0}/export")
        check("GET /api/presets/{id}/export", r.status_code == 200
              and "params" in r.json())
        # import
        r = await c.post("/api/presets/import", json={
            "name": "冒烟导入", "params": {"model": "", "ctx_size": 2048}})
        check("POST /api/presets/import", r.status_code == 200 and "id" in r.json())
        imp_id = r.json()["id"]
        # 404 路径
        r = await c.get("/api/presets/no_such_id")
        check("GET 不存在预设→404", r.status_code == 404)
        # 清理测试预设
        for tid in (new_id, clone_id, imp_id):
            await c.delete(f"/api/presets/{tid}")

        # ── 模型扫描 ──
        print("== 模型扫描 ==")
        r = await c.get("/api/models/scan", params={"directory": ""})
        check("GET /api/models/scan 空目录", r.status_code == 200 and "models" in r.json())
        r = await c.get("/api/models/browse", params={"directory": r"C:\\"})
        check("GET /api/models/browse", r.status_code == 200 and "items" in r.json())

        # ── 实例错误路径 ──
        print("== 实例错误路径 ==")
        r = await c.post("/api/instances", json={"params": None})
        check("无 params→400", r.status_code == 400)
        r = await c.post("/api/instances", json={"params": {"model": "", "port": None}})
        check("空模型→400", r.status_code == 400)
        r = await c.post("/api/instances", json={"params": {"model": r"D:\x.gguf", "port": 99999}})
        check("端口超范围→400", r.status_code == 400)
        r = await c.post("/api/instances", json={"params": {"model": r"D:\x.gguf", "port": "abc"}})
        check("端口非数字→400(非500)", r.status_code == 400, f"status={r.status_code}")
        r = await c.post("/api/instances/no_such/stop")
        check("停止不存在实例→400", r.status_code == 400)
        r = await c.get("/api/instances/no_such")
        check("GET 不存在实例→404", r.status_code == 404)
        r = await c.get("/api/instances")
        check("GET /api/instances", r.status_code == 200 and isinstance(r.json(), list))

        # ── 历史 ──
        print("== 历史 ==")
        r = await c.get("/api/history")
        check("GET /api/history", r.status_code == 200 and isinstance(r.json(), list))
        # 注意：不执行 clear，保护开发机现有历史数据
        r = await c.get("/api/history/999999")
        check("GET 不存在历史→404", r.status_code == 404)

        # ── Chat ──
        print("== Chat ==")
        r = await c.get("/api/chat/instances")
        check("GET /api/chat/instances", r.status_code == 200 and isinstance(r.json(), list))

        # ── SPA fallback ──
        print("== SPA fallback ==")
        r = await c.get("/api/undefined_endpoint_xyz")
        check("未定义 /api 路径→404 JSON", r.status_code == 404
              and r.headers.get("content-type", "").startswith("application/json"),
              f"status={r.status_code} ct={r.headers.get('content-type')}")
        # 冻结模式（backend.exe）不挂载前端，/ 与 /index.html 天然 404（文档 6.3.4）
        r = await c.get("/index.html")
        check("GET /index.html（开发 200 / 冻结 404）",
              r.status_code in (200, 404), f"status={r.status_code}")

        # ── WebSocket metrics ──
        print("== WebSocket ==")
        async with websockets.connect(f"{WS}/ws/system/metrics") as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            check("ws/metrics 推送", "cpu_percent" in msg and "timestamp" in msg)
        # 不存在实例的 logs ws 应被关闭（1008）
        try:
            async with websockets.connect(f"{WS}/ws/instances/no_such/logs") as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)
                check("ws/logs 不存在实例→关闭", False, "收到消息而非关闭")
        except websockets.exceptions.ConnectionClosedError as e:
            check("ws/logs 不存在实例→1008", e.rcvd and e.rcvd.code == 1008, str(e))
        except Exception as e:
            check("ws/logs 不存在实例→1008", False, f"{type(e).__name__}: {e}")

    print(f"\n===== 结果: {passed} 通过, {failed} 失败 =====")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
