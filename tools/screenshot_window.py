import ctypes
import sys
from ctypes import wintypes

try:
    from PIL import Image
except ImportError:
    print("PIL not available")
    sys.exit(1)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]

def find_window(title_substring):
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

def screenshot_window(hwnd, output_path):
    user32.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL
    user32.SetForegroundWindow(hwnd)
    import time
    time.sleep(0.5)

    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        print(f"Invalid window size: {w}x{h}")
        return False

    # Use screen DC BitBlt to capture DWM-composited windows (WebView2)
    hdc_screen = gdi32.CreateDCW("DISPLAY", None, None, None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbitmap = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbitmap)
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, rect.left, rect.top, 0x00CC0020)
    gdi32.DeleteDC(hdc_screen)

    # Create DIB section to read pixels
    bmi = (ctypes.c_ubyte * 40)()
    ctypes.memset(bmi, 0, 40)
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_uint32))[0] = 40
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_int32))[1] = w
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_int32))[2] = h
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_uint16))[7] = 1
    ctypes.cast(bmi, ctypes.POINTER(ctypes.c_uint16))[8] = 32

    pixels = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc_mem, hbitmap, 0, h, pixels, bmi, 0)

    # BGRA -> RGBA
    img = Image.frombytes('RGBA', (w, h), pixels.raw)
    r, g, b, a = img.split()
    img = Image.merge('RGB', (b, g, r))
    img.save(output_path)

    gdi32.DeleteObject(hbitmap)
    gdi32.DeleteDC(hdc_mem)
    print(f"Saved {output_path} {w}x{h}")
    return True

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: screenshot_window.py <title_substring> <output.png>")
        sys.exit(1)
    title = sys.argv[1]
    output = sys.argv[2]
    wins = find_window(title)
    if not wins:
        print(f"Window not found: {title}")
        sys.exit(1)
    hwnd, text = wins[0]
    print(f"Found window: {text}")
    ok = screenshot_window(hwnd, output)
    sys.exit(0 if ok else 1)
