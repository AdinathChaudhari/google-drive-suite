"""TokenManager: get a fresh Google Drive access token via rclone.

rclone is the token authority. Running `rclone backend drives <remote>:`
forces rclone to refresh/persist its OAuth token as a side effect; we then
read the fresh access_token out of `rclone config dump`.

The token value in the dump is itself a JSON string that must be parsed a
second time (double parse) to reach access_token / expiry.
"""
import asyncio
import datetime
import json
import subprocess
import time

RCLONE = "rclone"
# Refresh this many seconds before the real expiry to avoid edge-of-expiry 401s.
EXPIRY_SKEW_SECONDS = 120


class RcloneError(Exception):
    """Raised when rclone is missing, the remote is absent, or config is encrypted."""


def _parse_expiry(expiry_str):
    """Parse an RFC3339 expiry string into an epoch float. Returns None on failure."""
    if not expiry_str:
        return None
    s = expiry_str.strip()
    # Python's fromisoformat handles offsets; normalise a trailing Z just in case.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


class TokenManager:
    """Caches a Drive access token until shortly before expiry.

    Safe for concurrent use from asyncio: an asyncio.Lock serialises refreshes
    so a stampede of requests triggers only one rclone invocation.
    """

    def __init__(self, remote):
        self.remote = remote  # e.g. "gdrive1" (no trailing colon)
        self._token = None
        self._expiry = 0.0  # epoch seconds; 0 means "unknown / must refresh"
        self._lock = asyncio.Lock()

    # ---- blocking rclone calls (run in a thread from async context) ----

    def _run_rclone_drives(self):
        """Force rclone to refresh its token by hitting the Drive backend."""
        try:
            subprocess.run(
                [RCLONE, "backend", "drives", self.remote + ":"],
                check=True, capture_output=True, text=True,
            )
        except FileNotFoundError:
            raise RcloneError("rclone not found on PATH. Install rclone (brew install rclone).")
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            raise RcloneError(
                "'rclone backend drives %s:' failed. Is the '%s' remote configured?\n%s"
                % (self.remote, self.remote, stderr)
            )

    def _read_token_from_dump(self):
        """Return (access_token, expiry_epoch) from `rclone config dump`."""
        try:
            out = subprocess.run(
                [RCLONE, "config", "dump"],
                check=True, capture_output=True, text=True,
            ).stdout
        except FileNotFoundError:
            raise RcloneError("rclone not found on PATH. Install rclone (brew install rclone).")
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            if "password" in stderr.lower() or "encrypt" in stderr.lower():
                raise RcloneError(
                    "rclone config appears to be encrypted. drivecast cannot read the "
                    "token from an encrypted config non-interactively."
                )
            raise RcloneError("'rclone config dump' failed:\n%s" % stderr)
        try:
            dump = json.loads(out)
        except ValueError:
            raise RcloneError(
                "Could not parse 'rclone config dump'. The config may be encrypted."
            )
        remote = dump.get(self.remote)
        if not remote:
            raise RcloneError(
                "rclone remote '%s' not found. Run `rclone config` to set it up." % self.remote
            )
        tok_raw = remote.get("token")
        if not tok_raw:
            raise RcloneError(
                "Remote '%s' has no OAuth token. Re-authorise with `rclone config`." % self.remote
            )
        # Double parse: the token field is a JSON string inside the JSON.
        try:
            tok = json.loads(tok_raw)
        except ValueError:
            raise RcloneError("Could not parse the OAuth token for remote '%s'." % self.remote)
        access = tok.get("access_token")
        if not access:
            raise RcloneError(
                "No access_token for remote '%s'. Re-authorise with `rclone config`." % self.remote
            )
        return access, _parse_expiry(tok.get("expiry"))

    def _refresh_blocking(self):
        """Run the full refresh synchronously; returns the new access token."""
        self._run_rclone_drives()
        access, expiry = self._read_token_from_dump()
        self._token = access
        # If expiry is unknown, treat the token as short-lived (5 min) so we
        # re-check soon rather than trusting it forever.
        self._expiry = expiry if expiry else (time.time() + 300)
        return access

    # ---- async API ----

    def _is_fresh(self):
        return bool(self._token) and time.time() < (self._expiry - EXPIRY_SKEW_SECONDS)

    async def get_token(self):
        """Return a valid access token, refreshing via rclone if needed."""
        if self._is_fresh():
            return self._token
        async with self._lock:
            # Re-check inside the lock: another coroutine may have just refreshed.
            if self._is_fresh():
                return self._token
            return await asyncio.to_thread(self._refresh_blocking)

    async def force_refresh(self):
        """Force a refresh regardless of cache (used on a 401 retry)."""
        async with self._lock:
            return await asyncio.to_thread(self._refresh_blocking)

    async def token_expiry(self):
        """Return the current cached expiry as epoch seconds (refreshing if needed)."""
        await self.get_token()
        return self._expiry

    # ---- shared-drive listing (rclone is authority for this too) ----

    def _list_drives_blocking(self):
        try:
            out = subprocess.run(
                [RCLONE, "backend", "drives", self.remote + ":"],
                check=True, capture_output=True, text=True,
            ).stdout
        except FileNotFoundError:
            raise RcloneError("rclone not found on PATH. Install rclone (brew install rclone).")
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            raise RcloneError(
                "'rclone backend drives %s:' failed. Is the '%s' remote configured?\n%s"
                % (self.remote, self.remote, stderr)
            )
        try:
            return json.loads(out)
        except ValueError:
            raise RcloneError("Could not parse the shared-drive list from rclone.")

    async def list_drives(self):
        """Return the raw shared-drive list (also refreshes the token as a side effect)."""
        return await asyncio.to_thread(self._list_drives_blocking)
