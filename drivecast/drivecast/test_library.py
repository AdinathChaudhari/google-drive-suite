"""Tests for the library: classification, grouping, diff, backoff, scan.

All synthetic — no Drive API is ever contacted.
"""
import asyncio
import os

import pytest

from drivecast import config, library, sections
from drivecast.drive_api import FOLDER_MIME, DriveAPI, DriveAPIError


@pytest.fixture(autouse=True)
def _reset_tabs_cache(monkeypatch):
    """Isolate the sections.tabs() lazy cache per test (mirrors test_sections.py
    and conftest's plugin-cache reset) and give every test a live
    "entertainment" tab for free — most scan tests exercise plain
    movie/show scanning and shouldn't have to build a tab list just to
    assign a drive to it. Tests that need courses/podcasts/plugin tabs
    override with their own monkeypatch.setattr(sections, "_tabs", [...]).
    """
    monkeypatch.setattr(sections, "_tabs", [
        {"key": "entertainment", "label": "Entertainment", "icon": "🍿",
         "behavior": "entertainment"},
    ])
    yield


def _ent(*drive_ids):
    """drive_sections mapping every given drive id to the default "entertainment" tab."""
    return {d: "entertainment" for d in drive_ids}


def _tab(key, behavior, label=None, icon="📁", **extra):
    """A minimal validated-shape tab record for monkeypatching sections._tabs."""
    d = {"key": key, "label": label or key.title(), "icon": icon, "behavior": behavior}
    d.update(extra)
    return d


# ------------------------------------------------------------- helpers --------

def vid(fid, name, size=1000, dur=None, parent="p", ancestors=()):
    return {"id": fid, "name": name, "size": size, "duration_ms": dur,
            "parent_id": parent, "ancestors": list(ancestors)}


def node(name, videos, subfolders=(), fid="folder1", drive="drv1"):
    """Build a tree node. `subfolders` is a list of child NODES (nested)."""
    return {"id": fid, "name": name, "drive_id": drive,
            "videos": list(videos), "subfolders": list(subfolders)}


def rawfile(fid, name, mime="video/mp4", size=1000, dur=None, thumb=None):
    f = {"id": fid, "name": name, "mimeType": mime, "size": str(size)}
    if dur is not None:
        f["videoMediaMetadata"] = {"durationMillis": str(dur)}
    if thumb is not None:
        f["thumbnailLink"] = thumb
    return f


def rawfolder(fid, name):
    return {"id": fid, "name": name, "mimeType": FOLDER_MIME}


# ------------------------------------------------------- classify: movie ------

def only(recs):
    """Assert a single record was produced and return it."""
    assert len(recs) == 1, recs
    return recs[0]


def test_classify_single_movie_folder_titled_from_folder():
    n = node("Paper Lantern (2016) [BluRay] [1080p]",
             [vid("v1", "Paper.Lantern.2016.1080p.BluRay.mkv", size=5000, dur=360000)])
    rec = only(library.classify_node(n))
    assert rec["type"] == "movie"
    assert rec["title"] == "Paper Lantern"   # from the FOLDER name
    assert rec["year"] == 2016
    assert rec["id"] == "v1"             # id == the video's file_id
    assert rec["file_id"] == "v1"
    assert rec["folder_id"] == "folder1"
    assert rec["duration_ms"] == 360000
    assert rec["size"] == 5000


def test_leaf_folder_multiple_videos_expands_per_file():
    # A leaf folder with >1 main video -> one movie per file (file-named),
    # and sample/trailer files are ignored.
    n = node("Double Feature", [
        vid("s", "Movie-sample.mkv", size=9999),        # sample: excluded
        vid("t", "Movie-trailer.mp4", size=8888),       # trailer: excluded
        vid("a", "Dust Rider Iron Road 2015 1080p.mkv", size=5000),
        vid("b", "Riverline 2015 1080p.mkv", size=1000),
    ])
    recs = library.classify_node(n)
    by_id = {r["file_id"]: r for r in recs}
    assert set(by_id) == {"a", "b"}          # only the two real videos
    assert all(r["type"] == "movie" for r in recs)
    assert by_id["a"]["title"] == "Dust Rider Iron Road"   # from the FILE name
    assert by_id["b"]["title"] == "Riverline"


def test_empty_folder_returns_no_records():
    assert library.classify_node(node("Empty", [])) == []
    # Folder with only a sample is effectively empty.
    assert library.classify_node(node("OnlySample", [vid("s", "movie-sample.mkv")])) == []


# ------------------------------------------------ classify: recursion ---------

def test_container_of_movie_folders_expands_to_one_tile_each():
    # A collection folder ("Phase 1") of enumerated single-movie subfolders must
    # become one tile per movie, NOT a single "Phase 1" tile.
    alloy = node("01) Alloy Man (2008) [1080p]",
                [vid("f1", "01.Alloy Man (2008) 1080p.mkv")], fid="alloyF")
    brute = node("02) The Colossal Brute (2008) [1080p]",
                [vid("f2", "02.The Colossal Brute (2008) 1080p.mkv")], fid="bruteF")
    phase = node("Phase 1", [], subfolders=[alloy, brute], fid="phaseF")
    recs = library.classify_node(phase)
    titles = {r["title"] for r in recs}
    assert titles == {"Alloy Man", "The Colossal Brute"}
    assert "Phase 1" not in titles
    assert {r["file_id"] for r in recs} == {"f1", "f2"}


def test_movie_featurettes_attached():
    # A movie folder with a Featurettes subfolder is still ONE movie, but its
    # bonus clips ride along on the record's `extras` field (not discarded).
    extras = node("Featurettes", [vid("x", "Behind the scenes.mkv", parent="exF")],
                  fid="exF")
    movie = node("Dusk - Rebirth (2004) [1080p]",
                 [vid("m", "Dusk.Rebirth.2004.1080p.mkv")],
                 subfolders=[extras], fid="duskF")
    rec = only(library.classify_node(movie))
    assert rec["type"] == "movie"
    assert rec["title"] == "Dusk - Rebirth"
    assert rec["file_id"] == "m"
    assert [e["file_id"] for g in rec["extras"] for e in g["episodes"]] == ["x"]
    assert rec["extras"][0]["name"] == "Featurettes"
    assert rec["extras"][0]["extras"] is True


def test_movie_discard_subfolder_still_dropped():
    # Sample/Subs subfolders contribute nothing — no extras, still one movie.
    subs = node("Subs", [vid("s1", "movie.en.srt.mkv", parent="sb")], fid="sb")
    movie = node("Skyharbor (2016) [1080p]",
                 [vid("m", "Skyharbor.2016.1080p.mkv")],
                 subfolders=[subs], fid="skyF")
    rec = only(library.classify_node(movie))
    assert rec["type"] == "movie"
    assert rec.get("extras") in (None, [])


def test_broadened_bonus_folders_attach():
    # The widened vocabulary ("Special Features", "Making Of") is recognised.
    sf = node("Special Features", [vid("s1", "Cast interview.mkv", parent="sfF")],
              fid="sfF")
    mk = node("Making Of", [vid("m1", "On set.mkv", parent="mkF")], fid="mkF")
    movie = node("Some Film (2010) [1080p]",
                 [vid("m", "Some.Film.2010.1080p.mkv")],
                 subfolders=[sf, mk], fid="sfilmF")
    rec = only(library.classify_node(movie))
    names = {g["name"] for g in rec["extras"]}
    assert names == {"Special Features", "Making Of"}


def test_collection_extras_fan_out():
    # A collection folder with three film subfolders + a SHARED Featurettes
    # folder: three movie records, each carrying the identical (deep-copied,
    # not shared-reference) extras group.
    m1 = node("Film One (1995) [1080p]", [vid("a", "Film.One.1995.1080p.mkv")], fid="f1F")
    m2 = node("Film Two (2004) [1080p]", [vid("b", "Film.Two.2004.1080p.mkv")], fid="f2F")
    m3 = node("Film Three (2013) [1080p]", [vid("c", "Film.Three.2013.1080p.mkv")], fid="f3F")
    feats = node("Featurettes", [
        vid("x1", "Making Of.mkv", parent="ftF"),
        vid("x2", "Cast Reunion.mkv", parent="ftF"),
    ], fid="ftF")
    coll = node("A Trilogy", [], subfolders=[m1, m2, m3, feats], fid="collF")
    recs = library.classify_node(coll)
    assert {r["title"] for r in recs} == {"Film One", "Film Two", "Film Three"}
    assert all(r["type"] == "movie" for r in recs)
    for r in recs:
        assert {e["file_id"] for g in r["extras"] for e in g["episodes"]} == {"x1", "x2"}
    # Deep-copied, not the same list object across records.
    assert recs[0]["extras"] is not recs[1]["extras"]


def test_collection_film_own_extras_preserved_alongside_shared():
    # A film with its OWN Featurettes inside a collection that also has a shared
    # one keeps both (its own is not clobbered by the fan-out).
    own = node("Featurettes", [vid("o1", "Director commentary.mkv", parent="ownF")],
               fid="ownF")
    m1 = node("Film One (1995) [1080p]",
              [vid("a", "Film.One.1995.1080p.mkv")], subfolders=[own], fid="f1F")
    m2 = node("Film Two (2004) [1080p]", [vid("b", "Film.Two.2004.1080p.mkv")], fid="f2F")
    shared = node("Extras", [vid("s1", "Trilogy retrospective.mkv", parent="shF")],
                  fid="shF")
    coll = node("A Duology", [], subfolders=[m1, m2, shared], fid="collF")
    recs = library.classify_node(coll)
    by_title = {r["title"]: r for r in recs}
    f1_ids = [e["file_id"] for g in by_title["Film One"]["extras"] for e in g["episodes"]]
    assert set(f1_ids) == {"o1", "s1"}          # its own + the shared extra
    f2_ids = [e["file_id"] for g in by_title["Film Two"]["extras"] for e in g["episodes"]]
    assert f2_ids == ["s1"]                       # only the shared extra


def test_container_with_stray_videos_and_subfolders():
    # "Hollywood": loose movie files alongside a movie subfolder -> each is a tile.
    sub = node("The Patriarch (1972)",
               [vid("g", "The.Patriarch.1972.1080p.mkv")], fid="gfF")
    hollywood = node("Hollywood", [
        vid("h1", "Drumfire 2014 1080p.mkv"),
        vid("h2", "Riverline 2015 1080p.mkv"),
    ], subfolders=[sub], fid="hwF")
    recs = library.classify_node(hollywood)
    titles = {r["title"] for r in recs}
    assert titles == {"Drumfire", "Riverline", "The Patriarch"}


def test_nested_containers_recurse_to_movies():
    # Container of a container of movie-folders.
    inner_movie = node("Dusk (1998) [1080p]",
                       [vid("bl", "Dusk.1998.1080p.mkv")], fid="blF")
    dusk_series = node("Dusk Series", [], subfolders=[inner_movie], fid="bsF")
    top = node("Hollywood", [], subfolders=[dusk_series], fid="topF")
    rec = only(library.classify_node(top))
    assert rec["title"] == "Dusk"
    assert rec["file_id"] == "bl"


# ------------------------------------------------------- classify: show -------

def test_show_by_season_subfolders():
    s1 = node("Season 1", [
        vid("e1", "Ep 01.mkv", parent="s1", ancestors=["The Ladle", "Season 1"]),
        vid("e2", "Ep 02.mkv", parent="s1", ancestors=["The Ladle", "Season 1"]),
    ], fid="s1")
    s2 = node("Season 2", [
        vid("e3", "Ep 01.mkv", parent="s2", ancestors=["The Ladle", "Season 2"]),
    ], fid="s2")
    rec = only(library.classify_node(node("The Ladle", [], subfolders=[s1, s2])))
    assert rec["type"] == "show"
    seasons = {s["season"]: s for s in rec["seasons"]}
    assert set(seasons) == {1, 2}
    assert len(seasons[1]["episodes"]) == 2
    assert [s["season"] for s in rec["seasons"]] == [1, 2]  # ascending


def test_show_by_flat_sxxexx_episodes():
    videos = [
        vid("e2", "Brewing.Storm.S05E02.mkv"),
        vid("e1", "Brewing.Storm.S05E01.mkv"),
        vid("e3", "Brewing.Storm.S05E10.mkv"),
    ]
    rec = only(library.classify_node(node("Brewing Storm", videos)))
    assert rec["type"] == "show"
    eps = rec["seasons"][0]["episodes"]
    assert [e["episode"] for e in eps] == [1, 2, 10]  # sorted by episode number
    assert rec["seasons"][0]["season"] == 5


def test_single_episode_is_still_a_movie_heuristic():
    # One episode-named file and no season folder -> falls through to movie.
    rec = only(library.classify_node(node("Random", [vid("v", "Random.S01E01.mkv")])))
    assert rec["type"] == "movie"


def test_episode_title_extracted():
    videos = [
        vid("a", "Quillson (1993) - S05E10 - Where Every Kettle Sings.mkv"),
        vid("b", "Quillson (1993) - S05E11 - Perspectives.mkv"),
    ]
    rec = only(library.classify_node(node("Quillson", videos)))
    eps = rec["seasons"][0]["episodes"]
    assert eps[0]["title"] == "Where Every Kettle Sings"


# ------------------------------------------------------- classify: loose ------

def test_loose_sxxexx_files_group_into_show():
    loose = [
        rawfile("e1", "The Branch S03E01 720p.mkv"),
        rawfile("e2", "The Branch S03E02 720p.mkv"),
        rawfile("m1", "Drumfire 2014 1080p.mkv"),
    ]
    recs = library.classify_loose("drv1", loose)
    shows = [r for r in recs if r["type"] == "show"]
    movies = [r for r in recs if r["type"] == "movie"]
    assert len(shows) == 1
    assert shows[0]["title"] == "The Branch"
    assert shows[0]["id"].startswith("loose:")
    assert len(shows[0]["seasons"][0]["episodes"]) == 2
    assert len(movies) == 1 and movies[0]["title"] == "Drumfire"
    assert movies[0]["id"] == "m1"


def test_loose_sample_excluded():
    recs = library.classify_loose("drv1", [rawfile("s", "Movie-sample.mkv")])
    assert recs == []


# ------------------------------------------------------------- diff -----------

def test_diff_add_remove():
    old = {"a": {"id": "a"}, "b": {"id": "b"}}
    new = {"b": {"id": "b"}, "c": {"id": "c"}}
    added, removed = library.diff_library(old, new)
    assert added == ["c"]
    assert removed == ["a"]


def test_movie_record_has_quality_from_filename():
    n = node("Some Folder", [vid("v1", "Skyharbor.2016.2160p.UHD.BluRay.mkv")])
    rec = only(library.classify_node(n))
    assert rec["quality"] == "4K"


def test_movie_record_quality_falls_back_to_folder_name():
    # No quality in the file name, but the folder carries it.
    n = node("Skyharbor (2016) 1080p BluRay", [vid("v1", "Skyharbor.mkv")])
    rec = only(library.classify_node(n))
    assert rec["quality"] == "1080p"


def test_clean_show_name_strips_two_word_tv_show_prefix():
    # "TV Show // <name>" is the drive convention; the two-word prefix must be
    # stripped so the tile title isn't literally "TV Show // Grimwold".
    assert library._clean_show_name("TV Show // Grimwold") == "Grimwold"
    assert library._clean_show_name(
        "TV Show // Grimwold; Ashvale; Thornwood") == "Grimwold; Ashvale; Thornwood"
    assert library._clean_show_name(
        "TV Show // The '90s Kids (Part 1)") == "The '90s Kids"
    # Single-token prefixes still strip.
    assert library._clean_show_name("Movie // The Feature") == "The Feature"
    assert library._clean_show_name("TV | Grimwold") == "Grimwold"
    # A plain drive named "TV Shows" (no separator) must NOT collapse to "".
    assert library._clean_show_name("TV Shows") == "TV Shows"


def test_show_record_uses_best_episode_quality():
    s1 = node("Season 1", [
        vid("e1", "Show.S01E01.720p.mkv", ancestors=["Show", "Season 1"]),
        vid("e2", "Show.S01E02.1080p.mkv", ancestors=["Show", "Season 1"]),
        vid("e3", "Show.S01E03.480p.mkv", ancestors=["Show", "Season 1"]),
    ], fid="s1")
    rec = only(library.classify_node(node("Show", [], subfolders=[s1])))
    assert rec["type"] == "show"
    assert rec["quality"] == "1080p"   # best of 720p/1080p/480p


def test_loose_records_carry_quality():
    loose = [
        rawfile("e1", "The Branch S03E01 2160p.mkv"),
        rawfile("e2", "The Branch S03E02 720p.mkv"),
        rawfile("m1", "Drumfire 2014 1080p.mkv"),
    ]
    recs = library.classify_loose("drv1", loose)
    show = next(r for r in recs if r["type"] == "show")
    movie = next(r for r in recs if r["type"] == "movie")
    assert show["quality"] == "4K"      # best across the two episodes
    assert movie["quality"] == "1080p"


def test_grouped_show_carries_best_member_quality():
    m1 = _showrec("b1", "dBL", "Grimwold Season 1 S01", 1, 2)
    m1["quality"] = "SD"
    m2 = _showrec("b2", "dBL", "Grimwold Season 2 S02", 2, 2)
    m2["quality"] = "1080p"
    out = library.group_seasons([m1, m2], {"dBL": "TV | Grimwold"})
    assert len(out) == 1
    assert out[0]["quality"] == "1080p"


def test_assign_added_at_sets_and_preserves():
    old = {"a": {"id": "a", "added_at": 100.0}}
    new = {"a": {"id": "a"}, "b": {"id": "b"}}
    library.assign_added_at(old, new, now=555.0)
    assert new["a"]["added_at"] == 100.0   # preserved for existing title
    assert new["b"]["added_at"] == 555.0   # stamped for newly-added title


def test_merge_existing_metadata_carries_poster():
    old = {"a": {"id": "a", "poster": "p.jpg", "tmdb_id": 5, "overview": "o"}}
    new = {"a": {"id": "a", "poster": None, "tmdb_id": None, "overview": None}}
    library.merge_existing_metadata(old, new)
    assert new["a"]["poster"] == "p.jpg"
    assert new["a"]["tmdb_id"] == 5


def test_prune_removed_posters(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path))
    p1 = tmp_path / "gone.jpg"
    p2 = tmp_path / "shared.jpg"
    p1.write_bytes(b"x")
    p2.write_bytes(b"y")
    old = {"a": {"poster": "gone.jpg"}, "b": {"poster": "shared.jpg"}}
    new = {"c": {"poster": "shared.jpg"}}  # still references shared.jpg
    library.prune_removed_posters(old, new, ["a", "b"])
    assert not p1.exists()   # orphaned -> deleted
    assert p2.exists()       # still referenced -> kept


# ------------------------------------------------------- backoff --------------

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def get(self, url, params=None, headers=None):
        r = self.responses[self.calls]
        self.calls += 1
        return r


class _FakeTokens:
    async def get_token(self):
        return "tok"

    async def force_refresh(self):
        return "tok"


def test_get_retries_on_rate_limit_then_succeeds():
    rate = _Resp(403, {"error": {"errors": [{"reason": "rateLimitExceeded"}], "message": "slow down"}})
    ok = _Resp(200, {"files": [], "nextPageToken": None})
    api = DriveAPI(_FakeTokens(), lambda: [], backoffs=(0, 0, 0))
    api._client = _FakeClient([rate, ok])
    result = asyncio.run(api._get("http://x", {}))
    assert result == {"files": [], "nextPageToken": None}
    assert api._client.calls == 2  # retried once, did not crash


def test_get_raises_after_backoffs_exhausted():
    rate = _Resp(429, {"error": {"message": "too many"}})
    api = DriveAPI(_FakeTokens(), lambda: [], backoffs=(0,))
    api._client = _FakeClient([rate, rate])
    with pytest.raises(DriveAPIError):
        asyncio.run(api._get("http://x", {}))


# ------------------------------------------------------- scan end-to-end ------

class _FakeScanAPI:
    """Serves a synthetic folder tree; records that no real network is used."""

    def __init__(self, tree):
        self.tree = tree  # folder_id -> [raw file/folder dicts]
        self.seeded = []

    async def browse(self, drive_id, folder_id=None, page_token=None, page_size=200,
                     kinds=("video",)):
        key = folder_id or drive_id
        return {"files": self.tree.get(key, []), "nextPageToken": None}

    def seed_meta(self, meta):
        self.seeded.append(meta)


class _DisabledTMDB:
    enabled = False


def _cache(tmp_path):
    """A ScanCache stored in the test's tmp dir (shared within one test)."""
    from drivecast.scan_cache import ScanCache
    return ScanCache(path=str(tmp_path / "scan_cache.json"))


def test_scan_builds_library_without_network(tmp_path):
    tree = {
        "drv1": [
            rawfolder("movieF", "Skyharbor (2016)"),
            rawfolder("showF", "The Ladle"),
            rawfile("loose", "Drumfire 2014 1080p.mkv", size=42, dur=6000),
        ],
        "movieF": [rawfile("mv", "Skyharbor.2016.1080p.mkv", size=9000, dur=7200000)],
        "showF": [rawfolder("s1", "Season 1")],
        "s1": [
            rawfile("s1e1", "The.Ladle.S01E01.mkv", dur=1500000),
            rawfile("s1e2", "The.Ladle.S01E02.mkv", dur=1600000),
        ],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0, cache=_cache(tmp_path))
    status = asyncio.run(scanner.scan(["drv1"], drive_sections=_ent("drv1")))
    assert status["error"] is None
    assert status["running"] is False
    titles = {t["title"]: t for t in lib.titles_list()}
    assert set(titles) == {"Skyharbor", "The Ladle", "Drumfire"}
    assert titles["Skyharbor"]["type"] == "movie"
    assert titles["The Ladle"]["type"] == "show"
    assert len(titles["The Ladle"]["seasons"][0]["episodes"]) == 2
    # File index / meta seeding populated for playback without a Drive call.
    assert lib.file_info("mv")["duration_ms"] == 7200000


def test_scan_survives_folder_rate_limit(tmp_path):
    class _FlakyAPI(_FakeScanAPI):
        async def browse(self, drive_id, folder_id=None, page_token=None, page_size=200,
                         kinds=("video",)):
            if folder_id == "badF":
                raise DriveAPIError(403, "rate", "rateLimitExceeded")
            return await super().browse(drive_id, folder_id, page_token, page_size)

    tree = {
        "drv1": [rawfolder("badF", "Broken"), rawfolder("goodF", "Skyharbor (2016)")],
        "goodF": [rawfile("mv", "Skyharbor.2016.mkv", size=10)],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FlakyAPI(tree), _DisabledTMDB(), lib, throttle=0, cache=_cache(tmp_path))
    status = asyncio.run(scanner.scan(["drv1"], drive_sections=_ent("drv1")))
    # A folder failure invalidates the whole drive's scan (a PARTIAL result
    # must never become the cached truth) — scan never crashes, error recorded.
    assert status["running"] is False
    assert lib.titles_list() == []           # nothing cached from a partial walk
    assert status["error"] is not None

    # Once the folder recovers, a rescan lands everything.
    tree_ok = {k: v for k, v in tree.items()}
    tree_ok["drv1"] = [rawfolder("goodF", "Skyharbor (2016)")]
    scanner2 = library.Scanner(_FakeScanAPI(tree_ok), _DisabledTMDB(), lib, throttle=0,
                               cache=_cache(tmp_path))
    asyncio.run(scanner2.scan(["drv1"], drive_sections=_ent("drv1")))
    assert [t["title"] for t in lib.titles_list()] == ["Skyharbor"]


# --------------------------------------------- drive thumbnail fallback --------

class _ThumbScanAPI(_FakeScanAPI):
    """Fake scan API that also serves thumbnail bytes and records fetches."""

    def __init__(self, tree):
        super().__init__(tree)
        self.thumb_fetches = []

    async def fetch_thumbnail(self, url):
        self.thumb_fetches.append(url)
        return b"jpeg-bytes"


class _FakeTMDB:
    """Enabled TMDB stub that always resolves a poster."""
    enabled = True

    async def enrich(self, title, year=None, media_type="movie"):
        return {"tmdb_id": 1, "title": title, "year": year,
                "poster_key": "tmdb.jpg", "overview": "o"}


def _thumb_tree():
    return {
        "drv1": [rawfolder("movieF", "Skyharbor (2016)")],
        "movieF": [rawfile("mv", "Skyharbor.2016.mkv", size=10,
                           thumb="https://lh3.example/thumb=s220")],
    }


def test_scan_falls_back_to_drive_thumbnail(tmp_path, monkeypatch):
    # TMDB disabled: the poster comes from the video's own Drive thumbnail,
    # downloaded into POSTERS_DIR under a stable dthumb_* key.
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    api = _ThumbScanAPI(_thumb_tree())
    scanner = library.Scanner(api, _DisabledTMDB(), lib, throttle=0, cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"], drive_sections=_ent("drv1")))
    rec = lib.titles_list()[0]
    assert rec["poster"] and rec["poster"].startswith("dthumb_")
    assert os.path.exists(os.path.join(config.POSTERS_DIR, rec["poster"]))
    assert "_thumb" not in rec          # transient key never persisted
    assert api.thumb_fetches            # thumbnail actually fetched


def test_scan_prefers_tmdb_poster_over_drive_thumbnail(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    api = _ThumbScanAPI(_thumb_tree())
    scanner = library.Scanner(api, _FakeTMDB(), lib, throttle=0, cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"], drive_sections=_ent("drv1")))
    rec = lib.titles_list()[0]
    assert rec["poster"] == "tmdb.jpg"
    assert api.thumb_fetches == []      # fallback never triggered


def test_scan_thumbnail_failure_leaves_poster_none(tmp_path, monkeypatch):
    class _NoThumbAPI(_ThumbScanAPI):
        async def fetch_thumbnail(self, url):
            return None

    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_NoThumbAPI(_thumb_tree()), _DisabledTMDB(), lib, throttle=0, cache=_cache(tmp_path))
    status = asyncio.run(scanner.scan(["drv1"], drive_sections=_ent("drv1")))
    assert status["error"] is None
    rec = lib.titles_list()[0]
    assert rec["poster"] is None
    assert "_thumb" not in rec


def test_rescan_upgrades_dthumb_to_tmdb_poster(tmp_path, monkeypatch):
    # First scan without TMDB leaves a dthumb fallback; enabling TMDB and
    # rescanning must upgrade to the real poster (and delete the old file).
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    asyncio.run(library.Scanner(_ThumbScanAPI(_thumb_tree()), _DisabledTMDB(),
                                lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"], drive_sections=_ent("drv1")))
    rec = lib.titles_list()[0]
    old_file = os.path.join(config.POSTERS_DIR, rec["poster"])
    assert rec["poster"].startswith("dthumb_") and os.path.exists(old_file)

    asyncio.run(library.Scanner(_ThumbScanAPI(_thumb_tree()), _FakeTMDB(),
                                lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"], drive_sections=_ent("drv1")))
    rec = lib.titles_list()[0]
    assert rec["poster"] == "tmdb.jpg"
    assert not os.path.exists(old_file)  # superseded fallback cleaned up


class _DownloadingTMDB:
    """Enabled TMDB stub that materialises the poster file on disk (like the
    real download) and counts enrich() calls."""
    enabled = True

    def __init__(self):
        self.calls = 0

    async def enrich(self, title, year=None, media_type="movie"):
        self.calls += 1
        os.makedirs(config.POSTERS_DIR, exist_ok=True)
        with open(os.path.join(config.POSTERS_DIR, "tmdb.jpg"), "wb") as f:
            f.write(b"img")
        return {"tmdb_id": 1, "title": title, "year": year,
                "poster_key": "tmdb.jpg", "overview": "o", "genre_ids": []}


def test_rescan_redownloads_missing_tmdb_poster_file(tmp_path, monkeypatch):
    # A title already carries a TMDB poster KEY but its cached image file was
    # pruned (e.g. the title was reclassified into another tab) — a rescan must
    # re-resolve and re-materialise the file, not leave a permanent placeholder.
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    tmdb = _DownloadingTMDB()

    def scan_once():
        asyncio.run(library.Scanner(_ThumbScanAPI(_thumb_tree()), tmdb, lib,
                    throttle=0, cache=_cache(tmp_path)
                    ).scan(["drv1"], drive_sections=_ent("drv1")))

    scan_once()
    rec = lib.titles_list()[0]
    pf = os.path.join(config.POSTERS_DIR, rec["poster"])
    assert rec["poster"] == "tmdb.jpg" and os.path.exists(pf)
    calls_after_first = tmdb.calls

    os.remove(pf)                         # poster file goes missing, key stays
    scan_once()
    rec = lib.titles_list()[0]
    assert rec["poster"] == "tmdb.jpg"
    assert os.path.exists(pf)             # re-downloaded on rescan
    assert tmdb.calls > calls_after_first  # the dangling key was re-resolved


def test_tmdb_match_without_artwork_keeps_dthumb(tmp_path, monkeypatch):
    class _NoArtTMDB:
        enabled = True

        async def enrich(self, title, year=None, media_type="movie"):
            return {"tmdb_id": 7, "title": title, "year": year,
                    "poster_key": None, "overview": "o"}

    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    asyncio.run(library.Scanner(_ThumbScanAPI(_thumb_tree()), _DisabledTMDB(),
                                lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"], drive_sections=_ent("drv1")))
    dthumb = lib.titles_list()[0]["poster"]
    assert dthumb.startswith("dthumb_")

    asyncio.run(library.Scanner(_ThumbScanAPI(_thumb_tree()), _NoArtTMDB(),
                                lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"], drive_sections=_ent("drv1")))
    rec = lib.titles_list()[0]
    assert rec["poster"] == dthumb          # fallback survives
    assert rec["tmdb_id"] == 7              # metadata still enriched


def test_rescan_restores_missing_dthumb_file(tmp_path, monkeypatch):
    # If the cached fallback file is deleted, a rescan re-downloads it instead
    # of leaving the record pointing at a 404ing key.
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    asyncio.run(library.Scanner(_ThumbScanAPI(_thumb_tree()), _DisabledTMDB(),
                                lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"], drive_sections=_ent("drv1")))
    poster_file = os.path.join(config.POSTERS_DIR, lib.titles_list()[0]["poster"])
    os.remove(poster_file)

    api = _ThumbScanAPI(_thumb_tree())
    asyncio.run(library.Scanner(api, _DisabledTMDB(), lib, throttle=0, cache=_cache(tmp_path)).scan(["drv1"], drive_sections=_ent("drv1")))
    assert api.thumb_fetches                 # re-downloaded
    assert os.path.exists(poster_file)


def test_fetch_thumbnail_falls_back_when_bumped_request_raises():
    import httpx

    class _Img:
        status_code = 200
        content = b"img"

    class _FlakyThumbClient:
        def __init__(self):
            self.calls = []

        async def get(self, url, params=None, headers=None, follow_redirects=False):
            self.calls.append(url)
            if len(self.calls) == 1:
                raise httpx.ReadTimeout("slow")   # bumped =s640 times out
            return _Img()

    api = DriveAPI(_FakeTokens(), lambda: [])
    client = _FlakyThumbClient()
    api._client = client
    data = asyncio.run(api.fetch_thumbnail("https://lh3.example/abc=s220"))
    assert data == b"img"
    assert client.calls[0].endswith("=s640")     # tried the bumped size first
    assert client.calls[1].endswith("=s220")     # then fell back to original


def test_title_for_file_maps_movies_and_episodes(tmp_path):
    lib = library.Library(path=str(tmp_path / "library.json"))
    lib.replace({
        "movieA": {"id": "movieA", "type": "movie", "title": "Skyharbor",
                   "file_id": "fA", "size": 1, "duration_ms": None},
        "showB": {"id": "showB", "type": "show", "title": "The Ladle",
                  "seasons": [{"season": 1, "episodes": [
                      {"title": "System", "episode": 1, "file_id": "fE1",
                       "name": "e1.mkv", "duration_ms": None, "size": 1,
                       "parent_id": "s1"}]}]},
    })
    assert lib.title_for_file("fA")["id"] == "movieA"
    assert lib.title_for_file("fE1")["id"] == "showB"
    assert lib.title_for_file("nope") is None


# ------------------------------------------------- season grouping (v2) --------

def _showrec(fid, drive, folder_name, season, n_eps, year=None):
    """A classified show record for one season folder (with _folder_name)."""
    eps = [{"title": "Episode %d" % i, "episode": i, "file_id": "%se%d" % (fid, i),
            "name": "ep%d.mkv" % i, "duration_ms": None, "size": 1, "parent_id": fid}
           for i in range(1, n_eps + 1)]
    return {"id": fid, "type": "show", "title": folder_name, "year": year,
            "drive_id": drive, "folder_id": fid, "poster": None, "tmdb_id": None,
            "overview": None, "_folder_name": folder_name,
            "seasons": [{"season": season, "episodes": eps}]}


def test_group_prefixed_season_folders():
    # "Grimwold Season 1 S01" siblings -> one "Grimwold" show.
    recs = [_showrec("b1", "dBL", "Grimwold Season 1 S01", 1, 6),
            _showrec("b2", "dBL", "Grimwold Season 2 S02", 2, 6),
            _showrec("b3", "dBL", "Grimwold Specials", 0, 2)]
    out = library.group_seasons(recs, {"dBL": "TV | Grimwold"})
    assert len(out) == 1
    show = out[0]
    assert show["type"] == "show" and show["title"] == "Grimwold"
    assert sorted(s["season"] for s in show["seasons"]) == [0, 1, 2]
    assert show["id"].startswith("grp:")


def test_group_bare_season_folders_uses_drive_name():
    recs = [_showrec("f1", "dFR", "Season 1", 1, 24),
            _showrec("f2", "dFR", "Season 2", 2, 24)]
    out = library.group_seasons(recs, {"dFR": "Quillson"})
    assert len(out) == 1 and out[0]["title"] == "Quillson"
    assert sorted(s["season"] for s in out[0]["seasons"]) == [1, 2]


def test_bare_season_drives_stay_separate_shows():
    # Two Part drives, each with bare "Season N" folders, no longer merge:
    # drive-is-the-show groups key on the immutable drive id (rename-stable),
    # so two same-named such drives stay two distinct shows.
    recs = [_showrec("a", "dM1", "Season 1", 1, 16),
            _showrec("b", "dM2", "Season 5", 5, 22)]
    names = {"dM1": "TV | Milo in the Muddle (Part 1)",
             "dM2": "TV | Milo in the Muddle (Part 2)"}
    out = library.group_seasons(recs, names)
    assert len(out) == 2
    assert {r["title"] for r in out} == {"Milo in the Muddle"}
    assert {r["drive_id"] for r in out} == {"dM1", "dM2"}
    assert len({r["id"] for r in out}) == 2   # distinct, drive-id-derived


def test_group_leaves_range_named_wrapper_untouched():
    # "The Branch Season 1-9 S01-s09" is a whole-series wrapper, not one season.
    wrapper = {"id": "off", "type": "show", "title": "The Branch", "year": 2005,
              "drive_id": "dTV", "folder_id": "off", "poster": None, "tmdb_id": None,
              "overview": None, "_folder_name": "The Branch Season 1-9 S01-s09",
              "seasons": [{"season": n, "episodes": []} for n in range(1, 10)]}
    out = library.group_seasons([wrapper], {"dTV": "TV Shows"})
    assert len(out) == 1 and out[0]["id"] == "off"
    assert "_folder_name" not in out[0]  # transient key stripped


def test_group_passthrough_strips_transient_keys():
    movie = {"id": "mv", "type": "movie", "title": "Skyharbor", "year": 2016,
             "drive_id": "d", "folder_id": "mv", "file_id": "mv", "size": 10,
             "duration_ms": None, "poster": None, "tmdb_id": None, "overview": None,
             "_folder_name": "Skyharbor (2016)", "_video_name": "Skyharbor.2016.mkv"}
    out = library.group_seasons([movie], {"d": "Movies"})
    assert len(out) == 1 and out[0]["id"] == "mv" and out[0]["type"] == "movie"
    assert "_folder_name" not in out[0] and "_video_name" not in out[0]


# --------------------------------------------------- extras / featurettes ------

def test_show_featurettes_subfolder_becomes_extras_season():
    # Show/Featurettes videos become a labelled pseudo-season, NOT season-1
    # episodes mixed in with the real ones.
    feats = node("Featurettes", [
        vid("f1", "Behind the Kettle.mkv", parent="ft",
            ancestors=["Quillson", "Featurettes"]),
        vid("f2", "Celebrity Voices.mkv", parent="ft",
            ancestors=["Quillson", "Featurettes"]),
    ], fid="ft")
    s1 = node("Season 1", [
        vid("e1", "Quillson S01E01.mkv", parent="s1", ancestors=["Quillson", "Season 1"]),
        vid("e2", "Quillson S01E02.mkv", parent="s1", ancestors=["Quillson", "Season 1"]),
    ], fid="s1")
    rec = only(library.classify_node(node("Quillson", [], subfolders=[s1, feats])))
    assert rec["type"] == "show"
    real = [s for s in rec["seasons"] if not s.get("extras")]
    extras = [s for s in rec["seasons"] if s.get("extras")]
    assert len(real) == 1 and len(real[0]["episodes"]) == 2
    assert len(extras) == 1
    assert extras[0]["name"] == "Featurettes"
    assert [e["file_id"] for e in extras[0]["episodes"]] == ["f1", "f2"]
    assert rec["seasons"][-1] is extras[0]  # extras listed after real seasons


def test_discard_folders_dropped_and_featurette_filenames_kept():
    # Subs/Sample folder videos vanish; a file NAMED "...featurette..." inside
    # a bonus folder is content, not junk.
    feats = node("Featurettes", [
        vid("f1", "Making Of featurette.mkv", parent="ft"),
        vid("f2", "Episode 1 sample.mkv", parent="ft"),  # real sample: dropped
    ], fid="ft")
    subs = node("Subs", [vid("x1", "S01E01.forced.mkv", parent="sb")], fid="sb")
    s1 = node("Season 1", [
        vid("e1", "Show S01E01.mkv", parent="s1"),
        vid("e2", "Show S01E02.mkv", parent="s1"),
    ], fid="s1")
    rec = only(library.classify_node(node("Show", [], subfolders=[s1, feats, subs])))
    all_ids = [e["file_id"] for s in rec["seasons"] for e in s["episodes"]]
    assert "x1" not in all_ids and "f2" not in all_ids
    extras = [s for s in rec["seasons"] if s.get("extras")]
    assert [e["file_id"] for e in extras[0]["episodes"]] == ["f1"]


def test_root_featurettes_folder_attaches_to_drive_show():
    # The real-world layout: a drive that IS the show (bare Season N folders)
    # with a sibling Featurettes/Season N tree. No separate tile survives.
    s1 = node("Season 1", [
        vid("e1", "Quillson S01E01.mkv", parent="s1", ancestors=["Season 1"]),
        vid("e2", "Quillson S01E02.mkv", parent="s1", ancestors=["Season 1"]),
    ], fid="s1")
    s2 = node("Season 2", [
        vid("e3", "Quillson S02E01.mkv", parent="s2", ancestors=["Season 2"]),
        vid("e4", "Quillson S02E02.mkv", parent="s2", ancestors=["Season 2"]),
    ], fid="s2")
    feat = node("Featurettes", [], subfolders=[
        node("Season 1", [vid("f1", "Behind the Kettle.mkv", parent="fs1",
                              ancestors=["Featurettes", "Season 1"])], fid="fs1"),
        node("Season 2", [vid("f2", "And Then There Was Pip.mkv", parent="fs2",
                              ancestors=["Featurettes", "Season 2"])], fid="fs2"),
    ], fid="feat")
    records = (library.classify_node(s1) + library.classify_node(s2)
               + library.classify_node(feat))
    main, extras = library.split_extras_records(records)
    assert [r["id"] for r in extras] == ["feat"]
    out = library.attach_extras(
        library.group_seasons(main, {"drv1": "Quillson"}), extras)
    assert len(out) == 1
    show = out[0]
    assert show["title"] == "Quillson"
    real = [s for s in show["seasons"] if not s.get("extras")]
    ex = [s for s in show["seasons"] if s.get("extras")]
    assert [s["season"] for s in real] == [1, 2]
    assert [s["name"] for s in ex] == ["Featurettes · Season 1",
                                       "Featurettes · Season 2"]
    assert [e["file_id"] for s in ex for e in s["episodes"]] == ["f1", "f2"]


def test_root_extras_leaf_with_loose_clips_attaches_as_one_entry():
    # An extras folder holding loose clips classifies as one movie per file;
    # attach folds them back into a single labelled entry on the show.
    show = only(library.classify_node(node("Quillson", [
        vid("e1", "Quillson S01E01.mkv"),
        vid("e2", "Quillson S01E02.mkv"),
    ], fid="sh")))
    clips = library.classify_node(node("Extras", [
        vid("c2", "Clip 2.mkv", parent="ex"),
        vid("c1", "Clip 1.mkv", parent="ex"),
    ], fid="ex"))
    main, extras = library.split_extras_records([show] + clips)
    assert len(extras) == 2  # one movie record per clip
    out = library.attach_extras(library.group_seasons(main, {}), extras)
    assert len(out) == 1
    ex = [s for s in out[0]["seasons"] if s.get("extras")]
    assert len(ex) == 1 and ex[0]["name"] == "Extras"
    assert [e["name"] for e in ex[0]["episodes"]] == ["Clip 1.mkv", "Clip 2.mkv"]
    assert [e["episode"] for e in ex[0]["episodes"]] == [1, 2]


def test_extras_stay_a_tile_when_drive_show_is_ambiguous():
    # Two equally-small shows, extras season overlaps both: a genuine near-tie
    # (no strict season-overlap winner, no 2x episode lead) stays a tile.
    a = only(library.classify_node(node("Show A", [
        vid("a1", "Show A S01E01.mkv"), vid("a2", "Show A S01E02.mkv")], fid="A")))
    b = only(library.classify_node(node("Show B", [
        vid("b1", "Show B S01E01.mkv"), vid("b2", "Show B S01E02.mkv")], fid="B")))
    feat = only(library.classify_node(node("Featurettes", [
        vid("f1", "Bloopers S01E01.mkv"), vid("f2", "Bloopers S01E02.mkv")],
        fid="ft")))
    main, extras = library.split_extras_records([a, b, feat])
    out = library.attach_extras(library.group_seasons(main, {}), extras)
    assert {r["id"] for r in out} == {"A", "B", "ft"}
    tile = next(r for r in out if r["id"] == "ft")
    assert "_folder_name" not in tile  # leftover tile is still persistable


def test_drive_root_single_movie_extras_attach():
    # A drive root with exactly one loose movie + a root Featurettes folder:
    # the clips fold onto that movie's `extras` field, no separate tile.
    movie = only(library.classify_loose(
        "drv1", [rawfile("m", "Some Film 2010 1080p.mkv", size=5000)]))
    clips = library.classify_node(node("Featurettes", [
        vid("c2", "Clip 2.mkv", parent="ex"),
        vid("c1", "Clip 1.mkv", parent="ex"),
    ], fid="ex"))
    main, extras = library.split_extras_records([movie] + clips)
    out = library.attach_extras(library.group_seasons(main, {}), extras)
    assert len(out) == 1
    tgt = out[0]
    assert tgt["type"] == "movie" and tgt["file_id"] == "m"
    assert len(tgt["extras"]) == 1 and tgt["extras"][0]["name"] == "Featurettes"
    assert [e["name"] for e in tgt["extras"][0]["episodes"]] == ["Clip 1.mkv", "Clip 2.mkv"]


def test_drive_root_multi_movie_extras_leftover():
    # Two loose movies + a root Featurettes folder: owner is ambiguous, so the
    # Featurettes stays a standalone tile (no incorrect attach, no crash).
    movies = library.classify_loose("drv1", [
        rawfile("m1", "Film A 2001 1080p.mkv"),
        rawfile("m2", "Film B 2002 1080p.mkv"),
    ])
    clips = library.classify_node(node("Featurettes", [
        vid("c1", "Clip 1.mkv", parent="ex")], fid="ex"))
    main, extras = library.split_extras_records(movies + clips)
    out = library.attach_extras(library.group_seasons(main, {}), extras)
    assert {r["file_id"] for r in out} == {"m1", "m2", "c1"}
    for r in out:
        assert not r.get("extras")


def test_root_featurettes_attaches_to_dominant_show_among_two():
    # A drive with a dominant seven-season show (grouped "Season N" folders) + a
    # smaller second show + a root "Featurettes" folder whose season subfolders
    # {1,2,3,11} tie both shows on overlap. Episode dominance (21 vs 6, >= 2x)
    # folds the featurettes into the dominant show; the second show is untouched;
    # no standalone Featurettes tile survives.
    big = [node("Harborview Season %d" % s,
                [vid("hv%d_%d" % (s, e), "Harborview S%02dE%02d.mkv" % (s, e))
                 for e in range(1, 4)], fid="hv%d" % s)
           for s in range(1, 8)]
    small = node("Night Market", [
        vid("nm%d_%d" % (s, e), "Night Market S%02dE%02d.mkv" % (s, e))
        for s in range(1, 4) for e in range(1, 3)], fid="nm")
    feat = node("Featurettes", [], subfolders=[
        node("Season 1", [vid("g1", "BTS 1.mkv", parent="gs1",
                              ancestors=["Featurettes", "Season 1"])], fid="gs1"),
        node("Season 2", [vid("g2", "BTS 2.mkv", parent="gs2",
                              ancestors=["Featurettes", "Season 2"])], fid="gs2"),
        node("Season 3", [vid("g3", "BTS 3.mkv", parent="gs3",
                              ancestors=["Featurettes", "Season 3"])], fid="gs3"),
        node("Season 11", [vid("g11", "BTS 11.mkv", parent="gs11",
                               ancestors=["Featurettes", "Season 11"])], fid="gs11"),
    ], fid="feat")
    records = []
    for f in big + [small, feat]:
        records += library.classify_node(f)
    main, extras = library.split_extras_records(records)
    assert [r["id"] for r in extras] == ["feat"]
    out = library.attach_extras(library.group_seasons(main, {}), extras)
    assert not any(r.get("id") == "feat" for r in out)   # no stray tile
    dominant = next(r for r in out
                    if len([s for s in r["seasons"] if not s.get("extras")]) == 7)
    ex = [s for s in dominant["seasons"] if s.get("extras")]
    assert [s["name"] for s in ex] == [
        "Featurettes · Season 1", "Featurettes · Season 2",
        "Featurettes · Season 3", "Featurettes · Season 11"]
    second = next(r for r in out
                  if len([s for s in r["seasons"] if not s.get("extras")]) == 3)
    assert not any(s.get("extras") for s in second["seasons"])  # second untouched


def test_root_extras_seasons_match_one_show():
    # Equal-sized shows (dominance ties) with disjoint season ranges: a
    # "Featurettes/Season 4" attaches to the show that HAS a season 4, purely
    # on season overlap.
    a = only(library.classify_node(node("Show A", [
        vid("a1", "Show A S01E01.mkv"), vid("a2", "Show A S02E01.mkv")], fid="A")))
    b = only(library.classify_node(node("Show B", [
        vid("b1", "Show B S04E01.mkv"), vid("b2", "Show B S05E01.mkv")], fid="B")))
    feat = only(library.classify_node(node("Featurettes", [], subfolders=[
        node("Season 4", [vid("g1", "Clip.mkv", parent="gs4",
                              ancestors=["Featurettes", "Season 4"])], fid="gs4")],
        fid="ft")))
    main, extras = library.split_extras_records([a, b, feat])
    out = library.attach_extras(library.group_seasons(main, {}), extras)
    assert not any(r.get("id") == "ft" for r in out)
    b_out = next(r for r in out if r["title"] == "Show B")
    # A single extras season keeps the bare folder label (the "· Season N"
    # suffix only distinguishes multiple), and it went to B (has season 4).
    b_extras = [s for s in b_out["seasons"] if s.get("extras")]
    assert [s["name"] for s in b_extras] == ["Featurettes"]
    assert [e["file_id"] for s in b_extras for e in s["episodes"]] == ["g1"]
    a_out = next(r for r in out if r["title"] == "Show A")
    assert not any(s.get("extras") for s in a_out["seasons"])


def test_root_extras_near_tie_stays_tile():
    # Comparable shows sharing seasons 1-3: neither season overlap nor a 2x
    # episode lead decides (10 vs 9 eps), so the Featurettes stays its own tile.
    a = only(library.classify_node(node("Show A", [
        vid("a%d" % e, "Show A S%02dE%02d.mkv" % ((e - 1) // 4 + 1, (e - 1) % 4 + 1))
        for e in range(1, 11)], fid="A")))     # 10 eps across seasons 1-3
    b = only(library.classify_node(node("Show B", [
        vid("b%d" % e, "Show B S%02dE%02d.mkv" % ((e - 1) // 3 + 1, (e - 1) % 3 + 1))
        for e in range(1, 10)], fid="B")))     # 9 eps across seasons 1-3
    feat = only(library.classify_node(node("Featurettes", [], subfolders=[
        node("Season 1", [vid("g1", "Clip 1.mkv", parent="gs1")], fid="gs1"),
        node("Season 2", [vid("g2", "Clip 2.mkv", parent="gs2")], fid="gs2")],
        fid="ft")))
    main, extras = library.split_extras_records([a, b, feat])
    out = library.attach_extras(library.group_seasons(main, {}), extras)
    assert {r["id"] for r in out} == {"A", "B", "ft"}
    for r in out:
        assert not any(s.get("extras") for s in r.get("seasons", []))


def test_movie_extras_playable_via_rebuild_index(tmp_path):
    # A movie's featurette file must be resolvable for playback/HEAD + history.
    movie = {
        "id": "m", "type": "movie", "title": "Some Film", "file_id": "m",
        "size": 5000, "duration_ms": 360000, "drive_id": "drv1", "folder_id": "fF",
        "extras": [{
            "season": 1, "name": "Featurettes", "extras": True,
            "episodes": [{
                "title": "Making Of", "episode": 1, "file_id": "x1",
                "name": "Making Of.mkv", "duration_ms": 60000, "size": 100,
                "parent_id": "ftF",
            }],
        }],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    lib.replace({"m": movie})
    info = lib.file_info("x1")
    assert info is not None
    assert info["name"] == "Making Of.mkv"
    assert info["drive_id"] == "drv1" and info["parent_id"] == "ftF"
    owner = lib.title_for_file("x1")
    assert owner is not None and owner["id"] == "m"


def test_member_season_folder_extras_survive_group_seasons():
    # "<Show> Season N" folder with its own Featurettes subfolder: the merge
    # keeps the featurettes as a labelled pseudo-season of the merged show.
    n = node("Quillson Season 2", [
        vid("e1", "Quillson S02E01.mkv", parent="s2"),
        vid("e2", "Quillson S02E02.mkv", parent="s2"),
    ], subfolders=[node("Featurettes", [
        vid("f1", "And Then There Was Pip.mkv", parent="ft")], fid="ft")],
        fid="s2")
    rec = only(library.classify_node(n))
    out = library.group_seasons([rec], {})
    assert len(out) == 1
    show = out[0]
    assert show["title"] == "Quillson"
    real = [s for s in show["seasons"] if not s.get("extras")]
    ex = [s for s in show["seasons"] if s.get("extras")]
    assert len(real) == 1 and real[0]["season"] == 2
    assert [e["file_id"] for e in real[0]["episodes"]] == ["e1", "e2"]
    assert len(ex) == 1 and ex[0]["name"] == "Featurettes · Season 2"
    assert [e["file_id"] for e in ex[0]["episodes"]] == ["f1"]


# --------------------------------------------------- per-drive refresh (M1) ----

def _two_drive_tree():
    """drv1: a movie; drv2: a movie. Independent content."""
    return {
        "drv1": [rawfolder("m1F", "Skyharbor (2016)")],
        "m1F": [rawfile("mv1", "Skyharbor.2016.1080p.mkv", size=10)],
        "drv2": [rawfolder("m2F", "Riverline (2015)")],
        "m2F": [rawfile("mv2", "Riverline.2015.1080p.mkv", size=10)],
    }


def test_partial_refresh_equals_full_refresh(tmp_path):
    tree = _two_drive_tree()
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1", "drv2"], drive_sections=_ent("drv1", "drv2")))
    full = {t["id"]: t for t in lib.titles_list()}
    assert set(full) == {"mv1", "mv2"}

    # Add a movie to drv2, then refresh ONLY drv2.
    tree["drv2"].append(rawfolder("m3F", "Sandsea (2021)"))
    tree["m3F"] = [rawfile("mv3", "Sandsea.2021.2160p.mkv", size=10)]
    asyncio.run(scanner.scan(["drv1", "drv2"], scope=["drv2"], drive_sections=_ent("drv1", "drv2")))
    titles = {t["id"]: t for t in lib.titles_list()}
    # drv1's title survives untouched; drv2's addition landed.
    assert set(titles) == {"mv1", "mv2", "mv3"}
    assert scanner.status["scope"] == ["drv2"]
    assert scanner.status["total"] == 1


def test_partial_refresh_preserves_bare_season_drives(tmp_path):
    # Two "Part" drives with bare "Season N" folders are now SEPARATE shows
    # (keyed by immutable drive id). A partial refresh of either must keep both,
    # with their ids unchanged.
    tree = {
        "dM1": [rawfolder("s1F", "Season 1")],
        "s1F": [rawfile("m1e1", "ep1.mkv"), rawfile("m1e2", "ep2.mkv")],
        "dM2": [rawfolder("s5F", "Season 5")],
        "s5F": [rawfile("m5e1", "ep1.mkv")],
    }

    class _NamedAPI(_FakeScanAPI):
        async def list_drives(self, force=False):
            return [{"id": "dM1", "name": "TV | Milo in the Muddle (Part 1)"},
                    {"id": "dM2", "name": "TV | Milo in the Muddle (Part 2)"}]

    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_NamedAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["dM1", "dM2"], drive_sections=_ent("dM1", "dM2")))
    recs = lib.titles_list()
    assert len(recs) == 2
    by_drive = {r["drive_id"]: r for r in recs}
    assert sorted(by_drive) == ["dM1", "dM2"]
    assert all(r["id"].startswith("grp:") for r in recs)
    ids = {d: r["id"] for d, r in by_drive.items()}

    # Refresh only Part 2: both shows survive with unchanged ids.
    asyncio.run(scanner.scan(["dM1", "dM2"], scope=["dM2"], drive_sections=_ent("dM1", "dM2")))
    recs2 = lib.titles_list()
    assert len(recs2) == 2
    assert {r["drive_id"]: r["id"] for r in recs2} == ids


def test_scan_failure_keeps_previous_titles(tmp_path):
    tree = _two_drive_tree()

    class _FailingRootAPI(_FakeScanAPI):
        fail_drive = None

        async def browse(self, drive_id, folder_id=None, page_token=None, page_size=200,
                         kinds=("video",)):
            if self.fail_drive and (folder_id or drive_id) == self.fail_drive:
                raise DriveAPIError(500, "boom", "backendError")
            return await super().browse(drive_id, folder_id, page_token, page_size)

    api = _FailingRootAPI(tree)
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(api, _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1", "drv2"], drive_sections=_ent("drv1", "drv2")))
    assert {t["id"] for t in lib.titles_list()} == {"mv1", "mv2"}

    # drv1's root now errors: a rescan must NOT drop drv1's titles.
    api.fail_drive = "drv1"
    asyncio.run(scanner.scan(["drv1", "drv2"], drive_sections=_ent("drv1", "drv2")))
    assert {t["id"] for t in lib.titles_list()} == {"mv1", "mv2"}
    assert scanner.status["error"] is not None


def test_partial_scope_escalates_when_sibling_cache_missing(tmp_path):
    # Fresh cache + a scoped request: scanning only the scope would drop every
    # uncached drive's titles, so the scan escalates to all selected drives.
    tree = _two_drive_tree()
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1", "drv2"], scope=["drv2"], drive_sections=_ent("drv1", "drv2")))
    assert scanner.status["total"] == 2                    # escalated to full
    assert set(scanner.status["scope"]) == {"drv1", "drv2"}
    assert {t["id"] for t in lib.titles_list()} == {"mv1", "mv2"}


def test_deselected_drive_pruned_from_cache_and_library(tmp_path):
    tree = _two_drive_tree()
    lib = library.Library(path=str(tmp_path / "library.json"))
    cache = _cache(tmp_path)
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=cache)
    asyncio.run(scanner.scan(["drv1", "drv2"], drive_sections=_ent("drv1", "drv2")))
    assert set(cache.drive_ids()) == {"drv1", "drv2"}

    # Deselect drv2: its titles and cache entry disappear.
    asyncio.run(scanner.scan(["drv1"], drive_sections=_ent("drv1")))
    assert {t["id"] for t in lib.titles_list()} == {"mv1"}
    assert cache.drive_ids() == ["drv1"]


def test_added_at_stable_across_partial_refresh(tmp_path):
    tree = _two_drive_tree()
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1", "drv2"], drive_sections=_ent("drv1", "drv2")))
    first = lib.get("mv1")["added_at"]
    asyncio.run(scanner.scan(["drv1", "drv2"], scope=["drv2"], drive_sections=_ent("drv1", "drv2")))
    assert lib.get("mv1")["added_at"] == first


def test_scan_cache_get_returns_deep_copies(tmp_path):
    from drivecast.scan_cache import ScanCache
    cache = ScanCache(path=str(tmp_path / "sc.json"))
    cache.put("d1", [{"id": "a", "seasons": [{"season": 1, "episodes": []}]}])
    got = cache.get("d1")
    got[0]["seasons"][0]["season"] = 99      # mutate the copy
    assert cache.get("d1")[0]["seasons"][0]["season"] == 1   # cache pristine


# ------------------------------------------------------- categorization (M2) --

def test_category_for_matrix():
    from drivecast import sections
    # No TMDB match -> other for non-shows, but a structural show never falls
    # back to "other" (it stays a "show"); a drive hint still overrides both.
    assert sections.category_for(None, "movie") == "other"
    assert sections.category_for(None, "movie", None) == "other"
    assert sections.category_for(None, "show", None) == "show"
    assert sections.category_for(None, "show") == "show"
    assert sections.category_for(None, "show", "documentary") == "documentary"
    assert sections.category_for(None, "movie", "documentary") == "documentary"
    # Genre 99 -> documentary regardless of structure.
    assert sections.category_for({"genre_ids": [18, 99]}, "movie") == "documentary"
    assert sections.category_for({"genre_ids": [99]}, "show") == "documentary"
    # Otherwise the structural type.
    assert sections.category_for({"genre_ids": [35]}, "show") == "show"
    assert sections.category_for({"genre_ids": []}, "movie") == "movie"


def test_tmdb_cache_heal_drops_genreless_entries():
    from drivecast.tmdb import TMDB
    healed = TMDB._heal_cache({
        "movie|old|2000": {"tmdb_id": 1, "poster_key": "p.jpg"},   # pre-genres
        "movie|new|2020": {"tmdb_id": 2, "genre_ids": [28]},
        "movie|miss|": None,                                        # negative marker
    })
    assert "movie|old|2000" not in healed
    assert healed["movie|new|2020"]["tmdb_id"] == 2
    assert "movie|miss|" in healed and healed["movie|miss|"] is None


class _GenreTMDB:
    """TMDB stub: 'Coastland The Varen Question' is a documentary; others match
    plain movies; 'Unknown' has no TMDB entry at all."""
    enabled = True

    async def enrich(self, title, year=None, media_type="movie"):
        if "varen" in title.lower():
            return {"tmdb_id": 9, "title": title, "year": year,
                    "poster_key": None, "overview": None, "genre_ids": [99]}
        if "unknown" in title.lower():
            return None
        return {"tmdb_id": 5, "title": title, "year": year,
                "poster_key": None, "overview": None, "genre_ids": [35]}


def test_scanner_stamps_categories(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    tree = {
        "drv1": [rawfolder("docF", "Coastland The Varen Question"),
                 rawfolder("movF", "Skyharbor (2016)"),
                 rawfolder("unkF", "Unknown Home Video")],
        "docF": [rawfile("d1", "Coastland.The.Varen.Question.S01E01.mkv"),
                 rawfile("d2", "Coastland.The.Varen.Question.S01E02.mkv")],
        "movF": [rawfile("m1", "Skyharbor.2016.1080p.mkv")],
        "unkF": [rawfile("u1", "Unknown Home Video.mp4")],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _GenreTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"], drive_sections=_ent("drv1")))
    cats = {t["title"]: t["category"] for t in lib.titles_list()}
    assert cats["Coastland The Varen Question"] == "documentary"
    assert cats["Skyharbor"] == "movie"
    assert cats["Unknown Home Video"] == "other"


def test_scanner_category_hint_overrides_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    tree = {"drv1": [rawfolder("unkF", "Unknown Nature Film")],
            "unkF": [rawfile("u1", "Unknown Nature Film.mp4")]}
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _GenreTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"], drive_sections=_ent("drv1"),
                             drive_hints={"drv1": {"category": "documentary"}}))
    assert only(lib.titles_list())["category"] == "documentary"


def test_category_carried_across_rescans(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    tree = {"drv1": [rawfolder("movF", "Skyharbor (2016)")],
            "movF": [rawfile("m1", "Skyharbor.2016.mkv")]}
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _GenreTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"], drive_sections=_ent("drv1")))
    assert only(lib.titles_list())["category"] == "movie"
    # Rescan with TMDB disabled: the category is carried, not wiped.
    scanner2 = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                               cache=_cache(tmp_path))
    asyncio.run(scanner2.scan(["drv1"], drive_sections=_ent("drv1")))
    assert only(lib.titles_list())["category"] == "movie"


def test_category_null_when_tmdb_disabled(tmp_path):
    tree = {"drv1": [rawfolder("movF", "Skyharbor (2016)")],
            "movF": [rawfile("m1", "Skyharbor.2016.mkv")]}
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"], drive_sections=_ent("drv1")))
    assert only(lib.titles_list())["category"] is None


class _MissTMDB:
    """TMDB stub that is enabled but never matches (every lookup is a miss)."""
    enabled = True

    async def enrich(self, title, year=None, media_type="movie"):
        return None


def _bare_season_tree(drive_id):
    return {
        drive_id: [rawfolder("s1F", "Season 1"), rawfolder("s2F", "Season 2")],
        "s1F": [rawfile("e1", "ep1.mkv")],
        "s2F": [rawfile("e2", "ep1.mkv")],
    }


def test_bare_season_show_survives_drive_rename(tmp_path, monkeypatch):
    # Renaming a drive-is-the-show between scans keeps the record id stable
    # (so carried metadata isn't dropped), updates the DISPLAY title to the new
    # name, and — even with no TMDB match — keeps category "show", not "other".
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))

    class _RenamableAPI(_FakeScanAPI):
        drive_name = "Frasier"

        async def list_drives(self, force=False):
            return [{"id": "dSH", "name": self.drive_name}]

    api = _RenamableAPI(_bare_season_tree("dSH"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(api, _MissTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["dSH"], drive_sections=_ent("dSH")))
    rec = only(lib.titles_list())
    assert rec["title"] == "Frasier"
    assert rec["category"] == "show"     # structural show, no TMDB fallback
    first_id = rec["id"]

    # Rename the drive and rescan: same id, new title, still "show".
    api.drive_name = "Frasier Reboot"
    asyncio.run(scanner.scan(["dSH"], drive_sections=_ent("dSH")))
    rec2 = only(lib.titles_list())
    assert rec2["id"] == first_id
    assert rec2["title"] == "Frasier Reboot"
    assert rec2["category"] == "show"


def test_stuck_other_show_self_heals_on_rescan(tmp_path, monkeypatch):
    # A show record already poisoned with category "other" (no drive-hint
    # override) heals back to "show" on the next scan, without any TMDB hit.
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))

    class _NamedAPI(_FakeScanAPI):
        async def list_drives(self, force=False):
            return [{"id": "dSH", "name": "Frasier"}]

    api = _NamedAPI(_bare_season_tree("dSH"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(api, _MissTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["dSH"], drive_sections=_ent("dSH")))
    rec = only(lib.titles_list())
    # Simulate the poisoned state persisted by the old bug.
    lib.get(rec["id"])["category"] = "other"
    assert lib.get(rec["id"])["category"] == "other"

    asyncio.run(scanner.scan(["dSH"], drive_sections=_ent("dSH")))
    healed = only(lib.titles_list())
    assert healed["id"] == rec["id"]     # same title -> id stable, no rename
    assert healed["category"] == "show"


def test_stuck_other_show_respects_drive_hint_over_self_heal(tmp_path, monkeypatch):
    # The self-heal must not override an explicit drive category hint.
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))

    class _NamedAPI(_FakeScanAPI):
        async def list_drives(self, force=False):
            return [{"id": "dSH", "name": "Frasier"}]

    api = _NamedAPI(_bare_season_tree("dSH"))
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(api, _MissTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    hints = {"dSH": {"category": "other"}}
    asyncio.run(scanner.scan(["dSH"], drive_sections=_ent("dSH"), drive_hints=hints))
    rec = only(lib.titles_list())
    lib.get(rec["id"])["category"] = "other"

    asyncio.run(scanner.scan(["dSH"], drive_sections=_ent("dSH"), drive_hints=hints))
    assert only(lib.titles_list())["category"] == "other"


# ------------------------------------------------------- sections (M3) --------

def test_migrate_library_v1_stamps_fields(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(sections, "_tabs", [_tab("courses", "courses")])
    v1 = {"version": 1, "generated_at": 1.0, "titles": {
        "movieA": {"id": "movieA", "type": "movie", "title": "Skyharbor",
                   "drive_id": "drv1", "file_id": "fA", "size": 1},
    }}
    p = tmp_path / "library.json"
    p.write_text(_json.dumps(v1))
    lib = library.Library(path=str(p), drive_sections={"drv1": "courses"})
    rec = lib.get("movieA")
    assert lib.data["version"] == library.LIBRARY_VERSION
    assert rec["section"] == "courses"
    assert rec["category"] is None
    assert rec["media"] == "video"
    assert rec["source_drives"] == ["drv1"]
    # ids unchanged; file index still works
    assert lib.title_for_file("fA")["id"] == "movieA"


def test_scan_stamps_section_and_skips_tmdb_for_non_entertainment(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    monkeypatch.setattr(sections, "_tabs", [_tab("courses", "courses")])
    tree = {"drv1": [rawfolder("cF", "Intercourse and Communication")],
            "cF": [rawfile("c1", "1 - Tactical Empathy.mp4"),
                   rawfile("c2", "2 - Mirroring.mp4")]}

    calls = []

    class _SpyTMDB:
        enabled = True

        async def enrich(self, title, year=None, media_type="movie"):
            calls.append(title)
            return {"tmdb_id": 1, "title": title, "year": year,
                    "poster_key": "wrong.jpg", "overview": None, "genre_ids": []}

    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _SpyTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"], drive_sections={"drv1": "courses"}))
    recs = lib.titles_list()
    assert recs and all(r["section"] == "courses" for r in recs)
    assert calls == []                      # TMDB never consulted for courses
    assert all(not r.get("poster") for r in recs)   # no film posters attached


def test_course_named_part1_never_merges_as_season(tmp_path, monkeypatch):
    # An entertainment drive has "X Season 1"-style folders; a COURSES drive
    # has a "(Part 1)" folder — grouping must only appl to entertainment.
    monkeypatch.setattr(sections, "_tabs", [
        _tab("entertainment", "entertainment"), _tab("courses", "courses"),
    ])
    tree = {
        "ent": [rawfolder("s1", "Grimwold Season 1 S01")],
        "s1": [rawfile("e1", "ep1.mkv"), rawfile("e2", "ep2.mkv")],
        "crs": [rawfolder("p1", "Python Data Science (Part 1)")],
        "p1": [rawfile("l1", "01) Intro.mp4"), rawfile("l2", "02) Lists.mp4")],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["ent", "crs"],
                             drive_sections={"ent": "entertainment", "crs": "courses"}))
    secs = {t["title"]: t["section"] for t in lib.titles_list()}
    assert any(s == "courses" for s in secs.values())
    course = next(t for t in lib.titles_list() if t["section"] == "courses")
    assert not course["id"].startswith("grp:")   # never season-grouped


def test_scan_dispatches_course_and_plugin_classifiers(tmp_path, monkeypatch):
    # End-to-end through Scanner: a courses drive uses the course classifier,
    # and a drive assigned to a CUSTOM plugin section uses its classifier.
    from drivecast import sections as sections_mod

    def _plugin_classify(drive_id, drive_name, nodes, loose):
        # A tiny audio-series classifier: volume folders -> named seasons.
        seasons = []
        for node in nodes:
            for i, sf in enumerate(node["subfolders"]):
                eps = [{"title": v["name"], "episode": j + 1,
                        "file_id": v["id"], "name": v["name"],
                        "duration_ms": v.get("duration_ms"), "size": v.get("size"),
                        "parent_id": v.get("parent_id"), "media": "audio"}
                       for j, v in enumerate(sf["videos"])]
                seasons.append({"season": i + 1, "name": sf["name"], "episodes": eps})
            if seasons:
                return [{"id": node["id"], "type": "show", "title": node["name"],
                         "year": None, "drive_id": drive_id, "folder_id": node["id"],
                         "poster": None, "tmdb_id": None, "overview": None,
                         "quality": None, "shelf": "Series", "media": "audio",
                         "_thumb": None, "seasons": seasons}]
        return []

    monkeypatch.setattr(sections_mod, "_plugins", {
        "myaudio": {"key": "myaudio", "label": "My Audio", "icon": "♪",
                    "mimes": ("video", "audio"), "classify": _plugin_classify},
    })
    # Tabs are data now: a tab must exist for each drive's TAB assignment,
    # even when the tab key happens to equal its behavior key (allowed —
    # tabs no longer have to dodge behavior names).
    monkeypatch.setattr(sections_mod, "_tabs", [
        _tab("courses", "courses"), _tab("myaudio", "myaudio"),
    ])

    tree = {
        # courses drive: one flat course with two numbered lessons + workbook
        "crs": [rawfolder("courseF", "Art_of_Negotiation")],
        "courseF": [
            rawfile("l1", "1 - Tactical Empathy.mp4", dur=600000),
            rawfile("l2", "2 - Mirroring.mp4", dur=650000),
            rawfile("wb", "Class Workbook.pdf", mime="application/pdf"),
        ],
        # plugin drive: one series folder with one volume of audio tracks
        "aud": [rawfolder("seriesF", "Morning Series")],
        "seriesF": [rawfolder("v1F", "Volume 01")],
        "v1F": [rawfile("t1", "01 Track One.mp3", mime="audio/mpeg"),
                rawfile("t2", "02 Track Two.mp3", mime="audio/mpeg")],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["crs", "aud"],
                             drive_sections={"crs": "courses", "aud": "myaudio"}))
    by_section = {}
    for t in lib.titles_list():
        by_section.setdefault(t["section"], []).append(t)

    course = only(by_section["courses"])
    assert course["type"] == "show"
    assert course["title"] == "Art of Negotiation"
    eps = course["seasons"][0]["episodes"]
    assert [e["episode"] for e in eps] == [1, 2]
    assert course["materials"] and course["materials"][0]["name"] == "Class Workbook.pdf"

    series = only(by_section["myaudio"])
    assert series["media"] == "audio"
    assert series["shelf"] == "Series"
    vol = series["seasons"][0]
    assert vol["name"] == "Volume 01"
    assert [e["episode"] for e in vol["episodes"]] == [1, 2]
    assert all(e.get("media") == "audio" for e in vol["episodes"])
    # plugin episodes indexed for playback + Continue enrichment
    assert lib.title_for_file("t1")["id"] == series["id"]


def test_plugin_loading_from_dir(tmp_path, monkeypatch):
    # A SECTION plugin dropped into the plugin dir loads a BEHAVIOR; junk
    # files are ignored and a broken plugin never crashes the app. A behavior
    # only shows up in all_sections()/mimes_for/meta_list once some TAB is
    # actually built on it — behaviors never render by themselves.
    from drivecast import sections as sections_mod
    plug_dir = tmp_path / "sections"
    plug_dir.mkdir()
    (plug_dir / "custom.py").write_text(
        "def classify(drive_id, drive_name, nodes, loose):\n"
        "    return []\n"
        "SECTION = {'key': 'custom', 'label': 'Custom', 'icon': '*',\n"
        "           'mimes': ('video',), 'classify': classify}\n")
    (plug_dir / "broken.py").write_text("raise RuntimeError('boom')\n")
    (plug_dir / "notaplugin.py").write_text("x = 1\n")
    monkeypatch.setattr(sections_mod, "PLUGIN_DIR", str(plug_dir))
    monkeypatch.setattr(sections_mod, "_plugins", None)   # reset the cache
    monkeypatch.setattr(sections_mod, "_tabs", [
        _tab("entertainment", "entertainment"),
        _tab("courses", "courses"),
        _tab("podcasts", "podcasts"),
        _tab("custom", "custom"),
    ])
    assert "custom" in sections_mod.all_sections()
    assert sections_mod.mimes_for("custom") == ("video",)
    # classify_for is keyed by BEHAVIOR now, not tab — "custom" the tab key
    # and "custom" the behavior key happen to be equal here, which is legal.
    assert callable(sections_mod.classify_for("custom"))
    assert sections_mod.classify_for("entertainment") is None
    metas = {m["key"] for m in sections_mod.meta_list()}
    assert metas == {"entertainment", "courses", "podcasts", "custom"}
    monkeypatch.setattr(sections_mod, "_plugins", None)   # don't leak to other tests


# --------------------------------------------------- behaviors vs. tabs (S2) --
#
# Dispatch/gating now keys off a tab's BEHAVIOR (sections.behavior_for), never
# off the tab's own key string — a user-renamed "My Courses" tab built on the
# "courses" behavior must classify/stamp exactly like a tab literally named
# "courses" always did. These tests use tab keys that deliberately do NOT
# match their behavior's name to prove that.

def test_custom_courses_tab_dispatches_course_classifier_and_stamps_tab_key(tmp_path, monkeypatch):
    # Clone of test_scan_stamps_section_and_skips_tmdb_for_non_entertainment,
    # but the tab's KEY is "my-courses" while its BEHAVIOR is "courses" —
    # dispatch must still hit classify_course_drive, and every record must be
    # stamped with the TAB key, not the behavior key.
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    monkeypatch.setattr(sections, "_tabs",
                        [_tab("my-courses", "courses", label="My Courses")])
    tree = {"drv1": [rawfolder("cF", "Art_of_Negotiation")],
            "cF": [rawfile("l1", "1 - Tactical Empathy.mp4"),
                   rawfile("l2", "2 - Mirroring.mp4"),
                   rawfile("wb", "Class Workbook.pdf", mime="application/pdf")]}

    calls = []

    class _SpyTMDB:
        enabled = True

        async def enrich(self, title, year=None, media_type="movie"):
            calls.append(title)
            return {"tmdb_id": 1, "title": title, "year": year,
                    "poster_key": "wrong.jpg", "overview": None, "genre_ids": []}

    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _SpyTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"], drive_sections={"drv1": "my-courses"}))
    recs = lib.titles_list()
    course = only(recs)
    assert course["section"] == "my-courses"          # TAB key, not "courses"
    assert course["type"] == "show"
    assert course["materials"][0]["name"] == "Class Workbook.pdf"  # course classifier ran
    assert calls == []                                # TMDB never consulted for courses
    assert not course.get("poster")


def test_custom_entertainment_tab_gets_tmdb_and_is_stamped_with_tab_key(tmp_path, monkeypatch):
    # A tab named "flix" built on the "entertainment" behavior still gets the
    # TMDB poster + category passes (gated on behavior, not on the literal
    # string "entertainment"), and its records are stamped "flix".
    monkeypatch.setattr(config, "POSTERS_DIR", str(tmp_path / "posters"))
    monkeypatch.setattr(sections, "_tabs", [_tab("flix", "entertainment", label="Flix")])
    tree = {"drv1": [rawfolder("movF", "Skyharbor (2016)")],
            "movF": [rawfile("m1", "Skyharbor.2016.1080p.mkv")]}

    class _SpyTMDB:
        enabled = True

        async def enrich(self, title, year=None, media_type="movie"):
            return {"tmdb_id": 1, "title": title, "year": year,
                    "poster_key": "tmdb.jpg", "overview": "o", "genre_ids": []}

    lib = library.Library(path=str(tmp_path / "library.json"))
    scanner = library.Scanner(_FakeScanAPI(tree), _SpyTMDB(), lib, throttle=0,
                              cache=_cache(tmp_path))
    asyncio.run(scanner.scan(["drv1"], drive_sections={"drv1": "flix"}))
    rec = only(lib.titles_list())
    assert rec["section"] == "flix"
    assert rec["poster"] == "tmdb.jpg"      # TMDB poster pass ran
    assert rec["category"] == "movie"       # TMDB category pass ran


def test_per_tab_grouping_merges_within_tab_not_across_tabs(tmp_path):
    # Same show split across two drives via "<Show> Season N" sibling
    # folders. Season-merging is bucketed PER TAB now (not per behavior): the
    # split must merge into one grp: record when both drives share a tab, but
    # must NEVER merge when the two drives sit in two different
    # entertainment-behavior tabs.
    tree = {
        "d1": [rawfolder("s1", "Big Show Season 1")],
        "s1": [rawfile("e1", "ep1.mkv"), rawfile("e2", "ep2.mkv")],
        "d2": [rawfolder("s2", "Big Show Season 2")],
        "s2": [rawfile("e3", "ep1.mkv")],
    }
    cache = _cache(tmp_path)

    # Same tab ("taba" both) -> merges into one grp: record stamped "taba".
    # (Tab keys are lowercased by validate_tabs, so lowercase-only here.)
    sections.set_tabs([_tab("taba", "entertainment"), _tab("tabb", "entertainment")])
    lib_same = library.Library(path=str(tmp_path / "lib_same.json"))
    scanner_same = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib_same,
                                   throttle=0, cache=cache)
    asyncio.run(scanner_same.scan(["d1", "d2"],
                                  drive_sections={"d1": "taba", "d2": "taba"}))
    recs_same = lib_same.titles_list()
    assert len(recs_same) == 1
    assert recs_same[0]["id"].startswith("grp:")
    assert recs_same[0]["section"] == "taba"
    assert sorted(s["season"] for s in recs_same[0]["seasons"]) == [1, 2]

    # Different tabs ("taba"/"tabb") -> never merges, stays two records, each
    # stamped with its own tab.
    lib_diff = library.Library(path=str(tmp_path / "lib_diff.json"))
    scanner_diff = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib_diff,
                                   throttle=0, cache=cache)
    asyncio.run(scanner_diff.scan(["d1", "d2"],
                                  drive_sections={"d1": "taba", "d2": "tabb"}))
    recs_diff = lib_diff.titles_list()
    # Each tab's lone "Season N" folder still groups on its own (that's
    # group_seasons' normal single-drive behavior) -- the point is the two
    # tabs' groupings never fold INTO EACH OTHER: two distinct records, two
    # distinct ids, one season each, never the combined [1, 2].
    assert len(recs_diff) == 2
    assert {r["section"] for r in recs_diff} == {"taba", "tabb"}
    assert len({r["id"] for r in recs_diff}) == 2        # distinct ids, no collision
    for r in recs_diff:
        assert [s["season"] for s in r["seasons"]] != [1, 2]   # never merged


def test_unassigned_drive_skipped_keeps_cache_contributes_no_records(tmp_path):
    # A drive included in `selected` but with no live tab assignment
    # (section_for_drive -> None) is skipped: it counts as "scanned", but
    # contributes zero records to the rebuild, and its PRIOR scan-cache entry
    # is left untouched (a later reassignment is a cheap re-walk, not a full
    # rescan from empty).
    tree = {
        "drv1": [rawfolder("movF", "Skyharbor (2016)")],
        "movF": [rawfile("m1", "Skyharbor.2016.1080p.mkv")],
        "drv2": [rawfolder("s2F", "Riverline (2015)")],
        "s2F": [rawfile("m2", "Riverline.2015.1080p.mkv")],
    }
    lib = library.Library(path=str(tmp_path / "library.json"))
    cache = _cache(tmp_path)
    scanner = library.Scanner(_FakeScanAPI(tree), _DisabledTMDB(), lib, throttle=0,
                              cache=cache)

    # First scan: both drives assigned to "entertainment" -> both land.
    asyncio.run(scanner.scan(["drv1", "drv2"], drive_sections=_ent("drv1", "drv2")))
    assert {r["id"] for r in lib.titles_list()} == {"m1", "m2"}
    assert set(cache.drive_ids()) == {"drv1", "drv2"}
    cached_drv2_before = cache.get("drv2")

    # drv2's tab assignment is removed (unassigned): rescan both, but only
    # map drv1 -> entertainment now.
    status = asyncio.run(scanner.scan(["drv1", "drv2"], drive_sections=_ent("drv1")))
    assert {r["id"] for r in lib.titles_list()} == {"m1"}    # drv2 contributed nothing
    assert cache.get("drv2") == cached_drv2_before           # cache untouched, not cleared
    assert status["warning"] and "drv2" in status["warning"]
    assert status["scanned"] == 2                            # drv2 still counted as visited


def test_migrate_library_v1_stamps_custom_tab_key(tmp_path, monkeypatch):
    # A record whose drive is assigned a user-created custom tab key (not one
    # of the three historical builtin strings) stamps that custom key.
    import json as _json
    monkeypatch.setattr(sections, "_tabs", [_tab("my-flix", "entertainment")])
    v1 = {"version": 1, "generated_at": 1.0, "titles": {
        "movieA": {"id": "movieA", "type": "movie", "title": "Skyharbor",
                   "drive_id": "drv1", "file_id": "fA", "size": 1},
    }}
    p = tmp_path / "library.json"
    p.write_text(_json.dumps(v1))
    lib = library.Library(path=str(p), drive_sections={"drv1": "my-flix"})
    rec = lib.get("movieA")
    assert rec["section"] == "my-flix"
