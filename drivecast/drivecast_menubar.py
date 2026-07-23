#!/usr/bin/env python3
"""drivecast_menubar: a macOS menu-bar wrapper around the drivecast server.

Runs the SAME FastAPI app (drivecast.server.create_app) in-process, via uvicorn
in a background daemon thread, and exposes a tiny ☁ menu-bar item:

    drivecast: running on :8737   (status line)
    Open drivecast                -> opens http://127.0.0.1:<port>/
    ----
    Quit                          -> shuts the server down and exits

The server management (ServerThread) and the pre-launch checks are plain
functions/classes so the module stays importable; the rumps-specific UI lives in
`_run_app`, which imports rumps lazily (matching drive-offload's offload_app).

Run directly for a smoke test:
    ./venv/bin/python drivecast_menubar.py
Set DRIVECAST_NO_BROWSER=1 to skip auto-opening the browser (headless testing).
"""
import asyncio
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from drivecast import config as config_mod
from drivecast import localcert
from drivecast.rclone_auth import RcloneError, TokenManager
from drivecast.server import create_app

ICON = "☁"  # ☁


def _port_is_free(host, port):
    """True if we can bind host:port right now (pattern from app.py)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _preflight_ok(cfg):
    """True if rclone can currently produce a Drive token.

    Non-fatal: a False result just drives the "setup needed" status line — the
    server still starts and shows its own setup page. Any error (RcloneError or
    otherwise, e.g. a transient rate-limit) counts as "not ready".
    """
    tm = TokenManager(cfg["remote"])
    try:
        asyncio.run(tm.get_token())
        return True
    except RcloneError:
        return False
    except Exception:
        return False


def _open_browser_later(url, delay=1.2):
    """Open the browser once, after a short delay, unless suppressed."""
    if os.environ.get("DRIVECAST_NO_BROWSER") == "1":
        return

    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


class ServerThread:
    """Runs uvicorn.Server(app) in a daemon thread; stop() asks it to exit.

    When https_port/certfile/keyfile are given, a SECOND TLS listener is started
    on the same app (trusted-LAN HTTPS for iPhones). The plain-HTTP server owns
    AppState creation; the HTTPS listener runs with lifespan="off" and borrows
    app.state.dc once it exists."""

    def __init__(self, app, host, port, https_port=None, certfile=None, keyfile=None):
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.https_server = None
        self.https_thread = None
        if https_port and certfile and keyfile:
            https_config = uvicorn.Config(
                app, host="0.0.0.0", port=https_port,
                ssl_certfile=certfile, ssl_keyfile=keyfile,
                log_level="warning", access_log=False, lifespan="off")
            self.https_server = uvicorn.Server(https_config)
            self.https_thread = threading.Thread(target=self.https_server.run, daemon=True)

    def start(self):
        self.thread.start()
        if self.https_thread:
            self.https_thread.start()

    def stop(self, timeout=5.0):
        """Signal the server(s) to exit and wait (bounded) for the thread(s)."""
        self.server.should_exit = True
        if self.https_server:
            self.https_server.should_exit = True
        self.thread.join(timeout)
        if self.https_thread:
            self.https_thread.join(timeout)


# ==========================================================================
# Local HTTP helpers + menu model. Kept rumps-free so they stay importable and
# unit-testable (build_menu_spec) without a running NSApplication.
# ==========================================================================

def _api(method, url, payload=None, timeout=4.0):
    """Call the local drivecast HTTP API. Returns parsed JSON or None."""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        return json.loads(body) if body else {}
    except (urllib.error.URLError, ValueError, OSError):
        return None


def build_menu_spec(drives, selected_ids, auto_refresh, setup_ok, port, status_text=None):
    """Return a plain description of the menu (list of item dicts).

    Pure/data-only so it can be unit-tested without rumps. Item kinds:
      "status"   status line (title only)
      "action"   clickable item (key identifies the callback)
      "check"    checkable toggle (checked bool)
      "submenu"  nested {children: [item, ...]}
      "sep"      separator
    """
    selected = set(selected_ids or [])
    status = status_text or (
        "drivecast: running on :%d" % port if setup_ok else "drivecast: setup needed")

    drive_children = []
    if drives:
        for d in drives:
            drive_children.append({
                "kind": "check", "key": "drive:%s" % d["id"],
                "title": d.get("name") or d["id"],
                "checked": d["id"] in selected,
            })
    else:
        drive_children.append({"kind": "status", "title": "No drives found"})

    items = [
        {"kind": "status", "title": status},
        {"kind": "action", "key": "open", "title": "Open drivecast"},
        {"kind": "sep"},
        {"kind": "submenu", "title": "Drives to include", "children": drive_children},
        {"kind": "action", "key": "refresh", "title": "Refresh library"},
    ]
    # Per-drive refresh: one entry per SELECTED drive (user knows where they
    # uploaded; no need to rescan everything).
    refresh_children = [
        {"kind": "action", "key": "refresh_drive:%s" % d["id"],
         "title": d.get("name") or d["id"]}
        for d in (drives or []) if d["id"] in selected
    ]
    if refresh_children:
        items.append({"kind": "submenu", "title": "Refresh one drive",
                      "children": refresh_children})
    items += [
        {"kind": "check", "key": "auto_refresh", "title": "Auto-refresh on launch",
         "checked": bool(auto_refresh)},
        {"kind": "sep"},
        {"kind": "action", "key": "quit", "title": "Quit"},
    ]
    return items


# ==========================================================================
# UI (rumps). Imported lazily so the module above stays importable without it.
# ==========================================================================

def _run_app(server, port, setup_ok, url):
    import rumps

    base = "http://127.0.0.1:%d" % port

    class DrivecastApp(rumps.App):
        def __init__(self):
            super().__init__(ICON, quit_button=None)
            self._server = server
            self._url = url
            self._setup_ok = setup_ok
            self._drives = []
            self._selected = []
            self._auto_refresh = bool(config_mod.load_config().get("auto_refresh_on_startup"))
            self._rebuild_menu()
            # Poll the server for drive list + refresh progress after it binds.
            self.timer = rumps.Timer(self._on_tick, 5.0)
            self.timer.start()

        # ---- menu construction ----
        def _rebuild_menu(self, status_text=None):
            spec = build_menu_spec(self._drives, self._selected, self._auto_refresh,
                                   self._setup_ok, port, status_text)
            self.menu.clear()
            self.menu = [self._to_item(it) for it in spec]

        def _to_item(self, it):
            kind = it["kind"]
            if kind == "sep":
                return None
            if kind == "submenu":
                parent = rumps.MenuItem(it["title"])
                for child in it["children"]:
                    made = self._to_item(child)
                    if made is not None:
                        parent.add(made)
                return parent
            if kind == "status":
                return rumps.MenuItem(it["title"])
            item = rumps.MenuItem(it["title"], callback=self._dispatch)
            item._dc_key = it.get("key")
            if kind == "check":
                item.state = 1 if it.get("checked") else 0
            return item

        # ---- polling ----
        def _on_tick(self, _sender):
            threading.Thread(target=self._poll, daemon=True).start()

        def _poll(self):
            drives = _api("GET", base + "/api/drives")
            settings = _api("GET", base + "/api/settings")
            status = _api("GET", base + "/api/refresh/status")
            if drives is not None:
                self._drives = drives.get("drives", [])
            if settings is not None:
                self._selected = settings.get("selected_drives", [])
                self._auto_refresh = bool(settings.get("auto_refresh_on_startup"))
            status_text = None
            if status and status.get("running"):
                status_text = "drivecast: scanning… (%d/%d)" % (
                    status.get("scanned", 0), status.get("total", 0))
            self._rebuild_menu(status_text)

        # ---- dispatch ----
        def _dispatch(self, sender):
            key = getattr(sender, "_dc_key", None)
            if key == "open":
                self._open()
            elif key == "quit":
                self._quit()
            elif key == "refresh":
                threading.Thread(
                    target=lambda: _api("POST", base + "/api/refresh"), daemon=True).start()
            elif key and key.startswith("refresh_drive:"):
                drive_id = key[len("refresh_drive:"):]
                threading.Thread(
                    target=lambda: _api("POST", base + "/api/refresh",
                                        {"drives": [drive_id]}),
                    daemon=True).start()
            elif key == "auto_refresh":
                self._auto_refresh = not self._auto_refresh
                sender.state = 1 if self._auto_refresh else 0
                threading.Thread(
                    target=lambda: _api("POST", base + "/api/settings",
                                        {"auto_refresh_on_startup": self._auto_refresh}),
                    daemon=True).start()
            elif key and key.startswith("drive:"):
                drive_id = key[len("drive:"):]
                if drive_id in self._selected:
                    self._selected = [d for d in self._selected if d != drive_id]
                else:
                    self._selected = self._selected + [drive_id]
                sender.state = 1 if drive_id in self._selected else 0
                threading.Thread(
                    target=lambda: _api("POST", base + "/api/settings",
                                        {"selected_drives": self._selected}),
                    daemon=True).start()

        def _open(self):
            try:
                webbrowser.open(self._url)
            except Exception:
                pass

        def _quit(self):
            try:
                self._server.stop()
            except Exception:
                pass
            rumps.quit_application()

    # Open the browser once on launch (after the server has had a moment to bind).
    _open_browser_later(url)
    DrivecastApp().run()


def _notify_already_running(url):
    """Best-effort notification that another instance already holds the port."""
    try:
        import rumps
        rumps.notification(
            "drivecast", "Already running",
            "Opening the existing instance in your browser.")
    except Exception:
        pass


def main():
    cfg = config_mod.load_config()
    host = "0.0.0.0" if cfg.get("remote_access") else "127.0.0.1"
    port = int(cfg.get("port", 8737))
    # The browser URL is always loopback — "0.0.0.0" is a bind address, not
    # a navigable host.
    url = "http://127.0.0.1:%d/" % port

    # Another instance (or an old app.py) already owns the port: don't crash —
    # point the user at it and bow out.
    if not _port_is_free(host, port):
        _open_browser_later(url, delay=0.0)
        _notify_already_running(url)
        return

    setup_ok = _preflight_ok(cfg)

    # Trusted-LAN HTTPS (opt-in with remote access) so iPhones/iPads reach the
    # Wi-Fi URL despite Safari's HTTPS-Only behavior. A busy https_port only
    # disables HTTPS (the HTTP-port check above is the "already running" gate).
    https = None
    https_port = int(cfg.get("https_port", 8738))
    if cfg.get("remote_access"):
        https = localcert.ensure_certs()
        if https and not _port_is_free("0.0.0.0", https_port):
            https = None
        if https:
            cfg["_lan_https_port"] = https_port    # never persisted (not a SAVED_KEY)
            fp = localcert.ca_fingerprint()
            if fp:
                # Also surfaced in the Settings UI; log it so the user can verify
                # it against iOS's "More Details" fingerprint before installing.
                print("[drivecast] CA SHA-256 fingerprint: %s" % fp, file=sys.stderr)

    app = create_app(cfg)
    if https:
        server = ServerThread(app, host, port, https_port=https_port,
                              certfile=https[0], keyfile=https[1])
    else:
        server = ServerThread(app, host, port)
    server.start()

    _run_app(server, port, setup_ok, url)


if __name__ == "__main__":
    main()
