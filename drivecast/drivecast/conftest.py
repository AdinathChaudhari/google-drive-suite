"""Shared test fixtures.

Tests must be hermetic. Two developer-machine leaks are sealed here for every
test:

1. Custom section plugins installed in the developer's user directory must
   never leak into test runs, so the plugin dir is pointed at an empty temp
   path (tests that exercise plugin loading override this themselves). The tabs
   cache is a lazy module global too (it mirrors the plugins() pattern) and one
   test's ``set_tabs()`` — including the one the /api/settings POST handler
   calls internally — would otherwise leak into the next test in the process.

2. ``sections.tabs()`` lazily calls ``config.load_config()``, so an un-isolated
   config path would let tests READ the developer's real tabs/drive_sections
   (making assertions depend on live config) and, worse, ``migrate_config``'s
   save-on-migrate could WRITE a ``tabs`` key into the real config.json. Every
   config path is redirected at a temp dir so no test can touch real user data.
   Tests that drive config directly (test_config.py) override these themselves.
"""
import pytest

from drivecast import config, sections


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    # (1) no real section plugins / stale caches.
    monkeypatch.setattr(sections, "PLUGIN_DIR", str(tmp_path / "_no_plugins"))
    monkeypatch.setattr(sections, "_plugins", None)
    monkeypatch.setattr(sections, "_tabs", None)
    # (2) never read or write the developer's real config.
    cfg_dir = tmp_path / "_cfg"
    monkeypatch.setattr(config, "USER_DIR", str(cfg_dir))
    monkeypatch.setattr(config, "CONFIG_PATH", str(cfg_dir / "config.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(cfg_dir / "data"))
    monkeypatch.setattr(config, "POSTERS_DIR", str(cfg_dir / "data" / "posters"))
    monkeypatch.setattr(config, "SUBS_DIR", str(cfg_dir / "data" / "subs"))
    monkeypatch.setattr(config, "SECRETS_PATH",
                        str(cfg_dir / "secrets" / "secrets.json"))
    # EXAMPLE_PATH is left at the repo's static config.example.json (read-only,
    # ships no tabs / no selected_drives) so first-run copies it exactly as in
    # production; overriding it would make _ensure_config_file() fall back to
    # writing DEFAULTS (which carries tabs: []) and mask the true fresh-install
    # shape that the migration sentinel depends on.
    yield
