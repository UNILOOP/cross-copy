"""cross-copy popup cards (`python -m crosscopy.popup`).

The tray widget's own notification system: small always-on-top cards with
real Accept/Decline buttons, replacing OS notification-center toasts.
Each popup runs as its own short-lived process so tkinter never fights
pystray for a main thread.

Usage:
    python -m crosscopy.popup offer <offer_id> [--slot N] [--dry-run]
    python -m crosscopy.popup info "<title>" "<body>" [--slot N] [--dry-run]

stdlib only (tkinter + urllib).
"""

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request

DEFAULT_PORT = 7373

# Card geometry (px)
WIDTH = 340
HEIGHT_OFFER = 132
HEIGHT_INFO = 88
MARGIN = 16          # gap from the screen's top/right edges
GAP = 12             # vertical gap between stacked cards
RADIUS = 16

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
FONT = "Helvetica"

OFFER_TIMEOUT_S = 300   # matches server-side offer TTL
INFO_TIMEOUT_MS = 6000
POLL_MS = 2000
MAX_POLL_FAILURES = 5   # consecutive daemon errors before giving up


def daemon_port():
    home = os.environ.get("CROSSCOPY_HOME") or os.path.expanduser("~/.crosscopy")
    try:
        with open(os.path.join(home, "daemon.json")) as f:
            return int(json.load(f)["port"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        return int(os.environ.get("CROSSCOPY_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


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


def geometry(height, slot, screen_w, screen_h=None):
    x = screen_w - WIDTH - MARGIN
    y = MARGIN + slot * (HEIGHT_OFFER + GAP)
    return WIDTH, height, x, y


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
    frm = (offer.get("from") or {}).get("name") or "another device"

    ui = Popup(HEIGHT_OFFER, slot)
    ui.label(18, 16, "%s wants to send" % frm, size=12, bold=True)
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
                finish("Failed — see the cross-copy UI", color="#e46a76")
            elif res.get("kind") == "text":
                text = res.get("text") or ""
                try:
                    ui.root.clipboard_clear()
                    ui.root.clipboard_append(text)
                    finish("Text copied to your clipboard")
                except Exception:
                    finish("Text received")
            else:
                files = res.get("files_written") or []
                dest = os.path.dirname(files[0]) if files else "receive folder"
                finish("Saved to %s" % dest)
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


def run_info(title, body, slot):
    ui = Popup(HEIGHT_INFO, slot)
    ui.label(18, 16, title, size=12, bold=True)
    ui.label(18, 40, body, color=MUTED, size=10)
    ui.canvas.bind("<Button-1>", lambda e: ui.close())
    ui.root.bind("<Button-1>", lambda e: ui.close())
    ui.root.after(INFO_TIMEOUT_MS, ui.close)
    ui.run()
    return 0


def dry_run(args):
    """Print the computed content/geometry as JSON instead of showing a
    window (for headless testing)."""
    screen_w, screen_h = 1920, 1080
    if args.mode == "offer":
        offer = fetch_offer(args.offer_id)
        height = HEIGHT_OFFER
        content = {
            "found": bool(offer),
            "title": "%s wants to send" % ((offer or {}).get("from", {})
                                           .get("name") or "another device"),
            "body": offer_summary(offer) if offer else None,
            "buttons": ["Decline", "Accept"],
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
    for p in sub.choices.values():
        p.add_argument("--slot", type=int, default=0)
        p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.mode:
        parser.print_help()
        return 2

    if args.dry_run:
        return dry_run(args)

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("crosscopy.popup: tkinter is not available "
              "(install your platform's python3-tk package).", file=sys.stderr)
        return 1
    try:
        if args.mode == "offer":
            return run_offer(args.offer_id, max(0, args.slot))
        return run_info(args.title, args.body, max(0, args.slot))
    except Exception as e:  # no display / WM quirks — never hard-crash
        print("crosscopy.popup: could not show popup: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
