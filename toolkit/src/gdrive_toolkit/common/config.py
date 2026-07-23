"""Config plumbing shared by every tool in the toolkit.

Layout on disk, all outside the repo (survives reinstalls/rebuilds):

    ~/Library/Application Support/gdrive_toolkit/
        config.json        # shared: {"remote": "...", "repo_root": "..."}
        downloader.json     # downloader-only knobs (ports, transfers, ...)
        uploader.json       # uploader-only knobs
        hub_tools.json       # user-added hub registry entries (see hub/registry.py)
        logs/

Precedence for a tool's effective config: tool DEFAULTS ← shared config ←
per-tool file. i.e. the per-tool file wins, then the shared config, and the
tool's own DEFAULTS dict is the fallback for everything else.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

APP_ID = "gdrive_toolkit"

CONFIG_DIR = Path.home() / "Library" / "Application Support" / APP_ID
SHARED_CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_DIR = CONFIG_DIR / "logs"


def _read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        return {}  # fall back to defaults on a corrupt/unreadable file


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)  # atomic
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def tool_config_path(tool_name: str) -> Path:
    return CONFIG_DIR / ("%s.json" % tool_name)


def load_shared_config() -> dict:
    return _read_json(SHARED_CONFIG_PATH)


def save_shared_config(data: dict) -> None:
    _write_json_atomic(SHARED_CONFIG_PATH, data)


def load_config(tool_name: str, defaults: dict) -> dict:
    """tool DEFAULTS ← shared config ← per-tool file."""
    cfg = dict(defaults)
    cfg.update(load_shared_config())
    cfg.update(_read_json(tool_config_path(tool_name)))
    return cfg


def save_config(tool_name: str, cfg: dict) -> None:
    _write_json_atomic(tool_config_path(tool_name), cfg)
