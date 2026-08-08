#!/bin/bash
# macOS convenience launcher: opens a new Ghostty tab, activates the venv,
# and starts Alfred. Double-click to run, or adapt for your own terminal app.

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"

osascript <<EOF
tell application "Ghostty"
    activate
end tell
delay 0.5
tell application "System Events"
    tell process "Ghostty"
        keystroke "n" using command down
        delay 0.5
        keystroke "cd '$DIR' && source venv/bin/activate && python run.py"
        key code 36
    end tell
end tell
EOF
