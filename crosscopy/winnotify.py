"""Dependency-free Windows banner notifications via Shell_NotifyIconW."""

import argparse
import ctypes
import json
import sys
from ctypes import wintypes


NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
NIIF_LARGE_ICON = 0x00000020
NIIF_RESPECT_QUIET_TIME = 0x00000080
NOTIFYICON_VERSION_4 = 4
WM_USER = 0x0400
IDI_APPLICATION = 32512


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


def notification_payload(title, body):
    """Normalized values respecting NOTIFYICONDATA's fixed buffers."""
    return {
        "title": str(title)[:63],
        "body": str(body)[:255],
        "timeout_ms": 6000,
    }


def show_notification(title, body):
    if sys.platform != "win32":
        raise RuntimeError("Windows notifications require Windows")

    import tkinter

    payload = notification_payload(title, body)
    root = tkinter.Tk()
    root.withdraw()
    root.update_idletasks()

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    hwnd = root.winfo_id()
    while True:
        parent = user32.GetParent(hwnd)
        if not parent:
            break
        hwnd = parent

    user32.LoadIconW.restype = wintypes.HICON
    icon = user32.LoadIconW(None, IDI_APPLICATION)
    data = NOTIFYICONDATAW()
    data.cbSize = ctypes.sizeof(data)
    data.hWnd = hwnd
    data.uID = 1
    data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    data.uCallbackMessage = WM_USER + 20
    data.hIcon = icon
    data.szTip = "Cross Copy"
    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
        root.destroy()
        raise OSError("Shell_NotifyIconW(NIM_ADD) failed")

    data.uTimeoutOrVersion = NOTIFYICON_VERSION_4
    shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
    data.uFlags = NIF_INFO
    data.szInfoTitle = payload["title"]
    data.szInfo = payload["body"]
    data.uTimeoutOrVersion = payload["timeout_ms"]
    data.dwInfoFlags = (NIIF_INFO | NIIF_LARGE_ICON
                        | NIIF_RESPECT_QUIET_TIME)
    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def close():
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        root.destroy()

    root.after(payload["timeout_ms"] + 1500, close)
    root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="crosscopy.winnotify")
    parser.add_argument("title")
    parser.add_argument("body")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    payload = notification_payload(args.title, args.body)
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    show_notification(args.title, args.body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
