"""One-time ffprobe sweep of embedded subtitle tracks (Session B).

Measured motive: the sidecar cache covered under 3% of the library, which said
nothing about what the files themselves carry. A 10-file sample suggested 9
of 10 already had an embedded English track; a 30-file run put usable
English **text** tracks at 40%, image-only at 27% and nothing at 33% (see
docs/DECISIONS.md D-029 — the bigger sample governs, and it is the reason
the backfill queue is sized in hundreds of files rather than dozens). Either
way the point of this sweep is the same: turn a sample into a per-file fact
for the whole library, once, so every later decision (session C's player
short-circuit, session D's backfill worklist) works from real numbers
instead of a guess.

Runs the probe against the server's OWN loopback ``/stream/{file_id}``
endpoint — no auth needed there (``server._client_is_local`` and
``_remote_auth`` let a 127.0.0.1 request through before any token check) —
and records the result into a :class:`~drivecast.sub_index.SubIndex`, which
self-skips anything already probed at the same size.

Progress lives in its OWN status dict, separate from ``Scanner.status``.
CLAUDE.md already documents a ``total=0`` ambiguity on that dict (a
cache-only rebuild reports total=0, which callers must NOT read as "0 of 0
done"); merging a second, differently-shaped meter into the same dict would
recreate exactly that bug class for whichever caller reads it next.
"""
import asyncio
import json
import logging
import time

from . import config

log = logging.getLogger("drivecast.sub_sweep")

DEFAULT_CONCURRENCY = 6
# Concurrency used while the user is actually watching something. The sweep
# pulls its probe bytes through the very same /stream proxy playback uses, so
# six parallel probes contend with the stream for one uplink for the ~45 min
# a full first sweep takes. One probe at a time still finishes, just without
# stuttering the thing the user is looking at.
STREAMING_CONCURRENCY = 1
# How recently a /stream request must have arrived for the uplink to count as
# busy. Generous relative to a player's own read cadence: a paused or
# buffering player can go quiet for a while and is still "watching".
STREAM_HOT_SECONDS = 120.0
# Generous relative to the ~1.2 s/file measured through /stream — this bounds
# a single hung ffprobe, not the steady-state case.
PROBE_TIMEOUT = 20.0

# Subtitle codec -> track kind. Anything not listed here defaults to "text":
# subrip is overwhelmingly the common case in the sample, and an unrecognised
# codec_name (mov_text, ass, webvtt, ...) is far more likely to be a niche
# text format than an unseen image codec.
_IMAGE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "xsub"}


def _kind_for_codec(codec_name):
    return "image" if (codec_name or "") in _IMAGE_CODECS else "text"


def is_english(lang):
    """ffprobe reports ISO-639-2 ("eng"), but some releases tag "en" or
    "en-US" instead. Treat any of those as English; "und" or a missing tag
    is deliberately NOT English — a ``--slang=eng`` player flag and VLC's
    preferred-language device setting both match the DECLARED tag only, so
    an untagged track would look "covered" here while still losing the
    language match at actual playback. That mismatch is worse than treating
    it as uncovered, because nothing downstream would ever re-check it.
    """
    if not lang:
        return False
    base = lang.strip().lower().replace("_", "-").split("-")[0]
    return base in ("eng", "en")


def classify_probe_json(raw):
    """Parse ffprobe's ``-of json`` stdout into (tracks, has_text_en, has_image_en).

    Raises ValueError/TypeError on malformed input — the caller treats that
    as a failed probe (``ok: False``), never as "no subtitle streams".
    """
    data = json.loads(raw)
    tracks = []
    has_text_en = False
    has_image_en = False
    for s in data.get("streams") or []:
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        lang = tags.get("language")
        codec = s.get("codec_name")
        kind = _kind_for_codec(codec)
        track = {
            "index": s.get("index"),
            "codec": codec,
            "kind": kind,
            "lang": lang,
            "title": tags.get("title"),
            "forced": bool(disp.get("forced")),
            "hearing_impaired": bool(disp.get("hearing_impaired")),
        }
        tracks.append(track)
        if is_english(lang):
            if kind == "image":
                has_image_en = True
            else:
                has_text_en = True
    return tracks, has_text_en, has_image_en


class SubtitleSweeper:
    """Probes each (file_id, size) once and stores the result in a SubIndex."""

    def __init__(self, index, port=None, concurrency=DEFAULT_CONCURRENCY):
        self.index = index
        self.port = port or config.DEFAULTS.get("port", 8737)
        self.concurrency = concurrency
        self.sem = asyncio.Semaphore(concurrency)
        # Its own meter -- never merged into Scanner.status (see module doc).
        self.status = {"running": False, "probed": 0, "total": 0, "error": None}

    def set_concurrency(self, concurrency):
        """Replace the semaphore before a run starts.

        Only ever called with no probes in flight (`start_sweep` checks
        `running` first), which is what makes swapping the semaphore
        outright safe: a live one would have waiters parked on it. Used to
        drop to STREAMING_CONCURRENCY while a stream is hot.
        """
        concurrency = max(1, int(concurrency))
        if concurrency != self.concurrency:
            self.concurrency = concurrency
            self.sem = asyncio.Semaphore(concurrency)
        self.status["concurrency"] = concurrency

    def _url(self, file_id):
        return "http://127.0.0.1:%d/stream/%s" % (self.port, file_id)

    async def _probe_one(self, file_id):
        """Run the exact ffprobe command from the plan against one file.

        Every failure path (timeout, non-zero exit, unparsable output)
        returns a record with ``ok: False`` rather than raising — a sweep
        over thousands of files must not let one bad file abort the rest, and
        `run()`'s per-item wrapper still stores whatever this returns so a
        dead file is at least recorded as "checked, no answer" and does not
        get re-probed every sweep."""
        cmd = [
            "ffprobe", "-v", "error", "-probesize", "200k",
            "-analyzeduration", "200k", "-select_streams", "s",
            "-show_entries",
            "stream=index,codec_name,codec_type:stream_tags=language,title",
            "-show_entries", "stream_disposition=forced,hearing_impaired",
            "-of", "json", self._url(file_id),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                     timeout=PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {"ok": False, "tracks": [], "has_text_en": False,
                    "has_image_en": False, "error": "timeout"}
        if proc.returncode != 0:
            reason = (stderr or b"").decode("utf-8", "replace").strip()[:500]
            return {"ok": False, "tracks": [], "has_text_en": False,
                    "has_image_en": False, "error": reason or "ffprobe_error"}
        try:
            tracks, has_text_en, has_image_en = classify_probe_json(
                stdout.decode("utf-8", "replace"))
        except (ValueError, TypeError) as e:
            return {"ok": False, "tracks": [], "has_text_en": False,
                    "has_image_en": False, "error": "bad_json: %s" % e}
        return {"ok": True, "tracks": tracks, "has_text_en": has_text_en,
                "has_image_en": has_image_en, "error": None}

    async def _probe_and_store(self, file_id, size):
        try:
            async with self.sem:
                result = await self._probe_one(file_id)
        except Exception as e:  # pragma: no cover - defensive, mirrors resolve()
            log.exception("Subtitle probe crashed for %s", file_id)
            result = {"ok": False, "tracks": [], "has_text_en": False,
                      "has_image_en": False, "error": str(e)}
        record = dict(result)
        record["size"] = size
        record["probed_at"] = time.time()
        self.index.put(file_id, record)
        self.status["probed"] += 1

    async def run(self, file_ids_with_size):
        """Probe every ``(file_id, size)`` not already indexed at that size.

        Resumable across restarts: an id already probed at the same size
        costs zero subprocess calls, so re-running after a kill (or simply
        re-running the sweep later as the library grows) only pays for what
        is actually new or changed.
        """
        pending = [(fid, size) for fid, size in file_ids_with_size
                  if self.index.needs_probe(fid, size)]
        self.status.update(running=True, probed=0, total=len(pending), error=None)
        try:
            await asyncio.gather(*(self._probe_and_store(fid, size)
                                   for fid, size in pending))
        except Exception as e:  # pragma: no cover - defensive
            self.status["error"] = str(e)
            log.exception("Subtitle sweep failed")
        finally:
            # Unconditional flush: guarantees the tail since the last
            # periodic flush is never lost on a clean finish.
            self.index.flush()
            self.status["running"] = False
        return self.status
