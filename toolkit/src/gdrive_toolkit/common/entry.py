"""Shared entry point for both web tools: free-port check, start the rclone rc
daemon, open a browser, run Flask (threaded). Parametrized version of the two
old `app.py` `main()` functions.

If the shared config has no `remote` set yet: an interactive TTY runs
`gdrive-setup` inline before continuing; a non-interactive caller exits with
a pointer to run it manually.
"""
from __future__ import annotations

import atexit
import os
import socket
import sys
import threading
import time
import webbrowser
from typing import Callable

from .rclone_rc import RcloneError


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _open_browser_later(url: str) -> None:
    def _go():
        time.sleep(1.2)
        webbrowser.open(url)
    threading.Thread(target=_go, daemon=True).start()


def run(create_app: Callable[[], "Flask"], tool_name: str) -> None:  # noqa: F821
    app = create_app()
    state = app.state  # type: ignore[attr-defined]
    cfg = state.cfg

    if not cfg.get("remote"):
        if sys.stdin.isatty():
            print("No rclone remote configured yet — running `gdrive-setup`...")
            from .setup_remote import main as setup_main
            setup_main()
            # Re-create the app so it picks up the remote setup_remote just wrote.
            app = create_app()
            state = app.state  # type: ignore[attr-defined]
            cfg = state.cfg
        else:
            raise SystemExit("No rclone remote configured. Run: gdrive-setup")

    host, port = "127.0.0.1", int(cfg["port"])
    if not _port_is_free(host, port):
        raise SystemExit(
            "Port %d is busy — is %s already running? "
            "Change 'port' in the config or close the other instance."
            % (port, tool_name)
        )

    try:
        state.rc.start_daemon()
    except RcloneError as e:
        raise SystemExit("Could not start rclone: %s" % e)
    atexit.register(state.rc.stop_daemon)

    url = "http://%s:%d/" % (host, port)
    print("%s → %s" % (tool_name, url))
    if os.environ.get("GDRIVE_TOOLKIT_NO_BROWSER") != "1":
        _open_browser_later(url)

    # threaded=True so the long-lived SSE stream doesn't block other requests.
    app.run(host=host, port=port, threaded=True, debug=False)
