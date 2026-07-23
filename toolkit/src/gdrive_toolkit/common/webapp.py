"""Shared Flask-app scaffolding for the two web tools (downloader + uploader).

Factors out the pieces that were byte-identical between the two old
server.py files: the drives-cache AppState, the eta/speed formatters,
static-file serving (with no-cache for dev), and the SSE stream loop. Routes
and `_run_group` stay per-tool because the payload shapes differ (the
uploader also needs `_cleanup_staged`).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from .rclone_rc import RcloneRC

# Google Drive shared-drive IDs are short alnum/-/_ tokens. Reject anything
# with a comma or colon (rclone connection-string fs syntax delimiters) or
# any other character that has no business being in a drive id — empty
# string stays allowed (the non-gdrive pseudo-drive case).
_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{0,64}\Z")


def is_valid_drive_id(drive_id: str) -> bool:
    return bool(_DRIVE_ID_RE.match(drive_id or ""))


class AppState:
    """Holds the RcloneRC client, a 5-minute drives cache, and in-flight
    transfer `groups` (per-tool code owns the shape of each group dict)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rc = RcloneRC(cfg["remote"], cfg["rc_addr"])
        self.lock = threading.Lock()
        self._drives_cache: Optional[list[dict]] = None
        self._drives_at = 0.0
        self.groups: dict[str, dict] = {}

    def drives(self, force: bool = False) -> list[dict]:
        with self.lock:
            fresh = self._drives_cache is not None and (time.monotonic() - self._drives_at) < 300
            if fresh and not force:
                return self._drives_cache
        drives = self.rc.list_drives()  # network — outside lock
        with self.lock:
            self._drives_cache = drives
            self._drives_at = time.monotonic()
        return drives

    def drive_name(self, drive_id: str) -> str:
        for d in self.drives():
            if d["id"] == drive_id:
                return d["name"]
        return drive_id


def fmt_eta(secs) -> str:
    try:
        s = int(secs)
    except (TypeError, ValueError):
        return "-"
    if s < 0:
        return "-"
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


def fmt_speed(bps) -> str:
    try:
        b = float(bps)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if b < 1024:
            return "%.1f %s/s" % (b, unit)
        b /= 1024
    return "%.1f TiB/s" % b


def install_security_guards(app: Flask, port: int) -> None:
    """Reject any request whose Host isn't this app's own loopback origin, and
    any request carrying an Origin/Referer that doesn't match it either.

    This is a same-origin belt-and-suspenders check on top of the rc daemon's
    own auth — it stops a malicious page open in the same browser (DNS
    rebinding, or a cross-site form/XHR) from reaching these local APIs at
    all, regardless of what credentials it could or couldn't forge.
    """
    allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
    allowed_origins = {
        "http://127.0.0.1:%d" % port,
        "http://localhost:%d" % port,
    }

    @app.before_request
    def _check_origin():  # noqa: ANN202
        host = request.headers.get("Host", "")
        if host not in allowed_hosts:
            abort(403)
        for hdr in ("Origin", "Referer"):
            val = request.headers.get(hdr)
            if val is None:
                continue
            if not any(val == o or val.startswith(o + "/") for o in allowed_origins):
                abort(403)


def add_static_routes(app: Flask, static_dir: Path) -> None:
    """Serve the SPA (`/`, `/static/<fn>`) with dev-friendly no-cache headers."""

    @app.after_request
    def _no_cache(resp):  # keep the SPA fresh during dev
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/static/<path:fn>")
    def static_files(fn):
        return send_from_directory(static_dir, fn)


def sse_response(render: Callable[[], Optional[tuple[dict, bool]]]) -> Response:
    """Build an SSE `Response` from a per-tool `render()` callback.

    `render()` does its own `with state.lock:` snapshot + payload building
    each tick, and returns `(payload_dict, done)` — or `None` if the group
    vanished (stops the stream). This function owns only the loop, the
    1-second cadence, and the SSE headers.
    """

    def stream():
        while True:
            result = render()
            if result is None:
                break
            payload, done = result
            yield "data: %s\n\n" % json.dumps(payload)
            if done:
                break
            time.sleep(1.0)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
