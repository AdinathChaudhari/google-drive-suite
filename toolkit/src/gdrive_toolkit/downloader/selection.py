"""Pure selection -> rclone filter-rule logic. No I/O, fully unit-testable.

The UI is a sparse "override tree": the frontend sends only the nodes the user
EXPLICITLY toggled, each with a state:

    {"drive_id","path","is_dir","state": "on" | "off"}

A node with no explicit override inherits from its nearest explicit ancestor
(default: off). This lets you check a whole folder ("on") and then uncheck a few
files inside it ("off") — true file-level selection.

Each drive becomes one ordered rclone FilterRule list. rclone evaluates filters
top-to-bottom, first match wins, so more-specific (deeper) rules must come first:

    - /Course/notes.txt      (an unchecked file inside a checked folder)
    + /Course/**             (the checked folder)
    - **                     (exclude everything else — always last)

rclone auto-traverses ancestor directories to reach an included file, so a
"+ /A/keep.mp3" works even without a rule for A.
"""
from __future__ import annotations

from typing import Iterable

_SPECIAL = set("*?[]{}\\")


def escape_literal(path: str) -> str:
    """Escape rclone-glob metacharacters in a literal path string."""
    return "".join("\\" + c if c in _SPECIAL else c for c in path)


def _norm(path: str) -> str:
    return path.strip("/")


def _depth(path: str) -> int:
    return 0 if path == "" else path.count("/") + 1


def build_drive_rules(overrides: Iterable[dict]) -> list[str]:
    """overrides: [{'path','is_dir','state'}] for ONE drive -> FilterRule list."""
    ovs = [
        {"path": _norm(o["path"]), "is_dir": bool(o["is_dir"]), "state": o["state"]}
        for o in overrides
    ]
    # Deepest (most specific) first so child rules win over ancestor rules.
    ovs.sort(key=lambda o: (-_depth(o["path"]), o["path"]))

    rules: list[str] = []
    for o in ovs:
        p, esc, sign = o["path"], escape_literal(o["path"]), ("+" if o["state"] == "on" else "-")
        if p == "":
            rules.append("%s /**" % sign)
        elif o["is_dir"]:
            rules.append("%s /%s/**" % (sign, esc))
        else:
            rules.append("%s /%s" % (sign, esc))
    rules.append("- **")
    return rules


def build_jobs(overrides: Iterable[dict]) -> dict[str, list[str]]:
    """overrides across any drives -> {drive_id: FilterRule list}.

    Drives with no include ('+') rule are skipped (nothing to download).
    """
    by_drive: dict[str, list[dict]] = {}
    for o in overrides:
        by_drive.setdefault(o["drive_id"], []).append(o)
    out: dict[str, list[str]] = {}
    for drive_id, ovs in by_drive.items():
        rules = build_drive_rules(ovs)
        if any(r.startswith("+ ") for r in rules):
            out[drive_id] = rules
    return out
