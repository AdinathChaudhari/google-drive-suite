"""Tests for config: user-dir relocation, secret resolution, saved-key policy.

All synthetic — paths are monkeypatched into a tmp dir so the real
~/Library/Application Support/drivecast is never touched.
"""
import json
import os

from drivecast import config


def _redirect(monkeypatch, tmp_path):
    """Point every config path at a throwaway USER_DIR under tmp_path."""
    user_dir = tmp_path / "drivecast"
    data_dir = user_dir / "data"
    secrets_dir = user_dir / "secrets"
    monkeypatch.setattr(config, "USER_DIR", str(user_dir))
    monkeypatch.setattr(config, "CONFIG_PATH", str(user_dir / "config.json"))
    monkeypatch.setattr(config, "SECRETS_PATH", str(secrets_dir / "secrets.json"))
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(config, "POSTERS_DIR", str(data_dir / "posters"))
    monkeypatch.delenv("DRIVECAST_TMDB_API_KEY", raising=False)
    return user_dir, data_dir, secrets_dir


def test_tmdb_api_key_is_secret_not_saved():
    assert "tmdb_api_key" in config.SECRET_KEYS
    assert "tmdb_api_key" not in config.SAVED_KEYS


def test_secret_resolves_from_user_dir(tmp_path, monkeypatch):
    user_dir, _, secrets_dir = _redirect(monkeypatch, tmp_path)
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "secrets.json").write_text(json.dumps({"tmdb_api_key": "SEKRET"}))
    user_dir.mkdir(exist_ok=True)
    (user_dir / "config.json").write_text(json.dumps({"selected_drives": ["d1"]}))

    cfg = config.load_config()
    assert cfg["tmdb_api_key"] == "SEKRET"        # from USER_DIR/secrets
    assert cfg["selected_drives"] == ["d1"]       # from USER_DIR/config.json


def test_env_var_overrides_secrets_file(tmp_path, monkeypatch):
    user_dir, _, secrets_dir = _redirect(monkeypatch, tmp_path)
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "secrets.json").write_text(json.dumps({"tmdb_api_key": "FROMFILE"}))
    monkeypatch.setenv("DRIVECAST_TMDB_API_KEY", "FROMENV")

    cfg = config.load_config()
    assert cfg["tmdb_api_key"] == "FROMENV"


def test_load_creates_dirs_and_config_from_example(tmp_path, monkeypatch):
    user_dir, data_dir, _ = _redirect(monkeypatch, tmp_path)
    # No config.json yet -> first run copies from the repo example.
    cfg = config.load_config()
    assert os.path.isdir(str(data_dir))
    assert os.path.isdir(str(data_dir / "posters"))
    assert os.path.exists(str(user_dir / "config.json"))
    assert "tmdb_api_key" in cfg  # resolved (empty) even with no secrets file


def test_save_config_never_writes_secret(tmp_path, monkeypatch):
    user_dir, _, _ = _redirect(monkeypatch, tmp_path)
    user_dir.mkdir(parents=True)
    config.save_config({"selected_drives": ["x"], "tmdb_api_key": "SHOULD_NOT_PERSIST",
                        "port": 9000})
    on_disk = json.loads((user_dir / "config.json").read_text())
    assert on_disk["selected_drives"] == ["x"]
    assert on_disk["port"] == 9000
    assert "tmdb_api_key" not in on_disk


# ------------------------------------------------------------------- tabs -----
# "tabs" is the source of truth for the nav bar (see sections.py); config.py
# only owns its default/persistence and the one-time upgrade seam that seeds
# it from a pre-tabs install's drive_sections.

def test_tabs_in_defaults_and_saved_keys():
    assert config.DEFAULTS["tabs"] == []
    assert "tabs" in config.SAVED_KEYS


def test_tabs_roundtrips_through_save_and_load(tmp_path, monkeypatch):
    user_dir, _, _ = _redirect(monkeypatch, tmp_path)
    user_dir.mkdir(parents=True)
    tabs = [{"key": "entertainment", "label": "Entertainment", "icon": "🍿",
             "behavior": "entertainment"}]
    config.save_config({"tabs": tabs, "selected_drives": []})
    cfg = config.load_config()
    assert cfg["tabs"] == tabs


def test_fresh_install_has_no_tabs_key_and_stays_empty(tmp_path, monkeypatch):
    user_dir, _, _ = _redirect(monkeypatch, tmp_path)
    # No config.json yet; config.example.json ships no "tabs" key and no
    # selected_drives -> true zero-start, nothing seeded.
    cfg = config.load_config()
    assert cfg["tabs"] == []
    on_disk = json.loads((user_dir / "config.json").read_text())
    assert "tabs" not in on_disk  # migration was a no-op, nothing written


# -------------------------------------------------------------- migrate_config --

def test_migrate_config_noop_when_tabs_key_already_present():
    cfg = {"tabs": [{"key": "x"}], "selected_drives": ["d1"],
           "drive_sections": {"d1": "courses"}}
    out, changed = config.migrate_config(dict(cfg), had_tabs_key=True)
    assert changed is False
    assert out["tabs"] == cfg["tabs"]  # untouched


def test_migrate_config_noop_on_fresh_install():
    cfg = {"tabs": [], "selected_drives": [], "drive_sections": {}}
    out, changed = config.migrate_config(dict(cfg), had_tabs_key=False)
    assert changed is False
    assert out["tabs"] == []


def test_migrate_config_seeds_entertainment_courses_podcasts_and_plugin(monkeypatch):
    from drivecast import sections
    monkeypatch.setattr(sections, "_plugins", {
        "myaudio": {"key": "myaudio", "label": "My Audio", "icon": "♪",
                 "accent": "#3b82f6", "accent2": "#93c5fd",
                 "classify": lambda *a, **k: []},
    })

    cfg = {
        "selected_drives": ["d1", "d2", "d3", "d4", "d5"],
        "drive_sections": {
            "d1": "entertainment",
            "d2": "courses",
            "d3": "podcasts",
            "d4": "myaudio",
            "d5": "not-a-real-section",
            # d-unselected below must never surface in seeded tabs/drive_sections
            "d-unselected": "courses",
        },
    }
    out, changed = config.migrate_config(dict(cfg), had_tabs_key=False)

    assert changed is True
    seeded = {t["key"]: t for t in out["tabs"]}
    assert set(seeded) == {"entertainment", "courses", "podcasts", "myaudio"}

    assert seeded["entertainment"] == {
        "key": "entertainment", "label": "Entertainment", "icon": "🍿",
        "behavior": "entertainment",
    }
    assert seeded["courses"] == {
        "key": "courses", "label": "Courses", "icon": "🎓", "behavior": "courses",
        "accent": "#4ade80", "accent2": "#86efac",
    }
    assert seeded["podcasts"] == {
        "key": "podcasts", "label": "Podcasts", "icon": "🎙", "behavior": "podcasts",
        "accent": "#c084fc", "accent2": "#e0b3ff",
    }
    assert seeded["myaudio"] == {
        "key": "myaudio", "label": "My Audio", "icon": "♪", "behavior": "myaudio",
        "accent": "#3b82f6", "accent2": "#93c5fd",
    }

    # d5's value resolved to nothing we know how to render -> assignment
    # dropped, then rule 3 gives it (and any bare drive) the default tab.
    assert out["drive_sections"]["d1"] == "entertainment"
    assert out["drive_sections"]["d2"] == "courses"
    assert out["drive_sections"]["d3"] == "podcasts"
    assert out["drive_sections"]["d4"] == "myaudio"
    assert out["drive_sections"]["d5"] == "entertainment"
    # d-unselected isn't in selected_drives, so rule 3 never touches it; its
    # value happens to resolve ("courses" was seeded) so cleanup leaves it be.
    assert out["drive_sections"]["d-unselected"] == "courses"


def test_migrate_config_gives_selected_drive_without_entry_entertainment():
    cfg = {"selected_drives": ["d1"], "drive_sections": {}}
    out, changed = config.migrate_config(dict(cfg), had_tabs_key=False)
    assert changed is True
    assert out["drive_sections"]["d1"] == "entertainment"
    assert out["tabs"] == [{"key": "entertainment", "label": "Entertainment",
                             "icon": "🍿", "behavior": "entertainment"}]


def test_migrate_config_is_idempotent_via_load_config_roundtrip(tmp_path, monkeypatch):
    user_dir, _, _ = _redirect(monkeypatch, tmp_path)
    user_dir.mkdir(parents=True)
    (user_dir / "config.json").write_text(json.dumps({
        "selected_drives": ["d1", "d2"],
        "drive_sections": {"d1": "entertainment", "d2": "courses"},
    }))

    first = config.load_config()
    assert {t["key"] for t in first["tabs"]} == {"entertainment", "courses"}
    on_disk_after_first = json.loads((user_dir / "config.json").read_text())
    assert "tabs" in on_disk_after_first  # migration persisted itself

    second = config.load_config()
    assert second["tabs"] == first["tabs"]
    assert second["drive_sections"] == first["drive_sections"]
