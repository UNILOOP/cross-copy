import os
import plistlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

from crosscopy import cli
from crosscopy import macos
from crosscopy.macos import make_app_icon

try:
    import PIL  # noqa: F401
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


@unittest.skipUnless(HAS_PILLOW, "macOS widget extra is not installed")
class MacOSAppTests(unittest.TestCase):
    def test_app_icon_has_standard_size_and_transparency(self):
        icon = make_app_icon()
        self.assertEqual((1024, 1024), icon.size)
        self.assertEqual("RGBA", icon.mode)
        self.assertEqual(0, icon.getpixel((0, 0))[3])
        self.assertGreater(icon.getpixel((512, 512))[3], 0)

    def test_widget_bundle_has_cross_copy_identity(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(cli, "crosscopy_home", return_value=home):
                executable = cli.make_macos_widget_app()

            contents = os.path.join(home, "Cross Copy.app", "Contents")
            with open(os.path.join(contents, "Info.plist"), "rb") as f:
                info = plistlib.load(f)
            with open(executable) as f:
                launcher = f.read()

            self.assertEqual("Cross Copy", info["CFBundleDisplayName"])
            self.assertEqual("com.crosscopy.widget",
                             info["CFBundleIdentifier"])
            self.assertTrue(info["LSUIElement"])
            self.assertEqual("AppIcon.icns", info["CFBundleIconFile"])
            self.assertTrue(os.path.getsize(
                os.path.join(contents, "Resources", "AppIcon.icns")))
            self.assertTrue(os.stat(executable).st_mode & stat.S_IXUSR)
            self.assertIn("-m crosscopy.widget", launcher)

    def test_runtime_uses_accessory_policy_and_cross_copy_name(self):
        info = {"CFBundleName": "Python"}
        app = mock.Mock()
        native_icon = object()

        appkit = mock.Mock()
        appkit.NSApplicationActivationPolicyAccessory = 1
        appkit.NSApplication.sharedApplication.return_value = app
        appkit.NSImage.alloc.return_value.initWithData_.return_value = native_icon

        foundation = mock.Mock()
        foundation.NSBundle.mainBundle.return_value.infoDictionary.return_value = info
        foundation.NSData.dataWithBytes_length_.return_value = object()

        modules = {"AppKit": appkit, "Foundation": foundation}
        with mock.patch.object(macos.sys, "platform", "darwin"), \
                mock.patch.dict(sys.modules, modules):
            configured = macos.configure_application(make_app_icon(64))

        self.assertTrue(configured)
        self.assertEqual("Cross Copy", info["CFBundleName"])
        self.assertEqual("Cross Copy", info["CFBundleDisplayName"])
        self.assertTrue(info["LSUIElement"])
        app.setActivationPolicy_.assert_called_once_with(1)
        app.setApplicationIconImage_.assert_called_once_with(native_icon)


if __name__ == "__main__":
    unittest.main()
