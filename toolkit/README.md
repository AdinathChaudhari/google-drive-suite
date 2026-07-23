<!--
SEO / GitHub metadata (apply manually in repo settings — not rendered by GitHub):

Topics: google-drive, google-shared-drive, rclone, rclone-gui, download-manager,
        bulk-download, file-upload, file-transfer, self-hosted, web-ui, flask,
        server-sent-events, macos, menubar-app, cloud-storage, google-workspace

About field: Fast bulk download & parallel upload for Google Drive shared drives —
a self-hosted rclone web UI that bypasses Google's slow zip and desktop app.
Localhost-only.
-->

# Google Shared Drive Toolkit (Drivedeck) — fast bulk download, parallel upload & drive control via rclone

**The Google Drive UI Google won't build — parallel transfers, shared-drive control, localhost only.**

<!-- ![demo](docs/demo-placeholder.gif) -->

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Platform: macOS (hub) / cross-platform (CLI web UIs)](https://img.shields.io/badge/platform-macOS%20%7C%20web-lightgrey)

## What this is

Three small tools around one shared engine (`rclone`):

- **`gdrive-download`** — a local web UI to browse a Google Shared Drive, tick a
  tri-state checkbox tree of files/folders, and pull the selection to disk in
  parallel with live progress.
- **`gdrive-upload`** — the mirror image: pick local files/folders with a native
  macOS picker, choose a destination drive + folder, and push them up in
  parallel. Copy-only — it never moves or deletes your local files.
- **`gdrive-hub`** — a macOS menu-bar app that shows install/running status for
  this toolkit and any other tools you register (see [Fork it](#fork-it-making-it-yours))
  and launches them with one click.

All three talk to Google Drive through your own `rclone` remote and your own
OAuth token. Nothing is hosted, nothing phones home.

## Why it exists

Google Drive's own "Download" button zips your selection **server-side,
single-threaded**, with a size cap — for anything more than a handful of files
it's slow and sometimes just fails. The desktop sync client is built for
*one* drive syncing continuously, not "grab this folder from Shared Drive B
right now."

This toolkit skips that path entirely. It drives `rclone copy` directly:
many files (and byte-ranges of large files) transferred **in parallel**,
straight to or from disk, with the original folder structure preserved. Same
account, same permissions — just a faster, scriptable front door.

## Features

- **Parallel bulk download** — tri-state checkbox tree over any Shared Drive,
  live per-file and aggregate progress over Server-Sent Events, cancel
  mid-transfer.
- **Parallel copy-only upload** — native macOS file/folder picker, destination
  drive + folder tree with inline "New folder," collision-safe.
- **One menu-bar hub** — live status (installed / stopped / running) for this
  toolkit's own tools and any others you register; launches or focuses them.
- **Google Docs export handling** — native Google Docs/Sheets/Slides export to
  `docx`/`xlsx`/`pptx`/`svg` on download, or skip them entirely.
- **Works with any rclone remote** — Google Shared Drives are the focus, but a
  bare Drive or non-Drive remote runs in a reduced-feature mode automatically.

## Quickstart

Requires [`rclone`](https://rclone.org/downloads/) installed and at least one
remote configured (`rclone config`) — this toolkit drives your existing
remote, it doesn't set one up for you.

```sh
pip install '.[all]'   # from this toolkit/ dir — not yet published to PyPI

gdrive-setup       # pick which configured rclone remote to use (one-time)
gdrive-download    # opens http://127.0.0.1:8747/ in your browser
```

`gdrive-upload` and `gdrive-hub` work the same way once setup has run. Install
just what you need instead of `[all]`: `[download]`, `[upload]`, or `[hub]`
(macOS only).

## How it works

Each web tool starts one long-lived `rclone rcd` daemon on first launch and
talks to it over its local HTTP RC API for every browse/list/copy/progress
call — no subprocess spawned per click, and the daemon holds the authenticated
session open. A thin Flask app serves the UI and streams progress back to the
browser over Server-Sent Events.

Download and upload run **two separate `rcd` daemons** (`127.0.0.1:5572` and
`127.0.0.1:5573`) rather than sharing one: if they shared a daemon, quitting
one tool while the other had a transfer in flight would kill it out from under
it, since whichever tool started the daemon owns its process. Separate daemons
means either tool can be closed independently without touching the other's
transfers.

The hub (`gdrive-hub`) is a third, independent piece — it never touches the
rclone daemons directly. It shells out to launch the other tools and polls
their ports / launchd state to show status, the same way for this toolkit's
own tools and for anything you add to its registry.

### Built with a multi-agent AI workflow

This toolkit was designed and built end-to-end by three distinct AI roles —
a designer that wrote specs, an orchestrator that verified and merged, and an
executor that wrote code to trace — with a human directing and accepting each
stage. See [BUILD-WITH-AI.md](BUILD-WITH-AI.md) for the process, not a demo.

## Security model

> Not affiliated with, endorsed by, or sponsored by Google LLC. Google Drive™
> and Google Workspace™ are trademarks of Google LLC. This is an independent
> open-source client that interoperates with Google Drive via your own account
> and your own `rclone` installation.

- **Binds `127.0.0.1` only.** No tool in this repo listens on any other
  interface by default. `rclone rcd`'s own `rc_addr` is checked at daemon
  start and refuses to bind anywhere but loopback (127.0.0.0/8, `::1`, or
  `localhost`), regardless of what's in config. There is no hosted service
  and no telemetry — this runs entirely on your machine.
- **The rclone rc daemon is authenticated.** Each daemon this toolkit starts
  gets a random per-session secret (`secrets.token_urlsafe(32)`), passed to
  `rclone rcd` via `--rc-user`/`--rc-pass` and used for every request; the
  secret is written to a `0600` file under the config dir so a second tool
  sharing the same daemon (downloader + uploader use separate daemons/ports,
  but the pattern generalizes) can read and reuse it instead of restarting
  the process. It is never exposed with `--rc-no-auth` and no
  `--rc-allow-origin` is set.
- **Every request to the local web UI is Host/Origin-checked.** Both Flask
  apps reject any request whose `Host` header isn't `127.0.0.1:<port>` /
  `localhost:<port>`, and reject a present `Origin`/`Referer` that doesn't
  match either — a page open in the same browser (DNS rebinding, a
  cross-site form post) can't reach these APIs even before rclone's own auth
  is considered.
- **Auth is your own `rclone` OAuth token**, stored on disk in your `rclone`
  config exactly the way `rclone` itself stores it — which means unencrypted
  at rest. Protect it the way you'd protect any other credential: FileVault
  (or your OS's disk encryption) and normal file permissions on your home
  directory.
- **Upload is copy-only.** It never moves or deletes anything on your local
  disk. **Download is read-only** against the drive — it never deletes or
  modifies anything remotely.
- **Remote access beyond localhost is your job**, not this toolkit's. If you
  want to reach it from another device, put it behind something like
  Tailscale or WireGuard. Don't port-forward these ports to the open internet
  as-is.
- Consider a dedicated **read-only** rclone remote if you mainly use
  `gdrive-download` for browsing — one less way a bug here could ever write
  somewhere it shouldn't.
- **`hub_tools.json` is trusted input.** The hub launches whatever `argv` a
  registry entry (built-in or one you add to `hub_tools.json`) specifies, as
  your own user — treat that file like a shell script you'd write yourself,
  not like data from someone else.

## Fork it: making it yours

This is meant to be forked, not just installed. A few knobs to know about:

- **Register your own tools in the hub** — copy
  [`docs/hub_tools.example.json`](docs/hub_tools.example.json) to
  `~/Library/Application Support/gdrive_toolkit/hub_tools.json` and add an
  entry per tool (web app with a port, or a menu-bar/launchd app). The hub
  reads this file at every refresh — no code changes, no restart needed to
  pick up an edit.
- **Any rclone remote works**, not just Google Drive — point `gdrive-setup` at
  a different remote type and the toolkit falls back to a single-drive mode
  automatically (Drive-specific extras like Google Docs export and abuse
  acknowledgment are skipped for non-Drive remotes).
- **Config lives outside the repo**, at
  `~/Library/Application Support/gdrive_toolkit/` — safe to pull updates
  without losing your settings, and safe to delete without touching the repo.
- Package layout, extras, and entry points are documented in
  [CLAUDE.md](CLAUDE.md) if you're extending it.

## Roadmap

**Shipped:** parallel checkbox-tree download with live SSE progress and
cancel; copy-only upload with native destination picker, inline mkdir, and
collision handling; menu-bar hub with live status that's launchd-aware;
Google Docs export handling and abuse acknowledgment; JSON config per tool.

**Next:** a real transfer queue (multiple jobs, not strictly sequential);
resumable / re-attachable transfers across a browser refresh; in-UI search
across drives; merging download + upload into one two-tab app; support for
additional remote types beyond Google Drive (S3, Dropbox, etc.) as first-class
options, not just the fallback mode.

**Vision:** streaming browse of media without downloading first (see
[drivecast](#related-projects) for what that looks like as its own app);
share-link management from the UI; an optional web-hosted mode behind real
auth for access from anywhere; a PWA/mobile-friendly UI; a headless JSON API
for scripting against.

## Related projects

This toolkit is the control plane; these siblings are what it controls.

**Streaming** (paired — server + Fire TV client):

- [**drivecast**](../drivecast/README.md) — turns a
  Shared Drive into a cached media library (movies/TV, courses, podcasts) and
  streams it on demand to mpv/IINA/VLC/Infuse — "your personal Netflix,"
  nothing ever written to disk.
- [**drivecast-app**](../drivecast-app/README.md) — a
  native Android TV / Fire TV client for drivecast: browses the library and
  plays over its streaming endpoint from the couch, with resume and
  autoplay-next-episode.

**Ingest:**

- [**drive-offload**](../drive-offload/README.md) —
  watches a downloads folder and automatically pushes completed files up to a
  Shared Drive to keep a nearly-full local disk clear, plus a direct
  URL-to-cloud streaming mode that never touches local disk at all.

Each sibling is a standalone project you can adopt independently — this
toolkit's hub auto-detects and launches all three when checked out inside
this suite, and can launch and show status for any other tool you register
(see [Fork it](#fork-it-making-it-yours)).

## Trademark

Not affiliated with, endorsed by, or sponsored by Google LLC. Google Drive™
and Google Workspace™ are trademarks of Google LLC. This is an independent
open-source client that interoperates with Google Drive via your own account
and your own `rclone` installation. No Google branding, logos, or trade dress
are used in this project.

## License

MIT — see [LICENSE](LICENSE).

---

Part of the [Google Drive Suite](../README.md).
