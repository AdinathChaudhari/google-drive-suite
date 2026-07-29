# google-drive-suite

Consolidated monorepo for four previously-standalone repos, all built around
one shared engine (`rclone`) and one Google account's Shared Drives. Public
brand: **Drivedeck**. Each component's own `CLAUDE.md` has its package layout,
identity constants, and gotchas — this file is orientation across the suite.

## Component map

```
toolkit/        google-shared-drive-toolkit — download + upload web UIs, macOS menu-bar hub
drivecast/      streaming server (FastAPI) — "Netflix for your Drive"
drivecast-app/  Fire TV / Android TV client (Kotlin, Jetpack Compose for TV)
drive-offload/  ingest daemon — watcher + todrive/stream-dl/yt-show/yt-video CLIs
design/         drivedeck.css — canonical design tokens, vendored into each web app
docs/           DECISIONS.md (learnings log), case-studies/, media/ (planned assets)
scripts/        sync-css.sh, bench-transfers.py (rclone concurrency benchmark)
```

Each of the four component dirs was imported as a **snapshot** of its former
repo's current files (`git archive` of HEAD — tracked files only), NOT via
`git subtree`. This was a deliberate safety choice: pulling full history into
a public repo risks resurfacing any secret ever committed-then-removed. The
full commit history is preserved in the four former repos, which are archived
(read-only) on GitHub with a pointer README back to this monorepo. Each import
commit here cites the source repo's HEAD SHA.

## Two hard rules (read before touching either)

1. **drive-offload's flat file layout is load-bearing.** Sibling-path state
   and imports mean scripts assume their neighbors are flat, not nested under
   `src/`/`bin/`. Never restructure it into a conventional package layout —
   see `drive-offload/CLAUDE.md`.
2. **The Osho section plugin is PRIVATE, not suite code.** drivecast's custom
   classifiers (a personal one is named "Osho") live in
   `~/Library/Application Support/drivecast/sections/` — the user's own
   directory, outside git entirely. Never add a personal plugin under
   `drivecast/` in this repo; `conftest.py` in drivecast's tests stubs out
   the user plugin dir precisely so a real plugin can never leak into a run.

## Decisions log — read it before changing a tuning value

[`docs/DECISIONS.md`](docs/DECISIONS.md) records *why a value is that value*:
measurements that overturned an assumption (**Adopted**), constraints the code
is shaped around (**Adapted**), and questions asked but not yet answered
(**Open**, each with a `Revisit when` condition). Code shows what the suite
does and git history shows when it changed; neither explains why a number is
8 rather than 4.

Add an entry whenever a benchmark, a failure, or a platform limit changes a
decision — especially when the resulting code looks arbitrary without the
backstory. Use the template at the top of the file, take the next `D-NNN`, and
add the index row. `Where` must cite real `file:line`; a rotted citation is
worse than no entry.

Live one to know about: **D-002** — the toolkit's Drive concurrency values
(`Transfers`, `MultiThreadStreams`, `drive_chunk_size`) were never measured,
and the suite disagrees with itself across `rclone_rc.py`, `todrive`, and
`yt-show`. `scripts/bench-transfers.py` is the harness that settles it; it
needs a fast connection to say anything (on a slow uplink every setting scores
the same, because the pipe is the bottleneck, not rclone). Don't "tidy" those
constants into agreement without running it first.

## Design system

`design/drivedeck.css` is the canonical token sheet (`--dd-*` custom
properties). It's vendored (not shared at runtime) into:

- `toolkit/src/gdrive_toolkit/downloader/static/drivedeck.css`
- `toolkit/src/gdrive_toolkit/uploader/static/drivedeck.css`
- `drivecast/drivecast/static/drivedeck.css`

Each vendored copy carries a header comment pointing back here. After editing
`design/drivedeck.css`, run `scripts/sync-css.sh` to re-copy it into all
three locations — there's no build step or shared static route (the toolkit
package must stay self-contained when pip-installed).

Each app's own stylesheet aliases its local var names onto `--dd-*` tokens
rather than being rewritten — see the header comment in each app's `app.css`
/ `style.css`. drivecast's per-section inline accent overrides
(`/api/sections`, set as inline custom properties by `setSection`) sit above
the token defaults in the cascade and must not be touched when working on
theming.

## Per-component docs

- [`toolkit/CLAUDE.md`](toolkit/CLAUDE.md) — package layout, extras/entry
  points, the hub registry loader (including how it detects this suite
  layout and adds computed built-ins for the sibling components), and the
  Start/Restart/Stop lifecycle control it offers for peer menu-bar apps.
- [`drivecast/CLAUDE.md`](drivecast/CLAUDE.md) — stack, sections
  architecture, the Osho-plugin note above.
- [`drive-offload/CLAUDE.md`](drive-offload/CLAUDE.md) — the flat-layout
  rule above, plus the yt-show/yt-video pipeline internals.
- `drivecast-app/` has no CLAUDE.md yet; see its README for build/sideload
  instructions.

## Testing

No single command runs everything — each component keeps its own venv and
suite (see [CONTRIBUTING.md](CONTRIBUTING.md) for the exact commands). The
toolkit's 67-test suite is the fastest signal that a docs/CSS-only change
hasn't touched anything it shouldn't have.
