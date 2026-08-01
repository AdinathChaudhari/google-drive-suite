## Drivecast ecosystem (shared)

One of three sibling repos that make up **Drivecast**, a self-hosted media system:
- **drivecast/** — Python/FastAPI media server + vanilla-JS web UI. Scans Google Drive, classifies content into "sections" (tabs), serves streams/playlists. Tests: `venv/bin/python -m pytest drivecast/ -q`.
- **drivecast-app/** — Kotlin/Jetpack Compose **Fire TV** client; server-driven UI.
- **drive-offload/** — Python uploader/renamer + storage tooling that gets media onto Drive. (Sections/tabs do NOT live here.)

Environment:
- App build: no JDK on PATH — `JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home`; Android SDK at `/opt/homebrew/share/android-commandlinetools`.
- Fire TV Stick: `adb connect <fire-tv-ip>:5555`. VLC (`org.videolan.vlc`) is the default playback target since app v0.3.0 (stick rejects HEVC 10-bit in stock ExoPlayer).
- Server auth: `?token=` query param on every remote request.

# Drivecast (server)

Local web app that streams video/audio straight from Google **Shared Drives** — no downloads. It scans drives once, caches a structured catalogue to `library.json`, presents movie/show/course/podcast tiles, and proxies bytes on demand via a Range-aware endpoint. Strictly **read-only** on Drive.

## Stack
- **FastAPI** + **uvicorn** (127.0.0.1:8737), **httpx** (streaming proxy, HTTP/2 when optional `h2` is present).
- Frontend is **vanilla JS/HTML/CSS** in `drivecast/static/` (`app.js`, `index.html`, `style.css`) — no framework/build step.
- **rclone** is used only as the OAuth **token authority**; all browse/search/stream go directly against the Google Drive v3 API with the Bearer token.
- Optional macOS **menu-bar app** via `rumps`; standalone `.app` built with `py2app` (`requirements-dev.txt`, `setup_app.py`).

## Layout & entry points
- `app.py` — entry point: preflight-checks rclone, starts uvicorn, opens browser (`DRIVECAST_NO_BROWSER=1` to skip).
- `drivecast/server.py` — `create_app()` + all routes (`/stream/{file_id}` range proxy, `/api/library`, `/api/refresh`, `/api/sections`, `/api/play`, `/api/continue`, `/api/watched-map`, `/api/remote*`, …) and the token/remote-access middleware.
- `drivecast/library.py` — scanner, entertainment classifiers, library diff/persistence, and the section-dispatch (`Library._classify`).
- `drivecast/sections.py` — section model + plugin loader. `courses.py` / `playlists.py` — the Courses / Podcasts classifiers.
- Other modules: `drive_api.py`, `streaming.py`, `tmdb.py`, `history.py`, `subtitles.py`, `player.py`, `rclone_auth.py`, `scan_cache.py`, `config.py`.

## Running & testing
- Setup: `python3 -m venv venv && ./venv/bin/pip install -r requirements.txt`.
- Run: `./venv/bin/python app.py` (menu-bar: `./venv/bin/python drivecast_menubar.py`).
- Tests: `./venv/bin/python -m pytest drivecast/ -q` (11 `test_*.py` files; `conftest.py` stubs out the user plugin dir so real plugins never leak into runs).

## Sections architecture
- A *section* is a top-level tab; each selected drive maps to exactly one via the `drive_sections` config map (unassigned → entertainment).
- `BUILTIN_SECTIONS = ("entertainment", "courses", "podcasts")` in `sections.py`, each with its own mimes, accent colour, and vocabulary (`_BUILTIN_META`).
- **Custom private sections**: drop a plugin `.py` defining a `SECTION` dict into `~/Library/Application Support/drivecast/sections/` (outside the repo). `sections.py` lazily loads them; a broken plugin logs and is skipped, never crashes the app.
- Dispatch: `Library._classify` routes courses→`courses.classify_course_drive`, podcasts→`playlists.classify_playlist_drive`, plugins→their `classify` fn, else falls through to the built-in entertainment classifiers.
- Design doc: `SECTIONS_DESIGN.md`.

## Refresh scoping (three modes — don't collapse them)
`AppState.start_refresh(scope=…)` / `Scanner.scan(..., scope=…)` distinguish:
- `scope=None` — full refresh, walk every selected drive.
- `scope=[ids…]` — walk only those drives (per-drive refresh). The library is **always** rebuilt from the cached records of *all* selected drives, so cross-drive show merges stay correct.
- `scope=[]` (explicit empty) — **cache-only rebuild**: walk nothing, re-derive `library.json` from `data/scan_cache.json`. This is how a deselected drive's titles leave without touching Drive.

A non-empty scope that filters down to nothing (every named drive is unselected) returns `False` and starts nothing — it must NOT silently downgrade into the cache-only rebuild. The one exception is the mid-scan drain (`_drain_pending_scope`, `_draining=True`), where "nothing left selected" *is* the removal the queued rebuild exists to apply.

`POST /api/settings` scopes to `added_drives ∪ section_changed` (a union — the Settings UI sends an add and its tab assignment in ONE request, and an `if/elif` here silently dropped the add). Removals and a pure **reorder** both fall through to the cache-only rebuild: drive order IS meaningful, because `Scanner.scan` builds `all_records` with `for drive_id in selected` and `group_seasons` merges same-named seasons across drives first-seen-wins — see `docs/DECISIONS.md` D-013. Never revert any of this to full-replace-only scans.

A cache-only rebuild reports `total=0`, which is not "0 of 0 to scan" — both the web UI (`static/app.js` `pollScan`) and the menu bar (`drivecast_menubar.py` `_poll`) render "updating library…" for it instead of a progress fraction. New consumers of `/api/refresh/status` need the same guard.

## Conventions & gotchas
- Pure classifier functions do no I/O — they operate on the walked node trees the scanner builds; keep them side-effect-free (plugins get the same shapes).
- Read-only on Drive: only the local cache (`library.json`, `data/scan_cache.json`, posters, `data/history.json`) is written.
- rclone config must be **unencrypted** (token is read non-interactively); default remote name `gdrive1`.
- Local playback targets: mpv/IINA/VLC (position-tracked) + Infuse (launch-only via infuse:// URL scheme — no resume/watched/autoplay; never auto-picked).
