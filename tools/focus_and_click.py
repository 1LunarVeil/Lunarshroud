"""激活 LunarShroud 窗口并在指定客户端坐标点击。"""
import ctypes
import sys
import time
from ctypes import wintypes

try:
    import pyautogui
except ImportError:
    pyautogui = None

user32 = ctypes.windll.user32

class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

def find_main_window():
    result = []
    def enum_proc(hwnd, extra):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value == "LunarShroud":
                    result.append(hwnd)
        return True
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(enum_proc), 0)
    if not result:
        raise RuntimeError("未找到 LunarShroud 窗口")
    return result[0]

def client_to_screen(hwnd, cx, cy):
    pt = POINT()
    pt.x = int(cx)
    pt.y = int(cy)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y

def main():
    focus_only = "--focus-only" in sys.argv
    if focus_only:
        hwnd = find_main_window()
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        print("窗口已前置")
        time.sleep(0.3)
        return
    if len(sys.argv) < 3:
        print("用法: python focus_and_click.py <client_x> <client_y> [--focus-only]")
        sys.exit(1)
    cx, cy = float(sys.argv[1]), float(sys.argv[2])
    hwnd = find_main_window()
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    sx, sy = client_to_screen(hwnd, cx, cy)
    print(f"点击屏幕坐标: ({sx}, {sy})")
    if pyautogui:
        pyautogui.click(sx, sy)
    else:
        user32.SetCursorPos(sx, sy)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.3)

if __name__ == "__main__":
    main()
