#!/usr/bin/env bash
# build-app.sh: build the standalone Drivedeck.app (py2app) menu-bar bundle.
#
# Always builds from a clean, dedicated venv — NEVER the dev `-e` (editable)
# venv. py2app's module-graph analysis needs a real, non-editable install of
# gdrive_toolkit on disk to copy verbatim.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BUILD_VENV=".build-venv"

if [ ! -d "$BUILD_VENV" ]; then
    echo "Creating build venv at $BUILD_VENV"
    python3 -m venv "$BUILD_VENV"
fi

echo "Installing '.[hub]' + py2app into $BUILD_VENV (non-editable)"
"$BUILD_VENV/bin/pip" install --quiet --upgrade pip
"$BUILD_VENV/bin/pip" install --quiet '.[hub]' py2app

echo "Building dist/Drivedeck.app"
rm -rf build dist
"$BUILD_VENV/bin/python" setup_app.py py2app

echo ""
echo "Built: $SCRIPT_DIR/dist/Drivedeck.app"
echo "Copy it to /Applications, or run install-hub.sh for the LaunchAgent instead."
