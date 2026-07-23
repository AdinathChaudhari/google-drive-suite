"""Config for the uploader: tool DEFAULTS layered under the shared config
(see gdrive_toolkit.common.config for the precedence rules and on-disk layout)."""
from __future__ import annotations

from ..common.config import load_config as _load_config
from ..common.config import save_config as _save_config

TOOL_NAME = "uploader"

DEFAULTS = {
    "remote": "",                      # set by `gdrive-setup` (shared config)
    "port": 8748,                      # Flask UI port
    "rc_addr": "127.0.0.1:5573",       # rclone rc daemon address (own daemon —
                                        # NOT the downloader's 5572, so quitting
                                        # the downloader never kills an upload)
    "transfers": 8,                    # parallel files per job
    "drive_chunk_size": "64M",         # upload chunk size (connection-string param)
    "retries": 3,
    "low_level_retries": 10,
    "default_drive_id": "",            # remembered last destination drive
    "default_dest_path": "",           # remembered last destination path
}


def load_config() -> dict:
    return _load_config(TOOL_NAME, DEFAULTS)


def save_config(cfg: dict) -> None:
    _save_config(TOOL_NAME, cfg)
