"""py2app build config for drivecast.app (macOS menu-bar app).

Build a standalone bundle:
    ./venv/bin/pip install rumps py2app
    ./venv/bin/python setup_app.py py2app

Output:  dist/drivecast.app   ->  drag to /Applications.

Fallback (references the source tree/venv, so the folder can't be moved):
    ./venv/bin/python setup_app.py py2app -A

Custom icon (optional): create a drivecast.icns file and add
    'iconfile': 'drivecast.icns',
to OPTIONS below. A generic icon is used otherwise.
"""
from setuptools import setup

APP = ['drivecast_menubar.py']

# The web UI's static assets live inside the package at drivecast/static/, and
# server.py resolves them relative to its own __file__ (STATIC_DIR). Listing
# `drivecast` in `packages` makes py2app copy the entire package tree as a
# directory (not zipped) — including static/index.html, app.js and style.css —
# so STATIC_DIR resolves correctly inside the bundle. No DATA_FILES needed.
DATA_FILES = []

OPTIONS = {
    # argv_emulation uses Carbon and can hang GUI apps; off for a menu-bar app.
    'argv_emulation': False,
    # App icon. Regenerate with: ./venv/bin/python assets/make_icon.py then
    # rebuild the .icns (see assets/). Comment out to fall back to a generic icon.
    'iconfile': 'assets/drivecast.icns',
    'plist': {
        'CFBundleName': 'drivecast',
        'CFBundleDisplayName': 'drivecast',
        'CFBundleIdentifier': 'com.drivecast.app',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        # Menu-bar agent: no Dock icon / app-switcher clutter. Still launchable
        # from Spotlight and /Applications.
        'LSUIElement': True,
    },
    # Copy these as full package directories so dynamic imports (uvicorn's loop
    # and protocol submodules, starlette, etc.) and the local package's static
    # assets all make it into the bundle.
    'packages': [
        'drivecast',
        'uvicorn',
        'fastapi',
        'starlette',
        'anyio',
        'httpx',
        'httpcore',
        'h11',
        'click',
        'certifi',
        'idna',
        'pydantic',
        'pydantic_core',
        'rumps',
        # Lazy runtime import in server.py for the remote-access QR codes.
        'qrcode',
    ],
    # Modules the graph sometimes misses.
    'includes': [
        'annotated_types',
        'typing_extensions',
    ],
}

setup(
    app=APP,
    name='drivecast',
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
