"""Clipboard manifest handling for cross-copy.

The clipboard is a JSON manifest persisted at ~/.crosscopy/clipboard.json:

{
  "clipboard_id": "uuid4",
  "kind": "files" | "text",          // missing => "files" (pre-0.2 manifests)
  "op": "copy" | "move",
  "created_at": epoch_float,
  "host_id": "...", "host_name": "...",
  "total_size": int,
  "files": [{"index", "rel_path", "size", "source_path"}],  // kind == "files"
  "text": "..."                                             // kind == "text"
}

Directories are expanded recursively at copy time; each entry's rel_path is a
POSIX relative path that includes the top-level directory name. source_path is
local-only and stripped from manifests served to peers (public_manifest).

Text manifests carry the full string in "text" (UTF-8, max 1 MB) with
total_size = its byte length; text is never written to staging.
"""

import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path, PurePosixPath

from . import config

log = logging.getLogger("crosscopy.clipboard")

MAX_TEXT_BYTES = 1024 * 1024  # 1 MB cap on text clipboards


# ---------------------------------------------------------------------------
# Manifest construction

def build_manifest(paths, op="copy") -> dict:
    """Build a clipboard manifest from a list of file/dir paths.

    Directories are expanded recursively; rel_path for entries under a
    directory includes the top-level directory name. Raises ValueError on
    invalid op, missing paths, or an empty file set.
    """
    if op not in ("copy", "move"):
        raise ValueError("op must be 'copy' or 'move'")

    files = []
    missing = []
    for raw in paths:
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if path.is_file():
            files.append(_file_entry(path, path.name))
        elif path.is_dir():
            top = path
            for root, dirnames, filenames in os.walk(top):
                dirnames.sort()
                for name in sorted(filenames):
                    fp = Path(root) / name
                    if not fp.is_file():
                        continue  # skip broken symlinks, sockets, etc.
                    rel = fp.relative_to(top.parent).as_posix()
                    files.append(_file_entry(fp, rel))
        else:
            missing.append(str(raw))

    if missing:
        raise ValueError("paths do not exist: " + ", ".join(missing))
    if not files:
        raise ValueError("nothing to copy (no regular files found)")

    for i, entry in enumerate(files):
        entry["index"] = i

    return {
        "clipboard_id": str(uuid.uuid4()),
        "kind": "files",
        "op": op,
        "created_at": time.time(),
        "host_id": config.get_device_id(),
        "host_name": config.get_device_name(),
        "total_size": sum(f["size"] for f in files),
        "files": files,
    }


def build_text_manifest(text, op="copy") -> dict:
    """Build a text clipboard manifest. Raises ValueError on invalid op,
    non-string/empty text, or text larger than MAX_TEXT_BYTES."""
    if op not in ("copy", "move"):
        raise ValueError("op must be 'copy' or 'move'")
    if not isinstance(text, str) or not text:
        raise ValueError("text must be a non-empty string")
    size = len(text.encode("utf-8"))
    if size > MAX_TEXT_BYTES:
        raise ValueError("text too large (%d bytes, max %d)" % (size, MAX_TEXT_BYTES))
    return {
        "clipboard_id": str(uuid.uuid4()),
        "kind": "text",
        "op": op,
        "created_at": time.time(),
        "host_id": config.get_device_id(),
        "host_name": config.get_device_name(),
        "total_size": size,
        "text": text,
    }


def manifest_kind(manifest) -> str:
    """'text' or 'files'; a manifest without 'kind' (pre-0.2) is files."""
    return (manifest or {}).get("kind") or "files"


def _file_entry(path: Path, rel_path: str) -> dict:
    return {
        "index": 0,  # filled in by build_manifest
        "rel_path": rel_path,
        "size": path.stat().st_size,
        "source_path": str(path),
    }


def public_manifest(manifest):
    """Copy of the manifest with source_path stripped (for serving to peers)."""
    if not manifest:
        return None
    pub = dict(manifest)
    pub["files"] = [
        {k: v for k, v in f.items() if k != "source_path"}
        for f in manifest.get("files", [])
    ]
    return pub


# ---------------------------------------------------------------------------
# Persistence

def load_clipboard():
    """Return the current manifest, or None if the clipboard is empty."""
    path = config.clipboard_path()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        if isinstance(manifest, dict) and manifest.get("files"):
            return manifest
    except (OSError, ValueError):
        pass
    return None


def set_clipboard(manifest: dict) -> None:
    """Persist the manifest as the current clipboard and drop stale staging."""
    path = config.clipboard_path()
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    clean_staging(keep=manifest.get("clipboard_id"))


def clear_clipboard() -> None:
    """Remove the clipboard manifest and all staged uploads."""
    try:
        config.clipboard_path().unlink()
    except OSError:
        pass
    clean_staging(keep=None)


def clean_staging(keep=None) -> None:
    """Delete staging subdirectories except the one named `keep` (if any)."""
    staging = config.staging_dir()
    try:
        children = list(staging.iterdir())
    except OSError:
        return
    for child in children:
        if keep is not None and child.name == keep:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Move semantics: delete sources after a peer has consumed the clipboard

def delete_sources(manifest: dict) -> None:
    """Delete the manifest's source files, then any now-empty source dirs."""
    top_dirs = set()
    for entry in manifest.get("files", []):
        source = entry.get("source_path")
        if not source:
            continue
        sp = Path(source)
        try:
            sp.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete %s: %s", sp, exc)
        # If this file came from an expanded directory, remember the top-level
        # dir so we can clean it up if it ends up empty. For a rel_path like
        # "photos/2024/img.jpg" the top dir is source_path with the rel_path
        # components stripped, plus the first rel_path component.
        parts = PurePosixPath(entry.get("rel_path", "")).parts
        if len(parts) > 1:
            base = sp
            for _ in range(len(parts)):
                base = base.parent
            top_dirs.add(base / parts[0])

    for top in top_dirs:
        if not top.is_dir():
            continue
        for root, _dirs, _names in os.walk(top, topdown=False):
            try:
                os.rmdir(root)  # only succeeds if empty
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Convenience

def summarize(manifest) -> str:
    """Short human summary, e.g. '3 files, 2.1 MB'."""
    if not manifest or not manifest.get("files"):
        return "-"
    count = len(manifest["files"])
    noun = "file" if count == 1 else "files"
    return "%d %s, %s" % (count, noun, config.format_size(manifest.get("total_size", 0)))
