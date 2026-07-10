"""Configuration and home-directory handling for cross-copy.

Everything lives under ~/.crosscopy/ (override with CROSSCOPY_HOME):
  config.json    device name, device id (uuid4), manual peers list,
                 auto_update flag (default true), receive_dir (default
                 ~/Downloads/cross-copy), notifications flag (default true)
  daemon.json    running daemon identity: pid, port, and Windows start_time
  daemon.log     daemon output (written by the CLI's daemon-start redirect)
  staging/       files uploaded through the web UI
  clipboard.json current clipboard manifest
"""

import json
import os
import socket
import sys
import uuid
from pathlib import Path

DEFAULT_PORT = 7373
DEFAULT_RECEIVE_DIR = "~/Downloads/cross-copy"


# ---------------------------------------------------------------------------
# Paths

def get_home() -> Path:
    """Return the cross-copy home directory, creating it if needed."""
    env = os.environ.get("CROSSCOPY_HOME")
    home = Path(env).expanduser() if env else Path.home() / ".crosscopy"
    home.mkdir(parents=True, exist_ok=True)
    return home


def config_path() -> Path:
    return get_home() / "config.json"


def clipboard_path() -> Path:
    return get_home() / "clipboard.json"


def daemon_json_path() -> Path:
    return get_home() / "daemon.json"


def daemon_log_path() -> Path:
    return get_home() / "daemon.log"


def staging_dir() -> Path:
    path = get_home() / "staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Port / platform

def get_port() -> int:
    """Daemon port: CROSSCOPY_PORT env var or DEFAULT_PORT."""
    raw = os.environ.get("CROSSCOPY_PORT", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_PORT


def platform_name() -> str:
    """Stable peer-facing platform name (darwin/linux/win32)."""
    if sys.platform.startswith("darwin"):
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


# ---------------------------------------------------------------------------
# config.json

def _atomic_write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_config() -> dict:
    """Load config.json, filling in (and persisting) any missing defaults."""
    path = config_path()
    cfg = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                cfg = loaded
        except (OSError, ValueError):
            cfg = {}

    changed = False
    if not cfg.get("device_id"):
        cfg["device_id"] = str(uuid.uuid4())
        changed = True
    if not cfg.get("device_name"):
        cfg["device_name"] = socket.gethostname()
        changed = True
    if not isinstance(cfg.get("manual_peers"), list):
        cfg["manual_peers"] = []
        changed = True
    if not isinstance(cfg.get("auto_update"), bool):
        cfg["auto_update"] = True
        changed = True
    if not cfg.get("receive_dir") or not isinstance(cfg.get("receive_dir"), str):
        cfg["receive_dir"] = DEFAULT_RECEIVE_DIR
        changed = True
    if not isinstance(cfg.get("notifications"), bool):
        cfg["notifications"] = True
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    _atomic_write_json(config_path(), cfg)


def get_device_id() -> str:
    return load_config()["device_id"]


def get_device_name() -> str:
    return load_config()["device_name"]


def set_device_name(name: str) -> None:
    cfg = load_config()
    cfg["device_name"] = name
    save_config(cfg)


def get_auto_update() -> bool:
    """Whether the daemon self-updates automatically (default true)."""
    return bool(load_config().get("auto_update", True))


def get_receive_dir() -> Path:
    """Where accepted offers land (default ~/Downloads/cross-copy), with
    ~ expanded. The directory is NOT created here — callers mkdir on use."""
    raw = load_config().get("receive_dir") or DEFAULT_RECEIVE_DIR
    return Path(str(raw)).expanduser()


def get_notifications() -> bool:
    """Whether desktop notifications are enabled (default true)."""
    return bool(load_config().get("notifications", True))


# ---------------------------------------------------------------------------
# Manual peers

def get_manual_peers() -> list:
    """List of {"host": str, "port": int} entries from config.json."""
    peers = []
    for entry in load_config().get("manual_peers", []):
        if isinstance(entry, dict) and entry.get("host"):
            peers.append({
                "host": str(entry["host"]),
                "port": int(entry.get("port", DEFAULT_PORT)),
            })
    return peers


def add_manual_peer(host: str, port: int) -> None:
    """Add a manual peer to config.json (deduplicated by host:port)."""
    cfg = load_config()
    peers = [p for p in cfg.get("manual_peers", [])
             if isinstance(p, dict) and p.get("host")]
    for p in peers:
        if p["host"] == host and int(p.get("port", DEFAULT_PORT)) == int(port):
            return
    peers.append({"host": host, "port": int(port)})
    cfg["manual_peers"] = peers
    save_config(cfg)


# ---------------------------------------------------------------------------
# daemon.json

def write_daemon_info(port: int, pid: int = None) -> None:
    process_id = pid if pid is not None else os.getpid()
    info = {"pid": process_id, "port": int(port)}
    if sys.platform == "win32":
        from .windows import process_start_time
        started = process_start_time(process_id)
        if started is not None:
            info["start_time"] = started
    _atomic_write_json(daemon_json_path(), info)


def read_daemon_info():
    """Return {"pid", "port"} for the (last known) running daemon, or None."""
    path = daemon_json_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            info = json.load(fh)
        if isinstance(info, dict) and "pid" in info and "port" in info:
            return {"pid": int(info["pid"]), "port": int(info["port"])}
    except (OSError, ValueError):
        pass
    return None


def remove_daemon_info() -> None:
    try:
        daemon_json_path().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Misc helpers

def format_size(num_bytes) -> str:
    """Human-friendly size: 512 B, 2.1 KB, 3.4 MB, 1.2 GB."""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d B" % int(size)
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%d B" % int(num_bytes)
