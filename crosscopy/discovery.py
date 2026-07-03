"""Peer discovery for cross-copy.

Registers this daemon as `<device_id>._crosscopy._tcp.local.` via zeroconf and
browses for other instances. The peer registry is keyed by device id and
merges mDNS, manual (config.json) and reciprocal-"hello" sightings: the
freshest sighting wins and its "source" is the one shown. Manual peers never
expire.

Verify-before-drop (v0.4.2): mDNS removal events and hello-TTL staleness are
treated as *hearsay*, never as proof of death — macs in wifi power-save churn
their mDNS announcements every few seconds while staying perfectly reachable.
A registry record therefore moves through a small state machine:

    ACTIVE --(mDNS remove | last_seen older than HELLO_EXPIRY)--> SUSPECT
    SUSPECT --(any answered contact: probe, hello either way,
               confirm_contact, or a fresh mDNS/manual sighting)--> ACTIVE
    SUSPECT --(verification probe failed AND SUSPECT_GRACE elapsed AND no
               answered contact within CONTACT_FRESH)--> removed

Suspects are probed in the background (GET /api/ping on every known address,
short timeout) by a small capped pool, with a per-peer cooldown so a flapping
LAN never causes thundering probes. Entering/leaving SUSPECT is invisible to
clients: a "peers" event fires only on real membership or host/name/port
changes (suppressed transitions are logged at debug).

Reciprocal discovery (v0.3): a hello sender loop POSTs /api/hello (our device
info) to every known peer on daemon start, every 60 s, when a new mDNS peer
appears, and whenever hello_now() is called (the server calls it on local
clipboard changes so remote UIs react instantly). This fixes one-way mDNS
visibility: if either side can reach the other, both end up knowing each
other. A successful outbound hello counts as answered contact, so a reachable
peer stays fresh even if its own hellos never arrive here. Hello payloads and
responses carry "clip" (the current clipboard_id) so receivers can publish a
"peers" event only when the sender's clipboard actually changed.

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
PROBE_TIMEOUT = 1.5        # per-candidate reachability probe
HELLO_TIMEOUT = 2.0
HELLO_INTERVAL = 60.0      # periodic hello round
HELLO_EXPIRY = 10 * 60.0   # peers turn suspect after this long without a sighting

# Verify-before-drop state machine (see module docstring):
CONTACT_FRESH = 90.0       # answered contact this recent => never removed
SUSPECT_GRACE = 75.0       # minimum time in SUSPECT before removal is allowed
PROBE_COOLDOWN = 30.0      # per-peer minimum interval between verification probes
VERIFY_WORKERS = 3         # cap on concurrent verification probes

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


def _current_clip_id() -> str:
    """The local clipboard_id ("" when empty) — sent in hello payloads and
    responses so peers can tell whether our clipboard actually changed."""
    try:
        from . import clipboard
        manifest = clipboard.load_clipboard()
        return str((manifest or {}).get("clipboard_id") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Peer host selection
#
# Multi-homed peers advertise ALL their IPv4s over mDNS (docker bridges,
# tailscale CGNAT, the real LAN IP, ...). Blindly taking addresses[0] can
# pick e.g. 172.17.0.1 and every transfer then times out. Instead we keep
# every candidate on the peer record ("addresses") and choose "host" by:
#   (a) an address confirmed by actual contact (hello source IP / last
#       successful outbound request) always wins and is never clobbered by
#       an unprobed mDNS refresh;
#   (b) otherwise probe candidates concurrently (GET /api/ping) and take a
#       reachable one, preferring RFC1918 LAN ranges over CGNAT 100.64/10;
#   (c) otherwise fall back to the first address.
# The choice is cached; we only re-select when contact fails (the server's
# retry path calls confirm_contact() with whatever address actually worked).

def _addr_pref(addr: str) -> int:
    """Preference rank for a candidate address: RFC1918 LAN ranges (0) over
    anything else (1) over CGNAT 100.64/10 (2, e.g. tailscale)."""
    parts = addr.split(".")
    try:
        a, b = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return 1
    if a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
        return 0
    if a == 100 and 64 <= b <= 127:
        return 2
    return 1


def _ping_addr(addr: str, port: int) -> bool:
    """One reachability probe: GET /api/ping with a short timeout."""
    try:
        resp = requests.get("http://%s:%d/api/ping" % (addr, int(port)),
                            timeout=PROBE_TIMEOUT)
        return resp.status_code == 200
    except Exception:
        return False


def select_host(addresses: list, port: int, prober=None) -> str:
    """Pick the best host among candidate addresses: probe all concurrently
    and take a reachable one (preferring LAN-looking addresses, see
    _addr_pref); when nothing answers, fall back to the first address.
    `prober(addr, port) -> bool` is injectable for tests."""
    addresses = [a for a in addresses if a]
    if not addresses:
        return ""
    if len(addresses) == 1:
        return addresses[0]
    prober = prober or _ping_addr
    with ThreadPoolExecutor(max_workers=min(8, len(addresses))) as pool:
        alive = list(pool.map(lambda a: prober(a, port), addresses))
    reachable = [a for a, ok in zip(addresses, alive) if ok]
    if reachable:
        return min(reachable, key=lambda a: (_addr_pref(a), addresses.index(a)))
    return addresses[0]


class Discovery:
    """Zeroconf registration/browsing, hello sender loop, and the unified
    id-keyed peer registry with verify-before-drop (see module docstring).

    Registry records carry underscore-prefixed bookkeeping fields that are
    stripped from get_peers() output:
      _last_contact   epoch of the last *answered* contact (hello either
                      direction, confirm_contact, successful probe)
      _suspect_since  epoch the record entered SUSPECT, or None (ACTIVE)
      _last_probe     epoch of the last completed verification probe
      _probe_ok       result of that probe (None until first probe)
      _clip           the peer's last reported clipboard_id (hello "clip")

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
        self._prober = None        # test hook: prober(addr, port) -> bool
        self._probing = set()      # peer ids with an in-flight verify probe
        self._verify_pool = ThreadPoolExecutor(
            max_workers=VERIFY_WORKERS, thread_name_prefix="crosscopy-verify")

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
        self._verify_pool.shutdown(wait=False)
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
        """An mDNS goodbye/TTL expiry is hearsay, not proof of death: macs in
        wifi power-save churn announcements every few seconds while staying
        reachable. Never drop the record here — mark it SUSPECT and verify by
        probing; _reap_peers() removes it only if the probe fails AND the
        grace period elapses AND it hasn't answered anything recently."""
        with self._lock:
            peer_id = self._mdns_names.pop(name, None)
            record = self._peers.get(peer_id) if peer_id else None
        if record is None:
            log.debug("mDNS: announcement gone for unknown service %s", name)
            return
        log.info("mDNS: announcement gone: %s (%s) — verifying before drop",
                 record.get("name"), name.split(".", 1)[0])
        self._mark_suspect(peer_id, "mdns announcement removed")

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
            addresses=addresses,
        )
        if is_new or not known:
            log.info("mDNS: peer seen: %s (%s:%s)",
                     props.get("name", peer_id), addresses[0], info.port)
            # Say hello right away so they learn about us too (fixes one-way
            # mDNS visibility).
            self.hello_now()

    # -- unified registry ----------------------------------------------------

    def record_peer(self, peer_id, name, host, port, platform, version,
                    source, addresses=None, clip=None) -> bool:
        """Merge a sighting into the id-keyed registry (freshest wins; the
        "source" shown is the most recent sighting's). All candidate
        addresses ever seen for the peer are kept on the record
        ("addresses"); "host" is the selected one (see select_host above).
        A hello/manual sighting's host is contact-confirmed ground truth; an
        mDNS refresh never overwrites a confirmed host, and an already-cached
        unconfirmed choice is kept rather than re-probed. Any sighting clears
        a SUSPECT flag (the peer is evidently around); hello/manual sightings
        additionally refresh _last_contact (they answered / spoke to us).

        `clip` is the peer's clipboard_id from a hello exchange (None =
        unknown/not carried; "" = empty clipboard).

        Publishes a "peers" event only on real membership or
        name/host/port/platform/version/clip changes — plain last_seen
        refreshes, address-set-only changes, source flips and host_confirmed
        flips are suppressed (logged at debug). Returns True if the peer was
        new."""
        if not peer_id or peer_id == config.get_device_id():
            return False
        port = int(port)
        with self._lock:
            old = self._peers.get(peer_id)
            old = dict(old) if old is not None else None
        cand = []
        for addr in list(addresses or []) + [host] \
                + list((old or {}).get("addresses") or []):
            if addr and addr not in cand:
                cand.append(addr)

        # Host selection. hello = the request's source IP; manual = an
        # address we just successfully pinged: both are confirmed by actual
        # contact and win outright.
        confirmed = source in ("hello", "manual")
        if confirmed:
            chosen = host
        elif old and old.get("host_confirmed") and old.get("host"):
            chosen = old["host"]      # never clobber a confirmed host
            confirmed = True
        elif old and old.get("host") in cand:
            chosen = old["host"]      # cached choice; re-probe on failure only
        elif len(cand) > 1:
            chosen = select_host(cand, port, prober=self._prober)
        else:
            chosen = cand[0] if cand else host

        now = time.time()
        record = {
            "id": peer_id,
            "name": name or peer_id,
            "host": chosen,
            "port": port,
            "platform": platform or "unknown",
            "version": version or "unknown",
            "last_seen": now,
            "source": source,
            "addresses": cand,
            "host_confirmed": confirmed,
        }
        answered = source in ("hello", "manual")
        with self._lock:
            # select_host may have probed for a while; if a confirmed
            # sighting (hello) landed meanwhile, keep its host.
            current = self._peers.get(peer_id)
            if (current is not None and current.get("host_confirmed")
                    and not confirmed and current.get("host")):
                record["host"] = current["host"]
                record["host_confirmed"] = True
                for addr in current.get("addresses") or []:
                    if addr not in record["addresses"]:
                        record["addresses"].append(addr)
            # Carry over verification bookkeeping from the live record.
            live = current if current is not None else (old or {})
            record["_last_probe"] = live.get("_last_probe", 0)
            record["_probe_ok"] = live.get("_probe_ok")
            record["_clip"] = live.get("_clip") if clip is None else clip
            record["_last_contact"] = now if answered \
                else live.get("_last_contact", 0)
            if live.get("_suspect_since") is not None:
                log.debug("peer %s recovered from suspect (fresh %s sighting)",
                          record["name"], source)
            record["_suspect_since"] = None
            self._peers[peer_id] = record
        is_new = old is None
        # Membership and what clients display (name/host/port/...) are
        # meaningful; a "clip" change means the peer's clipboard changed and
        # clients must refetch. Source flips (mdns <-> hello sightings
        # alternate), address-set changes and host_confirmed flips are not.
        changed = is_new or any(
            old.get(key) != record[key]
            for key in ("name", "host", "port", "platform", "version")
        ) or (clip is not None and old.get("_clip") != clip)
        if changed:
            if is_new:
                log.info("peer added: %s (%s:%s) via %s [candidates: %s]",
                         record["name"], record["host"], port, source,
                         ", ".join(cand))
            bus.publish("peers")
        elif (set(old.get("addresses") or []) != set(record["addresses"])
                or old.get("source") != source
                or old.get("host_confirmed") != record["host_confirmed"]):
            log.debug("peers event suppressed for %s (%s sighting; no "
                      "membership/host change)", record["name"], source)
        return is_new

    def confirm_contact(self, peer_id, host) -> None:
        """A real request exchange with `host` just succeeded: make it the
        peer's selected host, mark it contact-confirmed so mDNS refreshes
        won't clobber it, refresh _last_contact and clear any SUSPECT flag.
        Called by the server after any successful outbound peer request
        (including the retry path that found a working alternate address).
        Publishes a "peers" event only when the host actually changed."""
        if not peer_id or not host:
            return
        now = time.time()
        with self._lock:
            record = self._peers.get(peer_id)
            if record is None:
                return
            addrs = record.setdefault("addresses", [])
            if host not in addrs:
                addrs.insert(0, host)
            host_changed = record.get("host") != host
            flag_changed = not record.get("host_confirmed")
            record["host"] = host
            record["host_confirmed"] = True
            record["last_seen"] = now
            record["_last_contact"] = now
            if record.get("_suspect_since") is not None:
                log.debug("peer %s recovered from suspect (answered contact)",
                          record.get("name"))
            record["_suspect_since"] = None
        if host_changed:
            log.info("peer %s host confirmed by contact: %s", peer_id, host)
            bus.publish("peers")
        elif flag_changed:
            log.debug("peers event suppressed for %s (host_confirmed flip "
                      "only)", peer_id)

    def inject_record_for_tests(self, record: dict) -> None:
        """TEST ONLY: overwrite a raw registry record, bypassing selection
        and probing. Used by the network test harness (via the
        CROSSCOPY_TEST_HOOKS server endpoint) to simulate a peer whose mDNS
        sighting picked an unroutable address."""
        with self._lock:
            self._peers[record["id"]] = dict(record)

    def _manual_hostports(self) -> set:
        return set((p["host"], int(p["port"])) for p in config.get_manual_peers())

    # -- suspect verification (verify-before-drop) ---------------------------

    def _mark_suspect(self, peer_id, reason) -> None:
        """Move a record into SUSPECT (idempotent) and kick a background
        verification probe. Publishes nothing — entering SUSPECT is invisible
        to clients."""
        with self._lock:
            record = self._peers.get(peer_id)
            if record is None:
                return
            if record.get("_suspect_since") is None:
                record["_suspect_since"] = time.time()
                log.debug("peer %s suspect (%s); removal suppressed pending "
                          "verification", record.get("name"), reason)
        self._schedule_probe(peer_id)

    def _schedule_probe(self, peer_id) -> None:
        """Submit a verification probe for a peer unless one is already in
        flight or ran within PROBE_COOLDOWN (no thundering probes; the pool
        caps concurrency at VERIFY_WORKERS)."""
        if self._stop.is_set():
            return
        with self._lock:
            record = self._peers.get(peer_id)
            if record is None or peer_id in self._probing:
                return
            if time.time() - record.get("_last_probe", 0) < PROBE_COOLDOWN:
                return
            self._probing.add(peer_id)
        try:
            self._verify_pool.submit(self._verify_peer, peer_id)
        except RuntimeError:  # pool shut down during daemon exit
            with self._lock:
                self._probing.discard(peer_id)

    def _verify_peer(self, peer_id) -> None:
        """Background verification probe: GET /api/ping on every known
        address of the peer (short timeout each). An answer clears the
        suspicion and confirms the answering host; a full miss records the
        failure so _reap_peers() can remove the peer once SUSPECT_GRACE has
        elapsed."""
        try:
            with self._lock:
                record = self._peers.get(peer_id)
                if record is None:
                    return
                hosts = [record.get("host")] \
                    + list(record.get("addresses") or [])
                port = int(record.get("port") or config.DEFAULT_PORT)
                pname = record.get("name")
            prober = self._prober or _ping_addr
            alive_host = None
            seen = set()
            for host in hosts:
                if not host or host in seen or self._stop.is_set():
                    continue
                seen.add(host)
                if prober(host, port):
                    alive_host = host
                    break
            now = time.time()
            with self._lock:
                record = self._peers.get(peer_id)
                if record is None:
                    return
                record["_last_probe"] = now
                record["_probe_ok"] = alive_host is not None
            if alive_host is not None:
                log.debug("suspect peer %s answered probe at %s:%d; keeping",
                          pname, alive_host, port)
                self.confirm_contact(peer_id, alive_host)
            else:
                log.debug("suspect peer %s failed probe on all addresses "
                          "(%s); will remove after grace",
                          pname, ", ".join(seen))
        finally:
            with self._lock:
                self._probing.discard(peer_id)

    def _reap_peers(self) -> None:
        """The only place peers are ever removed. Non-manual peers whose
        last sighting is older than HELLO_EXPIRY turn SUSPECT (verify before
        expiring). A SUSPECT peer is removed only when ALL of:
          - a verification probe issued after the suspicion failed,
          - SUSPECT_GRACE has elapsed since it turned suspect,
          - it hasn't answered any contact within CONTACT_FRESH.
        Otherwise it stays listed and gets re-probed (cooldown-limited).
        Manual peers (config.json) never expire."""
        now = time.time()
        manual = self._manual_hostports()
        removed, reprobe = [], []
        with self._lock:
            for peer_id, record in list(self._peers.items()):
                if record.get("source") == "manual":
                    continue
                if (record.get("host"), int(record.get("port", 0))) in manual:
                    continue
                suspect = record.get("_suspect_since")
                if suspect is None:
                    stale = now - record.get("last_seen", 0)
                    if stale <= HELLO_EXPIRY:
                        continue
                    record["_suspect_since"] = suspect = now
                    log.debug("peer %s stale (no sighting for %.0fs); "
                              "verifying before expiry",
                              record.get("name"), stale)
                if (now - suspect >= SUSPECT_GRACE
                        and now - record.get("_last_contact", 0) >= CONTACT_FRESH
                        and record.get("_probe_ok") is False
                        and record.get("_last_probe", 0) >= suspect):
                    removed.append(self._peers.pop(peer_id))
                else:
                    reprobe.append(peer_id)
        for peer_id in reprobe:
            self._schedule_probe(peer_id)
        for record in removed:
            log.info("peer removed (verified gone): %s (%s)",
                     record.get("name"), record.get("host"))
        if removed:
            bus.publish("peers")

    def get_peers(self) -> list:
        """Merged peer list from the unified registry (bookkeeping fields
        stripped). Suspect-but-unverified peers are still listed.

        Manual peers from config.json are probed live (short timeout) to learn
        id/name/platform; unreachable manual peers fall back to their last
        known record, or are skipped if they've never responded."""
        self._reap_peers()
        my_id = config.get_device_id()

        for entry in config.get_manual_peers():
            # Probe manual entries we have no fresh record for (and re-probe
            # all manual entries to keep their info current).
            self._probe_manual(entry["host"], entry["port"])

        with self._lock:
            peers = []
            for r in self._peers.values():
                if r["id"] == my_id:
                    continue
                pub = {k: v for k, v in r.items() if not k.startswith("_")}
                pub["addresses"] = list(r.get("addresses") or [])
                peers.append(pub)
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
                self._reap_peers()
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
            "clip": _current_clip_id(),
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
            clip=(str(data.get("clip") or "") if "clip" in data else None),
        )
        # The POST above actually reached `host` — that's answered contact
        # (record_peer keeps a previously-confirmed host, which may differ,
        # and refreshes _last_contact so this peer stays fresh even if its
        # own hellos never arrive here).
        self.confirm_contact(peer_id, host)
