#!/bin/bash
# Start Career Agent automatically when you log in (macOS LaunchAgent), so it can
# check job sources and write drafts in the background. Undo with: scripts/uninstall-autostart.sh
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
[ -z "$UV" ] && { echo "uv not found. Run ./start.sh once first."; exit 1; }
PLIST="$HOME/Library/LaunchAgents/com.careeragent.app.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$DIR/data"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.careeragent.app</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string><string>run</string><string>python</string><string>-m</string><string>career_agent</string><string>--no-browser</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/data/career-agent.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/career-agent.log</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
</dict>
</plist>
PLIST
launchctl bootout "gui/$(id -u)/com.careeragent.app" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Career Agent now starts at login and runs in the background: http://127.0.0.1:8765"
