#!/usr/bin/env python3
"""Drive GNOME/Mutter's window state from the widget over XWayland.

This is a Wayland session, so wmctrl/xdotool can't help — but Mutter still
honours EWMH hints on the XWayland root window. We read `_NET_SHOWING_DESKTOP`
with `xprop` and send EWMH ClientMessages (show-desktop, activate-window) to the
root via libX11 (ctypes) — the same thing wmctrl does under the hood.
"""
import ctypes
import subprocess

_ClientMessage = 33
_SubstructureRedirectMask = 1 << 20
_SubstructureNotifyMask = 1 << 19


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int),
                ("xclient", _XClientMessageEvent),
                ("pad", ctypes.c_long * 24)]   # keep the union XEvent-sized


def _send(window, type_name, data):
    """Send an EWMH ClientMessage to the root (window=None targets the root)."""
    x = ctypes.CDLL('libX11.so.6')
    x.XOpenDisplay.restype = ctypes.c_void_p
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XDefaultRootWindow.restype = ctypes.c_ulong
    x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x.XInternAtom.restype = ctypes.c_ulong
    x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                             ctypes.c_long, ctypes.POINTER(_XEvent)]
    x.XFlush.argtypes = [ctypes.c_void_p]
    x.XCloseDisplay.argtypes = [ctypes.c_void_p]

    dpy = x.XOpenDisplay(None)
    if not dpy:
        return False
    try:
        root = x.XDefaultRootWindow(dpy)
        ev = _XEvent()
        ev.xclient.type = _ClientMessage
        ev.xclient.send_event = 1
        ev.xclient.display = dpy
        ev.xclient.window = window if window else root
        ev.xclient.message_type = x.XInternAtom(dpy, type_name.encode(), False)
        ev.xclient.format = 32
        for i, v in enumerate(data[:5]):
            ev.xclient.data[i] = v
        x.XSendEvent(dpy, root, False,
                     _SubstructureRedirectMask | _SubstructureNotifyMask,
                     ctypes.byref(ev))
        x.XFlush(dpy)
        return True
    finally:
        x.XCloseDisplay(dpy)


def is_showing_desktop():
    """True if Mutter is currently in 'show desktop' mode (desktop exposed)."""
    try:
        out = subprocess.run(['xprop', '-root', '_NET_SHOWING_DESKTOP'],
                             capture_output=True, text=True, timeout=2).stdout
        return out.strip().endswith('= 1')
    except Exception:
        return False


def set_showing_desktop(on):
    """Minimise every window and expose the desktop (on=True), or restore (False)."""
    try:
        return _send(None, '_NET_SHOWING_DESKTOP', [1 if on else 0])
    except Exception:
        return False


def activate_window(xid):
    """Un-minimise + raise a single window (EWMH _NET_ACTIVE_WINDOW)."""
    try:
        return _send(int(xid), '_NET_ACTIVE_WINDOW', [2, 0, 0])
    except Exception:
        return False


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ('on', 'off'):
        print('set', sys.argv[1], '->', set_showing_desktop(sys.argv[1] == 'on'))
    else:
        print('showing_desktop =', is_showing_desktop())
