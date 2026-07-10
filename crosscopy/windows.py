"""Windows process, logging, and current-user autostart helpers."""

import hashlib
import os
import shutil
import subprocess
import sys


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_ENTRIES = {
    "daemon": ("Cross Copy Daemon", "Cross Copy Daemon.pyw"),
    "widget": ("Cross Copy Widget", "Cross Copy Widget.pyw"),
}

_stdio_handles = []
_mutex_handles = []


def is_windows():
    return sys.platform == "win32"


def pythonw_executable(executable=None):
    """Return pythonw.exe next to the active interpreter when available."""
    executable = executable or sys.executable
    if not is_windows():
        return executable
    candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else executable


def console_python_executable(executable=None):
    """Return python.exe next to a pythonw/Cross Copy launcher."""
    executable = executable or sys.executable
    if not is_windows():
        return executable
    candidate = os.path.join(os.path.dirname(executable), "python.exe")
    return candidate if os.path.exists(candidate) else executable


def make_windows_launcher():
    """Create a venv-local pythonw copy named Cross Copy.exe when possible."""
    source = pythonw_executable()
    if os.path.basename(source).lower() != "pythonw.exe":
        raise RuntimeError(
            "pythonw.exe is unavailable; reinstall Python with Tcl/Tk support")
    if sys.prefix == sys.base_prefix:
        return source
    target = os.path.join(os.path.dirname(source), "Cross Copy.exe")
    if os.path.exists(target):
        return target  # may be the executable of an already-running daemon
    try:
        shutil.copy2(source, target)
        return target
    except OSError:
        return source


def background_popen_kwargs():
    """Popen flags for a detached child without a console window."""
    if is_windows():
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"creationflags": flags}
    return {"start_new_session": True}


def pid_alive(pid):
    """Probe a Windows pid without os.kill(pid, 0), which is not POSIX-like."""
    if not is_windows():
        raise RuntimeError("Windows pid probing requires Windows")
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                     wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information, False, int(pid))
    if not handle:
        return kernel32.GetLastError() == 5  # access denied still means alive
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def process_start_time(pid):
    """Return a stable Windows process creation timestamp, or None.

    PIDs are reused.  Persisting this value alongside a PID lets lifecycle
    commands prove they are still addressing the process that wrote a
    pidfile instead of an unrelated process that later inherited its PID.
    """
    if not is_windows():
        return None
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                     wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        process_query_limited_information, False, int(pid))
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    finally:
        kernel32.CloseHandle(handle)


def pid_matches_start_time(pid, expected):
    """Whether pid still identifies the process creation recorded earlier."""
    try:
        expected = int(expected)
    except (TypeError, ValueError):
        return False
    actual = process_start_time(pid)
    return actual is not None and actual == expected


def ensure_stdio(log_path):
    """Give pythonw-hosted processes writable stdout/stderr streams."""
    if not is_windows() or (sys.stdout is not None and sys.stderr is not None):
        return
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    stream = open(log_path, "a", encoding="utf-8", buffering=1)
    _stdio_handles.append(stream)  # keep it alive for the process lifetime
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def daemon_mutex_name(home, port):
    identity = (os.path.abspath(str(home)).lower() + "|" + str(int(port)))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return "Local\\CrossCopyDaemon-" + digest


def widget_mutex_name(home):
    identity = os.path.abspath(str(home)).lower()
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return "Local\\CrossCopyWidget-" + digest


def _acquire_mutex(name):
    """Acquire one named mutex and retain its handle for process lifetime."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError()
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _mutex_handles.append(handle)
    return True


def acquire_daemon_mutex(home, port):
    """True when this is the only Windows daemon for the home/port pair."""
    if not is_windows():
        return True
    return _acquire_mutex(daemon_mutex_name(home, port))


def acquire_widget_mutex(home):
    """True when this is the only Windows widget for the data directory."""
    if not is_windows():
        return True
    return _acquire_mutex(widget_mutex_name(home))


def release_daemon_mutex():
    """Release this process's daemon mutex before a Windows self-exec."""
    if not is_windows():
        return
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    while _mutex_handles:
        kernel32.CloseHandle(_mutex_handles.pop())


def crosscopy_home():
    return (os.environ.get("CROSSCOPY_HOME")
            or os.path.join(os.path.expanduser("~"), ".crosscopy"))


def startup_launcher_path(kind):
    try:
        _entry_name, filename = STARTUP_ENTRIES[kind]
    except KeyError:
        raise ValueError("unknown Windows startup launcher: %s" % kind)
    return os.path.join(crosscopy_home(), filename)


def write_startup_launcher(kind, module):
    """Register a hidden, current-user login launcher and return its path.

    The HKCU Run key invokes a small .pyw file through pythonw, so this needs no
    admin access, creates no console, and does not rely on deprecated VBScript.
    Environment overrides are embedded just like launchd/systemd settings.
    """
    try:
        entry_name, _filename = STARTUP_ENTRIES[kind]
    except KeyError:
        raise ValueError("unknown Windows startup launcher: %s" % kind)
    path = startup_launcher_path(kind)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ["import os", "import runpy"]
    for name in ("CROSSCOPY_HOME", "CROSSCOPY_PORT"):
        value = os.environ.get(name)
        if value:
            lines.append("os.environ[%r] = %r" % (name, value))
    lines.extend([
        "os.chdir(os.path.expanduser('~'))",
        "runpy.run_module(%r, run_name='__main__')" % module,
    ])
    with open(path, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write("\n".join(lines) + "\n")

    import winreg
    command = subprocess.list2cmdline([make_windows_launcher(), path])
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, entry_name, 0, winreg.REG_SZ, command)
    return path


def launch_startup_entry(kind):
    """Start a registered .pyw launcher immediately and return its Popen."""
    return subprocess.Popen(
        [make_windows_launcher(), startup_launcher_path(kind)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **background_popen_kwargs())


def remove_startup_launcher(kind):
    try:
        entry_name, _filename = STARTUP_ENTRIES[kind]
    except KeyError:
        raise ValueError("unknown Windows startup launcher: %s" % kind)
    removed = False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, entry_name)
        removed = True
    except (FileNotFoundError, OSError):
        pass
    path = startup_launcher_path(kind)
    try:
        os.remove(path)
        removed = True
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return removed
