#!/usr/bin/env bash
# uninstall-agent.sh: remove the drive-offload LaunchAgent.
set -euo pipefail

LABEL="com.driveoffload.watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    echo "Removed $PLIST"
else
    echo "No plist at $PLIST"
fi
echo "Uninstalled $LABEL"
