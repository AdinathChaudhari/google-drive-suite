# Contributing

This is a skeleton to fork, not a product seeking contributors. It's built
around one person's Google account, one person's rclone remote, and one
person's media library — the value is in adopting the pattern, not in this
exact deployment growing a community around it.

- **PRs for bugs and security issues are welcome.**
- **Feature direction follows my own use** — a PR adding a feature I don't
  need may sit unreviewed for a while, or get declined even if it's good
  work. Open an issue first if you want a feature discussed before you build
  it.

## To make it yours

Each component is meant to be forked and re-identified, not run as-is
alongside the original:

- **toolkit** — change `APP_ID` in `common/config.py` (config dir, log dir,
  lock file, launchd label, py2app bundle id all derive from it). Register
  your own tools in the hub via `hub_tools.json` — see
  [toolkit/README.md → Fork it](toolkit/README.md#fork-it-making-it-yours).
- **drivecast** — drop custom section/tab plugins into
  `~/Library/Application Support/drivecast/sections/` (your user directory,
  outside the repo — never commit one here). Config, secrets, and library
  data all live outside the repo too.
- **drivecast-app** — it's a plain HTTP client against drivecast's API; point
  it at your own server and token.
- **drive-offload** — edit `config.json` (watch dirs, remotes, thresholds);
  nothing account-specific is hardcoded.

## Running the tests

Each component has its own test suite and its own venv:

```sh
# toolkit
cd toolkit && ./venv/bin/pip install -e '.[dev]' && ./venv/bin/python -m pytest tests/ -q

# drivecast
cd drivecast && ./venv/bin/python -m pytest drivecast/ -q

# drive-offload
cd drive-offload && ./.venv/bin/python -m pytest -q

# drivecast-app (compile check only, needs Android SDK)
cd drivecast-app && ./gradlew :app:assembleDebug
```

## What to check before opening a PR

- Which component does this touch, and does its own README/CLAUDE.md still
  describe the behavior accurately after your change?
- Does the change trace to a spec decision or a clear bug — see
  [BUILD-WITH-AI.md](BUILD-WITH-AI.md) for what "spec-driven" means here.
- Security-relevant changes (auth, localhost binding, delete paths) get read
  closely — see each component's Security model section.
