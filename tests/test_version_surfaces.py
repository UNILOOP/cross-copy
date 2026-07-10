import configparser
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import requests

from crosscopy import __version__, cli, server


class VersionSurfaceTests(unittest.TestCase):
    def test_package_metadata_matches_runtime_version(self):
        root = Path(__file__).resolve().parents[1]
        metadata = configparser.ConfigParser()
        metadata.read(root / "setup.cfg", encoding="utf-8")
        self.assertEqual(__version__, metadata["metadata"]["version"])

    def test_cli_reports_package_version(self):
        output = io.StringIO()
        with mock.patch.object(
                cli.requests, "get",
                side_effect=requests.exceptions.ConnectionError), \
                redirect_stdout(output):
            cli.cmd_version(None)
        self.assertEqual("cross-copy %s" % __version__,
                         output.getvalue().strip())

    def test_web_status_reports_package_version(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}):
            response = server.create_app().test_client().get("/api/status")
        self.assertEqual(200, response.status_code)
        self.assertEqual(__version__, response.get_json()["version"])

    def test_web_ui_renders_status_version(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "crosscopy" / "webui" / "app.js").read_text(
            encoding="utf-8")
        html = (root / "crosscopy" / "webui" / "index.html").read_text(
            encoding="utf-8")
        self.assertIn('status.version ? "v" + status.version', script)
        self.assertIn('id="daemon-version"', html)


if __name__ == "__main__":
    unittest.main()
