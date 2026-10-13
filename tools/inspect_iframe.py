import asyncio
import json

import requests
import websockets

r = requests.get("http://127.0.0.1:9222/json", timeout=5)
iframe = next(
    p
    for p in r.json()
    if p.get("type") == "iframe" and "127.0.0.1" in (p.get("url") or "")
)

async def main():
    async with websockets.connect(iframe["webSocketDebuggerUrl"]) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        await asyncio.sleep(0.3)
        js = """(function() {
          const btns = Array.from(document.querySelectorAll('button, a')).slice(0,30);
          return btns.map(b => ({
            tag: b.tagName,
            html: b.outerHTML.substring(0,300),
            title: b.title,
            aria: b.getAttribute('aria-label'),
            text: b.textContent.trim().substring(0,50)
          }));
        })();"""
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
                print(
                    json.dumps(
                        msg.get("result", {}).get("result", {}).get("value", []),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                break

asyncio.run(main())
