"""Flask HTTP server for cross-copy.

One Flask app serves three surfaces on the same port:
  - the peer-facing transfer API   (/api/ping, /api/clipboard/*)
  - the local control API          (/api/status, /api/peers, /api/copy, ...)
  - the web UI                     (static files from crosscopy/webui/)

Trusted-LAN model (v0.1.0): no auth, binds 0.0.0.0.
"""

import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory

from . import __version__, clipboard, config

log = logging.getLogger("crosscopy.server")

WEBUI_DIR = Path(__file__).resolve().parent / "webui"

PING_TIMEOUT = 2.0        # seconds, for live peer meta/ping fetches
CONSUMED_TIMEOUT = 5.0
TRANSFER_TIMEOUT = (5.0, 300.0)  # (connect, read) for file downloads
CHUNK_SIZE = 64 * 1024


# ---------------------------------------------------------------------------
# Helpers

def _device_info() -> dict:
    return {
        "id": config.get_device_id(),
        "name": config.get_device_name(),
        "platform": config.platform_name(),
        "version": __version__,
    }


def _peer_url(peer: dict, path: str) -> str:
    return "http://%s:%d%s" % (peer["host"], int(peer["port"]), path)


def _fetch_peer_clipboard(peer: dict):
    """Live-fetch a peer's public manifest; None if empty/unreachable."""
    try:
        resp = requests.get(_peer_url(peer, "/api/clipboard/meta"),
                            timeout=PING_TIMEOUT)
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


def _attach_clipboards(peers: list) -> None:
    """Fill in peer["clipboard"] for every peer, concurrently."""
    if not peers:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
        clipboards = list(pool.map(_fetch_peer_clipboard, peers))
    for peer, manifest in zip(peers, clipboards):
        peer["clipboard"] = manifest


def _safe_rel_parts(rel_path: str):
    """Validate a peer-supplied rel_path; returns path parts or None if unsafe."""
    parts = PurePosixPath(rel_path).parts
    if not parts:
        return None
    for part in parts:
        if part in ("..", ".", "") or part.startswith("/") or "\\" in part or ":" in part:
            return None
    if PurePosixPath(rel_path).is_absolute():
        return None
    return parts


def _unique_path(path: Path) -> Path:
    """Collision-rename: 'name.ext' -> 'name (1).ext', 'name (2).ext', ..."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        candidate = path.with_name("%s (%d)%s" % (stem, n, suffix))
        if not candidate.exists():
            return candidate
        n += 1


def _error(message: str, status: int):
    return jsonify({"error": message}), status


def _notify_consumed(peer: dict, clipboard_id: str) -> None:
    """Tell the source we're done (triggers source cleanup for op=move)."""
    try:
        requests.post(_peer_url(peer, "/api/clipboard/consumed"),
                      json={"clipboard_id": clipboard_id},
                      timeout=CONSUMED_TIMEOUT)
    except Exception as exc:
        log.warning("could not notify peer of consumption: %s", exc)


# ---------------------------------------------------------------------------
# App factory

def create_app(discovery=None) -> Flask:
    """Build the Flask app. `discovery` is a crosscopy.discovery.Discovery
    (or anything with a get_peers() -> list method); may be None for tests,
    in which case the peer list is empty."""
    app = Flask("crosscopy", static_folder=None)

    def _get_peers():
        if discovery is None:
            return []
        return [dict(p) for p in discovery.get_peers()]

    # -- peer-facing API ----------------------------------------------------

    @app.get("/api/ping")
    def api_ping():
        return jsonify(_device_info())

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
            return jsonify({"deleted": True})
        return jsonify({"deleted": False})

    # -- local control API --------------------------------------------------

    @app.get("/api/status")
    def api_status():
        info = _device_info()
        info["port"] = config.get_port()
        info["clipboard"] = clipboard.load_clipboard()
        return jsonify(info)

    @app.post("/api/name")
    def api_name():
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()
        if not name:
            return _error("missing 'name'", 400)
        config.set_device_name(name)
        return jsonify(_device_info())

    @app.get("/api/peers")
    def api_peers():
        peers = _get_peers()
        if request.args.get("with_clipboard") == "1":
            _attach_clipboards(peers)
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
            return _error("peer unreachable at %s:%d (%s)" % (host, port, exc), 502)
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

        _attach_clipboards(peers)
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
            _notify_consumed(peer, manifest["clipboard_id"])
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
                url = _peer_url(peer, "/api/clipboard/file/%s/%d"
                                % (manifest["clipboard_id"], entry["index"]))
                resp = requests.get(url, stream=True, timeout=TRANSFER_TIMEOUT)
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
            return _error("transfer failed: %s" % exc, 502)

        _notify_consumed(peer, manifest["clipboard_id"])

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
        return jsonify(manifest)

    # -- web UI -------------------------------------------------------------

    @app.get("/")
    def webui_index():
        index = WEBUI_DIR / "index.html"
        if index.is_file():
            return send_from_directory(str(WEBUI_DIR), "index.html")
        return ("<html><body><h1>cross-copy</h1>"
                "<p>Web UI files not installed.</p></body></html>", 200,
                {"Content-Type": "text/html"})

    @app.get("/<path:filename>")
    def webui_static(filename):
        return send_from_directory(str(WEBUI_DIR), filename)

    return app
