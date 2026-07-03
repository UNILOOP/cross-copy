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


# ---------------------------------------------------------------------------
# Local daemon plumbing (tiny re-implementation of the CLI helpers)
# ---------------------------------------------------------------------------

def crosscopy_home():
    return os.environ.get("CROSSCOPY_HOME") or os.path.expanduser("~/.crosscopy")


def daemon_port():
    try:
        with open(os.path.join(crosscopy_home(), "daemon.json")) as f:
            return int(json.load(f)["port"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
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
    Each popup is its own process so tkinter never fights pystray for the
    main thread. Returns the Popen, or None."""
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "crosscopy.popup"] + [str(a) for a in args],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return None


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


# ---------------------------------------------------------------------------
# Tray icon glyph — two overlapping rounded squares, brand blue
# ---------------------------------------------------------------------------

def make_icon_image(dark_tray=False):
    """64x64 RGBA glyph that stays recognizable at 22-32px."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
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
        self._popups = {}           # offer_id -> Popen of its popup card
        self._slots = {}            # offer_id -> stacking slot index
        self._my_sends = {}         # offer_id -> peer name (widget-initiated)
        self._panel_proc = None     # Popen of the native macOS panel

    # ----- data ------------------------------------------------------------

    def fetch_peers(self):
        data = api_get("/api/peers") or {}
        return data.get("peers") or []

    def fetch_offers(self):
        data = api_get("/api/offers") or {}  # endpoint may not exist yet
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
        for oid, proc in list(self._popups.items()):
            if proc.poll() is not None:
                self._popups.pop(oid, None)
                self._slots.pop(oid, None)

    def _spawn_offer_popup(self, offer_id):
        slot = self._alloc_slot()
        proc = spawn_popup("offer", offer_id, "--slot", slot)
        if proc is not None:
            self._popups[offer_id] = proc
            self._slots[offer_id] = slot

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
        for oid in self.diff_offers(self.fetch_offers()):
            if oid not in self._popups:
                self._spawn_offer_popup(oid)
        self._check_my_sends()
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

    def open_panel(self):
        url = "http://localhost:%d/widget" % daemon_port()
        if sys.platform == "darwin" and self._open_mac_panel(url):
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

        offers = self.fetch_offers()
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
                for line in r.iter_lines(decode_unicode=True):
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
                        self.refresh_menu()
            except requests.RequestException:
                pass
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    # ----- run ---------------------------------------------------------------

    def run(self):
        Menu = self.pystray.Menu
        self.icon = self.pystray.Icon(
            "cross-copy", icon=make_icon_image(),
            title="cross-copy — %s" % (self.device.get("name") or "?"),
            menu=Menu(lambda: self.build_menu()))
        threading.Thread(target=self.sse_loop, daemon=True).start()
        self.icon.run()


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
