#!/usr/bin/env bash
# uninstall-app.sh: remove the drive-offload menu-bar LaunchAgent.
set -euo pipefail

LABEL="com.driveoffload.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    echo "Removed $PLIST"
else
    echo "No plist at $PLIST"
fi
echo "Uninstalled $LABEL (the .venv is left in place; delete it manually if desired)"
