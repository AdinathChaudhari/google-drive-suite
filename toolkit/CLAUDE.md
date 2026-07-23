# google-shared-drive-toolkit

Three tools around one shared engine (`rclone`): a bulk-download web UI, a
parallel-upload web UI, and a macOS menu-bar hub that launches and shows
status for both plus any other tools registered with it. Public name/brand in
docs is "Drivedeck"; the import package, app id, and CLI names all use
`gdrive_toolkit` / `gdrive-*`.

Graduated from three standalone prototypes under `Playground/Tools/`
(`drive-downloader`, `drive-upload`, `drive-hub`) that shared a lot of
near-duplicate code (two copies of the rclone rc-client, two copies of the
Flask app bootstrap). This repo merges that duplication into `common/` and
generalizes the couple of places the prototypes had hardcoded to one specific
account.

## Package layout (src-layout)

```
src/gdrive_toolkit/
├── common/       shared engine + app bootstrap, used by all three tools
│   ├── rclone_rc.py     one merged RcloneRC class: daemon lifecycle, list_drives,
│   │                    list_dir, copy_async (download), mkdir + upload_async
│   │                    (upload), stats, job_status, job_stop
│   ├── config.py        APP_ID + shared config (remote, repo_root) +
│   │                    per-tool config load/save with DEFAULTS ← shared ←
│   │                    per-tool-file precedence
│   ├── entry.py         run(create_app, tool_name) — the parametrized "start
│   │                    daemon, open browser, run threaded Flask" every
│   │                    web tool's cli.py calls into
│   ├── webapp.py        AppState, formatting helpers, static-serving, and
│   │                    the SSE stream scaffolding shared by both web UIs
│   ├── setup_remote.py  gdrive-setup: pick an already-configured rclone
│   │                    remote and write it to shared config
│   └── _deps.py         require(feature, *modules) — lazy-import guard so
│                        `pip install google-shared-drive-toolkit` (no
│                        extras) still gives you working console scripts
│                        that fail with a clear pip-install-the-extra message
├── downloader/   web UI: browse a drive, checkbox-select, download
│   ├── selection.py     pure logic — checkbox tree → ordered rclone filter
│   │                    rules; unit-tested with no daemon/network involved
│   └── server.py        Flask routes + AppState wiring specific to download
├── uploader/     web UI: pick local files, choose destination, upload
│   ├── plan.py          pure-ish logic — local picks → copy jobs (dedup,
│   │                    collision detection, glob-escaping); unit-tested
│   └── server.py        Flask routes + the osascript native-picker code
└── hub/          macOS menu-bar app
    ├── registry.py       LOADER, not data: computes built-in entries for
    │                     this toolkit's own downloader/uploader from the
    │                     resolved repo root, then merges in whatever's in
    │                     the user's hub_tools.json (see below)
    ├── hub_core.py       stdlib-only install/status/launch logic, no
    │                     flask/rumps import — unit-tested with every probe
    │                     injected
    └── menubar.py        the rumps front-end; imports rumps lazily so this
                          module stays importable headless
```

`tests/` sits outside the package and imports the installed
`gdrive_toolkit.*` modules — it's testing what actually gets shipped, not a
path-hacked in-tree copy.

## Extras / entry points model

Console scripts always install; the heavy per-feature dependencies (`flask`,
`requests`, `rumps`) are all optional extras, and each script's `main()`
lazy-imports what it needs via `common._deps.require(...)` before doing
anything else. That means `pip install google-shared-drive-toolkit` with no
extras still gives you working `gdrive-*` commands that fail fast with a
"run `pip install 'google-shared-drive-toolkit[download]'`"-style message
instead of an ImportError, if you forgot the extra you needed.

| Script | Extra | Entry point |
|---|---|---|
| `gdrive-download` | `download` | `gdrive_toolkit.downloader.cli:main` |
| `gdrive-upload` | `upload` | `gdrive_toolkit.uploader.cli:main` |
| `gdrive-hub` | `hub` (macOS only) | `gdrive_toolkit.hub.menubar:main` |
| `gdrive-setup` | none (stdlib only) | `gdrive_toolkit.common.setup_remote:main` |

`[all]` pulls in `download` + `upload` + `hub`; `[dev]` adds `py2app` (macOS)
and `pytest` on top of `[all]` for local development and building the
menu-bar `.app`.

## The hub registry loader

`hub/registry.py` does not hold a hardcoded tool list. At import time it:

1. Resolves this toolkit's own repo root — `GDRIVE_TOOLKIT_ROOT` env var, then
   `repo_root` from the shared config file, then a `Path(__file__)` dev
   fallback (deliberately *not* used alone, since it breaks once the package
   is frozen into a `.app` by py2app).
2. Builds the downloader/uploader entries from that root, resolving the
   actual launch command (installed console script on `PATH`, or the venv
   script next to the running interpreter) rather than hardcoding paths to
   this specific checkout.
3. Resolves the **suite root** — `GDRIVE_SUITE_ROOT` env var, then `suite_root`
   from the shared config, then `repo_root.parent` *iff*
   `(parent/"drivecast"/"app.py").exists()`. When this toolkit is checked out
   inside a `google-drive-suite` monorepo (i.e. this repo lives at
   `<suite>/toolkit/`), that resolves to `<suite>` and the hub adds
   **computed built-ins** for the sibling suite members: `drivecast` (web,
   port 8737), `drive-offload` (menubar, `launchd_label: com.driveoffload.app`),
   and `drivecast-app` (kind `external`, inert — Fire TV, nothing to launch
   from this Mac). Standalone installs (no suite checkout) resolve suite root
   to `None` and just skip this step — no behavior change for them.
4. Reads `~/Library/Application Support/gdrive_toolkit/hub_tools.json` if it
   exists and layers in whatever tool dicts it finds — same schema `hub_core.py`
   already consumes, so adding (or overriding) a tool in the hub is "edit a
   JSON file," not "edit this package." A user entry whose `"id"` matches a
   built-in (this toolkit's own, or a suite sibling's from step 3) **replaces**
   that built-in in place rather than appending a duplicate row. See
   [`docs/hub_tools.example.json`](docs/hub_tools.example.json) for the schema
   and worked examples. Missing file → built-ins only, no error.

`hub_core.py` itself doesn't know or care where a tool dict came from — this
split is what keeps the hub's core logic free of any account-specific or
installation-specific facts.

## Identity strings

Everything that used to say `drive-hub` / `drive-downloader` / `drive-upload`
in a path, launchd label, or bundle id now derives from one constant,
`APP_ID = "gdrive_toolkit"`, in `common/config.py` — config directory name,
log directory, lock file path, launchd label (`com.gdrivetoolkit.hub`), and
py2app bundle id all come from it. If you fork this and want your own
identity, that's the one constant to change.

## Migration note

This package is a from-scratch restructure of three prototypes, not a direct
copy. The prototypes had one rclone remote and a couple of Shared-Drive facts
hardcoded; this repo generalizes all of that behind `gdrive-setup` (first-run
remote selection) and a `is_gdrive` capability check at daemon start that
gates Drive-only features (Google Docs export, abuse acknowledgment, chunked
upload connection strings) off automatically for a non-Drive rclone remote.
None of the original account-specific facts — which remote, how many drives,
any drive IDs — belong in this repo or its docs; if you're adding a doc or
comment here, keep it generic.

## Tests

```sh
./venv/bin/pip install -e '.[dev]'
./venv/bin/python -m pytest tests/
```

`selection.py`, `plan.py`, and `hub_core.py` are the three modules worth
reading first if you're extending this — each is pure/stdlib-only logic with
every external effect (rclone daemon, filesystem, `launchctl`) either absent
or injected, so their test files show the full behavior without needing a
live rclone remote.
