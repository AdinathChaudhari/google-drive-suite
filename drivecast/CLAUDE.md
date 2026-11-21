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

**Same show on several drives:** after `group_seasons`, `library.merge_shows_across_drives` (per tab bucket, before `attach_extras`) merges show records with the same `(naming.show_key_title(title), year)` — e.g. a clean nested `Show (2007)` on one drive and a torrent-named `Show (2007) Season 1-7 S01-S07 (...)` pack on another. Movies and other tabs never merge; a year-less record joins only if there is at most one candidate year; two drive-is-the-show records never merge. Merged id is `grp:`+sha1(`show|<key>|<year>`); metadata carries via transient `_member_ids` (popped by the scanner) — see `docs/DECISIONS.md` D-024.

A cache-only rebuild reports `total=0`, which is not "0 of 0 to scan" — both the web UI (`static/app.js` `pollScan`) and the menu bar (`drivecast_menubar.py` `_poll`) render "updating library…" for it instead of a progress fraction. New consumers of `/api/refresh/status` need the same guard.

## Subtitles (embedded-first — read before touching any of it)

The goal is "subtitles are always on". Most files already carry their own, so
the order is **look before you fetch**.

**Resolution order.** `/api/play` consults `data/sub_index.json` first. A
non-forced, non-SDH English **text** track means the player is simply told
which language to select (`prefer_lang="eng"` → mpv `--slang`, IINA
`--mpv-slang`, VLC `--sub-language`) and `resolve()` is skipped entirely — no
fetch, no quota, no timeout. Anything else falls through to `resolve()`:
cached sidecar → Drive sibling `.srt` → OpenSubtitles. Four things are
deliberately *not* coverage, and each one is a trap someone will want to
"fix": a **forced** track (a few foreign-language lines), a **hearing-impaired**
track (bracketed sound effects), an **`und`/untagged** track (`--slang` and
VLC's device setting both match the declared tag, so it would be a false
positive nothing rechecks), and an **image** track (see the config key below).
`sub_path` and `prefer_lang` are mutually exclusive *inside* each builder, not
by caller discipline — mpv prioritises an explicit `--sub-file` over a
`--slang` match, so sending both is a race with no upside.

**Infuse is excluded on purpose.** `infuse://` has no language or track
parameter, only `sub=`, so `/api/play` resolves the player kind *before* the
coverage decision and lets Infuse fall through to the sidecar path. Skipping
`resolve()` for it would hand it neither a sidecar nor a hint (D-025).

**One write point.** Every subtitle byte in the system passes through
`SubtitleResolver._accept`, which runs `sanitize_subtitle_bytes` and only then
writes. A new source cannot bypass the gate, because a source that does not
call `_accept` has no way into the cache. This exists because the ISP here
substitutes a 263-byte HTML block page for the subtitle host at HTTP 200, and
`cached()` trusts file existence forever — one substitution used to poison a
title permanently (D-030).

**Negative cache** (`data/subs_negative.json`, keyed `"<file_id>:<source>"`).
Poisoned verdicts — `html_block_page`, `ads_only_or_empty`, `zero_cue`,
`undecodable` — last **14 days**; `not_found` lasts **2 days**; transient
failures are **never recorded**, or one flaky listing would hide a real
subtitle. `?refresh=1`, `POST /api/subtitles/{id}/refresh` and the "Retry
failed subtitle lookups" Settings row all clear it.

**The sweep's scope guard is load-bearing.** The sweep probes bytes through
loopback `/stream`, which is live Drive egress, so it fires only after a scan
whose **final** scope was non-empty — never after `scope=[]`. Gate on the
post-merge `scope` local, not the caller's argument: a pending-scope drain can
escalate `[]` into a real walk, and a non-empty request can filter down to
nothing. The sweep also drops to serial while a `/stream` request has arrived
in the last 120 s, because it otherwise fights playback for the same uplink.

**The daily-budget backfill.** `subtitle_queue.json` holds the worklist
(anything without `has_text_en` *and* without an existing sidecar), tiered
continue-watching → unwatched movie → next episode of a show in progress →
everything else, FIFO inside a tier. `subtitle_daily_limit` defaults to 5
(anonymous OpenSubtitles). Three accounting rules that look arbitrary and are
not (all D-032):

1. A slot is charged only when a `/download` was really issued — `resolve()`
   often completes free from cache or a Drive sibling, and charging those made
   the queue spend its whole budget re-confirming finished files.
2. An item inside its negative-cache TTL is skipped free, and an item with 3
   real misses is retried weekly, not daily. Without both, a handful of
   genuinely-missing files hold the head of the queue forever.
3. A failure while the provider reports `remaining == 0` is a quota stop, not
   an item failure, so it is not blacklisted for the day — local midnight is
   5.5 h ahead of OpenSubtitles' UTC reset.

A backfill pass runs after each sweep and every
`subtitle_backfill_interval_hours` (default 6, `0` disables): the five-a-day
budget is a *daily* budget, and before that timer nothing scheduled a daily
run, so an app left running simply never spent it.

**Coverage numbers are unknown until the sweep has run.** With an empty index
every file reads as uncovered, so `swept()` gates the worklist and the
Settings panel says "run the subtitle scan first" rather than "0 of 2225 have
subtitles". Do not remove that gate: a stale `pending` list on disk from
before a sweep is exactly the case it also guards.

**Image-only English** (`hdmv_pgs_subtitle`/`dvd_subtitle`) is ~27% of the
sampled files and is **not** treated as coverage yet — `--slang` would
select it and these players are documented to render it, but that is
unverified on this hardware and a wrong "covered" is unrecoverable. Flip
`prefer_embedded_image_subtitles` to `True` after the device test; it is a
config key precisely so that is not a code change (D-022).

**The Fire TV is the weak path.** Its only mechanism is
`GET /api/subtitles/{file_id}`, which serves a sidecar file. That endpoint now
skips the OpenSubtitles step for embedded-covered files so TV probes stop
draining the same 5/day the queue needs — but it cannot *select* an embedded
track, and extracting one server-side reads 100% of the file (measured, D-026).
On the TV, embedded subtitles come from VLC's own **Settings → Subtitles →
Preferred subtitle language = English**. The web `<video>` player has never had
subtitles and still does not.

## Conventions & gotchas
- Pure classifier functions do no I/O — they operate on the walked node trees the scanner builds; keep them side-effect-free (plugins get the same shapes).
- Read-only on Drive: only the local cache (`library.json`, `data/scan_cache.json`, posters, `data/history.json`) is written.
- `/api/media-info/{id}` = cached ffprobe + VLC hardware-decode policy (`media_info.py`, D-028); INFO logs (per-request stream stats) go to `data/drivecast.log`.
- rclone config must be **unencrypted** (token is read non-interactively); default remote name `gdrive1`.
- Local playback targets: mpv/IINA/VLC (position-tracked) + Infuse (launch-only via infuse:// URL scheme — no resume/watched/autoplay; never auto-picked).
