#!/usr/bin/env bash
# uninstall-hub.sh: remove the gdrive-hub menu-bar LaunchAgent.
set -euo pipefail

LABEL="com.gdrivetoolkit.hub"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    echo "Removed $PLIST"
else
    echo "No plist at $PLIST"
fi
echo "Uninstalled $LABEL (the venv/ is left in place; delete it manually if desired)"
echo "This does NOT remove a Drivedeck.app you built with build-app.sh and copied to"
echo "/Applications — drag it to the Trash yourself if you want it gone too."
