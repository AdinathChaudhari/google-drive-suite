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
- **Then** (same day) — the shipped menu did nothing when clicked. The store
  was never written and no line reached the log. The app was verified healthy
  (one start, stable PID, no traceback) and the dialog script verified working
  in isolation, which leaves the confirmation itself: an `_osascript`
  `display dialog` belongs to a child osascript process and can open behind
  the frontmost window, timing out after 60s into a silent False — the one
  explanation consistent with all of it. Fixed by using `rumps.alert` — an NSAlert
  owned by the app, which fronts with it — and by logging every callback entry
  plus every confirm outcome, since "declined" and "never dispatched" are the
  same silence otherwise. The timeout was dropped with it: `_offer_repick`'s
  gate needs `giving up after` because it fires unattended, but a gate that
  only opens on a click can safely block until answered.
- **Revisit when** — decisions ever get pruned by age. A reaper that drops old
  records hits exactly the same re-ask trap, and would need the same
  tombstone-shaped answer.
