"""Range-aware streaming proxy for Google Drive files.

Video bytes NEVER touch disk. We forward the client's Range header verbatim
to the Drive media endpoint and stream the upstream response straight back.

We deliberately do NOT use `async with client.stream(...)`: that context would
exit (closing the response) before FastAPI has finished iterating the body
generator. Instead we build the request, send with stream=True, and close the
upstream response in the generator's finally block.
"""
import asyncio
import logging

import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse

log = logging.getLogger("drivecast.streaming")

# HTTP/2 (multiplexed, better keep-alive) if the optional `h2` package is
# installed; otherwise fall back to HTTP/1.1 transparently.
try:
    import h2  # noqa: F401
    HTTP2_AVAILABLE = True
except ImportError:
    HTTP2_AVAILABLE = False

MEDIA_URL = "https://www.googleapis.com/drive/v3/files/{id}"
CHUNK_SIZE = 1024 * 1024  # 1 MiB — fewer, larger relays than the old 64 KB

# Header names we relay from upstream to the client.
RELAY_HEADERS = ("content-range", "content-length", "content-type", "accept-ranges")

# Fatal Drive quota reasons -> 502 (no point retrying).
FATAL_REASONS = {"downloadQuotaExceeded", "cannotDownloadAbusiveFile"}
# Transient rate-limit reasons -> backoff + retry.
RATE_REASONS = {"userRateLimitExceeded", "rateLimitExceeded"}
BACKOFFS = (0.5, 1.0)


def _media_params():
    return {"alt": "media", "supportsAllDrives": "true"}


async def _parse_error(resp):
    """Return (reason, message) from a Drive error response body."""
    reason = None
    message = "Drive error %s" % resp.status_code
    try:
        body = await resp.aread()
        import json
        data = json.loads(body)
        err = data.get("error", {})
        message = err.get("message", message)
        errors = err.get("errors") or []
        if errors:
            reason = errors[0].get("reason")
    except Exception:
        pass
    return reason, message


class Streamer:
    def __init__(self, token_manager, drive_api, keepawake=None):
        self.tokens = token_manager
        self.api = drive_api
        # Optional KeepAwake: acquired for the life of each streamed body so the
        # Mac doesn't sleep mid-relay. None in contexts that don't need it.
        self.keepawake = keepawake
        # One long-lived client: connection pooling / keep-alive across the many
        # Range requests a player makes while seeking, instead of a fresh TCP +
        # TLS handshake per request.
        # read=None: streaming a large file has no overall read deadline;
        # connect has a sane bound.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
            http2=HTTP2_AVAILABLE,
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=60.0),
        )

    async def aclose(self):
        await self._client.aclose()

    async def _open_upstream(self, file_id, range_header):
        """Send the media request (with one 401-retry and rate-limit backoff).

        Returns an open httpx.Response (stream=True) on success, or raises.
        The caller owns closing the response.
        """
        attempt = 0
        did_auth_retry = False
        while True:
            tok = await self.tokens.get_token()
            headers = {"Authorization": "Bearer %s" % tok}
            if range_header:
                headers["Range"] = range_header
            req = self._client.build_request(
                "GET", MEDIA_URL.format(id=file_id),
                params=_media_params(), headers=headers,
            )
            resp = await self._client.send(req, stream=True)

            if resp.status_code == 401 and not did_auth_retry:
                await resp.aclose()
                await self.tokens.force_refresh()
                did_auth_retry = True
                continue

            if resp.status_code in (403, 429):
                reason, message = await _parse_error(resp)
                await resp.aclose()
                if resp.status_code == 403 and reason in FATAL_REASONS:
                    raise _FatalQuota(reason, message)
                if reason in RATE_REASONS or resp.status_code == 429:
                    if attempt < len(BACKOFFS):
                        await asyncio.sleep(BACKOFFS[attempt])
                        attempt += 1
                        continue
                    raise _RateLimited(reason or "rateLimitExceeded", message)
                # Some other 403 (e.g. insufficient permissions)
                raise _UpstreamError(resp.status_code, reason, message)

            if resp.status_code >= 400:
                reason, message = await _parse_error(resp)
                await resp.aclose()
                raise _UpstreamError(resp.status_code, reason, message)

            return resp

    async def stream(self, file_id, request):
        """Build a StreamingResponse (206/200) proxying the Drive file.

        HEAD is handled by the caller via head(); this handles GET.
        """
        range_header = request.headers.get("range")
        try:
            upstream = await self._open_upstream(file_id, range_header)
        except _FatalQuota as e:
            return JSONResponse(
                {"error": "download_quota", "reason": e.reason, "message": e.message},
                status_code=502,
            )
        except _RateLimited as e:
            return JSONResponse(
                {"error": "rate_limited", "reason": e.reason, "message": e.message},
                status_code=502,
            )
        except _UpstreamError as e:
            return JSONResponse(
                {"error": "upstream", "status": e.status, "reason": e.reason, "message": e.message},
                status_code=502,
            )

        status = upstream.status_code  # 206 with Range, else 200
        out_headers = {}
        for h in RELAY_HEADERS:
            if h in upstream.headers:
                out_headers[h.title()] = upstream.headers[h]
        out_headers.setdefault("Accept-Ranges", "bytes")

        # No-Range 200 should carry the size; fall back to cached metadata if
        # upstream omitted Content-Length.
        if status == 200 and "Content-Length" not in out_headers:
            try:
                meta = await self.api.file_meta(file_id)
                if meta.get("size"):
                    out_headers["Content-Length"] = str(meta["size"])
            except Exception:
                pass

        async def body():
            # Hold the Mac awake for as long as we're relaying bytes. The finally
            # runs on normal completion, on client disconnect (GeneratorExit /
            # aclose) and on seek-abort, so the reference is always released.
            if self.keepawake is not None:
                self.keepawake.acquire()
            try:
                async for chunk in upstream.aiter_raw(CHUNK_SIZE):
                    yield chunk
            except (asyncio.CancelledError, httpx.RemoteProtocolError, httpx.ReadError, GeneratorExit):
                # Player seeked or disconnected — normal, not an error.
                log.debug("stream aborted for %s (client seek/disconnect)", file_id)
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.debug("stream error for %s: %r", file_id, exc)
                raise
            finally:
                if self.keepawake is not None:
                    self.keepawake.release()
                await upstream.aclose()

        return StreamingResponse(body(), status_code=status, headers=out_headers)

    async def head(self, file_id):
        """Answer a HEAD from cached metadata only — no upstream body."""
        meta = await self.api.file_meta(file_id)
        size = meta.get("size")
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": meta.get("mimeType", "application/octet-stream"),
        }
        if size:
            headers["Content-Length"] = str(size)
        return Response(status_code=200, headers=headers)


class _FatalQuota(Exception):
    def __init__(self, reason, message):
        self.reason = reason
        self.message = message


class _RateLimited(Exception):
    def __init__(self, reason, message):
        self.reason = reason
        self.message = message


class _UpstreamError(Exception):
    def __init__(self, status, reason, message):
        self.status = status
        self.reason = reason
        self.message = message
