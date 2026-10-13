"""通过 WebView2 远程调试端口 (CDP) 操作页面并截图。

支持命令：
  screenshot <out.png>          截取当前页面
  click <selector>              点击指定 CSS 选择器的元素
  click-sidebar <index>         点击第 index 个侧边栏项（0-7）
  eval <js>                     执行 JS 并打印结果
  eval-async <js>               执行异步 JS 并等待 Promise 结果
  type <selector> <text>        在 input/textarea 中输入文本
  wait <seconds>                等待
"""

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


async def send_cmd(ws, method, params=None):
    cmd_id = getattr(send_cmd, "_id", 0) + 1
    send_cmd._id = cmd_id
    cmd = {"id": cmd_id, "method": method}
    if params:
        cmd["params"] = params
    await ws.send(json.dumps(cmd))
    while True:
        msg = await ws.recv()
        data = json.loads(msg)
        if data.get("id") == cmd_id:
            return data


async def capture_screenshot(ws, output_path):
    data = await send_cmd(ws, "Page.captureScreenshot", {"format": "png"})
    if "result" in data and "data" in data["result"]:
        img_data = base64.b64decode(data["result"]["data"])
        with open(output_path, "wb") as f:
            f.write(img_data)
        print(f"[截图] {output_path}")
    else:
        raise RuntimeError(f"截图失败: {data}")


async def eval_js(ws, js):
    data = await send_cmd(ws, "Runtime.evaluate", {"expression": js, "returnByValue": True})
    result = data.get("result", {}).get("result", {})
    if "value" in result:
        return result["value"]
    if "description" in result:
        return result["description"]
    return result


async def eval_js_async(ws, js):
    """执行异步 JS 并等待 Promise 结果"""
    data = await send_cmd(ws, "Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True})
    result = data.get("result", {}).get("result", {})
    if "value" in result:
        return result["value"]
    if "description" in result:
        return result["description"]
    # 若执行抛异常，result 中会有 exceptionDetails
    if data.get("result", {}).get("exceptionDetails"):
        details = data["result"]["exceptionDetails"]
        return "EXCEPTION: " + details.get("text", "") + " " + str(details.get("exception", {}).get("description", ""))
    return result


async def click_selector(ws, selector):
    js = f"""
    (function() {{
      const el = document.querySelector({json.dumps(selector)});
      if (!el) return 'NOT_FOUND';
      el.click();
      return 'OK';
    }})();
    """
    result = await eval_js(ws, js)
    print(f"[点击] {selector} => {result}")
    return result


async def type_text(ws, selector, text):
    js = f"""
    (function() {{
      const el = document.querySelector({json.dumps(selector)});
      if (!el) return 'NOT_FOUND';
      el.focus();
      el.value = {json.dumps(text)};
      el.dispatchEvent(new Event('input', {{ bubbles: true }}));
      el.dispatchEvent(new Event('change', {{ bubbles: true }}));
      return 'OK';
    }})();
    """
    result = await eval_js(ws, js)
    print(f"[输入] {selector} => {result}")
    return result


async def click_at(ws, x, y):
    """使用 CDP Input.dispatchMouseEvent 在视口坐标 (x,y) 点击"""
    await send_cmd(ws, "Input.dispatchMouseEvent", {
        "type": "mousePressed",
        "x": x,
        "y": y,
        "button": "left",
        "clickCount": 1,
    })
    await asyncio.sleep(0.05)
    await send_cmd(ws, "Input.dispatchMouseEvent", {
        "type": "mouseReleased",
        "x": x,
        "y": y,
        "button": "left",
        "clickCount": 1,
    })
    print(f"[点击坐标] ({x}, {y})")


async def bring_to_front(ws):
    await send_cmd(ws, "Page.bringToFront")
    print("[前置窗口]")


async def click_sidebar(ws, index):
    js = f"""
    (function() {{
      const index = {index};
      const items = document.querySelectorAll('.sidebar-item');
      if (index < 0 || index >= items.length) return 'OUT_OF_RANGE: ' + items.length;
      items[index].click();
      return 'OK';
    }})();
    """
    result = await eval_js(ws, js)
    print(f"[点击侧边栏] index={index} => {result}")
    return result


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    ws_url = get_ws_debug_url()

    async with websockets.connect(ws_url) as ws:
        # 启用 Runtime / Page domain
        await send_cmd(ws, "Runtime.enable")
        await send_cmd(ws, "Page.enable")
        await asyncio.sleep(0.3)

        if cmd == "screenshot":
            out = sys.argv[2] if len(sys.argv) > 2 else "cdp_screenshot.png"
            await capture_screenshot(ws, out)
        elif cmd == "click":
            selector = sys.argv[2]
            await click_selector(ws, selector)
        elif cmd == "click-at":
            x = float(sys.argv[2])
            y = float(sys.argv[3])
            await click_at(ws, x, y)
        elif cmd == "click-sidebar":
            idx = int(sys.argv[2])
            await click_sidebar(ws, idx)
        elif cmd == "eval":
            js = sys.argv[2]
            result = await eval_js(ws, js)
            print(result)
        elif cmd == "eval-async":
            js = sys.argv[2]
            result = await eval_js_async(ws, js)
            print(result)
        elif cmd == "type":
            selector = sys.argv[2]
            text = sys.argv[3]
            await type_text(ws, selector, text)
        elif cmd == "bring-to-front":
            await bring_to_front(ws)
        elif cmd == "wait":
            sec = float(sys.argv[2])
            await asyncio.sleep(sec)
        else:
            print(f"未知命令: {cmd}")


if __name__ == "__main__":
    asyncio.run(main())
