"""cross-copy CLI (`ccp`).

Thin HTTP client that talks to the local cross-copy daemon per SPEC.md.
Only stdlib + argparse + requests; no other crosscopy imports (except a
guarded __version__).
"""

import argparse
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
import webbrowser

import requests

try:
    from crosscopy import __version__
except Exception:  # pragma: no cover - package may not define it yet
    __version__ = "0.1.0"

DEFAULT_PORT = 7373
PING_TIMEOUT = 2.0
START_WAIT_SECS = 5.0
INSTALL_WAIT_SECS = 8.0  # service managers can be slower to start us
MAX_TEXT_BYTES = 1024 * 1024  # 1 MB, matches the server-side limit

SYSTEMD_UNIT_NAME = "cross-copy"
LAUNCHD_LABEL = "com.crosscopy.daemon"


# ---------------------------------------------------------------------------
# Paths / environment helpers
# ---------------------------------------------------------------------------

def crosscopy_home():
    return os.environ.get("CROSSCOPY_HOME") or os.path.expanduser("~/.crosscopy")


def default_port():
    try:
        return int(os.environ.get("CROSSCOPY_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def read_daemon_json():
    """Return {"pid": int, "port": int} from daemon.json, or None."""
    path = os.path.join(crosscopy_home(), "daemon.json")
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and "port" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def daemon_port():
    info = read_daemon_json()
    if info:
        try:
            return int(info["port"])
        except (TypeError, ValueError):
            pass
    return default_port()


def base_url():
    return "http://127.0.0.1:%d" % daemon_port()


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def err(msg):
    print(msg, file=sys.stderr)


def die(msg, code=1):
    err(msg)
    sys.exit(code)


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d B" % int(n)
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%d B" % int(n)


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def text_preview(text, limit=40):
    """Single-line preview of a text clipboard; newlines shown as ␤."""
    one_line = (text.replace("\r\n", "␤").replace("\n", "␤")
                .replace("\r", "␤").replace("\t", " "))
    if len(one_line) > limit:
        one_line = one_line[:limit] + "…"
    return one_line


def clipboard_summary(manifest):
    """Short summary like '3 files, 2.1 MB', 'text (52 chars) "..."' or '-'."""
    if not manifest:
        return "-"
    if manifest.get("kind") == "text":
        text = manifest.get("text") or ""
        summary = 'text (%s) "%s"' % (plural(len(text), "char"),
                                      text_preview(text))
    else:
        files = manifest.get("files") or []
        total = manifest.get("total_size", 0)
        summary = "%s, %s" % (plural(len(files), "file"), human_size(total))
    if manifest.get("op") == "move":
        summary += " (move)"
    return summary


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def ping(port=None, timeout=PING_TIMEOUT):
    """GET /api/ping on localhost; return response dict or None."""
    port = port or daemon_port()
    try:
        r = requests.get("http://127.0.0.1:%d/api/ping" % port, timeout=timeout)
        if r.ok:
            return r.json()
    except (requests.RequestException, ValueError):
        pass
    return None


def api_error_message(resp):
    try:
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("error"):
            return payload["error"]
    except ValueError:
        pass
    return "daemon returned HTTP %d" % resp.status_code


def api_get(path, timeout=10, **kwargs):
    try:
        return requests.get(base_url() + path, timeout=timeout, **kwargs)
    except requests.exceptions.ConnectionError:
        die("Could not reach the cross-copy daemon on port %d. "
            "Try 'ccp daemon start'." % daemon_port())
    except requests.exceptions.Timeout:
        die("Request to the cross-copy daemon timed out.")


def api_post(path, body=None, timeout=30, **kwargs):
    try:
        return requests.post(base_url() + path, json=body, timeout=timeout, **kwargs)
    except requests.exceptions.ConnectionError:
        die("Could not reach the cross-copy daemon on port %d. "
            "Try 'ccp daemon start'." % daemon_port())
    except requests.exceptions.Timeout:
        die("Request to the cross-copy daemon timed out.")


# ---------------------------------------------------------------------------
# Daemon lifecycle
# ---------------------------------------------------------------------------

def spawn_daemon():
    """Start a detached background daemon; stdout/err appended to daemon.log."""
    home = crosscopy_home()
    os.makedirs(home, exist_ok=True)
    log = open(os.path.join(home, "daemon.log"), "ab")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "crosscopy.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    finally:
        log.close()


def wait_for_ping(wait=START_WAIT_SECS):
    deadline = time.time() + wait
    while time.time() < deadline:
        info = ping(timeout=0.5)
        if info:
            return info
        time.sleep(0.2)
    return None


def ensure_daemon():
    """Ping the local daemon; transparently start it if it's down."""
    info = ping()
    if info:
        return info
    print("Starting cross-copy daemon...")
    spawn_daemon()
    info = wait_for_ping()
    if not info:
        die("Failed to start the cross-copy daemon. "
            "Check %s for details." % os.path.join(crosscopy_home(), "daemon.log"))
    return info


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def terminate_pid(pid, wait=5.0):
    """SIGTERM a pid and wait for it to exit. Returns an error string or None."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return "Failed to stop daemon (pid %d): %s" % (pid, e)
    deadline = time.time() + wait
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.1)
    if pid_alive(pid):
        return "Daemon (pid %d) did not exit after SIGTERM." % pid
    return None


def stop_running_daemon():
    """Best-effort stop of a running daemon (via daemon.json). Dies on failure."""
    info = read_daemon_json()
    pid = (info or {}).get("pid")
    try:
        pid = int(pid) if pid else None
    except (TypeError, ValueError):
        pid = None
    if pid and pid_alive(pid):
        print("Stopping the running daemon (pid %d) so the service can take over..."
              % pid)
        error = terminate_pid(pid)
        if error:
            die(error + "\nStop it manually, then re-run 'ccp daemon install'.")


def run_cmd(cmd):
    """Run a command, capturing output. Returns (returncode, output);
    returncode -1 if the command could not be executed at all."""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
        return proc.returncode, (proc.stdout or "").strip()
    except OSError as e:
        return -1, str(e)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def read_stdin_text():
    """Read piped stdin as UTF-8 text (up to 1 MB)."""
    data = sys.stdin.buffer.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        die("stdin is larger than 1 MB — the text clipboard is capped at 1 MB.\n"
            "Copy it as a file instead: ccp copy <path>")
    return data.decode("utf-8", errors="replace")


def do_copy_text(text, op):
    """POST text to /api/copy and print the result."""
    if not text:
        die("Nothing to copy: the text is empty.")
    ensure_daemon()
    r = api_post("/api/copy", {"text": text, "op": op})
    if not r.ok:
        die("Copy failed: %s" % api_error_message(r))
    verb = "Copied" if op == "copy" else "Cut"
    print("📝 %s text (%s) to the network clipboard"
          % (verb, plural(len(text), "char")))
    print("   Run 'ccp paste' on your other machine.")
    if op == "move":
        print("   The clipboard here will clear after the text is pasted.")


def do_copy_files(paths, op):
    """POST file paths to /api/copy and print the result."""
    ensure_daemon()
    r = api_post("/api/copy", {"paths": paths, "op": op})
    if not r.ok:
        die("Copy failed: %s" % api_error_message(r))
    manifest = r.json()
    size = human_size(manifest.get("total_size", 0))
    verb = "Copied" if op == "copy" else "Cut"
    print("📋 %s %s (%s) to the network clipboard"
          % (verb, plural(len(paths), "item"), size))
    print("   Run 'ccp paste' on your other machine.")
    if op == "move":
        print("   Files will be removed from this machine after they are pasted.")


def cmd_copy(args, op):
    items = args.paths

    if not items:
        if sys.stdin.isatty():
            die("ccp %s: missing paths or text.\n"
                "Usage: ccp %s <path...>  |  ccp %s --text <words...>  |  "
                "echo hi | ccp %s" % (op, op, op, op), code=2)
        do_copy_text(read_stdin_text(), op)
        return

    if getattr(args, "text", False):
        do_copy_text(" ".join(items), op)
        return

    # Path-vs-text detection: all args exist => files, none exist => text,
    # mixed => refuse and ask the user to disambiguate.
    paths = []
    missing = []
    for p in items:
        ap = os.path.abspath(p)
        if os.path.exists(ap):
            paths.append(ap)
        else:
            missing.append(p)

    if not missing:
        do_copy_files(paths, op)
    elif not paths:
        do_copy_text(" ".join(items), op)
    else:
        die("Mixed arguments: some exist as paths, but these do not: %s\n"
            "To send everything as text, use: ccp %s --text ...\n"
            "Otherwise fix the path and try again." % (", ".join(missing), op))


def resolve_peer(name_or_id):
    """Resolve a --from value to a peer id via /api/peers."""
    r = api_get("/api/peers", timeout=10)
    if not r.ok:
        die("Could not list devices: %s" % api_error_message(r))
    peers = r.json().get("peers", [])
    matches = [p for p in peers if p.get("id") == name_or_id]
    if not matches:
        matches = [p for p in peers
                   if (p.get("name") or "").lower() == name_or_id.lower()]
    if not matches:
        known = ", ".join(sorted(p.get("name", "?") for p in peers)) or "(none)"
        die("No device named '%s'. Known devices: %s" % (name_or_id, known))
    if len(matches) > 1:
        die("Multiple devices match '%s'; use the device id instead "
            "(see 'ccp devices')." % name_or_id)
    return matches[0]["id"]


def cmd_paste(args):
    dest = os.path.abspath(args.dir or os.getcwd())
    ensure_daemon()

    body = {"dest": dest}
    if getattr(args, "from_", None):
        body["peer_id"] = resolve_peer(args.from_)

    r = api_post("/api/paste", body, timeout=600)
    if r.status_code == 404:
        die("Nothing to paste — no device on the network has anything copied.\n"
            "Run 'ccp copy <file>' on another machine first.")
    if not r.ok:
        die("Paste failed: %s" % api_error_message(r))
    result = r.json()

    if args.json:
        print(json.dumps(result, indent=2))
        return

    src = result.get("from") or {}
    src_name = src.get("name") or src.get("id") or "unknown device"

    if result.get("kind") == "text":
        # Verbatim to stdout so `ccp paste > out.txt` / `ccp paste | pbcopy`
        # work; the info line goes to stderr.
        text = result.get("text", "")
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        err("📥 from %s" % src_name)
        if result.get("op") == "move":
            err("   Source clipboard was cleared on %s." % src_name)
        return

    files = result.get("files_written", [])
    print("📥 Pasted %s (%s) from %s"
          % (plural(len(files), "file"),
             human_size(result.get("total_bytes", 0)),
             src_name))
    for f in files:
        print("   %s" % f)
    if result.get("op") == "move":
        print("   Source files were removed from %s."
              % (src.get("name") or "the source machine"))


def cmd_devices(args):
    ensure_daemon()
    r = api_get("/api/peers", params={"with_clipboard": "1"}, timeout=30)
    if not r.ok:
        die("Could not list devices: %s" % api_error_message(r))
    peers = r.json().get("peers", [])

    if args.json:
        print(json.dumps(peers, indent=2))
        return

    if not peers:
        print("No other cross-copy devices found on the network.")
        print("If mDNS is blocked on your network, add a device manually:")
        print("   ccp add <ip>")
        return

    rows = []
    for p in peers:
        host = "%s:%s" % (p.get("host", "?"), p.get("port", DEFAULT_PORT))
        rows.append((
            p.get("name") or "?",
            host,
            p.get("platform") or "?",
            p.get("source") or "?",
            clipboard_summary(p.get("clipboard")),
        ))
    headers = ("NAME", "HOST", "PLATFORM", "SOURCE", "CLIPBOARD")
    widths = [max(len(headers[i]), max(len(r[i]) for r in rows))
              for i in range(len(headers))]
    fmt = "  ".join("%%-%ds" % w for w in widths)
    print(fmt % headers)
    for row in rows:
        print(fmt % row)


def cmd_status(args):
    info = read_daemon_json()
    ensure_daemon()
    r = api_get("/api/status")
    if not r.ok:
        die("Could not get status: %s" % api_error_message(r))
    status = r.json()

    if args.json:
        if info and info.get("pid"):
            status["pid"] = info["pid"]
        print(json.dumps(status, indent=2))
        return

    pid = (info or {}).get("pid")
    line = "✅ Daemon running on port %s" % status.get("port", daemon_port())
    if pid:
        line += " (pid %s)" % pid
    print(line)
    print("🖥  This device: %s [%s]"
          % (status.get("name", "?"), status.get("platform", "?")))
    manifest = status.get("clipboard")
    if manifest:
        print("📋 Clipboard: %s" % clipboard_summary(manifest))
        for f in (manifest.get("files") or [])[:10]:
            print("   %s (%s)" % (f.get("rel_path"), human_size(f.get("size", 0))))
        extra = len(manifest.get("files") or []) - 10
        if extra > 0:
            print("   ... and %s more" % plural(extra, "file"))
    else:
        print("📋 Clipboard: empty")


def cmd_clear(args):
    ensure_daemon()
    r = api_post("/api/clipboard/clear")
    if not r.ok:
        die("Clear failed: %s" % api_error_message(r))
    print("✅ Clipboard cleared.")


def cmd_add(args):
    ensure_daemon()
    r = api_post("/api/peers/add", {"host": args.host, "port": args.port})
    if r.status_code == 502:
        die("Could not reach %s:%d — is cross-copy running there?"
            % (args.host, args.port))
    if not r.ok:
        die("Failed to add device: %s" % api_error_message(r))
    peer = r.json()
    print("✅ Added device %s (%s:%s, %s)"
          % (peer.get("name", args.host), args.host,
             peer.get("port", args.port), peer.get("platform", "?")))


def cmd_name(args):
    new_name = args.newname
    if ping():
        try:
            r = requests.post(base_url() + "/api/name",
                              json={"name": new_name}, timeout=10)
        except requests.RequestException:
            r = None
        if r is not None and r.ok:
            print("✅ Device name set to '%s'." % new_name)
            return
        if r is not None and r.status_code != 404:
            die("Rename failed: %s" % api_error_message(r))
        # Daemon has no /api/name endpoint; fall back to config.json.
        _write_name_to_config(new_name)
        print("✅ Device name set to '%s' in config.json." % new_name)
        print("   Restart the daemon to apply: ccp daemon stop && ccp daemon start")
    else:
        _write_name_to_config(new_name)
        print("✅ Device name set to '%s'." % new_name)


def _write_name_to_config(new_name):
    home = crosscopy_home()
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, "config.json")
    config = {}
    try:
        with open(path) as f:
            config = json.load(f)
    except (OSError, ValueError):
        pass
    if not isinstance(config, dict):
        config = {}
    config["device_name"] = new_name
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Daemon autostart (systemd user unit / launchd agent)
# ---------------------------------------------------------------------------

def systemd_unit_path():
    return os.path.expanduser("~/.config/systemd/user/%s.service"
                              % SYSTEMD_UNIT_NAME)


def launchd_plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCHD_LABEL)


def install_systemd():
    manual = ("Set up autostart manually, or just run 'ccp daemon start' "
              "after login.")
    if not shutil.which("systemctl"):
        die("systemd (systemctl) was not found on this system, so autostart "
            "cannot be set up.\n" + manual)

    env_lines = ""
    for var in ("CROSSCOPY_PORT", "CROSSCOPY_HOME"):
        value = os.environ.get(var)
        if value:
            env_lines += "Environment=%s=%s\n" % (var, value)

    unit = (
        "[Unit]\n"
        "Description=cross-copy network clipboard daemon\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "ExecStart=%s -m crosscopy.daemon\n"
        "Restart=on-failure\n"
        "%s"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ) % (sys.executable, env_lines)

    unit_path = systemd_unit_path()
    os.makedirs(os.path.dirname(unit_path), exist_ok=True)
    with open(unit_path, "w") as f:
        f.write(unit)
    print("Wrote %s" % unit_path)

    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME]):
        code, output = run_cmd(cmd)
        if code != 0:
            die("'%s' failed%s\n%s" % (" ".join(cmd),
                                       (":\n%s" % output) if output else ".",
                                       manual))


def install_launchd():
    home = crosscopy_home()
    os.makedirs(home, exist_ok=True)
    log_path = os.path.join(home, "daemon.log")

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [sys.executable, "-m", "crosscopy.daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    env = {var: os.environ[var]
           for var in ("CROSSCOPY_PORT", "CROSSCOPY_HOME")
           if os.environ.get(var)}
    if env:
        plist["EnvironmentVariables"] = env

    plist_path = launchd_plist_path()
    os.makedirs(os.path.dirname(plist_path), exist_ok=True)
    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)
    print("Wrote %s" % plist_path)

    uid = os.getuid()
    # Unload any previously-loaded copy first (ignore failures).
    run_cmd(["launchctl", "bootout", "gui/%d" % uid, plist_path])
    code, output = run_cmd(["launchctl", "bootstrap", "gui/%d" % uid,
                            plist_path])
    if code != 0:
        # Older macOS: fall back to launchctl load -w.
        code, output = run_cmd(["launchctl", "load", "-w", plist_path])
        if code != 0:
            die("launchctl could not load the service%s\n"
                "Set up autostart manually, or run 'ccp daemon start' "
                "after login." % ((":\n%s" % output) if output else "."))


def cmd_daemon_install():
    if sys.platform == "darwin":
        stop_running_daemon()
        install_launchd()
        what = "launchd agent"
    elif sys.platform.startswith("linux"):
        if not shutil.which("systemctl"):
            die("systemd (systemctl) was not found on this system, so "
                "autostart cannot be set up.\n"
                "Run 'ccp daemon start' after login instead.")
        stop_running_daemon()
        install_systemd()
        what = "systemd user service"
    else:
        die("Autostart is only supported on macOS and Linux. "
            "Use 'ccp daemon start' instead.")

    info = wait_for_ping(INSTALL_WAIT_SECS)
    if not info:
        die("The %s was installed, but the daemon did not respond within "
            "%.0fs.\nCheck %s for details."
            % (what, INSTALL_WAIT_SECS,
               os.path.join(crosscopy_home(), "daemon.log")))
    print("✅ Autostart installed (%s) — daemon running on port %d "
          "(device '%s')." % (what, daemon_port(), info.get("name", "?")))
    print("   cross-copy will now start automatically when you log in.")


def cmd_daemon_uninstall():
    if sys.platform == "darwin":
        plist_path = launchd_plist_path()
        removed = False
        if os.path.exists(plist_path):
            uid = os.getuid()
            code, _ = run_cmd(["launchctl", "bootout", "gui/%d" % uid,
                               plist_path])
            if code != 0:
                run_cmd(["launchctl", "unload", plist_path])
            try:
                os.remove(plist_path)
                removed = True
            except OSError as e:
                die("Could not remove %s: %s" % (plist_path, e))
        if removed:
            print("✅ Autostart removed (%s)." % plist_path)
        else:
            print("No autostart service was installed; nothing to remove.")
    elif sys.platform.startswith("linux"):
        unit_path = systemd_unit_path()
        had_unit = os.path.exists(unit_path)
        if shutil.which("systemctl"):
            run_cmd(["systemctl", "--user", "disable", "--now",
                     SYSTEMD_UNIT_NAME])
        if had_unit:
            try:
                os.remove(unit_path)
            except OSError as e:
                die("Could not remove %s: %s" % (unit_path, e))
            if shutil.which("systemctl"):
                run_cmd(["systemctl", "--user", "daemon-reload"])
            print("✅ Autostart removed (%s)." % unit_path)
        else:
            print("No autostart service was installed; nothing to remove.")
    else:
        print("Autostart is only supported on macOS and Linux; "
              "nothing to remove.")


def cmd_daemon(args):
    action = args.action
    if action == "run":
        os.execv(sys.executable, [sys.executable, "-m", "crosscopy.daemon"])
    elif action == "start":
        if ping():
            print("✅ Daemon already running on port %d." % daemon_port())
            return
        print("Starting cross-copy daemon...")
        spawn_daemon()
        info = wait_for_ping()
        if not info:
            die("Daemon did not come up within %.0fs. Check %s."
                % (START_WAIT_SECS, os.path.join(crosscopy_home(), "daemon.log")))
        print("✅ Daemon running on port %d (device '%s')."
              % (daemon_port(), info.get("name", "?")))
    elif action == "stop":
        info = read_daemon_json()
        if not info or not info.get("pid"):
            if ping():
                die("Daemon appears to be running but %s is missing; "
                    "stop it manually." % os.path.join(crosscopy_home(), "daemon.json"))
            print("Daemon is not running.")
            return
        pid = int(info["pid"])
        if not pid_alive(pid):
            print("Daemon is not running (stale daemon.json removed).")
            try:
                os.remove(os.path.join(crosscopy_home(), "daemon.json"))
            except OSError:
                pass
            return
        error = terminate_pid(pid)
        if error:
            die(error)
        print("✅ Daemon stopped (pid %d)." % pid)
    elif action == "install":
        cmd_daemon_install()
    elif action == "uninstall":
        cmd_daemon_uninstall()
    elif action == "status":
        info = ping()
        if info:
            dj = read_daemon_json() or {}
            line = "✅ Daemon running on port %d" % daemon_port()
            if dj.get("pid"):
                line += " (pid %s)" % dj["pid"]
            print(line + " — device '%s'" % info.get("name", "?"))
        else:
            print("Daemon is not running. Start it with 'ccp daemon start'.")
            sys.exit(1)


def cmd_ui(args):
    ensure_daemon()
    url = "http://localhost:%d/" % daemon_port()
    print("Opening %s" % url)
    webbrowser.open(url)


def cmd_version(args):
    print("cross-copy %s" % __version__)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="ccp",
        description="cross-copy: a network file clipboard for Mac and Linux.",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("copy",
                       help="put files/dirs or text on the network clipboard")
    p.add_argument("paths", nargs="*", metavar="path-or-text")
    p.add_argument("-t", "--text", action="store_true",
                   help="treat the arguments as text, not paths")
    p.set_defaults(func=lambda a: cmd_copy(a, "copy"))

    p = sub.add_parser("move", help="like copy, but the source is removed after paste")
    p.add_argument("paths", nargs="*", metavar="path-or-text")
    p.add_argument("-t", "--text", action="store_true",
                   help="treat the arguments as text, not paths")
    p.set_defaults(func=lambda a: cmd_copy(a, "move"))

    p = sub.add_parser("paste", help="paste the newest peer clipboard into a directory")
    p.add_argument("dir", nargs="?", default=None,
                   help="destination directory (default: current directory)")
    p.add_argument("--from", dest="from_", metavar="NAME_OR_ID",
                   help="paste from a specific device")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_paste)

    p = sub.add_parser("devices", aliases=["list"],
                       help="list cross-copy devices on the network")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("status", help="show daemon status and local clipboard")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("clear", help="clear the local clipboard")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("add", help="add a peer manually (when mDNS is blocked)")
    p.add_argument("host")
    p.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT)
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("name", help="set this device's name")
    p.add_argument("newname")
    p.set_defaults(func=cmd_name)

    p = sub.add_parser(
        "daemon",
        help="manage the background daemon (run|start|stop|status|install|uninstall)")
    p.add_argument("action",
                   choices=["run", "start", "stop", "status",
                            "install", "uninstall"],
                   help="run: foreground; start/stop/status: background daemon; "
                        "install/uninstall: start-at-login service")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("ui", help="open the web UI in a browser")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("version", help="show version")
    p.set_defaults(func=cmd_version)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        sys.exit(1)
    try:
        args.func(args)
    except KeyboardInterrupt:
        err("Interrupted.")
        sys.exit(130)
    except requests.exceptions.ConnectionError:
        die("Lost connection to the cross-copy daemon on port %d." % daemon_port())
    except requests.exceptions.RequestException as e:
        die("Network error talking to the cross-copy daemon: %s" % e)


if __name__ == "__main__":
    main()
