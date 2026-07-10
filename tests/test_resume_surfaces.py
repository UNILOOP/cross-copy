import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

from crosscopy import cli, clipboard, config, offers, server, transfer


class BrokenResponse:
    status_code = 200
    headers = {}

    def raise_for_status(self):
        return None

    def iter_content(self, _size):
        yield b"abc"
        raise requests.ConnectionError("offline")

    def close(self):
        pass


class ApiResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400

    def json(self):
        return self.payload


class StreamResponse(ApiResponse):
    def __init__(self, payload, headers):
        super().__init__({}, 206)
        self.data = payload
        self.headers = headers

    def raise_for_status(self):
        return None

    def iter_content(self, _size):
        yield self.data

    def close(self):
        pass


class BlockingResponse:
    status_code = 200
    headers = {}

    def __init__(self, started, release):
        self.started = started
        self.release = release

    def raise_for_status(self):
        return None

    def iter_content(self, _size):
        yield b"abc"
        self.started.set()
        self.release.wait(5)
        yield b"def"

    def close(self):
        pass


class Discovery:
    def __init__(self, peers=()):
        self.peers = list(peers)

    def get_peers(self):
        return list(self.peers)

    def confirm_contact(self, _peer_id, _host):
        pass


class ResumeSurfaceTests(unittest.TestCase):
    def make_partial(self, home, dest=None):
        payload = b"abcdef"
        dest = Path(dest or Path(home, "received"))
        entry = {
            "index": 0,
            "rel_path": "file.bin",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        metadata = {
            "kind": "clipboard",
            "clipboard_id": "clip-1",
            "op": "copy",
            "source": {
                "id": "peer-1", "name": "Peer One",
                "host": "192.0.2.10", "port": 7373,
            },
            "files": [entry],
            "total_size": len(payload),
        }
        registry = Path(home, "resumes")
        with self.assertRaises(transfer.TransferError):
            transfer.download_files(
                [entry], [("file.bin",)], dest,
                "clipboard:peer-1:clip-1",
                lambda _entry, _offset: BrokenResponse(),
                max_attempts=1, retry_delay=0,
                registry_dir=registry, metadata=metadata)
        session_id = transfer.resume_session_id(
            dest, "clipboard:peer-1:clip-1")
        session = transfer.get_resume_session(registry, session_id)
        self.assertIsNotNone(session)
        return session, registry, dest

    def test_registry_reports_progress_and_cleanup_removes_only_partial_data(self):
        with tempfile.TemporaryDirectory() as home:
            session, registry, dest = self.make_partial(home)
            self.assertEqual(3, session["received_bytes"])
            self.assertEqual(6, session["total_bytes"])
            self.assertEqual(0.5, session["progress"])
            verified = Path(dest, "already-verified.txt")
            verified.write_text("keep", encoding="utf-8")
            self.assertTrue(transfer.discard_resume_session(
                registry, session["id"]))
            self.assertEqual("keep", verified.read_text(encoding="utf-8"))
            self.assertEqual([], transfer.list_resume_sessions(registry))
            self.assertFalse(Path(dest, transfer.RESUME_DIR_NAME).exists())

    def test_availability_endpoint_requires_exact_active_share(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}):
            source = Path(home, "source.bin")
            source.write_bytes(b"shared")
            manifest = clipboard.build_manifest([str(source)])
            clipboard.set_clipboard(manifest)
            client = server.create_app().test_client()
            body = {
                "kind": "clipboard",
                "clipboard_id": manifest["clipboard_id"],
                "files": clipboard.public_manifest(manifest)["files"],
            }
            available = client.post("/api/transfer/available", json=body)
            self.assertTrue(available.get_json()["available"])
            shallow = dict(body, verify_content=False)
            with mock.patch.object(
                    transfer, "source_snapshot",
                    side_effect=AssertionError("shallow probe hashed content")):
                quick = client.post("/api/transfer/available", json=shallow)
            self.assertTrue(quick.get_json()["available"])
            body["files"][0]["sha256"] = "0" * 64
            changed = client.post("/api/transfer/available", json=body)
            self.assertFalse(changed.get_json()["available"])
            clipboard.clear_clipboard()
            gone = client.post("/api/transfer/available", json=body)
            self.assertFalse(gone.get_json()["available"])

    def test_availability_rejects_same_size_same_mtime_content_change(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}):
            source = Path(home, "source.bin")
            source.write_bytes(b"before")
            manifest = clipboard.build_manifest([str(source)])
            clipboard.set_clipboard(manifest)
            original_mtime = source.stat().st_mtime_ns
            source.write_bytes(b"after!")
            os.utime(source, ns=(original_mtime, original_mtime))
            body = {
                "kind": "clipboard",
                "clipboard_id": manifest["clipboard_id"],
                "files": clipboard.public_manifest(manifest)["files"],
            }
            response = server.create_app().test_client().post(
                "/api/transfer/available", json=body)
            self.assertFalse(response.get_json()["available"])

    def test_resume_listing_marks_live_source_available(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}):
            session, _registry, _dest = self.make_partial(home)
            peer = {"id": "peer-1", "name": "Peer One",
                    "host": "192.0.2.10", "port": 7373}
            discovery = Discovery([peer])
            with mock.patch.object(
                    server, "_peer_request",
                    return_value=ApiResponse({"available": True})) as request:
                response = server.create_app(discovery).test_client().get(
                    "/api/resumes")
            listed = response.get_json()["resumes"][0]
            self.assertEqual(session["id"], listed["id"])
            self.assertTrue(listed["available"])
            self.assertIsNone(listed["unavailable_reason"])
            self.assertNotIn("metadata", listed)
            self.assertEqual("/api/transfer/available",
                             request.call_args.args[2])
            self.assertFalse(
                request.call_args.kwargs["json"]["verify_content"])

    def test_unshared_session_cannot_resume_but_can_be_removed(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}):
            session, _registry, dest = self.make_partial(home)
            client = server.create_app(Discovery()).test_client()
            blocked = client.post(
                "/api/resumes/%s/resume" % session["id"])
            self.assertEqual(409, blocked.status_code)
            self.assertTrue(Path(dest, transfer.RESUME_DIR_NAME).exists())
            removed = client.post(
                "/api/resumes/%s/remove" % session["id"])
            self.assertEqual(200, removed.status_code)
            self.assertFalse(Path(dest, transfer.RESUME_DIR_NAME).exists())

    def test_active_transfer_cannot_be_removed(self):
        with tempfile.TemporaryDirectory() as home:
            payload = b"abcdef"
            entry = {
                "index": 0, "rel_path": "file.bin", "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            registry = Path(home, "resumes")
            dest = Path(home, "received")
            started = threading.Event()
            release = threading.Event()
            errors = []

            def run():
                try:
                    transfer.download_files(
                        [entry], [("file.bin",)], dest, "active-transfer",
                        lambda _entry, _offset: BlockingResponse(
                            started, release),
                        registry_dir=registry,
                        metadata={"kind": "clipboard", "files": [entry]})
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(started.wait(2))
            session = transfer.list_resume_sessions(registry)[0]
            with self.assertRaises(transfer.ResumeActiveError):
                transfer.discard_resume_session(registry, session["id"])
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(b"abcdef", Path(dest, "file.bin").read_bytes())

    def test_symlinked_resume_state_can_be_safely_discarded(self):
        with tempfile.TemporaryDirectory() as home:
            session, registry, _dest = self.make_partial(home)
            record_path = next(registry.glob("*.json"))
            record = json.loads(record_path.read_text(encoding="utf-8"))
            resume_dir = Path(record["resume_dir"])
            outside = Path(home, "outside")
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            shutil.rmtree(resume_dir)
            try:
                resume_dir.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest("directory symlinks unavailable: %s" % exc)
            listed = transfer.get_resume_session(registry, session["id"])
            self.assertTrue(listed["broken"])
            self.assertTrue(transfer.discard_resume_session(
                registry, session["id"]))
            self.assertFalse(resume_dir.exists())
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))
            self.assertEqual([], transfer.list_resume_sessions(registry))

    def test_bad_resumed_suffix_preserves_previous_partial_progress(self):
        with tempfile.TemporaryDirectory() as home:
            session, registry, dest = self.make_partial(home)
            payload = b"abcdef"
            checksum = hashlib.sha256(payload).hexdigest()
            entry = {
                "index": 0, "rel_path": "file.bin", "size": len(payload),
                "sha256": checksum,
            }

            def fetch(_entry, offset):
                self.assertEqual(3, offset)
                return StreamResponse(
                    b"xyz", {"Content-Range": "bytes 3-5/6",
                              "X-CrossCopy-SHA256": checksum})

            with self.assertRaises(transfer.TransferError):
                transfer.download_files(
                    [entry], [("file.bin",)], dest,
                    "clipboard:peer-1:clip-1", fetch,
                    max_attempts=1, retry_delay=0,
                    registry_dir=registry,
                    metadata=session["metadata"])
            retained = transfer.list_resume_sessions(registry)[0]
            self.assertEqual(3, retained["received_bytes"])

    def test_resume_controls_are_loopback_only(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}):
            client = server.create_app().test_client()
            response = client.get(
                "/api/resumes",
                environ_base={"REMOTE_ADDR": "192.0.2.44"})
            self.assertEqual(403, response.status_code)

    def test_resume_availability_checks_run_concurrently(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}):
            for index in range(8):
                self.make_partial(home, Path(home, "received-%d" % index))
            peer = {"id": "peer-1", "name": "Peer One",
                    "host": "192.0.2.10", "port": 7373}
            barrier = threading.Barrier(8)

            def request(*_args, **_kwargs):
                barrier.wait(2)
                return ApiResponse({"available": True})

            with mock.patch.object(server, "_peer_request",
                                   side_effect=request):
                response = server.create_app(Discovery([peer])).test_client().get(
                    "/api/resumes")
            self.assertEqual(8, len(response.get_json()["resumes"]))
            self.assertTrue(all(item["available"]
                                for item in response.get_json()["resumes"]))

    def test_macos_path_planning_separates_case_and_unicode_collisions(self):
        with tempfile.TemporaryDirectory() as dest:
            planned = offers.plan_local_paths(
                [("Folder", "a.txt"), ("folder", "b.txt")],
                Path(dest), platform="darwin")
            self.assertNotEqual(planned[0][0], planned[1][0])
            unicode_planned = offers.plan_local_paths(
                [("é", "a.txt"), ("e\u0301", "b.txt")],
                Path(dest), platform="darwin")
            self.assertNotEqual(unicode_planned[0][0], unicode_planned[1][0])

    def test_resume_api_fetches_only_missing_range_after_live_validation(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}):
            session, registry, dest = self.make_partial(home)
            peer = {"id": "peer-1", "name": "Peer One",
                    "host": "192.0.2.10", "port": 7373}
            discovery = Discovery([peer])
            paths = []

            def request(_peer, method, path, **kwargs):
                paths.append((method, path, kwargs.get("headers")))
                if path == "/api/transfer/available":
                    self.assertTrue(kwargs["json"]["verify_content"])
                    self.assertEqual(server.RESUME_VERIFY_TIMEOUT,
                                     kwargs["timeout"])
                    return ApiResponse({"available": True})
                if path.startswith("/api/clipboard/file/"):
                    self.assertEqual({"Range": "bytes=3-"},
                                     kwargs.get("headers"))
                    checksum = hashlib.sha256(b"abcdef").hexdigest()
                    return StreamResponse(
                        b"def", {"Content-Range": "bytes 3-5/6",
                                 "X-CrossCopy-SHA256": checksum})
                if path == "/api/clipboard/consumed":
                    return ApiResponse({"deleted": False})
                raise AssertionError(path)

            with mock.patch.object(server, "_peer_request",
                                   side_effect=request):
                response = server.create_app(discovery).test_client().post(
                    "/api/resumes/%s/resume" % session["id"])
            self.assertEqual(200, response.status_code)
            self.assertEqual(b"abcdef", Path(dest, "file.bin").read_bytes())
            self.assertEqual([], transfer.list_resume_sessions(registry))
            self.assertTrue(any(item[1].startswith("/api/clipboard/file/")
                                for item in paths))

    def test_cli_lists_partial_progress_and_exposes_actions(self):
        session = {
            "id": "abcdef123456",
            "source": {"name": "Peer One"},
            "received_bytes": 512,
            "total_bytes": 1024,
            "available": False,
            "dest": "/tmp/received",
        }
        output = io.StringIO()
        args = SimpleNamespace(resume=None, remove=None, json=False)
        with mock.patch.object(cli, "ensure_daemon"), \
                mock.patch.object(cli, "fetch_partial_transfers",
                                  return_value=[session]), \
                redirect_stdout(output):
            cli.cmd_transfers(args)
        text = output.getvalue()
        self.assertIn("50%", text)
        self.assertIn("unavailable", text)
        self.assertIn("ccp transfers --remove", text)
        parsed = cli.build_parser().parse_args(
            ["transfers", "--resume", "abcdef12"])
        self.assertEqual("abcdef12", parsed.resume)

    def test_web_widget_and_tray_include_resume_controls(self):
        root = Path(__file__).resolve().parents[1]
        web = (root / "crosscopy" / "webui" / "app.js").read_text(
            encoding="utf-8")
        panel = (root / "crosscopy" / "widgetui" / "widget.js").read_text(
            encoding="utf-8")
        tray = (root / "crosscopy" / "widget.py").read_text(encoding="utf-8")
        for source in (web, panel, tray):
            self.assertIn("/api/resumes", source)
            self.assertIn("Resume", source)
            self.assertIn("Remove", source)
        self.assertIn("resume.disabled = !session.available", web)
        self.assertIn("resume.disabled = !session.available", panel)


if __name__ == "__main__":
    unittest.main()
