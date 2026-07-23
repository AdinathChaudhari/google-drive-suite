"""py2app build config for drive-offload.app (macOS menu-bar app).

Build a standalone bundle:
    ./.venv/bin/pip install rumps py2app
    ./.venv/bin/python setup_app.py py2app

Output:  dist/drive-offload.app   ->  drag to /Applications.

Fallback (references the source tree/venv, so the folder can't be moved):
    ./.venv/bin/python setup_app.py py2app -A

Custom icon: assets/drive-offload.icns (see BUILD.md for how to regenerate).
Comment out 'iconfile' below to fall back to a generic icon.
"""
from setuptools import setup

APP = ['offload_app.py']

# `todrive` is a plain Python script that offload_app.py invokes via
# subprocess with sys.executable; it resolves it as
# os.path.join(SCRIPT_DIR, "todrive"). py2app copies DATA_FILES into
# Contents/Resources — the same directory the main script runs from — so the
# existing SCRIPT_DIR join keeps working unchanged inside the bundle.
DATA_FILES = ['todrive']

OPTIONS = {
    # argv_emulation uses Carbon and can hang GUI apps; off for a menu-bar app.
    'argv_emulation': False,
    # App icon. Regenerate with: ./.venv/bin/python assets/make_icon.py then
    # rebuild the .icns (see BUILD.md). Comment out for a generic icon.
    'iconfile': 'assets/drive-offload.icns',
    'plist': {
        'CFBundleName': 'drive-offload',
        'CFBundleDisplayName': 'drive-offload',
        'CFBundleIdentifier': 'com.driveoffload.app',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        # Menu-bar agent: no Dock icon / app-switcher clutter. Still launchable
        # from Spotlight and /Applications.
        'LSUIElement': True,
    },
    # Copy rumps as a full package directory so its resources make it into
    # the bundle. The rest of offload_app.py is stdlib only.
    'packages': [
        'rumps',
    ],
}

setup(
    app=APP,
    name='drive-offload',
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
