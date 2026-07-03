"""cross-copy CLI (`ccp`).

Thin HTTP client that talks to the local cross-copy daemon per SPEC.md.
Only stdlib + argparse + requests; no other crosscopy imports (except a
guarded __version__).
"""

import argparse
import json
import os
import plistlib
import re
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
OFFER_WAIT_SECS = 300.0  # offers expire after 5 minutes (SPEC v0.4)
OFFER_POLL_SECS = 1.0

SYSTEMD_UNIT_NAME = "cross-copy"
LAUNCHD_LABEL = "com.crosscopy.daemon"

UPDATE_URL_DEFAULT = ("https://raw.githubusercontent.com/UNILOOP/cross-copy/"
                      "main/crosscopy/__init__.py")
UPDATE_PKG_DEFAULT = ("https://github.com/UNILOOP/cross-copy/"
                      "archive/refs/heads/main.tar.gz")


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


def offer_contents(offer):
    """Short offer description like '3 files (2.1 MB)' or 'text (12 chars)'."""
    if offer.get("kind") == "text":
        return "text (%s)" % plural(len(offer.get("text") or ""), "char")
    files = offer.get("files") or []
    return "%s (%s)" % (plural(len(files), "file"),
                        human_size(offer.get("total_size", 0)))


def human_age(epoch):
    """Age like '42s' or '3m 05s' for an epoch timestamp."""
    try:
        delta = max(0, int(time.time() - float(epoch)))
    except (TypeError, ValueError):
        return "?"
    if delta < 60:
        return "%ds" % delta
    if delta < 3600:
        return "%dm %02ds" % (delta // 60, delta % 60)
    return "%dh %02dm" % (delta // 3600, (delta % 3600) // 60)


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
            # Neutral CWD: a crosscopy/ dir in the caller's CWD would land
            # on sys.path and shadow the installed package.
            cwd=os.path.expanduser("~"),
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


def run_launchctl(args):
    """Run launchctl with stdout AND stderr captured separately, so the real
    error (usually on stderr) survives into our messages.
    Returns (returncode, combined output); -1 if launchctl couldn't run."""
    try:
        proc = subprocess.run(["launchctl"] + list(args),
                              capture_output=True, text=True)
        parts = [s.strip() for s in (proc.stdout, proc.stderr) if s and s.strip()]
        return proc.returncode, "\n".join(parts)
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


def classify_payload(args, cmd, usage_extra=""):
    """Shared path-vs-text detection for copy/move/send.

    All args exist as paths => ("files", [abs paths]); none exist => the args
    joined with spaces are ("text", str); mixed => die and ask the user to
    disambiguate. --text/-t forces text; no args + piped stdin reads stdin.
    """
    items = args.paths

    if not items:
        if sys.stdin.isatty():
            die("ccp %s: missing paths or text.\n"
                "Usage: ccp %s <path...>%s  |  ccp %s --text <words...>%s  |  "
                "echo hi | ccp %s%s"
                % (cmd, cmd, usage_extra, cmd, usage_extra, cmd, usage_extra),
                code=2)
        return "text", read_stdin_text()

    if getattr(args, "text", False):
        return "text", " ".join(items)

    paths = []
    missing = []
    for p in items:
        ap = os.path.abspath(p)
        if os.path.exists(ap):
            paths.append(ap)
        else:
            missing.append(p)

    if not missing:
        return "files", paths
    if not paths:
        return "text", " ".join(items)
    die("Mixed arguments: some exist as paths, but these do not: %s\n"
        "To send everything as text, use: ccp %s --text ...\n"
        "Otherwise fix the path and try again." % (", ".join(missing), cmd))


def cmd_copy(args, op):
    kind, payload = classify_payload(args, op)
    if kind == "text":
        do_copy_text(payload, op)
    else:
        do_copy_files(payload, op)


def list_peers():
    """GET /api/peers and return the peer list (dies on error)."""
    r = api_get("/api/peers", timeout=10)
    if not r.ok:
        die("Could not list devices: %s" % api_error_message(r))
    return r.json().get("peers", [])


def resolve_peer(name_or_id):
    """Resolve a --from/--to value to a peer dict via /api/peers.

    Exact id match first, then case-insensitive name match; dies with a
    helpful message on no/ambiguous matches.
    """
    peers = list_peers()
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
    return matches[0]


def resolve_send_target(to):
    """Pick the target peer for `ccp send`.

    --to given: resolve like paste's --from. --to omitted: OK only when
    exactly one peer exists; otherwise list the choices and require --to.
    """
    if to:
        return resolve_peer(to)
    peers = list_peers()
    if not peers:
        die("No other cross-copy devices found on the network.\n"
            "Make sure cross-copy is running on the target machine "
            "(or add it manually: ccp add <ip>).")
    if len(peers) == 1:
        return peers[0]
    names = ", ".join(sorted(p.get("name") or p.get("id") or "?"
                             for p in peers))
    die("There are %s on the network — pick one with --to <name>.\n"
        "Devices: %s" % (plural(len(peers), "device"), names))


def cmd_paste(args):
    dest = os.path.abspath(args.dir or os.getcwd())
    ensure_daemon()

    body = {"dest": dest}
    if getattr(args, "from_", None):
        body["peer_id"] = resolve_peer(args.from_)["id"]

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


# ---------------------------------------------------------------------------
# Targeted send / offers — see SPEC.md "Targeted send with accept/reject (v0.4)"
# ---------------------------------------------------------------------------

def offer_from_name(offer):
    src = offer.get("from") or {}
    return src.get("name") or src.get("id") or "unknown device"


def wait_for_offer_result(offer, peer_name, live):
    """Poll GET /api/send/<id> ~1/s until the offer reaches a terminal state.

    Returns the last-seen offer object (still 'pending'/'accepted' if we hit
    the 5-minute ceiling). With live=True a single status line is kept
    up to date in place.
    """
    offer_id = offer.get("offer_id")
    started = time.time()
    deadline = started + OFFER_WAIT_SECS
    current = offer
    while True:
        status = (current or {}).get("status", "pending")
        if status in ("completed", "declined", "failed", "expired"):
            break
        if time.time() >= deadline:
            break
        if live:
            if status == "accepted":
                line = "   %s accepted — transferring..." % peer_name
            else:
                line = ("   waiting for %s to accept... (%ds)"
                        % (peer_name, int(time.time() - started)))
            sys.stdout.write("\r\033[K" + line)
            sys.stdout.flush()
        time.sleep(OFFER_POLL_SECS)
        r = api_get("/api/send/%s" % offer_id, timeout=10)
        if r.status_code == 404:
            # Pruned on the daemon side — the offer expired.
            current = dict(current or {}, status="expired")
            break
        if r.ok:
            try:
                current = r.json()
            except ValueError:
                pass
    if live:
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()
    return current or offer


def cmd_send(args):
    kind, payload = classify_payload(args, "send", " --to <device>")
    ensure_daemon()
    peer = resolve_send_target(args.to)
    peer_name = peer.get("name") or peer.get("id") or "?"

    body = {"peer_id": peer["id"]}
    if kind == "text":
        if not payload:
            die("Nothing to send: the text is empty.")
        body["text"] = payload
    else:
        body["paths"] = payload

    r = api_post("/api/send", body, timeout=60)
    if r.status_code == 404:
        die("The daemon no longer knows device '%s' — check 'ccp devices' "
            "and try again." % peer_name)
    if r.status_code == 502:
        die("Could not reach %s — is cross-copy running there?" % peer_name)
    if not r.ok:
        die("Send failed: %s" % api_error_message(r))
    offer = r.json()

    if not args.json:
        print("📨 Offered %s to %s — waiting for them to accept..."
              % (offer_contents(offer), peer_name))
    live = (not args.json) and sys.stdout.isatty()

    try:
        final = wait_for_offer_result(offer, peer_name, live)
    except KeyboardInterrupt:
        if live:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        err("\nStopped waiting. The offer stays valid for about 5 minutes — "
            "%s can still accept it." % peer_name)
        sys.exit(130)

    status = (final or {}).get("status", "pending")
    if args.json:
        print(json.dumps(final, indent=2))

    if status == "completed":
        if not args.json:
            if final.get("kind") == "text":
                what = "text"
            else:
                what = plural(len(final.get("files") or []), "file")
            print("✅ %s accepted — %s delivered" % (peer_name, what))
        return
    if status == "declined":
        if not args.json:
            err("🚫 %s declined" % peer_name)
        sys.exit(1)
    if status == "failed":
        if not args.json:
            reason = final.get("error") or final.get("reason")
            err("❌ Transfer to %s failed%s"
                % (peer_name, (": %s" % reason) if reason else "."))
        sys.exit(1)
    # "expired" from the daemon, or we timed out with the offer still pending.
    if not args.json:
        err("⌛ No answer from %s within 5 minutes — the offer expired."
            % peer_name)
    sys.exit(1)


def fetch_offers():
    """GET /api/offers and return the pending incoming offers (dies on error)."""
    r = api_get("/api/offers")
    if not r.ok:
        die("Could not list offers: %s" % api_error_message(r))
    return r.json().get("offers", [])


def pick_offer(offers, offer_id):
    """Pick an offer: newest when offer_id is None, else exact-id or
    unambiguous short-id-prefix match. Returns None when nothing matches;
    dies when a prefix is ambiguous."""
    if not offer_id:
        return max(offers, key=lambda o: o.get("created_at") or 0)
    exact = [o for o in offers if o.get("offer_id") == offer_id]
    if exact:
        return exact[0]
    prefixed = [o for o in offers
                if (o.get("offer_id") or "").startswith(offer_id)]
    if len(prefixed) == 1:
        return prefixed[0]
    if len(prefixed) > 1:
        die("Offer id '%s' is ambiguous — use more characters "
            "(see 'ccp offers')." % offer_id)
    return None


def cmd_offers(args):
    ensure_daemon()
    offers = fetch_offers()

    if args.json:
        print(json.dumps(offers, indent=2))
        return

    if not offers:
        print("No pending offers.")
        print("   (Someone can send you one with: ccp send <file> --to <you>)")
        return

    offers = sorted(offers, key=lambda o: o.get("created_at") or 0,
                    reverse=True)
    rows = []
    for o in offers:
        rows.append((
            (o.get("offer_id") or "?")[:8],
            offer_from_name(o),
            clipboard_summary(o),
            human_age(o.get("created_at")),
        ))
    headers = ("ID", "FROM", "CONTENTS", "AGE")
    widths = [max(len(headers[i]), max(len(r[i]) for r in rows))
              for i in range(len(headers))]
    fmt = "  ".join("%%-%ds" % w for w in widths)
    print(fmt % headers)
    for row in rows:
        print(fmt % row)
    print("")
    print("Accept with: ccp accept [id] [dir]   ·   Decline with: "
          "ccp decline [id]")


def cmd_accept(args):
    ensure_daemon()
    offers = fetch_offers()
    if not offers:
        die("No pending offers to accept. (See them with 'ccp offers'.)")

    offer = pick_offer(offers, args.offer_id)
    if offer is None:
        # Convenience: `ccp accept ~/dir` — the lone argument is really a
        # destination directory, so accept the newest offer into it.
        maybe_dir = os.path.expanduser(args.offer_id or "")
        if args.offer_id and args.dir is None and os.path.isdir(maybe_dir):
            args.dir = args.offer_id
            offer = pick_offer(offers, None)
        else:
            die("No pending offer matches '%s'. See 'ccp offers'."
                % args.offer_id)

    body = {}
    if args.dir:
        # dir omitted => no "dest" key, so the daemon uses its receive_dir.
        body["dest"] = os.path.abspath(os.path.expanduser(args.dir))

    r = api_post("/api/offers/%s/accept" % offer["offer_id"], body,
                 timeout=600)
    if r.status_code == 404:
        die("That offer is gone — it may have expired or been withdrawn.")
    if r.status_code == 502:
        die("Transfer failed — could not pull the files from %s."
            % offer_from_name(offer))
    if not r.ok:
        die("Accept failed: %s" % api_error_message(r))
    result = r.json()

    if args.json:
        print(json.dumps(result, indent=2))
        return

    src_name = offer_from_name(offer)
    if result.get("kind") == "text":
        # Verbatim to stdout so it pipes; the info line goes to stderr
        # (exactly like `ccp paste` with a text clipboard).
        text = result.get("text", "")
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        err("📥 from %s" % src_name)
        return

    files = result.get("files_written", [])
    print("📥 Accepted %s (%s) from %s"
          % (plural(len(files), "file"),
             human_size(result.get("total_bytes",
                                   offer.get("total_size", 0))),
             src_name))
    for f in files:
        print("   %s" % f)


def cmd_decline(args):
    ensure_daemon()
    offers = fetch_offers()
    if not offers:
        die("No pending offers to decline.")

    offer = pick_offer(offers, args.offer_id)
    if offer is None:
        die("No pending offer matches '%s'. See 'ccp offers'." % args.offer_id)

    r = api_post("/api/offers/%s/decline" % offer["offer_id"])
    if r.status_code == 404:
        die("That offer is gone — it may have already expired.")
    if not r.ok:
        die("Decline failed: %s" % api_error_message(r))
    print("🚫 Declined %s from %s."
          % (offer_contents(offer), offer_from_name(offer)))


def cmd_widget(args):
    ensure_daemon()
    try:
        from crosscopy import widget
    except ImportError:
        die("The tray widget isn't available in this install.\n"
            '   Install the extras:  pip install "cross-copy[widget]"\n'
            '   (pipx users:  pipx install "cross-copy[widget]" --force)')
    # Missing optional deps (pystray/Pillow) are handled inside widget.main()
    # with a friendly install hint.
    widget.main()


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

    # Older daemons don't report an "update" key — that's fine.
    update = status.get("update") or {}
    if isinstance(update, dict) and update.get("available") and update.get("latest"):
        if update.get("auto_update"):
            hint = "auto-update will install it"
        else:
            hint = "run 'ccp update'"
        print("⬆️  Update v%s available — %s" % (update["latest"], hint))


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
# Updates (`ccp update`) — see SPEC.md "Updates & auto-update (v0.3)"
# ---------------------------------------------------------------------------

def update_url():
    return os.environ.get("CROSSCOPY_UPDATE_URL") or UPDATE_URL_DEFAULT


def update_pkg():
    return os.environ.get("CROSSCOPY_UPDATE_PKG") or UPDATE_PKG_DEFAULT


def version_tuple(version):
    """'0.3.0' -> (0, 3, 0) for comparison; unparseable -> (0,)."""
    parts = re.findall(r"\d+", version or "")
    return tuple(int(p) for p in parts) or (0,)


def fetch_latest_version():
    """Fetch the published version string, or die with a friendly error."""
    url = update_url()
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        die("Could not check for updates — is the network up?\n"
            "   %s\n   (%s)" % (url, e))
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', r.text)
    if not m:
        die("Could not find a version number at %s" % url)
    return m.group(1)


def run_self_update():
    """pip-install the latest package. Dies (with pip's tail) on failure."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if sys.prefix == sys.base_prefix:  # not in a venv
        cmd.append("--user")
    cmd.append(update_pkg())
    code, output = run_cmd(cmd)
    if code != 0:
        tail = "\n".join(output.splitlines()[-15:])
        die("Update failed: pip exited with code %d.%s"
            % (code, ("\n--- pip output (last lines) ---\n%s" % tail)
               if tail else ""))


def restart_daemon_after_update():
    """Stop the running daemon so the new code loads.

    If a systemd/launchd service is installed, its Restart/KeepAlive policy
    brings the daemon back by itself — poll ping briefly and only spawn one
    manually if nothing came back.
    """
    info = read_daemon_json()
    pid = (info or {}).get("pid")
    try:
        pid = int(pid) if pid else None
    except (TypeError, ValueError):
        pid = None
    if pid and pid_alive(pid):
        error = terminate_pid(pid)
        if error:
            err(error)
            err("Restart it manually to load the new version: "
                "ccp daemon stop && ccp daemon start")
            return
    if wait_for_ping(3.0):  # service manager restarted it
        return
    spawn_daemon()
    if not wait_for_ping():
        err("The daemon did not come back after the update. "
            "Start it with 'ccp daemon start'.")


def cmd_update(args):
    current = __version__
    latest = fetch_latest_version()
    available = version_tuple(latest) > version_tuple(current)

    def emit_json(updated):
        print(json.dumps({"current": current, "latest": latest,
                          "available": available, "updated": updated},
                         indent=2))

    if args.check:
        if args.json:
            emit_json(False)
        elif available:
            print("⬆️  Update available: %s → %s  (run 'ccp update')"
                  % (current, latest))
        else:
            print("✅ cross-copy %s is up to date" % current)
        return

    if not available:
        if args.json:
            emit_json(False)
        else:
            print("✅ cross-copy %s is up to date" % current)
        return

    if not args.json:
        print("⬆️  Updating cross-copy %s → %s ..." % (current, latest))
    was_running = ping() is not None
    run_self_update()
    if was_running:
        if not args.json:
            print("Restarting the daemon to load the new version...")
        restart_daemon_after_update()
    if args.json:
        emit_json(True)
    else:
        print("✅ Updated cross-copy %s → %s" % (current, latest))


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


MACOS_LAUNCHER_NAME = "Cross Copy"


def macos_launcher_candidates():
    """All places a 'Cross Copy' launcher may have been installed."""
    return [os.path.join(sys.prefix, "bin", MACOS_LAUNCHER_NAME),
            os.path.join(crosscopy_home(), "bin", MACOS_LAUNCHER_NAME)]


def make_macos_launcher():
    """Create a python launcher named 'Cross Copy' and return its path.

    macOS shows ProgramArguments[0]'s basename in Login Items and firewall
    prompts — with sys.executable that reads 'Python 3.x'. In a venv/pipx
    install we drop a real *copy* of the interpreter into <venv>/bin (a
    copied stub still finds pyvenv.cfg relative to its own directory, so the
    venv keeps working, and the firewall prompt names 'Cross Copy').
    Refreshed on every install so upgrades keep working. Outside a venv we
    fall back to a symlink under ~/.crosscopy/bin (fixes Login Items at
    least), and to plain sys.executable as a last resort.
    """
    if sys.prefix != sys.base_prefix:  # venv / pipx — the common install
        bin_dir = os.path.join(sys.prefix, "bin")
        launcher = os.path.join(bin_dir, MACOS_LAUNCHER_NAME)
        if os.access(bin_dir, os.W_OK):
            try:
                if os.path.lexists(launcher):
                    os.remove(launcher)
                shutil.copy2(sys.executable, launcher)
                os.chmod(launcher, 0o755)
                return launcher
            except OSError:
                pass
    try:
        bin_dir = os.path.join(crosscopy_home(), "bin")
        os.makedirs(bin_dir, exist_ok=True)
        launcher = os.path.join(bin_dir, MACOS_LAUNCHER_NAME)
        if os.path.lexists(launcher):
            os.remove(launcher)
        os.symlink(sys.executable, launcher)
        return launcher
    except OSError:
        return sys.executable


def remove_macos_launcher():
    """Best-effort removal of any 'Cross Copy' launcher we installed."""
    for path in macos_launcher_candidates():
        try:
            if os.path.lexists(path):
                os.remove(path)
        except OSError:
            pass


def install_launchd():
    home = crosscopy_home()
    os.makedirs(home, exist_ok=True)
    log_path = os.path.join(home, "daemon.log")

    launcher = make_macos_launcher()
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [launcher, "-m", "crosscopy.daemon"],
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
    # Unload any previously-loaded copy first (ignore failures) — bootstrap
    # errors out with "already loaded" otherwise.
    run_launchctl(["bootout", "gui/%d/%s" % (uid, LAUNCHD_LABEL)])
    code, bootstrap_out = run_launchctl(["bootstrap", "gui/%d" % uid,
                                         plist_path])
    if code != 0:
        # Older macOS: fall back to launchctl load -w.
        code, load_out = run_launchctl(["load", "-w", plist_path])
        if code != 0:
            details = []
            if bootstrap_out:
                details.append("launchctl bootstrap: %s" % bootstrap_out)
            if load_out:
                details.append("launchctl load -w: %s" % load_out)
            die("launchctl could not load the service%s\n"
                "Set up autostart manually, or run 'ccp daemon start' "
                "after login."
                % ((":\n%s" % "\n".join(details)) if details else "."))


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
            # Best-effort unload: by label first (modern), then by plist
            # path, then legacy unload. Failures are fine — it may simply
            # not be loaded.
            code, _ = run_launchctl(["bootout",
                                     "gui/%d/%s" % (uid, LAUNCHD_LABEL)])
            if code != 0:
                code, _ = run_launchctl(["bootout", "gui/%d" % uid,
                                         plist_path])
            if code != 0:
                run_launchctl(["unload", plist_path])
            try:
                os.remove(plist_path)
                removed = True
            except OSError as e:
                die("Could not remove %s: %s" % (plist_path, e))
        remove_macos_launcher()
        if removed:
            print("✅ Autostart removed (%s)." % plist_path)
        else:
            print("No autostart service was installed; nothing to remove.")
    elif sys.platform.startswith("linux"):
        unit_path = systemd_unit_path()
        had_unit = os.path.exists(unit_path)
        # Only touch systemd when THIS home's unit file exists — with a
        # custom $HOME/CROSSCOPY_HOME, disabling unconditionally could stop
        # a different install's live service.
        if had_unit and shutil.which("systemctl"):
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
        # Neutral CWD so a crosscopy/ dir here can't shadow the installed
        # package (matches spawn_daemon).
        try:
            os.chdir(os.path.expanduser("~"))
        except OSError:
            pass
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
    # Free update hint only: ask an *already-running* local daemon for its
    # cached update state. Never start the daemon or touch the internet here.
    try:
        r = requests.get(base_url() + "/api/status", timeout=0.8)
        update = (r.json() or {}).get("update") or {}
    except (requests.RequestException, ValueError):
        return
    if isinstance(update, dict) and update.get("available") and update.get("latest"):
        print("⬆️  Update v%s available — run 'ccp update'" % update["latest"])


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

    p = sub.add_parser(
        "send",
        help="offer files or text to one device — they must accept "
             "(AirDrop-style)")
    p.add_argument("paths", nargs="*", metavar="path-or-text")
    p.add_argument("-t", "--text", action="store_true",
                   help="treat the arguments as text, not paths")
    p.add_argument("--to", metavar="NAME_OR_ID",
                   help="target device (optional when there is exactly "
                        "one other device)")
    p.add_argument("--json", action="store_true",
                   help="print the final offer object as JSON")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("offers", help="list pending incoming offers")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_offers)

    p = sub.add_parser("accept",
                       help="accept a pending offer (newest if no id)")
    p.add_argument("offer_id", nargs="?", default=None,
                   help="offer id — a short prefix is enough (default: "
                        "newest offer)")
    p.add_argument("dir", nargs="?", default=None,
                   help="destination directory (default: your receive "
                        "folder, ~/Downloads/cross-copy)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("decline",
                       help="decline a pending offer (newest if no id)")
    p.add_argument("offer_id", nargs="?", default=None,
                   help="offer id — a short prefix is enough (default: "
                        "newest offer)")
    p.set_defaults(func=cmd_decline)

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

    p = sub.add_parser(
        "widget",
        help='run the menu-bar/tray widget (needs: pip install '
             '"cross-copy[widget]")')
    p.set_defaults(func=cmd_widget)

    p = sub.add_parser("update",
                       help="update cross-copy to the latest version")
    p.add_argument("--check", action="store_true",
                   help="only check whether an update is available")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_update)

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
