"""Subtitle resolution tests — all synthetic, no network."""
import asyncio

import pytest

from drivecast.subtitles import SubtitleResolver, pick_sibling_sub


def _f(fid, name):
    return {"id": fid, "name": name, "mimeType": "text/plain"}


# ------------------------------------------------------- sibling matching -----

def test_pick_exact_stem_match():
    files = [_f("a", "Skyharbor.2016.1080p.srt"), _f("b", "other-release.srt")]
    assert pick_sibling_sub("Skyharbor.2016.1080p.mkv", files)["id"] == "a"


def test_pick_prefers_english_marker_on_tie():
    files = [_f("fr", "Skyharbor.2016.fr.srt"), _f("en", "Skyharbor.2016.en.srt")]
    # both share the stem prefix; the English marker wins
    assert pick_sibling_sub("Skyharbor.2016.mkv", files)["id"] == "en"


def test_pick_matches_episode_marker():
    files = [_f("e1", "Show.S01E01.srt"), _f("e2", "Show.S01E02.srt")]
    assert pick_sibling_sub("Show.S01E02.1080p.WEB.mkv", files)["id"] == "e2"


def test_pick_lone_sub_without_signal():
    # A single unnamed .srt in a one-movie folder is trusted.
    files = [_f("s", "2_English.srt")]
    assert pick_sibling_sub("The.Year.Earth.Changed.2021.mkv", files)["id"] == "s"


def test_pick_none_when_ambiguous_without_signal():
    files = [_f("x", "random1.srt"), _f("y", "random2.srt")]
    assert pick_sibling_sub("Completely.Different.mkv", files) is None


def test_pick_ignores_non_subs_and_junk():
    files = [_f("j", "._Skyharbor.srt"), _f("v", "Skyharbor.mkv"), _f("t", "notes.txt")]
    assert pick_sibling_sub("Skyharbor.mkv", files) is None


# ------------------------------------------------------- resolver flow --------

class _FakeAPI:
    def __init__(self, files=None, data=b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"):
        self.files = files or []
        self.data = data
        self.browse_calls = []

    async def browse(self, drive_id, folder_id=None, page_token=None,
                     page_size=200, kinds=("video",)):
        self.browse_calls.append((drive_id, folder_id, kinds))
        return {"files": self.files, "nextPageToken": None}

    async def fetch_file_bytes(self, file_id, max_size=2 * 1024 * 1024):
        return self.data


def test_resolver_caches_drive_sibling(tmp_path):
    api = _FakeAPI(files=[_f("subid", "Skyharbor.2016.en.srt")])
    r = SubtitleResolver(api, subs_dir=str(tmp_path))
    path = asyncio.run(r.resolve("vid1", "Skyharbor.2016.mkv", "drv", "folder"))
    assert path and path.endswith(".srt")
    assert open(path, "rb").read().startswith(b"1\n")
    assert api.browse_calls == [("drv", "folder", ("subs",))]
    # Second resolve: served from cache, no further Drive calls.
    path2 = asyncio.run(r.resolve("vid1", "Skyharbor.2016.mkv", "drv", "folder"))
    assert path2 == path
    assert len(api.browse_calls) == 1
    assert r.cached("vid1") == path


def test_resolver_none_without_parent_or_key(tmp_path):
    r = SubtitleResolver(_FakeAPI(files=[]), subs_dir=str(tmp_path))
    assert asyncio.run(r.resolve("vid2", "Some.Movie.mkv", None, None)) is None


def test_resolver_opensubtitles_fallback(tmp_path, monkeypatch):
    # No sibling sub -> OpenSubtitles search + download, parsed from fake HTTP.
    api = _FakeAPI(files=[])

    class _Resp:
        def __init__(self, status, payload=None, content=b""):
            self.status_code = status
            self._payload = payload
            self.content = content

        def json(self):
            return self._payload

    class _FakeHTTP:
        def __init__(self):
            self.calls = []

        async def get(self, url, params=None, headers=None, follow_redirects=False):
            self.calls.append(("GET", url))
            if url.endswith("/subtitles"):
                assert params["languages"] == "en"
                return _Resp(200, {"data": [
                    {"attributes": {"language": "de", "files": [{"file_id": 1}]}},
                    {"attributes": {"language": "en", "files": [{"file_id": 42}]}},
                ]})
            return _Resp(200, content=b"WEBVTT-ish srt bytes")

        async def post(self, url, json=None, headers=None):
            self.calls.append(("POST", url))
            assert json == {"file_id": 42}
            return _Resp(200, {"link": "https://dl.example/sub.srt"})

        async def aclose(self):
            pass

    r = SubtitleResolver(api, opensubtitles_key="k123", subs_dir=str(tmp_path))
    r._client = _FakeHTTP()
    path = asyncio.run(r.resolve("vid3", "Skyharbor.2016.1080p.mkv", "drv", "folder"))
    assert path and open(path, "rb").read() == b"WEBVTT-ish srt bytes"
    kinds = [c for c in r._client.calls]
    assert ("POST", "https://api.opensubtitles.com/api/v1/download") in kinds


def test_resolver_survives_os_error(tmp_path):
    class _BoomHTTP:
        async def get(self, *a, **k):
            raise RuntimeError("network down")

        async def aclose(self):
            pass

    r = SubtitleResolver(_FakeAPI(files=[]), opensubtitles_key="k",
                         subs_dir=str(tmp_path))
    r._client = _BoomHTTP()
    assert asyncio.run(r.resolve("vid4", "X.mkv", "drv", "folder")) is None
