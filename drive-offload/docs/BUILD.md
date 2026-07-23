# Building drive-offload.app

How to package the menu-bar app as a standalone macOS bundle with py2app,
install it, and switch the LaunchAgent over to it.

## Prerequisites

The repo venv (`.venv/`, Python 3.13 **framework** build — required for GUI
apps) already has the build deps: `rumps`, `py2app`, `pillow`, `setuptools`.

## Build

```sh
cd drive-offload
rm -rf build dist
./.venv/bin/python setup_app.py py2app
```

Output: `dist/drive-offload.app` — a self-contained bundle (embeds Python and
rumps; `todrive` is copied into `Contents/Resources` next to the main script,
so the app's `SCRIPT_DIR` lookup finds it unchanged).

## Install

```sh
rm -rf /Applications/drive-offload.app
ditto dist/drive-offload.app /Applications/drive-offload.app
find /Applications/drive-offload.app \( -name "*.so" -o -name "*.dylib" \) \
    -exec codesign --force -s - {} \;
codesign --force -s - /Applications/drive-offload.app/Contents/Frameworks/Python.framework/Versions/3.13/Python
codesign --force -s - /Applications/drive-offload.app
```

The per-file signing pass matters: without it the kernel SIGKILLs the app at
launch (`last exit reason = OS_REASON_CODESIGNING`, `cs_invalid_page` on a
`Resources/*.so` in the system log). `codesign --deep` is not enough — it
skips Mach-O files under `Contents/Resources`, which is where py2app puts the
Python extension modules. Sign innermost-first, then the bundle.

It is an `LSUIElement` agent: no Dock icon, lives in the menu bar. Launchable
from Spotlight, `/Applications`, or the LaunchAgent below.

## State location when frozen

Run from the repo, the app keeps its state (`config.json`, `decisions.json`,
`app_state.json`, `app.log`) next to `offload_app.py`. Run as a frozen bundle,
writing inside `/Applications/...app/Contents/Resources` would be wrong, so
state lives in:

```
~/Library/Application Support/drive-offload/
```

On first frozen launch the app creates that directory if needed and starts
with fresh state — it does **not** copy anything over automatically. `todrive`
itself is still read from `Contents/Resources`. To carry over your current
setup, copy it manually:

```sh
mkdir -p ~/Library/Application\ Support/drive-offload
cp config.json decisions.json ~/Library/Application\ Support/drive-offload/
```

### How the frozen `todrive` finds that config

`config.json` is deliberately **not** shipped inside the bundle (it holds
user-specific state; the signed `.app` is reinstall-wiped). So `todrive`'s
`load_config()` resolves `config.json` from an ordered candidate list, taking
the first that exists and parses:

1. `$TODRIVE_CONFIG` — the menu-bar app exports this (pointing at the support
   dir `config.json`) on every `todrive` invocation, so parent and child
   provably read the same file;
2. `config.json` next to the `todrive` script — the run-from-source layout,
   which therefore still wins unchanged in dev;
3. `~/Library/Application Support/drive-offload/config.json` — the fallback
   that makes the `cp config.json …` step above sufficient for the frozen app,
   even when `todrive` is run directly from a terminal with no `$TODRIVE_CONFIG`.

Resolution is path/override based, not `sys.frozen` based: py2app runs
`todrive` as a plain script under `Contents/MacOS/python`, which never sets
`sys.frozen`. With no candidate present, `todrive` falls back to its built-in
defaults (`base_remote "gdrive:"`), exactly as before.

## Swap the LaunchAgent

The currently installed agent runs the repo script via the venv. Replace it
with the bundled-app template in `launchd/`:

```sh
launchctl unload ~/Library/LaunchAgents/com.driveoffload.app.plist
cp launchd/com.driveoffload.app.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.driveoffload.app.plist
```

The new agent execs `/Applications/drive-offload.app/Contents/MacOS/drive-offload`
with `KeepAlive` + `RunAtLoad`, and logs to
`~/Library/Application Support/drive-offload/launchd.{out,err}.log`.

## Regenerate the icon

The icon master is drawn with Pillow, then converted to `.icns` with
`iconutil`:

```sh
./.venv/bin/python assets/make_icon.py        # writes assets/icon_1024.png

cd assets
rm -rf drive-offload.iconset && mkdir drive-offload.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s icon_1024.png --out drive-offload.iconset/icon_${s}x${s}.png
  sips -z $((s*2)) $((s*2)) icon_1024.png \
      --out drive-offload.iconset/icon_${s}x${s}@2x.png
done
iconutil -c icns drive-offload.iconset -o drive-offload.icns
```

Then rebuild the app (`setup_app.py` points `iconfile` at
`assets/drive-offload.icns`).
