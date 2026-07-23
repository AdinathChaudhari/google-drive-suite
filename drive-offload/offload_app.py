#!/usr/bin/env python3
"""offload_app: a macOS menu-bar companion that watches Motrix (aria2) and
Transmission and, when a download starts, asks where it should go when it
finishes — keep it on the Mac, send it to an existing Google Shared Drive, or
type a new drive name (created automatically). On completion the chosen upload
runs via `todrive up`, which deletes the local copy after a verified upload.

The polling / decision / upload logic lives in plain classes (Aria2Client,
TransmissionClient, MultiClient, DecisionStore, Poller) that take injected
callables, so it is testable headless without Motrix, Transmission, Google, or
the UI. Both engines expose the same is_up()/tell_all() surface — Transmission
torrents are adapted into aria2-shaped download dicts — and a MultiClient fans
out over them. The rumps-specific menu-bar App lives at the bottom, guarded by
`if __name__ == "__main__":`, and imports rumps lazily.

Stdlib only for the logic (urllib JSON-RPC); rumps only for the UI.
See README.md.
"""
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime

import renamer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TODRIVE = os.path.join(SCRIPT_DIR, "todrive")


def support_dir(frozen=None):
    """Directory for the mutable files (decisions/state/log/config).

    Running from source that's SCRIPT_DIR, same as always. Inside a py2app
    bundle SCRIPT_DIR is Contents/Resources, and writing there mutates the
    .app — which breaks code signing and gets wiped on reinstall — so frozen
    builds use ~/Library/Application Support/drive-offload instead. TODRIVE
    stays on SCRIPT_DIR either way: py2app ships it read-only next to this
    script. `frozen` is injectable for tests; default reads sys.frozen.
    """
    if frozen is None:
        frozen = getattr(sys, "frozen", False)
    if frozen:
        return os.path.expanduser(
            "~/Library/Application Support/drive-offload")
    return SCRIPT_DIR


SUPPORT_DIR = support_dir()
os.makedirs(SUPPORT_DIR, exist_ok=True)
DECISIONS_FILE = os.path.join(SUPPORT_DIR, "decisions.json")
STATE_FILE = os.path.join(SUPPORT_DIR, "app_state.json")
LOG_FILE = os.path.join(SUPPORT_DIR, "app.log")
CONFIG_FILE = os.path.join(SUPPORT_DIR, "config.json")
# TV rename engine (renamer.py): the continuity cache and the crash-recovery
# journal both live beside decisions.json in SUPPORT_DIR.
RENAME_CACHE_FILE = os.path.join(SUPPORT_DIR, "rename_cache.json")
RENAME_JOURNAL_FILE = os.path.join(SUPPORT_DIR, "rename_journal.json")

# HOOK B config gate, read from config.json's "rename" object. DEFAULT dry_run
# is True (safety): the plan is built, journaled-in-dry-run only as a log line,
# and the upload is byte-identical to today until dry_run is flipped off after
# review. confirm_new_shows shows a one-time editable dialog for shows not yet
# in the cache; a cached show renames silently.
DEFAULT_RENAME_CFG = {"enabled": True, "dry_run": True,
                      "confirm_new_shows": True}

MOTRIX_SYSTEM_JSON = os.path.expanduser(
    "~/Library/Application Support/Motrix/system.json")

DEFAULT_RPC_PORT = 16800
DEFAULT_RPC_SECRET = ""

# Transmission RPC (default, overridable via config.json keys below).
DEFAULT_TM_PORT = 9091
TM_PREFIX = "tm-"   # gid namespace for Transmission (avoids aria2 collisions)

POLL_SECONDS = 3

# Suffixes that mark an in-progress download on disk (compared
# case-insensitively). Kept in sync with the copy in todrive.
INCOMPLETE_SUFFIXES = (".part", ".aria2", ".!qb", ".crdownload", ".download")

# A decision that is engine-complete but fails the on-disk readiness gate
# for this long triggers ONE notification (per app run).
STUCK_NOTIFY_SECONDS = 30 * 60

# Failed-upload retry policy. A failed upload is retried (the decision is not
# consumed), but NOT on every POLL_SECONDS tick: without a cap or backoff a
# permanent failure (revoked token, deleted remote, create-drive 403, quota)
# re-dispatches rclone and fires an "Upload failed" notification every ~3s
# forever. Retries use exponential backoff and give up after a cap, moving the
# gid to a terminal "failed" state. That state is NOT permanent: DecisionStore
# .load() clears the retry bookkeeping (failures/failed/next_attempt) for every
# not-yet-handled record on startup, so the next app run gives such a gid a
# fresh set of attempts — a transient outage (Wi-Fi drop, expired rclone token)
# that burns all attempts is retried on the next launch, not stuck forever.
# Giving up also fires notify_cb once (see Poller.upload_done) so the payload
# never fails silently.
#
# EXCEPTION — quota/full failures: a failure classified as a storage-quota /
# drive-full error (is_quota_failure) does NOT burn the backoff attempts. The
# same full drive cannot succeed on retry, so Poller.upload_done short-circuits
# straight to the terminal failed state (mark_failed) on the first such failure
# — no more attempts against the full drive, and the item surfaces immediately
# in the "Re-upload to Drive" menu (and the failure-recovery prompt) so the user
# can pick a different drive. DecisionStore.load()'s restart re-arm still applies
# unchanged, so a quota-failed gid gets fresh attempts on the next launch.
UPLOAD_MAX_ATTEMPTS = 5        # total attempts before giving up
UPLOAD_BACKOFF_BASE = 30       # seconds before the first retry
UPLOAD_BACKOFF_CAP = 3600      # cap the backoff at 1 hour

# Markers that identify a storage-quota / drive-full failure in todrive/rclone
# output (lowercase; kept identical to todrive's QUOTA_MARKERS so 'quota' has a
# single definition across both layers). A match steers the failure to the
# drive-full recovery path instead of the generic backoff retries.
QUOTA_MARKERS = ("storagequotaexceeded", "quotaexceeded",
                 "teamdrivefilelimitexceeded", "storage quota")


def is_quota_failure(output):
    """True if `output` names a storage-quota / drive-full error.

    Matches the same markers todrive uses; case-insensitive, tolerant of a
    None/empty argument (the _upload_worker exception path has no output)."""
    return any(m in (output or "").lower() for m in QUOTA_MARKERS)

# How often the "Drive storage" menu's per-drive usage table auto-refreshes.
# `todrive du` is a paged files.list scan across every shared drive -- it
# takes seconds, not milliseconds -- so it must NEVER run on the POLL_SECONDS
# render/timer tick. It runs on its own daemon thread instead, woken early by
# a successful upload or a manual "Refresh now" click (see _usage_loop).
DRIVE_USAGE_REFRESH_SECONDS = 300   # 5 minutes

# Menu-bar titles (short so they fit).
ICON_IDLE = "⭘"        # Motrix not running
ICON_CONNECTED = "☁"   # connected, nothing happening
ICON_BUSY = "⬆"        # an upload is running

# Upload speed-limit choices: label -> RCLONE_BWLIMIT value ("" = unlimited).
SPEED_LIMITS = [
    ("Unlimited", ""),
    ("2 MB/s", "2M"),
    ("5 MB/s", "5M"),
    ("10 MB/s", "10M"),
]


def log(msg):
    """Append a timestamped line to app.log (best effort)."""
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def human_size(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def human_size_dec(n):
    """DECIMAL (1000-based) size, for the Drive-storage menu only: so a drive's
    displayed size, its %, and the "/ 100 GB" cap all agree (Google accounts
    the shared-drive cap in decimal GB). Speeds/"freed" keep binary human_size."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1000.0


def read_motrix_rpc(path=MOTRIX_SYSTEM_JSON):
    """Read (port, secret) from Motrix's system.json, with fallbacks."""
    try:
        with open(path) as f:
            data = json.load(f)
        port = int(data.get("rpc-listen-port", DEFAULT_RPC_PORT))
        secret = data.get("rpc-secret", DEFAULT_RPC_SECRET) or ""
        return port, secret
    except (OSError, ValueError, TypeError):
        return DEFAULT_RPC_PORT, DEFAULT_RPC_SECRET


def read_transmission_rpc(path=CONFIG_FILE):
    """Read (port, user, password) for Transmission's RPC from the repo's
    config.json, merged over defaults. The file is shared with offloader.py and
    is never written here — a missing file or missing keys just yields defaults
    (port 9091, no auth)."""
    port, user, password = DEFAULT_TM_PORT, "", ""
    try:
        with open(path) as f:
            cfg = json.load(f)
        port = int(cfg.get("transmission_rpc_port", DEFAULT_TM_PORT))
        user = cfg.get("transmission_rpc_user", "") or ""
        password = cfg.get("transmission_rpc_pass", "") or ""
    except (OSError, ValueError, TypeError):
        pass
    return port, user, password


def read_rename_config(path=CONFIG_FILE):
    """Read the HOOK B gate from config.json's "rename" object, merged over
    DEFAULT_RENAME_CFG. A missing file/key just yields the safe defaults
    (enabled, dry_run=True, confirm_new_shows=True). Never written here."""
    cfg = dict(DEFAULT_RENAME_CFG)
    try:
        with open(path) as f:
            data = json.load(f)
        r = data.get("rename")
        if isinstance(r, dict):
            for k in cfg:
                if k in r:
                    cfg[k] = bool(r[k])
    except (OSError, ValueError, TypeError):
        pass
    return cfg


# Failure-recovery gate, read from config.json's "recover" object. When
# prompt_on_failure is True (the default) an upload failure offers a one-shot
# "pick another drive" dialog (see OffloadApp._offer_repick). Toggling it off
# requires a daemon restart, matching the rename gate's read-once convention.
DEFAULT_RECOVER_CFG = {"prompt_on_failure": True}


def read_recover_config(path=CONFIG_FILE):
    """Read the failure-recovery gate from config.json's "recover" object,
    merged over DEFAULT_RECOVER_CFG. A missing file/key just yields the safe
    default (prompt_on_failure=True). Never written here."""
    cfg = dict(DEFAULT_RECOVER_CFG)
    try:
        with open(path) as f:
            data = json.load(f)
        r = data.get("recover")
        if isinstance(r, dict):
            for k in cfg:
                if k in r:
                    cfg[k] = bool(r[k])
    except (OSError, ValueError, TypeError):
        pass
    return cfg


def esc(s):
    """Escape a string for embedding inside an AppleScript double-quoted
    literal. Backslashes and double quotes must be backslash-escaped so a name
    like  My "Show"  or  It's Fine  survives osascript intact."""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


class Aria2Client:
    """urllib-based JSON-RPC 2.0 client for aria2 (embedded in Motrix).

    No external deps. If a non-empty secret is configured, "token:<secret>" is
    prepended to every method's params, as aria2 requires."""

    def __init__(self, port=DEFAULT_RPC_PORT, secret="", timeout=4):
        self.url = "http://127.0.0.1:%d/jsonrpc" % port
        self.secret = secret or ""
        self.timeout = timeout
        self._id = 0

    def _call(self, method, params=None):
        self._id += 1
        p = list(params or [])
        if self.secret:
            p = ["token:%s" % self.secret] + p
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": str(self._id),
            "method": method,
            "params": p,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if "error" in data:
            raise RuntimeError("aria2 error: %s" % data["error"])
        return data.get("result")

    def is_up(self):
        """True if aria2's RPC answers."""
        try:
            self._call("aria2.getVersion")
            return True
        except Exception:
            return False

    def tell_all(self):
        """Merged list of active + waiting + stopped download objects.
        Raises on connection failure so the poller can treat it as 'down'."""
        active = self._call("aria2.tellActive") or []
        waiting = self._call("aria2.tellWaiting", [0, 50]) or []
        stopped = self._call("aria2.tellStopped", [0, 50]) or []
        return list(active) + list(waiting) + list(stopped)

    def stop_torrent(self, gid):
        """Pause a download so a finished torrent stops seeding before we
        upload (and delete) its payload. A download already in the stopped
        list can't be paused — aria2 errors, we swallow. Best-effort."""
        try:
            self._call("aria2.forcePause", [gid])
        except Exception as e:
            log("aria2 stop_torrent %s: %s" % (gid, e))

    def start_torrent(self, gid):
        """Unpause a download — used to resume seeding if an upload FAILED,
        so a failed offload doesn't leave the torrent stuck paused."""
        try:
            self._call("aria2.unpause", [gid])
        except Exception as e:
            log("aria2 start_torrent %s: %s" % (gid, e))

    def remove_torrent(self, gid):
        """Drop the entry from aria2 entirely (todrive already deleted the
        verified-uploaded payload; this keeps Motrix from showing a dataless
        download or trying to seed it). Best-effort."""
        try:
            self._call("aria2.remove", [gid])
        except Exception:
            pass  # already in the stopped list; nothing to remove
        try:
            self._call("aria2.removeDownloadResult", [gid])
        except Exception as e:
            log("aria2 remove_torrent %s: %s" % (gid, e))


def _tm_status(t):
    """Map a Transmission torrent object to an aria2-style status string.

    A finished torrent reads as "complete" even while it is seeding (status
    5/6) or has been stopped, so the poller uploads it once and only once.

    The verifying-status check comes FIRST, before the completeness check.
    2026-07-06 incident: a torrent in status 1/2 (queued to verify /
    verifying) transiently reported leftUntilDone == 0 while Transmission had
    not yet finished hashing pieces or renamed "<name>.part" -> "<name>", so
    the completeness branch read it "complete" and the poller uploaded the
    live .part file (truncated, then deleted locally). Pieces are NOT trusted
    until verification finishes, so status 1/2 can never map to "complete",
    whatever percentDone/leftUntilDone claim."""
    try:
        pct = float(t.get("percentDone") or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    try:
        total = int(t.get("totalSize") or 0)
        left = int(t.get("leftUntilDone") or 0)
    except (TypeError, ValueError):
        total, left = 0, 0
    status = t.get("status")
    if status in (1, 2):        # queued to verify / verifying: pieces are NOT
        return "waiting"        # trusted yet, .part rename hasn't happened --
                                # never "complete", whatever left/pct claim
    if pct >= 1.0 or (left == 0 and total > 0):
        return "complete"
    if status in (3, 4):        # queued to download / downloading
        return "active"
    return "paused"             # status 0 (stopped) while still incomplete


class TransmissionClient:
    """urllib-based client for Transmission's RPC (Transmission.app).

    No external deps. Speaks Transmission's JSON RPC and adapts each torrent
    into the aria2-shaped download dict the rest of the app consumes, so it
    drops in beside Aria2Client. Handles the CSRF session-id handshake (a first
    request gets HTTP 409 with an X-Transmission-Session-Id header; that id is
    stored and the request retried, and re-fetched whenever another 409
    appears) and optional HTTP basic auth."""

    # torrent-get fields we adapt from. sizeWhenDone/fileStats added for the
    # payload-readiness gate: sizeWhenDone excludes deselected files (unlike
    # totalSize) so the size check doesn't fail forever on partial-selection
    # torrents, and fileStats.wanted tells the gate which files to expect on
    # disk.
    FIELDS = ["id", "hashString", "name", "status", "percentDone",
              "leftUntilDone", "totalSize", "sizeWhenDone", "fileStats",
              "rateDownload", "downloadDir", "files", "isFinished"]

    def __init__(self, port=DEFAULT_TM_PORT, user="", password="", timeout=4):
        self.url = "http://127.0.0.1:%d/transmission/rpc" % port
        self.user = user or ""
        self.password = password or ""
        self.timeout = timeout
        self._session_id = ""

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self._session_id:
            h["X-Transmission-Session-Id"] = self._session_id
        if self.user or self.password:
            raw = ("%s:%s" % (self.user, self.password)).encode("utf-8")
            h["Authorization"] = "Basic %s" % (
                base64.b64encode(raw).decode("ascii"))
        return h

    def _call(self, method, arguments=None):
        payload = json.dumps({"method": method,
                              "arguments": arguments or {}}).encode("utf-8")
        data = None
        for attempt in (1, 2):
            req = urllib.request.Request(
                self.url, data=payload, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                # CSRF handshake: grab the session id and retry once.
                if e.code == 409 and attempt == 1:
                    self._session_id = (
                        e.headers.get("X-Transmission-Session-Id") or "")
                    continue
                raise
        if data is None or data.get("result") != "success":
            raise RuntimeError(
                "transmission error: %s" % (data or {}).get("result"))
        return data.get("arguments") or {}

    def is_up(self):
        """True if Transmission's RPC answers."""
        try:
            self._call("session-get")
            return True
        except Exception:
            return False

    def tell_all(self):
        """All torrents, adapted into aria2-shaped download dicts.
        Raises on connection failure so the caller can treat it as 'down'."""
        args = self._call("torrent-get", {"fields": self.FIELDS})
        return [self._adapt(t) for t in (args.get("torrents") or [])]

    @staticmethod
    def _adapt(t):
        """One Transmission torrent -> the aria2 download-object shape."""
        d = t.get("downloadDir") or ""
        try:
            total = int(t.get("totalSize") or 0)
            left = int(t.get("leftUntilDone") or 0)
        except (TypeError, ValueError):
            total, left = 0, 0
        # Prefer sizeWhenDone (bytes of the SELECTED files) over totalSize
        # (which includes deselected files): leftUntilDone is relative to
        # sizeWhenDone, so completedLength = sizeWhenDone - left is the honest
        # progress, and the readiness gate's size check must compare against
        # the selected total. Fall back to totalSize when sizeWhenDone is
        # absent (keeps test_shape_and_fields' fixture at totalLength == 100).
        try:
            size_when_done = int(t.get("sizeWhenDone") or 0)
        except (TypeError, ValueError):
            size_when_done = 0
        total = size_when_done or total
        # Zip fileStats onto files by index: Transmission documents them as
        # parallel arrays. A missing/short fileStats is tolerated as all-wanted
        # (the gate then only gets stricter, never looser).
        stats = t.get("fileStats") or []
        files = []
        for i, f in enumerate(t.get("files") or []):
            wanted = stats[i].get("wanted", True) if i < len(stats) else True
            files.append({"path": os.path.join(d, f.get("name") or ""),
                          "length": f.get("length") or 0,
                          "selected": "true" if wanted else "false"})
        try:
            speed = int(t.get("rateDownload") or 0)
        except (TypeError, ValueError):
            speed = 0
        return {
            "gid": TM_PREFIX + (t.get("hashString") or ""),
            "status": _tm_status(t),
            "dir": d,
            "bittorrent": {"info": {"name": t.get("name") or ""}},
            "files": files,
            "totalLength": total,
            "completedLength": total - left,
            "downloadSpeed": speed,
        }

    def stop_torrent(self, gid):
        """Stop (pause) a torrent so seeding ends before we upload it.
        Best-effort: log and swallow errors."""
        h = gid[len(TM_PREFIX):] if gid.startswith(TM_PREFIX) else gid
        try:
            self._call("torrent-stop", {"ids": [h]})
        except Exception as e:
            log("transmission stop_torrent %s error: %s" % (gid, e))

    def start_torrent(self, gid):
        """(Re)start a torrent — used to resume it if an upload FAILED, so a
        failed offload doesn't leave the download stuck paused. Best-effort."""
        h = gid[len(TM_PREFIX):] if gid.startswith(TM_PREFIX) else gid
        try:
            self._call("torrent-start", {"ids": [h]})
        except Exception as e:
            log("transmission start_torrent %s error: %s" % (gid, e))

    def remove_torrent(self, gid):
        """Remove a torrent from Transmission WITHOUT deleting local data
        (todrive already deleted the verified-uploaded payload; this just
        clears the now-dataless entry). Best-effort: log and swallow errors."""
        h = gid[len(TM_PREFIX):] if gid.startswith(TM_PREFIX) else gid
        try:
            self._call("torrent-remove",
                       {"ids": [h], "delete-local-data": False})
        except Exception as e:
            log("transmission remove_torrent %s error: %s" % (gid, e))


class MultiClient:
    """Fans out over several download engines (Motrix/aria2, Transmission),
    presenting the same is_up()/tell_all() surface the Poller expects. A gid's
    "tm-" prefix routes ownership back to the engine that produced it, so the
    upload hooks can reach the right engine."""

    def __init__(self, engines):
        # engines: list of (label, client) pairs, e.g.
        #   [("Motrix", Aria2Client()), ("Transmission", TransmissionClient())]
        self.engines = list(engines)

    @staticmethod
    def _safe_up(client):
        try:
            return bool(client.is_up())
        except Exception:
            return False

    def is_up(self):
        """True if ANY engine is up."""
        return any(self._safe_up(c) for _label, c in self.engines)

    def tell_all(self):
        """Merge downloads from every engine that is up. An engine that is
        down or raises is skipped (debug log), never fatal — the Poller already
        gates on is_up(), so this just returns what is collectible."""
        merged = []
        for label, client in self.engines:
            if not self._safe_up(client):
                continue
            try:
                merged.extend(client.tell_all())
            except Exception as e:
                log("DEBUG %s tell_all skipped: %s" % (label, e))
        return merged

    def engine_status(self):
        """[(label, up_bool)] for the menu status line."""
        return [(label, self._safe_up(client))
                for label, client in self.engines]

    def for_gid(self, gid):
        """The engine that owns a gid: a "tm-" prefix -> the Transmission
        engine, else the aria2 engine. None if no matching engine exists."""
        want_tm = str(gid).startswith(TM_PREFIX)
        for _label, client in self.engines:
            if isinstance(client, TransmissionClient) == want_tm:
                return client
        return None


def effective_status(dl):
    """A download's status with the aria2 seeding gotcha normalized away.

    aria2 keeps a torrent's status "active" after the payload is fully
    downloaded and verified, for as long as it seeds — so a naive
    status == "complete" check never fires and the upload waits (possibly
    forever) for seeding to end. Every piece is hash-verified once
    completedLength reaches totalLength, so the file is safe to upload then.

    Transmission is deliberately left untouched: _tm_status is already the
    authoritative mapping for it — a genuinely finished torrent reads
    "complete" and a still-verifying one (status 1/2) reads "waiting". Since
    _adapt sets completedLength = sizeWhenDone - leftUntilDone, a verifying
    torrent that transiently reports leftUntilDone == 0 has done >= total, so
    promoting "waiting" here would re-mark it "complete" and hand the poller a
    live/unrenamed ".part" payload — exactly the 2026-07-06 incident this
    guard was added to prevent. The seeding gotcha above is aria2-only, so
    tm- gids pass through with the status _tm_status already computed."""
    status = dl.get("status")
    if str(dl.get("gid", "")).startswith(TM_PREFIX):
        return status
    try:
        total = int(dl.get("totalLength", 0))
        done = int(dl.get("completedLength", 0))
    except (TypeError, ValueError):
        return status
    if total > 0 and done >= total and status in ("active", "waiting",
                                                  "paused"):
        return "complete"
    return status


def download_name(dl):
    """Best display name for a download object."""
    bt = (dl.get("bittorrent") or {}).get("info") or {}
    if bt.get("name"):
        return bt["name"]
    files = dl.get("files") or []
    if files:
        p = files[0].get("path") or ""
        if p:
            return os.path.basename(p.rstrip(os.sep))
    return dl.get("gid", "download")


def download_progress(dl):
    """(percent float 0..100, speed bytes/s int)."""
    try:
        total = int(dl.get("totalLength", 0))
        done = int(dl.get("completedLength", 0))
    except (TypeError, ValueError):
        total, done = 0, 0
    pct = (done * 100.0 / total) if total else 0.0
    try:
        speed = int(dl.get("downloadSpeed", 0))
    except (TypeError, ValueError):
        speed = 0
    return pct, speed


def resolve_local_path(dl):
    """Determine the local path a finished download produced.

    For torrents: <dir>/<bittorrent.info.name> if that exists. Otherwise the
    single file's path from files[0]. Otherwise the common parent directory of
    all files. Returns None if nothing usable is found."""
    d = dl.get("dir") or ""
    bt = (dl.get("bittorrent") or {}).get("info") or {}
    name = bt.get("name")
    if d and name:
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand

    files = dl.get("files") or []
    paths = [f.get("path") for f in files if f.get("path")]
    if len(paths) == 1:
        return paths[0]
    if paths:
        common = os.path.commonpath([os.path.abspath(p) for p in paths])
        # <dir>/<name> was already tried above and only returned when it
        # exists on disk; reaching here means it does NOT. The engine's
        # files[] carry the ACTUAL on-disk names, which can differ from the
        # torrent name when the filesystem sanitizes an illegal character
        # (Transmission writes "Ace Ventura: Pet Detective" as
        # "Ace Ventura_ Pet Detective" -- ':' is illegal in a macOS path).
        # Returning the non-existent <dir>/<name> here made tree_size() read 0
        # and payload_ready's size check ("size 0 < expected") block a
        # multi-file torrent's upload forever. Trust the file paths: their
        # common parent is where the data really landed. Fall back to
        # <dir>/<name> only when files[] give no usable common parent.
        if common and os.path.exists(common):
            return common
        if d and name:
            return os.path.join(d, name)
        return common
    # last resort: the dir itself
    return d or None


class DecisionStore:
    """Persists per-gid decisions to decisions.json.

    Shape: {gid: {"name": <display>, "choice": "local" | "drive:<Name>",
                  "handled": bool}}

    Mutations are serialized with an RLock and the file is replaced
    atomically: upload_done() now mutates the store from the upload worker
    thread while the poll timer records new decisions, and two interleaved
    plain open(..., "w") writers can tear the JSON — load() would then fall
    back to {} and silently re-ask every active download."""

    def __init__(self, path=DECISIONS_FILE):
        self.path = path
        self.data = {}
        self._lock = threading.RLock()
        self.load()

    def load(self):
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                self.data = {}
            # A terminal "failed" (or mid-backoff) state must not survive an
            # app restart: poll_once skips failed records forever, so an upload
            # that burned its attempts during an outage would otherwise never
            # retry — recoverable only by hand-editing this file. Clear the
            # retry bookkeeping for every not-yet-handled record so each run
            # starts its attempts fresh; handled records (already uploaded /
            # kept local) are left alone. In-memory only — poll_once reads
            # self.data, and the first later mutation persists the cleared
            # state, so load() stays side-effect-free on disk.
            for rec in self.data.values():
                if isinstance(rec, dict) and not rec.get("handled"):
                    rec.pop("failed", None)
                    rec.pop("failures", None)
                    rec.pop("next_attempt", None)
            return self.data

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2)
                os.replace(tmp, self.path)
            except OSError as e:
                log("ERROR saving decisions: %s" % e)

    def has(self, gid):
        return gid in self.data

    def get(self, gid):
        return self.data.get(gid)

    def record(self, gid, name, choice, handled=False):
        with self._lock:
            self.data[gid] = {"name": name, "choice": choice,
                              "handled": handled}
            self.save()

    def set_choice(self, gid, choice):
        with self._lock:
            if gid in self.data:
                self.data[gid]["choice"] = choice
                self.save()

    def mark_handled(self, gid):
        """Mark a decision resolved (uploaded / kept-local / already-on-drive).

        Also pops any retry bookkeeping (failed/failures/next_attempt): a
        success must not leave stale failure state behind, because
        reupload_candidates now surfaces any not-handled record with
        failures>0, and DecisionStore.load()'s restart re-arm (see load())
        deliberately skips handled records — so a fail-once-then-succeed
        record left carrying failures>0 would be trapped in the menu forever.
        Harmless at the poll_once / 'already:' call sites (no bookkeeping to
        pop)."""
        with self._lock:
            rec = self.data.get(gid)
            if rec is not None:
                rec["handled"] = True
                rec.pop("failed", None)
                rec.pop("failures", None)
                rec.pop("next_attempt", None)
                self.save()

    def requeue(self, gid, choice):
        """Redirect a past decision to a drive and re-arm it for upload.

        Recovery path for an item that never landed on a shared drive: one
        kept local (choice "local") or a drive upload that gave up after
        repeated failures (failed=True). Sets the new choice and clears
        every flag that would stop poll_once from dispatching -- handled (so
        it is no longer "done") and the failed/failures/next_attempt backoff
        bookkeeping. handled flips back to True only when the ensuing upload
        actually succeeds (upload_done). Returns False for an unknown gid."""
        with self._lock:
            rec = self.data.get(gid)
            if rec is None:
                return False
            rec["choice"] = choice
            rec["handled"] = False
            rec.pop("failed", None)
            rec.pop("failures", None)
            rec.pop("next_attempt", None)
            self.save()
            return True

    def record_failure(self, gid, now, max_attempts, base, cap):
        """Note one failed upload attempt and schedule (or give up on) a retry.

        Increments the persisted failure count and, until the count reaches
        max_attempts, sets next_attempt to now + an exponentially growing
        backoff (base, 2*base, 4*base, ... capped at cap). At the cap the gid
        moves to a terminal handled=False, failed=True state so poll_once stops
        re-dispatching it — no more rclone spawns or 'Upload failed'
        notifications every tick. Returns the new failure count."""
        with self._lock:
            rec = self.data.get(gid)
            if rec is None:
                return 0
            n = rec.get("failures", 0) + 1
            rec["failures"] = n
            if n >= max_attempts:
                rec["failed"] = True
                rec["next_attempt"] = 0
            else:
                delay = min(base * (2 ** (n - 1)), cap)
                rec["next_attempt"] = now + delay
            self.save()
            return n

    def mark_failed(self, gid):
        """Terminal-fail an upload immediately, skipping the backoff retries.

        Used for a quota/drive-full failure: more attempts against the same
        full drive provably cannot succeed, so the gid goes straight to the
        terminal handled=False, failed=True state — where poll_once stops
        dispatching it and reupload_candidates surfaces it for a manual
        re-pick. Uses exactly the failed/failures/next_attempt keys, so
        DecisionStore.load()'s restart re-arm and requeue()'s pops both cover
        it unchanged. Returns False for an unknown gid."""
        with self._lock:
            rec = self.data.get(gid)
            if rec is None:
                return False
            rec["failures"] = rec.get("failures", 0) + 1
            rec["failed"] = True
            rec["next_attempt"] = 0
            self.save()
            return True


def reupload_candidates(store_data):
    """gids that never made it onto a shared drive and could be re-uploaded.

    A decision is a candidate when it was kept on the Mac (choice "local"),
    a drive upload gave up (failed=True), OR any NOT-handled record has
    recorded a failure (failures>0) — even attempt 1 of 5, still mid-backoff.
    That last clause surfaces an item for manual recovery without waiting out
    all the retries; picking it goes through requeue, which clears the
    backoff. The `not handled` qualifier is REQUIRED: it excludes records that
    failed then succeeded on a retry — including legacy records written before
    mark_handled started clearing 'failures' (still on disk as handled=True +
    failures>0, no migration). Requeueing such a handled drive-upload would
    flip handled=False on a payload already uploaded and deleted from disk,
    whose torrent perform_upload removed from the engine — the gid never
    reappears in tell_all, so it would be stuck unhandled permanently. Items
    merely pending with zero failures, already uploaded ("drive:..." +
    handled), or found on a drive ("already:...") stay excluded. Returns
    [(gid, name), ...] sorted by display name for a stable menu."""
    out = []
    for gid, rec in store_data.items():
        if not isinstance(rec, dict):
            continue
        if (rec.get("choice", "") == "local" or rec.get("failed")
                or (rec.get("failures", 0) > 0 and not rec.get("handled"))):
            out.append((gid, rec.get("name", gid)))
    return sorted(out, key=lambda t: t[1].lower())


class Poller:
    """Headless polling/decision/upload engine.

    All side effects are injected so this is testable without a UI:
      - client:      Aria2Client-like (is_up(), tell_all())
      - store:       DecisionStore
      - ask_cb(gid, name) -> choice string ("local" or "drive:<Name>")
      - upload_cb(path, drive_name, gid, display_name)  # runs the upload
      - notify_cb(title, subtitle, message)             # optional
      - is_paused_cb() -> bool  (auto-keep-local when True)

    poll_once() runs one cycle and returns a small status dict the UI can
    render from. It never raises on a down aria2."""

    def __init__(self, client, store, ask_cb, upload_cb,
                 notify_cb=None, is_paused_cb=None, now_fn=None,
                 ask_existing_cb=None):
        self.client = client
        self.store = store
        self.ask_cb = ask_cb
        # Optional separate ask for downloads first seen already complete
        # (pre-existing seeding torrents). May return None = no decision;
        # without it those fall back to the plain ask_cb, as before.
        self.ask_existing_cb = ask_existing_cb
        self.upload_cb = upload_cb
        self.notify_cb = notify_cb or (lambda *a: None)
        self.is_paused_cb = is_paused_cb or (lambda: False)
        # Injectable clock so the failed-upload backoff is testable without
        # sleeping. Defaults to wall-clock time.
        self._now = now_fn or time.time
        self.last_status = {"connected": False, "active": [], "recent": []}
        # gids currently mid-ask, so a fast poll doesn't ask twice.
        self._asking = set()
        # gids whose existing-torrent ask ended without a decision (timeout,
        # cancel). In-memory only: skipped for the rest of this app run,
        # re-asked on the next one.
        self._snoozed = set()
        # gids currently mid-upload, so a fast poll doesn't dispatch twice.
        # A decision is only marked handled once its upload succeeds (see
        # upload_done); until then this set — not the persisted handled flag —
        # is what prevents re-dispatch, so a failed/crashed upload is retried.
        self._uploading = set()
        # gids that are engine-complete but failed the on-disk readiness gate:
        # gid -> {"since": ts, "notified": bool}. In-memory only, so an app
        # restart re-logs the NOTREADY line and re-arms the stuck
        # notification — acceptable (at most one extra notify per run).
        self._not_ready = {}

    def poll_once(self):
        if not self.client.is_up():
            self.last_status = {"connected": False, "active": [],
                                "recent": self.last_status.get("recent", [])}
            return self.last_status

        try:
            downloads = self.client.tell_all()
        except Exception as e:
            log("poll error (tell_all): %s" % e)
            self.last_status = {"connected": False, "active": [],
                                "recent": self.last_status.get("recent", [])}
            return self.last_status

        active = []
        for dl in downloads:
            gid = dl.get("gid")
            if not gid:
                continue
            # Ask on the RAW status, upload on the normalized one: a torrent
            # first seen while seeding (raw "active", normalized "complete")
            # must still get its destination ask — keying the ask off the
            # normalized status would skip it forever, since the complete
            # branch below requires a recorded decision.
            raw_status = dl.get("status")
            status = effective_status(dl)
            name = download_name(dl)

            # (b) never-seen active/waiting/paused download -> ask. A raw
            # active but normalized complete download is a pre-existing
            # torrent seen mid-seed: it gets the existing-torrent ask
            # (drive pre-check, upload-and-remove wording, snooze on no
            # answer) instead of the new-download one.
            if (not self.store.has(gid) and gid not in self._asking
                    and gid not in self._snoozed):
                if raw_status in ("active", "waiting", "paused"):
                    self._ask_new(gid, name,
                                  already_complete=(status == "complete"))

            # (c) completed downloads bound for a drive -> upload once
            if status == "complete":
                rec = self.store.get(gid)
                # Skip: already handled, in flight, given up on (terminal
                # failed), or still inside its post-failure backoff window.
                # The backoff/failed gate is what stops a permanently failing
                # upload from re-dispatching rclone and spamming notifications
                # every POLL_SECONDS tick.
                if (rec and not rec.get("handled")
                        and not rec.get("failed")
                        and gid not in self._uploading
                        and rec.get("next_attempt", 0) <= self._now()):
                    choice = rec.get("choice", "local")
                    if choice.startswith("drive:"):
                        drive = choice[len("drive:"):]
                        path = resolve_local_path(dl)
                        if path:
                            # On-disk readiness gate: the engine saying
                            # "complete" is NOT enough to trigger the
                            # destructive rclone move (2026-07-06 .part
                            # incident). If the payload isn't ready, skip this
                            # tick WITHOUT consuming the decision or adding to
                            # _uploading — the POLL_SECONDS loop retries.
                            ready, reason = payload_ready(dl, path)
                            if not ready:
                                self._note_not_ready(gid, name, reason)
                                continue
                            self._clear_not_ready(gid)
                            # Marked handled only after the upload succeeds
                            # (upload_done); a failed dispatch must not orphan
                            # the decision. _uploading guards re-dispatch here.
                            self._uploading.add(gid)
                            log("UPLOAD dispatch gid=%s %r -> %s (%s)" %
                                (gid, name, drive, path))
                            self.upload_cb(path, drive, gid, name)
                        else:
                            self.store.mark_handled(gid)
                            log("ERROR: no local path for completed gid=%s %r"
                                % (gid, name))
                    else:  # "local" -> nothing to do
                        self.store.mark_handled(gid)
                        log("KEEP local gid=%s %r" % (gid, name))

            if status in ("active", "waiting", "paused"):
                pct, speed = download_progress(dl)
                active.append({"gid": gid, "name": name,
                               "pct": pct, "speed": speed, "status": status})

        self.last_status = {
            "connected": True,
            "active": active,
            "recent": self.last_status.get("recent", []),
        }
        return self.last_status

    def _ask_new(self, gid, name, already_complete=False):
        """Ask the user where a download should go, then persist.

        already_complete routes to ask_existing_cb (when wired): a torrent
        first seen mid-seed. That ask may return "already:<Drive>" (found on
        a drive -> recorded handled, nothing to do) or None (no decision ->
        snoozed for this run, NOT recorded — neither a timeout nor an ask
        error may permanently commit a seeding torrent to local; the silent
        auto-local on a crashed ask is exactly how the encoding bug ate
        drive choices)."""
        existing = already_complete and self.ask_existing_cb is not None
        if self.is_paused_cb():
            if existing:
                self._snoozed.add(gid)
                log("PAUSED asking -> snooze existing gid=%s %r" % (gid, name))
            else:
                self.store.record(gid, name, "local", handled=False)
                log("PAUSED asking -> auto-keep local gid=%s %r" % (gid, name))
            return
        self._asking.add(gid)
        try:
            if existing:
                choice = self.ask_existing_cb(gid, name)
            else:
                choice = self.ask_cb(gid, name) or "local"
        except Exception as e:
            log("ask error gid=%s: %s" % (gid, e))
            choice = None if existing else "local"
        finally:
            self._asking.discard(gid)
        if existing and not choice:
            self._snoozed.add(gid)
            log("SNOOZE gid=%s %r (no decision; re-ask next app run)"
                % (gid, name))
            return
        already = choice.startswith("already:")
        self.store.record(gid, name, choice, handled=already)
        log("DECISION gid=%s %r -> %s" % (gid, name, choice))
        if already:
            self.notify_cb("Already uploaded", name,
                           "Found on shared drive '%s' — leaving it alone"
                           % choice[len("already:"):])

    def upload_done(self, gid, success, quota=False):
        """Report an upload's outcome so a decision is consumed only when it
        actually landed. On success the decision is finally marked handled; on
        failure it stays handled=false and records a retry with exponential
        backoff (giving up into a terminal failed state after
        UPLOAD_MAX_ATTEMPTS) so a permanent failure no longer re-dispatches on
        every poll tick.

        `quota` marks a storage-quota / drive-full failure: since more attempts
        against the same full drive cannot succeed, it short-circuits straight
        to the terminal failed state (mark_failed) on the first failure instead
        of burning the backoff retries, and skips the 'gave up' notification
        (the UI's own failure notification + re-pick prompt cover it). The item
        surfaces immediately as a reupload_candidate for a manual re-pick.

        Either way the gid leaves the in-flight set — but only AFTER the store
        reflects the outcome: this runs on the upload worker thread while
        poll_once runs on the timer, and discarding first opens a window where a
        poll sees handled=False / no backoff and dispatches the same upload
        again."""
        if success:
            self.store.mark_handled(gid)
        elif quota:
            self.store.mark_failed(gid)
            log("UPLOAD quota-full gid=%s -> terminal failed immediately "
                "(won't retry the full drive); re-pick a drive from "
                "Re-upload" % gid)
        else:
            n = self.store.record_failure(
                gid, self._now(), UPLOAD_MAX_ATTEMPTS,
                UPLOAD_BACKOFF_BASE, UPLOAD_BACKOFF_CAP)
            if n >= UPLOAD_MAX_ATTEMPTS:
                rec = self.store.get(gid) or {}
                name = rec.get("name", gid)
                log("UPLOAD giving up gid=%s after %d attempts; terminal "
                    "failed state until the next app restart re-arms it "
                    "(DecisionStore.load resets it)" % (gid, n))
                # A give-up used to be log-only, so a payload that burned all
                # its attempts (overnight Wi-Fi drop, expired token) vanished
                # silently — the user assumed it uploaded. Notify ONCE so they
                # know to investigate; the retry itself waits for a restart.
                self.notify_cb(
                    "Upload gave up", name,
                    "Failed %d times — will retry on next app restart" % n)
        self._uploading.discard(gid)

    def requeue_for_upload(self, gid, choice):
        """Re-arm a kept-local / failed decision for a drive upload.

        Delegates the persisted-state reset to DecisionStore.requeue, then
        clears the in-memory guards that would otherwise suppress the next
        dispatch: the snooze set and the not-ready tracker. The upload itself
        still goes through the normal poll_once path -- on-disk readiness
        gate, _uploading guard, and upload_done marking handled only on a
        verified success -- so nothing is marked done until it truly lands on
        the drive. Returns True if the gid was known."""
        ok = self.store.requeue(gid, choice)
        if ok:
            self._snoozed.discard(gid)
            self._not_ready.pop(gid, None)
            log("REQUEUE gid=%s -> %s (re-armed for upload)" % (gid, choice))
        return ok

    def _note_not_ready(self, gid, name, reason):
        """Log NOTREADY once per stretch (not every 3s tick) and notify
        once if the payload stays engine-complete but not-ready past
        STUCK_NOTIFY_SECONDS — a silently never-uploading download is
        worse than a nagging one. Uses the injected clock so the stuck
        notification is testable without sleeping."""
        now = self._now()
        ent = self._not_ready.get(gid)
        if ent is None:
            self._not_ready[gid] = {"since": now, "notified": False}
            log("NOTREADY gid=%s %r: %s (upload deferred)" %
                (gid, name, reason))
        elif not ent["notified"] and now - ent["since"] >= STUCK_NOTIFY_SECONDS:
            ent["notified"] = True
            log("STUCK gid=%s %r not ready for %ds: %s"
                % (gid, name, int(now - ent["since"]), reason))
            self.notify_cb("Upload waiting", name,
                           "Complete but files not ready on disk — "
                           "see app.log")

    def _clear_not_ready(self, gid):
        """Drop a gid from the not-ready set once its payload passes the gate,
        logging the READY transition once (mirrors the NOTREADY log)."""
        if self._not_ready.pop(gid, None) is not None:
            log("READY gid=%s payload passed the on-disk gate" % gid)


# --------------------------------------------------------------------------
# osascript-based native dialogs (used by the UI; pure subprocess, testable
# only for esc()). Kept out of the UI class so they're importable.
# --------------------------------------------------------------------------

def _osascript(script):
    """Run an AppleScript, return (returncode, stdout, stderr).

    encoding is pinned to UTF-8: the frozen .app launched by launchd runs
    under the C locale, where text=True decodes as ASCII — and the reply
    "button returned:Shared Drive…" (U+2026) then blows up mid-ask, which
    the caller's catch-all turns into a silent "local" decision."""
    proc = subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def list_drive_names():
    """Fetch shared-drive names via `todrive list --json`. [] on any error.

    Passes TODRIVE_CONFIG so the child reads the same config.json this app
    does (critical when frozen: todrive's own SCRIPT_DIR is the config-free
    Resources dir). Captures the child's stderr into the log so a config /
    remote failure is diagnosable instead of surfacing as just an empty
    drive picker."""
    env = dict(os.environ, TODRIVE_CONFIG=CONFIG_FILE)
    try:
        out = subprocess.check_output(
            [sys.executable, TODRIVE, "list", "--json"],
            text=True, encoding="utf-8", errors="replace",
            stderr=subprocess.PIPE, env=env, timeout=30)
        drives = json.loads(out)
        return [d.get("name", "") for d in drives if d.get("name")]
    except subprocess.CalledProcessError as e:
        log("list_drive_names error: rc=%s %s" %
            (e.returncode, (e.stderr or "").strip()))
        return []
    except Exception as e:
        log("list_drive_names error: %s" % e)
        return []


def list_drive_usage():
    """Fetch per-drive storage usage via `todrive du --json`. None on any
    error (caller keeps whatever it had cached rather than blanking the
    table). Shape: {"cap_bytes": int, "drives": [{"id","name","bytes",
    "objects","trash_bytes","trash_objects","pct"}, ...]} (bytes/objects are
    content-only; trash_* is the trashed remainder that still counts against
    the cap).

    Same TODRIVE_CONFIG/stderr-logging pattern as list_drive_names: this can
    take seconds (a paged files.list scan across every shared drive), so it
    is only ever called from the background _usage_loop thread, never the
    render tick."""
    env = dict(os.environ, TODRIVE_CONFIG=CONFIG_FILE)
    try:
        out = subprocess.check_output(
            [sys.executable, TODRIVE, "du", "--json"],
            text=True, encoding="utf-8", errors="replace",
            stderr=subprocess.PIPE, env=env, timeout=120)
        return json.loads(out)
    except subprocess.CalledProcessError as e:
        log("list_drive_usage error: rc=%s %s" %
            (e.returncode, (e.stderr or "").strip()))
        return None
    except Exception as e:
        log("list_drive_usage error: %s" % e)
        return None


def drive_has(name, timeout=45):
    """The shared-drive name holding something called `name`, or "".

    Runs `todrive exists --json` (one Drive API files.list across all
    drives). Any error returns "" so callers over-ask instead of silently
    skipping a torrent — a duplicate ask is recoverable, a skipped one is
    not."""
    env = dict(os.environ, TODRIVE_CONFIG=CONFIG_FILE)
    try:
        out = subprocess.check_output(
            [sys.executable, TODRIVE, "exists", name, "--json"],
            text=True, encoding="utf-8", errors="replace",
            stderr=subprocess.PIPE, env=env, timeout=timeout)
        info = json.loads(out)
        matches = info.get("matches") or []
        if info.get("found") and matches:
            return matches[0].get("drive", "") or ""
        return ""
    except subprocess.CalledProcessError as e:
        log("drive_has error: rc=%s %s" %
            (e.returncode, (e.stderr or "").strip()))
        return ""
    except Exception as e:
        log("drive_has error: %s" % e)
        return ""


def _usage_label(name, usage):
    """LOCKED DECISION 3: the drive-picker label for `name` -- "<name> — N%
    full" from the cached `todrive du` snapshot ("<name> — >100% full" for a
    grandfathered drive over the cap), or the bare name when usage isn't
    cached yet (first launch / a failed scan) so the picker never blocks on
    a scan. `usage` is the {"cap_bytes":.., "drives":[...]} shape
    list_drive_usage() returns, or None."""
    if not usage:
        return name
    for d in usage.get("drives") or []:
        if d.get("name") == name:
            pct = d.get("pct", 0)
            tag = ">100% full" if pct > 100 else "%.0f%% full" % pct
            return "%s — %s" % (name, tag)
    return name


def format_storage_table(drives, cap_bytes, sort_key="total", descending=True):
    """Render the "Drive storage" table as a list of already-aligned
    monospaced lines. PURE and stdlib-only (no rumps/AppKit, no I/O) so it is
    unit-testable headless; _storage_menu is a thin monospaced-font wrapper
    around it.

    `sort_key` is one of "name"/"content"/"trash"/"total"/"free" (anything else
    falls back to "total"); `descending` picks the direction. "free" sorts by
    per-drive headroom (cap - total, negative past the cap), so descending=free
    lists the emptiest drives first and ascending lists the fullest first. Name
    (ascending) is always the tiebreak for equal primary keys, so the order is
    deterministic regardless of direction.

    `drives` is the "drives" list from a `todrive du --json` payload -- dicts
    with "name" (str), "bytes" (int, CONTENT only) and optionally
    "trash_bytes" (int); every numeric read uses .get(key, 0) so pre-trash
    persisted snapshots (which lack trash_*) still render. `cap_bytes` is the
    payload's cap_bytes (int) or None.

    Columns are DRIVE | CONTENT | TRASH | TOTAL | FREE, where TOTAL =
    content + trash and FREE = per-drive headroom = max(0, cap - TOTAL) --
    "over" for a grandfathered drive past the cap, and "?" for every row when
    cap_bytes is falsy (the menu must never crash at render time). Only
    non-empty drives (content>0 or trash>0) are shown, sorted by TOTAL
    descending (name as a deterministic tiebreak); an aggregate footer sums
    CONTENT/TRASH/TOTAL across the shown drives (blank FREE -- summing
    per-drive headroom is meaningless when the fleet scales by minting new
    drives). Returns [] when nothing survives the filter (the caller renders a
    placeholder). A trailing "Trash reclaimable: <size>" note is appended only
    when the aggregate trash is non-zero."""
    shown = []
    for d in drives or []:
        content = d.get("bytes", 0)
        trash = d.get("trash_bytes", 0)
        if content > 0 or trash > 0:
            shown.append((d.get("name", ""), content, trash))
    if not shown:
        return []
    if sort_key not in ("name", "content", "trash", "total", "free"):
        sort_key = "total"

    def _key(r):
        name, content, trash = r
        total = content + trash
        if sort_key == "name":
            return name.lower()
        if sort_key == "content":
            return content
        if sort_key == "trash":
            return trash
        if sort_key == "free":
            # per-drive headroom; negative once past the cap. With no cap,
            # fall back to -total so "more used = less free" still holds.
            return (cap_bytes - total) if cap_bytes else -total
        return total

    # Stable name-ascending baseline first, so equal primary keys keep a
    # deterministic name-asc tiebreak in EITHER direction (Python sort is
    # stable and reverse= doesn't disturb equal-key runs).
    shown.sort(key=lambda r: r[0].lower())
    shown.sort(key=_key, reverse=descending)

    def free_cell(total):
        if not cap_bytes:
            return "?"
        if total > cap_bytes:
            return "over"
        return human_size_dec(max(0, cap_bytes - total))

    labels = ["DRIVE", "CONTENT", "TRASH", "TOTAL", "FREE"]
    rows = [labels]
    tot_c = tot_t = 0
    for name, content, trash in shown:
        total = content + trash
        tot_c += content
        tot_t += trash
        rows.append([name, human_size_dec(content), human_size_dec(trash),
                     human_size_dec(total), free_cell(total)])
    grand = tot_c + tot_t
    footer = ["TOTAL (%d)" % len(shown), human_size_dec(tot_c),
              human_size_dec(tot_t), human_size_dec(grand), ""]
    rows.append(footer)

    widths = [max(len(r[i]) for r in rows) for i in range(len(labels))]

    def render(cells):
        # DRIVE left-aligned, the four numeric columns right-aligned, joined
        # by two spaces (the cmd_du table idiom).
        parts = ["%-*s" % (widths[0], cells[0])]
        parts += ["%*s" % (widths[i], cells[i]) for i in range(1, len(cells))]
        return "  ".join(parts)

    header_line = render(labels)
    width = len(header_line)
    lines = [header_line]
    lines += [render(r) for r in rows[1:-1]]
    lines.append("-" * width)
    lines.append(render(footer))
    if tot_t > 0:
        lines.append("Trash reclaimable: %s" % human_size_dec(tot_t))
    return lines


def _pick_drive(name, drive_lister, route_hint=None, usage=None, exclude=None):
    """Shared step 2: choose from existing drives or name a new one.
    Returns "drive:<Name>" or "local" (cancel / empty input).

    HOOK A (non-destructive): when `route_hint` names a drive that a prior
    season of this show already went to, that drive is moved to the top of the
    list AND pre-selected as the picker's default, with a hint line prepended to
    the prompt. The user still decides — this only changes the default, never
    the outcome.

    LOCKED DECISION 3: each existing drive is shown with its cached fill %
    (_usage_label) so the user can steer around a nearly-full drive before
    overflow routing kicks in mid-upload. The list itself shows the annotated
    label, but the AppleScript "choose from list" returns whatever label the
    user picked, so a label -> real-name map translates the pick back before
    it becomes a "drive:<Name>" choice.

    `exclude` (a set of drive names) drops those drives from the list — used by
    the quota re-pick to keep the just-failed full drive out of the choices."""
    NEW = "➕ New shared drive…"  # ➕ New shared drive…
    names = drive_lister()
    # exclude drops a just-failed-full drive from a quota re-pick; a hinted
    # drive that gets excluded simply fails the 'hinted in names' check below
    # and is silently dropped. "➕ New shared drive…" stays available as the
    # natural escape when every existing drive is full.
    if exclude:
        names = [n for n in names if n not in exclude]
    hinted = route_hint.drive if route_hint else None
    if hinted and hinted in names:
        names = [hinted] + [n for n in names if n != hinted]
    labels = [_usage_label(n, usage) for n in names]
    label_to_name = dict(zip(labels, names))
    items = [NEW] + labels
    listing = ", ".join('"%s"' % esc(n) for n in items)
    prompt = 'Send \\"%s\\" to which shared drive?' % esc(name)
    default_clause = ""
    hinted_label = _usage_label(hinted, usage) if hinted else None
    if hinted and hinted_label in items:
        prompt = ('Prior season went to \\"%s\\".\n%s'
                  % (esc(hinted_label), prompt))
        default_clause = ' default items {"%s"}' % esc(hinted_label)
    script = ('set theList to {%s}\n'
              'choose from list theList with title "drive-offload" '
              'with prompt "%s"%s'
              % (listing, prompt, default_clause))
    rc, out, _err = _osascript(script)
    if rc != 0 or out == "false" or not out:
        return "local"
    picked = out
    if picked == NEW:
        script = ('display dialog "Name the new shared drive for:\n%s" '
                  'default answer "" with title "drive-offload"'
                  % esc(name))
        rc, out, _err = _osascript(script)
        if rc != 0:
            return "local"
        # out looks like: button returned:OK, text returned:<name>
        marker = "text returned:"
        idx = out.find(marker)
        newname = out[idx + len(marker):].strip() if idx >= 0 else ""
        if not newname:
            return "local"
        return "drive:%s" % newname
    return "drive:%s" % label_to_name.get(picked, picked)


def ask_destination(gid, name, drive_lister=list_drive_names, route_hint=None,
                    usage=None):
    """Native two-step ask. Returns "local" or "drive:<Name>".

    Step 1: Keep on Mac / Shared Drive… (60s timeout -> Keep on Mac).
    Step 2: choose from existing drives + "New shared drive…"; new -> text
    input. Cancel anywhere -> "local".

    HOOK A: an optional `route_hint` (a renamer.RouteResult) surfaces where a
    prior season went — as a hint line here and a pre-seeded picker default in
    step 2. Never overrides the user's choice.

    `usage` (LOCKED DECISION 3) is the cached `todrive du` snapshot, threaded
    through to _pick_drive so the picker can annotate each drive with its
    fill %."""
    prompt = ('New download:\n%s\n\nWhere should it go when it finishes?'
              % name)
    if route_hint and route_hint.drive:
        prompt = ('New download:\n%s\n\nPrior season went to "%s".\n'
                  'Where should it go when it finishes?'
                  % (name, route_hint.drive))
    script = ('display dialog "%s" buttons {"Keep on Mac", "Shared Drive…"} '
              'default button "Shared Drive…" '
              'with title "drive-offload" giving up after 60'
              % esc(prompt))
    rc, out, _err = _osascript(script)
    # timeout (gave up) or cancel -> keep local
    if rc != 0:
        log("ask gid=%s: dialog cancelled -> local" % gid)
        return "local"
    if "gave up:true" in out:
        log("ask gid=%s: dialog timed out -> local" % gid)
        return "local"
    if "Keep on Mac" in out:
        return "local"
    return _pick_drive(name, drive_lister, route_hint=route_hint, usage=usage)


def ask_existing(gid, name, drive_lister=list_drive_names,
                 drive_finder=drive_has, route_hint=None, usage=None):
    """Ask about a torrent first seen already complete (a seeding leftover).

    Pre-checks the drive so nothing already uploaded ever prompts. Returns:
      "already:<Drive>" — found on a shared drive; no dialog shown;
      "drive:<Name>"    — upload it and remove it from the download client;
      "local"           — user explicitly kept it on the Mac (never re-asked);
      None              — timeout/cancel: snooze; re-asked on the next app
                          run, because walking away from the Mac must not
                          permanently commit every seeding torrent to local
                          (the way the new-download ask deliberately does)."""
    found = drive_finder(name)
    if found:
        log("ALREADY on drive gid=%s %r -> %s" % (gid, name, found))
        return "already:%s" % found
    prompt = ('Seeding, and not on any shared drive yet:\n%s\n\n'
              'Upload it to a shared drive and remove it from '
              'Motrix/Transmission, or keep it on this Mac?' % name)
    script = ('display dialog "%s" '
              'buttons {"Keep on Mac", "Upload & Remove…"} '
              'default button "Upload & Remove…" '
              'with title "drive-offload" giving up after 60'
              % esc(prompt))
    rc, out, _err = _osascript(script)
    if rc != 0:
        log("ask-existing gid=%s: dialog cancelled -> snooze" % gid)
        return None
    if "gave up:true" in out:
        log("ask-existing gid=%s: dialog timed out -> snooze" % gid)
        return None
    if "Keep on Mac" in out:
        return "local"
    choice = _pick_drive(name, drive_lister, route_hint=route_hint, usage=usage)
    # Cancelling the picker isn't "keep": the user asked to upload and then
    # backed out, so snooze rather than record a permanent local decision.
    return choice if choice.startswith("drive:") else None


def confirm_new_show(show_name, sample_lines):
    """One-time HOOK B confirm for a show NOT yet in the rename cache.

    Shows the derived show name as an EDITABLE default plus the first few
    old->new episode lines, so the user can trim a vanity prefix ("PROD
    Northbound" -> "Northbound") once and pin it in the cache. Returns the
    (possibly edited) show name, or None on timeout/cancel — the caller then
    uploads AS-IS (never auto-guess a name overnight)."""
    preview = "\n".join("• %s\n     → %s" % (esc(a), esc(b))
                        for a, b in list(sample_lines)[:3])
    body = ("New show — clean up the name before upload?\n\n%s\n\n"
            "Show name:" % preview)
    script = ('display dialog "%s" default answer "%s" '
              'with title "drive-offload" giving up after 60'
              % (body, esc(show_name)))
    rc, out, _err = _osascript(script)
    if rc != 0 or "gave up:true" in out:
        return None
    marker = "text returned:"
    idx = out.find(marker)
    edited = out[idx + len(marker):].strip() if idx >= 0 else ""
    return edited or show_name


# --------------------------------------------------------------------------
# App state (speed limit + pause toggle), persisted to app_state.json.
# --------------------------------------------------------------------------

class AppState:
    def __init__(self, path=STATE_FILE):
        self.path = path
        self.data = {"paused_asking": False, "bwlimit": "", "drive_usage": None,
                     "sort_key": "total", "sort_desc": True}
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=2)
        except OSError:
            pass

    @property
    def paused(self):
        return bool(self.data.get("paused_asking"))

    @paused.setter
    def paused(self, v):
        self.data["paused_asking"] = bool(v)
        self.save()

    @property
    def bwlimit(self):
        return self.data.get("bwlimit", "")

    @bwlimit.setter
    def bwlimit(self, v):
        self.data["bwlimit"] = v or ""
        self.save()

    @property
    def drive_usage(self):
        return self.data.get("drive_usage")

    @drive_usage.setter
    def drive_usage(self, v):
        self.data["drive_usage"] = v
        self.save()

    @property
    def sort_key(self):
        return self.data.get("sort_key", "total")

    @sort_key.setter
    def sort_key(self, v):
        self.data["sort_key"] = v or "total"
        self.save()

    @property
    def sort_desc(self):
        return bool(self.data.get("sort_desc", True))

    @sort_desc.setter
    def sort_desc(self, v):
        self.data["sort_desc"] = bool(v)
        self.save()


def tree_size(path):
    """Total bytes at path (recursive for dirs). 0 on any error."""
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        for root, _dirs, files in os.walk(path):
            for fn in files:
                fp = os.path.join(root, fn)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def payload_ready(dl, path):
    """(ready, reason) -- is a download's on-disk payload safe to move?

    Defense in depth after the 2026-07-06 .part incident: the engine's
    "complete" can never again be the sole trigger for a destructive
    rclone move. Checks, in order:
      1. every SELECTED files[] path must exist under its FINAL name OR be
         verifiably ABSENT. Absent WITH an in-progress marker sibling for it
         (any INCOMPLETE_SUFFIX, case-insensitive) is the Transmission rename
         race (engine reports the final name while the disk still has
         "<name>.part") and BLOCKS -- for a single-file payload this per-file
         probe is the ONLY cover for that race (check 2 never scans a file
         payload's non-.aria2 siblings). Absent with NO marker means the file
         was already moved away or cleaned -- the partial-move case (a
         per-file rclone move uploaded then deleted the small sidecar files,
         then failed on the large file): rclone's
         per-file move uploaded+deleted the small files, then failed on the
         .mkv (drive quota), leaving a stable folder whose manifest still lists
         the gone files. Such a file is legitimately absent: it is collected
         into `absent` (dropped from the check-3 sum) instead of blocking, so
         readiness keys to the STABLE ON-DISK FOLDER, not the manifest. A guard
         then requires at least one selected file on disk -- nothing to move
         otherwise -- which keeps [METADATA] magnets and fully-moved/empty
         payloads not-ready. (Assumes the engine keeps in-progress data at
         "<final>+suffix" in the SAME dir -- Transmission rename-partial-files,
         aria2 final-name-from-byte-0; Transmission incomplete-dir mode would
         violate this, worst case a stranded late file, not data loss.)
      2. no incomplete-download marker (INCOMPLETE_SUFFIXES,
         case-insensitive, files AND dirs -- Safari's .download is a
         bundle) anywhere under path, EXCEPT a ".part" left behind by a
         deselected file (a complete torrent keeps a deselected "<name>.part"
         forever), nor a "<path>.aria2" control-file sibling -- the sibling
         only for a NON-BitTorrent payload, since aria2 keeps a verified BT
         torrent's control file through seeding;
      3. on-disk size >= expected size (sum of PRESENT selected files'
         lengths, else totalLength when nothing is legitimately absent).
         An absent file's length would inflate the expected sum and re-block
         the partial-move case one check down, so absent files are dropped; truncation
         detection for files that ARE on disk is unweakened. Sparse/
         preallocated files pass this while incomplete, so it only guards
         truncation; 1+2 are the real gates.

    Behavior note: an aria2 magnet still fetching metadata carries a
    files[0].path like "[METADATA]name" that never lands on disk (no marker),
    so the at-least-one-present guard keeps it not-ready here (surfacing via
    the 30-min stuck notification) rather than dispatching -- an improvement
    over the old five-failed-rclone path. The files-exist check is also inert
    for aria2 HTTP downloads (aria2 writes the final name from byte 0), where
    the "<path>.aria2" sibling is the only real gate."""
    def _selected(f):
        return str(f.get("selected", "true")).lower() != "false"

    def _marker_for(p):
        # Case-insensitive probe for an in-progress marker of ONE file: scan
        # the parent dir by name (not os.path.exists, which would miss ".PART"
        # on a case-sensitive FS) for "<basename>+suffix", file OR dir (Safari's
        # .download is a bundle). Returns the marker path, or None -- including
        # when the parent dir itself is gone (no marker can exist there).
        d, base = os.path.split(p)
        want = {(base + suf).lower() for suf in INCOMPLETE_SUFFIXES}
        try:
            names = os.listdir(d or ".")
        except OSError:
            return None
        for n in names:
            if n.lower() in want:
                return os.path.join(d, n)
        return None

    # (1) every selected file must be on disk under its final name, OR be
    # verifiably absent. Absent + a marker for it = the rename race (BLOCK);
    # absent + no marker = already moved/cleaned (collect into `absent`).
    absent = set()          # normpath of legitimately-absent selected paths
    present_any = False
    have_selected = False
    for f in (dl.get("files") or []):
        p = f.get("path")
        if not p or not _selected(f):
            continue
        have_selected = True
        if os.path.exists(p):
            present_any = True
            continue
        m = _marker_for(p)
        if m is not None:
            return False, "missing on disk: %s (in-progress marker %s)" % (p, m)
        absent.add(os.path.normpath(p))

    # Nothing to move if the manifest names selected files but none is on disk:
    # an aria2 [METADATA]name magnet (never lands, no marker) or a fully-moved/
    # empty payload. Stays not-ready (surfaces via the 30-min stuck notice).
    if have_selected and not present_any:
        return False, "nothing on disk: %s" % path

    # (2) no in-progress-download marker anywhere under path.
    #
    # A leftover ".part" from a DESELECTED file is NOT payload incompleteness:
    # Transmission's rename-partial-files keeps "<name>.part" on disk forever
    # for a file the user unchecked, and its final-name counterpart is one we
    # never expect on disk (check 1 already skips deselected files). Blocking
    # on it would defer a genuinely-complete torrent every tick, forever. So
    # skip a marker whose final name (its suffix stripped) maps to a deselected
    # files[] entry; the blanket walk still catches every other marker, and a
    # payload with no files[] metadata gets the plain blanket walk unchanged.
    deselected = {os.path.normpath(f["path"])
                  for f in (dl.get("files") or [])
                  if f.get("path") and not _selected(f)}

    def _is_deselected_marker(fp):
        low = fp.lower()
        for suf in INCOMPLETE_SUFFIXES:
            if low.endswith(suf):
                return os.path.normpath(fp[:-len(suf)]) in deselected
        return False

    if os.path.basename(path).lower().endswith(INCOMPLETE_SUFFIXES):
        return False, "incomplete marker: %s" % path
    # A verified BitTorrent payload (dl carries a "bittorrent" key) is safe the
    # moment it reads complete — every piece is hash-checked — yet aria2 can
    # keep its ".aria2" control file through the whole seeding phase (under
    # --force-save, which Motrix uses to preserve seeding across restarts).
    # Treating that lingering control file as "incomplete" would defer a
    # finished torrent forever, defeating effective_status's upload-during-
    # seeding purpose, so the .aria2-sibling check is aria2-HTTP-only. The
    # docstring's belt-and-suspenders truncation guard (check 3) still applies.
    is_bt = bool(dl.get("bittorrent"))
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            for n in dirs + files:
                if n.lower().endswith(INCOMPLETE_SUFFIXES):
                    fp = os.path.join(root, n)
                    if _is_deselected_marker(fp):
                        continue
                    return False, "incomplete marker: %s" % fp
        # aria2's control file for a multi-file payload sits BESIDE the dir as
        # "<dir>.aria2" (never inside it, so the walk above misses it).
        if not is_bt and os.path.exists(path + ".aria2"):
            return False, "incomplete marker: %s.aria2" % path
    elif not is_bt and os.path.exists(path + ".aria2"):
        # single-file payload: aria2's control file sits beside it.
        return False, "incomplete marker: %s.aria2" % path

    # (3) truncation guard. Expected size = sum of PRESENT selected files'
    # lengths when they all carry one (partial-selection safe), else
    # totalLength -- but the totalLength fallback ONLY when nothing is
    # legitimately absent, since totalLength counts the absent bytes and would
    # false-block forever once a file has been moved away. The ENTIRE
    # computation stays inside the try/except so a malformed files[].length
    # still degrades to expected = 0 (check skipped) rather than raising out of
    # payload_ready on a poll tick.
    expected = 0
    try:
        selected = [f for f in (dl.get("files") or []) if _selected(f)]
        present = [f for f in selected
                   if not (f.get("path")
                           and os.path.normpath(f["path"]) in absent)]
        lengths = [int(f["length"]) for f in present
                   if f.get("length") is not None]
        if lengths and len(lengths) == len(present):
            expected = sum(lengths)
        elif not absent:
            expected = int(dl.get("totalLength", 0) or 0)
        # else: absent files but incomplete per-file lengths -> leave
        # expected = 0; totalLength would count the absent bytes and 1+2
        # remain the real gates.
    except (TypeError, ValueError):
        expected = 0
    if expected > 0 and tree_size(path) < expected:
        return False, "size %d < expected %d" % (tree_size(path), expected)

    return True, ""


# rclone -P byte-progress line, e.g.
#   Transferred:   105.017 MiB / 1.938 GiB, 5%, 10.500 MiB/s, ETA 2m58s
# (the file-count "Transferred: 0 / 1, 0%" line has no B-unit and is skipped)
_PROGRESS_RE = re.compile(
    r"Transferred:\s+[\d.]+\s*\S*B\s*/\s*[\d.]+\s*\S*B,\s*(?P<pct>\d+)%"
    r"(?:,\s*(?P<speed>[\d.]+\s*\S+/s))?(?:,\s*ETA\s*(?P<eta>\S+))?")

# Lines of the repeating rclone -P stats block: dropped from the collected
# output (only the final byte-progress line is kept) so app.log stays small.
_PROGRESS_NOISE_RE = re.compile(
    r"^\s*$|^(Transferred|Checks|Checking|Deleted|Renamed|Errors"
    r"|Elapsed time|Transferring):?|^ \* ")


def run_todrive_up(path, drive_name, bwlimit="", cwd=SCRIPT_DIR,
                   progress_cb=None):
    """Run `todrive up <path> <drive_name>`, honoring an RCLONE_BWLIMIT env.

    Streams the child's output as it runs: each rclone `-P` stats line is
    parsed and fed to progress_cb as {"pct": int, "speed": str, "eta": str}
    so the UI can show live upload progress. Returns (returncode,
    combined_output) with the repeating stats blocks collapsed to the final
    byte-progress line. rclone maps every flag to an env var
    RCLONE_<UPPER_FLAG>, so RCLONE_BWLIMIT=5M == `--bwlimit 5M`."""
    env = dict(os.environ)
    if bwlimit:
        env["RCLONE_BWLIMIT"] = bwlimit
    else:
        env.pop("RCLONE_BWLIMIT", None)
    # The frozen .app runs under the C locale; without this the child's own
    # stdout falls back to ASCII and a non-ASCII filename in its "OK: <path>"
    # line would crash it after a successful upload.
    env["PYTHONIOENCODING"] = "utf-8"
    # Point todrive at this app's config so the child resolves the same
    # base_remote (frozen, todrive's SCRIPT_DIR ships no config.json).
    env["TODRIVE_CONFIG"] = CONFIG_FILE
    cmd = [sys.executable, "-u", TODRIVE, "up", path, drive_name]
    # errors="replace": a stray non-UTF-8 byte in a filename must not blow
    # up the read loop mid-upload.
    proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    lines = []
    last_progress = ""
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            m = _PROGRESS_RE.search(line)
            if m:
                # Slice from the match: rclone's refresh chunks can arrive
                # without a trailing newline, gluing a " * file: N%..."
                # sub-line onto the front of the next Transferred: line.
                last_progress = line[m.start():].strip()
                if progress_cb:
                    try:
                        progress_cb({"pct": int(m.group("pct")),
                                     "speed": (m.group("speed") or "").strip(),
                                     "eta": (m.group("eta") or "").strip()})
                    except Exception as e:
                        log("progress_cb error: %s" % e)
                continue
            if not _PROGRESS_NOISE_RE.search(line):
                lines.append(line)
        proc.stdout.close()
        rc = proc.wait()
    except BaseException:
        # Don't orphan a live rclone (which would upload AND delete the
        # payload with nobody watching the return code) — kill and reap.
        proc.kill()
        proc.wait()
        raise
    if last_progress:
        lines.append(last_progress)
    return rc, "\n".join(lines)


def _noop(*_a, **_k):
    pass


def _apply_rename_hook(path, gid, name, drive, rename_cfg, cache, confirm_cb,
                       support_dir=SUPPORT_DIR):
    """HOOK B core: decide + (in the live path) apply the TV rename between
    engine-stop and rclone-move. Returns a dict:

        upload_path : the path run_todrive_up should upload (== path unless a
                      live rename moved the payload to a canonical root)
        renamed     : True iff files were actually moved on disk
        finalize()  : call on a verified upload success (clears the journal,
                      records the season/history in the cache)
        rollback()  : call on an upload failure AFTER a live rename (dst->src)

    Every gate miss — disabled, not TV, plan rejected (R13), already canonical,
    dry-run, a multi-root series pack, or a cancelled/timed-out new-show confirm
    — returns UPLOAD-AS-IS (renamed False), byte-identical to pre-engine
    behavior. See RENAME_ENGINE_PLAN.md §3/§4/§9."""
    asis = {"upload_path": path, "renamed": False,
            "finalize": _noop, "rollback": _noop}
    if not rename_cfg or not rename_cfg.get("enabled", True):
        return asis
    try:
        base_dir, relpaths = renamer.scan_payload(path)
    except OSError as e:
        log("RENAME scan failed gid=%s: %s -> upload as-is" % (gid, e))
        return asis

    ident = renamer.identify(relpaths)
    if not ident.is_tv:
        log("RENAME skip gid=%s: not identified as TV -> upload as-is" % gid)
        return asis

    slug = ident.show_slug
    show_override = None
    show_rec = cache.lookup(slug) if cache is not None else None
    if show_rec:
        show_override = show_rec.get("display")   # cached shows rename silently
    elif rename_cfg.get("confirm_new_shows", True) and confirm_cb is not None:
        preview = renamer.plan(relpaths)
        samples = [(os.path.basename(s), os.path.basename(d))
                   for s, d in preview.file_ops[:3]]
        edited = confirm_cb(ident.show, samples)
        if edited is None:
            log("RENAME confirm cancelled/timeout gid=%s -> upload as-is" % gid)
            return asis
        show_override = edited
        if cache is not None:
            cache.record_show(slug, display=edited, aliases=[slug], drive=drive)
            log("RENAME cache stub seeded slug=%s display=%r drive=%s"
                % (slug, edited, drive))

    planobj = renamer.plan(relpaths, show_override=show_override)
    if planobj.rejected:
        log("RENAME PLAN rejected gid=%s: %s -> upload as-is"
            % (gid, planobj.reason))
        return asis
    if not planobj.ops and not planobj.deletes:
        log("RENAME gid=%s: already canonical, nothing to do" % gid)
        return asis

    if rename_cfg.get("dry_run", True):
        log("RENAME PLAN (dry-run) gid=%s show=%r:"
            % (gid, planobj.show or ident.show))
        for s, d in planobj.file_ops:
            log("  f  %s -> %s" % (s, d))
        for s, d in planobj.dir_ops:
            log("  D  %s -> %s" % (s, d))
        for d in planobj.deletes:
            log("  x  %s" % d)
        log("RENAME (dry-run) gid=%s: uploading as-is (byte-identical to today)"
            % gid)
        return asis

    # --- live path (dry_run False) ---------------------------------------
    new_top = renamer.plan_new_root(planobj)
    if new_top is None:
        log("RENAME gid=%s: plan yields multiple top-level roots (series "
            "pack) -> declining live rename, upload as-is" % gid)
        return asis

    renames = renamer.build_rename_ops(base_dir, planobj)
    jpath = renamer.journal_default_path(support_dir)
    try:
        renamer.write_journal(jpath, gid, base_dir, renames)
    except OSError as e:
        log("RENAME journal write failed gid=%s: %s -> upload as-is" % (gid, e))
        return asis
    log("RENAME journal written gid=%s (%d op(s))" % (gid, len(renames)))
    try:
        renamer.apply_rename(base_dir, planobj, log=log)
    except OSError as e:
        log("RENAME apply FAILED gid=%s: %s -- rolling back" % (gid, e))
        data = renamer.read_journal(jpath) or {"renames": []}
        renamer.rollback_rename([(s, d) for s, d in data["renames"]], log=log)
        renamer.clear_journal(jpath)
        return asis

    upload_path = os.path.join(base_dir, new_top)
    log("RENAME applied gid=%s -> %s" % (gid, upload_path))

    season = ident.season
    src_pattern = os.path.basename(path)
    history = [{"gid": gid, "original": os.path.basename(s),
                "renamed": os.path.basename(d), "ts": int(time.time())}
               for s, d in planobj.file_ops]

    def finalize():
        renamer.clear_journal(jpath)
        if cache is None:
            return
        if cache.lookup(slug) is None:
            cache.record_show(slug, display=(show_override or ident.show),
                              aliases=[slug], drive=drive)
        if season is not None:
            cache.record_season(slug, season, gid, len(ident.episodes),
                                src_pattern)
        cache.append_history(slug, history)
        log("RENAME cache updated slug=%s season=%s" % (slug, season))

    def rollback():
        data = renamer.read_journal(jpath) or {"renames": []}
        restored, partial = renamer.rollback_rename(
            [(s, d) for s, d in data["renames"]], log=log)
        renamer.clear_journal(jpath)
        if partial:
            log("RENAME ROLLBACK PARTIAL gid=%s: some files already moved by "
                "rclone; on-disk state may be mixed -- see above" % gid)
        else:
            log("RENAME rolled back gid=%s (%d op(s) restored)"
                % (gid, restored))

    return {"upload_path": upload_path, "renamed": True,
            "finalize": finalize, "rollback": rollback}


def perform_upload(client, path, drive, gid, bwlimit="",
                   todrive_up=run_todrive_up, progress_cb=None,
                   name=None, rename_cfg=None, cache=None, confirm_cb=None,
                   support_dir=SUPPORT_DIR):
    """Offload one completed download, with the owning engine's lifecycle
    wrapped around the upload. Returns (returncode, combined_output).

    For BOTH engines: stop the download first (a finished torrent may still
    be seeding, and it can't be mid-seed while todrive uploads and deletes
    the payload), then upload, then on a verified success (rc == 0) remove
    the now-dataless entry so the engine doesn't sit on a 'data missing'
    error. Each engine's hooks are best-effort no-ops where they don't apply
    (e.g. a completed plain-HTTP aria2 download can't be paused).

    HOOK B (RENAME_ENGINE_PLAN.md §3/§4): between engine-stop and rclone-move,
    the TV rename engine may journal + rewrite the payload into the canonical
    on-drive layout (gated by rename_cfg; dry-run by default = no on-disk
    change). On FAILURE the failure path forks on whether a rename happened:
      - NOT renamed: resume the paused download (today's behavior; data intact).
      - renamed: REMOVE-not-resume (plan §9). A resumed torrent would re-verify
        against the renamed files and fail, so the rename is rolled back
        (dst->src) and the torrent is left stopped rather than restarted."""
    eng = client.for_gid(gid)
    if eng is not None:
        log("ENGINE stop before upload gid=%s" % gid)
        eng.stop_torrent(gid)

    hook = _apply_rename_hook(path, gid, name, drive, rename_cfg, cache,
                              confirm_cb, support_dir=support_dir)

    rc, output = todrive_up(hook["upload_path"], drive, bwlimit,
                            progress_cb=progress_cb)

    if rc == 0:
        if eng is not None:
            log("ENGINE remove after upload gid=%s" % gid)
            eng.remove_torrent(gid)
        if hook["renamed"]:
            hook["finalize"]()
    else:
        if hook["renamed"]:
            # Remove-not-resume: never resume a renamed payload (it would
            # re-verify against the new names and fail). Restore the originals.
            log("ENGINE renamed+failed gid=%s: rolling back rename, NOT "
                "resuming torrent (remove-not-resume)" % gid)
            hook["rollback"]()
        elif eng is not None:
            # Upload failed with no rename: resume the download we paused so a
            # failed offload doesn't leave it stuck paused (data intact).
            log("ENGINE resume after failed upload gid=%s" % gid)
            eng.start_torrent(gid)
    return rc, output


# ==========================================================================
# UI (rumps). Everything below is only reached when run as a program; rumps
# is imported lazily so the logic above stays importable without it.
# ==========================================================================

def _run_app():
    import rumps
    # PyObjC is already present as rumps' own dependency; import it lazily here
    # (mirroring "rumps is imported lazily") so the module stays importable and
    # testable headless -- test_offload_app.py imports offload_app without
    # rumps. Used only by _mono_item for the monospaced storage table.
    from AppKit import NSFont, NSFontAttributeName
    from Foundation import NSAttributedString

    def _mono_item(text, callback=None):
        """A rumps.MenuItem whose displayed title is set in the monospaced
        system font, so the format_storage_table columns line up (macOS menus
        default to a proportional font). rumps.MenuItem wraps NSMenuItem at
        ._menuitem; setAttributedTitle_ overrides only the rendered title
        while .title stays the plain string. A font/PyObjC hiccup degrades to
        the plain proportional title rather than crashing the 3s render tick."""
        it = rumps.MenuItem(text, callback=callback)
        try:
            attr = NSAttributedString.alloc().initWithString_attributes_(
                text, {NSFontAttributeName:
                       NSFont.monospacedSystemFontOfSize_weight_(
                           NSFont.systemFontSize(), 0.0)})
            it._menuitem.setAttributedTitle_(attr)
        except Exception:
            pass
        return it

    class OffloadApp(rumps.App):
        def __init__(self):
            super().__init__(ICON_IDLE, quit_button=None)
            port, secret = read_motrix_rpc()
            tm_port, tm_user, tm_pass = read_transmission_rpc()
            aria2 = Aria2Client(port=port, secret=secret)
            transmission = TransmissionClient(
                port=tm_port, user=tm_user, password=tm_pass)
            self.client = MultiClient([("Motrix", aria2),
                                       ("Transmission", transmission)])
            self.store = DecisionStore()
            self.state = AppState()
            # TV rename engine: the continuity cache (HOOK A routing + HOOK B
            # naming) and the config gate. dry_run defaults True until reviewed.
            self.rename_cache = renamer.RenameCache(RENAME_CACHE_FILE)
            self.rename_cfg = read_rename_config()
            # Failure-recovery gate + the per-run set of gids already offered
            # the "pick another drive" prompt (caps it at one dialog per gid
            # per app run so generic retries don't nag).
            self.recover_cfg = read_recover_config()
            self._repick_prompted = set()
            self.uploads = {}   # gid -> {"name":.., "drive":..}
            self.recent = []    # list of strings, newest first
            self._lock = threading.Lock()

            # Drive storage usage (FEATURE A "Drive storage" menu). Mirrors
            # self.state.drive_usage (persisted) so a restart shows the
            # last-known table instead of blanking to "Loading…" every
            # launch; only ever None on a genuine first run. `todrive du` is
            # a paged files.list scan across every shared drive -- seconds,
            # not milliseconds -- so it runs on its own daemon thread and is
            # NEVER called from the POLL_SECONDS render tick. The thread is
            # woken early (instead of waiting the full
            # DRIVE_USAGE_REFRESH_SECONDS) right after a successful upload
            # and on a manual "Refresh now" click.
            self._usage = self.state.drive_usage
            self._usage_error = False
            self._sort_key = self.state.sort_key
            self._sort_desc = self.state.sort_desc
            self._usage_wake = threading.Event()
            threading.Thread(target=self._usage_loop, daemon=True).start()
            self._usage_wake.set()  # kick off an immediate first scan

            # Crash recovery (Phase 7): a leftover rename journal means the app
            # died between an on-disk rename and a verified upload. Replay it in
            # reverse to restore the original names BEFORE the first poll_once,
            # so a payload never sits mid-window with unfamiliar names.
            try:
                if renamer.replay_journal(RENAME_JOURNAL_FILE, log=log):
                    log("RENAME startup recovery complete")
            except Exception as e:
                log("RENAME startup recovery error: %s" % e)

            self.poller = Poller(
                self.client, self.store,
                ask_cb=self._ask_destination,
                ask_existing_cb=self._ask_existing,
                upload_cb=self._start_upload,
                notify_cb=self._notify,
                is_paused_cb=lambda: self.state.paused,
            )

            self._build_menu()
            self.timer = rumps.Timer(self._on_tick, POLL_SECONDS)
            self.timer.start()
            log("offload_app started (Motrix aria2 %s secret=%s; "
                "Transmission %s auth=%s)" %
                (aria2.url, "set" if secret else "none",
                 transmission.url, "set" if (tm_user or tm_pass) else "none"))

        # ---- FEATURE A: background drive-usage scan ----
        def _usage_loop(self):
            """Daemon thread: refresh self._usage (and the persisted
            self.state.drive_usage) via `todrive du --json`. Blocks on
            self._usage_wake so it fires immediately after a successful
            upload or a manual "Refresh now" click (see _refresh_usage_now)
            instead of only every DRIVE_USAGE_REFRESH_SECONDS. A failed scan
            sets self._usage_error and otherwise changes nothing -- the
            table keeps showing the last-known numbers rather than blanking
            (see _storage_menu)."""
            while True:
                self._usage_wake.wait(DRIVE_USAGE_REFRESH_SECONDS)
                self._usage_wake.clear()
                data = list_drive_usage()
                if data:
                    data["ts"] = time.time()
                    with self._lock:
                        self._usage = data
                        self._usage_error = False
                    # Persist OUTSIDE the lock: this thread is the only writer
                    # of drive_usage, and the setter's disk write must not
                    # block the render tick that briefly takes self._lock.
                    self.state.drive_usage = data
                else:
                    with self._lock:
                        self._usage_error = True

        # ---- menu construction ----
        def _build_menu(self):
            self.status_item = rumps.MenuItem("Engines: starting…")
            self.pause_item = rumps.MenuItem(
                "Pause asking (auto-keep local)", callback=self._toggle_pause)
            self.pause_item.state = 1 if self.state.paused else 0

            self.menu = [
                self.status_item,
                None,
                # active / upload / recent sections are rebuilt each tick
                self.pause_item,
                self._speed_menu(),
                self._reupload_menu(),
                self._storage_menu(),
                None,
                rumps.MenuItem("Open log", callback=self._open_log),
                rumps.MenuItem("Quit", callback=self._quit),
            ]

        # ---- HOOK A: continuity routing at the ask dialog ----
        def _route_hint(self, name):
            """A renamer.RouteResult for `name` (prior season's drive), or None.
            Consults the rename cache first, then mines decisions.json. Any
            error is swallowed so a routing hiccup never blocks the ask."""
            try:
                ident = renamer.identify(name)
                slug = ident.show_slug if ident.is_tv else None
                if not slug:
                    key = renamer.derive_show_key(name)
                    if key is None:
                        return None
                    slug = key[0]
                hint = renamer.route(slug, [], self.rename_cache,
                                     self.store.data)
                if hint is not None:
                    log("ROUTE hint gid? name=%r -> %s (%s, %.2f)"
                        % (name, hint.drive, hint.source, hint.confidence))
                return hint
            except Exception as e:
                log("route hint error name=%r: %s" % (name, e))
                return None

        def _ask_destination(self, gid, name):
            with self._lock:
                usage = self._usage
            return ask_destination(gid, name,
                                   route_hint=self._route_hint(name),
                                   usage=usage)

        def _ask_existing(self, gid, name):
            with self._lock:
                usage = self._usage
            return ask_existing(gid, name, route_hint=self._route_hint(name),
                                usage=usage)

        # ---- timer tick ----
        def _on_tick(self, _sender):
            try:
                status = self.poller.poll_once()
            except Exception as e:
                log("tick error: %s" % e)
                return
            self._render(status)

        def _render(self, status):
            connected = status.get("connected")
            active = status.get("active", [])
            engines = self.client.engine_status()
            with self._lock:
                # deep-ish copy: the worker thread mutates the inner dicts
                uploading = {g: dict(i) for g, i in self.uploads.items()}
                recent = list(self.recent[:5])

            # Per-engine status line, e.g.
            #   "Motrix: connected · Transmission: not running"
            self.status_item.title = " · ".join(
                "%s: %s" % (label, "connected" if up else "not running")
                for label, up in engines)

            if not connected:
                self.title = ICON_IDLE
            elif uploading:
                # Live percent in the menu bar: "⬆ 42%"; several uploads
                # average their percents and show the count: "⬆2 40%".
                pcts = [u["pct"] for u in uploading.values()
                        if u.get("pct") is not None]
                count = "" if len(uploading) == 1 else str(len(uploading))
                if pcts:
                    self.title = "%s%s %d%%" % (
                        ICON_BUSY, count, sum(pcts) // len(pcts))
                else:
                    self.title = ICON_BUSY + count
            else:
                self.title = ICON_CONNECTED

            # Rebuild the dynamic middle of the menu. Keep the fixed top
            # (status + separator) and fixed bottom (pause/speed/log/quit).
            dynamic = []
            for a in active:
                dynamic.append(rumps.MenuItem(
                    "%s — %.0f%% (%s/s)" %
                    (a["name"], a["pct"], human_size(a["speed"]))))
            for gid, info in uploading.items():
                if info.get("pct") is not None:
                    detail = " — %d%%" % info["pct"]
                    if info.get("speed"):
                        detail += " (%s%s)" % (
                            info["speed"],
                            ", ETA %s" % info["eta"] if info.get("eta")
                            else "")
                else:
                    detail = " — starting…"
                dynamic.append(rumps.MenuItem(
                    "⬆ %s → %s%s" % (info["name"], info["drive"], detail)))
            if recent:
                dynamic.append(rumps.MenuItem("Recent:"))
                for line in recent:
                    dynamic.append(rumps.MenuItem("   %s" % line))

            # Reset menu: status, separator, dynamic, separator, controls.
            self.menu.clear()
            self.menu = [self.status_item, None] + dynamic + [
                None, self.pause_item] + [
                self._speed_menu(), self._reupload_menu(),
                self._storage_menu()] + [
                None,
                rumps.MenuItem("Open log", callback=self._open_log),
                rumps.MenuItem("Quit", callback=self._quit),
            ]

        def _speed_menu(self):
            m = rumps.MenuItem("Upload speed limit")
            for label, val in SPEED_LIMITS:
                it = rumps.MenuItem(label, callback=self._set_speed)
                it.state = 1 if self.state.bwlimit == val else 0
                m.add(it)
            return m

        def _reupload_menu(self):
            """Submenu of items that never landed on a drive; picking one
            re-arms it for upload (see reupload_candidates / requeue)."""
            m = rumps.MenuItem("Re-upload to Drive")
            cands = reupload_candidates(self.store.data)
            if not cands:
                # No callback -> the item is inert (shows disabled).
                m.add(rumps.MenuItem("Nothing kept local"))
                return m
            for gid, name in cands:
                it = rumps.MenuItem(name, callback=self._reupload_pick)
                it._gid = gid            # stash gid for the callback
                m.add(it)
            return m

        def _reupload_pick(self, sender):
            gid = getattr(sender, "_gid", None)
            if not gid:
                return
            rec = self.store.get(gid) or {}
            name = rec.get("name", gid)
            # Snapshot the cached usage under the lock, then RELEASE it before
            # opening the picker: _pick_drive blocks on an untimed osascript
            # 'choose from list', and holding self._lock across it would freeze
            # _render's 3s tick, _set_upload_progress, _usage_loop, and
            # _storage_menu (they all take this lock). Same shape as
            # _ask_destination. No exclusion here — the user may have freed
            # space; the fill-% labels give them the information instead.
            with self._lock:
                usage = self._usage
            choice = _pick_drive(name, list_drive_names, usage=usage)
            if not choice.startswith("drive:"):
                log("REQUEUE cancelled gid=%s" % gid)
                return
            self.poller.requeue_for_upload(gid, choice)
            self._notify("Queued for upload", name,
                         "-> %s" % choice[len("drive:"):])
            log("REQUEUE menu gid=%s %r -> %s" % (gid, name, choice))

        def _offer_repick(self, gid, name, failed_drive, quota):
            """Prompt-on-failure recovery: after an upload fails, offer to pick
            a different drive right away instead of waiting out the retries and
            hunting the menu. Runs on the upload worker thread (osascript on the
            worker is established practice — confirm_new_show already does it).

            Gated by recover_cfg["prompt_on_failure"] and capped at one dialog
            per gid per app run (_repick_prompted). For a quota/drive-full
            failure the failed drive is excluded from the re-pick; a generic
            failure keeps its backoff retries whatever the user does here.
            Reuses _pick_drive (fill-% labels) and requeue_for_upload, so the
            requeued dispatch still flows through poll_once's readiness gate,
            the _uploading guard, and success-only mark_handled."""
            if (not self.recover_cfg.get("prompt_on_failure", True)
                    or gid in self._repick_prompted):
                return
            self._repick_prompted.add(gid)
            rec = self.store.get(gid)
            if not rec or rec.get("handled"):
                # A concurrent requeue+success already resolved it.
                return
            # Step 1: a timeout-able confirm dialog GATES the picker, because
            # 'choose from list' has no AppleScript timeout — an unattended Mac
            # would otherwise wedge forever. Mirrors ask_destination's step 1.
            if quota:
                msg = 'Drive "%s" is full — "%s" failed to upload.' % (
                    failed_drive, name)
            else:
                msg = '"%s" failed to upload to "%s" (will keep retrying).' % (
                    name, failed_drive)
            script = (
                'display dialog "%s" '
                'buttons {"Not now", "Pick another drive…"} '
                'default button "Pick another drive…" '
                'with title "drive-offload" giving up after 60'
                % esc(msg))
            rc, out, _err = _osascript(script)
            if (rc != 0 or "gave up:true" in out
                    or "Not now" in out):
                # Quota items stay terminal-failed menu candidates; generic
                # items keep their backoff retries.
                log("REPICK declined gid=%s" % gid)
                return
            # LOCK DISCIPLINE: snapshot usage under the lock, then RELEASE it
            # before the picker — _pick_drive blocks on an untimed osascript
            # dialog, and holding self._lock across it would freeze _render,
            # _set_upload_progress, _usage_loop, and _storage_menu (all take
            # this lock). The two statements must NOT share a with-block.
            with self._lock:
                usage = self._usage
            choice = _pick_drive(
                name, list_drive_names, usage=usage,
                exclude=({failed_drive} if quota else None))
            if choice.startswith("drive:"):
                self.poller.requeue_for_upload(gid, choice)
                self._notify("Queued for upload", name,
                             "-> %s" % choice[len("drive:"):])
                log("REPICK requeue gid=%s %r -> %s" % (gid, name, choice))
            else:
                log("REPICK cancelled gid=%s" % gid)

        def _storage_menu(self):
            """FEATURE A: "Drive storage" submenu, built from the in-memory
            self._usage snapshot only -- zero I/O at render time, since the
            actual `todrive du` scan runs on the _usage_loop background
            thread. Cloned from _speed_menu's rebuild-not-mutate shape."""
            m = rumps.MenuItem("Drive storage")
            with self._lock:
                usage = self._usage
                errored = self._usage_error
                skey = self._sort_key
                sdesc = self._sort_desc
            if usage is None:
                label = "Unavailable — Refresh now" if errored else "Loading…"
                m.add(rumps.MenuItem(label, callback=self._refresh_usage_now))
                return m
            header = ("Updated %s — Refresh now" %
                      datetime.fromtimestamp(usage.get("ts", 0))
                      .strftime("%H:%M"))
            m.add(rumps.MenuItem(header, callback=self._refresh_usage_now))
            m.add(self._sort_menu(skey, sdesc))
            m.add(rumps.separator)
            rows = format_storage_table(usage.get("drives") or [],
                                        usage.get("cap_bytes"),
                                        sort_key=skey, descending=sdesc)
            if not rows:
                m.add(rumps.MenuItem("No drive content yet"))
            else:
                for line in rows:
                    m.add(_mono_item(line))  # no callback -> inert rows
            return m

        # Menu label -> format_storage_table sort_key.
        _SORT_FIELDS = (("Name", "name"), ("Content", "content"),
                        ("Trash", "trash"), ("Total", "total"), ("Free", "free"))

        def _sort_menu(self, skey, sdesc):
            """A "Sort" submenu: a checkmarked field picker plus a Descending
            toggle. Rebuilt every render tick, so the checkmarks always mirror
            the current self._sort_key / self._sort_desc."""
            arrow = "↓" if sdesc else "↑"
            label = dict(self._SORT_FIELDS).get(skey, skey).lower()
            sub = rumps.MenuItem("Sort: %s %s" % (label, arrow))
            for text, key in self._SORT_FIELDS:
                it = rumps.MenuItem(text, callback=self._set_sort_field)
                it.state = 1 if key == skey else 0
                sub.add(it)
            sub.add(rumps.separator)
            desc = rumps.MenuItem("Descending", callback=self._toggle_sort_dir)
            desc.state = 1 if sdesc else 0
            sub.add(desc)
            return sub

        def _set_sort_field(self, sender):
            # Persist + update the live attr; the menu repaints with the new
            # order on the next _on_tick (POLL_SECONDS=3s), by which point the
            # clicked menu has already closed anyway.
            key = dict(self._SORT_FIELDS).get(sender.title)
            if not key:
                return
            with self._lock:
                self._sort_key = key
            self.state.sort_key = key  # persists

        def _toggle_sort_dir(self, _sender):
            with self._lock:
                self._sort_desc = not self._sort_desc
                v = self._sort_desc
            self.state.sort_desc = v  # persists

        def _refresh_usage_now(self, _sender):
            self._usage_wake.set()

        # ---- upload orchestration ----
        def _start_upload(self, path, drive, gid, display_name):
            with self._lock:
                self.uploads[gid] = {"name": display_name, "drive": drive,
                                     "pct": None, "speed": "", "eta": ""}
            self._notify("Uploading", display_name, "→ %s" % drive)
            log("UPLOAD start gid=%s %r -> %s" % (gid, display_name, drive))
            t = threading.Thread(target=self._upload_worker,
                                 args=(path, drive, gid, display_name),
                                 daemon=True)
            t.start()

        def _set_upload_progress(self, gid, progress):
            with self._lock:
                info = self.uploads.get(gid)
                if info is not None:
                    info.update(progress)

        def _upload_worker(self, path, drive, gid, display_name):
            success = False
            quota = False
            try:
                size_before = tree_size(path)
                rc, output = perform_upload(
                    self.client, path, drive, gid, self.state.bwlimit,
                    progress_cb=lambda p: self._set_upload_progress(gid, p),
                    name=display_name, rename_cfg=self.rename_cfg,
                    cache=self.rename_cache, confirm_cb=confirm_new_show)
                for line in output.splitlines():
                    log("  todrive: %s" % line)
                if rc == 0:
                    success = True
                    freed = human_size(size_before)
                    # FEATURE B: todrive resolves overflow internally (this
                    # app never retries/reroutes), so a routed upload still
                    # lands here with rc == 0 -- the last "ROUTED: <src> ->
                    # <final>" line (see todrive cmd_up) is the only signal
                    # it didn't land on `drive` itself.
                    final_drive = drive
                    for line in output.splitlines():
                        if line.startswith("ROUTED: "):
                            final_drive = line.rsplit(" -> ", 1)[-1].strip()
                    # decisions.json must reflect where the payload ACTUALLY
                    # landed, not the original ask, so a later "already
                    # uploaded" check or a rename-continuity lookup finds it.
                    # Recorded unconditionally on success (even when
                    # final_drive == drive) so a concurrent requeue that
                    # rewrote choice mid-flight can't leave a stale value;
                    # set_choice touches only 'choice', so ordering vs
                    # upload_done's mark_handled cleanup is safe either way.
                    self.poller.store.set_choice(
                        gid, "drive:%s" % final_drive)
                    if final_drive != drive:
                        self._notify("Upload complete", display_name,
                                     "-> %s (overflow) — freed %s" %
                                     (final_drive, freed))
                        log("UPLOAD ok gid=%s %r (freed %s, routed %s -> %s)"
                            % (gid, display_name, freed, drive, final_drive))
                        self._push_recent("✅ %s -> %s (overflow)" %
                                          (display_name, final_drive))
                    else:
                        self._notify("Upload complete", display_name,
                                     "freed %s" % freed)
                        log("UPLOAD ok gid=%s %r (freed %s)" %
                            (gid, display_name, freed))
                        self._push_recent("✅ %s → %s" %
                                          (display_name, drive))
                else:
                    quota = is_quota_failure(output)
                    if quota:
                        self._notify("Drive full", display_name,
                                     "'%s' is out of space — pick another "
                                     "drive" % drive)
                        log("UPLOAD FAILED (quota) rc=%d gid=%s drive=%s %r" %
                            (rc, gid, drive, display_name))
                    else:
                        self._notify("Upload failed", display_name,
                                     "rc=%d — see app.log" % rc)
                        log("UPLOAD FAILED rc=%d gid=%s %r" %
                            (rc, gid, display_name))
                    self._push_recent("❌ %s (failed)" % display_name)
            except Exception as e:
                self._notify("Upload failed", display_name,
                             "%s — see app.log" % e)
                log("UPLOAD exception gid=%s: %s" % (gid, e))
                self._push_recent("❌ %s (error)" % display_name)
            finally:
                # Consume the decision only on a verified success; a failure
                # leaves it pending so the next poll retries instead of
                # orphaning it. A quota failure short-circuits to terminal
                # failed (mark_failed) rather than burning the backoff. Recording
                # the outcome BEFORE any prompt opens closes the
                # re-dispatch-during-dialog store-state race.
                self.poller.upload_done(gid, success, quota=quota)
                with self._lock:
                    self.uploads.pop(gid, None)
                # Storage may have changed (a successful upload, possibly
                # routed to overflow) or not (a failure) -- either way wake
                # the usage thread so "Drive storage" reflects reality
                # without waiting up to DRIVE_USAGE_REFRESH_SECONDS for its
                # next scheduled scan. Covers success, failure, AND the
                # exception path in one place.
                self._usage_wake.set()
                # On ANY failure, promptly offer a re-pick (once per gid per
                # run) so the user isn't left hunting the menu; the store
                # already reflects the failure by now. quota=True excludes the
                # full drive.
                if not success:
                    self._offer_repick(gid, display_name, drive, quota)

        def _push_recent(self, line):
            with self._lock:
                self.recent.insert(0, line)
                self.recent = self.recent[:5]

        # ---- menu callbacks ----
        def _toggle_pause(self, sender):
            self.state.paused = not self.state.paused
            sender.state = 1 if self.state.paused else 0
            self.pause_item.state = sender.state
            log("Pause asking -> %s" % self.state.paused)

        def _set_speed(self, sender):
            for label, val in SPEED_LIMITS:
                if label == sender.title:
                    self.state.bwlimit = val
                    log("Upload speed limit -> %s" % (val or "unlimited"))
            # states refreshed on next render

        def _open_log(self, _sender):
            if not os.path.exists(LOG_FILE):
                open(LOG_FILE, "a").close()
            subprocess.run(["open", LOG_FILE])

        def _quit(self, _sender):
            log("offload_app quitting")
            rumps.quit_application()

        def _notify(self, title, subtitle, message):
            try:
                rumps.notification(title, subtitle, message)
            except Exception as e:
                log("notify error: %s" % e)

    OffloadApp().run()


if __name__ == "__main__":
    _run_app()
