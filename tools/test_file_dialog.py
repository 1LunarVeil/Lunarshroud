"""测试 Tauri 文件对话框是否能正常弹出。"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32

class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

def find_main_window():
    result = []
    def enum_proc(hwnd, extra):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                text = buf.value
                if text == "LunarShroud":
                    rect = RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    result.append((hwnd, w, h))
        return True
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(enum_proc), 0)
    if not result:
        raise RuntimeError("未找到 LunarShroud 窗口")
    # 选最大的窗口
    result.sort(key=lambda x: x[1] * x[2], reverse=True)
    return result[0][0]

def activate(hwnd):
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)

def main():
    selector = sys.argv[1] if len(sys.argv) > 1 else "button[data-browse='model']"
    out = sys.argv[2] if len(sys.argv) > 2 else "shots/file_dialog.png"
    hwnd = find_main_window()
    activate(hwnd)
    # 通过 CDP 点击浏览按钮
    subprocess.run(["python", "tools/cdp_tester.py", "click", selector], check=True)
    time.sleep(1.5)
    # 截取当前活动窗口（应为文件对话框）
    from PIL import ImageGrab
    hwnd = find_main_window()
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    # 对话框通常居中在父窗口上方；截全屏以包含对话框
    img = ImageGrab.grab(bbox=(rect.left-200, rect.top-200, rect.right+200, rect.bottom+200))
    img.save(out)
    print(f"[截图] {out}")
    # 按 Esc 关闭对话框
    import pyautogui
    pyautogui.press('esc')
    time.sleep(0.5)

if __name__ == "__main__":
    main()
