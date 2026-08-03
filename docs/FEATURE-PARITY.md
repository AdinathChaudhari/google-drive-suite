# Feature Parity — Web vs. Fire TV

What each Drivecast client can do, what is deliberately different, and the
canonical vocabulary both must speak. **This document IS the parity
mechanism** — there is no server endpoint enforcing sort/group parity, the
same way there is no shared runtime enforcing SeededShuffle parity (that one
is held by hand-mirroring `ui/player/SeededShuffle.kt` against the server's
shuffle, documented in `drivecast-app/CLAUDE.md`). A Kotlin client and a JS
client cannot share code, so they share this file instead.

**Maintenance rule**: update this file in the *same commit* as any change to
sort/group vocabulary, refresh behavior, or a client-only feature. A stale
row here is worse than no row.

**Citation style — symbols, not lines**: this file tracks the *current*
behavior of code that keeps changing, so every "Where" cites `path/file.ext`
plus the enclosing **symbol** (function/class/const), e.g. `app.js` §
`groupItems` or `HomeViewModel.kt` § `rescan()` — never a line number. A line
number is accurate the day it's written and wrong the next time anyone
touches the file above it; a symbol name only rots when the symbol itself is
renamed or deleted, which is exactly the moment this doc *should* get
re-checked. Before citing a symbol, grep for it — a citation naming a symbol
that isn't in the file is the same defect as a rotted line number. Where a
claim is about a specific constant (a poll interval, a cap, a threshold),
cite the symbol *and* quote the value, so a reader can check it without a
line number. This is a deliberate departure from `DECISIONS.md`, which
stays `file:line`: that file is append-only and each entry is a snapshot of
*why a value was that value at the time the decision was made* — a past
fact that doesn't move. This file describes what the code does *right now*,
which does move, so it needs a citation style that survives the code moving
under it.

**Last verified**: 2026-08-03, against the working tree at commit `5470b33`
plus the uncommitted sort/group-vocabulary + TV-rescan changes described in
§8 (`git status` at verification time showed those files modified/added, not
yet committed).

---

## 1. The three clients

| Client | Role | Transport | UI state persists in |
|---|---|---|---|
| **Web UI** (`drivecast/drivecast/static/app.js` + `index.html`) | Browser-based library browser/manager; also serves Settings, remote-pairing pages | Same-origin `fetch()` against the FastAPI app | `localStorage` — keys prefixed `dc.*` (`dc.sort`, `dc.sortAsc`, `dc.group`, `dc.section`, …) |
| **Fire TV app** (`drivecast-app`, Kotlin/Compose for TV) | Couch playback client; pairs to a server URL + token | Retrofit/OkHttp over LAN, token in query param (`api/DrivecastApi.kt`) | Jetpack DataStore, `ServerConfigStore.kt` § `ServerConfigStore.Keys` (`home_sort_key`, `home_sort_ascending`, `home_group_key`) |
| **macOS menu bar** (`drivecast/drivecast_menubar.py`) | Drive-selection + refresh-status tray icon; no library browsing UI | Same-process `rumps.Timer` polling `127.0.0.1` (`drivecast_menubar.py` § `_on_tick`, spawning `_poll`) | None — no sort/group concept, it doesn't render tiles |

---

## 2. Canonical sort vocabulary (source of truth)

| Canonical name | Web id (`dc.sort`) | TV id (`home_sort_key`) | Default direction | Nulls |
|---|---|---|---|---|
| Recently added | `added` | `recent` | descending | last, both directions |
| A–Z (title) | `title` | `title` | ascending | n/a — `displayTitle` always resolves ("Untitled" fallback) |
| Year | `year` | `year` | descending | last, both directions |
| Recently watched | `watched` | `watched` | descending | never-watched last, both directions |

Rules:

1. **Every key is direction-toggleable on both clients.** TV: `nextSortSpec` flips
   `ascending` when you re-pick the active key (`SortAndGroup.kt` § `nextSortSpec`). Web:
   a dedicated `#sortDirBtn` button (`index.html` § `#sortDirBtn`) flips `state.sortAsc`
   in its click handler (`app.js` § `$("sortDirBtn").addEventListener("click", …)`) —
   re-selecting a `<select>`'s current `<option>` fires
   no `change` event, so the web can't use the TV's "re-pick to flip" idiom.
2. **Nulls sort last in both directions**, on both clients. Web:
   `cmpNullsLast` (`app.js` § `cmpNullsLast`) returns the null side as *greater* before
   the direction multiplier is ever applied. TV: `nullsLastBy`
   (`SortAndGroup.kt` § `nullsLastBy`) does the same — direction only touches the
   non-null branch. This replaced a bug: the web used to coerce with
   `(b.year || 0)`, which was harmless descending but put every year-less
   title at the *top* once you flipped to ascending.
3. **Tiebreak is always title-ascending**, regardless of the active
   direction. Web: `|| byTitle(a, b)` (`app.js` § `sortItems`, the `byTitle`
   tiebreak). TV:
   `.then(titleTiebreak)` (`SortAndGroup.kt` § `comparatorFor`).
4. **Ids differ per client for storage-compat reasons** (`recent` vs `added`,
   etc.) — the canonical *name* in the table above is the contract; the ids
   are private to each client's persisted prefs.

> **Deliberate — defaults differ by device.** TV defaults to **Recently
> added** (`SortKey.RECENT`, `SortAndGroup.kt` § `SortKey`) — browsing what's new from
> the couch. Web defaults to **A–Z** (`SORT_DEFAULT_ASC.title`, and no stored
> `dc.sort` falls back to `"title"` — see `restoreControls`, `app.js` § `restoreControls`)
> — managing a library at a desk. Do not "fix" this into agreement; see §5.4.

> **Deliberate — direction-flip idiom differs by input device, same
> semantics.** TV: re-pick the active key in the Sort overlay (a D-pad SELECT
> on an already-focused row is cheap; `SortGroupOverlay.kt` § `SortGroupOverlayHost`,
> the `SORT` branch). Web: an
> explicit `↑`/`↓` button, because a `<select>` fires no `change` event when
> you choose its current value. Picking a genuinely *new* key resets to that
> key's own default direction on both clients (`nextSortSpec`'s else-branch;
> web's `sortSel` change handler, `app.js` § `$("sortSel").addEventListener("change", …)`,
> which calls `defaultAscFor`).

---

## 3. Canonical group vocabulary

| Canonical name | Web (`dc.group`) | TV (`home_group_key`) | Semantics |
|---|---|---|---|
| None | `none` | `NONE` | Flat grid. Web non-entertainment tabs (Courses, Podcasts) auto-shelve by `rec.shelf` **only as the default** — the guard is `state.group === "none"` (`app.js` § `groupItems`), so auto-shelf applies until the user explicitly picks `type`/`category`/`drive`, at which point that pick applies there too, same as an entertainment tab. See §5.2 and §5.7. |
| Type | `type` | `TYPE` | Structural movie-vs-show split. Web: `isShowRec(r)` — `r.type === "show" \|\| (r.seasons && r.seasons.length)` (`app.js` § `isShowRec`) — the structural twin of the TV's `Title.isShow`. TV: `it.isShow` (`SortAndGroup.kt` § `buildGridRows`, the `TYPE` branch; `Title.isShow` itself lives in `Models.kt`). Both clients now answer the movie-vs-show question with the exact same structural test, so a `type: null` record with seasons lands in TV Shows on both clients, never Movies on one and TV Shows on the other — the two UIs can't silently disagree about which bucket a title is in. Nothing is ever dropped — every record structurally falls into exactly one bucket. |
| Category | `category` | `CATEGORY` | TMDB-derived (`categoryOf`), falling back to the structural type when TMDB data is absent. Web buckets Movies/TV Shows/Documentaries/Other (`app.js` § `groupItems`, the `category` branch); TV has no Documentaries bucket and folds unknown categories into Other in `buildGridRows`'s `CATEGORY` branch (`KNOWN_CATEGORIES` at `SortAndGroup.kt` § `KNOWN_CATEGORIES` — `setOf("movie", "show")` — folded in `SortAndGroup.kt` § `buildGridRows`). |
| Drive | `drive` | *(none)* | **Web-only.** See §5.1. |

Shared rules:

- Empty groups are omitted from the rendered result on both clients (web:
  the `if (movies.length) …` / `map[cat]` guards in `app.js` § `groupItems`; TV:
  `buckets[k]?.let { … }` in `SortAndGroup.kt` § `buildGridRows`).
- Within-group order is the active sort order (grouping is a stable
  partition over an already-sorted list on both clients).
- Group headers are never focusable on TV (`HomeScreen.kt` § `GroupHeader`,
  "deliberately NOT focusable") and render as plain `<h3>`-equivalent chrome
  on web — neither client lets a D-pad/click focus land on a header.
- Grouping is suppressed on TV whenever a specific category chip narrows the
  tab **or the tab is not an entertainment-behaviour tab**:
  `effectiveGroupFor` returns `GroupKey.NONE` unless `isEntertainment &&
  selectedCat == null` (`SortAndGroup.kt` § `effectiveGroupFor`). The chip case exists
  because grouping-by-category on top of a category filter would render
  exactly one header repeating the chip's own label. The non-entertainment
  case exists because those tabs have no category vocabulary at all — see the
  §5.7 divergence row. The Group pill itself is only rendered inside the
  `isEntertainment` branch of the controls row, so non-entertainment tabs
  don't even show it (`HomeScreen.kt` § `HomeContent`, the controls row). **Web does not suppress either
  case** — chips and grouping coexist there, and every web tab (entertainment
  or not) exposes the group selector. Logged as minor, known divergences, not
  something to "fix" without a decision — the web's chip filter and group
  selector are independent controls in that UI and users have not asked for
  the TV's behavior.

---

## 4. Feature parity matrix

### On all clients
| Feature | Web | TV | Menubar | Where | Notes |
|---|---|---|---|---|---|
| Library browse (tabs/sections) | yes | yes | n/a (tray only) | `app.js` § `renderTabs`, `app.js` § `renderLibrary`; `HomeScreen.kt` § `HomeContent` (tab-bar `LazyRow`) | Both driven by `GET /api/sections` + `GET /api/library` |
| Continue watching | yes | yes | — | `app.js` § `loadContinue`; `HomeViewModel.kt` § `load()` | |
| Watched-map progress | yes | yes | — | `app.js` § `ensureWatchedMap` (`GET /api/watched-map`); `HomeViewModel.kt` § `load()`, `LibraryRepository.kt` § `watchedMap()` | |
| Full rescan trigger | yes | yes (this round) | yes | `app.js` § `triggerRefresh` (`POST /api/refresh`); `HomeViewModel.kt` § `rescan()`; menubar's refresh menu item | TV previously only re-fetched the cache (`refresh()` → `GET /api/library`); `rescan()` now calls the real endpoint |
| Live scan status, incl. total=0 contract | yes | yes (this round) | yes | `app.js` § `pollScan`; `ScanStatus.kt` § `scanStatusLabel`; menubar § `_poll` | See §6 |
| Sort/group per canonical vocabulary | yes | yes | n/a | §2–3 | |
| VLC / external player handoff | yes | yes | — | `app.js` § `launch` (`res.player === "vlc"`); `PlayerScreen.kt` § `VlcPlayerHost` (`ACTION_VIEW` + `VLC_PACKAGE`) | |
| Keep-awake prompt | yes | yes | — | `app.js` § `awakeWatch`/`pollAwake` (`api/awake/*`); `KeepAwakeController.kt` § `poll()`/`extend()`/`release()` | |

### Server/web-only
| Feature | Where (server route) | Notes |
|---|---|---|
| Advanced browse (`/api/browse`) | `server.py` § `api_browse` | Not in `DrivecastApi.kt` |
| Search (`/api/search`) | `server.py` § `api_search` | Not in `DrivecastApi.kt` |
| Drive listing + group-by-drive (`/api/drives`) | `server.py` § `api_drives` | See §5.1 |
| Full settings read (`GET /api/settings`) | `server.py` § `api_get_settings` | TV only has `POST api/settings` (tabs edit), never reads the full settings document |
| Per-drive refresh scope (`POST /api/refresh {"drives":[...]}`) | `server.py` § `api_refresh` | See [D-013](DECISIONS.md#d-013--selected-drive-order-is-data-not-presentation) / §5.5 |
| Poster enrich/override (`/api/enrich`, `/api/poster-candidates`, `/api/poster-override`) | `server.py` § `api_enrich` / `api_poster_candidates` / `api_poster_override` | |
| Remote pairing pages (`/api/remote/qr`, `/api/remote/ca`) | `server.py` § `api_remote_qr` / `api_remote_ca` | TV pairs by typing/scanning the URL once in `SetupScreen.kt`, not by rendering these pages |
| Local desktop players (`POST /api/play`) | `server.py` § `api_play` | TV always hands off to VLC/ExoPlayer, never a server-local player |
| `.m3u` playlist export (`/api/playlist/{id}.m3u`) | `server.py` § `api_playlist_m3u` | TV uses the JSON twin, `GET /api/playlist/{id}` (`DrivecastApi.kt` § `playlist`), for the same episode order |
| Courses materials, progress rings, auto-shelf grouping | `courses.py`; `app.js` shelf-grouping (`app.js` § `groupItems`) | See §5.2 |

**Every route in this table is one the TV genuinely does not call** —
confirmed by reading `DrivecastApi.kt` in full (16 endpoints: `remote`,
`library`, `sections`, `updateSettings`, `title`, `continueWatching`,
`removeContinue`, `watchedMap`, `progress`, `awakeStatus/Extend/Release`,
`playlist`, `streamRecent`, `startRefresh`, `refreshStatus`). Silence here is
deliberate, not an oversight.

### TV-only
| Feature | Where | Notes |
|---|---|---|
| Seeded-shuffle local playback queue | `ui/player/SeededShuffle.kt` | Byte-parity with the server's shuffle, held by hand (see `drivecast-app/CLAUDE.md` "SeededShuffle parity") |
| LAN auto-rediscovery of a moved server | `LibraryRepository.kt` § `withAutoRediscover` | Web has no equivalent — a browser tab just breaks and the user re-navigates |
| Tab rename/reorder from a TV Settings screen | `DrivecastApi.updateSettings`, `LibraryRepository.saveTabs` | Web has richer settings (drive assignment, tab creation) that the TV doesn't expose |
| D-pad focus-lane machinery (`tvFocusEnterFallback`, `tvDpadHop`, deterministic UP/DOWN hops) | `ui/common/FocusKit.kt`; `ui/home/FocusLanes.kt` | No web equivalent — a mouse/keyboard has no D-pad focus-search problem |

---

## 5. Deliberate divergences

### 5.1 Group-by-drive is web-only
The user's TV tabs are already drive-shaped (MCU / DCU / Star Wars / James
Bond) — a drive grouping *inside* one of those tabs would mostly produce one
group. More fundamentally, the app has no drive-name source: it never calls
`GET /api/drives` (absent from `DrivecastApi.kt`), so there is nothing to
label a drive group with. Do not add drive grouping to the TV. Do not remove
it from web (`index.html` § `#groupSel`, the "By drive" `<option>`; applied in
`app.js` § `groupItems`'s `drive` branch). **Revisit
if** the TV ever gains a reason to consume `/api/drives` for some other
feature — at that point drive grouping becomes cheap to add.

### 5.2 Courses are web-only
Course materials, progress rings, and the auto-shelf grouping for
non-entertainment tabs (`app.js` § `groupItems`, keyed off `rec.shelf`) exist only
on web. The Courses *tab* still shows on the TV (it renders via the same
generic section/tile machinery as any other tab) — hiding it was explicitly
considered and rejected as a separate, unrequested change. Only the
courses-specific chrome (materials list, progress rings, shelf grouping) is
missing from the TV.

### 5.3 Differing sort defaults
TV: Recently added. Web: A–Z. See the callout in §2 — recorded so neither
default gets "corrected" to match the other.

### 5.4 Direction-flip idiom differs
D-pad re-pick vs. an explicit button. See the callout in §2.

### 5.5 TV rescan is full-scan only
`POST /api/refresh` supports an optional `{"drives": [...]}` body to scope a
refresh to specific drives (`server.py` § `api_refresh`), and the web/menubar use
that for per-drive refresh. `DrivecastApi.startRefresh()` sends a bodyless
POST (`DrivecastApi.kt` § `startRefresh`), which the server treats as `scope=None` — a
full walk of every selected drive. Drive *order* (not just membership) feeds
that walk and the cross-drive merge downstream — see
[D-013](DECISIONS.md#d-013--selected-drive-order-is-data-not-presentation)
and `drivecast/CLAUDE.md`'s "Refresh scoping (three modes)" section.
Per-drive refresh scope from the TV was explicitly out of scope for this
round.

### 5.6 Category buckets differ
Web has a Documentaries bucket (`app.js` § `groupItems`, the `category`
branch); TV folds anything outside
`{movie, show}` into Other (`buildGridRows`'s `CATEGORY` branch,
`SortAndGroup.kt` § `buildGridRows`; `KNOWN_CATEGORIES` defined in `SortAndGroup.kt` § `KNOWN_CATEGORIES` — `setOf("movie", "show")`).
Both derive from the same `categoryOf()`-style TMDB category field — the TV
simply doesn't carry a fifth bucket in its UI yet.

### 5.7 TV grouping is entertainment-only
TV's `effectiveGroupFor` returns `GroupKey.NONE` for every non-entertainment
tab, and the Group pill itself is only rendered when `isEntertainment`
(`SortAndGroup.kt` § `effectiveGroupFor`, `HomeScreen.kt` § `HomeContent`, the controls row) — those tabs (Courses,
Podcasts) have no TMDB category field to group by, so there is nothing for
`GroupKey.CATEGORY` to bucket on and `GroupKey.TYPE`'s movie/show split
doesn't apply either. Web has no such restriction: the group selector is
unconditional on every tab, and non-entertainment tabs fall back to the
`rec.shelf` auto-grouping described in §3/§5.2 only while the selector is
still at its `none` default (`app.js` § `groupItems`) — an explicit `type`/`category`/
`drive` pick applies there exactly as it would on an entertainment tab. Not
something to "fix" — the TV simply has no vocabulary to offer on those tabs.

---

## 6. Rescan / refresh status contract

**`POST /api/refresh`** (`server.py` § `api_refresh`):
- 503 `{"error": "setup", "message": ...}` — server not configured
  (`state.setup_error`).
- 400 `{"error": "no_drives", "message": "No drives selected. Pick drives in
  Settings."}` — no drives selected.
- 200 `{"started": false, "running": true}` — a scan is already in flight;
  the caller should just start/rejoin the poll, not treat it as a rejection.
- 200 `{"started": true, "running": true, "scope": [...]}` — scan actually
  (re)started.
- Bodyless/empty-JSON POST = full refresh (`scope=None`) — this is what the
  TV always sends and what the menubar's simple "Refresh" relies on.

**`GET /api/refresh/status`** (`server.py` § `api_refresh_status`, mirrors
`Scanner.status` — the dict initialized in `library.py` § `Scanner` (`self.status` init)): `running`, `scanned`, `total`,
`added`, `removed`, `error`, `warning`, `scope`, plus a server-computed
`scope_names` (drive names resolved from `scope`, added in
`server.py` § `api_refresh_status`).

**The rule every consumer must honor**: `total == 0` while `running` means a
cache-only rebuild (walked nothing, just re-derived `library.json` from
`data/scan_cache.json`) — render **"Updating library…"**, never a `0/0`
fraction. Current honorers:

| Consumer | Where |
|---|---|
| Web | `app.js` § `pollScan` (`!st.total ? "Updating library…" : ...`) |
| Menu bar | `drivecast_menubar.py` § `_poll` (same `total == 0` check) |
| TV | `ScanStatus.kt` § `scanStatusLabel` (`st.total == 0 -> "Updating library…"` checked first) |

`status.warning` (`library.py` § `Scanner.scan`, set via `self.status["warning"] =`) is surfaced by **neither** client —
that's parity, not a gap.

**Poll cadence**:

| Client | Interval | Caps |
|---|---|---|
| Web | 1200 ms (`app.js` § `startScanWatch` — `setInterval(pollScan, 1200)`) | Uncapped — runs until `running` goes false |
| TV | 1200 ms (`HomeViewModel.kt` § `startPolling` — `delay(1_200)`) | 10 consecutive failures (~12 s) → give up with a notice (`startPolling`'s `failures >= 10` check); 30-minute wall-clock cap (`HomeViewModel.kt` § `startPolling` — `deadlineMs = scanStartedAtMs + 30L * 60 * 1000`); paused on `ON_STOP` / resumed on `ON_START` (`HomeScreen.kt` § `HomeScreen`, the `LifecycleEventObserver`) so a backgrounded app doesn't leak a poll loop |
| Menu bar | 5 s (`rumps.Timer(self._on_tick, 5.0)`, `drivecast_menubar.py` § `_on_tick`) | Runs for the life of the tray app |

---

## 7. How to add a feature without creating drift

1. Decide the tier — **all-clients** or **deliberately single-client** —
   *before* building anything.
2. All-clients: add the row to §4 here first, then implement web + TV in the
   same round. Don't ship one client's half and call it done.
3. Single-client: add it to §5 *with the reason*, not just the fact.
4. Pure logic gets a real test: TV follows the `SortAndGroupTest.kt` /
   `FocusLanesTest.kt` pattern (unit tests over pure functions, no Compose
   harness); web gets at minimum `node --check app.js` plus a manual
   checklist — there's no JS test runner in this repo yet.
5. New tuning *values* (timeouts, caps, concurrency numbers) go to
   `docs/DECISIONS.md`, not here. This file records **what** differs between
   clients; DECISIONS.md records **why** a number is what it is.
6. Cite symbols, not lines. A "Where" cell is `path/file.ext` § `symbolName`
   (function/class/const), never `file:line` — grep the symbol before you
   write it down, and if the claim is about a tuned value, quote the value
   next to the symbol. See the citation-style note at the top of this file.

---

## 8. Revision log

- **2026-08-03** — Initial version. Documents the round that added: TV —
  real server rescan via `POST /api/refresh` + status polling
  (`DrivecastApi.kt`, `Models.kt`, `LibraryRepository.kt`, `ScanStatus.kt`,
  `HomeViewModel.kt`, `HomeScreen.kt`), `SortKey.WATCHED` and `GroupKey.TYPE`
  (`SortAndGroup.kt`); Web — direction-toggle button, nulls-last-in-both-
  directions fix (replacing the `(b.year || 0)` coercion bug), `Category`
  group mode, and a `type` grouping fix so null-type records land in Movies
  instead of vanishing (`app.js`, `index.html`). Changes were present in the
  working tree, not yet committed, at verification time.
