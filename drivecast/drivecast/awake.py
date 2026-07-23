"""Hold a macOS power assertion while streams are active — on AC power only.

The Mac often runs lid-closed in clamshell mode (external display, passthrough
charging). If it sleeps mid-stream, remote playback on a phone/TV dies. So while
any /stream response is being relayed we keep a `caffeinate` child process alive
to inhibit sleep, and drop it once nothing is streaming.

Two refinements on top of plain reference counting:

1. AC-only. We hold the assertion ONLY while the Mac is on AC / passthrough
   charging — never on battery. `caffeinate -s` claims to be AC-aware but its
   detection missed this user's passthrough-charging hub, so we gate on the
   power source ourselves via `pmset -g batt` ("AC Power" in the first line).
   Checked on every 0->1 acquire and re-checked every ~60s while held: if power
   flips to battery the caffeinate child is killed, and respawned when AC returns
   (as long as we're still in a holding phase).

2. An "Are you still watching?" grace machine instead of a silent timeout. When
   active streams drop to zero the assertion is NOT dropped immediately:
     ACTIVE  (refs > 0)          — held
       -> GRACE  (120s)          — held; UIs may quietly poll
       -> PROMPT (30s)           — held; UIs show a live countdown popup
       -> IDLE                   — released; the Mac may sleep naturally
   A new stream at any phase cancels everything and returns to ACTIVE. The web
   UI can extend() (fresh grace) or release_now() from the prompt.

`caffeinate -i -s`: `-i` inhibits idle sleep, `-s` inhibits system sleep — kept
as belt-and-braces behind our own AC gate.
"""
import logging
import os
import subprocess
import threading
import time

log = logging.getLogger("drivecast.awake")

CAFFEINATE = "/usr/bin/caffeinate"
PMSET = "/usr/bin/pmset"
GRACE_SECONDS = 120.0
PROMPT_SECONDS = 30.0
AC_INTERVAL = 60.0

# Phase names, also surfaced verbatim in /api/awake/status for any client.
ACTIVE = "active"
GRACE = "grace"
PROMPT = "prompt"
IDLE = "idle"


class KeepAwake:
    """Reference-counted, AC-gated holder for a `caffeinate` power assertion.

    Thread-safe: releases fire from stream generators on the event loop, while
    the phase timer and AC monitor run on their own threads, so all state lives
    under a single lock.
    """

    def __init__(self, enabled=None, grace=GRACE_SECONDS, prompt=PROMPT_SECONDS,
                 ac_interval=AC_INTERVAL, caffeinate=CAFFEINATE, pmset=PMSET,
                 ac_check=None):
        # `enabled` is a callable so the live config toggle is read each time,
        # not frozen at construction. Default: always on.
        self._enabled = enabled or (lambda: True)
        self._grace = grace
        self._prompt = prompt
        self._ac_interval = ac_interval
        self._caffeinate = caffeinate
        self._pmset = pmset
        # `ac_check` overrides the pmset probe (injected by tests).
        self._ac_check = ac_check

        self._count = 0
        self._phase = IDLE
        self._phase_end = None       # monotonic deadline of the current phase
        self._proc = None
        self._phase_timer = None
        self._gen = 0                # invalidates stale timer callbacks
        self._ac_thread = None
        self._ac_stop = None
        self._lock = threading.Lock()
        self._warned_no_caffeinate = False
        self._warned_no_pmset = False

    # ---- public API ----

    def acquire(self):
        """Register one active stream; enter/refresh the ACTIVE (held) phase."""
        if not self._enabled():
            return
        with self._lock:
            starting = self._phase == IDLE
            self._count += 1
            self._cancel_phase_timer_locked()
            self._phase = ACTIVE
            self._phase_end = None
            if starting:
                self._begin_hold_locked()
            else:
                # Re-acquired during grace/prompt, or a concurrent stream: make
                # sure the assertion matches the current AC state.
                self._sync_proc_locked()

    def release(self):
        """Drop one active stream; on the last one enter GRACE (still held)."""
        with self._lock:
            if self._count > 0:
                self._count -= 1
            # Toggled off mid-stream: drop the assertion at once rather than
            # waiting out a grace no one asked for.
            if not self._enabled():
                self._go_idle_locked()
                return
            if self._count == 0 and self._phase == ACTIVE:
                self._enter_grace_locked()

    def extend(self):
        """User said 'Yes, keep watching': restart a fresh GRACE. No-op unless
        we're currently in grace/prompt. Returns the new status."""
        with self._lock:
            if self._phase in (GRACE, PROMPT):
                self._enter_grace_locked()
            return self._status_locked()

    def release_now(self):
        """User said 'No': release immediately (IDLE). Only acts when nothing is
        actively streaming. Returns the new status."""
        with self._lock:
            if self._count == 0:
                self._go_idle_locked()
            return self._status_locked()

    def status(self):
        """Client-agnostic snapshot for /api/awake/status."""
        with self._lock:
            return self._status_locked()

    def shutdown(self):
        """Kill caffeinate and stop all threads (called from server shutdown)."""
        with self._lock:
            self._count = 0
            self._go_idle_locked()

    # ---- phase transitions (all called with self._lock held) ----

    def _begin_hold_locked(self):
        """Idle -> holding: spawn caffeinate (if on AC) and start AC monitoring."""
        self._sync_proc_locked()
        self._start_ac_monitor_locked()

    def _enter_grace_locked(self):
        self._phase = GRACE
        self._phase_end = time.monotonic() + self._grace
        self._sync_proc_locked()   # still held; re-affirm against AC state
        self._start_phase_timer_locked(self._grace, self._to_prompt)

    def _to_prompt(self, gen):
        with self._lock:
            if gen != self._gen or self._phase != GRACE:
                return
            self._phase = PROMPT
            self._phase_end = time.monotonic() + self._prompt
            self._start_phase_timer_locked(self._prompt, self._to_idle)

    def _to_idle(self, gen):
        with self._lock:
            if gen != self._gen or self._phase != PROMPT:
                return
            self._go_idle_locked()

    def _go_idle_locked(self):
        self._cancel_phase_timer_locked()
        self._phase = IDLE
        self._phase_end = None
        self._kill_proc_locked()
        self._stop_ac_monitor_locked()

    # ---- caffeinate process, AC-gated ----

    def _sync_proc_locked(self):
        """Reconcile the caffeinate child with (holding-phase AND on-AC)."""
        if self._phase == IDLE or not self._on_ac():
            self._kill_proc_locked()
        else:
            self._ensure_proc_locked()

    def _ensure_proc_locked(self):
        if not os.path.exists(self._caffeinate):
            if not self._warned_no_caffeinate:
                log.info("keep-awake: %s not found; disabled on this system",
                         self._caffeinate)
                self._warned_no_caffeinate = True
            return
        # Respawn if never started or the process exited on its own.
        if self._proc is None or self._proc.poll() is not None:
            try:
                self._proc = subprocess.Popen([self._caffeinate, "-i", "-s"])
                log.debug("keep-awake: caffeinate started (pid %s)", self._proc.pid)
            except OSError as exc:  # pragma: no cover - defensive
                log.warning("keep-awake: could not start caffeinate: %r", exc)
                self._proc = None

    def _kill_proc_locked(self):
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.terminate()
            except OSError:  # pragma: no cover - defensive
                pass
            self._proc = None

    def _on_ac(self):
        """True when running on AC / charging power, False on battery."""
        if self._ac_check is not None:
            return bool(self._ac_check())
        if not os.path.exists(self._pmset):
            if not self._warned_no_pmset:
                log.info("keep-awake: %s not found; treating as battery (no-op)",
                         self._pmset)
                self._warned_no_pmset = True
            return False
        try:
            out = subprocess.run([self._pmset, "-g", "batt"],
                                 capture_output=True, text=True, timeout=2.0)
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            return False
        lines = (out.stdout or "").splitlines()
        return bool(lines) and "AC Power" in lines[0]

    # ---- AC monitor thread ----

    def _start_ac_monitor_locked(self):
        if self._ac_thread is not None:
            return
        self._ac_stop = threading.Event()
        self._ac_thread = threading.Thread(
            target=self._ac_loop, args=(self._ac_stop,), daemon=True)
        self._ac_thread.start()

    def _stop_ac_monitor_locked(self):
        if self._ac_stop is not None:
            self._ac_stop.set()
        self._ac_thread = None
        self._ac_stop = None

    def _ac_loop(self, stop_event):
        while not stop_event.wait(self._ac_interval):
            with self._lock:
                if stop_event.is_set() or self._phase == IDLE:
                    return
                self._sync_proc_locked()

    # ---- phase timer helpers ----

    def _start_phase_timer_locked(self, seconds, callback):
        self._cancel_phase_timer_locked()   # bumps _gen
        gen = self._gen
        self._phase_timer = threading.Timer(seconds, callback, args=(gen,))
        self._phase_timer.daemon = True
        self._phase_timer.start()

    def _cancel_phase_timer_locked(self):
        if self._phase_timer is not None:
            self._phase_timer.cancel()
            self._phase_timer = None
        # Any pending (already-fired, lock-waiting) callback is now stale.
        self._gen += 1

    # ---- status ----

    def _status_locked(self):
        seconds_left = None
        if self._phase in (GRACE, PROMPT) and self._phase_end is not None:
            seconds_left = max(0.0, self._phase_end - time.monotonic())
        holding = self._proc is not None and self._proc.poll() is None
        return {
            "active": self._count,
            "holding": holding,
            "phase": self._phase,
            "seconds_left": seconds_left,
        }
