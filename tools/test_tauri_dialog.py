"""直接通过 CDP 调用 Tauri open_file_dialog 命令并等待结果，同时用 pyautogui 操作对话框。"""
import asyncio
import ctypes
import json
import pathlib
import sys
import time
from ctypes import wintypes

import pyautogui
import requests
import websockets

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _local_model import require_model_path  # noqa: E402

user32 = ctypes.windll.user32
WM_CLOSE = 0x0010


def find_window(title):
    result = []

    def enum_proc(hwnd, extra):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == title:
                    result.append(hwnd)
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(enum_proc), 0)
    return result[0] if result else None


def activate(hwnd):
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def close_dialog():
    hwnd = find_window("打开")
    if hwnd:
        user32.SendMessageW(hwnd, WM_CLOSE, 0, 0)
        time.sleep(0.5)


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


async def main():
    file_path = sys.argv[1] if len(sys.argv) > 1 else require_model_path()

    close_dialog()

    ws_url = get_ws_debug_url()
    async with websockets.connect(ws_url) as ws:
        await send_cmd(ws, "Runtime.enable")
        await asyncio.sleep(0.3)

        js = """
        (async () => {
          try {
            const r = await window.__TAURI__.core.invoke('open_file_dialog');
            window.__dialogResult = r;
            return r;
          } catch (e) {
            window.__dialogError = String(e);
            throw e;
          }
        })()
        """

        # 开始执行 invoke（不等待，因为 dialog 会阻塞一段时间）
        task = asyncio.create_task(send_cmd(ws, "Runtime.evaluate", {"expression": js, "awaitPromise": True, "returnByValue": True}))

        # 等待 dialog 出现
        start = time.time()
        dialog_hwnd = None
        while time.time() - start < 10:
            dialog_hwnd = find_window("打开")
            if dialog_hwnd:
                break
            await asyncio.sleep(0.2)

        if not dialog_hwnd:
            print("dialog not found")
            task.cancel()
            return

        print(f"dialog found: {dialog_hwnd}")
        activate(dialog_hwnd)
        time.sleep(0.3)

        # 尝试多种方式操作 dialog
        # 方法1: Alt+N 聚焦文件名输入框
        pyautogui.keyDown('alt')
        pyautogui.keyDown('n')
        pyautogui.keyUp('n')
        pyautogui.keyUp('alt')
        time.sleep(0.3)
        pyautogui.typewrite(file_path, interval=0.01)
        time.sleep(0.3)
        pyautogui.press('enter')
        time.sleep(1.5)

        # 等待 invoke 返回
        try:
            data = await asyncio.wait_for(task, timeout=15)
            print("evaluate result:", json.dumps(data, indent=2, ensure_ascii=False))
        except TimeoutError:
            print("invoke timeout")
            close_dialog()


if __name__ == "__main__":
    asyncio.run(main())
