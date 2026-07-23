# drive-offload — A Case Study

*How a set of tools that keep a nearly-full Mac clear by pushing downloads onto
Google Shared Drives — automatically, from a native menu-bar app that watches
both Motrix and Transmission — got designed and built through orchestrated AI
agents, with Claude **Fable 5** leading and Claude **Opus** implementing.*

This document is deliberately thorough — problem, constraints, the physics that
shaped the design, the architecture, the build process, and the outcome.

---

## 1. The problem

My Mac's disk was chronically nearly full — only a few GB free. I download large
files (large files: Linux ISOs, archives, media) with **Motrix**, and later also with
**Transmission**. I also have **Google Shared Drives** — cloud folders that hold
enormous amounts, and I can create as many as I want.

The wish, simple to say:

> "While I download a big file, get it onto a Google Shared Drive so it never
> fills up my disk — and clear the local copy automatically."

---

## 2. The physics: why you can't upload *while* downloading

Motrix is a friendly face on **aria2**, which is fast because it splits a file
into many segments and downloads up to 64 of them **in parallel, out of order**.
Mid-download, the file on disk is Swiss cheese — real data interleaved with empty
holes. Uploading it then would send Google corrupt garbage, and Google's upload
protocol wants bytes **in order** anyway. So true simultaneous
download-and-upload is impossible with a segmented downloader in the loop.

That physical constraint shaped the whole design. The earliest safe moment to
upload is the **instant the download finishes**. So the project became a small
family of tools:

1. **A watcher** (`offloader.py`) that reacts the moment a download completes,
   uploads it, and deletes the local copy — so a finished file occupies the disk
   only for the minutes it takes to upload.
2. **A true zero-disk streamer** (`stream-dl`) that sidesteps the downloader
   entirely: for a direct download URL, it pipes the bytes straight into Drive
   via `rclone copyurl` — nothing ever lands on the disk.
3. **A per-show organizer** (`todrive`) that addresses a Shared Drive *by name*
   and creates it on demand, so you can say "put this in a drive called *My Show
   S01*" and it just happens.
4. **A native menu-bar app** (`offload_app.py`) that ties it together: it watches
   the download engines live, asks where each new download should go, and runs
   the upload on completion.

---

## 3. The foundation: rclone

None of the tools talk to Google directly for transfers — they delegate to
**rclone**, the open-source "USB cable to the cloud." Three rclone capabilities
do the work:

- `rclone move` (watcher) — copy to Drive, then delete the local source.
- `rclone copyurl` (stream-dl) — stream a URL straight into Drive, zero disk.
- Connection-string addressing — `gdrive1,team_drive=<ID>:` reaches any Shared
  Drive dynamically from a single remote, so you don't configure a remote per
  drive. `todrive` lists/creates drives via the Drive REST API using a token read
  out of `rclone config dump`.

Crucially, only **one** rclone remote is needed. `todrive`, `stream-dl`, and the
watcher all reach any Shared Drive by ID off that single remote. (This same
token-from-rclone trick was later reused wholesale in the sibling `drivecast`
media player.)

---

## 4. How "done" is detected

The watcher knows a download finished without any hooks: aria2 keeps a
`<filename>.aria2` control file next to each in-progress download and **deletes
it on completion**. So the rule is: *no `.aria2` sibling + size stable for a
while = done.* Safe, engine-native, no integration required. There are also
exclusion lists for partial-file extensions (`.part`, `.crdownload`, …) and junk
(`.DS_Store`).

Safety first-class: the watcher **deletes what it uploads**, so it refuses to run
unless pointed at a dedicated subfolder (never `~/Downloads` itself, never `/`,
never home).

---

## 5. The menu-bar app — watching two engines

The most product-like piece is `offload_app.py`, a macOS **rumps** menu-bar app:

- It talks to a download engine's RPC, sees every download live, and when a new
  one appears pops a native dialog: **Keep on Mac** / **Shared Drive…**, and if
  the latter, pick an existing Shared Drive or type a new name (created
  automatically).
- On completion, it uploads the chosen items to the chosen drive and deletes the
  local copy — showing progress and "freed X GB" in the menu.
- It has speed-limit and pause controls, persists decisions, and logs everything.

The engineering is cleanly split so the logic is testable headless: the polling /
decision / upload engine lives in plain classes that take injected callables;
only the rumps UI is impure.

### 5.1 Adding Transmission alongside Motrix
Originally the app only watched Motrix (aria2's JSON-RPC). It was extended to
watch **Transmission** too, at the same time:

- A `TransmissionClient` speaks Transmission's RPC — including its CSRF handshake
  (a first request returns HTTP 409 with a session-id header; store it and retry)
  — and **adapts** each torrent into the same shape the rest of the app already
  understood, so it drops in beside the aria2 client with no downstream changes.
- A `MultiClient` merges both engines: either can be up, both are shown in the
  status line, and each download is routed back to the engine that owns it.
- Torrent lifecycle: before uploading a torrent it's **stopped** (so it isn't
  mid-seed while the file is moved), and after a verified upload the torrent entry
  is **removed** (keeping any remaining local data flag off) so Transmission
  doesn't sit on a "data missing" error.

This was verified end to end against a **real** running Transmission, using a
legal Debian torrent added *paused* (metadata only, zero payload downloaded) to
confirm the adaptation produced the right name, id, and local path — then removed
and cleaned up.

---

## 6. A real bug the project surfaced

A one-line config mismatch — `base_remote` set to `gdrive:` when the actual
rclone remote was named `gdrive1:` — silently broke `todrive`, which made the
menu-bar app's "list existing Shared Drives" come back empty (only "➕ New shared
drive…" showed). Diagnosing it meant reading the app log, reproducing
`todrive list` failing with *"didn't find section in config file"*, and fixing
the one line. A good reminder that the un-glamorous integration details are where
real tools break.

---

## 7. How it was actually built — agents and subagents

drive-offload (and its later extensions) were built through **orchestrated AI
agents** in Claude Code, with a deliberate model-tier split:

- **Claude Fable 5** was the lead — the orchestrator that talked to me,
  inspected the real environment (the actual rclone config, the running
  Transmission, the app logs), made the design calls, wrote precise specs,
  spawned subagents, verified their work, and integrated it.
- **A code-writer subagent powered by Claude Opus** did the multi-file
  implementation from Fable 5's spec — e.g. the entire Transmission integration
  (client + CSRF handshake + MultiClient + torrent lifecycle + tests) landed as
  one well-scoped, tested delivery.
- The work was verified against **real infrastructure**, not mocks alone:
  Fable 5 enabled Transmission's remote access, launched it, added a paused legal
  torrent, and confirmed the adapter's output — then cleaned everything up.

The pattern that repeated: *Fable 5 grounds the design in reality → writes a
tight spec with the real edge cases → an Opus subagent implements and unit-tests
it → Fable 5 verifies live, fixes the gaps, and ships.* The same
custom subagents (a Fable-5 planner tier, an Opus code-writer tier, lighter tiers
for search/mechanical work) were reused across both this project and drivecast.

---

## 8. Outcome

A practical disk-pressure solution: download however you like (Motrix or
Transmission), get asked where each finished download should go, and have it
uploaded to the right Google Shared Drive and cleared locally — plus a zero-disk
URL streamer and an on-demand drive organizer, all off a single rclone remote.
It's a public open-source repo, and the token-authority pattern it proved became
the backbone of a second, larger project (drivecast).

*Tech: Python (stdlib + rumps), rclone, aria2/Motrix RPC, Transmission RPC,
Google Drive REST API, macOS LaunchAgent. Built with Claude Code — Fable 5
orchestrating, Opus implementing.*
