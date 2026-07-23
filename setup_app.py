"""py2app build config for the gdrive-hub menu-bar app.

Build a standalone bundle — see build-app.sh (NEVER build from the dev `-e`
venv; py2app needs a real, non-editable install of the package so its
module-graph analysis can find `gdrive_toolkit` to copy verbatim):

    python3 -m venv .build-venv
    ./.build-venv/bin/pip install '.[hub]' py2app
    ./.build-venv/bin/python setup_app.py py2app

Output: dist/Drivedeck.app -> drag to /Applications.

Custom icon (optional): create a Drivedeck.icns file and add
    'iconfile': 'Drivedeck.icns',
to OPTIONS below. A generic icon is used otherwise.
"""
from setuptools import setup

APP = ["hub_app.py"]

# Pure menubar app — no web UI, no static/ assets to bundle here (the
# downloader/uploader static/ dirs are inside the gdrive_toolkit package
# itself and come along for free via `packages=['gdrive_toolkit']` below).
DATA_FILES = []

OPTIONS = {
    # argv_emulation uses Carbon and can hang GUI apps; off for a menu-bar app.
    "argv_emulation": False,
    # Copy the whole gdrive_toolkit package tree verbatim (static/ and every
    # submodule) instead of relying on py2app's modulegraph analysis, which
    # can miss lazily-imported modules. This does NOT bundle flask/rumps/
    # requests themselves — the .app is menubar-only (hub_core + menubar,
    # rumps required) and launches the downloader/uploader web tools as
    # separate subprocesses (their own venv/console scripts), so those two
    # tools' flask/requests dependency never needs to live inside this
    # bundle. See "excludes" below.
    "packages": ["gdrive_toolkit"],
    # flask/requests are real dependencies of the downloader/uploader tools,
    # not of this menu-bar app — they're launched as subprocesses, never
    # imported in-process here. Exclude them so modulegraph doesn't warn
    # about being unable to resolve imports it will never need to bundle.
    "excludes": ["flask", "requests"],
    "plist": {
        "CFBundleName": "Drivedeck",
        "CFBundleDisplayName": "Drivedeck",
        "CFBundleIdentifier": "com.gdrivetoolkit.hub",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        # Menu-bar agent: no Dock icon / app-switcher clutter. Still launchable
        # from Spotlight and /Applications.
        "LSUIElement": True,
    },
}

setup(
    app=APP,
    name="Drivedeck",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
