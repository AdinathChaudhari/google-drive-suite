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

User tools: `~/Library/Application Support/gdrive_toolkit/hub_tools.json` —
a JSON array of tool dicts using the same schema as the built-ins (see
hub_core.py's docstrings for the field meanings: kind, dir, installed_if /
installed_if_any, launch.argv + port, or launchd_label + process_pattern).
Missing file -> built-ins only, no error.
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


def build_tools() -> list[dict]:
    repo_root = _repo_root()
    tools = [
        _web_tool("drive-download", "Drive Download", "gdrive-download",
                  "downloader", _DOWNLOAD_PORT_DEFAULT, repo_root),
        _web_tool("drive-upload", "Drive Upload", "gdrive-upload",
                  "uploader", _UPLOAD_PORT_DEFAULT, repo_root),
    ]
    tools.extend(_load_user_tools())
    return tools


TOOLS = build_tools()
