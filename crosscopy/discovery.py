"""Peer discovery for cross-copy.

Registers this daemon as `<device_id>._crosscopy._tcp.local.` via zeroconf and
browses for other instances. The peer registry merges mDNS-discovered peers
with manual peers from config.json (added via `ccp add` / POST /api/peers/add).

CROSSCOPY_NO_MDNS=1 disables zeroconf entirely; zeroconf import or
registration failures are logged and swallowed so the daemon still runs with
manual peers only.
"""

import logging
import os
import socket
import threading
import time

import requests

from . import __version__, config

log = logging.getLogger("crosscopy.discovery")

SERVICE_TYPE = "_crosscopy._tcp.local."
PING_TIMEOUT = 2.0

try:
    from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf
    _HAVE_ZEROCONF = True
except Exception as _exc:  # pragma: no cover - import failure path
    _HAVE_ZEROCONF = False
    log.warning("zeroconf unavailable (%s); mDNS discovery disabled", _exc)


def mdns_disabled() -> bool:
    return os.environ.get("CROSSCOPY_NO_MDNS") == "1"


def get_local_ip() -> str:
    """Best-effort LAN IP of this machine (no traffic is actually sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class Discovery:
    """Zeroconf registration/browsing plus a merged peer registry.

    Safe to use even when mDNS is disabled or zeroconf is broken: get_peers()
    then simply returns manual peers from config.json.
    """

    def __init__(self, port: int):
        self.port = int(port)
        self._lock = threading.Lock()
        self._mdns_peers = {}      # service name -> peer dict
        self._manual_cache = {}    # (host, port) -> last successful peer dict
        self._zeroconf = None
        self._browser = None
        self._service_info = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if mdns_disabled():
            log.info("CROSSCOPY_NO_MDNS=1: mDNS discovery disabled")
            return
        if not _HAVE_ZEROCONF:
            log.warning("zeroconf not importable; running with manual peers only")
            return
        try:
            self._zeroconf = Zeroconf()
            device_id = config.get_device_id()
            props = {
                "id": device_id,
                "name": config.get_device_name(),
                "platform": config.platform_name(),
                "version": __version__,
            }
            self._service_info = ServiceInfo(
                SERVICE_TYPE,
                "%s.%s" % (device_id, SERVICE_TYPE),
                addresses=[socket.inet_aton(get_local_ip())],
                port=self.port,
                properties=props,
            )
            self._zeroconf.register_service(self._service_info)
            self._browser = ServiceBrowser(self._zeroconf, SERVICE_TYPE, self)
            log.info("mDNS: registered %s on port %d", device_id, self.port)
        except Exception as exc:
            log.warning("mDNS setup failed (%s); running with manual peers only", exc)
            self._shutdown_zeroconf()

    def stop(self) -> None:
        self._shutdown_zeroconf()

    def _shutdown_zeroconf(self) -> None:
        zc = self._zeroconf
        self._zeroconf = None
        self._browser = None
        if zc is None:
            return
        try:
            if self._service_info is not None:
                zc.unregister_service(self._service_info)
        except Exception:
            pass
        try:
            zc.close()
        except Exception:
            pass
        self._service_info = None

    # -- zeroconf listener callbacks (duck-typed ServiceListener) -----------

    def add_service(self, zc, type_, name):
        self._refresh_service(zc, type_, name)

    def update_service(self, zc, type_, name):
        self._refresh_service(zc, type_, name)

    def remove_service(self, zc, type_, name):
        with self._lock:
            self._mdns_peers.pop(name, None)
        log.info("mDNS: peer left: %s", name)

    def _refresh_service(self, zc, type_, name):
        try:
            info = zc.get_service_info(type_, name, timeout=3000)
        except Exception as exc:
            log.debug("mDNS: get_service_info failed for %s: %s", name, exc)
            return
        if info is None:
            return
        props = {}
        for key, value in (info.properties or {}).items():
            try:
                k = key.decode("utf-8") if isinstance(key, bytes) else str(key)
                v = value.decode("utf-8") if isinstance(value, bytes) else value
            except Exception:
                continue
            props[k] = v
        peer_id = props.get("id") or name.split(".", 1)[0]
        if peer_id == config.get_device_id():
            return  # ourselves
        addresses = []
        try:
            addresses = info.parsed_addresses()
        except Exception:
            pass
        if not addresses:
            return
        peer = {
            "id": peer_id,
            "name": props.get("name", peer_id),
            "host": addresses[0],
            "port": info.port or config.DEFAULT_PORT,
            "platform": props.get("platform", "unknown"),
            "version": props.get("version", "unknown"),
            "last_seen": time.time(),
            "source": "mdns",
        }
        with self._lock:
            self._mdns_peers[name] = peer
        log.info("mDNS: peer seen: %s (%s:%s)", peer["name"], peer["host"], peer["port"])

    # -- registry -----------------------------------------------------------

    def get_peers(self) -> list:
        """Merged peer list: mDNS peers plus manual peers from config.json.

        Manual peers are pinged (short timeout) to learn id/name/platform;
        unreachable manual peers fall back to their last known info, or are
        skipped if they've never responded.
        """
        my_id = config.get_device_id()
        merged = {}
        with self._lock:
            mdns_peers = list(self._mdns_peers.values())
        for peer in mdns_peers:
            if peer["id"] != my_id:
                merged[peer["id"]] = peer

        for entry in config.get_manual_peers():
            peer = self._probe_manual(entry["host"], entry["port"])
            if peer and peer["id"] != my_id and peer["id"] not in merged:
                merged[peer["id"]] = peer

        return sorted(merged.values(), key=lambda p: p.get("name", ""))

    def _probe_manual(self, host: str, port: int):
        key = (host, int(port))
        try:
            resp = requests.get(
                "http://%s:%d/api/ping" % (host, int(port)), timeout=PING_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
            peer = {
                "id": data.get("id", "%s:%d" % key),
                "name": data.get("name", host),
                "host": host,
                "port": int(port),
                "platform": data.get("platform", "unknown"),
                "version": data.get("version", "unknown"),
                "last_seen": time.time(),
                "source": "manual",
            }
            with self._lock:
                self._manual_cache[key] = peer
            return peer
        except Exception:
            with self._lock:
                return self._manual_cache.get(key)
