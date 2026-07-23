"""Thin client over the rclone rc daemon (`rclone rcd`).

Everything the app needs — list shared drives, browse a folder, make a
folder, start a parallel copy/upload, poll progress — goes through the
daemon's HTTP API so we reuse one authed session instead of spawning a
subprocess per click.

Merged from the near-identical drive-downloader/rclone_rc.py and
drive-upload/rclone_rc.py. Both tools' methods live here; each tool only
calls the subset it needs.

Generalization (non-gdrive remotes): at daemon start we detect whether the
configured remote is a Google Drive backend (`is_gdrive`). For a non-gdrive
remote, `list_drives()` returns a single pseudo-drive `[{"id": "", "name":
remote}]` and the gdrive-only extras (acknowledge_abuse, skip_gdocs,
export_formats, chunk_size) are never sent — everything else (including
`_fs()`) is unchanged, so the existing gdrive flow (non-empty drive_id) is
byte-identical to before.
"""
from __future__ import annotations

import ipaddress
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

import requests

from .config import CONFIG_DIR

RC_USER = "gdrive"

# A launchd-spawned tool (e.g. launched by the menu-bar hub) inherits a minimal
# PATH (/usr/bin:/bin:/usr/sbin:/sbin) that omits Homebrew's bin dir, so a bare
# `shutil.which("rclone")` fails even though rclone is installed. Resolve the
# binary by absolute path, falling back to the usual install locations.
_RCLONE_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin")


def find_rclone() -> Optional[str]:
    """Absolute path to the rclone binary, or None if it can't be found.

    Checks PATH first, then common install dirs (Homebrew, MacPorts, /usr/bin).
    """
    found = shutil.which("rclone")
    if found:
        return found
    for d in _RCLONE_FALLBACK_DIRS:
        cand = os.path.join(d, "rclone")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


class RcloneError(RuntimeError):
    pass


def _secret_path(port: str) -> Path:
    return Path(CONFIG_DIR) / ("rc-%s.secret" % port)


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class RcloneRC:
    def __init__(self, remote: str, rc_addr: str):
        self.remote = remote
        self.rc_addr = rc_addr
        self.base = "http://%s" % rc_addr
        self._proc: Optional[subprocess.Popen] = None
        self._owns_daemon = False
        self._session = requests.Session()
        # None until start_daemon() runs detection; True/False after.
        self.is_gdrive: Optional[bool] = None

    # ---- daemon lifecycle -------------------------------------------------
    def _addr_in_use(self) -> bool:
        host, _, port = self.rc_addr.partition(":")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex((host, int(port))) == 0

    def start_daemon(self, wait_secs: float = 15.0) -> None:
        """Start rcd if nothing is already listening on rc_addr."""
        rclone_bin = find_rclone()
        if rclone_bin is None:
            raise RcloneError(
                "rclone not found (checked PATH and %s). Install rclone, or if "
                "it's installed, ensure it's on PATH or in one of those dirs."
                % ", ".join(_RCLONE_FALLBACK_DIRS)
            )

        host, _, port = self.rc_addr.partition(":")
        if not _is_loopback_host(host):
            raise RcloneError(
                "rc_addr must be loopback; binding beyond localhost exposes "
                "the daemon (%r is not 127.0.0.0/8, ::1, or 'localhost')" % host
            )

        secret_file = _secret_path(port)
        if self._addr_in_use():
            # Reuse an existing daemon; we don't own it, so we won't kill it.
            self._owns_daemon = False
            try:
                secret = secret_file.read_text(encoding="utf-8").strip()
            except OSError:
                raise RcloneError(
                    "Found an rclone rc daemon already listening on %s but no "
                    "saved auth secret at %s — restart the tool that started "
                    "it (or quit that process) so a fresh authenticated daemon "
                    "can be started." % (self.rc_addr, secret_file)
                )
            if not secret:
                raise RcloneError(
                    "rc auth secret file %s is empty — restart the tool that "
                    "started the existing daemon." % secret_file
                )
        else:
            secret = secrets.token_urlsafe(32)
            secret_file.parent.mkdir(parents=True, exist_ok=True)
            secret_file.unlink(missing_ok=True)
            fd = os.open(str(secret_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(secret)
            self._proc = subprocess.Popen(
                [
                    rclone_bin, "rcd",
                    "--rc-addr", self.rc_addr,
                ],
                env={**os.environ, "RCLONE_RC_USER": RC_USER, "RCLONE_RC_PASS": secret},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._owns_daemon = True

        self._session.auth = (RC_USER, secret)
        self._await_ready(wait_secs)
        self._detect_is_gdrive()

    def _await_ready(self, wait_secs: float) -> None:
        deadline = time.monotonic() + wait_secs
        last_err = None
        while time.monotonic() < deadline:
            try:
                self._post("core/version", {})
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.25)
        raise RcloneError("rclone rc daemon did not become ready: %s" % last_err)

    def _detect_is_gdrive(self) -> None:
        """Populate self.is_gdrive by inspecting the configured remote's type."""
        rtype = ""
        try:
            res = self._post("config/dump", {})
            rtype = (res.get(self.remote) or {}).get("type", "")
        except RcloneError:
            rtype = self._type_via_listremotes()
        self.is_gdrive = (rtype == "drive")

    def _type_via_listremotes(self) -> str:
        """Fallback when config/dump fails: shell out to
        `rclone listremotes --long`, which prints "<remote>:    <type>" lines."""
        try:
            proc = subprocess.run(
                [find_rclone() or "rclone", "listremotes", "--long"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:  # noqa: BLE001
            return ""
        prefix = "%s:" % self.remote
        for line in proc.stdout.splitlines():
            if line.startswith(prefix):
                parts = line.split()
                if len(parts) >= 2:
                    return parts[-1]
        return ""

    def stop_daemon(self) -> None:
        """Kill the daemon only if we started it."""
        if self._owns_daemon and self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    # ---- low-level --------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        try:
            r = self._session.post("%s/%s" % (self.base, path), json=payload, timeout=120)
        except requests.RequestException as e:
            raise RcloneError("rc call %s failed: %s" % (path, e)) from e
        if r.status_code != 200:
            raise RcloneError("rc %s -> %s: %s" % (path, r.status_code, r.text[:400]))
        return r.json()

    def _fs(self, drive_id: Optional[str] = None, extra: Optional[dict] = None) -> str:
        """Build a connection-string fs, baking per-drive overrides in.

        e.g. gdrive1,team_drive=<id>,acknowledge_abuse=true:

        Unchanged regardless of is_gdrive — an empty/falsy drive_id already
        produces a bare "remote:" (no team_drive param), which is exactly the
        pseudo-drive fs a non-gdrive remote needs. Callers gate the gdrive-only
        `extra` keys themselves (see copy_async/upload_async) so a non-gdrive
        remote never sees them.
        """
        parts = []
        if drive_id:
            parts.append("team_drive=%s" % drive_id)
        for k, v in (extra or {}).items():
            parts.append("%s=%s" % (k, str(v).lower() if isinstance(v, bool) else v))
        if parts:
            return "%s,%s:" % (self.remote, ",".join(parts))
        return "%s:" % self.remote

    # ---- operations -------------------------------------------------------
    def list_drives(self) -> list[dict]:
        """All shared drives on the account -> [{'id','name'}] sorted by name.

        Non-gdrive remote: one pseudo-drive so the rest of the UI (which is
        built around picking a drive first) still works.
        """
        if self.is_gdrive is False:
            return [{"id": "", "name": self.remote}]
        res = self._post("backend/command", {
            "command": "drives",
            "fs": self._fs(),
        })
        raw = res.get("result", res)
        if isinstance(raw, dict):
            raw = raw.get("drives", [])
        drives = [{"id": d["id"], "name": d.get("name", d["id"])} for d in raw]
        drives.sort(key=lambda d: d["name"].lower())
        return drives

    def list_dir(self, drive_id: str, path: str = "", dirs_only: bool = False,
                 skip_gdocs: bool = False) -> list[dict]:
        """One folder level inside a shared drive. Folders first.

        dirs_only=True is used by the uploader's destination picker (upload
        only ever targets a folder, never a file). skip_gdocs is a
        downloader-only, gdrive-only knob.
        """
        opt = {"noModTime": True, "dirsFirst": True}
        if dirs_only:
            opt["dirsOnly"] = True
        extra = {"skip_gdocs": True} if (skip_gdocs and self.is_gdrive is not False) else None
        res = self._post("operations/list", {
            "fs": self._fs(drive_id, extra),
            "remote": path,
            "opt": opt,
        })
        out = []
        for e in res.get("list", []):
            out.append({
                "path": e["Path"],
                "name": e["Name"],
                "is_dir": bool(e["IsDir"]),
                "size": e.get("Size", -1),
                "id": e.get("ID", ""),
            })
        out.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return out

    def mkdir(self, drive_id: str, path: str) -> None:
        """Create a folder (and any missing ancestors) at `path` inside the drive."""
        self._post("operations/mkdir", {
            "fs": self._fs(drive_id),
            "remote": path,
        })

    def copy_async(self, drive_id: str, dst: str, filter_rules: list[str],
                   cfg: dict) -> int:
        """Fire a parallel DOWNLOAD copy job; return the rc jobid."""
        extra = {}
        if self.is_gdrive is not False:
            if cfg.get("acknowledge_abuse"):
                extra["acknowledge_abuse"] = True
            if cfg.get("skip_gdocs"):
                extra["skip_gdocs"] = True
            elif cfg.get("export_formats"):
                extra["export_formats"] = cfg["export_formats"]

        _config = {
            "Transfers": cfg.get("transfers", 8),
            "MultiThreadStreams": cfg.get("multi_thread_streams", 4),
            "MultiThreadCutoff": cfg.get("multi_thread_cutoff", "256M"),
            "CheckFirst": True,
            "FastList": True,
        }
        res = self._post("sync/copy", {
            "srcFs": self._fs(drive_id, extra),
            "dstFs": dst,
            "_async": True,
            "_config": _config,
            "_filter": {"FilterRule": filter_rules},
        })
        # rclone auto-creates a stats group named "job/<jobid>" for async jobs;
        # scope core/stats to that group to isolate this job's progress.
        return int(res["jobid"])

    def upload_async(self, src_fs: str, drive_id: str, dest_path: str,
                     filter_rules: Optional[list[str]], cfg: dict) -> int:
        """Fire a parallel UPLOAD copy job (never move) from a local path into
        a drive folder.

        `dest_path` is the destination folder inside the drive (e.g.
        "Incoming/Videos"). `chunk_size` rides in the destination connection
        string (a backend option, gdrive-only), not in the local path.
        """
        extra = {}
        if self.is_gdrive is not False:
            extra["chunk_size"] = cfg.get("drive_chunk_size", "64M")
        dst_fs = self._fs(drive_id, extra) + dest_path

        num_sources = 1
        if filter_rules:
            n = sum(1 for r in filter_rules if r.startswith("+ "))
            num_sources = n or 1

        _config = {
            "Transfers": cfg.get("transfers", 8),
            "Retries": cfg.get("retries", 3),
            "LowLevelRetries": cfg.get("low_level_retries", 10),
        }
        if num_sources <= 50:
            _config["NoTraverse"] = True

        payload = {
            "srcFs": src_fs,
            "dstFs": dst_fs,
            "_async": True,
            "_config": _config,
        }
        if filter_rules:
            payload["_filter"] = {"FilterRule": filter_rules}

        res = self._post("sync/copy", payload)
        # rclone auto-creates a stats group named "job/<jobid>" for async jobs;
        # scope core/stats to that group to isolate this job's progress.
        return int(res["jobid"])

    def stats(self, group: Optional[str] = None) -> dict:
        payload = {"group": group} if group else {}
        return self._post("core/stats", payload)

    def job_status(self, jobid: int) -> dict:
        return self._post("job/status", {"jobid": jobid})

    def job_stop(self, jobid: int) -> dict:
        return self._post("job/stop", {"jobid": jobid})
