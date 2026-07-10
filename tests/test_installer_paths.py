import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
UNINSTALLER = ROOT / "uninstall.sh"


@unittest.skipIf(sys.platform == "win32", "Unix shell installer tests")
class UnixInstallerPathTests(unittest.TestCase):
    def run_path_setup(self, home, shell):
        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "CROSSCOPY_SHELL": shell,
            "CROSSCOPY_NO_SHELL_RELOAD": "1",
        })
        subprocess.run(
            ["bash", str(INSTALLER), "--path-only"],
            env=env, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)

    def test_zsh_path_setup_is_persistent_and_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            self.run_path_setup(home, "zsh")
            self.run_path_setup(home, "zsh")
            profile = (home / ".zshrc").read_text(encoding="utf-8")
        self.assertIn('$HOME/.local/bin', profile)
        self.assertEqual(1, profile.count("# >>> cross-copy PATH >>>"))

    def test_bash_updates_interactive_and_login_profiles(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            login = home / ".bash_profile"
            login.write_text("# existing login settings\n", encoding="utf-8")
            self.run_path_setup(home, "bash")
            interactive = (home / ".bashrc").read_text(encoding="utf-8")
            login_text = login.read_text(encoding="utf-8")
        self.assertIn('$HOME/.local/bin', interactive)
        self.assertIn("# existing login settings", login_text)
        self.assertIn('$HOME/.local/bin', login_text)

    def test_fish_uses_a_conf_d_file(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            profile = home / ".config" / "fish" / "conf.d" / "cross-copy.fish"
            profile.parent.mkdir(parents=True)
            profile.write_text("set -gx KEEP_THIS 1\n", encoding="utf-8")
            self.run_path_setup(home, "fish")
            text = profile.read_text(encoding="utf-8")
        self.assertIn("set -gx KEEP_THIS 1", text)
        self.assertIn("contains", text)
        self.assertIn('$HOME/.local/bin', text)

    def test_csh_updates_cshrc(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            self.run_path_setup(home, "tcsh")
            text = (home / ".cshrc").read_text(encoding="utf-8")
        self.assertIn('set path = ( "$HOME/.local/bin" $path )', text)

    def test_uninstaller_removes_only_managed_path_block(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            profile = home / ".zshrc"
            profile.write_text("export KEEP_THIS=1\n", encoding="utf-8")
            self.run_path_setup(home, "zsh")
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "PATH": os.environ.get("PATH", ""),
                "PIPX_HOME": str(home / ".local" / "share" / "pipx"),
                "PIPX_BIN_DIR": str(home / ".local" / "bin"),
            })
            subprocess.run(
                ["bash", str(UNINSTALLER)], env=env, check=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            text = profile.read_text(encoding="utf-8")
        self.assertIn("export KEEP_THIS=1", text)
        self.assertNotIn("cross-copy PATH", text)
        self.assertNotIn('$HOME/.local/bin', text)

    def test_interactive_install_reloads_login_shell(self):
        install = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('exec "$shell_path" -l < /dev/tty', install)
        self.assertGreaterEqual(install.count("start_refreshed_shell"), 3)


class WindowsInstallerPathTests(unittest.TestCase):
    def test_windows_installer_updates_current_and_persistent_path(self):
        install = (ROOT / "install.ps1").read_text(encoding="utf-8")
        uninstall = (ROOT / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn('SetEnvironmentVariable("Path", $NewPath, "User")',
                      install)
        self.assertIn('$env:Path = "$BinDir;$env:Path"', install)
        self.assertIn("Publish-EnvironmentChange", install)
        self.assertIn('SetEnvironmentVariable("Path", ($Kept -join ";"), "User")',
                      uninstall)
        self.assertIn("Publish-EnvironmentChange", uninstall)

if __name__ == "__main__":
    unittest.main()
