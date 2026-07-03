"""cross-copy daemon entry point: `python -m crosscopy.daemon`.

Writes daemon.json ({"pid", "port"}) to the cross-copy home dir, starts
zeroconf discovery (unless CROSSCOPY_NO_MDNS=1), and runs the Flask server
bound to 0.0.0.0. Logs to stdout — the CLI's `ccp daemon start` redirects
stdout/stderr to ~/.crosscopy/daemon.log. daemon.json is removed on clean
shutdown (SIGINT/SIGTERM/atexit).
"""

import atexit
import logging
import signal
import sys

from . import __version__, config
from .discovery import Discovery
from .server import create_app

log = logging.getLogger("crosscopy.daemon")

_cleanup_state = {"done": False, "discovery": None}


def _cleanup():
    if _cleanup_state["done"]:
        return
    _cleanup_state["done"] = True
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


def main():
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

    config.write_daemon_info(port)
    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    discovery = Discovery(port)
    _cleanup_state["discovery"] = discovery
    discovery.start()

    app = create_app(discovery)

    log.info("cross-copy %s daemon starting: device=%s (%s) home=%s port=%d",
             __version__, cfg["device_name"], cfg["device_id"], home, port)
    try:
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
