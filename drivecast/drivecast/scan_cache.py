"""Sidecar cache of raw, pre-grouping scan records, keyed by drive id.

This is the keystone of per-drive refresh: a partial refresh re-walks Drive
for the scoped drives only, then the library is rebuilt from the cached raw
records of ALL selected drives — so cross-drive grouping (grp: shows spanning
"Part 1"/"Part 2" drives) stays correct without re-scanning everything.

Records are stored pre-group_seasons with their transient keys
(_folder_name/_video_name/_thumb) intact, because grouping and poster
resolution need them. get() returns deep copies — group_seasons mutates its
input, and the cache must stay pristine for the next rebuild.
"""
import json
import os
import tempfile
import time

from . import config

SCAN_CACHE_PATH = os.path.join(config.DATA_DIR, "scan_cache.json")
CACHE_VERSION = 1


class ScanCache:
    def __init__(self, path=SCAN_CACHE_PATH):
        self.path = path
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("drives"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": CACHE_VERSION, "drives": {}}

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path),
                                   prefix=".scan_cache-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def has(self, drive_id):
        return drive_id in self.data["drives"]

    def get(self, drive_id):
        """Deep copies of a drive's cached records ([] if never scanned)."""
        entry = self.data["drives"].get(drive_id)
        if not entry:
            return []
        return json.loads(json.dumps(entry.get("records") or []))

    def put(self, drive_id, records):
        """Store a drive's raw records (deep-copied) and persist."""
        self.data["drives"][drive_id] = {
            "scanned_at": time.time(),
            "records": json.loads(json.dumps(records)),
        }
        self._save()

    def prune(self, keep_ids):
        """Drop cache entries for drives no longer selected."""
        keep = set(keep_ids)
        stale = [d for d in self.data["drives"] if d not in keep]
        for d in stale:
            del self.data["drives"][d]
        if stale:
            self._save()

    def drive_ids(self):
        return list(self.data["drives"])
