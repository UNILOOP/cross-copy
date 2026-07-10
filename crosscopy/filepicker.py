"""Native multi-file pickers for the Cross Copy tray widget.

The tray runs without a normal application window, so the picker must be
modal and self-contained. Platform APIs/tools are loaded lazily; tkinter is
kept only as a last-resort fallback for unusually minimal Linux desktops.
"""

import json
import os
import shutil
import subprocess
import sys


PICKER_TITLE = "Send files with Cross Copy"


def _macos_files(title=PICKER_TITLE):
    """Select files through AppKit's standard NSOpenPanel."""
    try:
        import AppKit

        app = AppKit.NSApplication.sharedApplication()
        app.activateIgnoringOtherApps_(True)
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setTitle_(title)
        panel.setPrompt_("Choose")
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(True)
        panel.setResolvesAliases_(True)
        response = panel.runModal()
        accepted = getattr(AppKit, "NSModalResponseOK", 1)
        if int(response) != int(accepted):
            return []
        return [str(url.path()) for url in (panel.URLs() or [])]
    except Exception:
        return None


def _windows_files(title=PICKER_TITLE):
    """Select files through Windows' current common file dialog."""
    powershell = None
    for name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        powershell = shutil.which(name)
        if powershell:
            break
    if not powershell:
        return None

    safe_title = title.replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "[System.Windows.Forms.Application]::EnableVisualStyles();"
        "$dialog=New-Object System.Windows.Forms.OpenFileDialog;"
        "$dialog.Title='%s';"
        "$dialog.Multiselect=$true;"
        "$dialog.AutoUpgradeEnabled=$true;"
        "$dialog.RestoreDirectory=$true;"
        "$chosen=@();"
        "if($dialog.ShowDialog() -eq "
        "[System.Windows.Forms.DialogResult]::OK){"
        "$chosen=@($dialog.FileNames)};"
        "[Console]::OutputEncoding="
        "[System.Text.UTF8Encoding]::new($false);"
        "[Console]::Out.Write((ConvertTo-Json -InputObject $chosen "
        "-Compress));"
        "$dialog.Dispose()" % safe_title
    )
    kwargs = {}
    if sys.platform == "win32":
        from .windows import background_popen_kwargs
        kwargs.update(background_popen_kwargs())
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-STA", "-Command", script],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", **kwargs)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        paths = json.loads(result.stdout or "[]")
    except (TypeError, ValueError):
        return None
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        return None
    return [str(path) for path in paths if path]


def _run_linux_picker(command, separator):
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace")
    except OSError:
        return None
    if result.returncode == 1:
        return []  # user cancelled
    if result.returncode != 0:
        return None
    output = (result.stdout or "").rstrip("\r\n")
    if not output:
        return []
    return [path for path in output.split(separator) if path]


def _linux_files(title=PICKER_TITLE):
    """Use the current Linux desktop's GTK or KDE file chooser."""
    desktop = "%s:%s" % (os.environ.get("XDG_CURRENT_DESKTOP", ""),
                          os.environ.get("DESKTOP_SESSION", ""))
    desktop = desktop.lower()
    record_separator = "\x1e"

    candidates = []
    kdialog = shutil.which("kdialog")
    zenity = shutil.which("zenity")
    qarma = shutil.which("qarma")
    if kdialog and ("kde" in desktop or "plasma" in desktop):
        candidates.append(([
            kdialog, "--title", title, "--getopenfilename",
            os.path.expanduser("~"), "All files (*)", "--multiple",
            "--separate-output"], "\n"))
    for executable in (zenity, qarma):
        if executable:
            candidates.append(([
                executable, "--file-selection", "--multiple",
                "--separator=%s" % record_separator,
                "--title=%s" % title], record_separator))
    if kdialog and not any(command[0][0] == kdialog for command in candidates):
        candidates.append(([
            kdialog, "--title", title, "--getopenfilename",
            os.path.expanduser("~"), "All files (*)", "--multiple",
            "--separate-output"], "\n"))

    for command, separator in candidates:
        paths = _run_linux_picker(command, separator)
        if paths is not None:
            return paths
    return None


def _tk_files(title=PICKER_TITLE):
    """Last-resort picker when a native desktop integration is unavailable."""
    root = None
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        return list(filedialog.askopenfilenames(parent=root, title=title))
    except Exception:
        return None
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def pick_files(title=PICKER_TITLE):
    """Return selected paths, [] on cancel, or None if no picker can open."""
    if sys.platform == "darwin":
        paths = _macos_files(title)
    elif sys.platform == "win32":
        paths = _windows_files(title)
    elif sys.platform.startswith("linux"):
        paths = _linux_files(title)
    else:
        paths = None
    return paths if paths is not None else _tk_files(title)
