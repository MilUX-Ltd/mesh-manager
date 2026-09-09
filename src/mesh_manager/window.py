"""Spec 061: the screen in the application's own window. macOS uses the WebKit view the system already carries;
Windows uses the Edge web view through a library that speaks to it. Where there is neither, the caller is told
so and opens a browser instead. Part of Mesh Manager, GPL-3.0-or-later."""
import sys
import threading

from . import __version__

_keep = []          # Cocoa does not retain what Python made; the window lives here
_lock = threading.Lock()


def user_agent():
    """What the view calls itself, so the screen's log can tell the app's own requests from a browser's."""
    return f"MeshManager/{__version__} (app window)"


def _backend():
    """The name of the web view this machine can show, or None."""
    if sys.platform == "darwin":
        try:
            import WebKit  # noqa: F401
            import AppKit  # noqa: F401
            return "webkit"
        except Exception:  # noqa: BLE001
            return None
    if sys.platform.startswith("win"):
        try:
            import webview  # noqa: F401
            return "edge"
        except Exception:  # noqa: BLE001
            return None
    return None


def available():
    return _backend() is not None


def open_window(url, title="Mesh Manager", width=1180, height=820):
    """Show the screen in a window and bring it forward. Returns a handle, or None where there is no web view.
    On macOS this must be called on the main thread, which is where the menu bar's own callbacks run."""
    back = _backend()
    if back == "webkit":
        return _open_webkit(url, title, width, height)
    if back == "edge":
        return _open_edge(url, title, width, height)
    return None


def _open_webkit(url, title, width, height):
    from AppKit import (NSApp, NSBackingStoreBuffered, NSMakeRect, NSScreen, NSWindow,
                        NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
                        NSWindowStyleMaskResizable, NSWindowStyleMaskTitled)
    from Foundation import NSURL, NSURLRequest
    from WebKit import WKWebView, WKWebViewConfiguration

    with _lock:
        for w in _keep:                      # one window: show the one there is
            try:
                w["window"].makeKeyAndOrderFront_(None)
                NSApp.activateIgnoringOtherApps_(True)
                return w
            except Exception:  # noqa: BLE001
                _keep.clear()
                break
        screen = NSScreen.mainScreen()
        vis = screen.visibleFrame() if screen else NSMakeRect(0, 0, 1440, 900)
        w_, h_ = min(width, int(vis.size.width) - 40), min(height, int(vis.size.height) - 40)
        x = vis.origin.x + (vis.size.width - w_) / 2
        y = vis.origin.y + (vis.size.height - h_) / 2
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(x, y, w_, h_), style, NSBackingStoreBuffered, False)
        win.setTitle_(title)
        win.setReleasedWhenClosed_(False)    # closing hides it; Quit is what stops the bridge
        conf = WKWebViewConfiguration.alloc().init()
        view = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, w_, h_), conf)
        try:
            view.setCustomUserAgent_(user_agent())
        except Exception:  # noqa: BLE001
            pass
        view.setAutoresizingMask_(2 | 16)    # width and height follow the window
        win.setContentView_(view)
        view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
        win.makeKeyAndOrderFront_(None)
        try:
            NSApp.activateIgnoringOtherApps_(True)
        except Exception:  # noqa: BLE001
            pass
        handle = {"window": win, "view": view, "url": url}
        _keep.append(handle)
        return handle


def _open_edge(url, title, width, height):
    import webview
    with _lock:
        if _keep:
            try:
                _keep[0]["window"].show()
                return _keep[0]
            except Exception:  # noqa: BLE001
                _keep.clear()
        win = webview.create_window(title, url, width=width, height=height)
        handle = {"window": win, "url": url}
        _keep.append(handle)
        return handle


def raise_window():
    """Bring the window forward if there is one; True when there was."""
    with _lock:
        if not _keep:
            return False
        h = _keep[0]
    try:
        if _backend() == "webkit":
            from AppKit import NSApp
            h["window"].makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
        else:
            h["window"].show()
        return True
    except Exception:  # noqa: BLE001
        return False
