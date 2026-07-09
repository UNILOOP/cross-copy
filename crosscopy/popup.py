"""cross-copy popup cards (`python -m crosscopy.popup`).

The tray widget's own notification system: small always-on-top cards with
real Accept/Decline buttons, replacing OS notification-center toasts.
Each popup runs as its own short-lived process so no GUI toolkit ever
fights pystray for a main thread.

Backends:
  darwin  — native AppKit cards (borderless non-activating NSPanel).
            tkinter `overrideredirect` windows are notoriously broken on
            aqua Tk (frameless windows often never appear or can't take
            clicks), so macOS renders each card with PyObjC instead
            (guaranteed present there: pystray depends on it).
  other   — tkinter (verified working on Linux WMs).
Last resort (no backend could show a window): a plain OS notification via
crosscopy.notify's platform helpers, so the user is never left in silence.

Usage:
    python -m crosscopy.popup offer <offer_id> [--slot N] [--dry-run]
    python -m crosscopy.popup info "<title>" "<body>" [--slot N] [--dry-run]
    python -m crosscopy.popup share <peer_id> <clipboard_id> \
        [--from-name NAME] [--summary TEXT] [--kind files|text] \
        [--slot N] [--dry-run]

stdlib only (tkinter + urllib), plus PyObjC on macOS.
"""

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request

DEFAULT_PORT = 7373

# Card geometry (px) — tkinter backend
WIDTH = 340
HEIGHT_OFFER = 132
HEIGHT_INFO = 88
MARGIN = 16          # gap from the screen's top/right edges
GAP = 12             # vertical gap between stacked cards
RADIUS = 16

# Card geometry (pt) — AppKit backend (native controls need more room)
MAC_WIDTH = 360
MAC_HEIGHT_OFFER = 150
MAC_HEIGHT_INFO = 96
MAC_RADIUS = 14.0

# Liquid-glass-inspired palette (within tkinter's limits: flat dark card,
# soft top highlight line, brand accent; -alpha translucency when the WM
# honors it).
BG = "#10131c"        # window bg — reads as the card's "shadow" corners
CARD = "#1d2230"
CARD_HI = "#2a3145"   # specular top edge
ACCENT = "#2f6fed"
ACCENT_ACTIVE = "#1e5cd6"
TEXT = "#edf1fa"
MUTED = "#9aa3b5"
OK = "#3ecf7a"
FAIL = "#e46a76"
FONT = "Helvetica"

FAIL_TEXT = "Failed — see the cross-copy UI"

OFFER_TIMEOUT_S = 300   # matches server-side offer TTL
INFO_TIMEOUT_MS = 6000
SHARE_TIMEOUT_MS = 60000  # clipboard-share cards auto-dismiss after 60 s
POLL_MS = 2000
MAX_POLL_FAILURES = 5   # consecutive daemon errors before giving up
DEFAULT_RECEIVE_DIR = "~/Downloads/cross-copy"


# ---------------------------------------------------------------------------
# Shared plumbing (both backends + --dry-run)
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


def receive_dir():
    """Where "Save here" puts files: the daemon's configured receive_dir
    (read from config.json — same machine, same home), expanded locally.
    /api/paste requires an explicit absolute 'dest'."""
    raw = None
    try:
        with open(os.path.join(crosscopy_home(), "config.json")) as f:
            raw = json.load(f).get("receive_dir")
    except (OSError, ValueError, AttributeError):
        pass
    if not raw or not isinstance(raw, str):
        raw = DEFAULT_RECEIVE_DIR
    return os.path.abspath(os.path.expanduser(raw))


def api(path, body=None, timeout=10):
    """GET (body None) or POST json to the local daemon. Returns a dict,
    or None on any error."""
    url = "http://127.0.0.1:%d%s" % (daemon_port(), path)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def fetch_offer(offer_id):
    data = api("/api/offers") or {}
    for offer in data.get("offers") or []:
        if offer.get("offer_id") == offer_id:
            return offer
    return None


def offer_title(offer):
    frm = ((offer or {}).get("from") or {}).get("name") or "another device"
    return "%s wants to send" % frm


def offer_summary(offer):
    if offer.get("kind") == "text":
        text = offer.get("text") or ""
        preview = " ".join(text.split())[:48]
        if len(text) > 48:
            preview += "…"
        return 'text (%d chars) "%s"' % (len(text), preview)
    files = offer.get("files") or []
    total = offer.get("total_size") or 0
    units = ["B", "KB", "MB", "GB"]
    n, i = float(total), 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    size = "%d B" % total if i == 0 else "%.1f %s" % (n, units[i])
    return "%d file%s · %s" % (len(files), "" if len(files) == 1 else "s", size)


def saved_message(res, fallback_dest=None):
    """Status line after a successful files accept/paste."""
    files = (res or {}).get("files_written") or []
    if files:
        return "Saved to %s" % os.path.dirname(files[0])
    return "Saved to %s" % (fallback_dest or "receive folder")


def slot_offset(slot, step=None):
    """Vertical offset (from the top screen edge) of a card in `slot`."""
    if step is None:
        step = HEIGHT_OFFER + GAP
    return MARGIN + slot * step


def geometry(height, slot, screen_w, screen_h=None, width=WIDTH, step=None):
    """Top-left-origin (w, h, x, y) for a card of `height` in `slot`."""
    x = screen_w - width - MARGIN
    y = slot_offset(slot, step)
    return width, height, x, y


# ---------------------------------------------------------------------------
# tkinter backend (Linux and any non-darwin platform)
# ---------------------------------------------------------------------------

def rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class Popup(object):
    def __init__(self, height, slot):
        import tkinter
        self.tk = tkinter
        self.root = tkinter.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        try:  # translucency where the WM supports it; opaque otherwise
            self.root.attributes("-alpha", 0.94)
        except Exception:
            pass
        w, h, x, y = geometry(height, slot, self.root.winfo_screenwidth())
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.root.configure(bg=BG)
        self.canvas = tkinter.Canvas(self.root, width=w, height=h, bg=BG,
                                     highlightthickness=0, bd=0)
        self.canvas.pack()
        # glass card: soft dark slab + specular top highlight + hairline
        rounded_rect(self.canvas, 1, 1, w - 2, h - 2, RADIUS,
                     fill=CARD, outline=CARD_HI)
        self.canvas.create_line(RADIUS, 3, w - RADIUS, 3,
                                fill=CARD_HI, width=1)
        self.root.deiconify()

    def label(self, x, y, text, color=TEXT, size=11, bold=False, anchor="nw"):
        return self.canvas.create_text(
            x, y, text=text, fill=color, anchor=anchor, width=WIDTH - 2 * x,
            font=(FONT, size, "bold" if bold else "normal"))

    def button(self, text, command, primary=False):
        btn = self.tk.Button(
            self.root, text=text, command=command, bd=0,
            highlightthickness=0, padx=14, pady=3, cursor="hand2",
            font=(FONT, 10, "bold"),
            bg=ACCENT if primary else CARD_HI,
            fg="#ffffff" if primary else TEXT,
            activebackground=ACCENT_ACTIVE if primary else "#39415a",
            activeforeground="#ffffff")
        return btn

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def run_offer(offer_id, slot):
    offer = fetch_offer(offer_id)
    if not offer:
        return 0  # already resolved/expired — nothing to show

    ui = Popup(HEIGHT_OFFER, slot)
    ui.label(18, 16, offer_title(offer), size=12, bold=True)
    ui.label(18, 40, offer_summary(offer), color=MUTED, size=10)
    status_id = ui.label(18, 100, "", color=MUTED, size=10)

    state = {"busy": False, "failures": 0, "result": []}

    def finish(message, color=OK, delay=2000):
        for b in (accept_btn, decline_btn):
            b.place_forget()
        ui.canvas.itemconfigure(status_id, text=message, fill=color)
        ui.root.after(delay, ui.close)

    def do_accept():
        if state["busy"]:
            return
        state["busy"] = True
        accept_btn.configure(state="disabled", text="Saving…")
        decline_btn.configure(state="disabled")

        def work():
            state["result"].append(
                api("/api/offers/%s/accept" % offer_id, body={}, timeout=600))

        threading.Thread(target=work, daemon=True).start()

        def poll_result():
            if not state["result"]:
                ui.root.after(200, poll_result)
                return
            res = state["result"][0]
            if res is None:
                finish(FAIL_TEXT, color=FAIL)
            elif res.get("kind") == "text":
                text = res.get("text") or ""
                try:
                    ui.root.clipboard_clear()
                    ui.root.clipboard_append(text)
                    finish("Text copied to your clipboard")
                except Exception:
                    finish("Text received")
            else:
                finish(saved_message(res))
        poll_result()

    def do_decline():
        if state["busy"]:
            return
        state["busy"] = True
        api("/api/offers/%s/decline" % offer_id, body={}, timeout=10)
        ui.close()

    decline_btn = ui.button("Decline", do_decline)
    accept_btn = ui.button("Accept", do_accept, primary=True)
    decline_btn.place(x=WIDTH - 170, y=HEIGHT_OFFER - 42, width=72, height=26)
    accept_btn.place(x=WIDTH - 90, y=HEIGHT_OFFER - 42, width=72, height=26)

    def watch():
        """Auto-close when the offer disappears (resolved elsewhere or
        expired). Survives daemon restarts: transient errors are tolerated,
        repeated ones close the card."""
        if state["busy"]:
            return
        data = api("/api/offers", timeout=4)
        if data is None:
            state["failures"] += 1
            if state["failures"] >= MAX_POLL_FAILURES:
                ui.close()
                return
        else:
            state["failures"] = 0
            ids = [o.get("offer_id") for o in data.get("offers") or []]
            if offer_id not in ids:
                ui.close()
                return
        ui.root.after(POLL_MS, watch)

    ui.root.after(POLL_MS, watch)
    ui.root.after(OFFER_TIMEOUT_S * 1000, ui.close)
    ui.run()
    return 0


def run_share(peer_id, clipboard_id, from_name, summary, kind, slot):
    """Interactive card for a peer's newly shared clipboard:
    "machine-b is sharing" + "3 files (2.1 MB)" with [Save here]/[Get text]
    and [Dismiss]. Save posts /api/paste pinned to this peer+clipboard_id."""
    ui = Popup(HEIGHT_OFFER, slot)
    ui.label(18, 16, "%s is sharing" % from_name, size=12, bold=True)
    ui.label(18, 40, summary, color=MUTED, size=10)
    status_id = ui.label(18, 100, "", color=MUTED, size=10)

    state = {"busy": False, "result": []}

    def finish(message, color=OK, delay=2500):
        for b in (save_btn, dismiss_btn):
            b.place_forget()
        ui.canvas.itemconfigure(status_id, text=message, fill=color)
        ui.root.after(delay, ui.close)

    def do_save():
        if state["busy"]:
            return
        state["busy"] = True
        save_btn.configure(state="disabled", text="Saving…")
        dismiss_btn.configure(state="disabled")
        dest = receive_dir()
        body = {"peer_id": peer_id, "clipboard_id": clipboard_id}
        if kind != "text":  # text pastes ignore dest; files require it
            body["dest"] = dest

        def work():
            state["result"].append(api("/api/paste", body=body, timeout=600))

        threading.Thread(target=work, daemon=True).start()

        def poll_result():
            if not state["result"]:
                ui.root.after(200, poll_result)
                return
            res = state["result"][0]
            if res is None:
                finish(FAIL_TEXT, color=FAIL)
            elif res.get("kind") == "text":
                try:
                    ui.root.clipboard_clear()
                    ui.root.clipboard_append(res.get("text") or "")
                    finish("Text copied to your clipboard")
                except Exception:
                    finish("Text received")
            else:
                finish(saved_message(res, dest))
        poll_result()

    dismiss_btn = ui.button("Dismiss", ui.close)
    save_btn = ui.button("Get text" if kind == "text" else "Save here",
                         do_save, primary=True)
    dismiss_btn.place(x=WIDTH - 178, y=HEIGHT_OFFER - 42, width=76, height=26)
    save_btn.place(x=WIDTH - 98, y=HEIGHT_OFFER - 42, width=80, height=26)

    def expire():
        if not state["busy"]:  # never yank the card mid-save
            ui.close()

    ui.root.after(SHARE_TIMEOUT_MS, expire)
    ui.run()
    return 0


def run_info(title, body, slot):
    ui = Popup(HEIGHT_INFO, slot)
    ui.label(18, 16, title, size=12, bold=True)
    ui.label(18, 40, body, color=MUTED, size=10)
    ui.canvas.bind("<Button-1>", lambda e: ui.close())
    ui.root.bind("<Button-1>", lambda e: ui.close())
    ui.root.after(INFO_TIMEOUT_MS, ui.close)
    ui.run()
    return 0


def run_tk(args):
    """tkinter backend entry point (raises when tkinter/display is broken
    so main() can fall back)."""
    import tkinter  # noqa: F401 — fail fast, before any daemon traffic
    if args.mode == "offer":
        return run_offer(args.offer_id, args.slot)
    if args.mode == "share":
        return run_share(args.peer_id, args.clipboard_id, args.from_name,
                         args.summary, args.kind, args.slot)
    return run_info(args.title, args.body, args.slot)


# ---------------------------------------------------------------------------
# AppKit backend (macOS) — PyObjC, one NSPanel card per process.
#
# Deliberately conservative bridging: only target/selector timers and
# actions (no block APIs except NSOperationQueue.addOperationWithBlock_,
# which macpanel.py already uses in production on macOS).
# ---------------------------------------------------------------------------

def run_mac(args):
    import AppKit
    import Foundation

    # ----- bridging helpers -------------------------------------------------

    def ns_color(hex_color, alpha=1.0):
        r = int(hex_color[1:3], 16) / 255.0
        g = int(hex_color[3:5], 16) / 255.0
        b = int(hex_color[5:7], 16) / 255.0
        return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
            r, g, b, alpha)

    def dispatch_main(fn):
        """Run `fn` on the AppKit main thread (UI must never be touched
        from our HTTP worker threads)."""
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(fn)

    class _Callback(Foundation.NSObject):
        """Generic target for NSButton actions and NSTimers: invokes the
        plain-Python callable stashed on the instance. Selector-based
        (universally bridged), no block APIs."""

        def invoke_(self, sender):  # selector "invoke:" — sender/timer arg
            cb = getattr(self, "callback", None)
            if cb is not None:
                cb()

    class _CardPanel(AppKit.NSPanel):
        # Borderless windows refuse key status by default; allow it so the
        # buttons behave like normal controls.
        def canBecomeKeyWindow(self):
            return True

    class _CardButton(AppKit.NSButton):
        def acceptsFirstMouse_(self, event):
            return True  # first click hits the button even if not key

    class _ClickView(AppKit.NSView):
        """Content view for info cards: click anywhere dismisses."""

        def mouseDown_(self, event):
            AppKit.NSApp().terminate_(None)

    from crosscopy.macos import configure_application
    configure_application()
    app = AppKit.NSApplication.sharedApplication()
    # Accessory: no Dock icon, no menu bar takeover — it's a toast card.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    keep = []  # strong refs: NSButton does NOT retain its target

    def make_target(fn):
        target = _Callback.alloc().init()
        target.callback = fn
        keep.append(target)
        return target

    def start_timer(seconds, fn, repeats=False):
        t = (AppKit.NSTimer.
             scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                 float(seconds), make_target(fn), "invoke:", None,
                 bool(repeats)))
        keep.append(t)
        return t

    def close_app():
        app.terminate_(None)

    def copy_text_to_pasteboard(text):
        try:
            pb = AppKit.NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
            return True
        except Exception:
            return False

    # ----- card window --------------------------------------------------

    class MacCard(object):
        def __init__(self, height, slot, click_dismiss=False):
            width = MAC_WIDTH
            screen = AppKit.NSScreen.mainScreen()
            if screen is not None:
                vis = screen.visibleFrame()
                right = vis.origin.x + vis.size.width
                top = vis.origin.y + vis.size.height
            else:
                right, top = 1440.0, 900.0
            # Same slot math as the tkinter backend, converted to AppKit's
            # bottom-left origin.
            y_off = slot_offset(slot, MAC_HEIGHT_OFFER + GAP)
            x = right - width - MARGIN
            y = top - y_off - height

            style = AppKit.NSWindowStyleMaskBorderless
            try:  # clicks shouldn't steal focus from the user's app
                style |= AppKit.NSWindowStyleMaskNonactivatingPanel
            except AttributeError:
                pass
            win = (_CardPanel.alloc().
                   initWithContentRect_styleMask_backing_defer_(
                       Foundation.NSMakeRect(x, y, width, height), style,
                       AppKit.NSBackingStoreBuffered, False))
            win.setLevel_(AppKit.NSStatusWindowLevel)
            win.setOpaque_(False)
            win.setBackgroundColor_(AppKit.NSColor.clearColor())
            win.setHasShadow_(True)
            win.setReleasedWhenClosed_(False)
            try:  # show over full-screen apps / every Space
                win.setCollectionBehavior_(
                    AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                    | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
            except AttributeError:
                pass
            try:  # dark controls regardless of the system theme (10.14+)
                win.setAppearance_(AppKit.NSAppearance.appearanceNamed_(
                    AppKit.NSAppearanceNameDarkAqua))
            except Exception:
                pass

            view_cls = _ClickView if click_dismiss else AppKit.NSView
            view = view_cls.alloc().initWithFrame_(
                Foundation.NSMakeRect(0, 0, width, height))
            view.setWantsLayer_(True)
            layer = view.layer()
            if layer is not None:  # rounded dark glass card
                layer.setCornerRadius_(MAC_RADIUS)
                layer.setMasksToBounds_(True)
                layer.setBackgroundColor_(ns_color(CARD, 0.92).CGColor())
                layer.setBorderWidth_(1.0)
                layer.setBorderColor_(ns_color(CARD_HI, 0.9).CGColor())
            win.setContentView_(view)

            self.win = win
            self.view = view
            self.width = width
            self.height = height
            keep.append(win)
            keep.append(view)

        def label(self, x, y_top, w, h, text, color=TEXT, size=11,
                  bold=False, wraps=False):
            """Static text at (x, y_top) measured from the card's top edge."""
            field = AppKit.NSTextField.alloc().initWithFrame_(
                Foundation.NSMakeRect(x, self.height - y_top - h, w, h))
            field.setStringValue_(str(text))
            field.setBezeled_(False)
            field.setBordered_(False)
            field.setDrawsBackground_(False)
            field.setEditable_(False)
            field.setSelectable_(False)
            field.setTextColor_(ns_color(color))
            field.setFont_(AppKit.NSFont.boldSystemFontOfSize_(size) if bold
                           else AppKit.NSFont.systemFontOfSize_(size))
            cell = field.cell()
            if cell is not None:
                if wraps:
                    cell.setWraps_(True)
                else:
                    cell.setWraps_(False)
                    cell.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
            self.view.addSubview_(field)
            keep.append(field)
            return field

        def set_status(self, field, message, color):
            field.setStringValue_(message)
            field.setTextColor_(ns_color(color))

        def button(self, title, fn, x, width=84, primary=False):
            btn = _CardButton.alloc().initWithFrame_(
                Foundation.NSMakeRect(x, 12, width, 28))
            btn.setTitle_(title)
            try:
                btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
            except AttributeError:  # very old PyObjC constant name
                btn.setBezelStyle_(AppKit.NSRoundedBezelStyle)
            btn.setFont_(AppKit.NSFont.systemFontOfSize_(12))
            btn.setTarget_(make_target(fn))
            btn.setAction_("invoke:")
            if primary:
                btn.setKeyEquivalent_("\r")  # default (accent-tinted) button
            self.view.addSubview_(btn)
            keep.append(btn)
            return btn

        def show(self):
            # No makeKey / no app activation: appear like a notification,
            # never steal the user's focus.
            self.win.orderFrontRegardless()

    # ----- modes ---------------------------------------------------------

    slot = args.slot

    def mac_offer():
        offer = fetch_offer(args.offer_id)
        if not offer:
            return 0  # already resolved/expired — nothing to show
        offer_id = args.offer_id

        card = MacCard(MAC_HEIGHT_OFFER, slot)
        card.label(18, 14, MAC_WIDTH - 36, 20, offer_title(offer),
                   size=13, bold=True)
        card.label(18, 38, MAC_WIDTH - 36, 34, offer_summary(offer),
                   color=MUTED, size=11, wraps=True)
        status = card.label(18, MAC_HEIGHT_OFFER - 34, MAC_WIDTH - 36, 18,
                            "", color=MUTED, size=11)

        state = {"busy": False, "failures": 0, "polling": False}

        def finish(message, color=OK, delay=2.0):
            accept_btn.setHidden_(True)
            decline_btn.setHidden_(True)
            card.set_status(status, message, color)
            start_timer(delay, close_app)

        def do_accept():
            if state["busy"]:
                return
            state["busy"] = True
            accept_btn.setEnabled_(False)
            accept_btn.setTitle_("Saving…")
            decline_btn.setEnabled_(False)

            def work():  # HTTP off the main thread; UI back on it
                res = api("/api/offers/%s/accept" % offer_id, body={},
                          timeout=600)

                def apply():
                    if res is None:
                        finish(FAIL_TEXT, color=FAIL)
                    elif res.get("kind") == "text":
                        ok = copy_text_to_pasteboard(res.get("text") or "")
                        finish("Text copied to your clipboard" if ok
                               else "Text received")
                    else:
                        finish(saved_message(res))
                dispatch_main(apply)

            threading.Thread(target=work, daemon=True).start()

        def do_decline():
            if state["busy"]:
                return
            state["busy"] = True
            accept_btn.setEnabled_(False)
            decline_btn.setEnabled_(False)

            def work():
                api("/api/offers/%s/decline" % offer_id, body={}, timeout=10)
                dispatch_main(close_app)

            threading.Thread(target=work, daemon=True).start()

        decline_btn = card.button("Decline", do_decline, MAC_WIDTH - 184)
        accept_btn = card.button("Accept", do_accept, MAC_WIDTH - 96,
                                 primary=True)

        def watch():
            """Auto-close when the offer disappears (resolved elsewhere or
            expired). Same tolerance rules as the tkinter backend."""
            if state["busy"] or state["polling"]:
                return
            state["polling"] = True

            def work():
                data = api("/api/offers", timeout=4)

                def apply():
                    state["polling"] = False
                    if state["busy"]:
                        return
                    if data is None:
                        state["failures"] += 1
                        if state["failures"] >= MAX_POLL_FAILURES:
                            close_app()
                    else:
                        state["failures"] = 0
                        ids = [o.get("offer_id")
                               for o in data.get("offers") or []]
                        if offer_id not in ids:
                            close_app()
                dispatch_main(apply)

            threading.Thread(target=work, daemon=True).start()

        start_timer(POLL_MS / 1000.0, watch, repeats=True)
        start_timer(OFFER_TIMEOUT_S, close_app)
        card.show()
        app.run()
        return 0

    def mac_share():
        card = MacCard(MAC_HEIGHT_OFFER, slot)
        card.label(18, 14, MAC_WIDTH - 36, 20, "%s is sharing" % args.from_name,
                   size=13, bold=True)
        card.label(18, 38, MAC_WIDTH - 36, 34, args.summary,
                   color=MUTED, size=11, wraps=True)
        status = card.label(18, MAC_HEIGHT_OFFER - 34, MAC_WIDTH - 36, 18,
                            "", color=MUTED, size=11)

        state = {"busy": False}

        def finish(message, color=OK, delay=2.5):
            save_btn.setHidden_(True)
            dismiss_btn.setHidden_(True)
            card.set_status(status, message, color)
            start_timer(delay, close_app)

        def do_save():
            if state["busy"]:
                return
            state["busy"] = True
            save_btn.setEnabled_(False)
            save_btn.setTitle_("Saving…")
            dismiss_btn.setEnabled_(False)
            dest = receive_dir()
            body = {"peer_id": args.peer_id, "clipboard_id": args.clipboard_id}
            if args.kind != "text":  # text pastes ignore dest; files need it
                body["dest"] = dest

            def work():
                res = api("/api/paste", body=body, timeout=600)

                def apply():
                    if res is None:
                        finish(FAIL_TEXT, color=FAIL)
                    elif res.get("kind") == "text":
                        ok = copy_text_to_pasteboard(res.get("text") or "")
                        finish("Text copied to your clipboard" if ok
                               else "Text received")
                    else:
                        finish(saved_message(res, dest))
                dispatch_main(apply)

            threading.Thread(target=work, daemon=True).start()

        dismiss_btn = card.button("Dismiss", close_app, MAC_WIDTH - 192,
                                  width=86)
        save_btn = card.button(
            "Get text" if args.kind == "text" else "Save here",
            do_save, MAC_WIDTH - 102, width=88, primary=True)

        def expire():
            if not state["busy"]:  # never yank the card mid-save
                close_app()

        start_timer(SHARE_TIMEOUT_MS / 1000.0, expire)
        card.show()
        app.run()
        return 0

    def mac_info():
        card = MacCard(MAC_HEIGHT_INFO, slot, click_dismiss=True)
        card.label(18, 14, MAC_WIDTH - 36, 20, args.title, size=13, bold=True)
        card.label(18, 38, MAC_WIDTH - 36, 40, args.body, color=MUTED,
                   size=11, wraps=True)
        start_timer(INFO_TIMEOUT_MS / 1000.0, close_app)
        card.show()
        app.run()
        return 0

    if args.mode == "offer":
        return mac_offer()
    if args.mode == "share":
        return mac_share()
    return mac_info()


# ---------------------------------------------------------------------------
# Last-resort fallback: plain OS notification (osascript / notify-send).
# We reuse crosscopy.notify's platform helpers *directly*, bypassing its
# widget-connected suppression — we ARE the widget's popup, so if we can't
# draw a card the OS toast is the only voice left.
# ---------------------------------------------------------------------------

def fallback_text(args):
    """(title, body) for the OS-notification fallback, or None when there
    is nothing worth announcing (offer already gone)."""
    if args.mode == "offer":
        offer = fetch_offer(args.offer_id)
        if not offer:
            return None  # resolved/expired — a toast would be noise
        return ("cross-copy",
                "%s %s — accept in the cross-copy UI or run 'ccp accept'."
                % (offer_title(offer), offer_summary(offer)))
    if args.mode == "share":
        return ("cross-copy",
                "%s is sharing %s — get it with 'ccp paste'."
                % (args.from_name, args.summary))
    return (args.title, args.body)


def fallback_os_notification(title, body):
    """Fire a plain OS notification. True when a helper was invoked."""
    try:
        if sys.platform == "darwin":
            from crosscopy.notify import _notify_macos
            _notify_macos(title, body)
            return True
        if sys.platform.startswith("linux"):
            from crosscopy.notify import _notify_linux
            _notify_linux(title, body)
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# --dry-run + entry point
# ---------------------------------------------------------------------------

def dry_run(args):
    """Print the computed content/geometry as JSON instead of showing a
    window (for headless testing). Platform-independent (tkinter metrics)."""
    screen_w, screen_h = 1920, 1080
    if args.mode == "offer":
        offer = fetch_offer(args.offer_id)
        height = HEIGHT_OFFER
        content = {
            "found": bool(offer),
            "title": offer_title(offer or {}),
            "body": offer_summary(offer) if offer else None,
            "buttons": ["Decline", "Accept"],
        }
    elif args.mode == "share":
        height = HEIGHT_OFFER
        content = {
            "title": "%s is sharing" % args.from_name,
            "body": args.summary,
            "buttons": ["Dismiss",
                        "Get text" if args.kind == "text" else "Save here"],
            "dest": receive_dir() if args.kind != "text" else None,
        }
    else:
        height = HEIGHT_INFO
        content = {"title": args.title, "body": args.body, "buttons": []}
    w, h, x, y = geometry(height, args.slot, screen_w, screen_h)
    print(json.dumps({"mode": args.mode, "slot": args.slot,
                      "geometry": {"w": w, "h": h, "x": x, "y": y},
                      "content": content}, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="crosscopy.popup")
    sub = parser.add_subparsers(dest="mode")
    p = sub.add_parser("offer")
    p.add_argument("offer_id")
    p = sub.add_parser("info")
    p.add_argument("title")
    p.add_argument("body")
    p = sub.add_parser("share")
    p.add_argument("peer_id")
    p.add_argument("clipboard_id")
    p.add_argument("--from-name", dest="from_name", default="peer")
    p.add_argument("--summary", default="new shared content")
    p.add_argument("--kind", choices=("files", "text"), default="files")
    for p in sub.choices.values():
        p.add_argument("--slot", type=int, default=0)
        p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.mode:
        parser.print_help()
        return 2
    args.slot = max(0, args.slot)

    if args.dry_run:
        return dry_run(args)

    # Backend chain: native AppKit on macOS (tkinter overrideredirect is
    # broken on aqua Tk), tkinter everywhere else / as the macOS fallback,
    # and finally a plain OS notification so the user is never left silent.
    backends = []
    if sys.platform == "darwin":
        backends.append(("AppKit", run_mac))
    backends.append(("tkinter", run_tk))

    errors = []
    for name, runner in backends:
        try:
            return runner(args)
        except Exception as e:
            errors.append("%s backend failed: %r" % (name, e))

    for msg in errors:  # stderr lands in widget.log via spawn_popup
        print("crosscopy.popup: %s" % msg, file=sys.stderr)
    text = fallback_text(args)
    if text and fallback_os_notification(*text):
        print("crosscopy.popup: fell back to an OS notification",
              file=sys.stderr)
        return 0
    print("crosscopy.popup: could not show a popup card or an OS "
          "notification.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
