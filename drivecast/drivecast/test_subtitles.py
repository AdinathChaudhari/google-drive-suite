"""Subtitle resolution tests — all synthetic, no network."""
import asyncio
import datetime

import pytest

from drivecast.subtitles import (NegativeCache, SubtitleResolver,
                                 pick_sibling_sub, sanitize_subtitle_bytes)


def _f(fid, name):
    return {"id": fid, "name": name, "mimeType": "text/plain"}


def _srt(cues=12, text="Some dialogue here", start=1):
    """A syntactically real SRT with `cues` numbered cues."""
    out = []
    for i in range(start, start + cues):
        out.append("%d\n00:%02d:%02d,000 --> 00:%02d:%02d,500\n%s\n"
                   % (i, i // 60, i % 60, i // 60, (i % 60) + 1, text))
    return "\n".join(out).encode("utf-8")


# The 263 bytes an ISP actually served instead of a subtitle file: a
# court-order notice, HTTP 200, cached forever as <file_id>.srt until the gate
# existed. Verbatim, because a paraphrase would not prove the sniffing works.
BLOCK_PAGE = (
    b'<meta name="viewport" content="width=device-width,initial-scale=1.0,'
    b'maximum-scale=1.0"/><style>body{margin:0px;padding:0px;}iframe{width:100%;'
    b'height:100%}</style><iframe src="https://www.airtel.in/court-orders/ " '
    b'width="100%" height="100%" frameborder=0></iframe>'
)

# A real 267-byte "subtitle" from a release folder: four cues, every one an
# advertisement, no dialogue at all.
ADS_ONLY = (
    b"1\n00:00:30,000 --> 00:00:36,000\nProvided by YTS.MX\n\n"
    b"2\n00:00:36,500 --> 00:00:42,000\nFind the official YIFY movies site at\n"
    b"https://YTS.MX\n\n"
    b"3\n00:30:30,000 --> 00:30:36,000\nDownloaded from YTS.MX\n\n"
    b"4\n01:00:30,000 --> 01:00:36,000\nDownload more movies for free\n"
    b"from YTS.MX\n"
)


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
    def __init__(self, files=None, data=None):
        self.files = files or []
        # A gate-passing payload by default: a 1-cue stub is now (correctly)
        # rejected as unusable, so the fixture has to look like a real track.
        self.data = _srt() if data is None else data
        self.browse_calls = []

    async def browse(self, drive_id, folder_id=None, page_token=None,
                     page_size=200, kinds=("video",)):
        self.browse_calls.append((drive_id, folder_id, kinds))
        return {"files": self.files, "nextPageToken": None}

    async def fetch_file_bytes(self, file_id, max_size=2 * 1024 * 1024):
        return self.data

    async def fetch_range(self, file_id, start, end):
        # Used by the moviehash step; content doesn't matter for these tests
        # (none of them assert on the computed hash value), only that a
        # ranged read of the right length happens.
        return b"\x00" * (end - start + 1)


def test_resolver_caches_drive_sibling(tmp_path):
    api = _FakeAPI(files=[_f("subid", "Skyharbor.2016.en.srt")])
    r = SubtitleResolver(api, subs_dir=str(tmp_path))
    path = asyncio.run(r.resolve("vid1", "Skyharbor.2016.mkv", "drv", "folder"))
    assert path and path.endswith(".srt")
    # Normalised on the way in: CRLF, UTF-8, no BOM.
    assert open(path, "rb").read().startswith(b"1\r\n00:00:01,000")
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
            return _Resp(200, content=_srt(20, "Downloaded dialogue"))

        async def post(self, url, json=None, headers=None):
            self.calls.append(("POST", url))
            assert json == {"file_id": 42}
            return _Resp(200, {"link": "https://dl.example/sub.srt"})

        async def aclose(self):
            pass

    r = SubtitleResolver(api, opensubtitles_key="k123", subs_dir=str(tmp_path))
    r._client = _FakeHTTP()
    path = asyncio.run(r.resolve("vid3", "Skyharbor.2016.1080p.mkv", "drv", "folder"))
    assert path and b"Downloaded dialogue" in open(path, "rb").read()
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


# ------------------------------------------------- OS fallback chain ----------

class _OSResp:
    def __init__(self, status, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


def test_resolver_falls_through_moviehash_500_to_text_query(tmp_path):
    # The moviehash search endpoint has forum reports of 500s -- one here
    # must fall through to the text query, not abort the whole chain.
    api = _FakeAPI(files=[])

    class _FakeHTTP:
        def __init__(self):
            self.search_params = []

        async def get(self, url, params=None, headers=None, follow_redirects=False):
            if url.endswith("/subtitles"):
                self.search_params.append(dict(params))
                if "moviehash" in params:
                    return _OSResp(500)
                assert "query" in params, "should reach the text query step"
                return _OSResp(200, {"data": [
                    {"attributes": {"language": "en", "files": [{"file_id": 7}]}},
                ]})
            return _OSResp(200, content=_srt(20, "Text query dialogue"))

        async def post(self, url, json=None, headers=None):
            assert json == {"file_id": 7}
            return _OSResp(200, {"link": "https://dl.example/sub.srt"})

        async def aclose(self):
            pass

    r = SubtitleResolver(api, opensubtitles_key="k", subs_dir=str(tmp_path))
    r._client = _FakeHTTP()
    path = asyncio.run(r.resolve("vid5", "Skyharbor.2016.mkv", "drv", "folder",
                                 size=999_999_999))
    assert path and b"Text query dialogue" in open(path, "rb").read()
    assert any("moviehash" in p for p in r._client.search_params)
    assert any("query" in p for p in r._client.search_params)
    # A 500 is not a verdict: it must not poison the negative cache.
    assert r.negative.blocked("vid5", "opensubtitles") is None


def test_resolver_prefers_tmdb_id_over_text_query_when_size_missing(tmp_path):
    # No size -> the moviehash step never runs at all (nothing to hash);
    # a tmdb_id match short-circuits before the text query is ever tried.
    api = _FakeAPI(files=[])

    class _FakeHTTP:
        def __init__(self):
            self.search_params = []

        async def get(self, url, params=None, headers=None, follow_redirects=False):
            if url.endswith("/subtitles"):
                self.search_params.append(dict(params))
                assert "moviehash" not in params
                assert "tmdb_id" in params, "text query ran despite a tmdb_id hit"
                return _OSResp(200, {"data": [
                    {"attributes": {"language": "en", "files": [{"file_id": 9}]}},
                ]})
            return _OSResp(200, content=_srt(20, "TMDB dialogue"))

        async def post(self, url, json=None, headers=None):
            assert json == {"file_id": 9}
            return _OSResp(200, {"link": "https://dl.example/sub.srt"})

        async def aclose(self):
            pass

    r = SubtitleResolver(api, opensubtitles_key="k", subs_dir=str(tmp_path))
    r._client = _FakeHTTP()
    path = asyncio.run(r.resolve("vid6", "Show.S01E02.mkv", "drv", "folder",
                                 tmdb_id=1234, season=1, episode=2))
    assert path and b"TMDB dialogue" in open(path, "rb").read()
    # Exactly one search call: the tmdb_id hit, no fallthrough to text query.
    assert r._client.search_params == [{
        "tmdb_id": "1234", "season_number": "1", "episode_number": "2",
        "languages": "en",
    }]


def test_resolver_does_not_record_not_found_when_every_step_transport_fails(tmp_path):
    # size + tmdb_id both present so all three steps run -- and all three
    # raise. The docstring's promise ("transient failures are never
    # recorded") must hold for the whole chain, not just a single attempt.
    class _AlwaysBoomHTTP:
        async def get(self, *a, **k):
            raise RuntimeError("network down")

        async def aclose(self):
            pass

    r = SubtitleResolver(_FakeAPI(files=[]), opensubtitles_key="k",
                         subs_dir=str(tmp_path))
    r._client = _AlwaysBoomHTTP()
    result = asyncio.run(r.resolve("vid7", "Show.S01E02.mkv", "drv", "folder",
                                   size=999_999_999, tmdb_id=1234, season=1,
                                   episode=2))
    assert result is None
    assert r.negative.blocked("vid7", "opensubtitles") is None


def test_rank_prefers_moviehash_match_over_download_count():
    # A hash match is an exact byte-content match; nothing about raw
    # popularity should be able to outrank it.
    huge_downloads_no_hash = {
        "attributes": {"language": "en", "download_count": 50_000_000,
                       "files": [{"file_id": 1}]},
    }
    hash_match_low_downloads = {
        "attributes": {"language": "en", "moviehash_match": True,
                       "download_count": 3, "files": [{"file_id": 2}]},
    }
    results = [huge_downloads_no_hash, hash_match_low_downloads]
    assert SubtitleResolver._rank_os_file(results) == 2
    # Order shouldn't matter -- scoring, not first-match.
    assert SubtitleResolver._rank_os_file(list(reversed(results))) == 2


def test_rank_penalises_machine_translated_below_a_plain_match():
    plain = {"attributes": {"language": "en", "download_count": 5,
                            "files": [{"file_id": 1}]}}
    machine = {"attributes": {"language": "en", "download_count": 500_000,
                              "machine_translated": True,
                              "files": [{"file_id": 2}]}}
    assert SubtitleResolver._rank_os_file([plain, machine]) == 1


def test_rank_skips_non_english_and_filterless_entries():
    non_en = {"attributes": {"language": "fr", "files": [{"file_id": 1}]}}
    no_files = {"attributes": {"language": "en", "files": []}}
    good = {"attributes": {"language": "eng", "files": [{"file_id": 3}]}}
    assert SubtitleResolver._rank_os_file([non_en, no_files, good]) == 3
    assert SubtitleResolver._rank_os_file([non_en, no_files]) is None


# ------------------------------------------------------- the quality gate -----

def test_sanitize_rejects_html_block_page():
    # The exact bytes an ISP substituted for a subtitle file.
    assert sanitize_subtitle_bytes(BLOCK_PAGE) == (None, "html_block_page")


def test_sanitize_rejects_ads_only_stub():
    out, reason = sanitize_subtitle_bytes(ADS_ONLY)
    assert (out, reason) == (None, "ads_only_or_empty")


def test_sanitize_accepts_short_sub_with_lone_credit_line():
    """The false-positive guard: one credit cue among real dialogue is normal."""
    raw = (b"1\n00:00:15,918 --> 00:00:20,918\nSubtitles by explosiveskull\n\n"
           + _srt(10, "Real dialogue", start=2))
    out, reason = sanitize_subtitle_bytes(raw)
    assert reason == "ok" and out and b"explosiveskull" in out


def test_sanitize_does_not_misdecode_bomless_utf16_lookalike():
    """A BOM-less legacy-8-bit SRT must not be decoded as UTF-16.

    This is the live-cache bug in miniature: the file is not valid UTF-8 (one
    cp1252 byte), so a UTF-16 attempt gets its turn — and BOM-less UTF-16
    "succeeds" on even-length text by pairing bytes into CJK garbage, which
    turned a real, full-length subtitle into a zero_cue reject. The payload is
    padded to an even length so UTF-16 really would decode it.
    """
    # Genuine cp1252 bytes, not a UTF-8 encoding of the same characters —
    # that difference is the whole precondition.
    raw = _srt(40, "PLACEHOLDER").replace(b"\n", b"\r\n")
    raw = raw.replace(b"PLACEHOLDER", b"caf\xe9 dialogue \x97 more")
    if len(raw) % 2:
        raw += b"\n"          # ONE byte: padding with two keeps it odd
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")           # the precondition the bug needed
    raw.decode("utf-16")              # ...and this is what used to win
    out, reason = sanitize_subtitle_bytes(raw)
    assert reason == "ok" and out and "café dialogue".encode("utf-8") in out


def test_sanitize_normalises_latin1_to_utf8_crlf():
    raw = _srt(6, "caf\xe9 na\xefve").replace(b"\n", b"\r\n")
    out, reason = sanitize_subtitle_bytes(raw)
    assert reason == "ok"
    assert out.decode("utf-8").count("\r\n") and "café naïve" in out.decode("utf-8")


def test_sanitize_rejects_binary_and_empty():
    assert sanitize_subtitle_bytes(b"") == (None, "ads_only_or_empty")
    assert sanitize_subtitle_bytes(b"\x00\x01\x02binary\x00") == (None, "undecodable")
    assert sanitize_subtitle_bytes(b"just a note, no cues") == (None, "zero_cue")


# ------------------------------------------------------- negative cache -------

def test_negative_cache_expires_by_reason(tmp_path, monkeypatch):
    import time as _time

    neg = NegativeCache(path=str(tmp_path / "neg.json"))
    neg.record("vid", "opensubtitles", "not_found")
    assert neg.blocked("vid", "opensubtitles") == "not_found"

    # not_found lives 2 days; step the clock just past that and it is gone.
    later = _time.time() + 2 * 86400 + 60

    class _Clock:
        @staticmethod
        def time():
            return later

    monkeypatch.setattr("drivecast.subtitles.time", _Clock)
    neg._data = None                      # force a re-read from disk
    assert neg.blocked("vid", "opensubtitles") is None
    assert neg.blocked("vid", "sibling") is None


def test_resolver_negative_caches_rejected_sibling_and_falls_through(tmp_path):
    """HTML from the sibling source is not cached, and is not re-fetched."""
    api = _FakeAPI(files=[_f("subid", "Movie.en.srt")], data=BLOCK_PAGE)
    r = SubtitleResolver(api, subs_dir=str(tmp_path),
                         negative=NegativeCache(path=str(tmp_path / "neg.json")))
    assert asyncio.run(r.resolve("vidX", "Movie.mkv", "drv", "folder")) is None
    assert r.cached("vidX") is None                 # nothing was written
    assert len(api.browse_calls) == 1
    assert r.negative.blocked("vidX", "sibling") == "html_block_page"
    # Second play inside the TTL does not dial the blocked source again.
    assert asyncio.run(r.resolve("vidX", "Movie.mkv", "drv", "folder")) is None
    assert len(api.browse_calls) == 1


def test_resolver_records_not_found_when_no_sibling_exists(tmp_path):
    api = _FakeAPI(files=[])
    r = SubtitleResolver(api, subs_dir=str(tmp_path),
                         negative=NegativeCache(path=str(tmp_path / "neg.json")))
    assert asyncio.run(r.resolve("vidY", "Movie.mkv", "drv", "folder")) is None
    assert r.negative.blocked("vidY", "sibling") == "not_found"
    assert asyncio.run(r.resolve("vidY", "Movie.mkv", "drv", "folder")) is None
    assert len(api.browse_calls) == 1               # skipped, not retried


def test_resolver_force_bypasses_negative_cache(tmp_path):
    api = _FakeAPI(files=[_f("subid", "Movie.en.srt")], data=BLOCK_PAGE)
    neg = NegativeCache(path=str(tmp_path / "neg.json"))
    r = SubtitleResolver(api, subs_dir=str(tmp_path), negative=neg)
    assert asyncio.run(r.resolve("vidZ", "Movie.mkv", "drv", "folder")) is None
    assert neg.blocked("vidZ", "sibling") == "html_block_page"
    # The ISP stops interfering and the real subtitle arrives on a forced retry.
    api.data = _srt(30, "Actual dialogue")
    path = asyncio.run(r.resolve("vidZ", "Movie.mkv", "drv", "folder", force=True))
    assert path and b"Actual dialogue" in open(path, "rb").read()
    assert neg.blocked("vidZ", "sibling") is None
    assert len(api.browse_calls) == 2


def test_resolver_force_keeps_existing_cache_when_retry_fails(tmp_path):
    """A forced retry that finds nothing must not strand playback subtitle-less."""
    api = _FakeAPI(files=[_f("subid", "Movie.en.srt")], data=_srt(30, "Good"))
    r = SubtitleResolver(api, subs_dir=str(tmp_path),
                         negative=NegativeCache(path=str(tmp_path / "neg.json")))
    good = asyncio.run(r.resolve("vidW", "Movie.mkv", "drv", "folder"))
    assert good
    api.data = BLOCK_PAGE
    again = asyncio.run(r.resolve("vidW", "Movie.mkv", "drv", "folder", force=True))
    assert again == good and b"Good" in open(good, "rb").read()


# ---------------------------------------- allow_opensubtitles / spend counter --
# Both of these guard behaviour the /api/subtitles route test cannot reach:
# that test stubs `resolve` wholesale, so the resolver's own gating and its
# provider-call counter were only asserted through a stub that faked them.

class _OSHappyHTTP:
    """Search hits, download succeeds. Records every call."""

    def __init__(self, remaining=None):
        self.calls = []
        self._remaining = remaining

    async def get(self, url, params=None, headers=None, follow_redirects=False):
        self.calls.append(("GET", url))
        if url.endswith("/subtitles"):
            return _OSResp(200, {"data": [
                {"attributes": {"language": "en", "download_count": 500,
                                "files": [{"file_id": 42}]}}]})
        return _OSResp(200, content=_srt(20, "Fetched dialogue"))

    async def post(self, url, json=None, headers=None):
        self.calls.append(("POST", url))
        payload = {"link": "https://dl.example/sub.srt"}
        if self._remaining is not None:
            payload["remaining"] = self._remaining
        return _OSResp(200, payload)

    async def aclose(self):
        pass


def test_resolve_allow_opensubtitles_false_skips_the_paid_step(tmp_path):
    """The TV's HEAD probe on an embedded-covered file must not spend a
    download on a redundant sidecar.

    The free sources still run -- this asserts on the Drive sibling browse
    happening while the OpenSubtitles endpoints are never touched, because
    skipping the sibling too would lose a subtitle that costs nothing.
    """
    api = _FakeAPI(files=[])          # sibling source runs, finds nothing
    r = SubtitleResolver(api, opensubtitles_key="k", subs_dir=str(tmp_path))
    r._client = _OSHappyHTTP()

    path = asyncio.run(r.resolve("vidX", "Skyharbor.2016.mkv", "drv", "folder",
                                 allow_opensubtitles=False))

    assert path is None
    assert api.browse_calls == [("drv", "folder", ("subs",))]   # free step ran
    assert r._client.calls == []                                 # paid step did not
    assert r.os_download_calls == 0

    # Default (True) on the very same resolver DOES reach OpenSubtitles --
    # proves the flag is what gated it, not the fixture.
    path = asyncio.run(r.resolve("vidY", "Skyharbor.2016.mkv", "drv", "folder"))
    assert path is not None
    assert any(url.endswith("/download") for _m, url in r._client.calls)


def test_download_counter_tracks_real_provider_calls(tmp_path):
    """os_download_calls is how the backfill queue tells a free hit from a
    spent slot. It counts the /download REQUEST, not a successful parse:
    OpenSubtitles debits the daily quota on the call itself, so an attempt
    that then fails at the file host still cost a slot."""
    api = _FakeAPI(files=[])
    r = SubtitleResolver(api, opensubtitles_key="k", subs_dir=str(tmp_path))
    r._client = _OSHappyHTTP(remaining=3)
    assert r.os_download_calls == 0

    asyncio.run(r.resolve("vidZ", "Skyharbor.2016.mkv", "drv", "folder"))
    assert r.os_download_calls == 1
    assert r.last_os_remaining == 3

    # A cached file short-circuits: no second provider call.
    asyncio.run(r.resolve("vidZ", "Skyharbor.2016.mkv", "drv", "folder"))
    assert r.os_download_calls == 1


def test_download_counter_counts_a_failed_file_host_fetch(tmp_path):
    """The quota is debited on /download even when the file host then fails,
    so the counter must not be conditional on getting usable bytes back."""
    api = _FakeAPI(files=[])

    class _DeadHostHTTP(_OSHappyHTTP):
        async def get(self, url, params=None, headers=None, follow_redirects=False):
            if url.endswith("/subtitles"):
                return await super().get(url, params, headers, follow_redirects)
            return _OSResp(404, content=b"")

    r = SubtitleResolver(api, opensubtitles_key="k", subs_dir=str(tmp_path))
    r._client = _DeadHostHTTP()

    assert asyncio.run(r.resolve("vidQ", "X.mkv", "drv", "folder")) is None
    assert r.os_download_calls == 1


# ------------------------------------------------- provider quota + glitches ---

class _StubResp:
    def __init__(self, status, payload=None, text="", content_type="application/json"):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = text.encode()
        self.headers = {"content-type": content_type}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _resolver_with_post(tmp_path, resp):
    class _HTTP:
        async def post(self, url, json=None, headers=None):
            return resp

        async def aclose(self):
            pass

    r = SubtitleResolver(_FakeAPI(files=[]), opensubtitles_key="k",
                         subs_dir=str(tmp_path),
                         negative=NegativeCache(path=str(tmp_path / "neg.json")))
    r._client = _HTTP()
    return r


def test_download_non_json_200_is_reported_not_silent(tmp_path, caplog):
    """HTTP 200 with an HTML body used to vanish into an except ValueError."""
    r = _resolver_with_post(tmp_path, _StubResp(
        200, payload=None, text="<html>edge error</html>", content_type="text/html"))
    with caplog.at_level("WARNING"):
        assert asyncio.run(r._download_os_file(4982777)) is None
    assert r.last_os_glitch == "non_json_200"
    assert r.os_download_calls == 1          # the slot really was spent
    assert any("non-JSON" in rec.message for rec in caplog.records)


def test_download_200_without_link_is_reported(tmp_path):
    r = _resolver_with_post(tmp_path, _StubResp(
        200, payload={"remaining": 3, "message": "no link for you"}))
    assert asyncio.run(r._download_os_file(1)) is None
    assert r.last_os_glitch == "no_link"
    assert r.last_os_remaining == 3


def test_download_406_records_remaining_and_utc_reset(tmp_path):
    """The quota wall carries the two facts the queue needs to park."""
    r = _resolver_with_post(tmp_path, _StubResp(406, payload={
        "requests": 5, "remaining": 0,
        "message": "You have downloaded your allowed 5 subtitles for 24h.",
        "reset_time_utc": "2026-08-24T23:59:59.000Z"}))
    assert asyncio.run(r._download_os_file(1)) is None
    assert r.last_os_remaining == 0
    expected = datetime.datetime(2026, 8, 24, 23, 59, 59,
                                 tzinfo=datetime.timezone.utc).timestamp()
    assert r.last_os_reset_utc == pytest.approx(expected, abs=1)
