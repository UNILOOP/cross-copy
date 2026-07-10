import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from crosscopy import (cli, config, daemon, notify, offers, popup, widget,
                       windows, winnotify)
from crosscopy import __version__


class WindowsSupportTests(unittest.TestCase):
    def test_startup_launcher_is_hidden_and_preserves_environment(self):
        with tempfile.TemporaryDirectory() as root:
            interpreter_dir = os.path.join(root, "venv", "Scripts")
            os.makedirs(interpreter_dir)
            python = os.path.join(interpreter_dir, "python.exe")
            pythonw = os.path.join(interpreter_dir, "pythonw.exe")
            Path(pythonw).touch()
            env = {
                "CROSSCOPY_HOME": os.path.join(root, "Cross Copy Data"),
                "CROSSCOPY_PORT": "7474",
            }
            winreg = mock.MagicMock()
            winreg.HKEY_CURRENT_USER = object()
            winreg.REG_SZ = 1
            registry_key = mock.MagicMock()
            winreg.CreateKey.return_value = registry_key
            with mock.patch.object(windows.sys, "platform", "win32"), \
                    mock.patch.object(windows.sys, "executable", python), \
                    mock.patch.object(windows.sys, "prefix", root), \
                    mock.patch.object(windows.sys, "base_prefix",
                                      os.path.join(root, "base")), \
                    mock.patch.object(windows, "brand_windows_executable"), \
                    mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.dict(sys.modules, {"winreg": winreg}):
                path = windows.write_startup_launcher(
                    "daemon", "crosscopy.daemon")

            text = Path(path).read_text(encoding="utf-8")
            self.assertTrue(path.endswith("Cross Copy Daemon.pyw"))
            self.assertIn("runpy.run_module", text)
            self.assertIn("crosscopy.daemon", text)
            self.assertIn("CROSSCOPY_HOME", text)
            self.assertIn("Cross Copy Data", text)
            self.assertIn("CROSSCOPY_PORT", text)
            self.assertIn("7474", text)
            winreg.SetValueEx.assert_called_once()
            registry_command = winreg.SetValueEx.call_args.args[-1]
            self.assertIn("Cross Copy.exe", registry_command)
            self.assertIn("Cross Copy Daemon.pyw", registry_command)

    def test_version_resource_identifies_cross_copy(self):
        resource = windows.windows_version_resource(__version__)
        length, value_length, value_type = windows.struct.unpack_from(
            "<HHH", resource)
        self.assertEqual(len(resource), length)
        self.assertEqual(52, value_length)
        self.assertEqual(0, value_type)
        self.assertIn(windows.struct.pack("<I", 0xFEEF04BD), resource)
        for value in ("Cross Copy", "UNILOOP LLC", __version__):
            self.assertIn(value.encode("utf-16le"), resource)

    def test_windows_launcher_is_branded_once_per_version(self):
        with tempfile.TemporaryDirectory() as root:
            scripts = os.path.join(root, "venv", "Scripts")
            os.makedirs(scripts)
            pythonw = os.path.join(scripts, "pythonw.exe")
            Path(pythonw).write_bytes(b"pythonw")
            with mock.patch.object(windows.sys, "platform", "win32"), \
                    mock.patch.object(windows.sys, "executable", pythonw), \
                    mock.patch.object(windows.sys, "prefix", scripts), \
                    mock.patch.object(windows.sys, "base_prefix", root), \
                    mock.patch.object(windows, "brand_windows_executable") \
                    as brand:
                first = windows.make_windows_launcher()
                second = windows.make_windows_launcher()
            self.assertEqual(os.path.join(scripts, "Cross Copy.exe"), first)
            self.assertEqual(first, second)
            brand.assert_called_once_with(first, __version__)
            self.assertEqual(
                __version__, Path(first + ".crosscopy-version").read_text(
                    encoding="ascii").strip())

    def test_locked_windows_launcher_gets_versioned_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            scripts = os.path.join(root, "venv", "Scripts")
            os.makedirs(scripts)
            pythonw = os.path.join(scripts, "pythonw.exe")
            Path(pythonw).write_bytes(b"pythonw")

            def brand(path, _version):
                if os.path.basename(path) == "Cross Copy.exe":
                    raise PermissionError("locked")

            with mock.patch.object(windows.sys, "platform", "win32"), \
                    mock.patch.object(windows.sys, "executable", pythonw), \
                    mock.patch.object(windows.sys, "prefix", scripts), \
                    mock.patch.object(windows.sys, "base_prefix", root), \
                    mock.patch.object(windows, "brand_windows_executable",
                                      side_effect=brand):
                launcher = windows.make_windows_launcher()
            self.assertEqual(
                os.path.join(scripts, "Cross Copy %s.exe" % __version__),
                launcher)

    @unittest.skipUnless(sys.platform == "win32", "native Windows check")
    def test_native_windows_launcher_version_info(self):
        with tempfile.TemporaryDirectory() as root:
            source = windows.pythonw_executable()
            target = os.path.join(root, "Cross Copy.exe")
            windows.shutil.copy2(source, target)
            windows.brand_windows_executable(target, __version__)
            env = dict(os.environ, CROSSCOPY_TEST_EXE=target)
            command = (
                "$v=[Diagnostics.FileVersionInfo]::GetVersionInfo("
                "$env:CROSSCOPY_TEST_EXE); "
                "@($v.FileDescription,$v.ProductName,$v.ProductVersion) "
                "| ConvertTo-Json -Compress")
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                check=True, capture_output=True, text=True, env=env)
            self.assertEqual(
                ["Cross Copy", "Cross Copy", __version__],
                json.loads(result.stdout))

    def test_cli_daemon_spawn_uses_branded_windows_launcher(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(cli.sys, "platform", "win32"), \
                mock.patch.object(cli, "crosscopy_home", return_value=home), \
                mock.patch("crosscopy.windows.make_windows_launcher",
                           return_value=r"C:\App\Cross Copy.exe"), \
                mock.patch.object(cli, "background_popen_kwargs",
                                  return_value={}), \
                mock.patch.object(cli.subprocess, "Popen") as popen:
            cli.spawn_daemon()
        self.assertEqual(
            [r"C:\App\Cross Copy.exe", "-m", "crosscopy.daemon"],
            popen.call_args.args[0])

    def test_widget_daemon_spawn_uses_branded_windows_launcher(self):
        info = {"name": "Windows PC"}
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.object(widget.sys, "platform", "win32"), \
                mock.patch.object(widget, "crosscopy_home",
                                  return_value=home), \
                mock.patch.object(widget, "ping", side_effect=[None, info]), \
                mock.patch.object(widget.time, "sleep"), \
                mock.patch("crosscopy.windows.make_windows_launcher",
                           return_value=r"C:\App\Cross Copy.exe"), \
                mock.patch.object(widget, "background_popen_kwargs",
                                  return_value={}), \
                mock.patch.object(widget.subprocess, "Popen") as popen:
            self.assertEqual(info, widget.ensure_daemon())
        self.assertEqual(
            [r"C:\App\Cross Copy.exe", "-m", "crosscopy.daemon"],
            popen.call_args.args[0])

    def test_refresh_startup_commands_only_updates_existing_entries(self):
        winreg = mock.MagicMock()
        winreg.HKEY_CURRENT_USER = object()
        winreg.KEY_QUERY_VALUE = 1
        winreg.KEY_SET_VALUE = 2
        winreg.REG_SZ = 1
        key = winreg.OpenKey.return_value.__enter__.return_value
        winreg.QueryValueEx.side_effect = [("old", 1), FileNotFoundError()]
        with mock.patch.object(windows.sys, "platform", "win32"), \
                mock.patch.dict(sys.modules, {"winreg": winreg}):
            windows.refresh_registered_startup_commands(
                r"C:\App\Cross Copy 0.5.2.exe")
        winreg.SetValueEx.assert_called_once()
        command = winreg.SetValueEx.call_args.args[-1]
        self.assertIn("Cross Copy 0.5.2.exe", command)
        self.assertIn("Cross Copy Daemon.pyw", command)

    def test_windows_background_process_uses_no_console(self):
        with mock.patch.object(windows.sys, "platform", "win32"), \
                mock.patch.object(windows.subprocess,
                                  "CREATE_NEW_PROCESS_GROUP", 0x200,
                                  create=True), \
                mock.patch.object(windows.subprocess,
                                  "CREATE_NO_WINDOW", 0x8000000,
                                  create=True):
            kwargs = windows.background_popen_kwargs()
        self.assertEqual({"creationflags": 0x8000200}, kwargs)

    def test_background_launcher_uses_console_python_for_pip(self):
        with tempfile.TemporaryDirectory() as root:
            launcher = os.path.join(root, "Cross Copy.exe")
            python = os.path.join(root, "python.exe")
            Path(python).touch()
            with mock.patch.object(windows.sys, "platform", "win32"):
                self.assertEqual(
                    python, windows.console_python_executable(launcher))

    def test_cli_uses_windows_pid_probe_instead_of_os_kill(self):
        with mock.patch.object(cli.sys, "platform", "win32"), \
                mock.patch("crosscopy.windows.pid_alive",
                           return_value=True) as probe, \
                mock.patch.object(cli.os, "kill") as kill:
            self.assertTrue(cli.pid_alive(1234))
        probe.assert_called_once_with(1234)
        kill.assert_not_called()

    def test_daemon_mutex_is_scoped_by_home_and_port(self):
        first = windows.daemon_mutex_name(r"C:\Users\A\.crosscopy", 7373)
        same = windows.daemon_mutex_name(r"c:\users\a\.crosscopy", 7373)
        other_port = windows.daemon_mutex_name(
            r"C:\Users\A\.crosscopy", 7474)
        self.assertEqual(first, same)
        self.assertNotEqual(first, other_port)

    def test_widget_mutex_is_scoped_by_home(self):
        first = windows.widget_mutex_name(r"C:\Users\A\.crosscopy")
        same = windows.widget_mutex_name(r"c:\users\a\.crosscopy")
        other = windows.widget_mutex_name(r"C:\Users\B\.crosscopy")
        self.assertEqual(first, same)
        self.assertNotEqual(first, other)

    def test_stale_windows_daemon_pid_is_never_force_killed(self):
        with mock.patch.object(cli.sys, "platform", "win32"), \
                mock.patch.object(cli, "read_daemon_json",
                                  return_value={"pid": 4242, "port": 7373}), \
                mock.patch.object(cli, "probe_orphan_daemon",
                                  return_value=(False, None)), \
                mock.patch.object(cli, "pid_alive", return_value=True), \
                mock.patch.object(cli, "terminate_pid") as terminate, \
                mock.patch.object(cli, "remove_daemon_pidfile") as remove:
            cli.cmd_daemon(SimpleNamespace(action="stop"))
        terminate.assert_not_called()
        remove.assert_called()

    def test_verified_windows_daemon_pid_is_stopped_and_cleaned(self):
        with mock.patch.object(cli.sys, "platform", "win32"), \
                mock.patch.object(cli, "read_daemon_json",
                                  return_value={"pid": 4242, "port": 7373}), \
                mock.patch.object(cli, "probe_orphan_daemon",
                                  return_value=(True, 4242)), \
                mock.patch.object(cli, "pid_alive", return_value=True), \
                mock.patch.object(cli, "terminate_pid", return_value=None) \
                as terminate, \
                mock.patch.object(cli, "remove_daemon_pidfile") as remove:
            cli.stop_running_daemon()
        terminate.assert_called_once_with(4242)
        remove.assert_called_once_with()

    def test_hung_windows_daemon_uses_creation_time_identity(self):
        info = {"pid": 4343, "port": 7373, "start_time": 987654321}
        with mock.patch.object(cli.sys, "platform", "win32"), \
                mock.patch.object(cli, "read_daemon_json", return_value=info), \
                mock.patch.object(cli, "probe_orphan_daemon",
                                  return_value=(False, None)), \
                mock.patch.object(cli, "pid_alive", return_value=True), \
                mock.patch("crosscopy.windows.pid_matches_start_time",
                           return_value=True) as matches, \
                mock.patch.object(cli, "terminate_pid", return_value=None) \
                as terminate, \
                mock.patch.object(cli, "remove_daemon_pidfile"):
            cli.stop_running_daemon()
        matches.assert_called_once_with(4343, 987654321)
        terminate.assert_called_once_with(4343)

    def test_windows_daemon_pidfile_records_creation_time(self):
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"CROSSCOPY_HOME": home}), \
                mock.patch.object(config.sys, "platform", "win32"), \
                mock.patch("crosscopy.windows.process_start_time",
                           return_value=123456789):
            config.write_daemon_info(7373, pid=4444)
            saved = json.loads(Path(home, "daemon.json").read_text(
                encoding="utf-8"))
        self.assertEqual(
            {"pid": 4444, "port": 7373, "start_time": 123456789}, saved)

    def test_stale_windows_widget_pid_is_never_force_killed(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, "widget.json").write_text(
                json.dumps({"pid": 5252, "start_time": 100}),
                encoding="utf-8")
            with mock.patch.object(cli.sys, "platform", "win32"), \
                    mock.patch.object(cli, "crosscopy_home",
                                      return_value=home), \
                    mock.patch.object(cli, "pid_alive", return_value=True), \
                    mock.patch("crosscopy.windows.pid_matches_start_time",
                               return_value=False), \
                    mock.patch.object(cli, "run_cmd") as run:
                cli.stop_running_widget()
            run.assert_not_called()
            self.assertFalse(Path(home, "widget.json").exists())

    def test_native_notification_helper_is_spawned_hidden(self):
        with mock.patch.object(notify.sys, "platform", "win32"), \
                mock.patch("crosscopy.windows.pythonw_executable",
                           return_value=r"C:\Python\pythonw.exe"), \
                mock.patch("crosscopy.windows.background_popen_kwargs",
                           return_value={"creationflags": 123}), \
                mock.patch.object(notify.subprocess, "Popen") as popen:
            shown = notify._notify_windows("Cross Copy", "Transfer complete")
        self.assertTrue(shown)
        argv = popen.call_args.args[0]
        self.assertEqual(r"C:\Python\pythonw.exe", argv[0])
        self.assertEqual(["-m", "crosscopy.winnotify"], argv[1:3])
        self.assertEqual(123, popen.call_args.kwargs["creationflags"])

    def test_notification_payload_respects_win32_buffers(self):
        payload = winnotify.notification_payload("t" * 100, "b" * 400)
        self.assertEqual(63, len(payload["title"]))
        self.assertEqual(255, len(payload["body"]))
        self.assertEqual(6000, payload["timeout_ms"])

    def test_windows_platform_is_advertised_to_peers(self):
        with mock.patch.object(config.sys, "platform", "win32"):
            self.assertEqual("win32", config.platform_name())

    def test_linux_filenames_are_made_safe_for_windows(self):
        parts = ("folder. ", "CON.txt", "report?.txt")
        self.assertEqual(("folder", "_CON.txt", "report_.txt"),
                         offers.local_path_parts(parts, platform="win32"))
        self.assertEqual(parts, offers.local_path_parts(parts,
                                                        platform="linux"))

    def test_windows_manifest_planning_separates_sanitized_collisions(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [
                ("a?", "one.txt"),
                ("a*", "two.txt"),
                ("foo",),
                ("foo.", "bar.txt"),
            ]
            planned = offers.plan_local_paths(
                paths, Path(root), platform="win32")
        self.assertNotEqual(planned[0][0].casefold(),
                            planned[1][0].casefold())
        self.assertNotEqual(planned[2][0].casefold(),
                            planned[3][0].casefold())
        self.assertEqual(("a_", "one.txt"), planned[0])
        self.assertEqual(("a_ (1)", "two.txt"), planned[1])

    def test_windows_redirected_text_is_utf8(self):
        class RedirectedStdout:
            def __init__(self):
                self.buffer = io.BytesIO()

            def isatty(self):
                return False

        stdout = RedirectedStdout()
        with mock.patch.object(cli.sys, "platform", "win32"), \
                mock.patch.object(cli.sys, "stdout", stdout):
            cli.write_verbatim_stdout("Zażółć 🪟")
        self.assertEqual("Zażółć 🪟\n".encode("utf-8"),
                         stdout.buffer.getvalue())

    @unittest.skipUnless(sys.platform == "win32", "native Windows check")
    def test_windows_redirected_text_subprocess_is_utf8(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root, "output.txt")
            code = ("from crosscopy.cli import write_verbatim_stdout; "
                    "write_verbatim_stdout('Zażółć 🪟')")
            with output.open("wb") as handle:
                subprocess.run([sys.executable, "-c", code], stdout=handle,
                               check=True)
            self.assertEqual("Zażółć 🪟\n".encode("utf-8"),
                             output.read_bytes())

    def test_tk_clipboard_is_flushed_before_popup_exits(self):
        root = mock.Mock()
        popup.set_tk_clipboard(root, "received text")
        root.clipboard_clear.assert_called_once_with()
        root.clipboard_append.assert_called_once_with("received text")
        root.update.assert_called_once_with()

    def test_windows_browser_install_locations_are_detected(self):
        with tempfile.TemporaryDirectory() as root:
            edge = os.path.join(root, "Microsoft", "Edge", "Application",
                                "msedge.exe")
            os.makedirs(os.path.dirname(edge))
            Path(edge).touch()
            with mock.patch.dict(os.environ, {"PROGRAMFILES": root},
                                 clear=True):
                self.assertEqual([edge], widget.windows_app_browsers())

    def test_daemon_install_uses_windows_startup_entry(self):
        info = {"name": "windows-pc", "version": "0.5.0"}
        with mock.patch.object(cli.sys, "platform", "win32"), \
                mock.patch.object(cli, "stop_running_daemon") as stop, \
                mock.patch.object(cli, "wait_for_ping", return_value=info), \
                mock.patch.object(cli, "warn_version_mismatch"), \
                mock.patch("crosscopy.windows.write_startup_launcher",
                           return_value=r"C:\Data\Cross Copy Daemon.pyw"), \
                mock.patch("crosscopy.windows.launch_startup_entry") as launch:
            cli.cmd_daemon_install()
        stop.assert_called_once_with()
        launch.assert_called_once_with("daemon")

    def test_widget_install_uses_windows_startup_entry(self):
        with tempfile.TemporaryDirectory() as home:
            pidfile = os.path.join(home, "widget.json")

            def launch(_kind):
                Path(pidfile).write_text(json.dumps({"pid": 123}),
                                         encoding="utf-8")

            with mock.patch.object(cli.sys, "platform", "win32"), \
                    mock.patch.object(cli, "crosscopy_home",
                                      return_value=home), \
                    mock.patch.object(cli, "ensure_daemon"), \
                    mock.patch.object(cli, "stop_running_widget"), \
                    mock.patch.object(cli.time, "sleep"), \
                    mock.patch("crosscopy.windows.write_startup_launcher",
                               return_value=r"C:\Data\Cross Copy Widget.pyw"), \
                    mock.patch("crosscopy.windows.launch_startup_entry",
                               side_effect=launch):
                cli.cmd_widget_install()

    def test_windows_install_scripts_cover_full_lifecycle(self):
        root = Path(__file__).resolve().parents[1]
        install = (root / "install.ps1").read_text(encoding="utf-8")
        uninstall = (root / "uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn('$Pip install --quiet "${Source}[widget]"', install)
        self.assertIn("import tkinter", install)
        self.assertIn("daemon install", install)
        self.assertIn("widget install", install)
        self.assertIn("daemon uninstall", uninstall)
        self.assertIn("widget uninstall", uninstall)

    def test_auto_update_restart_refreshes_widget_before_exec(self):
        original_state = dict(daemon._cleanup_state)
        daemon._cleanup_state.update(
            {"done": False, "discovery": None, "updater": None})
        try:
            with mock.patch("crosscopy.cli.restart_widget_after_update") \
                    as restart_widget, \
                    mock.patch.object(daemon.os, "sysconf", return_value=3), \
                    mock.patch.object(daemon.os, "chdir"), \
                    mock.patch.object(daemon.os, "execv",
                                      side_effect=RuntimeError("exec")):
                with self.assertRaisesRegex(RuntimeError, "exec"):
                    daemon._restart_daemon()
            restart_widget.assert_called_once_with(quiet=True)
        finally:
            daemon._cleanup_state.clear()
            daemon._cleanup_state.update(original_state)

    def test_windows_auto_update_reexecs_branded_new_version(self):
        original_state = dict(daemon._cleanup_state)
        daemon._cleanup_state.update(
            {"done": False, "discovery": None, "updater": None})
        launcher = r"C:\App\Cross Copy 0.5.3.exe"
        try:
            with mock.patch.object(daemon.sys, "platform", "win32"), \
                    mock.patch("importlib.metadata.version",
                               return_value="0.5.3"), \
                    mock.patch("crosscopy.windows.make_windows_launcher",
                               return_value=launcher) as make_launcher, \
                    mock.patch(
                        "crosscopy.windows.refresh_registered_startup_commands") \
                    as refresh, \
                    mock.patch("crosscopy.windows.release_daemon_mutex"), \
                    mock.patch("crosscopy.cli.restart_widget_after_update"), \
                    mock.patch.object(daemon.os, "sysconf", return_value=3), \
                    mock.patch.object(daemon.os, "chdir"), \
                    mock.patch.object(daemon.os, "execv",
                                      side_effect=RuntimeError("exec")) as execv:
                with self.assertRaisesRegex(RuntimeError, "exec"):
                    daemon._restart_daemon()
            make_launcher.assert_called_once_with("0.5.3")
            refresh.assert_called_once_with(launcher)
            execv.assert_called_once_with(
                launcher, [launcher, "-m", "crosscopy.daemon"])
        finally:
            daemon._cleanup_state.clear()
            daemon._cleanup_state.update(original_state)

    def test_legacy_windows_daemon_switches_identity_before_binding(self):
        current = os.path.join("C:\\App", "Cross Copy.exe")
        replacement = os.path.join("C:\\App", "Cross Copy 0.5.2.exe")
        with mock.patch.object(daemon.sys, "platform", "win32"), \
                mock.patch.object(daemon.sys, "executable", current), \
                mock.patch("crosscopy.windows.make_windows_launcher",
                           return_value=replacement), \
                mock.patch(
                    "crosscopy.windows.refresh_registered_startup_commands") \
                as refresh, \
                mock.patch.object(daemon.log, "warning"), \
                mock.patch.object(daemon.os, "execv",
                                  side_effect=RuntimeError("exec")) as execv:
            # The helper logs and returns if execv itself fails.
            daemon._ensure_windows_executable_identity()
        refresh.assert_called_once_with(replacement)
        execv.assert_called_once_with(
            replacement, [replacement, "-m", "crosscopy.daemon"])


if __name__ == "__main__":
    unittest.main()
