# drivecast

Stream video (and audio) straight from your Google **Shared Drives** — no
downloads, no syncing. drivecast is a tiny local web app that presents your
Shared Drives as a **cached media library** — movie and TV-show tiles with
pre-fetched posters, seasons and episodes — and plays files in mpv / IINA / VLC / Infuse
while proxying the bytes on demand. Nothing is ever written to disk.

> 📖 **[Read the case study](CASE-STUDY.md)** — how this was designed, built,
> hardened and shipped, almost entirely through orchestrated AI agents.

**Tabs & behaviors.** You organise the app into **tabs** you create yourself —
there are **no tabs by default**. In **Settings**, pick a drive's category from
the dropdown or choose **＋ New tab…** to make one: give it a **name** and an
**emoji**, and choose how it **behaves** (which built-in content-type learning
it reuses). A tab is just data; the *behavior* is the reusable engine:

- 🍿 **Movies & TV** (`entertainment`) — movies & TV shows, with category chips
  (Movies / TV Shows / Documentaries / Other) derived from TMDB genres.
- 🎓 **Course** (`courses`) — a course drive becomes courses with **Modules**
  and **Lessons**: numbered lesson files are ordered correctly, module folders
  become a module picker, workbook PDFs appear as **Materials**, tiles carry a
  **progress ring**, and **Resume course** continues from your first unwatched
  lesson. *Any* tab you point at this behavior gets the whole course-structuring
  learning — so a "Work Courses" tab and a "Personal Courses" tab both work alike.
- 🎙 **Podcast** (`podcasts`) — each folder on the drive (e.g. YouTube
  downloads) becomes a channel tile with its episodes; audio streams to mpv
  exactly like video, with resume.

So you can make a personal **Home Videos 🎥** tab (behaves like *Movies & TV*),
point a drive at it, and its titles get their own tab with posters and chips —
no code, no dependence on the built-in names.

**Custom private behaviors.** For a bespoke classifier, drop a plugin `.py` into
`~/Library/Application Support/drivecast/sections/` (same private home as your
secrets, never part of the repo); it registers a new **behavior** that your tabs
can then adopt — see the docstring in `drivecast/sections.py` and
`TABS_REFACTOR.md` for the model and the tiny plugin contract.

**Upgrading?** Existing setups migrate automatically: whatever you'd assigned
(Entertainment / Courses / Podcasts / a private plugin) is seeded as matching
tabs on first launch, so your library looks exactly the same — nothing to redo.

**Library model.** You pick which Shared Drives to include (Settings, or the
menu-bar app). drivecast scans those drives **once** and caches a structured
catalogue to `library.json`. From then on, normal browsing is instant and
hits the Google API **zero times** — tiles, seasons, episodes and posters all
come off disk. A **Refresh** (manual or on launch) rescans and diffs: new titles
are added (and their posters fetched), deleted titles are removed (and their
orphaned posters pruned), and show episode lists are updated.

**Per-drive refresh.** You usually know which drive you just uploaded to, so
you don't have to rescan everything: hover a drive in **Settings** for its ⟳
button, or use the menu-bar **Refresh one drive** submenu (the header ⟳ stays
a full refresh). Under the hood every scan stores each drive's raw records in
`data/scan_cache.json` and the library is rebuilt from the cache of **all**
selected drives — so shows spanning two drives ("Part 1"/"Part 2") stay merged
correctly no matter which drive you refresh. A drive whose scan errors keeps
its previous titles instead of vanishing. The raw folder-browser is still
available behind a demoted **Browse files** link.

**Collection folders.** The scan recurses *into* folder trees, so a collection
folder (`Phase 1`, `Classics`, `Trilogy Box`, `Saga Collection`, …) that
holds many films — as loose files or one-movie subfolders, possibly nested —
surfaces **each film as its own tile** rather than one wrong tile named after the
folder. Bonus-material subfolders (`Featurettes`, `Extras`, `Behind the Scenes`,
`Special Features`, …) become a movie's **Extras** — a list of bonus clips shown
below the Play button, just like a show's featurette pseudo-seasons (a collection's
*shared* bonus folder appears on every film in it). A leading enumeration prefix
(`01) `, `01.`, `1 - `) is stripped from titles. TV shows (season subfolders or
episode-marked files) are still detected and kept as a single show tile.

**Featurettes & extras stay inside their show.** Bonus-material folders
(`Featurettes`, `Extras`, `Bonus`, `Behind the Scenes`, `Deleted Scenes`,
`Trailers`) never become their own tile. Wherever one sits — inside a show
folder, beside bare `Season N` folders at a drive root, or inside a
`<Show> Season N` folder — its videos fold into that show as labelled
pseudo-seasons listed at the end of the season picker (`Featurettes`,
`Featurettes · Season 2`, …). They play like episodes but stay out of the
season count and Shuffle. Junk folders (`Sample(s)`, `Subs`, `Subtitles`) are
dropped outright. One guard: a root extras folder on a drive with no single
owning show keeps its own tile rather than guessing the wrong owner.

**Robust season detection.** Season subfolders survive messy release naming.
Beyond `Season 1` / `S01`, drivecast de-noises a folder name (dropping bracketed
groups and quality tokens) and reads a *leading* `S<number>`, so real-world
folders like `S01 (2017) 1080p 10bit HEVC NF WEBRip x265 [ENGLISH - SPANISH]`
and `S05 Part 1 (2021) …` group correctly (a five-season show ripped that way
still lands as one tile). The short form is anchored to the start, so a title that merely
contains an S-number mid-string is never mistaken for a season.

**Quality pills.** Each tile shows a small pill with the video quality parsed
from the filename — `4K`, `1080p`, `720p` or `SD` (with an optional `HDR` / `DV`
suffix). Movies show their file's quality; a show tile shows the **best**
quality available across its episodes. The pill also appears on the detail view.

> drivecast is strictly **read-only** on Google Drive — it never deletes, trashes
> or moves anything. Only the local cache (posters/temp) is written.

## How streaming works (no downloads)

drivecast never copies a file locally. When you press play it launches your
video player pointed at a local URL:

```
http://127.0.0.1:8737/stream/<file_id>
```

That endpoint is a **Range-aware proxy**. It forwards the player's `Range`
header verbatim to the Google Drive media API
(`files/{id}?alt=media`) and streams the response straight back — a 206 Partial
Content with the correct `Content-Range`, so seeking works and only the bytes
you actually watch are transferred. Video bytes pass through memory in 1 MiB
chunks and are never written to disk. The proxy reuses a single long-lived,
connection-pooled HTTP client (HTTP/2 when the optional `h2` package is present),
so a player's many seek/Range requests avoid per-request TCP+TLS setup.

rclone is used purely as the **token authority**: `rclone backend drives
gdrive1:` refreshes the OAuth token as a side effect, and drivecast reads the
fresh `access_token` from `rclone config dump`. All browsing, searching and
streaming go directly against the Google Drive v3 API with that Bearer token.

## Setup

### 1. Configure an rclone Google Drive remote (assumed already done)

drivecast expects a working rclone remote with Google Drive **full** scope,
named `gdrive1` by default:

```sh
brew install rclone
rclone config          # new remote -> type "drive" -> full access -> authorise
rclone backend drives gdrive1:   # should print your Shared Drives as JSON
```

The rclone config must **not** be encrypted — drivecast reads the token
non-interactively.

### 2. Install drivecast dependencies

```sh
cd drivecast
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3. (Optional) Install mpv for resume tracking

```sh
brew install mpv
```

mpv (and IINA) expose a JSON IPC socket that drivecast polls to save your
playback position, powering the **Continue Watching** shelf and resume prompts.
**VLC is tracked too**, via its built-in HTTP interface — drivecast launches VLC
with a private loopback HTTP server and polls its status for your position, so
resume and Continue Watching work in VLC as well. mpv stays the recommended
default (it needs no extra interface); if VLC's HTTP interface can't start
(older build, busy port), playback still works, just launch-only.

**Choosing a player.** By default drivecast auto-picks mpv → IINA → VLC. To force
one, use **Settings → Video player** in the app (it shows which players are
installed), or set `"player"` in `config.json` to `auto` / `mpv` / `iina` / `vlc` / `infuse`.
Playback works the same in any of them (each streams the local URL by requesting
byte-ranges); all three now report your position back for resume. **Infuse**
(Mac App Store) is supported as a launch-only target: drivecast opens the
stream via Infuse's URL scheme, so playback works great but there is no resume
tracking, Continue Watching, or autoplay-next for it (Infuse keeps its own
internal resume). Auto never picks Infuse — select it explicitly.

### 4. (Optional) TMDB posters

Get a free API key from https://www.themoviedb.org/settings/api and put it in
**`~/Library/Application Support/drivecast/secrets/secrets.json`** (see
[Secrets & security](#secrets--security)) as `{"tmdb_api_key": "…"}`. Without a
key (or when TMDB has no match), the scan falls back to the video file's own
**Google Drive thumbnail**; only titles with neither show a clean gradient
placeholder card (parsed title + year).

### 5. (Optional) Subtitles

drivecast loads **English subtitles automatically** when it can find them,
resolved at play time and cached locally (`data/subs/`):

1. a subtitle file sitting **next to the video on the drive** (release folders
   very often ship an `.srt`) — matched by name and downloaded once;
2. **OpenSubtitles**, if you put a free API key from
   https://www.opensubtitles.com/consumers into
   `~/Library/Application Support/drivecast/secrets/secrets.json` as
   `{"opensubtitles_api_key": "…"}` — searched by parsed title (+ year or
   season/episode), best English match downloaded.

The subtitle is handed to mpv / IINA / VLC as a local file, so it appears
pre-loaded (toggle it in the player as usual). Autoplay-advanced episodes
reuse cached subtitles. Remote clients get the same resolution over HTTP:
`GET /api/subtitles/{file_id}` serves the resolved file (SRT/VTT/ASS) with
the right MIME type — the [Fire TV app](https://github.com/AdinathChaudhari/drivecast-app)
attaches it as a subtitle track. Turn the whole feature off with **Settings →
English subtitles when available**.

## Watch on your iPhone / iPad

drivecast's web UI also works on a phone or tablet — same library, same
Continue Watching, same posters — you just need to let the device reach your
Mac. This is **off by default**.

1. Turn on **Settings → Watch on iPhone / iPad**, then restart drivecast
   (`Ctrl+C` and re-run `./venv/bin/python app.py`, or quit and relaunch the
   menu-bar app) — enabling it changes which address the server binds to, so
   it only takes effect on the next launch.
2. Settings then shows a QR code and URL to scan, using either:
   - **Same Wi-Fi (HTTPS)** — nothing to install, works with Safari's
     HTTPS-Only mode. drivecast mints its own little certificate authority
     on first launch and serves a trusted `https://…` address on your
     network (port `8738` by default). One-time setup per device: tap
     **Trust this Mac** in the Settings card (or scan its QR), install the
     downloaded profile (Settings → Profile Downloaded), then enable full
     trust under **Settings → General → About → Certificate Trust
     Settings**. Before tapping Install, compare the certificate's SHA-256
     fingerprint against the one the Settings card shows — if they don't
     match, don't install. Only works on that network; if the Mac's IP
     changes later, the Settings card will ask for a restart so drivecast
     can re-issue the certificate (the root you trusted stays the same).
   - **Tailscale** — install it on your Mac and your phone/iPad
     (`brew install` / App Store, on the same tailnet), then scan the QR.
     Works from anywhere — home, a cafe, cellular — and the traffic is
     encrypted end-to-end.

     If your phone's Safari has **HTTPS-Only mode** on it will refuse the
     plain `http://100.x…` URL. Fix it properly with **Tailscale Serve**,
     which gives drivecast a real HTTPS address with a valid certificate:
     run `tailscale serve --bg 8737` on the Mac (first time, it prints a
     link to enable Serve for your tailnet — one click), and drivecast's QR
     automatically switches to the `https://<mac>.<tailnet>.ts.net` URL.
3. Scan the QR (or open the URL). The link carries a secret **token** —
   treat it like a password: anyone with the link can stream your whole
   library, so don't post the QR or URL anywhere public.

What plays where:

- **MP4 / M4V / MOV and audio files** play right in the phone's browser (an
  inline video/audio player), with resume and **Continue Watching** kept in
  sync with the Mac.
- **MKV** (and anything else the browser can't play natively) hands off to
  **VLC iOS** or **Infuse** via a button instead of playing inline. Resume /
  Continue Watching is **not** tracked for files played this way — that only
  works for drivecast's own web player.

Tap **Add to Home Screen** in Safari afterward and it opens full-screen like a
real app, with no browser chrome.

## Watch on your TV (Fire TV / Android TV)

There's a **native TV client** — [drivecast-app](https://github.com/AdinathChaudhari/drivecast-app)
— built for the Amazon Fire TV Stick (and Android TV generally): Kotlin,
Compose for TV and ExoPlayer, sideloaded as an APK. It talks to the same
server as every other client, with the same token, and adds what the web
player can't do:

- **MKV / HEVC play natively** — ExoPlayer decodes them in hardware, so
  there's no hand-off to another app and no format whitelist.
- **Made for a remote** — D-pad rows, focus scaling, media keys; a
  Netflix-style Home with the Continue Watching shelf (dismissable there
  too) and one row per section.
- **Binge without lifting a finger** — when it hands a show to VLC it hands
  over a *playlist* of the remaining episodes, so VLC's own Next/Previous walk
  the season; there's also a Shuffle button for random-order play.
- **Auto-discovery** — the app finds your Mac by probing the network for
  drivecast's unauthenticated `GET /api/ping`, so pairing is just typing the
  access token.
- **Everything stays in sync** — resume positions, watched marks and the
  Continue Watching shelf are shared with the web UI and the Mac, both ways.

Requirements are the same as phone access: **Remote Access enabled** and the
TV able to reach your Mac (same network, or Tailscale's Fire TV app for
anywhere-access). Install and pairing instructions live in that repo's README.

### How safe is this?

Remote access is defended in layers; a stranger on the internet would need to
break several independent things at once:

1. **Reachability.** With the toggle off (the default), the server binds
   `127.0.0.1` and is unreachable from any network, full stop. With it on, the
   Tailscale address only routes for devices **signed into your own tailnet**
   (`tailscale serve` runs in "tailnet only" mode) — to a stranger the
   `…ts.net` name doesn't even resolve. The Wi-Fi address only exists on your
   own network. Nothing is ever exposed to the public internet.
2. **The token.** Every request from a non-local client must present the
   secret token (128-bit, auto-generated). It's compared in constant time,
   never written to logs (the access log is disabled precisely because tokens
   travel in URLs), and after the first visit it lives in an `HttpOnly`
   cookie. Without it, every endpoint — including streams — answers 401.
3. **Blast radius.** drivecast is strictly read-only on Google Drive, so even
   someone holding a valid link could only *watch* — never delete, modify or
   upload. And playback on the Mac (`/api/play`) refuses non-local clients
   outright.

About that Wi-Fi HTTPS certificate: it's signed by a private CA drivecast
generates locally with the Mac's built-in `openssl`. Both keys live in
`~/Library/Application Support/drivecast/certs/` with owner-only permissions
and never leave the Mac; the CA-download endpoint serves only the public
certificate. Installing the profile tells your phone to trust certificates
signed by *your* drivecast — which is exactly why the Settings card shows the
CA's SHA-256 fingerprint: it's your proof that the profile Safari downloaded
is the one your Mac generated, not something swapped in by whoever else is on
the network. If the fingerprints differ, don't install.

The QR / URL **is** the credential — treat it like a password. To revoke all
access instantly (lost phone, shared a link by mistake): set
`"remote_token": ""` in `~/Library/Application Support/drivecast/config.json`
and restart — a fresh token is generated the next time you save Settings, and
every old link, QR and cookie stops working immediately.

### Keeping the Mac awake while streaming

If the Mac sleeps mid-stream, playback on the phone/TV dies. So drivecast holds
a macOS power assertion (`caffeinate -i -s`) for as long as something is
streaming — acquired when a `/stream` response starts, dropped when it ends. It
works lid-closed in **clamshell mode**. The toggle is **Keep Mac awake while
streaming** in Settings (on by default).

**On AC only.** The assertion is held *only while the Mac is on AC / passthrough
charging* — never on battery, so it can't quietly drain a laptop that's unplugged.
drivecast checks the power source itself (`pmset -g batt`) rather than trusting
`caffeinate`'s own AC detection, which missed some passthrough-charging hubs; it
re-checks about once a minute and drops the assertion the moment you switch to
battery (and reclaims it if you plug back in while still streaming).

**"Are you still watching?"** When streaming stops (or a paused client stops
pulling bytes), drivecast doesn't cut the assertion instantly. It waits out a
**2-minute grace** (so seeks and gaps between episodes don't flap it), then opens
a **30-second countdown prompt** in any open drivecast tab: *Are you still
watching?* Click **Yes, keep watching** to reset another 2-minute grace, or
**No** to release right away. If you don't answer, the countdown lapses and the
Mac is allowed to sleep naturally. Starting playback again at any point cancels
the countdown.

One caveat: even with the assertion held, macOS clamshell rules can still force
the Mac to sleep if the **external display or power is disconnected** — that's
the OS, outside drivecast's control. The nuclear option (outside drivecast's
scope) is `sudo pmset -a disablesleep 1`, which disables sleep entirely until
you set it back to `0`.

## Run

```sh
./venv/bin/python app.py
```

This starts the server on `http://127.0.0.1:8737/` and opens your browser. Set
`DRIVECAST_NO_BROWSER=1` to skip opening the browser.

If rclone can't produce a token, the web UI shows a friendly setup page instead
of the library.

To **stop** the app, press `Ctrl+C` in the terminal where it's running (or just
close the terminal). Your player keeps playing independently — drivecast is only
needed to start playback and feed the stream, so quitting it stops new plays but
doesn't kill a movie already open in mpv/VLC.

## Menu-bar app / building `drivecast.app`

Prefer no terminal? drivecast ships a native macOS **menu-bar app**. It runs the
same server in-process and puts a small **☁** icon in your menu bar:

- a status line — `drivecast: running on :8737` (or `setup needed` if rclone
  isn't configured yet, or `scanning… n/total` while a refresh runs)
- **Open drivecast** — opens `http://127.0.0.1:8737/` in your browser
- **Drives to include** — a submenu listing every Shared Drive as a checkable
  item; toggling one updates `selected_drives` and kicks a background refresh
- **Refresh library** — rescans the selected drives now
- **Auto-refresh on launch** — checkable toggle (`auto_refresh_on_startup`)
- **Quit** — cleanly shuts the server down and exits

If drivecast is already running when you launch it again, it just opens the
existing instance in your browser and exits (it won't start a second server).

### Requirements

rclone must be set up on the machine (see **Setup** above) — the app reads the
Drive token from your rclone config exactly like `app.py` does. A player
(mpv / IINA / VLC, plus Infuse launch-only) is needed to actually play video;
mpv / IINA / VLC support resume tracking (VLC via its HTTP interface), with mpv
the recommended default (`brew install mpv`).

### Build the bundle

```sh
cd drivecast
./venv/bin/pip install rumps py2app        # runtime + build deps
./venv/bin/python setup_app.py py2app
```

The bundle is written to **`dist/drivecast.app`**.

### Install & launch

- Drag **`dist/drivecast.app`** to **/Applications**.
- Launch it from **Spotlight** (⌘-Space → "drivecast") or the Applications
  folder / Dock. It's a menu-bar agent (`LSUIElement`), so it shows a ☁ in the
  menu bar rather than a Dock icon.
- **Quit** from the menu-bar dropdown → **Quit**.

> The bundled app and the source app share the same settings, secrets and
> library in `~/Library/Application Support/drivecast/`, so your drive selection,
> TMDB key, watch history and posters **persist across rebuilds**. Change most
> settings in-app (**Settings** / the menu-bar submenus); edit
> `~/Library/Application Support/drivecast/config.json` directly for the rest.

### Run without building (quick test)

You can run the menu-bar app straight from source — this uses your repo's
`config.json`:

```sh
./venv/bin/python drivecast_menubar.py
```

Set `DRIVECAST_NO_BROWSER=1` to skip auto-opening the browser.

## Using the app (everyday flow)

Once `app.py` is running and the library opens in your browser:

1. **Pick your drives (first run).** If you haven't selected any drives yet, the
   home screen prompts you to open **Settings**, where every Shared Drive is
   listed as a checkbox. Tick the ones you want, optionally enable
   **Auto-refresh on launch**, and **Save**. Saving kicks off the first scan.
   (You can also do this from the menu-bar app's **Drives to include** submenu.)
2. **Wait for the scan.** A progress bar shows `Scanning drives… n/total` while
   drivecast walks the selected drives and builds the library. This is the only
   time normal use touches the Google API; it's throttled and retries on rate
   limits, so it's safe to leave running.
3. **Browse the library.** The home screen shows a **Continue Watching** shelf on
   top, then your library as a grid of movie/show tiles with posters (each with a
   quality pill). Filter by **All / Movies / TV Shows**; **Sort** by Title,
   Year, Recently added, or Recently watched; and **Group** by nothing, by type
   (Movies / TV Shows), or by drive. Sort/group happen instantly client-side over
   the cached data and your choice is remembered. The search box does an instant,
   offline search over the cached library. A **🌙/☀️ toggle** in the top bar
   switches between dark and light themes (applied before first paint, so no
   flash; remembered per device).
4. **Open a title.** Click a movie tile for its detail page (poster, overview,
   **Play**). Click a show tile for its detail page with a **season selector** and
   that season's episodes in order — click an episode to play it. A **⤨ Shuffle**
   button plays every episode of the show in a random order.
5. **Play.** Your player (mpv/IINA/VLC) opens and starts streaming within a
   couple of seconds (duration/size come from the cache, so there's no blocking
   metadata call). Seek anywhere — only the bytes you watch are fetched. If you've
   watched this file before, you'll first be asked **Resume / Start over**.
6. **Autoplay next episode.** When you start an episode, the rest of that season
   queues up behind it; when the episode **finishes** the next one starts
   automatically, chaining through the queue (and through the whole show when you
   press **Shuffle**). "Finished" means the player reached the end — the last 10%
   of the file, or within ~90s of the end. If you instead **quit mid-episode**
   (close the player before the end), the session simply stops — no autoplay. Turn
   the whole feature off with **Settings → Autoplay next episode**.
7. **Continue Watching.** With **mpv**, **IINA** *or* **VLC**, drivecast tracks
   your position automatically (VLC via its HTTP interface), so partly-watched
   titles (including the right *episode* of a show) reappear on the home shelf and
   resume where you left off. Each shelf card shows the title's **poster** (with
   the progress bar overlaid) and the clean library title — an in-progress episode
   shows its show's poster and name rather than the raw filename. An **✕** on
   each card removes it from the shelf (dropping that file's saved position).
   Files played outside the library (via **Browse files**) keep the gradient
   placeholder. mpv
   stays the recommended default; if VLC's HTTP interface can't start, playback
   still works, just without resume tracking (and without autoplay, which needs
   the position to know the episode finished).
8. **Refresh.** When you add or remove content on the drives, click **Refresh**
   (top bar) or the menu-bar **Refresh library** item. drivecast rescans, adds
   new titles, removes deleted ones, updates show episode lists, and backfills
   posters for any title still missing one (so enabling a TMDB key and hitting
   Refresh gives every existing tile a poster) — all without disturbing what
   you're watching. To refresh a **single drive** (you know where you just
   uploaded), hover it in Settings for its ⟳ button, or use the menu-bar
   **Refresh one drive** submenu.
9. **Tabs.** Create tabs in Settings (**＋ New tab…** in the per-drive dropdown):
   name + emoji + a **behavior** (Movies & TV / Course / Podcast, or a private
   plugin behavior). Then assign drives to your tabs. There are no tabs by
   default; an included drive you haven't put in a tab yet simply shows nothing
   until you assign it. Saving re-scans just the drives whose tab changed. Each
   tab gets its own accent colour and its behavior's layout (course progress
   rings, module/lesson naming, channel tiles). Tabs that behave like *Movies &
   TV* get **category chips** — Documentaries are detected from TMDB genres, and
   titles TMDB doesn't know land under *Other*. Optional per-drive hints live in `config.json` (`drive_hints`):
   `{"<drive_id>": {"category": "documentary"}}` categorises a whole drive's
   TMDB misses, and `{"<drive_id>": {"single_course": true}}` treats a drive
   as ONE course whose root folders are modules.
9. **Browse raw files (advanced).** The **Browse files** link still gives you the
   old live folder-by-folder browser over any drive, if you ever need it.

### Turning on posters (TMDB)

Posters are **pre-cached during the scan**: for each title without one drivecast
resolves TMDB (movie vs TV, by title + year), downloads the w342 poster to the
posters cache, and stores its path in the library record — so tiles load
instantly from disk with no per-card lookup. When TMDB has no match (or no key
is set), the scan falls back to the video file's own **Google Drive thumbnail**:
it's downloaded once into the same posters cache (Drive thumbnail URLs expire
within hours, so they're fetched at scan time, at a bumped-up size) and used as
the tile artwork. Only a title with neither a TMDB poster nor a Drive thumbnail
shows the gradient placeholder with the title and year.

To enable posters:

1. Get a free API key: https://www.themoviedb.org/settings/api (Developer plan,
   no fee). For the sign-up form, "Application URL" can be
   `http://localhost:8737` and "Type of Use" is **Desktop Application**.
2. Put the key in `~/Library/Application Support/drivecast/secrets/secrets.json`
   (see [Secrets & security](#secrets--security)).
3. Restart `app.py` and **Refresh** the library. The scan backfills posters for
   **every** title still missing one, so a first Refresh after adding the key
   fills in your whole existing library, not just newly-added titles.

## Secrets & security

drivecast is designed so **nothing personal ever reaches the repo**. All private
material lives outside the repo (in `~/Library/Application Support/drivecast/`)
and is loaded at runtime:

- **API keys** → `~/Library/Application Support/drivecast/secrets/secrets.json`:

  ```json
  { "tmdb_api_key": "your-tmdb-key", "opensubtitles_api_key": "your-os-key" }
  ```
  (The repo ships `secrets/secrets.example.json` only as a format reference.)

  You can also pass it via the `DRIVECAST_TMDB_API_KEY` environment variable.
  Keys are **never** written back into `config.json`, so they can't leak there.
- **Google Drive credentials** never touch drivecast — they live only in your
  rclone config.
- **Config, data and secrets live in a stable per-user directory**,
  `~/Library/Application Support/drivecast/` (`config.json`, `data/`,
  `secrets/secrets.json`) — *not* inside the repo or the packaged `.app`. This
  means rebuilding/reinstalling the app never wipes your selected drives, library
  or history, and the bundled app can read your TMDB key. It all stays local and
  never reaches git. (`config.example.json` in the repo is only the first-run
  template.)
- The web server binds to **`127.0.0.1`** by default; the opt-in remote-access
  mode binds to your LAN/tailnet and requires a secret token on every
  non-local request (see
  [Watch on your iPhone / iPad](#watch-on-your-iphone--ipad)).
- A **pre-commit hook** (`scripts/install-hooks.sh`) refuses to commit secret
  files or key-shaped strings as a backstop. Install it after cloning:

  ```sh
  scripts/install-hooks.sh
  ```

The only thing you must supply is the rclone remote and (optionally) a TMDB key;
neither is stored anywhere git can see.

## Configuration (`config.json`)

Lives at `~/Library/Application Support/drivecast/config.json`, auto-created from
the repo's `config.example.json` on first run. Holds **non-secret** settings
only — secrets go in `secrets/` (above).

| key            | default   | meaning                                             |
|----------------|-----------|-----------------------------------------------------|
| `remote`       | `gdrive1` | rclone remote name (no trailing colon)              |
| `player`       | `auto`    | `auto` \| `mpv` \| `iina` \| `vlc`                  |
| `port`         | `8737`    | local port (bound to 127.0.0.1 only)                |
| `remote_access` | `false`  | opt-in: expose the server on your LAN/tailnet (with a token) for phone/tablet viewing |
| `remote_token` | `""`      | auto-generated secret required on every non-local request when `remote_access` is on |
| `https_port`   | `8738`    | trusted-LAN HTTPS listener (only started when `remote_access` is on) |
| `page_size`    | `200`     | Drive files.list page size                          |
| `selected_drives` | `[]`   | Shared Drive ids included in the library            |
| `tabs`         | `[]`      | your tabs: `[{key,label,icon,behavior,accent?,accent2?}]` (seeded from a pre-tabs config on first launch) |
| `drive_sections` | `{}`    | drive id → a tab `key` from `tabs` (unassigned = shows in no tab) |
| `drive_hints`  | `{}`      | per-drive classifier hints (`category`, `single_course`) |
| `auto_refresh_on_startup` | `false` | rescan the library on each launch        |
| `scan_throttle` | `0.15`   | seconds to pause between scan API calls (quota)     |
| `autoplay_next` | `true`   | auto-play the next episode when one finishes        |
| `subtitles`    | `true`    | load English subtitles when available               |

`selected_drives`, `auto_refresh_on_startup` and `autoplay_next` are normally set from the
**Settings** view or the menu-bar app rather than by hand. (`tmdb_api_key` is a
secret — set it in `secrets/secrets.json`, not here.)

## Data (runtime, local)

All under `~/Library/Application Support/drivecast/data/` (persists across app
rebuilds):

- `library.json` — the cached catalogue (movie/show/course records, seasons,
  episodes, poster paths); rebuilt by a scan/refresh
- `scan_cache.json` — raw per-drive scan records; lets a per-drive refresh
  rebuild the whole library without re-walking the other drives
- `history.json` — resume positions & watched state, keyed by Drive file id
- `tmdb_cache.json` — cached TMDB lookups (including negative results)
- `subs/` — downloaded subtitles, keyed by video file id
- `posters/` — downloaded artwork: TMDB w342 posters, plus `dthumb_*.jpg`
  Google Drive thumbnails cached as fallbacks

## Notes

- The server binds to `127.0.0.1` by default; the opt-in remote-access mode
  binds to your LAN/tailnet and requires a secret token on every non-local
  request. The LAN URL is served over HTTPS from a locally-generated CA
  (keys in `~/Library/Application Support/drivecast/certs/`, owner-only).
- Operations are keyed by Drive **file id**, so filenames with quotes/unicode
  are handled safely.
- Shared-drive queries always set `supportsAllDrives` / `includeItemsFromAllDrives`.
- **Rate limits / quota.** The library cache is designed so normal browsing hits
  the Google API zero times; only a scan/refresh does, and it backs off and
  retries on `rateLimitExceeded` / 429 instead of crashing. If you share rclone's
  built-in OAuth client with many users you'll still hit its tiny per-minute
  quota during large scans. The real fix is to use **your own** Google OAuth
  client id/secret in the rclone remote (a free Google Cloud project) — that
  gives you your own generous quota. Set that up separately in `rclone config`.
