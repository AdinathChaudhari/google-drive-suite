"""Persistent, daily-budgeted priority queue for the OpenSubtitles backfill
(Session D of the subtitles plan).

Why this exists: session B's sweep turns "under 3% cached" into a real number —
a share of the library genuinely has no usable English
subtitle. OpenSubtitles' anonymous tier allows five downloads a day (D-022),
so backfilling that gap is a matter of weeks, not one sweep. This module is
the thing that remembers, across restarts, which files still need a fetch,
in what order, and how many of today's five slots are already spent — so an
automatic run after every scan/sweep can safely fire without ever spending
the same slot twice or losing an item.

Worklist: every file `sub_index.py` marks uncovered (no `has_text_en`).
Image-only files (`has_image_en` but not `has_text_en`) are deliberately
INCLUDED here for now — VLC/mpv/IINA are documented to render an image track
themselves, but that is unverified on this hardware (D-031), and nothing
text-based can ever use an image track, so excluding them from the worklist
would risk permanently orphaning a file if the device test (§6.5) ever comes
back negative. Flip the predicate that builds the worklist, not this module,
once that test settles it either way.

Priority, highest first (`PRIORITY_*`, LOWER int == worked first):
  1. continue-watching (a partially-played file, from history.py)
  2. an unwatched movie
  3. the next unwatched episode of a show already in progress
  4. everything else
FIFO by `queued_at` inside a tier. `classify_priorities()` is the pure
function that assigns tiers from plain data (title records + history
snapshots) rather than the Library/History objects themselves, so it is
testable without touching disk; `SubtitleQueue.sync_pending()` is what a
caller (server.py) uses to fold a freshly classified worklist into the
persisted queue between runs.

Day boundary — local midnight: a NEW convention. Nothing else in this
codebase does day/timezone arithmetic to match (history.py works in raw
epoch timestamps only), so this module defines the boundary as simple
`date.today().isoformat()` string equality — no timezone conversion, no
DST edge cases to get wrong, just "did the local calendar date on the
stored record change since we last touched it". `today` is an optional
constructor override specifically so a test can pin "today" without
depending on (or mocking) the wall clock — see
`test_queue_day_boundary_resets_spent`.

Budget: `remaining_hint` mirrors OpenSubtitles' own reported quota (the
`/download` response's `remaining` field, when the caller's `attempt`
passes one back) rather than being purely derived from `spent`. That way an
out-of-band spend (another tool sharing the same key) or a tier change
(anonymous -> logged-in, D-022) is picked up on the very next call instead
of silently drifting for a day. When no hint has ever been reported,
`remaining()` falls back to `daily_limit - spent`.

"A failed fetch must not spend the same slot twice": `failed_today` is
exactly that guard. An item that `run()` tried and failed goes on
`failed_today` and is skipped by every later `run()` call THE SAME DAY
(pending order still has it — it is never dropped — just not re-attempted
until `failed_today` resets at the next day boundary).

A slot is only spent when the PROVIDER was actually called
--------------------------------------------------------
`run()` charges the daily budget from the outcome's `spent` flag, not from
"an attempt happened". This matters because `SubtitleResolver.resolve()`
short-circuits on anything already cached and tries a free Drive sibling
before it ever touches OpenSubtitles, so a queued item can complete with
zero network cost. Charging those to the budget was measured to make the
queue asymptotically stop: a backfilled file is not "covered" by the
sub_index (a fetched sidecar never sets `has_text_en`), so it is re-queued
on the next worklist sync, re-attempted, instantly satisfied from cache,
and would burn one of five daily slots re-confirming work already done —
every cycle, forever. The caller reports `spent` (server.py counts real
`/download` calls); a free hit just leaves the queue.

Head-of-line stalls are what `is_blocked` and parking exist for
---------------------------------------------------------------
Items are ordered tier-then-`queued_at`, and a failure does not re-stamp
`queued_at`, so without the two guards below the same few permanently
missing files sit at the head of the queue forever and nothing behind them
is ever reached:

* `is_blocked(file_id)` — an optional, FREE predicate (the caller wires
  `NegativeCache.blocked`). An item whose provider is still inside its
  negative-cache TTL is skipped without an attempt and without a spend:
  attempting it would have `resolve()` return None with no network call at
  all, which used to be charged as a full slot.
* parking — an item that has genuinely missed `PARK_AFTER_ATTEMPTS` times
  (real provider calls that came back empty, not transport failures and
  not free skips) is retried at most every `PARK_RETRY_DAYS` instead of
  every day. `attempts`/`last_attempt` live on the pending item, so the
  parking survives restarts and day rolls.

A provider quota stop is not an item failure
--------------------------------------------
If a fetch fails while the provider reports `remaining == 0`, the run stops
and the item is left clean — NOT added to `failed_today`. In IST the local
calendar day rolls 5.5 h before OpenSubtitles' own UTC quota does, so a run
just after local midnight can attempt against a still-exhausted provider;
marking those items failed would blacklist them for the whole real day,
including the hours after the provider's quota actually reset.
"""
import json
import logging
import os
import tempfile
import time
from datetime import date, datetime

from . import config

log = logging.getLogger("drivecast.subtitle_queue")

QUEUE_FILENAME = "subtitle_queue.json"
DEFAULT_PROVIDER = "opensubtitles"
DEFAULT_DAILY_LIMIT = 5  # anonymous-tier OpenSubtitles; config.DEFAULTS overrides.

# An item that really missed this many times (a provider call that returned
# nothing -- see the module docstring) is retried at most every
# PARK_RETRY_DAYS instead of every day, so a file OpenSubtitles genuinely
# does not have cannot hold the head of the queue against everything behind
# it. Deliberately generous: three misses across three different days is
# strong evidence, and the retry never stops entirely.
PARK_AFTER_ATTEMPTS = 3
PARK_RETRY_DAYS = 7

# Priority tiers -- LOWER sorts first ("more urgent"). Kept as plain ints
# (not an enum) because they are persisted verbatim in subtitle_queue.json.
PRIORITY_CONTINUE_WATCHING = 0
PRIORITY_UNWATCHED_MOVIE = 1
PRIORITY_NEXT_EPISODE = 2
PRIORITY_OTHER = 3

REASON_FOR_PRIORITY = {
    PRIORITY_CONTINUE_WATCHING: "continue_watching",
    PRIORITY_UNWATCHED_MOVIE: "unwatched_movie",
    PRIORITY_NEXT_EPISODE: "next_episode",
    PRIORITY_OTHER: "other",
}


def _today_str():
    return date.today().isoformat()


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _is_watched(watched_map, file_id):
    entry = (watched_map or {}).get(file_id)
    return bool(entry and entry.get("watched"))


def _show_episodes(title_rec):
    return [ep for season in (title_rec.get("seasons") or [])
            for ep in (season.get("episodes") or [])]


def _next_unwatched_episode_id(title_rec, watched_map):
    """First episode file_id, in season/episode order, not yet watched.

    None if the show has no episodes with a file_id, or every episode is
    already watched (finished, not "in progress" for backfill purposes —
    its remaining files, if any are uncovered, still queue at the "other"
    tier via the caller's fallback, just without this priority bump).
    """
    ordered = []
    for season in sorted(title_rec.get("seasons") or [],
                         key=lambda s: s.get("season") if s.get("season") is not None else 0):
        eps = sorted(season.get("episodes") or [],
                    key=lambda e: e.get("episode") if e.get("episode") is not None else 10 ** 6)
        ordered.extend(eps)
    for ep in ordered:
        fid = ep.get("file_id")
        if fid and not _is_watched(watched_map, fid):
            return fid
    return None


def classify_priorities(titles, uncovered_ids, continue_watching_ids, watched_map):
    """{file_id: (priority, reason)} for every id in `uncovered_ids`.

    Pure: `titles` is `Library.titles_list()`'s raw records,
    `continue_watching_ids` a set/iterable of file_ids (from
    `History.continue_watching()`), `watched_map` the {file_id: {"percent",
    "watched"}} shape `History.progress_map()` returns. Passed as plain data
    rather than the Library/History instances so callers can test this
    without touching disk.

    A show counts as "in progress" once ANY of its episodes has a history
    entry at all (watched or merely started) -- not just "currently in
    Continue Watching", since a show can be mid-way through with its most
    recently finished episode already past the continue-watching window.
    Only ONE file per show earns the next-episode bump: the earliest
    unwatched episode in season/episode order. Every uncovered id not
    claimed by an earlier tier falls to PRIORITY_OTHER, so nothing in
    `uncovered_ids` is ever left unclassified.
    """
    uncovered = set(uncovered_ids)
    result = {}

    for fid in uncovered:
        if fid in continue_watching_ids:
            result[fid] = (PRIORITY_CONTINUE_WATCHING, REASON_FOR_PRIORITY[PRIORITY_CONTINUE_WATCHING])

    for rec in titles:
        if rec.get("type") == "movie":
            fid = rec.get("file_id")
            if fid in uncovered and fid not in result and not _is_watched(watched_map, fid):
                result[fid] = (PRIORITY_UNWATCHED_MOVIE, REASON_FOR_PRIORITY[PRIORITY_UNWATCHED_MOVIE])
        elif rec.get("type") == "show":
            episodes = _show_episodes(rec)
            in_progress = any((watched_map or {}).get(ep.get("file_id")) for ep in episodes)
            if in_progress:
                next_fid = _next_unwatched_episode_id(rec, watched_map)
                if next_fid and next_fid in uncovered and next_fid not in result:
                    result[next_fid] = (PRIORITY_NEXT_EPISODE, REASON_FOR_PRIORITY[PRIORITY_NEXT_EPISODE])

    for fid in uncovered:
        result.setdefault(fid, (PRIORITY_OTHER, REASON_FOR_PRIORITY[PRIORITY_OTHER]))

    return result


class SubtitleQueue:
    """Load/persist/work the backfill worklist.

    The default path is resolved LAZILY (a `path` property, not a
    module-level constant) so a test that redirects `config.DATA_DIR` is
    honoured -- the same trap `subtitles.NegativeCache` and `sub_index.SubIndex`
    already route around.

    `status` mirrors `sub_sweep.SubtitleSweeper.status`'s shape and its
    reason for being separate from anything else's progress meter: a second,
    differently-shaped "running" flag merged into someone else's dict is how
    CLAUDE.md's documented `total=0` ambiguity happens again.
    """

    def __init__(self, path=None, daily_limit=None, provider=DEFAULT_PROVIDER, today=None):
        self._path = path
        self.provider = provider
        self.daily_limit = DEFAULT_DAILY_LIMIT if daily_limit is None else int(daily_limit)
        # Override for "what day is it" -- production always leaves this
        # None (real local date); tests pin it so the day-boundary behaviour
        # is verified without mocking the wall clock. See module docstring.
        self._today_override = today
        self.data = self._load()
        self.status = {"running": False, "worked": 0, "ok": 0, "failed": 0,
                       "skipped": 0, "error": None}
        self._roll_day_if_needed()

    @property
    def path(self):
        return self._path or os.path.join(config.DATA_DIR, QUEUE_FILENAME)

    def _today(self):
        return self._today_override or _today_str()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("pending"), list):
                data.setdefault("date", self._today())
                data.setdefault("spent", {})
                data.setdefault("remaining_hint", {})
                data.setdefault("failed_today", [])
                return data
        except (OSError, ValueError):
            pass
        return {"date": self._today_override or _today_str(), "spent": {},
                "remaining_hint": {}, "pending": [], "failed_today": []}

    def _save(self):
        tmp = None
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path),
                                       prefix=".subq-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            log.warning("Could not persist the subtitle backfill queue")
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _roll_day_if_needed(self):
        """Local-midnight day boundary (see module docstring). Resets `spent`
        and `failed_today` on a date change; `pending` survives untouched --
        an item queued yesterday is still exactly as un-fetched today."""
        today = self._today()
        if self.data.get("date") != today:
            self.data["date"] = today
            self.data["spent"] = {}
            self.data["remaining_hint"] = {}
            self.data["failed_today"] = []
            self._save()

    # ---- budget ----

    def spent(self, provider=None):
        return int(self.data["spent"].get(provider or self.provider, 0))

    def remaining(self, provider=None):
        provider = provider or self.provider
        # A provider-reported wall outranks both the hint and the local day.
        # OpenSubtitles' 24 h window resets at a UTC instant; local midnight
        # is up to 5.5 h earlier here, and rolling the local day used to hand
        # the queue a fresh budget it did not have, which it then spent
        # discovering the wall again one slot at a time.
        until = self.data.get("quota_reset_utc", {}).get(provider)
        if until is not None:
            if time.time() < float(until):
                return 0
            # The wall has passed: the hint recorded against it (usually 0)
            # is stale and would otherwise hold the queue idle until local
            # midnight. Drop both and fall back to the normal budget.
            self.data["remaining_hint"].pop(provider, None)
            self.data["quota_reset_utc"].pop(provider, None)
            self._save()
        hint = self.data["remaining_hint"].get(provider)
        if hint is not None:
            return max(int(hint), 0)
        return max(self.daily_limit - self.spent(provider), 0)

    def note_quota_reset(self, reset_utc, provider=None):
        """Remember when the provider says its window reopens."""
        provider = provider or self.provider
        if reset_utc is None:
            return
        self.data.setdefault("quota_reset_utc", {})[provider] = float(reset_utc)
        self._save()

    def record_spend(self, file_id, ok, remaining=None, provider=None,
                     spend=True, mark_failed=True):
        """Book the outcome of one attempt.

        `spend=False` records the outcome WITHOUT charging the daily budget
        -- for an attempt that never reached the provider (already cached,
        or satisfied by the free Drive-sibling source). See the module
        docstring: charging those is what made the queue stop making
        progress. `mark_failed=False` likewise keeps a failure off
        `failed_today`, used only for a provider-quota stop, which is not
        the item's fault.
        """
        provider = provider or self.provider
        if spend:
            self.data["spent"][provider] = self.spent(provider) + 1
        if remaining is not None:
            self.data["remaining_hint"][provider] = max(int(remaining), 0)
        elif spend and self.data["remaining_hint"].get(provider) is not None:
            # A charged attempt that reported no quota: an older hint is now
            # one slot too generous.
            self.data["remaining_hint"][provider] = max(
                int(self.data["remaining_hint"][provider]) - 1, 0)
        if not ok and mark_failed and file_id not in self.data["failed_today"]:
            self.data["failed_today"].append(file_id)
        self._save()

    # ---- worklist sync ----

    def sync_pending(self, priorities, now=None):
        """Reconcile `pending` against the currently-uncovered worklist.

        `priorities` is {file_id: (priority, reason)}, e.g. from
        `classify_priorities`. A file_id not already pending is appended
        with `queued_at` = now (so it sorts after every existing entry in
        its tier -- FIFO). An existing entry's priority/reason is refreshed
        in place WITHOUT touching `queued_at` -- a file moving tiers (a show
        becomes "in progress" after already being queued as "other") must
        not lose the place in line it has already been waiting for. A
        file_id no longer present (now covered, or dropped from the
        library) is removed from both `pending` and `failed_today` -- so a
        stale id reappearing later can never be mistaken for "already
        attempted today". A no-op call (nothing changed) writes nothing.
        """
        incoming = dict(priorities)
        now_iso = now or _now_iso()
        kept = []
        seen = set()
        changed = False
        for item in self.data["pending"]:
            fid = item.get("file_id")
            if fid in incoming:
                priority, reason = incoming[fid]
                if item.get("priority") != priority or item.get("reason") != reason:
                    item["priority"] = priority
                    item["reason"] = reason
                    changed = True
                kept.append(item)
                seen.add(fid)
            else:
                changed = True  # dropped: no longer uncovered / no longer in the library
        for fid, (priority, reason) in incoming.items():
            if fid in seen:
                continue
            kept.append({"file_id": fid, "priority": priority, "reason": reason,
                        "queued_at": now_iso})
            seen.add(fid)
            changed = True
        if changed:
            self.data["pending"] = kept
            self.data["failed_today"] = [f for f in self.data["failed_today"] if f in incoming]
            self._save()

    def _is_parked(self, item, today):
        """True if this item has missed too often to be worth a daily retry
        yet (see the module docstring's head-of-line section). An item with
        fewer than PARK_AFTER_ATTEMPTS real misses is never parked, and an
        unparsable/absent `last_attempt` fails OPEN (not parked) -- a
        corrupt field must not silently retire a file forever."""
        if int(item.get("attempts") or 0) < PARK_AFTER_ATTEMPTS:
            return False
        last = item.get("last_attempt")
        if not last:
            return False
        try:
            gap = (date.fromisoformat(today) - date.fromisoformat(last)).days
        except (ValueError, TypeError):
            return False
        return gap < PARK_RETRY_DAYS

    def _note_attempt(self, item):
        """Count one REAL miss against this item (a provider call that came
        back empty). `item` is the live dict inside `self.data["pending"]`,
        so this persists with the next `_save()`. Transport failures and
        free skips deliberately do not land here -- parking is evidence
        that the subtitle does not exist, not that the network wobbled."""
        item["attempts"] = int(item.get("attempts") or 0) + 1
        item["last_attempt"] = self._today()

    def _ordered_pending(self):
        """`pending`, tier-then-FIFO, minus anything already attempted (and
        failed) today -- see the "must not spend the same slot twice" rule
        in the module docstring -- and minus anything parked after repeated
        real misses."""
        today = self._today()
        failed = set(self.data["failed_today"])
        eligible = [it for it in self.data["pending"]
                   if it["file_id"] not in failed and not self._is_parked(it, today)]
        return sorted(eligible, key=lambda it: (it["priority"], it["queued_at"]))

    def _remove_pending(self, file_id):
        self.data["pending"] = [it for it in self.data["pending"] if it["file_id"] != file_id]

    # ---- doing the work ----

    async def run(self, attempt, limit=None, is_blocked=None):
        """Work `pending` within today's remaining budget.

        `attempt` is `async def attempt(item) -> dict|None` with an
        `"ok"` bool, an optional `"remaining"` int (the provider's own
        reported quota, when the caller has one) and an optional `"spent"`
        bool (whether the provider was really called -- see the module
        docstring; absent is read as True, so an older caller keeps the
        conservative behaviour of charging every attempt). Injected so this
        module never imports SubtitleResolver/OpenSubtitles itself;
        server.py wires the real fetch, tests wire a stub and assert on
        call counts.

        `is_blocked(file_id) -> bool` is an optional FREE pre-check: an item
        it reports blocked is skipped without an attempt and without a
        spend. The caller wires the negative cache, whose TTL means
        `attempt` would have returned None with no network call -- which
        used to be charged as a whole slot and let a handful of dead files
        hold the queue head indefinitely.

        The budget check runs BEFORE each attempt, not after: a provider at
        (or below) zero remaining makes ZERO calls to `attempt` for the rest
        of this run, and every item that didn't get to run -- including the
        one the check stopped on -- stays in `pending`, untouched. Nothing
        queued is ever silently dropped; it is only ever removed on a
        successful fetch.

        `limit` additionally caps how many items THIS call will attempt
        (mainly for tests); production callers can leave it None and rely
        on the daily budget alone to bound the work.
        """
        self._roll_day_if_needed()
        self.status.update(running=True, worked=0, ok=0, failed=0, skipped=0,
                          error=None)
        try:
            for item in self._ordered_pending():
                if limit is not None and self.status["worked"] >= limit:
                    break
                if self.remaining() <= 0:
                    break
                if is_blocked is not None:
                    try:
                        blocked = bool(is_blocked(item["file_id"]))
                    except Exception:  # pragma: no cover - defensive
                        blocked = False
                    if blocked:
                        # Free skip: no attempt, no spend, stays pending. Its
                        # negative-cache TTL will lapse and it comes back.
                        self.status["skipped"] += 1
                        continue
                try:
                    outcome = await attempt(item)
                except Exception:
                    log.exception("Subtitle backfill attempt crashed for %s",
                                 item.get("file_id"))
                    outcome = {"ok": False}
                outcome = outcome or {}
                ok = bool(outcome.get("ok"))
                remaining = outcome.get("remaining")
                # Absent `spent` means "assume the provider was called" --
                # the safe reading for a caller that does not report it.
                spent = bool(outcome.get("spent", True))
                quota_stop = (not ok and spent and remaining is not None
                              and int(remaining) <= 0)
                glitch = outcome.get("glitch")
                # A transient failure (e.g. a 25 s timeout) says nothing about
                # the file: no parking attempt. If a slot was actually spent,
                # still keep it off the queue for the day so the next pass
                # does not burn a second slot on it.
                transient = bool(outcome.get("transient"))
                if outcome.get("reset_utc") is not None:
                    self.note_quota_reset(outcome["reset_utc"])
                self.record_spend(item["file_id"], ok, remaining=remaining,
                                  spend=spent,
                                  mark_failed=not (quota_stop or glitch
                                                    or (transient and not spent)))
                if ok:
                    self._remove_pending(item["file_id"])
                    self.status["ok"] += 1
                else:
                    if spent and not (quota_stop or glitch or transient):
                        # Provider faults (quota wall, malformed answer) are
                        # not evidence the subtitle does not exist.
                        self._note_attempt(item)
                    self.status["failed"] += 1
                self.status["worked"] += 1
                if glitch:
                    # Unlike a quota stop, a glitch leaves `remaining` unknown,
                    # so the budget check cannot end the run — say so and stop
                    # here. The item keeps its place in `pending` and is not
                    # blamed for the provider's answer.
                    self.status["glitch"] = glitch
                    log.warning("Subtitle backfill stopped: provider returned "
                                "%s (slot spent, nothing delivered)", glitch)
                    break
                # No explicit break for `quota_stop`: record_spend has just
                # stored remaining_hint=0, so the budget check at the top of
                # the next iteration ends the run by itself. (Verified as an
                # equivalent mutant -- deleting a break here changed no
                # observable behaviour, so there is no break to delete.)
                # What quota_stop actually buys is `mark_failed=False` above:
                # the provider being out is not this file's failure.
        except Exception as e:  # pragma: no cover - defensive, mirrors sub_sweep.run
            self.status["error"] = str(e)
            log.exception("Subtitle backfill run failed")
        finally:
            try:
                self._save()
            finally:
                self.status["running"] = False
        return dict(self.status)

    # ---- reporting ----

    def snapshot(self):
        """Everything the Settings UI / status endpoint needs in one dict:
        this run's live progress plus the persisted queue's current shape."""
        self._roll_day_if_needed()
        today = self._today()
        pending = self.data["pending"]
        by_reason = {}
        parked = 0
        for it in pending:
            by_reason[it["reason"]] = by_reason.get(it["reason"], 0) + 1
            if self._is_parked(it, today):
                parked += 1
        return {
            "running": self.status["running"],
            "worked": self.status["worked"],
            "ok": self.status["ok"],
            "failed": self.status["failed"],
            "skipped": self.status.get("skipped", 0),
            "parked_count": parked,
            "error": self.status["error"],
            "date": self.data["date"],
            "provider": self.provider,
            "daily_limit": self.daily_limit,
            "spent": dict(self.data["spent"]),
            "remaining": self.remaining(),
            "pending_count": len(pending),
            "pending_by_reason": by_reason,
            "failed_today_count": len(self.data["failed_today"]),
        }
