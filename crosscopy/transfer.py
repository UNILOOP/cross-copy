"""Resumable, integrity-checked file transfer primitives.

Partial bytes and a small state file live under the destination's hidden
``.crosscopy-resume`` directory. Final paths are only exposed after the
expected size and SHA-256 digest have been verified.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path


HASH_CHUNK_SIZE = 1024 * 1024
STREAM_CHUNK_SIZE = 64 * 1024
MAX_ATTEMPTS = 5
RETRY_DELAY = 0.5
RESUME_DIR_NAME = ".crosscopy-resume"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
_transfer_locks = {}
_transfer_locks_guard = threading.Lock()


class TransferError(RuntimeError):
    """A transfer could not finish; retained partial data may be resumed."""


class IntegrityError(TransferError):
    """Received bytes do not match the advertised file."""


class ResumeActiveError(TransferError):
    """A partial transfer cannot be removed while it is being written."""


def valid_sha256(value):
    return isinstance(value, str) and SHA256_RE.fullmatch(value.lower()) is not None


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def source_snapshot(path, attempts=2):
    """Return stable size/mtime/digest metadata for a local source file."""
    path = Path(path)
    for _attempt in range(max(1, int(attempts))):
        before = path.stat()
        digest = sha256_file(path)
        after = path.stat()
        before_mtime = getattr(before, "st_mtime_ns",
                               int(before.st_mtime * 1000000000))
        after_mtime = getattr(after, "st_mtime_ns",
                              int(after.st_mtime * 1000000000))
        if before.st_size == after.st_size and before_mtime == after_mtime:
            return {
                "size": int(after.st_size),
                "mtime_ns": int(after_mtime),
                "sha256": digest,
            }
    raise ValueError("file changed while Cross Copy was preparing it: %s" % path)


def _manifest_fingerprint(entries):
    identity = [{
        "index": entry.get("index"),
        "rel_path": entry.get("rel_path"),
        "size": int(entry.get("size", 0)),
        "sha256": str(entry.get("sha256") or "").lower(),
    } for entry in entries]
    encoded = json.dumps(identity, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def same_manifest(first, second):
    try:
        return _manifest_fingerprint(first) == _manifest_fingerprint(second)
    except (AttributeError, TypeError, ValueError):
        return False


def _resume_directory(dest, transfer_id):
    token = hashlib.sha256(str(transfer_id).encode("utf-8")).hexdigest()[:32]
    return Path(dest) / RESUME_DIR_NAME / token


def _prepare_resume_directory(dest, transfer_id):
    root = Path(dest) / RESUME_DIR_NAME
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise TransferError("unsafe resume directory at %s" % root)
    root.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            hidden = 0x2
            kernel32 = ctypes.windll.kernel32
            kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetFileAttributesW.restype = wintypes.DWORD
            kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR,
                                                     wintypes.DWORD]
            kernel32.SetFileAttributesW.restype = wintypes.BOOL
            attributes = kernel32.GetFileAttributesW(str(root))
            if attributes != 0xFFFFFFFF:
                kernel32.SetFileAttributesW(str(root), attributes | hidden)
        except Exception:
            pass
    resume_dir = _resume_directory(dest, transfer_id)
    if resume_dir.is_symlink() or (resume_dir.exists()
                                   and not resume_dir.is_dir()):
        raise TransferError("unsafe transfer resume state at %s" % resume_dir)
    resume_dir.mkdir(parents=True, exist_ok=True)
    return resume_dir


def _atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def resume_session_id(dest, transfer_id):
    identity = "%s\0%s" % (Path(dest).resolve(), transfer_id)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _registry_path(registry_dir, session_id):
    return Path(registry_dir) / (str(session_id) + ".json")


def _register_session(registry_dir, dest, transfer_id, resume_dir, metadata):
    registry_dir = Path(registry_dir)
    registry_dir.mkdir(parents=True, exist_ok=True)
    session_id = resume_session_id(dest, transfer_id)
    path = _registry_path(registry_dir, session_id)
    created_at = time.time()
    try:
        with open(path, encoding="utf-8") as handle:
            existing = json.load(handle)
        created_at = float(existing.get("created_at") or created_at)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    record = {
        "format": 1,
        "id": session_id,
        "transfer_id": str(transfer_id),
        "dest": str(Path(dest).resolve()),
        "resume_dir": str(Path(resume_dir).resolve()),
        "created_at": created_at,
        "updated_at": time.time(),
        "metadata": dict(metadata or {}),
    }
    _atomic_json(path, record)
    return session_id


def _unregister_session(registry_dir, session_id):
    if registry_dir is None or session_id is None:
        return
    try:
        _registry_path(registry_dir, session_id).unlink()
    except OSError:
        pass


def _read_registry_record(registry_dir, session_id):
    path = _registry_path(registry_dir, session_id)
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if (not isinstance(record, dict) or record.get("format") != 1
            or record.get("id") != session_id):
        return None
    try:
        expected = _resume_directory(record["dest"], record["transfer_id"])
        expected_path = Path(os.path.abspath(str(expected)))
        recorded_path = Path(os.path.abspath(str(record["resume_dir"])))
        if expected_path != recorded_path:
            return None
    except (KeyError, OSError, TypeError, ValueError):
        return None
    return record


def get_resume_session(registry_dir, session_id):
    record = _read_registry_record(registry_dir, str(session_id))
    return _session_summary(record) if record else None


def list_resume_sessions(registry_dir):
    registry_dir = Path(registry_dir)
    try:
        paths = list(registry_dir.glob("*.json"))
    except OSError:
        return []
    sessions = []
    for path in paths:
        record = _read_registry_record(registry_dir, path.stem)
        if record:
            sessions.append(_session_summary(record))
    return sorted(sessions, key=lambda item: item.get("updated_at", 0),
                  reverse=True)


def _session_summary(record):
    metadata = dict(record.get("metadata") or {})
    total_bytes = 0
    received_bytes = 0
    completed_files = 0
    file_count = 0
    resume_dir = Path(record.get("resume_dir") or "")
    broken = resume_dir.is_symlink() or resume_dir.parent.is_symlink()
    try:
        if broken:
            raise OSError("unsafe resume-directory symlink")
        with open(resume_dir / "state.json", encoding="utf-8") as handle:
            state = json.load(handle)
        files = state.get("files") or []
    except (OSError, ValueError, TypeError, AttributeError):
        files = []
    dest = Path(record.get("dest") or "")
    for position, item in enumerate(files):
        if not isinstance(item, dict):
            continue
        size = max(0, int(item.get("size", 0)))
        total_bytes += size
        file_count += 1
        target_parts = item.get("target_parts")
        target = dest.joinpath(*target_parts) \
            if _safe_target_parts(target_parts) else None
        partial = resume_dir / ("%06d.part" % position)
        if item.get("completed") and target is not None \
                and _matches(target, size, str(item.get("sha256") or "")):
            received_bytes += size
            completed_files += 1
        else:
            try:
                received_bytes += min(size, max(0, partial.stat().st_size))
            except OSError:
                pass
    summary = {
        "id": record.get("id"),
        "kind": metadata.get("kind") or "unknown",
        "source": metadata.get("source") or {},
        "dest": record.get("dest"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "total_bytes": total_bytes,
        "received_bytes": received_bytes,
        "file_count": file_count,
        "completed_files": completed_files,
        "progress": (float(received_bytes) / total_bytes
                     if total_bytes else 0.0),
        "broken": broken,
        "metadata": metadata,
    }
    return summary


def discard_resume_session(registry_dir, session_id):
    session_id = str(session_id)
    record = _read_registry_record(registry_dir, session_id)
    if not record:
        return False
    identity = "%s\0%s" % (
        Path(record["dest"]).resolve(), record["transfer_id"])
    lock_record = _claim_transfer_lock(identity)
    acquired = lock_record["lock"].acquire(blocking=False)
    if not acquired:
        _release_transfer_lock(identity, lock_record)
        raise ResumeActiveError(
            "transfer is currently active; wait for it to finish or stop")
    try:
        resume_dir = Path(record["resume_dir"])
        if resume_dir.is_symlink() or resume_dir.parent.is_symlink():
            if resume_dir.is_symlink():
                try:
                    resume_dir.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            # Never follow a resume or resume-root link. Even when the
            # filesystem refuses to unlink a direct link, discard the stale
            # registry pointer so recovery surfaces cannot stay wedged.
            _unregister_session(registry_dir, session_id)
            try:
                resume_dir.parent.rmdir()
            except OSError:
                pass
            return True
        try:
            shutil.rmtree(resume_dir)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        _unregister_session(registry_dir, session_id)
        try:
            resume_dir.parent.rmdir()
        except OSError:
            pass
        return True
    finally:
        lock_record["lock"].release()
        _release_transfer_lock(identity, lock_record)


def _safe_target_parts(parts):
    return (isinstance(parts, list) and bool(parts)
            and all(isinstance(part, str) and part not in ("", ".", "..")
                    and "/" not in part and "\\" not in part
                    for part in parts))


def _new_state(entries, planned):
    return {
        "format": 1,
        "fingerprint": _manifest_fingerprint(entries),
        "files": [{
            "index": entry.get("index"),
            "rel_path": entry.get("rel_path"),
            "size": int(entry.get("size", 0)),
            "sha256": str(entry.get("sha256") or "").lower(),
            "target_parts": list(parts),
            "completed": False,
        } for entry, parts in zip(entries, planned)],
    }


def _load_or_create_state(resume_dir, entries, planned):
    fingerprint = _manifest_fingerprint(entries)
    state_path = resume_dir / "state.json"
    try:
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            raise ValueError("resume state is not an object")
        files = state.get("files")
        valid = (state.get("format") == 1
                 and state.get("fingerprint") == fingerprint
                 and isinstance(files, list)
                 and len(files) == len(entries)
                 and all(_safe_target_parts(item.get("target_parts"))
                         for item in files if isinstance(item, dict)))
        if valid and len(files) == sum(isinstance(item, dict) for item in files):
            return state
    except (OSError, ValueError, TypeError):
        pass

    shutil.rmtree(resume_dir, ignore_errors=True)
    resume_dir.mkdir(parents=True, exist_ok=True)
    state = _new_state(entries, planned)
    _atomic_json(state_path, state)
    return state


def _matches(path, size, checksum):
    try:
        if path.stat().st_size != size:
            return False
        return not checksum or sha256_file(path) == checksum
    except OSError:
        return False


def _unique_path(path):
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    number = 1
    while True:
        candidate = path.with_name("%s (%d)%s" % (stem, number, suffix))
        if not candidate.exists():
            return candidate
        number += 1


def _response_write_offset(response, requested, expected_size):
    if response.status_code == 416:
        raise TransferError("sender rejected resume offset %d" % requested)
    response.raise_for_status()
    if response.status_code == 206:
        value = response.headers.get("Content-Range", "")
        match = CONTENT_RANGE_RE.fullmatch(value.strip())
        if not match or int(match.group(1)) != requested:
            raise TransferError("sender returned an invalid Content-Range")
        total = match.group(3)
        if total != "*" and int(total) != expected_size:
            raise IntegrityError(
                "sender size changed (expected %d, reported %s)"
                % (expected_size, total))
        return requested
    if response.status_code == 200:
        return 0  # legacy server ignored Range; safely restart this file
    raise TransferError("sender returned HTTP %d" % response.status_code)


def _download_one(entry, partial, fetch, max_attempts, retry_delay):
    expected_size = int(entry.get("size", 0))
    checksum = str(entry.get("sha256") or "").lower()
    if expected_size < 0:
        raise IntegrityError("invalid negative file size")
    if not checksum:
        raise IntegrityError(
            "sender did not provide a SHA-256 checksum; update Cross Copy on the sender")
    if not valid_sha256(checksum):
        raise IntegrityError("invalid SHA-256 in manifest")

    partial.parent.mkdir(parents=True, exist_ok=True)
    if partial.exists() and partial.stat().st_size > expected_size:
        partial.unlink()
    if expected_size == 0 and not partial.exists():
        partial.touch()

    attempts = max(1, int(max_attempts))
    last_error = None
    for attempt in range(attempts):
        offset = partial.stat().st_size if partial.exists() else 0
        rollback_offset = None
        try:
            if offset < expected_size:
                response = fetch(entry, offset)
                try:
                    remote_checksum = str(
                        response.headers.get("X-CrossCopy-SHA256") or "").lower()
                    if checksum and remote_checksum and remote_checksum != checksum:
                        raise IntegrityError("sender checksum changed during transfer")
                    write_offset = _response_write_offset(
                        response, offset, expected_size)
                    mode = "ab" if write_offset == offset and offset > 0 else "wb"
                    rollback_offset = write_offset
                    written = write_offset
                    with open(partial, mode) as handle:
                        for chunk in response.iter_content(STREAM_CHUNK_SIZE):
                            if not chunk:
                                continue
                            written += len(chunk)
                            if written > expected_size:
                                raise IntegrityError(
                                    "received more bytes than advertised")
                            handle.write(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    response.close()

            actual_size = partial.stat().st_size
            if actual_size < expected_size:
                raise TransferError(
                    "incomplete response (%d of %d bytes)"
                    % (actual_size, expected_size))
            if actual_size > expected_size:
                raise IntegrityError("received more bytes than advertised")
            if checksum and sha256_file(partial) != checksum:
                raise IntegrityError("SHA-256 checksum mismatch")
            return
        except Exception as exc:
            last_error = exc
            if isinstance(exc, IntegrityError) and partial.exists():
                try:
                    if rollback_offset is not None and rollback_offset > 0:
                        with open(partial, "r+b") as handle:
                            handle.truncate(rollback_offset)
                            handle.flush()
                            os.fsync(handle.fileno())
                    elif rollback_offset == 0 or offset >= expected_size:
                        partial.unlink()
                except OSError:
                    pass
            if attempt + 1 < attempts:
                time.sleep(float(retry_delay) * (2 ** attempt))

    detail = str(last_error) or last_error.__class__.__name__
    raise TransferError("%s; partial progress was saved and the transfer can be retried"
                        % detail)


def _download_files(entries, planned, dest, transfer_id, fetch,
                    max_attempts=MAX_ATTEMPTS, retry_delay=RETRY_DELAY,
                    registry_dir=None, metadata=None):
    """Download a complete manifest with persistent resume support.

    ``fetch(entry, offset)`` must return a requests-like streaming response.
    ``planned`` is a list of collision-safe path-part tuples aligned with
    ``entries``. Returns ``(final_paths, total_bytes)``.
    """
    entries = list(entries)
    planned = list(planned)
    dest = Path(dest)
    if len(entries) != len(planned):
        raise TransferError("manifest and destination plan differ")
    for entry in entries:
        try:
            size = int(entry.get("size", -1))
        except (AttributeError, TypeError, ValueError):
            raise IntegrityError("manifest contains an invalid file size")
        if size < 0:
            raise IntegrityError("manifest contains an invalid file size")
        if not transfer_checksum(entry):
            raise IntegrityError(
                "sender did not provide a valid SHA-256 checksum; "
                "update Cross Copy on the sender")
    dest.mkdir(parents=True, exist_ok=True)
    resume_dir = _prepare_resume_directory(dest, transfer_id)
    state = _load_or_create_state(resume_dir, entries, planned)
    state_path = resume_dir / "state.json"
    session_id = None
    if registry_dir is not None:
        session_id = _register_session(
            registry_dir, dest, transfer_id, resume_dir, metadata)

    final_paths = []
    for position, (entry, item) in enumerate(zip(entries, state["files"])):
        expected_size = int(item.get("size", 0))
        checksum = str(item.get("sha256") or "").lower()
        target = dest.joinpath(*item["target_parts"])
        target.parent.mkdir(parents=True, exist_ok=True)

        if _matches(target, expected_size, checksum):
            if not item.get("completed"):
                item["completed"] = True
                _atomic_json(state_path, state)
            final_paths.append(target)
            continue
        if target.exists():
            target = _unique_path(target)
            item["target_parts"] = list(target.relative_to(dest).parts)
            item["completed"] = False
            _atomic_json(state_path, state)

        partial = resume_dir / ("%06d.part" % position)
        _download_one(entry, partial, fetch, max_attempts, retry_delay)

        # A file may have appeared at the planned path while bytes streamed.
        if target.exists():
            target = _unique_path(target)
            item["target_parts"] = list(target.relative_to(dest).parts)
            _atomic_json(state_path, state)
        os.replace(partial, target)
        item["completed"] = True
        _atomic_json(state_path, state)
        final_paths.append(target)

    total_bytes = sum(path.stat().st_size for path in final_paths)
    shutil.rmtree(resume_dir, ignore_errors=True)
    try:
        resume_dir.parent.rmdir()
    except OSError:
        pass
    _unregister_session(registry_dir, session_id)
    return final_paths, total_bytes


def transfer_checksum(entry):
    """Return a normalized manifest checksum, or an empty string."""
    try:
        checksum = str(entry.get("sha256") or "").lower()
    except AttributeError:
        return ""
    return checksum if valid_sha256(checksum) else ""


def download_files(entries, planned, dest, transfer_id, fetch,
                   max_attempts=MAX_ATTEMPTS, retry_delay=RETRY_DELAY,
                   registry_dir=None, metadata=None):
    """Serialize matching transfers, then run the resumable download."""
    identity = "%s\0%s" % (Path(dest).resolve(), transfer_id)
    record = _claim_transfer_lock(identity)
    lock = record["lock"]
    try:
        with lock:
            return _download_files(
                entries, planned, dest, transfer_id, fetch,
                max_attempts=max_attempts, retry_delay=retry_delay,
                registry_dir=registry_dir, metadata=metadata)
    finally:
        _release_transfer_lock(identity, record)


def _claim_transfer_lock(identity):
    with _transfer_locks_guard:
        record = _transfer_locks.get(identity)
        if record is None:
            record = {"lock": threading.Lock(), "users": 0}
            _transfer_locks[identity] = record
        record["users"] += 1
        return record


def _release_transfer_lock(identity, record):
    with _transfer_locks_guard:
        record["users"] -= 1
        if record["users"] == 0 and _transfer_locks.get(identity) is record:
            _transfer_locks.pop(identity, None)
