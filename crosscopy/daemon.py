"""cross-copy daemon entry point: `python -m crosscopy.daemon`.

Writes daemon.json ({"pid", "port"}) to the cross-copy home dir, starts
zeroconf discovery (unless CROSSCOPY_NO_MDNS=1) plus the reciprocal-hello
sender loop and the update-check thread, and runs the Flask server bound to
0.0.0.0. Logs to stdout — the CLI's `ccp daemon start` redirects
stdout/stderr to ~/.crosscopy/daemon.log. daemon.json is removed on clean
shutdown (SIGINT/SIGTERM/atexit).

Auto-update: when the updater finds a newer version and config auto_update is
on, it pip-installs the new package and the daemon re-execs itself
(os.execv keeps the PID, so systemd/launchd supervision is undisturbed).
"""

import atexit
import logging
import os
import signal
import sys
import time

from . import __version__, config
from .discovery import Discovery
from .server import create_app
from .updater import Updater

log = logging.getLogger("crosscopy.daemon")

_cleanup_state = {"done": False, "discovery": None, "updater": None}


def _ensure_windows_executable_identity():
    """Move legacy/update starts onto the branded launcher before binding.

    A daemon updated from an older release may initially be re-executed by
    the old Python-branded file. At this point no mutex or socket exists yet,
    so switching to the new launcher is safe and keeps the firewall owner
    consistently identified as Cross Copy.
    """
    if sys.platform != "win32":
        return
    try:
        from .windows import (make_windows_launcher,
                              refresh_registered_startup_commands)
        executable = make_windows_launcher()
        current = os.path.normcase(os.path.abspath(sys.executable))
        replacement = os.path.normcase(os.path.abspath(executable))
        if (current != replacement
                and os.path.basename(executable).lower().startswith(
                    "cross copy")):
            refresh_registered_startup_commands(executable)
            os.execv(executable,
                     [executable, "-m", "crosscopy.daemon"])
    except Exception as exc:
        log.warning("could not activate Windows executable identity: %s", exc)


def _cleanup():
    if _cleanup_state["done"]:
        return
    _cleanup_state["done"] = True
    updater = _cleanup_state["updater"]
    if updater is not None:
        try:
            updater.stop()
        except Exception:
            pass
    discovery = _cleanup_state["discovery"]
    if discovery is not None:
        try:
            discovery.stop()
        except Exception:
            pass
    config.remove_daemon_info()
    log.info("daemon stopped")


def _handle_signal(signum, frame):
    log.info("received signal %d, shutting down", signum)
    # Raises SystemExit in the main thread; Flask's serve loop unwinds and
    # atexit runs _cleanup().
    sys.exit(0)


def _restart_daemon():
    """Re-exec the daemon in place after a successful self-update (called
    from the updater thread). The PID is preserved, so systemd/launchd keep
    supervising the same process."""
    log.info("restarting desktop processes to load the updated code")
    executable = sys.executable
    if sys.platform == "win32":
        try:
            from importlib.metadata import version as package_version
            installed_version = package_version("cross-copy")
        except Exception:
            installed_version = __version__
        try:
            from .windows import (make_windows_launcher,
                                  refresh_registered_startup_commands)
            executable = make_windows_launcher(installed_version)
            refresh_registered_startup_commands(executable)
        except Exception as exc:
            log.warning("could not refresh Windows executable identity: %s",
                        exc)
    try:
        # Login startup entries are not supervisors on Windows. Restart the
        # already-running widget explicitly so it does not keep old modules
        # loaded until the user's next login.
        from .cli import restart_widget_after_update
        restart_widget_after_update(quiet=True)
    except Exception as exc:
        log.warning("could not restart tray widget after update: %s", exc)
    log.info("restarting daemon to load the updated code (execv)")
    discovery = _cleanup_state["discovery"]
    if discovery is not None:
        try:
            discovery.stop()  # cleanly unregister zeroconf before execv
        except Exception:
            pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    # Werkzeug marks its listening socket inheritable and exports
    # WERKZEUG_SERVER_FD; if either survives the execv, the re-exec'd
    # process fails its bind with "Address already in use" and dies.
    os.environ.pop("WERKZEUG_SERVER_FD", None)
    try:
        max_fd = int(os.sysconf("SC_OPEN_MAX"))
    except (ValueError, OSError, AttributeError):
        max_fd = 4096
    for fd in range(3, min(max_fd, 65536)):
        try:
            os.set_inheritable(fd, False)
        except OSError:
            pass
    # Neutral CWD so a crosscopy/ dir in the old CWD can't shadow the
    # installed package on sys.path.
    try:
        os.chdir(os.path.expanduser("~"))
    except OSError:
        pass
    if sys.platform == "win32":
        from .windows import release_daemon_mutex
        # Windows' C-runtime exec launches the replacement while the old
        # process is still unwinding. Give its socket and mutex time to close.
        os.environ["CROSSCOPY_RESTART_DELAY"] = "0.5"
        release_daemon_mutex()
    os.execv(executable, [executable, "-m", "crosscopy.daemon"])


def main():
    if sys.platform == "win32":
        _ensure_windows_executable_identity()
        from .windows import ensure_stdio
        ensure_stdio(str(config.daemon_log_path()))
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet werkzeug's per-request lines a little but keep startup info.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    home = config.get_home()
    port = config.get_port()
    cfg = config.load_config()

    if sys.platform == "win32":
        try:
            delay = float(os.environ.pop("CROSSCOPY_RESTART_DELAY", "0"))
        except ValueError:
            delay = 0
        if delay > 0:
            time.sleep(min(delay, 5.0))
        from .windows import acquire_daemon_mutex
        if not acquire_daemon_mutex(home, port):
            log.info("another cross-copy daemon already owns home=%s port=%d",
                     home, port)
            return

    config.write_daemon_info(port)
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    discovery = Discovery(port)
    _cleanup_state["discovery"] = discovery
    discovery.start()        # mDNS register + browse (no-op if disabled)
    discovery.start_hello()  # reciprocal-hello sender loop

    updater = Updater()
    _cleanup_state["updater"] = updater
    updater.start(restart=_restart_daemon)

    app = create_app(discovery, updater)

    log.info("cross-copy %s daemon starting: device=%s (%s) home=%s port=%d",
             __version__, cfg["device_name"], cfg["device_id"], home, port)
    try:
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
