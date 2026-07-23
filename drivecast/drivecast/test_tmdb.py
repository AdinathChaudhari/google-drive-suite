"""TMDB enrichment tests.

Config paths (incl. POSTERS_DIR) are sandboxed to a temp dir by the autouse
conftest fixture, so these never touch real cached posters or hit the network
(the only method that would make a request, _download_poster, is stubbed)."""
import asyncio

from drivecast import tmdb as tmdb_mod


def test_enrich_cache_hit_redownloads_missing_poster(monkeypatch):
    """A cached positive lookup whose poster file is gone (pruned after a
    reclassify, or a download that failed after the result was cached) must be
    re-materialised on the next enrich() — otherwise the title shows a
    permanent placeholder because nothing re-fetches its poster."""
    t = tmdb_mod.TMDB("fake-key")
    calls = []

    async def fake_download(poster_path, poster_key):
        calls.append((poster_path, poster_key))

    monkeypatch.setattr(t, "_download_poster", fake_download)
    key = t._cache_key("Iron Man", 2008, "movie")
    t._cache[key] = {"tmdb_id": 1726, "title": "Iron Man", "year": "2008",
                     "poster_key": "abc.jpg", "overview": None, "genre_ids": []}

    res = asyncio.run(t.enrich("Iron Man", 2008, "movie"))
    assert res["poster_key"] == "abc.jpg"          # cached result returned
    assert calls == [("/abc.jpg", "abc.jpg")]      # and its poster re-fetched
    asyncio.run(t.aclose())


def test_enrich_cache_hit_negative_marker_does_not_download(monkeypatch):
    """A cached NEGATIVE result (None marker) is returned as-is with no poster
    work — _ensure_poster must tolerate a None cache entry."""
    t = tmdb_mod.TMDB("fake-key")
    calls = []

    async def fake_download(poster_path, poster_key):
        calls.append((poster_path, poster_key))

    monkeypatch.setattr(t, "_download_poster", fake_download)
    key = t._cache_key("Nonexistent Film", None, "movie")
    t._cache[key] = None

    res = asyncio.run(t.enrich("Nonexistent Film", None, "movie"))
    assert res is None
    assert calls == []
    asyncio.run(t.aclose())


def test_choose_prefers_title_match_over_popularity():
    """TMDB ranks by popularity; _choose must pick the real title match, not
    results[0]."""
    results = [
        {"id": 1, "title": "Racerz", "release_date": "2010-01-01"},        # popular, wrong
        {"id": 920, "title": "Racers", "release_date": "2006-01-01"},      # the actual film
    ]
    assert tmdb_mod.TMDB._choose(results, "Racers", None)["id"] == 920


def test_choose_rejects_weak_match():
    """A confidently-wrong poster is worse than a placeholder — a result whose
    title doesn't resemble the query is rejected."""
    results = [{"id": 5, "title": "Completely Unrelated Movie"}]
    assert tmdb_mod.TMDB._choose(results, "Masala Nights", 2009) is None


def test_choose_year_breaks_ties():
    """Same title, different years — the matching year wins."""
    results = [
        {"id": 1, "title": "Them", "release_date": "1990-01-01"},
        {"id": 2, "title": "Them", "release_date": "2017-09-08"},
    ]
    assert tmdb_mod.TMDB._choose(results, "Them", 2017)["id"] == 2


def test_lookup_strips_trailing_one_ordinal(monkeypatch):
    """'Racers 1' (colloquial) has no TMDB match; _lookup retries as 'Racers'."""
    t = tmdb_mod.TMDB("fake-key")

    async def fake_search(endpoint, title, year, media_type):
        if title == "Racers":
            return [{"id": 920, "title": "Racers", "release_date": "2006-01-01",
                     "poster_path": "/racers.jpg", "overview": "vroom",
                     "genre_ids": [16]}]
        return []

    async def fake_download(poster_path, poster_key):
        pass

    monkeypatch.setattr(t, "_search", fake_search)
    monkeypatch.setattr(t, "_download_poster", fake_download)
    res = asyncio.run(t._lookup("Racers 1", None, "movie"))
    assert res is not None and res["tmdb_id"] == 920
    asyncio.run(t.aclose())


def test_lookup_trims_trailing_distributor_word(monkeypatch):
    """A distributor/junk word the parser left on the title ('Golden Hour
    Criterion') is shed by progressive trailing-word trimming — no hardcoded
    name list. The match is still scored against the ORIGINAL title."""
    t = tmdb_mod.TMDB("fake-key")

    async def fake_search(endpoint, title, year, media_type):
        # TMDB only matches once the trailing "Criterion" is trimmed off.
        if title == "Golden Hour":
            return [{"id": 80, "title": "Golden Hour", "release_date": "2004-07-02",
                     "poster_path": "/gh.jpg", "overview": "walk", "genre_ids": [18]}]
        return []

    async def fake_download(poster_path, poster_key):
        pass

    monkeypatch.setattr(t, "_search", fake_search)
    monkeypatch.setattr(t, "_download_poster", fake_download)
    res = asyncio.run(t._lookup("Golden Hour Criterion", 2004, "movie"))
    assert res is not None and res["tmdb_id"] == 80
    asyncio.run(t.aclose())


# ---- ambiguity guard: "stop guessing when the year can't disambiguate" ----

def test_is_ambiguous_year_selects_unique():
    """Two same-titled films of different years: ambiguous with no year, but a
    year that uniquely picks one resolves it."""
    results = [
        {"id": 1, "title": "Them", "release_date": "1990-01-01"},
        {"id": 2, "title": "Them", "release_date": "2017-09-08"},
    ]
    assert tmdb_mod.TMDB._is_ambiguous(results, "Them", None) is True
    assert tmdb_mod.TMDB._is_ambiguous(results, "Them", 2017) is False


def test_is_ambiguous_same_year_not_a_conflict():
    """Two strong matches that agree on year aren't a year conflict."""
    results = [
        {"id": 1, "title": "Doubles", "release_date": "1988-12-09"},
        {"id": 2, "title": "Doubles", "release_date": "1988-01-01"},
    ]
    assert tmdb_mod.TMDB._is_ambiguous(results, "Doubles", None) is False


def test_lookup_ambiguous_years_returns_none(monkeypatch):
    """year=None + 2 strong title matches with DIFFERENT years -> return None
    (a placeholder) rather than confidently guessing the more popular one."""
    t = tmdb_mod.TMDB("fake-key")

    async def fake_search(endpoint, title, year, media_type):
        return [
            {"id": 1, "title": "Sherwood", "release_date": "1991-06-14",
             "poster_path": "/a.jpg"},
            {"id": 2, "title": "Sherwood", "release_date": "2010-05-14",
             "poster_path": "/b.jpg"},
        ]

    async def fake_download(poster_path, poster_key):
        pass

    monkeypatch.setattr(t, "_search", fake_search)
    monkeypatch.setattr(t, "_download_poster", fake_download)
    assert asyncio.run(t._lookup("Sherwood", None, "movie")) is None
    asyncio.run(t.aclose())


def test_lookup_single_match_still_resolves(monkeypatch):
    """A lone strong match resolves normally even with no year."""
    t = tmdb_mod.TMDB("fake-key")

    async def fake_search(endpoint, title, year, media_type):
        return [{"id": 27205, "title": "Dreamscape", "release_date": "2010-07-16",
                 "poster_path": "/dream.jpg", "overview": "dreams", "genre_ids": [28]}]

    async def fake_download(poster_path, poster_key):
        pass

    monkeypatch.setattr(t, "_search", fake_search)
    monkeypatch.setattr(t, "_download_poster", fake_download)
    res = asyncio.run(t._lookup("Dreamscape", None, "movie"))
    assert res is not None and res["tmdb_id"] == 27205
    asyncio.run(t.aclose())


# ---- poster overrides ----

def test_enrich_override_wins_before_lookup(monkeypatch):
    """A saved override is returned (with its poster re-materialised) before any
    TMDB search — _lookup must never run."""
    t = tmdb_mod.TMDB("fake-key")
    lookups, downloads = [], []

    async def fake_lookup(*a):
        lookups.append(a)
        return {"tmdb_id": 999}

    async def fake_download(poster_path, poster_key):
        downloads.append(poster_key)

    monkeypatch.setattr(tmdb_mod, "OVERRIDES_PATH", "/dev/null")  # inert save
    monkeypatch.setattr(t, "_lookup", fake_lookup)
    monkeypatch.setattr(t, "_download_poster", fake_download)
    meta = {"tmdb_id": 42, "title": "Chosen", "year": "2001",
            "poster_key": "chosen.jpg", "overview": "x", "genre_ids": []}
    t.set_override("Some Movie", "movie", meta)

    # A differently-cased/punctuated title still hits the normalized key.
    res = asyncio.run(t.enrich("some movie!", None, "movie"))
    assert res == meta
    assert lookups == []                 # never searched TMDB
    assert downloads == ["chosen.jpg"]   # override poster ensured on disk
    asyncio.run(t.aclose())


def test_override_persists_and_reloads(monkeypatch, tmp_path):
    """set_override writes atomically; a fresh instance loads it from disk."""
    path = str(tmp_path / "poster_overrides.json")
    monkeypatch.setattr(tmdb_mod, "OVERRIDES_PATH", path)
    t = tmdb_mod.TMDB("fake-key")
    meta = {"tmdb_id": 7, "title": "Foo", "year": "1999",
            "poster_key": "foo.jpg", "overview": None, "genre_ids": []}
    t.set_override("Foo", "movie", meta)

    t2 = tmdb_mod.TMDB("fake-key")
    assert t2._get_override("foo", "movie") == meta       # normalized key match
    assert t2._get_override("Foo", "tv") is None          # media_type is part of key
    asyncio.run(t.aclose())
    asyncio.run(t2.aclose())


# ---- candidate search + by_id (for the "Fix poster" picker) ----

class _FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append(url)
        return self._resp

    async def aclose(self):
        pass


def test_search_candidates_shape(monkeypatch):
    t = tmdb_mod.TMDB("fake-key")

    async def fake_search(endpoint, title, year, media_type):
        return [
            {"id": 603, "title": "The Lattice", "release_date": "1999-03-31",
             "poster_path": "/m.jpg", "overview": "neo"},
            {"id": 604, "title": "The Lattice Reloaded", "release_date": "2003-05-15",
             "poster_path": "/m2.jpg", "overview": "more"},
        ]

    downloaded = []

    async def fake_download(poster_path, poster_key):
        downloaded.append(poster_key)

    monkeypatch.setattr(t, "_search", fake_search)
    monkeypatch.setattr(t, "_download_poster", fake_download)
    cands = asyncio.run(t.search_candidates("The Lattice", "movie"))
    assert cands[0] == {"tmdb_id": 603, "title": "The Lattice", "year": "1999",
                        "poster_key": "m.jpg", "overview": "neo"}
    assert "m.jpg" in downloaded          # thumbnails pre-fetched for the picker
    asyncio.run(t.aclose())


def test_search_candidates_disabled_returns_empty():
    assert asyncio.run(tmdb_mod.TMDB("").search_candidates("anything")) == []


def test_by_id_happy_path(monkeypatch):
    t = tmdb_mod.TMDB("fake-key")
    t._client = _FakeClient(_FakeResp(200, {
        "id": 27205, "title": "Dreamscape", "release_date": "2010-07-16",
        "poster_path": "/i.jpg", "overview": "dreams",
        "genres": [{"id": 28, "name": "Action"}, {"id": 878, "name": "Sci-Fi"}],
    }))

    async def fake_download(poster_path, poster_key):
        pass

    monkeypatch.setattr(t, "_download_poster", fake_download)
    res = asyncio.run(t.by_id(27205, "movie"))
    assert res["tmdb_id"] == 27205
    assert res["poster_key"] == "i.jpg"
    assert res["year"] == "2010"
    assert res["genre_ids"] == [28, 878]   # full genre objects reduced to ids
    asyncio.run(t.aclose())


def test_by_id_not_found_returns_none():
    t = tmdb_mod.TMDB("fake-key")
    t._client = _FakeClient(_FakeResp(404, {"status_message": "not found"}))
    assert asyncio.run(t.by_id(999999, "movie")) is None
    asyncio.run(t.aclose())


def test_by_id_disabled_returns_none():
    assert asyncio.run(tmdb_mod.TMDB("").by_id(1, "movie")) is None
