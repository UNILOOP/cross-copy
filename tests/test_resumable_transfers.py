import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import requests
from werkzeug.serving import make_server

from crosscopy import clipboard, offers, server, transfer


def digest(data):
    return hashlib.sha256(data).hexdigest()


class FakeResponse:
    def __init__(self, status, chunks=(), headers=None):
        self.status_code = status
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError("HTTP %d" % self.status_code)
            error.response = self
            raise error

    def iter_content(self, _chunk_size):
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class ResumableTransferTests(unittest.TestCase):
    def entry(self, data, name="file.bin"):
        return {
            "index": 0,
            "rel_path": name,
            "size": len(data),
            "sha256": digest(data),
        }

    def test_manifest_advertises_checksum_but_hides_local_metadata(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": directory}):
            source = Path(directory, "shared.bin")
            source.write_bytes(b"checksum me")
            manifest = clipboard.build_manifest([str(source)])
            public = clipboard.public_manifest(manifest)
        self.assertEqual(digest(b"checksum me"), manifest["files"][0]["sha256"])
        self.assertIn("mtime_ns", manifest["files"][0])
        self.assertEqual(digest(b"checksum me"), public["files"][0]["sha256"])
        self.assertNotIn("mtime_ns", public["files"][0])
        self.assertNotIn("source_path", public["files"][0])

    def test_existing_local_manifest_is_upgraded_with_checksum(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": directory}):
            source = Path(directory, "legacy.bin")
            source.write_bytes(b"legacy clipboard")
            legacy = {
                "clipboard_id": "legacy-id",
                "kind": "files",
                "op": "copy",
                "files": [{
                    "index": 0,
                    "rel_path": "legacy.bin",
                    "size": source.stat().st_size,
                    "source_path": str(source),
                }],
            }
            Path(directory, "clipboard.json").write_text(
                json.dumps(legacy), encoding="utf-8")
            upgraded = clipboard.load_clipboard()
            persisted = json.loads(
                Path(directory, "clipboard.json").read_text(encoding="utf-8"))
        self.assertEqual(digest(b"legacy clipboard"),
                         upgraded["files"][0]["sha256"])
        self.assertEqual(upgraded["files"][0]["sha256"],
                         persisted["files"][0]["sha256"])

    def test_unverifiable_legacy_sender_is_rejected(self):
        entry = {"index": 0, "rel_path": "old.bin", "size": 3}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(transfer.IntegrityError,
                                        "update Cross Copy on the sender"):
                transfer.download_files(
                    [entry], [("old.bin",)], Path(directory), "legacy",
                    lambda _entry, _offset: None)
            self.assertFalse(
                Path(directory, transfer.RESUME_DIR_NAME).exists())

    def test_broken_stream_resumes_from_last_written_byte(self):
        data = b"abcdef"
        offsets = []

        def fetch(_entry, offset):
            offsets.append(offset)
            if offset == 0:
                return FakeResponse(200, [
                    b"abc", requests.ConnectionError("link dropped")])
            return FakeResponse(
                206, [data[offset:]],
                {"Content-Range": "bytes %d-%d/%d"
                 % (offset, len(data) - 1, len(data))})

        with tempfile.TemporaryDirectory() as directory:
            paths, total = transfer.download_files(
                [self.entry(data)], [("file.bin",)], Path(directory),
                "transfer-one", fetch, retry_delay=0)
            self.assertEqual(data, paths[0].read_bytes())
            self.assertFalse(Path(directory, transfer.RESUME_DIR_NAME).exists())
        self.assertEqual([0, 3], offsets)
        self.assertEqual(len(data), total)

    def test_partial_progress_survives_a_separate_retry_call(self):
        data = b"persistent resume"
        entry = self.entry(data)

        def broken(_entry, offset):
            self.assertEqual(0, offset)
            return FakeResponse(200, [
                data[:5], requests.ConnectionError("offline")])

        offsets = []

        def resumed(_entry, offset):
            offsets.append(offset)
            return FakeResponse(
                206, [data[offset:]],
                {"Content-Range": "bytes %d-%d/%d"
                 % (offset, len(data) - 1, len(data))})

        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory)
            with self.assertRaisesRegex(transfer.TransferError,
                                        "partial progress was saved"):
                transfer.download_files(
                    [entry], [("file.bin",)], dest, "persistent", broken,
                    max_attempts=1, retry_delay=0)
            self.assertFalse(Path(dest, "file.bin").exists())
            part_files = list(
                Path(dest, transfer.RESUME_DIR_NAME).rglob("*.part"))
            self.assertEqual(1, len(part_files))
            self.assertEqual(data[:5], part_files[0].read_bytes())

            paths, _total = transfer.download_files(
                [entry], [("file.bin",)], dest, "persistent", resumed,
                max_attempts=1, retry_delay=0)
            self.assertEqual(data, paths[0].read_bytes())
        self.assertEqual([5], offsets)

    def test_checksum_mismatch_discards_corruption_and_retries_from_zero(self):
        data = b"correct bytes"
        offsets = []

        def fetch(_entry, offset):
            offsets.append(offset)
            payload = (b"x" * len(data)) if len(offsets) == 1 else data
            return FakeResponse(200, [payload], {
                "X-CrossCopy-SHA256": digest(data),
            })

        with tempfile.TemporaryDirectory() as directory:
            paths, _total = transfer.download_files(
                [self.entry(data)], [("file.bin",)], Path(directory),
                "corruption", fetch, retry_delay=0)
            self.assertEqual(data, paths[0].read_bytes())
        self.assertEqual([0, 0], offsets)

    def test_retry_reuses_files_completed_before_later_failure(self):
        first = b"already complete"
        second = b"resume this one"
        entries = [self.entry(first, "one.bin"), {
            "index": 1,
            "rel_path": "two.bin",
            "size": len(second),
            "sha256": digest(second),
        }]

        def interrupted(entry, offset):
            if entry["index"] == 0:
                return FakeResponse(200, [first])
            return FakeResponse(200, [
                second[:4], requests.ConnectionError("offline")])

        retry_calls = []

        def resumed(entry, offset):
            retry_calls.append((entry["index"], offset))
            self.assertEqual(1, entry["index"])
            return FakeResponse(
                206, [second[offset:]],
                {"Content-Range": "bytes %d-%d/%d"
                 % (offset, len(second) - 1, len(second))})

        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory)
            with self.assertRaises(transfer.TransferError):
                transfer.download_files(
                    entries, [("one.bin",), ("two.bin",)], dest,
                    "multi-file", interrupted, max_attempts=1,
                    retry_delay=0)
            self.assertEqual(first, Path(dest, "one.bin").read_bytes())

            paths, total = transfer.download_files(
                entries, [("one (1).bin",), ("two.bin",)], dest,
                "multi-file", resumed, max_attempts=1, retry_delay=0)
            self.assertEqual(["one.bin", "two.bin"],
                             [path.name for path in paths])
            self.assertEqual(first, paths[0].read_bytes())
            self.assertEqual(second, paths[1].read_bytes())
            self.assertEqual(len(first) + len(second), total)
        self.assertEqual([(1, 4)], retry_calls)

    def test_http_file_endpoint_supports_ranges_and_checksum_headers(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": directory}):
            source = Path(directory, "source.bin")
            source.write_bytes(b"0123456789")
            manifest = clipboard.build_manifest([str(source)])
            clipboard.set_clipboard(manifest)
            client = server.create_app().test_client()
            response = client.get(
                "/api/clipboard/file/%s/0" % manifest["clipboard_id"],
                headers={"Range": "bytes=4-"})
            status = response.status_code
            body = response.get_data()
            headers = dict(response.headers)
            response.close()
        self.assertEqual(206, status)
        self.assertEqual(b"456789", body)
        self.assertEqual("bytes 4-9/10", headers["Content-Range"])
        self.assertEqual("bytes", headers["Accept-Ranges"])
        self.assertEqual(digest(b"0123456789"),
                         headers["X-CrossCopy-SHA256"])

    def test_real_http_stream_downloads_and_verifies_manifest(self):
        payload = (b"streamed payload " * 8192) + b"done"
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": directory}):
            source = Path(directory, "source.bin")
            source.write_bytes(payload)
            manifest = clipboard.build_manifest([str(source)])
            clipboard.set_clipboard(manifest)
            public_entries = clipboard.public_manifest(manifest)["files"]
            destination = Path(directory, "received")

            def seed_partial(_entry, offset):
                self.assertEqual(0, offset)
                return FakeResponse(200, [
                    payload[:4096], requests.ConnectionError("offline")])

            with self.assertRaises(transfer.TransferError):
                transfer.download_files(
                    public_entries, [("received.bin",)], destination,
                    "real-http", seed_partial, max_attempts=1,
                    retry_delay=0)

            app = server.create_app()
            try:
                with redirect_stderr(io.StringIO()):
                    httpd = make_server("127.0.0.1", 0, app, threaded=True)
            except (OSError, SystemExit) as exc:
                self.skipTest("loopback sockets unavailable: %s" % exc)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                base = "http://127.0.0.1:%d" % httpd.server_port
                offsets = []

                def fetch(entry, offset):
                    offsets.append(offset)
                    headers = {"Range": "bytes=%d-" % offset} if offset else {}
                    return requests.get(
                        "%s/api/clipboard/file/%s/%d" % (
                            base, manifest["clipboard_id"], entry["index"]),
                        headers=headers, stream=True, timeout=5)

                paths, total = transfer.download_files(
                    public_entries, [("received.bin",)], destination,
                    "real-http", fetch,
                    retry_delay=0)
                received = paths[0].read_bytes()
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()
        self.assertEqual(payload, received)
        self.assertEqual(len(payload), total)
        self.assertEqual([4096], offsets)

    def test_file_offer_without_checksum_is_rejected(self):
        body = {
            "offer_id": "legacy-offer",
            "from": {"id": "old-peer", "name": "Old peer"},
            "sender_port": 7373,
            "kind": "files",
            "files": [{"index": 0, "rel_path": "old.bin", "size": 3}],
        }
        response = server.create_app().test_client().post(
            "/api/offer", json=body)
        self.assertEqual(400, response.status_code)
        self.assertIn("update Cross Copy on the sender",
                      response.get_json()["error"])

    def test_source_changed_after_sharing_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": directory}):
            source = Path(directory, "source.bin")
            source.write_bytes(b"original")
            manifest = clipboard.build_manifest([str(source)])
            clipboard.set_clipboard(manifest)
            source.write_bytes(b"modified")
            old = manifest["files"][0]["mtime_ns"]
            os.utime(source, ns=(old + 1000000000, old + 1000000000))
            response = server.create_app().test_client().get(
                "/api/clipboard/file/%s/0" % manifest["clipboard_id"])
        self.assertEqual(409, response.status_code)
        self.assertIn("changed", response.get_json()["error"])

    def test_interrupted_offer_remains_pending_for_resume(self):
        offer = {
            "offer_id": "resume-offer",
            "from": {"id": "sender", "name": "Sender"},
            "sender_host": "127.0.0.1",
            "sender_port": 7373,
            "kind": "files",
            "status": "pending",
            "files": [self.entry(b"payload")],
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(offers.manager, "get_incoming",
                                  return_value=offer), \
                mock.patch.object(offers.manager, "set_incoming_status") \
                as set_status, \
                mock.patch.object(offers, "report_result",
                                  return_value=True) as report, \
                mock.patch.object(
                    offers, "pull_files",
                    side_effect=transfer.TransferError("offline; saved")), \
                mock.patch.object(server.log, "warning"):
            response = server.create_app().test_client().post(
                "/api/offers/resume-offer/accept", json={"dest": directory})
        self.assertEqual(502, response.status_code)
        self.assertIn("Accept again to resume", response.get_json()["error"])
        self.assertEqual(
            [mock.call("resume-offer", "accepted"),
             mock.call("resume-offer", "pending")],
            set_status.call_args_list)
        report.assert_called_once_with(offer, "accepted")


if __name__ == "__main__":
    unittest.main()
