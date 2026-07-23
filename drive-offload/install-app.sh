#!/usr/bin/env bash
# install-app.sh: set up the venv and install the drive-offload menu-bar app
# as a launchd LaunchAgent (runs at login, restarts if it dies).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.driveoffload.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
VENV="$SCRIPT_DIR/.venv"
PY="$VENV/bin/python3"
APP="$SCRIPT_DIR/offload_app.py"

# 1. venv + rumps
if [ ! -x "$PY" ]; then
    echo "Creating venv at $VENV"
    python3 -m venv "$VENV"
fi
echo "Installing rumps into the venv"
"$VENV/bin/pip" install --quiet rumps
"$PY" -c "import rumps" && echo "rumps import OK"

# 2. LaunchAgent
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
        <string>$PY</string>
        <string>$APP</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/launchd-app.out.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/launchd-app.err.log</string>
</dict>
</plist>
PLISTEOF

echo "Wrote $PLIST"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
echo "Bootstrapped $LABEL. Check status with: launchctl print gui/$UID/$LABEL"
