#!/usr/bin/env python3
"""TV-show cleaning / renaming engine for drive-offload.

Pure-Python (stdlib only, so it survives py2app freezing). Between torrent
identification and upload, this module rewrites messy scene-release payloads
into the ONE on-drive layout that provably nests *and* merges in drivecast:

    <Show> Season N/                       (folder, unpadded N)
    <Show> Season N/<Show> SxxEyy[ - Title].<ext>   (episode files)

Design reference: RENAME_ENGINE_PLAN.md (same directory).

Scope of this file (plan phases 0, 1, 2, 3, 5, and the disk/journal machinery
HOOK B / crash-recovery drive from offload_app.py):
  * Phase 0 — the vendored drivecast "contract" (the GATE, see _contract below).
  * Phase 1 — pure identify + normalize (R1..R13) with frozen-dataclass I/O.
  * Phase 2 — a dev CLI (`plan`).
  * Phase 3 — `RenameCache` (continuity store) + `route()` (three-tier routing).
  * Phase 5 — the server-side `heal` backfill CLI (dry-run by default).
  * HOOK B / Phase 7 support — `scan_payload`, `apply_rename`, `rollback_rename`,
    and the persisted rename journal (`write_journal`/`replay_journal`). These
    are the ONLY on-disk mutators outside the CLI; offload_app orchestrates them
    inside perform_upload (see RENAME_ENGINE_PLAN.md §3/§5/§9).

The identify/plan/route CORES do NO filesystem or network I/O — they operate on
name strings, relative-path lists, and in-memory dicts. Disk I/O is confined to
`RenameCache`, the journal helpers, `apply_rename`/`rollback_rename`, and the
CLI.
"""
import datetime
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ===========================================================================
# Phase 0 — VENDORED drivecast CONTRACT  (the GATE)
# ===========================================================================
# These regexes/functions are copied verbatim (behaviour-for-behaviour) from
#     drivecast/drivecast/naming.py
# They define what drivecast's library builder will accept. This module must
# NEVER emit a name that these reject — R13 self-checks every emitted name
# against them before a plan is returned. If drivecast's naming.py changes,
# bump CONTRACT_VERSION and re-sync this block (a shared golden fixture is the
# planned long-term guard; see RENAME_ENGINE_PLAN.md phase 9).
#
# Cross-reference (keep in sync): drivecast/drivecast/naming.py
CONTRACT_VERSION = 1

# --- verbatim from naming.py -------------------------------------------------
_C_SXXEXX_RE = re.compile(r"\bS(\d{1,2})[\s._-]?E(\d{1,3})\b", re.IGNORECASE)
_C_NXNN_RE = re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.IGNORECASE)
_C_BRACKETS_RE = re.compile(r"[\[\(\{].*?[\]\)\}]")

# The quality/release token vocabulary naming.py de-noises folders with, before
# reading a season out of them. Mirrored here so contract_split_season_suffix
# behaves byte-identically to drivecast's split_season_suffix.
_C_QUALITY_TOKENS = [
    "2160p", "1080p", "720p", "480p", "4k", "8k",
    "x264", "x265", "h264", "h265", "hevc", "av1", "xvid", "divx",
    "web-dl", "webdl", "webrip", "web", "bluray", "blu-ray", "bdrip", "brrip",
    "dvdrip", "dvd", "hdrip", "hdtv", "remux", "hdr", "hdr10", "dv", "dolby",
    "atmos", "ddp", "dd5", "aac", "ac3", "dts", "truehd", "flac", "mp3",
    "proper", "repack", "extended", "unrated", "remastered", "imax",
    "10bit", "8bit",
]
_C_QUALITY_ALT = "|".join(
    re.escape(t) for t in sorted(_C_QUALITY_TOKENS, key=len, reverse=True))
_C_QUALITY_RE = re.compile(
    r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % _C_QUALITY_ALT, re.IGNORECASE)


def _c_normalize_separators(text: str) -> str:
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _c_strip_folder_noise(name: str) -> str:
    t = _C_BRACKETS_RE.sub(" ", name or "")
    t = _C_QUALITY_RE.sub(" ", t)
    return _c_normalize_separators(t)


def contract_detect_episode(name: str):
    """drivecast's detect_episode: (season, episode) ints if TV, else None.

    The GATE for episode files — requires a *contiguous* SxxExx (a digit
    immediately after E). `S03.EP04` deliberately does NOT match here; that is
    exactly the bug the renamer exists to fix, so every episode file we emit is
    verified to match this before the plan is accepted (R13).
    """
    m = _C_SXXEXX_RE.search(name or "")
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = _C_NXNN_RE.search(name or "")
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def contract_pure_season(name: str):
    """drivecast's pure_season: a season number iff the name is JUST a season
    marker (`Season 3`, `S03`, `Series 2`, `Specials`), else None."""
    if not name:
        return None
    t = _c_strip_folder_noise(name)
    if t.lower() == "specials":
        return 0
    m = re.fullmatch(r"(?:season|series)\s*0*(\d+)", t, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"s0*(\d+)", t, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def contract_split_season_suffix(name: str):
    """drivecast's split_season_suffix: split "<Show> <season marker>" into
    (show_prefix, season_number), else (None, None).

    The GATE for season folders. A numeric RANGE like "Show Season 1-9 S01-S09"
    is REJECTED (returns (None, None)) — it stays a whole-series tile, not one
    season. The renamer must never emit a folder this rejects (R13), and must
    never emit a range folder at all (RENAME_ENGINE_PLAN.md §2).
    """
    if not name:
        return (None, None)
    t = _c_strip_folder_noise(name)
    if re.search(r"\d+\s*[-–]\s*\d+", t):  # a range (1-9, S01-S09)
        return (None, None)
    m = re.search(r"^(.*?)\s+s0*(\d+)$", t, re.IGNORECASE)
    if m:
        prefix, season = m.group(1), int(m.group(2))
        m2 = re.search(r"^(.*?)\s+(?:season|series)\s*0*(\d+)$",
                       prefix, re.IGNORECASE)
        if m2:
            prefix, season = m2.group(1), int(m2.group(2))
        prefix = prefix.strip(" -_.")
        return (prefix, season) if prefix else (None, None)
    m = re.search(r"^(.*?)\s+(?:season|series)\s*0*(\d+)$", t, re.IGNORECASE)
    if m:
        prefix = m.group(1).strip(" -_.")
        return (prefix, int(m.group(2))) if prefix else (None, None)
    m = re.search(r"^(.*?)\s+specials$", t, re.IGNORECASE)
    if m:
        prefix = m.group(1).strip(" -_.")
        return (prefix, 0) if prefix else (None, None)
    return (None, None)


# ===========================================================================
# Phase 1 — normalization ruleset R1..R13
# ===========================================================================

_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".flv",
               ".webm", ".mpg", ".mpeg", ".ts", ".m2ts"}
_SUB_EXTS = {".srt", ".ass", ".sub", ".vtt"}

# R1 — site / vanity prefix.
_SITE_BRACKET_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")
_SITE_DOMAIN_RE = re.compile(
    r"^(?:www\.)?[a-z0-9-]+\.(?:com|net|org|to|me|vip|life)\b[\s._-]*",
    re.IGNORECASE)

# R2a — episode token (the vanity-prefix fix): tolerates an EP / E.P. form and
# any run of separators, so S02E01, S02.E01, S03.EP04, S03 EP 04, S03EP04, s3e4
# all fold to canonical SxxEyy.
_R2A_RE = re.compile(r"\bS(\d{1,2})[\s._-]*E[Pp]?[\s._-]*(\d{1,3})\b",
                     re.IGNORECASE)
# R2b — NxNN (2x01).
_R2B_RE = re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.IGNORECASE)
# R2c — Season + Episode words in one string.
_R2C_RE = re.compile(
    r"\bSeason[\s._-]*0*(\d{1,2})\b.{0,16}?\bEp(?:isode)?[\s._-]*0*(\d{1,3})\b",
    re.IGNORECASE)
# R2d — episode-only marker in a file (season comes from the folder).
_R2D_RE = re.compile(r"\bE[Pp]?[\s._-]?0*(\d{1,3})\b", re.IGNORECASE)
# R2e — trailing multi-episode span (-E02 / & 02 / +02) after the primary token.
_R2E_RE = re.compile(r"^[\s._-]*[-–&+][\s._-]*E?[Pp]?0*(\d{1,3})\b",
                     re.IGNORECASE)

# Folder season markers (used to seed R2d and to name season folders).
_FOLDER_SEASON_WORD_RE = re.compile(r"\b(?:Season|Series)[\s._-]*0*(\d{1,2})\b",
                                    re.IGNORECASE)
# A bare "S03" folder token; the lookahead rejects range folders (S01-S09).
_FOLDER_SEASON_S_RE = re.compile(
    r"\bS0*(\d{1,2})\b(?!\s*[-–]?\s*S?\d)", re.IGNORECASE)
_RANGE_RE = re.compile(r"\d+\s*[-–]\s*\d+")

# R3 — dotted acronym protection (S.W.A.T.).
_ACRONYM_RE = re.compile(r"\b(?:[A-Za-z]\.){2,}")

# R4 — release-noise truncation. Everything from the first such token onward is
# quality/source/codec metadata, not part of the title.
_NOISE_RE = re.compile(
    r"(?<![a-z0-9])(?:2160p|1080p|720p|576p|480p|4k|uhd|hdr10\+?|hdr|dv|dovi|"
    r"web[\s._-]?dl|webrip|web|blu[\s._-]?ray|bdrip|brrip|hdtv|dvdrip|hdrip|"
    r"remux|hybrid|x26[45]|h[\s._-]?26[45]|hevc|avc|av1|aac(?:2[\s._-]?0)?|"
    r"e?ac3|ddp?[\s._-]?[257][\s._-]?[01]|atmos|dts(?:-?hd)?|10bit|8bit|"
    r"esubs?|msubs?|dual[\s._-]?audio|multi|amzn|nf|dsnp|hmax|atvp|complete|"
    r"proper|repack|internal|extended|uncut|remastered)(?![a-z0-9]).*$",
    re.IGNORECASE)

# R5 — year (lookbehind protects "'70s" from being read as a year token).
_YEAR_RE = re.compile(r"(?<!')\b(?:19|20)\d{2}\b")
_BRACKETS_RE = re.compile(r"[\[\(\{][^\)\]\}]*[\)\]\}]")

# R11 — subtitle language suffix (".en.srt", ".pt-BR.ass").
_SUB_LANG_RE = re.compile(r"\.([a-z]{2,3}(?:-[A-Za-z]{2})?)\.(srt|ass|sub|vtt)$",
                          re.IGNORECASE)

# R12 — junk names to prune. NEVER matches .part / .aria2 control files.
_JUNK_FOLDER_RE = re.compile(r"^0?\.?\s*websites?\s+you\s+may\s+like$",
                             re.IGNORECASE)
_JUNK_SAMPLE_RE = re.compile(r"\bsample\b", re.IGNORECASE)


# --- small helpers ----------------------------------------------------------

def _split_ext(name: str) -> Tuple[str, str]:
    """Split a basename into (root, ext) keeping the original ext casing."""
    root, ext = os.path.splitext(name)
    return root, ext


def _slug(text: str) -> str:
    """R7 cache/log key: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", (text or "").casefold())


def _title_case(text: str) -> str:
    out = []
    for w in text.split():
        if w.isupper() and len(w) <= 4:
            out.append(w)          # keep short all-caps (acronyms)
        else:
            out.append(w[:1].upper() + w[1:].lower() if w else w)
    return " ".join(out)


def _extract_year(text: str):
    matches = [m.group(0) for m in _YEAR_RE.finditer(text or "")]
    return int(matches[-1]) if matches else None


def _sanitize(name: str) -> str:
    """R10 — filesystem-safe, NFC-normalized, control-char-free, <=200 chars."""
    name = name.replace("/", "-").replace("\\", "-").replace(":", "-")
    name = "".join(ch for ch in name if ord(ch) >= 32)
    name = unicodedata.normalize("NFC", name)
    root, ext = _split_ext(name)
    if len(root) > 200:
        root = root[:200].rstrip(" .")
    name = (root + ext).strip()
    return name.rstrip(" .") if not ext else name


def _clean_show(region: str) -> Tuple[str, Optional[int]]:
    """R1/R3/R5/R6 — turn a raw show segment into a display title + year.

    Returns (display_show, year_or_None). Dotted acronyms (S.W.A.T.) survive,
    "'70s" is protected from year-stripping, brackets/site prefixes are removed.
    """
    s = region or ""
    # R1 — leading [tag] groups and site/domain prefixes.
    s = _SITE_BRACKET_RE.sub("", s)
    s = _SITE_DOMAIN_RE.sub("", s)
    # R3 — protect dotted acronyms before touching separators.
    holders = {}

    def _hold(m):
        k = "\x00%d\x00" % len(holders)
        holders[k] = m.group(0)
        return k

    s = _ACRONYM_RE.sub(_hold, s)
    # R3 — dotted/underscored release names -> spaces.
    if " " not in s and (s.count(".") + s.count("_")) >= 2:
        s = re.sub(r"[._]", " ", s)
    else:
        s = re.sub(r"_+", " ", s)
        s = re.sub(r"(?<=\w)\.(?=\w)", " ", s)   # lone dot between words
    # R5 — year (kept for the slug only) then brackets.
    year = _extract_year(s)
    s = _BRACKETS_RE.sub(" ", s)
    if year is not None:
        s = re.sub(r"\(?\b%d\b\)?" % year, " ", s)
    # R6 — separator/case cleanup.
    s = re.sub(r"[\s._-]{2,}", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_.")
    if s and "\x00" not in s and (s.upper() == s or s.lower() == s):
        s = _title_case(s)
    s = s.strip(" -_")            # trim spaces/dashes/underscores...
    for k, v in holders.items():  # ...before restoring acronym dots (S.W.A.T.)
        s = s.replace(k, v)
    return s, year


def _clean_title(tail: str) -> Optional[str]:
    """The episode title: text after the SxxEyy token, quality-free."""
    t = _NOISE_RE.sub("", tail or "")          # R4 — cut at first noise token
    t = _BRACKETS_RE.sub(" ", t)               # R5 — drop brackets
    t = re.sub(r"[._]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -_.")
    return t or None


def _folder_season(name: str):
    """R2d support — a folder's season as (season, match_start), else None.

    Returns None for numeric-range folders (series packs); their per-file
    seasons decide the layout instead. Never reads a season out of a range.
    """
    if not name:
        return None
    if _RANGE_RE.search(name):          # a range folder is not one season
        return None
    m = _FOLDER_SEASON_WORD_RE.search(name)
    if m:
        return (int(m.group(1)), m.start())
    m = _FOLDER_SEASON_S_RE.search(name)
    if m:
        return (int(m.group(1)), m.start())
    return None


# --- episode parsing --------------------------------------------------------

@dataclass(frozen=True)
class EpisodeParse:
    """Result of parsing a single episode basename."""
    season: int
    episode: int
    show: str                       # cleaned display show
    title: Optional[str]
    last_episode: Optional[int]     # end of a multi-episode span, else None
    year: Optional[int]


def _episode_match(base: str):
    """Try R2a -> R2b -> R2c in order. Returns (season, ep, start, end) or None.
    Run on the raw base (separators intact) so R2a can absorb S03.EP04 etc."""
    m = _R2A_RE.search(base)
    if m:
        return (int(m.group(1)), int(m.group(2)), m.start(), m.end())
    m = _R2B_RE.search(base)
    if m:
        return (int(m.group(1)), int(m.group(2)), m.start(), m.end())
    m = _R2C_RE.search(base)
    if m:
        return (int(m.group(1)), int(m.group(2)), m.start(), m.end())
    return None


def parse_episode(basename: str, folder_season: Optional[int] = None
                  ) -> Optional[EpisodeParse]:
    """Parse an episode filename per R1..R6. Returns EpisodeParse or None.

    `folder_season` supplies the season for episode-only files (R2d), e.g. an
    `EP04.mkv` inside a `... S03 ...` folder.
    """
    if not basename:
        return None
    root, _ext = _split_ext(basename)

    hit = _episode_match(root)
    if hit is not None:
        season, episode, start, end = hit
    else:
        # R2d — episode-only marker, season from the enclosing folder.
        if folder_season is None:
            return None
        m = _R2D_RE.search(root)
        if not m:
            return None
        season, episode, start, end = folder_season, int(m.group(1)), \
            m.start(), m.end()

    # R2e — a trailing multi-episode span (S01E01-E02).
    last_episode = None
    tail = root[end:]
    mspan = _R2E_RE.match(tail)
    if mspan:
        last_episode = int(mspan.group(1))
        end += mspan.end()
        tail = root[end:]

    show, year = _clean_show(root[:start])
    title = _clean_title(tail)
    return EpisodeParse(season=season, episode=episode, show=show,
                        title=title, last_episode=last_episode, year=year)


# --- canonical emitters (R9/R10) --------------------------------------------

def canonical_folder_name(show: str, season: int) -> str:
    """`<Show> Season N` (unpadded N). Never a bare/pure `Season NN`."""
    return _sanitize("%s Season %d" % (show, season))


def canonical_episode_name(show: str, ep: EpisodeParse, ext: str) -> str:
    """`<Show> SxxEyy[ - Title].<ext>`."""
    tok = "S%02dE%02d" % (ep.season, ep.episode)
    if ep.last_episode and ep.last_episode != ep.episode:
        tok += "-E%02d" % ep.last_episode
    name = "%s %s" % (show, tok)
    if ep.title:
        name += " - %s" % ep.title
    return _sanitize(name + ext)


def _is_junk(name: str) -> bool:
    """R12 — prune AppleDouble twins, .DS_Store, .url/.nfo, samples, ad folders.
    NEVER matches .part / .aria2 control files (load-bearing elsewhere)."""
    n = (name or "").strip()
    if not n:
        return False
    low = n.lower()
    if low.endswith(".part") or low.endswith(".aria2"):
        return False
    if n.startswith("._") or n == ".DS_Store":
        return True
    if low.endswith(".url") or low.endswith(".nfo"):
        return True
    if _JUNK_FOLDER_RE.match(n):
        return True
    if _JUNK_SAMPLE_RE.search(n):
        return True
    return False


# ===========================================================================
# Data contracts
# ===========================================================================

@dataclass(frozen=True)
class TVIdent:
    """Identification result for a name or a payload's relative paths."""
    is_tv: bool
    show_raw: str
    show: str
    show_slug: str
    year: Optional[int]
    season: Optional[int]
    episodes: tuple            # ((relpath, season, episode, title), ...)
    layout_hint: str           # season_folder | loose_file | series_pack | none
    confidence: float


@dataclass(frozen=True)
class RenamePlan:
    """A deterministic rename plan. `ops` = file_ops + dir_ops (each (src,dst),
    relative paths). Files must be applied before folders; folders deepest-first
    (see apply ordering in the CLI). `deletes` are junk paths (files then dirs).
    A rejected plan carries no ops — the caller falls back to upload-as-is."""
    ops: tuple
    deletes: tuple
    rejected: bool
    reason: str
    is_tv: bool = False
    show: str = ""
    file_ops: tuple = ()
    dir_ops: tuple = ()


# ===========================================================================
# identify()  — pure
# ===========================================================================

def identify(name_or_relpaths) -> TVIdent:
    """Identify a single name string OR a list of relative paths as TV.

    Pure: no filesystem/network access. For a list, all leaf files are examined
    and the dominant show/season is reported.
    """
    if isinstance(name_or_relpaths, str):
        relpaths = [name_or_relpaths]
    else:
        relpaths = list(name_or_relpaths)

    files = [p for p in relpaths if p and not p.endswith("/")]
    # Folder season, if every path shares one top-level folder.
    tops = {p.split("/")[0] for p in files if "/" in p}
    folder_season = None
    if len(tops) == 1:
        fs = _folder_season(next(iter(tops)))
        if fs is not None:
            folder_season = fs[0]

    episodes = []
    shows = []
    years = []
    for p in files:
        base = p.split("/")[-1]
        root, ext = _split_ext(base)
        if ext.lower() not in _VIDEO_EXTS:
            continue
        if _is_junk(base):
            continue
        ep = parse_episode(base, folder_season=folder_season)
        if ep is None:
            continue
        episodes.append((p, ep.season, ep.episode, ep.title))
        shows.append(ep.show)
        if ep.year is not None:
            years.append(ep.year)

    is_tv = bool(episodes)
    show = _dominant(shows) if shows else ""
    # A lone folder that is a season folder but whose only clue is its name.
    if not show and len(tops) == 1:
        fs = _folder_season(next(iter(tops)))
        if fs is not None:
            show = _clean_show(next(iter(tops))[:fs[1]])[0]
    year = years[0] if years else None
    seasons = {e[1] for e in episodes}
    season = next(iter(seasons)) if len(seasons) == 1 else None

    if not is_tv:
        layout = "none"
    elif len(seasons) > 1:
        layout = "series_pack"
    elif tops:
        layout = "season_folder"
    else:
        layout = "loose_file"
    confidence = 1.0 if is_tv and folder_season is None else (
        0.8 if is_tv else 0.0)

    slug = _slug(show)
    if year is not None:
        slug += str(year)
    return TVIdent(is_tv=is_tv, show_raw=show, show=show, show_slug=slug,
                   year=year, season=season, episodes=tuple(episodes),
                   layout_hint=layout, confidence=confidence)


def _dominant(items):
    counts = {}
    for it in items:
        counts[it] = counts.get(it, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]


# ===========================================================================
# plan()  — pure
# ===========================================================================

def plan(relpaths, show_override: Optional[str] = None) -> RenamePlan:
    """Build a rename plan for a payload's relative paths. Pure: no I/O.

    Groups paths by top-level component; each group is a season folder (renamed
    in place) or a loose/split payload (files wrapped into `<Show> Season N`).
    R13 self-check gates the whole plan: on any contract violation or duplicate
    destination the plan is rejected (caller uploads as-is).
    """
    files = [p.rstrip("/") for p in relpaths if p and not p.endswith("/")]
    groups = {}
    for p in files:
        groups.setdefault(p.split("/")[0], []).append(p)

    file_ops: List[Tuple[str, str]] = []
    dir_ops: List[Tuple[str, str]] = []
    deletes: List[str] = []
    any_tv = False
    show_seen = ""

    for top in sorted(groups):
        res = _plan_group(top, groups[top], show_override)
        if res is None:
            continue
        any_tv = True
        show_seen = res["show"] or show_seen
        file_ops.extend(res["file_ops"])
        dir_ops.extend(res["dir_ops"])
        deletes.extend(res["deletes"])

    ops = file_ops + dir_ops
    ok, reason = _validate_ops(ops, dir_ops)
    if not ok:
        return RenamePlan(ops=(), deletes=tuple(deletes), rejected=True,
                          reason=reason, is_tv=any_tv, show=show_seen)
    return RenamePlan(ops=tuple(ops), deletes=tuple(deletes), rejected=False,
                      reason="", is_tv=any_tv, show=show_seen,
                      file_ops=tuple(file_ops), dir_ops=tuple(dir_ops))


def _plan_group(top, members, show_override):
    """Plan one top-level group. Returns a dict of ops or None if not TV."""
    is_folder = any("/" in m for m in members)
    deletes = []
    kept = []
    for m in members:
        if _is_junk(m.split("/")[-1]):
            deletes.append(m)
        else:
            kept.append(m)

    folder_season = None
    folder_show = None
    if is_folder:
        fs = _folder_season(top)
        if fs is not None:
            folder_season = fs[0]
            folder_show = _clean_show(top[:fs[1]])[0]

    videos = []          # (relpath, EpisodeParse, ext)
    subs = []            # (relpath, ext)
    for m in kept:
        base = m.split("/")[-1]
        _root, ext = _split_ext(base)
        low = ext.lower()
        if low in _SUB_EXTS:
            subs.append((m, ext))
            continue
        if low not in _VIDEO_EXTS:
            continue
        ep = parse_episode(base, folder_season=folder_season)
        if ep is not None:
            videos.append((m, ep, ext))

    if not videos:
        return None       # not confidently TV — leave the group untouched

    show = show_override or folder_show or _dominant([v[1].show for v in videos])
    seasons = {v[1].season for v in videos}

    file_ops = []
    dir_ops = []
    # Map old episode relpath -> its new leaf name, for subtitle lockstep.
    ep_new_leaf = {}
    ep_key = {}          # (season, episode) -> new stem (no ext), for subs

    single_season_folder = is_folder and len(seasons) == 1

    if single_season_folder:
        season = next(iter(seasons))
        newfolder = canonical_folder_name(show, season)
        if top != newfolder:
            dir_ops.append((top, newfolder))
        for (m, ep, ext) in videos:
            nb = canonical_episode_name(show, ep, ext)
            parent = "/".join(m.split("/")[:-1])   # OLD parent (renamed later)
            dst = parent + "/" + nb if parent else nb
            ep_new_leaf[m] = nb
            ep_key[(ep.season, ep.episode)] = (_split_ext(nb)[0], parent)
            if m != dst:
                file_ops.append((m, dst))
    else:
        # Loose file, multi-season pack, or a non-season folder: wrap/split
        # each episode into its own `<Show> Season N/` (rclone auto-creates).
        for (m, ep, ext) in videos:
            newfolder = canonical_folder_name(show, ep.season)
            nb = canonical_episode_name(show, ep, ext)
            dst = newfolder + "/" + nb
            ep_new_leaf[m] = nb
            ep_key[(ep.season, ep.episode)] = (_split_ext(nb)[0], newfolder)
            if m != dst:
                file_ops.append((m, dst))
        if is_folder:
            deletes.append(top)   # the emptied source folder

    # R11 — subtitle lockstep: a sub whose episode tokens match a video's is
    # renamed to that video's stem + preserved language suffix. Orphans are
    # left untouched (never forced into the SxxEyy contract).
    for (m, ext) in subs:
        base = m.split("/")[-1]
        ep = parse_episode(base, folder_season=folder_season)
        if ep is None:
            continue
        target = ep_key.get((ep.season, ep.episode))
        if target is None:
            continue
        stem, parent = target
        lang = ""
        lm = _SUB_LANG_RE.search(base)
        if lm:
            lang = "." + lm.group(1)
        nb = _sanitize(stem + lang + ext)
        dst = parent + "/" + nb if parent else nb
        if m != dst:
            file_ops.append((m, dst))

    return {"show": show, "file_ops": file_ops, "dir_ops": dir_ops,
            "deletes": deletes}


def _validate_ops(ops, dir_ops):
    """R13 contract self-check. Returns (ok, reason)."""
    dir_srcs = {s for (s, _d) in dir_ops}
    seen_dst = {}
    for (src, dst) in ops:
        leaf = dst.split("/")[-1]
        is_dir = src in dir_srcs
        if is_dir:
            # Every folder must pass the vendored non-range split_season_suffix.
            if contract_split_season_suffix(leaf) == (None, None):
                return (False, "folder %r fails contract split_season_suffix"
                        % leaf)
        else:
            # Every episode file must match the vendored contiguous SxxExx.
            if not _C_SXXEXX_RE.search(_split_ext(leaf)[0]):
                return (False, "file %r fails contract detect_episode" % leaf)
            # The synthesized parent folder (loose/split wrap) must pass too.
            parent = "/".join(dst.split("/")[:-1])
            if parent and parent not in dir_srcs.union(
                    {d for (_s, d) in dir_ops}):
                tail = parent.split("/")[-1]
                if contract_split_season_suffix(tail) != (None, None):
                    pass  # a canonical `<Show> Season N` — good
                # else: parent is a pre-existing folder we don't rename; allow
        if dst in seen_dst:
            return (False, "two ops target the same destination %r" % dst)
        seen_dst[dst] = src
    return (True, "")


# ===========================================================================
# Sequence planning — synthesize the SxxEyy contract for an ORDERED sequence of
# source videos (e.g. a YouTube playlist) whose filenames carry no episode
# markers. Season/episode numbers are assigned by the CALLER (yt-show, which
# holds the per-show cache); this layer only cleans titles, builds canonical
# names, and gates them through the same R13 contract check every plan gets.
# ===========================================================================

_YT_LEAD_EP_RE = re.compile(r"^\s*E[Pp]?(?:isode)?[\s._#:\-]*\d+[\s._:\-]*",
                            re.IGNORECASE)


def clean_source_title(title, show=""):
    """A raw source (YouTube) video title -> an episode title str, or None.

    Strips a leading '<show>' prefix, a leading 'Episode N'/'Ep N' token, any
    embedded SxxEyy/NxNN marker, #hashtags, pipes, brackets, and emoji/symbol/
    control chars, then collapses separators. Original casing is preserved
    (source titles are already human-cased); returns None if nothing usable
    remains.

    NOTE: unlike _clean_title (used for scene releases), this does NOT truncate
    at the R4 noise vocabulary — ordinary video titles legitimately contain
    words like "web", "complete", "extended", "multi" ("Caught in the Web of
    Lies" must survive intact), so only brackets/separators are cleaned.
    """
    t = title or ""
    if show:
        t = re.sub(r"^\s*" + re.escape(show) + r"\s*[-:|–—]+\s*", "",
                   t, flags=re.IGNORECASE)
    t = _YT_LEAD_EP_RE.sub("", t)
    hit = _episode_match(t)
    if hit is not None:
        _s, _e, start, end = hit
        t = t[:start] + " " + t[end:]
    t = re.sub(r"#\w+", " ", t)
    t = t.replace("|", " ")
    t = "".join(ch for ch in t
                if unicodedata.category(ch)[0] != "C"
                and unicodedata.category(ch) not in ("So", "Sk"))
    t = _BRACKETS_RE.sub(" ", t)               # drop [..]/(..) groups
    t = re.sub(r"[._]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -_.")
    return t or None


def synth_episode(show, season, episode, raw_title):
    """Build an EpisodeParse for one sequence item (no multi-episode span)."""
    return EpisodeParse(season=season, episode=episode, show=show,
                        title=clean_source_title(raw_title, show),
                        last_episode=None, year=None)


def plan_from_sequence(items, show):
    """Plan canonical names for an ORDERED sequence with PRE-ASSIGNED seasons.

    items: iterable of (video_id, season, episode, raw_title, ext); ext includes
    the leading dot. video_id is carried through as each op's 'src' token — this
    plan is a naming + contract-validation GATE / preview, NOT fed to
    apply_rename (yt-show names files canonically at download time). Returns a
    RenamePlan; on any contract violation or duplicate destination it is
    rejected (rejected=True, ops empty).
    """
    file_ops = []
    folders = []
    for (vid, season, episode, raw_title, ext) in items:
        ep = synth_episode(show, season, episode, raw_title)
        folder = canonical_folder_name(show, season)
        leaf = canonical_episode_name(show, ep, ext)
        if folder not in folders:
            folders.append(folder)
        file_ops.append((vid, folder + "/" + leaf))
    # _validate_ops only gates folders that appear in dir_ops; the season
    # folders synthesized here never do, so gate them explicitly against the
    # same contract (a show name like "9-1-1" trips the range regex — R13).
    for folder in folders:
        if contract_split_season_suffix(folder.split("/")[-1]) == (None, None):
            return RenamePlan(ops=(), deletes=(), rejected=True,
                              reason="folder %r fails contract "
                              "split_season_suffix" % folder.split("/")[-1],
                              is_tv=True, show=show)
    ok, reason = _validate_ops(file_ops, ())
    if not ok:
        return RenamePlan(ops=(), deletes=(), rejected=True, reason=reason,
                          is_tv=True, show=show)
    return RenamePlan(ops=tuple(file_ops), deletes=(), rejected=False, reason="",
                      is_tv=True, show=show, file_ops=tuple(file_ops), dir_ops=())


# ===========================================================================
# Support-dir resolution (mirrors offload_app.support_dir, kept independent so
# renamer stays importable without offload_app; callers may override paths).
# ===========================================================================

def _default_support_dir():
    """Where the mutable JSON lives. Mirrors offload_app.support_dir(): a frozen
    py2app bundle writes to ~/Library/Application Support/drive-offload; running
    from source writes beside this script. Callers can override every path, so
    this is only the default."""
    if getattr(sys, "frozen", False):
        return os.path.expanduser("~/Library/Application Support/drive-offload")
    return os.path.dirname(os.path.abspath(__file__))


def _today():
    return datetime.date.today().isoformat()


# ===========================================================================
# Phase 3 — RenameCache  (continuity store; cloned from DecisionStore's
# RLock + write-temp + os.replace discipline, offload_app.py DecisionStore)
# ===========================================================================

def _default_cache_path():
    return os.path.join(_default_support_dir(), "rename_cache.json")


class RenameCache:
    """Persists per-show continuity + naming templates to rename_cache.json.

    Schema (RENAME_ENGINE_PLAN.md §5): a top-level {"version", "shows"} where
    each show is keyed by its slug and carries display/aliases/year/drive/
    drive_id/layout/formats/keep_episode_titles/confirmed_by_user plus a
    `seasons` map and a `history` list.

    Concurrency mirrors offload_app.DecisionStore exactly (an RLock guards every
    mutation and the file is replaced atomically via a .tmp + os.replace), so
    the poll timer and the upload worker can touch it from two threads without
    tearing the JSON. Pure stdlib — survives py2app freezing."""

    def __init__(self, path=None):
        self.path = path or _default_cache_path()
        self.data = {"version": 1, "shows": {}}
        self._lock = threading.RLock()
        self.load()

    def load(self):
        with self._lock:
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data = loaded
                    self.data.setdefault("version", 1)
                    self.data.setdefault("shows", {})
            except (OSError, ValueError):
                self.data = {"version": 1, "shows": {}}
            return self.data

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2)
                os.replace(tmp, self.path)
            except OSError:
                pass  # best-effort; a lost write just re-derives next time

    def _shows(self):
        return self.data.setdefault("shows", {})

    def lookup(self, slug, aliases=None):
        """Return a COPY of the show record whose slug or aliases match `slug`
        or any of `aliases`, else None. Read-only (returns a copy so callers
        can't mutate the cache without going through record_*)."""
        with self._lock:
            shows = self.data.get("shows", {})
            keys = {slug} | set(aliases or [])
            keys.discard(None)
            if slug in shows:
                return dict(shows[slug])
            for s, rec in shows.items():
                if s in keys:
                    return dict(rec)
                for a in rec.get("aliases", []):
                    if a in keys:
                        return dict(rec)
            return None

    def record_show(self, slug, display, aliases=None, year=None, drive=None,
                    drive_id=None, layout="shared", folder_format=None,
                    episode_format=None, keep_episode_titles=True,
                    confirmed_by_user=True):
        """Create (or update the stub of) a show record. Called on first-encounter
        confirm — it never clobbers an existing show's seasons/history, only
        fills/refreshes the display/drive/alias fields. Returns a copy."""
        with self._lock:
            shows = self._shows()
            rec = shows.get(slug)
            if rec is None:
                rec = {
                    "display": display,
                    "aliases": [],
                    "year": year,
                    "drive": drive,
                    "drive_id": drive_id,
                    "layout": layout,
                    "folder_format": folder_format or "{show} Season {season}",
                    "episode_format": (episode_format or
                                       "{show} S{season:02d}E{episode:02d}"
                                       "{title_part}"),
                    "keep_episode_titles": keep_episode_titles,
                    "confirmed_by_user": confirmed_by_user,
                    "seasons": {},
                    "history": [],
                }
                shows[slug] = rec
            else:
                if display:
                    rec["display"] = display
                if drive:
                    rec["drive"] = drive
                if drive_id:
                    rec["drive_id"] = drive_id
                if year is not None:
                    rec["year"] = year
                rec["confirmed_by_user"] = (confirmed_by_user or
                                            rec.get("confirmed_by_user", False))
            for a in (aliases or []):
                if a and a != slug and a not in rec["aliases"]:
                    rec["aliases"].append(a)
            self.save()
            return dict(rec)

    def add_alias(self, slug, alias):
        """Grow a known show's alias list (e.g. a new release group's slug), so
        future variants resolve without a prompt."""
        with self._lock:
            rec = self._shows().get(slug)
            if rec is None or not alias or alias == slug:
                return
            rec.setdefault("aliases", [])
            if alias not in rec["aliases"]:
                rec["aliases"].append(alias)
                self.save()

    def record_season(self, slug, season, gid, episodes, source_pattern,
                      uploaded=None):
        """Record that season N landed. Called ONLY from a verified upload
        success, so the cache never claims a season that didn't upload."""
        with self._lock:
            rec = self._shows().get(slug)
            if rec is None:
                return
            rec.setdefault("seasons", {})[str(season)] = {
                "gid": gid,
                "uploaded": uploaded or _today(),
                "episodes": episodes,
                "source_pattern": source_pattern,
            }
            self.save()

    def append_history(self, slug, entries):
        """Append per-episode rename records (the log decisions.json never had).
        Called only on upload success."""
        with self._lock:
            rec = self._shows().get(slug)
            if rec is None or not entries:
                return
            rec.setdefault("history", []).extend(entries)
            self.save()


# ===========================================================================
# Phase 3 — continuity routing  (route + decisions.json mining)
# ===========================================================================

@dataclass(frozen=True)
class RouteResult:
    """Where a show's prior season went. `source` is "cache" (Tier 1) or
    "decisions" (Tier 2); `confidence` is 1.0 for an authoritative cache/exact
    hit, lower for a fuzzy decisions match."""
    drive: str
    drive_id: Optional[str]
    source: str
    confidence: float


@dataclass(frozen=True)
class _Mined:
    slug: str
    base: str
    dist: frozenset
    drive: str
    gid: str


# Region qualifiers that distinguish otherwise-identical shows ("The Office UK"
# vs "The Office US"/"The Office 2005"). A trailing one of these in a slug is a
# distinguisher, not part of the base name.
_REGION_QUALIFIERS = ("usa", "gbr", "nzl", "aus", "uk", "us", "au", "ca",
                      "nz", "ie", "gb")
_SLUG_YEAR_RE = re.compile(r"(?:19|20)\d{2}$")


def _slug_base_dist(slug):
    """Split a slug into (base, distinguishers). A distinguisher is a trailing
    year and/or a trailing region qualifier — the tokens that must AGREE before
    two near-identical shows are allowed to merge (the year/qualifier guard)."""
    s = slug or ""
    dist = set()
    m = _SLUG_YEAR_RE.search(s)
    if m:
        dist.add(("y", int(m.group(0))))
        s = s[:m.start()]
    for q in _REGION_QUALIFIERS:
        if s.endswith(q) and len(s) > len(q) + 2:
            dist.add(("r", q))
            s = s[:-len(q)]
            break
    return s, frozenset(dist)


def derive_show_key(name):
    """Extract a (slug, base) key from a raw torrent/decision NAME string.

    Runs the name through R1..R7: find its episode/season marker, clean the
    show segment before it, slug it (with year appended). Returns None when the
    name carries no TV marker at all — that is how movies/music in decisions.json
    are ignored by the miner."""
    if not name:
        return None
    n = name
    if n.startswith("[METADATA]"):
        n = n[len("[METADATA]"):]
    n = n.replace("+", " ")            # magnet-style names use '+' separators
    root, _ext = _split_ext(n)
    hit = _episode_match(root)
    if hit is not None:
        region = root[:hit[2]]
    else:
        fs = _folder_season(root)
        if fs is None:
            return None
        region = root[:fs[1]]
    show, year = _clean_show(region)
    if not show:
        return None
    slug = _slug(show)
    if year is not None:
        slug += str(year)
    return slug, show


def _mine_decisions(decisions):
    """Index decisions.json (a {gid: record} dict) as slug -> drive for every
    record that chose a drive ("drive:<D>" or "already:<D>"). Non-TV records
    (no episode/season marker) are dropped. Returns a list of _Mined."""
    out = []
    for gid, rec in (decisions or {}).items():
        if not isinstance(rec, dict):
            continue
        choice = rec.get("choice") or ""
        if choice.startswith("drive:"):
            drive = choice[len("drive:"):]
        elif choice.startswith("already:"):
            drive = choice[len("already:"):]
        else:
            continue
        if not drive:
            continue
        key = derive_show_key(rec.get("name") or "")
        if key is None:
            continue
        slug = key[0]
        base, dist = _slug_base_dist(slug)
        out.append(_Mined(slug=slug, base=base, dist=dist, drive=drive,
                          gid=gid))
    return out


def _match_score(q_slug, q_base, q_dist, m):
    """Score a query key against one mined show. 0.0 = no match. Order:
    exact slug -> base equality/containment (<=~1 token) -> difflib >=0.85.
    The year/qualifier guard blocks every NON-exact match whose distinguishers
    disagree ("The Office UK" must never wire onto "The Office 2005")."""
    if q_slug == m.slug:
        return 1.0
    # Guard: for any fuzzy match, distinguishers must not conflict.
    if q_dist and m.dist and q_dist != m.dist:
        return 0.0
    if q_base and m.base:
        if q_base == m.base:
            return 0.95
        shorter, longer = sorted((q_base, m.base), key=len)
        # Containment with a bounded extra (~one short vanity token, e.g.
        # "northbound" inside "prodnorthbound").
        if shorter and shorter in longer and (len(longer) - len(shorter)) <= 5:
            return 0.9
    ratio = difflib.SequenceMatcher(None, q_base, m.base).ratio()
    return ratio if ratio >= 0.85 else 0.0


def route(slug, aliases, cache, decisions):
    """Three-tier continuity lookup (RENAME_ENGINE_PLAN.md §5).

    Tier 1 — the RenameCache (authoritative): an exact slug/alias hit returns
             the pinned drive.
    Tier 2 — mine decisions.json (cold start): index slug->drive and match by
             exact slug -> containment -> difflib, with the year/qualifier guard.
    Tier 3 — estate probe (one `rclone lsd`): DEFERRED, no network for now.

    Returns a RouteResult or None."""
    aliases = [a for a in (aliases or []) if a]
    keys = [slug] + aliases

    # Tier 1 — cache.
    if cache is not None:
        rec = cache.lookup(slug, aliases)
        if rec and rec.get("drive"):
            return RouteResult(drive=rec["drive"],
                               drive_id=rec.get("drive_id"),
                               source="cache", confidence=1.0)

    # Tier 2 — mine decisions.
    index = _mine_decisions(decisions)
    best, best_score = None, 0.0
    for m in index:
        for key in keys:
            kb, kd = _slug_base_dist(key)
            score = _match_score(key, kb, kd, m)
            if score > best_score:
                best_score, best = score, m
    if best is not None and best_score > 0.0:
        return RouteResult(drive=best.drive, drive_id=None,
                           source="decisions", confidence=best_score)

    # Tier 3 — estate probe: TODO (one `rclone lsd` to adopt an existing
    # on-drive folder convention). Deliberately skipped to stay no-network.
    return None


# ===========================================================================
# HOOK B / Phase 7 — on-disk apply, rollback, and the persisted journal
# (the ONLY filesystem mutators outside the CLI). offload_app.perform_upload
# orchestrates these between engine-stop and rclone-move.
# ===========================================================================

def scan_payload(abs_path):
    """(base_dir, relpaths) for a completed download's on-disk payload.

    relpaths are FILES only, relative to `base_dir` = the payload's PARENT, so
    the payload's own top folder (or a loose file's own name) is the first path
    component — exactly the frame plan() groups on. A single loose file yields
    its own basename; a directory yields every file under it."""
    abs_path = os.path.abspath(abs_path)
    base_dir = os.path.dirname(abs_path)
    if os.path.isdir(abs_path):
        rels = []
        for root, _dirs, files in os.walk(abs_path):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), base_dir)
                rels.append(rel.replace(os.sep, "/"))
        return base_dir, rels
    return base_dir, [os.path.basename(abs_path)]


def plan_new_root(planobj):
    """The single new top-level component every op resolves under, or None if
    the plan produces MULTIPLE top-level roots (a multi-season series pack —
    the live hook declines to rename those, since one upload maps to one root).

    A season-folder rename keeps each episode file's dst under the OLD folder
    name (it moves in place; the folder rename relocates it), so a file dst's
    top must be resolved through the top-level dir renames before counting."""
    remap = {}
    for src, dst in planobj.dir_ops:
        if "/" not in src:                 # a top-level folder rename
            remap[src] = dst.split("/")[0]
    tops = set()
    for _s, dst in planobj.ops:
        top = dst.split("/")[0]
        tops.add(remap.get(top, top))
    if len(tops) == 1:
        return next(iter(tops))
    return None


def _depth(relpath):
    return relpath.count("/")


def build_rename_ops(base_dir, planobj):
    """Ordered absolute (src, dst) rename ops: files deepest-first, then folders
    deepest-first (root dir last). This is both the apply order and the journal
    order; rollback simply reverses it."""
    files = sorted(planobj.file_ops, key=lambda o: _depth(o[0]), reverse=True)
    dirs = sorted(planobj.dir_ops, key=lambda o: _depth(o[0]), reverse=True)
    ops = [(os.path.join(base_dir, s), os.path.join(base_dir, d))
           for s, d in files]
    ops += [(os.path.join(base_dir, s), os.path.join(base_dir, d))
            for s, d in dirs]
    return ops


def apply_rename(base_dir, planobj, log=None):
    """Apply a plan on disk under base_dir. Returns the list of (abs_src,abs_dst)
    renames actually performed (for rollback). Order:
      1. delete junk FILES (old paths still valid, before any folder rename);
      2. rename files deepest-first;
      3. rename folders deepest-first (root last);
      4. rmdir emptied source folders (a split series pack's old container).
    NEVER deletes a .part/.aria2 control file (plan()'s _is_junk already refuses
    to, and this only acts on plan.deletes). Raises OSError on a rename failure
    so the caller can roll back via the persisted journal."""
    log = log or (lambda *a: None)
    dir_deletes = []
    for d in planobj.deletes:
        ab = os.path.join(base_dir, d)
        if os.path.isdir(ab):
            dir_deletes.append(ab)
            continue
        try:
            os.remove(ab)
        except FileNotFoundError:
            pass
        except OSError as e:
            log("rename: could not delete junk %s: %s" % (ab, e))

    done = []
    for a_src, a_dst in build_rename_ops(base_dir, planobj):
        parent = os.path.dirname(a_dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        os.rename(a_src, a_dst)
        done.append((a_src, a_dst))

    for ab in dir_deletes:
        try:
            os.rmdir(ab)
        except OSError as e:
            log("rename: leftover source dir not removed %s: %s" % (ab, e))
    return done


def rollback_rename(renames, log=None):
    """Undo renames (dst -> src) in REVERSE order. Best-effort and idempotent:
    a dst that is already back at src is skipped; a dst that is MISSING (rclone
    moved/deleted it after a partial upload) is logged loudly and skipped —
    rollback never blocks. Returns (restored_count, partial_bool)."""
    log = log or (lambda *a: None)
    restored, partial = 0, False
    for a_src, a_dst in reversed(list(renames)):
        if os.path.exists(a_src):
            continue
        if not os.path.exists(a_dst):
            log("ROLLBACK partial: %s missing (already moved by rclone?)"
                % a_dst)
            partial = True
            continue
        try:
            parent = os.path.dirname(a_src)
            if parent:
                os.makedirs(parent, exist_ok=True)
            os.rename(a_dst, a_src)
            restored += 1
        except OSError as e:
            log("ROLLBACK error %s -> %s: %s" % (a_dst, a_src, e))
            partial = True
    # Best-effort: drop now-empty wrap dirs we created (e.g. a loose-file wrap).
    for _a_src, a_dst in renames:
        parent = os.path.dirname(a_dst)
        try:
            if os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            pass
    return restored, partial


def journal_default_path(support_dir=None):
    return os.path.join(support_dir or _default_support_dir(),
                        "rename_journal.json")


def write_journal(path, gid, base_dir, renames):
    """Persist the planned renames BEFORE applying them (atomic temp+replace),
    so a crash between apply and a verified upload is recoverable on startup."""
    data = {"version": 1, "gid": gid, "base_dir": base_dir,
            "renames": [[s, d] for s, d in renames], "ts": int(time.time())}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def read_journal(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clear_journal(path):
    try:
        os.remove(path)
    except OSError:
        pass


def replay_journal(path, log=None):
    """Crash recovery. If a leftover journal exists, roll its renames back
    (dst -> src) to restore the original names, then clear it. Returns True iff
    a journal was found. Call this on startup BEFORE the first poll_once."""
    log = log or (lambda *a: None)
    data = read_journal(path)
    if not data:
        return False
    renames = [(s, d) for s, d in data.get("renames", [])]
    log("RENAME journal recovery: replaying %d op(s) in reverse for gid=%s"
        % (len(renames), data.get("gid")))
    rollback_rename(renames, log=log)
    clear_journal(path)
    return True


# ===========================================================================
# Phase 5 — `heal` backfill CLI  (I/O lives here only)
# ===========================================================================

BASE_REMOTE = "gdrive1:"


def _run(cmd):
    """Run a command (argument list, no shell). Returns (rc, stdout, stderr)."""
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, proc.stdout, proc.stderr


def _resolve_drive(name):
    """Resolve a shared-drive NAME to its team-drive id via rclone. Returns id
    or None."""
    rc, out, err = _run(["rclone", "backend", "drives", BASE_REMOTE])
    if rc != 0:
        sys.stderr.write("rclone backend drives failed: %s\n" % err.strip())
        return None
    try:
        drives = json.loads(out)
    except json.JSONDecodeError as e:
        sys.stderr.write("could not parse rclone drives JSON: %s\n" % e)
        return None
    for d in drives:
        if d.get("name") == name:
            return d.get("id")
    return None


def _list_tree(drive_id):
    """List a team drive recursively. Returns (files, dirs) as relpath lists."""
    rc, out, err = _run(["rclone", "lsjson", "-R",
                         "--drive-team-drive", drive_id, BASE_REMOTE])
    if rc != 0:
        sys.stderr.write("rclone lsjson failed: %s\n" % err.strip())
        return None, None
    try:
        entries = json.loads(out)
    except json.JSONDecodeError as e:
        sys.stderr.write("could not parse rclone lsjson JSON: %s\n" % e)
        return None, None
    files = [e["Path"] for e in entries if not e.get("IsDir")]
    dirs = [e["Path"] for e in entries if e.get("IsDir")]
    return files, dirs


def _filter_show(files, show_slug):
    """Keep only files whose top-level folder's show-slug contains show_slug."""
    if not show_slug:
        return files
    want = _slug(show_slug)
    kept = []
    for p in files:
        top = p.split("/")[0]
        fs = _folder_season(top)
        base_region = top[:fs[1]] if fs else top
        top_slug = _slug(_clean_show(base_region)[0])
        if want == top_slug or want in top_slug:
            kept.append(p)
    return kept


def _moveto_remote(drive_id, path):
    """Build a connection-string remote spec for one path on the team drive."""
    return "gdrive1,team_drive=%s:%s" % (drive_id, path)


def cmd_heal(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="renamer.py heal",
                                 description="Server-side TV cleanup (dry-run "
                                             "by default).")
    ap.add_argument("--drive", required=True, help="Shared-drive NAME (e.g. DriveB)")
    ap.add_argument("--show", default=None, help="Filter to a show slug")
    ap.add_argument("--show-name", default=None,
                    help="Override the emitted show display name (e.g. trim a "
                         "vanity prefix: --show-name Northbound)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually perform the renames (default: dry-run)")
    args = ap.parse_args(argv)

    drive_id = _resolve_drive(args.drive)
    if not drive_id:
        sys.stderr.write("drive %r not found on %s\n" % (args.drive, BASE_REMOTE))
        return 2
    print("Drive %s -> %s" % (args.drive, drive_id))

    files, _dirs = _list_tree(drive_id)
    if files is None:
        return 2
    files = _filter_show(files, args.show)
    if not files:
        print("No matching files.")
        return 0

    result = plan(files, show_override=args.show_name)
    if result.rejected:
        print("PLAN REJECTED: %s" % result.reason)
        print("(no changes; the payload would upload as-is)")
        return 1

    if not result.ops and not result.deletes:
        print("Nothing to do — already canonical.")
        return 0

    # --- print the diff (files first, then folders deepest-first) ----------
    file_ops = list(result.file_ops)
    dir_ops = sorted(result.dir_ops,
                     key=lambda o: (o[0].count("/"), len(o[0])), reverse=True)

    print("\n%d file rename(s):" % len(file_ops))
    for src, dst in file_ops:
        print("  f  %s\n     -> %s" % (src, dst))
    print("\n%d folder rename(s):" % len(dir_ops))
    for src, dst in dir_ops:
        print("  D  %s\n     -> %s" % (src, dst))
    if result.deletes:
        print("\n%d junk/empty path(s) to remove:" % len(result.deletes))
        for d in result.deletes:
            print("  x  %s" % d)

    if not args.apply:
        print("\nDRY-RUN — nothing changed. Re-run with --apply to execute.")
        return 0

    # --- apply: files first, then folders deepest-first --------------------
    print("\nAPPLYING (files, then folders deepest-first)...")
    failures = 0
    for src, dst in file_ops:
        rc, _out, err = _run(["rclone", "moveto",
                              _moveto_remote(drive_id, src),
                              _moveto_remote(drive_id, dst)])
        status = "ok" if rc == 0 else ("FAIL(%d)" % rc)
        print("  f  %s -> %s  [%s]" % (src, dst, status))
        if rc != 0:
            failures += 1
            sys.stderr.write("    %s\n" % err.strip())
    for src, dst in dir_ops:
        rc, _out, err = _run(["rclone", "moveto",
                              _moveto_remote(drive_id, src),
                              _moveto_remote(drive_id, dst)])
        status = "ok" if rc == 0 else ("FAIL(%d)" % rc)
        print("  D  %s -> %s  [%s]" % (src, dst, status))
        if rc != 0:
            failures += 1
            sys.stderr.write("    %s\n" % err.strip())

    if failures:
        print("\nCompleted with %d failure(s) — see above." % failures)
        return 1
    print("\nDone. Trigger drivecast's per-drive scan_cache to rebuild tiles.")
    return 0


def cmd_plan(argv):
    """Dev loop: `renamer.py plan '<name or relpath>' [more...]`."""
    if not argv:
        sys.stderr.write("usage: renamer.py plan <name-or-relpath> [...]\n")
        return 2
    ident = identify(argv)
    print("identify: is_tv=%s show=%r slug=%s season=%s layout=%s conf=%.1f"
          % (ident.is_tv, ident.show, ident.show_slug, ident.season,
             ident.layout_hint, ident.confidence))
    result = plan(argv)
    if result.rejected:
        print("PLAN REJECTED: %s" % result.reason)
        return 1
    for src, dst in result.file_ops:
        print("  f  %s -> %s" % (src, dst))
    for src, dst in result.dir_ops:
        print("  D  %s -> %s" % (src, dst))
    for d in result.deletes:
        print("  x  %s" % d)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: renamer.py {plan|heal} ...\n")
        return 2
    sub, rest = argv[0], argv[1:]
    if sub == "heal":
        return cmd_heal(rest)
    if sub == "plan":
        return cmd_plan(rest)
    sys.stderr.write("unknown subcommand %r (use plan|heal)\n" % sub)
    return 2


if __name__ == "__main__":
    sys.exit(main())
