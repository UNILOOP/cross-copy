"""Native macOS panel for the cross-copy widget.

`python -m crosscopy.macpanel <url>` opens a compact floating NSPanel-style
window with a WKWebView showing the glass panel page — a native Apple
window instead of a browser tab. One short-lived process per panel (same
pattern as popup.py) so AppKit never contends with pystray's run loop.

Requires PyObjC (pystray already depends on it on macOS) plus the WebKit
framework bindings (`pyobjc-framework-WebKit`, part of the `widget` extra).
Exits with code 3 when the bindings are missing so the widget can fall back
to a browser window.
"""

import sys

PANEL_W, PANEL_H = 420, 680
MARGIN = 12
DEPS_MISSING_EXIT = 3


def main():
    if len(sys.argv) < 2:
        print("usage: python -m crosscopy.macpanel <url>", file=sys.stderr)
        sys.exit(2)
    if sys.platform != "darwin":
        print("crosscopy.macpanel only runs on macOS", file=sys.stderr)
        sys.exit(2)
    url = sys.argv[1]

    try:
        import AppKit
        import Foundation
        import WebKit
    except ImportError as e:
        print("PyObjC WebKit bindings unavailable (%s) — falling back to a "
              "browser window. Install with: pip install "
              "pyobjc-framework-WebKit" % e, file=sys.stderr)
        sys.exit(DEPS_MISSING_EXIT)

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
    web.loadRequest_(Foundation.NSURLRequest.requestWithURL_(
        Foundation.NSURL.URLWithString_(url)))
    win.contentView().addSubview_(web)

    # Quit this helper process when the panel is closed.
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
