"""Peer discovery for cross-copy.

Registers this daemon as `<device_id>._crosscopy._tcp.local.` via zeroconf and
browses for other instances. The peer registry is keyed by device id and
merges mDNS, manual (config.json) and reciprocal-"hello" sightings: the
freshest sighting wins and its "source" is the one shown. Manual peers never
expire; hello peers expire after 10 minutes without contact. A "peers" event
is published on add / update-that-changes-something / expiry — not on every
refresh.

Reciprocal discovery (v0.3): a hello sender loop POSTs /api/hello (our device
info) to every known peer on daemon start, every 60 s, when a new mDNS peer
appears, and whenever hello_now() is called (the server calls it on local
clipboard changes so remote UIs react instantly). This fixes one-way mDNS
visibility: if either side can reach the other, both end up knowing each
other.

CROSSCOPY_NO_MDNS=1 disables zeroconf entirely; zeroconf import or
registration failures are logged and swallowed so the daemon still runs with
manual + hello peers only.
"""

import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import __version__, config
from .events import bus

log = logging.getLogger("crosscopy.discovery")

SERVICE_TYPE = "_crosscopy._tcp.local."
PING_TIMEOUT = 2.0
HELLO_TIMEOUT = 2.0
HELLO_INTERVAL = 60.0      # periodic hello round
HELLO_EXPIRY = 10 * 60.0   # hello-sourced peers expire after 10 min

try:
    from zeroconf import InterfaceChoice, ServiceBrowser, ServiceInfo, Zeroconf
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


def get_local_ips() -> list:
    """All non-loopback IPv4 addresses of this machine (best effort).

    Used to register the mDNS service on every interface so peers on any
    attached network can reach us. Falls back to the single routing-trick IP."""
    ips = set()
    primary = get_local_ip()
    if not primary.startswith("127."):
        ips.add(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET, socket.SOCK_STREAM):
            addr = info[4][0]
            if not addr.startswith("127."):
                ips.add(addr)
    except OSError:
        pass
    try:  # zeroconf enumerates interfaces properly when available
        from zeroconf import get_all_addresses
        for addr in get_all_addresses():
            if not addr.startswith("127."):
                ips.add(addr)
    except Exception:
        pass
    return sorted(ips) or [primary]


class Discovery:
    """Zeroconf registration/browsing, hello sender loop, and the unified
    id-keyed peer registry.

    Safe to use even when mDNS is disabled or zeroconf is broken: manual and
    hello peers still work.
    """

    def __init__(self, port: int):
        self.port = int(port)
        self._lock = threading.Lock()
        self._peers = {}           # device id -> peer record dict
        self._mdns_names = {}      # zeroconf service name -> device id
        self._zeroconf = None
        self._browser = None
        self._service_info = None
        self._stop = threading.Event()
        self._hello_wake = threading.Event()
        self._hello_thread = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if mdns_disabled():
            log.info("CROSSCOPY_NO_MDNS=1: mDNS discovery disabled")
            return
        if not _HAVE_ZEROCONF:
            log.warning("zeroconf not importable; running without mDNS")
            return
        try:
            # Browse and register on ALL interfaces so multi-homed machines
            # (VPNs, docker bridges, wifi+ethernet) stay discoverable.
            self._zeroconf = Zeroconf(interfaces=InterfaceChoice.All)
            device_id = config.get_device_id()
            props = {
                "id": device_id,
                "name": config.get_device_name(),
                "platform": config.platform_name(),
                "version": __version__,
            }
            addresses = []
            for ip in get_local_ips():
                try:
                    addresses.append(socket.inet_aton(ip))
                except OSError:
                    pass
            if not addresses:
                addresses = [socket.inet_aton(get_local_ip())]
            self._service_info = ServiceInfo(
                SERVICE_TYPE,
                "%s.%s" % (device_id, SERVICE_TYPE),
                addresses=addresses,
                port=self.port,
                properties=props,
            )
            self._zeroconf.register_service(self._service_info)
            self._browser = ServiceBrowser(self._zeroconf, SERVICE_TYPE, self)
            log.info("mDNS: registered %s on port %d (addresses: %s)",
                     device_id, self.port, ", ".join(get_local_ips()))
        except Exception as exc:
            log.warning("mDNS setup failed (%s); running without mDNS", exc)
            self._shutdown_zeroconf()

    def start_hello(self) -> None:
        """Start the background hello sender loop (works without mDNS)."""
        if self._hello_thread is not None:
            return
        self._hello_thread = threading.Thread(
            target=self._hello_loop, name="crosscopy-hello", daemon=True)
        self._hello_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._hello_wake.set()
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
            peer_id = self._mdns_names.pop(name, None)
            removed = None
            if peer_id is not None:
                record = self._peers.get(peer_id)
                # Only drop the record if mDNS was its latest sighting;
                # manual/hello records outlive the mDNS announcement.
                if record is not None and record.get("source") == "mdns":
                    removed = self._peers.pop(peer_id, None)
        log.info("mDNS: peer left: %s", name)
        if removed is not None:
            bus.publish("peers")

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
        with self._lock:
            known = peer_id in self._peers
            self._mdns_names[name] = peer_id
        is_new = self.record_peer(
            peer_id,
            name=props.get("name", peer_id),
            host=addresses[0],
            port=info.port or config.DEFAULT_PORT,
            platform=props.get("platform", "unknown"),
            version=props.get("version", "unknown"),
            source="mdns",
        )
        if is_new or not known:
            log.info("mDNS: peer seen: %s (%s:%s)",
                     props.get("name", peer_id), addresses[0], info.port)
            # Say hello right away so they learn about us too (fixes one-way
            # mDNS visibility).
            self.hello_now()

    # -- unified registry ----------------------------------------------------

    def record_peer(self, peer_id, name, host, port, platform, version,
                    source) -> bool:
        """Merge a sighting into the id-keyed registry (freshest wins; the
        "source" shown is the most recent sighting's). Publishes a "peers"
        event when the sighting adds a peer or changes something meaningful —
        not on plain last_seen refreshes. Returns True if the peer was new."""
        if not peer_id or peer_id == config.get_device_id():
            return False
        record = {
            "id": peer_id,
            "name": name or peer_id,
            "host": host,
            "port": int(port),
            "platform": platform or "unknown",
            "version": version or "unknown",
            "last_seen": time.time(),
            "source": source,
        }
        with self._lock:
            old = self._peers.get(peer_id)
            self._peers[peer_id] = record
        is_new = old is None
        # "source" flips (mdns <-> hello sightings alternate) are not
        # meaningful changes; everything else is.
        changed = is_new or any(
            old.get(key) != record[key]
            for key in ("name", "host", "port", "platform", "version"))
        if changed:
            if is_new:
                log.info("peer added: %s (%s:%s) via %s",
                         record["name"], host, port, source)
            bus.publish("peers")
        return is_new

    def _manual_hostports(self) -> set:
        return set((p["host"], int(p["port"])) for p in config.get_manual_peers())

    def _expire_peers(self) -> None:
        """Drop hello-sourced peers not seen for HELLO_EXPIRY. Manual peers
        never expire (also protects hello records for configured host:ports)."""
        now = time.time()
        manual = self._manual_hostports()
        expired = []
        with self._lock:
            for peer_id, record in list(self._peers.items()):
                if record.get("source") != "hello":
                    continue
                if (record.get("host"), int(record.get("port", 0))) in manual:
                    continue
                if now - record.get("last_seen", 0) > HELLO_EXPIRY:
                    expired.append(self._peers.pop(peer_id))
        for record in expired:
            log.info("peer expired: %s (%s)", record.get("name"),
                     record.get("host"))
        if expired:
            bus.publish("peers")

    def get_peers(self) -> list:
        """Merged peer list from the unified registry.

        Manual peers from config.json are probed live (short timeout) to learn
        id/name/platform; unreachable manual peers fall back to their last
        known record, or are skipped if they've never responded."""
        self._expire_peers()
        my_id = config.get_device_id()

        known_hostports = set()
        with self._lock:
            for record in self._peers.values():
                known_hostports.add((record["host"], int(record["port"])))
        for entry in config.get_manual_peers():
            # Probe manual entries we have no fresh record for (and re-probe
            # all manual entries to keep their info current).
            self._probe_manual(entry["host"], entry["port"])

        with self._lock:
            peers = [dict(r) for r in self._peers.values() if r["id"] != my_id]
        return sorted(peers, key=lambda p: p.get("name", ""))

    def _probe_manual(self, host: str, port: int):
        try:
            resp = requests.get(
                "http://%s:%d/api/ping" % (host, int(port)), timeout=PING_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return  # keep whatever record (if any) we already have
        self.record_peer(
            data.get("id", "%s:%d" % (host, int(port))),
            name=data.get("name", host),
            host=host,
            port=int(port),
            platform=data.get("platform", "unknown"),
            version=data.get("version", "unknown"),
            source="manual",
        )

    # -- hello sender ---------------------------------------------------------

    def hello_now(self) -> None:
        """Ask the hello loop to run a round immediately (non-blocking; safe
        to call from request handlers)."""
        self._hello_wake.set()

    def _hello_loop(self) -> None:
        # A round on start, then every HELLO_INTERVAL or whenever woken.
        while not self._stop.is_set():
            try:
                self._expire_peers()
                self._send_hellos()
            except Exception as exc:  # never kill the loop
                log.warning("hello round failed: %s", exc)
            self._hello_wake.wait(timeout=HELLO_INTERVAL)
            self._hello_wake.clear()

    def _hello_targets(self) -> list:
        """(host, port) pairs to greet: every registry record plus every
        manual config entry (deduplicated)."""
        targets = {}
        with self._lock:
            for record in self._peers.values():
                targets[(record["host"], int(record["port"]))] = record
        for entry in config.get_manual_peers():
            targets.setdefault((entry["host"], int(entry["port"])), None)
        return list(targets.items())

    def _send_hellos(self) -> None:
        targets = self._hello_targets()
        if not targets:
            return
        payload = {
            "id": config.get_device_id(),
            "name": config.get_device_name(),
            "platform": config.platform_name(),
            "version": __version__,
            "port": self.port,
        }
        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            pool.map(lambda t: self._send_hello(t[0], t[1], payload), targets)

    def _send_hello(self, hostport, record, payload) -> None:
        host, port = hostport
        try:
            resp = requests.post("http://%s:%d/api/hello" % (host, port),
                                 json=payload, timeout=HELLO_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.debug("hello to %s:%d failed: %s", host, port, exc)
            return
        peer_id = data.get("id")
        if not peer_id or peer_id == config.get_device_id():
            return
        # A successful hello response is a fresh sighting; keep the record's
        # existing source (manual entries stay manual, mdns stays mdns).
        source = (record or {}).get("source") or (
            "manual" if (host, int(port)) in self._manual_hostports()
            else "hello")
        self.record_peer(
            peer_id,
            name=data.get("name", host),
            host=host,
            port=int(data.get("port") or port),
            platform=data.get("platform", "unknown"),
            version=data.get("version", "unknown"),
            source=source,
        )
