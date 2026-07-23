"""API tests that serve entirely from a synthetic cache — no Drive network.

The DriveAPI's low-level GET is stubbed to raise, so any test that accidentally
reaches the Drive API fails loudly. Playback and library/settings endpoints must
answer from the cached library alone.
"""
import json

import pytest
from fastapi.testclient import TestClient

from drivecast import config as config_mod
from drivecast import history as history_mod
from drivecast import library as library_mod
from drivecast import server as server_mod
from drivecast.drive_api import DriveAPI
from drivecast.player import PlayerManager
from drivecast.rclone_auth import TokenManager


SYNTHETIC = {
    "version": 1,
    "generated_at": 123.0,
    "titles": {
        "movieA": {
            "id": "movieA", "type": "movie", "title": "Skyharbor", "year": 2016,
            "drive_id": "drv1", "folder_id": "movieA", "poster": None,
            "tmdb_id": None, "overview": "aliens", "quality": "4K",
            "file_id": "fileA", "size": 5000, "duration_ms": 7200000,
        },
        "showB": {
            "id": "showB", "type": "show", "title": "The Ladle", "year": 2022,
            "drive_id": "drv1", "folder_id": "showB", "poster": "bear.jpg",
            "tmdb_id": None, "overview": "kitchen",
            "seasons": [
                {"season": 1, "episodes": [
                    {"title": "System", "episode": 1, "file_id": "fileE1",
                     "name": "The.Ladle.S01E01.mkv", "duration_ms": 1500000,
                     "size": 900, "parent_id": "s1"},
                    {"title": "Hands", "episode": 2, "file_id": "fileE2",
                     "name": "The.Ladle.S01E02.mkv", "duration_ms": 1400000,
                     "size": 850, "parent_id": "s1"},
                ]},
                {"season": 2, "episodes": [
                    {"title": "Emergency", "episode": 1, "file_id": "fileE3",
                     "name": "The.Ladle.S02E01.mkv", "duration_ms": 1600000,
                     "size": 950, "parent_id": "s2"},
                ]},
                {"season": 0, "name": "Extras", "extras": True, "episodes": [
                    {"title": "Behind the Scenes", "episode": 1, "file_id": "fileX1",
                     "name": "Ladle.Extras.mkv", "duration_ms": 300000,
                     "size": 200, "parent_id": "sx"},
                ]},
            ],
        },
    },
}


def _install_stubs(tmp_path, monkeypatch):
    """Point the server at a synthetic library and stub out rclone / Drive /
    player / save_config so no test touches the network or the user's data.
    Returns the dict the fake player records its call into."""
    # Write a synthetic library the server will load.
    lib_path = tmp_path / "library.json"
    lib_path.write_text(json.dumps(SYNTHETIC))

    # Point the server's Library/Scanner at the temp file.
    monkeypatch.setattr(server_mod, "Library",
                        lambda **kw: library_mod.Library(path=str(lib_path), **kw))

    # Keep watch history and the scan cache in the temp dir too — never touch
    # the user's real data.
    monkeypatch.setattr(server_mod, "History",
                        lambda: history_mod.History(path=str(tmp_path / "history.json")))
    from drivecast import scan_cache as scan_cache_mod
    monkeypatch.setattr(server_mod, "ScanCache",
                        lambda: scan_cache_mod.ScanCache(path=str(tmp_path / "scan_cache.json")))

    # No rclone / no Drive network anywhere.
    async def _fake_token(self):
        return "faketoken"

    def _no_network(self, url, params):
        raise AssertionError("Drive API was contacted: %s" % url)

    captured = {}

    def _fake_play(self, file_id, name, duration_ms=None, drive_id=None,
                   parent_id=None, queue=None, media=None, sub_path=None):
        captured["media"] = media
        captured["sub_path"] = sub_path
        captured["file_id"] = file_id
        captured["duration_ms"] = duration_ms
        captured["queue"] = queue
        return {"player": "mpv", "resumed_from": 0}

    monkeypatch.setattr(TokenManager, "get_token", _fake_token)
    monkeypatch.setattr(DriveAPI, "_get", _no_network)
    monkeypatch.setattr(PlayerManager, "play", _fake_play)
    monkeypatch.setattr(config_mod, "save_config", lambda cfg: None)
    return captured


def _base_cfg(**overrides):
    cfg = dict(config_mod.DEFAULTS)
    cfg.update({"tmdb_api_key": "", "selected_drives": ["drv1"],
                "auto_refresh_on_startup": False})
    cfg.update(overrides)
    return cfg


@pytest.fixture
def client(tmp_path, monkeypatch):
    captured = _install_stubs(tmp_path, monkeypatch)
    app = server_mod.create_app(_base_cfg())
    with TestClient(app) as c:
        # Keep the subtitle cache in the temp dir; Drive lookups already fail
        # loudly via the _no_network stub.
        c.app.state.dc.subtitles.subs_dir = str(tmp_path / "subs")
        c._captured = captured
        yield c


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """Factory for TestClients with arbitrary cfg + a spoofed socket peer.

    starlette's TestClient sets scope["client"] from its `client` kwarg (never
    from headers), so this is the same socket-level seam the middleware trusts.
    Returns a context manager yielding the client.
    """
    captured = _install_stubs(tmp_path, monkeypatch)

    from contextlib import contextmanager

    @contextmanager
    def _make(cfg_overrides=None, client_addr=("203.0.113.7", 51234)):
        app = server_mod.create_app(_base_cfg(**(cfg_overrides or {})))
        with TestClient(app, client=client_addr) as c:
            c.app.state.dc.subtitles.subs_dir = str(tmp_path / "subs")
            c._captured = captured
            yield c

    return _make


def test_library_endpoint_serves_cache(client):
    r = client.get("/api/library")
    assert r.status_code == 200
    data = r.json()
    titles = {t["title"] for t in data["titles"]}
    assert titles == {"Skyharbor", "The Ladle"}
    assert data["selected_drives"] == ["drv1"]
    assert data["scanning"] is False
    # Quality field is serialized straight through from the record.
    skyharbor = next(t for t in data["titles"] if t["title"] == "Skyharbor")
    assert skyharbor["quality"] == "4K"


def test_watched_map_endpoint(client):
    # Record a play position, then the watched-map exposes its last_played.
    client.post("/api/play", json={"file_id": "fileA", "name": "Skyharbor"})
    client.app.state.dc.history.update("fileA", position=120.0, duration=7200.0, force=True)
    r = client.get("/api/watched-map")
    assert r.status_code == 200
    m = r.json()["map"]
    assert "fileA" in m and m["fileA"] > 0


def test_title_endpoint(client):
    r = client.get("/api/title/showB")
    assert r.status_code == 200
    rec = r.json()
    assert rec["type"] == "show"
    assert rec["seasons"][0]["episodes"][0]["title"] == "System"

    assert client.get("/api/title/nope").status_code == 404


def test_settings_get(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["selected_drives"] == ["drv1"]
    assert body["auto_refresh_on_startup"] is False
    assert "player" in body            # player preference exposed
    assert "available_players" in body  # which players are installed


def test_settings_post_player(client):
    r = client.post("/api/settings", json={"player": "vlc"})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["player"] == "vlc"
    # invalid choice is ignored (stays a valid value)
    client.post("/api/settings", json={"player": "bogus"})
    assert client.get("/api/settings").json()["player"] == "vlc"


def test_settings_post_player_infuse(client):
    r = client.post("/api/settings", json={"player": "infuse"})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["player"] == "infuse"


def test_available_players_includes_infuse(client, monkeypatch):
    import drivecast.player as player_mod
    monkeypatch.setattr(
        player_mod, "detect_player",
        lambda pref="auto": (pref, "/x") if pref in ("mpv", "iina", "vlc", "infuse") else (None, None),
    )
    assert "infuse" in client.get("/api/settings").json()["available_players"]


def test_settings_post_toggle_auto_refresh(client):
    r = client.post("/api/settings", json={"auto_refresh_on_startup": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["auto_refresh_on_startup"] is True
    # No drive change -> no refresh kicked.
    assert body["refresh_started"] is False


def test_play_uses_cached_duration_no_drive_call(client):
    # POST with only a file_id: duration must come from the cached library,
    # and no Drive metadata call may happen (DriveAPI._get raises if it does).
    r = client.post("/api/play", json={"file_id": "fileA", "name": "Skyharbor"})
    assert r.status_code == 200
    assert r.json()["player"] == "mpv"
    assert client._captured["duration_ms"] == 7200000  # from the cache


def test_play_episode_cached_duration(client):
    r = client.post("/api/play", json={"file_id": "fileE1", "name": "Ep1"})
    assert r.status_code == 200
    assert client._captured["duration_ms"] == 1500000


def test_play_passes_queue_through(client):
    # An autoplay queue is whitelisted and handed to PlayerManager.play.
    r = client.post("/api/play", json={
        "file_id": "fileE1", "name": "Ep1",
        "queue": [
            {"file_id": "fileE2", "name": "Ep2", "duration_ms": 1200000},
            {"file_id": "fileE3", "name": "Ep3"},
        ],
    })
    assert r.status_code == 200
    q = client._captured["queue"]
    assert [x["file_id"] for x in q] == ["fileE2", "fileE3"]
    assert q[0]["duration_ms"] == 1200000
    assert q[0]["name"] == "Ep2"


def test_play_queue_drops_malformed_items(client):
    # Items without a file_id (or non-dicts) are dropped; a non-list is ignored.
    r = client.post("/api/play", json={
        "file_id": "fileE1", "name": "Ep1",
        "queue": [{"name": "no id"}, "garbage", {"file_id": "fileE2"}],
    })
    assert r.status_code == 200
    assert [x["file_id"] for x in client._captured["queue"]] == ["fileE2"]

    r2 = client.post("/api/play", json={"file_id": "fileE1", "name": "Ep1", "queue": "nope"})
    assert r2.status_code == 200
    assert client._captured["queue"] == []


def test_settings_roundtrips_autoplay_next(client):
    # Default is on; POST toggles it off and GET reflects the change.
    assert client.get("/api/settings").json()["autoplay_next"] is True
    r = client.post("/api/settings", json={"autoplay_next": False})
    assert r.status_code == 200
    assert r.json()["autoplay_next"] is False
    assert client.get("/api/settings").json()["autoplay_next"] is False


def test_continue_enriched_with_library_title(client):
    # A partially-watched episode surfaces on the Continue shelf carrying the
    # owning show's title/poster so the UI can render a thumbnail.
    client.app.state.dc.history.update("fileE1", name="The.Ladle.S01E01.mkv",
                                       position=600.0, duration=1500.0, force=True)
    r = client.get("/api/continue")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["file_id"] == "fileE1"
    assert it["title"] == "The Ladle"
    assert it["title_id"] == "showB"
    assert it["type"] == "show"
    assert it["poster"] == "bear.jpg"


def test_continue_unknown_file_passes_through(client):
    # Files played outside the library (e.g. via Browse) stay unenriched.
    client.app.state.dc.history.update("strayFile", name="stray.mkv",
                                       position=100.0, duration=1000.0, force=True)
    items = client.get("/api/continue").json()["items"]
    it = next(x for x in items if x["file_id"] == "strayFile")
    assert it["name"] == "stray.mkv"
    assert "poster" not in it and "title" not in it


def test_history_remove_drops_entry_and_persists(tmp_path):
    hist = history_mod.History(path=str(tmp_path / "history.json"))
    hist.update("fileA", name="Skyharbor", position=100.0, duration=1000.0, force=True)
    assert hist.get("fileA") is not None
    # Removing an existing entry returns True and rewrites the file without it.
    assert hist.remove("fileA") is True
    assert hist.get("fileA") is None
    with open(hist.path) as f:
        assert "fileA" not in json.load(f)
    # An unknown id is a no-op returning False.
    assert hist.remove("nope") is False


def test_continue_remove_endpoint_deletes_entry(client):
    hist = client.app.state.dc.history
    hist.update("fileE1", name="The.Ladle.S01E01.mkv",
                position=600.0, duration=1500.0, force=True)
    assert any(it["file_id"] == "fileE1"
               for it in client.get("/api/continue").json()["items"])
    r = client.delete("/api/continue/fileE1")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "removed": True}
    # Gone from history (and thus the shelf) and off disk.
    assert hist.get("fileE1") is None
    assert not any(it["file_id"] == "fileE1"
                   for it in client.get("/api/continue").json()["items"])
    assert "fileE1" not in _disk(client)


def test_continue_remove_unknown_file_id(client):
    r = client.delete("/api/continue/does-not-exist")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "removed": False}


def test_static_assets_carry_no_cache(client):
    # Static assets must revalidate so a rebuilt app.js/style.css isn't served
    # stale from the browser's heuristic cache.
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"


def test_refresh_status_shape(client):
    r = client.get("/api/refresh/status")
    assert r.status_code == 200
    st = r.json()
    for k in ("running", "scanned", "total", "added", "removed", "error",
              "scope", "scope_names"):
        assert k in st


def _capture_refresh(client):
    """Stub start_refresh on the live AppState, capturing the scope."""
    calls = []

    def _fake(scope=None):
        calls.append(scope)
        return True

    client.app.state.dc.start_refresh = _fake
    return calls


def test_refresh_scoped_to_one_drive(client):
    calls = _capture_refresh(client)
    r = client.post("/api/refresh", json={"drives": ["drv1"]})
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is True
    assert body["scope"] == ["drv1"]
    assert calls == [["drv1"]]


def test_refresh_bodyless_is_full(client):
    # The menubar POSTs with no body: full refresh over all selected drives.
    calls = _capture_refresh(client)
    r = client.post("/api/refresh")
    assert r.status_code == 200
    assert r.json()["scope"] == ["drv1"]
    assert calls == [None]


def test_refresh_rejects_unselected_drive(client):
    calls = _capture_refresh(client)
    r = client.post("/api/refresh", json={"drives": ["not-selected"]})
    assert r.status_code == 400
    assert calls == []

    r2 = client.post("/api/refresh", json={"drives": "drv1"})   # not a list
    assert r2.status_code == 400


def test_settings_drive_sections_roundtrip_and_scoped_refresh(client):
    # A drive can only be assigned to a TAB that exists (tabs are user data now,
    # zero by default) — create one first, then assign drv1 to it.
    client.post("/api/settings", json={
        "tabs": [{"key": "podcasts", "label": "Podcasts", "behavior": "podcasts"}]})
    calls = _capture_refresh(client)
    r = client.post("/api/settings", json={"drive_sections": {"drv1": "podcasts", "x": "bogus"}})
    assert r.status_code == 200
    body = r.json()
    assert body["drive_sections"] == {"drv1": "podcasts"}   # invalid value dropped
    # Changing drv1's section triggered a refresh scoped to just drv1.
    assert calls == [["drv1"]]
    assert client.get("/api/settings").json()["drive_sections"] == {"drv1": "podcasts"}


def test_watched_map_progress_shape(client):
    client.app.state.dc.history.update("fileA", position=3600.0, duration=7200.0, force=True)
    body = client.get("/api/watched-map").json()
    assert "map" in body and "progress" in body
    p = body["progress"]["fileA"]
    assert p["percent"] == 50.0 and p["watched"] is False


def test_play_resolves_and_passes_subtitle(client, tmp_path):
    async def _fake_resolve(file_id, name, drive_id=None, parent_id=None):
        return str(tmp_path / "subs" / ("%s.srt" % file_id))

    client.app.state.dc.subtitles.resolve = _fake_resolve
    r = client.post("/api/play", json={"file_id": "fileA", "name": "Skyharbor"})
    assert r.status_code == 200
    assert r.json()["subtitles"] is True
    assert client._captured["sub_path"].endswith("fileA.srt")


def test_play_subtitles_toggle_off(client):
    calls = []

    async def _spy_resolve(*a, **k):
        calls.append(a)
        return None

    client.app.state.dc.subtitles.resolve = _spy_resolve
    client.post("/api/settings", json={"subtitles": False})
    r = client.post("/api/play", json={"file_id": "fileA", "name": "Skyharbor"})
    assert r.status_code == 200
    assert r.json()["subtitles"] is False
    assert calls == []                     # resolver never consulted
    assert client._captured["sub_path"] is None


def test_settings_roundtrips_subtitles(client):
    assert client.get("/api/settings").json()["subtitles"] is True
    client.post("/api/settings", json={"subtitles": False})
    assert client.get("/api/settings").json()["subtitles"] is False


def test_settings_roundtrips_keep_awake(client):
    # Default is on; POST toggles it off and GET reflects the change. The live
    # KeepAwake reads the same cfg, so the toggle takes effect immediately.
    assert config_mod.DEFAULTS["keep_awake"] is True
    assert client.get("/api/settings").json()["keep_awake"] is True
    r = client.post("/api/settings", json={"keep_awake": False})
    assert r.status_code == 200
    assert r.json()["keep_awake"] is False
    assert client.get("/api/settings").json()["keep_awake"] is False
    assert client.app.state.dc.cfg["keep_awake"] is False


# ---- keep-awake status / extend / release ----

def test_awake_status_shape(client):
    r = client.get("/api/awake/status")
    assert r.status_code == 200
    st = r.json()
    assert set(st) == {"active", "holding", "phase", "seconds_left"}
    assert st["phase"] == "idle" and st["active"] == 0 and st["holding"] is False


def test_awake_extend_release_roundtrip(client):
    awake = client.app.state.dc.awake
    awake._ac_check = lambda: True          # avoid shelling pmset in tests
    awake.acquire()                         # active (held)
    awake.release()                         # -> grace (still held)
    assert client.get("/api/awake/status").json()["phase"] == "grace"
    # Yes, keep watching: a fresh grace, returned as the new status.
    assert client.post("/api/awake/extend").json()["phase"] == "grace"
    # No: drop to idle immediately.
    assert client.post("/api/awake/release").json()["phase"] == "idle"
    assert client.get("/api/awake/status").json()["phase"] == "idle"


def test_awake_endpoints_require_token_for_remote(make_client):
    # No exemptions in the middleware: a remote client without a token is 401.
    with make_client({"remote_access": True, "remote_token": "sekret"}) as c:
        assert c.get("/api/awake/status").status_code == 401
        assert c.post("/api/awake/extend").status_code == 401
        assert c.post("/api/awake/release").status_code == 401


# ==========================================================================
# Remote access: config, auth middleware, /api/remote(/qr), /api/progress.
# ==========================================================================

def _disk(client):
    """Read the on-disk history.json for the fixture's temp history file."""
    with open(client.app.state.dc.history.path) as f:
        return json.load(f)


def test_config_defaults_include_remote_keys():
    assert config_mod.DEFAULTS["remote_access"] is False
    assert config_mod.DEFAULTS["remote_token"] == ""
    # Non-secret, so both persist to config.json.
    assert "remote_access" in config_mod.SAVED_KEYS
    assert "remote_token" in config_mod.SAVED_KEYS


# ---- /api/progress ----

def test_progress_requires_file_id(client):
    r = client.post("/api/progress", json={"position": 5.0})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_progress_updates_history_and_ended_forces_write(client):
    hist = client.app.state.dc.history
    # First report persists (the initial write is never debounced) and history
    # computes percent from position/duration.
    r = client.post("/api/progress", json={"file_id": "fileA", "name": "Skyharbor",
                                            "position": 100.0, "duration": 1000.0})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert _disk(client)["fileA"]["percent"] == 10.0
    # A follow-up within the debounce window updates memory but not disk...
    client.post("/api/progress", json={"file_id": "fileA", "position": 200.0,
                                        "duration": 1000.0})
    assert _disk(client)["fileA"]["position"] == 100.0
    assert hist.get("fileA")["position"] == 200.0
    # ...until ended=True forces the write straight through.
    client.post("/api/progress", json={"file_id": "fileA", "position": 300.0,
                                        "duration": 1000.0, "ended": True})
    assert _disk(client)["fileA"]["position"] == 300.0


def test_progress_ended_marks_watched(client):
    client.post("/api/progress", json={"file_id": "fileA", "name": "Skyharbor",
                                        "position": 950.0, "duration": 1000.0,
                                        "ended": True})
    assert client.app.state.dc.history.get("fileA")["watched"] is True


# ---- auth middleware allow/deny matrix ----

def test_middleware_local_passes_when_remote_disabled(client):
    # The fixture client is the "testclient" socket peer -> always trusted.
    assert client.get("/api/library").status_code == 200


def test_middleware_remote_denied_when_disabled(make_client):
    with make_client() as c:                 # remote_access False by default
        r = c.get("/api/library")
        assert r.status_code == 403
        assert r.json() == {"error": "remote_disabled"}


def test_middleware_remote_token_matrix(make_client):
    with make_client({"remote_access": True, "remote_token": "sekret"}) as c:
        # No token -> 401 JSON.
        r = c.get("/api/library")
        assert r.status_code == 401
        assert r.json()["error"] == "unauthorized"
        # A one-character-off token fails (compared with hmac.compare_digest).
        assert c.get("/api/library?token=sekrey").status_code == 401
        # The exact token is authorized.
        assert c.get("/api/library?token=sekret").status_code == 200


def test_middleware_empty_config_token_never_authorizes(make_client):
    with make_client({"remote_access": True, "remote_token": ""}) as c:
        assert c.get("/api/library?token=").status_code == 401
        assert c.get("/api/library?token=anything").status_code == 401


def test_middleware_html_login_page_on_failure(make_client):
    with make_client({"remote_access": True, "remote_token": "sekret"}) as c:
        r = c.get("/api/library", headers={"accept": "text/html"})
        assert r.status_code == 401
        assert "text/html" in r.headers["content-type"]
        # A GET form re-requesting "/" with a `token` field.
        assert 'name="token"' in r.text and 'action="/"' in r.text


def test_middleware_query_token_bootstraps_cookie(make_client):
    with make_client({"remote_access": True, "remote_token": "sekret"}) as c:
        r = c.get("/api/remote?token=sekret")
        assert r.status_code == 200
        set_cookie = r.headers.get("set-cookie", "").lower()
        assert "dc_token" in set_cookie and "httponly" in set_cookie
        # The planted cookie authorizes a later request that carries no ?token=.
        assert c.get("/api/remote").status_code == 200


def test_play_rejected_for_remote_client(make_client):
    with make_client({"remote_access": True, "remote_token": "sekret"}) as c:
        # Authorized by the query token, but playback still refuses a non-local
        # client — a phone must never launch mpv on the Mac.
        r = c.post("/api/play?token=sekret", json={"file_id": "fileA", "name": "Skyharbor"})
        assert r.status_code == 403
        assert r.json()["error"] == "local_only"


# ---- settings plumbing ----

def test_settings_get_exposes_remote_access(client):
    assert client.get("/api/settings").json()["remote_access"] is False


def test_settings_enable_remote_generates_token_and_flags_restart(client):
    r = client.post("/api/settings", json={"remote_access": True})
    body = r.json()
    assert body["remote_access"] is True
    assert body["restart_required"] is True          # value changed
    token = client.get("/api/remote").json()["token"]
    assert token and len(token) >= 16                # secrets.token_urlsafe(16)
    # Re-enabling (no change) needs no restart and keeps the same token.
    r2 = client.post("/api/settings", json={"remote_access": True})
    assert r2.json()["restart_required"] is False
    assert client.get("/api/remote").json()["token"] == token
    # Disabling flips the value again -> restart required.
    r3 = client.post("/api/settings", json={"remote_access": False})
    assert r3.json()["restart_required"] is True
    assert client.get("/api/settings").json()["remote_access"] is False


# ---- /api/remote + /api/remote/qr ----

def test_remote_endpoint_lists_tailscale_first(client, monkeypatch):
    monkeypatch.setattr(server_mod, "_tailscale_serve_url", lambda port: None)
    monkeypatch.setattr(server_mod, "_tailscale_ip", lambda: "100.101.102.103")
    monkeypatch.setattr(server_mod, "_lan_ip", lambda: "192.168.1.50")
    client.post("/api/settings", json={"remote_access": True})
    body = client.get("/api/remote").json()
    assert body["enabled"] is True
    assert body["port"] == 8737
    assert [u["label"] for u in body["urls"]] == ["Tailscale", "Wi-Fi"]
    tok = body["token"]
    assert body["urls"][0]["url"] == "http://100.101.102.103:8737/?token=%s" % tok
    assert body["urls"][1]["url"] == "http://192.168.1.50:8737/?token=%s" % tok


def test_remote_qr_404_when_disabled(client):
    assert client.get("/api/remote/qr").status_code == 404


def test_remote_qr_serves_svg(client, monkeypatch):
    monkeypatch.setattr(server_mod, "_tailscale_ip", lambda: None)
    monkeypatch.setattr(server_mod, "_lan_ip", lambda: "192.168.1.50")
    client.post("/api/settings", json={"remote_access": True})
    r = client.get("/api/remote/qr")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert b"<svg" in r.content


def test_remote_qr_404_when_no_ip(client, monkeypatch):
    monkeypatch.setattr(server_mod, "_tailscale_serve_url", lambda port: None)
    monkeypatch.setattr(server_mod, "_tailscale_ip", lambda: None)
    monkeypatch.setattr(server_mod, "_lan_ip", lambda: None)
    client.post("/api/settings", json={"remote_access": True})
    r = client.get("/api/remote/qr")
    assert r.status_code == 404
    assert r.json()["error"] == "no_url"


def test_tailscale_ip_filters_cgnat_range(monkeypatch):
    class _Proc:
        def __init__(self, stdout, rc=0):
            self.stdout, self.returncode = stdout, rc

    monkeypatch.setattr(server_mod.subprocess, "run", lambda *a, **k: _Proc("100.101.102.103\n"))
    assert server_mod._tailscale_ip() == "100.101.102.103"
    # A non-CGNAT address (e.g. a plain LAN IP) is not a Tailscale address.
    monkeypatch.setattr(server_mod.subprocess, "run", lambda *a, **k: _Proc("192.168.1.5\n"))
    assert server_mod._tailscale_ip() is None
    # Non-zero exit from both candidate commands -> None.
    monkeypatch.setattr(server_mod.subprocess, "run", lambda *a, **k: _Proc("", rc=1))
    assert server_mod._tailscale_ip() is None


def test_remote_prefers_tailscale_serve_https(client, monkeypatch):
    # With `tailscale serve` proxying our port, the HTTPS ts.net URL is listed
    # first and the QR uses it (fixes HTTPS-Only Safari on iOS).
    monkeypatch.setattr(server_mod, "_tailscale_serve_url",
                        lambda port: "https://mac.tailnet.ts.net")
    monkeypatch.setattr(server_mod, "_tailscale_ip", lambda: "100.1.2.3")
    monkeypatch.setattr(server_mod, "_lan_ip", lambda: "192.168.1.9")
    client.app.state.dc.cfg["remote_access"] = True
    client.app.state.dc.cfg["remote_token"] = "tok123"
    body = client.get("/api/remote").json()
    labels = [u["label"] for u in body["urls"]]
    assert labels[0] == "Tailscale (HTTPS)"
    assert body["urls"][0]["url"] == "https://mac.tailnet.ts.net/?token=tok123"
    qr = client.get("/api/remote/qr")
    assert qr.status_code == 200
    assert qr.headers["content-type"].startswith("image/svg")


def test_serve_url_parses_status_output(monkeypatch):
    class _P:
        returncode = 0
        stdout = ("https://mac.tailnet.ts.net (tailnet only)\n"
                  "|-- / proxy http://127.0.0.1:8737\n")
        stderr = ""

    monkeypatch.setattr(server_mod.subprocess, "run", lambda *a, **k: _P())
    assert server_mod._tailscale_serve_url(8737) == "https://mac.tailnet.ts.net"
    assert server_mod._tailscale_serve_url(9999) is None   # different port


# ---- trusted-LAN HTTPS ----

def test_remote_lists_wifi_https_first(client, monkeypatch):
    # With the HTTPS listener live, "Wi-Fi (HTTPS)" leads (Tailscale-independent)
    # and a ca_url is offered for the one-time trust step.
    monkeypatch.setattr(server_mod, "_tailscale_serve_url", lambda port: None)
    monkeypatch.setattr(server_mod, "_tailscale_ip", lambda: "100.1.2.3")
    monkeypatch.setattr(server_mod, "_lan_ip", lambda: "192.168.1.50")
    # The live leaf covers this LAN IP.
    monkeypatch.setattr(server_mod.localcert, "leaf_covers", lambda ip: True)
    monkeypatch.setattr(server_mod.localcert, "ca_fingerprint", lambda: "AB:CD")
    client.app.state.dc.cfg["remote_access"] = True
    client.app.state.dc.cfg["remote_token"] = "tok123"
    client.app.state.dc.cfg["_lan_https_port"] = 8738
    body = client.get("/api/remote").json()
    assert body["urls"][0] == {
        "label": "Wi-Fi (HTTPS)",
        "url": "https://192.168.1.50:8738/?token=tok123"}
    assert body["https_port"] == 8738
    assert body["ca_url"] == "http://192.168.1.50:8737/api/remote/ca"
    assert body["ca_fingerprint"] == "AB:CD"
    assert body["https_stale"] is False
    # Plain Wi-Fi is still offered, last (Android/guests).
    assert body["urls"][-1]["label"] == "Wi-Fi"


def test_remote_https_omitted_when_leaf_stale(client, monkeypatch):
    # LAN IP changed since the listener bound its cert: don't advertise a URL
    # the live listener can't serve; flag a restart instead.
    monkeypatch.setattr(server_mod, "_tailscale_serve_url", lambda port: None)
    monkeypatch.setattr(server_mod, "_tailscale_ip", lambda: None)
    monkeypatch.setattr(server_mod, "_lan_ip", lambda: "10.0.1.7")
    monkeypatch.setattr(server_mod.localcert, "leaf_covers", lambda ip: False)
    client.app.state.dc.cfg["remote_access"] = True
    client.app.state.dc.cfg["_lan_https_port"] = 8738
    body = client.get("/api/remote").json()
    assert [u["label"] for u in body["urls"]] == ["Wi-Fi"]  # plain only
    assert body["ca_url"] is None
    assert body["ca_fingerprint"] is None
    assert body["https_stale"] is True


def test_remote_no_https_entry_without_listener(client, monkeypatch):
    # Regression guard: without _lan_https_port the ordering is unchanged.
    monkeypatch.setattr(server_mod, "_tailscale_serve_url", lambda port: None)
    monkeypatch.setattr(server_mod, "_tailscale_ip", lambda: "100.1.2.3")
    monkeypatch.setattr(server_mod, "_lan_ip", lambda: "192.168.1.50")
    client.app.state.dc.cfg["remote_access"] = True
    body = client.get("/api/remote").json()
    assert [u["label"] for u in body["urls"]] == ["Tailscale", "Wi-Fi"]
    assert body["https_port"] is None
    assert body["ca_url"] is None


def test_remote_ca_download_no_token(make_client, monkeypatch):
    # A phone with no token cookie can still fetch the public CA cert.
    monkeypatch.setattr(server_mod.localcert, "ca_pem_bytes", lambda: b"-----CA-----")
    with make_client({"remote_access": True, "remote_token": "sekret"}) as c:
        r = c.get("/api/remote/ca")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/x-x509-ca-cert"
        assert r.content == b"-----CA-----"


def test_remote_ca_403_when_remote_disabled(make_client, monkeypatch):
    monkeypatch.setattr(server_mod.localcert, "ca_pem_bytes", lambda: b"-----CA-----")
    with make_client() as c:                 # remote_access False by default
        r = c.get("/api/remote/ca")
        assert r.status_code == 403
        assert r.json()["error"] == "remote_disabled"


def test_remote_ca_404_when_no_cert(client, monkeypatch):
    monkeypatch.setattr(server_mod.localcert, "ca_pem_bytes", lambda: None)
    client.app.state.dc.cfg["remote_access"] = True
    r = client.get("/api/remote/ca")
    assert r.status_code == 404
    assert r.json()["error"] == "not_available"


def test_remote_qr_trust_label(client, monkeypatch):
    monkeypatch.setattr(server_mod, "_tailscale_serve_url", lambda port: None)
    monkeypatch.setattr(server_mod, "_tailscale_ip", lambda: None)
    monkeypatch.setattr(server_mod, "_lan_ip", lambda: "192.168.1.50")
    monkeypatch.setattr(server_mod.localcert, "leaf_covers", lambda ip: True)
    client.app.state.dc.cfg["remote_access"] = True
    client.app.state.dc.cfg["_lan_https_port"] = 8738
    r = client.get("/api/remote/qr?label=trust")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg")
    assert b"<svg" in r.content


def test_remote_qr_trust_404_without_https(client, monkeypatch):
    # No HTTPS listener -> no ca_url -> the trust QR 404s.
    monkeypatch.setattr(server_mod, "_lan_ip", lambda: "192.168.1.50")
    client.app.state.dc.cfg["remote_access"] = True
    r = client.get("/api/remote/qr?label=trust")
    assert r.status_code == 404


def test_cookie_secure_only_over_https(tmp_path, monkeypatch):
    # A ?token= bootstrap cookie minted over TLS carries Secure; over plain
    # HTTP it does not (so it never leaks onto the plain listener).
    _install_stubs(tmp_path, monkeypatch)
    cfg = _base_cfg(remote_access=True, remote_token="sekret")
    app = server_mod.create_app(cfg)
    with TestClient(app, client=("203.0.113.7", 51234),
                    base_url="https://testserver") as c:
        r = c.get("/api/remote?token=sekret")
        assert r.status_code == 200
        assert "secure" in r.headers.get("set-cookie", "").lower()
    app2 = server_mod.create_app(_base_cfg(remote_access=True, remote_token="sekret"))
    with TestClient(app2, client=("203.0.113.7", 51234),
                    base_url="http://testserver") as c:
        r = c.get("/api/remote?token=sekret")
        assert "secure" not in r.headers.get("set-cookie", "").lower()


def test_remote_qr_label_selects_url(client, monkeypatch):
    monkeypatch.setattr(server_mod, "_tailscale_serve_url",
                        lambda port: "https://mac.tailnet.ts.net")
    monkeypatch.setattr(server_mod, "_tailscale_ip", lambda: "100.1.2.3")
    monkeypatch.setattr(server_mod, "_lan_ip", lambda: "192.168.1.9")
    client.app.state.dc.cfg["remote_access"] = True
    client.app.state.dc.cfg["remote_token"] = "tok123"
    # default = best (HTTPS); an explicit label picks that URL; unknown -> default
    for label, code in ((None, 200), ("Wi-Fi", 200), ("wi-fi", 200), ("nope", 200)):
        url = "/api/remote/qr" + (("?label=" + label) if label else "")
        r = client.get(url)
        assert r.status_code == code
        assert r.headers["content-type"].startswith("image/svg")


# ---- Fire TV support: file-index enrichment, /api/subtitles, /api/ping ----

def test_file_info_includes_drive_and_parent(client):
    lib = client.app.state.dc.library
    movie = lib.file_info("fileA")
    assert movie["drive_id"] == "drv1"
    assert movie["parent_id"] == "movieA"  # the movie's folder
    ep = lib.file_info("fileE1")
    assert ep["drive_id"] == "drv1"
    assert ep["parent_id"] == "s1"


def _seed_sub(client, file_id, ext=".srt", text="1\n00:00:01,000 --> 00:00:02,000\nhello\n"):
    import os
    subs_dir = client.app.state.dc.subtitles.subs_dir
    os.makedirs(subs_dir, exist_ok=True)
    path = os.path.join(subs_dir, file_id + ext)
    with open(path, "w") as f:
        f.write(text)
    return path


def test_subtitles_cached_hit(client):
    _seed_sub(client, "fileA")
    r = client.get("/api/subtitles/fileA")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-subrip")
    assert "hello" in r.text


def test_subtitles_miss_returns_404(client):
    # Unknown file: not in the index, no drive_id -> no sibling lookup, no
    # OpenSubtitles key -> resolver returns None without any network.
    r = client.get("/api/subtitles/unknownfile")
    assert r.status_code == 404
    assert r.json() == {"error": "no_subtitles"}


def test_subtitles_head_hit_and_miss(client):
    _seed_sub(client, "fileE1")
    hit = client.head("/api/subtitles/fileE1")
    assert hit.status_code == 200
    assert not hit.content  # HEAD: headers only
    miss = client.head("/api/subtitles/unknownfile")
    assert miss.status_code == 404


def test_subtitles_sub_ext_unsupported(client):
    # MicroDVD .sub resolves from the cache but can't be served to players.
    _seed_sub(client, "fileA", ext=".sub", text="{1}{50}hello")
    r = client.get("/api/subtitles/fileA")
    assert r.status_code == 404
    assert r.json() == {"error": "no_subtitles"}


def test_subtitles_disabled_returns_404(client):
    _seed_sub(client, "fileA")
    client.app.state.dc.cfg["subtitles"] = False
    r = client.get("/api/subtitles/fileA")
    assert r.status_code == 404
    assert r.json() == {"error": "subtitles_disabled"}


def test_ping_reachable_from_remote_without_token(make_client):
    # Discovery must answer even with remote access off, so a TV client can
    # find the server and tell the user to enable Remote Access.
    with make_client({"remote_access": False}) as c:
        r = c.get("/api/ping")
        assert r.status_code == 200
        body = r.json()
        assert body["app"] == "drivecast"
        assert body["remote"] is False
        assert "version" in body


def test_ping_local_ok(client):
    r = client.get("/api/ping")
    assert r.status_code == 200
    assert r.json()["app"] == "drivecast"


def test_subtitles_requires_token_for_remote(make_client):
    with make_client({"remote_access": True, "remote_token": "sekret"}) as c:
        r = c.get("/api/subtitles/fileA")
        assert r.status_code == 401


# ---- playlist (/api/playlist/{title}.m3u + JSON twin) / shuffle / stream activity ----

def _extinf_urls(body):
    """Just the stream-URL lines (skip #EXTM3U/#PLAYLIST/#EXTINF lines)."""
    return [l for l in body.splitlines() if l.startswith("http")]


def _file_ids(urls):
    # http://testserver/stream/<file_id>[?token=...]
    return [u.rsplit("/", 1)[-1].split("?", 1)[0] for u in urls]


def test_playlist_m3u_full_show_order_and_extras_excluded(client):
    r = client.get("/api/playlist/showB.m3u")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/x-mpegurl")
    body = r.text
    assert body.startswith("#EXTM3U")
    urls = _extinf_urls(body)
    assert _file_ids(urls) == ["fileE1", "fileE2", "fileE3"]  # declared order, extras skipped
    assert "fileX1" not in body
    assert "#EXTINF:" in body
    assert urls[0] == "http://testserver/stream/fileE1"


def test_playlist_m3u_start_crosses_season_boundary(client):
    r = client.get("/api/playlist/showB.m3u?start=fileE2")
    assert r.status_code == 200
    ids = _file_ids(_extinf_urls(r.text))
    assert ids == ["fileE2", "fileE3"]  # fileE1 dropped, fileE3 (season 2) kept


def test_playlist_m3u_unknown_start_returns_full_list(client):
    r = client.get("/api/playlist/showB.m3u?start=bogus")
    assert r.status_code == 200
    ids = _file_ids(_extinf_urls(r.text))
    assert ids == ["fileE1", "fileE2", "fileE3"]


def test_playlist_m3u_bakes_token_for_remote(make_client):
    with make_client({"remote_access": True, "remote_token": "sekret"}) as c:
        r = c.get("/api/playlist/showB.m3u?token=sekret")
        assert r.status_code == 200
        urls = _extinf_urls(r.text)
        assert urls
        assert all(u.endswith("?token=sekret") for u in urls)


def test_playlist_m3u_omits_token_when_local_and_unset(client):
    r = client.get("/api/playlist/showB.m3u")
    assert "token=" not in r.text


def test_playlist_m3u_shuffle_deterministic_and_seed_sensitive(client):
    r1 = client.get("/api/playlist/showB.m3u?shuffle=1&seed=42")
    r2 = client.get("/api/playlist/showB.m3u?shuffle=1&seed=42")
    assert r1.status_code == 200
    assert r1.text == r2.text  # same seed -> identical body
    ids = _file_ids(_extinf_urls(r1.text))
    assert set(ids) == {"fileE1", "fileE2", "fileE3"}
    assert len(ids) == 3  # each episode exactly once

    r3 = client.get("/api/playlist/showB.m3u?shuffle=1&seed=7")
    ids3 = _file_ids(_extinf_urls(r3.text))
    assert set(ids3) == set(ids)  # still every episode exactly once
    assert ids3 != ids            # a different seed changes the order


def test_playlist_m3u_shuffle_start_resumes_shuffled_suffix(client):
    # An up-next relaunch passes ?start=<next episode>; the server must slice the
    # SHUFFLED order at that episode (not replay from the top).
    full = _file_ids(_extinf_urls(client.get("/api/playlist/showB.m3u?shuffle=1&seed=42").text))
    assert len(full) == 3
    start = full[1]
    r = client.get(f"/api/playlist/showB.m3u?shuffle=1&seed=42&start={start}")
    assert _file_ids(_extinf_urls(r.text)) == full[1:]


def test_playlist_json_matches_m3u_order(client):
    m3u_ids = _file_ids(_extinf_urls(client.get("/api/playlist/showB.m3u").text))
    r = client.get("/api/playlist/showB")
    assert r.status_code == 200
    body = r.json()
    assert body["title_id"] == "showB"
    assert [it["file_id"] for it in body["items"]] == m3u_ids
    ep1 = next(it for it in body["items"] if it["file_id"] == "fileE1")
    assert ep1["name"] and ep1["duration_ms"] == 1500000


def test_playlist_unknown_title_404_both_routes(client):
    assert client.get("/api/playlist/nope.m3u").status_code == 404
    assert client.get("/api/playlist/nope").status_code == 404


def test_playlist_movie_single_entry(client):
    r = client.get("/api/playlist/movieA")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["file_id"] == "fileA"
    assert items[0]["duration_ms"] == 7200000

    r2 = client.get("/api/playlist/movieA.m3u")
    assert _file_ids(_extinf_urls(r2.text)) == ["fileA"]


def test_stream_recent_records_gets(client, monkeypatch):
    from starlette.responses import Response as StarletteResponse

    async def _fake_stream(file_id, request):
        return StarletteResponse(status_code=200)

    monkeypatch.setattr(client.app.state.dc.streamer, "stream", _fake_stream)
    client.get("/stream/fileE1")
    client.get("/stream/fileE2")
    r = client.get("/api/stream/recent")
    assert r.status_code == 200
    body = r.json()
    assert "now" in body
    ids = [it["file_id"] for it in body["items"]]
    assert ids[0] == "fileE2"  # most recent first
    assert "fileE1" in ids
    for it in body["items"]:
        assert "ts" in it and it["age"] >= 0


def test_stream_recent_requires_token_for_remote(make_client):
    with make_client({"remote_access": True, "remote_token": "sekret"}) as c:
        assert c.get("/api/stream/recent").status_code == 401


# ---- "Fix poster" picker endpoints ----

def test_poster_candidates_disabled_returns_empty(client):
    """With no TMDB key the picker still answers cleanly (empty list)."""
    r = client.get("/api/poster-candidates?title=Skyharbor&type=movie")
    assert r.status_code == 200
    assert r.json()["candidates"] == []


def test_poster_candidates_and_override(client, monkeypatch):
    """Candidate search returns the stubbed matches; an override persists to the
    store AND updates the matching in-memory library record live."""
    dc = client.app.state.dc
    dc.tmdb.enabled = True

    async def fake_candidates(title, media_type="movie", limit=6):
        return [{"tmdb_id": 55, "title": "Skyharbor", "year": "2016",
                 "poster_key": "arr.jpg", "overview": "aliens"}]

    async def fake_by_id(tmdb_id, media_type="movie"):
        return {"tmdb_id": tmdb_id, "title": "Skyharbor", "year": "2016",
                "poster_key": "arr55.jpg", "overview": "aliens", "genre_ids": [878]}

    saved = {}

    def fake_set_override(title, media_type, meta):
        saved.update(title=title, media_type=media_type, meta=meta)

    monkeypatch.setattr(dc.tmdb, "search_candidates", fake_candidates)
    monkeypatch.setattr(dc.tmdb, "by_id", fake_by_id)
    monkeypatch.setattr(dc.tmdb, "set_override", fake_set_override)

    r = client.get("/api/poster-candidates?title=Skyharbor&type=movie")
    assert r.status_code == 200
    assert r.json()["candidates"][0]["tmdb_id"] == 55

    r = client.post("/api/poster-override",
                    json={"title": "Skyharbor", "type": "movie", "tmdb_id": 55})
    assert r.status_code == 200
    body = r.json()
    assert body["poster_key"] == "arr55.jpg"
    assert body["updated"] == 1

    # Persisted to the override store, keyed by title + media type.
    assert saved["meta"]["poster_key"] == "arr55.jpg"
    assert saved["title"] == "Skyharbor" and saved["media_type"] == "movie"

    # The in-memory record reflects the pick without a rescan (Fire TV + web).
    rec = dc.library.get("movieA")
    assert rec["poster"] == "arr55.jpg"
    assert rec["tmdb_id"] == 55


def test_poster_override_bad_request(client):
    """Missing title or id is a 400 before any TMDB work."""
    r = client.post("/api/poster-override", json={"type": "movie", "tmdb_id": 5})
    assert r.status_code == 400
    r = client.post("/api/poster-override", json={"title": "Skyharbor", "type": "movie"})
    assert r.status_code == 400


def test_poster_override_tmdb_disabled(client):
    r = client.post("/api/poster-override",
                    json={"title": "Skyharbor", "type": "movie", "tmdb_id": 5})
    assert r.status_code == 400
    assert r.json()["error"] == "tmdb_disabled"


def test_poster_override_invalid_id(client, monkeypatch):
    """An id TMDB can't resolve is a 404 (and nothing is persisted)."""
    dc = client.app.state.dc
    dc.tmdb.enabled = True

    async def fake_by_id(tmdb_id, media_type="movie"):
        return None

    monkeypatch.setattr(dc.tmdb, "by_id", fake_by_id)
    r = client.post("/api/poster-override",
                    json={"title": "Skyharbor", "type": "movie", "tmdb_id": 999999})
    assert r.status_code == 404
