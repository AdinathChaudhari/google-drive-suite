# drive-offload, Explained From the Ground Up

*A layer-by-layer walkthrough of what was built, why it was built that way, and
how every piece works — written for someone with a basic understanding of code.*

---

## 1. The problem we started with

Your Mac's disk is nearly full — **only a few GB of free space left**. You
download large files (shows, archives, anything big) with **Motrix**, a
download manager. You also have access to **Google Shared Drives** — cloud folders that
can each hold 100 GB, and you can create as many as you want.

So the wish was simple to say out loud:

> "While Motrix downloads a file, upload it to a Google Shared Drive at the
> same time, so it never fills up my disk."

To understand what got built, you first need to understand why that exact
wish — *simultaneous* upload of a Motrix download — is physically impossible,
and what the two next-best things are.

---

## 2. Why you can't upload a file while Motrix is still downloading it

Motrix is really a pretty face on top of an engine called **aria2**. aria2 is
fast because of one trick: instead of downloading a file from start to finish
like a browser does, it splits the file into many segments and downloads up to
**64 segments in parallel**, from different parts of the file at once.

To do that, aria2 first creates the full-size file on your disk as an empty
shell (this is called *preallocation*), then fills in segments **out of
order** as they arrive. Halfway through a download, the file on disk looks
like Swiss cheese:

```
[ data ][ empty ][ data ][ data ][ empty ][ empty ][ data ]...
```

Now think about what "uploading it at the same time" would mean. An upload
reads the file from byte 0 onwards and sends it to Google. If you did that
mid-download, you'd be sending Google the Swiss cheese — chunks of real data
mixed with empty holes. The upload would complete, and the file in your Drive
would be **corrupt garbage**. Google's upload protocol also requires bytes to
arrive *in order*, so there's no way to "patch the holes later."

**Conclusion:** with Motrix in the picture, the earliest safe moment to upload
is the instant the download *finishes*. So the project became two tools
instead of one:

1. A tool that reacts **the instant** a Motrix download finishes, uploads it,
   and deletes the local copy — so a file only occupies your disk for the
   minutes it takes to upload. (`offloader.py`, "the watcher")
2. A tool that achieves **true simultaneous download-and-upload** by cutting
   Motrix out of the loop entirely — it streams a download link straight into
   Google Drive so the file **never touches your disk at all**. (`stream-dl`)

Later, a third tool was added when you asked for per-show organization:

3. A tool that lets you say "put these files in a shared drive called
   *My Show S01*" — and if that drive doesn't exist, it **creates it
   automatically**. (`todrive`)

Everything lives in one project folder: `drive-offload/` (wherever you keep
it on your machine).

---

## 3. The foundation everything stands on: rclone

None of these tools talk to Google directly for file transfers. They all
delegate to **rclone**, a famous open-source command-line program (installed
on your Mac via Homebrew) whose whole job is moving files between computers
and cloud storage services. Think of rclone as "a USB cable to the cloud that
you control by typing commands."

Three rclone commands do the heavy lifting in this project:

| Command | What it does | Used by |
|---|---|---|
| `rclone move A B` | Copy A to B, **verify the copy is byte-perfect** (checksums), and only then delete A | the watcher, `todrive` |
| `rclone copyurl URL B` | Download from a URL and upload to B **simultaneously**, streaming through memory | `stream-dl` |
| `rclone backend drives` | List all your Google Shared Drives with their names and IDs | `todrive` |

The "verify then delete" behavior of `rclone move` matters enormously here:
since our tools **delete your local files after upload**, we needed a transfer
method that mathematically proves the cloud copy is identical before anything
gets deleted. rclone does that out of the box.

### How rclone gets permission to touch your Google account

Before rclone can upload anything, you must log it in once. You run
`rclone config`, it opens your browser, you sign in to Google and click
"Allow." Google then hands rclone a **token** — a long secret string that
means "the holder of this string may act on this account's Drive." rclone
saves that token in a config file on your Mac and silently renews it forever.
This is the standard system called **OAuth**, and it's the one step only you
can do (a program can't click "Allow" in your browser for you).

A key design decision: you log in **once**, creating **one** rclone remote
(a saved connection) named `gdrive`. You do *not* create one remote per
shared drive. Section 6 explains the trick that makes one login reach every
shared drive you'll ever create.

---

## 4. Layer one: `stream-dl` — downloads that never touch your disk

**The problem it solves:** on a nearly-full disk, any file bigger than your
remaining free space *cannot* be downloaded to your Mac at all, by any tool.
There's simply no room for it to land.

**The idea:** don't land it. `rclone copyurl` connects to the download link
and to Google Drive *at the same time*, and shovels data from one to the
other through a small buffer in memory — like a bucket brigade. A few
megabytes are in transit at any moment; the file as a whole never exists on
your Mac. A 50 GB file streams through a machine with almost no free space
without issue.

**What the script actually is:** `stream-dl` is a ~60-line shell script (a
script written in the language of the terminal itself). All it really does
is:

1. Take the URL you gave it.
2. Figure out the destination: either you named a drive
   (`--drive "My Show S01"` — see layer three), or you gave an explicit
   destination, or it falls back to the default written in `config.json`.
3. Run `rclone copyurl <url> <destination>` with a progress display, and
   report success or failure.

It's deliberately thin — rclone does 99% of the work; the script just saves
you from remembering rclone's syntax.

**Its limits:** it works for direct download links (an `https://...` URL that
points at a file). It cannot do torrents or magnet links — those need a
torrent engine like Motrix, which is exactly why the watcher exists.

---

## 5. Layer two: `offloader.py` — the watcher that cleans up after Motrix

**The problem it solves:** Motrix downloads (including torrents) must land on
disk first. You want them shipped to a Shared Drive and wiped locally the
moment they finish, without you lifting a finger.

**The shape of the solution:** a **daemon** — a small program that runs in
the background forever, wakes up every 10 seconds, looks at a folder, and
acts on what it sees. It's about 400 lines of Python using nothing but
Python's standard library (no packages to install, nothing to break).

The interesting part is the sequence of checks it runs every 10 seconds.
Each one answers a question, and a file must pass all of them before
anything happens to it:

### Check 1 — "Is this file still downloading?" (the `.aria2` trick)

Remember that aria2 engine inside Motrix? While it downloads
`My.Show.S01E01.mkv`, it keeps a small bookkeeping file right next to it
called `My.Show.S01E01.mkv.aria2` (it stores which segments are done, so a
paused download can resume). The moment the download completes, **aria2
deletes that bookkeeping file**.

That deletion is a perfect, reliable "download finished" signal that Motrix
gives us for free. The watcher simply asks: *does a `.aria2` file with this
file's name exist next to it?* If yes → still downloading (or paused), don't
touch it. If no → possibly done, keep checking. For torrents that arrive as
a whole folder, it also scans inside the folder for any `.aria2` files.

### Check 2 — "Has the file stopped growing?" (the stability window)

Belt and suspenders. The watcher records each file's size on every pass. Only
when the size has stayed **exactly the same for 20 seconds** does it consider
the file settled. This protects against edge cases the `.aria2` trick might
miss — a file mid-copy from another app, a download tool that doesn't use
control files, and so on. (These size records are saved to a small state file
on disk, so even if the watcher restarts, it doesn't lose track.)

### Check 3 — "Is it something we should ignore?"

Files named `.DS_Store` (macOS clutter), or ending in `.part`, `.tmp`,
`.crdownload`, `.download`, `.aria2` — all skipped. Zero-byte files — skipped.

### Then: upload, verify, delete, with a fallback chain

A file that passes every check gets handed to `rclone move`, pointed at the
first Shared Drive in your config. Three outcomes:

- **Success** — rclone confirmed checksums match, deleted the local file.
  Disk space freed. Logged.
- **The drive is full** — Google's error message contains recognizable
  phrases (like `storageQuotaExceeded`). The watcher spots them and
  automatically retries with the **next** drive in your list. That's how
  "multiple 100 GB drives" become one big pool: drive 1 fills up, everything
  starts flowing to drive 2.
- **Any other failure** (Wi-Fi dropped, Google hiccup) — the watcher logs the
  error, leaves your file alone, and won't retry that file for 5 minutes
  (so it doesn't hammer a broken connection). Your file is never deleted
  unless an upload verifiably succeeded.

### The safety rails

A program that *deletes what it uploads* deserves paranoia, so two rules are
hard-coded:

1. **It refuses to watch dangerous folders.** If the config points it at your
   home folder, `/`, `/Users`, `/Volumes`, or even `~/Downloads` itself, it
   prints an explanation and refuses to start. It will only ever watch a
   dedicated folder like `~/Downloads/Offload` — a folder whose entire
   purpose is "things in here get shipped to the cloud."
2. **Deletion is rclone's checksum-verified `move`, never a blind `rm`.**

There's also a `--dry-run` mode (show what *would* happen, touch nothing) and
a `--once` mode (one scan pass, then exit) for testing.

### How it runs "forever": launchd

macOS has a built-in supervisor called **launchd** that starts programs at
login and restarts them if they crash. `install-agent.sh` writes a small
configuration file (a `.plist`) telling launchd "keep `offloader.py` running
at all times," and registers it. `uninstall-agent.sh` reverses it. You run
the installer once; from then on the watcher is just always on, invisibly.

*(Since you decided you'd rather hand-pick what gets uploaded, installing the
watcher is optional. It's there if you ever want the automatic mode.)*

---

## 6. Layer three: `todrive` — shared drives on demand, by name

**The problem it solves:** you didn't want to pre-register drives in a config
file. You wanted: *"when I download a new show, I want a fresh shared drive
named after it, created automatically, and I'll choose what goes there."*

This needed two pieces of engineering.

### Piece 1: one login that reaches every drive (connection strings)

The naive approach is one rclone remote per shared drive — meaning you'd
re-run the browser login dance for every new show. Unacceptable.

The trick: to Google, every shared drive has a unique **ID** (a string like
`0AExampleSharedDriveID`). And rclone has a syntax called a **connection
string** that says "use my saved `gdrive` login, but aim it at this specific
drive ID":

```
gdrive,team_drive=0AExampleSharedDriveID:
```

That whole string acts as a destination, built on the fly — no config edit,
no new login. So `todrive`'s core job is just a lookup: **turn a
human-friendly name into one of these strings.** It runs
`rclone backend drives gdrive:` (which returns every shared drive's name and
ID as JSON), finds the name you asked for, and assembles the string.

### Piece 2: creating drives that don't exist yet (borrowing the token)

Here's the catch: rclone can *list* and *use* shared drives, but it has **no
command to create one**. Creating a shared drive is only possible through the
**Google Drive API** — Google's raw web interface for programs, where you
send an HTTPS request like:

```
POST https://www.googleapis.com/drive/v3/drives
Authorization: Bearer <access token>
Body: {"name": "My Show S01"}
```

...and Google creates the drive and replies with its new ID.

But that request needs a valid access token — and you only ever logged in
through rclone. Solution: **borrow rclone's token.** rclone will print its
saved configuration (including the current token) via `rclone config dump`.
One wrinkle: Google tokens expire every hour, and only rclone knows how to
renew them. So `todrive` uses a small ordering trick: it always runs a real
rclone command *first* (the drive-listing call — which it needs anyway),
because any real use forces rclone to renew and save a fresh token. *Then* it
reads the config and grabs a token guaranteed to be fresh. No duplicate
login, no token management code — rclone remains the single keeper of your
credentials.

(If your Google Workspace admin has API-creation switched off, Google answers
with error 403 — `todrive` recognizes that and tells you in plain English to
create the drive once in the Drive website instead, after which everything
else works normally.)

### What using it looks like

```sh
todrive list                                        # table of all your shared drives
todrive new "My Show S01"                          # just create one
todrive up ~/Downloads/My.Show.S01* "My Show S01" # THE main command
todrive up file.mkv "Drive Name" --keep             # upload but keep local copy
stream-dl "https://..." --drive "My Show S01"      # stream a link into a named drive
```

`todrive up` chains everything in this document together: last argument is
the drive name, everything before it is files/folders → look the name up →
not found? create it via the API → build the connection string → hand each
file to `rclone move` with a live progress bar → verified upload → local copy
deleted → disk space back.

---

## 7. The connective tissue

**`config.json`** — one small settings file shared by all three tools, so
there's a single place to change things:

```json
{
  "watch_dirs":   ["~/Downloads/Offload"],        // what the watcher watches
  "remotes":      ["gdrive1:", "gdrive2:"],       // the watcher's fallback chain
  "base_remote":  "gdrive:",                      // the one login todrive uses
  "stable_seconds": 20,                           // the "stopped growing" window
  "rclone_flags": ["--drive-chunk-size", "64M", "--transfers", "4", ...]
}
```

(The `rclone_flags` tune performance — e.g. upload in 64 MB chunks, move 4
files at once.)

**Global commands** — `stream-dl` and `todrive` were symlinked into
`/opt/homebrew/bin/`, a folder your terminal always searches for commands. A
**symlink** is a signpost, not a copy: the real scripts stay in the project
folder, so improving them there instantly updates the commands everywhere.

---

## 8. How it was all tested without touching Google

Nothing has logged into Google yet (that's your step), so how was any of this
verified? Two techniques, both classics worth knowing:

**Local stand-in.** rclone treats a plain folder path as a valid destination.
So the watcher was tested against a fake drive at `/tmp/offload-test/fake-drive`
— real completed files got moved there and deleted locally (pass), a file
with a `.aria2` sibling was left untouched (pass), a folder moved as one unit
(pass), the watcher refused to watch `~/Downloads` (pass), dry-run touched
nothing (pass).

**A fake rclone.** For `todrive`, a tiny impostor script named `rclone` was
placed first in the search path. When `todrive` called `rclone backend
drives`, the impostor answered with a scripted list of pretend drives; when
`todrive` called `rclone move`, the impostor just wrote the arguments to a
log file. That log proved `todrive` builds exactly the right commands —
correct connection strings, correct handling of duplicate names, correct
create-if-missing behavior — all without any real network traffic. (The one
thing mocks can't prove — Google actually accepting the API call — gets
verified the first time you run it for real.)

---

## 9. How to use it

### Step 0 — the one-time setup (~5 minutes, done once, ever)

**A. Log rclone into Google.** Open Terminal and run:

```sh
rclone config
```

Answer the prompts like this: `n` (new remote) → name: `gdrive` → storage:
`drive` → client id and secret: just press **Enter** (blank) → scope: `1`
(full access) → service account: **Enter** (blank) → edit advanced config:
`n` → use auto config: `y` — your **browser opens**; sign in to your Google
account and click **Allow** → "Configure this as a Shared Drive (Team
Drive)?": **`n`** — this matters: saying no keeps the login flexible, so the
tools can aim it at *any* shared drive on the fly (saying yes would chain it
to a single drive forever).

**B. Install the menu bar app** (this is the "intuitive Mac app" mode — for
most people it's the only interface you'll ever touch):

```sh
cd /path/to/drive-offload
./install-app.sh
```

That's it. The app now starts automatically every time you log in to your
Mac. You'll see its icon in the menu bar (top-right of your screen).

### Everyday use — the menu bar app does the thinking

1. **Start a download in Motrix exactly like you always do.** Nothing about
   your Motrix habits changes.
2. Within a few seconds, a **native macOS dialog** appears:

   > *New download: My.Show.S01E01.mkv — where should it go when it
   > finishes?*
   >
   > **[ Keep on Mac ]  [ Shared Drive… ]**

3. Your three choices, in plain terms:
   - **Keep on Mac** — a completely normal download. Nothing is uploaded,
     nothing is deleted, speeds are untouched. (Ignoring the dialog for 60
     seconds picks this automatically — so an unattended Mac never uploads
     anything by surprise.)
   - **Shared Drive… → pick an existing drive** from the list that pops up.
   - **Shared Drive… → "➕ New shared drive…"** — type a name like
     `My Show S01`, and a brand-new 100 GB shared drive with that name is
     created for you automatically. No Google website, no config editing.
4. Walk away. When the download finishes, the upload starts by itself. You
   get a notification when it begins and another when it's done — including
   how many GB of disk space you just got back (the local copy is deleted
   only after Google's copy is checksum-verified).

**The menu itself** (click the icon) shows: whether Motrix is connected, each
active download with its progress and speed, any upload currently running,
and the last few completed items. Two useful switches live there too:

- **Pause asking** — while on, no dialogs appear and every download is
  auto-kept local. Flip it on when you're doing a batch of ordinary
  downloads and don't want popups.
- **Upload speed limit** (Unlimited / 2 / 5 / 10 MB/s) — caps how much of
  your internet's *upload* lane the transfers may use, so a big upload never
  makes your other downloads or video calls feel slow. See section 9½ below.

### The terminal tools — for when you want manual control

Everything the app does is also a command (the app literally calls these):

```sh
todrive list                                          # see all your shared drives + IDs
todrive new "My Show S01"                            # create a drive, upload nothing
todrive up ~/Downloads/My.Show.S01* "My Show S01"   # upload files/folders there
                                                      #   (creates the drive if missing,
                                                      #    deletes local copies after verify)
todrive up big.mkv "My Show S01" --keep              # same, but KEEP the local copy
stream-dl "https://example.com/big.iso"               # stream a link to the default remote
stream-dl "https://example.com/big.iso" --drive "My Show S01"   # ...into a named drive
```

When to reach for `stream-dl`: the file is **bigger than your free disk
space**. It never lands on your Mac at all — but note it's slower than
Motrix (one connection instead of 64, moving at the pace of your slower
internet lane). Rule of thumb: *fits on disk → Motrix + the app popup; too
big for disk → stream-dl.*

### The optional third mode — the fully automatic folder

If you ever want a "drop zone" where **everything** ships to the cloud with
no questions asked, that's the watcher from section 5:

```sh
./install-agent.sh        # start watching ~/Downloads/Offload forever
./uninstall-agent.sh      # stop
```

Anything that finishes downloading into `~/Downloads/Offload` is uploaded
(to the drives listed in `config.json`) and wiped locally, automatically.
Since you prefer choosing per download, you can simply never install this —
the app and the terminal tools don't need it.

### 9½. Will this slow my downloads? (and how to opt out entirely)

Short version: **downloads and uploads travel in different lanes** —
downloading uses your connection's *downstream*, uploading its *upstream* —
and every upload here starts only *after* its download has finished. So a
download is never fighting its own upload. The only interaction: if a new
download runs while an *earlier* file is still uploading, a completely
saturated upload lane can slightly drag downloads (their small
acknowledgment packets travel upstream). If you ever notice that, set the
menu's **Upload speed limit** to 5 MB/s and it disappears.

And a "simple download" that touches none of this machinery: click **Keep on
Mac** (or turn on **Pause asking**). That download behaves exactly as if
none of these tools existed.

### If something goes wrong

- The app's activity log: **menu → Open log** (or `app.log` in the project
  folder). The watcher logs to `offload.log`.
- "todrive says the remote isn't configured" → Step 0-A hasn't been done yet.
- "Upload failed with `rclone not found on PATH`, and my torrent got stuck
  paused" → this was a real early bug: when launchd starts the menu-bar app it
  hands it a minimal `PATH` without Homebrew, so `todrive` couldn't find
  `rclone`. It's fixed — `todrive` now locates `rclone` on its own (PATH →
  `/opt/homebrew/bin` → `/usr/local/bin`). And a **failed upload now resumes the
  torrent** it had paused (it used to leave it stopped), so a hiccup never leaves
  a download hanging. Your data is safe either way: `todrive` only deletes the
  local copy after a byte-verified upload.
- Google refuses to create a drive (HTTP 403) → your Workspace admin
  disabled API creation; create the drive once at drive.google.com
  (New → Shared drive), and `todrive up` will find it by name from then on.
- Remove everything: `./uninstall-app.sh` and `./uninstall-agent.sh`, then
  delete the project folder. Your rclone login (if you want it gone too):
  `rclone config delete gdrive`.

---

## 10. File map

```
drive-offload/
├── offload_app.py       # the menu bar app: watches Motrix, asks per download (Python, ~690 lines)
├── offloader.py         # the optional auto-watcher daemon (Python, ~400 lines)
├── stream-dl            # zero-disk streaming downloads (shell, ~80 lines)  [global command]
├── todrive              # named shared drives: list/create/upload (Python, ~300 lines)  [global command]
├── config.example.json  # sample settings — copy to config.json and edit
├── config.json          # your personal settings (never committed to git)
├── install-app.sh       # set up + auto-start the menu bar app     ├─ uninstall-app.sh undoes it
├── install-agent.sh     # set up + auto-start the watcher daemon   ├─ uninstall-agent.sh undoes it
├── test_offload_app.py  # headless tests for the app's logic
├── .gitignore           # keeps logs/state/personal config out of git
├── README.md            # quick-start setup guide
├── EXPLAINER.md         # this document
└── app.log / offload.log / decisions.json / ...   # runtime files (created as things run)
```

**The whole project in one sentence:** since a Motrix download can't be
uploaded until it's complete, the project gives you three tools around that
fact — `stream-dl` streams direct links to Google with zero disk use,
`offloader.py` ships completed downloads to the cloud the moment they finish
and frees the space, and `todrive` organizes it all into shared drives
created on demand by name — all built on one rclone login, with
checksum-verified deletes and hard safety rails.
