"""Tests for the podcasts/videos-drive classifier. All synthetic — no network."""
from drivecast import playlists


# ------------------------------------------------------------- helpers --------

def media(fid, name, media="video", size=1000, dur=None, parent="ch", thumb=None):
    return {"id": fid, "name": name, "size": size, "duration_ms": dur,
            "parent_id": parent, "ancestors": [], "thumb": thumb, "media": media}


def node(name, videos, subfolders=(), fid="ch1", drive="drv1"):
    return {"id": fid, "name": name, "drive_id": drive,
            "videos": list(videos), "files": [], "subfolders": list(subfolders)}


def loosefile(fid, name, mime="video/mp4", size=1000, dur=None, thumb=None):
    f = {"id": fid, "name": name, "mimeType": mime, "size": str(size)}
    if dur is not None:
        f["videoMediaMetadata"] = {"durationMillis": str(dur)}
    if thumb is not None:
        f["thumbnailLink"] = thumb
    return f


def only(recs):
    assert len(recs) == 1, recs
    return recs[0]


# ----------------------------------------------------------------- tests ------

def test_channel_folder_becomes_one_show():
    n = node("My Podcast", [media("v1", "ep one.mp4"),
                            media("v2", "ep two.mp4")])
    rec = only(playlists.classify_playlist_drive("drv1", "Pods", [n], []))
    assert rec["type"] == "show"
    assert rec["id"] == "ch1"
    assert rec["title"] == "My Podcast"
    assert rec["shelf"] is None
    assert rec["quality"] is None
    assert rec["year"] is None
    assert rec["poster"] is None and rec["tmdb_id"] is None and rec["overview"] is None
    assert len(rec["seasons"]) == 1
    season = rec["seasons"][0]
    assert season["season"] == 1 and season["name"] is None
    assert len(season["episodes"]) == 2
    ep = season["episodes"][0]
    assert ep["title"] == "ep one"          # ext stripped
    assert ep["episode"] == 1
    assert ep["file_id"] == "v1"
    assert ep["name"] == "ep one.mp4"


def test_episodes_natural_sorted():
    # "file2" must sort before "file10" (numeric-aware, not lexicographic).
    n = node("Channel", [media("a", "file10.mp4"),
                         media("b", "file2.mp4"),
                         media("c", "file1.mp4")])
    rec = only(playlists.classify_playlist_drive("drv1", "P", [n], []))
    eps = rec["seasons"][0]["episodes"]
    assert [e["file_id"] for e in eps] == ["c", "b", "a"]
    assert [e["episode"] for e in eps] == [1, 2, 3]


def test_nested_subfolder_content_flattened():
    inner = node("2021", [media("x", "clip3.mp4")], fid="sub1")
    n = node("Channel", [media("y", "clip1.mp4"), media("z", "clip2.mp4")],
             subfolders=[inner])
    rec = only(playlists.classify_playlist_drive("drv1", "P", [n], []))
    eps = rec["seasons"][0]["episodes"]
    assert {e["file_id"] for e in eps} == {"x", "y", "z"}
    assert len(eps) == 3


def test_mixed_audio_video_media():
    n = node("Channel", [media("v", "talk.mp4", media="video"),
                         media("a", "talk.mp3", media="audio")])
    rec = only(playlists.classify_playlist_drive("drv1", "P", [n], []))
    assert rec["media"] == "mixed"
    eps = {e["file_id"]: e for e in rec["seasons"][0]["episodes"]}
    assert eps["a"].get("media") == "audio"
    assert "media" not in eps["v"]          # video episodes omit the key


def test_all_audio_channel():
    n = node("Audio Channel", [media("a", "1.mp3", media="audio"),
                               media("b", "2.mp3", media="audio")])
    rec = only(playlists.classify_playlist_drive("drv1", "P", [n], []))
    assert rec["media"] == "audio"
    assert all(e.get("media") == "audio" for e in rec["seasons"][0]["episodes"])


def test_record_thumb_is_first_episode_thumb():
    # First episode after sort is "1.mp4" (id b) which carries the thumb.
    n = node("Channel", [media("a", "2.mp4", thumb=None),
                         media("b", "1.mp4", thumb="http://thumb/1")])
    rec = only(playlists.classify_playlist_drive("drv1", "P", [n], []))
    assert rec["_thumb"] == "http://thumb/1"


def test_loose_file_becomes_movie_with_thumb():
    f = loosefile("m1", "Some Clip 1080p.mp4", size=5000, dur=60000,
                  thumb="http://thumb/m1")
    rec = only(playlists.classify_playlist_drive("drv1", "P", [], [f]))
    assert rec["type"] == "movie"
    assert rec["id"] == "m1" and rec["file_id"] == "m1"
    assert rec["title"] == "Some Clip"
    assert rec["quality"] == "1080p"
    assert rec["media"] == "video"
    assert rec["size"] == 5000 and rec["duration_ms"] == 60000
    assert rec["_thumb"] == "http://thumb/m1"


def test_loose_audio_file_media():
    f = loosefile("m2", "song.mp3", mime="audio/mpeg")
    rec = only(playlists.classify_playlist_drive("drv1", "P", [], [f]))
    assert rec["media"] == "audio"


def test_empty_folder_dropped():
    empty = node("Empty Channel", [], fid="e1")
    good = node("Real Channel", [media("v", "x.mp4")], fid="g1")
    recs = playlists.classify_playlist_drive("drv1", "P", [empty, good], [])
    assert [r["id"] for r in recs] == ["g1"]


def test_bracket_junk_folder_name_cleaned():
    n = node("[FreeCoursesOnline.Me] The Best Channel",
             [media("v", "ep.mp4")])
    rec = only(playlists.classify_playlist_drive("drv1", "P", [n], []))
    assert rec["title"] == "The Best Channel"
