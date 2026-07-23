# drivecast — A Case Study

*How a personal, Infuse-style media player that streams straight from Google
Shared Drives got designed, built, hardened, and shipped — almost entirely
through orchestrated AI agents and subagents, with Claude **Fable 5** as the
lead engineer, Claude **Opus** as the implementer, and **Sonnet** and **Haiku**
in supporting tiers.*

This document is written to be complete: the problem, the constraints, the
architecture, every major technical decision, the build process, the hard bugs,
and the outcome. It intentionally over-explains.

---

## 1. The problem

I had already solved *storage*: a companion tool (`drive-offload`) pushes my
movies and shows off my nearly-full Mac and onto Google **Shared Drives** (each
can hold enormous amounts, and you can make many). That was great for capacity
but terrible for *watching*. To watch anything I'd have to download it back
first — which defeats the entire point of moving it to the cloud.

So the goal:

> Something like **Infuse or VLC** — browse my Shared Drives as a media library,
> click a poster, and it plays — except it must **stream** the video and
> **never download** it to disk.

Concretely, the wish list grew over the project into:

- A real **library** (movie/show tiles, posters), not a file browser.
- **TV shows** organized into seasons and episodes.
- **Instant playback and seeking** even on 20 GB files.
- **Zero disk footprint** for video.
- **Resume / Continue Watching**.
- **Search** across all drives.
- Later: quality badges, grouping/sorting, shuffle, autoplay, a native menu-bar
  app, and the ability to pick the player.
- Later still: per-drive refresh, movie/show/documentary categories, whole new
  **sections** (Courses, Podcasts, and a private one) with their own classifiers
  and UIs, automatic **English subtitles**, and watching from an **iPhone/iPad**
  over Tailscale with proper HTTPS and token auth.

---

## 2. The key insight that makes it possible

The mental unlock: **there is no real difference between "streaming" and
"downloading."** Netflix downloads video to your device too — it just downloads
only the few seconds you're about to watch, plays them, throws them away, and
asks for the next few seconds.

The mechanism is a standard HTTP feature: the **Range request**. A normal
download says "give me the file." A Range request says "give me bytes
5,000,000–6,000,000." Google Drive's API answers Range requests. That single
capability gives you:

- **Instant start** — the player grabs the first chunk and plays while fetching
  the next.
- **Instant seeking** — jump to the middle → the player just asks for bytes from
  the middle.
- **Zero disk usage** — bytes flow Drive → drivecast → player memory → screen,
  and are gone.

Video players (mpv, VLC) already do all of this over HTTP. So drivecast doesn't
implement a player — it implements the thing a player needs on the other end: a
local URL that answers Range requests with your Drive's bytes.

---

## 3. Architecture

```
┌───────────┐   JSON API   ┌────────────────────┐   Range/206   ┌──────────────┐
│  Browser   │◄────────────►│     drivecast      │◄─────────────►│ Google Drive │
│ (library   │              │  FastAPI on        │  chunks       │   v3 API     │
│  UI, SPA)  │              │  127.0.0.1:8737    │               │              │
└───────────┘              └─────────┬──────────┘               └──────────────┘
                                     │ launches + tracks
                                     ▼
                            ┌────────────────────┐
                            │  mpv / IINA / VLC   │ ◄── plays 127.0.0.1:8737/stream/<id>
                            └────────────────────┘
```

Three ideas do the heavy lifting:

### 3.1 rclone as the "token authority"
drivecast never runs its own Google login flow for day-to-day use. It borrows the
credentials already sitting in the user's **rclone** remote (`gdrive1`). Google
needs a fresh **access token** (they expire hourly); rclone knows how to refresh
and persist it. drivecast:

1. runs `rclone backend drives gdrive1:` — lists the Shared Drives *and*, as a
   side effect, makes rclone refresh + save its token;
2. reads the fresh `access_token` out of `rclone config dump` (the token is JSON
   nested inside JSON — a double-parse);
3. calls the Drive v3 REST API directly with that Bearer token for browsing,
   search, and streaming.

This exact trick was proven first in the sibling `drive-offload` project, so
adopting it was low-risk.

### 3.2 The stream proxy (the heart)
`GET|HEAD /stream/{file_id}` forwards the player's `Range` header **verbatim** to
`https://www.googleapis.com/drive/v3/files/{id}?alt=media`, attaches a fresh
Bearer token, and pipes Google's `206 Partial Content` response straight back —
same `Content-Range`, `Content-Length`, `Accept-Ranges: bytes`. Details that
mattered:

- **Token expiry can never interrupt a movie.** Because playback is thousands of
  small Range requests, each request carries a fresh token; Google validates at
  request start. A 3-hour movie survives the hourly expiry effortlessly.
- **Seeks look like errors but aren't.** Every seek makes the player drop its
  connection and open a new one; the server sees a burst of aborted requests.
  The proxy swallows those (CancelledError / broken pipe) and closes the upstream
  cleanly.
- **httpx lifetime gotcha.** You can't wrap the upstream stream in
  `async with client.stream(...)` around a returned generator — the context exits
  before FastAPI iterates. The fix: build the request, `send(stream=True)`, and
  close in the generator's `finally`.
- Later hardened: a single pooled, long-lived HTTP client (keep-alive, optional
  HTTP/2) and 1 MiB chunks, so a player's many seeks avoid per-request TCP+TLS.

### 3.3 Localhost by default, remote by explicit choice
The server binds `127.0.0.1` by default. It is, in effect, an authenticated
proxy into the user's entire Drive, so it is deliberately not exposed to the
network. Much later an **opt-in remote mode** was added for phones — but only
behind a secret token on every non-local request, with Tailscale as the
recommended transport (see §11).

---

## 4. From file browser to cached library

The first working version browsed Drive live — every click was an API call. Two
problems: it was slow, and on rclone's **shared** OAuth credentials it hit
Google's tiny per-minute quota almost instantly.

The redesign: **scan once, cache everything.**

- A scan walks each *selected* drive's folder tree and produces a structured
  `library.json`: movie/show records with titles, years, posters, and (for shows)
  the full season/episode tree — with each file's size and duration pulled from
  the list response so playback needs no extra metadata call.
- After that, all browsing/search/poster rendering is served from disk: **zero
  API calls**. Only a scan/refresh touches Google.
- The quota problem was fully solved by having the user create their **own Google
  OAuth client id** and pointing rclone at it — swapping the crowded shared quota
  (~a handful of queries/minute) for a private one (thousands/minute). The scan
  code is still quota-resilient (exponential backoff + retry on
  `rateLimitExceeded`/429) so it degrades gracefully.

### 4.1 The classification problem (the deepest rabbit hole)
Real drives are organized in wildly inconsistent ways. Getting one clean tile per
movie and one per show meant handling all of these:

- **Movie folders**: `Paper Lantern (2016)/movie.mkv` → one movie.
- **Collection folders**: `Phase 1/01) Alloy Man (2008)/movie.mkv`,
  `Hollywood/...`, `Dusk Series/...` → the scanner must **recurse into the tree
  until it finds actual films** and surface each as its own tile, ignoring
  bonus-material subfolders (`Featurettes`, `Extras`, …) and stripping enumeration
  prefixes like `01)`.
- **Shows nested under a folder**: `The Branch/Season 1/...` → one show.
- **Shows whose seasons are separate top-level folders**:
  `Grimwold Season 1 S01`, `Grimwold Season 2 S02` → grouped by shared prefix.
- **Whole drives that are one show** with bare `Season 1`, `Season 2` folders →
  grouped under the drive's name (e.g. *Quillson*).
- **Shows split across two drives** (`… (Part 1)` / `(Part 2)`) → merged into one.
- **Noisy season names**: `S01 (2017) 1080p 10bit HEVC NF WEBRip x265 [ENGLISH -
  SPANISH]` and `S05 Part 1 (2021) …` — quality junk had to be stripped before
  the season number could be read (this was the *Vault Rush* bug).
- **Range-named wrappers**: `The Branch Season 1-9 S01-s09` must be left intact,
  not mis-split into a bogus season.

Every one of these was captured as a pure, unit-tested function operating on
synthetic file lists, then verified against the *real* Drive.

### 4.2 Posters, quality, sort/group
- **Posters** are fetched from TMDB during the scan (parse filename → title +
  year → look up movie/tv → download the poster, cache locally), so tiles load
  instantly. No key → clean gradient placeholder cards.
- **Quality pills** (`4K`, `4K HDR`, `1080p`, `720p`, `SD`) parsed from filenames
  and shown on each tile; a show shows its best available.
- **Sort** (Title / Year / Recently added / Recently watched) and **Group**
  (None / Type / Drive) are client-side over the cache and remembered.

---

## 5. Playback, resume, and player choice

drivecast hands the local `/stream` URL to an external player, detected in order
**mpv → IINA → VLC** (or forced via a Settings dropdown). Two design points:

- **Resume / Continue Watching** requires knowing the current playback position.
  mpv/IINA expose a JSON-IPC socket; drivecast polls `playback-time` every few
  seconds and saves it. **VLC** has no socket — but it *does* have an HTTP control
  interface, so drivecast launches VLC with a private loopback HTTP server + a
  random password and polls `/requests/status.xml` for the time. That brought
  full resume support to VLC too.
- **Faster playback** came from giving mpv real network-buffering flags
  (`--cache=yes --cache-secs=30 --demuxer-readahead-secs=20 --hwdec=auto-safe`
  …), pooling the proxy connection, and removing a pre-launch metadata API call.
- **Autoplay + shuffle** for shows: episodes queue up and the next one launches
  automatically when the current finishes (distinguished from an early quit by a
  ≥90%-watched rule); a Shuffle button randomizes the whole show into a queue.

---

## 6. Shipping it like a real app

- A native macOS **menu-bar app** (rumps) runs the server in-process behind a ☁
  icon: *Open drivecast*, *Drives to include* (checkable), *Refresh*,
  *Auto-refresh on launch*, *Quit*. No terminal.
- Packaged into a standalone **`drivecast.app`** with py2app, installable in
  /Applications and launchable from Spotlight. A custom cloud-and-play icon was
  generated programmatically (Pillow → `.icns`).
- A **PATH fix** was needed: a Spotlight-launched `.app` inherits a minimal PATH
  without Homebrew, so it couldn't find `rclone`/`mpv`; the package prepends the
  usual bin dirs on import.
- Config, secrets, library, history, and posters were relocated to
  `~/Library/Application Support/drivecast/` so they **persist across rebuilds**
  and the bundle can read the TMDB key.

---

## 7. Security, and going open-source

The repo is public, so a lot of care went into making sure **only the program**
is in it — never any personal data:

- API keys load from `secrets/secrets.json` (gitignored) or an env var and are
  **never written back** into `config.json`.
- Google credentials live only in rclone, never in drivecast.
- The library cache (drive/file names, watch history, posters) lives outside the
  repo entirely.
- A **pre-commit hook** blocks committing secret-shaped files or values.
- The full working tree *and* git history were scanned for keys, tokens, drive
  IDs, and local paths before publishing; the commit author email was rewritten
  to a GitHub `noreply` address.

A memorable non-bug: at one point it looked like "the code deleted files off my
Drive." A careful audit proved drivecast is strictly **read-only** on Drive
(every call is list/get; the only deletions are local poster/temp files), and a
direct query of Drive's trash showed the deletions were manual, attributed to the
account owner. The scare drove home a permanent guardrail: no delete/trash/move
operations against Drive, ever.

---

## 8. Round two: the app grows sections

Months of daily use produced a second wave of asks, delivered as one batch:
per-drive refresh, proper categories, and three entirely new areas of the app.
This round was run **ultracode-style**: multi-agent workflows for discovery,
design, implementation, and review, with models assigned by job.

### 8.1 Per-drive refresh (the architectural keystone)

"I know which drive I just uploaded to — why rescan all 24?" The naive fix
(rescan one drive, splice its records into the library) is subtly wrong: shows
that span two drives (*Milo in the Muddle (Part 1)/(Part 2)*) are merged into
a single grouped record that cannot be split back apart per-drive. The fix that
is actually correct: every scan writes each drive's **raw, pre-merge records**
into a sidecar cache (`scan_cache.json`); a scoped refresh re-walks only the
chosen drive on the Drive API, then the whole library is **rebuilt from the
cached raw records of all drives** — so cross-drive grouping is recomputed
correctly every time, by construction. Bonus correctness fixes fell out of the
same design: a drive whose scan fails now keeps its previous titles instead of
silently vanishing, and refresh requests that arrive mid-scan queue up instead
of being dropped.

### 8.2 Categories without a web search

"Movies vs TV shows vs documentaries" sounded like it needed a web search per
title. It didn't: the TMDB lookup the scanner already makes for posters returns
`genre_ids`, and genre **99 = Documentary** for movies and TV alike. One field
in an existing API call replaced an entire scraping subsystem. No match at all →
"Other" (home videos, wedding footage), with an optional per-drive hint for
drives that are categorically one thing (a "Documentries" drive).

### 8.3 Discovery before design: agents reading the real drives

Before designing the new sections, a workflow fanned out **five discovery
agents**, each running read-only `rclone` listings against a different group of
real drives — the course drives, a personal media drive, documentaries/
audiobooks/anime, and a sample of everything else. They came back with 13
structured reports: exact folder layouts, numbering conventions (`01)`, `1 -`,
`MasterClass 12`, `EP3`), where the workbook PDFs live, which folders are
wrappers vs containers, non-Latin filenames with combining characters (Unicode
NFC normalization became mandatory for matching), and
booby traps like macOS `._` AppleDouble twin files that Google Drive reports
with *real video mimetypes* (name-based junk filtering became mandatory or
every episode appeared twice).

Two independent **design agents** then produced full architectures — one
data-model-first, one UX-first — and a synthesis agent merged them, resolving
eighteen recorded conflicts (which store holds raw scan records, how tabs
appear, what a category value of `null` means…). That merged design document
became the build plan, milestone by milestone.

### 8.4 The build: parallel Opus classifiers, one shared contract

The three new content classifiers (courses, podcasts, and the private one) are
pure functions over the same walked-folder node shape, so three **Opus
code-writer agents wrote them in parallel**, each owning only its own module and
test file, against a frozen record-shape contract. Courses got modules/lessons
with correct numeric ordering, workbook PDFs as "Materials," per-lesson
resources, cover-image posters, progress rings, and a "Resume course" button
that queues the rest of the course. Podcasts got channel tiles from folders.
The UI grew section tabs with per-section accent colours and vocabulary
(Season→Module, Episode→Lesson) — one render path, per-section configuration.

A **21-agent adversarial review** of the finished diff (security/correctness/
frontend lenses, every finding independently verified) confirmed 15 real bugs —
stale TMDB posters surviving a section reassignment, section changes silently
dropped mid-scan, partial scan failures poisoning the cache, audio playing
headless from the Continue shelf — all fixed with regression tests before
shipping. Live verification on the real drives: 13 courses classified with
modules/lessons/materials, and every per-volume `.m4b` audiobook file matched
to its series (which required discovering that Drive labels `.m4b` files
`application/octet-stream` — an extension fallback fixed it).

---

## 9. A private plugin system (the best feature nobody will see)

One of the new sections is personal, and the requirement arrived bluntly: *it
must never appear in the public repo — code, docs, tests, or commit messages —
but the repo stays public as a portfolio.* The answer turned a constraint into
a feature: a **custom-section plugin system**. Drop a single `.py` file with a
small `SECTION` manifest (label, icon, accent colour, season/episode nouns,
mime families, a pure classifier function) into the private user directory —
the same gitignored home as the secrets — and the app gives it a tab, theming,
shelves, playback, everything, via a `/api/sections` endpoint the frontend
builds its UI from.

The personal classifier moved out of the repo and became the first plugin. Every
mention was scrubbed from tracked files; the end-to-end test now exercises a
synthetic plugin instead; tests were made hermetic against locally-installed
plugins; and because none of the feature commits had been pushed yet, the local
history was **squashed and rewritten so the topic never reached GitHub at
all**. (This very document is the sanitized proof it works: the section you're
reading about remains unnamed.)

---

## 10. Subtitles that find themselves

"Add subtitles" decomposed into a three-source resolution chain, run at play
time and cached forever by video file id:

1. the **local cache** — instant on every replay;
2. a **sibling subtitle file in the video's own Drive folder** (release folders
   very often ship an `.srt` next to the movie) — matched by filename stem,
   `SxxExx` episode marker and language tags, then downloaded once;
3. **OpenSubtitles** — an optional free API key in secrets (same pattern as
   TMDB); search by parsed title + year or season/episode, take the best
   English match.

Whatever wins is handed to mpv/IINA/VLC as a **local file** (`--sub-file`), so
subtitles arrive pre-loaded with zero player-side setup, and playback never
blocks on a lookup (bounded, failure-silent). Live-testing against the real
OpenSubtitles API found two quirks unit tests could never catch: the search
endpoint 301-redirects to a normalized URL (the client must follow), and the
file host intermittently throws Cloudflare 520s (two quick retries land it).
Verified with real downloads: a full SRT for a classic drama and an
episode-exact *Dockside Nine-Nine* S07E02 match.

---

## 11. iPhone and iPad: remote access done carefully

The scariest feature: taking a server that is *deliberately* localhost-only and
letting a phone reach it. It shipped as defense in layers:

- **Off by default.** The toggle changes the bind address only on restart.
- **A secret token on every non-local request** — 128-bit, auto-generated,
  compared in constant time, never logged (uvicorn's access log is disabled
  precisely because tokens travel in URLs). A `?token=` link bootstraps a
  180-day `HttpOnly` cookie; browsers without one get a minimal token page.
  Loopback clients stay tokenless, so the Mac experience is unchanged.
- **`/api/play` refuses non-local clients** — a phone must never launch mpv on
  the Mac. Remote playback uses a new in-browser `<video>` player that reports
  progress to a `/api/progress` endpoint, so Continue Watching and resume stay
  in sync across devices. MKV (which Safari can't play) hands off to **VLC iOS
  or Infuse** via x-callback deep links carrying a tokened stream URL.
- **Tailscale as the recommended transport**, with a QR code in Settings.

The build itself was another model-tiered workflow: two Opus agents implemented
backend and frontend **in parallel against a frozen API contract** (neither read
the other's code), Sonnet wrote the docs, an Opus security lens + Sonnet
correctness lenses reviewed, and Haiku ran the mechanical test sweeps. The
security lens earned its keep: it caught the **secret token being written to
uvicorn's access logs** and an unauthenticated remote request that could crash
pre-auth with a non-ASCII token — plus five more real bugs across the stack.

Reality then delivered the kind of bug no test suite ever will: the phone's
Safari had **HTTPS-Only mode** enabled and refused the plain `http://100.x`
tailnet URL outright. The proper fix, not a workaround: **Tailscale Serve**,
which terminates TLS with a *real certificate* for the Mac's `…ts.net` name and
proxies to drivecast. drivecast learned to detect an active Serve config and
prefer the HTTPS URL in its QR automatically. Verified end-to-end over the
actual tailnet: valid TLS with strict verification, 401 without the token, 200
with it, and a genuine 206 byte-range stream — the exact request a video player
makes. A final touch from usage questions: each connection URL row in Settings
is tappable to show *its* QR (the Wi-Fi one exists for guests without
Tailscale).

A closing **five-model security audit** (Opus on secrets and attack surface,
Sonnet sweeping every tracked file and the complete git history including
commit messages, Haiku running exhaustive pattern greps, adversarial verifiers
on every candidate) found **zero leaks** — no keys, tokens, emails, tailnet
names, IPs, or Drive ids anywhere public — and one preventive gap: the
pre-commit hook didn't yet recognize the newer secret shapes, which was
hardened the same day.

One dependency still rankled: the *home Wi-Fi* URL — the one a phone on the
same network should just use — was plain HTTP, so Safari refused it and
Tailscale remained mandatory even on the couch. The fix made drivecast its
own tiny certificate authority: shelling out to the Mac's **built-in
LibreSSL** (zero new dependencies, nothing for py2app to bundle), it mints a
frozen local root CA plus a rotating server certificate for the Mac's LAN IP
and mDNS name, and opens a second TLS listener beside the loopback one. Trust
the root once on the phone — the Settings card serves the profile and shows
the CA's **SHA-256 fingerprint** to compare before installing, since a
poisoned full-trust root would be strictly worse than plain HTTP — and the
Wi-Fi URL is real HTTPS. The adversarial review pass earned its keep here
too, catching a substring bug that let a cert for `192.168.1.50` claim it
covered `192.168.1.5` (so the cert would never rotate) and the drift when a
long-running Mac roams networks: the UI now detects a certificate that no
longer matches the current address and asks for a restart instead of
advertising a URL that can't work. The implementing agent, testing against a
strict validator instead of trusting the plan, also found the planned openssl
flags produced chains iOS would reject — missing authority-key-identifier
extensions — and fixed them before a single device ever saw a cert error.


---

## 12. How it was actually built — agents and subagents

This is the part worth dwelling on. drivecast was built through **orchestrated AI
agents** inside Claude Code, with a deliberate division of labor across model
tiers:

- **Claude Fable 5** was the lead — the always-on orchestrator that talked to me,
  investigated the real environment, made architectural calls, wrote the plans,
  spawned subagents, verified their output, and integrated everything. Fable 5
  sits in a model tier above Opus and did the reasoning-heavy work: diagnosing
  the quota errors, the classification taxonomy, the config-relocation strategy,
  the security audit.
- **Custom subagents**, configured once and reused throughout:
  - a **planner/architect** tier (Fable-5-powered) for design and analysis,
  - a **code-writer** tier (**Claude Opus**) for the actual multi-file
    implementation — given a tight spec, it wrote working, tested code,
  - lighter tiers for search and mechanical work.
- **Plan mode** was used up front: read-only *Explore* agents fanned out across
  the codebase and the real Drive structure, a *Plan* agent designed the
  approach, and only then was a concrete plan written and approved before any
  code was touched.

The loop that repeated for every feature:

1. **Fable 5 investigates reality first** — it never guessed the Drive layout, it
   went and listed the actual folders (Grimwold, Quillson, Vault Rush, Saga
   Phase 1) and read the real filenames. Grounding the design in real data is why
   the classifier handles so many odd cases.
2. **Fable 5 writes a precise spec** — including the exact real-world examples and
   the edge cases to unit-test.
3. **An Opus code-writer subagent implements it** — often several running in the
   background in parallel for independent pieces.
4. **Verification is adversarial and real** — unit tests on synthetic data *plus*
   a live scan of the actual drives to confirm (e.g. "Vault Rush → one show,
   seasons 1–5, poster resolved"), plus rebuilding the `.app` and booting it under
   a stripped PATH to prove the bundle works, not just the source.
5. **Fable 5 integrates, fixes the gaps the subagent missed** (a stale doc note,
   an over-eager pre-commit regex, a `season_from_folder` that didn't strip
   quality noise), commits in logical chunks, and pushes.

Notable moments that show the agentic workflow paying off:

- The **season-grouping** feature: Fable 5 discovered from real data that "The
  Branch worked but Grimwold didn't" purely because of folder structure,
  explained *why*, designed a grouping pass, had it implemented, and verified it
  against the live drives — Grimwold collapsed from 5 tiles to 1 show.
- The **poster/quota bug**: Fable 5 traced "posters don't show" to the bundle
  reading config from *inside* the `.app` (no key, wiped on every rebuild), and
  fixed it by relocating everything to a stable user directory — a
  non-obvious root cause found by inspecting the running bundle's filesystem.
- The **quota fix**: rather than fight the shared rclone quota, Fable 5 walked
  the user through creating their own Google OAuth client, then drove the rclone
  reconnect (browser OAuth and all) and *proved* the fix by firing 8 rapid API
  calls with zero rate-limits.

By the second half of the project the workflow itself had matured into
repeatable **multi-agent patterns**, invoked explicitly ("ultracode") for the
big rounds:

- **Discovery workflows** — parallel read-only agents mapping the real
  environment (rclone listings of 13 drives) before anything is designed.
- **Design by competition** — two independent architecture agents (data-first
  vs UX-first) plus a synthesis agent that merges them and *records every
  conflict it resolved*, producing a build plan grounded in both correctness
  and experience.
- **Parallel implementation against a frozen contract** — multiple Opus
  code-writers own disjoint files and build to a written API spec, never
  reading each other's code; integration is the lead's job.
- **Adversarial review as a gate, not a formality** — every substantial diff
  goes through lens-specific reviewer agents (security on Opus, correctness
  and frontend on Sonnet) whose findings are then *independently verified* by
  skeptic agents told to refute them. Across the project this pattern
  confirmed and fixed 30+ real bugs that the (eventually 288-strong) test
  suite passed over — including two genuine security issues before the remote
  feature ever shipped.
- **Models assigned by job** — Fable 5 leads and integrates; Opus implements
  and attacks; Sonnet reviews, documents, verifies; Haiku runs the mechanical
  sweeps (test suites, syntax checks, pattern greps). Cost tracks difficulty.
- **Live verification as the last word** — every feature was exercised against
  the real world before shipping: real drives scanned, real subtitles
  downloaded from OpenSubtitles, real TLS-verified byte-range streams over the
  actual tailnet, the packaged .app rebuilt and relaunched.

The throughput this enabled is the real story: a genuinely complex system — a
Range-streaming proxy, a resilient scanner with per-drive refresh, four
content classifiers plus a private plugin system, three player integrations
with position tracking, an automatic subtitle pipeline, token-authenticated
remote access with an in-browser player, a packaged macOS app, and a
security-hardened public release — was built iteratively, each layer verified
against real infrastructure, by a lead model delegating to specialist
subagents.

---

## 13. Outcome

A working, installed macOS app that turns 40 Google Shared Drives into an
Infuse-style media *platform*: pick your drives, assign them to sections
(Entertainment with movie/TV/documentary categories, Courses with modules and
progress rings, Podcasts, plus private plugin sections), and browse posters,
seasons, and episodes. Press play and the video streams (never downloads) to
mpv/IINA/VLC — or to your **iPhone/iPad** over trusted HTTPS (a local CA on
home Wi-Fi, or Tailscale anywhere) with the same
resume and Continue Watching — with English subtitles found automatically,
instant seeking, autoplay, shuffle, and per-drive refresh when you add content.
It's a self-contained `.app`, a public open-source repo audited to contain no
personal data whatsoever, and — notably — a system whose complexity was managed
by AI agents coordinating other AI agents, with the human setting direction and
clicking exactly one Tailscale approval button.

*Tech: Python, FastAPI, httpx, rclone, Google Drive v3 API, TMDB,
OpenSubtitles, LibreSSL (local CA), Tailscale (Serve/HTTPS), mpv/IINA/VLC,
rumps, py2app, qrcode.
Built with Claude Code — Fable 5 orchestrating; Opus implementing and
security-reviewing; Sonnet designing docs and verifying; Haiku sweeping.*
