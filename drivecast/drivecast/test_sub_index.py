"""Embedded-subtitle index + sweep tests (Session B) — all synthetic, no
network, no real ffprobe. `asyncio.create_subprocess_exec` is mocked at the
module the sweeper actually calls it through, the same seam used everywhere
else in this suite for "never shell out in a test"."""
import asyncio
import json

import pytest

from drivecast import config as config_mod
from drivecast import server as server_mod
from drivecast.sub_index import SubIndex
from drivecast.sub_sweep import SubtitleSweeper, classify_probe_json, is_english


def _probe_json(streams):
    return json.dumps({"streams": streams})


SUBRIP_EN = _probe_json([
    {"index": 4, "codec_name": "subrip", "codec_type": "subtitle",
     "tags": {"language": "eng", "title": "English"},
     "disposition": {"forced": 0, "hearing_impaired": 0}},
])

PGS_EN = _probe_json([
    {"index": 5, "codec_name": "hdmv_pgs_subtitle", "codec_type": "subtitle",
     "tags": {"language": "eng"},
     "disposition": {"forced": 0, "hearing_impaired": 0}},
])

FORCED_AND_HI = _probe_json([
    {"index": 2, "codec_name": "subrip", "codec_type": "subtitle",
     "tags": {"language": "eng", "title": "Forced"},
     "disposition": {"forced": 1, "hearing_impaired": 0}},
    {"index": 3, "codec_name": "subrip", "codec_type": "subtitle",
     "tags": {"language": "eng", "title": "SDH"},
     "disposition": {"forced": 0, "hearing_impaired": 1}},
])


class _FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        pass


def _fake_exec_factory(stdout_json, calls, returncode=0):
    """A drop-in for asyncio.create_subprocess_exec that records every call's
    argv and always answers with the same canned ffprobe JSON."""
    async def _fake_exec(*cmd, stdout=None, stderr=None):
        calls.append(cmd)
        return _FakeProc(stdout=stdout_json.encode("utf-8"), returncode=returncode)
    return _fake_exec


# ---------------------------------------------------------------- test 1 --

def test_probe_cache_keys_on_id_and_size(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec_factory(SUBRIP_EN, calls))
    index = SubIndex(path=str(tmp_path / "sub_index.json"))
    sweeper = SubtitleSweeper(index, port=8737)

    asyncio.run(sweeper.run([("f1", 100)]))
    assert len(calls) == 1
    assert index.get("f1")["size"] == 100
    assert index.get("f1")["has_text_en"] is True

    # Same file_id, same size, second run -> zero new subprocess calls.
    asyncio.run(sweeper.run([("f1", 100)]))
    assert len(calls) == 1

    # A different size for the same id -> probed exactly once more, and the
    # old entry is replaced (not merged/duplicated).
    asyncio.run(sweeper.run([("f1", 200)]))
    assert len(calls) == 2
    assert index.get("f1")["size"] == 200


# ---------------------------------------------------------------- test 2 --

class _FakeScanner:
    """Stands in for library.Scanner: records the scope it was asked to walk
    and finishes instantly, so AppState.start_refresh's scope-guard logic can
    be exercised without any Drive network or real scanning."""

    def __init__(self, *a, **kw):
        self.status = {"running": False, "scanned": 0, "total": 0,
                       "added": 0, "removed": 0, "error": None, "scope": []}
        self.calls = []

    async def scan(self, drives, scope=None, **kw):
        self.calls.append(list(scope) if scope is not None else None)
        self.status["running"] = False
        return self.status


def _appstate(monkeypatch, selected_drives=("drv1",)):
    monkeypatch.setattr(server_mod, "Scanner", _FakeScanner)
    cfg = dict(config_mod.DEFAULTS)
    cfg["selected_drives"] = list(selected_drives)
    return server_mod.AppState(cfg)


async def _refresh_and_settle(state, scope):
    state.start_refresh(scope=scope)
    task = state._refresh_task
    await task
    # start_refresh's done_callbacks are scheduled via call_soon, not run
    # synchronously when the task finishes -- give the loop one more tick so
    # they've actually fired before we assert on their side effects.
    await asyncio.sleep(0)


def test_sweep_not_triggered_by_cache_only_rebuild(monkeypatch):
    state = _appstate(monkeypatch)
    sweep_calls = []
    monkeypatch.setattr(state, "start_sweep",
                        lambda *a, **kw: sweep_calls.append(1))

    asyncio.run(_refresh_and_settle(state, scope=[]))

    assert sweep_calls == []


def test_sweep_triggered_after_walking_scan(monkeypatch):
    """Companion positive case: scope=None (full walk) DOES fire the sweep --
    proves the guard in test 2 is discriminating on scope, not just always
    skipping."""
    state = _appstate(monkeypatch)
    sweep_calls = []
    monkeypatch.setattr(state, "start_sweep",
                        lambda *a, **kw: sweep_calls.append(1))

    asyncio.run(_refresh_and_settle(state, scope=None))

    assert sweep_calls == [1]


# ---------------------------------------------------------------- test 3 --

def test_classifies_pgs_as_image_kind():
    tracks, has_text_en, has_image_en = classify_probe_json(PGS_EN)
    assert has_image_en is True
    assert has_text_en is False
    assert tracks[0]["kind"] == "image"

    tracks, has_text_en, has_image_en = classify_probe_json(SUBRIP_EN)
    assert has_text_en is True
    assert has_image_en is False
    assert tracks[0]["kind"] == "text"


# ---------------------------------------------------------------- test 4 --

def test_captures_forced_and_hearing_impaired():
    tracks, has_text_en, _has_image_en = classify_probe_json(FORCED_AND_HI)
    # Forced/SDH-only English is NOT coverage (matches the player predicate).
    assert has_text_en is False
    by_title = {t["title"]: t for t in tracks}
    assert by_title["Forced"]["forced"] is True
    assert by_title["Forced"]["hearing_impaired"] is False
    assert by_title["SDH"]["forced"] is False
    assert by_title["SDH"]["hearing_impaired"] is True


# ---------------------------------------------------------------- test 5 --

def test_sweep_resumes_after_kill(tmp_path):
    path = str(tmp_path / "sub_index.json")

    calls1 = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec",
                  _fake_exec_factory(SUBRIP_EN, calls1))
        # flush_every=1: every put() is flushed to disk immediately. Calling
        # _probe_and_store directly (never run()) is the point -- a real
        # SIGKILL wouldn't execute run()'s `finally: self.index.flush()`
        # either, so the ONLY thing that can be proven to survive a kill is
        # the periodic flush inside put() itself.
        index1 = SubIndex(path=path, flush_every=1)
        sweeper1 = SubtitleSweeper(index1, port=8737)
        asyncio.run(sweeper1._probe_and_store("a", 10))
        # "Killed" here: no run()/flush() call after this point.
    assert len(calls1) == 1

    # "Restart": a brand-new SubIndex reloading the same on-disk file, and a
    # brand-new SubtitleSweeper with its own call counter.
    index2 = SubIndex(path=path)
    assert index2.get("a") is not None and index2.get("a")["size"] == 10

    calls2 = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec",
                  _fake_exec_factory(SUBRIP_EN, calls2))
        sweeper2 = SubtitleSweeper(index2, port=8737)
        asyncio.run(sweeper2.run([("a", 10), ("b", 20), ("c", 30)]))

    # "a" was already probed at the same size -> skipped; only b and c cost
    # a subprocess call.
    assert len(calls2) == 2
    assert index2.get("b") is not None
    assert index2.get("c") is not None


# ---------------------------------------------------------------- test 6 --
# is_english's whole job is normalising the handful of tag spellings a real
# release uses ("eng" vs "en" vs "en-US") while still treating "und"/missing
# as NOT English (see the module docstring's playback-mismatch reasoning).
# Every other test in this file only ever feeds it "eng", so none of them
# would notice if the en/en-US branch broke -- this is that direct coverage.

def test_is_english_normalizes_tag_variants():
    assert is_english("eng") is True
    assert is_english("en") is True
    assert is_english("en-US") is True
    assert is_english("EN") is True
    assert is_english("und") is False
    assert is_english(None) is False
    assert is_english("") is False
    assert is_english("fre") is False


# ---------------------------------------------------------------- test 7 --

def test_second_sweep_does_not_start_while_running(monkeypatch):
    """AppState.start_sweep must refuse a second call while the first sweep's
    task is still in flight -- the check-then-set has to happen before the
    coroutine gets a chance to run, not after."""
    state = _appstate(monkeypatch)
    release = asyncio.Event()
    run_calls = []

    async def _blocking_run(ids):
        run_calls.append(list(ids))
        await release.wait()
        return state.sub_sweep.status

    monkeypatch.setattr(state.sub_sweep, "run", _blocking_run)

    async def main():
        first = state.start_sweep([("f1", 1)])
        # Let the first task actually start and reach `await release.wait()`
        # before trying the second call -- the guard itself is synchronous
        # (see start_sweep's docstring), but this confirms the first sweep
        # is genuinely in flight, not merely queued, when the second is
        # rejected.
        await asyncio.sleep(0)
        second = state.start_sweep([("f2", 2)])
        assert first is True
        assert second is False
        # Only the first call's ids ever reached run() -- the second call
        # never created a task at all.
        assert run_calls == [[("f1", 1)]]
        release.set()
        await state._sweep_task

    asyncio.run(main())


# ---------------------------------------------------------------- test 8 --

def test_run_finish_flush_persists_below_threshold(tmp_path):
    """A clean run() over fewer files than flush_every never crosses the
    periodic-flush threshold inside put() -- the only thing that can put
    those probes on disk is run()'s own unconditional `finally: flush()`.
    Reload from a brand-new SubIndex pointed at the same path to prove the
    write actually reached disk, not just the in-memory dict the original
    object still holds."""
    path = str(tmp_path / "sub_index.json")
    calls = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_exec",
                  _fake_exec_factory(SUBRIP_EN, calls))
        index = SubIndex(path=path)  # default flush_every=25
        sweeper = SubtitleSweeper(index, port=8737)
        asyncio.run(sweeper.run([("a", 10), ("b", 20)]))
    assert len(calls) == 2

    reloaded = SubIndex(path=path)
    assert reloaded.get("a") is not None and reloaded.get("a")["size"] == 10
    assert reloaded.get("b") is not None and reloaded.get("b")["size"] == 20


# ------------------------------------------- preflight-review follow-ups --

def test_forced_plus_normal_track_counts_as_text_en():
    both = _probe_json(json.loads(FORCED_AND_HI)["streams"]
                       + json.loads(SUBRIP_EN)["streams"])
    assert classify_probe_json(both)[1] is True


def _raising_exec(*cmd, **kw):
    async def _boom(*a, **k):
        raise FileNotFoundError("ffprobe")
    return _boom(*cmd, **kw)


def test_missing_ffprobe_stores_nothing_and_sets_error(tmp_path, monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raising_exec)
    index = SubIndex(path=str(tmp_path / "sub_index.json"))
    sweeper = SubtitleSweeper(index, port=8737)
    asyncio.run(sweeper.run([("a", 1), ("b", 2)]))
    assert index.probed_count() == 0
    assert index.get("a") is None and index.get("b") is None
    assert "ffprobe" in sweeper.status["error"]
    assert index.sweep_complete() is False
    assert sweeper.status["running"] is False


def test_failed_probe_is_retried_and_not_counted(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec_factory("", calls, returncode=1))
    index = SubIndex(path=str(tmp_path / "sub_index.json"))
    sweeper = SubtitleSweeper(index, port=8737)
    asyncio.run(sweeper.run([("a", 5)]))
    assert index.get("a")["ok"] is False
    assert index.probed_count() == 0
    assert index.needs_probe("a", 5) is True
    # Recovers on the next sweep once ffprobe answers.
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec_factory(SUBRIP_EN, calls))
    asyncio.run(sweeper.run([("a", 5)]))
    assert index.get("a")["ok"] is True
    assert index.probed_count() == 1


def test_sweep_complete_only_after_run_finishes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec_factory(SUBRIP_EN, calls))
    index = SubIndex(path=str(tmp_path / "sub_index.json"))
    sweeper = SubtitleSweeper(index, port=8737)
    asyncio.run(sweeper._probe_and_store("a", 1))
    assert index.probed_count() == 1
    assert index.sweep_complete() is False  # mid-sweep state
    asyncio.run(sweeper.run([("a", 1)]))
    assert index.sweep_complete() is True
    assert SubIndex(path=index.path).sweep_complete() is True


def test_all_probes_failed_does_not_mark_sweep_complete(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec_factory("", calls, returncode=1))
    index = SubIndex(path=str(tmp_path / "sub_index.json"))
    sweeper = SubtitleSweeper(index, port=8737)
    asyncio.run(sweeper.run([("a", 1), ("b", 2)]))
    assert index.sweep_complete() is False
    assert "failed" in sweeper.status["error"]
    # Recovers once ffprobe answers.
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec_factory(SUBRIP_EN, calls))
    asyncio.run(sweeper.run([("a", 1), ("b", 2)]))
    assert index.sweep_complete() is True


def test_minority_failures_still_mark_complete(tmp_path, monkeypatch):
    async def _exec(*cmd, stdout=None, stderr=None):
        bad = cmd[-1].endswith("/bad")
        return _FakeProc(stdout=SUBRIP_EN.encode("utf-8"),
                         returncode=1 if bad else 0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    index = SubIndex(path=str(tmp_path / "sub_index.json"))
    sweeper = SubtitleSweeper(index, port=8737)
    asyncio.run(sweeper.run([("g1", 1), ("g2", 2), ("bad", 3)]))
    assert index.get("bad")["ok"] is False
    assert index.sweep_complete() is True  # one dead file must not block


def test_transient_oserror_is_per_file_failure_not_abort(tmp_path, monkeypatch):
    def _emfile(*cmd, **kw):
        async def _boom(*a, **k):
            raise OSError(24, "Too many open files")
        return _boom(*cmd, **kw)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _emfile)
    index = SubIndex(path=str(tmp_path / "sub_index.json"))
    sweeper = SubtitleSweeper(index, port=8737)
    asyncio.run(sweeper.run([("a", 1), ("b", 2)]))
    assert sweeper._aborted is False
    assert index.get("a")["ok"] is False and index.get("b")["ok"] is False
    assert index.needs_probe("a", 1) is True


def test_flush_failure_does_not_wedge_running(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _fake_exec_factory(SUBRIP_EN, calls))
    index = SubIndex(path=str(tmp_path / "sub_index.json"))

    def _bad_flush():
        raise OSError("disk full")
    monkeypatch.setattr(index, "flush", _bad_flush)
    sweeper = SubtitleSweeper(index, port=8737)
    asyncio.run(sweeper.run([("a", 1)]))
    assert sweeper.status["running"] is False
    assert "flush" in sweeper.status["error"]


def test_filtered_empty_request_does_not_sweep(monkeypatch):
    state = _appstate(monkeypatch)
    sweep_calls = []
    monkeypatch.setattr(state, "start_sweep",
                        lambda *a, **kw: sweep_calls.append(1))

    async def go():
        # Non-empty request naming only an unselected drive filters to [].
        assert state.start_refresh(scope=["not-selected"]) is False
        await asyncio.sleep(0)
    asyncio.run(go())
    assert sweep_calls == []


def test_drained_pending_scope_escalates_to_sweep(monkeypatch):
    state = _appstate(monkeypatch)
    sweep_calls = []
    monkeypatch.setattr(state, "start_sweep",
                        lambda *a, **kw: sweep_calls.append(1))

    async def go():
        state.scanner.status["running"] = True  # scanner busy
        assert state.start_refresh(scope=["drv1"]) is False  # queued
        state.scanner.status["running"] = False
        # Drain-style cache-only request: merged with the pending scope the
        # FINAL scope is non-empty, so the sweep must fire.
        assert state.start_refresh(scope=[]) is True
        await state._refresh_task
        await asyncio.sleep(0)
    asyncio.run(go())
    assert state.scanner.calls == [["drv1"]]
    assert sweep_calls == [1]
