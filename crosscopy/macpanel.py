"""Native macOS panel for the cross-copy widget.

Two modes:

One-shot (fallback):
    python -m crosscopy.macpanel <url>
Opens a compact floating NSPanel-style window with a WKWebView showing the
glass panel page and exits when it is closed. This is the original behavior
(a fresh process per click) — kept as a fallback, but slow: every launch
pays the PyObjC import (~1-3 s) plus the page load.

Persistent server (pre-warmed, spawned by the widget at startup):
    python -m crosscopy.macpanel --server <url>
Creates the window and loads the page immediately but keeps it ordered out
(hidden). Single-line commands are read from stdin:
    show    order the panel front (orderFrontRegardless + activate);
            reloads the page first if the initial load failed
    hide    order the panel out
    toggle  show/hide depending on current visibility
    quit    terminate the helper
The window close button hides the panel instead of terminating, so the
helper stays warm. When stdin reaches EOF (the widget died), the helper
terminates — no orphaned processes.

Requires PyObjC (pystray already depends on it on macOS) plus the WebKit
framework bindings (`pyobjc-framework-WebKit`, part of the `widget` extra).
Exits with code 3 when the bindings are missing so the widget can fall back
to a browser window.
"""

import sys
import threading

PANEL_W, PANEL_H = 420, 680
MARGIN = 12
DEPS_MISSING_EXIT = 3


def main():
    args = [a for a in sys.argv[1:] if a != "--server"]
    server_mode = "--server" in sys.argv[1:]
    if not args:
        print("usage: python -m crosscopy.macpanel [--server] <url>",
              file=sys.stderr)
        sys.exit(2)
    if sys.platform != "darwin":
        print("crosscopy.macpanel only runs on macOS", file=sys.stderr)
        sys.exit(2)
    url = args[0]

    try:
        import AppKit
        import Foundation
        import WebKit
    except ImportError as e:
        print("PyObjC WebKit bindings unavailable (%s) — falling back to a "
              "browser window. Install with: pip install "
              "pyobjc-framework-WebKit" % e, file=sys.stderr)
        sys.exit(DEPS_MISSING_EXIT)

    from crosscopy.macos import configure_application
    configure_application()
    app = AppKit.NSApplication.sharedApplication()
    # Accessory: no Dock icon, no menu bar takeover — it's a panel.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    # Top-right corner of the visible screen area (below the menu bar).
    screen = AppKit.NSScreen.mainScreen()
    if screen is not None:
        vis = screen.visibleFrame()
        x = vis.origin.x + vis.size.width - PANEL_W - MARGIN
        y = vis.origin.y + vis.size.height - PANEL_H - MARGIN
    else:
        x, y = 100, 100

    style = (AppKit.NSWindowStyleMaskTitled
             | AppKit.NSWindowStyleMaskClosable
             | AppKit.NSWindowStyleMaskResizable
             | AppKit.NSWindowStyleMaskFullSizeContentView)
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        Foundation.NSMakeRect(x, y, PANEL_W, PANEL_H),
        style, AppKit.NSBackingStoreBuffered, False)
    win.setTitle_("Cross Copy")
    win.setTitlebarAppearsTransparent_(True)
    win.setTitleVisibility_(AppKit.NSWindowTitleHidden)
    # Float above normal windows, like a status-bar popover.
    win.setLevel_(AppKit.NSFloatingWindowLevel)
    win.setReleasedWhenClosed_(False)
    win.setMinSize_(Foundation.NSMakeSize(360, 420))

    web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
        win.contentView().bounds(),
        WebKit.WKWebViewConfiguration.alloc().init())
    web.setAutoresizingMask_(
        AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    win.contentView().addSubview_(web)

    # Track page-load failures (daemon not up yet at pre-warm time) so
    # "show" can retry the load instead of presenting an error page.
    state = {"load_failed": False}

    class _NavDelegate(Foundation.NSObject):
        def webView_didFailProvisionalNavigation_withError_(self, wv, nav, err):
            state["load_failed"] = True

        def webView_didFailNavigation_withError_(self, wv, nav, err):
            state["load_failed"] = True

        def webView_didFinishNavigation_(self, wv, nav):
            state["load_failed"] = False

    nav_delegate = _NavDelegate.alloc().init()  # noqa: F841 — must stay alive
    web.setNavigationDelegate_(nav_delegate)

    def load_url():
        state["load_failed"] = False
        web.loadRequest_(Foundation.NSURLRequest.requestWithURL_(
            Foundation.NSURL.URLWithString_(url)))

    load_url()  # server mode pre-warms hidden; one-shot shows right away

    if server_mode:
        # Close button hides the panel — the helper stays warm.
        class _HideOnClose(Foundation.NSObject):
            def windowShouldClose_(self, sender):
                sender.orderOut_(None)
                return False

        win_delegate = _HideOnClose.alloc().init()  # noqa: F841 — keep alive
        win.setDelegate_(win_delegate)

        def show():
            if state["load_failed"]:
                load_url()
            win.orderFrontRegardless()
            win.makeKeyWindow()
            app.activateIgnoringOtherApps_(True)

        def hide():
            win.orderOut_(None)

        def handle(cmd):
            if cmd == "show":
                show()
            elif cmd == "hide":
                hide()
            elif cmd == "toggle":
                if win.isVisible():
                    hide()
                else:
                    show()
            elif cmd == "quit":
                app.terminate_(None)

        def stdin_reader():
            """Read commands off stdin on a background thread and dispatch
            them to the AppKit main loop. EOF = the widget died — terminate
            so no headless helper lingers."""
            queue = AppKit.NSOperationQueue.mainQueue()
            try:
                for line in sys.stdin:
                    cmd = line.strip().lower()
                    if not cmd:
                        continue
                    queue.addOperationWithBlock_(lambda c=cmd: handle(c))
                    if cmd == "quit":
                        return
            except (OSError, ValueError):
                pass
            queue.addOperationWithBlock_(
                lambda: AppKit.NSApp().terminate_(None))

        threading.Thread(target=stdin_reader, daemon=True).start()
        # Stay hidden until the first "show"/"toggle" arrives.
    else:
        # One-shot: quit this helper process when the panel is closed.
        class _Closer(Foundation.NSObject):
            def windowWillClose_(self, note):
                AppKit.NSApp().terminate_(None)

        closer = _Closer.alloc().init()
        Foundation.NSNotificationCenter.defaultCenter(
        ).addObserver_selector_name_object_(
            closer, b"windowWillClose:",
            AppKit.NSWindowWillCloseNotification, win)

        win.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)

    app.run()


if __name__ == "__main__":
    main()
