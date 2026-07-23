#!/usr/bin/env python3
"""drive-offload: watch dirs for completed downloads and move them to
rclone remotes (Google Shared Drives), then delete locally.

Stdlib only. See README.md for setup.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "watch_dirs": ["~/Downloads/Offload"],
    "base_remote": "gdrive:",
    "remotes": ["gdrive1:"],
    "remote_subdir": "Offload",
    "poll_seconds": 10,
    "stable_seconds": 20,
    "delete_after_upload": True,
    "exclude_names": [".DS_Store"],
    "exclude_extensions": [".aria2", ".part", ".crdownload", ".tmp", ".download"],
    "rclone_flags": ["--drive-chunk-size", "64M", "--transfers", "4",
                     "--retries", "3", "--low-level-retries", "10"],
    "log_file": "offload.log",
}

# rclone stderr substrings that mean "this remote is full / rate limited" ->
# fail over to the next remote.
QUOTA_MARKERS = ("storageQuotaExceeded", "quota", "insufficient",
                 "userRateLimitExceeded")

# entries that fail with a non-quota error are parked for this long
FAILURE_BACKOFF_SECONDS = 300

RCLONE = "rclone"

# Directories a watch dir may not resolve to (this daemon DELETES what it uploads).
FORBIDDEN_ROOTS = ("/", "/Users", "/Volumes")

_shutdown = False
_current_proc = None
_log_fh = None


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    if _log_fh is not None:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def human_size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def write_default_config(path):
    with open(path, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
        f.write("\n")


def load_state(path):
    """Load persisted stability/backoff state. Returns (state, backoff).
    Persisting lets repeated --once passes accumulate stability observations
    (the daemon loop keeps them in memory anyway)."""
    state, backoff = {}, {}
    try:
        with open(path) as f:
            data = json.load(f)
        state = {k: tuple(v) for k, v in data.get("state", {}).items()}
        backoff = data.get("backoff", {})
    except (OSError, ValueError):
        pass
    return state, backoff


def save_state(path, state, backoff):
    try:
        with open(path, "w") as f:
            json.dump({"state": {k: list(v) for k, v in state.items()},
                       "backoff": backoff}, f)
    except OSError:
        pass


def expand(p):
    return os.path.abspath(os.path.expanduser(p))


def is_forbidden(path):
    """True if path is a dir we must refuse to watch/delete from."""
    resolved = os.path.realpath(expand(path))
    home = os.path.realpath(os.path.expanduser("~"))
    downloads = os.path.join(home, "Downloads")
    if resolved == home:
        return True
    if resolved in (os.path.realpath(r) for r in FORBIDDEN_ROOTS):
        return True
    if resolved == os.path.realpath(downloads):
        return True
    return False


def is_excluded(name, cfg):
    if name in cfg["exclude_names"]:
        return True
    lower = name.lower()
    for ext in cfg["exclude_extensions"]:
        if lower.endswith(ext.lower()):
            return True
    return False


def entry_size(path):
    """Total size in bytes (recursive for directories)."""
    if os.path.islink(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(path):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def dir_has_incomplete(path, cfg):
    """True if any file inside the dir tree is a control/excluded file."""
    for root, dirs, files in os.walk(path):
        for fn in files:
            if fn.lower().endswith(".aria2"):
                return True
            lower = fn.lower()
            for ext in cfg["exclude_extensions"]:
                if lower.endswith(ext.lower()):
                    return True
    return False


def is_complete(path, cfg):
    """Return True if the entry is done downloading (control-file checks only;
    stability handled separately)."""
    name = os.path.basename(path.rstrip(os.sep))
    if is_excluded(name, cfg):
        return False
    # sibling <entry>.aria2 control file
    if os.path.exists(path + ".aria2"):
        return False
    if os.path.isdir(path) and dir_has_incomplete(path, cfg):
        return False
    return True


def build_rclone_cmd(src, remote, cfg, dry_run):
    """Construct the rclone move/copy command list."""
    subdir = cfg["remote_subdir"].strip("/")
    is_dir = os.path.isdir(src)
    action = "move" if cfg["delete_after_upload"] and not dry_run else "copy"
    # Note: --dry-run forces copy semantics (never delete). rclone still
    # honors --dry-run below, so nothing is actually transferred/removed.
    if dry_run:
        action = "copy"

    # For a directory, place its contents under remote:subdir/<dirname>/ so the
    # directory is preserved as a unit. For a file, target the subdir itself.
    if is_dir:
        dirname = os.path.basename(src.rstrip(os.sep))
        dest = "%s%s/%s/" % (remote, subdir, dirname) if subdir else "%s%s/" % (remote, dirname)
    else:
        dest = "%s%s/" % (remote, subdir) if subdir else remote

    cmd = [RCLONE, action, src, dest]
    cmd += list(cfg["rclone_flags"])
    cmd += ["--stats-one-line", "--stats", "30s"]
    if dry_run:
        cmd.append("--dry-run")
    return cmd, dest


def run_upload(src, cfg, dry_run):
    """Try each remote in order. Returns (True, dest) on success.
    Returns (False, None) on failure (caller applies backoff)."""
    global _current_proc
    size = entry_size(src)
    for remote in cfg["remotes"]:
        cmd, dest = build_rclone_cmd(src, remote, cfg, dry_run)
        log("UPLOAD start: %s -> %s (%s)%s" %
            (src, dest, human_size(size), " [dry-run]" if dry_run else ""))
        start = time.time()
        try:
            _current_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            out, err = _current_proc.communicate()
            rc = _current_proc.returncode
        except FileNotFoundError:
            log("ERROR: rclone not found on PATH; cannot upload")
            _current_proc = None
            return False, None
        finally:
            proc = _current_proc
            _current_proc = None

        if _shutdown:
            log("UPLOAD interrupted by shutdown: %s" % src)
            return False, None

        dur = time.time() - start
        if rc == 0:
            log("UPLOAD finish: %s -> %s in %.1fs (%s)" %
                (src, dest, dur, human_size(size)))
            return True, dest

        err_tail = (err or "").strip().splitlines()[-10:]
        err_text = err or ""
        if any(m.lower() in err_text.lower() for m in QUOTA_MARKERS):
            log("REMOTE full/limited (%s), failing over. stderr tail:\n%s" %
                (remote, "\n".join(err_tail)))
            continue
        log("UPLOAD failed (rc=%d) for %s on %s; will retry later. stderr tail:\n%s" %
            (rc, src, remote, "\n".join(err_tail)))
        return False, None

    log("UPLOAD failed: all remotes exhausted for %s" % src)
    return False, None


def scan_once(cfg, watch_dirs, state, backoff, dry_run):
    """One scan pass. Mutates state/backoff. Uploads eligible entries."""
    now = time.time()
    seen = set()
    for wd in watch_dirs:
        if not os.path.isdir(wd):
            continue
        try:
            names = sorted(os.listdir(wd))
        except OSError as e:
            log("ERROR listing %s: %s" % (wd, e))
            continue
        for name in names:
            path = os.path.join(wd, name)
            seen.add(path)

            # backoff for recently-failed entries
            if path in backoff and now < backoff[path]:
                continue

            if is_excluded(name, cfg):
                continue
            if not is_complete(path, cfg):
                # not done yet; drop any stale stability record
                state.pop(path, None)
                continue

            size = entry_size(path)
            if size == 0:
                state.pop(path, None)
                continue

            prev = state.get(path)
            if prev is None or prev[0] != size:
                # size changed (or new) -> reset the stability clock
                state[path] = (size, now)
                continue

            first_stable = prev[1]
            if now - first_stable < cfg["stable_seconds"]:
                continue  # not stable long enough yet

            # eligible -> upload
            ok, _dest = run_upload(path, cfg, dry_run)
            state.pop(path, None)
            if ok:
                backoff.pop(path, None)
            else:
                backoff[path] = now + FAILURE_BACKOFF_SECONDS
            if _shutdown:
                return

    # forget state/backoff for entries that no longer exist
    for gone in [p for p in state if p not in seen]:
        state.pop(gone, None)
    for gone in [p for p in backoff if p not in seen]:
        backoff.pop(gone, None)


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log("Received signal %d, shutting down..." % signum)
    if _current_proc is not None:
        try:
            _current_proc.terminate()
        except Exception:
            pass


def main():
    global _log_fh
    parser = argparse.ArgumentParser(description="drive-offload watcher daemon")
    parser.add_argument("--once", action="store_true",
                        help="single scan pass then exit (testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="pass --dry-run to rclone, never delete")
    parser.add_argument("--config", default=None,
                        help="path to config.json")
    args = parser.parse_args()

    config_path = args.config or os.path.join(SCRIPT_DIR, "config.json")

    if not os.path.exists(config_path):
        write_default_config(config_path)
        print("Wrote default config to %s. Edit it (set your rclone remotes "
              "and watch dir) then re-run." % config_path)
        sys.exit(1)

    cfg = load_config(config_path)

    # resolve log file relative to script dir
    log_file = cfg["log_file"]
    if not os.path.isabs(log_file):
        log_file = os.path.join(SCRIPT_DIR, log_file)
    _log_fh = open(log_file, "a")

    watch_dirs = [expand(p) for p in cfg["watch_dirs"]]

    # safety guards
    for wd in watch_dirs:
        if is_forbidden(wd):
            log("FATAL: refusing to watch %r. This daemon DELETES what it "
                "uploads; point it at a dedicated subfolder like "
                "~/Downloads/Offload, not your home, /, /Users, /Volumes, or "
                "~/Downloads itself." % wd)
            sys.exit(2)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log("drive-offload starting")
    log("  config: %s" % config_path)
    log("  watch_dirs: %s" % watch_dirs)
    log("  remotes: %s" % cfg["remotes"])
    log("  remote_subdir: %s" % cfg["remote_subdir"])
    log("  poll_seconds=%s stable_seconds=%s delete_after_upload=%s" %
        (cfg["poll_seconds"], cfg["stable_seconds"], cfg["delete_after_upload"]))
    if args.dry_run:
        log("  DRY-RUN mode: nothing will be transferred or deleted")

    # persist stability state next to the log file so repeated --once passes
    # (and daemon restarts) don't lose their stability clock.
    state_file = os.path.join(os.path.dirname(log_file), ".offload-state.json")
    state, backoff = load_state(state_file)  # path -> (size, first_seen_stable_ts)

    if args.once:
        scan_once(cfg, watch_dirs, state, backoff, args.dry_run)
        save_state(state_file, state, backoff)
        log("--once pass complete, exiting")
        return

    while not _shutdown:
        scan_once(cfg, watch_dirs, state, backoff, args.dry_run)
        save_state(state_file, state, backoff)
        if _shutdown:
            break
        # sleep in small increments so signals are responsive
        slept = 0.0
        while slept < cfg["poll_seconds"] and not _shutdown:
            time.sleep(0.5)
            slept += 0.5

    log("drive-offload stopped")


if __name__ == "__main__":
    main()
