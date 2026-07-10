"""Windows process, logging, and current-user autostart helpers."""

import hashlib
import os
import re
import shutil
import struct
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


def _align4(data):
    data.extend(b"\0" * ((-len(data)) % 4))


def _utf16z(value):
    return str(value).encode("utf-16le") + b"\0\0"


def _version_block(key, value=b"", value_length=0, value_type=1,
                   children=()):
    """Build one DWORD-aligned VERSIONINFO block."""
    block = bytearray(b"\0" * 6)
    block.extend(_utf16z(key))
    _align4(block)
    block.extend(value)
    _align4(block)
    for child in children:
        block.extend(child)
        _align4(block)
    if len(block) > 0xFFFF:
        raise ValueError("Windows version resource is too large")
    struct.pack_into("<HHH", block, 0, len(block), int(value_length),
                     int(value_type))
    return bytes(block)


def windows_version_resource(version):
    """Return a VERSIONINFO resource branded for Cross Copy."""
    numbers = [int(part) for part in re.findall(r"\d+", str(version))[:4]]
    numbers.extend([0] * (4 - len(numbers)))
    major, minor, patch, build = numbers
    version_ms = (major << 16) | minor
    version_ls = (patch << 16) | build
    fixed = struct.pack(
        "<13I",
        0xFEEF04BD, 0x00010000,
        version_ms, version_ls, version_ms, version_ls,
        0x0000003F, 0,
        0x00040004,  # VOS_NT_WINDOWS32
        0x00000001,  # VFT_APP
        0, 0, 0,
    )
    text_values = {
        "CompanyName": "UNILOOP LLC",
        "FileDescription": "Cross Copy",
        "FileVersion": "%d.%d.%d.%d" % tuple(numbers),
        "InternalName": "Cross Copy",
        "LegalCopyright": "Copyright UNILOOP LLC",
        "OriginalFilename": "Cross Copy.exe",
        "ProductName": "Cross Copy",
        "ProductVersion": str(version),
    }
    strings = []
    for key, text in text_values.items():
        encoded = _utf16z(text)
        strings.append(_version_block(
            key, encoded, value_length=len(text) + 1, value_type=1))
    string_table = _version_block("040904B0", children=strings)
    string_file_info = _version_block(
        "StringFileInfo", children=[string_table])
    translation = _version_block(
        "Translation", struct.pack("<HH", 0x0409, 1200),
        value_length=4, value_type=0)
    var_file_info = _version_block(
        "VarFileInfo", children=[translation])
    return _version_block(
        "VS_VERSION_INFO", fixed, value_length=len(fixed), value_type=0,
        children=[string_file_info, var_file_info])


def brand_windows_executable(path, version):
    """Replace executable VERSIONINFO so Windows presents Cross Copy."""
    if not is_windows():
        raise RuntimeError("Windows executable branding requires Windows")
    import ctypes
    from ctypes import wintypes

    resource = windows_version_resource(version)
    kernel32 = ctypes.windll.kernel32
    kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR,
                                               wintypes.BOOL]
    kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel32.UpdateResourceW.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD,
        ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.UpdateResourceW.restype = wintypes.BOOL
    kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    kernel32.EndUpdateResourceW.restype = wintypes.BOOL

    handle = kernel32.BeginUpdateResourceW(os.path.abspath(path), False)
    if not handle:
        raise ctypes.WinError(kernel32.GetLastError())
    buffer = ctypes.create_string_buffer(resource)
    if not kernel32.UpdateResourceW(
            handle, ctypes.c_void_p(16), ctypes.c_void_p(1), 0x0409,
            buffer, len(resource)):
        error = kernel32.GetLastError()
        kernel32.EndUpdateResourceW(handle, True)
        raise ctypes.WinError(error)
    if not kernel32.EndUpdateResourceW(handle, False):
        raise ctypes.WinError(kernel32.GetLastError())


def _launcher_marker(path):
    return path + ".crosscopy-version"


def _launcher_is_current(path, version):
    if not os.path.isfile(path):
        return False
    try:
        with open(_launcher_marker(path), encoding="ascii") as handle:
            return handle.read().strip() == str(version)
    except OSError:
        return False


def _mark_launcher(path, version):
    marker = _launcher_marker(path)
    temp = marker + ".tmp"
    with open(temp, "w", encoding="ascii", newline="\n") as handle:
        handle.write(str(version) + "\n")
    os.replace(temp, marker)


def _create_branded_launcher(source, target, version):
    if not os.path.exists(target):
        shutil.copy2(source, target)
    brand_windows_executable(target, version)
    _mark_launcher(target, version)
    return target


def make_windows_launcher(version=None):
    """Return a venv-local Python GUI interpreter branded as Cross Copy."""
    if version is None:
        from . import __version__
        version = __version__

    source = pythonw_executable()
    if os.path.basename(source).lower() != "pythonw.exe":
        raise RuntimeError(
            "pythonw.exe is unavailable; reinstall Python with Tcl/Tk support")
    if sys.prefix == sys.base_prefix:
        return source
    target = os.path.join(os.path.dirname(source), "Cross Copy.exe")
    if _launcher_is_current(target, version):
        return target
    try:
        return _create_branded_launcher(source, target, version)
    except OSError:
        # An existing daemon may have Cross Copy.exe locked during an
        # automatic update. Build a versioned replacement beside it so the
        # new process and future login entries can switch immediately.
        fallback = os.path.join(
            os.path.dirname(source), "Cross Copy %s.exe" % version)
        if _launcher_is_current(fallback, version):
            return fallback
        return _create_branded_launcher(source, fallback, version)


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


def refresh_registered_startup_commands(executable=None):
    """Point existing login entries at the current branded executable.

    During an update Windows can keep the running executable locked. The
    replacement then has a versioned filename, so existing Run entries must
    follow it for the next login. Entries the user removed are not recreated.
    """
    executable = executable or make_windows_launcher()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE
                            | winreg.KEY_SET_VALUE) as key:
            for kind, (entry_name, _filename) in STARTUP_ENTRIES.items():
                try:
                    winreg.QueryValueEx(key, entry_name)
                except FileNotFoundError:
                    continue
                command = subprocess.list2cmdline(
                    [executable, startup_launcher_path(kind)])
                winreg.SetValueEx(key, entry_name, 0, winreg.REG_SZ, command)
    except FileNotFoundError:
        pass


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
