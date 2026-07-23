"""Tests for the menu-bar menu model (build_menu_spec) — no rumps required."""
import drivecast_menubar as mb


def _drives():
    return [{"id": "d1", "name": "Movies"}, {"id": "d2", "name": "Anime"}]


def test_build_menu_spec_structure():
    spec = mb.build_menu_spec(_drives(), ["d1"], auto_refresh=True,
                              setup_ok=True, port=8737)
    kinds = [it["kind"] for it in spec]
    assert kinds[0] == "status"
    keys = {it.get("key") for it in spec}
    assert {"open", "refresh", "auto_refresh", "quit"} <= keys

    # The auto-refresh toggle reflects the passed state.
    auto = next(it for it in spec if it.get("key") == "auto_refresh")
    assert auto["kind"] == "check" and auto["checked"] is True

    # Drives live in a submenu with one checkable item each; d1 is checked.
    submenu = next(it for it in spec if it["kind"] == "submenu")
    assert submenu["title"] == "Drives to include"
    children = {c["title"]: c for c in submenu["children"]}
    assert children["Movies"]["checked"] is True
    assert children["Anime"]["checked"] is False
    assert children["Movies"]["key"] == "drive:d1"


def test_build_menu_spec_no_drives_and_setup_needed():
    spec = mb.build_menu_spec([], [], auto_refresh=False, setup_ok=False, port=9000)
    status = spec[0]
    assert status["kind"] == "status"
    assert "setup needed" in status["title"]
    submenu = next(it for it in spec if it["kind"] == "submenu")
    assert submenu["children"][0]["title"] == "No drives found"


def test_build_menu_spec_custom_status_text():
    spec = mb.build_menu_spec(_drives(), [], False, True, 8737,
                              status_text="drivecast: scanning… (1/2)")
    assert spec[0]["title"] == "drivecast: scanning… (1/2)"


def test_menu_spec_refresh_drive_submenu():
    drives = [{"id": "d1", "name": "Anime"}, {"id": "d2", "name": "Courses"},
              {"id": "d3", "name": "Unselected"}]
    spec = mb.build_menu_spec(drives, ["d1", "d2"], False, True, 8737)
    sub = next(it for it in spec
               if it["kind"] == "submenu" and it["title"] == "Refresh one drive")
    keys = [c["key"] for c in sub["children"]]
    assert keys == ["refresh_drive:d1", "refresh_drive:d2"]   # selected only
    titles = [c["title"] for c in sub["children"]]
    assert titles == ["Anime", "Courses"]


def test_menu_spec_no_refresh_drive_submenu_without_selection():
    spec = mb.build_menu_spec([{"id": "d1", "name": "X"}], [], False, True, 8737)
    assert not any(it.get("title") == "Refresh one drive"
                   for it in spec if it["kind"] == "submenu")
