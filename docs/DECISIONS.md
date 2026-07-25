# Decisions & Learnings

An append-only log of things this suite learned the hard way, and what was
done about each one. Code shows *what* the suite does; git history shows
*when* it changed. Neither shows **why a value is that value** — that is what
this file is for.

Two kinds of entry, and the difference matters:

- **Adopted** — we learned something and changed the code to match it.
- **Adapted** — we hit a constraint we could not remove, and shaped the code
  around it. These are the ones worth re-reading before a refactor, because
  the code looks arbitrary until you know the constraint.

A third status, **Open**, means the question has been asked and not yet
answered. An Open entry with a `Revisit when` line is a standing invitation,
not a TODO that rots — it names the exact condition under which the answer
becomes knowable.

## How to add an entry

Copy the template, give it the next `D-NNN`, add a row to the index. Keep
`Where` pointing at real files — an entry whose citation has rotted is worse
than no entry.

```markdown
### D-0NN — Short title
**Status:** Adopted | Adapted | Open · **When:** YYYY-MM-DD

- **Hit** — the problem, symptom, or question that started it.
- **Learned** — what turned out to be true, and how we know (measurement,
  docs, failure). "We assumed X, it was actually Y" is the best shape here.
- **Did** — the change, or the shape we adopted to live with it.
- **Where** — `file:line` for the code this explains.
- **Revisit when** — the condition that would change the answer. Omit if none.
```

## Index

| # | Title | Status | Component |
|---|---|---|---|
| [D-001](#d-001--yt-dlp-native-beats-aria2c-when-nothing-is-throttling) | yt-dlp native beats aria2c when nothing is throttling | Adopted | drive-offload |
| [D-002](#d-002--drive-transfer-concurrency-was-never-measured) | Drive transfer concurrency was never measured | Open | toolkit |
| [D-003](#d-003--snapshot-import-not-git-subtree) | Snapshot import, not `git subtree` | Adapted | suite |
| [D-004](#d-004--drive-offloads-flat-file-layout-is-load-bearing) | drive-offload's flat file layout is load-bearing | Adapted | drive-offload |
| [D-005](#d-005--personal-section-plugins-live-outside-git) | Personal section plugins live outside git | Adapted | drivecast |
| [D-006](#d-006--the-uploader-runs-its-own-rclone-daemon) | The uploader runs its own rclone daemon | Adapted | toolkit |
| [D-007](#d-007--piped-rclone-needs--v-not--p-for-live-stats) | Piped rclone needs `-v`, not `-P`, for live stats | Adopted | drive-offload |
| [D-008](#d-008--disk-is-bounded-by-a-semaphore-not-by-a-size-check) | Disk is bounded by a semaphore, not by a size check | Adapted | drive-offload |
| [D-009](#d-009--a-download-is-complete-by-name-shape-never-by-size) | A download is "complete" by name shape, never by size | Adopted | drive-offload |
| [D-010](#d-010--the-100-gb-shared-drive-cap-is-googles-not-ours) | The 100 GB shared-drive cap is Google's, not ours | Adapted | drive-offload |
| [D-011](#d-011--the-design-tokens-are-vendored-not-shared-at-runtime) | The design tokens are vendored, not shared at runtime | Adapted | suite |

---

### D-001 — yt-dlp native beats aria2c when nothing is throttling
**Status:** Adopted · **When:** pre-monorepo (inherited from the drive-offload repo)

- **Hit** — the obvious assumption was that a multi-connection downloader
  (aria2c, Motrix-style, 16 connections per video) would be faster than
  letting yt-dlp download normally.
- **Learned** — it was the opposite. Native yt-dlp benchmarked **~3–4×
  faster** than aria2c on un-throttled YouTube. Splitting one file across
  many connections only wins when the server is throttling each connection
  individually; when it isn't, the extra handshakes and reassembly are pure
  overhead and all the streams contend for the same pipe you already had.
  Worth being precise about what "native" means here: it is not
  single-streamed. The native branch still sets
  `concurrent_fragment_downloads = 4`. The measured comparison was
  *4 native fragments* vs *16 aria2c connections*, not one vs many.
- **Did** — `--connections 1` (the native branch) is the default. aria2c is
  kept behind `--connections 16` as an escape hatch for the case it actually
  solves: a single connection being throttled to a crawl.
- **Where** — `drive-offload/yt-show:743-765`, `drive-offload/CLAUDE.md:54`,
  and the `--connections` help text at `drive-offload/yt-show:1033`.

### D-002 — Drive transfer concurrency was never measured
**Status:** Open · **When:** 2026-07-25

- **Hit** — given D-001, the natural question: are the toolkit's Drive
  concurrency numbers measured, or inherited? Audit says **inherited**. There
  is no benchmark, no note, and no commit message behind any of them. They
  are rclone's stock defaults with two values nudged — `Transfers` 4→8 and
  `drive_chunk_size` 8Mi→64M — and the suite does not even agree with itself:
  the toolkit uses `transfers 8`, `todrive` and `offloader.py` use
  `transfers 4`, `yt-show` and `stream-dl` use `chunk 128M` while everything
  else uses `64M`. Nobody chose those; they accumulated.
- **Learned (so far)** — D-001's answer does **not** transfer to Drive, and
  the reason matters more than the result. YouTube throttles *per
  connection*, which is what makes splitting a file pay off there. Google
  Drive limits by API quota and by your own pipe, so it is a different
  bottleneck and needs its own measurement. Two separate knobs have to be
  judged separately: parallelism **across files** (`--transfers`) and
  parallelism **within one file** (`--multi-thread-streams` on download,
  chunk size on upload).

  A smoke run on a deliberately tiny payload (32 MiB big file, 6 × 2 MiB
  small files) hinted that across-files parallelism is a real win (1.8–2.6×
  from `transfers 1→4`) while within-one-file parallelism is noise
  (`streams 1→4` and `chunk 8M→64M` both landed inside the error bars). Too
  small to conclude from — at that size the run is dominated by per-file API
  round-trips — but it is the shape the full sweep has to confirm or kill.

  Second learning, about benchmarking itself: the first sweep showed a 23%
  swing between two runs of an **identical** config. Any single-pass result
  on a moving network is indistinguishable from a real effect of that size,
  so the harness now re-runs its first config last as a drift control and
  flags the sweep when the two disagree.
- **Did** — wrote `scripts/bench-transfers.py`: four sweeps, one knob each,
  everything else pinned, a fresh destination per run so nothing benefits
  from a skipped transfer, a drift control per sweep, and a scratch purge
  with `--drive-use-trash=false` (trash still counts against the 100 GB cap —
  see D-010). Nothing in the suite has been changed yet; the values above
  stay as they are until there is a number behind them.
- **Where** — `scripts/bench-transfers.py`;
  `toolkit/src/gdrive_toolkit/common/rclone_rc.py:298-304` (download config)
  and `:335-341` (upload config); `drive-offload/todrive:28`;
  `drive-offload/yt-show:818`.
- **Revisit when** — on a fast connection. At ~10 Mbps up every upload sweep
  is bottlenecked by the pipe, so every setting scores the same and the
  benchmark measures the ISP rather than rclone. Run:

  ```sh
  python3 scripts/bench-transfers.py --drive-name <empty-drive> \
      --big-size 256M --small-count 40 --small-size 4M \
      --repeat 3 --out bench.json --yes
  ```

  Then fold the winning values into `rclone_rc.py`, `todrive`, and `yt-show`
  together, and convert this entry to **Adopted** with the numbers in it.

### D-003 — Snapshot import, not `git subtree`
**Status:** Adapted · **When:** 2026-07-23

- **Hit** — four standalone repos were being merged into one public monorepo.
  `git subtree` is the conventional answer and preserves history.
- **Learned** — preserved history is a liability when the destination is
  public. Any secret ever committed-then-removed in any of the four repos
  would resurface in the merged history, and scrubbing four histories
  reliably is far harder than not importing them.
- **Did** — each component was imported as a `git archive` of HEAD (tracked
  files only). Full history stays in the four original repos, archived
  read-only on GitHub with a pointer README. Every import commit cites its
  source repo's HEAD SHA, so the trail is followable even though the commits
  are not here.
- **Where** — `CLAUDE.md` § "Component map"; the four `import <component> @
  <sha>` commits.

### D-004 — drive-offload's flat file layout is load-bearing
**Status:** Adapted · **When:** pre-monorepo

- **Hit** — drive-offload is a pile of scripts at one directory level. Every
  instinct says restructure it into `src/` and `bin/`.
- **Learned** — the scripts locate their state files and each other by
  sibling path. A conventional package layout breaks both at once, and it
  breaks them at runtime rather than at import, so tests can pass and the
  daemon still fails in the field.
- **Did** — the flat layout is a documented hard rule, not an accident
  awaiting cleanup. It survived the monorepo import unchanged.
- **Where** — `CLAUDE.md` § "Two hard rules"; `drive-offload/CLAUDE.md`.

### D-005 — Personal section plugins live outside git
**Status:** Adapted · **When:** pre-monorepo

- **Hit** — drivecast's classifiers are pluggable, and one of them is a
  personal collection. Plugins naturally want to live next to the code they
  extend.
- **Learned** — "remember not to commit it" is not a mechanism. In a repo
  that is going public, the leak has to be structurally impossible rather
  than merely against policy.
- **Did** — user plugins load from `~/Library/Application
  Support/drivecast/sections/`, outside the repo entirely. drivecast's
  `conftest.py` stubs the user plugin directory so a real personal plugin can
  never be picked up by a test run either.
- **Where** — `CLAUDE.md` § "Two hard rules"; `drivecast/CLAUDE.md`;
  drivecast's `tests/conftest.py`.

### D-006 — The uploader runs its own rclone daemon
**Status:** Adapted · **When:** pre-monorepo

- **Hit** — downloader and uploader both drive rclone over its rc API. One
  shared daemon on one port is the simpler design.
- **Learned** — sharing couples their lifecycles. Quitting the downloader
  tears down the daemon, which kills an in-flight upload that has nothing to
  do with it — a data-losing surprise from an action that looks harmless.
- **Did** — two daemons on two ports: downloader `127.0.0.1:5572`, uploader
  `127.0.0.1:5573`. Slightly more process to manage, no cross-tool kill.
- **Where** — `toolkit/src/gdrive_toolkit/uploader/config.py` (`rc_addr`, and
  the comment explaining the port choice);
  `toolkit/src/gdrive_toolkit/downloader/config.py`.

### D-007 — Piped rclone needs `-v`, not `-P`, for live stats
**Status:** Adopted · **When:** pre-monorepo

- **Hit** — the live upload progress bar had nothing to read. `-P` is the
  documented progress flag and it produced only a single line at the end.
- **Learned** — `-P` renders progress for a terminal. Piped, it collapses to
  one final line. The periodic `--stats` output is a *log* line, so it only
  appears once the log level is raised — which `-v` does.
- **Did** — uploads run with `-v --stats=1s --stats-one-line` and the parser
  reads stats lines separated by carriage returns. Verified against rclone's
  actual piped output, not inferred from the docs.
- **Where** — `drive-offload/yt-show:818-835` (`upload_file`), including the
  comment recording the reasoning.

### D-008 — Disk is bounded by a semaphore, not by a size check
**Status:** Adapted · **When:** pre-monorepo

- **Hit** — yt-show downloads and uploads concurrently. Unthrottled, a fast
  download leg and a slow upload leg fill the disk with staged episodes.
- **Learned** — a free-space check is the wrong instrument: it fires after
  the disk is already nearly full, it races other writers, and the number it
  needs (how big is the next episode) is not known before the download
  starts. Bounding the *count* of in-flight items is checkable up front.
- **Did** — `threading.Semaphore(--max-gap)`, default 3. The producer takes a
  permit before each download; the uploader releases one after each upload.
  At most 3 episodes are downloaded-but-not-yet-uploaded, and downloads
  **pause** when uploads fall behind. State is written only by the uploader
  thread, so a single writer means no lock.
- **Where** — `drive-offload/yt-show` pipeline section;
  `drive-offload/CLAUDE.md:46-53`.

### D-009 — A download is "complete" by name shape, never by size
**Status:** Adopted · **When:** pre-monorepo

- **Hit** — crash recovery has to decide whether a staged file on disk is
  finished or half-written. Comparing size against the expected total is the
  obvious test.
- **Learned** — it is also wrong: a file being actively written passes a size
  check the instant it happens to reach the right length, and the expected
  total is not reliably known. The safe signal is structural — yt-dlp renames
  `.part` to the final name only on completion, and a merge lands via
  `<stem>.temp.mp4` plus an atomic `os.rename`. The name *is* the completion
  flag.
- **Did** — `staged_complete_path` judges completion purely by name shape and
  marker siblings (`.part` / `.ytdl` / `.aria2`, plus the pre-merge `.fNNN.`
  and mid-merge `.temp.` patterns) and never looks at size. One classifier,
  shared by the download success path and the crash-recovery skip, so writer
  and reader cannot drift apart.
- **Where** — `drive-offload/yt-show:706-737`.

### D-010 — The 100 GB shared-drive cap is Google's, not ours
**Status:** Adapted · **When:** pre-monorepo

- **Hit** — uploads started failing on full drives with a quota error.
- **Learned** — Google caps a shared drive at 100 GB **decimal** (not GiB),
  and it is not configurable. It cannot be raised, only routed around. A
  second subtlety: deleted files sit in the drive's trash and still count,
  so "delete to make room" does not, unless trash is bypassed.
- **Did** — the cap is a module constant, not a config key, because a config
  key would imply it is tunable. On a quota marker, `todrive` mints or
  advances to an overflow drive (`<root> overflow`, `<root> overflow 2`, …).
  The quota-marker list there is deliberately **narrower** than
  `offloader.py`'s: a transient `userRateLimitExceeded` must retry the same
  drive, never mint a new one. Anything that bulk-deletes from a drive passes
  `--drive-use-trash=false` — see `scripts/bench-transfers.py`, which would
  otherwise leave several GB of benchmark payload counting against the cap.
- **Where** — `drive-offload/todrive:32-45` (`SHARED_DRIVE_CAP_BYTES`,
  `QUOTA_MARKERS`, `OVERFLOW_SUFFIX`); `scripts/bench-transfers.py` (`purge`).

### D-011 — The design tokens are vendored, not shared at runtime
**Status:** Adapted · **When:** 2026-07-23

- **Hit** — three web apps need one design language. The clean answer is a
  shared static route or a build step that imports one stylesheet.
- **Learned** — the toolkit is pip-installable, and a pip-installed package
  cannot reach a sibling directory in a monorepo it was not installed from.
  Any runtime sharing makes the package non-self-contained; any build step
  makes a CSS tweak require a build.
- **Did** — `design/drivedeck.css` is canonical and gets **copied** into all
  three apps by `scripts/sync-css.sh`. Each copy carries a header comment
  pointing back to the source. Each app's own stylesheet *aliases* its local
  variable names onto the `--dd-*` tokens rather than being rewritten, so
  vendoring never turns into a rename sweep.
- **Where** — `CLAUDE.md` § "Design system"; `design/drivedeck.css`;
  `scripts/sync-css.sh`.
