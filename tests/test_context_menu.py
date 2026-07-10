import os
import plistlib
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from crosscopy import cli, contextmenu


class Response:
    def __init__(self, payload=None, status=200):
        self.payload = payload or {}
        self.status_code = status
        self.ok = status < 400

    def json(self):
        return self.payload


class FakeRegistryKey:
    def __init__(self, registry, path):
        self.registry = registry
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def CreateKey(self, _root, path):
        return FakeRegistryKey(self, path)

    def SetValueEx(self, key, name, _reserved, _kind, value):
        self.values[(key.path, name)] = value


class ContextMenuTests(unittest.TestCase):
    def test_macos_installs_two_finder_quick_actions_and_uninstalls(self):
        with tempfile.TemporaryDirectory() as home:
            paths = contextmenu.install(platform="darwin", home=home)
            self.assertEqual(2, len(paths))
            documents = [plistlib.loads(Path(path).read_bytes())
                         for path in paths]
            commands = [document["actions"][0]["action"]
                        ["ActionParameters"]["COMMAND_STRING"]
                        for document in documents]
            self.assertTrue(any("context share-all" in item
                                for item in commands))
            self.assertTrue(any("context share-to" in item
                                for item in commands))
            for document in documents:
                metadata = document["workflowMetaData"]
                self.assertEqual("com.apple.finder",
                                 metadata["serviceApplicationBundleID"])
                self.assertEqual(1, document["actions"][0]["action"]
                                 ["ActionParameters"]["inputMethod"])
            removed = contextmenu.uninstall(platform="darwin", home=home)
            self.assertEqual(2, len(removed))
            self.assertTrue(all(not Path(path).exists() for path in paths))

    def test_linux_installs_major_file_manager_scripts_and_kde_menu(self):
        with tempfile.TemporaryDirectory() as home:
            paths = contextmenu.install(platform="linux", home=home)
            self.assertEqual(8, len(paths))
            scripts = [Path(path) for path in paths
                       if Path(path).name != "cross-copy.desktop"]
            self.assertEqual(6, len(scripts))
            for script in scripts:
                text = script.read_text(encoding="utf-8")
                self.assertIn("context share-", text)
                self.assertIn('"$@"', text)
                self.assertTrue(script.stat().st_mode & stat.S_IXUSR)
            menus = [Path(path) for path in paths
                     if Path(path).name == "cross-copy.desktop"]
            self.assertEqual(2, len(menus))
            menu = menus[0].read_text(encoding="utf-8")
            self.assertIn("Actions=CrossCopyAll;CrossCopyDevice;", menu)
            self.assertIn("context share-all %F", menu)
            self.assertIn("context share-to %F", menu)

            unrelated = scripts[0].parent / "keep-me"
            unrelated.write_text("keep", encoding="utf-8")
            contextmenu.uninstall(platform="linux", home=home)
            self.assertEqual("keep", unrelated.read_text(encoding="utf-8"))
            self.assertTrue(all(not menu.exists() for menu in menus))

    def test_windows_menu_command_is_hidden_launcher_multiselect(self):
        launcher = r"C:\Program Files\Cross Copy\Cross Copy.exe"
        command = contextmenu._windows_command("share-to", launcher)
        self.assertIn('"%s"' % launcher, command)
        self.assertIn("-m crosscopy.cli context share-to", command)
        self.assertTrue(command.endswith(" %*"))
        self.assertIn("AllFilesystemObjects", contextmenu.WINDOWS_MENU_KEY)

    def test_windows_registration_builds_cascading_per_user_menu(self):
        registry = FakeWinreg()
        launcher = r"C:\CrossCopy\Cross Copy.exe"
        with mock.patch.dict(sys.modules, {"winreg": registry}), \
                mock.patch("crosscopy.windows.make_windows_launcher",
                           return_value=launcher):
            locations = contextmenu.install(platform="win32", home="C:\\Users\\A")
        root = contextmenu.WINDOWS_MENU_KEY
        self.assertEqual("Cross Copy", registry.values[(root, "MUIVerb")])
        self.assertEqual("Player",
                         registry.values[(root, "MultiSelectModel")])
        commands = [value for (path, name), value in registry.values.items()
                    if path.endswith("\\command") and name is None]
        self.assertEqual(2, len(commands))
        self.assertTrue(all("ExtendedSubCommandsKey\\shell" in path
                            for (path, name) in registry.values
                            if path.endswith("\\command") and name is None))
        self.assertTrue(any("context share-all" in item for item in commands))
        self.assertTrue(any("context share-to" in item for item in commands))
        self.assertEqual(["registry:HKCU\\" + root], locations)

    def test_selected_paths_supports_arguments_and_file_manager_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory, "one.txt")
            second = Path(directory, "two.txt")
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")
            self.assertEqual(
                [str(first.resolve())],
                contextmenu.selected_paths([str(first), str(first) + ".missing"]))
            selected = contextmenu.selected_paths([], {
                "NAUTILUS_SCRIPT_SELECTED_FILE_PATHS":
                    "%s\n%s\n" % (first, second),
            })
            self.assertEqual([str(first.resolve()), str(second.resolve())],
                             selected)

    def test_context_share_all_sets_network_clipboard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "report.pdf")
            path.write_bytes(b"pdf")
            args = SimpleNamespace(
                context_action="share-all", paths=[str(path)], to=None)
            with mock.patch.object(cli, "ensure_daemon"), \
                    mock.patch.object(cli, "api_post",
                                      return_value=Response({"total_size": 3})) \
                    as post, \
                    mock.patch.object(cli, "_context_notify") as notify:
                cli.cmd_context(args)
            self.assertEqual("/api/copy", post.call_args.args[0])
            self.assertEqual([str(path.resolve())],
                             post.call_args.args[1]["paths"])
            notify.assert_called_once()

    def test_context_share_to_uses_native_choice_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "photo.jpg")
            path.write_bytes(b"jpg")
            peer = {"id": "peer-1", "name": "Laptop"}
            args = SimpleNamespace(
                context_action="share-to", paths=[str(path)], to=None)
            with mock.patch.object(cli, "ensure_daemon"), \
                    mock.patch.object(cli, "api_get",
                                      return_value=Response({"peers": [peer]})), \
                    mock.patch.object(contextmenu, "choose_peer",
                                      return_value=peer), \
                    mock.patch.object(cli, "api_post",
                                      return_value=Response({"offer_id": "o1"})) \
                    as post, \
                    mock.patch.object(cli, "_context_notify"):
                cli.cmd_context(args)
            self.assertEqual("/api/send", post.call_args.args[0])
            self.assertEqual("peer-1", post.call_args.args[1]["peer_id"])

    def test_cli_and_installers_expose_context_lifecycle(self):
        parsed = cli.build_parser().parse_args(
            ["context", "share-to", "--to", "Laptop", "/tmp/file"])
        self.assertEqual("share-to", parsed.context_action)
        self.assertEqual("Laptop", parsed.to)
        root = Path(__file__).resolve().parents[1]
        install_sh = (root / "install.sh").read_text(encoding="utf-8")
        uninstall_sh = (root / "uninstall.sh").read_text(encoding="utf-8")
        install_ps = (root / "install.ps1").read_text(encoding="utf-8")
        uninstall_ps = (root / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn('"$CCP" context install', install_sh)
        self.assertIn('"$CCP_BIN" context uninstall', uninstall_sh)
        self.assertIn("$CcpExe context install", install_ps)
        self.assertIn("$CcpExe context uninstall", uninstall_ps)


if __name__ == "__main__":
    unittest.main()
