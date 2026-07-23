"""Google Drive v3 API client (browse / search / metadata).

All calls use a Bearer token from the TokenManager and ALWAYS pass
supportsAllDrives=true / includeItemsFromAllDrives=true so Shared Drives work.
"""
import asyncio
import logging
import re
import time

import httpx

log = logging.getLogger("drivecast.drive_api")

FILES_URL = "https://www.googleapis.com/drive/v3/files"

FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,videoMediaMetadata,thumbnailLink,parents"
)
LIST_FIELDS = "nextPageToken,files(%s)" % FILE_FIELDS

FOLDER_MIME = "application/vnd.google-apps.folder"

DRIVES_CACHE_TTL = 300  # 5 minutes

# Drive error reasons that mean "you're going too fast" — retry with backoff.
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
# Exponential backoff schedule (seconds) for rate-limited requests during a scan.
DEFAULT_BACKOFFS = (1.0, 2.0, 4.0, 8.0, 16.0)


def escape_q(value):
    """Escape a value for use inside a Drive query string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveAPIError(Exception):
    """A Drive API call returned an error we surface to the caller."""

    def __init__(self, status, message, reason=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason  # google error reason, e.g. "notFound"


class DriveAPI:
    def __init__(self, token_manager, drives_lister, backoffs=DEFAULT_BACKOFFS):
        """
        token_manager: TokenManager instance.
        drives_lister: async callable returning the raw drives list (from rclone).
        backoffs: exponential backoff schedule (seconds) for rate-limited GETs.
        """
        self.tokens = token_manager
        self._drives_lister = drives_lister
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._drives_cache = None
        self._drives_cache_at = 0.0
        self._meta_cache = {}  # file_id -> file dict
        self._backoffs = tuple(backoffs)

    async def aclose(self):
        await self._client.aclose()

    async def _auth_headers(self):
        tok = await self.tokens.get_token()
        return {"Authorization": "Bearer %s" % tok}

    async def _get(self, url, params):
        """GET with a 401-retry (token refresh) and rate-limit backoff/retry.

        On 403 rateLimitExceeded / userRateLimitExceeded or 429 we sleep for an
        exponentially increasing delay and retry, so a scan survives Drive's
        tiny per-minute quota instead of crashing. Retries are exhausted into a
        DriveAPIError only after the whole backoff schedule fails.
        """
        did_auth_retry = False
        attempt = 0
        while True:
            headers = await self._auth_headers()
            resp = await self._client.get(url, params=params, headers=headers)

            if resp.status_code == 401 and not did_auth_retry:
                await self.tokens.force_refresh()
                did_auth_retry = True
                continue

            if resp.status_code in (403, 429):
                reason, message = self._error_info(resp)
                if (resp.status_code == 429 or reason in RATE_REASONS) and attempt < len(self._backoffs):
                    delay = self._backoffs[attempt]
                    log.warning("Drive rate-limited (%s); backing off %.0fs (attempt %d)",
                                reason or resp.status_code, delay, attempt + 1)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise DriveAPIError(resp.status_code, message, reason)

            if resp.status_code >= 400:
                reason, message = self._error_info(resp)
                raise DriveAPIError(resp.status_code, message, reason)

            return resp.json()

    @staticmethod
    def _error_info(resp):
        """Return (reason, message) parsed from a Drive error response body."""
        reason = None
        message = "Drive API error %s" % resp.status_code
        try:
            body = resp.json()
            err = body.get("error", {})
            message = err.get("message", message)
            errors = err.get("errors") or []
            if errors:
                reason = errors[0].get("reason")
        except Exception:
            pass
        return reason, message

    def _raise_for(self, resp):
        reason, message = self._error_info(resp)
        raise DriveAPIError(resp.status_code, message, reason)

    # ---- drives ----

    async def list_drives(self, force=False):
        """Return [{"id","name"}, ...] of Shared Drives (cached ~5 min)."""
        now = time.time()
        if not force and self._drives_cache is not None and (now - self._drives_cache_at) < DRIVES_CACHE_TTL:
            return self._drives_cache
        raw = await self._drives_lister()
        drives = [
            {"id": d.get("id"), "name": d.get("name")}
            for d in raw
            if d.get("id")
        ]
        drives.sort(key=lambda d: (d.get("name") or "").lower())
        self._drives_cache = drives
        self._drives_cache_at = now
        return drives

    # ---- browse ----

    async def browse(self, drive_id, folder_id=None, page_token=None, page_size=200,
                     kinds=("video",)):
        """List folders + media files within a folder of a Shared Drive.

        Root folder id == the drive id itself. `kinds` selects which file
        families come back besides folders — the default matches the classic
        video-only behaviour; section scans widen it (audio for podcasts and
        custom audio sections,
        pdf/image for course materials and covers).
        """
        parent = folder_id or drive_id
        q = "'%s' in parents and trashed = false" % escape_q(parent)
        params = {
            "q": q,
            "corpora": "drive",
            "driveId": drive_id,
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "fields": LIST_FIELDS,
            "orderBy": "folder,name",
            "pageSize": str(page_size),
        }
        if page_token:
            params["pageToken"] = page_token
        data = await self._get(FILES_URL, params)
        files = self._filter_browse(data.get("files", []), kinds)
        self._cache_metas(files)
        return {"files": files, "nextPageToken": data.get("nextPageToken")}

    _KIND_PREFIXES = {"video": "video/", "audio": "audio/", "image": "image/"}
    # Drive reports some audio containers (notably .m4b audiobooks) as
    # application/octet-stream — fall back to the extension for those.
    _AUDIO_EXTS = (".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus", ".wav")
    # Subtitle files get inconsistent mimes (text/plain, octet-stream, x-subrip)
    # — extension is the only reliable signal.
    _SUB_EXTS = (".srt", ".vtt", ".ass", ".sub")

    def _filter_browse(self, files, kinds=("video",)):
        """Keep folders plus files whose mime family is in `kinds`."""
        out = []
        for f in files:
            mime = f.get("mimeType", "")
            if mime == FOLDER_MIME:
                out.append(f)
                continue
            name = (f.get("name") or "").lower()
            for k in kinds:
                if k == "pdf" and mime == "application/pdf":
                    out.append(f)
                    break
                if (k == "audio" and mime == "application/octet-stream"
                        and name.endswith(self._AUDIO_EXTS)):
                    out.append(f)
                    break
                if k == "subs" and name.endswith(self._SUB_EXTS):
                    out.append(f)
                    break
                prefix = self._KIND_PREFIXES.get(k)
                if prefix and mime.startswith(prefix):
                    out.append(f)
                    break
        return out

    async def fetch_file_bytes(self, file_id, max_size=2 * 1024 * 1024):
        """Download a small file's content (subtitles); None on error/too big."""
        try:
            headers = await self._auth_headers()
            resp = await self._client.get(
                "%s/%s" % (FILES_URL, file_id),
                params={"alt": "media", "supportsAllDrives": "true"},
                headers=headers, follow_redirects=True)
            if resp.status_code == 200 and len(resp.content) <= max_size:
                return resp.content
        except httpx.HTTPError:
            pass
        return None

    # ---- search ----

    async def search(self, query, page_token=None, page_size=200):
        """Search video files across all drives by name substring."""
        term = escape_q(query)
        q = "name contains '%s' and mimeType contains 'video/' and trashed = false" % term
        params = {
            "q": q,
            "corpora": "allDrives",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "fields": LIST_FIELDS,
            "orderBy": "name",
            "pageSize": str(page_size),
        }
        if page_token:
            params["pageToken"] = page_token
        data = await self._get(FILES_URL, params)
        files = [f for f in data.get("files", []) if f.get("mimeType", "").startswith("video/")]
        self._cache_metas(files)
        return {"files": files, "nextPageToken": data.get("nextPageToken")}

    # ---- thumbnails ----

    async def fetch_thumbnail(self, url):
        """Download a file's thumbnailLink image; returns bytes or None.

        Drive hands out small (=s220) thumbnails by default — bump the size
        parameter so the image survives being displayed as a poster. Falls
        back to the original URL if the resized variant fails.
        """
        bumped = re.sub(r"=s\d+[^&]*$", "=s640", url)
        headers = await self._auth_headers()
        # Guard each attempt separately: a timeout on the resized variant must
        # not abort the fallback to the original URL.
        for u in ([bumped, url] if bumped != url else [url]):
            try:
                resp = await self._client.get(u, headers=headers, follow_redirects=True)
                if resp.status_code == 200:
                    return resp.content
            except httpx.HTTPError:
                continue
        return None

    # ---- metadata ----

    def _cache_metas(self, files):
        for f in files:
            if f.get("id"):
                self._meta_cache[f["id"]] = f

    def seed_meta(self, meta):
        """Prime the metadata cache from an external source (e.g. the library).

        Lets HEAD/stream answer size/type without a Drive call for cached files.
        """
        if meta.get("id"):
            self._meta_cache[meta["id"]] = meta

    def has_meta(self, file_id):
        return file_id in self._meta_cache

    async def file_meta(self, file_id, force=False):
        """Return metadata for a single file (cached)."""
        if not force and file_id in self._meta_cache:
            return self._meta_cache[file_id]
        params = {
            "supportsAllDrives": "true",
            "fields": FILE_FIELDS,
        }
        data = await self._get("%s/%s" % (FILES_URL, file_id), params)
        self._meta_cache[file_id] = data
        return data
