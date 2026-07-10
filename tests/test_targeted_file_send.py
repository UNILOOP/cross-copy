import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crosscopy import config, offers, server, widget


class Discovery:
    def __init__(self, peers):
        self.peers = list(peers)

    def get_peers(self):
        return list(self.peers)

    def confirm_contact(self, _peer_id, _host):
        pass


class Response:
    status_code = 200
    ok = True

    def json(self):
        return {}

    def raise_for_status(self):
        return None


class MultipartResponse:
    status = 200

    def read(self):
        return b'{"offer_id":"offer-1"}'


class Connection:
    def __init__(self):
        self.headers = {}
        self.sent = []
        self.closed = False

    def putrequest(self, method, path):
        self.method = method
        self.path = path

    def putheader(self, name, value):
        self.headers[name] = value

    def endheaders(self):
        pass

    def send(self, data):
        self.sent.append(data)

    def getresponse(self):
        return MultipartResponse()

    def close(self):
        self.closed = True


class TargetedFileSendTests(unittest.TestCase):
    def setUp(self):
        self.peer = {
            "id": "peer-1",
            "name": "Other device",
            "host": "192.0.2.10",
            "port": 7373,
        }

    def test_multipart_send_stages_files_and_cleans_after_completion(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}):
            manager = offers.OffersManager()
            with mock.patch.object(offers, "manager", manager), \
                    mock.patch.object(server, "_peer_request",
                                      return_value=Response()) as push:
                client = server.create_app(Discovery([self.peer])).test_client()
                response = client.post("/api/send", data={
                    "peer_id": self.peer["id"],
                    "files": [
                        (io.BytesIO(b"first"), "report.txt"),
                        (io.BytesIO(b"second"), "report.txt"),
                    ],
                }, content_type="multipart/form-data")

            self.assertEqual(200, response.status_code)
            offer = manager.get_outgoing(response.get_json()["offer_id"])
            self.assertEqual(["report.txt", "report (1).txt"],
                             [f["rel_path"] for f in offer["files"]])
            stage = Path(offer["staging_dir"])
            self.assertTrue(stage.is_dir())
            self.assertEqual(b"first", (stage / "report.txt").read_bytes())
            pushed = push.call_args.kwargs["json"]
            self.assertNotIn("staging_dir", pushed)
            self.assertNotIn("source_path", pushed["files"][0])

            manager.set_outgoing_status(offer["offer_id"], "completed")
            self.assertFalse(stage.exists())

    def test_failed_push_removes_targeted_upload(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}):
            manager = offers.OffersManager()
            unreachable = server.PeerUnreachable(self.peer, [self.peer["host"]])
            with mock.patch.object(offers, "manager", manager), \
                    mock.patch.object(server, "_peer_request",
                                      side_effect=unreachable):
                response = server.create_app(
                    Discovery([self.peer])).test_client().post(
                        "/api/send", data={
                            "peer_id": self.peer["id"],
                            "files": (io.BytesIO(b"payload"), "file.bin"),
                        }, content_type="multipart/form-data")
            self.assertEqual(502, response.status_code)
            root = config.offer_staging_dir()
            self.assertEqual([], list(root.iterdir()))

    def test_unreadable_json_path_is_an_actionable_client_error(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}):
            manager = mock.Mock()
            manager.create_outgoing.side_effect = PermissionError(
                "Operation not permitted")
            with mock.patch.object(offers, "manager", manager):
                response = server.create_app(
                    Discovery([self.peer])).test_client().post(
                        "/api/send", json={
                            "peer_id": self.peer["id"],
                            "paths": ["/protected/file.txt"],
                        })
            self.assertEqual(403, response.status_code)
            self.assertIn("Choose it through the widget",
                          response.get_json()["error"])

    def test_widget_streams_picker_files_instead_of_sending_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory, "picked.txt")
            selected.write_bytes(b"picked")
            connection = Connection()
            with mock.patch.object(widget.http.client, "HTTPConnection",
                                   return_value=connection) as connect:
                ok, payload = widget.api_post_files(
                    "/api/send", self.peer["id"], [str(selected)])
            self.assertTrue(ok)
            self.assertEqual("offer-1", payload["offer_id"])
            self.assertEqual("POST", connection.method)
            self.assertEqual("/api/send", connection.path)
            body = b"".join(connection.sent)
            self.assertEqual(len(body), int(connection.headers["Content-Length"]))
            self.assertIn(b'name="peer_id"', body)
            self.assertIn(self.peer["id"].encode("ascii"), body)
            self.assertIn(b'filename="picked.txt"', body)
            self.assertIn(b"picked", body)
            self.assertTrue(connection.closed)
            connect.assert_called_once_with("127.0.0.1", 7373, timeout=3600)

    def test_main_web_ui_has_per_device_native_file_picker(self):
        app_js = Path("crosscopy/webui/app.js").read_text(encoding="utf-8")
        self.assertIn('function doSendFiles(peer, fileInput, btn)', app_js)
        self.assertIn('form.append("peer_id", peer.id)', app_js)
        self.assertIn('fileInput.type = "file"', app_js)
        self.assertIn('fileInput.multiple = true', app_js)
        self.assertIn('el("label", "btn file-btn", "Send files")', app_js)


if __name__ == "__main__":
    unittest.main()
