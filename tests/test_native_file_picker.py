import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from crosscopy import filepicker


class NativeFilePickerTests(unittest.TestCase):
    def test_macos_uses_nsopenpanel_with_multiple_selection(self):
        panel = mock.MagicMock()
        panel.runModal.return_value = 1
        panel.URLs.return_value = [
            SimpleNamespace(path=lambda: "/Users/me/one.txt"),
            SimpleNamespace(path=lambda: "/Users/me/two.txt"),
        ]
        appkit = SimpleNamespace(
            NSModalResponseOK=1,
            NSApplication=SimpleNamespace(
                sharedApplication=mock.Mock(return_value=mock.MagicMock())),
            NSOpenPanel=SimpleNamespace(
                openPanel=mock.Mock(return_value=panel)),
        )
        with mock.patch.dict(sys.modules, {"AppKit": appkit}):
            paths = filepicker._macos_files()
        self.assertEqual(
            ["/Users/me/one.txt", "/Users/me/two.txt"], paths)
        panel.setCanChooseFiles_.assert_called_once_with(True)
        panel.setCanChooseDirectories_.assert_called_once_with(False)
        panel.setAllowsMultipleSelection_.assert_called_once_with(True)

    def test_macos_cancel_returns_empty_selection(self):
        panel = mock.MagicMock()
        panel.runModal.return_value = 0
        appkit = SimpleNamespace(
            NSModalResponseOK=1,
            NSApplication=SimpleNamespace(
                sharedApplication=mock.Mock(return_value=mock.MagicMock())),
            NSOpenPanel=SimpleNamespace(
                openPanel=mock.Mock(return_value=panel)),
        )
        with mock.patch.dict(sys.modules, {"AppKit": appkit}):
            self.assertEqual([], filepicker._macos_files())

    def test_windows_uses_modern_multiselect_common_dialog(self):
        selected = [r"C:\Users\me\one.txt", r"C:\Users\me\two.txt"]
        result = SimpleNamespace(
            returncode=0, stdout=json.dumps(selected), stderr="")
        with mock.patch.object(filepicker.sys, "platform", "win32"), \
                mock.patch.object(filepicker.shutil, "which",
                                  return_value=r"C:\Windows\powershell.exe"), \
                mock.patch.object(filepicker.subprocess, "run",
                                  return_value=result) as run, \
                mock.patch("crosscopy.windows.background_popen_kwargs",
                           return_value={"creationflags": 123}):
            self.assertEqual(selected, filepicker._windows_files())
        argv = run.call_args.args[0]
        self.assertEqual(r"C:\Windows\powershell.exe", argv[0])
        self.assertIn("-STA", argv)
        script = argv[-1]
        self.assertIn("System.Windows.Forms.OpenFileDialog", script)
        self.assertIn("$dialog.Multiselect=$true", script)
        self.assertIn("$dialog.AutoUpgradeEnabled=$true", script)
        self.assertEqual(123, run.call_args.kwargs["creationflags"])

    def test_windows_cancel_is_not_reported_as_unavailable(self):
        result = SimpleNamespace(returncode=0, stdout="[]", stderr="")
        with mock.patch.object(filepicker.shutil, "which",
                               return_value="powershell.exe"), \
                mock.patch.object(filepicker.subprocess, "run",
                                  return_value=result):
            self.assertEqual([], filepicker._windows_files())

    def test_kde_prefers_kdialog(self):
        tools = {"kdialog": "/usr/bin/kdialog", "zenity": "/usr/bin/zenity"}
        result = SimpleNamespace(
            returncode=0, stdout="/home/me/one.txt\n/home/me/two.txt\n",
            stderr="")
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}), \
                mock.patch.object(filepicker.shutil, "which",
                                  side_effect=lambda name: tools.get(name)), \
                mock.patch.object(filepicker.subprocess, "run",
                                  return_value=result) as run:
            paths = filepicker._linux_files()
        self.assertEqual(
            ["/home/me/one.txt", "/home/me/two.txt"], paths)
        self.assertEqual("/usr/bin/kdialog", run.call_args.args[0][0])

    def test_gnome_uses_gtk_picker_with_lossless_separator(self):
        tools = {"zenity": "/usr/bin/zenity"}
        result = SimpleNamespace(
            returncode=0,
            stdout="/home/me/one.txt\x1e/home/me/two.txt\n", stderr="")
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}), \
                mock.patch.object(filepicker.shutil, "which",
                                  side_effect=lambda name: tools.get(name)), \
                mock.patch.object(filepicker.subprocess, "run",
                                  return_value=result) as run:
            paths = filepicker._linux_files()
        self.assertEqual(
            ["/home/me/one.txt", "/home/me/two.txt"], paths)
        self.assertEqual("/usr/bin/zenity", run.call_args.args[0][0])
        self.assertIn("--separator=\x1e", run.call_args.args[0])

    def test_tk_is_only_used_when_native_picker_is_unavailable(self):
        with mock.patch.object(filepicker.sys, "platform", "linux"), \
                mock.patch.object(filepicker, "_linux_files",
                                  return_value=None), \
                mock.patch.object(filepicker, "_tk_files",
                                  return_value=["fallback.txt"]) as fallback:
            self.assertEqual(["fallback.txt"], filepicker.pick_files())
        fallback.assert_called_once_with(filepicker.PICKER_TITLE)


if __name__ == "__main__":
    unittest.main()
