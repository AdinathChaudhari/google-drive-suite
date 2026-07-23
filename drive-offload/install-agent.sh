#!/usr/bin/env bash
# install-agent.sh: install the drive-offload launchd LaunchAgent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.driveoffload.watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
OFFLOADER="$SCRIPT_DIR/offloader.py"

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$OFFLOADER</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/launchd.err.log</string>
</dict>
</plist>
PLISTEOF

echo "Wrote $PLIST"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
echo "Bootstrapped $LABEL. Check status with: launchctl print gui/$UID/$LABEL"
