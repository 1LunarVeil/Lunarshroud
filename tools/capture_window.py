"""截取指定窗口的客户区。

WebView2 使用 DirectX 渲染，BitBlt/PrintWindow 经常截到空白或残影。
这里改用 PIL.ImageGrab 直接抓取屏幕像素（窗口需可见且在前台），
截图更稳定、颜色更准确。
"""

import ctypes
import sys
import time
from ctypes import wintypes

try:
    from PIL import ImageGrab
except ImportError:
    print("请先安装 Pillow: pip install Pillow")
    sys.exit(1)

user32 = ctypes.windll.user32

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_SHOWWINDOW = 0x0040
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001


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


def get_client_rect(hwnd):
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


def capture_window(hwnd, output_path):
    # 恢复、前置并短暂置顶，确保窗口可见
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    time.sleep(0.5)

    cr = get_client_rect(hwnd)
    bbox = (cr["left"], cr["top"], cr["left"] + cr["width"], cr["top"] + cr["height"])

    # 直接抓取屏幕像素（最兼容 WebView2/DirectX）
    img = ImageGrab.grab(bbox=bbox)
    img.save(output_path)
    print(f"[截图] {output_path} ({cr['width']}x{cr['height']})")

    # 取消置顶
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "window_capture.png"
    wins = find_window("LunarShroud")
    if not wins:
        print("未找到 LunarShroud 窗口")
        sys.exit(1)
    hwnd = wins[0][0]
    capture_window(hwnd, out)


if __name__ == "__main__":
    main()
