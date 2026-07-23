"""Course-drive classifier: turn a walked "courses" drive into show records.

A courses drive is a bag of online courses (DataCamp, MasterClass, Udemy rips,
...). This module folds that mess into the same ``type:"show"`` records the
library uses for TV — a course is a "show", a module is a "season", a lesson is
an "episode" — so the whole play/queue/history pipeline works unchanged.

All pure: it operates on the nested node shape ``Scanner._walk_title`` builds
(``{id,name,drive_id,videos,files,subfolders}``) plus the raw loose-file dicts
at the drive root. No I/O, no network. See SECTIONS_DESIGN.md §2.2 for the rules
(1-7) this implements.
"""
import os
import re

from . import naming

# Defensive recursion guard: real course trees are 2-4 deep; a cycle or a
# pathological upload must never spin forever (rule 2 "depth cap 10").
MAX_DEPTH = 10

# A folder whose name LEADS with a number is a numbered module (rule 2/3):
# "01) Basics", "1. Intro", "02 - Setup". Deliberately anchored so a course
# named "3 Idiots" is not mistaken for a module.
_NUMBERED_MODULE_RE = re.compile(r"^\s*\d+\s*[.)\-]")
# A root folder that looks like a module for the single-course heuristic
# (rule 6): "05) ...", "05. ...".
_ROOT_NUM_RE = re.compile(r"^\s*\d+\s*[).]")
# "Step N)" sub-modules that flatten into a module's lesson list (rule 6).
_STEP_RE = re.compile(r"\bstep\s+(\d{1,3})\b", re.IGNORECASE)

# Extras that are never a lesson or a workbook: bundle archives / shortcuts.
_DROP_EXTS = (".zip", ".rar", ".url")


# ------------------------------------------------------------- small helpers --

def _is_numbered_module(name):
    """True if a folder name leads with a module number ("01) ...", "1. ...")."""
    return bool(_NUMBERED_MODULE_RE.match(name or ""))


def _module_number(name):
    """Ordering number for a module/step folder, else None.

    Reuses ``naming.lesson_number`` (handles "05)", "1.", "3 -", ...) and also
    recognises "Step N)" sub-modules, so N. / NN) / Step N) all order alike.
    """
    n = naming.lesson_number(name)
    if n is not None:
        return n
    m = _STEP_RE.search(name or "")
    return int(m.group(1)) if m else None


def _mod_sort_key(node):
    """Sort key ordering modules: numbered ascending, unnumbered last (stable)."""
    n = _module_number(node.get("name", ""))
    return (n is None, n if n is not None else 0)


def _lesson_key(video):
    """Sort key for lessons: numbered ascending, unnumbered after, by name.

    Mirrors library's episode ordering so unpadded "EP2" < "EP10" and a stray
    "Conclusion.mp4" lands last (rule 1).
    """
    n = naming.lesson_number(video.get("name") or "")
    return (n if n is not None else 10 ** 6, (video.get("name") or "").lower())


def _is_image(f):
    return (f.get("mime") or "").startswith("image/")


def _has_media(node):
    """True if a node has any video anywhere beneath it."""
    if node.get("videos"):
        return True
    return any(_has_media(sf) for sf in node.get("subfolders", []))


def _all_files(node):
    """Every non-media extra (pdf/image) under a node, recursively."""
    out = list(node.get("files", []))
    for sf in node.get("subfolders", []):
        out.extend(_all_files(sf))
    return out


def _collect_lessons(node):
    """Flatten a module's lessons: its direct videos (lesson-sorted), then each
    sub-module's lessons — numbered/Step subfolders in number order, unnumbered
    last in drive order (rule 6's "Step N) flattening", used for every module)."""
    lessons = sorted(node.get("videos", []), key=_lesson_key)
    subs = node.get("subfolders", [])
    numbered = [sf for sf in subs if _module_number(sf.get("name", "")) is not None]
    unnumbered = [sf for sf in subs if _module_number(sf.get("name", "")) is None]
    numbered.sort(key=_mod_sort_key)
    for sf in numbered + unnumbered:
        lessons.extend(_collect_lessons(sf))
    return lessons


def _episode_title(name):
    """Human lesson title: drop the extension and the leading lesson number."""
    base = naming.strip_ext(name or "")
    base = naming.strip_enum_prefix(base).strip(" -_.")
    return base or naming.strip_ext(name or "") or (name or "")


def _course_episode(video, position):
    """One lesson record, shaped exactly like a library episode."""
    return {
        "title": _episode_title(video.get("name") or ""),
        "episode": position,
        "file_id": video.get("id"),
        "name": video.get("name"),
        "duration_ms": video.get("duration_ms"),
        "size": video.get("size"),
        "parent_id": video.get("parent_id"),
    }


def _cover_thumb(images):
    """Poster source (rule 4): a "Cover.*" image, else the only image, else None."""
    if not images:
        return None
    for f in images:
        if os.path.splitext(f.get("name") or "")[0].strip().lower() == "cover":
            return f.get("thumb")
    if len(images) == 1:
        return images[0].get("thumb")
    return None


# -------------------------------------------------------------- record build --

def _assemble(rec_id, folder_id, title, shelf, seasons_src, all_files, drive_id):
    """Build one course record from ordered (module-name, videos) groups.

    Each group's videos must arrive already in display order (callers sort
    flat seasons; ``_collect_lessons`` orders module/Step lessons). Returns None
    when the course has zero playable media (rule 7). Non-media files are sorted
    into per-lesson ``resources`` (same folder + lesson number, rule 4), course
    ``materials`` (unnumbered / number-0 pdfs), and a cover image (``_thumb``).
    Archives / shortcuts are dropped.
    """
    seasons = []
    ep_by_key = {}          # (parent_id, lesson_number) -> episode dict
    total = 0
    for i, (name, videos) in enumerate(seasons_src):
        episodes = []
        for pos, v in enumerate(videos, start=1):
            ep = _course_episode(v, pos)
            episodes.append(ep)
            ln = naming.lesson_number(v.get("name") or "")
            if ln is not None:
                ep_by_key[(v.get("parent_id"), ln)] = ep
            total += 1
        seasons.append({"season": i + 1, "name": name, "episodes": episodes})

    if total == 0:
        return None

    materials = []
    images = []
    for f in all_files:
        if _is_image(f):
            images.append(f)
            continue
        if (f.get("name") or "").lower().endswith(_DROP_EXTS):
            continue  # bundle archive / shortcut: not content
        ln = naming.lesson_number(f.get("name") or "")
        key = (f.get("parent_id"), ln)
        if ln is not None and ln > 0 and key in ep_by_key:
            ep_by_key[key].setdefault("resources", []).append(
                {"file_id": f.get("id"), "name": f.get("name"), "mime": f.get("mime")})
        else:
            # Unnumbered / number-0 pdf (e.g. "00.Class Workbook.pdf") -> a
            # course-level material, never a lesson-0 episode (rule 4).
            materials.append({"file_id": f.get("id"), "name": f.get("name"),
                              "size": f.get("size"), "mime": f.get("mime")})

    rec = {
        "id": rec_id,
        "type": "show",
        "title": title,
        "year": None,
        "drive_id": drive_id,
        "folder_id": folder_id,
        "poster": None,
        "tmdb_id": None,
        "overview": None,
        "quality": None,
        "shelf": shelf,
        "media": "video",
        # Cover image when the course ships one; else the first lesson's own
        # Drive video thumbnail, so every course tile gets artwork.
        "_thumb": _cover_thumb(images) or next(
            (v.get("thumb") for _, videos in seasons_src for v in videos
             if v.get("thumb")), None),
        "seasons": seasons,
    }
    if materials:
        rec["materials"] = materials
    return rec


def _build_course(node, title, shelf, drive_id):
    """Build a course record from a course FOLDER node.

    Numbered/unnumbered module subfolders become named seasons (numbered first,
    ascending; unnumbered last in drive order, rule 3). Direct videos in the
    course folder itself form a leading unnamed season — which for a flat course
    with no module subfolders is the single ``name:None`` season.
    """
    subs = [sf for sf in node.get("subfolders", []) if _has_media(sf)]
    numbered = [sf for sf in subs if _is_numbered_module(sf.get("name", ""))]
    unnumbered = [sf for sf in subs if not _is_numbered_module(sf.get("name", ""))]
    numbered.sort(key=_mod_sort_key)

    seasons_src = []
    if node.get("videos"):
        seasons_src.append((None, sorted(node["videos"], key=_lesson_key)))
    for sf in numbered + unnumbered:
        seasons_src.append((sf.get("name"), _collect_lessons(sf)))

    return _assemble(node["id"], node["id"], title, shelf, seasons_src,
                     _all_files(node), drive_id)


def _walk(node, depth, shelf, title, drive_id):
    """Recursively locate courses under a node (rule 2).

    * course       (>=2 direct videos, or a numbered-module child with media)
                   -> one record; ``title`` overrides the folder name when the
                   course was reached through wrapper(s).
    * wrapper      (no direct videos, exactly one non-empty child) -> recurse
                   through it, carrying the OUTERMOST wrapper name as the title.
    * container    (several course-bearing children) -> recurse each; this
                   folder's name becomes the shelf (nearest named container).
    """
    if depth > MAX_DEPTH:
        return []
    if _is_course(node):
        rec = _build_course(node, naming.clean_course_title(title or node.get("name") or ""),
                            shelf, drive_id)
        return [rec] if rec else []

    non_empty = [sf for sf in node.get("subfolders", []) if _has_media(sf)]
    if not node.get("videos") and len(non_empty) == 1:
        return _walk(non_empty[0], depth + 1, shelf,
                     title or node.get("name"), drive_id)

    out = []
    for sf in non_empty:
        out.extend(_walk(sf, depth + 1, node.get("name"), None, drive_id))
    return out


def _is_course(node):
    """A folder is a course with >=2 direct videos or >=1 numbered-module child."""
    if len(node.get("videos", [])) >= 2:
        return True
    return any(_is_numbered_module(sf.get("name", "")) and _has_media(sf)
               for sf in node.get("subfolders", []))


# --------------------------------------------------------------- single course --

def _is_single_course(nodes, hints):
    """Whether the whole drive is ONE course (rule 6).

    True when the ``single_course`` hint is set, or the majority of the drive's
    root folders lead with a module number ("05) ...") — the tell-tale of a
    drive whose top-level folders are modules, not separate courses.
    """
    if (hints or {}).get("single_course"):
        return True
    if not nodes:
        return False
    numbered = sum(1 for n in nodes if _ROOT_NUM_RE.match(n.get("name") or ""))
    return numbered * 2 > len(nodes)


def _order_modules(nodes):
    """Root folders in module order: numbered ascending, unnumbered last."""
    return sorted(nodes, key=_mod_sort_key)


def _build_single(drive_id, drive_name, nodes, loose_videos):
    """Build the one drive-as-course record: root folders are its modules."""
    seasons_src = []
    if loose_videos:
        seasons_src.append((None, sorted(loose_videos, key=_lesson_key)))
    for n in _order_modules(nodes):
        seasons_src.append((n.get("name"), _collect_lessons(n)))
    all_files = []
    for n in nodes:
        all_files.extend(_all_files(n))
    return _assemble(drive_id, drive_id, naming.clean_course_title(drive_name),
                     None, seasons_src, all_files, drive_id)


# -------------------------------------------------------------- loose videos --

def _is_loose_video(f):
    return (f.get("mimeType") or "").startswith("video/")


def _loose_video(f):
    """Normalise a raw loose Drive video into the internal video-dict shape."""
    vmm = f.get("videoMediaMetadata") or {}
    d = vmm.get("durationMillis")
    try:
        dur = int(d) if d is not None else None
    except (TypeError, ValueError):
        dur = None
    try:
        size = int(f.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "id": f.get("id"), "name": f.get("name") or "", "size": size,
        "duration_ms": dur, "parent_id": None, "ancestors": [],
        "thumb": f.get("thumbnailLink"), "media": "video",
    }


# --------------------------------------------------------------- entry point --

def classify_course_drive(drive_id, drive_name, nodes, loose, hints):
    """Classify a courses drive into course records. Pure.

    ``nodes`` are the walked root folder nodes, ``loose`` the raw Drive file
    dicts sitting at the drive root, ``hints`` the drive's config hints (may hold
    ``{"single_course": True}``). Returns a list of ``type:"show"`` records.
    """
    nodes = nodes or []
    hints = hints or {}
    loose_videos = [_loose_video(f) for f in (loose or [])
                    if _is_loose_video(f) and not naming.is_junk(f.get("name") or "")]

    # Whole drive is one course: root folders become its modules, loose root
    # videos ride along as a leading unnamed module.
    if _is_single_course(nodes, hints):
        rec = _build_single(drive_id, drive_name, nodes, loose_videos)
        return [rec] if rec else []

    records = []
    for node in nodes:
        records.extend(_walk(node, 0, None, None, drive_id))

    # Rare: bare videos at the drive root -> a course named after the drive.
    if loose_videos:
        rec = _assemble(drive_id, drive_id, naming.clean_course_title(drive_name),
                        None, [(None, sorted(loose_videos, key=_lesson_key))],
                        [], drive_id)
        if rec:
            records.append(rec)
    return records
