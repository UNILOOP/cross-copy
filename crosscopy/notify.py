"""Best-effort desktop notifications for cross-copy (v0.4).

notify(title, body): never raises, no new dependencies.
  macOS: `osascript -e 'display notification ...'`
  Linux: `notify-send` if on PATH, else `gdbus call
         org.freedesktop.Notifications.Notify`, else silent no-op.
  Windows: a hidden stdlib helper using Shell_NotifyIconW.

Gated by the "notifications" config key (default true). All commands run as
argument lists (never shell=True); the strings interpolated into the
AppleScript source are escaped.
"""

import logging
import shutil
import subprocess
import sys

from . import config, events

log = logging.getLogger("crosscopy.notify")

_TIMEOUT = 5  # seconds; a notification is not worth blocking on


def notify(title, body) -> None:
    """Show a desktop notification. Best-effort: never raises."""
    try:
        if not config.get_notifications():
            return
        # The tray widget renders its own popup cards (with accept/decline
        # actions) — while one is connected, stay out of the OS
        # notification center entirely.
        if events.bus.has_client("widget"):
            return
        title = str(title)
        body = str(body)
        if sys.platform.startswith("darwin"):
            _notify_macos(title, body)
        elif sys.platform.startswith("linux"):
            _notify_linux(title, body)
        elif sys.platform == "win32":
            _notify_windows(title, body)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("notification failed: %s", exc)


# ---------------------------------------------------------------------------
# Backends

def _escape_applescript(text: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _notify_macos(title: str, body: str) -> None:
    script = 'display notification "%s" with title "%s"' % (
        _escape_applescript(body), _escape_applescript(title))
    _run(["osascript", "-e", script])


def _notify_linux(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        if _run(["notify-send", "--app-name=cross-copy", "--", title, body]):
            return
    if shutil.which("gdbus"):
        _run([
            "gdbus", "call", "--session",
            "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
            "--method", "org.freedesktop.Notifications.Notify",
            "cross-copy", "0", "", title, body, "[]", "{}", "5000",
        ])
    # neither tool available: silent no-op


def _notify_windows(title: str, body: str) -> bool:
    """Launch the Win32 banner helper without opening a console."""
    try:
        from crosscopy.windows import (background_popen_kwargs,
                                       pythonw_executable)
        subprocess.Popen(
            [pythonw_executable(), "-m", "crosscopy.winnotify", title, body],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **background_popen_kwargs())
        return True
    except Exception as exc:
        log.debug("Windows notification helper failed: %s", exc)
        return False


def _run(argv) -> bool:
    """Run a command quietly; True if it exited 0. Never raises."""
    try:
        proc = subprocess.run(argv, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=_TIMEOUT)
        return proc.returncode == 0
    except Exception as exc:
        log.debug("notification command %s failed: %s", argv[0], exc)
        return False
