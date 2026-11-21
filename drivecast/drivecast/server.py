"""FastAPI application: API routes, streaming proxy, static UI."""
import asyncio
import hmac
import io
import ipaddress
import logging
import os
import secrets
import socket
import subprocess
import time
import urllib.parse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config as config_mod
from . import localcert
from . import media_info
from . import naming
from .awake import KeepAwake
from .drive_api import DriveAPI, DriveAPIError
from .history import History
from .library import Library, Scanner
from .player import PlayerError, PlayerManager, detect_player
from .rclone_auth import RcloneError, TokenManager
from .scan_cache import ScanCache
from . import __version__ as APP_VERSION
from .subtitles import SUB_EXTS, SubtitleResolver, mime_for
from .sub_index import SubIndex
from . import sub_sweep as sub_sweep_mod
from .sub_sweep import SubtitleSweeper, is_english
from .subtitle_queue import SubtitleQueue, classify_priorities
from .streaming import Streamer
from .tmdb import TMDB

log = logging.getLogger("drivecast.server")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _has_embedded_english_text_track(record):
    """True if a sub_index probe `record` has an English TEXT track that is
    usable as "subtitles are already on" without any further fetch.

    Session C's whole short-circuit hinges on this being narrower than the
    index's own `has_text_en` flag: that flag is True for ANY English text
    track, forced or hearing-impaired included. A forced track is
    near-empty (only a few foreign-language lines) and a hearing-impaired
    (SDH) track is bracketed sound-effect text -- handing either over as
    the answer to "turn subtitles on" reads as a regression, not a feature,
    so both are excluded here and the caller falls through to resolve() as
    if there were no embedded track at all. `und`/missing language tags are
    likewise never covered (`is_english` already encodes that), because
    --slang and VLC's language setting both match the DECLARED tag only --
    an untagged match here would be a false positive nothing downstream
    ever rechecks. `record` is None (never probed) or `ok: False` (probe
    failed) both mean "unknown", so both fall through, same as uncovered.
    """
    if not record or not record.get("ok"):
        return False
    for track in record.get("tracks") or []:
        if (track.get("kind") == "text"
                and is_english(track.get("lang"))
                and not track.get("forced")
                and not track.get("hearing_impaired")):
            return True
    return False


def _has_embedded_english_image_track(record):
    """True if the only English subtitles in `record` are IMAGE tracks
    (PGS/dvd_subtitle) on a non-forced, non-SDH stream.

    Split from the text predicate on purpose. mpv, IINA and VLC are all
    documented to render an image track selected by `--slang`/
    `--sub-language`, which would make these files covered for free -- but
    that is unverified on this hardware (D-031), and Infuse's `sub=` is a
    text-sidecar-only consumer, so it can never be true there. A wrong
    "covered" here is unrecoverable in practice: nothing re-checks it, and
    the file would be excluded from the backfill worklist forever. So the
    caller gates this behind `prefer_embedded_image_subtitles`, default
    False, and the config key is the whole point -- once §6.5 passes per
    player, turning it on is a settings edit, not a code change.
    """
    if not record or not record.get("ok"):
        return False
    for track in record.get("tracks") or []:
        if (track.get("kind") == "image"
                and is_english(track.get("lang"))
                and not track.get("forced")
                and not track.get("hearing_impaired")):
            return True
    return False


class AppState:
    """Container for all long-lived services."""

    def __init__(self, cfg):
        self.cfg = cfg
        # sections.py's tabs cache otherwise self-loads from
        # config.load_config() on first touch (mirroring its plugins() lazy
        # pattern) -- but this AppState's `cfg` is the actual source of
        # truth (tests build one from scratch without ever touching disk;
        # in prod it's the same dict load_config() just returned anyway).
        # Sync the cache up front so /api/sections and /api/settings always
        # reflect *this* cfg, never a stale or unrelated on-disk one.
        from . import sections as sections_mod
        sections_mod.set_tabs(cfg.get("tabs"))
        self.tokens = TokenManager(cfg["remote"])
        self.api = DriveAPI(self.tokens, self.tokens.list_drives)
        # Hold a power assertion while streams are active (clamshell sleep would
        # otherwise kill remote playback). Reads the live config toggle.
        self.awake = KeepAwake(enabled=lambda: bool(self.cfg.get("keep_awake", True)))
        self.streamer = Streamer(self.tokens, self.api, keepawake=self.awake)
        self.history = History()
        self.tmdb = TMDB(cfg.get("tmdb_api_key"))
        self.library = Library(drive_sections=cfg.get("drive_sections") or {})
        self.library.seed_api_cache(self.api)
        self.scanner = Scanner(self.api, self.tmdb, self.library,
                               throttle=cfg.get("scan_throttle", 0.15),
                               cache=ScanCache())
        port = cfg.get("port", 8737)
        self.player = PlayerManager(cfg, self.history, "http://127.0.0.1:%d" % port)
        self.subtitles = SubtitleResolver(self.api, cfg.get("opensubtitles_api_key"))
        # Embedded-subtitle index + the sweep that fills it (session B of the
        # subtitles plan). Separate store, separate progress meter -- see
        # sub_index.py / sub_sweep.py module docstrings for why. Created
        # BEFORE sub_decision_lookup is wired below, since that closure
        # reads self.sub_index.
        self.sub_index = SubIndex()
        self.sub_sweep = SubtitleSweeper(self.sub_index, port=cfg.get("port", 8737))
        # Session D: the OpenSubtitles backfill queue for the (small) measured
        # remainder the sweep can't already answer from an embedded track.
        # daily_limit is a config key (config.py's "subtitle_daily_limit")
        # specifically so raising it later -- e.g. after logging into a
        # 20/day account -- is a one-line change, not a code change.
        self.subtitle_queue = SubtitleQueue(
            daily_limit=cfg.get("subtitle_daily_limit", 5))
        self._backfill_task = None
        self._backfill_timer_task = None
        # Autoplay-advanced episodes get the same embedded-vs-sidecar
        # decision /api/play makes (session C) -- NOT just a cached-sidecar
        # lookup. sub_cache_lookup is left wired too, as the fallback
        # PlayerManager._advance uses if a future caller never sets
        # sub_decision_lookup.
        self.player.sub_cache_lookup = self.subtitles.cached
        self.player.sub_decision_lookup = self._sub_decision_lookup
        self._sweep_task = None
        self.setup_error = None  # populated by preflight if rclone is unusable
        self._refresh_task = None
        self._pending_scope = set()  # scopes requested while a scan was running
        self._pending_rebuild = False  # a rebuild/scope arrived mid-scan; re-kick on finish
        self._inflight_resolves = {}  # (file_id, force, allow_os) -> resolve() task
        self._detached_resolves = set()  # resolve() tasks outliving their wait bound
        self.stream_activity = {}  # file_id -> epoch of last GET /stream request
        # Cached ffprobe facts + the VLC hardware-decode policy (/api/media-info).
        self.media_info = media_info.MediaInfoCache(
            port=int(port),
            size_for=lambda fid: (self.library.file_info(fid) or {}).get("size"))

    def start_refresh(self, scope=None, _draining=False):
        """Kick a background library scan, if idle.

        `scope=None` is a full refresh over all selected drives (returns
        False when none are selected). `scope=[]` is a cache-only rebuild —
        no Drive walk, but deselected drives' titles still leave the library.
        A non-empty `scope` that filters down to nothing (every named drive
        is unselected) starts nothing and returns False — same as an empty
        `scope=None` refresh — rather than silently downgrading into that
        cache-only rebuild.
        A request that arrives while a scan is running is queued and
        re-kicked when the running scan finishes (a section change or
        removal saved mid-scan must not be silently dropped). `_draining` is
        set only by that re-kick (`_drain_pending_scope`): the drives it
        named may have been deselected entirely while the scan ran, and that
        removal still needs the library rebuilt from cache — so on drain a
        non-empty scope that filters to nothing degrades to the cache-only
        rebuild instead of the "start nothing" early return below.
        """
        drives = self.cfg.get("selected_drives") or []
        if scope is None:
            if not drives:
                return False
            scope = list(drives)
        else:
            requested = list(scope)
            # Explicit scope: filter to selected. An (explicitly) empty result
            # still runs — a cache-only rebuild that prunes deselected drives.
            # But a NON-EMPTY request that filters down to nothing (every
            # named drive turned out to be unselected) must not be silently
            # treated as that same "walk nothing" rebuild request — unless
            # we're draining a queued scope, where "nothing left selected"
            # is exactly the removal the queued rebuild exists to apply.
            scope = [d for d in requested if d in drives]
            if requested and not scope:
                if not _draining:
                    return False
                scope = []
        if self.scanner.status.get("running"):
            self._pending_scope.update(scope)
            self._pending_rebuild = True
            return False
        if self._pending_scope or self._pending_rebuild:
            scope = sorted(set(scope) | self._pending_scope)
            self._pending_scope.clear()
            self._pending_rebuild = False
        self._refresh_task = asyncio.create_task(self.scanner.scan(
            drives, scope=scope,
            drive_hints=self.cfg.get("drive_hints") or {},
            drive_sections=self.cfg.get("drive_sections") or {}))
        self._refresh_task.add_done_callback(self._drain_pending_scope)
        # Scope guard (hard requirement, see sub_sweep.py's module doc): `scope`
        # here is the FINAL list this call is about to hand to scanner.scan,
        # after all the collapsing/merging above. `scope == []` is a
        # cache-only rebuild that walks no Drive bytes and can introduce no
        # new file_id -- the sweep must never fire for it, because the sweep
        # itself pulls bytes through /stream, which IS live Drive egress.
        # A non-empty scope (whether the full `None`-expanded list, a
        # per-drive partial, or a drained pending scope) did walk Drive, so
        # it may have introduced ids the index has never seen.
        if scope:
            self._refresh_task.add_done_callback(self._after_scan_maybe_sweep)
        return True

    def _after_scan_maybe_sweep(self, task):
        """Fire the subtitle sweep once a WALKING scan finishes.

        Only ever attached (in start_refresh above) when that scan's scope
        was non-empty -- never after a scope=[] cache-only rebuild.
        """
        if task.cancelled():
            return
        if task.exception() is not None:
            return
        # Subtitles switched off: no probe sweep (live Drive egress) either.
        if not self.cfg.get("subtitles", True):
            return
        self.start_sweep()

    def _video_files_for_sweep(self):
        """(file_id, size) for every movie and episode in the CURRENT library.

        Extras and audiobooks are left out on purpose: movies + episodes is
        the exact file set this plan measures against,
        and extras/audiobooks were never part of that count.
        """
        out = []
        for rec in self.library.titles_list():
            if rec.get("type") == "movie" and rec.get("file_id"):
                out.append((rec["file_id"], rec.get("size") or 0))
            elif rec.get("type") == "show":
                for season in rec.get("seasons") or []:
                    for ep in season.get("episodes") or []:
                        if ep.get("file_id"):
                            out.append((ep["file_id"], ep.get("size") or 0))
        return out

    def start_sweep(self, file_ids_with_size=None):
        """Fire-and-forget the embedded-subtitle sweep, unless one is already
        running. `running` is set synchronously (before `create_task`, not
        inside the coroutine) so two calls made back-to-back -- the
        post-scan hook and a manual POST landing in the same event-loop
        tick, say -- can't both pass the check before either's task gets its
        first chance to run; asyncio never interleaves between here and the
        `create_task` call below, so this check-then-set is race-free
        without a lock."""
        if self.sub_sweep.status.get("running"):
            return False
        self.sub_sweep.status["running"] = True
        try:
            return self._start_sweep_task(file_ids_with_size)
        except BaseException:
            self.sub_sweep.status["running"] = False
            raise

    def _start_sweep_task(self, file_ids_with_size):
        # The sweep's probe bytes come through the SAME /stream proxy the
        # player is reading from, so six parallel probes and a live stream
        # fight over one uplink for the ~45 min a first full sweep takes.
        # Drop to serial while something is being watched; the sweep is
        # never urgent (it self-skips probed ids, so it just resumes).
        self.sub_sweep.set_concurrency(
            sub_sweep_mod.STREAMING_CONCURRENCY if self._stream_is_hot()
            else sub_sweep_mod.DEFAULT_CONCURRENCY)
        ids = (file_ids_with_size if file_ids_with_size is not None
              else self._video_files_for_sweep())
        self._sweep_task = asyncio.create_task(self.sub_sweep.run(ids))
        # Session D: a bounded automatic backfill once the sweep's fresh
        # coverage numbers land -- bounded by start_backfill/SubtitleQueue.run
        # to today's remaining daily budget (5 by default), so "automatic"
        # never means "unbounded". No separate scope check is needed here:
        # start_sweep itself never runs for a scope=[] cache-only rebuild
        # (see start_refresh's scope guard above) or fires only when the user
        # explicitly asked for it (POST /api/subs/sweep) -- either way there
        # is no scope left to re-examine by the time a sweep exists to finish.
        self._sweep_task.add_done_callback(self._after_sweep_maybe_backfill)
        return True

    def _stream_is_hot(self):
        """True if any /stream request arrived recently enough that the
        uplink should be treated as in use. `stream_activity` is
        {file_id: epoch}, written by the /stream route on every request."""
        if not self.stream_activity:
            return False
        newest = max(self.stream_activity.values())
        return (time.time() - newest) < sub_sweep_mod.STREAM_HOT_SECONDS

    def _after_sweep_maybe_backfill(self, task):
        if task.cancelled() or task.exception() is not None:
            return
        # Same gate as the periodic timer: subtitles off means no provider calls.
        if not self.cfg.get("subtitles", True):
            return
        self.start_backfill()

    async def _backfill_timer(self):
        """Run a backfill pass every `subtitle_backfill_interval_hours`.

        Why a timer exists at all: the five-a-day budget is a DAILY budget,
        but before this the only automatic trigger was a walking scan's
        sweep finishing. With `auto_refresh_on_startup` defaulting False, an
        app left running for weeks never scanned, never swept, never
        backfilled -- the budget simply evaporated unused and the "~six
        weeks to close the gap" projection never started counting. Every
        existing guard still applies: `start_backfill` refuses while one is
        running, and `SubtitleQueue.run` stops at today's remaining budget,
        so a spurious extra tick costs nothing.
        """
        hours = self.cfg.get("subtitle_backfill_interval_hours", 6)
        try:
            interval = float(hours)
        except (TypeError, ValueError):
            interval = 6.0
        if interval <= 0:
            return  # explicitly disabled
        while True:
            await asyncio.sleep(interval * 3600.0)
            try:
                if not self.cfg.get("subtitles", True):
                    continue
                # Before the first sweep there is no real coverage data, so
                # the worklist is deliberately empty (see
                # _refresh_backfill_worklist) and there is nothing to do.
                if not self.sub_index.sweep_complete():
                    continue
                if self.subtitle_queue.remaining() <= 0:
                    continue
                self._refresh_backfill_worklist()
                if not self.subtitle_queue.data.get("pending"):
                    continue
                self.start_backfill()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - a timer must never die
                log.exception("Periodic subtitle backfill tick failed")

    def start_backfill_timer(self):
        """Start the periodic backfill loop, if configured and not running."""
        if self._backfill_timer_task is not None:
            return False
        hours = self.cfg.get("subtitle_backfill_interval_hours", 6)
        try:
            if float(hours) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        self._backfill_timer_task = asyncio.create_task(self._backfill_timer())
        return True

    def _sidecar_covered_ids(self):
        """file_ids that already have a cached sidecar subtitle on disk.

        ONE listdir of the subs directory, not four os.path.exists calls per
        file: the caller runs this over every id on each worklist sync and
        status poll, and four stat calls per id per poll is a real cost for an
        answer one directory read gives exactly.
        """
        out = set()
        try:
            entries = os.listdir(self.subtitles.subs_dir)
        except OSError:
            return out
        for entry in entries:
            stem, ext = os.path.splitext(entry)
            if ext.lower() in SUB_EXTS:
                out.add(stem)
        return out

    def _uncovered_file_ids(self):
        """The backfill worklist: files with neither a usable embedded English
        TEXT track nor an already-fetched sidecar.

        The sidecar half is load-bearing, and its absence was measured to
        make the queue stop making progress. A successfully backfilled file
        never becomes "covered" in sub_index -- a fetched .srt does not set
        has_text_en, and only a re-probe of the video could -- so on the
        index alone every file the queue ever completed came straight back
        into the worklist, got re-attempted, was instantly satisfied from
        cache, and still cost one of five daily slots. Over weeks the budget
        would go almost entirely on re-confirming finished work.

        Image-only files (has_image_en but not has_text_en) are DELIBERATELY
        still included: nothing text-based can use them, and D-031's "does
        the device actually render an image track" test hasn't run yet, so
        excluding them here would risk permanently orphaning a file if that
        test ever comes back negative. A file never probed (record is None)
        counts as uncovered too -- "unknown" and "known uncovered" get the
        same treatment: queue it, the fetch itself is harmless if it later
        turns out to have had an embedded track all along."""
        have_sidecar = self._sidecar_covered_ids()
        return [fid for fid, _size in self._video_files_for_sweep()
               if fid not in have_sidecar
               and not self._embedded_covers(self.sub_index.get(fid))]

    def _embedded_covers(self, record):
        """True if an embedded English track in `record` counts as "subtitles
        already on". The single coverage predicate shared by /api/play, the
        autoplay lookup and the backfill worklist: a usable text track, or --
        only when `prefer_embedded_image_subtitles` is on -- an image track.
        (The player-kind check stays in /api/play: it needs the live player.)"""
        if not self.cfg.get("prefer_embedded_subtitles", True):
            return False
        if _has_embedded_english_text_track(record):
            return True
        return bool(self.cfg.get("prefer_embedded_image_subtitles", False)
                    and _has_embedded_english_image_track(record))

    async def _resolve_bounded(self, timeout, *args, **kw):
        """resolve() with a wait bound that does NOT cancel it: on timeout
        the lookup finishes detached, so an OpenSubtitles slot already spent
        still lands a cached sidecar instead of being thrown away."""
        # One in-flight resolve per (file, force, allow_opensubtitles): a
        # retry made while a detached lookup is still running joins it
        # instead of starting a second one that would spend another slot.
        key = (args[0] if args else kw.get("file_id"), bool(kw.get("force")),
               kw.get("allow_opensubtitles", True))
        task = self._inflight_resolves.get(key)
        if task is None or task.done():
            task = asyncio.ensure_future(self.subtitles.resolve(*args, **kw))
            self._inflight_resolves[key] = task
            self._detached_resolves.add(task)

            def _finished(t, key=key):
                self._detached_resolves.discard(t)
                if self._inflight_resolves.get(key) is t:
                    del self._inflight_resolves[key]
                t.cancelled() or t.exception()
            task.add_done_callback(_finished)
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    def swept(self):
        """Has the embedded-track sweep ever produced a result?

        Everything downstream is meaningless before it has. With an empty
        index every file reads as uncovered, which built a
        whole-library pending list (rewritten on every later spend), told the
        Settings panel "0 of 2225 files have subtitles", and pointed the
        Fetch-now button at five files that are likely to have had an
        embedded English track all along.
        """
        return self.sub_index.sweep_complete()

    def _refresh_backfill_worklist(self):
        """Sync subtitle_queue.pending against the CURRENT uncovered set and
        priority tiers. Cheap and in-memory (classify_priorities does no
        I/O); called before every backfill run/status read so stale
        priorities (a show that just entered "in progress") never linger.

        A no-op until the sweep has run at least once (see `swept`): there is
        no coverage data yet, so there is no worklist to build -- only a
        library-sized list of guesses."""
        if not self.swept():
            return
        uncovered = self._uncovered_file_ids()
        continue_ids = {it.get("file_id") for it in self.history.continue_watching()}
        watched_map = self.history.progress_map()
        priorities = classify_priorities(self.library.titles_list(), uncovered,
                                         continue_ids, watched_map)
        self.subtitle_queue.sync_pending(priorities)

    def _os_match_hints(self, file_id, info=None):
        """(size, tmdb_id, season, episode) for SubtitleResolver.resolve.

        These feed OpenSubtitles' precise fallback chain -- moviehash first
        (an exact byte-content match), then tmdb_id+season+episode, then the
        weak title-text query. Gathered here rather than inline at each call
        site because the backfill path had them and the two interactive
        paths (/api/play, the TV's /api/subtitles probe) did not, which left
        the whole precision chain as dead code for every subtitle a person
        actually waits on: a play-time lookup could miss a file the queue
        would later match exactly by hash.
        """
        info = info if info is not None else (self.library.file_info(file_id) or {})
        title_rec = self.library.title_for_file(file_id) or {}
        season = episode = None
        if title_rec.get("type") == "show":
            for s in title_rec.get("seasons") or []:
                for ep in s.get("episodes") or []:
                    if ep.get("file_id") == file_id:
                        season, episode = s.get("season"), ep.get("episode")
        return (info.get("size"), title_rec.get("tmdb_id"), season, episode)

    async def _backfill_attempt(self, item):
        """The real fetch subtitle_queue.run() calls per queued item.

        Reuses SubtitleResolver.resolve() end-to-end (sibling-on-drive first,
        THEN OpenSubtitles' moviehash/tmdb/text fallback chain) rather than
        re-implementing any of it -- resolve() is the single write point
        (_accept) already, and calling it here is what makes a sibling hit
        cost zero OpenSubtitles quota even for a queued item.
        """
        fid = item["file_id"]
        info = self.library.file_info(fid) or {}
        size, tmdb_id, season, episode = self._os_match_hints(fid, info)
        self.subtitles.last_os_remaining = None
        self.subtitles.last_os_glitch = None
        self.subtitles.last_os_reset_utc = None
        # Snapshot the provider call counter: the queue charges its daily
        # budget from a REAL /download, not from "an attempt happened".
        # resolve() returns a cached file or a free Drive sibling without
        # ever reaching OpenSubtitles, and charging those was what made the
        # queue re-spend its budget on already-finished files every cycle.
        before = self.subtitles.os_download_calls
        transient = False
        try:
            # Bounded but not cancelled: a download already charged still
            # lands its sidecar after the wait gives up.
            path = await self._resolve_bounded(
                25.0, fid, info.get("name") or fid, info.get("drive_id"),
                info.get("parent_id"), size=size,
                tmdb_id=tmdb_id, season=season, episode=episode)
        except Exception as e:
            # A timeout or crash is not a "not found": say so in the log and
            # flag it so the queue can tell it apart from a genuine miss.
            transient = True
            log.warning("Subtitle backfill lookup failed for %s: %r", fid, e)
            path = None
        return {"ok": bool(path), "transient": transient,
                "remaining": self.subtitles.last_os_remaining,
                "spent": self.subtitles.os_download_calls > before,
                # A malformed 200 from the provider: the slot is gone and the
                # next item would almost certainly meet the same answer, so
                # the queue stops the run instead of feeding it the rest of
                # today's budget (this cost a whole day's budget once).
                "glitch": self.subtitles.last_os_glitch,
                "reset_utc": self.subtitles.last_os_reset_utc}

    def start_backfill(self, limit=None):
        """Fire-and-forget one backfill pass, unless one is already running.
        Same race-free check-then-set shape as start_sweep (see its
        docstring): `running` flips synchronously before `create_task`."""
        if self.subtitle_queue.status.get("running"):
            return False
        self.subtitle_queue.status["running"] = True
        try:
            self._refresh_backfill_worklist()
            self._backfill_task = asyncio.create_task(
                self.subtitle_queue.run(self._backfill_attempt, limit=limit,
                                        is_blocked=self._backfill_blocked))
        except BaseException:
            # No task exists to reset the flag, so reset it here.
            self.subtitle_queue.status["running"] = False
            raise
        return True

    def _backfill_blocked(self, file_id):
        """Free pre-check the queue uses to skip an item instead of spending
        a slot on it. A file inside its OpenSubtitles negative-cache TTL
        would have resolve() return None with no network call at all, and
        that no-op used to be charged as a full download -- which let a
        handful of genuinely-missing files hold the head of the queue and
        starve everything behind them. An already-cached sidecar is skipped
        for the same reason (the worklist normally excludes those, but a
        sync can lag a fetch that just landed)."""
        if self.subtitles.cached(file_id):
            return True
        return bool(self.subtitles.negative.blocked(file_id, "opensubtitles"))

    def _sub_decision_lookup(self, file_id):
        """(sub_path, prefer_lang) for PlayerManager._advance (autoplay-next).

        Mirrors /api/play's embedded-first decision, but is a strictly
        SYNCHRONOUS, in-memory-only read -- unlike /api/play it must NEVER
        call `subtitles.resolve()` (which fetches over the network): this
        runs inside a poller thread's `finally` block, and blocking there
        on OpenSubtitles would stall that thread's own history writes for
        as long as the fetch takes. An uncovered file simply reuses
        whatever sidecar is already cached from an earlier resolve() (the
        same fallback `sub_cache_lookup` always offered), or nothing.
        """
        if not self.cfg.get("subtitles", True):
            return None, None
        if self._embedded_covers(self.sub_index.get(file_id)):
            return None, "eng"
        return self.subtitles.cached(file_id), None

    def _drain_pending_scope(self, _task):
        """After a scan finishes, run any refresh that was requested meanwhile."""
        if not self._pending_scope and not self._pending_rebuild:
            return
        pending = sorted(self._pending_scope)
        self._pending_scope.clear()
        self._pending_rebuild = False
        self.start_refresh(scope=pending, _draining=True)

    def maybe_autorefresh(self):
        """On startup: rescan if configured to, or if we have drives but no cache.

        Skipped entirely when every selected drive resolves to no live tab
        (all assignments point at tabs that have since been deleted, or the
        drives were never assigned at all): with zero tabs by default, a scan
        in that state would classify nothing and stamp an empty library.json,
        which looks identical to genuine data loss rather than "nothing to
        scan yet" — better to leave the last-known-good cache alone.
        """
        if self.setup_error:
            return
        drives = self.cfg.get("selected_drives") or []
        if not drives:
            return
        from . import sections as sections_mod
        drive_sections = self.cfg.get("drive_sections") or {}
        if all(sections_mod.section_for_drive(drive_sections, d) is None for d in drives):
            return
        if self.cfg.get("auto_refresh_on_startup") or self.library.is_empty():
            self.start_refresh()

    async def preflight(self):
        """Verify rclone can produce a token; record a friendly error if not."""
        try:
            await self.tokens.get_token()
            self.setup_error = None
        except RcloneError as e:
            self.setup_error = str(e)
            log.warning("Preflight failed: %s", e)

    async def aclose(self):
        try:
            self.history.flush()
        except Exception:
            pass
        self.awake.shutdown()
        await self.api.aclose()
        await self.streamer.aclose()
        await self.tmdb.aclose()
        await self.subtitles.aclose()


def _playlist_entries(rec, start=None, shuffle=False, seed=0):
    """Build the canonical ordered playlist entries for a title record.

    Mirrors the Fire TV app's own queue-flattening exactly, so the m3u the
    server emits and the queue the app builds locally agree episode-for-
    episode: extras seasons are skipped, movies contribute their single
    file, and `start`/`shuffle` slice/reorder identically on both sides.
    """
    entries = []
    if rec.get("type") == "show":
        show_title = rec.get("title")
        for s in rec.get("seasons", []):
            if s.get("extras"):
                continue
            season_num = s.get("season")
            for ep in s.get("episodes", []):
                file_id = ep.get("file_id")
                if not file_id:
                    continue
                ep_num = ep.get("episode")
                ep_title = ep.get("title") or ep.get("name") or file_id
                if season_num is not None and ep_num is not None:
                    label = "%s · S%02dE%02d · %s" % (show_title, season_num, ep_num, ep_title)
                else:
                    label = ep_title
                entries.append({
                    "file_id": file_id,
                    "label": label,
                    "duration_ms": ep.get("duration_ms"),
                })
    elif rec.get("type") == "movie":
        file_id = rec.get("file_id")
        if file_id:
            entries.append({
                "file_id": file_id,
                "label": rec.get("title"),
                "duration_ms": rec.get("duration_ms"),
            })

    if shuffle:
        from .shuffle import seeded_shuffle
        entries = seeded_shuffle(entries, seed)
    # Slice to the start episode AFTER any shuffle, so an up-next relaunch that
    # passes ?start=<next episode> resumes the shuffled order at that episode
    # rather than replaying from the top. (App and server shuffle identically,
    # so the sliced suffix matches the app's queue position.)
    if start:
        idx = next((i for i, e in enumerate(entries) if e["file_id"] == start), None)
        if idx is not None:
            entries = entries[idx:]
    return entries


def _drive_error_response(e):
    status = 404 if e.status == 404 else (403 if e.status == 403 else 502)
    return JSONResponse(
        {"error": "drive_api", "status": e.status, "reason": e.reason, "message": e.message},
        status_code=status,
    )


def create_app(cfg=None):
    cfg = cfg or config_mod.load_config()

    @asynccontextmanager
    async def lifespan(app):
        state = AppState(cfg)
        app.state.dc = state
        await state.preflight()
        state.maybe_autorefresh()
        # The five-a-day subtitle budget is a DAILY budget; nothing else
        # schedules a daily run (see AppState._backfill_timer).
        state.start_backfill_timer()
        yield
        await state.aclose()

    app = FastAPI(title="drivecast", lifespan=lifespan)

    # ---- remote access (opt-in) ----
    # Loopback is trusted (the socket peer, not a spoofable header). "testclient"
    # is starlette's TestClient seam and is likewise a socket-level identity.
    LOCAL_HOSTS = ("127.0.0.1", "::1", "testclient")

    def _client_is_local(request):
        client = request.client
        return bool(client) and client.host in LOCAL_HOSTS

    @app.middleware("http")
    async def _remote_auth(request: Request, call_next):
        if _client_is_local(request):
            return await call_next(request)
        # /api/ping is an unauthenticated discovery probe, reachable even when
        # remote access is off, so a TV/phone client scanning the LAN can find
        # the server and prompt the user to enable Remote Access. It discloses
        # nothing beyond the app's presence.
        if request.url.path == "/api/ping":
            return await call_next(request)
        if not cfg.get("remote_access"):
            return JSONResponse({"error": "remote_disabled"}, status_code=403)
        # The CA certificate is public material (its subject + public key are
        # disclosed in every TLS handshake; no private key is served), and the
        # phone has no token cookie yet when it fetches it — exempt it from the
        # token check. The remote_access 403 gate above still applies.
        if request.url.path == "/api/remote/ca":
            return await call_next(request)
        cookie_token = request.cookies.get("dc_token")
        query_token = request.query_params.get("token")
        token = cookie_token or query_token
        cfg_token = cfg.get("remote_token") or ""
        # Constant-time compare; an empty configured token never authorizes.
        # Bytes compare: constant-time AND tolerant of non-ASCII client input
        # (str compare_digest raises TypeError on non-ASCII, a pre-auth 500).
        ok = (bool(cfg_token) and bool(token)
              and hmac.compare_digest(str(token).encode("utf-8"),
                                      str(cfg_token).encode("utf-8")))
        if not ok:
            if "text/html" in request.headers.get("accept", ""):
                return HTMLResponse(_token_page(), status_code=401)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        response = await call_next(request)
        # A ?token= link bootstraps a durable browser session so later
        # same-origin requests (the <video> src, static assets) carry the cookie.
        if not cookie_token and query_token:
            # secure= only when minted over TLS, so a cookie set on the HTTPS
            # listener never rides the plain-HTTP listener.
            response.set_cookie("dc_token", query_token, httponly=True,
                                samesite="lax", max_age=180 * 24 * 3600,
                                secure=(request.url.scheme == "https"))
        return response

    @app.middleware("http")
    async def _static_no_cache(request: Request, call_next):
        """Force revalidation of static assets. StaticFiles already emits an
        ETag/Last-Modified, but without Cache-Control browsers cache
        heuristically and can serve a stale app.js/style.css after an update.
        `no-cache` means "revalidate every time" — the etag makes that a cheap
        304, so the browser always picks up a new build."""
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # ---- setup / preflight gate ----

    @app.get("/", response_class=HTMLResponse)
    async def index():
        state = app.state.dc
        if state.setup_error:
            return HTMLResponse(_setup_page(state.setup_error))
        # Read as UTF-8 explicitly: inside a packaged .app the process locale can
        # default to ASCII, which would choke on the page's unicode glyphs.
        with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
            return HTMLResponse(f.read())

    # ---- API ----

    @app.get("/api/drives")
    async def api_drives():
        state = app.state.dc
        if state.setup_error:
            return JSONResponse({"error": "setup", "message": state.setup_error}, status_code=503)
        try:
            return {"drives": await state.api.list_drives()}
        except RcloneError as e:
            return JSONResponse({"error": "setup", "message": str(e)}, status_code=503)

    @app.get("/api/browse")
    async def api_browse(drive_id: str, folder_id: str = None, page_token: str = None):
        state = app.state.dc
        try:
            res = await state.api.browse(
                drive_id, folder_id, page_token, page_size=cfg.get("page_size", 200)
            )
            return res
        except DriveAPIError as e:
            return _drive_error_response(e)
        except RcloneError as e:
            return JSONResponse({"error": "setup", "message": str(e)}, status_code=503)

    @app.get("/api/search")
    async def api_search(q: str, page_token: str = None):
        state = app.state.dc
        if not q.strip():
            return {"files": [], "nextPageToken": None}
        try:
            return await state.api.search(q, page_token, page_size=cfg.get("page_size", 200))
        except DriveAPIError as e:
            return _drive_error_response(e)
        except RcloneError as e:
            return JSONResponse({"error": "setup", "message": str(e)}, status_code=503)

    # ---- library ----

    @app.get("/api/library")
    async def api_library():
        state = app.state.dc
        return {
            "titles": state.library.titles_list(),
            "generated_at": state.library.generated_at(),
            "scanning": bool(state.scanner.status.get("running")),
            "selected_drives": state.cfg.get("selected_drives", []),
        }

    @app.get("/api/title/{title_id}")
    async def api_title(title_id: str):
        state = app.state.dc
        rec = state.library.get(title_id)
        if not rec:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return rec

    @app.get("/api/playlist/{title_id}.m3u")
    async def api_playlist_m3u(title_id: str, request: Request,
                               start: str = None, shuffle: int = 0, seed: int = 0):
        """M3U playlist for a title, in the exact order the Fire TV app's own
        queue would play it (see _playlist_entries). Declared BEFORE the JSON
        twin below: starlette matches routes in declaration order and
        `{title_id}` ([^/]+) would otherwise swallow the ".m3u" suffix."""
        state = app.state.dc
        rec = state.library.get(title_id)
        if not rec:
            return JSONResponse({"error": "not_found"}, status_code=404)
        entries = _playlist_entries(rec, start, bool(shuffle), seed)
        base = str(request.base_url).rstrip("/")
        token = state.cfg.get("remote_token") or ""
        suffix = ("?token=" + urllib.parse.quote(token)) if token else ""
        lines = ["#EXTM3U", "#PLAYLIST:" + (rec.get("title") or title_id)]
        for e in entries:
            dur = e.get("duration_ms")
            lines.append("#EXTINF:%d,%s" % (int(dur / 1000) if dur else -1, e["label"]))
            lines.append("%s/stream/%s%s" % (base, e["file_id"], suffix))
        return Response("\n".join(lines) + "\n", media_type="audio/x-mpegurl")

    @app.get("/api/playlist/{title_id}")
    async def api_playlist_json(title_id: str, request: Request,
                                start: str = None, shuffle: int = 0, seed: int = 0):
        """JSON twin of the m3u above (same order for identical params) — the
        app fetches this to learn the exact queue it should build locally."""
        state = app.state.dc
        rec = state.library.get(title_id)
        if not rec:
            return JSONResponse({"error": "not_found"}, status_code=404)
        entries = _playlist_entries(rec, start, bool(shuffle), seed)
        return {
            "title_id": title_id,
            "items": [{"file_id": e["file_id"], "name": e["label"],
                      "duration_ms": e.get("duration_ms")} for e in entries],
        }

    @app.post("/api/refresh")
    async def api_refresh(request: Request):
        state = app.state.dc
        if state.setup_error:
            return JSONResponse({"error": "setup", "message": state.setup_error}, status_code=503)
        if state.scanner.status.get("running"):
            return {"started": False, "running": True}
        selected = state.cfg.get("selected_drives") or []
        if not selected:
            return JSONResponse(
                {"error": "no_drives", "message": "No drives selected. Pick drives in Settings."},
                status_code=400,
            )
        # Optional body {"drives": [...]} scopes the refresh to those drives
        # (must be selected). No/empty body = full refresh (menubar compat).
        try:
            body = await request.json()
        except Exception:
            body = {}
        drives = body.get("drives") if isinstance(body, dict) else None
        if drives is not None and not isinstance(drives, list):
            return JSONResponse({"error": "bad_request", "message": "drives must be a list"},
                                status_code=400)
        if drives:
            unknown = [d for d in drives if d not in selected]
            if unknown:
                return JSONResponse(
                    {"error": "bad_request",
                     "message": "Not a selected drive: %s" % ", ".join(unknown)},
                    status_code=400,
                )
        started = state.start_refresh(scope=drives or None)
        return {"started": started, "running": True, "scope": drives or selected}

    @app.get("/api/refresh/status")
    async def api_refresh_status():
        state = app.state.dc
        st = dict(state.scanner.status)
        scope = st.get("scope") or []
        names = {}
        if scope:
            try:
                names = {d["id"]: d.get("name") for d in await state.api.list_drives()}
            except Exception:
                pass
        st["scope_names"] = [names.get(d) or d for d in scope]
        return st

    @app.get("/api/sections")
    async def api_sections():
        """Tab + behavior metadata for the UI (see sections.py for the split):
        `sections` is the user's actual nav bar (fully resolved, each entry
        already carries which behavior powers it), `behaviors` is the
        catalog a "create tab" picker offers ("behaves like...")."""
        from . import sections as sections_mod
        return {"sections": sections_mod.meta_list(), "behaviors": sections_mod.behaviors_meta()}

    @app.get("/api/settings")
    async def api_get_settings():
        state = app.state.dc
        from .player import detect_player
        available = [k for k in ("mpv", "iina", "vlc", "infuse") if detect_player(k)[0]]
        return {
            "selected_drives": state.cfg.get("selected_drives", []),
            "drive_sections": state.cfg.get("drive_sections", {}),
            "tabs": state.cfg.get("tabs", []),
            "auto_refresh_on_startup": bool(state.cfg.get("auto_refresh_on_startup", False)),
            "autoplay_next": bool(state.cfg.get("autoplay_next", True)),
            "subtitles": bool(state.cfg.get("subtitles", True)),
            "prefer_embedded_subtitles": bool(state.cfg.get("prefer_embedded_subtitles", True)),
            "keep_awake": bool(state.cfg.get("keep_awake", True)),
            "player": state.cfg.get("player", "auto"),
            "available_players": available,
            "remote_access": bool(state.cfg.get("remote_access", False)),
        }

    @app.post("/api/settings")
    async def api_post_settings(request: Request):
        state = app.state.dc
        body = await request.json()
        added_drives = []
        removed_drives = []
        order_changed = False
        if "selected_drives" in body:
            old_drives = state.cfg.get("selected_drives") or []
            new_drives = list(body.get("selected_drives") or [])
            # Set diff, not `!=`, for added/removed — but order itself IS
            # meaningful downstream and a pure reorder still needs a rebuild:
            # Scanner.scan builds all_records by `for drive_id in selected`
            # (library.py:1370, `selected` == this selected-drives list), and
            # group_seasons (library.py:543-650) merges same-named seasons
            # across drives first-seen-wins via its `order` list (library.py
            # :556/592) — the merged record's drive_id/year/_thumb and
            # source_drives order all follow whichever drive's records were
            # walked first. Reordering with no add/remove is flagged
            # separately (order_changed) so it can still kick a cheap
            # cache-only rebuild below without a Drive re-walk.
            added_drives = [d for d in new_drives if d not in old_drives]
            removed_drives = [d for d in old_drives if d not in new_drives]
            order_changed = (not added_drives and not removed_drives
                             and new_drives != old_drives)
            state.cfg["selected_drives"] = new_drives
        # Tabs MUST be processed before drive_sections below: the
        # drive_sections `valid = sections_mod.all_sections()` check has to
        # see tabs created in *this same request* (the settings UI creates a
        # tab and assigns a drive to it in one save), and set_tabs() is what
        # makes that happen — it swaps sections.py's in-process cache before
        # all_sections() is next called.
        if "tabs" in body:
            from . import sections as sections_mod
            raw_tabs = body.get("tabs")
            if not isinstance(raw_tabs, list):
                raw_tabs = []
            old_by_key = {t.get("key"): t for t in (state.cfg.get("tabs") or [])
                          if isinstance(t, dict)}
            valid_behaviors = sections_mod.behaviors()
            cleaned = sections_mod.validate_tabs(raw_tabs)
            # validate_tabs() is lenient by design (existing garbage in
            # config.json shouldn't crash Settings) — it just silently drops
            # anything it can't repair. That's fine for an UNCHANGED round-trip
            # of the current tab list, but an entry the client is actively
            # CREATING or EDITING must not vanish silently — the user asked to
            # add/change a tab and would get a 200 with nothing back. Only
            # free-pass a genuine no-op round-trip (same key AND same
            # label+behavior as the stored record); anything else (a create, or
            # an edit — including one that turns an existing tab invalid) falls
            # through to strict validation below so it 400s instead of dropping.
            for entry in raw_tabs:
                if not isinstance(entry, dict):
                    continue
                raw_key = str(entry.get("key") or "").strip().lower()
                label = str(entry.get("label") or "").strip()
                behavior = entry.get("behavior")
                old = old_by_key.get(raw_key) if raw_key else None
                if (old is not None
                        and str(old.get("label") or "").strip() == label
                        and old.get("behavior") == behavior):
                    continue  # unchanged existing tab -- lenient round-trip
                if not label or len(label) > 40:
                    return JSONResponse(
                        {"error": "bad_request",
                         "message": "Tab label must be 1-40 characters"},
                        status_code=400)
                if behavior not in valid_behaviors:
                    return JSONResponse(
                        {"error": "bad_request",
                         "message": "Unknown tab behavior: %r" % (behavior,)},
                        status_code=400)
                prospective_key = sections_mod._slugify(raw_key or label, "tab")
                survived = any(
                    t["key"] == prospective_key and t["label"] == label
                    and t["behavior"] == behavior
                    for t in cleaned
                )
                if not survived:
                    return JSONResponse(
                        {"error": "bad_request",
                         "message": "Tab key %r collides with an existing tab"
                                    % prospective_key},
                        status_code=400)
            state.cfg["tabs"] = cleaned
            sections_mod.set_tabs(cleaned)
        # Section assignments: validate values, refresh just the drives whose
        # section changed (their content must be re-classified).
        section_changed = []
        if "drive_sections" in body:
            from . import sections as sections_mod
            raw = body.get("drive_sections")
            if not isinstance(raw, dict):
                raw = {}
            selected_now = state.cfg.get("selected_drives") or []
            valid = sections_mod.all_sections()
            # Only selected drives can carry an assignment — a stale entry for
            # an unchecked drive would keep an empty section tab alive.
            new_sections = {k: v for k, v in raw.items()
                            if isinstance(k, str) and v in valid
                            and k in selected_now}
            old_sections = state.cfg.get("drive_sections") or {}
            # Compare RAW values (no "or entertainment" fallback — that
            # fallback doesn't make sense once tabs are user-defined and
            # zero-default). A drive whose assignment appears, changes, or
            # disappears entirely (e.g. its tab was just deleted, above) is a
            # change that needs a scoped refresh: a now-unassigned drive's
            # records must disappear from the library, and a rescan of a
            # tab-less drive is a no-op, which is exactly what we want.
            section_changed = [
                d for d in selected_now
                if old_sections.get(d) != new_sections.get(d)
            ]
            state.cfg["drive_sections"] = new_sections
        if "auto_refresh_on_startup" in body:
            state.cfg["auto_refresh_on_startup"] = bool(body.get("auto_refresh_on_startup"))
        if "autoplay_next" in body:
            state.cfg["autoplay_next"] = bool(body.get("autoplay_next"))
        if "subtitles" in body:
            state.cfg["subtitles"] = bool(body.get("subtitles"))
        if "prefer_embedded_subtitles" in body:
            state.cfg["prefer_embedded_subtitles"] = bool(body.get("prefer_embedded_subtitles"))
        if "keep_awake" in body:
            state.cfg["keep_awake"] = bool(body.get("keep_awake"))
        if "player" in body:
            choice = str(body.get("player") or "auto")
            if choice in ("auto", "mpv", "iina", "vlc", "infuse"):
                state.cfg["player"] = choice
        # Remote access: a change to the bind mode only takes effect on the next
        # launch, so flag restart_required when it flips. Enabling it mints a
        # token if none exists yet (persisted like any other non-secret setting).
        restart_required = False
        if "remote_access" in body:
            new_remote = bool(body.get("remote_access"))
            if new_remote != bool(state.cfg.get("remote_access", False)):
                restart_required = True
            state.cfg["remote_access"] = new_remote
            if new_remote and not state.cfg.get("remote_token"):
                state.cfg["remote_token"] = secrets.token_urlsafe(16)
        # A tab deletion arrives as a tabs-only POST (the Settings delete
        # button sends just the new tab list, no drive_sections). Reconcile any
        # drive still pointing at a now-deleted tab: drop the dangling
        # assignment and fold that drive into section_changed so it is rescanned
        # and its orphaned records leave the library. When drive_sections IS in
        # the body it was already filtered against all_sections() above, so this
        # pass is a harmless no-op in that case.
        if "tabs" in body:
            from . import sections as sections_mod
            valid = set(sections_mod.all_sections())
            ds = state.cfg.get("drive_sections") or {}
            orphaned = [d for d, v in ds.items() if v not in valid]
            if orphaned:
                state.cfg["drive_sections"] = {d: v for d, v in ds.items()
                                               if v in valid}
                for d in orphaned:
                    if d not in section_changed:
                        section_changed.append(d)
        config_mod.save_config(state.cfg)
        started = False
        # Walk only what changed: newly selected drives plus drives whose tab
        # assignment changed (union — an add + a section change in one POST,
        # which is exactly what the Settings UI sends, must not drop either).
        scope = sorted(set(added_drives) | set(section_changed))
        if scope:
            started = state.start_refresh(scope=scope)
            if not started and (removed_drives or order_changed):
                # The scoped refresh named only drives that turned out to be
                # unselected by the time this ran (e.g. a section change on a
                # drive that this same POST also deselected) — start_refresh
                # correctly started nothing, but a rebuild is still owed
                # below, so fall through to it instead of dropping it.
                started = state.start_refresh(scope=[])
        elif removed_drives or order_changed:
            # Nothing new to walk, but either the removed drives' titles must
            # leave, or the reorder can flip which drive wins a cross-drive
            # season merge (see order_changed's comment above) — a cache-only
            # rebuild (empty scope) re-derives the library from cache without
            # touching Drive.
            started = state.start_refresh(scope=[])
        return {
            "ok": True,
            "selected_drives": state.cfg.get("selected_drives", []),
            "drive_sections": state.cfg.get("drive_sections", {}),
            "tabs": state.cfg.get("tabs", []),
            "auto_refresh_on_startup": bool(state.cfg.get("auto_refresh_on_startup", False)),
            "autoplay_next": bool(state.cfg.get("autoplay_next", True)),
            "subtitles": bool(state.cfg.get("subtitles", True)),
            "prefer_embedded_subtitles": bool(state.cfg.get("prefer_embedded_subtitles", True)),
            "keep_awake": bool(state.cfg.get("keep_awake", True)),
            "remote_access": bool(state.cfg.get("remote_access", False)),
            "restart_required": restart_required,
            "refresh_started": started,
        }

    @app.api_route("/stream/{file_id}", methods=["GET", "HEAD"])
    async def stream(file_id: str, request: Request):
        state = app.state.dc
        if request.method == "HEAD":
            try:
                return await state.streamer.head(file_id)
            except DriveAPIError as e:
                return _drive_error_response(e)
        state.stream_activity[file_id] = time.time()
        if len(state.stream_activity) > 64:
            del state.stream_activity[min(state.stream_activity, key=state.stream_activity.get)]
        return await state.streamer.stream(file_id, request)

    @app.get("/api/stream/recent")
    async def api_stream_recent():
        """Recently-streamed file_ids, most-recent first — lets the app learn
        which episode a next-episode/shuffle playlist actually landed on."""
        state = app.state.dc
        now = time.time()
        items = sorted(state.stream_activity.items(), key=lambda kv: kv[1], reverse=True)[:25]
        return {"now": now, "items": [{"file_id": fid, "ts": ts, "age": now - ts} for fid, ts in items]}

    @app.get("/api/media-info/{file_id}")
    async def api_media_info(file_id: str):
        """Video stream facts (cached ffprobe) + whether the Fire TV app should
        ask VLC for software decoding (see media_info.vlc_policy)."""
        state = app.state.dc
        info = await state.media_info.get(file_id)
        return {"file_id": file_id, "known": info is not None, **(info or {}),
                **media_info.vlc_policy(info, state.cfg)}

    # ---- keep-awake ("Are you still watching?") ----
    # Behind the normal auth middleware (no exemptions): the TV app will adopt
    # these later, so the status shape is deliberately client-agnostic.

    @app.get("/api/awake/status")
    async def api_awake_status():
        return app.state.dc.awake.status()

    @app.post("/api/awake/extend")
    async def api_awake_extend():
        """User said 'Yes, keep watching' — restart a fresh grace period."""
        return app.state.dc.awake.extend()

    @app.post("/api/awake/release")
    async def api_awake_release():
        """User said 'No' — drop the assertion now; the Mac may sleep."""
        return app.state.dc.awake.release_now()

    @app.post("/api/play")
    async def api_play(request: Request):
        state = app.state.dc
        # A phone must never launch mpv on the Mac — it uses the web player.
        if not _client_is_local(request):
            return JSONResponse(
                {"error": "local_only",
                 "message": "Playback from this device uses the web player."},
                status_code=403,
            )
        body = await request.json()
        file_id = body.get("file_id")
        name = body.get("name") or file_id
        if not file_id:
            return JSONResponse({"error": "bad_request", "message": "file_id required"}, status_code=400)
        duration_ms = body.get("duration_ms")
        # Prefer the library cache for duration so playback launches without a
        # blocking Drive metadata call — and for the media kind, so audio
        # files play windowed no matter which UI path launched them
        # (Continue shelf and movie tiles don't send media).
        info = state.library.file_info(file_id)
        if not duration_ms and info:
            duration_ms = info.get("duration_ms")
        media = body.get("media") or (info or {}).get("media")
        drive_id = body.get("drive_id")
        parent_id = body.get("parent_id")
        # Optional autoplay queue: episodes to play AFTER this one. Whitelist the
        # fields and drop anything malformed (non-list / items without a file_id).
        queue = []
        raw_queue = body.get("queue")
        if isinstance(raw_queue, list):
            for item in raw_queue:
                if not isinstance(item, dict):
                    continue
                fid = item.get("file_id")
                if not fid:
                    continue
                qinfo = state.library.file_info(fid)
                qdur = item.get("duration_ms")
                if not qdur and qinfo:
                    qdur = qinfo.get("duration_ms")
                qmedia = item.get("media") or (qinfo or {}).get("media")
                queue.append({
                    "file_id": fid,
                    "name": item.get("name") or fid,
                    "duration_ms": qdur,
                    "media": "audio" if qmedia == "audio" else None,
                })
        if body.get("start_over"):
            # Clear the saved resume position so the player starts at 0.
            state.history.update(file_id, name=name, drive_id=drive_id,
                                 parent_id=parent_id, position=0.0, force=True)
        # English subtitles: embedded track first (session C), else cached /
        # sibling .srt / OpenSubtitles (session A) — bounded so a slow
        # lookup can only delay playback, never block it.
        sub_path = None
        prefer_lang = None
        if state.cfg.get("subtitles", True) and media != "audio":
            record = state.sub_index.get(file_id)
            # Which player will actually receive this decides whether an
            # embedded track is usable at all. Infuse's infuse:// scheme has
            # no language or track parameter -- only `sub=` -- so a
            # prefer_lang hint is silently dropped at launch. Short-
            # circuiting resolve() for Infuse would therefore hand it
            # NEITHER a sidecar (which it used to get) nor a language hint,
            # and then report subtitles=True: strictly worse than before this
            # feature existed. Resolve the player kind up front and let
            # Infuse fall through to the sidecar path it can actually use.
            player_kind, _player_path = detect_player(state.cfg.get("player", "auto"))
            embedded_usable = player_kind in ("mpv", "iina", "vlc")
            covered = (embedded_usable
                       and state.cfg.get("prefer_embedded_subtitles", True)
                       and _has_embedded_english_text_track(record))
            if (not covered and embedded_usable
                    and state.cfg.get("prefer_embedded_subtitles", True)
                    and state.cfg.get("prefer_embedded_image_subtitles", False)
                    and _has_embedded_english_image_track(record)):
                # Image-only English (PGS/dvd_subtitle): the same --slang /
                # --sub-language flag selects it, and these players are
                # documented to render it. Off by default until D-031's
                # device test confirms it paints pixels on this hardware.
                covered = True
            if covered:
                # Covered by an embedded track: tell the player which
                # language to select and skip resolve() entirely -- no
                # sidecar fetch, no quota spent, no 10s bound needed. A
                # forced-only or hearing-impaired-only English track does
                # NOT reach here (see _has_embedded_english_text_track's
                # docstring); it falls through to resolve() below exactly
                # as if the index had never seen an embedded track.
                prefer_lang = "eng"
            else:
                size, tmdb_id, season, episode = state._os_match_hints(file_id)
                try:
                    sub_path = await state._resolve_bounded(
                        10.0, file_id, name, drive_id, parent_id,
                        size=size, tmdb_id=tmdb_id,
                        season=season, episode=episode)
                except Exception:
                    sub_path = None
        try:
            result = state.player.play(
                file_id, name, duration_ms=duration_ms,
                drive_id=drive_id, parent_id=parent_id, queue=queue,
                media="audio" if media == "audio" else None,
                sub_path=sub_path, prefer_lang=prefer_lang,
            )
            result["subtitles"] = bool(sub_path) or bool(prefer_lang)
            return result
        except PlayerError as e:
            return JSONResponse({"error": "no_player", "message": str(e)}, status_code=501)

    @app.get("/api/continue")
    async def api_continue():
        state = app.state.dc
        items = state.history.continue_watching()
        # Enrich each item with its owning library title so the shelf can show
        # the poster and a clean display name instead of the raw filename.
        for item in items:
            rec = state.library.title_for_file(item.get("file_id"))
            if rec:
                item["title"] = rec.get("title")
                item["title_id"] = rec.get("id")
                item["type"] = rec.get("type")
                item["poster"] = rec.get("poster")
                item["section"] = rec.get("section")
        return {"items": items}

    @app.delete("/api/continue/{file_id}")
    async def api_continue_remove(file_id: str):
        """Dismiss a tile from Continue Watching by dropping its history entry.

        The resume position is intentionally discarded. Auth is handled by the
        remote-access middleware, same as /api/progress."""
        state = app.state.dc
        removed = state.history.remove(file_id)
        return {"ok": True, "removed": removed}

    @app.get("/api/watched-map")
    async def api_watched_map():
        """file_id -> last_played epoch, for the client-side "Recently watched"
        sort; plus per-file progress for course/episode completion UI."""
        state = app.state.dc
        return {"map": state.history.last_played_map(),
                "progress": state.history.progress_map()}

    @app.get("/api/ping")
    async def api_ping():
        """Unauthenticated discovery probe (exempted in _remote_auth) so LAN
        clients can find the server even before Remote Access is enabled."""
        return {"app": "drivecast", "version": APP_VERSION,
                "remote": bool(cfg.get("remote_access"))}

    @app.api_route("/api/subtitles/{file_id}", methods=["GET", "HEAD"])
    async def api_subtitles(file_id: str, name: str = None,
                            drive_id: str = None, parent_id: str = None,
                            refresh: bool = False):
        """Resolve (and cache) an English subtitle for a video and serve it.

        Remote players (the web player can't use these yet, but TV/phone apps
        can) probe with HEAD before playback and stream the same URL as a
        subtitle track. Query overrides support files outside the library
        index (browse-only playback). Same bounded-resolution philosophy as
        /api/play: a slow lookup 404s rather than blocking.

        `refresh=1` forgets this file's remembered dead ends and re-fetches,
        for the case where the quality gate rejected a subtitle it should have
        kept.

        The OpenSubtitles step is skipped when the index already shows a
        usable embedded English text track. This endpoint is the TV client's
        only subtitle mechanism, and it HEAD-probes before every playback --
        so on the files that carry their own English track it was
        spending scarce anonymous-tier downloads fetching a redundant
        sidecar, invisibly draining the same five-a-day the backfill queue
        is budgeted against. The free sources (cache, Drive sibling) still
        run: if a sidecar is already there, or sits next to the video, the
        TV gets it and renders it without depending on VLC-Android's own
        track selection. `refresh=1` always allows the paid step -- that
        request is a person explicitly asking for it."""
        state = app.state.dc
        if not state.cfg.get("subtitles", True):
            return JSONResponse({"error": "subtitles_disabled"}, status_code=404)
        info = state.library.file_info(file_id) or {}
        name = name or info.get("name") or file_id
        drive_id = drive_id or info.get("drive_id")
        parent_id = parent_id or info.get("parent_id")
        embedded_covered = (
            state.cfg.get("prefer_embedded_subtitles", True)
            and _has_embedded_english_text_track(state.sub_index.get(file_id)))
        size, tmdb_id, season, episode = state._os_match_hints(file_id, info)
        try:
            sub_path = await state._resolve_bounded(
                15.0, file_id, name, drive_id, parent_id, force=bool(refresh),
                size=size, tmdb_id=tmdb_id, season=season, episode=episode,
                allow_opensubtitles=bool(refresh) or not embedded_covered)
        except Exception:
            sub_path = None
        media_type = mime_for(sub_path) if sub_path else None
        if not media_type:
            return JSONResponse({"error": "no_subtitles"}, status_code=404)
        return FileResponse(sub_path, media_type=media_type)

    @app.post("/api/subtitles/{file_id}/refresh")
    async def api_subtitles_refresh(file_id: str):
        """Re-fetch one file's subtitle, ignoring cache and remembered misses.

        The UI-facing twin of `?refresh=1`: it returns a small JSON verdict
        instead of the file, so a Settings/detail button can report "found" or
        "still nothing" without downloading the track."""
        state = app.state.dc
        if not state.cfg.get("subtitles", True):
            return JSONResponse({"error": "subtitles_disabled"}, status_code=404)
        info = state.library.file_info(file_id) or {}
        name = info.get("name") or file_id
        size, tmdb_id, season, episode = state._os_match_hints(file_id, info)
        try:
            sub_path = await state._resolve_bounded(
                20.0, file_id, name, info.get("drive_id"),
                info.get("parent_id"), force=True,
                size=size, tmdb_id=tmdb_id, season=season, episode=episode)
        except Exception:
            sub_path = None
        return {"ok": True, "file_id": file_id, "subtitles": bool(sub_path)}

    @app.delete("/api/subtitles/cache")
    async def api_subtitles_clear_cache():
        """Forget every remembered subtitle dead end.

        Deletes no cached subtitle — only the negative cache, so the next play
        of every uncovered title retries its sources. This is the remedy after
        a mistuned gate threshold or a spell of ISP interference, and the
        Settings row that calls it is the reason neither needs hand-written
        curl."""
        state = app.state.dc
        cleared = state.subtitles.negative.clear()
        return {"ok": True, "cleared": cleared}

    @app.post("/api/subs/sweep")
    async def api_subs_sweep_start():
        """Start the embedded-subtitle-track sweep on demand, so the user can
        run it without a rescan. A sweep already in flight is reported, not
        restarted -- same contract as /api/refresh when a scan is already
        running."""
        state = app.state.dc
        if state.sub_sweep.status.get("running"):
            return {"started": False, "running": True}
        started = state.start_sweep()
        return {"started": started, "running": True}

    @app.get("/api/subs/sweep/status")
    async def api_subs_sweep_status():
        return app.state.dc.sub_sweep.status

    @app.post("/api/subs/backfill")
    async def api_subs_backfill_start():
        """Work the OpenSubtitles backfill queue on demand, within today's
        remaining daily budget. A run already in flight is reported, not
        restarted -- same contract as /api/subs/sweep."""
        state = app.state.dc
        if state.subtitle_queue.status.get("running"):
            return {"started": False, "running": True}
        if not state.swept():
            # Nothing to work on yet, and pressing on would spend the day's
            # five slots on files that are likely to already carry an
            # embedded English track.
            return {"started": False, "running": False, "swept": False}
        started = state.start_backfill()
        return {"started": started, "running": True}

    @app.get("/api/subs/backfill/status")
    async def api_subs_backfill_status():
        """Everything the Settings "Subtitle coverage" block shows: how many
        files are covered/uncovered per the sweep's index, how many are
        queued, and today's backfill run/budget state. Re-syncs the worklist
        first so a poll during/after a scan reflects the library's current
        shape, not a stale one."""
        state = app.state.dc
        state._refresh_backfill_worklist()
        total = len(state._video_files_for_sweep())
        snap = state.subtitle_queue.snapshot()
        snap["total_count"] = total
        # Before the first sweep there is NO coverage data. Reporting
        # "0 of 2225 covered" then is not a cautious estimate, it is wrong
        # in the alarming direction -- roughly 40% of those files
        # carry their own text track (30-file sample). The UI shows "run the scan first"
        # off this flag instead of a fabricated number.
        snap["swept"] = state.swept()
        if snap["swept"]:
            uncovered = len(state._uncovered_file_ids())
            snap["covered_count"] = total - uncovered
            snap["uncovered_count"] = uncovered
        else:
            snap["covered_count"] = None
            snap["uncovered_count"] = None
        snap["queued_count"] = snap["pending_count"]
        return snap

    @app.post("/api/enrich")
    async def api_enrich(request: Request):
        """Batch parse filenames and (optionally) enrich with TMDB."""
        state = app.state.dc
        body = await request.json()
        names = body.get("names") or []
        out = {}
        for raw in names:
            parsed = naming.parse(raw)
            entry = {
                "title": parsed["title"],
                "year": parsed["year"],
                "type": parsed["type"],
                "season": parsed["season"],
                "episode": parsed["episode"],
                "poster_key": None,
            }
            if state.tmdb.enabled:
                meta = await state.tmdb.enrich(parsed["title"], parsed["year"], parsed["type"])
                if meta:
                    entry["poster_key"] = meta.get("poster_key")
                    if meta.get("year"):
                        entry["year"] = meta["year"]
            out[raw] = entry
        return {"results": out}

    @app.get("/api/poster/{key:path}")
    async def api_poster(key: str, request: Request):
        """Serve a cached TMDB poster, or proxy a Drive thumbnailLink fallback."""
        state = app.state.dc
        # thumbnailLink fallback: key is passed as ?thumb=<url>
        thumb = request.query_params.get("thumb")
        local = state.tmdb.poster_path(key) if key and key != "_" else None
        if local:
            return FileResponse(local, media_type="image/jpeg")
        if thumb:
            try:
                import httpx
                tok = await state.tokens.get_token()
                async with httpx.AsyncClient(timeout=15.0) as c:
                    r = await c.get(thumb, headers={"Authorization": "Bearer %s" % tok})
                if r.status_code == 200:
                    return Response(
                        content=r.content,
                        media_type=r.headers.get("content-type", "image/jpeg"),
                    )
            except Exception:
                pass
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.get("/api/poster-candidates")
    async def api_poster_candidates(title: str, type: str = "movie"):
        """Candidate TMDB matches for the "Fix poster" picker. Each candidate's
        poster is pre-downloaded, so the UI shows thumbnails via /api/poster."""
        state = app.state.dc
        media = "tv" if type == "tv" else "movie"
        candidates = await state.tmdb.search_candidates(title, media)
        return {"candidates": candidates}

    @app.post("/api/poster-override")
    async def api_poster_override(request: Request):
        """Save a hand-picked poster for a title and apply it live.

        Persists the pick to the override store (consulted first on future
        enrich() calls, so it survives rescans) AND updates the matching
        in-memory library record(s) so both the web UI and the Fire TV app see
        the corrected poster without a full rescan.
        """
        state = app.state.dc
        body = await request.json()
        title = (body.get("title") or "").strip()
        media = "tv" if body.get("type") == "tv" else "movie"
        tmdb_id = body.get("tmdb_id")
        if not title or tmdb_id in (None, ""):
            return JSONResponse({"error": "bad_request"}, status_code=400)
        if not state.tmdb.enabled:
            return JSONResponse({"error": "tmdb_disabled"}, status_code=400)
        meta = await state.tmdb.by_id(tmdb_id, media)
        if not meta:
            return JSONResponse({"error": "not_found"}, status_code=404)
        state.tmdb.set_override(title, media, meta)
        updated = state.library.apply_poster_override(title, media, meta)
        return {"poster_key": meta.get("poster_key"), "title": meta.get("title"),
                "year": meta.get("year"), "updated": updated}

    # ---- remote access endpoints ----

    @app.get("/api/remote")
    async def api_remote():
        """Connection info for the phone. Only reachable through the middleware,
        so any authorized caller may see the token (it's already in their URL)."""
        state = app.state.dc
        port = int(state.cfg.get("port", 8737))
        token = state.cfg.get("remote_token") or ""
        lan_ip = _lan_ip()
        # The trusted-LAN HTTPS listener; set by the entry point when it's live.
        https_port = state.cfg.get("_lan_https_port")
        ca_url = None
        ca_fingerprint = None
        https_stale = False
        urls = []
        # Trusted-LAN HTTPS first: it's the goal (Tailscale-independent, survives
        # HTTPS-Only Safari) and urls[0] drives the default QR. Then Tailscale
        # Serve HTTPS, plain-HTTP tailnet, and plain Wi-Fi last (Android/guests).
        if https_port and lan_ip:
            # The listener bound its leaf at process start; if the LAN IP has
            # since changed (DHCP roam, or Wi-Fi came up after launch) the live
            # cert has no SAN for it, so advertising the HTTPS URL would hand the
            # phone a cert-mismatch error. Only advertise when the running leaf
            # actually covers this IP; otherwise flag that a restart re-issues it.
            if localcert.leaf_covers(lan_ip):
                urls.append({"label": "Wi-Fi (HTTPS)",
                             "url": _remote_url(lan_ip, int(https_port), token, scheme="https")})
                # The one-time trust download rides plain HTTP (the phone has no
                # trusted cert yet); Safari shows one "not secure" interstitial.
                ca_url = "http://%s:%d/api/remote/ca" % (lan_ip, port)
                ca_fingerprint = localcert.ca_fingerprint()
            else:
                https_stale = True
        serve_url = _tailscale_serve_url(port)
        if serve_url:
            urls.append({"label": "Tailscale (HTTPS)",
                         "url": "%s/?token=%s" % (serve_url, token)})
        ts_ip = _tailscale_ip()
        if ts_ip:
            urls.append({"label": "Tailscale", "url": _remote_url(ts_ip, port, token)})
        if lan_ip:
            urls.append({"label": "Wi-Fi", "url": _remote_url(lan_ip, port, token)})
        return {
            "enabled": bool(state.cfg.get("remote_access")),
            "token": token,
            "port": port,
            "https_port": int(https_port) if https_port else None,
            "ca_url": ca_url,
            "ca_fingerprint": ca_fingerprint,
            "https_stale": https_stale,
            "urls": urls,
        }

    @app.get("/api/remote/qr")
    async def api_remote_qr(label: str = None):
        """QR for a connection URL. `label` picks one of /api/remote's urls
        (e.g. "Wi-Fi" for a guest device without Tailscale); default = best."""
        state = app.state.dc
        if not state.cfg.get("remote_access"):
            return JSONResponse({"error": "remote_disabled"}, status_code=404)
        info = await api_remote()
        # label="trust" -> QR of the CA-download URL (the one-time iOS trust step).
        if label and label.strip().lower() == "trust":
            url = info.get("ca_url")
            if not url:
                return JSONResponse({"error": "no_url"}, status_code=404)
        else:
            urls = info["urls"]
            if not urls:
                return JSONResponse({"error": "no_url"}, status_code=404)
            url = urls[0]["url"]
            if label:
                want = label.strip().lower()
                for u in urls:
                    if u["label"].lower() == want:
                        url = u["url"]
                        break
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        return Response(content=buf.getvalue(), media_type="image/svg+xml")

    @app.get("/api/remote/ca")
    async def api_remote_ca():
        """Download the local CA cert so iOS can trust the LAN HTTPS listener.
        Token-exempt (see the middleware) but still gated on remote_access."""
        pem = localcert.ca_pem_bytes()
        if not pem or not app.state.dc.cfg.get("remote_access"):
            return JSONResponse({"error": "not_available"}, status_code=404)
        return Response(content=pem, media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="drivecast-ca.crt"'})

    @app.post("/api/progress")
    async def api_progress(request: Request):
        """The web player reports watch progress here so Continue Watching,
        resume and watched state stay in sync across devices."""
        state = app.state.dc
        body = await request.json()
        file_id = body.get("file_id")
        if not file_id:
            return JSONResponse({"error": "bad_request", "message": "file_id required"},
                                status_code=400)
        raw_dur = body.get("duration")
        state.history.update(
            file_id,
            name=body.get("name"),
            position=float(body.get("position") or 0.0),
            duration=float(raw_dur) if raw_dur else None,
            force=bool(body.get("ended")),
        )
        return {"ok": True}

    # ---- static assets ----
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


def _remote_url(ip, port, token, scheme="http"):
    return "%s://%s:%d/?token=%s" % (scheme, ip, port, token)


def _lan_ip():
    """Best-effort LAN IP via the UDP-connect trick. Failure-silent (None)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


# Tailscale hands out addresses from the CGNAT range 100.64.0.0/10.
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _tailscale_ip():
    """First Tailscale IPv4 (100.64.0.0/10) from the CLI, else None. Silent."""
    for cmd in (["tailscale", "ip", "-4"],
                ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"]):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        for line in (proc.stdout or "").splitlines():
            ip = line.strip()
            if not ip:
                continue
            try:
                if ipaddress.ip_address(ip) in _TAILSCALE_NET:
                    return ip
            except ValueError:
                pass
            break  # only inspect the first non-empty line
    return None


def _tailscale_serve_url(port):
    """HTTPS URL when `tailscale serve` proxies this port, else None. Silent.

    Serve terminates TLS with a real certificate for the machine's ts.net
    name — the fix for phones with HTTPS-Only Safari, which refuse the plain
    http:// IP URLs.
    """
    for cmd in (["tailscale", "serve", "status"],
                ["/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                 "serve", "status"]):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        text = proc.stdout or ""
        if (":%d" % port) not in text:
            continue  # serve is on, but proxying something else
        for word in text.split():
            if word.startswith("https://"):
                return word.rstrip("/")
    return None


def _token_page():
    """Minimal dark login page (matches the setup-page aesthetic): a GET form
    with a single password field that re-requests "/" with ?token=."""
    return """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>drivecast</title>
<style>
body{background:#0f0f13;color:#e8e8ea;font-family:-apple-system,system-ui,sans-serif;
     display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{max-width:360px;width:100%;box-sizing:border-box;padding:32px;background:#17171d;
      border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.5)}
h1{margin-top:0;color:#7aa2ff;font-size:20px}
input{width:100%;box-sizing:border-box;padding:12px;margin:12px 0;border-radius:8px;
      border:1px solid #333;background:#000;color:#e8e8ea;font-size:16px}
button{width:100%;padding:12px;border:0;border-radius:8px;background:#7aa2ff;color:#0f0f13;
       font-size:16px;font-weight:600;cursor:pointer}
</style></head>
<body><div class="card">
<h1>drivecast</h1>
<p>Enter the access token to continue.</p>
<form method="get" action="/">
<input type="password" name="token" placeholder="Access token" autofocus>
<button type="submit">Unlock</button>
</form>
</div></body></html>"""


def _setup_page(error):
    return """<!doctype html>
<html><head><meta charset="utf-8"><title>drivecast — setup</title>
<style>
body{background:#0f0f13;color:#e8e8ea;font-family:-apple-system,system-ui,sans-serif;
     display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{max-width:640px;padding:40px;background:#17171d;border-radius:16px;
      box-shadow:0 12px 40px rgba(0,0,0,.5)}
h1{margin-top:0;color:#7aa2ff} code{background:#000;padding:2px 6px;border-radius:4px}
pre{background:#000;padding:14px;border-radius:8px;overflow:auto;color:#ff9e9e}
a{color:#7aa2ff}
</style></head>
<body><div class="card">
<h1>drivecast setup needed</h1>
<p>drivecast couldn't get a Google Drive token from rclone:</p>
<pre>%s</pre>
<p>Fix it, then reload:</p>
<ol>
<li>Install rclone: <code>brew install rclone</code></li>
<li>Configure a Google Drive remote (default name <code>gdrive1</code>):
    <code>rclone config</code> &rarr; new remote &rarr; type <b>drive</b> &rarr; full access &rarr; authorise in browser.</li>
<li>Make sure the remote name matches <code>remote</code> in <code>config.json</code>.</li>
<li>Test it: <code>rclone backend drives gdrive1:</code></li>
</ol>
<p>The rclone config must not be encrypted (drivecast reads the token non-interactively).</p>
</div></body></html>""" % (error,)
