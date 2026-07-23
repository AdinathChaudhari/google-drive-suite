"""Podcasts / videos-drive classifier: each root folder is a channel. Pure.

A podcasts drive is mostly YouTube downloads: one folder per channel, holding
arbitrarily named media files (often ``yt-dlp`` style with dates or upload
indexes), possibly nested in subfolders. The v1 rule is deliberately simple
(no real drive exists yet, so there is nothing to over-fit):

  * every root folder with >=1 playable media anywhere in its subtree becomes
    ONE ``type:"show"`` record (the channel), its whole subtree flattened into
    a single season of natural-sorted episodes;
  * empty folders yield nothing;
  * loose media at the drive root become one ``type:"movie"`` record each.

No I/O — operates on the node shape ``Scanner._walk_title`` builds and raw
Drive file dicts, exactly like the entertainment classifiers in library.py.
"""
import re

from . import naming


def _natural_key(name):
    """Numeric-aware sort key so "file2" sorts before "file10".

    Splits the (lowercased) name into alternating text / number chunks and
    compares number chunks as ints. re.split with a single capture group always
    yields text at even indices and digits at odd indices, so the chunk types
    line up positionally between any two names and the lists compare cleanly.
    """
    parts = re.split(r"(\d+)", (name or "").lower())
    return [int(p) if p.isdigit() else p for p in parts]


def _all_videos(node):
    """Flatten every media file under a node (recursively) into one list."""
    out = list(node.get("videos", []))
    for sf in node.get("subfolders", []):
        out.extend(_all_videos(sf))
    return out


def _media_kind(episodes):
    """Roll episode media values up into "video" / "audio" / "mixed"."""
    kinds = {ep.get("media", "video") for ep in episodes}
    if kinds == {"audio"}:
        return "audio"
    if kinds == {"video"}:
        return "video"
    return "mixed"


def _episode_record(video, number):
    """One episode dict from a walked media file at 1-based position `number`."""
    ep = {
        "title": naming.strip_ext(video["name"]),
        "episode": number,
        "file_id": video["id"],
        "name": video["name"],
        "duration_ms": video.get("duration_ms"),
        "size": video.get("size"),
        "parent_id": video.get("parent_id"),
    }
    if video.get("media") == "audio":
        ep["media"] = "audio"
    return ep


def _channel_record(node):
    """Build one show record for a channel folder, or None when it has no media."""
    videos = _all_videos(node)
    if not videos:
        return None
    videos.sort(key=lambda v: _natural_key(v["name"]))
    episodes = [_episode_record(v, i + 1) for i, v in enumerate(videos)]
    return {
        "id": node["id"],
        "type": "show",
        "title": naming.clean_course_title(node["name"]),
        "year": None,
        "drive_id": node.get("drive_id"),
        "folder_id": node["id"],
        "shelf": None,
        "poster": None,
        "tmdb_id": None,
        "overview": None,
        "quality": None,
        "media": _media_kind(episodes),
        "_thumb": next((v.get("thumb") for v in videos if v.get("thumb")), None),
        "seasons": [{"season": 1, "name": None, "episodes": episodes}],
    }


def _loose_movie(drive_id, f):
    """One movie record for a loose media file at the drive root."""
    title, year = naming.clean_title(f.get("name") or "")
    try:
        size = int(f.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    vmm = f.get("videoMediaMetadata") or {}
    try:
        dur = int(vmm["durationMillis"])
    except (KeyError, TypeError, ValueError):
        dur = None
    media = "audio" if (f.get("mimeType") or "").startswith("audio/") else "video"
    return {
        "id": f.get("id"),
        "type": "movie",
        "title": title,
        "year": year,
        "drive_id": drive_id,
        "folder_id": None,
        "shelf": None,
        "poster": None,
        "tmdb_id": None,
        "overview": None,
        "quality": naming.detect_quality(f.get("name") or ""),
        "media": media,
        "file_id": f.get("id"),
        "size": size,
        "duration_ms": dur,
        "_thumb": f.get("thumbnailLink"),
    }


def classify_playlist_drive(drive_id, drive_name, nodes, loose):
    """Classify a podcasts/videos drive: each root folder = a channel. Pure.

    `nodes` are the walked root folder nodes; each channel's entire subtree is
    flattened into one season of natural-sorted episodes. `loose` are raw Drive
    media files sitting at the drive root, each becoming a standalone movie.
    `drive_name` is unused (channel titles come from folder names) but kept for
    signature parity with the other section classifiers.
    """
    records = []
    for node in nodes:
        rec = _channel_record(node)
        if rec is not None:
            records.append(rec)
    for f in loose:
        records.append(_loose_movie(drive_id, f))
    return records
