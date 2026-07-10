"""Offer state machine + transfer logic for cross-copy (v0.4).

AirDrop-style targeted send: the sender builds an "offer" (files or text),
pushes it to one chosen peer, and nothing transfers until the receiver
accepts. All offer state is in-memory in the daemon (lost on restart).

Offer object (the wire form has no "status" and no local-only keys):

{
  "offer_id": "uuid4",
  "from": {"id", "name", "platform"},
  "sender_port": 7373,
  "kind": "files" | "text",
  "files": [{"index", "rel_path", "size", "sha256"}], // kind == "files"
  "text": "...",                              // kind == "text" (<= 1 MB)
  "total_size": int,
  "created_at": epoch_float,
  "status": "pending" | "accepted" | "declined" | "completed" | "failed"
            | "expired"
}

Outgoing offers additionally keep "source_path" per file entry and a
"to": {"id","name"} record — both sender-side only, stripped from the wire.
Incoming offers additionally keep "sender_host" (the request's source IP).

Pending offers expire after OFFER_TTL (300 s, env override
CROSSCOPY_OFFER_TTL for tests); accepted/resumable transfers remain active for
ACCEPTED_TTL (24 hours by default). Expired/terminal offers are pruned lazily
on access. Every state transition publishes an "offers" event on events.bus; desktop
notifications fire on: incoming offer (receiver), declined/completed/failed
(sender), files saved (receiver).
"""

import logging
import os
import shutil
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path, PurePosixPath

import requests

from . import clipboard, config, transfer
from .events import bus
from .notify import notify

log = logging.getLogger("crosscopy.offers")

OFFER_TTL = float(os.environ.get("CROSSCOPY_OFFER_TTL", "") or 300.0)
RESULT_TIMEOUT = 5.0             # cross-peer result/offer POSTs stay short
TRANSFER_TIMEOUT = (5.0, 300.0)  # (connect, read) for file pulls
ACCEPTED_TTL = float(os.environ.get("CROSSCOPY_ACCEPTED_TTL", "") or 86400.0)

ACTIVE_STATES = ("pending", "accepted")
TERMINAL_STATES = ("declined", "completed", "failed", "expired")
RESULT_STATES = ("accepted", "declined", "completed", "failed")


# ---------------------------------------------------------------------------
# Path helpers (shared with server.py's paste; they live here so the offer
# pull can reuse them without a circular import)

def safe_rel_parts(rel_path: str):
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


_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def local_path_parts(parts, platform=None):
    """Make validated wire path parts writable on the receiving OS.

    Linux and macOS preserve names. Windows substitutes characters its file
    APIs reject and prefixes reserved DOS device names, so a Linux peer's
    otherwise-valid file cannot make the whole transfer fail.
    """
    platform = sys.platform if platform is None else platform
    if platform != "win32":
        return tuple(parts)
    cleaned = []
    invalid = set('<>:"/\\|?*')
    for part in parts:
        name = "".join("_" if ch in invalid or ord(ch) < 32 else ch
                       for ch in part).rstrip(" .")
        if not name:
            name = "_"
        if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            name = "_" + name
        cleaned.append(name)
    return tuple(cleaned)


def unique_path(path: Path) -> Path:
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


def _collision_component(name, number, is_dir):
    if is_dir:
        return "%s (%d)" % (name, number)
    path = Path(name)
    return "%s (%d)%s" % (path.stem, number, path.suffix)


def plan_local_paths(paths, dest: Path, platform=None):
    """Plan collision-safe local paths for a complete incoming manifest.

    Planning the hierarchy as a unit is important on Windows: distinct peer
    names such as ``a?`` and ``a*`` sanitize to the same component. Resolving
    only the final filename would merge those directories or turn a file into
    another entry's parent. The returned list stays aligned with ``paths``.
    """
    platform = sys.platform if platform is None else platform
    source_paths = [tuple(parts) for parts in paths]
    tree = {"children": {}, "terminal": False}
    for parts in source_paths:
        node = tree
        for part in parts:
            node = node["children"].setdefault(
                part, {"children": {}, "terminal": False})
        node["terminal"] = True

    mapped = {}

    def key(name):
        if platform == "win32":
            return name.casefold()
        if platform == "darwin":
            # Default APFS/HFS+ volumes are case- and normalization-insensitive.
            return unicodedata.normalize("NFD", name).casefold()
        return name

    def walk(node, source_prefix, local_prefix, local_parent):
        used = set()
        for original, child in node["children"].items():
            if child["terminal"] and child["children"]:
                raise RuntimeError(
                    "incoming manifest uses a path as both file and directory: %r"
                    % "/".join(source_prefix + (original,)))
            base = local_path_parts((original,), platform=platform)[0]
            is_dir = bool(child["children"])
            candidate = base
            number = 1
            while (key(candidate) in used
                   or (local_parent / candidate).exists()):
                candidate = _collision_component(base, number, is_dir)
                number += 1
            used.add(key(candidate))
            source_path = source_prefix + (original,)
            local_path = local_prefix + (candidate,)
            if child["terminal"]:
                mapped[source_path] = local_path
            if child["children"]:
                walk(child, source_path, local_path,
                     local_parent / candidate)

    walk(tree, (), (), Path(dest))

    # Duplicate manifest entries are kept distinct too. They are unusual but
    # should not overwrite one another if received from a malformed peer.
    result = []
    used_targets = set()
    for source_path in source_paths:
        local = mapped[source_path]
        candidate = local
        number = 1
        while (tuple(key(part) for part in candidate) in used_targets
               or Path(dest).joinpath(*candidate).exists()):
            last = _collision_component(local[-1], number, False)
            candidate = local[:-1] + (last,)
            number += 1
        used_targets.add(tuple(key(part) for part in candidate))
        result.append(candidate)
    return result


# ---------------------------------------------------------------------------
# Offer views & summaries

def wire_offer(offer: dict) -> dict:
    """The offer as POSTed to the peer: no status, no local-only keys."""
    pub = {k: v for k, v in offer.items()
           if k not in ("status", "to", "sender_host", "last_activity",
                        "staging_dir")}
    if offer.get("kind") == "files":
        pub["files"] = [{k: v for k, v in f.items()
                         if k not in ("source_path", "mtime_ns")}
                        for f in offer.get("files", [])]
    return pub


def public_offer(offer: dict) -> dict:
    """The offer as returned by the local API: status kept, internals
    (source_path, sender_host) stripped."""
    pub = {k: v for k, v in offer.items()
           if k not in ("sender_host", "last_activity", "staging_dir")}
    if offer.get("kind") == "files":
        pub["files"] = [{k: v for k, v in f.items()
                         if k not in ("source_path", "mtime_ns")}
                        for f in offer.get("files", [])]
    return pub


def summarize(offer: dict) -> str:
    """'3 files (2.1 MB)' or 'text (52 chars)'."""
    if offer.get("kind") == "text":
        return "text (%d chars)" % len(offer.get("text", ""))
    files = offer.get("files", [])
    noun = "file" if len(files) == 1 else "files"
    return "%d %s (%s)" % (len(files), noun,
                           config.format_size(offer.get("total_size", 0)))


def _notify_async(title: str, body: str) -> None:
    """Fire a desktop notification without blocking the caller."""
    threading.Thread(target=notify, args=(title, body), daemon=True).start()


def notify_files_saved(offer: dict, dest, count: int, total_bytes: int) -> None:
    noun = "file" if count == 1 else "files"
    _notify_async("cross-copy", "💾 Saved %d %s (%s) from %s into %s" % (
        count, noun, config.format_size(total_bytes),
        offer.get("from", {}).get("name", "peer"), dest))


# ---------------------------------------------------------------------------
# Manager

class OffersManager:
    """Thread-safe in-memory store of incoming + outgoing offers."""

    def __init__(self, ttl: float = None):
        self.ttl = OFFER_TTL if ttl is None else float(ttl)
        self._lock = threading.Lock()
        self._incoming = {}  # offer_id -> record (has "sender_host")
        self._outgoing = {}  # offer_id -> record (has source_path, "to")

    # -- construction --------------------------------------------------------

    def create_outgoing(self, peer: dict, paths=None, text=None,
                        staging_dir=None) -> dict:
        """Build and store a pending outgoing offer for `peer`. Directory
        expansion reuses clipboard.build_manifest; raises ValueError on bad
        input (missing paths, empty file set, oversized text, both/neither)."""
        if (paths is None) == (text is None):
            raise ValueError("give either 'paths' or 'text', not both")
        offer = {
            "offer_id": str(uuid.uuid4()),
            "from": {
                "id": config.get_device_id(),
                "name": config.get_device_name(),
                "platform": config.platform_name(),
            },
            "sender_port": config.get_port(),
            "created_at": time.time(),
            "last_activity": time.time(),
            "status": "pending",
            "to": {"id": peer.get("id"), "name": peer.get("name")},
        }
        if text is not None:
            manifest = clipboard.build_text_manifest(text)  # validates size
            offer["kind"] = "text"
            offer["text"] = manifest["text"]
            offer["total_size"] = manifest["total_size"]
        else:
            manifest = clipboard.build_manifest(paths)  # expands dirs
            offer["kind"] = "files"
            offer["files"] = [
                {"index": f["index"], "rel_path": f["rel_path"],
                 "size": f["size"], "sha256": f["sha256"],
                 "mtime_ns": f["mtime_ns"],
                 "source_path": f["source_path"]}
                for f in manifest["files"]
            ]
            offer["total_size"] = manifest["total_size"]
            if staging_dir is not None:
                offer["staging_dir"] = str(staging_dir)
        with self._lock:
            self._prune_locked()  # creation publishes below anyway
            self._outgoing[offer["offer_id"]] = offer
        bus.publish("offers")
        return dict(offer)

    def record_incoming(self, offer: dict, sender_host: str) -> dict:
        """Store a peer-pushed offer as pending incoming; fires the SSE event
        and the receiver-side desktop notification."""
        record = dict(offer)
        record["status"] = "pending"
        record["sender_host"] = sender_host
        record.setdefault("created_at", time.time())
        record["last_activity"] = time.time()
        with self._lock:
            self._prune_locked()
            self._incoming[record["offer_id"]] = record
        bus.publish("offers")
        _notify_async("cross-copy",
                      "📥 %s wants to send %s — accept in the cross-copy UI "
                      "or `ccp accept`" % (
                          record.get("from", {}).get("name", "a peer"),
                          summarize(record)))
        return dict(record)

    # -- lookups (lazily pruning) ---------------------------------------------

    def get_outgoing(self, offer_id: str):
        return self._get(self._outgoing, offer_id)

    def get_incoming(self, offer_id: str):
        return self._get(self._incoming, offer_id)

    def _get(self, table: dict, offer_id: str):
        with self._lock:
            expired = self._prune_locked()
            offer = table.get(offer_id)
            offer = dict(offer) if offer is not None else None
        if expired:
            bus.publish("offers")
        return offer

    def list_incoming_pending(self) -> list:
        """Pending incoming offers, newest first."""
        with self._lock:
            expired = self._prune_locked()
            offers = [dict(o) for o in self._incoming.values()
                      if o.get("status") == "pending"]
        if expired:
            bus.publish("offers")
        return sorted(offers, key=lambda o: o.get("created_at", 0.0),
                      reverse=True)

    def discard_outgoing(self, offer_id: str) -> None:
        """Drop an outgoing offer that never reached the peer (send failed)."""
        with self._lock:
            removed = self._outgoing.pop(offer_id, None)
        if removed is not None:
            self._cleanup_staging(removed)
            bus.publish("offers")

    # -- state transitions ----------------------------------------------------

    def set_outgoing_status(self, offer_id: str, status: str):
        """Apply a receiver-reported result to an outgoing offer. Returns the
        updated offer, or None if unknown/pruned. Terminal offers are left
        unchanged. Fires SSE + sender-side notifications per spec."""
        changed, offer = self._transition(self._outgoing, offer_id, status)
        if changed:
            to_name = offer.get("to", {}).get("name", "peer")
            if status == "declined":
                _notify_async("cross-copy", "🚫 %s declined %s"
                              % (to_name, summarize(offer)))
            elif status == "completed":
                _notify_async("cross-copy", "✅ %s received %s"
                              % (to_name, summarize(offer)))
            elif status == "failed":
                _notify_async("cross-copy", "⚠️ Sending %s to %s failed"
                              % (summarize(offer), to_name))
        return offer

    def set_incoming_status(self, offer_id: str, status: str,
                            remove: bool = False):
        """Transition an incoming offer (receiver side). No notifications
        here — the receiver notifies on arrival and on files-saved only."""
        _changed, offer = self._transition(self._incoming, offer_id, status,
                                           remove=remove)
        return offer

    def _transition(self, table: dict, offer_id: str, status: str,
                    remove: bool = False):
        with self._lock:
            expired = self._prune_locked()
            offer = table.get(offer_id)
            changed = False
            if offer is not None:
                if offer.get("status") not in TERMINAL_STATES \
                        and offer.get("status") != status:
                    offer["status"] = status
                    offer["last_activity"] = time.time()
                    changed = True
                if remove:
                    table.pop(offer_id, None)
                offer = dict(offer)
        if expired or changed:
            bus.publish("offers")
        if changed:
            log.info("offer %s -> %s (%s)", offer_id, status, summarize(offer))
            if status in TERMINAL_STATES:
                self._cleanup_staging(offer)
        return changed, offer

    @staticmethod
    def _cleanup_staging(offer: dict) -> None:
        """Remove daemon-owned files for a completed/abandoned upload.

        Only direct children of offer-staging are eligible. This guard keeps
        a malformed or accidentally reused local-only path from deleting an
        arbitrary directory.
        """
        raw = offer.get("staging_dir")
        if not raw:
            return
        try:
            root = config.offer_staging_dir().resolve()
            target = Path(raw).resolve()
            if target.parent != root:
                log.warning("refusing to clean unsafe offer staging path %s",
                            target)
                return
            shutil.rmtree(target, ignore_errors=True)
        except OSError as exc:
            log.warning("could not clean offer staging directory %s: %s",
                        raw, exc)

    # -- expiry ----------------------------------------------------------------

    def _prune_locked(self) -> int:
        """Drop offers past their TTL (marking still-active ones expired).
        Caller holds the lock; returns how many active offers expired so the
        caller can publish an "offers" event outside the lock."""
        now = time.time()
        expired = 0
        for table in (self._incoming, self._outgoing):
            for oid, offer in list(table.items()):
                ttl = ACCEPTED_TTL if offer.get("status") == "accepted" \
                    else self.ttl
                activity = offer.get("last_activity",
                                     offer.get("created_at", 0.0))
                if now - activity > ttl:
                    if offer.get("status") not in TERMINAL_STATES:
                        offer["status"] = "expired"
                        expired += 1
                        log.info("offer %s expired (%s)", oid,
                                 summarize(offer))
                    self._cleanup_staging(offer)
                    del table[oid]
        return expired


# Shared process-wide manager (like events.bus).
manager = OffersManager()


# ---------------------------------------------------------------------------
# Cross-peer transfer helpers (receiver side)

def sender_base_url(offer: dict) -> str:
    return "http://%s:%d" % (offer.get("sender_host"),
                             int(offer.get("sender_port")
                                 or config.DEFAULT_PORT))


def resume_metadata(offer: dict) -> dict:
    source = dict(offer.get("from") or {})
    source["host"] = offer.get("sender_host")
    source["port"] = int(offer.get("sender_port") or config.DEFAULT_PORT)
    return {
        "kind": "offer",
        "offer_id": offer.get("offer_id"),
        "source": source,
        "files": [dict(entry) for entry in offer.get("files") or []],
        "total_size": int(offer.get("total_size") or sum(
            int(entry.get("size", 0)) for entry in offer.get("files") or [])),
    }


def report_result(offer: dict, result: str) -> bool:
    """POST an outcome to the sender's /api/offer/<id>/result. Returns True
    on a 200; never raises."""
    url = "%s/api/offer/%s/result" % (sender_base_url(offer),
                                      offer["offer_id"])
    try:
        resp = requests.post(url, json={"result": result},
                             timeout=RESULT_TIMEOUT)
        return resp.status_code == 200
    except Exception as exc:
        log.warning("could not report '%s' to sender for offer %s: %s",
                    result, offer.get("offer_id"), exc)
        return False


def pull_files(offer: dict, dest: Path):
    """Pull offered files with persistent range-resume and SHA-256 checks."""
    try:
        entries = offer.get("files", [])
        source_parts = []
        for entry in entries:
            parts = safe_rel_parts(entry.get("rel_path", ""))
            if parts is None:
                raise RuntimeError("unsafe rel_path from peer: %r"
                                   % entry.get("rel_path"))
            source_parts.append(parts)
        planned = plan_local_paths(source_parts, dest)

        def fetch(entry, offset):
            url = "%s/api/offer/%s/file/%d" % (
                sender_base_url(offer), offer["offer_id"], int(entry["index"]))
            headers = {"Range": "bytes=%d-" % offset} if offset else {}
            return requests.get(url, headers=headers, stream=True,
                                timeout=TRANSFER_TIMEOUT)

        return transfer.download_files(
            entries, planned, dest, "offer:%s" % offer["offer_id"], fetch,
            registry_dir=config.resume_registry_dir(),
            metadata=resume_metadata(offer))
    except Exception as exc:
        if isinstance(exc, transfer.TransferError):
            raise
        raise transfer.TransferError(str(exc))
