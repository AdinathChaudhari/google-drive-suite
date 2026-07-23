#!/usr/bin/env bash
# install-hub.sh: set up the dev venv and install gdrive-hub as a launchd
# LaunchAgent (runs at login, restarts if it dies).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LABEL="com.gdrivetoolkit.hub"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
VENV="$SCRIPT_DIR/venv"
# Invoke the interpreter directly (python -m gdrive_toolkit.hub) rather than the
# `gdrive-hub` shebang script. Under launchd, a script argv0 living in
# ~/Documents makes macOS TCC treat that path as the responsible executable and
# deny reads of the venv (EPERM on pyvenv.cfg); a real python binary as argv0
# avoids it (matches the pattern the predecessor menu-bar agent used).
PY="$VENV/bin/python"
LOG_DIR="$HOME/Library/Application Support/gdrive_toolkit/logs"

# 1. venv + editable install (dev mode: edits to the package take effect on
#    the next agent restart, no rebuild needed).
if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating venv at $VENV"
    python3 -m venv "$VENV"
fi
echo "Installing '.[dev]' into the venv"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e '.[dev]'
"$VENV/bin/python" -c "import rumps" && echo "rumps import OK"

# 2. LaunchAgent
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$LOG_DIR"

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
        <string>-m</string>
        <string>gdrive_toolkit.hub</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/agent.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/agent.log</string>
</dict>
</plist>
PLISTEOF

echo "Wrote $PLIST"

# 3. Load (robust to an already-loaded/stale agent from a prior install)
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo ""
echo "gdrive-hub installed and running as a LaunchAgent."
echo "Look for the 🧰 icon in your menu bar."
echo "Logs: $LOG_DIR/agent.log"
echo "Check status with: launchctl print gui/$(id -u)/$LABEL"
