"""Per-file embedded-subtitle truth table (Session B of the subtitles plan).

Standalone file (``data/sub_index.json``) — deliberately NOT folded into
``scan_cache.json``. ``ScanCache.put`` (scan_cache.py) replaces a drive's
*entire* record list on every walk, so a probe result stored there would be
re-paid on every single scan instead of surviving forever. This store is
keyed on the immutable ``file_id`` used everywhere else in the app, with the
file's ``size`` alongside: a changed size (re-encode, different rip dropped
into the same slot) means the old probe no longer describes the bytes on
disk, so it is re-probed exactly once and replaced — never trusted forever,
never re-probed on every scan either.

No ``CACHE_VERSION``-style bump lives here on purpose: this file has no
upgrade seam to protect yet, and adding one before there is a shape to
migrate away from would be speculative.
"""
import json
import os
import tempfile

from . import config

SUB_INDEX_FILENAME = "sub_index.json"
SUB_INDEX_VERSION = 1
# Roughly how many probes accumulate between atomic flushes to disk. A sweep
# killed between flushes only re-does work since the last one — never the
# whole run — which is what makes the sweep safely resumable across restarts.
FLUSH_EVERY = 25


class SubIndex:
    """Load/lookup/store for the embedded-subtitle probe results.

    The default path is resolved LAZILY (a ``path`` property, not a
    module-level constant baked in at import time) so a test that redirects
    ``config.DATA_DIR`` is honoured — the same trap ``subtitles.NegativeCache``
    already had to route around.
    """

    def __init__(self, path=None, flush_every=FLUSH_EVERY):
        self._path = path
        self.flush_every = flush_every
        self.data = self._load()
        self._dirty = 0

    @property
    def path(self):
        return self._path or os.path.join(config.DATA_DIR, SUB_INDEX_FILENAME)

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("probed"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": SUB_INDEX_VERSION, "probed": {}}

    def flush(self):
        """Atomic write (tempfile + os.replace), the same shape scan_cache.py
        and config.save_config already use. Always resets the dirty counter,
        even called with nothing pending, so a caller can flush
        unconditionally at the end of a run."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path),
                                   prefix=".sub_index-", suffix=".tmp")
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
        self._dirty = 0

    def get(self, file_id):
        """The stored probe record for a file id, or None if never probed."""
        return self.data["probed"].get(file_id)

    def probed_count(self):
        """How many files this index has a probe result for.

        Zero means the sweep has never run, which callers must NOT read as
        "nothing is covered": before the first sweep every file looks
        uncovered, and a backfill worklist built from that would be the whole
        library (2,225 entries) plus a Settings panel confidently reporting
        0% coverage. See server.py's `_refresh_backfill_worklist`.
        """
        return len(self.data["probed"])

    def needs_probe(self, file_id, size):
        """True if this file has never been probed, or was probed at a
        different size (the file's bytes changed since the last sweep)."""
        entry = self.data["probed"].get(file_id)
        if entry is None:
            return True
        return entry.get("size") != size

    def put(self, file_id, record):
        """Store (or replace) a probe result, flushing every ~flush_every
        puts so a killed sweep loses only the tail since the last flush."""
        self.data["probed"][file_id] = record
        self._dirty += 1
        if self._dirty >= self.flush_every:
            self.flush()
