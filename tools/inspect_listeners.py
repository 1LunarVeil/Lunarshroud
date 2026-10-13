"""检查页面上某个元素的事件监听器（CDP DOMDebugger.getEventListeners）。"""
import asyncio
import json
import sys

import requests
import websockets


async def main():
    selector = sys.argv[1] if len(sys.argv) > 1 else '[data-key="ctx_size"]'
    r = requests.get("http://127.0.0.1:9222/json", timeout=5)
    pages = r.json()
    ws_url = pages[0]["webSocketDebuggerUrl"]
    async with websockets.connect(ws_url) as ws:
        async def send(method, params=None):
            cmd = {"id": 1, "method": method}
            if params:
                cmd["params"] = params
            await ws.send(json.dumps(cmd))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 1:
                    return msg

        await send("Runtime.enable")
        expr = f'document.querySelector({json.dumps(selector)})'
        res = await send("Runtime.evaluate", {"expression": expr, "objectGroup": "test"})
        if "exceptionDetails" in res.get("result", {}):
            print("EVAL ERROR:", res["result"]["exceptionDetails"])
            return
        obj_id = res["result"]["result"].get("objectId")
        if not obj_id:
            print("元素未找到或 objectId 缺失")
            return
        listeners = await send("DOMDebugger.getEventListeners", {"objectId": obj_id})
        data = listeners.get("result", {}).get("listeners", [])
        print(json.dumps([{"type": item.get("type"), "useCapture": item.get("useCapture"), "handler": item.get("handler", {}).get("description", "")[:80]} for item in data], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
