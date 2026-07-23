"""English subtitles for playback, resolved at play time and cached locally.

Resolution order for a video file (first hit wins):
  1. the local cache (``data/subs/<file_id>.srt`` — instant on replays);
  2. a sibling subtitle file in the video's own Drive folder (release folders
     very often ship an ``.srt`` right next to the video) — matched by name,
     downloaded once;
  3. OpenSubtitles (https://www.opensubtitles.com), when an API key is set in
     ``secrets/secrets.json`` as ``opensubtitles_api_key`` — searched by the
     parsed title (+ year / season / episode), best English match downloaded.

The resolved subtitle is a LOCAL file path handed to the player (mpv/IINA/VLC
all take a local ``--sub-file``), so players never deal with Drive auth.
Failures at any step are silent — playback must never block on subtitles.
"""
import asyncio
import logging
import os
import tempfile

import httpx

from . import config
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


class SubtitleResolver:
    """Resolves + caches an English subtitle for a video file id."""

    def __init__(self, api, opensubtitles_key=None, subs_dir=None):
        self.api = api                       # DriveAPI (browse + fetch_file_bytes)
        self.os_key = (opensubtitles_key or "").strip()
        self.subs_dir = subs_dir or config.SUBS_DIR
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))

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

    async def resolve(self, file_id, name, drive_id=None, parent_id=None):
        """Return a local subtitle path for the video, or None. Never raises."""
        try:
            hit = self.cached(file_id)
            if hit:
                return hit
            path = await self._from_drive_sibling(file_id, name, drive_id, parent_id)
            if path:
                return path
            return await self._from_opensubtitles(file_id, name)
        except Exception:  # pragma: no cover - defensive: never block playback
            log.exception("Subtitle resolution failed for %r", name)
            return None

    # ---- source 2: sibling file on the drive ----

    async def _from_drive_sibling(self, file_id, name, drive_id, parent_id):
        if not drive_id or not parent_id:
            return None
        try:
            res = await self.api.browse(drive_id, parent_id, kinds=("subs",))
        except Exception:
            return None
        sub = pick_sibling_sub(name, res.get("files") or [])
        if not sub or not sub.get("id"):
            return None
        data = await self.api.fetch_file_bytes(sub["id"])
        if not data:
            return None
        ext = os.path.splitext((sub.get("name") or "").lower())[1] or ".srt"
        dest = self.cache_path(file_id, ext if ext in SUB_EXTS else ".srt")
        if _atomic_write(dest, data):
            log.info("Subtitle from drive: %r -> %s", sub.get("name"), dest)
            return dest
        return None

    # ---- source 3: OpenSubtitles ----

    def _os_headers(self):
        return {"Api-Key": self.os_key, "User-Agent": OS_USER_AGENT,
                "Content-Type": "application/json"}

    async def _from_opensubtitles(self, file_id, name):
        if not self.os_key:
            return None
        parsed = naming.parse(name or "")
        params = {"query": parsed["title"], "languages": "en",
                  "order_by": "download_count", "order_direction": "desc"}
        if parsed["type"] == "tv" and parsed["season"] is not None:
            params["season_number"] = str(parsed["season"])
            params["episode_number"] = str(parsed["episode"])
        elif parsed["year"]:
            params["year"] = str(parsed["year"])
        try:
            # follow_redirects: the API 301s to a normalised query URL.
            resp = await self._client.get(OS_BASE + "/subtitles",
                                          params=params, headers=self._os_headers(),
                                          follow_redirects=True)
            if resp.status_code != 200:
                log.warning("OpenSubtitles search HTTP %s", resp.status_code)
                return None
            results = (resp.json().get("data") or [])
        except (httpx.HTTPError, ValueError):
            return None
        os_file_id = self._best_os_file(results)
        if os_file_id is None:
            return None
        data = await self._download_os_file(os_file_id)
        if not data:
            return None
        dest = self.cache_path(file_id, ".srt")
        if _atomic_write(dest, data):
            log.info("Subtitle from OpenSubtitles for %r -> %s", parsed["title"], dest)
            return dest
        return None

    @staticmethod
    def _best_os_file(results):
        """First English result's downloadable file id, else None."""
        for r in results:
            attrs = (r or {}).get("attributes") or {}
            if (attrs.get("language") or "").lower() not in ("en", "eng", "english"):
                continue
            files = attrs.get("files") or []
            if files and files[0].get("file_id") is not None:
                return files[0]["file_id"]
        return None

    async def _download_os_file(self, os_file_id):
        try:
            resp = await self._client.post(OS_BASE + "/download",
                                           json={"file_id": os_file_id},
                                           headers=self._os_headers())
            if resp.status_code != 200:
                log.warning("OpenSubtitles download HTTP %s", resp.status_code)
                return None
            link = (resp.json() or {}).get("link")
            if not link:
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
