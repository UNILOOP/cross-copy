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
import logging
import os
import queue
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory)

from . import __version__, clipboard, config, offers
from .events import bus
from .offers import safe_rel_parts as _safe_rel_parts
from .offers import unique_path as _unique_path

log = logging.getLogger("crosscopy.server")

WEBUI_DIR = Path(__file__).resolve().parent / "webui"
WIDGETUI_DIR = Path(__file__).resolve().parent / "widgetui"

PING_TIMEOUT = 2.0        # seconds, for live peer meta/ping fetches
CONSUMED_TIMEOUT = 5.0
OFFER_TIMEOUT = 5.0       # cross-peer offer push
TRANSFER_TIMEOUT = (5.0, 300.0)  # (connect, read) for file downloads
RETRY_CONNECT_TIMEOUT = 3.0      # connect timeout for alternate-address retries
CHUNK_SIZE = 64 * 1024
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
            )
        # Always publish: peers hello us when *their* state changes (e.g.
        # their clipboard), so local clients should refetch even when the
        # peer record itself is unchanged.
        bus.publish("peers")
        info = _device_info()
        info["port"] = config.get_port()
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
        source = files[index].get("source_path")
        if not source or not os.path.isfile(source):
            return _error("source file missing", 404)
        return send_file(source, mimetype="application/octet-stream",
                         conditional=False)

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
                    entries.append({
                        "index": int(f.get("index", i)),
                        "rel_path": str(f["rel_path"]),
                        "size": int(f.get("size", 0)),
                    })
                except (TypeError, ValueError):
                    return _error("invalid file entry", 400)
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
        source = files[index].get("source_path")
        if not source or not os.path.isfile(source):
            return _error("source file missing", 404)
        return send_file(source, mimetype="application/octet-stream",
                         conditional=False)

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
        info["clipboard"] = clipboard.load_clipboard()
        info["update"] = _update_state()
        return jsonify(info)

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

        written = []
        total_bytes = 0
        try:
            for entry in manifest.get("files", []):
                parts = _safe_rel_parts(entry.get("rel_path", ""))
                if parts is None:
                    raise RuntimeError("unsafe rel_path from peer: %r"
                                       % entry.get("rel_path"))
                target = dest.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target = _unique_path(target)
                resp = _peer_request(
                    peer, "GET", "/api/clipboard/file/%s/%d"
                    % (manifest["clipboard_id"], entry["index"]),
                    discovery=discovery, stream=True,
                    timeout=TRANSFER_TIMEOUT)
                if resp.status_code == 410:
                    raise RuntimeError("peer clipboard changed during paste")
                resp.raise_for_status()
                try:
                    with open(target, "wb") as fh:
                        for chunk in resp.iter_content(CHUNK_SIZE):
                            if chunk:
                                fh.write(chunk)
                except BaseException:
                    try:
                        target.unlink()
                    except OSError:
                        pass
                    raise
                finally:
                    resp.close()
                written.append(target)
                total_bytes += target.stat().st_size
        except Exception as exc:
            for path in written:
                try:
                    path.unlink()
                except OSError:
                    pass
            log.warning("paste from %s failed: %s", peer.get("name"), exc)
            if isinstance(exc, PeerUnreachable):
                return _error(str(exc), 502)
            return _error("transfer failed: %s" % _friendly_error(exc), 502)

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
                files.append({
                    "index": len(files),
                    "rel_path": target.name,
                    "size": target.stat().st_size,
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
        """Build an outgoing offer and push it at the chosen peer."""
        body = request.get_json(silent=True) or {}
        peer_id = str(body.get("peer_id") or "").strip()
        paths = body.get("paths")
        text = body.get("text")
        if not peer_id:
            return _error("missing 'peer_id'", 400)
        if (paths is None) == (text is None):
            return _error("give either 'paths' or 'text', not both", 400)
        if paths is not None and (not isinstance(paths, list) or not paths):
            return _error("'paths' must be a non-empty list", 400)

        matches = [p for p in _get_peers()
                   if p.get("id") == peer_id or p.get("name") == peer_id]
        if not matches:
            return _error("peer not found: %s" % peer_id, 404)
        peer = matches[0]

        try:
            offer = offers.manager.create_outgoing(peer, paths=paths, text=text)
        except ValueError as exc:
            return _error(str(exc), 400)

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
        except RuntimeError as exc:
            offers.report_result(offer, "failed")  # best effort
            offers.manager.set_incoming_status(offer_id, "failed")
            log.warning("offer %s from %s failed: %s", offer_id,
                        sender.get("name"), exc)
            return _error("transfer failed: %s" % exc, 502)

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
