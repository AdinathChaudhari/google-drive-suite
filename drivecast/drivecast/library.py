"""Cached media library: scan Shared Drives into movie/show records.

The library is the primary browsing surface. A scan walks each SELECTED drive's
folder tree once (quota-resilient, throttled) and produces a structured,
persisted catalogue at data/library.json, so normal browsing hits the Drive API
zero times — tiles, seasons, episodes and pre-cached posters all come off disk.

Design split:
  * Pure functions (classify_node, classify_loose, diff_library, ...) operate on
    plain dicts and are exhaustively unit-tested on synthetic data — no network.
  * The Scanner does the I/O: it walks drives via DriveAPI (which handles
    rate-limit backoff), resolves posters via TMDB, diffs against the existing
    library, prunes orphaned posters, and writes atomically.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time

from . import config
from . import naming
from . import sections
from .drive_api import FOLDER_MIME, DriveAPIError

log = logging.getLogger("drivecast.library")

LIBRARY_PATH = os.path.join(config.DATA_DIR, "library.json")
LIBRARY_VERSION = 2


def migrate_library_v1(data, drive_sections=None):
    """Upgrade a v1 library dict to v2 in place (pure; returns data).

    Stamps the fields v2 records carry so the app is fully functional before
    the first rescan: section (from the drive assignment), category (None =
    provisional until a TMDB pass), media, source_drives.

    `section_for_drive` now returns None for a drive with no live tab (e.g. a
    since-deselected drive, or one whose tab was deleted) — config migration
    always runs before this (see config.load_config/migrate_config), so by
    the time this executes, a still-selected drive already resolves to
    whatever tab it was seeded/assigned into. A None here is harmless: the
    record is simply invisible until the next scan prunes or reassigns it.
    """
    for rec in (data.get("titles") or {}).values():
        rec.setdefault("section",
                       sections.section_for_drive(drive_sections, rec.get("drive_id")))
        rec.setdefault("category", None)
        rec.setdefault("media", "video")
        rec.setdefault("source_drives",
                       [rec["drive_id"]] if rec.get("drive_id") else [])
    data["version"] = LIBRARY_VERSION
    return data

# Guess a mimeType from a filename so seeded HEAD responses are sensible.
_EXT_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".webm": "video/webm",
    ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv", ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg", ".ts": "video/mp2t", ".m2ts": "video/mp2t",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".opus": "audio/opus", ".wav": "audio/wav", ".pdf": "application/pdf",
}


def _mime_for(name):
    _, ext = os.path.splitext(name or "")
    return _EXT_MIME.get(ext.lower(), "video/mp4")


def _is_folder(f):
    return f.get("mimeType") == FOLDER_MIME


def _is_video(f):
    return (f.get("mimeType") or "").startswith("video/")


def _is_audio(f):
    mime = f.get("mimeType") or ""
    if mime.startswith("audio/"):
        return True
    # Drive reports .m4b audiobooks (and friends) as octet-stream.
    if mime == "application/octet-stream":
        return _EXT_MIME.get(os.path.splitext(f.get("name") or "")[1].lower(),
                             "").startswith("audio/")
    return False


def _is_media(f):
    """Playable media: video or audio."""
    return _is_video(f) or _is_audio(f)


def _file_dict(f, parent_id):
    """Normalise a non-media extra (pdf/image) into the node "files" shape."""
    try:
        size = int(f.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "id": f.get("id"),
        "name": f.get("name") or "",
        "mime": f.get("mimeType") or "",
        "size": size,
        "parent_id": parent_id,
        "thumb": f.get("thumbnailLink"),
    }


def _duration_ms(f):
    vmm = f.get("videoMediaMetadata") or {}
    d = vmm.get("durationMillis")
    try:
        return int(d) if d is not None else None
    except (TypeError, ValueError):
        return None


def _video_dict(f, parent_id, ancestors):
    """Normalise a raw Drive media file into the internal shape classify uses."""
    try:
        size = int(f.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "id": f.get("id"),
        "name": f.get("name") or "",
        "size": size,
        "duration_ms": _duration_ms(f),
        "parent_id": parent_id,
        "ancestors": list(ancestors),
        "thumb": f.get("thumbnailLink"),
        "media": "audio" if _is_audio(f) else "video",
    }


def _loose_show_id(drive_id, title):
    h = hashlib.sha1(("%s|%s" % (drive_id, title.lower())).encode("utf-8")).hexdigest()
    return "loose:" + h[:16]


# ------------------------------------------------------------------ classify --

def _season_of(video):
    """Determine a video's season: nearest season-named ancestor, else SxxExx, else 1."""
    for anc in reversed(video["ancestors"]):
        s = naming.season_from_folder(anc)
        if s is not None:
            return s
    ep = naming.detect_episode(video["name"])
    if ep is not None:
        return ep[0]
    return 1


def _episode_record(video, show_title, fallback_number):
    e = naming.episode_number(video["name"])
    title = naming.episode_title(video["name"])
    if not title:
        title = "Episode %d" % e if e is not None else naming.strip_ext(video["name"])
    return {
        "title": title,
        "episode": e if e is not None else fallback_number,
        "file_id": video["id"],
        "name": video["name"],
        "duration_ms": video["duration_ms"],
        "size": video["size"],
        "parent_id": video["parent_id"],
    }


def _group_episodes(videos, show_title):
    """Group videos into ascending seasons, each with episodes sorted by number."""
    buckets = {}
    for v in videos:
        buckets.setdefault(_season_of(v), []).append(v)
    seasons = []
    for s in sorted(buckets):
        vids = buckets[s]
        vids.sort(key=lambda v: (
            naming.episode_number(v["name"]) if naming.episode_number(v["name"]) is not None else 10 ** 6,
            v["name"].lower(),
        ))
        episodes = [_episode_record(v, show_title, i + 1) for i, v in enumerate(vids)]
        seasons.append({"season": s, "episodes": episodes})
    return seasons


def _all_videos(node):
    """Flatten every descendant video under a node (recursively)."""
    out = list(node["videos"])
    for sf in node["subfolders"]:
        out.extend(_all_videos(sf))
    return out


def _split_videos(node):
    """Split a node's descendant videos into (main, bonus) structurally.

    Videos under a bonus-named subfolder (Featurettes/Extras/...) come back
    separately as (folder_label, video) pairs so they become labelled extras
    pseudo-seasons instead of polluting the show's real seasons. Discard
    folders (Sample/Subs/...) are dropped outright.
    """
    main = list(node["videos"])
    bonus = []
    for sf in node["subfolders"]:
        if naming.is_discard_folder(sf["name"]):
            continue
        if naming.is_bonus_folder(sf["name"]):
            label = (sf["name"] or "").strip() or "Extras"
            bonus.extend((label, v) for v in _all_videos(sf))
        else:
            m, b = _split_videos(sf)
            main.extend(m)
            bonus.extend(b)
    return main, bonus


def _extras_label(label, season, multi):
    """Display name for an extras pseudo-season ("Featurettes · Season 2")."""
    if not multi:
        return label
    return "%s · %s" % (label, "Specials" if season == 0 else "Season %d" % season)


def _extras_seasons(bonus):
    """Build labelled pseudo-seasons from (label, video) bonus pairs.

    One entry per (label, source season) so per-season featurettes stay
    distinguishable. Entries carry "extras": True so the UI can list them in
    the season picker without counting them as real seasons.
    """
    buckets = {}
    for label, v in bonus:
        if naming.is_strict_sample(v["name"]):
            continue
        buckets.setdefault((label, _season_of(v)), []).append(v)
    label_counts = {}
    for label, _ in buckets:
        label_counts[label] = label_counts.get(label, 0) + 1
    entries = []
    for label, s in sorted(buckets, key=lambda k: (k[0].lower(), k[1])):
        vids = buckets[(label, s)]
        vids.sort(key=lambda v: (
            naming.episode_number(v["name"]) if naming.episode_number(v["name"]) is not None else 10 ** 6,
            v["name"].lower(),
        ))
        entries.append({
            "season": s,
            "name": _extras_label(label, s, label_counts[label] > 1),
            "extras": True,
            "episodes": [_episode_record(v, label, i + 1) for i, v in enumerate(vids)],
        })
    return entries


def _is_show(node):
    """True if a folder node is a TV show.

    A show has a DIRECT season-named subfolder, OR >=2 of its videos (direct, or
    in its immediate season subfolders) carry episode markers. Detection looks at
    immediate children only — deeper folders are recursed into as their own tiles.
    """
    if any(naming.season_from_folder(sf["name"]) is not None for sf in node["subfolders"]):
        return True
    count = sum(1 for v in node["videos"] if naming.episode_number(v["name"]) is not None)
    for sf in node["subfolders"]:
        if naming.season_from_folder(sf["name"]) is not None:
            count += sum(1 for v in sf["videos"] if naming.episode_number(v["name"]) is not None)
    return count >= 2


def _show_record(node):
    """Build one show record from a node's descendant videos, or None if empty.

    Bonus subfolders (Featurettes/Extras/...) become labelled extras
    pseudo-seasons appended after the real seasons, so a show's featurettes
    live inside the show instead of leaking into season 1.
    """
    main, bonus = _split_videos(node)
    videos = [v for v in main if not naming.is_sample(v["name"])]
    extras = _extras_seasons(bonus)
    if not videos and not extras:
        return None
    title, year = naming.clean_title(node["name"])
    # Best quality across all episodes so the tile advertises the best available.
    quality = naming.best_quality(v["name"] for v in videos) or naming.detect_quality(node["name"])
    thumb = next((v.get("thumb") for v in videos if v.get("thumb")), None)
    if thumb is None:
        thumb = next((v.get("thumb") for _, v in bonus if v.get("thumb")), None)
    return {
        "id": node["id"],
        "type": "show",
        "title": title,
        "year": year,
        "drive_id": node["drive_id"],
        "folder_id": node["id"],
        "poster": None,
        "tmdb_id": None,
        "overview": None,
        "quality": quality,
        # Raw folder name kept transiently so the season-grouping pass can detect
        # "<Show> Season N" / bare "Season N" siblings. Stripped before persisting.
        "_folder_name": node["name"],
        # First available Drive thumbnail; poster fallback when TMDB has none.
        "_thumb": thumb,
        "seasons": _group_episodes(videos, title) + extras,
    }


def _movie_record(node, video, from_folder):
    """One movie record for a single video; title from the folder or the file."""
    title, year = naming.clean_title(node["name"] if from_folder else video["name"])
    # Quality from the video filename; fall back to the folder name.
    quality = naming.detect_quality(video["name"]) or naming.detect_quality(node["name"])
    return {
        "id": video["id"],
        "type": "movie",
        "title": title,
        "year": year,
        "drive_id": node["drive_id"],
        "folder_id": node["id"],
        "poster": None,
        "tmdb_id": None,
        "overview": None,
        "quality": quality,
        "file_id": video["id"],
        "size": video["size"],
        "duration_ms": video["duration_ms"],
        # Transient keys kept so group_seasons can still merge a stray movie folder
        # that turns out to be a bare/prefixed season; stripped before persisting.
        "_folder_name": node["name"],
        "_video_name": video["name"],
        "_thumb": video.get("thumb"),
    }


def _movie_extras(node):
    """Labelled extras pseudo-season entries from a movie node's bonus
    subfolders (Featurettes/Extras/Trailers/...), or [] when there are none.

    Reuses the exact machinery the show path uses (_extras_seasons); discard
    folders (Sample/Subs) contribute nothing.
    """
    pairs = []
    for sf in node["subfolders"]:
        if naming.is_bonus_folder(sf["name"]):
            label = (sf["name"] or "").strip() or "Extras"
            pairs.extend((label, v) for v in _all_videos(sf))
    return _extras_seasons(pairs)


def _expand_movies(node):
    """Expand a non-show node into movie records, recursing into subfolders.

    * leaf movie folder (videos, no non-extras subfolders): 1 video -> one movie
      named from the FOLDER; multiple videos -> one movie per video (file-named).
    * container (has non-extras subfolders): each stray direct video becomes a
      file-named movie, plus the recursion into each non-extras subfolder.
    Bonus subfolders (Featurettes/Extras/...) become an ``extras`` list on the
    movie record(s) instead of being discarded: attached to the single film for
    a per-movie folder, or fanned onto every sibling film for a collection
    folder with a shared bonus folder (e.g. a trilogy's shared Featurettes).
    Discard subfolders (Sample/Subs) are still dropped entirely.
    """
    direct = [v for v in node["videos"] if not naming.is_sample(v["name"])]
    subs = [sf for sf in node["subfolders"] if not naming.is_extras_folder(sf["name"])]
    extras = _movie_extras(node)

    if not subs:
        if not direct:
            return []
        if len(direct) == 1:
            rec = _movie_record(node, direct[0], from_folder=True)
            if extras:
                rec["extras"] = extras
            return [rec]
        recs = [_movie_record(node, v, from_folder=False) for v in direct]
        for rec in recs:
            if extras:
                rec["extras"] = copy.deepcopy(extras)
        return recs

    records = [_movie_record(node, v, from_folder=False) for v in direct]
    for sf in subs:
        records.extend(classify_node(sf))
    # A collection folder with a SHARED bonus folder beside the film subfolders:
    # fan the extras onto every film the recursion produced (appended, so a
    # film's own Featurettes folder is preserved). If the recursion yielded a
    # single show instead, the shared extras fold into it as pseudo-seasons.
    if extras:
        movies = [r for r in records if r.get("type") == "movie"]
        shows = [r for r in records if r.get("type") == "show"]
        if movies:
            for r in movies:
                r["extras"] = list(r.get("extras") or []) + copy.deepcopy(extras)
        elif len(shows) == 1:
            shows[0]["seasons"] = (list(shows[0].get("seasons") or [])
                                   + copy.deepcopy(extras))
    return records


def classify_node(node):
    """Classify one folder node into a list of movie/show records (recursive).

    node = {
      "id", "name", "drive_id",
      "videos":     [ _video_dict(...) ],  # videos DIRECTLY in this folder
      "subfolders": [ child node, ... ],   # nested child nodes (same shape)
    }

    A show yields a single show record; anything else expands into one or more
    movie tiles, recursing through collection/container folders so each film is
    its own tile.
    """
    if _is_show(node):
        rec = _show_record(node)
        return [rec] if rec else []
    return _expand_movies(node)


def classify_loose(drive_id, loose_files):
    """Classify loose video files at a drive root into shows / standalone movies.

    Files with SxxExx/NxNN markers group into shows keyed by parsed show title;
    everything else is a standalone movie.
    """
    videos = [f for f in loose_files
              if _is_video(f) and not naming.is_sample(f.get("name") or "")
              and not naming.is_junk(f.get("name") or "")]
    shows = {}
    movies = []
    for f in videos:
        if naming.detect_episode(f.get("name") or "") is not None:
            st = naming.parse(f["name"])["title"]
            shows.setdefault(st, []).append(f)
        else:
            movies.append(f)

    records = []
    for show_title, files in shows.items():
        vids = [_video_dict(f, drive_id, []) for f in files]
        year = None
        for f in files:
            year = naming.parse(f["name"])["year"]
            if year:
                break
        records.append({
            "id": _loose_show_id(drive_id, show_title),
            "type": "show",
            "title": show_title,
            "year": year,
            "drive_id": drive_id,
            "folder_id": drive_id,
            "poster": None,
            "tmdb_id": None,
            "overview": None,
            "quality": naming.best_quality(f.get("name") or "" for f in files),
            "_thumb": next((v.get("thumb") for v in vids if v.get("thumb")), None),
            "seasons": _group_episodes(vids, show_title),
        })
    for f in movies:
        title, year = naming.clean_title(f["name"])
        v = _video_dict(f, drive_id, [])
        records.append({
            "id": f.get("id"),
            "type": "movie",
            "title": title,
            "year": year,
            "drive_id": drive_id,
            "folder_id": None,
            "poster": None,
            "tmdb_id": None,
            "overview": None,
            "quality": naming.detect_quality(f.get("name") or ""),
            "file_id": v["id"],
            "size": v["size"],
            "duration_ms": v["duration_ms"],
            "_thumb": v.get("thumb"),
        })
    return records


# ------------------------------------------------------------- season grouping --

# Drive-name noise to strip when a whole drive is one show (e.g. "TV | Grimwold",
# "Movie // Saga", "TV Show // Grimwold", "Milo in the Muddle (Part 1)").
# The optional trailing "show(s)" handles the two-word "TV Show //" / "Movie Show //"
# convention. A separator MUST follow, so a plain drive literally named "TV Shows"
# (no separator) is left intact rather than stripped to "".
_DRIVE_PREFIX_RE = re.compile(
    r"^\s*(?:(?:tv|movies?)(?:\s+shows?)?|shows?)\s*[|/:\-]+\s*", re.IGNORECASE)
_PART_SUFFIX_RE = re.compile(r"\s*[\(\[]?\s*part\s*\d+\s*[\)\]]?\s*$", re.IGNORECASE)


def _clean_show_name(name):
    """Human display name for a show derived from a drive/prefix name."""
    n = _DRIVE_PREFIX_RE.sub("", name or "")
    n = _PART_SUFFIX_RE.sub("", n)
    return n.strip(" -_|/").strip() or (name or "").strip()


def _norm_key(name):
    """Normalised grouping key: lowercased, de-noised, alphanumerics only."""
    n = _clean_show_name(name).lower()
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _episodes_of(rec, fallback_season):
    """Flatten a member record's videos into episode dicts (movie -> 1 episode).

    Extras pseudo-seasons are skipped: group_seasons carries them through
    separately so a member's featurettes never mix into the merged seasons.
    """
    if rec.get("type") == "show":
        eps = []
        for s in rec.get("seasons", []):
            if s.get("extras"):
                continue
            eps.extend(s.get("episodes", []))
        return eps
    return [{
        "title": rec.get("title"),
        "episode": 1,
        "file_id": rec.get("file_id"),
        "name": rec.get("_video_name") or rec.get("title"),
        "duration_ms": rec.get("duration_ms"),
        "size": rec.get("size"),
        "parent_id": rec.get("folder_id"),
    }]


def group_seasons(records, drive_names):
    """Merge sibling season-folders (and single-show drives) into one show each.

    Handles the two layouts that otherwise show one tile per season:
      * bare "Season N" top-level folders  -> the DRIVE is the show; keyed by
        the immutable drive id, so renaming the drive keeps the show's record
        id stable (only its display title follows the new name). Two same-named
        such drives therefore stay separate shows rather than merging.
      * "<Show> Season N" sibling folders  -> grouped by the shared show prefix
    Nested "Show/Season N" folders already classify correctly and pass through.
    Prefixed-folder shows sharing a normalised key merge across drives too.
    """
    groups = {}          # key -> {"display","drive_id","year","members":[(season,rec)]}
    order = []           # preserve first-seen ordering for stable output
    passthrough = []

    for rec in records:
        fname = rec.get("_folder_name")
        drive_id = rec.get("drive_id")
        drive_name = drive_names.get(drive_id, "") if drive_id else ""

        key = display = None
        season_num = None
        if fname is not None:
            ps = naming.pure_season(fname)
            if ps is not None:
                # The drive IS the show: key on the immutable drive id so a
                # drive rename can't change the persisted record id (which would
                # drop all carried metadata). The display title still follows
                # the current drive name so renames stay visible on rescan.
                key = "drive:" + str(drive_id)
                display = _clean_show_name(drive_name) or "Unknown"
                season_num = ps
            else:
                prefix, snum = naming.split_season_suffix(fname)
                if prefix:
                    key = _norm_key(prefix)
                    display = _clean_show_name(prefix)
                    season_num = snum

        if key is None:
            passthrough.append(rec)
            continue

        g = groups.get(key)
        if g is None:
            g = {"display": display, "drive_id": drive_id, "year": rec.get("year"),
                 "members": []}
            groups[key] = g
            order.append(key)
        if not g["year"] and rec.get("year"):
            g["year"] = rec.get("year")
        g["members"].append((season_num, rec))

    merged = []
    for key in order:
        g = groups[key]
        buckets = {}
        extras_entries = []
        best_q, best_q_rank = None, 0
        thumb = None
        member_drives = []
        for season_num, rec in g["members"]:
            buckets.setdefault(season_num, []).extend(_episodes_of(rec, season_num))
            # A member season-folder's own extras (e.g. "Show Season 2/
            # Featurettes") survive the merge as labelled pseudo-seasons.
            for s in (rec.get("seasons") or []) if rec.get("type") == "show" else []:
                if not s.get("extras"):
                    continue
                entry = dict(s)
                if season_num is not None and "·" not in (entry.get("name") or ""):
                    entry["name"] = _extras_label(entry.get("name") or "Extras",
                                                  season_num, True)
                    entry["season"] = season_num
                extras_entries.append(entry)
            r = naming.quality_rank(rec.get("quality"))
            if r > best_q_rank:
                best_q, best_q_rank = rec.get("quality"), r
            if thumb is None:
                thumb = rec.get("_thumb")
            for d in rec.get("source_drives") or ([rec["drive_id"]] if rec.get("drive_id") else []):
                if d not in member_drives:
                    member_drives.append(d)
        seasons = []
        for s in sorted(buckets):
            eps = buckets[s]
            eps.sort(key=lambda e: (e.get("episode") if e.get("episode") is not None else 10 ** 6,
                                    (e.get("name") or "").lower()))
            seasons.append({"season": s, "episodes": eps})
        extras_entries.sort(key=lambda e: ((e.get("name") or "").lower(),
                                           e.get("season") or 0))
        seasons.extend(extras_entries)
        merged.append({
            "id": "grp:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16],
            "type": "show",
            "title": g["display"],
            "year": g["year"],
            "drive_id": g["drive_id"],
            "folder_id": None,
            "poster": None,
            "tmdb_id": None,
            "overview": None,
            "quality": best_q,
            "source_drives": member_drives,
            "_thumb": thumb,
            "seasons": seasons,
        })

    return _strip_transient(passthrough) + merged


def _strip_transient(records):
    """Remove the transient _folder_name/_video_name keys before persisting."""
    for rec in records:
        rec.pop("_folder_name", None)
        rec.pop("_video_name", None)
    return records


# ------------------------------------------------------------ extras folding --

def split_extras_records(records):
    """Partition classified records into (main, extras) lists.

    A record whose source folder is bonus-named — a top-level "Featurettes"/
    "Extras" folder that classified as its own show or movie(s) — is an extras
    record: attach_extras() folds it into the show it sits beside instead of
    letting it surface as a library tile.
    """
    main, extras = [], []
    for rec in records:
        if naming.is_bonus_folder(rec.get("_folder_name") or ""):
            extras.append(rec)
        else:
            main.append(rec)
    return main, extras


# A root-level extras folder on a drive with several shows attaches to the
# drive's headline show only when it has at least this many times the
# runner-up's episodes ("the drive is basically this show"); below it, and with
# no season-overlap signal, the folder stays its own tile.
_EXTRAS_DOMINANCE_RATIO = 2


def _pick_extras_owner(shows, members):
    """Which of several same-drive shows owns a root extras folder, or None.

    Tier 1 (season overlap): the extras folder's internal season numbers vs
    each show's present seasons — a strictly-best, non-zero overlap wins (e.g.
    a "Featurettes/Season 5" beside an S1-7 show and an S1-3 show is decisive).
    Tier 2 (episode dominance): on a tie or no overlap, the show with at least
    _EXTRAS_DOMINANCE_RATIO x the runner-up's episodes owns the drive's
    identity. None = genuinely ambiguous; the caller keeps the tile.

    Only non-``extras`` seasons count on both sides: a grouped show's own
    member-folder extras (group_seasons) must not skew the season set or counts.
    """
    def real(rec):
        return [s for s in rec.get("seasons") or [] if not s.get("extras")]

    ex_seasons = {s.get("season") for m in members if m.get("type") == "show"
                  for s in (m.get("seasons") or [])
                  if s.get("episodes") and s.get("season") is not None
                  and not s.get("extras")}
    if ex_seasons:
        scores = [len(ex_seasons & {s.get("season") for s in real(sh)})
                  for sh in shows]
        ranked = sorted(range(len(shows)), key=scores.__getitem__, reverse=True)
        if scores[ranked[0]] > 0 and scores[ranked[0]] > scores[ranked[1]]:
            return shows[ranked[0]]

    counts = [sum(len(s.get("episodes") or []) for s in real(sh)) for sh in shows]
    ranked = sorted(range(len(shows)), key=counts.__getitem__, reverse=True)
    if (counts[ranked[0]] > 0
            and counts[ranked[0]] >= _EXTRAS_DOMINANCE_RATIO * counts[ranked[1]]):
        return shows[ranked[0]]
    return None


def attach_extras(records, extras_records):
    """Fold standalone extras records into the show they sit beside.

    Runs AFTER group_seasons, so a drive laid out as bare "Season N" folders
    plus a sibling "Featurettes" folder attaches to the merged drive-show.
    A single show on the drive owns the extras outright; with several shows,
    _pick_extras_owner disambiguates (season overlap, then episode dominance)
    and only a genuine tie leaves the extras record as its own tile.
    """
    if not extras_records:
        return records
    shows_by_drive = {}
    movies_by_drive = {}
    for rec in records:
        drives = rec.get("source_drives") or (
            [rec["drive_id"]] if rec.get("drive_id") else [])
        if rec.get("type") == "show":
            for d in drives:
                shows_by_drive.setdefault(d, []).append(rec)
        elif rec.get("type") == "movie":
            for d in drives:
                movies_by_drive.setdefault(d, []).append(rec)

    # One extras folder can classify as several records (a leaf folder with
    # loose clips expands one movie per file) — regroup by (drive, folder).
    groups = {}
    order = []
    for ex in extras_records:
        key = (ex.get("drive_id"), (ex.get("_folder_name") or "Extras").strip())
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ex)

    leftovers = []
    for drive_id, label in order:
        members = groups[(drive_id, label)]
        shows = shows_by_drive.get(drive_id) or []
        movies = movies_by_drive.get(drive_id) or []
        # A single show owns the extras; several shows -> disambiguate (or
        # None); else a lone loose movie gets them on its `extras` field.
        # Genuinely ambiguous folders stay a tile.
        if len(shows) == 1:
            target = shows[0]
        elif len(shows) > 1:
            target = _pick_extras_owner(shows, members)
        else:
            target = None
        if target is not None:
            target["seasons"] = (list(target.get("seasons") or [])
                                 + _extras_entries(members, label))
        elif not shows and len(movies) == 1:
            target = movies[0]
            target["extras"] = (list(target.get("extras") or [])
                                + _extras_entries(members, label))
        else:
            leftovers.extend(members)
    return records + _strip_transient(leftovers)


def _extras_entries(members, label):
    """Convert an extras folder's records into labelled pseudo-seasons."""
    show_entries = []
    loose_eps = []
    for rec in members:
        if rec.get("type") == "show":
            show_entries.extend(s for s in rec.get("seasons") or []
                                if s.get("episodes"))
        else:
            loose_eps.extend(_episodes_of(rec, 1))
    multi = (len(show_entries) + (1 if loose_eps else 0)) > 1
    entries = []
    for s in show_entries:
        entries.append({
            "season": s["season"],
            "name": _extras_label(label, s["season"], multi),
            "extras": True,
            "episodes": s["episodes"],
        })
    if loose_eps:
        loose_eps.sort(key=lambda e: (e.get("name") or "").lower())
        for i, e in enumerate(loose_eps):
            e["episode"] = i + 1
        entries.append({"season": 1, "name": label, "extras": True,
                        "episodes": loose_eps})
    return entries


# ----------------------------------------------------------------------- diff --

def diff_library(old_titles, new_titles):
    """Return (added_ids, removed_ids) comparing title id sets."""
    old_ids = set(old_titles)
    new_ids = set(new_titles)
    added = [tid for tid in new_titles if tid not in old_ids]
    removed = [tid for tid in old_titles if tid not in new_ids]
    return added, removed


def merge_existing_metadata(old_titles, new_titles):
    """Carry poster/tmdb/overview from prior records into freshly-scanned ones.

    Avoids re-hitting TMDB for titles we already resolved; episode lists in the
    new record are kept as-scanned (that's the point of a refresh).
    """
    for tid, rec in new_titles.items():
        old = old_titles.get(tid)
        if not old:
            continue
        # A record reclassified into a different TAB keeps NOTHING: its old
        # TMDB poster/overview (matched as a film) would be wrong for a
        # course/series, and a stale poster would block the cover-image
        # fallback forever. Compare the raw tab keys directly — there is no
        # more "entertainment" fallback to paper over a missing/None section.
        if old.get("section") != rec.get("section"):
            continue
        # Same id, different title = a rename (drive-is-the-show groups keep
        # their id across a drive rename). The old poster/tmdb/overview/category
        # described the OLD name; drop them so they recompute against the new
        # title instead of carrying stale enrichment forever.
        if (old.get("title") or "") != (rec.get("title") or ""):
            continue
        for k in ("poster", "tmdb_id", "overview", "category"):
            if old.get(k) and not rec.get(k):
                rec[k] = old[k]


def assign_added_at(old_titles, new_titles, now=None):
    """Stamp each new record with an ``added_at`` epoch-seconds timestamp.

    Existing titles keep the timestamp they were first seen (carried over like
    poster metadata); freshly-added titles get ``now``. Powers the "Recently
    added" sort in the UI.
    """
    if now is None:
        now = time.time()
    for tid, rec in new_titles.items():
        old = old_titles.get(tid)
        if old and old.get("added_at"):
            rec["added_at"] = old["added_at"]
        elif not rec.get("added_at"):
            rec["added_at"] = now


def prune_removed_posters(old_titles, new_titles, removed_ids):
    """Delete poster files belonging to removed titles that nothing else uses."""
    keep = {t.get("poster") for t in new_titles.values() if t.get("poster")}
    for tid in removed_ids:
        pk = old_titles.get(tid, {}).get("poster")
        if pk and pk not in keep:
            try:
                os.remove(os.path.join(config.POSTERS_DIR, pk))
            except OSError:
                pass


# -------------------------------------------------------------------- Library --

class Library:
    """In-memory catalogue backed by data/library.json (atomic writes)."""

    def __init__(self, path=LIBRARY_PATH, drive_sections=None):
        self.path = path
        self._lock = threading.Lock()
        self.data = self._load(drive_sections)
        self._file_index = {}
        self._file_title = {}
        self._rebuild_index()

    def _load(self, drive_sections=None):
        try:
            with open(self.path) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("titles"), dict):
                if (data.get("version") or 1) < LIBRARY_VERSION:
                    data = migrate_library_v1(data, drive_sections)
                return data
        except (OSError, ValueError):
            pass
        return {"version": LIBRARY_VERSION, "generated_at": 0, "titles": {}}

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".library-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _rebuild_index(self):
        """Map file_id -> {name,size,duration_ms} for fast play/HEAD lookups,
        plus file_id -> title id so history items can find their title record."""
        idx = {}
        owners = {}
        for rec in self.data["titles"].values():
            if rec.get("type") == "movie" and rec.get("file_id"):
                idx[rec["file_id"]] = {
                    "name": rec.get("title"),
                    "size": rec.get("size"),
                    "duration_ms": rec.get("duration_ms"),
                    "media": rec.get("media") or "video",
                    "drive_id": rec.get("drive_id"),
                    "parent_id": rec.get("folder_id"),  # the movie's folder
                }
                owners[rec["file_id"]] = rec.get("id")
                # Featurettes/extras clips are playable too: index their files
                # so play/HEAD lookups and history resolve to the movie.
                for grp in rec.get("extras") or []:
                    for ep in grp.get("episodes", []):
                        if ep.get("file_id"):
                            idx[ep["file_id"]] = {
                                "name": ep.get("name"),
                                "size": ep.get("size"),
                                "duration_ms": ep.get("duration_ms"),
                                "media": ep.get("media") or "video",
                                "drive_id": rec.get("drive_id"),
                                "parent_id": ep.get("parent_id"),
                            }
                            owners[ep["file_id"]] = rec.get("id")
            elif rec.get("type") == "show":
                for season in rec.get("seasons", []):
                    for ep in season.get("episodes", []):
                        if ep.get("file_id"):
                            idx[ep["file_id"]] = {
                                "name": ep.get("name"),
                                "size": ep.get("size"),
                                "duration_ms": ep.get("duration_ms"),
                                "media": ep.get("media") or "video",
                                "drive_id": rec.get("drive_id"),
                                "parent_id": ep.get("parent_id"),
                            }
                            owners[ep["file_id"]] = rec.get("id")
                    ab = season.get("audiobook")
                    if ab and ab.get("file_id"):
                        idx[ab["file_id"]] = {
                            "name": ab.get("name"),
                            "size": ab.get("size"),
                            "duration_ms": None,
                            "media": "audio",
                            "drive_id": rec.get("drive_id"),
                            "parent_id": ab.get("parent_id"),
                        }
                        owners[ab["file_id"]] = rec.get("id")
        self._file_index = idx
        self._file_title = owners

    # ---- reads ----

    def snapshot_titles(self):
        with self._lock:
            return json.loads(json.dumps(self.data["titles"]))

    def titles_list(self):
        with self._lock:
            return list(self.data["titles"].values())

    def get(self, title_id):
        with self._lock:
            return self.data["titles"].get(title_id)

    def generated_at(self):
        return self.data.get("generated_at", 0)

    def is_empty(self):
        return not self.data["titles"]

    def file_info(self, file_id):
        return self._file_index.get(file_id)

    def title_for_file(self, file_id):
        """Return the title record that owns a file_id (movie or episode)."""
        tid = self._file_title.get(file_id)
        if tid is None:
            return None
        with self._lock:
            return self.data["titles"].get(tid)

    # ---- writes ----

    def replace(self, titles):
        with self._lock:
            self.data = {
                "version": LIBRARY_VERSION,
                "generated_at": time.time(),
                "titles": titles,
            }
            self._rebuild_index()
            self._save()

    def apply_poster_override(self, title, media_type, meta):
        """Apply a hand-picked TMDB match (from the "Fix poster" picker) to every
        in-memory record whose normalized title matches, so the corrected poster
        shows without a full rescan. Persists atomically. Returns how many
        records were updated (0 if the title isn't in the current library).
        """
        from . import tmdb
        want = tmdb._norm_title(title)
        want_type = "show" if media_type == "tv" else "movie"
        n = 0
        with self._lock:
            for rec in self.data["titles"].values():
                if rec.get("type") != want_type:
                    continue
                if tmdb._norm_title(rec.get("title")) != want:
                    continue
                # Only overwrite the image when the pick actually has artwork, so
                # a match without a poster doesn't wipe an existing one.
                if meta.get("poster_key"):
                    rec["poster"] = meta["poster_key"]
                rec["tmdb_id"] = meta.get("tmdb_id")
                rec["overview"] = meta.get("overview")
                n += 1
            if n:
                self._save()
        return n

    def seed_api_cache(self, api):
        """Prime the DriveAPI metadata cache so playback needs no Drive call."""
        with self._lock:
            titles = list(self.data["titles"].values())
        for rec in titles:
            if rec.get("type") == "movie" and rec.get("file_id"):
                api.seed_meta(self._meta(rec["file_id"], rec.get("title"),
                                         rec.get("size"), rec.get("duration_ms")))
                for grp in rec.get("extras") or []:
                    for ep in grp.get("episodes", []):
                        if ep.get("file_id"):
                            api.seed_meta(self._meta(
                                ep["file_id"], ep.get("name"),
                                ep.get("size"), ep.get("duration_ms")))
            elif rec.get("type") == "show":
                for season in rec.get("seasons", []):
                    for ep in season.get("episodes", []):
                        if ep.get("file_id"):
                            api.seed_meta(self._meta(ep["file_id"], ep.get("name"),
                                                     ep.get("size"), ep.get("duration_ms")))
                    ab = season.get("audiobook")
                    if ab and ab.get("file_id"):
                        api.seed_meta(self._meta(ab["file_id"], ab.get("name"),
                                                 ab.get("size"), None))

    @staticmethod
    def _meta(file_id, name, size, duration_ms):
        meta = {
            "id": file_id,
            "name": name,
            "mimeType": _mime_for(name),
        }
        if size:
            meta["size"] = str(size)
        if duration_ms:
            meta["videoMediaMetadata"] = {"durationMillis": str(duration_ms)}
        return meta


# -------------------------------------------------------------------- Scanner --

def _default_cache():
    from .scan_cache import ScanCache
    return ScanCache()


class Scanner:
    """Walks selected drives and (re)builds the library, quota-resiliently.

    Every scan writes raw per-drive records into a ScanCache, then rebuilds
    the whole library from the cache — so a scoped (per-drive) refresh only
    re-walks that drive on the Drive API while cross-drive grouping stays
    correct.
    """

    def __init__(self, api, tmdb, library, throttle=0.15, cache=None):
        self.api = api
        self.tmdb = tmdb
        self.library = library
        self.throttle = throttle
        self.cache = cache if cache is not None else _default_cache()
        self.status = {
            "running": False, "scanned": 0, "total": 0,
            "added": 0, "removed": 0, "error": None, "scope": [],
            # Non-fatal: drives that were scanned-over but skipped because
            # they aren't assigned to any live tab (see scan()'s loop).
            "warning": None,
        }

    async def _throttle(self):
        if self.throttle:
            await asyncio.sleep(self.throttle)

    async def _list_folder(self, drive_id, folder_id, kinds=("video",)):
        """Return all raw children of a folder, following pagination + throttle."""
        out = []
        page_token = None
        while True:
            res = await self.api.browse(drive_id, folder_id, page_token, kinds=kinds)
            out.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            await self._throttle()
            if not page_token:
                break
        return out

    async def _walk_title(self, drive_id, folder, ancestors=(), kinds=("video",)):
        """Recursively build a nested tree node preserving folder hierarchy.

        node = {id, name, drive_id, videos:[direct media], files:[direct
        pdf/image extras], subfolders:[child nodes]}. "videos" holds ALL
        playable media (audio too, when the section's kinds include it — each
        entry carries a "media" key). Each media file carries its ancestor
        folder-name chain so season detection and episode grouping still work
        when a show is nested inside a container.
        """
        name = folder.get("name") or ""
        node = {
            "id": folder.get("id"),
            "name": name,
            "drive_id": drive_id,
            "videos": [],
            "files": [],
            "subfolders": [],
        }
        child_anc = tuple(ancestors) + (name,)
        children = await self._list_folder(drive_id, node["id"], kinds)
        for c in children:
            if naming.is_junk(c.get("name") or ""):
                continue  # AppleDouble twins / .DS_Store / torrent-ad spam
            if _is_folder(c):
                node["subfolders"].append(
                    await self._walk_title(drive_id, c, child_anc, kinds))
            elif _is_media(c):
                node["videos"].append(_video_dict(c, node["id"], child_anc))
            else:
                node["files"].append(_file_dict(c, node["id"]))
        return node

    async def _scan_drive(self, drive_id, section, hints=None, drive_name=""):
        """Return the list of title records for one drive, or None if the
        drive's root couldn't be listed (caller keeps the previous cache
        entry so a flaky drive doesn't empty its titles). Never raises.

        `section` is a TAB key and must never be None here — scan() already
        filtered out drives with no live tab assignment before calling in."""
        kinds = sections.mimes_for(section)
        try:
            root = await self._list_folder(drive_id, drive_id, kinds)
        except DriveAPIError as e:
            log.warning("Skipping drive %s: %s", drive_id, e.message)
            self.status["error"] = "Drive %s: %s" % (drive_id, e.message)
            return None

        nodes = []
        loose = []
        for entry in root:
            if naming.is_junk(entry.get("name") or ""):
                continue
            if _is_folder(entry):
                try:
                    nodes.append(await self._walk_title(drive_id, entry, (), kinds))
                except DriveAPIError as e:
                    # A partial walk must NOT become the drive's cached truth —
                    # the failed folder's titles would vanish (and their
                    # posters be pruned) until this drive is rescanned.
                    # Invalidate the whole drive; the old cache entry stays.
                    log.warning("Folder %r failed; keeping the drive's previous "
                                "scan: %s", entry.get("name"), e.message)
                    self.status["error"] = "Folder %s: %s" % (entry.get("name"), e.message)
                    return None
            elif _is_media(entry):
                loose.append(entry)
        return self._classify(section, drive_id, drive_name, nodes, loose,
                              hints or {})

    def _classify(self, section, drive_id, drive_name, nodes, loose, hints):
        """Dispatch walked nodes + loose root files to the TAB's BEHAVIOR classifier.

        `section` is a TAB key; the actual dispatch happens on the BEHAVIOR
        that tab is built on (multiple tabs can share one behavior, e.g. two
        "courses" tabs fed by different drives — both must dispatch to the
        course classifier). Falls back to treating `section` itself as the
        behavior key if the tab can't be resolved (defensive; scan() already
        guards against a None section reaching here).
        """
        base = sections.behavior_for(section) or section
        if base == "courses":
            from .courses import classify_course_drive
            return classify_course_drive(drive_id, drive_name, nodes, loose, hints)
        if base == "podcasts":
            from .playlists import classify_playlist_drive
            return classify_playlist_drive(drive_id, drive_name, nodes, loose)
        plugin_classify = sections.classify_for(base)
        if plugin_classify is not None:
            # Custom private section (see sections.py): plugin classifiers
            # receive the same node shape as the built-ins.
            try:
                return plugin_classify(drive_id, drive_name, nodes, loose)
            except Exception:  # pragma: no cover - a plugin must not kill scans
                log.exception("Custom section %r (behavior %r) classifier failed",
                             section, base)
                return []
        # Entertainment (or an unresolvable base): the plain movie/show walk.
        records = []
        for node in nodes:
            records.extend(classify_node(node))
        records.extend(classify_loose(drive_id, loose))
        return records

    async def _resolve_poster(self, rec):
        if not self.tmdb.enabled:
            return
        media = "tv" if rec["type"] == "show" else "movie"
        try:
            meta = await self.tmdb.enrich(rec["title"], rec.get("year"), media)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("TMDB lookup failed for %r: %r", rec["title"], e)
            return
        if not meta:
            return
        # Only overwrite the poster when TMDB actually has artwork, so a match
        # without a poster image doesn't wipe an existing dthumb_ fallback.
        if meta.get("poster_key"):
            rec["poster"] = meta.get("poster_key")
        rec["tmdb_id"] = meta.get("tmdb_id")
        rec["overview"] = meta.get("overview")
        # Adopt TMDB's canonical title for display. _lookup only returns a match
        # that cleared the similarity threshold, so this is the authoritative
        # name — and it sheds any distributor/release junk the parser left on
        # ("Golden Hour Criterion" -> "Golden Hour") without a hardcoded list.
        if meta.get("title"):
            rec["title"] = meta["title"]
        if meta.get("year") and not rec.get("year"):
            try:
                rec["year"] = int(str(meta["year"])[:4])
            except (TypeError, ValueError):
                pass

    async def _resolve_drive_thumb(self, rec):
        """Fallback poster: cache the title's own Drive thumbnail locally.

        Used when TMDB is disabled or found no match. thumbnailLink URLs are
        short-lived, so the image is downloaded at scan time into POSTERS_DIR
        under a stable key derived from the title id.
        """
        url = rec.get("_thumb")
        if not url:
            return
        poster = rec.get("poster")
        # Keep a real (TMDB) poster, or a fallback whose cached file still
        # exists; a dthumb_ key whose file went missing gets re-downloaded.
        if poster and (not poster.startswith("dthumb_")
                       or os.path.exists(os.path.join(config.POSTERS_DIR, poster))):
            return
        key = "dthumb_%s.jpg" % hashlib.sha1(rec["id"].encode("utf-8")).hexdigest()[:16]
        dest = os.path.join(config.POSTERS_DIR, key)
        if not os.path.exists(dest):
            try:
                data = await self.api.fetch_thumbnail(url)
            except Exception as e:  # pragma: no cover - defensive
                log.debug("Thumbnail fetch failed for %r: %r", rec.get("title"), e)
                return
            if not data:
                return
            os.makedirs(config.POSTERS_DIR, exist_ok=True)
            tmp = dest + ".tmp"
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dest)
            except OSError:
                return
        rec["poster"] = key

    async def scan(self, selected_drives, scope=None, drive_hints=None,
                   drive_sections=None):
        """Scan + diff + poster refresh + persist. Never raises.

        `scope` limits which drives are re-walked on the Drive API (per-drive
        refresh); the library is ALWAYS rebuilt from the cached raw records of
        every selected drive, so cross-drive grouping stays correct. No scope
        (or a scope covering everything) is a full refresh.
        """
        selected = list(selected_drives)
        drive_sections = drive_sections or {}
        drive_hints = drive_hints or {}
        scope = [d for d in (scope or selected) if d in selected] or selected
        # First run after upgrade / cleared cache: an unscanned drive with no
        # cache entry would lose its titles in the rebuild — escalate to full.
        if any(not self.cache.has(d) for d in selected if d not in scope):
            scope = list(selected)
        self.status.update(running=True, scanned=0, total=len(scope),
                           added=0, removed=0, error=None, warning=None,
                           scope=list(scope))
        try:
            try:
                drive_names = {d["id"]: d.get("name", "") for d in await self.api.list_drives()}
            except Exception:
                drive_names = {}

            unassigned = []
            for drive_id in scope:
                section = sections.section_for_drive(drive_sections, drive_id)
                if section is None:
                    # No live tab claims this drive (never assigned, or its
                    # tab was since deleted). Don't touch the cache: the old
                    # scan-cache entry for this drive stays put so simply
                    # reassigning it to a tab later is a cheap re-walk, not a
                    # full rescan. Count it "scanned" (it WAS visited) but
                    # skip classification entirely.
                    log.warning("Drive %s has no tab assignment; skipping scan", drive_id)
                    unassigned.append(drive_id)
                    self.status["scanned"] += 1
                    continue
                records = await self._scan_drive(
                    drive_id, section, drive_hints.get(drive_id) or {},
                    drive_names.get(drive_id, ""))
                self.status["scanned"] += 1
                if records is None:
                    continue  # root listing failed: keep the old cache entry
                for rec in records:
                    rec["source_drives"] = [drive_id]
                    rec["section"] = section
                    rec.setdefault("media", "video")
                self.cache.put(drive_id, records)
            self.cache.prune(selected)
            if unassigned:
                self.status["warning"] = (
                    "%d drive(s) need a tab assignment in Settings: %s"
                    % (len(unassigned), ", ".join(sorted(unassigned))))

            # Rebuild from cached raw records of every selected drive that's
            # STILL assigned to a live tab (deep copies — group_seasons
            # mutates its input). A drive whose tab was deleted since its
            # last successful scan must not resurrect its cached records
            # under a now-dead section key.
            all_records = []
            for drive_id in selected:
                if sections.section_for_drive(drive_sections, drive_id) is None:
                    continue
                all_records.extend(self.cache.get(drive_id))

            # Season-merging is an ENTERTAINMENT-BEHAVIOR concept: a course or
            # podcast named "... Part 1" must never merge as someone's season.
            # There is no single entertainment pool anymore — tabs are data,
            # and two different entertainment-behavior tabs must never merge
            # into each other's records — so records are bucketed PER TAB and
            # grouped independently; a grp:/extras record is stamped with its
            # own bucket's tab key, never a neighboring tab's.
            ent_buckets = {}   # tab key -> records, for entertainment-behavior tabs
            rest = []
            for r in all_records:
                sec = r.get("section")
                if sections.behavior_for(sec) == "entertainment":
                    ent_buckets.setdefault(sec, []).append(r)
                else:
                    rest.append(r)

            # group_seasons() derives a "grp:" id from the show's normalised
            # NAME alone (see its docstring) — fine when there was one global
            # entertainment pool, but two DIFFERENT entertainment-behavior
            # tabs that each happen to have a "<Show> Season N" pattern for
            # the same show name would otherwise mint the IDENTICAL id and
            # silently clobber each other in new_titles below. Only
            # disambiguate when more than one entertainment-behavior tab is
            # actually in play, so the overwhelmingly common single-tab install
            # keeps its "grp:" ids byte-for-byte stable.
            #
            # NB: decide this from the CONFIGURED tab list, not from how many
            # buckets happened to produce records in THIS scan. Keying off the
            # transient ent_buckets would flip the flag the moment an unrelated
            # second entertainment tab starts/stops contributing, silently
            # rehashing the grp: ids of *unchanged* tabs — which then miss in
            # merge_existing_metadata() by id and lose their category/poster/
            # added_at. The configured count only changes when the user
            # deliberately adds/removes an entertainment tab (a one-time
            # transition), so unchanged tabs keep stable ids across scans.
            multi_ent_tabs = sum(
                1 for t in sections.tabs()
                if sections.behavior_for(t.get("key")) == "entertainment"
            ) > 1
            grouped = []
            for tab_key, bucket in ent_buckets.items():
                # Standalone extras folders (a drive-root "Featurettes" beside
                # the show's season folders) fold into their show after
                # grouping, per bucket so cross-tab folding can't happen.
                bucket, bucket_extras = split_extras_records(bucket)
                bucket_grouped = attach_extras(
                    group_seasons(bucket, drive_names), bucket_extras)
                for rec in bucket_grouped:
                    rec["section"] = tab_key
                    if multi_ent_tabs and rec.get("id", "").startswith("grp:"):
                        rec["id"] = "grp:" + hashlib.sha1(
                            ("%s|%s" % (tab_key, rec["id"])).encode("utf-8")
                        ).hexdigest()[:16]
                grouped.extend(bucket_grouped)
            grouped = grouped + _strip_transient(rest)
            new_titles = {rec["id"]: rec for rec in grouped}
            for rec in new_titles.values():
                rec.setdefault("media", "video")
                rec.setdefault("category", None)

            old_titles = self.library.snapshot_titles()
            added, removed = diff_library(old_titles, new_titles)
            merge_existing_metadata(old_titles, new_titles)
            assign_added_at(old_titles, new_titles)

            self.status["added"] = len(added)

            # Resolve posters for EVERY title still missing one when TMDB is
            # enabled. This backfills existing titles the first time a key is
            # configured; titles that already have a poster are skipped.
            if self.tmdb.enabled:
                for rec in new_titles.values():
                    # TMDB is for entertainment BEHAVIOR only: a course like
                    # "Intercourse and Communication" would happily match a
                    # film. Other tabs poster via Drive thumbnails. Gated on
                    # the tab's BEHAVIOR, not its key, so a user-renamed
                    # entertainment tab still gets posters.
                    if sections.behavior_for(rec.get("section")) != "entertainment":
                        continue
                    poster = rec.get("poster") or ""
                    # Re-resolve when there's no poster yet, an upgradeable
                    # dthumb_ fallback, OR a TMDB poster key whose cached image
                    # file has gone missing (pruned when the title was
                    # reclassified, or a download that failed after the lookup
                    # was cached). Without the last case a dangling key shows a
                    # permanent placeholder — the record has a poster name but
                    # no file, so neither this test nor the category pass below
                    # (which skips already-categorised titles) ever refetches
                    # it. _resolve_poster -> enrich() re-materialises the file.
                    tmdb_file_gone = (
                        poster and not poster.startswith("dthumb_")
                        and not os.path.exists(
                            os.path.join(config.POSTERS_DIR, poster)))
                    if not poster or poster.startswith("dthumb_") or tmdb_file_gone:
                        await self._resolve_poster(rec)
                        new_poster = rec.get("poster")
                        if (poster.startswith("dthumb_") and new_poster
                                and new_poster != poster):
                            try:
                                os.remove(os.path.join(config.POSTERS_DIR, poster))
                            except OSError:
                                pass
                # Category pass: every entertainment title still uncategorised
                # gets movie/show/documentary/other from TMDB genres — even
                # titles that already have a poster (they skip the pass above).
                # enrich() answers from its cache, so this is cheap.
                for rec in new_titles.values():
                    if sections.behavior_for(rec.get("section")) != "entertainment":
                        continue
                    if rec.get("category") is not None:
                        continue
                    hint = ((drive_hints or {}).get(rec.get("drive_id")) or {}).get("category")
                    media = "tv" if rec["type"] == "show" else "movie"
                    try:
                        meta = await self.tmdb.enrich(rec["title"], rec.get("year"), media)
                    except Exception:  # pragma: no cover - defensive
                        continue
                    rec["category"] = sections.category_for(meta, rec["type"], hint)

            # Self-heal: a structurally-detected show stuck at "other" (poisoned
            # by an earlier drive rename that changed its id and dropped it back
            # through TMDB's negative-lookup path) resets to "show", unless the
            # drive explicitly overrides its category. Runs with TMDB off too so
            # already-poisoned libraries recover; needs no TMDB call.
            for rec in new_titles.values():
                if sections.behavior_for(rec.get("section")) != "entertainment":
                    continue
                if rec.get("type") == "show" and rec.get("category") == "other":
                    hint = ((drive_hints or {}).get(rec.get("drive_id")) or {}).get("category")
                    if not hint:
                        rec["category"] = "show"

            # Titles TMDB couldn't cover fall back to the video's own Drive
            # thumbnail (also the only poster source when TMDB is disabled).
            for rec in new_titles.values():
                await self._resolve_drive_thumb(rec)
            for rec in new_titles.values():
                rec.pop("_thumb", None)

            prune_removed_posters(old_titles, new_titles, removed)
            self.status["removed"] = len(removed)

            self.library.replace(new_titles)
            self.library.seed_api_cache(self.api)
        except Exception as e:  # pragma: no cover - defensive, keep the app alive
            log.exception("Library scan failed")
            self.status["error"] = str(e)
        finally:
            self.status["running"] = False
        return self.status
