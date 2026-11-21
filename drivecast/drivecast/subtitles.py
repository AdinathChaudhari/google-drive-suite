"""English subtitles for playback, resolved at play time and cached locally.

Resolution order for a video file (first hit wins):
  1. the local cache (``data/subs/<file_id>.srt`` — instant on replays);
  2. a sibling subtitle file in the video's own Drive folder (release folders
     very often ship an ``.srt`` right next to the video) — matched by name,
     downloaded once;
  3. OpenSubtitles (https://www.opensubtitles.com), when an API key is set in
     ``secrets/secrets.json`` as ``opensubtitles_api_key`` — searched through
     a fallback chain, most precise first: moviehash (exact byte-content
     match, see ``moviehash.py``) -> tmdb_id (+season/episode) -> today's
     parsed title (+ year / season / episode) text query. Each step is
     independent (a moviehash 500 falls through, it doesn't abort the
     chain), and every candidate across whichever step succeeds is scored by
     ``SubtitleResolver._rank_os_file`` rather than trusting the API's own
     ordering.

The resolved subtitle is a LOCAL file path handed to the player (mpv/IINA/VLC
all take a local ``--sub-file``), so players never deal with Drive auth.
Failures at any step are silent — playback must never block on subtitles.

Two hard-won rules govern what reaches that cache:

* **Nothing is cached unvalidated.** An ISP that substitutes a court-order
  block page for the subtitle host returns HTTP 200 with HTML, and the old
  code wrote those 263 bytes as ``<file_id>.srt`` — permanently, because
  ``cached()`` only checks that the file exists. Every byte now passes
  :func:`sanitize_subtitle_bytes` at the single write point.
* **A failure is remembered.** Without memory, a blocked source is retried on
  every play inside the caller's 10-15 s budget and burns an OpenSubtitles
  download slot each time (the free tier allows five a day). Rejects and
  genuine misses land in :class:`NegativeCache` with a per-reason TTL, and
  ``force=True`` (Settings -> re-fetch) is the escape hatch for a subtitle the
  gate judged wrongly.
"""
import asyncio
import codecs
import datetime
import json
import logging
import math
import os
import re
import tempfile
import time

import httpx

from . import config
from . import moviehash
from . import naming

log = logging.getLogger("drivecast.subtitles")

SUB_EXTS = (".srt", ".vtt", ".ass", ".sub")
OS_BASE = "https://api.opensubtitles.com/api/v1"
OS_USER_AGENT = "drivecast v1.0"
# Markers that suggest an English subtitle file.
_EN_MARKERS = ("english", ".en.", ".eng.", "_en.", "-en.", ".en-", "[en]")

# MIME types for subtitle formats that remote players (e.g. Media3/ExoPlayer)
# can parse when served over HTTP. MicroDVD .sub is deliberately absent —
# nothing downstream can render it, so it's treated as "no subtitles".
SUB_MIME = {
    ".srt": "application/x-subrip",
    ".vtt": "text/vtt",
    ".ass": "text/x-ssa",
}


def mime_for(path):
    """MIME type for a subtitle file, or None if the format can't be served."""
    ext = os.path.splitext(path or "")[1].lower()
    return SUB_MIME.get(ext)


def is_subtitle_name(name):
    return (name or "").lower().endswith(SUB_EXTS)


def _looks_english(name):
    n = (name or "").lower()
    return any(m in n for m in _EN_MARKERS)


def pick_sibling_sub(video_name, files):
    """Choose the best subtitle file for a video from its folder's files.

    Scoring: exact stem match beats prefix stem match beats a matching
    SxxExx episode marker; an English marker breaks ties. With a single
    candidate and no signal at all we still take it (single-movie folders
    ship one unnamed .srt). Returns the file dict or None.
    """
    subs = [f for f in (files or [])
            if is_subtitle_name(f.get("name")) and not naming.is_junk(f.get("name") or "")]
    if not subs:
        return None
    stem = naming.strip_ext(video_name or "").lower()
    # strip_ext only removes video extensions; do subtitle stems by hand.
    ep = naming.detect_episode(video_name or "")

    def score(f):
        n = (f.get("name") or "").lower()
        fstem = os.path.splitext(n)[0]
        # a ".en" style language tag between stem and extension still matches
        s = 0
        if fstem == stem:
            s += 100
        elif fstem.startswith(stem) or stem.startswith(fstem):
            s += 50
        if ep is not None and naming.detect_episode(n) == ep:
            s += 40
        if _looks_english(n):
            s += 10
        return s

    best = max(subs, key=score)
    if score(best) > 0:
        return best
    # No signal: only trust the lone candidate (typical one-movie folder).
    return subs[0] if len(subs) == 1 else None


# ---- quality gate ----------------------------------------------------------
#
# Every failure mode below was found in the live 62-file cache, not imagined:
# two 263-byte Airtel court-order block pages saved as .srt, one 267-byte
# "Provided by YTS.MX" stub whose four cues carry no dialogue, and two files in
# legacy 8-bit encodings that render as mojibake on Android VLC.

# Fewer cues than this is not usable content at any length.
MIN_CUES = 3
# At or below this cue count, a majority-promotional file is rejected. Above
# it, a lone credit cue ("Subtitles by <name>") is ordinary and must survive —
# that asymmetry is the whole false-positive guard.
AD_ONLY_MAX_CUES = 12
# Promotional markers, deliberately brand- and URL-shaped. A bare "Subtitles
# by <name>" credit must NOT match: real releases open with one.
_AD_MARKERS = (
    "yts.mx", "yts.am", "yts.lt", "yify", "rarbg", "torrent", "1337x",
    "www.", "http://", "https://", ".com/", ".org/", ".net/",
    "advertise your product", "openload", "subscene.com", "watch free",
    "@gmail", "@hotmail", "uploaded by", "sync by",
)
_HTML_MARKERS = ("<html", "<!doctype", "<iframe", "<script", "<meta ", "<body",
                 "<div", "<span style", "<head>")
# SRT/VTT cue arrow, both comma and dot fractions.
_CUE_RE = re.compile(r"\d{1,3}:\d{2}:\d{2}[,.]\d{1,3}\s*-->")
# SubStation dialogue lines carry no arrow.
_ASS_CUE_RE = re.compile(r"^\s*Dialogue\s*:", re.M)
# Encodings tried in order once no BOM has settled it. UTF-16 is deliberately
# ABSENT: a BOM-less UTF-16 attempt "succeeds" on plain ASCII by pairing bytes
# into CJK garbage, which silently turned a real 1,752-cue subtitle in the live
# cache into a zero-cue reject. latin-1 never fails, so it is the floor and the
# reason "undecodable" is reserved for bytes that are not text at all.
_ENCODINGS = ("utf-8", "cp1252", "latin-1")

REJECT_REASONS = ("html_block_page", "zero_cue", "ads_only_or_empty",
                  "undecodable")


def _decode_text(raw):
    """Decode subtitle bytes to str, or None if they are not text.

    Binary sniffing comes first: a NUL byte outside a UTF-16 BOM means this is
    a container or an image, and latin-1 would happily "decode" it into
    garbage that then fails the cue check with a misleading reason.
    """
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8):]
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in raw[:2048]:
        return None
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def count_cues(text):
    """Number of timed cues in a subtitle body (SRT/VTT arrows, ASS dialogue)."""
    return len(_CUE_RE.findall(text or "")) or len(_ASS_CUE_RE.findall(text or ""))


def _cue_texts(text):
    """The spoken lines only — cue numbers, timestamps and blanks dropped."""
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.isdigit() or "-->" in s:
            continue
        if s.startswith("WEBVTT") or s.startswith("[") and s.endswith("]"):
            continue
        if s.lower().startswith("dialogue:"):
            s = s.split(",", 9)[-1]
        out.append(s)
    return out


def _is_ad(line):
    low = line.lower()
    return any(m in low for m in _AD_MARKERS)


def sanitize_subtitle_bytes(raw, ext=".srt"):
    """Validate and normalise subtitle bytes before they are ever cached.

    Returns ``(normalised_bytes, "ok")`` on accept, or ``(None, reason)`` where
    reason is one of :data:`REJECT_REASONS`. Normalisation is UTF-8 without a
    BOM and CRLF line endings — the form every target player (VLC on Android
    and macOS, mpv, IINA) reads without a manual encoding override. The format
    itself is never transcoded: ``mime_for`` maps by extension, so writing ASS
    bytes under a .srt name would serve a mislabelled track.
    """
    if not raw or not raw.strip():
        return None, "ads_only_or_empty"
    text = _decode_text(raw)
    if text is None:
        return None, "undecodable"
    head = text[:4096].lstrip().lower()
    if any(m in head for m in _HTML_MARKERS):
        return None, "html_block_page"
    cues = count_cues(text)
    if cues == 0:
        return None, "zero_cue"
    lines = _cue_texts(text)
    if not lines:
        return None, "ads_only_or_empty"
    if cues < MIN_CUES:
        return None, "ads_only_or_empty"
    if cues <= AD_ONLY_MAX_CUES:
        ads = sum(1 for line in lines if _is_ad(line))
        if ads * 2 >= len(lines):
            return None, "ads_only_or_empty"
    body = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if not body.endswith("\r\n"):
        body += "\r\n"
    return body.encode("utf-8"), "ok"


# ---- negative cache --------------------------------------------------------

# How long a source stays skipped, by why it failed. A poisoned or junk
# response is a property of the source that will not change today; a plain miss
# might be uploaded tomorrow. Transient failures (5xx, timeouts) are never
# recorded at all — the existing retry loop already covers them, and recording
# them would hide a subtitle behind a momentary network fault.
NEG_TTL = {
    "html_block_page": 14 * 86400,
    "ads_only_or_empty": 14 * 86400,
    "zero_cue": 14 * 86400,
    "undecodable": 14 * 86400,
    "not_found": 2 * 86400,
}
DEFAULT_NEG_TTL = 2 * 86400
NEG_FILENAME = "subs_negative.json"


class NegativeCache:
    """Remembers, per (file_id, source), that a lookup is not worth repeating.

    Keys are ``"<file_id>:<source>"`` with source in ``sibling``,
    ``opensubtitles`` or ``migration``. The store is small (one entry per dead
    end) and read-mostly, so it is loaded once and rewritten atomically on
    change, the same tempfile + ``os.replace`` shape ``config.save_config``
    uses. The path is resolved lazily on first use so a test that redirects
    ``config.DATA_DIR`` is honoured.
    """

    def __init__(self, path=None):
        self._path = path
        self._data = None

    @property
    def path(self):
        return self._path or os.path.join(config.DATA_DIR, NEG_FILENAME)

    def _load(self):
        if self._data is None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                self._data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def _save(self):
        data = self._data if self._data is not None else {}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path),
                                       prefix=".subsneg-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            log.warning("Could not persist the subtitle negative cache")

    @staticmethod
    def _key(file_id, source):
        return "%s:%s" % (file_id, source)

    def blocked(self, file_id, source):
        """The reason this source is being skipped, or None to go ahead."""
        entry = self._load().get(self._key(file_id, source))
        if not entry:
            return None
        reason = entry.get("reason") or "not_found"
        ttl = NEG_TTL.get(reason, DEFAULT_NEG_TTL)
        if time.time() - float(entry.get("at") or 0) > ttl:
            self._load().pop(self._key(file_id, source), None)
            self._save()
            return None
        return reason

    def record(self, file_id, source, reason):
        key = self._key(file_id, source)
        prev = self._load().get(key) or {}
        self._load()[key] = {"reason": reason, "at": time.time(),
                             "attempts": int(prev.get("attempts") or 0) + 1}
        self._save()
        log.info("Subtitle dead end remembered: %s (%s)", key, reason)

    def clear(self, file_id=None):
        """Forget entries for one file id, or every entry. Returns the count."""
        data = self._load()
        keys = ([k for k in data if k.split(":", 1)[0] == file_id]
                if file_id else list(data))
        for k in keys:
            data.pop(k, None)
        if keys:
            self._save()
        return len(keys)


def _atomic_write(dest, data):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".sub-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


class _OSSearchFailed(Exception):
    """One OpenSubtitles search attempt didn't get a clean answer.

    Internal signal only, caught inside _from_opensubtitles's fallback
    chain -- it exists so a non-200 (including the moviehash endpoint's
    reported 500s) and a raw transport error are handled identically:
    move to the next strategy, and never mistake "the request failed" for
    "the search succeeded and found nothing".
    """


class SubtitleResolver:
    """Resolves + caches an English subtitle for a video file id."""

    def __init__(self, api, opensubtitles_key=None, subs_dir=None,
                 negative=None):
        self.api = api                       # DriveAPI (browse + fetch_file_bytes)
        self.os_key = (opensubtitles_key or "").strip()
        self.subs_dir = subs_dir or config.SUBS_DIR
        # Dead ends are remembered here so a blocked host cannot be re-dialled
        # on every play. Tests hand in their own instance with a temp path.
        self.negative = negative if negative is not None else NegativeCache()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        # Session D (subtitle_queue.py): the backfill queue's remaining_hint
        # wants OpenSubtitles' OWN reported quota, not a locally-derived
        # guess, so a tier change or an out-of-band spend is picked up
        # instead of silently drifting. Set (or cleared to None) by every
        # _download_os_file call; a caller that cares reads it right after
        # awaiting resolve()/_from_opensubtitles -- single-flight, not a
        # queue of its own, so it only ever reflects the MOST RECENT
        # download attempt.
        self.last_os_remaining = None
        # How many OpenSubtitles /download calls this process has ISSUED.
        # The backfill queue charges its five-a-day budget from a real
        # provider call, never from "an attempt happened" -- resolve()
        # short-circuits on a cached file and tries the free Drive sibling
        # first, so a queued item routinely completes without touching
        # OpenSubtitles at all. A caller snapshots this before awaiting
        # resolve() and compares after; last_os_remaining cannot answer the
        # same question, because it stays None whenever the response carried
        # no "remaining" field (or the call failed before parsing one).
        self.os_download_calls = 0
        # Set by the last /download call: how many slots the provider says are
        # left, when its 24 h window resets (epoch seconds, UTC), and whether
        # the call was spent on a malformed answer. The queue reads all three.
        self.last_os_remaining = None
        self.last_os_glitch = None
        self.last_os_reset_utc = None

    async def aclose(self):
        await self._client.aclose()

    def cache_path(self, file_id, ext=".srt"):
        return os.path.join(self.subs_dir, "%s%s" % (file_id, ext))

    def cached(self, file_id):
        """Existing cached subtitle path for a video file id, else None."""
        for ext in SUB_EXTS:
            p = self.cache_path(file_id, ext)
            if os.path.exists(p):
                return p
        return None

    async def resolve(self, file_id, name, drive_id=None, parent_id=None,
                      force=False, size=None, tmdb_id=None, season=None,
                      episode=None, allow_opensubtitles=True):
        """Return a local subtitle path for the video, or None. Never raises.

        ``force=True`` is the user pressing "re-fetch": it forgets this file's
        remembered dead ends and ignores the cached file, so a subtitle the
        gate rejected wrongly — or one that simply arrived corrupted — can be
        replaced without hand-editing the cache. A forced attempt that fails
        falls back to whatever was already cached rather than leaving playback
        with nothing.

        ``size``/``tmdb_id``/``season``/``episode`` feed the OpenSubtitles
        fallback chain (moviehash, then tmdb_id+season+episode) — all optional
        because a caller without them degrades to the original title/year/
        season text query exactly as before; nothing here fails without them.

        ``allow_opensubtitles=False`` runs only the free sources (cache, then
        the Drive sibling) and stops. It exists for the caller that already
        knows the file carries a usable embedded English track: the sidecar
        would be redundant, and fetching one anyway spends a scarce
        anonymous-tier download that the backfill queue is counting on. The
        sibling step stays enabled because it costs no quota at all.
        """
        try:
            if force:
                self.negative.clear(file_id)
            else:
                hit = self.cached(file_id)
                if hit:
                    return hit
            path = await self._from_drive_sibling(file_id, name, drive_id, parent_id)
            if path:
                return path
            if allow_opensubtitles:
                path = await self._from_opensubtitles(file_id, name, size=size,
                                                      tmdb_id=tmdb_id, season=season,
                                                      episode=episode)
                if path:
                    return path
            return self.cached(file_id) if force else None
        except Exception:  # pragma: no cover - defensive: never block playback
            log.exception("Subtitle resolution failed for %r", name)
            return None

    # ---- the single write point ----

    def _accept(self, file_id, source, raw, ext):
        """Gate bytes, then cache them. Returns the path, or None on reject.

        Every subtitle byte in the system passes through here — the gate cannot
        be bypassed by adding a source, because a source that does not call
        this has no way to write into the cache.
        """
        clean, reason = sanitize_subtitle_bytes(raw, ext)
        if clean is None:
            log.warning("Subtitle rejected (%s) for %s from %s",
                        reason, file_id, source)
            self.negative.record(file_id, source, reason)
            return None
        dest = self.cache_path(file_id, ext if ext in SUB_EXTS else ".srt")
        if not _atomic_write(dest, clean):
            return None
        return dest

    # ---- source 2: sibling file on the drive ----

    async def _from_drive_sibling(self, file_id, name, drive_id, parent_id):
        if not drive_id or not parent_id:
            return None
        if self.negative.blocked(file_id, "sibling"):
            return None
        try:
            res = await self.api.browse(drive_id, parent_id, kinds=("subs",))
        except Exception:
            # Transient: never remembered, or one flaky listing would hide a
            # sibling subtitle for two days.
            return None
        sub = pick_sibling_sub(name, res.get("files") or [])
        if not sub or not sub.get("id"):
            self.negative.record(file_id, "sibling", "not_found")
            return None
        data = await self.api.fetch_file_bytes(sub["id"])
        if not data:
            return None
        ext = os.path.splitext((sub.get("name") or "").lower())[1] or ".srt"
        dest = self._accept(file_id, "sibling", data, ext)
        if dest:
            log.info("Subtitle from drive: %r -> %s", sub.get("name"), dest)
        return dest

    # ---- source 3: OpenSubtitles ----

    def _os_headers(self):
        return {"Api-Key": self.os_key, "User-Agent": OS_USER_AGENT,
                "Content-Type": "application/json"}

    async def _from_opensubtitles(self, file_id, name, size=None, tmdb_id=None,
                                  season=None, episode=None):
        """Search OpenSubtitles through a fallback chain, best match wins.

        Three attempts, most precise first, EACH in its own try/except:
        moviehash (exact byte-content match) -> tmdb_id (+season/episode for
        a TV episode) -> today's original title/year/season text query. The
        moviehash search param in particular has forum reports of returning
        HTTP 500 -- a failure there must fall through to the next step, not
        abort resolution and cost the file a whole negative-cache TTL for an
        OpenSubtitles hiccup that had nothing to do with whether a subtitle
        exists. ``size``/``tmdb_id``/``season``/``episode`` are all optional:
        a step whose inputs are missing is simply skipped, same as if it had
        run and found nothing.

        ``searched_ok`` tracks whether ANY step got as far as a clean 200
        response, as opposed to every step raising or erroring out. Only a
        clean response proves "nothing English exists for this today" --
        recording "not_found" (a 2-day TTL) after a run of pure transport
        failures would poison the cache for a subtitle that was never really
        looked up, exactly the "never record a transient failure" rule the
        module docstring already promises for the single-attempt case.
        """
        if not self.os_key:
            return None
        if self.negative.blocked(file_id, "opensubtitles"):
            return None
        parsed = naming.parse(name or "")
        results = []
        searched_ok = False

        if size:
            try:
                movie_hash = await moviehash.osdb_hash_from_drive(self.api, file_id, size)
                if movie_hash:
                    results = await self._search_os({"moviehash": movie_hash})
                    searched_ok = True
            except Exception:
                log.info("OpenSubtitles moviehash lookup failed for %r", name,
                         exc_info=True)
                results = []

        if not results and tmdb_id:
            try:
                params = {"tmdb_id": str(tmdb_id)}
                if season is not None and episode is not None:
                    params["season_number"] = str(season)
                    params["episode_number"] = str(episode)
                results = await self._search_os(params)
                searched_ok = True
            except Exception:
                log.info("OpenSubtitles tmdb_id lookup failed for %r", name,
                         exc_info=True)
                results = []

        if not results:
            try:
                params = {"query": parsed["title"],
                          "order_by": "download_count", "order_direction": "desc"}
                if parsed["type"] == "tv" and parsed["season"] is not None:
                    params["season_number"] = str(parsed["season"])
                    params["episode_number"] = str(parsed["episode"])
                elif parsed["year"]:
                    params["year"] = str(parsed["year"])
                results = await self._search_os(params)
                searched_ok = True
            except Exception:
                log.info("OpenSubtitles text query failed for %r", name,
                         exc_info=True)
                results = []

        if not searched_ok:
            # Every step either had nothing to work with or blew up on the
            # network/HTTP layer -- a transient condition, not a verdict.
            return None
        os_file_id = self._rank_os_file(results)
        if os_file_id is None:
            # A real miss: at least one attempt got a clean answer, and none
            # of them turned up an English match.
            self.negative.record(file_id, "opensubtitles", "not_found")
            return None
        data = await self._download_os_file(os_file_id)
        if not data:
            return None
        dest = self._accept(file_id, "opensubtitles", data, ".srt")
        if dest:
            log.info("Subtitle from OpenSubtitles for %r -> %s",
                     parsed["title"], dest)
        return dest

    async def _search_os(self, params):
        """One OpenSubtitles /subtitles search.

        Raises (transport error, or a non-200 -- including the moviehash
        endpoint's reported 500s) so the caller's per-step try/except treats
        every failure mode identically: fall through to the next strategy,
        and if it was the last strategy, don't record a negative-cache
        verdict. A non-200 is not proof the subtitle doesn't exist, so it
        must never look like a clean empty result to the caller.
        """
        full = dict(params)
        full.setdefault("languages", "en")
        # follow_redirects: the API 301s to a normalised query URL.
        resp = await self._client.get(OS_BASE + "/subtitles", params=full,
                                      headers=self._os_headers(),
                                      follow_redirects=True)
        if resp.status_code != 200:
            log.warning("OpenSubtitles search HTTP %s (%r)", resp.status_code, params)
            raise _OSSearchFailed(resp.status_code)
        return (resp.json().get("data") or [])

    @staticmethod
    def _rank_os_file(results):
        """Best downloadable file id among all English candidates, or None.

        Scored, never just ``results[0]``: a moviehash match wins outright
        over everything else (it's an exact byte-content match, nothing else
        here is), then log-scaled ``download_count`` (so one wildly popular
        upload doesn't crowd out otherwise-fine alternatives on raw count
        alone), then a small bonus for ``hearing_impaired == false`` (SDH
        tracks carry non-dialogue captions a straight English speaker didn't
        ask for), then a heavy penalty for ``ai_translated``/
        ``machine_translated`` (auto-translated subs are usually worse than
        no exact match at all, but still better than nothing if that's every
        candidate there is).
        """
        best_id, best_score = None, None
        for r in results:
            attrs = (r or {}).get("attributes") or {}
            if (attrs.get("language") or "").lower() not in ("en", "eng", "english"):
                continue
            files = attrs.get("files") or []
            if not files or files[0].get("file_id") is None:
                continue
            score = 0.0
            if attrs.get("moviehash_match"):
                score += 1_000_000.0
            try:
                downloads = max(float(attrs.get("download_count") or 0), 0.0)
            except (TypeError, ValueError):
                downloads = 0.0
            score += math.log1p(downloads) * 100.0
            if attrs.get("hearing_impaired") is False:
                score += 50.0
            if attrs.get("ai_translated") or attrs.get("machine_translated"):
                score -= 5_000.0
            if best_score is None or score > best_score:
                best_score = score
                best_id = files[0]["file_id"]
        return best_id

    @staticmethod
    def _os_payload(resp):
        """The response body as a dict, or None when it is not JSON at all."""
        try:
            body = resp.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _note_os_quota(self, payload):
        """Record the provider's own quota countdown and reset instant."""
        remaining = payload.get("remaining")
        if isinstance(remaining, (int, float)):
            self.last_os_remaining = int(remaining)
        raw = payload.get("reset_time_utc")
        if isinstance(raw, str) and raw:
            try:
                # e.g. "2026-08-24T23:59:59.000Z" — the provider's window is
                # UTC, and local midnight is up to a day away from it (IST
                # rolls 5.5 h early), so the queue must key on this instant
                # rather than on its own date change.
                stamp = raw.replace("Z", "+00:00")
                self.last_os_reset_utc = (
                    datetime.datetime.fromisoformat(stamp).timestamp())
            except ValueError:
                pass

    async def _download_os_file(self, os_file_id):
        # Counted BEFORE the request: OpenSubtitles debits the daily quota on
        # the /download call itself, so an attempt that then fails at the
        # file host still cost a slot and must still be charged.
        self.os_download_calls += 1
        self.last_os_remaining = None
        self.last_os_glitch = None
        self.last_os_reset_utc = None
        try:
            resp = await self._client.post(OS_BASE + "/download",
                                           json={"file_id": os_file_id},
                                           headers=self._os_headers())
            payload = self._os_payload(resp)
            # 406 is the quota wall: the body carries `remaining: 0` and the
            # UTC reset time, both of which the queue needs so it can park
            # instead of spending the rest of the day discovering the same
            # wall one slot at a time.
            if resp.status_code != 200:
                if isinstance(payload, dict):
                    self._note_os_quota(payload)
                log.warning("OpenSubtitles download HTTP %s%s", resp.status_code,
                            (" — %s" % payload.get("message")).rstrip()
                            if isinstance(payload, dict) and payload.get("message")
                            else "")
                return None
            if not isinstance(payload, dict):
                # HTTP 200 with a body that is not JSON. Observed in the wild:
                # five consecutive downloads answered this way, each one
                # debited by OpenSubtitles (requests=5, remaining=0) and each
                # one discarded here in silence, so a whole day's budget went
                # missing with nothing in the log to explain it. The slot is
                # already gone; what matters now is saying so and letting the
                # queue stop rather than feeding the rest of the budget in.
                self.last_os_glitch = "non_json_200"
                log.warning("OpenSubtitles download returned HTTP 200 with a "
                            "non-JSON body (%s, %d bytes) — slot spent, no "
                            "link: %r",
                            resp.headers.get("content-type"), len(resp.content),
                            resp.text[:120])
                return None
            # "remaining" is the API's own quota countdown for today -- see
            # subtitle_queue.py's remaining_hint. Absent on some responses
            # (or on a malformed body caught below), in which case it's left
            # None and the queue falls back to its own spent-based estimate.
            self._note_os_quota(payload)
            link = payload.get("link")
            if not link:
                self.last_os_glitch = "no_link"
                log.warning("OpenSubtitles download 200 carried no link "
                            "(keys: %s, message: %r) — slot spent",
                            sorted(payload)[:8], payload.get("message"))
                return None
            # The file host occasionally throws transient 5xx (Cloudflare 520);
            # a couple of quick retries usually land it. Stays well inside the
            # play-request's overall subtitle timeout.
            for attempt in range(3):
                if attempt:
                    await asyncio.sleep(1.5)
                body = await self._client.get(
                    link, headers={"User-Agent": OS_USER_AGENT},
                    follow_redirects=True)
                if body.status_code == 200 and body.content:
                    return body.content
                if body.status_code < 500:
                    break  # 4xx won't heal by retrying
                log.info("OpenSubtitles file host HTTP %s (attempt %d)",
                         body.status_code, attempt + 1)
        except (httpx.HTTPError, ValueError):
            pass
        return None
