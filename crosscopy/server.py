"""Flask HTTP server for cross-copy.

One Flask app serves three surfaces on the same port:
  - the peer-facing transfer API   (/api/ping, /api/hello, /api/clipboard/*,
                                    /api/offer*)
  - the local control API          (/api/status, /api/peers, /api/copy,
                                    /api/send, /api/offers,
                                    /api/events (SSE), ...)
  - the web UI + widget panel      (static files from crosscopy/webui/ at /,
                                    crosscopy/widgetui/ at /widget)

Trusted-LAN model (v0.1.0): no auth, binds 0.0.0.0.
"""

import json
import ipaddress
import logging
import os
import queue
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

import requests
from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory)

from . import __version__, clipboard, config, offers, transfer
from .events import bus
from .offers import safe_rel_parts as _safe_rel_parts
from .offers import plan_local_paths as _plan_local_paths
from .offers import unique_path as _unique_path

log = logging.getLogger("crosscopy.server")

WEBUI_DIR = Path(__file__).resolve().parent / "webui"
WIDGETUI_DIR = Path(__file__).resolve().parent / "widgetui"

PING_TIMEOUT = 2.0        # seconds, for live peer meta/ping fetches
CONSUMED_TIMEOUT = 5.0
OFFER_TIMEOUT = 5.0       # cross-peer offer push
TRANSFER_TIMEOUT = (5.0, 300.0)  # (connect, read) for file downloads
RESUME_VERIFY_TIMEOUT = (5.0, 24 * 60 * 60)  # hashing may precede response
RETRY_CONNECT_TIMEOUT = 3.0      # connect timeout for alternate-address retries
SSE_HEARTBEAT = 15.0      # seconds between ": ping" comments on /api/events


# ---------------------------------------------------------------------------
# Helpers

def _device_info() -> dict:
    return {
        "id": config.get_device_id(),
        "name": config.get_device_name(),
        "platform": config.platform_name(),
        "version": __version__,
    }


class PeerUnreachable(Exception):
    """A peer answered on none of its known candidate addresses. str() is
    the human-facing message ("could not reach <name> (tried ... on port
    N)") — safe to return in API error bodies."""

    def __init__(self, peer: dict, tried: list):
        self.peer = peer
        self.tried = list(tried)
        self.port = int(peer.get("port") or config.DEFAULT_PORT)
        super().__init__("could not reach %s (tried %s on port %d)" % (
            peer.get("name") or peer.get("host") or "peer",
            ", ".join(self.tried) or "no addresses", self.port))


def _friendly_error(exc) -> str:
    """Short human reason for a failed peer request. Raw requests exception
    text (HTTPConnectionPool(...) etc.) must never reach API clients."""
    if isinstance(exc, PeerUnreachable):
        return str(exc)
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection failed"           # includes connect timeouts
    if isinstance(exc, requests.exceptions.Timeout):
        return "peer timed out"
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None:
            return "peer answered HTTP %d" % resp.status_code
        return "peer answered with an error"
    if isinstance(exc, requests.exceptions.RequestException):
        return "request failed"
    return str(exc)


def _peer_hosts(peer: dict) -> list:
    """Candidate addresses to try: selected host first, then the rest."""
    hosts = [peer.get("host")]
    for addr in peer.get("addresses") or []:
        if addr not in hosts:
            hosts.append(addr)
    return [h for h in hosts if h]


def _confirm_peer_host(peer: dict, host: str, discovery) -> None:
    """`host` just answered a real request: use it for the rest of this
    handler and pin it on the discovery record for future calls."""
    peer["host"] = host
    if discovery is not None:
        try:
            discovery.confirm_contact(peer.get("id"), host)
        except Exception:
            pass


def _retry_timeout(timeout):
    """Same read timeout, short connect timeout, for alternate-address tries."""
    if isinstance(timeout, tuple):
        return (RETRY_CONNECT_TIMEOUT, timeout[1])
    return RETRY_CONNECT_TIMEOUT


def _peer_request(peer: dict, method: str, path: str, discovery=None,
                  timeout=PING_TIMEOUT, **kwargs):
    """requests.request() against a peer with multi-address resilience: on a
    connect error/timeout, retry the peer's other candidate addresses (short
    connect timeout); whichever address answers becomes the peer's host (see
    _confirm_peer_host). Raises PeerUnreachable when every candidate fails;
    HTTP-level errors pass through untouched."""
    port = int(peer.get("port") or config.DEFAULT_PORT)
    tried = []
    for host in _peer_hosts(peer):
        url = "http://%s:%d%s" % (host, port, path)
        try:
            resp = requests.request(
                method, url,
                timeout=timeout if not tried else _retry_timeout(timeout),
                **kwargs)
        except requests.exceptions.ConnectionError as exc:
            tried.append(host)
            log.debug("peer %s unreachable at %s:%d (%s); %s",
                      peer.get("name"), host, port, exc.__class__.__name__,
                      "trying alternates" if len(tried) == 1 else "next")
            continue
        if tried:
            log.info("peer %s reached at alternate address %s (host was %s)",
                     peer.get("name"), host, tried[0])
        _confirm_peer_host(peer, host, discovery)
        return resp
    raise PeerUnreachable(peer, tried)


def _fetch_peer_clipboard(peer: dict, discovery=None):
    """Live-fetch a peer's public manifest; None if empty/unreachable."""
    try:
        resp = _peer_request(peer, "GET", "/api/clipboard/meta",
                             discovery=discovery, timeout=PING_TIMEOUT)
        if resp.status_code == 200:
            manifest = resp.json()
            if isinstance(manifest, dict):
                if clipboard.manifest_kind(manifest) == "text":
                    if manifest.get("text"):
                        return manifest
                elif manifest.get("files"):
                    return manifest
    except Exception:
        pass
    return None


def _attach_clipboards(peers: list, discovery=None) -> None:
    """Fill in peer["clipboard"] for every peer, concurrently."""
    if not peers:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
        clipboards = list(pool.map(
            lambda p: _fetch_peer_clipboard(p, discovery), peers))
    for peer, manifest in zip(peers, clipboards):
        peer["clipboard"] = manifest


def _error(message: str, status: int):
    return jsonify({"error": message}), status


def _notify_consumed(peer: dict, clipboard_id: str, discovery=None) -> None:
    """Tell the source we're done (triggers source cleanup for op=move)."""
    try:
        _peer_request(peer, "POST", "/api/clipboard/consumed",
                      discovery=discovery,
                      json={"clipboard_id": clipboard_id},
                      timeout=CONSUMED_TIMEOUT)
    except Exception as exc:
        log.warning("could not notify peer of consumption: %s",
                    _friendly_error(exc))


# ---------------------------------------------------------------------------
# App factory

def create_app(discovery=None, updater=None) -> Flask:
    """Build the Flask app. `discovery` is a crosscopy.discovery.Discovery
    (or anything with a get_peers() -> list method); may be None for tests,
    in which case the peer list is empty. `updater` is a
    crosscopy.updater.Updater (or anything with a state() -> dict method);
    may be None, in which case /api/status reports a never-checked state."""
    app = Flask("crosscopy", static_folder=None)

    def _source_entry_available(entry, verify_content=True):
        source = entry.get("source_path")
        if not source or not os.path.isfile(source):
            return False, "source file is missing"
        try:
            stat_result = os.stat(source)
            actual_mtime = getattr(
                stat_result, "st_mtime_ns",
                int(stat_result.st_mtime * 1000000000))
            if stat_result.st_size != int(entry.get("size", -1)):
                return False, "source file changed since sharing"
            expected_mtime = entry.get("mtime_ns")
            if (expected_mtime is not None
                    and actual_mtime != int(expected_mtime)):
                return False, "source file changed since sharing"
            if verify_content:
                snapshot = transfer.source_snapshot(source, attempts=1)
                expected_checksum = transfer.transfer_checksum(entry)
                if (not expected_checksum
                        or snapshot["sha256"] != expected_checksum):
                    return False, "source file changed since sharing"
        except (OSError, TypeError, ValueError):
            return False, "source file is unavailable"
        return True, None

    def _serve_source_file(entry):
        """Serve a stable manifest entry with HTTP Range and integrity data."""
        # Manifest creation already hashed the source and the receiver always
        # verifies the advertised digest. Keep serving responsive for very
        # large files; explicit Resume performs a deep check before this GET.
        available, reason = _source_entry_available(
            entry, verify_content=False)
        if not available:
            status = 404 if "missing" in reason or "unavailable" in reason else 409
            return _error(reason, status)
        source = entry["source_path"]
        checksum = str(entry.get("sha256") or "").lower()
        response = send_file(
            source, mimetype="application/octet-stream", conditional=True,
            etag=checksum if transfer.valid_sha256(checksum) else True)
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["X-CrossCopy-Size"] = str(entry.get("size", 0))
        if checksum:
            response.headers["X-CrossCopy-SHA256"] = checksum
        return response

    def _resume_metadata_for_clipboard(peer, manifest):
        return {
            "kind": "clipboard",
            "clipboard_id": manifest.get("clipboard_id"),
            "op": manifest.get("op", "copy"),
            "source": {
                "id": peer.get("id"),
                "name": peer.get("name"),
                "host": peer.get("host"),
                "port": int(peer.get("port") or config.DEFAULT_PORT),
            },
            "files": [dict(entry) for entry in manifest.get("files") or []],
            "total_size": int(manifest.get("total_size") or 0),
        }

    def _get_peers():
        if discovery is None:
            return []
        return [dict(p) for p in discovery.get_peers()]

    def _clipboard_changed():
        """Local clipboard changed (copy/upload/clear/consumed): tell local
        SSE clients and kick off a hello round so remote UIs react too."""
        bus.publish("clipboard")
        if discovery is not None:
            try:
                discovery.hello_now()
            except Exception:
                pass

    def _check_resume_availability(session, verify_content=True,
                                   timeout=PING_TIMEOUT):
        if session.get("broken"):
            return False, "Partial transfer state is unsafe or damaged", None
        metadata = session.get("metadata") or {}
        kind = metadata.get("kind")
        files = metadata.get("files") or []
        body = {
            "kind": kind,
            "clipboard_id": metadata.get("clipboard_id"),
            "offer_id": metadata.get("offer_id"),
            "files": files,
            "verify_content": bool(verify_content),
        }
        if kind == "clipboard":
            source = metadata.get("source") or {}
            matches = [peer for peer in _get_peers()
                       if peer.get("id") == source.get("id")]
            if not matches:
                return False, "Source device is offline or no longer discovered", None
            peer = matches[0]
            try:
                response = _peer_request(
                    peer, "POST", "/api/transfer/available",
                    discovery=discovery, json=body, timeout=timeout)
            except Exception:
                return False, "Source device is currently unreachable", None
        elif kind == "offer":
            source = metadata.get("source") or {}
            host = source.get("host")
            port = int(source.get("port") or config.DEFAULT_PORT)
            if not host:
                return False, "Sender address is unavailable", None
            try:
                response = requests.post(
                    "http://%s:%d/api/transfer/available" % (host, port),
                    json=body, timeout=timeout)
            except requests.RequestException:
                return False, "Sender is currently unreachable", None
            peer = None
        else:
            return False, "Unknown partial transfer type", None
        try:
            result = response.json()
        except ValueError:
            result = {}
        if not response.ok or not result.get("available"):
            reason = result.get("reason") or "Source is no longer sharing these files"
            return False, reason, None
        return True, None, peer

    def _public_resume_session(session):
        # Menus only need a quick proof that the exact manifest is active.
        # The Resume action performs the potentially expensive full checksum
        # validation before requesting any additional bytes.
        available, reason, _peer = _check_resume_availability(
            session, verify_content=False)
        public = {key: value for key, value in session.items()
                  if key != "metadata"}
        metadata = session.get("metadata") or {}
        public["available"] = available
        public["unavailable_reason"] = reason
        public["clipboard_id"] = metadata.get("clipboard_id")
        public["offer_id"] = metadata.get("offer_id")
        return public

    def _unavailable_resume_session(session, reason):
        public = {key: value for key, value in session.items()
                  if key != "metadata"}
        metadata = session.get("metadata") or {}
        public["available"] = False
        public["unavailable_reason"] = reason
        public["clipboard_id"] = metadata.get("clipboard_id")
        public["offer_id"] = metadata.get("offer_id")
        return public

    def _local_resume_api_error():
        """Recovery controls may expose paths or delete local partial data."""
        try:
            address = str(request.remote_addr or "").split("%", 1)[0]
            if ipaddress.ip_address(address).is_loopback:
                return None
        except ValueError:
            pass
        return _error("partial transfer controls are local-only", 403)

    def _update_state() -> dict:
        if updater is not None:
            return updater.state()
        return {
            "current": __version__,
            "latest": None,
            "available": False,
            "last_checked": None,
            "auto_update": config.get_auto_update(),
        }

    # -- peer-facing API ----------------------------------------------------

    @app.get("/api/ping")
    def api_ping():
        return jsonify(_device_info())

    @app.post("/api/hello")
    def api_hello():
        """Reciprocal discovery: a peer introduces itself; we record it
        (host = the request's source address) and answer with our own info."""
        body = request.get_json(silent=True) or {}
        peer_id = str(body.get("id") or "").strip()
        if not peer_id:
            return _error("missing 'id'", 400)
        try:
            port = int(body.get("port") or config.DEFAULT_PORT)
        except (TypeError, ValueError):
            return _error("invalid 'port'", 400)
        if discovery is not None:
            discovery.record_peer(
                peer_id,
                name=body.get("name") or peer_id,
                host=request.remote_addr,
                port=port,
                platform=body.get("platform", "unknown"),
                version=body.get("version", "unknown"),
                source="hello",
                # "clip" = the sender's clipboard_id ("" when empty);
                # record_peer publishes a "peers" event only when it (or
                # membership/host/name/...) actually changed — periodic
                # keep-alive hellos no longer spam SSE clients.
                clip=(str(body.get("clip") or "") if "clip" in body else None),
            )
        if "clip" not in body:
            # Pre-0.4.2 senders don't say what changed; keep the legacy
            # always-refetch behavior so their clipboard changes still
            # propagate to our UIs.
            log.debug("peers event published for legacy hello from %s (%s)",
                      body.get("name") or peer_id, request.remote_addr)
            bus.publish("peers")
        info = _device_info()
        info["port"] = config.get_port()
        # Mirror our clipboard_id back so the *sender* can also detect our
        # clipboard changes from its own outbound hellos.
        manifest = clipboard.load_clipboard()
        info["clip"] = str((manifest or {}).get("clipboard_id") or "")
        return jsonify(info)

    @app.get("/api/clipboard/meta")
    def api_clipboard_meta():
        manifest = clipboard.load_clipboard()
        if not manifest:
            return _error("clipboard empty", 404)
        return jsonify(clipboard.public_manifest(manifest))

    @app.get("/api/clipboard/file/<clipboard_id>/<int:index>")
    def api_clipboard_file(clipboard_id, index):
        manifest = clipboard.load_clipboard()
        if not manifest or manifest.get("clipboard_id") != clipboard_id:
            return _error("clipboard no longer current", 410)
        files = manifest.get("files", [])
        if index < 0 or index >= len(files):
            return _error("bad file index", 404)
        return _serve_source_file(files[index])

    @app.post("/api/clipboard/consumed")
    def api_clipboard_consumed():
        body = request.get_json(silent=True) or {}
        clipboard_id = body.get("clipboard_id")
        manifest = clipboard.load_clipboard()
        if (manifest and clipboard_id
                and manifest.get("clipboard_id") == clipboard_id
                and manifest.get("op") == "move"):
            clipboard.delete_sources(manifest)
            clipboard.clear_clipboard()
            log.info("clipboard %s consumed by peer; sources deleted", clipboard_id)
            _clipboard_changed()
            return jsonify({"deleted": True})
        return jsonify({"deleted": False})

    # -- peer-facing offers API (v0.4) ---------------------------------------

    @app.post("/api/offer")
    def api_offer():
        """A peer pushes a targeted offer at us; we store it as pending,
        fire the SSE event + desktop notification, and wait for the local
        user to accept/decline."""
        body = request.get_json(silent=True) or {}
        offer_id = str(body.get("offer_id") or "").strip()
        sender = body.get("from")
        kind = body.get("kind")
        if not offer_id or not isinstance(sender, dict) or not sender.get("id"):
            return _error("invalid offer", 400)
        try:
            sender_port = int(body.get("sender_port") or config.DEFAULT_PORT)
        except (TypeError, ValueError):
            return _error("invalid 'sender_port'", 400)
        try:
            created_at = float(body.get("created_at") or time.time())
        except (TypeError, ValueError):
            created_at = time.time()

        record = {
            "offer_id": offer_id,
            "from": {
                "id": sender.get("id"),
                "name": sender.get("name") or sender.get("id"),
                "platform": sender.get("platform", "unknown"),
            },
            "sender_port": sender_port,
            "kind": kind,
            "created_at": created_at,
        }
        if kind == "text":
            text = body.get("text")
            if not isinstance(text, str) or not text:
                return _error("text offer without text", 400)
            if len(text.encode("utf-8")) > clipboard.MAX_TEXT_BYTES:
                return _error("text too large", 400)
            record["text"] = text
            record["total_size"] = len(text.encode("utf-8"))
        elif kind == "files":
            files = body.get("files")
            if not isinstance(files, list) or not files:
                return _error("files offer without files", 400)
            entries = []
            for i, f in enumerate(files):
                if not isinstance(f, dict) or not f.get("rel_path"):
                    return _error("invalid file entry", 400)
                try:
                    entry = {
                        "index": int(f.get("index", i)),
                        "rel_path": str(f["rel_path"]),
                        "size": int(f.get("size", 0)),
                    }
                except (TypeError, ValueError):
                    return _error("invalid file entry", 400)
                checksum = str(f.get("sha256") or "").lower()
                if not transfer.valid_sha256(checksum):
                    return _error(
                        "file offer requires a valid SHA-256 checksum; "
                        "update Cross Copy on the sender", 400)
                entry["sha256"] = checksum
                entries.append(entry)
            record["files"] = entries
            try:
                record["total_size"] = int(
                    body.get("total_size")
                    or sum(e["size"] for e in entries))
            except (TypeError, ValueError):
                record["total_size"] = sum(e["size"] for e in entries)
        else:
            return _error("invalid 'kind'", 400)

        offers.manager.record_incoming(record, request.remote_addr)
        log.info("incoming offer %s from %s: %s", offer_id,
                 record["from"]["name"], offers.summarize(record))
        return jsonify({"status": "pending"})

    @app.get("/api/offer/<offer_id>/file/<int:index>")
    def api_offer_file(offer_id, index):
        """Sender side: stream one offered file to the receiver while the
        offer is still pending/accepted; 410 otherwise."""
        offer = offers.manager.get_outgoing(offer_id)
        if (offer is None or offer.get("kind") != "files"
                or offer.get("status") not in offers.ACTIVE_STATES):
            return _error("offer no longer available", 410)
        files = offer.get("files", [])
        if index < 0 or index >= len(files):
            return _error("bad file index", 404)
        return _serve_source_file(files[index])

    @app.post("/api/transfer/available")
    def api_transfer_available():
        """Confirm an exact resumable manifest is still actively shared."""
        body = request.get_json(silent=True) or {}
        kind = body.get("kind")
        if kind == "clipboard":
            record = clipboard.load_clipboard()
            if (not record or record.get("clipboard_id")
                    != body.get("clipboard_id")):
                return jsonify({"available": False,
                                "reason": "Clipboard is no longer being shared"})
        elif kind == "offer":
            record = offers.manager.get_outgoing(str(body.get("offer_id") or ""))
            if (not record or record.get("status") not in offers.ACTIVE_STATES):
                return jsonify({"available": False,
                                "reason": "Offer is no longer being shared"})
        else:
            return _error("invalid transfer kind", 400)
        expected = body.get("files")
        actual = record.get("files") or []
        if not isinstance(expected, list) or not transfer.same_manifest(expected, actual):
            return jsonify({"available": False,
                            "reason": "Shared files changed since the interruption"})
        verify_content = body.get("verify_content", True) is not False
        for entry in actual:
            available, reason = _source_entry_available(
                entry, verify_content=verify_content)
            if not available:
                return jsonify({"available": False, "reason": reason})
        return jsonify({"available": True})

    @app.post("/api/offer/<offer_id>/result")
    def api_offer_result(offer_id):
        """Sender side: the receiver reports an outcome for our offer."""
        body = request.get_json(silent=True) or {}
        result = body.get("result")
        if result not in offers.RESULT_STATES:
            return _error("invalid 'result'", 400)
        offer = offers.manager.set_outgoing_status(offer_id, result)
        if offer is None:
            return _error("unknown offer", 404)
        return jsonify({"status": offer.get("status")})

    # -- local control API --------------------------------------------------

    @app.get("/api/status")
    def api_status():
        info = _device_info()
        info["port"] = config.get_port()
        # The serving process's real pid — daemon.json can go stale (orphaned
        # daemons survive it); this is ground truth for stop/restart tooling.
        info["pid"] = os.getpid()
        info["clipboard"] = clipboard.load_clipboard()
        info["partial_transfers"] = len(transfer.list_resume_sessions(
            config.resume_registry_dir()))
        info["update"] = _update_state()
        return jsonify(info)

    @app.get("/api/resumes")
    def api_resumes():
        local_error = _local_resume_api_error()
        if local_error:
            return local_error
        sessions = transfer.list_resume_sessions(config.resume_registry_dir())
        if not sessions:
            return jsonify({"resumes": []})
        executor = ThreadPoolExecutor(max_workers=min(8, len(sessions)))
        futures = [executor.submit(_public_resume_session, session)
                   for session in sessions]
        done, _pending = wait(futures, timeout=PING_TIMEOUT + 0.25)
        results = []
        for session, future in zip(sessions, futures):
            if future in done:
                try:
                    results.append(future.result())
                    continue
                except Exception:
                    pass
            future.cancel()
            results.append(_unavailable_resume_session(
                session, "Source availability check timed out"))
        executor.shutdown(wait=False, cancel_futures=True)
        return jsonify({"resumes": results})

    @app.post("/api/resumes/<session_id>/remove")
    def api_resume_remove(session_id):
        local_error = _local_resume_api_error()
        if local_error:
            return local_error
        try:
            removed = transfer.discard_resume_session(
                config.resume_registry_dir(), session_id)
        except transfer.ResumeActiveError as exc:
            return _error(str(exc), 409)
        if not removed:
            return _error("unknown partial transfer", 404)
        bus.publish("resumes")
        return jsonify({"removed": True})

    @app.post("/api/resumes/<session_id>/resume")
    def api_resume_transfer(session_id):
        local_error = _local_resume_api_error()
        if local_error:
            return local_error
        session = transfer.get_resume_session(
            config.resume_registry_dir(), session_id)
        if not session:
            return _error("unknown partial transfer", 404)
        available, reason, peer = _check_resume_availability(
            session, verify_content=True, timeout=RESUME_VERIFY_TIMEOUT)
        if not available:
            return _error(reason or "source is no longer sharing these files", 409)
        metadata = session.get("metadata") or {}
        entries = metadata.get("files") or []
        dest = Path(session["dest"])
        source_parts = []
        for entry in entries:
            parts = _safe_rel_parts(entry.get("rel_path", ""))
            if parts is None:
                return _error("partial transfer has an unsafe path", 409)
            source_parts.append(parts)
        planned = _plan_local_paths(source_parts, dest)
        try:
            if metadata.get("kind") == "clipboard":
                clipboard_id = metadata["clipboard_id"]

                def fetch(entry, offset):
                    headers = {"Range": "bytes=%d-" % offset} if offset else {}
                    return _peer_request(
                        peer, "GET", "/api/clipboard/file/%s/%d"
                        % (clipboard_id, entry["index"]), discovery=discovery,
                        stream=True, timeout=TRANSFER_TIMEOUT, headers=headers)

                transfer_id = "clipboard:%s:%s" % (
                    (metadata.get("source") or {}).get("id"), clipboard_id)
                written, total_bytes = transfer.download_files(
                    entries, planned, dest, transfer_id, fetch,
                    registry_dir=config.resume_registry_dir(), metadata=metadata)
                _notify_consumed(peer, clipboard_id, discovery)
                op = metadata.get("op", "copy")
            else:
                source = metadata.get("source") or {}
                offer = {
                    "offer_id": metadata.get("offer_id"),
                    "from": {"id": source.get("id"), "name": source.get("name")},
                    "sender_host": source.get("host"),
                    "sender_port": source.get("port"),
                    "kind": "files",
                    "files": entries,
                    "total_size": metadata.get("total_size"),
                }
                if not offers.report_result(offer, "accepted"):
                    return _error("sender no longer has this offer", 409)
                written, total_bytes = offers.pull_files(offer, dest)
                offers.report_result(offer, "completed")
                incoming = offers.manager.get_incoming(offer["offer_id"])
                if incoming:
                    offers.manager.set_incoming_status(
                        offer["offer_id"], "completed")
                op = "copy"
        except Exception as exc:
            bus.publish("resumes")
            return _error("resume interrupted: %s" % _friendly_error(exc), 502)
        bus.publish("resumes")
        return jsonify({
            "kind": "files",
            "files_written": [str(path) for path in written],
            "total_bytes": total_bytes,
            "dest": str(dest),
            "op": op,
        })

    @app.get("/api/events")
    def api_events():
        """Server-sent events: payload-free {"type": ...} JSON lines with a
        ": ping" heartbeat comment every 15 s. Clients refetch /api/status or
        /api/peers on receipt.

        `?client=widget` tags the subscription: while a widget client is
        connected, the daemon suppresses OS notifications (the widget shows
        its own popup cards with accept/decline actions instead)."""
        tag = request.args.get("client") or None

        def stream():
            q = bus.subscribe(tag=tag)
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event = q.get(timeout=SSE_HEARTBEAT)
                        yield "data: %s\n\n" % json.dumps(event)
                    except queue.Empty:
                        yield ": ping\n\n"
            finally:  # client disconnected (or server shutdown)
                bus.unsubscribe(q)

        return Response(stream(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # defeat proxy buffering
        })

    @app.post("/api/name")
    def api_name():
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()
        if not name:
            return _error("missing 'name'", 400)
        config.set_device_name(name)
        bus.publish("peers")
        if discovery is not None:
            try:
                discovery.hello_now()  # propagate the new name to peers
            except Exception:
                pass
        return jsonify(_device_info())

    @app.get("/api/peers")
    def api_peers():
        peers = _get_peers()
        if request.args.get("with_clipboard") == "1":
            _attach_clipboards(peers, discovery)
        return jsonify({"peers": peers})

    @app.post("/api/peers/add")
    def api_peers_add():
        body = request.get_json(silent=True) or {}
        host = (body.get("host") or "").strip()
        if not host:
            return _error("missing 'host'", 400)
        try:
            port = int(body.get("port") or config.DEFAULT_PORT)
        except (TypeError, ValueError):
            return _error("invalid 'port'", 400)
        try:
            resp = requests.get("http://%s:%d/api/ping" % (host, port),
                                timeout=PING_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return _error("peer unreachable at %s:%d (%s)"
                          % (host, port, _friendly_error(exc)), 502)
        config.add_manual_peer(host, port)
        return jsonify({
            "id": data.get("id"),
            "name": data.get("name"),
            "host": host,
            "port": port,
            "platform": data.get("platform"),
            "version": data.get("version"),
            "last_seen": time.time(),
            "source": "manual",
        })

    @app.post("/api/copy")
    def api_copy():
        body = request.get_json(silent=True) or {}
        paths = body.get("paths")
        text = body.get("text")
        op = body.get("op", "copy")
        if paths is not None and text is not None:
            return _error("give either 'paths' or 'text', not both", 400)
        if text is not None:
            try:
                manifest = clipboard.build_text_manifest(text, op=op)
            except ValueError as exc:
                return _error(str(exc), 400)
        else:
            if not paths or not isinstance(paths, list):
                return _error("missing 'paths' or 'text'", 400)
            try:
                manifest = clipboard.build_manifest(paths, op=op)
            except ValueError as exc:
                return _error(str(exc), 400)
        clipboard.set_clipboard(manifest)
        log.info("clipboard set: %s (%s)", clipboard.summarize(manifest), op)
        _clipboard_changed()
        return jsonify(manifest)

    @app.post("/api/paste")
    def api_paste():
        body = request.get_json(silent=True) or {}
        dest_raw = body.get("dest")  # validated below; ignored for text pastes
        peer_id = body.get("peer_id")
        want_clipboard_id = body.get("clipboard_id")

        peers = _get_peers()
        if peer_id:
            peers = [p for p in peers
                     if p.get("id") == peer_id or p.get("name") == peer_id]
            if not peers:
                return _error("peer not found: %s" % peer_id, 404)

        _attach_clipboards(peers, discovery)
        candidates = [p for p in peers if p.get("clipboard")]
        if want_clipboard_id:
            candidates = [p for p in candidates
                          if p["clipboard"].get("clipboard_id") == want_clipboard_id]
        if not candidates:
            return _error("no peer has a clipboard", 404)
        # Newest non-empty clipboard wins.
        peer = max(candidates,
                   key=lambda p: p["clipboard"].get("created_at", 0.0))
        manifest = peer["clipboard"]

        # Text clipboards: nothing to download, dest is ignored.
        if clipboard.manifest_kind(manifest) == "text":
            _notify_consumed(peer, manifest["clipboard_id"], discovery)
            log.info("pasted text (%s) from %s",
                     config.format_size(manifest.get("total_size", 0)),
                     peer.get("name"))
            return jsonify({
                "from": {"id": peer.get("id"), "name": peer.get("name")},
                "kind": "text",
                "text": manifest.get("text", ""),
                "op": manifest.get("op", "copy"),
            })

        if not dest_raw:
            return _error("missing 'dest'", 400)
        dest = Path(str(dest_raw)).expanduser()
        if not dest.is_absolute():
            return _error("'dest' must be an absolute path", 400)
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _error("cannot create dest dir: %s" % exc, 400)

        try:
            entries = manifest.get("files", [])
            source_parts = []
            for entry in entries:
                parts = _safe_rel_parts(entry.get("rel_path", ""))
                if parts is None:
                    raise RuntimeError("unsafe rel_path from peer: %r"
                                       % entry.get("rel_path"))
                source_parts.append(parts)
            planned = _plan_local_paths(source_parts, dest)

            def fetch(entry, offset):
                headers = {"Range": "bytes=%d-" % offset} if offset else {}
                return _peer_request(
                    peer, "GET", "/api/clipboard/file/%s/%d"
                    % (manifest["clipboard_id"], entry["index"]),
                    discovery=discovery, stream=True,
                    timeout=TRANSFER_TIMEOUT, headers=headers)

            transfer_id = "clipboard:%s:%s" % (
                peer.get("id") or peer.get("host"), manifest["clipboard_id"])
            written, total_bytes = transfer.download_files(
                entries, planned, dest, transfer_id, fetch,
                registry_dir=config.resume_registry_dir(),
                metadata=_resume_metadata_for_clipboard(peer, manifest))
        except Exception as exc:
            bus.publish("resumes")
            log.warning("paste from %s failed: %s", peer.get("name"), exc)
            if isinstance(exc, PeerUnreachable):
                return _error(str(exc), 502)
            return _error("transfer interrupted: %s. Retry paste to resume."
                          % _friendly_error(exc), 502)

        _notify_consumed(peer, manifest["clipboard_id"], discovery)

        log.info("pasted %d files (%s) from %s into %s",
                 len(written), config.format_size(total_bytes),
                 peer.get("name"), dest)
        return jsonify({
            "from": {"id": peer.get("id"), "name": peer.get("name")},
            "kind": "files",
            "files_written": [str(p) for p in written],
            "total_bytes": total_bytes,
            "op": manifest.get("op", "copy"),
        })

    @app.post("/api/clipboard/clear")
    def api_clipboard_clear():
        clipboard.clear_clipboard()
        _clipboard_changed()
        return jsonify({"cleared": True})

    @app.post("/api/upload")
    def api_upload():
        uploads = request.files.getlist("files")
        uploads = [u for u in uploads if u and u.filename]
        if not uploads:
            return _error("no files uploaded (use multipart field 'files')", 400)

        clipboard_id = str(uuid.uuid4())
        stage = config.staging_dir() / clipboard_id
        stage.mkdir(parents=True, exist_ok=True)

        files = []
        try:
            for upload in uploads:
                name = os.path.basename(upload.filename.replace("\\", "/")) or "upload"
                target = _unique_path(stage / name)
                upload.save(str(target))
                snapshot = transfer.source_snapshot(target)
                files.append({
                    "index": len(files),
                    "rel_path": target.name,
                    "size": snapshot["size"],
                    "sha256": snapshot["sha256"],
                    "mtime_ns": snapshot["mtime_ns"],
                    "source_path": str(target),
                })
        except Exception as exc:
            import shutil
            shutil.rmtree(stage, ignore_errors=True)
            return _error("upload failed: %s" % exc, 500)

        manifest = {
            "clipboard_id": clipboard_id,
            "kind": "files",
            "op": "copy",
            "created_at": time.time(),
            "host_id": config.get_device_id(),
            "host_name": config.get_device_name(),
            "total_size": sum(f["size"] for f in files),
            "files": files,
        }
        clipboard.set_clipboard(manifest)  # also clears older staging dirs
        log.info("clipboard set from upload: %s", clipboard.summarize(manifest))
        _clipboard_changed()
        return jsonify(manifest)

    # -- local offers API (v0.4) ----------------------------------------------

    @app.post("/api/send")
    def api_send():
        """Build an outgoing offer and push it at the chosen peer.

        JSON accepts daemon-readable paths or text. Multipart accepts actual
        file bytes, which is required for browsers and for macOS native
        pickers whose protected-folder access is scoped to the widget process.
        """
        uploads = [u for u in request.files.getlist("files")
                   if u and u.filename]
        if request.files or request.form:
            peer_id = str(request.form.get("peer_id") or "").strip()
            paths = None
            text = None
        else:
            body = request.get_json(silent=True) or {}
            peer_id = str(body.get("peer_id") or "").strip()
            paths = body.get("paths")
            text = body.get("text")
        if not peer_id:
            return _error("missing 'peer_id'", 400)
        if uploads and (paths is not None or text is not None):
            return _error("give either uploaded files, 'paths', or 'text'", 400)
        if not uploads and (paths is None) == (text is None):
            return _error("give either 'paths' or 'text', not both", 400)
        if paths is not None and (not isinstance(paths, list) or not paths):
            return _error("'paths' must be a non-empty list", 400)

        matches = [p for p in _get_peers()
                   if p.get("id") == peer_id or p.get("name") == peer_id]
        if not matches:
            return _error("peer not found: %s" % peer_id, 404)
        peer = matches[0]

        stage = None
        if uploads:
            stage = config.offer_staging_dir() / str(uuid.uuid4())
            stage.mkdir(parents=True, exist_ok=True)
            paths = []
            try:
                for upload in uploads:
                    name = os.path.basename(
                        upload.filename.replace("\\", "/")) or "upload"
                    target = _unique_path(stage / name)
                    upload.save(str(target))
                    paths.append(str(target))
            except Exception as exc:
                shutil.rmtree(stage, ignore_errors=True)
                return _error("upload failed: %s" % _friendly_error(exc), 500)

        try:
            offer = offers.manager.create_outgoing(
                peer, paths=paths, text=text, staging_dir=stage)
        except ValueError as exc:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
            return _error(str(exc), 400)
        except PermissionError as exc:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
            log.warning("cannot read file selected for %s: %s",
                        peer.get("name"), exc)
            return _error(
                "Cross Copy cannot read a selected file. Choose it through "
                "the widget or grant Cross Copy access to that folder.", 403)
        except OSError as exc:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
            log.warning("cannot prepare offer for %s: %s",
                        peer.get("name"), exc)
            return _error("could not read a selected file: %s"
                          % _friendly_error(exc), 400)

        try:
            resp = _peer_request(peer, "POST", "/api/offer",
                                 discovery=discovery,
                                 json=offers.wire_offer(offer),
                                 timeout=OFFER_TIMEOUT)
            resp.raise_for_status()
        except PeerUnreachable as exc:
            offers.manager.discard_outgoing(offer["offer_id"])
            return _error(str(exc), 502)
        except Exception as exc:
            offers.manager.discard_outgoing(offer["offer_id"])
            log.warning("offer to %s failed: %s", peer.get("name"), exc)
            return _error("peer %s rejected the offer (%s)"
                          % (peer.get("name"), _friendly_error(exc)), 502)

        log.info("offer %s sent to %s: %s", offer["offer_id"],
                 peer.get("name"), offers.summarize(offer))
        return jsonify(offers.public_offer(offer))

    @app.get("/api/send/<offer_id>")
    def api_send_status(offer_id):
        offer = offers.manager.get_outgoing(offer_id)
        if offer is None:
            return _error("unknown offer", 404)
        return jsonify(offers.public_offer(offer))

    @app.get("/api/offers")
    def api_offers():
        return jsonify({"offers": [offers.public_offer(o)
                                   for o in offers.manager.list_incoming_pending()]})

    @app.post("/api/offers/<offer_id>/accept")
    def api_offers_accept(offer_id):
        body = request.get_json(silent=True) or {}
        offer = offers.manager.get_incoming(offer_id)
        if offer is None:
            return _error("unknown offer", 404)
        if offer.get("status") != "pending":
            return _error("offer is %s" % offer.get("status"), 409)
        sender = offer.get("from", {})

        # Text offers: the content already arrived with the offer.
        if offer.get("kind") == "text":
            offers.manager.set_incoming_status(offer_id, "accepted")
            offers.report_result(offer, "accepted")   # best effort
            offers.report_result(offer, "completed")  # best effort
            offers.manager.set_incoming_status(offer_id, "completed")
            log.info("accepted text offer %s from %s", offer_id,
                     sender.get("name"))
            return jsonify({
                "from": {"id": sender.get("id"), "name": sender.get("name")},
                "kind": "text",
                "text": offer.get("text", ""),
            })

        # Files: pull them from the sender (the client waits on this).
        dest_raw = body.get("dest")
        if dest_raw:
            dest = Path(str(dest_raw)).expanduser()
            if not dest.is_absolute():
                return _error("'dest' must be an absolute path", 400)
        else:
            dest = config.get_receive_dir()
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _error("cannot create dest dir: %s" % exc, 400)

        if not offers.report_result(offer, "accepted"):
            offers.manager.set_incoming_status(offer_id, "failed")
            return _error("sender unreachable or offer no longer available", 502)
        offers.manager.set_incoming_status(offer_id, "accepted")

        try:
            written, total_bytes = offers.pull_files(offer, dest)
        except transfer.TransferError as exc:
            # Keep both sides active: the next Accept resumes the saved
            # partial bytes instead of forcing the sender to create a new
            # offer or retransmitting completed files.
            offers.manager.set_incoming_status(offer_id, "pending")
            bus.publish("resumes")
            log.warning("offer %s from %s interrupted: %s", offer_id,
                        sender.get("name"), exc)
            return _error("transfer interrupted: %s. Accept again to resume."
                          % exc, 502)

        offers.report_result(offer, "completed")  # best effort
        offers.manager.set_incoming_status(offer_id, "completed")
        offers.notify_files_saved(offer, dest, len(written), total_bytes)
        log.info("accepted offer %s: %d files (%s) from %s into %s",
                 offer_id, len(written), config.format_size(total_bytes),
                 sender.get("name"), dest)
        return jsonify({
            "from": {"id": sender.get("id"), "name": sender.get("name")},
            "kind": "files",
            "files_written": [str(p) for p in written],
            "total_bytes": total_bytes,
            "dest": str(dest),
        })

    @app.post("/api/offers/<offer_id>/decline")
    def api_offers_decline(offer_id):
        offer = offers.manager.get_incoming(offer_id)
        if offer is None:
            return _error("unknown offer", 404)
        offers.report_result(offer, "declined")  # best effort
        offers.manager.set_incoming_status(offer_id, "declined", remove=True)
        log.info("declined offer %s from %s", offer_id,
                 offer.get("from", {}).get("name"))
        return jsonify({"declined": True})

    # -- test hooks (CROSSCOPY_TEST_HOOKS=1 only) ------------------------------

    if os.environ.get("CROSSCOPY_TEST_HOOKS") == "1":
        @app.post("/api/_test/peer")
        def api_test_peer():
            """TEST ONLY: inject a raw peer record (id/name/host/port/
            addresses/...) straight into the discovery registry, bypassing
            selection and probing. Lets the network test harness simulate a
            peer whose mDNS sighting picked an unroutable address. Never
            registered unless the daemon runs with CROSSCOPY_TEST_HOOKS=1."""
            body = request.get_json(silent=True) or {}
            if discovery is None or not body.get("id"):
                return _error("no discovery or missing 'id'", 400)
            body.setdefault("last_seen", time.time())
            discovery.inject_record_for_tests(body)
            return jsonify(body)

    # -- web UI -------------------------------------------------------------

    @app.get("/")
    def webui_index():
        index = WEBUI_DIR / "index.html"
        if index.is_file():
            return send_from_directory(str(WEBUI_DIR), "index.html")
        return ("<html><body><h1>cross-copy</h1>"
                "<p>Web UI files not installed.</p></body></html>", 200,
                {"Content-Type": "text/html"})

    @app.get("/widget")
    def widget_index():
        """Compact tray-widget panel (static files from crosscopy/widgetui/)."""
        page = WIDGETUI_DIR / "widget.html"
        if page.is_file():
            return send_from_directory(str(WIDGETUI_DIR), "widget.html")
        return ("<html><body><h1>cross-copy widget</h1>"
                "<p>Widget UI files not installed.</p></body></html>", 200,
                {"Content-Type": "text/html"})

    @app.get("/widget/<path:filename>")
    def widget_static(filename):
        return send_from_directory(str(WIDGETUI_DIR), filename)

    @app.get("/<path:filename>")
    def webui_static(filename):
        return send_from_directory(str(WEBUI_DIR), filename)

    return app
