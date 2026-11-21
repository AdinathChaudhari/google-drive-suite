"""subtitle_queue.py tests (Session D) -- hermetic, no network, no real
OpenSubtitles calls. `attempt` is always a stub coroutine so `run()` can be
asserted on call counts, the same "spy and count calls" convention
test_subtitles.py already uses for browse_calls."""
import asyncio

from drivecast.subtitle_queue import (
    PARK_AFTER_ATTEMPTS,
    PRIORITY_CONTINUE_WATCHING,
    PRIORITY_NEXT_EPISODE,
    PRIORITY_OTHER,
    PRIORITY_UNWATCHED_MOVIE,
    SubtitleQueue,
    classify_priorities,
)


def _queue(tmp_path, **kw):
    return SubtitleQueue(path=str(tmp_path / "subtitle_queue.json"), **kw)


def _run(coro):
    return asyncio.run(coro)


# ---- test 4: day boundary resets spent (and failed_today), pending survives ----

def test_queue_day_boundary_resets_spent(tmp_path):
    path = tmp_path / "subtitle_queue.json"
    q = _queue(tmp_path, today="2026-08-22")
    q.data["pending"] = [{"file_id": "f1", "priority": PRIORITY_OTHER,
                          "reason": "other", "queued_at": "2026-08-20T10:00:00"}]
    q.record_spend("f2", ok=False)  # spends one slot, adds f2 to failed_today
    assert q.spent() == 1
    assert q.data["failed_today"] == ["f2"]

    # Re-open on the SAME day: nothing resets.
    same_day = SubtitleQueue(path=str(path), today="2026-08-22")
    assert same_day.spent() == 1
    assert same_day.data["failed_today"] == ["f2"]
    assert len(same_day.data["pending"]) == 1

    # Re-open on the NEXT day: spent and failed_today reset, pending untouched.
    next_day = SubtitleQueue(path=str(path), today="2026-08-23")
    assert next_day.spent() == 0
    assert next_day.data["failed_today"] == []
    assert next_day.data["date"] == "2026-08-23"
    assert next_day.data["pending"] == [
        {"file_id": "f1", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-20T10:00:00"}
    ]


# ---- test 5: stops at daily budget, zero network calls, nothing dropped ----

def test_queue_stops_at_daily_budget(tmp_path):
    q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
    q.data["pending"] = [
        {"file_id": "f1", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:00:00"},
        {"file_id": "f2", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:01:00"},
    ]
    # Already at budget for today (5 spent, no hint override).
    q.data["spent"]["opensubtitles"] = 5
    assert q.remaining() == 0

    calls = []

    async def attempt(item):
        calls.append(item["file_id"])
        return {"ok": True}

    result = _run(q.run(attempt))

    assert calls == []                      # zero network calls
    assert result["worked"] == 0
    fids = {it["file_id"] for it in q.data["pending"]}
    assert fids == {"f1", "f2"}              # both stay in pending, nothing dropped


def test_queue_mid_run_budget_stop_leaves_remainder_pending(tmp_path):
    """A provider that reports remaining=0 mid-run stops the loop; the item
    that would have gone over budget (and everything after it) stays
    pending -- proves the "never silently dropped" half of test 5 for the
    dynamic, not just the pre-computed, budget case."""
    q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
    q.data["pending"] = [
        {"file_id": "f1", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:00:00"},
        {"file_id": "f2", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:01:00"},
    ]

    async def attempt(item):
        # First attempt reports the provider is now out of quota.
        return {"ok": True, "remaining": 0}

    result = _run(q.run(attempt))

    assert result["worked"] == 1
    assert [it["file_id"] for it in q.data["pending"]] == ["f2"]


# ---- a failed fetch must not spend the same slot twice ----

def test_queue_failed_fetch_not_retried_same_day(tmp_path):
    q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
    q.data["pending"] = [
        {"file_id": "f1", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:00:00"},
    ]
    calls = []

    async def attempt(item):
        calls.append(item["file_id"])
        return {"ok": False}

    _run(q.run(attempt))
    assert calls == ["f1"]
    assert q.spent() == 1
    assert "f1" in q.data["failed_today"]
    # f1 is still pending (not dropped) but a second run today must not
    # call attempt on it again.
    assert any(it["file_id"] == "f1" for it in q.data["pending"])

    _run(q.run(attempt))
    assert calls == ["f1"]                  # unchanged -- no second attempt
    assert q.spent() == 1                   # no second spend


# ---- priority ordering: tier first, FIFO by queued_at within a tier ----

def test_priority_ordering_tier_then_fifo(tmp_path):
    q = _queue(tmp_path, today="2026-08-22")
    priorities = {
        "other-early": (PRIORITY_OTHER, "other"),
        "movie": (PRIORITY_UNWATCHED_MOVIE, "unwatched_movie"),
        "continue": (PRIORITY_CONTINUE_WATCHING, "continue_watching"),
        "episode": (PRIORITY_NEXT_EPISODE, "next_episode"),
    }
    # Enqueue out of tier order, and out of alphabetical order too, so a test
    # pass can't be an accident of insertion or sort-key coincidence.
    q.sync_pending({"other-early": priorities["other-early"]}, now="2026-08-22T08:00:00")
    q.sync_pending({"other-early": priorities["other-early"],
                    "movie": priorities["movie"]}, now="2026-08-22T09:00:00")
    q.sync_pending({"other-early": priorities["other-early"], "movie": priorities["movie"],
                    "continue": priorities["continue"]}, now="2026-08-22T10:00:00")
    q.sync_pending(priorities, now="2026-08-22T11:00:00")

    ordered = [it["file_id"] for it in q._ordered_pending()]
    assert ordered == ["continue", "movie", "episode", "other-early"]

    # FIFO within a tier: two same-tier items keep the order they FIRST
    # joined `pending` in, each stamped with `now` at the moment it was
    # added, not by the order arguments happen to be passed in later.
    q2 = SubtitleQueue(path=str(tmp_path / "fifo_check.json"), today="2026-08-22")
    q2.sync_pending({"first": (PRIORITY_OTHER, "other")}, now="2026-08-22T08:00:00")
    q2.sync_pending({"first": (PRIORITY_OTHER, "other"),
                     "second": (PRIORITY_OTHER, "other")}, now="2026-08-22T09:00:00")
    ordered2 = [it["file_id"] for it in q2._ordered_pending()]
    assert ordered2 == ["first", "second"]

    # Re-syncing an EXISTING pending item with a later `now` must not bump
    # its place in line -- queued_at is stamped once, at first insertion.
    q2.sync_pending({"first": (PRIORITY_OTHER, "other"),
                     "second": (PRIORITY_OTHER, "other")}, now="2026-08-22T23:59:59")
    assert [it["file_id"] for it in q2._ordered_pending()] == ["first", "second"]


# ---- classify_priorities: pure tiering from plain data ----

def test_classify_priorities_tiers_and_fallback():
    titles = [
        {"type": "movie", "file_id": "m-continue"},
        {"type": "movie", "file_id": "m-unwatched"},
        {"type": "movie", "file_id": "m-watched"},
        {"type": "show", "seasons": [
            {"season": 1, "episodes": [
                {"file_id": "s1e1"}, {"file_id": "s1e2"}, {"file_id": "s1e3"},
            ]},
        ]},
        {"type": "show", "seasons": [
            {"season": 1, "episodes": [{"file_id": "untouched-show-ep"}]},
        ]},
    ]
    uncovered = {"m-continue", "m-unwatched", "m-watched", "s1e2", "s1e3",
                "untouched-show-ep"}
    continue_watching_ids = {"m-continue"}
    watched_map = {
        "m-watched": {"percent": 100.0, "watched": True},
        "s1e1": {"percent": 100.0, "watched": True},   # show is "in progress"
        # s1e2, s1e3: no entry -> unwatched
    }

    result = classify_priorities(titles, uncovered, continue_watching_ids, watched_map)

    assert result["m-continue"] == (PRIORITY_CONTINUE_WATCHING, "continue_watching")
    assert result["m-unwatched"] == (PRIORITY_UNWATCHED_MOVIE, "unwatched_movie")
    assert result["m-watched"] == (PRIORITY_OTHER, "other")  # already watched movie
    assert result["s1e2"] == (PRIORITY_NEXT_EPISODE, "next_episode")  # first unwatched
    assert result["s1e3"] == (PRIORITY_OTHER, "other")       # not THE next episode
    # A show nobody has ever pressed play on is not "in progress".
    assert result["untouched-show-ep"] == (PRIORITY_OTHER, "other")


# ---- manager pass: the two accounting blockers the advisor found ----
#
# Both of these were found by review, not by a failing test, and both make the
# queue stop making net progress after roughly its first week -- so each gets a
# test that fails if the fix is reverted.

def test_queue_free_hit_does_not_spend_a_slot(tmp_path):
    """A successful attempt that never reached the provider must not be
    charged to the daily budget.

    This is the queue half of the "asymptotically stops" bug. A backfilled
    file never becomes covered in sub_index (a fetched .srt cannot set
    has_text_en), so it comes straight back into the worklist, gets
    re-attempted, and resolve() satisfies it instantly from cache with zero
    network -- which used to burn one of five daily slots. Over weeks the
    budget goes almost entirely on re-confirming finished work.
    """
    q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
    q.data["pending"] = [
        {"file_id": "cached", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:00:00"},
        {"file_id": "real", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:01:00"},
    ]

    async def attempt(item):
        # "cached" completes without touching OpenSubtitles; "real" pays.
        if item["file_id"] == "cached":
            return {"ok": True, "spent": False}
        return {"ok": True, "spent": True, "remaining": 4}

    result = _run(q.run(attempt))

    assert result["ok"] == 2                 # both succeeded
    assert q.spent() == 1                    # only the real download was charged
    assert q.data["pending"] == []            # both left the queue


def test_queue_free_failure_does_not_spend_a_slot(tmp_path):
    """The same rule for a failure: an attempt that never reached the
    provider is not a spent slot either. A file inside its negative-cache
    TTL makes resolve() return None with no network call at all, and
    charging that was the other half of the wasted budget."""
    q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
    q.data["pending"] = [{"file_id": "f1", "priority": PRIORITY_OTHER,
                          "reason": "other", "queued_at": "2026-08-22T09:00:00"}]

    async def attempt(item):
        return {"ok": False, "spent": False}

    _run(q.run(attempt))

    assert q.spent() == 0
    assert q.data["failed_today"] == ["f1"]  # still not retried again today
    assert [it["file_id"] for it in q.data["pending"]] == ["f1"]
    # A free failure is not evidence the subtitle is missing, so it must not
    # count toward parking.
    assert int(q.data["pending"][0].get("attempts") or 0) == 0


def test_queue_missing_spent_flag_still_charges(tmp_path):
    """Back-compat: an `attempt` that reports no `spent` key is read as
    "the provider was called". A caller that cannot tell must not get free
    downloads -- over-charging is recoverable, silently blowing through a
    five-a-day provider quota is not."""
    q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
    q.data["pending"] = [{"file_id": "f1", "priority": PRIORITY_OTHER,
                          "reason": "other", "queued_at": "2026-08-22T09:00:00"}]

    async def attempt(item):
        return {"ok": True}

    _run(q.run(attempt))
    assert q.spent() == 1


def test_queue_skips_blocked_items_without_spending(tmp_path):
    """Head-of-line stall, part one. An item whose provider is still inside
    its negative-cache TTL is skipped FREE, and the run moves on to the item
    behind it -- which used to be unreachable, because the blocked item was
    attempted, instantly failed with no network call, and still consumed a
    whole slot."""
    q = _queue(tmp_path, daily_limit=1, today="2026-08-22")
    q.data["pending"] = [
        {"file_id": "dead", "priority": PRIORITY_CONTINUE_WATCHING,
         "reason": "continue_watching", "queued_at": "2026-08-01T09:00:00"},
        {"file_id": "fresh", "priority": PRIORITY_CONTINUE_WATCHING,
         "reason": "continue_watching", "queued_at": "2026-08-22T09:00:00"},
    ]
    calls = []

    async def attempt(item):
        calls.append(item["file_id"])
        return {"ok": True, "spent": True}

    result = _run(q.run(attempt, is_blocked=lambda fid: fid == "dead"))

    assert calls == ["fresh"]                # the blocked head never attempted
    assert q.spent() == 1                    # the one slot went to real work
    assert result["skipped"] == 1
    # "dead" is never dropped -- its TTL lapses and it comes back.
    assert [it["file_id"] for it in q.data["pending"]] == ["dead"]


def test_queue_parks_item_after_repeated_real_misses(tmp_path):
    """Head-of-line stall, part two. An item that has really missed
    PARK_AFTER_ATTEMPTS times is not retried every day; it waits
    PARK_RETRY_DAYS. Without this, the same handful of files OpenSubtitles
    genuinely lacks sit at the head of the queue forever, alternating
    real-miss days and negative-cache days, and nothing behind them is ever
    attempted."""
    q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
    q.data["pending"] = [
        {"file_id": "missing", "priority": PRIORITY_CONTINUE_WATCHING,
         "reason": "continue_watching", "queued_at": "2026-08-01T09:00:00",
         "attempts": PARK_AFTER_ATTEMPTS, "last_attempt": "2026-08-20"},
        {"file_id": "behind", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-02T09:00:00"},
    ]
    calls = []

    async def attempt(item):
        calls.append(item["file_id"])
        return {"ok": False, "spent": True}

    _run(q.run(attempt))
    # Two days after its third miss: parked, so the item behind it gets the slot.
    assert calls == ["behind"]

    # PARK_RETRY_DAYS later it is eligible again -- parking defers, never retires.
    later = SubtitleQueue(path=str(tmp_path / "subtitle_queue.json"),
                          daily_limit=5, today="2026-09-01")
    assert "missing" in {it["file_id"] for it in later._ordered_pending()}


def test_queue_counts_only_real_misses_toward_parking(tmp_path):
    """A real provider miss increments the item's attempt count (and stamps
    the day), which is what parking is measured in."""
    q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
    q.data["pending"] = [{"file_id": "f1", "priority": PRIORITY_OTHER,
                          "reason": "other", "queued_at": "2026-08-22T09:00:00"}]

    async def attempt(item):
        return {"ok": False, "spent": True}

    _run(q.run(attempt))
    item = q.data["pending"][0]
    assert item["attempts"] == 1
    assert item["last_attempt"] == "2026-08-22"


def test_queue_quota_stop_does_not_blacklist_the_item(tmp_path):
    """A provider quota stop is not the item's failure.

    In IST the local calendar day rolls 5.5 h before OpenSubtitles' UTC
    quota does, so a run just after local midnight attempts against a
    still-exhausted provider. Marking those items failed_today would
    blacklist them for the whole real day -- including the hours after the
    provider's quota actually reset."""
    q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
    q.data["pending"] = [
        {"file_id": "f1", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:00:00"},
        {"file_id": "f2", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:01:00"},
    ]
    calls = []

    async def attempt(item):
        calls.append(item["file_id"])
        return {"ok": False, "spent": True, "remaining": 0}

    _run(q.run(attempt))

    assert calls == ["f1"]                   # stopped the run, didn't burn f2
    assert q.data["failed_today"] == []      # neither item blacklisted
    assert {it["file_id"] for it in q.data["pending"]} == {"f1", "f2"}


def test_queue_stops_at_daily_limit_without_a_provider_hint(tmp_path):
    """remaining()'s spent-based fallback, exercised on its own.

    Both pre-existing budget tests drive the `remaining_hint` path, so a
    mutation that ignored `spent` whenever no hint had ever been reported
    would have survived the suite -- and the anonymous tier reports no
    `remaining` field at all on some responses."""
    q = _queue(tmp_path, daily_limit=2, today="2026-08-22")
    q.data["pending"] = [
        {"file_id": "f%d" % i, "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:0%d:00" % i} for i in range(4)
    ]
    calls = []

    async def attempt(item):
        calls.append(item["file_id"])
        return {"ok": False, "spent": True}     # no "remaining" key at all

    _run(q.run(attempt))

    assert q.data["remaining_hint"] == {}       # nothing ever hinted
    assert len(calls) == 2                      # stopped at daily_limit


# ---------------------------------------------------------------------------
# The provider's own wall, and its malformed answers. Both of these cost a
# real day's budget on the first live run: five /download calls returned HTTP
# 200 with a non-JSON body, OpenSubtitles debited all five, and the resolver
# discarded each one in silence.
# ---------------------------------------------------------------------------

def test_remaining_is_zero_until_the_provider_reset_instant(tmp_path):
    import time as _time
    q = SubtitleQueue(path=str(tmp_path / "q.json"), daily_limit=5)
    q.note_quota_reset(_time.time() + 3600)
    assert q.remaining() == 0
    # A local-day roll must NOT hand back a budget the provider has not reset.
    q.data["date"] = "1999-01-01"
    q._roll_day_if_needed()
    assert q.spent() == 0 and q.remaining() == 0
    # Past the reset instant, the normal budget applies again.
    q.note_quota_reset(_time.time() - 5)
    assert q.remaining() == 5


def test_glitch_stops_the_run_without_blaming_the_item(tmp_path):
    q = SubtitleQueue(path=str(tmp_path / "q.json"), daily_limit=5)
    q.sync_pending({"a": (1, "unwatched_movie"), "b": (1, "unwatched_movie"),
                    "c": (1, "unwatched_movie")})
    seen = []

    async def attempt(item):
        seen.append(item["file_id"])
        return {"ok": False, "spent": True, "glitch": "non_json_200"}

    asyncio.run(q.run(attempt))
    # Stopped after ONE item instead of feeding the whole budget in.
    assert len(seen) == 1
    assert q.spent() == 1
    # The provider misbehaved; the file is not at fault and keeps its place.
    assert q.data["failed_today"] == []
    assert [i["file_id"] for i in q.data["pending"]] == ["a", "b", "c"]
    assert q.status.get("glitch") == "non_json_200"


def test_stale_zero_hint_is_dropped_after_the_reset_instant(tmp_path):
    import time as _time
    q = SubtitleQueue(path=str(tmp_path / "q.json"), daily_limit=5)
    q.record_spend("f1", False, remaining=0, spend=False)
    q.note_quota_reset(_time.time() - 5)
    assert q.remaining() == 5
    assert "quota_reset_utc" not in q.data or not q.data["quota_reset_utc"]


def test_quota_stop_and_glitch_do_not_count_toward_parking(tmp_path):
    for outcome in ({"ok": False, "spent": True, "remaining": 0},
                    {"ok": False, "spent": True, "glitch": "non_json_200"}):
        q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
        q.data["pending"] = [
            {"file_id": "f1", "priority": PRIORITY_OTHER, "reason": "other",
             "queued_at": "2026-08-22T09:00:00"}]

        async def attempt(item, outcome=outcome):
            return dict(outcome)

        _run(q.run(attempt))
        assert not q.data["pending"][0].get("attempts")


def test_transient_failure_is_not_blamed_on_the_item(tmp_path):
    q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
    q.data["pending"] = [
        {"file_id": "f1", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:00:00"}]

    async def attempt(item):
        return {"ok": False, "spent": False, "transient": True}

    _run(q.run(attempt))
    assert q.data["failed_today"] == []
    assert not q.data["pending"][0].get("attempts")
    assert [i["file_id"] for i in q.data["pending"]] == ["f1"]


def test_spent_transient_failure_is_not_retried_same_day(tmp_path):
    q = _queue(tmp_path, daily_limit=5, today="2026-08-22")
    q.data["pending"] = [
        {"file_id": "f1", "priority": PRIORITY_OTHER, "reason": "other",
         "queued_at": "2026-08-22T09:00:00"}]

    async def attempt(item):
        return {"ok": False, "spent": True, "transient": True}

    _run(q.run(attempt))
    assert q.spent() == 1
    assert q.data["failed_today"] == ["f1"]
    assert not q.data["pending"][0].get("attempts")


def test_spend_without_remaining_decrements_existing_hint(tmp_path):
    q = _queue(tmp_path, daily_limit=5)
    q.record_spend("f1", True, remaining=3)
    q.record_spend("f2", False, remaining=None)
    assert q.remaining() == 2
    q.record_spend("f3", False, remaining=None, spend=False)
    assert q.remaining() == 2


def test_save_failure_cleans_temp_and_clears_running(tmp_path, monkeypatch):
    q = _queue(tmp_path, daily_limit=5)
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("drivecast.subtitle_queue.os.replace", boom)
    q._save()
    assert [p for p in tmp_path.iterdir() if p.name.startswith(".subq-")] == []

    def mkboom(*a, **k):
        raise PermissionError("nope")
    monkeypatch.setattr("drivecast.subtitle_queue.tempfile.mkstemp", mkboom)
    async def attempt(item):
        return {"ok": True}
    _run(q.run(attempt))
    assert q.status["running"] is False
