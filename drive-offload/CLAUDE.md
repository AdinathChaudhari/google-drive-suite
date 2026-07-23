# drive-offload

Uploader/renamer + storage tooling that gets media onto Google Shared Drives
for the Drivecast ecosystem (see `drivecast/CLAUDE.md` for the sibling repos).

## yt-video

`yt-video` is the SINGLE-video sibling to `yt-show`: download one YouTube video
and upload it as a plain file (`<Title>.ext`) to a named Shared Drive — at the
drive ROOT by default, or under `--folder`. No show/season/episode machinery and
no `yt_shows.json` state; it imports the hyphenated `yt-show` module (via
`importlib.machinery.SourceFileLoader`) and reuses its download leg, `upload_file`,
`UI` dashboard, drive picker, and capacity readout as-is. The only `yt-show`
change it needs: `upload_file` now treats an empty folder (`""`) as the drive
root (yt-show itself always passes a real season folder, so unchanged there).

`--movie` cleans the messy YouTube title into a `Name (Year)` filename
(`clean_movie_title`, composing `renamer`'s `_extract_year` / `_NOISE_RE` /
`_clean_show` / `_sanitize` plus a lead-in/quality-word stripper) and, on a TTY,
prompts to confirm or retype it (`--name`/`-y`/`--yes` skip the prompt). It
downloads NO poster/thumbnail: Drivecast's server resolves the image itself — a
TMDB poster from the parsed name+year (`drivecast/tmdb.py` `enrich`), falling
back to Google Drive's auto-generated video thumbnail (`drivecast/drive_api.py`
`fetch_thumbnail`) when TMDB has none. A clean `Name (Year).ext` with no SxxEyy
markers is all the server needs to file it as a movie. Tests: `test_yt_video.py`.

## yt-show

`yt-show` is a sibling CLI to `stream-dl` / `todrive`: it builds a Drivecast TV
show out of a YouTube playlist, downloading each video (via `yt-dlp`) and
uploading it as `<Show> SxxEyy - <Title>.ext` under `<Show> Season N/` on a
named Shared Drive. It reuses `renamer.py`'s canonical naming and `todrive`'s
upload leg rather than duplicating either.

Per-show incremental state (what's already downloaded/uploaded, current
season/episode cursor) lives in `yt_shows.json`, keyed by show name. Episode
numbering is per-video-ID and append-only — re-running against the same or an
additional playlist only adds new episodes; existing numbers never shift.

`assign_new_episodes` (in `yt-show`) handles the assignment/season logic —
`--new-season` vs `--same-season`, auto-rolling to the next season past
episode 100. Canonical naming (`clean_source_title` / `synth_episode` /
`plan_from_sequence`) lives in `renamer.py`; the R13 contract gate rejects bad
folder names, e.g. digit-dash-digit show names like "9-1-1".

Download/upload is pipelined: videos download sequentially while a background
thread does per-file `rclone moveto` into `<Show> Season N/` for the previous
video. Backpressure is a `threading.Semaphore(--max-gap)` (default 3): the
producer acquires a permit before each download, the uploader releases one after
each upload, so at most 3 episodes are downloaded-but-not-yet-uploaded and
downloads PAUSE when uploads fall behind (disk bounded to ~3 videos). State is
written only by the uploader thread, and only after `rclone`'s exit code is 0.
The **native** yt-dlp downloader is the DEFAULT (`--connections 1`) — it
benchmarked ~3–4× faster than aria2c on un-throttled YouTube; `--connections 16`
switches to aria2c multi-connection, which only helps when a single connection
is being throttled to a crawl.

`--pick`, and every run in general, prints live per-drive capacity via
`todrive du --json`.

Live dashboard (`rich`, optional): the `UI` class owns a single `rich.Live`
showing a bold header (`show → drive`), a dim one-line drive-capacity readout,
an overall `episodes N/total` bar, and the concurrent download + upload progress
bars (both visible at once — *N* uploads while *N+1* downloads) with speed/ETA.
rich is a soft dep: a guarded `_import_rich()` sets `USE_RICH`; without rich (or
a non-TTY stdout, or `--plain`) it falls back to terse plain-text progress.
`download_one` feeds the download bar via yt-dlp `progress_hooks`; `upload_file`
feeds the upload bar by parsing `rclone -v --stats-one-line` (the `-v` matters:
piped, rclone's `-P` prints only the final line, so periodic `--stats` must be
raised to log level via `-v`). The `UI` lock guards the board/counters, mutated
from both the producer (download) and consumer (upload) threads.

**Interruption / crash recovery.** `staged_complete_path(out_stem_path)` is the
single classifier used everywhere a "is this file actually done" question comes
up: it globs `<stem>.*`, drops `.part`/`.ytdl`/`.aria2` control files, drops
pre-merge stream fragments (`\.f\d+\.`) and mid-rename merge temp files
(`\.temp\.`), and drops any survivor whose own `.part`/`.ytdl`/`.aria2` sibling
still exists (aria2c writes the real filename with only a `.aria2` sibling
while in progress). Completion is judged by name shape + marker siblings
ONLY — never by size, since a slowly-writing file must not be mistaken for
complete. Among survivors it prefers the exact `<stem>.mp4` (the hardcoded
`merge_output_format`), else the newest by mtime — this also fixed a latent
wrong-file bug in `download_one`'s old success path, which shares this same
helper now (`download_one`'s `except Exception` cleanup no longer deletes
`stem.*`: every artifact under a stem is either resumable state kept for
yt-dlp's default `continuedl=True`, or a complete final that must never be
wiped just because a later step failed).

In `main()`'s producer loop, `staged_complete_path` is checked (after
`gap.acquire()`, so a reused file still occupies a pending-upload slot) BEFORE
calling `download_one` — a complete-but-uncommitted file enqueues straight to
upload with zero yt-dlp/network involvement, which is what makes recovery from
a kill mid-upload (or in the `rc==0` → `save_state` window) both fast and safe
even if the source video has since gone private.

`reconcile_stage(stage, display, show_rec, plan, ui_note)` runs once at
startup, before `ui.live()` opens (it prints plain lines): for every already-
committed episode it recomputes the canonical stem from the stored
season/episode/title and deletes any leftover staged files matching it (state
commits only after `rclone` rc==0, so anything still there is drift residue,
e.g. from a crash between upload and the end-of-run stage cleanup). Anything
else left in this show's season dirs that matches neither a committed stem nor
a stem from the current run's plan is printed as a single warning line and
never deleted — it may belong to a concurrent run or another playlist for the
same show.

A `SIGTERM` handler (installed just before the upload worker starts) raises
`KeyboardInterrupt` in the main thread, so launchd/system shutdown drains
through the identical sentinel + `worker.join()` path as Ctrl-C — unlike
Ctrl-C's process-group SIGINT, SIGTERM doesn't reach the in-flight `rclone`
subprocess, so that upload actually finishes during the drain. Re-uploading is
always idempotent (`rclone moveto` overwrites the existing dest name), so the
one accepted crash window — `rc==0` on upload but killed before the state
commit — costs one redundant re-upload on the next run, never a duplicate on
the drive.
