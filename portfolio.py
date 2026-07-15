#!/usr/bin/env python3
"""portfolio — a transparent, expandable live-stock widget for the GNOME desktop.

A frameless glass card that floats over the wallpaper (top-right by default),
shows your live portfolio value + per-holding sparklines, and grows into a big
graph-rich detail view when you click a holding. Live prices come from Yahoo
Finance (see datafeed.py) and refresh on a background thread.

Runs under XWayland (GDK_BACKEND=x11) so it can position itself, keep below
other windows, stay on all workspaces and paint a real transparent background —
the same trick claude-ask uses.

Ctrl+Shift+M drives this widget and the surf one together through the `panels`
launcher, which decides one target state and hands it here as --show / --hide.
Relaunching with no command just toggles.
"""
import os
# Native Wayland forbids self-positioning / keep-below; XWayland allows it.
os.environ.setdefault('GDK_BACKEND', 'x11')

import sys, json, signal, threading, traceback, subprocess

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gtk, Gdk, GLib, WebKit2
try:
    gi.require_version('GdkX11', '3.0')
    from gi.repository import GdkX11  # noqa: F401  (needed for X11 window XIDs)
except Exception:
    GdkX11 = None

HERE = os.path.dirname(os.path.realpath(__file__))
UI = os.path.join(HERE, 'ui.html')
CONFIG = os.path.expanduser('~/.config/portfolio/holdings.json')
STATE = os.path.expanduser('~/.local/state/portfolio')
PIDFILE = os.path.join(STATE, 'pid')
PIDFILE_WINDOW = os.path.join(STATE, 'pid-window')
CMDFILE = os.path.join(STATE, 'cmd')        # what a relaunch wants us to do
VISFILE = os.path.join(STATE, 'visible')    # 1/0, so `panels` can read our state
GEOFILE = os.path.join(STATE, 'geometry')   # where we sit, so surf can tuck beside us
REFRESH_SECS = 60

COMPACT = (380, 600)
EXPAND = (1040, 720)
MARGIN = 22                 # gap from the screen edge

sys.path.insert(0, HERE)
import datafeed

# --- accent borrowed from the wallpaper-rotation theme (matches claude-ask) ---
ORDER = ['green', 'blue', 'purple', 'red', 'gold', 'brown', 'wood']
ACCENTS = {'green': '#4ade80', 'blue': '#60a5fa', 'purple': '#c084fc',
           'red': '#f87171', 'gold': '#fbbf24', 'brown': '#d2a679', 'wood': '#deb887'}


def current_accent():
    try:
        idx = int(open(os.path.expanduser(
            '~/.local/state/desktop-rotation/index')).read().strip())
        return ACCENTS[ORDER[idx % len(ORDER)]]
    except Exception:
        return '#c084fc'


def _take_command():
    """Read + clear the command a relaunch left for us: show / hide / toggle."""
    try:
        with open(CMDFILE) as f:
            cmd = f.read().strip()
        os.remove(CMDFILE)
        return cmd or 'toggle'
    except Exception:
        return 'toggle'


class Widget:
    def __init__(self, window_mode=False):
        self.window_mode = window_mode
        self.expanded = False
        self._want_visible = True    # what the user asked for, not what X reports
        self.pidfile = PIDFILE_WINDOW if window_mode else PIDFILE
        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title('Portfolio')
        if window_mode:
            # a normal application window: title bar, taskbar, focusable, centered
            self.win.set_decorated(True)
            self.win.set_resizable(True)
            self.win.set_position(Gtk.WindowPosition.CENTER)
        else:
            # a frameless desktop widget that sits below other windows
            self.win.set_decorated(False)
            self.win.set_resizable(True)   # undecorated anyway; avoids WM recentering on resize
            self.win.set_skip_taskbar_hint(True)
            self.win.set_skip_pager_hint(True)
            self.win.set_keep_below(True)            # sit on the desktop, under windows
            self.win.set_focus_on_map(False)
            self.win.stick()                         # show on every workspace
        self.win.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.win.set_default_size(*COMPACT)

        # --- background --------------------------------------------------------
        # widget mode: real transparency (RGBA visual + transparent paint) so the
        # wallpaper shows through. window mode: a solid, opaque app background.
        if not window_mode:
            screen = self.win.get_screen()
            vis = screen.get_rgba_visual()
            if vis:
                self.win.set_visual(vis)
            self.win.set_app_paintable(True)
            self.win.connect('draw', self._clear)

        # --- webview ---------------------------------------------------------
        self.web = WebKit2.WebView()
        # transparent for the desktop widget; solid dark backdrop for the window
        self.web.set_background_color(
            Gdk.RGBA(0, 0, 0, 0) if not window_mode
            else Gdk.RGBA(0.05, 0.06, 0.09, 1.0))
        st = self.web.get_settings()
        for prop, val in (('enable-developer-extras', True),
                          ('allow-file-access-from-file-urls', True),
                          ('allow-universal-access-from-file-urls', True),
                          ('enable-write-console-messages-to-stdout', True)):
            try:
                st.set_property(prop, val)
            except Exception:
                pass
        self.win.add(self.web)

        ucm = self.web.get_user_content_manager()
        ucm.register_script_message_handler('bridge')
        ucm.connect('script-message-received::bridge', self._on_message)

        self.web.load_uri('file://' + UI)
        self.win.connect('destroy', lambda *_: self.quit())
        if not window_mode:
            # widget: closing just hides it (toggle re-shows). a normal window quits.
            self.win.connect('delete-event', lambda *a: (self.hide(), True)[1])

        self.win.show_all()
        self._reposition()
        if not window_mode:
            self._watch_layout()
            self._write_visible(True)

    # ------------------------------------------------------------------ paint
    def _clear(self, widget, cr):
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(1)  # CAIRO_OPERATOR_SOURCE
        cr.paint()
        return False

    # ------------------------------------------------------- geometry / state
    def _reposition(self):
        w, h = (EXPAND if self.expanded else COMPACT)
        if self.window_mode:
            # normal window: just resize in place, let the WM handle placement
            self.win.resize(w, h)
            return
        try:
            disp = self.win.get_display()
            mon = disp.get_primary_monitor() or disp.get_monitor(0)
            geo = mon.get_geometry()
            self._tx = geo.x + geo.width - w - MARGIN
            self._ty = geo.y + MARGIN
        except Exception:
            self._tx, self._ty = 1200, MARGIN
        self.win.resize(w, h)
        self.win.move(self._tx, self._ty)
        self._publish_geometry(w)
        # some compositors re-center a window after a programmatic resize — re-assert
        for delay in (30, 140, 320):
            GLib.timeout_add(delay, self._reassert)

    def _publish_geometry(self, w):
        # The surf widget sits immediately left of us and anchors its right edge
        # to our left one. It cannot know we doubled in width on expand, so say
        # where we are on every move; it slides aside instead of vanishing under.
        try:
            with open(GEOFILE, 'w') as f:
                json.dump({'x': self._tx, 'w': w}, f)
        except Exception:
            pass

    def _reassert(self):
        try:
            self.win.move(self._tx, self._ty)
        except Exception:
            pass
        return False

    def set_expanded(self, on):
        if on == self.expanded:
            return
        self.expanded = on
        self._reposition()

    def hide(self):
        self._want_visible = False
        self.win.hide()
        self._write_visible(False)

    def show(self):
        self._want_visible = True
        self.win.show_all()
        self._sit_on_desktop()
        self._reposition()
        self._write_visible(True)
        GLib.timeout_add(120, lambda: (self.refresh(), False)[1])

    def toggle(self):
        if self.win.get_visible():
            self.hide()
        else:
            self.show()

    def on_signal(self):
        # re-launch behaviour: a normal window just raises to front
        if self.window_mode:
            self.win.show_all()
            self.win.present()
            return
        # widget: obey the command `panels` left for us. Each widget deciding for
        # itself is what let the two drift apart (money up, surf down) after any
        # single toggle or crash.
        cmd = _take_command()
        if cmd == 'show':
            self.show()
        elif cmd == 'hide':
            self.hide()
        else:
            self.toggle()

    def _sit_on_desktop(self):
        # Below other windows, on every workspace. `panels` clears the desktop
        # for us (Show Desktop extension) when we should be seen; we never lift
        # ourselves over the user's apps. Below must be re-asserted and above
        # explicitly dropped: Mutter will otherwise hold both hints at once.
        try:
            self.win.set_keep_above(False)
            self.win.set_keep_below(True)
            self.win.stick()
        except Exception:
            pass

    def _write_visible(self, on):
        # `panels` reads this to drive both widgets to one shared state.
        try:
            with open(VISFILE, 'w') as f:
                f.write('1' if on else '0')
        except Exception:
            pass

    def _watch_layout(self):
        # Plugging in a screen reshuffles the monitor list: the primary moves, or
        # the monitor we sit on disappears and leaves us stranded off-screen.
        try:
            disp = self.win.get_display()
            disp.connect('monitor-added', lambda *_: self._layout_changed())
            disp.connect('monitor-removed', lambda *_: self._layout_changed())
        except Exception:
            pass
        try:
            screen = self.win.get_screen()
            screen.connect('monitors-changed', lambda *_: self._layout_changed())
            screen.connect('size-changed', lambda *_: self._layout_changed())
        except Exception:
            pass

    def _layout_changed(self):
        # XWayland hears about the new layout a beat after the compositor applies
        # it, and a dock settles in stages, so re-assert until it stops moving.
        for delay in (200, 900, 2000):
            GLib.timeout_add(delay, self._restore)

    def _restore(self):
        if self._want_visible:
            if not self.win.get_visible():
                self.win.show_all()
            self._sit_on_desktop()
            self._reposition()
        return False

    # --------------------------------------------------------------- JS bridge
    def js(self, script):
        try:
            self.web.run_javascript(script, None, None, None)
        except Exception:
            traceback.print_exc()

    def _on_message(self, ucm, result):
        try:
            try:
                raw = result.get_js_value().to_string()
            except Exception:
                raw = result.get_value().to_string()
            msg = json.loads(raw)
        except Exception:
            return
        a = msg.get('action')
        if a == 'ready':
            self.js('window.setAccent(%s);' % json.dumps(current_accent()))
            self.refresh()
            GLib.timeout_add_seconds(REFRESH_SECS, self._tick)
        elif a == 'expand':
            self.set_expanded(True)
        elif a == 'collapse':
            self.set_expanded(False)
        elif a == 'hide':
            self.hide()
        elif a == 'refresh':
            self.refresh()
        elif a == 'settings':
            self.open_config()
        elif a == 'drag':
            self._begin_drag()
        elif a == 'chart':
            self.fetch_chart(msg.get('ticker'), msg.get('range', '1D'),
                             msg.get('tag', msg.get('ticker')))

    def _begin_drag(self):
        try:
            seat = self.win.get_display().get_default_seat()
            _, x, y = seat.get_pointer().get_position()
            self.win.begin_move_drag(1, x, y, Gtk.get_current_event_time())
        except Exception:
            pass

    # ----------------------------------------------------------------- actions
    def open_config(self):
        try:
            subprocess.Popen(['xdg-open', CONFIG],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            traceback.print_exc()

    def _tick(self):
        self.refresh()
        return True   # keep the periodic timer alive

    def refresh(self):
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            snap = datafeed.build_snapshot()
            payload = json.dumps(snap)
            GLib.idle_add(self.js, 'window.setData(%s);' % payload)
        except Exception:
            traceback.print_exc()
            GLib.idle_add(self.js,
                          'document.getElementById("status").textContent='
                          '%s;' % json.dumps('data error — check connection'))

    def fetch_chart(self, ticker, rng, tag):
        def work():
            try:
                ch = datafeed.fetch_chart(ticker, rng)
                GLib.idle_add(self.js, 'window.setChart(%s,%s,%s,%s);' % (
                    json.dumps(tag), json.dumps(rng),
                    json.dumps(ch['t']), json.dumps(ch['c'])))
            except Exception:
                traceback.print_exc()
        threading.Thread(target=work, daemon=True).start()

    def quit(self):
        try:
            if os.path.exists(self.pidfile):
                os.remove(self.pidfile)
        except Exception:
            pass
        if not self.window_mode:
            self._write_visible(False)
            # drop our geometry so surf falls back to its default slot
            try:
                os.remove(GEOFILE)
            except Exception:
                pass
        Gtk.main_quit()


# --------------------------------------------------------------- single instance
def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def main():
    os.makedirs(STATE, exist_ok=True)
    # default = a normal application window; --widget = the desktop widget
    window_mode = '--widget' not in sys.argv
    pidfile = PIDFILE_WINDOW if window_mode else PIDFILE

    if '--quit' in sys.argv:
        try:
            os.kill(int(open(pidfile).read()), signal.SIGTERM)
        except Exception:
            pass
        return

    # what a relaunch should do to an already-running widget. `panels` passes an
    # explicit --show/--hide so both widgets end up in the same state.
    cmd = ('show' if '--show' in sys.argv else
           'hide' if '--hide' in sys.argv else 'toggle')

    # already running? second launch raises the window / drives the widget, then exits.
    if os.path.exists(pidfile):
        try:
            pid = int(open(pidfile).read().strip())
            if _alive(pid):
                if not window_mode:
                    with open(CMDFILE, 'w') as f:
                        f.write(cmd)
                os.kill(pid, signal.SIGUSR1)
                return
        except Exception:
            pass

    if not os.path.exists(CONFIG):
        print('No holdings file at', CONFIG, file=sys.stderr)

    with open(pidfile, 'w') as f:
        f.write(str(os.getpid()))

    w = Widget(window_mode=window_mode)
    if not window_mode and cmd == 'hide':
        w.hide()          # asked to be down before we even existed
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1,
                         lambda: (w.on_signal(), True)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM,
                         lambda: (w.quit(), False)[1])
    try:
        Gtk.main()
    finally:
        if os.path.exists(pidfile):
            try:
                os.remove(pidfile)
            except Exception:
                pass


if __name__ == '__main__':
    main()
