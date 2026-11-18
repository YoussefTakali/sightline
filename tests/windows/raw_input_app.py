"""A test window that, like a Citrix seamless window, IGNORES clicks sent as window messages
and reacts only to raw hardware input (Raw Input API: posted/sent messages cannot fake it).
Each real left click inside it toggles white/green and counts. Title: 'RawOnly Test'."""
import ctypes
from ctypes import wintypes

u = ctypes.WinDLL("user32", use_last_error=True)
g = ctypes.WinDLL("gdi32")
k = ctypes.WinDLL("kernel32")
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
u.DefWindowProcW.restype = LRESULT
u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.CreateWindowExW.restype = wintypes.HWND
u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
u.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                              ctypes.POINTER(wintypes.UINT), wintypes.UINT]
u.BeginPaint.restype = wintypes.HDC
u.FillRect.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.HANDLE]
g.CreateSolidBrush.restype = wintypes.HANDLE
k.GetModuleHandleW.restype = wintypes.HMODULE


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class _BTN(ctypes.Structure):
    _fields_ = [("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT)]


class _BU(ctypes.Union):
    _fields_ = [("ulButtons", wintypes.ULONG), ("b", _BTN)]


class RAWMOUSE(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("usFlags", wintypes.USHORT), ("u", _BU), ("ulRawButtons", wintypes.ULONG),
                ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


state = {"clicks": 0}


def wndproc(h, msg, wp, lp):
    if msg == 0x00FF:                                        # WM_INPUT: real hardware input only
        size = wintypes.UINT(0)
        u.GetRawInputData(lp, 0x10000003, None, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        buf = ctypes.create_string_buffer(size.value)
        u.GetRawInputData(lp, 0x10000003, buf, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
        if raw.header.dwType == 0 and raw.mouse.b.usButtonFlags & 0x0001:     # left button down
            pt = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            u.ScreenToClient(h, ctypes.byref(pt))
            rc = wintypes.RECT()
            u.GetClientRect(h, ctypes.byref(rc))
            if 0 <= pt.x < rc.right and 0 <= pt.y < rc.bottom:
                state["clicks"] += 1
                u.SetWindowTextW(h, f"RawOnly Test - clicks {state['clicks']}")
                u.InvalidateRect(h, None, True)
    elif msg == 0x000F:                                      # WM_PAINT
        ps = PAINTSTRUCT()
        hdc = u.BeginPaint(h, ctypes.byref(ps))
        rc = wintypes.RECT()
        u.GetClientRect(h, ctypes.byref(rc))
        brush = g.CreateSolidBrush(0x0050C050 if state["clicks"] % 2 else 0x00FFFFFF)
        u.FillRect(hdc, ctypes.byref(rc), brush)
        g.DeleteObject(brush)
        u.EndPaint(h, ctypes.byref(ps))
        return 0
    elif msg == 0x0002:                                      # WM_DESTROY
        u.PostQuitMessage(0)
        return 0
    # NOTE: WM_LBUTTONDOWN & co. are deliberately ignored (default handling only)
    return u.DefWindowProcW(h, msg, wp, lp)


proc = WNDPROC(wndproc)
wc = WNDCLASSW(lpfnWndProc=proc, hInstance=k.GetModuleHandleW(None), lpszClassName="RawOnlyTest",
               hCursor=u.LoadCursorW(None, 32512))
u.RegisterClassW(ctypes.byref(wc))
hwnd = u.CreateWindowExW(0, "RawOnlyTest", "RawOnly Test - clicks 0", 0x10CF0000,  # visible overlapped
                         100, 100, 420, 320, None, None, wc.hInstance, None)
rid = RAWINPUTDEVICE(1, 2, 0x00000100, hwnd)                 # mouse, RIDEV_INPUTSINK
u.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE))
msg = wintypes.MSG()
while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    u.TranslateMessage(ctypes.byref(msg))
    u.DispatchMessageW(ctypes.byref(msg))
