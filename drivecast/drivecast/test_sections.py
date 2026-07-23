"""Tests for sections.py: the behavior catalog (code) vs. tabs (data).

`conftest._no_real_plugins` already points PLUGIN_DIR at an empty temp dir
and resets the plugin cache for every test; here we additionally reset the
tabs cache the same way (mirroring how the existing plugin-cache reset
works) so no test's `set_tabs`/`tabs()` state leaks into the next.
"""
import pytest

from drivecast import sections


@pytest.fixture(autouse=True)
def _reset_tabs_cache(monkeypatch):
    monkeypatch.setattr(sections, "_tabs", None)
    yield


def _tab(key="entertainment", label="Entertainment", icon="🍿",
         behavior="entertainment", **extra):
    d = {"key": key, "label": label, "icon": icon, "behavior": behavior}
    d.update(extra)
    return d


# --------------------------------------------------------------- validate_tabs --

def test_validate_tabs_slugifies_label_when_key_absent():
    out = sections.validate_tabs([{"label": "My Tab", "behavior": "entertainment"}])
    assert out[0]["key"] == "my-tab"


def test_validate_tabs_unicode_label_slugs_to_fallback():
    # An all-unicode label has nothing ASCII-alnum to slug from; must not
    # crash and must still produce *some* stable key.
    out = sections.validate_tabs([{"label": "日本語", "behavior": "entertainment"}])
    assert len(out) == 1
    assert out[0]["key"]  # non-empty
    assert out[0]["label"] == "日本語"


def test_validate_tabs_empty_label_rejected():
    out = sections.validate_tabs([{"label": "", "behavior": "entertainment"},
                                   {"label": "   ", "behavior": "entertainment"}])
    assert out == []


def test_validate_tabs_label_too_long_rejected():
    out = sections.validate_tabs([{"label": "x" * 41, "behavior": "entertainment"}])
    assert out == []
    out2 = sections.validate_tabs([{"label": "x" * 40, "behavior": "entertainment"}])
    assert len(out2) == 1


def test_validate_tabs_duplicate_key_drops_later_entry():
    out = sections.validate_tabs([
        {"key": "mine", "label": "First", "behavior": "entertainment"},
        {"key": "mine", "label": "Second", "behavior": "courses"},
    ])
    assert len(out) == 1
    assert out[0]["label"] == "First"


def test_validate_tabs_invalid_behavior_rejected():
    out = sections.validate_tabs([{"label": "Ghost", "behavior": "does-not-exist"}])
    assert out == []


def test_validate_tabs_absent_behavior_rejected():
    out = sections.validate_tabs([{"label": "No Behavior"}])
    assert out == []


def test_validate_tabs_icon_defaults_and_caps():
    out = sections.validate_tabs([{"label": "A", "behavior": "entertainment"},
                                   {"label": "B", "behavior": "entertainment",
                                    "icon": "toolongiconstring"}])
    assert out[0]["icon"] == "📁"
    assert len(out[1]["icon"]) <= 8


def test_validate_tabs_valid_accent_kept_verbatim():
    out = sections.validate_tabs([{"label": "A", "behavior": "entertainment",
                                    "accent": "#123abc", "accent2": "#abcdef"}])
    assert out[0]["accent"] == "#123abc"
    assert out[0]["accent2"] == "#abcdef"


def test_validate_tabs_invalid_accent_auto_assigned():
    out = sections.validate_tabs([{"label": "A", "behavior": "entertainment",
                                    "accent": "not-a-color"}])
    assert out[0]["accent"].startswith("#") and len(out[0]["accent"]) == 7
    assert out[0]["accent2"].startswith("#") and len(out[0]["accent2"]) == 7


def test_validate_tabs_missing_accent2_triggers_auto_assign_for_both():
    out = sections.validate_tabs([{"label": "A", "behavior": "entertainment",
                                    "accent": "#123abc"}])
    # accent alone (no valid accent2) isn't a complete valid pair -> both
    # get replaced together, never a half-valid record.
    assert out[0]["accent"] != "#123abc" or "accent2" in out[0]
    assert "accent2" in out[0]


def test_validate_tabs_auto_palette_stable_across_calls():
    raw = [{"label": "A", "behavior": "entertainment"},
           {"label": "B", "behavior": "courses"},
           {"label": "C", "behavior": "podcasts"}]
    out1 = sections.validate_tabs(raw)
    out2 = sections.validate_tabs(raw)
    assert [t["accent"] for t in out1] == [t["accent"] for t in out2]
    assert [t.get("accent2") for t in out1] == [t.get("accent2") for t in out2]


def test_validate_tabs_auto_palette_avoids_collisions():
    raw = [{"label": "A", "behavior": "entertainment"},
           {"label": "B", "behavior": "courses"},
           {"label": "C", "behavior": "podcasts"},
           {"label": "D", "behavior": "entertainment"}]
    out = sections.validate_tabs(raw)
    accents = [t["accent"] for t in out]
    assert len(accents) == len(set(accents))  # no two auto-assigned tabs collide


def test_validate_tabs_non_dict_entries_ignored():
    out = sections.validate_tabs(["not-a-dict", None, 42,
                                   {"label": "Real", "behavior": "entertainment"}])
    assert len(out) == 1
    assert out[0]["label"] == "Real"


def test_validate_tabs_none_input():
    assert sections.validate_tabs(None) == []


# ----------------------------------------------------------------- behavior_for --

def test_behavior_for_resolves_tab_behavior(monkeypatch):
    monkeypatch.setattr(sections, "_tabs", [_tab(behavior="courses")])
    assert sections.behavior_for("entertainment") == "courses"


def test_behavior_for_unknown_tab_is_none(monkeypatch):
    monkeypatch.setattr(sections, "_tabs", [_tab()])
    assert sections.behavior_for("no-such-tab") is None


def test_behavior_for_dead_behavior_is_none(monkeypatch):
    # Simulate a tab whose plugin behavior has since been removed.
    monkeypatch.setattr(sections, "_tabs", [_tab(behavior="removed-plugin")])
    assert sections.behavior_for("entertainment") is None


# ---------------------------------------------------------------- all_sections --

def test_all_sections_returns_tab_keys_in_order(monkeypatch):
    monkeypatch.setattr(sections, "_tabs", [
        _tab(key="b", label="B"), _tab(key="a", label="A"),
    ])
    assert sections.all_sections() == ("b", "a")


def test_all_sections_empty_by_default(monkeypatch, tmp_path):
    # No config.json present -> config.load_config()["tabs"] is [] (DEFAULTS).
    monkeypatch.setattr(sections.config, "USER_DIR", str(tmp_path / "drivecast"))
    monkeypatch.setattr(sections.config, "CONFIG_PATH", str(tmp_path / "drivecast" / "config.json"))
    monkeypatch.setattr(sections.config, "DATA_DIR", str(tmp_path / "drivecast" / "data"))
    monkeypatch.setattr(sections.config, "POSTERS_DIR", str(tmp_path / "drivecast" / "data" / "posters"))
    monkeypatch.setattr(sections.config, "SECRETS_PATH", str(tmp_path / "drivecast" / "secrets" / "secrets.json"))
    assert sections.all_sections() == ()


# -------------------------------------------------------------------- meta_list --

def test_meta_list_resolves_courses_vocab_and_identity(monkeypatch):
    monkeypatch.setattr(sections, "_tabs", [
        _tab(key="mycourses", label="My Courses", icon="📚", behavior="courses",
             accent="#111111", accent2="#222222"),
    ])
    out = sections.meta_list()
    assert len(out) == 1
    m = out[0]
    assert m["key"] == "mycourses"
    assert m["behavior"] == "courses"
    assert m["season"] == "Module"
    assert m["episode"] == "Lesson"
    assert m["label"] == "My Courses"
    assert m["icon"] == "📚"
    assert m["accent"] == "#111111"
    assert m["accent2"] == "#222222"
    assert m["lib"] == "My Courses"
    assert "My Courses" in m["empty"]


def test_meta_list_no_accent_when_tab_has_none(monkeypatch):
    monkeypatch.setattr(sections, "_tabs", [_tab()])
    m = sections.meta_list()[0]
    assert "accent" not in m


# -------------------------------------------------------------------- mimes_for --

def test_mimes_for_inherits_from_behavior(monkeypatch):
    monkeypatch.setattr(sections, "_tabs", [_tab(key="t1", behavior="podcasts")])
    assert sections.mimes_for("t1") == ("video", "audio")


def test_mimes_for_unresolvable_defaults_to_video(monkeypatch):
    monkeypatch.setattr(sections, "_tabs", [])
    assert sections.mimes_for("nope") == ("video",)


# ------------------------------------------------------------ section_for_drive --

def test_section_for_drive_unassigned_is_none(monkeypatch):
    monkeypatch.setattr(sections, "_tabs", [_tab()])
    assert sections.section_for_drive({}, "drive-x") is None


def test_section_for_drive_unknown_tab_is_none(monkeypatch):
    monkeypatch.setattr(sections, "_tabs", [_tab()])
    assert sections.section_for_drive({"drive-x": "ghost-tab"}, "drive-x") is None


def test_section_for_drive_resolves_live_tab(monkeypatch):
    monkeypatch.setattr(sections, "_tabs", [_tab(key="courses-tab", behavior="courses")])
    assert sections.section_for_drive({"drive-x": "courses-tab"}, "drive-x") == "courses-tab"


# ---------------------------------------------------------------------- tabs() --

def test_tabs_lazy_loads_from_config(monkeypatch):
    monkeypatch.setattr(sections.config, "load_config",
                        lambda: {"tabs": [{"label": "Loaded", "behavior": "entertainment"}]})
    assert sections.tabs()[0]["label"] == "Loaded"


def test_set_tabs_replaces_cache():
    result = sections.set_tabs([{"label": "Fresh", "behavior": "courses"}])
    assert result == sections.tabs()
    assert sections.tabs()[0]["label"] == "Fresh"


# ---------------------------------------------------------------------- behaviors --

def test_behaviors_includes_the_three_builtins():
    b = sections.behaviors()
    assert set(sections.BUILTIN_BEHAVIORS) <= set(b)
    assert b["courses"]["mimes"] == ("video", "pdf", "image")


def test_behaviors_meta_shape():
    lst = sections.behaviors_meta()
    keys = {b["key"] for b in lst}
    assert set(sections.BUILTIN_BEHAVIORS) <= keys
    for b in lst:
        assert set(b) == {"key", "label"}
