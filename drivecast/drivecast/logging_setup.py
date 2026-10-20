"""File logging for drivecast's INFO lines (stream stats, media-info probes).

The menu-bar .app runs uvicorn at log_level="warning" and has no console, so
without a handler INFO from the drivecast.* loggers is dropped. This attaches a
rotating file at data/drivecast.log.

The file is opened as UTF-8 explicitly: the py2app stub starts Python without
UTF-8 mode, so under Finder/`open` the locale encoding is ASCII and any line
with a non-ASCII character (the stream line's "…") would fail to encode and be
dropped silently (the error goes to stderr, which the .app sends to /dev/null).
"""
import logging
import logging.handlers
import os

from . import config as config_mod

LOG_PATH = os.path.join(config_mod.DATA_DIR, "drivecast.log")
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(to_file=True):
    """Route drivecast.* INFO+ to data/drivecast.log. Idempotent."""
    logger = logging.getLogger("drivecast")
    logger.setLevel(logging.INFO)
    if not to_file:
        return None
    for h in logger.handlers:
        if getattr(h, "_drivecast_file", False):
            return h
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return None
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(FORMAT))
    handler._drivecast_file = True
    logger.addHandler(handler)
    return handler
