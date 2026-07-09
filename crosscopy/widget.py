"""cross-copy tray widget (`ccp widget` / `python -m crosscopy.widget`).

System-tray / menu-bar companion per SPEC.md "Tray widget (v0.4)".
pystray + Pillow are an optional extra (`cross-copy[widget]`); everything
else is stdlib + requests. Talks only to the *local* daemon.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

import requests

DEFAULT_PORT = 7373
BRAND_BLUE = (47, 111, 237, 255)      # #2f6fed
BRAND_SOFT = (155, 183, 245, 255)     # lighter companion square
APP_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium",
                "chromium-browser", "brave-browser", "brave", "msedge",
                "microsoft-edge")
# macOS browsers live in app bundles, not on PATH (fallback when the native
# NSPanel helper can't run).
MAC_APP_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
PANEL_SIZE = (420, 680)               # compact panel app-window, w x h


def panel_command(exe, url):
    """Command list to open `url` as a compact panel app-window.

    `--window-size` keeps the `--app=` window from inheriting the browser's
    last (often maximized) window size. No `--user-data-dir`: that would
    fork a second browser profile. Caveat: when an already-running browser
    process adopts the window, it may ignore `--window-size` — the panel's
    own JS (widget.js fitPanelWindow) then resizes itself as a fallback.
    """
    return [exe, "--app=%s" % url,
            "--window-size=%d,%d" % PANEL_SIZE]

INSTALL_HINT = (
    "The tray widget needs the optional 'widget' extra (pystray + Pillow).\n"
    "Install it with one of:\n"
    '  pip install "cross-copy[widget]"\n'
    '  pipx install "cross-copy[widget]" --force\n'
)

MAX_POPUPS = 3  # concurrent popup cards; beyond that the oldest is dropped


# ---------------------------------------------------------------------------
# Local daemon plumbing (tiny re-implementation of the CLI helpers)
# ---------------------------------------------------------------------------

def crosscopy_home():
    return os.environ.get("CROSSCOPY_HOME") or os.path.expanduser("~/.crosscopy")


def widget_log_path():
    return os.path.join(crosscopy_home(), "widget.log")


def log_line(msg):
    """Append a timestamped line to widget.log (popup decisions and spawn
    failures were invisible when everything went to /dev/null)."""
    try:
        with open(widget_log_path(), "a") as f:
            f.write("%s [widget] %s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


_last_port = None  # last port read from daemon.json (survives its absence)


def daemon_port():
    """Port from daemon.json, falling back to the last port successfully
    read from it. daemon.json disappears for a moment during a daemon
    stop/start; without the cache a widget reconnecting right then would
    silently attach to CROSSCOPY_PORT/7373 — possibly a different daemon —
    and stay there for the life of the SSE stream."""
    global _last_port
    try:
        with open(os.path.join(crosscopy_home(), "daemon.json")) as f:
            _last_port = int(json.load(f)["port"])
            return _last_port
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if _last_port is not None:
        return _last_port
    try:
        return int(os.environ.get("CROSSCOPY_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def base_url():
    return "http://127.0.0.1:%d" % daemon_port()


def ping(timeout=2.0):
    try:
        r = requests.get(base_url() + "/api/ping", timeout=timeout)
        if r.ok:
            return r.json()
    except (requests.RequestException, ValueError):
        pass
    return None


def ensure_daemon():
    """Ping the local daemon; spawn a detached one if it's down."""
    info = ping()
    if info:
        return info
    home = crosscopy_home()
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "daemon.log"), "ab") as log:
        subprocess.Popen([sys.executable, "-m", "crosscopy.daemon"],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        info = ping(timeout=0.5)
        if info:
            return info
        time.sleep(0.2)
    return None


def spawn_popup(*args):
    """Launch a crosscopy.popup subprocess (fire and forget).
    Each popup is its own process so its GUI toolkit (AppKit on macOS,
    tkinter elsewhere — popup.py picks its own backend) never fights
    pystray for the main thread. Popup stderr goes to widget.log so
    failures (no display, missing tkinter, PyObjC errors, tracebacks) are
    diagnosable. Returns the Popen, or None."""
    argv = [sys.executable, "-m", "crosscopy.popup"] + [str(a) for a in args]
    stderr = subprocess.DEVNULL
    try:
        stderr = open(widget_log_path(), "ab")
    except OSError:
        pass
    try:
        return subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=stderr, start_new_session=True)
    except OSError as e:
        log_line("popup spawn failed: %s (args: %s)"
                 % (e, " ".join(argv[3:])))
        return None
    finally:
        if stderr is not subprocess.DEVNULL:
            try:
                stderr.close()  # child keeps its inherited copy
            except OSError:
                pass


def notify(title, body):
    """Widget-owned notification: show an info popup card; never raises."""
    if spawn_popup("info", title, body) is None:
        print("%s: %s" % (title, body), file=sys.stderr)


def api_get(path, timeout=4):
    try:
        r = requests.get(base_url() + path, timeout=timeout)
        if r.ok:
            return r.json()
    except (requests.RequestException, ValueError):
        pass
    return None


def api_post(path, body=None, timeout=30):
    try:
        r = requests.post(base_url() + path, json=body or {}, timeout=timeout)
        payload = {}
        try:
            payload = r.json()
        except ValueError:
            pass
        return r.ok, payload
    except requests.RequestException as e:
        return False, {"error": str(e)}


def format_size(total):
    units = ["B", "KB", "MB", "GB"]
    n, i = float(total or 0), 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return "%d B" % (total or 0) if i == 0 else "%.1f %s" % (n, units[i])


def clip_summary(manifest):
    """'3 files (2.1 MB)' / 'text (52 chars)' for a clipboard manifest."""
    if (manifest.get("kind") or "files") == "text":
        n = len(manifest.get("text") or "") or manifest.get("total_size") or 0
        return "text (%d chars)" % n
    files = manifest.get("files") or []
    return "%d file%s (%s)" % (len(files), "" if len(files) == 1 else "s",
                               format_size(manifest.get("total_size")))


# ---------------------------------------------------------------------------
# Tray icon glyph — two overlapping rounded squares, brand blue
# ---------------------------------------------------------------------------

def make_icon_image(dark_tray=False):
    """64x64 RGBA glyph that stays recognizable at 22-32px."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if sys.platform == "darwin":
        # macOS menu extras are template images: one opaque colour plus alpha.
        # pystray scales this to the current menu-bar thickness.
        d.rounded_rectangle((8, 8, 39, 39), radius=7,
                            outline=(0, 0, 0, 255), width=6)
        d.rounded_rectangle((25, 25, 56, 56), radius=7,
                            outline=(0, 0, 0, 255), width=6)
        return img
    back = (255, 255, 255, 235) if dark_tray else BRAND_SOFT
    d.rounded_rectangle((6, 6, 42, 42), radius=10, fill=back)
    d.rounded_rectangle((22, 22, 58, 58), radius=10, fill=BRAND_BLUE,
                        outline=(255, 255, 255, 200), width=2)
    return img


# ---------------------------------------------------------------------------
# tkinter helpers (lazy — tkinter may be missing on minimal installs)
# ---------------------------------------------------------------------------

TK_HINT = ("tkinter is not available. Install your platform's python3-tk "
           "package (e.g. 'sudo apt install python3-tk').")


def _tk_root():
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    return root


def pick_files():
    """File-picker dialog. Returns a list of paths ([] on cancel),
    or None when tkinter is unusable."""
    try:
        root = _tk_root()
        from tkinter import filedialog
        paths = list(filedialog.askopenfilenames(
            parent=root, title="cross-copy — send files"))
        root.destroy()
        return paths
    except Exception:
        return None


def read_clipboard_text():
    """Desktop clipboard text, '' when empty, None when tkinter is unusable."""
    try:
        root = _tk_root()
    except Exception:
        return None
    try:
        text = root.clipboard_get()
    except Exception:  # tkinter.TclError — empty/non-text clipboard
        text = ""
    root.destroy()
    return text


# ---------------------------------------------------------------------------
# Widget app
# ---------------------------------------------------------------------------

class WidgetApp(object):
    def __init__(self, pystray_mod):
        self.pystray = pystray_mod
        self.icon = None
        self.device = ping() or {}
        self._stop = threading.Event()
        self._seen_offers = set()   # incoming offer ids we already popped up
        self._seen_clips = None     # peer clipboard_ids seen (None = no baseline yet)
        self._popups = {}           # card key -> Popen (insertion-ordered = age)
        self._slots = {}            # card key -> stacking slot index
        self._my_sends = {}         # offer_id -> peer name (widget-initiated)
        self._panel_proc = None     # Popen of the one-shot macOS panel (fallback)
        self._panel_server = None   # Popen of the pre-warmed macOS panel helper

    # ----- data ------------------------------------------------------------

    def fetch_peers(self):
        data = api_get("/api/peers") or {}
        return data.get("peers") or []

    def fetch_offers(self):
        """Pending incoming offers, or None when the daemon is unreachable
        (callers must NOT treat a failed fetch as 'no offers' — that used to
        wipe the seen-set and re-popup everything)."""
        data = api_get("/api/offers")
        if data is None:
            return None
        return data.get("offers") or []

    # ----- popup cards (the widget IS the notification system) --------------

    def diff_offers(self, offers):
        """Return offer ids not seen before; remember the current set."""
        ids = [o.get("offer_id") for o in offers if o.get("offer_id")]
        new = [i for i in ids if i not in self._seen_offers]
        self._seen_offers = set(ids)
        return new

    def _alloc_slot(self):
        used = set(self._slots.values())
        slot = 0
        while slot in used:
            slot += 1
        return slot

    def _reap_popups(self):
        for key, proc in list(self._popups.items()):
            if proc.poll() is not None:
                self._popups.pop(key, None)
                self._slots.pop(key, None)

    def _spawn_card(self, key, args):
        """Spawn a popup card in a free slot, rate-limited to MAX_POPUPS
        concurrent cards (the oldest is dropped to make room)."""
        while len(self._popups) >= MAX_POPUPS:
            oldest = next(iter(self._popups))
            proc = self._popups.pop(oldest)
            self._slots.pop(oldest, None)
            try:
                proc.terminate()
            except OSError:
                pass
            log_line("popup limit (%d): dropped oldest card %s"
                     % (MAX_POPUPS, oldest))
        slot = self._alloc_slot()
        proc = spawn_popup(*(list(args) + ["--slot", slot]))
        if proc is not None:
            self._popups[key] = proc
            self._slots[key] = slot

    def _check_my_sends(self):
        """Popup the terminal outcome of sends *this widget* initiated
        (CLI sends report in the terminal; the daemon covers the rest)."""
        outcomes = {"completed": "Delivered to %s",
                    "declined": "%s declined",
                    "failed": "Send to %s failed",
                    "expired": "Offer to %s expired"}
        for oid, peer_name in list(self._my_sends.items()):
            data = api_get("/api/send/%s" % oid)
            if data is None:  # pruned/unknown — stop tracking
                self._my_sends.pop(oid, None)
                continue
            status = data.get("status")
            if status in outcomes:
                notify("cross-copy", outcomes[status] % peer_name)
                self._my_sends.pop(oid, None)

    def on_offers_event(self):
        """SSE 'offers' event (or reconnect catch-up): popup new incoming
        offers, report widget-initiated send outcomes, refresh the tray."""
        self._reap_popups()
        offers = self.fetch_offers()
        if offers is None:
            log_line("offers event: /api/offers unreachable — skipping diff")
        else:
            new = self.diff_offers(offers)
            log_line("offers event: %d pending, %d new" % (len(offers), len(new)))
            for oid in new:
                key = "offer:%s" % oid
                if key not in self._popups:
                    self._spawn_card(key, ["offer", oid])
        self._check_my_sends()
        self.refresh_menu()

    def on_peers_event(self):
        """SSE 'peers' event (or reconnect catch-up): popup an interactive
        card when a peer's clipboard gains new content ('machine-b is
        sharing 3 files (2.1 MB)' with Save here / Dismiss), and refresh
        the tray. Peer clipboard changes reach us as 'peers' events (the
        peer sends a hello whenever its clipboard changes)."""
        self._reap_popups()
        data = api_get("/api/peers?with_clipboard=1", timeout=10)
        if data is None:
            log_line("peers event: /api/peers unreachable — skipping diff")
        else:
            my_id = self.device.get("id")
            current = {}
            for p in data.get("peers") or []:
                clip = p.get("clipboard") or {}
                cid = clip.get("clipboard_id")
                if cid and p.get("id") != my_id:  # never our own clipboard
                    current[cid] = (p, clip)
            if self._seen_clips is None:
                # First successful fetch: baseline silently so pre-existing
                # shares don't popup at widget startup.
                self._seen_clips = set(current)
                log_line("peers event: baseline %d shared clipboard(s)"
                         % len(current))
            else:
                for cid, (peer, clip) in current.items():
                    if cid in self._seen_clips:
                        continue
                    self._seen_clips.add(cid)
                    key = "share:%s" % cid
                    if key in self._popups:
                        continue
                    name = peer.get("name") or peer.get("id") or "peer"
                    summary = clip_summary(clip)
                    log_line("peers event: new clipboard %s from %s (%s)"
                             % (cid[:8], name, summary))
                    self._spawn_card(key, [
                        "share", peer.get("id") or "", cid,
                        "--from-name", name, "--summary", summary,
                        "--kind", (clip.get("kind") or "files")])
                if len(self._seen_clips) > 512:  # keep the set bounded
                    self._seen_clips = set(current)
        self.refresh_menu()

    # ----- actions (run in threads so the tray stays responsive) -----------

    def _in_thread(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def send_files(self, peer):
        def work():
            paths = pick_files()
            if paths is None:
                notify("cross-copy", TK_HINT)
                return
            if not paths:
                return  # dialog cancelled
            ok, res = api_post("/api/send",
                               {"peer_id": peer["id"], "paths": paths})
            if not ok:
                notify("cross-copy", "Send failed: %s"
                       % (res.get("error") or "daemon error"))
            elif res.get("offer_id"):  # popup its outcome when it resolves
                self._my_sends[res["offer_id"]] = peer.get("name") or "peer"
        self._in_thread(work)

    def send_clipboard(self, peer):
        def work():
            text = read_clipboard_text()
            if text is None:
                notify("cross-copy", TK_HINT)
                return
            if not text:
                notify("cross-copy", "Clipboard is empty — nothing to send.")
                return
            ok, res = api_post("/api/send",
                               {"peer_id": peer["id"], "text": text})
            if not ok:
                notify("cross-copy", "Send failed: %s"
                       % (res.get("error") or "daemon error"))
            elif res.get("offer_id"):
                self._my_sends[res["offer_id"]] = peer.get("name") or "peer"
        self._in_thread(work)

    def offer_action(self, offer_id, action):
        def work():
            ok, res = api_post("/api/offers/%s/%s" % (offer_id, action))
            if not ok:
                notify("cross-copy", "Could not %s offer: %s"
                       % (action, res.get("error") or "daemon error"))
            self.refresh_menu()
        self._in_thread(work)

    # ----- macOS pre-warmed panel helper ------------------------------------

    def start_panel_server(self):
        """darwin only: spawn crosscopy.macpanel --server at widget startup.
        The helper imports PyObjC and loads the panel page while hidden, so
        the first "Open panel" click is a one-line stdin command instead of
        a 1-3 s cold start. Returns the Popen, or None."""
        if sys.platform != "darwin":
            return None
        url = "http://localhost:%d/widget" % daemon_port()
        stderr = subprocess.DEVNULL
        try:
            stderr = open(widget_log_path(), "ab")
        except OSError:
            pass
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "crosscopy.macpanel", "--server", url],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=stderr, start_new_session=True)
        except OSError as e:
            log_line("mac panel helper spawn failed: %s" % e)
            return None
        finally:
            if stderr is not subprocess.DEVNULL:
                try:
                    stderr.close()
                except OSError:
                    pass
        self._panel_server = proc
        return proc

    def _panel_send(self, cmd):
        """Write one command line to the panel helper's stdin. False when
        the helper is gone or the pipe is broken."""
        proc = self._panel_server
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(("%s\n" % cmd).encode("utf-8"))
            proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def open_panel(self):
        url = "http://localhost:%d/widget" % daemon_port()
        if sys.platform == "darwin":
            # Pre-warmed helper: toggling is instant. Helper died / pipe
            # broken -> respawn once; still failing -> browser fallback.
            if self._panel_send("toggle"):
                return
            proc = self.start_panel_server()
            if proc is not None:
                time.sleep(0.6)  # a deps-missing exit (code 3) is instant
                if proc.poll() is None and self._panel_send("show"):
                    return
            log_line("mac panel helper unavailable — falling back")
            # exit 3 = PyObjC/WebKit bindings missing: the one-shot panel
            # would fail identically, go straight to a browser window.
            deps_missing = proc is not None and proc.poll() == 3
            if not deps_missing and self._open_mac_panel(url):
                return
        for name in APP_BROWSERS:
            exe = shutil.which(name)
            if exe:
                try:
                    subprocess.Popen(panel_command(exe, url),
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     start_new_session=True)
                    return
                except OSError:
                    pass
        # macOS: browser binaries live in app bundles, not on PATH.
        if sys.platform == "darwin":
            for bundle_exe in MAC_APP_BROWSERS:
                if os.path.exists(bundle_exe):
                    try:
                        subprocess.Popen(panel_command(bundle_exe, url),
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL,
                                         start_new_session=True)
                        return
                    except OSError:
                        pass
        webbrowser.open(url)

    def _open_mac_panel(self, url):
        """Open the native NSPanel (crosscopy.macpanel). Clicking "Open
        panel" while one is up closes and reopens it (refresh semantics).
        Returns False when the helper can't run (missing WebKit bindings,
        exit code 3) so the caller falls back to a browser window."""
        proc = self._panel_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "crosscopy.macpanel", url],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            return False
        # Give it a moment: a deps-missing exit (code 3) is near-instant.
        time.sleep(0.6)
        if proc.poll() is not None:
            return False
        self._panel_proc = proc
        return True

    def open_webui(self):
        webbrowser.open("http://localhost:%d/" % daemon_port())

    def quit(self):
        self._stop.set()
        server = self._panel_server
        if server is not None and server.poll() is None:
            self._panel_send("quit")
            try:
                if server.stdin is not None:
                    server.stdin.close()
            except OSError:
                pass
            try:
                server.terminate()
            except OSError:
                pass
        proc = self._panel_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        if self.icon:
            self.icon.stop()

    # ----- menu (rebuilt each time the tray menu opens) ---------------------

    @staticmethod
    def _bind(fn, *args):
        """pystray actions must take exactly (icon, item); close over args."""
        return lambda icon, item: fn(*args)

    def build_menu(self):
        Item, Menu = self.pystray.MenuItem, self.pystray.Menu
        self.device = ping(timeout=1.0) or self.device
        name = self.device.get("name") or "daemon offline"
        items = [Item("cross-copy — %s" % name, None, enabled=False),
                 Menu.SEPARATOR]

        peers = self.fetch_peers()
        if peers:
            for peer in peers:
                plat = {"darwin": "mac", "linux": "linux"}.get(
                    peer.get("platform"), peer.get("platform") or "?")
                items.append(Item(
                    "%s (%s)" % (peer.get("name") or peer.get("id"), plat),
                    Menu(Item("Send files…",
                              self._bind(self.send_files, peer)),
                         Item("Send clipboard text",
                              self._bind(self.send_clipboard, peer)))))
        else:
            items.append(Item("No devices found", None, enabled=False))
        items.append(Menu.SEPARATOR)

        offers = self.fetch_offers() or []
        items.append(Item("Offers (%d)" % len(offers), None, enabled=False))
        for offer in offers:
            oid = offer.get("offer_id", "")
            frm = (offer.get("from") or {}).get("name") or "?"
            if offer.get("kind") == "text":
                what = "text (%d chars)" % len(offer.get("text") or "")
            else:
                n = len(offer.get("files") or [])
                what = "%d file%s" % (n, "" if n == 1 else "s")
            items.append(Item(
                "%s — %s" % (frm, what),
                Menu(Item("Accept",
                          self._bind(self.offer_action, oid, "accept")),
                     Item("Decline",
                          self._bind(self.offer_action, oid, "decline")))))
        items += [
            Menu.SEPARATOR,
            Item("Open panel", self._bind(self.open_panel)),
            Item("Open web UI", self._bind(self.open_webui)),
            Menu.SEPARATOR,
            Item("Quit", self._bind(self.quit)),
        ]
        return items

    def refresh_menu(self):
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    # ----- SSE listener ------------------------------------------------------

    def sse_loop(self):
        """Follow /api/events; refresh the tray on peers/offers changes.
        Survives daemon restarts (auto-update execv) via backoff reconnect."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                # ?client=widget tells the daemon to suppress its own OS
                # notifications while the widget owns them (popup cards).
                r = requests.get(base_url() + "/api/events?client=widget",
                                 stream=True, timeout=(3.05, 60))
                backoff = 1.0
                self.on_offers_event()  # catch up on anything missed
                self.on_peers_event()
                # chunk_size=1 is essential: with the default (512) requests
                # buffers the tiny SSE lines for many minutes, so popups
                # only fired long after every offer had already expired.
                for line in r.iter_lines(chunk_size=1, decode_unicode=True):
                    if self._stop.is_set():
                        return
                    if not line or not line.startswith("data:"):
                        continue  # heartbeats / blank keep-alives
                    try:
                        event = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if event.get("type") == "offers":
                        self.on_offers_event()
                    elif event.get("type") == "peers":
                        self.on_peers_event()
            except requests.RequestException:
                pass
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    # ----- run ---------------------------------------------------------------

    def run(self):
        Menu = self.pystray.Menu
        if sys.platform == "darwin":
            from crosscopy.macos import configure_application
            configure_application()
        self.icon = self.pystray.Icon(
            "cross-copy", icon=make_icon_image(),
            title="Cross Copy — %s" % (self.device.get("name") or "?"),
            menu=Menu(lambda: self.build_menu()))
        self.start_panel_server()  # darwin only: pre-warm the native panel
        threading.Thread(target=self.sse_loop, daemon=True).start()

        def setup(icon):
            icon.visible = True
            if sys.platform == "darwin":
                # pystray does not expose NSImage's template flag.  Marking it
                # here lets macOS recolour the glyph for light/dark menu bars.
                try:
                    icon._icon_image.setTemplate_(True)
                except (AttributeError, TypeError):
                    pass

        self.icon.run(setup=setup)


def main():
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print(INSTALL_HINT, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        # pystray picks its backend at import time; with no usable display
        # it raises backend errors (e.g. Xlib DisplayNameError), not
        # ImportError.
        print("Could not initialize a system-tray backend: %s" % e,
              file=sys.stderr)
        print("Make sure you are in a graphical session with a system tray.",
              file=sys.stderr)
        sys.exit(1)

    if not ensure_daemon():
        print("Could not start the cross-copy daemon. Check %s."
              % os.path.join(crosscopy_home(), "daemon.log"), file=sys.stderr)
        sys.exit(1)

    # Pidfile so `ccp widget install/uninstall` can find a running widget;
    # log the backend pystray picked (appindicator vs xorg matters on GNOME).
    import atexit
    import json as _json
    pidfile = os.path.join(crosscopy_home(), "widget.json")

    def _remove_pidfile():
        try:
            os.remove(pidfile)
        except OSError:
            pass

    try:
        with open(pidfile, "w") as f:
            _json.dump({"pid": os.getpid()}, f)
        atexit.register(_remove_pidfile)
    except OSError:
        pass
    print("tray backend: %s" % pystray.Icon.__module__, file=sys.stderr)

    app = WidgetApp(pystray)
    try:
        app.run()
    except KeyboardInterrupt:
        app.quit()
    except Exception as e:
        # pystray backends raise at run() when no tray is available
        # (headless session, missing appindicator, no $DISPLAY, ...).
        print("Could not start the tray icon: %s" % e, file=sys.stderr)
        print("Make sure you are in a graphical session with a system tray.\n"
              + INSTALL_HINT, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
