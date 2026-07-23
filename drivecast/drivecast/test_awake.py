"""Tests for KeepAwake: AC gating, the grace->prompt->idle machine, config gate.

All synthetic — subprocess.Popen is monkeypatched to a fake so nothing actually
spawns caffeinate, the AC checker is injected, and tiny phase/AC durations are
injected so timers fire fast.
"""
import asyncio
import time

import pytest

from drivecast import awake as awake_mod
from drivecast.awake import KeepAwake
from drivecast.streaming import Streamer


class FakeProc:
    """Stand-in for a caffeinate Popen: tracks terminate() and stays alive."""

    _next_pid = 1000

    def __init__(self):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.terminated = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False


@pytest.fixture
def spawned(monkeypatch):
    """Patch Popen to record every FakeProc handed out, and make the caffeinate
    binary path always 'exist' so _ensure_proc_locked proceeds."""
    procs = []

    def _fake_popen(args):
        assert args == [awake_mod.CAFFEINATE, "-i", "-s"]
        p = FakeProc()
        procs.append(p)
        return p

    monkeypatch.setattr(awake_mod.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(awake_mod.os.path, "exists", lambda path: True)
    return procs


def _wait(cond, timeout=1.5):
    """Poll until cond() is true or timeout — timers/threads run off-thread."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


# ---- AC gating ----

def test_acquire_spawns_only_on_ac(spawned):
    ka = KeepAwake(grace=0.05, prompt=0.05, ac_interval=0.02, ac_check=lambda: True)
    ka.acquire()
    assert len(spawned) == 1
    assert ka.status()["holding"] is True
    ka.shutdown()


def test_acquire_noop_on_battery(spawned):
    ka = KeepAwake(grace=0.05, prompt=0.05, ac_interval=0.02, ac_check=lambda: False)
    ka.acquire()
    assert spawned == []
    st = ka.status()
    assert st["phase"] == "active" and st["holding"] is False and st["active"] == 1
    ka.shutdown()


def test_battery_mid_hold_kills_and_ac_return_respawns(spawned):
    # On AC at acquire -> spawn. Flip to battery -> the AC monitor kills the
    # child. Flip back to AC while still held -> it respawns automatically.
    power = {"ac": True}
    ka = KeepAwake(grace=60.0, prompt=1.0, ac_interval=0.02,
                   ac_check=lambda: power["ac"])
    ka.acquire()
    assert len(spawned) == 1
    proc1 = spawned[0]
    power["ac"] = False
    assert _wait(lambda: proc1.terminated is True)
    assert ka.status()["holding"] is False
    power["ac"] = True
    assert _wait(lambda: len(spawned) == 2)
    assert ka.status()["holding"] is True
    ka.shutdown()


def test_pmset_parsing_detects_ac(monkeypatch):
    # The default checker shells `pmset -g batt` and looks for "AC Power".
    monkeypatch.setattr(awake_mod.os.path, "exists", lambda path: True)

    class _Out:
        def __init__(self, text):
            self.stdout = text

    monkeypatch.setattr(awake_mod.subprocess, "run",
                        lambda *a, **k: _Out("Now drawing from 'AC Power'\n"))
    ka = KeepAwake()
    assert ka._on_ac() is True
    monkeypatch.setattr(awake_mod.subprocess, "run",
                        lambda *a, **k: _Out("Now drawing from 'Battery Power'\n"))
    assert ka._on_ac() is False


def test_pmset_missing_is_battery(monkeypatch):
    monkeypatch.setattr(awake_mod.os.path, "exists", lambda path: False)
    ka = KeepAwake()
    assert ka._on_ac() is False   # no pmset -> treat as battery


# ---- grace -> prompt -> idle machine ----

def test_grace_prompt_idle_progression(spawned):
    ka = KeepAwake(grace=0.08, prompt=0.08, ac_interval=1.0, ac_check=lambda: True)
    ka.acquire()
    assert ka.status()["phase"] == "active"
    ka.release()                 # 1 -> 0: enter grace, still held
    assert ka.status()["phase"] == "grace"
    assert spawned[0].terminated is False
    assert _wait(lambda: ka.status()["phase"] == "prompt")
    assert spawned[0].terminated is False   # still held during the prompt
    assert _wait(lambda: ka.status()["phase"] == "idle")
    assert spawned[0].terminated is True    # released at idle
    ka.shutdown()


def test_new_stream_during_grace_returns_to_active(spawned):
    ka = KeepAwake(grace=0.2, prompt=0.2, ac_interval=1.0, ac_check=lambda: True)
    ka.acquire()
    ka.release()                 # grace
    time.sleep(0.05)
    ka.acquire()                 # a new stream cancels the countdown
    assert ka.status()["phase"] == "active"
    assert len(spawned) == 1     # reused the live process
    time.sleep(0.3)              # past the original grace+prompt deadlines
    assert spawned[0].terminated is False
    ka.shutdown()


def test_nested_acquire_does_not_respawn_or_kill(spawned):
    ka = KeepAwake(grace=0.05, prompt=0.05, ac_interval=1.0, ac_check=lambda: True)
    ka.acquire()                 # 0 -> 1: spawn
    ka.acquire()                 # 1 -> 2: nothing new
    ka.release()                 # 2 -> 1: still streaming, no grace
    assert ka.status()["phase"] == "active"
    assert len(spawned) == 1
    time.sleep(0.15)
    assert spawned[0].terminated is False
    ka.shutdown()


def test_extend_resets_grace(spawned):
    ka = KeepAwake(grace=0.3, prompt=0.1, ac_interval=1.0, ac_check=lambda: True)
    ka.acquire()
    ka.release()                 # grace deadline ~ +0.3
    time.sleep(0.15)
    st = ka.extend()             # fresh grace, deadline ~ +0.3 from now
    assert st["phase"] == "grace"
    time.sleep(0.25)             # past the ORIGINAL 0.3 deadline
    assert ka.status()["phase"] == "grace"   # extend kept it alive
    assert spawned[0].terminated is False
    ka.shutdown()


def test_extend_noop_when_active_or_idle(spawned):
    ka = KeepAwake(grace=0.05, prompt=0.05, ac_interval=1.0, ac_check=lambda: True)
    ka.acquire()
    assert ka.extend()["phase"] == "active"   # no-op while active
    ka.release()
    assert _wait(lambda: ka.status()["phase"] == "idle")
    assert ka.extend()["phase"] == "idle"     # no-op while idle
    ka.shutdown()


def test_release_now_goes_idle(spawned):
    ka = KeepAwake(grace=60.0, prompt=60.0, ac_interval=1.0, ac_check=lambda: True)
    ka.acquire()
    ka.release()                 # grace (long)
    assert ka.status()["phase"] == "grace"
    st = ka.release_now()        # user said No
    assert st["phase"] == "idle"
    assert spawned[0].terminated is True
    ka.shutdown()


def test_status_shape(spawned):
    ka = KeepAwake(grace=0.2, prompt=0.2, ac_interval=1.0, ac_check=lambda: True)
    idle = ka.status()
    assert set(idle) == {"active", "holding", "phase", "seconds_left"}
    assert idle == {"active": 0, "holding": False, "phase": "idle", "seconds_left": None}
    ka.acquire()
    active = ka.status()
    assert active["active"] == 1 and active["holding"] is True
    assert active["phase"] == "active" and active["seconds_left"] is None
    ka.release()
    grace = ka.status()
    assert grace["phase"] == "grace"
    assert isinstance(grace["seconds_left"], float) and 0 < grace["seconds_left"] <= 0.2
    ka.shutdown()


# ---- shutdown / config gate / missing binaries ----

def test_shutdown_kills_immediately(spawned):
    ka = KeepAwake(grace=60.0, prompt=60.0, ac_interval=1.0, ac_check=lambda: True)
    ka.acquire()
    ka.shutdown()
    assert spawned[0].terminated is True
    assert ka.status()["phase"] == "idle"


def test_disabled_never_spawns(spawned):
    ka = KeepAwake(enabled=lambda: False, grace=0.05, prompt=0.05, ac_check=lambda: True)
    ka.acquire()
    ka.acquire()
    assert spawned == []
    ka.release()                 # no crash
    assert ka.status()["phase"] == "idle"


def test_release_when_disabled_cleans_up_running_proc(spawned):
    enabled = {"on": True}
    ka = KeepAwake(enabled=lambda: enabled["on"], grace=60.0, prompt=60.0,
                   ac_interval=1.0, ac_check=lambda: True)
    ka.acquire()
    proc = spawned[0]
    enabled["on"] = False
    ka.release()                 # toggled off mid-stream -> drop at once
    assert proc.terminated is True
    assert ka.status()["phase"] == "idle"


def test_missing_caffeinate_is_noop(monkeypatch):
    monkeypatch.setattr(awake_mod.os.path, "exists", lambda path: False)
    calls = []
    monkeypatch.setattr(awake_mod.subprocess, "Popen",
                        lambda args: calls.append(args))
    ka = KeepAwake(grace=0.05, prompt=0.05, ac_check=lambda: True)
    ka.acquire()
    ka.acquire()
    assert calls == []
    assert ka.status()["holding"] is False
    ka.shutdown()   # no crash with no proc


# ---- the Streamer body actually holds a reference while relaying ----

class _RecordingKeepAwake:
    """Records acquire/release ordering without touching subprocess."""

    def __init__(self):
        self.events = []

    def acquire(self):
        self.events.append("acquire")

    def release(self):
        self.events.append("release")


class _FakeUpstream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.status_code = 206
        self.headers = {"content-length": "3"}
        self.closed = False

    async def aiter_raw(self, _chunk_size):
        for c in self._chunks:
            yield c

    async def aclose(self):
        self.closed = True


class _FakeRequest:
    headers = {}


def test_streamer_body_acquires_and_releases(monkeypatch):
    ka = _RecordingKeepAwake()
    streamer = Streamer(token_manager=None, drive_api=None, keepawake=ka)
    upstream = _FakeUpstream([b"abc", b"def"])

    async def _fake_open(file_id, range_header):
        return upstream

    monkeypatch.setattr(streamer, "_open_upstream", _fake_open)

    async def _drive():
        resp = await streamer.stream("fileX", _FakeRequest())
        # Draining the StreamingResponse body runs the generator start -> finally.
        return [chunk async for chunk in resp.body_iterator]

    chunks = asyncio.run(_drive())
    assert chunks == [b"abc", b"def"]
    # Acquired once at the top of the body, released once in the finally.
    assert ka.events == ["acquire", "release"]
    assert upstream.closed is True
