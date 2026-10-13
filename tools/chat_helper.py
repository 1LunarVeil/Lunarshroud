"""操作 chat 页面 iframe 内的元素。"""

import asyncio
import json
import sys

import requests
import websockets


def get_ws_debug_url(target_type="page", url_contains=None):
    r = requests.get("http://127.0.0.1:9222/json", timeout=5)
    pages = r.json()
    candidates = []
    for page in pages:
        if page.get("type") != target_type:
            continue
        url = page.get("url") or ""
        if url_contains and url_contains not in url:
            continue
        candidates.append((url, page["webSocketDebuggerUrl"]))
    if not candidates:
        raise RuntimeError(f"未找到 target_type={target_type} url_contains={url_contains} 的调试目标")
    # iframe 优先选 127.0.0.1 本地实例，其次选第一个
    if target_type == "iframe":
        for url, ws_url in candidates:
            if "127.0.0.1" in url:
                return ws_url
    return candidates[0][1]


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


async def eval_js(ws, js):
    data = await send_cmd(ws, "Runtime.evaluate", {"expression": js, "returnByValue": True})
    result = data.get("result", {}).get("result", {})
    if "value" in result:
        return result["value"]
    if "description" in result:
        return result["description"]
    return result


async def main():
    action = sys.argv[1]
    # iframe 操作使用 iframe 目标，页面操作使用 page 目标
    target_type = "iframe" if action in ("close-mcp", "type-message", "send-message", "new-chat") else "page"
    ws_url = get_ws_debug_url(target_type=target_type)
    async with websockets.connect(ws_url) as ws:
        await send_cmd(ws, "Runtime.enable")
        await asyncio.sleep(0.3)

        if action == "close-mcp":
            # 在 iframe 目标内直接操作 document
            js = """
            (function() {
              const doc = document;
              // 方法1：找 aria-label 为 Close 的按钮
              let btn = doc.querySelector('button[aria-label="Close"]');
              // 方法2：找包含 'Not now' 文本的按钮
              if (!btn) {
                const buttons = doc.querySelectorAll('button');
                for (const b of buttons) {
                  if (b.textContent.trim() === 'Not now') { btn = b; break; }
                }
              }
              // 方法3：点击弹窗遮罩层
              if (!btn) {
                const overlay = doc.querySelector('.modal-overlay, [role="dialog"]');
                if (overlay) {
                  const ev = new MouseEvent('click', { bubbles: true });
                  overlay.dispatchEvent(ev);
                  return 'CLICKED_OVERLAY';
                }
              }
              if (btn) { btn.click(); return 'CLICKED_BTN'; }
              return 'NOT_FOUND';
            })();
            """
            result = await eval_js(ws, js)
            print(f"[关闭 MCP 弹窗] {result}")
        elif action == "new-chat":
            js = """
            (function() {
              const btn = document.querySelector('button[aria-label="New chat"]');
              if (btn) { btn.click(); return 'OK'; }
              return 'NOT_FOUND';
            })();
            """
            result = await eval_js(ws, js)
            print(f"[新建对话] {result}")
        elif action == "type-message":
            text = sys.argv[2]
            js = f"""
            (function() {{
              const doc = document;
              // 常见输入框选择器
              let input = doc.querySelector('textarea[placeholder*="message"], textarea[placeholder*="Message"], #message-input, .message-input, textarea');
              if (!input) return 'NO_INPUT';
              input.focus();
              input.value = {json.dumps(text)};
              input.dispatchEvent(new Event('input', {{ bubbles: true }}));
              return 'OK';
            }})();
            """
            result = await eval_js(ws, js)
            print(f"[输入消息] {result}")
        elif action == "send-message":
            js = """
            (function() {
              const doc = document;
              // 常见发送按钮选择器
              let btn = doc.querySelector('button[type="submit"], button[aria-label="Send"], .send-button');
              if (!btn) {
                const buttons = doc.querySelectorAll('button');
                for (const b of buttons) {
                  if (b.textContent.trim().toLowerCase().includes('send')) { btn = b; break; }
                }
              }
              if (btn) { btn.click(); return 'OK'; }
              return 'NO_SEND_BTN';
            })();
            """
            result = await eval_js(ws, js)
            print(f"[发送消息] {result}")
        elif action == "screenshot":
            out = sys.argv[2] if len(sys.argv) > 2 else "chat_iframe.png"
            data = await send_cmd(ws, "Page.captureScreenshot", {"format": "png"})
            if "result" in data and "data" in data["result"]:
                import base64
                img_data = base64.b64decode(data["result"]["data"])
                with open(out, "wb") as f:
                    f.write(img_data)
                print(f"[截图] {out}")


if __name__ == "__main__":
    asyncio.run(main())
