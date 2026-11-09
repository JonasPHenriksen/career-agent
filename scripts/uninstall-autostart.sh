#!/bin/bash
launchctl bootout "gui/$(id -u)/com.careeragent.app" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.careeragent.app.plist"
echo "Autostart removed."
