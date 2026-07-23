"""menubar.py — macOS menubar front-end for the hub.

All detection/launch logic lives in hub_core.py (stdlib-only, shared with any
other future front-end); this module is just a rumps shell on top of it.
`rumps` is imported lazily inside main() so this module stays importable
headless (e.g. for a quick sanity check or a future test) without PyObjC/rumps
installed.

Menu: one MenuItem per tool in registry.TOOLS (flat list), then a separator
and Quit. Title reflects hub_core.status(tool):
  RUNNING + kind=web     -> "● <label> (:<port>)"
  RUNNING + kind=menubar -> "● <label>"
  STOPPED                -> "○ <label>"
  NOT_INSTALLED          -> "<label> — not installed"

Grey-out mechanism: a NOT_INSTALLED tool's MenuItem has callback=None — rumps
(via NSMenuItem) renders a callback-less item disabled/greyed automatically.
That is the entire grey-out implementation; no manual styling needed.

Click behavior:
  STOPPED + installed   -> hub_core.launch(tool), then refresh immediately
  RUNNING + web          -> webbrowser.open(hub_core.open_ui(tool))
  RUNNING + menubar      -> no-op (title already shows it's running)

A rumps.Timer polls every 5s and rewrites every item's title AND callback, so
a tool that becomes installed/started/stopped between clicks reflects live
(all hub_core probes are sub-second: path stat, 0.3s TCP connect, or one
`launchctl print`).

Single-instance guard: an flock on
~/Library/Application Support/gdrive_toolkit/hub.lock. If another instance
already holds it, this one prints a message and exits 0 instead of running a
second menubar icon.
"""
from __future__ import annotations

import fcntl
import sys
import webbrowser

from ..common.config import CONFIG_DIR
from . import hub_core, registry

APP_ICON = "🧰"  # menu-bar glyph — a colour emoji is far easier to spot than a thin unicode arrow

LOCK_PATH = CONFIG_DIR / "hub.lock"

# Kept as a module global so the file object (and thus the flock) stays open
# for the lifetime of the process; closing/GC'ing it would release the lock.
_lock_file = None


def _acquire_lock():
    """Try to take an exclusive, non-blocking flock on LOCK_PATH.

    Returns the open file object (hold a reference for the process lifetime)
    on success, or None if another instance already holds it.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def _title_for(tool: dict, st) -> str:
    label = tool.get("label", tool.get("id", "tool"))
    if st == hub_core.Status.EXTERNAL:
        return "%s — external" % label
    if st == hub_core.Status.NOT_INSTALLED:
        return "%s — not installed" % label
    if st == hub_core.Status.RUNNING:
        if tool["kind"] == "web":
            port = hub_core.resolve_port(tool)
            return "● %s (:%d)" % (label, port)
        return "● %s" % label
    # STOPPED
    return "○ %s" % label


def main():
    global _lock_file

    if sys.platform != "darwin":
        raise SystemExit("gdrive-hub is macOS-only (menu bar app via rumps).")
    from ..common import _deps
    _deps.require("hub", "rumps")

    _lock_file = _acquire_lock()
    if _lock_file is None:
        print("gdrive-hub is already running (lock held) — exiting.")
        sys.exit(0)

    import rumps  # lazy: keep this module importable without PyObjC/rumps

    print("gdrive-hub started — look for the %s icon at the top-right of your menu bar." % APP_ICON,
          flush=True)
    print("If you don't see it, it may be hidden behind the notch or a crowded menu bar "
          "(try a menu-bar manager, or ⌘-drag icons to make room). This window stays open "
          "while the app runs; press Ctrl-C here to quit.", flush=True)

    class HubApp(rumps.App):
        def __init__(self):
            super().__init__(APP_ICON, quit_button=None)
            self.items = {}

            menu = []
            for tool in registry.TOOLS:
                item = rumps.MenuItem(tool["label"], callback=None)
                self.items[tool["id"]] = item
                menu.append(item)
            menu.append(None)  # separator
            menu.append(rumps.MenuItem("Quit", callback=self._quit))
            self.menu = menu

            self.refresh(None)
            self.timer = rumps.Timer(self.refresh, 5)
            self.timer.start()

        def _make_click_handler(self, tool):
            def _handler(_sender):
                self._on_click(tool)
            return _handler

        def _on_click(self, tool):
            # Re-probe at click time (never trust the cached title/status).
            # A malformed hub_tools.json entry must degrade this one click,
            # not take down the running menubar app.
            try:
                st = hub_core.status(tool)
                if st == hub_core.Status.STOPPED:
                    try:
                        hub_core.launch(tool)
                    except hub_core.HubError as e:
                        self._report_error(tool, e)
                    self.refresh(None)
                elif st == hub_core.Status.RUNNING:
                    if tool.get("kind") == "web":
                        url = hub_core.open_ui(tool)
                        if url:
                            webbrowser.open(url)
                    # kind=menubar + RUNNING: no-op, nothing to open/launch.
                # NOT_INSTALLED / EXTERNAL: unreachable — callback is None
                # (item disabled).
            except Exception as e:  # noqa: BLE001
                self._report_error(tool, e)

        def _report_error(self, tool, exc):
            try:
                rumps.alert(title="gdrive-hub", message=str(exc))
            except Exception:
                pass

        def refresh(self, _sender):
            for tool in registry.TOOLS:
                item = self.items.get(tool.get("id"))
                if item is None:
                    continue
                # One malformed entry (bad kind, missing key, ...) degrades to
                # a greyed "error" row instead of raising out of refresh() —
                # which would otherwise crash HubApp.__init__ and prevent the
                # whole menubar from starting.
                try:
                    st = hub_core.status(tool)
                    title = _title_for(tool, st)
                    inert = st in (hub_core.Status.NOT_INSTALLED, hub_core.Status.EXTERNAL)
                except Exception as e:  # noqa: BLE001
                    title = "%s — error" % tool.get("label", tool.get("id", "tool"))
                    inert = True
                    print("gdrive-hub: tool %r failed status check: %s" % (tool.get("id"), e))
                item.title = title
                if inert:
                    item.set_callback(None)          # greys the item out
                else:
                    item.set_callback(self._make_click_handler(tool))

        def _quit(self, _sender):
            rumps.quit_application()

    HubApp().run()


if __name__ == "__main__":
    main()
