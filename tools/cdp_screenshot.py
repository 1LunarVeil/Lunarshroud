"""通过 WebView2 远程调试端口 (CDP) 截取页面截图。"""

import asyncio
import base64
import json
import sys

import requests
import websockets


def get_ws_debug_url():
    r = requests.get("http://127.0.0.1:9222/json", timeout=5)
    pages = r.json()
    for page in pages:
        if page.get("type") == "page":
            return page["webSocketDebuggerUrl"]
    raise RuntimeError("未找到 page 调试目标")


async def capture_screenshot(output_path):
    ws_url = get_ws_debug_url()
    async with websockets.connect(ws_url) as ws:
        # 等待页面加载完成
        await asyncio.sleep(0.5)

        # 发送 Page.captureScreenshot
        cmd = {"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}}
        await ws.send(json.dumps(cmd))

        # 接收响应
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            if data.get("id") == 1:
                if "result" in data and "data" in data["result"]:
                    img_data = base64.b64decode(data["result"]["data"])
                    with open(output_path, "wb") as f:
                        f.write(img_data)
                    print(f"[截图] {output_path}")
                    return
                else:
                    raise RuntimeError(f"截图失败: {data}")


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "cdp_screenshot.png"
    asyncio.run(capture_screenshot(out))


if __name__ == "__main__":
    main()
