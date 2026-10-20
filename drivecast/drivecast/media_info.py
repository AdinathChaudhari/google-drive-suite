"""Per-file video stream facts (ffprobe) + the VLC hardware-decode policy.

The Fire TV app hands playback to VLC. On the Fire TV Stick 4K Plus
(AFTMA08C15) the vendor hardware AVC decoder crashes mid-file on some H.264
files (the decoder service segfaults; VLC keeps playing audio over a frozen
frame and never falls back to software). VLC's software decoder played the
same 1080p-class H.264 file straight through the crash point at a mean ~111%
(max ~152%) of the Stick's 400% CPU. So for light H.264 we tell VLC to decode
in software; everything else (notably 4K HEVC Main10, which software cannot
keep up with on this SoC) stays on hardware. See docs/DECISIONS.md D-028.

The probe runs ffprobe against our own loopback /stream endpoint (loopback is
trusted by the auth middleware, so no token is needed), and results are cached
on disk in data/media_info.json.
"""
import asyncio
import json
import logging
import os
import shutil
import tempfile
import time

from . import config as config_mod

log = logging.getLogger("drivecast.media_info")

# Software-decode envelope. 1918x816p24 measured ~111% of 400% CPU in VLC's
# software decoder; 1080p24 at 15 Mbps is estimated <=250%. 1080p60 or
# >15 Mbps is not safe in software and keeps hardware.
SW_DECODE_MAX_WIDTH = 1920
SW_DECODE_MAX_HEIGHT = 1088
SW_DECODE_MAX_FPS = 30.5
SW_DECODE_MAX_MBPS = 15.0

PROBE_TIMEOUT = 20.0
NEGATIVE_TTL = 3600.0  # re-probe a failed probe after an hour


def parse_rate(value):
    """'24000/1001' -> 23.976; '0/0' / junk -> None."""
    if not value:
        return None
    try:
        if "/" in str(value):
            num, den = str(value).split("/", 1)
            num, den = float(num), float(den)
            if num <= 0 or den <= 0:
                return None
            return round(num / den, 3)
        rate = float(value)
        return rate if rate > 0 else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalise(data):
    """Turn raw ffprobe JSON into the flat dict the API serves, or None."""
    streams = (data or {}).get("streams") or []
    if not streams:
        return None
    s = streams[0]
    fmt = (data or {}).get("format") or {}
    bit_rate = _int_or_none(s.get("bit_rate")) or _int_or_none(fmt.get("bit_rate"))
    return {
        "codec": s.get("codec_name"),
        "profile": s.get("profile"),
        "width": _int_or_none(s.get("width")),
        "height": _int_or_none(s.get("height")),
        "pix_fmt": s.get("pix_fmt"),
        "fps": parse_rate(s.get("avg_frame_rate")),
        "bitrate_mbps": round(bit_rate / 1e6, 3) if bit_rate else None,
        "container": fmt.get("format_name"),
    }


def vlc_policy(info, cfg):
    """Decide whether VLC should skip hardware decoding for this file.

    Pure function. Unknown files keep hardware (the status quo)."""
    if not cfg.get("vlc_software_decode", True):
        return {"vlc_disable_hardware": False, "reason": "disabled_by_config"}
    if not info:
        return {"vlc_disable_hardware": False, "reason": "unknown_keep_hw"}
    codec = info.get("codec")
    if codec != "h264":
        return {"vlc_disable_hardware": False, "reason": "%s_keep_hw" % (codec or "unknown")}
    pix_fmt = info.get("pix_fmt")
    width, height = info.get("width"), info.get("height")
    fps, mbps = info.get("fps"), info.get("bitrate_mbps")
    light = ((pix_fmt is None or pix_fmt.startswith("yuv420p"))
             and width is not None and width <= SW_DECODE_MAX_WIDTH
             and height is not None and height <= SW_DECODE_MAX_HEIGHT
             and (fps is None or fps <= SW_DECODE_MAX_FPS)
             and (mbps is None or mbps <= SW_DECODE_MAX_MBPS))
    if light:
        return {"vlc_disable_hardware": True, "reason": "h264_sd_hd_software"}
    return {"vlc_disable_hardware": False, "reason": "h264_too_heavy_for_software"}


async def probe(file_id, port):
    """ffprobe the first video stream via the loopback /stream endpoint.

    Returns the normalised dict, or None on a missing binary, timeout or
    nonzero exit."""
    exe = shutil.which("ffprobe")
    if not exe:
        log.info("media-info: ffprobe not found on PATH")
        return None
    url = "http://127.0.0.1:%d/stream/%s" % (int(port), file_id)
    args = [exe, "-v", "error", "-rw_timeout", "15000000",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=codec_name,profile,level,width,height,pix_fmt,avg_frame_rate,bit_rate"
            ":format=bit_rate,format_name,duration",
            "-of", "json", url]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT)
    except (asyncio.TimeoutError, OSError) as exc:
        log.info("media-info: probe of %s… failed: %s", file_id[:6], type(exc).__name__)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return None
    if proc.returncode != 0:
        log.info("media-info: probe of %s… exited %s", file_id[:6], proc.returncode)
        return None
    try:
        return normalise(json.loads(out or b"{}"))
    except ValueError:
        return None


class MediaInfoCache:
    """file_id -> probe result, persisted to data/media_info.json.

    Negative results are cached too and re-probed after NEGATIVE_TTL.
    Concurrent lookups of the same id share one probe (per-id lock)."""

    def __init__(self, port, path=None, size_for=None, prober=None):
        self.port = port
        self.path = path or os.path.join(config_mod.DATA_DIR, "media_info.json")
        # Optional callable file_id -> size; a size change invalidates the entry.
        self.size_for = size_for or (lambda _fid: None)
        self._prober = prober
        self._locks = {}
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            d = os.path.dirname(self.path)
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".media_info-", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _fresh(self, entry, size):
        if not isinstance(entry, dict):
            return False
        if size is not None and entry.get("size") not in (None, size):
            return False
        if entry.get("info") is None:
            return time.time() - float(entry.get("ts") or 0) < NEGATIVE_TTL
        return True

    async def get(self, file_id):
        size = self.size_for(file_id)
        entry = self.data.get(file_id)
        if self._fresh(entry, size):
            return entry["info"]
        lock = self._locks.setdefault(file_id, asyncio.Lock())
        async with lock:
            entry = self.data.get(file_id)
            if self._fresh(entry, size):
                return entry["info"]
            # Looked up at call time so tests can monkeypatch media_info.probe.
            prober = self._prober or probe
            info = await prober(file_id, self.port)
            self.data[file_id] = {"info": info, "size": size, "ts": time.time()}
            self._save()
            return info
