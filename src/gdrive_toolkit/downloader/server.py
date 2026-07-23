"""Flask app: browse shared drives, select, and parallel-download via rclone."""
from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request

from ..common.rclone_rc import RcloneError
from ..common.webapp import (
    AppState,
    add_static_routes,
    fmt_eta,
    fmt_speed,
    install_security_guards,
    is_valid_drive_id,
    sse_response,
)
from .config import load_config
from .selection import build_jobs

_PATH_SEP_RE = re.compile(r"[/\\]")


def _sanitize_dest_name(name: str, dest_root: Path) -> str:
    """Turn a shared-drive display name (attacker-controlled, comes from
    Google) into a safe single path component under dest_root: collapse any
    path separators so it can't smuggle in extra directory levels, reject
    `.`/`..`, then verify the resulting join can't escape dest_root."""
    safe = _PATH_SEP_RE.sub("_", name)
    if safe in ("", ".", ".."):
        safe = "_"
    dst = dest_root / safe
    if not Path(dst).resolve().is_relative_to(dest_root.resolve()):
        raise ValueError("resolved destination escapes dest_root: %s" % dst)
    return str(dst)

STATIC_DIR = Path(__file__).parent / "static"


def _run_group(state: AppState, group_id: str) -> None:
    """Worker thread: fire each drive's copy job sequentially (rate-limit safe)."""
    group = state.groups[group_id]
    for job in group["drives"]:
        with state.lock:
            job["state"] = "running"
        try:
            jobid = state.rc.copy_async(job["drive_id"], job["dst"], job["rules"], state.cfg)
            with state.lock:
                job["jobid"] = jobid
            # poll to completion
            while True:
                st = state.rc.job_status(jobid)
                if st.get("finished"):
                    with state.lock:
                        if st.get("success"):
                            job["state"] = "done"
                        else:
                            job["state"] = "error"
                            job["error"] = st.get("error", "unknown error")
                    break
                time.sleep(1.0)
        except RcloneError as e:
            with state.lock:
                job["state"] = "error"
                job["error"] = str(e)
    with state.lock:
        group["done"] = True


def create_app(cfg: dict | None = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__)
    state = AppState(cfg)
    app.state = state  # type: ignore[attr-defined]

    install_security_guards(app, int(cfg["port"]))
    add_static_routes(app, STATIC_DIR)

    @app.get("/api/drives")
    def api_drives():
        try:
            return jsonify(state.drives(force=request.args.get("force") == "1"))
        except RcloneError as e:
            return jsonify({"error": str(e)}), 502

    @app.get("/api/browse")
    def api_browse():
        drive_id = request.args.get("drive_id", "")
        path = request.args.get("path", "")
        if not is_valid_drive_id(drive_id):
            return jsonify({"error": "invalid drive_id"}), 400
        # A non-gdrive remote's pseudo-drive has id "" — only reject an empty
        # drive_id when the remote actually needs a real one.
        if not drive_id and state.rc.is_gdrive is not False:
            return jsonify({"error": "drive_id required"}), 400
        try:
            return jsonify(state.rc.list_dir(drive_id, path, skip_gdocs=cfg["skip_gdocs"]))
        except RcloneError as e:
            return jsonify({"error": str(e)}), 502

    @app.post("/api/download")
    def api_download():
        body = request.get_json(silent=True)
        if body is None:
            return jsonify({"error": "expected a JSON body"}), 400
        selected = body.get("selected", [])
        if not selected:
            return jsonify({"error": "nothing selected"}), 400
        for item in selected:
            if not is_valid_drive_id(item.get("drive_id", "")):
                return jsonify({"error": "invalid drive_id"}), 400
        jobs = build_jobs(selected)
        dest_root = Path(cfg["dest_root"])
        group_id = uuid.uuid4().hex
        drives = []
        for drive_id, rules in jobs.items():
            name = state.drive_name(drive_id)
            try:
                dst = _sanitize_dest_name(name, dest_root)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            drives.append({
                "drive_id": drive_id,
                "name": name,
                "dst": dst,
                "rules": rules,
                "state": "pending",
                "jobid": None,
                "error": None,
            })
        with state.lock:
            state.groups[group_id] = {"drives": drives, "done": False}
        threading.Thread(target=_run_group, args=(state, group_id), daemon=True).start()
        return jsonify({"group_id": group_id, "drives": [d["name"] for d in drives]})

    @app.get("/api/progress/<group_id>")
    def api_progress(group_id):
        if group_id not in state.groups:
            return jsonify({"error": "unknown group"}), 404

        def render():
            with state.lock:
                group = state.groups.get(group_id)
                if not group:
                    return None
                drives_snapshot = [dict(d) for d in group["drives"]]
                done = group["done"]
            total_pct = 0.0
            payload_drives = []
            for d in drives_snapshot:
                pct, speed, eta, files = 0, "-", "-", []
                if d["state"] == "running" and d["jobid"] is not None:
                    try:
                        s = state.rc.stats(group="job/%d" % d["jobid"])
                        tb = s.get("totalBytes", 0) or 0
                        b = s.get("bytes", 0) or 0
                        pct = int(b * 100 / tb) if tb else 0
                        speed = fmt_speed(s.get("speed"))
                        eta = fmt_eta(s.get("eta"))
                        for t in (s.get("transferring") or [])[:8]:
                            ttb = t.get("size", 0) or 0
                            tbb = t.get("bytes", 0) or 0
                            files.append({
                                "name": t.get("name", ""),
                                "pct": int(tbb * 100 / ttb) if ttb else 0,
                                "speed": fmt_speed(t.get("speed")),
                            })
                    except RcloneError:
                        pass
                elif d["state"] == "done":
                    pct = 100
                payload_drives.append({
                    "name": d["name"], "state": d["state"], "pct": pct,
                    "speed": speed, "eta": eta, "error": d["error"], "files": files,
                })
                total_pct += pct
            overall = int(total_pct / len(payload_drives)) if payload_drives else 0
            return {"overall_pct": overall, "drives": payload_drives, "done": done}, done

        return sse_response(render)

    @app.post("/api/cancel/<group_id>")
    def api_cancel(group_id):
        with state.lock:
            group = state.groups.get(group_id)
            if not group:
                return jsonify({"error": "unknown group"}), 404
            jobids = [d["jobid"] for d in group["drives"] if d["jobid"] and d["state"] == "running"]
        for jid in jobids:
            try:
                state.rc.job_stop(jid)
            except RcloneError:
                pass
        return jsonify({"cancelled": jobids})

    return app
