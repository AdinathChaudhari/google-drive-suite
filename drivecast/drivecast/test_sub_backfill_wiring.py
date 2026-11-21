"""Server-side wiring for the subtitle sweep -> coverage -> backfill chain.

Why this file exists: the queue module (`test_subtitle_queue.py`) and the
index (`test_sub_index.py`) were both well covered, but the AppState glue
that actually connects them to production had NO tests at all. An
architecture review found that all of these mutations survived the full
suite: making `_uncovered_file_ids` return `[]` (the backfill never has
work), deleting the sweep-done callback (the automatic backfill never
fires), and dropping the tmdb/season extraction that feeds OpenSubtitles'
precise match. The feature's entry points could have been deleted with the
suite green.

Everything here is hermetic: `state.library` is a plain stub (the same
"pass plain data, not the Library object" convention `classify_priorities`
is built around), `state.subtitles.resolve` is a stub coroutine, and no
test touches the network, ffprobe, or the developer's data.
"""
import asyncio

import pytest

from drivecast import config as config_mod
from drivecast import server as server_mod
from drivecast import sub_sweep as sub_sweep_mod


class _FakeScanner:
    """Enough Scanner for AppState to construct and 'scan' instantly."""

    def __init__(self, *a, **kw):
        self.status = {"running": False, "total": 0, "done": 0}

    async def scan(self, *a, **kw):
        return None


class _FakeLibrary:
    """titles_list/file_info/title_for_file over plain records."""

    def __init__(self, titles=None, files=None):
        self._titles = list(titles or [])
        self._files = dict(files or {})

    def titles_list(self):
        return list(self._titles)

    def file_info(self, file_id):
        return self._files.get(file_id)

    def title_for_file(self, file_id):
        for rec in self._titles:
            if rec.get("file_id") == file_id:
                return rec
            for season in rec.get("seasons") or []:
                for ep in season.get("episodes") or []:
                    if ep.get("file_id") == file_id:
                        return rec
        return None

    def is_empty(self):
        return not self._titles


MOVIE = {"id": "t1", "type": "movie", "title": "Skyharbor",
         "file_id": "movie1", "size": 111, "tmdb_id": 8801}
SHOW = {"id": "t2", "type": "show", "title": "Deep Field", "tmdb_id": 4402,
        "seasons": [{"season": 2, "episodes": [
            {"file_id": "ep1", "episode": 3, "name": "Deep Field S02E03.mkv",
             "size": 222}]}]}


def _state(monkeypatch, cfg_overrides=None, titles=None, files=None):
    monkeypatch.setattr(server_mod, "Scanner", _FakeScanner)
    cfg = dict(config_mod.DEFAULTS)
    cfg["selected_drives"] = ["drv1"]
    cfg.update(cfg_overrides or {})
    state = server_mod.AppState(cfg)
    state.library = _FakeLibrary(
        titles if titles is not None else [MOVIE, SHOW],
        files if files is not None else {
            "movie1": {"name": "Skyharbor.mkv", "size": 111,
                       "drive_id": "drv1", "parent_id": "p1"},
            "ep1": {"name": "Deep Field S02E03.mkv", "size": 222,
                    "drive_id": "drv1", "parent_id": "p2"}})
    return state


def _text_en_record(size):
    return {"ok": True, "size": size, "has_text_en": True, "has_image_en": False,
            "tracks": [{"index": 2, "codec": "subrip", "kind": "text",
                        "lang": "eng", "title": "English", "forced": False,
                        "hearing_impaired": False}]}


def _no_subs_record(size):
    return {"ok": True, "size": size, "has_text_en": False,
            "has_image_en": False, "tracks": []}


# ---------------------------------------------------------------------------
# The sweep -> backfill handoff
# ---------------------------------------------------------------------------

def test_sweep_completion_fires_backfill_exactly_once(monkeypatch):
    """The only automatic trigger in the whole chain. Deleting the
    sweep-done callback used to leave the suite green while the automatic
    backfill silently never ran again."""
    state = _state(monkeypatch)
    calls = []
    monkeypatch.setattr(state, "start_backfill", lambda *a, **kw: calls.append(1))

    async def _drive():
        # Nothing to probe (empty id list) -- the sweep completes immediately.
        assert state.start_sweep([]) is True
        await state._sweep_task
        await asyncio.sleep(0)   # done_callbacks fire via call_soon

    asyncio.run(_drive())
    assert calls == [1]


def test_sweep_yields_the_uplink_to_an_active_stream(monkeypatch):
    """A first full sweep runs ~45 min of 6-way ffprobe through the same
    /stream proxy playback reads from. While something is actually being
    watched it drops to serial instead of contending with the stream."""
    state = _state(monkeypatch)
    monkeypatch.setattr(state, "start_backfill", lambda *a, **kw: None)
    import time as _time
    state.stream_activity["movie1"] = _time.time()

    async def _drive():
        state.start_sweep([])
        await state._sweep_task

    asyncio.run(_drive())
    assert state.sub_sweep.concurrency == sub_sweep_mod.STREAMING_CONCURRENCY

    # Nothing streaming for a long while: back to full concurrency.
    state2 = _state(monkeypatch)
    monkeypatch.setattr(state2, "start_backfill", lambda *a, **kw: None)
    state2.stream_activity["movie1"] = _time.time() - (
        sub_sweep_mod.STREAM_HOT_SECONDS + 60)

    async def _drive2():
        state2.start_sweep([])
        await state2._sweep_task

    asyncio.run(_drive2())
    assert state2.sub_sweep.concurrency == sub_sweep_mod.DEFAULT_CONCURRENCY


# ---------------------------------------------------------------------------
# What counts as "uncovered"
# ---------------------------------------------------------------------------

def test_uncovered_excludes_records_with_an_english_text_track(monkeypatch):
    state = _state(monkeypatch)
    state.sub_index.put("movie1", _text_en_record(111))
    state.sub_index.put("ep1", _no_subs_record(222))

    assert state._uncovered_file_ids() == ["ep1"]


def test_uncovered_excludes_files_that_already_have_a_sidecar(monkeypatch, tmp_path):
    """The blocker that made the queue stop making progress.

    A backfilled file never becomes covered in sub_index -- a fetched .srt
    cannot set has_text_en, only a re-probe of the video could -- so judging
    coverage on the index alone put every completed file straight back into
    the worklist. It was then re-attempted, instantly satisfied from cache,
    and still charged one of five daily slots: over weeks the budget goes on
    re-confirming finished work rather than on new files.
    """
    state = _state(monkeypatch)
    subs = tmp_path / "subs"
    subs.mkdir()
    state.subtitles.subs_dir = str(subs)
    # Neither file has an embedded track; one has a fetched sidecar.
    state.sub_index.put("movie1", _no_subs_record(111))
    state.sub_index.put("ep1", _no_subs_record(222))
    (subs / "movie1.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")

    assert state._uncovered_file_ids() == ["ep1"]


def test_no_worklist_before_the_first_sweep(monkeypatch):
    """With an empty index every file reads as uncovered. Building a
    worklist from that meant a 2,225-entry pending list persisted on the
    first Settings visit, a panel reporting "0 of 2225 have subtitles", and
    a Fetch-now button aimed at five files that are ~90% likely to already
    carry an English track."""
    state = _state(monkeypatch)
    assert state.swept() is False

    state._refresh_backfill_worklist()
    assert state.subtitle_queue.data["pending"] == []

    # One probe result is enough to make the numbers real.
    state.sub_index.put("movie1", _no_subs_record(111))
    assert state.swept() is True
    state._refresh_backfill_worklist()
    assert {it["file_id"] for it in state.subtitle_queue.data["pending"]} == {
        "movie1", "ep1"}


# ---------------------------------------------------------------------------
# What one backfill attempt reports back
# ---------------------------------------------------------------------------

def _stub_resolve(state, path=None, downloads=0, remaining=None, capture=None):
    async def _resolve(file_id, name, drive_id=None, parent_id=None, **kw):
        if capture is not None:
            capture.update(kw)
            capture["file_id"] = file_id
            capture["name"] = name
        state.subtitles.os_download_calls += downloads
        state.subtitles.last_os_remaining = remaining
        return path
    state.subtitles.resolve = _resolve


def test_backfill_attempt_reports_a_free_hit_as_unspent(monkeypatch):
    """resolve() short-circuits on a cached file and tries the free Drive
    sibling before OpenSubtitles, so a queued item routinely completes with
    zero provider calls. The queue charges its budget from this flag."""
    state = _state(monkeypatch)
    _stub_resolve(state, path="/tmp/movie1.srt", downloads=0)

    outcome = asyncio.run(state._backfill_attempt({"file_id": "movie1"}))
    assert outcome["ok"] is True
    assert outcome["spent"] is False


def test_backfill_attempt_reports_a_real_download_as_spent(monkeypatch):
    state = _state(monkeypatch)
    _stub_resolve(state, path="/tmp/movie1.srt", downloads=1, remaining=4)

    outcome = asyncio.run(state._backfill_attempt({"file_id": "movie1"}))
    assert outcome["ok"] is True
    assert outcome["spent"] is True
    assert outcome["remaining"] == 4


def test_backfill_attempt_passes_the_precise_match_hints(monkeypatch):
    """size feeds the OSDb moviehash search and tmdb_id+season+episode the
    next fallback. Dropping either silently downgrades every lookup to the
    weak title-text query -- with no error and no failing test."""
    state = _state(monkeypatch)
    seen = {}
    _stub_resolve(state, path=None, capture=seen)

    asyncio.run(state._backfill_attempt({"file_id": "ep1"}))
    assert seen["size"] == 222
    assert seen["tmdb_id"] == 4402
    assert seen["season"] == 2
    assert seen["episode"] == 3


def test_backfill_blocked_skips_negative_cached_and_already_cached(monkeypatch, tmp_path):
    """The free pre-check the queue uses instead of spending a slot to
    rediscover a dead end."""
    state = _state(monkeypatch)
    subs = tmp_path / "subs"
    subs.mkdir()
    state.subtitles.subs_dir = str(subs)

    assert state._backfill_blocked("movie1") is False

    state.subtitles.negative.record("movie1", "opensubtitles", "not_found")
    assert state._backfill_blocked("movie1") is True

    (subs / "ep1.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    assert state._backfill_blocked("ep1") is True


# ---------------------------------------------------------------------------
# The periodic trigger
# ---------------------------------------------------------------------------

def test_periodic_backfill_runs_a_pass(monkeypatch):
    """The five-a-day budget is a DAILY budget, and nothing scheduled a
    daily run: the only automatic trigger was a walking scan's sweep. With
    auto_refresh_on_startup defaulting False, an app left running for weeks
    spent none of its budget and the projected completion never arrived."""
    state = _state(monkeypatch, {"subtitle_backfill_interval_hours": 1})
    state.sub_index.put("movie1", _no_subs_record(111))
    calls = []
    monkeypatch.setattr(state, "start_backfill", lambda *a, **kw: calls.append(1))

    slept = []

    async def _no_sleep(seconds):
        slept.append(seconds)
        if len(slept) > 1:            # one tick, then stop the loop
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    async def _drive():
        with pytest.raises(asyncio.CancelledError):
            await state._backfill_timer()

    asyncio.run(_drive())
    assert slept[0] == 3600.0        # honoured the configured interval
    assert calls == [1]


def test_periodic_backfill_disabled_by_zero_interval(monkeypatch):
    """A zero interval must return BEFORE the loop, not enter it with a
    zero-length sleep. asyncio.sleep is stubbed to raise so that reaching
    the loop at all is an unmistakable failure -- with a real sleep(0) a
    missing guard is an infinite tight loop, which hangs the test run
    instead of failing it."""
    state = _state(monkeypatch, {"subtitle_backfill_interval_hours": 0})
    calls = []
    monkeypatch.setattr(state, "start_backfill", lambda *a, **kw: calls.append(1))

    async def _boom(seconds):
        raise AssertionError("entered the timer loop with the interval disabled")

    monkeypatch.setattr(asyncio, "sleep", _boom)

    asyncio.run(state._backfill_timer())     # returns immediately
    assert calls == []
    assert state.start_backfill_timer() is False
    assert state._backfill_timer_task is None


def test_periodic_backfill_waits_for_the_first_sweep(monkeypatch):
    """Before the sweep there is no coverage data, so there is no worklist
    -- ticking anyway would spend the day's slots on guesses."""
    state = _state(monkeypatch, {"subtitle_backfill_interval_hours": 1})
    assert state.swept() is False
    calls = []
    monkeypatch.setattr(state, "start_backfill", lambda *a, **kw: calls.append(1))

    slept = []

    async def _no_sleep(seconds):
        slept.append(seconds)
        if len(slept) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    async def _drive():
        with pytest.raises(asyncio.CancelledError):
            await state._backfill_timer()

    asyncio.run(_drive())
    assert calls == []


def test_periodic_backfill_ignores_a_pending_list_left_from_before_the_sweep(monkeypatch):
    """The probed_count guard is not redundant with the worklist gate.

    `_refresh_backfill_worklist` returning early keeps a NEW guess-list from
    being built, but it cannot un-persist one that is already on disk. This
    build's own predecessor wrote exactly that: opening Settings before the
    first sweep produced a library-sized `pending` list in
    subtitle_queue.json. Without this guard the timer would then find that
    list non-empty and spend the day's five slots working through files that
    are ~90% likely to already carry an embedded English track.
    """
    state = _state(monkeypatch, {"subtitle_backfill_interval_hours": 1})
    # A stale worklist persisted by an earlier build, with no sweep results.
    state.subtitle_queue.data["pending"] = [
        {"file_id": "movie1", "priority": 3, "reason": "other",
         "queued_at": "2026-08-20T10:00:00"},
        {"file_id": "ep1", "priority": 3, "reason": "other",
         "queued_at": "2026-08-20T10:00:01"},
    ]
    assert state.swept() is False
    calls = []
    monkeypatch.setattr(state, "start_backfill", lambda *a, **kw: calls.append(1))

    slept = []

    async def _no_sleep(seconds):
        slept.append(seconds)
        if len(slept) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    async def _drive():
        with pytest.raises(asyncio.CancelledError):
            await state._backfill_timer()

    asyncio.run(_drive())
    assert calls == []                       # the stale list is NOT worked
    # And it is still there, untouched, for the sweep to reconcile properly.
    assert len(state.subtitle_queue.data["pending"]) == 2
