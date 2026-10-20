"""media_info: ffprobe normalisation, the VLC hardware policy, the cache, and
the per-request stream log line."""
import asyncio
import logging

import pytest

from drivecast import media_info
from drivecast.streaming import Streamer

ON = {"vlc_software_decode": True}


def _info(codec="h264", w=1920, h=1080, fps=24.0, mbps=5.0, pix="yuv420p"):
    return {"codec": codec, "profile": "High", "width": w, "height": h,
            "pix_fmt": pix, "fps": fps, "bitrate_mbps": mbps, "container": "mov,mp4"}


@pytest.mark.parametrize("info,disable,reason", [
    (_info(w=1920, h=800, fps=23.976, mbps=2.1), True, "h264_sd_hd_software"),
    (_info(w=1280, h=720, fps=None, mbps=None), True, "h264_sd_hd_software"),
    (_info(fps=59.94), False, "h264_too_heavy_for_software"),
    (_info(fps=24, mbps=22.0), False, "h264_too_heavy_for_software"),
    (_info(w=3840, h=2160), False, "h264_too_heavy_for_software"),
    (_info(pix="yuv420p10le"), True, "h264_sd_hd_software"),
    (_info(pix="yuv444p"), False, "h264_too_heavy_for_software"),
    (_info(codec="hevc", w=3840, h=1600), False, "hevc_keep_hw"),
    (None, False, "unknown_keep_hw"),
])
def test_vlc_policy_table(info, disable, reason):
    out = media_info.vlc_policy(info, ON)
    assert out == {"vlc_disable_hardware": disable, "reason": reason}


def test_vlc_policy_config_off_keeps_hardware():
    out = media_info.vlc_policy(_info(w=1920, h=800), {"vlc_software_decode": False})
    assert out == {"vlc_disable_hardware": False, "reason": "disabled_by_config"}


def test_vlc_policy_defaults_on_when_key_missing():
    assert media_info.vlc_policy(_info(), {})["vlc_disable_hardware"] is True


@pytest.mark.parametrize("raw,want", [
    ("24000/1001", 23.976), ("25/1", 25.0), ("0/0", None), ("", None), (None, None), ("x", None),
])
def test_parse_rate(raw, want):
    assert media_info.parse_rate(raw) == want


def test_normalise_bitrate_falls_back_to_format():
    raw = {"streams": [{"codec_name": "h264", "width": 1920, "height": 800,
                        "pix_fmt": "yuv420p", "avg_frame_rate": "24000/1001"}],
           "format": {"bit_rate": "2200000", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}
    out = media_info.normalise(raw)
    assert out["bitrate_mbps"] == 2.2
    assert out["fps"] == 23.976
    assert out["container"].startswith("mov,mp4")
    raw["streams"][0]["bit_rate"] = "1500000"
    assert media_info.normalise(raw)["bitrate_mbps"] == 1.5
    assert media_info.normalise({"streams": []}) is None


def test_cache_probes_once_and_persists(tmp_path):
    calls = []

    async def fake_probe(fid, port):
        calls.append(fid)
        await asyncio.sleep(0)
        return _info()

    path = str(tmp_path / "media_info.json")
    cache = media_info.MediaInfoCache(port=1, path=path, prober=fake_probe)

    async def run():
        # Concurrent lookups of one id share one probe.
        return await asyncio.gather(cache.get("f1"), cache.get("f1"))

    a, b = asyncio.run(run())
    assert a == b == _info()
    assert calls == ["f1"]
    # A fresh cache instance reads the persisted entry: still no new probe.
    cache2 = media_info.MediaInfoCache(port=1, path=path, prober=fake_probe)
    assert asyncio.run(cache2.get("f1")) == _info()
    assert calls == ["f1"]


def test_cache_negative_result_expires(tmp_path, monkeypatch):
    calls = []

    async def fake_probe(fid, port):
        calls.append(fid)
        return None

    cache = media_info.MediaInfoCache(port=1, path=str(tmp_path / "m.json"), prober=fake_probe)
    assert asyncio.run(cache.get("f1")) is None
    assert asyncio.run(cache.get("f1")) is None
    assert len(calls) == 1
    cache.data["f1"]["ts"] -= media_info.NEGATIVE_TTL + 1
    asyncio.run(cache.get("f1"))
    assert len(calls) == 2


def test_cache_size_change_reprobes(tmp_path):
    calls = []
    size = {"v": 100}

    async def fake_probe(fid, port):
        calls.append(fid)
        return _info()

    cache = media_info.MediaInfoCache(port=1, path=str(tmp_path / "m.json"),
                                      size_for=lambda fid: size["v"], prober=fake_probe)
    asyncio.run(cache.get("f1"))
    asyncio.run(cache.get("f1"))
    size["v"] = 200
    asyncio.run(cache.get("f1"))
    assert len(calls) == 2


# ---- per-request stream log line (streaming.py) ----

class _FakeUpstream:
    status_code = 206
    headers = {"content-length": "3", "content-type": "video/mp4"}

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def aiter_raw(self, size):
        for c in self.chunks:
            yield c

    async def aclose(self):
        self.closed = True


class _FakeRequest:
    headers = {"range": "bytes=0-"}


def test_stream_logs_one_info_line_on_completion(caplog):
    upstream = _FakeUpstream([b"a" * 1000, b"b" * 1000, b"c" * 1000])

    async def run():
        streamer = Streamer(token_manager=None, drive_api=None)

        async def fake_open(file_id, range_header):
            return upstream
        streamer._open_upstream = fake_open
        resp = await streamer.stream("abcdefghijk", _FakeRequest())
        body = b"".join([c async for c in resp.body_iterator])
        await streamer.aclose()
        return body

    with caplog.at_level(logging.INFO, logger="drivecast.streaming"):
        body = asyncio.run(run())
    assert len(body) == 3000
    assert upstream.closed
    lines = [r.getMessage() for r in caplog.records
             if r.name == "drivecast.streaming" and r.levelno == logging.INFO]
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith("stream abcdef… ")
    assert "abcdefghijk" not in line  # id is truncated
    assert "range=bytes=0-" in line and "status=206" in line
    assert "sent=0.0MB" in line and "end=complete" in line
