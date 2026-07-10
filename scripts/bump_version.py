#!/usr/bin/env python3
"""Bump Cross Copy's release version in every managed source file.

Examples:
    python3 scripts/bump_version.py patch
    python3 scripts/bump_version.py minor
    python3 scripts/bump_version.py 1.0.0
    python3 scripts/bump_version.py --check
"""

import argparse
import os
import re
import stat
import sys
import tempfile
from pathlib import Path


SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

MANAGED_VERSIONS = (
    (
        Path("crosscopy/__init__.py"),
        re.compile(
            r'^(?P<prefix>__version__\s*=\s*["\'])'
            r'(?P<version>\d+\.\d+\.\d+)'
            r'(?P<suffix>["\']\s*)$',
            re.MULTILINE,
        ),
    ),
    (
        Path("setup.cfg"),
        re.compile(
            r"^(?P<prefix>version\s*=\s*)"
            r"(?P<version>\d+\.\d+\.\d+)"
            r"(?P<suffix>\s*)$",
            re.MULTILINE,
        ),
    ),
    (
        Path("README.md"),
        re.compile(
            r"(?P<prefix>Cross Copy version )"
            r"(?P<version>\d+\.\d+\.\d+)"
            r"(?P<suffix> uses a trusted-LAN model\.)"
        ),
    ),
)


class VersionBumpError(Exception):
    pass


def parse_version(value):
    match = SEMVER_RE.fullmatch(str(value))
    if not match:
        raise VersionBumpError(
            "versions must use MAJOR.MINOR.PATCH with non-negative integers")
    return tuple(int(part) for part in match.groups())


def version_text(parts):
    return ".".join(str(part) for part in parts)


def next_version(current, requested):
    current_parts = parse_version(current)
    if requested == "major":
        target = (current_parts[0] + 1, 0, 0)
    elif requested == "minor":
        target = (current_parts[0], current_parts[1] + 1, 0)
    elif requested == "patch":
        target = (current_parts[0], current_parts[1], current_parts[2] + 1)
    else:
        target = parse_version(requested)
    if target <= current_parts:
        raise VersionBumpError(
            "new version %s must be greater than current version %s"
            % (version_text(target), current))
    return version_text(target)


def read_managed_versions(root):
    records = []
    for relative_path, pattern in MANAGED_VERSIONS:
        path = root / relative_path
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise VersionBumpError("could not read %s: %s" % (path, exc))
        matches = list(pattern.finditer(content))
        if len(matches) != 1:
            raise VersionBumpError(
                "%s must contain exactly one managed version (found %d)"
                % (relative_path, len(matches)))
        records.append({
            "path": path,
            "relative_path": relative_path,
            "pattern": pattern,
            "content": content,
            "version": matches[0].group("version"),
        })
    versions = {record["version"] for record in records}
    if len(versions) != 1:
        details = ", ".join(
            "%s=%s" % (record["relative_path"], record["version"])
            for record in records)
        raise VersionBumpError("managed versions disagree: %s" % details)
    return versions.pop(), records


def _replace_version(record, target):
    def replacement(match):
        return match.group("prefix") + target + match.group("suffix")

    content, count = record["pattern"].subn(
        replacement, record["content"], count=1)
    if count != 1:
        raise VersionBumpError(
            "could not update %s" % record["relative_path"])
    return content


def _atomic_write(path, content):
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def bump_version(root, requested, dry_run=False):
    root = Path(root).resolve()
    current, records = read_managed_versions(root)
    target = next_version(current, requested)
    updates = [(record, _replace_version(record, target))
               for record in records]
    if dry_run:
        return current, target, [record["relative_path"] for record, _ in updates]

    written = []
    try:
        for record, content in updates:
            _atomic_write(record["path"], content)
            written.append(record)
        verified, _records = read_managed_versions(root)
        if verified != target:
            raise VersionBumpError(
                "post-write verification returned %s instead of %s"
                % (verified, target))
    except Exception as exc:
        rollback_errors = []
        for record in reversed(written):
            try:
                _atomic_write(record["path"], record["content"])
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise VersionBumpError(
                "version bump failed (%s); rollback also failed: %s"
                % (exc, "; ".join(rollback_errors)))
        if isinstance(exc, VersionBumpError):
            raise
        raise VersionBumpError("version bump failed and was rolled back: %s" % exc)
    return current, target, [record["relative_path"] for record, _ in updates]


def build_parser():
    parser = argparse.ArgumentParser(
        description="Bump Cross Copy's version in all managed files.")
    parser.add_argument(
        "version", nargs="?", metavar="VERSION|major|minor|patch",
        help="explicit semantic version or the component to increment")
    parser.add_argument(
        "--check", action="store_true",
        help="verify managed files agree without changing them")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show the requested bump without changing files")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if args.check:
            if args.version or args.dry_run:
                parser.error("--check cannot be combined with a version or --dry-run")
            current, records = read_managed_versions(root)
            print("Version consistency OK: %s (%d managed files)"
                  % (current, len(records)))
            return 0
        if not args.version:
            parser.error("provide VERSION, major, minor, or patch")
        current, target, paths = bump_version(
            root, args.version, dry_run=args.dry_run)
    except VersionBumpError as exc:
        print("Version bump failed: %s" % exc, file=sys.stderr)
        return 2

    verb = "Would bump" if args.dry_run else "Bumped"
    print("%s Cross Copy %s -> %s" % (verb, current, target))
    for path in paths:
        print("  %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
