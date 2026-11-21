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
| [D-012](#d-012--keepalive-true-silently-breaks-every-quit-button) | `KeepAlive: true` silently breaks every Quit button | Adopted | suite |
| [D-013](#d-013--selected-drive-order-is-data-not-presentation) | Selected-drive *order* is data, not presentation | Adopted | drivecast |
| [D-014](#d-014--never-key-a-saveablestateprovider-on-state-you-want-to-reset) | Never key a `SaveableStateProvider` on state you want to reset | Adapted | drivecast-app |
| [D-015](#d-015--on-android-a-cancelled-focus-enter-consumes-the-d-pad-press) | On Android, a cancelled focus-enter *consumes* the D-pad press | Adapted | drivecast-app |
| [D-016](#d-016--bring-into-view-follows-the-focus-target-not-the-tile) | Bring-into-view follows the focus target, not the tile | Adopted | drivecast-app |
| [D-017](#d-017--a-tab-less-drive-silently-disabled-per-drive-refresh) | A tab-less drive silently disabled per-drive refresh | Adopted | drivecast |
| [D-018](#d-018--focusrestorer-over-a-lazy-lane-kills-the-process-not-the-keypress) | `focusRestorer` over a lazy lane kills the process, not the keypress | Adapted | drivecast-app |
| [D-019](#d-019--forgetting-a-decision-means-scrubbing-it-not-deleting-it) | Forgetting a decision means scrubbing it, not deleting it | Adopted | drive-offload |
| [D-020](#d-020--a-season-by-season-torrent-is-copied-as-it-finishes-moved-only-at-the-end) | A season-by-season torrent is copied as it finishes, moved only at the end | Adopted | drive-offload |
| [D-021](#d-021--each-season-folder-is-freed-once-it-is-verifiably-on-the-drive-then-the-next-one-starts) | Each season folder is freed once it is verifiably on the drive, then the next one starts | Adopted | drive-offload |
| [D-022](#d-022--image-only-english-tracks-stay-uncovered-until-a-device-test) | Image-only English tracks stay uncovered until a device test | Open | drivecast |
| [D-023](#d-023--one-subtitle-provider-anonymous-tier-no-stored-credentials) | One subtitle provider, anonymous tier, no stored credentials | Open | drivecast |
| [D-024](#d-024--the-same-show-on-two-drives-merges-by-normalised-title--year-after-group_seasons) | The same show on two drives merges by normalised title + year, after `group_seasons` | Adopted | drivecast |
| [D-025](#d-025--infuse-cannot-be-told-a-language-so-it-keeps-the-sidecar-path) | Infuse cannot be told a language, so it keeps the sidecar path | Adapted | drivecast |
| [D-026](#d-026--extracting-an-embedded-subtitle-reads-the-whole-file) | Extracting an embedded subtitle reads the whole file | Adapted | drivecast |
| [D-027](#d-027--editing-an-infoplist-inside-a-signed-bundle-breaks-tcc-weeks-later) | Editing an `Info.plist` inside a signed bundle breaks TCC, weeks later | Adapted | suite |
| [D-028](#d-028--vlc-software-decodes-light-h264-on-the-fire-tv-stick) | VLC software-decodes light H.264 on the Fire TV Stick | Adapted | drivecast, drivecast-app |
| [D-029](#d-029--embedded-first-subtitles-index-before-fetch) | Embedded-first subtitles: index before fetch | Adopted | drivecast |
| [D-030](#d-030--subtitle-bytes-are-validated-at-the-single-write-point) | Subtitle bytes are validated at the single write point | Adopted | drivecast |
| [D-031](#d-031--android-vlc-per-item-xspf-subtitles-still-unmeasured) | Android VLC per-item XSPF subtitles: still unmeasured | Open | drivecast-app |
| [D-032](#d-032--a-backfill-slot-is-spent-only-when-the-provider-was-really-called) | A backfill slot is spent only when the provider was really called | Adopted | drivecast |

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

---

### D-012 — `KeepAlive: true` silently breaks every Quit button
**Status:** Adopted · **When:** 2026-07-29

- **Hit** — quitting either menu-bar app (drive-offload's ⬆/☁ icon or the
  hub's 🧰) from its own **Quit** menu item did nothing observable: the icon
  vanished for a moment and came straight back. It read as a bug in the apps'
  quit handling, and separately made a wedged drive-offload unrecoverable
  without a terminal.
- **Learned** — nothing was wrong with either app. Both `_quit` callbacks call
  `rumps.quit_application()`, which is a clean **exit 0**; both LaunchAgents
  set the bare `KeepAlive: true`, which relaunches on *any* exit including a
  deliberate one. launchd was overriding the user, and neither app could have
  detected or prevented it. Switching to the dict predicate
  `KeepAlive = {SuccessfulExit: false}` restricts relaunch to unsuccessful
  exits. Verified with a throwaway probe agent rather than from the man page,
  because the third case below is unintuitive and is the one that bites:

  | Exit path | Starts observed | Behavior |
  |---|---|---|
  | clean `exit 0` (the Quit item) | 1 | stays down ✅ |
  | `exit 3` (a crash) | 3 | auto-heals ✅ |
  | `launchctl kill SIGTERM` | 2 | **relaunched** ⚠️ |

  Two timing traps make this easy to "verify" wrongly: launchd throttles
  respawns to ~10 s apart, so a 6-second observation window shows one start
  for *every* case and looks like a pass; and a probe that exits after 1 s is
  already gone before a SIGTERM sent at t+2 can land on it.
- **Did** — dict-form `KeepAlive` in all three generated/templated plists.
  The SIGTERM row is why `hub_core.stop()` uses **`launchctl bootout`** (which
  removes the job from the domain, so no predicate is left to evaluate, and
  which escalates to SIGKILL on its own) and never `launchctl kill` — a kill
  would have reintroduced the exact same "I quit it and it came back" bug from
  the outside. `bootout`'s mirror is `bootstrap`, which `launch()` already did
  for an on-disk-but-unloaded plist, so stop/start compose with no new state.
- **Where** — `drive-offload/launchd/com.driveoffload.app.plist`,
  `drive-offload/install-app.sh`, `toolkit/install-hub.sh`;
  `toolkit/src/gdrive_toolkit/hub/hub_core.py` § "stop / restart".
- **Revisit when** — a menu-bar app needs to stay dead across a *reboot* too.
  `bootout` only lasts until the next login, since `RunAtLoad` bootstraps the
  agent again; a persistent off switch would need `launchctl disable`, which
  writes to a system-wide override database and is a different tool.

---

### D-013 — Selected-drive *order* is data, not presentation
**Status:** Adopted · **When:** 2026-08-01

- **Hit** — making "add a drive" rescan only that drive (instead of the whole
  library) meant replacing an order-sensitive `new_drives != old_drives` check
  in `POST /api/settings` with a set diff of added/removed ids. That silently
  reclassified a pure **reorder** of `selected_drives` as a no-op: config was
  saved, nothing was rebuilt. The comment written to justify it claimed drive
  order "isn't meaningful downstream, only membership".
- **Learned** — that claim was false, and a probe against the real code
  disproved it. `Scanner.scan` builds `all_records` with
  `for drive_id in selected` — *list* order — and `group_seasons` merges
  same-named seasons across drives **first-seen-wins** through its `order`
  list. Two drives each holding `The Ladle Season 1` / `Season 2` produce, for
  order `[A, B]`: `drive_id=drvA`, `year=2019`, `_thumb=th_drvA`,
  `source_drives=[drvA, drvB]`; for `[B, A]`: `drvB`, `2022`, `th_drvB`,
  `[drvB, drvA]`. The group **id** is stable, so nothing looks broken — but the
  merged record's owning drive, year and poster source all flip. Reordering is
  a real content change that happens to walk zero drives.
- **Did** — `order_changed` is tracked separately from added/removed and falls
  through to the **cache-only rebuild** (`scope=[]`): no Drive walk, but the
  library is re-derived so the merge resolves in the new order. Removals take
  the same path — that is what makes a deselected drive's titles leave without
  touching Drive. The general rule this bought: a scoped refresh answers "what
  must be re-*walked*", never "whether a rebuild is owed"; those are separate
  questions and the settings route now dispatches on both.
- **Where** — `drivecast/drivecast/server.py` § `api_post_settings` (the
  `added_drives`/`removed_drives`/`order_changed` block and the refresh
  dispatch at the end); `drivecast/drivecast/library.py:1370` (`for drive_id in
  selected`), `library.py:543-650` (`group_seasons`, `order` at `:556`/`:592`);
  `AppState.start_refresh`'s three scope modes in `server.py`.
- **Revisit when** — the library gains an explicit per-drive priority field. A
  real priority would make list order presentational again, and *then* a
  reorder could legitimately skip the rebuild.

### D-014 — Never key a `SaveableStateProvider` on state you want to reset
**Status:** Adapted · **When:** 2026-08-01

- **Hit** — the Fire TV home grid's new Sort/Group controls reorder every tab's
  list at once, but each tab holds its own `rememberSaveable` `LazyGridState`
  inside `tabStateHolder.SaveableStateProvider(idx)`. After a reorder, an
  off-screen tab's saved scroll offset points into a list that no longer
  exists. The obvious fix — fold a `reorderEpoch` into the provider key so a
  pick invalidates every tab's saved state — compiled, passed its unit tests,
  and broke two things that no JVM test could see.
- **Learned** — `SaveableStateProvider` calls
  `Composer.startReusableGroup(reuseKey, key)`, i.e. **`ReusableContent`**
  semantics: on a key change the group is *reused*, every `remember` recomputes,
  and LayoutNodes are reused via `resetModifierState()` / `onReset()`.
  `FocusTargetNode.onReset` force-clears focus when its state was
  Active/Captured — so every Sort pick reset the tab's `FocusRequester`s and the
  focus of the very pill the user had just pressed, leaving the D-pad dead
  (nothing re-requests focus: the initial-focus effect is gated by a
  `focusedOnce` flag that is already true). Second failure: the epoch was a
  plain `remember` while every state keyed on it was `rememberSaveable`, so a
  back-nav or process death reset the epoch to 0 while the Bundle still held
  entries saved under epoch 1 — the grid lost its restored position, and a
  later pick re-hit the stranded entry and restored a *stale* offset.
- **Did** — the provider is keyed on `idx` **alone**. The epoch survives only
  as a `LaunchedEffect` key that `removeState()`s the tabs that are *not* on
  screen; the on-screen tab keeps its live subtree (and its focus) and is
  snapped to top by its own `snapshotFlow` effect. Two guards fell out of the
  lifetimes: the effect no-ops while `reorderEpoch == 0`, so a fresh mount
  can't wipe just-restored state, and `maxTabCount` is `rememberSaveable`
  (matching the saveable holder it guards) so a tab list that shrank while the
  app was dead can't strand an entry above a freshly-initialised high-water
  mark.
- **Where** — `drivecast-app/app/src/main/java/com/drivecast/tv/ui/home/HomeScreen.kt`
  — `reorderEpoch` (`:147`), the purge `LaunchedEffect` + `maxTabCount`
  (`:340`), `tabStateHolder.SaveableStateProvider(idx)` (`:485`).
- **Revisit when** — the app moves to Compose ≥1.8 / a tv-material that changes
  `focusRestorer`. The focus-reset half of this is a Compose 1.7 behavior, and
  the pinned-BOM constraint in `drivecast-app/CLAUDE.md` is what keeps it
  stable; verify against the new runtime before trusting the reasoning above.

### D-015 — On Android, a cancelled focus-enter *consumes* the D-pad press
**Status:** Adapted · **When:** 2026-08-01

- **Hit** — on any home tab with a Continue Watching shelf, the D-pad chain
  skipped lanes: DOWN off the continue card landed on a grid tile instead of
  the chips row, and UP off the first tile row jumped past both the chips row
  and the shelf straight to the tab bar. A tab with no shelf was perfectly
  symmetric, which made it look intermittent. Measured on the real stick by
  driving `adb shell input keyevent` and dumping the focused node per press;
  reproduced identically on the commit before the sort/group work, so it was
  not a regression from that.
- **Learned** — two framework behaviors in the pinned Compose 1.7.2, both
  contrary to what `FocusKit.kt`'s own comment assumed:
  1. `AndroidComposeView`'s key-input path is
     `focusSearch(dir, rect) { it.requestFocus(dir) ?: true } ?: true`. A
     **Cancelled** custom-enter result is `null`, which is coerced to `true` —
     "handled". So returning `FocusRequester.Cancel` from a `focusRestorer`
     enter lambda does not fall through to the next candidate and is not a
     "harmless no-op": it consumes the press and strands focus wherever the
     enter lambda's own `requestFocus()` probe last committed it.
  2. `TwoDimensionalFocusSearch.searchChildren` *removes* a deactivated
     candidate group from the candidate set when its subtree search yields
     nothing, then continues to the next candidate. A lane that is mid-scroll
     or recycled presents exactly that shape — its group node still passes the
     `isAttached` filter while its children fail `isPlaced && isAttached`.
  The shelf is what arms both: it is ~596px tall, so with the `0.30f` pivot one
  or two DOWN presses push the header stack out of the composed window exactly
  when a search tries to enter it. A shelf-less tab keeps its controls row as
  the topmost grid item, which holds start focus and therefore has a *saved*
  restorer hash — the success path, which works.
- **Did** — `tvFocusRestorer` no longer returns `Cancel` on a failed probe, and
  both header lanes (shelf `LazyRow`, controls `Row`) moved off `tvFocusRestorer`
  onto `tvFocusEnterFallback`, which re-resolves its target live. On top of that,
  three UP/DOWN hops are wired explicitly via `tvDpadHop` rather than trusting
  focus search: shelf→controls, controls→shelf, and first-tile-row→controls (the
  last two scroll the target back into view first). The pure decision table lives
  in `ui/home/FocusLanes.kt` with 14 unit tests. Verified on-device afterwards:
  the full chain is symmetric in both directions and every press moves focus.
- **Where** — `drivecast-app/app/src/main/java/com/drivecast/tv/ui/common/FocusKit.kt`
  (`tvFocusRestorer`, `tvDpadHop`, `PositionFocusedItemInLazyLayout`),
  `ui/home/FocusLanes.kt`, `ui/home/HomeScreen.kt` (the three hop call sites).
- **Revisit when** — the compose BOM moves off 2024.09.x. Both behaviors above
  are 1.7.2 specifics read out of that exact AAR; `tvDpadHop` is a workaround
  whose whole reason to exist may evaporate (or change shape) on a newer
  foundation. Re-run the on-device matrix before trusting either way. Also note
  the standing risk a review raised and the device did not reproduce:
  `requestFocus()` returns `void` in 1.7.2, so a hop that consumes a press
  cannot actually prove focus moved — every path tested moved focus, but a
  future lane inserted between shelf/controls/tiles could turn that into a dead
  press if its resolvers are not updated.

### D-016 — Bring-into-view follows the focus target, not the tile
**Status:** Adopted · **When:** 2026-08-02

- **Hit** — on the last row of the home grid, a tile's name and year were
  unreachable: scrolled all the way down, the poster sat flush with the bottom
  edge and its labels stayed underneath it, at every scroll position. Reported
  as "you cannot even see the name of the movie in last tile".
- **Learned** — it is not a height budget being exceeded, which is why two
  plausible fixes both failed. The focus target is the poster `Card`, while
  `LibraryTile` puts the name and year *below* it as siblings in the same
  `Column`. `PositionFocusedItemInLazyLayout`'s spec opens with
  `if (offset >= 0f && offset + size <= containerSize) return 0f` — and `size`
  is the **requesting node**, the poster. Once the poster is fully on screen
  the grid is told no scrolling is needed, and the labels are never in the
  calculation at all. Two attempts that only changed the odds:
  - Raising the grid's bottom `contentPadding` 48dp → 96dp. Measured: the name
    came into view, the year still clipped (1075..1080 of 28px needed).
  - Shrinking the tile. Measured across three sizes, each clipping a *different*
    amount — 160dp cut name+year, 132dp showed the name and cut the year, and
    124dp cut the name again at 13px of 33. Smaller tiles just relocate where
    the poster's bottom lands relative to the screen edge.
  A smaller *font* fails the same way and fails arithmetic too: the measured
  deficit was 23px, and dropping name/year from 14/13sp to 12/11sp recovers
  about 9px, to an unreadable 10/9sp about 18px — while 12sp is the floor of
  the type scale for a 10-foot UI.
- **Did** — a `BringIntoViewRequester` on `LibraryTile`'s `Column`, triggered
  from the poster's `onFocused`, so the request carries the whole tile's
  height. Deliberately not a scroll-behaviour change in the common case: the
  same spec still returns `0f` when the Column is already fully visible, so
  only an edge row ever moves. Verified on the stick — zero clipped labels,
  last row reads poster 626..998, name 1010..1043, year 1043..1071.
- **Where** — `drivecast-app/app/src/main/java/com/drivecast/tv/ui/home/HomeScreen.kt`
  (`LibraryTile`), `ui/common/FocusKit.kt` (`PositionFocusedItemInLazyLayout`,
  the `return 0f` short-circuit).
- **Revisit when** — a tile grows a third line, or the focus treatment moves to
  wrap the labels. Either changes which node should own the request; the rule
  to keep is that whatever asks for bring-into-view must be the thing you
  actually need on screen.

### D-017 — A tab-less drive silently disabled per-drive refresh
**Status:** Adopted · **When:** 2026-08-03

- **Hit** — after per-drive refresh scoping shipped ([D-013](#d-013--selected-drive-order-is-data-not-presentation)),
  refreshes were still walking all 35 drives. Separately, ticking a new drive in
  Settings and saving appeared to do nothing at all.
- **Learned** — one root cause behind both. A drive with **no tab assignment**
  classifies into nothing: it is walked, produces no records, and therefore never
  earns a `scan_cache` entry. `Scanner.scan`'s escalation guard treated any
  uncached selected drive as "would lose its titles in a rebuild" and escalated
  the scope to every selected drive — so a single unassigned drive turned *every*
  scoped refresh, per-drive button included, into a full re-walk, permanently,
  because walking it again could never produce the cache entry that would end the
  escalation. The server had been reporting the cause all along in
  `/api/refresh/status.warning` ("N drive(s) need a tab assignment in Settings")
  and **no client displayed it**. The web Settings compounded it: the tab picker
  was only built for drives *already* included (`openSettings`), so a drive ticked
  in the current visit never showed one, and `saveSettings`' "choose a tab for
  every included drive" guard — which walks `select.drive-section` — never saw the
  row. The drive saved unassigned, scanned, and vanished into nothing.
- **Did** — three changes, one per layer. The escalation guard now exempts drives
  that resolve to no section (`sections.section_for_drive(...) is None`), matching
  what `maybe_autorefresh` already did; such a drive has no titles to lose, so the
  guard was protecting nothing. The tab picker is built for every drive row and
  revealed the moment its checkbox is ticked, focus moving to it, so the blocked
  save can actually be acted on — and the block now names the offending drive.
  `pollScan` surfaces `status.warning`. Two tests pin the guard: a tab-less
  uncached sibling must NOT escalate, an assigned uncached sibling still must.
- **Where** — `drivecast/drivecast/library.py` § `Scanner.scan` (the `needs_cache`
  filter), `drivecast/drivecast/static/app.js` § `openSettings` / `saveSettings` /
  `pollScan`, `drivecast/drivecast/test_library.py` §
  `test_tabless_drive_does_not_escalate_a_scoped_scan` and
  § `test_assigned_uncached_sibling_still_escalates`.
- **Revisit when** — drives get a default tab again. A zero-default tab model is
  what makes "selected but classifies into nothing" reachable at all; restore a
  fallback and this whole failure mode disappears with it.

---

### D-018 — `focusRestorer` over a lazy lane kills the process, not the keypress
**Status:** Adapted · **When:** 2026-08-06

- **Hit** — on the Fire TV Stick, opening one particular show (call it **show A**,
  a series whose `seasons[0]` is a Season 0 of specials), stepping LEFT off the
  episode list onto the season list, and picking Season 1 killed the app every
  single time. Movies were fine, and so was every other show tried, which made it
  look like bad metadata for one title. It wasn't: show A's `/api/title` is clean —
  every season populated, no duplicate `file_id`s, no nulls.
- **Learned** — `Modifier.focusRestorer` (wrapped as `tvFocusRestorer`) does more
  than remember a child. When focus *leaves* the lane, `FocusRestorerNode` PINS
  the focused child through the lazy layout's `PinnableContainer`, and releases
  that pin again from its own `onDetach`. Tear the pinned lane down while the pin
  is still live and the pin is released twice — `LazyLayoutPinnableItem.release`
  does `check(pinsCount > 0)` and throws
  `IllegalStateException("Release should only be called once")` straight out of
  the measure/layout pass. That is an uncatchable process kill, a whole different
  failure class from the swallowed-D-pad-press problem `tvFocusRestorer`'s
  `onRestoreFailed` was built for (D-015). The R8-obfuscated stack retraced
  against `mapping.txt` says it exactly:
  `LazyLayoutPinnableItem.release <- FocusRestorerNode.onDetach <- LayoutNode` detach
  during `RemoveNode`. The season `Crossfade` is that teardown; stepping LEFT onto
  the season pills is what leaves the pin live.
  **Show A is not special — its season *shape* is.** Its Season 0 of specials is
  `seasons[0]`, so detail opens *on the specials* and the LEFT-then-switch-season
  detour is the only way to reach Season 1. Every other show opens on the season
  you wanted, so nobody switched, so nobody pinned. Any show would have crashed
  the moment you changed season; a season-0 show just makes it unavoidable.
- **Did** — both of DetailScreen's lazy lanes moved to `tvFocusEnterFallback`,
  which resolves an enter target from live state and never pins anything, so the
  double release is structurally impossible rather than merely unlikely. To keep
  what the restorer was actually good for, the lanes now own explicit per-item
  `FocusRequester` lists: the season lane enters on the *selected* pill, and the
  episode lane enters on the last-focused row (reset on season change) falling
  back to the resume row. `lastFocusedEpisode` is written from a row focus
  callback and read **only** inside the enter lambda — never during composition —
  so walking the list still costs zero recompositions. Only the season currently
  showing attaches its requesters, so nothing is attached to two nodes during the
  220ms fade.
- **Where** — `drivecast-app/app/src/main/java/com/drivecast/tv/ui/detail/DetailScreen.kt`
  § `ShowSeasons` (the requester lists and both lanes' `tvFocusEnterFallback`),
  `drivecast-app/app/src/main/java/com/drivecast/tv/ui/common/FocusKit.kt`
  § `tvFocusRestorer` KDoc (the never-on-a-lazy-lane rule).
- **Revisit when** — Compose foundation is bumped past 1.7. If a later
  `LazyLayoutPinnableItem.release` becomes idempotent, `focusRestorer` over a lazy
  lane stops being a crash — but it still restores by a stale layout-node hash
  after a scroll round-trip (D-015), so `tvFocusEnterFallback` stays the default
  for recycling lanes regardless.

---

### D-019 — Forgetting a decision means scrubbing it, not deleting it
**Status:** Adopted · **When:** 2026-08-27

- **Hit** — the "Re-upload to Drive" menu lists every item that never reached a
  shared drive, by name, in a menu bar that is visible over a shared screen.
  Some of those names are personal. There was no way to take one out of the
  list, and no way to clear the app's memory at all.
- **Learned** — the obvious implementation (delete the record from
  `decisions.json`) puts the name straight back. That record is the ONLY thing
  making `Poller.poll_once` quiet about a gid: the ask fires for any engine
  download with no decision, and a torrent that has been kept local is usually
  still seeding in Motrix/Transmission long after the decision was made. Delete
  it and the very next 3-second tick pops an ask dialog carrying the name that
  was just "forgotten" — worse than not offering the feature. A record's
  personal payload is its `name` alone; the gid and `drive:<Name>` are not
  identifying.
- **Did** — `DecisionStore.forget` overwrites `name` with `FORGOTTEN_NAME`
  ("(forgotten)"), sets `handled=True`, adds `forgotten=True`, and pops the
  retry bookkeeping — so the gid stays remembered and inert. `forgotten` is
  checked FIRST in `reupload_candidates`, because forget keeps `choice` and a
  scrubbed kept-local record would otherwise re-match the "local" clause and
  return to the same menu as a "(forgotten)" row. The placeholder deliberately
  carries no season/episode marker, so `renamer.derive_show_key` returns None
  for it and the Tier-2 route miner skips forgotten records instead of indexing
  them all under one bogus show. A name reaches disk in two other places, so
  the submenu also clears them: `app.log` (truncated, not unlinked — `log()`
  appends and never recreates) and `rename_cache.json` (`RenameCache.clear`).
  The per-item rows exclude in-flight uploads, whose rows are being redrawn
  from that same name.
- **Where** — `drive-offload/offload_app.py` § `FORGOTTEN_NAME` /
  `DecisionStore.forget` / `forget_all` / `rememberable_items` /
  `reupload_candidates` / `OffloadApp._forget_menu`,
  `drive-offload/renamer.py` § `RenameCache.clear`,
  `drive-offload/test_offload_app.py` §
  `test_forget_does_not_re_ask_a_torrent_still_in_the_engine` (mutation-checked:
  deleting instead of scrubbing makes the ask fire twice).
- **Revisit when** — decisions ever get pruned by age. A reaper that drops old
  records hits exactly the same re-ask trap, and would need the same
  tombstone-shaped answer.

---

### D-020 — A season-by-season torrent is copied as it finishes, moved only at the end
**Status:** Adopted · **When:** 2026-09-29

- **Hit** — a multi-season torrent downloaded one season at a time (tick one
  season's files, wait, tick the next; the rest stay `wanted=false`). When the
  ticked files finished, Transmission read "complete", so `poll_once` handed the
  WHOLE directory to `todrive up` — a move. That stopped the torrent, then
  `todrive` refused because the unselected files leave `<name>.part` behind, and
  the gid burned 5 attempts into a terminal failed state. Had it worked, the
  move would have deleted local data and removed the torrent mid-download.
- **Learned** — "complete" describes the *selection*, not the torrent
  (`sizeWhenDone` counts selected bytes only). A destructive move is only right
  when nothing is left to download, i.e. every file is selected. Until then the
  safe primitive is a COPY of exactly the files that are finished.
- **Did** — `is_partial_selection` (multi-file, at least one file unselected)
  routes a drive-bound torrent to a copy path: `pending_partial_files` picks the
  files that are selected, byte-complete, on disk under their final name with no
  marker sibling, and not already in the record's `uploaded_files`. It runs while
  the torrent is "complete" or "active" (never verifying), so an early season
  uploads while a later one still downloads. `todrive up --keep --files-from`
  copies them to `<drive>/<top dir>/<rel>`, exactly where the final whole-dir
  move would put them, so that move dedups. Nothing local is touched: no stop, no
  remove, no rename hook. Once all files are selected the existing move path
  runs unchanged, except it skips the rename hook (renaming would change the
  names the copies already have) and reuses the drive. **Overflow:** `todrive`
  picks a drive by sizing the payload against the 100 GB cap and reports a reroute
  only in a `ROUTED:` line, so two passes could land on different drives. The
  first successful batch records the drive it actually used as `partial_drive`;
  every later batch and the final pass pass `--no-overflow` to that drive. Cost:
  if the pinned drive fills, that upload fails as quota (terminal until a manual
  re-pick, which clears the pin and re-uploads to the new drive).
- **Where** — `drive-offload/offload_app.py` § `is_partial_selection` /
  `pending_partial_files` / `final_pass_plan` / `Poller._poll_partial` /
  `Poller.upload_partial_done` / `DecisionStore.add_partial_upload` /
  `perform_partial_upload`, `drive-offload/todrive` § `cmd_up` (`--files-from`) /
  `build_up_argv` / `find_incomplete_markers_listed`,
  `drive-offload/test_offload_app.py` § `TestPartialPoll` (mutation-checked:
  dropping the `uploaded_files` filter, the marker-sibling check, the drive pin,
  or the rename skip each fails a test).
- **Revisit when** — a torrent with permanently deselected files (junk the user
  never wants) should still be finished off. Today it is copied and then left
  seeding, because the move only runs once every file is selected.

### D-021 — Each season folder is freed once it is verifiably on the drive, then the next one starts
**Status:** Adopted · **When:** 2026-09-29

- **Hit** — D-020 made partial mode COPY finished files and keep everything
  local. On a 460 GB Mac a 179 GB multi-season torrent ("Show (2007) Season
  1-7") still filled the disk: nothing was ever deleted until every file was
  selected, which never happens when the disk cannot hold the whole thing.
- **Learned** — the copy is only safe to delete against once it is *proven*
  on the drive, and Transmission adds an ordering trap: if SELECTED files
  disappear it errors and stops the whole torrent. So the delete must come
  after the files are unticked, and the untick must be confirmed, not assumed.
  A "folder" is the natural unit (a top-level child dir of the torrent root);
  files directly in the root are never folders, so never auto-freed or advanced.
- **Did** — `Poller._poll_folders` runs one action per tick for a
  drive-bound partial Transmission torrent, all on a worker thread (rclone
  check and RPC never block the poll timer): **free** a folder whose files are
  ALL selected and ALL in `uploaded_files` and that still exists —
  `rclone check --one-way` against `<remote>:<torrent dir>/<folder>` (remote from
  `todrive resolve`; only rc 0 counts) -> `torrent-set files-unwanted` -> re-read
  `torrent-get` and require every one of those files `wanted=false` -> `rmtree`
  only if the folder is a real (non-symlink) direct child of the torrent root ->
  record it in `freed_folders`. A folder already absent, fully uploaded and fully
  unwanted is just recorded. **Advance:** once the user's selection has no
  incomplete file and no copy is owed, tick the first folder (season number
  ascending, then natural sort) with zero selected files, no uploaded files and
  not freed — if `disk_usage(root).free >= folder size + margin` (default 5 GB;
  else one "Not enough space" notice per gid+folder) — then `torrent-start`.
  Nothing the user ticked is ever changed. **Done:** when every file is in
  `uploaded_files`, check what is left locally, remove the torrent WITHOUT data,
  confirm it left the engine, mark handled, delete the root (only if every
  regular file left is uploaded, ignoring `.part` markers and empty dirs). COPY,
  free, advance and done share the one `_uploading` in-flight guard per gid, so
  they can never overlap; their failures back off in `free_*` keys, apart from
  `partial_*` and `failures`. A torrent with `freed_folders` is never whole-dir
  moved (freed folders are unwanted, and an explicit guard covers a re-ticked
  one). Config (top-level keys, default on): `partial_free_after_upload`,
  `partial_auto_advance`, `partial_free_margin_gb`.
- **Where** — `drive-offload/offload_app.py` § `Poller._poll_folders` /
  `_poll_free` / `_free_work` / `_poll_done` / `_done_work` / `_poll_advance`,
  `partial_folders` / `safe_child_dir` / `rclone_check_argv` / `verify_on_drive`,
  `TransmissionClient.set_files_wanted` / `get_files_wanted`,
  `DecisionStore.add_freed_folder` / `record_free_failure`,
  `drive-offload/test_offload_app.py` § `TestFreeAfterUpload` /
  `TestAutoAdvance` / `TestPartialDone` / `TestFreedFolderMoveGuard`
  (mutation-checked: freeing before the untick, ignoring the check result or
  the untick confirmation, dropping the symlink/direct-child guard, freeing on
  partial upload, ignoring free space, skipping the leftover scan, and dropping
  the move guard each fail a test).
- **Revisit when** — a torrent has permanently unwanted files in its ROOT
  (never a folder, so never ticked): it is never "done" and stays seeding. Also
  a re-pick of a different drive keeps `freed_folders` but drops
  `uploaded_files`, so freed folders are not re-uploaded (their data only exists
  on the old drive).

### D-024 — The same show on two drives merges by normalised title + year, after `group_seasons`
**Status:** Adopted · **When:** 2026-09-30

- **Hit** — one show uploaded across two Shared Drives rendered as two tiles:
  drive A held a clean `Show (2007)/Season 1..4` tree, drive B a nested pack
  folder named like a torrent (`Show (2007) Season 1-7 S01-S07 (1080p BluRay
  x265 ...)/Season 5..7`). `group_seasons` only merges `<Show> Season N`
  siblings and bare `Season N` drive roots; nested `Show/Season N` records pass
  through untouched, so nothing ever joined them.
- **Learned** — the identity is already on the record: the scanner stores
  `naming.clean_title(folder)`, which cuts at quality tokens and the year but
  leaves pack noise behind (`Show Season 1-7 S01-s07`, `Show Complete Series`).
  So the key needs one more, trailing-only, strip; a season *range* must not be
  mistaken for a title, and a real title that merely contains such a word
  (`Season of the Witch`) must survive.
- **Did** — `naming.show_key_title` (strip trailing season ranges / `Sxx` tags
  / "Complete Series" style words, then alphanumeric-lowercase) and
  `library.merge_shows_across_drives`, run per tab bucket between
  `group_seasons` and `attach_extras`. Key = `(show_key_title, year)`. A
  year-less record joins only when there is at most one candidate year for that
  title (with none, year-less records merge on title); two bare-`Season N`
  drive-is-the-show records never merge with each other (D-013 keeps same-named
  drives separate). Movies and other tabs are never touched. The merged record
  has the `group_seasons` shape, id `grp:` + `sha1("show|<key>|<year>")[:16]`
  (still re-hashed per tab when several entertainment tabs exist), season
  numbers unioned, an exact duplicate episode (same number AND same size or
  name) dropped, extras kept as labelled pseudo-seasons. Metadata: the record
  carries transient `_member_ids` (old ids of its members) so
  `merge_existing_metadata` / `assign_added_at` inherit poster, tmdb, category
  and the earliest `added_at` (same tab + same show key only); the scanner
  pops it. Scoped refreshes already rebuild from every selected drive's cached
  records, so no extra handling was needed — the tests pin it.
- **Where** — `drivecast/drivecast/naming.py` § `show_key_title`;
  `drivecast/drivecast/library.py` § `merge_shows_across_drives`,
  `_merge_show_members`, `_carry_from_members`; tests in `test_library.py`
  (cross-drive section) and `test_naming.py` (mutation-checked: disabling the
  merge call, the dedupe, member metadata carry, the year in the key, the tab
  guard on carry, and the drive-show guard each fail a test).
- **Revisit when** — two genuinely different shows share a title and both lack
  a year across two drives (they would merge), or a torrent name carries a
  title-shaped word the trailing strip does not know.

### D-028 — VLC software-decodes light H.264 on the Fire TV Stick
**Status:** Adapted · **When:** 2026-10-20

- **Hit** — movies on the Fire TV Stick 4K Plus (AFTMA08C15, VLC 3.6.5)
  froze mid-file: audio kept playing over a frozen frame, forever. Raising
  VLC's network caching did nothing. The obvious suspects were Wi-Fi, a Drive
  stall, or the relay's `read=None` timeout.
- **Learned** — it is the Stick's vendor hardware AVC decoder crashing, not
  the stream. On a light 1080p-class H.264 High@L4.0 MP4 (~2 Mbps, AAC
  stereo) the decoder service segfaulted in
  every hardware run: 6 reproductions at 4 media positions, deterministic per
  start offset (identical frame counts on repeats). VLC stops its MediaCodec
  output thread and neither falls back to software nor shows an error.
  Controls: the original bytes served by a plain LAN HTTP server (Drive and
  drivecast out of the path) crashed at the identical frame; the relay showed
  no throughput dip at the crash second; Drive→Mac pulled ~116 Mbps with a
  max 1.9 s gap between 1 MiB chunks. The same file and seek with VLC's
  `disable_hardware` extra played 150 s straight through the crash point at a
  mean ~111% (max ~152%) of the Stick's 400% CPU. A byte-identical `-c copy`
  remux survives in hardware, so the trigger is container/timestamp-dependent
  inside the vendor decoder; the mechanism is unknown and routing around it
  does not need it. Software is not an option for 4K HEVC Main10 on this SoC
  (~28 fps at 1080p), and much of a typical library is 4K, so a global VLC
  "hardware acceleration off" is wrong.
- **Did** — a per-file policy on the server: `GET /api/media-info/{id}`
  ffprobes the file once over loopback `/stream` (cached in
  `data/media_info.json`; failures re-probed after an hour) and answers
  `vlc_disable_hardware=true` only for H.264, 8/10-bit 4:2:0, ≤1920×1088,
  ≤30.5 fps, ≤15 Mbps. Everything else and every unknown keeps hardware.
  The app (0.5.2) forwards it as the `disable_hardware` intent extra — VLC
  tests the extra's *presence*, so `false` is never put. Playlists carry it
  for the first item only (VLC drops extras on m3u expansion). The first
  probe of a moov-at-end MP4 took ~10 s over Drive, so the detail screen
  prefetches it and the app waits at most 15 s before falling back to
  hardware. `"vlc_software_decode": false` in config.json turns the policy
  off. The relay also logs one INFO line per request to `data/drivecast.log`
  (`stream <id6>… range=… status=… sent=…MB dur=…s avg=…Mbps max_gap=…s
  gaps>2s=N end=complete|client_abort|upstream_error`) and a WARNING for any
  gap over 5 s between upstream chunks, so a real Drive stall would show up.
- **Where** — `drivecast/drivecast/media_info.py:84` (`vlc_policy`,
  thresholds at `:31`); `drivecast/drivecast/server.py:722`;
  `drivecast/drivecast/streaming.py:213` (log line), `:32` (stall threshold);
  `drivecast/drivecast/logging_setup.py`;
  `drivecast-app/app/src/main/java/com/drivecast/tv/ui/player/PlayerScreen.kt:153`,
  `ExternalPlayerViewModel.kt:38`. Tests: `test_media_info.py`,
  `test_server.py` (media-info section), `HardwareDecodePolicyTest.kt`
  (mutation-checked: forcing the policy to hardware, and demoting the stream
  log line to DEBUG, each fail a test).
- **Revisit when** — VLC on the Stick updates (re-run the hardware control:
  if the decoder no longer crashes, the policy can go); a 1080p H.264 title
  stutters in software (tighten the thresholds); many 1080p H.264 titles never
  crash in hardware (narrow the gate, e.g. to MP4 only); or the stream log ever
  shows `max_gap` > 5 s (then build a stall-resilient relay — "long 4K movies
  keep buffering" is unmeasured and may be a separate issue).
### D-029 — Embedded-first subtitles: index before fetch
**Status:** Adopted · **When:** 2026-08-23

- **Hit** — the subtitle cache held sidecars for under 3% of the video files,
  and the plan that followed was "fetch the other 97%": months at
  OpenSubtitles' 20/day, more than a year at the anonymous 5/day.
- **Learned** — that share was the wrong denominator. It counts sidecars, not
  coverage; most of these files already carry their own subtitle tracks (138
  subtitle streams across 30 files, 4.6 per file on average). But the first
  10-file sample said "9 of 10 carry an embedded English track", and a
  **30-file measurement refuted that number**. Real, seed-pinned
  (`seed=20260823`), 15 movies + 15 episodes, ffprobe through the server's own
  `/stream` proxy:

  | Bucket | Files | Share |
  |---|---|---|
  | usable English **text** track (not forced, not SDH) | 12 | 40.0% |
  | English **image** track only (PGS / dvd_subtitle) | 8 | 26.7% |
  | English text but forced/SDH only | 0 | 0.0% |
  | no English subtitle track at all | 10 | 33.3% |
  | probe failed | 0 | 0.0% |

  So two thirds of files carry *some* English track, but only **40%** carry the
  text track the desktop short-circuit needs — not 90%. The backfill worklist
  (anything without `has_text_en`) is therefore ~60% of the files, several
  times what the plan projected. Tagging held up well enough to build
  on: 24 English-tagged tracks, and 35 of 138 streams untagged/`und` — present
  where it matters, so `--slang=eng` has something to match. Cost also came in
  better than estimated: **0.65 s/file** wall-clock at 6-way concurrency (19.4 s
  for 30 files), which puts a full sweep of a few thousand files in the tens of minutes,
  about three times the estimate.
- **Did** — coverage comes from a one-time ffprobe index into
  `data/sub_index.json`, keyed on the immutable `file_id` with `size` alongside,
  and external fetching serves only the measured remainder. Standalone, not
  inside `scan_cache.json`, because `ScanCache.put` replaces a drive's whole
  record list on every walk and would re-pay the sweep on each scan. The sweep
  fires automatically after a *walking* scan and never after a `scope=[]`
  cache-only rebuild, which walks no Drive bytes by definition.
- **Where** — `drivecast/drivecast/sub_index.py:31` (the store),
  `drivecast/drivecast/sub_index.py:28` (`FLUSH_EVERY = 25`, the resume
  granularity); `drivecast/drivecast/sub_sweep.py:107` (`SubtitleSweeper`),
  `:58` (`is_english`, why `und` is not English);
  `drivecast/drivecast/server.py:43` (`_has_embedded_english_text_track`),
  `:218` (the scope guard that keeps the sweep off a cache-only rebuild),
  `:252` (`start_sweep`); `drivecast/drivecast/scan_cache.py:64` (the `put`
  behaviour this store avoids).
- **Revisit when** — the full sweep finishes. These are 30 sampled files; the
  real figure is what the sweep prints. If it lands near 40% text coverage, the
  five-a-day budget is badly undersized (see D-023) and D-022's device test
  becomes the cheapest way to cut the gap almost in half.

### D-030 — Subtitle bytes are validated at the single write point
**Status:** Adopted · **When:** 2026-08-23

- **Hit** — two cached "subtitles" were 263-byte Airtel court-order block pages
  with an `<iframe>`, served as HTTP 200; a third was a 267-byte ads-only stub
  with 4 cues and no dialogue. Each was permanent for its title.
- **Learned** — the old `_atomic_write` accepted any bytes and `cached()` trusts
  file existence forever, so one ISP substitution poisoned a title for good. The
  substitution is network-level and can hit any host, so per-provider validation
  copies would drift — and migration-versus-live-gate drift is exactly how a
  purged file gets re-poisoned.
- **Did** — one gate, `sanitize_subtitle_bytes`, applied at the resolver's single
  write point after any source's bytes arrive, and reused verbatim by the
  migration script: HTML sniff, cue count, ad-density check on short files,
  charset normalised to UTF-8 without BOM and CRLF line endings. A source that
  does not call `_accept` has no way to write into the cache at all. Rejects go
  to `data/subs_negative.json` with reason-scoped TTLs — poisoned 14 days (the
  block page recurs deterministically on this ISP), not-found 2 days, transient
  failures never recorded. `?refresh=1`, `POST /api/subtitles/{id}/refresh` and
  a Settings row all override. Accepted files keep their native extension —
  writing ASS bytes under `.srt` would mislabel them for `mime_for`. UTF-16 is
  deliberately absent from the decode list: a BOM-less UTF-16 attempt destroyed
  a real, full-length subtitle.
- **Where** — `drivecast/drivecast/subtitles.py:210`
  (`sanitize_subtitle_bytes`), `:462` (`_accept`, the single write point),
  `:253` (`NEG_TTL`), `:407` (`cached`, the existence-trust this bounds);
  `drivecast/scripts/migrate_subtitles.py` (dry-run by default, quarantines
  rather than deletes).
- **Revisit when** — the migration prints a reject you believe is good. Verified
  on the live cache on 2026-08-23: almost every file kept (most re-encoded),
  a handful rejected — block pages and an ad stub, no false positives. The
  cue-count and ad-ratio thresholds are named constants for exactly this.

### D-031 — Android VLC per-item XSPF subtitles: still unmeasured
**Status:** Open · **When:** 2026-08-23

- **Hit** — a show played as a playlist gets a sidecar subtitle for episode 1
  only; the code's own comment admits it may not survive VLC's playlist
  expansion.
- **Learned (so far)** — per-track `<vlc:option>input-slave=URL</vlc:option>` is
  documented for **desktop libvlc**. Whether Android VLC's playlist engine
  honours it is unknown. Android VLC's documented intent extras are
  `from_start`, `position`, `title`, `extra_duration`, `subtitles_location` —
  one external file, no track selection. Not settled without a device test.
- **Did** — nothing in code, deliberately. The 15-minute adb procedure is
  recorded in the implementation plan (§6.1); it has **not been run**. No XSPF
  route gets built either way until the sweep shows a real population of shows
  that are both embedded-uncovered *and* sidecar-fetched.
- **Where** —
  `drivecast-app/app/src/main/java/com/drivecast/tv/ui/player/ExternalPlayerViewModel.kt`
  (the playlist branch and its comment, and the single-item branch that does
  this correctly); `drivecast/drivecast/server.py:792` (`api_playlist_m3u`, the
  m3u route, which cannot carry a per-item subtitle).
- **Revisit when** — the test runs; or VLC auto-updates on the Stick (the Play
  Store updates it independently of the app release, so re-run and record the
  `versionName` here).

### D-022 — Image-only English tracks stay uncovered until a device test
**Status:** Open · **When:** 2026-08-23

- **Hit** — image-based English subtitles (`hdmv_pgs_subtitle`, `dvd_subtitle`)
  are invisible to a text pipeline, and a text sidecar fetched for such a file
  spends a scarce quota slot on a title that may already be watchable subtitled.
- **Learned** — this is much bigger than the first sample suggested. At 30 files,
  **8 (26.7%)** have an English *image* track and no English text track (the
  10-file sample said 2). Extrapolated, that is roughly **590 files** of the
  library sitting in exactly this bucket. VLC, mpv and IINA are all *documented*
  to render an image track selected by language, and `--slang`/`--sub-language`
  select it the same way — but that is **unverified on this hardware**, and an
  image track can never be extracted to a sidecar (Infuse's `sub=` is a
  text-only consumer). A wrong "covered" here is unrecoverable in practice:
  nothing re-checks it, and the file would leave the backfill worklist forever.
- **Did** — the index stores `has_text_en` and `has_image_en` **separately**,
  plus per-stream `forced`/`hearing_impaired` dispositions, all captured in the
  first sweep because a re-sweep costs another ~24 minutes of uplink. Play-time
  use of an image track is gated behind `prefer_embedded_image_subtitles`,
  **default False**, so turning it on after the device test is a settings edit
  and not a code change. Until then image-only files stay in the backfill
  worklist.
- **Where** — `drivecast/drivecast/server.py:72`
  (`_has_embedded_english_image_track`), `:1140` (`api_play`, where the config
  key gates it), `:373` (`_uncovered_file_ids`, which deliberately keeps these
  files queued); `drivecast/drivecast/config.py:65`
  (`prefer_embedded_image_subtitles`); `drivecast/drivecast/sub_sweep.py:51`
  (`_IMAGE_CODECS`); `drivecast/drivecast/player.py:174`
  (`build_infuse_url`, the text-only consumer).
- **Revisit when** — someone plays a PGS-only file and confirms text appears **on
  screen**, per player (Fire TV VLC, then desktop mpv/IINA/VLC). This is now the
  highest-value 10 minutes available: a pass moves about a quarter of the files out of the
  backfill queue and cuts the remaining gap by roughly 44%.

### D-023 — One subtitle provider, anonymous tier, no stored credentials
**Status:** Open · **When:** 2026-08-23

- **Hit** — OpenSubtitles allows 5 downloads/day anonymous, 20/day with an
  account, 1000/day VIP. Against a library of a few thousand files that is months even at 20/day,
  which argued for a login and a second provider.
- **Learned** — the embedded-first re-weighting shrank the gap, but not nearly
  as far as hoped. The 30-file measurement (D-029) puts the text-uncovered
  worklist at **~60% of the files**: most of a year at 5/day
  and a couple of months at 20/day. If D-022's device test passes and image-only files
  count as covered, the worklist drops to ~33% — roughly half that time at
  either rate. So the 5/day default is comfortably undersized, and the
  honest ranking of remedies is: run D-022's device test first (free, cuts the
  gap ~44%), then consider the 20/day login. A live download on 2026-08-23
  returned `remaining: 2`, confirming the field name and shape the queue's
  `remaining_hint` reads.
- **Did** — kept the single `opensubtitles_api_key` and the 5/day default, both
  as config keys so raising the limit is a one-line edit rather than a code
  change. Added the priority queue, the OSDb moviehash match, and a real ranking
  function (`moviehash_match` outright, then log-scaled `download_count`, then a
  penalty for machine-translated) instead of trusting `results[0]`. No provider
  ABC: an interface with one implementation is speculation, and extracting one
  later from a working provider is mechanical. subdl.com was declined — same
  block-page-substituting ISP, no moviehash search, self-reported limits.
- **Where** — `drivecast/drivecast/config.py:57` (`subtitle_daily_limit: 5`),
  `:51` (`prefer_embedded_subtitles: True`);
  `drivecast/drivecast/subtitle_queue.py:111` (`DEFAULT_DAILY_LIMIT`);
  `drivecast/drivecast/subtitles.py:415` (`resolve`'s match-hint kwargs),
  `:663` (`_download_os_file`, where `remaining` is read);
  `drivecast/drivecast/moviehash.py` (the OSDb hash).
- **Revisit when** — the full sweep prints the real worklist size. If it
  confirms a worklist near 60% and D-022 fails, the login is the cheaper of the two
  reinstatements and this entry flips to Adopted.

### D-032 — A backfill slot is spent only when the provider was really called
**Status:** Adopted · **When:** 2026-08-23

- **Hit** — the first cut of the backfill queue charged one of its five daily
  slots to every *attempt*. Reviewed before it ever ran, that accounting makes
  the queue stop making net progress after roughly its first week.
- **Learned** — two separate leaks, both invisible to a green test suite.
  (1) A successfully backfilled file never becomes "covered": a fetched `.srt`
  cannot set `has_text_en` — only a re-probe of the video could — so judging
  coverage on the index alone put every completed file straight back into the
  worklist, where it was re-attempted, satisfied instantly from cache with zero
  network, and still charged a slot. Over weeks the budget goes almost entirely
  on re-confirming finished work. A free Drive-sibling hit leaked the same way.
  (2) Items are ordered tier-then-`queued_at` and a failure does not re-stamp
  `queued_at`, so the handful of files OpenSubtitles genuinely lacks sat at the
  head of the queue forever: `failed_today` resets at local midnight, the 2-day
  `not_found` negative-cache entry then makes the retry an instant no-network
  failure — which still burned a slot — and nothing behind them was ever reached.
- **Did** — the budget is charged from a **real provider call**, not an attempt:
  `SubtitleResolver.os_download_calls` counts issued `/download` requests, the
  caller snapshots it around `resolve()`, and `run()` charges only on a
  difference. Coverage now also counts an existing sidecar, read with **one**
  `os.listdir` rather than four `os.path.exists` per file across thousands of ids. The
  head-of-line stall gets two guards: a free `is_blocked` pre-check (the
  negative cache) skips an item without an attempt or a spend, and an item with
  `PARK_AFTER_ATTEMPTS` real misses is retried weekly instead of daily. A
  failure while the provider reports `remaining == 0` is a quota stop, not the
  item's fault, so it is not added to `failed_today` — in IST the local calendar
  day rolls 5.5 h before OpenSubtitles' UTC quota does, and blacklisting there
  would waste the rest of the real day.
- **Where** — `drivecast/drivecast/subtitle_queue.py:414` (`run`, the accounting),
  `:305` (`record_spend`'s `spend`/`mark_failed` flags), `:372` (`_is_parked`),
  `:119` (`PARK_AFTER_ATTEMPTS = 3`, `PARK_RETRY_DAYS = 7`);
  `drivecast/drivecast/server.py:373` (`_uncovered_file_ids`), `:354`
  (`_sidecar_covered_ids`, the one-listdir read), `:451` (`_backfill_attempt`,
  which reports `spent`), `:495` (`_backfill_blocked`);
  `drivecast/drivecast/subtitles.py:399` (`os_download_calls`).
- **Revisit when** — a week of real running. `data/subtitle_queue.json`'s
  `spent` should track actual downloads, and `pending` should shrink
  monotonically rather than oscillate.

### D-025 — Infuse cannot be told a language, so it keeps the sidecar path
**Status:** Adapted · **When:** 2026-08-23

- **Hit** — the embedded-first short-circuit skips `resolve()` and hands the
  player `prefer_lang="eng"` instead. For Infuse that produced a silent
  regression: worse than before the feature existed.
- **Learned** — the `infuse://` scheme has no language or track parameter, only
  `sub=`. So `prefer_lang` is dropped at launch with nothing to replace it: the
  short-circuit gave Infuse **neither** the sidecar it used to get **nor** a
  usable hint, and `/api/play` then reported `subtitles: true`. There is no
  parameter to invent — this is a consumer limit, not a missing feature.
- **Did** — `/api/play` resolves the player kind *before* the coverage decision
  and only treats an embedded track as usable for mpv, IINA and VLC. Infuse
  falls through to `resolve()` exactly as it did before, and the reported
  `subtitles` flag then describes what it actually received. `detect_player`
  never auto-picks Infuse, so this only affects an explicit `player: infuse`.
- **Where** — `drivecast/drivecast/server.py:1140` (`api_play`, the
  `embedded_usable` gate); `drivecast/drivecast/player.py:174`
  (`build_infuse_url`, whose docstring states the ceiling), `:324` (`_launch`,
  where `prefer_lang` is dropped for Infuse).
- **Revisit when** — Infuse's URL scheme gains a subtitle-language parameter.
  Nothing else changes this; it is a property of the consumer.

### D-026 — Extracting an embedded subtitle reads the whole file
**Status:** Adapted · **When:** 2026-08-23

- **Hit** — the Fire TV's only subtitle mechanism is `GET /api/subtitles/{id}`,
  which serves a *sidecar file*. The clean-sounding fix for the ~40% of files
  that carry their own English text track is to extract that track server-side
  with `ffmpeg -map 0:s:m:language:eng -f srt -`, run it through the same
  quality gate, and serve it — deterministic on the TV and zero provider quota.
- **Learned** — measured, it is not cheap. Against a byte-counting local HTTP
  server and a 6,979,911-byte MKV carrying a 60-cue `subrip` track:

  | Operation | Bytes read | Share of file |
  |---|---|---|
  | the sweep's `ffprobe -probesize 200k` (build the index) | 917,504 | 13.1% |
  | `ffmpeg -map 0:s:m:language:eng -f srt -` (extract) | 6,979,911 | **100.0%** |

  Extraction is a **full-file read**, and unavoidably so: subtitle packets are
  interleaved across the whole container, so the last cue lives near the last
  byte. For a typical film that means pulling a multi-gigabyte movie through the
  Drive proxy to obtain ~60 KB of text — per file, for hundreds of files. The
  probe, by contrast, is bounded by `-probesize` and stays near a megabyte
  regardless of how large the film is.
- **Did** — did not build it. Instead `/api/subtitles` stops *wasting* quota:
  when the index shows a usable embedded English text track it skips the
  OpenSubtitles step and serves only the free sources (cache, Drive sibling), so
  the TV's HEAD probe on the ~40% covered files no longer spends anonymous-tier
  downloads on a redundant sidecar. `?refresh=1` still forces the paid step,
  since that is a person explicitly asking. The consequence is recorded rather
  than hidden: on the Fire TV, an embedded-covered file depends on VLC's own
  **Settings → Subtitles → Preferred subtitle language = English**, which is a
  two-minute device setting and the single highest-value action in this whole
  effort.
- **Where** — `drivecast/drivecast/server.py:1294` (`api_subtitles`, the
  skip); `drivecast/drivecast/subtitles.py:415` (`resolve`'s
  `allow_opensubtitles` flag), `:462` (`_accept`, the gate any future
  extraction source would still have to write through);
  `docs/FEATURE-PARITY.md` § 4 (the subtitle rows and the TV's gap).
- **Revisit when** — extraction becomes cheap for a *specific* case worth the
  bytes: a single title on demand ("get subtitles for this one now"), where the
  user has chosen to spend the egress, is defensible. An automatic path over the
  library is not.

### D-027 — Editing an `Info.plist` inside a signed bundle breaks TCC, weeks later
**Status:** Adapted · **When:** 2026-09-09

- **Hit** — the hub's Download and Upload rows stopped working. Clicking
  either did nothing at all: no window, no browser, no error, no alert. The
  menu bar itself was responsive and both console scripts ran perfectly from a
  terminal, which pointed suspicion at the hub's script resolution and cost a
  whole first pass on the wrong layer.
- **Learned** — the child process was dying before it reached a single line of
  suite code, and the per-tool log had said so all along:
  `PermissionError: [Errno 1] Operation not permitted:
  <repo>/toolkit/venv/pyvenv.cfg`, then `Fatal Python error:
  init_import_site`. `pyvenv.cfg` is mode `644` and reads fine from a shell,
  so **EPERM on a world-readable file is macOS TCC**, not a file permission.
  The real cause sat one level further out: `codesign --verify --deep --strict`
  reported `invalid Info.plist (plist or signature have been modified)`. The
  ad-hoc signature was made when the `.app` was built; the `Info.plist` was
  rewritten **six weeks later** by the path sweep that followed moving this
  repo into the monorepo — a `.app`'s `Info.plist` records the interpreter
  path, so a path sweep is precisely the thing that touches it. With the seal
  broken, `tccd` logs `IDENTITY_ATTRIBUTION: Failed to copy signing info …
  #-67030` on every request and can no longer attribute the app, so no
  Files-and-Folders grant applies to it *or to anything it spawns* — and this
  suite's checkout lives under a TCC-protected folder. It then kept working
  for another two weeks on a cached grant, which is what makes this worth an
  entry: the break and the symptom are separated by weeks, so the change that
  caused it is nowhere near the failure in time or in git history.
- **Did** — nothing in the code; this is a constraint on how the `.app` is
  built and installed, not something the suite can defend against. Recovery is
  re-seal, then re-grant: `codesign --force --deep --sign -
  /Applications/Drivedeck.app`, relaunch the app, then allow the Documents (or
  Full Disk Access) prompt — re-signing changes the cdhash, so TCC treats the
  result as a new app and any surviving grant does not carry over. The standing
  rule this buys: **never edit anything inside a signed bundle in place.**
  Rebuild the `.app`, or re-sign immediately afterwards, and confirm with
  `codesign --verify --deep --strict` rather than assuming — a broken seal is
  invisible until the OS happens to need the identity.
- **Where** — `toolkit/src/gdrive_toolkit/hub/hub_core.py:232` (the
  `kind == "web"` launch), `:234` (`_log_path` — the per-tool log that was the
  only diagnostic anywhere in the system), `:241` (`start_new_session=True`,
  which detaches the child so its death is silent);
  `toolkit/src/gdrive_toolkit/hub/menubar.py:219` (`rumps.alert`, which only
  fires for a `HubError` and never for a child that exits after `Popen` has
  already returned).
- **Revisit when** — the hub is asked to report a launched web tool's exit
  status. A child that dies milliseconds after `Popen` is currently
  indistinguishable from one still starting, and `installed_if_any` is
  satisfied by `argv[0]` merely existing, so an unrunnable tool still reports
  installed. A short post-launch port re-probe would turn this whole class of
  failure from a silent no-op into a visible error.
