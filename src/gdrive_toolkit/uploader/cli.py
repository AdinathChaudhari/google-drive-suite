"""`gdrive-upload` console-script entry point."""
from __future__ import annotations

from ..common import _deps


def main() -> None:
    _deps.require("upload", "flask", "requests")
    from ..common.entry import run
    from .server import create_app
    run(create_app, "drive-upload")


if __name__ == "__main__":
    main()
