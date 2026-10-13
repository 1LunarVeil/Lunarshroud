import asyncio
import json
import sys

import requests
import websockets

r = requests.get("http://127.0.0.1:9222/json", timeout=5)
iframe = next(
    p
    for p in r.json()
    if p.get("type") == "iframe" and "127.0.0.1" in (p.get("url") or "")
)

action = sys.argv[1] if len(sys.argv) > 1 else "new-chat"

async def main():
    async with websockets.connect(iframe["webSocketDebuggerUrl"]) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        await asyncio.sleep(0.5)

        if action == "new-chat":
            js = """(function() {
              const btn = document.querySelector('button[aria-label="New chat"]');
              if (btn) { btn.click(); return {found: true, aria: btn.getAttribute('aria-label')}; }
              return {found: false, buttons: Array.from(document.querySelectorAll('button')).slice(0,5).map(b=>b.getAttribute('aria-label'))};
            })();"""
        elif action == "history":
            js = """(function() {
              const btn = document.querySelector('button[aria-label="Search"]');
              if (btn) { btn.click(); return {found: true}; }
              return {found: false};
            })();"""
        elif action == "screenshot":
            out = sys.argv[2] if len(sys.argv) > 2 else "iframe.png"
            await ws.send(json.dumps({"id": 2, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == 2:
                    import base64
                    img_data = base64.b64decode(msg["result"]["data"])
                    with open(out, "wb") as f:
                        f.write(img_data)
                    print(f"[截图] {out}")
                    return
        else:
            js = sys.argv[2]

        await ws.send(
            json.dumps(
                {
                    "id": 2,
                    "method": "Runtime.evaluate",
                    "params": {"expression": js, "returnByValue": True},
                }
            )
        )
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 2:
                print(json.dumps(msg.get("result", {}).get("result", {}).get("value"), ensure_ascii=False))
                break

asyncio.run(main())
