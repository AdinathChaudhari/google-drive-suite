"""Watch history / resume positions, persisted to data/history.json.

Keyed by Drive file id. Writes are atomic (tempfile + os.replace) and the
poller-driven saves are debounced so we don't rewrite the file every few
seconds.
"""
import json
import os
import tempfile
import threading
import time

from . import config

HISTORY_PATH = os.path.join(config.DATA_DIR, "history.json")

WATCHED_PERCENT = 90.0        # >= this fraction watched -> mark watched
DEBOUNCE_SECONDS = 10.0       # min gap between poller-driven disk writes
CONTINUE_MIN = 2.0            # continue-watching lower bound (percent)
CONTINUE_MAX = 92.0           # continue-watching upper bound (percent)
CONTINUE_LIMIT = 20


class History:
    def __init__(self, path=HISTORY_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._load()
        self._last_write = 0.0

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _write_atomic(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".history-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self.path)
            self._last_write = time.time()
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, file_id):
        with self._lock:
            entry = self._data.get(file_id)
            return dict(entry) if entry else None

    def resume_position(self, file_id):
        """Return saved position (seconds) if it's worth resuming, else 0."""
        entry = self.get(file_id)
        if not entry:
            return 0.0
        if entry.get("watched"):
            return 0.0
        pos = float(entry.get("position") or 0.0)
        pct = float(entry.get("percent") or 0.0)
        # Don't resume trivially-short or near-complete plays.
        if pct <= CONTINUE_MIN or pct >= CONTINUE_MAX:
            return 0.0
        return pos

    def update(self, file_id, name=None, drive_id=None, parent_id=None,
               position=None, duration=None, force=False):
        """Update an entry. Poller calls are debounced unless force=True.

        Returns True if the update was persisted to disk.
        """
        with self._lock:
            entry = self._data.get(file_id, {})
            if name is not None:
                entry["name"] = name
            if drive_id is not None:
                entry["drive_id"] = drive_id
            if parent_id is not None:
                entry["parent_id"] = parent_id
            if position is not None:
                entry["position"] = float(position)
            if duration is not None and duration:
                entry["duration"] = float(duration)
            dur = float(entry.get("duration") or 0.0)
            pos = float(entry.get("position") or 0.0)
            pct = (pos / dur * 100.0) if dur > 0 else 0.0
            entry["percent"] = round(pct, 2)
            if pct >= WATCHED_PERCENT:
                entry["watched"] = True
            else:
                entry.setdefault("watched", False)
            entry["last_played"] = time.time()
            self._data[file_id] = entry

            if force or (time.time() - self._last_write) >= DEBOUNCE_SECONDS:
                self._write_atomic()
                return True
            return False

    def remove(self, file_id):
        """Drop an entry entirely (Continue Watching dismiss).

        Returns True if the entry existed. Persists immediately when it did —
        the resume position is intentionally discarded.
        """
        with self._lock:
            existed = file_id in self._data
            if existed:
                self._data.pop(file_id, None)
                self._write_atomic()
            return existed

    def mark_watched(self, file_id, watched=True):
        with self._lock:
            entry = self._data.get(file_id)
            if entry:
                entry["watched"] = watched
                self._write_atomic()

    def flush(self):
        with self._lock:
            self._write_atomic()

    def last_played_map(self):
        """Return {file_id: last_played_epoch} for every tracked entry.

        Lets the UI sort titles by "Recently watched" (including finished ones,
        which the Continue Watching shelf deliberately omits).
        """
        with self._lock:
            return {fid: float(e.get("last_played") or 0.0)
                    for fid, e in self._data.items()}

    def progress_map(self):
        """{file_id: {"percent": p, "watched": bool}} for every tracked entry.

        Powers per-lesson checkmarks and course progress rings in the UI.
        """
        with self._lock:
            return {fid: {"percent": float(e.get("percent") or 0.0),
                          "watched": bool(e.get("watched"))}
                    for fid, e in self._data.items()}

    def continue_watching(self):
        """Return partially-watched items for the Continue Watching shelf."""
        with self._lock:
            items = []
            for fid, e in self._data.items():
                pct = float(e.get("percent") or 0.0)
                if e.get("watched"):
                    continue
                if CONTINUE_MIN < pct < CONTINUE_MAX:
                    item = dict(e)
                    item["file_id"] = fid
                    items.append(item)
            items.sort(key=lambda x: x.get("last_played", 0), reverse=True)
            return items[:CONTINUE_LIMIT]
