"""LunarShroud 自动化测试助手

通过 Windows API 定位 Tauri 窗口，使用 pyautogui 进行点击、截图等操作。
所有坐标均相对于窗口客户区（不含标题栏和边框）。
"""

import ctypes
import sys
import time
from ctypes import wintypes

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.2
except ImportError:
    print("请先安装 pyautogui: pip install pyautogui")
    sys.exit(1)

try:
    from PIL import ImageGrab
except ImportError:
    print("PIL not available")
    sys.exit(1)

user32 = ctypes.windll.user32

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def find_window(title_substring="LunarShroud"):
    result = []

    def enum_proc(hwnd, extra):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                text = buf.value
                if title_substring.lower() in text.lower():
                    result.append((hwnd, text))
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(enum_proc), 0)
    return result


def get_window_rect(hwnd):
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


def get_client_rect(hwnd):
    """返回客户区相对窗口左上角的矩形"""
    rect = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return {
        "left": pt.x,
        "top": pt.y,
        "width": rect.right - rect.left,
        "height": rect.bottom - rect.top,
    }


def activate_window(hwnd):
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def screenshot_client(hwnd, output_path):
    """截取窗口客户区"""
    activate_window(hwnd)
    cr = get_client_rect(hwnd)
    img = ImageGrab.grab(bbox=(cr["left"], cr["top"], cr["left"] + cr["width"], cr["top"] + cr["height"]))
    img.save(output_path)
    print(f"[截图] {output_path} ({cr['width']}x{cr['height']})")
    return output_path


def click_client(hwnd, x, y, clicks=1):
    """点击客户区相对坐标"""
    cr = get_client_rect(hwnd)
    sx = cr["left"] + x
    sy = cr["top"] + y
    activate_window(hwnd)
    pyautogui.click(sx, sy, clicks=clicks)


def type_text(text):
    pyautogui.typewrite(text, interval=0.01)


def press_key(key):
    pyautogui.press(key)


# 根据当前窗口大小计算的 UI 位置（客户区坐标）
classUILayout = {
    "header_height": 48,
    "sidebar_width": 60,
    "sidebar_item_height": 42,
    "sidebar_top_padding": 6,
}


def sidebar_item_center(index):
    """返回第 index 个侧边栏项的中心坐标（从 0 开始）"""
    layout = classUILayout
    x = layout["sidebar_width"] // 2
    y = layout["sidebar_top_padding"] + layout["header_height"] + layout["sidebar_item_height"] * index + layout["sidebar_item_height"] // 2
    return x, y


def get_app_window():
    wins = find_window("LunarShroud")
    if not wins:
        raise RuntimeError("未找到 LunarShroud 窗口")
    # 选择标题完全匹配或最匹配的
    for hwnd, text in wins:
        if text == "LunarShroud":
            return hwnd
    return wins[0][0]


def main():
    if len(sys.argv) < 2:
        print("用法: python app_tester.py <cmd> [args]")
        print("  screenshot <out.png>")
        print("  click-sidebar <index>")
        print("  click <x> <y>")
        print("  type <text>")
        sys.exit(1)

    cmd = sys.argv[1]
    hwnd = get_app_window()

    if cmd == "screenshot":
        out = sys.argv[2] if len(sys.argv) > 2 else "app_screenshot.png"
        screenshot_client(hwnd, out)
    elif cmd == "click-sidebar":
        idx = int(sys.argv[2])
        x, y = sidebar_item_center(idx)
        click_client(hwnd, x, y)
        print(f"[点击侧边栏] index={idx} ({x}, {y})")
    elif cmd == "click":
        x, y = int(sys.argv[2]), int(sys.argv[3])
        click_client(hwnd, x, y)
        print(f"[点击] ({x}, {y})")
    elif cmd == "type":
        type_text(sys.argv[2])
        print(f"[输入] {sys.argv[2]}")
    elif cmd == "press":
        press_key(sys.argv[2])
    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
