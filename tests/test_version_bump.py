import runpy
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bump_version.py"
BUMP = runpy.run_path(str(SCRIPT))


class VersionBumpScriptTests(unittest.TestCase):
    def make_project(self, root, versions=("1.2.3", "1.2.3", "1.2.3")):
        root = Path(root)
        (root / "crosscopy").mkdir()
        (root / "crosscopy" / "__init__.py").write_text(
            '__version__ = "%s"\n' % versions[0], encoding="utf-8")
        (root / "setup.cfg").write_text(
            "[metadata]\nversion = %s\n" % versions[1], encoding="utf-8")
        (root / "README.md").write_text(
            "Cross Copy version %s uses a trusted-LAN model.\n" % versions[2],
            encoding="utf-8")
        return root

    def test_patch_bump_updates_every_managed_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_project(directory)
            current, target, paths = BUMP["bump_version"](root, "patch")
            self.assertEqual(("1.2.3", "1.2.4"), (current, target))
            self.assertEqual(3, len(paths))
            for relative in paths:
                self.assertIn(
                    "1.2.4", (root / relative).read_text(encoding="utf-8"))

    def test_minor_and_major_reset_lower_components(self):
        self.assertEqual("1.3.0", BUMP["next_version"]("1.2.3", "minor"))
        self.assertEqual("2.0.0", BUMP["next_version"]("1.2.3", "major"))

    def test_explicit_version_must_move_forward(self):
        with self.assertRaises(BUMP["VersionBumpError"]):
            BUMP["next_version"]("1.2.3", "1.2.3")
        with self.assertRaises(BUMP["VersionBumpError"]):
            BUMP["next_version"]("1.2.3", "1.2.2")

    def test_inconsistent_files_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_project(
                directory, versions=("1.2.3", "1.2.4", "1.2.3"))
            before = (root / "crosscopy" / "__init__.py").read_text(
                encoding="utf-8")
            with self.assertRaises(BUMP["VersionBumpError"]):
                BUMP["bump_version"](root, "patch")
            self.assertEqual(
                before, (root / "crosscopy" / "__init__.py").read_text(
                    encoding="utf-8"))

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_project(directory)
            before = {
                relative: (root / relative).read_text(encoding="utf-8")
                for relative, _pattern in BUMP["MANAGED_VERSIONS"]
            }
            current, target, _paths = BUMP["bump_version"](
                root, "patch", dry_run=True)
            self.assertEqual(("1.2.3", "1.2.4"), (current, target))
            for relative, content in before.items():
                self.assertEqual(
                    content, (root / relative).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
