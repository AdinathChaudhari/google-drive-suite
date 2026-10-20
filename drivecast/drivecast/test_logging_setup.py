import logging

from drivecast import logging_setup


def test_log_file_is_utf8_regardless_of_locale(tmp_path, monkeypatch):
    # The .app's interpreter runs with an ASCII locale encoding; the stream
    # line contains "…", so the handler must not depend on the locale.
    monkeypatch.setattr(logging_setup, "LOG_PATH", str(tmp_path / "drivecast.log"))
    logger = logging.getLogger("drivecast")
    handler = logging_setup.setup_logging(to_file=True)
    try:
        assert handler is not None
        assert handler.encoding == "utf-8"
        logging.getLogger("drivecast.streaming").info("stream abc123… end=complete")
        handler.flush()
        text = (tmp_path / "drivecast.log").read_text(encoding="utf-8")
        assert "stream abc123… end=complete" in text
    finally:
        logger.removeHandler(handler)
        handler.close()
