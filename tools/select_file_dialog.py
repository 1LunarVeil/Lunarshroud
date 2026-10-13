"""点击浏览按钮后，在弹出的文件对话框中输入路径并确认，最后验证输入框值。"""
import ctypes
import pathlib
import subprocess
import sys
import time
from ctypes import wintypes

import pyautogui

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


def find_main_window():
    hwnd = find_window("LunarShroud")
    if not hwnd:
        raise RuntimeError("未找到 LunarShroud 窗口")
    return hwnd


def activate(hwnd):
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def close_dialog():
    """关闭可能残留的文件对话框。"""
    hwnd = find_window("打开")
    if hwnd:
        user32.SendMessageW(hwnd, WM_CLOSE, 0, 0)
        time.sleep(0.5)


def list_visible_windows():
    """列出所有可见窗口标题（用于调试）。"""
    result = []

    def enum_proc(hwnd, extra):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                result.append(buf.value)
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(enum_proc), 0)
    return result


def wait_for_dialog(timeout=15):
    """等待文件对话框出现。"""
    start = time.time()
    while time.time() - start < timeout:
        hwnd = find_window("打开")
        if hwnd:
            print(f"[dialog found] hwnd={hwnd}")
            return hwnd
        time.sleep(0.2)
    print("[visible windows]", list_visible_windows())
    return None


def set_input_value(selector, value):
    """通过 CDP 设置 input 的值。"""
    subprocess.run(
        ["python", "tools/cdp_tester.py", "type", selector, value],
        check=True,
    )


def eval_js(js):
    """通过 CDP 执行 JS 并返回结果。"""
    result = subprocess.run(
        ["python", "tools/cdp_tester.py", "eval", js],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main():
    selector = sys.argv[1] if len(sys.argv) > 1 else "button[data-browse='model']"
    file_path = sys.argv[2] if len(sys.argv) > 2 else require_model_path()
    out = sys.argv[3] if len(sys.argv) > 3 else "shots/file_select.png"

    # 清理残留对话框
    close_dialog()

    # 激活主窗口并点击浏览按钮
    main_hwnd = find_main_window()
    activate(main_hwnd)
    subprocess.run(["python", "tools/cdp_tester.py", "click", selector], check=True)

    # 等待文件对话框出现
    dialog_hwnd = wait_for_dialog(timeout=10)
    if not dialog_hwnd:
        raise RuntimeError("文件对话框未弹出")

    # 激活对话框并输入路径
    activate(dialog_hwnd)
    time.sleep(0.3)

    # 聚焦文件名输入框（Alt+N）并输入路径
    pyautogui.keyDown('alt')
    pyautogui.keyDown('n')
    pyautogui.keyUp('n')
    pyautogui.keyUp('alt')
    time.sleep(0.3)
    pyautogui.typewrite(file_path, interval=0.01)
    time.sleep(0.3)
    pyautogui.press('enter')
    time.sleep(1.5)

    # 等待对话框关闭
    start = time.time()
    while time.time() - start < 5:
        if not find_window("打开"):
            break
        time.sleep(0.2)

    # 截图验证
    subprocess.run(["python", "tools/cdp_screenshot.py", out], check=True)
    print(f"[截图] {out}")

    # 验证输入框值
    value = eval_js("document.querySelector('[data-key=model] input')?.value || 'EMPTY'")
    print(f"[输入框值] {value}")
    if file_path not in value:
        raise RuntimeError(f"输入框值不匹配: {value}")


if __name__ == "__main__":
    main()
