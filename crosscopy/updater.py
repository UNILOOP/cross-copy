"""Update checking and self-update for cross-copy (v0.3).

Version source: fetch CROSSCOPY_UPDATE_URL (default: raw __init__.py on the
main branch) and regex-parse __version__. Installs come from
CROSSCOPY_UPDATE_PKG (default: the main-branch tarball URL — pip installs
tarball URLs directly, no git needed).

The Updater keeps a small state dict {current, latest, available,
last_checked, auto_update} that the server exposes under /api/status
"update". The daemon starts the background loop: first check ~90 s after
start, then every 6 h; when a newer version is found and auto_update is on,
self_update() runs pip and the daemon re-execs itself. All failures are
logged and never crash the daemon.
"""

import logging
import os
import re
import subprocess
import sys
import threading
import time

import requests

from . import __version__, config
from .events import bus

log = logging.getLogger("crosscopy.updater")

DEFAULT_UPDATE_URL = ("https://raw.githubusercontent.com/UNILOOP/cross-copy/"
                      "main/crosscopy/__init__.py")
DEFAULT_PKG_URL = ("https://github.com/UNILOOP/cross-copy/"
                   "archive/refs/heads/main.tar.gz")

def _env_seconds(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# Env overrides exist for testing (a 6-hour loop is otherwise untestable).
FIRST_CHECK_DELAY = _env_seconds("CROSSCOPY_UPDATE_FIRST_DELAY", 90.0)
CHECK_INTERVAL = _env_seconds("CROSSCOPY_UPDATE_INTERVAL", 6 * 60 * 60)
FETCH_TIMEOUT = 10.0
PIP_TIMEOUT = 600.0

_VERSION_RE = re.compile(r"__version__\s*=\s*[\"']([^\"']+)[\"']")


def update_url() -> str:
    return os.environ.get("CROSSCOPY_UPDATE_URL") or DEFAULT_UPDATE_URL


def pkg_url() -> str:
    return os.environ.get("CROSSCOPY_UPDATE_PKG") or DEFAULT_PKG_URL


def version_tuple(version) -> tuple:
    """'0.3.0' -> (0, 3, 0); tolerant of junk (falls back to (0,))."""
    parts = re.findall(r"\d+", str(version or ""))
    return tuple(int(p) for p in parts) if parts else (0,)


def is_newer(latest, current) -> bool:
    return version_tuple(latest) > version_tuple(current)


def get_latest_version() -> str:
    """Fetch the update URL and parse __version__ out of it.

    Raises on network errors or if no __version__ is found."""
    resp = requests.get(update_url(), timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    match = _VERSION_RE.search(resp.text)
    if not match:
        raise ValueError("no __version__ found at %s" % update_url())
    return match.group(1)


def self_update() -> bool:
    """Run pip install --upgrade <PKG_URL>; returns True on success.

    Adds --user when running outside a venv. Output is captured and logged
    (the daemon's stdout goes to daemon.log)."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if sys.prefix == sys.base_prefix:  # not in a virtualenv
        cmd.append("--user")
    cmd.append(pkg_url())
    log.info("self-update: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=PIP_TIMEOUT)
    except Exception as exc:
        log.warning("self-update could not run pip: %s", exc)
        return False
    stdout = (proc.stdout or b"").decode("utf-8", "replace").strip()
    stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if stdout:
        log.info("pip output:\n%s", stdout)
    if stderr:
        log.info("pip stderr:\n%s", stderr)
    if proc.returncode != 0:
        log.warning("self-update failed (pip exit code %d)", proc.returncode)
        return False
    log.info("self-update succeeded")
    return True


class Updater:
    """Holds update-check state and runs the daemon's background check loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self._latest = None
        self._last_checked = None
        self._stop = threading.Event()
        self._thread = None

    # -- state ---------------------------------------------------------------

    def state(self) -> dict:
        with self._lock:
            latest = self._latest
            last_checked = self._last_checked
        return {
            "current": __version__,
            "latest": latest,
            "available": bool(latest and is_newer(latest, __version__)),
            "last_checked": last_checked,
            "auto_update": config.get_auto_update(),
        }

    def check(self) -> dict:
        """Fetch the latest version, update state, publish an "update" event
        if something changed. Never raises."""
        latest = None
        try:
            latest = get_latest_version()
        except Exception as exc:
            log.warning("update check failed: %s", exc)
        with self._lock:
            changed = latest is not None and latest != self._latest
            if latest is not None:
                self._latest = latest
            self._last_checked = time.time()
        state = self.state()
        if changed:
            bus.publish("update")
            if state["available"]:
                log.info("update available: %s -> %s", __version__, latest)
            else:
                log.info("up to date (current %s, latest %s)", __version__, latest)
        return state

    # -- background loop -----------------------------------------------------

    def start(self, restart=None) -> None:
        """Start the background check thread. `restart` is called after a
        successful auto self-update (the daemon re-execs itself there)."""
        self._thread = threading.Thread(
            target=self._loop, args=(restart,),
            name="crosscopy-updater", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self, restart) -> None:
        if self._stop.wait(FIRST_CHECK_DELAY):
            return
        while not self._stop.is_set():
            try:
                state = self.check()
                if state["available"] and state["auto_update"]:
                    log.info("auto-update enabled; upgrading to %s",
                             state["latest"])
                    if self_update() and restart is not None:
                        restart()  # normally does not return (execv)
            except Exception as exc:  # belt and braces: never kill the daemon
                log.warning("updater loop error: %s", exc)
            if self._stop.wait(CHECK_INTERVAL):
                return
