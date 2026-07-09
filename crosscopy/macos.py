"""Small macOS-only helpers for Cross Copy's native application identity."""

import io
import sys


APP_NAME = "Cross Copy"


def make_app_icon(size=1024):
    """Return the Cross Copy application icon as a Pillow image.

    The icon is generated instead of shipped as a single-resolution bitmap so
    the installer can build a proper multi-resolution ``.icns`` file.
    """
    from PIL import Image, ImageDraw

    scale = size / 1024.0

    def box(values):
        return tuple(round(value * scale) for value in values)

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # A restrained macOS-style tile with enough transparent margin for the
    # system's standard app-icon silhouette.
    draw.rounded_rectangle(box((70, 78, 954, 962)), radius=round(205 * scale),
                           fill=(20, 57, 138, 42))
    draw.rounded_rectangle(box((62, 62, 946, 946)), radius=round(205 * scale),
                           fill=(47, 111, 237, 255))

    # Two overlapping clipboard panes.  Their open corners form a subtle
    # transfer path while remaining legible at 16 px.
    stroke = max(1, round(58 * scale))
    radius = round(94 * scale)
    draw.rounded_rectangle(box((236, 226, 626, 616)), radius=radius,
                           outline=(220, 232, 255, 255), width=stroke)
    draw.rounded_rectangle(box((398, 388, 788, 778)), radius=radius,
                           fill=(47, 111, 237, 255),
                           outline=(255, 255, 255, 255), width=stroke)

    # Directional notches make the mark read as copying, not two generic
    # squares, without adding detail that turns muddy in the Dock.
    arrow = [(516, 492), (650, 492), (650, 448),
             (738, 536), (650, 624), (650, 580), (516, 580)]
    draw.polygon([box((x, y)) for x, y in arrow], fill=(255, 255, 255, 255))
    return image


def configure_application(icon_image=None):
    """Configure a Python-hosted process as a Cross Copy menu-bar app.

    This covers manual ``ccp widget`` runs and native helper processes, where
    macOS would otherwise expose the host interpreter as "Python" and put its
    application icon in the Dock.
    """
    if sys.platform != "darwin":
        return False
    try:
        import AppKit
        import Foundation

        # Framework builds of Python carry a Python.app Info.plist.  AppKit's
        # localizedName comes from that dictionary (not processName), so update
        # the in-memory metadata before creating NSApplication.
        bundle_info = Foundation.NSBundle.mainBundle().infoDictionary()
        if bundle_info is not None:
            try:
                bundle_info["CFBundleName"] = APP_NAME
                bundle_info["CFBundleDisplayName"] = APP_NAME
                bundle_info["LSUIElement"] = True
            except (KeyError, TypeError):
                pass
        Foundation.NSProcessInfo.processInfo().setProcessName_(APP_NAME)
        app = AppKit.NSApplication.sharedApplication()
        # LSUIElement from our generated bundle is lost when its launcher
        # execs the Python framework binary.  Enforce accessory mode at
        # runtime so the widget stays out of the Dock and Cmd-Tab switcher.
        app.setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)
        if icon_image is None:
            icon_image = make_app_icon(512)
        buffer = io.BytesIO()
        icon_image.save(buffer, "PNG")
        payload = buffer.getvalue()
        data = Foundation.NSData.dataWithBytes_length_(
            payload, len(payload))
        native_icon = AppKit.NSImage.alloc().initWithData_(data)
        if native_icon is not None:
            app.setApplicationIconImage_(native_icon)
        return True
    except Exception:
        # Identity polish must never stop clipboard sharing from starting.
        return False
