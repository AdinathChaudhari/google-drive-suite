#!/usr/bin/env python3
"""drivecast entry point.

Preflight-checks rclone, starts uvicorn on 127.0.0.1:<port>, and opens the
browser (unless DRIVECAST_NO_BROWSER=1).
"""
import logging
import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from drivecast import config as config_mod
from drivecast import localcert
from drivecast.player import detect_player
from drivecast.rclone_auth import RcloneError, TokenManager
from drivecast.server import create_app


def _port_is_free(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _preflight_rclone(cfg):
    """Non-fatal preflight: warn if rclone can't produce a token.

    We still start the server (it shows a friendly setup page) but print a
    clear console message.
    """
    tm = TokenManager(cfg["remote"])
    import asyncio
    try:
        asyncio.run(tm.get_token())
        print("[drivecast] rclone remote '%s' OK." % cfg["remote"])
    except RcloneError as e:
        print("[drivecast] WARNING: %s" % e, file=sys.stderr)
        print("[drivecast] Starting anyway; the web UI will show setup instructions.",
              file=sys.stderr)


def _report_player(cfg):
    kind, path = detect_player(cfg.get("player", "auto"))
    if kind == "mpv":
        print("[drivecast] Player: mpv (%s) — resume tracking enabled." % path)
    elif kind == "iina":
        print("[drivecast] Player: IINA (%s) — resume tracking enabled." % path)
    elif kind == "vlc":
        print("[drivecast] Player: VLC (%s) — no resume tracking; install mpv for that "
              "(brew install mpv)." % path)
    else:
        print("[drivecast] WARNING: no player found. Install mpv: brew install mpv",
              file=sys.stderr)


def _start_https_thread(app, port, certfile, keyfile):
    """Run a second uvicorn listener with TLS in a daemon thread, sharing the
    same app. lifespan="off": the plain-HTTP server owns AppState creation, this
    listener just borrows app.state.dc once it exists (a request in the first
    ~1s could 500 before it's set — self-heals). access_log=False: tokens ride
    in query strings and must never reach logs."""
    config = uvicorn.Config(app, host="0.0.0.0", port=port,
                            ssl_certfile=certfile, ssl_keyfile=keyfile,
                            log_level="info", access_log=False, lifespan="off")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()


def _open_browser_later(url):
    if os.environ.get("DRIVECAST_NO_BROWSER") == "1":
        return

    def _open():
        time.sleep(1.2)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Line-buffer stdout/stderr so console messages appear promptly even when
    # output is redirected to a file/pipe.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    cfg = config_mod.load_config()
    host = "0.0.0.0" if cfg.get("remote_access") else "127.0.0.1"
    port = int(cfg.get("port", 8737))

    if not _port_is_free(host, port):
        print("[drivecast] ERROR: port %d on %s is already in use. "
              "Change \"port\" in config.json or stop the other process."
              % (port, host), file=sys.stderr)
        sys.exit(1)

    _preflight_rclone(cfg)
    _report_player(cfg)

    # Trusted-LAN HTTPS (opt-in with remote access): a second TLS listener so
    # iPhones/iPads reach the Wi-Fi URL despite Safari's HTTPS-Only behavior.
    # Failure (no openssl, busy port) silently leaves plain HTTP as-is.
    https = None
    hp = int(cfg.get("https_port", 8738))
    if cfg.get("remote_access"):
        https = localcert.ensure_certs()      # self-detects the LAN IP
        if https and not _port_is_free("0.0.0.0", hp):
            print("[drivecast] WARNING: https_port %d busy; LAN HTTPS disabled." % hp,
                  file=sys.stderr)
            https = None
        if https:
            cfg["_lan_https_port"] = hp       # never persisted (not a SAVED_KEY)

    # The browser URL is always loopback — "0.0.0.0" is a bind address, not
    # a navigable host.
    url = "http://127.0.0.1:%d/" % port
    if host != "127.0.0.1":
        print("[drivecast] Remote access ON — also reachable on your network "
              "(see Settings for the phone URL/QR).")
        if https:
            print("[drivecast] Trusted-LAN HTTPS on :%d (scan the 'Trust this Mac' "
                  "QR once per iPhone/iPad)." % hp)
            fp = localcert.ca_fingerprint()
            if fp:
                print("[drivecast] CA SHA-256 fingerprint (verify this against the "
                      "one iOS shows under 'More Details' before installing):")
                print("[drivecast]   %s" % fp)
    print("[drivecast] Serving at %s" % url)
    _open_browser_later(url)

    app = create_app(cfg)
    if https:
        _start_https_thread(app, hp, certfile=https[0], keyfile=https[1])
    # access_log=False: request lines include ?token= query strings, and the
    # remote-access token must never reach stdout/log files.
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
