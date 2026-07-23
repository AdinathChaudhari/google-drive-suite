"""Config for the downloader: tool DEFAULTS layered under the shared config
(see gdrive_toolkit.common.config for the precedence rules and on-disk layout)."""
from __future__ import annotations

import os
from pathlib import Path

from ..common.config import load_config as _load_config
from ..common.config import save_config as _save_config

TOOL_NAME = "downloader"

DEFAULTS = {
    "remote": "",                     # set by `gdrive-setup` (shared config)
    "dest_root": str(Path.home() / "Downloads"),
    "port": 8747,                     # Flask UI port
    "rc_addr": "127.0.0.1:5572",      # rclone rc daemon address
    "transfers": 8,                   # parallel files per job
    "multi_thread_streams": 4,        # parallel byte-streams per big file
    "multi_thread_cutoff": "256M",    # only files bigger than this get split
    "skip_gdocs": False,              # skip native Google Docs/Sheets/Slides
    "acknowledge_abuse": True,        # download files Google flags as "abusive"
    "export_formats": "docx,xlsx,pptx,svg",  # used only when skip_gdocs is False
}


def load_config() -> dict:
    cfg = _load_config(TOOL_NAME, DEFAULTS)
    cfg["dest_root"] = str(Path(os.path.expanduser(cfg["dest_root"])))  # ~ expansion
    return cfg


def save_config(cfg: dict) -> None:
    _save_config(TOOL_NAME, cfg)
