"""实例生命周期测试（真实 llama-server）

验证：启动 → RUNNING → /health → 双实例端口自动分配 → 停止 → 历史记录。
运行前提：后端已运行在 127.0.0.1:17520。
"""
import asyncio
import pathlib
import sys
import time

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _local_model import require_model_path  # noqa: E402

B = "http://127.0.0.1:17520"
MODEL = require_model_path()

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {detail}")


async def wait_status(c, iid, target, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = await c.get(f"/api/instances/{iid}")
        j = r.json()
        if j.get("status") == target:
            return j
        if j.get("status") in ("failed",):
            return j
        await asyncio.sleep(2)
    return None


async def main():
    async with httpx.AsyncClient(base_url=B, timeout=15) as c:
        params = {
            "model": MODEL,
            "ctx_size": 2048,
            "n_gpu_layers": "99",     # 全 GPU（RTX 5060 8GB）
            "port": 0,                # 自动分配
            "threads": 8,
        }

        # 1. 启动实例 1（自动分配端口）
        print("== 启动实例 1（自动分配端口）==")
        r = await c.post("/api/instances", json={"params": params, "name": "冒烟实例1"})
        check("POST /api/instances", r.status_code == 200, r.text[:200])
        j = r.json()
        iid1 = j["id"]
        port1 = j.get("port")
        check("实例1 端口自动分配 (8080-8090)", isinstance(port1, int) and 8080 <= port1 <= 8090, f"port={port1}")

        j = await wait_status(c, iid1, "running", timeout=90)
        check("实例1 → RUNNING", j is not None and j.get("status") == "running",
              f"status={j.get('status') if j else None} err={j.get('error_message') if j else ''}")

        # 2. 探测 llama-server /health
        if j and j.get("port"):
            try:
                hr = await c.get(f"http://127.0.0.1:{j['port']}/health")
                check("llama-server /health 200", hr.status_code == 200, f"status={hr.status_code}")
                check("/health 返回 ok", "ok" in hr.text, hr.text[:60])
            except Exception as e:
                check("llama-server /health 200", False, str(e))

        # 3. 启动实例 2（并发自动分配不冲突）
        print("== 启动实例 2（端口不冲突）==")
        r2 = await c.post("/api/instances", json={"params": params, "name": "冒烟实例2"})
        check("POST 实例2", r2.status_code == 200, r2.text[:200])
        j2 = r2.json()
        iid2 = j2["id"]
        port2 = j2.get("port")
        check("实例2 端口不同", port2 != port1, f"p1={port1} p2={port2}")

        j2b = await wait_status(c, iid2, "running", timeout=90)
        check("实例2 → RUNNING", j2b is not None and j2b.get("status") == "running",
              f"status={j2b.get('status') if j2b else None}")

        # 4. 实例列表
        r = await c.get("/api/instances")
        lst = r.json()
        check("实例列表含 2 个", len(lst) >= 2, f"count={len(lst)}")

        # 5. 显式端口占用报错
        print("== 显式端口冲突 ==")
        used_port = j.get("port") if j else port1
        r = await c.post("/api/instances", json={"params": {**params, "port": used_port}})
        check("显式占用端口→400", r.status_code == 400, f"status={r.status_code} {r.text[:100]}")

        # 6. 停止实例 1 → 端口释放
        print("== 停止与释放 ==")
        r = await c.post(f"/api/instances/{iid1}/stop")
        check("停止实例1", r.status_code == 200 and r.json().get("status") == "stopped", r.text[:120])
        time.sleep(1)
        r = await c.post("/api/instances", json={"params": {**params, "port": used_port}})
        check("端口释放后复用→200", r.status_code == 200, f"status={r.status_code} {r.text[:100]}")
        if r.status_code == 200:
            iid3 = r.json()["id"]
            j3 = await wait_status(c, iid3, "running", timeout=90)
            check("实例3 → RUNNING", j3 is not None and j3.get("status") == "running")
            await c.post(f"/api/instances/{iid3}/stop")

        # 7. 停止实例 2 + 清理
        await c.post(f"/api/instances/{iid2}/stop")

        # 8. 历史记录
        print("== 历史记录 ==")
        r = await c.get("/api/history?limit=10")
        hist = r.json()
        check("历史列表非空", isinstance(hist, list) and len(hist) > 0, f"count={len(hist)}")
        rec = None
        for h in hist:
            if h.get("instance_id") == iid1:
                rec = h
                break
        if rec:
            check("历史记录状态→stopped", rec.get("status") == "stopped", str(rec.get("status")))
            check("历史记录含 duration", "duration_seconds" in rec, str(list(rec.keys())))
            # 列表接口为轻量设计不含 command；详情接口才含完整 command+params
            d = (await c.get(f"/api/history/{rec['id']}")).json()
            check("历史详情含 command", bool(d.get("command_text")), "")
            check("历史详情含 params", isinstance(d.get("params"), dict), "")
        else:
            check("历史记录状态→stopped", False, "未找到实例1记录")

        # 9. 日志 WebSocket（实例已停止，验证历史日志补发）
        import websockets
        print("== 日志 WS ==")
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:17520/ws/instances/{iid1}/logs",
                open_timeout=8,
            ) as ws:
                first = await asyncio.wait_for(ws.recv(), timeout=8)
                check("ws/logs 补发历史", first is not None and ("line" in first or "type" in first))
                await ws.close()
        except Exception as e:
            check("ws/logs 补发历史", False, str(e))

    print(f"\n===== 生命周期结果: {passed} 通过, {failed} 失败 =====")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
