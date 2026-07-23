"""registry.py — LOADER, not data.

The old prototype's registry.py was a hardcoded TOOLS list (this toolkit's
own tools plus a couple of sibling repos, with absolute paths baked in). This
version computes the built-in downloader/uploader entries from the resolved
repo root — so it works whether you're running from an editable dev checkout,
an installed wheel, or a frozen py2app .app — and then layers in whatever the
user has registered for OTHER tools via a JSON file. hub_core.py is
completely unaware of this split: every dict this module hands it has the
exact same shape it always consumed.

Repo-root resolution chain (first hit wins):
  1. `GDRIVE_TOOLKIT_ROOT` env var
  2. `repo_root` in the shared config (written by `gdrive-setup`)
  3. dev fallback: walk up from this file's location

(3) is a fallback, not the primary source — it breaks once this package is
frozen into a .app bundle by py2app, where `__file__` lives inside
`Contents/Resources/lib/pythonX.Y/gdrive_toolkit/hub/registry.py` and there's
no meaningful "repo" above it.

Suite-root resolution (separate from repo-root above) is the same idea one
level up: when this toolkit lives inside a `google-drive-suite` checkout
(this repo's `toolkit/` subdir has sibling product dirs `drivecast/` and
`drive-offload/` next to it), the hub also shows those siblings as BUILT-IN
tools — no `hub_tools.json` entry required. Chain (first hit wins):
  1. `GDRIVE_SUITE_ROOT` env var
  2. `suite_root` in the shared config (written by `gdrive-setup` when it
     detects the suite layout)
  3. `repo_root.parent`, but ONLY if `(parent / "drivecast" / "app.py")`
     exists — this guards against false-positives when the toolkit is
     installed standalone (e.g. from PyPI) with an unrelated parent dir.
Resolves to `None` when none of these hit — suite built-ins are then simply
skipped, so the standalone-toolkit story (no drivecast/drive-offload
anywhere) is unaffected.

User tools: `~/Library/Application Support/gdrive_toolkit/hub_tools.json` —
a JSON array of tool dicts using the same schema as the built-ins (see
hub_core.py's docstrings for the field meanings: kind, dir, installed_if /
installed_if_any, launch.argv + port, or launchd_label + process_pattern).
Missing file -> built-ins only, no error. A user entry whose "id" matches a
built-in (toolkit's own, or a suite sibling's) REPLACES that built-in rather
than duplicating it — see the dedup in `build_tools()`.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from ..common.config import CONFIG_DIR, load_shared_config, tool_config_path

_DOWNLOAD_PORT_DEFAULT = 8747
_UPLOAD_PORT_DEFAULT = 8748
_DRIVECAST_PORT_DEFAULT = 8737

HUB_TOOLS_PATH = CONFIG_DIR / "hub_tools.json"


def _repo_root() -> Path:
    env = os.environ.get("GDRIVE_TOOLKIT_ROOT")
    if env:
        return Path(env).expanduser().resolve()

    shared_root = load_shared_config().get("repo_root")
    if shared_root:
        return Path(shared_root).expanduser().resolve()

    # Dev fallback: .../<repo_root>/src/gdrive_toolkit/hub/registry.py
    return Path(__file__).resolve().parents[3]


def _suite_root(repo_root: Path) -> Path | None:
    """Resolve the google-drive-suite root this toolkit checkout lives in,
    or None when it isn't part of a suite checkout (standalone install)."""
    env = os.environ.get("GDRIVE_SUITE_ROOT")
    if env:
        return Path(env).expanduser().resolve()

    shared_suite_root = load_shared_config().get("suite_root")
    if shared_suite_root:
        return Path(shared_suite_root).expanduser().resolve()

    candidate = repo_root.parent
    if (candidate / "drivecast" / "app.py").exists():
        return candidate

    return None


def _resolve_script(name: str, repo_root: Path) -> list[str]:
    """Find the console script for `name`, preferring the venv this process
    is already running in, then PATH, then the configured repo's venv."""
    sibling = Path(sys.executable).parent / name
    if sibling.exists():
        return [str(sibling)]

    found = shutil.which(name)
    if found:
        return [found]

    in_repo_venv = repo_root / "venv" / "bin" / name
    if in_repo_venv.exists():
        return [str(in_repo_venv)]

    # Nothing resolved — hand back the bare name; hub_core will report this
    # tool NOT_INSTALLED (installed_if_any won't find it either) rather than
    # silently pretending it's runnable.
    return [name]


def _web_tool(tool_id: str, label: str, script_name: str, config_name: str,
              port_default: int, repo_root: Path) -> dict:
    argv = _resolve_script(script_name, repo_root)
    return {
        "id": tool_id,
        "label": label,
        "kind": "web",
        "dir": str(repo_root),
        "installed_if_any": [argv[0]],
        "launch": {"argv": argv},
        "port": {
            "config": str(tool_config_path(config_name)),
            "key": "port",
            "default": port_default,
        },
    }


def _load_user_tools() -> list[dict]:
    try:
        with open(HUB_TOOLS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict):
        data = data.get("tools", [])
    return data if isinstance(data, list) else []


def _drivecast_tool(suite_root: Path) -> dict:
    tool_dir = suite_root / "drivecast"
    return {
        "id": "drivecast",
        "label": "Drivecast",
        "kind": "web",
        "dir": str(tool_dir),
        "installed_if": ["app.py", "venv/bin/python"],
        "launch": {"argv": [str(tool_dir / "venv" / "bin" / "python"), "app.py"]},
        "port": {
            "config": str(Path("~/Library/Application Support/drivecast/config.json").expanduser()),
            "key": "port",
            "default": _DRIVECAST_PORT_DEFAULT,
        },
    }


def _drive_offload_tool(suite_root: Path) -> dict:
    return {
        "id": "drive-offload",
        "label": "Drive Offload",
        "kind": "menubar",
        "dir": str(suite_root / "drive-offload"),
        "installed_if_any": [
            "~/Library/LaunchAgents/com.driveoffload.app.plist",
            "/Applications/drive-offload.app",
        ],
        "launchd_label": "com.driveoffload.app",
        "process_pattern": r"drive-offload\.app/Contents/MacOS|offload_app\.py",
    }


def _drivecast_app_tool() -> dict:
    return {
        "id": "drivecast-app",
        "label": "Drivecast (Fire TV)",
        "kind": "external",
        "note": (
            "Android TV / Fire TV client, not a process this Mac can launch "
            "or check the status of. Listed here as a reminder of what "
            "exists in the ecosystem; the hub renders an 'external' kind as "
            "an inert, non-clickable row rather than erroring on the "
            "missing launch/port fields other kinds require."
        ),
    }


def _suite_tools(repo_root: Path) -> list[dict]:
    """Computed built-ins for the OTHER suite members (drivecast,
    drive-offload, drivecast-app) when this toolkit checkout is part of a
    google-drive-suite monorepo. Empty list when it isn't (standalone
    install) — hub_core never sees a difference either way."""
    suite_root = _suite_root(repo_root)
    if suite_root is None:
        return []
    return [
        _drivecast_tool(suite_root),
        _drive_offload_tool(suite_root),
        _drivecast_app_tool(),
    ]


def build_tools() -> list[dict]:
    repo_root = _repo_root()
    built_ins = [
        _web_tool("drive-download", "Drive Download", "gdrive-download",
                  "downloader", _DOWNLOAD_PORT_DEFAULT, repo_root),
        _web_tool("drive-upload", "Drive Upload", "gdrive-upload",
                  "uploader", _UPLOAD_PORT_DEFAULT, repo_root),
    ]
    built_ins.extend(_suite_tools(repo_root))

    # id -> tool, insertion-ordered. User hub_tools.json entries are layered
    # in last, and any user entry whose "id" matches a built-in (toolkit's
    # own, or a suite sibling's) REPLACES that built-in in place rather than
    # appending a duplicate row — a user who wants to override how a tool
    # launches doesn't end up with two rows for it. A user entry with no
    # "id" at all (malformed, or an old free-form entry) can't be deduped
    # and is just appended, same as before.
    by_id: dict[str, dict] = {}
    no_id: list[dict] = []

    for tool in built_ins:
        by_id[tool["id"]] = tool

    for user_tool in _load_user_tools():
        tool_id = user_tool.get("id")
        if tool_id is None:
            no_id.append(user_tool)
        else:
            by_id[tool_id] = user_tool

    return list(by_id.values()) + no_id


TOOLS = build_tools()
