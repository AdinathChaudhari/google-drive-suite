"""Pure(ish) local-selection -> upload-job planning logic. Mirror of selection.py,
but inverted: instead of turning remote drive picks into filter rules for a
download, this turns LOCAL file/folder picks into rclone copy jobs for an upload.

Input is a flat list of already-picked, already-existing local paths:

    [{"path": "/Users/me/Movies/Show", "is_dir": True, "staged": False}, ...]

Rules:
  1. Dedup: if a selected dir is an ancestor of another selected path (dir or
     file), the descendant is redundant — the ancestor's job already covers it.
  2. Collision: two sources that would land under the SAME name at the
     destination (two dirs with the same basename, or two files with the same
     basename landing loose in dest_path) is an error — surface it, don't
     silently clobber one.
  3. Each surviving directory becomes its own job: `srcFs` = the dir itself,
     destination = `dest_path/<basename>` — the directory is preserved as a
     single unit (same convention as offloader.py / todrive), no filters needed.
  4. Surviving loose files are grouped by parent directory; each group becomes
     one job: `srcFs` = the parent dir, destination = `dest_path` (files land
     directly in it), with an rclone filter allow-listing just those basenames.

`escape_literal` is copied verbatim from selection.py (selection.py itself is
not part of drive-upload — its inverse lives here).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

_SPECIAL = set("*?[]{}\\")


def escape_literal(path: str) -> str:
    """Escape rclone-glob metacharacters in a literal path string."""
    return "".join("\\" + c if c in _SPECIAL else c for c in path)


class PlanError(Exception):
    """Raised for a planning conflict the UI should surface (e.g. HTTP 400)."""


@dataclass
class Job:
    src_fs: str                       # local path to use as rclone srcFs
    dst_path: str                     # destination folder inside the drive (relative, no leading '/')
    filter_rules: Optional[list[str]]  # None for a whole-dir job; FilterRule list for a file group
    label: str                        # human label for the progress UI
    size: int                         # bytes, best-effort (tolerant of unreadable entries)
    sources: list[dict] = field(default_factory=list)  # original source dicts this job covers


def _norm(path: str) -> str:
    """Normalize a local path (no symlink resolution — rclone handles those)."""
    return os.path.normpath(path)


def _is_strict_ancestor(anc: str, of: str) -> bool:
    if anc == of:
        return False
    anc_slash = anc.rstrip("/") + "/"
    return of.startswith(anc_slash)


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def dir_size(path: str) -> int:
    """Recursive size of a directory, tolerant of unreadable entries (permission
    errors, broken symlinks, races) — same convention as todrive:672-689.
    Exposed (not private) so server.py's /api/stat can reuse it.
    """
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def _dest_join(dest_path: str, name: str) -> str:
    dp = dest_path.strip("/")
    return "%s/%s" % (dp, name) if dp else name


def build_upload_jobs(sources: list[dict], dest_path: str) -> list[Job]:
    """sources: [{'path','is_dir', 'staged'(optional)}], local absolute paths
    that already exist. Returns the planned Job list, or raises PlanError.
    """
    norm = []
    for s in sources:
        norm.append({
            "path": _norm(s["path"]),
            "is_dir": bool(s.get("is_dir")),
            "staged": bool(s.get("staged", False)),
            "orig": s,
        })

    # de-dup exact-duplicate paths (keep first)
    seen: set[str] = set()
    unique = []
    for s in norm:
        if s["path"] in seen:
            continue
        seen.add(s["path"])
        unique.append(s)

    # 1. drop anything that is a descendant of another selected DIRECTORY.
    dir_paths = [s["path"] for s in unique if s["is_dir"]]
    survivors = []
    for s in unique:
        if any(_is_strict_ancestor(d, s["path"]) for d in dir_paths):
            continue  # covered by an ancestor dir's job
        survivors.append(s)

    dirs = [s for s in survivors if s["is_dir"]]
    files = [s for s in survivors if not s["is_dir"]]

    # 2. collision check: everything lands directly under dest_path by its
    #    basename (a dir's own name, or a loose file's own name) — two
    #    entries claiming the same name is ambiguous.
    landing: dict[str, str] = {}

    def _claim(name: str, path: str) -> None:
        if name in landing:
            raise PlanError(
                "Two sources would both land as %r under the destination: %s and %s"
                % (name, landing[name], path)
            )
        landing[name] = path

    for d in dirs:
        _claim(os.path.basename(d["path"]), d["path"])
    for f in files:
        _claim(os.path.basename(f["path"]), f["path"])

    jobs: list[Job] = []

    # 3. one job per surviving directory — preserved as a unit.
    for d in dirs:
        name = os.path.basename(d["path"])
        jobs.append(Job(
            src_fs=d["path"],
            dst_path=_dest_join(dest_path, name),
            filter_rules=None,
            label=name,
            size=dir_size(d["path"]),
            sources=[d["orig"]],
        ))

    # 4. loose files grouped by parent dir -> one filtered job per group.
    by_parent: dict[str, list[dict]] = {}
    for f in files:
        by_parent.setdefault(os.path.dirname(f["path"]), []).append(f)

    for parent, group in by_parent.items():
        names = [os.path.basename(f["path"]) for f in group]
        rules = ["+ /%s" % escape_literal(n) for n in names] + ["- **"]
        size = sum(_file_size(f["path"]) for f in group)
        label = names[0] if len(names) == 1 else "%d files from %s" % (len(names), parent)
        jobs.append(Job(
            src_fs=parent,
            dst_path=dest_path,
            filter_rules=rules,
            label=label,
            size=size,
            sources=[f["orig"] for f in group],
        ))

    return jobs
