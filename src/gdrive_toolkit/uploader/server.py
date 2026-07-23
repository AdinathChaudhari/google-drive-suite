"""Flask app: pick local files/folders, choose a drive+folder, and parallel-upload
via rclone. Mirror image of the downloader's server.py.
"""
from __future__ import annotations

import os
import shutil
import subprocess
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
from .config import load_config, save_config
from .plan import PlanError, build_upload_jobs, dir_size

STATIC_DIR = Path(__file__).parent / "static"


def _cleanup_staged(job: dict) -> None:
    """rmtree any staged spool dirs this job's sources came from, after success."""
    for src in job.get("sources", []):
        if src.get("staged"):
            try:
                shutil.rmtree(src["path"], ignore_errors=True)
            except OSError:
                pass


def _run_group(state: AppState, group_id: str) -> None:
    """Worker thread: fire each job sequentially (rate-limit safe, same as the downloader)."""
    group = state.groups[group_id]
    for job in group["jobs"]:
        with state.lock:
            job["state"] = "running"
        try:
            jobid = state.rc.upload_async(job["src_fs"], group["drive_id"], job["dst_path"],
                                          job["filter_rules"], state.cfg)
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
        if job["state"] == "done":
            _cleanup_staged(job)
    with state.lock:
        group["done"] = True


# ---- native macOS picker (Decision 2) --------------------------------------
_PICK_SCRIPT = '''
tell application "System Events" to activate
set fs to choose %s with multiple selections allowed
set out to ""
repeat with f in fs
  set out to out & POSIX path of f & linefeed
end repeat
return out
'''


def _run_picker(kind: str) -> list[str]:
    """kind: "files" or "folders". Returns POSIX paths, [] on user cancel."""
    aeword = "file" if kind == "files" else "folder"
    proc = subprocess.run(
        ["osascript", "-e", _PICK_SCRIPT % aeword],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        if "-128" in (proc.stderr or ""):
            return []  # user cancelled
        raise RuntimeError((proc.stderr or "osascript failed").strip())
    paths = [ln.rstrip("/") for ln in proc.stdout.splitlines() if ln.strip()]
    return paths


def create_app(cfg: dict | None = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__)
    state = AppState(cfg)
    state.pick_lock = threading.Lock()  # guards the native osascript picker
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
            # Destination picker only ever targets a folder — folders only.
            return jsonify(state.rc.list_dir(drive_id, path, dirs_only=True))
        except RcloneError as e:
            return jsonify({"error": str(e)}), 502

    @app.post("/api/mkdir")
    def api_mkdir():
        body = request.get_json(silent=True)
        if body is None:
            return jsonify({"error": "expected a JSON body"}), 400
        drive_id = body.get("drive_id", "")
        path = body.get("path", "")
        name = body.get("name", "")
        if not is_valid_drive_id(drive_id):
            return jsonify({"error": "invalid drive_id"}), 400
        if (not drive_id and state.rc.is_gdrive is not False) or not name:
            return jsonify({"error": "drive_id and name required"}), 400
        new_path = ("%s/%s" % (path.strip("/"), name)) if path.strip("/") else name
        try:
            state.rc.mkdir(drive_id, new_path)
            listing = state.rc.list_dir(drive_id, path, dirs_only=True)
        except RcloneError as e:
            return jsonify({"error": str(e)}), 502
        return jsonify({"path": new_path, "listing": listing})

    @app.post("/api/pick")
    def api_pick():
        body = request.get_json(silent=True)
        if body is None:
            return jsonify({"error": "expected a JSON body"}), 400
        kind = body.get("kind", "files")
        if kind not in ("files", "folders"):
            return jsonify({"error": "kind must be 'files' or 'folders'"}), 400
        if not state.pick_lock.acquire(blocking=False):
            return jsonify({"error": "a picker dialog is already open"}), 409
        try:
            paths = _run_picker(kind)
        except (RuntimeError, subprocess.SubprocessError) as e:
            return jsonify({"error": str(e)}), 500
        finally:
            state.pick_lock.release()
        return jsonify({"paths": paths})

    @app.post("/api/stat")
    def api_stat():
        body = request.get_json(silent=True)
        if body is None:
            return jsonify({"error": "expected a JSON body"}), 400
        paths = body.get("paths", [])
        out = []
        for p in paths:
            try:
                exists = os.path.exists(p)
                is_dir = os.path.isdir(p) if exists else False
                if not exists:
                    size = -1
                elif is_dir:
                    size = dir_size(p)
                else:
                    size = os.path.getsize(p)
            except OSError:
                exists, is_dir, size = False, False, -1
            out.append({
                "path": p, "name": os.path.basename(p.rstrip("/")),
                "is_dir": is_dir, "size": size, "exists": exists,
            })
        return jsonify(out)

    # TODO(/api/stage, optional v2): multipart drag-drop fallback. A browser drop
    # would POST files here (werkzeug spools each part to
    # ~/Library/Caches/gdrive_toolkit/stage/<uuid>/<relpath>, preserving the
    # dropped folder structure via webkitRelativePath-derived form field names);
    # the response's staged root path would then be added to the source list
    # like any other picked path, tagged {"staged": true}. Not needed for v1 —
    # the native /api/pick dialog covers the primary flow.

    @app.post("/api/upload")
    def api_upload():
        body = request.get_json(silent=True)
        if body is None:
            return jsonify({"error": "expected a JSON body"}), 400
        sources = body.get("sources", [])
        drive_id = body.get("drive_id", "")
        dest_path = body.get("dest_path", "")
        if not is_valid_drive_id(drive_id):
            return jsonify({"error": "invalid drive_id"}), 400
        if not sources:
            return jsonify({"error": "nothing selected"}), 400
        if not drive_id and state.rc.is_gdrive is not False:
            return jsonify({"error": "drive_id required"}), 400

        for s in sources:
            p = s.get("path", "")
            if not p or not os.path.exists(p):
                return jsonify({"error": "path does not exist: %s" % p}), 400

        try:
            planned = build_upload_jobs(sources, dest_path)
        except PlanError as e:
            return jsonify({"error": str(e)}), 400
        if not planned:
            return jsonify({"error": "nothing to upload"}), 400

        jobs = [{
            "label": j.label,
            "src_fs": j.src_fs,
            "dst_path": j.dst_path,
            "filter_rules": j.filter_rules,
            "size": j.size,
            "sources": j.sources,
            "state": "pending",
            "jobid": None,
            "error": None,
        } for j in planned]

        group_id = uuid.uuid4().hex
        with state.lock:
            state.groups[group_id] = {"jobs": jobs, "drive_id": drive_id, "done": False}
        threading.Thread(target=_run_group, args=(state, group_id), daemon=True).start()

        # remember last-used destination
        cfg["default_drive_id"] = drive_id
        cfg["default_dest_path"] = dest_path
        save_config(cfg)

        return jsonify({"group_id": group_id, "jobs": [j.label for j in planned]})

    @app.get("/api/progress/<group_id>")
    def api_progress(group_id):
        if group_id not in state.groups:
            return jsonify({"error": "unknown group"}), 404

        def render():
            with state.lock:
                group = state.groups.get(group_id)
                if not group:
                    return None
                jobs_snapshot = [dict(j) for j in group["jobs"]]
                done = group["done"]
            total_pct = 0.0
            payload_jobs = []
            for j in jobs_snapshot:
                pct, speed, eta, files = 0, "-", "-", []
                if j["state"] == "running" and j["jobid"] is not None:
                    try:
                        s = state.rc.stats(group="job/%d" % j["jobid"])
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
                elif j["state"] == "done":
                    pct = 100
                payload_jobs.append({
                    "label": j["label"], "state": j["state"], "pct": pct,
                    "speed": speed, "eta": eta, "error": j["error"], "files": files,
                })
                total_pct += pct
            overall = int(total_pct / len(payload_jobs)) if payload_jobs else 0
            return {"overall_pct": overall, "jobs": payload_jobs, "done": done}, done

        return sse_response(render)

    @app.post("/api/cancel/<group_id>")
    def api_cancel(group_id):
        with state.lock:
            group = state.groups.get(group_id)
            if not group:
                return jsonify({"error": "unknown group"}), 404
            jobids = [j["jobid"] for j in group["jobs"] if j["jobid"] and j["state"] == "running"]
        for jid in jobids:
            try:
                state.rc.job_stop(jid)
            except RcloneError:
                pass
        return jsonify({"cancelled": jobids})

    return app
