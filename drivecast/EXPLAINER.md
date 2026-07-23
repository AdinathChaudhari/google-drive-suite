# drivecast, Explained From the Ground Up

*A layer-by-layer walkthrough of what was built, why it was built that way, and
how every piece works — written for someone with a basic understanding of code.*

---

## 1. The problem we started with

Thanks to `drive-offload`, your movies and shows now live on **Google Shared
Drives** instead of your nearly-full Mac. That solved storage — but created a
viewing problem. To watch anything, you'd have to download it back first,
which defeats the whole point of pushing it to the cloud.

So the wish was:

> "Give me something like Infuse or VLC — browse my Shared Drives like a
> media library and press play — but it must **stream** the video, never
> download it."

drivecast is that something: a small web app that runs on your Mac. You open
it in a browser, see all 40 of your Shared Drives, browse folders as a
poster grid, and click a video. A real video player opens and starts playing
within seconds — even if the file is 20 GB — and you can jump to any point in
the movie instantly. Nothing lands on your disk.

---

## 2. The key idea: watching *is* downloading — just only the part you watch

Here's the mental shift that makes everything click: **there is no magical
difference between "streaming" and "downloading."** Netflix downloads video
to your device too — it just downloads *only the few seconds you're about to
watch*, plays them, throws them away, and asks for the next few seconds.

The mechanism behind this is a standard HTTP feature called a **Range
request**. A normal download says "give me the file." A Range request says
"give me **bytes 5,000,000 through 6,000,000** of the file." Google Drive's
API happily answers Range requests.

That single feature gives us everything a player needs:

- **Instant start** — the player asks for the first chunk and starts playing
  it while requesting the next one. No waiting for 20 GB.
- **Instant seeking** — jump to the middle of the movie and the player simply
  asks for bytes starting at the middle. Google serves them in a second or
  two, no matter how big the file is.
- **Zero disk usage** — the chunks flow from Google, through drivecast, into
  the player's memory, onto your screen, and are gone. The stream path in the
  code contains no file writes at all — it *can't* download to disk even by
  accident.

Video players already know how to do all of this. mpv and VLC constantly make
Range requests when playing anything over the network. So drivecast doesn't
implement a player — it implements the thing players need on the other end:
a URL that answers Range requests with your Drive's bytes.

---

## 3. The keymaster: rclone (again)

Just like drive-offload, drivecast never asks you to log into Google or set
up developer credentials. It borrows from the work you already did: your
rclone remote **`gdrive1:`**, which holds a valid Google login.

Google's API doesn't accept "I'm rclone, trust me" — every request needs an
**access token**, a temporary password string that expires after about an
hour. rclone stores its current token in its config file and knows how to
refresh it when it goes stale. drivecast piggybacks on that:

1. It runs `rclone backend drives gdrive1:` — ostensibly to list your Shared
   Drives, but with a useful side effect: rclone notices if its token is
   stale, refreshes it with Google, and saves the fresh one.
2. It then runs `rclone config dump` and reads the fresh token out of the
   config.

So rclone remains the one and only keeper of your Google login; drivecast
just asks it for the current key whenever needed. (This is the exact trick
`todrive` in drive-offload already used — proven to work on your machine.)

The token expiring every hour sounds like a problem for a 3-hour movie, but
it isn't — and the reason is elegant. Because playback is thousands of small
Range requests rather than one giant download, **each request gets a fresh
token stamped on it** as it passes through drivecast. Google checks the token
at the *start* of each request. So even if the token expires mid-movie, the
very next chunk request simply carries the new one. Expiry can never
interrupt playback.

---

## 4. The architecture: three players and a middleman

```
┌───────────┐        ┌────────────────────┐        ┌──────────────┐
│  Browser   │  API   │     drivecast      │ Range  │ Google Drive │
│ (library   │◄──────►│  (Python web app   │◄──────►│     API      │
│  UI)       │        │   on 127.0.0.1)    │ chunks │              │
└───────────┘        └─────────┬──────────┘        └──────────────┘
                               │ launches, then feeds
                               ▼
                      ┌────────────────┐
                      │  mpv / VLC     │  ◄── plays http://127.0.0.1:8737/stream/<file-id>
                      └────────────────┘
```

- **The browser** shows the library: drive list, folders, posters, search,
  Continue Watching. It never touches video — it only talks to drivecast's
  small JSON API ("what's in this folder?", "play this file").
- **drivecast** is a Python web server (FastAPI) running only on your own
  machine (`127.0.0.1` — deliberately unreachable from the network, because
  it's effectively a proxy into your entire Drive).
- **The video player** (mpv or VLC) is given a plain local URL like
  `http://127.0.0.1:8737/stream/abc123`. As far as the player knows, it's
  playing an ordinary web video. It has no idea Google Drive exists.

### The stream proxy — the heart of the app

When the player asks drivecast for `bytes 5,000,000–6,000,000` of file
`abc123`, drivecast:

1. grabs a fresh access token from the rclone trick above,
2. forwards the *exact same* Range request to Google's
   `files/abc123?alt=media` endpoint with the token attached,
3. pipes Google's answer straight back to the player, 64 KB at a time,
   never touching disk.

It's a relay — a translator that adds the secret handshake (the token) that
the player doesn't know.

One funny-looking behavior is actually normal: every time you **seek**, the
player rudely hangs up its current connection and opens a new one at the new
position. The server logs show a burst of aborted requests. Early versions of
software like this often treat those as errors; drivecast expects them and
quietly closes the matching connection to Google.

---

## 5. What happens when you click Play (the whole story)

1. You click a poster in the browser. If you've watched part of it before, a
   dialog asks **"Resume from 42:10, or start over?"**
2. The browser tells drivecast: "play file `abc123`, resume at 2530 seconds."
3. drivecast picks a player. By default it auto-detects in order of preference:
   **mpv** (best) → **IINA** (a Mac app built on mpv) → **VLC**. You can also
   force a specific one in **Settings → Video player** (the dropdown shows which
   are installed) — e.g. choose **VLC** if you'd rather play there. Whichever it
   is, drivecast just hands it the local stream URL; VLC plays it via its own
   network-streaming (requesting byte-ranges) exactly like mpv.
4. The player is launched with the local stream URL and told to start at
   42:10. It makes its first Range request, drivecast relays it, and the
   movie appears within a few seconds.
5. drivecast also opens a tiny side-channel to read the current playback time.
   **mpv / IINA** expose a "remote control socket"; **VLC** exposes an HTTP
   interface (drivecast launches VLC with a private loopback HTTP server on a
   spare port and a random password). Either way a background thread asks
   *"what's the current playback time?"* every 3 seconds and writes the answer
   into a small history file — so resume positions and the Continue Watching
   shelf stay accurate to within ~3 seconds, even if you force-quit the player.
6. When the player exits, the final position is saved and drivecast makes one
   decision: **did this file finish, or did you quit early?** "Finished" means the
   last position reached the end — the last 10 % of the file, or within ~90s of
   the end (a small pure function, `should_advance(position, duration)`, so the
   rule is unit-tested without launching anything). If it finished it's marked
   **watched** and drops off Continue Watching; if you quit mid-file it just stops.

All three players (mpv, IINA, VLC) therefore support resume. mpv stays the
recommended default because it needs no extra interface; if VLC's HTTP interface
can't start (an older build, or the port is busy), drivecast quietly degrades to
launch-only for that session and everything else still works.

### Autoplay next episode (and Shuffle)

Playing a TV episode also hands drivecast a **queue** — the episodes that come
after it. When an episode *finishes* (the same finished-vs-quit rule above), and
autoplay is on, drivecast pops the next item off the queue and launches it with
the *same* player, passing the remainder as its queue. This chains through the
whole list, one relaunch per episode (simple, and it keeps per-episode resume
tracking intact). If you instead **quit mid-episode**, the queue is dropped and
the session ends — closing the player is how you stop the marathon.

Only one session/queue is ever active: starting a new play stops the previous
poller first, and the just-finished poller refuses to auto-advance if a newer
session has already superseded it. The clicked-episode path queues the rest of
that season; the **⤨ Shuffle** button on a show's page shuffles *all* the show's
episodes (Fisher–Yates, in the browser) and plays them as one big queue. Autoplay
can be turned off entirely in **Settings → Autoplay next episode**
(`autoplay_next` in config, default on); when off, the queue is ignored and only
the clicked item plays.

### English subtitles (found for you, cached forever)

Before the player launches, drivecast tries to find an English subtitle for the
file — bounded to a few seconds and failure-silent, so playback never waits on
it. Three sources, in order: the **local cache** (`data/subs/`, keyed by the
video's file id — instant on every replay); a **sibling subtitle file in the
video's own Drive folder** (release folders very often ship an `.srt` next to
the movie — matched by filename stem, SxxExx marker and language tags, then
downloaded once); and **OpenSubtitles**, when a free API key sits in secrets —
searched by the parsed title plus year or season/episode, best English match
taken. Whatever wins is stored as a local file and handed to the player
(`--sub-file` for mpv and friends), so subtitles arrive pre-loaded in all three
players with zero player-side setup. Autoplay-advanced episodes reuse whatever
is already cached. **Settings → English subtitles when available** switches the
whole thing off.

### On your phone

Everything above assumed the browser and drivecast live on the same machine —
`127.0.0.1` talking to itself, which is exactly why the server never bothered
asking anyone to log in. Put drivecast on your phone's Wi-Fi, or a shared
Tailscale tailnet, and that assumption stops holding: a JSON API that can list
your whole cloud library and stream any file is now reachable by something
other than you. drivecast's answer is to stay locked down by default, and once
you flip it open, to require a password.

The trust boundary is just `127.0.0.1` (and its IPv6 twin `::1`) — "is this
request the Mac talking to itself?" That question can't be faked: the address
drivecast checks comes from the real TCP socket the operating system handed
the server, never from a header a client could type in, so nothing arriving
over the network can dress itself up as loopback. Turn on **Watch on iPhone /
iPad** in Settings and drivecast starts listening on your LAN/tailnet address
too — but now every request that *doesn't* come from loopback has to prove it
knows a secret **token** (a random string generated the moment you flip the
setting on) or it's turned away.

Typing a token on every visit would be tedious, so the QR code and URLs on the
Settings page bake it in as `?token=…` right there in the link — scan it once
and that first page load hands your phone's browser a **cookie** that
remembers you for six months, so after that you just reopen the bookmark or
tap the home-screen icon. The token itself is compared with
`hmac.compare_digest` rather than a plain `==`, so a network attacker can't
guess it one character at a time by timing how fast the comparison fails.

There's one more hoop, courtesy of modern Safari: it increasingly refuses
plain `http://` addresses outright (HTTPS-Only mode), and no public
certificate authority will ever sign a certificate for a private address
like `192.168.1.23`. So drivecast becomes its own tiny certificate
authority. On the first remote-access launch it shells out to the Mac's
built-in `openssl`, creates a private root CA plus a server certificate
covering the Mac's names and addresses, and opens a second, TLS-wrapped
listener (port 8738) serving the very same app. Trust that root once on
your phone — the Settings card walks you through installing the profile
and checking its SHA-256 fingerprint first — and the Wi-Fi URL becomes
real, padlock-and-all HTTPS. The CA's private key never leaves
`~/Library/Application Support/drivecast/certs/`; if the Mac's LAN address
changes, a fresh server certificate is minted at the next launch while the
root you trusted stays the same, so the phone never has to be set up twice.

One asymmetry is deliberate: `/api/play` — the endpoint that launches
mpv/VLC **on your Mac** — refuses any request that isn't from loopback, full
stop. A phone must never be able to pop a video player open on your desktop
from across the room. Instead, a remote browser gets its own path entirely:
files the browser can play natively (MP4, M4V, MOV, audio) stream straight
into an in-page `<video>`/`<audio>` element; everything else (chiefly MKV)
hands off to VLC iOS or Infuse through a URL scheme — the phone opening its
own app with its own copy of the stream URL and token. drivecast has no idea
what happens after that handoff, which is also why resume tracking stops
working for files played that way.

### On your TV

The same remote surface powers a **native Fire TV / Android TV app**,
[drivecast-app](https://github.com/AdinathChaudhari/drivecast-app) — a separate
repo, because it's a separate program (Kotlin + ExoPlayer) that just happens to
speak drivecast's API. It needed almost nothing new from the server, which is
the point: the JSON API plus the range-aware stream proxy already were a
complete client contract. A few small additions closed the gaps:

- `GET /api/ping` — the one deliberately **unauthenticated** endpoint. A TV
  has no camera for QR pairing, so the app instead probes the local network
  for something that answers "I'm a drivecast server". It reveals nothing but
  the app's presence, and it answers even when remote access is off — so the
  TV can tell you to go enable it rather than pretending nothing's there.
- `GET /api/subtitles/{file_id}` — the subtitle machinery above always ended
  in a *local file path*, which no remote player can use. This endpoint runs
  the same resolution and serves the winning file over HTTP with its real
  MIME type. No format conversion: ExoPlayer parses SRT/VTT/ASS natively.
- `GET /api/playlist/{title_id}.m3u` (plus a JSON twin) — an ordered M3U of a
  show's episodes with token-baked stream URLs, so when the app hands a show to
  VLC it hands over a *playlist*, not one file, and VLC's own Next/Previous
  buttons walk the season. `?start=<file_id>` trims it to that episode onward;
  `?shuffle=1&seed=<n>` reorders it with the same SplitMix64 shuffle the app
  runs, so both sides agree on order episode-for-episode.
- `GET /api/stream/recent` — VLC returns the *playlist* URL when you quit, not
  the episode you stopped on. This endpoint reports which files streamed most
  recently, so the app can figure out where you actually were and report
  progress against the right episode — Continue Watching stays honest even when
  VLC drives the playlist itself.

Because ExoPlayer decodes MKV and HEVC in hardware, the browser's format
whitelist — and the whole VLC/Infuse handoff dance — simply doesn't exist on
the TV. And since the app reports progress through the same `/api/progress`
calls as the web player, the TV, the phone and the Mac all share one Continue
Watching shelf.

### Keeping the Mac awake (only while it matters)

Remote playback surfaced a physical problem: the Mac is the relay, and a
sleeping relay kills the stream. The lazy fix — never let the Mac sleep — is
wrong for a laptop, so drivecast scopes it tightly. Every active `/stream`
response holds a reference on a little power-assertion manager
(`drivecast/awake.py`); while the count is above zero, a `caffeinate` child
keeps the system awake — but **only when the Mac reports AC power**, checked
via `pmset` about once a minute (not trusted to `caffeinate -s`, whose own AC
detection misses some passthrough-charging hubs). On battery the manager
simply stands down: streaming never drains an unplugged laptop.

When the last stream stops, the assertion isn't dropped instantly — that
would flap on every seek and episode gap. Instead a small state machine runs:
**2 minutes of grace**, then a **30-second prompt window** during which any
client (web tab or TV app) can show *"Are you still watching?"* — answering
yes (`POST /api/awake/extend`) buys another 2 minutes, answering no
(`POST /api/awake/release`) or staying silent lets the Mac sleep naturally.
Clients read the phase from `GET /api/awake/status`; the machinery is
client-agnostic on purpose, so every screen gets the Netflix-style prompt
from the same three endpoints.

---

## 6. The library cache (and browsing, search, posters)

Early drivecast was a live folder browser: every click was a Google API call.
That was slow and, on rclone's shared credentials, quick to hit the rate limit.
The current design flips it around: drivecast keeps a **cached library** and
serves browsing entirely from disk.

**Selecting drives.** You choose which Shared Drives to include (Settings view or
the menu-bar **Drives to include** submenu). Only those drives appear; the rest
are invisible. The choice lives in `config.json` as `selected_drives`.

**The scan.** A scan (first run, on launch if you enabled auto-refresh, or when
you hit **Refresh**) walks each selected drive's folder tree via `files.list`,
building a **nested tree** that preserves the folder hierarchy, and then
classifies each folder **recursively**:

- A folder is a **TV show** if it has a direct season-named subfolder (`Season 1`,
  `S01`, `Series 2`, …) **or** ≥2 of its videos (directly, or in its immediate
  season subfolders) carry episode markers (`S01E02`, `2x05`, `Episode 3`, `E04`).
  A show becomes **one tile**: its descendant videos are grouped into seasons
  (from the subfolder name, else the `SxxExx` marker, else season 1) and sorted by
  episode number. Season-folder detection de-noises the folder name first
  (dropping bracketed groups and quality tokens) and reads a *leading* `S<number>`,
  so gnarly real-world names like `S01 (2017) 1080p 10bit HEVC NF WEBRip x265
  [ENGLISH - SPANISH]` and `S05 Part 1 (2021) …` still resolve to seasons 1 and 5,
  so a five-season show ripped that way still comes through as one tile. The short
  form is anchored to the start, so a title merely containing an S-number
  mid-string is never mistaken for a season.
- Otherwise the folder **expands into movies**, recursing down the tree so each
  film is its own tile:
  - A **leaf movie folder** with one main video becomes one movie titled from the
    *folder* name; with several main videos, one movie per video, titled from each
    *file* name.
  - A **container** (a collection like `Phase 1`, `Classics`, `Trilogy Box`,
    `Saga Collection`, holding movie subfolders and/or loose movie files,
    possibly nested) recurses into each subfolder and turns any stray loose video
    into its own tile — so a `Phase 1` folder yields each film inside it
    rather than one bogus `Phase 1` tile.
  - **Bonus-material subfolders** (`Featurettes`, `Extras`, `Bonus`, `Behind the
    Scenes`, `Deleted Scenes`, `Trailers`, `Special Features`, `Making Of`, …)
    become the movie's **`extras`** — labelled groups of bonus clips carried on
    the record (the same pseudo-season shape shows use), so a film's featurettes
    are one click away instead of discarded. A collection folder's *shared* bonus
    folder fans out onto every film it contains. Only **discard** folders
    (`Sample(s)`, `Subs`, `Subtitles`) hold no library content and are dropped. A
    leading **enumeration prefix** (`01) `, `01.`, `1 - `) is stripped from titles
    (without touching a title that legitimately *starts* with a number — a digit
    followed by a plain space, or an all-digit name, is left alone).
- Loose videos sitting at a drive's root are parsed by filename: `SxxExx` files
  group into a show; everything else is a standalone movie.

Each movie record is keyed by its Drive **file id** (unique and stable), so the
same film never collides with another tile.

**Grouping seasons into one show.** Real drives store seasons in wildly different
ways, so after the first pass drivecast merges them so each show is a single tile:

- `The Branch/Season 1/…` — seasons nested under a show folder (already one show).
- `Grimwold Season 1 S01`, `Grimwold Season 2 S02`, … — separate top-level
  folders sharing a name prefix are grouped under that prefix (`Grimwold`).
- `Season 1`, `Season 2`, … as bare folders — the **whole drive** is the show, so
  they're grouped under the drive's name.
- A show split across `… (Part 1)` / `(Part 2)` drives merges into one show.
- A sibling bonus folder (`Featurettes/Season N/…` beside the season folders, or
  inside a `<Show> Season N` folder) attaches to the merged show as
  `Featurettes · Season N` pseudo-seasons — marked `extras: true` so the UI
  lists them in the season picker without counting them as real seasons or
  pulling them into Shuffle. If a drive has no single owning show, the extras
  folder keeps its own tile instead of guessing the wrong owner.

Quality noise in folder names (`Season 1 (480p DVD)`,
`Grimwold (1983) S01 (576p x265 …)`) is stripped before detection, and a
whole-series wrapper named as a range (`Season 1-9 S01-s09`) is left as-is.

(Show names in examples throughout are invented.)

The result is written to **`data/library.json`** (atomically) as structured
records — title, year, type, a parsed **quality** label, an **`added_at`**
timestamp, and for shows the full season/episode tree, with each file's size and
**duration** pulled straight from the list response so playback later needs no
extra metadata call. From then on the home grid, detail pages, seasons and
episodes all render from this file: **zero API calls**.

**Quality pills.** Each record stores a short quality label parsed from the
filename — `4K` / `1080p` / `720p` / `SD`, with an optional `HDR` / `DV` suffix.
A movie takes its video-file's quality (folder-name fallback); a show takes the
**best** quality across its episodes, so the tile advertises the best copy you
have. The UI renders it as a small pill in the poster's top-right corner.

**`added_at`.** Each title is stamped with the epoch second it was first seen and
keeps that value across refreshes (carried over like poster metadata), so the
"Recently added" sort is stable.

**Refresh diffing.** A refresh rescans and diffs against the existing library:
newly-found titles are added, titles whose files are gone are removed (and their
now-orphaned posters deleted), and show episode lists are updated in place. A
`/api/refresh/status` endpoint drives the progress bar.

**Per-drive refresh.** You usually know exactly which drive you uploaded to,
so drivecast can rescan just that one: every scan stores each drive's raw
(pre-merge) records in `data/scan_cache.json`, and the library is then rebuilt
from the cache of **all** selected drives. A scoped refresh only re-walks the
scoped drive on the Drive API; everything else replays from cache — which is
what keeps a show split across two drives ("Part 1"/"Part 2" merged into one
tile) correct no matter which half you refresh. A drive whose scan errors
keeps its previous titles, and the first refresh after upgrading escalates to
a full scan to seed the cache.

**Tabs & behaviors.** The nav is split into two concepts. A **behavior** is a
reusable content-type engine that lives in code: `entertainment` (movie/TV),
`courses` (modules/lessons), `podcasts` (channel tiles), plus any private `.py`
plugin dropped into the user directory (never the repo) that declares a small
`SECTION` manifest — label, icon, accent, season/episode nouns, mime families —
and a pure classifier. A **tab** is pure user data (`config["tabs"]`: a list of
`{key,label,icon,behavior,accent?}`) that *picks* a behavior. There are **no
tabs by default**; you create them in Settings (name + emoji + "behaves like"),
assign drives to them, and each tab renders with its behavior's classifier,
accent and vocabulary. So the course-structuring learning is reusable for
*any* tab that points at the `courses` behavior — not tied to a hardcoded name.
`section_for_drive()` returns the tab key or `None` (unassigned = shows in no
tab); nothing falls back to "entertainment" anymore. Existing installs are
seeded once from their old `drive_sections` (see `migrate_config` in `config.py`
and `TABS_REFACTOR.md`) so upgrades are invisible. Audio streams through the
exact same Range proxy as video — mpv just gets a `--force-window` flag so
audio-only playback still has a window. Tabs whose behavior is `entertainment`
additionally get **categories** (Movies / TV Shows / Documentaries / Other) from
the TMDB genres we already fetch for posters — genre 99 is Documentary; a title
TMDB doesn't know lands in Other. TMDB is never consulted for other behaviors
(a course named "Intercourse and Communication" would happily match a film).

**Posters, pre-cached.** During the scan, every title that doesn't already have a
poster is resolved against **TMDB** (movie vs TV, by title+year, if you set an API
key), its w342 poster is downloaded to the posters cache, and the local path is
stored in the record — so tiles load instantly from disk with no per-card lookup.
Because the scan backfills *every* poster-less title (not just newly-added ones),
adding a TMDB key and hitting Refresh fills in your whole existing library.
Negative results are cached too. When TMDB has no answer (no key, or no match —
home videos, obscure releases), the scan falls back to the file's own **Google
Drive thumbnail**: Drive generates one for most videos and hands out a
short-lived URL with each file listing, so drivecast downloads it during the
scan (bumping Drive's default tiny size up to poster size) into the same
posters cache under a stable `dthumb_*` key. Only a title with neither source
gets the clean **gradient placeholder** with the title and year. TMDB is purely
additive.

**Search** is now instant and offline: the home search box filters the cached
library client-side. The old server-side `corpora=allDrives` search (one query
across all drives at Google) still backs the demoted **Browse files** view, along
with the original live folder browser.

**Sorting & grouping.** Above the grid, **Sort** (Title A–Z / Year newest /
Recently added / Recently watched) and **Group** (None / By type / By drive)
dropdowns reshape the library entirely **client-side** over the already-cached
`/api/library` data — no extra API load — and the chosen sort/group persists in
`localStorage`. "Recently added" reads each record's `added_at`; "Recently
watched" joins a small `/api/watched-map` (file id → last-played, including
finished titles the Continue Watching shelf omits); "By drive" maps drive ids to
names via the cached `/api/drives`. Each group renders under its own header,
reusing the same tile grid.

`library.json`, the posters, and the watch-history JSON are the *only* things
drivecast writes to disk (a few MB in `data/`). Video bytes: never.

---

## 7. The one external annoyance: shared rate limits

rclone's built-in Google credentials are shared by every rclone user in the
world, so Google enforces a tiny per-minute query quota on them. Because normal
browsing now serves from `data/library.json`, day-to-day use hits the API **zero
times**; only a scan/refresh talks to Google. That scan is built to survive the
quota: it throttles between calls and, on a `rateLimitExceeded` / 429 response,
**backs off exponentially and retries** rather than crashing — and if a single
folder keeps failing it's skipped so the rest of the scan still completes.

If you routinely scan large drives on the shared credentials you'll still hit the
limit eventually. The real fix is to put **your own** Google OAuth client
id/secret into the rclone remote (a free Google Cloud project), which gives you
your own generous quota. That's configured separately in `rclone config`.

---

## 8. Map of the code

```
drivecast/
├── app.py                 # entry point: checks rclone works, starts the server, opens browser
└── drivecast/
    ├── config.py         # config/data/secrets under ~/Library/Application Support/drivecast (persists across rebuilds)
    ├── rclone_auth.py     # the keymaster: gets fresh tokens out of rclone (section 3)
    ├── drive_api.py       # talks to Google: list drives, browse, search — with rate-limit backoff
    ├── library.py         # the cache: recursive scan/classify drives → library.json, diff, posters (section 6)
    ├── streaming.py       # the stream proxy / relay (section 4) — the heart
    ├── player.py          # finds mpv/IINA/VLC, launches it (with cache/hwdec flags), mpv IPC + VLC HTTP pollers, autoplay queue + should_advance (section 5)
    ├── history.py         # history.json: positions, watched flags, Continue Watching, watched-map, progress
    ├── naming.py          # filename/folder → clean {title, year, season/episode, quality}; lesson numbers, junk filter
    ├── tmdb.py            # poster fetching + caching + genre ids (drives the category chips)
    ├── sections.py        # drive→section assignment, categories, custom private section plugins
    ├── courses.py         # courses classifier: modules/lessons/materials from course drives
    ├── playlists.py       # podcasts classifier: channel folders → shows
    ├── scan_cache.py      # raw per-drive scan records — the per-drive-refresh keystone
    ├── subtitles.py       # English subs: sibling .srt on the drive, or OpenSubtitles (cached locally)
    ├── server.py          # the web API: /api/library, /api/title, /api/refresh, /api/sections, /stream, …
    └── static/            # the library UI: one HTML page, one JS file, one CSS file

Config, data and secrets live outside the tree, in
~/Library/Application Support/drivecast/ (config.json, data/, secrets/secrets.json),
so they survive app rebuilds and the packaged .app can read them.
```

Run it with `python app.py`, and your cloud media library is at
`http://127.0.0.1:8737`.

---

## 9. How you actually use it, day to day

The whole thing boils down to three steps: **start it, browse or search, click
to play.**

**Starting it.** Open a terminal, go to the drivecast folder, and run:

```
./venv/bin/python app.py
```

The terminal prints a couple of status lines (that rclone is OK, and which
player it found), then your browser opens to the library. Leave that terminal
window running in the background the entire time you're watching — drivecast is
the middleman feeding video to the player, so it needs to stay alive. When
you're done for the day, click back to that terminal and press **Ctrl+C** to
stop it. (A movie already open in VLC/mpv keeps playing; you just can't start
new ones once drivecast is stopped.)

**The library.** The first thing you see is a home screen with two things: a
**Continue Watching** shelf across the top (empty at first — it fills in as you
watch), and a row of all 40 of your Shared Drives. It looks like Netflix or
Infuse, not a file browser.

**Watching something.**

1. Click a Shared Drive → you see its folders and videos as cards.
2. Click into folders to drill down; the breadcrumb bar at the top takes you
   back. (A folder with thousands of files shows the first 200 with a "Load
   more" button.)
3. Click a video → your player opens and the movie starts in a few seconds.
   Jump to any point and it re-buffers from there almost instantly.
4. If you've watched part of it before, drivecast first asks **"Resume from
   42:10, or start over?"**

**Finding something fast.** Instead of browsing, type a title into the search
box at the top. drivecast asks Google to search *all* your drives at once, so a
film buried three folders deep in some drive you forgot about shows up in a
couple of seconds. Click the result to play.

**Continue Watching** works on **mpv**, **IINA** *and* **VLC** — each lets
drivecast peek at your current position every few seconds (mpv/IINA over their
control socket, VLC over its HTTP interface), so partly-watched titles reappear
on the resume shelf and pick up where you left off. The shelf looks up each
in-progress file in the cached library, so the card shows the movie's (or, for
an episode, the show's) poster with a progress bar across the bottom and the
clean title — not the raw release filename. mpv stays the recommended
default (it needs no extra interface); on the rare VLC where the HTTP interface
can't start, playback still works, just without the resume shelf for that
session — which is the only reason the app still suggests `brew install mpv`.

**Posters.** Out of the box most tiles get artwork from the file's own **Google
Drive thumbnail** (cached during the scan); anything Drive has no thumbnail for
shows a tidy placeholder card (title + year on a gradient). If you drop a free
**TMDB API key** into `secrets/secrets.json` (see the README), the cards turn
into real movie and show posters — fetched once and cached, so it's fast forever
after. This is purely cosmetic; the app works identically without it.

That's it. There's no library to import, no scan to wait for, nothing syncs. You
run one command and your entire cloud collection is right there to play.

---

## 10. Keeping your data private (why this repo is safe to open-source)

drivecast is deliberately built so the *code* is shareable but *your stuff* never
is. The split is clean:

- **Secrets, config and data live outside the repo**, in a stable per-user
  directory: `~/Library/Application Support/drivecast/`. Your TMDB key sits in
  `secrets/secrets.json` there (or the `DRIVECAST_TMDB_API_KEY` environment
  variable), and it's — importantly — **never written back into `config.json`**,
  so it can't accidentally end up in a committed file. Keeping everything in this
  stable location also means it **persists across app rebuilds** (a py2app rebuild
  no longer wipes your selected drives / library / history) and the packaged
  `.app` can read your key. Only `config.example.json` — a blank template — ships
  in the repo.
- **Your Google login** isn't in drivecast at all. It lives in rclone's own
  config; drivecast only ever borrows a short-lived token at runtime.
- **Your library** — the drive and file names, watch history, posters — all sit
  in `~/Library/Application Support/drivecast/data/`, outside the repo. None of it
  is in git.
- The server only ever listens on `127.0.0.1`, so it's not reachable from the
  network even while running.
- As a backstop, a **pre-commit hook** scans staged changes and refuses to commit
  anything that looks like a key, token, or credential file — so even a slip of
  `git add` can't leak a secret.

The result: the repository contains only the program. Clone it, add your own
rclone remote and (optionally) your own TMDB key, and it's yours — with none of
the original author's data anywhere in it.
