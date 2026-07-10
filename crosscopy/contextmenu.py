"""Native file-manager context-menu integration for Cross Copy.

The integrations are intentionally per-user and invoke the installed Python
environment directly. That keeps them stable when a terminal PATH differs
from the graphical desktop environment.
"""

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


ACTION_ALL = "share-all"
ACTION_DEVICE = "share-to"
MAC_WORKFLOWS = (
    ("Cross Copy - Share to All.workflow", ACTION_ALL),
    ("Cross Copy - Share to Device.workflow", ACTION_DEVICE),
)
LINUX_SCRIPT_DIRS = (
    Path(".local/share/nautilus/scripts/Cross Copy"),
    Path(".local/share/nemo/scripts/Cross Copy"),
    Path(".config/caja/scripts/Cross Copy"),
)
LINUX_SERVICE_DIRS = (
    Path(".local/share/kio/servicemenus"),
    Path(".local/share/kservices5/ServiceMenus"),
)
WINDOWS_MENU_KEY = r"Software\Classes\AllFilesystemObjects\shell\CrossCopy"


class ChooserUnavailable(RuntimeError):
    """The desktop has no supported native device chooser."""


def _python_command(action):
    return [sys.executable, "-m", "crosscopy.cli", "context", action]


def _background_shell_command(action):
    command = " ".join(shlex.quote(part) for part in _python_command(action))
    return '%s "$@" >/dev/null 2>&1 &' % command


def _mac_workflow(action):
    parameters = {
        "COMMAND_STRING": _background_shell_command(action),
        "CheckedForUserDefaultShell": True,
        "inputMethod": 1,
        "shell": "/bin/sh",
    }
    workflow = {
        "AMApplicationBuild": "523",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [{
            "action": {
                "AMAccepts": {
                    "Container": "List",
                    "Optional": True,
                    "Types": ["com.apple.cocoa.path"],
                },
                "AMActionVersion": "2.0.3",
                "AMApplication": ["Automator"],
                "AMParameterProperties": {},
                "AMProvides": {
                    "Container": "List",
                    "Types": ["com.apple.cocoa.path"],
                },
                "ActionBundlePath": (
                    "/System/Library/Automator/Run Shell Script.action"),
                "ActionName": "Run Shell Script",
                "ActionParameters": parameters,
                "BundleIdentifier": "com.apple.RunShellScript",
                "CFBundleVersion": "2.0.3",
                "CanShowSelectedItemsWhenRun": False,
                "CanShowWhenRun": True,
                "Category": ["AMCategoryUtilities"],
                "Class Name": "RunShellScriptAction",
                "InputUUID": "00000000-0000-0000-0000-000000000001",
                "Keywords": ["Shell", "Script", "Command", "Run"],
                "OutputUUID": "00000000-0000-0000-0000-000000000002",
                "UUID": "00000000-0000-0000-0000-000000000003",
            },
            "isViewVisible": True,
        }],
        "connectors": {},
        "workflowMetaData": {
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": (
                "/System/Library/CoreServices/Finder.app"),
            "serviceInputTypeIdentifier": (
                "com.apple.Automator.fileSystemObject"),
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": 0,
        },
    }
    return plistlib.dumps(workflow, fmt=plistlib.FMT_XML, sort_keys=False)


def _install_macos(home):
    services = home / "Library/Services"
    installed = []
    for name, action in MAC_WORKFLOWS:
        contents = services / name / "Contents"
        contents.mkdir(parents=True, exist_ok=True)
        document = contents / "document.wflow"
        document.write_bytes(_mac_workflow(action))
        installed.append(document)
    return installed


def _linux_script(action):
    return "#!/bin/sh\n%s\n" % _background_shell_command(action)


def _dolphin_service_menu():
    all_command = " ".join(
        shlex.quote(part) for part in _python_command(ACTION_ALL))
    device_command = " ".join(
        shlex.quote(part) for part in _python_command(ACTION_DEVICE))
    return """[Desktop Entry]
Type=Service
MimeType=application/octet-stream;inode/directory;
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
Actions=CrossCopyAll;CrossCopyDevice;
Icon=edit-copy

[Desktop Action CrossCopyAll]
Name=Share to all devices
Icon=edit-copy
Exec=%s %%F

[Desktop Action CrossCopyDevice]
Name=Share to a device…
Icon=network-workgroup
Exec=%s %%F
""" % (all_command, device_command)


def _install_linux(home):
    installed = []
    names = (("Share to all devices", ACTION_ALL),
             ("Share to a device…", ACTION_DEVICE))
    for relative in LINUX_SCRIPT_DIRS:
        directory = home / relative
        directory.mkdir(parents=True, exist_ok=True)
        for name, action in names:
            path = directory / name
            path.write_text(_linux_script(action), encoding="utf-8")
            path.chmod(0o755)
            installed.append(path)
    menu = _dolphin_service_menu()
    for relative in LINUX_SERVICE_DIRS:
        directory = home / relative
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "cross-copy.desktop"
        path.write_text(menu, encoding="utf-8")
        path.chmod(0o755)
        installed.append(path)
    return installed


def _windows_command(action, launcher):
    prefix = subprocess.list2cmdline(
        [launcher, "-m", "crosscopy.cli", "context", action])
    return prefix + " %*"


def _install_windows(_home):
    import winreg
    from .windows import make_windows_launcher

    launcher = make_windows_launcher()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_MENU_KEY) as root:
        winreg.SetValueEx(root, "MUIVerb", 0, winreg.REG_SZ, "Cross Copy")
        winreg.SetValueEx(root, "Icon", 0, winreg.REG_SZ, launcher)
        winreg.SetValueEx(root, "MultiSelectModel", 0, winreg.REG_SZ,
                          "Player")
    actions = (
        ("01ShareAll", "Share to all devices", ACTION_ALL),
        ("02ShareDevice", "Share to a device…", ACTION_DEVICE),
    )
    for key_name, label, action in actions:
        path = (WINDOWS_MENU_KEY
                + "\\ExtendedSubCommandsKey\\shell\\" + key_name)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, label)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, launcher)
            winreg.SetValueEx(key, "MultiSelectModel", 0, winreg.REG_SZ,
                              "Player")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              path + r"\command") as command:
            winreg.SetValueEx(
                command, None, 0, winreg.REG_SZ,
                _windows_command(action, launcher))
    return ["registry:HKCU\\" + WINDOWS_MENU_KEY]


def install(platform=None, home=None):
    """Install per-user native context actions; return written locations."""
    platform = platform or sys.platform
    home = Path(home or Path.home())
    if platform == "darwin":
        return _install_macos(home)
    if platform.startswith("linux"):
        return _install_linux(home)
    if platform == "win32":
        return _install_windows(home)
    raise RuntimeError("context menus are unsupported on %s" % platform)


def _uninstall_macos(home):
    removed = []
    for name, _action in MAC_WORKFLOWS:
        workflow = home / "Library/Services" / name
        if workflow.exists():
            shutil.rmtree(workflow)
            removed.append(workflow)
    return removed


def _uninstall_linux(home):
    removed = []
    for relative in LINUX_SCRIPT_DIRS:
        directory = home / relative
        for name in ("Share to all devices", "Share to a device…"):
            path = directory / name
            try:
                path.unlink()
                removed.append(path)
            except FileNotFoundError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass
    for relative in LINUX_SERVICE_DIRS:
        path = home / relative / "cross-copy.desktop"
        try:
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            pass
    return removed


def _delete_registry_tree(winreg, root, path):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            children = []
            index = 0
            while True:
                try:
                    children.append(winreg.EnumKey(key, index))
                    index += 1
                except OSError:
                    break
        for child in children:
            _delete_registry_tree(winreg, root, path + "\\" + child)
        winreg.DeleteKey(root, path)
        return True
    except FileNotFoundError:
        return False


def _uninstall_windows(_home):
    import winreg
    removed = _delete_registry_tree(
        winreg, winreg.HKEY_CURRENT_USER, WINDOWS_MENU_KEY)
    return ["registry:HKCU\\" + WINDOWS_MENU_KEY] if removed else []


def uninstall(platform=None, home=None):
    """Remove only Cross Copy-owned context-menu registrations."""
    platform = platform or sys.platform
    home = Path(home or Path.home())
    if platform == "darwin":
        return _uninstall_macos(home)
    if platform.startswith("linux"):
        return _uninstall_linux(home)
    if platform == "win32":
        return _uninstall_windows(home)
    return []


def selected_paths(arguments, environ=None):
    """Normalize file-manager arguments and legacy selection variables."""
    values = list(arguments or [])
    environ = environ or os.environ
    if not values:
        for name in ("NAUTILUS_SCRIPT_SELECTED_FILE_PATHS",
                     "NEMO_SCRIPT_SELECTED_FILE_PATHS",
                     "CAJA_SCRIPT_SELECTED_FILE_PATHS"):
            raw = environ.get(name)
            if raw:
                values.extend(line for line in raw.splitlines() if line)
                break
    paths = []
    for value in values:
        path = Path(value).expanduser().resolve()
        if path.exists():
            paths.append(str(path))
    return paths


def _peer_labels(peers):
    labels = []
    seen = set()
    for peer in peers:
        name = peer.get("name") or peer.get("id") or "Unknown device"
        label = " ".join(str(name).splitlines()).strip()
        label = "".join(character for character in label
                        if character.isprintable())[:160]
        label = label or "Unknown device"
        if label.casefold() in seen:
            label = "%s (%s)" % (label, str(peer.get("id") or "")[:8])
        seen.add(label.casefold())
        labels.append(label)
    return labels


def _choose_macos(peers, labels):
    escaped = [str(label).replace("\\", "\\\\").replace('"', '\\"')
               for label in labels]
    choices = "{" + ", ".join('"%s"' % label for label in escaped) + "}"
    script = ('set picked to choose from list %s with title "Cross Copy" '
              'with prompt "Share selected items to:"\n'
              'if picked is false then return ""\nreturn item 1 of picked'
              % choices)
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True,
            timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    selected = (result.stdout or "").strip()
    return peers[labels.index(selected)] if selected in labels else None


def _choose_linux(peers, labels):
    if shutil.which("zenity"):
        argv = ["zenity", "--list", "--title=Cross Copy",
                "--text=Share selected items to:", "--column=Device",
                "--column=ID", "--print-column=2", "--hide-column=2", "--"]
        for index, label in enumerate(labels):
            argv.extend([label, str(index)])
    elif shutil.which("kdialog"):
        argv = ["kdialog", "--title", "Cross Copy", "--menu",
                "Share selected items to:", "--"]
        for index, label in enumerate(labels):
            argv.extend([str(index), label])
    else:
        if len(peers) == 1:
            return peers[0]
        raise ChooserUnavailable(
            "Install zenity or kdialog to choose a Cross Copy device.")
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    selected = (result.stdout or "").strip()
    try:
        return peers[int(selected)]
    except (ValueError, IndexError):
        return None


def _choose_windows(peers, labels):
    powershell = next((shutil.which(name) for name in
                       ("powershell.exe", "powershell", "pwsh.exe", "pwsh")
                       if shutil.which(name)), None)
    if not powershell:
        if len(peers) == 1:
            return peers[0]
        raise ChooserUnavailable(
            "PowerShell is required to choose a Cross Copy device.")
    payload = [{"id": peer.get("id"), "label": label}
               for peer, label in zip(peers, labels)]
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$devices = @(([Console]::In.ReadToEnd() | ConvertFrom-Json))
$form = New-Object Windows.Forms.Form
$form.Text = 'Cross Copy'
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object Drawing.Size(420,330)
$form.MinimizeBox = $false
$form.MaximizeBox = $false
$list = New-Object Windows.Forms.ListBox
$list.Location = New-Object Drawing.Point(18,18)
$list.Size = New-Object Drawing.Size(368,220)
foreach ($device in $devices) { [void]$list.Items.Add([string]$device.label) }
if ($list.Items.Count -gt 0) { $list.SelectedIndex = 0 }
$ok = New-Object Windows.Forms.Button
$ok.Text = 'Share'
$ok.Location = New-Object Drawing.Point(216,250)
$ok.DialogResult = [Windows.Forms.DialogResult]::OK
$cancel = New-Object Windows.Forms.Button
$cancel.Text = 'Cancel'
$cancel.Location = New-Object Drawing.Point(306,250)
$cancel.DialogResult = [Windows.Forms.DialogResult]::Cancel
$form.AcceptButton = $ok
$form.CancelButton = $cancel
$form.Controls.AddRange(@($list,$ok,$cancel))
if ($form.ShowDialog() -eq [Windows.Forms.DialogResult]::OK -and
    $list.SelectedIndex -ge 0) {
    [Console]::Out.Write([string]$devices[$list.SelectedIndex].id)
}
'''
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-STA", "-Command", script],
            input=json.dumps(payload), capture_output=True, text=True,
            timeout=300, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return None
    selected = (result.stdout or "").strip()
    return next((peer for peer in peers
                 if str(peer.get("id") or "") == selected), None)


def choose_peer(peers, platform=None):
    peers = list(peers or [])
    if not peers:
        return None
    labels = _peer_labels(peers)
    platform = platform or sys.platform
    if platform == "darwin":
        return _choose_macos(peers, labels)
    if platform.startswith("linux"):
        return _choose_linux(peers, labels)
    if platform == "win32":
        return _choose_windows(peers, labels)
    return peers[0] if len(peers) == 1 else None
